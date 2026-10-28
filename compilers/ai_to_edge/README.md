# 从大模型到边缘设备：AI 编译器 × LLVM 后端全链路

> 给会写 Python、用过 PyTorch、想搞清楚「一个模型到底怎么变成边缘设备上跑得动的机器码」的工程师。

这门课沿一条真实链路走到底：**PyTorch 模型 → 图捕获 → 图级优化 → 调度 → 代码生成 → MLIR/LLVM IR → 目标后端（AArch64 / Cortex-M / RISC-V）→ 链接 → 在设备上跑出数字**。不是背 API，而是每一层都能回答：这里改了什么、为什么这样改、改坏了会怎样。

全课 **30 章 + 4 个练习 + 1 个 Final Project**，主线跑在真实工具链上：LLVM 14、PyTorch 2.9.1+cpu、MLIR 14、`clang-14`、`llc-14`、`aarch64-linux-gnu-gcc`、`arm-none-eabi-gcc`、`qemu-aarch64-static`、`qemu-system-arm`。

> 证据约定：`实测` = 本机真实输出，可复现；`源码依据` = 对照具体版本源码或官方文档；`guided` = 本机缺硬件或工具链，只给步骤与预期现象，不伪造输出。

## 为什么学这个？

- **AI 编译器的工作有一半在后端**：算子融合只是开始。真正决定能不能上设备的是寄存器、栈帧、ABI、内存布局这些后端问题。
- **边缘部署失败几乎都不是「模型不对」**：是 shape 静态化不了、是 `memref` 降级后调用约定变了、是裸机上根本没有 `malloc`。这些只有走通全链路才看得见。
- **工程与面试都需要这条链**：能说清「`torch.compile` 之后到 ARM 上那条指令是谁生成的」，比会背十个框架名字有用得多。

## 先修知识

- Python 与 PyTorch 基础（能写 `nn.Module`、跑过 `torch.compile`）
- C 语言与基本内存模型（指针、栈、结构体布局）
- 一点汇编阅读能力（看得懂 `ldr` / `str` / `add` 即可，不需要会写）
- 没有也没关系：LLVM 内部、MLIR、链接器细节，课程从零建立

## 课程地图

### Part 0 — 起点与全景（Ch 0–2）
- [Ch 0 环境搭建：一条能真跑的工具链](./00-environment-setup.md)
- [Ch 1 全景：一个模型到一条指令的七次变身](./01-pipeline-overview.md)
- [Ch 2 边缘设备的真实约束](./02-edge-constraints.md)

### Part 1 — 模型侧：编译器到底看到了什么（Ch 3–5）
- [Ch 3 三种「模型」：nn.Module / FX Graph / ExportedProgram](./03-three-representations.md)
- [Ch 4 图捕获：Dynamo 的符号追踪与 guard](./04-graph-capture.md)
- [Ch 5 动态 shape 与控制流](./05-dynamic-shape.md)
- [练习 A：把一张图抓下来并读懂它](./practice-a-graph-capture.md)

### Part 2 — AI 编译器内部（Ch 6–11）
- [Ch 6 TorchInductor 全链路实测](./06-inductor-pipeline.md)
- [Ch 7 图级优化：融合、常量折叠与 layout](./07-graph-optimizations.md)
- [Ch 8 调度：为什么融合之后还要 tiling](./08-scheduling.md)
- [Ch 9 代码生成的三个出口](./09-codegen-backends.md)
- [Ch 10 MLIR：把「算子」变成「循环」](./10-mlir-lowering.md)
- [Ch 11 量化在编译器中的位置](./11-quantization.md)
- [练习 B：手写一个融合 pass](./practice-b-fusion-pass.md)

### Part 3 — 交给 LLVM（Ch 12–17）
- [Ch 12 从 MLIR 到 LLVM IR：方言降级的真实样子](./12-mlir-to-llvm-ir.md)
- [Ch 13 LLVM 优化管线：pass 顺序为什么重要](./13-llvm-pipeline.md)
- [Ch 14 Triple、DataLayout 与 TargetMachine](./14-target-description.md)
- [Ch 15 合法化与指令选择](./15-legalization-isel.md)
- [Ch 16 寄存器分配与栈帧](./16-regalloc-stackframe.md)
- [Ch 17 汇编、目标文件与重定位](./17-assembly-object-relocation.md)
- [练习 C：读一条指令的完整来历](./practice-c-instruction-origin.md)

