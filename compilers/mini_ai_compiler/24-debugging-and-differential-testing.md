# 第 24 章 调试与差分测试：定位误编译（miscompile）

> **目标**：当 C 或 LLVM 后端给出错误数值时，能用 IR dump、pass 分阶段对照和 op 级隔离把问题缩小到一条规则或一个索引；能搭一个随机图差分测试器，用解释器作金标准同时压 C 与 LLVM；能解释 NaN、别名、workspace 偏移、bias broadcast、stale plan 这几类常见 bug 的触发条件；能用真实错误信息判断问题在 verifier、pass、内存规划还是代码生成。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，llvmlite 0.50.0，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本章的差分测试、二分输出、NaN 对比和错误信息均来自本机实际运行。

## 为什么需要调试方法

编译器最危险的 bug 不是崩溃，而是**静默算错**。索引偏一位、bias 加到错误的列、workspace 复用了还在读的 buffer、LLVM 的 `fcmp` 把 NaN 归到零，这些都可能让程序正常返回，输出一个形状正确、数值错误的张量。没有对照，你甚至不知道它错了。

单元测试能覆盖已知路径，但覆盖不了组合爆炸：shape、broadcast、融合模式、内存复用、后端差异。差分测试（differential testing）用两个独立实现跑同一输入，比较输出；随机图测试（random graph testing）自动生成输入图，扩大覆盖；二分（bisection）在失败时快速缩小范围。三者组合，才能把「大概是 pass 的问题」变成「第 3 个 pass 的 bias 索引写错了」。

本章的工具都在仓库里：`graph.dump()`、`standard_pipeline().run(verbose=True)`、`evaluate_graph`、`max_abs_error`、`assert_close`、`llvm_ir`、`run_c_graph`。你不需要另造一套框架，按正确顺序使用它们即可。

## 先建立直觉

调试的核心是「用独立金标准逐步缩小分歧」：

```text
PyTorch 模型输出
      |
      |  参考实现（最高信任）
      v
解释器 evaluate_graph
      |
      |  C 后端输出 ── 比较 ──> 第一处分歧
      |  LLVM 后端输出 ── 比较 ──> 第一处分歧
      v
分阶段 IR
  raw -> constant_fold -> algebraic -> cse -> fuse -> dce
      |
      |  每个阶段都和 raw 的解释器结果比较
      v
第一个误差超过阈值的阶段
      |
      |  在该阶段的输入图上做 op 级隔离
      v
单条规则 / 单个索引 / 单个常量
```

「第一处分歧」比「最终误差」有用。最终误差 1.883e-01 只能告诉你结果错了；分阶段对照会告诉你是 `bad_drop_bias` 那一步引入的。op 级隔离再告诉你，是哪一个 `matmul_bias` 的 bias 没加。

另一条直觉是「三个实现，两个独立」：

```text
             PyTorch
            /       \
      解释器         C / LLVM
            \       /
             比较输出
```

PyTorch 和解释器共享 float32 语义，但实现路径不同；解释器和 C/LLVM 共享 IR，但执行方式不同。任意两条路径一致，第三条不一致，就能定位到「第三条独有的假设」。如果三条都不一致，先怀疑输入、shape 或参考值本身。

## 核心内容

### 1. 三个金标准与容差

本课有三层参考：

1. **PyTorch**：模型作者的原始语义。`demo_e2e.py` 先用 `model(x)` 得到 `expected`。
2. **解释器**：`evaluate_graph` 用 NumPy 逐算子执行，是 IR 语义的金标准。它不依赖 C/LLVM，适合定位后端问题。
3. **后端互比**：C 与 LLVM 的输出互相比对。它们共享同一张 IR 和同一个内存计划，适合发现某一后端的代码生成问题。

比较函数：

```python
def max_abs_error(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    return float(np.max(np.abs(_as_float32(a) - _as_float32(b)))) if a.size else 0.0
```

