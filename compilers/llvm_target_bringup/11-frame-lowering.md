# Ch 11 — FrameLowering

> **目标**：能说清 PEI 在什么时刻向 `TargetFrameLowering` 要什么，并把 MiniRV 大栈帧里的每条 `lui/addi/add`、每个 `.cfi_def_cfa_offset` 与源码决策一一对上；能解释 2047/2048 边界为什么落在 `2048 - stackAlign`。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`；本文命令均在该构建实跑。

## 为什么需要这个？

指令选择结束时，栈上的东西还只是抽象的 `FrameIndex`。帧大小此时未知：要等寄存器分配完成，才知道哪些寄存器要溢出、有没有动态 alloca。而帧布局又决定寻址是否合法——RISC-V 的 load/store 只有 12 位有符号立即数，4800 字节的帧，溢出槽离 SP 有 4800 字节，一条 `sd` 写不到。

于是 LLVM 把这件事拆成独立阶段 `PrologEpilogInserter`（PEI，pass 名 `prolog-epilog`）：它负责时机、布局和插入位置，目标只负责“具体发什么指令”。这一章讲这份契约，以及 MiniRV 用约 300 行实现它的方式。

## 先建立直觉

向下增长的栈，`CFA`（Canonical Frame Address）就是进入函数那一刻的 SP，也是 DWARF 展开的基准：

```text
高地址
        ┌────────────────────────────────────┐
        │ 调用者的栈帧                         │
        ├────────────────────────────────────┤ ← 进入时的 SP = CFA
        │ 传入的栈参数（第 9、10… 个 i64）      │   相对 CFA 为正偏移
        ├────────────────────────────────────┤
        │ x1(ra) / x8(fp) 保存槽                │   callee-saved，-8 / -16
        ├────────────────────────────────────┤
        │ 局部对象：alloca、临时溢出槽           │   负偏移
        ├────────────────────────────────────┤
        │ 出栈参数区 outgoing args             │   hasReservedCallFrame 时预留
        └────────────────────────────────────┘ ← 当前 SP（16 字节对齐）
低地址
```

同一张图，`getFrameIndexReference` 有三种解读：

```text
① 无 FP：addr(FI) = X2 + (ObjOffset + StackSize)
② 有 FP：addr(FI) = X8 + ObjOffset                 X8 = CFA
③ callee-saved 槽：addr(FI) = X2 + (ObjOffset + Adjust)
   Adjust = 第一段 SP 调整量（拆分时）或 StackSize
```

没有动态 alloca 时 SP 在函数内是常数，相对 SP 最省寄存器；一旦 `alloca i64, i64 %n` 让 SP 中途移动，序言排好的对象就只能靠一个不动的基准——frame pointer。

## 主线 1：PEI 向 FrameLowering 要什么

`PEIImpl::run` 的顺序固定，理解它就理解每个钩子“何时被调用、能看到什么”：

| 顺序 | PEI 步骤 | 目标钩子 | 此时已知 / 未知 |
|---|---|---|---|
| 1 | `spillFPBP` | `spillFPBP` | 尚无帧布局 |
| 2 | `calculateCallFrameInfo` | `eliminateCallFramePseudoInstr` | 已知每个 call 的栈参数大小 |
| 3 | `calculateSaveRestoreBlocks` | — | 序言块、返回块已定 |
| 4 | `spillCalleeSavedRegs` | `determineCalleeSaves`、`spillCalleeSavedRegisters` | 已知哪些物理寄存器被改过 |
| 5 | `calculateFrameObjectOffsets` | `getOffsetOfLocalArea`、`getStackAlign` | 给每个对象分配最终 offset |
| 6 | `insertPrologEpilogCode` | `emitPrologue`、`emitEpilogue` | 布局已定，序言在这里生成 |
| 7 | `replaceFrameIndices` | `getFrameIndexReference`（经 `eliminateFrameIndex`） | SP 移动量已确定 |

两个常见误解在这里拆掉：序言**不是**指令选择时生成的；`getFrameIndexReference` 在序言**之后**才被大量调用，所以它能看到“中间 SP”这种状态——这正是 MiniRV 让溢出槽相对中间 SP 寻址的前提。

契约可以压成三句：

- `emitPrologue`/`emitEpilogue` 负责“把 SP 减到帧底、建 FP、对齐、保存/恢复”；PEI 只管调用时机与插入块。
- `determineCalleeSaves` 负责“哪些寄存器要存”；PEI 随后自己分配栈槽并回调 `spillCalleeSavedRegisters` 生成 store。
- `getFrameIndexReference` 是唯一的 FI→(基址寄存器, 偏移) 映射，PEI 不假设基址一定是 SP。

出栈参数区默认由序言预留（`hasReservedCallFrame`），调用点只需往 `0(x2)` 存参数；有变长 alloca 时 SP 会动，只能让 `ADJCALLSTACKDOWN/UP` 在调用点自己调整：

```cpp
bool MiniRVFrameLowering::hasReservedCallFrame(
    const MachineFunction &MF) const {
  // A dynamic alloca moves the stack pointer, so the outgoing argument area
  // cannot be reserved up front: the pseudo instructions at the call sites
  // claim it instead.
  return !MF.getFrameInfo().hasVarSizedObjects();
}
```

`eliminateCallFramePseudoInstr` 对应两条路：有保留调用帧就直接删伪指令；否则变成真正的 `addi x2, x2, ±Amount`，并用 `alignTo(..., getStackAlign())` 向上取整，保证 SP 仍是 16 的倍数。

## 主线 2：MiniRV 的实现

### 构造参数与 `determineCalleeSaves`

```cpp
MiniRVFrameLowering::MiniRVFrameLowering(const MiniRVSubtarget &STI)
    // Match the S128 stack alignment the data layout advertises: every call
    // keeps the stack pointer a multiple of sixteen.
    : TargetFrameLowering(TargetFrameLowering::StackGrowsDown, Align(16), 0,
                          Align(16)),
      STI(STI), TII(*STI.getInstrInfo()), TRI(*STI.getRegisterInfo()) {}
