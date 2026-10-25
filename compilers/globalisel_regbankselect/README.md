# LLVM GlobalISel RegBankSelect 学习笔记：从 bank 到 mapping、repair 与 COPY

> 给已经读过 LLVM backend、知道 GlobalISel 五阶段，但 RegBankSelect 只停在“听过”的工程师。

这系列以 **RegBankSelect** 为主轴：先把 bank、LLT、regclass 的关系讲清楚，再拆开 `RegisterBankInfo` 的 mapping 数据模型，解释 Fast/Greedy 如何用局部代价选 bank、何时插入跨 bank COPY，最后在 AArch64 上完成一个受 gate 控制的 custom opcode 的完整 RegBankSelect 支持。全程以 LLVM 23.1.4 源码和 MIR 实验为准，RISC-V 作为对照。

> 证据约定：能在官方 LLVM 23.1.4 预编译包上复现的 MIR 输出标为实测；需要改源码重编译的 custom opcode、TableGen 生成物与 lit 执行标为 guided/源码依据，不伪造构建或测试输出。

## 为什么学这个？

- **真实管线位置**：AArch64 的 GlobalISel 管线会在 `-O0` 默认启用（由 `-aarch64-enable-global-isel-at-O=0` 控制），`-O1` 以上可显式 `-global-isel`。RegBankSelect 是 Legalizer 与 InstructionSelect 之间无法绕过的一站；bank 选错，后面要么多出 COPY，要么直接无法选择。
- **调 bank 比调 pattern 更常遇到**：实际 bug 经常不是“pattern 没写”，而是“这个值为什么在 GPR 而不是 FPR”“为什么 PHI 两边 bank 不一致”“为什么 greedy 跑了却没变化”。这些问题的根因都在 mapping、cost 和 repair。
- **理解代价模型**：RegBankSelect 不是全局最优，它按指令做局部贪心；`copyCost`、`MappingCost`、`RepairingPlacement` 共同决定最终形态。理解这套模型，才能解释 COPY 数量和寄存器压力。
- **上游贡献与面试**：新 target 补 GlobalISel 时，`RegisterBankInfo` 往往是最先要写的部分之一；上游 review 很看重 mapping 的合法性、测试覆盖和 compile-time 影响。
- **与现有课程互补**：[`globalisel_instruction_select`](../globalisel_instruction_select/README.md) 的 Ch 4 只讲 RegBankSelect 的最小必要；本课从那里开始，深入 `RegisterBankInfo`、Fast/Greedy、repair、CFG/call/memory/vector、调试测试与实现。

## 先修知识

- **C++17**（程度：能读模板、虚函数、`std::optional`，能改中小型 C++ 项目）
- **LLVM IR 与 backend 基础**（程度：知道 `llc`、`opt`、MIR、TableGen；读过 [`compiler_backend`](../compiler_backend/README.md) Ch 4–11 或等价内容）
- **GlobalISel 五阶段**（程度：知道 IRTranslator → Legalizer → RegBankSelect → InstructionSelect；不要求写过 selector）
- **AArch64 汇编基础**（程度：认识 `x0`/`w0`/`s0`/`d0`/`q0`，知道 GPR 与 FP/SIMD 是不同寄存器文件）
- 没有也没关系：RISC-V、SVE、寄存器分配内部算法；这些会在需要时补

## 课程地图

### Part 0 — 基础与契约（Ch 0–4）

- [Ch 0 环境搭建与最小 RegBankSelect 实验](./00-environment-setup.md)
- [Ch 1 GlobalISel 全景中的 RegBankSelect](./01-regbankselect-in-globalisel.md)
- [Ch 2 pass 契约、MIR 属性与失败模式](./02-pass-contract-and-mir-state.md)
- [Ch 3 寄存器、寄存器类、bank 与 LLT](./03-registers-banks-and-llt.md)
- [Ch 4 AArch64 的 GPR / FPR / CC bank（RISC-V 对照）](./04-aarch64-banks.md)
- [练习 A：手写 MIR，预测 bank](./practice-a-predict-banks.md)

### Part 1 — 数据模型与算法（Ch 5–10）

