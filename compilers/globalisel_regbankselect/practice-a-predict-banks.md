# 练习 A — 手写 MIR，预测 bank

> **目标**：不看输出，先为一个包含 `i64`、`f32`、`<4 x i32>` 和 `G_PHI` 的手写 MIR 填出每个 vreg 的 bank；再用 `-run-pass=regbankselect` 和 `-stop-after=regbankselect` 验证。练的是“从指令和类型推断 bank”，不是背一份答案。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（`llc` 实测）。所有 MIR 输出都来自该包；需要改源码或跑 lit 的部分标 guided。
>
> **证据约定**：`实测` 表示在官方 23.1.4 包上实际运行并读取输出；`源码依据` 表示结论来自 23.1.4 源码；`guided` 表示需要自建或未执行，文中会说明缺哪一步。

## 背景与动机

RegBankSelect 的输出很容易“看起来对”：只要最终汇编能跑，很少有人回头检查某个 vreg 到底在 `gpr` 还是 `fpr`。但 bank 选错不会总是让程序崩溃，它更常见的表现是：

- 多出几条跨 bank `COPY`，寄存器压力和指令数上升；
- `G_PHI` 的两条入边落在不同 bank，后续 phi elimination 或寄存器分配替你补 `COPY`；
- 一条本该在 FPR 的链被拆成 FPR→GPR→FPR，性能悄悄变差；
- 你以为 `G_ICMP` 的结果在 CC bank，实际在本路径下是 `i32`/GPR，预测从此全错。

这些问题的共同点是：**bank 由指令语义、操作数类型和 target mapping 共同决定，不由 LLT 单独决定**。同一个 `s32`，既可能是 FPR 里的浮点位模式，也可能是 GPR 里的整数；同一个 `s64`，既可能是普通整数，也可能是指针。只看 `%0:_(s64)` 猜 bank，和抛硬币差不多。

练习 A 把变量压到最少：没有 alternative mapping 的干扰，没有 `optnone`，没有调用和内存。你只需要回答一个问题：

> 给定一条 legalized gMIR 指令，它的每个 vreg 应该落在哪个 bank？

如果你能稳定答对，后面的 Fast/Greedy、repair、PHI、call、memory 才有讨论基础；如果答不对，先回到 Ch 3–Ch 4 把“寄存器、寄存器类、bank、LLT”四件事分开。

## 先建立直觉

把 bank 想成机场航站楼，把这次练习想成一张“旅客分楼表”：

```text
                 ┌──────────────────────────────┐
                 │        legal gMIR            │
                 │  %0(s64)  %2(<4 x s32>)      │
                 │  %3(s32)  %4(s32)  ...       │
                 └──────────────┬───────────────┘
                                │ 你先填
                                ▼
                 ┌──────────────────────────────┐
                 │  GPR 楼：整数、指针、条件      │
                 │  FPR 楼：浮点、向量、大位宽    │
                 │  CC  楼：NZCV（本练习不出现）  │
                 └──────────────┬───────────────┘
                                │ RegBankSelect
                                ▼
                 ┌──────────────────────────────┐
                 │  %0:gpr(s64)  %2:fpr(...)     │
                 │  %3:fpr(s32)  %4:gpr(s32)     │
                 └──────────────────────────────┘
```

先记住三条粗糙但有效的规则，后面再用源码修正：

1. **标量整数/指针/条件** → GPR。
2. **标量浮点指令的操作数和结果** → FPR。
3. **向量** → FPR（AArch64 的 FPR bank 覆盖 QQQQ/ZPR，见 `AArch64RegisterBanks.td`）。

`G_PHI` 是特例：它是 copy-like 指令，bank 不是由“PHI 自己”决定的，而是从已经带 bank 的入边继承；所以两条入边必须一致，否则就会留下跨 bank 的隐患。练习 D 会专门拆这个坑。

## 任务规格

### 输入

下面是一份**完整可跑**的手写 MIR。把它保存为 `practice-a/mix.mir`（UTF-8 无 BOM）。它声明 `legalized: true`、`regBankSelected: false`，所以可以直接用 `-run-pass=regbankselect`。

