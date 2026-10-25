# Final Project：造一个端到端 mini AI 编译器

> **目标**：把前面各章拆开实现的东西合成一条完整流水线：PyTorch FX 抓图 → 自有 IR → verifier → pass → 内存规划 → C 后端 / LLVM 后端 → runtime → 差分测试与 benchmark。最终提交一个能复现、能解释、能被别人验收的 mini 编译器，而不是一份「跑通了 demo」的截图。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，llvmlite 0.50.0，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本 Final 的验收命令和数值来自本机实际运行；你的提交必须带自己的环境、命令和输出。

## 为什么需要 Final

前面各章每章解决一个问题：抓图、建 IR、写解释器、做 pass、算 liveness、生成 C、生成 LLVM、测性能。单章测试通过，不等于整条流水线成立。接口会在边界处出错：

- `from_torch` 生成的 `Value` 和 `plan_memory` 看到的不是同一个对象，内存计划找不到 allocation。
- pass 融合后输出名变了，C 后端的输出拷贝还在找旧名字。
- 内存计划按旧图算好，之后又加了一个 op，offset 越界。
- C 后端用 `int` 返回错误码，LLVM 后端用 `void`，runtime 把两者当成同一种 ABI。
- benchmark 把编译时间算进运行时间，得出「LLVM 比 C 快 10 倍」的结论。

Final 的任务是定义并守住这些接口：每个阶段输入什么、输出什么、失败时抛什么、什么条件下结果可信。你需要交出一个别人能按文档跑起来、能对照数值、能复现实验的工程。

课程提供的 `code/` 是参考实现。你可以从它复制骨架，也可以自己重写；如果复用，必须在报告里标注来源，并能解释关键路径。验收看的是流水线是否闭合、证据是否充分、边界是否写清楚，不是代码行数。

## 先建立直觉

完整流水线：

```text
PyTorch nn.Module + example inputs
        |
        |  from_torch：FX symbolic_trace + ShapeProp
        |  nn.Linear -> matmul + add；权重转置成常量
        v
Graph（Value / Op / TensorType，静态 float32，拓扑序）
        |
        |  Graph.validate：SSA、ownership、拓扑序、输出存在
        v
raw Graph（MLP 5 个 op）
        |
        |  standard_pipeline：constant_fold -> algebraic -> cse -> fuse -> dce
        v
optimized Graph（MLP 2 个 op：matmul_bias_relu / matmul_bias）
        |
        |  plan_memory：liveness -> 64B 对齐 -> arena offset
        v
MemoryPlan（arena_bytes=320）
        |
        +-------------+-------------+
        |                           |
        v                           v
C backend                     LLVM backend
loops + arena                 unrolled IR + workspace
cl /LD /O2 /fp:fast           MCJIT + ctypes
        |                           |
        +-------------+-------------+
                      |
                      v
runtime：run_c_graph / LLVMJIT.run / benchmark / assert_close
                      |
                      v
报告：误差、内存、IR 行数、median/min/max、已知限制
```

三条不变量：解释器是数值金标准；shape/dtype 在 IR 层闭合；内存安全优先于复用（`end < start`）。`MemoryPlan` 和 `LLVMJIT` 都是 `Graph` 的派生对象：图变就重新 plan、重新 JIT、重新生成 C，不能跨图复用。

## 任务规格

### 1. 交付物

在仓库下创建 `final/`（不要修改 `compilers/mini_ai_compiler/code/`），至少包含：

```text
final/
  mini_ai_final/    # ir / frontend / interpreter / passes / memory / codegen_c / codegen_llvm / runtime
  tests/            # test_ir / frontend / passes / memory / backends / differential
  run_demo.py
  report.md
```

可以合并文件，但报告里的「参考实现路径」必须能对应到你的文件。`run_demo.py` 要能一条命令跑完：抓图、打印 raw/optimized IR、内存计划、C 误差、LLVM 误差、benchmark。

### 2. 支持的算子子集

必须支持：

- elementwise：`add`、`sub`、`mul`、`div`、`relu`、`exp`、`add_relu`。
- matmul：`matmul`、`matmul_bias`、`matmul_bias_relu`。
- reduction：`sum(axis, keepdims)`、`softmax(axis)`。
- 2-D matmul，静态 shape，`float32`，rank 0–4。
- broadcast：NumPy 风格，stride 0 表示广播维。

可以不支持：动态 shape、rank > 4 的常量、批量 matmul、卷积、LayerNorm、量化、GPU。不支持的输入必须抛 `IRError`，不能静默降级或返回错误结果。

### 3. 前端契约

`from_torch(model, example_inputs)` 必须：

