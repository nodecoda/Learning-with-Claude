---
name: llvm-lit-course-scope
description: LLVM lit 深潜课程（2026）— 26 章 + 4 练习 + 可发布框架 final，从零基础到能读改 llvm/utils/lit 源码并给上游提交。
metadata:
  type: project
---

`compilers/llvm_lit/` 是一门以「lit 实际执行了什么」为主线的测试基础设施课，不是 FileCheck 语法课，也不是 LLVM IR 课。

**Why:** 使用者此前**完全没接触过 lit**，但明确要两条线都覆盖：① LLVM 贡献者视角（会跑/筛/调 `check-llvm`，看懂 `lit.cfg`，最终能改 `llvm/utils/lit`）；② 自建测试框架视角（把 lit 用在自己的编译器/工具项目）。规模选完整厚书 26 章 + 4 练习 + 1 final，对齐 repo 既有 LLVM 课程深度。

**How to apply:**

- 文件结构：`README.md` + Ch 00–25（26 章）+ 练习 A–D + Final，共 32 篇。
- Part 0（00–03）：环境与最小 suite、LLVM 测试哲学、lit 全链路架构、发现与配置级联。
- Part 1（04–09 + 练习 A）：ShTest 写法、directives、替换系统、内置 shell、builtins、FileCheck 边界。
- Part 2（10–14 + 练习 B）：`lit_config`/`config` 生命周期、`lit.cfg` 从零、site cfg + CMake、features、工具替换。
- Part 3（15–18 + 练习 C）：并行、筛选/分片/重跑、报告、失败诊断。
- Part 4（19–23 + 练习 D）：`TestRunner` 解析与执行、Result 模型、discovery/config 源码、GoogleTest format。
- Part 5（24–25 + Final）：自建 suite 工程化、上游贡献、可发布框架 final。
- 基线：LLVM `24.0.0git`，lit `24.0.0dev`，hub commit `3fa53fc0e1b2`（2026-10-30），Ubuntu 22.04 x86_64，Python 3.10.12。
- 关键版本事实：`%T` 在 LLVM 24 已移除（commit `7ff6973f1ae9`，2025-09-26），但 `TestingGuide.md` 仍文档化，属于滞后；`%b` 当前源码不存在；`execute_external=True` 自 LLVM 23 起废弃，默认内置 shell。
- 刻意不涵盖：FileCheck 语言全貌、LLVM IR/pass 编写、TestSuite/LNT 性能基准、Clang 测试套件细节、GUI/IDE 集成。
- 教学信条：先问「lit 实际执行了什么」；文档与源码冲突信源码；测试必须能失败；配置从最小开始；每个源码结论落到文件/函数。
- 证据纪律：所有命令输出必须本机真跑；改源码重构建的章节标 **guided**，不伪造构建日志。

✅ 全部完成 (2026-11-08)：32 篇，15,483 行；26 章 + 4 练习 + final 全部落盘，索引（README/MEMORY/根 README）已同步。
