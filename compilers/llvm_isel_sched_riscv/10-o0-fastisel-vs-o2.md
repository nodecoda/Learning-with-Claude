# Ch 10 — `-O0`/FastISel 与 `-O2`：为什么 O0 也会 spill

> **目标**：拆掉两个常见误解：「`-O0` 等于不优化」和「`-O0` 会用 FastISel」。学完这章，你能解释同一段 IR 在 `-O0` 为什么多出 spill、为什么 `-regalloc=greedy` 能让它变回 `-O2` 的样子、为什么 RISC-V 上 `-fast-isel` 开关没有效果，以及 `-O0` 到底还保留了哪些 SelectionDAG 行为。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章用 `min.ll` 和 `combine.ll`，统一 `-mtriple=riscv64 -mcpu=generic -mattr=+m`。寄存器分配日志用 `-debug-only=regalloc`，isel 日志用 `-debug-only=isel`，调度日志用 `-debug-only=pre-RA-sched` / `-debug-only=machine-scheduler`。

## 为什么需要这个？

面试里经常出现这种对话：

> 面试官：「为什么 `-O0` 这段代码 spill 了？」
>
> 候选人：「因为 `-O0` 不做优化。」
>
> 面试官：「那为什么 `-O0 -regalloc=greedy` 就不 spill 了？」

或者：

> 候选人：「`-O0` 用 FastISel，所以指令选得差。」
>
> 面试官：「RISC-V 有 FastISel 吗？」

这两个问题的答案都不在 instruction selection 本身：

- `-O0` 的 spill 主要来自 **fast register allocator**，不是 ISel。
- RISC-V **没有 FastISel 实现**，`-fast-isel` 开关不会改变选择路径。

这一章用同一段 IR 跑四种配置，把「谁造成差异」拆开。

## 先建立直觉

`-O0` 和 `-O2` 的流水线差异：

```text
-O0:
  IR（几乎没有 InstCombine/GVN）
    -> SelectionDAG（combine/legalize 仍然跑）
    -> DAG source scheduler
    -> Fast register allocator
    -> 汇编（可能 spill）

-O2:
  IR（InstCombine/GVN/LICM...）
    -> SelectionDAG（combine/legalize）
    -> DAG source scheduler
    -> MachineScheduler
    -> Greedy register allocator
    -> 汇编
```

关键点：

- `-O0` 仍然跑 SelectionDAG、combine、legalize、DAG scheduler。
- `-O0` 默认用 Fast regalloc；`-O2` 默认用 Greedy regalloc。
- `-O0` 不跑 MachineScheduler；`-O2` 跑。
- FastISel 是另一条 instruction selection 路径，RISC-V 没有实现它。

## 同一段 IR，四种输出

`min.ll`：

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

### `-O0` 默认

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 min.ll -o -
```

```asm
f:
	addi	sp, sp, -16
	sd	a2, 8(sp)          # 8-byte Folded Spill
	mv	a5, a1
	mv	a1, a0
	ld	a0, 8(sp)          # 8-byte Folded Reload
	ld	a2, 0(a1)
	mul	a2, a2, a5
	ld	a1, 8(a1)
	mul	a1, a1, a3
	add	a0, a4, a0
	add	a0, a0, a2
	add	a0, a0, a1
	addi	sp, sp, 16
	ret
```

有一个 spill/reload 对：

```asm
sd	a2, 8(sp)
ld	a0, 8(sp)
```

`a2` 是第三个参数 `%c`。它被存到栈上，再读回来。

### `-O0 -regalloc=greedy`

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 \
  -regalloc=greedy min.ll -o -
```

```asm
f:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
```

和 `-O2` 的输出逐行相同：

```asm
f:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
```

### `-O0 -regalloc=basic`

```asm
f:
	mv	a5, a4
	mv	a4, a3
	ld	a3, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a3, a1
	mul	a3, a0, a4
	add	a0, a5, a2
	add	a0, a0, a1
	add	a0, a0, a3
	ret
```

没有 spill，但多了 `mv`。这是 basic allocator 的分配策略造成的。

### `-O2`

```asm
f:
	ld	a5, 0(a0)
	ld	a0, 8(a0)
	mul	a1, a5, a1
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
```

四组输出说明：

- `-O0` 的 spill 不是不可避开的；换 Greedy 就消失。
- `-O0 -regalloc=greedy` 和 `-O2` 相同，说明这段 IR 的 ISel 结果在两种优化级别下一致（至少这个函数）。
- `-regalloc=basic` 又是一版，说明 allocator 策略直接改变最终汇编。

## Fast regalloc 在做什么

`-O0 -debug-only=regalloc` 的第一行：

```text
********** FAST REGISTER ALLOCATION **********
********** Function: f
```

