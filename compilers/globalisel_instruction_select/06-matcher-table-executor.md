# 第 6 章 匹配表执行器：从 `.td` 到机器指令的最后一公里

> **本章目标**：读懂 `executeMatchTable` 如何把一张字节码表跑成选择结果；知道 `GIM_*`（匹配）与 `GIR_*`（渲染）指令的分工，以及回溯栈、记录指令、类型检查分别在做什么。
>
> **前置**：第 2、5 章。**环境**：LLVM 23.1.4。

---

## 6.1 表长什么样

`getMatchTable()` 返回的是：

```cpp
virtual const uint8_t *getMatchTable() const {
  llvm_unreachable("Should have been overridden by tablegen if used");
}
```

一个**字节数组**，不是结构体数组。执行器按顺序读：先读 1 字节操作码，再按该操作码的格式读后续操作数（ULEB128、u16、u32、s8……）。

`executeMatchTable` 的签名（`GIMatchTableExecutor.h`）：

```cpp
template <class TgtExecutor, class PredicateBitset, class ComplexMatcherMemFn,
          class CustomRendererFn>
bool GIMatchTableExecutor::executeMatchTable(
    TgtExecutor &Exec, MatcherState &State,
    const ExecInfoTy<...> &ExecInfo,
    MachineIRBuilder &Builder, const uint8_t *MatchTable,
    const TargetInstrInfo &TII, MachineRegisterInfo &MRI,
    const TargetRegisterInfo &TRI, const RegisterBankInfo &RBI,
    const PredicateBitset &AvailableFeatures,
    CodeGenCoverage *CoverageInfo) const;
```

参数分三类：

| 参数 | 作用 |
| --- | --- |
| `State` | 当前匹配状态：`MIs`（已记录的指令）、临时寄存器、物理寄存器 |
| `ExecInfo` | 静态数据：类型表、特性位集、复杂谓词函数表、自定义渲染器表 |
| `TII/MRI/TRI/RBI` | 目标信息，渲染时需要 |
| `CoverageInfo` | 规则覆盖率（可选） |

---

## 6.2 执行循环骨架

`GIMatchTableExecutorImpl.h`：

```cpp
uint64_t CurrentIdx = 0;
SmallVector<uint64_t, 8> OnFailResumeAt;
NewMIVector OutMIs;

auto handleReject = [&]() -> RejectAction {
  if (OnFailResumeAt.empty())
    return RejectAndGiveUp;
  CurrentIdx = OnFailResumeAt.pop_back_val();
  return RejectAndResume;
};

while (true) {
  uint8_t MatcherOpcode = MatchTable[CurrentIdx++];
  switch (MatcherOpcode) {
  ...
  }
}
```

三个核心变量：

- `CurrentIdx`：字节码游标；
- `OnFailResumeAt`：**回溯栈**，保存"这条规则失败后该跳到哪"；
- `OutMIs`：待输出的新指令列表。

失败处理只有一个入口 `handleReject()`：

- 栈非空 → 弹出一个位置，跳过去继续试下一条候选规则；
- 栈空 → `return false`，整个 `selectImpl` 失败。

成功路径是遇到 `GIR_Done`：

```cpp
case GIR_Done: {
  propagateFlags();
  return true;
}
```

---

## 6.3 三类指令

### 6.3.1 匹配类 `GIM_*`

| 指令 | 作用 | 典型操作数 |
| --- | --- | --- |
| `GIM_Try` | 开始一个候选块，压入失败跳转位置 | `OnFail(4)` |
| `GIM_Try_CheckFeatures` | 特性不匹配就直接跳到失败点 | `OnFail(4)`、`Feature(2)` |
| `GIM_RecordInsn` | 把某个操作数定义者记录为新编号的 MI | `NewInsnID`、`InsnID`、`OpIdx` |
| `GIM_CheckOpcode` | 检查操作码 | `InsnID`、`Opc(2)` |
| `GIM_CheckOpcodeIsEither` | 两个操作码之一 | 同上 + `Opc2` |
| `GIM_CheckNumOperands` | 操作数个数 | `InsnID`、`Ops` |
| `GIM_CheckType` | 操作数 LLT 必须是某个类型 | `InsnID`、`OpIdx`、`Ty(1)` |
| `GIM_CheckRegBankForClass` | 操作数 bank 必须匹配 | `InsnID`、`OpIdx`、`RC(2)` |
| `GIM_CheckConstantInt` | 必须是特定常量 | `InsnID`、`OpIdx`、`Val(8)` |
| `GIM_CheckComplexPattern` | 交给 GI 复杂谓词 | `InsnID`、`OpIdx`、`RendererID`、`Pred` |
| `GIM_SwitchType` | 按类型分派到不同子表 | 类型区间 + 跳转表 |
| `GIM_Reject` | 主动拒绝当前候选 | — |

