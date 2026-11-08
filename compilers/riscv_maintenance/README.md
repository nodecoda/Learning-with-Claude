# LLVM RISC-V 上游维护与贡献：从 trunk 源码到可合并 PR

> 基线：`/home/ubuntu/llvm/trees/ct-instr-trunk`，LLVM `24.0.0git`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`（2026-11-08）。本课所有“实测”都来自这棵树 + `builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON）。

这门课不是再写一遍“RISC-V 后端全景”。它要回答的是维护者每天真正要回答的四个问题：

1. **这段代码改了什么、会影响谁？** 从 `RISCVFeatures.td` 的一个 `Feature`，追到 TableGen predicate、C++ `hasStdExt*()`、ISel pattern、ABI 校验和 ELF `e_flags`。
2. **怎么证明它坏了、又怎么证明你修好了？** 用 `llc`、`llvm-mc`、`llvm-lit`、MIR、`llvm-reduce` 产出可复现的证据，而不是“看代码觉得对”。
3. **reviewer 会问什么？** 以真实 `[RISCV]` 提交为样本，拆出“一行源码 + 一百行测试”的补丁形状、`Fixes #NNNNN` 的写法、NFC 与 release note 的边界。
4. **怎么把补丁送进上游？** issue 复现 → 根因 → 最小修复 → lit 回归 → PR 描述 → review 往返 → squash merge。

学完你应该站在这个位置：**能独立读懂 RISC-V 后端的任意一个站位，能对一个 `[RISCV]` / `[RISCV][GISel]` / `[RISCV][MC]` issue 做出可验证的修复，并知道上游 reviewer 会盯哪里。**

---

## 为什么是 RISC-V（维护视角）

- **规模足够大，但主线清楚。** 本树 `llvm/lib/Target/RISCV` 有 210 个 `.cpp/.h/.td`、157,290 行；过去 12 个月有 1,545 条提交改到这里，最近 6 个月仍有 820 条。`RISCVISelLowering.cpp` 一年被改 498 次，是绝对热点。
- **扩展模型是数据驱动的。** `RISCVFeatures.td` 有 2,407 行，`+zba` 不是一个 `if`，而是“Feature 记录 → `HasStdExtZba` predicate → `hasStdExtZba()` → pattern → 汇编器校验 → ELF 属性”的一条链。改扩展最容易漏掉其中一环。
- **一个 `-march` 改动经常要跨三个项目。** `clang/lib/Driver/ToolChains/Arch/RISCV.cpp`（选项优先级）、`clang/lib/Basic/Targets/RISCV.cpp` + `RISCVISAInfo.cpp`（校验）、`llvm/lib/Target/RISCV`（代码生成）。只改一处，CI 会在另一处抓到你。
- **测试面巨大，脚本是杠杆。** `llvm/test/CodeGen/RISCV` 有 2,742 个文件，`llvm/test/MC/RISCV` 622 个，`clang/test/CodeGen/RISCV` 2,824 个。不会用 `update_llc_test_checks.py` / `update_cc_test_checks.py` / MIR，贡献速度会慢一个数量级。
- **贡献路径公开且活跃。** `llvm/Maintainers.md:357-366` 明确列出 4 位 RISC-V reviewer；`llvm/docs` 在本快照已是 `.md`；流程、revert、release note 都有成文规则。

---

## 这门课与已有课程的关系

