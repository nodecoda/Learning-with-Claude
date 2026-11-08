# Ch 21 — 调度模型与 llvm-mca：读懂一个 CPU 的 TableGen

> **目标**：把 `SchedWrite`/`SchedRead`、`WriteRes`、`ReadAdvance` 与一个真实 CPU 的 `SchedMachineModel` 对上；能用 `llvm-mca` 量化“这条指令在这个核上要几拍”，并能判断新增指令漏了 `SchedRW` 会怎样失真。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`。本章所有“实测”均来自该构建；未跑过的明确标“未实测”。

> **前置**：读完 Ch 03（流水线与 MIR）与 Ch 04（指令的 TableGen 建模）。本章默认你知道 `Sched<...>` 不是编码、`ADD` 来自 `ALU_rr`；不重复讲指令格式。

---
## 21.1 为什么维护者要读调度模型

调度模型不影响正确性，只影响**性能判断**。一条新指令只要编码、ISel、AsmPrinter 都对，程序就能跑；但如果它的 `SchedRW` 漏了，机器调度器和 `llvm-mca` 就失去了“这条指令占什么资源、几拍出结果”的事实。于是会出现几类问题：

| 症状 | 根因 | 先看哪里 |
|---|---|---|
| 新指令在 `llc` 里被排到很怪的位置 | `SchedRW` 为空或写错，调度器按默认 1 拍处理 | 指令 `def` 的 `Sched<...>` |
| `llvm-mca` 报 `unable to resolve scheduling class` | 该指令没有可解析的 sched class | `RISCVSchedule*.td` 与 CPU 模型的 `WriteRes` |
| 同一段代码在 `-mcpu=a` / `-mcpu=b` 上 IPC 差很多 | 两个 `SchedMachineModel` 的 `IssueWidth`、资源数、`Latency` 不同 | `RISCVSched*.td` |
| 只换 `-mtune` 后指令序变了，但 ISA 没变 | `-mtune` 只换调度模型/tune features，不换 ISA | `RISCVProcessors.td`、`MCSubtargetInfo.cpp` |

本树 `llvm/lib/Target/RISCV` 有 **28 个**：5 个是公共定义（`RISCVSchedule.td`、`RISCVScheduleV.td`、`RISCVScheduleZb.td`、`RISCVScheduleXSf.td`、`RISCVScheduleZvk.td`），23 个是具体 CPU 模型 `RISCVSched*.td`。`RISCVSchedule.td` 只有 546 行，却定义了 **120 个 `SchedWrite`、111 个 `SchedRead`**；真正的时序数字分散在各 CPU 文件里。

一个必须知道的事实：RISC-V 的 CPU 模型普遍写 `CompleteModel = false`（`RISCVSchedRocket.td:19`、`RISCVSchedSiFiveP600.td:311`）。这意味着**新增指令漏了 `SchedRW`，TableGen 通常不会让构建失败**；错误会推迟到调度或 `llvm-mca` 才暴露。这正是维护者要主动读模型的原因。

---
## 21.2 先建立直觉：一条指令的调度类怎么被选中

以 `ADD` 为例，完整链路是：

```text
def ADD : ALU_rr<...>, Sched<[WriteIALU, ReadIALU, ReadIALU]>
                              │
                              │  SchedRW = [1 个 def, 2 个 use]
                              ▼
RISCVSchedule.td:  def WriteIALU : SchedWrite;   // 只有“名字”，没有时序
                   def ReadIALU  : SchedRead;
                              │
                              │  在某个 SchedModel 作用域内绑定
                              ▼
RISCVSchedRocket.td:
  let SchedModel = RocketModel in
  def : WriteRes<WriteIALU, [RocketUnitALU]>;    // 资源 + 默认 Latency=1
  def : ReadAdvance<ReadIALU, 0>;                // 读操作不额外提前/延后
                              │
                              │  TableGen 生成 sched class
                              ▼
llvm-mca:  Latency: 1, RThroughput: 1.00
           Resource pressure: RocketUnitALU 1.00 / instruction