### 6.3.2 渲染类 `GIR_*`

| 指令 | 作用 |
| --- | --- |
| `GIR_MutateOpcode` | 原地改写指令（最常见：`G_ADD` → `ADDXrr`） |
| `GIR_BuildMI` | 新建一条指令 |
| `GIR_Copy` | 把已匹配指令的操作数复制到新指令 |
| `GIR_CopyOrAddZeroReg` | 复制，或在特定情况下改用零寄存器 |
| `GIR_ConstrainOperandToRegClass` | 给操作数约束寄存器类 |
| `GIR_MakeTempReg` | 创建临时 vreg |
| `GIR_Coverage` | 记录规则命中（插桩构建） |
| `GIR_Done` | 成功结束 |

### 6.3.3 控制类

`GIM_Try` / `GIM_SwitchType` 会往回溯栈压位置，`GIM_Reject` 和 `GIR_Done` 决定"继续回溯"还是"结束"。

---

## 6.4 一条规则的表序列（示意）

以 `G_ADD` → `ADDXrr` 为例，表大致长这样：

```text
GIM_Try            OnFail=下一条规则
GIM_CheckOpcode    MIs[0], G_ADD
GIM_CheckNumOperands MIs[0], 3
GIM_CheckType      MIs[0], 0, s64
GIM_CheckType      MIs[0], 1, s64
GIM_CheckRegBankForClass MIs[0], 1, GPR64
GIM_CheckRegBankForClass MIs[0], 2, GPR64
GIR_MutateOpcode   MIs[0] -> OutMIs[0], ADDXrr
GIR_Copy           OutMIs[0], MIs[0], 0
GIR_Copy           OutMIs[0], MIs[0], 1
GIR_Copy           OutMIs[0], MIs[0], 2
GIR_ConstrainOperandToRegClass OutMIs[0], 0, GPR64
GIR_Coverage       RuleID
GIR_Done
```

> 这是**语义示意**：真实表的顺序、编号、跳转偏移由 TableGen 生成，可能把类型检查合并成 `GIM_SwitchType`，也可能调整指令顺序。但它展示了"检查—改写—复制—约束—完成"的基本节奏。

关键点：

- **根指令固定是 `MIs[0]`**。执行器里的注释明确写了这个优化：
  ```cpp
  // As an optimisation we require that MIs[0] is always the root.
  assert(NewInsnID != 0 && "Refusing to modify MIs[0]");
  ```
  （这条断言在 `GIM_RecordInsn` 里，防止把根覆盖掉。）
- 匹配阶段只读，渲染阶段才写；
- 寄存器类约束是**选择器责任**（第 2 章 2.4.2），表里通过 `GIR_ConstrainOperandToRegClass` 完成。

---

## 6.5 回溯：为什么需要栈

一条 `G_ADD` 可能匹配几十条规则（不同寄存器类、不同立即数形态、不同特性）。执行器的策略是：

1. 每条规则开头一个 `GIM_Try`，压入"下一条规则"的偏移；
2. 规则内任一步失败，`GIM_Reject` 弹出栈顶，跳到那个偏移；
3. 全部失败且栈空 → 返回 false。

这等价于一个手写的 `if/else if` 链，但规则数量可以上千，且支持嵌套的子匹配。

### 6.5.1 `GIM_SwitchType`

类型分派不是链式比较，而是紧凑跳转表：

```text
GIM_SwitchType  MIs[0], OpIdx, Lower, Upper, Default, JumpTable...
```

执行器直接按类型 ID 索引跳转表。这解释了为什么生成表里"类型"是编号而不是 `LLT` 对象：`ExecInfo.TypeObjects` 是一张去重后的类型表，编号就是索引。

### 6.5.2 `GIM_Try_CheckFeatures`

特性检查被单独优化：

