# Ch 10 — repair 与 COPY 插入
> **目标**：理解 `RepairingPlacement` 如何决定 COPY 插在 def 之后、use 之前，还是 CFG 边上；`getRepairCost` / `repairReg` / `applyMapping` 如何协作；什么情况下 repair 是不可能的。最后能逐条解释 `or.ll` Fast 的 COPY 和 `mix.ll` 的 copy chain。
>
> **环境**：LLVM 23.1.4 官方预编译包（实测）+ 源码（源码依据）。`or.ll`、`mix.ll` 的 MIR 都在官方包上跑过；不可能 repair 的路径没有可复现的官方包实验，标为源码依据。
>
> **证据约定**：MIR 标为**实测**；插入点规则、`getRepairCost`、`repairReg`、`applyMapping` 标为**源码依据**；需要改源码/自定义 target 才能构造的 impossible 场景标为**guided**，不伪造输出。
## 为什么需要这个？
RegBankSelect 的输出里，最容易让人困惑的就是 COPY：

- 哪些 COPY 是 ABI 边界，哪些是 pass 插的？
- 为什么有的 COPY 在指令前面，有的在后面？
- 为什么 `or.ll` Fast 有两条 repair COPY，而 `mix.ll` 有两条但方向相反？
- 什么时候 pass 会说“我修不了”，然后 `unable to map instruction`？

这些问题的答案都在 repair 路径。Fast/Greedy 只决定“选哪个 mapping”；repair 决定“为了让现实符合 mapping，要搬什么、搬到哪里”。理解 repair，才能解释 COPY 数量、寄存器压力和 failed ISel。
## 先建立直觉
把 repair 想成搬家：

- **Reassign**：房子还没人住，直接把地址改成新 bank，免费。
- **Insert**：人已经住在旧 bank，要在新 bank 再放一份，插一条 COPY。
- **Edge insert**：两份房子在 CFG 的两条街之间，COPY 要放在“边”上；如果边是 critical edge，还得先修一条新路（split edge）。
- **Impossible**：两边根本不能搬（例如 GPR 放不下 512 位），或者边不能 split。
```text
use repair:        COPY 插在指令之前
def repair:        COPY 插在指令之后
PHI use repair:    插在 predecessor 的 terminator 之前；不行就 split edge
terminator def:    插在每条出边上（源码对多插入点有硬限制）
```
## 核心概念
### `RepairingKind`
`RegBankSelect.h`：
```cpp
enum RepairingKind {
  None,        // 不需要 repair
  Insert,      // 插入 COPY/merge/unmerge
  Reassign,    // 直接改 bank，不插指令
  Impossible   // 修不了
};
```
`RepairingPlacement` 保存：

- `Kind`、`OpIdx`；
- `CanMaterialize`；
- `HasSplit`；
- `InsertionPoints`（`SmallVector<unique_ptr<InsertPoint>, 2>`）；
- `Pass &P`（materialize 时更新分析）。
### 三种 `InsertPoint`
| 类 | 位置 | 频率 | 能否 split |
| --- | --- | --- | --- |
| `InstrInsertPoint` | 某条指令之前/之后 | 所在块频率 | 在 terminator 之后时需要 split |
| `MBBInsertPoint` | 块首/块尾 | 块频率 | 不 split |
| `EdgeInsertPoint` | `Src -> Dst` 的边上 | 边频率 | critical edge 需要 split |

