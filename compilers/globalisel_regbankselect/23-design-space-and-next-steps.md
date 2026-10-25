# Ch 23 — 设计空间与下一步

> **目标**：把 RegBankSelect 放回设计空间：局部贪心之外，官方还留下了 Global mode、指令复制、TableGen 静态表三条“未来路线”。读完你能判断一个 bank 问题该在 mapping 表、legalizer/combiner，还是根本不该由 RegBankSelect 修，并说出参与上游的现实路径。
>
> **环境**：LLVM 23.1.4，源码快照 `.scratch/llvm-23.1.4-src`。本章不运行新的 llc 实验；源码引用可直接核对，设计推演标 `guided`，Ch 0 已跑过的结论标 `实测`。
>
> **证据约定**：`源码依据` 指 23.1.4 源文件中的代码/注释；`实测` 仅指前文展示过的 MIR；`guided` 指需要你自己构建、改 target 或跑基准才能验证的推演。本章不给出编造的 MIR、耗时或加速比。

## 为什么需要这个？

前 22 章回答“RegBankSelect 怎么做”，这一章回答“它为什么只能这么做，以及还能怎么做”。很多看似缺陷的行为其实是明确取舍：

- 逐条指令做决定、不回头，所以看不到后面的 use，也无法撤销已插入的 COPY。
- 只在 target 给出的候选里选；没有 alternative，Greedy 就退化成 Fast。
- mapping 表手写，新 opcode、新位宽、新 bank 都要人工补齐。
- 假设 `InstructionSelect` 能接住自己选的 bank；接不住时问题在下游才爆。

不理解这些边界，就容易用错误方式“修”问题：硬塞一个 ISel 不支持的 bank，或为了少一次 COPY 破坏 PHI 一致性。理解设计空间后，你会先问“这是局部决策的问题，还是输入形状的问题”，再决定改哪一层。本章也是课程收束：把前 22 章的工具放进一张地图。

## 先建立直觉

```text
LLVM IR
  │ IRTranslator
  ▼
generic MIR（G_*，vreg 还没有 bank）
  │ Legalizer
  ▼
legal MIR（opcode 与类型都合法）
  │ Combiner / target lowering（可选，改指令形状）
  ▼
RegBankSelect（给每个 vreg 选 bank，必要时插 COPY）
  │ InstructionSelect
  ▼
target MIR（具体指令与 regclass）
  │ Register Allocation
  ▼
物理寄存器与最终汇编
```

两个容易忽略的事实：

1. **输入已被前序 pass 塑造。** Legalizer 决定 opcode 和类型，Combiner 可能把指令换掉、拆开或合并；mapping 表里的 opcode 不一定是 IR 里原来的操作。
2. **输出还不是最终答案。** RegBankSelect 只给 vreg 标 bank，不选物理寄存器，也不保证 ISel 能选中。bank 再“便宜”，没有 pattern 也是死路。

局部贪心与全局分析，可以想成两种交通管理：

```text
局部贪心：每个路口独立装红绿灯，只看当前车流
全局分析：整个城市协调配时，允许为干道牺牲支路
```

前者快、可预测、易实现；后者可能更优，但需要全局信息、更长求解时间，还要处理“一个路口改了，整张网重算”。LLVM 23.1.4 选择前者，并把后者写成未来工作。指令复制则是另一条思路：与其把结果从 GPR 复制到 FPR，不如在两边各算一次；当计算很便宜或可重物化时，这可能更划算，但“哪些指令能安全做两份、输入从哪来、代码体积和压力怎么算”都是难题。

## 核心概念

### 1. 局部贪心：一个被明确承认的妥协

`RegBankSelect.h` 的文件头说得很直白：

```cpp
/// By default, the reg bank selector relies on local decisions to
/// assign the register bank. In other words, it looks at one instruction
/// at a time to decide where the operand of that instruction should live.
///
/// At higher optimization level, we could imagine that the reg bank selector
/// would use more global analysis and do crazier thing like duplicating
/// instructions and so on. This is future work.
///
/// For now, the pass uses a greedy algorithm to decide where the operand
/// of an instruction should live.
```

