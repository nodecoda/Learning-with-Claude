# LLVM 源码阅读与上游 Review：从「会用 opt」到「敢留 blocker」

> 给已经会写 LLVM IR、跑过 `opt`/`llc`、读过某个 backend，但想从「熟悉一个角落」升级到「能在 llvm-project 任意位置定位问题，并对别人的 PR 说清楚哪里会坏」的工程师。

这门课不按目录讲 LLVM，而按**一个 reviewer 需要回答的问题**组织：这段 IR 合法吗？这个 pass 凭什么可以不改 DominatorTree？这个 pattern 为什么必须放在 target 而不是 generic？这条测试真的会在改动前失败吗？前半程补齐 core IR、Analysis、PassManager、中端与代码生成的源码地图；后半程把这些知识变成 review 判断：正确性、测试、API、分层、性能证据。学完你能对一个陌生 PR 做出结构化预审，而不是只挑格式。

**课程状态**：33 章正文 + 5 个练习 + 1 个 review-ready PR final，共 40 篇 16,832 行（2026-11-20 完成）。每章都有源码 `path:line`、本机真跑输出、至少一个失败实验与 review 检查表。

## 为什么学这个？

- **Review 是预测，不是挑错**：LLVM reviewer 的核心工作是在 diff 上预测「未来哪个 pass 会因此失效、哪个 target 会因此少一条 pattern、哪条测试其实什么都没锁住」。这要求你把 IR 不变量、analysis 契约、pass 失效、测试语义放在同一条链上。
- **LLVM 太大，没有地图会永远停在 target**：源码树 1.7G、`llvm/lib` 下 3184 个 `.cpp`。只读自己熟悉的 backend，会错过 `Verifier`、`AnalysisManager`、`PreservedAnalyses`、`DominatorTree` 这些真正决定正确性的地方。
- **本仓库现有 LLVM 课都在挖单点**：[`llvm_isel_sched_riscv`](../llvm_isel_sched_riscv/README.md) 挖 ISel×调度，[`globalisel_instruction_select`](../globalisel_instruction_select/README.md) 挖 GlobalISel，[`tablegen_internals`](../tablegen_internals/README.md) 挖 TableGen，[`ssa_optimizations`](../ssa_optimizations/README.md) 挖中端分析，[`riscv_maintenance`](../riscv_maintenance/README.md) 挖上游流程。本课补的是它们之间的接缝：core IR、New PM、analysis invalidation、测试证明、review 写作。
- **reviewer 能力可训练**：把「这条 comment 属于哪一类、对应哪个不变量、最小改法是什么」拆成固定流程，练 30 个真实 diff，比泛读源码有效。

## 先修知识

- **LLVM IR**（程度：能读 `.ll`，知道 `phi`、`getelementptr`、`attributes`、`metadata`、`poison` 的存在）
- **`opt` / `llc`**（程度：用过 `-passes=`、`-print-after`、`-S`、`-mtriple`；不要求记得全部 flag）
- **lit / FileCheck**（程度：能读 `RUN:` 行与 `CHECK:` 断言的对应关系）
- **C++17**（程度：能读模板、继承、`dyn_cast`、lambda；不要求能手写 LLVM 风格的 ADT）
- **Git / GitHub PR**（程度：会 rebase、看 diff、按 commit 读历史）
- 没有也没关系：TableGen 内部、SelectionDAG 细节、GlobalISel、特定 target ABI；需要时会指到对应课程

## 课程地图

### Part 0 — 基线与地图（Ch 0–3）

- [Ch 0 环境与证据基线](./00-environment-and-evidence-baseline.md)
- [Ch 1 源码树地图：从 3184 个 `.cpp` 找到该看的那一个](./01-source-tree-map.md)
- [Ch 2 一次编译的完整旅程：driver → cc1 → opt → llc → MC → lld](./02-compilation-journey.md)
- [Ch 3 读 LLVM 源码的方法论：类层次、`dyn_cast`、visitor、测试反推](./03-how-to-read-llvm-source.md)
- [练习 A：源码考古——从一个 flag 追到实现](./practice-a-source-archaeology.md)

