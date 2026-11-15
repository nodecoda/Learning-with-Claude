# Ch 11 MachineScheduler 全景：region、pre-RA/post-RA、策略

> **目标**：把 MachineScheduler 放回 pass pipeline 的正确位置，弄清它「按什么单位调度」「pre-RA 和 post-RA 各自解决什么问题」「RISC-V 在哪里改写了默认策略」。学完你能对着任意一段 MIR 说出：这里有几个 scheduling region、边界是谁、这次重排是哪个策略做的。
>
> **环境**：LLVM 24.0.0git（`/home/ubuntu/llvm/builds/explore/bin/llc`），triple `riscv64-unknown-elf`，`-mattr=+m` 固定，CPU 在 `sifive-u74` / `rocket-rv64` / `generic` 之间切换。所有输出为本机实测。

## 为什么先看全景

第 8 章讲的是 SelectionDAG 内部的 `ScheduleDAGSDNodes`：它在指令选择的同时顺带把 DAG 排成线性顺序。那一步结束后，你得到的 MIR 已经「有顺序」了，但那个顺序只是「依赖允许的某个拓扑序」，没有认真考虑目标机器的发射宽度、功能单元、load 延迟和寄存器压力。

MachineScheduler 就是来干这件事的。它是 MIR 层的一个独立 pass，在寄存器分配前后各跑一次（post-RA 那一次可选）。面试里问「这个顺序是谁排的」，答案经常就在这两次之间。但它有三个容易混淆的点：

1. **调度单位是 region，不是函数**：遇到 call、terminator、改栈指针的指令就断开。跨 call 不会重排。
2. **pre-RA 和 post-RA 用的是同一个框架、不同策略**：pre-RA 有虚拟寄存器、能跟踪压力；post-RA 只有物理寄存器，主要目标是填延迟槽和打散反依赖。
3. **target 可以只换策略，不换 pass**：RISC-V 就是 `RISCVPreRAMachineSchedStrategy : GenericScheduler`，加了一条 `vsetvli` 相关的 tie-break。

先把这三件事钉死，后面读 sched model 才有落点。

## 先建立直觉：pipeline 里的两个位置

用 `-debug-pass=Structure` 看 `min.ll` 在 `-O2` 下的真实顺序（只摘相关行）：

```text
      Two-Address instruction pass
      Register Coalescer
      Machine Instruction Scheduler          <-- pre-RA
      ...
      Greedy Register Allocator
      Virtual Register Rewriter
      ...
      PostRA Machine Sink
      ...
      Prologue/Epilogue Insertion & Frame Finalization
      ...
      Post-RA pseudo instruction expansion pass
      RISC-V Pseudo Instruction Expansion - Post-RA
      RISC-V Load / Store Optimizer
      PostRA Machine Instruction Scheduler   <-- post-RA
```

两个位置对应两种自由度：

```text
        ISel / DAG scheduler
                │
                ▼
   ┌────────────────────────────┐
   │ pre-RA MachineScheduler    │  虚拟寄存器还在
   │  - 可改顺序               │  可以跟踪 regpressure
   │  - 可插入/拆分? 否        │  能为了压力放弃一点 ILP
   └────────────────────────────┘
                │
                ▼
         Register Allocation
                │
                ▼
   ┌────────────────────────────┐
   │ post-RA MachineScheduler   │  只有物理寄存器
   │  - 只能在依赖内重排       │  目标：填 delay slot、打散反依赖
   │  - 不能新增虚拟寄存器     │  受 hazard / 资源约束
   └────────────────────────────┘
```

关键差别不是「谁更聪明」，而是**可用信息不同**：pre-RA 能看见虚拟寄存器的 live range，所以能判断「把这条 load 提前会不会把压力顶爆」；post-RA 看不见 live range，只能看物理寄存器号，重排空间被反依赖和 fixed register 限制得更死。

## 主线：一个函数被切成几个 region

