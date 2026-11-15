# 练习 B — `-pre-RA-sched` 六种模式与 MachineScheduler 开关

> **目标**：用同一段 IR 跑一个完整矩阵：`-pre-RA-sched` 的常用模式 × `-enable-misched` 开/关。学完你能判断「最终顺序是谁定的」，能解释为什么开着 MachineScheduler 时换 DAG scheduler 没有效果，也能说出 `source` 和 `list-burr` 的优先级差异。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。固定 `-mtriple=riscv64 -mcpu=generic -mattr=+m -O2 min.ll`。所有输出都是本机实测。

## 为什么做这个实验

第 8 章讲过两层调度：

```text
SelectionDAG 内部：ScheduleDAGSDNodes
Machine IR 层：   MachineScheduler
```

但只看源码很难建立直觉。这个练习用同一段 IR 做控制变量实验：

- 固定 IR、CPU、feature、优化级别。
- 只改 `-pre-RA-sched`（DAG scheduler 的实现）。
- 只改 `-enable-misched`（MachineScheduler 开关）。

这样可以把「DAG scheduler 的影响」和「MachineScheduler 的影响」分开。

实验用的 `min.ll`：

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

依赖结构：

```text
ld p[0] ──► mul x a ──┐
                      ├──► add ──► add ──► add ──► ret
ld p[1] ──► mul x c ──┘
add d, b ─────────────┘
```

三条可并行的链：

- `ld p[0] → mul a`
- `ld p[1] → mul c`
- `add d, b`

最后的 `add` 链必须按顺序合并。调度器的任务就是在这些链之间做选择。

## 实验矩阵

### 模式

`llc -help-hidden` 里 `-pre-RA-sched` 的可用值：

```text
=source       Similar to list-burr but schedules in source order when possible
=list-burr    Bottom-up register reduction list scheduling
=list-hybrid  Bottom-up register pressure aware list scheduling
=list-ilp     Bottom-up register pressure aware list scheduling, balances ILP and pressure
=fast         Fast suboptimal list scheduling
=linearize    Linearize DAG, no scheduling
=vliw-td      VLIW scheduler
=default      Best scheduler for the target
```

本练习覆盖：

- 六个常用模式：`default`、`source`、`list-burr`、`list-hybrid`、`list-ilp`、`fast`。
- 两个对照：`linearize`、`vliw-td`。

### 命令

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc

for ms in on off; do
  for s in default source list-burr list-hybrid list-ilp fast linearize vliw-td; do
    if [ "$ms" = off ]; then extra="-enable-misched=false"; else extra=""; fi
    echo "===== misched=$ms sched=$s"
    "$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 $extra \
      -pre-RA-sched=$s min.ll -o - 2>&1 \
      | sed -n '/^f:/,/ret/p'
  done
