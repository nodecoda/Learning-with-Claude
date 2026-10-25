# 第 12 章 Custom SDNode、pseudo 与 GINodeEquiv

> **本章目标**：搞清 target-specific generic opcode 为什么存在、`GINodeEquiv` 的每个字段在 LLVM 23.1.4 里到底控制什么，以及一个 GI pseudo 从产生、选中到 expand 的完整生命周期。
>
> **前置**：第 5–7、11 章。**环境**：LLVM 23.1.4，AArch64 主线，RISC-V 对照。

---

## 12.1 为什么 GlobalISel 需要 target-specific opcode

Legalizer 的职责是把 IR 级 generic 操作降到"目标能选择"的形态。问题在于，有些目标操作在通用 `G_*` 集合里根本没有对应项：

- AArch64 的 `REV32` / `REV64` 是 NEON 语义，不是通用整数操作；
- 小代码模型的 `ADRP + add low` 是一个成对的重定位计算；
- `MOVaddr` 是选择后、调度后才展开的地址伪指令。

如果 Legalizer 只能产出通用 `G_*`，这些操作要么被迫留在 SelectionDAG，要么在 InstructionSelect 里靠复杂的 C++ 模式重新识别 IR 形状。target-specific generic opcode 提供了第三条路：**在 generic 命名空间里定义一个目标自己的操作码，让 Legalizer 可以产生它，让 TableGen 或 C++ selector 可以选中它。**

### 12.1.1 三类"伪"要分清

| 类别 | 例子 | 产生者 | 选中者 | 后续 |
| --- | --- | --- | --- | --- |
| target generic opcode | `G_REV32`、`G_REV64` | PostLegalizerLowering | 导入的 TableGen pattern | 直接变成真实指令 |
| pre-ISel pseudo | `G_ADD_LOW` | Legalizer | C++ `select()` | 变成 `MOVaddr` / `ADDXri` |
| post-ISel pseudo | `MOVaddr` | C++ selector | 已经是"选中"状态 | ExpandPseudo 展开成 `ADRP + ADDXri` |

三者都常被口语化地叫 "pseudo"，但生命周期完全不同。把 `G_REV32` 当成 `MOVaddr` 那样等 expand pass 处理，或者把 `MOVaddr` 当成 generic opcode 再送进 `selectImpl`，都会得到错误的测试与错误的 fallback。

### 12.1.2 generic opcode 不是"随便起的 G_ 名字"

`G_*` 前缀只是命名习惯。真正的约束是：

1. 它必须继承 `GenericInstruction`（或目标的派生类），这样 TableGen 才会把它当作 GI 操作码；
2. 它必须出现在 Legalizer 能产生的 opcode 集合里，否则匹配表有规则也永远不会命中；
3. 它要么有导入 pattern，要么在 C++ `select()` 里有 case，否则 InstructionSelect 只能 fallback。

第 3 条是新手最常漏的：定义了一个漂亮的 `G_FOO`，写了 `GINodeEquiv`，但没有任何 pass 产生 `G_FOO`，测试自然永远是空的。

---

## 12.2 `AArch64GenericInstruction` 与真实定义

`AArch64InstrGISel.td` 开头：

```tablegen
class AArch64GenericInstruction : GenericInstruction {
  let Namespace = "AArch64";
}
```

`Namespace` 决定生成的 C++ 枚举名是 `AArch64::G_REV32`，而不是全局的 `G_REV32`。这也是为什么 C++ 里写 `case AArch64::G_ADD_LOW:`，而通用操作码写 `TargetOpcode::G_SELECT`。

### 12.2.1 `G_ADD_LOW`

```tablegen
// A pseudo to represent a relocatable add instruction as part of address
// computation.
def G_ADD_LOW : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src, type2:$imm);
  let hasSideEffects = 0;
}
```

三个字段值得注意：

- `type0` / `type1` 是 generic 类型占位符，不是具体寄存器类；
- 第二个输入是 `$imm`，实际放的是 `GlobalAddress` 加 target flags；
- `hasSideEffects = 0`，它只是地址计算，不是内存操作。

它没有 `GISelMatchGenericTypes`。这不是遗漏：`G_ADD_LOW` 由 C++ selector 直接处理，不靠导入 pattern 匹配多种向量类型。

