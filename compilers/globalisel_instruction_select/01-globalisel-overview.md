# Ch 1 — GlobalISel 全景与 ISel 的位置

> **目标**：建立 GlobalISel 的完整心智模型：它不是“另一个 pattern matcher”，而是一套分阶段的框架；`InstructionSelect` 只是其中负责把 banked gMIR 变成 target MIR 的一站。学完能说清每个阶段的职责、边界，以及失败时该往哪里查。

> **环境**：LLVM 23.1.4，AArch64 主線，RISC-V 对照。命令沿用 Ch 0 的 `build/bin/llc`。

## 为什么需要这个？

很多人第一次接触 GlobalISel，会把它理解成“SelectionDAG 的替代品”：输入 IR，输出汇编，中间有一堆 pattern。这个理解会立刻导致三个错误：

1. 把 legalize 失败当成 selector 失败；
2. 看到 `G_ADD` 没被选成 `ADDXrr`，就去翻 `AArch64InstructionSelector.cpp`，但其实 Legalizer/RegBankSelect 还没准备好；
3. 以为“Global”是指跨 function 或整个 module 做全局优化。

实际上，GlobalISel 是一套**分阶段框架**，每个阶段有独立的输入/输出契约；`InstructionSelect` 只负责最后一站。你要先知道这一站在哪、收到什么、必须交出什么，后面的源码才有意义。

## 先建立直觉

把编译器的 backend 想成一条机场行李分拣线：

```text
LLVM IR
  │ IRTranslator：把 IR 翻译成通用 MIR（gMIR）
  ▼
gMIR
  │ Legalizer：把 target 不会处理的操作/类型改写成合法形态
  ▼
legal gMIR
  │ RegBankSelect：决定每个虚拟寄存器走哪个 register bank
  ▼
banked gMIR
  │ InstructionSelect：把 generic 操作换成 target 指令
  ▼
selected MIR
  │ RA / Scheduler / MC
  ▼
assembly
```

`InstructionSelect` 是“换登机牌”的那一站：它不负责决定行李该不该带上飞机（Legalizer），也不负责分配座位（RegBankSelect）；它只把“通用描述”换成“这架飞机能执行的具体指令”。

“Global”指的是：**在一个 `MachineFunction` 内，选择作用域跨 basic block**，而不是跨 function。它仍然是 per-function 的 pass。

## GlobalISel 是什么，不是什么

**是**：

- 一套以 MIR 为中心的 instruction selection 框架。
- 一组有明确契约的 pass：IRTranslator、Legalizer、RegBankSelect、InstructionSelect。
- 一套 target 扩展点：`InstructionSelector`、`LegalizerInfo`、`RegisterBankInfo`、`CallLowering`。
- 一套 TableGen 工具链：从 SelectionDAG pattern 导入 matcher table，也支持 GlobalISel 自己的 combiner。

**不是**：

- 不是单个 pass，也不是单个算法。
- 不是“把 SelectionDAG 换一层皮”：它没有 per-BB DAG，直接在 MIR 上工作。
- 不是完整的 target port：你仍然要提供指令定义、寄存器类、calling convention、scheduling model。
- 不是“一定比 SelectionDAG 快”：它在 `-O0` 的 compile time 有优势，但 `-O2` 的代码质量/覆盖仍取决于 target 成熟度。

## 五个阶段的职责

| 阶段 | 输入 | 输出 | 核心问题 | 失败会怎样 |
|---|---|---|---|---|
| IRTranslator | LLVM IR | gMIR | IR 指令怎么变成 `G_*`？ | `failedISel`，fallback |
| Legalizer | gMIR | legal gMIR | target 不会的操作/类型怎么改写？ | `failedISel`，fallback |
| RegBankSelect | legal gMIR | banked gMIR | 每个 vreg 走哪个 bank？ | `failedISel`，fallback |
| **InstructionSelect** | banked gMIR | selected MIR | 用哪条 target 指令？ | `failedISel`，fallback |
| FinalizeISel / RA / Scheduler | selected MIR | 可分配/可调度 MIR | 伪指令展开、寄存器分配、调度 | 通常不是 fallback，而是编译错误 |

此外还有可选的优化 pass：

