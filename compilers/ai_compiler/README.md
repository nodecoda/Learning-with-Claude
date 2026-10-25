# AI 编译器：从计算图到机器码

> 给会写 Python、懂一点编译器、想把「模型为什么能跑快」讲清楚的工程师。

这门课沿着一条真实链路走：**PyTorch 图 → 图级优化 → 调度 → 代码生成 → runtime**。主力实验栈是 **PyTorch FX / torch.export + Apache TVM 0.27（CPU）**，并用 TorchInductor、ONNX Runtime、XLA/StableHLO、MLIR/IREE 做对照。目标不是背 API，而是能回答三个问题：这张图为什么被这样改写、这个 kernel 为什么快、这条 lowering 路径为什么这么设计。

> 证据约定：`实测` = 本机真实输出；`源码依据` = 对照具体版本源码；`guided` = 需要 GPU/额外工具链，本课不伪造输出。

## 为什么学这个？

- **AI 编译器的核心不是「翻译」，是「选择」**：融合哪些算子、怎么切 tile、用哪个 layout、要不要量化——每个选择都直接换性能。
- **图级和 kernel 级是两套问题**：图优化关心算子边界与内存流量；调度关心循环、并行与 cache。只学一头，遇到性能问题就定位不了。
- **生产栈差异很大**：TVM、TorchInductor、XLA、IREE 对同一张图会给出不同 IR 和不同 kernel；理解取舍才能选型。
- **和已有课程互补**：[`mlir_core`](../mlir_core/README.md) 讲 MLIR 基础设施，[`globalisel_regbankselect`](../globalisel_regbankselect/README.md) 讲 LLVM 机器层；本课补上「模型 → 图 IR → kernel」这一段。

## 先修知识

- **Python**（能读装饰器、类、`torch.nn.Module`）
- **PyTorch 基础**（知道 tensor、autograd、`eval()`、dtype/device）
- **编译原理基础**（IR、pass、SSA、lowering 的概念；不要求写过编译器）
- **命令行与 PowerShell**（能建 venv、跑脚本、读退出码）
- 不要求：CUDA、TVM 经验、MLIR 经验（Ch 21 会回指 `mlir_core`）

## 课程地图

### Part 0 — 心智模型与最小闭环（Ch 0–4）

- [Ch 0 环境与最小闭环](./00-environment-setup.md)：FX 抓图 → TVM 编译 → 执行 → 数值对齐
- [Ch 1 AI 编译器是什么](./01-what-is-ai-compiler.md)：框架、图编译器、kernel 编译器、runtime 的分层
- [Ch 2 图 IR 对照](./02-graph-ir.md)：FX Graph、`torch.export`、ONNX、Relax、StableHLO
- [Ch 3 张量语义](./03-tensor-semantics.md)：shape/dtype/layout/stride、broadcast、view vs copy
- [Ch 4 算子与融合边界](./04-operators-and-fusion.md)：elementwise / reduction / matmul / conv 的代价模型
- [练习 A：抓图、解释器与数值对齐](./practice-a-fx-interpreter.md)

### Part 1 — 图级优化（Ch 5–10）

- [Ch 5 FX 变换](./05-fx-transformations.md)：Interpreter、Transformer、`replace_pattern`、subgraph rewriter
- [Ch 6 常量折叠与代数化简](./06-constant-folding.md)：DCE、CSE、等价改写
- [Ch 7 算子融合](./07-operator-fusion.md)：纵向、横向、epilogue、reduction 融合与代价
- [Ch 8 布局与格式](./08-layout-and-format.md)：NCHW/NHWC、channels_last、layout propagation
- [Ch 9 量化](./09-quantization.md)：PTQ/QAT、scale/zero-point、量化算子融合、误差
- [Ch 10 内存规划](./10-memory-planning.md)：生命周期、in-place、buffer 复用、峰值内存
- [练习 B：融合、折叠与内存复用](./practice-b-graph-passes.md)

