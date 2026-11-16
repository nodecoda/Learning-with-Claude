# Ch 18 — 栈帧进阶

> **目标**：能解释 VLA、栈对齐、stack protector、frameaddress/returnaddress、stacksave/restore 如何改变帧的布局，并说清 MiniRV 为什么拒绝 stackmap/patchpoint。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

普通栈帧只有一条规则：prologue 减去一个编译期常量，所有对象用 `sp + 常量` 寻址，epilogue 加回去。只要引入下面任何一项，这条规则就不成立了：

- **VLA（dynamic alloca）**：`sp` 在函数中途移动，而且移动量只有运行时才知道；
- **过对齐对象**：`sp` 必须向下取整到 32/64/…，取整量不是编译期常量；
- **stack protector**：帧里多一个 canary 槽，返回前要比较并可能跳转到 `__stack_chk_fail`；
- **`llvm.frameaddress` / `llvm.returnaddress`**：程序显式要求“入口 SP”和“入口 RA”，这两个值在 prologue 之后都不在 `sp` 里；
- **`llvm.stacksave` / `llvm.stackrestore`**：程序要手动保存/恢复 `sp`；
- **stackmap / patchpoint**：伪指令没有编码，还需要 `.llvm_stackmaps` 记录和可 patch 的调用序列。

这些特性共同把“一个基址”变成“三个地址”：

| 名字 | 值 | 谁用 |
|---|---|---|
| 入口 SP / CFA | 调用者调用瞬间的 `x2` | fixed object、incoming args、varargs 保存区、`frameaddress(0)` |
| 当前 SP | `x2`，可能被 VLA 或 realign 改动 | 动态分配的对象的底部、call frame pseudo |
| frame pointer | `x8`，保存入口 SP | VLA/realign 下访问固定对象、恢复未取整的 SP |

Ch 11 讲基础 prologue/epilogue；本章讲这些“破坏常量帧”的特性，以及 MiniRV 在哪些地方选择支持、哪些地方选择明确拒绝。

## 先建立直觉

一个有 VLA、又有一个 64 字节对齐对象的帧（MiniRV 实际会拒绝这个组合；这里把两类特性叠在一起，只为展示三个基址）：

```
高地址
┌──────────────────────────────────┐
│ incoming args                    │ 0(x8), 8(x8), ...
├──────────────────────────────────┤ ← x8 = 入口 SP = CFA
│ callee-saved spill slots         │ x8-8, x8-16, ...
│ fixed locals                     │ x8-24, ...
├──────────────────────────────────┤ ← x2 = x8 - StackSize（未取整的帧底）
│ realign 的取整余量（最多 63 字节）│ andi x2,x2,-64 之后 x2 可能更低
├──────────────────────────────────┤ ← 取整后的 x2
│ over-aligned object              │ 0(x2)
├──────────────────────────────────┤
│ dynamic alloca（VLA）            │ x2 再向下移动，运行时决定
└──────────────────────────────────┘ ← 新的 x2
低地址
```

三条不变量：

1. `x8` 一旦建立就不再变化，所以 fixed object 永远能通过 `x8 + 固定偏移` 访问（incoming args 是正偏移，varargs 保存区是负偏移）。
2. 固定对象在 realign 之后仍然以 `x8` 为基址；普通 local 以取整后的 `x2` 为基址，因为 `x8 - StackSize` 和取整后的 `x2` 之间可能有几十字节差距。
3. epilogue 必须先把 `x2` 恢复成 `x8 - StackSize`（未取整），再按这个 `x2` 去 reload callee-saved；否则所有 spill 槽的偏移都会错。

## 主线 1：`hasFP` 与三个基址

`TargetFrameLowering::hasFP` 只是包了一层 naked 检查，真正决定的是 target 的 `hasFPImpl`：

```cpp
bool MiniRVFrameLowering::hasFPImpl(const MachineFunction &MF) const {
  return MF.getFrameInfo().hasVarSizedObjects() ||
         MF.getFrameInfo().isFrameAddressTaken() ||
         MF.getSubtarget().getRegisterInfo()->hasStackRealignment(MF);
}
```

三个条件分别对应三类“`sp` 不再等于入口 SP 减去常量”的情况。注意 **varargs 不在其中**：varargs 只需要栈空间，不需要 frame pointer；这正是 Ch 17 里 `sum` 有帧但没有 `x8` 的原因。

`hasFP` 为真时，`MiniRVRegisterInfo::getReservedRegs` 保留 `x8`，`determineCalleeSaves` 把 `x8` 加进保存集合，`getFrameRegister` 返回 `x8`；否则 `x8` 就是普通 callee-saved 寄存器，可以被分配器拿去做基址（`large-frame.ll` 的 `caller` 里 `addi x8,x2,0` 就是这种用法）。

