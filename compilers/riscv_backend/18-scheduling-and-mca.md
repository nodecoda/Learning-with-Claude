# 18 调度模型与 llvm-mca：把 .td 里的数字变成可读的周期

> 目标：读懂 `RISCVSched*.td` 里的机器模型，并用 `llvm-mca` 把一段汇编变成 IPC、uOps、资源压力和依赖链。读完本章，你能判断一条指令为什么被排到某个周期、为什么 `addi` 的 Latency 写着 3 而依赖链每拍就能推进一条、以及什么时候不该相信 mca 的数字。

> 环境：LLVM 23.1.4 预编译。`llvm-mca` 已在本机，命令统一通过 `compilers/riscv_backend/code/tools/run.ps1` 执行。本章所有周期数都是 `-mtriple=riscv64 -mcpu=sifive-u74` 的默认 `-iterations=100` 结果，除非另注。

## 为什么

后端里最难解释的一类 bug 是"代码看起来更少了，跑起来更慢了"。指令选择只决定用哪条指令，寄存器分配只决定用哪个寄存器，真正决定顺序的是调度器；而调度器的全部依据是 `.td` 里的机器模型。模型写错一个 `Latency`，整段循环的软件流水就可能反了。

麻烦在于这个模型平时不可见。你改 `RISCVSchedSiFive7.td` 的一个数字，`llc` 不会告诉你任何事，只会输出顺序不同的汇编。`llvm-mca` 的作用是把模型摊开：给定一段汇编和一个 `-mcpu`，它按模型模拟流水线，打印每条指令的 uOps、Latency、RThroughput、资源占用，以及最终的 IPC 和总周期。

但它是**模型模拟器**，不是芯片模拟器。它不知道缓存、分支预测、内存别名，也不知道你手上这颗核的实际旁路网络。本章因此有两个同等重要的目标：学会读它的输出，以及学会识别它的边界。只学前者，你会把 401 周期当成实测性能；只学后者，你会放弃唯一一个不需要硬件的量化工具。

## 主线

### 18.1 三层词汇：SchedWrite / SchedRead、指令上的 Sched、处理器模型

RISC-V 的调度描述分成三层，缺一层都读不懂输出。

第一层是**抽象的读写类型**，定义在 `RISCVSchedule.td`：

```tablegen
def WriteIALU       : SchedWrite;    // 32 or 64-bit integer ALU operations
def WriteShiftImm   : SchedWrite;
def WriteIDiv       : SchedWrite;
def WriteIMul       : SchedWrite;
def WriteLDD        : SchedWrite;
def WriteSTD        : SchedWrite;
```

这些名字不带任何周期数，只是给"一次整数 ALU 写"、"一次 64 位 load"这类事件起名。

第二层是**指令到事件的绑定**。`RISCVInstrInfo.td` 里 `ADD` 的定义就是这样：

```tablegen
def ADD  : ALU_rr<0b0000000, 0b000, "add", Commutable=1>,
           Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

读法是"这条指令产生一个 `WriteIALU` 事件，读两个 `ReadIALU` 事件"。它仍然不含周期数，只说明这条指令的形状。

第三层是**处理器模型把事件映射到资源和周期**。`RISCVSchedSiFive7.td` 里：

```tablegen
// Integer arithmetic and logic
let Latency = 3 in {
  def : WriteRes<WriteIALU, [PipeAB]>;
  def : WriteRes<WriteIALU32, [PipeAB]>;
  ...
}