`max_abs_error` 只做最大绝对差。它适合快速报告，不适合作为唯一判据：一个大误差会掩盖很多小误差，NaN 会让结果变成 `nan`，形状不同会直接抛错。更完整的检查用 `assert_close`：

```python
def assert_close(actual, expected, *, atol=1e-5, rtol=1e-5) -> float:
    a = _f32(actual)
    b = _f32(expected)
    if a.shape != b.shape:
        raise AssertionError(f"shape mismatch: {a.shape} vs {b.shape}")
    err = float(np.max(np.abs(a - b))) if a.size else 0.0
    if not np.allclose(a, b, atol=atol, rtol=rtol):
        raise AssertionError(f"values differ: max_abs_error={err:.6g}, atol={atol}, rtol={rtol}")
    return err
```

容差选择：

- 解释器 vs PyTorch：`1e-5` 绝对+相对，覆盖 float32 累加顺序差异。
- C vs 解释器：`1e-6` 到 `1e-5`，同一 IR 下通常更接近。
- LLVM vs 解释器：`1e-6` 到 `1e-5`，同样取决于是否启用 fast-math。
- 融合前后：`1e-6`，融合不应改变数学结果，只改变中间存储。

容差不是越松越好。1e-3 能掩盖 bias 少加、索引偏一位这类错误；1e-6 又可能被正常的 float32 舍入误报。先用已知正确的实现测一遍，看正常误差分布，再定阈值。

### 2. IR dump：先看结构，再看数值

`Graph.dump()` 是最便宜的调试工具。MLP 的原始图：

```text
inputs:
  x0: tensor<4, 8>
constants:
  fc1_weight: tensor<16, 8> numel=128
  fc1_bias: tensor<16> numel=16
  linear_weight_t: tensor<8, 16> numel=128
  fc2_weight: tensor<4, 16> numel=64
  fc2_bias: tensor<4> numel=4
  linear_1_weight_t: tensor<16, 4> numel=64
ops:
  00 %linear_matmul = matmul(x0, linear_weight_t) : tensor<4, 16>
  01 %linear = add(linear_matmul, fc1_bias) : tensor<4, 16>
  02 %relu = relu(linear) : tensor<4, 16>
  03 %linear_1_matmul = matmul(relu, linear_1_weight_t) : tensor<4, 4>
  04 %linear_1 = add(linear_1_matmul, fc2_bias) : tensor<4, 4>
outputs:
  linear_1: tensor<4, 4>
```

优化后的图：

```text
ops:
  00 %relu_fused = matmul_bias_relu(x0, linear_weight_t, fc1_bias) : tensor<4, 16>
  01 %linear_1_fused = matmul_bias(relu_fused, linear_1_weight_t, fc2_bias) : tensor<4, 4>
outputs:
  linear_1_fused: tensor<4, 4>
```

先看三件事：

1. **op 数量**：5 → 2，说明融合发生了。
2. **op 类型**：两个 matmul 变体，bias 是否还在输入列表里。
3. **输出名和 shape**：`tensor<4,4>` 没变，避免把 shape 错误当成数值错误。

pass 的 verbose 输出给出每一步的变化：

```text
constant_fold                    ops   5 ->   5  changed=0
algebraic_simplify               ops   5 ->   5  changed=0
cse                              ops   5 ->   5  changed=0
fuse_ops                         ops   5 ->   2  changed=2
dce                              ops   2 ->   2  changed=0
```

如果 `fuse_ops` 显示 `5 -> 4`，说明只融合了一半；如果 `changed=2` 但 op 类型不对，说明匹配模式或替换逻辑有问题。dump 和 verbose 一起看，能快速区分「pass 没触发」和「pass 触发了但结果错」。

内存计划也是 dump 的一部分：

