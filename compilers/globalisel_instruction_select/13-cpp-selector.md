# 第 13 章 C++ 手写 selector：`select()` 的真实执行顺序

> **本章目标**：按 LLVM 23.1.4 源码逐段拆开 `AArch64InstructionSelector::select()`，搞清 `preISelLower`、`earlySelect`、`selectImpl` 与 C++ `switch` 的先后关系，并能安全地新增一个 case。
>
> **前置**：第 5–7、10–12 章。**环境**：LLVM 23.1.4，AArch64 主线，RISC-V 对照。

---

## 13.1 完整骨架

`AArch64InstructionSelector.cpp`（约 2562 行）：

```cpp
bool AArch64InstructionSelector::select(MachineInstr &I) {
  assert(I.getParent() && "Instruction should be in a basic block!");
  assert(I.getParent()->getParent() && "Instruction should be in a function!");

  MachineBasicBlock &MBB = *I.getParent();
  MachineFunction &MF = *MBB.getParent();
  MachineRegisterInfo &MRI = MF.getRegInfo();

  const AArch64Subtarget *Subtarget = &MF.getSubtarget<AArch64Subtarget>();
  if (Subtarget->requiresStrictAlign()) {
    LLVM_DEBUG(dbgs() << "AArch64 GISel does not support strict-align yet\n");
    return false;
  }

  MIB.setInstrAndDebugLoc(I);
  unsigned Opcode = I.getOpcode();

  if (!I.isPreISelOpcode() || Opcode == TargetOpcode::G_PHI) {
    // 非 generic 指令、PHI/G_PHI、COPY、debug 指令的特殊处理
    ...
    return true;
  }

  if (I.getNumOperands() != I.getNumExplicitOperands()) {
    LLVM_DEBUG(dbgs() << "Generic instruction has unexpected implicit operands\n");
    return false;
  }

  if (preISelLower(I))
    Opcode = I.getOpcode();

  if (earlySelect(I))
    return true;

  if (selectImpl(I, *CoverageInfo))
    return true;

  LLT Ty = I.getOperand(0).isReg() ? MRI.getType(I.getOperand(0).getReg()) : LLT{};

  switch (Opcode) {
  ...
  }
  return false;
}
```

把这段读成五层漏斗：

1. **全局门槛**：strict-align、非 generic、隐式操作数；
2. **选择前改写**：`preISelLower`；
3. **抢在 TableGen 前面**：`earlySelect`；
4. **导入 pattern**：`selectImpl`；
5. **C++ 兜底**：`switch`。

每一层都有明确的"返回 true 就结束"语义。新增代码前先确定逻辑属于哪一层，否则会把本可在 TableGen 里维护的规则写进 C++，或反过来把必须手写的上下文逻辑塞进 pattern。

### 13.1.1 为什么 `Opcode` 要单独缓存

`preISelLower` 可能改操作码，例如把 `G_SITOFP` 改成 `G_SITOF`。如果后面还用进入函数时的 `Opcode`，`selectImpl` 失败后的 `switch` 会查错分支。源码在 `preISelLower` 返回 true 后重新读取 `I.getOpcode()`，就是为这个。

这也是新增 case 时最容易忽略的细节：如果你在 `select()` 前半段改了 opcode，必须同步刷新局部变量。

### 13.1.2 `MIB.setInstrAndDebugLoc(I)`

`MIB` 是类成员 `MachineIRBuilder`。`setInstrAndDebugLoc(I)` 同时设置插入点与 debug location，后续 `MIB.buildInstr` 创建的指令会继承当前指令的位置与调试信息。不要在每个 case 里手动 `BuildMI(..., I.getDebugLoc(), ...)`，除非你确实要插到别的位置。

---

## 13.2 strict-align 与 pre-ISel 特判

### 13.2.1 strict-align 直接拒绝

```cpp
if (Subtarget->requiresStrictAlign()) {
  LLVM_DEBUG(dbgs() << "AArch64 GISel does not support strict-align yet\n");
  return false;
}
```

