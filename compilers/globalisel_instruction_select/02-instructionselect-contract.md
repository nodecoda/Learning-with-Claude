# 第 2 章 InstructionSelect 的契约：pass、函数与指令三层

> **本章目标**：能准确回答"InstructionSelect 承诺了什么、谁在检查、破坏承诺会在哪里爆"，并能看懂任意一份 `-stop-after=instruction-select` 的 MIR。
>
> **前置**：第 0、1 章。**验证环境**：LLVM 23.1.4 官方 x86_64 Windows 预编译包（Optimized build，assertions 关闭），AArch64 后端。

---

## 2.1 一条指令的完整变化

对最小的 `add`：

```llvm
define i64 @add(i64 %a, i64 %b) {
entry:
  %r = add i64 %a, %b
  ret i64 %r
}
```

在 `regbankselect` 后停下：

```yaml
legalized:       true
regBankSelected: true
selected:        false
failedISel:      false
```

```yaml
    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```

把这份 MIR 单独喂给 `instruction-select`：

```console
llc -mtriple=aarch64-linux-gnu -O0 \
    -run-pass=instruction-select add-rbs.mir -o add-sel.mir
```

得到：

```yaml
legalized:       true
regBankSelected: true
selected:        true
failedISel:      false
```

```yaml
    %0:gpr64 = COPY $x0
    %1:gpr64 = COPY $x1
    %2:gpr64 = ADDXrr %0, %1
    $x0 = COPY %2
    RET_ReallyLR implicit $x0
```

三处变化对应本章的三个层次：

| 观察点 | 选择前 | 选择后 | 契约层次 |
| --- | --- | --- | --- |
| 函数属性 | `selected: false` | `selected: true` | pass 级 |
| 操作码 | `G_ADD` | `ADDXrr` | 函数级 postcondition |
| 虚拟寄存器 | `%2:gpr(i64)` | `%2:gpr64` | 指令级 `select()` 副作用 |

`%2:gpr64` 后面不再有 `(i64)`，因为 `clearVirtRegTypes()` 真的删掉了 LLT，而不是打印器省略。

---

## 2.2 第一层：pass 声明的属性

`llvm/include/llvm/CodeGen/GlobalISel/InstructionSelect.h`：

```cpp
MachineFunctionProperties getRequiredProperties() const override {
  return MachineFunctionProperties()
      .setIsSSA().setLegalized().setRegBankSelected();
}

MachineFunctionProperties getSetProperties() const override {
  return MachineFunctionProperties().setSelected();
}
```

pass 不自己检查前置条件，而是声明"我需要什么、我会设置什么"，由基础设施统一检查。属性本身是位集合：

```cpp
enum class Property : unsigned {
  IsSSA, NoPHIs, TracksLiveness, NoVRegs, FailedISel,
  Legalized, RegBankSelected, Selected, /* ... */
};

bool verifyRequiredProperties(const MachineFunctionProperties &V) const {
  return (Properties | ~V.Properties).all();   // V 要求的每一位都为 1
}
```

检查点在 `llvm/lib/CodeGen/MachineFunctionPass.cpp`：

```cpp
#ifndef NDEBUG
  if (!MFProps.verifyRequiredProperties(RequiredProperties)) {
    errs() << "MachineFunctionProperties required by " << getPassName()
           << " pass are not met by function " << F.getName() << ".\n";
    ...
    reportFatalUsageError("MachineFunctionProperties check failed");
  }
#endif
```

两个要点：

1. 这是 **fatal usage error**，不是 fallback。属性不满足意味着 pass 调度写错了，属于编译器自身 bug。
2. **整段被 `#ifndef NDEBUG` 包住**。官方 release 包（`llc --version` 显示 `Optimized build`）里这段不存在。

做实验：把 `add-rbs.mir` 的 `regBankSelected` 改成 `false`，其它不动：

```console
llc -mtriple=aarch64-linux-gnu -O0 \
    -run-pass=instruction-select -verify-machineinstrs add-nobank.mir -o out.mir
```