`MBBInsertPoint` 的构造函数有两个断言：
```cpp
assert((!Beginning || MBB.getFirstNonPHI() == MBB.begin()) &&
       "Invalid beginning point");
assert((Beginning || MBB.getFirstTerminator() == MBB.end()) &&
       "Invalid end point");
```
意思是：不能在 PHI 之前插（要用 incoming edge 的插入点），也不能在 terminator 之后插（要用 outgoing edge 的插入点）。这就是为什么 PHI/terminator 需要专门的 placement 逻辑。
### `RepairingPlacement` 的构造规则
源码（简化）：
```cpp
RepairingPlacement(MachineInstr &MI, unsigned OpIdx,
                   const TargetRegisterInfo &TRI, Pass &P,
                   RepairingKind Kind) {
  const MachineOperand &MO = MI.getOperand(OpIdx);
  if (Kind != RepairingKind::Insert) return;

  bool Before = !MO.isDef();   // use 在 MI 前，def 在 MI 后

  if (!MI.isPHI() && !MI.isTerminator()) {
    addInsertPoint(MI, Before);
    return;
  }

  if (MI.isPHI()) {
    if (!Before) { /* def: 插到最后一个 PHI 之后 */ }
    else { /* use: 插到 predecessor 的 terminator 之前，必要时 split edge */ }
  } else {
    if (Before) { /* use: 插到第一个 terminator 之前 */ }
    else { /* def: 对每条出边 addInsertPoint(Src, Succ) */ }
  }
}
```
四条规则：

1. **普通指令的 use**：`InstrInsertPoint(MI, Before=true)`，COPY 在 MI 前。
2. **普通指令的 def**：`InstrInsertPoint(MI, Before=false)`，COPY 在 MI 后。
3. **PHI 的 use**：插入点在 predecessor 的 terminator 之前；如果 predecessor 的 terminator 会修改这个值，不能 hoist，改为 `EdgeInsertPoint(Pred, MBB)`，必要时 split。
4. **terminator 的 def**：对每条出边加 `EdgeInsertPoint(Src, Succ)`；源码注释明确说这依赖“物理寄存器可以有多条定义”，虚拟寄存器多定义会破坏 SSA。
### `assignmentMatch`：什么时候免费
```cpp
bool RegBankSelect::assignmentMatch(
    Register Reg, const RegisterBankInfo::ValueMapping &ValMapping,
    bool &OnlyAssign) const {
  OnlyAssign = false;
  if (ValMapping.NumBreakDowns != 1)
    return false;   // 多块分解一定需要 repair

  const RegisterBank *CurRegBank = RBI->getRegBank(Reg, *MRI, *TRI);
  const RegisterBank *DesiredRegBank = ValMapping.BreakDown[0].RegBank;
  OnlyAssign = CurRegBank == nullptr;
  return CurRegBank == DesiredRegBank;
}
```
三种结果：

| `CurRegBank` | `assignmentMatch` | `OnlyAssign` | 结果 |
| --- | --- | --- | --- |
| 等于 desired | true | false | 免费 |
| `nullptr` | false | true | `Reassign`，免费 |
| 不同 | false | false | `Insert`，可能插 COPY |

`Reassign` 只改 `MRI->setRegBank`，不插指令；这是“值还没 bank”的常见情况。
### `getRepairCost`
```cpp
uint64_t RegBankSelect::getRepairCost(
    const MachineOperand &MO,
    const RegisterBankInfo::ValueMapping &ValMapping) const {
  assert(MO.isReg() && "We should only repair register operand");
  assert(ValMapping.NumBreakDowns && "Nothing to map??");

  const RegisterBank *CurRegBank = RBI->getRegBank(MO.getReg(), *MRI, *TRI);
  assert(CurRegBank || MO.isDef());

  if (ValMapping.NumBreakDowns != 1)
    return RBI->getBreakDownCost(ValMapping, CurRegBank);

  const RegisterBank *DesiredRegBank = ValMapping.BreakDown[0].RegBank;
  if (MO.isDef())
    std::swap(CurRegBank, DesiredRegBank);

  unsigned Cost = RBI->copyCost(*DesiredRegBank, *CurRegBank,
                                RBI->getSizeInBits(MO.getReg(), *MRI, *TRI));
  return Cost != ImpossibleRepairCost ? Cost : ImpossibleRepairCost;
}
```
要点：

