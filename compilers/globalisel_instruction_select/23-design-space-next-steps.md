# 第 23 章 设计空间与下一步

> **本章目标**：把前 22 章的工具与案例收束成一组可复用的判断：什么时候写 pattern，什么时候写 C++ selector，什么时候引入 pseudo/ComplexPattern，什么时候接受 fallback；以及继续深入 GlobalISel 的路线图。
>
> **前置**：第 1–22 章。**环境**：LLVM 23.1.4。本章以设计讨论为主，不要求新增实验。

---

## 23.1 先画一张“选择责任图”

InstructionSelect 不是一个孤立函数。它站在四个系统的交界处：

```text
frontend IR
   │
   ▼
IRTranslator ──► gMIR（G_* + LLT）
   │
   ▼
Legalizer ─────► 类型/操作合法化
   │
   ▼
RegBankSelect ─► bank 选择
   │
   ▼
InstructionSelect ──► 机器指令 / pseudo
   │
   ▼
pseudo expansion / MC / RA / scheduling
```

每一条“选不出来”的问题，都可以先问：**责任在哪个框？**

| 现象 | 首要怀疑对象 | 不是首要怀疑对象 |
| --- | --- | --- |
| gMIR 里根本没有想要的 `G_*` | IRTranslator / Legalizer | matcher |
| 类型是 `s128`，但 target 只有 `s64` 指令 | Legalizer | pattern 的 `ImmLeaf` |
| `G_ADD` 在 FPR bank，pattern 只接受 GPR | RegBankSelect | C++ `select()` |
| `G_LOAD` 是 atomic，普通 `ldr` pattern 不匹配 | 谓词/内存语义 | 寄存器分配 |
| pattern 存在、bank/type 都对，仍 fallback | matcher/生成表 | Legalizer |
| 选中了 pseudo，但最终汇编错 | pseudo expansion / MC | matcher |
| `selected: true` 且 `failedISel: true` | fallback 契约 | verifier |

这张表不是绝对诊断顺序，但它能阻止你在错误的层里改代码。比如“`i8 add` 没选出来”通常不该去改 `ADDXri` 的 pattern，而该先看 Legalizer 是否已经把它提升成 `s32`。

---

## 23.2 四条实现路线

GlobalISel 里增加一条指令选择，通常有四条路线。它们不是互斥的，而是按“表达力 / 维护成本”排成阶梯。

### 23.2.1 路线 A：纯 TableGen pattern

适合：

- 操作数结构固定；
- 谓词能用 `ImmLeaf`、`PatFrag`、`ComplexPattern` 表达；
- 不需要跨越多个基本块；
- 不需要临时构造复杂 MIR。

优点：声明式、可被 SelectionDAG 与 GlobalISel 共享、容易 review。
缺点：复杂 pattern 会让 matcher 表膨胀，调试信息不如 C++ 直观。

### 23.2.2 路线 B：pattern + C++ 谓词

适合：

- 结构能匹配，但条件依赖 target 状态、多个操作数的关系、或已有 helper；
- 例如“两个操作数必须来自同一个 base register”“必须满足某个 subtarget 的扩展”。

做法是把复杂条件放进 `PatFrag::GISelPredicateCode` 或 C++ predicate，而不是把整条规则搬到 `select()`。这样仍保留 pattern 的结构匹配，失败时也能自然回溯。

### 23.2.3 路线 C：C++ `select()` case

适合：

- 需要构造多条指令、插入 COPY、修改 CFG（注意：ISel 不允许插入基本块）；
- 需要访问 `MachineFunction`、`MachineRegisterInfo`、`TII` 的复杂状态；
- pattern 表达能力不足，或者生成的表太大；
- 需要精确控制 fallback 与错误信息。

代价：可测试性、可移植性、review 成本都更高；必须自己处理寄存器类约束、implicit operands、debug location、flags。

### 23.2.4 路线 D：custom pseudo / target-specific generic opcode

适合：

- 需要一个“中间态”指令，稍后由 pseudo expansion 或另一个 pass 展开；
- 目标指令的最终形态依赖后续信息（例如 frame index、large immediate、寄存器对）；
- 想把选择与展开解耦，让 matcher 只负责识别语义。

代价：多一个 IR 层概念，必须定义 expansion、验证和测试；如果 pseudo 泄漏到最终汇编，verifier 或 MC 会报错。