```

四个参数：栈向下长、栈对齐 16、`LocalAreaOffset = 0`、临时栈对齐 16。`LocalAreaOffset = 0` 让 `calculateFrameObjectOffsets` 从 CFA 开始排，callee-saved 槽才会落在 `-8/-16`。

```cpp
  TargetFrameLowering::determineCalleeSaves(MF, SavedRegs, RS);

  // The prologue overwrites the frame pointer, but that happens after the
  // callee-saved spills have been placed, so the register machinery cannot
  // see the clobber and has to be told about it.
  if (hasFP(MF))
    SavedRegs.set(MiniRV::X8);
```

基类只标记“函数体里真的写过”的 CSR；X8 是在**序言**里被写的，而序言生成晚于本钩子，寄存器分配器看不到这次写。漏掉这一行，函数能编译，但返回后调用者的 X8 已被破坏。

### `hasFP` 的三个触发条件

```cpp
  return MF.getFrameInfo().hasVarSizedObjects() ||
         MF.getFrameInfo().isFrameAddressTaken() ||
         MF.getSubtarget().getRegisterInfo()->hasStackRealignment(MF);
```

- `hasVarSizedObjects()`：动态 alloca 让 SP 中途移动，必须有第二基准。
- `isFrameAddressTaken()`：`llvm.frameaddress` 要“进入时的 SP”，此时 SP 已减、帧大小未知，只能靠 FP 记住。
- `hasStackRealignment(MF)`：对象对齐超过 16，序言会把 SP 向下取整，取整量编译期未知。

FP 的链子由此顺下来：`hasFP` → `getReservedRegs` 排除 X8 → `getFrameRegister` 返回 X8 → `LowerFRAMEADDR` 发 `CopyFromReg X8` → 序言先存旧 X8 再写 `X8 = X2 + StackSize`。要往上走一层（`frameaddress(1)`）得从 `-16` 槽读旧 X8，MiniRV 直接拒绝：`report_fatal_error("MiniRV cannot walk the frame chain")`。

### `getFrameIndexReference`：四条路

```cpp
  if (MFI.isCalleeSavedObjectIndex(FI)) {
    // A large frame is allocated in two steps and the spills happen between
    // them, so their slots are relative to the intermediate stack pointer.
    uint64_t Adjust = getFirstSPAdjustAmount(MF);
    FrameReg = MiniRV::X2;
    return StackOffset::getFixed(MFI.getObjectOffset(FI) +
                                 (Adjust ? Adjust : MFI.getStackSize()));
  }
  if (!hasFP(MF)) {
    FrameReg = MiniRV::X2;
    return StackOffset::getFixed(MFI.getObjectOffset(FI) + MFI.getStackSize());
  }
  // ...（realign 分支：非 fixed 对象走 SP）
  FrameReg = MiniRV::X8;
  return StackOffset::getFixed(MFI.getObjectOffset(FI));