// Integer multiplication
let Latency = 3 in {
  def : WriteRes<WriteIMul, [PipeB]>;
  def : WriteRes<WriteIMul32, [PipeB]>;
}
```

到这里才出现三个关键量：`Latency = 3`、资源列表 `[PipeAB]`（`PipeA` 与 `PipeB` 的组）或 `[PipeB]`、以及每条指令占用的 uOps。同一条 `ADD` 在 Rocket 模型和 SiFive7 模型下的周期数可以完全不同，因为第三层不同。

`SiFive7SchedMachineModel` 还定义了一组整机参数，它们直接决定 mca 的行为：

```tablegen
class SiFive7SchedMachineModel<int vlen> : SchedMachineModel {
  let MicroOpBufferSize = 0; // Explicitly set to zero since SiFive7 is in-order.
  let IssueWidth = 2;        // 2 micro-ops are dispatched per cycle.
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  ...
}
```

`MicroOpBufferSize = 0` 是"顺序核"的标记，也是后面 18.5 那条 warning 的来源。`IssueWidth = 2` 就是报告里的 `Dispatch Width: 2`。`LoadLatency = 3` 与后面 `ld` 的 Latency 3 对应。

对照 `RISCVSchedGenericOOO.td` 的 `GenericOOOModel`：`IssueWidth = 6`、`MicroOpBufferSize = 192`、`LoadLatency = 4`。同一段汇编换 `-mcpu`，这些参数一换，所有数字都会变。这就是为什么 mca 报告的第一行永远是 region 名，第二行必须自己确认 `-mcpu`。

### 18.2 读一份完整报告：mul_add 的 401 周期

最小样本是 `mul` 加 `addi`：

```asm
# LLVM-MCA-BEGIN mul_add
mul a0, a0, a1
addi a0, a0, 7
# LLVM-MCA-END
```

`llvm-mca -mtriple=riscv64 -mcpu=sifive-u74` 的实测输出：

```text
[0] Code Region - mul_add

Iterations:        100
Instructions:      200
Total Cycles:      401
Total uOps:        200

Dispatch Width:    2
uOps Per Cycle:    0.50
IPC:               0.50
Block RThroughput: 1.0
```

六个数字逐个解释：

- `Iterations: 100` 是默认重复次数。mca 把这段代码当作"重复执行的块"，不是循环。
- `Instructions: 200` 是 100 次 × 2 条。
- `Total Cycles: 401` 是模拟出的总周期。200 条指令用 401 拍，正好对应 IPC 0.5。
- `Total uOps: 200` 说明每条汇编指令都是 1 个 uOp；RISC-V 的整数指令基本如此，向量指令会出现 >1。
- `Dispatch Width: 2` 来自模型 `IssueWidth = 2`。
- `Block RThroughput: 1.0` 是"每轮迭代的下界"，即 1 拍/迭代。它由资源压力和依赖共同决定，是判断循环体理论下限最快的数。

再看指令明细：

```text
[1]    [2]    [3]    [4]    [5]    [6]    Instructions:
 1      3     1.00                        mul	a0, a0, a1
 1      3     0.50                        addi	a0, a0, 7
```

`mul` 的 RThroughput 是 1.00，`addi` 是 0.50。含义是：`mul` 独占 `PipeB`，每 1 拍才能发一条；`addi` 可以走 `PipeA` 或 `PipeB`，两选一，所以平均 0.5 拍一条。资源压力表印证了这一点：

```text
Resource pressure per iteration:
[0]    [1]    [2]    [3]    [4]    [5]    [6]    [7]
 -      -     1.00   1.00    -      -      -      -
```

`[2]` 是 `VLEN512SiFive7PipeA`，`[3]` 是 `VLEN512SiFive7PipeB`，各 1.00。也就是说这段两指令的块每轮把两条流水线各占满一拍，`Block RThroughput = 1.0` 与资源表自洽。

资源名前面的 `VLEN512` 不是笔误：SiFive7 的模型按向量长度参数化，`sifive-u74` 绑定的是 `SiFive7VLEN512Model`。同一份模型在 `-mattr=+v` 下还会激活 `SiFive7VA1`、`VCQ`、`VL`、`VS` 等向量资源，名字会一起出现在报告里。

### 18.3 latency=3 的 addi：模型是近似，不是测量

报告里 `addi` 的 Latency 是 3。很多人读到这里会得出"两条依赖的 addi 之间要隔 3 拍"。实测不是这样。四个首尾相接的 `add`：

```asm
# LLVM-MCA-BEGIN dep_add
add a0, a0, a1
add a0, a0, a1
add a0, a0, a1
add a0, a0, a1
# LLVM-MCA-END
```

默认 100 次迭代得到 `Total Cycles: 403`，即 **4 拍/迭代，每条依赖 add 只占 1 拍**。而四条依赖的 `mul` 是 `Total Cycles: 1201`，即 **12 拍/迭代，每条 3 拍**。同样的 Latency 字段，行为完全不同。

原因在模型里另有一层：`RISCVSchedSiFive7.td` 定义了旁路规则

```tablegen
class SiFive7AnyToGPRBypass<SchedRead read, int cycles = 2>
    : ReadAdvance<read, cycles, [WriteIALU, WriteIALU32, ...
                                 WriteIMul, WriteIMul32, ...
                                 WriteLDD]>;
