# Ch 9 — Greedy mode：alternative mappings 与局部代价
> **目标**：理解 Greedy 如何把“默认 mapping + alternative mappings”放进 `MappingCost` 比较；分清 `LocalCost` / `NonLocalCost` 和块频率；解释 AArch64 为什么只给 `G_OR` / `G_BITCAST` / `G_LOAD` 提供 alternatives；用 `or.ll` 算出 Fast 3 条跨 bank COPY、Greedy 0 条的原因。
>
> **环境**：LLVM 23.1.4 官方预编译包（实测）；源码 `.scratch/llvm-23.1.4-src`（源码依据）。
>
> **证据约定**：MIR 与哈希标为**实测**；`MappingCost` 公式、alternative 表、索引/代价数字标为**源码依据**；“为什么局部贪心可能次优”的解释标为**guided**。
## 为什么需要这个？
Fast 只拿默认 mapping，所以 `or.ll` 里 `G_OR` 被放在 GPR，前后各插一条 FPR→GPR 的 COPY。Greedy 看到同样的指令，却能把 `G_OR` 留在 FPR，repair COPY 变成 0。差异不是“Greedy 更聪明地猜”，而是：

1. AArch64 为 `G_OR` 提供了 FPR alternative mapping；
2. Greedy 计算了“指令在 GPR + 两条搬运”与“指令在 FPR + 零条搬运”的局部代价；
3. 后者更小，于是选 FPR。

这一章把这套代价模型拆开。理解之后，你能判断一个 opcode 在 Greedy 下**有没有可能**改变 bank，而不是盲目加 `-regbankselect-greedy`。
## 先建立直觉
把 Greedy 想成在超市里买一顿饭：

- 默认 mapping 是“超市门口最近的货架”，Fast 直接拿；
- alternative mappings 是“别的货架也有同款，但要走过去”；
- `MappingCost` 是“商品价格 + 走过去的路费”；
- Greedy 只比较**当前这一件商品**的局部总价，不会重新规划整张购物清单。
```text
G_OR 在 GPR:
  指令价 1
  + 操作数 1 从 FPR 搬来 5
  + 操作数 2 从 FPR 搬来 5
  = 11

G_OR 在 FPR:
  指令价 1
  + 操作数 1 已在 FPR 0
  + 操作数 2 已在 FPR 0
  = 1
```
Greedy 选 1，于是 `G_OR` 留在 FPR。注意这里的 5 是 `copyCost(GPR, FPR)` 的返回值；方向语义见下面的“代价方向”一节。
## 核心概念
### 候选 mapping 从哪来
`RegisterBankInfo::getInstrPossibleMappings`：
```cpp
InstructionMappings PossibleMappings;
const auto &Mapping = getInstrMapping(MI);
if (Mapping.isValid())
  PossibleMappings.push_back(&Mapping);   // 默认放第一个

InstructionMappings AltMappings = getInstrAlternativeMappings(MI);
append_range(PossibleMappings, AltMappings);

for (const InstructionMapping *Mapping : PossibleMappings)
  assert(Mapping->verify(MI) && "Mapping is invalid");
return PossibleMappings;
```
顺序很重要：**默认 mapping 永远排第一**。`findBestMapping` 用严格小于比较：
```cpp
if (CurCost < Cost) {
  Cost = CurCost;
  BestMapping = CurMapping;
  ...
}
```
所以代价相同时，先出现的默认 mapping 胜出。Greedy 不会因为“alternative 看起来更酷”而换 bank；必须严格更便宜。
### `MappingCost` 的三个字段
`RegBankSelect.h`：
```cpp
class MappingCost {
  uint64_t LocalCost = 0;      // 本地指令代价，不含块频率
  uint64_t NonLocalCost = 0;   // 非本地代价，已含频率
  uint64_t LocalFreq;          // 本地指令所在块的频率
};
```
- `LocalCost`：指令本身 + 同一块内 repair 的代价，**未乘频率**；
- `NonLocalCost`：需要 split 的边/块上的 repair 代价，**已乘频率**；
- `LocalFreq`：本地块频率，用于在比较时把 `LocalCost` 缩放到同一量纲。