### 12.2.2 `G_REV32` 与 `G_REV64`

```tablegen
// Pseudo for a rev32 instruction. Produced post-legalization from
// G_SHUFFLE_VECTORs with appropriate masks.
def G_REV32 : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type0:$src);
  let hasSideEffects = 0;
  let GISelMatchGenericTypes = 1;
}
```

`G_REV64` 结构相同。`GISelMatchGenericTypes = 1` 让匹配表按 generic 类型族处理这条伪指令，因此同一个 `G_REV32` 可以覆盖 `<4 x i16>`、`<8 x i8>`、`<8 x i16>`、`<16 x i8>` 等多种向量形状；具体的合法组合由导入的 pattern 与类型检查决定。

如果没有这个字段，TableGen 会尝试把操作数绑定到某个具体寄存器类，而 Legalizer 阶段还没有做寄存器类选择，匹配表就无法表达"先按 generic 类型匹配、渲染时再约束"的流程。

### 12.2.3 为什么不用真实指令

真实指令 `REV32v4i16` 的寄存器类是 `V64`，`REV32v8i16` 是 `V128`。Legalizer 产出 `G_REV32` 时只知道 LLT 形状，还不知道最终落在哪个寄存器类；更重要的是，`G_REV32` 需要作为一个**单一操作码**被 pattern 匹配，而不是在 Legalizer 里就决定具体指令。

把"语义"与"编码"分开，是 GI pseudo 的核心价值。

---

## 12.3 `GINodeEquiv` 的字段与语义

`SelectionDAGCompat.td`（23.1.4）：

```tablegen
class GINodeEquiv<Instruction i, SDNode node> {
  Instruction I = i;
  SDNode Node = node;

  // SelectionDAG has separate nodes for atomic and non-atomic memory operations
  // (ISD::LOAD, ISD::ATOMIC_LOAD, ISD::STORE, ISD::ATOMIC_STORE) but GlobalISel
  // stores this information in the MachineMemoryOperand.
  bit CheckMMOIsNonAtomic = false;
  bit CheckMMOIsAtomic = false;

  // SelectionDAG has one node for all loads and uses predicates to
  // differentiate them. GlobalISel on the other hand uses separate opcodes.
  Instruction IfSignExtend = ?;
  Instruction IfZeroExtend = ?;
  Instruction IfFPExtend = ?;
  Instruction IfFPTrunc = ?;

  // SelectionDAG has one setcc for all compares. This differentiates
  // for G_ICMP and G_FCMP.
  Instruction IfFloatingPoint = ?;

  // SelectionDAG does not differentiate between convergent and non-convergent
  // intrinsics. This specifies an alternate opcode for a convergent intrinsic.
  Instruction IfConvergent = ?;
}
```

逐字段：

| 字段 | 作用 |
| --- | --- |
| `I` | 目标端的 generic 指令（如 `G_LOAD`） |
| `Node` | 被复用的 SelectionDAG 节点（如 `ld`） |
| `CheckMMOIsNonAtomic` | 只匹配非原子 MMO |
| `CheckMMOIsAtomic` | 只匹配原子 MMO |
| `IfSignExtend` | 带符号扩展的 SDNode 改用哪个 G_* |
| `IfZeroExtend` | 零扩展改用哪个 G_* |
| `IfFPExtend` | 浮点扩展加载改用哪个 G_* |
| `IfFPTrunc` | 浮点截断存储改用哪个 G_* |
| `IfFloatingPoint` | `setcc` 是浮点比较时改用哪个 G_* |
| `IfConvergent` | convergent intrinsic 改用哪个 G_* |

### 12.3.1 它是导入期信息，不是运行时映射

最容易误解的一点：`GINodeEquiv` **不参与运行时选择**。它只在 `llvm-tblgen -gen-global-isel` 时告诉导入器："这个 SDNode 可以改写成这个 G_*"。

运行时匹配表里看到的是 `GIM_CheckOpcode ... G_LOAD`、`GIM_CheckMMOIsNonAtomic` 这类具体检查，而不是 `GINodeEquiv` 对象。把 `GINodeEquiv` 当成"GI 版的 opcode 映射表"会在调试时找错地方。

### 12.3.2 一个 SDNode 可以映射到多个 G_*