| 已有课程 | 本课怎么用它 | 本课新增什么 |
|---|---|---|
| [`compilers/riscv_backend`](../riscv_backend/README.md) | 复用 TableGen → MC → ABI → ISel → 机器 pass → 链接的站位划分 | **trunk 基线**、可运行构建、维护/测试/review 视角、真实 `[RISCV]` 提交案例 |
| [`compilers/tablegen_internals`](../tablegen_internals/README.md) | 复用 Record/Init、`-gen-*` 生成物知识 | 在 `RISCVInstrInfo*.td` / `RISCVFeatures.td` 上做真实改动与 diff |
| [`compilers/elf_linking`](../elf_linking/README.md) | 复用重定位、relaxation、ELF 节基础 | `R_RISCV_*`、GP relaxation、lld 的 `scanSectionImpl` 状态机 |
| [`compilers/globalisel_instruction_select`](../globalisel_instruction_select/README.md) | 复用 gMIR/Legalizer/RegBankSelect 基础 | RISC-V GISel 的 500 个测试、仍以 fallback 兜底的现状、可贡献的缺口 |
| [`compilers/loongarch_backend`](../loongarch_backend/README.md) | 复用“树审计 → 上游 PR”的组织方式 | RISC-V 特有的扩展矩阵、RVV、ABI 变体、P-ext 热点 |

先修要求：

- C/C++：能读模板、继承、`switch` 分派、指针；不要求写过 LLVM pass。
- 编译原理：IR、SSA、指令选择、寄存器分配的基本概念。
- 汇编：能读 `addi`、`ld`、`beqz` 这类指令；不要求写过 RISC-V 裸机程序。
- 命令行：能跑 `ninja`、`llvm-lit`、`llvm-readobj`；会看 `git log` / `git diff`。

---

## 课程地图：7 个 Part，32 章 + 5 个练习 + 1 个 Final

### Part 0 — 这棵树与这张地图（Ch 00–03）

- [Ch 00 树审计](./00-tree-audit-analysis-branch.md)：`ct-instr-trunk`、`analysis/riscv-maintenance`、构建重配、RISC-V 库存与上游位置
- [Ch 01 后端全景](./01-backend-map.md)：IR → DAG → MIR → MC → ELF 的十二个站位，以及每个站位对应的文件
- [Ch 02 ISA、扩展与 Subtarget](./02-isa-extensions-subtarget.md)：从 `+zba` 到 `hasStdExtZba()`，再到 pattern 与 ELF 属性
- [Ch 03 流水线与 MIR](./03-pipeline-and-mir.md)：`RISCVPassConfig` 的真实 pass 名单、`-stop-after`/`-run-pass`

### Part 1 — TableGen、编码与 MC 层（Ch 04–08 + 练习 A）

- [Ch 04 指令的 TableGen 建模](./04-tablegen-instruction-model.md)：`RISCVInstrFormats.td`、扩展文件、`RISCVInstrInfoP.td` 案例
- [Ch 05 寄存器、寄存器类与调用约定](./05-registers-and-calling-convention.md)：GPR/FPR/VR、`CalleeSavedRegs`、`CSR_*`
- [Ch 06 指令编码与 MC 层](./06-instruction-encoding-and-mc.md)：`MCCodeEmitter`、RVC、fixup、`MCExpr`
- [Ch 07 汇编器、反汇编器与 InstPrinter](./07-asm-parser-disassembler-printer.md)：`MatchInstructionImpl`、别名、往返实验
- [Ch 08 常量与地址物化](./08-constant-materialization.md)：`RISCVMatInt`、`lui/addi`、`movImm`
- [练习 A：加一条指令并跑通往返](./practice-a-tablegen-and-mc.md)：改 `.td`、重编 `llc`/`llvm-mc`、验证 encoding 与 disassembly

### Part 2 — ABI、栈帧、TLS 与链接（Ch 09–12 + 练习 B）

- [Ch 09 调用约定 lowering](./09-calling-convention-lowering.md)：`lp64d`、varargs、sret/byval、RVV 与 Zilsd 的 ABI 细节
- [Ch 10 栈帧与 prologue/epilogue](./10-frame-lowering-and-stack.md)：`determineFrameLayout`、callee-saved、stack clash、RVV padding
- [Ch 11 代码模型、TLS 与 GP relaxation](./11-code-models-tls-gp-relaxation.md)：small/medium/large、`la.*`、`%tprel_*`、`-msmall-data-limit`
- [Ch 12 LLD RISC-V 重定位与 relaxation](./12-lld-relocations-and-relaxation.md)：`R_RISCV_*`、PLT/GOT、`relax()` 状态机
- [练习 B：ABI 与链接审计](./practice-b-abi-and-linker.md)：多种签名 + TLS 模型，追到 `.rela.text` 与最终 ELF