done
```

## 结果总表

为了压缩表格，用符号表示指令：

```text
L1 = ld a5, 0(a0)        ; load p[0]
L2 = ld a0, 8(a0)        ; load p[1]
M1 = mul a1, a5, a1      ; p[0] * a
M2 = mul a0, a0, a3      ; p[1] * c
A0 = add a2, a4, a2      ; d + b
A1 = add a1, a2, a1      ; + M1
A2 = add a0, a1, a0      ; + M2
```

不同模式下寄存器名会变（比如 `list-burr` 用 `a4` 装 load 结果），但依赖形状不变。表里只记顺序。

### `-enable-misched=true`（默认）

| `-pre-RA-sched` | 最终顺序 | 备注 |
|---|---|---|
| `default` | `L1 L2 M1 M2 A0 A1 A2` | 和 `source` 相同 |
| `source` | `L1 L2 M1 M2 A0 A1 A2` | 和 `default` 相同 |
| `list-burr` | `L1 L2 M1 M2 A0 A1 A2` | 和 `source` 相同 |
| `list-hybrid` | `L1 L2 M1 M2 A0 A1 A2` | 和 `source` 相同 |
| `list-ilp` | `L1 L2 M1 M2 A0 A1 A2` | 和 `source` 相同 |
| `fast` | `L1 L2 M1 M2 A0 A1 A2` | 和 `source` 相同 |
| `linearize` | `L1 L2 M1 M2 A0 A1 A2` | 和 `source` 相同 |
| `vliw-td` | 崩溃 | `Unimplemented CreateTargetScheduleState` |

**第一个结论**：开着 MachineScheduler 时，这段函数的最终顺序由 MachineScheduler 决定，DAG scheduler 的选择被覆盖了。

### `-enable-misched=false`

| `-pre-RA-sched` | 最终顺序 | 备注 |
|---|---|---|
| `default` | `L1 M1 L2 M2 A0 A1 A2` | source order |
| `source` | `L1 M1 L2 M2 A0 A1 A2` | source order |
| `list-burr` | `A0 L1 M1 A1 L2 M2 A2` | `A0` 提前 |
| `list-hybrid` | `A0 L1 M1 A1 L2 M2 A2` | 和 `list-burr` 相同 |
| `list-ilp` | `A0 L1 M1 A1 L2 M2 A2` | 和 `list-burr` 相同 |
| `fast` | `A0 L1 M1 A1 L2 M2 A2` | 和 `list-burr` 相同 |
| `linearize` | `A0 L1 M1 A1 L2 M2 A2` | DAG 顺序 |
| `vliw-td` | 崩溃 | 同上 |

**第二个结论**：关掉 MachineScheduler 后，DAG scheduler 的差异才显现。`source/default` 保持源码顺序；`list-burr` 等把 `A0` 提前。

### 原始输出摘录

为了避免符号表掩盖细节，下面给出三组真实汇编（`-enable-misched=false`）。

`-pre-RA-sched=source`：

```asm
f:
	ld	a5, 0(a0)
	mul	a1, a5, a1
	ld	a0, 8(a0)
	mul	a0, a0, a3
	add	a2, a4, a2
	add	a1, a2, a1
	add	a0, a1, a0
	ret
```

`-pre-RA-sched=list-burr`：

```asm
f:
	add	a2, a4, a2
	ld	a4, 0(a0)
	mul	a1, a4, a1
	add	a1, a2, a1
	ld	a0, 8(a0)
	mul	a0, a0, a3
	add	a0, a1, a0
	ret
```

`-pre-RA-sched=linearize`：

```asm
f:
	add	a2, a4, a2
	ld	a4, 0(a0)
	mul	a1, a4, a1
	add	a1, a2, a1
	ld	a0, 8(a0)
	mul	a0, a0, a3
	add	a0, a1, a0
	ret
```

`list-burr` 和 `linearize` 的寄存器名和顺序完全一样，但日志里的机制不同：

```text
list-burr:  ********** List Scheduling %bb.0 '' **********
linearize:  ********** DAG Linearization **********
```

这就是为什么不能只看汇编判断「用了哪个 scheduler」。

### 矩阵判读四步法

拿到一组输出后，按这个顺序判断：

1. **先看 `-enable-misched`**：如果是 on，最终顺序很可能由 MachineScheduler 决定；DAG scheduler 的差异要去 `-print-before=machine-scheduler` 看。
2. **再看 `-pre-RA-sched`**：如果是 `source`/`default`，优先源码顺序；如果是 `list-*`/`linearize`，按各自的优先级/拓扑顺序。
3. **再看 SUnit 图**：用 `-debug-only=pre-RA-sched` 找关键节点的 `Height`/`Depth`，解释为什么某个节点被提前。
4. **最后看寄存器分配**：寄存器名和 `mv` 数量可能变，不要把 RA 的差异误判成调度差异。

### 结果不符合预期时怎么查

- **所有模式输出都一样**：先确认 `-enable-misched` 是否开着；MachineScheduler 可能覆盖了 DAG 顺序。
- **`source` 也重排了**：正常。`source` 只是「when possible」，遇到依赖/hazard 仍会动。
- **`linearize` 没有保持源码顺序**：正常。它按 DAG 拓扑顺序，不是源码顺序。
- **`vliw-td` 崩溃**：RISC-V 没有实现 VLIW resource model；换掉这个模式，不要试图改 IR 绕过。
- **`list-burr` 和 `list-hybrid` 相同**：小函数上可能恰好相同；换更大函数或不同 `-mcpu`。
- **汇编里出现额外的 `mv`**：先看寄存器分配，不要归因于 scheduler。

## 观察一：MachineScheduler 覆盖了 DAG scheduler

用 `-print-before=machine-scheduler` 看 `source` 和 `list-burr` 的输入：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -pre-RA-sched=source -print-before=machine-scheduler min.ll -o /dev/null
```

