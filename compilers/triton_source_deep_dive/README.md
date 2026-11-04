# Triton 源码深潜：从 Python AST 到 MLIR、LLVM 与 GPU 机器码

> 给已经懂编译器前端/IR/后端、想真正读懂并修改 Triton 的工程师。

这门课以本机 **Triton 3.9.0**（commit `a972119e92a27b445cd160017ea461e56148590c`）为准，沿着一条真实 kernel 的完整生命周期走：Python AST → TTIR → TTGIR → LLIR（MLIR LLVM dialect）→ LLVM IR → PTX → cubin → SASS → launch。目标不是会写 `@triton.jit`，而是能回答：这段 IR 为什么长这样、这个 layout 为什么被选中、这个 pass 改了什么、这条 lowering 为什么这么写、出问题时应该在哪一层定位。

> **证据约定**：`实测` = 本机真实运行得到；`源码依据` = 对照上述 commit 的文件与行号；`guided` = 需要 Hopper/Blackwell、ROCm 或额外工具链，本课不伪造输出。版本、驱动、GPU 架构不同的地方会明确标注。
>
> **运行时证据的版本说明**：本机 venv 里可用的 Triton wheel 是 3.9.0（另有章节用 3.8.0/3.6.0 wheel 观察 lowering 形态），因此部分 `实测` 的 IR/PTX/SASS 来自 wheel 而非从 commit `a972119e92` 的源码构建；每章会写明用的是哪个版本。**所有 `源码依据` 的行号始终以 `a972119e92` 为准**，两者不要混用。

## 为什么学这个？

- **AI 编译器的主战场已经从“图”下沉到“kernel”**：PyTorch Inductor、vLLM、SGLang、FlashAttention、Triton kernels 生态都把 Triton 当作可编程的 GPU 后端。读懂 Triton，才能判断一个算子该融合、该换 layout，还是该手写 CUDA。
- **Triton 是“编译器教科书概念的活体标本”**：它同时包含 AST 解释/生成、SSA、MLIR dialect、类型转换、layout 分析、循环变换、软件流水线、warp specialization、LLVM/NVPTX 后端和运行时缓存。你可以在一套代码里看到这些概念如何互相咬合。
- **能改和能维护是两回事**：会写 pass 不够；你还要知道 pipeline 顺序为什么不能乱、layout 冲突如何复现、cache key 为什么失效、lit/pytest 如何锁行为、上游 review 关心什么。
- **和已有课程互补**：[`compiler_frontend`](../compiler_frontend/README.md) 讲传统 lexer/parser/AST；[`mlir_core`](../mlir_core/README.md) 讲 MLIR 基础设施；[`ssa_optimizations`](../ssa_optimizations/README.md) 讲 SSA 优化；[`inductor_deep_dive`](../inductor_deep_dive/README.md) 讲 TorchInductor。本课把 Triton 这一段放大到源码级，并把它们串成一条端到端链。

## 先修知识

- **C++17**（程度：能读模板、虚函数、LLVM/MLIR 的 `dyn_cast`、`RewritePattern`）
- **LLVM IR 与后端基础**（程度：知道 module/function/basic block/SSA、`llc`、PTX 大致是什么）
- **MLIR 基础**（程度：知道 Operation/Region/Block/Value、dialect、trait、interface、pass、rewrite pattern、dialect conversion；不熟可以先读 [`mlir_core`](../mlir_core/README.md)）
- **GPU 编程基础**（程度：知道 grid/block/warp/thread、global/shared memory、coalescing、barrier、occupancy；不熟可以先读 [`gpu_cuda`](../../parallel/gpu_cuda/README.md)）
- **Python 运行时基础**（程度：知道装饰器、`inspect`、AST、`exec`、缓存）
- 不要求：写过 Triton kernel、写过 MLIR dialect、懂 Hopper/Blackwell、懂 ROCm

## 课程地图

### Part 0 — 建立全局地图（Ch 0–3）