### Part 4 — 边缘后端（Ch 18–24）
- [Ch 18 AArch64 后端实战](./18-aarch64-backend.md)
- [Ch 19 Cortex-M 裸机后端：没有 OS 的世界](./19-cortex-m-baremetal.md)
- [Ch 20 RISC-V 边缘后端](./20-riscv-backend.md)
- [Ch 21 ABI 与调用约定：memref 为什么会变成七个参数](./21-abi-calling-convention.md)
- [Ch 22 内存规划：边缘设备没有 malloc 的自由](./22-memory-planning.md)
- [Ch 23 算子库还是代码生成](./23-libraries-vs-codegen.md)
- [Ch 24 最小运行时](./24-minimal-runtime.md)
- [练习 D：给一个算子换后端](./practice-d-new-backend.md)

### Part 5 — 端到端与工程化（Ch 25–29）
- [Ch 25 数值正确性：逐层对齐](./25-numerical-correctness.md)
- [Ch 26 性能测量：在设备上量什么、怎么量](./26-performance-measurement.md)
- [Ch 27 调试：从错误现象反推出错层](./27-debugging.md)
- [Ch 28 生产工具链对照：ExecuTorch / TVM / IREE / ORT / TFLite](./28-production-stacks.md)
- [Ch 29 取舍与决策表](./29-tradeoffs.md)
- [Final Project：把一个真实模型编译到 AArch64 与 Cortex-M 并跑通](./final-project-end-to-end.md)

## 代码目录

所有可复现实验都在 [`code/`](./code/)，每个脚本头部写明验证环境与运行命令。课程里的输出都来自这些脚本的真实运行。

## 学习方式建议

1. **先跑通再理解**：Ch 0 的工具链先装好，后面每章都有可复现命令；跑出数字比读完更重要。
2. **故意把它弄坏**：改错 target triple、去掉 `-static`、把 memref 当结构体传——本课的踩坑几乎都来自真实失误。
3. **读中间产物**：不要只看最终结果。每个阶段都 `cat`、`objdump`、`grep` 一次，中间产物才是这门课的主体。

## 精选资料库

### 必读基础

- **[LLVM Language Reference Manual](https://llvm.org/docs/LangRef.html)**
  - 权威 IR 语义；遇到「这条指令到底保证什么」时回来查，不要靠记忆
- **[MLIR Dialect Documentation](https://mlir.llvm.org/docs/Dialects/)**
  - `linalg` / `memref` / `scf` / `llvm` 各自的能力边界

### 推荐论文

- **[TVM: An Automated End-to-End Optimizing Compiler for Deep Learning](https://www.usenix.org/conference/osdi18/presentation/chen)** — Chen et al., OSDI 2018
  - 计算图与调度分离的原始论述，第 4 节直接对应本课 Ch 8
- **[Glow: Graph Lowering Compiler Techniques for Neural Networks](https://arxiv.org/abs/1805.00907)** — Rotem et al., 2018
  - 图级 lowering 与内存规划，对应 Ch 7 与 Ch 22
- **[TASO: Optimizing Deep Learning Computation with Automatic Generation of Graph Substitutions](https://cs.stanford.edu/~matei/papers/2019/sosp_taso.pdf)** — Jia et al., SOSP 2019
  - 图替换为什么需要搜索，对应 Ch 7

### 推荐博客 / 文章

- **[torch/_inductor 源码](https://github.com/pytorch/pytorch/tree/main/torch/_inductor)**
  - 本课 Ch 6 的第一手依据；配合 `TORCH_LOGS=graph_breaks,inductor` 读
- **[Eli Bendersky — LLVM 系列](https://eli.thegreenplace.net/tag/llvm)**
  - 后端概念讲得最清楚的一批文章，适合 Ch 14–17 之前热身
- **[Krister Walfridsson — 编译器优化博客](https://kristerw.github.io/)**
  - 指令选择与合法化的实战细节

### 读完本课之后

- **《Engineering a Compiler》** — Cooper & Torczon
  - 把 Ch 13–17 的理论补齐；第 8–10 章对应本课后端部分
- **[IREE 文档](https://iree.dev/)**（把 MLIR 全链路做到生产级，本课之后最自然的方向）
