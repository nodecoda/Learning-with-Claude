# Ch 10 — 寄存器类与调用约定

> **目标**：能把 `MiniRVRegisterInfo.td` 的 `GPR`、`CSR_MiniRV`、`reservedRegs` 与 `CC_MiniRV`/`RetCC_MiniRV` 对应起来，读懂 9 个参数的函数在 `minirv64`/`riscv64` 上分别落在哪些寄存器和栈槽，并说清 `CallingConv.td` 与 `LowerCall`/`LowerFormalArguments` 的分工。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`；本文命令均在该构建实跑。

## 为什么需要这个？

寄存器分配器面对的是虚拟寄存器，它需要知道“这个值能放进哪些物理寄存器”。调用约定面对的是 IR 参数，它需要知道“第 9 个 `i64` 是进寄存器还是进栈”。这两件事看起来都属于“后端细节”，其实是一条链上的两个接口：

- `RegisterClass` 定义“哪些物理寄存器可以互相替代”，以及它们的合法值类型、对齐、分配顺序。
- `CalleeSavedRegs`/`reservedRegs` 定义“哪些寄存器不能被随意用、调用前后谁负责保存”。
- `CallingConv.td` 定义“参数/返回值按什么规则分配到寄存器或栈”。
- `LowerFormalArguments`/`LowerCall`/`LowerReturn` 把前三条的结论变成 DAG 里的 COPY/LOAD/STORE/CALL。

少任何一环，调用双方就会对“第 9 个参数在哪”产生分歧。这种 bug 不会在 `llc` 里报错，只会在运行时读到错误的值，所以必须从表到汇编逐层验证。

## 先建立直觉

把调用想成两个人交接货物：调用者按清单把前 8 件放进 `x10..x17` 八个格子，第 9 件放在栈顶 `0(x2)`；被调用者按同一张清单去同样的格子取货。

```text
caller（sum9 的调用者）                    callee（sum9）
  %a0..%a7 虚拟寄存器                        liveins: $x10..$x17
        │                                         ▲
        │ CCState.AnalyzeCallOperands(CC_MiniRV)  │ CCState.AnalyzeFormalArguments
        ▼                                         │
  CCValAssign 列表                                │
  [Reg x10][Reg x11]...[Reg x17][Mem 0]           │
        │                                         │
        ├─ RegLoc → CopyToReg $x10..$x17 ─────────┤
        └─ MemLoc → SD val, 0($x2) ───────────────┘
                          │
                    PseudoCALL @sum9
                    implicit $x10..$x17, $x1
```

寄存器文件本身也要先有地图：

```text
x0 zero(保留)   x1 ra(CSR)      x2 sp(保留)
x3 gp           x4 tp           x5-x6 scratch(保留)   x7 t2
x8 fp(hasFP 时保留，否则 s0)    x9 s1(CSR)
x10-x17 a0-a7   x18-x27 s2-s11(CSR)   x28-x31 t3-t6
```

MiniRV 的 GPR 只有这一张表；RISC-V 还有 `f0-f31` 的 `FPR32`/`FPR64`，以及把同一个 X 寄存器看成 `f32` 的 `GPRF32`。这是后面比较的重点。

## 主线 1：寄存器类与物理寄存器

### MiniRV：一个 `GPR` 打天下

`MiniRVRegisterInfo.td` 只有 26 行，核心是三段：

```tablegen
foreach i = 0-31 in {
  def X#i : MiniRVReg<i, "x"#i>;
}

def GPR : RegisterClass<"MiniRV", [i64], 64, (sequence "X%u", 0, 31)>;

def CSR_MiniRV : CalleeSavedRegs<(add X1, (sequence "X%u", 8, 9),
                                     (sequence "X%u", 18, 27))>;
```

- `MiniRVReg` 给每个寄存器 5 位硬件编码和 DWARF number，`Namespace = "MiniRV"` 让生成的名字是 `MiniRV::X10`。
- `GPR` 的合法值类型只有 `i64`，对齐 64，包含 `x0..x31`。注意“包含”不等于“可分配”：`x0/x2/x5/x6` 会在 `getReservedRegs` 里被排除，`x8` 在需要 frame pointer 时也会被排除。
- `CSR_MiniRV` 是 callee-saved 集合：`x1, x8, x9, x18-x27`，和 RISC-V 的 `CSR_ILP32_LP64` 完全一致。注意 `x1` 不在 psABI 的 callee-saved 集合里（`ra` 是 caller-saved）；把它列进来，是让 callee-saved 机制在函数调用别人、需要保留自己的 `ra` 时自动 spill 它。

### RISC-V：一个 `GPR` 类，多个“视图”和子类

RISC-V 的 `RISCVRegisterInfo.td` 复杂得多，但骨架相同：

```tablegen
def GPR : GPRRegisterClass<(add (sequence "X%u", 10, 17),
                                (sequence "X%u", 5, 7),
                                (sequence "X%u", 28, 31),
                                (sequence "X%u", 8, 9),
                                (sequence "X%u", 18, 27),
                                (sequence "X%u", 0, 4))>;