- **Combiner**：在 legalize 前/后、regbankselect 前/后，把多条 gMIR 合并成更优形态。
- **Localizer**：把某些跨 BB 的操作拉回使用点附近，方便后续选择/调度。
- **LoadStoreOpt**：处理 load/store 的合并/优化。
- **CSEMIRBuilder**：在选择/组合过程中做 CSE，避免重复构造相同指令。

这些 pass 会影响 `InstructionSelect` 看到的输入形态，但**不改变 ISel 的契约**：进来的必须是 legal、banked 的 gMIR，出去必须没有 pre-ISel generic opcode。

## InstructionSelect 在代码里的两层结构

这是最容易混淆的地方：`InstructionSelect` 和 `InstructionSelector` 不是同一个东西。

### 第一层：`InstructionSelect` pass

文件：`llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`

它是 MachineFunctionPass，负责：

1. 检查输入是否 legal（assertions build）；
2. 按 **post-order** 遍历 basic block；
3. 在每个 block 内 **从后往前**（reverse）遍历指令；
4. 对每条指令调用 target 的 `InstructionSelector::select()`；
5. 失败时设置 `failedISel`；
6. 成功后清理类型、删除不可达 block、做 COPY 清理。

核心循环（简化自 LLVM 23.1.4）：

```cpp
for (MachineBasicBlock *MBB : post_order(&MF)) {
  MIIMaintainer.MII = MBB->rbegin();
  for (auto End = MBB->rend(); MIIMaintainer.MII != End;) {
    MachineInstr &MI = *MIIMaintainer.MII;
    ++MIIMaintainer.MII;

    if (!selectInstr(MI)) {
      reportGISelFailure(MF, MORE, "gisel-select", "cannot select", MI);
      return false;
    }
  }
}
```

> **注意**：上面的循环省略了真实代码里的若干分支。`AArch64InstructionSelector::select()` 还会单独处理 `LOAD_STACK_GUARD`、普通 `COPY`（直接判成功，留给后续 pass）、以及 debug 指令等情形；删掉它们只是为了让控制流更清楚。完整实现见 `AArch64InstructionSelector.cpp`。

`post_order(&MF)` + `rbegin()` 不是实现细节，而是 GlobalISel 的核心设计：

- **uses before defs**：先看到使用者的指令，再看到定义者的指令。
- 选完一条指令后，如果它的结果没人用，可以立刻做 trivial dead code elimination。
- 可以在选择当前指令时，沿着 use-def 链把生产者折进来（fold），甚至跨 basic block。

### 第二层：`InstructionSelector` target API

文件：`llvm/include/llvm/CodeGen/GlobalISel/InstructionSelector.h`

它只声明一个纯虚函数：

```cpp
virtual bool select(MachineInstr &I) = 0;
```

契约写得很清楚：

- 输入是一条可能还是 generic 的 `MachineInstr`。
- 成功返回 `true`，并且所有被修改/插入的指令都不能是 pre-ISel generic opcode。
- 失败返回 `false`；pass 会把 function 标记为 `failedISel`，然后由上层决定是否 fallback。

`InstructionSelector` 本身不实现 matcher；它继承自 `GIMatchTableExecutor`，后者才是 matcher table 的执行器。target 的 `select()` 通常会调用由 TableGen 生成的 `selectImpl()`。

## AArch64 的 `select()` 长什么样

以 LLVM 23.1.4 的 `AArch64InstructionSelector::select()` 为例，控制流大致是：

```cpp
bool AArch64InstructionSelector::select(MachineInstr &I) {
  if (Subtarget->requiresStrictAlign()) {
    // 当前不支持 strict-align，直接失败
    return false;
  }

  // 非 generic 指令 / G_PHI / COPY / debug 指令的特殊处理
  if (!I.isPreISelOpcode() || Opcode == TargetOpcode::G_PHI) {
    ...
    return true;
  }

  // 先做 ISel 前的 lowering；可能改写 opcode，但选择还要继续
  if (preISelLower(I))
    Opcode = I.getOpcode();

  // 某些手写选择优先于导入的 pattern
  if (earlySelect(I))
    return true;

  // TableGen 生成的 matcher
  if (selectImpl(I, *CoverageInfo))
    return true;

  // 剩下的交给手写 switch
  switch (Opcode) {
  case TargetOpcode::G_SBFX:
  case TargetOpcode::G_UBFX:
    ...
  case TargetOpcode::G_BRCOND:
    ...
  }

  return false;  // 没选出来
}
```

