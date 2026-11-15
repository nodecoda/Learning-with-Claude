# Ch 15 调度策略：critical path、pressure、top-down/bottom-up

> **目标**：把 `GenericScheduler` 选点的优先级顺序讲清楚，并用同一段 IR、同一个 CPU、只改方向的方式，量出「方向 → live range → spill → 周期」这条链。学完你能解释为什么某个函数上 `topdown` 比 `bottomup` 少 60 条栈访问，也能解释为什么有些函数三个方向完全一样。
>
> **环境**：LLVM 24.0.0git；`-mcpu=sifive-u74 -mattr=+m`；`-O2`；`llvm-mca` 用同一 CPU 评估。`ldmul.ll` 是本章的主力测试（40 个 `load` + 40 个 `mul` + 累加链），`big.ll` 是小型对照。

## 为什么方向不是「从哪头填」这么简单

`-misched-prera-direction` 表面上是「从 DAG 的顶部往下排」还是「从底部往上排」。但 DAG 的顶部是「没有前驱的指令」，通常是 load 和参数拷贝；底部是「没有后继的指令」，通常是返回前的累加。方向不同，意味着：

- **top-down**：先把能发的都发了，load 尽早进流水线，但所有中间值同时活着，寄存器压力高。
- **bottom-up**：从结果倒推，优先满足关键路径上的消费者，中间值活得短，但 load 可能被拖到很晚才发。
- **bidirectional**：两头同时推进，每步比较「从顶部取一个」和「从底部取一个」哪个更划算。

所以方向直接决定 **live range 长度**，进而决定 spill 数量。这不是理论推演——下面有一张 0 到 84 的实测表。

## 先建立直觉：三个方向，三种填充顺序

以 `ldmul.ll` 的简化 DAG 为例（40 组 `LD → MUL`，最后串行累加）：

```text
        LD0  LD1  LD2 ... LD39
         │    │    │        │
        MUL0 MUL1 MUL2 ... MUL39
          \    |    /        /
           \   |   /        /
            ADD ... ADD ...          <- 串行累加链
                    │
                   RET
```

三种策略看到的世界：

```text
top-down                          bottom-up
─────────                         ─────────
先发 LD0..LDk（能发多少发多少）    先看 RET 需要的最后一个 ADD
→ 同时活着的 load 结果很多          → 递归地要求它的两个输入
→ 压力高，可能 spill               → load 被「按需」拉进来
→ 但 load 早，延迟被遮住            → 压力低，但 load 可能太晚

bidirectional
─────────────
每步比较：
   Top 队列里最该发的 vs Bot 队列里最该发的
   谁的关键路径贡献大 / 谁不涨压力 → 发谁
```

`ldmul.ll` 上，这三种选择的结果差距极大：

| 配置 | spill/restore 条数（栈访问） | mca 周期（10 iter） | IPC |
|---|---|---|---|
| `-misched-prera-direction=topdown` | **0** | **704** | 1.70 |
| 默认（bidirectional） | 60 | 1424 | 1.28 |
| `-misched-prera-direction=bottomup` | 64 | 1604 | 1.16 |
| `-enable-misched=false -pre-RA-sched=source` | 84 | 2014 | 1.02 |
| `-enable-misched=false -pre-RA-sched=list-ilp` | 40 | 1274 | 1.27 |

这张表值得贴在显示器上：**同一份 IR、同一个 CPU、同一套 sched model，只改调度方向，栈访问从 0 到 84，周期从 704 到 2014**。面试里「调度对性能有多大影响」的标准答案就是这种量级的实验。

## 主线一：候选排序的优先级

`GenericScheduler::tryCandidate()` 是一个「逐级淘汰」的比较器。新候选 `TryCand` 只有在某一级严格优于当前最优 `Cand` 时才替换，替换原因记在 `Reason` 里。顺序（`MachineScheduler.cpp` 约 4258–4362 行）：

