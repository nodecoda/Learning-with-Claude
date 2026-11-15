# Ch 27 — 性能归因：mca、perf、`-stats`、`-time-passes`

> **目标**：当一段代码「变慢了」或「汇编变了」时，能在十分钟内回答四个问题：变的是选择还是顺序？慢在依赖还是资源？是哪个 pass 动的？这个判断有多少证据、多少是模型的假设。学完你能把 `llc` 的自带工具和 `llvm-mca` 组合成一条归因链，并知道每一环的盲区。
>
> **环境**：LLVM 24.0.0git（assertions 构建）；`llc`、`llvm-mca`、`opt`；构建 `/home/ubuntu/llvm/builds/explore`。本机**没有** `perf`，这一事实本身会作为边界写进本章。所有数字、警告、YAML 均为本机实跑。

## 为什么归因比「看汇编」难

同一个函数从 IR 到 `.s`，至少有五层能改最终形态：

```text
IR ──► DAG combine/legalize ──► ISel ──► DAG scheduler ──► machine-combiner
   ──► pre-RA scheduler ──► register allocator ──► post-RA scheduler
   ──► pseudo expansion ──► asm printer
```

「这条 `add` 被提前了」可能是：

- DAG combine 把两个 `add` 重结合了；
- machine-combiner 改了依赖图；
- pre-RA scheduler 为了压寄存器压力重排；
- post-RA scheduler 为了填延迟槽重排；
- 甚至只是 asm printer 的指令压缩（`c.add`）改变了字节数。

如果没有归因方法，你只能看到「汇编不一样」，然后把锅甩给最近改的那个 pass。面试官问「你怎么知道是 scheduler 而不是 combiner」，就是在筛这个。

## 先建立直觉：归因漏斗

```text
① 现象层：最终 .s / 周期数真的变了吗？
        │  差分、固定 mcpu/mattr
        ▼
② 瓶颈层：变的是依赖链、执行资源，还是前端 dispatch？
        │  llvm-mca：timeline / resource pressure / bottleneck
        ▼
③ pass 层：哪一层 dump 第一次出现差异？
        │  -print-before/after、-debug-only、-stats
        ▼
④ 决策层：那个 pass 为什么这么选？
           sched model、policy、pattern、源码
```

**顺序不能反**。先看源码猜「应该是 scheduler」，再去找证据，很容易把任何顺序变化都解释成 scheduler。正确做法是先用差分定位第一次变化，再读那一层的代码。

工具与能回答的问题：

| 工具 | 能回答 | 不能回答 | 前提 |
|---|---|---|---|
| `diff` 两份 `.s` | 最终形态变了吗 | 谁改的 | 固定 `-mcpu`/`-mattr` |
| `llvm-mca` | 模型视角的周期、瓶颈类型 | 真实芯片行为 | 模型可信；同 CPU 比较 |
| `-print-before/after` | 哪个 pass 的输入输出不同 | 为什么这么决策 | pass 名正确 |
| `-debug-only=<pass>` | pass 内部策略、region 划分 | 无 assertions 时不可用 | assertions 构建 |
| `-stats` / `-save-stats` | 各 pass 的计数器 | 单条指令的因果 | assertions 构建 |
| `-time-passes` | 编译时间花在哪 | 运行时性能 | 相对比较，不跨机 |
| `-pass-remarks-output` | 每函数的指令 mix、栈大小 | 调度顺序 | 无 |
| `perf` | 硅上的真实周期/缺失 | 为什么编译器这么排 | 有硬件和权限 |

## 工具一：`-stats` 与 `-save-stats`

`llc -stats` 在 assertions 构建里汇总所有 `STATISTIC` 计数器：

```text
$ llc -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 -stats sel.ll -o /dev/null
===-------------------------------------------------------------------------===
                          ... Statistics Collected ...
===-------------------------------------------------------------------------===

  6 isel                    - Number of blocks selected using DAG
320 isel                    - Number of times dag isel has to try another path
 20 machine-scheduler       - Number of scheduling units chosen from bottom queue pre-RA
 30 machine-scheduler       - Number of instructions scheduled by pre-RA scheduler
  9 machine-scheduler       - Number of instructions scheduled by post-RA scheduler
 22 machine-scheduler       - Number of scheduling units chosen for Only1 heuristic pre-RA
  8 machine-scheduler       - Number of scheduling units chosen for PhysReg heuristic pre-RA
 10 machine-scheduler       - Number of scheduling units chosen from top queue pre-RA
 22 regalloc                - Number of registers assigned
 19 regalloc                - Number of identity moves eliminated after rewriting
```

