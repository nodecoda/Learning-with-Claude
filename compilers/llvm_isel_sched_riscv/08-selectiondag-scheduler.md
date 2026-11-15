# Ch 8 — SelectionDAG 的调度器：`ScheduleDAGSDNodes`

> **目标**：看懂 SelectionDAG 内部的第一层调度。学完这章，你能从 `-debug-only=pre-RA-sched` 的 SUnit 图里读出 `Depth`/`Height`、`preds left`/`succs left`，能解释 bottom-up list scheduling 为什么从 `PseudoRET` 开始，能说清 `-enable-misched=false` 和 `-pre-RA-sched` 各控制什么，以及 DAG scheduler 和 MachineScheduler 的分工。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章用 `min.ll`，统一 `-mtriple=riscv64 -mcpu=generic -mattr=+m -O2`。DAG 调度日志用 `-debug-only=pre-RA-sched`，MIR 顺序用 `-print-before/after=machine-scheduler`。

## 为什么需要这个？

LLVM 的 pre-RA 调度有两层，名字很容易混：

```text
SelectionDAG 内部：ScheduleDAGSDNodes   （本章）
Machine IR 层：   MachineScheduler      （Part 2）
```

两层都叫「调度」，但输入、输出、目标不同：

| 维度 | `ScheduleDAGSDNodes` | `MachineScheduler` |
|---|---|---|
| 输入 | `SDNode` / `SUnit` 图 | `MachineInstr` / `ScheduleDAGMILive` |
| 时机 | instruction selection 之后、EmitSchedule 之前 | isel 之后、寄存器分配之前 |
| 顺序 | 给 DAG 节点排序，然后 emit 成 MI | 给已有 MI 重排 |
| 默认行为 | 在 MachineScheduler 开启时通常是 source order | 按 sched model 做真正的调度 |
| 关闭方式 | 不能用 `-enable-misched=false` 单独关 | `-enable-misched=false` |

最常见的误解是：

> 「`-enable-misched=false` 就没有调度了。」

不是。它只是关掉 MachineScheduler，SelectionDAG 内部的 `ScheduleDAGSDNodes` 仍然会跑。你看到的顺序变化，可能来自 DAG scheduler，而不是「没有调度」。

这一章要建立三个判断：

1. **DAG scheduler 的输入是 SUnit 图，不是 MIR**：chain/glue/data 依赖变成 `SDep` 边。
2. **它默认是 bottom-up**：从根节点 `PseudoRET` 开始，往操作数方向排，最后把序列反转。
3. **它和 MachineScheduler 是接力关系**：DAG scheduler 给初版顺序，MachineScheduler 再按模型重排。

## 先建立直觉

一条流水线：

```text
SelectionDAG
   │
   ├─ BuildSchedGraph
   │    ├─ ClusterNodes（合并相邻 load）
   │    ├─ AddSchedEdges（data / order / glue / barrier）
   │    └─ 计算 Latency / Depth / Height
   │
   ├─ ListScheduleBottomUp
   │    ├─ 从 ExitSU / root 开始
   │    ├─ Available 队列按优先级取节点
   │    └─ 排完后 reverse(Sequence)
   │
   └─ EmitSchedule
        ├─ 按 Sequence 顺序
        ├─ 先发射 glued 节点
        └─ InstrEmitter 生成 MachineInstr
```

SUnit 图里每个节点长这样：

```text
SU(9): t13: i64,ch = LD<...>
  # preds left : 1
  # succs left : 1
  # rdefs left : 1
  Latency      : 1
  Depth        : 1
  Height       : 4
  Predecessors:
    SU(5): Data Latency=1
  Successors:
    SU(7): Data Latency=1
```

读法：

- `SU(9)`：SUnit 编号，不是 DAG 的 `tN`。一个 SUnit 可以包含多个 glue 在一起的 `SDNode`。
- `preds left` / `succs left`：还有多少前驱/后继没被调度。两者都归零时，节点进入 Available 队列。
- `rdefs left`：还有多少个寄存器定义没处理，影响反依赖。
- `Latency`：这条边的延迟，来自 sched model 或默认值。
- `Depth`：从根到它的最长路径（以 latency 计）。
- `Height`：从它到叶子的最长路径。bottom-up 调度优先选 Height 大的节点。

## 什么时候跑，跑哪个 scheduler

`SelectionDAGISel::CodeGenAndEmitDAG()` 在 instruction selection 之后调用：