- 用 FX 抓图，展开 `nn.Linear`，不把整个 `Linear` 当黑盒。
- 用 `ShapeProp` 取得每个节点的 shape。
- 把 `nn.Linear` 降成 `matmul(x, W.T) + bias`，权重转成常量。
- 支持 `torch.relu`、`torch.add/sub/mul/div`、`torch.matmul`、`torch.sum`、`torch.softmax`。
- 遇到不支持的 target 抛 `IRError`，错误信息包含节点名和 target。
- 输出 `Graph` 通过 `validate()`。

### 4. 后端契约

C 后端：

- 生成 `MINI_EXPORT int mini_forward(const float* x0, ..., float* out0, ...)`。
- 函数内用 64 字节对齐的 arena，大小来自 `MemoryPlan.arena_bytes`。
- 每个 op 生成显式循环；matmul 用三重循环；输出逐元素拷贝。
- 用 `cl /LD /O2 /fp:fast` 编译 DLL；失败时保留 `build.log`。

LLVM 后端：

- 生成 `void mini_forward_llvm(float* inputs..., float* outputs..., float* workspace)`。
- 静态 shape 全展开；常量是 `internal constant [N x float]`。
- 用 `llvmlite` 构造模块，`parse_assembly` + `verify` 后 JIT。
- workspace 由调用者分配，大小 `max(1, arena_bytes // 4)` 个 float。
- 提供 `LLVMJIT` 复用实例，benchmark 不用每次编译的 `jit_run`。

### 5. 报告契约

报告至少包含：环境、架构、支持的算子、IR/verifier 不变量、pass 表、内存表、C/LLVM 对照、差分测试、autotune、已知问题、复现命令。完整模板见后文「报告模板」。

## 验收标准

### 1. 正确性

| 项 | 门槛 | 证据 |
| --- | --- | --- |
| 前端 vs PyTorch | max_abs_error ≤ 1e-5 | MLP demo，固定 seed |
| C vs 解释器 | max_abs_error ≤ 1e-5 | 同一图、同一 feed |
| LLVM vs 解释器 | max_abs_error ≤ 1e-5 | 同一图、同一 feed |
| C vs LLVM | max_abs_error ≤ 1e-6（或说明为何放宽） | 同一图、同一 feed |
| 随机图差分 | 50 个种子，0 个失败（1e-5） | 固定生成器和 seed 列表 |
| 失败必须显式 | 不支持 op/坏 shape 抛 `IRError` | 负向测试 |

C 与 LLVM 的 1e-6 是目标，不是绝对保证。`/fp:fast`、FMA、累加顺序都可能让差异到 1e-5 量级。达不到 1e-6 时，报告要给出误差分布和原因，不能悄悄改成 1e-3。

### 2. 结构

- 每次 pass 后、两个后端入口都调用 `validate()`。
- pass 不改变输出 shape；`MemoryPlan.validate()` 通过（对齐、不越界、活值不别名）。
- C/LLVM 签名顺序一致：输入、输出、workspace；不支持的 op 显式报错。
- 随机测试固定种子，失败保存 seed 和 feed。

### 3. 性能与可复现

- benchmark 至少 warmup=5、repeat=30；报告 median/min/max。
- 编译时间与运行时间分开计时；C 的 DLL 加载不混进 kernel 时间（或明确标注包含）。
- 同一命令连跑两次，数值一致；耗时允许波动，但报告条件。
- autotune 至少 8 组配置，报告两次运行；差异小于 1.5 倍时不下结论。

## 分阶段实现

按阶段推进，每阶段有明确产物和检查点。不要一次写完所有模块再调试；接口错误会堆在一起，定位成本高。

### 阶段 0：环境与最小闭环

建 venv，安装 torch/numpy/llvmlite，确认 `cl` 可用；跑课程 `demo_e2e.py`，记录版本和输出。检查点：

```text
C max_abs_error: 4.470348358154297e-08
LLVM max_abs_error: 4.470348358154297e-08
ir_lines: 6413
```

常见失败：`vcvars64.bat` 找不到；`llvmlite` 版本和 Python 不匹配；DLL 被旧进程锁住。先解决环境，不要带着环境问题调代码。

### 阶段 1：IR 与解释器

实现 `TensorType`、`Value`、`Op`、`Graph`、`broadcast_shape`、`broadcast_strides`、`infer_output_type`、`evaluate_graph`。产物是手工构造的 `x(2,3) + b(3) -> relu -> sum(axis=1)`，输出和 NumPy 一致。

检查点：`graph.dump()` 显示 `add -> relu -> sum` 三个 op，`sum` 的输出 shape 是 `tensor<2>`，常量 `b` 的 `numel=3`。

