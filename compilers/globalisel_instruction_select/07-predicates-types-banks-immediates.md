# 第 7 章 谓词系统 I：类型、寄存器 bank 与立即数

> **本章目标**：把 matcher table 里最常见的三类“守卫条件”拆开：操作数类型（LLT）、寄存器 bank、立即数谓词。读完后你应该能看着一条 `.td` pattern，预测它会生成哪些 `GIM_Check*`，以及为什么同一个 `G_ADD` 在 `4095`、`4096`、`5000` 三个常量上会选出三种不同形态。
>
> **前置**：第 5、6 章。**环境**：LLVM 23.1.4，AArch64 与 x86-64 宿主均可。

---

## 7.1 为什么匹配表需要“谓词”

第 6 章说过，matcher table 是字节码：`GIM_CheckOpcode` 检查根指令是不是 `G_ADD`，`GIM_RecordInsn` 把某个操作数的定义者拉进来，`GIR_*` 负责生成机器指令。如果只有 opcode 检查，那么一条 `G_ADD s64` 和一条 `G_ADD s32` 会走同一条规则，这显然不行。

于是每条规则在“结构匹配”之外，还需要一组**谓词（predicate）**：

- **类型谓词**：这个虚拟寄存器是 `s64` 还是 `s32`？是指针还是整数？向量元素多宽？
- **bank 谓词**：它已经在 GPR bank，还是在 FPR bank？
- **值谓词**：它是不是常量？常量是否落在指令能编码的范围内？是不是某个特定值？
- **特性谓词**：当前 subtarget 有没有 `+fullfp16`、`+neon`？（第 8 章）
- **内存/原子谓词**：访问大小、对齐、地址空间、ordering 是否匹配？（第 8 章）
- **use 谓词**：这个中间值是否只有一个使用者，能不能安全折叠？（第 8、9 章）

本章只讲前三类。它们有一个共同点：**都作用在单个操作数上**，而且都在 `executeMatchTable` 的线性执行路径里完成。先把这三类吃透，再看 feature 和 fold，就不会把“匹配失败”与“渲染失败”混为一谈。

### 7.1.1 谓词失败不是编译错误

所有 `GIM_Check*` 失败时都调用同一个 `handleReject()`：

```cpp
auto handleReject = [&]() -> RejectAction {
  if (OnFailResumeAt.empty())
    return RejectAndGiveUp;
  CurrentIdx = OnFailResumeAt.pop_back_val();
  return RejectAndResume;
};
```

也就是说，谓词不匹配只是“这条规则不适用”，执行器会回到 `GIM_Try` 记录的候选位置继续试。只有整个候选集合都失败，`selectImpl` 才返回 `false`，然后进入 fallback 或报错。这个区别很重要：

- **谓词写错**：通常表现为“选成了另一条指令”或“悄悄 fallback”，不一定是崩溃。
- **谓词写得太宽**：可能选出一条无法编码的指令，最后在 MC 层或 verifier 才炸。
- **谓词写得太窄**：本来能选的指令选不出来，性能下降或 fallback。

所以调试谓词问题时，第一步不是改 C++，而是确认“期望的规则到底有没有进入候选集”。

---

## 7.2 类型谓词：LLT 才是 gMIR 的类型

### 7.2.1 从 MVT 到 LLT

SelectionDAG 用 `MVT`/`EVT` 描述类型，GlobalISel 用 **LLT（Low-Level Type）**。常见形式：

| LLT | 含义 | 典型来源 |
| --- | --- | --- |
| `s32` | 32 位标量整数 | `i32`、legalize 后的 `i8` |
| `s64` | 64 位标量整数 | `i64`、AArch64 GPR64 |
| `s128` | 128 位标量整数 | `i128`、partial legalize |
| `p0` | 地址空间 0 的指针 | `ptr`、`ptr addrspace(0)` |
| `p270` | 地址空间 270 的指针 | AArch64 的某些特殊地址空间 |
| `<4 x s32>` | 4 元素、每元素 32 位的固定向量 | `<4 x i32>` |
| `<vscale x 4 x s32>` | 可伸缩向量 | SVE/SVE2 |

