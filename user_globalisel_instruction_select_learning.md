---
name: globalisel_instruction_select-course-scope
description: LLVM 23.1.4 GlobalISel InstructionSelect course — 24 chapters + 4 practices + custom AArch64 pseudo final, source/experiment-driven, AArch64 mainline with RISC-V comparison.
metadata:
  type: project
---

`compilers/globalisel_instruction_select/` 是一门以 InstructionSelect 为主轴的 LLVM backend 课程：从 gMIR、Legalizer、RegBankSelect 的必要背景出发，拆开 TableGen 生成的 matcher table，最后在 AArch64 上完成一条带 GlobalISel 支持的 custom pseudo。

**Why:** 用户希望系统学习 LLVM GlobalISel 的指令生成/InstructionSelect，而不是只看 API 或概念。课程按用户确认的方案采用 LLVM 23.1.4、AArch64 主线、RISC-V 对照、24 章 + 4 练习 + 1 final，并要求每章可跑；教材语言为简体中文。

**How to apply:**
- 文件结构：README + Ch 0–23 + Practice A–D + Final Project，共 30 个 Markdown 文件。
- Part 0（Ch 0–4）：环境、全景、pass 契约、IRTranslator、Legalizer/RegBankSelect。
- Part 1（Ch 5–10）：TableGen 导入、matcher 执行器、类型/bank/立即数、feature/内存/原子/fold、嵌套匹配、fallback。
- Part 2（Ch 11–15）：ComplexPattern、custom node/pseudo、C++ selector、多输出/内存/原子、向量/intrinsic/RISC-V。
- Part 3（Ch 16–19）：MIR/lit、调试工具链、诊断树、上游质量标准。
- Part 4（Ch 20–23）：AArch64 源码走读、端到端 custom instruction、RISC-V 现状、设计空间。
- Final：feature-gated `ADDXri_Custom` pseudo，整合 pattern、pseudo、MIR/lit、调试与 review。
- 刻意涵盖：AArch64 matcher/pattern/selector 的真实源码路径、可跑 `llc` 实验、fallback 行为、MIR/lit 测试。
- 刻意不涵盖：完整 LLVM 构建教学、寄存器分配/调度细节、GlobalISel Combiner 的完整规则语言、真实新指令编码。
- 教学信条：先直觉后形式化；每个版本特定行为标注 LLVM 23.1.4；可跑输出优先于想象；无法实跑时明确标注源码依据。

✅ 全部完成 (2026-10-23)：24 章 + 练习 A–D + Final Project；根目录 README 索引与 memory 已同步。