| 优先级 | `Reason` | 判断 | 目的 |
|---|---|---|---|
| 1 | `FirstValid` | 第一个候选 | 初始化 |
| 2 | `PhysReg` | 物理寄存器 def/use 亲和 | 减少 COPY |
| 3 | `RegExcess` | 是否超过压力上限 | 避免溢出 |
| 4 | `RegCritical` | 是否推高临界压力集 | 保护最紧的寄存器类 |
| 5 | `Stall` | 无缓冲资源的 stall 拍数 | 避免卡在非流水单元 |
| 6 | `Cluster` | 是否与当前 cluster 同组 | 保持 load/store 聚集 |
| 7 | `Weak` | 弱依赖剩余数 | 解锁下游 |
| 8 | `RegMax` | 整个 region 的最大压力 | 全局压力控制 |
| 9 | `ResourceReduce` | 关键资源占用更少 | 平衡功能单元 |
| 10 | `ResourceDemand` | 需求资源更多（更专用） | 把通用资源留给别人 |
| 11 | `TopDepthReduce` / `BotHeightReduce` | 关键路径深度/高度 | 缩短 critical path |
| 12 | `TopPathReduce` / `BotPathReduce` | 路径长度 | 同上 |
| 13 | `NodeOrder` | 原始顺序 | 稳定排序 |

前四级全部和压力有关，这解释了 RISC-V 为什么强制 `ShouldTrackPressure = true`：如果不跟踪压力，第 3、4、8 级直接失效，调度器会退化成「只看关键路径」，spill 数量会失控。

### 实测：一次真实的候选比较

`ldmul.ll`、`sifive-u74`、`topdown`、`-debug-only=machine-scheduler`：

```text
RegionPolicy:  ShouldTrackPressure=1 OnlyTopDown=1 OnlyBottomUp=0
Top Pressure: GPRF16C=2 GPRF16C_with_GPRS07=2 GPRTC=2 GPRF16=2
Pick Top Cand SU(0) FIRST      [pre-RA]
Pick Top Cand SU(1) ONLY1      [pre-RA]
Pick Top Cand SU(2) WEAK       [pre-RA]
  TopQ.A TopLatency SU(2) 3c
Pick Top Cand SU(3) CLUSTER    [pre-RA]
  TopQ.A BotLatency SU(3) 18c
Pick Top Cand SU(4) CLUSTER    [pre-RA]
...
Pick Top Cand SU(42) ORDER      [pre-RA]
```

读法：

- `FIRST` / `ONLY1`：队列里只有一个候选，不用比。
- `WEAK`：SU(2) 靠「弱依赖」赢，说明它解锁了下游。
- `CLUSTER`：SU(3)、SU(4) 是 load cluster 的成员，被优先保持在一起。
- `ORDER`：到后面所有启发式打平，退回 NodeNum 顺序。

`TopQ.A` / `BotQ.A` 是队列标识，`TopLatency` / `BotLatency` 是这一步离关键路径还有多远。这些字母串看起来吵，但它们是「调度器为什么选它」的唯一直接证据。

## 主线二：为什么 topdown 在 `ldmul.ll` 上完胜

`ldmul.ll` 的形状是「40 个独立 load → 40 个独立 mul → 40 个串行 add」。三种方向的差别：

**topdown**：

```asm
ld  a2, 0(a1)
ld  a3, 8(a1)
ld  a4, 16(a1)
ld  a5, 24(a1)
mul a2, a2, a0
mul a3, a3, a0
mul a4, a4, a0
add a2, a2, a3
mul a5, a5, a0
ld  a3, 32(a1)
add a4, a4, a5
ld  a5, 40(a1)
...
```

load 只提前 4 条左右就被消费，`mul` 的结果立刻并入累加链。任何时刻活着的值不超过 5 个，所以 **0 次 spill**。

**bottomup**：

```asm
addi sp, sp, -240
sd   ra, 232(sp)
sd   s0, 224(sp)
sd   s1, 216(sp)
...
```

从结果倒推时，累加链的每个中间值都要保留到前面的 `mul` 算完，40 个 `mul` 结果几乎同时活着，寄存器不够，只能溢出到栈。栈帧 240 字节，`sd/ld` 共 64 条。