```

只关心写侧时常把 `ADD` 简写成 `Sched<[WriteIALU]>`；完整 `ADD` 仍要列两个 `ReadIALU`。

`Sched<...>` 里的列表按“先 def、后 use”组织；同一类内部按操作数顺序一一对应。`ADD` 有 1 个显式 def、2 个显式 use，所以是 `WriteIALU, ReadIALU, ReadIALU`。`ADDI` 有 1 个 def、1 个 use，所以是 `WriteIALU, ReadIALU`（`RISCVInstrInfo.td:786`）。`LD` 是 `WriteLDD, ReadMemBase`（`RISCVInstrInfo.td:1002`）。

| 概念 | 定义位置 | 一句话含义 | 写错的后果 |
|---|---|---|---|
| `SchedReadWrite` | `TargetSchedule.td:208` | `SchedWrite`/`SchedRead` 的共同基类 | 只影响类型，不直接产生时序 |
| `SchedWrite` | `TargetSchedule.td:225` | 描述一个 def 的“写结果” | 漏写 → 该 def 没有 sched class |
| `SchedRead` | `TargetSchedule.td:229` | 描述一个 use 的“读操作数” | 漏写 → 依赖边丢失或按默认读 |
| `WriteRes` | `TargetSchedule.td:310` | 在某个 `SchedModel` 里把已有 `SchedWrite` 绑到 `ProcResource` | 绑错资源 → 资源压力算错 |
| `SchedWriteRes` | `TargetSchedule.td:318` | 直接定义“新 `SchedWrite` + 资源”，常配 `InstRW`/`SchedAlias`/`SchedWriteVariant` | 与 `WriteRes` 混用 → 作用域/覆盖关系混乱 |
| `Latency` | `ProcWriteResources` | 结果从发射到可被消费者读的拍数 | 写小 → mca 过于乐观 |
| `ReadAdvance` | `TargetSchedule.td:347` | 从生产者 `Latency` 里减去的拍数（bypass） | 写大 → 真实依赖被隐藏；写小 → 虚假停顿 |
| `ReleaseAtCycles` / ResourceCycles | `ProcWriteResources` | 每个资源占用多少拍 | 写错 → 吞吐/资源压力失真 |

`NumMicroOps` 默认是 1，表示这条指令占 1 个 dispatch/ROB 槽。RISC-V 里大多数 `WriteIALU` 都是默认 1；只有少数指令显式写 2，例如 SiFive7 的 `WriteSFB`（`RISCVSchedSiFive7.td:328`）和 P600 的 `P600WriteCMOV`（`RISCVSchedSiFiveP600.td:370-373`）。

---
## 21.3 `RISCVSchedule.td` 的类层次

先纠正一个常见说法：**本树没有 `RISCVSchedModel` 这个 TableGen 记录**。执行 `grep -rn "RISCVSchedModel" llvm/lib/Target/RISCV` 没有输出。真正的三层是：

```text
SchedMachineModel                 // TargetSchedule.td:76，资源/宽度/延迟的容器
  └─ RocketModel / SiFiveP600Model / SiFive7SchedMachineModel<vlen> ...
ProcessorModel                    // TargetSubtarget.td:172，把模型绑到 CPU 名 + feature
  └─ RISCVProcessorModel          // RISCVProcessors.td:76
  └─ RISCVTuneProcessorModel      // RISCVProcessors.td:90，只用于 -mtune
```

`RISCVProcessorModel` 用于 `-mcpu`（带 ISA feature），`RISCVTuneProcessorModel` 用于 `-mtune`（只带 tune feature）。例如 `rocket-rv64` 是前者（`RISCVProcessors.td:150`），`rocket` 是后者（`:156`）；`sifive-u74` 是前者（`:280`），`sifive-7-series` 是后者（`:162`）。

`RISCVSchedule.td` 内部可以按四块读：

| 块 | 行号 | 内容 |
|---|---|---|
| 标量写资源 | 16–149 | `WriteIALU`、`WriteIALU32`、`WriteIMul`、`WriteIDiv`、`WriteIRem`、`WriteLDB`…`WriteSTD`、`WriteFAdd16..128`、`WriteFMul*`、`WriteFMA*`、`WriteFDiv*`、`WriteFSqrt*` |
| 标量读资源 | 150–264 | `ReadIALU`、`ReadIALU32`、`ReadIMul`、`ReadIDiv`、`ReadIRem`、`ReadMemBase`、`ReadStoreData`、`ReadFAdd*`… |
| `UnsupportedSched*` | 267–525、538–545 | 每个 multiclass 用 `let Unsupported = true` + `WriteRes<..., []>` 声明“这个核不支持这类指令” |
| 扩展 include | 532–535 | `RISCVScheduleZb.td`、`RISCVScheduleV.td`、`RISCVScheduleXSf.td`、`RISCVScheduleZvk.td` |

几个真实条目：

```tablegen
def WriteIALU       : SchedWrite;    // RISCVSchedule.td:16，32/64 位整数 ALU
def WriteIDiv       : SchedWrite;    // :22，32/64 位除法
def WriteFAdd32     : SchedWrite;    // :50，单精度浮点加减
def ReadIALU        : SchedRead;     // :161
def ReadIDiv        : SchedRead;     // :167
```

注意：**本树没有字面上的 `WriteVALU`**。向量写资源由 `RISCVScheduleV.td` 的 multiclass 生成：

```tablegen
defm "" : LMULSchedWrites<"WriteVIALUV">;   // RISCVScheduleV.td:389
defm "" : LMULSchedWrites<"WriteVIALUX">;   // :390
defm "" : LMULSchedWrites<"WriteVIALUI">;   // :391
defm "" : LMULSEWSchedWritesF<"WriteVFALUV">; // :466
defm "" : LMULSEWSchedWritesF<"WriteVFALUF">; // :467
```

每个 `WriteVIALUV` 再按 LMUL 生成 `_M1`、`_M2`、`_M8`、`_MF2`… 等记录；所以你在 mca 里看到的是 `WriteVIALUV_M1_E32` 这类名字，而不是一个笼统的 `WriteVALU`。同理，Zb 的写资源在 `RISCVScheduleZb.td:12-46`（`WriteSHXADD`、`WriteCLZ`、`WriteCPOP`、`WriteBEXT` 等），由 `RISCVSchedule.td:532` include 进来。

`UnsupportedSched*` 是维护新扩展时最容易漏的地方。比如 Rocket 明确写了 `defm : UnsupportedSchedV;`、`UnsupportedSchedZba;`、`UnsupportedSchedZbb;`（`RISCVSchedRocket.td:253-266`）。它的作用是告诉 TableGen：“这个 CPU 不支持这些 sched class，别拿它们做完整性检查。”新增扩展时，如果只加了指令和 `WriteRes`，却没有给不支持的 CPU 加对应的 `UnsupportedSched<Ext>`，模型检查就会不完整。

---
## 21.4 代表 CPU：Rocket、SiFive7 与 P600

一个常见误解是把 SiFive7 当成乱序核；源码并不是这样：`SiFive7SchedMachineModel` 明确写 `MicroOpBufferSize = 0; // ... in-order`（`RISCVSchedSiFive7.td:1609`），`IssueWidth = 2`（`:1610`）。SiFive7/U74 是 **2 发射顺序核**。真正的乱序核用 P600/P670：`MicroOpBufferSize = 192`、`IssueWidth = 4`（`RISCVSchedSiFiveP600.td:303-304`）。下面三方对照。

