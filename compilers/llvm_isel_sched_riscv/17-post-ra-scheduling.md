# Ch 17 Post-RA scheduling、hazard 与 anti-dependency

> **目标**：搞清寄存器分配之后的第二次调度还能做什么、不能做什么。重点回答三个问题：为什么 RISC-V 的 post-RA 默认是 top-down、`-break-anti-dependencies` 为什么对 RISC-V 没效果、`Disabled scoreboard hazard recognizer` 到底在说什么。
>
> **环境**：LLVM 24.0.0git；`llc`；pass 名 `postmisched`；`-mcpu=sifive-u74 -mattr=+m -O2`；对照 `-enable-post-misched=false`、`-misched-postra-direction`、`-break-anti-dependencies`。周期数用 `llvm-mca` 在同一 CPU 上评估。

## 为什么 RA 之后还要再排一次

寄存器分配会把虚拟寄存器换成物理寄存器，这个过程中：

- 插入大量 spill/reload。
- 把长 live range 切开（split）。
- 引入 COPY，随后被 coalesce 或消除。

这些操作会打乱 pre-RA scheduler 精心排好的顺序。举一个最直观的例子：pre-RA 时两条指令用的是不同的虚拟寄存器，可以自由换序；RA 之后它们可能被分到同一个物理寄存器，产生了 **反依赖**（WAR）或 **输出依赖**（WAW），顺序就被锁死了；反过来，spill 插入的 `ld/sd` 也可能创造出新的调度机会。

post-RA scheduler 的任务不是「再优化一遍」，而是：

1. 在物理寄存器的约束下，把被 RA 打乱的顺序修回来。
2. 填延迟槽（load-use、乘法器、分支延迟）。
3. 打散不必要的反依赖（如果 target 支持）。
4. 为后续的 peephole（load/store pair、macro fusion）创造相邻关系。

它**不能**做的事：新增寄存器、改变 spill 决策、跨 call 重排、跨基本块移动。

## 先建立直觉：两次调度看到的世界

```text
                pre-RA scheduler              post-RA scheduler
信息           虚拟寄存器 + live interval     物理寄存器 + 固定约束
能改顺序吗     能，空间大                    能，空间小
能改 spill 吗  间接（通过压力）              不能，spill 已经定了
能跨 call 吗   不能                          不能
主要目标       ILP vs pressure 平衡           填延迟、打散反依赖
默认方向       bidirectional（RISC-V）        topdown（RISC-V TuneInfo）
```

最后一行是本章第一个反直觉点：**pre-RA 默认双向，post-RA 默认单向 top-down**。来源是 `RISCVTuneInfo` 的字段默认值：

```tablegen
class RISCVTuneInfo {
  ...
  code PostRASchedDirection = TopDown;
}
```

而 pre-RA 的方向在 `RISCVSubtarget::overrideSchedPolicy()` 里被改成双向。两者不是同一个旋钮。

## 主线一：post-RA 到底改了多少

`min.ll`、`sifive-u74`，`-print-after=postmisched`：

默认（top-down）：

```text
renamable $x12 = ADD killed renamable $x14, killed renamable $x12
renamable $x14 = LD renamable $x10, 0
renamable $x10 = LD killed renamable $x10, 8
renamable $x11 = MUL killed renamable $x14, killed renamable $x11
renamable $x10 = MUL killed renamable $x10, killed renamable $x13
renamable $x11 = ADD killed renamable $x12, killed renamable $x11
renamable $x10 = ADD killed renamable $x11, killed renamable $x10
```

`-misched-postra-direction=bidirectional`：

```text
renamable $x12 = ADD killed renamable $x14, killed renamable $x12
renamable $x14 = LD renamable $x10, 0
renamable $x10 = LD killed renamable $x10, 8
renamable $x11 = MUL killed renamable $x14, killed renamable $x11
renamable $x11 = ADD killed renamable $x12, killed renamable $x11   <-- 提前
renamable $x10 = MUL killed renamable $x10, killed renamable $x13
renamable $x10 = ADD killed renamable $x11, killed renamable $x10
```

