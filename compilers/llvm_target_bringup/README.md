# LLVM Target Bring-up 实战：以 RISC-V 为参照，从零接入一个后端

> 给已经会读 LLVM IR、跑过 `llc`、但一想到“给 LLVM 加一个新架构”就不知道从哪下手的人。

这门课讲的不是“怎么用 `clang --target=riscv64`”，而是**一个新 target 如何一步步进入 LLVM**：先让 `llvm-mc` 认识它，再让 `llc` 为它生成汇编，再让它产出可链接的 ELF，最后让 Clang 和 LLD 也认识它。全程以 RISC-V 为参照实现，以一个真实的、从零长出来的 `MiniRV` target 为实验对象——它有 42 个 bring-up commit、53 个 lit 测试、17 个 qemu 端到端程序，不是玩具伪代码。

学完你应当能回答四个问题：

1. 加一个新 target，到底要改哪些文件？哪些是必须的，哪些可以后补？
2. 每个里程碑的**验收标准**是什么？什么时候可以进入下一层？
3. 为什么 LLVM 的 target 接口长成这样？RISC-V 为什么这样实现？
4. 一个 target 从“能出汇编”到“能跑真实程序”，中间还差哪几层？

## 为什么学这个？

- **这是理解 LLVM 架构最快的方式**。平时读 pass、读 IR，看到的都是“框架已经替你接好了”。只有自己接一个 target，才会被迫回答：Triple 是谁解析的？`TargetRegistry` 里注册了什么？`llc` 怎么找到你的 `TargetMachine`？MC 层和 CodeGen 层的边界在哪？
- **面试里很值钱**。编译器岗位常问“如果让你支持一个新 ISA，你会怎么做”“为什么 RISC-V 后端有 `RISCVTargetLowering` 和 `RISCVISelDAGToDAG` 两个类”“MC-only target 是什么意思”。能按层回答的人很少。
- **RISC-V 是最合适的参照**。它是 load/store 架构、指令格式规整、ABI 文档完整、上游实现活跃；同时有 RV32/RV64、M/A/F/D/C/V 扩展，足够展示“feature 如何影响 codegen”。比 x86 的复杂寻址和 ARM 的历史包袱更适合教学。
- **“接入”不等于“写完整个后端”**。真实的 bring-up 是分阶段的：先 MC-only，再直线代码，再控制流和调用，再 ABI，再链接和运行，最后才是工具链和上游化。本课把这条路径拆开，每一层都有可验证的产物。
- **和仓库现有课程互补**：[`riscv_backend`](../riscv_backend/README.md) 教你读完整 RISC-V 后端；[`loongarch_backend`](../loongarch_backend/README.md) 教你维护一个成熟后端；[`tablegen_internals`](../tablegen_internals/README.md) 讲 TableGen 内部；[`llvm_lit`](../llvm_lit/README.md) 讲测试框架。本课只做一件事：**从零把一个 target 接进来，并按里程碑验收**。

## 先修知识

- **LLVM IR 与 `llc`**（程度：能读 `.ll`，知道 `-mtriple`、`-mattr`、`-filetype=obj`、`-stop-after`）
- **Machine IR / MIR 基础**（程度：知道虚拟寄存器、`COPY`、`PSEUDO`、`-run-pass`；不要求会写复杂 MIR）
- **C++17 阅读能力**（程度：能读继承、模板、`switch` 分派、`TargetLowering` 调用链；不要求写过 LLVM pass）
- **RISC-V 基础**（程度：知道 `addi` 的 12-bit immediate、`lui`、`auipc`、load/store、`jalr`、`x0`；不要求写过裸机程序）
- **命令行**（程度：能跑 `ninja`、`llvm-lit`、`llvm-readobj`、`git log`/`git diff`）
- 没有也没关系：GlobalISel、TableGen 内部实现、Clang driver 细节、LLD 源码；这些会在需要时补，或指向现有课程

## 课程地图：5 个 Part，29 章 + 4 个练习 + 1 个 Final

### Part 0 — 地图与基线（Ch 00–03）

