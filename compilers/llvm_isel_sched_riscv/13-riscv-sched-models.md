# Ch 13 RISC-V sched models 走读：Rocket / SiFive7 / GenericOOO / XiangShan

> **目标**：把「CPU 名 → 模型 → 资源 → 延迟」这条映射走通。读完你能打开任意一个 `RISCVSched*.td`，在五分钟内说出：这是顺序还是乱序核、几发射、load 几拍、除法器是不是流水化、有没有 bypass、这个模型漏掉了哪些扩展。
>
> **环境**：LLVM 24.0.0git；`llc -mtriple=riscv64 -mcpu=help` 列出 50 多个 CPU；`llvm-mca` 的资源名可以反推模型。本机实测的 CPU 包括 `rocket-rv64`、`sifive-u74`、`sifive-x160`（RV32）、`sifive-x390`、`spacemit-x60`、`xiangshan-nanhu`、`generic-ooo`。

## 为什么模型走读比读文档有用

厂商手册给你的是流水线图，不是 TableGen。真正决定 `llc` 行为的，是有人把那张图翻译成了 `SchedMachineModel` + `ProcResource` + `WriteRes`。翻译过程一定有权衡：哪些单元合并了、哪些延迟取了最坏值、哪些扩展干脆没建模。这些取舍在 `.td` 的注释里写得比手册诚实。

而且 RISC-V 的模型有一个很好的性质：**同一个 `WriteIALU`/`WriteIMul`/`WriteLDD` 被所有模型共享**（都定义在 `RISCVSchedule.td`）。所以比较模型时，变量只有「绑定」，不是「指令集合」。这让你能非常干净地回答「为什么这条 `mul` 在这个核上更慢」。

## 先建立直觉：CPU、模型、资源的三层映射

```text
 -mcpu=sifive-u74
        │
        ▼
 RISCVProcessors.td
 def SIFIVE_U74 : RISCVProcessorModel<"sifive-u74", SiFive7Model, [...]>
        │                                  │
        │                                  └── SiFive7Model = SiFive7VLEN512Model
        ▼
 RISCVSchedSiFive7.td
 def SiFive7SchedMachineModel<512> { IssueWidth=2; MicroOpBufferSize=0; ... }
        │
        ▼
 SiFive7ProcResources<512, false, ...>
   VLEN512SiFive7PipeA / PipeB / IDiv / ...
        │
        ▼
 WriteRes<WriteIALU, [PipeAB]> { Latency = 3; }
```

四层里任何一层换了，`llc` 的行为都会变：

- 换 CPU：`sifive-u74` → `sifive-x390`，模型从 VLEN512 变 VLEN1024X300，资源名和向量延迟都变。
- 换 `-mtune`：模型跟着 tune CPU 走（LLVM 24 的行为，见 Ch 0 踩雷 3）。
- 换 feature：`+v` 打开后，`UnsupportedFeatures` 里的扩展会让 mca 报 `lack-sched`。

## 主线：五个模型，五种取舍

### 1. Rocket：最小的完整模型

`RISCVSchedRocket.td` 只有 200 行左右，是读模型的最佳起点。

```tablegen
def RocketModel : SchedMachineModel {
  let MicroOpBufferSize = 0; // Rocket is in-order.
  let IssueWidth = 1;        // 1 micro-op is dispatched per cycle.
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  let CompleteModel = false;
  let UnsupportedFeatures = [HasStdExtZbkb, HasStdExtZbkc, HasStdExtZbkx,
                             HasStdExtZcmt, HasStdExtZknd, HasStdExtZkne,
                             HasStdExtZknh, HasStdExtZksed, HasStdExtZksh,
                             HasStdExtZkr, HasVInstructions, HasVInstructionsI64];
}
```

资源定义很短：

```tablegen
let BufferSize = 0 in {
def RocketUnitALU        : ProcResource<1>; // Int ALU
def RocketUnitIMul       : ProcResource<1>; // Int Multiply
def RocketUnitMem        : ProcResource<1>; // Load/Store
def RocketUnitB          : ProcResource<1>; // Branch
def RocketUnitFPALU      : ProcResource<1>; // FP ALU
}

let BufferSize = 1 in {
def RocketUnitIDiv       : ProcResource<1>; // Int Division
def RocketUnitFPDivSqrt  : ProcResource<1>; // FP Divide/Sqrt
}
```

