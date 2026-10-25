# 练习 A：手写 MIR，手动跑一次 InstructionSelect

> **目标**：不经过 Clang、不经过 IRTranslator，直接手写一份"已经合法、已经分好 bank"的 MIR，观察 InstructionSelect 如何把它变成目标指令。
>
> **产出**：6 份可复现的 `.mir` + 一份"预测 vs 实际"对照记录。
> **时间**：2–4 小时。**环境**：第 0 章的 `llc`。

---

## A.0 准备

建立工作目录：

```console
mkdir isel-lab && cd isel-lab
```

三条通用命令，后面反复使用：

```console
:: 1. 只跑选择阶段
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select in.mir -o out.mir

:: 2. 失败时立刻报错，而不是静默 fallback
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 in.mir -o out.mir

:: 3. 打开机器验证器
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -verify-machineinstrs in.mir -o out.mir
```

判断成功的唯一标准（第 2 章）：

```yaml
selected:   true
failedISel: false
```

只看 `selected` 会误判。

---

## A.1 先解剖一份真实 MIR

不要从零开始。先生成一份，读懂它的每个字段：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=regbankselect add.ll -o add-rbs.mir
```

`add.ll`：

```llvm
define i64 @add(i64 %a, i64 %b) {
entry:
  %r = add i64 %a, %b
  ret i64 %r
}
```

打开 `add-rbs.mir`，你会看到三个文档：

1. `--- |` 开头的是 LLVM IR（MIR 文件内嵌原始 IR，用于调试信息和函数签名）；
2. `...` 结束 IR；
3. `---` 之后是**函数级 YAML**，`body: |` 里是机器指令。

必须认识的字段：

| 字段 | 作用 | 缺失/写错的后果 |
| --- | --- | --- |
| `name` | 函数名，必须与内嵌 IR 中的定义一致 | 解析报 `isn't defined in the provided LLVM IR` |
| `isSSA` | 虚拟寄存器单定义 | 属性检查（assertions 构建）失败 |
| `legalized` | 已合法化 | 同上 |
| `regBankSelected` | 已分 bank | 同上 |
| `registers` | 每个 vreg 的 bank/class | 解析报 `Cannot determine class/bank` |
| `liveins` | 物理寄存器入口 | 验证器报 live-in 不一致 |
| `body` | 基本块与指令 | — |

一个最小的函数文档长这样：

```yaml
---
name:            add
alignment:       4
isSSA:           true
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
liveins:
  - { reg: '$x0', virtual-reg: '' }
  - { reg: '$x1', virtual-reg: '' }
body:             |
  bb.0.entry:
    liveins: $x0, $x1

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
...
```

### 任务 A.1

把上面这份 MIR 存成 `add-hand.mir`（内嵌 IR 用你之前的 `add.ll`），跑：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select add-hand.mir -o add-hand-out.mir
```

**先预测**：`G_ADD` 会变成什么？`%2:gpr(i64)` 会变成什么？

然后对照输出，写下差异。参考答案在 A.8。

---

## A.2 任务：常量折叠

### 目标

理解"选择阶段可以把常量折进立即数，并把死掉的 `G_CONSTANT` 删掉"。

### 输入

```yaml
body:             |
  bb.0.entry:
    liveins: $x0

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = G_CONSTANT i64 1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```

（`registers` 需要 0、1、2 三个 `gpr`。）

### 预测

1. `G_ADD` 会变成 `ADDXrr` 还是 `ADDXri`？
2. `G_CONSTANT` 会保留还是消失？
3. vreg 编号会出现空洞吗？

### 验证

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select const.mir -o const-out.mir
```

对照 A.8 的答案。记录你错在哪一条、为什么。

---

## A.3 任务：一条 generic 变两条目标指令

### 目标

看到 `G_SELECT` 如何依赖隐式的条件码寄存器。

### 输入

