# Ch 10 — 栈帧与 prologue/epilogue

> **目标**：从 `MachineFrameInfo` 的抽象描述走到真实的 `addi sp, sp, -N`；能回答四个问题——N 由谁算、callee-saved 由谁选、`s0` 何时变成 frame pointer、超出 12 位立即数的 `%stack.N` 如何落地；并能用 MIR 与汇编证明你的判断。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `0fc5afa04c23`。本章所有“实测”均来自该构建；未跑过的明确标“未实测”。

> **前置**：Ch 05（CSR 名单与寄存器类）、Ch 09（参数分配与 outgoing argument area）。Ch 05 已经列过 `csr_ilp32d_lp64d` 的成员，本章不再重复；这里只问“这些保存动作如何变成栈上的字节”。

---

## 10.1 为什么维护者要读栈帧 lowering

栈帧是后端的“会计部门”：寄存器分配、调用约定、RVV、异常、stack protector 都往这里记账。它出错时的症状往往不在栈帧代码里：

| 症状 | 真实根因 | 先看哪里 |
|---|---|---|
| 函数返回后 `sp` 偏移 8/16 字节 | `stackSize` 与 prologue 实际减量不一致（RVV padding、split adjust） | `determineFrameLayout`、`getStackSizeWithRVVPadding` |
| `s0` 被调用者改写，调用者数据错 | `hasFP` 为真但 `ra`/`s0` 没进 `SavedRegs` | `determineCalleeSaves` |
| 大栈帧下 `ld` 的偏移被截断 | 超过 simm12，未生成 base register | `needsFrameBaseReg` / `eliminateFrameIndex` |
| 大 alloca 跨过 guard page | probe 未启用或 probe size 不对 | `hasInlineStackProbe`、`allocateStack` |
| 中断入口先改了 `sp` | shrink-wrapping 把 prologue 放错位置 | `enableShrinkWrapping`、`canUseAsPrologue` |

本章主战场是 `RISCVFrameLowering.cpp`（2,922 行）。它不负责“哪些寄存器属于 CSR”——那是 `RISCVCallingConv.td` 与 `RISCVRegisterInfo::getCalleeSavedRegs` 的事（Ch 05）——它负责把已经选好的保存集合排成字节。

---

## 10.2 先建立直觉：栈从高到低分区

RISC-V 的栈向下增长。一个非叶函数的典型布局如下（高地址在上）：

```
        调用者的 frame
   ┌──────────────────────────────┐
   │ incoming stack arguments      │  a0–a7 放不下的参数
   ├──────────────────────────────┤ ← 调用前的 sp（也是 callee 的 CFA）
   │ varargs save area             │  仅 varargs：寄存器参数的转存区
   ├──────────────────────────────┤ ← FP（s0），当 hasFP 为真
   │ callee-saved registers        │  ra、s0–s11、fs0–fs11 …（名单见 Ch 05）
   ├──────────────────────────────┤
   │ scalar locals / spill slots   │  标量对象，偏移是固定整数
   ├──────────────────────────────┤ ← MFI.getStackSize() 的底部
   │ RVV alignment padding         │  只对 RVV 有意义，可为零
   ├──────────────────────────────┤
   │ RVV objects                   │  stack-id: scalable-vector，大小 = vscale × 字节
   ├──────────────────────────────┤
   │ dynamic alloca / outgoing args│  有 VLA 时由 call site 调整 sp
   └──────────────────────────────┘ ← SP
```

三个容易混淆的量：

- `MFI.getStackSize()`：**固定部分**的字节数，包含 callee-saved 槽、varargs save area（若有）与标量局部变量；**不包含** scalable-vector 对象。
- `RVFI.getRVVStackSize()`：RVV 区域的“vscale 单位”大小；以 8 字节为一个 block 计数，prologue 再乘 `vlenb`。
- `RVFI.getRVVPadding()`：为了让 RVV 区域在标量区域下方正确对齐而插入的填充；它不计入 `stackSize`，但计入 `addi sp, sp, -N` 的 N。

`%stack.N` 的 `offset` 通常是负数；`getFrameIndexReference` 会把它换算成“相对于 SP 或 FP 的偏移”。同一个 `%stack.N` 在不同 `FrameReg` 下有不同的数值，这是读 MIR 时最常见的困惑来源。

---

## 10.3 谁决定 N：`determineFrameLayout` 与 split adjust

`emitPrologue` 的第一件正事是调用 `determineFrameLayout`（`RISCVFrameLowering.cpp:1040`）。函数本体在 533：