注意 `BufferSize` 的分组：ALU/IMul/Mem/Branch/FPALU 都是 0（全流水，每拍可接新指令），只有除法器是 1（有一格缓冲）。这是「顺序单发射、除法器几乎不流水」的直接翻译。

绑定上有三档：

```tablegen
def : WriteRes<WriteIALU, [RocketUnitALU]>;            // 默认 Latency = 1
let Latency = 4 in {
def : WriteRes<WriteIMul, [RocketUnitIMul]>;           // mul = 4
}
def : WriteRes<WriteIDiv, [RocketUnitIDiv]> {
  let Latency = 33;
  let ReleaseAtCycles = [33];                          // 完全占用 33 拍
}
let Latency = 2 in {
def : WriteRes<WriteLDW, [RocketUnitMem]>;
def : WriteRes<WriteLDD, [RocketUnitMem]>;
}
```

实测（`llvm-mca -instruction-info ops.s`）：

```text
 1      1     1.00                        add	a0, a1, a2
 1      4     1.00                        mul	a0, a1, a2
 1      33    33.00                       div	a0, a1, a2
 1      2     1.00    *                   ld	a0, 0(a1)
 1      1     1.00           *            sd	a0, 0(a1)
```

`LoadLatency = 3` 但 `ld` 是 2 拍，这个矛盾在 Ch 12 已经解释过：前者是启发式字段，后者来自 `WriteLDD`。

**一句话总结**：Rocket 模型适合「验证工具链」——单发射、顺序、延迟数字干净，任何顺序变化都容易归因。

### 2. SiFive7：一个模型，三个 VLEN 变体

SiFive7 不是一个模型，而是一个 `multiclass`，按向量长度实例化三次：

```tablegen
def SiFive7VLEN128X100Model : SiFive7SchedMachineModel<128> {
  let FPLatencies = SiFive7LowFPLatencies;
  let HasFastGather = true;
}

def SiFive7VLEN512Model : SiFive7SchedMachineModel<512> {
  let FPLatencies = SiFive7DefaultFPLatencies;
}

def SiFive7VLEN1024X300Model : SiFive7SchedMachineModel<1024> {
  let HasDualVALU = true;
  let FPLatencies = SiFive7LowFPLatencies;
  let HasFastGather = true;
}

defvar SiFive7Model = SiFive7VLEN512Model;
defvar SiFiveX100Model = SiFive7VLEN128X100Model;
defvar SiFiveX390Model = SiFive7VLEN1024X300Model;
```

这解释了为什么同一个 `sifive-u74` 和 `sifive-x390` 的 mca 资源名不同：

```text
$ llvm-mca ... -mcpu=sifive-u74 -instruction-info min.s
[0]   - VLEN512SiFive7FDiv
[1]   - VLEN512SiFive7IDiv
[2]   - VLEN512SiFive7PipeA
[3]   - VLEN512SiFive7PipeB

$ llvm-mca ... -mcpu=sifive-x390 -instruction-info min.s
[0]   - VLEN1024X300SiFive7FDiv
[1]   - VLEN1024X300SiFive7IDiv
[2]   - VLEN1024X300SiFive7PipeA
[3]   - VLEN1024X300SiFive7PipeB
```

RV32 的 `sifive-x160` 用 128 位变体：

```text
$ llvm-mca -mtriple=riscv32 -mcpu=sifive-x160 -mattr=+m -instruction-info rv32ops.s
[0]   - VLEN128X100SiFive7FDiv
[1]   - VLEN128X100SiFive7IDiv
[2]   - VLEN128X100SiFive7PipeA
[3]   - VLEN128X100SiFive7PipeB
```

资源命名规则来自 `SiFive7ProcResources<VLEN, ...>` 里的 `NAME#"PipeA"` 拼接，所以名字里带 VLEN 和型号后缀。

