# GlobalISel InstructionSelect 学习笔记：从 matcher table 到可工作的指令选择

> 给已经读过 LLVM backend、知道 SelectionDAG 大致流程，但 GlobalISel 只停在“听过”的工程师。

这系列以 **InstructionSelect** 为主轴：先补齐 IRTranslator / Legalizer / RegBankSelect 的必要背景，再拆开 TableGen 生成的 matcher table，最后在 AArch64 上完成一条带 GlobalISel 支持的 custom instruction。全程以 LLVM 23.1.4 源码和 MIR 实验为准，RISC-V 作为对照。

> 证据约定：能在官方 LLVM 23.1.4 预编译包上复现的 MIR/汇编均标为实测；需要改源码重编译的 custom pseudo、matcher 生成物与 lit 执行标为 guided/源码依据，不伪造构建或测试输出。

## 为什么学这个？

- **真实工作需求**：AArch64 在 `-O0` 默认使用 GlobalISel（由 `-aarch64-enable-global-isel-at-O=0` 控制）；`-O1` 以上需显式 `-global-isel` 或调高该选项。越来越多 target 在补 GlobalISel 支持。会读 `selectImpl()`、会改 pattern、能判断“该补 pattern 还是写 C++ select”，是 backend 工程师的日常。
- **底层理解**：InstructionSelect 不是“查表替换 opcode”这么简单。它要同时处理类型、寄存器 bank、立即数谓词、内存/原子语义、use-def 折叠和 fallback。理解这些约束，才能解释“为什么这个 IR 没被选成那条指令”。
- **调试能力**：GlobalISel 的失败经常表现为“悄悄 fallback 到 SelectionDAG”。不会用 `-global-isel-abort=1`、`-debug-only=global-isel` 和 lit/MIR 测试，就只能看到最终汇编，看不到选择过程。
- **上游贡献与面试**：LLVM backend 的 review 很看重 pattern 的正确性、测试覆盖和 compile-time 影响。这门课会把这些标准拆成可操作的 checklist。
- **与现有课程互补**：[`compiler_backend`](../compiler_backend/README.md) 的 Ch 10 是 GlobalISel 概览；本课假设你已经知道“有这五个阶段”，直接进入 matcher、pattern、selector 和调试。

## 先修知识

- **C++17**（程度：能读模板、虚函数、`std::optional`，能改中小型 C++ 项目）
- **LLVM IR 与 backend 基础**（程度：知道 `llc`、`opt`、MIR、TableGen；读过 [`compiler_backend`](../compiler_backend/README.md) Ch 4–11 或等价内容）
- **SelectionDAG 基本概念**（程度：知道 pattern matching、legalize、DAG 是什么；不要求能手写复杂 pattern）
- **Linux/WSL2 命令列**（程度：能 configure/build 大型 CMake 项目、跑 `llvm-lit`）
- 没有也没关系：AArch64 汇编细节、GlobalISel combiner、LLVM 上游 review 经验；这些会在需要时补

## 课程地图

### Part 0 — 先把输入弄对（Ch 0–4）

- [Ch 0 环境搭建与最小实验](./00-environment-setup.md)
- [Ch 1 GlobalISel 全景与 ISel 的位置](./01-globalisel-overview.md)
- [Ch 2 InstructionSelect 的硬契约](./02-instructionselect-contract.md)
- [Ch 3 IRTranslator 与 gMIR 阅读](./03-irtranslator-gmir.md)
- [Ch 4 Legalizer 与 RegBankSelect 的最小必要](./04-legalizer-regbankselect.md)
- [练习 A：手工 MIR 走完 instruction-select](./practice-a-manual-mir-isel.md)

### Part 1 — Matcher 内核（Ch 5–10）

- [Ch 5 TableGen 如何生成 GlobalISel matcher](./05-tablegen-globalisel-import.md)
- [Ch 6 matcher table 解释器](./06-matcher-table-executor.md)
- [Ch 7 谓词系统 I：类型、bank、立即数](./07-predicates-types-banks-immediates.md)
- [Ch 8 谓词系统 II：feature、内存、原子、fold 安全性](./08-predicates-features-memory-fold.md)
- [Ch 9 嵌套匹配与折叠](./09-nested-match-fold.md)
- [Ch 10 失败路径与 fallback](./10-failure-and-fallback.md)
- [练习 B：改一个 pattern，预测 matcher 命中](./practice-b-pattern-matcher.md)

### Part 2 — 进阶原语与手写 selector（Ch 11–15）

- [Ch 11 ComplexPattern 迁移](./11-complex-pattern-migration.md)
- [Ch 12 Custom SDNode、pseudo 与 GINodeEquiv](./12-custom-node-pseudo.md)
- [Ch 13 C++ 手写 selector](./13-cpp-selector.md)
- [Ch 14 多输出、变长、内存/原子、隐式操作数与 flags](./14-multi-output-memory-atomic.md)
- [Ch 15 向量、intrinsic 与 RISC-V 边界](./15-vector-intrinsic-riscv.md)
- [练习 C：ComplexPattern 迁移或手写 select case](./practice-c-complex-pattern-or-cpp-select.md)