```text
arena_bytes=320 alignment=64 peak_live_bytes=320
value                 interval     size   offset
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

如果两个值的 interval 重叠但 offset 相同，`plan.validate()` 会报错；如果 interval 不重叠但 offset 意外相同，可能是复用策略生效。看 offset 时始终对照 interval，不要只看字节数。

### 3. 差分测试：解释器 vs 两个后端

一个最小的差分测试器：

```python
def check(graph, feed, *, atol=1e-5):
    ref = evaluate_graph(graph, feed)[0]
    c_out = run_c_graph(graph, feed, dll_path)[0]
    llvm_out = LLVMJIT(graph).run(feed)[0]
    return (
        max_abs_error(c_out, ref),
        max_abs_error(llvm_out, ref),
    )
```

真实使用时，`dll_path` 要先 `compile_graph_c` 生成。三个输出都按 `graph.outputs` 顺序取第一个；多输出时逐个比较。

随机图测试把「手写用例」换成「生成器 + 固定种子」。下面这个生成器覆盖 add/mul/relu/sum、广播常量和 keepdims：

```python
def random_graph(seed):
    rng = np.random.default_rng(seed)
    g = Graph()
    x = g.add_input("x", (2, 3))
    y = g.add_input("y", (2, 3))
    vals = [x, y]
    for i in range(int(rng.integers(4, 10))):
        kind = str(rng.choice(["add", "mul", "relu", "sum"]))
        if kind == "relu":
            v = g.add_op("relu", [vals[int(rng.integers(len(vals)))]])
        elif kind == "sum":
            v = g.add_op("sum", [vals[int(rng.integers(len(vals)))]], {"axis": 1, "keepdims": True})
        else:
            a = vals[int(rng.integers(len(vals)))]
            b = vals[int(rng.integers(len(vals)))]
            v = g.add_op(kind, [a, b])
        vals.append(v)
        if rng.random() < 0.3:
            c = g.add_const(f"c{i}", np.array(rng.uniform(-2, 2, 3), np.float32))
            vals.append(g.add_op("add", [vals[-1], c]))
    g.add_output(vals[-1])
    g.validate()
    return g
```

实测 20 个种子：

```text
seed=0 C err=0.000e+00
seed=1 C err=0.000e+00
seed=2 C err=0.000e+00
seed=3 C err=0.000e+00
seed=4 C err=0.000e+00
20 graphs: failures=0 worst_interp=0.000e+00 worst_llvm=0.000e+00 worst_c(first5)=0.000e+00
```

前 5 个种子同时跑了 C 后端，后 15 个只跑 LLVM；解释器在优化前后都比对了一次。全部误差 0，说明这组算子的 pass 流水线、内存规划和两个后端在当前 shape 下一致。注意：0 误差不代表所有输入都安全，只代表这 20 个种子、这个输入分布、这些 shape 没触发差异。随机测试的价值在于扩大覆盖，不在于证明「没有 bug」。

随机图的设计要点：

- **形状**：至少覆盖 `(2,3)`、`(1,3)`、`(2,1)`、标量常量；广播错误常在这里暴露。
- **算子**：add/mul/relu/sum 是基础；再加上 matmul、softmax、exp 才能覆盖后端的全部路径。
- **常量**：包含 0、1、-1、负数、零附近的值；代数化简的边界在这。
- **深度**：4–10 个 op 足够产生融合和复用；太浅覆盖不到，太深难以最小化。
- **种子**：固定种子可复现；失败后保存种子和输入，不要只保存「某次随机跑挂了」。
- **最小化**：失败图要缩到最少 op、最小 shape，再手工检查。

### 4. miscompile 二分：找到第一个坏阶段

先看一条正确的流水线。对 MLP 的原始图，按 pass 顺序逐个执行，每步都和原始解释器结果比较：

```text
stage 0 raw: 0.000e+00 ops 5
stage 1 after constant_fold: 0.000e+00 ops 5
stage 2 after algebraic_simplify: 0.000e+00 ops 5
stage 3 after cse: 0.000e+00 ops 5
stage 4 after fuse_ops: 0.000e+00 ops 2
LLVM vs raw interpreter: 2.980e-08
```

每一步都 0，说明当前 pass 序列在这张图上没有引入误差。LLVM 相对解释器的 2.980e-08 是浮点路径差异，远小于 1e-5 阈值。

现在故意写一个坏 pass，把 `matmul_bias` 的 bias 丢掉：

```python
def bad_drop_bias(g):
    changed = 0
    for op in list(g.ops):
        if op.kind != "matmul_bias":
            continue
        out = g.add_op("matmul", op.inputs[:2], name=op.output.name + "_nobias")
        g.replace_all_uses(op.output, out)
        g.remove_ops([op])
        changed += 1
    g.recompute_users()
    return changed