`return false` 表示这条指令没被选中，由上层决定 fallback。23.1.4 的 AArch64 GlobalISel 不支持 strict-align；如果你的测试目标带 `+strict-align`，不要期待 GI 路径能工作。

### 13.2.2 非 generic 指令：只做少量特判

```cpp
if (!I.isPreISelOpcode() || Opcode == TargetOpcode::G_PHI) {
  if (Opcode == TargetOpcode::LOAD_STACK_GUARD) {
    constrainSelectedInstRegOperands(I, TII, TRI, RBI);
    return true;
  }

  if (Opcode == TargetOpcode::PHI || Opcode == TargetOpcode::G_PHI) {
    const Register DefReg = I.getOperand(0).getReg();
    const LLT DefTy = MRI.getType(DefReg);
    ... // 选寄存器类
    I.setDesc(TII.get(TargetOpcode::PHI));
    return RBI.constrainGenericRegister(DefReg, *DefRC, MRI);
  }

  if (I.isCopy())
    return selectCopy(I, TII, MRI, TRI, RBI);

  if (I.isDebugInstr())
    return selectDebugInstr(I, MRI, RBI);

  return true;
}
```

四个分支：

| 指令 | 处理 |
| --- | --- |
| `LOAD_STACK_GUARD` | 直接约束操作数，已可发射 |
| `PHI` / `G_PHI` | 按类型与 bank 选寄存器类，改成 `PHI` |
| `COPY` | `selectCopy` 处理跨 bank / 子寄存器 |
| DBG_VALUE 等 debug 指令 | `selectDebugInstr` |
| 其他已经是目标指令的 MI | 直接返回 true |

注意 `G_PHI` 虽然是 generic，但被提到这个分支里与 `PHI` 一起处理。它不会进入 `selectImpl`，也不会进入 fallback switch。


---

## 13.3 `preISelLower`：选择前的纯改写

`preISelLower` 只做"选择前必须完成的 MIR 变换"，不负责最终选指令。它返回 true 表示指令被修改，`select()` 要继续选。

### 13.3.1 指针常量改成整数

```cpp
if (DefTy.isPointer() && (PtrSize == 32 || PtrSize == 64)) {
  // Convert pointer typed constants to integers so TableGen can select.
  MRI.setType(DefReg, LLT::integer(PtrSize));
  return true;
}
```

导入 pattern 的类型检查按整数 LLT 生成，指针类型会让 `G_CONSTANT` 匹配不上。改成整数后，选择继续；语义上它仍然是一个地址常量。

### 13.3.2 store 的跨 bank copy 与指针

```cpp
case TargetOpcode::G_STORE: {
  bool Changed = contractCrossBankCopyIntoStore(I, MRI);
  MachineOperand &SrcOp = I.getOperand(0);
  if (MRI.getType(SrcOp.getReg()).isPointer()) {
    auto Copy = MIB.buildCopy(LLT::scalar(64), SrcOp);
    Register NewSrc = Copy.getReg(0);
    SrcOp.setReg(NewSrc);
    RBI.constrainGenericRegister(NewSrc, AArch64::GPR64RegClass, MRI);
    Changed = true;
  }
  return Changed;
}
```

`contractCrossBankCopyIntoStore` 把 `G_STORE (COPY gpr<-fpr)` 折成直接 store gpr 源，省一次跨 bank copy。指针 store 则先插一个 `s64` copy，让导入 pattern 能匹配。

### 13.3.3 `G_PTR_ADD`、`G_LOAD` 与向量

- `G_PTR_ADD`：除非启用 Checked Pointer Arithmetic，否则 `convertPtrAddToAdd` 把指针加法改成整数 `G_ADD`，并可能把 `0 - x` 识别成 `G_SUB`；
- `G_LOAD`：指针结果类型从 `p0` 改成 `s64`，让导入 pattern 匹配；
- `G_DUP` / `G_INSERT_VECTOR_ELT`：指针向量元素改成 `s64`，或把 s8/s16 元素扩成 s32；
- `G_SITOFP` / `G_UITOFP`：源与目标都在 FPR bank 时改成 `G_SITOF` / `G_UITOF`，避免生成跨 bank copy。

