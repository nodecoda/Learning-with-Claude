# Ch 19 — 机器层优化 pass：从 W 指令到压缩与基址合并

> **目标**：读懂 RISC-V 自己的机器层优化 pass——它们修什么、在 pipeline 的哪个位置跑、依赖哪些机器层事实；能对 `riscv-opt-w-instrs`、`riscv-dead-defs`、`riscv-merge-base-offset`、`riscv-fold-mem-offset`、`riscv-make-compressible`、`riscv-insert-read-write-csr`、`riscv-insert-write-vxrm`、`riscv-vmv0-elimination` 单独做 `-run-pass` 实验，并解释为什么这些事在 IR 层做不了。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`。本章所有“实测”均来自该构建；未跑过的明确标“未实测”。

> **前置**：读完 Ch 03（pass 顺序与 `-run-pass`）、Ch 10（`eliminateFrameIndex` 与栈帧边界）、Ch 18（`Pseudo*` 的三层展开）。本章不重复栈帧布局，只讲机器层折叠与改写。

---
## 19.1 为什么机器层优化值得读

IR 层看到 `add i32`、`load i32`、`call`；机器层看到另外四类事实：

| 机器层事实 | IR 为什么看不见 | 代表 pass |
|---|---|---|
| RV64 的 32 位运算要把 bit 31 符号扩展到 bit 63 | `i32` 没有“高 32 位 = bit 31”的契约 | `RISCVOptWInstrs` |
| 压缩指令只认 `x8–x15` 和 6 位立即数 | IR 没有物理寄存器编号 | `RISCVMakeCompressible`、AsmPrinter `compress` |
| `frm`/`vxrm`/`v0` 是隐式状态 | IR 不建模 CSR 与向量掩码寄存器 | `RISCVInsertReadWriteCSR`、`RISCVInsertWriteVXRM`、`RISCVVMV0Elimination` |
| `lui+addi`、`%lo`、`%stack.N` 是机器层地址 | IR 里只有 `@g` 和 `alloca` | `RISCVMergeBaseOffset`、`RISCVFoldMemOffset` |

这类 pass 是 `[RISCV]` patch 的高频区：一个 W 删除条件、一个 `rd=x0` 改写、一个压缩机会判定，都会同时影响 code size、正确性和大量 lit 测试。读它们的收益是**能判断一条改动应该发生在 IR、MachineSSA、PreRA 还是 PostRA**。

| pass 名（`-run-pass`） | 文件（行数） | 一句话职责 |
|---|---|---|
| `riscv-opt-w-instrs` | `RISCVOptWInstrs.cpp`（926） | 删冗余 `sext.w`，规范化 W/非 W 后缀 |
| `riscv-dead-defs` | `RISCVDeadRegisterDefinitions.cpp`（142） | 把 dead virtual def 改成 `x0` |
| `riscv-merge-base-offset` | `RISCVMergeBaseOffset.cpp`（824） | 把尾部偏移折进 `lui/auipc+addi` |
| `riscv-fold-mem-offset` | `RISCVFoldMemOffset.cpp`（309） | 把 `addi` 折进后续 load/store |
| `riscv-make-compressible` | `RISCVMakeCompressible.cpp`（557） | 为 load/store 换压缩寄存器/偏移 |
| `riscv-insert-read-write-csr` | `RISCVInsertReadWriteCSR.cpp`（204） | 在向量浮点指令前后保存/恢复 `frm` |
| `riscv-insert-write-vxrm` | `RISCVInsertWriteVXRM.cpp`（537） | 为定点向量指令插入 `vxrm` 写 |
| `riscv-vmv0-elimination` | `RISCVVMV0Elimination.cpp`（191） | 把 `vmv0` 改成 `$v0` 拷贝 |

第一个坑：**文件名和 pass 名不同**。`RISCVDeadRegisterDefinitions.cpp` 注册的是 `riscv-dead-defs`；`riscv-dead-register-definitions` 在本树会直接报 `run-pass ... is not registered`（19.4 有实测）。

---
## 19.2 先建立直觉：从 ISel 到 AsmPrinter 的 pass 顺序

按 `RISCVTargetMachine.cpp` 的 `add*Passes` 回调展开；`[O2]` 表示 `-O0` 跳过，`← 本章` 是主角。

```text
LLVM IR
  │ addIRPasses()                         RISCVTargetMachine.cpp:400
  │   AtomicExpand → RISCVZacasABIFix
  │   [O2] RISCVGatherScatterLowering → InterleavedAccess
  │        → RISCVCodeGenPrepare
  │   [O3] SelectOptimize
  ▼
SelectionDAG
  │ addPreISel()                          :419
  │   RISCVPromoteConstant → BarrierNoop → GlobalMerge
  │ addCodeGenPrepare()                   :443   TypePromotion
  │ addInstSelector()                     :449   RISCVISelDAG
  ▼
MachineInstr（虚拟寄存器，SSA）
  │ addMachineSSAOptimization()           :544
  │   EarlyMachineLICM → RISCVVLOptimizer → RISCVVectorPeephole
  │   → RISCVFoldMemOffset               ← 本章
  │   → 通用 MachineSSAOptimization（MachineCombiner 等）
  │   → RISCVOptWInstrs（仅 RV64）        ← 本章
  ▼