```

三点：callee-saved 槽**永远走 SP** 且优先判断（溢出发生在 X8 建立前、恢复在 X8 恢复后）；拆分时它的基准是**中间 SP**；realign 时局部对象走取整后的 SP，fixed 对象（传入参数）走 X8。

### `emitPrologue`：先减 SP、再放溢出、最后建 FP

```cpp
  uint64_t FirstSPAdjust = getFirstSPAdjustAmount(MF);
  uint64_t FirstAdjust = FirstSPAdjust ? FirstSPAdjust : StackSize;
  if (FirstAdjust) {
    AfterSP = adjustReg(MBB, MBB.begin(), DL, MiniRV::X2, MiniRV::X2,
                        -static_cast<int64_t>(FirstAdjust),
                        MachineInstr::FrameSetup);
    if (NeedsCFI)
      CFIInstBuilder(MBB, AfterSP, MachineInstr::FrameSetup)
          .buildDefCFAOffset(FirstAdjust);
  }
```

这段必须在块首：PEI 随后插入的 callee-saved store 用的是“新 SP 相对偏移”。然后跳过所有 `FrameSetup` 指令找到插入点，补 `.cfi_offset`，再发第二段：

```cpp
  if (FirstSPAdjust) {
    uint64_t SecondAdjust = StackSize - FirstSPAdjust;
    AfterSP = adjustReg(MBB, InsertPt, DL, MiniRV::X2, MiniRV::X2,
                        -static_cast<int64_t>(SecondAdjust),
                        MachineInstr::FrameSetup);
    if (NeedsCFI)
      CFIInstBuilder(MBB, AfterSP, MachineInstr::FrameSetup)
          .buildDefCFAOffset(StackSize);
    InsertPt = AfterSP;
  }
  if (!HasFP)
    return;
  AfterSP = adjustReg(MBB, InsertPt, DL, MiniRV::X8, MiniRV::X2,
                      static_cast<int64_t>(StackSize),
                      MachineInstr::FrameSetup);
```

顺序是“第一段（中间 SP）→ 溢出 → 第二段（最终 SP）→ X8 = X2 + StackSize”。FP 最后建，因为它要等于进入时的 SP，而这时 X2 已是帧底。

### `emitEpilogue`：溢出夹在两段之间

有 FP 时先 `X2 = X8 - StackSize` 把 SP 拉回帧底（动态 alloca 会推得更低，不能靠 `addi` 猜），并 `.cfi_def_cfa_register x2`；然后**先释放第二段**，让溢出槽回到和序言相同的中间 SP，等 PEI 的 reload 执行完，最后释放第一段：

```cpp
  uint64_t FirstSPAdjust = getFirstSPAdjustAmount(MF);
  if (FirstSPAdjust) {
    uint64_t SecondAdjust = StackSize - FirstSPAdjust;
    auto AfterSecond =
        adjustReg(MBB, RestorePt, DL, MiniRV::X2, MiniRV::X2,
                  static_cast<int64_t>(SecondAdjust),
                  MachineInstr::FrameDestroy);
    // CFI：偏移变回 FirstSPAdjust
    MachineBasicBlock::iterator MBBI = MBB.getLastNonDebugInstr();
    auto AfterFirst = adjustReg(MBB, MBBI, DL, MiniRV::X2, MiniRV::X2,
                                static_cast<int64_t>(FirstSPAdjust),
                                MachineInstr::FrameDestroy);
    // CFI：偏移归 0
    return;
  }
```

这个“restore 夹在两段 addi 之间”的顺序，就是大帧能只用单条 load/store 访问溢出槽的全部秘密。

### `getFirstSPAdjustAmount` 与 `adjustReg`

```cpp
  uint64_t StackSize = MFI.getStackSize();
  if (isInt<12>(StackSize) || MFI.getCalleeSavedInfo().empty())
    return 0;
  // Realignment moves the stack pointer again; keep the single adjustment.
  if (MF.getSubtarget().getRegisterInfo()->hasStackRealignment(MF))
    return 0;
  // 2048 itself would need a split in the epilogue; 2048 minus the alignment
  // stays aligned and leaves every spill slot within reach.
  return 2048 - getStackAlign().value();