### 23.2.5 选择顺序

实际工程里建议按这个顺序尝试：

1. 先看现有 pattern 能否复用（`PatFrag`、`ComplexPattern`、`GINodeEquiv`）；
2. 再加一个窄的 C++ 谓词；
3. 仍不够，再写 C++ `select()` case；
4. 最后才引入 custom pseudo / 新 generic opcode。

原因是：每往下一层，维护成本、测试成本和 reviewer 的认知负担都上升。不要把“能写 C++”当成“应该写 C++”。
### 23.2.6 三个具体判断

把上面的阶梯套到三个常见操作上：

| 操作 | 首选 | 理由 | 何时升级 |
| --- | --- | --- | --- |
| `add i64 x, 4095` | 纯 pattern | 结构固定，立即数谓词可表达 | 编码约束无法用 `ImmLeaf` 表达时加 C++ 谓词 |
| `fadd half` | pattern + feature predicate | `FADDHrr` 只在 `+fullfp16` 可用 | 需要多指令 fallback 时写 C++ |
| `atomicrmw` | pattern + 原子谓词，必要时 C++ | ordering/MMO 约束复杂 | 需要构造 loop/CAS 序列时用 C++ 或 pseudo |

判断的核心不是“哪个更高级”，而是“这条逻辑是否只依赖局部结构”。局部结构能表达，就留在 pattern；需要全局信息或副作用序列，就往下走。

---

## 23.3 Pattern 的边界：什么该放进 `.td`

一条 pattern 适合表达“**结构 + 局部约束**”，不适合表达“全局策略”。

适合：

- opcode 树：`G_ADD(G_LOAD, G_CONSTANT)`；
- 类型/bank/立即数约束；
- 局部的 one-use、atomic、alignment 条件；
- 目标指令的固定 operand 布局。

不适合：

- 需要遍历 use-def 图做全局决策；
- 需要根据多个基本块的概率/频率选择；
- 需要插入新基本块；
- 需要复杂的资源分配或调度决策；
- 需要访问尚未稳定的 RA 信息。

一个实用判断：**如果这段逻辑需要写注释解释“为什么在这里做”，它大概率不属于 pattern。** pattern 应该让读者一眼看出“这条 IR 形状对应哪条机器指令”；策略性逻辑应该留在 C++，并有独立测试。

---

## 23.4 fallback 是设计工具，不是垃圾桶

GlobalISel 的 fallback 让新 target 可以渐进式接入：先支持一小部分 opcode，其余交给 SelectionDAG。这个设计非常成功，但也容易被滥用。

### 23.4.1 三种 fallback 心态

1. **临时脚手架**：已知缺口，有 issue/计划，测试明确标注；
2. **永久策略**：某些冷门 opcode 永远交给 SelectionDAG，成本可接受；
3. **隐藏债务**：没有测试、没有统计、没人知道有多少函数在 fallback。

前两种可以是合理设计；第三种会在某天变成“为什么这个函数在 `-O2` 下突然变慢”的根因。

### 23.4.2 如何量化 fallback

至少追踪：

- 有多少函数触发 fallback；
- 触发在哪个 pass/哪类 opcode；
- fallback 后的 SelectionDAG 代码是否真的更优；
- 是否影响 compile time；
- 是否有测试覆盖“失败路径”。

LLVM 的 `-global-isel-abort=1` 适合调试单点，不适合统计全局。真实项目里应结合日志、统计计数器或自定义 instrumentation。不要用“我跑了几个 case 没 fallback”代替覆盖率数据。

### 23.4.3 什么时候可以接受 fallback

- 该 opcode 极其冷门，补 pattern 的收益低于维护成本；
- SelectionDAG 已经能生成正确且性能可接受的代码；
- GlobalISel 路径还没准备好，但 feature flag 可以控制；
- 有明确的迁移计划和回归测试。

反过来，以下情况不应长期 fallback：

- 热点算术/内存操作；
- 目标平台默认开启 GlobalISel 的 `-O0` 路径；
- fallback 会导致 ABI/调试信息不一致；
- 目标是把某个 target 的 GlobalISel 做到生产可用。

---

## 23.5 GlobalISel、SelectionDAG、FastISel 的分工

