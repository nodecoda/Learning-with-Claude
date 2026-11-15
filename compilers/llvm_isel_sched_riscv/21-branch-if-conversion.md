# Ch 21 — 分支、if-conversion、`MachineCombiner`

> **目标**：把「分支还是无分支」这件事从 IR 一路追到汇编：RISC-V 基础 ISA 没有 `cmov`，`select` 可能变成分支、`Zicond` 的 `czero`、或 `PseudoCCMOVGPR` 的短前向分支；`SelectOptimize` 还会用 sched model 的 `MispredictPenalty` 反过来决定要不要保留 `select`。最后看 `MachineCombiner` 如何在 RA 之前重排加法树，以及它为什么可能增加 spill。
>
> **环境**：LLVM 24.0.0git；`llc` 与 `opt`；`-mtriple=riscv64`；CPU 对照 `rocket-rv64`、`sifive-p670`、`sifive-p550`；`-mattr=+zicond`；`-O3` 触发 `select-optimize`；`-riscv-sched-mispredict-penalty`。所有 IR/汇编/MIR 均为本机实跑。

## 为什么分支属于「选择 × 调度」的接缝

分支不是一条普通指令，它改变控制流图。控制流图一变：

- 基本块数量变，**调度区域**跟着变。
- 跨块移动指令不再合法，scheduler 的自由度骤降。
- 分支预测失败有代价，这个代价由 sched model 的 `MispredictPenalty` 描述。
- 反过来，`MispredictPenalty` 又会影响 IR 层要不要把 `select` 变成分支（`SelectOptimize`）。

所以「分支」是少数几个能把 **sched model 的参数反向传回指令选择** 的地方。这一章的主线就是这条反馈回路。

## 先建立直觉：一个 `select` 的四种命运

```llvm
define i64 @cmp_sel(i64 %a, i64 %b, i64 %x, i64 %y) {
  %c = icmp slt i64 %a, %b
  %r = select i1 %c, i64 %x, i64 %y
  ret i64 %r
}
```

在 RISC-V 上至少有四种 lowering：

```text
1. 分支 + PHI           bnez/blt + mv，两个基本块
2. Zicond              slt + czero.nez + czero.eqz + or，一个基本块
3. 短前向分支伪指令     PseudoCCMOVGPRNoX0，pre-emit 才展开成 blt + c.mv
4. IR 层转回分支        SelectOptimize 把 select 改成 br + phi
```

第 1 和第 3 看起来都是「分支 + mv」，差别在**伪指令何时展开**：第 3 种在 post-RA 调度里还是一条 SUnit，展开发生在 `riscv-expand-pseudo-pre-emit`，任何调度器都看不到里面的 `blt` 和 `mv`。

## 主线一：基线、Zicond、短前向分支

### 基线：分支 + PHI

`rocket-rv64`：

```asm
cmp_sel:
        mv      a4, a0
        mv      a0, a2
        blt     a4, a1, .LBB0_2
        mv      a0, a3
.LBB0_2:
        ret
```

MIR 是标准的 `BNE`/`BLT` + `PHI`：

```text
bb.0:
  %3:gpr = SLT %0:gpr, %1:gpr
  BNE %3:gpr, $x0, %bb.2
bb.1:
bb.2:
  %6:gpr = PHI %3:gpr, %bb.0, %5:gpr, %bb.1
```

两个后继、三个基本块。调度器在 `bb.0` 末尾被切断。

### Zicond：一个基本块

加 `+zicond`：

```asm
cmp_sel:
        slt       a0, a0, a1
        czero.nez a1, a3, a0
        czero.eqz a0, a2, a0
        or        a0, a0, a1
        ret
```

一个基本块、四条指令。两条 `czero` 并行，`or` 汇合。没有分支预测，代价是无论条件真假都执行两条 `czero`（各 1 cycle）和一条 `or`。

### 短前向分支：`PseudoCCMOVGPRNoX0`

`sifive-p670` 打开 `TuneConditionalCompressedMoveFusion`，同一段 IR 在 `finalize-isel` 变成：

```text
%6:gpr = COPY $x0
%7:gprnox0 = PseudoCCMOVGPRNoX0 %5:gprnox0(tied-def 0), %3:gprnox0, 14180, %2:gpr, %6:gpr
```

`14180` 是 `bcc_opcode`，`%2` 是条件左值，`%6 = 0` 是右值。最终汇编：