```

三个提前返回：帧 ≤ 2047 一条 `addi` 就够；没有 callee-saved 溢出就没有“围绕溢出拆分”的动机；有栈对齐时后面还有 `andi` 改 SP，再拆一次只会让中间 SP 更难描述。

```cpp
  if (DestReg == SrcReg && Val == 0)
    return II;
  if (isInt<12>(Val)) { /* 一条 ADDI */ }
  assert(isInt<32>(Val) && "MiniRV cannot materialise a larger adjustment");
  const int64_t Align = getStackAlign().value();
  const int64_t MaxPosAdjStep = 2048 - Align;
  if (Val > -4096 && Val <= 2 * MaxPosAdjStep) {
    int64_t FirstAdj = Val < 0 ? -2048 : MaxPosAdjStep;
    Val -= FirstAdj;           // 两条 ADDI
  }
  int64_t Hi20 = (Val + 0x800) >> 12;
  int64_t Lo12 = Val - (Hi20 << 12);
  Register Scratch = DestReg == SrcReg ? Register(MiniRV::X6) : DestReg;
  // LUI Scratch, Hi20; ADDI Scratch, Scratch, Lo12; ADD Dest, Scratch, Src
```

三个决策点：`isInt<12>` 的范围是 `[-2048, 2047]`；两条 `addi` 覆盖 `(-4096, 4064]`（负向第一段可取 `-2048`，正向只能取 `2032`，因为 `+2048` 编不出来）；否则 `lui+addi+add`，`Dest == Src` 时必须借 X6，因为 `lui` 会写坏还存着旧 SP 的目标寄存器。这就是 `getReservedRegs` 保留 X5/X6 的原因。

## 实测：600 × i64 与 riscv64 对照

输入 `/tmp/ch11/big600.ll`：

```llvm
target triple = "minirv64"

define i64 @big600(i64 %n) {
entry:
  %a = alloca [600 x i64], align 8
  %p = getelementptr inbounds [600 x i64], ptr %a, i64 0, i64 %n
  store i64 %n, ptr %p, align 8
  %v = load i64, ptr %a, align 8
  ret i64 %v
}
```

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch11/big600.ll -o - | sed -n '/^big600:/,/jalr/p'
big600:                                 # @big600
	.cfi_startproc
# %bb.0:                                # %entry
	lui	x6, -1
	addi	x6, x6, -704
	add	x2, x6, x2
	.cfi_def_cfa_offset 4800
	slli	x3, x10, 3
	addi	x4, x2, 0
	add	x3, x4, x3
	sd	x10, 0(x3)
	ld	x10, 0(x4)
	lui	x6, 1
	addi	x6, x6, 704
	add	x2, x6, x2
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

逐条解释（省略 `.size/.cfi_endproc` 等尾部指令）：

- 帧大小 = `600 × 8 = 4800`。叶子函数无 callee-saved 溢出，`getFirstSPAdjustAmount` 返回 0，整帧一次调整。
- `-4800` 既不在 `[-2048, 2047]`，也不在两条 addi 能覆盖的 `(-4096, 4064]`（`-4800 < -4096`），只能 `lui+addi+add`。
- `Hi20 = (-4800 + 0x800) >> 12 = -2752 >> 12 = -1`（算术右移向下取整），`lui x6, -1` 得到 `-4096`。
- `Lo12 = -4800 - (-4096) = -704`，`addi x6, x6, -704` 后 X6 = -4800。
- `add x2, x6, x2`：立即数装不下只能物化再加；`Dest == Src`，所以必须借 X6。
- `.cfi_def_cfa_offset 4800`：CFA 在当前 SP 之上 4800 字节。尾声对称：`Hi20 = (4800+2048)>>12 = 1`，`Lo12 = 4800-4096 = 704`。
- `addi x4, x2, 0` 是 `%a` 的地址：`ADDI %stack.0.a, 0` 解析后偏移恰好 0（对象在帧底）。

同一个 IR 换成 RISC-V（`+m`，MiniRV 是 RV64IM）：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/ch11/big600.ll -o - | sed -n '/^big600:/,/ret/p'
big600:                                 # @big600
	.cfi_startproc
# %bb.0:                                # %entry
	lui	a1, 1
	addi	a1, a1, 720
	sub	sp, sp, a1
	.cfi_def_cfa_offset 4816
	addi	a1, sp, 16
	slli	a2, a0, 3
	add	a1, a1, a2
	sd	a0, 0(a1)
	ld	a0, 16(sp)
	lui	a1, 1
	addi	a1, a1, 720
	add	sp, sp, a1
	.cfi_def_cfa_offset 0
	ret
```