```
hasFP == false          hasFP == true
x2 = frame base         x8 = 入口 SP
x8 = 可分配              x2 = 当前 SP（可能被 VLA/realign 改动）
fixed object: x2+StackSize+off     fixed object: x8+off
local:        x2+off               local: x8+off；realign 时 x2+StackSize+off
```

## 主线 2：`getFrameIndexReference` 的四个分支

`eliminateFrameIndex` 和所有 frame index 解析都走这个函数：

```cpp
if (MFI.isCalleeSavedObjectIndex(FI)) {
  uint64_t Adjust = getFirstSPAdjustAmount(MF);
  FrameReg = MiniRV::X2;
  return StackOffset::getFixed(MFI.getObjectOffset(FI) +
                               (Adjust ? Adjust : MFI.getStackSize()));
}
if (!hasFP(MF)) {
  FrameReg = MiniRV::X2;
  return StackOffset::getFixed(MFI.getObjectOffset(FI) + MFI.getStackSize());
}
if (hasStackRealignment(MF) && !MFI.isFixedObjectIndex(FI)) {
  FrameReg = MiniRV::X2;
  return StackOffset::getFixed(MFI.getObjectOffset(FI) + MFI.getStackSize());
}
FrameReg = MiniRV::X8;
return StackOffset::getFixed(MFI.getObjectOffset(FI));
```

四个分支分别解释：

1. **callee-saved spill**：这些槽在 `x8` 建立之前写；epilogue 也是先用 `x2` 回到固定帧底，再 reload 它们（`x8` 本身正是被 reload 的对象之一）。所以必须用 `x2`。大帧分两段调整时，spill 位于“中间 SP”，偏移是对象偏移加第一段调整量（`getFirstSPAdjustAmount`）。
2. **没有 FP**：`x2` 就是帧底，对象偏移加上 `StackSize`。
3. **有 FP 且 realign**：非 fixed 对象跟着取整后的 `x2` 走，所以仍用 `x2 + StackSize`；fixed object（incoming args 在入口 SP 之上，varargs 保存区在入口 SP 之下）以入口 SP 为基准，必须用 `x8`。
4. **有 FP、无 realign**：`x8` 就是入口 SP，对象偏移直接就是相对 `x8` 的偏移。

这个函数是 Ch 18 的核心：它把“帧的三种形态”编码成四个返回分支，`eliminateFrameIndex` 只是执行者。

## 主线 3：`eliminateFrameIndex`、`needsFrameBaseReg`、`materializeFrameBaseRegister`

后 RA 的解析在 `MiniRVRegisterInfo::eliminateFrameIndex`：

```cpp
int64_t Offset = TFI->getFrameIndexReference(MF, FrameIndex, FrameReg).getFixed();
if (MI.getOpcode() == TargetOpcode::LOCAL_ESCAPE) {
  MI.getOperand(FIOperandNum).ChangeToImmediate(Offset);
  return false;
}
Offset += MI.getOperand(FIOperandNum + 1).getImm();
if (isInt<12>(Offset)) {
  MI.getOperand(FIOperandNum).ChangeToRegister(FrameReg, false);
  MI.getOperand(FIOperandNum + 1).ChangeToImmediate(Offset);
  return false;
}
if (MI.getOpcode() == MiniRV::ADDI) {   // 地址物化指令，可以原地改
  TFI->adjustReg(MBB, II, DL, Dest, FrameReg, Offset, MachineInstr::NoFlags);
  MI.eraseFromParent();
  return true;
}
TFI->adjustReg(MBB, II, DL, MiniRV::X5, FrameReg, Offset, MachineInstr::NoFlags);
MI.getOperand(FIOperandNum).ChangeToRegister(MiniRV::X5, false);
MI.getOperand(FIOperandNum + 1).ChangeToImmediate(0);
```

- `LOCAL_ESCAPE` 只列 frame index、没有位移，`llvm.localrecover` 在运行时再加偏移，所以直接改成立即数。
- 12 位能放下就 `基址 + 位移`，这是绝大多数情况。
- 放不下时，`ADDI <fi>, 0` 这种“物化地址”的指令可以原地改成 `adjustReg` 的序列（可能是两条 `addi`，也可能 `lui+addi+add`），然后删掉原指令。
- 其他 load/store 用保留的 `X5` 做 scratch，位移清零。`X5` 在 `getReservedRegs` 里被保留，就是为这个准备的。

RA 之前还有一层：`LocalStackSlotAllocation` 会问 `needsFrameBaseReg`：

