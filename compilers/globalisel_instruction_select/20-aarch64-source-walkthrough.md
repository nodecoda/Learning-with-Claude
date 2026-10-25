# 第 20 章 AArch64 源码全走读

> **本章目标**：把前 19 章落到 AArch64 真实源码：知道 `select()` 的完整控制流，知道 `G_CONSTANT`、`G_SELECT`、`G_LOAD/G_STORE`、`G_BRCOND`、`G_PHI`、`G_INTRINSIC` 从哪一行进入、经过哪个 helper、最后落到哪条指令；同时能读 TableGen 侧和生成物。
>
> **前置**：第 2、5–10、13、17、18 章。**环境**：LLVM 23.1.4。行号以 `llvmorg-23.1.4` 的 `AArch64InstructionSelector.cpp`（8248 行）为准；本章 MIR/asm 都在本机 23.1.4 预编译包上实测。

---

## 20.1 文件地图：谁负责什么

| 文件 | 23.1.4 规模 | 职责 | 关注点 |
| --- | --- | --- | --- |
| `AArch64InstructionSelector.cpp` | 8248 行 | selector C++ 主体 | `select()`、`preISelLower()`、`earlySelect()`、各 `select*` |
| `AArch64InstrGISel.td` | 590 行 | GlobalISel 专用 pseudo | `G_ADD_LOW`、`G_REV32`、`G_DUP`、`GINodeEquiv` |
| `AArch64InstrInfo.td` | 11993 行 | 指令 pattern、feature predicate | `MOVi32imm`、`ADD`、`CSEL`、`HasNEON` |
| `AArch64InstrFormats.td` | 13644 行 | 指令格式、`ComplexPattern`、立即数 | `AddSubImmShift`、`am_indexed64` |
| `AArch64RegisterBankInfo.cpp` | 1489 行 | GPR/FPR bank mapping | `getInstrMapping()`、`getInstrAlternativeMappings()` |
| `AArch64LegalizerInfo.cpp` | 2793 行 | legal/custom/lower 规则 | `G_LOAD`/`G_STORE` 的合法形态 |
| `AArch64CallLowering.cpp` | 1543 行 | 调用约定 lowering | `lowerReturn()`、`lowerFormalArguments()`、`lowerCall()` |
| `AArch64GenGlobalISel.inc` | 生成物 | `selectImpl()`、matcher table | 不手改，用来对照生成结果 |

生成物由 `llvm-tblgen -gen-global-isel` 产出，核心只有：

```cpp
bool AArch64InstructionSelector::selectImpl(MachineInstr &I,
                                            CodeGenCoverage &CoverageInfo) const;
```

声明在 selector 第 97 行，实现在 `.inc`。整条链是：

```text
select()
  ├─ preISelLower()   前置改写
  ├─ earlySelect()    优先于 importer 的手写特例
  ├─ selectImpl()     TableGen matcher table
  └─ switch (Opcode)  C++ 兜底
```

`.inc` 通过宏分段包含：`GET_GLOBALISEL_PREDICATE_BITSET`（65–67）、`PREDICATES_DECL`（547–549）、`TEMPORARIES_DECL`（553–555）、`IMPL`（560–562）。宏名就是“读哪一段”。

---

## 20.2 `select()` 的完整控制流

入口在 `AArch64InstructionSelector.cpp:2562`。按顺序拆：

### 20.2.1 `setupMF()` 与 `processPHIs()`

`setupMF()`（79–92）在 `select()` 之前运行：设置 `MIB`、缓存 `ProduceNonFlagSettingCondBr`、调用 `processPHIs(MF)`（91）。`processPHIs()`（8183）扫描所有 `G_PHI`，如果操作数混有 GPR/FPR bank，就用 `fixupPHIOpBanks()`（8150）插入跨 bank `COPY`。所以 `G_PHI` 的选择逻辑不需要自己处理 bank 不均。

### 20.2.2 非 generic 指令与 `G_PHI`

```cpp
if (!I.isPreISelOpcode() || Opcode == TargetOpcode::G_PHI) { ... }
```

第 2581 行。分支内：