LLT 不是“IR 类型的复制”。Legalizer 会把小整数提升、把非法向量拆分或标量化，因此进入 InstructionSelect 时看到的类型，往往已经和前端 IR 不一样。例如 AArch64 上 `i8 add` 在 legalize 后通常变成 `s32 G_ADD`，第 4 章已经验证过。

### 7.2.2 `GIM_CheckType` 到底比较什么

执行实现非常直接（`GIMatchTableExecutorImpl.h`）：

```cpp
case GIM_RootCheckType:
case GIM_CheckType: {
  uint64_t InsnID = (MatcherOpcode == GIM_RootCheckType) ? 0 : readULEB();
  uint64_t OpIdx = readULEB();
  int TypeID = readS8();

  MachineOperand &MO = State.MIs[InsnID]->getOperand(OpIdx);
  if (!MO.isReg() || MRI.getType(MO.getReg()) != getTypeFromIdx(TypeID)) {
    if (handleReject() == RejectAndGiveUp)
      return false;
  }
  break;
}
```

三个细节：

1. **它只接受寄存器操作数**。如果这个操作数是立即数、`MBB`、`CImm`，`!MO.isReg()` 直接失败。想匹配 `G_CONSTANT` 的常量值，要用 7.4 的立即数谓词，而不是 `GIM_CheckType`。
2. **它是精确相等**，不是“至少这么大”。`s32` 不会匹配 `s64`，`p0` 不会匹配 `p1`。
3. **`GIM_RootCheckType` 省略 `InsnID`**，固定检查 `MIs[0]`。`MIs[0]` 永远是当前根指令，根类型检查在生成表里出现得非常多，省一个 ULEB128 能减小表体积。

`TypeID` 是一个 `s8`，指向执行器静态类型表里的下标。表里存的是 `LLT`，不是字符串；TableGen 在生成 `<Target>GenGlobalISel.inc` 时已经把类型去重并编号。

### 7.2.3 `GIM_CheckPointerToAny`

指针 pattern 有时只关心“是 64 位指针”，不关心地址空间。`GIM_CheckPointerToAny` 为此存在：

```cpp
case GIM_CheckPointerToAny: {
  uint64_t InsnID = readULEB();
  uint64_t OpIdx = readULEB();
  uint64_t SizeInBits = readULEB();
  const LLT Ty = MRI.getType(MO.getReg());

  // iPTR must be looked up in the target.
  if (SizeInBits == 0) {
    MachineFunction *MF = State.MIs[InsnID]->getParent()->getParent();
    const unsigned AddrSpace = Ty.getAddressSpace();
    SizeInBits = MF->getDataLayout().getPointerSizeInBits(AddrSpace);
  }

  if (MO.isReg()) {
    if (!Ty.isPointer() || Ty.getSizeInBits() != SizeInBits)
      if (handleReject() == RejectAndGiveUp)
        return false;
  } else if (handleReject() == RejectAndGiveUp)
    return false;
  break;
}
```

它只要求 `Ty.isPointer()` 且总位宽匹配，不要求地址空间相同；`SizeInBits == 0` 表示 `iPTR`，运行时按地址空间查 `DataLayout`。AArch64 的 `p0`、`p270` 都是 64 位指针，所以只想约束位宽的 pattern 用 `CheckPointerToAny`，必须区分 tagged pointer 的 pattern 用精确 `GIM_CheckType`。
### 7.2.4 例子：同一条 `add`，不同类型

先看一个最小 IR：

```llvm
define i64 @add64(i64 %x, i64 %y) {
  %r = add i64 %x, %y
  ret i64 %r
}

define i32 @add32(i32 %x, i32 %y) {
  %r = add i32 %x, %y
  ret i32 %r
}
```

在 AArch64、`-O0 -global-isel` 下，停在 instruction-select 之前：

```bash
llc -mtriple=aarch64-linux-gnu -global-isel -O0 \
    -stop-before=instruction-select add.ll -o -
```

会看到类似：

```mir
body:             |
  bb.1 (%ir-block.0):
    liveins: $x0, $x1

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```

