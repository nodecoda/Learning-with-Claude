# Ch 14 `llvm-mca`：把模型变成周期数，以及它的边界

> **目标**：能读 `llvm-mca` 的每一块报告，知道每个数字来自 sched model 的哪个字段，也知道哪些东西它根本不模拟。学完你能用 mca 做「同模型下的 A/B 对比」，而不会把它的周期数当成真机性能。
>
> **环境**：LLVM 24.0.0git 的 `llvm-mca`；汇编输入来自 `llc` 的实际输出；`-mattr=+m`；CPU 覆盖 `rocket-rv64`、`sifive-u74`、`generic-ooo`（需 `+64bit`）。周期数为模型输出。

## 为什么单独讲一个工具

前面两章把 sched model 拆成了资源、延迟、bypass。模型建好之后，怎么验证它「说得对」？两条路：

1. 上真机跑 `perf`，拿硬件计数器。
2. 用 `llvm-mca` 在模型上做静态模拟。

第一条路慢、依赖板子、受 cache 和频率干扰；第二条路快、可复现、能精确到「第几拍发射了哪条指令」。所以编译器开发里，mca 是改 sched model 时的第一道验证，也是面试里「你怎么知道这个改动有用」的标准答案之一。

但 mca 有一个必须说清楚的边界：**它模拟的是你给它的模型，不是芯片**。模型里 `mul=6`，mca 就按 6 算；真机可能是 5 也可能是 7。这一章后半段专门讲这个边界。

## 先建立直觉：mca 在模拟什么

```text
        汇编输入（-mcpu 决定模型）
                  │
                  ▼
        ┌──────────────────────┐
        │ 前端：取指/解码       │  mca 假设完美：无 miss、无分支错误
        └──────────────────────┘
                  │
                  ▼
        ┌──────────────────────┐
        │ Dispatch（发射宽度）  │  Dispatch Width 来自 SchedMachineModel.IssueWidth
        └──────────────────────┘
                  │
                  ▼
        ┌──────────────────────┐
        │ 调度器/保留站         │  顺序核看 MicroOpBufferSize=0
        │ 依赖 + 资源           │  乱序核看 buffer 大小
        └──────────────────────┘
                  │
                  ▼
        ┌──────────────────────┐
        │ 执行单元（ProcResource）│  WriteRes 的资源与 ReleaseAtCycles
        └──────────────────────┘
                  │
                  ▼
              Retire（按序）
```

mca 做的事：把每条指令拆成 uOp，按模型在每一拍决定「能不能发射、发到哪个单元、什么时候执行完、什么时候退休」。它**不**模拟 cache、TLB、分支预测、频率、功耗，也不模拟真实的乱序唤醒网络（只用一个近似的 buffer 模型）。

## 最小报告：七个字段

```bash
"$MCA" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m -iterations=100 s.s
```

```text
Iterations:        100
Instructions:      800
Total Cycles:      1001
Total uOps:        800

Dispatch Width:    1
uOps Per Cycle:    0.80
IPC:               0.80
Block RThroughput: 8.0
```

逐字段：

| 字段 | 含义 | 来源 |
|---|---|---|
| `Iterations` | 输入块重复几次 | 命令行 / 默认 100 |
| `Instructions` | 总指令数（含重复） | 输入 × iterations |
| `Total uOps` | 总微操作数 | 每条指令的 `NumMicroOps` 之和 |
| `Total Cycles` | 模拟的总周期（含流水线排空） | 模拟结果 |
| `Dispatch Width` | 每拍最多发射几个 uOp | `SchedMachineModel.IssueWidth` |
| `uOps Per Cycle` | `Total uOps / Total Cycles` | 计算值 |
| `IPC` | `Instructions / Total Cycles` | 计算值 |
| `Block RThroughput` | 无依赖背靠背执行时每轮的资源下界 | 资源压力最大值 |

`Block RThroughput = 8.0` 的意思是：这段 8 条指令的块，在 Rocket 模型上每轮至少占 8 拍（单发射，每拍一条）。`Total Cycles = 1001` 比 `8 × 100 = 800` 多，是因为块内 `ld → mul → add` 的依赖链每轮要等，流水线还有启动/排空。

### 同一段代码，三个模型

`s.s`（`llc` 输出的 `min.ll` 汇编，8 条指令）：