- `LOAD_STACK_GUARD`：constrain 后返回（2584–2587）；
- `PHI`/`G_PHI`：取 def 的 regclass，或按 bank/type 查，设置成 `PHI` 并 constrain（2589–2613）；
- `COPY`：`selectCopy()`（2616）；
- debug 指令：`selectDebugInstr()`（2619）；
- 其他已是目标指令：直接 true（2622）。

`G_PHI` 虽然是 generic opcode，但处理方式和 `PHI` 一样，所以放在这里，不走 matcher。

### 20.2.3 隐式操作数检查

```cpp
if (I.getNumOperands() != I.getNumExplicitOperands())
  return false;   // 2626–2630
```

generic 指令不允许带额外隐式操作数；带了就直接拒绝。这是第 18 章 verifier 问题的一个上游来源。

### 20.2.4 `preISelLower()`

```cpp
if (preISelLower(I))
  Opcode = I.getOpcode();   // 2635–2637
```

`preISelLower()`（2137）是选择前改写，不是优化：

- `G_CONSTANT` 指针类型 → 整数（2143–2153）；
- `G_STORE` 跨 bank 标量 → 折掉 `COPY`（2155–2168，`contractCrossBankCopyIntoStore()`）；
- `G_PTR_ADD` → `G_ADD`（2170–2176，`convertPtrAddToAdd()`）；
- `G_LOAD` 指针结果 → `s64`（2178–2188）；
- `G_DUP` 指针向量 → 整数向量（2190–2200）；
- `G_INSERT_VECTOR_ELT` 指针/小整数 → 改写（2202–2228）；
- `G_UITOFP`/`G_SITOFP` 的 FPR→FPR 特例（2230 起）。

返回 true 表示“我改了 MIR”，`select()` 刷新 opcode 后继续，不直接返回。

### 20.2.5 `earlySelect()`

```cpp
if (earlySelect(I)) return true;
if (selectImpl(I, *CoverageInfo)) return true;
```

第 2644–2648 行。`earlySelect()`（2389）优先于 importer，防止 importer 先选出一个次优序列：

- `AArch64::G_DUP`：常量 splat 走 `emitConstantVector`（2398–2417）；
- `G_SEXT`：先试 `selectUSMovFromExtend()`，选 `SMOV`（2418–2423）；
- `G_BR`：false（2424）；
- `G_SHL`：`earlySelectSHL()`（2426）；
- `G_CONSTANT` 值为 0：改成 `COPY $xzr/$wzr`（2428–2450）；
- `G_ADD` 被 `G_ICMP` 喂养：尝试折成 `cinc`（2453 起）。

`G_CONSTANT 0` 在这里被截走，永远不走 `selectImpl()`。

### 20.2.6 `selectImpl()` 与 C++ switch

`selectImpl()`（2647）是 matcher table 入口：收集 feature，把当前指令放进 `State.MIs[0]`，交给 `executeMatchTable()`。命中返回 true；不命中继续走第 2653 行的 `switch (Opcode)`。

C++ switch 覆盖 importer 表达不了的情况：`G_SBFX/G_UBFX`（2654）、`G_BRCOND`（2678）、`G_BRINDIRECT`（2681）、`G_BRJT`（2698）、`G_ADD_LOW`（2701）、`G_FCONSTANT`（2727）、`G_GLOBAL_VALUE`（2909）、`G_LOAD/G_STORE`（2955–3132）、`G_SELECT`（3475）、`G_ICMP/G_FCMP`（3495–3525）、`G_INTRINSIC`（3529–3532）、`G_IMPLICIT_DEF`（3533）、`G_BLOCK_ADDR`（3542）、向量/MOPS 等。全不接则 `return false`（3624），整函数 fallback 或 abort。

---

## 20.3 关键 helper