MachineScheduler 不按函数建图，而是把每个基本块再切成若干 region。规则在 `MachineScheduler.cpp` 的 `isSchedBoundary()`：

```cpp
static bool isSchedBoundary(MachineBasicBlock::iterator MI,
                            MachineBasicBlock *MBB, MachineFunction *MF,
                            const TargetInstrInfo *TII) {
  return MI->isCall() || TII->isSchedulingBoundary(*MI, MBB, *MF) ||
         MI->isFakeUse() || MI->isPHI();
}
```

`TargetInstrInfo::isSchedulingBoundary()` 的默认实现把 terminator、label/position、`INLINEASM_BR`，以及**修改栈指针的指令**都算边界：

```cpp
  if (MI.isTerminator() || MI.isPosition())
    return true;
  ...
  return MI.modifiesRegister(TLI.getStackPointerRegisterToSaveRestore(), &TRI);
```

这解释了两件常见现象：

- 为什么 `ADJCALLSTACKDOWN/UP` 会把 region 切断：它改 `sp`。
- 为什么 call 两侧的指令永远不会被换序：call 本身就是边界。

### 实测：三个基本块，三个 region

`regions.ll`：

```llvm
target triple = "riscv64-unknown-elf"
declare void @g()
declare i64 @h(i64)
define i64 @multi(i64 %a, i64 %b, ptr %p) {
entry:
  %x = add i64 %a, %b
  %c = icmp eq i64 %x, 0
  br i1 %c, label %then, label %else
then:
  call void @g()
  %y = call i64 @h(i64 %x)
  store i64 %y, ptr %p
  ret i64 %y
else:
  %z = sub i64 %a, %b
  ret i64 %z
}
```

```bash
"$LLC" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m -O2 \
  -misched-print-dags regions.ll -o /dev/null
```

输出（只留 region 头）：

```text
Current Schedule Region
multi:%bb.0 entry
  From: %3:gpr = COPY $x12
 RegionInstrs: 4
Current Schedule Region
multi:%bb.1 then
  From: %5:gpr = COPY $x10
 RegionInstrs: 3
Current Schedule Region
multi:%bb.2 else
  From: %4:gpr = SUB %1:gpr, %2:gpr
 RegionInstrs: 2
```

注意 `bb.1` 只有 3 条：两个 call 把这一块切成了好几段，最后一段才是可调度的尾巴。`PseudoRET` 是 terminator，是边界但不算进 DAG。想确认边界到底是谁，用 `-print-after=machine-scheduler` 看完整 MIR：

```text
112B  bb.1.then:
128B    ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
144B    PseudoCALL target-flags(riscv-call) @g, <regmask ...>
160B    ADJCALLSTACKUP 0, 0, implicit-def dead $x2, implicit $x2
176B    ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
192B    $x10 = COPY %0:gpr
208B    PseudoCALL target-flags(riscv-call) @h, ...
224B    ADJCALLSTACKUP 0, 0, implicit-def dead $x2, implicit $x2
240B    %5:gpr = COPY $x10
272B    $x10 = COPY %5:gpr
280B    SD %5:gpr, %3:gpr, 0 :: (store (s64) into %ir.p)
288B    PseudoRET implicit $x10
```

`From: %5:gpr = COPY $x10` 对应的就是 `240B` 这条，往后三条（`COPY $x10`、`SD`、`PseudoRET`）构成 region，`RegionInstrs: 3` 里 `PseudoRET` 作为边界被算进 region 但不进 DAG。

### region 内部的四步

`MachineSchedulerBase::scheduleRegions()` 对每个 region 做的事很固定：

```cpp
Scheduler.enterRegion(&*MBB, I, RegionEnd, NumRegionInstrs);
...
Scheduler.schedule();      // 建图 + 列表调度 + 改写 MIR
Scheduler.exitRegion();    // 收尾：anti-dep breaking、cluster
```

四个阶段对应的关键调用：

