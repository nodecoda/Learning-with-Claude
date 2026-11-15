# Ch 12 Sched model 三层：`SchedRead/Write`、`WriteRes`、`ReadAdvance`

> **目标**：把 `.td` 里的调度模型拆成三层——指令侧的 `Sched<[...]>`、写侧的 `WriteRes`、读侧的 `ReadAdvance`——并且能用 `llc` 和 `llvm-mca` 的实测数字反推每一层。学完你能拿到一条没见过的 RISC-V 指令，判断它缺哪一层绑定，以及缺了会怎样。
>
> **环境**：LLVM 24.0.0git，`llc` 与 `llvm-mca` 均来自 `/home/ubuntu/llvm/builds/explore/bin/`；`-mattr=+m`；CPU 覆盖 `rocket-rv64`、`sifive-u74`、`xiangshan-nanhu`、`spacemit-x60`、`generic-ooo`。所有周期数字都是模型输出，不是芯片实测。

## 为什么值得拆成三层

初学者看 sched model，容易把它当成一张「指令 → 周期数」的表。真到改模型时就会发现：同一条 `mul`，在不同 CPU 上是 3、4、6 个周期；同一个 `ld`，对 `add` 是 4 拍、对 `mul` 是 5 拍。这说明模型至少有两套独立的旋钮：

- **谁产生结果、占什么资源、多久可用** → 写侧（`SchedWrite` + `WriteRes`）。
- **谁来读这个结果、能提前几拍读** → 读侧（`SchedRead` + `ReadAdvance`）。

再加一层**指令到这两个东西的绑定**（`Sched<[...]>`），才是完整的三层。少任何一层，调度器要么算错延迟，要么根本不知道这条指令存在。

## 先建立直觉：一条 `add` 的完整链路

```text
        RISCVInstrInfo.td
   def ADD : ALU_rr<...>, Sched<[WriteIALU, ReadIALU, ReadIALU]>;
                                │            │        │
                    ┌───────────┘            └────────┴────────┐
                    ▼                                          ▼
             WriteIALU (SchedWrite)                     ReadIALU (SchedRead)
                    │                                          │
                    ▼                                          ▼
   WriteRes<WriteIALU, [SiFive7PipeAB]>            ReadAdvance<ReadIALU, 0>
   { Latency = 3; }                                 （或 XS 的 1 拍 bypass）
                    │
                    ▼
        建图时：SU(add) → SU(consumer) 的 Data Latency
```

三个箭头对应三个问题：

1. `Sched<[...]>`：这条指令定义哪个 `SchedWrite`、每个 use 用哪个 `SchedRead`？
2. `WriteRes`：这个 `SchedWrite` 占哪些 `ProcResource`、延迟多少、资源占用几拍？
3. `ReadAdvance`：这个 `SchedRead` 能从生产者的延迟里减掉几拍？

下面逐层拆。

## 第一层：`SchedWrite` / `SchedRead` 只是名字

`RISCVSchedule.td` 前半部分全是「声明」，没有数字：

```tablegen
def WriteIALU       : SchedWrite;    // 32 or 64-bit integer ALU operations
def WriteIALU32     : SchedWrite;    // 32-bit integer ALU operations on RV64I
def WriteShiftImm   : SchedWrite;
def WriteIDiv       : SchedWrite;    // 32-bit or 64-bit divide
def WriteIMul       : SchedWrite;    // 32-bit or 64-bit multiply
def WriteJmp        : SchedWrite;
def WriteLDB        : SchedWrite;    // Load byte
def WriteLDH        : SchedWrite;
def WriteLDW        : SchedWrite;
def WriteLDD        : SchedWrite;    // Load double-word
def WriteSTB        : SchedWrite;
...
def ReadIALU        : SchedRead;
def ReadJalr        : SchedRead;
def ReadMemBase     : SchedRead;
def ReadStoreData   : SchedRead;
```

这一层的唯一作用是把「语义类别」变成可以被 `WriteRes`/`ReadAdvance` 引用的符号。它本身不含任何微架构信息，所以同一个 `WriteIALU` 可以被 Rocket 绑到 1 拍、被 SiFive7 绑到 3 拍。

为什么分这么细？看几个真实用例：

