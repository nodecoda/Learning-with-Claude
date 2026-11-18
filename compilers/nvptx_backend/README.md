# NVPTX 后端深潜：从 PTX 契约到 LLVM 的 GPU codegen

> 给已经走通 LLVM「IR → MIR → MC → .s/.o」全管线、想搞懂 GPU 目标怎么建模的编译器工程师。

这门课只做一件事：把 `llvm/lib/Target/NVPTX` 拆开。你会看到 LLVM 怎么把 IR 变成 PTX 文字、哪些机器层工作它刻意不做、以及为什么这些「不做」正好是自研 GPU 后端必须补上的空白。全课每条指令、每个输出，都在本机 `ct-instr-trunk` 构建 + CUDA 12.4 + Tesla T4 上真跑过。

## 为什么学这个？

- **它是「GPU 后端」的最小完整样本**：NVPTX 只有约 3.4 万行，却覆盖 TargetMachine、ISel、frame lowering、AsmPrinter、ABI、地址空间、intrinsic 全套。读完它，你对任何 GPU target 都不会陌生。
- **它会打破你从 CPU 后端带来的直觉**：这里没有寄存器分配、没有调度模型、没有 object emission。知道「为什么可以没有」，比知道「怎么写一个」更值钱。
- **它直接对应 CUDA 兼容编译器的现实路线**：自研 GPU 吃 PTX、或接 NVVM IR，都必须先精确理解 NVPTX 的语义边界与 ptxas 的分工。

## 先修知识

- **LLVM 后端全管线**（程度：写过 pass、读过 SelectionDAG、知道 MachineInstr / MC / AsmPrinter 的分工）
- **TableGen**（程度：看得懂 `Instruction`、`Pattern`、`RegisterClass`，不必从零写）
- **CUDA 编程**（程度：写过 kernel、知道 grid/block/thread、shared memory、warp）
- **一点 GPU 微架构**（程度：知道 SM、warp、occupancy 是什么；不熟可先看 `parallel/gpu_cuda` Ch 7–11）
- 没有也没关系的：ptxas 内部实现、SASS 私有位域布局、Tensor Core layout

## 课程地图

### Part 0 — 定位与环境（Ch 0–1）
- [Ch 0 环境与全链路：llc → .ptx → ptxas → T4](./00-environment-and-pipeline.md)
- [Ch 1 NVPTX 的定位：PTX 发射器，不是机器码后端](./01-what-nvptx-is.md)

### Part 1 — PTX 契约（Ch 2–5）
- [Ch 2 PTX 契约（一）：虚拟 ISA、类型与状态空间](./02-ptx-contract-isa.md)
- [Ch 3 PTX 契约（二）：版本、目标与特性门控](./03-ptx-version-and-target.md)
- [Ch 4 内核 ABI 与调用约定](./04-kernel-abi.md)
- [Ch 5 指针与地址空间：generic / global / shared / local](./05-address-spaces.md)
- [练习 A：用手写 IR 逼出 ABI 与地址空间](./practice-a-handwritten-ir.md)

### Part 2 — 从 CUDA 到机器指令（Ch 6–10）
- [Ch 6 CUDA → NVVM IR：clang 的 device 前端](./06-cuda-to-nvvm-ir.md)
- [Ch 7 IR 预处理 pass 群：真正的 GPU 语义修正](./07-ir-preprocessing-passes.md)
- [Ch 8 函数 lowering：参数、返回与调用](./08-function-lowering.md)
- [Ch 9 类型合法化：i1/i8/i16 与 PTX 的类型规则](./09-type-legalization.md)
- [Ch 10 指令选择：DAG 到 PTX 指令](./10-instruction-selection.md)
- [练习 B：三路对拍（手写 IR / clang / nvcc）](./practice-b-three-way-diff.md)

