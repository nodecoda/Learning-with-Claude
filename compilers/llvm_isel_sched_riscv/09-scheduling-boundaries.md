# Ch 9 — 调度边界：内存、副作用、fence、atomics

> **目标**：看懂 scheduler 的「不能动」边界从哪里来。学完这章，你能区分普通内存依赖、`volatile`、`fence`、atomic RMW 四种约束，能读懂 SUnit 图里的 `Ord` / `Barrier` / `Memory`，也能解释为什么 `atomicrmw` 在没有 `A` 扩展时会变成一个函数调用。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章用 `mem.ll`、`volatile.ll`，统一 `-mtriple=riscv64 -mcpu=generic -mattr=+m,+a -O2`。DAG 日志用 `-debug-only=isel`，SUnit 日志用 `-enable-misched=false -debug-only=pre-RA-sched`，MachineScheduler 日志用 `-debug-only=machine-scheduler`。

## 为什么需要这个？

Instruction selection 决定了「有哪些指令」，scheduling 决定了「怎么排」。但有一类指令，scheduler 从一开始就没有自由：

- `store` 不能越过可能别名的 `load`。
- `volatile` 访问不能删除、不能合并、不能随意重排。
- `fence` 两侧的内存操作不能跨过它。
- atomic RMW 既是 load 又是 store，还带内存序。

这些约束不是 scheduler 自己猜的，而是前面阶段用 chain、`AtomicFence`、`MemOperand` 和 `SDep` 表达出来的。面试里常见的问法：

- 「为什么这条 `ld` 不能提到 `sd` 前面？」
- 「`fence` 在 DAG 里长什么样？scheduler 怎么知道不能跨？」
- 「`atomicrmw` 为什么有时候是 `amoadd.d`，有时候是 `__atomic_fetch_add_8`？」
- 「`volatile` 和 atomic 有什么区别？」

这一章用真实日志回答。

## 先建立直觉

把依赖分成三层：

```text
数据依赖：值从 A 流到 B
          A 算完，B 才能算

普通内存依赖：load/store 可能访问同一块内存
          顺序由 alias analysis + chain 决定
          scheduler 可以在不违反依赖的前提下重排

强制顺序：volatile / fence / atomic
          用 Barrier 边表达
          scheduler 不能跨过
```

在 SUnit 图里，这三种约束最终都表现为 `SDep`，但强弱不同：

```text
Data Latency=1       值依赖
Ord  Latency=0 Memory  普通内存顺序
Ord  Latency=1 Barrier 强制屏障
```

`Barrier` 是 `Order` 边上的一个标记：它不仅要求「先后」，还要求「不能为了调度把中间塞满或跨过去」。普通 `Memory` 边允许 scheduler 在满足依赖的前提下做更大的重排。

## 普通内存：`store → load → store`

`mem.ll`：

```llvm
target triple = "riscv64-unknown-elf"

define void @mem(ptr %p, ptr %q, i64 %x) {
  store i64 %x, ptr %p
  %v = load i64, ptr %q
  store i64 %v, ptr %p
  ret void
}
```

初始 DAG（节选）：

```text
t9: ch = store<(store (s64) into %ir.p)> t0, t6, t2, poison:i64
t10: i64,ch = load<(load (s64) from %ir.q)> t9, t4, poison:i64
t11: ch = store<(store (s64) into %ir.p)> t10:1, t10, t2, poison:i64
```

读法：

- 第一个 store 的输入 chain 是 `t0`。
- load 的输入 chain 是 `t9`，所以它排在第一个 store 后面。
- 第二个 store 的输入 chain 是 `t10:1`，所以它排在 load 后面。

为什么 load 不能提前？因为 `%p` 和 `%q` 可能别名。编译器没有证据证明它们不重叠，只能保守地串行化。

SUnit 日志（`-enable-misched=false -debug-only=pre-RA-sched`）：

```text
SU(3): t10: i64,ch = LD<Mem:(load (s64) from %ir.q)> t4, TargetConstant:i64<0>, t9
  Preds:
    SU(6): Data Latency=1
    SU(4): Ord  Latency=1 Barrier

SU(4): t9: ch = SD<Mem:(store (s64) into %ir.p)> t6, t2, TargetConstant:i64<0>, t0
  Succs:
    SU(3): Ord  Latency=1 Barrier
```

