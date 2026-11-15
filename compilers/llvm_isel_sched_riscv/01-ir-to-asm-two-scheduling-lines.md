# Ch 1 — 一条 IR 到汇编：两条调度主线

> **目标**：跟着一个函数走完 IR → SelectionDAG → MachineInstr → MachineScheduler → 最终汇编，能在每一步指出「这条指令是谁选的、这个顺序是谁排的」。学完这章，你看到一段汇编时，不会再笼统地说「LLVM 优化了」。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，源码 commit `700e551714cc`。所有输出都是本机实测。

## 为什么需要这个？

面试里最容易被追问倒的一题是：

> 「这段 IR 为什么生成这个顺序？」

很多人会答「因为调度器」。但 LLVM 里有三个「调度器」：

1. `ScheduleDAGSDNodes`：SelectionDAG 内部的调度器，决定 MachineInstr 的初始顺序。
2. `MachineScheduler`：Machine IR 层的调度器，pre-RA 和 post-RA 各跑一次，按 `MCSchedModel` 重排。
3. `PostMachineScheduler`：post-RA 的简化调度，处理物理寄存器约束和 hazard。

如果你只说「调度器」，面试官无法判断你知不知道是哪一层。更糟的是，你可能会把 **instruction selection 的选择**（比如 `sh3add` 替代 `slli+add`）误认为 scheduling，或者反过来。

这一章用一个函数把三层拆开。我们只看一个基本块，但每一层的证据都齐全。

## 先建立直觉

同一道菜，有三次「摆盘」：

```
源码/IR 的顺序          →  你写代码的顺序
ISel 之后的 MIR 顺序    →  SelectionDAG 选完指令后的初始摆盘
MachineScheduler 之后   →  按 CPU 模型重新摆盘
最终汇编                →  寄存器分配、伪指令展开后的最终摆盘
```

「食材」是指令：`LD`、`MUL`、`ADD`。Instruction selection 决定用哪些食材；scheduling 决定摆盘顺序。食材换了，摆盘当然会变；但摆盘变了，不代表食材换了。

本章的函数只有 8 条真实指令，足够看清三次摆盘。

## 完整流水线

```
LLVM IR
  │  SelectionDAGBuilder::visit()
  ▼
SelectionDAG（SDNode：load / mul / add / token / glue）
  │  DAGCombiner
  │  LegalizeDAG
  ▼
合法的 SelectionDAG
  │  DoInstructionSelection()
  │    └─ RISCVDAGToDAGISel::Select()
  ▼
MachineInstr（LD / MUL / ADD / COPY / PseudoRET）
  │  ScheduleDAGSDNodes::BuildSchedGraph()
  │  ScheduleDAGSDNodes::EmitSchedule()
  ▼
MachineInstr（第一层顺序）
  │  MachineScheduler（pre-RA）
  │    └─ ScheduleDAGMILive::schedule()
  ▼
MachineInstr（第二层顺序）
  │  Register Allocation
  ▼
MachineInstr（物理寄存器）
  │  Post-RA Scheduler / Pseudo Expansion
  ▼
最终汇编
```

关键点：**SelectionDAG 自己也会调度**。`ScheduleDAGSDNodes` 不是摆设；它在 `EmitSchedule()` 里决定先发射哪个 SUnit。只是这个顺序后面还会被 `MachineScheduler` 改掉。

## 第 1 步：IR

用 Ch 0 的 `min.ll`：

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

IR 的顺序是：

```
load x0
mul  x1
gep  p1
load y0
mul  y1
add  s0
add  s1
add  s2
ret
```

注意 `%s0 = add %d, %b` 在 IR 里排在两个 `mul` 后面，但它**不依赖**任何 load 或 mul。这是一个「可以提前」的候选。谁会发现它可以提前？DAG scheduler 和 MachineScheduler 都可能，但最终谁做主，要看后面的证据。