两个 4816 的差别值得掰开：RISC-V 的 frameInfo dump 里有一个 **8 字节、没有 callee-saved 寄存器的 spill slot**（offset `-4816`），加 8 字节对齐填充。它来自 `RISCVFrameLowering::processFunctionBeforeFrameFinalized` 的 `if (!isInt<11>(MFI.estimateStackSize(MF))) ScavSlotsNum = 1;`：大帧要留一个紧急溢出槽给寄存器 scavenger，因为它靠 scavenger 消除远距离 frame index。4800 超过 11 位有符号上限 2047 → +8 → 向上取整到 16 → 4816，对象从 `sp+16` 开始。MiniRV 的 4800 是“真的只有 4800”（`large-frame.ll` 末尾的 `attributes #0 = { nounwind }` 会去掉 CFI 行，去掉它就能看到本文这些指令）。

## 实测：2047/2048 边界与 `2048 - stackAlign`

给函数加一个调用（产生 x1 溢出），形状是 `alloca [N x i64]` + `call void @sink(ptr)`。先看**实际**边界 252/253：

```console
$ diff <($B/llc -mtriple=minirv64 -O2 /tmp/ch11/f252.ll -o - | sed -n '/^f252:/,/cfi_endproc/p' | sed -n '4,12p') <($B/llc -mtriple=minirv64 -O2 /tmp/ch11/f253.ll -o - | sed -n '/^f253:/,/cfi_endproc/p' | sed -n '4,12p')
7,9c7,9
< 	addi	x8, x2, 0
< 	sd	x10, 0(x8)
< 	addi	x10, x8, 0
---
> 	addi	x2, x2, -16
> 	.cfi_def_cfa_offset 2048
> 	addi	x8, x2, 8
```

（`diff` 退出码 1 表示有差异，正常。）

- 252 个：`252×8 = 2016`，加 16 字节溢出槽 = 2032，`isInt<12>` 成立 → 一条 `addi -2032`；上半段是 `addi x8, x2, 0; sd x10, 0(x8)`。
- 253 个：`253×8 = 2024`，加 16 = 2040，按 16 对齐 = **2048** → 拆成 `-2032` + `-16`。
- 所以字节约界是 2047/2048，但用 i64 个数表达是 252/253，不是 255/256。

再看题目点名的 255/256/257（只贴序言后半）：

```console
$ for n in 255 256 257; do echo "=== $n ==="; $B/llc -mtriple=minirv64 -O2 /tmp/ch11/f$n.ll -o - | sed -n "/^f$n:/,/cfi_endproc/p" | sed -n '10,12p'; done
=== 255 ===
	addi	x2, x2, -32
	.cfi_def_cfa_offset 2064
	addi	x8, x2, 8
=== 256 ===
	addi	x2, x2, -32
	.cfi_def_cfa_offset 2064
	addi	x8, x2, 0
=== 257 ===
	addi	x2, x2, -48
	.cfi_def_cfa_offset 2080
	addi	x8, x2, 8
```

第二段分别 `-32/-32/-48`，帧 2064/2064/2080。`addi x8, x2, 8` **不是** frame pointer（这三例都没有 FP），它是 `%a` 的地址；x8 被选中且跨调用存活，所以前面有 `sd x8, 2016(x2)`。

拆分点为什么是 `2048 - stackAlign`？设第一段为 `A`，中间 SP = `CFA - A`，三个约束：

1. `A` 是 16 的倍数，中间 SP 才对齐。
2. 溢出槽在 `CFA-8`/`CFA-16`，相对中间 SP 是 `A-8`/`A-16`，要 ≤ 2047（`A ≤ 2055`）。
3. **epilogue 要把同一个 `A` 加回去，而 `addi` 正上限是 2047 → `A ≤ 2047`。**

满足 1、3 的最大值是 `2032 = 2048 - 16`。约束 3 才是决定性的：`-2048` 可编码、`+2048` 不可编码，源码注释 “2048 itself would need a split in the epilogue” 说的就是它。

## 实测：大帧 + 溢出 = 两段拆 + 两段再拆