```yaml
name:            sel
alignment:       4
isSSA:           true
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
  - { id: 3, class: gpr }
liveins:
  - { reg: '$x0', virtual-reg: '' }
  - { reg: '$x1', virtual-reg: '' }
  - { reg: '$w2', virtual-reg: '' }
body:             |
  bb.0.entry:
    liveins: $x0, $x1, $w2

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = COPY $x1
    %2:gpr(i32) = COPY $w2
    %3:gpr(i64) = G_SELECT %2(i32), %0, %1
    $x0 = COPY %3(i64)
    RET_ReallyLR implicit $x0
```

内嵌 IR：

```llvm
define i64 @sel(i64 %a, i64 %b, i1 %c) {
entry:
  %r = select i1 %c, i64 %a, i64 %b
  ret i64 %r
}
```

### 预测

1. `G_SELECT` 会变成一条还是多条指令？
2. 条件值 `%2` 需要满足什么条件？会不会有指令专门"测试"它？
3. 会出现对 `$nzcv` 的隐式引用吗？

### 验证

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select sel.mir -o sel-out.mir
```

这是一个"输入里没有 `G_ICMP`、但输出里出现标志位"的例子，值得多想一步：为什么 `G_SELECT` 需要自己测条件？

---

## A.4 任务：循环与 PHI

### 目标

观察 `G_PHI`、`G_ICMP`、`G_BRCOND` 的组合如何变成 `PHI`、`SUBSXrr`、`Bcc`。

### 输入

直接生成，避免手写出错：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=regbankselect phi.ll -o phi-rbs.mir
```

`phi.ll`：

```llvm
define i64 @sum(i64 %n) {
entry:
  br label %loop
loop:
  %i = phi i64 [ 0, %entry ], [ %next, %loop ]
  %acc = phi i64 [ 0, %entry ], [ %add, %loop ]
  %add = add i64 %acc, %i
  %next = add i64 %i, 1
  %cmp = icmp slt i64 %next, %n
  br i1 %cmp, label %loop, label %exit
exit:
  ret i64 %add
}
```

然后手工做两件事（这是本任务的核心）：

1. 把 `phi-rbs.mir` 的 `selected: false` 改成 `true`，观察 `-verify-machineinstrs` 是否报错；
2. 改回 `false`，跑 `-run-pass=instruction-select`。

### 预测

1. 两个 `G_PHI` 会变成什么？操作数顺序会变吗？
2. `G_CONSTANT 0` 会不会变成真正的常量指令？
3. `G_ICMP` + `G_BRCOND` 会合并吗？

---

## A.5 任务：内存操作

### 目标

看 `G_LOAD`/`G_STORE` 如何选成带寻址模式的指令，以及全局变量如何走 GOT。

### 输入

```llvm
@g = global i32 7
define i32 @mem(i32 %x, ptr %p) {
  %a = alloca i32, align 4
  store i32 %x, ptr %a
  %v = load i32, ptr %p
  %s = add i32 %v, 1
  %w = load i32, ptr @g
  %r = add i32 %s, %w
  ret i32 %r
}
```

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=regbankselect mem.ll -o mem-rbs.mir
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    mem-rbs.mir -o mem-out.mir
```

### 预测

1. `G_STORE` 到栈槽会变成哪条指令？地址怎么表示？
2. `G_LOAD` 从 `@g` 取的地址会变成 `ADRP` 还是别的伪指令？
3. `add i32 %s, 1` 的常量会折进哪里？

---

## A.6 任务：破坏实验（三种失败）

### 6a. 错误的 bank

把 `fadd` 的 MIR 中所有 `:fpr(f32)` 改成 `:gpr(f32)`、`class: fpr` 改成 `class: gpr`：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    fadd-wrongbank.mir -o wrong-out.mir
```

观察 `wrong-out.mir` 的头部。**它退出码是 0**，但：

```yaml
selected:   true
failedISel: true
```

说明什么？再用 `-global-isel-abort=1` 跑一次，记录报错文本。

### 6b. 未定义的 vreg

