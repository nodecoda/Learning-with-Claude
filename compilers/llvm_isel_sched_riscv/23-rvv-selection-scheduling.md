# Ch 23 — RVV：`vsetvli`、向量选择与调度

> **目标**：理解 RVV 后端与标量后端的三处结构差异：向量指令依赖 `VL`/`VTYPE` 隐式状态；`vsetvli` 在寄存器分配之后才插入；pre-RA scheduler 有一个专门的 `RISCVPreRAMachineSchedStrategy` 做 `vsetvli` tie-break。学完你能解释为什么一段混合 SEW 的代码会插入六条 `vsetivli`，以及向量指令的 sched class 为什么难以精确建模。
>
> **环境**：LLVM 24.0.0git；`llc -mtriple=riscv64 -mattr=+m,+v -riscv-v-vector-bits-min=256/512`；`llvm-mca -mcpu=sifive-p670` 与 `-mcpu=spacemit-x60`；pass `riscv-insert-vsetvli`；`RISCVPreRAMachineSchedStrategy`。所有汇编、MIR、周期数均为本机实跑。

## 为什么 RVV 不能照搬标量的调度模型

标量指令的操作数是寄存器，指令之间通过寄存器依赖。RVV 多了一层**隐式状态**：

- `VL`：当前处理的元素个数（vector length）。
- `VTYPE`：SEW（元素宽度）、LMUL（寄存器组倍数）、tail/mask 策略。
- `vsetvli` / `vsetivli` 修改这两个状态，后续向量指令隐式读取它们。

这带来三个后果：

1. **依赖边不只来自显式操作数**。一条 `vadd.vv` 依赖最近的 `vsetvli`，即使它的汇编操作数里没有 `vl`。
2. **`vsetvli` 有副作用**，不能随意删除或移动；它的位置直接决定循环里要切换多少次 VTYPE。
3. **向量指令的延迟与 VL/SEW/LMUL 有关**。VL 越大，执行时间越长；sched model 只能给一个固定值（通常是保守值）。

所以 RVV 的「选择 → 调度」接缝比标量宽得多：选择不仅决定用哪条向量指令，还决定 VTYPE 的组合；调度不仅要排指令，还要尽量把相同 VTYPE 的指令聚在一起，减少 `vsetvli` 切换。

## 先建立直觉：一条向量指令的生命周期

以固定长度向量加法为例：

```llvm
define void @vadd(ptr %a, ptr %b, ptr %c) {
  %x = load <4 x i64>, ptr %a, align 32
  %y = load <4 x i64>, ptr %b, align 32
  %z = add <4 x i64> %x, %y
  store <4 x i64> %z, ptr %c, align 32
  ret void
}
```

编译：

```bash
llc -O2 -mtriple=riscv64 -mattr=+m,+v -riscv-v-vector-bits-min=256 -o - rvv.ll
```

汇编：

```asm
vadd:
        vsetivli zero, 4, e64, m1, ta, ma
        vle64.v  v8, (a0)
        vle64.v  v9, (a1)
        vadd.vv  v8, v8, v9
        vse64.v  v8, (a2)
        ret
```

时间线：

```text
finalize-isel        PseudoVLE64_V_M1 / PseudoVADD_VV_M1（没有 vsetvli）
pre-RA scheduler     同样没有 vsetvli
RA                   ...
riscv-insert-vsetvli 插入 PseudoVSETIVLI
post-RA scheduler    看到 PseudoVSETIVLI + 向量伪指令
asm printer          PseudoVSETIVLI -> vsetivli
```

关键点：**pre-RA scheduler 看不到 `vsetvli`**。它只能根据向量伪指令的 sched class 估算延迟，无法知道「这条指令会不会触发 VTYPE 切换」。切换的代价要等到 post-RA 才进入模型。

## 主线一：`finalize-isel` 里没有 `vsetvli`

`-print-after=finalize-isel`：