```cpp
uint64_t FrameSize = MFI.getStackSize();
if (RVFI->useQCIInterrupt(MF))
  FrameSize = std::max(FrameSize, QCIInterruptPushAmount);   // 96
Align StackAlign = getStackAlign();
FrameSize = alignTo(FrameSize, StackAlign);                  // 548
MFI.setStackSize(FrameSize);                                 // 551
```

对齐用的 `StackAlign` 来自 ABI（LP64D 为 16，ILP32E 为 4）。第二步才是 RVV padding（558–563）：

```cpp
if (RVFI->getRVVStackSize() && (!hasFP(MF) || TRI->hasStackRealignment(MF))) {
  int ScalarLocalVarSize = FrameSize - RVFI->getCalleeSavedStackSize() -
                           RVFI->getVarArgsSaveSize();
  if (auto RVVPadding = offsetToAlignment(ScalarLocalVarSize, RVFI->getRVVStackAlign()))
    RVFI->setRVVPadding(RVVPadding);
}
```

真正减到 SP 上的是 `getStackSizeWithRVVPadding`（569）：

```cpp
return alignTo(MFI.getStackSize() + RVFI->getRVVPadding(), getStackAlign());
```

所以**不要用 MIR 里的 `stackSize` 直接解释 `addi sp, sp, -N`**。标量例子恰好相等；RVV 例子会差一个 padding。10.7 的实测是 `stackSize: 32` 但 `addi sp, sp, -48`。

N 太大（不能塞进 simm12）时，`emitPrologue` 会拆成两阶段调整。`getFirstSPAdjustAmount`（2269）在 `!isInt<12>(StackSize) && CSI.size() > 0` 时返回一个“先把 callee-saved 偏移压进 12 位”的量；默认是 `2048 - StackAlign`（2331），有 Zca 时优先 496（`C.ADDI16SP` 的范围）。实测 `big()`：

```mir
stackSize:       5024
$x2 = frame-setup ADDI $x2, -2032
...
$x2 = frame-setup ADDI $x2, -2048
$x2 = frame-setup ADDI killed $x2, -944
```

2032 + 2048 + 944 = 5024。第一次 2032 = 2048 − 16，让后面的 `sd ra, 2024(sp)` 仍能用 12 位立即数。

---

## 10.4 谁决定保存哪些寄存器：`determineCalleeSaves`

`determineCalleeSaves`（1850）先调用 target-independent 版本，后者根据“函数体是否 clobber 某个 CSR”填 `SavedRegs`。RISC-V 在其上补了四条规则：

1. **向量寄存器按整组判断**（1855–1880）。generic 逻辑只看子寄存器是否被 clobber；RVV 的 `v24m2` 只有在 `v24` 与 `v25` 都被 clobber 时才应保存。代码先 `SavedRegs.reset(CSReg)`，再按 `MRI.def_empty` / `getUsedPhysRegsMask` 与 `TRI.subregs` 重新置位。
2. **有 FP 就强制保存 `ra` 与 `s0`**（1885–1889）。`ra` 不是 CSR 名单里的“普通成员”，但有 FP 时 epilogue 必须能回到调用者；`s0` 则因为被征用为 FP。
3. **有 BP 就保存 `bp`**（1890–1891）。BP 在 RISC-V 上是 `x9`（`RISCVBaseInfo.cpp:144`），不是 `s0`。
4. **push/pop 的配对约束**：`cm.push` 保存 `x26` 时必须同时保存 `x27`（1898–1900）。

保存集合只是“寄存器名单”；把名单变成栈槽的是 `assignCalleeSavedSpillSlots`（2336）。普通 CSR 走 `CreateStackObject(Size, Alignment, true)`，然后标记为 callee-saved object；RVV 寄存器额外设置 `stack-id: scalable-vector`（2435–2436）。MIR 里 `callee-saved-register: '$x1'` 这样的字段就是这一步的产物。

实测 `press()` 有 13 个 CSR 保存槽（具体名单见 Ch 05），每个 8 字节，加上 5 个真 spill 槽，得到 144：

```mir
frameInfo:
  stackSize:       144
stack:
  - { id: 0, type: spill-slot, offset: -112, size: 8, alignment: 8 }
  - { id: 5, type: spill-slot, offset: -8, size: 8, alignment: 8,
      callee-saved-register: '$x1' }
  - { id: 17, type: spill-slot, offset: -104, size: 8, alignment: 8,
      callee-saved-register: '$x27' }
```

汇编开头因此是：

