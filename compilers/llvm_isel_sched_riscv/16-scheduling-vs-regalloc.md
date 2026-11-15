# Ch 16 调度与寄存器分配的拉扯

> **目标**：把「调度改变 live range → 影响 spill」这条因果链量出来，同时划清它的边界：哪些 spill 是调度能救的，哪些是 CFG/ABI 钉死、调度无能为力的。学完你能判断一个 spill 问题该改调度、改 RA、还是改 IR。
>
> **环境**：LLVM 24.0.0git；`-mcpu=sifive-u74 -mattr=+m -O2`；对照 `-regalloc=greedy` 与 `-regalloc=basic`；`-stats` 需要 assertion build（本机是）。测试文件：`ldmul.ll`（40 load + 40 mul）、`spill.ll`（24 个值跨 call）、`spill40.ll`（40 个值跨 call）。

## 为什么这两件事必须一起看

面试里经常出现这样的问答：

> 「为什么这段代码 spill 这么严重？」
> 「寄存器不够。」
> 「那为什么寄存器不够？」
> 「……因为值太多了？」

这个回答没有信息量。真正的链条是：

```text
IR 的依赖结构
   → 调度器选择的顺序
      → 每个虚拟寄存器的 live range 长度
         → 同时活着的值数量（压力）
            → RA 是否 spill、spill 谁
```

调度器是这条链上**唯一能改顺序**的一环。IR 的依赖结构定死了哪些值必须同时活着（比如 call 的参数），RA 只能在给定顺序下做最优分配。所以「spill 多」的根因可能在调度，也可能根本不在。

这一章用两组实验把两种情形分开：

- `ldmul.ll`：纯计算、无 call、依赖结构允许大量交错 → 调度**能**把 spill 从 64 降到 0。
- `spill.ll` / `spill40.ll`：所有值都要跨 call 活着 → 调度**不能**改变 spill 数量，只能改变 spill 的位置。

## 先建立直觉：live range 是被顺序拉长的

假设有三个值 `a`、`b`、`c`，都要参与最后的加法：

```text
顺序 A（load 全提前）              顺序 B（用到才算）
──────────────────                ──────────────────
ld a                                ld a
ld b                                mul a
ld c                                ld b
mul a                               mul b
mul b                               ld c
mul c                               mul c
add ...                             add ...

live range:                        live range:
a: ██████████                      a: ███
b:   ████████                      b:     ███
c:     ██████                      c:         ███
峰值压力 = 3                        峰值压力 = 2
```

寄存器够的时候（峰值 3 < 32），两种顺序都没有 spill，只有周期差异。寄存器不够的时候（峰值 41 > 32），顺序就直接决定 spill 数量。

**所以第一个判断永远是：峰值压力有没有超过可分配寄存器数？** 没超，讨论 spill 是浪费时间；超了，才轮到调度和 RA。

## 主线一：`ldmul.ll`——调度能救的那一类

`ldmul.ll` 的形状（40 组）：

```llvm
%l0 = load i64, ptr %p0
...
%m0 = mul i64 %l0, %a
...
%s = add i64 %m0, %m1
```

40 个 load 的结果如果同时活着，峰值压力是 40，超过 32 个通用寄存器。三种方向的实测（Ch 15 的表，这里从 RA 角度重看）：

| 配置 | 栈访问 | 栈帧 | mca 周期（10 iter） |
|---|---|---|---|
| topdown | 0 | 无 | 704 |
| bidirectional | 60 | 240 字节 | 1424 |
| bottomup | 64 | 240 字节 | 1604 |
| misched off + source | 84 | 更大 | 2014 |

`topdown` 的汇编开头：

```asm
ld  a2, 0(a1)
ld  a3, 8(a1)
ld  a4, 16(a1)
ld  a5, 24(a1)
mul a2, a2, a0
mul a3, a3, a0
mul a4, a4, a0
add a2, a2, a3
mul a5, a5, a0
ld  a3, 32(a1)
add a4, a4, a5
```

