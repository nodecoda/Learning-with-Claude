# Ch 29 设计取舍：TVM、TorchInductor、XLA 与 MLIR/IREE

> **目标**：把“同一个模型，为什么四套栈给出不同 IR、不同 kernel、不同部署方式”讲清楚。用五个维度——抽象层次、融合、调度与 codegen、动态 shape 与量化、部署与生态——做对照，最后给一张“什么时候选谁”的判断清单。不排名，只解释差异来源。

## 为什么需要这个？

学完 Ch 18–22，你手里有四套栈：

- **TVM**：Relax + TIR，显式 schedule，自己 codegen。
- **TorchInductor**：FX/ATen → Inductor IR → Triton/C++，藏在 `torch.compile` 后面。
- **XLA**：HLO 纯函数式图，全局 fusion + layout assignment。
- **MLIR / IREE**：编译器基础设施 + linalg/tensor/memref，端到端 lower 到 HAL。

它们都能把 `relu(x @ W + b)` 跑起来，但做选择的位置完全不同。不理解这些位置，就会问出错误的问题：

- “为什么 TVM 不自动融合？”——它融，但融合与调度是分开的两层。
- “为什么 Inductor 不生成 matmul kernel？”——CPU 上它把 matmul 交给 BLAS。
- “为什么 XLA 的图看起来没有循环？”——HLO 是纯函数式图，循环在后端。
- “为什么 IREE 什么都能接？”——它站在 MLIR 的 dialect 生态上。

这一章不教你新 API，而是给你一张**选型地图**。选型不是“谁快选谁”，而是“谁的责任边界和你的问题匹配”。

## 先建立直觉：编译器在四个位置做选择

把一次编译拆成四个选择点：

```text
模型
 │ ① 保留成什么抽象？图算子 / 结构化循环 / kernel DSL
 ▼
IR
 │ ② 谁决定融合？图 pass / scheduler / 全局 fusion / linalg fusion
 ▼
优化后的 IR
 │ ③ 谁决定循环与线程？用户 schedule / 启发式 / 编译器内部 / transform dialect
 ▼
kernel 代码
 │ ④ 怎么部署？runtime module / in-process / PJRT / VM+HAL
 ▼
可执行产物
```

四套栈的差异，本质上是对这四个问题的回答不同：

| 选择点 | TVM | TorchInductor | XLA | MLIR/IREE |
|---|---|---|---|---|
| ① 抽象 | Relax 图 + TIR 循环 | ATen 图 + Inductor IR | HLO 纯函数图 | linalg/tensor/memref |
| ② 融合 | `FuseOps` + TIR | Scheduler 自动 | 全局 HLO fusion | linalg fusion |
| ③ 调度 | 用户写 `s_tir` / 自动调优 | 启发式 + extern | 编译器内部 + layout | transform dialect |
| ④ 部署 | TVM runtime module | torch / AOTInductor | PJRT | IREE VM/HAL |

记住这张表，后面的五维对照只是把它展开。

## 核心概念一：抽象层次决定“谁能改什么”

抽象层次不是审美问题，它决定了**哪些优化是合法的、哪些信息已经丢了**。

### 图 IR：只有算子和边

FX Graph、Relax、HLO 都属于这一类。它们描述“谁依赖谁”，不描述“循环怎么排”。

- FX 的节点是 Python 层算子（`operator.matmul`、`torch.relu`）。
- Relax 的节点是 `relax.call_tir`、`relax.matmul` 等，带 shape/dtype。
- HLO 的节点是 `dot`、`add`、`maximum`，每个值都有静态 shape。

图 IR 能做：常量折叠、DCE、CSE、算子融合、布局传播、量化改写。
图 IR 不能做：决定 `k` 切 8 还是 16、决定用不用 AVX2、决定 shared memory 大小。

### 调度 IR：循环、块、buffer

TIR 是典型代表：`for`、`sblock`、`axis`、`buffer` 都在 IR 里。调度原语（`split/reorder/vectorize`）改的就是这些结构。

Inductor IR 介于两者之间：它有 `Pointwise`、`Reduction`、`Scan` 这样的循环级节点，但不暴露给用户写 schedule。它的“调度”是 Scheduler 内部的启发式。