| 阶段 | 入口 | 做的事 |
|---|---|---|
| 进入 | `ScheduleDAGMILive::enterRegion` | 初始化压力跟踪、live interval 引用 |
| 建图 | `buildSchedGraph(AA, &RPTracker, &SUPressureDiffs, LIS, ...)` | 生成 `SUnit`、依赖边、压力差 |
| 调度 | `SchedImpl->pickNode()` / `schedule()` | 按策略选点、改 MIR 顺序 |
| 退出 | `exitRegion()` | 反依赖打断、cluster、清理 |

## 底层机制：三个类，各管一段

```text
ScheduleDAGInstrs
   └── ScheduleDAGMI            // 通用 MI 调度：建图、策略接口、anti-dep
         └── ScheduleDAGMILive  // pre-RA：+ LiveIntervals + RegPressure
PostMachineScheduler            // post-RA：用 ScheduleDAGMI + PostGenericScheduler
```

`MachineScheduler.h` 开头的注释把扩展点写得很清楚：

> The default scheduler, ScheduleDAGMILive, builds the DAG and drives list scheduling while updating the instruction stream, register pressure, and live intervals. Most targets don't need to override the DAG builder and list scheduler, but subtargets that require custom scheduling heuristics may plugin an alternate MachineSchedStrategy.

也就是说，target 通常只提供两样东西：

1. **一个 `MachineSchedStrategy`**：决定「下一个发谁」。
2. **可选的 DAG mutation**：建图后、调度前改依赖边（例如 cluster load/store、macro fusion）。

`ScheduleDAGMILive` 比 `ScheduleDAGMI` 多的成员，全是为了压力：

```cpp
  RegisterClassInfo *RegClassInfo;
  VReg2SUnitMultiMap VRegUses;      // 每个 vreg 在本 region 的所有 use
  PressureDiffs SUPressureDiffs;    // 每个 SU 的压力增减
  bool ShouldTrackPressure = false;
  IntervalPressure RegPressure;
  RegPressureTracker RPTracker;
  IntervalPressure TopPressure, BotPressure;
  RegPressureTracker TopRPTracker, BotRPTracker;
```

`TopRPTracker` / `BotRPTracker` 分别跟踪「已调度区顶部」和「未调度区底部」的压力增量，这是双向调度的基础：从两头往中间填，压力可以从任意一侧估算。

### 策略：默认双向，RISC-V 把它写死

`GenericScheduler::initPolicy()` 的默认是 bottom-up：

```cpp
  // For generic targets, we default to bottom-up, because it's simpler and more
  // compile-time optimizations have been implemented in that direction.
  RegionPolicy.OnlyBottomUp = true;

  // Allow the subtarget to override default policy.
  SchedRegion Region(Begin, End, NumRegionInstrs);
  MF.getSubtarget().overrideSchedPolicy(RegionPolicy, Region);
```

RISC-V 的 `overrideSchedPolicy()` 直接改成双向，并且强制打开压力跟踪：

```cpp
void RISCVSubtarget::overrideSchedPolicy(MachineSchedPolicy &Policy,
                                         const SchedRegion &Region) const {
  // Do bidirectional scheduling since it provides a more balanced scheduling
  // leading to better performance. This will increase compile time.
  Policy.OnlyTopDown = false;
  Policy.OnlyBottomUp = false;
  ...
  Policy.ShouldTrackPressure = true;
}
```

实测确认（`min.ll`，`-debug-only=machine-scheduler`）：

```text
GenericScheduler RegionPolicy:  ShouldTrackPressure=1 OnlyTopDown=0 OnlyBottomUp=0
```

换成 `rocket-rv64`、`sifive-u74` 都是同一行。命令行可以覆盖它：

| 命令 | `OnlyTopDown` | `OnlyBottomUp` |
|---|---|---|
| 不加 flag（RISC-V 覆盖后） | 0 | 0 |
| `-misched-prera-direction=topdown` | 1 | 0 |
| `-misched-prera-direction=bottomup` | 0 | 1 |
| `-misched-prera-direction=bidirectional` | 0 | 0 |

