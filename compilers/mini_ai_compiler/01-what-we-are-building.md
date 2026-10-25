# Ch 1 我们要造什么

> 目标：在写第一行编译器代码之前，先建立一张完整的架构图，明确每一层的输入、输出和失败模式；知道这个 mini 编译器支持哪些算子、五个里程碑分别交付什么、以及为什么我们不直接套 TVM 或 MLIR。

> 环境：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；numpy 2.4.6；MSVC 14.43。所有命令在 `D:\Learning-with-Claude` 下执行，并设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"`。

## 为什么需要

很多编译器项目死在“先写了一个大框架，但没有任何一条路径能端到端跑通”。你需要先知道最小可运行系统长什么样，再决定每一层做到什么深度。

本章要回答四个问题：

1. 一个 PyTorch 模型经过哪些层，才变成可执行代码？
2. 哪些信息必须在 IR 里表达，哪些可以留到后端？
3. 五个里程碑各自能验证什么，不能验证什么？
4. 为什么不直接把模型喂给 TVM、MLIR 或 TorchInductor？

先有这张地图，后面每一章才知道自己在修哪一段路。

## 先建立直觉

### 分层栈

```text
┌─────────────────────────────────────────────────────────────┐
│ 用户层：torch.nn.Module + example_inputs                     │
└───────────────────────────┬─────────────────────────────────┘
                            │ frontend.py
                            │ symbolic_trace + ShapeProp
                            v
┌─────────────────────────────────────────────────────────────┐
│ 图 IR：ir.py                                                 │
│ TensorType / Value / Op / Graph                              │
│ 静态 shape、float32、SSA、拓扑序                             │
└───────────────────────────┬─────────────────────────────────┘
                            │ passes.py
                            │ constant_fold / algebraic / cse / fuse / dce
                            v
┌─────────────────────────────────────────────────────────────┐
│ 优化后的 Graph：仍然是同一套 IR                              │
└───────────────┬───────────────────────────────┬─────────────┘
                │ memory.py                     │
                │ liveness + offset             │
                v                               v
┌───────────────────────────────┐   ┌─────────────────────────┐
│ MemoryPlan：arena + offsets   │   │ codegen_c.py            │
└───────────────┬───────────────┘   │ C 源码 + cl + DLL        │
                │                   └────────────┬────────────┘
                │                                │
                │                   ┌────────────v────────────┐
                │                   │ codegen_llvm.py          │
                │                   │ LLVM IR + llvmlite JIT   │
                │                   └────────────┬────────────┘
                v                                v
┌─────────────────────────────────────────────────────────────┐
│ runtime.py：ctypes / JIT / benchmark / assert_close          │
└─────────────────────────────────────────────────────────────┘
```

### 一次图的生命周期

```text
nn.Module
  -> FX GraphModule
  -> env: {fx.Node -> ir.Value}
  -> Graph(inputs, ops, outputs)
  -> validate()
  -> PassManager.run()
  -> plan_memory()
  -> CCodegen.emit() 或 LLVMCodegen
  -> DLL 或 JIT 函数
  -> ndarray 进，ndarray 出
```

注意中间只有一种 IR（Intermediate Representation，中间表示）。前端、Pass、内存规划、两个后端都围绕 `Graph` 工作。这是本项目最重要的架构约束：**不让 PyTorch 对象泄漏到 Pass，也不让 C 指针泄漏到 IR**。

`CCodegen` 和 `LLVMCodegen` 都接收 `Graph` 与 `MemoryPlan`；C 后端把偏移落到 arena 指针，LLVM 后端把偏移落到 workspace 指针。

## 核心内容

### 1.1 每一层的职责与契约

