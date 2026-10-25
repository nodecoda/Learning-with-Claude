# Ch 22 — bank 设计、代价模型与性能权衡

> **目标**：把“多加一个 bank”“把 mapping 写细一点”“把 copyCost 调大”这些看似局部的决定，放回编译时间、COPY 数量、寄存器分配与最终代码质量里评估。读完你能回答三个问题：bank 该不该拆？mapping 该多细？Greedy 值不值得开？
>
> **环境**：LLVM 23.1.4，源码快照 `.scratch/llvm-23.1.4-src`。代价公式、repair、候选遍历来自源码；基准测试方法是 `guided`，本章不提供任何编造的耗时或加速数字。
>
> **证据约定**：`源码依据` 指可直接在 23.1.4 源文件里核对的行为；`实测` 仅限 Ch 0 已经跑出的 MIR；`guided` 指需要你自己构建、跑基准或改 target 的步骤。

## 为什么需要这个？

`RegisterBankInfo` 的代码看起来只是“填表”，但它同时影响三件互相拉扯的事：

1. **代码质量**：bank 选得对，跨 bank COPY 少；选得错，指令数、寄存器压力和调度窗口都变差。
2. **编译时间**：Greedy 要为每条指令比较多个候选、算 repair、看 block frequency；候选越多，代价越高。
3. **可维护性**：每多一个 bank，就要多一组 mapping、copyCost、alternative 和测试；表越大，越容易在某个位宽上返回 invalid。

最危险的误区，是把“更细”当成“更好”。把 GPR 拆成 GPR32/GPR64、把 FPR 拆成 FPR16/32/64/128，看起来更贴近硬件，实际上会让 mapping 组合爆炸，并在窄值/宽值之间制造大量 COPY。AArch64 选择 3 个 bank、用位宽区分大小，不是偷懒，而是权衡后的结果。

## 先建立直觉

把 bank 想成城市里的行政区，COPY 是跨区通勤：

```text
2 个区：      住址选择少，通勤距离短，但有些设施必须跨区
3 个区：      设施分布更自然，跨区次数可控
6 个区：      每个区更“专”，但地址表爆炸，跨区通勤变多
```

bank 设计的核心不是“区越多越专业”，而是：

```text
总代价 ≈ Σ 指令在所选 bank 上的代价
       + Σ 跨 bank 搬操作数的代价
       + 编译期为“找方案”付出的时间
```

这条式子里的三项经常互相矛盾。把指令放回“最自然”的 bank，可能让操作数跨区；把操作数留在原 bank，可能让指令本身更贵。Greedy 只会在 target 给出的候选里做局部最小化，它不会替你发明更好的 bank 划分。

## 核心概念

### bank 数量：先问“硬件是否真的分离”

| 设计 | 例子 | 好处 | 风险 |
| --- | --- | --- | --- |
| 一个 bank 覆盖所有 | 极简 bring-up | mapping 最简单 | 无法表达寄存器文件差异，ISel 可能选不出指令 |
| 按寄存器文件分 | AArch64 GPR/FPR/CC | 贴近硬件，copyCost 有意义 | 跨文件 COPY 与 repair |
| 按位宽再拆 | GPR32/GPR64 两个 bank | 位宽约束更直接 | 窄/宽值互转产生大量 COPY |
| 按用途再拆 | 地址/数据分开 | 约束更精确 | mapping 组合与测试量上升 |

AArch64 的选择是“按寄存器文件分，位宽交给同一个 bank 的 mapping 处理”：GPR 覆盖 32/64/128 位，FPR 覆盖 16–512 位。这样 `G_ADD s32` 和 `G_ADD s64` 都在 GPR，不需要跨 bank 拷贝；代价是 mapping 表要按位宽展开。

RISC-V 同样是 3 个 bank（GPRB/FPRB/VRB），但 FPRB 只覆盖 `FPR64`，VRB 覆盖 `VRM8`。它的 bank 数不多，mapping 表也薄；这不是设计优劣，而是支持范围不同。

### mapping 粒度：整条指令还是逐操作数

`InstructionMapping` 的本质是“每个操作数一个 `ValueMapping`”。因此粒度有两层：

- **指令级**：所有操作数用同一个 bank（AArch64 的 `getSameKindOfOperandsMapping`）。
- **操作数级**：不同操作数用不同 bank（AArch64 的 `G_LOAD`：结果 GPR/FPR，地址固定 GPR64；`G_STORE` 同理）。

粒度越细，越能表达真实约束，但候选数量也越多。AArch64 只在必要的地方做逐操作数：