- [Ch 0 环境、源码地图与可复现实验](./00-environment-and-source-map.md)：构建/检查 Triton 3.9，跑通第一颗 kernel，收集 IR/PTX/SASS
- [Ch 1 Triton 在 AI 编译器栈中的位置](./01-triton-in-the-ai-compiler-stack.md)：从 PyTorch/Inductor 到 Triton，再到 MLIR/LLVM/NVPTX
- [Ch 2 Python AST 与 Triton 的语言边界](./02-python-ast-and-triton-language-boundary.md)：lexer/parser 在哪里结束，AST/语义/IR 在哪里开始
- [Ch 3 第一颗 kernel 的完整旅程](./03-first-kernel-end-to-end.md)：`ttir → ttgir → llir → ptx → cubin` 每一层看什么

### Part 1 — 前端与运行时（Ch 4–9）

- [Ch 4 `@triton.jit`、JITFunction 与特化](./04-jitfunction-specialization-and-cache.md)：调用、binder、cache key、`ASTSource`
- [Ch 5 AST → TTIR：CodeGenerator 与控制流](./05-ast-to-ttir-code-generator.md)：`visit_*`、SSA 构造、`scf.if/for/while`、循环携带值
- [Ch 6 语义层：类型、提升、广播与指针](./06-semantic-layer-types-broadcast-and-pointers.md)：`TritonSemantic` 如何把 `tl.*` 变成 IR op
- [Ch 7 语言值模型：tensor、dtype、constexpr、descriptor](./07-language-value-model-and-tensor-descriptors.md)：`core.py` 的对象模型与 flatten/unflatten
- [Ch 8 运行时：driver、launcher、CompiledKernel、autotuner](./08-runtime-driver-launcher-and-autotuner.md)：从 `kernel[grid]` 到 `cuLaunchKernel`
- [Ch 9 缓存、knobs、错误定位与前端调试](./09-cache-knobs-and-frontend-debugging.md)：cache invalidation、IR dump、traceback、reproducer
- [练习 A：前端追踪器](./practice-a-frontend-tracer.md)：不看答案，追踪一颗 kernel 从 Python AST 到 TTIR 的每个值

### Part 2 — IR、layout 与内存模型（Ch 10–15）

- [Ch 10 TTIR 方言：类型、op、属性与验证器](./10-ttir-dialect-and-verifiers.md)：`tt.load/store/dot/reduce`、pointer/tensor descriptor、接口
- [Ch 11 MLIR 对象模型在 Triton 中的落点](./11-mlir-object-model-in-triton.md)：Operation/Region/Block/SSA、trait/interface、verifier、rewriter
- [Ch 12 TritonGPU 布局代数](./12-tritongpu-layout-algebra.md)：Blocked/Slice/DotOperand/Shared/MMA/CGA
- [Ch 13 LinearLayout：把布局变成可计算的矩阵](./13-linear-layout.md)：basis、维度、bijection、layout 转换与验证
- [Ch 14 布局传播与转换消除](./14-layout-propagation-and-conversion-elimination.md)：RemoveLayoutConversions、Coalesce、ReduceDataDuplication
- [Ch 15 内存层次与异步原语](./15-memory-hierarchy-tma-and-barriers.md)：global/shared/TMEM、TMA、async copy、mbarrier/fence
- [练习 B：layout 与 TTGIR 实验](./practice-b-layout-and-ttgir.md)：从 TTGIR 读出线程映射，故意制造 layout 冲突并修复

### Part 3 — Pass、调度与并行（Ch 16–21）

- [Ch 16 Pass 基础设施与 NVIDIA pipeline](./16-pass-infrastructure-and-nvidia-pipeline.md)：从 `add_stages` 读完整 pipeline，理解顺序约束
- [Ch 17 TTIR 优化与 rewrite pattern](./17-ttir-optimizations-and-rewrite-patterns.md)：canonicalize、combine、CSE、LICM、unroll、dialect conversion
- [Ch 18 TTGIR 优化](./18-ttgir-optimizations.md)：coalesce、thread locality、dot operand、data duplication
- [Ch 19 软件流水线](./19-software-pipelining.md)：latency assignment、schedule、pipeline expander、`num_stages`
- [Ch 20 Warp specialization](./20-warp-specialization.md)：partition、producer/consumer、TMA/WGMMA 协作
- [Ch 21 Blackwell：TMEM、tcgen05 与 CLC](./21-blackwell-tmem-tcgen05-clc.md)：SM100 的新内存层次与 MMA 路径
- [练习 C：写一个 pass 并接入 pipeline](./practice-c-pass-and-pipeline.md)：实现一个可验证的 TTGIR 变换，锁 lit/pytest，观察 pipeline 影响