## 第 2 步：SelectionDAG 选完，`-print-after-isel`

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-after-isel min.ll -o /dev/null
```

实测输出：

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

逐行读：

| MIR | 来源 | 说明 |
|---|---|---|
| `%0..%4 = COPY $x10..$x14` | 参数 lowering | ABI 把参数放在 `a0..a4`，对应 `$x10..$x14` |
| `%5 = LD %0, 0` | `load i64, ptr %p` | 选了 `LD`，offset 0 |
| `%6 = MUL %5, %1` | `mul %x0, %a` | 选了 `MUL`（不是 `MULH` 或 libcall，因为 `+m`） |
| `%7 = LD %0, 8` | `load %p1` | `getelementptr` 被 folded 成 offset 8 |
| `%8 = MUL %7, %3` | `mul %y0, %c` | 第二条乘法链 |
| `%9 = ADD %4, %2` | `add %d, %b` | 选了 `ADD`，但位置还在 `%8` 后面 |
| `%10 = ADD %9, %6` | `add %s0, %x1` | |
| `%11 = ADD %10, %8` | `add %s1, %y1` | |
| `$x10 = COPY %11` | 返回值 | 把结果放进 `a0` |
| `PseudoRET` | `ret` | 伪指令，后面展开 |

这里已经能看出 selection 的三个决定：

1. `load` 选了 `LD`，不是 `LW`/`LB`，因为类型是 `i64`。
2. `mul` 选了 `MUL`，因为 `+m`；如果没有 `M`，会变成 `__muldi3` 调用。
3. `getelementptr` 没有单独选成一条 `ADD`，而是 folded 进 `LD` 的 offset 8。

但 `%9 = ADD` 的位置仍然在 `%8` 后面。**这不是最终顺序。**

## 第 3 步：第一层调度，`ScheduleDAGSDNodes`

SelectionDAG 在发射 MachineInstr 之前，会构建一个 `ScheduleDAGSDNodes`。它把 DAG 节点变成 SUnit，加数据依赖、内存依赖，然后按 `-pre-RA-sched` 指定的策略排序。

`-pre-RA-sched=default` 对 RISC-V 会走 `createDefaultScheduler()`：RISC-V 的 `RISCVSubtarget::enableMachineScheduler()` 直接返回 true，而 `enableMachineSchedDefaultSched()` 默认也是 true，所以它返回 `createSourceListDAGScheduler()`，先把顺序基本保持源码序，把真正的重排交给 MachineScheduler。源码在 `SelectionDAGISel.cpp` 的 `createDefaultScheduler()` 和 `RISCVSubtarget.h` 的 `enableMachineScheduler()`。如果你把 MachineScheduler 关掉，就能看到这一层的输出：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -enable-misched=false min.ll -o - \
  | sed -n '/^f:/,/^\.Lfunc_end/p'
```

实测输出：

```asm
f:                                      # @f
	.cfi_startproc
# %bb.0:
	ld	a5, 0(a0)
	add	a2, a2, a4
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a1, a1, a2
	add	a0, a0, a1
	ret
.Lfunc_end0:
```

这就是 `createSourceListDAGScheduler()`：基本保持源码序，`ld a5` 先发，`add` 夹在两个 `ld` 中间。它不负责最终的性能排序，只负责把 DAG 发射成 MachineInstr。

再看 `-pre-RA-sched=source`，输出和 default 一样：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -enable-misched=false -pre-RA-sched=source min.ll -o - \
  | sed -n '/^f:/,/^\.Lfunc_end/p'
```

```asm
f:                                      # @f
	.cfi_startproc
# %bb.0:
	ld	a5, 0(a0)
	add	a2, a2, a4
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a1, a1, a2
	add	a0, a0, a1
	ret
.Lfunc_end0:
```

换一个更激进的 DAG scheduler，`-pre-RA-sched=list-burr`：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -enable-misched=false -pre-RA-sched=list-burr min.ll -o - \
  | sed -n '/^f:/,/^\.Lfunc_end/p'
```

实测输出：

```asm
f:                                      # @f
	.cfi_startproc
# %bb.0:
	add	a2, a2, a4
	ld	a4, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a4, a1
	add	a1, a1, a2
	mul	a0, a0, a3
	add	a0, a0, a1
	ret
.Lfunc_end0:
```

`list-burr` 把 `add` 提前到两个 `ld` 前面，并把 `mul a0` 挪到 `add a1` 后面。这是 **DAG scheduler** 做的，不是 MachineScheduler。`source` 和 `list-burr` 的差异证明：**`-pre-RA-sched` 真的会改变 DAG 调度结果**。但如果你打开 MachineScheduler，最终顺序还会被重排。