```cpp
bool MiniRVRegisterInfo::needsFrameBaseReg(MachineInstr *MI, int64_t Offset) const {
  const MachineFrameInfo &MFI = MI->getMF()->getFrameInfo();
  return !isFrameOffsetLegal(MI, MiniRV::X2, Offset + MFI.getLocalFrameSize());
}
```

如果对象离 `x2` 太远，就在 RA 前用 `materializeFrameBaseRegister` 建一个虚拟基址寄存器：

```cpp
Register BaseReg = MF.getRegInfo().createVirtualRegister(&MiniRV::GPRRegClass);
BuildMI(*MBB, MBBI, DL, TII.get(MiniRV::ADDI), BaseReg)
    .addFrameIndex(FrameIdx)
    .addImm(Offset);
```

这个 pass 在 RA 之前，所以虚拟寄存器还能参与分配；否则每个超范围访问都要在 RA 之后用 `X5` 重新物化地址。VLA 函数被排除在外（`requiresVirtualBaseRegisters` 返回 false）：它围绕 `x8` 布局，而这个 pass 生成的基址是 `sp` 相对的，会失效；由 `eliminateFrameIndex` 用 `X5` 兜底。

## 主线 4：`getFirstSPAdjustAmount` 与大帧拆分

```cpp
uint64_t MiniRVFrameLowering::getFirstSPAdjustAmount(const MachineFunction &MF) const {
  const MachineFrameInfo &MFI = MF.getFrameInfo();
  uint64_t StackSize = MFI.getStackSize();
  if (isInt<12>(StackSize) || MFI.getCalleeSavedInfo().empty())
    return 0;
  if (MF.getSubtarget().getRegisterInfo()->hasStackRealignment(MF))
    return 0;
  return 2048 - getStackAlign().value();
}
```

条件很明确：帧超过 2047 字节、有 callee-saved 槽、且没有 realign。返回值是 `2048 - 16 = 2032`，不是 2048：

- `addi` 的立即数是 12 位有符号，正向最大 2047；用 2032 给 epilogue 的正向调整留出余量；
- 2032 是 16 的倍数，中间 SP 仍然满足 ABI 对齐；
- spill 槽位于“中间 SP”的正偏移 0..2031 内，单条 `sd/ld` 就能访问。

prologue 的顺序因此变成：

```
addi x2, x2, -2032      # 第一段
sd x1, 2024(x2)         # spills 位于中间 SP 的 12 位范围内
sd x8, 2016(x2)
addi x2, x2, -384       # 第二段（StackSize - 2032）
```

epilogue 必须反着来：先 `+384` 回到中间 SP，reload spills，再 `+2032`。这就是 `caller` 里那组 `addi x2,x2,384; ld x8,2016(x2); ld x1,2024(x2); addi x2,x2,2032` 的来历。如果没有这个拆分，每个 spill/reload 都要先 materialize 地址，帧越大代码越差。

## 主线 5：dynamic alloca（VLA）

VLA 的合法化只有一行：

```cpp
setOperationAction(ISD::DYNAMIC_STACKALLOC, MVT::i64, Expand);
```

通用展开就是 MiniRV 需要的全部：DAG builder 已经把大小向上取整到栈对齐，展开后是“算大小、减、写回 SP”。实测的 `vla`：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/misc.ll -o - | sed -n '/^vla:/,/jalr/p'
vla:                                    # @vla
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x8, 8(x2)
	.cfi_offset x8, -8
	addi	x8, x2, 16
	.cfi_def_cfa_register x8
	slli	x3, x10, 3
	addi	x3, x3, 15
	andi	x3, x3, -16
	sub	x3, x2, x3
	addi	x2, x3, 0
	addi	x10, x0, 5
	sd	x10, 0(x3)
	addi	x2, x8, -16
	.cfi_def_cfa_register x2
	ld	x8, 8(x2)
	.cfi_restore x8
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

逐段解释：

- `addi x2,x2,-16; sd x8,8(x2); addi x8,x2,16`：先分配固定帧、保存旧 `x8`，再把 `x8` 设成入口 SP。
- `slli x3,x10,3`：`n * 8`；`addi x3,x3,15; andi x3,x3,-16`：向上取整到 16。
- `sub x3,x2,x3; addi x2,x3,0`：`newsp = sp - size`，再写回 `x2`。`addi x2,x3,0` 是通用展开保留“结果值”的复制，不是多余指令。
- `sd x10,0(x3)`：对象就在新的 `x2` 处。
- `addi x2,x8,-16`：epilogue 用 `x8` 恢复固定帧底，**不是**在当前 `x2` 上加常量（VLA 已经把 `x2` 移走了）。
- 之后才 `ld x8,8(x2)`、`addi x2,x2,16`。