`source` 的 Before：

```text
%5:gpr = LD %0:gpr, 0
%6:gpr = MUL %5:gpr, %1:gpr
%7:gpr = LD %0:gpr, 8
%8:gpr = MUL %7:gpr, %3:gpr
%9:gpr = ADD %4:gpr, %2:gpr
%10:gpr = ADD %9:gpr, %6:gpr
%11:gpr = ADD %10:gpr, %8:gpr
```

`list-burr` 的 Before：

```text
%5:gpr = ADD %4:gpr, %2:gpr
%6:gpr = LD %0:gpr, 0
%7:gpr = MUL %6:gpr, %1:gpr
%8:gpr = ADD %5:gpr, %7:gpr
%9:gpr = LD %0:gpr, 8
%10:gpr = MUL %9:gpr, %3:gpr
%11:gpr = ADD %8:gpr, %10:gpr
```

Before 明显不同。但 `-print-after=machine-scheduler` 的 After 都是：

```text
%5:gpr = LD %0:gpr, 0
%7:gpr = LD %0:gpr, 8
%6:gpr = MUL %5:gpr, %1:gpr
%8:gpr = MUL %7:gpr, %3:gpr
%9:gpr = ADD %4:gpr, %2:gpr
%10:gpr = ADD %9:gpr, %6:gpr
%11:gpr = ADD %10:gpr, %8:gpr
```

MachineScheduler 把两个 `LD` 提前、两个 `MUL` 推后、`ADD d,b` 放到 `MUL` 之后。这个顺序在四个模式下都一样，所以最终汇编相同。

这就是为什么「换 `-pre-RA-sched` 没效果」不一定是 bug：如果 MachineScheduler 开着，DAG scheduler 的输出只是它的输入。

## 观察二：`source` 和 `list-burr` 的优先级不同

`source` 的描述是：

```text
Similar to list-burr but schedules in source order when possible
```

它不是「完全不调度」，而是在多个可用节点中优先选源码顺序靠前的。`min.ll` 的源码顺序是：

```text
L1 M1 L2 M2 A0 A1 A2
```

所以 `source` 输出 `L1 M1 L2 M2 A0 A1 A2`。

`list-burr` 按 bottom-up 的 Height/寄存器压力选节点。`A0` 没有前驱，Height 较低，但它能减少活跃值，所以被提前。输出：

```text
A0 L1 M1 A1 L2 M2 A2
```

注意 `A1` 也被提前到 `L2` 之前，因为它只依赖 `A0` 和 `M1`，可以尽早把 `A0` 的结果消费掉。这是「减少寄存器压力」的典型动作。

`list-hybrid`、`list-ilp`、`fast` 在这段小函数上给出了相同顺序，不代表它们在任何函数上都相同。要观察差异，需要更大的函数、更多独立链，或者不同的 `-mcpu` sched model。

## 观察三：`linearize` 不是「源码顺序」

`-pre-RA-sched=linearize` 的日志：

```text
********** DAG Linearization **********

*** Scheduling: t24: ch = PseudoRET ...
*** Scheduling: t23: ch,glue = CopyToReg ...
*** Scheduling: t21: i64 = ADD t20, t18
...
*** Final schedule ***
t10: CopyFromReg %4
t6:  CopyFromReg %2
t19: ADD t10, t6
t2:  CopyFromReg %0
t13: LD p
...
```

它的 `Final schedule` 是 `A0 L1 M1 A1 L2 M2 A2`，和 `list-burr` 相同。`linearize` 的意思是「按 DAG 的拓扑顺序线性化，不做优先级调度」，不是「按源码顺序」。DAG 的拓扑顺序本身就可能把 `A0` 排在前面，因为 `A0` 是 `t19`，在 DAG 里比 `t13` 更早可用。