SiFive7 的流水线注释值得抄下来：

```tablegen
// The SiFive7 microarchitecture has three kinds of pipelines: A, B, V.
// Pipe A can handle memory, integer alu and vector operations.
// Pipe B can handle integer alu, control flow, integer multiply and divide,
// and floating point computation.
```

对应到绑定：

```tablegen
def PipeAB : ProcResGroup<[PipeA, PipeB]>;

let Latency = 3 in {
def : WriteRes<WriteIALU, [PipeAB]>;      // ALU 可走 A 或 B
}
let Latency = 3 in {
def : WriteRes<WriteIMul, [PipeB]>;       // 乘法只能在 B
}
```

所以 SiFive7 上 `add` 的 RThroughput 是 0.50（两条流水都能发），`mul` 是 1.00（只能走 B）。实测：

```text
 1      3     0.50                        add	a0, a1, a2
 1      3     1.00                        mul	a0, a1, a2
 1      66    65.00                       div	a0, a1, a2
```

`add` 和 `mul` 的 latency 都是 3，但吞吐差一倍——这是「latency 相同、资源不同」的典型例子。

SiFive7 还有两个本课会用到的细节：

- `EnableIntervals = true`：mca 的 timeline 会用 `InstrItinerary` 风格的区间，报告更细。
- `SchedAlias`：`WriteIDiv` 被重定向到一个按 VLEN 变化的 variant，所以文件里直接搜 `WriteRes<WriteIDiv` 只能看到 32 位版本。

### 3. GenericOOO：给「通用乱序核」的占位模型

`RISCVSchedGenericOOO.td` 的头部注释写明了目标：

```tablegen
// `generic-ooo` is expected to target out-of-order application processors
// designed for general-purpose computing.
def GenericOOOModel : SchedMachineModel {
  int IssueWidth = 6;
  int MicroOpBufferSize = 192;
  int LoadLatency = 4;
  int MispredictPenalty = 8;
  let CompleteModel = 0;
}
```

资源用 `ProcResGroup` 表达「多选一」：

```tablegen
def GenericOOOBranch : ProcResource<1>;
def GenericOOOMulDiv : ProcResource<1>;
def GenericOOOInt : ProcResource<2>;
def GenericOOOALU
    : ProcResGroup<[GenericOOOBranch, GenericOOOMulDiv, GenericOOOInt]>;
def GenericOOOLSU : ProcResource<2>;
def GenericOOOFMulDiv : ProcResource<1>;
def GenericOOOFloat : ProcResource<1>;
def GenericOOOFPU : ProcResGroup<[GenericOOOFMulDiv, GenericOOOFloat]>;
```

注意 `GenericOOOALU` 把 Branch、MulDiv、Int 三个组都包进来了。这意味着在模型眼里，一条 `add` 既可能走分支单元，也可能走乘除单元——乱序核的发射端口共享被抽象成「资源组」。

用 `generic-ooo` 时必须加 `-mattr=+64bit`（它本身是 tune-only，不带 `Feature64Bit`）：

```bash
"$MCA" -mtriple=riscv64 -mcpu=generic-ooo -mattr=+m,+64bit -instruction-info ops.s
```

实测：`add=1`、`mul=4`、`div=21`、`ld=4`、`sd=1`。`MicroOpBufferSize=192` 会让 mca 的调度窗口很大，`Block RThroughput` 反而更能反映吞吐瓶颈。

### 4. XiangShan NanHu：最细的公开模型之一

模型头：

```tablegen
def XiangShanNanHuModel : SchedMachineModel {
  let MicroOpBufferSize = 256;
  let LoopMicroOpBufferSize = 48;  // Instruction queue size
  let IssueWidth = 6;  // 6-way decode and dispatch
  let LoadLatency = 4;
  let MispredictPenalty = 11; // Based on estimate of pipeline depth.
  let CompleteModel = 0;
  let UnsupportedFeatures = [HasStdExtZcmt, HasStdExtZkr, HasVInstructions,
                             HasVInstructionsI64];
}
```

资源用 16 项的小缓冲建模发射队列：