注意这里显示的是 `Barrier`，不是 `Memory`。DAG scheduler 把可能别名的 store→load 视为强顺序；MachineScheduler 的普通内存依赖则会显示 `Ord Latency=0 Memory`。两者语义相关，但边类型和强度不同。

最终汇编：

```asm
mem:
	sd	a2, 0(a0)
	ld	a1, 0(a1)
	sd	a1, 0(a0)
	ret
```

顺序和源码一致。scheduler 没有把 load 提前，因为 chain 和 `Barrier` 边不允许。

## `volatile`：不能删、不能合并、不能随便排

把上面的 load/store 加上 `volatile`：

```llvm
define i64 @vol(ptr %p, ptr %q) {
  %a = load volatile i64, ptr %p
  store volatile i64 %a, ptr %q
  ret i64 %a
}
```

初始 DAG：

```text
t7: i64,ch = load<(volatile load (s64) from %ir.p)> t0, t2, poison:i64
t8: ch = store<(volatile store (s64) into %ir.q)> t7:1, t7, t4, poison:i64
```

和普通 load/store 的 DAG 看起来很像，但有两个关键差异：

1. `load` 的类型是 `volatile load`，`store` 是 `volatile store`；这些标记会进入 `MemOperand`。
2. DAG combine 不会删除 `store(load)`，也不会把两个 volatile 访问合并。第 5 章的 `storeload` 优化在这里不成立。

最终汇编：

```asm
vol:
	ld	a0, 0(a0)
	sd	a0, 0(a1)
	ret
```

SUnit 日志：

```text
SU(1): t7: i64,ch = LD<Mem:(volatile load (s64) from %ir.p)>
  Succs:
    SU(3): Ord  Latency=1 Barrier

SU(3): t8: ch = SD<Mem:(volatile store (s64) into %ir.q)>
  Preds:
    SU(1): Ord  Latency=1 Barrier
```

`volatile` 的语义不是「更快」或「更慢」，而是「每次访问都必须发生，且不能被优化掉」。编译器不能因为「load 出来的值马上 store 回去」就删掉 store；硬件也可能依赖这次访问的副作用。

## `fence`：DAG 里的 `AtomicFence`

```llvm
define i64 @fence2(ptr %p, ptr %q) {
  %a = load i64, ptr %p
  fence seq_cst
  %b = load i64, ptr %q
  %s = add i64 %a, %b
  ret i64 %s
}
```

初始 DAG：

```text
t7: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
t10: ch = AtomicFence t7:1, TargetConstant:i64<7>, TargetConstant:i64<1>
t11: i64,ch = load<(load (s64) from %ir.q)> t10, t4, poison:i64
```

三个信息：

1. `AtomicFence` 接收前一个 load 的 chain `t7:1`，产生新的 chain `t10`。
2. 第二个 load 的输入 chain 是 `t10`，所以它必须排在 fence 后面。
3. `TargetConstant<7>` 是 fence 的 ordering code（`7` 对应 `seq_cst`），第二个 `TargetConstant<1>` 是 target immediate。RISC-V 的 pattern 把它映射成 `(FENCE 0b11, 0b11)`，也就是 predecessor/successor 都是 `rw`，最终汇编是 `fence rw, rw`。

SUnit 日志：

```text
SU(4): t7: LD p
  Succs:
    SU(1): Data Latency=1
    SU(6): Ord  Latency=1 Barrier

SU(6): t10: ch = FENCE TargetConstant:i64<3>, TargetConstant:i64<3>, t7:1
  Preds:
    SU(4): Ord  Latency=1 Barrier
  Succs:
    SU(0): Ord  Latency=1 Barrier
    SU(2): Ord  Latency=1 Barrier

SU(2): t11: LD q
  Preds:
    SU(6): Ord  Latency=1 Barrier
```

`FENCE` 在 SUnit 图里是一个真正的屏障节点：它和前后内存操作之间都是 `Barrier` 边。scheduler 不能把第二个 load 提前到 fence 前面，也不能把第一个 load 推到 fence 后面。

最终汇编：

```asm
fence2:
	ld	a0, 0(a0)
	fence	rw, rw
	ld	a1, 0(a1)
	add	a0, a0, a1
	ret
```

注意：`fence` 不是 `AtomicFence` 的最终名字。DAG 里是 target-independent 的 `AtomicFence`，isel 后变成 RISC-V 的 `FENCE`，MC 层打印成 `fence rw, rw`。

