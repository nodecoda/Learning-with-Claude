---
name: llvm-source-reviewer-course-scope
description: Scope and teaching creed for the LLVM source-reading and upstream-review course
metadata:
  type: project
---

# LLVM 源码阅读与上游 Review 课程范围

## 定位

一门面向「已会 LLVM IR / opt / 某个 backend，但没系统读过 core IR、Analysis、New PM，也没有结构化 review 训练」的工程师课程。目标不是再讲一遍 SelectionDAG，而是让人能在 `llvm-project` 任意位置定位实现、判断一个 PR 的正确性/测试/分层/性能证据，并写出 reviewer 级别的 comment。

## Why

- 用户目标：学习 LLVM 源代码，达到 LLVM PR reviewer 的水平。
- 仓库已有 20 门 LLVM 相关课，但都聚焦单点（RISC-V backend、GlobalISel、TableGen、lit、ISel×调度、target bring-up、维护流程）。缺的是 core IR / Analysis / PassManager / 全树导航 / review 判断力这条横切线。
- 用户已有后端与上游流程背景，所以本课不补 ISA、不补 TableGen 语法，直接讲「reviewer 需要什么」。

## How to apply

- 源码基线：`/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20），LLVM 24.0.0git。
- 实验构建：`/home/ubuntu/llvm/builds/pr-lane/bin`（assertions ON，X86;AArch64;RISCV）。行号会漂，引用以文件+符号为主。
- 结构：Part 0 基线与地图 → Part 1 IR 对象模型与不变量 → Part 2 工程基础设施 → Part 3 Pass 与分析 → Part 4 中端变换 → Part 5 CodeGen/MC → Part 6 Review 能力。
- 每章必须落到真实源码路径/符号；能跑的命令要真跑并贴真实输出，跑不了要标「未实测」。
- Review 章节的判断以 LLVM 官方 `CodeReview.md` / `DeveloperPolicy.md` 与本机 `llvm-reviewer-patterns` skill 的形态统计为依据，不虚构 reviewer 语录。
- 不覆盖：具体 target ISA 细节、Clang 语义实现、MLIR、lldb；需要时指到仓库既有课程。
- 教学信条：先建立「reviewer 会问什么」的问题意识，再给源码位置和可复现实验；每章至少一个反例/失败实验。

## 完成状态

- ✅ 全部完成（2026-11-20）：`compilers/llvm_source_reviewer/` 共 40 篇 16,832 行。
- 33 章正文（Ch 0–32）：Ch 0 环境与证据基线；Ch 1–3 源码地图/编译旅程/阅读方法论；Ch 4–9 IR 对象模型与不变量；Ch 10–12 ADT/TableGen/CMake；Ch 13–17 New PM/支配树/SCEV-AA-MemorySSA/PreservedAnalyses/验证与观测；Ch 18–22 InstCombine/SimplifyCFG/Inliner/GVN/循环与向量化；Ch 23–26 CodeGen 流水线/MIR/寄存器分配/Target-MC-AsmPrinter；Ch 27–32 review 制度、读 PR、正确性、poison/UB、测试、写作与复盘。
- 5 个练习（A 源码考古、B 不变量猎杀、C 分层审计、D 树外 pass + 故意漏报 PreservedAnalyses、E 中端 miscompile 定位）+ 1 个 review-ready PR final（524 行）。
- 质量门槛：每章有目标/环境块、源码 `path:line`、真跑输出、至少一个失败例子、踩雷、重点整理、自我检核、延伸阅读与下一章链接；禁用词扫描、代码围栏配对、相对链接存在性均已机械校验。
- 实测基线：源码 `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`（LLVM 24.0.0git，assertions ON，X86/AArch64/RISCV）；两处已知偏差（构建 worktree `a5eaf194d8b6`；Ch 15 中 `9ac5159b3c46` 修复后输出未实测）已在对应章节标注。