这张表是本机跑出来的，不是文档抄的。注意这个 flag **没有** `default` 这个取值：写 `-misched-prera-direction=default` 会直接报 `Cannot find option named 'default'`。不加 flag 时策略由 `overrideSchedPolicy()` 决定；RISC-V 恰好也是双向，所以日志和显式 `bidirectional` 一样。

### RISC-V 的定制策略做了什么

`RISCVMachineScheduler.cpp` 里的 `RISCVPreRAMachineSchedStrategy` 只做一件事：在 `GenericScheduler::tryCandidate` 的排序结果上，插入 `vsetvli` 兼容性判断。

```cpp
bool RISCVPreRAMachineSchedStrategy::tryCandidate(SchedCandidate &Cand,
                                                  SchedCandidate &TryCand,
                                                  SchedBoundary *Zone) const {
  //-------------------------------------------------------------------------//
  // Below is copied from `GenericScheduler::tryCandidate`.
  // FIXME: Is there a way to not replicate this?
  //-------------------------------------------------------------------------//
```

这段注释值得记住：RISC-V 并没有重写整个启发式，而是复制一份再插入自己的 tie-break。文件顶部的 `DEBUG_TYPE` 是 `riscv-prera-sched-strategy`，所以调试时用：

```bash
"$LLC" ... -debug-only=riscv-prera-sched-strategy ...
```

对纯标量代码，这条策略不改变任何东西；它只在 RVV 的 `vsetvli` 附近影响顺序。第 23 章会专门拆它。

### post-RA：同一个框架，另一套默认值

post-RA 的 pass 名是 `postmisched`（`INITIALIZE_PASS_BEGIN(PostMachineSchedulerLegacy, "postmisched", ...)`），默认开启（`EnablePostRAMachineSched` 的 `cl::init(true)`），但是否真的跑由 target 决定。

RISC-V 的 post-RA 方向来自 `RISCVTuneInfo`：

```tablegen
class RISCVTuneInfo {
  ...
  code PostRASchedDirection = TopDown;
}
```

所以除非 `-misched-postra-direction` 覆盖，RISC-V 的 post-RA 是 **top-down**。这一点和 pre-RA 的 bidirectional 正好相反，是本课后面 Ch 17 要展开的重点。

## 观测手段速查

| 想看什么 | 命令 |
|---|---|
| 调度前后的 MIR | `-print-before=machine-scheduler -print-after=machine-scheduler` |
| DAG 结构、SU 编号、边延迟 | `-misched-print-dags` |
| 每个 region 的调度过程 | `-debug-only=machine-scheduler` |
| 只跑一个函数/基本块 | `-misched-only-func=f -misched-only-block=0` |
| 限制调度指令数（调试用） | `-misched-limit=N` |
| 关掉 pre-RA / post-RA | `-enable-misched=false` / `-enable-post-misched=false` |
| pass 顺序 | `-debug-pass=Structure` |

`-misched-print-dags` 的输出里，`SU(n)` 就是 DAG 节点编号，和 MIR 一一对应。`min.ll` 在 `rocket-rv64` 下：

```text
SU(5):   %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
  # preds left       : 1
  # succs left       : 1
  Latency            : 2
  Depth              : 1
  Height             : 9
  Predecessors:
    SU(4): Data Latency=1 Reg=%0
  Successors:
    SU(6): Data Latency=2 Reg=%5
    SU(7): Ord  Latency=0 Cluster
```

同一个 `SU(5)`，换成 `sifive-u74` 就是 `Latency: 3`、`SU(6): Data Latency=3`；换成 `xiangshan-nanhu` 是 `5`。**DAG 形状一样，数字不一样，差别全部来自 sched model**——这就是下一章的主题。

## 踩雷集锦