```tablegen
let BufferSize = 16 in {
  def XS2ALU : ProcResource<4>;
  def XS2MDU : ProcResource<2>;
  def XS2MISC : ProcResource<1>;

  def XS2FMAC : ProcResource<4>;
  def XS2FMISC : ProcResource<2>;

  // Load/Store queues are ignored.
  def XS2LD : ProcResource<2>;
  def XS2ST : ProcResource<2>;
}
```

四个 ALU、两个 MDU，和 mca 的资源列表对得上：

```text
[0.0] - XS2ALU
[0.1] - XS2ALU
[0.2] - XS2ALU
[0.3] - XS2ALU
[1.0] - XS2FMAC
```

真正的特色是 load-to-ALU bypass：

```tablegen
class XS2LoadToALUBypass<SchedRead read>
    : ReadAdvance<read, 1, [WriteLDB, WriteLDH, WriteLDW, WriteLDD,
                            WriteAtomicW, WriteAtomicD,
                            WriteAtomicLDW, WriteAtomicLDD>];
```

实测 `ld=5`，但 `LD → ADD` 的边是 4。这个 1 拍就是它。

XiangShan 还有一行很实用的覆盖：

```tablegen
def : InstRW<[WriteIALU], (instrs COPY)>;
```

它把 `COPY` 的延迟钉成 1。对比 SiFive7：`COPY` 落到 `WriteIALU` 的 3 拍，所以 `min.ll` 的 `COPY → LD` 边在 `sifive-u74` 上是 `Data Latency=3`，在 `xiangshan-nanhu` 上是 1。这类「看不见的边」经常是同一段 IR 在两个核上排程不同的原因。

### 5. SpacemitX60：RV64 的默认模型

前面说过，`-mcpu=generic` 在 RV64 下会被规范化成 `generic-rv64`，而 `generic-rv64` 绑的是 `SpacemitX60Model`。所以**你什么都不写时，调度用的就是它**。

```tablegen
def SpacemitX60Model : SchedMachineModel {
  let IssueWidth        = 2; // dual-issue
  let MicroOpBufferSize = 0; // in-order
  let LoadLatency       = 3; // worse case: >= 3
  let MispredictPenalty = 9; // nine-stage
  let CompleteModel = 0;
  ...
}
```

资源是双发射的经典结构：

```tablegen
let BufferSize = 0 in {
  def SMX60_LS : ProcResource<2>;           // LSU 双发射
  def SMX60_IEUA : ProcResource<1>;
  def SMX60_IEUB : ProcResource<1>;
  def SMX60_IEU : ProcResGroup<[SMX60_IEUA, SMX60_IEUB]>;
  def SMX60_FP : ProcResource<1>;
  ...
}
```

延迟的取舍写在注释里：

```tablegen
// Integer multiplication
def : WriteRes<WriteIMul32, [SMX60_IEU]>  { let Latency = 3; }

// The latency of mul is 5, while in mulh, mulhsu, mulhu is 6
// Worst case latency is used
def : WriteRes<WriteIMul, [SMX60_IEU]>  { let Latency = 6; }
```

`mul` 实际 5 拍，但因为 `mulh` 系列是 6，模型统一取 6。**这就是「最坏值」原则**：模型宁可保守，也不要低估。

实测：`add=1`、`mul=6`、`div=20`、`ld=4`、`sd=4`。注意 `sd` 也是 4 拍，因为 store 在这颗核上走 LSU 且要等数据。

## 横向对比：同一组指令，五个模型

`llvm-mca -instruction-info` 实测（`-mattr=+m`，`generic-ooo` 额外 `+64bit`）：

| 指令 | Rocket | SiFive7(u74) | GenericOOO | XiangShan | SpacemitX60 |
|---|---|---|---|---|---|
| `add` | 1 | 3 | 1 | 1 | 1 |
| `mul` | 4 | 3 | 4 | 3 | 6 |
| `div` | 33 | 66 | 21 | 20 | 20 |
| `ld` | 2 | 3 | 4 | 5 | 4 |
| `sd` | 1 | 1 | 1 | 1 | 4 |

同一张表的 RThroughput：