验收：`broadcast_shape((4,1),(1,3)) == (4,3)`；`broadcast_shape((2,3),(4,))` 抛 `IRError`；`validate()` 能抓出坏拓扑序。

### 阶段 2：前端

接 FX，展开 `nn.Linear`，处理常量、shape、输出列表；不支持 target 抛错。产物是 MLP 的 raw graph，5 个 op。

检查点：`graph.dump()` 显示两个 `matmul`、两个 `add`、一个 `relu`，输出 shape 是 `tensor<4, 4>`。

验收：解释器输出与 `model(x)` 的 max_abs_error ≤ 1e-5；权重常量是 `W.T`，shape 是 `(in_features, out_features)`。

### 阶段 3：Verifier 与 pass 流水线

把 `validate` 接到每次 pass 后；实现 constant fold、algebraic simplify、CSE、fusion、DCE；加 `PassManager` 和 verbose。产物是标准流水线，MLP 从 5 op 降到 2 op。

检查点：

```text
constant_fold                    ops   5 ->   5  changed=0
algebraic_simplify               ops   5 ->   5  changed=0
cse                              ops   5 ->   5  changed=0
fuse_ops                         ops   5 ->   2  changed=2
dce                              ops   2 ->   2  changed=0
```

验收：每个 pass 后 `validate()` 通过；融合前后解释器结果差 ≤ 1e-6；错误融合（比如丢 bias）能被差分测试抓到。

### 阶段 4：内存规划

算 last-use interval，按 start 排序，线性扫描 + best-fit，64 字节对齐，输出 offset/arena/peak。

检查点：

```text
arena_bytes=320 alignment=64 peak_live_bytes=320
value                 interval     size   offset
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

验收：`end < start` 才能复用；同 op 读写不复用；输出 end 设为 `len(ops)`；`validate` 能抓出活值别名。

### 阶段 5：C 后端

生成 C 源码、常量数组、arena、逐 op 循环、输出拷贝；用 `cl` 编译 DLL；用 ctypes 调用。

检查点：

```text
source: .scratch\mini-ai-demo\mlp.c
dll: .scratch\mini-ai-demo\mlp.dll
max_abs_error: 4.470348358154297e-08
```

验收：C 源码里能看到 `MINI_EXPORT int mini_forward`、`_aligned_malloc(320, 64)`、两个 matmul 循环、输出拷贝；DLL 能被 ctypes 加载；错误码非 0 时抛异常。

### 阶段 6：LLVM 后端与 runtime

用 llvmlite 构造模块，JIT，workspace，ctypes 调用；实现 `benchmark` 和 `assert_close`。

检查点：

```text
ir_lines: 6413
max_abs_error: 4.470348358154297e-08
C median ms: 0.1805
LLVM median ms: 0.0592
PyTorch median ms: 0.2104
```

验收：`llvm_ir` 能打印；`verify()` 通过；恒等图能跑；NaN 案例的行为被记录；benchmark 不把 JIT 编译算进运行时间。

### 阶段 7：差分测试、autotune 与报告

随机图差分 50 个种子；C/LLVM 对照；8 组 tile 搜索两次；写报告。

检查点：

```text
Ran 12 tests ... OK
20 graphs: failures=0 worst_interp=0.000e+00 worst_llvm=0.000e+00
best: (8, 8, 8) median_ms= 0.278
```

验收：失败种子可复现；报告包含所有表格；已知问题（NaN、全展开、benchmark 闭包）写清楚。

## 核心内容：实现串联

这一节按数据流把关键接口串起来。每一段都对应参考实现的一个文件；你的实现可以不同，但契约要等价。

### 1. 前端：从 FX 节点到 Value

`from_torch` 先用 `_InlineTracer` trace，再用 `ShapeProp` 传播 shape。`_InlineTracer.is_leaf_module` 返回 `False`，让 `nn.Linear` 被展开成内部算子，而不是保留为黑盒。然后遍历 FX 节点：

- `placeholder` → `graph.add_input`，shape 从 `tensor_meta` 或 example 取。
- `get_attr` → `graph.add_const`，权重/偏置变成常量。
- `call_function` → 按 target 映射到 `add/sub/mul/div/relu/exp/matmul/sum/softmax`。
- `linear` → 权重转置后 `matmul`，再 `add` bias。
- `output` → `graph.add_output`，支持单值和 tuple/list。

`nn.Linear` 的降级是 Final 的关键接口：

```python
w_t = graph.add_const(f"{node.name}_weight_t", vals[1].data.T.copy())
out = graph.add_op("matmul", [vals[0], w_t], name=f"{node.name}_matmul")
if len(vals) == 3:
    out = graph.add_op("add", [out, vals[2]], name=node.name)