```asm
ld a5, 0(a0)
ld a0, 8(a0)
mul a1, a5, a1
mul a0, a0, a3
add a2, a2, a4
add a1, a1, a2
add a0, a0, a1
ret
```

`-iterations=100` 的实测：

| CPU | Total Cycles | Dispatch Width | IPC | Block RThroughput |
|---|---|---|---|---|
| `rocket-rv64` | 1001 | 1 | 0.80 | 8.0 |
| `sifive-u74` | 901 | 2 | 0.89 | 4.0 |
| `generic`（SpacemitX60） | 1201 | 2 | 0.67 | 4.0 |

三个数字很能说明问题：

- SiFive7 双发射，RThroughput 只有 Rocket 的一半（4 对 8），所以总周期少。
- SpacemitX60 也是双发射（RThroughput 4.0），但 `mul=6`、`ld=4`，依赖链更长，总周期反而比 SiFive7 多 300。
- Rocket 单发射但 `mul=4`、`ld=2`，依赖短，最终只比 SiFive7 多 100 拍。

**所以「发射宽度」和「延迟」是两个独立的杠杆**，这也是面试里「为什么双发射的核反而更慢」的标准解释路径。

## Instruction Info：每条指令的模型视图

```bash
"$MCA" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -instruction-info ops.s
```

```text
[1]    [2]    [3]    [4]    [5]    [6]    Instructions:
 1      3     0.50                        add	a0, a1, a2
 1      3     1.00                        mul	a0, a1, a2
 1      66    65.00                       div	a0, a1, a2
 1      3     1.00    *                   ld	a0, 0(a1)
 1      1     1.00           *            sd	a0, 0(a1)
```

列含义：

- `[1] #uOps`：这条指令拆成几个微操作。RISC-V 标量指令基本都是 1。
- `[2] Latency`：`WriteRes` 的 `Latency`（考虑 `ReadAdvance` 前）。
- `[3] RThroughput`：倒数吞吐，来自资源占用。
- `[4] MayLoad` / `[5] MayStore`：是否访问内存。
- `[6] HasSideEffects`：是否有副作用（`U` 标记）。

这张表就是 Ch 12/13 的数字来源。看到 `div` 的 `Latency=66`、`RThroughput=65.00`，就知道它几乎完全占用除法器；看到 `add` 的 `0.50`，就知道有两条流水可选。

## Resources 与资源压力

同一份输出的下半部分：

```text
Resources:
[0]   - VLEN512SiFive7FDiv
[1]   - VLEN512SiFive7IDiv
[2]   - VLEN512SiFive7PipeA
[3]   - VLEN512SiFive7PipeB
...

Resource pressure per iteration:
[0]    [1]    [2]    [3]    [4]    [5]    [6]    [7]
 -      -     4.00   4.00    -      -      -      -

Resource pressure by instruction:
[0]    [1]    [2]    [3]    [4]    [5]    [6]    [7]    Instructions:
 -      -     1.00    -      -      -      -      -     ld	a5, 0(a0)
...
```

`Resource pressure per iteration` 是「每轮迭代每个资源被占用多少拍」。上面这段 8 条指令里，PipeA 和 PipeB 各 4.00 拍，正好等于 `Block RThroughput` 的上限——这就是 4.0 的来源。

判断瓶颈的方法：**看哪个资源的 pressure 最接近（或超过）总周期数**。如果某个资源是 4.00，而总周期是 4，说明它满了；如果总周期是 9 而资源只有 4，说明瓶颈不在资源，在依赖。

## Dynamic Dispatch Stalls：为什么发不出去

`-all-views` 会多打印几块。`min.s`、`sifive-u74`、`-iterations=3`：

```text
Dynamic Dispatch Stall Cycles:
RAT     - Register unavailable:                      16  (57.1%)
RCU     - Retire tokens unavailable:                 0
SCHEDQ  - Scheduler full:                            0
LQ      - Load queue full:                           0
SQ      - Store queue full:                          0
GROUP   - Static restrictions on the dispatch group: 3  (10.7%)
USH     - Uncategorised Structural Hazard:           0
```

这里的 `RAT` 是 Register Alias Table：指令要写的目的寄存器还没有空闲的物理寄存器，所以发不出去。57.1% 的周期卡在这里，说明这段代码在当前模型下是**寄存器依赖**受限，不是执行单元受限。

`GROUP` 是静态发射组限制（比如某些指令必须成组发射）；RISC-V 标量基本没有，3 个周期属于收尾。