**bidirectional** 介于两者之间（60 条）。它不是「取平均」，而是每步独立比较 Top 和 Bot 两个候选，实际路径取决于压力阈值和关键路径的即时状态。

### 为什么 `min.ll` 三个方向完全一样

`min.ll` 只有一条依赖链：

```text
LD0 ─┐
     ├─ MUL0 ─┐
LD1 ─┘        ├─ ADD ─ ADD ─ ADD ─ RET
     └─ MUL1 ─┘
```

任何方向都必须遵守这条链，唯一的自由度是两条 `LD → MUL` 支路谁先。实测三个方向在 `rocket-rv64` 上给出完全相同的顺序（`LD LD MUL MUL ADD ADD ADD`）。这不是 bug，是「没有选择空间」。

**结论**：方向是策略，不是保证。判断某个函数值不值得调方向，先看它的 DAG 有没有多条可交错的独立链。

## 主线三：压力跟踪的粒度

`GenericScheduler::initPolicy()` 里有一个阈值：

```cpp
  RegionPolicy.ShouldTrackPressure = true;
  for (unsigned VT = MVT::i64; VT > (unsigned)MVT::i1; --VT) {
    ...
      unsigned NIntRegs = Context->RegClassInfo->getNumAllocatableRegs(
        TLI->getRegClassFor(LegalIntVT));
      RegionPolicy.ShouldTrackPressure = NumRegionInstrs > (NIntRegs / 2);
      break;
  }
```

小 region（指令数 ≤ 可分配寄存器数的一半）不跟踪压力，省编译时间。RISC-V 的 `overrideSchedPolicy()` 又把 `ShouldTrackPressure` 强制设回 `true`，等于取消了这个优化——因为 RISC-V 的 spill 代价高。

跟踪压力的两个视角：

- `TopPressure` / `BotPressure`：当前已调度区的边界压力。
- `Max Pressure`：整个 region 的静态最大压力（不随调度顺序变）。

实测 `ldmul.ll`：

```text
Max Pressure: GPRF16C=1 GPRF16C_with_GPRS07=1 GPRTC=1 GPRF16=41
```

三个方向都是 41——因为这是「region 里同时最多有多少个值活着」的上界，由 DAG 决定，不由顺序决定。真正随顺序变的是 `Top Pressure` / `Bot Pressure` 的即时值，以及最终 RA 是否 spill。

**这解释了一个常见误判**：看到 `Max Pressure` 相同就以为方向没用。Max 是静态上界，RA 看到的是动态 live range；`ldmul.ll` 的 0 vs 64 条栈访问就是动态差异。

## 主线四：其他策略旋钮

### load/store clustering

RISC-V 默认开启 load clustering（`EnableMISchedLoadClustering`）。它把相邻的 load 放进同一个 cluster，让它们尽量连续发射，为后续的 peephole（如 `Zilsd` 的 load pair）创造条件。

关掉它：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 \
  -mattr=+m,+disable-misched-load-clustering -O2 ldmul.ll -o -
```

实测 `ldmul.ll`：

| 配置 | 栈访问 | mca 周期（10 iter） |
|---|---|---|
| 默认（clustering 开） | 60 | 1424 |
| `+disable-misched-load-clustering` | 54 | 1374 |

clustering 在这里略微增加了压力（54 → 60），但换来了连续的 load，可能对真实核的取指/发射更友好。**没有免费的午餐**，这类开关必须用目标工作负载验证。

### latency heuristic

RISC-V 有一个 tune feature `disable-latency-sched-heuristic`。打开它（`-mattr=+disable-latency-sched-heuristic`）会设置 `Policy.DisableLatencyHeuristic`，跳过 `tryLatency` 那一级。

实测 `ldmul.ll` 上栈访问仍是 60、周期不变——因为这段代码的瓶颈是压力，不是延迟启发式。换一个关键路径更深的函数（比如 `big.ll` 加长乘法链）才会看到差异。这提醒你：**消融实验必须在会触发该启发式的输入上做**。

### `-disable-sched-*` 不是给 misched 用的

`llc -help-hidden` 里有一串：

```text
--disable-sched-critical-path
--disable-sched-height
--disable-sched-live-uses
--disable-sched-reg-pressure
--disable-sched-stalls
--disable-sched-vrcycle
```

这些是 **`sched=list-ilp`（旧 DAG 调度器）** 的开关，不是 MachineScheduler 的。对 misched 用它们不会有任何效果。要消融 misched，用：

- `-misched-prera-direction`
- `-enable-misched=false`
- `+disable-misched-load-clustering` / `+disable-misched-store-clustering`
- `+disable-latency-sched-heuristic`
- `-misched-postra-direction`（post-RA）

## 底层机制：双向调度怎么选边

`GenericScheduler::pickNodeBidirectional()` 的骨架：

```cpp
  // Schedule as far as possible in the direction of no choice.
  if (SUnit *SU = Bot.pickOnlyChoice()) { ... }
  if (SUnit *SU = Top.pickOnlyChoice()) { ... }

  // Otherwise compare the best candidate from each boundary.
  ...