VLA 还改变调用约定：`hasReservedCallFrame` 在 `hasVarSizedObjects()` 时返回 false，出参区不能在 prologue 预留，`ADJCALLSTACKDOWN/UP` 必须变成调用点真实的 `addi x2,x2,-N` / `+N`。`dynamic-alloca.ll` 的 `vla_call` 里 `addi x2,x2,-16; sd x10,0(x2); jal x1,nine; addi x2,x2,16` 就是证据；对比 Ch 17 的 `call9`，后者只在 prologue 预留，调用点没有 `sp` 调整。

## 主线 6：stack realignment 与过对齐对象

`hasStackRealignment(MF)` 是 `shouldRealignStack(MF) && canRealignStack(MF)`；对 MiniRV 来说，就是帧里有对象的对齐要求超过 16。

prologue 在设好 `x8` 之后做：

```cpp
if (TRI.hasStackRealignment(MF)) {
  if (MFI.hasVarSizedObjects())
    report_fatal_error("MiniRV cannot realign the stack with variable-sized allocations");
  realignStack(MBB, InsertPt, DL, MFI.getMaxAlign());
}
```

`realignStack` 用 `-MaxAlign` 做掩码：`andi` 能放下 12 位立即数就直接 `andi x2,x2,-64`；否则把掩码 `lui+addi` 到 `X6`，再 `and x2,x2,x6`。实测 `alloca i64, align 64`：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch18-align.ll -o - | sed -n '/^over64:/,/jalr/p' | grep -v '\.cfi_'
over64:                                 # @over64
# %bb.0:
	addi	x2, x2, -64
	sd	x8, 56(x2)
	addi	x8, x2, 64
	andi	x2, x2, -64
	addi	x10, x2, 0
	addi	x3, x0, 1
	sd	x3, 0(x10)
	addi	x2, x8, -64
	ld	x8, 56(x2)
	addi	x2, x2, 64
	jalr	x0, 0(x1)
```

- `addi x2,x2,-64`：帧大小被撑到 64，保证取整后仍有足够空间。
- `addi x8,x2,64`：`x8` 记录**未取整**的帧底。
- `andi x2,x2,-64`：真正的 realign；对象地址就是取整后的 `x2`。
- epilogue 先 `addi x2,x8,-64` 回到未取整的帧底，再 reload `x8`。如果直接在取整后的 `x2` 上加 64，reload 的偏移会整体错位。

为什么拒绝 VLA + realign？realign 之后 local 以取整后的 SP 为基址，而 VLA 又会在它之上移动 SP；local 的取整基址必须另外保存（源码注释的原话是 “a second base register”）。MiniRV 只有一个 `x8`，所以直接报错，而不是生成会踩坏对象的代码。

## 主线 7：stack protector

`d23fc5f` 只在 `MiniRVSubtarget::initLibcallLoweringInfo` 里加了两行：

```cpp
Info.setLibcallImpl(RTLIB::STACK_CHECK_GUARD, RTLIB::impl___stack_chk_guard);
Info.setLibcallImpl(RTLIB::STACKPROTECTOR_CHECK_FAIL, RTLIB::impl___stack_chk_fail);
```

canary 的读取、存放、比较、失败调用全部由通用 lowering 完成；MiniRV 不需要在 prologue/epilogue 里写任何代码。`sspstrong` 函数的实测：

```console
$ $B/llc -mtriple=minirv64 $TREE/llvm/test/CodeGen/MiniRV/stack-protector.ll -o - \
    | sed -n '/^guarded:/,/Lfunc_end0/p' | grep -v '\.cfi_'
guarded:                                # @guarded
# %bb.0:
	addi	x2, x2, -48
	sd	x1, 40(x2)
	sd	x8, 32(x2)
.Lpcrel_hi0:
	auipc	x3, __stack_chk_guard
	addi	x3, x3, .Lpcrel_hi0
	ld	x3, 0(x3)
	addi	x8, x2, 24
	sd	x3, 0(x8)
	addi	x10, x2, 8
	addi	x3, x0, 0
	sb	x3, 0(x10)
	jal	x1, use
	ld	x3, 0(x8)
.Lpcrel_hi1:
	auipc	x4, __stack_chk_guard
	addi	x4, x4, .Lpcrel_hi1
	ld	x4, 0(x4)
	bne	x4, x3, .LBB0_2
# %bb.1:
	ld	x8, 32(x2)
	ld	x1, 40(x2)
	addi	x2, x2, 48
	jalr	x0, 0(x1)