`MappingCost::print` 的格式是：
```cpp
OS << LocalFreq << " * " << LocalCost << " + " << NonLocalCost;
```
也就是 `LocalFreq * LocalCost + NonLocalCost`。如果只看单块、频率为 1，就是 `LocalCost + NonLocalCost`。
### 头文件里的代价公式
`RegBankSelect.h` 的文件头注释给出：
```text
cost(I, RegBank) = cost(I.Opcode, RegBank)
                 + sum(for each arg in I.arguments:
                         costCrossCopy(arg.RegBank, RegBank))
```
对应到代码：

- `cost(I.Opcode, RegBank)` 是 `InstructionMapping::getCost()`；
- `costCrossCopy` 是 `getRepairCost`，最终落到 target 的 `copyCost`；
- 只有需要 repair 的操作数才加；已经匹配或只需 reassign 的操作数是 0。
### `computeMapping` 的流程
```cpp
MappingCost Cost(MBFI ? MBFI->getBlockFreq(MI.getParent())
                      : BlockFrequency(1));
bool Saturated = Cost.addLocalCost(InstrMapping.getCost());

for (每个 operand) {
  if (!MO.isReg()) continue;
  if (assignmentMatch(Reg, ValMapping, Assign)) continue; // 已匹配，0
  if (Assign) { RepairPts.emplace_back(..., Reassign); continue; } // 只赋值，0

  RepairPts.emplace_back(..., Insert);
  if (RepairPt.hasSplit()) tryAvoidingSplit(...);
  if (!RepairPt.canMaterialize()) return ImpossibleCost();

  if (!BestCost || Saturated) continue;  // Fast 不累加
  uint64_t RepairCost = getRepairCost(MO, ValMapping);
  if (RepairCost == ImpossibleRepairCost) return ImpossibleCost();

  const uint64_t Bias = (RepairCost * 5 + 99) / 100; // split 偏置 5%
  for (每个 insertion point) {
    if (!InsertPt->isSplit())
      Saturated = Cost.addLocalCost(RepairCost);
    else
      Saturated = Cost.addNonLocalCost(InsertPt->frequency(*this) *
                                       (RepairCost + Bias));
  }
  if (BestCost && Cost > *BestCost)
    return Cost;   // 已经比当前最优贵，提前退出
}
```
三个要点：

- `assignmentMatch` 返回 true 时完全免费；
- `OnlyAssign == true` 表示“寄存器还没 bank，只需设 bank”，也是 0，不插 COPY；
- `hasSplit()` 的 repair 会加 5% 偏置，因为 split 本身有额外成本/复杂度。
### `LocalCost` vs `NonLocalCost`
| 情况 | 计入 | 是否乘频率 |
| --- | --- | --- |
| 指令本身 | `LocalCost` | 比较时乘 `LocalFreq` |
| 同一块内的 use/def repair | `LocalCost` | 比较时乘 `LocalFreq` |
| 需要 split 的 PHI incoming / terminator 出边 repair | `NonLocalCost` | 计算时已乘边频率 |
| split 出来的新块 | `NonLocalCost` | 边频率 × `(repair + 5% bias)` |

为什么分开？源码注释说得很清楚：大多数 repair 是局部的，可以省略块频率；但 PHI 等可能产生非局部代价，需要按频率缩放。如果一律乘频率，容易把代价饱和，反而让不同 alternative 看起来一样贵。
### `MappingCost::operator<` 的比较策略
完整实现比较长，核心是：

1. 相等直接返回 false；
2. `ImpossibleCost` 最贵；
3. saturated 次贵；
4. 如果 `LocalFreq` 相同，先把 `LocalCost` 的差值按同一频率缩放，再与非本地差值比较；`NonLocalCost` 相同时直接比较 `LocalCost`；
5. 如果频率不同，把 `LocalCost * LocalFreq + NonLocalCost` 缩放后比较；
6. 溢出时，谁溢出谁更贵；两边都溢出则返回 false（无法比较）。