只有 4 个 load 结果同时活着，乘完立刻并入累加链，所以 RA 一个都不用 spill。

`bottomup` 的汇编开头：

```asm
addi sp, sp, -240
sd   ra, 232(sp)
sd   s0, 224(sp)
sd   s1, 216(sp)
...
```

栈帧 240 字节，`sd/ld` 共 64 条。它在从结果倒推时，把所有 `mul` 的结果都保留到累加链轮到它们，40 个值同时活着，只能溢出。

**这里的因果链是干净的**：方向 → live range → 压力 → spill。改调度确实能救。

### 为什么 `Max Pressure` 三个方向一样，spill 却差这么多

`-debug-only=machine-scheduler` 的 `Max Pressure` 是**静态上界**：

```text
Max Pressure: GPRF16C=1 GPRF16C_with_GPRS07=1 GPRTC=1 GPRF16=41
```

41 是「如果所有值同时活着」的上界，由 DAG 决定，不随顺序变。真正随顺序变的是**实际达到的峰值**，它由 `TopPressure` / `BotPressure` 的即时轨迹决定。RA 看的是后者。

这解释了一个常见困惑：调度器日志说 `Max Pressure=41`，你以为肯定 spill，但 topdown 实际只到 4。Max 不是预测值，是上界。

## 主线二：`spill.ll`——调度救不了的那一类

`spill.ll`：24 个独立计算，全部作为参数传给一个 call，call 之后再全部使用：

```llvm
%v0 = add i64 %a, 0
...
%v23 = add i64 %a, 23
call void (i64, ...) @sink(i64 %v0, ..., i64 %v23)
%w0 = add i64 %v0, %b
...
```

这些值在 call 处必须全部活着（call 要读它们），call 之后还要用。无论调度器怎么排，**live range 都跨过 call**，而 call 会 clobber 大部分寄存器。结果：

| 配置 | 栈访问 |
|---|---|
| misched on + greedy | 68 |
| misched on + basic | 68 |
| misched off + greedy | 68 |
| misched off + basic | 70 |

三个方向（topdown/bottomup/bidirectional）也都是 68。把值加到 40 个：

| 配置 | 栈访问 |
|---|---|
| misched on（三个方向） | 116 |
| misched off | 118 |

**调度在这里几乎没有作用**，因为 live range 的长度是被「call 的位置」钉死的，不是被顺序拉长的。RA 只能决定「先 spill 谁、spill 到哪个槽」，不能决定「要不要 spill」。

这就是面试里区分「懂调度」和「背结论」的地方：不是所有 spill 都能靠调度解决。

### RA 实际做了什么

`-print-after=greedy` 能看到 spill 插入后的 MIR：

```text
496B  SD %21:gpr, $x2, 96 :: (store (s64) into stack + 96)
504B  SD %22:gpr, $x2, 104 :: (store (s64) into stack + 104)
512B  SD %23:gpr, $x2, 112 :: (store (s64) into stack + 112)
520B  SD %24:gpr, $x2, 120 :: (store (s64) into stack + 120)
560B  SD %17:gpr, $x2, 64 :: (store (s64) into stack + 64)
...
```

`-stats` 给出更细的数字（`spill.ll`、默认配置）：

```text
134 regalloc - Number of registers assigned
 23 regalloc - Number of copies inserted for splitting
 24 regalloc - Number of interferences evicted
 23 regalloc - Number of splits finished
 13 regalloc - Number of reloads inserted
 23 regalloc - Number of split local live ranges
119 regalloc - Number of new live ranges queued
 23 regalloc - Number of splits that were simple
 13 regalloc - Number of instructions deleted by DCE
```

读法：

- `24 interferences evicted`：greedy 为了给更「值钱」的 live range 腾位置，把 24 个已分配的区间赶出去（被赶的会 spill 或 split）。
- `23 splits`：把长 live range 切成几段，只在真正需要的地方占寄存器。
- `13 reloads`：从栈里重新读回 13 次。

