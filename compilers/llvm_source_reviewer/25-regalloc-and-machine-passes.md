# Ch 25 — 寄存器分配与机器 pass

> **目标**：拿到一个改 `RegAllocGreedy`、调 `TargetPassConfig` 顺序、或修某个机器 pass 的 PR 时，能说出它动的是哪一层、凭什么不会在别的 target 上坏、该看哪条测试；并且能自己用 `-regalloc=`、`-print-after=`、`-stats`、MIR 手术把「优化器说它更好了」变成可复现的证据。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（LLVM 24.0.0git，assertions ON，注册了 X86/AArch64/RISCV）。构建目录缓存的 worktree 是 `a5eaf194d8b6`，与源码 hub 不是同一 commit；pass 名、flag 名和输出形态可信，行号以 7c7a 为准。本章所有命令输出都是在 `pr-lane` 上真跑得到的。

## 为什么需要这个？

中端 review 的赌注是「这段 IR 变换对不对」；CodeGen review 的赌注换了一层：**这段机器码变换在 20 个 target、3 个 opt level、2 套 pass manager 下是否还成立**。

这带来三个中端没有的麻烦：

1. **机器 IR 不是 SSA 封闭的**。物理寄存器是共享资源，`%eax` 同时是返回值、隐式 def、调用约定的一部分。一个看起来局部的改写可能破坏 `TracksLiveness`、`TiedOpsRewritten` 这些 machine function 属性。
2. **pass 顺序是全局契约**。`TargetPassConfig::addMachinePasses()`（`llvm/lib/CodeGen/TargetPassConfig.cpp:985`）里挪一个 pass，影响所有 target 的所有函数；而 `CodeGenPassBuilder`（`llvm/lib/Passes/CodeGenPassBuilder.cpp`）是同一件事的新 PM 版本。LLVM 现在**两套 codegen pipeline 并存**，review 时必须问清楚改的是哪一套。
3. **「更少的 spill」不是目标函数**。寄存器分配在指令数、栈流量、callee-saved 保存/恢复、编译时间之间做权衡；一个统计数字变好，另一个数字常常变坏。

本章按「数据结构 → 三个 allocator → spiller → 机器 pass 地图 → 顺序与影响面」展开，每一节都配一个可以在本机复现的实验。

## 先建立直觉

寄存器分配要解决四件事，顺序固定：

```text
   MachineInstr (虚拟寄存器 %5:gr32)
        │
        │ 1. 活跃分析：LiveIntervals / SlotIndexes
        ▼
   LiveInterval（每个虚拟寄存器的 [定义, 使用) 区间集合）
        │
        │ 2. 分配：RegAllocGreedy 在 LiveRegMatrix 上占位
        ▼
   VirtRegMap（虚拟寄存器 → 物理寄存器 或 栈槽）
        │
        │ 3. 溢出：InlineSpiller 在区间太长时插入 spill/reload
        ▼
   VirtRegRewriter（真正把 %5 替换成 $edi，删掉 COPY）
        │
        ▼
   物理寄存器机器码（可送 MC）
```

四个数据结构各管一件事，review 时按这张图定位：

| 结构 | 文件 | 回答的问题 |
| --- | --- | --- |
| `LiveInterval` / `SlotIndex` | `llvm/include/llvm/CodeGen/LiveInterval.h` | 这个虚拟寄存器在哪些点活着 |
| `LiveIntervals` | `llvm/lib/CodeGen/LiveIntervals.cpp:165` | 全函数的区间集合怎么算、怎么增量更新 |
| `VirtRegMap` | `llvm/lib/CodeGen/VirtRegMap.cpp:99` | 谁被分到了哪个物理寄存器 / 哪个栈槽 |
| `LiveRegMatrix` | `llvm/include/llvm/CodeGen/LiveRegMatrix.h:150` | 这个物理寄存器在这段区间能不能占 |
| `LiveRangeEdit` | `llvm/lib/CodeGen/LiveRangeEdit.cpp` | 拆分后新区间怎么记账、死定义怎么删 |