```

注释写得很清楚：这个顺序代表优先分配顺序，依次是 caller-save、callee-save、特殊寄存器。TableGen 的声明顺序是 `a0-a7`、`t0-t2`、`t3-t6`、`s0-s1`、`s2-s11`、`x0-x4`；但运行时 `RegisterClassInfo::compute` 还会把 CSR alias 再挪到最后，所以实际 `AllocationOrder` 不完全等于这段声明顺序。MiniRV 的 `GPR` 是朴素的 `x0..x31`，同样会被重排——后面 `-debug-only=regalloc` 会看到。

RISC-V 的寄存器类分三组：

| 类 | 物理寄存器 | 值类型 | 用途 |
|---|---|---|---|
| `GPR` | `x0-x31` | `XLenVT`、`XLenFVT`、P 扩展 packed 类型 | 整数、指针、软浮点/地址 |
| `FPR32` | `f0-f31`（`F0_F` 视图） | `f32` | 单精度浮点 |
| `FPR64` | `f0-f31`（`F0_D` 视图） | `f64` | 双精度浮点 |
| `FPR16` | `f0-f31`（`F0_H` 视图） | `f16/bf16` | 半精度 |
| `GPRF32` | `x0-x31`（`X0_W` 视图） | `f32` | Zfinx：浮点值放整数寄存器 |
| `GPRF16` | `x0-x31`（`X0_H` 视图） | `f16` | Zfinx 的半精度 |

`GPRF32` 不是“另一组寄存器”，而是同一个 `x10` 的 32 位子寄存器视图 `X10_W`。`FPR32`/`FPR64` 才是独立的浮点寄存器文件，`f10` 的 `F10_F` 和 `F10_D` 是同一个物理寄存器的不同宽度视图。理解这一点，才能读懂 `-mattr=+d` 时为什么参数从 `a0` 变成 `fa0`。

RISC-V 还定义了大量子类：`GPRNoX0`、`GPRNoX2`、`SP`、`GPRJALR`、`GPRC`（压缩指令可用的 `x8-x15`）、`GPRTC`（尾调用可用的 caller-save）、`GPRPair`（一对寄存器）。这些子类不是“为了好看”，而是让 instruction pattern 和寄存器分配器能表达“这条指令只能用某个子集”。MiniRV 的语料小，一个 `GPR` 就够了；你的 target 一旦有“某条指令不能用 x0/sp”或“压缩指令只能用 x8-x15”，就需要同样的子类。

## 主线 2：CalleeSaved、reserved、frame、ra、pointer、stack

### `CalleeSavedRegs`

MiniRV 的 `getCalleeSavedRegs` 直接返回 `CSR_MiniRV_SaveList`：

```cpp
const MCPhysReg *
MiniRVRegisterInfo::getCalleeSavedRegs(const MachineFunction *MF) const {
  return CSR_MiniRV_SaveList;
}
```

RISC-V 则按调用约定和 ABI 选：

```cpp
if (MF->getFunction().getCallingConv() == CallingConv::GHC)
  return CSR_NoRegs_SaveList;
if (MF->getFunction().hasFnAttribute("interrupt")) { ... }
...
if (RISCVABI::isRVE(ABI))
  return CSR_ILP32E_LP64E_SaveList;
if (RISCVABI::isSoftFP(ABI))
  return CSR_ILP32_LP64_SaveList;
if (RISCVABI::isSingleFP(ABI))
  return CSR_ILP32F_LP64F_SaveList;
return CSR_ILP32D_LP64D_SaveList;
```

`lp64d` 的 `CSR_ILP32D_LP64D_SaveList` 在整数集合之外加了 `f8/f9/f18-f27`（`F8_D`、`F9_D`……）。也就是说，**ABI 决定 callee-saved 集合**：软浮点只保存整数寄存器，硬浮点还要保存浮点 callee-saved。MiniRV 没有浮点寄存器，所以永远只有整数集合。

`getCallPreservedMask` 是同一张表的 regmask 版本，给 `PseudoCALL` 用；MiniRV 返回 `CSR_MiniRV_RegMask`，RISC-V 按 ABI 返回 `CSR_ILP32D_LP64D_RegMask` 等。MIR 里的 `csr_minirv` 就是这张掩码。

### `reservedRegs`

MiniRV 的保留集合：

```cpp
Reserved.set(MiniRV::X0); // zero
Reserved.set(MiniRV::X2); // stack pointer
Reserved.set(MiniRV::X5); // 大栈帧地址物化的 scratch
Reserved.set(MiniRV::X6); // 栈调整的 scratch
if (MF.getSubtarget().getFrameLowering()->hasFP(MF))
  Reserved.set(MiniRV::X8);