这些改写的共同点是：**只改类型或操作码，不改变指令数量与语义**。需要新增指令时，它们会显式 `MIB.buildCopy` / `MIB.buildPtrToInt`，并立刻设置寄存器类或 bank。

### 13.3.4 `preISelLower` 与 `earlySelect` 的边界

`preISelLower` 的结果仍然要经过 `earlySelect` 与 `selectImpl`；它不是最终选择。`earlySelect` 则可以直接把指令选中并返回 true。判断标准：

- 只是"让后续匹配更容易"的改写 → `preISelLower`；
- 已经能确定目标指令、且要抢在导入 pattern 前面 → `earlySelect`；
- 需要完整上下文、且导入 pattern 表达不了 → fallback `switch`。

---

## 13.4 `earlySelect`：抢在导入 pattern 前面

`earlySelect` 的注释说明了动机：有些 pattern 能被导入器选中，但结果不最优，等到 C++ fallback 时已经没机会改。于是给这些情况一个"提前选择"的入口。

23.1.4 的 `earlySelect` 只覆盖少量 opcode：

```cpp
switch (I.getOpcode()) {
case AArch64::G_DUP: ...
case TargetOpcode::G_SEXT: ...
case TargetOpcode::G_BR: ...
case TargetOpcode::G_SHL: ...
case TargetOpcode::G_CONSTANT: ...
case TargetOpcode::G_ADD: ...
case TargetOpcode::G_OR: ...
case TargetOpcode::G_FENCE: ...
default:
  return false;
}
```

### 13.4.1 零常量：`G_CONSTANT` 的提前分支

```cpp
case TargetOpcode::G_CONSTANT: {
  bool IsZero = false;
  if (I.getOperand(1).isCImm())
    IsZero = I.getOperand(1).getCImm()->isZero();
  else if (I.getOperand(1).isImm())
    IsZero = I.getOperand(1).getImm() == 0;

  if (!IsZero)
    return false;

  Register DefReg = I.getOperand(0).getReg();
  LLT Ty = MRI.getType(DefReg);
  if (Ty.getSizeInBits() == 64) {
    I.getOperand(1).ChangeToRegister(AArch64::XZR, false);
    RBI.constrainGenericRegister(DefReg, AArch64::GPR64RegClass, MRI);
  } else if (Ty.getSizeInBits() <= 32) {
    I.getOperand(1).ChangeToRegister(AArch64::WZR, false);
    RBI.constrainGenericRegister(DefReg, AArch64::GPR32RegClass, MRI);
  } else
    return false;

  I.setDesc(TII.get(TargetOpcode::COPY));
  return true;
}
```

三点：

- 只有零常量走这里；非零返回 false，继续走 `selectImpl`；
- 64 位用 `XZR`，≤32 位用 `WZR`；
- 直接把 `G_CONSTANT` 改成 `COPY`，不是新建一条指令。

### 13.4.2 其他 early 分支

- `G_DUP`：常量 splat 可能更适合 `MOV` 或常量池，先尝试 `emitConstantVector`；
- `G_SEXT`：`i64 sext(i32 vector_extract)` 优先选 `SMOV`；
- `G_SHL`：立即数形式走 `UBFM`，寄存器形式留给导入 pattern；
- `G_ADD`：如果一侧来自 `G_ICMP`，尝试把 compare 折进 add；
- `G_OR`：识别 compare 组合；
- `G_FENCE`：直接选内存屏障；
- `G_BR`：返回 false，明确不在这里处理。

---

## 13.5 `selectImpl` 返回 false 之后

`selectImpl` 是 TableGen 生成的入口，内部只做三件事：收集特性位集、把当前 MI 放进匹配状态、调用 `executeMatchTable`。它返回 false 表示**所有规则都没命中**，不是"选择失败"的最终结论。

接下来进入 C++ `switch (Opcode)`。23.1.4 的 fallback 覆盖大量 opcode，按职责分组：