| helper | 行号 | 作用 |
| --- | --- | --- |
| `preISelLower()` | 2137 | 选择前改写类型/opcode |
| `earlySelect()` | 2389 | 优先于 importer 的特例 |
| `selectCompareBranch()` | 1779 | `G_BRCOND` 主入口 |
| `selectCompareBranchFedByFCmp()` | 1646 | FP 比较 → `Bcc`（可能两条） |
| `selectCompareBranchFedByICmp()` | 1760 | ICmp 分支，先试 `TBZ/CBZ` |
| `tryOptCompareBranchFedByICmp()` | 1665 | `TB(N)Z`/`CB(N)Z` 判断 |
| `tryOptAndIntoCompareBranch()` | 1584 | `and` + branch → `TBZ/TST` |
| `tryOptSelect()` | 4995 | `G_SELECT` 与 compare 合并 |
| `tryOptSelectConjunction()` | 4983 | 条件合取特例 |
| `emitSelect()` | 1122 | 生成 `CSEL/CSINC/CSINV/CSNEG/FCSEL` |
| `selectLoadStoreUIOp()` | 904 | 按 bank/宽度选 `LDR*ui`/`STR*ui` |
| `selectAddrModeIndexed()` | 7665 | 地址模式折叠 |
| `selectIntrinsic()` | 6709 | 普通 intrinsic |
| `selectIntrinsicWithSideEffects()` | 6145 | 有副作用 intrinsic |
| `processPHIs()` | 8183 | PHI bank 预处理 |
| `fixupPHIOpBanks()` | 8150 | 插入跨 bank PHI COPY |

### 20.3.1 `constrain*` 在哪里

AArch64 没有一堆 `constrainXxx()` 成员；常见的是：

```cpp
constrainSelectedInstRegOperands(*MI, TII, TRI, RBI);
RBI.constrainGenericRegister(Reg, AArch64::GPR64RegClass, MRI);
```

前者按指令定义约束所有操作数，后者只约束一个 vreg。`getRegClassForTypeOnBank()`（584）按 LLT/bank 返回最小类：

```cpp
if (RB.getID() == AArch64::GPRRegBankID) {
  if (Ty.getSizeInBits() <= 32) return &AArch64::GPR32RegClass;
  if (Ty.getSizeInBits() == 64) return &AArch64::GPR64RegClass;
  ...
}
```

这解释了为什么 `ADDXri` 的输出常是 `gpr64sp`：选择器选的是能覆盖 SP 的类，不是“最窄的类”。

---

## 20.4 典型 opcode 的选指令路径

### 20.4.1 `G_CONSTANT`

**零常量**：`earlySelect()` 2428–2450。`IsZero` 为真后，64 位把操作数改成 `XZR`，32 位改成 `WZR`，`setDesc(COPY)` 并 constrain。实测 `zero.mir`（只跑 `instruction-select`）输出：

```yaml
%0:gpr64 = COPY $xzr
$x0 = COPY %0
RET_ReallyLR implicit $x0
```

最终汇编：

```asm
mov x0, xzr
ret
```

**非零常量**：`earlySelect()` 返回 false，走 `selectImpl()`。`GINodeEquiv<G_CONSTANT, imm>` 在 `SelectionDAGCompat.td:65`；`MOVi32imm`/`MOVi64imm` 在 `AArch64InstrInfo.td:2667–2674`；`i64` 小常量 pattern 在 2700–2701：

```tablegen
def : Pat<(i64 i64imm_32bit:$src),
          (SUBREG_TO_REG (MOVi32imm (trunc_imm imm:$src)), sub_32)>;
```

实测 `konst42.mir` 输出：

```yaml
%1:gpr32 = MOVi32imm 42
%0:gpr64all = SUBREG_TO_REG %1, %subreg.sub_32
$x0 = COPY %0
```

最终汇编：

```asm
mov w8, #42        // =0x2a
mov w0, w8
ret
```

路径：`G_CONSTANT 0 -> earlySelect(2428) -> COPY XZR`；`G_CONSTANT 42 -> selectImpl -> Pat(2700) -> MOVi32imm + SUBREG_TO_REG`。

### 20.4.2 `G_SELECT`

C++ case 3475–3494：

```cpp
case TargetOpcode::G_SELECT: {
  auto &Sel = cast<GSelect>(I);
  if (tryOptSelect(Sel)) return true;
  Register DeadVReg = MRI.createVirtualRegister(&AArch64::GPR32RegClass);
  auto TstMI = MIB.buildInstr(AArch64::ANDSWri, {DeadVReg}, {Sel.getCondReg()})
                   .addImm(AArch64_AM::encodeLogicalImmediate(1, 32));
  constrainSelectedInstRegOperands(*TstMI, TII, TRI, RBI);
  if (!emitSelect(Sel.getReg(0), Sel.getTrueReg(), Sel.getFalseReg(),
                  AArch64CC::NE, MIB))
    return false;
  Sel.eraseFromParent();
  return true;
}
```