```

`Linear` 的权重 shape 是 `(out_features, in_features)`，matmul 需要 `(in_features, out_features)`，所以转置。这个转置如果漏了，shape 推断会在 `a[1] != b[0]` 处报错；如果 shape 恰好相等（方阵），错误会静默通过 shape 检查，直到数值比较才暴露。Final 要专门测一个非方阵 Linear。

### 2. Verifier：所有阶段的守门人

`Graph.validate` 检查四件事：

```python
for i, op in enumerate(self.ops):
    for v in op.inputs:
        if v.kind not in {"input", "constant"} and (v.producer is None or id(v.producer) not in op_index):
            raise IRError(f"{op.kind} uses {v.name} before it is defined")
    for v in op.outputs:
        if id(v) in seen or v.producer is not op:
            raise IRError(f"value {v.name} is redefined or has a stale producer")
```

四个不变量：

1. **所有权**：输入值必须属于当前图，不能拿别的图的 `Value` 混进来。
2. **拓扑序**：非 input/constant 的输入，其 producer 必须已经出现。
3. **SSA**：每个输出值只定义一次，producer 指针必须指向当前 op。
4. **输出存在**：至少一个输出，且输出属于当前图。

`PassManager.run` 在每个 pass 后调用 `graph.validate()`：

```python
for name, fn, kwargs in self.passes:
    before = len(graph.ops)
    changed = int(fn(graph, **kwargs))
    graph.validate()
    records.append(PassRecord(name, changed))
```

这让 pass 的错误在发生的阶段就暴露，而不是等到 codegen。Final 要求所有 pass 后都 validate；如果某个 pass 暂时无法保持不变量，先修 pass，不要注释掉检查。

### 3. 内存：严格小于才复用

`plan_memory` 的核心释放条件：

```python
for alloc in active:
    if alloc.end < start:      # 同一 op 内读写不能复用
        free_block(alloc.offset, alloc.size)
    else:
        still_active.append(alloc)
```

`alloc.end` 是最后一次被读的 op 下标，`start` 是新值的生产 op 下标。如果 `end == start`，新 op 在同一个算子内还要读旧值，不能复用。比如 `relu` 的输入和输出在同一个 op 内，输入 interval 的 end 等于输出的 start，复用会让 `relu` 读到被覆盖的输入。

对齐与输出：

```python
need = max(alignment, align_up(value.type.numel * 4, alignment))
if v in graph.outputs:
    end = len(graph.ops)
