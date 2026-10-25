# Ch 11 寄存器与寄存器类：从 x0 到 spill slot

> 目标：把 `RISCVRegisterInfo.td` 里的寄存器定义、寄存器类、分配顺序、callee-saved 与 reserved 规则串起来，再用 MIR 观察虚拟寄存器怎样变成物理寄存器。读完后你能解释为什么代码总从 `a0` 和 `fa5` 开始用、为什么 `x0` 永远不能被分配、为什么 8 个活跃值会换来 48 字节栈帧。

> 环境：Windows；LLVM 23.1.4；PowerShell。工具目录与统一入口同第 08 章。源码依据来自 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。

> 约定：标“实测”的片段来自本机重跑；“源码依据”给文件名与符号名；需要重编译 llc 才能验证的结论标 `guided`。

## 为什么

第 10 章把指令写进了 ELF，但指令里的寄存器从哪来，还没有回答。后端里有两套容易混淆的东西：寄存器（`X10`、`F10_D`、`V8`）与寄存器类（`GPR`、`FPR64`、`VR`）。前者是硬件资源，后者是“分配器可以从中挑一个”的集合。`RISCVRegisterInfo.td` 同时定义了两者，并顺手决定了三件影响代码形状的事：

- 分配顺序：`GPR` 把 `a0-a7` 放在最前面，所以简单函数几乎总是从 `a0` 开始。
- spill 大小：`XLenRI` 在 RV64 上是 `RegInfo<64,64,64>`，所以一个 GPR 溢出占 8 字节、按 8 字节对齐。
- 保留集合：`x0` 是常量寄存器，`sp/gp/tp` 永远保留，启用帧指针时 `x8` 也保留。

本机实测能把这三件事连起来。函数 `mix(i64, i64, double, float)` 在 `-stop-after=finalize-isel` 的 MIR 里有 11 个虚拟寄存器，class 分别是 `gpr / fpr64 / fpr32`；对应的 `liveins` 是 `$x10, $x11, $f10_d, $f11_f`。到了 `-stop-after=postrapseudos`，同一段代码变成 `renamable $x10 = MUL ...`、`renamable $f15_d = ...`，`noVRegs: true`。最终汇编进一步把 `$f15_d` 打印成 `fa5`，把 `$f14_d` 打印成 `fa4`。

另一个实测是压力实验：8 个 `i64` 参数、8 个中间值、一次 8 参数调用，最终得到 48 字节栈帧，`sd ra, 40(sp)`、`sd s0, 32(sp)`、`sd s1, 24(sp)`、`sd s2, 16(sp)`、`sd s3, 8(sp)`，注释写着 `8-byte Folded Spill`。为什么是 `s0-s3` 而不是 `t0-t6`？因为调用会破坏 caller-saved，跨调用活跃的值必须放进 callee-saved。为什么只有 4 个？因为 `s0-s3` 足够覆盖这段压力，分配器不会多存。

## 主线

### 11.1 寄存器定义的层次：X#_H → X#_W → X#

`RISCVRegisterInfo.td` 先定义三个基类：

```tablegen
class RISCVReg<bits<5> Enc, string n, list<string> alt = []> : Register<n> {
  let HWEncoding{4-0} = Enc;
  let AltNames = alt;
}
class RISCVReg16<bits<5> Enc, string n, list<string> alt = []> : Register<n> { ... }
class RISCVRegWithSubRegs<bits<5> Enc, string n, list<Register> subregs,
                          list<string> alt = []>
    : RegisterWithSubRegs<n, subregs> { ... }
```

`HWEncoding{4-0}` 是 5 位硬件编号。压缩指令的寄存器字段只有 3 位，编码器需要的是“编号”，不是名字；这就是为什么所有别名最终都要能回落到同一个 5 位编号。

整数寄存器分三级：

```tablegen
def X0_H  : RISCVReg<0, "x0", ["zero"]>;
def X0_W  : RISCVRegWithSubRegs<0, "x0", [X0_H], ["zero"]>;
def X0    : RISCVRegWithSubRegs<0, "x0", [X0_W], ["zero"]>, DwarfRegNum<[0]>;
```

`X#_H` 是 16 位子寄存器，`X#_W` 是 32 位子寄存器，`X#` 在 RV64 上是 64 位本体。`RISCVReg32` 用 `sub_16` 把 `X#_W` 接到 `X#_H`，`RISCVReg64` 用 `sub_32` 把 `X#` 接到 `X#_W`。RV32 上 `X#` 本身就是 32 位，`X#_W` 与它同宽但仍是独立的 TableGen 记录。

`X0` 多了一个 `isConstant = true`。这不是注释，而是分配器与 MachineVerifier 都会读的属性：常量寄存器不能被写、不能被分配、读它等价于读 0。`getReservedRegs` 里还有一段“把所有 `isConstantPhysReg` 的寄存器及其子寄存器标记为 reserved”的循环，`x0` 就是靠这条规则被永久保留的。

别名（`AltNames`）是 ABI 名：

```tablegen
def X2  : ... ["sp"], DwarfRegNum<[2]>;
def X8  : ... ["s0", "fp"], DwarfRegNum<[8]>;
def X10 : ... ["a0"], DwarfRegNum<[10]>;
```