Fast regalloc 的特点是：

- 单遍扫描，按指令顺序分配。
- 不做全局 live interval 分析，不做 live range splitting。
- 遇到冲突就 spill，选择当前可用的寄存器。
- 不追求最少 spill，也不做复杂 coalescing。

对 `f` 来说，入口有五个参数：

```text
%p -> $x10 (a0)
%a -> $x11 (a1)
%b -> $x12 (a2)
%c -> $x13 (a3)
%d -> $x14 (a4)
```

函数体需要同时保留 `%a`、`%b`、`%c`、`%d` 和两个 load 结果，寄存器压力超过可用 caller-saved 寄存器。Fast regalloc 在顺序扫描时把 `%c` spill 到 `8(sp)`，后面再 reload。

Greedy regalloc 会做 live interval 分析、拆分和 coalescing，能找到不需要 spill 的分配。所以：

```text
-O0 默认 spill 的主因 = Fast regalloc，不是 SelectionDAG
```

## RISC-V 没有 FastISel

FastISel 是 SelectionDAG 之外的一条快速 instruction selection 路径，目标是 `-O0` 时跳过 DAG，直接生成 MachineInstr。它需要 target 自己实现。

RISC-V 的 target 目录里没有 `RISCVFastISel.cpp`。`TargetLowering::createFastISel` 的默认实现返回 `nullptr`：

```cpp
virtual FastISel *createFastISel(FunctionLoweringInfo &,
                                 const TargetLibraryInfo *,
                                 const LibcallLoweringInfo *) const {
  return nullptr;
}
```

AArch64、X86、ARM 等 target 会 override 它；RISC-V 没有 override。因此：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 -fast-isel min.ll -o -
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 -fast-isel=false min.ll -o -
```

两条命令的输出完全相同，都是前面那个带 spill 的版本。

`-debug-only=isel` 会打印：

```text
FastISel is enabled
...
Enabling fast-isel
```

但接下来仍然是：

```text
Initial selection DAG: %bb.0 'f:'
```

因为 `createFastISel` 返回空，`SelectionDAGISel` 回落到 DAG 路径。日志里的 `Enabling fast-isel` 只表示「选项打开了」，不表示「target 真的用了 FastISel」。

这就是一个典型的「开关存在 ≠ 功能可用」。面试里如果被问「RISC-V 的 FastISel 怎么工作」，正确回答是「RISC-V 没有 FastISel 实现；`-fast-isel` 不会改变选择路径」。

## `-O0` 仍然跑 SelectionDAG

`-O0` 不等于「跳过 DAG」。用 `combine.ll` 的 `mul2`：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 \
  -print-after-isel combine.ll -o /dev/null
```

`-O0` 的 isel 后 MIR：

```text
bb.0 (%ir-block.0):
  %0:gpr = COPY $x10
  %1:gpr = SLLI %0:gpr, 1
  $x10 = COPY %1:gpr
  PseudoRET implicit $x10
```

`mul x, 2` 仍然被 DAG combine 成 `shl`，最终是 `slli`。`-O0 -debug-only=dagcombine` 也有 48 条 `Combining` 日志（本机实测）。

所以 `-O0` 仍然保留：

- SelectionDAGBuilder 的 lowering。
- DAG combine（至少 target-independent 的部分）。
- 类型合法化、操作合法化。
- DAG source scheduler（`-O0` 强制 source scheduler）。
- Fast regalloc。

`-O0` 少掉的主要是：

- IR 层的 InstCombine/GVN/LICM 等优化。
- MachineScheduler（本机实测 `-O0 -debug-only=machine-scheduler` 没有输出）。
- Greedy regalloc 的全局分配。

## 为什么 `-O0` 不跑 MachineScheduler

`-O0` 的 pass pipeline 不注册 MachineScheduler；`-enable-misched` 默认值虽然是 true，但 `-O0` 的 pipeline 不会调用它。验证：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 \
  -debug-only=machine-scheduler min.ll -o /dev/null
```

本机实测没有输出。相反：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O0 \
  -debug-only=pre-RA-sched min.ll -o /dev/null
```

会打印：

```text
********** List Scheduling %bb.0 '' **********
SU(0): t24: ch = PseudoRET ...
```

也就是说，`-O0` 的初始顺序仍由 SelectionDAG 的 DAG scheduler 决定，MachineScheduler 不参与。

这解释了一个常见现象：`-O0` 的指令顺序通常更接近源码顺序。不是「没有调度」，而是只有 source DAG scheduler，没有第二层 MachineScheduler。

## 用 allocator 隔离变量

想知道一段 `-O0` 汇编的差异是 ISel 还是 RA 造成的，最直接的方法固定其他变量：