- [Ch 00 课程地图与环境基线](./00-course-map-and-environment.md)：六个里程碑、MiniRV/RISC-V 双基线、工具链指纹、如何复现
- [Ch 01 什么算“接入一个 target”](./01-what-is-target-bringup.md)：Triple → TargetRegistry → MC → CodeGen → Clang → LLD 的七层边界
- [Ch 02 RISC-V 作为参照](./02-riscv-as-reference.md)：RV64IMA、lp64 ABI、寄存器与指令格式，以及 RISC-V 后端的文件地图
- [Ch 03 里程碑 0：MC-only target](./03-mc-only-milestone.md)：最小可验收的汇编器/反汇编器/ELF 输出，解剖 MiniRV 的第一个 commit

### Part 1 — 让 LLVM 认识你（Ch 04–08 + 练习 A）

- [Ch 04 Triple 与 DataLayout](./04-triple-and-datalayout.md)：一个 arch 名字要穿过 `Triple.h`/`Triple.cpp` 的哪些 switch
- [Ch 05 构建系统与 TargetRegistry](./05-build-and-target-registry.md)：`LLVM_ALL_TARGETS`、CMake 组件库、`TargetInfo`、初始化链与 `Targets.def`
- [Ch 06 最小 TableGen](./06-tablegen-minimum.md)：寄存器、指令、格式、AsmWriter、AsmMatcher、Disassembler 各自生成什么
- [Ch 07 MC 层五件套](./07-mc-layer-five-pieces.md)：`MCAsmInfo`/`MCInstrInfo`/`MCRegisterInfo`/`MCSubtargetInfo`/`MCInstPrinter` 的注册与生命周期
- [Ch 08 汇编器、打印器与编码器](./08-assembler-printer-encoder.md)：`llvm-mc` 往返、fixup、ELF object writer、复用 `EM_RISCV` 的代价
- [练习 A：给 MiniRV 加一条 MC 指令](./practice-a-mc-instruction.md)：编码、解析、打印、反汇编、lit 全链路

### Part 2 — 从 IR 到汇编（Ch 09–15 + 练习 B）

- [Ch 09 TargetMachine、Subtarget 与 TargetLowering](./09-targetmachine-subtarget-lowering.md)：CodeGen 的入口、pass 配置与 `CodeGenPassBuilder`
- [Ch 10 寄存器类与调用约定](./10-registers-and-calling-convention.md)：GPR/FPR、CalleeSaved、`CallingConv.td`、RISC-V ABI 对照
- [Ch 11 FrameLowering](./11-frame-lowering.md)：prologue/epilogue、栈对齐、frame pointer、2047 边界与大栈帧拆分
- [Ch 12 Legalization](./12-legalization.md)：类型/操作/寻址模式的 Legal/Custom/Expand/LibCall，RV64 上的 i8/i16/i32/i128
- [Ch 13 SelectionDAG 选指](./13-selectiondag-patterns.md)：`Pat`、`ComplexPattern`、`ImmLeaf`，从 DAG 到机器指令
- [Ch 14 内存与全局地址](./14-memory-and-global-addresses.md)：load/store、寻址模式、`GlobalAddress`、`auipc+addi`、GOT/常量池
- [Ch 15 控制流与尾调用](./15-control-flow-and-tail-calls.md)：`br`/`brcond`/`switch`/jump table、tail call、return address
- [练习 B：加一条带 DAG pattern 的指令](./practice-b-dag-pattern-instruction.md)：从 `.td` 到 `llc` 汇编、MIR 与 CodeGen 测试

### Part 3 — 让真实程序跑起来（Ch 16–22 + 练习 C）

- [Ch 16 调用序列](./16-call-sequence.md)：direct/indirect call、`jalr`、`PseudoCALL`、musttail、call frame pseudos
- [Ch 17 参数与返回值](./17-arguments-and-returns.md)：整数/浮点/varargs/sret/byval、i128 寄存器对、栈传参
- [Ch 18 栈帧进阶](./18-stack-frames-advanced.md)：dynamic alloca、stack protector、frameaddress/returnaddress、stackmap/patchpoint 拒绝
- [Ch 19 常量与位运算](./19-constants-and-bit-intrinsics.md)：`lui/addi`、64-bit 常量、移位、bit intrinsics、i128
- [Ch 20 软浮点与 libcall](./20-soft-float-and-libcalls.md)：soft-float、libm intrinsics、compiler-rt helpers、`__muldi3`
- [Ch 21 异常、CFI 与调试信息](./21-exceptions-cfi-debug.md)：invoke、DwarfCFI、`.debug_frame`、`dbg.declare`、unwind tables
- [Ch 22 对象文件与链接](./22-object-file-and-linking.md)：ELF writer、重定位、`ld.lld`、relaxation、端到端差分
- [练习 C：端到端跑通一个程序](./practice-c-end-to-end.md)：同一段 IR 双编译、链接、qemu 运行、比较退出码与指令数