一个常见的 review 误区是只看 allocator 主文件。实际上一半的 bug 落在 `LiveIntervals` 的增量维护上：分配器拆了一个区间、`shrinkToUses` 重算了 use、但 `SlotIndexes` 没跟着改，后面 `-verify-machineinstrs` 才会炸。

## 三个 allocator：不是「快慢三档」，是三种契约

源码里注册了三个（还有 PBQP，但默认不用）：

```cpp
// llvm/lib/CodeGen/RegAllocFast.cpp:68
static RegisterRegAlloc fastRegAlloc("fast", "fast register allocator", createFastRegisterAllocator);
// llvm/lib/CodeGen/RegAllocBasic.cpp:39
static RegisterRegAlloc basicRegAlloc("basic", "basic register allocator", createBasicRegisterAllocator);
// llvm/lib/CodeGen/RegAllocGreedy.cpp:154
static RegisterRegAlloc greedyRegAlloc("greedy", "greedy register allocator", createGreedyRegisterAllocator);
```

它们的差别不是实现质量，而是**允许做什么**：

- **Fast**（`RegAllocFastImpl`，`RegAllocFast.cpp:185`）：按基本块线性扫描，虚拟寄存器只在块内分配，跨块就 spill。它**不做 live range splitting**，也不做全局 eviction。所以它便宜、可预测，适合 `-O0`。
- **Greedy**（`RAGreedy`，`RegAllocGreedy.cpp:158`）：按 spill weight 排序，尝试 split、尝试 evict 已有区间（`evictInterference` 在 `:629`，`tryEvict` 在 `:746`），失败才 spill（`selectOrSplitImpl`，`:2777`）。它是生产默认。
- **Basic**：一个教学/参考实现，走同样的 `RegAllocBase::allocatePhysRegs()`（`llvm/lib/CodeGen/RegAllocBase.h:104`）骨架，但没有 split 和 evict 的启发式。它能跑，但不是为性能准备的。

`RegAllocBase` 把公共循环抽出来了：拿一个未分配的 interval、问 `selectOrSplit`、成功就 `assign`，失败就交给 `spiller()`。三个 allocator 的差异全部在 `selectOrSplit` 和 `spiller()` 的返回值里。读 PR 时先找这两个入口，比从头读 3000 行快。

### 实验 1：同一个函数，三种 allocator

```llvm
; /tmp/ra25.ll
define i32 @mix(i32 %a, i32 %b, i32 %c, i32 %d, i32 %e, i32 %f) {
entry:
  %p1 = add i32 %a, %b
  %p2 = add i32 %c, %d
  %p3 = add i32 %e, %f
  %q1 = mul i32 %p1, %p2
  %q2 = xor i32 %p2, %p3
  %r1 = add i32 %q1, %q2
  %r2 = sub i32 %q1, %p3
  %s1 = mul i32 %r1, %r2
  %s2 = add i32 %s1, %a
  br label %loop
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %acc = phi i32 [ %s2, %entry ], [ %acc.next, %loop ]
  %acc.next = add i32 %acc, %i
  %i.next = add i32 %i, 1
  %cond = icmp slt i32 %i.next, 10
  br i1 %cond, label %loop, label %exit
exit:
  %res = add i32 %acc.next, %s2
  ret i32 %res
}
```

```console
$ for ra in fast basic greedy; do echo "=== $ra ==="; \
    llc -mtriple=x86_64-unknown-linux-gnu -regalloc=$ra -stats ra25.ll -o /dev/null 2>&1 \
    | rg -i "regalloc|coalesc|loads added|stores added"; done
=== fast ===
 12 regalloc             - Number of copies coalesced
  4 regalloc             - Number of loads added
  6 regalloc             - Number of stores added
=== basic ===
  8 regalloc                - Number of registers assigned
  6 regalloc                - Number of identity moves eliminated after rewriting
 15 regalloc                - Number of interval joins performed
=== greedy ===
  8 regalloc                - Number of registers assigned
  7 regalloc                - Number of identity moves eliminated after rewriting
 15 regalloc                - Number of interval joins performed
```

三个数字要连起来读：