```

并在 1263 行附近实例化了 `SiFive7AnyToGPRBypass<ReadIALU>` 等。`ReadAdvance` 的含义是"消费者可以提前若干拍读到结果"。所以：

- 报告里的 `Latency` 是 `WriteRes` 的延迟，描述的是"写回发生在第几拍"。
- 实际相邻指令的间隔还要经过 `ReadAdvance` 修正。
- 因此 Latency 3 不代表依赖链每步 3 拍；`addi` 的实测间隔是 1 拍。

这不是 bug，而是模型表达能力的体现：一个 3 级流水、带旁路的 ALU，吞吐是 1 拍/条，写回在第 3 拍，消费者可以提前拿到。把这三件事压成一个"Latency"数字本来就会失真，mca 用 `Latency + ReadAdvance` 两个字段分别表达。

单次迭代的时间线更直观（`-iterations=1 -timeline`）：

```text
Timeline view:
                    012
Index     0123456789

[0,0]     DeeE .    . .   mul	a0, a0, a1
[0,1]     .  DeeE   . .   mul	a0, a0, a1
[0,2]     .    .DeeE. .   mul	a0, a0, a1
[0,3]     .    .   DeeE   mul	a0, a0, a1
```

四个 `D` 相隔 3 拍，`E`（写回）在每个 `D` 之后第 3 拍。这正是 13 拍 = 1 + 3×4 的来源（首条 1 拍进入，之后每条等 3 拍）。

### 18.4 依赖链、资源压力：两种瓶颈怎么区分

把同一组四条指令改成互不依赖：

```asm
add a0, a0, a1
add a2, a2, a3
add a4, a4, a5
add a6, a6, a7
```

实测 `Total Cycles: 203`，即 2 拍/迭代；`IPC 1.97`。四条指令、发射宽度 2，下界就是 2 拍——这是**资源/发射瓶颈**。对照 18.3 的依赖版（403 拍，IPC 0.99），两种瓶颈的判别方法就很清楚：

| 序列 | Total Cycles | IPC | 每迭代 | 瓶颈 |
|---|---|---|---|---|
| 4 条依赖 add | 403 | 0.99 | 4 拍 | 依赖链（每拍一条） |
| 4 条独立 add | 203 | 1.97 | 2 拍 | 发射宽度（2 条/拍） |
| 4 条依赖 mul | 1201 | 0.33 | 12 拍 | 依赖链（每条 3 拍） |
| ld / add / sd | 401 | 0.75 | 4 拍 | 访存 + 依赖 |

第三行是"同一条链、不同延迟"的对照：`mul` 的 RThroughput 是 1.00 且只走 `PipeB`，依赖间隔 3 拍，所以 4 条需要 12 拍。第二行则说明：当依赖被打破后，性能由 `IssueWidth` 而非 Latency 决定。

再看 `ld a1, 0(a0)` / `add a1, a1, a2` / `sd a1, 0(a3)` 这一组。实测 `Total Cycles: 401`、`IPC 0.75`，指令明细里 `ld` 的 Latency 3、RThroughput 1.00、`MayLoad` 为 `*`；`sd` 的 Latency 1、RThroughput 1.00、`MayStore` 为 `*`。资源压力是 `PipeA 2.00`、`PipeB 1.00`：

```text
Resource pressure by instruction:
[0]    [1]    [2]    [3]    [4]    [5]    [6]    [7]    Instructions:
 -      -     1.00    -      -      -      -      -     ld	a1, 0(a0)
 -      -      -     1.00    -      -      -      -     add	a1, a1, a2
 -      -     1.00    -      -      -      -      -     sd	a1, 0(a3)