几个字段的读法：

- `isel.NumDAGIselRetries = 320`：DAG selector 尝试 pattern 失败后回退的次数。**它高不代表慢**，但突然从 20 涨到 3000，通常意味着某个 pattern 变复杂了。
- `NumTopPreRA` / `NumBotPreRA`：top-down 队列和 bottom-up 队列各被选了多少次。两者都非零，说明调度器在双向工作，不是纯 top-down。
- `NumOnly1PreRA`：只在一个队列里有候选（另一个被依赖卡住）的次数；比例高说明依赖链很紧，调度的自由度低。
- `NumPhysRegPreRA`：因为物理寄存器依赖（live-in、call-clobber）被迫选的次数。
- `regalloc.NumAssigned` 与 `NumIdCopies`：前者是分配的寄存器数，后者是 coalescing 后消除的 identity copy 数；两者一起看能判断 RA 是否在「搬来搬去」。

要把统计送进 CI，用 `-save-stats`：

```bash
$ llc ... -save-stats=obj sel.ll -o sel.s
$ ls
sel.s  sel.stats
```

本机 `sel.stats` 是 JSON：

```json
{
	"asm-printer.EmittedInsts": 19,
	"isel.NumDAGIselRetries": 320,
	"machine-scheduler.NumTopPreRA": 10,
	"machine-scheduler.NumBotPreRA": 20,
	"regalloc.NumAssigned": 22,
	...
}
```

`-save-stats=cwd` 写到当前目录，`=obj` 写到输出文件旁边。它的价值是**趋势**：同一个 benchmark 每次提交的计数器变化，比单次绝对值有用。

## 工具二：`-time-passes`

```text
$ llc ... -time-passes sel.ll -o /dev/null
===-------------------------------------------------------------------------===
                          Pass execution timing report
===-------------------------------------------------------------------------===
  Total Execution Time: 0.0063 seconds (0.0063 wall clock)

   ---User Time---   --System Time--   --User+System--   ---Wall Time---  --- Name ---
   0.0008 ( 21.2%)   0.0004 ( 16.5%)   0.0012 ( 19.3%)   0.0012 ( 19.3%)  RISC-V DAG->DAG Pattern Instruction Selection
   0.0005 ( 13.0%)   0.0000 (  0.0%)   0.0005 (  7.7%)   0.0005 (  7.7%)  Basic Alias Analysis (stateless AA impl)
   0.0001 (  2.3%)   0.0002 (  9.5%)   0.0003 (  5.3%)   0.0003 (  5.3%)  Machine Instruction Scheduler
```

正确用法：

- 只看**占比**，不看绝对值；assertions + RelWithDebInfo 构建的时间没有生产意义。
- 比较同一台机器、同一构建、同一输入的两次运行。
- 它测的是 pass 自身，不含 `llc` 启动、IO、以及被 inline 掉的 analysis。

错误用法：拿 `-time-passes` 的 6ms 去论证「这个 pass 很慢」。6ms 里大部分是进程启动和文件系统。

## 工具三：`-pass-remarks-output`

`llc` 不认 clang 的 `-Rpass`（本机实测 `Unknown command line argument '-Rpass=loop-unroll'`），但 codegen pass 会通过 remark 机制输出结构化信息：

```bash
$ llc ... -pass-remarks-output=rem.yaml sel.ll -o /dev/null
$ head -20 rem.yaml
--- !Analysis
Pass:            prolog-epilog
Name:            StackSize
Function:        f
Args:
  - NumStackBytes:   '0'
  - String:          ' stack bytes in function '''
...
--- !Analysis
Pass:            asm-printer
Name:            InstructionCount
Function:        f
Args:
  - NumInstructions: '12'
  - String:          ' instructions in function'
```

本机的 `rem.yaml` 还包含：

- `asm-printer / InstructionMix`：**按基本块**列出每条指令的数量（`INST_ld: '2'`、`INST_mul: '2'`、`INST_add: '2'`）。
- `target-features / EnabledFeatures`：实际生效的 feature 列表（`64bit`、`m`、`c`、`zmmul`、`sifive7`、`use-postra-scheduler`……）。

这比 `-stats` 更适合做「这个函数的指令组合变了没有」的回归：它是 per-function、per-BB 的，而且不需要断言构建。

一个实用技巧：把 `InstructionMix` 和 mca 的 `Instructions:` 表对照。如果 mix 里 `mul` 从 3 条变成 2 条，而 mca 的周期没降，说明瓶颈不在乘法吞吐，而在依赖链或 load。