同一个 `G_LOAD` 既对应 `ld`，也对应 `atomic_load`；区别是两条 `GINodeEquiv` 的 `CheckMMOIs*` 不同。这种"一对多"是设计的一部分，不是冲突。

同理，一个 `G_STORE` 对应 `st` 与 `atomic_store`；`G_INTRINSIC` 对应 `intrinsic_wo_chain`，convergent 版本通过 `IfConvergent` 切到 `G_INTRINSIC_CONVERGENT`。

---

## 12.4 真实映射：`G_LOAD`、`G_STORE`、`G_ICMP`

### 12.4.1 非原子 load

```tablegen
def : GINodeEquiv<G_LOAD, ld> {
  let CheckMMOIsNonAtomic = true;
  let IfSignExtend = G_SEXTLOAD;
  let IfZeroExtend = G_ZEXTLOAD;
  let IfFPExtend = G_FPEXTLOAD;
}
```

SelectionDAG 的 `ld` 是一个节点，靠 `ISD::LoadExtType` 区分 `ZEXTLOAD` / `SEXTLOAD` / `EXTLOAD`。GlobalISel 用不同 opcode 表达这些语义，于是 `IfSignExtend` 与 `IfZeroExtend` 负责在导入时把 pattern 里的根操作码替换成对应 G_*。

`CheckMMOIsNonAtomic = true` 则保证这条规则只匹配非原子内存操作。没有它，一个普通 `ld` pattern 可能被错误地用于原子 load。

### 12.4.2 非原子 store

```tablegen
def : GINodeEquiv<G_STORE, st> {
  let CheckMMOIsNonAtomic = true;
  let IfFPTrunc = G_FPTRUNCSTORE;
}
```

注释里特别说明：SelectionDAG 的 `isTruncStore` 谓词在 GlobalISel 里被拆成 `G_STORE (G_TRUNCATE x)` 或专门的 `G_FPTRUNCSTORE` 形式。因此 `G_STORE` 不是 `st` 的完美 1:1 映射，`IfFPTrunc` 只负责浮点截断这一支。

### 12.4.3 原子操作

```tablegen
def : GINodeEquiv<G_STORE, atomic_store> {
  let CheckMMOIsNonAtomic = false;
  let CheckMMOIsAtomic = true;
}

def : GINodeEquiv<G_LOAD, atomic_load> {
  let CheckMMOIsNonAtomic = false;
  let CheckMMOIsAtomic = true;
  let IfSignExtend = G_SEXTLOAD;
  let IfZeroExtend = G_ZEXTLOAD;
}
```

原子 load 没有 `IfFPExtend`，因为 23.1.4 里没有对应的原子浮点扩展加载形式。不要为了"对称"补一个不存在的 opcode；`GINodeEquiv` 的字段必须对应真实存在的 G_*。

### 12.4.4 `setcc` 分叉成 `G_ICMP` / `G_FCMP`

```tablegen
def : GINodeEquiv<G_ICMP, setcc> {
  let IfFloatingPoint = G_FCMP;
}
```

SelectionDAG 只有一个 `setcc`，比较类型由操作数类型决定；GlobalISel 把整数与浮点比较拆成两个 opcode。`IfFloatingPoint` 让导入器在看到浮点 `setcc` 时把根操作码换成 `G_FCMP`。

这也解释了为什么 `G_ICMP` 的 pattern 与 `G_FCMP` 的 pattern 可以共享同一份 SDAG 描述，却在 GI 匹配表里形成两条不同规则。

### 12.4.5 intrinsic 与 convergent

```tablegen
def : GINodeEquiv<G_INTRINSIC, intrinsic_wo_chain> {
  let IfConvergent = G_INTRINSIC_CONVERGENT;
}
```

SelectionDAG 不区分 convergent 与普通 intrinsic；GlobalISel 用 `G_INTRINSIC_CONVERGENT` 表达收敛语义。`IfConvergent` 是 23.1.4 里真实存在的字段，不要漏写，也不要把它和 `IfFloatingPoint` 混用。

### 12.4.6 这些映射解决了什么

它们让 AArch64 已有的 SelectionDAG pattern 直接参与 GI 选择：