### Part 1 — IR 对象模型与不变量（Ch 4–9）

- [Ch 4 `Value` / `User` / `Use`：def-use 链表与操作数所有权](./04-value-user-use.md)
- [Ch 5 `Type` 与 `DataLayout`：类型系统、布局与 opaque pointer](./05-type-and-datalayout.md)
- [Ch 6 `Module` / `Function` / `BasicBlock` / `Instruction`：容器、所有权、迭代器](./06-module-function-basicblock-instruction.md)
- [Ch 7 常量、全局值与调用契约：`Constant`、`GlobalValue`、`AttributeList`、`CallingConv`](./07-constants-globals-attributes.md)
- [Ch 8 `Metadata` 与调试信息：`MDNode`、`DILocation`、`!tbaa`、`assume`](./08-metadata-and-debuginfo.md)
- [Ch 9 `Verifier` 与 IR 不变量：什么样的 IR 才算合法](./09-verifier-and-ir-invariants.md)
- [练习 B：给一段「看起来能跑」的 IR 找不变量违规](./practice-b-ir-invariant-hunt.md)

### Part 2 — 工程基础设施（Ch 10–12）

- [Ch 10 `ADT` 与 `Support`：为什么 LLVM 不用 STL 的那一半](./10-adt-and-support.md)
- [Ch 11 TableGen 在 LLVM 中的位置：`.td` 如何变成 `.inc`](./11-tablegen-in-llvm.md)
- [Ch 12 库分层、CMake 与 include 规则：为什么这个依赖不能加](./12-cmake-layering-and-build.md)
- [练习 C：给一条新依赖画分层图](./practice-c-layering-audit.md)

### Part 3 — Pass 基础设施与分析（Ch 13–17）

- [Ch 13 New Pass Manager：`PassBuilder`、`AnalysisManager`、pipeline 文本](./13-new-pass-manager.md)
- [Ch 14 支配树、循环与 CFG 分析：`DominatorTree`、`LoopInfo`、`CycleInfo`](./14-dominators-loops-analyses.md)
- [Ch 15 SCEV、`AliasAnalysis` 与 `MemorySSA`：三种「循环与内存」推理](./15-scalarevolution-alias-memoryssa.md)
- [Ch 16 `PreservedAnalyses` 与失效：pass 说「没坏」之前，先证明](./16-preserved-analyses-and-invalidation.md)
- [Ch 17 验证与观测：`Verifier`、`Debugify`、`-print-after`、statistics](./17-verification-and-observability.md)
- [练习 D：写一个 out-of-tree pass 并故意漏报 `PreservedAnalyses`](./practice-d-out-of-tree-pass.md)

### Part 4 — 中端变换怎么读（Ch 18–22）

- [Ch 18 `InstCombine` 与 `InstSimplify`：canonicalization、worklist、fixpoint](./18-instcombine-and-instsimplify.md)
- [Ch 19 `SimplifyCFG` 与 CFG 变换：`DomTreeUpdater` 的正确用法](./19-simplifycfg-and-cfg-transforms.md)
- [Ch 20 Inliner、CallGraph 与 SCC：跨函数变换的边界](./20-inliner-callgraph-scc.md)
- [Ch 21 `GVN` / `EarlyCSE` / `NewGVN`：冗余消除的三种代价模型](./21-gvn-earlycse.md)
- [Ch 22 循环与向量化：`LoopUnroll`、`LICM`、`LoopVectorize`、TTI](./22-loop-and-vectorizer.md)
- [练习 E：定位一个中端 miscompile 的引入 pass](./practice-e-middle-end-bug-hunt.md)

### Part 5 — 代码生成与 MC（Ch 23–26）

- [Ch 23 CodeGen 流水线源码入口：ISel → Machine IR → RA → MC](./23-codegen-pipeline.md)
- [Ch 24 Machine IR 与 MIR：`MachineFunction`、`MachineInstr`、`LiveIntervals`](./24-machine-ir-and-mir.md)
- [Ch 25 寄存器分配与机器 pass：`LiveIntervals`、spiller、pass 顺序](./25-regalloc-and-machine-passes.md)
- [Ch 26 Target / MC / AsmPrinter：后端 review 的判断面](./26-mc-target-asmprinter.md)

