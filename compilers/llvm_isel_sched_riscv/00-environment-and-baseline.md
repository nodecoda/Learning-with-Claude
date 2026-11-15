# Ch 0 — 环境与证据基线

> **目标**：把本课的实验环境钉死，能用一条命令区分「指令选择变了」和「调度变了」。学完这章，你手上应该有一套可重复的 `llc` / `llvm-mca` 基线，而不是一堆凭记忆的汇编。

> **环境**：LLVM 24.0.0git，源码树 `/home/ubuntu/llvm/trees/explore`，commit `700e551714cc`（2026-11-10），构建 `/home/ubuntu/llvm/builds/explore`。本章所有输出都在这个构建上真实运行得到。你换版本、换 CPU 或换 `-mattr`，数字和顺序都可能不同。

## 为什么需要这个？

面试里被问「为什么这条 IR 选成了 `sh3add`，为什么 `add` 排在两个 `ld` 前面」时，最危险的不是不会，而是**把两层不同的东西混成一句**：

- 「因为 LLVM 觉得这样比较快」——太虚，面试官会继续追问哪一层、依据什么模型。
- 「因为 SelectionDAG 的调度器把它提前了」——可能错。最终顺序往往是 `MachineScheduler` 排的，SelectionDAG 只给了初始顺序。
- 「因为 `sifive-u74` 有双发射」——只对了一半。双发射是 `MCSchedModel` 的参数，真正决定这条 `add` 能不能提前的是依赖、资源、寄存器压力和调度策略。

要回答这类问题，先要有一个**可重复的最小实验**：同一段 IR、同一组 feature、只换一个变量，看汇编和 MIR 怎么变。本章就是搭这个实验台。

## 先建立直觉

LLVM 的代码生成不是「IR 进来，汇编出去」一条直线。它至少有两层「选择」和两层「调度」：

```
LLVM IR
   │
   ▼
SelectionDAGBuilder          把 IR 拆成 DAG
   │
   ▼
DAG Combine / Legalize       改 DAG，决定哪些操作合法
   │
   ▼
Instruction Selection        选 MachineInstr（opcode、operand、pseudo）
   │
   ▼
ScheduleDAGSDNodes           第一层调度：给 MachineInstr 一个初始顺序
   │
   ▼
MachineScheduler (pre-RA)    第二层调度：按 sched model 重排
   │
   ▼
Register Allocation          分配物理寄存器，可能 spill
   │
   ▼
Post-RA Scheduler            第三层：在物理寄存器约束下再排
   │
   ▼
MC layer                     编码、压缩、发射
```

这张图里，**只有 Instruction Selection 决定「用哪条指令」**；`ScheduleDAGSDNodes`、`MachineScheduler`、`PostRA` 决定「按什么顺序」。但 selection 会通过 opcode、operand、pseudo、`SchedRW` 标注，深深影响后三层能怎么排。这就是本课要打通的接缝。

## 最小可复现实验

先写一段不依赖 Clang、不依赖头文件的 IR。把它保存成 `min.ll`：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @f(ptr %p, i64 %a, i64 %b, i64 %c, i64 %d) {
  %x0 = load i64, ptr %p
  %x1 = mul i64 %x0, %a
  %p1 = getelementptr i64, ptr %p, i64 1
  %y0 = load i64, ptr %p1
  %y1 = mul i64 %y0, %c
  %s0 = add i64 %d, %b
  %s1 = add i64 %s0, %x1
  %s2 = add i64 %s1, %y1
  ret i64 %s2
}
```

这段 IR 有两个独立乘法链、两个 load、一个可以先算的加法。它足够小，能在一屏里看完；又足够有结构，能看出调度器把哪条指令提前。

设定两个变量：

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc
MCA=/home/ubuntu/llvm/builds/explore/bin/llvm-mca
```

先生成三个 CPU 的汇编。注意 `-mattr=+m`：`generic` 和 `rocket-rv64` 默认只带 `I`，不加 `M` 会把 `mul` 降成 `__muldi3` 调用，这不是调度差异，是 feature 差异。

```bash
for cpu in generic rocket-rv64 sifive-u74; do
  echo "===== $cpu ====="
  "$LLC" -mtriple=riscv64 -mcpu=$cpu -mattr=+m -O2 min.ll -o - \
    | sed -n '/^f:/,/^\.Lfunc_end/p'
done
```