Rocket（`RISCVSchedRocket.td:14-19`）：

```tablegen
def RocketModel : SchedMachineModel {
  let MicroOpBufferSize = 0; // Rocket is in-order.
  let IssueWidth = 1;        // 1 micro-op is dispatched per cycle.
  let LoadLatency = 3;
  let MispredictPenalty = 3;
  let CompleteModel = false;
}
```

同一个 `WriteIALU` 的绑定：

```tablegen
// Rocket: 1 个 ALU，未写 Latency → 默认 1；ReleaseAtCycles 为空 → 每资源 1 拍
def : WriteRes<WriteIALU, [RocketUnitALU]>;          // :57

// SiFive7: 2 条 ALU 流水 A/B 的资源组，Latency 3
let Latency = 3 in {
  def : WriteRes<WriteIALU, [PipeAB]>;               // :335
}

// P600: 4 条 IEXQ 组成的 IntArith 资源组，未写 Latency → 默认 1
def : WriteRes<WriteIALU, [SiFiveP600IntArith]>;     // :358
```

| 项目 | Rocket | SiFive7（U74） | P600（P670） |
|---|---|---|---|
| 类型 | 顺序、单发射 | 顺序、2 发射 | 乱序、4 发射 |
| `IssueWidth` | 1 | 2 | 4 |
| `MicroOpBufferSize` | 0 | 0 | 192 |
| `LoadLatency` | 3 | 3 | 4 |
| `MispredictPenalty` | 3 | 3 | 9 |
| `WriteIALU` 资源 | `RocketUnitALU`（1） | `PipeAB`（A+B，2） | `IntArith`（4×IEXQ） |
| `WriteIALU` `Latency` | 默认 1 | 3 | 默认 1 |
| `WriteIALU` `NumMicroOps` | 默认 1 | 默认 1 | 默认 1 |
| mca `add` Latency / RThroughput | 1 / 1.00 | 3 / 0.50 | 1 / 0.25 |
| `div` Latency / RThroughput | 33 / 33.00 | 66 / 65.00 | 35 / 34.00 |

`div` 的差异来自 `ReleaseAtCycles`，不是 `Latency` 一个数：

```tablegen
// Rocket: RISCVSchedRocket.td:75-78
def : WriteRes<WriteIDiv, [RocketUnitIDiv]> {
  let Latency = 33;
  let ReleaseAtCycles = [33];
}

// SiFive7: RISCVSchedSiFive7.td:351-354
def SiFive7RV64IDivRemSchedWriteRes : SchedWriteRes<[PipeB, IDiv]> {
  let Latency = 66;
  let ReleaseAtCycles = [1, 65];   // PipeB 占 1 拍，IDiv 占 65 拍
}

// P600: RISCVSchedSiFiveP600.td:386-389
def : WriteRes<WriteIDiv, [SiFiveP600MulI2F, SiFiveP600Div]> {
  let Latency = 35;
  let ReleaseAtCycles = [1, 34];
}
```

这就是“为什么不能只看 `Latency`”：Rocket 的 `div` 33 拍才释放除法器，吞吐就是 33 拍/条；SiFive7 的 `IDiv` 资源占 65 拍，但 `PipeB` 只占 1 拍，所以 mca 报 `RThroughput 65.00`，同时 `PipeB` 压力是 1.00/条。

---
## 21.5 `-mcpu` / `-mtune`：换 ISA 还是只换时钟

`llc -mcpu=help` 在本树列出 **60 个** RISC-V CPU。片段（实测）：

```text
  generic             - Select the generic processor.
  generic-ooo         - Select the generic-ooo processor.
  rocket              - Select the rocket processor.
  rocket-rv64         - Select the rocket-rv64 processor.
  sifive-7-series     - Select the sifive-7-series processor.
  sifive-p670         - Select the sifive-p670 processor.
  sifive-u74          - Select the sifive-u74 processor.
  ...
```