- use：`copyCost(Desired, Cur)`；
- def：交换后同样调用，所以方向反过来；
- 多块分解：走 `getBreakDownCost`，基类默认返回 `UINT_MAX`（不可能）；
- AArch64 没有覆写 `getBreakDownCost`，所以 AArch64 上多块 repair 基本不可用。
### `repairReg`：真正插指令
单块（`NumBreakDowns == 1`）：
```cpp
Register Src = MO.getReg();
Register Dst = *NewVRegs.begin();
if (MO.isDef())
  std::swap(Src, Dst);

MI = MIRBuilder.buildInstrNoInsert(TargetOpcode::COPY)
       .addDef(Dst)
       .addUse(Src);
```
源码用的是 `TargetOpcode::COPY`，在 gMIR 里打印为 `COPY`。23.1.4 里没有 `G_COPY` 这个 opcode；它也不是 target 的 `COPY` 伪指令。

多块：

- def：`G_MERGE_VALUES`（标量）、`G_BUILD_VECTOR`（向量且块数等于元素数）、`G_CONCAT_VECTORS`（其它向量）；
- use：`G_UNMERGE_VALUES`；
- 要求 `partsAllUniform()`，否则 assert；
- 最后 `if (RepairPt.getNumInsertPoints() != 1) report_fatal_error("need testcase to support multiple insertion points");`。

也就是说：**23.1.4 的 `repairReg` 实际上只支持一个插入点**。terminator def 如果产生多条出边，会走到这个 fatal error；报错信息本身就写着 `need testcase to support multiple insertion points`。不要在需要多出边 repair 的 target 上依赖它。
### `applyMapping`
```cpp
for (RepairingPlacement &RepairPt : RepairPts) {
  if (!RepairPt.canMaterialize() ||
      RepairPt.getKind() == RepairingPlacement::Impossible)
    return false;

  switch (RepairPt.getKind()) {
  case Reassign:
    MRI->setRegBank(Reg, *ValMapping.BreakDown[0].RegBank);
    break;
  case Insert:
    if (MI.isDebugInstr()) break;   // debug instr 不插
    if (!OpdMapper) OpdMapper.emplace(MI, InstrMapping, *MRI);
    OpdMapper->createVRegs(OpIdx);
    if (!repairReg(MO, ValMapping, RepairPt, OpdMapper->getVRegs(OpIdx)))
      return false;
    break;
  }
}

if (!OpdMapper && InstrMapping.getID() == DefaultMappingID)
  return true;

if (!OpdMapper) OpdMapper.emplace(MI, InstrMapping, *MRI);
RBI->applyMapping(MIRBuilder, *OpdMapper);
```
顺序是：**先放 repair，再重写指令**。repair 创建的新 vreg 由 `OperandsMapper` 记录，`RBI->applyMapping` 负责把 MI 的操作数换成新 vreg。如果 mapping 是默认且没有 repair，直接返回，不改指令。
## 底层机制
### 为什么新插入的 COPY 不会被再次 repair
`assignRegisterBanks` 在进入每个基本块时做一次快照：
```cpp
SmallVector<MachineInstr *> WorkList(
    make_pointer_range(reverse(MBB->instrs())));
while (!WorkList.empty()) {
  MachineInstr &MI = *WorkList.pop_back_val();
  ...
}
```
repair 插入的 COPY 不在这个快照里，所以本轮不会再遍历它。否则，一条跨 bank COPY 本身可能又需要 repair，导致递归。这个设计也意味着：repair COPY 的 bank 已经由 `repairReg` 直接设好（新 vreg 的 bank 是目标 bank），不需要再走一遍 mapping。
### def / use / edge 的放置
| 场景 | 插入点 | 源码依据 |
| --- | --- | --- |
| 普通指令 use | MI 之前 | `Before = !MO.isDef()` |
| 普通指令 def | MI 之后 | 同上 |
| PHI def | 最后一个 PHI 之后 | `getFirstNonPHI()` |
| PHI use | predecessor 的 terminator 之前 | 找 `getLastNonDebugInstr()` 并跳过 terminator |
| PHI use，值被 terminator 修改 | incoming edge 上 | `addInsertPoint(Pred, *MI.getParent())` |
| terminator use | 第一个 terminator 之前 | 反向跳过 terminator |
| terminator def | 每条出边 | `for (auto &Succ : Src.successors())` |
| critical edge | split 后新块 | `EdgeInsertPoint::materialize` → `SplitCriticalEdge` |