```cpp
case GIM_Try_CheckFeatures: {
  unsigned OnFail = readU32();
  uint16_t ExpectedBitsetID = readU16();
  if ((AvailableFeatures & ExecInfo.FeatureBitsets[ExpectedBitsetID]) !=
      ExecInfo.FeatureBitsets[ExpectedBitsetID]) {
    CurrentIdx = OnFail;          // 不压栈，直接跳
  } else {
    OnFailResumeAt.push_back(OnFail);
  }
```

原因很实际：一个 subtarget 特性不匹配的规则块可能有几百条规则，跳过时没必要逐条压栈。

---

## 6.6 折叠：`GIM_RecordInsn`

第 2 章讲过遍历是 uses-before-defs，所以选择一条指令时，它的生产者可能还没被选。执行器用 `GIM_RecordInsn` 把生产者"记录"进状态：

```cpp
case GIM_RecordInsn: {
  uint64_t NewInsnID = readULEB();
  uint64_t InsnID = readULEB();
  uint64_t OpIdx = readULEB();

  MachineOperand &MO = State.MIs[InsnID]->getOperand(OpIdx);
  if (!MO.isReg()) { /* reject */ }
  if (MO.getReg().isPhysical()) { /* reject */ }

  MachineInstr *NewMI;
  if (MatcherOpcode == GIM_RecordInsnIgnoreCopies)
    NewMI = getDefIgnoringCopies(MO.getReg(), MRI);
  else
    NewMI = MRI.getVRegDef(MO.getReg());

  if ((size_t)NewInsnID < State.MIs.size())
    State.MIs[NewInsnID] = NewMI;
  else
    State.MIs.push_back(NewMI);
}
```

之后规则里就可以用 `MIs[NewInsnID]` 引用生产者，检查它的操作码/类型/立即数，并在渲染时把它折进结果。

### 6.6.1 常量折叠就是这样发生的

练习 A.2 里 `G_CONSTANT 1` 被折进 `ADDXri`，对应的表逻辑是：

1. 根是 `G_ADD`（`MIs[0]`）；
2. `GIM_RecordInsn MIs[1], MIs[0], 2` 记录第二个操作数的定义者；
3. 检查 `MIs[1]` 是 `G_CONSTANT`，且常量能用 12 位立即数表示；
4. 渲染 `ADDXri`，把常量直接写成立即数操作数；
5. 生产者 `MIs[1]` 没有被输出——之后在遍历到它时，`selectInstr` 的 `isTriviallyDead` 会把它删掉。

这也解释了为什么 `GIM_RecordInsn` 只允许记录 vreg 定义者：物理寄存器没有"定义指令"可折。

### 6.6.2 `GIM_RecordInsnIgnoreCopies`

第 5 章提到 `PatIgnoreCopies`。它生成的规则会在记录生产者时**穿过 COPY**，直接找到 COPY 的源。对应操作码是 `GIM_RecordInsnIgnoreCopies`，与 `GIM_RecordInsn` 共用一个 `case`，区别只在是否跳过 COPY。

---

## 6.7 渲染：`GIR_*` 如何构造指令

### 6.7.1 原地改写 vs 新建

- `GIR_MutateOpcode`：把根指令的操作码换掉。适合 1:1 且操作数数量兼容的情况（`G_ADD` → `ADDXrr`）。
- `GIR_BuildMI`：新建一条指令。适合 1:N（`G_SELECT` → `ANDSWri` + `CSELXr`）或操作数布局变化很大的情况。

### 6.7.2 操作数复制

```cpp
case GIR_Copy: {
  uint64_t NewInsnID = readULEB();
  uint64_t OldInsnID = readULEB();
  uint64_t OpIdx = readULEB();
  OutMIs[NewInsnID].add(State.MIs[OldInsnID]->getOperand(OpIdx));
  break;
}
```

`GIR_CopyOrAddZeroReg` 是它的变体：当某个操作数需要"零"时，直接用 `XZR/WZR`，而不是物化一个常量寄存器。这就是第 5 章 `G_CONSTANT 0` 走 C++ 的同类优化在表里的对应物。

### 6.7.3 约束寄存器类

`GIR_ConstrainOperandToRegClass` 在渲染完成后把 vreg 约束到具体类。这一步如果漏了，就是第 2 章说的 `VReg has no regclass after selection`。