### kernel DSL：block 级程序

Triton 是代表：你写的是“一个 program 处理一个 block”，`tl.load/tl.store/tl.dot` 是 block 级原语。它比 TIR 更接近 GPU 编程模型，但可移植性更窄（Ch 19）。

### 一个具体例子

同一段 `y = relu(x @ W + b)`：

| 栈 | 它看到的“最小单位” | 它能直接改什么 |
|---|---|---|
| TVM | `matmul` + `add` + `relu` 三个 Relax 算子，lower 后是三个 TIR block | 融合边界、循环顺序、向量宽度 |
| Inductor | `addmm`（extern）+ `relu`（pointwise） | 是否把 relu 融进 pointwise、是否并行 |
| XLA | `dot` + `add` + `maximum` 三个 HLO 指令 | 全局 fusion、layout |
| IREE | `linalg.matmul` + `linalg.generic` | tile、fusion、bufferization |

**抽象层次越低，能做的局部优化越多；越高，能做的全局决策越多。** 这是四套栈最根本的取舍。

## 核心概念二：谁负责融合

融合不是“一个 pass”，而是“谁有权力把两个计算放进同一个 kernel”。

| 栈 | 融合决策者 | 典型机制 | 边界 |
|---|---|---|---|
| TVM | 图级 `FuseOps` + TIR schedule | 把 elementwise 生产者/消费者分到同一个 group，再 lower 成 TIR | reduction/matmul 的融合要手写或靠 pattern |
| Inductor | Scheduler | 自动把 pointwise 链合成一个 kernel；reduction 视情况；matmul 交给 extern | 跨 extern kernel 不融合 |
| XLA | HLO fusion pass | 全局 producer-consumer fusion，layout 也参与决策 | 以 computation 为单位，编译期全局看 |
| IREE | linalg fusion pass | `linalg.generic` 之间的 producer-consumer 融合 | 依赖 linalg 的结构化信息 |

三个具体场景：

1. **elementwise 链** `y = relu(x * 2 + 1)`：四家都会融。Ch 4 实测，TVM 融合后相对 eager 快 **2.5–3.5 倍**（N=8M，float32，i7-10510U，4 线程；单次运行会漂，比值比绝对值稳）。
2. **matmul + bias + relu**：TVM 可以用手写 TIR 或 pattern 把 bias/relu 融进 epilogue；Inductor 在 GPU 上可以把 pointwise 融进 Triton，CPU 上 `addmm` 仍走 BLAS；XLA 可以把 `add/maximum` 融进 dot 的输出；IREE 用 linalg fusion 做类似的事。
3. **attention 的 `scores → softmax → context`**：softmax 是 reduction，融合边界很敏感。把 scale 融进 softmax 是安全的；把 softmax 融进下一个 matmul 通常不合适，因为 reduction 的中间结果需要完整物化。Ch 30 会实测这个边界。

一个反例：Ch 4 的 diamond 图里，把一个高算术强度的 matmul 复制给两个消费者，实测慢 **2.0–2.4 倍**。**融合不是越多越好，融合是在“省内存流量”和“避免重复计算”之间做选择。**

## 核心概念三：谁负责调度与 codegen

| 栈 | 调度责任 | codegen 责任 | 用户能介入到哪 |
|---|---|---|---|
| TVM | 用户写 `s_tir`，或 `meta_schedule`/`dlight` 自动 | TVM 自己生成 LLVM/C/CUDA | 可以写到循环级 |
| Inductor | Scheduler 启发式 | CPU 生成 C++，GPU 生成 Triton；matmul 调 extern | 基本不介入，除非写自定义 backend |
| XLA | 编译器内部 pass | LLVM（CPU/GPU）、TPU 后端 | 基本不介入，靠 HLO 表达意图 |
| IREE | transform dialect / linalg tiling | LLVM/SPIR-V 等，经 HAL | 可以写 transform script |

TVM 的独特之处是**把调度暴露给用户**。Ch 11 实测：同一个 N=256 matmul，`vectorize` 约快 10 倍，`parallel + vectorize` 约快 16 倍（同轮 baseline 比值，不是跨机器绝对值）。这不是 TVM “更会优化”，而是“它允许你把优化写出来”。