这解释了为什么 `MappingCost` 不直接存一个 `uint64_t total`：它要保留“本地未缩放”和“非本地已缩放”两部分，以便在不同块频率下比较，同时尽量减少溢出。
## 底层机制
### `findBestMapping`：严格最小 + 提前退出
```cpp
const InstructionMapping *BestMapping = nullptr;
MappingCost Cost = MappingCost::ImpossibleCost();
SmallVector<RepairingPlacement, 4> LocalRepairPts;

for (const InstructionMapping *CurMapping : PossibleMappings) {
  MappingCost CurCost = computeMapping(MI, *CurMapping, LocalRepairPts, &Cost);
  if (CurCost < Cost) {
    Cost = CurCost;
    BestMapping = CurMapping;
    RepairPts.clear();
    for (RepairingPlacement &RepairPt : LocalRepairPts)
      RepairPts.emplace_back(std::move(RepairPt));
  }
}
```
`computeMapping` 的第四个参数 `&Cost` 是“当前最优”；一旦某个候选的累计代价已经超过它，就提前返回，不再算完所有 insertion point。这是 Greedy 控制编译时间的手段之一。

如果所有候选都 impossible：
```cpp
if (!BestMapping && GlobalISelAbort != Enable) {
  BestMapping = *PossibleMappings.begin();
  RepairPts.emplace_back(
      RepairingPlacement(MI, 0, *TRI, *this, RepairingPlacement::Impossible));
} else
  assert(BestMapping && "No suitable mapping for instruction");
```
abort 关闭时，它选第一个 mapping 并塞一个 `Impossible` repair point，让后续 `applyMapping` 失败，从而触发 failed ISel。abort 打开时，它期望至少有一个可行 mapping。
### 局部贪心为什么不是全局最优
`assignRegisterBanks` 用 RPOT 遍历：
```cpp
ReversePostOrderTraversal<MachineFunction*> RPOT(&MF);
for (MachineBasicBlock *MBB : RPOT) {
  for (MachineInstr &MI : ...) {
    if (!assignInstr(MI)) ...
  }
}
```
每条指令只根据**当前已经分配的 bank** 做决定，不回看、不回改。可能的次优情形：

- 前面一条指令选了 GPR，后面一条指令的 alternative 因此变贵；
- PHI 的另一边还没处理，局部看不到跨块代价；
- 为了省一条 COPY 选了 FPR，但导致后续更多 GPR 指令各加一条 COPY。

`RegBankSelect.h` 的文件头也承认这是局部算法：

> By default, the reg bank selector relies on local decisions... At higher optimization level, we could imagine that the reg bank selector would use more global analysis and do crazier thing like duplicating instructions and so on. This is future work.

所以 Greedy 的名字是“局部贪心”，不是“全局最优”。
### AArch64 的 alternatives 只有三类
`AArch64RegisterBankInfo::getInstrAlternativeMappings`：
```cpp
switch (MI.getOpcode()) {
case TargetOpcode::G_OR: {
  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 32 && Size != 64) break;
  if (MI.getNumOperands() != 3) break;
  // GPRMapping: ID 1, Cost 1, getValueMapping(PMI_FirstGPR, Size), 3
  // FPRMapping: ID 2, Cost 1, getValueMapping(PMI_FirstFPR, Size), 3
  return {&GPRMapping, &FPRMapping};
}
case TargetOpcode::G_BITCAST: {
  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 32 && Size != 64) break;
  if (MI.getNumOperands() != 2) break;
  // GPRMapping ID1, FPRMapping ID2,
  // GPRToFPRMapping ID3, FPRToGPRMapping ID3
  return {...};
}
case TargetOpcode::G_LOAD: {
  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 64) break;
  if (MI.getNumOperands() != 2) break;
  // GPRMapping ID1, FPRMapping ID2; 地址固定 GPR64
  return {...};
}
default:
  break;
}
return RegisterBankInfo::getInstrAlternativeMappings(MI); // 空
```
三个限制条件很容易踩：

- **opcode 限制**：只有这三类；`G_AND` / `G_XOR` 没有 alternatives。
- **大小限制**：`G_OR`/`G_BITCAST` 只支持 32/64，`G_LOAD` 只支持 64。
- **操作数个数限制**：`G_OR` 必须恰好 3 个，`G_BITCAST`/`G_LOAD` 必须恰好 2 个；带 implicit-defs/uses 的指令会被跳过。
### 代价方向：`copyCost(A, B)` 是 `A = COPY B`
`RegisterBankInfo.h` 的注释：