四个关键点：

1. **`preISelLower`**：还在 ISel 阶段，但先做“选择前的 lowering”。它可能改写 opcode，所以后面要重新读 `I.getOpcode()`。
2. **`earlySelect`**：给某些手写选择优先权，避免被导入的 pattern 抢先选成次优序列。
3. **`selectImpl`**：TableGen 生成的 matcher，负责大部分常规 pattern。
4. **`switch` + `return false`**：pattern 覆盖不到的手写 case；仍然失败就返回 `false`，交给 fallback。

这也解释了为什么“为什么这条 IR 没被选上”不能只查 pattern：可能被 `preISelLower` 改写了，可能被 `earlySelect` 拦了，也可能在 `switch` 里没命中。

## “Global”到底 global 在哪？

GlobalISel 的“全局”有三个层次，别混：

| 说法 | 是否正确 | 解释 |
|---|---|---|
| 跨 basic block 选择 | ✅ | pass 以 function 为单位，block 之间可以 fold/看 use-def |
| 跨 function 选择 | ❌ | 仍然是 per-`MachineFunction`；不做链接时/跨函数选择 |
| 整个 module 全局优化 | ❌ | 那不是 ISel 的职责；IR 层优化在更早的 pipeline |

跨 BB 的能力来自 post-order + use-def 链。例如一个值在 `bb1` 定义、在 `bb2` 使用，selector 在 `bb2` 先看到使用者，可以沿着 vreg 的 def 找到 `bb1` 的生产者，判断是否能 fold。SelectionDAG 每个 BB 单独建 DAG，看不到这种跨 BB 关系。

但“能跨 BB”不等于“随便跨 BB 折”：还要满足副作用、单 use、寄存器约束等条件。Ch 9 会讲 `GIM_CheckIsSafeToFold` 和 `GIM_RecordInsn`。

## 失败与 fallback 的真实路径

当 `select()` 返回 `false`：

```text
InstructionSelect::selectInstr()
  └─ ISel->select(MI) == false
       └─ reportGISelFailure(...)
            ├─ MF.getProperties().setFailedISel()
            └─ 发出 remark（abort=2）或 fatal error（abort=1）

InstructionSelect::runOnMachineFunction()
  └─ 返回 false

TargetPassConfig::addCoreISelPasses()
  ├─ 如果 abort=1：不添加 SelectionDAG，编译失败
  └─ 如果 abort=0/2：添加 SelectionDAG 作为 fallback
```

关键源码在 `llvm/lib/CodeGen/GlobalISel/Utils.cpp`：

```cpp
void llvm::reportGISelFailure(MachineFunction &MF,
                              MachineOptimizationRemarkEmitter &MORE,
                              MachineOptimizationRemarkMissed &R) {
  MF.getProperties().setFailedISel();
  reportGISelDiagnostic(DS_Error, MF, MORE, R);
}
```

以及 `TargetPassConfig.cpp`：

```cpp
if (Selector == SelectorType::GlobalISel)
  addPass(createResetMachineFunctionPass(
      reportDiagnosticWhenGlobalISelFallback(), isGlobalISelAbortEnabled()));

if (Selector != SelectorType::GlobalISel || !isGlobalISelAbortEnabled())
  if (addInstSelector())
    return true;
```

所以 `failedISel` 不是“selector 内部的一个 bool”，而是 **MachineFunction 的属性**：它告诉后续 pass“这个 function 的 GlobalISel 已经失败，别再往下选了”。

## 与 SelectionDAG、FastISel 的对比

| 维度 | SelectionDAG | FastISel | GlobalISel |
|---|---|---|---|
| 工作对象 | per-BB DAG | per-BB IR/MI | per-function MIR |
| 作用域 | 单个 basic block | 单个 basic block | 跨 basic block（function 内） |
| 主要目标 | 代码质量 | 极快编译 | 可扩展的现代框架；`-O0` 编译速度 |
| Pattern 来源 | `*InstrInfo.td` 的 DAG pattern | 手写 C++ 为主 | SelectionDAG pattern 导入 + 手写 selector |
| 失败回退 | 通常无 | 回退 SelectionDAG | 回退 SelectionDAG（可配置） |
| 典型 target | 所有成熟 target | X86/ARM 等 | AArch64、AMDGPU、RISC-V（部分） |