`rely on local decisions`、`future work` 是当前实现的契约，不是“暂时没优化”。`assignRegisterBanks` 按块顺序遍历；处理某条指令时，操作数 bank 已由前面的指令决定，它只在这个既成事实上最小化。好处是可预测、编译时间可控、实现简单；代价是可能为当前指令省一次 COPY，却给后面制造两次，或在循环入口做了局部最优、全局不优的决定。

### 2. Global mode：官方文档里的“未来时”

官方 `RegBankSelect.rst` 写得很清楚：

```text
RegBankSelect currently has two modes:

* **Fast** --- For each instruction, pick a target-provided "default" bank
  assignment.  This is the default at -O0.

* **Greedy** --- For each instruction, pick the cheapest of several
  target-provided bank assignment alternatives.

We intend to eventually introduce an additional optimizing mode:

* **Global** --- Across multiple instructions, pick the cheapest combination of
  bank assignments.
```

但 23.1.4 的 `Mode` 枚举只有两个值：

```cpp
enum Mode {
  /// Assign the register banks as fast as possible (default).
  Fast,
  /// Greedily minimize the cost of assigning register banks.
  /// This should produce code of greater quality, but will
  /// require more compile time.
  Greedy
};
```

所以“Global mode 存在”是常见误读。文档说的是 `intend to eventually introduce`，不是已实现；命令行也只有 `-regbankselect-fast` / `-regbankselect-greedy`。rst 里「default at -O0」是定位描述：源码默认构造就是 Fast，真正强制 Fast 的是 `optnone`（见踩雷 4）。

真要做 Global（设计推演，`guided`），问题会从“给一条指令选 bank”变成“给整张数据流图做划分”：

- **变量**：每个 vreg 的 bank。
- **目标**：所有指令的 bank 代价 + 所有跨 bank 边的 COPY 代价。
- **约束**：每条指令的操作数组合必须在 target 允许的 mapping 里；PHI 输入与结果同 bank；跨 bank 必须能插合法 COPY。
- **难点**：循环、临界边、代码体积、编译时间，以及 tie-break 的确定性。

这不是“多跑几遍 Greedy”能解决的。Greedy 一旦 `applyMapping`，bank 就写进 MRI；第二遍看到的是已固定的 bank，不会重新打开选择。要做全局，必须显式“取消赋值、重算、再修复”，还要防止迭代震荡。

还有一个 upstream 状态值得知道：rst 的 NOTE 说 AArch64 正在考虑在 `-O0`（或 backend `-O1`）也用 Greedy，因为 `gmir-llt` 不区分浮点/整数标量，load/store 的默认赋值落整数 bank，导致大多数 FP 操作跨 bank COPY。但 23.1.4 的 `addRegBankSelect()` 仍是 `addPass(new RegBankSelect());`，即 Fast；这个 NOTE 是考虑，不是已实现。

### 3. 指令复制：用代码体积换 COPY

`repair` 是“搬数据”：在需要处插 `G_COPY`。指令复制是“多做一份计算”：在另一个 bank 里再生成一份定义，让不同消费者各取所需。

```text
        %v = G_ADD %a, %b        (GPR)
       /                    \
  G_AND %v, %c          G_FADD %v, %d
     (GPR)                  (FPR)
```

- **repair**：保留一份 `G_ADD`，在 FPR 边上插 `%v.fpr = COPY %v`。
- **duplication**：在 FPR 边上再生成 `%v.fpr = G_ADD %a.fpr, %b.fpr`，两边各用各的。

复制不免费：多一条指令，代码体积和 I-cache 压力上升；输入不在目标 bank 时还要再插 COPY，可能递归扩散；多一份活跃值会提高寄存器压力，增加 spill；只有纯、可安全推测、无副作用的指令才能复制，load/store/call/可能 trap 的除法都要特别小心。这也解释了为什么上游至今没做通用复制：它需要全局代价模型和合法性分析，不是“多写几行”。