```asm
addi    sp, sp, -144
sd      ra, 136(sp)
sd      s0, 128(sp)
...
```

`offset: -8` 的 `%stack.5` 在 SP 调整后落在 `sp + 136`；`getFrameIndexReference` 对 callee-saved 区间统一用 SP 作基址，并加上当前栈大小（1676–1681）。

---

## 10.5 `fp` 何时出现：`hasFPImpl` 的五个条件

`hasFP` 是 target-independent 的薄包装（`TargetFrameLowering.h:306`），实际判断在 `hasFPImpl`（493）：

```cpp
if (MF.disableFramePointerElim() || RegInfo->hasStackRealignment(MF) ||
    MFI.hasVarSizedObjects() || MFI.isFrameAddressTaken())
  return true;
if (MFI.isMaxCallFrameSizeComputed() && MFI.getMaxCallFrameSize() > 2047)
  return true;   // 512 行
```

五条触发路径分别对应：`"frame-pointer"="all"`（或 `-frame-pointer=all`）、栈需要动态重对齐、VLA/动态 alloca、`llvm.frameaddress`、以及**大 outgoing argument area**（> 2047 字节，给 scavenging emergency spill slot 留 FP 访问能力）。

`hasFP` 为真会带来两个连锁反应：

- `getReservedRegs` 把 `x8` 标成保留（`RISCVRegisterInfo.cpp:183-188`），寄存器分配器不会再把 `s0` 当普通 GPR。
- `determineCalleeSaves` 强制把 `ra` 与 `x8` 放进保存集合（1885–1889）。

实测三条路径：

```mir
# 1) VLA：hasVarSizedObjects
$x2 = frame-setup ADDI $x2, -32
$x8 = frame-setup ADDI $x2, 32
$x2 = frame-destroy ADDI $x8, -32

# 2) -frame-pointer=all：连叶子函数也建帧
stackSize:       16
framePointerPolicy: all
$x8 = frame-setup ADDI $x2, 16

# 3) 调用 300 个 long 参数：maxCallFrameSize 2336 > 2047
stackSize:       2368
maxCallFrameSize: 2336
$x8 = frame-setup ADDI $x2, 496
```

注意第三条的 `framePointerPolicy` 仍是 `none`；FP 是 `hasFPImpl` 因大 call frame 主动加的，不是属性要求。

BP 是另一个问题。`hasBP`（518）只在“需要动态重对齐，且（有 VLA 或 call frame 未预留）”时为真；它用 `x9`。实测 `both()`（VLA + 64 字节对齐）：

```asm
addi    s0, sp, 64
andi    sp, sp, -64
mv      s1, sp          # s1 = BP
...
andi    s3, a0, -64
mv      sp, s3
```

只有 `andi sp, sp, -64` 而没有 VLA 时（10.6 的 `ral()`），`hasBP` 为假，epilogue 直接用 `addi sp, s0, -128` 恢复。

---

## 10.6 动态 alloca 与 stack realignment

有 VLA 时，`MFI.hasVarSizedObjects()` 为真，`hasReservedCallFrame`（2206）返回 false：

```cpp
return !MFI.hasVarSizedObjects() &&
       !(hasFP(MF) && hasRVVFrameObject(MF));
```

于是 `eliminateCallFramePseudoInstr`（2212）不能再假设 outgoing argument area 已经在 prologue 预留，必须在每个 call site 用 `ADJCALLSTACKDOWN/UP` 调整 SP。`hasFP` 同时为真，epilogue 便不能靠常量 `addi sp, sp, N` 回退——它改用 FP：

```mir
$x2 = frame-destroy ADDI $x8, -32
```

`stack-realignment.ll` 是这条路径的正面测试：它为 16/32/64/128/256 字节对齐各准备一对函数，`callerN` 检查 `andi sp, sp, -N` + FP 恢复，`caller_no_realignN` 用 `"no-realign-stack"` 属性关掉重对齐，验证不会多生成 `andi`。本地跑这个文件：

```bash
$T/bin/llvm-lit -sv llvm/test/CodeGen/RISCV/stack-realignment.ll
# Passed: 1 (100.00%)
```

`ral()` 的汇编把顺序讲得很清楚：先按 ABI 对齐减 128、保存 `ra`/`s0`、把 FP 指向旧 SP，再 `andi sp, sp, -64` 把 SP 拉到 64 字节边界；epilogue 用 `addi sp, s0, -128` 一次回到保存区。

---

## 10.7 固定帧 vs 可伸缩帧：RVV 的两段式 prologue

