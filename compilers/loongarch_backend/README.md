# LLVM LoongArch 后端与工具链：从 ISA 到链接，从 lit 到上游 PR

> 基线：`/home/ubuntu/llvm/trees/ct-instr-trunk`，LLVM `24.0.0git`，HEAD `01df7c365d7c`（2026-11-07），分支 `fix/issue-239812-relax-marker-order`。本课所有“实测”输出都来自这棵树 + 一份补上了 `LoongArch`/`lld` 的本机构建；命令与环境见下方“实验环境”。

这门课不是“`clang --target=loongarch64` 怎么用”的教程。它要回答四个更具体的问题：

1. **一条 LoongArch 指令是谁生成的？** 从 IR、SelectionDAG、DAG pattern、伪指令展开、寄存器分配，到 `MCInst` 和 32 位编码，每一站都能在 `llvm/lib/Target/LoongArch` 里指到文件和符号。
2. **一个全局变量/TLS 变量怎么变成 ELF 里的重定位？** 从 `lowerGlobalAddress`、代码模型、`MO_*` 标志、`%le_hi20_r` 这类汇编修饰符，到 `R_LARCH_*` 和 `ld.lld` 的 relaxation。
3. **怎么证明你改对了？** 用 `llc`、`llvm-mc`、`llvm-readobj`、`llvm-objdump`、`ld.lld`、`qemu-loongarch64` 产出可对照的证据，而不是只读代码猜。
4. **怎么把修复送进上游？** 以本树分支正在做的 issue #239812 为完整案例：issue 复现 → 根因定位 → 最小修复 → lit 回归 → PR 描述与 review 关注点。

学完你应该站在这个位置：**能独立读懂 LoongArch 后端的任意一个 pass，能对一个 `[LoongArch]`/`[lld][LoongArch]` 的 issue 做出可验证的修复，并知道上游 reviewer 会问什么。**

---

## 为什么是 LoongArch

选 LoongArch 做“完整后端 + 工具链”的深潜，有几个很实际的理由：

1. **ISA 小而规整，但不是玩具。** 基础整数指令是典型的 3 操作数 RISC；寄存器 `$r0`–`$r31`、`$f0`–`$f31`；寻址模式只有“基址 + 12 位立即数”。你不需要先背两周指令集就能开始读选指代码。
2. **它有一套别处不常见的“成对重定位”设计。** `R_LARCH_RELAX` 是一个和另一条重定位同 offset 的标记；`lu12i.w + add.d + addi.d` 这种 TLS LE 序列靠它被 linker 收缩成一条 `addi.d $a0, $tp, 0`。本树正在修的 #239812 就出在这里：标记和它标注的重定位谁先谁后，ABI 并没有规定。
3. **向量扩展是“两个宽度、同一套语义”。** LSX 是 128 位，LASX 是 256 位；很多 lowering 要在两者之间复用同一套 DAG 逻辑，是读 `LowerOperation`/`PerformDAGCombine` 的好材料。
4. **后端仍然以 SelectionDAG 为主，没有 GlobalISel。** 这对学习反而友好：你只需要跟一条主线，不会被两套选指框架分散注意力；同时又能对照你已经学过的 GlobalISel 课程，理解“为什么这个后端还没迁”。
5. **贡献面完整且活跃。** 同一棵 monorepo 里同时有 `llvm/lib/Target/LoongArch`、`clang` 的 LoongArch target、`lld/ELF/Arch/LoongArch.cpp`、`compiler-rt` 的 LoongArch 支持。上游最近 40 条 LoongArch 相关提交覆盖调度模型、向量成本、伪指令展开、分支优化、链接 relaxation——都是可以上手的方向。

---

## 这门课与已有课程的关系

| 已有课程 | 本课怎么用它 | 本课新增什么 |
|---|---|---|
| [`compilers/riscv_backend`](../riscv_backend/README.md) | 复用“TableGen → MC → ABI → ISel → 机器 pass → 链接”的站位划分 | LoongArch 特有的成对重定位、`MO_RELAX`、LSX/LASX、LA32/LA64 双模式 |
| [`compilers/elf_linking`](../elf_linking/README.md) | 复用重定位、relaxation、ELF 节的基础 | 把 `R_LARCH_*` 和 lld 的 `relax()` 状态机逐行拆开 |
| [`compilers/tablegen_internals`](../tablegen_internals/README.md) | 复用 TableGen 生成物、pattern 匹配的知识 | 用 LoongArch 的 `InstrFormats.td`/`InstrInfo.td` 做真实案例 |
| [`compilers/globalisel_instruction_select`](../globalisel_instruction_select/README.md) | 对照“另一条选指路线” | 解释 LoongArch 为什么仍只有 SelectionDAG，以及迁移的难点 |