### Part 2 — 调度与代码生成（Ch 11–17）

- [Ch 11 循环优化](./11-loop-optimizations.md)：tiling / reorder / unroll / vectorize
- [Ch 12 TVM TIR 与 schedule](./12-tvm-tir-schedule.md)：0.27 新 API、split/reorder/tile/fuse
- [Ch 13 自动调优](./13-autotuning.md)：meta_schedule / autotvm、搜索空间、实测
- [Ch 14 后端代码生成](./14-codegen-backends.md)：LLVM IR / C / CUDA 三条路径
- [Ch 15 向量化与 SIMD](./15-vectorization.md)：宽度、对齐、tail
- [Ch 16 内存层次与 Roofline](./16-memory-and-roofline.md)：cache blocking、算术强度
- [Ch 17 运行时](./17-runtime.md)：GraphExecutor/VM、内存池、kernel launch
- [练习 C：手写 schedule 跑赢朴素实现](./practice-c-schedule-matmul.md)

### Part 3 — 生产栈对照（Ch 18–22）

- [Ch 18 TorchInductor](./18-torchinductor.md)：FX → Inductor IR → Triton/C++
- [Ch 19 Triton](./19-triton.md)：block-level 编程模型与编译器
- [Ch 20 XLA / StableHLO](./20-xla-stablehlo.md)：HLO 管线、fusion、layout assignment
- [Ch 21 MLIR 在 AI 编译器](./21-mlir-in-ai-compilers.md)：linalg/tensor/memref、IREE
- [Ch 22 ONNX Runtime 与 TensorRT](./22-onnxruntime-tensorrt.md)：图优化、EP、校准
- [练习 D：同一模型，三种栈](./practice-d-stack-comparison.md)

### Part 4 — 专项与边界（Ch 23–27）

- [Ch 23 动态 shape 与控制流](./23-dynamic-shapes.md)
- [Ch 24 并行与分布式](./24-parallelism.md)：数据/张量并行、通信算子
- [Ch 25 稀疏与低精度](./25-sparsity-low-precision.md)：BF16/FP8/INT4
- [Ch 26 正确性](./26-correctness.md)：golden、容差、模糊测试、数值稳定性
- [Ch 27 性能工程](./27-performance-engineering.md)：profiling、瓶颈定位、基准回归

### Part 5 — 源码与设计（Ch 28–30）

- [Ch 28 读 TVM 源码](./28-reading-tvm-source.md)：Relax → TIR → codegen
- [Ch 29 设计取舍](./29-design-tradeoffs.md)：TVM vs Inductor vs XLA vs MLIR/IREE
- [Ch 30 端到端复盘](./30-end-to-end.md)：一个 transformer block 的完整链路
- [Final Project：mini AI compiler](./final-project-mini-compiler.md)

## 环境与版本

> **环境**：Windows 10/11 x86_64，Python 3.11.12，CPU-only。所有 IR 级与 CPU 实验均在本机实测。

```powershell
python -m venv .scratch\aic-env
$py = ".scratch\aic-env\Scripts\python.exe"
& $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
& $py -m pip install apache-tvm numpy onnx onnxruntime onnxscript
$env:PYTHONIOENCODING = "utf-8"
& $py -c "import torch, tvm; print(torch.__version__, tvm.__version__)"
```

实测版本：

```text
torch 2.14.1+cpu
tvm 0.27.0.post1
onnx 1.23.2
onnxruntime 1.31.0
```

**重要**：TVM 0.27 与网上大多数教程的 API 不同（`te.create_schedule`、`tvm.nd.array`、`tvm.tir` 都已变化）。本课以**本机 0.27 实测 API** 为准，Ch 12 有完整迁移对照表。

## 本课不涵盖什么