## 第 4 步：第二层调度，`MachineScheduler`

打开 MachineScheduler（默认开），看 before/after：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-before=machine-scheduler min.ll -o /dev/null

"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -print-after=machine-scheduler min.ll -o /dev/null
```

`Before`（节选）：

```text
96B	  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
112B	  %6:gpr = MUL %5:gpr, %1:gpr
128B	  %7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
144B	  %8:gpr = MUL %7:gpr, %3:gpr
160B	  %9:gpr = ADD %4:gpr, %2:gpr
176B	  %10:gpr = ADD %9:gpr, %6:gpr
192B	  %11:gpr = ADD %10:gpr, %8:gpr
```

`After`（节选）：

```text
56B	  %9:gpr = ADD %4:gpr, %2:gpr
96B	  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
128B	  %7:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
136B	  %6:gpr = MUL %5:gpr, %1:gpr
144B	  %8:gpr = MUL %7:gpr, %3:gpr
176B	  %10:gpr = ADD %9:gpr, %6:gpr
192B	  %11:gpr = ADD %10:gpr, %8:gpr
```

`%9 = ADD` 从 `%8` 后面移到了 `%5` 前面。指令、操作数、虚拟寄存器都没变，只有顺序变了。

**这一步是本课的核心证据：最终顺序是 MachineScheduler 排的，不是 ISel 排的。**

为什么它要提前 `%9`？看调试输出：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -debug-only=machine-scheduler min.ll -o /dev/null
```

实测（节选）：

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
Live Out:
Live Thru:
Top Pressure: GPRF16C=5 GPRF16C_with_GPRS07=5 GPRTC=5 GPRF16=5
Bottom Pressure: GPRF16C=1 GPRF16C_with_GPRS07=1 GPRTC=1 GPRF16=1
Num BaseOps: 1, Offset: 0, OffsetIsScalable: 0, Width: LocationSize::precise(8)
Num BaseOps: 1, Offset: 8, OffsetIsScalable: 0, Width: LocationSize::precise(8)
Cluster ld/st SU(5) - SU(7)
```

读法：

- `RegionPolicy: ShouldTrackPressure=1`：调度器不是只追延迟，还在跟踪寄存器压力。
- `OnlyTopDown=0 OnlyBottomUp=0`：默认是双向调度，top-down 和 bottom-up 都考虑。
- `Cluster ld/st SU(5) - SU(7)`：两个 load 被当作一个 cluster，调度器会把它们放在一起考虑。
- `Max Pressure: GPRF16C=5 ...`：这个 region 的寄存器压力上限是 5 左右。提前 `%9` 不会增加压力，因为它只读 `%4`、`%2`，不产生新的活跃区间。

这就是「为什么可以提前」的完整解释：**有依赖空间、不增加压力、能填充 load 的延迟**。

## 第 5 步：最终汇编

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 min.ll -o -
```

实测输出：

```asm
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zifencei2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
	.file	"min.ll"
	.text
	.globl	f                               # -- Begin function f
	.p2align	1
	.type	f,@function
f:                                      # @f
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

到这里，`%9`、`%10`、`%11` 已经变成物理寄存器 `a2`、`a1`、`a0`。`PseudoRET` 展开成 `ret`。`COPY` 被寄存器分配消掉。顺序和 MachineScheduler 的 after 一致（除了伪指令展开和寄存器重命名）。

## 同一段 IR，三个 CPU

现在把 `generic` 和 `rocket-rv64` 也跑一遍（都加 `-mattr=+m`，保持 feature 一致）：

```bash
for cpu in generic rocket-rv64 sifive-u74; do
  echo "===== $cpu ====="
  "$LLC" -mtriple=riscv64 -mcpu=$cpu -mattr=+m -O2 min.ll -o - \
    | sed -n '/^f:/,/^\.Lfunc_end/p'
done
```

实测：

```asm
===== generic =====
f:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret

===== rocket-rv64 =====
f:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret

===== sifive-u74 =====
f:
	add	a2, a2, a4
	ld	a4, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a4, a1
	mul	a0, a0, a3
	add	a1, a1, a2
	add	a0, a0, a1
	ret