`-regalloc=basic` 没有 greedy 的 eviction/splitting，所以在 misched off 时多出 2 条栈访问（70 vs 68）。差异不大，因为 call 屏障让所有分配器都只能做同一件事：把跨 call 的值放进 callee-saved 或栈。

## 主线三：调度器的压力模型 vs RA 的真实分配

调度器的 `RegPressureTracker` 是一个**估算器**，它跟踪的是「当前 region 内每个压力集有多少个值活着」。它不知道：

- 物理寄存器的固定约束（`x2` 是 sp、`x8` 是 s0/fp）。
- 调用约定（哪些寄存器 call 会 clobber）。
- RA 的 split/eviction 策略。
- 后续 pass（two-address、copy coalescing）会插入多少 COPY。

所以会出现这种情况：调度器认为压力是 9（`big.ll` 的 `Max Pressure: GPRF16=9`），RA 却仍然插入了一堆 COPY；或者调度器认为压力 41，但 topdown 排完实际只到 4，RA 一条 spill 都没有。

**正确的用法**：把调度器的压力日志当成「趋势」而不是「预测」。趋势对了（topdown 比 bottomup 压力低），就值得做 A/B；具体数字以 RA 后的栈访问为准。

## 底层机制：RA 的四个动作

`RegAllocGreedy` 的核心循环（概念上）：

```text
for each live interval (按 spill cost 排序):
    1. 尝试直接分配一个空闲物理寄存器
    2. 若没有：
       a. 尝试 split（把区间切成几段，分别分配）
       b. 尝试 evict（赶走一个更便宜的区间）
       c. 都不行 → spill
    3. 记录 spill/reload，重写指令
```

调度器能影响的是第 1 步的成功率：live range 越短，越容易找到空闲寄存器。

但第 2 步的 split/evict 会「修复」一部分调度造成的压力。这就是为什么调度的影响不是线性的：`ldmul.ll` 上 60 → 0 的差距，一部分被 RA 的 split 吸收了（否则 bottomup 的 40 个同时活着的值会产生远超 64 条栈访问）。

## 踩雷集锦

1. **以为开了 misched 就一定减少 spill**。`spill.ll` 上四个组合都是 68（basic 70），三个方向也一样。live range 被 call 钉死时，调度无能为力。判断方法：先看 DAG 里有没有「跨屏障的长 live range」。

2. **只看栈访问总数，不看位置**。54 vs 60 的差异可能全在冷路径（比如 call 前的保存），对性能没影响。要看热路径，得结合 `perf` 或至少看循环体内的 `sd/ld`。

3. **用 `-O0` 做调度/RA 对照**。`-O0` 的 fast regalloc 会把所有值往栈上放，调度差异被淹没。用 `-O2`。

4. **把 `Max Pressure` 当预测值**。它是静态上界；实际峰值要看 `TopPressure`/`BotPressure` 轨迹和最终栈访问。

5. **忘了 `-regalloc` 的默认值**。`-O2` 默认是 `greedy`。写对照实验时明确写 `-regalloc=greedy`，否则换个 `-O` 级别结论就变了。

6. **在 `ldmul.ll` 上得出「topdown 总是更好」**。这段代码的形状是「大量独立 load + 串行累加」，topdown 天然合适。换成「结果要尽早算出来给分支」的函数，bottomup 可能更好。结论必须带输入形状。

## 动手练习

### 练习 1：复现两组实验

```bash
# 调度能救的
for d in topdown bidirectional bottomup; do
  "$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
    -misched-prera-direction=$d ldmul.ll -o ldmul_$d.s
  echo -n "$d: "
  grep -cE '^\s+(sd|ld)\s+[^,]+,\s*-?[0-9]+\(sp\)' ldmul_$d.s
done

# 调度救不了的
for r in greedy basic; do
  "$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 \
    -regalloc=$r spill.ll -o spill_$r.s
  echo -n "$r: "
  grep -cE '^\s+(sd|ld)\s+[^,]+,\s*-?[0-9]+\(sp\)' spill_$r.s
done
```

两组数字应该分别是 0/60/64 和 68/70。解释为什么第一组差 64，第二组只差 2。