| 维度 | FastISel | SelectionDAG | GlobalISel |
| --- | --- | --- | --- |
| 输入 | IR | SelectionDAG | gMIR |
| 类型 | 较宽松 | EVT/MVT | LLT |
| 优化能力 | 很弱 | 成熟 | 逐步补齐 |
| 调试 | 简单 | DAG 可视化 | MIR + matcher table |
| 适用 | `-O0` 快速路径 | 通用/成熟后端 | 新后端、需渐进迁移 |
| 失败策略 | 回退 | 通常不适用 | 回退到 SelectionDAG |

GlobalISel 的长期价值不只是“另一个 ISel”，而是把 legalize、bank select、combine、select 放在统一的 gMIR 表示上，让不同 target 共享更多基础设施。代价是前期需要补大量 pattern 和 legalizer 规则。

一个常见误解是“GlobalISel 一定比 SelectionDAG 快”。它可能减少某些阶段的复杂度，也可能因为 bank/COPY 或 fallback 变慢。是否启用要看 target、优化级别、workload 和测量结果，不要用架构叙事代替 benchmark。

---

## 23.6 MIR Patterns 与 ISel 的边界

LLVM 还有一套 **MIR Patterns**（`llvm/docs/GlobalISel/MIRPatterns.rst`），主要服务 GlobalISel Combiner。它和本章说的 SelectionDAG pattern 导入不是同一套东西：

- **SelectionDAG pattern 导入**：`.td` 里的 `Pat<>` 被 `GlobalISelEmitter` 翻译成 `GIM_*`/`GIR_*` matcher table；
- **MIR Patterns**：用更接近 gMIR 的语法描述 combine 规则，由 Combiner 执行；
- 两者都能“匹配 + 重写”，但阶段、语义和调试工具不同。

不要把 Combiner 的规则直接当成 ISel pattern，也不要在 ISel 里做本该由 Combiner 完成的规范化。一个实用分工：

- **Combiner**：把 gMIR 规范化成更容易选择的形状（例如把 `G_SEXT(G_LOAD)` 变成 `G_SEXTLOAD`）；
- **InstructionSelect**：把规范化后的形状映射到具体指令；
- **Legalizer**：保证类型/操作合法；
- **RegBankSelect**：保证 bank 合理。

跨阶段的职责错位，往往表现为“pattern 越写越复杂”。这时应该问：是不是前面某个阶段应该先做一次 combine/legalize？

---

## 23.7 新 target 的迁移策略

如果要把 GlobalISel 移植到一个新 target，建议按“能跑通”而不是“全覆盖”排序：

### 阶段 1：最小闭环

- `IRTranslator`：能翻译函数、调用、返回、基本块；
- `Legalizer`：最小合法化规则；
- `RegBankSelect`：至少一个 bank；
- `InstructionSelect`：能选 load/store、算术、分支、return；
- `CallLowering`：能处理简单调用；
- 测试：一条 IR → asm 的 lit。

目标是让 `-global-isel -global-isel-abort=1` 在一个小函数上不 fallback。

### 阶段 2：常见 opcode

- 整数算术/比较/分支；
- 栈槽、frame index；
- 常量物化；
- 常见 addressing mode；
- 调用约定与返回值。

### 阶段 3：质量

- 边界立即数；
- atomic/volatile；
- 向量/intrinsic；
- 多输出；
- debug info；
- compile-time 与代码质量对比。

### 阶段 4：生产化

- fallback 策略与监控；
- 大规模测试；
- ABI/异常/调试；
- 与 SelectionDAG 的代码质量对比；
- 文档与上游 review。

每一阶段都要有“退出标准”。否则很容易出现“支持了很多 opcode，但没有一个端到端测试”的假进度。

---

## 23.8 质量与 compile-time 的权衡

pattern 不是免费的。每多一条规则，matcher 表就多一段字节码，候选集可能变大，编译时间可能上升。评估时至少看：

- 生成文件大小（`AArch64GenGlobalISel.inc`）；
- `llvm-tblgen` 时间；
- `llc -time-passes` 中 InstructionSelect 的占比；
- 典型 workload 的编译时间；
- 规则命中率（coverage）。

优化手段：

- 用更具体的 opcode/type 检查提前排除；
- 合并重复 pattern 为 `PatFrags`；
- 避免在谓词里做昂贵计算；
- 把冷门规则放到更窄的 feature/type 分支；
- 用 C++ selector 处理真正复杂的少数情况。