- 分支/位域：`G_SBFX`、`G_UBFX`、`G_BRCOND`、`G_BRINDIRECT`、`G_BRJT`；
- 地址/常量：`AArch64::G_ADD_LOW`、`G_FRAME_INDEX`、`G_GLOBAL_VALUE`、`G_FCONSTANT`（`G_CONSTANT` 不在这里）；
- 内存：`G_LOAD`、`G_STORE`、`G_ZEXTLOAD`、`G_INDEXED_*`；
- 算术/转换：`G_LSHR`、`G_ASHR`、`G_SHL`、`G_OR`、`G_PTR_ADD`、`G_PTRMASK`、`G_TRUNC`、`G_ANYEXT`、`G_SEXT`；
- 选择/内建/向量：`G_SELECT`、`G_ICMP`、`G_FCMP`、`G_VASTART`、`G_INTRINSIC`、`AArch64::G_DUP`、`G_BUILD_VECTOR`、`G_SHUFFLE_VECTOR`；溢出用 `G_*ADDE/SUBE/ADDO/SUBO` 一组。

如果所有 case 都不匹配，`select()` 末尾 `return false`。上层可以把这条指令 fallback 到 SelectionDAG，或在 `-global-isel-abort=1` 下直接报错。

### 13.5.1 为什么有些 opcode 必须手写

导入 pattern 擅长"固定形状 → 固定指令"，但以下情况必须 C++：

- 需要根据 code model、subtarget 特性、调用约定做分支；
- 需要构造多条指令或伪指令（`MOVaddr`、比较 + 分支）；
- 需要读取 MMO 的原子序、对齐、地址空间；
- 需要处理 intrinsic 的语义与参数校验；
- 需要在一个 case 里同时改写操作数类型与寄存器类。

`G_CONSTANT` 是一个特例：零值在 `earlySelect`，非零交给导入 pattern，C++ switch 里没有它。把 `G_CONSTANT` 列进 fallback 表是一个常见错误。

---

## 13.6 `G_CONSTANT`：零与非零的分叉

### 13.6.1 零常量

`earlySelect` 的代码见 13.4.1；上游 `select-constant.mir`：

```mir
; 输入
%0(i32) = G_CONSTANT i32 0
$w0 = COPY %0(i32)
; CHECK
[[COPY:%[0-9]+]]:gpr32 = COPY $wzr
$w0 = COPY [[COPY]]
```


### 13.6.2 非零常量

`earlySelect` 返回 false，`selectImpl` 命中导入 pattern：

```mir
; 输入
%0(i32) = G_CONSTANT i32 42
; CHECK
[[MOVi32imm:%[0-9]+]]:gpr32 = MOVi32imm 42
```

64 位大常量：

```mir
%0(i64) = G_CONSTANT i64 1234567890123
; CHECK
[[MOVi64imm:%[0-9]+]]:gpr64 = MOVi64imm 1234567890123
```

s8/s16 常量通常先经过 `G_ANYEXT`，上游输出是 `MOVi32imm 42` 加一条 `COPY` 到 `gpr32all`。这属于合法化/类型扩展路径，不是 `G_CONSTANT` 手写 case 的行为。


> 说明：本章 MIR 输出均来自 23.1.4 上游测试的 `CHECK` 期望；本机未构建 `llc`，未在本地执行。

---

## 13.7 `G_SELECT`：CSEL / CSNEG / CSINV / CSINC

### 13.7.1 主路径

```cpp
case TargetOpcode::G_SELECT: {
  auto &Sel = cast<GSelect>(I);
  const Register CondReg = Sel.getCondReg();
  const Register TReg = Sel.getTrueReg();
  const Register FReg = Sel.getFalseReg();

  if (tryOptSelect(Sel))
    return true;

  Register DeadVReg = MRI.createVirtualRegister(&AArch64::GPR32RegClass);
  auto TstMI = MIB.buildInstr(AArch64::ANDSWri, {DeadVReg}, {CondReg})
                   .addImm(AArch64_AM::encodeLogicalImmediate(1, 32));
  constrainSelectedInstRegOperands(*TstMI, TII, TRI, RBI);
  if (!emitSelect(Sel.getReg(0), TReg, FReg, AArch64CC::NE, MIB))
    return false;
  Sel.eraseFromParent();
  return true;
}
```

三步：

