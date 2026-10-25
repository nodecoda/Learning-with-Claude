# Ch 6 — 默认 mapping：约束、regclass 与 fallback
> **目标**：读懂 `RegisterBankInfo::getInstrMappingImpl` 的完整决策路径：它如何从 target 编码约束和 copy-like 语义推导出一条默认 mapping；什么时候返回 invalid；target 为什么还要自己覆写 `getInstrMapping`。
>
> **环境**：LLVM 23.1.4 源码；`add.ll`、`phi.ll` 的 MIR 来自官方 23.1.4 预编译包（实测）。本章不修改源码。
>
> **证据约定**：算法与分支标为**源码依据**；MIR 标为**实测**；需要自己构建 assertions 版本才能看到的 `-debug-only` 输出不伪造。
## 为什么需要这个？
Ch 5 讲了 mapping 长什么样。现在的问题是：**这条 mapping 是谁产生的？** 对大多数 generic opcode，LLVM 并没有一张“G_ADD i32 → GPR”的万能表；`RegisterBankInfo` 的基类只能做三件事：

1. 如果指令是 target-specific 指令，读它的 MCInstrDesc 编码约束，把 regclass 翻成 bank；
2. 只有 copy-like（`COPY`、`PHI`、`REG_SEQUENCE`）才会沿用它已有的 bank，因为这些指令本身没有约束；
3. copy-like 的 mapping 只描述 def，其余操作数不参与。

这三件事就是 `getInstrMappingImpl`。它不聪明，但非常快，而且不需要 target 为每个 opcode 写表。注意 AArch64 的调用条件：只有“非 generic 且不是 COPY”的指令或 `G_PHI` 才会先试它；普通 generic opcode（包括 `G_ADD`）直接走 target 的 opcode switch。理解这个分界，才能解释为什么有些指令的 bank 来自“约束”，有些来自“启发式”。
## 先建立直觉
把 `getInstrMappingImpl` 想成一个**证件检查员**：

- 先看指令的“身份证”（MCInstrDesc / regclass 约束）：如果上面写明了“这个操作数必须是 FPR64”，就直接放行到 FPR。
- 如果身份证没写，只有当这是一张 `COPY` / `PHI` / `REG_SEQUENCE` 时，才看目的地**已经**被谁登记过：已经有人给它发了 GPR 的登机牌，就沿用。
- 普通指令没有登记就到此为止：它不会去猜“这个 G_ADD 应该放 GPR”，也不会沿用操作数现有的 bank。
- 其余情况：拒绝，返回 invalid mapping。

它不会猜“这个 G_ADD 应该放 GPR 还是 FPR”。那是 target 的 `getInstrMapping` 要做的启发式。
```text
getInstrMappingImpl(MI)
        │
        ├─ isCopyLike? ──是──► 只映射 def；从任一已有 bank / 约束推导
        │
        └─否─► 对每个 reg operand:
                1. 编码约束（regclass）? 用
                2. 没有? ──► invalid
                （已有 bank 不参与）
```
## 核心概念
### 默认 mapping 的语义
`getInstrMapping` 的注释定义了“默认”：

> This mapping should be the direct translation of MI. In other words, when MI is mapped with the returned mapping, only the register banks of the operands of MI need to be updated. In particular, neither the opcode nor the type of MI needs to be updated for this direct mapping.