- `ld` / `st` 的 addressing-mode pattern 可以复用；
- `setcc` 的 compare pattern 可以复用；
- 原子 load/store 不会与普通 load/store 混淆；
- 符号扩展、零扩展、浮点扩展/截断各自落到正确 opcode。

但映射只解决"根节点"。pattern 里的 `ComplexPattern`、`PatLeaf`、`SDNodeXForm` 仍需各自的 GI 等价物，这正是第 11 章的内容。

---

## 12.5 案例一：`G_REV32` 的完整生命周期

### 12.5.1 产生：PostLegalizerLowering 识别 shuffle mask

`AArch64PostLegalizerLowering.cpp` 的 `matchREV`：

```cpp
if (MI.getOpcode() != TargetOpcode::G_SHUFFLE_VECTOR)
  return false;
ArrayRef<int> Mask = MI.getOperand(3).getShuffleMask();
LLT Ty = MRI.getType(MI.getOperand(0).getReg());
if (Ty.getScalarSizeInBits() == 64)
  return false;

for (unsigned LaneSize : {64U, 32U, 16U}) {
  if (isREVMask(Mask, EltSize, NumElts, LaneSize)) {
    unsigned Opc = LaneSize == 64 ? AArch64::G_REV64
                 : LaneSize == 32 ? AArch64::G_REV32
                                  : AArch64::G_BSWAP;
    MatchInfo = ShuffleVectorPseudo(Opc, Dst, {Src});
    return true;
  }
}
return false;
```

关键点：

- 输入必须是 `G_SHUFFLE_VECTOR`；
- 元素宽度 64 位不处理（AArch64 没有对应的单元素反转）；
- lane 宽度 64 → `G_REV64`，32 → `G_REV32`，16 → `G_BSWAP`；
- 匹配成功后只产生一个伪指令，不直接产生真实 `REV`。

这里 `G_REV32` 是 **post-legalization** 产生的：Legalizer 已经把向量类型合法化，Lowering 只负责把 shuffle 的 mask 识别成反转语义。

### 12.5.2 选中：`GINodeEquiv` + 导入 pattern

`AArch64InstrGISel.td`：

```tablegen
def : GINodeEquiv<G_REV32, AArch64rev32>;
def : GINodeEquiv<G_REV64, AArch64rev64>;
```

`AArch64InstrInfo.td` 的 `REV32` multiclass 生成 `(set Vd, (AArch64rev32 Vn))` 形式的 pattern。例如：

```tablegen
def : Pat<(v4f16 (AArch64rev32 V64:$Rn)), (REV32v4i16 V64:$Rn)>;
def : Pat<(v8f16 (AArch64rev32 V128:$Rn)), (REV32v8i16 V128:$Rn)>;
```

`GINodeEquiv` 把 `AArch64rev32` 换成 `G_REV32`，导入器再把这些 pattern 变成 `G_REV32 → REV32v4i16` / `REV32v8i16` 等规则。`G_REV32` 本身没有 C++ `case`，它完全靠 TableGen 选中。

### 12.5.3 可跑的 MIR：`select-rev.mir`

RUN：

```bash
llc -mtriple aarch64 -run-pass=instruction-select -verify-machineinstrs %s -o - | FileCheck %s
```

输入：

```mir
%copy:fpr(<4 x i16>) = COPY $d0
%rev:fpr(<4 x i16>) = G_REV32 %copy
$d0 = COPY %rev(<4 x i16>)
```

上游 `CHECK` 的期望输出：

```mir
%copy:fpr64 = COPY $d0
%rev:fpr64 = REV32v4i16 %copy
$d0 = COPY %rev
```

同一文件还覆盖 `<8 x i8>` → `REV32v8i8`、`<8 x i16>` → `REV32v8i16`、`<16 x i8>` → `REV32v16i8`。这些不同形状共享同一个 `G_REV32`，正是 `GISelMatchGenericTypes = 1` 与导入 pattern 的类型分派共同完成的。

### 12.5.4 为什么不在 Legalizer 里直接产生 `REV32v4i16`

因为 Legalizer 不应该知道最终寄存器类与编码。`<4 x i16>` 可能落在 `V64`，`<8 x i16>` 落在 `V128`；同一个语义在不同形状下对应不同真实指令。用一个 generic `G_REV32` 承接，再让 InstructionSelect 按类型选具体指令，职责更清晰，测试也更容易分层。

