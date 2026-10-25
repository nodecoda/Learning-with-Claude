# 第 10 章 算子融合：少一次内存往返，还是多一份寄存器压力

> **目标**：读懂 `fuse_ops` 支持的三条融合模式（`matmul_bias_relu`、`matmul_bias`、`add_relu`）以及它拒绝融合的五道合法性门槛；用真实 IR 对比融合前后，用真实 benchmark 说明融合什么时候赚、什么时候不赚；能自己写下一条新模式的匹配条件和插入位置。

> **环境**：Windows 10/11 x86_64，Python 3.11.12，numpy 2.4.6，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

benchmark 部分要编译 DLL，需要 MSVC；`code/mini_ai/codegen_c.py` 会自动查找 `vcvars64.bat`。本章 Python 代码块按顺序在同一个会话中执行，后文沿用前文的导入。

## 为什么需要这个？

`relu(x @ W + b)` 在 IR 里是三条 op：`matmul`、`add`、`relu`。按 C 后端的实现，这是三个独立的嵌套循环：

```text
op0: matmul  读 x, W        写 t1
op1: add     读 t1, b       写 t2
op2: relu    读 t2          写 t3
op9: copy    读 t3          写 out
```

`t1`、`t2`、`t3` 各是一次全量写加一次全量读。融合（fusion）要做的事，是把这三条 op 合成一个 kernel，让中间值留在寄存器里：

```text
op0': matmul_bias_relu  读 x, W, b   写 t1   （循环内累加、加 bias、取 max）
op9': copy              读 t1         写 out
```

收益来自减少内存往返和减少循环开销；代价是 kernel 变复杂、可用的优化空间变小、原本能调库的 matmul 变成手写循环。这一章要看的是**这两笔账怎么算**，而不是“融合一定更快”。

先给结论，后面用实测支撑：

- 对纯 elementwise 链，融合稳定有收益（本章实测 `1.20x`）。
- 对 matmul epilogue，中间张量小到能待在 cache 里时，融合可能白干甚至略慢（本章实测 `0.98x`）。
- 融合的合法性比收益更难写对：多用户、形状、消费关系三者任一不满足，融合出来的图就是错的或亏的。

## 先建立直觉：融合是把“生产者和消费者”圈进同一个循环

```text
模式 1（最高优先级）
  x ──┐
  W ──┼─> matmul ──> add(b) ──> relu ──> y
  b ──┘        └──────── 三条 op 合成一条 ────────┘

模式 2
  x ──┐
  W ──┼─> matmul ──> add(b) ──> y
  b ──┘        └──── 两条 op 合成一条 ────┘

模式 3
  x ──┐
  c ──┼─> add ──> relu ──> y
      └──── 两条 op 合成一条 ────┘
```

三条模式不是并列的，而是**按优先级依次尝试**。原因在模式 3 的匹配条件里：`add` 的两个输入都不能来自 `matmul` 产出。如果不先跑模式 1 和模式 2，模式 3 会去抢 `matmul + add + relu` 里的 `add + relu`，把 `matmul` 留在外面，融合出来的图反而更差。

再看“能不能融”的四道门：

```text
生产者 → 消费者
  [1] 生产者只有一个用户？
  [2] 消费者只有一个用户（或被当成图输出）？
  [3] 形状/广播关系合法？
  [4] 这条模式没被更高优先级的模式占用？
```

任何一道不满足，`fuse_ops` 就跳过这一处，继续看下一个候选。它不会报错，也不会部分融合。

## 核心概念一：三条模式的输入、输出与形状规则

`code/mini_ai/passes.py` 的 `fuse_ops` 文档字符串写明了支持范围：

```python
def fuse_ops(graph: Graph) -> int:
    """Fuse common producer chains.

    Supported patterns:
      matmul + add + relu -> matmul_bias_relu
      matmul + add        -> matmul_bias
      add + relu          -> add_relu
    """
```

形状规则由 `infer_output_type` 统一处理，融合后的 op 与融合前形状一致：