先修要求：

- C/C++：能读模板、继承、`switch` 分派、指针；不要求写过 LLVM pass。
- 编译原理：IR、SSA、指令选择、寄存器分配的基本概念。
- 汇编：能读 `add.d`、`ld.d`、`bnez` 这类指令；不要求写过 LoongArch 裸机程序。
- 命令行：能跑 `ninja`、`llvm-lit`、`llvm-readobj`；会看 `git log`/`git diff`。

不要求你装过 LoongArch 交叉工具链。第 00 章会告诉你本机已经有什么，以及缺什么怎么补。

---

## 课程地图：7 个 Part，31 章 + 5 个练习 + 1 个 Final

### Part 0 — 这棵树与这张地图（Ch 00–03）

- [Ch 00 深入分析 `ct-instr-trunk`](./00-tree-audit-ct-instr-trunk.md)：这棵树是什么、当前分支在修什么、构建缺了什么、#239812 的根因与验证
- [Ch 01 后端全景](./01-backend-map.md)：IR → DAG → MIR → MC → ELF 的十二个站位，以及每个站位对应的文件
- [Ch 02 ISA、Subtarget 与 ABI](./02-isa-subtarget-abi.md)：LA32/LA64、Feature、CPU、`ilp32/lp64`、寄存器与代码模型
- [Ch 03 流水线与 MIR](./03-pipeline-and-mir.md)：真实的 pass 名单、`-stop-after`/`-run-pass`、MIR 语法

### Part 1 — TableGen、编码与 MC 层（Ch 04–08 + 练习 A）

- [Ch 04 指令的 TableGen 建模](./04-tablegen-instruction-model.md)：`LAInst`、`Fmt*`、`ALU_3R`、`Sched<>`、pattern
- [Ch 05 寄存器、寄存器类与调用约定](./05-registers-and-calling-convention.md)：GPR/FPR/CFR/SCR、CSR、`CalleeSavedRegs`
- [Ch 06 指令编码与 MC 层](./06-instruction-encoding-and-mc.md)：`MCCodeEmitter`、fixup、`MCExpr`、ELF object writer
- [Ch 07 汇编器、反汇编器与 InstPrinter](./07-asm-parser-disassembler-printer.md)：`MatchInstructionImpl`、别名、`$` 前缀、往返实验
- [Ch 08 常量物化](./08-constant-materialization.md)：`MatInt`、`lu12i.w/ori/lu32i.d/lu52i.d`、`movImm`
- [练习 A：加一条指令并跑通往返](./practice-a-tablegen-and-mc.md)：改 `.td`、重编 `llc`/`llvm-mc`、验证 encoding 与 disassembly

### Part 2 — ABI、栈帧、TLS 与链接（Ch 09–12 + 练习 B）

- [Ch 09 调用约定 lowering](./09-calling-convention-lowering.md)：`analyzeInputArgs`/`analyzeOutputArgs`、`LowerFormalArguments`、varargs、sret/byval
- [Ch 10 栈帧与 prologue/epilogue](./10-frame-lowering-and-stack.md)：`hasFP`、`adjustReg`、callee-saved、stack probe、shrink-wrapping
- [Ch 11 代码模型与 TLS](./11-code-models-and-tls.md)：small/medium/large、`la.pcrel`/`la.got`/`la.tls.*`、`MO_*` 标志
- [Ch 12 lld 重定位与 relaxation](./12-lld-relocations-and-relaxation.md)：`scanSectionImpl`、`getRelExpr`、`relax()` 状态机、`R_LARCH_RELAX`
- [练习 B：ABI 与链接审计](./practice-b-abi-and-linker.md)：六种签名 + 三种 TLS 模型，追到 `.rela.text` 与最终 ELF

### Part 3 — SelectionDAG（Ch 13–17 + 练习 C）