`i32` 版本则是 `%2:gpr(i32) = G_ADD %0, %1`。选指令时，`ADDXrr` 的 pattern 要求 `s64`，`ADDWrr` 的 pattern 要求 `s32`；`GIM_CheckType` 把两者分开。若没有类型检查，matcher 只能靠 opcode 和 bank 猜，必然选错位宽。

> **注意**：MIR 打印里的 `gpr(i64)` 是“寄存器 bank + LLT”，不是最终的 `GPR64` 寄存器类。InstructionSelect 之后才会把虚拟寄存器约束到具体寄存器类，并在寄存器分配时落成物理寄存器。

### 7.2.5 类型谓词不负责合法性

`GIM_CheckType` 通过只说明“当前虚拟寄存器的 LLT 等于表里记录的类型”，不证明该类型能被这条指令处理。合法性由 Legalizer、pattern 类型映射和渲染阶段的寄存器类约束共同保证。若 Legalizer 留下 target 不支持的 `s128`，类型谓词可能仍匹配，但渲染或 verifier 会失败。类型谓词是匹配条件，不是合法性证明。
---

## 7.3 寄存器 bank 谓词：为什么选指令时还要看 bank

### 7.3.1 bank 与寄存器类的区别

GlobalISel 的寄存器分配分两步：

- **RegBankSelect**：把虚拟寄存器分到粗粒度的 **bank**，例如 AArch64 的 `GPR`、`FPR`；
- **InstructionSelect**：根据选中的指令，把虚拟寄存器约束到具体的 **寄存器类**，例如 `GPR64`、`FPR64`；
- **Register Allocator**：真正分配物理寄存器。

bank 是“这一类指令能操作的寄存器集合”，寄存器类是“这个 bank 里满足某个具体约束的子集”。AArch64 的 `GPR` bank 里同时有 `GPR32` 和 `GPR64`，`FPR` bank 里有 `FPR8/16/32/64/128`。选 `ADDXrr` 时，操作数必须在 GPR bank；选 `FADDDrr` 时，操作数必须在 FPR bank。

### 7.3.2 `GIM_CheckRegBankForClass` 的实现

头文件注释写得很清楚：参数是一个**寄存器类**，但检查的是它对应的 bank。

```cpp
case GIM_CheckRegBankForClass: {
  uint64_t InsnID =
      (MatcherOpcode == GIM_RootCheckRegBankForClass) ? 0 : readULEB();
  uint64_t OpIdx = readULEB();
  uint16_t RCEnum = readU16();

  MachineOperand &MO = State.MIs[InsnID]->getOperand(OpIdx);
  if (!MO.isReg() ||
      &RBI.getRegBankFromRegClass(*TRI.getRegClass(RCEnum),
                                  MRI.getType(MO.getReg())) !=
          RBI.getRegBank(MO.getReg(), MRI, TRI)) {
    if (handleReject() == RejectAndGiveUp)
      return false;
  }
  break;
}
```

注意 `getRegBankFromRegClass(RC, LLT)`：同一个寄存器类在不同 LLT 下可能映射到不同 bank，所以必须把类型一起传进去。`getRegBank(MO.getReg(), MRI, TRI)` 取的是 RegBankSelect 已经写进 `MRI` 的结果。

`GIM_RootCheckRegBankForClass` 同样只是省略 `InsnID`，固定检查根指令。

### 7.3.3 一个 bank 不匹配的典型场景

```llvm
define double @f(double %x, i64 %y) {
  %xi = bitcast double %x to i64
  %r = add i64 %xi, %y
  %rf = bitcast i64 %r to double
  ret double %rf
}
```

在 AArch64 上，`%x` 来自 FPR bank，`%y` 是 GPR bank。`bitcast` 不产生机器指令，但两条 use 的 bank 不同，RegBankSelect 会插入 COPY。进入 InstructionSelect 前的 gMIR 大致是：

```mir
%0:fpr(s64) = COPY $d0
%1:gpr(s64) = COPY $x0
%2:gpr(s64) = COPY %0(s64)
%3:gpr(s64) = G_ADD %2, %1
%4:fpr(s64) = COPY %3(s64)
$d0 = COPY %4(s64)
```