| 融合后 op | 输入 | 输出形状 | 语义 |
|---|---|---|---|
| `matmul_bias_relu` | `a, b, bias` | `(a.shape[0], b.shape[1])` | `relu(a @ b + bias)` |
| `matmul_bias` | `a, b, bias` | `(a.shape[0], b.shape[1])` | `a @ b + bias` |
| `add_relu` | `x, y` | `broadcast(x.shape, y.shape)` | `relu(x + y)` |

`infer_output_type` 对三个 matmul 变体共用一段检查：

```python
    if kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
        if len(inputs) not in {2, 3}:
            raise IRError(f"{kind} expects 2 or 3 inputs, got {len(inputs)}")
        a, b = inputs[0].shape, inputs[1].shape
        if len(a) != 2 or len(b) != 2:
            raise IRError(f"{kind} currently supports 2-D matrices, got {a} and {b}")
        if a[1] != b[0]:
            raise IRError(f"matmul inner dimensions differ: {a} @ {b}")
        out_shape = (a[0], b[1])
        if len(inputs) == 3:
            # Bias must broadcast to the output shape.
            broadcast_shape(inputs[2].shape, out_shape)
        return TensorType(out_shape)
```

最后那行 `broadcast_shape(inputs[2].shape, out_shape)` 是融合安全的基础：bias 必须是 `(N,)`、`(1, N)`、`(M, N)` 或标量之一，否则 `add` 原始节点在建图时就已经被 `add_op` 拒绝，融合阶段不会遇到这种图。

## 核心概念二：五道合法性门槛

`fuse_ops` 先给用户表拍快照，然后逐个模式扫描。模式 1 的完整判定是这样的（节选，保留原始缩进）：

```python
    for relu in graph.ops:
        if relu.kind != "relu" or id(relu) in consumed:
            continue
        add = relu.inputs[0].producer
        if add is None or add.kind != "add" or id(add) in consumed:
            continue
        matmul_candidates = [(i, v) for i, v in enumerate(add.inputs) if v.producer is not None and v.producer.kind == "matmul"]
        if len(matmul_candidates) != 1:
            continue
        mm_idx, mm_out = matmul_candidates[0]
        mm = mm_out.producer
        assert mm is not None
        if len(users[id(mm_out)]) != 1 or len(users[id(add.output)]) != 1:
            continue
        if len(users[id(relu.output)]) == 0 and relu.output not in graph.outputs:
            continue
```

逐条读：

1. `relu.inputs[0].producer` 必须是 `add`。这要求 `relu` 的唯一输入就是 `add` 的输出，也就是融合链中间没有别的分支。
2. `matmul_candidates` 必须恰好一个。`add(bias, mm)` 和 `add(mm, bias)` 都支持，`add(mm1, mm2)` 不支持 —— 两个 matmul 相加没有对应的融合 op。
3. `len(users[id(mm_out)]) != 1` 拒绝多用户。matmul 的结果如果还被别的 op 读（例如 `sum`、另一个分支），就不能吞进 epilogue，否则那个用户的输入就没了。
4. `len(users[id(add.output)]) != 1` 同理，`add` 的结果只能被这一个 `relu` 读。
5. 最后一条是死代码检查：`relu` 的结果既没人用、也不是图输出，那这条链整体是死的，交给 DCE，融合没有意义。

模式 2（`matmul_bias`）多一条反向保护：

```python
        # Leave the add alone if a relu consumes it; the first pattern may still
        # be rejected because the matmul has other users.
        if any(u.kind == "relu" for u in users[id(add.output)]):
            continue
```

它的含义是：如果 `add` 后面跟着 `relu`，但模式 1 因为别的原因被拒了（通常是 matmul 多用户），那么**也不要**只融 `matmul + add`。只融一半会把 `relu` 留在外面，`relu` 还得单独跑一遍、单独读写一次中间张量；而且融合后 `add` 消失，`add_relu` 也没法再匹配。宁可整条链都不融。

模式 3（`add_relu`）的门槛是“`add` 的两个输入都不是 matmul 产出”：

