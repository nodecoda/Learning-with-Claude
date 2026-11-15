# Ch 2 — SelectionDAG、FastISel、GlobalISel：到底谁在选择

> **目标**：分清 LLVM 里三条 instruction selection 路线，知道 RISC-V 在当前版本实际走哪条、`-O0` 为什么不等于 FastISel、`-global-isel` 什么时候会失败。学完这章，别人说「LLVM 的指令选择」时，你会先反问一句：哪个 selector、哪个 opt level、哪个 target。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。RISC-V 在本构建里默认使用 SelectionDAG；GlobalISel 可用但仍是部分实现；RISC-V 没有 FastISel。

## 为什么需要这个？

「LLVM 的指令选择」不是一个单一模块。至少有三条路线：

- **SelectionDAG**：从 IR 建 DAG，combine、legalize，再用 pattern 选 MachineInstr。RISC-V 的默认路线。
- **FastISel**：一遍扫描，不做 DAG，追求编译速度，通常用于 `-O0`。但它是 target-specific 的，RISC-V 没有实现。
- **GlobalISel**：在 Machine IR 上做 legalize、regbankselect、instruction-select，目标是替代 SelectionDAG 的一部分。AArch64 支持较完整，RISC-V 仍在补。

面试官问「为什么这条 IR 选成这条指令」时，如果你不先确认 selector，答案可能完全跑偏。比如：

- 在 RISC-V 上看到 `-O0` 的 spill，不一定是 FastISel 的锅；RISC-V 根本没有 FastISel。
- 在 AArch64 `-O0` 看到很差的代码，可能是 FastISel，而不是 SelectionDAG。
- 在 RISC-V 上加 `-global-isel`，可能 Legalizer 直接报错，而不是「选得不好」。

这章把三条路线放在同一个函数上跑，让你看到 MIR 和最终汇编的差异。

## 先建立直觉

把 instruction selection 想成三种「翻译方式」：

```
LLVM IR ──► SelectionDAG ──► DAG Combine/Legalize ──► Pattern Match ──► MachineInstr
LLVM IR ──► FastISel（一遍扫描，target-specific）──────────────────► MachineInstr
LLVM IR ──► IRTranslator ──► Legalizer ──► RegBankSelect ──► InstructionSelect ──► MachineInstr
```

三种方式都会产出 MachineInstr，但：

- 产出的指令可能不同；
- 虚拟寄存器的编号和 `COPY` 的位置可能不同；
- 进入 MachineScheduler 的 MIR 不同，所以最终顺序也可能不同。

这就是为什么「换 selector」不只是「换选择算法」，它会改变 scheduling 的输入。

## 三条路线的对比

| 维度 | SelectionDAG | FastISel | GlobalISel |
|---|---|---|---|
| 默认用于 RISC-V？ | 是 | 否（RISC-V 无实现） | 否，需 `-global-isel` |
| 默认用于 X86/AArch64？ | `-O1` 以上通常是 | `-O0` 常用 | AArch64 `-O0` 可用；X86 部分 |
| 核心数据结构 | `SDNode` / `SelectionDAG` | 直接看 IR，逐条 lowering | `MachineIRBuilder` / gMIR |
| 主要阶段 | Builder → Combine → Legalize → ISel | 一遍扫描 | IRTranslator → Legalizer → RegBankSelect → InstructionSelect |
| 优点 | pattern 表达力强，成熟 | 编译快 | 保留 SSA、可复用 MI 优化 |
| 缺点 | 编译慢、DAG 内存开销 | 代码质量差、target 覆盖有限 | 仍在补，RISC-V 不完整 |
| 观察命令 | `-print-after-isel` | 无（RISC-V） | `-print-after=instruction-select` |

## SelectionDAG：RISC-V 的默认路线

调用链：