也就是说，默认 mapping **不改 opcode、不改类型**，只改操作数的 bank。它的 ID 固定是 `RegisterBankInfo::DefaultMappingID`，cost 在基类实现里写死为 1；target 自己的 `getInstrMapping` 分支可以给别的值（例如 AArch64 的 COPY 用 `copyCost`，同 bank 时为 0）。target 的 alternative mapping 才可能通过 `applyMappingImpl` 改指令。
### `getInstrMappingImpl` 的完整路径
源码在 `RegisterBankInfo.cpp`，核心逻辑可以压缩成下面几步：
```cpp
static bool isCopyLike(const MachineInstr &MI) {
  return MI.isCopy() || MI.isPHI() ||
         MI.getOpcode() == TargetOpcode::REG_SEQUENCE;
}

const RegisterBankInfo::InstructionMapping &
RegisterBankInfo::getInstrMappingImpl(const MachineInstr &MI) const {
  bool IsCopyLike = isCopyLike(MI);
  unsigned NumOperandsForMapping = IsCopyLike ? 1 : MI.getNumOperands();

  SmallVector<const ValueMapping *, 8> OperandsMapping(NumOperandsForMapping);
  for (unsigned OpIdx = 0, EndIdx = MI.getNumOperands(); OpIdx != EndIdx;
       ++OpIdx) {
    const MachineOperand &MO = MI.getOperand(OpIdx);
    if (!MO.isReg() || !MO.getReg())
      continue;
    Register Reg = MO.getReg();
    TypeSize Size = getSizeInBits(Reg, MRI, TRI);

    const RegisterBank *AltRegBank = getRegBank(Reg, MRI, TRI);
    const RegisterBank *CurRegBank = IsCopyLike ? AltRegBank : nullptr;

    if (!CurRegBank) {
      CurRegBank = getRegBankFromConstraints(MI, OpIdx, TII, MRI);
      if (!CurRegBank) {
        CompleteMapping = false;
        if (!IsCopyLike)
          return getInvalidInstructionMapping();
        continue;
      }
    }

    const ValueMapping *ValMapping =
        &getValueMapping(0, Size.getKnownMinValue(), *CurRegBank);

    if (IsCopyLike) {
      if (!OperandsMapping[0]) { ... }
      // 检查所有操作数之间是否 cannotCopy
      ...
      CompleteMapping = true;
      break;
    }

    OperandsMapping[OpIdx] = ValMapping;
  }

  if (IsCopyLike && !CompleteMapping)
    return getInvalidInstructionMapping();

  return getInstructionMapping(
      DefaultMappingID, /*Cost*/ 1,
      getOperandsMapping(OperandsMapping), NumOperandsForMapping);
}
```
这段代码有几个容易看漏的细节：

| 细节 | 含义 |
| --- | --- |
| `NumOperandsForMapping` | copy-like 是 1，其他是 `MI.getNumOperands()` |
| `CurRegBank = IsCopyLike ? AltRegBank : nullptr` | 非 copy-like 直接丢弃已有 bank，不把它当备选 |
| `getRegBankFromConstraints` | 只有 target-specific 指令才可能返回非空 |
| `CompleteMapping` | copy-like 允许部分操作数没有 bank；普通指令不允许 |
| `Cost = 1` | 基类默认映射不做代价建模 |
### `getRegClassConstraint`：约束从哪里来
`getRegBankFromConstraints` 的第一句是：
```cpp
const TargetRegisterClass *RC = MI.getRegClassConstraint(OpIdx, &TII, TRI);
```
`MachineInstr::getRegClassConstraint` 的实现（`MachineInstr.cpp`）对普通指令直接查 MCInstrDesc：
```cpp
if (!isInlineAsm())
  return TII->getRegClass(getDesc(), OpIdx);
```
`TII->getRegClass` 返回该 operand 的寄存器类约束。generic opcode 的 MCInstrDesc 没有这些约束，所以返回 `nullptr`。这就是为什么“基类无法映射大多数 generic opcode”。