| 层 | 输入 | 输出 | 不负责 |
| --- | --- | --- | --- |
| 前端 `frontend.py` | `nn.Module`、示例输入 | `Graph` | 图优化、内存分配 |
| IR `ir.py` | op 种类、输入 Value、attrs | `Value` / `Op` / `Graph` | 执行、代码生成 |
| 校验 `Graph.validate` | 一张图 | 通过或 `IRError` | 类型推导之外的分析 |
| Pass（优化遍）`passes.py` | 一张合法图 | 改写后的合法图 | 后端选择、实际执行 |
| 内存 `memory.py` | 拓扑序图 | `MemoryPlan` | 生成循环、管理 DLL |
| C 后端 `codegen_c.py` | 图 + 内存计划 | C 源码、DLL | 解释执行、动态 shape |
| LLVM 后端 `codegen_llvm.py` | 图 | LLVM IR、JIT 函数 | C ABI、DLL 加载 |
| 运行时 `runtime.py` | DLL/JIT、ndarray | ndarray、计时 | 图变换 |

这张表是你排错时的第一张地图。比如输出数值不对，先判断是“图本身错了”还是“后端读错了 stride”。前者看 `dump()` 和解释器，后者看生成代码和 `run_c_graph`。

### 1.2 支持的算子子集

IR 当前支持这些 op：

| op | 输入 | 语义 | C 后端 | LLVM 后端 |
| --- | --- | --- | --- | --- |
| `add` / `sub` / `mul` / `div` | 2 个张量 | 逐元素，支持 broadcast | 是 | 是 |
| `add_relu` | 2 个张量 | `relu(a + b)` | 是 | 是 |
| `relu` / `exp` | 1 个张量 | 逐元素 | 是 | 是 |
| `matmul` | 2 个二维张量 | 矩阵乘 | 是 | 是 |
| `matmul_bias` | 2 或 3 个 | matmul，可选 bias | 是 | 是 |
| `matmul_bias_relu` | 2 或 3 个 | matmul + bias + relu | 是 | 是 |
| `sum` | 1 个张量 | 单轴或全归约 | 是 | 是 |
| `softmax` | 1 个张量 | 按轴归一化 | 否，显式报错 | 否，显式报错 |

这个子集不是随手挑的。它覆盖了 AI 编译器的四类核心问题：

- 逐元素与 broadcast：索引生成、stride 为 0 的读法。
- 矩阵乘：循环嵌套、tiling（分块）、累加顺序。
- 归约：输出 shape 变化、轴处理。
- 融合：`matmul + add + relu` 如何变成一个 op。

`softmax` 被保留在 IR 和解释器里，但两个后端都没有实现。这是刻意的边界：它让你看到“语义层支持”和“代码生成层支持”不是一回事，也迫使学生处理显式失败。

### 1.3 五个里程碑

环境是前置条件，不计入五个里程碑；下面是从 M1 到 M5 的交付物。

| 里程碑 | 交付物 | 验收命令 | 能证明什么 |
| --- | --- | --- | --- |
| M1 IR + 解释器 | `Graph`、`evaluate_graph` | 手写图与 NumPy 对比 | 语义金标准存在 |
| M2 前端 | `from_torch` | MLP 输出与 PyTorch 对齐 | 能接住真实模型 |
| M3 优化 | `standard_pipeline` | op 数下降、输出仍对齐 | 改写不破坏语义 |
| M4 内存 + C | `plan_memory`、DLL | `run_c_graph` 误差小于 `1e-5` | 能生成可执行二进制 |
| M5 LLVM + 运行时 | `llvm_ir`、`LLVMJIT`、`benchmark` | C/LLVM/PyTorch 三方对比 | 能比较两条后端路线 |

不要把 M1 跳过。没有解释器，你无法判断 C 后端的误差是“浮点累加顺序”还是“索引写错”。解释器是整个项目的语义锚点。

### 1.4 一次真实的端到端运行

用 `examples/demo_e2e.py` 的 MLP：`Linear(8,16) -> ReLU -> Linear(16,4)`，输入 `4x8`。实际运行的关键输出如下。

原始图：

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

Pass 流水线：