1. `tryOptSelect` 尝试把条件 def 的 compare 直接折进来；
2. 折不了就发 `ANDSWri`，只保留条件的最低位并设置 `$nzcv`；
3. `emitSelect` 根据 true/false 的形状选 `CSEL` / `CSNEG` / `CSINV` / `CSINC`。

用未使用的 vreg 而不是 `wzr` 接收 `ANDSWri` 的结果，是为了让后续 peephole 有机会优化。

### 13.7.2 `emitSelect` 的多形态

`emitSelect` 先看 bank：

- FPR → `FCSELSrrr` / `FCSELDrrr`；
- GPR → 默认 `CSELWr` / `CSELXr`。

然后尝试折叠：

| 形态 | 结果 |
| --- | --- |
| `G_SUB 0, x` | `CSNEG` |
| `G_XOR x, -1` | `CSINV` |
| `G_ADD x, 1` 或 `G_PTR_ADD x, 1` | `CSINC` |

常量组合还有专门规则：

| true / false | 结果 |
| --- | --- |
| `0 / 1` | `CSINC z, z, cc` |
| `0 / -1` | `CSINV z, z, cc` |
| `1 / f` | `CSINC f, z, inv_cc` |
| `-1 / f` | `CSINV f, z, inv_cc` |
| `t / 1` | `CSINC t, z, cc` |
| `t / -1` | `CSINV t, z, cc` |

### 13.7.3 可跑的 MIR

普通 `csel`：

```mir
; 输入
%cmp:gpr(i32) = G_ICMP intpred(ne), %reg0(i32), %reg1
%select:gpr(i32) = G_SELECT %cmp, %t, %f
; CHECK（select-select.mir）
[[SUBSWrr:%[0-9]+]]:gpr32 = SUBSWrr %reg0, %reg1, implicit-def $nzcv
%select:gpr32 = CSELWr %t, %f, 1, implicit $nzcv
```

`G_SELECT cc, 0, 1`：

```mir
%select:gpr32 = CSINCWr $wzr, $wzr, 1, implicit $nzcv
```

---

## 13.8 `G_BRCOND`：compare 折叠与 `tst + bcc`

`G_BRCOND` 在 fallback switch 里直接转给 `selectCompareBranch`；后者先看条件的 def 是 `G_FCMP` 还是 `G_ICMP`，再分别折叠。

条件直接来自 compare 时，把 compare 与分支合成：

```mir
%fcmp:gpr(s32) = G_FCMP floatpred(oeq), %lhs, %rhs
G_BRCOND %fcmp, %bb.2

// 期望：
nofpexcept FCMPSrr %lhs, %rhs, implicit-def $nzcv, implicit $fpcr
Bcc 0, %bb.2, implicit $nzcv
```

`Bcc 0` 是 `EQ`。如果条件不是 compare，23.1.4 走两条路：

- `ProduceNonFlagSettingCondBr` 为真：`emitTestBit` 发 `TB(N)Z`；
- 否则：`ANDSWri ... 1` + `Bcc NE`，避免不设 flags 的条件分支破坏 Speculative Load Hardening 的假设。
`ProduceNonFlagSettingCondBr` 在 `setupMF` 里缓存为 `!hasFnAttribute(Attribute::SpeculativeLoadHardening)`。

所以同一条 MIR，在带 SLH 属性的函数里可能得到 `tst + bcc`，而不是 `tbnz`。测试时要固定函数属性，否则看到的指令形态会变。

### 13.8.1 为什么不能只写 pattern

compare 折叠需要看条件的**使用者**与 def 链，还要判断条件是否还有别的 use；`G_BRCOND` 还要读写 MBB 操作数、处理 SLH 属性。这些都不是单条 pattern 能表达的，因此必须 C++。

---

## 13.9 新增一个 case 的正确写法

### 13.9.1 先验证，再改写