如果想知道某个模式到底做了什么，用 `-debug-only=pre-RA-sched` 看 `Scheduling` 序列和 `Final schedule`，不要只看模式名字。

## 观察四：`vliw-td` 在 RISC-V 上不可用

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -pre-RA-sched=vliw-td min.ll -o /dev/null
```

本机实测直接断言：

```text
llc: .../ResourcePriorityQueue.cpp:52:
Assertion `ResourcesModel && "Unimplemented CreateTargetScheduleState."' failed.
```

原因：VLIW scheduler 需要 target 提供 `CreateTargetScheduleState` / resource model；RISC-V 没有为这条路径实现。这个选项在 RISC-V 上不是「效果差」，而是「不可用」。

这也是一个面试可用的点：不要假设所有通用调度器都能跑所有 target；很多 scheduler 需要 target hook。

## 观察五：同一顺序不等于同一原因

`-enable-misched=false` 下，`list-burr`、`list-hybrid`、`list-ilp`、`fast`、`linearize` 都给出 `A0 L1 M1 A1 L2 M2 A2`。这不代表它们的算法相同：

- `list-burr`：bottom-up register reduction，优先减少寄存器压力。
- `list-hybrid`：在 latency 和 pressure 之间折中。
- `list-ilp`：更偏向 ILP。
- `fast`：快速、次优的 list scheduling。
- `linearize`：不做优先级调度，只按 DAG 拓扑顺序线性化。

这段函数太小，三种策略恰好落到同一个顺序。换一个更大的函数，或者换 `-mcpu=sifive-u74`，差异会出现。

## 用 SUnit 图解释 `A0` 为什么能提前

`min.ll` 的 SUnit 日志（节选）：

```text
SU(10): t19: i64 = ADD t10, t6
  Latency : 1
  Depth   : 1
  Height  : 3
  Predecessors:
    SU(12): Data Latency=1
    SU(11): Data Latency=1
  Successors:
    SU(6): Data Latency=1
```

`A0`（`t19`）没有内存依赖，只有两个 `CopyFromReg` 前驱。它在 DAG 里很早就可用，Height 也不低，所以 bottom-up 调度可以在 `L2` 之前选它。

`source` 不选它，是因为源码顺序里 `A0` 在 `M2` 后面；`source` 在可用节点中优先保持源码顺序。`list-burr` 没有这个偏好，按 pressure/height 选，于是 `A0` 提前。

## 参考答案：五个判断题

### 1. 为什么开着 MachineScheduler 时，换 `-pre-RA-sched` 没效果？

因为 MachineScheduler 在 DAG scheduler 之后运行，它会按自己的 sched model 和策略重排。DAG scheduler 的输出只是它的输入；对这段函数，MachineScheduler 最终把顺序统一成 `L1 L2 M1 M2 A0 A1 A2`。

### 2. `-enable-misched=false` 后，哪些模式保持源码顺序？

`default` 和 `source`。`default` 在 RISC-V 上解析成 source scheduler；`source` 显式选择 source scheduler。两者都输出 `L1 M1 L2 M2 A0 A1 A2`。

### 3. 为什么 `list-burr` 把 `A0` 提前？

`A0` 没有前驱，提前它可以尽早消费 `d` 和 `b`，减少活跃值；bottom-up register reduction 优先级会奖励这种选择。`source` 没有这个偏好，所以保持源码顺序。

### 4. `linearize` 和 `source` 有什么区别？

`linearize` 按 DAG 拓扑顺序线性化，不做优先级调度；`source` 是 list scheduler，只是在可用节点中优先源码顺序。两者都可能重排；`linearize` 的顺序取决于 DAG 的拓扑结构，不是源码顺序。

### 5. 为什么 `vliw-td` 崩溃？

VLIW scheduler 需要 target 提供 resource model / `CreateTargetScheduleState`；RISC-V 没有实现这条路径，断言失败。它不是「效果差」，而是「不可用」。

## 踩雷集锦

1. **以为换 `-pre-RA-sched` 一定改变最终汇编**：MachineScheduler 开着时，它可能覆盖 DAG scheduler 的选择。