实测输出：

```asm
===== generic =====
f:                                      # @f
	.cfi_startproc
# %bb.0:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
.Lfunc_end0:

===== rocket-rv64 =====
f:                                      # @f
	.cfi_startproc
# %bb.0:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
.Lfunc_end0:

===== sifive-u74 =====
f:                                      # @f
	.cfi_startproc
# %bb.0:
	add	a2, a2, a4
	ld	a4, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a4, a1
	mul	a0, a0, a3
	add	a1, a1, a2
	add	a0, a0, a1
	ret
.Lfunc_end0:
```

先不要急着解释。把差异写成一句话：

> `generic` 和 `rocket-rv64` 的指令**种类**和**顺序**一样；`sifive-u74` 的指令种类一样，但把 `add a2, a2, a4` 提到了两个 `ld` 前面。

这句话里有两个可检验的断言：

1. 指令种类没变 → 不是 instruction selection 变了。
2. 顺序变了 → 是 scheduling 变了。

这就是本课后面每一章都要用的推理方式。

## 证据工具箱：四个命令，四种视角

### 1. `-print-after-isel`：看「选完但还没排」的 MachineInstr

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-after-isel min.ll -o /dev/null
```

实测输出（节选）：

```text
# After Instruction Selection:
# Machine code for function f: IsSSA, TracksLiveness
Function Live Ins: $x10 in %0, $x11 in %1, $x12 in %2, $x13 in %3, $x14 in %4

bb.0 (%ir-block.0):
  liveins: $x10, $x11, $x12, $x13, $x14
  %4:gpr = COPY $x14
  %3:gpr = COPY $x13
  %2:gpr = COPY $x12
  %1:gpr = COPY $x11
  %0:gpr = COPY $x10
  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
  %6:gpr = MUL %5:gpr, %1:gpr
  %7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
  %8:gpr = MUL %7:gpr, %3:gpr
  %9:gpr = ADD %4:gpr, %2:gpr
  %10:gpr = ADD %9:gpr, %6:gpr
  %11:gpr = ADD %10:gpr, %8:gpr
  $x10 = COPY %11:gpr
  PseudoRET implicit $x10

# End machine code for function f.
```

读法：

- `COPY $x14` 这些是参数从物理寄存器进入虚拟寄存器的桥。
- `LD` / `MUL` / `ADD` 是 SelectionDAG 选出来的 MachineInstr。
- `%9 = ADD %4, %2` 已经对应源码里的 `%s0 = add i64 %d, %b`，但它的位置还在 `%8` 后面。**这是 ISel 的顺序，不是最终顺序。**
- `PseudoRET` 是伪指令，后面会展开成 `ret`。

### 2. `-print-before/after=machine-scheduler`：看 pre-RA 调度改了什么

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-before=machine-scheduler min.ll -o /dev/null

"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-after=machine-scheduler min.ll -o /dev/null
```

`Before` 的关键片段：

```text
96B	  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
112B	  %6:gpr = MUL %5:gpr, %1:gpr
128B	  %7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
144B	  %8:gpr = MUL %7:gpr, %3:gpr
160B	  %9:gpr = ADD %4:gpr, %2:gpr
176B	  %10:gpr = ADD %9:gpr, %6:gpr
192B	  %11:gpr = ADD %10:gpr, %8:gpr
```

`After` 的关键片段：

```text
56B	  %9:gpr = ADD %4:gpr, %2:gpr
96B	  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
128B	  %7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
136B	  %6:gpr = MUL %5:gpr, %1:gpr
144B	  %8:gpr = MUL %7:gpr, %3:gpr
176B	  %10:gpr = ADD %9:gpr, %6:gpr
192B	  %11:gpr = ADD %10:gpr, %8:gpr
```

`%9 = ADD` 从 `%8` 后面移到了两个 `LD` 前面。指令没有变，只是顺序变了。**这是 MachineScheduler 做的，不是 ISel 做的。**

### 3. `-debug-only=machine-scheduler`：看调度器为什么这么排

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -debug-only=machine-scheduler min.ll -o /dev/null
```

实测输出（节选）：

```text
Current Schedule Region
f:%bb.0
  From: %4:gpr = COPY $x14
    To: PseudoRET implicit $x10
 RegionInstrs: 13