注意：GlobalISel 并不是“所有情况都比 SelectionDAG 好”。它的优势主要在框架统一、`-O0` 编译速度、可测试性（MIR 可序列化）；代价是 target 需要补大量 pattern/手写 case，且 fallback 路径增加调试复杂度。

## LLVM 23.1.4 的 target 成熟度

- **AArch64**：最成熟。`-O0` 默认 GlobalISel（`-aarch64-enable-global-isel-at-O=0`）；`-O1` 及以上默认 SelectionDAG，需显式 `-global-isel`。Ch 0 已验证。
- **RISC-V**：已有 GlobalISel 支持，基本整数操作能跑，但覆盖范围明显小于 AArch64；很多 case 会 fallback。Ch 22 会系统对照。
- **X86**：有 GlobalISel 支持，但成熟度和默认使用范围远不如 AArch64；本课不做主線。
- **AMDGPU**：GlobalISel 使用较深，但属于 GPU backend，和本课 AArch64 主線差异大。

**GlobalISel 的成熟度是 per-target 议题**。看到“LLVM 支持 GlobalISel”不等于“你的 target 的所有指令都能被 GlobalISel 选择”。判断方法永远是：跑 `-global-isel-abort=1`，看有没有 fallback。

## 为什么迁移这么慢？

1. **pattern 覆盖**：SelectionDAG 积累了几千条 pattern，GlobalISel 要逐条导入/重写。
2. **手写 C++ case**：很多 target 特殊指令、ABI、intrinsic 没有通用 pattern，只能写 selector。
3. **向量/浮点/原子/内联汇编**：这些领域的语义复杂，pattern 和 legalize 都需要大量工作。
4. **性能 parity**：`-O2` 代码质量要追平 SelectionDAG，否则生产环境不敢切。
5. **fallback 的隐性成本**：fallback 让“部分支持”可用，但也让测试/调试更复杂。

所以现实策略通常是：**新 target 或 `-O0` 先走 GlobalISel；成熟 target 的主力仍是 SelectionDAG，逐步扩大 GlobalISel 覆盖**。

## 一个最小例子在框架中的位置

回顾 Ch 0 的 `add`：

```text
IR:        %r = add i64 %a, %b
IRTranslator:  %2:_(i64) = G_ADD %0, %1
Legalizer:     legalized: true
RegBankSelect: %2:gpr(i64) = G_ADD %0, %1
InstructionSelect: %2:gpr64 = ADDXrr %0, %1
```

这段变化对应：

- `IRTranslator` 把 LLVM IR 的 `add` 映射成 generic `G_ADD`。
- `Legalizer` 确认 i64 `G_ADD` 在 AArch64 legal（body 不变，只设属性）。
- `RegBankSelect` 把 vreg 放进 `gpr` bank。
- `InstructionSelect` 用 TableGen matcher 命中 `ADDXrr`，并把寄存器类收紧为 `gpr64`。

如果你在 `InstructionSelect` 之前就看到 `ADDXrr`，说明有 pass 做了不该做的事；如果在 `InstructionSelect` 之后还看到 `G_ADD`，说明契约被破坏。

## 踩雷集锦

1. **把 GlobalISel 当成一个 pass**：它是一条 pipeline；`-global-isel` 只是打开整条 pipeline 的开关。
2. **把 legalize 失败当成 selector 失败**：先看 `failedISel` 是在哪个 pass 设置的；`-debug-only=global-isel` 和 `-pass-remarks-missed='gisel*'` 会告诉你。
3. **以为 Global 是跨 function**：它是 per-`MachineFunction`，只是跨 basic block。
4. **以为 pattern 导入 = 全部支持**：SelectionDAG pattern 有一部分无法导入（PatLeaf、ComplexPattern、custom SDNode），需要 target 自己补。
5. **忽略 fallback**：默认 `abort=0` 时，`failedISel` 的 function 会被 SelectionDAG 接管；最终汇编正确不代表 GlobalISel 成功。
6. **把 `InstructionSelect` 和 `InstructionSelector` 混为一谈**：前者是 pass driver，后者是 target API；读源码时先分清文件。

## 进阶：接下来会遇到的相邻组件