### 4. Legalizer 与 Combiner：输入不是“原始 IR”

一个典型例子是 `G_ICMP`。AArch64 的 legalizer 规则里有：

```cpp
getActionDefinitionsBuilder(G_ICMP)
    .legalFor({{i32, i32}, {i32, i64}, {i32, p0}})
    .widenScalarOrEltToNextPow2(1)
    .minScalarOrElt(1, s8)
    .clampScalar(1, s32, s64)
    .clampScalar(0, s32, s32)
```

`clampScalar(0, s32, s32)` 把第 0 个操作数（结果）夹到 `s32`。因此 IR 里的 `i1` 比较结果，进入 RegBankSelect 前通常已变成 `s32`；普通 AArch64 路径下它落在 GPR，而不是 CC bank。这就是“CC 不是默认布尔 bank”的根源之一。

再看 AArch64 的 pass 配置：

```cpp
void AArch64PassConfig::addPreRegBankSelect() {
  const bool IsGlobalISelOptNone =
      getAArch64TargetMachine().isGlobalISelOptNone();
  if (!IsGlobalISelOptNone) {
    addPass(createAArch64PostLegalizerCombinerLegacy(IsGlobalISelOptNone));
    if (EnableGISelLoadStoreOptPostLegal)
      addPass(new LoadStoreOpt());
  }
  addPass(createAArch64PostLegalizerLowering());
}
```

RegBankSelect 之前，AArch64 还会跑 post-legalizer combiner 和 target lowering（`optnone` 时跳过前者）。它们可能把一条 generic 指令换成另一条、拆分/合并向量操作、生成 target-specific generic opcode。后果是：mapping 表里某个条目可能永远走不到，也可能出现新 opcode 没覆盖，只能走 `getInstrMappingImpl` 的 regclass 兜底或直接失败。调试“bank 选错”时，不能只看 IR，要停在 `-stop-after=regbankselect` 看实际进入的 MIR。

### 5. TableGen 化：已经生成了什么，还没生成什么

AArch64 和 RISC-V 的 `RegisterBankInfo.cpp` 文件头都有同一句：

```cpp
/// \todo This should be generated by TableGen.
```

但“TableGen 化”要分两层：

- **已经生成**：bank ID、覆盖的寄存器类、每个 bank 的 size。Ch 20 用 `llvm-tblgen -gen-register-bank` 实测过，生成了 `CCRegBankID=0`、`FPRRegBankID=1`、`GPRRegBankID=2` 及覆盖位图。
- **还没生成**：`getInstrMapping` / `getInstrAlternativeMappings` 里的 mapping 表。`AArch64GenRegisterBankInfo.def` 名字里带 `Gen`，但它是手写文件。

mapping 难生成，因为它不只依赖 opcode 和类型，还依赖操作数是定义还是使用、是标量还是向量，值的 use 情况（`isLoadFromFPType`、`onlyUsesFP` 这类分析），target 的 legalizer/ISel 支持范围，以及 `G_CONSTANT` 小位宽扩展之类的特殊规则。TableGen 擅长“opcode + 类型 → pattern”的静态关系，不擅长任意 C++ 谓词。官方 rst 也承认：

```text
TODO:
All this information should eventually be static and generated by TableGen,
mostly using existing information augmented by bank descriptions.

TODO:
getInstrMapping is currently separate from getInstrAlternativeMapping
because the latter is more expensive: as we move to static mapping info,
both methods should be free, and we should merge them.
```

静态化的收益不只是少写代码：两个查询可以合并，Greedy 枚举候选的成本下降，表也更容易做一致性检查。但这是跨 target 的基础设施工作，不是某个 target 能单独完成的。

## 底层机制

### 1. 为什么一次 top-down 遍历无法回头

`assignRegisterBanks` 的核心顺序是：

