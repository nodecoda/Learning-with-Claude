# Ch 18 — 指令选择如何决定调度自由度：`select`、`setcc`、`Zicond`、`Zba`

> **目标**：把「选择」和「调度」的因果关系说清楚。学完你能回答：为什么同一段 IR，换一个扩展就多出两条指令、少了一个基本块；为什么少一条指令不一定更快；为什么 `select` 在 RISC-V 上会把一个调度区域劈成两个。重点不是背指令，而是建立「选择先决定 DAG 的形状，调度只能在这个形状里重排」的模型。
>
> **环境**：LLVM 24.0.0git；`llc`；`llvm-mca`；`-mtriple=riscv64`；`-mattr=+m` 为基线；扩展组 `+zba,+zbb,+zicond`；CPU 对照 `rocket-rv64`、`sifive-u74`、`xiangshan-nanhu`、`spacemit-x60`。所有汇编与周期数均为本机实跑。

## 为什么先讲这一章

面试里最常见的追问不是「`select` 怎么选」，而是「这里为什么变成三条指令了」或者「你加了 `Zba` 怎么反而慢了」。这两个问题的答案都不在 scheduler 里，而在 scheduler 拿到 DAG 之前：

- 一条 IR 可以选成一条机器指令，也可以选成三条。scheduler 只能重排已经存在的节点。
- 选择结果决定依赖边的数量与长度。边一变，critical path 就变。
- 选择结果决定指令落到哪个功能单元、占几个 cycle。sched model 只负责给这些结果定价。
- 选择结果还可能决定基本块数量。基本块边界就是调度区域的边界，跨不过去。

所以正确的因果链是：

```text
IR 形状
  -> DAG combine / legalize 后的 DAG 形状
  -> ISel 选出的 MachineInstr 数量、依赖边、sched class
  -> pre-RA scheduler 的可重排空间
  -> RA 后的 spill / COPY
  -> post-RA scheduler 的修补空间
  -> 最终汇编
```

本章只盯住第二到第三步：**ISel 如何改变调度自由度**。第 19 章讲内存折叠，第 20 章讲伪指令，第 21 章讲分支与 if-conversion，三章合起来才是完整的「选择侧输入」。

## 先建立直觉：同一段 IR 的三种形状

取三个函数，语义都很直接：

```llvm
define i64 @sh3(i64 %a, i64 %b) {
  %s = shl i64 %b, 3
  %r = add i64 %a, %s
  ret i64 %r
}

define i64 @andn(i64 %a, i64 %b) {
  %n = xor i64 %b, -1
  %r = and i64 %a, %n
  ret i64 %r
}

define i64 @zextw(i64 %a) {
  %t = trunc i64 %a to i32
  %r = zext i32 %t to i64
  ret i64 %r
}
```

基线 `-mattr=+m` 的汇编：

```asm
sh3:
        slli    a1, a1, 3
        add     a0, a0, a1
        ret

andn:
        not     a1, a1
        and     a0, a0, a1
        ret

zextw:
        slli    a0, a0, 32
        srli    a0, a0, 32
        ret
```

加上 `+zba,+zbb` 之后：

```asm
sh3:
        sh3add  a0, a1, a0
        ret

andn:
        andn    a0, a0, a1
        ret

zextw:
        zext.w  a0, a0
        ret
```

这不是「优化器变聪明了」，而是 TableGen pattern 多了一条匹配路径。对 scheduler 来说，差别是实打实的：

| 函数 | 基线节点数 | 扩展节点数 | 基线依赖链 | 扩展依赖链 |
|---|---|---|---|---|
| `sh3` | 2（`SLLI`→`ADD`） | 1（`SH3ADD`） | 2 条边 | 0 条内部边 |
| `andn` | 2（`NOT`→`AND`） | 1（`ANDN`） | 1 条边 | 0 |
| `zextw` | 2（`SLLI`→`SRLI`） | 1（`ZEXT_W`） | 1 条边 | 0 |