| 指令 | Rocket | SiFive7 | GenericOOO | XiangShan | SpacemitX60 |
|---|---|---|---|---|---|
| `add` | 1.00 | 0.50 | 0.25 | 0.25 | 0.50 |
| `mul` | 1.00 | 1.00 | 1.00 | 0.50 | 0.50 |
| `div` | 33.00 | 65.00 | 21.00 | 10.00 | 20.00 |

几个可以直接拿去做面试答案的观察：

1. **SiFive7 的 `add` 延迟最大（3），但吞吐最好（0.50）**。顺序双发射核用更深的流水线换吞吐，单条依赖链反而更慢。
2. **XiangShan 的 `div` latency 20、RThroughput 10**，说明它至少部分流水化；Rocket 的 33/33 是完全阻塞。
3. **SpacemitX60 的 `sd` 是 4 拍**，其他模型都是 1。原因写在 `.td` 里：store 和 load 被放进同一个 `let Latency = 4 in { ... }` 块（`RISCVSchedSpacemitX60.td` 约 264–276 行），而别的模型让 store 走默认的 1 拍。
4. **`mul` 的数字从 3 到 6 都有**，所以「乘法几拍」这个问题没有跨核答案。

## 底层机制：模型是怎么被选中的

选择发生在 `RISCVSubtarget::initializeSubtargetDependencies()`：

```cpp
  if (CPU.empty() || CPU == "generic")
    CPU = Is64Bit ? "generic-rv64" : "generic-rv32";

  if (TuneCPU.empty())
    TuneCPU = CPU;
  if (TuneCPU == "generic")
    TuneCPU = Is64Bit ? "generic-rv64" : "generic-rv32";
  ...
  ParseSubtargetFeatures(CPU, TuneCPU, FS);
```

然后 `MCSubtargetInfo::InitMCProcessorInfo()` 决定用哪张模型：

```cpp
  if (!TuneCPU.empty())
    CPUSchedModel = &getSchedModelForCPU(TuneCPU);
  else
    CPUSchedModel = &MCSchedModel::Default;
```

所以严格说：**LLVM 24 的调度模型跟 tune CPU 走，而不是跟 `-mcpu` 走**。因为 `RISCVSubtarget` 总会把 `TuneCPU` 填上（默认等于 CPU），实际效果是「`-mcpu` 决定 feature 和默认 tune，`-mtune` 覆盖模型」。

一个容易踩的组合：

```bash
# 想用 sifive-u74 的模型，但手滑写成 tune-only 的 sifive-7-series
"$LLC" -mtriple=riscv64 -mcpu=sifive-7-series -mattr=+m min.ll -o -
# LLVM ERROR: RV64 target requires an RV64 CPU
```

`sifive-7-series` 是 `RISCVTuneProcessorModel`，不带 `Feature64Bit`；要用它必须补 `-mattr=+64bit`，但更常见的做法是直接用 `sifive-u74`。

### `UnsupportedFeatures` 到底管什么

每个模型都有一串 `UnsupportedFeatures`。它们不阻止你打开 feature，但会让「模型没有覆盖的指令」显式失效。用 mca 验证：

```bash
printf 'vsetvli t0, a0, e32, m1, ta, ma\nvadd.vv v1, v2, v3\n' > v.s
"$MCA" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+v -instruction-info v.s
# error: found an unsupported instruction in the input assembly sequence, ...
# note: instruction: 	vsetvli	t0, a0, e32, m1, ta, ma
```

Rocket 的 `UnsupportedFeatures` 里就有 `HasVInstructions`。同理，SiFive7 结尾有一整段：

```tablegen
  defm : UnsupportedSchedZabha;
  defm : UnsupportedSchedZbc;
  defm : UnsupportedSchedZbkb;
  ...
```

意思是「这些扩展我没建模」。打开它们不会报错，但这些指令会落到默认延迟——性能分析时看到「这条指令延迟 1」，要先怀疑模型没覆盖，而不是硬件真的这么快。

实测：在 `sifive-u74` 上打开 `+zbc`，`clmul` 会被选出来且没有任何 warning；但 DAG 里