.LBB0_2:
	jal	x1, __stack_chk_fail
.Lfunc_end0:
```

`auipc+addi` 是 `PseudoLA` 的 PC 相对序列；`ld` 读 guard，`sd` 存到 canary 槽，调用后 `ld` 回来，`bne` 不等就 `jal __stack_chk_fail`。注意这里 `x8` 只是普通的 callee-saved 寄存器，被寄存器分配器选来做 canary 槽的基址；`hasFP` 是 false。

## 主线 8：frameaddress、returnaddress、stacksave/restore

`2735b99` 一并实现了这组 intrinsic。

`llvm.frameaddress(0)` 是入口 SP，prologue 之后无法从 `x2` 反推，所以它强制 `hasFP`：

```cpp
if (Op.getConstantOperandVal(0) != 0)
  report_fatal_error("MiniRV cannot walk the frame chain");
MF.getFrameInfo().setFrameAddressIsTaken(true);
Register FrameReg = MF.getSubtarget().getRegisterInfo()->getFrameRegister(MF);
return DAG.getCopyFromReg(DAG.getEntryNode(), DL, FrameReg, VT);
```

`llvm.returnaddress(0)` 是入口 `x1`，它是一个 live-in：

```cpp
MF.getFrameInfo().setReturnAddressIsTaken(true);
Register Reg = MF.addLiveIn(MiniRV::X1, getRegClassFor(MVT::i64));
return DAG.getCopyFromReg(DAG.getEntryNode(), DL, Reg, VT);
```

两者都拒绝非零 depth：MiniRV 没有帧链，不能像 RISC-V 那样沿 `fp-16` 逐级 load。

`llvm.stacksave`/`llvm.stackrestore` 不需要 lowering 代码，只需要声明“哪个寄存器是 SP”：

```cpp
setStackPointerRegisterToSaveRestore(MiniRV::X2);
```

通用展开就是“读 `x2`”和“写 `x2`”。实测：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/misc.ll -o - | sed -n '/^fa:/,/jalr/p' | grep -v '\.cfi_'
fa:                                     # @fa
# %bb.0:
	addi	x2, x2, -16
	sd	x8, 8(x2)
	addi	x8, x2, 16
	addi	x10, x8, 0
	addi	x2, x8, -16
	ld	x8, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
$ $B/llc -mtriple=minirv64 -O2 /tmp/misc.ll -o - | sed -n '/^ra:/,/jalr/p'
ra:                                     # @ra
	.cfi_startproc
# %bb.0:
	addi	x10, x1, 0
	jalr	x0, 0(x1)
```

`fa` 里 `addi x10,x8,0` 返回的就是 `x8`；`ra` 是叶子函数，`x1` 还没被覆盖，直接 `addi x10,x1,0`，连帧都不需要。

有调用时就不一样了：`x1` 会被 `jal` 覆盖，必须在调用前把入口 RA 拷到 callee-saved 寄存器：

```console
$ $B/llc -mtriple=minirv64 -O2 $TREE/llvm/test/CodeGen/MiniRV/frame-info.ll -o - \
    | sed -n '/^retaddr_after_call:/,/jalr/p'
retaddr_after_call:                     # @retaddr_after_call
# %bb.0:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	sd	x8, 0(x2)
	addi	x8, x1, 0
	jal	x1, opaque
	addi	x10, x8, 0
	ld	x8, 0(x2)
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

`addi x8,x1,0` 在 `jal` 之前，`addi x10,x8,0` 在之后。这里还有一个容易踩的细节：`spillCalleeSavedRegisters` 不能在 `isReturnAddressTaken()` 时把 `x1` 的 spill 标成 kill，因为 intrinsic 后面还要读这个 live-in 值；否则机器验证器会拒绝。

`stacksave`/`stackrestore` 的行为：

```console
$ $B/llc -mtriple=minirv64 -O2 $TREE/llvm/test/CodeGen/MiniRV/frame-info.ll -o - \
    | sed -n '/^save_restore:/,/jalr/p'
save_restore:                           # @save_restore
# %bb.0:
	addi	x2, x2, -16
	sd	x8, 8(x2)
	addi	x8, x2, 16
	addi	x3, x2, 0
	slli	x4, x10, 3
	addi	x4, x4, 15
	andi	x4, x4, -16
	sub	x4, x2, x4
	addi	x2, x4, 0
	addi	x10, x0, 5
	sd	x10, 0(x4)
	addi	x2, x3, 0
	addi	x2, x8, -16
	ld	x8, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