拿到 RC 后，`getRegBankFromConstraints` 做：
```cpp
const RegisterBank &RegBank = getRegBankFromRegClass(*RC, MRI.getType(Reg));
assert(RegBank.covers(*RC) && "The mapping of the register bank does not make sense");
return &RegBank;
```
注意 `assert`：如果 target 的 `getRegBankFromRegClass` 返回了一个不覆盖该 RC 的 bank，assertions build 会直接报错。release 包不会检查，但后面的 `PartialMapping::verify` 仍可能在别处暴露问题。
### `getRegBankFromRegClass`：RC → bank 的唯一桥
基类的 `getRegBankFromRegClass` 是纯虚语义：
```cpp
virtual const RegisterBank &
getRegBankFromRegClass(const TargetRegisterClass &RC, LLT Ty) const {
  llvm_unreachable("The target must override this method");
}
```
AArch64 的覆写很短：
```cpp
const RegisterBank &
AArch64RegisterBankInfo::getRegBankFromRegClass(const TargetRegisterClass &RC,
                                                LLT Ty) const {
  switch (RC.getID()) {
  case AArch64::GPR64sponlyRegClassID:
    return getRegBank(AArch64::GPRRegBankID);
  default:
    return AArch64GenRegisterBankInfo::getRegBankFromRegClass(RC, Ty);
  }
}
```
`GPR64sponlyRegClassID` 需要特判；其余 RC 交给 TableGen 生成的查表。Ch 7 会拆这张生成表。
### 已有 bank 的读取：`getRegBank`
`getInstrMappingImpl` 里的 `getRegBank(Reg, MRI, TRI)` 不是“查表”，而是读 `MachineRegisterInfo`：
```cpp
const RegClassOrRegBank &RegClassOrBank = MRI.getRegClassOrRegBank(Reg);
if (auto *RB = dyn_cast_if_present<const RegisterBank *>(RegClassOrBank))
  return RB;
if (auto *RC = dyn_cast_if_present<const TargetRegisterClass *>(RegClassOrBank))
  return &getRegBankFromRegClass(*RC, MRI.getType(Reg));
return nullptr;
```
虚拟寄存器要么带 bank，要么带 regclass，要么都没有。物理寄存器走另一条路：
```cpp
if (!Reg.isVirtual()) {
  const TargetRegisterClass *RC = getMinimalPhysRegClass(Reg, TRI);
  return RC ? &getRegBankFromRegClass(*RC, LLT()) : nullptr;
}
```
所以 `%0:gpr(i64) = COPY $x0` 里的 `$x0` 会通过“最小物理寄存器类”得到 GPR。注意这里传的是空 `LLT()`，不是虚拟寄存器的类型；物理寄存器的 bank 只由寄存器类决定。
### copy-like 的特殊处理
`COPY`、`PHI`、`REG_SEQUENCE` 在 `getInstrMappingImpl` 里被特殊对待：

- 只关心 `OperandsMapping[0]`（def）；
- 对每个 reg operand，先看它已有的 bank；第一个找到的 bank 作为 def 的 bank；
- `REG_SEQUENCE` 的 def 大小可能和输入不同，所以它用 `getSizeInBits(MI.getOperand(0).getReg())` 的结果大小，而不是输入大小；
- 最后检查所有操作数之间是否 `cannotCopy`；如果某个 copy 不可能，返回 invalid。

源码注释解释了原因：

> For copy like instruction, only the mapping of the definition is important. The rest is not constrained.

这意味着 `COPY` 的 mapping 不允许“给源操作数指定另一个 bank”。对 `COPY` 来说，跨 bank 正是指令本身的语义（它自己就是那条搬运）；对 `G_PHI`，incoming value 的 bank 由边上的 repair 处理，而不是在 mapping 里描述。
### invalid mapping 与 fallback
invalid mapping 由 `getInvalidInstructionMapping()` 产生：
```cpp
ID = InvalidMappingID;
Cost = 0;
OperandsMapping = nullptr;
NumOperands = 0;
```
`isValid()` 为 false。之后有两条路：

1. **target 还有自己的 `getInstrMapping`**：AArch64 在 `getInstrMappingImpl` 失败后会继续走自己的 opcode switch 或类型回退，用类型/opcode 启发式生成 mapping。
2. **target 也没有**：`RegisterBankInfo::getInstrMapping` 的基类实现会：
```cpp
const InstructionMapping &Mapping = getInstrMappingImpl(MI);
if (Mapping.isValid())
  return Mapping;
llvm_unreachable("The target must implement this");
```
也就是说，基类的 `getInstrMapping` 在 invalid 时是 `llvm_unreachable`，不是“返回 invalid 让 pass 处理”。target 必须覆写。
## 底层机制
### 决策表
把 `getInstrMappingImpl` 的结果按输入分类：