- `WriteIALU` vs `WriteIALU32`：RV64 上 `addw` 这类 32 位运算在某些核上有额外 1 拍，所以单独建模。
- `WriteLDB/LDH/LDW/LDD`：按访问宽度分开，因为很多核的 byte/half load 比 word/dword 慢（Rocket 就是 `LDB/LDH = 3`、`LDW/LDD = 2`）。
- `WriteIDiv` vs `WriteIRem`：除法和取余可能共用单元但延迟不同；XiangShan 把两者都设成 20，Rocket 把 `IDiv` 设成 33、`IDiv32` 设成 34。

### 指令侧怎么绑

真实定义（`RISCVInstrInfo.td`）：

```tablegen
class ALU_rr<bits<7> funct7, bits<3> funct3, string opcodestr,
             list<Register> Regs = [X]]>
    : RVInstR<funct7, funct3, OPC_OP, (outs GPR:$rd), (ins GPR:$rs1, GPR:$rs2),
              opcodestr, "$rd, $rs1, $rs2">, Sched<[WriteIALU, ReadIALU, ReadIALU]>;

def LB  : Load_ri<0b000, "lb">, Sched<[WriteLDB, ReadMemBase]>;
def LW  : Load_ri<0b010, "lw">, Sched<[WriteLDW, ReadMemBase]>;
def SW  : Store_rri<0b010, "sw">, Sched<[WriteSTW, ReadStoreData, ReadMemBase]>;
def SB  : Store_rri<0b000, "sb">, Sched<[WriteSTB, ReadStoreData, ReadMemBase]>;
```

读这个列表的规则：**第一个是 def 侧的 write，后面按操作数顺序是 use 侧的 reads**。`SW` 有两个源操作数（store data、base address），所以是 `ReadStoreData, ReadMemBase`。store 没有结果寄存器，但仍有 `WriteSTW`——因为写侧建模的不只是「产生寄存器值」，还包括「占用 store 单元多久」。

这一点在面试里常被追问：「store 没有 def，为什么还要 SchedWrite？」答案是资源占用：`WriteSTW` 决定 store 在 `ProcResource` 上占几拍，以及后续依赖它的指令要等多久。

## 第二层：`WriteRes` 把名字变成资源与延迟

`WriteRes` 是 `SchedWrite` 与微架构之间的绑定。Rocket 的例子（`RISCVSchedRocket.td`）：

```tablegen
// Integer arithmetic and logic
def : WriteRes<WriteIALU32, [RocketUnitALU]>;
def : WriteRes<WriteIALU, [RocketUnitALU]>;

// Integer multiplication
let Latency = 4 in {
def : WriteRes<WriteIMul, [RocketUnitIMul]>;
def : WriteRes<WriteIMul32, [RocketUnitIMul]>;
}

// Integer division
// Worst case latency is used.
def : WriteRes<WriteIDiv32, [RocketUnitIDiv]> {
  let Latency = 34;
  let ReleaseAtCycles = [34];
}
def : WriteRes<WriteIDiv, [RocketUnitIDiv]> {
  let Latency = 33;
  let ReleaseAtCycles = [33];
}
```

三个字段：

- **资源列表** `[RocketUnitIMul]`：这条指令占用哪个 `ProcResource`。
- **`Latency`**：结果从发射到可被消费者读取的拍数，默认是 1。
- **`ReleaseAtCycles`**：资源在第几拍释放。对流水化单元通常等于 1（每拍都能接新指令）；对非流水化单元（除法器）就是整个占用长度。

`ReleaseAtCycles` 有两个数的时候，要和资源列表一一对应。SiFive7 的 32 位除法：

```tablegen
def : WriteRes<WriteIDiv32,  [PipeB, IDiv]> {
  let Latency = 34;
  let ReleaseAtCycles = [1, 33];
}
```

含义是：`PipeB` 只被占 1 拍（发射那一下），`IDiv` 被占 33 拍。这解释了为什么 SiFive7 上连续两条 `div` 的吞吐是 65 拍而不是 33 拍——`PipeB` 空出来了，但除法器还忙着。

### 实测：同一个 `SchedWrite`，不同数字

`llvm-mca -instruction-info` 对 `ops.s`（`add/mul/div/ld/sd`）的输出，本机实测：

| CPU | add | mul | div | ld | sd |
|---|---|---|---|---|---|
| `rocket-rv64` | 1 | 4 | 33 | 2 | 1 |
| `sifive-u74` | 3 | 3 | 66 | 3 | 1 |
| `xiangshan-nanhu` | 1 | 3 | 20 | 5 | 1 |
| `spacemit-x60` | 1 | 6 | 20 | 4 | 4 |
| `generic-ooo`（`+64bit`） | 1 | 4 | 21 | 4 | 1 |

