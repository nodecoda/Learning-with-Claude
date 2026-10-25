# Ch 8 — Fast mode：默认 mapping 与 optnone
> **目标**：确认 23.1.4 的 RegBankSelect 默认就是 Fast；分清“构造函数默认值”“命令行覆盖”“`optnone` 强制”三层；用真实 MIR 证明 `-O0` 本身不会让 `-regbankselect-greedy` 失效。
>
> **环境**：LLVM 23.1.4 官方预编译包（实测）；源码 `.scratch/llvm-23.1.4-src`（源码依据）。实验文件在临时目录，不写入课程仓库。
>
> **证据约定**：文中的命令和 MIR/哈希都在官方 23.1.4 包上实际跑过，标为**实测**；构造函数、分析依赖、`optnone` 分支标为**源码依据**；“为什么这样设计”的解释标为**guided**。
## 为什么需要这个？
很多资料把 RegBankSelect 的模式说成“`-O0` 用 Fast，`-O2` 用 Greedy”。这句话在 23.1.4 上**不准确**：

- 默认模式是 Fast，和优化级别无关；
- `-regbankselect-greedy` 可以在 `-O0` 下生效；
- 真正强制 Fast 的是函数上的 `optnone`，不是 `-O0` 这个命令行开关；
- `clang -O0` 默认给函数加 `optnone`，所以现象上“`clang -O0` 下 greedy 不生效”成立，但归因给 `-O0` 是错的。

这一章把三层机制拆开，并用三组实验固定结论。后面 Ch 9 讲 Greedy 的代价模型时，你会反复用到“Fast 根本不比较 alternative”这个事实。
## 先建立直觉
把两种模式想成两种出行策略：

- **Fast**：只看默认路线。旅行社给你一张票，你就走这条；它仍然会帮你处理“行李需要搬运”（repair COPY），但不会比较“换一条路线是不是更便宜”。
- **Greedy**：把默认路线和所有备选路线都算一遍，选局部总价最低的；备选路线来自 target 的 `getInstrAlternativeMappings`。
```text
Fast:
  default mapping ──► repair ──► apply
  （不比较 alternatives，不累加 repair 代价）

Greedy:
  default mapping ──┐
  alternative 1  ───┼──► computeMapping(cost) ──► 选最小 ──► repair ──► apply
  alternative 2  ───┘
```
`optnone` 的作用是：不管命令行选了 Greedy 还是 target 传了 Greedy，进函数时一律把 `OptMode` 改成 Fast。
## 核心概念
### 默认值：构造函数就是 Fast
`RegBankSelect.h`：
```cpp
enum Mode {
  /// Assign the register banks as fast as possible (default).
  Fast,
  /// Greedily minimize the cost of assigning register banks.
  Greedy
};

RegBankSelect(Mode RunningMode = Fast);
```
AArch64 的 pass 配置（`AArch64TargetMachine.cpp`）：
```cpp
bool AArch64PassConfig::addRegBankSelect() {
  addPass(new RegBankSelect());
  return false;
}
```
`new RegBankSelect()` 没传参数，所以用默认 `Fast`。RISC-V 同样是 `new RegBankSelect()`（`RISCVTargetMachine.cpp`）。这意味着：**不指定任何命令行选项时，AArch64/RISC-V 的 RegBankSelect 都是 Fast**。
### 命令行覆盖：`-regbankselect-fast` / `-regbankselect-greedy`
`RegBankSelect.cpp` 顶部：
```cpp
static cl::opt<RegBankSelect::Mode> RegBankSelectMode(
    cl::desc("Mode of the RegBankSelect pass"), cl::Hidden, cl::Optional,
    cl::values(clEnumValN(RegBankSelect::Mode::Fast, "regbankselect-fast",
                          "Run the Fast mode (default mapping)"),
               clEnumValN(RegBankSelect::Mode::Greedy, "regbankselect-greedy",
                          "Use the Greedy mode (best local mapping)")));
```
构造函数：
```cpp
RegBankSelect::RegBankSelect(Mode RunningMode)
    : MachineFunctionPass(ID), OptMode(RunningMode) {
  if (RegBankSelectMode.getNumOccurrences() != 0) {
    OptMode = RegBankSelectMode;
    if (RegBankSelectMode != RunningMode)
      LLVM_DEBUG(dbgs() << "RegBankSelect mode overrided by command line\n");
  }
}
```
所以优先级是：
```text
命令行选项（如果出现） > 构造函数参数（AArch64 默认 Fast）
```
`-regbankselect-fast` 和 `-regbankselect-greedy` 是同一个 `cl::opt` 的两个枚举值；实际使用时只指定一个，不要依赖“两个都写”时的顺序。
### `optnone`：函数级强制 Fast
`runOnMachineFunction`：
```cpp
const Function &F = MF.getFunction();
Mode SaveOptMode = OptMode;
if (F.hasOptNone())
  OptMode = Mode::Fast;
init(MF);

#ifndef NDEBUG
if (!checkFunctionIsLegal(MF))
  return false;
#endif

assignRegisterBanks(MF);
OptMode = SaveOptMode;
return false;
```
注意顺序：