```

- `x0` 恒零，不能分配。
- `x2` 是栈指针，不能分配。
- `x5/x6` 是 frame lowering 的 scratch：大偏移地址物化、prologue/epilogue 的 `addi` 源/目的重叠时用。保留它们是为了让寄存器分配器不要把活值放在这里。
- `x8` 只在 `hasFP(MF)` 时保留：需要 frame pointer 的函数用它，不需要的函数可以把它当普通 `s0` 用。这比“永远保留 fp”更省寄存器。

RISC-V 的 `getReservedRegs` 逻辑更长：`x0`、`x2`、`x3`（gp）、`x4`（tp）、`hasFP` 时的 `x8`、`hasBP` 时的 base pointer、RVE 的 `x16-x31`、向量 CSR（`VL`/`VTYPE`/`VXSAT`/`VXRM`/`VLENB`）、浮点环境（`FRM`/`FFLAGS`）等。它用 `markSuperRegs` 把别名一起保留，避免只保留 `X10` 却漏了 `X10_W`/`X10_H` 这种子寄存器。

### `getFrameRegister`

MiniRV 和 RISC-V 的实现几乎一样：

```cpp
Register MiniRVRegisterInfo::getFrameRegister(const MachineFunction &MF) const {
  if (MF.getSubtarget().getFrameLowering()->hasFP(MF))
    return MiniRV::X8;
  return MiniRV::X2;
}
```

有 frame pointer 就用 `x8`，否则用 `sp`。这个返回值会被 `eliminateFrameIndex` 用来把 frame index 换成 `base + offset`。Ch 11 会讲 `hasFP` 的判定和 `getFrameIndexReference` 的特例。

### `getRARegister`、`getPointerReg`、`getStackRegister`

这三个名字容易让人误会，因为它们在 LLVM 24 里的位置和你想的不一样：

- **`getRARegister()`** 不在 `TargetRegisterInfo`，而在 `MCRegisterInfo`。它的值由生成的寄存器信息构造函数传入：MiniRV 写 `MiniRVGenRegisterInfo(MiniRV::X1)`，RISC-V 写 `RISCVGenRegisterInfo(RISCV::X1, ...)`。所以 `getRARegister()` 返回 `x1`，不需要你 override。
- **`getPointerReg()`** 是旧版接口里的名字；当前 `TargetRegisterInfo` 里没有这个 virtual。指针的寄存器类由 `TargetLowering::addRegisterClass(MVT::i64, &MiniRV::GPRRegClass)` 和 DataLayout 的 pointer 类型共同决定；`getRegClassFor(MVT::i64)` 就能拿到 `GPR`。
- **`getStackRegister()`** 也是旧接口里的名字。现在栈指针通过 `TargetLowering::setStackPointerRegisterToSaveRestore(MiniRV::X2)` 告诉通用代码；RISC-V 同样写 `setStackPointerRegisterToSaveRestore(RISCV::X2)`。`getFrameRegister` 则负责“消除 frame index 时用哪个基址”。

如果你读的是 LLVM 10 时代的教程，看到 `getPointerReg`/`getStackRegister` 不要照抄；先确认当前 `TargetRegisterInfo` 的 virtual 列表。

## 主线 3：`CallingConv.td` 只分配位置

MiniRV 的 `MiniRVCallingConv.td` 全部内容只有两个 record：

```tablegen
def CC_MiniRV : CallingConv<[
  CCIfType<[i32], CCPromoteToType<i64>>,
  CCIfType<[i64], CCAssignToReg<[X10, X11, X12, X13, X14, X15, X16, X17]>>,
  CCIfType<[i64], CCAssignToStack<8, 8>>
]>;

def RetCC_MiniRV : CallingConv<[
  CCIfType<[i32], CCPromoteToType<i64>>,
  CCIfType<[i64], CCAssignToReg<[X10, X11]>>
]>;
```

TableGen 生成的是 `CC_MiniRV`/`RetCC_MiniRV` 两个 `CCAssignFn`。对 `CC_MiniRV`，生成的逻辑等价于：

```cpp
if (LocVT == MVT::i32) {
  LocVT = MVT::i64;
  LocInfo = ArgFlags.isSExt() ? CCValAssign::SExt
         : ArgFlags.isZExt() ? CCValAssign::ZExt
                             : CCValAssign::AExt;
}
if (LocVT == MVT::i64) {
  if (Reg = State.AllocateReg({X10,...,X17}))
    return getReg(ValNo, ValVT, Reg, LocVT, LocInfo);
}
if (LocVT == MVT::i64) {
  int64_t Offset = State.AllocateStack(8, Align(8));
  return getMem(ValNo, ValVT, Offset, LocVT, LocInfo);
}
```

三个规则按顺序匹配：

1. `i32` 先提升成 `i64`，并记录 `SExt/ZExt/AExt`。这一步只改变“位置类型”和“扩展方式”，不改变参数在栈上的宽度。
2. `i64` 尝试 `x10-x17`；`State.AllocateReg` 会跳过已经被前面的参数占用的寄存器。
3. 寄存器用完，`AllocateStack(8, 8)` 分配一个 8 字节、8 对齐的栈槽，偏移从 0 开始递增。

`RetCC_MiniRV` 只允许 `x10/x11`。一个 `i128` 返回值会在类型合法化后拆成两个 `i64`，正好用这对寄存器（Ch 20）。

### 为什么 MiniRV 要 `CCIfType<[i32], CCPromoteToType<i64>>`？

因为 MiniRV 的 `GPR` 只有 `i64` 合法值类型，而调用约定分析发生在类型合法化之前：`CC_MiniRV` 会先看到 `LocVT == i32`。如果不 promote，没有任何规则匹配 `i32`，`AnalyzeFormalArguments` 会返回 true（未匹配），最终报 “unable to allocate function argument”。

promote 之后：

- `LocVT` 是 `i64`，可以放进 `GPR`。
- `LocInfo` 记录 `SExt`/`ZExt`/`AExt`，`LowerFormalArguments` 用 `TRUNCATE` 把 `i64` 截回 `i32`，`LowerCall` 用 `SIGN_EXTEND` 把 `i32` 扩回 `i64`。
- 栈槽仍按 8 字节分配，但 `LowerFormalArguments` 用 `ValVT.getStoreSize()` 建 fixed object，`i32` 只读 4 字节（`LW`）。promote 的是“位置类型”，不是“槽的分配宽度”。

实测一个 `i8/i16/i32/i64` 混合的函数：

```console
$ $B/llc -mtriple=minirv64 -O2 narrow.ll -o - | sed -n '/^narrow:/,/jalr/p'
narrow:                                 # @narrow
	.cfi_startproc