```yaml
--- |
  target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
  target triple = "aarch64-unknown-linux-gnu"
  define i64 @mix(i64 %a, i64 %b, <4 x i32> %v, float %f, i1 %c) {
  entry:
    br i1 %c, label %then, label %else
  then:
    %t = add i64 %a, %a
    %fm = fmul float %f, %f
    br label %merge
  else:
    br label %merge
  merge:
    %p = phi i64 [ %t, %then ], [ %a, %else ]
    %q = phi float [ %fm, %then ], [ %f, %else ]
    %r = add i64 %p, %b
    ret i64 %r
  }
...

---
name:            mix
alignment:       4
isSSA:           true
legalized:       true
regBankSelected: false
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: _ }
  - { id: 1, class: _ }
  - { id: 2, class: _ }
  - { id: 3, class: _ }
  - { id: 4, class: _ }
  - { id: 5, class: _ }
  - { id: 6, class: _ }
  - { id: 7, class: _ }
  - { id: 8, class: _ }
  - { id: 9, class: _ }
  - { id: 10, class: _ }
  - { id: 11, class: _ }
  - { id: 12, class: _ }
  - { id: 13, class: _ }
liveins:
  - { reg: '$x0', virtual-reg: '' }
  - { reg: '$x1', virtual-reg: '' }
  - { reg: '$q2', virtual-reg: '' }
  - { reg: '$s0', virtual-reg: '' }
  - { reg: '$w3', virtual-reg: '' }
body:             |
  bb.0.entry:
    successors: %bb.1(0x40000000), %bb.2(0x40000000)
    liveins: $x0, $x1, $q2, $s0, $w3

    %0(s64) = COPY $x0
    %1(s64) = COPY $x1
    %2(<4 x s32>) = COPY $q2
    %3(s32) = COPY $s0
    %4(s32) = COPY $w3
    %5(s64) = G_ADD %0, %1
    %6(<4 x s32>) = G_ADD %2, %2
    %7(s32) = G_FADD %3, %3
    %8(s32) = G_ICMP intpred(slt), %5(s64), %1
    G_BRCOND %8(s32), %bb.1
    G_BR %bb.2

  bb.1.then:
    successors: %bb.3(0x80000000)

    %9(s64) = G_ADD %5, %1
    %10(s32) = G_FMUL %7, %3
    G_BR %bb.3

  bb.2.else:
    successors: %bb.3(0x80000000)

    G_BR %bb.3

  bb.3.merge:
    %11(s64) = G_PHI %9(s64), %bb.1, %0(s64), %bb.2
    %12(s32) = G_PHI %10(s32), %bb.1, %3(s32), %bb.2
    %13(s64) = G_ADD %11, %1
    $x0 = COPY %13(s64)
    RET_ReallyLR implicit $x0
...
```

这份 fixture 有三个刻意设计：

- `%2` 和 `%6` 是向量，用来逼你区分“向量 → FPR”与“标量整数 → GPR”。
- `%12` 是一个没有用户的 `f32` PHI。手写 MIR 不跑 DCE，所以它保留下来，用来多测一个 PHI 预测点；如果你从 IR 生成同样的函数，`%q` 会被 DCE 掉。
- 物理 liveins 用 `$q2/$s0/$w3` 是为了让三类 bank 在同一份 fixture 里出现；真实 IR 路径的 ABI 是 `$q0/$s1/$w2`（见下），bank 结论不受影响。

### 输出

你需要交出三样东西：

1. 一张 **14 行**的 vreg/bank 预测表，覆盖 `id: 0` 到 `id: 13`。
2. 一条实际命令和它的 MIR 输出（至少包含 `registers:` 与 `body:`）。
3. 一段解释：为什么某些 `s32` 在 `gpr`，另一些在 `fpr`；为什么这份 fixture 没有跨 bank repair `COPY`。

### 约束