## 工具四：`-debug-only=<pass>`

assertions 构建里，每个 pass 的 `LLVM_DEBUG` 都能单独打开：

```bash
$ llc ... -debug-only=machine-scheduler sel.ll -o /dev/null 2>&1 | head
Current Schedule Region
 RegionInstrs: 5
ScheduleDAGMILive::schedule starting
GenericScheduler RegionPolicy:  ShouldTrackPressure=1 OnlyTopDown=0 OnlyBottomUp=0
** ScheduleDAGMILive::schedule picking next node
```

读法：

- `RegionInstrs: 5`：这个 scheduling region 有 5 条指令；region 太大/太小会影响调度质量（第 11 章）。
- `ShouldTrackPressure=1`：调度器在压寄存器压力；如果这时 spill 反而变多，说明 pressure 模型和 RA 的实际行为不一致（第 16 章）。
- `OnlyTopDown=0 OnlyBottomUp=0`：双向调度，不是单一方向。

限制：release 构建里 `LLVM_DEBUG` 被编译掉，`-debug-only` 静默无输出。**看到空输出先确认构建类型**，不要以为 pass 没跑。

## 工具五：`-print-before/after`：定位「第一次变化」

`one.ll` 的 `@f` 做三次 load、三次 mul、三次 add。直接看最终汇编，add 的树形是：

```text
%11 = m0 + m1
%16 = m2 + d
%13 = %11 + %16
```

但这个形状**不是 scheduler 排的**。用 machine-combiner 的前后 dump：

```text
# *** IR Dump Before Machine InstCombiner (machine-combiner) ***:
  %11:gpr = ADD %8:gpr, %9:gpr
  %12:gpr = ADD %11:gpr, %10:gpr
  %13:gpr = ADD %12:gpr, %4:gpr
# *** IR Dump After Machine InstCombiner (machine-combiner) ***:
  %11:gpr = ADD %8:gpr, %9:gpr
  %16:gpr = ADD %10:gpr, %4:gpr
  %13:gpr = ADD %11:gpr, %16:gpr
```

第一次出现 `%16` 的地方是 `machine-combiner`，不是 scheduler。这就是归因：**差分要沿管线做，不能只在终点看**。

同样的方法可以定位任何一层：

```bash
llc ... -print-before=machine-scheduler -print-after=machine-scheduler foo.ll -o /dev/null
llc ... -print-before=finalize-isel -print-after=finalize-isel foo.ll -o /dev/null
```

注意 `-print-before` 打印的是「这个 pass 的输入」，而输入可能已经被更早的 pass 改过。看到差异时，先把 `-print-before` 往前挪一层，直到差异消失。

## 工具六：`llvm-mca`：把顺序翻译成周期

用一段循环体（两次 load、两次 mul、一次 add、一次 store）：

```asm
# LLVM-MCA-BEGIN loop
ld   a1, 0(a0)
ld   a2, 8(a0)
mul  a3, a1, a2
mul  a4, a3, a1
add  a5, a4, a2
sd   a5, 0(a0)
addi a0, a0, 16
bne  a0, a6, loop
# LLVM-MCA-END
```

```bash
$ llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -iterations=100 mca.s
```

本机报告（节选）：

```text
Iterations:        100
Instructions:      800
Total Cycles:      1302
Total uOps:        800
Dispatch Width:    2
uOps Per Cycle:    0.61
IPC:               0.61
Block RThroughput: 4.0
```

以及瓶颈分析：

```text
warning: bottleneck analysis is not supported for in-order CPU 'sifive-u74'.

Cycles with backend pressure increase [ 84.33% ]
Throughput Bottlenecks:
  Resource Pressure       [ 7.68% ]
  Data Dependencies:      [ 76.65% ]
  - Register Dependencies [ 76.65% ]
  - Memory Dependencies   [ 0.00% ]
```

三条结论，按可信度排序：

1. **资源压力只有 7.68%，寄存器依赖 76.65%**：这段代码不是吞吐受限，是依赖链受限。这个结论在 in-order 模型上也成立，因为依赖分析不依赖 OoO 假设。
2. `Block RThroughput: 4.0`：每迭代 8 条指令、dispatch width 2，理论下限 4 周期；实际 13.02 周期/迭代，差 3 倍，进一步确认是延迟没被填满。
3. **bottleneck analysis 的 warning 必须读**：in-order CPU 上它仍打印百分比，但那是依赖/资源的分解，不是 OoO 的 issue-width 分析。别把百分比当成前端瓶颈的证明。