语义：

- `-mcpu=rocket-rv64` 同时选 **ISA feature** 和 **sched model**；`-mcpu=sifive-u74` 会带 `M/A/F/D/C`。
- `-mtune=sifive-u74` 只选 **tune CPU / sched model / tune features**，不改 ISA。
- 不写 `-mtune` 时，`TuneCPU = CPU`（`RISCVSubtarget.cpp:64-65`）；`TuneCPU` 再决定 `CPUSchedModel`（`MCSubtargetInfo.cpp:245-248`）。

验证方法：同一段 `sdiv i64`，只换参数，看 `.attribute 5` 和最终指令：

```bash
cat > /tmp/f.ll <<'EOF'
define i64 @sdiv64(i64 %a, i64 %b) {
  %r = sdiv i64 %a, %b
  ret i64 %r
}
EOF
$T/bin/llc -mtriple=riscv64 -mcpu=rocket-rv64 -O2 /tmp/f.ll -o -
$T/bin/llc -mtriple=riscv64 -mcpu=rocket-rv64 -mtune=sifive-7-series -O2 /tmp/f.ll -o -
$T/bin/llc -mtriple=riscv64 -mcpu=sifive-u74 -O2 /tmp/f.ll -o -
```

实测：`-mcpu=rocket-rv64` 与 `-mtune=sifive-7-series` 的 `.attribute 5` 都是 `"rv64i2p1_zicsr2p0_zifencei2p0"`，尾部都是 `tail __divdi3`；只有 `-mcpu=sifive-u74` 才出现 `m2p0` 并生成 `div a0, a0, a1`。

结论：`-mtune` 没有把 `M` 加进来，所以仍然调 `__divdi3`；它只改变后续调度/成本判断。`-mtune` 还可能改变 `TuneInfo`（对齐、prefetch 距离等）和 tune features（如 `TuneNoDefaultUnroll`、`TunePostRAScheduler`），所以**代码正确性不变，指令序/选择可能变**。

`-mattr` 与 `-mcpu` 冲突时，`-mattr` 后应用、优先级更高：

```bash
$T/bin/llc -mtriple=riscv64 -mcpu=sifive-u74 -mattr=-m -O2 /tmp/f.ll -o -
```

实测 `.attribute 5` 变成 `rv64i2p1_a2p1_f2p2_d2p2_c2p0_...`（`m2p0` 消失），代码回到 `tail __divdi3`。没有报错，只有静默覆盖。

最后，`llvm-mca` **没有 `-mtune`**：

```text
$ llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -mtune=sifive-u74 ...
llvm-mca: Unknown command line argument '-mtune=sifive-u74'.
```

要测 `-mtune` 的调度差异，用 `llc -print-after=machine-scheduler` 或 `-stop-after=machine-scheduler`，不要指望 mca。

---
## 21.6 `RISCVMachineScheduler`：自定义策略只在 `vsetvli` 上动手

`RISCVMachineScheduler.h:24-25`：

```cpp
class RISCVPreRAMachineSchedStrategy : public GenericScheduler {
  const RISCVSubtarget *ST;
  RISCV::RISCVVSETVLIInfoAnalysis VIA;
  RISCV::VSETVLIInfo TopInfo;
  RISCV::VSETVLIInfo BottomInfo;
```

它继承 `GenericScheduler`，只重写四个钩子：`tryCandidate`、`enterMBB`、`leaveMBB`、`schedNode`。一般资源/延迟比较仍然走 `GenericScheduler::tryCandidate` 的逻辑；RISC-V 附加的启发式在 `RISCVMachineScheduler.cpp:169-180`：

```cpp
if (ST->enableVsetvliSchedHeuristic() &&
    tryVSETVLIInfo(getVSETVLIInfo(TryCand.SU->getInstr()),
                   getVSETVLIInfo(Cand.SU->getInstr()), TryCand, Cand,
                   Cluster))
  return TryCand.Reason != NoCand;
```

它比较的是候选指令的 `VSETVLIInfo` 与当前 top/bottom 边界的兼容性，目的是减少 `vsetvli` 的重复配置。`schedNode` 在每次调度后更新 `TopInfo`/`BottomInfo`（`:195-210`），`enterMBB`/`leaveMBB` 负责清空。调试类型是 `riscv-prera-sched-strategy`。

接入点要精确说：

- `RISCVTargetMachine.cpp:215` 重写的是 **`createMachineScheduler`**，不是 `addMachineScheduler`。它创建 `ScheduleDAGMILive`，注入 `RISCVPreRAMachineSchedStrategy`，再按需加 macro-fusion、load/store clustering、vector-mask mutation。
- 真正的 pass 插入在通用 `TargetPassConfig::addOptimizedRegAlloc()`：`TargetPassConfig.cpp:1368` 的 `addPass(&MachineSchedulerID)`，位置在 PHI elimination、two-address、coalescer 之后，寄存器分配之前。
- `RISCVSubtarget.h:170` 的 `enableMachineScheduler()` 返回 `true`，所以 RISC-V 默认启用 MachineScheduler。
- PostRA 侧：`createPostMachineScheduler` 在 `RISCVTargetMachine.cpp:239`；`RISCVPassConfig` 在 `:325` 把 `PostRASchedulerID` 替换成 `PostMachineSchedulerID`；`enablePostRAScheduler()` 返回 `UsePostRAScheduler`（`RISCVSubtarget.h:172`）。