- 先填表，再运行；不要在运行后把表改成“我就知道”。
- 只跑 RegBankSelect，不要跑完整 ISel；本练习不关心最终指令选择。
- 不要加 `-simplify-mir`；它会把 `registers:`、`liveins:` 省掉，bank 更难读。
- 如果输出里出现 `class: _` 或 `%n:_(...)`，先确认 pass 是否真的跑了，而不是继续猜。

### 验收标准

- 14 个 vreg 的 bank 全部正确。
- 能指出 5 条入参 ABI 边界 `COPY`（来自 `$x0`、`$x1`、`$q2`、`$s0`、`$w3`）和 1 条返回 ABI `COPY`，并说明它们都不是 repair。
- 能解释 `%3` 与 `%4` 都是 `s32`，为什么一个在 FPR、一个在 GPR。
- 能解释 `%11`/`%12` 两条 PHI 的 bank 分别由哪条入边决定，以及为什么没有产生 repair。
- `-verify-machineinstrs` 不报错。

## 期望输出

先不要看下面的表。你自己填完之后，再对照。

| vreg | LLT | 定义指令 | 预测 bank | 依据 |
| --- | --- | --- | --- | --- |
| `%0` | `s64` | `COPY $x0` | `gpr` | 整数参数，ABI 用 `$x0` |
| `%1` | `s64` | `COPY $x1` | `gpr` | 整数参数 |
| `%2` | `<4 x s32>` | `COPY $q2` | `fpr` | 向量，FPR bank 覆盖 QQQQ |
| `%3` | `s32` | `COPY $s0` | `fpr` | 浮点参数，后续被 `G_FADD`/`G_FMUL` 使用 |
| `%4` | `s32` | `COPY $w3` | `gpr` | `i1` 条件被提升为 `i32`，走 GPR |
| `%5` | `s64` | `G_ADD` | `gpr` | 标量整数加法 |
| `%6` | `<4 x s32>` | `G_ADD` | `fpr` | 向量加法，AArch64 放 FPR |
| `%7` | `s32` | `G_FADD` | `fpr` | 浮点 opcode，操作数和结果都在 FPR |
| `%8` | `s32` | `G_ICMP` | `gpr` | 本路径下 `i32` 布尔结果，不是 CC |
| `%9` | `s64` | `G_ADD` | `gpr` | 整数加法 |
| `%10` | `s32` | `G_FMUL` | `fpr` | 浮点乘法 |
| `%11` | `s64` | `G_PHI` | `gpr` | 两条入边 `%9`/`%0` 都是 GPR |
| `%12` | `s32` | `G_PHI` | `fpr` | 两条入边 `%10`/`%3` 都是 FPR |
| `%13` | `s64` | `G_ADD` | `gpr` | 整数加法 |

把这份表压缩成一句话：

> **看生产者/消费者语义，不要只看 LLT。** `%3` 和 `%4` 的 LLT 都是 `s32`，但一个被浮点指令消费，一个被条件分支消费；bank 由使用方式决定。

### 预测表模板

练习时建议用下面这张空表，而不是直接抄上面的答案。第三列写“定义”，第四列写“用户”，最后一列写“我为什么这么判”。如果你只能填出 bank、写不出理由，说明你还在猜。

| vreg | LLT | 定义 | 主要用户 | 预测 bank | 理由 |
| --- | --- | --- | --- | --- | --- |
| `%0` |  |  |  |  |  |
| `%1` |  |  |  |  |  |
| `%2` |  |  |  |  |  |
| `%3` |  |  |  |  |  |
| `%4` |  |  |  |  |  |
| `%5` |  |  |  |  |  |
| `%6` |  |  |  |  |  |
| `%7` |  |  |  |  |  |
| `%8` |  |  |  |  |  |
| `%9` |  |  |  |  |  |
| `%10` |  |  |  |  |  |
| `%11` |  |  |  |  |  |
| `%12` |  |  |  |  |  |
| `%13` |  |  |  |  |  |

### 逐指令推导

把“定义”和“用户”两列填完后，真正的推导只有三类：