```python
        add = relu.inputs[0].producer
        if add is None or add.kind != "add" or id(add) in consumed:
            continue
        if any(v.producer is not None and v.producer.kind == "matmul" for v in add.inputs):
            continue
        if len(users[id(add.output)]) != 1:
            continue
```

这条检查防止模式 3 抢走本该属于模式 1/2 的 `add`。

## 核心概念三：真实 IR 前后对比

### 模式 1：`matmul + add + relu`

构造三条 op 的图，先 dump 一次；调用 `fuse_ops` 后再 dump 一次。输出里 `changed: 1` 之前是融合前，之后是融合后：

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.passes import fuse_ops

g = Graph()
x = g.add_input("x", (2, 4))
w = g.add_const("w", np.arange(12, dtype=np.float32).reshape(4, 3) / 10)
b = g.add_const("b", np.array([0.5, -0.5, 1.0], np.float32))
mm = g.add_op("matmul", [x, w], name="mm")
add = g.add_op("add", [mm, b], name="bias_add")
r = g.add_op("relu", [add], name="act")
g.add_output(r)
print(g.dump())
print("changed:", fuse_ops(g))
print(g.dump())
```

```text
inputs:
  x: tensor<2, 4>
constants:
  w: tensor<4, 3> numel=12
  b: tensor<3> numel=3
ops:
  00 %mm = matmul(x, w) : tensor<2, 3>
  01 %bias_add = add(mm, b) : tensor<2, 3>
  02 %act = relu(bias_add) : tensor<2, 3>
outputs:
  act: tensor<2, 3>
changed: 1
inputs:
  x: tensor<2, 4>
constants:
  w: tensor<4, 3> numel=12
  b: tensor<3> numel=3
ops:
  00 %act_fused = matmul_bias_relu(x, w, b) : tensor<2, 3>
outputs:
  act_fused: tensor<2, 3>
```

`changed=1`，三条 op 变一条。三个常量里 `w` 与 `b` 都还在用，没有孤儿被清掉。输出名从 `act` 变成 `act_fused`，因为 `_new_fused_op` 用 `f"{relu.output.name}_fused"` 命名，然后 `replace_all_uses` 把 `graph.outputs` 里的 `act` 换成了新值。

数值对齐：融合前后 `max_abs_error` 为 `0.0`。这不是巧合。`apply_op` 的 `matmul_bias_relu` 分支与三条 op 的顺序完全一致：

```python
        out = _as_float32(a[0] @ a[1])
        if kind in {"matmul_bias", "matmul_bias_relu"}:
            out = _as_float32(out + a[2])
        if kind == "matmul_bias_relu":
            out = _as_float32(np.maximum(out, np.float32(0.0)))
```

先 matmul、再加 bias、再取 max，与三条 op 的累加顺序相同。C 后端也一样：先在 `acc` 里累加，再 `acc += bias`，再 `fmaxf(acc, 0.0f)`，最后一次性写入。**融合不改变累加顺序**，所以结果是逐位相同的；如果融合把 `acc` 改成按 tile 分块累加，就会出现末位差异（第 18 章会看到这种差异）。

### 模式 3：`add + relu`（模式 2 的 `matmul_bias` 见练习 B）

```python
g2 = Graph()
x2 = g2.add_input("x", (2, 3))
c2 = g2.add_const("c", np.full((3,), 0.25, np.float32))
a2 = g2.add_op("add", [x2, c2], name="shift")
r2 = g2.add_op("relu", [a2], name="act2")
g2.add_output(r2)
print("changed:", fuse_ops(g2))
print(g2.dump())
```

```text
changed: 1
inputs:
  x: tensor<2, 3>
constants:
  c: tensor<3> numel=3
ops:
  00 %act2_fused = add_relu(x, c) : tensor<2, 3>
outputs:
  act2_fused: tensor<2, 3>