```

`ld` 与 `sd` 各占一条 `PipeA` 槽，`add` 走 `PipeB`。`IPC` 低于 1 的原因不是发射宽度，而是每轮两次访存都要经过 `PipeA`：3 条指令里有 2 条必须用同一条流水线，`Block RThroughput` 因此是 2.0 而不是 1.5。

`-bottleneck-analysis` 本可以把这些百分比直接列出来，但它对顺序核不工作。实测第一行就是：

```text
warning: bottleneck analysis is not supported for in-order CPU 'sifive-u74'.
```

警告之后它仍然打印了 `Resource Pressure [0.00%]`、`Data Dependencies [99.00%]`、`Register Dependencies [99.00%]`。这些百分比是按通用逻辑算的，**不能当作顺序核的结论**。正确用法是：顺序核只看 `Total Cycles`、`IPC`、`Block RThroughput` 和 `Resource pressure`，瓶颈分析留给 OOO 模型。

换一个 OOO 模型对照，数字立刻不同。同样四条依赖 `mul`，`-mcpu=sifive-p670` 得到：

```text
Total Cycles:      803
Dispatch Width:    4
IPC:               0.50
Block RThroughput: 4.0
```

`Dispatch Width` 从 2 变成 4，`Total Cycles` 从 1201 降到 803，时间线里能看到 `D==eeER` 这种带等待的形态。这不是"p670 更快"的结论，而是"两份模型对同一段代码的假设不同"。拿 mca 做跨核比较时，先确认 `Dispatch Width`、`MicroOpBufferSize`、`LoadLatency` 三个参数，再谈周期。

### 18.5 调度器在哪：pre-RA 与 post-RA 两个位置

调度不是一次，而是两次。`llc -debug-pass=Structure` 的本机输出（节选真实 pass 名）里可以按顺序找到：

```text
... -register-coalescer -rename-independent-subregs -machine-scheduler
    -livedebugvars -livestacks -virtregmap -liveregmatrix
    -spill-code-placement ... -greedy -virtregrewriter
    -riscv-insert-vsetvli -riscv-dead-defs ... -greedy -virtregrewriter
    ... -postra-machine-sink ... -shrink-wrap -prolog-epilog ...
    -postrapseudos -riscv-post-ra-expand-pseudo -kcfi -riscv-load-store-opt
    -postmisched -gc-analysis ... -block-placement ...