> Get the cost of a copy from \p B to \p A, or put differently, get the cost of A = COPY B.

AArch64：
```cpp
if (&A == &GPRRegBank && &B == &FPRRegBank) return 5;
if (&A == &FPRRegBank && &B == &GPRRegBank) return 4;
return RegisterBankInfo::copyCost(A, B, Size);
```
所以：

| 调用 | 语义 | 返回值 |
| --- | --- | --- |
| `copyCost(GPR, FPR)` | `GPR = COPY FPR` | 5 |
| `copyCost(FPR, GPR)` | `FPR = COPY GPR` | 4 |

`getRepairCost` 对 use 调用 `copyCost(Desired, Cur)`；对 def 会交换 Cur/Desired。不要只背“GPR 5 / FPR 4”，要看参数顺序和方向。
## 示例一：`or.ll` 的 Fast 3 条 vs Greedy 0 条
【实测】`or.ll`，`-O2 -global-isel -stop-after=regbankselect`。Fast：
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
Greedy：
```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %4:fpr(i32) = G_OR %2, %3
    %5:fpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
```
数 COPY：

| 模式 | `G_OR` bank | RegBankSelect 插入的 repair COPY | 跨 bank COPY（含 ABI） |
| --- | --- | --- | --- |
| Fast | `gpr` | 2 条：`%6`、`%7`（FPR→GPR） | 3 条（再加 `$s0 = COPY %5`） |
| Greedy | `fpr` | 0 条 | 0 条 |

Ch 0 的“Fast 3 条 / Greedy 0 条”用的是**跨 bank COPY（含 ABI 返回）**口径；如果只数 RegBankSelect 的 repair，Fast 是 2 条。两个口径都成立，但写测试/报告时要说明你数的是哪一种。
### 代价算术
Greedy 对 `G_OR` 的两个候选：
```text
GPR mapping（默认，ID=DefaultMappingID）:
  instr cost = 1
  operand %2: FPR→GPR, copyCost(GPR, FPR) = 5
  operand %3: FPR→GPR, copyCost(GPR, FPR) = 5
  总 local cost = 11

FPR mapping（alternative，ID=2）:
  instr cost = 1
  operand %2: 已在 FPR, 0
  operand %3: 已在 FPR, 0
  总 local cost = 1
```
`11 > 1`，所以选 FPR。这个 11 是 Greedy 才会算的；Fast 直接拿默认 GPR，不比较。
### 为什么 `G_BITCAST` 不跟着搬
`%2`/`%3` 的默认 mapping 是 `G_BITCAST` 源 bank（FPR）→ cost 0；`%5` 在 `%4` 变成 FPR 后也是 FPR→cost 0。所以整条链没有额外的 bitcast 搬运。Greedy 不需要为 bitcast 做额外选择；它的默认 mapping 已经跟着源 bank 走。
## 示例二：`and.ll` 没有 alternatives，Greedy 不变
【实测】`and.ll`：
```llvm
define i32 @and_i(i32 %a, i32 %b) {
  %r = and i32 %a, %b
  ret i32 %r
}
```
Fast 与 Greedy 的 MIR SHA-256：
```text
and-fast.mir   4DD0087F950674ADDB4B88D28D6B16B9ECCA299111AA05D0585229BE9135062A
and-greedy.mir 4DD0087F950674ADDB4B88D28D6B16B9ECCA299111AA05D0585229BE9135062A
```
哈希口径同 Ch 8：包含 `ModuleID` / `source_filename` 里的输入路径，换目录不可直接比较；这里两边是同一路径下的两次运行。
相同。body 都是：
```yaml
%0:gpr(i32) = COPY $w0
%1:gpr(i32) = COPY $w1
%2:gpr(i32) = G_AND %0, %1
$w0 = COPY %2(i32)
```
原因：AArch64 的 `getInstrAlternativeMappings` 没有 `G_AND` 分支，基类返回空。Greedy 只有默认 GPR mapping 可选，所以和 Fast 一样。

