# LLVM 指令选择 × 调度：以 RISC-V 打通后端的两条主线

> 给已经知道 LLVM IR、跑过 `llc`、读过一点 backend，但面试时被问「为什么选这条指令、为什么排成这样」答不圆的工程师。

这门课不把 instruction selection 和 scheduling 当成两个独立章节。它沿着一条真实函数的生命周期走：IR 进入 SelectionDAG，pattern 选出 MachineInstr，DAG scheduler 给出第一版顺序，MachineScheduler 再按 sched model 重排，寄存器分配和 post-RA 收尾。每一层都能改变最终汇编，但改变的原因不同。学完你能对着 `llc` 的输出说清楚：这条指令是谁选的、这个顺序是谁排的、下一步该看哪个 pass。

## 为什么学这个？

- **面试的真实问法**：CPU 编译器岗位很少单独问「SelectionDAG 有哪些阶段」。更常见的是给一段 IR 或汇编，问「为什么这里选了 `sh3add` 而不是 `slli+add`」「为什么 `add` 被提到两个 `ld` 前面」「换成 `sifive-u74` 为什么顺序变了」。这要求你把选择、调度、sched model、寄存器压力放在同一条链上解释。
- **两层调度经常被混为一谈**：SelectionDAG 内部有 `ScheduleDAGSDNodes`，Machine IR 层有 `MachineScheduler`。前者决定初始顺序，后者在 pre-RA 和 post-RA 再排。只读其中一层，就会把最终顺序错误归因给 instruction selection。
- **RISC-V 是最好的放大镜**：load/store 架构、没有 indexed addressing、12-bit immediate、扩展指令（`Zba`/`Zbb`/`Zicond`）让「一条 IR 能选成几种指令」非常直观。x86 的复杂寻址模式反而会掩盖 selection 与 scheduling 的边界。
- **和仓库现有课程互补**：[`compiler_backend`](../compiler_backend/README.md) 的 Ch 6/13 分别讲 ISel 与 scheduler；[`globalisel_instruction_select`](../globalisel_instruction_select/README.md) 专攻 GlobalISel；[`riscv_backend`](../riscv_backend/README.md) 的 Ch 15/18 分别讲 SelectionDAG 与 sched model。本课只做一件事：把「选择」和「调度」的接缝拆开。

## 先修知识

- **LLVM IR 与 `llc`**（程度：能读 `.ll`、知道 `-O2`、`-mtriple`、`-mcpu`、`-mattr`、`-print-after`）
- **Machine IR / MIR 基础**（程度：知道虚拟寄存器、`COPY`、`PseudoRET`、`-run-pass`；不要求会写复杂 MIR）
- **RISC-V 基础**（程度：知道 load/store、`addi` 的 12-bit immediate、`lui`、`beqz`、`M`/`A`/`F`/`D`/`C` 扩展）
- **C++17 阅读能力**（程度：能读 LLVM 的 `switch`、`SDValue`、`MachineInstr` 调用链；不要求能改大型 backend）
- 没有也没关系：GlobalISel 细节、TableGen 内部实现、RVV、上游 review 经验；这些会在需要时补

## 课程地图

### Part 0 — 基线与全景（Ch 0–3）

- [Ch 0 环境与证据基线](./00-environment-and-baseline.md)
- [Ch 1 一条 IR 到汇编：两条调度主线](./01-ir-to-asm-two-scheduling-lines.md)
- [Ch 2 SelectionDAG、FastISel、GlobalISel：到底谁在选择](./02-selectiondag-fastisel-globalisel.md)
- [Ch 3 RISC-V 的 ISA 约束如何塑造选择](./03-riscv-isa-constraints.md)
- [练习 A：解剖一个函数的 ISel → MISched](./practice-a-dissect-isel-misched.md)

### Part 1 — SelectionDAG：选择与第一层调度（Ch 4–10）