MachineInstr（PreRA，仍是 SSA）
  │ addPreRegAlloc()                      :568
  │   RISCVExpandPseudoPreRA
  │   [O2] RISCVMergeBaseOffset          ← 本章
  │   [O2] RISCVPreAllocZilsdOpt
  │   RISCVInsertReadWriteCSR            ← 本章
  │   RISCVInsertWriteVXRM               ← 本章
  │   RISCVLandingPadSetup
  │   RISCVVMV0Elimination               ← 本章
  ▼
RegAlloc + VirtRegRewriter + InsertVSETVLI
  │ addRegAssignAndRewriteOptimized()     :391
  │   [O2] RISCVDeadRegisterDefinitions  ← 本章
  ▼
MachineInstr（物理寄存器）
  │ addPostRegAlloc()                     :587   RISCVRedundantCopyElimination
  │ addPreSched2()                        :488
  │   RISCVExpandPseudoPostRA → KCFI → RISCVLoadStoreOpt
  │ addPreEmitPass()                      :497
  │   MachineCopyPropagation → RISCVLateBranchOpt
  │   → RISCVIndirectBranchTracking → BranchRelaxation
  │   → RISCVMakeCompressible            ← 本章
  │ addPreEmitPass2()                     :516
  │   RISCVMoveMerge → RISCVPushPopOpt → RISCVExpandPseudoPreEmit
  │   → [RV32] QCRelaxMarking → RISCVExpandPseudoAtomics
  ▼
AsmPrinter
  │ RISCVRVC::compress                     RISCVAsmPrinter.cpp:279
  ▼
MCInst → 编码 → ELF
```

用一行命令核对：

```bash
$T/bin/llc -mtriple=riscv64 -O2 -debug-pass=Structure /tmp/ch19/p.ll \
  -o /dev/null 2>&1 | head -1 | tr ' ' '\n' | grep riscv
```

---
## 19.3 `RISCVOptWInstrs`：W 指令的符号扩展账本

RV64 的 `ADDW/ADDIW/SUBW/SLLW/SRLW/SRAW/MULW/DIVW` 把结果截到 32 位，再把 bit 31 符号扩展到 bit 63；非 W 指令按 64 位算。于是同一个 `i32` 值可能“已符号扩展”或“只有低 32 位有意义”，这个 pass 就负责这两本账：

1. **删冗余 `sext.w`**：`ADDIW rd, rs1, 0` 如果结果本来就符号扩展，或所有使用者只关心低 32 位，就删掉。
2. **规范化 W 后缀**：所有使用者只依赖低 32 位时，`ADDW→ADD`、`ADDIW→ADDI`、`MULW→MUL`、`SLLIW→SLLI`、`SUBW→SUB`；若 subtarget 偏好 W（`preferWInst`），反向加 W。

核心判定：

| 函数/位置 | 作用 | 关键细节 |
|---|---|---|
| `hasAllNBitUsers`（:119） | worklist 检查所有使用者是否只要求低 N 位 | `ADD/ADDI/COPY/PHI` 传播；`SLLI` 加需求位数；`SRLI` 减；`SW/SH/SB` 只看 value 操作数 |
| `hasAllWUsers`（:374） | `hasAllNBitUsers(..., 32)` | W 删除的主判据 |
| `isSignExtendingOpW`（:381） | 判断输出是否天然满足 bit 63:32 = bit 31 | 先看 TableGen `IsSignExtendingOpWMask`；再特判 `SRAI imm>=32`、`SRLI imm>32`、`ADDI rd,x0,imm`、`ANDI`、`ORI`、`BSETI`、`COPY x0` |
| `isSignExtendedW`（:443） | 反向追源寄存器是否已符号扩展 | 可修复的 `ADDI/LD/LWU/MUL/SLLI/ADD/SUB` 记进 `FixableDefs` |
| `removeSExtWInstrs`（:740） | 找并删除 `ADDIW rd, rs1, 0` | 先 `constrainRegClass`，再 `replaceRegWith` 合并寄存器 |

真实替换表（`getWOp`，:717）：

```text
ADDI → ADDIW     ADD → ADDW      SUB → SUBW
MUL  → MULW      SLLI → SLLIW
LD   → LW        LWU → LW        LXD → LXW      LXWU → LXW
```

选项的真实名字在 `RISCVOptions.td:62-65`：`-riscv-sextw-removal=<bool>`、`-riscv-strip-w-suffix=<bool>`。没有 `-riscv-disable-sextw-removal`；关删除用 `-riscv-sextw-removal=0`，保留 W 后缀用 `-riscv-strip-w-suffix=0`。实测：

```bash
$T/bin/llc -mtriple=riscv64 -riscv-sextw-removal=0 \
  -run-pass=riscv-opt-w-instrs /tmp/ch19/sextw.mir -o - | grep -E 'ADDW|ADDIW'
```

```text
    %3:gpr = ADDIW %2, 0