`tryOptSelect()`（4995）先尝试和前面的 `G_FCMP/G_ICMP` 合成 `FCSEL`/`CSEL`；否则 `ANDSWri` 把条件变成 `$nzcv` 的 Z 标志，`emitSelect()`（1122）生成 `CSELXr/CSELWr/FCSEL`。

实测 `sel.mir` 输出：

```yaml
%4:gpr32 = ANDSWri %2, 0, implicit-def $nzcv
%3:gpr64 = CSELXr %0, %1, 1, implicit $nzcv
```

最终汇编：

```asm
ands w8, w2, #0x1
csel x0, x0, x1, ne
ret
```

要点：一条 `G_SELECT` 变两条；`$nzcv` 隐式；`CSELXr` 最后立即数 `1` 是条件码 `NE`；`tryOptSelect` 命中时可能只有一条 `FCSEL/CSEL`。

### 20.4.3 `G_LOAD` / `G_STORE`

C++ case 2955。先检查指针：

```cpp
if (PtrTy != LLT::pointer(0, 64))
  return false;
```

只处理地址空间 0、64 位指针。然后读 MMO：

```cpp
AtomicOrdering Order = LdSt.getMMO().getSuccessOrdering();
if (isStrongerThanMonotonic(Order)) { /* LDAR/STLR/LDAPR */ }
```

原子路径 2972–3002；普通路径用 `selectLoadStoreUIOp()`（904）按 bank/宽度选 `LDR*ui/STR*ui`，再用 `selectAddrModeIndexed()`（3069）尝试折地址。实测：

```llvm
define i64 @load_add(ptr %p) {
entry:
  %v = load i64, ptr %p, align 8
  %r = add i64 %v, 1
  ret i64 %r
}
```

```asm
ldr x8, [x0]
add x0, x8, #1
ret
```

关键不是 opcode，而是 MMO：`cloneMemRefs`、ordering、对齐、地址空间都要保留。第 18 章已展示 `monotonic/acquire/release` 落到 `ldr/ldar/stlr`。

### 20.4.4 `G_BRCOND`

C++ case 2678：

```cpp
case TargetOpcode::G_BRCOND:
  return selectCompareBranch(I, MF, MRI);
```

`selectCompareBranch()`（1779）看条件定义者：`G_FCMP` 走 1646，`G_ICMP` 走 1760。ICmp 路径先试 `tryOptCompareBranchFedByICmp()`（1665）：

- `x == 0`/`x != 0` → `CBZ/CBNZ`（1750，`emitCBZ` 1625）；
- `x < 0`/`x >= 0` → `TBZ/TBNZ`（1698–1721，`emitTestBit` 1551）；
- 否则 compare + `Bcc`（1770–1775）。

实测 IR `icmp eq` 的 selected MIR：

```yaml
bb.1.entry:
  successors: %bb.2, %bb.3
  CBZX %0, %bb.2
  B %bb.3
```

最终汇编：

```asm
cbz x0, .LBB1_1
b .LBB1_2
```

### 20.4.5 `G_PHI`

两段：`setupMF()` 的 `processPHIs()`（91/8183）先做 bank 均一化；`select()` 的 non-generic 分支（2589–2613）把 `G_PHI` 改成 `PHI` 并 constrain。实测循环求和的 selected MIR（节选）：

```yaml
bb.2.loop:
  %1:gpr64common = PHI %7, %bb.1, %4, %bb.2
  %2:gpr64 = PHI %7, %bb.1, %5, %bb.2
  %4:gpr64sp = ADDXri %1, 1, 0
  %5:gpr64 = ADDXrr %2, %1
  %11:gpr64 = SUBSXrr %1, %0, implicit-def $nzcv
  Bcc 11, %bb.2, implicit $nzcv
  B %bb.3
```

`G_CONSTANT 0` 在入口变成 `COPY $xzr`，所以 PHI 入值是 `%7`；`G_ICMP + G_BRCOND` 被折成 `SUBSXrr + Bcc`，比较结果没有物化成 0/1。