```

注意 `add_relu` 的两个输入保留了原始 `add` 的输入顺序：`(x, c)`，不是 `(c, x)`。C 后端的 `add_relu` 分支按 `a[0] + a[1]` 计算，加法交换律让顺序不影响结果；但如果以后加入 `sub_relu` 这类非交换模式，输入顺序就必须原样保留。

## 核心概念四：被拒绝的融合长什么样

给 matmul 加第二个用户：

```python
g3 = Graph()
x3 = g3.add_input("x", (2, 4))
w3 = g3.add_const("w", np.ones((4, 3), np.float32))
b3 = g3.add_const("b", np.ones((3,), np.float32))
mm3 = g3.add_op("matmul", [x3, w3], name="mm3")
add3 = g3.add_op("add", [mm3, b3], name="bias")
r3 = g3.add_op("relu", [add3], name="act3")
s3 = g3.add_op("sum", [mm3], {"axis": 1}, name="mm_sum")
g3.add_output(r3)
g3.add_output(s3)
print("changed:", fuse_ops(g3))
print([op.kind for op in g3.ops])
```

```text
changed: 0
['matmul', 'add', 'relu', 'sum']
```

四条 op 一条没动。三种模式各被拒绝一次：

- 模式 1：`len(users[id(mm3)]) == 2`（`bias` 和 `mm_sum`），拒绝。
- 模式 2：`add` 后面跟着 `relu`，那条保护条件成立，拒绝。
- 模式 3：`add` 的输入里有 matmul 产出，拒绝。

这就是 `changed=0` 的完整解释。如果只看“有个 `matmul + add + relu` 链”，会以为这里能融；实际约束是**生产者的所有用户都必须在这条链上**。

## 底层机制：两阶段提交与插入位置

融合不能边遍历边改图。`fuse_ops` 用的是“先记录、后重写”：

```python
    graph.recompute_users()
    users = {id(v): list(v.users) for v in graph.values}
    consumed: set[int] = set()
    replacements: dict[int, Value] = {}
    insert_at: dict[int, Op] = {}
    count = 0
```

- `users` 是快照，因为后面 `graph.ops` 会被重建，实时查 `v.users` 会拿到中间状态。
- `consumed` 记录“这些 op 将被删掉”，避免同一条 op 被两个模式重复消费。
- `replacements` 是“旧输出值 → 新输出值”，最后一次性重写所有 op 的输入、`graph.outputs`、以及融合 op 自己的输入。
- `insert_at` 记录“融合 op 插到哪个旧 op 的位置”。

插入位置必须是最外层 op（`relu` 或 `add`）的位置，因为融合 op 读取的是**最内层**的输入（`mm.inputs` 与 `bias`），这些值在更早的位置就已经定义；放在最外层位置，拓扑序天然成立。代码里对应的就是：

```python
    for op in graph.ops:
        if id(op) in consumed:
            if id(op) in insert_at:
                new_ops.append(insert_at[id(op)])
        else:
            new_ops.append(op)
    graph.ops = new_ops
    graph.recompute_users()
    graph.dce()
```

`_new_fused_op` 直接构造 `Op`，没有走 `add_op`：

```python
def _new_fused_op(graph: Graph, kind: str, inputs: list[Value], out_type, name: str) -> Op:
    out = graph._new_value(name, out_type, kind="temp")
    op = Op(kind=kind, inputs=inputs, outputs=[out], attrs={}, id=graph._next_op_id)
    graph._next_op_id += 1
    out.producer = op
    return op
```

绕开 `add_op` 的理由：`add_op` 会调用 `infer_output_type` 重新推断形状，而融合 op 的形状已经由被替换的最外层 op 保证（`relu.output.type` 或 `add.output.type`）。代价是这里**跳过了验证**：如果模式匹配写错、把 `(M, N)` 的 bias 塞进一个 `(M, K)` 的输出，`_new_fused_op` 不会报错，只会在很久以后变成数值错误。这是本实现里风险最高的一段代码。

## 收益与代价：两组真实测量

环境：本机 Windows，MSVC `/O2 /fp:fast`，`runtime.benchmark` 取中位数；每组先 warmup 再重复。两个后端产物都经过 `run_c_graph`（含每次调用的 `ascontiguousarray` 与输出拷贝）。

### 纯 elementwise 链（内存受限，融合应该赚）

图：`relu(x + c)`，`x` 是 `(1024, 1024)`，`c` 是 `(1024,)`。

```text
unfused median ms: 36.2336   fused median ms: 30.1329   speedup: 1.202
```

融合后 C 代码只剩一个循环：

```c
    /* add_relu: act_fused */
    for (int i2_0 = 0; i2_0 < 1024; ++i2_0) {
        for (int i2_1 = 0; i2_1 < 1024; ++i2_1) {
            v_4_act_fused[(i2_0)*1024 + i2_1] = fmaxf(v_0_x[(i2_0)*1024 + i2_1] + v_1_c[i2_1], 0.0f);
        }
    }