```
SelectionDAGISel::runOnMachineFunction()
  └─ SelectBasicBlock()
       └─ CodeGenAndEmitDAG()
            ├─ SelectionDAGBuilder::visit()       IR → DAG
            ├─ DAGCombiner::Run()                 DAG combine
            ├─ LegalizeDAG()                      type/op legalize
            ├─ DoInstructionSelection()           DAG → MachineInstr
            │    └─ RISCVDAGToDAGISel::Select()
            └─ ScheduleDAGSDNodes::Run()          第一层调度
                 └─ EmitSchedule()                发射 MachineInstr
```

源码落点：

- `llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`：`CodeGenAndEmitDAG()`（约 942 行）、`DoInstructionSelection()`（约 1282 行）。
- `llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp`：IR 到 DAG 的 lowering。
- `llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp`：combine。
- `llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp`：legalize。
- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`：`RISCVDAGToDAGISel::Select()`（约 1178 行）。

SelectionDAG 的特点：

- pattern 用 TableGen 写，表达力强；
- 可以做 DAG-level combine、load folding、 addressing mode matching；
- 但 DAG 构建和 combine 有编译时间/内存成本；
- 它的调度器 `ScheduleDAGSDNodes` 只给初始顺序，后面还有 MachineScheduler。

## FastISel：RISC-V 没有这条路

FastISel 的设计目标不是「选得更好」，而是「选得够快」。它不做 DAG，直接按 IR 指令逐条 lowering，适合 `-O0` 的调试构建。

但 FastISel 是 target-specific 的。当前源码里：

```text
llvm/lib/Target/X86/X86FastISel.cpp
llvm/lib/Target/AArch64/AArch64FastISel.cpp
```

`llvm/lib/Target/RISCV/` 下没有 `*FastISel*`。也就是说，**RISC-V 没有 FastISel 实现**。

在 RISC-V 上跑：

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O0 -fast-isel min.ll -o -
```

和 `-fast-isel=false` 的输出完全一样：

```asm
f:                                      # @f
	.cfi_startproc
# %bb.0:
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	a2, 8(sp)                       # 8-byte Folded Spill
	mv	a5, a1
	mv	a1, a0
	ld	a0, 8(sp)                       # 8-byte Folded Reload
	ld	a2, 0(a1)
	mul	a2, a2, a5
	ld	a1, 8(a1)
	mul	a1, a1, a3
	add	a0, a0, a4
	add	a0, a0, a2
	add	a0, a0, a1
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
.Lfunc_end0:
```

这不是 FastISel 的输出，是 **SelectionDAG 在 `-O0` 下的输出**。`sd a2, 8(sp)` 是 `-O0` 的寄存器分配/栈槽选择，不是 FastISel 的「快速选择」。

所以：

- 在 RISC-V 上说「`-O0` 用 FastISel」是错的。
- 在 X86/AArch64 上说「`-O0` 一定是 FastISel」也不严谨；要看 target 和 `-fast-isel` 是否真正启用。
- RISC-V 的 `-fast-isel` 更多是「尝试后回退」或「无实现，直接走 SelectionDAG」，不会给你一个独立的 FastISel 结果。

## GlobalISel：可用，但还不是全覆盖

GlobalISel 的流水线：

```
LLVM IR
  │ IRTranslator        生成 gMIR（generic Machine IR）
  ▼
gMIR
  │ Legalizer           把不合法的 generic op 变成合法的
  ▼
gMIR（Legalized）
  │ RegBankSelect       选寄存器 bank（gpr / fpr / ...）
  ▼
gMIR（RegBankSelected）
  │ InstructionSelect   用 matcher 选具体指令
  ▼
MachineInstr（Selected）
```

RISC-V 的实现文件在：

```text
llvm/lib/Target/RISCV/GISel/
├── RISCVCallLowering.cpp
├── RISCVInstructionSelector.cpp
├── RISCVLegalizerInfo.cpp
├── RISCVRegisterBankInfo.cpp
├── RISCVPreLegalizerCombiner.cpp
├── RISCVPostLegalizerCombiner.cpp
└── ...
```

跑 `min.ll`：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -global-isel -global-isel-abort=1 min.ll -o - \
  | sed -n '/^f:/,/^\.Lfunc_end/p'