`G_ADD` 的操作数已经是 GPR bank，所以 `ADDXrr` 的 `GIM_CheckRegBankForClass` 能通过。若 RegBankSelect 因代价模型选了另一条路径，让 `G_ADD` 留在 FPR bank，matcher 就会拒绝 `ADDXrr`，转而寻找 FPR 版本的规则；找不到时 fallback。**InstructionSelect 不会替你改 bank**，它只在已有 bank 上做选择。

### 7.3.4 为什么不在 InstructionSelect 里做 bank 选择

bank 选择需要全局代价模型（COPY 数量、cross-bank 代价、寄存器压力），而指令选择是局部、自底向上的。如果在选指令时临时插 COPY，可能破坏已选好的 use-def 结构，还会让 matcher 的 `MIs[]` 记录失效。因此 LLVM 把它拆成两个 pass：RegBankSelect 决定“粗粒度放在哪”，InstructionSelect 只决定“在这个 bank 里选哪条指令”。
---

## 7.4 立即数谓词：从 `ImmLeaf` 到 `GIM_Check*Imm*`

### 7.4.1 三类 `ImmLeaf`

SelectionDAG 的 `ImmLeaf` 是“匹配一个带约束的立即数”的 pattern fragment。GlobalISel 能导入它，但 C++ 谓词里 `Imm` 的静态类型取决于用哪个子类：

| TableGen 类 | `Imm` 的类型 | 适用场景 |
| --- | --- | --- |
| `ImmLeaf` | `int64_t`（符号扩展后） | 值能放进 64 位有符号整数，且希望按符号解释 |
| `IntImmLeaf` | `APInt` | 需要零扩展、值超过 64 位、或要精确位宽 |
| `FPImmLeaf` | `APFloat` | 浮点常量，如 `G_FCONSTANT` |

`TargetSelectionDAG.td` 的注释：

```tablegen
// ImmLeaf is a pattern fragment for immediates that match a constraint.  The
// constraint is a C++ snippet that is run on the immediate (always with the
// value sign extended out to an int64_t) as Imm.
class ImmLeaf<ValueType vt, code pred, SDNodeXForm xform = NOOP_SDNodeXForm,
              SDNode ImmNode = imm>
  : PatFrag<(ops), (vt ImmNode), [{}], xform> {
  let ImmediateCode = pred;
  bit FastIselShouldIgnore = false;
  bit IsAPInt = false;
  bit IsAPFloat = false;
}
```

三个子类只是把 `IsAPInt` / `IsAPFloat` 打开：

```tablegen
class IntImmLeaf<ValueType vt, code pred, SDNodeXForm xform = NOOP_SDNodeXForm>
    : ImmLeaf<vt, pred, xform> {
  let IsAPInt = true;
  let FastIselShouldIgnore = true;
}

class FPImmLeaf<ValueType vt, code pred, SDNodeXForm xform = NOOP_SDNodeXForm>
    : ImmLeaf<vt, pred, xform, fpimm> {
  let IsAPFloat = true;
  let FastIselShouldIgnore = true;
}
```

`FastIselShouldIgnore = true` 表示 FastISel 不为这类规则生成代码；这不影响 GlobalISel。移植 pattern 时，如果原规则用了 `PatLeaf` 而 GlobalISel 导不进来，换成 `IntImmLeaf` 往往能让它被导入——这是第 5 章提过的“PatLeaf → IntImmLeaf”迁移技巧。

### 7.4.2 `ImmediateCode` 的作用域

`ImmLeaf` 的 `ImmediateCode` 里能直接用这些名字：

| 名字 | 类型/含义 |
| --- | --- |
| `Imm` | 立即数值；类型由 `ImmLeaf`/`IntImmLeaf`/`FPImmLeaf` 决定 |
| `MO` | 当前 `MachineOperand`（GlobalISel 路径） |
| `MF` | 当前 `MachineFunction` |
| `MRI` | 当前 `MachineRegisterInfo` |
| `this` | 目标 `InstructionSelector` 实例 |
| `State` | 当前 `MatcherState` |
| `Operands` | 命名的操作数数组（仅当谓词声明使用 operands） |