1. 先保存原来的 `OptMode`；
2. 如果函数有 `optnone`，改成 Fast；
3. `init(MF)` 根据新的 `OptMode` 决定是否拿 MBFI/MBPI；
4. 跑完恢复 `OptMode`。

因此 `optnone` **压过命令行**：即使你传了 `-regbankselect-greedy`，带 `optnone` 的函数仍然走 Fast。恢复 `OptMode` 是为了同一个 pass 实例处理下一个函数时不受影响；每个函数独立判断。
### `optnone` 不是 `-O0`
- `optnone` 是 LLVM IR 的函数属性：`attributes #0 = { optnone noinline }`。
- `-O0` 是 `llc` / `clang` 的优化级别开关。
- `clang -O0` 默认会给函数加 `optnone`，所以两者经常一起出现；但 `llc -O0` 只是读你给的 IR，**不会自动加** `optnone`。
- 因此 `llc -O0` 读一个没有 `optnone` 的 IR 时，`-regbankselect-greedy` 仍然生效。

这条区分是本章最重要的结论。下面的实验会逐字节验证。
## 底层机制
### Fast 路径：只取默认 mapping
`assignInstr`：
```cpp
if (OptMode == RegBankSelect::Mode::Fast) {
  BestMapping = &RBI->getInstrMapping(MI);
  MappingCost DefaultCost = computeMapping(MI, *BestMapping, RepairPts);
  (void)DefaultCost;
  if (DefaultCost == MappingCost::ImpossibleCost())
    return false;
} else {
  RegisterBankInfo::InstructionMappings PossibleMappings =
      RBI->getInstrPossibleMappings(MI);
  if (PossibleMappings.empty())
    return false;
  BestMapping = &findBestMapping(MI, PossibleMappings, RepairPts);
}
```
Fast 只调用 `getInstrMapping`（默认 mapping），**不调用** `getInstrPossibleMappings`，所以不会看到 alternatives。
### Fast 仍然计算 repair 位置，但不累加 repair 代价
`computeMapping` 里：
```cpp
MappingCost Cost(MBFI ? MBFI->getBlockFreq(MI.getParent())
                      : BlockFrequency(1));
bool Saturated = Cost.addLocalCost(InstrMapping.getCost());
...
for (每个操作数) {
  ...
  if (assignmentMatch(...)) continue;
  if (Assign) { RepairPts.emplace_back(..., Reassign); continue; }

  RepairPts.emplace_back(..., Insert);
  ...
  if (!RepairPt.canMaterialize())
    return MappingCost::ImpossibleCost();

  if (!BestCost || Saturated)
    continue;   // Fast: BestCost == nullptr，直接跳过 repair 代价
  ...
}
```
Fast 调用 `computeMapping(MI, *BestMapping, RepairPts)` 时没有传 `BestCost`，所以：

- 仍然会为每个不匹配的操作数建立 `RepairingPlacement`；
- 仍然检查 `canMaterialize()`（例如 critical edge 能不能 split）；
- **不会**调用 `getRepairCost` 累加跨 bank copy 的代价；
- 因此 Fast 的“代价”只有指令本身的 `InstrMapping.getCost()`，主要用于判断 mapping 是否 valid/impossible。