### 20.4.6 `G_INTRINSIC`

C++ case 3529–3532：

```cpp
case TargetOpcode::G_INTRINSIC: return selectIntrinsic(I, MRI);
case TargetOpcode::G_INTRINSIC_W_SIDE_EFFECTS:
  return selectIntrinsicWithSideEffects(I, MRI);
```

`selectIntrinsic()` 6709 处理无副作用 intrinsic；`selectIntrinsicWithSideEffects()` 6145 处理有副作用版本。很多 intrinsic 会先被 TableGen pattern 命中（`GINodeEquiv<G_INTRINSIC, intrinsic_wo_chain>`，`SelectionDAGCompat.td:136`），importer 表达不了的才落到 C++。

---

## 20.5 TableGen 侧：pseudo、pattern、ComplexPattern、feature

### 20.5.1 `AArch64InstrGISel.td`

```tablegen
class AArch64GenericInstruction : GenericInstruction {   // 16
  let Namespace = "AArch64";
}
def G_ADD_LOW : AArch64GenericInstruction { ... }         // 22
def G_REV32 : AArch64GenericInstruction { ... }           // 30
def G_DUP : AArch64GenericInstruction { ... }             // 84
```

`GINodeEquiv` 在 306–356，例如：

```tablegen
def : GINodeEquiv<G_REV32, AArch64rev32>;   // 306
def : GINodeEquiv<G_DUP, AArch64dup>;       // 312
def : GINodeEquiv<G_EXTRACT_VECTOR_ELT, vector_extract>; // 351
```

这些 pseudo 不是“假指令”，而是 gMIR 的 target-specific opcode：Legalizer/Combiner 产生，ISel 再匹配成真实指令。`GISelMatchGenericTypes = 1` 表示按 generic 类型规则匹配，不固定 MVT。

### 20.5.2 `AArch64InstrInfo.td` 与 `AArch64InstrFormats.td`

常用 `GINodeEquiv` 在 `SelectionDAGCompat.td`：

```tablegen
def : GINodeEquiv<G_CONSTANT, imm>;   // 65
def : GINodeEquiv<G_ADD, add>;        // 72
def : GINodeEquiv<G_SELECT, select>;  // 110
def : GINodeEquiv<G_BRCOND, brcond>;  // 154
```

AArch64 pattern：

```tablegen
defm ADD : AddSub<0, "add", "sub", add>;          // InstrInfo.td:2767
defm CSEL : CondSelect<0, 0b00, "csel">;          // 3535
defm MOVZ : MoveImmediate<0b10, "movz">;          // 2592
```

`ADD` 的寄存器/立即数形式来自 `InstrFormats.td`：

```tablegen
class BaseAddSubRegPseudo<RegisterClass regtype, SDPatternOperator OpNode>
    : Pseudo<(outs regtype:$Rd), (ins regtype:$Rn, regtype:$Rm),
             [(set regtype:$Rd, (OpNode regtype:$Rn, regtype:$Rm))]>;   // 3002

class AddSubImmShift<...>
    : BaseAddSubImm<..., (set dstRegtype:$Rd,
                          (OpNode srcRegtype:$Rn, immtype:$imm))> { ... } // 2989
```

`AddSub` 给立即数形式 `AddedComplexity = 6`（3102/3107），让 `ADDXri` 优先于扩展寄存器形式。feature predicate：

```tablegen
def HasNEON : Predicate<"Subtarget->isNeonAvailable()">, ...;  // InstrInfo.td:91
def HasSVE  : Predicate<"Subtarget->isSVEAvailable()">, ...;   // 125
let Predicates = [HasNEON, HasBF16] in { ... }                 // 1728
```

### 20.5.3 `ComplexPattern` 与 `GIComplexOperandMatcher`

`InstrFormats.td:3742–3746`：

```tablegen
def am_indexed64 : ComplexPattern<iPTR, 2, "SelectAddrModeIndexed64", []>;
```

GlobalISel 对应物在 3764–3766：

```tablegen
def gi_am_indexed64 :
    GIComplexOperandMatcher<s64, "selectAddrModeIndexed<64>">,
    GIComplexPatternEquiv<am_indexed64>;
```