```

### 19.3.1 实测 1：`ADDW` → `ADD`

输入 `/tmp/ch19/addw.mir`：

```yaml
%3:gpr = ADDW %1, %2
SW %3, %0, 0 :: (store (s32))
```

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs /tmp/ch19/addw.mir -o -
```

```yaml
%3:gpr = ADD %1, %2
SW %3, %0, 0 :: (store (s32))
```

唯一使用者是 `SW` 的 value 操作数，只需低 32 位，`ShouldStripW` 生效。

### 19.3.2 实测 2：删掉 `sext.w`

输入：

```yaml
%2:gpr = ADDW %0, %1
%3:gpr = ADDIW %2, 0
$x10 = COPY %3
```

输出：

```yaml
%2:gpr = ADDW %0, %1
$x10 = COPY %2
```

`ADDIW` 被删，`%3` 合并回 `%2`。两个条件同时成立：`ADDW` 本身 `IsSignExtendingOpW`；`ADDIW` 的使用者只要求低 32 位。

> 维护提示：删 `sext.w` 的前提是定义端已经保证符号扩展。把 IR 的 `sext` 改成 `trunc` 后，机器层可能不再生成 `ADDIW`，返回值高 32 位就是垃圾。

---
## 19.4 `RISCVDeadRegisterDefinitions`：把 dead def 改成 `x0`

pass 名 `riscv-dead-defs`，加在 `addRegAssignAndRewriteOptimized()`（`RISCVTargetMachine.cpp:391`）：**寄存器分配之后**、PostRA 之前。它只处理四类指令（`RISCVDeadRegisterDefinitions.cpp:60-80`）：`mayLoad()`、`mayStore()`、`hasUnmodeledSideEffects()`、`PseudoVSETVLI`/`PseudoVSETIVLI`。排除普通计算指令是因为某些 `rd=x0` 的 NOP 编码是保留 HINT。

改写条件（:82-102）：不是寄存器/不是 def/early-clobber 跳过；`isRegTiedToUseOperand(I)` 跳过；不是 virtual 或不是 `MO.isDead()` 跳过；`TII->getRegClass(Desc, I)` 不含 `X0/X0_W/X0_H/X0_Pair` 跳过。满足后 `LIS.removeInterval(Reg); MO.setReg(X0Reg);`。

**本树没有 `hasDeadRegisterDefinitions` 这个 hook**；`grep -rn hasDeadRegisterDefinitions llvm/lib/Target/RISCV` 为空。真正决定能否写 `x0` 的是 `getRegClass` 返回的寄存器类是否含 `X0` 家族（:93-102）。别把别的 target 或旧版本的概念套过来。

`rd=x0` 在 RISC-V 编码里合法：load 的 `rd=x0` 是 HINT；LLVM 在这里假设 load 仍会访问内存（volatile 实测生成 `lw zero, 0(a0)`），只是结果丢弃。但 `x0` 不是压缩寄存器——压缩 load 的 `rd'` 只能编码 `x8–x15`，所以这个改写会**主动放弃**该 load 的压缩机会。实测：

```bash
printf 'lw x0,0(a0)\nlw a1,0(a0)\n' | \
  $T/bin/llvm-mc -triple=riscv64 -mattr=+c -show-encoding
```

```text
lw zero, 0(a0)     # encoding: [0x03,0x20,0x05,0x00]   4 字节
lw a1, 0(a0)       # encoding: [0x0c,0x41]             2 字节
```

所以它是“dead def 不占寄存器、编码合法”的清理，不是 code-size pass；和 `riscv-make-compressible` 在目标上有张力。

实测：

```yaml
dead %1:gpr = LD %0, 0 :: (load (s64))
```

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-dead-defs /tmp/ch19/dead.mir -o -
```

```yaml
dead $x0 = LD %0, 0 :: (load (s64))
```

写成 `-run-pass=riscv-dead-register-definitions` 会报：

```text
llc: error: run-pass riscv-dead-register-definitions is not registered.
```

可用 `-riscv-enable-dead-defs=0` 关闭（`RISCVOptions.td:97`）。

---
## 19.5 地址折叠两兄弟：`MergeBaseOffset` 与 `FoldMemOffset`

### 19.5.1 先纠正误解：`MergeBaseOffset` 不处理 `%stack`

它处理全局地址 lowering 序列：

```text
medlow:   lui   vreg1, %hi(s)
          addi  vreg2, vreg1, %lo(s)
medany:   auipc vreg1, %pcrel_hi(s)
          addi  vreg2, vreg1, %pcrel_lo(.Lpcrel_hi1)