- [Ch 5 RegisterBankInfo 数据模型](./05-registerbankinfo-data-model.md)
- [Ch 6 默认 mapping：约束、regclass 与 fallback](./06-default-mapping.md)
- [Ch 7 target mapping 表：opcode/type 分派](./07-target-mapping-tables.md)
- [Ch 8 Fast mode：默认 mapping 与 optnone](./08-fast-mode.md)
- [Ch 9 Greedy mode：alternative mappings 与局部代价](./09-greedy-mode.md)
- [Ch 10 repair 与 COPY 插入](./10-repair-and-copy-insertion.md)
- [练习 B：改一个 mapping，比较 Fast/Greedy](./practice-b-mapping-and-cost.md)

### Part 2 — 真实语义（Ch 11–15）

- [Ch 11 PHI、循环与跨块 bank 一致性](./11-phi-and-control-flow.md)
- [Ch 12 调用、返回与物理寄存器](./12-calls-and-physical-registers.md)
- [Ch 13 内存操作与地址 bank](./13-memory-and-addressing.md)
- [Ch 14 转换指令与跨 bank 语义](./14-conversions-and-cross-bank.md)
- [Ch 15 向量、聚合与边角类型](./15-vectors-aggregates-edge-types.md)
- [练习 C：实现一个 custom mapping](./practice-c-custom-mapping.md)

### Part 3 — 观察、调试与质量（Ch 16–19）

- [Ch 16 观察 RegBankSelect：MIR 与 verifier](./16-observing-regbankselect.md)
- [Ch 17 Fast vs Greedy 实验设计](./17-fast-vs-greedy-experiments.md)
- [Ch 18 常见失败与诊断树](./18-failure-diagnosis.md)
- [Ch 19 MIR/lit 测试与上游质量标准](./19-testing-and-upstream-quality.md)
- [练习 D：调试一个 bank 选错的案例](./practice-d-debug-wrong-bank.md)

### Part 4 — 源码、实现与收束（Ch 20–23）

- [Ch 20 AArch64 RegisterBankInfo 源码走读](./20-aarch64-source-walkthrough.md)
- [Ch 21 为新 target / 新 bank 写 RegisterBankInfo](./21-writing-a-registerbankinfo.md)
- [Ch 22 bank 设计、代价模型与性能权衡](./22-bank-design-and-performance.md)
- [Ch 23 设计空间与下一步](./23-design-space-and-next-steps.md)
- [Final Project：给 AArch64 加一个 feature-gated custom bank/mapping](./final-project-aarch64-custom-regbank.md)

## 学习方式建议

1. **先跑 MIR，再读源码**：每章先 `-stop-after=regbankselect` 或 `-run-pass=regbankselect` 看数据，再打开对应的 `.cpp` / `.def`。没有数据支撑的源码阅读很容易把“可能”当成“必然”。
2. **默认打开 abort**：`-global-isel-abort=1` 让 fallback 显形；`-verify-machineinstrs` 让 bank/COPY 的非法形态尽早暴露。
3. **同一份 MIR 对比 Fast/Greedy**：不要拿两份不同 IR 比较；先固定 legalized MIR，再分别跑默认与 `-regbankselect-greedy`。
4. **故意把它弄坏**：改错 `ValueMapping`、删掉 alternative、让 PHI 两边 bank 不同，观察 `unable to map instruction`、COPY 爆炸与 verifier 报错。
5. **不要把 `-O0` 和 `optnone` 混为一谈**：本课会用 MIR 证明，强制 Fast 的是函数上的 `optnone`，不是 `llc -O0` 本身。
6. **final 不要跳过**：读一百遍 mapping table，不如自己给一个 custom opcode 写默认 mapping、alternative mapping 和 MIR 测试。

## 环境与版本

> **环境**：LLVM 23.1.4，Windows 10/11 x86_64 官方预编译包（实测 MIR），WSL2 Ubuntu 24.04 或 Windows MSVC 源码构建（guided）。主 target 为 AArch64，RISC-V 作对照。

```powershell
# 官方包路径示例；以你机器上的解压位置为准
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
& $llc --version
```