```

把它接在标准流水线之后：

```text
stage 0 raw: err=0.000e+00 ops 5
stage 4 after standard_pipeline: err=0.000e+00 ops 2
stage 5 after bad_drop_bias: err=1.883e-01 ops 2 changed 1
```

第一个坏阶段是 stage 5，误差 1.883e-01。二分结束，不需要再看后面的 dce 或后端。接下来检查这个阶段的输入图：`linear_1_fused` 原本是 `matmul_bias(relu_fused, W2, fc2_bias)`，被替换成 `matmul(relu_fused, W2)`，bias 输入消失。错误定位到规则本身，不是内存或 LLVM。

真实调试中，坏 pass 不会这么明显。流程仍然是：

1. **复现**：固定种子、固定输入、固定 shape，确认每次都能得到同样的错误。
2. **确认参考**：先比较 raw 图的两个实现，排除前端或输入错误。
3. **分阶段**：按 pass 顺序跑前缀，记录每步的 `max_abs_error` 和 op 列表。
4. **取第一个坏阶段**：它之前的阶段可信，之后的阶段不用看。
5. **看 before/after dump**：对比 op 类型、输入列表、attrs、shape。
6. **缩小图**：删掉无关分支、缩小 shape、把常量换成简单值，保留失败。
7. **op 级隔离**：把坏阶段前后的单个 op 拆成子图，分别用解释器和后端跑。
8. **修规则**：改匹配条件、替换逻辑或索引，再把原图加回回归测试。

第 6 步的「缩小图」就是 delta debugging 的思路：不断删减不相关部分，如果失败仍然存在，就保留删减。一个 50 op 的图最后可能缩成 3 op，问题就暴露出来。

op 级隔离的代码骨架：

```python
def prefix_graph(g, k):
    h = Graph()
    # 复制 inputs/constants，按原顺序重建前 k 个 op，把第 k 个 op 的输出设为输出
    ...
    return h
```

对每个 k，比较 `evaluate_graph(prefix)` 和 `LLVMJIT(prefix).run(feed)`。第一个不一致的 k 就是问题 op。注意：前缀图的 shape、内存计划和原图不同，误差可能因为复用策略变化而出现/消失；先确认失败在完整图上稳定，再用前缀定位。

二分不总是按 pass 顺序。也可以按 op 列表二分：把图切成两半，分别作为输出，比较两边。或者按 shape 二分：从 64 缩到 32、16、8，看错误从哪个尺寸开始。选择哪种二分，取决于你怀疑「规则错误」还是「边界/索引错误」。

### 5. 常见 bug 目录

**NaN 语义不一致。** 真实输出：

```text
interpreter: [nan  0.]
llvm: [0. 0.]
nan agree: False
```

解释器的 `np.maximum(NaN, 0)` 返回 NaN；C 的 `fmaxf(NaN, 0)` 和 LLVM 的 `fcmp ogt` + `select` 返回 0。三条路径对 NaN 的处理不同。如果你的模型可能产生 NaN，必须在规范里写清楚策略，并在测试里固定。`assert_close` 对这种差异的报错是：

```text
AssertionError: values differ: max_abs_error=nan, atol=1e-05, rtol=1e-05
```

`max_abs_error=nan` 不会告诉你哪个元素错了，只会告诉你「比较失效」。先用 `np.isnan` 定位，再决定是修实现还是修期望。

**workspace 偏移按字节/元素混用。** `plan_memory` 的 offset 是字节，GEP 对 `float*` 的索引是元素。少除 4 会让所有临时值错位。症状通常是「小图对、大图错」或「某些输出元素是别的值的片段」。检查点：`_ptr` 的 `off // 4`、`off % 4`、`arena_bytes // 4`。