| 指令 | 操作数约束 | 结果约束 | 推导结论 |
| --- | --- | --- | --- |
| `COPY $x0/$x1` | 源是 GPR 物理寄存器 | 目标继承源 bank | `%0`/`%1` = GPR |
| `COPY $q2` | 源是 128 位向量寄存器 | 目标继承源 bank | `%2` = FPR |
| `COPY $s0` | 源是浮点/向量物理寄存器 | 目标继承源 bank | `%3` = FPR |
| `COPY $w3` | 源是 32 位 GPR | 目标继承源 bank | `%4` = GPR |
| `G_ADD s64` | 标量整数 | 同 bank | `%5` = GPR |
| `G_ADD <4 x s32>` | 向量 | 同 bank | `%6` = FPR |
| `G_FADD s32` | 浮点 opcode | 同 bank | `%7` = FPR |
| `G_ICMP` | 输入 GPR | 结果 `s32`，本路径 GPR | `%8` = GPR |
| `G_ADD s64` | 标量整数 | 同 bank | `%9` = GPR |
| `G_FMUL s32` | 浮点 opcode | 同 bank | `%10` = FPR |
| `G_PHI s64` | 入边 `%9`/`%0` | 继承已 bank 入边 | `%11` = GPR |
| `G_PHI s32` | 入边 `%10`/`%3` | 继承已 bank 入边 | `%12` = FPR |
| `G_ADD s64` | 标量整数 | 同 bank | `%13` = GPR |

注意这张表里没有“LLT 单独决定 bank”这一行。LLT 只告诉你有多少位、是不是向量、是不是指针；它不告诉你这 32 位是整数还是浮点。`s32` 的 `%3` 和 `%4` 就是反例。

## 如果你卡住了

1. **把 `G_ICMP` 当成 CC bank**：在 23.1.4 的这条 AArch64 GlobalISel 路径下，`G_ICMP` 的结果被合法化成 `i32`，RegBankSelect 把它放 GPR；CC bank 主要服务 `NZCV` 和 `G_FCMP` 的某些路径，不是默认布尔 bank。看 Ch 4 的“CC 不是默认布尔 bank”一节。
2. **把 `%3` 判断成 GPR**：`s32` 只说明 32 位，不说明整数还是浮点。看它的定义：`COPY $s0` 来自浮点参数寄存器；看它的用户：`G_FADD`、`G_FMUL`。这两条都把它拉向 FPR。
3. **把向量 `G_ADD` 判断成 GPR**：AArch64 的标量整数和向量走不同寄存器文件；`<4 x s32>` 是向量，默认映射到 FPR（`AArch64RegisterBanks.td` 的 `FPRRegBank` 覆盖 `QQQQ`、`ZPR`）。
4. **不知道 PHI 怎么判**：`G_PHI` 是 copy-like。先看两条入边已经落在哪个 bank；如果一致，PHI 跟着走；如果不一致，RegBankSelect 不会为 PHI 的每个入边做局部 repair（练习 D 会解释源码原因）。本 fixture 的两条 PHI 都是同 bank 入边，所以没有这个问题。
5. **分不清 `-run-pass` 和 `-stop-after`**：手写 MIR 已经是 legalized 状态，用 `-run-pass=regbankselect`；从 `.ll` 开始跑整条 pipeline 才用 `-stop-after=regbankselect`。两者混用会得到“pass 没跑”或“属性不满足”的假象。

## 实作步骤建议

1. **先抄 fixture，不运行**：把上面的 MIR 写到 `practice-a/mix.mir`。确认文件是 UTF-8 无 BOM；PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，`llc` 可能报 `expected top-level entity`。
2. **手填表**：在纸上或编辑器里写 14 行 `vreg -> bank`，每行写一句依据。不要只写 bank；写不出依据的，标记为“不确定”，运行后重点看。
3. **跑 hand-written MIR**：
   ```powershell
   $llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
   & $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
     -verify-machineinstrs practice-a\mix.mir -o practice-a\mix-rbs.mir
   ```
   先看 `registers:`，再看 `body:`。不要只看汇编。