```cpp
OpRegBankIdx = {Idx0, /* Predicate */ PMI_None, PMI_FirstFPR, PMI_FirstFPR};
```

这是 `G_FCMP`：结果按标量/向量选 GPR/FPR，谓词不占 bank，两个输入在 FPR。若把整条指令强行统一到一个 bank，谓词或结果就会被错误地搬来搬去。

### 代价模型：相对权重，不是周期数

基类 `copyCost` 只区分同 bank/不同 bank：

```cpp
return &A != &B;
```

AArch64 覆盖成 `copyCost(A=GPR, B=FPR)=5`（FPR→GPR，FMOVXDr/FMOVWSr）与 `copyCost(A=FPR, B=GPR)=4`（GPR→FPR，FMOVDXr/FMOVSWr），并带 `FIXME: This should be deduced from the scheduling model.`。这两个数不是周期数，也不保证与任何 CPU 的延迟/吞吐对应；它们只用于在候选之间排序。

代价模型还有两个隐藏输入：

- **指令自身 cost**：`InstructionMapping::getCost()`，AArch64 大多填 1，个别 `G_LOAD` 在 FPR 上填 2；
- **repair cost**：`getRepairCost` 对单段 mapping 调 `copyCost`，对多段 mapping 调 `getBreakDownCost`；基类 `getBreakDownCost` 默认返回 `UINT_MAX`，也就是“不可 repair”。

### Fast 与 Greedy：不是“开不开优化”，而是“候选集大小”

| 模式 | 候选 | 是否比较代价 | 典型用途 |
| --- | --- | --- | --- |
| Fast | 只有 `getInstrMapping` 的默认 mapping | 计算一次，确认可实现 | `optnone`、`-O0`、快速 bring-up |
| Greedy | 默认 + `getInstrAlternativeMappings` | 逐个 `computeMapping`，取最小 | 追求 COPY 数量/代码质量 |

Fast 也会调用 `computeMapping`，目的不是比较，而是确认默认 mapping 能落地（`ImpossibleCost` 就失败）。所以 Fast 不是“跳过 repair 计算”，而是“不比较候选”。

## 底层机制

### 1. Greedy 的候选遍历

```cpp
RegisterBankInfo::InstructionMappings PossibleMappings =
    RBI->getInstrPossibleMappings(MI);
if (PossibleMappings.empty())
  return false;
BestMapping = &findBestMapping(MI, PossibleMappings, RepairPts);
```

`getInstrPossibleMappings` 把默认 mapping 放在最前面，再追加 alternatives；debug 下还会对每个候选做 `verify(MI)`。`findBestMapping` 从 `ImpossibleCost()` 开始，对每个候选调 `computeMapping`：

```cpp
MappingCost CurCost = computeMapping(MI, *CurMapping, LocalRepairPts, &Cost);
if (CurCost < Cost) {
  Cost = CurCost;
  BestMapping = CurMapping;
  ...
}
```

如果所有候选都不可实现，且 `-global-isel-abort` 没开，它会选第一个候选并插入一个 `Impossible` repair point，随后触发 failed ISel。这条路径是“静默 fallback”的来源之一。

### 2. 代价怎么累加

`computeMapping` 先加指令自身 cost：

```cpp
MappingCost Cost(MBFI ? MBFI->getBlockFreq(MI.getParent())
                      : BlockFrequency(1));
bool Saturated = Cost.addLocalCost(InstrMapping.getCost());
```

然后逐个操作数检查：

- 已经匹配目标 mapping：免费；
- 只需改 bank 赋值（简单 reassignment）：免费；
- 需要插 COPY 或拆分：累计 repair cost。

非 split 的 insert point 加到 `LocalCost`；需要切边/切块的 insert point 按频率加到 `NonLocalCost`，并且加 5% 的 split bias：

```cpp
const uint64_t PercentageForBias = 5;
uint64_t Bias = (RepairCost * PercentageForBias + 99) / 100;
...
uint64_t PtCost = InsertPt->frequency(*this) * CostForInsertPt;
```

代价的比较与打印形式是：

```text
LocalFreq * LocalCost + NonLocalCost
```

这也是为什么“循环里的 COPY”比“入口块的 COPY”贵：`LocalFreq` 和 insert point 的 frequency 都来自 `MachineBlockFrequencyInfo`。

### 3. repair 的边界

`getRepairCost` 对单段 mapping 调 `copyCost`；`copyCost` 返回 `UINT_MAX` 时，repair 被视为 impossible。对多段 mapping，它调 `getBreakDownCost`，基类默认直接返回 `UINT_MAX`，所以不支持 breakdown 的 target 会在 Greedy 下直接判 impossible。