命令（每个 CPU 跑一次）：

```bash
"$MCA" -mtriple=riscv64 -mcpu=$cpu -mattr=+m -instruction-info ops.s
```

这张表里没有任何一条指令的 opcode 变了，变的只是 `WriteRes`：

- `sifive-u74` 的 `add=3` 来自 `let Latency = 3 in { def : WriteRes<WriteIALU, [PipeAB]>; ... }`。
- `spacemit-x60` 的 `mul=6` 来自注释「mul 是 5，mulh/mulhsu/mulhu 是 6，取最坏情况」。
- `xiangshan-nanhu` 的 `div=20` 来自 SRT16 算法的 `let Latency = 20, ReleaseAtCycles = [20]`。

### `Latency` 和 `RThroughput` 不是一回事

`-instruction-info` 的第二列是 Latency，第三列是 RThroughput（reciprocal throughput，倒数吞吐）。二者区别：

```text
rocket  div:  Latency 33, RThroughput 33.00   -> 完全非流水化
sifive  div:  Latency 66, RThroughput 65.00   -> 几乎完全非流水化
xs      div:  Latency 20, RThroughput 10.00   -> 有两级可以重叠？
spacemit div: Latency 20, RThroughput 20.00   -> 非流水化
```

面试里问「这条指令要几个周期」，正确反问是「你说的是 latency 还是 throughput」。写调度模型时，`Latency` 决定依赖边的长度，`RThroughput` 决定资源压力——后者由 `WriteRes` 的资源数和 `ReleaseAtCycles` 共同推出。

## 第三层：`ReadAdvance` 从消费者一侧改延迟

如果只有 `Latency`，那 `ld` 对任何消费者都是同样的等待。真实 CPU 有 bypass 网络：load 的结果可能提前一拍送到 ALU，但没有提前送到乘法器。`ReadAdvance` 就是建模这个的。

XiangShan 的写法（`RISCVSchedXiangShanNanHu.td`）：

```tablegen
class XS2LoadToALUBypass<SchedRead read>
    : ReadAdvance<read, 1, [WriteLDB, WriteLDH, WriteLDW, WriteLDD,
                            WriteAtomicW, WriteAtomicD,
                            WriteAtomicLDW, WriteAtomicLDD]>;

def : ReadAdvance<ReadJmp, 0>;
def : ReadAdvance<ReadMemBase, 0>;
def : XS2LoadToALUBypass<ReadIALU>;
def : XS2LoadToALUBypass<ReadIALU32>;
def : XS2LoadToALUBypass<ReadShiftImm>;
...
def : ReadAdvance<ReadIMul, 0>;
```

语义：当 `ReadIALU` 这个 use 的生产者是上面列出的 load 时，消费者的等待时间减 1 拍。`ReadAdvance<ReadIMul, 0>` 则明确「乘法器没有这个 bypass」。

### 实测：同一个 load，两种消费者

`la.ll`：

```llvm
target triple = "riscv64-unknown-elf"
define i64 @la(ptr %p, i64 %a) {
  %x = load i64, ptr %p
  %y = add i64 %x, %a
  ret i64 %y
}
```

```bash
"$LLC" -mtriple=riscv64 -mcpu=xiangshan-nanhu -mattr=+m -O2 \
  -misched-print-dags la.ll -o /dev/null
```

输出：

```text
SU(2):   %2:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
  Latency            : 5
  Successors:
    SU(3): Data Latency=4 Reg=%2
```

`ld` 本身 `Latency: 5`，但到 `add` 的边只有 `Data Latency=4`——那 1 拍就是 `XS2LoadToALUBypass<ReadIALU>`。

同一份 IR，`min.ll` 里 load 的消费者是 `mul`：

```text
SU(5):   %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
  Latency            : 5
  Successors:
    SU(6): Data Latency=5 Reg=%5
```

差 1 拍，因为 `mul` 用的是 `ReadIMul`，没有 bypass。

### 四个 CPU 的同一张边

同一个 `min.ll`、同一条 `LD → MUL` 边，`-misched-print-dags` 的 `Data Latency`：