`X8` 有两个别名，`s0` 与 `fp`。打印器默认选 `s0`，但 `-M emit-x8-as-fp` 会选 `fp`；两者是同一个寄存器，不是两个。`DwarfRegNum` 与 ABI 编号一致（x0 到 x31 对应 DWARF 0 到 31），调试信息与 CFI 都用它。

文件里有一段注释解释了为什么汇编器要做寄存器“升降级”：

```text
Because RISCVReg64 register have AsmName and AltNames that alias with their
16/32-bit sub-register, RISCVAsmParser will need to coerce a register number
from a RISCVReg16/RISCVReg32 to the equivalent RISCVReg64 when appropriate.
```

`x10`、`X10_W`、`X10_H` 的 `AsmName` 都是 `"x10"`，文本上无法区分。解析器先按文本匹配到一个记录，再根据指令操作数期望的寄存器类提升到正确的宽度。这也是第 08 章 `ParseRegister` 要处理“同名不同宽”的原因。

浮点寄存器用同样的三级结构，只是位宽多一档：`F#_H`（16）、`F#_F`（32）、`F#_D`（64）、`F#_Q`（128），每级用 `RISCVReg128/64/32` 串起来，并带 `DwarfRegAlias`。向量寄存器 `V0..V31` 是另一套：`V8M2` 这种名字表示“从 v8 开始、LMUL=2 的寄存器组”，它的子寄存器是 `V8` 与 `V9`；`V8M4` 覆盖 `V8..V11`。这些组合由 `RegisterTuples` 与 `SubRegSet` 生成，不需要手写 32 份。

### 11.2 寄存器类：类型、对齐、RegInfos

`GPR` 的定义只有几行，但信息量很大：

```tablegen
class GPRRegisterClass<dag regList>
    : RISCVRegisterClass<[XLenVT, XLenFVT,
                          XLenVecI8VT, XLenVecI16VT, XLenVecI32VT], 32, regList> {
  let RegInfos = XLenRI;
}

def GPR : GPRRegisterClass<(add (sequence "X%u", 10, 17),
                                (sequence "X%u", 5, 7),
                                (sequence "X%u", 28, 31),
                                (sequence "X%u", 8, 9),
                                (sequence "X%u", 18, 27),
                                (sequence "X%u", 0, 4))>;
```

三个字段要分开看：

- 类型列表 `[XLenVT, XLenFVT, ...]`：`XLenVT` 在 RV32 是 `i32`、RV64 是 `i64`；`XLenFVT` 只有 RV64 有，取值 `f64`，用于 Zdinx 这类“浮点值放整数寄存器”的场景。
- 对齐参数 `32`：这是寄存器类的声明对齐，不是 spill 对齐。真正的 spill 大小由 `RegInfos` 决定。
- `RegInfos = XLenRI`：

```tablegen
def XLenRI : RegInfoByHwMode<
      [RV32,              RV64],
      [RegInfo<32,32,32>, RegInfo<64,64,64>]>;
```

在 RV64 上，一个 GPR 的 spill size 是 64 位、spill alignment 是 64 位。于是 8 个活跃 GPR 至少需要 64 字节的 spill 空间（实际布局还会加 ra 与对齐填充）。如果只看 C 类型 `long` 是 8 字节，结论碰巧一样；但看 `f32` 就不一样了：`FPR32` 的 `RegInfo` 是 32 位，而把 `f32` 放进 GPR 容器时会按 `XLenVT` 存，spill 是 64 位。这个差异在第 12 章的 soft-float ABI 里会再次出现。

浮点类的定义直接给出对齐：

```tablegen
def FPR16  : FPRRegisterClass<[f16, bf16], 16, "H">;
def FPR32  : FPRRegisterClass<[f32], 32, "F">;
def FPR64  : FPRRegisterClass<[f64], 64, "D">;
def FPR128 : FPRRegisterClass<[f128], 128, "Q">;
```

`FPR64` 的 spill 是 8 字节、8 字节对齐；`FPR32` 是 4 字节、4 字节对齐。压缩浮点类 `FPR16C/FPR32C/FPR64C` 只包含 `f8-f15` 这 8 个可压缩寄存器，供压缩指令的模式匹配使用。

向量类 `VReg` 用另一套公式：

```tablegen
class VReg<list<ValueType> regTypes, dag regList, int Vlmul, int nf = 1>
    : RISCVRegisterClass<regTypes, 64, regList> {
  let IsVRegClass = 1;
  let VLMul = Vlmul;
  let NF = nf;
  let Size = !mul(VLMul, NF, 64);
  let CopyCost = !mul(VLMul, NF);
  let AllocationPriority = !if(!gt(Vlmul, 1), Vlmul, 0);
}
```

`Size = VLMul * NF * 64` 是“最大 ELEN 为 64 位”假设下的最小尺寸；实际 VLEN 由运行时 `vtype` 决定。`AllocationPriority` 让 LMUL 大的组优先分配，避免先占掉小组导致大组放不下。

`GPRPair` 是另一类特殊类：

```tablegen
def XLenPairRI : RegInfoByHwMode<
      [RV32,                RV64],
      [RegInfo<64, 64, 32>, RegInfo<128, 128, 64>]>;
```