ScheduleDAGMILive::schedule starting
GenericScheduler RegionPolicy:  ShouldTrackPressure=1 OnlyTopDown=0 OnlyBottomUp=0
Max Pressure: GPRF16C=5 GPRF16C_with_GPRS07=5 GPRTC=5 GPRF16=6
Live In: X10_H X11_H X12_H X13_H X14_H
...
Cluster ld/st SU(5) - SU(7)
```

这里能读出三件事：

- 调度单位是 **region**（这里是整个 `bb.0`），不是单条指令。
- `ShouldTrackPressure=1`：调度器在跟踪寄存器压力，不是只看延迟。
- `Cluster ld/st SU(5) - SU(7)`：两个 load 被当成一个 cluster 考虑。

### 4. `llvm-mca`：把同一段汇编放到不同 CPU 模型上量化

把 `sifive-u74` 的汇编保存成 `s.s`，然后跑三个 CPU：

```bash
cat > s.s <<'ASM'
add a2, a2, a4
ld a4, 0(a0)
ld a0, 8(a0)
mul a1, a4, a1
mul a0, a0, a3
add a1, a1, a2
add a0, a0, a1
ret
ASM

for cpu in generic rocket-rv64 sifive-u74; do
  echo "--- $cpu ---"
  "$MCA" -mtriple=riscv64 -mcpu=$cpu -mattr=+m -iterations=100 s.s \
    | grep -E "Iterations|Instructions|Total Cycles|Total uOps|Dispatch Width|IPC|Block RThroughput"
done
```

实测输出：

```text
--- generic ---
Iterations:        100
Instructions:      800
Total Cycles:      1201
Total uOps:        800
Dispatch Width:    2
IPC:               0.67
Block RThroughput: 4.0
--- rocket-rv64 ---
Iterations:        100
Instructions:      800
Total Cycles:      1001
Total uOps:        800
Dispatch Width:    1
IPC:               0.80
Block RThroughput: 8.0
--- sifive-u74 ---
Iterations:        100
Instructions:      800
Total Cycles:      901
Total uOps:        800
Dispatch Width:    2
IPC:               0.89
Block RThroughput: 4.0
```

同一段汇编，模型不同，周期数从 901 到 1201。**这不是代码变慢了，是模型不同。** 第 14 章会逐项拆 `llvm-mca` 的报告；现在先记住：每个周期数都要带 `-mcpu`、`-mattr`、`-iterations`。

## 版本与路径：为什么必须钉住

LLVM 的 RISC-V backend 变化很快。同一个 `-mcpu=sifive-u74`，在不同版本可能：

- 指向不同的 `SchedMachineModel`；
- 多出或少掉某个扩展的 pattern；
- 改变 `MachineScheduler` 的默认策略；
- 改变 `generic` 的 CPU 映射。

本课钉住：

```text
LLVM 24.0.0git
source: /home/ubuntu/llvm/trees/explore
commit: 700e551714cc (2026-11-10)
build:  /home/ubuntu/llvm/builds/explore
targets: X86;AArch64;RISCV;WebAssembly;PowerPC;ARM;Mips;SPIRV
```

验证命令：

```bash
"$LLC" --version | head -6
"$LLC" -march=riscv64 -mcpu=help | head -20
"$MCA" --version | head -6
```

`llc --version` 实测：

```text
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
  Default target: x86_64-unknown-linux-gnu
  Host CPU: znver2
```

`llvm-mca --version` 实测：

```text
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
  Default target: x86_64-unknown-linux-gnu
  Host CPU: znver2