Inductor 的独特之处是**把调度藏起来**。它用启发式选 tile/并行，用户只写 `torch.compile`。代价是：遇到启发式不覆盖的形状，你很难插手。

XLA 的独特之处是**全局决策**。它可以在整张 computation 上做 layout assignment 和 fusion，代价是编译期更重、对动态 shape 更保守。

IREE 的独特之处是**可组合**。它不发明一套新 IR，而是用 MLIR 的 dialect 拼管线；代价是你要理解多层 dialect 的边界（Ch 21）。

## 核心概念四：部署与生态是硬约束

编译器选型到最后，常常不是“哪个 IR 更优雅”，而是“产物能不能在目标环境加载”。四套栈的部署形态差别很大：

| 栈 | 典型产物 | 怎么加载 | 运行时依赖 | 适合的场景 |
|---|---|---|---|---|
| TVM | `.so/.dll` + TVM runtime module | `tvm.runtime.load_module` 或 C++ runtime | libtvm / 目标后端库 | 独立 C++ 服务、边缘设备 |
| Inductor | 进程内编译产物；AOTInductor 可导出 | `torch.compile` 直接调用；AOT 产物用 C++ 接口 | PyTorch / libtorch | 已有 PyTorch 服务 |
| XLA | executable（PJRT） | PJRT client | XLA/PJRT 插件 | JAX/TF、TPU/GPU 集群 |
| IREE | `.vmfb` | IREE runtime / HAL | IREE runtime + 目标 HAL | 多硬件、嵌入式、自定义后端 |

三个具体问题，选型时一定要问：

1. **目标环境有没有 Python/PyTorch？** 没有的话，Inductor 的进程内路线就要排除，除非用 AOTInductor。
2. **目标硬件的算子覆盖够不够？** 编译器能生成代码，不等于每个算子都有高效实现；attention、LayerNorm、量化算子经常是缺口。
3. **出问题能不能定位？** TVM 能打印 TIR/LLVM IR；Inductor 能 dump 生成的 C++/Triton；XLA 能 dump HLO；IREE 能 dump 各层 dialect。选你能读得懂的那一层。

生态还包括社区、文档、版本节奏。TVM 的调度/自动调优资料多，但 0.27 的 API 变动大；Inductor 跟着 PyTorch 走，版本兼容性好但内部 API 不稳定；XLA 绑定 JAX/TF 生态；IREE 的文档相对新，适合愿意读源码的团队。**选型不是选“最强”，是选“出问题时你能修”的。**

### 量化与部署的入口

把量化和部署拆开看，四套栈的入口大致是：

| 栈 | 量化入口 | 部署入口 | 常见坑 |
|---|---|---|---|
| TVM | Relax `quantize/dequantize` 或自定义 pass | `export_library` + runtime | 量化算子覆盖不全，回退后反而更慢 |
| Inductor | PyTorch pt2e / `torch.ao` | `torch.compile` / AOTInductor | 量化后的算子仍可能走 extern |
| XLA | int8 量化类型 + 后端 legalize | PJRT | 后端不支持时退化为 fp32 |
| IREE | quantized linalg | IREE VM/HAL | scale/zero-point 的 layout 要匹配 |

一个反直觉的结论：**“编译器支持量化”不等于“你的模型会变快”**。Ch 22 的实测里，小矩阵动态量化 int8 反而比 fp32 慢（0.1017 ms vs 0.0625 ms），因为每次 run 都要做 `DynamicQuantizeLinear`。量化收益取决于 shape、算子、校准质量，不要让编译器替你做这个判断。

## 底层机制：差异到底来自哪里

四套栈的差异，不是“实现质量”，而是三个结构性选择：

1. **IR 的粒度**：TVM 把循环暴露出来，所以能把 `relu` 融进 matmul 的 epilogue；Inductor 的 IR 已经是循环级节点，融合在 Scheduler 里以 kernel 为单位做；XLA 的 HLO 是纯函数图，融合是全局 pass；IREE 的 linalg 带 indexing maps，融合依赖结构化信息。
2. **决策权归谁**：TVM 把调度权交给用户/调优器；Inductor 交给启发式；XLA 交给编译器内部；IREE 交给 transform script。决策权越靠用户，越能针对特定硬件；越靠编译器，越容易跨模型复用。
3. **runtime 的边界**：TVM 自带 module/runtime；Inductor 依附 PyTorch；XLA 通过 PJRT 插件；IREE 用 VM/HAL。runtime 决定了“编译产物能不能在目标环境加载”，也决定了调试手段。