- `fast` 报的是 **copies coalesced / loads added / stores added**——它的世界观里，跨块一律走栈，所以它的指标是「我加了多少访存」。
- `basic` / `greedy` 报的是 **registers assigned / interval joins / identity moves eliminated**——它们的世界观里，区间可以合并，所以指标是「我消了多少 COPY」。
- `basic` 和 `greedy` 在这段小函数上几乎一样；差异要在有压力、有 split 机会的函数上才显现。**不要用小函数证明 greedy 的改进**，这是 review 里最常见的一类无效证据。

### 实验 2：压力函数暴露权衡

一个 20 个值跨调用活跃的函数：

```bash
$ python3 - <<'PY'
n=20
L=["declare i32 @bar(i32)","define i32 @pressure(i32 %a0,i32 %a1,i32 %a2,i32 %a3,i32 %a4,i32 %a5) {","entry:"]
for i in range(n):
    L.append(f"  %v{i} = add i32 %a{i%6}, {i*7+1}")
L.append("  %c = call i32 @bar(i32 %v0)")
L.append("  %s0 = add i32 %v0, %c")
for i in range(1,n):
    L.append(f"  %s{i} = add i32 %s{i-1}, %v{i}")
L.append(f"  ret i32 %s{n-1}")
L.append("}")
open("spill25.ll","w").write("\n".join(L)+"\n")
PY
$ for ra in fast basic greedy; do echo "=== $ra ==="; \
    llc -mtriple=x86_64-unknown-linux-gnu -regalloc=$ra -stats spill25.ll -o /dev/null 2>&1 \
    | rg -i "spill|reload|loads added|stores added|registers assigned"; done
=== fast ===
  6 regalloc         - Number of loads added
  6 regalloc         - Number of stores added
=== basic ===
 13 regalloc                - Number of registers assigned
=== greedy ===
 13 regalloc                - Number of registers assigned
```

按「spill 越少越好」的直觉，greedy 应该赢。看真实汇编：

```console
$ llc -mtriple=x86_64-unknown-linux-gnu -regalloc=fast spill25.ll -o - | wc -l
63
$ llc -mtriple=x86_64-unknown-linux-gnu -regalloc=fast spill25.ll -o - | rg -c "Spill|Reload"
12
$ llc -mtriple=x86_64-unknown-linux-gnu -regalloc=greedy spill25.ll -o - | wc -l
76
$ llc -mtriple=x86_64-unknown-linux-gnu -regalloc=greedy spill25.ll -o - | rg -c "Spill|Reload"
0
```

greedy 没有一次 spill/reload，但总行数更多。原因在它的选择：

```asm
# greedy 的序言
	pushq	%rbp
	pushq	%r15
	pushq	%r14
	pushq	%r13
	pushq	%r12
	pushq	%rbx
	pushq	%rax
	...
	leal	1(%r13), %edi
	callq	bar@PLT
	leal	1(%r13,%rax), %eax
```

它把 20 个值放进 callee-saved 寄存器（`%rbx`、`%rbp`、`%r12`–`%r15`），用 6 组 push/pop 换掉了 12 次 spill/reload；同时把 `%v_i = add %a, const` **rematerialize** 成调用后的 `leal`，而不是存下来。

这段对比值得记住三点：

- 「`Number of loads added` 为 0」不等于「没有栈访问」，callee-saved 的保存/恢复也是栈访问，只是记在别的账上。
- rematerialization 用**指令**换**内存**。哪种更好取决于函数被调用多少次、调用点是否在热路径，不是静态可判定的。
- 小函数上的指令数差异可以反转。review 一个「减少 spill」的 PR 时，要看它报的是哪个 metric、在哪个 benchmark 上、有没有对应的回归测试锁住。

## Spiller：溢出不是一个动作，是一组决策

分配失败时，`RegAllocBase` 调 `spiller().spill(edit, order)`。生产实现是 `InlineSpiller`（`llvm/lib/CodeGen/InlineSpiller.cpp`），入口在 `:1454`，内部按顺序做：