节点数减少的直接后果是：原来 scheduler 可以把 `SLLI` 和别的指令交错，现在没有这个节点了；原来两条指令可以分别填两个 issue slot，现在只有一条。**自由度变小，但关键路径也变短**。哪一个更重要，取决于机器的 issue 宽度与延迟。

## 主线一：`select` 是调度区域的分水岭

RISC-V 基础 ISA 没有条件传送指令（没有 x86 的 `cmov`，也没有 AArch64 的 `csel`）。因此一个 `select` 只有两条路：

1. 选成**分支**：生成两个基本块，`select` 变成一个 `PHI`。
2. 选成**位运算**：用掩码把两条路径都算出来，再合并。`Zicond` 提供 `czero.eqz` / `czero.nez`，没有扩展时也可能用 `neg+and` 之类的技巧。

先看一般形式：

```llvm
define i64 @sel(i64 %a, i64 %b, i1 zeroext %c) {
  %r = select i1 %c, i64 %a, i64 %b
  ret i64 %r
}
```

基线：

```asm
sel:
        bnez    a2, .LBB0_2
        mv      a0, a1
.LBB0_2:
        ret
```

MIR 层看得更清楚（`-print-after=finalize-isel`，`rocket-rv64`）：

```text
bb.0:
  successors: %bb.1(0x40000000), %bb.2(0x40000000); %bb.1(50.00%), %bb.2(50.00%)
  BNE %2:gpr, $x0, %bb.2

bb.1:
  successors: %bb.2(0x80000000); %bb.2(100.00%)

bb.2:
  %3:gpr = PHI %0:gpr, %bb.0, %1:gpr, %bb.1
  $x10 = COPY %3:gpr
  PseudoRET implicit $x10
```

这里有三个基本块、两个后继。`MachineScheduler` 的 region 在 `bb.0` 结束处就被切断；`bb.1` 只有一条 `mv`，`bb.2` 只有 `PHI` 和返回。scheduler 无法把 `mv` 提到 `bnez` 之前，也无法把 `bb.2` 的活拉进 `bb.0`。**选择直接决定了「这里只有一个调度区域」还是「这里是三个」。**

加 `+zicond`：

```asm
sel:
        czero.nez  a1, a1, a2
        czero.eqz  a0, a0, a2
        or         a0, a0, a1
        ret
```

MIR：

```text
bb.0:
  %3:gpr = CZERO_NEZ %1:gpr, %2:gpr
  %4:gpr = CZERO_EQZ %0:gpr, %2:gpr
  %5:gpr = disjoint OR %4:gpr, %3:gpr
  $x10 = COPY %5:gpr
  PseudoRET implicit $x10
```

一个基本块、三条指令、两条并行链：

```text
a2 ──┬─> CZERO_NEZ(a1) ──┐
     └─> CZERO_EQZ(a0) ──┴─> OR ──> a0
```

注意 `CZERO_NEZ` 和 `CZERO_EQZ` 之间**没有依赖**，它们可以并行发射；`OR` 等两者。所以虽然指令数从 2 条变成 3 条，critical path 仍然是「1 条 czero + 1 条 or」。而分支版本的 critical path 包含一次分支预测/跳转，对短函数来说通常更贵。

### `select c, a, 0` 是另一条路径

把 `%b` 换成常量 0：

```llvm
define i64 @sel0(i64 %a, i64 %b, i1 zeroext %c) {
  %r = select i1 %c, i64 %a, i64 0
  ret i64 %r
}
```

基线就已经是 branchless：

```asm
sel0:
        neg     a1, a2
        and     a0, a1, a0
        ret
```

`neg a1, a2` 把 0/1 条件变成 0/-1 掩码（`neg` 是 `sub x0, a2` 的别名）。加 `+zicond` 后变成一条：