- [Ch 13 合法化](./13-legalization.md)：类型/操作的 Legal/Custom/Expand/LibCall，LA32 上的 i64
- [Ch 14 SelectionDAG 选指与 pattern](./14-selection-dag-patterns.md)：`LoongArchISelDAGToDAG`、`Pat<>`、`ComplexPattern`
- [Ch 15 自定义 lowering 与 intrinsic](./15-custom-lowering-and-intrinsics.md)：`LowerOperation`、`lowerINTRINSIC_*`、`IntrinsicsLoongArch.td`
- [Ch 16 DAG combine 与 known bits](./16-dag-combine-and-known-bits.md)：`PerformDAGCombine`、`computeKnownBitsForTargetNode`
- [Ch 17 LSX/LASX 向量 lowering](./17-lsx-lasx-vector-lowering.md)：`BUILD_VECTOR`、shuffle、reduction、`V{ALL,ANY}_*`
- [练习 C：选一条向量指令](./practice-c-vector-selection.md)：从 IR 到 LSX/LASX 汇编，解释为什么没选中你想的那条

### Part 4 — 机器层 pass（Ch 18–23 + 练习 D）

- [Ch 18 Pre-RA 伪指令展开](./18-prera-pseudo-expansion.md)：`PseudoLA_*`、TLS 五种模型、`expandPcaxxu12iInstPair`
- [Ch 19 MergeBaseOffset 与 OptWInstrs](./19-merge-base-offset-and-optw.md)：地址折叠、`W` 后缀消除/转换
- [Ch 20 MachineCombiner 与 LateBranchOpt](./20-machine-combiner-and-late-branch-opt.md)：结合律重排、分支反转、无条件跳转消除
- [Ch 21 原子操作与内存屏障优化](./21-atomics-and-memory-barrier-opt.md)：`ExpandAtomicPseudoInsts`、`dbar`、`MemoryBarrierOpt`
- [Ch 22 DeadRegisterDefinitions 与寄存器信息](./22-dead-defs-and-register-info.md)：`$r0` 写、`eliminateFrameIndex`、spill/restore
- [Ch 23 调度模型与 llvm-mca](./23-scheduling-models-and-mca.md)：`SchedWrite`/`SchedRead`、LA464/LA664、资源压力
- [练习 D：用 MIR 驱动机器 pass](./practice-d-machine-passes.md)：`-run-pass` 实测 LateBranchOpt / DeadRegisterDefinitions / MergeBaseOffset，含手写 MIR 的四个坑

### Part 5 — Clang 与运行时（Ch 24–26 + 练习 E）

- [Ch 24 Clang Driver](./24-clang-driver.md)：`-march`/`-mabi`/`-mfpu`、默认 ABI、multilib、`--target`
- [Ch 25 Clang TargetInfo 与 Builtins](./25-clang-targetinfo-and-builtins.md)：宏、类型、LSX/LASX/LBT builtins、Sema 检查
- [Ch 26 compiler-rt 与运行时](./26-compiler-rt-and-runtimes.md)：builtins、sanitizer、xray、libunwind 的 LoongArch 支持面
- [练习 E：端到端工具链](./practice-e-clang-end-to-end.md)：clang → `.o` → `ld.lld` → qemu；TLS LE 从 3 条指令收缩到 1 条，LSX 向量真实运行

### Part 6 — 测试、调试与贡献（Ch 27–30 + Final）

- [Ch 27 lit 测试与 update 脚本](./27-lit-testing-and-update-scripts.md)：CodeGen/MC/LLD 三类测试、`update_llc_test_checks.py`、`yaml2obj`
- [Ch 28 调试 CodeGen](./28-debugging-codegen.md)：`-debug-pass=Structure`、`-stop-after`/`-run-pass`、`-print-after`、`-stats`、`-debug-only`、手写 MIR 四坑
- [Ch 29 上游贡献流程](./29-contribution-workflow.md)：issue → 复现 → 修复 → 测试 → PR → review，以 #239812 为例
- [Ch 30 维护者视角](./30-maintainership-and-triage.md)：review 关注点、回归策略、release notes、triage
- [Final Project](./final-project-loongarch-contribution.md)：独立完成一个可提交上游的 LoongArch 修复或小特性

---

## 实验环境