| 指令类型 | 有 regclass 约束 | 操作数已有 bank | 结果 |
| --- | --- | --- | --- |
| target-specific | 有 | 无所谓 | 从约束得到 bank |
| target-specific | 无 | 有 | invalid（已有 bank 不参与） |
| target-specific | 无 | 无 | invalid |
| generic（非 copy-like） | 无 | 全部有 | invalid（同上） |
| generic（非 copy-like） | 无 | 有缺失 | invalid |
| COPY / PHI / REG_SEQUENCE | 无 | 至少一个 | 只映射 def；检查 cannotCopy |
| COPY / PHI / REG_SEQUENCE | 无 | 一个都没有 | invalid |

表里最容易记错的是中间几行：**手工在 MIR 里给 `G_ADD` 写 `%0:gpr`，并不会让基类映射成功。** 头文件注释里仍写着 “Any instruction if all the register operands have already been assigned a register, a register class, or a register bank”，但 23.1.4 的实现不是这样：`CurRegBank = IsCopyLike ? AltRegBank : nullptr` 把非 copy-like 的已有 bank 直接丢掉了。这是文档与源码不一致的一个真实例子，读写 mapping 时以代码为准。
### 为什么普通指令不沿用已有 bank
代码里这一行很关键：
```cpp
const RegisterBank *CurRegBank = IsCopyLike ? AltRegBank : nullptr;
```
对非 copy-like 指令，`AltRegBank` 读出来之后就被丢掉了，不会进入 mapping。源码给出的理由是：已有 bank 可能只是前一条指令临时留下的副作用，不代表当前指令的最佳选择。注释接着承诺：

> The register bank of Reg is just a side effect of the current execution and in particular, there is no reason to believe this is the best default mapping for the current instruction. Keep it as an alternative register bank if we cannot figure out something.

但注释里的 “Keep it as an alternative register bank if we cannot figure out something” 在当前代码里并没有对应的实现：`CurRegBank` 仍然是 `nullptr`，随后直接返回 invalid。所以普通指令的默认 mapping 必须来自约束或 target 的显式选择；已有 bank 只在 copy-like 里被信任。
### `cannotCopy` 检查
copy-like 分支最后会遍历所有 reg 操作数：
```cpp
if (AltRegBank &&
    cannotCopy(*CurRegBank, *AltRegBank, getSizeInBits(Reg, MRI, TRI)))
  return getInvalidInstructionMapping();
```
`cannotCopy` 等价于 `copyCost(...) == UINT_MAX`。AArch64 的 `copyCost` 对 GPR/FPR 返回 4 或 5，不会返回 `UINT_MAX`，所以这条检查在 AArch64 上通常不会触发；但自定义 target 可以用它表达“这两个 bank 之间根本不能 copy”。
## 示例一：ABI 边界的 COPY
【实测】`add.ll` 在 `-O0 -global-isel -stop-after=regbankselect` 下的 body：
```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $x0, $x1

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```
看 `%0:gpr(i64) = COPY $x0`：

- `MI.getNumOperands() == 2`（def + 物理源）；
- 但 AArch64 的 COPY 分支只映射 def，`NumOperands = 1`；
- `$x0` 是物理寄存器，`getRegBank` 通过最小物理寄存器类得到 GPR；
- AArch64 的 COPY 分支先取 `DstRB = getRegBank(%0)`（此时为空），再用 `SrcRB = GPR` 补上，得到 `ValueMapping = {0, 64, GPR}`；
- 最后返回 `InstructionMapping(DefaultMappingID, copyCost(GPR, GPR, 64), {GPR64}, 1)`；同 bank copy 的 `copyCost` 是 0，不是 1。

这条 COPY 不是 repair，而是 ABI 边界：物理参数寄存器 `$x0` 进入 vreg 世界。它的 mapping 只描述 def `%0`，源 `$x0` 不在数组里。