真实 pipeline 片段（`-O2 -debug-pass=Structure`）：

```text
-machine-sink
-machinedomtree
-machine-scheduler          ← PreRA，RISCVMachineScheduler
-lazy-machine-block-freq
-machine-opt-remark-emitter
-machine-cp
...
-postmisched                ← PostRA，RISC-V 替换了 legacy PostRAScheduler
```

---
## 21.7 `llvm-mca` 实操：七个必跑实验

以下全部在本机 `builds/ct-instr-trunk` 跑过。输入文件都用同一份 10 条独立 `add`：

```bash
cat > /tmp/alu10.s <<'EOF'
add a0, a0, a1
add a2, a2, a3
add a4, a4, a5
add a6, a6, a7
add s0, s0, s1
add s2, s2, s3
add s4, s4, s5
add s6, s6, s7
add s8, s8, s9
add s10, s10, s11
EOF
```

### 实验 1：Rocket 上的 10 条 ALU

```bash
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -iterations=100 /tmp/alu10.s
```

实测 Summary：

```text
Iterations:        100
Instructions:      1000
Total Cycles:      1001
Total uOps:        1000

Dispatch Width:    1
uOps Per Cycle:    1.00
IPC:               1.00
Block RThroughput: 10.0
```

单发射、单 ALU，10 条独立 ALU 刚好 10 拍一轮；资源压力表只有 `RocketUnitALU 10.00`，`uOps Per Cycle` 被 `IssueWidth=1` 卡住。

### 实验 2：同一段换 `sifive-u74` / `sifive-p670`

```bash
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 /tmp/alu10.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-p670 /tmp/alu10.s
```

实测对比：

| CPU | Dispatch Width | Total Cycles | IPC | Block RThroughput | `add` Latency | `add` RThroughput | ALU 资源压力 |
|---|---|---|---|---|---|---|---|
| `rocket-rv64` | 1 | 1001 | 1.00 | 10.0 | 1 | 1.00 | `RocketUnitALU` 10.00 |
| `sifive-u74` | 2 | 503 | 1.99 | 5.0 | 3 | 0.50 | `PipeA` 5.00 + `PipeB` 5.00（前缀见下） |
| `sifive-p670` | 4 | 253 | 3.95 | 2.5 | 1 | 0.25 | 4×`IEXQ` 各 2.50 |

U74 的 `add` 指令行是 `1 3 0.50`：`Latency=3`，但两条 ALU 流水让它每 2 拍就能收一条。U74 直接复用 `SiFive7Model`（`RISCVProcessors.td:280`），而本树把它实例化成 VLEN=512，所以 mca 打印的资源名是 `VLEN512SiFive7PipeA`/`VLEN512SiFive7PipeB`；后面的 `IDiv`、`PipeB` 同理带这个前缀。P670 的 `add` 是 `1 1 0.25`：4 条 IEXQ，每 4 拍收 4 条，mca 把 10 条均摊成 2.5 拍/资源。

### 实验 3：load-use 依赖链（`ld → add`）

```bash
cat > /tmp/loaduse.s <<'EOF'
ld a0, 0(a1)
add a0, a0, a2
add a3, a0, a4
add a5, a3, a6
EOF
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 /tmp/loaduse.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-p670 /tmp/loaduse.s
```

实测：

| CPU | `ld` Latency | `add` Latency | Total Cycles | IPC | Block RThroughput |
|---|---|---|---|---|---|
| Rocket | 2 | 1 | 501 | 0.80 | 4.0 |
| P670 | 4 | 1 | 109 | 3.67 | 1.0 |

Rocket 的 `-timeline`（`-iterations=2`）：

```text
Timeline view:
Index     0123456789

[0,0]     DeE  .    .   ld  a0, 0(a1)
[0,1]     . DE .    .   add a0, a0, a2
[0,2]     .  DE.    .   add a3, a0, a4
[0,3]     .   DE    .   add a5, a3, a6
[1,0]     .    DeE  .   ld  a0, 0(a1)
```

`D`=dispatch、`e`=executing、`E`=executed；每轮 5 拍：2 拍 load + 3 条串行 add。P670 虽然 `ld` 的 `Latency` 更大（4），但乱序窗口把下一轮的 load 提前发射，400 条指令只用 109 拍，`IPC 3.67`。这就是“顺序核看依赖链，乱序核看窗口”的差别。

### 实验 4：`div` 的长延迟吞吐

```bash
cat > /tmp/div.s <<'EOF'
div a0, a1, a2
div a3, a4, a5
div a6, a7, s0
div s1, s2, s3
EOF
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+m /tmp/div.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-u74 /tmp/div.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-p670 /tmp/div.s
```