差异只有一条 `ADD` 的位置。最终汇编也一致地反映出来：

```asm
# 默认 / topdown / bottomup
add a2, a2, a4
ld  a4, 0(a0)
ld  a0, 8(a0)
mul a1, a4, a1
mul a0, a0, a3
add a1, a1, a2
add a0, a0, a1

# bidirectional
add a2, a2, a4
ld  a4, 0(a0)
ld  a0, 8(a0)
mul a1, a4, a1
add a1, a1, a2        <-- 提前一条
mul a0, a0, a3
add a0, a0, a1
```

### 先确认它到底跑不跑

pass 在 `-O1` 以上的 pipeline 里（`RISCVPassConfig` 构造函数把旧的 `PostRASchedulerID` 替换成 `PostMachineSchedulerID`），但**是否真正调度**由两件事决定：

```cpp
  if (EnablePostRAMachineSched.getNumOccurrences()) {
    if (!EnablePostRAMachineSched)
      return false;
  } else if (!MF.getSubtarget().enablePostRAMachineScheduler()) {
    LLVM_DEBUG(dbgs() << "Subtarget disables post-MI-sched.\n");
    return false;
  }
```

也就是说：

- 你显式写了 `-enable-post-misched=false` → 关。
- 你显式写了 `-enable-post-misched=true` → 强制开，跳过 target gate。
- 什么都没写 → 看 `enablePostRAMachineScheduler()`；RISC-V 的 `enablePostRAScheduler()` 返回 tune feature `UsePostRAScheduler`。

`UsePostRAScheduler` 由 `TunePostRAScheduler` 打开，`RISCVProcessors.td` 里有 17 处引用，主要集中在 SiFive 系列。实测（`ldmul.ll`，比较默认与 `-enable-post-misched=false` 的最终汇编）：

```text
rocket-rv64:     SAME     <- 没跑
sifive-u74:      DIFF     <- 跑了
xiangshan-nanhu: SAME     <- 没跑
generic:         SAME     <- 没跑
```

这解释了一个常见困惑：在 `rocket-rv64` 上改 `-misched-postra-direction` 没有任何效果——post-RA 根本没跑。想强制验证，加 `-enable-post-misched=true`。

`min.ll` 太小，三种方向的周期都一样。换 `ldmul.ll`（40 load + 40 mul）才有量级差异：

| 配置 | 栈访问 | mca 周期（10 iter） |
|---|---|---|
| 默认（top-down） | 60 | 1424 |
| `-enable-post-misched=false` | 60 | 1514 |
| `-misched-postra-direction=topdown` | 60 | 1424 |
| `-misched-postra-direction=bottomup` | 60 | 1474 |
| `-misched-postra-direction=bidirectional` | 60 | 1454 |

三个观察：

1. **默认 = topdown**（1424 完全一致），验证了 `TuneInfo` 的默认值。
2. **关掉 post-RA 会变慢**（1514），说明这一次重排确实有价值。
3. **栈访问数完全不变**（60）——post-RA 不能改 spill，它只能在既有 spill 之间重排。

第 3 点很重要：如果你看到「post-RA 之后 spill 变多了」，那一定不是 post-RA 干的，去看 RA 或更早的 pass。

### 为什么 post-RA 的收益比 pre-RA 小

`ldmul.ll` 上 pre-RA 方向能让栈访问从 64 变 0（Ch 15），post-RA 只能让周期从 1514 变 1424。原因：

- pre-RA 能改 live range，进而改 spill；post-RA 不能。
- post-RA 的物理寄存器约束更紧，可移动的窗口更小。
- RISC-V 是 load/store 架构，内存操作的顺序受地址依赖限制，能挪的空间本来就少。

所以优化的优先级永远是：**先看 pre-RA 和 IR，再看 post-RA**。

## 主线二：anti-dependency 与 `-break-anti-dependencies`

反依赖（WAR）是「后面的指令写了一个前面的指令要读的寄存器」。它本身不是真依赖，但会限制重排：