```text
for each MachineBasicBlock (in layout order)
  for each MachineInstr
    getInstrMapping / getInstrAlternativeMappings
    computeMapping（算代价、找 repair 点）
    applyMapping（写 MRI.setRegBank，插 COPY）
```

处理到指令 `I` 时，`I` 的操作数要么是前面指令定义的 vreg（bank 已写死），要么是 PHI（由前驱/后继关系推断），要么是物理寄存器/常量（由 target 规则决定）。`computeMapping` 只能在这些既成 bank 上算“搬到候选 bank 要多少 COPY”，不能建议“把前面那条指令改到另一个 bank”。

PHI 让这件事更明显。`getInstrMappingImpl` 把 `COPY`、`PHI`、`REG_SEQUENCE` 当 copy-like 指令：mapping 不按 opcode 查表，而从已有操作数/结果推断。PHI 输入来自多个前驱，若某个前驱 bank 与结果不一致，就要在边上修复；边是临界边时，修复位置还受限。全局分析必须把这些约束一起解。

### 2. 代价模型只覆盖“当前指令 + 当前 repair”

`MappingCost` 的形式是 `LocalFreq * LocalCost + NonLocalCost`：

- `LocalCost`：当前指令在候选 mapping 上的代价，加上同一块内 repair COPY 的代价。
- `NonLocalCost`：跨块 repair 的代价，按边频率/块频率折算。
- `LocalFreq`：当前块的频率。

它已比“只看指令数”聪明，但仍只评估当前指令的候选。它不会问“如果我在这里选 FPR，后面三条指令会不会都要 COPY”，也不会问“如果把值留在 GPR，循环里的 PHI 会不会更便宜”。5% 的 split bias 也是局部规则：repair 点可放多处时稍微偏向拆分，避免 COPY 集中在热边；它不是全局最优，而是减少局部决策下的最坏情况。

### 3. 复制的合法性条件

如果要认真设计 instruction duplication，至少要过下面这张表（`guided`，设计推演，不是 23.1.4 的实现）：

| 条件 | 原因 | 反例 |
| --- | --- | --- |
| 纯函数、无副作用 | 复制会多执行一次 | `G_STORE`、`G_CALL` |
| 可安全推测 | 复制可能把执行提前到不该执行的路径 | 可能 trap 的除法、volatile load |
| 非 terminator / 非 PHI | 复制会破坏 CFG 或 SSA 形式 | `G_BR`、`G_PHI` |
| 操作数在目标 bank 可得 | 否则要为输入再复制，成本爆炸 | 两个输入都只在源 bank |
| 结果有多个不同 bank 的消费者 | 否则复制没有收益 | 只有一个消费者 |
| 代码体积/寄存器压力可接受 | 复制不是免费的 | 热循环里的大段计算 |

当前代码里能看到的“邻近机制”只有：`applyMappingImpl` 对 `G_CONSTANT` 的重物化；`G_LOAD` alternative 让结果直接落 GPR/FPR；`G_OR`/`G_BITCAST` 在两个 bank 间二选一。这些是“选一个 bank”，不是“同一指令做两份”。

### 4. 与 ISel / RA 的契约

RegBankSelect 的输出必须能被 `InstructionSelect` 接住，契约有三层：

1. **bank → regclass**：ISel pattern 期望某个 bank 的 vreg 能匹配具体寄存器类；bank 选错，pattern 匹配不上。
2. **COPY 的合法性**：repair 插入的 `G_COPY` 必须在目标 bank 之间有合法实现，否则 ISel/RA 会失败。
3. **RA 的可合并性**：同一 regclass 的 COPY 可能被 coalesce；跨 bank COPY 往往会变成真实 `FMOV`/`MOV`。MIR 里的 COPY 数不等于最终指令数。

`RegBankSelect::init` 还有一条硬断言：

```cpp
RBI = MF.getSubtarget().getRegBankInfo();
assert(RBI && "Cannot work without RegisterBankInfo");
```