### Part 3 — 验证、调试与质量（Ch 16–19）

- [Ch 16 MIR/lit 测试方法](./16-mir-lit-testing.md)
- [Ch 17 调试工具链](./17-debugging-toolchain.md)
- [Ch 18 常见错误与诊断树](./18-common-bugs-diagnosis.md)
- [Ch 19 质量与上游标准](./19-quality-upstream.md)
- [练习 D：给一个选错的 case 做根因分析](./practice-d-debug-wrong-selection.md)

### Part 4 — AArch64 实战与收束（Ch 20–23）

- [Ch 20 AArch64 源码全走读](./20-aarch64-source-walkthrough.md)
- [Ch 21 端到端案例：从 spec 到 lit（guided walkthrough）](./21-end-to-end-custom-instruction.md)
- [Ch 22 RISC-V GlobalISel 现状与迁移策略](./22-riscv-globalisel-status.md)
- [Ch 23 设计空间与下一步](./23-design-space-next-steps.md)
- [Final Project：给 AArch64 加一条带 GlobalISel 支持的 custom instruction](./final-project-aarch64-custom-instruction.md)

## 学习方式建议

1. **先 build 再读**：Ch 0 的 build 不是形式。没有可跑的 `llc`、`llvm-tblgen` 和 `llvm-lit`，后面的 matcher table、skipped patterns、lit 测试都只能凭空想象。
2. **每章至少跑一次 MIR**：读源码前先用 `-stop-after` / `-run-pass` 把该章涉及的 pass 状态 dump 出来。先看数据，再看代码。
3. **读生成代码，不只读 `.td`**：pattern 写得对不对，最终要看 `*GenGlobalISel.inc` 里的 matcher table。每章会指出该看哪一段。
4. **故意把它弄坏**：改错 type、regbank、opcode、feature predicate，观察 matcher 拒绝、fallback、verifier 报错。错误输出是最好的教材。
5. **用 abort 暴露 fallback**：默认 fallback 会让“没选上”看起来像“成功”。调试 GlobalISel 时先加 `-global-isel-abort=1`，确认到底有没有掉回 SelectionDAG。
6. **final 不要跳过**：读一百遍 pattern 不如自己加一条 instruction。final 会要求 spec、TableGen、selector、lit 测试和 RISC-V 对照分析全部到位。

## 环境与版本

> **环境**：LLVM 23.1.4，WSL2 Ubuntu 24.04 x86_64，Ninja + ccache。主 target 为 AArch64，RISC-V 作对照。

```bash
# 只列关键选项；完整步骤与常见 build 错误见 Ch 0
cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AArch64;RISCV;X86" \
  -DLLVM_CCACHE_BUILD=ON \
  -DLLVM_USE_LINKER=lld \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++

ninja -C build llc llvm-tblgen FileCheck not count split-file
```

`build/bin/llvm-lit` 是 configure 阶段生成的脚本，不是 Ninja target；源码构建后直接调用它。需要 C/C++ → IR 时，再额外加 `-DLLVM_ENABLE_PROJECTS=clang`。

版本差异会明确标注；如果使用其他 LLVM 版本，先看 Ch 0 的“版本与默认 selector 的检查表”。

## 本课不涵盖什么

- **从零 port 一个全新 target**：本课用 AArch64 的真实源码和 RISC-V 对照，不带你写完整的 `TargetMachine`/`Subtarget`/`InstrInfo`。
- **SelectionDAG 全套**：只讲与 GlobalISel 对照所需的部分；DAG combine、legalize 细节请回看 [`compiler_backend`](../compiler_backend/README.md)。
- **Register Allocator 与 Scheduler 内部**：ISel 的输出如何被 RA/scheduler 消费只讲接口，不深入算法。
- **GlobalISel combiner 的完整规则语言**：只在它影响 ISel 输入/输出时讲；`GICombineRule` 的系统教学不在本课范围。
- **AArch64 ISA 全集**：指令编码、SIMD/SVE 细节只在案例需要时出现。

## 精选资料库

### 官方文档