### Part 3 — SelectionDAG（Ch 13–17 + 练习 C）

- [Ch 13 合法化](./13-legalization.md)：类型/操作的 Legal/Custom/Expand/LibCall，RV32 上的 i64
- [Ch 14 SelectionDAG 选指与 pattern](./14-selection-dag-patterns.md)：`RISCVDAGToDAGISel`、`Pat<>`、`ComplexPattern`
- [Ch 15 自定义 lowering 与 intrinsic](./15-custom-lowering-and-intrinsics.md)：`LowerOperation`、`LowerINTRINSIC_*`、`IntrinsicsRISCV.td`
- [Ch 16 DAG combine 与 known bits](./16-dag-combine-and-known-bits.md)：`PerformDAGCombine`、`computeKnownBitsForTargetNode`
- [Ch 17 RVV lowering](./17-rvv-lowering.md)：LMUL、`vsetvli`、`RISCVVLOptimizer`、`RISCVVectorPeephole`
- [练习 C：诊断向量指令选择](./practice-c-vector-selection.md)：从 IR 到 RVV 汇编，解释为什么没选中你想的那条

### Part 4 — 机器层 pass（Ch 18–23 + 练习 D）

- [Ch 18 伪指令展开](./18-pseudo-expansion.md)：PreRA、PostRA、PreEmit 三层与 `Pseudo*` 家族
- [Ch 19 机器层优化](./19-machine-optimizations.md)：`MachineCombiner`、`OptWInstrs`、`MergeBaseOffset`、`LoadStoreOpt`
- [Ch 20 原子操作与内存屏障](./20-atomics-and-memory-barrier.md)：`ExpandAtomicPseudoInsts`、`fence`、Zacas/Zilsd
- [Ch 21 调度模型与 llvm-mca](./21-scheduling-models-and-mca.md)：`SchedWrite`/`SchedRead`、SiFive/XiangShan、资源压力
- [Ch 22 AsmPrinter 与 ELF flags](./22-asm-printer-and-elf-flags.md)：`emitInstruction`、`e_flags`、`.attribute arch`
- [Ch 23 GlobalISel 现状](./23-globalisel-status.md)：500 个测试、fallback 边界、可贡献缺口
- [练习 D：用 MIR 驱动机器 pass](./practice-d-machine-passes.md)：`-run-pass` 实测伪指令展开与机器优化

### Part 5 — Clang 与运行时（Ch 24–26 + 练习 E）

- [Ch 24 Clang Driver](./24-clang-driver.md)：`-march`/`-mabi`/`-mcpu`/`-mtune` 的优先级与默认值
- [Ch 25 Clang TargetInfo 与 builtins](./25-clang-targetinfo-and-builtins.md)：宏、类型、`interrupt` attribute、RVV vector bits
- [Ch 26 compiler-rt、libc 与 qemu](./26-compiler-rt-libc-qemu.md)：builtins、hwprobe、端到端运行
- [练习 E：端到端工具链](./practice-e-toolchain-end-to-end.md)：clang → `.o` → `ld.lld` → qemu

### Part 6 — 测试、调试与贡献（Ch 27–31 + Final）

- [Ch 27 lit 测试与 update 脚本](./27-lit-testing-and-update-scripts.md)：CodeGen/MC/Clang/LLD 四类测试与自动更新
- [Ch 28 调试 CodeGen](./28-debugging-codegen.md)：`-debug-pass`、`-print-after`、`-stats`、`llvm-reduce`、手写 MIR
- [Ch 29 上游贡献流程](./29-contribution-workflow.md)：issue → 复现 → 修复 → 测试 → PR → review，以真实提交为例
- [Ch 30 维护者视角](./30-maintainership-and-triage.md)：triage、review 关注点、release notes、revert 政策
- [Ch 31 高价值贡献地图](./31-contribution-map.md)：按 churn 热点与 issue 类型找可上手的题目
- [Final Project](./final-project-riscv-contribution.md)：独立完成一个可提交上游的 RISC-V 修复或小特性

