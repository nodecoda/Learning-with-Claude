---
name: tablegen_internals-course-scope
description: LLVM 24 TableGen 源码深潜课程 — 23 章（Ch 0–22）+ 4 练习 + 可合并 backend final，目标是能改 lib/TableGen 与 utils/TableGen 并参与上游。
metadata:
  type: project
---

`compilers/tablegen_internals/` 是一门以 **TableGen 实现本体**为主轴的 LLVM 源码课：不教怎么写 `.td`，而是拆开 `llvm/lib/TableGen/`（14 个 `.cpp`，约 1.1 万行）与 `llvm/include/llvm/TableGen/Record.h`（约 2500 行），最后在 `llvm/utils/TableGen/` 里写出一个能被上游 review 通过的 backend。

**Why:** 用户明确要求「深入分析 td 源码」，并补充「希望学习后可以达到修改源码的水平」「希望学习后可以参与 td 的贡献」。因此课程必须覆盖：词法/语法实现、Record/Init 数据模型、folding 与 multiclass 展开、backend 注册与生成、测试策略、性能、上游流程。语言为简体中文。

**How to apply:**
- 版本基准：本机 LLVM `24.0.0git`（commit `ae47e0668aed`），Release + assertions，只构建 `llvm-tblgen`。
- 文件结构：README + Ch 0–22 + Practice A–D + Final Project。
- Part 0（Ch 0–3）：环境、三段式架构、TGLexer、TGParser。
- Part 1（Ch 4–8）：Record/RecordVal/RecordKeeper、Init 类层次 I/II、fold/resolve、multiclass 展开。
- Part 2（Ch 9–12）：RecTy 类型系统、bits/field 语义、assert/dump/!instances、错误报告基础设施。
- Part 3（Ch 13–17）：backend 注册、生产级 backend、多文件输出、生成代码形态、InstrInfoEmitter 走读。
- Part 4（Ch 18–22）：加语言特性全链路、改 Record 语义的兼容性、测试策略、性能、上游流程。
- 练习 D：`gen-isa-stats` backend 的完整注册流程；Final：写一个 `gen-sched-coverage` 级别的可合并 backend（含 lit 测试、文档、性能预算、PR 模板）。
- 刻意涵盖：真实可跑的 `llvm-tblgen` 实验、`-print-records`/`--dump-json` 观察法、自编译 backend 的完整链接命令、upstream 测试惯例。
- 刻意不涵盖：完整 backend/codegen 教学、各 emitter 生成物的语义细节、Clang backend 内部、LSP/jupyter kernel 深入。
- 教学信条：先数据结构后算法；每个语法结构都要问「它变成哪几个 Init 节点」；所有输出必须实跑；改源码章节标 guided 不伪造构建日志。

状态：✅ 全部完成 (2026-10-29)：29 个文件、13,590 行；Ch 0–22 + Practice A–D + Final，关键实验全部实跑并还原源码树。