```asm
cmp_sel:
        blt     a0, a1, .LBB0_2
        mv      a2, a3
.LBB0_2:
        mv      a0, a2
        ret
```

pre-emit 展开后的 MIR：

```text
bb.0:
  BLT killed $x10, killed $x11, %bb.2
bb.1:
  $x12 = ADDI killed $x13, 0
bb.2:
  $x10 = ADDI killed $x12, 0
```

`ADDI x, y, 0` 就是 `mv`。这条路径的本质是「短前向分支 + 压缩 `c.mv`」，比普通分支少一次跳转，但它仍然是分支；在不可预测的条件下，Zicond 通常更稳。

关键细节：`PseudoCCMOVGPRNoX0` 的 TableGen 定义是 `Sched<[]>`（`RISCVInstrInfoSFB.td`），也就是**没有调度信息**。post-RA 调度把它当默认 1-cycle 节点，直到 pre-emit 才变成 `blt+mv`。这和 `PseudoCCMOVGPR`（SFB 版本）不同，后者有 `WriteSFB`/`ReadSFBALU` 等完整声明。

## 主线二：`diamond` 不会被 ISel 自动 if-convert

很多人以为「加了 `Zicond`，所有分支都会变无分支」。并不是。看一个真正的 CFG 钻石：

```llvm
define i64 @diamond(i64 %a, i64 %b, i1 zeroext %c) {
entry:
  br i1 %c, label %t, label %f
t:
  %x = mul i64 %a, %b
  br label %m
f:
  %y = add i64 %a, %b
  br label %m
m:
  %v = phi i64 [ %x, %t ], [ %y, %f ]
  ret i64 %v
}
```

`-mattr=+zicond` 下仍然是：

```asm
diamond:
        beqz    a2, .LBB1_2
        mul     a0, a0, a1
        ret
.LBB1_2:
        add     a0, a0, a1
        ret
```

原因：ISel 只负责把 IR 的 `select` 节点 lowering；CFG 形状的钻石是 IR 层的控制流，`SelectionDAG` 不会主动把它合并成 `select`。要 if-convert，得靠 IR pass（`SimplifyCFG`、`CodeGenPrepare`、`SelectOptimize`）或 Machine 层的 if-converter（RISC-V 没开）。

这也说明「Zicond 能消除分支」的说法必须加限定：**它只能消除 IR 里已经是 `select` 的分支**。手工写的 `if/else` 如果没被 IR pass 转成 `select`，Zicond 无能为力。

## 主线三：sched model 的 `MispredictPenalty` 反向影响选择

这是本章最重要的一段。`SelectOptimize` 是 IR pass，只在 `-O3` 且 CPU 的 `TuneEnableSelectOptimize` 为真时加入流水线。它决定「一个 `select` 要不要改回分支」时，会算：

```text
BranchCost    = PredictedPathCost + MispredictCost
MispredictCost = max(MispredictPenalty, CondCost) * MispredictRate / 100
```

`MispredictPenalty` 来自 **sched model**。LLVM 提供一个覆盖开关 `-riscv-sched-mispredict-penalty`，官方测试 `sched-model-mispredict-penalty.ll` 就是用它证明这条反馈回路。

取测试里的 `sum_filtered`：

```llvm
%cond = icmp slt i64 %v, 0
%sel = select i1 %cond, i64 %v, i64 0
%acc.next = add i64 %acc, %sel
```

用 `opt` 跑 `select-optimize`，只改 `MispredictPenalty`：

```bash
opt -passes='require<profile-summary>,function(select-optimize)' \
    -mtriple=riscv64 -mcpu=sifive-p550 \
    -riscv-sched-mispredict-penalty=10000 \
    -select-opti-loop-cycle-gain-threshold=1 -S test.ll
```

`penalty=10000`（分支预测失败极贵）时，`select` 原样保留：

```llvm
%cond = icmp slt i64 %v, 0
%sel = select i1 %cond, i64 %v, i64 0
%acc.next = add i64 %acc, %sel
```

`penalty=0`（预测失败不要钱）时，`select` 被改成分支：

```llvm
%cond.frozen = freeze i1 %cond
br i1 %cond.frozen, label %select.end, label %select.false

select.false:
  br label %select.end

select.end:
  %sel = phi i64 [ %v, %loop ], [ 0, %select.false ]
```

这条链的完整因果是：