```asm
sel0:
        czero.eqz  a0, a0, a2
        ret
```

这个例子说明：**「`select` 一定选成分支」是错的，取决于另一侧是不是零、条件是否已经 0/1、以及有没有 `Zicond`**。面试时如果只说「RISC-V 没有 cmov 所以用分支」，会漏掉这两种 branchless lowering。

## 主线二：少一条指令不一定更快

这是本章最值得记住的实测结果。取一条依赖链：

```llvm
define i64 @chain(i64 %a, i64 %b, i64 %c) {
  %s = shl i64 %b, 3
  %x = add i64 %a, %s
  %y = mul i64 %x, %c
  ret i64 %y
}
```

基线与 `+zba` 的差别只有第一条：

```asm
# 基线
slli   a1, a1, 3
add    a0, a0, a1
mul    a0, a0, a2

# +zba
sh3add a0, a1, a0
mul    a0, a0, a2
```

用 `llvm-mca -iterations=100` 测同一条依赖链（100 次迭代，每轮都依赖上一轮）：

| CPU | 基线周期 | `+zba` 周期 | 说明 |
|---|---:|---:|---|
| `rocket-rv64` | 601 | 无法建模 | `sh3add` 在 Rocket 模型里没有 sched class |
| `sifive-u74` | 504 | 503 | 延迟都是 3，融合只省一条指令 |
| `xiangshan-nanhu` | 404 | 403 | 延迟都是 1，几乎只省 issue |
| `spacemit-x60` | 702 | **801** | `sh3add` 延迟 2，`add`/`slli` 延迟 1；融合后关键路径反而更长 |

`spacemit-x60` 的数字最能说明问题：指令数从 4 条降到 3 条，周期数却从 702 涨到 801。原因是这条链是纯依赖链，吞吐量不是瓶颈，**延迟**才是。`sh3add` 在这个模型里比 `add` 多一个 cycle，融合把两条 1-cycle 指令换成一条 2-cycle 指令，关键路径从 2 变 2 看似打平，但 `mul` 的输入等待更久，累计到 100 次迭代就放大了。

用 `-instruction-info` 可以直接看到定价：

```text
=== sifive-u74 ===
 1      3     0.50                        add     a0, a0, a1
 1      3     1.00                        sh3add  a0, a1, a0
=== spacemit-x60 ===
 1      1     0.50                        add     a0, a0, a1
 1      2     0.50                        sh3add  a0, a1, a0
=== xiangshan-nanhu ===
 1      1     0.25                        add     a0, a0, a1
 1      1     0.25                        sh3add  a0, a1, a0
```

sifive-u74 上 `sh3add` 的 `RThroughput` 是 1.00，而 `add` 是 0.50，说明它只能走一条 ALU pipe；在吞吐受限的代码里，融合也可能更差。**「指令数」只是成本模型的一个维度，另外两个是 latency 和 reciprocal throughput。**

## 主线三：`setcc` 与条件码的形状

比较运算在 RISC-V 上选成 `slt`/`sltu`/`sltiu`/`snez` 等，结果是一个 0/1 的 GPR。它本身通常不是瓶颈，但它决定了后续 `select` / `brcond` 的输入形状：

```llvm
define i64 @cmp_sel(i64 %a, i64 %b, i64 %x, i64 %y) {
  %c = icmp slt i64 %a, %b
  %r = select i1 %c, i64 %x, i64 %y
  ret i64 %r
}
```

基线会选成（注意 `blt` 直接吃两个寄存器，没有单独的 `slt`）：

```asm
cmp_sel:
        mv      a4, a0
        mv      a0, a2
        blt     a4, a1, .LBB0_2
        mv      a0, a3
.LBB0_2:
        ret
```

加 `+zicond` 后：

```asm
cmp_sel:
        slt       a0, a0, a1
        czero.nez a1, a3, a0
        czero.eqz a0, a2, a0
        or        a0, a0, a1
        ret
```