```

实测：

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

和 SelectionDAG 的 `sifive-u74` 输出对比：

```asm
# SelectionDAG
add	a2, a2, a4
ld	a4, 0(a0)
ld	a0, 8(a0)
mul	a1, a4, a1
mul	a0, a0, a3
add	a1, a1, a2
add	a0, a0, a1

# GlobalISel
ld	a5, 0(a0)
add	a2, a2, a4
ld	a0, 8(a0)
mul	a1, a5, a1
mul	a0, a0, a3
add	a1, a1, a2
add	a0, a0, a1
```

指令种类一样，顺序不同。为什么？因为 GlobalISel 产出的 MIR 不同：虚拟寄存器编号、`COPY` 位置、指令的初始顺序都可能不同；MachineScheduler 看到的 DAG 和压力不同，最终顺序就不同。

看 GlobalISel 的 InstructionSelect 之后 MIR：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
  -global-isel -global-isel-abort=1 \
  -print-after=instruction-select min.ll -o /dev/null
```

实测（节选）：

```text
# *** IR Dump After InstructionSelect (instruction-select) ***:
# Machine code for function f: IsSSA, TracksLiveness, Legalized, RegBankSelected, Selected
Function Live Ins: $x10, $x11, $x12, $x13, $x14

bb.1 (%ir-block.0):
  liveins: $x10, $x11, $x12, $x13, $x14
  %0:gpr = COPY $x10
  %1:gpr = COPY $x11
  %2:gpr = COPY $x12
  %3:gpr = COPY $x13
  %4:gpr = COPY $x14
  %5:gpr = LD %0:gpr, 0 :: (load (s64) from %ir.p)
  %6:gpr = MUL %5:gpr, %1:gpr
  %9:gpr = LD %0:gpr, 8 :: (load (s64) from %ir.p1)
  %10:gpr = MUL %9:gpr, %3:gpr
  %11:gpr = ADD %4:gpr, %2:gpr
  %12:gpr = ADD %11:gpr, %6:gpr
  %13:gpr = ADD %12:gpr, %10:gpr
  $x10 = COPY %13:gpr
  PseudoRET implicit $x10
```

注意两点：

1. `bb.1` 而不是 `bb.0`：GlobalISel 的 IRTranslator 可能创建了额外的 entry block 或重命名。
2. 虚拟寄存器编号从 `%0` 开始，和 SelectionDAG 的 `%0..%11` 不同；`%7`、`%8` 被 Legalizer/RegBankSelect 用掉或删掉，所以 `LD` 是 `%9`。

这些差异会一路传到 MachineScheduler。**换 selector 不是只换一张 pattern 表，它改变了 MIR 的形状。**

## GlobalISel 不是全覆盖：一个真实的失败

写一个带 `shufflevector` 的函数：

```llvm
target triple = "riscv64-unknown-elf"

define <4 x i32> @shuf(<4 x i32> %a, <4 x i32> %b) {
  %r = shufflevector <4 x i32> %a, <4 x i32> %b,
                     <4 x i32> <i32 0, i32 5, i32 2, i32 7>
  ret <4 x i32> %r
}
```

用 GlobalISel + RVV：

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m,+v -O2 \
  -global-isel -global-isel-abort=1 hard2.ll -o - 2>&1