把这三条落到 `relu(x @ W + b)`：

| 栈 | IR 粒度 | 融合决策 | runtime 边界 |
|---|---|---|---|
| TVM | TIR 循环 + block | `FuseOps` 分组，TIR epilogue | TVM module |
| Inductor | `Pointwise`/`ExternKernel` | Scheduler 分区 | PyTorch 进程 |
| XLA | HLO 指令 | 全局 fusion pass | PJRT |
| IREE | `linalg` + `scf` | linalg fusion + transform | IREE VM/HAL |

一个常见误解是“抽象层次越高越先进”。实际上，层次越高，编译器能看到的全局信息越多，但能控制的局部细节越少。TVM 让你写 `vectorize(16)`，XLA 不给你这个开关；反过来，XLA 能做跨整图的 layout 决策，TVM 需要你手动传播 layout。**选择抽象层次，就是选择“你想控制哪一段”。**

## 五维对照表

| 维度 | TVM | TorchInductor | XLA | MLIR / IREE |
|---|---|---|---|---|
| 抽象层次 | Relax 图 + TIR 循环 | ATen 图 + Inductor IR（循环级节点） | HLO 纯函数图 | linalg/tensor/memref 多级 |
| 融合 | `FuseOps` 分组 + TIR epilogue | Scheduler 自动，extern 例外 | 全局 HLO fusion | linalg fusion |
| 调度/codegen | 显式 schedule + 自动调优；自研 codegen | 启发式；CPU C++ / GPU Triton；matmul extern | 编译器内部；LLVM/TPU | transform dialect；LLVM/SPIR-V/HAL |
| 动态 shape/量化 | Relax 符号维；`quantize/dequantize` | SymInt + guards；pt2e 量化算子 | 有界动态维；int8 量化类型 | 动态 shape；quantized linalg |
| 部署/生态 | TVM runtime module；MLC 等 | `torch.compile` 进程内；AOTInductor | PJRT；JAX/TF/TPU | IREE VM/HAL；多硬件 |

### 维度 1：抽象层次

TVM 是“图 + 循环”双层：Relax 负责图级，TIR 负责循环级。Inductor 只有一层“循环级 IR”，图级信息在 lowering 时被吸收。XLA 是单层纯函数图，循环完全在后端。IREE 是多层 dialect，逐层 lower。

具体例子：`q @ k^T` 在 TVM 里最终是 `T.grid(4,16,16,16)` 的 TIR；在 Inductor 里可能是一个 `ExternKernel`（CPU BLAS）或一个 Triton kernel（GPU）；在 XLA 里是一条 `dot`；在 IREE 里是 `linalg.matmul`，再 tile 成 `scf.for`。

### 维度 2：融合

TVM 的 `FuseOps` 是图级分组，真正的循环融合由 TIR 决定；Inductor 的 Scheduler 直接以 kernel 为单位融合；XLA 的 fusion 是全局 pass；IREE 的 fusion 依赖 linalg 的 producer-consumer 关系。

具体例子：`relu(x @ W + b)`。TVM 可以用手写 TIR 或 pattern 把 `add/relu` 融进 matmul 的 epilogue；Inductor CPU 把 `addmm` 交给 BLAS，`relu` 单独一个 pointwise kernel；在支持的后端上，XLA 可以把 `add/maximum` 融进 dot 的输出；IREE 用 linalg fusion 把 `generic` 融进 matmul 的消费者。

### 维度 3：调度与 codegen

TVM 让你写 `split/reorder/vectorize`，也可以用 `meta_schedule` 搜；Inductor 用启发式；XLA 用内部 pass；IREE 用 transform dialect。

具体例子：Ch 11 的 matmul，TVM 通过 `vectorize` 把标量循环变成 `<16 x float>`，再用 `parallel` 分核；Inductor 在 CPU 上直接调 BLAS，根本不给用户这个机会；XLA 在 HLO 层决定 layout，循环由 LLVM 生成；IREE 用 `linalg.tile` + `scf.for` 生成循环。