不要为了“表更小”而牺牲可读性；也不要为了“全部 TableGen”而写出无法维护的巨型谓词。工程判断比教条重要。
### 23.8.2 一个可操作的测量顺序

评估新 pattern 时，按成本从低到高做：

1. **先生成**：记录 `AArch64GenGlobalISel.inc` 的大小与 tblgen 时间；
2. **再跑单点**：用 `llc -time-passes` 看 InstructionSelect 的绝对时间；
3. **再跑集合**：选一组真实 IR，比较加 pattern 前后的 compile time；
4. **最后跑代码质量**：对比汇编指令数、spill、branch 数，而不是只看编译时间。

如果第 1 步就出现表体积异常增长，先别急着跑大 benchmark；通常是 pattern 太宽或谓词太复杂，应该先缩小匹配面。
### 23.8.1 什么时候停止优化 pattern

一个 pattern 的复杂度超过某个阈值时，继续在 `.td` 里加条件通常不划算：

- 谓词超过一屏，且需要多层 `if`；
- 同一条规则要处理三种以上完全不同的操作数形态；
- 需要访问 `MachineFunction` 的全局状态；
- 需要生成多条指令或临时虚拟寄存器；
- 测试需要大量 `CHECK-NOT` 才能锁定行为。

这时应该把“结构匹配”留给 pattern，把“策略”移到 C++，或者引入 pseudo 作为中间层。停止优化不是放弃质量，而是避免把 TableGen 变成一门不可调试的编程语言。

---

## 23.9 继续深入的学习路线

如果你已经读完本课，下一步可以按兴趣选一条：


### 23.9.0 一个通用的阅读顺序

不管选哪条路线，建议按“驱动 → 规则 → 数据”的顺序读源码：

1. **驱动**：先读 pass 的 `runOnMachineFunction` / `select()`，知道阶段边界；
2. **规则**：再读 `.td` pattern 或 combiner 规则，知道输入形状；
3. **数据**：最后读 MIR/TableGen 生成物，确认类型、bank、operand 的真实形态。

反过来从生成物开始读，容易被几千行 `.inc` 淹没；从规则开始读，又容易误以为规则就是执行语义。驱动 + 规则 + 数据三遍对照，才是可持续的读法。
### 路线 1：Combiner

读 `GlobalISelCombinerEmitter.cpp`、`CombinerHelper.cpp` 和 MIR Patterns 文档。目标：能写一条 combine 规则，并解释它为什么在 ISel 之前运行。

### 路线 2：Legalizer

读 `LegalizerInfo`、`LegalizerHelper` 和目标 legalizer。目标：能解释一个 `i128` 操作如何被拆成多条 `s64` 操作，以及为什么某些类型必须标量化。

### 路线 3：RegisterBankSelect

读 `RegBankSelect.cpp`、`RegisterBankInfo.cpp` 和目标 bank info。目标：能解释 cross-bank COPY 的代价模型，以及 greedy 与 fast 模式的差异。

### 路线 4：后端移植

选一个支持较少的 target，按 23.7 的阶段做最小闭环。目标：提交一个带 MIR 测试的 pattern，并能让 `-global-isel-abort=1` 通过。

### 路线 5：上游贡献

从 `globalisel` label 的 issue 或测试缺口开始，修一个小 bug，补一个边界测试。目标：熟悉 Phabricator/GitHub PR 的 review 标准与 lit 测试要求。

---

## 23.10 动手练习

### 练习 23.1：给一个 opcode 选路线

选三个你熟悉的 IR 操作（例如 `sdiv`、`frem`、`atomicrmw`），分别判断应该走纯 pattern、pattern+C++ 谓词、C++ select 还是 custom pseudo，并写出理由。要求每个判断都指出“失败时如何 fallback”。

### 练习 23.2：画责任图

找一条你遇到过的“选错指令”案例，画出它经过 IRTranslator → Legalizer → RegBankSelect → InstructionSelect 的路径，标出实际出错的那一层。不要从最终汇编倒推，要从 MIR 证据出发。

### 练习 23.3：评估 fallback 债务

在一个真实 target 上，用 `-global-isel-abort=1` 跑一组测试，统计 fallback 的 opcode 分布。设计一个优先级表：哪些先补、哪些可以永久交给 SelectionDAG、哪些需要 Combiner 先做规范化。

### 练习 23.4：读一条上游 review