| CPU | `div` Latency | `div` RThroughput | Total Cycles（4 条×100 次） | IPC |
|---|---|---|---|---|
| Rocket +m | 33 | 33.00 | 13201 | 0.03 |
| U74 | 66 | 65.00 | 26002 | 0.02 |
| P670 | 35 | 34.00 | 13604 | 0.03 |

Rocket 的资源压力是 `RocketUnitIDiv 132.00`（4 条×33 拍）；U74 是 `IDiv 260.00`、`PipeB 4.00`。注意 U74 的 `Latency=66` 但 `RThroughput=65.00`，因为 `ReleaseAtCycles=[1,65]` 里 `PipeB` 只占 1 拍；这两个数不要混。

### 实验 5：`-iterations`、`-timeline` 与 `-bottleneck-analysis`

`-iterations=100` 是默认值，也可以显式写；`-timeline` 在本树可用。用实验 3 的 load-use：

```bash
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -iterations=2 -timeline /tmp/loaduse.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -bottleneck-analysis /tmp/div.s -mattr=+m
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=sifive-p670 -bottleneck-analysis /tmp/loaduse.s
```

实测：Rocket 的 `-bottleneck-analysis` 直接警告：

```text
warning: bottleneck analysis is not supported for in-order CPU 'rocket-rv64'.
```

P670 则直接给出 `No resource or data dependency bottlenecks discovered.`。所以：顺序核用 `-timeline` + `Average Wait times`；乱序核才用 `-bottleneck-analysis`。`-timeline` 的图例是 `D` dispatch、`e` executing、`E` executed、`R` retired、`=` 已 dispatch 等待执行、`-` 已执行等待 retire。

### 实验 6：缺 `SchedRW` 的指令

`FENCE`、`FENCE_I`、`SFENCE_VMA` 等在 `RISCVInstrInfo.td` 里写的是 `Sched<[]>`（空列表，例如 `:941`、`:956`、`:1086`）。用 mca 跑：

```bash
printf 'sfence.vma\nfence.i\nadd a0, a0, a1\n' > /tmp/nosched.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 /tmp/nosched.s
```

实测报错：

```text
error: unable to resolve scheduling class for write variant., use -skip-unsupported-instructions=lack-sched to ignore these on the input.
note: instruction: 	sfence.vma
```

加 `-skip-unsupported-instructions=lack-sched` 后，mca 会警告并丢掉这两条，只统计 `add`（`Instructions: 100`、`Total Cycles: 101`、`IPC: 0.99`）。注意：这个构建不是“把缺 sched 的指令当 0 延迟”，而是**直接报错**；跳过之后它等于不存在，统计更失真。新增指令如果漏了 `Sched<>`，就会落到这个错误路径。

### 实验 7：压缩指令的 `SchedRW`

```bash
printf 'c.addi a0, 1\nc.add a0, a1\n' > /tmp/comp.s
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 -mattr=+c -instruction-tables=full /tmp/comp.s
```

实测关键行：

```text
[1]  [2]  [3]  [4]  [5]  [6]  [7]  [8]              [9]        Instructions:
 1    1    1.00                    1   RocketUnitALU  C_ADDI     addi a0, a0, 1
 1    1    1.00                    1   RocketUnitALU  C_ADD      add  a0, a0, a1
```

压缩指令**不会自动继承**同名非压缩指令的 `SchedRW`。`C_ADDI` 在 `RISCVInstrInfoC.td:324-327` 自己写 `Sched<[WriteIALU, ReadIALU]>`；`C_ADD` 在 `:468-472` 自己写 `Sched<[WriteIALU, ReadIALU, ReadIALU]>`。它们只是复用同一套 `WriteIALU`/`ReadIALU` 记录，所以资源和延迟看起来“继承”了，但 TableGen 层面是显式写的。新增压缩指令时，这条 `Sched<...>` 不能省。

**未实测**：真机性能相关性（本机没有 RISC-V 板卡）与“故意改错 `ReadAdvance` 再重编”的对照；后者要改 `RISCVSchedSiFive7.td` 并重建，本章只读源码，未做该实验。

---
## 21.8 源码落点