`ComplexPattern` 负责“匹配 + 渲染”两段；`GIComplexOperandMatcher` 把它接到 GlobalISel 的复杂操作数机制；selector 侧对应 `selectAddrModeIndexed()`（7665）。

---

## 20.6 读源码的方法：四步

1. **先看 `select()` 分派**（2562）：确认目标 opcode 走 `preISelLower`、`earlySelect`、`selectImpl` 还是 C++ case。
2. **再看生成物 `selectImpl`**：在 `AArch64GenGlobalISel.inc` 搜 `GIM_CheckOpcode`/`GIM_RecordInsn`；assertions 构建用 `-debug-only=instruction-select` 看轨迹。
3. **回 `.td` 找 pattern**：`SelectionDAGCompat.td` 查 `GINodeEquiv`，`AArch64InstrInfo.td`/`InstrFormats.td` 查 `Pat`/`PatFrag`/`ImmLeaf`/`ComplexPattern`/`Predicates`。
4. **用 MIR 反驳自己**：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=regbankselect in.ll -o rbs.mir
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 -verify-machineinstrs rbs.mir -o sel.mir
```

如果 `sel.mir` 与源码推导不一致，先查 LLT/bank/feature，再查路径是否走对。

---

## 20.7 三个可跑例子（命令与观察）

### 20.7.1 `G_CONSTANT 0` / `42`

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 zero.mir -o -
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 konst42.mir -o -
```

观察：`zero` 输出 `COPY $xzr`；`42` 输出 `MOVi32imm 42` + `SUBREG_TO_REG`。最终汇编分别是 `mov x0, xzr` 和 `mov w8,#42; mov w0,w8`。

### 20.7.2 `G_SELECT`

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 sel.mir -o -
```

观察：`ANDSWri` + `CSELXr`，`CSELXr` 带 `implicit $nzcv`。最终汇编：

```asm
ands w8, w2, #0x1
csel x0, x0, x1, ne
ret
```

### 20.7.3 `G_BRCOND`

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 \
    -stop-after=instruction-select br.ll -o -
```

观察：`CBZX %0, %bb.2` + `B %bb.3`；最终汇编 `cbz x0, .LBB1_1`。

---

## 20.8 踩雷清单

1. **以为 `selectImpl()` 包含全部逻辑**。它只是 matcher table 入口；`G_SELECT`、`G_BRCOND`、普通 load/store 有大量 C++ 路径。
2. **忘了 `G_PHI` 在 `isPreISelOpcode` 分支之前处理**。它不走普通 matcher。
3. **把 `preISelLower()` 当优化**。它会改类型/opcode，返回 true 后选择继续；按旧 opcode 推路径会错。
4. **只读 `.td`，不用 MIR 验证**。`AddedComplexity`、规则顺序、feature 都会改变实际命中。
5. **把 `getRegClassForTypeOnBank()` 当唯一来源**。它只按 bank/type 给最小类；具体指令约束由 `constrainSelectedInstRegOperands` 完成。
6. **拿不同版本的行号硬套**。本章行号只对 23.1.4 负责；换版本先重新 `grep` 函数名。

---

## 20.9 进阶：沿着生成物继续挖

- **matcher table**：在 `.inc` 里找 `getMatchTable()`，观察 `GIM_Try`、`GIM_CheckOpcode`、`GIR_MutateOpcode` 的顺序。
- **谓词位集**：`GET_GLOBALISEL_PREDICATE_BITSET` 段给出 feature 位，对照 `AArch64Subtarget` 的查询。
- **覆盖率**：assertions 构建用 `-coverage` 看哪些规则从未命中。
- **递归选择**：`selectAndRestoreState()`（3627）在一个 `select()` 内递归选择新指令，保存/恢复 `MIB` 状态。
- **复杂操作数**：从 `GIComplexOperandMatcher` 的 `selectAddrModeIndexed<64>` 追到 7665，理解匹配与渲染为什么分开。
- **特征分叉**：同一 opcode 在不同 `-mattr` 下命中不同规则，用 `-debug-only=instruction-select` 对照 feature 位。

---

## 20.10 动手练习

### 练习 20.1 画 `G_ADD` 路径