```text
  mul  t0, a0, a1      # t0 = ...
  add  a2, a2, t0      # 读 t0
  mul  t0, a3, a4      # 写 t0 -> 与上面的读构成 WAR
```

如果能把第三条的目标寄存器换成 `t1`，前两条和第三条就能重叠。这个变换叫 **anti-dependency breaking**，由 post-RA scheduler 在 `exitRegion()` 里做（`AntiDepState::breakAntiDependencies`）。

`llc` 的开关：

```text
--break-anti-dependencies=<critical|all|none>
--misched-postra-break-antideps=<value>
```

但 RISC-V **没有启用它**。`TargetSubtargetInfo::getAntiDepBreakMode()` 的默认实现是：

```cpp
virtual AntiDepBreakMode getAntiDepBreakMode() const { return ANTIDEP_NONE; }
```

而 RISC-V 没有覆盖这个方法（在 `RISCVSubtarget.h/.cpp` 里搜不到 `getAntiDepBreakMode`），所以模式是 `ANTIDEP_NONE`。实测也印证了这一点：

```bash
for b in critical all none; do
  "$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
    -break-anti-dependencies=$b min.ll -o /dev/null -print-after=postmisched
done
```

三份输出完全一样。

**为什么 RISC-V 不启用？** 反依赖打断需要「有空闲的物理寄存器可以换」，而 RISC-V 在 RA 之后通常没有富余；而且它要改写指令的目标寄存器，可能破坏 kill flags 和后续的 peephole。对顺序核来说，收益通常不如填延迟槽。这是一个典型的「通用框架有，target 选择不用」的例子。

对比：X86 覆盖成 `ANTIDEP_CRITICAL`，PowerPC 覆盖成 `ANTIDEP_ALL`，所以同样的 `-break-anti-dependencies` 在它们上面会有效果。AArch64 在 LLVM 24.0.0git 里没有覆盖这个方法，行为与 RISC-V 一样是 `ANTIDEP_NONE`。

## 主线三：hazard 到底是什么

`-debug-only=machine-scheduler` 里会看到两类 hazard 相关日志：

```text
Disabled scoreboard hazard recognizer
Disabled scoreboard hazard recognizer
  hazard: SU(12) ReadyCycle = 3 is later than CurrCycle = 0 on an unbuffered resource
  hazard: SU(11) ReadyCycle = 6 is later than CurrCycle = 3 on an unbuffered resource
```

这两类是完全不同的东西。

### `Disabled scoreboard hazard recognizer`

`ScoreboardHazardRecognizer` 是旧式（`InstrItinerary` 风格）的 hazard 识别器，靠 itinerary 的阶段表判断「这条指令现在发会不会撞上前面指令的流水阶段」。它的构造函数：

```cpp
  // If MaxLookAhead is not set above, then we are not enabled.
  if (!isEnabled())
    LLVM_DEBUG(dbgs() << "Disabled scoreboard hazard recognizer\n");
```

RISC-V 没有 `InstrItineraryData`，`MaxLookAhead` 保持 0，所以这个识别器不启用。**这不是错误，也不是「RISC-V 没有 hazard」**，只是它不用这套机制。

### `hazard: SU(...) ... on an unbuffered resource`

这条来自 `SchedBoundary::checkHazard()`（`MachineScheduler.cpp` 约 3129 行）：

```cpp
  bool IsBuffered = SchedModel->getMicroOpBufferSize() != 0;
  bool HazardDetected = !IsBuffered && ReadyCycle > CurrCycle;
```

含义：模型是**顺序核**（`MicroOpBufferSize == 0`），且这条指令的资源要到 `ReadyCycle` 才空闲，所以现在发不出去，只能等。

注意 `!IsBuffered` 这个条件：乱序核（`MicroOpBufferSize > 0`）永远不会打印这条日志，因为后面的指令可以先进入缓冲区。**hazard 检查只对顺序核有意义**，这也解释了为什么 `generic-ooo` / `xiangshan-nanhu` 的日志里看不到它。