```text
bb.0:
  %3:vr = PseudoVLE64_V_M1 $noreg(tied-def 0), %0:gpr, 4, 6, 2 :: (load (s256) from %ir.a)
  %4:vr = PseudoVLE64_V_M1 $noreg(tied-def 0), %1:gpr, 4, 6, 2 :: (load (s256) from %ir.b)
  %5:vr = PseudoVADD_VV_M1 $noreg(tied-def 0), %3:vr, %4:vr, 4, 6, 1
  PseudoVSE64_V_M1 %5:vr, %2:gpr, 4, 6 :: (store (s256) into %ir.c)
  PseudoRET
```

三个立即数 `4, 6, 2` 分别是 VL、SEW 编码、LMUL 编码（具体编码由 TableGen 的 `VTypeIOp` 决定）。此时没有 `VL`/`VTYPE` 的 def，也没有 `vsetivli`。

`-print-after=postmisched`：

```text
dead $x0 = PseudoVSETIVLI 4, 216, implicit-def $vl, implicit-def $vtype
renamable $v8 = PseudoVLE64_V_M1 undef renamable $v8(tied-def 0), killed renamable $x10, 4, 6, 2, implicit $vl, implicit $vtype
renamable $v9 = PseudoVLE64_V_M1 undef renamable $v9(tied-def 0), killed renamable $x11, 4, 6, 2, implicit $vl, implicit $vtype
renamable $v8 = PseudoVADD_VV_M1 undef renamable $v8(tied-def 0), killed renamable $v8, killed renamable $v9, 4, 6, 1, implicit $vl, implicit $vtype
PseudoVSE64_V_M1 killed renamable $v8, killed renamable $x12, 4, 6, implicit $vl, implicit $vtype
```

`216` 是 vtypei 的编码：`0xC0 | (3 << 3) | 0` = `192 + 24`，其中 `0xC0` 是 tail/mask 策略位（`ta, ma`），`3` 是 SEW=64 的编码，`0` 是 LMUL=m1。这条 `PseudoVSETIVLI` 是 `riscv-insert-vsetvli` 在 RA 之后插入的。

## 主线二：LMUL 与寄存器组

把向量宽度从 4 个 `i64` 改成 8 个，并把最小 VLEN 设为 256：

```llvm
%x = load <8 x i64>, ptr %a, align 64
```

汇编变成：

```asm
vadd8:
        vsetivli zero, 8, e64, m2, ta, ma
        vle64.v  v8, (a0)
        vle64.v  v10, (a1)
        vadd.vv  v8, v8, v10
        vse64.v  v8, (a2)
        ret
```

两个变化：

- `m1` → `m2`：8 个 64-bit 元素需要两个向量寄存器（`v8` 和 `v9` 组成一组，`v10`/`v11` 组成另一组）。
- 目的寄存器仍是 `v8`，源操作数是 `v10`，因为 `v8` 的组已经占了 `v8/v9`。

如果 `-riscv-v-vector-bits-min=512`，同样的 `<8 x i64>` 会用 `m1`（VLEN 足够大，一组寄存器就能放下 8 个元素）：

```asm
vsetivli zero, 8, e64, m1, ta, ma
vle64.v  v8, (a0)
vle64.v  v9, (a1)
vadd.vv  v8, v8, v9
```

**LMUL 不是由 IR 的向量宽度单独决定的，还取决于目标的最小 VLEN。** 这是 RVV 选择与标量选择最大的不同：同一条 IR 在不同的 `-riscv-v-vector-bits-min` 下会选成不同的 LMUL，进而改变寄存器组、寄存器压力和 `vsetvli` 编码。

## 主线三：混合 SEW 的代价

看一个混合 `e64` 与 `e32` 的函数：

```llvm
define void @vmix(ptr %a, ptr %b, ptr %c) {
  %x = load <4 x i64>, ptr %a, align 32
  %y = load <8 x i32>, ptr %b, align 32
  %z = add <4 x i64> %x, %x
  %w = add <8 x i32> %y, %y
  store <4 x i64> %z, ptr %c, align 32
  ...
}
```

`-riscv-v-vector-bits-min=512` 的汇编：