### 6.7.4 成功与标志传播

`GIR_Done` 之前会调用 `propagateFlags()`：

```cpp
const auto propagateFlags = [&]() {
  for (auto MIB : OutMIs) {
    uint32_t MIBFlags = Flags | MIB.getInstr()->getFlags();
    if (NoFPException && MIB->mayRaiseFPException())
      MIBFlags |= MachineInstr::NoFPExcept;
    ...
  }
};
```

它把根指令的标志位传给新指令，并在"原指令不可能抛 FP 异常、新指令可能"时打上 `NoFPExcept`。这类细节不会出现在 `.td` 里，但会影响正确性。

---

## 6.8 复杂操作数：谓词与渲染器分离

`GIM_CheckComplexPattern` 只做匹配，把结果存进 `RendererID`：

```text
GIM_CheckComplexPattern  InsnID, OpIdx, RendererID(2), Pred(2)
```

真正的渲染发生在后面的 `GIR_*` 阶段，通过 `RendererID` 找到对应的 `ComplexRendererFn`。这与第 5 章的实现纪律一致：

- 谓词阶段只读 MIR；
- 渲染器阶段才创建指令；
- 渲染器按值捕获，避免悬空引用。

---

## 6.9 类型与 bank 的检查顺序

一个常见疑问：为什么有的规则检查类型、有的检查 bank？

- `GIM_CheckType`：确认 LLT 完全匹配（`s64` 而不是 `s32`）；
- `GIM_CheckRegBankForClass`：确认 bank 匹配（`gpr` 而不是 `fpr`）；
- `GIR_ConstrainOperandToRegClass`：把 bank 收紧到具体类（`gpr64`）。

执行器并不要求固定顺序；生成器通常把"操作码 → 操作数个数 → 类型 → bank → 立即数/复杂模式 → 渲染"排成这个次序。读调试输出时按这个次序对照，比较容易定位是在哪一类检查上被拒绝。

---

## 6.10 覆盖率与调试

- `GIR_Coverage` 在插桩构建里记录命中的 RuleID；
- 输出用 `-gisel-coverage-prefix=<path>`（需要 configure 时定义 `LLVM_GISEL_COV_PREFIX`）；
- 官方预编译包没有这个能力。

运行时调试（需要 assertions 构建）：

```console
llc -debug-only=instruction-select ...
```

会打印每一步的 `GIM_*` / `GIR_*` 轨迹：

```text
0: GIM_Try
...
12: GIM_CheckOpcode(MIs[0], ExpectedOpcode=G_ADD) // Got=G_ADD
...
40: GIR_MutateOpcode
41: GIR_Done
```

这是定位"为什么这条规则没命中"最直接的手段：看它在哪一步 `Rejected`。

---

## 6.11 性能上的取舍

| 设计 | 收益 | 代价 |
| --- | --- | --- |
| 字节码表 | 紧凑、缓存友好 | 不可读，只能靠调试输出 |
| 线性扫描 + 回溯 | 实现简单、规则顺序可控 | 最坏情况 O(规则数) |
| 类型编号 + 跳转表 | 避免重复比较 LLT | 需要静态类型表 |
| 记录指令而非递归 | 折叠逻辑统一 | 需要维护 `MIs` 编号 |
| 只在必要时建 `MachineIRBuilder` | 减少构造开销 | 代码里有延迟初始化 |

与 SelectionDAG 的树匹配相比，GlobalISel 的表匹配没有"自底向上重建 DAG"的开销，但规则顺序敏感：把高频规则放前面是 TableGen 的职责（`AddedComplexity` 等）。

---

## 进阶延伸

- 用 `-gen-global-isel` 生成真实表，统计一条规则的字节数与检查数量。
- 读 `CodeGenCoverage`，理解 matcher 覆盖率如何帮助发现“永远不命中”的 pattern。
- 思考：如果新增一个 `GIM_*` 操作码，需要同步修改哪些 emitter、executor 和测试？
## 踩雷清单