`-timeline` 能把「哪条指令在等谁」画出来：

```text
[0,0]     DeeE .    .    .    .    . .   ld	a1, 0(a0)
[0,2]     .   DeeE  .    .    .    . .   mul	a3, a1, a2
[0,3]     .    . DeeE    .    .    . .   mul	a4, a3, a1
[0,4]     .    .  DeeE   .    .    . .   add	a5, a4, a2
```

每个 `D`/`e`/`E` 是 dispatch/execute/retire 的占位；竖着看空列，就是流水线在等。`-resource-pressure` 则给出每迭代各 pipe 的占用：

```text
Resource pressure per iteration:
[2] SiFive7PipeA  4.00
[3] SiFive7PipeB  4.00
```

PipeA/PipeB 各 4.00，说明 ALU 不是瓶颈；真正的瓶颈是 `mul` 的 3 周期延迟串在依赖链上。

## 工具七：硬件 `perf`，以及没有它的时候

本机：

```text
$ perf stat -e cycles true
/bin/bash: line 1: perf: command not found
```

没有 `perf` 时，必须把结论降级：

- mca 能证明「在这个模型下，依赖链需要 13 周期/迭代」。
- mca **不能**证明「芯片上也是 13 周期」；模型可能错，cache 行为不在模型里，频率/乱序也不在。
- 真正的裁决是目标板上的 `perf stat -e cycles,instructions,cache-misses`，或者厂商的 performance counter。

面试里可以这样表述边界：「我先用 mca 定位到依赖链，再用 perf 在板子上验证 IPC；如果两者矛盾，先怀疑模型，而不是怀疑硬件。」

## 归因案例 A：选择变了，周期也变了

同一个 `shl 3 + add`，开 `+zba` 选 `sh3add`，不开选 `slli+add`。用 mca 单独比较两条序列（`sifive-u74`）：

```text
[0] Code Region - sh3add
Instructions:      1
Total Cycles:      4
Block RThroughput: 1.0

[1] Code Region - slli-add
Instructions:      2
Total Cycles:      5
Block RThroughput: 1.0
```

`Total Cycles` 差 1，`Block RThroughput` 相同（都是 1.0）。这说明：

- 收益来自**少一条指令、少一级依赖**，不是吞吐翻倍。
- 如果这段在一个已经吞吐受限的循环里，`sh3add` 可能一点用都没有；先看瓶颈类型，再决定要不要为它加 pattern。

这就是第 18 章「selection 的自由度」和本章的连接：**选择改变的是图，调度改变的是序，mca 把两者都翻译成周期，但只有瓶颈分析能告诉你该改哪一个。**

## 归因案例 B：中间顺序变了，最终汇编没变

用第 26 章的 `one_test2.mir`，pre-RA 方向换成 bottom-up，MIR 里 `COPY $x12` 被插到两条 `LD` 之间。但把 `one.ll` 分别用 topdown/bottomup 编译成最终汇编：

```bash
$ diff big_topdown.s big_bottomup.s
$ echo $?
0
```

本机上这两个函数的最终汇编**逐字节相同**（`big.ll` 也是 0 行差异）。原因：寄存器分配和 post-RA 调度把 pre-RA 的顺序差异吸收掉了。

教训：

- **不能用中间 MIR 的顺序变化去论证性能变化**。中间态是调度器的草稿，不是最终代码。
- 反过来，如果你要测调度器本身，就必须在 MIR 层测（第 26 章）；要测性能，必须在最终 `.s` 或硬件上测。
- 面试时如果被问「你怎么证明这个调度改动有效」，正确回答是「mca + 目标板」，不是「MIR 顺序变了」。

## 踩雷清单

1. **mca 的 `-mcpu` 必须和 `llc` 一致**。用 `llc -mcpu=sifive-u74` 生成、`llvm-mca -mcpu=rocket-rv64` 分析，数字没有意义；`-mtriple=riscv64` 也必须写，否则 mca 可能按 host 解析。
2. **`-stats` 只在 assertions 构建存在**。release 版 `llc` 里这个选项可能直接不存在；先 `llc --version` 看是不是 assertions。
3. **`-time-passes` 是编译时间，不是运行时**。它能告诉你「哪个 pass 编译慢」，不能告诉你「哪个 pass 生成的代码慢」。
4. **in-order CPU 的 bottleneck analysis 不受支持**。本机 `sifive-u74` 会打 warning；百分比仍然打印，但语义是依赖/资源分解，不是 OoO 分析。
5. **`-debug-only` 在 release 构建静默**。没有输出不等于 pass 没跑；先确认构建类型。
6. **`-print-before` 打印的是 pass 的输入**，不是「上一层的输出」。差异定位要一层层往前挪。
7. **mca 的 `Total Cycles` 随 `-iterations` 变**。比较时必须固定 iterations；默认 100 会把启动开销摊薄，`-iterations=1` 更适合看单次延迟。