### Part 4 — 降到 LLVM 与机器码（Ch 22–28）

- [Ch 22 TritonGPU → LLVM conversion 框架](./22-tritongpu-to-llvm-conversion.md)：ConversionTarget、TypeConverter、pattern 注册、materialization
- [Ch 23 标量与逐元素 lowering](./23-elementwise-and-type-lowering.md)：scalarization、vectorization、fp8/fp4、rounding
- [Ch 24 Load/store/atomic lowering](./24-load-store-atomic-lowering.md)：地址计算、mask、cache modifier、地址空间、原子语义
- [Ch 25 Dot/MMA lowering](./25-dot-mma-lowering.md)：MMAv2/v3/v5、WGMMA、tcgen05 的寄存器与 shared memory 视图
- [Ch 26 Reduce/scan/gather/histogram 与 convert_layout](./26-reduce-scan-gather-and-layout-conversion.md)：warp shuffle、shared memory、跨 warp/CTA
- [Ch 27 Shared memory 分配与同步](./27-shared-memory-allocation-and-synchronization.md)：AllocateSharedMemory、membar、proxy fence、barrier 插入
- [Ch 28 LLVM IR、NVPTX、PTX、cubin、SASS](./28-llvm-ir-nvptx-ptx-cubin-sass.md)：`llvm.to_module` 之后的全部世界
- [练习 D：自定义 op 的完整 lowering](./practice-d-custom-lowering.md)：从 TTIR op 到 LLVM/PTX，写 lit + pytest

### Part 5 — Gluon、AMD、正确性与维护（Ch 29–33）

- [Ch 29 Gluon：显式 layout 的“低层 Triton”](./29-gluon-explicit-layouts.md)：为什么需要 Gluon、async copy、TMA、WGMMA/tcgen05、warp specialization
- [Ch 30 AMD 后端与可移植性](./30-amd-backend-and-portability.md)：MFMA/WMMA、HIP/ROCm、AMD pipeline 与 NVIDIA 的差异
- [Ch 31 调试、正确性与 sanitizer](./31-debugging-correctness-and-sanitizers.md)：IR dump、interpreter、GSan/ConSan/FPSan、最小复现
- [Ch 32 测试与上游贡献](./32-testing-and-upstream-contribution.md)：lit/pytest、FileCheck、CI、review、bisect、提交策略
- [Ch 33 性能工程与架构维护](./33-performance-engineering-and-maintenance.md)：profiling、roofline、occupancy、扩展点、技术债
- [练习 E：定位一个真实问题](./practice-e-debug-and-performance.md)：从错误 IR 或性能回归出发，做分层定位与修复
- [Final Project：全栈 Triton 扩展](./final-project-full-stack-triton-extension.md)：新增一个可验证的 Triton op/pass/lowering，带测试、benchmark 和上游风格说明

## 环境与证据

本课的主要环境：

- Triton 源码：`/home/ubuntu/aicomp/triton`
- Triton 版本：`3.9.0`
- commit：`a972119e92a27b445cd160017ea461e56148590c`
- LLVM pin：`18f9e623e4c891ba31ff817c07197707ac9bc593`
- GPU：NVIDIA Tesla T4（`sm_75`，16 GB）
- 驱动：`535.216.01`，CUDA runtime 12.4（`/usr/local/cuda`）
- Python：3.10.12

> **T4 的边界**：本课可以在 T4 上实测 TTIR/TTGIR/LLIR/PTX/cubin/SASS 和 FMA 路径。注意 Triton 3.9 对 sm_75 的 `tt.dot` 直接回退 FMA（`AccelerateMatmul.cpp:516-521`，lit 用例 `dot_fall_back_fma_before_ampere`），**不会生成 MMAv2**；MMAv2/v3/v5、Hopper（SM90）的 WGMMA、Blackwell（SM100）的 TMEM/tcgen05、AMD 的 MFMA/WMMA 都需要对应硬件或 ROCm 环境，相关章节会明确标 `guided`。`guided` 不等于“随便写”：源码路径、IR 结构和 pass 顺序仍然按本机源码核对。