```text
SU(2):   %2:gpr = CLMUL %0:gpr, %1:gpr
  Latency            : 1
  Successors:
    SU(3): Data Latency=1 Reg=%2
```

`CLMUL` 的延迟是 1，而它两个输入的 `COPY` 边是 3——前者就是「没建模，落默认值」的证据。

## 踩雷集锦

1. **以为 `sifive-u74`、`sifive-x280`、`sifive-x390` 用同一个模型**。它们共享 `SiFive7SchedMachineModel` 模板，但 VLEN 不同：u74 是 512，x390 是 1024X300，x160（RV32）是 128。资源名直接告诉你答案。

2. **用 `-mcpu=sifive-7-series` 跑 RV64**。它是 tune-only，不带 64 位 feature，直接报 `RV64 target requires an RV64 CPU`。要 tune 就写 `-mtune=sifive-7-series`，要 CPU 就写 `-mcpu=sifive-u74`。

3. **把 `CompleteModel = 0` 当成「模型完整度 0」**。它是布尔字段：`false` 表示「不保证覆盖所有指令」，于是 TableGen 跳过完整性检查。所有主流 RISC-V 模型都是 `false`，这是为了允许增量补模型。

4. **看到 `ld` 的 latency 和 `LoadLatency` 不一致就以为有 bug**。Rocket 是 `LoadLatency=3`、`ld=2`；SpacemitX60 是 `LoadLatency=3`、`ld=4`。这两个字段本来就不在同一个子系统里。

5. **拿 mca 的周期数当硬件实测**。mca 是按模型做的静态模拟，`Total Cycles` 是「这个模型认为的最优调度」。同一段代码在真机上可能因为 cache miss、频率、TLB 差出数倍。写报告时永远标注 `-mcpu` 和「模型周期」。

6. **在 RV32 上跑 `sifive-x390`**。它只有 RV64 版本，实测报 `LLVM ERROR: RV32 target requires an RV32 CPU`。先看 `RISCVProcessors.td` 里的 `Feature32Bit`/`Feature64Bit`，不要靠 CPU 名字猜位宽。

## 动手练习

### 练习 1：用资源名反推 VLEN

分别跑：

```bash
"$MCA" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -instruction-info ops.s
"$MCA" -mtriple=riscv64 -mcpu=sifive-x390 -mattr=+m -instruction-info ops.s
"$MCA" -mtriple=riscv32 -mcpu=sifive-x160 -mattr=+m -instruction-info rv32ops.s
```

记录三个资源名前缀，回到 `RISCVSchedSiFive7.td` 找到对应的 `SchedMachineModel<vlen>` 实例。验证：资源名前缀里的数字是否等于 `VLEN`？

### 练习 2：找一条「延迟相同、吞吐不同」的指令

在 SiFive7 上，`add` 和 `mul` 的 latency 都是 3，RThroughput 分别是 0.50 和 1.00。回到 `.td` 找到两条 `WriteRes`，解释差异来自哪个 `ProcResource`。再用 `-mcpu=xiangshan-nanhu` 跑同一组指令，看这个差异是否仍然存在。

### 练习 3：验证 `UnsupportedFeatures`

```bash
"$MCA" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m,+zbc -instruction-info ops.s
```

`+zbc` 是 SiFive7 的 `UnsupportedSchedZbc` 之一。如果 `ops.s` 里没有 `zbc` 指令，这个实验不会报错——请自己加一条 `clmul a0, a1, a2` 再试，观察 `lack-sched` 报错。

### 练习 4：给两个模型排同一段 IR

用 `ldmul.ll`（40 个 load + 40 个 mul），分别用 `rocket-rv64` 和 `sifive-u74` 编译，比较：

```bash
"$LLC" -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m -O2 ldmul.ll -o - | sed -n '/^ldmul:/,/^\.Lfunc_end/p' | head -30
```

观察两者的 load/mul 交错方式和栈访问条数。把差异归因到「模型延迟」还是「调度策略」，各写一句。

## 重点整理