`EdgeInsertPoint::canMaterialize` 只对 critical edge 返回 `Src.canSplitCriticalEdge(Dst)`；非 critical edge 不应该走到这里（源码 assert `Src.succ_size() > 1 && Dst.pred_size() > 1`）。
### 什么时候 `tryAvoidingSplit` 能救回来
`computeMapping` 在 `RepairPt.hasSplit()` 时调用：
```cpp
if (MI.isPHI() && !MO.isDef() && ValMapping.NumBreakDowns == 1)
  RepairPt.switchTo(RepairingPlacement::RepairingKind::Reassign);
```
PHI 的 incoming value 本来就是一条边上的 COPY；如果只映射一个值，直接改 incoming 的 bank 即可，不需要 split。

terminator def 的情形更复杂：

- 物理寄存器：可以 split 每条出边，在边上 repair；
- 虚拟寄存器且单块：源码 `assert(false && "Repairing cost may not be accurate")`，release 下继续，但代价可能不准；
- 虚拟寄存器且多块：`switchTo(Impossible)`，因为要改所有 uses（包括已经处理过的 PHI）。
### impossible repair 的三条路
1. **`canMaterialize == false`**：critical edge 不能 split，`applyMapping` 直接 false。
2. **`RepairingKind::Impossible`**：`tryAvoidingSplit` 对“虚拟寄存器 + 多块 + terminator def”切换；或者 `findBestMapping` 在所有候选都 impossible 时手动塞一个。
3. **`getRepairCost` 返回 `ImpossibleRepairCost`**：多块走 `getBreakDownCost`（基类默认 max），或 `copyCost` 返回 max。

`findBestMapping` 的处理：
```cpp
if (!BestMapping && GlobalISelAbort != Enable) {
  BestMapping = *PossibleMappings.begin();
  RepairPts.emplace_back(
      RepairingPlacement(MI, 0, *TRI, *this, RepairingPlacement::Impossible));
} else
  assert(BestMapping && "No suitable mapping for instruction");
```
abort 关闭时，它选第一个 mapping + Impossible placement，让 `applyMapping` 失败，最终 `reportGISelFailure("unable to map instruction")`。abort 打开时，它期望至少有一个可行 mapping；全部 impossible 不是“静默降级”。
## 示例一：`or.ll` Fast 的两条 use repair
【实测】`or.ll` Fast：
```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %6:gpr(i32) = COPY %2(i32)
    %7:gpr(i32) = COPY %3(i32)
    %4:gpr(i32) = G_OR %6, %7
    %5:gpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
```
逐条分类：

| COPY | 类型 | 方向 | 说明 |
| --- | --- | --- | --- |
| `%0:fpr = COPY $s0` | ABI | `$s0` → vreg | 参数进入 vreg 世界 |
| `%1:fpr = COPY $s1` | ABI | `$s1` → vreg | 同上 |
| `%6:gpr = COPY %2` | repair（use） | FPR → GPR | 喂 `G_OR` 的 operand 1 |
| `%7:gpr = COPY %3` | repair（use） | FPR → GPR | 喂 `G_OR` 的 operand 2 |
| `$s0 = COPY %5` | ABI | vreg → `$s0` | 返回值边界 |