4. **逐行 diff**：把实际 bank 和你填的表对齐。每错一个，问自己：我看错了定义，还是看错了用户？如果两者都没错，再去看 target mapping。
5. **换一条 IR 路径复核**：把同一函数写成 `.ll`，并给 `%q`、`%v` 加真实用户（否则 DCE 会删掉），用 `-stop-after=regbankselect` 再跑一次。bank 规则应该一致，但 vreg 编号和 repair `COPY` 可能不同。

## 完整参考解答

<details>
<summary>展开：完整命令、实际 MIR 输出与逐组解释</summary>

### 命令

```powershell
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -verify-machineinstrs practice-a\mix.mir -o practice-a\mix-rbs.mir
```

在 23.1.4 官方包上，退出码 0，输出包含以下内容（实测）：

```yaml
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 3, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 4, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 5, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 6, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 7, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 8, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 9, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 10, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 11, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 12, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 13, class: gpr, preferred-register: '', flags: [  ] }
```

```yaml
body:             |
  bb.0.entry:
    successors: %bb.1(0x40000000), %bb.2(0x40000000)
    liveins: $x0, $x1, $q2, $s0, $w3

    %0:gpr(s64) = COPY $x0
    %1:gpr(s64) = COPY $x1
    %2:fpr(<4 x s32>) = COPY $q2
    %3:fpr(s32) = COPY $s0
    %4:gpr(s32) = COPY $w3
    %5:gpr(s64) = G_ADD %0, %1
    %6:fpr(<4 x s32>) = G_ADD %2, %2
    %7:fpr(s32) = G_FADD %3, %3
    %8:gpr(s32) = G_ICMP intpred(slt), %5(s64), %1
    G_BRCOND %8(s32), %bb.1
    G_BR %bb.2

  bb.1.then:
    successors: %bb.3(0x80000000)

    %9:gpr(s64) = G_ADD %5, %1
    %10:fpr(s32) = G_FMUL %7, %3
    G_BR %bb.3

  bb.2.else:
    successors: %bb.3(0x80000000)

    G_BR %bb.3

  bb.3.merge:
    %11:gpr(s64) = G_PHI %9(s64), %bb.1, %0(s64), %bb.2
    %12:fpr(s32) = G_PHI %10(s32), %bb.1, %3(s32), %bb.2
    %13:gpr(s64) = G_ADD %11, %1
    $x0 = COPY %13(s64)
    RET_ReallyLR implicit $x0
...
```

### 逐组解释

**第一组：参数 COPY（`%0`–`%4`）**

这 5 条都是 ABI 边界，不是 repair：

- `$x0`/`$x1` 是整数参数寄存器，所以 `%0`/`%1` 是 GPR。
- `$q2` 是 128 位向量寄存器，所以 `%2` 是 FPR。
- `$s0` 是 32 位浮点/向量寄存器，所以 `%3` 是 FPR。
- `$w3` 是 32 位 GPR，承载提升后的 `i1`，所以 `%4` 是 GPR。

判断 ABI COPY 的实用方法：一边是物理寄存器（`$x0`、`$s0`），另一边是 vreg；它通常来自调用约定，不是 RegBankSelect 为了修复 bank 插入的。真正的 repair COPY 通常两边都是 vreg，且 bank 不同，例如 `%6:gpr = COPY %2:fpr`。

**第二组：整数与向量加法（`%5`、`%6`）**

`%5` 是 `G_ADD s64`。AArch64 的 `getSameKindOfOperandsMapping` 对非向量、非浮点 opcode 选 `PMI_FirstGPR`（源码依据：`AArch64RegisterBankInfo.cpp` 的 `getSameKindOfOperandsMapping`），所以三个操作数都在 GPR。

`%6` 是 `G_ADD <4 x s32>`。同一个 `G_ADD`，因为类型是向量，`IsFPR = Ty.isVector()` 为真，映射到 FPR。这就是“同一个 opcode，不同类型，不同 bank”的最小例子。

**第三组：浮点运算（`%7`、`%10`）**