```

以及 `PseudoMovAddr`、`QC_E_LI`。`detectFoldable`（:77）要求：Hi 只有 ADDI 一个使用者；地址操作数是 global/CPI/blockaddress；offset 为 0；target flags 是 `MO_HI`/`MO_LO` 或 `MO_PCREL_HI`/`MO_PCREL_LO`。

`foldIntoMemoryOps` 有两处明确拒绝（:469、:492）：`if (UseMI.getOperand(1).isFI()) return false;`。**带 frame index 的 load/store 直接不折**；`%stack.N` 的落地是 Ch 10 的 `eliminateFrameIndex` 与 `LocalStackSlotAllocation` 的职责。

三条折叠路径：

| 函数 | 输入形态 | 结果 |
|---|---|---|
| `detectAndFoldOffset`（:331） | `LUI+ADDI` 后跟 `ADDI`/`ADD`/`SHxADD` | 把尾部算术偏移折进 Hi/Lo，删尾部算术 |
| `foldIntoMemoryOps`（:418） | `LUI+ADDI` 结果只被同偏移 load/store 当基址 | 改 `lui %hi(g+8)`，删 `addi`，mem op 用 `%lo(g+8)` |
| `foldShxaddIntoScaledMemory`（:674） | `SHxADD` + 基址 + load/store | 生成 `QC_LR*`/`QC_SR*` 缩放 load/store（源码依据，未实测） |

实测 `/tmp/ch19/merge.mir`：

```yaml
%0:gpr = LUI target-flags(riscv-hi) @g
%1:gpr = ADDI %0, target-flags(riscv-lo) @g
%2:gpr = LW %1, 8 :: (load (s32) from @g + 8)
```

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-merge-base-offset /tmp/ch19/merge.mir -o -
```

```yaml
%0:gpr = LUI target-flags(riscv-hi) @g + 8
%2:gpr = LW %0, target-flags(riscv-lo) @g + 8 :: (load (s32) from @g + 8)
```

`LUI` offset 变成 `+8`，`ADDI` 消失，`LW` 基址直接是 `%0`，`%lo` 也带 `+8`。

### 19.5.2 `FoldMemOffset`：把 `addi` 折进 mem offset

它跑在 `addMachineSSAOptimization()`（`RISCVTargetMachine.cpp:559`），也在 RA 之前。条件：函数不能是 `optsize`（:246，怕增加尺寸/破坏压缩）；只看 `ADDI rd, rs1, imm` 且 `rs1 != x0`（:256）；`ADDI` 与最终 load/store 之间至少有一条算术指令（:216）；只支持 `ADD`、`SH1ADD/SH2ADD/SH3ADD`、`SLLI` 等可重结合运算；合并后的 mem offset 必须仍是 `simm12`。

实测 `/tmp/ch19/fold2.mir`：

```yaml
%1:gpr = ADDI %0, 4
%2:gpr = ADD %1, %5
%3:gpr = LW %2, 8 :: (load (s32))
```

```bash
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-fold-mem-offset /tmp/ch19/fold2.mir -o -
```

```yaml
%3:gpr = ADD %0, %1
%4:gpr = LW %3, 12 :: (load (s32))
```

`ADDI` 被删，`4+8` 合到 `LW` 的 `12`。

### 19.5.3 与 Ch 10 的边界

| 事情 | 发生在 | 谁做 |
|---|---|---|
| `%stack.N` → `sp/s0 + 偏移` | `prolog-epilog` 前后的 frame index elimination | `RISCVRegisterInfo::eliminateFrameIndex`（Ch 10） |
| 超出 `simm12` 的帧偏移建 base register | frame lowering / local stack allocation | `needsFrameBaseReg`、`materializeFrameBaseRegister`（Ch 10） |
| 全局地址尾部算术折进 `lui/auipc+addi` | PreRA，SSA | `RISCVMergeBaseOffset` |
| `addi` 折进 load/store 机器偏移 | MachineSSA，RA 之前 | `RISCVFoldMemOffset` |

一句话：Ch 10 解决“栈对象怎么落到 `sp/s0`”，本章解决“已落地的机器地址还能不能再少一条指令”；不要混在同一个 patch 里改。

---
## 19.6 `RISCVMakeCompressible`：让 load/store 够得着 `c.*`

`RISCVMakeCompressible.cpp:9-55` 的头注释给出两个场景：**寄存器不可压缩**（`sw zero, 0(a0)` 的 `zero` 进不了 `c.sw` 的 3 位字段，找 `x8–x15` 拷过去换 use）；**偏移太大**（`sw a1, -236(a2)` 超出 `c.sw` 范围，找可压缩寄存器做 `new = base + adjust`）。

它只处理 load/store，不处理 `addi`；真正的 `addi a0,a0,1 → c.addi` 在 AsmPrinter/MC 层（19.6.1）。

`getRegImmPairPreventingCompression`（:297）先判断缺什么：

```text
{Reg, 0}  → 只需把 Reg 换成压缩寄存器
{Reg, N}  → 换寄存器，并把新基址调整 N
{NoRegister, 0} → 没救
```

`analyzeCompressibleUses`（:360）向后扫描，收集同一次替换影响的 mem op，并做成本判断（:407-410）：

```cpp
unsigned BaseCost = XqciloLdSt ? 2 : 3;
unsigned CopyCost = RISCV::GPRPairRegClass.contains(RegImm.Reg) ? 2 : 1;
if (MIs.size() <= CopyCost || (RegImm.Imm != 0 && MIs.size() < BaseCost))
  return Register();
```