```

小张量也占至少 64 字节，保证下一个 offset 仍然 64 对齐。输出值的 end 强制为 `len(graph.ops)`，因为输出拷贝发生在所有 op 之后。

### 4. C 后端：显式循环与 arena

生成的签名与核心结构：

```c
MINI_EXPORT int mini_forward(const float* x0, float* out0) {
    const size_t arena_bytes = 320;
    float* arena = (float*)_aligned_malloc(arena_bytes, 64);
    if (!arena) return 1;
    /* 逐 op 生成循环；临时值指针 = arena + offset */
    for (int i = 0; i < 16; ++i) out0[i] = v_13_linear_1_fused[i];
    _aligned_free(arena);
    return 0;
}
```

要点：`MINI_EXPORT` 在 MSVC 下展开为 `__declspec(dllexport)`；arena 用 `_aligned_malloc(arena_bytes, 64)`；临时值指针是 `(char*)arena + off`；每个 op 生成独立循环；输出拷贝在所有 op 之后，然后释放 arena，返回 0。C 后端的优势是「看得见」：循环、索引、bias、`fmaxf` 都在源码里。

### 5. LLVM 后端：模块、JIT、workspace

JIT 核心：

```python
module, self.plan = build_llvm_module(graph, plan)
backing = binding.parse_assembly(str(module)); backing.verify()
target = binding.Target.from_default_triple()
self.tm = target.create_target_machine()
self.engine = binding.create_mcjit_compiler(backing, self.tm)
self.engine.finalize_object()
addr = self.engine.get_function_address("mini_forward_llvm")
self.cfn = ctypes.CFUNCTYPE(None, *([self.ptr_ty] * (len(graph.inputs) + len(graph.outputs) + 1)))(addr)
```

调用：

```python
ws = np.zeros(max(1, self.plan.arena_bytes // 4), dtype=np.float32)
args = [a.ctypes.data_as(self.ptr_ty) for a in inputs]
args += [a.ctypes.data_as(self.ptr_ty) for a in outputs]
args.append(ws.ctypes.data_as(self.ptr_ty))
self.cfn(*args)
```

Final 要求你能解释：为什么 workspace 用 `arena_bytes // 4`、为什么 `max(1, ...)`、为什么 engine 必须挂在实例上、为什么 `jit_run` 不适合 benchmark。答不出来，说明只是复制了代码。

### 6. Runtime：加载、调用、计时

```python
def run_c_graph(graph: Graph, feed: dict[str, np.ndarray], dll_path: str | Path) -> list[np.ndarray]:
    inputs = [feed[v.name] for v in graph.inputs]
    shapes = [v.type.shape for v in graph.outputs]
    fn = load_c_function(dll_path, len(inputs), len(shapes))
    return fn(inputs, shapes)

def benchmark(fn: Callable[[], object], *, warmup: int = 5, repeat: int = 30) -> dict[str, float]:
    """Return median/min/max milliseconds.

    Median is the headline number.  The min is useful for noise diagnosis, but
    should not be used to claim a speedup on its own.
    """
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "repeat": float(repeat),
    }
```

上面两段摘自 `code/mini_ai/runtime.py`，省略了 `Callable`、`Sequence`、`Graph`、`Path` 的导入。Final 要求把「编译/加载」和「运行」分开。C 的 `run_c_graph` 每次重新加载 DLL，demo 的 benchmark 因此包含加载开销；公平对比要预加载 `fn`，或把 workspace/输出分配也移出计时区间。

### 7. 实测闭环

课程参考实现的完整输出（节选）：

```text
raw ops: matmul, add, relu, matmul, add
fuse_ops                         ops   5 ->   2  changed=2
memory: arena_bytes=320 peak_live_bytes=320
C max_abs_error: 4.470348358154297e-08
LLVM ir_lines: 6413
LLVM max_abs_error: 4.470348358154297e-08
benchmark: C 0.1805 ms, LLVM 0.0592 ms, PyTorch 0.2104 ms
```

测试 `Ran 12 tests in 9.198s / OK`；随机差分 `20 graphs: failures=0 worst_interp=0.000e+00 worst_llvm=0.000e+00`；autotune 两次最优都是 8x8x8（0.2780 / 0.2421 ms），更早一次是 16x16x16（0.2132 ms）。你的数字不必完全一致，但误差应在同一量级；报告要给出实际条件和输出。

## 测试用例

至少覆盖下表。前 12 个是单元/集成，后 5 个是端到端与边界。每个用例都要能在 `final/tests/` 里独立运行。

| # | 用例 | 输入 | 期望 | 类型 |
| --- | --- | --- | --- | --- |
| 1 | broadcast 形状 | `(4,1)` + `(1,3)` | `(4,3)` | 正常 |
| 2 | 非法 broadcast | `(2,3)` + `(4,)` | `IRError` | 失败 |
| 3 | 标量广播 | `()` + `(2,3)` | `(2,3)` | 边界 |
| 4 | ReLU 数值/NaN | `[-1,0,2]`；`[NaN,-1]` | `[0,0,2]`；解释器 `[NaN,0]` vs LLVM `[0,0]` | 正常/边界 |
| 5 | sum axis/keepdims | `(2,3)`，axis=1 | `(2,)`；keepdims `(2,1)` | 正常 |
| 6 | softmax 行和 | `(2,3)`，axis=1 | 每行和≈1 | 正常 |
| 7 | Linear 非方阵/转置 | 8→16 | `linear_weight_t` shape `(8,16)`，vs PyTorch ≤1e-5 | 正常/结构 |
| 8 | 不支持 op | `torch.sigmoid` | `IRError` 含节点名 | 失败 |
| 9 | 融合结构与正确性 | MLP 5 op | 2 op，类型正确，误差≤1e-6 | 结构/正常 |
| 10 | 三件套 | 常量 add / 重复 relu / 死分支 | fold、CSE、DCE 各自生效 | 正常 |
| 11 | 内存复用 | 死值 + 新值 | offset 相同 | 正常 |
| 12 | 活值别名 | `a->b` 链 | offset 不同 | 失败防护 |
| 13 | C/LLVM vs 解释器 | 小图 | 各自 ≤1e-6 | 集成 |
| 14 | 多输出/恒等图 | 两个输出；输入=输出 | 顺序正确；workspace 可空 | 结构/边界 |
| 15 | 随机图 | 50 seeds | 0 失败（1e-5） | 差分 |
| 16 | 确定性 | 同 seed 两次 | 输出逐位一致 | 复现 |
| 17 | benchmark 字段 | 任意 kernel | median/min/max/repeat | 报告 |

负向用例必须断言「抛了什么异常、消息包含什么」，不能只写 `with self.assertRaises(Exception)`。异常类型和消息是接口的一部分。

## 评分表（100 分）

| 维度 | 分值 | 优秀（90–100%） | 合格（60–89%） | 不合格（<60%） |
| --- | ---: | --- | --- | --- |
| 正确性 | 30 | PyTorch/C/LLVM 全 ≤1e-5，50 随机图 0 失败 | 小图通过，随机图有少量失败但已记录 | 主路径错，或靠放宽容差通过 |
| IR 与 verifier | 10 | 不变量完整，错误定位清楚 | 能 validate，但错误消息弱 | 不验证，坏图进入 codegen |
| Pass | 10 | 5→2，正确性有前后对照 | 能融合，边界有遗漏 | pass 改变语义或崩溃 |
| 内存 | 10 | interval/offset/峰值正确，validate 通过 | 能分配，复用保守 | 活值别名或越界 |
| C 后端 | 10 | DLL 可加载，误差达标，源码可读 | 能跑，少量边界问题 | 编译失败或 ABI 错 |
| LLVM 后端 | 10 | JIT 可跑，IR 可读，workspace 正确 | 能跑，规模/生命周期有瑕疵 | JIT 崩溃或结果错 |
| 测试 | 10 | 覆盖表 17 项，负向用例完整 | 覆盖主路径 | 只有 demo |
| 报告与性能 | 10 | 证据链完整，benchmark 条件清楚，autotune 有噪声分析 | 有数字但条件不全 | 只有结论，没有命令/表格 |

总分 ≥90 为优秀，75–89 为良好，60–74 为及格，<60 需要重做关键阶段。评分不看你写了多少行，而看「结论是否有证据、失败是否可复现、边界是否写清楚」。

## 参考实现路径

课程参考实现位于 `compilers/mini_ai_compiler/code/`。读的时候按接口读，不要从头到尾背。

| 文件 | 作用 | 重点看 | 对应章节 |
| --- | --- | --- | --- |
| `mini_ai/ir.py` | `TensorType`、`Value`、`Op`、`Graph` | `add_op`、`validate`、`dce`、`clone`、`dump` | Ch 2、4、6 |
| `mini_ai/frontend.py` | FX → IR | `_InlineTracer`、`ShapeProp`、Linear 降级、常量 | Ch 3 |
| `mini_ai/interpreter.py` | NumPy 金标准 | `apply_op`、`evaluate_graph`、`max_abs_error` | Ch 5 |
| `mini_ai/passes.py` | 图优化 | `constant_fold`、CSE、algebraic、`fuse_ops`、`PassManager` | Ch 7–10 |
| `mini_ai/memory.py` | liveness + 线性扫描 | `align_up`、`plan_memory`、`end < start`、`validate` | Ch 12–15 |
| `mini_ai/codegen_c.py` | C 后端 | `CCodegen.emit`、`_emit_matmul`、arena、`compile_c` | Ch 16–20 |
| `mini_ai/codegen_llvm.py` | LLVM 后端 | `LLVMCodegen`、`llvm_ir`、`LLVMJIT`、workspace | Ch 21–22 |
| `mini_ai/runtime.py` | ctypes + benchmark | `load_c_function`、`run_c_graph`、`benchmark`、`assert_close` | Ch 20 |
| `examples/demo_e2e.py` | 端到端 demo | 抓图、pass、内存、C/LLVM、benchmark | Final |
| `examples/autotune_matmul.py` | tile 搜索 | `kernel`、8 配置、benchmark | Ch 23 |
| `tests/test_mini_ai.py` | 12 个 unittest | IR、前端、pass、内存、后端 | 全课 |

复现命令：

```text
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& ".scratch\aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\demo_e2e.py"
& ".scratch\aic-env\Scripts\python.exe" -m unittest discover -s "compilers\mini_ai_compiler\code\tests" -v
& ".scratch\aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\autotune_matmul.py"
```

参考实现的 12 个测试全部通过 `Ran 12 tests in 9.198s / OK`。你的 Final 至少要有 17 个测试（对应上表），其中负向用例不少于 4 个。

## 报告模板

`report.md` 建议按下面结构写。每一节都要求「结论 + 证据 + 命令」，不要只放截图。

```markdown
# Mini AI Compiler Final Report

## 1. 环境
| 项 | 值 | 验证命令 |

## 2. 架构
（ASCII 图：FX -> IR -> pass -> memory -> C/LLVM -> runtime）

## 3. 支持的算子与边界
| op | 支持 | 限制 |

## 4. IR 与 verifier 不变量
- SSA / 拓扑序 / shape / 错误示例

## 5. Pass 流水线
| pass | before | after | changed | 作用 |

## 6. 内存计划
| value | interval | size | offset |
arena_bytes=... peak_live_bytes=...

## 7. 后端对照
| 后端 | max_abs_error | IR 行数 | median | min | max |

## 8. 差分测试
| 用例 | shape | seed | maxerr | 通过 |

## 9. Autotune
| tile | run A | run B | maxerr |

## 10. 已知问题
- NaN / 动态 shape / 全展开规模 / benchmark 闭包

## 11. 复现命令
## 12. 改动与反思
- 复用来源 / 自己的改动 / 最难定位的 bug / 下一步
```

报告里的数字必须来自你的机器。课程的数字（4.47e-08、6413 行、0.1805 ms）只能作为对照，不能直接抄进「我的结果」。

## 底层机制：五个接口不变量

1. **Value 身份，不是名字。** `plan_memory.allocations`、`const_globals`、`replace_all_uses` 都按 `id(v)` 索引。同名新对象不会自动接管旧用途；替换用途用 `replace_all_uses`。
2. **shape/dtype 在 IR 层闭合。** `add_op` 同时推断和校验 shape；后端只按 `TensorType.shape` 生成索引，不自己猜。pass 创建新 op 时走 `graph.add_op`，不要手搓 `Op` 绕过检查。
3. **内存所有权清晰。** 输入/输出/workspace 由调用者持有；C 的 arena 函数内分配释放；LLVM 的 workspace 由 Python 分配；常量在只读数据区。函数返回后还能用的指针必须指向调用者内存。
4. **ABI 顺序固定。** C 是 `int f(const float* x..., float* out...)`；LLVM 是 `void f(float* x..., float* out..., float* workspace)`。ctypes 参数个数必须严格匹配，多输出按 `graph.outputs` 顺序。
5. **派生分析不能跨图复用。** 图变则 MemoryPlan 失效、LLVMJIT 失效、C 源码和 DLL 失效。缓存键至少包含图结构、shape、dtype 和常量哈希。

## 扩展方向：进一步用法与变体

完成基础 Final 后，可以沿这些方向扩展。每次只加一个维度，保持差分测试可回归。

1. **动态 shape**：把 `TensorType.shape` 改成符号维度，后端生成带运行时参数的循环；内存计划改成按最大 shape 或运行时 arena。先做单维度动态。
2. **循环 LLVM IR**：把全展开 matmul 改成 tile 循环，用 `phi` 维护归纳变量；代码体积从 6413 行降到几十行，但需要处理边界和 phi。
3. **更多算子**：卷积、LayerNorm、GELU、batch matmul、concat、reshape。每个新算子都要补 shape 推断、解释器、C/LLVM 和测试。
4. **融合扩展**：matmul + bias + activation 之外的 reduction 融合、elementwise 链融合、softmax 融合。先写合法性条件，再写替换。
5. **内存优化**：in-place、alias 分析、跨 op 复用、arena 池化。每加一种复用，补一个 canary 和差分测试。
6. **自动调优**：把 8 点穷举换成随机搜索/坐标下降，加成本模型剪枝；按 shape 缓存最优配置。
7. **LLVM 优化 pass**：`mem2reg`、`instcombine`、`GVN`、循环展开；对比 IR 行数、误差、耗时。
8. **AOT / ORC / 量化 / GPU**：`emit_object` 出目标文件、换 ORC JIT、int8 权重或 CUDA kernel。每项都会改变语义或布局，先完成 float32 CPU 版本。

## 对比与取舍

| 决策 | 选项 A | 选项 B | 建议 |
| --- | --- | --- | --- |
| LLVM IR 形态 | 全展开 | 循环 + phi | 小 shape 全展开，大 shape 循环 |
| 后端 | C | LLVM JIT | 交付用 C，迭代用 LLVM，两者都测 |
| pass 激进度 | 保守 | 激进 | 先保守正确，再用差分测试放开 |
| 内存复用 | 不复用 | 激进复用 | 先 `end < start`，再加 canary |
| 调度 | 静态启发式 | autotune | 固定 shape 可 autotune，通用场景用启发式 |
| 容差 | 1e-6 | 1e-3 | 先测正常误差分布，再定阈值 |
| benchmark | 含编译/加载 | 只测 kernel | 报告两者，标题写清楚 |

没有「永远正确」的选项。Final 评分看的是：你是否说明了选择的条件、代价和验证方式。只写「我选了 LLVM，因为更快」不够；要给出同条件对照和误差。

## 踩雷集锦

1. **pass 后不重建 plan。** 图从 5 op 变 2 op，offset 全变；旧 plan 还在用，workspace 越界或别名。每次 pass 后重新 `plan_memory`。
2. **输出名变了，拷贝还在找旧名。** 融合创建 `relu_fused`，但 C 后端按 `v.name` 查旧 `relu`。用 `Value` 对象/`id` 索引，不用名字。
3. **benchmark 把编译算进去。** `LLVMJIT(g)` 构造在计时循环内，测到的是编译时间；`run_c_graph` 每次加载 DLL，测到的是加载时间。把编译/加载移到循环外。
4. **NaN 没定义策略。** 解释器传播、C/LLVM 归零，三条路径不一致。Final 报告必须写清楚选择，并加测试。
5. **随机测试不保存种子。** 失败一次后无法复现。失败时打印 seed、shape、feed 摘要和第一个坏阶段。

## 进阶

- 写一个 pass 固定点驱动器：反复跑 `constant_fold + algebraic + cse + dce` 直到 changed 全 0，记录迭代次数。
- 给 matmul 加 tile 参数，接到 Ch 23 的 autotune；比较 C tile 与 LLVM 全展开的误差/耗时。
- 实现 loop IR + phi，把 6413 行压到几百行，验证数值不变；再加 in-place 复用和 canary。
- 用 `emit_object` 做 AOT，比较 JIT 与 AOT 的启动/运行时间；把报告做成脚本自动填表。

## 动手练习

按阶段提交，每个检查点都要有命令和输出。

1. **阶段 0–1**：跑通环境 demo；手写 `x(2,3)+b(3)->relu->sum`，解释器输出和 NumPy 一致；提交 `graph.dump()`。
2. **阶段 2–3**：MLP raw graph 5 op；标准流水线 5→2；提交融合前后误差和 pass 表。
3. **阶段 4–5**：内存计划 arena=320；C DLL 跑通；提交 `plan.summary()`、C 源码关键段、max_abs_error。
4. **阶段 6**：LLVM JIT 跑通；提交 `ir_lines`、`max_abs_error`、benchmark 表；解释 workspace 大小。
5. **阶段 7**：50 个随机种子差分；8 组 autotune 两次；提交报告和失败种子（如有）。

## 本章重点

- Final 的核心是接口闭合：FX → IR → verifier → pass → memory → C/LLVM → runtime，每一段都有明确输入/输出和不变量。
- Value 身份、shape 闭合、内存所有权、ABI 顺序、派生分析不复用，是五条硬约束。
- 参考实现的真实闭环是：5 op → 2 op，arena 320 字节，C/LLVM 误差 4.47e-08，LLVM IR 6413 行。
- 测试要覆盖正常、边界、失败；随机图差分至少 50 个种子；负向用例断言异常类型和消息。
- benchmark 必须区分编译/加载和运行，报告 median/min/max；差异小于 1.5 倍不下结论。
- 报告是证据链：环境、命令、表格、已知问题、参考路径，一个都不能少。
- 已知限制要主动写：NaN 语义、全展开规模、无动态 shape、单线程、benchmark 闭包内容。

## 自我检核

- [ ] 我能画出一条命令从 `nn.Module` 到 C DLL 和 LLVM JIT 的完整路径。
- [ ] 我能解释 `Value` 身份为什么不能用名字替代。
- [ ] 我能说明每次 pass 后为什么要重新 `validate` 和 `plan_memory`。
- [ ] 我能手算 MLP 的 arena 320 字节和两个 offset。
- [ ] 我能解释 C 与 LLVM 的 ABI 差异和 workspace 所有权。
- [ ] 我能跑 50 个随机种子差分，并保存失败种子。
- [ ] 我能写一份包含环境、pass 表、内存表、后端对照、autotune、已知问题的报告。
- [ ] 我知道 NaN 在三条路径上的差异，并写清楚策略。
- [ ] 我能指出 benchmark 闭包里包含了哪些非 kernel 工作。

## 延伸阅读

- **课程参考实现 `compilers/mini_ai_compiler/code/`**：按 `ir.py → frontend.py → interpreter.py → passes.py → memory.py → codegen_c.py → codegen_llvm.py → runtime.py` 的顺序读；每读一个文件，跑对应测试，最后用 `demo_e2e.py` 串起来。关联：Final 的每个阶段都能在这条路径里找到对照。
- **LLVM LangRef 与 llvmlite 文档**：读 Types、Functions、`getelementptr`、`phi` 和 JIT API；关联 Ch 21–22，帮助你从「能跑」走到「能解释 IR」。
- **《Engineering a Compiler》**：读 SSA、数据流、指令选择、寄存器分配；关联 Ch 2、6、12、16，补上 Final 背后的理论。
- **PyTorch FX 文档**：读 `symbolic_trace`、`GraphModule`、`ShapeProp`、`replace_pattern`；关联 Ch 3，帮助你把前端从 demo 扩到更多模型。

---

[返回课程首页](./README.md)