```

注意：`llc --version` 不会打印源码 commit。要知道你跑的是哪棵树，必须自己记录构建路径和 commit。本课在每章开头都写环境，就是为了避免「我这边输出不一样」时找不到原因。

## 踩雷集锦

1. **`llc` 没有 `-S`**：`-S` 是 `clang`/`opt` 的常用选项，`llc` 默认就输出汇编。第一次跑 `llc -S` 会得到 `Unknown command line argument '-S'`。本项目实测就是这样，不是你的 LLVM 坏了。

2. **`-mcpu` 同时改 feature 和 sched model**：`-mcpu=generic` 在 RV64 下会映射到 `generic-rv64`，而 `generic-rv64` 只带 `I`。不加 `-mattr=+m`，`mul` 会变成 `__muldi3` 调用。你以为在看调度，其实在看 feature selection。做调度对比时，先把 `-mattr` 固定住。

3. **`-mcpu=generic`、`-mtune=generic` 都不是 `NoSchedModel`**：`RISCVSubtarget.cpp:61-67` 会在 CPU 为空或 `"generic"` 时把 CPU 改成 `generic-rv64`/`generic-rv32`，`generic-rv64` 用 `SpacemitX60Model`；`TuneCPU` 为空时跟随 CPU，`"generic"` 同样被规范化成 `generic-rv64`。所以两者都落在 `SpacemitX60Model` 上。`.td` 里的 `def GENERIC : RISCVTuneProcessorModel<"generic", NoSchedModel>` 只是这个 tune 条目自身绑了 `NoSchedModel`，CLI 上的 `"generic"` 根本不会以原样到达调度器。真要观察 `NoSchedModel`，用 `-mcpu=andes-ax25`（或 `-mcpu=sifive-u74 -mtune=andes-ax25`）：`llvm-mca` 直接报 `unable to find instruction-level scheduling information`，`llc` 退化成 `MCSchedModel::Default`（`IssueWidth=1`、无 `ProcResource`）。本项目实测：`ldmul.ll` 在 `sifive-u74` 下 60 条栈访问，换成 `-mtune=andes-ax25` 后 84 条。

4. **`llc` 默认把汇编写进 `<input>.s`，不是 stdout**：`llc min.ll` 之后 stdout 是空的，结果在 `min.s`。本课所有命令都显式写 `-o -`，让结果进 stdout，方便管道和 diff。用 `-o /dev/null` 跑分析类选项时，`-print-*` 的调试输出仍然走 stderr。

5. **`generic-ooo` 是 tune-only model**：它不在 `RISCVProcessorModel` 里，本身不带 `Feature64Bit`。直接用 `-mcpu=generic-ooo` 跑 RV64 会得到 `LLVM ERROR: RV64 target requires an RV64 CPU`；要加 `-mattr=+64bit`（以及你需要的扩展）才能跑。这个错误不是代码坏了，是 CPU 定义缺 feature。

6. **`llvm-mca` 不会替你补 feature**：用 `-mcpu=rocket-rv64` 跑含 `mul` 的汇编，会报 `instruction requires ... 'Zmmul'`；加 `-mattr=+m` 才能解析。`llc` 和 `llvm-mca` 的 feature 推导不完全一样，永远把 `-mattr` 写全。

7. **函数上的 `target-cpu` 属性会覆盖命令行**：如果你用 `clang` 生成 IR 再喂给 `llc`，IR 里可能带 `"target-cpu"="x86-64"`。这时 `llc -mtriple=riscv64 -mcpu=sifive-u74` 会先抱怨 `'x86-64' is not a recognized processor`，然后可能报 `RV64 target requires an RV64 CPU`。本课尽量用手写 `.ll`，就是为了隔离 frontend 属性。

## 动手练习

1. 把 `min.ll` 复制到 `/tmp/isel-sched/`，用 `generic`、`rocket-rv64`、`sifive-u74` 各跑一次，填表：

   | CPU | 指令种类是否变化 | 顺序是否变化 | 你认为哪一层变了 |
   |---|---|---|---|
   | generic |  |  |  |
   | rocket-rv64 |  |  |  |
   | sifive-u74 |  |  |  |

2. 不加 `-mattr=+m` 再跑一次，观察 `mul` 变成什么。解释为什么这时候不能把差异归因于 scheduler。

3. 用 `-print-after-isel` 和 `-print-after=machine-scheduler` 各存一份输出，用 `diff -u` 对比。找出第一条位置变化的指令。

4. 把 `s.s` 里的 `add` 删掉，再跑 `llvm-mca`。周期数怎么变？如果周期数下降很多，说明这条 `add` 在关键路径上还是在填充延迟？

5. 用 `-mcpu=generic-ooo -mattr=+64bit,+m` 跑一次，看它和 `sifive-u74` 的顺序是否相同。相同或不同都写下你的解释。

## 本章重点整理

- LLVM 的代码生成至少有两层调度：`ScheduleDAGSDNodes` 给初始顺序，`MachineScheduler` 按 sched model 重排，post-RA 还能再排一次。
- 判断差异归属的第一步：**看指令种类有没有变**。种类变 → selection/feature；种类不变、顺序变 → scheduling。
- `-mcpu` 同时影响 feature 和 sched model。做调度实验时，先固定 `-mattr`。
- `llc --version` 不告诉你源码 commit；本课用构建路径 + commit 钉住证据。
- `llvm-mca` 是模型模拟器，不是芯片模拟器。周期数必须带 `-mcpu`、`-mattr`、`-iterations`。

## 自我检核

- [ ] 能不看笔记说出 `ScheduleDAGSDNodes` 和 `MachineScheduler` 各在哪一层、各做什么
- [ ] 能解释为什么 `-mcpu=generic` 下 `mul` 可能变成 `__muldi3`，以及这和 scheduling 无关
- [ ] 能说出至少三个会让 `llc` 报错或输出不符合预期的「环境陷阱」
- [ ] 能用 `-print-before/after=machine-scheduler` 证明某条指令是 scheduler 移动的，而不是 ISel 选的
- [ ] 知道 `llvm-mca` 的周期数不能直接当成硬件实测

## 延伸阅读

### 官方文档

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)**
  - **读哪里**：先读「The LLVM Target-Independent Code Generator」的总览，再读「SelectionDAG」和「Machine Code」两节。
  - **和本章的关联**：本章的 pipeline 图就是这份文档的简化版；读完后你会知道每个阶段在官方术语里叫什么。

- **[llvm-mca Command Guide](https://llvm.org/docs/CommandGuide/llvm-mca.html)**
  - **读哪里**：「Description」和「Options」两节；先看 `-mtriple`、`-mcpu`、`-mattr`、`-iterations`。
  - **和本章的关联**：本章只用了 mca 的汇总行；第 14 章会拆 `Instruction Info` 和 `Resources`。

- **[LLVM Language Reference](https://llvm.org/docs/LangRef.html)**
  - **读哪里**：`getelementptr`、`load`、`mul`、`add` 的语义；尤其是 `getelementptr` 的 `inbounds` 和类型。
  - **和本章的关联**：`min.ll` 里的 `getelementptr i64, ptr %p, i64 1` 就是后面 `ld ... 8(a0)` 的来源。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - **读哪里**：`CodeGenAndEmitDAG()`（约 942 行）、`DoInstructionSelection()`（约 1282 行）、`CreateScheduler()`（约 2231 行）。
  - **和本章的关联**：这三个函数把「建 DAG → 选指令 → 调度 → 发射」串起来。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - **读哪里**：`BuildSchedGraph()`（约 543 行）、`EmitSchedule()`（约 874 行）。
  - **和本章的关联**：第一层调度的建图和发射都在这里。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`**
  - **读哪里**：`ScheduleDAGMILive::schedule()`（约 1996 行）、`GenericScheduler` 相关段落。
  - **和本章的关联**：第二层调度的主循环；`-debug-only=machine-scheduler` 的输出就是它打的。