```bash
# 只换 allocator，其他不变
"$LLC" ... -O0 -regalloc=fast   min.ll -o -
"$LLC" ... -O0 -regalloc=basic  min.ll -o -
"$LLC" ... -O0 -regalloc=greedy min.ll -o -

# 只换优化级别，allocator 固定
"$LLC" ... -O0 -regalloc=greedy min.ll -o -
"$LLC" ... -O2 -regalloc=greedy min.ll -o -
```

本机结果：

- `-O0 -regalloc=greedy` 与 `-O2` 相同。
- `-O0 -regalloc=basic` 是另一版，没有 spill，但多 `mv`。
- `-O0` 默认（fast）有 spill。

这个实验能直接回答「spill 是谁造成的」：把 allocator 换成 greedy，spill 消失，说明主因是 allocator，不是 ISel。

## 源码落点

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - `SelectAllBasicBlocks()`（约 1655 行）：`if (TM.Options.EnableFastISel)` 创建 FastISel。
  - `FastIS = TLI->createFastISel(...)`：返回空则回落 DAG。
  - **读哪里**：看 `FastIS` 为空时的分支。

- **`llvm/include/llvm/CodeGen/TargetLowering.h`**
  - `createFastISel()` 默认实现（约 5496 行）返回 `nullptr`。
  - **读哪里**：确认「target 不 override 就没有 FastISel」。

- **`llvm/lib/CodeGen/RegAllocFast.cpp`**
  - Fast regalloc 的主循环、spill 策略。
  - **读哪里**：`FAST REGISTER ALLOCATION` 日志对应的分配逻辑。

- **`llvm/lib/CodeGen/RegAllocGreedy.cpp`**
  - Greedy regalloc 的 live interval、split、evict。
  - **读哪里**：为什么同样 IR 不 spill。

- **`llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`**
  - `addFastRegAlloc()`（约 583 行）：RISC-V 的 fast RA 配置。
  - **读哪里**：确认 `-O0` 默认走哪条 RA 路径。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - `createDefaultScheduler()`（约 302 行）：`OptLevel == None` 强制 source scheduler。
  - **读哪里**：解释 `-O0` 只有 DAG scheduler、没有 MachineScheduler。

## 踩雷集锦

1. **以为 `-O0` 等于不优化**：SelectionDAG 的 combine/legalize 仍然跑，`mul x, 2` 仍然变 `slli`。

2. **以为 `-O0` 一定用 FastISel**：RISC-V 没有 FastISel，`-fast-isel` 不会改变路径。

3. **把 `-fast-isel` 和 `-fast-isel=false` 输出相同当成 bug**：这是 target 不支持 FastISel 的正常表现。

4. **把 `-O0` 的 spill 归因于 instruction selection**：先换 `-regalloc=greedy` 试一次；如果 spill 消失，主因是 RA。

5. **以为 `-O0 -regalloc=greedy` 等于 `-O2`**：这个函数恰好相同，不代表所有函数相同；IR 优化和 MachineScheduler 仍然不同。

6. **以为 `-O0` 没有调度**：DAG source scheduler 仍然在跑，只是没有 MachineScheduler。

7. **以为 `-O0` 的 `COPY` 都会保留**：Fast regalloc 之后还有 coalescing/copy elimination，部分 `COPY` 会消失。

8. **用 `-O0` 的汇编推断 `-O2` 的 ISel**：两边的 IR 形状不同，DAG 可能完全不同。要研究 ISel，用 `-O2`；要研究 RA，才用 `-O0`。

## 动手练习

1. 用 `min.ll` 跑 `-O0`、`-O0 -regalloc=greedy`、`-O0 -regalloc=basic`、`-O2`，记录四组汇编。标出哪一组有 spill，哪一组有额外 `mv`。

2. 用 `-O0 -debug-only=regalloc` 找到 `FAST REGISTER ALLOCATION`，跟踪 `%c` 被 spill 的位置。再用 `-O0 -regalloc=greedy -debug-only=regalloc` 看 Greedy 的分配。

3. 用 `combine.ll` 跑 `-O0 -print-after-isel`，确认 `mul x, 2` 仍然变 `slli`。再用 `-O0 -debug-only=dagcombine` 数 `Combining` 行数。

4. 用 `-O0 -debug-only=machine-scheduler` 和 `-O0 -debug-only=pre-RA-sched` 各跑一次，确认前者没有输出、后者有 SUnit 日志。

5. 用 `-O0 -fast-isel` 和 `-O0 -fast-isel=false` 跑同一个函数，比较汇编和 `-debug-only=isel` 的 `FastISel is enabled/disabled`。解释为什么输出相同。