把 `G_ADD %0, %1` 改成 `G_ADD %0, %9`（`%9` 没有定义、也没在 `registers` 里）：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select undef.mir -o NUL
```

记录报错。它发生在解析阶段还是选择阶段？

### 6c. 属性说谎

把一份还没选择的 MIR 的 `selected` 改成 `true`，然后：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=finalize-isel \
    -verify-machineinstrs bad-sel.mir -o NUL
```

记录 `Bad machine code` 的行数，并解释每一条。

---

## A.7 任务：自选一条指令

从 `AArch64InstrGISel.td` 里挑一个 `GI*` 伪指令（例如 `G_BSWAP`、`G_CTLZ`、`G_SDIV`），手写一份 MIR 让它被选中。

示例：`G_BSWAP`

```llvm
define i32 @bswap(i32 %x) {
  %r = call i32 @llvm.bswap.i32(i32 %x)
  ret i32 %r
}
declare i32 @llvm.bswap.i32(i32)
```

```yaml
body:             |
  bb.0.entry:
    liveins: $w0

    %0:gpr(i32) = COPY $w0
    %1:gpr(i32) = G_BSWAP %0(i32)
    $w0 = COPY %1(i32)
    RET_ReallyLR implicit $w0
```

验证后回答：`G_BSWAP` 在 AArch64 上对应哪条真实指令？`.td` 里是哪个 `GINodeEquiv` 把它和 pattern 关联起来的？

---

## A.8 参考答案

### A.1 与 A.2

`add-hand.mir` 的输出：

```yaml
%0:gpr64 = COPY $x0
%1:gpr64 = COPY $x1
%2:gpr64 = ADDXrr %0, %1
$x0 = COPY %2
RET_ReallyLR implicit $x0
```

`const.mir` 的输出：

```yaml
%0:gpr64sp = COPY $x0
%2:gpr64sp = ADDXri %0, 1, 0
$x0 = COPY %2
RET_ReallyLR implicit $x0
```

要点：

- `G_ADD` 选成 `ADDXri`，第三操作数是移位量 `0`；
- `G_CONSTANT` 被折进立即数后删除，`%1` 消失（编号空洞）；
- 寄存器类变成 `gpr64sp`，因为 `ADDXri` 允许 SP 作为基址。

### A.3

```yaml
%0:gpr64 = COPY $x0
%1:gpr64 = COPY $x1
%2:gpr32 = COPY $w2
%4:gpr32 = ANDSWri %2, 0, implicit-def $nzcv
%3:gpr64 = CSELXr %0, %1, 1, implicit $nzcv
$x0 = COPY %3
RET_ReallyLR implicit $x0
```

要点：

- 一条 `G_SELECT` 变两条：`ANDSWri` 测条件（`& 0` 只为了设置标志位），`CSELXr` 按条件选值；
- 条件码通过 `$nzcv` 隐式传递；
- `CSELXr` 的最后操作数 `1` 是条件码编号（`EQ`/`NE` 等由 TableGen 生成）。

### A.4

```yaml
bb.0:
  %0:gpr64 = COPY $x0
  %7:gpr64 = COPY $xzr
  B %bb.1

bb.1.loop:
  %1:gpr64common = PHI %7, %bb.0, %5, %bb.1
  %2:gpr64 = PHI %7, %bb.0, %3, %bb.1
  %3:gpr64 = ADDXrr %2, %1
  %5:gpr64common = ADDXri %1, 1, 0
  %11:gpr64 = SUBSXrr %5, %0, implicit-def $nzcv
  Bcc 11, %bb.1, implicit $nzcv
  B %bb.2
```

要点：

- `G_CONSTANT 0` 选成 `COPY $xzr`（零寄存器，不占真实寄存器）；
- `G_ICMP` + `G_BRCOND` 合成 `SUBSXrr` + `Bcc`，比较结果不再物化成 0/1；
- `Bcc 11` 的 `11` 是 `LT` 的条件码编号。

### A.5

