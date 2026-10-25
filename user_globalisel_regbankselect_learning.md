---
name: globalisel_regbankselect-course-scope
description: LLVM 23.1.4 GlobalISel RegBankSelect course — 24 chapters + 4 practices + feature-gated custom AArch64 opcode final, source/experiment-driven, AArch64 mainline with RISC-V comparison.
metadata:
  type: project
---

`compilers/globalisel_regbankselect/` 是一门以 RegBankSelect 为主轴的 LLVM backend 课程：先把 bank、LLT、regclass 的关系讲清楚，再拆开 `RegisterBankInfo` 的 mapping 数据模型，解释 Fast/Greedy 如何用局部代价选 bank、何时插入跨 bank COPY，最后在 AArch64 上完成一个受 gate 控制的 custom opcode 的 RegBankSelect 支持。

**Why:** 用户希望系统学习 LLVM GlobalISel 的指令生成/RegBankSelect，而不是只看 API 或概念。课程按用户确认的方案采用 LLVM 23.1.4、AArch64 主线、RISC-V 对照、24 章 + 4 练习 + 1 final，并要求每章尽量可跑；教材语言为简体中文。

**How to apply:**
- 文件结构：README + Ch 0–23 + Practice A–D + Final Project，共 30 个 Markdown 文件。
- Part 0（Ch 0–4）：环境与最小实验、GlobalISel 全景、pass 契约/MIR 属性、寄存器/bank/LLT、AArch64 GPR/FPR/CC 与 RISC-V 对照。
- Part 1（Ch 5–10）：RegisterBankInfo 数据模型、默认 mapping、target mapping 表、Fast、Greedy、repair/COPY。
- Part 2（Ch 11–15）：PHI/循环、调用/物理寄存器、内存/地址、转换/跨 bank、向量/聚合/边角类型。
- Part 3（Ch 16–19）：观察 MIR、Fast vs Greedy 实验、失败诊断树、MIR/lit 与上游质量。
- Part 4（Ch 20–23）：AArch64 源码走读、为新 target/新 bank 写 RegisterBankInfo、bank 设计与性能、设计空间。
- Final：feature-gated custom opcode（暂定 `G_AARCH64_CUSTOM_LOGIC`）的 GPR/FPR mapping、copy cost、repair 与 MIR 测试；停在 `-stop-after=regbankselect`，不要求完整 ISel。
- 关键校正：23.1.4 的 RegBankSelect 默认是 Fast；强制 Fast 的是函数上的 `optnone`，不是 `-O0` 本身；AArch64 的 `copyCost` 不对称（GPR→FPR=4，FPR→GPR=5）；alternative mappings 只覆盖少数 opcode。
- 刻意涵盖：AArch64 真实源码路径、可跑 MIR 实验、Fast/Greedy 对比、mapping/cost/repair、MIR/lit 测试与 review。
- 刻意不涵盖：完整 target port、InstructionSelect matcher table/pattern、RA/scheduler 内部、SVE 指令编码全集。
- 教学信条：先直觉后形式化；版本特定行为标注 LLVM 23.1.4；可跑输出优先于想象；无法实跑时明确标注源码依据或 guided。

✅ 全部完成 (2026-10-24)：24 章 + 练习 A–D + Final Project，共 30 个 Markdown 文件、约 13,100 行；根 README 索引与 MEMORY 已同步。