官方预编译包上它**成功退出**，输出与正常情况一样（`%2:gpr64 = ADDXrr %0, %1`），因为检查被编译掉了，AArch64 选择器并不真的读这个属性。换成 assertions 构建，同样的输入会直接：

```text
MachineFunctionProperties required by InstructionSelect pass are not met by function add.
Required properties: IsSSA, Legalized, RegBankSelected
Current properties: IsSSA, Legalized, TracksLiveness
LLVM ERROR: MachineFunctionProperties check failed
```

**属性契约在 debug 构建里是硬门槛，在 release 构建里是文档。** 写测试时要意识到：本地 release 能过，CI 的 assertions 构建可能炸。

同一条执行路径里还有两行容易被跳过的代码：

```cpp
  MFProps.reset(ClearedProperties);
  RV = runOnMachineFunction(MF);
  MFProps.set(SetProperties);        // 无论 RV 真假
```

这就是 2.3.3 节那个反直觉现象的来源。

---

## 2.3 第二层：函数级 postcondition

`InstructionSelect.h` 的类注释只有一句：

```cpp
/// \post for all inst in MF: not isPreISelGenericOpcode(inst.opcode)
```

`isPreISelGenericOpcode` 是纯范围检查（`TargetOpcodes.h`）：

```cpp
inline bool isPreISelGenericOpcode(unsigned Opcode) {
  return Opcode >= TargetOpcode::PRE_ISEL_GENERIC_OPCODE_START &&
         Opcode <= TargetOpcode::PRE_ISEL_GENERIC_OPCODE_END;
}
```

翻译成人话：**跑完 InstructionSelect，函数里不允许再有任何 pre-isel generic 操作码**（`COPY` 等少数"永远合法"的指令除外）。

### 2.3.1 `selected: true` 的三重含义

`MachineFunction.h` 的注释比头文件更具体，拆成可检查的三条：

1. 所有 pre-isel generic 指令都已消除；
2. 所有虚拟寄存器都被约束到 register class（不再只是 bank）；
3. 虚拟寄存器上的 LLT 尺寸信息已删除。

第 1 条推出第 2、3 条：只有 generic 指令才带 generic vreg 操作数，generic 指令没了，vreg 就必须落到具体寄存器类；类型尺寸也就不再需要。

### 2.3.2 谁在检查 postcondition

不是 InstructionSelect 自己，而是 **MachineVerifier**（`MachineVerifier.cpp`）：

```cpp
void MachineVerifier::verifyPreISelGenericInstruction(const MachineInstr *MI) {
  if (isFunctionSelected)
    report("Unexpected generic instruction in a Selected function", MI);
```

`isFunctionSelected` 由 `Props.hasSelected()` 初始化。构造一个违反契约的输入：拿 `add-sel.mir`，把 `ADDXrr` 改回 `G_ADD`：

```console
llc -mtriple=aarch64-linux-gnu -O0 \
    -run-pass=finalize-isel -verify-machineinstrs add-badsel.mir -o NUL
```

```text
# Machine code for function add: IsSSA, TracksLiveness, Legalized, RegBankSelected, Selected
*** Bad machine code: Unexpected generic instruction in a Selected function ***
*** Bad machine code: Generic instruction is missing a virtual register type ***
LLVM ERROR: Found 4 machine code errors.
```

后几条是连带伤害：`clearVirtRegTypes()` 已经删了类型，手工塞回来的 `G_ADD` 自然没有 LLT。

### 2.3.3 反直觉：失败时 `selected` 也是 true

用 `+strict-align` 触发 AArch64 选择器开头的"不支持"分支，在 `regbankselect` 后停下，再单独跑 instruction-select：

```console
llc -mtriple=aarch64-linux-gnu -O0 -mattr=+strict-align \
    -stop-after=regbankselect strict.ll -o strict-rbs.mir
llc -mtriple=aarch64-linux-gnu -O0 \
    -run-pass=instruction-select strict-rbs.mir -o strict-sel.mir
```

函数头：

```yaml
selected:        true
failedISel:      true
```