RV64 上一个偶奇寄存器对的 spill 是 128 位、64 位对齐（`RegInfo<128,128,64>` 的三个参数依次是寄存器大小、spill 大小、spill 对齐）。`GPRPair` 的 `CopyCost = 2`，表示复制一个对按两份成本计；它服务于明确要求偶奇对的指令与类型，而不是所有 128 位值。

### 11.3 allocation order：代码形状的隐形手

`GPR` 的寄存器列表顺序就是分配顺序。展开 `sequence`：

```text
x10 x11 x12 x13 x14 x15 x16 x17   (a0-a7)
x5 x6 x7                          (t0-t2)
x28 x29 x30 x31                   (t3-t6)
x8 x9                             (s0-s1)
x18 ... x27                       (s2-s11)
x0 x1 x2 x3 x4                    (zero, ra, sp, gp, tp)
```

源码注释写得很直白：

```text
The order of registers represents the preferred allocation sequence.
Registers are listed in the order caller-save, callee-save, specials.
```

先 caller-save、再 callee-save、最后 specials，目的是减少 prologue/epilogue 的保存量。`x0` 排在最后不是“可以分配”，而是“如果分配器真的走到这里，它已经没有选择”；`x0` 同时被 `isConstant` 与 `getReservedRegs` 双重排除，正常路径不会选中它。

`FPR` 的顺序更容易让人意外：

```text
F15 F14 F13 F12 F11 F10   (fa5-fa0)
F0 F1 F2 F3 F4 F5 F6 F7   (ft0-ft7)
F16 F17                   (fa6-fa7)
F28 F29 F30 F31           (ft8-ft11)
F8 F9                     (fs0-fs1)
F18 ... F27               (fs2-fs11)
```

所以第一个被分配的浮点临时寄存器是 `fa5`，不是 `fa0`。注释解释了原因：从参数寄存器倒序开始，因为它们可压缩。`fa0-fa5`（F10-F15）落在 `c.fld/c.fsd` 能编码的 `f8-f15` 范围内，先用高编号的 `fa5`，可以把低编号留给更需要压缩编码的参数与结果。`fa6/fa7` 是 F16/F17，不在压缩范围内，所以排在很后面。

这直接对应第 11.7 节的实测：`mix` 的第一个 `FCVT_D_L` 写到 `$f15_d`，打印成 `fa5`；第二个临时值用 `$f14_d`，打印成 `fa4`；函数返回值仍然回到 `fa0`。如果只看“从 fa0 开始”的直觉，就会误判 MIR。

`VR` 的顺序是 `V8..V31`，然后 `V7..V0`。源码注释：

```text
We reverse the order of last 8 registers so that we don't needlessly prevent
allocation of higher lmul register groups while still putting v0 last in the
allocation order.
```

`v0` 是掩码寄存器，很多指令隐式使用它，所以放最后。`VRM2` 是 `V8M2..V30M2` 再 `V6M2..V0M2`，`VRM4` 是 `V8M4,V12M4,V16M4,V20M4,V24M4,V28M4,V4M4,V0M4`，`VRM8` 是 `V8M8,V16M8,V24M8,V0M8`。大 LMUL 的组按 2/4/8 的步长走，保证不与自己重叠。

还有一个影响分配顺序的细节：`X1_H` 到 `X7_H` 以及 `X16_H` 到 `X31_H` 带 `CostPerUse = [0, 1]`。源码注释说明，不在 `GPRC`（x8-x15）里的寄存器不能被压缩指令编码，所以提高一点使用成本，让分配器优先选可压缩的寄存器。这是“分配顺序”之外的第二层偏好，用成本而不是硬顺序表达。
### 11.4 子寄存器与 CoveredBySubRegs：寄存器对为什么能当整体用

`X0..X31` 自己是 `RegisterWithSubRegs`，但*没有* `CoveredBySubRegs`。`CoveredBySubRegs = 1` 出现在寄存器对上：

```tablegen
def X0_Pair : RISCVRegWithSubRegs<0, X0.AsmName, [X0, DUMMY_REG_PAIR_WITH_X0],
                                  X0.AltNames> {
  let SubRegIndices = [sub_gpr_even, sub_gpr_odd];
  let CoveredBySubRegs = 1;
}
foreach I = 1-15 in {
  def "X" # Index # "_X" # IndexP1 : RISCVRegWithSubRegs<Index, Reg.AsmName,
                                                          [Reg, RegP1],
                                                          Reg.AltNames> {
    let SubRegIndices = [sub_gpr_even, sub_gpr_odd];
    let CoveredBySubRegs = 1;
  }
}
```

`CoveredBySubRegs` 告诉 TableGen 与后端：这个“大寄存器”的每一位都由列出的子寄存器覆盖，因此可以安全地在大小寄存器之间做 subreg 复制，不需要额外保留“未定义位”。`X10_X11` 覆盖 `X10` 与 `X11`，`sub_gpr_even` / `sub_gpr_odd` 分别指向低半与高半；在 RV64 上这两个索引是 `SubRegIndex<64>` 与 `SubRegIndex<64,64>`（通过 `SubRegRangeByHwMode` 随 XLEN 变化）。

`X0_Pair` 的一半是真实 `X0`，另一半是 `DUMMY_REG_PAIR_WITH_X0`：

```tablegen
def DUMMY_REG_PAIR_WITH_X0 : RISCVReg<0, "0">;
```