```cpp
// InlineSpiller.cpp 的关键函数（行号见 7c7a）
bool InlineSpiller::isSnippet(const LiveInterval &SnipLI);          // :326
void InlineSpiller::collectRegsToSpill();                          // :389
bool InlineSpiller::hoistSpillInsideBB(LiveInterval &SpillLI, ...); // :441
bool InlineSpiller::reMaterializeFor(LiveInterval &VirtReg, MachineInstr &MI); // :674
void InlineSpiller::insertReload(Register NewVReg, SlotIndex Idx, ...);        // :1245
void InlineSpiller::insertSpill(Register NewVReg, bool isKill, ...);           // :1276
void InlineSpiller::spillAroundUses(Register Reg);                             // :1314
void InlineSpiller::spillAll();                                                // :1405
```

`spillAroundUses` 是整个 spiller 的核心：遍历 interval 的所有 use，在每个 use 之前插 reload，之后插 spill；如果 use 可以被 rematerialize（`reMaterializeFor`），就重算而不是读栈；如果多条 interval 来自同一个定义（snippet），就共享一次 spill。

review spiller 相关 PR 时，问题清单很短但很硬：

1. 新插入的 `MachineInstr` 有没有正确的 `SlotIndex`？没有的话 `LiveIntervals` 立刻不一致。
2. 新插入的指令有没有正确的 debug location / `DBG_VALUE` 处理？丢了 debug info 的 PR 会被 debugify 测试抓到。
3. 新指令的 operand 有没有正确的 `isKill` / `isUndef`？live interval 的 kill flag 由 `LiveIntervals::addKillFlags`（`LiveIntervals.cpp:~852`）统一加，手写 kill 容易和它打架。
4. 有没有更新 `MachineFunction` 的属性？spiller 之后仍然是 SSA 前的状态，但 `TracksLiveness` 必须保持真实。

## 机器 pass 地图：RA 前后各有什么

`llc -print-pass-numbers -print-after-all` 会把 codegen pipeline 完整打出来。下面是从 `finalize-isel` 到 asm printer 的真实顺序（截取，X86，`-O2`）：

```text
finalize-isel → x86-domain-reassignment → early-tailduplication → opt-phis
→ slot-indexes → stack-coloring → localstackalloc → dead-mi-elimination
→ early-ifcvt → machine-combiner → x86-cmov-conversion → early-machinelicm
→ machine-cse → machine-sink → peephole-opt → lrshrink → x86-fixup-setcc
→ x86-optimize-leas → x86-cf-opt → detect-dead-lanes → init-undef
→ process-implicit-defs → phi-node-elimination → two-address-instruction
→ register-coalescer → rename-independent-subregs → machine-scheduler
→ greedy → virt-reg-rewriter → regallocscoringpass → stack-slot-coloring
→ machine-cp → machinelicm → postra-machine-sink → shrink-wrap
→ prolog-epilog → machine-latecleanup → branch-folder → tailduplication
→ machine-cp → post-ra-pseudos → x86-expand-pseudo → post-RA-sched
→ block-placement → break-false-deps → x86-fixup-bw-insts
→ x86-compress-evex → cfi-instr-inserter → x86-asm-printer
```

把这张表和源码对上：

| pass | 源码入口 | 职责 | review 时的风险点 |
| --- | --- | --- | --- |
| `MachineScheduler` | `MachineScheduler.cpp:240`（`MachineSchedulerImpl`）、`:330`（legacy wrapper） | RA 前按 target 的 `MachineSchedStrategy` 重排 | 改调度模型影响所有 target 的指令序，必须有大量 codegen 测试 diff |
| `PostRAScheduler` | `PostRASchedulerList.cpp:55` | RA 后按 latency 重排，受寄存器依赖限制 | 只用 `-mcpu` 特定路径时，别忘了别的 subtarget 也走这里 |
| `MachineCombiner` | `MachineCombiner.cpp:45`（`MachineCombinerImpl`） | 用 target 提供的 combine 规则替换指令序列 | 规则要证明「延迟/吞吐」收益，否则是纯 churn |
| `BranchFolder` | `BranchFolding.cpp:80` | 合并块、删空块、tail merge | 改 CFG 必须维护 `MachineDominatorTree` / `MachineLoopInfo` |
| `MachineBlockPlacement` | `MachineBlockPlacement.cpp:3568`（`run`）、`:2797`（`buildCFGChains`） | 按 profile 排块 | 依赖 `MachineBlockFrequencyInfo`；profile 缺失时的 fallback 要一致 |
| `PEI` | `PrologEpilogInserter.cpp:79`（`PEIImpl`）、`:126`（legacy） | 算栈帧、插 prologue/epilogue、spill callee-saved | 栈对齐、`shrink-wrapping`、`cfi` 指令三者互相牵制 |