```cpp
case TargetOpcode::G_FOO: {
  if (!I.getOperand(0).isReg() || !I.getOperand(1).isReg())
    return false;
  const LLT DstTy = MRI.getType(I.getOperand(0).getReg());
  if (DstTy != LLT::scalar(64))
    return false;

  Register Dst = I.getOperand(0).getReg();
  Register Src = I.getOperand(1).getReg();

  auto NewMI = MIB.buildInstr(AArch64::FOOXr, {Dst}, {Src});
  I.eraseFromParent();
  constrainSelectedInstRegOperands(*NewMI, TII, TRI, RBI);
  return true;
}
```

顺序很重要：所有可能失败的检查放在任何 MIR 修改之前。一旦 `buildInstr` 或 `eraseFromParent` 之后再 `return false`，MIR 会处于半改写状态，后续 fallback 也无法恢复。

### 13.9.2 新建还是原地改

两种合法写法：新建（`buildInstr` + `eraseFromParent`）或原地改写（`setDesc`）。新建适合操作数布局变化大、需要插入多条指令的情况；原地改写适合一一对应、操作数顺序不变的情况。原地改写后不要再 `eraseFromParent`。

### 13.9.3 debug location

`select()` 开头已经 `MIB.setInstrAndDebugLoc(I)`，`MIB.buildInstr` 会继承当前位置与 debug loc。若用 `BuildMI` 手工插入，必须显式传 `I.getDebugLoc()`，否则新指令丢调试信息。

### 13.9.4 `markUsesAsRegClass` 不存在

23.1.4 的 AArch64 selector 里没有 `markUsesAsRegClass` 这个调用。不要照抄伪代码。实际可用的 API：

| API | 用途 |
| --- | --- |
| `constrainSelectedInstRegOperands(*MI, TII, TRI, RBI)` | 选中后统一约束显式操作数，必要时插 COPY |
| `constrainOperandRegClass(...)` | 只约束某个操作数 |
| `RBI.constrainGenericRegister(Reg, RC, MRI)` | 把一个 vreg 限定到寄存器类 |
| `MIB.constrainAllUses(TII, TRI, RBI)` | builder 上的批量约束 |

`constrainSelectedInstRegOperands` 会跳过非寄存器、物理寄存器与零寄存器，并处理 tied operands。它是新 case 的标准收尾动作。

---

## 13.10 什么时候写 C++，什么时候写 pattern

| 需求 | 选择 |
| --- | --- |
| 固定 opcode + 固定类型 → 单条指令 | TableGen pattern |
| 操作数有立即数范围/对齐约束 | 先试 `ImmLeaf` / `ComplexPattern` |
| 需要 subtarget 特性分支 | C++ 或带 Predicate 的 pattern |
| 需要多条指令/伪指令 | C++ |
| 需要读 MMO、原子序、调用约定 | C++ |
| 需要修改 MIR 类型或 bank | `preISelLower` |

C++ 的优势是表达力，代价是可测试性与 review 成本：

- 每个 case 都要自己保证类型、bank、寄存器类正确；
- 失败路径必须不留下半改写状态；
- 新增 case 容易绕过 TableGen 的规则覆盖统计；
- 同一语义如果 pattern 已能表达，C++ 会造成两套维护点。

review 时重点看：失败检查是否前置、是否 `eraseFromParent`、是否 `constrainSelectedInstRegOperands`、是否处理了所有类型/bank 组合、是否更新了 `Opcode` 缓存。

---

## 13.11 可测试性与 RISC-V 对照

### 13.11.1 MIR 测试

```bash
llc -mtriple=aarch64 -run-pass=instruction-select -global-isel-abort=1 \
    -verify-machineinstrs test.mir -o - | FileCheck test.mir
```

- `-global-isel-abort=1`：把 fallback 变成失败；
- `-verify-machineinstrs`：抓寄存器类、tied operand、隐式操作数错误；
- assertions 构建加 `-debug-only=instruction-select`：看匹配表与 fallback 轨迹。

### 13.11.2 RISC-V 对照

RISC-V 的 C++ selector 有自己的 `select()` 顺序与 fallback，不能假设与 AArch64 相同。迁移 case 前先确认目标 selector 是否已处理该 opcode，再看 Legalizer 是否会产生它。

---

## 踩雷清单