```asm
vmix:
        vsetivli zero, 8, e32, mf2, ta, ma
        vle32.v  v8, (a1)
        vsetivli zero, 4, e64, m1, ta, ma
        vle64.v  v9, (a0)
        vsetivli zero, 8, e32, mf2, ta, ma
        vadd.vv  v8, v8, v8
        vsetivli zero, 4, e64, m1, ta, ma
        vadd.vv  v9, v9, v9
        addi     a0, a2, 64
        vsetivli zero, 8, e32, mf2, ta, ma
        vse32.v  v8, (a0)
        vsetivli zero, 4, e64, m1, ta, ma
        vse64.v  v9, (a2)
        ret
```

六条 `vsetivli`。这不是编译器「不会优化」，而是源码把两种 VTYPE 交错在一起：`e32/mf2` 和 `e64/m1` 交替出现，每条向量指令前都要切换。`vsetvli` 的延迟与流水线冲刷代价在 sched model 里通常按固定值计，实际可能更贵。

如果重排 IR，把同 VTYPE 的 load/add/store 放一起，`vsetivli` 可以降到两条（每种 VTYPE 一条）。这就是为什么 RVV 的性能优化经常从**循环/基本块内的类型分组**开始，而不是先调 scheduler。

## 主线四：`RISCVPreRAMachineSchedStrategy` 的 tie-break

RISC-V 的 pre-RA scheduler 不是直接用 `GenericScheduler`，而是 `RISCVPreRAMachineSchedStrategy`（`RISCVMachineScheduler.cpp`）。它只加了一个启发式：**在其它条件都相同时，优先选择与当前 `vsetvli` 信息兼容的指令**。

核心逻辑（简化）：

```cpp
bool RISCVPreRAMachineSchedStrategy::tryCandidate(Cand, TryCand, Zone) {
  // 1. 先复制 GenericScheduler 的全部启发式
  //    PhysReg, RegExcess, RegCritical, Stall, Cluster, Weak, RegMax,
  //    ResourceReduce, ResourceDemand, Latency, NodeOrder
  ...
  // 2. 只有在 TryCand 落到 NodeOrder（最后一级）时，才用 vsetvli 启发式
  if (TryCand.Reason != NodeOrder && TryCand.Reason != NoCand)
    return true;

  if (ST->enableVsetvliSchedHeuristic() &&
      tryVSETVLIInfo(getVSETVLIInfo(TryCand.SU->getInstr()),
                     getVSETVLIInfo(Cand.SU->getInstr()),
                     TryCand, Cand, Cluster))
    return TryCand.Reason != NoCand;

  return TryCand.Reason != NoCand;
}
```

`tryVSETVLIInfo` 做两件事：

- **不比较 top 与 bottom 边界的候选**（`Cand.AtTop != TryCand.AtTop` 直接返回 false），因为两个方向的 `vsetvli` 数据流不可比。
- 用 `RISCV::DemandedFields::all()` 判断候选指令的 VTYPE 信息是否与当前 `TopInfo`/`BottomInfo` 兼容；兼容的优先。

`TopInfo`/`BottomInfo` 在 `enterMBB` 时清空，在 `schedNode` 里根据已调度的 `vsetvli` 更新。也就是说，这个启发式是**局部贪心**：它让当前基本块内尽量少切换 VTYPE，但不做全局重排。

注意两个限制：

1. 它是最后一级 tie-break，只有在寄存器压力、资源、延迟、NodeOrder 都打平时才起作用。遇到压力大的区域，寄存器压力优先。
2. 它由 tune feature `enable-vsetvli-sched-heuristic` 控制。默认的 CPU 模型里不一定打开；可以显式加 `-mattr=+enable-vsetvli-sched-heuristic` 观察。

## 主线五：向量指令的 sched class

RVV 的 sched class 在 `RISCVScheduleV.td`，按 LMUL 与指令类型展开。以 `PseudoVSETIVLI` 为例：

```tablegen
def PseudoVSETIVLI : Pseudo<...>,
                     PseudoInstExpansion<(VSETIVLI ...)>,
                     Sched<[WriteVSETIVLI]>;
```