源码里还有一条 TODO：如果被 repair 的值本身来自一条 COPY，且 COPY 的源已经在目标 bank，本可以免费复用，但当前实现仍然按一次 `copyCost` 计。这意味着 **Greedy 的代价是保守估计，不是最终机器码的精确 COPY 数**；后面的 RA 还可能把 COPY 合并掉。

### 4. 与 InstructionSelect、RA 的互动

- **ISel**：RegBankSelect 输出的是 bank，不是 regclass。ISel 看到 `%0:gpr(s64)` 才能匹配 GPR 指令；bank 选错，pattern 直接匹配不上，或者只能选到更差的变体。
- **RA**：bank 决定了可分配的寄存器文件。跨 bank COPY 会变成真实的复制指令或 `COPY` 伪指令；RA 可能 coalesce 掉一部分，也可能因为寄存器压力保留下来。**MIR 里的 COPY 数不等于最终汇编的 COPY 数**。
- **寄存器压力**：拆 bank 可能让两个值分别落在不同寄存器文件，降低单文件压力；但 repair COPY 会引入新的短生命周期 vreg，反而提高压力。
- **调度**：多出来的 COPY 占用 issue slot，可能拉长依赖链；bank 选对时，FP 链可以完全留在 FPR，不产生搬移。

### 5. 编译时间从哪里来

Greedy 的成本大致与以下因素相乘：

| 因素 | 影响 |
| --- | --- |
| 每条指令的候选数 | 默认 + alternatives；AArch64 大多数是 1，`G_OR`/`G_BITCAST`/`G_LOAD` 是 2–4 |
| 每个候选的操作数数 | `computeMapping` 逐操作数检查 |
| repair 搜索 | 每个不匹配操作数要构造 insert point、尝试避免 split |
| block frequency | 需要 `MachineBlockFrequencyInfo` / `MachineBranchProbabilityInfo` |
| 断言与 verify | assertions build 下每个候选 `verify(MI)`，代价不可忽略 |

AArch64 只给 3 个 opcode 加 alternative，是控制编译时间的直接手段。RISC-V 一个都没有，所以 Greedy 的候选集通常只有 1 个；这不是“Greedy 更快”，而是“没有可比较的方案”。

## 三个例子

### 例 1：`or.ll` 的代价对比（源码依据 + Ch 0 实测）

Ch 0 已经实测：Fast 下 `G_OR` 在 GPR，产生 3 条跨 bank COPY；Greedy 下在 FPR，0 条。用本章的模型解释：

| 方案 | 指令 cost | 操作数 repair | 总代价（相对值） |
| --- | --- | --- | --- |
| GPR（Fast 默认） | 1 | 2 × FPR→GPR(5) + 1 × GPR→FPR(4) | 15 |
| FPR（Greedy 候选） | 1 | 0 | 1 |

这里的两条 FPR→GPR 是 `G_BITCAST` 的结果喂给 `G_OR`，一条 GPR→FPR 是返回值从 GPR 回到 `$s0`。数值来自 AArch64 的 `copyCost`，不是运行时测量；Ch 0 的 MIR 只证明了 COPY 的条数与 bank，不证明“15 vs 1”这个具体算术。

这个例子也说明：**默认 mapping 的“自然”选择不一定便宜**。Fast 选 GPR 是因为标量位运算默认在 GPR，而不是因为它对这条链最优。

### 例 2：`G_LOAD` 的 2 与 alternative（源码依据）

AArch64 在默认 mapping 里给“结果在 FPR 的 load”一个更高的指令 cost：

```cpp
// Loading in vector unit is slightly more expensive.
// ... for the fast mode this number does not matter and
// for the greedy mode the cost of the cross bank copy will
// offset this number.
if (OpRegBankIdx[0] != PMI_FirstGPR) {
  Cost = 2;
  break;
}
```

而 `getInstrAlternativeMappings` 对 64 位 `G_LOAD` 提供两个候选：结果 GPR 或 FPR，cost 都是 1。把这两段放在一起看：

- Fast：默认 mapping 可能已经把 FP load 放 FPR（通过 MMO/use 分析），cost 2，但没有跨 bank COPY；
- Greedy：alternative 提供 FPR 结果，若操作数本来就在 FPR，repair 为 0；指令 cost 1，可能比默认更便宜；
- 若 load 的地址不在 GPR，两个候选都要先修地址，代价由 `copyCost` 决定。

注释里那句“for the fast mode this number does not matter”很关键：Fast 不比较候选，所以 cost 2 只影响 Greedy 的相对排序。读代价表时先问“这个数在哪个模式下会被用到”。