再往下还有：

```text
Dispatch Logic - number of cycles where we saw N micro opcodes dispatched:
[# dispatched], [# cycles]
 0,              10  (35.7%)
 1,              12  (42.9%)
 2,              6  (21.4%)
```

35.7% 的周期一条都没发出去——这就是依赖链的实际代价。

## Timeline：第几拍发生了什么

```text
Timeline view:
                    0123456789
Index     0123456789          01234567

[0,0]     DeeE .    .    .    . .   ld	a5, 0(a0)
[0,1]     .DeeE.    .    .    . .   ld	a0, 8(a0)
[0,2]     .  DeeE   .    .    . .   mul	a1, a5, a1
[0,3]     .   DeeE  .    .    . .   mul	a0, a0, a3
[0,4]     .   DeeE  .    .    . .   add	a2, a4, a2
[0,5]     .    DeeE .    .    . .   add	a1, a2, a1
[0,6]     .    .DeeE.    .    . .   add	a0, a1, a0
[0,7]     .    .DeeE.    .    . .   ret
```

字符含义（`TimelineView.h` 的 `DisplayChar`）：

| 字符 | 含义 |
|---|---|
| `D` | Dispatched：进入调度器 |
| `=` | Waiting：在调度器里等依赖或资源 |
| `e` | Executing：在执行单元里 |
| `E` | Executed：执行完 |
| `-` | Retire lag：等退休 |
| `R` | Retired：退休 |

读法：上面这张表里没有 `=`，因为 `sifive-u74` 是顺序核（`MicroOpBufferSize = 0`），mca 只在能发射时才派发，所以「派发」和「开始执行」几乎贴在一起；一行里 `D` 之后的横向偏移表示它占用的拍数。

想看真正的「派发后等待」，换成乱序模型 `generic-ooo` 和一段有长依赖的代码：

```bash
cat > stall.s <<'ASM'
ld a0, 0(a1)
ld a2, 8(a1)
div a3, a0, a2
mul a4, a3, a0
add a5, a4, a2
ASM
"$MCA" -mtriple=riscv64 -mcpu=generic-ooo -mattr=+m,+64bit -iterations=1 -timeline stall.s
```

```text
[0,0]     DeeeeER   .    .    .    .    . .   ld	a0, 0(a1)
[0,1]     DeeeeER   .    .    .    .    . .   ld	a2, 8(a1)
[0,2]     D====eeeeeeeeeeeeeeeeeeeeeER  . .   div	a3, a0, a2
[0,3]     D=========================eeeeER.   mul	a4, a3, a0
[0,4]     D=============================eER   add	a5, a4, a2
```

这张图一眼就能看出瓶颈：

- `div` 第 0 拍派发，`====` 等 4 拍（等两个 `ld` 的结果），然后执行 19 拍。
- `mul` 等 25 拍（等 `div`），`add` 等 29 拍。
- 两条 `ld` 的 `e` 只有 4 拍，`R` 在最后——退休是严格按序的，所以后面的长指令会拖住整条队列。

Timeline 是 mca 最有教学价值的部分：它把「调度器为什么这么排」画成了时间轴。改 sched model 或改调度方向后，看 timeline 的变化比看总周期更直观。

## Bottleneck Analysis：谁在拖后腿

`-bottleneck-analysis` 对乱序模型才有完整支持。对 `sifive-u74`（顺序核）实测会先警告：

```text
warning: bottleneck analysis is not supported for in-order CPU 'sifive-u74'.
```

但仍然会打印资源/依赖占比。换成 `generic-ooo`（`-mattr=+m,+64bit`）：

```text
Cycles with backend pressure increase [ 6.90% ]
Throughput Bottlenecks:
  Resource Pressure       [ 6.90% ]
  - GenericOOOLSU  [ 1.13% ]
  - GenericOOOMulDiv  [ 6.90% ]
  Data Dependencies:      [ 0.14% ]
  - Register Dependencies [ 0.14% ]
  - Memory Dependencies   [ 0.00% ]

Critical sequence based on the simulation:

              Instruction                                 Dependency Information
        0.    add	a0, a1, a2
 +----< 1.    mul	a0, a1, a2
 +----> 2.    div	a0, a1, a2                        ## RESOURCE interference:  GenericOOOMulDiv [ probability: 63% ]
        3.    ld	a0, 0(a1)
        4.    sd	a0, 0(a1)
```