# %bb.0:                                # %entry
	slli	x3, x10, 56
	srai	x3, x3, 56
	slli	x4, x11, 48
	srai	x4, x4, 48
	add	x3, x3, x4
	addiw	x4, x12, 0
	add	x3, x3, x4
	add	x10, x3, x13
	jalr	x0, 0(x1)
```

`x10` 是 `i8` 参数，`x11` 是 `i16`，`x12` 是 `i32`，`x13` 是 `i64`。`i32` 用 `addiw x4, x12, 0`（截断到 32 位并符号扩展），`i8/i16` 用 `slli/srai` 重新符号扩展。这说明 ABI 约定“窄整数在 XLEN 里符号扩展”，callee 需要时再截断。

### RISC-V：`CC_RISCV`/`RetCC_RISCV` 为什么不在 td 里

`RISCVCallingConv.td` 的注释第一句就是：

```tablegen
// The RISC-V calling convention is handled with custom code in
// RISCVISelLowering.cpp (CC_RISCV).
```

实际代码在 `RISCVCallingConv.cpp`：

```cpp
bool llvm::CC_RISCV(unsigned ValNo, MVT ValVT, MVT LocVT, ...,
                    Type *OrigTy, CCState &State) {
  if (State.getCallingConv() == CallingConv::GHC)
    return CC_RISCV_GHC(...);
  if (State.getCallingConv() == CallingConv::Fast)
    return CC_RISCV_FastCC(...);
  return CC_RISCV_Impl(..., /*IsRet=*/false);
}

bool llvm::RetCC_RISCV(...) {
  return CC_RISCV_Impl(..., /*IsRet=*/true);
}
```

为什么 RISC-V 不能用纯 TableGen？因为 `lp64d` 的规则远比“8 个整数寄存器 + 栈”复杂：

- **整数**：`a0-a7` = `x10-x17`；RVE 只有 `a0-a5`。`RISCV::getArgGPRs` 返回这个列表。
- **浮点**：`fa0-fa7` = `f10-f17`；`RISCV::getArgFPRs` 按 ABI 和 `+f/+d/+q` 选 `FPR16/FPR32/FPR64/FPR128`。`lp64d` 下 `double` 走 `fa0-fa7`。
- **返回**：整数 `a0/a1`，浮点 `fa0/fa1`；`RetCC_RISCV` 走同一个 `CC_RISCV_Impl`，只是 `IsRet=true`。
- **栈对齐**：`lp64d` 的 `RISCVABI::getStackAlignment` 返回 `Align(16)`；`sp` 必须 16 字节对齐。MiniRV 的 `MiniRVFrameLowering` 也用 `Align(16)`，但这是 frame lowering 的决定，不在 td 里。
- **可变参数**：`AllowFPRForF64 = !ArgFlags.isVarArg()`；也就是说，**变参的浮点参数不走 `fa0-fa7`，而走整数寄存器/栈**。这是 psABI 的规则，TableGen 的 `CCIfType` 表达不了这种“看 ArgFlags”的分支。
- **对齐的变参**：RV64 下 2×XLEN 对齐的变参要跳过奇数号整数寄存器（`State.getFirstUnallocated(ArgGPRs) % 2 == 1` 时先 `AllocateReg` 一个）。这是 `CC_RISCV_Impl` 里的显式逻辑。
- **sret**：RISC-V 后端不把 `sret` 当成特殊的 CC 规则；前端已经把“返回大结构”改写成“第一个参数是指针，带 `sret` 属性”。所以你在 `CC_RISCV` 里看不到 `sret` 分支，只看到普通指针参数。
- **结构体拆分**：小于 2×XLEN 的结构体可能拆成两个寄存器，大于 2×XLEN 的走 `byval` 指针；这需要 `CC_RISCVAssign2XLen` 和 pending locs，TableGen 的线性规则写不出来。

实测 `double @addd(double, double)`：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m,+d -O2 addd.ll -o - | sed -n '/^addd:/,/ret/p'
addd:                                   # @addd
	.cfi_startproc
# %bb.0:                                # %entry
	fadd.d	fa0, fa0, fa1
	ret
```

`fa0/fa1` 是参数，`fa0` 是返回值。显式加 `-target-abi=lp64` 时，同样的函数会用 `fmv.d.x fa5, a1` 把整数参数搬进浮点寄存器，证明默认 ABI 是 `lp64d`。同一个函数在 MiniRV 上会走软浮点 libcall。

## 主线 4：`CallingConv.td` 与 `LowerCall`/`LowerFormalArguments` 的分工

一句话：**td 决定“放哪”，lowering 决定“怎么放”**。MiniRV 的 `LowerFormalArguments`（`MiniRVISelLowering.cpp`，946 行文件里的第 574 行起）做的是：