构建与最小闭环见 [Ch 0](./00-environment-and-source-map.md)。推荐把实验产物放在 `compilers/triton_source_deep_dive/code/.cache`，不要污染 Triton 源码树。

## 学习方式建议

1. **读一章，跑一条命令，改一个东西**：每章至少有一个“把它弄坏”的实验；只读不跑，layout 和 pipeline 很快就会变成名词记忆。
2. **先看 IR，再看 C++**：遇到一个 pass，先 dump 前后的 TTGIR，确认它到底改了什么，再读实现。不要从 1000 行的 pass 文件第一行开始。
3. **用最小 reproducer**：任何问题都缩到一颗 kernel、一个 shape、一个 `num_warps`/`num_stages`。Triton 的 bug 往往死在“多一个 layout conversion”或“少一个 barrier”。
4. **区分四层证据**：源码、IR dump、运行结果、性能数字。四者不一致时，先怀疑环境和版本，再怀疑自己的假设。
5. **把 final 当成上游 PR 来写**：有设计说明、有最小测试、有失败路径、有性能条件、有回滚方案。

## 源码地图（先记住这几个入口）

| 层 | 入口 | 你会在这里看到 |
|---|---|---|
| Python JIT | `python/triton/runtime/jit.py` | `JITFunction`、binder、specialization、cache key |
| 编译编排 | `python/triton/compiler/compiler.py` | `ASTSource`、`IRSource`、`compile`、`CompiledKernel` |
| AST → TTIR | `python/triton/compiler/code_generator.py` | `CodeGenerator`、`visit_*`、SSA、控制流 |
| 语义 | `python/triton/language/semantic.py` | 类型提升、广播、load/store/dot/reduce |
| 语言值模型 | `python/triton/language/core.py` | `tensor`、`dtype`、`constexpr`、descriptor |
| TTIR | `include/triton/Dialect/Triton/IR/`、`lib/Dialect/Triton/` | `tt.*` op/type/attribute、TTIR pass |
| TTGIR | `include/triton/Dialect/TritonGPU/IR/`、`lib/Dialect/TritonGPU/` | layout encoding、`ttg.*` op、TTGIR pass |
| NVIDIA 专用 IR | `include/triton/Dialect/TritonNvidiaGPU/`、`lib/Dialect/TritonNvidiaGPU/` | TMA、TMEM、tcgen05、barrier |
| 后端 pipeline | `third_party/nvidia/backend/compiler.py` | `make_ttir/make_ttgir/make_llir/make_ptx/make_cubin` |
| GPU → LLVM | `lib/Conversion/TritonGPUToLLVM/`、`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/` | TypeConverter、各 op 的 lowering pattern |
| LLVM/PTX | `third_party/nvidia/backend/compiler.py`、`third_party/nvidia/backend/driver.py` | `llvm.to_module`、NVPTX、ptxas、launcher |
| AMD | `third_party/amd/` | HIP/ROCm、MFMA/WMMA、AMD pipeline |
| Gluon | `python/triton/experimental/gluon/`、`include/triton/Dialect/Gluon/` | 显式 layout 前端与 dialect |
| 测试 | `test/`、`python/test/`、`unittest/` | lit、pytest、interpreter、C++ 单测 |

## 精选资料库

### 必读论文与官方文档