标量 spill 的目标是 `TargetStackID::Default`，大小是编译期常量，进 `stackSize`。向量 spill 的目标是 `TargetStackID::ScalableVector`，大小是 `vscale × 字节`，不能进固定 `stackSize`。这条分界线由 `getStackIDForScalableVectors`（2790）给普通 spill slot 打标、`assignCalleeSavedSpillSlots`（2435–2436）给 RVV CSR 打标，再由 `assignRVVStackObjectOffsets`（1968）统一排偏移。

实测一个 6 个 `vint32m1_t` 跨调用存活的函数：

```mir
stackSize:       32
stack:
  - { id: 0, type: spill-slot, offset: -8, size: 8, alignment: 8,
      stack-id: scalable-vector }
  ...
  - { id: 5, type: spill-slot, offset: -48, size: 8, alignment: 8,
      stack-id: scalable-vector }
```

`stackSize` 只有 32（标量部分），但 prologue 是两段：

```asm
addi    sp, sp, -48
sd      ra, 40(sp)
csrr    a0, vlenb
li      a1, 6
mul     a0, a0, a1
sub     sp, sp, a0
```

- 第一段 48 = `alignTo(32 + RVVPadding(8), 16)`；`stackSize` 与 `addi` 的差就是 padding。
- 第二段 `6` = `RVVStackSize / RVVBytesPerBlock` = 48 / 8；`vlenb` 是“一个 vscale 的字节数”，`mul` 后就是 6 个 vector block 的实际字节数。`adjustReg` 的 scalable 分支在 `RISCVRegisterInfo.cpp:252-335`，`PseudoReadVLENB` 在 315。
- epilogue 对称：`add sp, sp, a0`，再 `ld ra, 40(sp)`，最后 `addi sp, sp, 48`。

为什么不能把 RVV 大小直接加进 `stackSize`？因为 `vlenb` 在编译期未知；`MachineFrameInfo` 的 `stackSize` 是 `uint64_t` 常量，无法表达 `vscale`。所以 RVV 区域用独立的 `StackID` + `StackOffset::getScalable` 表示，`getFrameIndexReference` 的 scalable 分支（1830–1836）把它换算成 `(scalarLocalVarSize, RVVStackSize)` 的二元组。

---

## 10.8 stack clash protection：真实开关名与 probe 形状

在本树执行 `grep -rn "stack-clash\|StackProbe" llvm/lib/Target/RISCV/`，能看到的开关不是 `-stack-clash-protection` 这个 driver 选项，而是两个函数属性：

- `"probe-stack"`：只有值为 `"inline-asm"` 时 `hasInlineStackProbe` 才为真（`RISCVISelLowering.cpp:31203-31211`）。
- `"stack-probe-size"`：默认 4096，按栈对齐向下取整（31213-31222；`RISCVMachineFunctionInfo.cpp:39-40`）。

`emitPrologue` 在 1125 取这两个值，把 probe 决策交给 `allocateStack`（842）：

- `Offset <= ProbeSize`：只减 SP，不 probe；若 `DynAllocation` 为真，再补一条 `sd zero, 0(sp)`。
- `ProbeSize < Offset < 5 * ProbeSize`（879）：展开成若干次 `sub sp, sp, ProbeSize; sd zero, 0(sp)`，余数单独减。
- 更大：生成 `PROBED_STACKALLOC` 伪指令，由 `inlineStackProbe`（2872）与 `emitStackProbeInline`（2795）展开成循环。

用 clang 的 `-fstack-clash-protection` 实测 16 KiB 的 `volatile` 缓冲区：

```asm
addi    sp, sp, -496      # 随后保存 ra/s0，此处省略
lui     a0, 1
sub     sp, sp, a0
sd      zero, 0(sp)      # 同样的 4096 字节 probe 再两次
addi    sp, sp, -2048
addi    sp, sp, -1584
```

496 是 `getFirstSPAdjustAmount` 选出的“先减一点，让 CSR 保存偏移可用压缩指令”的量；随后 3 次 4096 的 probe 覆盖 12288 字节，余下 2048 + 1584 = 3632，总计 16416。这个例子中 `16416 < 5 * 4096`，所以走展开路径，没有出现 `PROBED_STACKALLOC` 循环。

---

## 10.9 shrink-wrapping：`interrupt` 测试为什么存在

shrink-wrapping 允许把 prologue 从 entry 下沉到真正需要栈的 block。RISC-V 在 `enableShrinkWrapping`（2707）里只排除两类函数：