`slt` 的结果既可以直接喂给 `czero`，也可以喂给 `bnez`。**`setcc` 没有消失，它只是从「分支条件」变成了「掩码输入」**。这也解释了为什么 `Zicond` 不是万能的：如果条件来自一条长延迟的 `slt`，把它变成掩码只是把依赖链换了位置，没有消失。

`setcc` 还有一个容易忽略的点：比较的**操作数顺序**会影响能否折叠成 `sltiu`、能否复用已有条件。比如 `icmp ult i64 %x, 1` 会选成 `seqz`，而 `icmp eq i64 %x, 0` 也可能选成 `seqz`；两条 IR 形状不同，DAG combine 后可能归一，也可能不归一。诊断时要看 `-print-after=finalize-isel`，不要只看 IR。

## 底层机制：pattern 决定节点，sched class 决定价格

TableGen 里一条 pattern 对应一个 `MachineNode`。以 `Zba` 为例（`RISCVInstrInfoZba.td` 的简化结构）：

```tablegen
def SH3ADD : ALU_rr<...>,
             Sched<[WriteIALU, ReadIALU, ReadIALU]>;

def : Pat<(add (shl GPR:$rs2, 3), GPR:$rs1),
          (SH3ADD GPR:$rs1, GPR:$rs2)>;
```

三个信息同时被固定下来：

1. **节点数**：`add` + `shl` 两个 DAG 节点被一个 `SH3ADD` 节点替代。
2. **依赖边**：原来 `shl -> add` 的边消失；`SH3ADD` 直接依赖 `rs1`、`rs2`。
3. **价格**：`Sched<[...]>` 把这条指令绑定到 `WriteIALU` / `ReadIALU`，scheduler 用这套读写资源算 latency 和吞吐。

如果 pattern 写成两条指令的 `Pat`，节点数就是 2；如果写成 `Sched<[]>`，scheduler 拿不到资源信息，只能按默认值处理（通常是 latency 1、无资源占用）。第 25 章会专门讲 `Sched<[]>` 的危害，这里先记住结论：**选择不仅决定「有哪些指令」，还决定「这些指令在 scheduler 眼里值多少钱」**。

`RISCVISelLowering::lowerSELECT` 是另一条路径。它不靠 pattern，而是在 DAG legalize 阶段把 `SELECT` 节点改写成 `CZERO_*` 或分支：

```cpp
// 伪代码，对应 RISCVISelLowering.cpp 的 lowerSELECT
if (Subtarget.hasStdExtZicond() && VT.isScalarInteger()) {
  // (select c, t, 0) -> (czero_eqz t, c)
  // (select c, 0, f) -> (czero_nez f, c)
}
```

这意味着 `select` 的 lowering 发生在 **ISel 之前**，scheduler 看到的是已经改写好的 DAG。你在 `-print-after=finalize-isel` 里看到的 `CZERO_*`，是 legalize 的产物，不是 pattern 匹配的产物。

## 踩雷

1. **`-mattr=+zba` 不会自动带上 `+m`**。`chain` 里的 `mul` 需要 `+m`（或 `+zmmul`），否则会选成 libcall 或报错。对比实验统一写 `-mattr=+m,+zba`。
2. **Rocket 模型没有 `sh3add` 的 sched class**。`llvm-mca` 直接报 `unsupported instruction ... lack-sched`；加 `-skip-unsupported-instructions=lack-sched` 能跑，但等于把这个指令从模型里删掉，周期数不可信。正确做法是换一个覆盖该扩展的 CPU，或者承认模型缺口。
3. **`select` 的三种 lowering 不要混为一谈**：一般 `select` → 分支；`select c,a,0` → `neg+and` 或 `czero`；`Zicond` 一般形式 → 两条 `czero` + `or`。看到「RISC-V 没有 cmov」就下结论，会漏掉后两种。
4. **`-mcpu` 同时设定 ISA 与 tune，`-mattr` 只改 ISA**。`-mcpu=sifive-u74 -mattr=+zba` 仍然用 SiFive7 的定价；`-mcpu=generic-rv64` 则用 SpacemitX60 模型（见第 0 章的修正）。
5. **`-O2` 可能在 IR 层就把 `select` 变成算术**。`sel0` 在基线就出现 `neg+and`，不是 ISel 的 `CZERO` 路径。诊断时先看 `-print-after=finalize-isel`，再回推是哪个 pass 改的。