```

两处关键：

1. `machine-scheduler` 在 `register-coalescer` 之后、`greedy`（寄存器分配）之前。这是 pre-RA 调度，能看到虚拟寄存器、能做较激进的移动。
2. `postmisched` 在 `prolog-epilog`、`riscv-post-ra-expand-pseudo` 之后。这是 post-RA 调度，只能做局部重排，因为物理寄存器已定、伪指令已展开。

RISC-V 在 `RISCVTargetMachine::createMachineScheduler` 里返回 `createSchedLive<RISCVPreRAMachineSchedStrategy>(C)`，并挂上宏融合、load/store clustering、向量 mask 等 mutation；`createPostMachineScheduler` 用 `createSchedPostRA(C)`，mutation 集合更少。策略不同，但读模型的方式相同：都查 `SchedModel` 里那条指令的 `WriteRes`。

一个实践含义：改 `RISCVSched*.td` 的 Latency，最先受影响的是 pre-RA 调度的顺序；post-RA 只能微调。看到"顺序几乎没变"时，先确认改动是否落在 post-RA 能动的范围内。

### 18.6 用 mca 判断依赖链瓶颈的四步

给一段循环体，想判断它卡在哪：

1. 用真实的 `-mcpu` 跑一遍，记下 `Total Cycles`、`IPC`、`Block RThroughput`。
2. 看 `Resource pressure per iteration`。若某个资源接近或超过 `Block RThroughput`，瓶颈在资源；若所有资源都远低于它，瓶颈在依赖。
3. 用 `-timeline -iterations=1` 看首轮。`D` 之间的间隔就是实际依赖间隔；如果某条指令前面有空格（等待），那一格就是依赖造成的停顿。
4. 改写序列（拆依赖、换累加器、加深流水），重跑同一命令。只有数字下降才算改对；`IPC` 上升但 `Total Cycles` 不变，说明只是把等待挪了位置。

这四步的顺序不能反。先看资源再看依赖，是因为资源压力是模型里最硬的部分（由 `WriteRes` 的资源列表决定），而依赖间隔会被 `ReadAdvance`、renaming、顺序/乱序假设共同影响，解释空间更大。

## 可跑实验

### 实验 1：一次跑完五个 region

```powershell
cd D:\Learning-with-Claude
.\compilers\riscv_backend\code\tools\run.ps1 labs\18-mca.ps1
```

脚本在 `%TEMP%\rvlab18` 写 `regions.s`，包含 `mul_add`、`dep_add`、`indep_add`、`dep_mul`、`load_use` 五个 `LLVM-MCA-BEGIN/END` 区域，然后打印摘要。预期摘要与 18.2/18.4 的表一致：

```text
[0] Code Region - mul_add      Total Cycles: 401   IPC: 0.50
[1] Code Region - dep_add      Total Cycles: 403   IPC: 0.99
[2] Code Region - indep_add    Total Cycles: 203   IPC: 1.97
[3] Code Region - dep_mul      Total Cycles: 1201  IPC: 0.33
[4] Code Region - load_use     Total Cycles: 401   IPC: 0.75
```

### 实验 2：完整报告与时间线

同一脚本会打印 `mul_add` 的完整报告（前 45 行），再单独对 `dep_mul` 跑 `-iterations=1 -timeline`。预期看到 18.3 里的 `DeeE` 间隔，以及 `Average Wait times` 表全为 0.0——顺序核在依赖满足时不会显示队列等待。

### 实验 3（失败/边界）：三种不该相信 mca 的情形

脚本最后三行分别演示：

- `-bottleneck-analysis` 对 `sifive-u74` 打印 `warning: bottleneck analysis is not supported for in-order CPU`，但仍输出百分比。
- 不给 `-mcpu`：mca 退到宿主默认 CPU，打印 `'skylake' is not a recognized processor for this target (ignoring processor)`，随后用默认模型算 RISC-V 指令——数字没有意义。
- 给 `-mcpu=generic-rv64`：这是 RV64I 的通用 CPU，不含 M 扩展，于是 `mul` 直接解析失败：`error: instruction requires the following: 'Zmmul'`。

第三条最值得记：`-mcpu` 同时决定 XLEN 和扩展集合。mca 不会从指令反推 ISA，它只按 CPU 的 feature 解析汇编。

### 实验 4：给循环体做一次"改写—复测"

把练习 D 的三段循环体（`loop_a` / `loop_b` / `loop_c`）复制到自己的文件，逐个跑：

```powershell
.\compilers\riscv_backend\code\tools\run.ps1 labs\practice-d-sched-link.ps1
```

本机结果是 `loop_a` 603 拍/100 迭代（7 条指令）、`loop_b` 803 拍（10 条指令，但每次处理 2 个元素）、`loop_c` 1303 拍（16 条指令，每次 4 个元素）。换算成"每元素周期"是 6.03 / 4.02 / 3.26。这组数字说明：单看 `Total Cycles` 会误判，必须先除以每轮处理的元素数。

## 源码落点

路径相对 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。

| 位置 | 符号 | 读它回答什么问题 |
|---|---|---|
| `RISCVSchedule.td` | `WriteIALU`、`WriteIMul`、`WriteIDiv`、`WriteLDD`、`WriteSTD`、`ReadIALU` | 抽象读写事件的名字与含义 |
| `RISCVInstrInfo.td` | `ADD`（约 818 行）、`ADDI`、`LD`、`SD` 的 `Sched<[...]>` | 每条指令声明了哪些写/读事件 |
| `RISCVSchedSiFive7.td` | `SiFive7SchedMachineModel`、`WriteRes<WriteIALU, [PipeAB]>`、`SiFive7AnyToGPRBypass` | 延迟、资源、旁路、顺序核参数 |
| `RISCVSchedGenericOOO.td` | `GenericOOOModel`（`IssueWidth`、`MicroOpBufferSize`、`LoadLatency`） | 乱序模型的对照参数 |
| `RISCVProcessors.td` | `SIFIVE_U74`（`SiFive7Model` + `SiFive7TuneFeatures`）、`SIFIVE_U54`（`RocketModel`） | `-mcpu` 名字到底映射到哪个机器模型 |
| `RISCVTargetMachine.cpp` | `RISCVTargetMachine::createMachineScheduler`、`createPostMachineScheduler`、`substitutePass(&PostRASchedulerID, &PostMachineSchedulerID)` | pre-RA / post-RA 调度的装配与 mutation |
| `RISCVMachineScheduler.cpp` | `RISCVMachineScheduler`、`TryCand.initResourceDelta(DAG, SchedModel)` | 调度策略如何用资源增量做决策 |
| `MCA/RISCVCustomBehaviour.cpp` | llvm-mca 的 RISC-V 自定义行为 | 为什么某些指令的 mca 行为需要额外表 |

## 常见坑

1. **把 Latency 当成依赖间隔**。`addi` 的 Latency 3，但依赖链实测 1 拍/条，因为模型里还有 `ReadAdvance` 旁路。判断间隔看 `-timeline`，不要看表里的数字。
2. **在顺序核上用 `-bottleneck-analysis`**。`sifive-u74` 会打印 "not supported for in-order CPU"，后面的百分比只是通用计算，不是这颗核的结论。
3. **不给 `-mcpu` 就开跑**。mca 会退到宿主 CPU（本机提示 `'skylake' is not a recognized processor`），此时周期数没有任何参考价值。
4. **以为 `-mcpu` 只影响调度**。它还决定 XLEN 和扩展集合：`generic-rv64` 不含 M，`mul` 直接解析失败。
5. **mca 标记文件末尾没有换行**。`# LLVM-MCA-END` 是最后一行且无结尾换行时，会报 `Unknown instrumentation type in LLVM-MCA comment: EN`。加一个空行即可。
6. **拿 `Total Cycles` 直接比较不同展开度的循环**。必须先除以每轮处理的元素数；`loop_c` 的 1303 拍看起来最差，实际每元素只要 3.26 拍。
7. **把 mca 当性能测量**。它不建模缓存、分支预测、TLB、内存别名，也不建模真实的旁路网络。它适合比较同一模型下的两种写法，不适合预测绝对性能。
8. **忽略 `Block RThroughput` 与资源表的关系**。资源表里某个资源常年 1.00，而 `Block RThroughput` 也是 1.0，说明这个块被该资源钉死；此时改指令顺序无效，要改指令组合。
9. **用 `-mcpu` 名字猜模型**。`sifive-u74` 用 `SiFive7Model`，`sifive-u54` 用 `RocketModel`；资源名前缀 `VLEN512` 说明向量长度也被参数化。