- `optnone`：保持调试时的传统代码流；
- QCI / SiFive CLIC 中断：它们的入口序列（`qc.c.mienter`、`csrrw sp, sf.mscratchcsw, sp`）必须先于一切 handler 代码。

`canUseAsPrologue`（2721）再加两条限制：在 `preferVsetvliOverReadVLENB()` 时，若 `VTYPE`/`VL` 是 live-in，prologue 用 `vsetvli` 读 VLEN 会 clobber 它们；若使用 `__riscv_save` libcall，`t0`（`x5`）必须可用。`canUseAsEpilogue`（2749）则要求 restore 能安全地做成 tail call。

`interrupt-shrink-wrap.ll` 的存在就是为了锁住“**标准中断可以 shrink-wrap，但专用中断不行**”这个边界。实测 `machine` 中断：

```asm
machine:
    beqz    tp, .LBB0_2
    mret
.LBB0_2:
    addi    sp, sp, -128
    sd      ra, 120(sp)
    sd      t0, 112(sp)
    ...
```

返回路径 `mret` 完全不碰栈；只有 `tp == 0` 的 cold block 才建帧。`interrupt` 属性让 cold block 保存 16 个 caller-saved 寄存器（共 128 字节；名单见 Ch 05），这是 CSR 规则在栈帧上的直接结果。QCI/SiFive 的测试则检查入口序列仍然是第一条指令。

---

## 10.10 `eliminateFrameIndex`：从 `%stack.N` 到 `sd s0, 8(sp)`

`RISCVRegisterInfo::eliminateFrameIndex`（`RISCVRegisterInfo.cpp:583`）是最后一步。它先问 `getFrameLowering(MF)->getFrameIndexReference`（599）要“基址寄存器 + 偏移”，然后处理立即数：

```cpp
int64_t Val = Offset.getFixed();              // 610
int64_t Lo12 = SignExtend64<12>(Val);         // 611
...
MI.getOperand(FIOperandNum + 1).ChangeToImmediate(Lo12);
Offset = StackOffset::get((uint64_t)Val - (uint64_t)Lo12, Offset.getScalable());
```

小偏移的形状就是 `sd s0, 8(sp)`；`press()` 的 `%stack.3`（offset −136，`stackSize: 144`）落在 `sp+8`，只是分配到的寄存器是 `a0`，所以实际是 `sd a0, 8(sp)`。若剩余 `Offset` 非零（665），调用 `adjustReg` 生成一条或多条 `addi`/`lui+add`（671），并把 FI 操作数改成该临时寄存器（673）。所以 `%stack.N` 的最终形态可能是：

- 偏移在 simm12 内：直接 `sd sX, off(sp)` / `ld sX, off(fp)`；
- 偏移超出：先 `addi t0, sp, ...`，再 `sd sX, rest(t0)`；
- RVV 对象：`addi` 之外还要 `csrr vlenb` / `mul`。

**超出 12 位时谁决定要 base register？** 是 `needsFrameBaseReg`（733），由 `LocalStackSlotAllocation` 在 PEI 之前调用（`LocalStackSlotAllocation.cpp:358`）。它只对 I/S 格式且 `mayLoad()/mayStore()` 的指令返回真；先估算 FP 相对偏移（扣除 callee-saved 大小，774），再估算 SP 相对偏移（`Offset + 128 + localFrameSize`，788），两者都不能编码才要求虚拟 base。随后 `materializeFrameBaseRegister`（812）插入 `ADDI %stack.N, offset`，`resolveFrameIndex`（832）把原指令改成基于该寄存器。

实测 `big()`：5000 字节的 `buf`，`stackSize: 5024`，`buf` 的 `%stack.0` 偏移 −5016，即 `sp + 8`。访问 `buf[4999]` 时：

```mir
# -stop-after=localstackalloc
%0:gpr = ADDI %stack.0.buf, 0
%1:gpr = ADDI %0, 2047
...
%2:gpr = LBU %1, 953
```

最终汇编：

```asm
addi    a0, sp, 8
addi    s0, a0, 2047
lbu     a0, 953(s0)
```

8 + 2047 + 953 = 3008，正好是 `buf[3000]` 的地址（`buf` 起点在 `sp+8`）。这里的 `s0` 不是 FP，只是分配器挑中的临时 GPR；`hasFP` 在这个函数里为假。

RV32 的 `!isInt<32>` 范围检查（605）未实测：本机只跑了 riscv64。

---

## 10.11 源码落点