```bash
# 需要改源码 / 用 -debug-only / 跑 lit 时，构建 assertions 版本
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

官方预编译包是 **Optimized build**，没有 `-debug-only`，也没有 `FileCheck` / `llvm-lit`。这不是环境配置错误，Ch 0 会解释哪些实验只需要预编译包、哪些必须自建。

版本差异会明确标注；如果使用其他 LLVM 版本，先看 Ch 0 的“版本与默认模式检查表”。

## 本课不涵盖什么

- **从零 port 一个全新 target**：本课用 AArch64 真实源码和 RISC-V 对照，不带你写完整 `TargetMachine` / `Subtarget` / `InstrInfo`。
- **InstructionSelect 的 matcher table / pattern**：这是另一门课的主线；本课只讲 RegBankSelect 的输出如何被 ISel 消费。
- **Register Allocator 与 Scheduler 内部**：只讲 bank 与 regclass 的接口，不深入 RA 算法。
- **GlobalISel combiner 的完整规则语言**：只在它改变 RegBankSelect 输入时讲。
- **AArch64 ISA 全集 / SVE 指令编码**：只在案例需要时出现。

## 精选资料库

### 官方文档

- **[LLVM GlobalISel](https://llvm.org/docs/GlobalISel/index.html)**
  - 整个 GlobalISel 文档集的入口。先读首页的 pipeline 概览，再按本课章节进入具体页面。
- **[Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - 五个阶段的契约、pass 顺序、MachineVerifier 与测试/调试方法。Ch 1–2 的主要依据。
- **[RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - `RegisterBankInfo`、mapping、Fast/Greedy 的官方说明；注意文档与源码默认值可能不一致，Ch 8 会对照源码纠正。
- **[Generic Opcode](https://llvm.org/docs/GlobalISel/GenericOpcode.html)**
  - `G_*` 指令的语义索引；读 MIR 时查 operand 含义。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - MIR 的 YAML 结构、`regBankSelected` 等属性、`-run-pass` 的用法。Ch 2 的语法依据。

### 源码

- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - pass 主流程、Fast/Greedy 分派、`getRepairCost`、`RepairingPlacement`。Ch 8–10 的核心。
- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - 默认 mapping、copy-like 特例、`getRegBankFromConstraints`。Ch 5–6 的核心。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - AArch64 的 `copyCost`、`getInstrMapping`、alternative mappings。Ch 4、20 的核心。
- **[AArch64RegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterBanks.td)**
  - GPR / FPR / CC bank 的 TableGen 定义与 coverage。
- **[AArch64GenRegisterBankInfo.def](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def)**
  - 生成的 partial mapping 表；Ch 7 会逐段读。

### 测试与演讲

- **[AArch64 GlobalISel tests](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/llvm/test/CodeGen/AArch64/GlobalISel)**
  - `regbankselect-*.mir` 是最接近真实 review 的范例；Ch 19 会挑几份精读。
- **[RISC-V GlobalISel tests](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/llvm/test/CodeGen/RISCV/GlobalISel)**
  - 对照 target 的 bank 结构与支持边界。
- **[LLVM Developers' Meeting 2016: GlobalISel](https://llvm.org/devmtg/2016-11/)**
  - GlobalISel 最早的设计动机与 pipeline 草图；适合建立历史脉络。
- **[LLVM Developers' Meeting 2017](https://llvm.org/devmtg/2017-10/)**
  - 包含 GlobalISel 进展与设计取舍的演讲；读 Ch 22–23 前看更有感觉。

## 课程状态

✅ **全部完成（2026-10-24）**：24 章正文 + 练习 A–D + Final Project，共 30 个 Markdown 文件、约 13,100 行。

- **Part 0（Ch 0–4）+ 练习 A**：环境、全景、pass 契约、bank/LLT、AArch64 三 bank 与 RISC-V 对照。
- **Part 1（Ch 5–10）+ 练习 B**：数据模型、默认 mapping、target mapping 表、Fast、Greedy、repair/COPY。
- **Part 2（Ch 11–15）+ 练习 C**：PHI/循环、调用/物理寄存器、内存/寻址、转换/跨 bank、向量/聚合。
- **Part 3（Ch 16–19）+ 练习 D**：MIR 观察、Fast vs Greedy 实验、诊断树、MIR/lit 与上游质量。
- **Part 4（Ch 20–23）**：AArch64 源码走读、写 RegisterBankInfo、bank 设计与性能、设计空间。
- **Final Project**：feature-gated custom opcode 的 GPR 默认 mapping + FPR alternative，停在 `-stop-after=regbankselect`。

证据等级：`实测`（官方 23.1.4 预编译包可复现的 MIR）、`源码依据`（对照 llvmorg-23.1.4 源码）、`guided`（需自建 assertions 版本或改源码，未伪造输出）。已知 gap：custom opcode 的构建与 lit 执行属于 guided，未在本环境实际跑通。