`addi x3,x2,0` 保存 SP；VLA 之后 `addi x2,x3,0` 恢复；最后仍然用 `x8` 回到固定帧底。`stackrestore` 只恢复 `sp`，不恢复寄存器，也不回滚 `x8` 的值。

## 主线 9：为什么拒绝 stackmap / patchpoint

`ac86a8a` 在 `MiniRVAsmPrinter::emitInstruction` 的最前面加了检查（当前代码覆盖三种伪指令）：

```cpp
if (MI->getOpcode() == TargetOpcode::STACKMAP ||
    MI->getOpcode() == TargetOpcode::PATCHPOINT ||
    MI->getOpcode() == TargetOpcode::STATEPOINT)
  report_fatal_error("MiniRV does not support stack maps");
```

诊断实测：

```console
$ $B/llc -mtriple=minirv64 $TREE/llvm/test/CodeGen/MiniRV/stackmap-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support stack maps
$ $B/llc -mtriple=minirv64 $TREE/llvm/test/CodeGen/MiniRV/patchpoint-error.ll -o /dev/null
LLVM ERROR: MiniRV does not support stack maps
$ $B/llc -mtriple=minirv64 $TREE/llvm/test/CodeGen/MiniRV/stack-align-error.ll -o /dev/null
LLVM ERROR: MiniRV cannot realign the stack with variable-sized allocations
```

前两条是 `report_fatal_error`，进程会 abort（exit 134），所以测试写 `not --crash`。重点不是“不崩溃”，而是**在固定位置给出可读诊断**，而不是让生成的指令打印机在一条没有编码的伪指令上 assert。

为什么不能静默忽略？三个原因：

1. `STACKMAP`/`PATCHPOINT`/`STATEPOINT` 是伪指令，没有机器编码，MC 层无法打印；
2. 真正的 stackmap 支持要写 `.llvm_stackmaps` 段、记录每个 map 的 PC 偏移、栈槽和寄存器位置，还要给 patchpoint 预留精确的可写字节数；MiniRV 的 AsmPrinter 没有这些；
3. 消费这些 map 的是 GC/JIT/deopt 运行时。静默丢掉 map 或生成错位的 call，运行时会在错误的 PC/栈槽上恢复状态，通常表现为随机崩溃或内存破坏，比编译期报错难查得多。

配套的两个细节：

- `MiniRVSubtarget` 给 `RTLIB::DEOPTIMIZE` 命名了 `__llvm_deoptimize`，让 `llvm.experimental.deoptimize` 走到 patchpoint，从而被 AsmPrinter 拒绝；如果不命名，通用代码只会打印错误并**丢掉调用**，那是更危险的静默行为。
- `LowerCall` 即使 `NumBytes == 0` 也发 `CALLSEQ_START/END`，因为 patchpoint 的 lowering 期望每个 patchpoint 外面都有 callseq（`11d2d44`）。MiniRV 虽然拒绝 patchpoint，但仍保持这个形状，避免在拒绝之前生成结构不合法的 MIR。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| FP 判定 | `MiniRVFrameLowering.cpp` | `hasFPImpl` | VLA / frameaddress / realign 三个条件 |
| 帧基址 | `MiniRVFrameLowering.cpp` | `getFrameIndexReference` | 四个分支、callee-saved 特例 |
| 帧 index 解析 | `MiniRVRegisterInfo.cpp` | `eliminateFrameIndex` | `LOCAL_ESCAPE`、12 位、`X5` scratch |
| RA 前基址 | `MiniRVRegisterInfo.cpp` | `needsFrameBaseReg`、`materializeFrameBaseRegister` | `LocalStackSlotAllocation` 的接口 |
| 大帧拆分 | `MiniRVFrameLowering.cpp` | `getFirstSPAdjustAmount` | 2032 的来源与对称性 |
| VLA | `MiniRVISelLowering.cpp` | `DYNAMIC_STACKALLOC`、`hasReservedCallFrame` | 通用展开、调用点 `sp` 调整 |
| realign | `MiniRVFrameLowering.cpp` | `realignStack` | `andi`/`lui+addi+and`，VLA 组合拒绝 |
| stack protector | `MiniRVSubtarget.cpp` | `initLibcallLoweringInfo` | `__stack_chk_guard`/`__stack_chk_fail` |
| 地址 intrinsic | `MiniRVISelLowering.cpp` | `LowerFRAMEADDR`、`LowerRETURNADDR`、`setStackPointerRegisterToSaveRestore` | depth 拒绝、X1 live-in、SP 保存 |
| stackmap 拒绝 | `MiniRVAsmPrinter.cpp` | `emitInstruction` | 三个伪指令的检查 |
| RISC-V 参照 | `RISCVFrameLowering.cpp`、`RISCVISelLowering.cpp` | `hasFPImpl`、`lowerFRAMEADDR`、`lowerRETURNADDR` | depth>0 沿 fp 链 load；支持 stackmap |