`G_OR` 的默认 mapping 是 GPR；`%2`/`%3` 已经是 FPR，`assignmentMatch` 返回 false、`OnlyAssign` false，于是 `RepairingPlacement` 在 `G_OR` 之前插 COPY。`%6`/`%7` 是 use repair，位置正确。

`$s0 = COPY %5` 不是 repair：`%5` 是 `G_BITCAST` 的结果，bank 由源 `%4` 决定（GPR）；返回物理寄存器 `$s0` 是 FPR，所以这条 COPY 是 ABI 边界。Ch 0 把它们都算进“3 条跨 bank COPY”，但 repair 只有 2 条。
## 示例二：`mix.ll` 的 copy chain
【实测】`mix.ll`：
```llvm
define i32 @mix(float %a, i32 %b) {
  %ai = bitcast float %a to i32
  %x = add i32 %ai, %b
  %y = bitcast i32 %x to float
  %z = fadd float %y, 1.0
  %w = bitcast float %z to i32
  ret i32 %w
}
```
Fast body：
```yaml
%0:fpr(f32) = COPY $s0
%1:gpr(i32) = COPY $w0
%2:fpr(i32) = G_BITCAST %0(f32)
%8:gpr(i32) = COPY %2(i32)
%3:gpr(i32) = G_ADD %8, %1
%4:gpr(f32) = G_BITCAST %3(i32)
%5:fpr(f32) = G_FCONSTANT float 1.000000e+00
%9:fpr(f32) = COPY %4(f32)
%6:fpr(f32) = G_FADD %9, %5
%7:fpr(i32) = G_BITCAST %6(f32)
$w0 = COPY %7(i32)
```
COPY 链：
```text
%2:fpr --COPY--> %8:gpr --G_ADD--> %3:gpr --G_BITCAST--> %4:gpr
                                                          │
                                                       COPY
                                                          ▼
%9:fpr --G_FADD--> %6:fpr --G_BITCAST--> %7:fpr --COPY--> $w0
```
- `%8`：`G_ADD` 的 use repair（FPR → GPR）；
- `%9`：`G_FADD` 的 use repair（GPR → FPR）；
- `$w0 = COPY %7`：ABI 返回边界（FPR → GPR）。

两条 repair 方向相反，中间隔着 `G_ADD` / `G_BITCAST` / `G_FCONSTANT` / `G_FADD`，形成一条“跨 bank 往返”的 copy chain。Greedy 在这个例子里输出相同，因为 `G_ADD` / `G_FADD` 都没有 alternatives，选不了别的 bank。
## 示例三：impossible repair 的源码路径
【源码依据】以下场景没有在官方包上构造出可复现 MIR，只按源码说明：

**场景 A：多块 ValueMapping + AArch64**

`getRepairCost` 对 `NumBreakDowns != 1` 调用 `getBreakDownCost`。AArch64 没有覆写，基类返回 `UINT_MAX`；`computeMapping` 立即返回 `ImpossibleCost`。所以 AArch64 的表里不要引入多块 `ValueMapping`，否则 repair 直接不可行。

**场景 B：critical edge 不能 split**

`EdgeInsertPoint::canMaterialize` 返回 `Src.canSplitCriticalEdge(Dst)`。如果目标架构/IR 让这条边不能 split，`RepairPt.canMaterialize()` 为 false，`applyMapping` 返回 false，最终 `unable to map instruction`。

**场景 C：虚拟寄存器 + 多块 + terminator def**

`tryAvoidingSplit`：
```cpp
if (Reg.isPhysical()) {
  // split 每条出边
} else if (ValMapping.NumBreakDowns == 1) {
  assert(false && "Repairing cost may not be accurate");
} else {
  RepairPt.switchTo(RepairingPlacement::RepairingKind::Impossible);
}
```
虚拟寄存器如果被拆成多块，又由 terminator 定义，无法只改当前块；所有 uses（包括已经处理过的 PHI）都要改，所以直接标 Impossible。

**场景 D：多条插入点**