## Atomic RMW：有 `A` 和没有 `A` 是两种结构

```llvm
define i64 @amo(ptr %p) {
  %v = atomicrmw add ptr %p, i64 1 seq_cst
  ret i64 %v
}
```

### 有 `+a`：`AMOADD_D_AQRL`

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m,+a -O2 mem.ll -o -
```

最终汇编：

```asm
amo:
	li	a1, 1
	amoadd.d.aqrl	a0, a1, (a0)
	ret
```

`-print-after-isel`：

```text
%0:gpr = COPY $x10
%1:gpr = ADDI $x0, 1
%2:gpr = AMOADD_D_AQRL %1:gpr, %0:gpr :: (load store seq_cst (s64) on %ir.p)
$x10 = COPY %2:gpr
PseudoRET implicit $x10
```

`AMOADD_D_AQRL` 的 `MemOperand` 同时带 `load` 和 `store`，还有 `seq_cst`。它在 SUnit 图里是一个内存操作：

```text
SU(1): t4: i64,ch = AMOADD_D_AQRL<Mem:(load store seq_cst (s64) on %ir.p)>
  Predecessors:
    SU(3): Data Latency=1
    SU(2): Data Latency=1
  Successors:
    SU(0): Ord  Latency=1 Barrier
    SU(0): Data Latency=1
```

因为它既是 load 又是 store，所以它和前后内存操作之间有顺序约束；`aqrl` 又把它变成 acquire/release 语义的屏障。

### 没有 `+a`：libcall

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 mem.ll -o -
```

最终汇编：

```asm
amo:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	li	a1, 1
	li	a2, 5
	call	__atomic_fetch_add_8
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

这不是「少了一条指令」，而是完全不同的结构：

- 有 `A`：一条 `amoadd.d.aqrl`，在基本块内完成，scheduler 可以把它和相邻指令一起排。
- 没有 `A`：一次函数调用，有 `ra` 保存/恢复、参数设置、调用边界；scheduling region 被调用切分，寄存器分配压力也不同。

同一个 IR，`-mattr=+a` 和没有 `A` 的差异，不是 pattern 的差异，而是 **operation legalization / libcall lowering** 的差异。第 5 章的 `mul` 无 `M` 也是同一类现象。

## MachineScheduler 里的 `Memory` 和 `Barrier`

DAG scheduler 和 MachineScheduler 都会建内存依赖，但日志用词不同。`mem.ll` 的 MachineScheduler 日志：

```text
SU(3):   SD %2:gpr, %0:gpr, 0 :: (store (s64) into %ir.p)
  Successors:
    SU(5): Ord  Latency=0 Memory
    SU(4): Ord  Latency=1 Memory

SU(4):   LD %1:gpr, 0 :: (load (s64) from %ir.q)
  Predecessors:
    SU(3): Ord  Latency=1 Memory
```

这里有 `Ord Latency=0 Memory` 和 `Ord Latency=1 Memory` 两种。读法：

- `Ord`：顺序依赖，不是数据依赖。
- `Memory`：来自普通内存别名分析，不是强屏障。
- `Latency=0`：这条边本身不增加延迟，scheduler 可以在满足依赖的前提下重排。
- `Barrier`：强制屏障，不能跨过。

`fence` 的 MachineScheduler 日志则是：

```text
SU(3):   FENCE 3, 3
  Successors:
    SU(4): Ord  Latency=0 Barrier
```

`volatile` 的日志：

```text
SU(2):   LD ... (volatile load ...)
  Successors:
    SU(3): Ord  Latency=0 Barrier

SU(3):   SD ... (volatile store ...)
  Predecessors:
    SU(2): Ord  Latency=0 Barrier