- **`llvm/lib/Target/RISCV/RISCVFrameLowering.cpp`**（2,922 行）
  - 493–574：`hasFPImpl`、`hasBP`、`determineFrameLayout`、`getStackSizeWithRVVPadding`；512 大 call frame，548 对齐，558–563 RVV padding。
  - 715–762、842–955：`allocateAndProbeStackForRVV`、`allocateStack`；856 小帧直减，879 展开 probe，933 伪指令路径。
  - 1005–1497：`emitPrologue`/`emitEpilogue`；1040 布局，1064 `RealStackSize`，1078 split adjust，1130 分配，1168–1180 FP，1201–1211 RVV，1344 `RestoreSPFromFP`。
  - 1631–1837：`getFrameIndexReference`；1652 固定偏移，1655 scalable，1676 CSR 走 SP，1730–1753 FP/BP，1830–1836 RVV。
  - 1850–2038：`determineCalleeSaves` 与 `assignRVVStackObjectOffsets`；1855 向量规则，1885 FP，1890 BP。
  - 2040–2101：`getScavSlotsNumForRVV`、`hasRVVFrameObject`。
  - 2141–2332：`processFunctionBeforeFrameFinalized`、`hasReservedCallFrame`、`eliminateCallFramePseudoInstr`、`getFirstSPAdjustAmount`；2169 `isInt<11>`，2331 默认 `2048 - StackAlign`。
  - 2336–2479：`assignCalleeSavedSpillSlots`；2435–2436 RVV 的 `StackID`。
  - 2707–2904：`enableShrinkWrapping`、`canUseAsPrologue/Epilogue`、probe 展开；2920 `getStackThreshold`。

- **`llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp`**（1,216 行）
  - 165–226：`getReservedRegs`；183–188 在 `hasFP`/`hasBP` 时保留 `x8`/`x9`。
  - 236–448：`adjustReg`；252–335 scalable 分支，315 `PseudoReadVLENB`。
  - 583–722：`eliminateFrameIndex`；599 取基址，611 低 12 位，665 剩余偏移，671 `adjustReg`。
  - 733–848：`needsFrameBaseReg`、`isFrameOffsetLegal`、`materializeFrameBaseRegister`、`resolveFrameIndex`；774/788 两处偏移估算。
  - 860–863：`getFrameRegister`，`hasFP ? X8 : X2`。

- **`llvm/lib/CodeGen/LocalStackSlotAllocation.cpp`**：358 调 `needsFrameBaseReg`，449 物化 base，466 `resolveFrameIndex`。
- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**：31203–31211 `hasInlineStackProbe`，31213–31222 `getStackProbeSize`。
- **测试**：`stack-realignment.ll`（16–256 字节对齐）、`interrupt-shrink-wrap.ll`（入口顺序）、`rvv/rv64-spill-vector.ll`（RVV spill）。

---

## 10.12 可跑实验

以下命令均可直接复制；`T` 先按 Ch 00 设置。

### 实验 1：标量 spill 的固定帧

```bash
cd /tmp && cat > spill.c <<'EOF'
extern long g[32]; extern void bar(void);
long press(void) {
  long a0=g[0],a1=g[1],a2=g[2],a3=g[3],a4=g[4],a5=g[5],a6=g[6],a7=g[7];
  long a8=g[8],a9=g[9],a10=g[10],a11=g[11],a12=g[12],a13=g[13],a14=g[14],a15=g[15];
  bar(); g[16] = a0 + a15;
  return a0+a1+a2+a3+a4+a5+a6+a7+a8+a9+a10+a11+a12+a13+a14+a15;
}
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 \
  -S -emit-llvm -o spill.ll spill.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c \
  -stop-after=prolog-epilog -simplify-mir spill.ll -o spill.mir
grep -E 'stackSize|callee-saved-register' spill.mir | head
```

真实输出：`stackSize: 144`，13 个 `callee-saved-register`；汇编首行 `addi sp, sp, -144`，`sd ra, 136(sp)`。

### 实验 2：RVV 的可伸缩帧