`repairReg` 末尾：
```cpp
if (RepairPt.getNumInsertPoints() != 1)
  report_fatal_error("need testcase to support multiple insertion points");
```
这不是 failed ISel，而是编译器直接 fatal error。23.1.4 里不要依赖“terminator def 自动在每条出边插 COPY”的完整支持。
## 对比与取舍
| 方案 | 何时用 | 代价 | 风险 |
| --- | --- | --- | --- |
| Reassign | 值还没有 bank | 0 | 不能用于已有不同 bank |
| 同块 Insert | use/def 与 mapping 不同 | `copyCost` | 可能增加寄存器压力 |
| Edge Insert | PHI incoming / terminator 出边 | `copyCost` × 边频率 + 5% bias | 可能 split CFG，增加块数 |
| Impossible | 无法本地修复 | — | failed ISel 或 fatal error |

repair 的目标是“局部正确”，不是“全局最优”。它会在需要的地方插 COPY，但不保证 COPY 最终不被寄存器分配器 coalesce 掉；MIR 里的 COPY 数量不等于最终汇编里的 MOV 数量。
## 踩雷集锦
### 1. 把 ABI COPY 当成 repair COPY
错：
```text
or.ll Fast 有 3 条 repair COPY
```
对。repair 只有 `%6`、`%7` 两条；`$s0 = COPY %5` 是 ABI 返回边界。判断方法：看它连接的是物理寄存器还是两个 vreg；ABI COPY 通常一边是 `$x0`/`$s0` 这类物理寄存器。
### 2. 以为 def repair 也插在指令之前
错：
```text
所有 repair COPY 都在 MI 前面
```
对。`Before = !MO.isDef()`：use 在前，def 在后。def repair 通常出现在 def 已经被设过不同 bank 的情况（例如 target-specific 指令先给了约束，或更早的处理已经设过 bank）；本章的实测例子都是 use repair。
### 3. 以为 `Reassign` 也会插 COPY
错：
```text
assignmentMatch 返回 false 就一定插 COPY
```
对。如果 `OnlyAssign == true`（寄存器还没有 bank），`RepairingPlacement` 是 `Reassign`，只调用 `MRI->setRegBank`，不插指令。只有已有不同 bank 才 `Insert`。
### 4. 忽略 `repairReg` 的多插入点限制
错：
```text
terminator def 会自动在每条出边插 COPY
```
对。源码在 `RepairPt.getNumInsertPoints() != 1` 时 `report_fatal_error`。23.1.4 的实现只支持单插入点；多出边场景需要额外的 target 支持或后续版本。
### 5. 以为 MIR 里的 COPY 都会保留到汇编
错：
```text
or.ll Fast 有 3 条 COPY，所以最终一定有 3 条 MOV
```
对。repair COPY 可能被寄存器分配器 coalesce，也可能因为 bank 相同被消掉；MIR 数量只说明 RegBankSelect 阶段插了多少。要判断最终成本，继续跑 `-stop-after=regalloc` 或看汇编。
## 进阶
- **读 `EdgeInsertPoint::materialize`**：它调用 `SplitCriticalEdge`，把 `DstOrSplit` 换成新块；`getPointImpl` 断言新块只有一个前驱和一个后继。理解这个断言，才能解释为什么 critical edge 的 split 是“安全”的。
- **读 `MBBInsertPoint` 的两个断言**：它们把“PHI 之前/terminator 之后”的非法插入点挡在构造期，避免 materialize 时才发现。
- **读 `repairReg` 的多块分支**：`G_BUILD_VECTOR` / `G_CONCAT_VECTORS` / `G_MERGE_VALUES` / `G_UNMERGE_VALUES` 的选择依赖类型和块数；这是未来支持多块 mapping 的基础。
- **看寄存器分配后的 COPY**：用 `-stop-after=regalloc` 或 `llc` 汇编，比较 RegBankSelect 阶段的 COPY 数量；coalescing 会显著改变最终 MOV 数量。
## 动手练习
1. 【实测】跑 `or.ll` Fast，给每条 COPY 标注“ABI / repair / 其它”，并说明 repair 的插入点（use 前还是 def 后）。
2. 【实测】跑 `mix.ll` Fast 和 Greedy，数出两条 repair COPY 和一条 ABI COPY；解释为什么 Greedy 没有消除它们。
3. 【源码依据】在 `RepairingPlacement` 构造函数里找到 PHI use 的分支，说明什么情况下会 split incoming edge。
4. 【guided】自己构建 assertions 版本，构造一个“虚拟寄存器 + 多块 ValueMapping + terminator def”的 MIR，观察 `tryAvoidingSplit` 如何切到 `Impossible`；官方包不能稳定复现，不要伪造输出。
## 本章重点整理
- repair 有三种结果：`Reassign`（免费改 bank）、`Insert`（插 COPY/merge/unmerge）、`Impossible`。
- use repair 在 MI 前，def repair 在 MI 后；PHI use 在 predecessor 的 terminator 前，必要时 split edge；terminator def 在出边上。
- `assignmentMatch` 决定是否免费；`OnlyAssign` 为 true 时走 `Reassign`。
- `getRepairCost` 对 use 用 `copyCost(Desired, Cur)`，对 def 交换方向。
- `repairReg` 单块插 `COPY`；多块用 merge/unmerge；多插入点会 `report_fatal_error`。
- `or.ll` Fast 有 2 条 repair + 1 条 ABI；`mix.ll` 有 2 条方向相反的 repair + 1 条 ABI。
- repair COPY 可能被后续 coalesce，MIR 数量不等于最终 MOV 数量。
## 自我检核
- [ ] 能区分 `Reassign`、`Insert`、`Impossible` 三种 repair
- [ ] 能说出 use/def/PHI/terminator 四类插入点
- [ ] 能解释 `assignmentMatch` 的 `OnlyAssign` 为什么免费
- [ ] 能说出 `getRepairCost` 对 use/def 的方向差异
- [ ] 能解释 `repairReg` 为什么对多插入点 fatal error
- [ ] 能逐条标注 `or.ll` Fast 的 COPY
## 延伸阅读
### 源码
- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：`InsertPoint`、`RepairingPlacement`、`repairReg` / `getRepairCost` 的声明与注释。
  - **学什么**：repair 的类型系统和插入点抽象。
  - **和本章的关联**：本章所有 placement 规则的来源。
- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`assignmentMatch`、`repairReg`、`getRepairCost`、`computeMapping`、`applyMapping`、`RepairingPlacement` 构造函数、`tryAvoidingSplit`。
  - **学什么**：repair 的完整执行路径和 impossible 分支。
  - **和本章的关联**：本章的主线源码。
- **[RegisterBankInfo.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/RegisterBankInfo.h)**
  - **读哪里**：`OperandsMapper`、`getBreakDownCost`、`copyCost`。
  - **学什么**：repair 如何创建新 vreg、如何计算多块代价。
  - **和本章的关联**：解释 `repairReg` 的 `NewVRegs` 和 `OperandsMapper`。
### 文档
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：repair / COPY 的概述。
  - **学什么**：官方文档如何描述“bank 不匹配时插入 copy”。
  - **和本章的关联**：先看概念，再看源码的插入点细节。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`COPY`、`liveins`、基本块和 successor 的写法。
  - **学什么**：如何在 MIR 里识别 ABI COPY 和 repair COPY。
  - **和本章的关联**：本章的 COPY 分类依赖 MIR 阅读能力。

Part 1 到此结束。练习 B 用你已经掌握的 Fast/Greedy 与 repair 机制，改一处 mapping 并预测结果，把「读代码」变成「能算账」。

→ [练习 B：改一个 mapping，比较 Fast/Greedy](./practice-b-mapping-and-cost.md)