函数体仍然是 `%2:gpr(i64) = G_ADD %0, %1`。`selected: true` 与"还有 G_ADD"并存，看起来直接违反契约——原因就是 2.2 节那行无条件的 `MFProps.set(SetProperties)`：pass 返回 `false`，但属性照样被置位。

真正把这种情况排除在外的是 MachineVerifier 的提前返回：

```cpp
  // If we're mid-GlobalISel and we already triggered the fallback path then
  // it's expected that the MIR is somewhat broken but that's ok since we'll
  // reset it and clear the FailedISel attribute in ResetMachineFunction.
  if (isFunctionFailedISel)
    return true;
```

**`failedISel` 的优先级高于 `selected`。** 判断一份 MIR 是否真的选择成功，不能只看 `selected: true`。这是本章最值得记住的实践知识。

---

## 2.4 第三层：`select()` 的指令级契约

`InstructionSelector.h`：

```cpp
/// Select the (possibly generic) instruction \p I to only use target-specific
/// opcodes. It is OK to insert multiple instructions, but they cannot be
/// generic pre-isel instructions.
///
/// \returns whether selection succeeded.
/// \post
///   if returns true:
///     for I in all mutated/inserted instructions:
///       !isPreISelGenericOpcode(I.getOpcode())
virtual bool select(MachineInstr &I) = 0;
```

拆成可执行规则：

| 规则 | 含义 | 违反后果 |
| --- | --- | --- |
| 返回 `true` = 这条处理完了 | 可删除、改写、拆成多条 | 留下 generic → verifier 报错 |
| 可插入多条指令 | 一条 generic 变多条 target 指令是常态 | 插入 generic → 同上 |
| 不得留 pre-isel generic | 包括 `G_*` 与部分伪指令 | 同上 |
| 返回 `false` = 选不出来 | **整函数**放弃，不是跳过本条 | 性能损失，非正确性问题 |

### 2.4.1 不能插入基本块

`selectMachineFunction` 在 assertions 构建里记录入口块数：

```cpp
  const size_t NumBlocks = MF.size();
  ...
  if (MF.size() != NumBlocks) {
    MachineOptimizationRemarkMissed R("gisel-select", "GISelFailure", ...);
    R << "inserting blocks is not supported yet";
    reportGISelFailure(MF, MORE, R);
    return false;
  }
```

需要拆块的 lowering（大整数除法、某些调用展开）必须更早完成或退回 SelectionDAG。这是 GlobalISel 与 SelectionDAG 的真实能力差异。

### 2.4.2 vreg 必须落到 regclass

收尾时的断言检查：

```cpp
    const TargetRegisterClass *RC = MRI.getRegClassOrNull(VReg);
    if (!RC) {
      reportGISelFailure(MF, MORE, "gisel-select",
                         "VReg has no regclass after selection", *MI);
      return false;
    }
    const LLT Ty = MRI.getType(VReg);
    if (Ty.isValid() &&
        TypeSize::isKnownGT(Ty.getSizeInBits(), TRI.getRegSizeInBits(*RC))) {
      reportGISelFailure(MF, MORE, "gisel-select",
          "VReg's low-level type and register class have different sizes", *MI);
      return false;
    }
```

第一条：选择器"忘了"给结果约束寄存器类。
第二条：LLT 比 regclass 还大，说明选错类（i64 塞进 32 位类）。用 `isKnownGT` 严格大于，所以 i32 放进 64 位类是允许的。

两者都是 **failure + fallback**，不是 fatal error：退回 SelectionDAG，产物仍然正确。

---

## 2.5 遍历顺序：post-order + reverse

```cpp
for (MachineBasicBlock *MBB : post_order(&MF)) {
  SelectedBlocks.insert(MBB);
  MIIMaintainer.MII = MBB->rbegin();
  for (auto End = MBB->rend(); MIIMaintainer.MII != End;) {
    MachineInstr &MI = *MIIMaintainer.MII;
    ++MIIMaintainer.MII;   // 先自增，跳过 select() 插入的指令
    if (!selectInstr(MI)) {
      reportGISelFailure(MF, MORE, "gisel-select", "cannot select", MI);
      return false;
    }
  }
}
```