```bash
cd /tmp && cat > vspill.c <<'EOF'
#include <riscv_vector.h>
extern int32_t g[4096*6]; extern void bar(void);
vint32m1_t vpress(void) {
  size_t vl = __riscv_vsetvlmax_e32m1();
  vint32m1_t a0 = __riscv_vle32_v_i32m1(g + 0*4096, vl);
  vint32m1_t a1 = __riscv_vle32_v_i32m1(g + 1*4096, vl);
  vint32m1_t a2 = __riscv_vle32_v_i32m1(g + 2*4096, vl);
  vint32m1_t a3 = __riscv_vle32_v_i32m1(g + 3*4096, vl);
  vint32m1_t a4 = __riscv_vle32_v_i32m1(g + 4*4096, vl);
  vint32m1_t a5 = __riscv_vle32_v_i32m1(g + 5*4096, vl);
  bar();
  vint32m1_t s = __riscv_vadd_vv_i32m1(a0, a1, vl);
  s = __riscv_vadd_vv_i32m1(s, a2, vl);
  s = __riscv_vadd_vv_i32m1(s, a3, vl);
  s = __riscv_vadd_vv_i32m1(s, a4, vl);
  s = __riscv_vadd_vv_i32m1(s, a5, vl);
  return s;
}
EOF
# 无 C 库头文件时用 clang 自带头
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gcv -mabi=lp64d -O1 \
  -ffreestanding -nostdinc -isystem $T/lib/clang/24/include \
  -S -emit-llvm -o vspill.ll vspill.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c,+v \
  -stop-after=prolog-epilog -simplify-mir vspill.ll -o vspill.mir
grep -c 'stack-id: scalable-vector' vspill.mir       # 6
grep 'stackSize' vspill.mir                         # 32
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c,+v vspill.ll -o - \
  | sed -n '/^vpress:/,/sub.*sp, sp, a0/p'
```

真实输出：`stackSize: 32`，6 个 scalable 槽；prologue 为 `addi sp, sp, -48` → `csrr a0, vlenb` → `li a1, 6` → `mul` → `sub sp, sp, a0`。

### 实验 3：三条 FP 触发路径

```bash
cd /tmp && cat > leaf.c <<'EOF'
long leaf(long x) { return x + 1; }
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 \
  -S -emit-llvm -o leaf.ll leaf.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -frame-pointer=all \
  -stop-after=prolog-epilog -simplify-mir leaf.ll -o - | grep -E 'stackSize|x8 = frame-setup'
```

真实输出：`stackSize: 16`，`$x8 = frame-setup ADDI $x2, 16`。把 `leaf.c` 换成调用 300 个 `long` 参数的函数，可见 `maxCallFrameSize: 2336` 与 `$x8 = frame-setup ADDI $x2, 496`。VLA 版本则在 10.6 已给出 `$x8 = frame-setup ADDI $x2, 32`。

### 实验 4：stack realignment 与 BP

```bash
cd /tmp && cat > both.c <<'EOF'
extern void sink(void *);
long both(long n) {
  char buf[n] __attribute__((aligned(64)));
  sink(buf); buf[0] = 1; return buf[0] + n;
}
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 -S -o both.s both.c
sed -n '/^both:/,/\.Lfunc_end/p' both.s | head -24
```

真实输出含 `addi s0, sp, 64`、`andi sp, sp, -64`、`mv s1, sp`；`s1` 就是 BP（`x9`）。

### 实验 5：stack clash probe

```bash
cd /tmp && cat > probe.c <<'EOF'
extern void sink(void *);
long probe(void) {
  volatile char buf[16384];
  buf[0] = 1; buf[16383] = 2; sink((void *)buf);
  return buf[0] + buf[16383];
}
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 \
  -fstack-clash-protection -S -o probe.s probe.c
sed -n '/^probe:/,/\.Lfunc_end/p' probe.s | head -20
```

真实输出：`addi sp, sp, -496`，随后 3 次 `lui a0, 1; sub sp, sp, a0; sd zero, 0(sp)`，再 `addi sp, sp, -2048; addi sp, sp, -1584`。

### 实验 6：>12 位偏移的 base register

```bash
cd /tmp && cat > big.c <<'EOF'
extern void sink(void *);
long big(void) {
  char buf[5000]; sink(buf); buf[0] = 1; buf[4999] = 2;
  return buf[0] + buf[4999] + buf[3000];
}
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 \
  -S -emit-llvm -o big.ll big.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c \
  -stop-after=localstackalloc -simplify-mir big.ll -o - | grep -E 'ADDI|LBU'
```

真实输出：`%0:gpr = ADDI %stack.0.buf, 0`、`%1:gpr = ADDI %0, 2047`、`%2:gpr = LBU %1, 953`。完整汇编见 10.10。

---

## 10.13 踩雷集锦