还要注意：这条 mapping 不是 `getInstrMappingImpl` 给的。AArch64 对 `COPY` 不调用基类（条件里的 `Opc != TargetOpcode::COPY`），而是走自己的 `case TargetOpcode::COPY`；这也是 cost 为 0 而不是基类固定 1 的原因。
### 同一段 MIR 里的 `G_ADD`
`%2:gpr(i64) = G_ADD %0, %1` 的 mapping 不是 `getInstrMappingImpl` 给的，因为 AArch64 根本不会为 generic opcode 调用基类：

- `G_ADD` 是 pre-isel generic opcode；
- 即使调用了，它的 MCInstrDesc 也没有 regclass 约束，基类会返回 invalid；
- AArch64 直接进自己的 opcode switch，由 `getSameKindOfOperandsMapping` 按类型选 GPR。

条件是 `(Opc != COPY && !isPreISelGenericOpcode(Opc)) || Opc == G_PHI`：`G_ADD` 不满足，直接进 switch。对比之下，`G_PHI` 是 pre-isel generic 里唯一会被 AArch64 送去基类的 opcode；target-specific 的 pre-isel 伪指令也会走基类。
## 示例二：PHI 与 G_BRCOND
【实测】`phi.ll` 在 `-O0 -global-isel -stop-after=regbankselect` 下的关键片段：
```yaml
  bb.4.m:
    %6:gpr(i64) = G_PHI %1(i64), %bb.2, %2(i64), %bb.3
    $x0 = COPY %6(i64)
    RET_ReallyLR implicit $x0
```
`G_PHI` 的 MI 操作数是：
```text
0: def %6
1: incoming %1
2: MBB %bb.2
3: incoming %2
4: MBB %bb.3
```
但 `getInstrMappingImpl` 只映射 operand 0，`NumOperands = 1`。它从 `%1` 或 `%2` 已有的 bank 推导 def 的 bank；这里两者都是 GPR，所以 `%6` 也是 GPR。MBB 操作数不参与 mapping。

同一份 MIR 的 entry 块里还有：
```yaml
    %7:gpr(i32) = G_AND %4, %9
    G_BRCOND %7(i32), %bb.2
```
`G_BRCOND` 的 operand 1 是 MBB（非寄存器）。AArch64 的 `getInstrMapping` 对它有专门分支：
```cpp
case TargetOpcode::G_BRCOND:
  return getInstructionMapping(
      DefaultMappingID, /*Cost=*/1,
      getOperandsMapping({getValueMapping(PMI_FirstGPR, ...), nullptr}),
      /*NumOperands=*/2);
```
数组第二项是 `nullptr`，最终变成无效 `ValueMapping`。这正好对应 Ch 5 的规则：非寄存器位置必须是无效 mapping。
## 示例三：没有 target 覆写时会发生什么
【源码依据】假设一个 target 只继承了 `RegisterBankInfo`，没有覆写 `getInstrMapping`。对 `G_ADD`：

1. `isCopyLike(G_ADD)` 为 false；
2. `NumOperandsForMapping = 3`；
3. operand 0 是 def，`getRegBank` 返回 `nullptr`（还没分配）；
4. `getRegBankFromConstraints` 调 `MI.getRegClassConstraint(0, ...)`；
5. `G_ADD` 是 generic opcode，MCInstrDesc 没有 regclass，返回 `nullptr`；
6. `CompleteMapping = false`，且 `!IsCopyLike`，直接 `return getInvalidInstructionMapping()`；
7. 基类 `getInstrMapping` 看到 invalid，执行 `llvm_unreachable("The target must implement this")`。