两个维度都是反的：块按后序（先后继、后前驱），块内从尾到头。合起来是 **uses before defs**，换来三种能力：

1. **即时 DCE**：选完后结果没人用，可以立刻删。
2. **生产者折叠**：选使用者时顺着 use-def 链把生产者折进来（如常量折成立即数），再删掉已死的生产者。
3. **跨块折叠**：后继块先处理，特定 CFG 形状下跨块使用者也先于定义者出现（不要理解成任意跨块都能折）。

### 2.5.1 迭代器维护

`select()` 允许删除任意指令，包括"当前指令的前一条"，所以有 `MIIteratorMaintainer`：

```cpp
  void erasingInstr(MachineInstr &MI) override {
    if (MII.getInstrIterator().getNodePtr() == &MI) {
      // 继续从被删指令的前驱开始
      ++MII;
    }
  }
```

如果遍历器崩了，通常是某个选择器删了不该删的东西，而不是遍历逻辑本身有问题。

### 2.5.2 顺序留下的证据

`strict-align` 的失败信息是：

```text
LLVM ERROR: cannot select: RET_ReallyLR implicit $x0 (in function: strict)
```

被抱怨的是函数**最后一条**指令而不是最前面的 `COPY`。这正是 `rbegin()` 的结果：AArch64 对 strict-align 一律返回 false，遍历到的第一条（块内最后一条）立刻失败。反过来说，如果失败信息指向函数中部，说明源码顺序更靠后的指令都已选择成功。

---

## 2.6 `selectInstr` 的前置处理

```cpp
bool InstructionSelect::selectInstr(MachineInstr &MI) {
  MachineRegisterInfo &MRI = ISel->MF->getRegInfo();

  if (isTriviallyDead(MI, MRI)) {          // 已被折死
    salvageDebugInfo(MRI, MI);
    MI.eraseFromParent();
    return true;
  }

  if (isPreISelGenericOptimizationHint(MI.getOpcode()) ||
      MI.getOpcode() == TargetOpcode::G_CONSTANT_FOLD_BARRIER) {
    // 传播 regclass → 删除 → replaceRegWith(Dst, Src)
    return true;
  }

  if (MI.getOpcode() == TargetOpcode::G_INVOKE_REGION_START) {
    MI.eraseFromParent();
    return true;
  }

  return ISel->select(MI);
}
```

### 2.6.1 优化提示

`G_ASSERT_ZEXT` / `G_ASSERT_SEXT` 是"高位已知"的提示，本身不产生代码，必须在选择前消失。手写 MIR 验证：

```yaml
    %0:gpr(s64) = COPY $x0
    %1:gpr(s64) = G_ASSERT_ZEXT %0, 32
    $x0 = COPY %1(s64)
```

跑 `-run-pass=instruction-select` 后：

```yaml
    %0:gpr64all = COPY $x0
    $x0 = COPY %0
```

`G_ASSERT_ZEXT` 整条消失，`%1` 的使用被替换成 `%0`。源码里那段 regclass 传播是为了避免删掉提示后源寄存器丢掉本该有的寄存器类约束。

### 2.6.2 折叠后变死的生产者

`add i64 %a, 1` 在选择前：

```yaml
    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = G_CONSTANT i64 1
    %2:gpr(i64) = G_ADD %0, %1
```

选择后：

```yaml
    %0:gpr64sp = COPY $x0
    %2:gpr64sp = ADDXri %0, 1, 0
```

`G_CONSTANT` 被折进立即数，随后作为死指令删除。vreg 编号从 `%0` 跳到 `%2`，`%1` 永远消失，这是"何时被删"的间接证据。另外 `gpr64sp` 而非 `gpr64`，因为 `ADDXri` 允许 SP 操作数——选择器约束寄存器类的真实例子。

---

## 2.7 失败之后：`FailedISel` 与 fallback

任何一条指令选不出来都会走：