- [Ch 4 SelectionDAG 的数据结构：chain、glue、token](./04-selectiondag-data-structures.md)
- [Ch 5 DAG Combine 与 Legalize：选择之前的战争](./05-dag-combine-and-legalize.md)
- [Ch 6 Pattern matching：从 `.td` 到 `Select()`](./06-pattern-matching.md)
- [Ch 7 一条 pattern 如何生成多条指令](./07-pattern-to-multiple-instructions.md)
- [Ch 8 SelectionDAG 的调度器：`ScheduleDAGSDNodes`](./08-selectiondag-scheduler.md)
- [Ch 9 调度边界：内存、副作用、fence、atomics](./09-scheduling-boundaries.md)
- [Ch 10 `-O0`/FastISel 与 `-O2`：为什么 O0 也会 spill](./10-o0-fastisel-vs-o2.md)
- [练习 B：用 `-pre-RA-sched` 六种模式跑同一段 IR](./practice-b-pre-ra-sched-modes.md)

### Part 2 — MachineScheduler：第二层调度与模型（Ch 11–17）

- [Ch 11 MachineScheduler 全景：region、pre-RA/post-RA、策略](./11-machinescheduler-overview.md)
- [Ch 12 Sched model 三层：`SchedRead/Write`、`WriteRes`、`ReadAdvance`](./12-sched-model-three-layers.md)
- [Ch 13 RISC-V sched models 走读：Rocket / SiFive7 / GenericOOO / XiangShan](./13-riscv-sched-models.md)
- [Ch 14 `llvm-mca`：把模型变成周期数，以及它的边界](./14-llvm-mca.md)
- [Ch 15 调度策略：critical path、pressure、top-down/bottom-up](./15-scheduling-policy.md)
- [Ch 16 调度与寄存器分配的拉扯](./16-scheduling-vs-regalloc.md)
- [Ch 17 Post-RA scheduling、hazard 与 anti-dependency](./17-post-ra-scheduling.md)
- [练习 C：给一条 RISC-V 指令补 `SchedRW`，用 mca 验证](./practice-c-schedrw-and-mca.md)

### Part 3 — 选择 × 调度的交互面（Ch 18–24）

- [Ch 18 指令选择如何决定调度自由度：`select`、`setcc`、`Zicond`、`Zba`](./18-isel-scheduling-freedom.md)
- [Ch 19 内存操作数与 folding：RISC-V 没有 indexed addressing 意味着什么](./19-memory-folding.md)
- [Ch 20 伪指令、宏融合与调度可见性](./20-pseudo-expansion.md)
- [Ch 21 分支、if-conversion、`MachineCombiner`](./21-branch-if-conversion.md)
- [Ch 22 循环、软件流水与调度边界](./22-loop-software-pipelining.md)
- [Ch 23 RVV：`vsetvli`、向量选择与调度](./23-rvv-selection-scheduling.md)
- [Ch 24 诊断树：选错、排错、spill、pseudo 未展开](./24-diagnosis-tree.md)
- [练习 D：给「选对了但排得慢」做根因分析](./practice-d-slow-schedule-root-cause.md)

### Part 4 — 质量、上游与设计空间（Ch 25–29）

- [Ch 25 TableGen 质量：pattern、predicate、`SchedRW` 完整性](./25-tablegen-quality.md)
- [Ch 26 MIR/lit 测试：同时锁定选择和调度](./26-mir-lit-testing.md)
- [Ch 27 性能归因：mca、perf、`-stats`、`-time-passes`](./27-performance-attribution.md)
- [Ch 28 上游 PR 与 review：RISC-V backend 的规矩](./28-upstream-pr-review.md)
- [Ch 29 设计空间：SelectionDAG vs GlobalISel、VLIW、未来](./29-design-space.md)
- [Final Project：给 RISC-V 加一条带调度模型的自定义指令](./final-project-riscv-custom-instruction-sched.md)

## 学习方式建议