`-disable-sched-hazard` 只是关掉这个检查（用于调试），不改变模型；实测 `min.ll` 的输出顺序不变，因为真正决定顺序的是资源和依赖，不是这条日志。

## 底层机制：post-RA 的调度器长什么样

post-RA 用的是 `ScheduleDAGMI`（不是 `ScheduleDAGMILive`），因为没有虚拟寄存器需要跟踪压力：

```text
PostMachineScheduler
   └── ScheduleDAGMI  +  PostGenericScheduler
         - 建图：buildSchedGraph（物理寄存器依赖 + 内存依赖）
         - 调度：PostGenericScheduler::pickNode
         - 收尾：AntiDepState::breakAntiDependencies（RISC-V 不启用）
```

`PostGenericScheduler` 的启发式和 `GenericScheduler` 不同：它没有压力项（压力已经是物理寄存器的既成事实），主要看：

- 关键路径（height/depth）。
- 资源冲突。
- 延迟槽填充。
- 原始顺序。

所以 post-RA 的重排通常更「保守」，只在能明确减少 stall 时才动。

## 踩雷集锦

1. **用 `-print-after=machine-scheduler` 看 post-RA**。那是 pre-RA。post-RA 的 pass 名是 `postmisched`，要用 `-print-after=postmisched`。两个 pass 的输出混在一起看，会把 pre-RA 的功劳记到 post-RA 头上。

2. **以为 `-enable-post-misched=false` 关掉了所有调度**。它只关 post-RA；pre-RA 的 `MachineScheduler` 和 SelectionDAG 内部的 `ScheduleDAGSDNodes` 都还在。要逐层关，分别用 `-enable-misched=false` 和 `-pre-RA-sched=source`。

3. **以为 post-RA 能减少 spill**。`ldmul.ll` 上 post-RA 开/关的栈访问都是 60。spill 在 RA 阶段就定了，post-RA 只能重排既有的 `ld/sd`。

4. **以为 RISC-V 的 `-break-anti-dependencies` 有用**。`getAntiDepBreakMode()` 是 `ANTIDEP_NONE`，三个取值输出完全一样。这是「通用选项 + target 未启用」的典型陷阱。

5. **把 `Disabled scoreboard hazard recognizer` 当成「RISC-V 不检查 hazard」**。它只是说不用旧式 scoreboard 识别器；真正限制发射的是 sched model 的 unbuffered resource 检查，日志里另有一行 `hazard: ... on an unbuffered resource`。

6. **在乱序核上找 hazard 日志**。`MicroOpBufferSize > 0` 时 `IsBuffered` 为真，`checkHazard` 那条日志永远不会打印。用 `rocket-rv64` 或 `sifive-u74` 才能看到。

## 动手练习

### 练习 1：确认默认方向

对 `ldmul.ll` 跑：

```bash
for d in topdown bottomup bidirectional; do
  "$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
    -misched-postra-direction=$d ldmul.ll -o pra_$d.s
done
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 ldmul.ll -o pra_default.s
```

用 `cmp` 确认 `pra_default.s` 和 `pra_topdown.s` 是否完全相同（本机实测：相同）。再比较周期：默认 1424、bottomup 1474、bidirectional 1454。

### 练习 2：验证 post-RA 不改 spill

比较 `pra_default.s` 和 `-enable-post-misched=false` 的栈访问条数，确认都是 60。然后在 `diff` 里找出顺序变化的指令，说明 post-RA 只动了顺序。

### 练习 3：对比 target 的 anti-dep 策略

用 `-mtriple=x86_64` 跑同样的 `-break-anti-dependencies=all`（x86 返回 `ANTIDEP_CRITICAL`），观察输出变化；再回到 RISC-V 确认无变化。如果不方便跑 x86，可以读 `X86Subtarget.h:452` 和 `PowerPCSubtarget.cpp:160` 两处实现，说明 `CRITICAL` 与 `ALL` 的区别。

### 练习 4：制造 hazard 日志