**同一 op 内复用 buffer。** `plan_memory` 的释放条件是 `alloc.end < start`，不是 `<=`。如果两个值在同一 op 内一个被读、一个被写，`end == start`，不能复用。把条件放宽成 `<=`，会让 op 读到被覆盖的输入。`test_no_alias_for_live_values` 就是为这个写的：

```python
g = Graph()
x = g.add_input("x", (8,))
a = g.add_op("relu", [x])
b = g.add_op("relu", [a])
g.add_output(b)
plan = plan_memory(g)
self.assertNotEqual(plan.offset_of(a), plan.offset_of(b))
```

**bias broadcast 索引错。** `_emit_matmul` 用 `_broadcast_flat(bias.type.shape, (m, n), (i, j))` 算 bias 的扁平下标。bias 是 `(n,)` 时，`j` 直接对应；bias 是 `(1,n)` 时，第一维 stride 0；bias 是 `(m,n)` 时，需要 `i*n+j`。如果手写成 `bias[j]`，`(m,n)` 的 bias 只会用第一行。症状：输出每一行都加了同一个 bias，误差随行数增大。

**stale plan / stale JIT。** 先 `plan_memory(g)` 或构造 `LLVMJIT(g)`，再修改图（加 op、换输出、改常量），旧 plan 的 offset 和旧 JIT 的机器码都不再匹配。症状：改图后结果还是旧值，或 workspace 越界。规则：图变了就重新 plan、重新 JIT；不要在可变图上缓存分析结果。

**输出拷贝遗漏。** `_copy_outputs` 按 `graph.outputs` 顺序把值拷到输出参数。如果新增输出但忘了在 `run` 里创建对应数组，ctypes 参数个数对不上；如果输出是输入本身，`_ptr` 返回输入参数，拷贝仍然要发生（调用者可能传入只读缓冲）。检查签名参数个数和 `outputs` 列表长度。

**C 与 LLVM 的浮点差异。** C 的 `/fp:fast` 允许重排和融合，LLVM 默认更保守；`fma` 会把 `a*b+c` 合成一条指令，改变舍入。症状：误差在 1e-7 到 1e-6，随 shape 增大。用 `1e-5` 容差通常能过；如果要求逐位一致，必须关闭 fast-math/FMA，或统一两条后端的浮点选项。

### 6. 真实错误信息：先判断在哪一层

**缺失输入**：

```text
KeyError: "missing input 'x'; got []"
```

来自 `evaluate_graph` 的输入检查。说明 feed 的键和 `graph.inputs` 的名字不一致；先打印 `[v.name for v in graph.inputs]`。

**坏拓扑序**：

```text
IRError: op relu uses relu_0 before it is defined
```

来自 `Graph.validate`。说明 op 列表的顺序被破坏，或者手工构造图时先用了后定义的值。LLVM 后端在 `build()` 第一行调用 validate，所以这个错误会阻止 IR 生成。

**广播失败**：

```text
IRError: shapes are not broadcastable: ((2, 3), (4,))
```

来自 `broadcast_shape`。`(2,3)` 和 `(4,)` 在最后一维 3 vs 4 不兼容。先检查常量 shape，再检查 `add_op` 的 `shape=` 参数是否和推断一致。

**断言失败**：