```cpp
CCState CCInfo(CallConv, IsVarArg, MF, ArgLocs, *DAG.getContext());
CCInfo.AnalyzeFormalArguments(Ins, CC_MiniRV);
for (CCValAssign &VA : ArgLocs) {
  if (VA.isRegLoc()) {
    Register VReg = RegInfo.createVirtualRegister(&MiniRV::GPRRegClass);
    RegInfo.addLiveIn(VA.getLocReg(), VReg);
    Arg = DAG.getCopyFromReg(Chain, DL, VReg, VA.getLocVT());
  } else {
    unsigned Offset = VA.getLocMemOffset();
    int FI = MF.getFrameInfo().CreateFixedObject(ValVT.getStoreSize(), Offset, true);
    Arg = DAG.getLoad(VA.getLocVT(), DL, Chain, FIN, ...);
  }
  if (Arg.getValueType() != VA.getValVT())
    Arg = DAG.getNode(ISD::TRUNCATE, DL, VA.getValVT(), Arg);
  InVals.push_back(Arg);
}
```

- `AnalyzeFormalArguments` 调 `CC_MiniRV`，得到 `ArgLocs`。
- 寄存器参数：建虚拟寄存器、`addLiveIn(物理寄存器, 虚拟寄存器)`、`CopyFromReg`。`addLiveIn` 是让后续寄存器分配知道“这个虚拟寄存器在入口处等于 `$x10`”。
- 栈参数：`CreateFixedObject` 建一个从入口 `sp` 偏移 `Offset` 的固定对象，然后 `getLoad`/`getExtLoad`。
- 最后按 `VA.getValVT()` 截断。注意 `ValVT` 是 IR 的窄类型，`LocVT` 是 ABI 的位置类型（`i64`）。

`LowerCall`（第 728 行起）做的是反过来：

```cpp
CCState CCInfo(CallConv, IsVarArg, MF, ArgLocs, *DAG.getContext());
CCInfo.AnalyzeCallOperands(Outs, CC_MiniRV);
for (CCValAssign &VA : ArgLocs) {
  SDValue Val = OutVals[I];
  if (Val.getValueType() != VA.getLocVT())
    Val = DAG.getNode(ISD::SIGN_EXTEND, DL, VA.getLocVT(), Val);
  if (VA.isRegLoc())
    RegsToPass.emplace_back(VA.getLocReg(), Val, VA.getLocVT());
  else
    MemOpChains.push_back(DAG.getStore(Chain, DL, Val,
        DAG.getNode(ISD::ADD, DL, PtrVT, StackPtr,
                    DAG.getIntPtrConstant(VA.getLocMemOffset(), DL)), ...));
}
for (auto &[Reg, Val, VT] : RegsToPass)
  Chain = DAG.getCopyToReg(Chain, DL, Reg, Val, Glue);
```

- 寄存器参数：`CopyToReg` 到 `$x10..$x17`，并把 `DAG.getRegister(Reg, VT)` 作为 `CALL` 的 implicit use。
- 栈参数：在 `sp + offset` 处 `Store`。如果函数有 reserved call frame，`StackPtr` 就是当前 `sp`；否则先 `CopyFromReg` 读 `$x2`。
- 调用节点带 `RegisterMask`（`getCallPreservedMask`），告诉寄存器分配器哪些寄存器在调用后还活着。
- `CALLSEQ_END` 释放调用点临时空间；返回值从 `$x10`（`RetCC_MiniRV`）`CopyFromReg` 出来。

`LowerReturn`（第 697 行起）只做 `AnalyzeReturn(Outs, RetCC_MiniRV)` + `CopyToReg` + `RET`；`CanLowerReturn` 用 `CCInfo.CheckReturn(Outs, RetCC_MiniRV)` 判断返回值能不能全部放进 `x10/x11`。

RISC-V 的对应函数规模完全不同：

- `RISCVTargetLowering::LowerFormalArguments` 在 `RISCVISelLowering.cpp` 第 28,796 行起。
- `RISCVTargetLowering::LowerCall` 在第 29,108 行起。
- `RISCVTargetLowering::LowerReturn` 在第 29,634 行起。
- 你可能听过 ISelLowering 是几万行；在这个树里 RISC-V 是 31,441 行，MiniRV 的 `MiniRVISelLowering.cpp` 只有 946 行。函数名一样，复杂度差两个数量级，因为 RISC-V 要处理结构体拆分、向量 tuple、变参、byval、sret、tail call、CFI 等。

所以改调用约定时，永远要同时改两处：

- 改 `CallingConv.td`（或 `CC_RISCV_Impl`）：参数/返回值的位置。
- 改 `LowerCall`/`LowerFormalArguments`/`LowerReturn`：怎么把值搬进/搬出这些位置。
- 再改 `CalleeSavedRegs`/`reservedRegs`：如果新约定动了 callee-saved 集合或保留寄存器。
- 最后跑 9 参数、变参、结构体返回的端到端测试。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 寄存器 | `MiniRVRegisterInfo.td` | `X0-X31`、`GPR`、`CSR_MiniRV` | 32 个 GPR、callee-saved 集合 |
| 寄存器信息 | `MiniRVRegisterInfo.cpp` | `getCalleeSavedRegs`、`getReservedRegs`、`getFrameRegister` | 保留集合、帧基址 |
| 调用约定 | `MiniRVCallingConv.td` | `CC_MiniRV`、`RetCC_MiniRV` | x10-x17 + 栈、x10/x11 返回 |
| 调用 lowering | `MiniRVISelLowering.cpp` | `LowerFormalArguments`、`LowerCall`、`LowerReturn` | 搬值、栈槽、varargs |
| RISC-V 参照 | `RISCVRegisterInfo.td`、`RISCVCallingConv.cpp`、`RISCVISelLowering.cpp` | `GPR`/`FPR*`、`CC_RISCV`、`LowerCall` | lp64d、CSR、变参 |