```

三个结论：

1. **指令种类完全一样**：都是 2 个 `ld`、2 个 `mul`、3 个 `add`、1 个 `ret`。
2. **`generic` 和 `rocket-rv64` 的顺序一样**：`add` 留在后面。
3. **`sifive-u74` 把 `add` 提前了**：因为它的 `SiFive7Model` 是双发射、顺序核，`LoadLatency=3`，scheduler 认为提前 `add` 能填充 load 延迟。

这不是「`sifive-u74` 的 ISel 更聪明」。ISel 选的指令一样；差别在 `MCSchedModel` 和 `MachineScheduler`。

## 再看 `llvm-mca`：模型不同，周期不同

把 `sifive-u74` 的汇编存成 `s.s`，用三个 CPU 跑 mca：

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
  /home/ubuntu/llvm/builds/explore/bin/llvm-mca \
    -mtriple=riscv64 -mcpu=$cpu -mattr=+m -iterations=100 s.s \
    | grep -E "Iterations|Instructions|Total Cycles|Total uOps|Dispatch Width|IPC|Block RThroughput"
done
```

实测：

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

同一段汇编，`rocket-rv64` 是单发射（`Dispatch Width: 1`），`sifive-u74` 是双发射（`Dispatch Width: 2`），所以周期数差很多。`generic` 在 RV64 下映射到 `generic-rv64`，用的是 `SpacemitX60Model`（双发射、顺序核），周期数又不一样。

**注意**：这里比较的是同一段汇编在三个模型上的模拟结果，不是三个编译器输出的性能。要看「不同调度哪个更快」，应该把三个 `llc` 输出分别喂给同一个 `-mcpu` 的 mca，控制变量。

## 谁做了什么：一张归因表

| 你看到的变化 | 最可能做这件事的层 | 证据 |
|---|---|---|
| `mul` 变成 `__muldi3` 调用 | feature selection / ISel | 检查 `-mattr=+m`；看 `-print-after-isel` |
| `slli+add` 变成 `sh3add` | DAG combine / ISel pattern | `-mattr=+zba`；看 `-print-after-isel` |
| `select` 从分支变成 `czero` | ISel lowering / pattern | `-mattr=+zicond`；看 `-print-after-isel` |
| `add` 从 `%8` 后面移到两个 `ld` 前面 | MachineScheduler | `-print-before/after=machine-scheduler` |
| 两个 `ld` 被放在一起 | DAG scheduler 或 MachineScheduler 的 cluster | `-debug-only=machine-scheduler` 的 `Cluster ld/st` |
| `PseudoRET` 变成 `ret` | pseudo expansion | `-print-after=prologepilog` 或最终汇编 |
| 多出 `sd`/`ld` spill | register allocation | `-print-after=regalloc`；看 `-regalloc` |

这张表是面试时的「归因模板」。先说层级，再说证据，最后说模型。

## 选择如何约束调度：三个活例子

### 例子 1：`sh3add` vs `slli+add`

`idx` 函数：

```llvm
define i64 @idx(ptr %p, i64 %i) {
  %q = getelementptr i64, ptr %p, i64 %i
  %v = load i64, ptr %q
  ret i64 %v
}
```

不加 `+zba`：

```asm
slli	a1, a1, 3
add	a0, a0, a1
ld	a0, 0(a0)
ret
```

加 `+zba`：

```asm
sh3add	a0, a1, a0
ld	a0, 0(a0)
ret
```

Selection 把两条指令合成一条，依赖链从 `slli → add → ld` 变成 `sh3add → ld`。scheduler 能排的空间变小了，但关键路径也短了。这是「selection 改变调度自由度」的最直观例子。

### 例子 2：`select` 的分支 vs `czero`

```llvm
define i64 @sel(i1 %c, i64 %a, i64 %b) {
  %r = select i1 %c, i64 %a, i64 %b
  ret i64 %r
}
```

不加 `+zicond`：

```asm
andi	a3, a0, 1
mv	a0, a1
bnez	a3, .LBB0_2
mv	a0, a2
.LBB0_2:
ret
```

加 `+zicond`：

```asm
andi	a0, a0, 1
czero.nez	a2, a2, a0
czero.eqz	a0, a1, a0
or	a0, a0, a2
ret
```