### 例 3：基准测试的最小方法（guided）

不要用“我觉得 COPY 少了”当结论。最小可比实验应固定：

```bash
# 1) 只测 RegBankSelect：喂同一份 legalized MIR
llc -mtriple=aarch64-linux-gnu -O2 -run-pass=regbankselect \
    -regbankselect-fast input.mir -o fast.mir
llc -mtriple=aarch64-linux-gnu -O2 -run-pass=regbankselect \
    -regbankselect-greedy input.mir -o greedy.mir

# 2) 端到端：同一 IR、同一 triple、同一 -mcpu
hyperfine --warmup 3 --runs 20 \
  'llc -mtriple=aarch64-linux-gnu -O2 -global-isel input.ll -o fast.s' \
  'llc -mtriple=aarch64-linux-gnu -O2 -global-isel -regbankselect-greedy input.ll -o greedy.s'

# 3) 代码质量：比较 vreg 间跨 bank COPY，而不是所有 COPY
#    ABI 边界的 COPY 两边是物理寄存器，不算 repair
```

报告至少要有：中位数耗时、COPY 计数、代码大小（`llvm-size` 或 `size`）、以及测试 IR 的来源与规模。单次运行、单个函数、不同构建类型之间的比较都不算证据。`-stats` 可以看到 `NumInstructionMappingsCreated` 等计数器，但前提是构建开启了统计；不要假设官方预编译包一定支持。

## 对比与取舍

| 决定 | 偏保守 | 偏激进 | 何时选激进 |
| --- | --- | --- | --- |
| bank 数量 | 1–3 个，按寄存器文件 | 按位宽/用途继续拆 | 硬件真的分离，且有测试证明收益 |
| mapping 粒度 | 整条指令同 bank | 逐操作数精细映射 | 地址/谓词/结果约束不同 |
| alternative 数量 | 只给热点 opcode | 广泛提供候选 | Greedy 默认开启且编译时间可接受 |
| copyCost | 基类 0/1 | 按指令/方向细调 | 有 scheduling 数据或实测支撑 |
| 默认模式 | Fast | Greedy | `optnone` 之外、且质量收益可测 |
| mapping 表 | 静态、可 verify | 动态构造 | 组合多、原型阶段 |

一个实用的判断顺序：

1. 先问“这个 bank 是否对应真实寄存器文件”。不是，就不要拆。
2. 再问“不拆是否会导致 ISel 选不出指令”。会，才拆。
3. 拆了之后，先用 Fast 跑通，再用 Greedy 测 COPY 与编译时间。
4. 只有当 Greedy 在代表性 IR 上稳定减少跨 bank COPY，且编译时间可接受，才考虑扩大 alternative 覆盖。

## 踩雷集锦

1. **把 `copyCost` 当周期数。**
   5/4 只是相对权重，源码自己都标了 `FIXME`，说应该从 scheduling model 推导。用它做“这条 COPY 会花 5 个 cycle”的结论是错的。

2. **用 MIR 的 COPY 总数衡量收益。**
   ABI 边界、参数传递、返回值都会产生 COPY；真正与 bank 相关的是“两边都是 vreg 且 bank 不同”的 repair COPY。统计前先过滤。

3. **认为 Greedy 一定比 Fast 好。**
   Greedy 只在 target 给出的候选里做局部最优。候选少（RISC-V）、代价模型偏（copyCost 不准）、或 repair 被高估时，它可能选不出更好的方案，甚至因为额外候选拖慢编译。

4. **为了“更精确”拆出 GPR32/GPR64 两个 bank。**
   这会让 s32↔s64 的扩展/截断频繁跨 bank，mapping 组合翻倍，测试量也翻倍。AArch64 用一个 GPR bank + 位宽 mapping，正是为了避免这种碎片化。

5. **在 `optnone` 函数上比较 Fast/Greedy。**
   `RegBankSelect::runOnMachineFunction` 里 `if (F.hasOptNone()) OptMode = Mode::Fast;` 会覆盖命令行。用 `clang -O0` 生成的 IR 做 Greedy 实验，很可能两者完全一样；要用没有 `optnone` 的 IR 或 `llc` 直接读 IR。

## 进阶

### 评审者会问什么

一个新 bank 或一组新 mapping 提交上游时，下面这些问题基本都会被问到：