> 说明：本章 MIR 输出来自 23.1.4 上游测试的 `CHECK` 行；本机未构建 `llc`，未在本地执行。

---

## 12.6 案例二：`G_ADD_LOW` 与 `MOVaddr`

### 12.6.1 Legalizer 产生 `ADRP + G_ADD_LOW`

`AArch64LegalizerInfo::legalizeSmallCMGlobalValue`：

```cpp
auto ADRP = MIRBuilder.buildInstr(AArch64::ADRP, {LLT::pointer(0, 64)}, {})
                .addGlobalAddress(GV, Offset, OpFlags | AArch64II::MO_PAGE);
MRI.setRegClass(ADRP.getReg(0), &AArch64::GPR64RegClass);

MIRBuilder.buildInstr(AArch64::G_ADD_LOW, {DstReg}, {ADRP})
    .addGlobalAddress(GV, Offset,
                      OpFlags | AArch64II::MO_PAGEOFF | AArch64II::MO_NC);
MI.eraseFromParent();
```

注释说明了动机：把 `G_GLOBAL_VALUE` 拆开，才能在 small code model 下把 `G_ADD_LOW` 折进 load/store 的 offset。TLS 与 GOT 全局变量会提前返回，不进入这条路径。

### 12.6.2 选择：C++ `case AArch64::G_ADD_LOW`

`AArch64InstructionSelector::select` 的 fallback switch 中：

```cpp
case AArch64::G_ADD_LOW: {
  MachineInstr *BaseMI = MRI.getVRegDef(I.getOperand(1).getReg());
  if (BaseMI->getOpcode() != AArch64::ADRP) {
    I.setDesc(TII.get(AArch64::ADDXri));
    I.addOperand(MachineOperand::CreateImm(0));
    constrainSelectedInstRegOperands(I, TII, TRI, RBI);
    return true;
  }
  auto Op1 = BaseMI->getOperand(1);
  auto Op2 = I.getOperand(2);
  auto MovAddr = MIB.buildInstr(AArch64::MOVaddr, {I.getOperand(0)}, {})
      .addGlobalAddress(Op1.getGlobal(), Op1.getOffset(), Op1.getTargetFlags())
      .addGlobalAddress(Op2.getGlobal(), Op2.getOffset(), Op2.getTargetFlags());
  I.eraseFromParent();
  constrainSelectedInstRegOperands(*MovAddr, TII, TRI, RBI);
  return true;
}
```

两种形态：

- 前驱是 `ADRP` → 合成 `MOVaddr`，把两个 global address 操作数都带上；
- 前驱不是 `ADRP` → 直接改成 `ADDXri`，补一个 `0` 移位立即数。

`G_ADD_LOW` 没有 `GINodeEquiv`，因为 `MOVaddr` 不是从某个 SDNode pattern 导入的；它由 C++ 在 InstructionSelect 里手工合成。

### 12.6.3 可跑的 MIR：`select-add-low.mir`

RUN：

```bash
llc -mtriple=aarch64 -run-pass=instruction-select -verify-machineinstrs %s -o - | FileCheck %s
```

有 ADRP、无 offset：

```mir
%adrp:gpr64(p0) = ADRP target-flags(aarch64-page) @x
%add_low:gpr(p0) = G_ADD_LOW %adrp(p0), target-flags(aarch64-pageoff, aarch64-nc) @x
```

期望：

```mir
%add_low:gpr64common = MOVaddr target-flags(aarch64-page) @x, target-flags(aarch64-pageoff, aarch64-nc) @x
```


前驱不是 ADRP：

```mir
%ptr:gpr(p0) = COPY $x0
%add_low:gpr(p0) = G_ADD_LOW %ptr(p0), target-flags(aarch64-pageoff, aarch64-nc) @x
```

期望：

```mir
%ptr:gpr64sp = COPY $x0
%add_low:gpr64sp = ADDXri %ptr, target-flags(aarch64-pageoff, aarch64-nc) @x, 0
```

`ADDXri` 的最后一个 `0` 是 shift 立即数，不是偏移；偏移已经在 global address 操作数里。

### 12.6.4 后续：ExpandPseudo 展开 `MOVaddr`