1. **每条结论都跑一遍**：这门课的输出都来自本机 LLVM 24.0.0git 构建。不要只看书里的汇编，自己跑 `llc` 对比 `-mcpu`、`-mattr`、`-pre-RA-sched`。
2. **先看 MIR，再看汇编**：`-print-after-isel` 和 `-print-before/after=machine-scheduler` 是分界线。只看最终汇编，你会把 scheduler 的功劳/锅算到 selection 头上。
3. **故意换 CPU**：同一段 IR 用 `generic`、`rocket-rv64`、`sifive-u74` 各跑一次，记录哪些指令变了、哪些顺序变了。顺序变而指令不变，说明是 scheduling；指令变而顺序不变，说明是 selection 或 feature。
4. **用 `llvm-mca` 量化，但别迷信**：mca 是模型模拟器，不是芯片模拟器。每个周期数都要带上 `-mcpu` 和 `-mattr`。
5. **把问题写成「谁做的」**：每次看到差异，先问「这是 SelectionDAG、DAG scheduler、MachineScheduler、RA 还是 pseudo expansion 做的？」再去看对应源码。

## 证据约定

- 环境：LLVM 24.0.0git，源码树 `/home/ubuntu/llvm/trees/explore`，commit `700e551714cc`（2026-11-10），构建 `/home/ubuntu/llvm/builds/explore`。
- 所有 `llc` / `llvm-mca` 输出都是在本机真实运行得到的；命令与完整参数随文给出。
- 需要改 LLVM 源码并重新构建的段落会明确标 **guided**，不会伪造构建日志或测试结果。
- RISC-V 行为随 LLVM 版本变化很快；版本不同先看 `llc --version` 和 `llc -march=riscv64 -mcpu=help`。

## 本课不涵盖什么

- 不教 LLVM 的完整构建流程；只假设你有一个能跑 RISC-V 的 `llc`。
- 不展开 TableGen 的 lexer/parser/记录模型；这些在 [`tablegen_internals`](../tablegen_internals/README.md)。
- 不把 GlobalISel 的 matcher table 再讲一遍；这些在 [`globalisel_instruction_select`](../globalisel_instruction_select/README.md)。
- 不覆盖 GPU/加速器后端、JIT、链接器细节。
- 不承诺「学完就能改任何后端」；目标是让你能定位、解释、验证 RISC-V 上的选择与调度问题。

## 精选资料

### 必读基础

- **[LLVM Language Reference](https://llvm.org/docs/LangRef.html)** — 读 IR 的最终仲裁；本课所有 `.ll` 都以它为准。
- **[LLVM SelectionDAG 文档](https://llvm.org/docs/CodeGenerator.html#selectiondag)** — 官方 CodeGenerator 文档的 SelectionDAG 小节；先建立阶段地图，再读源码。
- **[LLVM Machine Code Layer 文档](https://llvm.org/docs/CodeGenerator.html#machine-code)** — MachineInstr、MIR、scheduler 的官方入口。
- **[llvm-mca 文档](https://llvm.org/docs/CommandGuide/llvm-mca.html)** — 每个报告字段的定义；第 14 章会逐项拆。

### 推荐论文 / 演讲

- **"Global Instruction Selection"** — LLVM Developers' Meeting 系列演讲；理解 SelectionDAG 与 GlobalISel 的设计取舍。
- **"Instruction Scheduling in LLVM"** — LLVM Developers' Meeting 的 scheduler 演讲；MachineScheduler 的策略与模型。

### 推荐博客 / 源码

- **[LLVM 源码](https://github.com/llvm/llvm-project)** — 本课主要教材。重点文件在每章「源码落点」列出。
- **[RISC-V LLVM 文档](https://llvm.org/docs/RISCVUsage.html)** — RISC-V target 的 feature、ABI、扩展支持。
- **[RISC-V ISA Spec](https://riscv.org/technical/specifications/)** — 指令语义、immediate 范围、扩展定义。

### 读完之后

- **[`globalisel_instruction_select`](../globalisel_instruction_select/README.md)** — 把本课的 matcher/selector 视角扩展到 GlobalISel。
- **[`riscv_backend`](../riscv_backend/README.md)** — 补 RISC-V 的 MC 层、ABI、重定位、RVV。
- **[`llvm_lit`](../llvm_lit/README.md)** — 把本课的 MIR/lit 测试做成可维护的回归套件。