## 动手练习

1. 用 `sel.ll` 跑 `-stats`，记录 `NumTopPreRA`/`NumBotPreRA`/`NumOnly1PreRA`；然后把 `-mcpu` 换成 `rocket-rv64`，解释哪些计数器变了、为什么。
2. 用 `-print-before=machine-combiner -print-after=machine-combiner` 找出 `one.ll` 里 `%16` 的诞生点；再往前一层（`-print-before=peephole-opt`）确认不是它做的。
3. 用 `llvm-mca -timeline -iterations=2` 分析本章的循环体，指出哪一列是 `mul` 的等待；把第二条 `mul` 改成 `add`，看 `Total Cycles` 降多少。
4. 写一个脚本：对同一份 `.ll` 用 `-mcpu=rocket-rv64` 和 `-mcpu=sifive-u74` 各生成 `.s`，再用对应 CPU 的 mca 算周期，输出一张对比表。思考：这个表能证明什么、不能证明什么。
5. **guided**：在目标板（或 QEMU + `perf` 可用的环境）上跑 `perf stat`，把硬件周期和 mca 的 `Total Cycles` 对照；记录差异来源（cache、频率、模型误差）。

## 重点整理

| 症状 | 第一步 | 命令 | 判读 |
|---|---|---|---|
| 汇编变了 | 定位第一次变化 | `-print-before/after=<pass>` | 差异出现在哪一层 |
| 周期变多 | 看瓶颈类型 | `llvm-mca -bottleneck-analysis` | 依赖 vs 资源 vs dispatch |
| 不确定哪个 pass | 看计数器 | `-stats` / `-save-stats=obj` | 计数器突变 |
| 想知道指令 mix | 看 remark | `-pass-remarks-output` | 每 BB 的 opcode 计数 |
| 调度器怎么想的 | 看 debug | `-debug-only=machine-scheduler` | region 大小、pressure 策略 |
| 编译变慢 | 看 pass 时间 | `-time-passes` | 占比，不跨机比较 |
| 模型对不对 | 上硬件 | `perf stat` | 与 mca 对照 |

- 归因顺序：现象 → 瓶颈 → pass → 决策；不能从源码猜。
- mca 是模型，perf 是硅；两者矛盾时先怀疑模型。
- 中间 MIR 的顺序变化不能当性能证据；最终 `.s` 和硬件才是。
- `-stats`/`-debug-only` 依赖 assertions 构建；release 下静默。
- `-pass-remarks-output` 是 codegen 层最实用的 per-function 结构化输出。

## 自我检核

- [ ] 我能说出 `-stats`、`-time-passes`、`-pass-remarks-output` 三者各自回答什么问题。
- [ ] 我能用 `-print-before/after` 证明某个顺序变化是 machine-combiner 而不是 scheduler 造成的。
- [ ] 我能读懂 mca 的 `Total Cycles`、`Block RThroughput`、`IPC` 和资源压力表。
- [ ] 我知道 in-order CPU 上 bottleneck analysis 的限制，并会读那条 warning。
- [ ] 我能解释为什么「MIR 顺序变了但最终汇编没变」，以及这对性能结论意味着什么。
- [ ] 我能说出没有 `perf` 时，mca 结论的适用范围和降级方式。

## 延伸阅读

- `llvm/docs/CommandGuide/llvm-mca.html`：`-timeline`、`-resource-pressure`、`-bottleneck-analysis` 每个字段的定义；本章只用了其中一部分。
- `llvm/lib/CodeGen/MachineScheduler.cpp`：`NumTopPreRA`/`NumBotPreRA`/`NumOnly1PreRA` 这些计数器的自增点；读它能知道计数器对应的决策。
- `llvm/lib/CodeGen/MachineCombiner.cpp`：本章 `%16` 重结合的实现；看 `reassociate` 的合法性条件。
- `llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp`：`InstructionMix`/`InstructionCount` remark 的发出位置；理解 remark 的粒度。
- `llvm/test/CodeGen/RISCV/sched-model-mispredict-penalty.ll`：上游如何用测试锁定「分支代价」这类模型行为，可与本章的归因方法对照。

→ [下一章：上游 PR 与 review](./28-upstream-pr-review.md)