```cpp
reportGISelFailure(MF, MORE, "gisel-select", "cannot select", MI);
return false;
```

而 `reportGISelFailure` 的第一件事是设属性（`GlobalISel/Utils.cpp`）：

```cpp
void llvm::reportGISelFailure(MachineFunction &MF, ...) {
  MF.getProperties().setFailedISel();
  reportGISelDiagnostic(DS_Error, MF, MORE, R);
}
```

随后由 `TargetPassConfig.cpp` 根据 `-global-isel-abort` 决定：

| 取值 | 行为 | 场景 |
| --- | --- | --- |
| `0` | 静默 fallback 到 SelectionDAG | 生产默认 |
| `1` | 报错退出 | 移植时定位"哪条没选" |
| `2` | 继续编译，输出 remark/warning | 批量统计失败率 |

失败后 MIR 会同时带 `selected: true` 与 `failedISel: true`；`ResetMachineFunctionPass` 在退回 SelectionDAG 前把函数整个重置并清掉属性。

**实践含义**：看到 `failedISel: true` 就不要继续分析 MIR 里的 generic 指令为什么"没被选"，这份 MIR 已作废。先用 `-global-isel-abort=1` 拿到确切失败指令。

---

## 2.8 成功路径的收尾

`selectMachineFunction` 在返回前做四件事：

1. **清不可达块**。后序遍历不会访问从入口不可达的块，收尾时 `MBB.clear()`，但**不删块**——块地址可能被取过，或还有 PHI 引用。写 MIR 测试时会看到这些空块。
2. **合并冗余 COPY**。只在源/目的寄存器类完全相同且无子寄存器时 `replaceRegWith` + 删除；跨类 COPY 必须保留，寄存器分配还要用它做约束。
3. **校验 regclass 与大小**（2.4.2 节），遍历所有虚拟寄存器，不只是刚选过的。
4. **清 LLT**：

```cpp
  // 选择后不再需要 vreg 类型，MIRPrinter 除外
  MRI.clearVirtRegTypes();
```

---

## 2.9 契约违反速查表

| 症状 | 最可能原因 | 检查手段 |
| --- | --- | --- |
| `Unexpected generic instruction in a Selected function` | 返回 true 但留下 `G_*` | `-verify-machineinstrs` |
| `VReg has no regclass after selection` | 结果 vreg 没 `constrainRegClass` | assertions 构建看报错 MI |
| `VReg's low-level type and register class have different sizes` | 选错寄存器类 | 对比 LLT 位宽与 RC 大小 |
| `inserting blocks is not supported yet` | 选择器里拆了基本块 | 改成更早阶段 lowering |
| `cannot select: ...` | 无匹配 pattern 且无手写 fallback | `-global-isel-abort=1` |
| 属性检查 fatal error | pass 调度顺序错 | 看 Required/Current 两行 |
| `selected: true` + `failedISel: true` | pass 失败但属性无条件置位 | 以 `failedISel` 为准 |

---

## 进阶延伸

- 读 `MachineFunctionPass::runOnFunction` 和 `MachineVerifier` 的断言分支，区分 release 与 assertions build 的诊断差异。
- 构造一个 `failedISel` 函数，观察 `selected`、`failedISel`、verifier 与 SelectionDAG fallback 的交互。
- 思考：如果允许 InstructionSelect 插入基本块，哪些现有 postcondition 会失效？
## 踩雷清单

1. **把 `selected: true` 当成功标志**。必须同时看 `failedISel`，失败时两者会同时为 true。
2. **在 release 构建里依赖属性检查**。官方预编译包把 `verifyRequiredProperties` 编译掉了。
3. **以为 `select()` 返回 false 只是跳过当前指令**。它是整函数放弃，后面的指令都不会再被处理。
4. **在选择器里插入 `G_*` 指令**。哪怕只是临时的，只要函数结束时还在，postcondition 就破了。需要中间表示时用 target-specific 伪指令。
5. **忘了 `constrainRegClass`**。返回 true 但结果 vreg 没有 regclass，assertions 构建会 fallback，release 构建可能到寄存器分配才炸。