| CPU | `LD` 节点 Latency | `LD → MUL` Data Latency |
|---|---|---|
| `rocket-rv64` | 2 | 2 |
| `sifive-u74` | 3 | 3 |
| `generic`（= `generic-rv64` / SpacemitX60） | 4 | 4 |
| `xiangshan-nanhu` | 5 | 5 |

这张表是本章最该记住的东西：**DAG 的形状由 IR 决定，边上的数字由 sched model 决定**。你在 `-print-after=machine-scheduler` 里看到的顺序差异，很多时候就是这几个数字换了。

## 全局层：`SchedMachineModel` 的字段

除了逐指令的绑定，每个模型还有一组全局参数。四个真实模型的头部：

```tablegen
// Rocket（顺序、单发射）
def RocketModel : SchedMachineModel {
  let MicroOpBufferSize = 0; // Rocket is in-order.
  let IssueWidth = 1;        // 1 micro-op is dispatched per cycle.
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  let CompleteModel = false;
  ...
}

// SiFive7（顺序、双发射）
class SiFive7SchedMachineModel<int vlen> : SchedMachineModel {
  let MicroOpBufferSize = 0;
  let IssueWidth = 2;
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  let CompleteModel = 0;
  let EnableIntervals = true;
  int VLEN = vlen;
  ...
}

// GenericOOO（乱序）
def GenericOOOModel : SchedMachineModel {
  int IssueWidth = 6;
  int MicroOpBufferSize = 192;
  int LoadLatency = 4;
  int MispredictPenalty = 8;
  let CompleteModel = 0;
}

// XiangShan NanHu（乱序）
def XiangShanNanHuModel : SchedMachineModel {
  let MicroOpBufferSize = 256;
  let LoopMicroOpBufferSize = 48;  // Instruction queue size
  let IssueWidth = 6;  // 6-way decode and dispatch
  let LoadLatency = 4;
  let MispredictPenalty = 11; // Based on estimate of pipeline depth.
  let CompleteModel = 0;
}
```

字段含义：

| 字段 | 作用 | 影响什么 |
|---|---|---|
| `IssueWidth` | 每拍最多发射几个微操作 | 发射端资源压力、`-instruction-info` 的 Dispatch Width |
| `MicroOpBufferSize` | 乱序缓冲大小；`0` 表示顺序 | 调度器是否允许「先发后面的、等前面就绪」 |
| `LoadLatency` | load 的**启发式**延迟 | `getLoadLatency()`、地址构造代价等，**不是** DAG 边延迟 |
| `MispredictPenalty` | 分支预测失败的代价 | if-conversion、分支代价模型 |
| `CompleteModel` | 模型是否覆盖所有指令 | `false` 时未列出的指令走 fallback |
| `EnableIntervals` | 是否用 `InstrItinerary` 风格 | 影响 mca 的 timeline 精度 |

### `LoadLatency` 的坑：它不改调度边

Rocket 的 `LoadLatency = 3`，但实测 `ld` 的 DAG latency 是 2。原因是这两个数字来自不同的地方：

- `LoadLatency` 是 `RISCVSubtarget::getLoadLatency()` 的返回值，用于 `getMaxBuildIntsCost()` 之类的启发式。
- 调度边用的是 `WriteRes<WriteLDD, [RocketUnitMem]>` 上的 `Latency = 2`。

有一个隐藏选项可以改前者：`-riscv-sched-load-latency=N`。实测：

```bash
"$LLC" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m -O2 \
  -riscv-sched-load-latency=10 -misched-print-dags min.ll -o /dev/null
```

`LD → MUL` 的 `Data Latency` 仍然是 2。这条实验能一次性打消「`LoadLatency` 是调度延迟」的误解。

## 底层机制：TableGen 生成什么，调度器怎么用

`WriteRes` 在 TableGen 里最终变成 `MCSchedModel` 的三张表：

```text
WriteResTable[]     : 每个 SchedWrite -> { 资源掩码, Latency, ReleaseAtCycles, NumMicroOps }
ReadAdvanceTable[]  : 每个 SchedRead  -> { 默认 advance, [producer write, advance] 列表 }
ProcResourceTable[] : 每个 ProcResource -> { 名称, BufferSize, 父资源 }
```

调度器建图时（`ScheduleDAGInstrs::buildSchedGraph`），对每条依赖边做：

```text
edge.Latency = producer.WriteRes.Latency - consumer.ReadAdvance(producer.Write)
```

这就是为什么 `LD → ADD` 能是 5 - 1 = 4，而 `LD → MUL` 是 5 - 0 = 5。