```

所以判断一条边有多强，先看 `SDep::Kind` 和 `isBarrier()`，不要只看 `Ord`。

## hazard：资源冲突不是正确性约束

MachineScheduler 日志里还有：

```text
hazard: SU(4) ReadyCycle = 4 is later than CurrCycle = 0 on an unbuffered resource
hazard: SU(3) ReadyCycle = 5 is later than CurrCycle = 4 on an unbuffered resource
```

`hazard` 是资源/流水线冲突，不是内存正确性。它表示「这个 SUnit 最早要到 ReadyCycle 才能发射，当前周期太早」。scheduler 会插入等待或换一个节点。

这类信息和 `Barrier` 要分开：

- `Barrier`：不能跨过的顺序约束。
- `hazard`：可以等，但不能提前发射的资源约束。

面试里如果问「为什么这里插了 `nop` 或为什么顺序变了」，先看是 `Barrier` 还是 `hazard`。

## 源码落点

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - `AddSchedEdges()`（约 441 行）：把 chain/glue 变成 `SDep`。
  - `BuildSchedGraph()`（约 543 行）：建 SUnit 图。
  - **读哪里**：看 `Order` 边和 `Barrier` 的设置。

- **`llvm/lib/CodeGen/ScheduleDAGInstrs.cpp`**
  - `addSchedBarrierDeps()`（约 205 行）：处理 `fence`、call、barrier 等。
  - `ScheduleDAGDependencyBuilder` 的 `addBarrierChain()`（约 857 行）。
  - **读哪里**：MachineScheduler 如何把 `FENCE` 变成 `Barrier` 边。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp`**
  - 内存操作的 chain 管理、`PendingLoads`、`TokenFactor`。
  - **读哪里**：`visitLoad`、`visitStore`、`visitAtomicRMW` 的 chain 处理。

- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - `FENCE` 指令定义、`AtomicFence` 的 pattern。
  - **读哪里**：`fence` 的 predecessor/successor 编码。

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**
  - atomic RMW 的 lowering、`AMOADD` 的生成。
  - **读哪里**：`LowerATOMIC_RMW` / `ReplaceNodeResults` 相关路径。

## 踩雷集锦

1. **以为所有 `Ord` 边都一样强**：`Ord Latency=0 Memory` 是普通内存依赖，`Ord Latency=1 Barrier` 是强制屏障。看 `Barrier` 标记。

2. **以为 `volatile` 只是「不优化」**：它还要求每次访问都发生、不能被合并、不能随意重排。`volatile` 的 SUnit 边是 `Barrier`。

3. **以为 `fence` 是编译器插入的 NOP**：`fence` 在 DAG 里是 `AtomicFence`，在 SUnit 图里是屏障节点，在汇编里是 `fence rw, rw`。

4. **以为 `atomicrmw` 一定是一条 `amoadd`**：没有 `A` 扩展时走 `__atomic_fetch_add_8` libcall，结构完全不同。

5. **把 `hazard` 当成正确性约束**：`hazard` 是资源冲突，scheduler 可以等；`Barrier` 是不能跨。两者不要混。

6. **以为 alias analysis 只在 IR 层**：DAG builder 和 MachineScheduler 都会用 alias 信息决定内存边；`noalias` 会改变 chain 形状（第 4 章）。

7. **忽略 `MemOperand`**：`load`/`store`/`AMOADD` 的 `MemOperand` 带 `volatile`、`atomic`、`seq_cst` 等标记；这些标记决定边是 `Memory` 还是 `Barrier`。

8. **以为 `fence` 的 `rw, rw` 是固定文本**：`fence` 的 predecessor/successor 集合来自 IR 的内存序和 target lowering；不同序可能生成不同组合。

## 动手练习

1. 用 `mem.ll` 跑 `-enable-misched=false -debug-only=pre-RA-sched`，找出 `SD → LD → SD` 的三条 `Ord Barrier` 边。把 `%q` 改成 `noalias`，看边是否消失。

2. 用 `volatile.ll` 跑 `-debug-only=isel`，确认 `volatile load`/`volatile store` 的 DAG 标记。再把 `volatile` 去掉，看 `store(load)` 是否被删除。

3. 用 `fence2` 跑 `-enable-misched=false -debug-only=pre-RA-sched`，画出 `LD → FENCE → LD` 的 SUnit 图。把 `fence seq_cst` 改成 `fence acquire`，看 `AtomicFence` 的 target constant 是否变化。

4. 用 `mem.ll` 分别跑 `-mattr=+m` 和 `-mattr=+m,+a`，对比 `amo` 的汇编和 MIR。解释 libcall 版本的调用边界如何改变 scheduling region。

5. 用 `-debug-only=machine-scheduler` 找一条 `hazard` 日志，解释它和 `Barrier` 的区别。`hazard` 会导致错误吗？