1. **以为 MachineScheduler 会跨 call 重排**。不会。call 是硬边界，`ADJCALLSTACKDOWN/UP` 改 `sp` 也是边界。看到「call 前后的指令顺序没变」不是调度器失灵，是它根本没权限动。想把两侧的指令拉到一起，只能靠 inlining 或 machine sinking 这类别的 pass。

2. **把 `-print-before=machine-scheduler` 当成 ISel 输出**。这两者之间还隔着 PHI 消除、two-address、register coalescer、pseudo expansion 等一串 pass（见上面的 pipeline 摘录）。要看 ISel 的原始选择，用 `-print-after-isel`（或新版 pass 名 `-print-after=finalize-isel`）。

3. **拿 `-misched-prera-direction` 在小函数上找差异**。`min.ll` 三个方向在 `rocket-rv64` 上给出完全相同的顺序（这条函数只有一条关键路径，谁来排都一样）。到了 `ldmul.ll`（40 个 load + 40 个 mul），topdown 0 条栈访问、bottomup 64 条、bidirectional 60 条。方向是策略，不是魔法：**依赖结构决定它有没有发挥空间**。

4. **跑 `llc` 不等输出**。这个构建里 `llc min.ll` 默认写 `min.s`，stdout 什么都没有。分析类命令要么 `-o -`，要么 `-o /dev/null` 加 `-print-*`（调试输出走 stderr）。写脚本时漏了 `-o -`，diff 会拿两个旧文件比较，结论全错。

5. **把 `-enable-post-misched=false` 当成关掉所有调度**。它只关 post-RA 那一次。pre-RA 的 `MachineScheduler` 还在，SelectionDAG 内部的 `ScheduleDAGSDNodes` 更是一直都在。要逐层剥离，得分别关：`-enable-misched=false`、`-pre-RA-sched=source`、`-enable-post-misched=false`。

6. **在 `-O0` 上研究 post-RA 重排**。`-O0` 的 pipeline 里 fast regalloc + 大量 spill 会主导顺序，post-RA 几乎没有可移动的空间。先用 `-O2` 建立基线。

## 动手练习

### 练习 1：数 region

用 `regions.ll`，把 `@h` 的调用改成 `add`（去掉第二个 call），重新跑 `-misched-print-dags`，看 `bb.1` 的 `RegionInstrs` 从 3 变成多少。解释为什么不是 +1。

<details>
<summary>提示</summary>

去掉 call 后，`ADJCALLSTACKUP` 也没了，边界少了一组；region 会把原来被切开的几段合并。
</details>

### 练习 2：制造一个新边界

在 `multi` 的 `entry` 块里插入一条内联汇编：

```llvm
  call void asm sideeffect "", "~{memory}"()
```

重新看 region 数量和 `RegionInstrs`。这条 asm 没有寄存器约束，为什么仍然可能改变切分？（提示：`INLINEASM_BR` 才是硬边界，普通 `INLINEASM` 靠内存依赖影响建图，不一定切断 region。用输出验证你的猜想，不要背结论。）

### 练习 3：只调度一个函数

在有两个函数的 IR 上（把 `big.ll` 复制一份，加一个 `@other`），对比：

```bash
"$LLC" ... -debug-only=machine-scheduler big2.ll -o /dev/null 2>&1 | grep -c 'Current Schedule Region'
"$LLC" ... -misched-only-func=big -debug-only=machine-scheduler big2.ll -o /dev/null 2>&1 | grep -c 'Current Schedule Region'
```

确认 `-misched-only-func` 只影响调试输出/调度范围，不改变另一个函数的代码。