- **Combiner**：在 ISel 前把多条 gMIR 合成更优形态；Ch 23 会讲它和 ISel 的边界。
- **GISelValueTracking**：在选择/组合时提供 known bits 等值跟踪，帮助判断常量/掩码。
- **Localizer**：把跨 BB 的操作拉回使用点，减少选择时的跨 BB 复杂度。
- **LoadStoreOpt**：处理 load/store 的合并与优化，影响 ISel 看到的 MIR。
- **CSEMIRBuilder**：在选择过程中构造指令时做 CSE，避免重复。
- **CodeGenCoverage**：`selectImpl` 会记录命中的 pattern rule。只有在 configure 时开启 `LLVM_GISEL_COV_PREFIX` 的 instrument 构建里，运行时 `-gisel-coverage-prefix=<path>` 才会把覆盖率写到文件；官方预编译包通常没有这个能力。

## 动手练习

1. **画 pipeline**：不看笔记，画出 IRTranslator → Legalizer → RegBankSelect → InstructionSelect 的输入/输出，并标出三个 MachineFunctionProperties。
2. **找 pass 边界**：在 `InstructionSelect.cpp` 中找到 `post_order`、`rbegin`、`reportGISelFailure` 三处，解释它们各自解决什么问题。
3. **读 AArch64 `select()`**：找出 `preISelLower`、`earlySelect`、`selectImpl`、`switch`、`return false` 的位置，说明为什么顺序不能随便换。
4. **制造 fallback**：用 Ch 0 的 `strict.ll` 跑 `-global-isel-abort=2`，确认 `failedISel` 是在哪个 pass 设置的。
5. **对比 target**：用同一个 `add.ll` 跑 AArch64 与 RISC-V，观察 `selected` MIR 的差异。

## 本章重点整理

- GlobalISel 是分阶段框架，不是单个 pattern matcher；`InstructionSelect` 只是最后一站。
- `InstructionSelect` pass 负责 post-order、reverse 遍历、失败标记；`InstructionSelector` 是 target 实现的 `select(MI)` API。
- “Global”指 function 内跨 basic block，不是跨 function/module。
- `select()` 的顺序是 `preISelLower` → `earlySelect` → `selectImpl` → 手写 switch → `return false`。
- 失败通过 `reportGISelFailure` 设置 `failedISel`，再由 `TargetPassConfig` 决定是否 fallback 到 SelectionDAG。

## 自我检核

- [ ] 能解释 `InstructionSelect` 与 `InstructionSelector` 的区别
- [ ] 能说出 post-order + reverse 遍历带来的两个好处
- [ ] 能解释 `failedISel` 是谁设置的、谁消费的
- [ ] 能说出 AArch64 `select()` 的四个阶段及顺序
- [ ] 知道 GlobalISel 的“global”边界是 function，不是 module
- [ ] 能说出至少两个 target 成熟度差异

## 延伸阅读

- **[GlobalISel Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - 读 “Core Pipeline” 与 “MachineVerifier” 两节；本章的 pass 职责表和属性契约都来自这里。Ch 2 会逐条拆开 `InstructionSelect` 的 postcondition。
- **[InstructionSelect](https://llvm.org/docs/GlobalISel/InstructionSelect.html)**
  - 读开头的 “This pass transforms generic machine instructions...” 与 “API: InstructionSelector”；本章的 `selectImpl`、`select()` 契约、ComplexPattern 说明都以此为准。
- **[Porting GlobalISel to a New Target](https://llvm.org/docs/GlobalISel/Porting.html)**
  - 读 target 需要实现哪些类（InstructionSelector、LegalizerInfo、RegisterBankInfo、CallLowering）；本章的“不是完整 target port”边界在这里有完整清单。
- **[LLVM 源码：InstructionSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp)**
  - 读 `selectMachineFunction()` 与 `selectInstr()`；本章的 post-order/reverse 循环、`reportGISelFailure` 都在这两个函数里。
- **[LLVM 源码：AArch64InstructionSelector.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp)**
  - 先读 `select()` 的前 120 行，不要从头读 8,249 行；本章的 `preISelLower` / `earlySelect` / `selectImpl` / `switch` 顺序就在这里。

## 下一章

[Ch 2 — InstructionSelect 的硬契约](./02-instructionselect-contract.md)