## 实测：大帧拆分

`large-frame.ll` 的 `caller`（2416 字节帧）：

```console
$ $B/llc -mtriple=minirv64 -O2 $TREE/llvm/test/CodeGen/MiniRV/large-frame.ll -o - \
    | sed -n '/^caller:/,/End function/p'
caller:                                 # @caller
# %bb.0:                                # %entry
	addi	x2, x2, -2032
	sd	x1, 2024(x2)
	sd	x8, 2016(x2)
	addi	x2, x2, -384
	addi	x8, x2, 0
	sd	x10, 0(x8)
	jal	x1, callee
	ld	x3, 0(x8)
	add	x10, x10, x3
	addi	x2, x2, 384
	ld	x8, 2016(x2)
	ld	x1, 2024(x2)
	addi	x2, x2, 2032
	jalr	x0, 0(x1)
```

`2032 + 384 = 2416`；spill 在中间 SP 的 `2024/2016`，正好在 12 位内。这里的 `x8` 不是 frame pointer（`hasFP` 为 false）；它是寄存器分配器为数组地址选的 callee-saved 寄存器，因为这个地址要跨过 `jal` 继续使用（`sd x10,0(x8)` 与 `ld x3,0(x8)`）。

对应验收测试：

```console
$ $B/llvm-lit -q $TREE/llvm/test/CodeGen/MiniRV/dynamic-alloca.ll \
      $TREE/llvm/test/CodeGen/MiniRV/frame-info.ll \
      $TREE/llvm/test/CodeGen/MiniRV/stack-align.ll \
      $TREE/llvm/test/CodeGen/MiniRV/stack-protector.ll \
      $TREE/llvm/test/CodeGen/MiniRV/stackmap-error.ll \
      $TREE/llvm/test/CodeGen/MiniRV/patchpoint-error.ll
-- Testing: 6 tests, 6 workers --

Total Discovered Tests: 6
$ echo $?
0
```

## 反直觉与踩雷

1. **错误直觉**：frame pointer 只是调试用的，优化掉无所谓，反正 `sp` 能寻址。
   **正确认识**：VLA、`llvm.frameaddress`、realign 下 `x8` 是正确性基础。它保存的是**入口 SP**，用来恢复未取整/未移动的 `sp`，并访问 fixed object；删掉它，epilogue 的 reload 偏移和 incoming arg 全部会错。
2. **错误直觉**：`alloca i64, align 64` 只是让对象地址好看，编译器顶多多留点 padding。
   **正确认识**：prologue 真的执行 `andi x2,x2,-64`，把 `sp` 向下取整；epilogue 必须先回到 `x8 - StackSize` 再 reload。取整后的 `sp` 和固定帧底之间的差距不是常量，不能直接 `addi x2,x2,N`。
3. **错误直觉**：有 VLA 也能顺手 realign，最多多用几条指令。
   **正确认识**：MiniRV 直接拒绝组合（`MiniRV cannot realign the stack with variable-sized allocations`）。realign 后 `x2` 被取整，VLA 又在其上移动，固定对象和动态对象需要两个不同的基址；只有一个 `x8` 时无法同时满足。
4. **错误直觉**：`llvm.returnaddress` 就是读 `x1`，什么时候读都行。
   **正确认识**：调用之后 `x1` 已经是返回地址（指向本函数的调用点），不是入口 RA。必须在调用前拷到 callee-saved 寄存器（`retaddr_after_call` 的 `addi x8,x1,0`）；并且 `x1` 的 prologue spill 不能标 kill。
5. **错误直觉**：stackmap 不支持就不支持，忽略伪指令继续生成代码即可。
   **正确认识**：伪指令没有编码，忽略会生成错位的调用/栈布局；消费 map 的运行时会在错误的 PC 和栈槽上恢复状态。MiniRV 选择在 AsmPrinter 统一 `report_fatal_error`，把静默错误变成编译期诊断。

## 进阶：再往深一层