`G_FADD`/`G_FMUL` 是 pre-isel 浮点 opcode，`getSameKindOfOperandsMapping` 里 `isPreISelGenericFloatingPointOpcode(Opc)` 为真，选 `PMI_FirstFPR`。因此 `%7`、`%10` 以及它们的操作数 `%3` 都在 FPR。

**第四组：比较与分支（`%8`）**

`%8` 是 `G_ICMP`，结果是 `s32`。在本路径下，它被映射到 GPR，不是 CC。`G_BRCOND` 的操作数也是 GPR。不要因为“比较产生条件”就把它放进 CC；CC bank 覆盖的是 `NZCV`/`CCR`，不是默认的 `i1` 值。

**第五组：PHI（`%11`、`%12`）**

- `%11` 的两条入边是 `%9:gpr` 和 `%0:gpr`，所以 PHI 是 GPR。
- `%12` 的两条入边是 `%10:fpr` 和 `%3:fpr`，所以 PHI 是 FPR。

`G_PHI` 是 copy-like 指令；`getInstrMappingImpl` 只映射定义（`NumOperandsForMapping = 1`），bank 来自第一个已经带 bank 的入边。两条入边一致时，没有 repair；不一致时，RegBankSelect 不会像普通指令那样逐个操作数检查 PHI 入边，这就是练习 D 的入口。

### 三个常见错误预测

错误不是“运气不好”，通常来自同一个偷懒：把 LLT 当成语义。

| 错误预测 | 实际 | 错在哪 | 修正方法 |
| --- | --- | --- | --- |
| `%3` 是 `s32`，所以 GPR | `%3:fpr(s32)` | `s32` 只说明 32 位；它来自 `$s0`，被 `G_FADD`/`G_FMUL` 使用 | 先看定义和用户，再看 LLT |
| `%8` 是 `G_ICMP`，所以 CC | `%8:gpr(s32)` | 本路径下布尔结果被提升成 `i32`，不是 `NZCV` | 看 Ch 4 的 CC bank 边界 |
| `%12` 是 `G_PHI`，所以跟着 `%10` 走 FPR | 正确，但理由是入边 `%3` 也在 FPR | 只看了第一条入边，没检查第二条 | PHI 要检查所有入边是否同 bank |

再加一个容易忽略的：

| 错误预测 | 实际 | 错在哪 | 修正方法 |
| --- | --- | --- | --- |
| `%6` 是 `G_ADD`，所以 GPR | `%6:fpr(<4 x s32>)` | 只看 opcode，没看类型是向量 | 向量默认走 FPR |

这些错误在后面的练习里会反复出现。把“定义、用户、类型”三件事分开看，比背一张 bank 表有用。

### 为什么没有跨 bank repair COPY

这份 fixture 里每一条指令的操作数和结果都在同一个 bank：

- `G_ADD s64` 的输入 `%0`/`%1` 是 GPR，输出 `%5` 也是 GPR；
- `G_ADD <4 x s32>` 的输入 `%2` 是 FPR，输出 `%6` 也是 FPR；
- `G_FADD`/`G_FMUL` 的输入输出都在 FPR；
- `G_ICMP` 的输入是 GPR，输出 `%8` 是 GPR；
- 两条 PHI 的入边与定义同 bank；
- `G_BRCOND` 的操作数 `%8` 是 GPR。

所以跨 bank 修复 COPY 数为 **0**。你看到的 6 条 `COPY` 全是 ABI 边界：5 条入参 + 1 条返回。

### 再用 IR 路径复核一次（实测）

把同一函数写成 `.ll`，并给死值加用户，避免 DCE：

```llvm
define i64 @mix(i64 %a, i64 %b, <4 x i32> %v, float %f, i1 %c) {
entry:
  br i1 %c, label %then, label %else
then:
  %t = add i64 %a, %a
  %fm = fmul float %f, %f
  br label %merge
else:
  br label %merge
merge:
  %p = phi i64 [ %t, %then ], [ %a, %else ]
  %q = phi float [ %fm, %then ], [ %f, %else ]
  %s = fptosi float %q to i64
  %e = extractelement <4 x i32> %v, i32 0
  %ei = sext i32 %e to i64
  %r = add i64 %p, %b
  %r2 = add i64 %r, %s
  %r3 = add i64 %r2, %ei
  ret i64 %r3
}
```