注释说明，`X0_Pair` 只用 `X0`，不会读写 `X1`，所以需要一个占位寄存器填满“高半”。这个占位寄存器不属于 `GPR`，而被单独放进 `GPRAll`：

```tablegen
def GPRAll : GPRRegisterClass<(add GPR, DUMMY_REG_PAIR_WITH_X0)>;
```

源码注释给出了原因：如果把 `DUMMY_REG_PAIR_WITH_X0` 直接塞进 `GPR`，会改变 `GPR` 的 `regPressureSetLimit`，从而影响代码生成。这是一个很典型的“TableGen 里多加一个寄存器，寄存器压力模型就变了”的坑。

`getReservedRegs` 会显式保留 `DUMMY_REG_PAIR_WITH_X0`，并调用 `markSuperRegs` 把所有别名一起标上。`markSuperRegs` 的必要性在于：`X0_Pair` 是 `X0` 的超寄存器，只保留 `X0` 不够，任何能通过别名访问到的记录都必须一起保留。

### 11.5 callee-saved 与 reserved：两套完全不同的集合

`RISCVCallingConv.td` 里全是 `CalleeSavedRegs`，没有参数分配规则（第 12 章展开）。最基础的四组：

```tablegen
def CSR_ILP32E_LP64E : CalleeSavedRegs<(add X1, X8, X9)>;
def CSR_ILP32_LP64
    : CalleeSavedRegs<(add CSR_ILP32E_LP64E, (sequence "X%u", 18, 27))>;
def CSR_ILP32F_LP64F
    : CalleeSavedRegs<(add CSR_ILP32_LP64,
                       F8_F, F9_F, (sequence "F%u_F", 18, 27))>;
def CSR_ILP32D_LP64D
    : CalleeSavedRegs<(add CSR_ILP32_LP64,
                       F8_D, F9_D, (sequence "F%u_D", 18, 27))>;
```

展开后：整数 callee-saved 是 `ra, s0, s1, s2-s11`；浮点 ABI 再加 `fs0, fs1, fs2-fs11`。注意 `ra` 也在列表里，因为被调函数如果调用别人，必须自己保存返回地址；叶子函数可以不做。

`RISCVRegisterInfo::getCalleeSavedRegs` 按 ABI 选表：

```cpp
switch (Subtarget.getTargetABI()) {
case RISCVABI::ABI_ILP32E:
case RISCVABI::ABI_LP64E:
  return CSR_ILP32E_LP64E_SaveList;
case RISCVABI::ABI_ILP32:
case RISCVABI::ABI_LP64:
  return CSR_ILP32_LP64_SaveList;
case RISCVABI::ABI_ILP32F:
case RISCVABI::ABI_LP64F:
  return CSR_ILP32F_LP64F_SaveList;
case RISCVABI::ABI_ILP32D:
case RISCVABI::ABI_LP64D:
  return CSR_ILP32D_LP64D_SaveList;
}
```

在 switch 之前还有三个特殊分支：`CallingConv::GHC` 返回 `CSR_NoRegs_SaveList`；`CallingConv::PreserveMost` 返回 `CSR_RT_MostRegs_SaveList`；带 `interrupt` 属性的函数按 F/D/V 与 RVE 选择 `CSR_*_Interrupt_*_SaveList`，保存 `X5-X31`（RVE 去掉 x16-x31）。`CSR_Interrupt` 的定义就是 `(add X1, (sequence "X%u", 5, 31))`。

`getReservedRegs` 是另一套逻辑，返回一个 `BitVector`，表示“分配器绝对不能碰”的寄存器。源码依据（`RISCVRegisterInfo.cpp`）：

```cpp
for (size_t Reg = 0; Reg < getNumRegs(); Reg++) {
  if (Subtarget.isRegisterReservedByUser(Reg)) { ... }
  if (isConstantPhysReg(Reg)) {
    for (MCPhysReg Sub : subregs_inclusive(Reg))
      markSuperRegs(Reserved, Sub);
  }
}
markSuperRegs(Reserved, RISCV::X2_H); // sp
markSuperRegs(Reserved, RISCV::X3_H); // gp
markSuperRegs(Reserved, RISCV::X4_H); // tp
if (TFI->hasFP(MF)) markSuperRegs(Reserved, RISCV::X8_H); // fp
if (TFI->hasBP(MF)) markSuperRegs(Reserved, RISCVABI::getBPReg()); // bp
markSuperRegs(Reserved, RISCV::DUMMY_REG_PAIR_WITH_X0);
if (Subtarget.hasStdExtE())
  for (MCPhysReg Reg = RISCV::X16_H; Reg <= RISCV::X31_H; Reg++)
    markSuperRegs(Reserved, Reg);
markSuperRegs(Reserved, RISCV::VL);
markSuperRegs(Reserved, RISCV::VTYPE);
markSuperRegs(Reserved, RISCV::VXSAT);
markSuperRegs(Reserved, RISCV::VXRM);
markSuperRegs(Reserved, RISCV::FRM);
markSuperRegs(Reserved, RISCV::FFLAGS);
markSuperRegs(Reserved, RISCV::SF_VCIX_STATE);
markSuperRegs(Reserved, RISCV::SSP);
for (MCPhysReg Reg = RISCV::T0; Reg <= RISCV::T15; Reg++)
  markSuperRegs(Reserved, Reg);
```