### 练习 4：确认 post-RA 的默认方向

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 min.ll -o - \
  | sed -n '/^f:/,/ret/p'
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -misched-postra-direction=bidirectional min.ll -o - | sed -n '/^f:/,/ret/p'
```

两条命令的输出应当不同。再用 `-print-after=postmisched` 确认差异发生在 post-RA 这一层，而不是更早。

## 重点整理

- MachineScheduler 是 MIR 层 pass，在 pre-RA 和 post-RA 各有一个实例；两者共用 `ScheduleDAGMI` 框架。
- 调度单位是 **region**：call、terminator、label、修改 `sp` 的指令、PHI、fake use 都是边界。
- region 内部固定四步：`enterRegion → buildSchedGraph → schedule → exitRegion`。
- `ScheduleDAGMILive` 在 `ScheduleDAGMI` 之上加了 live interval 与寄存器压力跟踪（`RPTracker`、`VRegUses`、`SUPressureDiffs`）。
- RISC-V 通过 `overrideSchedPolicy()` 把 pre-RA 默认改成 **bidirectional**，并强制 `ShouldTrackPressure = true`。
- RISC-V 的 pre-RA 策略类是 `RISCVPreRAMachineSchedStrategy`，只在 `GenericScheduler` 之上插了 `vsetvli` tie-break。
- post-RA 由 `postmisched` pass 执行，RISC-V 的 `TuneInfo` 默认方向是 **TopDown**，与 pre-RA 相反。
- 观测入口：`-print-before/after=machine-scheduler`、`-misched-print-dags`、`-debug-only=machine-scheduler`、`-debug-pass=Structure`。

## 自我检核

- [ ] 我能说出 `regions.ll` 里每个 region 的边界指令是哪一条，以及为什么。
- [ ] 我能解释 `ADJCALLSTACKDOWN` 为什么会切断 region。
- [ ] 我能不看源码写出 RISC-V pre-RA 的 `RegionPolicy` 三个字段的值。
- [ ] 我知道 `ScheduleDAGMILive` 比 `ScheduleDAGMI` 多了哪三类状态。
- [ ] 我能用一条命令打印某个函数的 DAG 和 SU 编号。
- [ ] 我知道 `-enable-misched=false` 和 `-enable-post-misched=false` 分别关掉什么、不关掉什么。
- [ ] 我能解释为什么 `min.ll` 上三个调度方向给出相同顺序，而 `ldmul.ll` 上差异巨大。

## 延伸阅读

- **`llvm/include/llvm/CodeGen/MachineScheduler.h`（文件头注释 + `ScheduleDAGMILive` 类声明）**
  - **读哪里**：第 1–70 行的扩展点说明；第 487–540 行的成员变量。
  - **和本章的关联**：target 如何只替换策略而不替换 pass；压力跟踪状态到底存在哪。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`scheduleRegions`，约 1016–1105 行）**
  - **读哪里**：region 收集、空 region 跳过、`enterRegion/schedule/exitRegion` 的调用顺序。
  - **和本章的关联**：把「region 是调度单位」这句话落到代码。

- **`llvm/lib/Target/RISCV/RISCVSubtarget.cpp`（`overrideSchedPolicy`，约 257–271 行）**
  - **读哪里**：三行策略赋值和两段注释。
  - **和本章的关联**：RISC-V 为什么默认双向、为什么强制跟踪压力。

- **`llvm/lib/Target/RISCV/RISCVMachineScheduler.cpp`（全文，约 300 行）**
  - **读哪里**：`tryVSETVLIInfo` 与 `tryCandidate` 的拼接方式。
  - **和本章的关联**：一个真实的 target 策略扩展长什么样。

- **`llvm/lib/CodeGen/TargetInstrInfo.cpp`（`isSchedulingBoundary`，约 1690–1710 行）**
  - **读哪里**：terminator、position、`INLINEASM_BR`、栈指针四条规则。
  - **和本章的关联**：解释 region 的实际切分位置。

- **LLVM 官方文档：*Machine Instruction Scheduler*（CodeGenerator 文档页）**
  - **读哪里**：Overview 与 "Scheduling Regions" 两节。
  - **和本章的关联**：把本章的实测结论对回上游设计意图；注意文档版本可能落后于 24.0.0git 的实现细节。

→ [下一章：Sched model 三层](./12-sched-model-three-layers.md)