### Part 4 — 工程化与上游（Ch 23–28 + 练习 D + Final）

- [Ch 23 诊断与显式拒绝](./23-diagnostics-and-rejection.md)：TLS、scalable vector、patchpoint 的“先拒绝再实现”策略
- [Ch 24 测试一个新 target](./24-testing-a-new-target.md)：lit/FileCheck、CodeGen 回归、MC 测试、`update_llc_test_checks`
- [Ch 25 端到端与差分测试](./25-end-to-end-differential-testing.md)：qemu、`start.s`、17 个程序、静态指令数基线
- [Ch 26 Clang 前端接入](./26-clang-frontend-integration.md)：`TargetInfo`、driver、ABI、builtins（MiniRV 尚未接，标 guided）
- [Ch 27 上游化与评审](./27-upstreaming-and-review.md)：补丁拆分、review checklist、常见 reviewer 意见、RISC-V 上游历史
- [Ch 28 复盘：42 个 commit 的 bring-up 时间线](./28-bringup-retrospective.md)：哪一步最贵、哪一步最容易返工、下一个 target 怎么排
- [练习 D：定位一个 CodeGen 回归](./practice-d-codegen-regression.md)：`-debug-only`、`-stop-after`、MIR、lit 的联合诊断
- [Final Project：从零接入 `MiniRV+`](./final-project-new-target.md)：新增指令/ABI/测试/端到端/补丁系列

## 学习方式建议

1. **按里程碑验收，不要按章节验收**。每读完一个 Part，用该 Part 的“验收单”跑一遍：`llvm-mc` 能往返吗？`llc` 能出汇编吗？对象能链接吗？程序能跑吗？跑不通就先别往下读。
2. **RISC-V 是参照，不是答案**。每章都问：RISC-V 在同一个接口上是怎么做的？MiniRV 为了“最小可运行”省略了什么？如果换成你的 target，你会保留哪一边？
3. **先看 MIR，再看汇编**。`-stop-after=finalize-isel`、`-debug-only=isel`、`-run-pass` 是分界线；只看最终汇编，你会把 frame lowering、regalloc、pseudo expansion 的功劳算到 instruction selection 头上。
4. **每章至少跑一次“反例”**。比如去掉一个 `setOperationAction`、把 `isInt<12>` 改成 `isInt<13>`、把 `CalleeSavedRegs` 改错，看 LLVM 在哪一层报错。错误信息是接口契约最好的文档。
5. **把“拒绝”当成功能**。TLS、scalable vector、stackmap 这些暂时不支持的路径，显式报错比静默生成错误代码好。第 23 章会专门讲这个策略。

## 证据约定

- 环境：LLVM **24.0.0git**，源码树 `/home/ubuntu/llvm/trees/dsl-tablegen`，commit `6e7e7abeb2b7`（2026-11-14），构建 `/home/ubuntu/llvm/builds/dsl-tablegen`（`X86;AArch64;RISCV;RVTrim;MiniRV`，clang，assertions）。
- 参照工具链：`ld.lld` 来自 `/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld`；`qemu-riscv64-static` 来自 `/usr/bin/qemu-riscv64-static`。
- `实测` = 本机真实运行得到的输出；`源码依据` = 对照上述 commit 的文件与符号；`guided` = 需要改源码并重新构建，课程给出命令、差异思路与预期现象，不伪造构建日志。
- 行号会漂移。本课优先给“文件 + 符号名”，只有写作时用 `grep -n` 现场核对过的行号才写出来。

## 和已有课程的关系