```

未融合时是两个循环，中间经过 `v_2_bias`：

```c
    /* add: bias */
    for (int i0_0 = 0; i0_0 < 1024; ++i0_0) {
        for (int i0_1 = 0; i0_1 < 1024; ++i0_1) {
            v_2_bias[(i0_0)*1024 + i0_1] = v_0_x[(i0_0)*1024 + i0_1] + v_1_c[i0_1];
        }
    }
```

`1.20x` 而不是理论上的 1.5–2x，原因在于：两个版本都要在函数末尾把结果拷贝到 `out0`（各 4 MB），而融合省下的是中间张量的写回与重读，输入读取和输出拷贝这两笔开销没有变化。

### matmul epilogue（计算受限，融合可能白干）

图：`relu(x @ W + b)`，`M=128, K=256, N=256`。权重与 bias 是常量，所以比较的是“三个循环”与“一个融合循环”。

```text
unfused median ms: 23.6812   fused median ms: 24.1972   speedup: 0.979
```

融合版反而慢了约 2%。两个数值误差都是 `4.9591e-05`，来自 C 与 NumPy 的累加差异，与融合无关。

可能的原因：中间张量 `(128, 256)` 是 128 KB，整个工作集能待在 L2 里，再省一次读写几乎不省时间；而 matmul 本身是 128×256×256 ≈ 8.4M 次乘加，计算时间占绝对主导。融合版把 bias 加法与 `fmaxf` 收进同一个循环，内层循环的代码体积变大，向量化与寄存器分配的空间变小，净收益就可能变成负数。

这组数据要表达的不是“融合不好”，而是：**融合的收益 ≈ 省下的内存流量 / 总时间**。当分母被计算占满，分子再大也看不出效果。判断该不该融，需要知道这个 op 是 memory-bound 还是 compute-bound（第 16 章的 roofline 会给出判据）。

### 代价清单

| 代价 | 具体表现 |
|---|---|
| 可调库能力丧失 | `matmul` 本可以换成 BLAS；融合后只能用手写循环 |
| 优化空间变窄 | 融合 op 内部不能再单独 tiling、不能再单独换循环顺序（第 18 章） |
| 寄存器压力 | epilogue 的中间值要与累加器争寄存器 |
| 匹配复杂度 | 每加一种 epilogue，模式数量按组合增长 |
| 调试成本 | 一条 op 出错，要同时怀疑三个语义 |

## 进一步用法与变体

**变体 1：加残差 epilogue。** `matmul_bias_relu` 之外常见的是 `matmul_bias_relu_add`（`relu(x@W+b) + residual`）。实现方式与本章一致：多一个输入、多一条模式，形状检查用 `broadcast_shape(residual.shape, out_shape)`。

**变体 2：把 epilogue 写成属性而不是新 op。** 现在的做法是“一种组合一个 op kind”，op 数量随组合爆炸。另一种设计是保留一个 `matmul` op，用 `attrs={"epilogue": ["bias", "relu"]}` 描述后续动作，后端按属性生成代码。TVM 的 `te` schedule 与 Inductor 的 epilogue 都是这个思路。代价是 `infer_output_type` 与解释器都要按属性分派。

**变体 3：reduction 融合。** `sum(relu(x), axis=1)` 可以合成一个 op：沿归约轴累加时顺手取 max，不落盘中间结果。本仓库没有实现，因为 `sum` 的 `keepdims`、`axis` 组合会影响输出索引，模式匹配要处理更多分支。

**变体 4：横向融合。** 两个同 shape 的分支（`a = x*2+1`、`b = x*3-1`）合成一个循环、两个输出。收益来自共享输入读取；代价是需要保证两个分支的循环边界一致，形状不同就只能退化成两个循环。

**变体 5：融合组。** 生产编译器不逐个模式匹配，而是把图切成“可融合组”（fusion group），组内所有 elementwise/reduction 一起生成，组间才落盘。这需要一套“消费者可达性 + 形状兼容”的分组算法，比本章的三条模式通用得多。

## 对比与取舍

| 层次 | 代表 | 融合粒度 | 优点 | 缺点 |
|---|---|---|---|---|
| 图级模式匹配（本章） | `fuse_ops` | 固定几条链 | 可读、可测、与后端无关 | 组合爆炸，漏匹配多 |
| 调度级融合 | TVM `FuseOps` + schedule | 由调度决定 | 能改循环、tile、向量化 | 与后端绑定，pass 不可复用 |
| 编译器后端 | Inductor scheduler、XLA fusion | 自动分组 | 覆盖广，能配合代价模型 | 行为难预测，调试链路长 |

还有一组取舍是“融合到什么程度”：

| 策略 | 结果 |
|---|---|
| 只融 elementwise | 收益稳定，风险低 |
| 融 matmul epilogue | 收益依赖形状与 cache，需要测量 |
| 融 reduction | 并行度可能下降，要单独评估 |

## 踩雷集锦

1. **融合多用户的生产者**：如果 `matmul` 的结果还被 `sum` 读，把它吞进 epilogue 会让 `sum` 的输入消失。本章实测 `changed=0` 就是这条门槛在起作用；自己写新模式时，`users` 的数量检查不能省。
2. **只融一半**：`matmul + add` 融了，`relu` 留在外面。图看起来少了一条 op，实际多了一次中间张量读写，还破坏了后续 `add_relu` 的匹配机会。模式 2 的 `if any(u.kind == "relu" ...)` 就是防这个。
3. **插入位置选错**：把融合 op 插在被消费 op 的原位置（而不是最外层 op 的位置），或者在 `consumed` 集合还没建完时就改 `graph.ops`，都会破坏拓扑序。`validate()` 会报 `uses ... before it is defined`。
4. **以为融合一定更快**：本章 matmul 例子实测 `0.979x`。中间张量能进 cache 时，省下的流量换不来时间。
5. **忘了 `recompute_users()`**：融合后 `Value.users` 还是旧表，下一个 pass（例如再跑一次 `fuse_ops` 或 `dce`）会基于过期信息做决定。`fuse_ops` 在结尾调用了它，自己写 pass 时要照做。

## 进阶：再往深一层

- **代价模型**：生产编译器会用“省下的字节数 / 估算的带宽”与“新增的计算量”做比较，而不是无条件融合。第 23 章的 autotune 是同一思路的另一种形态：让测量代替猜测。
- **融合与布局的交互**：`matmul_bias_relu` 的 bias 是 `(N,)`，在 C 后端里按 `j` 索引，连续访问；如果 bias 是 `(1, N)` 的常量，`broadcast_strides` 会给出 `(0, 1)`，生成的索引仍然是 `j`。融合不会改变布局要求，但会改变“哪一次访问发生在内层循环”。
- **数值顺序**：本章的三条模式都保持累加顺序，所以结果逐位相同。一旦融合引入分块累加（tiling），`acc` 的加法顺序变化，末位差异就出现。这不是 bug，是浮点加法的非结合性。
- **源码对照**：读 `code/mini_ai/passes.py` 里 `fuse_ops` 全部约 120 行，重点看 `consumed` / `insert_at` / `replacements` 三个字典的生命周期，以及为什么最后的 `dce()` 仍然必要（融合可能让某些常量失去唯一用户）。

## 动手练习

### 练习 1：手算一次 `changed`

构造 `relu(add(matmul(x, w), b))`，其中 `matmul` 的结果还被第二个 `add` 使用（`extra = add(mm, c)`），`extra` 是图输出。先写下你预测的 `changed` 值和最终 op 列表，再运行验证。要求解释每一道门槛是拒绝还是通过。

### 练习 2：加一条 `sub_relu` 模式

实现 `sub(x, c)` 后接 `relu` 的融合：

- 新 op kind 叫 `sub_relu`，输入顺序必须保留（`sub` 不满足交换律）；
- `infer_output_type` 要支持它（形状规则与 `add` 相同）；
- 解释器要支持它（`np.maximum(a[0] - a[1], 0)`）；
- C 后端要支持它（`fmaxf(x[i] - c[i], 0.0f)`）。

验收：融合前后 `max_abs_error == 0`，且 `sub_relu` 的模式不会抢走 `matmul + add` 的 `add`。

### 练习 3：把模式 2 的保护条件去掉

注释掉 `if any(u.kind == "relu" for u in users[id(add.output)]): continue`，然后重跑“matmul 多用户”的例子。记录 `changed` 与最终 op 列表，并解释为什么这条链的 `relu` 再也融不进去。

## 本章重点

- `fuse_ops` 支持三条模式，按优先级依次尝试：`matmul+add+relu`、`matmul+add`、`add+relu`。
- 合法性靠五道门槛：消费者必须是目标 op、matmul 候选恰好一个、生产者单用户、消费者单用户、结果不是死代码。
- 实现用两阶段提交：快照 `users`，记录 `consumed`/`replacements`/`insert_at`，最后一次性重写并重建 `graph.ops`。
- 融合 op 插在最外层 op 的位置，保证拓扑序；输出名变成 `<原名>_fused`。
- 收益取决于“省下的内存流量 / 总时间”：elementwise 实测 `1.20x`，matmul epilogue 实测 `0.979x`。
- 融合不改变累加顺序时结果逐位相同；引入分块累加才会出现末位差异。

## 自我检核

- [ ] 我能画出三条模式的匹配条件，并说明优先级为什么不能调换
- [ ] 我能解释为什么 `add` 后面有 `relu` 时不能只融 `matmul + add`
- [ ] 我能说出 `users` 快照与 `consumed` 集合各自解决什么问题
- [ ] 我能解释融合 op 为什么插在最外层 op 的位置
- [ ] 我能复现 `changed=0` 的多用户例子，并指出是哪三处拒绝
- [ ] 我知道 `_new_fused_op` 绕开了哪项检查，以及由此带来的风险
- [ ] 我能解释为什么同一个融合在 elementwise 上赚、在 matmul 上不赚

## 延伸阅读

- **TVM: An Automated End-to-End Optimizing Compiler for Deep Learning**（https://www.usenix.org/conference/osdi18/presentation/chen）：读 Section 3 的算子融合与图级优化。与本章的关联：`FuseOps` 的分组思路，以及“融合组”为什么比逐模式匹配更通用。
- **`code/mini_ai/passes.py` 的 `fuse_ops`**：通读三条模式的循环与最后的图重建。与本章的关联：本章所有门槛与插入逻辑的原始出处。
- **`code/mini_ai/codegen_c.py` 的 `_emit_matmul` 与 `_emit_expr`**：看融合 op 如何落成循环。与本章的关联：`matmul_bias_relu` 的 `acc += bias; acc = fmaxf(acc, 0)` 与 `add_relu` 的单循环。
- **PyTorch Inductor 的 scheduler**（https://github.com/pytorch/pytorch/tree/main/torch/_inductor）：读 `scheduler.py` 里 fusion 的可行性判断。与本章的关联：真实编译器如何用依赖关系而不是固定模式做分组。
- **MLIR Linalg Fusion**（https://mlir.llvm.org/docs/Dialects/Linalg/）：读 elementwise fusion 一节。与本章的关联：在 tensor 级别做 producer-consumer 融合的通用表述。

---

下一章：[练习 B：把图 pass 串成一条能验收的流水线](./practice-b-graph-passes.md)