新 target 没有 `RegisterBankInfo`，pass 直接断言失败；提供了 RBI 但 ISel 不支持某个 bank，问题会推迟到 ISel 才暴露——这是 review 时最容易被忽略的坑。

## 三个例子

### 例 1：读 header 的 “future work”（源码依据）

把 `RegBankSelect.h` 和 `RegBankSelect.rst` 并排读：

```cpp
/// At higher optimization level, we could imagine that the reg bank selector
/// would use more global analysis and do crazier thing like duplicating
/// instructions and so on. This is future work.
```

```text
* **Global** --- Across multiple instructions, pick the cheapest combination of
  bank assignments.
```

两处都是“未来”语气，但信息量不同：header 说的是算法方向（global analysis + instruction duplication），rst 说的是模式命名（Global mode，跨指令选最便宜组合）。它们都没承诺时间表。读源码时要把“设计意图”和“已实现行为”分开：`Mode` 只有 `Fast`/`Greedy`，命令行只有两个 flag，这就是 23.1.4 的事实。

### 例 2：复制什么时候划算（guided，纸面推演）

假设 target 允许同一条 `G_ADD` 在 GPR 和 FPR 各生成一份（**上游 AArch64 23.1.4 不允许**）。用 AArch64 的 copyCost 量级：GPR→FPR = 4，FPR→GPR = 5。

场景 A：`%r = G_ADD %a, %b`，`%a`、`%b` 已在 GPR，`%r` 同时被 GPR 和 FPR 消费者使用。

```text
方案 1（不复制）：G_ADD 在 GPR，FPR 消费者前插 COPY
    cost = 1 (G_ADD) + 4 (GPR→FPR) = 5

方案 2（复制到 FPR）：再生成一份 G_ADD，但两个输入要先搬到 FPR
    cost = 1 (原 G_ADD) + 2 (FPR 版 G_ADD，假设)
         + 4 + 4 (两个输入 GPR→FPR) = 11
```

方案 2 更差。原因不是复制本身贵，而是输入也要复制；复制会沿数据流向上扩散。

场景 B：`%c = G_CONSTANT i32 42`，同时被 GPR 和 FPR 消费者使用。**假设 target 允许在 FPR 物化整型常量**；AArch64 23.1.4 的 `applyMappingImpl` 里 `G_CONSTANT` 分支只处理 GPR 上小于 32 位的标量，并断言 Dst 在 GPR，所以下面只是推演。

```text
方案 1：常量在 GPR，FPR 消费者前插 COPY
    cost = 1 (G_CONSTANT) + 4 (GPR→FPR) = 5

方案 2：在 FPR 重新物化常量
    cost = 1 (GPR 常量) + 1 (FPR 常量，假设) = 2
```

方案 2 更好，因为常量是叶子，没有输入要搬。`applyMappingImpl` 里的 `G_CONSTANT` 重物化只处理 GPR 上小于 32 位的标量（为了避开 AArch64 没有 8/16 位 GPR 指令的问题），不是跨 bank；但收益逻辑相同：真正的 `G_CONSTANT` 重物化（GPR 内的小位宽常量）也说明，复制/重物化是否划算，取决于能否在目标 bank 低成本地重新制造，而不是指令本身有多简单。

场景 C：`%v = G_LOAD ...`，结果同时被 GPR 和 FPR 消费者使用。复制 load 会多一次内存访问；volatile/atomic 直接错语义；普通 load 也会增加 cache 压力和延迟。所以上游给 `G_LOAD` 的 64 位形式一个 alternative mapping（选结果 bank），而不是复制 load。

三个场景说明：通用 duplication 的难点不在“克隆一条 MI”，而在“判断克隆的闭包是否便宜、是否合法”。局部看一条指令，根本算不清这笔账——这也是 header 把它和 global analysis 放在同一句的原因。

### 例 3：legalizer 如何让 CC bank 不出现（源码依据）

把 `G_ICMP` 的关键行单独看：