```

实测：

```text
LLVM ERROR: unable to legalize instruction:
%6:_(<vscale x 2 x s32>) = G_INSERT_SUBVECTOR %5:_, %4:_(<4 x s32>), 0
(in function: shuf)
```

这不是「选错了指令」，是 **Legalizer 根本还没支持这个 generic op**。在 RISC-V 上，GlobalISel 仍在补；遇到不支持的 op，可能 hard error，也可能回退到 SelectionDAG，取决于失败点。

所以：

- `-global-isel` 不是「更先进的 SelectionDAG」，它有自己的覆盖边界。
- 调试 GlobalISel 先加 `-global-isel-abort=1`，把静默回退变成显式失败。
- 看到 `G_INSERT_SUBVECTOR`、`G_*` 这类错误，先查 `RISCVLegalizerInfo.cpp` 和 `RISCVInstructionSelector.cpp` 有没有对应规则，而不是怀疑 scheduling。

## 三条路线的输出对比

同一个 `min.ll`，三种设置：

| 设置 | 输出特征 | 归因 |
|---|---|---|
| `-O2`（SelectionDAG） | `add` 在两个 `ld` 前面 | SelectionDAG ISel + MachineScheduler |
| `-O2 -global-isel` | `ld a5; add a2; ld a0; ...` | GlobalISel 产出不同 MIR，scheduler 结果不同 |
| `-O0` | 入口 `sd a2, 8(sp)`，有 spill | SelectionDAG at O0 + O0 寄存器分配，不是 FastISel |

把这三份输出放在一起看，你会意识到：

- 「指令选择」决定 opcode 和 MIR 形状；
- 「调度」决定顺序；
- 「寄存器分配」决定 spill 和物理寄存器；
- 三者不是独立的，但可以用 `-print-after-*` 分开看。

## 面试里怎么答

如果被问「LLVM 的指令选择是怎么做的」，一个及格答案：

> 看 target 和 opt level。RISC-V 默认是 SelectionDAG：IR 先建 DAG，combine/legalize 后用 TableGen pattern 选 MachineInstr，再由 `ScheduleDAGSDNodes` 给初始顺序，`MachineScheduler` 按 sched model 重排。`-O0` 在 RISC-V 上仍然是 SelectionDAG，因为 RISC-V 没有 FastISel。GlobalISel 需要 `-global-isel`，RISC-V 已有部分实现，但还会在 Legalizer 失败。

一个加分答案：

> 换 selector 会改变 MIR 的形状（虚拟寄存器、COPY、初始顺序），所以即使 sched model 相同，最终调度也可能不同。判断差异归属要看 `-print-after-isel` / `-print-after=instruction-select` 和 `-print-before/after=machine-scheduler`。

## 踩雷集锦

1. **以为 `-O0` 就是 FastISel**：RISC-V 没有 FastISel 实现，`-O0` 仍是 SelectionDAG。`sd a2, 8(sp)` 是 O0 的寄存器分配/栈槽，不是 FastISel。

2. **以为 `-global-isel` 一定更好**：RISC-V GlobalISel 还是部分实现；`shufflevector` 在 Legalizer 就可能失败。先看覆盖，再谈性能。

3. **不加 `-global-isel-abort=1`**：默认可能静默回退到 SelectionDAG，你看到的「GlobalISel 结果」其实是 SelectionDAG。调试时先显式 abort。

4. **把 GlobalISel 的 `bb.1` 当成异常**：IRTranslator 可能重命名/拆分 block；看 MIR 时先看 `selected: true` 和 `RegBankSelected` 标志，不要只看 block 编号。

5. **用 `-fast-isel` 在 RISC-V 上做对比**：没有实现，输出不会变。你要对比的是 `-O0` vs `-O2`，或者 `-global-isel` vs SelectionDAG。

6. **把 selector 差异当成 scheduler 差异**：GlobalISel 和 SelectionDAG 的最终顺序不同，第一嫌疑是 MIR 形状不同，不是 `MachineScheduler` 突然变了。

## 动手练习

1. 用 `min.ll` 跑三次：`-O2`、`-O2 -global-isel`、`-O0`。把三份 MIR（`-print-after-isel` / `-print-after=instruction-select`）和最终汇编放在一起，标出哪些差异来自 selection、哪些来自 RA。

2. 用 `hard2.ll` 的 `shuf` 跑 `-global-isel -global-isel-abort=1`，记录错误。然后去掉 `-global-isel`，看 SelectionDAG 能不能编过。解释为什么「GlobalISel 失败」不等于「这个 IR 不能生成代码」。

3. 在 X86 或 AArch64 上跑 `-O0 -fast-isel` 和 `-O0 -fast-isel=false`，对比输出。你会看到 FastISel 的真实效果；再回到 RISC-V，确认没有这个差异。

4. 找一个 RISC-V GlobalISel 能编过、但输出和 SelectionDAG 不同的函数，用 `-print-before/after=machine-scheduler` 看顺序差异是 MIR 形状导致的，还是 scheduler 策略导致的。

5. 用 `-global-isel-abort=2` 跑 `hard2.ll`，看错误信息和 `=1` 有什么不同。查 `llc --help-hidden` 里 `-global-isel-abort` 的说明。

## 本章重点整理

- RISC-V 默认使用 SelectionDAG；`-O0` 也是 SelectionDAG，因为没有 FastISel 实现。
- FastISel 是 target-specific 的，X86/AArch64 有，RISC-V 没有。
- GlobalISel 是 opt-in，RISC-V 已有部分实现，但 Legalizer/Selector 仍会失败。
- 换 selector 会改变 MIR 形状，进而改变 MachineScheduler 的输入和输出。
- 调试 GlobalISel 先加 `-global-isel-abort=1`，不要相信静默回退的结果。

## 自我检核

- [ ] 能说出三条 instruction selection 路线各自的核心阶段
- [ ] 能解释为什么 RISC-V 的 `-O0` 不是 FastISel
- [ ] 能说出至少两个 RISC-V GlobalISel 的限制或失败模式
- [ ] 能用 `-print-after-isel` 和 `-print-after=instruction-select` 对比两种 selector 的 MIR
- [ ] 能解释「换 selector 为什么会影响最终调度」
- [ ] 知道在面试里被问「LLVM 指令选择」时，先确认 target 和 opt level

## 延伸阅读

### 官方文档

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)**
  - **读哪里**：「SelectionDAG」和「Global Instruction Selection」两节。
  - **和本章的关联**：官方对两条路线的定位；读完你会知道 GlobalISel 不是 SelectionDAG 的简单替代。

- **[GlobalISel 文档](https://llvm.org/docs/GlobalISel/index.html)**
  - **读哪里**：`IRTranslator`、`Legalizer`、`RegBankSelect`、`InstructionSelect` 四页。
  - **和本章的关联**：本章的 GlobalISel 流水线就是这四页的压缩版。

- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`selected:`、`legalized:`、`regBankSelected:` 这些 MachineFunction 属性。
  - **和本章的关联**：`-print-after=instruction-select` 输出里的 `Selected` 就是这些属性。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - **读哪里**：`CodeGenAndEmitDAG()`（约 942 行）。
  - **和本章的关联**：SelectionDAG 的主流程。

- **`llvm/lib/Target/X86/X86FastISel.cpp` / `llvm/lib/Target/AArch64/AArch64FastISel.cpp`**
  - **读哪里**：类的入口和 `selectInstruction` 相关函数。
  - **和本章的关联**：对比 RISC-V 没有这个文件，理解 FastISel 的 target-specific 性。

- **`llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp`**
  - **读哪里**：`RISCVLegalizerInfo::RISCVLegalizerInfo()` 里的 legalize 规则。
  - **和本章的关联**：`G_INSERT_SUBVECTOR` 失败时，先看这里有没有规则。

- **`llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp`**
  - **读哪里**：`selectImpl()` 和 `select()` 的入口。
  - **和本章的关联**：GlobalISel 的 RISC-V 指令选择实现。

### 演讲

- **"Global Instruction Selection"**（LLVM Developers' Meeting）
  - **这讲什么**：GlobalISel 的设计目标、与 SelectionDAG 的取舍。
  - **为什么值得看**：理解为什么 LLVM 要维护两套 selector，而不是直接把 SelectionDAG 重写。

下一章进入 RISC-V 的 ISA 约束。你会看到「没有 indexed addressing」「12-bit immediate」「扩展指令」如何直接改变 selection，并进一步改变 scheduling 的自由度。

→ [Ch 3 RISC-V 的 ISA 约束如何塑造选择](./03-riscv-isa-constraints.md)