不需要调整基址时至少 2 条 use 才值得插一条 `c.mv`/`c.li`；要调整基址时至少 3 条（GPR pair 拷贝成本 2）。可用寄存器用 `RegScavenger::scavengeRegisterBackwards` 从 `GPRCRegClass` 找（:419），即 `x8–x15`。pass 有两个硬门槛（:481-491）：`hasOptSize()` 且 `hasStdExtZca()`。

实测 `/tmp/ch19/comp3.mir`（`optsize` + 物理基址 `$x10`）：

```yaml
SW $x0, $x10, 0
SW $x0, $x10, 4
SW $x0, $x10, 8
SW $x0, $x10, 12
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+c -run-pass=riscv-make-compressible \
  /tmp/ch19/comp3.mir -o -
```

```yaml
$x11 = ADDI $x0, 0
SW $x11, $x10, 0
SW $x11, $x10, 4
SW $x11, $x10, 8
SW $x11, $x10, 12
```

`$x11` 在 `x8–x15` 内；`ADDI $x11, $x0, 0` 就是 `li a1, 0`，之后四条 `SW` 会编码成 `c.sw`。

### 19.6.1 `addi` 的压缩在 AsmPrinter

`addi a0,a0,1` 能否变 `c.addi`，判定在 `RISCVAsmPrinter.cpp:279` 的 `RISCVRVC::compress`（实现 `RISCVBaseInfo.cpp:184`，由 `RISCVGenCompressInstEmitter.inc` 生成匹配代码）。`RISCVInstrInfo::getInstSizeInBytes`（:2090）调用同一个生成的 `getCompressedSize`（:2105/:2116），所以“指令大小”和“最终编码”用同一套规则。

`llvm-mc -show-encoding` 实测：

```text
addi a0, a0, 1     # [0x05,0x05]              2 字节
addi a0, a1, 1     # [0x13,0x85,0x15,0x00]   4 字节
addi s0, s0, 1     # [0x05,0x04]              2 字节
addi a0, a0, 100   # [0x13,0x05,0x45,0x06]   4 字节
addi a0, a0, -32   # [0x01,0x15]              2 字节
addi a0, a0, 32    # [0x13,0x05,0x05,0x02]   4 字节
addiw a0, a0, 1    # [0x05,0x25]              2 字节
addiw a0, a1, 1    # [0x1b,0x85,0x15,0x00]   4 字节
```

规则：`rd == rs1` 且都在 `x8–x15`；立即数在 `[-32,31]`。这也解释了 `int f(int a){return a+1;}` 在 `-O2` 下是 `addiw a0,a0,1; ret`（4 字节），而 `int g(int a,int b){return b+1;}` 是 `addiw a0,a1,1; ret`（6 字节）：前者压缩，后者不压缩。

---
## 19.7 隐式状态：`frm`、`vxrm` 与 `v0`

### 19.7.1 `RISCVInsertReadWriteCSR`：`frm` 的保存/恢复

浮点舍入模式在 `fcsr.frm` 里。指令可带静态模式（`rne/rtz/rdn/rup/rmm`），也可用 `dyn`（7）表示“读 `frm`”。IR 没有 `frm`，必须由机器层插 `fsrmi`/`fsrm`。

`RISCVInsertReadWriteCSR.cpp` 遍历带 `FRM` 操作数的指令（`RISCVII::getFRMOpNum`）：静态模式在指令前 `SwapFRMImm`（保存旧值并写新值）、指令后 `WriteFRM` 恢复；`dyn` 不插入，只给指令加 `implicit $frm`；遇到 call、inline asm、或显式读 `frm` 的指令先恢复 `SavedFRM`。`frm_insert_opt=true`（默认）时同块只在第一次/最后一次真正需要时保存/恢复（`emitWriteRoundingModeOpt`，:57）；关掉后退化成每条一对（`emitWriteRoundingMode`，:136，未实测）。

实测（从 `frm-insert.ll` 取 MIR）：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -target-abi=lp64d \
  -stop-after=finalize-isel frm-insert.ll -o frm-isel.mir
$T/bin/llc -mtriple=riscv64 -mattr=+v -target-abi=lp64d \
  -run-pass=riscv-insert-read-write-csr frm-isel.mir -o - | grep -A6 'name:            test$'
```

```yaml
%5:gpr = SwapFRMImm 0, implicit-def $frm, implicit $frm
%3:vr = nofpexcept PseudoVFADD_VV_MF2_E32 $noreg, %0, %1, 0 /* frm=rne */, ... implicit $frm
%4:vr = nofpexcept PseudoVFADD_VV_MF2_E32 $noreg, %3, %3, 0 /* frm=rne */, ... implicit $frm
WriteFRM %5, implicit-def $frm
```

两条 `PseudoVFADD` 共享一次 `SwapFRMImm`/`WriteFRM`，这就是 `frm_insert_opt` 的收益。

### 19.7.2 `RISCVInsertWriteVXRM`：定点舍入模式

`vxrm` 是 2 位 CSR，控制 `vaadd`、`vasub`、`vnclip` 等定点向量指令的舍入。每条指令在 MIR 里带一个 `vxrm` 立即数操作数；pass 要在需要的地方写 `csrwi vxrm, N`，但不重复写。

算法是两个数据流：`computeAvailable`（:272）前向算块入口可用值；`computeAnticipated`（:314）后向算块入口将来需要的值；`emitWriteVXRM`（:370）在 anticipated 但 not available 的位置插 `WriteVXRMImm`；遇到 call、inline asm、显式改 `vxrm` 的指令状态变 `Unknown`。源码明确说不切 critical edge（:20-21），所以某些 CFG 仍有冗余写（源码依据，未实测）。

实测：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -run-pass=riscv-insert-write-vxrm \
  llvm/test/CodeGen/RISCV/rvv/vxrm.mir -o -
```