```cpp
.legalFor({{i32, i32}, {i32, i64}, {i32, p0}})
.clampScalar(0, s32, s32)
```

`legalFor` 的第一个类型是结果类型 `i32`；`clampScalar(0, s32, s32)` 把结果夹在 `s32`。因此 IR 里的 `i1` 比较结果在 legalize 后变成 `s32`；普通 AArch64 路径下，`s32` 整数值默认落 GPR，CC bank 主要用于需要显式条件码的场景，而不是“所有布尔值”。

启示是：**bank 的选择不只由 mapping 表决定，还由 legalizer 决定 opcode/类型，由 combiner 决定指令形状。** 如果发现某个值“应该在 CC 却在 GPR”，先检查 legalizer 规则，而不是急着改 `getInstrMapping`。具体 MIR 会随 pipeline 和 `optnone` 变化；本章不伪造 dump，可用 Ch 16 的方法停在 `-stop-after=regbankselect` 验证。

## 对比与取舍

| 方案 | 编译时间 | 代码质量 | 实现/维护复杂度 | 23.1.4 状态 |
| --- | --- | --- | --- | --- |
| Fast | 最低 | 仅保证“能选”，不比较候选 | 低 | 默认模式 |
| Greedy（局部） | 中 | 在当前指令范围内最小化 | 中；依赖 target 提供 alternative | 已实现 |
| Global | 高 | 理论最优或接近最优 | 很高；需要全局求解与确定性 tie-break | 未实现，文档称未来工作 |
| 指令复制/重物化 | 中到高 | 可消除 COPY，但可能增大代码体积 | 高；需要合法性、闭包与压力分析 | 仅局部重物化（如 G_CONSTANT） |
| TableGen 静态 mapping | 运行期最低 | 取决于表的质量 | 基础设施工作量大 | bank/coverage 已生成，mapping 手写 |

没有“赢家”，选择取决于 target 成熟度：bring-up 阶段用 Fast + 少量 mapping 先保证能选；有明确 FP/向量热点时给少数 opcode 加 alternative，让 Greedy 有东西可选；研究/实验可在离线工具里做 Global 或 duplication 原型；长期维护则推动 TableGen 化，减少手写表的不一致。

## 踩雷集锦

1. **以为 `Global` mode 已经能用。** `Mode` 枚举只有 `Fast` 和 `Greedy`，命令行也只有 `-regbankselect-fast` / `-regbankselect-greedy`；rst 里的 Global 是 `intend to eventually introduce`。不要在脚本里写 `-regbankselect-global`，它不存在。

2. **把 instruction duplication 和 repair COPY 当成一回事。** repair 是“搬值”，插 `G_COPY`，不改变计算；duplication 是“多做一份计算”，克隆定义指令，可能消除 COPY，但会增加指令数、寄存器压力和合法性风险。两者代价模型和实现位置都不同。

3. **以为 `AArch64GenRegisterBankInfo.def` 是 TableGen 生成的。** 文件名有 `Gen`，但文件头明确写着 `\todo This should be generated by TableGen.`；23.1.4 里它仍是手写文件，TableGen 只生成 bank ID、覆盖和 size（`AArch64GenRegisterBank.inc`）。

4. **以为 `-O0` 一定走 Fast。** 强制 Fast 的是函数上的 `optnone`，不是 `llc -O0` 本身。`runOnMachineFunction` 里有 `if (F.hasOptNone()) OptMode = Mode::Fast;`；没有 `optnone` 时，`-regbankselect-greedy` 在 `-O0` 下仍然生效。

5. **以为全局分析/复制一定更好。** Global 求解可能增加编译时间、破坏确定性；复制可能增大代码体积、提高寄存器压力，甚至改变语义（不安全的推测）。任何这类改动都需要基准数据、MIR 测试和合法性论证，不能凭“看起来少了几条 COPY”下结论。

## 进阶

### 如果要在上游做 Global 或 duplication

现实做法不是直接改 `RegBankSelect.cpp`，而是先做可验证的原型：