一个典型例子是 AArch64 的 `add_like`：

```tablegen
def add_like : PatFrags<(ops node:$lhs, node:$rhs),
                        [(add node:$lhs, node:$rhs),
                         (or_disjoint node:$lhs, node:$rhs)]> {
  let GISelPredicateCode = [{
    return MI.getFlag(MachineInstr::Disjoint);
  }];
}
```

这里用的是 `GISelPredicateCode`，不是 `ImmediateCode`，因为 `add_like` 匹配的是指令本身，不是立即数。它示范了 GlobalISel 谓词的作用域：`MI` 是正在检查的 `MachineInstr`，可以直接读 flag。`PatFrag`/`PatFrags` 的 `GISelPredicateCode` 里还能用 `MF`、`MRI`、`this`、`State`、`Operands`，具体以 `TargetSelectionDAG.td` 的注释为准。

### 7.4.3 matcher 里的几种立即数检查

| 指令 | 检查内容 | 何时生成 |
| --- | --- | --- |
| `GIM_CheckIsImm` | 操作数必须是 `isImm()` | 需要“这是个立即数”但值任意 |
| `GIM_CheckConstantInt` | 操作数是特定整数值 | 常量折叠后精确匹配 |
| `GIM_CheckConstantInt8` | 特定 8 位有符号整数 | 表体积优化 |
| `GIM_CheckI64ImmPredicate` | 用 `int64_t` 调用谓词 | `ImmLeaf` |
| `GIM_CheckAPIntImmPredicate` | 用 `APInt` 调用谓词 | `IntImmLeaf` |
| `GIM_CheckAPFloatImmPredicate` | 用 `APFloat` 调用谓词 | `FPImmLeaf` |
| `GIM_CheckImmOperandPredicate` | 指定操作数上的立即数谓词 | 非根/非默认操作数 |
| `GIM_CheckLiteralInt` | 操作数是字面整数（`isImm()` 或 `isCImm()`） | 特定字面量比较 |

注意 `GIM_CheckIsImm` 只看 `isImm()`，不看 `isCImm()`；而 `GIM_CheckLiteralInt` 两者都接受。为什么？`G_CONSTANT` 的操作数通常是 `CImm`（`ConstantInt`），而某些伪指令/立即数操作数是普通 `Imm`。写自定义 pattern 时，选错检查会导致“明明常量就在那里却匹配不上”。

### 7.4.4 `Imm` 的符号扩展陷阱

`ImmLeaf` 的 `Imm` 是**符号扩展**到 `int64_t` 的。假设你写：

```tablegen
def imm8_unsigned : ImmLeaf<i32, [{ return Imm >= 0 && Imm <= 255; }]>;
```

对 `i32 255` 没问题；对 `i32 -1`（位模式 `0xffffffff`），`Imm` 是 `-1`，谓词返回 false。这通常符合“无符号 8 位常量”的直觉。但如果你想要的是“低 8 位全 1”，就应该用 `IntImmLeaf` 拿到 `APInt`，再检查 `Imm.getZExtValue()` 或具体位。

反过来，`IntImmLeaf` 的 `APInt` 保留了原始位宽。写：

```tablegen
def u32_imm : IntImmLeaf<i32, [{ return Imm.getActiveBits() <= 32; }]>;
```

不会因为值大于 `INT64_MAX` 而发生符号扩展意外。代价是谓词函数签名和生成的表项不同，不能和 `ImmLeaf` 混用。

### 7.4.5 例子：AArch64 `add` 的立即数边界

这是本章最值得亲手跑的实验。AArch64 的 `ADDXri` 接受 12 位无符号立即数，并可附带 12 位左移。`llc` 在 `-O0 -global-isel` 下会为不同常量选出不同形态。

输入：

```llvm
define i64 @add4095(i64 %x) {
  %r = add i64 %x, 4095
  ret i64 %r
}

define i64 @add4096(i64 %x) {
  %r = add i64 %x, 4096
  ret i64 %r
}

define i64 @add5000(i64 %x) {
  %r = add i64 %x, 5000
  ret i64 %r
}
```