## 自我检查

- [ ] 能说出 `SchedWrite` / `SchedRead`、指令上的 `Sched<[...]>`、`WriteRes` 三层各自的作用，并指出哪一层带周期数。
- [ ] 能解释 `mul_add` 报告里 `IPC 0.50`、`Block RThroughput 1.0`、`PipeA/PipeB 各 1.00` 三者为什么自洽。
- [ ] 能解释 `addi` 的 Latency 3 与依赖间隔 1 拍为什么不矛盾，并指出 `ReadAdvance` 在哪个文件。
- [ ] 能用 `dep_add` 与 `indep_add` 的 `Total Cycles`（403 vs 203）区分依赖瓶颈与发射瓶颈。
- [ ] 能说出 pre-RA `machine-scheduler` 与 post-RA `postmisched` 在 pass 序列里的位置，以及为什么改 Latency 主要影响前者。
- [ ] 能复现 `-bottleneck-analysis` 对顺序核的 warning，并说明为什么不能采信其百分比。
- [ ] 能复现 `-mcpu=generic-rv64` 下 `mul` 的 `Zmmul` 解析错误，并解释 `-mcpu` 与扩展集合的关系。
- [ ] 能用"每元素周期"而不是 `Total Cycles` 比较 `loop_a/b/c`。

## 延伸阅读

- LLVM 文档 `llvm-mca`：命令行选项、`-timeline` 视图、`-bottleneck-analysis` 的适用条件。
- `llvm/docs/CodeGenerator.rst` 中 "Machine Scheduling" 一节：`ScheduleDAGMI`、`ScheduleDAGMILive` 与 mutation 的关系。
- `RISCVSchedSiFive7.td` 的注释：Pipe A / Pipe B / V 三条流水线的职责划分。
- 本课程第 19 章（伪指令展开）：`riscv-insert-vsetvli` 与 `riscv-expand-pseudo` 在 pass 序列里的位置，正好补全本章 18.5 的调度上下文。
- 本课程练习 D：用 mca 比较三种循环写法，并把 `Total Cycles` 换算成每元素周期。

[上一章：GlobalISel 在 RISC-V](./17-globalisel.md) | [下一章：伪指令展开与机器 pass](./19-pseudo-expansion.md)