```yaml
%0:gpr32 = COPY $w0
%1:gpr64sp = COPY $x1
STRWui %0, %stack.0.a, 0 :: (store (i32) into %ir.a)
%3:gpr32common = LDRWui %1, 0 :: (load (i32) from %ir.p)
%5:gpr32common = ADDWri %3, 1, 0
%7:gpr64common = LOADgot target-flags(aarch64-got) @g
%6:gpr32 = LDRWui %7, 0 :: (load (i32) from @g)
%8:gpr32 = ADDWrr %5, %6
```

要点：

- 栈槽用 `%stack.0.a` 表示，`STRWui` 直接把帧索引当基址；
- 全局变量通过 `LOADgot` 取 GOT 表项，再用 `LDRWui` 解引用；
- 常量 `1` 折进 `ADDWri`；
- `gpr32common` 是比 `gpr32` 更宽的类，因为 `LDRWui` 的结果可以进任何 32 位寄存器。

### A.6

| 实验 | 现象 | 解释 |
| --- | --- | --- |
| 6a 错误 bank | 退出码 0，`selected: true` + `failedISel: true`，`G_FADD` 仍在 | 选择失败被静默 fallback；`abort=1` 时输出 `cannot select: %2:gpr(f32) = G_FADD ...` |
| 6b 未定义 vreg | 解析期报 `Cannot determine class/bank of virtual register 9` | MIR 解析器要求每个 vreg 在 `registers` 中声明 |
| 6c 属性说谎 | `Unexpected generic instruction in a Selected function` 等 | MachineVerifier 在 `selected: true` 时执行 postcondition 检查 |

### A.7

```yaml
%0:gpr32 = COPY $w0
%1:gpr32 = REVWr %0
$w0 = COPY %1
```

`G_BSWAP` 对应 `REVWr`；关联在 `AArch64InstrGISel.td` 的 `GINodeEquiv<...>` 与 `AArch64InstrInfo.td` 的 pattern 中。第 5 章会展开这个机制。

---

## A.9 验收清单

- [ ] 能独立写出一份可解析的 MIR（含内嵌 IR、函数头、`registers`、`liveins`、`body`）
- [ ] 能预测 `G_ADD` + `G_CONSTANT` 的折叠结果
- [ ] 能解释为什么 `G_SELECT` 会变成两条指令
- [ ] 能解释 `G_PHI` 为什么在输出中仍是 `PHI`
- [ ] 能区分 `selected: true` 与"真的选择成功"
- [ ] 能说出错误 bank、未定义 vreg、属性说谎分别在哪一层被抓住

---

## A.10 常见错误

| 报错 | 原因 | 修复 |
| --- | --- | --- |
| `isn't defined in the provided LLVM IR` | `name` 与内嵌 IR 的函数名不一致 | 改 `name` 或补 IR 定义 |
| `Cannot determine class/bank of virtual register N` | vreg 没在 `registers` 里声明，或操作数写错 | 补声明、检查拼写 |
| `Use of undefined value '%N'` | 用了没定义的 vreg | 补定义指令 |
| `Bad machine code: Using an undefined physical register` | `liveins` 与实际使用的物理寄存器不一致 | 对齐 `liveins` |
| `cannot select: ...` | bank/类型与可用 pattern 不匹配 | 对照 `-stop-after=regbankselect` 的真实输出 |
| 输出仍是 `G_*` 但无报错 | 静默 fallback | 看 `failedISel`，或加 `-global-isel-abort=1` |

---

## 延伸

- 第 5 章：TableGen 如何把 `.td` 的 pattern 变成选择表。
- 第 6 章：匹配表执行器如何工作，为什么 `G_SELECT` 能变成两条指令。
- 第 16 章：如何把这些手写 MIR 固化成 lit 测试。

---

## 下一章

[第 5 章 TableGen 如何把 pattern 导入 GlobalISel](./05-tablegen-globalisel-import.md) —— 手工 MIR 让你看清“输入长什么样”；下一章进入 `.td`，看 `G_ADD` 等 generic opcode 如何被映射成 matcher table，以及哪些 SelectionDAG pattern 根本导不进来。