命令：

```bash
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 add.ll -o -
```

在 LLVM 23.1.4 的官方 Windows 预编译包上实测输出（节选）：

```asm
add4095:
        add     x0, x0, #4095
        ret

add4096:
        add     x0, x0, #1, lsl #12    // =4096
        ret

add5000:
        mov     w8, #5000               // =0x1388
        add     x0, x0, x8
        ret
```

三个结果对应三种不同的匹配路径：

1. **4095**：`4095 = 0xfff`，正好是 12 位无符号立即数的最大值，直接匹配 `ADDXri` 的 12 位字段。
2. **4096**：`4096 = 1 << 12`，低 12 位为 0，高 12 位为 1。`ADDXri` 的立即数可以左移 12 位，所以编码成 `#1, lsl #12`。
3. **5000**：`5000 = 0x1388`，既不是 12 位无符号数，也不是 12 位对齐的移位值。AArch64 没有“任意 16 位立即数加法”，只能先把 5000 放进寄存器，再走寄存器版 `ADDXrr`。

> 这里看到的是**最终汇编**，不是 matcher 表本身。`5000` 的 `mov w8, #5000` 来自另一条 `G_CONSTANT` 规则的渲染，随后 `G_ADD` 走 `ADDXrr`。GlobalISel 不会把 `5000` 直接折叠进 `ADDXri`，因为编码不存在。

### 7.4.6 这个实验说明了什么

立即数谓词是指令编码能力在 matcher 里的投影：`4096` 能选 `ADDXri`，是因为谓词允许“12 位值 + 12 位左移”；`5000` 落回 `ADDXrr`，是因为谓词拒绝，matcher 转而匹配寄存器版本，常量由 `G_CONSTANT` 规则单独物化。谓词写宽会选出无法编码的指令，写窄会造成次优的 `mov` + `add`。
### 7.4.7 用 MIR 看常量如何进入匹配

停在 instruction-select 之前，`add4095` 的关键 gMIR 是：

```mir
%0:gpr(s64) = COPY $x0
%1:gpr(s64) = G_CONSTANT i64 4095
%2:gpr(s64) = G_ADD %0, %1
$x0 = COPY %2(s64)
RET_ReallyLR implicit $x0
```

`G_ADD` 的操作数 2 是虚拟寄存器 `%1`，不是立即数。`ADDXri` 的 pattern 必须先 `GIM_RecordInsn` 把 `%1` 的定义者 `G_CONSTANT` 拉进来，再检查它的操作数 1。第 9 章会展开“记录定义者 + 折叠”这条链路。
## 7.5 三类谓词在 matcher 表里的顺序

TableGen 生成检查时有一个大致的顺序：先结构、再类型、再 bank、再值。原因很实际——便宜的检查放前面，失败得快。

一条典型的 `ADDXri` 规则可能长这样（伪代码，非实际字节码）：

```text
GIM_Try OnFail=...
  GIM_CheckOpcode MIs[0], G_ADD
  GIM_RootCheckType 1, s64
  GIM_RootCheckType 2, s64
  GIM_RecordInsn NewInsnID=1, MIs[0], OpIdx=2
  GIM_CheckOpcode MIs[1], G_CONSTANT
  GIM_CheckType MIs[1], 0, s64
  GIM_CheckRegBankForClass MIs[1], 0, GPR64
  GIM_CheckI64ImmPredicate MIs[1], Predicate=isAddSubImm
  GIR_...
```

真实表里还会有 `GIM_CheckNumOperands`、`GIM_CheckIsSafeToFold`、bank 检查等，顺序也可能因 pattern 结构而变。但这个骨架解释了“为什么类型/bank 检查总在立即数谓词之前”：如果类型都不对，没必要去读常量值。

## 7.6 踩雷集锦

### 雷 1：把 bank 当成寄存器类

**错误**：“`GIM_CheckRegBankForClass` 检查的是 `GPR64`，所以它保证操作数一定是 64 位 GPR。”