### 维度 4：动态 shape 与量化

TVM Relax 用符号维表达动态 shape；Inductor 用 SymInt + guards，shape 变了可能重编译；XLA 用有界动态维；IREE 支持动态维，但优化空间受限于编译期可见的 bound。

量化方面：TVM 0.27 的 Relax 有 `relax.op.quantize/dequantize`（旧 Relay 时代常用 qnn 方言）；Inductor 通常走 `torch.ao`/pt2e 导出的量化算子，再交给后端；XLA 有 int8 量化类型；IREE 用 quantized linalg。**量化不是编译器单独能解决的**，它牵涉校准、scale/zero-point、算子支持（Ch 9、Ch 22）。

具体例子：batch 从 1 变到 8。TVM 如果编译时写死 1，就要重编译；Relax 符号维可以编一次跑多 shape，但调度要保守。Inductor 的 guard 会检测 shape 变化并重编译，Ch 18 的“越跑越慢”就是这个机制。XLA 需要有界动态维，否则按静态 shape 特化。IREE 支持动态维，但 tile 大小要保守。

### 维度 5：部署与生态

TVM 产出 runtime module，可以 `export_library` 成 `.so/.dll`，自带 runtime；Inductor 主要活在 PyTorch 进程里，AOTInductor 可以导出独立产物；XLA 通过 PJRT 插件部署到 TPU/GPU/CPU；IREE 用 VM + HAL，面向多硬件。

具体例子：把模型部署到没有 PyTorch 的 C++ 服务。TVM 的 `export_library` + runtime 是直接路径；Inductor 需要 AOTInductor 或 TorchScript 路线；XLA 需要 PJRT 客户端；IREE 产出 `.vmfb`，用 IREE runtime 加载。

## 示例一：同一段计算，四家分别落在哪

以 `y = relu(x @ W + b)`，`x: [1,16,64]`、`W: [64,128]`、`b: [128]` 为例。下面不是某次运行的输出，而是**每条栈的落点**（XLA/IREE 本机未装，标 `guided`）：

| 步骤 | TVM | Inductor | XLA（guided） | IREE（guided） |
|---|---|---|---|---|
| 图入口 | Relax `matmul/add/relu` | ATen `addmm/relu` | HLO `dot/add/maximum` | `linalg.matmul` + `linalg.generic` |
| 融合 | `FuseOps` 把 add/relu 分组 | Scheduler 把 relu 做成 pointwise | HLO fusion 把 add/max 融进 dot | linalg fusion |
| 调度 | 你写/自动生成 TIR schedule | 启发式；CPU 走 BLAS | layout assignment + LLVM | transform dialect tile |
| 产物 | LLVM/C module | C++/Triton kernel | LLVM/TPU executable | `.vmfb` + HAL |

一个容易误读的点：**“融合”发生在不同层，不等于效果一样**。TVM 的融合是“图分组 + TIR 循环”，Inductor 的融合是“Scheduler 分区”，XLA 的融合是“HLO 指令替换”，IREE 的融合是“linalg 生产者-消费者合并”。名字相同，IR 不同。

## 示例二：动态 shape 的四种处理

假设模型 batch 在 1–8 之间变化：

| 栈 | 做法 | 代价 | 什么时候会翻车 |
|---|---|---|---|
| TVM Relax | 符号维 `n`，编译一次 | 调度保守，可能不如特化快 | 形状约束太多、pass 不支持符号 |
| Inductor | SymInt + guards | shape 变了触发重编译 | 每种 shape 都编译，启动抖动 |
| XLA | 有界动态维 | 编译期要知道上界 | 无界动态维退化为特化/填充 |
| IREE | 动态维 + tile | 运行时循环边界 | tile 假设与真实 shape 不匹配 |

判断标准：**shape 变化频率**。如果线上只有 3 种 shape，特化编译三次可能比动态 shape 更快；如果 shape 每次请求都不同，动态 shape 或 padding 更合适。不要背“动态 shape 一定更好”。

## 示例三：量化与部署不是一回事

量化回答“用什么精度算”，部署回答“产物怎么加载”。两者经常被混为一谈：