- **[LLVM GlobalISel](https://llvm.org/docs/GlobalISel/index.html)**
  - 整个 GlobalISel 文档集的入口。先读首页的 pipeline 概览，再按本课章节进入具体页面。
- **[Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - 五个阶段的契约、pass 顺序、MachineVerifier 与测试/调试方法。Ch 1–2 的主要依据。
- **[InstructionSelect](https://llvm.org/docs/GlobalISel/InstructionSelect.html)**
  - `InstructionSelector::select()`、`selectImpl()`、SelectionDAG rule import、PatLeaf/Custom SDNode/ComplexPattern 的官方说明。全课的核心参考。
- **[MIR Patterns in TableGen](https://llvm.org/docs/GlobalISel/MIRPatterns.html)**
  - GlobalISel 自有 pattern/combiner 语法。注意：它主要服务 combiner，不等同于 ISel 的 SelectionDAG pattern 导入；Ch 23 会厘清边界。
- **[Generic Opcodes](https://llvm.org/docs/GlobalISel/GenericOpcode.html)**
  - `G_*` opcode 的语义与操作数约定。读 gMIR 时的字典。
- **[Legalizer](https://llvm.org/docs/GlobalISel/Legalizer.html)** / **[RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)** / **[IRTranslator](https://llvm.org/docs/GlobalISel/IRTranslator.html)**
  - 上游三个阶段的权威说明；Ch 3–4 只取 ISel 必需的 20%，深入细节时回来查。
- **[Porting GlobalISel to a New Target](https://llvm.org/docs/GlobalISel/Porting.html)**
  - 新增 target 支持时要补哪些类、哪些 hook。Ch 22 的迁移策略会引用。
- **[TableGen](https://llvm.org/docs/TableGen/)**
  - pattern、`PatFrag`、`ImmLeaf`、`ComplexPattern` 的语法基础；读 Ch 5–12 前建议先扫一遍。

### 源码地图（LLVM 23.1.4）

- **`llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`**
  - ISel pass 的 driver：post-order 遍历、bottom-up 选指令、`failedISel` 设置、MachineVerifier 调用。Ch 2/10 逐段读。
- **`llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h`**
  - `GIM_*` matcher opcode 的完整 enum 与语义注释。Ch 6–9 的对照表来源。
- **`llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`**
  - matcher table 的执行实现（约 64KB）。Ch 6 会挑 `GIM_Try`、`GIM_SwitchOpcode`、`GIM_RecordInsn`、`GIM_CheckIsSafeToFold` 逐条走。
- **`llvm/utils/TableGen/GlobalISelEmitter.cpp`**
  - 把 SelectionDAG pattern 转成 GlobalISel matcher table 的 emitter。Ch 5 的核心源码。
- **`llvm/utils/TableGen/GlobalISelCombinerEmitter.cpp`**
  - combiner 规则的 emitter；本课只读它与 ISel 的边界，不展开规则语言。
- **`llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`**
  - AArch64 的 selector 实现（8,249 行）。Ch 13/20 提供阅读地图，不要求逐行读完。
- **`llvm/lib/Target/AArch64/AArch64InstrGISel.td`**
  - AArch64 的 GlobalISel pseudo 与 `GINodeEquiv` 映射；Ch 12/20 的主要 TableGen 案例。
- **`llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp`**
  - RISC-V selector；用来对照“支持范围较小、fallback 更多”的现实。
- **`llvm/lib/Target/RISCV/RISCVInstrGISel.td`**
  - RISC-V 的 GI pseudo / `GINodeEquiv` / ComplexPattern 映射；Ch 11/22 的对照材料。

### 测试语料

- **`llvm/test/CodeGen/AArch64/GlobalISel/`**（767 个文件）
  - AArch64 GlobalISel 的主测试库。Ch 16 会教你按 `instruction-select`、`legalizer`、`regbankselect` 前缀筛选用例。
- **`llvm/test/CodeGen/RISCV/GlobalISel/`**
  - RISC-V 的对照测试；观察哪些 case 仍 fallback。
- **`llvm/test/CodeGen/GlobalISel/`**
  - target-independent 的 MIR/legalizer/selector 测试；适合看最小可复现案例。

### 讨论与演进

- **[LLVM Discourse — globalisel tag](https://discourse.llvm.org/tag/globalisel)**
  - GlobalISel 的 design/RFC/status 讨论。读 Ch 22–23 时用它确认“某个限制是设计选择还是尚未实作”。
- **[GitHub issues — globalisel label](https://github.com/llvm/llvm-project/labels/globalisel)**
  - 真实 bug、fallback 案例与修复讨论。适合练习 D 找案例。
- **[LLVM Project YouTube](https://www.youtube.com/@LLVMProject)**
  - LLVM Dev Meeting 的官方频道；搜索 “GlobalISel” 可找到 status/porting 相关演讲。先看近几年的 status talk，再回头看早期设计动机。

### 读完之后

- **[compiler_backend](../compiler_backend/README.md)**（本 repo）— 补 SelectionDAG、TableGen、MIR、RA/Scheduler 的完整背景。
- **[ssa_optimizations](../ssa_optimizations/README.md)**（本 repo）— 理解 IR 优化如何改变进入 ISel 的形态。
- **[cpu_design](../../architecture/cpu_design/README.md)**（本 repo）— 从 RTL/微架构侧理解“为什么这条指令要这样选”。
- **[gpu_cuda](../../parallel/gpu_cuda/README.md)**（本 repo）— 另一条 backend/代码生成路线，对照 SIMD/GPU 的 selection 思维。

## 课程状态

- [x] Ch 0–4（Part 0）
- [x] Ch 5–10（Part 1）
- [x] Ch 11–15（Part 2）
- [x] Ch 16–19（Part 3）
- [x] Ch 20–23（Part 4）
- [x] Final Project

> ✅ 全部完成（2026-10-23）：24 章 + 练习 A–D + Final Project。根目录 `README.md` 索引与课程 memory 已同步。