- **CUDA kernel 手写与 GPU 性能调优**：本机无 CUDA，GPU 相关标 `guided`。
- **训练框架内部**：autograd、分布式训练器不在范围。
- **芯片后端与指令集**：那属于 `globalisel_*` 课程。
- **生产级推理服务**：只讲编译器与 runtime，不讲 serving/调度系统。
- **完整实现一个工业级编译器**：Final 是 mini compiler，不是 TVM 替代品。

## 精选资料库

### 官方文档

- **[Apache TVM 文档](https://tvm.apache.org/docs/)**：本课主线。注意看 0.27 的 TIR/Relax API，不要照抄旧教程。
- **[PyTorch FX 文档](https://pytorch.org/docs/stable/fx.html)**：`symbolic_trace`、Interpreter、Transformer 的权威说明。Ch 5 的依据。
- **[torch.export 文档](https://pytorch.org/docs/stable/export.html)**：导出 ATen 图与 dynamic shape。Ch 2、Ch 23 的依据。
- **[ONNX Runtime 文档](https://onnxruntime.ai/docs/)**：Execution Provider 与图优化。Ch 22 的依据。
- **[OpenXLA / StableHLO](https://openxla.org/stablehlo)**：HLO 与 StableHLO 规范。Ch 20 的依据。
- **[MLIR Linalg](https://mlir.llvm.org/docs/Dialects/Linalg/)**：Ch 21 与 `mlir_core` 的接口。

### 源码

- **[TVM 源码](https://github.com/apache/tvm)**：`python/tvm/relax`、`python/tvm/s_tir`、`src/target/llvm`。
- **[PyTorch Inductor](https://github.com/pytorch/pytorch/tree/main/torch/_inductor)**：`scheduler.py`、`codegen/triton.py`。
- **[Triton](https://github.com/triton-lang/triton)**：`python/triton/language`、`lib/Conversion`。
- **[IREE](https://github.com/iree-org/iree)**：MLIR 端到端编译器的生产样例。

### 论文与演讲

- **[TVM: An Automated End-to-End Optimizing Compiler for Deep Learning (OSDI 2018)](https://www.usenix.org/conference/osdi18/presentation/chen)**：TVM 的设计动机与调度抽象。
- **[Ansor: Generating High-Performance Tensor Programs for Deep Learning (OSDI 2020)](https://www.usenix.org/conference/osdi20/presentation/zheng)**：自动调优的搜索空间与 cost model。
- **[MLIR: A Compiler Infrastructure for the End of Moore's Law (arXiv:2002.11054)](https://arxiv.org/abs/2002.11054)**：多级 IR 的理论背景。

### 博客 / 技术文章

- **[PyTorch Dev Discuss: TorchInductor](https://dev-discuss.pytorch.org/)**：Inductor 设计讨论与性能报告。
- **[TVM Discuss](https://discuss.tvm.apache.org/)**：调度、codegen、自动调优的一手问答。

## 课程状态

✅ **全部完成（2026-10-24）**：README + Ch 0–30 + 练习 A–D + Final Project，共 **37 个 Markdown 文件、约 17,400 行**。

- **实测覆盖**：PyTorch FX / `torch.export` / ONNX / ONNX Runtime / TVM 0.27 的图、TIR、LLVM IR、C 代码、`.dll` 导出与运行均为本机真实输出；Final 的 mini compiler 真的生成了 C、用 `cl` 编译成 DLL 并跑出 `maxerr=0`。
- **关键校正**：TVM 0.27 已移除 `te.create_schedule`、`tvm.nd.array`、`sch.get_block`、`tvm.tir`、`get_source()`；本课按 0.27 实测 API 写，并在 Ch 12 给迁移表。
- **guided 边界**：CUDA/Triton/TensorRT/FP8 硬件（本机无可用工具链）、XLA/StableHLO 执行（未安装 JAX）、TVM C++ 源码（只有 wheel）、完整 meta_schedule（缺 `psutil`/`xgboost`）——相关章节均就地标注，未伪造输出。