**纠正**：它检查的是 `GPR64` 对应的 **bank**（AArch64 的 `GPR`），不是具体寄存器类。一个 `GPR32` 虚拟寄存器也在 `GPR` bank 里，可能通过这项检查。真正的位宽约束来自 `GIM_CheckType` 和后续的 `GIR_ConstrainOperandToRegClass`。

### 雷 2：用 `ImmLeaf` 匹配超过 64 位的常量

**错误**：写 `ImmLeaf<i128, ...>`，以为 `Imm` 是 128 位。

**纠正**：`ImmLeaf` 的 `Imm` 是 `int64_t`，值会被符号扩展到 64 位。超过 64 位的常量必须用 `IntImmLeaf`（`APInt`），否则谓词看到的不是完整值。

### 雷 3：忘记立即数在 `G_CONSTANT` 的操作数上

**错误**：在 `G_ADD` 的操作数 2 上直接调用 `GIM_CheckI64ImmPredicate`，期望它读到 `4095`。

**纠正**：`G_ADD` 的操作数 2 是一个**虚拟寄存器**，不是立即数。必须先 `GIM_RecordInsn` 把它的定义者 `G_CONSTANT` 拉进来，再检查 `G_CONSTANT` 的操作数 1。直接检查会命中“操作数不是立即数”的断言或失败路径。

### 雷 4：忽略地址空间

**错误**：用 `GIM_CheckType` 匹配 `p0`，以为所有 64 位指针都能过。

**纠正**：`p0` 与 `p270` 是不同的 LLT。只想约束位宽时用 `GIM_CheckPointerToAny`；必须区分地址空间时用精确的 `GIM_CheckType`。AArch64 的某些特殊地址空间（如 270）在 pattern 里是有意义的。

### 雷 5：把谓词失败当成代码 bug

**错误**：看到 `-global-isel-abort=1` 报 “instruction selection failed”，就认为 matcher 实现有问题。

**纠正**：先确认失败的是哪条规则、哪个操作数、哪个谓词。谓词失败是正常控制流；只有**所有**候选都失败才是问题。用 `-global-isel-abort=1` 把 fallback 变成硬错误，再逐步缩小 MIR，才能定位是类型、bank 还是立即数约束不满足。

---

## 7.7 进阶：自己写一个立即数谓词

假设你要为某个假想指令 `ADDimm9` 写 pattern，它接受 `[-256, 255]` 的有符号 9 位立即数。可以这样写：

```tablegen
def simm9 : ImmLeaf<i32, [{ return Imm >= -256 && Imm <= 255; }]>;
```

如果目标指令实际接受的是“无符号 9 位”，则：

```tablegen
def uimm9 : IntImmLeaf<i32, [{
  return Imm.getActiveBits() <= 9;
}]>;
```

两者的区别在负数和位模式上：

- `simm9` 对 `-1` 返回 true（`-1` 在范围内），对 `256` 返回 false；
- `uimm9` 对 `-1`（`APInt` 的 `0xffffffff`）返回 false，因为 `getActiveBits()` 是 32；对 `255` 返回 true；对 `256` 返回 false。

写完后，用 `llvm-tblgen -gen-global-isel` 生成 `<Target>GenGlobalISel.inc`，再在 `llc` 里用边界值（`-257`、`-256`、`255`、`256`）各跑一遍。**不要只测中间值**；立即数 bug 几乎都藏在边界上。

### 7.7.1 谓词与编码器的责任边界

谓词只保证“matcher 认为这个值可以尝试”，真正的编码合法性由 MC encoder 保证。两者出现偏差时，优先修谓词，让非法值在匹配阶段就被拒绝；不要依赖 encoder 报错兜底，因为那通常是编译失败，而不是 graceful fallback。
## 7.8 动手练习

### 练习 7.1：预测三种常量的选择

不运行 `llc`，先写出下面三个函数在 AArch64 `-O0 -global-isel` 下的预期汇编形态：

```llvm
define i64 @a(i64 %x) { %r = add i64 %x, 255 ret i64 %r }
define i64 @b(i64 %x) { %r = add i64 %x, 256 ret i64 %r }
define i64 @c(i64 %x) { %r = add i64 %x, 4097 ret i64 %r }
```