4800 字节局部对象 + 一次调用 → 帧 4816，出现“第一段 2032 + 第二段 2784”，第二段自己又超过 2047：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch11/big600call.ll -o - | sed -n '/^big600call:/,/jalr/p'
big600call:                             # @big600call
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -2032
	.cfi_def_cfa_offset 2032
	sd	x1, 2024(x2)
	sd	x8, 2016(x2)
	.cfi_offset x1, -8
	.cfi_offset x8, -16
	addi	x2, x2, -2048
	addi	x2, x2, -736
	.cfi_def_cfa_offset 4816
	addi	x8, x2, 0
	sd	x10, 0(x8)
	addi	x10, x8, 0
	jal	x1, sink
	ld	x10, 0(x8)
	addi	x2, x2, 2032
	addi	x2, x2, 752
	.cfi_def_cfa_offset 2032
	ld	x8, 2016(x2)
	.cfi_restore x8
	ld	x1, 2024(x2)
	.cfi_restore x1
	addi	x2, x2, 2032
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

- 第一段 `-2032`，溢出写在 `2024(x2)`/`2016(x2)`，都是单条 `sd`。
- 第二段 `2784 = 4816 - 2032`，走两条 addi：`-2048` 余 `-736`；合计 `-2032-2048-736 = -4816`。
- 尾声先 `+2032`（2784 的第一条）再 `+752`，SP 回到中间位置，`ld` 偏移与序言完全一致；最后 `+2032` 回到 CFA。
- CFI 每步更新 `2032 → 4816 → 2032 → 0`；不更新的话，unwinder 在函数中途拿到的 CFA 就是错的。

## `6e7e7ab`：为什么拆分必须围绕溢出

```console
$ git -C $TREE show 6e7e7ab --stat | sed 's/[[:space:]]*$//'
commit 6e7e7abeb2b772e6ebac14241eab1ad94f28950b
Author: nodecoda <jemapam939@gmail.com>
Date:   Sat Nov 14 03:32:02 2026 +0800

    [MiniRV] Split large frames around the callee-saved spills

    A frame larger than 2047 bytes puts the callee-saved slots out of reach
    of a 12-bit displacement, so every spill and reload cost two addis to
    recompute the address.  RISC-V avoids this by lowering the stack pointer
    in two steps with the spills in between.

    Do the same: adjust by 2048 minus the stack alignment, spill and restore
    at that intermediate stack pointer, then adjust by the remainder.  The
    epilogue releases the second half before the restores and the first half
    afterwards, and the unwind state is updated at each step.

    vla 100 -> 88 instructions against the reference's 87, bigframe 30 -> 26
    (27), and the spill sequences become single stores and loads.

    Assisted-by: Codex

 llvm/lib/Target/MiniRV/MiniRVFrameLowering.cpp | 93 ++++++++++++++++++++++----
 llvm/lib/Target/MiniRV/MiniRVFrameLowering.h   |  6 ++
 llvm/test/CodeGen/MiniRV/dynamic-alloca.ll     | 11 +--
 llvm/test/CodeGen/MiniRV/large-frame.ll        | 27 +++-----
 4 files changed, 104 insertions(+), 33 deletions(-)
```

同提交里的测试 before/after 最能说明问题：

```diff
 ; CHECK-LABEL: caller:
-; CHECK:       addi x2, x2, -2048
-; CHECK-NEXT:  addi x2, x2, -368
-; CHECK:       addi x5, x2, 2032
-; CHECK-NEXT:  addi x5, x5, 376
-; CHECK-NEXT:  sd x1, 0(x5)
+; CHECK:       addi x2, x2, -2032
+; CHECK-NEXT:  sd x1, 2024(x2)
+; CHECK-NEXT:  sd x8, 2016(x2)
+; CHECK-NEXT:  addi x2, x2, -384
```

旧做法只调一次 SP，溢出槽在最终 SP 之上 2416 字节，12 位寻址够不着，只能每次 `addi x5, x2, 2032; addi x5, x5, 376; sd x1, 0(x5)`——每个 spill/reload 多两条指令。新做法先把 SP 降到 `CFA-2032`，溢出变成 `2024(x2)`/`2016(x2)`，再降剩余部分。commit message 的 “bigframe 30 -> 26 (27)” 就是这个差别（27 是 RISC-V 参考实现的指令数）。这也解释了为什么 callee-saved 槽用 `Adjust` 而不是 `StackSize`，以及为什么没有溢出时 `getFirstSPAdjustAmount` 直接返回 0。

## 实测：`hasFP` 的三个触发条件