| 文件 | 行数/行号 | 读什么 |
|---|---|---|
| `llvm/lib/Target/RISCV/RISCVSchedule.td` | 546 | 标量 `SchedWrite`/`SchedRead`、`UnsupportedSched*`、扩展 include |
| `llvm/lib/Target/RISCV/RISCVScheduleV.td` | 1201 | 向量 `WriteVIALU*`/`WriteVFALU*` 的 LMUL/SEW 生成 |
| `llvm/lib/Target/RISCV/RISCVScheduleZb.td` | 169 | Zb 的 `WriteSHXADD`、`WriteCLZ`、`WriteCPOP` 等 |
| `llvm/lib/Target/RISCV/RISCVSchedRocket.td` | 267 | 顺序单发射模型；`RocketModel:14`、`WriteIALU:57`、`WriteIDiv:75` |
| `llvm/lib/Target/RISCV/RISCVSchedSiFive7.td` | 1669 | 2 发射顺序核；模型 `:1608`、`WriteIALU:335`、`IDiv:351` |
| `llvm/lib/Target/RISCV/RISCVSchedSiFiveP600.td` | 1502 | 4 发射乱序核；模型 `:302`、`WriteIALU:358`、`IDiv:386` |
| `llvm/lib/Target/RISCV/RISCVProcessors.td` | 1159 | CPU 名与模型绑定；`RISCVProcessorModel:76`、`ROCKET_RV64:150`、`SIFIVE_U74:280`、`SIFIVE_P670:559` |
| `llvm/lib/Target/RISCV/RISCVMachineScheduler.cpp` | 216 | `RISCVPreRAMachineSchedStrategy`、`tryVSETVLIInfo`、`schedNode` |
| `llvm/lib/Target/RISCV/RISCVTargetMachine.cpp` | 617 | `createMachineScheduler:215`、`createPostMachineScheduler:239`、`:325` 替换 PostRA |
| `llvm/lib/Target/RISCV/RISCVSubtarget.h` | 489 | `enableMachineScheduler():170`、`enablePostRAScheduler():172` |
| `llvm/lib/CodeGen/TargetPassConfig.cpp` | 1368 | `addPass(&MachineSchedulerID)` 的真实位置 |
| `llvm/lib/MC/MCSubtargetInfo.cpp` | 245–248 | `TuneCPU` 如何选 `CPUSchedModel` |
| `llvm/include/llvm/Target/TargetSchedule.td` | 76/208/225/229/310/318/347 | `SchedMachineModel`、`SchedReadWrite`、`WriteRes`、`ReadAdvance` 的通用定义 |

---
## 21.9 可跑实验

把 21.7 串成一个脚本：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
SRC=/home/ubuntu/llvm/trees/ct-instr-trunk

for cpu in rocket-rv64 sifive-u74 sifive-p670; do
  echo "== $cpu =="
  $T/bin/llvm-mca -mtriple=riscv64 -mcpu=$cpu /tmp/alu10.s \
    | grep -E "Total Cycles|Dispatch Width|IPC|Block RThroughput"
done

$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 \
  -iterations=2 -timeline /tmp/loaduse.s | tail -20
$T/bin/llvm-mca -mtriple=riscv64 -mcpu=rocket-rv64 /tmp/nosched.s