- **Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations** — Philippe Tillet, H. T. Kung, David Cox，MAPL 2019（[ACM DOI](https://dl.acm.org/doi/10.1145/3315508.3329973)）
  - **核心贡献**：把“tiled neural network computation”抽象成块级程序，并用编译器自动完成 layout、并行和内存层次映射；这是 Triton 设计思想的原始论文。
  - **读哪里**：第 2–4 节看块级编程模型和 IR；第 5 节看编译流程。和本课 Ch 1、Ch 12、Ch 16 最直接相关。
  - **前提**：懂 GPU 的 block/warp/shared memory，以及基本编译器 IR。
- **[MLIR: A Compiler Infrastructure for the End of Moore's Law](https://arxiv.org/abs/2002.11054)** — Chris Lattner et al., 2020
  - **核心贡献**：多级 IR、dialect、progressive lowering 的系统化描述；Triton 的 TTIR/TTGIR/LLIR 就是这套思想的实例。
  - **读哪里**：第 2–3 节（IR 对象模型、dialect）和第 4 节（pass/转换）。如果只读一篇 MLIR 论文，就是这篇。
- **[LLVM Language Reference Manual](https://llvm.org/docs/LangRef.html)**
  - **读哪里**：`LLVM IR` 的 module/function/basic block、`getelementptr`、`addrspace`、`call`、`metadata`。Ch 28 会反复回来查。
- **[PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)**
  - **读哪里**：`ld/st`、`mma`、`wgmma`、`tcgen05`、`mbarrier`、`cp.async` 的指令语义。Ch 25、Ch 27、Ch 28 会用到。
- **[CUDA C++ Programming Guide](https://docs.nvidia.com/cuda/cuda-c-programming-guide/)**
  - **读哪里**：shared memory、coalescing、TMA（Tensor Memory Accelerator）、warp-level primitives。Ch 12、Ch 15、Ch 20 的背景。
- **[CUTLASS Documentation](https://docs.nvidia.com/cutlass/)**
  - **读哪里**：GEMM 的 tile/iterator/layout、MMA 的 operand layout、Hopper/Blackwell 的 WGMMA/tcgen05。Ch 25 的对照实现。

### Triton 官方与社区资料

- **[Triton GitHub](https://github.com/triton-lang/triton)**
  - **读哪里**：`docs/`、`python/tutorials/`、`python/test/unit/`。本课所有源码引用都以该仓库的 3.9.0 commit 为准。
- **[Triton 官方文档](https://triton-lang.org/main/index.html)**
  - **读哪里**：Programming Guide、Python API、Gluon 文档。遇到 API 语义不确定时，以源码和本机输出为准。
- **Triton meetup notes**（源码树 `docs/meetups/`）
  - **读哪里**：2023–2026 的 dev meetup 笔记，尤其是 layout、pipelining、warp specialization、Blackwell 相关场次。它们是“设计者自己怎么解释设计”的一手材料。
- **[PyTorch Inductor 文档](https://docs.pytorch.org/docs/stable/torch.compiler.html)**
  - **读哪里**：`torch.compile`、Inductor、Triton codegen 相关章节。Ch 1 会用它说明 Triton 在真实 AI 编译器栈中的位置。

### 读完之后

- **《Engineering a Compiler》** — Keith Cooper, Linda Torczon（第 3 版，2022）
  - 补传统编译器的 IR、数据流、循环优化、指令选择；本课默认你已经有这部分基础。
- **[LLVM 源码](https://github.com/llvm/llvm-project)** 与 [`mlir_core`](../mlir_core/README.md)
  - 想把 MLIR/LLVM 这一侧推得更深，继续读 MLIR 的 dialect conversion、LLVM NVPTX backend、SelectionDAG/GlobalISel。
- **[Triton 的 issue/PR 历史](https://github.com/triton-lang/triton/pulls)**
  - 最好的维护训练：挑一个 layout/pipeline 相关的 PR，读 diff、测试和 review 讨论，再自己复现。

## 课程完成标准

学完本课，你应该能独立完成下面这些事：

- 给一颗新 kernel，手工画出它的 TTIR → TTGIR → LLIR → PTX 关键变化；
- 看到一个 TTGIR layout，能说出每个线程大概拿哪些元素、为什么需要/不需要 `convert_layout`；
- 能定位一个 pass 在 pipeline 中的位置，解释“为什么不能提前/推后”；
- 能写一个带 lit + pytest 的 TTGIR/TTIR 变换，并证明它没有破坏语义；
- 能从一个错误的 PTX/SASS 或数值结果，反推到 layout、barrier、lowering 或 LLVM 层；
- 能按上游风格提交一个小的 Triton 扩展，并说清楚兼容性、性能和测试边界。

→ 从 [Ch 0 环境、源码地图与可复现实验](./00-environment-and-source-map.md) 开始。