资源侧则在 `ScheduleDAGMI` 的 ready 队列里用 `ResourceSegments` 记录「这个资源下一次可用的周期」，`ReleaseAtCycles` 决定占用区间。

### 一个容易忽略的绑定：`InstRW`

除了在指令类上写 `Sched<[...]>`，还可以用 `InstRW` 直接覆盖某条/某类指令：

```tablegen
def : InstRW<[WriteIALU], (instrs COPY)>;
```

XiangShan 用这一行把 `COPY` 绑到 `WriteIALU`（latency 1）。这解释了为什么 `min.ll` 的 DAG 里 `COPY → LD` 的边是 `Data Latency=1`，而 `sifive-u74` 上同一条边是 3——SiFive7 没给 `COPY` 单独覆盖，它落到了 `WriteIALU` 的 3 拍上。

## 踩雷集锦

1. **把 `Latency` 当 `RThroughput`**。`div` 的 latency 是 33，不代表每 33 拍才能发一条；如果除法器流水化，吞吐可能更低。反过来，SiFive7 的 `div` latency 66、RThroughput 65，两者也不是同一个数。写模型时两个都要看。

2. **用 `LoadLatency` 解释 load 的调度延迟**。`LoadLatency` 是全局启发式字段，DAG 边来自 `WriteLDD` 的 `WriteRes`。`-riscv-sched-load-latency=10` 的实验能直接证伪这个误解。

3. **以为 `ReadAdvance` 是「加几拍」**。它是**减**：消费者可以在生产者结果可读之前 `advance` 拍就开始读（靠 bypass）。所以 `ReadAdvance<ReadIALU, 1>` 让边从 5 变 4，不是变 6。

4. **`Sched<[...]>` 的 read 顺序和操作数顺序不一致**。`SW` 是 `[WriteSTW, ReadStoreData, ReadMemBase]`；如果把两个 read 写反，base 和 data 的 bypass 会绑错，调度结果会变成「看起来能跑但周期不对」。这种 bug 不会崩，只会让性能悄悄退化。

5. **以为 `CompleteModel = false` 表示「没有模型」**。它的意思是「这张表没覆盖所有指令」，未覆盖的走 fallback（通常是 `WriteRes` 默认 latency 1、无资源）。RISC-V 所有主流模型都是 `false`，包括 XiangShan 和 SiFive7。

6. **以为缺 `WriteRes` 会被 TableGen 抓出来**。完整性检查 `CodeGenSchedModels::checkCompleteness()` 的第一行是 `if (!ProcModel.ModelDef->getValueAsBit("CompleteModel")) continue;`；RISC-V 所有主流模型都写 `CompleteModel = false`，所以缺 `WriteRes` 不会报错。运行时它会走 `TargetInstrInfo::defaultDefLatency()`：transient 返回 0、`mayLoad` 返回 `STI.getLoadLatency()`、high-latency def 返回 `SchedModel.HighLatency`、其余返回 1，并且完全没有资源占用信息。结果是「能编译、能跑、周期全错」。加新指令时三层都要自己检查：`Sched<>`、`WriteRes`、`ReadAdvance`。

## 动手练习

### 练习 1：从 `.td` 反推 mca 的数字

对 `sifive-u74`，找到 `WriteIALU`、`WriteIMul`、`WriteIDiv` 的 `WriteRes`，解释为什么 mca 给出 `add=3`、`mul=3`、`div=66`。特别说明 `div` 的 `RThroughput=65` 是怎么从资源和 `ReleaseAtCycles` 推出来的。

<details>
<summary>提示</summary>

`WriteIDiv` 在 SiFive7 里是 `SchedAlias` 指向一个 variant；先找到 variant 的定义，再看 `[PipeB, IDiv]` 和 `ReleaseAtCycles`。
</details>

### 练习 2：验证 `ReadAdvance` 的方向

把 `la.ll` 的消费者从 `add` 改成 `mul`，重新跑 `xiangshan-nanhu` 的 `-misched-print-dags`，确认 `Data Latency` 从 4 变 5。再把消费者改成 `store` 的 data 操作数，观察 `ReadStoreData` 的 advance。

### 练习 3：让 TableGen 报出缺失的调度信息