这条实验很重要：它证明“Greedy 没变化”不一定是 bug，可能只是 target 没给 alternatives。
## 示例三：`G_LOAD` / `G_BITCAST` 的 alternatives
【源码依据】`G_LOAD` 64 位、2 操作数时，AArch64 返回：
```text
GPR mapping: {dest GPR64, addr GPR64}, ID 1, Cost 1
FPR mapping: {dest FPR64, addr GPR64}, ID 2, Cost 1
```
地址永远是 GPR64；只有 dest 可以在 GPR/FPR 之间选。Greedy 会根据 dest 的用途（例如后面是否接 FP 指令）和已有 bank 算 repair 代价。

`G_BITCAST` 返回四个 mapping：
```text
GPRMapping      ID 1  cost 1   GPR -> GPR
FPRMapping      ID 2  cost 1   FPR -> FPR
GPRToFPRMapping ID 3  cost 5   FPR = COPY GPR
FPRToGPRMapping ID 3  cost 5   GPR = COPY FPR
```
注意 `GPRToFPRMapping` / `FPRToGPRMapping` 都用了 ID 3，且源码两处都调用 `copyCost(GPR, FPR)`（返回 5）；`applyMappingImpl` 只检查 ID 在 1–4 之间。ID 表示 apply 逻辑类别，不唯一标识 mapping（Ch 5 的踩雷 5）。

`G_BITCAST` 的默认 mapping 通常已经是源 bank、cost 0，所以 Greedy 很少会选跨 bank alternative；除非源 bank 未知或后续 use 强烈偏好另一个 bank。
## 对比与取舍
| 模式 | 候选数 | 代价来源 | 优点 | 局限 |
| --- | --- | --- | --- | --- |
| Fast | 1 | 指令 cost | 快、可预测 | 不利用 alternatives |
| Greedy | 1 + N | 指令 + repair | 局部更优 | 仍非全局最优；依赖 target 提供 alternatives |
| 全局搜索（未实现） | 指数级 | 全局 | 理论更优 | 编译时间不可接受；源码明确说 future work |