1. **离线分析**：用 `-stop-after=regbankselect` 导出 legal MIR，统计每条指令的候选 bank、跨 bank 边、块频率。
2. **建立基线**：记录 Fast/Greedy 的 COPY 数、指令数、代码大小、编译时间。
3. **做区域求解**：先只优化单个基本块或单个循环，不要一上来做全函数 ILP。
4. **验证确定性**：同样输入必须产生同样输出；tie-break 要显式。
5. **证明合法性**：复制只用于纯、可推测、无副作用的指令；PHI/临界边单独处理。
6. **回到上游讨论**：先发 RFC，带上数据和最小复现，再谈实现。

### 贡献路径

不一定非要写大算法，以下几条都有人需要：

- **Bug report + 最小 MIR**：说明版本、triple、`llc` 命令、`-stop-after=regbankselect` 的输出、期望与实际。能稳定复现的 MIR 比一段 IR 更有价值。
- **补 MIR/lit 测试**：给现有 mapping、alternative、repair 行为加回归测试，尤其是 Fast/Greedy 的差异。
- **补 target 支持**：给 RISC-V 这类缺少 alternative 的 target 加一两个明确候选，附 ISel 支持和测试。
- **推动 TableGen 化**：先写清楚“哪些 mapping 可以静态表达、哪些必须保留 C++ 谓词”，再改 emitter。
- **修文档**：`copyCost` 方向、Fast 的默认性、CC 不是默认布尔 bank，这些点在实际使用中经常被误解。

### 读源码的顺序

建议按依赖顺序，而不是文件字母序：

1. `RegBankSelect.h`：先读文件头的 cost 公式和 `Mode`，建立“它在优化什么”的直觉。
2. `RegBankSelect.cpp`：跟 `runOnMachineFunction` → `assignRegisterBanks` → `computeMapping` → `applyMapping`。
3. `RegisterBankInfo.h`：看 `PartialMapping`/`ValueMapping`/`InstructionMapping` 和 `copyCost` 的契约。
4. `RegisterBankInfo.cpp`：看 `getInstrMappingImpl` 的 copy-like 特判和 regclass 兜底。
5. `AArch64RegisterBankInfo.cpp` + `.def`：看成熟 target 如何组合表、策略、特例。
6. `RegisterBankEmitter.cpp`：看哪些东西已由 TableGen 生成，边界在哪里。
7. `RISCVRegisterBankInfo.cpp`：看更薄的实现，理解“缺的是支持，不是算法”。

## 动手练习

1. 打开 `RegBankSelect.h` 和 `RegBankSelect.rst`，各列三条“已实现”和三条“未来工作”的陈述，并说明依据哪一行代码判断。
2. 用纸面推演一个菱形数据流：`%v` 同时被 GPR 和 FPR 消费者使用。分别计算“插 COPY”“复制定义指令”“重物化”三种方案的成本，写出合法性前提。
3. 用 Ch 0 的 `or.ll` 跑 Fast 与 Greedy（guided），数出两种模式的 COPY 数，并解释为什么 Greedy 选 FPR。不要凭记忆写数字，以你自己的输出为准。
4. 在 `AArch64RegisterBankInfo.cpp` 里找一处依赖 use 分析的 mapping（提示：`isLoadFromFPType`、`onlyUsesFP`），解释为什么它很难直接交给 TableGen 生成。
5. 写一份 bug report 模板：版本、triple、最小 IR/MIR、`llc` 命令、`-stop-after=regbankselect` 的关键片段、期望与实际、是否 fallback。用 Ch 18 的诊断树检查是否遗漏关键信息。

## 本章重点整理