1. 为什么需要新 bank，而不是把类加进现有 bank 的覆盖？
2. 新 bank 覆盖哪些 regclass？有没有会被查询但未覆盖的类？
3. 每个 alternative 对应的指令/pattern 是什么？ISel 真的能选吗？
4. `copyCost` 的方向与数值依据是什么？为什么不是对称的？
5. 在哪些 IR 上验证过 COPY 减少？测试是否覆盖 Fast 与 Greedy？
6. 编译时间影响多大？候选数增加了多少？
7. 对寄存器压力/溢出率有没有副作用？
8. PHI、临界边、call 边界有没有覆盖？
9. mapping 表是手写还是生成？以后加位宽怎么维护？
10. 如果去掉这个改动，最坏情况是什么？有没有更小的修法？

能提前回答这十个问题，基本就不会在 review 里被“感觉不对”卡住。

### 基准测试的边界

- **只看 pass 时间**：用 `-run-pass=regbankselect` 喂同一份 legalized MIR，隔离 Legalizer/ISel 的噪声。
- **只看端到端**：用完整 `llc`，但固定 `-mcpu`、`-O`、构建类型与 IR。
- **代码质量**：COPY 计数、指令数、代码大小、必要时加动态指令数；不要只看汇编“看起来更短”。
- **统计**：至少 20 次，报中位数与离散度；不同机器/不同构建类型的结果不能直接比。
- **回归**：把代表性 IR 固化成 lit/MIR 测试，避免只靠一次性脚本。

## 动手练习

1. 用 Ch 0 的 `or.ll`，把 `copyCost` 的两个方向都改成 1，手算 Fast/Greedy 的总代价，判断 Greedy 是否仍选 FPR。
2. 给 `G_AND` 加一个 FPR alternative（只改本地副本），预测 `and.ll` 的 Greedy 结果，并解释为什么 AArch64 上游没有这么做。
3. 选一个包含循环的 IR，比较入口块与循环体内 repair COPY 的代价差异，说明 `LocalFreq * LocalCost + NonLocalCost` 里哪一项占主导。
4. 设计一个基准实验：同一份 legalized MIR，分别跑 Fast/Greedy 20 次，记录中位数、COPY 数与代码大小。写出你会固定哪些变量、如何过滤 ABI COPY。

## 本章重点整理

- bank 设计的第一问是“硬件是否真的分离”，不是“能不能拆得更细”。
- AArch64 用 3 个 bank + 位宽 mapping，避免 GPR32/GPR64 碎片化。
- mapping 粒度可以是指令级，也可以逐操作数；地址、谓词、结果常需要不同 bank。
- `copyCost` 是相对权重；AArch64 的 FPR→GPR=5、GPR→FPR=4 不是周期数。
- Fast 只算默认 mapping 的可实现性；Greedy 才比较候选。
- `MappingCost` 的形式是 `LocalFreq * LocalCost + NonLocalCost`；split 有 5% bias。
- repair 代价用 `copyCost` / `getBreakDownCost`，且对可 coalesce 的 COPY 是保守估计。
- MIR 的 COPY 总数不等于 repair COPY 数；RA 还可能合并一部分。

## 自我检核

- [ ] 能解释为什么 AArch64 不把 GPR 拆成 GPR32/GPR64 两个 bank
- [ ] 能说出 `copyCost` 在代价模型里的角色，以及它不是周期数的原因
- [ ] 能写出 Greedy 从 `getInstrPossibleMappings` 到 `findBestMapping` 的路径
- [ ] 能解释 `LocalFreq * LocalCost + NonLocalCost` 中两项分别来自哪里
- [ ] 能说出为什么 Fast 也会计算 repair，却不比较候选
- [ ] 能区分 ABI COPY 与 repair COPY，并说明统计时为什么要过滤
- [ ] 能列出至少五个 review 新 bank 时该问的问题

## 延伸阅读

- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：文件头的 cost 公式、`MappingCost` 的 `LocalCost` / `NonLocalCost` / `LocalFreq`。
  - **学什么**：局部贪心的形式化定义，以及“指令代价 + 操作数搬运代价”的组合方式。
- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`computeMapping`、`getRepairCost`、`findBestMapping`、`MappingCost::operator<`。
  - **学什么**：5% split bias、block frequency、impossible cost 的实际处理。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`copyCost`、`G_LOAD` 的 cost 2、三个 alternative opcode。
  - **学什么**：真实 target 如何用少量候选控制编译时间与代码质量。
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：Fast/Greedy 两小节与 NOTE。
  - **学什么**：官方对两种模式的定位；注意文档默认值与源码可能不一致，以构造函数为准。

下一章把视角再拉高：除了“局部贪心 + 手写 alternative”，还有哪些设计空间？全局分析、指令复制、combiner/legalizer 的互相影响，以及如何参与上游。

→ [Ch 23 设计空间与下一步](./23-design-space-and-next-steps.md)