```cpp
ScheduleDAGSDNodes *Scheduler = CreateScheduler();
Scheduler->Run(CurDAG, FuncInfo->MBB);
```

`CreateScheduler()` 走 `-pre-RA-sched` 选项，默认是 `createDefaultScheduler`。`createDefaultScheduler` 的逻辑（`SelectionDAGISel.cpp` 约 302 行）：

```cpp
if (auto *SchedulerCtor = ST.getDAGScheduler(OptLevel))
  return SchedulerCtor(IS, OptLevel);

if (OptLevel == CodeGenOptLevel::None ||
    (ST.enableMachineScheduler() && ST.enableMachineSchedDefaultSched()) ||
    TLI->getSchedulingPreference() == Sched::Source)
  return createSourceListDAGScheduler(IS, OptLevel);
if (TLI->getSchedulingPreference() == Sched::RegPressure)
  return createBURRListDAGScheduler(IS, OptLevel);
if (TLI->getSchedulingPreference() == Sched::Hybrid)
  return createHybridListDAGScheduler(IS, OptLevel);
...
```

对 RISC-V 来说：

- 没有 target-specific 的 `getDAGScheduler`。
- 默认 `getSchedulingPreference()` 是 `Sched::Source`（`TargetLowering` 的默认值）。
- 所以默认走 `createSourceListDAGScheduler`，也就是「尽量保持 source order」的 bottom-up list scheduler。
- 当 MachineScheduler 开启且使用默认策略时，也会强制走 source DAG scheduler，避免两层调度互相打架。

`-pre-RA-sched` 可选值（`llc -help-hidden` 实测）：

```text
=source       Similar to list-burr but schedules in source order when possible
=list-burr    Bottom-up register reduction list scheduling
=list-hybrid  Bottom-up register pressure aware list scheduling
=list-ilp     Bottom-up register pressure aware list scheduling, balances ILP and pressure
=fast         Fast suboptimal list scheduling
=linearize    Linearize DAG, no scheduling
=default      Best scheduler for the target
```

注意 `source` 的描述：**similar to list-burr but schedules in source order when possible**。它不是「完全不调度」，只是在有选择时优先保持源码顺序。

## 建图：`BuildSchedGraph`

`ScheduleDAGSDNodes::BuildSchedGraph()`（约 543 行）做三件事：

1. `ClusterNodes()`（约 310 行）：把某些节点合并成一个 SUnit。
2. `AddSchedEdges()`（约 441 行）：把 DAG 边翻译成 `SDep`。
3. 计算每个 SUnit 的 `Latency`、`Depth`、`Height`，以及 `NumRegDefsLeft`。

### `ClusterNodes`：相邻 load 合并

`ClusterNeighboringLoads()`（约 199 行）会把地址相近的 load 用 glue 粘成一个 SUnit。这样做的目的是让它们在最终序列里相邻，减少地址计算和内存端口的冲突。

DAG scheduler 的 cluster 不一定在 `-debug-only=pre-RA-sched` 里逐条打印；可以用 `-stats` 看 `LoadsClustered` 计数。合并后，SUnit 数量少于 DAG 节点数，所以 `SU(N)` 的编号不能和 `tN` 一一对应。

### `AddSchedEdges`：四种边

`AddSchedEdges()` 把 DAG 的边翻译成 `SDep`：

| DAG 边 | SDep 类型 | 含义 |
|---|---|---|
| 数据依赖 | `SDep::Data` | 值被使用；带 latency |
| chain | `SDep::Order` | 内存/控制顺序；可能带 `Barrier` |
| glue | `SDep::Glue` | 原子组，不能拆 |
| 人工/反依赖 | `SDep::Artificial` / `Anti` | 寄存器压力、二地址指令等 |

在 `min.ll` 的日志里：

```text
SU(1): t21: i64 = ADD t20, t18
  Predecessors:
    SU(6): Data Latency=1
    SU(2): Data Latency=1
```

`ADD` 有两个数据前驱：`t20` 和 `t18`。`Latency=1` 来自默认 sched model。

内存操作的边会显示 `Ord` 和 `Barrier`：

```text
SU(3): t10: i64,ch = LD<...>
  Predecessors:
    SU(6): Data Latency=1
    SU(4): Ord  Latency=1 Barrier
```

`Barrier` 表示这条顺序边不能为了调度而忽略。第 9 章会专门讲内存边界。

### Depth 和 Height