找一条 GlobalISel 的 pattern/selector PR，读 review comment，总结 reviewer 最关心的三件事。把它们对照本课第 19 章的 checklist。

---

## 23.11 重点整理

- GlobalISel 的问题要先定位到阶段：IRTranslator、Legalizer、RegBankSelect、InstructionSelect、pseudo expansion，别在错误的层改代码。
- 实现路线从 pattern → C++ 谓词 → C++ select → custom pseudo，表达力上升，维护成本也上升。
- Pattern 适合结构 + 局部约束，不适合全局策略或 CFG 变换。
- fallback 可以是临时脚手架或永久策略，但不能是无人监控的隐藏债务。
- GlobalISel 不天然更快；要用 workload、compile-time 和代码质量数据说话。
- Combiner、Legalizer、RegBankSelect、ISel 各有职责，跨阶段错位会让 pattern 越来越复杂。
- 新 target 按最小闭环 → 常见 opcode → 质量 → 生产化推进，每阶段都要有退出标准。
- 读源码按“驱动 → 规则 → 数据”三遍对照，避免从生成物或规则单方面推断执行语义。
- 先定位阶段，再选实现路线；不要用 C++ 或 pseudo 掩盖前面阶段的缺口。

---

## 23.12 自我检核

> 这一节不考记忆，而是检查你能否把“先定位阶段、再选实现路线”的判断复述出来。如果某一题答不上来，回到对应章节的“重点整理”，不要从结论硬背。


1. 一条“pattern 存在但选不出来”的问题，你会按什么顺序排查？请说出至少四个阶段。
2. 什么情况下应该写 C++ `select()` 而不是继续加 pattern？给出三个判据。
3. 为什么 GlobalISel 的 fallback 既是优点也是风险？
4. 如果一条 pattern 需要访问多个基本块的概率信息，它应该放在哪个阶段？为什么？
5. 新 target 的“最小闭环”应该包含哪些组件？至少列五个。
6. 你如何量化一条新 pattern 的 compile-time 成本？

---

## 23.13 延伸阅读

- **[LLVM 文档：GlobalISel](https://llvm.org/docs/GlobalISel/)** — 整个 GlobalISel 文档集的入口。先读 InstructionSelect、Legalizer、RegBankSelect 三篇，再读 Porting；本章的阶段划分与这些文档的 pipeline 描述对应。
- **[LLVM 文档：MIR Patterns](https://llvm.org/docs/GlobalISel/MIRPatterns.html)** — Combiner 规则的语法与语义。读它来区分“ISel pattern”与“combine pattern”，不要混用两者。
- **[LLVM 源码：`GlobalISelEmitter.cpp`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/utils/TableGen/GlobalISelEmitter.cpp)** — pattern 导入与 matcher 表生成。重点看它如何过滤不支持的 SDNode、如何映射 MVT→LLT，以及如何处理 `PatLeaf`。
- **[LLVM 源码：`InstructionSelect.cpp`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp)** — pass 契约、遍历、失败设置与收尾。对照第 2、10 章读。
- **[LLVM Discourse：globalisel](https://discourse.llvm.org/tag/globalisel)** — 设计讨论与状态更新。读 recent threads 时，重点看“这是设计限制还是实现缺口”的区分，这正是本章反复强调的判断。


## 23.14 最后的踩雷与进阶

- **雷 1：把 GlobalISel 当成“更快的 SelectionDAG”**。它不是同构替代品；bank、fallback、pseudo 都会改变代码形状与编译时间。
- **雷 2：用 pattern 解决策略问题**。需要全局 use-def、CFG 或概率信息的逻辑，应放到更合适的阶段。
- **雷 3：用 fallback 掩盖缺口**。没有统计与测试的 fallback 是隐藏债务，不是设计。
- **雷 4：把 `selected: true` 当成成功**。失败时它仍可能为 true，必须同时看 `failedISel` 与 verifier。
- **进阶**：选一条你熟悉的 target opcode，画出它在 IRTranslator、Legalizer、RegBankSelect、InstructionSelect、pseudo expansion 五层的形态，并标出每层可能失败的条件。

---

## 下一章

[Final Project：为 AArch64 增加一条带 GlobalISel 支持的 custom instruction](./final-project-aarch64-custom-instruction.md) —— 把 pattern、谓词、C++ selector、MIR/lit 测试、fallback 与 review checklist 串成一个完整交付。