1. **把 `GIM_*` 和 `GIR_*` 混为一谈**。前者只读，后者才写；在匹配阶段创建指令会导致状态不一致。
2. **以为 `MIs[0]` 可以随便改**。执行器明确拒绝修改根（`Refusing to modify MIs[0]`）。
3. **以为表是结构体数组**。它是紧凑字节码，偏移和编码都是 TableGen 算好的。
4. **在 release 构建里期待调试轨迹**。`-debug-only` 需要 assertions。
5. **忽略 `GIR_ConstrainOperandToRegClass`**。匹配成功但 vreg 没类，最终会 fallback。
6. **以为一条规则只对应一条目标指令**。`GIR_BuildMI` 可以建多条，`G_SELECT` 就是两条。

---

## 动手练习

### 练习 6.1 读执行器

打开 `GIMatchTableExecutorImpl.h`，找出：

1. `GIM_Try` 如何压栈；
2. `GIM_Reject` 如何回溯；
3. `GIR_Done` 返回前做了什么。

用三句话概括。

### 练习 6.2 数一条规则的检查

在断言构建里用 `-debug-only=instruction-select` 跑第 4 章的 `add-hand.mir`，数一数 `G_ADD` 从开始匹配到 `GIR_Done` 经过了多少条指令。哪些是类型检查、哪些是 bank 检查？

### 练习 6.3 观察回溯

构造一条会失败的规则场景（例如把 `G_ADD` 的操作数类型改成 `s32`），观察调试输出里 `Rejected` 与 `Resume at` 的配对关系。

### 练习 6.4 折叠的边界

用 `add i64 %a, 5000`（超出 12 位立即数范围）测试，观察是否还折进 `ADDXri`。如果不折，选择器改用了什么？这对应表里的哪类检查？

### 练习 6.5 多指令输出

用 `G_SELECT` 的 MIR 跑调试输出，找出第二条指令是通过哪个 `GIR_*` 创建的，以及 `$nzcv` 是在哪一步被引用。

---

## 重点整理

- 匹配表是 `const uint8_t*` 字节码，`executeMatchTable` 用 `switch` 解释执行。
- `GIM_*` 只读匹配，`GIR_*` 负责渲染，`GIM_Try`/`GIM_Reject` 提供回溯。
- 根指令固定是 `MIs[0]`；`GIM_RecordInsn` 把生产者记录成 `MIs[k]` 以支持折叠。
- 常量折叠 = 记录 `G_CONSTANT` + 检查立即数范围 + 渲染成立即数操作数。
- 类型用编号 + 跳转表；bank 用 `GIM_CheckRegBankForClass`；具体类用 `GIR_ConstrainOperandToRegClass`。
- 复杂操作数把"匹配"和"渲染"分成两步，保证谓词阶段不修改 MIR。

---

## 自我检核

1. `getMatchTable()` 返回什么类型？执行器如何区分"操作码"和"操作数"？
2. `OnFailResumeAt` 的作用是什么？什么时候会 `return false`？
3. 为什么 `GIM_RecordInsn` 不能记录物理寄存器？
4. `GIR_MutateOpcode` 与 `GIR_BuildMI` 分别适合什么场景？
5. `GIR_CopyOrAddZeroReg` 与第 5 章的 `G_CONSTANT 0` 有什么关系？
6. 一条 `G_SELECT` 规则如何产生两条目标指令？
7. 为什么类型检查通常排在 bank 检查之前？

---

## 延伸阅读

- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h`：全部 `GIM_*`/`GIR_*` 操作码及其编码格式，注释就是字节码规范。
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`：执行器实现，重点看 `handleReject`、`GIM_RecordInsn`、`GIR_Done`。
- `llvm/utils/TableGen/GlobalISelEmitter.cpp`：表是怎么被"写"出来的，与执行器互为逆过程。
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h` 的 `MatcherState`：`MIs`、`TempRegisters`、`RecordedTypes` 的布局。
- 官方文档《InstructionSelect》的 "API: InstructionSelector" 一节：<https://llvm.org/docs/GlobalISel/InstructionSelect.html> —— 说明 `select()` 与生成表的分工；读完执行器后再看，能对上 `selectImpl()` 的调用点。

---

## 下一章

第 7 章开始拆 pattern 的细节：**谓词、类型、bank 与立即数**。我们会看 `Predicate` 如何变成位集、`ImmLeaf` 如何限制立即数范围，以及为什么同一个操作码会按类型分叉出多条规则。

[第 7 章 谓词系统 I：类型、寄存器 bank 与立即数](./07-predicates-types-banks-immediates.md)