这张清单可以按来源分组：

- 常量：`x0`（由 `isConstantPhysReg` 循环处理）。
- 固定用途：`sp/gp/tp`、帧指针 `x8`（仅 `hasFP`）、基址指针（仅 `hasBP`）、影子栈指针 `SSP`。
- 硬件限制：RVE 只有 16 个 GPR，`x16-x31` 必须保留。
- 不可分配的状态寄存器：`VL/VTYPE/VXSAT/VXRM`、`FRM/FFLAGS`、`SF_VCIX_STATE`、XSfmmbase 的 `T0-T15`。
- 特殊调用约定：Graal 保留 `x23/x27`。
- 占位：`DUMMY_REG_PAIR_WITH_X0`。

`hasFP` 不是“函数一定用帧指针”，而是 FrameLowering 的判断结果。默认情况下叶子函数不需要 FP，`x8` 可以当普通 callee-saved 用；启用 `-fno-omit-frame-pointer` 或存在可变长栈对象时，`x8` 被保留为 FP，`getFrameRegister` 也会返回 `RISCV::X8`：

```cpp
Register RISCVRegisterInfo::getFrameRegister(const MachineFunction &MF) const {
  return TFI->hasFP(MF) ? RISCV::X8 : RISCV::X2;
}
```

这条一行函数是读栈帧代码的第一把钥匙：`sd s0, ...` 里的 `s0` 可能只是普通保存，也可能是帧指针，取决于 `hasFP`。

### 11.6 eliminateFrameIndex：FrameIndex 变成立即数或地址计算

栈帧布局完成后，指令里还留着 `FrameIndex` 伪操作数。`eliminateFrameIndex` 负责把它换成“基址寄存器 + 立即数”，签名是：

```cpp
bool RISCVRegisterInfo::eliminateFrameIndex(MachineBasicBlock::iterator II,
                                            int SPAdj, unsigned FIOperandNum,
                                            RegScavenger *RS) const;
```

流程：

1. `getFrameLowering(MF)->getFrameIndexReference(MF, FrameIndex, FrameReg)` 取得该槽相对帧基址的偏移与基址寄存器（`sp` 或 `fp`）。
2. 如果不是 RVV spill，把指令里原有的立即数加进偏移（`MI.getOperand(FIOperandNum + 1).getImm()`）。
3. RV32 上如果固定偏移超出有符号 32 位，直接 `reportFatalUsageError("Frame offsets outside of the signed 32-bit range not supported on RV32")`。
4. 取低 12 位 `Lo12 = SignExtend64<12>(Val)`，能塞进 `simm12` 就填进立即数；否则清掉立即数，剩余部分交给 `adjustReg`。
5. `adjustReg` 生成 `lui/addi` 或 `add` 序列，把新地址放进一个虚拟寄存器，再把 FrameIndex 操作数改成这个寄存器。

源码里有一串按 opcode 分支的特殊情况，读的时候可以当成“哪些指令的立即数字段不是普通 simm12”的清单：

- `ADDI` 且偏移超出 12 位：立即数清零，走 `adjustReg`，避免把大偏移硬塞进 ADDI。
- `PREFETCH_I/R/W`：偏移必须 32 字节对齐，不对齐就清零。
- `MIPS_PREF`：只接受 9 位无符号立即数。
- `PseudoRV32ZdinxLD/SD`、`PseudoLD_RV32_OPT/SD_RV32_OPT`：低 12 位接近 2044 时清零，因为后续会拆成两条指令，第二条要加 4。
- Xqcilo 的宽指令：偏移超出 12 位时提升到 26 位立即数的宽 opcode。

这里有一个重要的边界：`eliminateFrameIndex` 自己不做寄存器分配，它创建的虚拟寄存器要等后续 pass 分配；如果发生在 post-RA，则由 `RegScavenger` 保证有可用临时寄存器。这也是为什么签名里有一个 `RegScavenger *RS`，虽然 RISC-V 的实现大多不直接用 `RS`。

### 11.7 观察寄存器压力：从 vreg 到物理寄存器

用这个函数观察整数与浮点混合的参数传递：

```llvm
define dso_local double @mix(i64 %a, i64 %b, double %x, float %y) {
entry:
  %m = mul i64 %a, %b
  %i = sitofp i64 %m to double
  %f = fadd double %i, %x
  %y2 = fpext float %y to double
  %r = fadd double %f, %y2
  %u = fptoui double %r to i64
  %d = uitofp i64 %u to double
  ret double %d
}
```

`llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -O2 -stop-after=finalize-isel mix.ll -o mix-isel.mir` 的真实片段：