```text
sched model 的 MispredictPenalty
  -> SelectOptimize 的成本模型
  -> IR 里 select 还是 branch
  -> 基本块数量
  -> 调度区域数量
  -> 最终汇编
```

面试里如果能讲出这条回路，基本就能把「调度模型只是排指令用的」这个误解打掉。**sched model 也参与选择。**

### 为什么 IR 里多了一个 `freeze`

注意 `penalty=0` 的版本：

```llvm
%cond.frozen = freeze i1 %cond
br i1 %cond.frozen, label %select.end, label %select.false
```

`select` 和 `br` 对 poison 的传播规则不同。`select` 只对**被选中**的那一侧求值语义；拆成 `br` 后，条件本身如果带 poison，分支行为就变得不确定。`freeze` 把 poison 固定成任意非 poison 值，保证转换前后语义一致。这不是优化，是正确性补丁。

看 IR 时如果只盯 `select` 消失，会漏掉 `freeze`；反过来，看到 `freeze` 就知道这里发生过「select → branch」的转换，可以作为诊断线索。

### profile 信息也会改变答案

`SelectOptimize` 不只看 `MispredictPenalty`，还看分支概率。`MispredictRate` 来自 profile；没有 profile 时用启发式。一个「99% 走 true」的 select，转成分支后预测准确率很高，`MispredictCost` 很小，转换更容易发生；50/50 的 select 在高 penalty 机器上则倾向于保留 `select`。

所以同一个函数、同一个 CPU，只换 profile 数据，IR 形状就可能不同。分析「为什么这个 select 变成了分支」时，除了 `-mcpu`，还要检查输入是否带 `!prof`。

## 主线四：`MachineCombiner` 在 RA 之前改加法树

`MachineCombiner` 不是 ISel，也不是 scheduler，它位于 `addILPOpts()`，在 pre-RA scheduler 之前运行。它做的是「用结合律/分配律重排机器指令，缩短 critical path」，典型 pattern：

- 浮点 `fmul + fadd` → `fmadd`（需要 contract 允许）
- `add (shl x, c), y` → `shXadd`（Zba）
- 通用整数加法树的重关联

RISC-V 的 `getMachineCombinerPatterns` 先查 FP pattern，再查 `getSHXADDPatterns`，最后回落到 `TargetInstrInfo` 的通用 pattern。它默认开启（`-riscv-enable-machine-combiner` 默认 true）。

`ldmul.ll`（40 个 load + mul 的归约链）在 `sifive-u74` 上的实测很反直觉：

```bash
# 默认（MachineCombiner 开）
llc -O2 -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 -o - ldmul.ll
#   addi sp, sp, -240
#   17 spills

# 关闭
llc -O2 -mtriple=riscv64 -mattr=+m -mcpu=sifive-u74 \
    -riscv-enable-machine-combiner=false -o - ldmul.ll
#   addi sp, sp, -80
```

同一个函数，开启后栈帧从 80 字节涨到 240 字节。`-stats` 显示 `30 machine-combiner - Number of machineinst combined`。`-debug-only=machine-combiner` 能看到它在重关联 `ADD` 链：

```text
Dependence data for %84:gpr = ADD %83:gpr, %45:gpr
  NewRootDepth: 5  RootDepth: 6  It MustReduceDepth and it does it
  Resource length before replacement: 120 and after: 120
  As result it IMPROVES/PRESERVES Resource Length
```

它确实把深度从 6 降到 5，但重关联让中间值活得更久，greedy allocator 在多条并行链之间被逼出更多 spill。**「缩短 critical path」和「降低寄存器压力」在这里是对立的**；MachineCombiner 只看 depth 和 resource length，不看 live range 数量。

这也解释了一个常见现象：某个 pass 的统计说「优化了 30 条指令」，最终代码却更慢。统计的是它自己的目标函数，不是端到端性能。

## 踩雷