`PseudoVSETVLI` 则是 `Sched<[WriteVSETVLI, ReadVSETVLI]>`。`vsetivli` 没有显式源寄存器（VL 是立即数），所以没有 `Read`；`vsetvli` 要读 `rs1` 作为 AVL，所以有 `ReadVSETVLI`。

向量 load/store/add 的定价用 `llvm-mca -instruction-info` 看：

```text
=== sifive-p670 ===
 1   1   1.00   U     vsetivli zero, 4, e64, m1, ta, ma
 1   8   1.00   *     vle64.v  v8, (a0)
 1   8   1.00   *     vle64.v  v9, (a1)
 1   2   0.50         vadd.vv  v8, v8, v9
 1   8   1.00   *     vse64.v  v8, (a2)

=== spacemit-x60 ===
 1   1   1.00   U     vsetivli zero, 4, e64, m1, ta, ma
 1   3   3.00   *     vle64.v  v8, (a0)
 1   3   3.00   *     vle64.v  v9, (a1)
 1   4   2.00         vadd.vv  v8, v8, v9
 1   3   3.00   *     vse64.v  v8, (a2)
```

`100` 次迭代的总周期：P670 是 221，X60 是 1302；`Block RThroughput` 分别是 2.0 和 9.0。差异来自：

- P670 的向量 load 延迟 8、吞吐 1.0；X60 的 load 延迟 3、吞吐 3.0（每 3 cycle 才能发一条）。
- P670 的 `vadd` 延迟 2；X60 是 4。

这些数字是**固定值**，不随 VL 变化。真实的 RVV 硬件里，一条 `vadd.vv` 在 VL=4 与 VL=64 时的延迟可能差一个数量级；sched model 只能取一个保守值。这是 RVV 调度模型最大的近似，也是为什么很多 RVV 性能问题不能只靠 `llc` 的调度解决，要看硬件计数器。

## 主线六：RVV 的 pass 顺序

把标量与向量的关键 pass 放在一条时间线上（`-debug-pass=Structure` 的真实顺序）：

```text
-riscv-isel
-finalize-isel
-machine-scheduler          # pre-RA：只有向量伪指令，没有 vsetvli
-greedy                     # RA
-riscv-insert-vsetvli       # 插入 PseudoVSETVLI/PseudoVSETIVLI
-riscv-dead-defs
-virt-reg-rewriter
-postmisched                # post-RA：看到 vsetvli 与向量伪指令
-riscv-expand-pseudo-post-ra
```

两个调度器看到的世界不同：

| 阶段 | 看到的向量指令 | 看到的 `vsetvli` | 能改 VTYPE 分组吗 |
|---|---|---|---|
| pre-RA `machine-scheduler` | `PseudoVLE64_V_M1` 等 | 没有 | 只能靠伪指令的隐式信息（有限） |
| post-RA `postmisched` | 同样的伪指令 + 物理寄存器 | 有 `PseudoVSETIVLI` | 能调顺序，但 RA 已定，寄存器组不可变 |

这解释了一个常见现象：pre-RA 调度看起来「没有为 vsetvli 做任何事」，因为它确实看不到。真正能做 VTYPE 分组的时机在 IR 层（重排循环体）和 pre-RA 的 tie-break（有限），而不是 post-RA。

## 踩雷

1. **固定长度向量需要 `-riscv-v-vector-bits-min`**。不设这个参数，`<4 x i64>` 可能被 scalarize 或走不同的 LMUL；本课统一写 256 或 512，并在结论里注明。
2. **`vsetvli` 在 `finalize-isel` 里不存在**。它由 `riscv-insert-vsetvli` 在 RA 之后插入；用 `-print-after=finalize-isel` 找不到它，不代表它不会生成。
3. **pre-RA scheduler 看不到 VTYPE 切换**。`RISCVPreRAMachineSchedStrategy` 只能用伪指令的 sched class 估算，真正的 `vsetvli` 代价要到 post-RA 才进入模型。
4. **`vsetivli` 的 `216` 不是任意常数**。它编码 SEW/LMUL/ta/ma；换 SEW 或 LMUL，立即数就变。不要手工改这个数。
5. **`llvm-mca` 必须带 `-mattr=+v`**。只写 `-mcpu=sifive-p670` 可能因为没有 `+v` 而拒绝向量指令；本课统一写 `-mattr=+v`。
6. **`Block RThroughput` 不是 VL 相关延迟**。它假设固定 VL/SEW；真实性能要用硬件计数器或 `-iterations` 之外的建模。
7. **混合 SEW 的 `vsetivli` 数量由 IR 顺序决定**。先做类型分组，再谈调度；scheduler 不会跨基本块重排 VTYPE。