一个实用的观察：**RA 前的 pass 改的是「虚拟寄存器世界」，RA 后的 pass 改的是「物理寄存器世界」**。同一个变换放在 RA 前还是后，正确性论证完全不同：

- RA 前可以随便重排、复制、删除指令（只要 `LiveIntervals` 还没建立，或者你同步更新了它）。
- RA 后不能引入新的虚拟寄存器，不能假设任意指令可重排（物理寄存器有 anti-dependency），必须尊重 `TracksLiveness`。

所以 review 里看到「把 X 从 pre-RA 挪到 post-RA」这类 PR，第一个问题永远是：它还能不能拿到 `LiveIntervals`？拿不到，说明它只能做局部窥孔；拿得到，说明它在 RA 后重建了分析，代价要交代。

## 实验 3：看 RA 到底把什么变成了什么

`-print-after=greedy` 打印的是 **pass 的注册名**，不是 `regalloc` 这种类别名：

```console
$ llc -mtriple=x86_64-unknown-linux-gnu -print-after=greedy ra25.ll -o /dev/null 2>&1 | head -6
# *** IR Dump After Greedy Register Allocator (greedy) ***:
# Machine code for function mix: NoPHIs, TracksLiveness, TiedOpsRewritten, TracksDebugUserValues
Function Live Ins: $edi in %5, $esi in %6, $edx in %7, $ecx in %8, $r8d in %9, $r9d in %10

0B	bb.0.entry:
```

注意 header 上的三个属性：`NoPHIs`、`TracksLiveness`、`TiedOpsRewritten`。它们是 machine function 的**不变量声明**，后面的 pass 和 verifier 都信它们。一个 PR 如果手工改 MIR 却没维护这些 flag，`-verify-machineinstrs` 会报，而普通编译不会。

再看 `machine-scheduler` 的输出，物理寄存器还没出现，但 tied operand 已经显式标出来了：

```console
$ llc -mtriple=x86_64-unknown-linux-gnu -print-after=machine-scheduler ra25.ll -o /dev/null 2>&1 | sed -n '7,20p'
16B	  %10:gr32 = COPY $r9d
...
112B	  %15:gr32 = ADD32rr %15:gr32(tied-def 0), %5:gr32, implicit-def dead $eflags
```

`tied-def 0` 的意思是「这条指令的第 0 个 def 必须和第 1 个 use 用同一个物理寄存器」。x86 的两地址指令在 `two-address-instruction` pass 里被改写成这种形态，RA 必须满足它。review 一个改 `TwoAddressInstructionPass` 或改 x86 `ADD32rr` 定义的 PR 时，这是要检查的第一件事。

## 实验 4：把 MIR 停下来做手术

review codegen PR 时最有用的技能是「在任意 pass 前后取 MIR，改一改，再接着跑」：

```console
$ llc -mtriple=x86_64-unknown-linux-gnu -stop-after=greedy ra25.ll -o ra25.mir
$ rg -n "^---|^name:|^body:" ra25.mir | head
1:--- |
33:---
34:name:            mix
117:body:             |
$ llc -mtriple=x86_64-unknown-linux-gnu -run-pass=greedy ra25.mir -o - | rg "gr32" | head -3
    %10:gr32 = COPY $r9d
    %14:gr32 = COPY $r8d
```

`-run-pass` 只跑指定的那个 pass，前后状态由 MIR 决定。这让「这个 bug 是 RA 引入的还是 RA 之前就有的」变成二分问题：在 `-stop-after=` 的每个阶段各取一份 MIR，用 `-run-pass` 单独重跑，看哪一步开始出现错误。