### 练习 2：找出 spill 的位置

对 `spill_greedy.s` 用 `grep -n 'sp'` 找出所有栈访问，标出哪些在 call 前（保存）、哪些在 call 后（恢复）。再对 `ldmul_bottomup.s` 做同样的事，说明两者的 spill 模式为什么不同。

### 练习 3：只用 8 个值

把 `spill.ll` 的值减到 8 个，重新跑四个组合。预测：还会 spill 吗？为什么？（提示：8 个值跨 call，callee-saved 寄存器有 12 个。）

### 练习 4：看 `-stats`

```bash
"$LLC" -mtriple=riscv64 -mcpu=sifive-u74 -mattr=+m -O2 -stats spill.ll -o /dev/null 2>&1 \
  | grep -E 'spill|reload|evict|split'
```

把数字和本章的表对上。哪一项最能解释「68 条栈访问」？

## 重点整理

- 因果链：依赖结构 → 调度顺序 → live range → 压力 → spill。调度只控制中间一环。
- 峰值压力没超过寄存器数时，讨论 spill 没有意义；超过了才轮到调度/RA。
- `ldmul.ll`：topdown 0 spill、bidirectional 60、bottomup 64——调度能救的典型。
- `spill.ll`/`spill40.ll`：跨 call 的值把 live range 钉死，四个组合几乎一样——调度救不了的典型。
- `Max Pressure` 是静态上界，不等于实际峰值；实际峰值看 `TopPressure`/`BotPressure` 和最终栈访问。
- `RegAllocGreedy` 通过 split/evict 修复一部分调度造成的压力，所以调度的影响不是线性的。
- `-stats` 的 `interferences evicted`、`splits`、`reloads` 是判断 RA 行为的第一手数据。

## 自我检核

- [ ] 我能画出 `ldmul.ll` 在 topdown/bottomup 下的 live range 差异。
- [ ] 我能解释为什么 `spill.ll` 的三个方向都是 68 条栈访问。
- [ ] 我知道 `Max Pressure=41` 和实际 spill 0 为什么不矛盾。
- [ ] 我能说出 `interferences evicted` 和 `splits` 的区别。
- [ ] 我知道 `-regalloc=basic` 和 `greedy` 的关键差异。
- [ ] 我能判断一个 spill 问题该先改调度还是先改 IR。
- [ ] 我知道为什么不能在 `-O0` 上做这类对照。

## 延伸阅读

- **`llvm/lib/CodeGen/RegAllocGreedy.cpp`（`RAGreedy::selectOrSplit`，全文核心）**
  - **读哪里**：eviction、split、spill 的决策顺序。
  - **和本章的关联**：解释「RA 如何修复调度造成的压力」。

- **`llvm/lib/CodeGen/RegAllocBasic.cpp`（全文）**
  - **读哪里**：最简单的线性扫描分配器。
  - **和本章的关联**：`greedy` vs `basic` 的对照基线。

- **`llvm/lib/CodeGen/MachineScheduler.cpp`（`ScheduleDAGMILive::initRegPressure` / `PressureDiff`）**
  - **读哪里**：压力差如何计算、`TopPressure`/`BotPressure` 如何更新。
  - **和本章的关联**：调度器的压力估算和 RA 的真实分配差在哪。

- **`llvm/include/llvm/CodeGen/RegisterPressure.h`**
  - **读哪里**：`PressureChange`、`IntervalPressure` 的定义。
  - **和本章的关联**：`Max Pressure` 日志里的每个字段来源。

- **`llvm/lib/CodeGen/LiveInterval.cpp`（全文）**
  - **读哪里**：live range 的表示与 split。
  - **和本章的关联**：理解「live range 长度」到底怎么算。

- **LLVM 官方文档：*Register Allocation***（CodeGenerator 文档页）
  - **读哪里**：Greedy Allocator 一节。
  - **和本章的关联**：把本章的实测对回上游设计说明。

→ [下一章：Post-RA scheduling、hazard 与 anti-dependency](./17-post-ra-scheduling.md)