```text
AssertionError: values differ: max_abs_error=nan, atol=1e-05, rtol=1e-05
```

来自 `assert_close`。`nan` 说明有 NaN 或 Inf，先定位元素；如果是有限值，报告会给出具体 max_abs_error，用它和容差比较。

错误信息的第一行通常已经给出层级：`IRError` 在 IR/verifier，`KeyError` 在输入，`AssertionError` 在数值比较，`RuntimeError` 在 C 编译或 JIT。不要一看到错误就去改后端；先确认它来自哪一层。

## 底层机制：bug 从哪一层进入

| 层 | 典型 bug | 第一处异常信号 | 首先检查 |
| --- | --- | --- | --- |
| 前端 | 权重没转置、`nn.Linear` 展开错 | 解释器 vs PyTorch 就不一致 | `from_torch` 的 `w_t`、shape |
| IR/verifier | 拓扑序、SSA、shape 推断 | `IRError` | `graph.dump()`、`validate()` |
| Pass | 融合丢输入、代数化简过激 | 某阶段后误差跳变 | 阶段前缀、before/after dump |
| 内存 | 同 op 复用、byte/element 混用 | 小图对、大图错 | interval、offset、`off // 4` |
| C 代码生成 | 索引、bias、输出拷贝 | C 错、LLVM 对 | 生成的 `.c`、`build.log` |
| LLVM 代码生成 | GEP、workspace、签名 | LLVM 错、C 对 | `llvm_ir`、`verify()` |
| Runtime | ctypes 参数、数组生命周期 | 崩溃或垃圾值 | 参数个数、`data_as`、局部变量 |

定位顺序建议从最靠近输入的一层开始：

1. 先确认 PyTorch 和解释器一致。不一致，问题在前端或 IR 语义。
2. 再确认解释器在 pass 前后一致。不一致，问题在 pass。
3. 再确认 C 和 LLVM 各自与解释器一致。只有一个后端错，问题在该后端的代码生成或 ABI。
4. 两个后端都错且误差相同，问题更可能在 IR、内存计划或共享的常量。

op 级隔离的关键是「一次只改变一个变量」。不要同时改 shape、pass 和 backend；否则误差变化无法归因。把图缩到最小后，保留一个 op、一个常量、一个输入，逐项加回，观察哪一项让误差出现。

内存 bug 还可以用 Ch 15 的 debug allocator/canary 辅助：在 arena 前后填哨兵值，函数返回后检查哨兵是否被改写；把每个 allocation 的 offset/size 打印出来，和 `plan.summary()` 对照。canary 被改写说明越界；输出值像别的张量片段，说明别名。两者都比「盯着数值猜」快。

## 进一步用法与变体

**属性测试。** 用 Hypothesis 生成 shape、常量、op 序列，自动缩小失败用例。它的 `@given` 和 `example` 能把随机失败保存成固定回归。当前随机生成器是手写的，换成 Hypothesis 后覆盖面更广，但需要控制生成空间，避免产生不支持的类型。

**IR golden test。** 对固定图保存 `graph.dump()` 和 `llvm_ir` 的哈希或关键行，pass 改动后对比。它抓的是结构变化，不是数值变化；两者互补。不要保存整个 6413 行 IR 做逐字比较，容易因 llvmlite 版本或临时值编号变化而误报；保存 op 序列、签名、关键 GEP 和统计量更稳。

**`opt -print-after-all` 类比。** 本课的 `PassManager.run(verbose=True)` 只打印 op 数变化。可以在每个 pass 后调用 `graph.dump()` 写到文件，形成 pass 快照。LLVM 侧可以用 `binding` 的 pass manager 加打印，或把模块文本写到临时文件。

**Sanitizer / 调试分配器。** C 侧可以用 `/fsanitize=address`（clang）或 CRT 的 `_CrtSetDbgFlag` 检查越界和泄漏；LLVM JIT 侧没有现成的 ASan 集成，主要靠 workspace 大小检查、canary 和差分测试。Ch 15 的手写 debug allocator 是最轻的方案。