```text
constant_fold                    ops   5 ->   5  changed=0
algebraic_simplify               ops   5 ->   5  changed=0
cse                              ops   5 ->   5  changed=0
fuse_ops                         ops   5 ->   2  changed=2
dce                              ops   2 ->   2  changed=0
```

优化后：

```text
ops:
  00 %relu_fused = matmul_bias_relu(x0, linear_weight_t, fc1_bias) : tensor<4, 16>
  01 %linear_1_fused = matmul_bias(relu_fused, linear_1_weight_t, fc2_bias) : tensor<4, 4>
```

内存计划：

```text
arena_bytes=320 alignment=64 peak_live_bytes=320
value                 interval     size   offset
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

后端与运行时：

```text
C max_abs_error: 4.470348358154297e-08
LLVM ir_lines: 6413
LLVM max_abs_error: 4.470348358154297e-08
benchmark: C 0.0897 ms, LLVM 0.0455 ms, PyTorch 0.0937 ms
```

这组数字说明三件事：

- 融合把 5 个 op 降到 2 个，同时保持 `1e-8` 量级的误差。
- 内存规划按 op index 算生命周期；这里 `relu_fused` 的 `end=1` 与 `linear_1_fused` 的 `start=1` 相接，`end < start` 不成立，所以没有复用，两个缓冲区分别位于 0 和 256。
- 这次运行里 LLVM 比 C 快，但这是单次样本。Ch 23 会讲为什么不能用一次 benchmark 下结论。

### 1.5 正常路径与边界路径

正常路径可以是任何一条已经支持的链路：

```python
import torch
from mini_ai import from_torch, standard_pipeline, evaluate_graph

model = torch.nn.Sequential(torch.nn.Linear(4, 8), torch.nn.ReLU()).eval()
x = torch.randn(3, 4)
g = from_torch(model, (x,))
standard_pipeline().run(g, verbose=True)
out = evaluate_graph(g, {"x0": x.numpy()})[0]
```

边界路径是往 IR 里塞一个不支持的算子：

```python
from mini_ai.ir import Graph