6. **guided**：在 `RISCVDAGToDAGISel` 里加一个假的 `createFastISel` override，返回 `nullptr`，确认行为不变；再尝试实现一个只处理 `ret i64 0` 的 FastISel，观察 `-fast-isel` 是否生效。不要提交这个实验代码。

## 本章重点整理

- `-O0` 仍然跑 SelectionDAG、combine、legalize、DAG source scheduler；少掉的是 IR 优化、MachineScheduler 和 Greedy RA。
- `-O0` 默认用 Fast regalloc，容易 spill；`-regalloc=greedy` 可以让同一段 IR 不 spill。
- RISC-V 没有 FastISel 实现；`TargetLowering::createFastISel` 默认返回 `nullptr`，`-fast-isel` 不会改变选择路径。
- `-O0 -regalloc=greedy` 和 `-O2` 在 `min.ll` 上相同，说明差异主要来自 RA；不要推广到所有函数。
- `-O0` 的指令顺序来自 DAG source scheduler，不是「没有调度」。
- 分析 `-O0` 汇编时，先固定 allocator，再判断是 ISel、scheduler 还是 RA 的问题。

## 自我检核

- [ ] 能解释 `-O0` 为什么仍然可能 spill，以及 Fast/Greedy RA 的差异
- [ ] 能说出 `-O0` 仍然保留哪些 SelectionDAG 行为
- [ ] 能解释 RISC-V 为什么没有 FastISel，以及 `-fast-isel` 为什么无效
- [ ] 能用 `-regalloc=greedy|basic|fast` 隔离 RA 变量
- [ ] 能解释 `-O0` 为什么没有 MachineScheduler，但仍有 DAG scheduler
- [ ] 能说出 `-O0` 和 `-O2` 在这段 IR 上相同的条件
- [ ] 能避免把 `-O0` 的 spill 归因于 instruction selection

## 延伸阅读

### 官方文档

- **[LLVM Code Generator — Register Allocation](https://llvm.org/docs/CodeGenerator.html#register-allocation)**
  - **读哪里**：Fast、Basic、Greedy、PBQP 的定位。
  - **和本章的关联**：`-O0` spill 的主因。

- **[LLVM Code Generator — Instruction Selection](https://llvm.org/docs/CodeGenerator.html#instruction-selection-section)**
  - **读哪里**：FastISel 和 SelectionDAG 的关系。
  - **和本章的关联**：RISC-V 没有 FastISel 的背景。

- **[LLVM Command Guide — llc](https://llvm.org/docs/CommandGuide/llc.html)**
  - **读哪里**：`-O0`、`-regalloc`、`-fast-isel` 的选项说明。
  - **和本章的关联**：实验参数。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - **读哪里**：`SelectAllBasicBlocks()`（约 1655 行）、`FastIS = TLI->createFastISel(...)`。
  - **和本章的关联**：FastISel 的创建和回落。

- **`llvm/include/llvm/CodeGen/TargetLowering.h`**
  - **读哪里**：`createFastISel()` 默认实现（约 5496 行）。
  - **和本章的关联**：RISC-V 没有 override，所以返回 `nullptr`。

- **`llvm/lib/CodeGen/RegAllocFast.cpp`**
  - **读哪里**：`RegAllocFast::run()`、`allocVirtReg()`、spill 逻辑。
  - **和本章的关联**：`FAST REGISTER ALLOCATION` 日志。

- **`llvm/lib/CodeGen/RegAllocGreedy.cpp`**
  - **读哪里**：live interval、split、evict。
  - **和本章的关联**：为什么 Greedy 不 spill。

- **`llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`**
  - **读哪里**：`addFastRegAlloc()`（约 583 行）。
  - **和本章的关联**：RISC-V 的 `-O0` RA 路径。

### 文章 / 演讲

- **"Register Allocation in LLVM"**（LLVM Developers' Meeting 演讲）
  - **这讲什么**：Greedy、Basic、Fast 的设计和取舍。
  - **为什么值得看**：理解为什么换 allocator 会改变 spill。

- **"FastISel: A Fast Instruction Selector"**（LLVM 后端演讲/博客）
  - **这讲什么**：FastISel 的设计、适用场景、target 支持。
  - **和本章的关联**：为什么 RISC-V 没有它。

Part 1 到这里结束。下一部分进入 MachineScheduler：它会用 sched model 重新排列 isel 产出的 MI。你会看到 `SchedRead/Write`、`WriteRes`、`ReadAdvance` 三层模型，RISC-V 的 Rocket/SiFive7/GenericOOO，以及 `llvm-mca` 如何把模型变成周期数。

→ [练习 B：用 `-pre-RA-sched` 六种模式跑同一段 IR](./practice-b-pre-ra-sched-modes.md)