**崩溃 triage。** 如果 JIT 调用直接崩溃，先检查：函数地址是否非零、参数个数是否匹配、数组是否存活、workspace 是否足够、图是否在 JIT 后被修改。用最小恒等图确认 JIT 本身可用，再逐步加 op。

**CI 矩阵。** 在 CI 里跑三组：解释器 vs PyTorch、C vs 解释器、LLVM vs 解释器；再加随机种子和 shape 矩阵。每组都保存 `maxerr` 和失败种子。不要只在本地跑一次 demo 就认为后端稳定。

## 对比与取舍

| 方法 | 覆盖 | 定位速度 | 成本 | 适用 |
| --- | --- | --- | --- | --- |
| 手写单元测试 | 窄而深 | 快 | 低 | 已知边界 |
| 随机图差分 | 宽而浅 | 中 | 中 | 回归、探索 |
| 分阶段二分 | 依赖复现 | 快 | 低 | pass 引入的错误 |
| op 级隔离 | 精确 | 中 | 中 | 索引/规则错误 |
| golden IR | 结构 | 快 | 低 | pass 重构 |
| 属性测试 | 很宽 | 慢 | 高 | 发布前 |

差分测试的盲区是「两个实现共享同一个错误」。比如解释器和 C 都用同一个 `broadcast_strides`，如果 stride 逻辑错，两者会一起错；这时 PyTorch 参考值才能发现。反过来，PyTorch 和解释器都用 NumPy 的广播语义，如果 IR 的 shape 推断错了，后端可能一起错。三层参考要尽量独立，不要只比较两个共享代码的实现。

容差也有取舍：太松会漏 bug，太紧会把正常舍入当 bug。先用已知正确实现测误差分布，再定阈值；对 NaN/Inf 单独写策略，不要用 `equal_nan=True` 掩盖所有差异。

## 踩雷集锦

1. **只比较最终输出，不做分阶段对照。** 误差 1.883e-01 只能说明「错了」，不能说明「哪一步错」。先跑 pass 前缀，把范围缩到一个阶段。
2. **用 `==` 或 `max(abs())` 比较含 NaN 的输出。** 结果是 `nan`，条件永远不成立。先检查 `np.isnan`/`np.isinf`，再决定期望语义。
3. **在可变图上缓存 plan 或 JIT。** 改图后旧 offset/机器码失效，症状是「改了代码没生效」或越界。图变即重建。
4. **随机测试只用一个 shape，失败后又不保存种子和输入。** `(2,3)` 覆盖不到 rank-0、rank-1、广播、keepdims、大 shape；只留下「刚才跑挂了一次」也无法复现。至少覆盖 3 种 rank 和 2 种广播方向，失败时打印 seed、shape、feed 摘要和第一个坏阶段的 dump。
5. **把编译错误当成数值错误。** `cl` 失败、DLL 链接失败、JIT verify 失败都发生在运行前，先读构建日志/异常文本，不要直接调后端。

## 进阶

- 写一个最小化器：从失败图开始，反复删除 op、缩小 shape、把常量替换成 0/1，如果失败仍存在就保留简化；最后输出最小复现。
- 写一个 op 级差分器：对每个前缀图同时跑解释器和后端，打印第一个不一致的 op 编号、输出元素和误差。
- 用 Hypothesis 生成随机图，设置 `deadline=None`，把失败用例写入 `tests/regressions/`。
- 给 C 后端加 `_CrtSetDbgFlag` 或 ASan，跑一组越界/别名用例，确认 canary 能抓到。
- 用 LLVM 的 `verify()` 和 `opt -verify-each` 检查每个 pass 后的 IR；故意插入一个坏 phi，记录错误。
- 把 NaN 策略写成显式配置，分别实现 `propagate` 和 `zero`，用差分测试固定两条路径的行为。