所以“写一个新 target 的 RegisterBankInfo”时，只实现 `getRegBankFromRegClass` 是不够的；还要为 generic opcode 写 `getInstrMapping`（或至少写一个类型启发式）。Ch 21 会给骨架。
## 对比与取舍
| 方案 | 优点 | 局限 |
| --- | --- | --- |
| 完全依赖 `getInstrMappingImpl` | 无需为每个 opcode 写表；target-specific 指令自动工作 | generic opcode 基本不可用；没有代价建模 |
| target 覆写 `getInstrMapping` + 类型启发式 | 能覆盖所有 generic opcode；实现简单 | 规则是局部的，可能选错 bank；需要大量特判 |
| target 表驱动（Ch 7） | 可预测、易测试 | 表大，维护成本高；仍需处理边角类型 |
| 完全手写每个 opcode 的 mapping | 最精确 | 不现实，且容易和 Legalizer/ISel 脱节 |

`getInstrMappingImpl` 的定位不是“通用映射器”，而是“**能用编码约束或 copy-like 的已有 bank 解决的部分**”。target 的 `getInstrMapping` 才是策略层。
## 踩雷集锦
### 1. 以为 `getInstrMappingImpl` 能映射所有 generic 指令
错：
```cpp
// 新 target 只实现 getRegBankFromRegClass，指望 G_ADD 自动工作
```
对。generic opcode 没有 regclass 约束，基类直接返回 invalid；已有 bank 也救不了——非 copy-like 根本不看它。target 必须自己写 `getInstrMapping`。
### 2. 对普通指令沿用已有 bank 作为默认
错：
```cpp
// 看到 %0 已经是 GPR，就认为 G_ADD 的默认 mapping 也是 GPR
```
对。非 copy-like 指令的 `CurRegBank` 被刻意设为 `nullptr`，已有 bank 连“备选”都算不上，只是被 `getRegBank` 读出来后又丢掉了。默认 mapping 必须来自约束或 target 选择。
### 3. 对 `COPY` 的 mapping 写两个操作数
错：
```cpp
InstructionMapping m(ID, cost, ops, /*NumOperands=*/2);
```
对。`COPY` 是 copy-like，`NumOperands = 1`。写 2 会在 `InstructionMapping::verify` 里触发 `NumOperands must match`。
### 4. 把 `getRegClassConstraint` 的返回值当成 bank
错：
```cpp
const RegisterBank *RB = MI.getRegClassConstraint(OpIdx, &TII, &TRI);
```
对。它返回 `const TargetRegisterClass *`，还要经过 `getRegBankFromRegClass` 才能变成 bank。类型不匹配，编译期就会报错；更隐蔽的是忘了处理 `nullptr`。
### 5. 把 invalid mapping 当成“自动 fallback 到 SelectionDAG”
错：
```text
getInstrMappingImpl 返回 invalid → 一定 fallback
```
对。invalid 只表示“基类不知道”。target 的 `getInstrMapping` 可能接手并生成有效 mapping；只有最终 `assignInstr` 失败，才会 `reportGISelFailure` 并设置 `FailedISel`。是否 abort 由 `-global-isel-abort` 决定。
## 进阶
- **读 `getRegBankFromConstraints` 的 assert**：`RegBank.covers(*RC)` 是 target 实现正确性的第一道检查。写新 bank 时，先确保 coverage 包含所有会用到的 RC。
- **读 `MachineInstr::getRegClassConstraint` 的 inline asm 分支**：inline asm 的约束存在 flag word 里，和普通 MCInstrDesc 不同；`assignRegisterBanks` 会跳过 inline asm，但理解这条路径有助于排查“为什么这个操作数没有约束”。
- **读 `G_ASSERT_ZEXT` / `G_ASSERT_SEXT` / `G_ASSERT_ALIGN`**：`assignInstr` 对它们是特判，直接沿用源操作数 bank，不调用 `getInstrMapping`。这类“优化提示”指令是默认 mapping 的例外。
- **读 AArch64 的 `getInstrMapping` 开头**：它对 `Opc != COPY && !isPreISelGenericOpcode(Opc)` 或 `G_PHI` 才调用 `getInstrMappingImpl`；普通 generic opcode 直接进 switch。这个条件决定了“哪些指令走基类”。
## 动手练习
1. 【实测】用 `add.ll` 和 `phi.ll` 各跑一次 `-stop-after=regbankselect`：确认 `add.ll` 里没有指令走 `getInstrMappingImpl`（COPY 走 AArch64 的 COPY 分支，`G_ADD` 走 `getSameKindOfOperandsMapping`），而 `phi.ll` 的 `G_PHI` 走基类的 copy-like 分支。
2. 【实测】用 `phi.ll` 找到 `G_PHI`，数出它的 MI 操作数个数和 mapping 的 `NumOperands`，解释为什么两者不相等。
3. 【源码依据】在 `RegisterBankInfo.cpp` 里找到 `CompleteMapping` 的所有赋值点，画出“什么情况下 copy-like 会返回 invalid”。
4. 【guided】自己构建 assertions 版本，给 generic `G_ADD` 的 `%0` 预先设成 `gpr`、其余不设，确认基类仍然返回 invalid（已有 bank 不会救场）；官方包看不到 `-debug-only`，不要伪造输出。
## 本章重点整理
- `getInstrMappingImpl` 只处理两类信息：target 编码约束，以及 copy-like 自带的已有 bank。
- 普通 generic 指令没有 regclass 约束，基类直接返回 invalid，不看操作数已有的 bank。
- copy-like（`COPY` / `PHI` / `REG_SEQUENCE`）只映射 def，`NumOperands = 1`。
- `getRegClassConstraint` 返回 RC，不是 bank；还要 `getRegBankFromRegClass`。
- AArch64 的 `getInstrMapping` 只对 `(Opc != COPY && !isPreISelGenericOpcode(Opc)) || Opc == G_PHI` 先试基类；基类无效或条件不满足时进 opcode switch。不是“基类失败就 fallback 到 SelectionDAG”。
- invalid mapping 的 `ID = InvalidMappingID`、`OperandsMapping = nullptr`，`isValid()` 为 false。
## 自我检核
- [ ] 能说出 `getInstrMappingImpl` 能处理的两类情况
- [ ] 能解释为什么 generic `G_ADD` 在基类里通常返回 invalid
- [ ] 能说出 `COPY` / `PHI` / `REG_SEQUENCE` 的 `NumOperands` 为什么是 1
- [ ] 能解释 `getRegClassConstraint` 与 `getRegBankFromRegClass` 的分工
- [ ] 能区分“invalid mapping”和“pipeline fallback”
- [ ] 能看着 `G_BRCOND` 的 mapping 说出哪个位置是无效 ValueMapping
## 延伸阅读
### 源码
- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - **读哪里**：`isCopyLike`、`getInstrMappingImpl`、`getRegBankFromConstraints`、`getRegBank`。
  - **学什么**：默认 mapping 的完整决策路径，以及 invalid 的返回条件。
  - **和本章的关联**：本章的主线就是这四个函数。
- **[MachineInstr.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/MachineInstr.cpp)**
  - **读哪里**：`MachineInstr::getRegClassConstraint`。
  - **学什么**：普通指令、inline asm、tied operand 分别从哪里取约束。
  - **和本章的关联**：解释了“为什么 generic opcode 没有约束”。
### 文档
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：默认 mapping 与 target 覆写的说明。
  - **学什么**：官方文档对 `getInstrMapping` / `getInstrAlternativeMappings` 的定位。
  - **和本章的关联**：先看文档的分层，再回到源码看实现。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`G_PHI`、`G_BRCOND`、`COPY` 的操作数写法。
  - **学什么**：MI 操作数顺序，尤其是 MBB / immediate 这类非寄存器操作数。
  - **和本章的关联**：不会数操作数，就无法理解 `NumOperands` 的不变量。

下一章拆 AArch64 的 mapping 表：`AArch64GenRegisterBankInfo.def` 里的 `PMI_FirstGPR` / `PMI_FirstFPR`、三连表项、跨 bank copy 表和 opcode/type 分派。

→ [Ch 7 target mapping 表：opcode/type 分派](./07-target-mapping-tables.md)