cd "$SRC"
$T/bin/llvm-lit -sv -j16 llvm/test/tools/llvm-mca/RISCV/
```

第 4 条实测：`357 tests`，`Passed: 357 (100.00%)`。这些测试用 `-instruction-tables=full` 固定了 `C_ADDI`、`ADDI`、`LD` 等指令的资源名和 `RThroughput`，是改 `RISCVSched*.td` 后最便宜的安全网。

再补两个 `llc` 观察点：`-stop-after=machine-scheduler` 看调度后的 MIR；`-debug-pass=Structure` 确认 pipeline 里有 `-machine-scheduler` 和 `-postmisched`。

## 21.10 踩雷集锦

1. **`-mcpu` 与 `-mattr` 冲突不会报错。** `-mattr` 后应用，直接覆盖 CPU 的 feature。`-mcpu=sifive-u74 -mattr=-m` 会把 `m2p0` 从 `.attribute 5` 里去掉并退回 `__divdi3`。复现性能问题时先确认命令行有没有 `-mattr`；mca 的 `-mcpu` 还必须存在于本树，否则报 `unrecognized processor`。

2. **`Sched<[]>` 不是“没有调度信息”，而是“空的 sched class”。** `FENCE`/`SFENCE_VMA` 用空列表表达“这类指令不建模”；mca 会报 `unable to resolve scheduling class`。如果一条普通 ALU 指令误写成 `Sched<[]>`，它也会落到同一个错误。

3. **漏写 `Sched<>` 不会让 RISC-V 构建失败。** 因为 `CompleteModel = false`。真正兜底的是 `llvm-mca` 和 `llvm/test/tools/llvm-mca/RISCV/`。新增指令时至少用两个 CPU 跑一次 mca。

4. **`-mtune` 不改 ISA，但会改调度。** 它只换 `TuneCPU`/`CPUSchedModel`/tune features；代码仍必须能在 `-mcpu` 的 ISA 上运行。`-mcpu=rocket-rv64 -mtune=sifive-u74` 是“Rocket 的 ISA，SiFive7 的时序”，不是“U74 的代码”。

5. **`llvm-mca` 默认假设完美前端。** 文档明确说指令已经解码并放进队列，取指/解码不建模，分支预测也不建模（`llvm/docs/CommandGuide/llvm-mca.md:948-953`）。所以 mca 的 IPC 是后端上限，不是实际程序的 IPC；前端瓶颈、ICache、分支预测失败都要另测。

6. **`ReadAdvance` 写反会制造虚假/消失的依赖。** 正数表示“消费者可以提前读”，从生产者 `Latency` 里减。写大 → mca 看不到真实 stall，报告过于乐观；写小 → 消费者多等，报告虚假停顿。SiFive7 用 `ReadAdvance<ReadIALU, 2, [WriteIALU, ...]>` 建模 GPR bypass（`RISCVSchedSiFive7.td:250-263`），但 `ReadMemBase` 是 0（`:1270`）；把这两个抄反，load-use 的结论就全错。

7. **压缩指令的 `SchedRW` 不会自动继承。** `C_ADDI`、`C_ADD` 都显式写 `Sched<...>`（`RISCVInstrInfoC.td:324-327`、`:468-472`）。它们复用 `WriteIALU`/`ReadIALU`，所以 mca 结果看起来和非压缩版一致；但新增压缩指令时如果只写编码、不写 `Sched`，mca 仍然会报错。

8. **`LoadLatency` 不等于 load 指令的 `Latency`。** Rocket 的模型写 `LoadLatency = 3`，但 `WriteLDD` 的 `WriteRes` 是 `Latency = 2`（`RISCVSchedRocket.td:103-105`）；mca 报的 `ld` latency 是 2。`LoadLatency` 主要供 target hook 做成本判断（`sched-model-load-latency.ll` 就是测这个）。改 `LoadLatency` 前先确认你要影响的是哪条路径。

---
## 21.11 本章重点整理

- 调度模型是**性能契约**，不是正确性契约；漏 `SchedRW` 不会让程序算错，但会让 mca/调度器的结论失真。
- `Sched<[Write..., Read...]>` 是 per-operand 列表：每个 def 一个 `SchedWrite`，每个 use 一个 `SchedRead`；`SchedWrite`/`SchedRead` 都继承 `SchedReadWrite`。
- `WriteRes` 在具体 `SchedModel` 里绑定资源和延迟；`SchedWriteRes` 直接定义新写类型，常配 `InstRW`/`SchedAlias`/`SchedWriteVariant`。
- `Latency` 是结果可用时间；`ReleaseAtCycles` 是资源占用；`NumMicroOps` 是 dispatch/ROB 槽；`ReadAdvance` 是 bypass 提前量。四者不要互相替代。
- 本树没有 `RISCVSchedModel`；基类是 `SchedMachineModel` + `RISCVProcessorModel`/`RISCVTuneProcessorModel`。
- Rocket 是 1 发射顺序核；SiFive7/U74 是 2 发射顺序核；P600/P670 是 4 发射乱序核。`IssueWidth`、`MicroOpBufferSize`、资源组共同决定 mca 的 IPC/RThroughput。
- `-mcpu` 选 ISA + 模型；`-mtune` 只选模型/tune features；`-mattr` 覆盖 CPU feature；mca 只支持 `-mcpu`。
- RISC-V 的 `MachineScheduler` 没有另起炉灶：它继承 `GenericScheduler`，只在 `vsetvli` 兼容性上加启发式；pass 插入点是通用 `TargetPassConfig::addOptimizedRegAlloc()`。

---
## 21.12 自我检核

- [ ] 能画出 `ADD → Sched<[WriteIALU, ReadIALU, ReadIALU]> → RocketModel/WriteRes → mca Resource pressure` 的完整链路
- [ ] 能说出本树为什么没有 `RISCVSchedModel`，以及 `RISCVProcessorModel` 与 `RISCVTuneProcessorModel` 的分工
- [ ] 能对比 Rocket / SiFive7 / P600 的 `IssueWidth`、`MicroOpBufferSize`、`WriteIALU` 资源和 `div` 的 `ReleaseAtCycles`
- [ ] 能用 `.attribute 5` 和 `div`/`__divdi3` 证明 `-mtune` 没有改变 ISA
- [ ] 能解释 `RISCVMachineScheduler` 继承什么、在 pipeline 的哪个 pass 前后、`enableMachineScheduler()` 返回什么
- [ ] 能独立跑出 10 条 ALU 在三种 CPU 上的 IPC / Block RThroughput，并解释差异来自哪个字段
- [ ] 能说出 mca 的前端假设，以及为什么顺序核用 `-timeline`、乱序核才用 `-bottleneck-analysis`

---
## 21.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVSchedule.td`**：16–149、150–264、267–545、532–535。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVScheduleV.td`**：155–230、389–408、466–481。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVSchedRocket.td`**：14–20、57、75–88。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVSchedSiFive7.td`**：250–263、335、351–372、1608–1610。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVSchedSiFiveP600.td`**：302–311、358、386–403、370–374。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVProcessors.td`**：76–95、150–156、280–290、559。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVMachineScheduler.cpp`** 与 **`RISCVTargetMachine.cpp`**：15–216；215–260、325。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/TargetPassConfig.cpp`**：985–1100、1368；**`llvm/lib/MC/MCSubtargetInfo.cpp`**：245–248；**`llvm/docs/CommandGuide/llvm-mca.md`**：528–573、631–646、948–990。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/tools/llvm-mca/RISCV/`**：357 个测试；另见 **`llvm/test/CodeGen/RISCV/sched-model-load-latency.ll`**。

→ 下一章：[Ch 22 — AsmPrinter 与 ELF flags](./22-asm-printer-and-elf-flags.md)