```

意思是：

1. 如果某一侧只有一个候选（`ONLY1`），直接发，不用比。
2. 否则分别从 Top 和 Bot 队列取最优候选，再用 `tryCandidate` 比较。
3. 比较时 `Zone` 传 `nullptr`（不同边界），所以 `SameBoundary` 为 false，`Stall`、`Weak`、`ResourceReduce` 这些「同级微调」被跳过，只比压力、cluster、关键路径这些跨边界可比的指标。

这就是为什么双向调度在压力大的图上表现好：它可以在「顶部发一个 load」和「底部发一个 add」之间做全局选择，而不是一条路走到黑。

## 踩雷集锦

1. **以为改方向一定改周期**。`big.ll`（20 条指令、两条独立链）在 `sifive-u74` 上，三个方向的 MIR 顺序明显不同，但 mca 都是 1403 拍。小函数上，顺序的自由度不足以改变资源/依赖的下界。方向的效果要在「压力接近上限」或「多链可交错」的输入上才明显。

2. **以为 topdown 总是更好**。`ldmul.ll` 上 topdown 0 spill，但换一个「结果需要尽早算出来给分支用」的函数，bottomup 可能更好。方向没有全局最优，只有对某类 DAG 形状更合适。

3. **只看 spill 条数，不看 spill 位置**。0 条 vs 64 条很直观，但 54 vs 60 这种小差异要看 spill 是否落在热路径、是否能用 `s0-s11` 而不是内存。用 `-print-after=regalloc` 或看最终汇编的栈偏移。

4. **拿 `--disable-sched-*` 调 misched**。这些 flag 属于 `sched=list-ilp`，对 MachineScheduler 无效。混淆这两个调度器是面试里最常见的减分项之一。

5. **忘了 RISC-V 默认是 bidirectional**。不加 flag 时不是 bottom-up，也不是 top-down。你在 `-debug-only=machine-scheduler` 里看到 `OnlyTopDown=0 OnlyBottomUp=0` 就是双向。

6. **在 `-O0` 上做方向实验**。`-O0` 的 fast regalloc 和大量 spill 会淹没调度差异；`-O2` 才有干净的对照。

## 动手练习

### 练习 1：复现 0/60/64 的表

```bash
for d in topdown bottomup bidirectional; do
  "$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
    -misched-prera-direction=$d ldmul.ll -o ldmul_$d.s
  echo -n "$d "
  grep -cE '^\s+(sd|ld)\s+[^,]+,\s*-?[0-9]+\(sp\)' ldmul_$d.s
done
```

确认数字，然后用 `sed -n '/^ldmul:/,/^\.Lfunc_end/p'` 对比 topdown 和 bottomup 的前 20 条指令，指出 live range 的差别。

### 练习 2：验证「顺序变、周期不变」

对 `big.ll` 跑三个方向，保存汇编，分别用 mca 评估。确认三份汇编不同但周期相同。再解释：为什么 `big.ll` 的压力只有 9（`Max Pressure: GPRF16=9`），远低于 32 个寄存器，所以方向不影响 spill。

### 练习 3：关掉 clustering

对 `ldmul.ll` 加 `+disable-misched-load-clustering`，比较栈访问和周期。再对 `min.ll` 做同样的事，看有没有区别（提示：`min.ll` 只有两个 load，cluster 的收益本来就小）。

### 练习 4：读候选原因

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -misched-prera-direction=bidirectional -debug-only=machine-scheduler \
  ldmul.ll -o /dev/null 2>&1 | grep 'Pick' | head -20
```