## 动手练习

1. 用 `-riscv-v-vector-bits-min=256` 与 `512` 分别编译 `<8 x i64>` 加法，记录 LMUL、寄存器组、`vsetivli` 立即数。
2. 把 `vmix` 的 IR 重排成「先做完全部 e32，再做全部 e64」，数 `vsetivli` 从几条降到几条。
3. 用 `-mattr=+enable-vsetvli-sched-heuristic` 与默认编译 `vmix`，对比 pre-RA 调度后的 MIR 顺序；解释为什么差异可能很小（tie-break 只在最后一级生效）。
4. 用 `llvm-mca -instruction-info` 对比 `sifive-p670` 与 `spacemit-x60` 的 `vle64.v`/`vadd.vv`/`vse64.v`，解释 221 与 1302 的来源。
5. 把 `vadd` 的 VL 从 4 改成 64（`<64 x i64>`，`-riscv-v-vector-bits-min=512`），观察 LMUL 与 `vsetivli`，并讨论 sched model 的固定延迟为什么不适用。

## 重点整理

- RVV 的隐式状态 `VL`/`VTYPE` 让依赖边超出显式操作数；`vsetvli` 是有副作用的配置指令。
- `vsetvli` 由 `riscv-insert-vsetvli` 在 RA 之后插入；pre-RA scheduler 看不到它。
- LMUL 由向量宽度与 `-riscv-v-vector-bits-min` 共同决定；同一 IR 可能选成 m1 或 m2。
- 混合 SEW 会导致大量 `vsetivli` 切换；类型分组比调度更根本。
- `RISCVPreRAMachineSchedStrategy` 只在最后一级 tie-break 用 `vsetvli` 兼容性，且受 tune feature 控制。
- 向量指令的 sched class 是固定延迟，不随 VL 变化；这是模型的主要近似。

## 自我检核

- [ ] 我能画出向量指令从 ISel 到 `vsetivli` 插入的 pass 顺序。
- [ ] 我能解释 `<8 x i64>` 在 256 与 512 下为什么选不同 LMUL。
- [ ] 我能数出 `vmix` 里 `vsetivli` 的条数，并说明如何减少。
- [ ] 我知道 `RISCVPreRAMachineSchedStrategy` 与 `GenericScheduler` 的唯一区别。
- [ ] 我能用 `llvm-mca` 读出 `vle64.v`/`vadd.vv` 的延迟与吞吐，并说明它为什么不随 VL 变。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVMachineScheduler.cpp`：读 `tryCandidate` 与 `tryVSETVLIInfo`，重点看 `NodeOrder` 与 `DemandedFields::all()`。
- `llvm/lib/Target/RISCV/RISCVInsertVSETVLI.cpp`：看 `vsetvli` 如何在 RA 之后插入、如何做数据流合并。
- `llvm/lib/Target/RISCV/RISCVScheduleV.td`：看 `WriteVLDE`/`WriteVADD` 的 LMUL 展开与 `Sched` 声明。
- `llvm/lib/Target/RISCV/RISCVInstrInfoVPseudos.td` 的 `PseudoVSETVLI` / `PseudoVSETIVLI`：看 sched class 与 `PseudoInstExpansion`。
- `llvm/test/CodeGen/RISCV/rvv/` 下与 `vsetvli` 调度相关的测试：看官方如何用 `-print-after` 与 FileCheck 验证插入位置。

→ [下一章：诊断树](./24-diagnosis-tree.md)