```yaml
WriteVXRMImm 0, implicit-def $vxrm
%3:vr = PseudoVAADD_VV_MF8 undef $noreg, %0, %1, 0 /* vxrm=rnu */, ... implicit $vxrm
```

对应汇编是 `csrwi vxrm, 0`。

### 19.7.3 `RISCVVMV0Elimination`：把 `vmv0` 变成 `$v0` 拷贝

向量指令的 mask 必须在 `v0`。ISel 先把 mask 选成 `vmv0` singleton 寄存器类，而不是立刻拷到 `$v0`，这样寄存器合并更容易。但 coalescing 可能把多个 copy 合成同一条指令里的多个 `vmv0` use，寄存器分配器无法分配：

```yaml
%x:vrnov0 = PseudoVADD_VV_M1_MASK %0:vrnov0, %1:vr, %2:vmv0, %3:vmv0, ...
```

pass 在 RA 之前把每个 `vmv0` use 换成 `$v0 = COPY src`：

```yaml
$v0 = COPY %3:vr
%x:vrnov0 = PseudoVADD_VV_M1_MASK %0:vrnov0, %1:vr, %2:vr, $v0, ...
```

实测：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -run-pass=riscv-vmv0-elimination \
  llvm/test/CodeGen/RISCV/rvv/vmv0-elimination.mir -o -
```

```yaml
$v0 = COPY %x
PseudoVSE8_V_M1_MASK $noreg, $noreg, $v0, -1 /* vl=VLMAX */, 3 /* e8 */
```

唯一保留的例外是 inline asm 的 `vm` constraint（`RISCVVMV0Elimination.cpp:13-15`，源码依据，未实测），因为 asm 的寄存器约束不能在 MIR 里重算。

### 19.7.4 为什么必须在机器层

`frm`、`vxrm`、`v0` 都是隐式状态：IR 没有“这条 `fadd` 用哪个舍入模式”的类型，没有“mask 必须在 `v0`”的约束，inline asm 还能任意读写这些状态。因此这三类 pass 只能放机器层，且必须在 RA 之前（`v0`/`frm` 需要虚拟寄存器）或紧邻 RA（`vxrm` 数据流需要 CFG 但不需要物理寄存器）。提前到 IR 会丢约束，拖到 PostRA 会失去插虚拟寄存器的机会。

---
## 19.8 源码落点

| 主题 | 文件与行号 | 读什么 |
|---|---|---|
| pass 顺序 | `RISCVTargetMachine.cpp:400-597` | 本章 ASCII 图的唯一来源；加 pass 改这里 |
| W 指令 | `RISCVOptWInstrs.cpp:374-443`、`:717-930` | 判定、替换、两个 run 阶段 |
| W 选项 | `RISCVOptions.td:62-65` | `riscv-sextw-removal`、`riscv-strip-w-suffix` |
| dead def | `RISCVDeadRegisterDefinitions.cpp:60-120` | 四类过滤、tied/early-clobber、`X0` 家族 |
| 基址合并 | `RISCVMergeBaseOffset.cpp:77-141`、`:331-418`、`:787` | `detectFoldable`、`foldOffset`、`foldIntoMemoryOps` |
| mem offset | `RISCVFoldMemOffset.cpp:98-290` | worklist、optsize 拒绝、simm12 |
| 压缩机会 | `RISCVMakeCompressible.cpp:210-490` | `isCompressedReg`、成本模型、`GPRCRegClass` |
| 实际编码 | `RISCVAsmPrinter.cpp:279`、`MCTargetDesc/RISCVBaseInfo.cpp:184`、`RISCVInstrInfo.cpp:2090` | `RISCVRVC::compress`、`getInstSizeInBytes`、`getCompressedSize` |
| `frm` | `RISCVInsertReadWriteCSR.cpp:57-180` | `SwapFRMImm`/`WriteFRM`、call/inline asm 恢复 |
| `vxrm` | `RISCVInsertWriteVXRM.cpp:242-470` | available/anticipated、`WriteVXRMImm` |
| `v0` | `RISCVVMV0Elimination.cpp:83-180` | `vmv0` 判定、`$v0 = COPY`、`recomputeRegClass` |

---
## 19.9 可跑实验：9 条 `-run-pass` 命令

实验文件放 `/tmp/ch19`；仓库里的对应测试见 19.13。每个命令都能独立跑，不需要完整 pipeline。`-help-hidden` 不会列出这些机器 pass 名，直接跑 `-run-pass=<name>` 才知道是否注册。

9 条命令覆盖 8 个 pass：`addw.mir` 与 `sextw.mir` 都跑 `riscv-opt-w-instrs`；其余依次是 `riscv-dead-defs`、`riscv-merge-base-offset`、`riscv-fold-mem-offset`、`riscv-make-compressible`、`riscv-insert-read-write-csr`、`riscv-insert-write-vxrm`、`riscv-vmv0-elimination`。

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
S=/home/ubuntu/llvm/trees/ct-instr-trunk

$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs /tmp/ch19/addw.mir -o -
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-opt-w-instrs /tmp/ch19/sextw.mir -o -
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-dead-defs /tmp/ch19/dead.mir -o -
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-merge-base-offset /tmp/ch19/merge.mir -o -
$T/bin/llc -mtriple=riscv64 -run-pass=riscv-fold-mem-offset /tmp/ch19/fold2.mir -o -
$T/bin/llc -mtriple=riscv64 -mattr=+c -run-pass=riscv-make-compressible /tmp/ch19/comp3.mir -o -
$T/bin/llc -mtriple=riscv64 -mattr=+v -target-abi=lp64d \
  -run-pass=riscv-insert-read-write-csr /tmp/ch19/frm-isel.mir -o - | grep -A6 'name:            test$'
$T/bin/llc -mtriple=riscv64 -mattr=+v -run-pass=riscv-insert-write-vxrm \
  $S/llvm/test/CodeGen/RISCV/rvv/vxrm.mir -o -
$T/bin/llc -mtriple=riscv64 -mattr=+v -run-pass=riscv-vmv0-elimination \
  $S/llvm/test/CodeGen/RISCV/rvv/vmv0-elimination.mir -o -
```