- RISC-V 的调度模型都复用 `RISCVSchedule.td` 里的 `Write*`/`Read*` 符号，差异全在绑定的数字和 `ProcResource` 结构。
- Rocket：单发射、顺序、`BufferSize=0`、`mul=4`、`div=33`、`ld=2`，最小完整模型。
- SiFive7：双发射、A/B 两条整数流水、三个 VLEN 变体（128/512/1024）、`add` 和 `mul` latency 都是 3 但吞吐不同。
- GenericOOO：6 发射、192 项缓冲、资源组把 Branch/MulDiv/Int 混在一起，适合泛化的乱序目标。
- XiangShan：4 ALU + 2 MDU、256 项缓冲、load-to-ALU bypass 1 拍，是最值得逐行读的公开模型。
- SpacemitX60：RV64 默认模型（`generic` → `generic-rv64`），双发射、`mul=6`（取最坏）、`sd=4`。
- 模型选择跟 tune CPU 走；`-mtune` 会覆盖模型，`-mcpu` 决定 feature 与默认 tune。
- `UnsupportedFeatures` 不阻止开 feature，只让未建模指令在 mca 里报 `lack-sched`、在 llc 里落默认延迟。

## 自我检核

- [ ] 我能说出 `sifive-u74`、`sifive-x280`、`sifive-x390` 的模型和 VLEN 分别是什么。
- [ ] 我能解释为什么 SiFive7 的 `add` latency 是 3 但 RThroughput 是 0.50。
- [ ] 我知道 `generic` 在 RV64 下最终用哪个模型、为什么。
- [ ] 我能列出 Rocket 模型的四类资源和各自的 `BufferSize`。
- [ ] 我能用 mca 的资源名判断一个 CPU 用的是哪个模型变体。
- [ ] 我知道 XiangShan 的 `LD → ADD` 比 `LD → MUL` 少 1 拍的原因。
- [ ] 我能解释 `UnsupportedFeatures` 与 `lack-sched` 报错的关系。

## 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVProcessors.td`（全文，约 1100 行）**
  - **读哪里**：每个 `RISCVProcessorModel` 的第二个模板参数（模型）和 feature 列表；`SIFIVE_U74`、`SIFIVE_X280`、`GENERIC_OOO`、`XIangShan` 附近。
  - **和本章的关联**：CPU 名到模型的唯一映射表；也是「为什么某个 CPU 没有 64 位」的答案。

- **`llvm/lib/Target/RISCV/RISCVSchedRocket.td`（全文）**
  - **读哪里**：模型头、`RocketUnit*`、三档 `WriteRes`。
  - **和本章的关联**：最小模型，适合先建立「模型 = 资源 + 延迟绑定」的直觉。

- **`llvm/lib/Target/RISCV/RISCVSchedSiFive7.td`（约 1–400 行 + 1600–1680 行）**
  - **读哪里**：`SiFive7ProcResources`、`SiFive7WriteResBase`、三个 VLEN 模型与别名。
  - **和本章的关联**：多流水、多 VLEN 变体、`SchedAlias` 的完整例子。

- **`llvm/lib/Target/RISCV/RISCVSchedXiangShanNanHu.td`（全文，约 300 行）**
  - **读哪里**：模型头、`XS2*` 资源、`XS2LoadToALUBypass`、`InstRW<..., (instrs COPY)>`。
  - **和本章的关联**：读一遍就能理解「乱序模型为什么要建模 bypass」。

- **`llvm/lib/Target/RISCV/RISCVSchedSpacemitX60.td`（约 150–260 行）**
  - **读哪里**：模型头、`SMX60_*` 资源、IALU/IMul/IDiv 的 `WriteRes`。
  - **和本章的关联**：RV64 默认模型的真实数字，所有「不加 `-mcpu`」的实验都落在这里。

- **`llvm/lib/MC/MCSubtargetInfo.cpp`（`InitMCProcessorInfo`，约 239–250 行）**
  - **读哪里**：`CPUSchedModel` 从 `TuneCPU` 取值的那三行。
  - **和本章的关联**：解释 `-mtune` 为什么会改调度模型。

→ [下一章：`llvm-mca` 与模型边界](./14-llvm-mca.md)