```text
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: fpr64, preferred-register: '', flags: [  ] }
  - { id: 3, class: fpr32, preferred-register: '', flags: [  ] }
  - { id: 4, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 5, class: fpr64, preferred-register: '', flags: [  ] }
  - { id: 6, class: fpr64, preferred-register: '', flags: [  ] }
  - { id: 7, class: fpr64, preferred-register: '', flags: [  ] }
  - { id: 8, class: fpr64, preferred-register: '', flags: [  ] }
  - { id: 9, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 10, class: fpr64, preferred-register: '', flags: [  ] }
liveins:
  - { reg: '$x10', virtual-reg: '%0' }
body:             |
  liveins: $x10, $x11, $f10_d, $f11_f
  %3:fpr32 = COPY $f11_f
  %2:fpr64 = COPY $f10_d
  %1:gpr = COPY $x11
  %0:gpr = COPY $x10
  %4:gpr = MUL %0, %1
  %5:fpr64 = nofpexcept FCVT_D_L killed %4, 7, implicit $frm
  %6:fpr64 = nofpexcept FADD_D killed %5, %2, 7, implicit $frm
  %7:fpr64 = nofpexcept FCVT_D_S %3, 0
  %8:fpr64 = nofpexcept FADD_D killed %6, killed %7, 7, implicit $frm
  %9:gpr = nofpexcept FCVT_LU_D killed %8, 1
  %10:fpr64 = nofpexcept FCVT_D_LU killed %9, 7, implicit $frm
```

三点观察：

- `liveins` 已经告诉你 ABI：`i64` 参数在 `$x10/$x11`，`double` 在 `$f10_d`，`float` 在 `$f11_f`。这是寄存器类 `gpr/fpr64/fpr32` 的实例化结果。
- `%3:fpr32` 对应 `float`，`%2:fpr64` 对应 `double`；`fpext` 在 isel 之后变成 `FCVT_D_S`，不是 `FPExt` 指令。
- `%4:gpr = MUL` 之后的 `FCVT_D_L` 直接消费 `%4`，说明 MUL 的结果没有落栈。

再加 `-stop-after=postrapseudos`，同一段代码变成：

```text
noVRegs:         true
  renamable $x10 = MUL killed renamable $x10, killed renamable $x11
  renamable $f15_d = nofpexcept FCVT_D_L killed renamable $x10, 7, implicit $frm
  renamable $f15_d = nofpexcept FADD_D killed renamable $f15_d, killed renamable $f10_d, 7, implicit $frm
  renamable $f14_d = nofpexcept FCVT_D_S killed renamable $f11_f, 0
  renamable $f15_d = nofpexcept FADD_D killed renamable $f15_d, killed renamable $f14_d, 7, implicit $frm
  renamable $x10 = nofpexcept FCVT_LU_D killed renamable $f15_d, 1
  renamable $f10_d = nofpexcept FCVT_D_LU killed renamable $x10, 7, implicit $frm
```

分配结果完全对应 11.3 节的顺序：整数临时用 `$x10`（a0），浮点临时先用 `$f15_d`（fa5），下一只 `$f14_d`（fa4），返回值用 `$f10_d`（fa0）。`renamable` 表示这个物理寄存器还可以被重命名以消除复制；它不影响“已经是物理寄存器”这个事实。

最终汇编：

```asm
mix:
	mul	a0, a0, a1
	fcvt.d.l	fa5, a0
	fadd.d	fa5, fa5, fa0
	fcvt.d.s	fa4, fa1
	fadd.d	fa5, fa5, fa4
	fcvt.lu.d	a0, fa5, rtz
	fcvt.d.lu	fa0, a0
	ret
```

`fa5` 与 `fa4` 的出现不是随机，而是 `FPR64` 分配顺序前两位。若把 `-mattr` 改成只开 `+f`，`double` 会走 soft-float，代码形状会完全不同；这是第 12 章的主题。

压力实验用 8 个 `i64` 参数与一次 8 参数调用：

```llvm
declare i64 @ext8(i64, i64, i64, i64, i64, i64, i64, i64)
define dso_local i64 @pressure2(i64 %a0, i64 %a1, i64 %a2, i64 %a3,
                                i64 %a4, i64 %a5, i64 %a6, i64 %a7) {
entry:
  %s0 = mul i64 %a0, %a1
  %s1 = mul i64 %a2, %a3
  %s2 = mul i64 %a4, %a5
  %s3 = mul i64 %a6, %a7
  %s4 = add i64 %s0, %s1
  %s5 = add i64 %s2, %s3
  %s6 = xor i64 %s4, %s5
  %s7 = sub i64 %s4, %s5
  %c = call i64 @ext8(i64 %s0, i64 %s1, i64 %s2, i64 %s3,
                      i64 %s4, i64 %s5, i64 %s6, i64 %s7)
  %t0 = add i64 %c, %s0
  %t1 = add i64 %t0, %s1
  %t2 = add i64 %t1, %s2
  %t3 = add i64 %t2, %s3
  %t4 = add i64 %t3, %s6
  %t5 = add i64 %t4, %s7
  ret i64 %t5
}
```

实测汇编（节选）：

```asm
pressure2:
	addi	sp, sp, -48
	sd	ra, 40(sp)                      # 8-byte Folded Spill
	sd	s0, 32(sp)                      # 8-byte Folded Spill
	sd	s1, 24(sp)                      # 8-byte Folded Spill
	sd	s2, 16(sp)                      # 8-byte Folded Spill
	sd	s3, 8(sp)                       # 8-byte Folded Spill
	mul	a0, a0, a1
	mul	a1, a2, a3
	mul	a2, a4, a5
	mul	a3, a6, a7
	add	s0, a0, a1
	add	s1, a2, a3
	xor	s2, s0, s1
	sub	s3, s0, s1
	mv	a4, s0
	mv	a5, s1
	mv	a6, s2
	mv	a7, s3
	call	ext8
	add	s1, s1, s2
	add	a0, s0, a0
	add	s1, s1, s3
	add	a0, s1, a0
	ld	ra, 40(sp)
	...
```