这段输出的读法：

- `Resource Pressure 6.90%`：6.90% 的周期因为后端资源压力而无法提升吞吐。
- `GenericOOOMulDiv` 是主要贡献者——`add`、`mul`、`div` 都可能走这个资源组。
- `Critical sequence` 给出最长的依赖链，并标注资源冲突的概率（63%）。

注意：`add`、`mul`、`div` 都写 `a0`，所以它们之间还有寄存器依赖（WAW），critical sequence 把它们串起来了。这和你写的汇编有关，不是模型乱编。

## 底层机制：mca 怎么把模型变成时间

mca 的核心循环（概念上）：

```text
for cycle in 0..:
    1. 按序 dispatch 最多 IssueWidth 个 uOp（受 RAT/队列限制）
    2. 对每个已 dispatch 的 uOp：
         - 检查操作数是否 ready（依赖边的 latency）
         - 检查所需 ProcResource 是否空闲（ReleaseAtCycles）
         - 都满足则 issue 到执行单元
    3. 执行满 Latency 的 uOp 标记 executed
    4. 按序 retire
```

它用的是 `MCSchedModel` 生成的表（Ch 12 说的三张表），所以 mca 的行为和 `llc` 的 MachineScheduler 用的是同一份数据——这也是为什么改了 `.td` 之后，`llc` 的调度和 mca 的周期会一起变。

但两者的目标不同：

- `llc` 的 scheduler 要**产出一个顺序**（受寄存器压力、编译时间约束）。
- `mca` 是**评估一个已给定的顺序**（它不重排输入，只模拟）。

这解释了一个常见困惑：为什么 `llc` 排出来的顺序，mca 算出来不是最优？因为 `llc` 的启发式不是全局最优，而且它还要考虑寄存器压力。想找更优顺序，可以用 mca 做搜索，但那是另一个工具链（比如 `llvm-mca` 的 iterated search 或外部 autotuner）。

## 踩雷集锦

1. **不加 `-mtriple`**。mca 默认按宿主机（x86_64）解析，`-mcpu=rocket-rv64` 会被忽略，报 `'rocket-rv64' is not a recognized processor`，然后 `unable to find instruction-level scheduling information`。本机第一次跑就踩了这个。

2. **忘了 `-mattr=+m`**。含 `mul/div` 的汇编会解析失败：`instruction requires ... 'Zmmul'`。mca 不会替你补 feature，`-mcpu` 也不一定带 `M`（`rocket-rv64` 就不带）。

3. **用 `-mcpu=generic-ooo` 跑 RV64 汇编**。它是 tune-only，不带 `Feature64Bit`，`ld/sd` 会被当成 Zilsd 指令报错。加 `-mattr=+64bit`，或者改用 `-mcpu=generic-rv64`。

4. **把 `Total Cycles` 当硬件周期**。mca 假设 cache 全命中、分支全预测对、前端无限宽。真机上 `Total Cycles` 可能差一个数量级。写结论时必须写「模型周期，`-mcpu=sifive-u74`」。

5. **用 mca 比较两个不同 `-mcpu` 的 `llc` 输出**。这样同时改了「代码顺序」和「评估模型」两个变量。正确做法：固定 mca 的 `-mcpu`，只比较两份汇编；或者固定汇编，只换 mca 的 `-mcpu`。

6. **忽略 `ret` 的警告**。`warning: found a return instruction in the input assembly sequence. note: program counter updates are ignored.` 这不是错误，但意味着 `ret` 后面的控制流不被模拟。把 `ret` 留在输入里会让最后几拍多算一点收尾。

7. **以为 `-iterations` 越大越准**。`Total Cycles` 会随迭代线性增长，但 `Block RThroughput` 不变；依赖链的启动开销被摊薄后，`IPC` 会趋近稳态。比较两个版本时，两边必须用同一个 `-iterations`。

## 动手练习

### 练习 1：预测再验证

先用 Ch 1 的方法生成 `min_on.s`（misched 开）和 `min_off.s`（`-enable-misched=false -pre-RA-sched=source`），然后用 mca 预测：

```bash
"$MCA" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m -iterations=100 min_on.s
"$MCA" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m -iterations=100 min_off.s
```

本机实测是 1001 对 1301。解释这 300 拍从哪来（提示：`LD` 和 `MUL` 的交错方式，以及 `ld` 的 2 拍延迟）。