| 已有课程 | 本课怎么用它 | 本课新增什么 |
|---|---|---|
| [`riscv_backend`](../riscv_backend/README.md) | 需要 RISC-V 的 ISA/ABI/MC/ISel 细节时回查 | 把“读后端”变成“接后端”：先 MC-only，再 CodeGen，再工具链 |
| [`loongarch_backend`](../loongarch_backend/README.md) | 看一个成熟后端如何组织 ABI、TLS、LLD 和上游流程 | 用 42 个 commit 的真实顺序展示“从零到能跑”的分层 |
| [`tablegen_internals`](../tablegen_internals/README.md) | 需要 `-gen-*` 生成物内部结构时回查 | 只讲 bring-up 必需的最小 TableGen，以及生成物与手写代码的边界 |
| [`llvm_lit`](../llvm_lit/README.md) | 需要 lit/FileCheck 语法细节时回查 | 讲“一个新 target 应该有哪些测试”，以及端到端差分测试 |
| [`llvm_isel_sched_riscv`](../llvm_isel_sched_riscv/README.md) | 需要选指与调度的交互细节时回查 | 本课只讲“选指能不能跑通”，不展开调度模型质量 |

## 本课不涵盖什么

- 不教你写一个生产级完整后端。MiniRV 是 RV64IMA 子集，没有浮点寄存器、没有 RVV、没有 TLS、没有完整的 ABI 兼容层。
- 不展开 TableGen 的 lexer/parser/记录模型；这些在 [`tablegen_internals`](../tablegen_internals/README.md)。
- 不展开 Clang 前端的完整实现。第 26 章只给接入点和 guided 步骤。
- 不覆盖 GPU/加速器后端、JIT、完整 LLD 源码、compiler-rt 的全部运行时。
- 不承诺“学完就能让任何 ISA 一次通过所有测试”；目标是让你能按层定位、验证、逐步补齐。

## 精选资料

### 必读基础

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — 官方后端总览；先读 “The LLVM Target-Independent Code Generator” 和 “Target Machine” 两节，建立本课 Ch 01 的七层地图。
- **[LLVM TableGen](https://llvm.org/docs/TableGen/)** — `-gen-instr-info`、`-gen-asm-matcher`、`-gen-dag-isel` 的官方说明；配合 Ch 06/13 读。
- **[LLVM MC Layer](https://llvm.org/docs/CommandGuide/llvm-mc.html)** — `llvm-mc` 的输入/输出与 `-show-encoding`/`-disassemble` 语义；Ch 08 的实验都基于它。
- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — 调用约定、寄存器角色、栈对齐、参数传递的权威来源；Ch 10/17 对照用。
- **[RISC-V ISA Manual](https://riscv.org/technical/specifications/)** — 指令编码、immediate 范围、`auipc`/`jalr` 语义；Ch 02/08/14 对照用。

### 推荐阅读

- **[How to add a new target to LLVM](https://llvm.org/docs/WritingAnLLVMBackend.html)** — 官方 “Writing an LLVM Backend” 教程；它偏旧、偏概念，但能补齐本课没展开的 `TargetMachine` 细节。
- **[LLVM 源码：`llvm/lib/Target/RISCV`](https://github.com/llvm/llvm-project/tree/main/llvm/lib/Target/RISCV)** — 本课最主要的参照实现；重点看 `RISCVTargetMachine.cpp`、`RISCVSubtarget.cpp`、`RISCVISelLowering.cpp`、`RISCVFrameLowering.cpp`。
- **[LLVM 源码：`llvm/lib/Target/MiniRV`](https://github.com/llvm/llvm-project)** — 本课实验对象；在本机树里是 `llvm/lib/Target/MiniRV`，不在上游。
- **[Alex Bradbury 的 RISC-V LLVM 工作](https://www.lowrisc.org/)** — RISC-V 后端早期上游化的背景；Ch 27 会提到这段历史。

### 读完之后

- **[`riscv_backend`](../riscv_backend/README.md)** — 把本课的“能跑”推进到“每个 pass 都能解释”。
- **[`llvm_isel_sched_riscv`](../llvm_isel_sched_riscv/README.md)** — 把“选对了”推进到“选得好、排得好”。
- **[`llvm_lit`](../llvm_lit/README.md)** — 把本课的测试基线扩展成完整的 lit 框架能力。