8 个中间值要跨 `call ext8` 存活，而调用会破坏 `a0-a7` 与 `t0-t6`，所以分配器把它们放进 `s0-s3`，并在 prologue 保存。栈帧 48 字节 = 5 个 8 字节槽 + 8 字节对齐填充（`ra`、`s0-s3` 共 5 个，40 字节，向上取整到 48）。`addi sp, sp, -48` 同时满足 RISC-V 的 16 字节栈对齐。

## 可跑实验

实验脚本：`code/labs/11-registers.ps1`，五个用例。所有输出都加 `OUT:` 前缀，避免 PowerShell 把 stderr 当终止错误。

### 实验 A：观察 `finalize-isel` 的 vreg 与 class

```powershell
.\code\tools\run.ps1 labs\11-registers.ps1 -Case A
```

脚本生成 11.7 节的 `mix.ll`，运行：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -O2 -stop-after=finalize-isel mix.ll -o mix-isel.mir
```

预期：`noVRegs: false`，`registers:` 有 11 项，class 出现 `gpr / fpr64 / fpr32`，`liveins` 显示 `$x10, $x11, $f10_d, $f11_f`。重点确认 `%3:fpr32` 与 `%2:fpr64` 的类型差异来自 IR 的 `float`/`double`，不是名字。

### 实验 B：观察 `postrapseudos` 的物理寄存器与最终汇编

```powershell
.\code\tools\run.ps1 labs\11-registers.ps1 -Case B
```

同一个 IR，分别跑 `-stop-after=postrapseudos` 与默认汇编输出。预期：`noVRegs: true`，出现 `renamable $x10 = MUL`、`renamable $f15_d`、`renamable $f14_d`、`renamable $f10_d`；最终汇编出现 `fa5`、`fa4`、`fa0`。把 `$f15_d` 与 `fa5` 对上，是理解“物理编号 → ABI 名”的关键一步。

### 实验 C：压力与 spill

```powershell
.\code\tools\run.ps1 labs\11-registers.ps1 -Case C
```

预期：`addi sp, sp, -48`，5 条 `sd` 带 `8-byte Folded Spill` 注释，调用后 5 条对应的 `ld`。数一数 `s0-s3` 的个数，并与 11.5 节的 `CSR_ILP32_LP64` 对照：`s0-s3` 正是 `X8, X9, X18, X19`。

### 实验 D：零寄存器与 ABI 名回显

```powershell
.\code\tools\run.ps1 labs\11-registers.ps1 -Case D
```

输入三行，`llvm-mc -triple=riscv64 "-mattr=+c" -show-encoding` 的实测输出：

```text
	mv	a0, a1                          # encoding: [0x2e,0x85]
	li	zero, 1                         # encoding: [0x13,0x00,0x10,0x00]
	sd	s0, 0(sp)                       # encoding: [0x22,0xe0]