---

## 实验环境

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git status -sb                 # analysis/riscv-maintenance
T="$LLVM_HOME/builds/ct-instr-trunk"
$T/bin/llc --version | grep -i riscv     # 必须有 riscv32/riscv64
$T/bin/llvm-config --targets-built       # X86 AArch64 RISCV
```

关键事实（2026-11-08 实测）：

- 分支 `analysis/riscv-maintenance` 从 `upstream/main`（`0fc5afa04c23`）开出；原 `fix/issue-239812-relax-marker-order` 分支仍在，可随时切回。
- 收尾复核（2026-11-08）：`upstream/main` 已前进到 `e44fff5decc7`，本分析分支**落后 13 个提交**。全课行号与实测都钉在 `0fc5afa04c23`；跟读时不要先 merge/rebase，否则 `grep -n` 的行号会整体漂移。要跟踪新提交，另开一个 `analysis/riscv-maintenance-<date>` 分支重跑受影响实验。
- `builds/ct-instr-trunk` 原先配置为 `X86;AArch64;LoongArch`，**没有 RISCV**；本课已用 `toolset/configure.sh` 重配为 `X86;AArch64;RISCV`。要恢复 LoongArch，重跑一次 configure 即可，代价是一次增量构建。
- 测试命令示例：

```bash
$T/bin/llvm-lit -sv -j16 llvm/test/CodeGen/RISCV/
$T/bin/llvm-lit -sv llvm/test/MC/RISCV/
$T/bin/llvm-lit -sv clang/test/Driver/riscv-arch.c
```

### 证据约定

- **实测**：在本机构建上真实跑出的输出；命令与输出一起给。
- **源码依据**：对照本树的具体文件、函数或行号。
- **未实测**：需要重编、需要真机、或需要特定上游 commit 才能验证的内容；明确标注，不伪造日志。

> 本课只读取本任务自己的 worktree `trees/ct-instr-trunk` 与 `builds/ct-instr-trunk`，不引用其它 `trees/<task>`。

---

## 建议的读法

- **只想能修 bug**：Part 0 → Ch 27–29 → 从 Ch 31 挑一个热点，回读对应 Part。
- **想能加扩展/指令**：Part 0 → Part 1 → Ch 09–12 → 练习 A/B。
- **想改代码生成质量**：Part 0 → Part 3 → Part 4 → 练习 C/D。
- **想做完整 feature**：全课 + Final；Part 5 至少读 Ch 24–25。

每章固定结构：`为什么` → `主线` → `源码落点` → `可跑实验` → `常见坑` → `自我检查` → `延伸阅读`。练习带可折叠参考解答；Final 要求交出一个 PR-ready 的 patch。

---

## 当前进度

**状态：38/38 全部完成（32 章 + 5 练习 + Final），共 21,284 行。**

| # | 主题 | 行数 | 核心实测 |
|---|---|---|---|
| 00 | 树审计 | 560 | 三组 lit 基线全绿 |
| 01 | 后端全景 | 459 | 十二站位与 pass 顺序 |
| 02 | ISA/扩展/Subtarget | 561 | `+zba` 全链、`+m,-m` 留 `zmmul` |
| 03 | 流水线与 MIR | 440 | 十二个 `add*Passes` 钩子 |
| 04 | TableGen 指令建模 | 491 | 手算 ADD/ADDI/SH3ADD 编码 |
| 05 | 寄存器与调用约定 | 661 | `fa5` 倒序、`v0` 最后、reserve-x18 实测 |
| 06 | 指令编码与 MC | 549 | `MCCodeEmitter` 全链 |
| 07 | 汇编器/反汇编器/Printer | 550 | operand 解析与 alias |
| 08 | 常量与地址物化 | 550 | `RISCVMatInt` 与三处相邻值优化 |
| 09 | 调用约定 lowering | 614 | vararg 偶数规则、`sret`/`byval` |
| 10 | 栈帧与 prologue | 560 | `hasFPImpl` 五条件、RVV 可伸缩帧 |
| 11 | 代码模型/TLS/GP | 560 | 2 GiB 边界、TLS 四模型 |
| 12 | LLD 重定位与 relaxation | 562 | `call→jal`、`--relax-gp` |
| 13 | 合法化 | 560 | RV32 i64/i128 拆分 |
| 14 | SelectionDAG pattern | 557 | `selectSHXADDOp`、MatcherTable |
| 15 | 自定义 lowering 与 intrinsic | 474 | `czero`、RV32 移位序列 |
| 16 | DAG combine 与 known bits | 550 | `SHL_ADD`/`ORC_B`/`SLLW` |
| 17 | RVV lowering | 558 | VLMAX、`$noreg`、VL 传播 |
| 18 | 伪指令展开 | 560 | PreRA/PostRA/PreEmit/Atomics |
| 19 | 机器层优化 pass | 557 | `ADDW→ADD`、压缩、基址合并 |
| 20 | 原子与内存屏障 | 559 | LR/SC、`.aq/.rl`、Zacas |
| 21 | 调度模型与 llvm-mca | 559 | Rocket/U74/P670 实测 |
| 22 | AsmPrinter 与 ELF flags | 559 | `.option`、`.attribute`、`e_flags` |
| 23 | GlobalISel 现状 | 559 | 500 测试全绿、fallback 边界 |
| 24 | Clang Driver | 560 | `-march/-mabi/-mcpu` 优先级 |
| 25 | TargetInfo 与 builtins | 560 | 宏矩阵、`interrupt` attribute |
| 26 | compiler-rt/libc/qemu | 558 | builtins 真跑、ABI 混用错值 |
| 27 | lit 与 update 脚本 | 558 | 3,362 基线、五脚本 diff |
| 28 | 调试 CodeGen | 560 | 分层定位、`llvm-reduce` |
| 29 | 上游贡献流程 | 560 | 三个真实提交解剖 |
| 30 | 维护者视角 | 560 | triage、revert、release note |
| 31 | 高价值贡献地图 | 512 | churn 表与七类题目 |
| A | TableGen 与 MC 练习 | 600 | ANDN 编码、7 道门 |
| B | ABI 与链接器练习 | 599 | 混链报错、relax 前后同输出 |
| C | 向量选择诊断练习 | 590 | 5 个期望 vs 实际 |
| D | MIR 驱动机器 pass 练习 | 599 | 五个 pass、verifier |
| E | 端到端工具链练习 | 600 | clang→lld→qemu |
| Final | 可提交的 RISC-V 修复 | 699 | patch + 测试 + PR 模板 |

### 环境与基线（2026-11-08）

- worktree：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`。
- 构建：`builds/ct-instr-trunk`，`X86;AArch64;RISCV`，assertions ON。
- lit 基线：CodeGen+MC/RISCV **3,362 全绿**；Driver+mca 361；Clang CodeGen+LLD 2,827。
- 运行：`qemu-riscv64-static` 6.2（scalar 端到端可用；RVV 1.0 会 SIGILL，相关结论均标「未实测」）。

### 质量约定

- 每章至少 5 处真实实验（文中标「实测」或「真实输出」）：可复制命令 + 真实输出；跑不了的标「未实测」+ 原因。
- 所有源码行号用 `grep -n` 对本树核对；所有相对链接已验证存在。
- 抽查复核：独立 verifier 对 12 章的载荷实验逐条重跑，11 MATCH / 0 MISMATCH / 1 无法复现（Ch 05 夹具已补全并修正）。