- **TVM**：Relax 层 `quantize/dequantize`，或把量化算子 lower 成 TIR；部署用 runtime module。
- **Inductor**：量化通常由 PyTorch 的 pt2e 流程完成，Inductor 只负责把量化后的算子编译/调用；部署用 `torch.compile` 或 AOTInductor。
- **XLA**：int8 量化类型 + 后端支持；部署走 PJRT。
- **IREE**：quantized linalg + HAL；部署走 VM。

一个实际建议：如果你的目标是“在 NVIDIA GPU 上低延迟推理”，ORT/TensorRT（Ch 22）往往比“自己用 TVM 做量化”更省事；如果你的目标是“自定义硬件 + 自定义量化格式”，TVM/IREE 的可控性更重要。

## 什么时候选谁：判断清单

按顺序问自己：

1. **模型已经在 PyTorch 里，想要一行加速？**
   → 先试 TorchInductor（`torch.compile`）。失败再加 TVM/IREE。理由：抓图、guard、fallback 都是现成的。
2. **需要自己写 schedule、搜 tile、针对特定 CPU/加速器调优？**
   → TVM。理由：`s_tir` 把循环暴露给你，`meta_schedule` 能搜。
3. **主力是 JAX/TensorFlow，或者目标包含 TPU？**
   → XLA。理由：HLO 是这些前端的第一等公民，PJRT 是部署路径。
4. **需要接多种硬件/自定义 dialect/自己做编译器基础设施？**
   → MLIR/IREE。理由：dialect + pass + HAL 的组合性最好。
5. **要导出独立于框架的 `.so/.dll`，在 C++ 服务里跑？**
   → TVM 或 IREE；Inductor 需要 AOTInductor。先看目标硬件有没有现成 backend。
6. **动态 shape 是核心需求？**
   → TVM Relax 或 IREE；Inductor 的 guard 会重编译；XLA 需要有界动态维。
7. **只是想把 ONNX 模型跑快，不想改编译器？**
   → ONNX Runtime / TensorRT（Ch 22），不是本章四选一。

### 不要选的情况

- **团队里没人愿意读 IR**：不要选需要手写 schedule 的路线；Inductor/ORT 更省人力。
- **模型每秒变一次结构**：任何 AOT 编译器都会把成本花在重编译上；先解决“为什么结构会变”。
- **目标硬件没有后端**：TVM/IREE 的“可扩展”不等于“免费支持”；写一个新 backend 是项目级投入。
- **只想量化一个小模型**：先试 ORT 动态量化（Ch 22 的实测里，int8 甚至更慢），不要直接上编译器。

## 一个真实的选型案例

把上面的维度落到四个常见场景：

| 场景 | 约束 | 推荐 | 理由 | 放弃什么 |
|---|---|---|---|---|
| 推荐系统 CPU 服务，已有 PyTorch | 两人团队，要求 1.2–1.5 倍加速 | Inductor，必要时 ORT | 抓图/回退/部署都现成 | 细粒度调度控制 |
| 自研 NPU，需要自定义 tile 与指令 | 团队能读 IR，有编译期人力 | TVM | `s_tir` + 自定义 codegen | 自动融合的便利 |
| JAX 训练 + TPU 推理 | 前端已是 JAX | XLA | HLO/PJRT 是一等公民 | 非 TPU 后端的灵活性 |
| 多硬件产品线，需统一 IR | 要接 CPU/GPU/NPU | MLIR/IREE | dialect + HAL 可组合 | 短期交付速度 |

这张表不是标准答案，而是**约束到选择的映射**。同样叫“推理加速”，推荐系统、训练框架、边缘设备的最优解完全不同。

## 对比与取舍

没有“最好的 AI 编译器”，只有“责任边界匹配”的编译器。把前面的实测数字放在一起看：