## 动手练习

1. **复现分阶段二分。** 对 MLP 跑 `standard_pipeline` 的每个前缀，记录误差和 op 数。再用 `bad_drop_bias` 制造失败，确认第一个坏阶段是 5。
2. **写随机图。** 用本章生成器跑 50 个种子，覆盖 `(2,3)`、`(1,3)`、`(2,1)` 三种 shape。报告失败种子和最大误差。
3. **制造 NaN。** 用 `[NaN, -1]` 跑 `relu`，记录解释器和 LLVM 的输出；给 `assert_close` 加 NaN 检查，输出具体坏元素的下标。
4. **缩小失败图。** 构造一个 10 op 的图，故意把某个 `matmul_bias` 的 bias 去掉；用删除 op/缩小 shape 的方式缩到 2 op，保留误差。
5. **检查 workspace。** 对一个复用 buffer 的图，打印 `plan.summary()`，确认 `end < start` 的两个值 offset 相同；把条件改成 `<=`，跑差分测试，记录哪个输出开始错。
6. **写回归测试。** 把本章的 NaN 案例和 `bad_drop_bias` 案例写成 unittest，确保以后改 pass 或后端时能被抓到。

## 本章重点

- 编译器 bug 多数是静默数值错误，必须用独立金标准做差分测试。
- 三层参考：PyTorch、解释器、C/LLVM；先确认前两层一致，再查后端。
- `graph.dump()`、pass verbose、`llvm_ir`、`plan.summary()`、生成的 `.c` 是最常用的五个 dump。
- 分阶段二分找第一个坏 pass；op 级隔离找具体规则或索引；delta debugging 缩小失败图。
- 随机图测试要固定种子、覆盖多 shape、保存失败输入；0 误差只代表当前分布没触发问题。
- NaN 是真实的语义差异：解释器传播，C/LLVM 的 ReLU 归零；必须显式定义策略。
- 内存 bug 优先看 interval、offset、`end < start` 和 `off // 4`，不要先怀疑 LLVM。

## 自我检核

- [ ] 我能说出三个金标准，以及先比较哪两个。
- [ ] 我能用 `graph.dump()` 和 pass verbose 判断融合是否发生。
- [ ] 我能写出一个分阶段二分脚本，找到第一个坏 pass。
- [ ] 我能把失败图缩到最小，并保存种子和输入。
- [ ] 我知道 NaN 在解释器、C、LLVM 三条路径上的差异。
- [ ] 我能解释同 op 复用为什么必须用 `end < start`。
- [ ] 我能从错误信息判断问题在 IR、输入、数值比较还是构建。
- [ ] 我能为每个修过的 bug 补一个回归测试。

## 延伸阅读

- **Zeller 的 Delta Debugging 论文**（https://www.st.cs.uni-saarland.de/papers/tse2002/）：读最小化算法和 `ddmin` 的思路；本章的「删 op、缩 shape」是它的手工版本。
- **Hypothesis 文档，Property-based testing**（https://hypothesis.readthedocs.io/）：读 `@given`、策略组合、失败用例保存；把本章的随机生成器升级成可缩小、可复现的属性测试。
- **LLVM 文档，`opt -print-after-all` 与 `-verify-each`**（https://llvm.org/docs/CommandGuide/opt.html）：读 pass 调试和 IR 校验选项；对应本章的 pass 快照和 `verify()`。
- **Google Sanitizers 文档**（https://github.com/google/sanitizers）：读 AddressSanitizer、UndefinedBehaviorSanitizer 的用法；为 C 后端的越界和 UB 检查提供工具。
- **《Software Testing: A Craftsman's Approach》第 3 部分**：读等价类、边界值、随机测试和差分测试；本章的 shape/常量/种子设计对应这些测试设计方法。

---

[Final Project：端到端 mini AI 编译器](./final-project-mini-ai-compiler.md)