`-stress-regalloc=N` 是另一个常用工具：把所有 register class 的可分配寄存器砍到 N 个，强迫 allocator 走 spill 路径。

```console
$ llc -mtriple=x86_64-unknown-linux-gnu -stress-regalloc=2 -stats spill25.ll -o /dev/null 2>&1 | rg -i "spill|unassigned"
 28 regalloc                - Number of registers assigned
  9 regalloc                - Number of spill slots allocated
 10 regalloc                - Number of spilled live ranges
 10 regalloc                - Number of spills inserted
  7 regalloc                - Number of registers unassigned
```

`Number of registers unassigned` 统计的是 `LiveRegMatrix::unassign()`（`llvm/lib/CodeGen/LiveRegMatrix.cpp:151`）被调用的次数，也就是分配被**回滚**的次数（典型来源是 eviction 尝试失败后撤销），不是错误计数。这两个统计都在 `LiveRegMatrix.cpp:38-39` 注册，描述文字用的是 `Number of registers assigned/unassigned`，容易被读成「有多少寄存器没分出去」。真正的信号是 assertion 或 `-verify-machineinstrs` 报错；如果它们在 stress 模式下出现，基本可以确定这个 PR 的 spill 路径没处理某种 operand 形态。

## 踩雷清单

### 1. `-print-after=regalloc` 静默无输出

```console
$ llc -mtriple=x86_64-unknown-linux-gnu -print-after=regalloc ra25.ll -o /dev/null 2>&1 | head -3
$ echo $?
0
```

没有报错，也没有输出。因为 `-print-after` 匹配的是 pass 注册名（`greedy`、`fast`、`machine-scheduler`、`prolog-epilog`），不是「类别」。想看完整名单，用 `-print-pass-numbers -print-after-all` 再 `rg "IR Dump After"`。**这个坑会让 review 里的实验证据看起来像「没有变化」**，实际是你根本没打开打印。

### 2. `-stats` 在 release 构建上不可用

`--stats` 的帮助文本写着 `(available with Asserts)`。在发行版 LLVM 上跑 `-stats` 不会给你数字，可能直接忽略或报错。**引用 stats 的 PR 必须同时说明构建配置**，否则 reviewer 无法复现。本课的 `pr-lane` 是 assertions ON，所以上面的数字可信。

### 3. 手写 SSA「重新赋值」直接被 verifier 拒

我第一版压力函数写成了：

```llvm
  %m0 = mul i32 %v0, %v1
  %m0 = add i32 %m0, %v2      ; 想表达累积
```

```console
$ llc spill25.ll -o /dev/null
llc: error: llc: spill25.ll:28:3: error: multiple definition of local value named 'm0'
```

LLVM textual IR 没有赋值语句，名字只能定义一次。写实验 IR 时用 `%m0/%m1/%m2...` 或者 `phi`，不要依赖「后写覆盖」。这个错误在 MLIR 里不会出现，从 MLIR 过来的人容易踩。

### 4. `-regalloc` 与 `-regalloc-npm` 是两条路

`llc --help-hidden` 同时列出 `--regalloc` 和 `--regalloc-npm`。前者服务 legacy codegen pipeline（`TargetPassConfig`），后者在 `CodeGenPassBuilder::addRegAllocPass`（`CodeGenPassBuilder.cpp:823`）里被读取，而那个 switch 只接受 `fast` 和 `greedy`，其他值走 `reportFatalUsageError("register allocator not supported yet")`。

在本机上 `-regalloc-npm=basic` 并没有触发这个错误（`llc` 默认没走那条 NPM codegen 路径），说明**不能从源码推断运行时行为**。判断某个 flag 到底喂给了哪套 pipeline，看 `-print-pass-numbers` 的实际输出，而不是看 flag 名字。

### 5. 不加 `-verify-machineinstrs`，坏 MIR 不会报

`llc` 默认不验证每条 machine pass 之后的状态。CI 里 `llvm/test/CodeGen` 的大量测试也没开这个 flag。所以「本地能出汇编」不等于「machine IR 合法」。凡是要提交 codegen 修复，至少在调试时加 `-verify-machineinstrs` 跑一遍，并把能触发 bug 的 MIR 做成 `-run-pass` 测试。