- **为什么 realign 在 spill 之后？** callee-saved spill 用的是未取整的 `sp`（`getFrameIndexReference` 的第一个分支），而 local 用的是取整后的 `sp`。如果先 realign 再 spill，spill 偏移会基于取整后的 SP，epilogue 恢复时的基址就不一致了。
- **`getFirstSPAdjustAmount` 为什么要求 `!CalleeSavedInfo.empty()`？** 没有 spill 就没有“必须落在中间 SP 12 位范围内”的访问，拆不拆都一样；不拆反而少一条指令。
- **RISC-V 的 `lowerFRAMEADDR` 支持 depth>0**：它沿 `fp-16` 逐级 `load` 旧 FP；`lowerRETURNADDR` 再在 `frameaddr-8` 读 RA。MiniRV 没有帧链，所以 `report_fatal_error("MiniRV cannot walk the frame chain")`。这是“能力边界要显式”的典型例子。
- **确认你的 target 的 stackmap 策略**：支持就实现 `STACKMAP`/`PATCHPOINT` lowering + `.llvm_stackmaps` + 可 patch 的 call 序列；不支持就像 MiniRV 一样在 AsmPrinter 最前面拒绝。最糟的是“编译通过、运行时 map 错位”。

## 本章重点整理

- `hasFP` 的三个条件：VLA、frameaddress、stack realignment；varargs 不在其中。
- `getFrameIndexReference` 的四个分支决定了 fixed object、local、callee-saved spill 各自以 `x8` 还是 `x2` 为基址。
- `eliminateFrameIndex` 负责 12 位内直接寻址，超范围时用 `X5` 或原地改 `ADDI`；`needsFrameBaseReg`/`materializeFrameBaseRegister` 在 RA 前解决同一问题。
- 大帧用 `2032 + 余数` 拆成两段，spill 落在中间 SP 的 12 位范围内。
- VLA 用 `x8` 保存入口 SP，epilogue 从 `x8` 恢复固定帧；realign 用 `andi` 取整，epilogue 先回到未取整的帧底。
- stack protector 只靠两个 libcall 名字；frameaddress/returnaddress 是 live-in 拷贝；stacksave/restore 只是 SP 的读写。
- stackmap/patchpoint 没有编码，MiniRV 在 AsmPrinter 显式拒绝，避免静默错位。

## 自我检核

- [ ] 不看源码，画出 `vla` 的帧，标出入口 SP、当前 SP、`x8` 和 VLA 对象的位置。
- [ ] 解释 `alloca align 64` 的 prologue/epilogue 为什么必须用 `x8`，以及 `andi x2,x2,-64` 之后 `x8 - StackSize` 为什么仍能 reload spill。
- [ ] 说出 `getFirstSPAdjustAmount` 返回 2032 而不是 2048 的两个理由。
- [ ] 解释 `llvm.returnaddress` 在叶子和有调用两种情况下分别怎么取，以及 spill 的 kill 标记为什么特殊。
- [ ] 说出 MiniRV 拒绝 stackmap/patchpoint 的三个理由，以及为什么 `not --crash` 仍算“显式拒绝”。

## 延伸阅读

- **[LLVM Code Generator — Frame Lowering](https://llvm.org/docs/CodeGenerator.html#frame-lowering)** — LLVM Project
  - **读哪里**：`TargetFrameLowering` 的 `hasFP`、`getFrameIndexReference`、prologue/epilogue 职责，以及 `TargetRegisterInfo` 的 `eliminateFrameIndex`。
  - **能学到什么**：为什么帧基址解析要分成多个分支；target 需要覆写哪些虚函数。
  - **前提**：先读 Ch 11 和本章的主线 1–3。
- **[LLVM StackMaps](https://llvm.org/docs/StackMaps.html)** — LLVM Project
  - **读哪里**：`.llvm_stackmaps` 的格式、stackmap/patchpoint/statepoint 的语义、patchpoint 的调用序列要求。
  - **能学到什么**：为什么 stackmap 不是一个“可选优化”，而是需要 target 完整实现的运行时契约。
  - **前提**：知道 MachineInstr、AsmPrinter 和调用约定。
- **[LLVM Language Reference — frameaddress / returnaddress / stacksave](https://llvm.org/docs/LangRef.html#llvm-frameaddress-intrinsic)** — LLVM Project
  - **读哪里**：四个 intrinsic 的 depth 参数、返回值和“何时强制 frame pointer”的说明。
  - **能学到什么**：`frameaddress(0)` 为什么是入口 SP；depth>0 的帧链语义；stacksave/restore 只保证 SP。
  - **前提**：读完本章的主线 8。
- **[LLVM `StackProtector.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/StackProtector.cpp)** — LLVM Project
  - **读哪里**：canary 的插入、guard 的读取、返回前比较、`__stack_chk_fail` 的生成。
  - **能学到什么**：为什么 target 只需要提供 libcall 名字；哪些部分由通用 IR pass 完成。
  - **前提**：了解 IR pass 和 `TargetLowering::getLibcallName`。

→ [Ch 19 常量与位运算](./19-constants-and-bit-intrinsics.md)