g = Graph()
x = g.add_input("x", (2, 3))
g.add_op("sigmoid", [x])
```

实际报错：

```text
IRError: unsupported op: sigmoid
```

错误在 `add_op` 调用 `infer_output_type` 时就发生了，图根本没建起来。这是第一道防线。第二道防线是 `Graph.validate()`，第三道是各后端的 `_emit_op`。三层都显式拒绝，避免“错误图一路走到 C 代码”。

## 底层机制

### 1.6 为什么 IR 是扁平的拓扑序列表

`Graph.ops` 是一个 `list[Op]`，没有基本块，没有嵌套区域，没有控制流。这样做的代价是表达力有限，收益是每个组件都容易写：

- Pass 只需顺序扫描或反向扫描。
- 内存规划只需按 op index 算生命周期。
- 代码生成只需按顺序展开循环。
- `validate()` 只需一次线性扫描检查“使用是否在定义之后”。

真实编译器会用 SSA（Static Single Assignment，静态单赋值）的 CFG（Control Flow Graph，控制流图）、region、block 来表达控制流。本课程先把数据流和内存问题讲透，再在 Ch 21 用 LLVM 看 SSA 和 `phi`。

### 1.7 静态 shape 与 float32 的取舍

`TensorType` 只接受 `float32`，shape 必须是具体整数。这带来两个直接结果：

- 编译器可以在生成代码时把循环边界写成常量，C 后端不需要运行时 shape 参数。
- 内存规划可以在编译期算出精确的字节数和偏移。

代价是无法处理动态 batch、混合精度、量化。这些不是“忘了做”，而是边界。Ch 4 会讲 shape/dtype/stride，Ch 24 会讨论动态 shape 的扩展方向。

### 1.8 两条后端共享什么，不共享什么

共享：

- 同一个 `Graph`。
- 同一套算子语义（`interpreter.apply_op` 是金标准）。
- 同一套静态 shape 和行主序布局。

不共享：

- C 后端生成 `mini_forward(const float* x0, float* out0)`，通过 DLL 和 `ctypes` 调用。
- LLVM 后端生成 JIT（即时编译）函数，签名是 `(inputs..., outputs..., workspace)`，由 `llvmlite` 执行。

两条路径的 ABI 不同，所以 `runtime.py` 里有两套调用封装。不要假设 C 的调用方式能直接套到 LLVM。

## 进一步用法与变体

### 1.9 新增一个算子要改哪些地方

以新增 `neg` 为例，至少要碰五处：

1. `ir.infer_output_type`：声明输入数量与输出 shape。
2. `interpreter.apply_op`：给出 NumPy 语义，作为金标准。
3. `passes.py`：如果需要常量折叠或代数化简，补规则。
4. `codegen_c.CCodegen._emit_op`：生成 C 循环。
5. `codegen_llvm.LLVMCodegen._emit_op`：生成 LLVM IR。

少改任何一处，都会在某条路径上留下“能建图但不能执行”或“能执行但结果不对”的洞。

### 1.10 换后端不改前端

前端只产出 `Graph`，所以你可以：

- 只跑解释器，做快速语义验证。
- 只跑 C，做部署验证。
- 只跑 LLVM，做 IR 和 JIT 实验。
- 在 Pass 后 dump 图，比较不同 pipeline 的形态。

这种可替换性来自 IR 的边界，不是来自某个后端的 API。

## 对比与取舍

### 1.11 为什么不直接写 TVM / MLIR / TorchInductor

| 方案 | 优点 | 你学不到的部分 | 本项目的位置 |
| --- | --- | --- | --- |
| TVM | 成熟 schedule、自动调优、多后端 | 图 IR 到 kernel 的每一层细节被封装 | 学完本课再看 `src/tir` |
| MLIR | 多 dialect、可组合 pass 基础设施 | 基础设施本身就很大，容易迷失 | 学完本课再看 Toy Tutorial |
| TorchInductor | 生产级 fusion、Triton 代码生成 | 生成链路复杂，调试门槛高 | 作为工业对照 |
| 本课程 | 500 行量级、每层可读、可改 | 不支持动态 shape、GPU、量化 | 建立第一手直觉 |

直接上 TVM 的典型问题是：你会调 `schedule`，但说不清 `compute_at` 为什么能复用 buffer；直接上 MLIR 的典型问题是：你会写 dialect，但说不清 SSA dominance 在内存规划里对应什么。本课程把这些问题缩小到能在一个下午读完的代码里。

### 1.12 C 与 LLVM 的取舍

| 维度 | C 后端 | LLVM 后端 |
| --- | --- | --- |
| 中间表示 | 可读的 C 循环 | LLVM IR，静态全展开 |
| 编译速度 | 依赖 `cl`，较慢 | `llvmlite` 构建，较快 |
| 调试 | 看 C、看汇编 | 看 IR、看 JIT |
| 优化 | 交给 MSVC | 交给 LLVM |
| 部署 | DLL 可直接分发 | 需要 Python + llvmlite |
| 本课程重点 | ABI、内存、循环 | SSA、JIT、目标机器 |

先做 C，再做 LLVM，不是为了“多写一个后端”，而是为了对比同一条 IR 在两种抽象层的落点。

## 踩雷集锦

1. **把 Pass 当成“随便改图”。** 任何改写后都必须保持 SSA、拓扑序和输出存在；否则 `validate()` 会在后端之前失败。Ch 7 会讲 Pass 的不变量。
2. **没有解释器就直接调 C。** 一旦数值不对，你无法区分浮点误差、索引错误和 ABI 错误。先跑 `evaluate_graph`。
3. **拿一次 benchmark 下结论。** 本机 `C 0.0897 ms`、`LLVM 0.0455 ms` 只代表这一次运行；需要 median、min、max 和多次重复。
4. **把 `softmax` 当成已支持。** IR 和解释器支持，C/LLVM 后端显式拒绝。看能力表，不要看函数名猜。
5. **忽略 shape 的静态约束。** 换 batch size 需要重新 trace、重新编译；`Graph` 里没有符号维度。

## 进阶

- 读 `passes.standard_pipeline()`，画出每个 pass 的输入输出形态，预测 MLP 融合后的 op 数。
- 给 `Graph` 加一个 `neg` 算子，按 1.9 的五处清单走一遍，并用 `evaluate_graph` 与 C 后端对比。
- 把 `standard_pipeline()` 里的 `fuse_ops` 去掉，观察 C 代码里多了哪些中间缓冲区，`arena_bytes` 变成多少。
- 用 `Graph.clone()` 在 Pass 前保存原图，比较优化前后的 `dump()`，确认融合没有改变输出值。

## 动手练习

1. **画架构图。** 不看本章，凭记忆画出前端、IR、Pass、内存、C/LLVM、runtime 六层，并标出每层的一个失败模式。
2. **数 op。** 运行 `demo_e2e.py`，确认原始 5 个 op、优化后 2 个 op，并解释 `fuse_ops` 的两次匹配分别是什么。
3. **制造不支持算子。** 用 `sigmoid` 复现 `IRError: unsupported op: sigmoid`，说明它发生在建图阶段还是代码生成阶段。
4. **换输入尺寸。** 把 demo 的 `4x8` 改成 `8x8`，重新运行，记录 `arena_bytes` 和两个后端的误差。思考为什么不能复用旧 DLL。
5. **只看解释器。** 跳过 C 和 LLVM，只用 `from_torch + standard_pipeline + evaluate_graph` 跑 MLP，确认数值仍与 PyTorch 对齐。

## 本章重点

- 整个项目只有一种 IR，前端、Pass、内存、后端都围绕 `Graph`。
- 支持算子是一个显式子集；`softmax` 只在语义层存在，两个后端都拒绝。
- 五个里程碑从“能解释”走到“能编译、能比较”，每一层都要有独立验收。
- 静态 shape + float32 是刻意的简化，换来可预测的循环和内存。
- 不直接上 TVM/MLIR 是为了保留每一层的可读性和可改性。

## 自我检核

- [ ] 我能画出六层架构图，并说出每层的输入输出。
- [ ] 我能列出至少 6 个支持的 op，并指出哪个后端不支持 `softmax`。
- [ ] 我能解释五个里程碑各自验证什么。
- [ ] 我知道为什么 Pass 之后还要 `validate()`。
- [ ] 我能说明 C 与 LLVM 两条后端的 ABI 差异。
- [ ] 我能在不看代码的情况下说清 `fuse_ops` 把 5 个 op 降到 2 个的大致过程。

## 延伸阅读

- **`code/mini_ai/ir.py`**：读 `TensorType`、`Graph.add_op`、`Graph.validate`；学 IR 的最小数据模型；关联 Ch 2 的 SSA 与拓扑序。
- **`code/mini_ai/passes.py`**：读 `standard_pipeline` 与 `PassManager`；学 pass 顺序和固定点；关联 Ch 7 到 Ch 10。
- **`code/mini_ai/memory.py`**：读 `plan_memory` 与 `MemoryPlan.summary`；学生命周期和偏移；关联 Ch 12 到 Ch 13。
- **`code/mini_ai/codegen_c.py`**：读 `CCodegen.emit` 与 `_emit_op`；学 C 代码生成与 arena；关联 Ch 16 到 Ch 18。
- **TVM 论文 Section 3**：读图级优化与 schedule 分离；学工业系统如何划分层次；关联本章的架构分层与 Ch 10 的融合。

[下一章：图 IR 设计](./02-graph-ir.md)