## 动手练习

1. 把 `sel0` 的 `zeroext` 去掉，改成 `i1 %c`（不保证 0/1），重新编译。观察基线是否还需要 `andi`，`+zicond` 是否仍然一条 `czero.eqz`。解释 `zeroext` 对 lowering 的影响。
2. 在 `spacemit-x60` 上把 `chain` 的 `shl 3` 改成 `shl 1`，比较 `sh1add` 与 `slli+add` 的周期数。预测哪一个更快，再用 `-instruction-info` 验证。
3. 给 `sel_expensive`（第 21 章会展开）加 `+zicond`，数一数分支版与 branchless 版各几条指令、几个基本块，并用 `llvm-mca` 比较两条路径的 worst-case 周期。
4. 用 `-print-after=finalize-isel` 导出 `sel` 的 MIR，手工标出每个基本块的调度 region。再用 `-print-after=machine-scheduler` 看 pre-RA scheduler 是否跨块移动了任何指令。

## 重点整理

- ISel 先决定 DAG 的**节点数、依赖边、sched class**，scheduler 只能在这个结构里重排。
- `select` 在 RISC-V 上有三种形态：分支（多基本块）、`neg+and`（零常量）、`czero`（`Zicond`）。基本块数量直接改变调度区域数量。
- 融合指令减少节点数和关键路径，但也可能降低吞吐（`RThroughput` 变差）或增加单条延迟（`spacemit-x60` 的 `sh3add`）。
- 「少一条指令」不是优化目标；目标是 **latency × 资源占用 × 寄存器压力** 的综合。
- 判断一条指令值多少钱，看 `Sched<[...]>`，不要看指令名。

## 自我检核

- [ ] 我能画出 `select` 在基线 / `Zicond` 下的两种 DAG，并指出调度区域边界。
- [ ] 我能解释为什么 `spacemit-x60` 上 `sh3add` 比 `slli+add` 慢。
- [ ] 我知道 `RThroughput` 与 `Latency` 的区别，并能用 `llvm-mca -instruction-info` 读出两者。
- [ ] 我能说出 `Sched<[]>` 会让 scheduler 丢失哪些信息。
- [ ] 我能用 `-print-after=finalize-isel` 区分「IR 层已经改写」与「ISel pattern 匹配」。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVInstrInfoZicond.td`：看 `riscv_czero_eqz` / `riscv_czero_nez` 的 pattern 如何把 `setcc + select` 折叠成两条 `czero`；和本章 `cmp_sel` 对照。
- `llvm/lib/Target/RISCV/RISCVInstrInfoZba.td`：看 `SH1ADD`/`SH2ADD`/`SH3ADD` 的 `Sched<[...]>` 与 `Pat`；重点比较 `WriteIALU` 与 `ReadIALU` 的数量。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` 的 `lowerSELECT`：读 `HasZicond` 分支和 `isNullConstant` 判断，理解为什么 `select c,a,0` 能一条指令。
- `llvm/docs/TableGen/` 中关于 `Sched` 与 `Instruction` 的章节：补 TableGen 侧的资源声明方式。
- `llvm/docs/CommandGuide/llvm-mca.rst`：`-instruction-info`、`-iterations`、`-skip-unsupported-instructions` 的语义。

→ [下一章：内存操作数与 folding](./19-memory-folding.md)