`Depth` 和 `Height` 的定义：

- `Depth(SU)` = 从任意根到 `SU` 的最长路径（按 latency 累加）。
- `Height(SU)` = 从 `SU` 到根（`PseudoRET`）的最长路径。

在 `min.ll` 里：

```text
SU(0): PseudoRET       Depth 5  Height 0
SU(1): ADD             Depth 4  Height 1
SU(6): ADD             Depth 3  Height 2
SU(9): LD              Depth 1  Height 4
SU(5): CopyFromReg     Depth 0  Height 5
```

bottom-up 调度从 `PseudoRET` 开始，优先选 `Height` 大的节点，因为它在关键路径上更「深」。这就是为什么日志里的 `Examining Available:` 会按 `Height` 分组：

```text
Height 0: SU(0): PseudoRET
Height 1: SU(1): ADD
Height 2: SU(6): ADD
Height 3: SU(10): ADD
...
```

## 调度：`ListScheduleBottomUp`

`ScheduleDAGRRList::ListScheduleBottomUp()`（约 1546 行）的主循环：

```cpp
ReleasePredecessors(&ExitSU);

if (!SUnits.empty()) {
  SUnit *RootSU = &SUnits[DAG->getRoot().getNode()->getNodeId()];
  RootSU->isAvailable = true;
  AvailableQueue->push(RootSU);
}

while (!AvailableQueue->empty() || !Interferences.empty()) {
  SUnit *SU = PickNodeToScheduleBottomUp();
  AdvancePastStalls(SU);
  ScheduleNodeBottomUp(SU);
  ...
}

std::reverse(Sequence.begin(), Sequence.end());
```

关键点：

1. **从根开始**：`PseudoRET` 是 DAG 的根，先进入 Available。
2. **反向传播**：调度一个节点后，释放它的前驱；前驱的 `succs left` 减到 0 时进入 Available。
3. **优先级**：`PickNodeToScheduleBottomUp()` 按 Height、寄存器压力、source order 等综合排序。不同 scheduler 的 priority queue 不同。
4. **最后反转**：bottom-up 得到的 `Sequence` 是「从根到叶子」，反转后才是最终发射顺序。

`min.ll` 的调度日志（节选）：

```text
*** Scheduling [0]: SU(0): PseudoRET

*** Scheduling [1]: SU(1): ADD t20, t18

*** Scheduling [2]: SU(6): ADD t19, t14

*** Scheduling [3]: SU(10): ADD t10, t6

*** Scheduling [4]: SU(2): MUL t17, t8

*** Scheduling [5]: SU(4): LD p1

*** Scheduling [6]: SU(7): MUL t13, t4

*** Scheduling [7]: SU(9): LD p

*** Scheduling [8]: SU(5): CopyFromReg %0
...
*** Final schedule ***
SU(11): CopyFromReg %2
SU(12): CopyFromReg %4
SU(3): CopyFromReg %3
SU(8): CopyFromReg %1
SU(5): CopyFromReg %0
SU(9): LD p
SU(7): MUL
SU(4): LD p1
SU(2): MUL
SU(10): ADD
SU(6): ADD
SU(1): ADD
SU(0): PseudoRET
```

`Final schedule` 是反转后的 top-down 顺序。忽略 `CopyFromReg`（它们在寄存器分配后多数被消掉），顺序是：

```text
LD p -> MUL -> LD p1 -> MUL -> ADD(add d,b) -> ADD -> ADD -> RET
```

这正是 `-enable-misched=false` 的汇编顺序：

```asm
ld   a5, 0(a0)
mul  a1, a5, a1
ld   a0, 8(a0)
mul  a0, a0, a3
add  a2, a4, a2
add  a1, a2, a1
add  a0, a1, a0
ret
```

注意：`Final schedule` 里有很多 `CopyFromReg`，但最终汇编里没有对应的 `mv`。这是因为 `EmitSchedule` 会为它们生成 `COPY`，后续寄存器分配/合并又把 `COPY` 消掉。**不要拿 `Final schedule` 的行数和最终汇编行数一一对应。**

## `-enable-misched=false` 到底关了什么

`-enable-misched=false` 关的是 **MachineScheduler pass**，不是 DAG scheduler。对比三种情况：

```bash
# 1. 默认：DAG source scheduler + MachineScheduler
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 min.ll -o -

# 2. 只关 MachineScheduler：DAG source scheduler
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -enable-misched=false min.ll -o -

# 3. 关 MachineScheduler，换 DAG list-burr
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -enable-misched=false -pre-RA-sched=list-burr min.ll -o -
```