前者有分支，scheduler 只能在一个基本块内排；后者是直线代码，scheduler 可以把 `czero` 和周围的独立指令交错。但后者多了两条指令，寄存器压力也可能变高。**没有绝对更好，只有模型和场景。**

### 例子 3：pseudo 指令隐藏了真实成本

`PseudoRET` 在 MIR 里只占一条，但展开后是 `ret`。`TAIL`、`CALL`、`PseudoCCMOVGPR` 等伪指令也一样。scheduler 看到的是 pseudo 的 `SchedRW`，不是展开后的序列。如果 pseudo 的 sched 标注不准，调度器就会用错误的延迟/资源做决策。

第 20 章会专门拆 pseudo expansion。现在记住：**MIR 里的一条不等于最终汇编的一条。**

## 踩雷集锦

1. **把最终汇编顺序当成 ISel 顺序**：最终顺序经过了 `ScheduleDAGSDNodes`、`MachineScheduler`、`PostRA`、pseudo expansion。要看 ISel 顺序，必须用 `-print-after-isel`。

2. **以为 `-enable-misched=false` 就「没有调度」**：关掉 MachineScheduler 后，`ScheduleDAGSDNodes` 还在跑。你看到的是第一层调度的结果，不是源码顺序。想看更接近源码顺序，用 `-pre-RA-sched=source`。

3. **只换 `-mcpu` 不固定 `-mattr`**：`-mcpu` 会改 feature。`generic`/`rocket-rv64` 默认没有 `M`，`mul` 会变成 libcall；`sifive-u74` 有 `M`。这样比较的是 feature + scheduling，不是纯 scheduling。

4. **把 `-mcpu=generic` 当成「无模型」**：RV64 下它映射到 `generic-rv64`，使用 `SpacemitX60Model`。`-mtune=generic` 也会被规范化成 `generic-rv64`，同样有模型——LLVM 24 里 tune CPU 决定 `MCSchedModel`（`MCSubtargetInfo.cpp:245-246`），所以 `-mtune` 不是「只改微调、不改调度」。要看到真正的 `NoSchedModel`，用 `-mcpu=andes-ax25` 或 `-mtune=andes-ax25`。

5. **用 `llvm-mca` 比较不同 `llc` 输出时忘了统一 `-mcpu`**：`llc -mcpu=sifive-u74` 的输出喂给 `llvm-mca -mcpu=rocket-rv64`，你比较的是「sifive 的代码在 rocket 模型上跑」，不是「sifive 的调度 vs rocket 的调度」。

6. **忽略 `-O0`**：`-O0` 下 FastISel/SelectionDAG 的选择和 `-O2` 不同，寄存器压力也完全不同。用 `-O0` 的汇编解释 `-O2` 的调度，会得出错误结论。

## 动手练习

1. 用 `-print-after-isel` 和 `-print-after=machine-scheduler` 各存一份 `min.ll` 的输出，用 `diff -u` 找出所有位置变化的指令。逐条解释：它为什么能移动？依赖是什么？压力是什么？

2. 跑 `-enable-misched=false` 加 `-pre-RA-sched=source`、`list-burr`、`list-hybrid`、`list-ilp`、`fast`、`linearize` 六种组合，记录 `add a2` 的位置。哪几种把它提前？哪几种保持源码顺序？

3. 把 `min.ll` 的 `%s0 = add i64 %d, %b` 改成依赖 `%x1` 的表达式，再跑一次。`add` 还能提前吗？为什么？

4. 用 `-mcpu=generic`、`-mcpu=rocket-rv64`、`-mcpu=sifive-u74` 各跑一次 `-debug-only=machine-scheduler`，比较 `Max Pressure` 和 `Cluster ld/st` 的输出。哪些字段变了？

5. 把 `s.s` 里的 `add a2, a2, a4` 删掉，用三个 CPU 跑 mca。周期数变化最大的是哪个模型？为什么？

## 本章重点整理