## 实测：从 9 个参数到 `x10` vs `a0`

测试文件：`sum9.ll` 是 9 个 `i64` 逐项相加；`call9.ll` 调用它并传 9 个参数；`narrow.ll` 是 `i8/i16/i32/i64` 混合；`addd.ll` 是两个 `double` 相加。

### 实测 1：9 个 `i64` 参数的 callee

`sum9(i64 a,...,i64 i)` 的 MiniRV 汇编：

```console
$ $B/llc -mtriple=minirv64 -O2 sum9.ll -o - | sed -n '/^sum9:/,/jalr/p'
sum9:                                   # @sum9
	.cfi_startproc
# %bb.0:                                # %entry
	add	x3, x10, x11
	add	x3, x3, x12
	add	x3, x3, x13
	add	x3, x3, x14
	add	x3, x3, x15
	add	x3, x3, x16
	add	x3, x3, x17
	ld	x4, 0(x2)
	add	x10, x3, x4
	jalr	x0, 0(x1)
```

前 8 个参数在 `x10-x17`，第 9 个在 `0(x2)`。`ld x4, 0(x2)` 就是 `LowerFormalArguments` 为第 9 个参数建的 fixed object。

RISC-V 的同一函数：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 sum9.ll -o - | sed -n '/^sum9:/,/ret/p'
sum9:                                   # @sum9
	.cfi_startproc
# %bb.0:                                # %entry
	ld	t0, 0(sp)
	add	a0, a0, a1
	add	a2, a2, a3
	add	a4, a4, a5
	add	a0, a0, a2
	add	a4, a4, a6
	add	a0, a0, a4
	add	a0, a0, a7
	add	a0, a0, t0
	ret
```

同样前 8 个在 `a0-a7`，第 9 个在 `0(sp)`。RISC-V 的 `ret` 是 `jalr x0, 0(x1)` 的伪指令；MiniRV 没有 `ret` 伪指令，直接打印 `jalr x0, 0(x1)`。

### 实测 2：调用者把第 9 个参数写到栈上

```console
$ $B/llc -mtriple=minirv64 -O2 call9.ll -o - | sed -n '/^call9:/,/jalr/p'
call9:                                  # @call9
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x1, 8(x2)
	.cfi_offset x1, -8
	sd	x10, 0(x2)
	addi	x10, x0, 1
	addi	x11, x0, 2
	addi	x12, x0, 3
	addi	x13, x0, 4
	addi	x14, x0, 5
	addi	x15, x0, 6
	addi	x16, x0, 7
	addi	x17, x0, 8
	jal	x1, sum9
	ld	x1, 8(x2)
	.cfi_restore x1
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

这里同时看到三件事：

- `addi x2, x2, -16` + `sd x1, 8(x2)`：16 字节栈帧，`ra` 存在 `8(sp)`。MiniRV 的 `MiniRVFrameLowering` 用 `Align(16)`，和 RISC-V 一样。
- `sd x10, 0(x2)`：第 9 个参数 `%x` 写到 `0(sp)`。`x10` 里原本是调用者的第一个参数，先存到栈上，再被 `addi x10, x0, 1` 覆盖。
- `jal x1, sum9`：MiniRV 的调用指令。RISC-V 会打印 `call sum9`，编码是 `auipc ra` + `jalr ra` 的组合。

RISC-V 的调用者同样先 `addi sp, sp, -16`、`sd ra, 8(sp)`，把第 9 个参数存到 `0(sp)`（`sd t0, 0(sp)`），再 `call sum9`；只是寄存器名换成了 ABI 名。这就是“同一套 psABI 规则，两个 target 的最小实现”。

### 实测 3：`-debug-only=regalloc` 的分配顺序

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=regalloc call9.ll -o /dev/null 2>&1 | head -5
AllocationOrder(GPR) = [ $x3 $x4 $x7 $x10 $x11 $x12 $x13 $x14 $x15 $x16 $x17 $x28 $x29 $x30 $x31 $x1 $x8 $x9 $x18 $x19 $x20 $x21 $x22 $x23 $x24 $x25 $x26 $x27 ]
Computing live-in reg-units in ABI blocks.
0B	%bb.0 X10#0
Created 1 new intervals.
********** INTERVALS **********
```

怎么读：

- `x0/x2/x5/x6` 不在列表里：被 `getReservedRegs` 排除了。
- `x3/x4/x7/x10-x17/x28-x31` 在前：它们是 caller-save（或普通可分配）寄存器。
- `x1/x8/x9/x18...x27` 在最后：它们是 `CSR_MiniRV` 的成员，`RegisterClassInfo::compute` 把“alias 到 CSR 的寄存器”留到最后（源码注释：`CSR aliases go after the volatile registers`）。所以 `s0-s11` 和 `ra` 不会被优先分配，除非调用者保存的压力需要。
- `x10-x17` 排在前面，不是随机的：它们在 GPR 类顺序里靠前，也是参数寄存器，所以早于 CSR 被分配。

这条输出是检查 `reservedRegs`/`CalleeSavedRegs` 是否写对的最快方式。如果你发现 `x5` 出现在分配顺序里，说明 scratch 没保留；如果 `x18-x27` 排在最前，说明 CSR 集合没生效。

### 实测 4：`llvm-mc` 里 `x10` 与 `a0` 是同一个寄存器

```console
$ printf 'add x10, x11, x12\n' | $B/llvm-mc -triple=minirv64 -show-encoding
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]