### 练习 2：改 iterations 看哪个字段变

对 `s.s`、`sifive-u74`，分别跑 `-iterations=1`、`10`、`100`、`1000`，记录 `Total Cycles`、`IPC`、`Block RThroughput`。哪些是线性变化的，哪些是常数？为什么？

### 练习 3：用 timeline 找 stall

对 `stall.s` 用 `generic-ooo` 跑 `-iterations=1 -timeline`，数 `div`、`mul`、`add` 各自有几个 `=`。再把 `-mcpu` 换成 `xiangshan-nanhu`（用同一份汇编），看等待变长还是变短，并回到 `.td` 找出原因（提示：XiangShan 的 `div=20`、`mul=3`，而且有 load-to-ALU bypass）。

### 练习 4：制造一个资源瓶颈

把 `ops.s` 改成连续 8 条 `div`，用 `sifive-u74` 跑 `-all-views`。观察 `Resource pressure per iteration` 里 `VLEN512SiFive7IDiv` 的值，以及 `Total Cycles`。验证：`div` 的 RThroughput 是 65.00，8 条应该是多少拍？

## 重点整理

- mca 是**模型模拟器**：输入是汇编 + `-mcpu`/`-mattr`，输出是「按这份模型，每拍发生了什么」。
- 报告头七个字段里，`Dispatch Width` 来自 `IssueWidth`，`Block RThroughput` 来自资源压力，`Total Cycles` 是模拟结果。
- `Instruction Info` 直接暴露 `WriteRes` 的 latency/RThroughput；`Resources` 暴露 `ProcResource` 结构。
- `Dynamic Dispatch Stalls` 的 `RAT` 高，说明寄存器依赖受限；`Resource pressure` 高，说明执行单元受限。
- Timeline 的 `D/=/e/E/-/R` 分别对应 dispatch、等待、执行、执行完、等退休、退休。
- Bottleneck analysis 对顺序核只给警告；对乱序核会给资源/依赖占比和 critical sequence。
- mca 不模拟 cache、TLB、分支预测、频率；所有周期数都必须带模型名。

## 自我检核

- [ ] 我能解释 `Block RThroughput` 与 `Total Cycles` 为什么不同。
- [ ] 我能从 `Resource pressure per iteration` 判断瓶颈在资源还是依赖。
- [ ] 我知道 `RAT` stall 的含义，并能举一个 RISC-V 的例子。
- [ ] 我能读懂 timeline 里的 `DeeE` 和空格。
- [ ] 我知道为什么 `-mcpu=generic-ooo` 需要额外 `+64bit`。
- [ ] 我能说出 mca 至少三项不模拟的东西。
- [ ] 我知道 `-bottleneck-analysis` 在顺序核上会发生什么。

## 延伸阅读

- **`llvm/tools/llvm-mca/Views/TimelineView.h`（`DisplayChar`，约 156–163 行）**
  - **读哪里**：六个字符常量的定义。
  - **和本章的关联**：timeline 图例的权威来源，比任何博客都准。

- **`llvm/tools/llvm-mca/Views/BottleneckAnalysis.cpp`（全文）**
  - **读哪里**：资源干扰概率的计算、critical sequence 的构造。
  - **和本章的关联**：解释 `[ probability: 63% ]` 是怎么来的。

- **`llvm/tools/llvm-mca/README.txt`**
  - **读哪里**：报告字段说明与使用示例。
  - **和本章的关联**：mca 的官方使用手册；注意其中的例子多为 x86。

- **`llvm/lib/MC/MCSchedule.cpp`（`computeInstrLatency` 等）**
  - **读哪里**：如何从 `MCSchedClassDesc` 查 write latency。
  - **和本章的关联**：mca 的延迟计算路径，和 `llc` 共用同一套表。

- **`llvm/lib/CodeGen/TargetSchedule.cpp`（`computeOperandLatency`，约 160–235 行）**
  - **读哪里**：`Latency - Advance` 的实现与 `DefaultDefLatency` 的回退。
  - **和本章的关联**：解释 mca 与 llc 为什么会对同一条边给出相同数字。

- **LLVM 官方文档：*llvm-mca — Machine Code Analyzer***
  - **读哪里**：Quick Start 与 "Bottleneck Analysis" 两节。
  - **和本章的关联**：把本章的实测字段对回官方术语。

→ [下一章：调度策略与消融实验](./15-scheduling-policy.md)