- 局部贪心是明确的设计取舍，不是临时缺陷；header 和 rst 都把它写成现状。
- 23.1.4 只有 Fast 和 Greedy 两种模式；Global 是文档中的未来工作。
- Global 的本质是整张数据流图的 bank 划分，需要处理循环、PHI、临界边、确定性和编译时间。
- instruction duplication 与 repair COPY 不同：前者克隆计算，后者搬值；复制受纯度、可推测性、输入可得性和代码体积约束。
- Legalizer 决定 opcode/类型，Combiner 决定指令形状；RegBankSelect 的输入已被它们塑造。
- TableGen 目前生成 bank ID、覆盖、size 和 `getRegBankFromRegClass` 查表；mapping 表仍手写，两个 RBI 都有 `\todo`。
- RegBankSelect 与 ISel/RA 有硬契约：bank 必须能被 ISel 选中，COPY 必须合法，RA 可能合并部分 COPY。
- 参与上游的最小路径是“最小 MIR + 可复现命令 + 期望/实际 + 测试”，而不是先写大算法。

## 自我检核

- [ ] 能说出 23.1.4 的 `Mode` 枚举有哪两个值，以及 Global 为什么不在其中
- [ ] 能解释局部贪心为什么“不能回头”，并举出 PHI 或临界边的例子
- [ ] 能区分 repair COPY 与 instruction duplication，各说出一个适用场景
- [ ] 能说出 Legalizer/Combiner 如何改变 RegBankSelect 看到的 opcode 或类型
- [ ] 能解释 TableGen 已经生成了什么、还没生成什么
- [ ] 能列出至少三条 RegBankSelect 与 ISel/RA 的契约
- [ ] 能写出一份包含最小 MIR 与复现命令的 bug report 提纲
- [ ] 能根据 target 成熟度选择 Fast、Greedy、alternative 或离线原型

## 延伸阅读

- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：文件头的 local decisions / future work 段落、`Mode` 枚举、`MappingCost`。
  - **学什么**：当前算法的自我定位，以及“全局分析 + 指令复制”为什么被放在未来工作里。
  - **和本章的关联**：例 1 和“局部贪心”一节的直接来源。
- **[RegBankSelect.rst](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：Fast/Greedy 两小节、Global 的未来模式、AArch64 的 NOTE、两个 TODO。
  - **学什么**：官方对模式、TableGen 化和 AArch64 `-O0` 取舍的表述。
  - **和本章的关联**：区分“文档意图”和“23.1.4 实现”的关键材料。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：文件头的 `\todo`、`copyCost`、`applyMappingImpl` 的 `G_CONSTANT` 分支、`getInstrAlternativeMappings`。
  - **学什么**：成熟 target 如何在局部框架里用重物化和少量 alternative 解决问题。
  - **和本章的关联**：例 2 的“重物化”直觉和“复制受限”的现实依据。
- **[RegisterBankEmitter.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/utils/TableGen/RegisterBankEmitter.cpp)**
  - **读哪里**：`visitRegisterBankClasses`、`emitBaseClassImplementation`、覆盖位图的生成。
  - **学什么**：TableGen 已经能生成哪些静态信息，为什么 mapping 还没被纳入。
  - **和本章的关联**：理解“TableGen 化”的边界，避免把 `.def` 误当成生成物。
- **[LLVM GlobalISel 文档](https://llvm.org/docs/GlobalISel/)**
  - **读哪里**：Pipeline、gMIR、RegBankSelect 的上下游关系。
  - **学什么**：把 RegBankSelect 放回整条流水线，理解 Legalizer/Combiner/ISel 的分工。
  - **和本章的关联**：先建立全局视角，再回读单章源码，避免“只见 mapping 表，不见流水线”。

课程到这里就结束了。你已经走完“观察现象 → 理解数据模型 → 跟算法 → 读 target 源码 → 自己写 RBI → 评估设计空间”的闭环。下一步不是继续读，而是动手：给 AArch64 加一个 feature-gated 的 custom opcode，写默认 mapping、FPR alternative、copyCost/repair 和 Fast/Greedy 的 MIR 测试。做到能解释每一条 COPY 的来源，这门课才算真正学完。

→ [Final Project：给 AArch64 加一个 feature-gated custom bank/mapping](./final-project-aarch64-custom-regbank.md)