1. **在 `selectImpl` 失败前就改 MIR**。后续 fallback 看到的是半改写状态。
2. **把 `G_CONSTANT` 列进 fallback switch**。零值在 `earlySelect`，非零走 `selectImpl`。
3. **忘记 `constrainSelectedInstRegOperands`**。匹配成功但 vreg 没有寄存器类，`-verify-machineinstrs` 会报错。
4. **用 `markUsesAsRegClass`**。23.1.4 没有这个 API；用 `constrainSelectedInstRegOperands` 或 `RBI.constrainGenericRegister`。
5. **新建指令后忘记 `eraseFromParent`**。原指令仍在，可能被重复选择。

---

## 进阶

- 给 `G_SELECT` 加一个常量组合，先在 MIR 里验证 `emitSelect` 的折叠顺序。
- 读 `tryOptSelect` 的多 use 检查，解释为什么条件被两个 `G_SELECT` 共享时仍可折叠。

---

## 动手练习

### 练习 13.1 零常量

分别用 i32/i64/指针类型跑 `G_CONSTANT 0`，写出输出并解释 `WZR` / `XZR` 的选择。

### 练习 13.2 G_SELECT 形态

构造 `G_SELECT cc, 0, 1`、`cc, 1, f`、`cc, t, -1` 三个 MIR，写出期望的 `CSINC` / `CSINV` 形态，并解释条件码是否被反转。

### 练习 13.3 新增 case

为一条假想的 `G_FOO` 写完整 case：前置检查、`buildInstr`、`eraseFromParent`、`constrainSelectedInstRegOperands`、返回 true。说明哪一步不能提前。

---

## 重点整理

- `select()` 的顺序是：strict-align → 非 generic/PHI/COPY/debug → `preISelLower` → `earlySelect` → `selectImpl` → C++ switch。
- `preISelLower` 只改类型/操作码，让后续匹配更容易；`earlySelect` 可以直接选中。
- `G_CONSTANT` 零值在 `earlySelect` 变成 `COPY XZR/WZR`；非零走 `selectImpl`，fallback switch 没有它。
- `G_SELECT` 先 `tryOptSelect`，再 `ANDSWri` + `emitSelect`，可生成 `CSEL/CSNEG/CSINV/CSINC` 与 `FCSEL`。
- `G_BRCOND` 优先折叠 compare；否则按 SLH 属性选 `TB(N)Z` 或 `tst+bcc`。

---

## 自我检核

1. `select()` 中 `preISelLower`、`earlySelect`、`selectImpl` 的先后顺序是什么？
2. 为什么 `G_CONSTANT` 不在 fallback switch 里？
3. `G_SELECT` 的 `ANDSWri` 作用是什么？为什么用未使用的 vreg 接收结果？
4. `CSNEG` / `CSINV` / `CSINC` 分别由哪种 true/false 形态触发？
5. `G_BRCOND` 在什么情况下发 `tst + bcc` 而不是 `TB(N)Z`？
6. 新增 case 时为什么所有失败检查必须在 `buildInstr` 之前？

---

## 延伸阅读

- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`：`select()`、`preISelLower`、`earlySelect`、`G_SELECT`、`G_BRCOND`。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp` 的 `emitSelect`、`tryOptSelect`、`selectCompareBranch`。
- `llvm/test/CodeGen/AArch64/GlobalISel/select-constant.mir`：零/非零常量的期望输出。
- `llvm/test/CodeGen/AArch64/GlobalISel/select-select.mir`：`CSEL/CSINC/CSINV` 与 FPR 形态。
- `llvm/test/CodeGen/AArch64/GlobalISel/fold-brcond-fcmp.mir`：compare 折进分支。
- `llvm/include/llvm/CodeGen/GlobalISel/InstructionSelector.h`：`constrainSelectedInstRegOperands` 等接口。

---

## 下一章

第 14 章进入多输出、内存与原子：**multi-output、load/store 的 MMO、原子操作与 `G_INDEXED_*`**。我们会看一个 `G_*` 如何产生多个结果、`selectLoadStore` 如何选择寻址模式，以及原子序如何影响指令选择。继续阅读：[`./14-multi-output-memory-atomic.md`](./14-multi-output-memory-atomic.md)。