动态 alloca（`alloca i64, i64 %n`）：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch11/vla.ll -o - | sed -n '/^vla:/,/cfi_endproc/p' | head -18
vla:                                    # @vla
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -32
	.cfi_def_cfa_offset 32
	sd	x1, 24(x2)
	sd	x8, 16(x2)
	sd	x9, 8(x2)
	.cfi_offset x1, -8
	.cfi_offset x8, -16
	.cfi_offset x9, -24
	addi	x8, x2, 32
	.cfi_def_cfa_register x8
	slli	x3, x10, 3
	addi	x3, x3, 15
	andi	x3, x3, -16
	sub	x9, x2, x3
	addi	x2, x9, 0
```

序言 `addi x8, x2, 32` 后立刻 `.cfi_def_cfa_register x8`：CFA 从“SP+偏移”变成“X8+0”。动态分配是 `sub x9, x2, x3`（x3 已按 16 取整），SP 从此不是帧底；尾声必须先 `addi x2, x8, -32` 再 reload，然后 `.cfi_def_cfa_register x2` 切回。

`llvm.frameaddress` 只读 X8，帧仅 16 字节：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch11/fa.ll -o - | sed -n '/^fa:/,/cfi_endproc/p' | sed -n '1,10p'
fa:                                     # @fa
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x8, 8(x2)
	.cfi_offset x8, -8
	addi	x8, x2, 16
	.cfi_def_cfa_register x8
	addi	x10, x8, 0
```

栈对齐（`alloca [8 x i64], align 64`）：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch11/align64.ll -o - | sed -n '/^align64:/,/cfi_endproc/p' | sed -n '1,16p'
align64:                                # @align64
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -128
	.cfi_def_cfa_offset 128
	sd	x1, 120(x2)
	sd	x8, 112(x2)
	sd	x9, 104(x2)
	.cfi_offset x1, -8
	.cfi_offset x8, -16
	.cfi_offset x9, -24
	addi	x8, x2, 128
	.cfi_def_cfa_register x8
	andi	x2, x2, -64
	addi	x9, x2, 0
	sd	x10, 0(x9)