本机实测：

```asm
# 1. 默认
ld   a5, 0(a0)
ld   a0, 8(a0)
mul  a1, a5, a1
mul  a0, a0, a3
add  a2, a4, a2
add  a1, a2, a1
add  a0, a1, a0
ret

# 2. -enable-misched=false（source）
ld   a5, 0(a0)
mul  a1, a5, a1
ld   a0, 8(a0)
mul  a0, a0, a3
add  a2, a4, a2
add  a1, a2, a1
add  a0, a1, a0
ret

# 3. -enable-misched=false -pre-RA-sched=list-burr
add  a2, a4, a2
ld   a4, 0(a0)
mul  a1, a4, a1
add  a1, a2, a1
ld   a0, 8(a0)
mul  a0, a0, a3
add  a0, a1, a0
ret
```

三个顺序，三种原因：

- 默认：DAG source 保持 `ld, mul, ld, mul, add...`，然后 MachineScheduler 把两个 `ld` 提前、两个 `mul` 推后，形成 `ld, ld, mul, mul`。
- `-enable-misched=false`：MachineScheduler 不跑，留下 source DAG 顺序 `ld, mul, ld, mul`。
- `list-burr`：DAG scheduler 自己把 `add a2` 提前，因为它按 Height/寄存器压力选节点，不按 source order。

这个对比是面试里解释「这条指令是谁排的」的标准实验。看到顺序变化，先确认是哪一层在跑。

## 和 MachineScheduler 的接力

用 `-print-before/after=machine-scheduler` 看默认流程：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -print-before=machine-scheduler -print-after=machine-scheduler \
  min.ll -o /dev/null