1. **`SelectOptimize` 需要 `-O3`**。`RISCVPassConfig::addIRPasses` 的条件是 `getOptLevel() == Aggressive`，`-O2` 不会加入这个 pass；在 `-O2` 下比较「有没有 select-opt」是无效实验。
2. **`MispredictPenalty` 是 sched model 字段，不是命令行魔法**。`-riscv-sched-mispredict-penalty` 只是覆盖它；真实 CPU 的值来自 `RISCVSched*.td` 的 `MispredictPenalty`。换 CPU 就会换选择。
3. **`PseudoCCMOVGPRNoX0` 的 `Sched<[]>` 是空的**。post-RA 调度看不到它的内部延迟；不要用它来研究「短前向分支的调度」。
4. **`diamond` 不会因为 `+zicond` 自动变成无分支**。只有 IR 里的 `select` 节点才会走 `lowerSELECT`；CFG 钻石要靠 IR pass 先转成 `select`。
5. **`MachineCombiner` 可能增加 spill**。`ldmul.ll` 的 240 vs 80 是真实反例；看到 `-stats` 的 combined 计数不要直接当成收益。
6. **`ADDI x, y, 0` 在汇编里打印成 `mv`**。反汇编/看 MIR 时不要把它当成一次真实加法；它的延迟按 `ADDI` 算。
7. **`-mattr=+zicond` 与 `-mcpu=sifive-p670` 是两条不同路径**。前者走 `czero`，后者走 `PseudoCCMOVGPRNoX0`；同时给两者时，要看 lowering 的优先级，不要假设 `zicond` 一定赢。

## 动手练习

1. 用 `-mattr=+m`、`+m,+zicond`、`-mcpu=sifive-p670` 三种配置编译 `cmp_sel`，画出各自的 CFG 与调度区域数量。
2. 用 `opt` 复现 `sum_filtered` 的 `penalty=10000` 与 `penalty=0` 两个版本，确认 IR 差异；再把 `-mcpu` 换成 `rocket-rv64`，观察 `TuneEnableSelectOptimize` 是否生效。
3. 在 `ldmul.ll` 上分别开关 `-riscv-enable-machine-combiner`，记录栈帧大小、spill 数、`-stats` 的 `machine-combiner` 计数，解释为什么「深度降低」会带来「spill 增加」。
4. 把 `diamond` 手工改写成 `select`，确认 `+zicond` 后基本块从 3 个降到 1 个；再改回 CFG，确认它不会自动合并。

## 重点整理

- RISC-V 的 `select` 有四条路：分支+PHI、`Zicond` 的 `czero`、`PseudoCCMOVGPR` 的短前向分支、IR 层 `SelectOptimize` 转回分支。
- 基本块数量决定调度区域数量；分支是调度自由度的硬边界。
- `MispredictPenalty` 是 sched model 字段，会被 `SelectOptimize` 用来决定 IR 形状，形成「调度模型 → 选择」的反馈。
- `PseudoCCMOVGPRNoX0` 在 pre-emit 才展开，且 `Sched<[]>`，调度器看不到内部结构。
- `MachineCombiner` 在 RA 前重关联，能缩短 depth，也可能增加 live range 与 spill；统计数字不等于端到端收益。

## 自我检核

- [ ] 我能画出 `cmp_sel` 在基线、Zicond、p670 三种配置下的 CFG。
- [ ] 我能解释 `MispredictPenalty` 如何改变 `SelectOptimize` 的决策，并复现两个 IR 版本。
- [ ] 我知道 `PseudoCCMOVGPRNoX0` 为什么在 post-RA 调度里是一条节点。
- [ ] 我能解释 `diamond` 为什么不会被 ISel 自动 if-convert。
- [ ] 我能用 `ldmul.ll` 的 240 vs 80 说明 MachineCombiner 的副作用。

## 延伸阅读

- `llvm/lib/CodeGen/SelectOptimize.cpp`：读 `BranchCost`/`SelectCost` 的计算与 `MispredictPenalty` 的来源；和本章 `sum_filtered` 对照。
- `llvm/lib/Target/RISCV/RISCVInstrInfoSFB.td`：看 `PseudoCCMOVGPR` 与 `PseudoCCMOVGPRNoX0` 的 Predicates 与 `Sched` 差异。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` 的 `lowerSELECT`：看 `Zicond`、`Select_GPR_Using_CC_*`、分支三条路径的优先级。
- `llvm/lib/CodeGen/MachineCombiner.cpp`：读 `getMachineCombinerPatterns` 的调用与 `CombinerObjective`；理解为什么它不检查 live range。
- `llvm/test/CodeGen/RISCV/sched-model-mispredict-penalty.ll`：官方如何用 `-riscv-sched-mispredict-penalty` 测这条反馈回路。
- `llvm/test/CodeGen/RISCV/selectopt.ll`：`+enable-select-opt` 与 `+predictable-select-expensive` 两个 tune feature 的行为差异。

→ [下一章：循环、软件流水与调度边界](./22-loop-software-pipelining.md)