这解释了一个常见误解：“Fast 不插 COPY”。错。Fast 照样插 repair COPY，只是不比较哪条路线更便宜。
### Fast 不需要 MBFI/MBPI
`getAnalysisUsage`：
```cpp
if (OptMode != Mode::Fast) {
  AU.addRequired<MachineBlockFrequencyInfoWrapperPass>();
  AU.addRequired<MachineBranchProbabilityInfoWrapperPass>();
}
```
`init`：
```cpp
if (OptMode != Mode::Fast) {
  MBFI = &getAnalysis<MachineBlockFrequencyInfoWrapperPass>().getMBFI();
  MBPI = &getAnalysis<MachineBranchProbabilityInfoWrapperPass>().getMBPI();
} else {
  MBFI = nullptr;
  MBPI = nullptr;
}
```
这是 Fast 编译时间优势的来源之一：不做块频率/分支概率分析，也不做 alternative 代价比较。`optnone` 强制 Fast 的动机通常也在这里——调试构建优先编译速度和可预测性，而不是寄存器 bank 的局部最优。这一点是**guided** 解释，源码只规定了行为，没有写动机。
## 示例一：默认模式与 `-regbankselect-fast` 逐字节相同
【实测】同一份 `or.ll`，AArch64：
```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -stop-after=regbankselect `
  or.ll -o or-default.mir

& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-fast -stop-after=regbankselect `
  or.ll -o or-fastflag.mir
```
两份文件的 SHA-256：
```text
or-default.mir  D63F2FE70B0E53735186B71B0315415598814180D8F110D70CD30790643236F6
or-fastflag.mir D63F2FE70B0E53735186B71B0315415598814180D8F110D70CD30790643236F6
```
> 哈希口径：MIR 头部的 `ModuleID` / `source_filename` 记录的是输入 IR 的路径，会进入哈希。本文的哈希是用绝对路径 `E:\WindowsTemp\rbs-course\or.ll` 跑出的；按上面的相对路径命令重跑，只有这两行路径不同，哈希会变，但 `body:` 仍逐字节相同。比较成对输出时看 `body:`，或在自己的路径下把两边都重跑一遍。
完全相同。`-O0` 下不加 `optnone` 的默认输出也是同一个哈希：
```text
or-o0-default.mir D63F2FE70B0E53735186B71B0315415598814180D8F110D70CD30790643236F6
```
这说明：默认 = `-regbankselect-fast`；`-O0` 的默认也没有变成 Greedy。
## 示例二：`-O0` + greedy 与 `-O2` + greedy 相同
【实测】同一份没有 `optnone` 的 `or.ll`：
```powershell
# A. -O0 + greedy
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  or.ll -o or-o0-greedy.mir
# B. -O2 + greedy
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  or.ll -o or-o2-greedy.mir
```
两份文件的 SHA-256：
```text
or-o0-greedy.mir 777C1DD17B4318B05242B08EE5FBC6874E8AE87B6DF26DF0285F389CDB9AEF35
or-o2-greedy.mir 777C1DD17B4318B05242B08EE5FBC6874E8AE87B6DF26DF0285F389CDB9AEF35
```
逐字节相同。也就是说，**`-O0` 本身没有覆盖 `-regbankselect-greedy`**。Greedy 仍然把 `G_OR` 放在 FPR，输出 0 条 repair COPY：
```yaml
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%4:fpr(i32) = G_OR %2, %3
%5:fpr(f32) = G_BITCAST %4(i32)
$s0 = COPY %5(f32)
```
## 示例三：`optnone` 压过 greedy
【实测】`or-optnone.ll`：
```llvm
define float @or_f(float %a, float %b) #0 {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
attributes #0 = { optnone noinline }
```
跑：
```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  or-optnone.ll -o or-optnone-greedy.mir
```
得到的是 **Fast 形态**：
```yaml
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%6:gpr(i32) = COPY %2(i32)
%7:gpr(i32) = COPY %3(i32)
%4:gpr(i32) = G_OR %6, %7
%5:gpr(f32) = G_BITCAST %4(i32)
$s0 = COPY %5(f32)
```
`G_OR` 在 GPR，出现两条 FPR→GPR 的 repair COPY。整文件哈希和 `or.ll` 的 Fast 输出不同（ModuleID/source_filename 不同），但 **body 逐行相同**；比较时看 body，不要只看整文件哈希。

三组实验合起来：