统计前 20 次选择里各 `Reason` 出现几次。为什么 `REG-EXCESS` / `REG-CRIT` 在压力大的函数上出现得更频繁？

## 重点整理

- `GenericScheduler::tryCandidate` 是逐级淘汰：物理寄存器亲和 → 压力（Excess/Critical）→ stall → cluster → weak → 全局压力 → 资源 → 关键路径 → 原始顺序。
- 前四级以压力为核心，这是 RISC-V 强制 `ShouldTrackPressure = true` 的原因。
- 三个方向的实测差异（`ldmul.ll`）：topdown 0 spill / 704 拍，bidirectional 60 / 1424，bottomup 64 / 1604。
- 方向只在「多链可交错、压力接近上限」的 DAG 上有发挥空间；单链函数三个方向相同。
- `Max Pressure` 是静态上界，不随方向变；动态 live range 才决定 spill。
- RISC-V 默认 bidirectional；post-RA 默认 topdown（`RISCVTuneInfo::PostRASchedDirection = TopDown`）。
- `--disable-sched-*` 属于 `sched=list-ilp`，不是 MachineScheduler 的开关。

## 自我检核

- [ ] 我能背出 `tryCandidate` 前六级的顺序，并解释每一级防的是什么。
- [ ] 我能解释为什么 topdown 在 `ldmul.ll` 上 0 spill，而 bottomup 64 条。
- [ ] 我知道 `Max Pressure` 和实际 spill 数量为什么可能不一致。
- [ ] 我能用一条命令复现三个方向的栈访问数量。
- [ ] 我知道 `-misched-prera-direction` 不影响 post-RA。
- [ ] 我能说出至少两个「方向无用」的 DAG 形状。
- [ ] 我知道 `--disable-sched-reg-pressure` 对 misched 无效，以及正确的替代开关。

## 延伸阅读

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`GenericScheduler::tryCandidate`，约 4258–4362 行）**
  - **读哪里**：每一级 `tryPressure` / `tryLess` / `tryGreater` 的调用和 `Reason`。
  - **和本章的关联**：候选排序的唯一权威实现；对照本章的优先级表读。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`pickNodeBidirectional`，约 4390–4460 行）**
  - **读哪里**：`pickOnlyChoice` 与 Top/Bot 两侧比较的逻辑。
  - **和本章的关联**：解释双向调度如何做跨边界选择。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`initPolicy`，约 3980–4028 行）**
  - **读哪里**：`ShouldTrackPressure` 的阈值计算、`OnlyBottomUp` 的默认值。
  - **和本章的关联**：为什么小 region 不跟踪压力，以及 target 在哪里覆盖。

- **`llvm/lib/Target/RISCV/RISCVSubtarget.cpp`（`overrideSchedPolicy`）**
  - **读哪里**：三行赋值。
  - **和本章的关联**：RISC-V 为什么默认双向、为什么强制跟踪压力。

- **`llvm/lib/Target/RISCV/RISCVFeatures.td`（约 2080–2120 行）**
  - **读哪里**：`TuneDisableMISchedLoadClustering`、`TuneDisableMISchedStoreClustering`、`TuneDisableLatencySchedHeuristic`。
  - **和本章的关联**：RISC-V 暴露的三个调度 tuning 开关，以及它们的默认值。

- **LLVM Developers' Meeting: "Machine Instruction Scheduling in LLVM"（Andy Trick）**
  - **读哪里**：GenericScheduler 的启发式设计部分。
  - **和本章的关联**：设计动机；注意演讲年代较早，具体优先级以源码为准。

→ [下一章：调度与寄存器分配的拉扯](./16-scheduling-vs-regalloc.md)