6. **guided**：在 `ScheduleDAGInstrs::addSchedBarrierDeps()` 里加一条 `dbgs()`，打印每个 barrier 节点。重新构建后跑 `fence2`，对照 SUnit 日志。不要提交这条调试代码。

## 本章重点整理

- 普通内存依赖由 alias analysis + chain 决定；可能别名的 store→load 会形成顺序边。
- `volatile` 保留访问、禁止删除/合并/随意重排；SUnit 里是 `Barrier`。
- `fence` 在 DAG 里是 `AtomicFence`，在 SUnit 里是屏障节点，在汇编里是 `fence rw, rw`。
- `atomicrmw` 有 `A` 时是 `AMOADD_D_AQRL`，没有 `A` 时是 libcall；两者 scheduling region 不同。
- `Ord Latency=0 Memory` 是普通内存依赖，`Ord Latency=1 Barrier` 是强制屏障；不要只看 `Ord`。
- `hazard` 是资源冲突，不是正确性约束；`Barrier` 才是不能跨的边界。

## 自我检核

- [ ] 能解释普通 load/store 的 chain 如何形成，以及 alias analysis 的作用
- [ ] 能说出 `volatile` 的三个语义要求，以及它在 SUnit 里的边类型
- [ ] 能画出 `LD → FENCE → LD` 的 DAG 和 SUnit 图
- [ ] 能解释 `AtomicFence` 的 target constant 和最终 `fence rw, rw` 的关系
- [ ] 能说出 `atomicrmw` 有/无 `A` 扩展的两种 lowering
- [ ] 能区分 `Ord Memory`、`Ord Barrier`、`hazard`
- [ ] 能解释 `MemOperand` 的 `volatile`/`atomic`/`seq_cst` 标记如何影响调度

## 延伸阅读

### 官方文档

- **[LLVM Atomic Instructions and Concurrency Guide](https://llvm.org/docs/Atomics.html)**
  - **读哪里**：`atomicrmw`、`fence`、内存序的语义。
  - **和本章的关联**：IR 层语义到 DAG 的映射。

- **[LLVM Code Generator — Instruction Scheduling](https://llvm.org/docs/CodeGenerator.html#instruction-scheduling)**
  - **读哪里**：scheduler 的依赖边、barrier、hazard。
  - **和本章的关联**：`SDep` 的官方说明。

- **[RISC-V ISA Spec — FENCE](https://riscv.org/technical/specifications/)**
  - **读哪里**：`fence` 的 predecessor/successor 集合、`aq`/`rl` 位。
  - **和本章的关联**：`fence rw, rw` 和 `amoadd.d.aqrl` 的语义。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - **读哪里**：`AddSchedEdges()`（约 441 行）、`BuildSchedGraph()`（约 543 行）。
  - **和本章的关联**：DAG chain 到 SDep 的翻译。

- **`llvm/lib/CodeGen/ScheduleDAGInstrs.cpp`**
  - **读哪里**：`addSchedBarrierDeps()`（约 205 行）、`addBarrierChain()`（约 857 行）。
  - **和本章的关联**：MachineScheduler 的 barrier 处理。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp`**
  - **读哪里**：`visitLoad`、`visitStore`、`visitAtomicRMW`、`PendingLoads`。
  - **和本章的关联**：内存 chain 的生成。

- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - **读哪里**：`FENCE`、`AMOADD_D_AQRL` 等指令定义。
  - **和本章的关联**：最终指令的编码和 MemOperand。

### 文章 / 演讲

- **"Memory Model and Atomics in LLVM"**（LLVM 相关演讲）
  - **这讲什么**：LLVM 内存模型、`atomic`/`volatile`/`fence` 的区别。
  - **为什么值得看**：先把语义搞清楚，再看调度边。

- **"RISC-V Atomics and Memory Model"**（RISC-V 相关演讲/博客）
  - **这讲什么**：`A` 扩展、`aq`/`rl`、`fence` 的组合。
  - **和本章的关联**：`amoadd.d.aqrl` 和 `fence rw, rw` 的硬件语义。

下一章看不那么「优化」的路径：`-O0` 也会 spill，`FastISel` 在 RISC-V 上为什么用不了，以及 `-regalloc=fast|basic|greedy` 如何改变同一段 IR 的最终汇编。

→ [Ch 10 `-O0`/FastISel 与 `-O2`：为什么 O0 也会 spill](./10-o0-fastisel-vs-o2.md)