| IR | 命令行 | 实际模式 | `G_OR` bank | FPR→GPR repair COPY |
| --- | --- | --- | --- | --- |
| `or.ll` | 默认 | Fast | gpr | 2 |
| `or.ll` | `-regbankselect-fast` | Fast | gpr | 2 |
| `or.ll` | `-regbankselect-greedy` | Greedy | fpr | 0 |
| `or.ll` | `-O0 -regbankselect-greedy` | Greedy | fpr | 0 |
| `or-optnone.ll` | `-regbankselect-greedy` | **Fast** | gpr | 2 |

> 表中的 “FPR→GPR repair COPY” 只数 `%6`/`%7` 这两条由 repair 插入的 COPY。`$s0 = COPY %5` 是 ABI 返回边界，不计入 repair；Ch 10 会详细区分。
## 对比与取舍
| 维度 | Fast | Greedy |
| --- | --- | --- |
| 默认 | 是 | 否，需要 `-regbankselect-greedy` |
| 候选 mapping | 只有 `getInstrMapping` | 默认 + `getInstrAlternativeMappings` |
| 代价比较 | 不比较 alternatives | `MappingCost` 比较 |
| repair 代价 | 不累加 | 用 `getRepairCost` 累加 |
| MBFI/MBPI | 不需要 | 需要 |
| 编译时间 | 低 | 高 |
| 代码质量 | 依赖默认 mapping | 局部更优，但不保证全局最优 |
| `optnone` | 保持 Fast | 被强制回 Fast |

Fast 不是“错误模式”，而是“默认 mapping 模式”。如果 target 的默认 mapping 已经很好，Fast 和 Greedy 可能完全一样；`and.ll` 就是一个例子（Ch 9 会展示哈希相同）。
## 踩雷集锦
### 1. 把 `-O0` 当成强制 Fast
错：
```text
llc -O0 会自动让 RegBankSelect 走 Fast
```
对。`llc -O0` 不会给 IR 加 `optnone`；只有函数上真的有 `optnone` 才会强制 Fast。示例二的 `-O0 + greedy` 与 `-O2 + greedy` 哈希相同，就是反证。
### 2. 以为 `optnone` 只影响优化 pass
错：
```text
optnone 只让 IR 优化跳过这个函数，RegBankSelect 仍按命令行
```
对。`RegBankSelect::runOnMachineFunction` 直接读 `F.hasOptNone()`，在 RegBankSelect 内部把 `OptMode` 改成 Fast。它影响的是这个 pass 自己。
### 3. 以为 `-regbankselect-fast` 会关闭 GlobalISel
错：
```text
-regbankselect-fast 等于回到 SelectionDAG
```
对。它只选择 RegBankSelect 的模式；GlobalISel 管线、Legalizer、InstructionSelect 都还在。要关闭 GlobalISel 用 `-global-isel=false` 或 target 相关开关。
### 4. 以为 Fast 不插 COPY
错：
```text
Fast 只改 bank，不 repair
```
对。Fast 仍然为不匹配的操作数建立 `RepairingPlacement` 并插入 COPY；它只是不比较 alternatives、不累加 repair 代价。`or.ll` Fast 的两条 `%6`/`%7` 就是证据。
### 5. 用整文件哈希比较带不同文件名的 MIR
错：
```text
or-optnone-greedy.mir 和 or-fast.mir 哈希不同，所以模式不同
```
对。MIR 头部包含 `ModuleID` / `source_filename`，记录的是输入 IR 的路径；文件名不同哈希必然不同。比较模式要看 body，或先把这两行归一化。示例三的 body 与 Fast 相同，说明 `optnone` 生效。
## 进阶
- **读 `RegBankSelectMode` 的 `cl::Optional`**：它不是 `cl::init(Fast)`，默认值来自构造函数参数；命令行只在“出现过”时覆盖。这个设计让 target 可以在代码里传 Greedy，同时保留命令行覆盖能力。
- **读 `getAnalysisUsage` 的条件**：Fast 不注册 MBFI/MBPI，所以在一个只跑 Fast 的管线里，这两个分析不会被计算。这是 compile-time 差异的源码依据。
- **读 `optnone` 的恢复逻辑**：`OptMode = SaveOptMode` 在 `assignRegisterBanks` 之后执行；如果 `assignRegisterBanks` 提前返回 false，恢复语句仍会执行（函数末尾），但 `FailedISel` 已经设置。
- **看 `clang -O0` 的 IR**：用 `clang -O0 -S -emit-llvm` 观察函数上的 `optnone noinline`；再用 `llc -O0` 读同一份 IR，确认 `optnone` 是 IR 属性而不是 `llc` 开关。
## 动手练习
1. 【实测】给 `add.ll` 的 `add` 函数加 `optnone`，分别在 `-O2` 下跑默认与 `-regbankselect-greedy`，比较 body。两者应该都是 Fast。
2. 【实测】把 `or-optnone.ll` 的 `attributes #0 = { optnone noinline }` 删掉，重跑 `-O0 + greedy` 和 `-O2 + greedy`，确认两者哈希相同且 `G_OR` 在 FPR。
3. 【实测】对 `and.ll` 跑默认与 `-regbankselect-greedy`，比较哈希；解释为什么没有变化（提示：AArch64 的 `getInstrAlternativeMappings` 只覆盖 `G_OR` / `G_BITCAST` / `G_LOAD`）。
4. 【源码依据】在 `RegBankSelect.cpp` 里找到 `if (F.hasOptNone())`，说出它相对于 `init(MF)` 的位置为什么重要。
## 本章重点整理
- 23.1.4 的 `RegBankSelect` 默认模式是 Fast；AArch64/RISC-V 都用 `new RegBankSelect()`。
- 优先级：命令行选项 > 构造函数参数；`optnone` 在运行时再压过两者。
- `-O0` 不会自动加 `optnone`；`clang -O0` 会。
- Fast 只取默认 mapping，不比较 alternatives，也不累加 repair 代价；但仍然插 repair COPY。
- Fast 不需要 MBFI/MBPI，编译时间更低。
- 比较 MIR 模式要看 body，不要被 `ModuleID` / `source_filename` 的哈希差异骗了。
## 自我检核
- [ ] 能说出 RegBankSelect 默认模式的源码位置
- [ ] 能解释命令行选项、构造函数参数、`optnone` 三者的优先级
- [ ] 能解释为什么 `llc -O0` 下 `-regbankselect-greedy` 仍可能生效
- [ ] 能说出 Fast 与 Greedy 在 `assignInstr` 里的分派差异
- [ ] 能解释 Fast 为什么不需要 MBFI/MBPI
- [ ] 能区分“repair COPY”和“ABI 边界 COPY”
## 延伸阅读
### 源码
- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：`enum Mode`、`RegBankSelect(Mode RunningMode = Fast)`、`assignInstr` 的注释。
  - **学什么**：默认值、两种模式的职责。
  - **和本章的关联**：默认 Fast 的第一手证据。
- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`RegBankSelectMode`、构造函数、`init`、`getAnalysisUsage`、`assignInstr`、`runOnMachineFunction`。
  - **学什么**：命令行覆盖、`optnone` 分支、Fast/Greedy 分派、分析依赖。
  - **和本章的关联**：本章的主线源码。
- **[AArch64TargetMachine.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64TargetMachine.cpp)**
  - **读哪里**：`AArch64PassConfig::addRegBankSelect`。
  - **学什么**：AArch64 如何构造 RegBankSelect。
  - **和本章的关联**：证明 AArch64 使用默认 Fast。
### 文档
- **[LLVM 的 `optnone` 属性](https://llvm.org/docs/LangRef.html#function-attributes)**
  - **读哪里**：`optnone` 的语义与对优化 pass 的影响。
  - **学什么**：它是函数属性，不是命令行开关。
  - **和本章的关联**：解释 `clang -O0` 与 `llc -O0` 的差异。
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：Fast/Greedy 的官方描述。
  - **学什么**：官方文档与源码默认值是否一致。
  - **和本章的关联**：Ch 0 提到过“文档与源码默认值可能不一致”，本章用源码和实验确认。

下一章进入 Greedy：alternative mappings 从哪里来、`MappingCost` 的 `LocalCost` / `NonLocalCost` 怎么算、为什么局部贪心仍然可能不是全局最优，以及 `or.ll` 的 3 条跨 bank COPY 如何变成 0 条。

→ [Ch 9 Greedy mode：alternative mappings 与局部代价](./09-greedy-mode.md)