2. **以为 `source` 完全不调度**：它只是优先源码顺序，遇到依赖/hazard 仍会重排。

3. **以为 `linearize` 等于源码顺序**：它按 DAG 拓扑顺序线性化，`A0` 可能提前。

4. **以为 `list-burr` 和 `list-hybrid` 永远不同**：小函数上可能恰好相同；要换更大函数或不同 sched model。

5. **在 RISC-V 上用 `vliw-td`**：会断言失败；这不是配置问题，是 target 没实现。

6. **只看最终汇编，不看 `Before`**：`-print-before=machine-scheduler` 才能看到 DAG scheduler 的输出。

7. **把寄存器名不同当成依赖不同**：`list-burr` 的 `a4` 和 `source` 的 `a5` 只是分配结果不同，依赖形状一样。

8. **在 `-O0` 下重复这个矩阵**：`-O0` 强制 source scheduler，且没有 MachineScheduler；矩阵会退化。

## 动手练习

1. 把上面的矩阵完整跑一遍，保存 16 组输出。确认 `vliw-td` 的断言文本。

2. 用 `-print-before=machine-scheduler` 对比 `source` 和 `list-burr` 的 Before，再用 `-print-after=machine-scheduler` 确认 After 相同。

3. 把 `min.ll` 的两个 `mul` 改成 `add`，让依赖链变短，重跑矩阵。哪些模式的顺序变了？

4. 把 `-mcpu=generic` 换成 `-mcpu=sifive-u74`，重跑 `-enable-misched=false` 的矩阵。`list-burr` 和 `source` 的差异还在吗？

5. 用 `-debug-only=pre-RA-sched` 找 `A0` 的 `Height`，解释为什么 `list-burr` 选它。

6. **guided**：在 `ScheduleDAGRRList::PickNodeToScheduleBottomUp()` 里加日志，打印每个候选的优先级。重新构建后跑 `list-burr`，看 `A0` 为什么赢。不要提交调试代码。

## 本章重点整理

- 实验矩阵：`-pre-RA-sched` × `-enable-misched`；固定 IR/CPU/feature/O2。
- MachineScheduler 开启时，最终顺序由它决定；DAG scheduler 的选择可能被覆盖。
- 关闭 MachineScheduler 后，`source/default` 保持源码顺序，`list-burr/hybrid/ilp/fast/linearize` 把 `A0` 提前。
- `source` 不是不调度；`linearize` 不是源码顺序；`vliw-td` 在 RISC-V 上不可用。
- 同一顺序不等于同一算法；小函数上多种策略可能恰好相同。

## 自我检核

- [ ] 能独立跑完 8 种模式 × 2 种 MachineScheduler 状态的矩阵
- [ ] 能解释为什么 MachineScheduler 开启时最终顺序相同
- [ ] 能说出 `source`、`list-burr`、`linearize` 的优先级差异
- [ ] 能解释 `A0` 为什么能被提前
- [ ] 能说出 `vliw-td` 崩溃的原因
- [ ] 能用 `-print-before/after=machine-scheduler` 分离两层调度
- [ ] 能避免把寄存器名差异当成依赖差异

## 延伸阅读

- **[Ch 8 SelectionDAG 的调度器](./08-selectiondag-scheduler.md)** — 本章的 SUnit、bottom-up、`Final schedule` 基础。
- **[Ch 11 MachineScheduler 全景](./11-machinescheduler-overview.md)** — 下一章解释 MachineScheduler 如何覆盖 DAG 顺序。
- **[llvm-mca 文档](https://llvm.org/docs/CommandGuide/llvm-mca.html)** — 用周期数验证顺序差异。
- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGRRList.cpp`** — `source`、`list-burr`、`list-hybrid`、`list-ilp` 的实现。
- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGFast.cpp`** — `fast`、`linearize` 的实现。

下一章进入 Part 2：MachineScheduler 如何用 sched model 重新排列 isel 产出的 MI。

→ [Ch 11 MachineScheduler 全景：region、pre-RA/post-RA、策略](./11-machinescheduler-overview.md)