```

三条各说明一件事：`add a0, zero, a1` 被打印成 `mv a0, a1` 并压缩成 `c.mv`；`addi x0, x0, 1` 的结果被丢弃，打印成 `li zero, 1`；`sd x8, ...` 按 ABI 名打印成 `sd s0, ...` 并压缩。零寄存器在文本上仍然叫 `zero`，但它不是可分配资源。

### 失败/边界例子：`-stop-after=greedy` 不等于“已分配”

```powershell
llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -O2 -stop-after=greedy mix.ll -o mix-greedy.mir
```

本机 23.1.4 的实测结果：`noVRegs: false`，`registers:` 仍然列出 11 个 vreg，body 与 `finalize-isel` 完全相同。`-stop-after=virtregrewriter` 也是同样的结果。只有 `-stop-after=postrapseudos` 才显示 `noVRegs: true` 与物理寄存器。这不是文档笔误，而是本版本的实测行为；写 MIR 断言时不要用 pass 名字猜时机。

另外，`-stop-after=prologepilog` 在本机报：

```text
LLVM ERROR: "prologepilog" pass is not registered.
```

改用 `-stop-after=postrapseudos` 这类已注册的 pass 名；`prologepilog` 本身不是本机可用的 stop 名。名字是否注册，用 `llc --help-hidden | Select-String stop-after` 查不到完整列表时，最稳的办法是先用 `-print-after` 试一个已知名字。

## 源码落点

1. `RISCVRegisterInfo.td`
   - `RISCVReg` / `RISCVReg16` / `RISCVReg32` / `RISCVReg64`：`sub_16`、`sub_32`、`HWEncoding`。
   - `X0` 的 `isConstant = true`；`X8` 的 `["s0", "fp"]`。
   - `XLenRI` / `XLenPairRI`：spill size 与 alignment 的真正来源。
   - `GPR`、`FPR16/32/64/128`、`VR/VRM2/VRM4/VRM8`：类型、对齐、分配顺序。
   - `X0_Pair`、`DUMMY_REG_PAIR_WITH_X0`、`GPRPair` 的 `CoveredBySubRegs`。
2. `RISCVRegisterInfo.cpp`
   - `getCalleeSavedRegs`：ABI switch 与 interrupt/GHC/PreserveMost 分支。
   - `getReservedRegs`：`isConstantPhysReg` 循环、`markSuperRegs`、RVE、状态寄存器。
   - `getFrameRegister`：`hasFP ? X8 : X2`。
   - `eliminateFrameIndex`：`getFrameIndexReference`、`Lo12/Lo26`、`adjustReg`。
   - `getRegClassForTypeOnBank`：GlobalISel 下类型到 `GPR/FPR16/FPR32/FPR64/VR*` 的映射。
3. `RISCVCallingConv.td`
   - `CSR_ILP32E_LP64E`、`CSR_ILP32_LP64`、`CSR_ILP32F_LP64F`、`CSR_ILP32D_LP64D`。
   - `CSR_Interrupt`、`CSR_RT_MostRegs`、`CSR_NoRegs`。
4. `RISCVInstrInfo.cpp`
   - `copyPhysReg`、`storeRegToStackSlot`、`loadRegFromStackSlot`：spill 的实际指令选择。
5. `RISCVFrameLowering.cpp`
   - `determineCalleeSaves`、`processFunctionBeforeFrameFinalized`、`emitPrologue`：谁决定 `s0-s3` 要保存。

## 常见坑

1. **把 vreg 的 `class` 当成物理寄存器**。`%4:gpr` 只说明它属于 `GPR` 类，不代表它是 `a0`；到 `postrapseudos` 才有物理编号。
2. **以为分配从 `a0` / `fa0` 开始**。`FPR64` 的顺序是 `fa5, fa4, ..., fa0`，第一个临时浮点寄存器是 `fa5`；`fa0` 通常留给返回值或第一个浮点参数。
3. **把 `x0` 当普通寄存器分配**。`isConstant` 与 `getReservedRegs` 双重排除；`addi x0, x0, 1` 的效果是丢弃结果，不是写入。
4. **用 C 类型大小推 spill 大小**。spill 由 `RegInfo` 决定：RV64 的 `GPR` 是 64/64，`FPR32` 是 32/32，`GPRPair` 是 128 位 spill、64 位对齐。`f32` 放进 GPR 容器时按 `XLenVT` 存，是 64 位。
5. **混淆 `getReservedRegs` 与 `getCalleeSavedRegs`**。前者是“永远不能分配”，后者是“被调函数如果想用就要保存”。`s0` 是 callee-saved，但不是 reserved；`sp` 两者都不是“可分配”。
6. **在 RVE 上假设 32 个 GPR**。`hasStdExtE()` 时 `x16-x31` 全部保留，只剩 `x0-x15`，参数寄存器也缩到 `a0-a5`。
7. **以为 `hasFP` 一定为真**。默认叶子函数不需要 FP，`x8` 可作普通 callee-saved；只有需要固定帧基址时才保留为 FP。
8. **用 `-stop-after=greedy` 判断分配是否完成**。本机 23.1.4 上它仍显示 vreg；用 `-stop-after=postrapseudos`。
9. **忽略 `DUMMY_REG_PAIR_WITH_X0`**。它不属于 `GPR`，但必须保留；把它塞进 `GPR` 会改变寄存器压力上限。
10. **在 RV32 上使用超出 32 位的帧偏移**。`eliminateFrameIndex` 会直接 fatal，不是静默截断。

## 自我检查

- [ ] 能说出 `X#_H`、`X#_W`、`X#` 三者的子寄存器关系，以及 RV32/RV64 的差异。
- [ ] 能解释 `X0` 的 `isConstant = true` 与 `getReservedRegs` 里的 `isConstantPhysReg` 循环如何共同保证 `x0` 不被分配。
- [ ] 能写出 `GPR` 的完整分配顺序，并解释“caller-save → callee-save → specials”的动机。
- [ ] 能解释为什么 `FPR64` 的第一个临时寄存器是 `fa5` 而不是 `fa0`。
- [ ] 能根据 `XLenRI` 算出 RV64 上一个 GPR 的 spill size 与 alignment。
- [ ] 能区分 `CSR_ILP32_LP64` 与 `getReservedRegs` 的职责，并各举两个寄存器。
- [ ] 能读懂 `-stop-after=finalize-isel` 的 `registers:` 与 `liveins:`，并把 `%3:fpr32` 对上 `float` 参数。
- [ ] 能解释 `noVRegs: true` 的含义，以及为什么 `-stop-after=greedy` 在本机不够。
- [ ] 能从 48 字节栈帧与 5 条 `sd` 反推出 `ra + s0-s3` 的保存布局。

## 延伸阅读

- LLVM 源码：`RISCVRegisterInfo.td`、`RISCVRegisterInfo.cpp`、`RISCVCallingConv.td`。
- LLVM 文档：TableGen `RegisterClass`、`RegInfo`、`RegisterWithSubRegs`、`CoveredBySubRegs`。
- RISC-V psABI：整数/浮点寄存器角色、callee-saved 列表、RV32E 的 16 寄存器限制。
- 课程第 03 章：MIR 的结构与 `-stop-after`；第 12 章：这些寄存器如何被调用约定选中；第 13 章：`eliminateFrameIndex` 之后的栈帧布局。
- 本机命令：`llc --help-hidden` 查 pass 名，`llvm-tblgen -gen-register-info` 观察生成的寄存器表（需要完整构建环境）。

[上一章：MC 层与目标文件](./10-mc-layer-and-objects.md) | [下一章：调用约定](./12-calling-convention.md)