`AArch64ExpandPseudoInsts.cpp` 的 `case AArch64::MOVaddr` 调用 `AArch64_IMM::expandMOVAddr`，按目标平台生成 `ADRP` + `ADDXri`（某些形态还会生成 `LDRXui` / `MOVKXi`）。因此 `MOVaddr` 是一个 **post-ISel pseudo**：InstructionSelect 之后仍然存在，等 expand pass 才消失。

这也解释了为什么 `select-add-low.mir` 只跑 `-run-pass=instruction-select`：它验证的是 `MOVaddr` 是否正确合成，而不是最终汇编。

### 12.6.5 生命周期对照

```text
G_GLOBAL_VALUE
  --(Legalizer)--> ADRP + G_ADD_LOW
  --(InstructionSelect, C++)--> MOVaddr
  --(AArch64ExpandPseudo)--> ADRP + ADDXri
```

测试应分层：

1. legalizer 测试检查 `G_GLOBAL_VALUE` 是否变成 `ADRP + G_ADD_LOW`；
2. instruction-select 测试检查 `G_ADD_LOW` 是否变成 `MOVaddr` / `ADDXri`；
3. expand-pseudo 测试检查 `MOVaddr` 是否展开成正确的重定位组合。

把三层混在一个测试里，失败时很难判断是 legalize、select 还是 expand 的问题。

---

## 12.7 pseudo 设计检查表

新增一个 target generic opcode 或 pseudo 时，逐项确认：

1. **谁产生它？** Legalizer、PostLegalizerLowering、C++ selector，还是某个 combine？
2. **谁选中它？** 导入 pattern、C++ `switch`，还是它本身就是 post-ISel pseudo？
3. **类型怎么表达？** 需要 `GISelMatchGenericTypes` 吗？还是固定 LLT / 寄存器类？
4. **有没有副作用？** 内存操作必须正确设置 MMO；纯计算设 `hasSideEffects = 0`。
5. **失败路径是什么？** 没有匹配时是 fallback 到 SelectionDAG，还是 `select()` 返回 false？
6. **需要 expand 吗？** 需要的话，expand pass 的测试与 select 测试分开写。
7. **名字空间对吗？** `AArch64::G_FOO` 与 `TargetOpcode::G_FOO` 不能混用。

### 12.7.1 调试方法

```bash
llc -mtriple=aarch64 -run-pass=instruction-select -global-isel-abort=1 \
    -verify-machineinstrs test.mir -o -
```

- `-global-isel-abort=1` 把 fallback 变成硬失败；
- `-verify-machineinstrs` 抓寄存器类与操作数约束错误；
- assertions 构建下加 `-debug-only=instruction-select` 看匹配表轨迹。

如果 `G_REV32` 没有被选中，先确认它真的出现在输入 MIR 里，再确认 `GINodeEquiv` 对应的 SDAG 节点名没有写错，最后才怀疑 pattern。

### 12.7.2 RISC-V 对照

RISC-V 也有自己的 target generic opcode / pseudo 命名空间，但名字与覆盖范围不能照抄 AArch64。迁移时先看目标后端的 `*InstrGISel.td`（或等价文件）与 Legalizer 的 custom legalization，确认：

- 目标是否已有 `G_*` 伪指令；
- 它由哪个 pass 产生；
- 它是靠 TableGen 还是 C++ 选中；
- 是否需要在 expand pass 里展开。

没有这些信息，直接复制 `G_REV32` / `G_ADD_LOW` 的定义只会得到一个永远不会被产生的操作码。

---

## 踩雷清单

1. **定义了 `G_*` 却没人产生**。匹配表有规则，MIR 里永远不出现，测试看起来"没生效"。
2. **把 `GINodeEquiv` 当运行时映射**。它只影响 TableGen 导入；运行时是 `GIM_CheckOpcode`。
3. **漏写 `CheckMMOIsNonAtomic` / `CheckMMOIsAtomic`**。原子与非原子 load/store 会互相污染。
4. **把 `G_ADD_LOW` 当成 ComplexPattern**。它没有 `GINodeEquiv`，由 C++ selector 处理。
5. **忘记 post-ISel pseudo 还需要 expand**。`MOVaddr` 不是最终指令，测试要跑到 expand pass 才能看汇编。