用 `rocket-rv64`（`MicroOpBufferSize=0`）跑 `ldmul.ll`，`grep 'hazard:'`；再用 `xiangshan-nanhu`（256 项 buffer）跑同一段，确认没有这条日志。解释 `IsBuffered` 的作用。

## 重点整理

- post-RA scheduler 的 pass 名是 `postmisched`；`-O1+` 的 pipeline 里有它，但默认是否调度由 `enablePostRAMachineScheduler()`（RISC-V 上是 tune feature `UsePostRAScheduler`）决定。`sifive-u74` 跑，`rocket-rv64`/`xiangshan-nanhu`/`generic` 不跑；显式传 `-enable-post-misched=true/false` 会覆盖这个 gate。
- RISC-V 的 post-RA 默认方向是 **TopDown**（`RISCVTuneInfo::PostRASchedDirection`），与 pre-RA 的 bidirectional 不同。
- post-RA 不能改 spill、不能跨 call、不能新增寄存器；它只能重排既有的物理寄存器指令。
- `ldmul.ll`：post-RA 开 1424、关 1514，栈访问都是 60——重排有价值，但不改 spill。
- RISC-V 的 `getAntiDepBreakMode()` 是 `ANTIDEP_NONE`，`-break-anti-dependencies` 无效。
- `Disabled scoreboard hazard recognizer` 表示没有 InstrItinerary，不是「没有 hazard」。
- `hazard: ... on an unbuffered resource` 只在 `MicroOpBufferSize == 0` 的顺序核上出现。

## 自我检核

- [ ] 我知道 post-RA 的 pass 名，并能用 `-print-after=` 打印它。
- [ ] 我能说出 RISC-V pre-RA 和 post-RA 的默认方向，以及各自的来源。
- [ ] 我能解释为什么 post-RA 不能减少 spill。
- [ ] 我知道 `-break-anti-dependencies` 对 RISC-V 无效的源码原因。
- [ ] 我能区分 `Disabled scoreboard hazard recognizer` 和 `hazard: ... unbuffered resource`。
- [ ] 我知道 `IsBuffered` 与 `MicroOpBufferSize` 的关系。
- [ ] 我能用 `ldmul.ll` 复现 post-RA 开/关的周期差。

## 延伸阅读

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`PostMachineScheduler` 与 `PostGenericScheduler`，约 390–470 行、4650–4760 行）**
  - **读哪里**：post-RA 的策略类、`initPolicy`、`pickNode`。
  - **和本章的关联**：post-RA 和 pre-RA 的启发式差异。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`AntiDepState::breakAntiDependencies`，约 1160–1290 行）**
  - **读哪里**：反依赖打断的扫描与重写逻辑。
  - **和本章的关联**：为什么它需要空闲物理寄存器，以及为什么 RISC-V 不启用。

- **`llvm/include/llvm/CodeGen/TargetSubtargetInfo.h`（第 300 行附近）**
  - **读哪里**：`getAntiDepBreakMode()` 的默认实现。
  - **和本章的关联**：`ANTIDEP_NONE` 的来源。

- **`llvm/lib/CodeGen/ScoreboardHazardRecognizer.cpp`（约 40–75 行）**
  - **读哪里**：`MaxLookAhead` 与 `isEnabled()`。
  - **和本章的关联**：解释 `Disabled scoreboard hazard recognizer`。

- **`llvm/lib/Target/RISCV/RISCVSubtarget.cpp`（`overridePostRASchedPolicy`，约 273–286 行）**
  - **读哪里**：post-RA 方向的四个分支。
  - **和本章的关联**：`-misched-postra-direction` 如何覆盖 `TuneInfo`。

- **`llvm/lib/Target/RISCV/RISCVProcessors.td`（`RISCVTuneInfo`，约 18–52 行）**
  - **读哪里**：`PostRASchedDirection = TopDown` 的默认值。
  - **和本章的关联**：RISC-V post-RA 默认方向的唯一来源。

→ [练习 C：给一条 RISC-V 指令补 `SchedRW`，用 mca 验证](./practice-c-schedrw-and-mca.md)