AArch64 的 alternatives 很克制：只给三个 opcode。这个“克制”不是遗漏，而是权衡：每多一个 alternative，Greedy 就多一次 `computeMapping`，多一次 repair 分析；对大多数 opcode，默认 mapping 已经足够好。
## 踩雷集锦
### 1. 以为 Greedy 总会把浮点链放到 FPR
错：
```text
Greedy 看到 float 就选 FPR
```
对。Greedy 只看候选 mapping 的代价。`G_AND` 没有 FPR alternative，所以即使输入是 float bitcast 来的 i32，也仍然在 GPR。`and.ll` 的哈希相同就是证据。
### 2. 把 ABI COPY 算进 repair
错：
```text
or.ll Fast 有 3 条 repair COPY
```
对。Fast 的 repair 是 `%6`、`%7` 两条；`$s0 = COPY %5` 是 ABI 返回边界。若按“跨 bank COPY（含 ABI）”口径，总共 3 条；写测试时要说清楚口径。
### 3. 只背“GPR 5 / FPR 4”，不看参数顺序
错：
```cpp
// 以为 copyCost(GPR, FPR) 表示 GPR -> FPR
```
对。`copyCost(A, B)` 的语义是 `A = COPY B`；`copyCost(GPR, FPR)=5` 表示 `GPR = COPY FPR`。`getRepairCost` 对 use 用 `copyCost(Desired, Cur)`，方向会随 use/def 交换。
### 4. 给带 implicit 操作数的指令期待 alternatives
错：
```text
G_OR 只要 opcode 对，Greedy 就能选 FPR
```
对。AArch64 的 `G_OR` 分支要求 `MI.getNumOperands() == 3`；带 implicit-defs/uses 会直接 `break`，返回空 alternatives。`G_BITCAST`/`G_LOAD` 同理，分别要求 2 个操作数。
### 5. 以为 Greedy 会回改前面的指令
错：
```text
Greedy 发现后面更便宜，会把前面已经选的 bank 改掉
```
对。`assignRegisterBanks` 按 RPOT 单向前进，`assignInstr` 只处理当前指令；没有回溯，也没有全局重写。局部贪心的次优是设计的一部分。
## 进阶
- **读 `MappingCost::operator<` 的溢出分支**：当两个代价都溢出时，它返回 false（无法比较），而不是随便选一个；这会影响极端大函数的候选顺序。
- **读 `tryAvoidingSplit`**：PHI use 如果只映射一个值，可以改成 `Reassign`，避免 split；这是 Greedy 在 CFG 边上的一个重要优化。
- **读 `computeMapping` 的 `Bias`**：split 的 repair 会加 5% 偏置，避免 Greedy 为了省一点点代价频繁切边。
- **看 `-regbankselect-greedy` 的编译时间**：在大型函数上，每个候选都要走一遍操作数和 repair 分析；这就是为什么 `optnone` 要强制 Fast。
## 动手练习
1. 【实测】把 `or.ll` 的 `or` 换成 `and`、`xor`，分别跑 Fast/Greedy，比较哈希。解释为什么 Greedy 不再改变 bank。
2. 【实测】把 `or.ll` 的 `i32` 改成 `i64`，重复 Fast/Greedy；`G_OR` 仍然有 alternatives 吗？结果是否相同？
3. 【源码依据】用 Ch 7 的索引公式，写出 `G_OR i32` 的 GPR/FPR alternative 分别指向哪个 `ValMappingIdx`。
4. 【guided】在 `AArch64RegisterBankInfo.cpp` 里给 `G_AND` 临时加一个 FPR alternative，重新编译并跑 `or.ll` 的变体；观察 Greedy 是否改变。官方包不能做这个实验，不要伪造输出。
## 本章重点整理
- Greedy 的候选 = 默认 mapping（第一个）+ `getInstrAlternativeMappings`。
- `findBestMapping` 用严格 `<`；代价相同时默认 mapping 胜出。
- `MappingCost` = `LocalCost`（未乘频率）+ `NonLocalCost`（已乘频率），比较时按 `LocalFreq` 缩放。
- 头文件公式：指令代价 + 每个操作数的跨 bank copy 代价。
- AArch64 alternatives 只有 `G_OR`（32/64，3 操作数）、`G_BITCAST`（32/64，2 操作数）、`G_LOAD`（64，2 操作数）。
- `or.ll`：Fast 默认 GPR，2 条 repair + 1 条 ABI = 3 条跨 bank；Greedy 选 FPR，0 条。
- `and.ll`：没有 alternatives，Fast/Greedy 逐字节相同。
- Greedy 是局部贪心，不回溯、不全局搜索。
## 自我检核
- [ ] 能说出 `getInstrPossibleMappings` 的返回顺序
- [ ] 能解释 `MappingCost` 为什么分 `LocalCost` / `NonLocalCost`
- [ ] 能算出 `or.ll` 中 `G_OR` 在 GPR/FPR 两种 mapping 下的代价
- [ ] 能说出 AArch64 只对哪三个 opcode 提供 alternatives，以及各自的操作数个数限制
- [ ] 能解释 `and.ll` 为什么 Greedy 不变
- [ ] 能区分“repair COPY”和“ABI COPY”的计数口径
## 延伸阅读
### 源码
- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：文件头的代价公式、`MappingCost` 类、`findBestMapping` / `computeMapping` 的注释。
  - **学什么**：局部贪心的设计意图和代价模型。
  - **和本章的关联**：本章公式和字段的直接来源。
- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`computeMapping`、`findBestMapping`、`MappingCost::operator<`、`assignRegisterBanks`。
  - **学什么**：候选比较、提前退出、RPOT 遍历。
  - **和本章的关联**：解释 Greedy 的实际行为。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`getInstrAlternativeMappings`、`copyCost`。
  - **学什么**：AArch64 的 alternatives 范围和代价。
  - **和本章的关联**：`or.ll` 能留在 FPR 的直接原因。
### 文档
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：Greedy 模式的官方说明。
  - **学什么**：官方文档如何描述“best local mapping”。
  - **和本章的关联**：和源码对照，确认“局部”而非“全局”。

下一章拆 repair：`RepairingPlacement` 如何决定 COPY 插在 def、use 还是 CFG 边上；`getRepairCost` / `repairCost` 如何选位置；什么情况下 repair 是不可能的。

→ [Ch 10 repair 与 COPY 插入](./10-repair-and-copy-insertion.md)