### 6. 在 `-O0` 上验证 `-O2` 的优化

`-O0` 走 `addFastRegAlloc`（`CodeGenPassBuilder.cpp:866`），根本不跑 coalescer、scheduler、greedy。用它来测「greedy 的改动」等于什么都没测。同理，`-O2` 下 x86 会跑两遍 greedy（`tile` 相关 pass 前后各一次），`-print-after=greedy` 会打印两段，别看错。

## reviewer 视角：一个 codegen PR 要回答的六个问题

1. **它改的是哪套 pipeline？** legacy `TargetPassConfig`、NPM `CodeGenPassBuilder`，还是两者都要改？只改一套是常见的半成品。
2. **影响面是 target-specific 还是全局？** 改 `llvm/lib/CodeGen/` 下的通用 pass，要在 PR 描述里给出至少两个 target 的测试；只改 `X86/` 下的，影响面可以说清楚是 X86。
3. **它假设了哪个 opt level？** `-O0` 和 `-O2` 的 pass 集合不同，声明「减少 spill」必须说明在哪个级别、哪个 allocator 下测的。
4. **测试锁住了什么？** codegen 测试应该锁指令序列或关键指令存在性；只锁 `ret` 的测试什么都没锁。MIR 测试（`-run-pass`）比 `llc` 端到端测试更能定位回归。
5. **它维护了哪些 machine 不变量？** `TracksLiveness`、`NoPHIs`、tied operand、`SlotIndex`、debug location，逐条对照。
6. **代价是什么？** 编译时间、代码大小、callee-saved 压力、对其他 subtarget 的影响。只报收益不报代价的 PR，reviewer 有理由要求补数据。

## 一个失败例子：只在一种配置下成立的「优化」

设想一个 PR 声称「在 `MachineCombiner` 里新增一条 x86 规则，把 `add` + `load` 合成 `lea`，减少一条指令」，测试是一个 `-O2 -mcpu=znver2` 的 lit 测试，`CHECK` 只匹配了 `leal`。

这条 PR 的问题不在规则本身，而在证据：

- `MachineCombiner` 在 RA 前后都会跑；如果规则依赖 RA 后的物理寄存器形态，它在 RA 前不成立，代码里必须有 guard，测试里必须有一个能触发 guard 的例子。
- `lea` 的可达性受 addressing mode 限制（base + index*scale + disp），在 32 位模式、大 displacement、或 `-mno-lea` 类 subtarget 上不成立。只测 znver2 等于只测了一种编码域。
- 指令数减少不等于更快：`lea` 占用 AGU 端口，和 `add` 走 ALU 的端口压力不同。没有 `llvm-mca` 或 benchmark 数据的「优化」只能算 code size 改动。

review 这类 PR 的写法应该是：先确认规则的正确性条件（谓词、编码域、opt level），再要求补第二种配置的测试，最后才讨论收益是否值得。顺序反了，就会陷入「我觉得这样更快」的争论。

## 本章重点整理

- 寄存器分配分四步：`LiveIntervals` 建区间、allocator 占 `LiveRegMatrix`、`InlineSpiller` 插 spill/reload、`VirtRegRewriter` 落地物理寄存器。
- `fast` 是块内分配 + 跨块 spill，指标是 loads/stores added；`greedy` 会 split 和 evict，指标是 registers assigned / identity moves；`basic` 是参考实现。
- 「spill 更少」可以和「代码更大」同时成立：greedy 用 callee-saved 保存/恢复和 rematerialization 换掉了栈槽访存。
- `InlineSpiller::spillAroundUses` 是 spill 的核心；改它必须同时维护 `SlotIndex`、kill flag、debug location 和 `TracksLiveness`。
- 机器 pass 的顺序由 `TargetPassConfig::addMachinePasses`（`TargetPassConfig.cpp:985`）和 `CodeGenPassBuilder` 两套代码决定；RA 前是虚拟寄存器世界，RA 后是物理寄存器世界。
- `-print-after` 用 pass 注册名（`greedy`、`machine-scheduler`、`prolog-epilog`），`-print-pass-numbers -print-after-all` 是查名单的手段。
- MIR 手术三件套：`-stop-after=` 取状态、`-run-pass=` 单独重跑、`-verify-machineinstrs` 验证合法性。
- review codegen PR 的核心不是判断「这个优化好不好」，而是判断「它在哪些配置下成立、证据覆盖了哪些配置」。