```

`MaxAlign=64 > 16` → `hasFP` 为真，`realignStack` 发 `andi x2, x2, -64`（`-64` 能进 imm12，不用 `lui` 版本）。局部对象用取整后的 `x2`，恢复 SP 却用未取整的 `x8`——这就是 `getFrameIndexReference` realign 分支的由来。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 目标无关 | `llvm/lib/CodeGen/PrologEpilogInserter.cpp` | `PEIImpl::run`、`spillCalleeSavedRegs`、`calculateFrameObjectOffsets`、`insertPrologEpilogCode`、`replaceFrameIndices` | 调用顺序；callee-saved 槽先于局部对象排布 |
| API | `llvm/include/llvm/CodeGen/TargetFrameLowering.h` | `emitPrologue`、`emitEpilogue`、`determineCalleeSaves`、`getFrameIndexReference`、`hasReservedCallFrame` | 每个虚函数的默认语义 |
| MiniRV 帧 | `llvm/lib/Target/MiniRV/MiniRVFrameLowering.cpp` | `emitPrologue`、`emitEpilogue`、`getFirstSPAdjustAmount`、`adjustReg`、`getFrameIndexReference`、`realignStack` | 两段拆分的插入点与 CFI 更新 |
| MiniRV 寄存器 | `llvm/lib/Target/MiniRV/MiniRVRegisterInfo.cpp` | `eliminateFrameIndex`、`requiresVirtualBaseRegisters`、`getReservedRegs`、`getFrameRegister` | FI 如何变成基址+偏移；远对象走 X5 |
| 测试 | `llvm/test/CodeGen/MiniRV/large-frame.ll`、`dynamic-alloca.ll`、`stack-align.ll` | `big_array`、`two_arrays`、`caller`、`vla_big` | 大帧、拆分、动态 alloca 的断言 |

## 反直觉与踩雷

1. **错误直觉**：帧是 8 的倍数，2047 边界会在 2040/2048 之间。
   **正确认识**：帧还要加 callee-saved 槽并按 16 对齐。252 个 i64 → 2032，253 个 → 2040→2048；真正边界是 252/253，255/256 的帧一样大（2064）。

2. **错误直觉**：`addi x8, x2, 8` 在序言里，所以是 frame pointer。
   **正确认识**：这三例 `hasFP` 为假，X8 未被保留；那条是 `ADDI %stack.0.a, 0` 解析出的对象地址。真 FP 后面一定跟 `.cfi_def_cfa_register x8`。

3. **错误直觉**：`2048 - stackAlign` 是为了让溢出槽够得着。
   **正确认识**：够得着的上界是 2055。卡住的是 epilogue 要把同一个常数用 `addi` 加回去，而正上限是 2047。

4. **错误直觉**：大帧一定拆两段。
   **正确认识**：没有 callee-saved 溢出或有栈对齐时返回 0。4800 字节叶子函数仍是一次 `lui+addi+add`。

5. **错误直觉**：先存 callee-saved 再减 SP 也一样。
   **正确认识**：溢出 store 的偏移相对新 SP 计算（`2024(x2)`），SP 必须先减；顺序不能换。

## 进阶：再往深一层

- **`eliminateFrameIndex` 的两条路**：偏移仍装不进 imm12 时，`ADDI <fi>, 0`（物化对象地址）就地 `adjustReg` 到目标寄存器；load/store 则借 X5 算基址、偏移改 0。没有 scavenger 的后端只能这样处理远对象。

## 本章重点整理

- PEI 顺序：定 callee-saved → 排 offset → 生成序言/尾声 → 解析 FI；序言生成时布局已定，FI 解析在其后。
- `getFrameIndexReference` 是唯一的 FI→(基址, 偏移) 映射，MiniRV 有 callee-saved / 无 FP / realign / FP 四条路。
- `hasFP` 只在动态 alloca、`frameaddress`、栈对齐时为真；FP 建立在溢出后，撤销在 reload 前。
- `addi` 的 imm12 是 `[-2048, 2047]`；负向可用 `-2048`，正向最大 `+2032`，所以拆分点是 `2048 - stackAlign`。
- 大帧拆分的收益全在溢出槽；没有溢出就不拆。4800 的 MiniRV 是 `lui -1; addi -704; add`；riscv64 的 4816 = 4800 + 大帧紧急溢出槽 8 + 对齐 8，不是 ABI 固定帧头。

## 自我检核

- [ ] 不看源码，说出 PEI 的 7 个步骤，并指出 `emitPrologue` 与 `replaceFrameIndices` 谁先。
- [ ] 解释 `getFirstSPAdjustAmount` 三个提前返回各避免什么问题。
- [ ] 区分 `addi x8, x2, 8` 与 `addi x8, x2, 32` + `.cfi_def_cfa_register x8`。
- [ ] 解释 riscv64 的 4800 字节叶子函数为什么是 4816。

## 延伸阅读

- **[PrologEpilogInserter.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/PrologEpilogInserter.cpp)** — LLVM 项目
  - **读哪里**：`PEIImpl::run`、`calculateFrameObjectOffsets`、`spillCalleeSavedRegs`、`replaceFrameIndices`。
  - **能学到什么**：帧布局完整算法（callee-saved 先排、scavenging 槽、stack slot coloring 的交互）与每个钩子的调用时刻。
  - **前提**：先读本章的调用顺序表。

- **[TargetFrameLowering.h](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/CodeGen/TargetFrameLowering.h)** — LLVM 项目
  - **读哪里**：`emitPrologue`、`getFrameIndexReference`、`hasReservedCallFrame` 的注释与默认实现。
  - **能学到什么**：哪些必须由目标实现、哪些有默认实现，以及 `StackOffset` 为何区分 fixed/scalable。
  - **前提**：知道 `FrameIndex` 是什么。

- **[RISCVFrameLowering.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/RISCV/RISCVFrameLowering.cpp)** — LLVM 项目
  - **读哪里**：`getFirstSPAdjustAmount`、`determineFrameLayout`、`getFrameIndexReference`、`processFunctionBeforeFrameFinalized` 的 scavenger 槽。
  - **能学到什么**：成熟后端如何处理同一组约束（RVV 栈区、保存/恢复 libcall、大帧紧急槽），以及 4816 的来源。
  - **前提**：先读本章；RVV 细节可跳过。

- **[RISC-V psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V 国际基金会
  - **读哪里**：stack frame layout、callee-saved 寄存器、16 字节对齐。
  - **能学到什么**：区分 ABI 规定（16 字节对齐）与 LLVM 编码约束（2032）。
  - **前提**：无。

- **[课程：RISC-V 后端全貌](../riscv_backend/README.md)** — 本仓库
  - **读哪里**：帧布局与 calling convention 章节。
  - **能学到什么**：把 MiniRV 的最小实现放回完整后端上下文。
  - **前提**：读完本章。

→ [Ch 12 合法化：类型、操作与寻址模式](./12-legalization.md)