```powershell
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -global-isel-abort=1 -stop-after=regbankselect `
  practice-a\mix.ll -o practice-a\mix-ir-rbs.mir
```

关键片段（实测）：

```yaml
%0:gpr(i64) = COPY $x0
%1:gpr(i64) = COPY $x1
%2:fpr(<4 x i32>) = COPY $q0
%3:fpr(f32) = COPY $s1
%6:gpr(i32) = COPY $w2
%8:gpr(i64) = G_ADD %0, %0
%9:fpr(f32) = G_FMUL %3, %3
%10:gpr(i64) = G_PHI %8(i64), %bb.2, %0(i64), %bb.3
%11:fpr(f32) = G_PHI %9(f32), %bb.2, %3(f32), %bb.3
%12:gpr(i64) = G_FPTOSI %11(f32)
%13:fpr(i32) = G_EXTRACT_VECTOR_ELT %2(<4 x i32>), %19(i64)
%23:gpr(i32) = COPY %13(i32)
%15:gpr(i64) = G_SEXT %23(i32)
```

这里多了一条 `%23:gpr = COPY %13:fpr`。它不是 PHI 的问题，而是 `G_EXTRACT_VECTOR_ELT` 从 FPR 向量里取出标量、后续 `G_SEXT` 要在 GPR 里处理，RegBankSelect 为这次跨 bank 使用插入了 repair。它说明：**bank 规则一致，不代表 repair COPY 数量一致；repair 取决于每条指令的操作数银行约束。**

</details>

## MIR 语法补充

- `%0(s64)` 是“手写整数标量”的打印形式；从 IR 生成的 MIR 常见 `%0(i64)`。两者都表示 64 位标量，`llc` 都能解析。
- `%3:fpr(f32)` 里的 `f32` 是浮点标量的打印形式；手写 MIR 里你写 `%3(s32)` 也能被接受，因为 LLT 本身不区分整数/浮点，语义由 opcode 决定。
- `class: _` 表示该 vreg 还没有 bank；`regBankSelected: true` 之后，`registers:` 里才会出现 `gpr`/`fpr`。
- 顶层的 `liveins:` 和 `bb.0.entry` 里的 `liveins:` 是两份信息；前者是 MachineFunction 的属性，后者是基本块的属性。手写 fixture 时两份都写，输出更接近真实 pipeline。
- `G_PHI` 的入边写成 `%9(s64), %bb.1`；第二个操作数是前驱 MBB，不是 vreg。

## 测试用例

| 检查点 | 观察位置 | 期望 | 说明 |
| --- | --- | --- | --- |
| `i64 G_ADD` | `%5:gpr(s64)` | GPR | 标量整数 |
| 向量 `G_ADD` | `%6:fpr(<4 x s32>)` | FPR | 向量走 FPR |
| `G_FADD` | `%7:fpr(s32)` | FPR | 浮点 opcode |
| `G_ICMP` | `%8:gpr(s32)` | GPR | 本路径下 `i32` 布尔，不是 CC |
| `G_PHI i64` | `%11:gpr(s64)` | GPR | 两条入边 GPR |
| `G_PHI f32` | `%12:fpr(s32)` | FPR | 两条入边 FPR |
| `G_BRCOND` | `G_BRCOND %8(s32)` | 操作数 GPR | 条件在 GPR |
| repair COPY 数 | 两条 vreg 之间的跨 bank COPY | 0 | 本 fixture 无跨 bank 使用 |
| ABI COPY 数 | 物理寄存器 ↔ vreg | 6（5 入参 + 1 返回） | `$x0/$x1/$q2/$s0/$w3`，加返回 `$x0` |
| verifier | `-verify-machineinstrs` | 退出码 0 | 没有 bank/类型不匹配 |

## 延伸挑战

1. **把 `%6` 的向量 `G_ADD` 换成 `G_FADD <4 x f32>`**：先预测 bank 是否仍然全是 FPR，再跑一次。注意 `G_FADD` 的向量形式和标量形式在 `getSameKindOfOperandsMapping` 里都走 FPR，但 legalizer 的合法性检查不同。
2. **加入 `G_FCMP`**：预测结果 bank。提示：在本路径下标量结果仍被提升为 `i32`/GPR；不要因为它是浮点比较就把结果放进 FPR 或 CC。把实际输出和 Ch 4 的说明对照。
3. **把 `%8` 改成 `G_ICMP` 后接 `G_SELECT`**：预测 `G_SELECT` 的条件、真值、假值分别在哪个 bank。AArch64 的 `getInstrMapping` 对 `G_SELECT` 有专门的“FP 使用多还是 GPR 使用多”的启发式，先读源码再跑。
4. **把 `i64` 换成 `i128`**：预测 bank 和 partial mapping。AArch64 GPR bank 覆盖到 128 位（`getMaximumSize(GPR) == 128`），但这不代表 `i128` 的每个操作都直接落在单个物理寄存器；看 `XSeqPairsClass` 和 `GPR128` partial mapping。
5. **换 RISC-V 对照（guided）**：同一份 MIR 不能直接换 triple，因为物理 liveins 和 bank 名不同（RISC-V 是 `GPRB`/`FPRB`/`VRB`）。把 fixture 的 `COPY` 源改成 RISC-V 的 `$a0`/`$fa0` 等，再预测；本练习未在 RISC-V 上执行，标 guided。

## 延伸阅读

- **[LLVM MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：Machine Function 的 YAML 字段、`registers`、`body`、`-run-pass`。
  - **学什么**：`class: _`、`%0:gpr(s64)`、`liveins` 的语法。
  - **和本练习的关联**：不会读 MIR，就无法把预测表和实际输出对齐。
- **[AArch64RegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterBanks.td)**
  - **读哪里**：`GPRRegBank`、`FPRRegBank`、`CCRegBank` 的 coverage。
  - **学什么**：为什么向量和大位宽值可以进 FPR，为什么 `s32` 既可能在 GPR 也可能在 FPR。
  - **和本练习的关联**：`%3` 与 `%4` 的差异最终由 bank coverage 和 opcode 语义共同决定。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`getSameKindOfOperandsMapping`、`getInstrMapping` 的 `G_ICMP`/`G_SELECT`/`G_LOAD` 分支。
  - **学什么**：标量整数、向量、浮点三类 opcode 的默认 bank 选择。
  - **和本练习的关联**：本练习的 14 个 bank 都能在这段源码里找到依据。

## 自我检核

- [ ] 我能不看表，解释 `%3` 和 `%4` 为什么同为 `s32` 却在不同 bank
- [ ] 我能说出向量 `G_ADD` 与标量 `G_ADD` 的 bank 差异，以及源码里的判断条件
- [ ] 我能区分 ABI 边界 COPY 与 RegBankSelect 的 repair COPY
- [ ] 我能解释 `G_PHI` 的 bank 从哪里继承，以及两条入边不一致时会发生什么
- [ ] 我能用 `-run-pass` 和 `-stop-after` 各跑一次，并说出两者输入状态的差别
- [ ] 我知道本练习为什么没有跨 bank repair COPY，而 IR 路径多了一条
- [ ] 我能解释 `%0(s64)` 与 `%0(i64)` 只是打印差异，不是两个不同的 LLT
- [ ] 我能指出 `class: _` 与 `class: gpr` 分别代表 RegBankSelect 之前和之后的状态
- [ ] 我能说出 `G_PHI` 的第二操作数是前驱 MBB，而不是另一个 vreg

练习 A 到此结束。下一站进入 Part 1：先建立 `RegisterBankInfo` 的数据模型，再拆默认 mapping 与 target mapping 表。

→ [Ch 5 RegisterBankInfo 数据模型](./05-registerbankinfo-data-model.md)