提示：`4097 = 1 + 4096`，但 `ADDXri` 只能编码“一个 12 位立即数 + 可选左移 12”，不能同时编码两个部分。跑完后对照：

```bash
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 ex7.ll -o -
```

### 练习 7.3：构造一个 bank 不匹配的 MIR

手写一个 MIR，让 `G_ADD` 的一个操作数在 FPR bank，另一个在 GPR bank，然后跑：

```bash
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 \
    -run-pass=instruction-select ex7.mir -o -
```

观察它是被 matcher 拒绝、被 verifier 拒绝，还是直接崩溃。解释你看到的行为。

---

## 7.9 重点整理

- **类型谓词**比较的是 LLT，精确相等；`GIM_CheckType` 只接受寄存器操作数，`GIM_CheckPointerToAny` 只约束“是指针且位宽正确”。
- **bank 谓词**比较的是 `RegisterBank`，不是具体寄存器类；`GIM_CheckRegBankForClass` 用寄存器类反查 bank。
- **立即数谓词**的 `Imm` 类型由 `ImmLeaf`/`IntImmLeaf`/`FPImmLeaf` 决定：`int64_t`、`APInt`、`APFloat`。
- **`ImmLeaf` 是符号扩展的**；超过 64 位或需要零扩展时用 `IntImmLeaf`。
- **常量通常在被记录进来的 `G_CONSTANT` 操作数上**，不在 `G_ADD` 的操作数上。
- **谓词失败是正常控制流**，通过 `handleReject()` 回溯；只有候选耗尽才是选择失败。
- **AArch64 的 `4095/4096/5000` 实验**展示了立即数谓词与指令编码能力的对应关系。

---

## 7.10 自我检核

1. 为什么 `GIM_CheckType` 对立即数操作数会失败？如果我想检查 `G_CONSTANT` 的值，应该用哪条指令？
2. `GIM_CheckRegBankForClass` 的参数是寄存器类，为什么它不能保证操作数的具体位宽？
3. `ImmLeaf` 和 `IntImmLeaf` 在 `Imm` 的静态类型上有什么区别？各自适合什么场景？
4. 在 `add4096` 的例子里，`ADDXri` 的立即数谓词为什么允许 `4096`？编码上它对应什么形式？
5. 如果一条 pattern 的 `GIM_CheckType` 全部通过，但最终仍然 fallback，可能的原因有哪些？（至少列三个，不限于本章内容。）

---

## 7.11 延伸阅读

- **[LLVM 文档：Generic Opcodes](https://llvm.org/docs/GlobalISel/GenericOpcode.html)** — 查 `G_CONSTANT`、`G_FCONSTANT`、`G_ADD` 的操作数约定。本章的立即数例子依赖这些约定；读的时候重点看每个 opcode 的 operand 列表，而不是只看名字。
- **[LLVM 源码：`TargetSelectionDAG.td`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/Target/TargetSelectionDAG.td)** — `ImmLeaf`、`IntImmLeaf`、`FPImmLeaf`、`PatFrags::GISelPredicateCode` 的权威注释都在这里。建议从 `class PatFrags` 读到 `class ImmLeaf`，对照本章 7.4 节。
- **[LLVM 源码：`GIMatchTableExecutor.h`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h)** — `GIM_*` enum 的完整列表和每个操作数的编码格式。本章只列了常用项；写自定义 matcher 时以这个文件为准。
- **[LLVM 源码：`GIMatchTableExecutorImpl.h`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h)** — 每个 `GIM_Check*` 的真实实现。重点读 `GIM_CheckType`、`GIM_CheckRegBankForClass`、`GIM_CheckI64ImmPredicate` 三段，理解它们失败时如何调用 `handleReject()`。


---

## 下一章

[第 8 章 谓词系统 II：feature、内存、原子与 fold 安全性](./08-predicates-features-memory-fold.md) —— 从“单个操作数”扩展到“整个 subtarget、内存访问和 use-def 图”，解释 `+fullfp16` 如何改变 `fadd half` 的选择，以及为什么折叠一条指令之前必须问“它安全吗”。