---

## 动手练习

### 练习 2.1 读属性

用 `-stop-after=irtranslator / legalizer / regbankselect / instruction-select` 导出同一函数的 MIR，列出 `legalized`、`regBankSelected`、`selected` 的取值，并指出每一步谁设置了哪个属性。

### 练习 2.2 制造一次契约违反

拿 `add-sel.mir`，把 `ADDXrr` 改回 `G_ADD`，然后：(a) 直接跑 `llc -run-pass=finalize-isel`；(b) 加上 `-verify-machineinstrs`；(c) 把 `selected: true` 改成 `false` 再跑。解释三次差异。

### 练习 2.3 观察死指令清理

构造 `add i64 %a, 1`，分别在 `regbankselect` 与 `instruction-select` 后导出 MIR，指出 `G_CONSTANT` 在哪一步消失，以及 vreg 编号为什么出现空洞。

### 练习 2.4 观察 COPY 合并

构造一个会产生同类别 COPY 的函数（如 `ret i64 %a`），对比选择前后的 COPY 数量，解释收尾阶段删掉了哪一条、为什么跨类 COPY 不能删。

---

## 重点整理

- 契约分三层：pass 属性、函数 postcondition、指令级 `select()`。
- 属性检查是 assertions-only 的 fatal error；release 构建里它是文档。
- `selected: true` 不等于成功，`failedISel: true` 优先级更高。
- 遍历是"后序块 + 块内逆序"（uses before defs），支撑即时 DCE 与生产者折叠。
- `selectInstr` 先处理死指令、优化提示、`G_CONSTANT_FOLD_BARRIER`、`G_INVOKE_REGION_START`，再交给 target。
- 成功后清不可达块、合并同类 COPY、校验 regclass 与大小、清 LLT。
- 失败即整函数 fallback，不是单条跳过。

---

## 自我检核

1. `getRequiredProperties()` 返回哪三个属性？分别由哪个 pass 设置？
2. 为什么 `verifyRequiredProperties` 在 release 构建里不起作用？对 CI 有什么影响？
3. 一份 MIR 同时有 `selected: true` 和 `failedISel: true`，发生了什么？
4. 为什么遍历要从块尾开始？举一个依赖这个顺序的优化。
5. 选择器插入新基本块会怎样？为什么？
6. 收尾为什么要 `clearVirtRegTypes()`？不做会有什么后果？

---

## 延伸阅读

- `llvm/include/llvm/CodeGen/GlobalISel/InstructionSelect.h`：契约的权威定义，只有几十行，值得逐字读。
- `llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`：本章引文出处，重点看 `selectMachineFunction` 与 `selectInstr`。
- `llvm/include/llvm/CodeGen/MachineFunction.h` 的 `MachineFunctionProperties`：属性枚举与注释，是 GlobalISel 流水线状态机的地图。
- `llvm/lib/CodeGen/MachineFunctionPass.cpp`：属性检查与 `SetProperties` 的实际执行点。
- `llvm/lib/CodeGen/MachineVerifier.cpp` 的 `verifyPreISelGenericInstruction`：postcondition 的执法者。
- `llvm/lib/CodeGen/GlobalISel/Utils.cpp` 的 `reportGISelFailure`：失败路径起点。
- `llvm/lib/CodeGen/TargetPassConfig.cpp`：`-global-isel-abort` 三种模式与 fallback 的组装。
- 官方文档：<https://llvm.org/docs/GlobalISel/InstructionSelect.html> —— 选择器 API 与 pipeline 的总览；读 `select()`/`selectImpl()` 契约时对照本章的 postcondition。

---

## 下一章

第 3 章回到流水线起点：**IRTranslator 与 gMIR**。看 LLVM IR 如何变成 `G_*` 指令、LLT 如何表示类型、`COPY` 与 `G_CONSTANT` 从哪来，以及为什么"读 gMIR"是调试选择失败的第一步。

[第 3 章 IRTranslator 与 gMIR](./03-irtranslator-gmir.md)