1. **拿 `stackSize` 当 `addi` 的立即数。** RVV padding、`reservedSpillsSize`、split adjust 都会让它不相等。永远用 `getStackSizeWithRVVPadding` 和 `getFirstSPAdjustAmount` 重算。
2. **把 RVV 对象算进 `stackSize`。** 它的大小是 `vscale` 的函数，只能是 `StackOffset::getScalable`；写成固定字节会在 VLEN 不同的机器上错位。
3. **以为 `hasFP` 只由 VLA 触发。** 大 call frame（>2047）、`frame-pointer=all`、`llvm.frameaddress`、realignment 都会触发；MIR 里看 `framePointerPolicy` 不够，要看实际的 `$x8 = ADDI $x2, ...`。
4. **以为有 FP 就一定有 BP。** `hasBP` 还要求“重对齐 + VLA/未预留 call frame”；只重对齐的 `ral()` 没有 BP。
5. **以为 `eliminateFrameIndex` 什么指令都能处理。** `needsFrameBaseReg` 只对 I/S 格式的 load/store 生效；`ADDI` 走 616–623 的专门分支，prefetch 还有 32 字节对齐约束。
6. **只加 `-fstack-clash-protection` 却检查 IR 属性。** 后端只认 `"probe-stack"="inline-asm"`；若手写 IR 忘了这个属性，`hasInlineStackProbe` 直接返回 false。
7. **在 `interrupt` 函数上随意改 shrink-wrapping。** QCI/SiFive 的入口序列必须先执行；`enableShrinkWrapping` 的排除不是优化偏好，是正确性。
8. **改 `determineCalleeSaves` 的向量规则时只测单寄存器。** `v24m2` 需要 `v24` 与 `v25` 同时被 clobber 才保存；只 reset 一部分会让 prologue 少存半个寄存器组。

---

## 10.14 本章重点整理

- N 的公式是 `alignTo(MFI.getStackSize() + RVVPadding, StackAlign)`；`stackSize` 只是输入之一。
- 大帧会拆成两次 SP 调整，默认首段 `2048 - StackAlign`（Zca 可到 496），目的是让 CSR 保存偏移落在 simm12。
- `determineCalleeSaves` 先跑 generic，再补 RVV 整组规则、FP 强制的 `ra`/`s0`、BP 的 `x9`。
- `hasFPImpl` 五条件：禁消除、重对齐、VLA、取帧地址、`maxCallFrameSize > 2047`。
- FP 是 `s0`（`x8`），BP 是 `s1`（`x9`）；`getFrameRegister` 返回 `hasFP ? X8 : X2`。
- RVV 用 `TargetStackID::ScalableVector` 独立记账；prologue 第二段是 `csrr vlenb` + `mul` + `sub sp, sp, a0`。
- stack clash 的开关是函数属性 `probe-stack=inline-asm` 与 `stack-probe-size`，不是单独的 target feature。
- 超出 simm12 的 FI 由 `needsFrameBaseReg` 判定、`materializeFrameBaseRegister` 物化 base、`eliminateFrameIndex` 收尾。

---

## 10.15 自我检核

- [ ] 能解释为什么 `stackSize: 32` 的函数会生成 `addi sp, sp, -48`
- [ ] 能说出 `getFirstSPAdjustAmount` 存在的两个理由（simm12 与压缩指令）
- [ ] 给定一个 VLA 函数，能指出 `hasFP`、`hasBP`、`hasReservedCallFrame` 各自的返回值
- [ ] 能解释 `frame-pointer=all` 与 `maxCallFrameSize > 2047` 是两条独立的 FP 触发路径
- [ ] 能在 MIR 里区分 `stack-id: scalable-vector` 与普通 `spill-slot`
- [ ] 能写出 RVV prologue 的第二段三条指令，并解释 `20` 从哪来
- [ ] 能说出 `probe-stack` 的合法值、4096 默认来源，以及 `%stack.N` 超出 12 位时为什么需要 `needsFrameBaseReg`

---

## 10.16 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVFrameLowering.cpp`**：493–574、1005–1497、1631–1837、2141–2332；本章所有“谁决定 N / 谁决定保存 / FP 何时出现”的原始答案。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp`** 与 **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/LocalStackSlotAllocation.cpp`**：165–226、236–448、583–848、340–470；保留寄存器、scalable 调整、FI 消除与 base register 时机。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/stack-realignment.ll`** 与 **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/interrupt-shrink-wrap.ll`**：`caller64`/`caller_no_realign64` 对照；入口序列顺序是正确性问题。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rvv/rv64-spill-vector.ll`**：`stack-id: scalable-vector` 与 prologue 的 CHECK；RVV 栈对象的现成回归样本。

---

→ 下一章：[Ch 11 — 代码模型、TLS 与 GP relaxation](./11-code-models-tls-gp-relaxation.md)