```

`Before`：

```text
%5:gpr = LD %0:gpr, 0
%6:gpr = MUL %5:gpr, %1:gpr
%7:gpr = LD %0:gpr, 8
%8:gpr = MUL %7:gpr, %3:gpr
%9:gpr = ADD %4:gpr, %2:gpr
%10:gpr = ADD %9:gpr, %6:gpr
%11:gpr = ADD %10:gpr, %8:gpr
```

`After`：

```text
%5:gpr = LD %0:gpr, 0
%7:gpr = LD %0:gpr, 8
%6:gpr = MUL %5:gpr, %1:gpr
%8:gpr = MUL %7:gpr, %3:gpr
%9:gpr = ADD %4:gpr, %2:gpr
%10:gpr = ADD %9:gpr, %6:gpr
%11:gpr = ADD %10:gpr, %8:gpr
```

`Before` 是 DAG source scheduler 的结果，`After` 是 MachineScheduler 按 sched model 重排的结果。日志里还能看到：

```text
GenericScheduler RegionPolicy:  ShouldTrackPressure=1 OnlyTopDown=0 OnlyBottomUp=0
Cluster ld/st SU(5) - SU(7)
```

这是 Part 2 的内容，这里先记住分界线：**DAG scheduler 给初版顺序，MachineScheduler 再排。**

## `EmitSchedule`：从 SUnit 到 MachineInstr

`ScheduleDAGSDNodes::EmitSchedule()`（约 874 行）遍历 `Sequence`：

```cpp
for (SUnit *SU : Sequence) {
  if (!SU) { TII->insertNoop(*Emitter.getBlock(), InsertPos); continue; }

  SmallVector<SDNode *, 4> GluedNodes;
  for (SDNode *N = SU->getNode()->getGluedNode(); N; N = N->getGluedNode())
    GluedNodes.push_back(N);
  while (!GluedNodes.empty()) {
    SDNode *N = GluedNodes.back();
    EmitNode(N, ...);
    GluedNodes.pop_back();
  }
  EmitNode(SU->getNode(), ...);
}
```

三个细节：

1. **glue 先发射**：一个 SUnit 里的 glued 节点按逆序先发射，最后发射主节点。这保证了 `CopyToReg` 在 `CALL` 之前、`CopyFromReg` 在用户之前。
2. **一个 SDNode 可以生成多条 MI**：`EmitNode` 调用 `InstrEmitter::EmitNode`，后者可能生成 0 条、1 条或多条 MachineInstr。第 7 章的 `ADDI + SH3ADD` 就是这里落地的。
3. **`Sequence` 是 top-down**：`ListScheduleBottomUp` 最后反转，`EmitSchedule` 直接按顺序发射。

`EmitSchedule` 还会处理 `CopyFromReg`/`CopyToReg` 的物理寄存器复制，插入 `COPY`。这些 `COPY` 在后续 RA/coalescing 中可能消失，所以 `Final schedule` 和最终汇编不是一一对应。

## 源码落点

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - `ClusterNeighboringLoads()`（约 199 行）、`ClusterNodes()`（约 310 行）。
  - `AddSchedEdges()`（约 441 行）、`BuildSchedGraph()`（约 543 行）。
  - `EmitSchedule()`（约 874 行）。
  - **读哪里**：先看 `BuildSchedGraph` 的三步，再看 `EmitSchedule` 的 glue 处理。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGRRList.cpp`**
  - `ListScheduleBottomUp()`（约 1546 行）。
  - `dumpSchedule()` / `Final schedule`（约 380 行）。
  - **读哪里**：看 Available 队列、`PickNodeToScheduleBottomUp()`、最后的 `std::reverse`。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - `createDefaultScheduler()`（约 302 行）。
  - `CodeGenAndEmitDAG()` 里的 `CreateScheduler()` / `Scheduler->Run()`（约 1159 行）。
  - **读哪里**：确认当前配置走哪个 scheduler。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.h`**
  - `SUnit`、`SDep`、`ScheduleDAGSDNodes` 的接口。
  - **读哪里**：`SDep::Kind` 的 `Data`、`Order`、`Glue`、`Barrier`、`Artificial`。

## 踩雷集锦

1. **以为 `-enable-misched=false` 就没有调度**：DAG scheduler 仍然在跑。要单独观察它，用 `-debug-only=pre-RA-sched`。

2. **以为 `-pre-RA-sched=source` 完全保持源码顺序**：它只是「when possible」；遇到依赖、寄存器压力、hazard 时仍会重排。

3. **把 `Final schedule` 和最终汇编一一对应**：`CopyFromReg`/`CopyToReg` 会生成 `COPY`，后续可能被消掉；一个 SDNode 也可能生成多条 MI。

4. **把 `Depth`/`Height` 当成指令条数**：它们是按 latency 累加的最长路径。默认 `Latency=1` 时看起来像条数，换了 sched model 就不是。

5. **以为 `Latency=1` 是 RISC-V 的真实延迟**：`-mcpu=generic` 的 sched model 很粗糙，很多操作都是默认 1。要看真实延迟，用具体 CPU 的 sched model 或 `llvm-mca`。

6. **把 `SU(N)` 当成 `tN`**：SUnit 会合并 glued 节点和 clustered load，编号和 DAG 节点不是一一对应。

7. **忽略 `ClusterNodes`**：相邻 load 可能被合并成一个 SUnit，日志里的 SUnit 数少于 DAG 节点数。

8. **在 `-O0` 下用同一套结论**：`-O0` 也走 DAG scheduler，但 `OptLevel == None` 会强制 source scheduler，且 IR 形状不同。

## 动手练习

1. 用 `min.ll` 跑 `-enable-misched=false -debug-only=pre-RA-sched`，找出 `SU(1)` 的 `Depth`/`Height`、前驱、后继。解释为什么 bottom-up 先选 `PseudoRET`。

2. 把 `min.ll` 的两个 load 改成三个 load，重新看 SUnit 图和 `-stats` 里的 `LoadsClustered`。`ClusterNodes` 合并了哪些 load？

3. 用 `-enable-misched=false` 分别跑 `-pre-RA-sched=source`、`list-burr`、`list-ilp`，记录三种汇编顺序。哪一种最接近源码顺序？哪一种把 `add` 提前了？

4. 用 `-print-before/after=machine-scheduler` 对比默认流程，标出哪几条指令是 MachineScheduler 移动的。再用 `-enable-misched=false` 确认移动前的顺序。

5. 把 `min.ll` 的 `mul` 改成 `add`，让依赖链变短，重新看 `Depth`/`Height`。scheduler 的选择会变吗？

6. **guided**：在 `ScheduleDAGSDNodes::BuildSchedGraph()` 里加一条 `dbgs()`，打印每个 SUnit 的 `Depth`/`Height`。重新构建后跑 `min.ll`，和 `-debug-only=pre-RA-sched` 的输出对照。不要提交这条调试代码。

## 本章重点整理

- `ScheduleDAGSDNodes` 是 SelectionDAG 内部的 pre-RA 调度器；它接收 `SDNode`，输出 `Sequence`，再由 `EmitSchedule` 变成 MI。
- `BuildSchedGraph()` 做三件事：`ClusterNodes`、`AddSchedEdges`、计算 `Latency`/`Depth`/`Height`。
- DAG 的 data/chain/glue 边分别变成 `SDep::Data`、`SDep::Order`、`SDep::Glue`；`Barrier` 表示不能忽略的顺序。
- bottom-up list scheduling 从根 `PseudoRET` 开始，优先选 Height 大的节点，最后反转 `Sequence`。
- `-enable-misched=false` 只关 MachineScheduler；DAG scheduler 仍在跑。`-pre-RA-sched` 选择 DAG scheduler 的实现。
- 默认 RISC-V 走 source DAG scheduler；MachineScheduler 开启时，它通常只负责给初版顺序。
- `Final schedule` 包含会生成 0 条或多条 MI 的节点，不能和最终汇编一一对应。

## 自我检核

- [ ] 能说出 `ScheduleDAGSDNodes` 和 `MachineScheduler` 的输入、输出、时机差异
- [ ] 能解释 `BuildSchedGraph()` 的三步，以及 `ClusterNodes` 的作用
- [ ] 能读懂 SUnit 日志的 `preds left`、`succs left`、`Depth`、`Height`
- [ ] 能解释 bottom-up 调度为什么从 `PseudoRET` 开始，以及为什么最后要反转
- [ ] 能说出 `-enable-misched=false` 和 `-pre-RA-sched` 各控制什么
- [ ] 能解释 `Final schedule` 和最终汇编为什么不是一一对应
- [ ] 能用 `-print-before/after=machine-scheduler` 区分两层调度

## 延伸阅读

### 官方文档

- **[LLVM Code Generator — Instruction Scheduling](https://llvm.org/docs/CodeGenerator.html#instruction-scheduling)**
  - **读哪里**：SelectionDAG scheduler、MachineScheduler 的分层。
  - **和本章的关联**：两层调度的官方定位。

- **[LLVM Machine Code Layer](https://llvm.org/docs/CodeGenerator.html#machine-code)**
  - **读哪里**：MachineInstr、MIR、post-RA pass。
  - **和本章的关联**：`EmitSchedule` 之后的流程。

- **[llvm-mca 文档](https://llvm.org/docs/CommandGuide/llvm-mca.html)**
  - **读哪里**：sched model、latency、resource pressure。
  - **和本章的关联**：`Latency=1` 的来源和局限。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - **读哪里**：`ClusterNodes()`（约 310 行）、`AddSchedEdges()`（约 441 行）、`BuildSchedGraph()`（约 543 行）、`EmitSchedule()`（约 874 行）。
  - **和本章的关联**：SUnit 图的构建和发射。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGRRList.cpp`**
  - **读哪里**：`ListScheduleBottomUp()`（约 1546 行）、`PickNodeToScheduleBottomUp()`。
  - **和本章的关联**：bottom-up 调度主循环。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - **读哪里**：`createDefaultScheduler()`（约 302 行）、`CodeGenAndEmitDAG()`（约 942 行）。
  - **和本章的关联**：scheduler 的选择和调用时机。

- **`llvm/include/llvm/CodeGen/ScheduleDAG.h`**
  - **读哪里**：`SDep`、`SUnit`、`ScheduleDAG` 的接口。
  - **和本章的关联**：`Data`/`Order`/`Glue`/`Barrier` 的定义。

### 文章 / 演讲

- **"Instruction Scheduling in LLVM"**（LLVM Developers' Meeting 演讲）
  - **这讲什么**：DAG scheduler 和 MachineScheduler 的设计、策略、模型。
  - **为什么值得看**：把本章的 SUnit 图和 Part 2 的 MachineScheduler 串起来。

- **"LLVM's SelectionDAG"**（LLVM 后端演讲/博客）
  - **这讲什么**：SelectionDAG 的构建、合法化、调度、发射。
  - **和本章的关联**：本章在整条 DAG 流水线中的位置。

下一章看不那么「纯数据」的调度边界：内存操作、`fence`、atomics、`volatile` 如何变成 `Order`/`Barrier` 边，以及为什么 scheduler 不能把一条 load 随随便便提到 store 前面。

→ [Ch 9 调度边界：内存、副作用、fence、atomics](./09-scheduling-boundaries.md)