### Part 6 — Review 能力（Ch 27–32）

- [Ch 27 LLVM review 制度：GitHub PR、reviewer 职责、LGTM、revert、RFC](./27-review-process-and-culture.md)
- [Ch 28 读一个 PR 的顺序：diff 形态、baseline commit、拆分](./28-reading-a-pr.md)
- [Ch 29 正确性 review 一：IR 合法性、支配关系、迭代器、生命周期](./29-correctness-review-ir-and-cfg.md)
- [Ch 30 正确性 review 二：poison、undef、freeze、溢出、UB、fast-math](./30-correctness-review-poison-ub.md)
- [Ch 31 测试 review：这条 lit 测试真的会在改动前失败吗？](./31-test-review.md)
- [Ch 32 Review 写作与真实 PR 复盘：从 comment 到 LGTM](./32-review-writing-and-case-studies.md)
- [Final Project：从 issue 到 review-ready PR](./final-project-review-ready-pr.md)

## 学习方式建议

1. **每个结论都指到源码**：本课的 `file:line` 以 2026-11-20 的 `llvm-project` `main`（`7c7a9072f736`）为准。行号会漂，函数名和文件路径相对稳定；找不到时用 `rg` 搜符号，不要背行号。
2. **先把实验跑出来再读结论**：`opt -passes=... -print-after=...`、`-debug-only=...`、`-verify-each` 的输出比任何描述都可靠。教材里的输出都标了产生它的构建。
3. **读 PR 时先写三句话**：它改了什么、它声称为什么安全、它用什么测试证明。三句写不出来，说明你还没读懂，不要开始挑格式。
4. **每章至少做一次「反向实验」**：把 `PreservedAnalyses` 改错、把测试的 `CHECK` 删一行、把 pattern 的 predicate 放宽，观察哪个检查会抓到你。
5. **用真实 PR 练 review**：仓库的 `git log` 与 GitHub PR 都是素材；先自己写评论，再对照真实 reviewer 的评论。

## 精选资料库

### 必读基础

- **[LLVM Language Reference](https://llvm.org/docs/LangRef.html)**
  - 权威 IR 语义来源；`poison`/`undef`/`freeze`、`getelementptr`、`attributes` 出问题时以此为准
- **[LLVM Developer Policy](https://llvm.org/docs/DeveloperPolicy.html)**
  - 提交、review、revert、RFC、测试要求的制度来源；Ch 27 的主线
- **[LLVM Code Review Policy](https://llvm.org/docs/CodeReview.html)**
  - LGTM、post-commit review、拆分请求的官方措辞
- **[Writing an LLVM New PM Pass](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)**
  - 最小可运行 pass 的官方骨架；Ch 13/16 的对照

### 推荐阅读

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - `Value`/`User`/`Type`/ADT 的官方导览；Ch 4/10 的补充
- **[LLVM Analysis and Transform Passes](https://llvm.org/docs/Passes.html)**
  - pass 清单与一句话职责；查「这个 pass 到底做什么」的第一站
- **[How to Write a LLVM Backend](https://llvm.org/docs/WritingAnLLVMBackend.html)**
  - target 分层与 TableGen 的角色；Ch 11/26 的背景

### 读完之后

- **[LLVM Discourse](https://discourse.llvm.org/)**：RFC 与设计讨论的真实战场；读 review 卡住时，先搜这里有没有先例
- **[LLVM GitHub PRs](https://github.com/llvm/llvm-project/pulls)**：本课 Part 6 的题库；按 label 找 `bug`、`miscompile`、`tablegen` 各读 10 个
- [`llvm_isel_sched_riscv`](../llvm_isel_sched_riscv/README.md) / [`tablegen_internals`](../tablegen_internals/README.md)：把本课 Part 5 的两个方向继续挖深