从 `G_ADD` 出发，画出 `select()` → `selectImpl()` → `ADDXrr/ADDXri` 的路径，指出 `GINodeEquiv` 和 `AddSub` 行号。

### 练习 20.2 验证 `G_CONSTANT 0`

用 `zero.mir` 跑 `instruction-select`，解释为什么是 `COPY $xzr` 而不是 `MOVi32imm 0`。

### 练习 20.3 破坏 `G_SELECT`

用 `sel.mir` 跑 `instruction-select`，删掉 `CSELXr` 的 `implicit $nzcv`，再用 `-verify-machineinstrs` 观察报错。

### 练习 20.4 追 `G_BRCOND`

把 `icmp eq` 改成 `icmp slt`、`icmp ne`，预测 `CBZ/CBNZ/TBZ/Bcc`，再跑 `llc` 验证。

### 练习 20.5 读生成物

在本地源码构建里找到 `AArch64GenGlobalISel.inc`，搜索 `GIM_RecordInsn`，找一条带常量折叠的规则，说明它对应哪个 `.td` pattern。

---

## 20.11 重点整理

- `select()` 顺序：非 generic/`G_PHI` → 隐式操作数检查 → `preISelLower()` → `earlySelect()` → `selectImpl()` → C++ switch → false。
- `G_CONSTANT 0` 走 `earlySelect()`；非零常量走 `selectImpl()` 的 `MOVi32imm/MOVi64imm` pattern。
- `G_SELECT` 走 C++ case：`ANDSWri` 设置 `$nzcv`，`emitSelect()` 生成 `CSEL`。
- `G_LOAD/G_STORE` 在 C++ case 里处理指针、MMO、原子 ordering、地址模式。
- `G_BRCOND` 走 `selectCompareBranch()`，优先 `CBZ/CBNZ/TBZ`，否则 compare + `Bcc`。
- `G_PHI` 在 `setupMF()` 由 `processPHIs()` 预处理，选择阶段按 `PHI` 处理。
- `G_INTRINSIC` 先由 TableGen pattern 尝试，失败后走 `selectIntrinsic()`/`selectIntrinsicWithSideEffects()`。
- TableGen：`AArch64InstrGISel.td` 管 pseudo/`GINodeEquiv`，`AArch64InstrInfo.td` 管 pattern/feature，`AArch64InstrFormats.td` 管格式/ComplexPattern。

---

## 20.12 自我检核

1. `select()` 在 `preISelLower()` 之前做了哪三件事？
2. 为什么 `G_PHI` 不走普通 matcher？
3. `G_CONSTANT 0` 和 `G_CONSTANT 42` 分别走哪条路径？
4. `G_SELECT` 为什么需要 `implicit $nzcv`？
5. `G_BRCOND` 在什么条件下生成 `CBZ`，什么条件下生成 `Bcc`？
6. `getRegClassForTypeOnBank()` 和 `constrainSelectedInstRegOperands()` 的分工是什么？
7. 生成文件里的 `selectImpl()` 做了什么、没做什么？
8. 读一个 opcode 的源码时，为什么要先看 `select()` 分派，再看 `.td`？

---

## 20.13 延伸阅读

- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`：本章主线，先读 `select()` 与 `preISelLower()`。
- `llvm/lib/Target/AArch64/AArch64InstrGISel.td`：pseudo 与 `GINodeEquiv`，590 行，适合通读。
- `llvm/lib/Target/AArch64/AArch64InstrInfo.td` / `AArch64InstrFormats.td`：pattern、`ImmLeaf`、`ComplexPattern`、`AddedComplexity`。
- `llvm/include/llvm/Target/GlobalISel/SelectionDAGCompat.td`：`G_CONSTANT/G_ADD/G_SELECT/G_BRCOND` 的映射。
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`：matcher table 执行实现，配合生成物阅读。
- `llvm/docs/GlobalISel/InstructionSelect.rst`：官方契约与 pipeline 说明。

---

## 下一章

[第 21 章 端到端案例：从 spec 到 lit](./21-end-to-end-custom-instruction.md) —— 把本章的源码地图用于一次完整改动：从指令 spec、TableGen pattern、C++ selector，到 MIR/lit 测试和上游 review。