$ printf 'add x10, x11, x12\n' | $B/llvm-mc -triple=riscv64 -mattr=+m -show-encoding
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
```

两条命令的编码完全一样：`0x33,0x85,0xc5,0x00`。`x10` 和 `a0` 是同一个物理寄存器的两个名字。MiniRV 的 InstPrinter 只认 `x` 名；RISC-V 的 InstPrinter 按 ABI 名打印，所以同一个寄存器在 RISC-V 汇编里显示为 `a0`。编码器只认 5 位寄存器号，名字是打印层的选择。

## 反直觉与踩雷

1. **错误直觉**：`a0` 和 `x10` 是两个寄存器，`llc` 输出 `a0` 说明 RISC-V 用了不同的寄存器文件。→ **正确认识**：它们是同一个物理寄存器的两个名字，编码都是 `0x85`（rd=10）。MiniRV 打印 `x10`、RISC-V 打印 `a0`，是 InstPrinter 的 ABI 命名差异。
2. **错误直觉**：`GPR` 类包含 `x0..x31`，所以 32 个寄存器都能分配。→ **正确认识**：`RegisterClass` 只定义“属于这个类的物理寄存器”，`getReservedRegs` 再把 `x0/x2/x5/x6`（以及 `hasFP` 时的 `x8`）排除。分配顺序里没有它们，才是真的不可分配。
3. **错误直觉**：`CCIfType<[i32], CCPromoteToType<i64>>` 会把 `i32` 参数在栈上占 8 字节。→ **正确认识**：它只把 `LocVT` 提升到 `i64`，并记录 `SExt/ZExt/AExt`；栈槽仍按 8 字节分配，但实际只读 `ValVT.getStoreSize()` 字节（`i32` 用 `LW` 读 4 字节）。promote 是“位置类型”，不是“槽的分配宽度”。
4. **错误直觉**：改 `CallingConv.td` 就能改调用约定。→ **正确认识**：td 只产生 `CCAssignFn`，告诉你每个参数落在哪个寄存器/栈偏移；真正搬值的 `CopyToReg`/`Store`/`CopyFromReg` 在 `LowerCall`/`LowerFormalArguments`/`LowerReturn` 里。只改 td 不改 lowering，最常见的现象是参数落在正确的寄存器但没人写进去，或者栈槽分配了但没人 store。
5. **错误直觉**：RISC-V 的 `lp64d` 和 MiniRV 一样，都是 8 个参数寄存器 + 16 字节栈对齐。→ **正确认识**：整数部分一样（`a0-a7`），但 `lp64d` 还有 `fa0-fa7`、浮点 callee-saved `f8/f9/f18-f27`、变参浮点走整数寄存器、2×XLEN 对齐的变参跳过奇数寄存器、结构体拆分/`byval`/`sret` 规则。MiniRV 只实现了整数子集。
6. **错误直觉**：`getRARegister`/`getPointerReg`/`getStackRegister` 都是 `TargetRegisterInfo` 的 virtual，应该 override。→ **正确认识**：`getRARegister` 在 `MCRegisterInfo`，由 `MiniRVGenRegisterInfo(MiniRV::X1)` 设置；`getPointerReg`/`getStackRegister` 是旧 API，当前分别由 `addRegisterClass` + DataLayout 和 `setStackPointerRegisterToSaveRestore` 取代。

## 进阶：再往深一层

- **变参保存区**：`LowerFormalArguments` 在 `IsVarArg` 时把 `x10-x17` 中未被命名参数消耗的寄存器保存到“当前 `sp` 下方”的连续区域，`VarArgsFrameIndex` 指向它。`va_start` 拿到的指针就是“下一个参数槽”，`va_arg` 按 8 字节步进。MiniRV 的 `MiniRVMachineFunctionInfo` 只存这个 index，真正的布局在 lowering 里。Ch 17 展开。
- **`sret`/`byval`**：后端通常不把它们当 CC 规则，而是前端改写函数签名：大结构返回变成第一个指针参数 + `sret` 属性；大结构传参变成指针 + `byval`。RISC-V 的 `CC_RISCV_Impl` 里没有 `sret` 分支，原因就在这里。你写新 target 时，先确认 Clang 的 ABI lowering 是否已经把结构体降成标量/指针，再把剩余规则写进 `CallingConv.td`。
- **寄存器分配顺序不是 td 的顺序**：MiniRV 的 `GPR` 写的是 `x0..x31`，实际分配顺序是 `x3,x4,x7,x10...x31,x1,x8,x9`。原因是 `RegisterClassInfo::compute` 会剔除 reserved、把 CSR alias 移到末尾。调寄存器分配时，先看 `-debug-only=regalloc` 的 `AllocationOrder`，不要凭 td 猜。

## 本章重点整理

- `RegisterClass` 定义可分配的物理寄存器集合、合法值类型、对齐和默认顺序；`reservedRegs` 再剔除不能用的寄存器。MiniRV 只有 `GPR`（`i64`），RISC-V 有 `GPR`/`GPRF32`/`FPR16`/`FPR32`/`FPR64` 等。
- `CSR_MiniRV` = `x1,x8,x9,x18-x27`，和 RISC-V `CSR_ILP32_LP64` 相同；`lp64d` 还要加 `f8/f9/f18-f27`。`getCalleeSavedRegs` 按调用约定/ABI 选表。
- `getReservedRegs`：MiniRV 保留 `x0/x2/x5/x6`（以及 `hasFP` 时的 `x8`）；RISC-V 还保留 `x3/x4`、bp、RVE 高位寄存器、向量/浮点 CSR。
- `getRARegister` 由 `MCRegisterInfo` 构造函数设置（MiniRV/RISC-V 都是 `X1`）；`getFrameRegister` 返回 `x8` 或 `x2`；`getPointerReg`/`getStackRegister` 是旧 API，当前用 `addRegisterClass` + `setStackPointerRegisterToSaveRestore`。
- `CC_MiniRV`：`i32` promote 到 `i64`，`i64` 依次 `x10-x17`，溢出到 8 字节栈槽；`RetCC_MiniRV` 用 `x10/x11`。
- RISC-V 的 `CC_RISCV`/`RetCC_RISCV` 在 C++ 里：整数 `a0-a7`、浮点 `fa0-fa7`、`lp64d` 16 字节栈对齐、变参浮点走整数寄存器、2×XLEN 变参跳过奇数寄存器、结构体拆分/`sret`/`byval`。
- `CallingConv.td` 只分配位置；`LowerCall`/`LowerFormalArguments`/`LowerReturn` 负责搬值。
- `llvm-mc` 证明 `x10` 和 `a0` 编码相同；`-debug-only=regalloc` 证明分配顺序会剔除 reserved 并把 CSR 放最后。

## 自我检核

- [ ] 不看源码，画出 MiniRV 的 `x0-x31` 角色表：哪些保留、哪些 CSR、哪些是参数寄存器、哪些是 scratch。
- [ ] 为什么 MiniRV 的 `GPR` 只声明 `i64`，而 RISC-V 的 `GPR` 同时声明 `XLenVT`/`XLenFVT`/packed 类型？如果 MiniRV 要支持 `f32` 软浮点，寄存器类需要改吗？
- [ ] 解释 `CSR_MiniRV` 为什么包含 `x1`。如果去掉 `x1`，一个“调用别人且自己还要返回”的函数会在哪里出错？
- [ ] `getReservedRegs` 为什么对 `x8` 用 `hasFP(MF)` 条件保留？如果永远保留，会损失什么？
- [ ] `CCIfType<[i32], CCPromoteToType<i64>>` 和 `CCAssignToStack<8, 8>` 分别改变什么？`i32` 参数在栈上到底占几字节？
- [ ] 9 个 `i64` 参数在 `minirv64` 和 `riscv64` 上分别落在哪？第 9 个的地址是什么？调用者为什么要先 `addi sp, sp, -16`？
- [ ] `CallingConv.td` 和 `LowerCall` 的分工是什么？只改前者会出现什么现象？

## 延伸阅读

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V International
  - **读哪里**：先读 “Calling Convention” 的整数/浮点参数传递、栈对齐、变参、`sret`/`byval` 小节，再对照 `lp64d` 的表格。
  - **能学到什么**：`a0-a7`/`fa0-fa7`、16 字节栈对齐、变参浮点走整数寄存器、2×XLEN 对齐规则的权威来源。
  - **前提**：知道 RV64 的 `x`/`f` 寄存器编号和基本 load/store；不需要读 LLVM 源码。

- **[LLVM TableGen CallingConv](https://llvm.org/docs/TableGen/)** — LLVM Project
  - **读哪里**：`CallingConv`、`CCIfType`、`CCAssignToReg`、`CCAssignToStack`、`CCPromoteToType` 的定义和示例。
  - **能学到什么**：MiniRV 的 `CC_MiniRV` 为什么能生成 `CCAssignFn`，以及哪些规则（变参、结构体拆分）TableGen 表达不了。
  - **前提**：读过本章的 `CC_MiniRV`，知道 `LocVT`/`LocInfo`/`CCValAssign` 的含义。

- **[LLVM 源码：`RISCVCallingConv.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/RISCV/RISCVCallingConv.cpp)** — LLVM Project
  - **读哪里**：`CC_RISCV`、`RetCC_RISCV`、`CC_RISCV_Impl`、`RISCV::getArgGPRs`、`RISCV::getArgFPRs`，以及变参奇数寄存器对齐的代码。
  - **能学到什么**：一个成熟后端如何把 psABI 的复杂规则写成 C++；对照 MiniRV 的二十几行 td，理解“最小实现”和“完整 ABI”的差距。
  - **前提**：读完本章，知道 `lp64d` 的基本规则；否则会被 `PendingLocs`/`CC_RISCVAssign2XLen` 绕晕。

→ [Ch 11 FrameLowering](./11-frame-lowering.md)