- **`llvm/lib/Target/RISCV/RISCVSubtarget.cpp`**
  - **读哪里**：约 61–67 行，`CPU == "generic"` 与 `TuneCPU == "generic"` 的两次规范化。
  - **和本章的关联**：解释为什么 `-mcpu=generic`、`-mtune=generic` 都落在 `generic-rv64` 上，而不是 `NoSchedModel`。

### 博客 / 演讲

- **LLVM Developers' Meeting: "Instruction Scheduling in LLVM"**（LLVM YouTube 频道）
  - **这讲什么**：MachineScheduler 的设计、策略、sched model。
  - **为什么值得看**：先听作者讲设计动机，再读 `MachineScheduler.cpp` 会顺很多。

- **RISC-V LLVM 文档**：<https://llvm.org/docs/RISCVUsage.html>
  - **读哪里**：`-mcpu` / `-mattr` / extension 支持表。
  - **和本章的关联**：确认 `sifive-u74` 默认带哪些扩展，避免再踩 `+m` 的坑。

下一章把这条流水线完整走一遍：从 IR 到 `-print-after-isel`，再到 MachineScheduler 的 before/after，最后到最终汇编和 mca。你会看到同一条指令在每一层的位置变化。

→ [Ch 1 一条 IR 到汇编：两条调度主线](./01-ir-to-asm-two-scheduling-lines.md)