### 这棵树已经有什么

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git status -sb
"$LLVM_HOME"/toolset/status.sh
```

关键事实（2026-11-07 实测）：

- HEAD `01df7c365d7c`，分支 `fix/issue-239812-relax-marker-order`，相对 `upstream/main` 领先 1 条、落后 19 条。
- 构建目录 `builds/ct-instr-trunk` 原本只有 `X86;AArch64` 且没有 `lld`——**这棵树自己的分支改的是 lld，却跑不了 lld 测试**。本课把构建重配成：
  ```bash
  LLVM_TARGETS_TO_BUILD='X86;AArch64;LoongArch' \
  LLVM_ENABLE_PROJECTS='clang;lld' \
  ./toolset/configure.sh ct-instr-trunk
  ```
  之后 `llc --version` 能看到 `loongarch32/loongarch64`，`bin/ld.lld` 也可用。
- 本机另有一套交叉工具链可做“非 LLVM 生产者”的对照：`loongarch64-unknown-linux-gnu-gcc 16.2.0`、`qemu-loongarch64 8.2.2`。

### 每次开新终端的最小检查

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
T="$LLVM_HOME/builds/ct-instr-trunk"
$T/bin/llc --version | grep -i loongarch        # 必须有输出
$T/bin/llvm-config --targets-built              # X86 AArch64 LoongArch
ls -l $T/bin/ld.lld                              # -> lld
```

### 证据约定

- **实测**：在本机上述构建上真实跑出来的输出；命令和输出一起给。
- **源码依据**：对照本树的具体文件与符号；给行号或函数名。
- **未实测**：需要重编、需要真机、或需要上游特定 commit 才能验证的内容；明确标注，不伪造日志。

> 注意：`ct-instr-trunk` 是**当前任务自己的** worktree。按 `/home/ubuntu/admin/AGENTS.md` §1.1，本课不读取其它 `trees/<task>` 或 `builds/<task>`；所有结论只来自这棵树。

---

## 建议的读法

- **只想知道这棵树在干什么**：读 Ch 00，跳到 Ch 12 和 Ch 29。
- **想能改后端**：Part 0 → Part 1 → Part 3 → Part 4，练习 A/C/D 至少做两个。
- **想能改链接器**：Part 0 → Ch 11–12 → Ch 29，练习 B。
- **想能做完整 feature**：全课 + Final；Part 5 至少读 Ch 24–25。

每章结尾有“本章证据”和“下一步”，练习有可折叠的参考解答。Final 要求你交出一个 **PR-ready** 的 patch：代码、测试、release note（如适用）、以及一段能被 reviewer 接受的说明。

---

## 当前进度

- 2026-11-08：**全课完成**。README + Ch 00–30 + 练习 A–E + Final Project，共 38 篇、11,595 行。
- 2026-11-08 补强：31 章全部加了「延伸阅读」（每章 4 条，共 124 条，逐条说明读什么/学到什么/前提，路径均已校验存在）；5 个练习全部加了 `<details>` 可折叠参考解答（共 24 个），并修正了 Practice A 的玩具 opcode 与 Ch 04/D 的过时数值。
- Part 0（Ch 00–03）：树审计、后端全景、ISA/Subtarget/ABI、流水线与 MIR。
- Part 1（Ch 04–08 + 练习 A）：TableGen、寄存器/CC、MC 编码、asm 往返、常量物化。
- Part 2（Ch 09–12 + 练习 B）：CC lowering、栈帧、代码模型/TLS、lld relocation 与 relaxation。
- Part 3（Ch 13–17 + 练习 C）：合法化、选指 pattern、自定义 lowering/intrinsic、DAG combine、LSX/LASX。
- Part 4（Ch 18–23 + 练习 D）：伪指令三层、MergeBaseOffset/OptWInstrs、MachineCombiner/LateBranchOpt、原子/屏障、死定义、调度模型/llvm-mca。
- Part 5（Ch 24–26 + 练习 E）：clang driver、builtin/TargetInfo、compiler-rt 与 runtimes。
- Part 6（Ch 27–30 + Final）：lit/update 脚本、调试手册、#239812 贡献复盘、triage 与维护。
- 基线构建已补齐 `LoongArch` + `lld`；验证记录见 Ch 00。所有“实测”输出均来自本树与 `builds/ct-instr-trunk`；未执行的部分（如 compiler-rt 自建）在正文中显式标注“未实测”。