### Part 3 — 机器层的空白（Ch 11–14）
- [Ch 11 寄存器类与「没有寄存器分配」](./11-registers-and-no-regalloc.md)
- [Ch 12 栈帧、alloca 与 local memory](./12-frames-and-local-memory.md)
- [Ch 13 调度：为什么 NVPTX 几乎不调度](./13-scheduling.md)
- [Ch 14 内存操作与向量化：float4 为什么变成两个 b64](./14-memory-and-vectorization.md)
- [练习 C：版本矩阵、ptxas 资源与 SASS 对照](./practice-c-target-matrix-and-ptxas.md)

### Part 4 — 发射、落地与边界（Ch 15–17）
- [Ch 15 AsmPrinter 与 MC：PTX 文字是怎么印出来的](./15-asmprinter-and-mc.md)
- [Ch 16 ptxas 接手：PTX → SASS 的真实分配与调度](./16-ptxas-and-sass.md)
- [Ch 17 扩展与边界：加一个 intrinsic，以及自研 GPU 还差什么](./17-extending-and-limits.md)
- [练习 D：给 NVPTX 加一个 intrinsic](./practice-d-add-intrinsic.md)

### Part 5 — 内存模型与机器码（Ch 18–19）
- [Ch 18 PTX 内存一致性模型：scope、ordering 与 fence](./18-ptx-memory-model.md)
- [Ch 19 SASS 编码与调度控制位](./19-sass-encoding-and-control-bits.md)
- [练习 E：内存序与编码对照](./practice-e-memory-and-sass.md)

### 收尾
- [Final Project：PTX → 自研 ISA 的最小闭环](./final-project-ptx-to-own-isa.md)

## 学习方式建议

1. **每章都自己跑一次**：`llc` 的输出行为比文档可靠。看到 `.ptx` 就先猜「为什么是这条指令」，再对照本章解释。
2. **把 `-O0` 和 `-O2` 并排看**：NVPTX 的很多秘密藏在两者的差异里（generic load 变成 `ld.global`、`mul+add` 变成 `mad.lo`）。
3. **读源码要带问题**：每章末尾的「源码落点」给的是文件 + 函数名，不是行号；行号会随 upstream 漂移。
4. **拿 T4 当 golden model**：任何你怀疑的语义，就编成 cubin 丢上 GPU 验；本课已把这条路铺好。

## 精选资料库

### 必读基础

- **[PTX ISA](https://docs.nvidia.com/cuda/parallel-thread-execution/)** — NVIDIA
  - 权威规格。本课 Ch 2–5 的所有语义都以它为最终仲裁；查指令语法先看「Instruction Set」章。
- **[LLVM NVPTX Usage](https://llvm.org/docs/NVPTXUsage.html)** — LLVM 官方
  - 说明 NVPTX target 的 triple、CPU、feature 用法，以及哪些 CUDA 特性不支援。
- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 官方
  - 后端各阶段的地图。读 NVPTX 前先复习 SelectionDAG 与 MachineInstr 两节。

### 推荐博客 / 文章

- **[NVIDIA CUDA Binary Utilities](https://docs.nvidia.com/cuda/cuda-binary-utilities/)** — NVIDIA
  - `cuobjdump` / `nvdisasm` 的官方说明；Ch 16 的 SASS 对照实验靠它。
- **[ZLUDA](https://github.com/vosen/ZLUDA)** — Andrzej Janik
  - 在非 NVIDIA GPU 上跑 CUDA 的开源实现。看它怎么处理 PTX / cubin 边界，是「CUDA 兼容」最现实的案例。
- **[SPIRV-LLVM-Translator](https://github.com/KhronosGroup/SPIRV-LLVM-Translator)** — Khronos
  - 看另一个「把虚拟 ISA / IR 接到自家后端」的工程组织方式。

### 读完本课之后

- **`llvm/test/CodeGen/NVPTX/`** — 最好的行为文件；每条 lit 测试都是一个可执行的语义断言。
- **[CUTLASS](https://github.com/NVIDIA/cutlass)**（把 PTX 层的 layout / MMA 推到极限，适合接 Ch 14、17）
- **[Triton](https://github.com/triton-lang/triton)**（看一个真实产品怎么在 LLVM 之上补 GPU 专属优化；搭配 `compilers/triton_source_deep_dive`）