- **融合的收益是真实的，但有边界**：Ch 4 实测 elementwise 融合快 2.5–3.5 倍；同一章 diamond 反例里，重复 matmul 慢 2.0–2.4 倍。融合不是无条件正确。
- **调度的收益可以很大**：Ch 11 实测 N=256 matmul，`vectorize` 约 10 倍、`parallel + vectorize` 约 16 倍（同轮 baseline 比值）。但这是“写了 schedule 的 TVM”，不是“默认 TVM”。
- **默认调度可能很慢**：练习 D 实测，TVM 默认 lowering 的 `64×784 @ 784×256` matmul median 约 **52.5 ms**；同一张图 ORT `ENABLE_ALL` 的 median 是 **0.239 / 0.354 ms**（两次运行）。这不是 TVM 不行，是“没调度的 TVM”不行。
- **数值对齐不是排名**：练习 D 里 ORT `maxerr 2.09e-06`、TVM `3.87e-06`、Inductor `0.0`。`0.0` 只说明 Inductor 复用了同一个 BLAS `addmm`（Ch 18），不说明它更准。
- **首次成本差异巨大**：练习 D 实测 TVM build 0.32–0.44 s、ORT session 0.005–0.037 s、Inductor 首次 12.7–27.9 s（新进程 + C++ 编译）。选型时必须区分首次成本和稳态成本。

这些数字都来自本机 CPU（i7-10510U 级别，4 线程）的一次或多次运行，会随机器状态漂移。**引用时带章节和条件，不要把一次测量写成常数。**

### 成本模型：首次 vs 稳态

| 栈 | 首次成本 | 稳态成本 | 本课证据 |
|---|---|---|---|
| TVM | `tvm.build` 秒级 | kernel 质量决定 | 练习 D：0.32–0.44 s |
| Inductor | 十几秒到几十秒（含 C++ 编译） | kernel + guards | 练习 D：12.7–27.9 s；Ch 18：36.8 s |
| XLA | 编译期较重（`guided`） | executable 执行 | 本机未装，不给数字 |
| IREE | 编译期中等（`guided`） | VM/HAL 调度 | 本机未装，不给数字 |

这张表解释了一个常见错觉：“Inductor 比 TVM 慢”。在小模型、首次调用场景，Inductor 的编译成本占主导；在稳态、大模型场景，结论可能反过来。**先问你看的是首次还是稳态，再谈快慢。**

## 踩雷集锦

1. **把“自动融合”当成“所有算子都融合”**：Inductor 的 matmul 走 extern，TVM 的 reduction 融合要手写/pattern，XLA 的 fusion 也受 layout 限制。
2. **拿默认 TVM 和调过的 Inductor 比**：练习 D 的 52.5 ms 是“无 schedule 的 TVM”；Ch 11 的 10–16 倍是“写了 schedule 的 TVM”。比较要同条件。
3. **把 `maxerr=0.0` 当“更准”**：那只是同一条 BLAS 路径。
4. **忽略首次成本**：Inductor 首次 12.7–27.9 s，TVM build 0.3–0.4 s；交互式场景和离线场景结论不同。
5. **以为 XLA 能随便动态 shape**：HLO 需要静态/有界 shape，无界动态维会被特化或拒绝。
6. **以为 MLIR 等于 IREE**：MLIR 是基础设施；IREE 是用它搭的端到端编译器。Ch 21 已区分。
7. **把量化当成编译器的开关**：量化牵涉校准、算子支持、精度验收；ORT/TensorRT 的量化路径和 TVM 的 quantize op 不是同一层。
8. **忽略部署 runtime**：编译产物能不能在目标环境加载，和 kernel 快不快是两个问题。

## 进阶：再往深一层

### 1. 用同一张图做“责任边界”实验

把 `relu(x @ W + b)` 分别用 TVM、Inductor 导出 IR/代码，标出“融合发生在哪一行”。不要比时间，先比**决策位置**。这比背表格有用。

### 2. 读 Inductor 的 Scheduler 注释

`torch/_inductor/scheduler.py` 的类注释直接写了 “responsible for fusion, reorder, and graph partition”。Ch 18 引过。把它和 TVM 的 `FuseOps` 对照，能看到“自动”与“显式”的差别。

### 3. 读 XLA 的 layout assignment

HLO 的 layout 是全局决策；TIR 的 layout 多半体现在 buffer 索引和 schedule 上。Ch 20 有 HLO 管线说明（`guided`）。

### 4. 读 IREE 的 transform dialect