RISC-V 的模型是 `CompleteModel = false`，所以缺 `WriteRes` 默认静默。把某个模型的 `CompleteModel` 临时改成 `true`，重新构建 TableGen，就能看到 `No schedule information for instruction '...' in SchedMachineModel '...'`。**这一步需要重新构建，属于 guided 实验**；不改源码时，可以直接读 `llvm/utils/TableGen/Common/CodeGenSchedule.cpp` 的 `checkCompleteness()`，确认它在什么条件下才会报告。

### 练习 4：比较 `IssueWidth`

用 `-instruction-info` 跑 `min.s`，比较 `rocket-rv64`（IssueWidth 1）和 `sifive-u74`（IssueWidth 2）的 `Dispatch Width` 与 `Block RThroughput`。解释为什么后者的 `add` latency 更大，但整体吞吐可能更好。

## 重点整理

- 三层结构：`Sched<[...]>` 绑定 → `WriteRes` 给资源和延迟 → `ReadAdvance` 给消费者的提前量。
- `SchedWrite` 建模 def 侧（包括 store 的资源占用）；`SchedRead` 建模 use 侧。
- `Latency` 决定依赖边；`ReleaseAtCycles` 决定资源占用；`RThroughput` 是二者的综合结果。
- `ReadAdvance` 是减法，且可以只对特定 producer 生效（`XS2LoadToALUBypass`）。
- `SchedMachineModel` 的 `IssueWidth`、`MicroOpBufferSize`、`LoadLatency`、`MispredictPenalty` 是全局参数；`LoadLatency` 不参与 DAG 建边。
- 同一个 `WriteIALU` 在不同 CPU 上可以是 1 拍或 3 拍，所以「这条指令几拍」必须先说 CPU。

## 自我检核

- [ ] 我能写出 `SW` 的 `Sched<[...]>` 列表，并解释为什么 store 也有 `SchedWrite`。
- [ ] 我能解释 `ReleaseAtCycles = [1, 33]` 里两个数字分别对应哪个资源。
- [ ] 我能说出 `Latency` 与 `RThroughput` 的区别，并各举一条指令。
- [ ] 我能用一条命令打印 `LD → ADD` 与 `LD → MUL` 的 `Data Latency` 差异。
- [ ] 我知道 `-riscv-sched-load-latency` 改的是什么、不改什么。
- [ ] 我能解释 `InstRW<[WriteIALU], (instrs COPY)>` 的作用。
- [ ] 我知道 `CompleteModel = false` 的实际含义。

## 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVSchedule.td`（第 1–260 行）**
  - **读哪里**：所有 `Write*` / `Read*` 的声明，以及后面的 `SchedReadAdvance` 辅助类。
  - **和本章的关联**：第一层的完整符号表；改模型前先确认有没有现成的 write/read 可用。

- **`llvm/lib/Target/RISCV/RISCVSchedRocket.td`（全文，约 200 行）**
  - **读哪里**：模型头、`ProcResource` 定义、`WriteRes` 段落。
  - **和本章的关联**：最小的完整例子，适合逐行读；Rocket 的 `LoadLatency=3` 与 `LDD latency=2` 的对比就在这。

- **`llvm/lib/Target/RISCV/RISCVSchedSiFive7.td`（约 300–380 行）**
  - **读哪里**：`SiFive7WriteResBase` 里的 `WriteIALU`、`WriteIMul`、`WriteIDiv32`。
  - **和本章的关联**：多资源 `ReleaseAtCycles` 和 `SchedAlias` 的真实用法。

- **`llvm/lib/Target/RISCV/RISCVSchedXiangShanNanHu.td`（约 200–250 行）**
  - **读哪里**：`XS2LoadToALUBypass` 与整段 `ReadAdvance`。
  - **和本章的关联**：第三层最清晰的例子；`LD→ADD=4`、`LD→MUL=5` 就来自这里。

- **`llvm/include/llvm/MC/MCSchedule.h`**
  - **读哪里**：`MCSchedModel`、`MCWriteLatencyEntry`、`MCReadAdvanceEntry` 的结构定义。
  - **和本章的关联**：TableGen 生成的三张表在 C++ 侧长什么样。

- **LLVM 官方文档：*TableGen Backends — Scheduling Models***
  - **读哪里**：`SchedMachineModel`、`WriteRes`、`ReadAdvance` 的语法说明。
  - **和本章的关联**：语法查漏；注意文档里的例子大多是旧版写法，实际以 RISC-V 的 `.td` 为准。

→ [下一章：RISC-V sched models 走读](./13-riscv-sched-models.md)