- LLVM 至少有两层调度：`ScheduleDAGSDNodes`（第一层，DAG 内）和 `MachineScheduler`（第二层，Machine IR）。
- Instruction selection 决定「用哪条指令」，scheduling 决定「按什么顺序」；selection 通过 opcode、operand、pseudo、`SchedRW` 约束 scheduling。
- 判断差异归属：先看指令种类，再看顺序，再看 feature。
- `-print-after-isel` 看选择，`-print-before/after=machine-scheduler` 看第二层调度，`-debug-only=machine-scheduler` 看策略和压力。
- 同一段汇编在不同 `-mcpu` 下的 mca 周期数不同，这是模型差异，不是代码差异。

## 自我检核

- [ ] 能画出 IR → SelectionDAG → MIR → MachineScheduler → 最终汇编的流程图，并标出两层调度
- [ ] 能解释 `-print-after-isel` 和 `-print-after=machine-scheduler` 的区别
- [ ] 能说出至少三个「selection 改变调度自由度」的例子
- [ ] 能解释为什么 `-mcpu=generic` 下 `mul` 可能变成 libcall，以及如何避免这个干扰
- [ ] 能用 `-debug-only=machine-scheduler` 的 `ShouldTrackPressure`、`Cluster ld/st` 解释一次调度决策
- [ ] 知道 `llvm-mca` 的周期数不能直接当成硬件性能

## 延伸阅读

### 官方文档

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)**
  - **读哪里**：「SelectionDAG」小节的 `SelectionDAGISel`、`ScheduleDAGSDNodes`；「Machine Code」小节的 `MachineScheduler`。
  - **和本章的关联**：本章的流水线图对应这两节；读完你会知道每个类在官方文档里的位置。

- **[Machine IR (MIR) Format Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`-print-after-isel` 输出里的 `bb.0`、`liveins`、虚拟寄存器、`PseudoRET` 语法。
  - **和本章的关联**：本章的 MIR 片段都能用这份文档解释。

- **[llvm-mca Command Guide](https://llvm.org/docs/CommandGuide/llvm-mca.html)**
  - **读哪里**：`-iterations`、`-mcpu`、`-mattr`；以及 `Total Cycles`、`IPC`、`Block RThroughput` 的定义。
  - **和本章的关联**：本章最后的三组周期数就是这些字段。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - **读哪里**：`CodeGenAndEmitDAG()`（约 942 行）到 `DoInstructionSelection()`（约 1282 行），再到 `CreateScheduler()`（约 2231 行）。
  - **和本章的关联**：这是「建 DAG → 选指令 → 调度」的主调用链。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - **读哪里**：`BuildSchedGraph()`（约 543 行）、`EmitSchedule()`（约 874 行）。
  - **和本章的关联**：第一层调度的建图和发射。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`**
  - **读哪里**：`ScheduleDAGMILive::schedule()`（约 1996 行）、`GenericScheduler` 的 `pickNode` / `tryCandidate`。
  - **和本章的关联**：第二层调度的主循环；`-debug-only=machine-scheduler` 的输出就是它打的。

- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - **读哪里**：`def ADD`（约 915 行）的 `Sched<[WriteIALU, ReadIALU, ReadIALU]>`。
  - **和本章的关联**：ISel 选出的 `ADD` 自带 `SchedRW`；scheduler 用它查模型。

- **`llvm/lib/Target/RISCV/RISCVSchedSiFive7.td`**
  - **读哪里**：`SiFive7SchedMachineModel`（约 1608 行）的 `IssueWidth = 2`、`MicroOpBufferSize = 0`、`LoadLatency = 3`；以及 `WriteRes<WriteIALU, [PipeAB]>`。
  - **和本章的关联**：解释为什么 `sifive-u74` 会提前 `add`。

### 演讲

- **LLVM Developers' Meeting: "Machine Instruction Scheduling"**（LLVM YouTube 频道）
  - **这讲什么**：MachineScheduler 的 region、策略、压力跟踪。
  - **为什么值得看**：先建立策略直觉，再读 `MachineScheduler.cpp` 的 `tryCandidate` 会快很多。

下一章先不急着进 SelectionDAG 内部，而是把三个选择器放在一起：SelectionDAG、FastISel、GlobalISel。你会发现「LLVM 指令选择」这句话在不同 opt level、不同 target 下指的不是同一个东西。

→ [Ch 2 SelectionDAG、FastISel、GlobalISel：到底谁在选择](./02-selectiondag-fastisel-globalisel.md)