从真实 IR 取 MIR：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=finalize-isel \
  -simplify-mir input.ll -o input.mir
```

`-simplify-mir` 不是硬性要求，但是好习惯：它去掉/规范化一些只在完整 pipeline 里有意义的字段，减少“这份 MIR 只能被某个 pass 解析”的意外。

---
## 19.10 踩雷集锦

1. **删 `sext.w` 破坏符号扩展契约。** `ADDIW rd,rs1,0` 不只是复制低 32 位，它同时建立“高 32 位 = bit 31”的契约。删除前提是定义端已保证，或所有使用者只读低 32 位。把 IR 的 `sext` 改成 `trunc` 后，机器层可能不再生成 `ADDIW`，返回值高 32 位就是垃圾。
2. **`rd=x0` 改写与 verifier。** pass 只改 virtual、dead、非 tied、非 early-clobber 的 def；手写 MIR 漏 `dead` 标记就不会改。改后要同步 `LiveIntervals`（源码 `LIS.removeInterval`），否则后续 pass 看到不存在的活跃区间。pass 名是 `riscv-dead-defs`，写 `riscv-dead-register-definitions` 会直接失败。
3. **压缩改变分支距离。** `BranchRelaxation` 在 `addPreEmitPass` 里先跑，`RISCVMakeCompressible` 紧随其后，真正 `c.*` 编码在 AsmPrinter。任何在 BranchRelaxation 之后改变指令字节数的 pass 都必须保证分支距离仍成立；`-run-pass` 单跑没有 BranchRelaxation 兜底，MIR 偏移和最终汇编可能对不上（未构造回归，未实测）。
4. **`-run-pass` 需要 `-simplify-mir` 的习惯。** 本树 parser 能接受 `varArgsSaveSize` 等字段，但 `-stop-after` 输出仍可能带 IR block 标注、完整寄存器类、只在完整 pipeline 有意义的属性。先 `-simplify-mir` 再单跑，能少踩“未知字段/未定义 IR block”的坑。
5. **pass 顺序敏感：同一 MIR 跑两次结果不同。** `riscv-fold-mem-offset` 只对当前 MBB 做一遍线性扫描。对 `ADDI → ADDI → ADD → LW`，第一次只折后一个 `ADDI`，第二次才折前一个：
   ```text
   第一次: ADDI %0,4 ; ADD %2,%1 ; LW %4,8
   第二次: ADD %0,%1 ; LW %4,12
   ```
   这不是 bug，但说明“单跑一次 pass”不等于“pipeline 跑一次”的最终形态；定位回归要在完整 pipeline 里看。
6. **CSR pass 与 inline asm 的交互。** `RISCVInsertReadWriteCSR` 把 `MI.isInlineAsm()` 当成未知操作，会先恢复 `frm`；`RISCVInsertWriteVXRM` 同样把 inline asm 标成 `Unknown`。手写 MIR 的 inline asm 若实际读了 `frm`/`vxrm` 却没声明，pass 看不见，代码可能用错舍入模式。
7. **`RISCVMergeBaseOffset` 只对特定 pattern 生效。** 要求 `lui`/`auipc` 地址操作数 offset 为 0、Hi 只有一个使用者、Lo 是 `ADDI` 且 target flags 匹配。`%stack.N` 的 load/store 被 `isFI()` 明确拒绝；想把栈偏移折进 `lui` 是找错了 pass。
8. **`riscv-opt-w-instrs` 只在 RV64 跑。** `run()` 第一行是 `if (!ST.is64Bit()) return false;`。RV32 上跑不报错但什么也不做；不要用 RV32 MIR 验证 W 指令行为。相关开关 `-riscv-enable-dead-defs=0` 关掉后 `dead $x0` 不会出现。

---
## 19.11 本章重点整理

- 机器层优化处理 IR 看不见的事实：W 指令的符号扩展、物理寄存器编号、压缩编码、`frm`/`vxrm`/`v0` 隐式状态、全局地址 lowering 序列。
- `RISCVOptWInstrs` 两阶段：`removeSExtWInstrs` 删 `ADDIW rd,rs1,0`；`canonicalizeWSuffixes` 在 W/非 W 间规范化。核心是 `hasAllWUsers` 与 `isSignExtendingOpW`。
- `RISCVDeadRegisterDefinitions`（`riscv-dead-defs`）在 RA 之后把 dead virtual def 改成 `x0`；没有 `hasDeadRegisterDefinitions` hook，能否写 `x0` 由寄存器类决定。
- `RISCVMergeBaseOffset` 折 `lui/auipc+addi` 全局地址序列，`isFI()` 明确拒绝栈对象；`RISCVFoldMemOffset` 折 `addi` 到后续 mem offset，两者都在 RA 之前。
- `RISCVMakeCompressible` 只在 `optsize` + Zca 下跑，只处理 load/store；`addi → c.addi` 的判定在 AsmPrinter 的 `RISCVRVC::compress`，规则是 `rd==rs1`、`x8–x15`、`[-32,31]`。
- `frm`/`vxrm`/`v0` 三个 pass 都在 RA 前后处理隐式状态：`SwapFRMImm`/`WriteFRM`、`WriteVXRMImm`、`$v0 = COPY`。
- 判断优化该放哪层：能改 IR 语义的在 IR；需要虚拟寄存器/SSA 的在 MachineSSA；需要物理寄存器/压缩编码的在 PostRA 或 AsmPrinter。

---
## 19.12 自我检核

- [ ] 能画出 `addMachineSSAOptimization` → `addPreRegAlloc` → RA → `addPreEmitPass` 的顺序，并指出本章 8 个 pass 各自的位置
- [ ] 能解释 `hasAllWUsers` 为什么不是“所有使用者都是 W 指令”，而是“所有使用者只要求低 32 位”
- [ ] 能说出 `isSignExtendingOpW` 至少三个特判（`SRAI`、`ADDI x0`、`ANDI`）及其理由
- [ ] 能解释 `ADDIW rd,rs1,0` 为什么可以删，以及删错会导致什么
- [ ] 能说明 `rd=x0` 对 load 的语义、对压缩编码的影响，以及本树为什么没有 `hasDeadRegisterDefinitions`
- [ ] 能区分 `RISCVMergeBaseOffset`（全局地址）与 `RISCVFoldMemOffset`（mem offset），并指出前者对 `isFI()` 的处理
- [ ] 能解释 `RISCVMakeCompressible` 的成本模型（`BaseCost=3`、`CopyCost=1`）和 `x8–x15` 的来源
- [ ] 能说出 `frm`、`vxrm`、`v0` 为什么必须在机器层处理，以及三个 pass 各自插入什么指令
- [ ] 能对一个 `-run-pass` 失败给出至少三种排查方向（pass 名、`-simplify-mir`、所需 analyses/属性）

---
## 19.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVOptWInstrs.cpp`**：374–443（判定）、717–930（替换与两个 run 阶段）；改 `sext.w` 删除条件前必读。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVDeadRegisterDefinitions.cpp`**：60–120；四类过滤、tied/early-clobber、`X0` 家族，也是“文件名 ≠ pass 名”的样本。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVMergeBaseOffset.cpp`** 与 **`RISCVFoldMemOffset.cpp`**：Merge 的 77–141、331–418、787；Fold 的 98–290；两种地址折叠粒度与 Ch 10 边界。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVMakeCompressible.cpp`**：210–490；压缩机会、成本模型、`GPRCRegClass`；配合 `RISCVAsmPrinter.cpp:279` 看真正编码。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInsertReadWriteCSR.cpp`**、**`RISCVInsertWriteVXRM.cpp`**、**`RISCVVMV0Elimination.cpp`**：CSR 57–180、VXRM 242–470、VMV0 83–180；三个隐式状态的完整策略。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/opt-w-instrs.mir`**、**`sextw-removal.ll`**：`RUN` 与 `CHECK` 的 before/after，`-riscv-sextw-removal=0` 的对照也在这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/make-compressible.mir`**、**`rvv/vxrm.mir`**、**`rvv/vmv0-elimination.mir`**：本章三个 PostRA/PreRA pass 的最小可跑样本。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/merge-base-offset-debug-only.mir`**、**`fold-mem-offset.mir`**：地址折叠边界（debug、FI、offset 0）；**`llvm/docs/MIRLangRef.md`**：Machine Function 字段与 `dead`/`implicit`/`renamable`。

---
→ 下一章：[Ch 20 — 原子操作与内存屏障](./20-atomics-and-memory-barrier.md)