IREE 用 transform script 描述 tile/fuse，而不是 C++ pass 写死。Ch 21 和 `mlir_core` 课程有基础。适合想“把调度策略变成数据”的团队。

## 动手练习

### 练习 1：给四套栈各写一句“谁负责融合”

要求不超过 30 字，且指出机制名。写完对照本章表格。

<details>
<summary>参考思路</summary>

TVM：图级 `FuseOps` 分组，TIR 决定循环融合。Inductor：Scheduler 自动分区。XLA：HLO 全局 fusion pass。IREE：linalg producer-consumer fusion。
</details>

### 练习 2：同一张图，比较“默认”和“调过”的 TVM

用 Ch 11 的 matmul 脚本，分别跑无 schedule、`vectorize`、`parallel+vectorize`。记录同轮 baseline 比值。回答：为什么不能把 52.5 ms 当成 TVM 的代表值？

<details>
<summary>参考思路</summary>

52.5 ms 来自练习 D 的无 schedule TE matmul；Ch 11 的 vectorize/parallel 是显式调度。两者不是同一个配置。比较必须同图、同 shape、同线程数、同测量方法。
</details>

### 练习 3：动态 shape 决策

假设线上 batch 只有 1、4、8 三种。分别估算“特化三次”和“符号维一次”的编译/运行成本。你会选哪种？给出理由。

<details>
<summary>参考思路</summary>

如果编译成本可接受（TVM build 秒级），特化三次通常更快；如果编译成本高（Inductor 首次十几秒）或 shape 组合爆炸，符号维/动态 shape 更好。没有绝对答案，取决于请求分布。
</details>

### 练习 4：写一张自己的选型清单

给你的项目加三个约束（硬件、团队、部署方式），从本章清单里删掉不适用的选项，留下 1–2 个。说明你放弃了什么。

<details>
<summary>参考思路</summary>

例如“边缘 ARM + 无 PyTorch runtime + 两人团队”：TVM 或 IREE；放弃 Inductor（部署重）和 XLA（生态不匹配）。放弃的是“自动融合的便利”，换“可导出的 runtime”。
</details>

## 本章重点整理

- 四套栈的差异不是“谁更强”，而是“谁在哪个位置做选择”。
- TVM：图 + 循环双层，调度显式，codegen 自研。
- Inductor：循环级 IR，Scheduler 自动融合，matmul 常走 extern。
- XLA：HLO 纯函数图，全局 fusion + layout。
- IREE：MLIR dialect 组合，linalg → transform dialect → HAL。
- 五个维度：抽象、融合、调度/codegen、动态 shape/量化、部署/生态。
- 选型先问“团队能维护哪一层”，再问“性能”。
- 所有性能数字都要带机器、样本数、波动范围；本章只引用前文章节的实测，并注明来源。

## 自我检核

- [ ] 我能说出 TVM 的融合和调度分别发生在哪一层。
- [ ] 我能解释 Inductor 为什么在 CPU 上“看不到 matmul kernel”。
- [ ] 我知道 HLO 为什么没有显式循环。
- [ ] 我能区分 MLIR（基础设施）和 IREE（端到端编译器）。
- [ ] 我能说出动态 shape 在四套栈里的四种处理方式。
- [ ] 我不会用一次 benchmark 给四套栈排名。

## 延伸阅读

- **Ch 18《TorchInductor》**：读 Dynamo → Inductor IR → Scheduler → C++/Triton。学什么：自动融合的责任边界。与本课关联：本章维度 2、3。
- **Ch 20《XLA / StableHLO》**：读 HLO 管线、fusion、layout assignment。学什么：全局决策的代价。与本课关联：本章维度 1、3。
- **Ch 21《MLIR 在 AI 编译器》**：读 tensor/linalg/memref 与 bufferization。学什么：多级 IR 的分工。与本课关联：本章维度 1、5。
- **TVM 论文（OSDI 2018）**：读调度抽象的设计动机。学什么：为什么把 schedule 暴露给用户。与本课关联：本章维度 3。
- **IREE 文档**：读 input dialect → flow → HAL 的管线。学什么：生产级 MLIR 编译器的分层。与本课关联：本章维度 5。

---

→ [Ch 30 端到端复盘：一个 transformer block 的完整链路](./30-end-to-end.md)