## 自我检核

- [ ] 能不看笔记说出 `LiveIntervals`、`VirtRegMap`、`LiveRegMatrix` 各自的职责，以及它们分别在哪一步被写
- [ ] 能解释 `fast` allocator 为什么在 `-O0` 下是合理选择，而不只是「因为它快」
- [ ] 能说明 `Number of loads added` 为 0 时，栈流量为什么可能反而更高
- [ ] 能画出 `RegAllocBase::allocatePhysRegs()` 的主循环，并指出三个 allocator 在哪里分叉
- [ ] 能说出 `InlineSpiller` 至少四个需要维护的不变量
- [ ] 能用 `-print-after-all` 找到 `register-coalescer`、`greedy`、`prolog-epilog` 在 pipeline 中的位置
- [ ] 能解释 RA 前变换与 RA 后变换在正确性论证上的区别
- [ ] 能写出一个用 `-stop-after` + `-run-pass` 二分定位 codegen bug 的步骤
- [ ] 拿到一个「减少 spill」的 PR，能列出至少四个必须追问的配置维度
- [ ] 能解释为什么 `-stress-regalloc=2` 下 `registers unassigned` 不为 0 不一定是错误

## 延伸阅读

- **[LLVM 文档：Register Allocation](https://llvm.org/docs/CodeGenerator.html#register-allocation)**
  - **读哪里**：`Register Allocation` 整节，以及前面的 `Machine code` 一节
  - **能学到什么**：官方对 SSA-based RA、live interval、spill 的叙述；本章四步模型的规范版本
  - **前提**：知道 `MachineInstr` 与虚拟寄存器的表示；不熟悉的话先读 Ch 24
- **[LLVM 文档：The LLVM Machine Code Instruction Representation](https://llvm.org/docs/CodeGenerator.html#the-llvm-machine-code-instruction-representation)**
  - **读哪里**：`MachineInstr` 的 operand、tied operand、implicit operand 部分
  - **能学到什么**：`tied-def`、`implicit-def dead $eflags` 这些 dump 里的词到底是什么意思
  - **和本章的关联**：实验 3 的输出全靠这套词汇才能读懂
- **[LLVM 源码：`RegAllocGreedy.cpp` 顶部注释与 `selectOrSplitImpl`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/RegAllocGreedy.cpp)**
  - **读哪里**：文件头注释、`selectOrSplitImpl`（约 `:2777`）、`evictInterference`（约 `:629`）
  - **能学到什么**：greedy 的 split/evict 顺序，以及「为什么这个区间值得 evict」的代价模型
  - **前提**：读过本章的 `LiveRegMatrix` 一节；不需要先懂 PBQP
- **[LLVM 源码：`InlineSpiller.cpp` 的 `spillAroundUses`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/InlineSpiller.cpp)**
  - **读哪里**：`spillAroundUses`（约 `:1314`）、`reMaterializeFor`（约 `:674`）、`insertReload`/`insertSpill`
  - **能学到什么**：spill 的真实插入位置、snippet 共享、rematerialization 的判定
  - **前提**：知道 `SlotIndex` 与 `LiveInterval` 的基本结构
- **[LLVM 文档：Machine IR (MIR) Format Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`machineFunction`、`body`、`liveins`、`stack` 几节
  - **能学到什么**：`-stop-after` 产出的 MIR 每个字段的含义，以及怎么手写一个 `-run-pass` 测试
  - **和本章的关联**：实验 4 的完整格式说明

把这一章压成一句话：**regalloc 的 review 不是判断「哪个 allocator 更好」，而是判断「这个改动在哪个 pipeline、哪个 opt level、哪个 target 上成立，证据有没有覆盖这些维度」。**

→ [Ch 26 — Target / MC / AsmPrinter：后端 review 的判断面](./26-mc-target-asmprinter.md)