---

## 进阶

- 给一个自定义 `G_FOO` 写最小生命周期：Legalizer 产生、pattern 选中、MIR 测试覆盖三种类型。
- 读 `GlobalISelEmitter.cpp` 里处理 `IfSignExtend` / `IfZeroExtend` 的代码，确认导入器如何选择根操作码。
- 对比 `G_REV32` 与 `G_BSWAP` 的 mask 条件，解释为什么 lane 16 走 `G_BSWAP` 而不是 `G_REV32`。

---

## 动手练习

### 练习 12.1 追 `G_REV32`

从 `G_SHUFFLE_VECTOR` 的 mask 开始，写出 `G_REV32` 的产生、`GINodeEquiv`、导入 pattern、最终 `REV32v4i16` 的完整链条。标出每一步发生在哪个 pass。

### 练习 12.2 设计一个 `G_FOO`

假设目标有一条 `FOO` 指令，语义是"对向量做两两交换"。写出：`AArch64GenericInstruction` 定义、Legalizer/Lowering 产生点、`GINodeEquiv`（如果需要）、MIR 测试的最小骨架。

### 练习 12.3 读字段

对 `G_LOAD` / `G_STORE` / `G_ICMP` / `G_INTRINSIC` 四条映射，列出每个 `If*` 字段的实际值，并说明哪一个字段对应原子性、哪一个对应浮点比较。

### 练习 12.4 分层测试

为 `G_ADD_LOW` 写三个测试的 RUN 行：legalizer、instruction-select、expand-pseudo。说明每个测试应该 CHECK 什么，不该 CHECK 什么。

---

## 重点整理
- target-specific generic opcode 让 Legalizer 能产生目标语义，同时保持 InstructionSelect 的可匹配性。
- `G_REV32` 由 PostLegalizerLowering 产生，靠 `GINodeEquiv` + 导入 pattern 选中，不需要 C++ case。
- `G_ADD_LOW` 由 Legalizer 产生，靠 C++ `select()` 变成 `MOVaddr` / `ADDXri`，再由 ExpandPseudo 展开。
- `GINodeEquiv` 的 `CheckMMOIs*` 区分原子性，`If*` 字段区分扩展、截断、浮点比较与 convergent。
- `GINodeEquiv` 是导入期信息，不是运行时映射。
- post-ISel pseudo（`MOVaddr`）与 generic opcode（`G_REV32`）的生命周期不同，测试要分层。

---

## 自我检核
1. 为什么不能把 `G_REV32` 直接定义成 `REV32v4i16`？
2. `GINodeEquiv` 的 `IfFloatingPoint` 解决什么分叉？
3. `CheckMMOIsNonAtomic` 与 `CheckMMOIsAtomic` 同时为 false 时表示什么？
4. `G_ADD_LOW` 为什么没有 `GINodeEquiv`？
5. `MOVaddr` 在哪个 pass 被展开？展开成什么？
6. `GISelMatchGenericTypes = 1` 对 `G_REV32` 的多形状匹配有什么作用？

---

## 延伸阅读
- `llvm/include/llvm/Target/GlobalISel/SelectionDAGCompat.td`：`GINodeEquiv` 定义与全部真实映射。
- `llvm/lib/Target/AArch64/AArch64InstrGISel.td`：`AArch64GenericInstruction`、`G_ADD_LOW`、`G_REV32/64` 与节点映射。
- `llvm/lib/Target/AArch64/AArch64PostLegalizerLowering.cpp`：`matchREV` 的 mask 识别。
- `llvm/lib/Target/AArch64/AArch64LegalizerInfo.cpp`：`legalizeSmallCMGlobalValue`。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`：`case AArch64::G_ADD_LOW`。
- `llvm/lib/Target/AArch64/AArch64ExpandPseudoInsts.cpp`：`MOVaddr` 的展开。

---

## 下一章

第 13 章进入手写选择器：**C++ selector**。我们会按 23.1.4 的真实顺序拆 `select()`：strict-align、`preISelLower`、`earlySelect`、`selectImpl`、switch fallback，并逐行看 `G_CONSTANT`、`G_SELECT`、`G_BRCOND` 如何被选中。继续阅读：[`./13-cpp-selector.md`](./13-cpp-selector.md)。
