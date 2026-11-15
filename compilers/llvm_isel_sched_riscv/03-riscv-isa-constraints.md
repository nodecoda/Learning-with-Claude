# Ch 3 — RISC-V 的 ISA 约束如何塑造选择

> **目标**：把 RISC-V 的 ISA 约束（load/store、无 indexed addressing、12-bit immediate、无 condition code、扩展指令）映射到具体的 selection 结果，再映射到 scheduling 后果。学完这章，你能看着一条 IR 预测「大概会选成几条指令、依赖链多长、scheduler 有多少空间」。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章为了隔离 feature，统一用 `-mcpu=generic`，再用 `-mattr` 单独打开扩展。

## 为什么需要这个？

x86 的 instruction selection 有一个很诱人的错觉：一条 `load` 可以带 `base + index*scale + disp`，一条 `add` 可以带内存操作数，很多 IR 操作能「折叠进一条指令」。到了 RISC-V，这些折叠大多不存在：

- 没有 indexed addressing：`p[i]` 需要先算地址，再 `ld`。
- `addi` 只有 12-bit signed immediate：大常量要 `lui + addi`。
- 没有 condition code：比较结果要显式写进寄存器（`slt`、`sltu`、`snez`）。
- `select` 没有原生的条件移动（除非有 `Zicond`）：默认走分支。
- 很多优化靠扩展：`Zba` 的 `sh1add/sh3add`、`Zbb` 的 `andn/rori`、`Zicond` 的 `czero`。

这些约束会直接改变 **指令数量、依赖链长度、基本块结构**，从而改变 scheduler 能做什么。面试里问「为什么这里选了两条指令而不是一条」「为什么这个 `select` 变成分支」，答案往往在 ISA 约束，而不是 scheduler。

## 先建立直觉

把 RISC-V 想成一个「地址计算和算术都要自己写」的 ISA：

```
x86 幻觉：
  add  rax, [rdi + rsi*8 + 16]     ; 一条指令：load + add + 地址计算

RISC-V 现实：
  slli t0, rsi, 3                  ; index * 8
  add  t1, rdi, t0                 ; base + index*8
  ld   t2, 16(t1)                  ; load
  add  rax, rax, t2                ; add
```

当然，RISC-V 也有 `sh3add`（Zba）可以把 `slli+add` 合成一条，但这是扩展，不是基线。编译器要先判断「这个 target 有没有 Zba」，再决定选几条指令。

这就是为什么同一段 IR，在 `-mattr=+zba` 和没有 Zba 时，指令数量和依赖链会变。**selection 的每个决定，都会变成 scheduler 的输入。**

## 约束一：load/store 架构，没有 indexed addressing

### 常量索引：可以 folded 进 offset

```llvm
define i64 @idx_const(ptr %p) {
  %q = getelementptr i64, ptr %p, i64 1
  %v = load i64, ptr %q
  ret i64 %v
}
```

`llc -mtriple=riscv64 -mcpu=generic -O2`：

```asm
idx_const:
	ld	a0, 8(a0)
	ret
```

`getelementptr` 的 `+1` 被 folded 成 `LD` 的 offset 8。这是 SelectionDAG 的 `SelectAddrRegImm`（`RISCVISelDAGToDAG.cpp` 约 3630 行）做的：它把「base + 常量」匹配成 `base, offset`，offset 在 `simm12` 范围内就塞进 load/store。

### 变量索引：必须先算地址

```llvm
define i64 @idx(ptr %p, i64 %i) {
  %q = getelementptr i64, ptr %p, i64 %i
  %v = load i64, ptr %q
  ret i64 %v
}
```

没有 Zba：

```asm
idx:
	slli	a1, a1, 3
	add	a0, a0, a1
	ld	a0, 0(a0)
	ret
```

有 Zba：

```asm
idx:
	sh3add	a0, a1, a0
	ld	a0, 0(a0)
	ret
```

命令：

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc

# 无 Zba
"$LLC" -mtriple=riscv64 -mcpu=generic -O2 idx.ll -o -

# 有 Zba
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zba -O2 idx.ll -o -
```

差异的源码落点：

- `RISCVISelDAGToDAG.cpp` 的 `SelectAddrRegRegScale()`（约 3977 行）尝试匹配 `base + index*scale`。
- `RISCVInstrInfoZb.td` 的 `Sh3AddPat<SH3ADD>`（约 819–846 行）把 `(add (shl x, 3), y)` 匹配成 `sh3add`。
- `CSImm12MulBy8Pat<SH3ADD>`（约 741–753 行）处理 `base + index*8 + imm12` 的形式。

### 调度后果

- 三条指令的地址链 `slli → add → ld` 有 3 个依赖级；`sh3add → ld` 只有 2 个。
- 没有 Zba 时，`slli` 和 `add` 可以和其他独立指令交错，scheduler 有更多「填充槽」；有 Zba 时，指令少了，但 `sh3add` 和 `ld` 的依赖更紧。
- 如果地址计算不依赖 load，scheduler 可以把它提前到 load 之前很远；如果它依赖前一个 load，就要等。

这就是「selection 改变调度自由度」的最直接例子：**不是指令越少越好，而是关键路径和可交错空间要一起看。**

## 约束二：12-bit immediate

`addi` 的立即数是 signed 12-bit，范围 `[-2048, 2047]`。超出范围，编译器要拆成 `lui + addi`（或常量池）。

```llvm
define i64 @big() {
  ret i64 305419896
}
```

`305419896 = 0x12345678`。实测：

```asm
big:
	lui	a0, 74565
	addi	a0, a0, 1656
	ret
```

`74565 = 0x12345`，`1656 = 0x678`。因为：

```text
0x12345 << 12 = 0x12345000
0x12345000 + 0x678 = 0x12345678
```

`lui` 把 20-bit 立即数放到高 20 位，低 12 位补 0；`addi` 补上低 12 位。两条指令，依赖链 `lui → addi`。

对比小常量：

```llvm
define i64 @small() {
  ret i64 100
}
```

```asm
small:
	li	a0, 100
	ret
```

`li` 是伪指令，汇编器会把它展开成 `addi a0, zero, 100`。100 超出 `c.li` 的 6-bit immediate 范围，所以这里不会压缩成 `c.li`；实测 `llvm-mc -mattr=+c -show-encoding` 的 encoding 是 4 字节。从 selection 的角度看，它在 `simm12` 范围内，不需要 `lui`。这就是 immediate 范围对 selection 的直接影响。

### 比较也用 immediate

```llvm
define i64 @cmp(i64 %a) {
  %c = icmp ult i64 %a, 100
  %r = zext i1 %c to i64
  ret i64 %r
}
```

实测：

```asm
cmp:
	sltiu	a0, a0, 100
	ret
```

`sltiu` 直接带 12-bit unsigned immediate，所以一条就够。如果比较的常量超出范围，就要先 materialize 常量，再 `sltu`，指令数和依赖链都会变。

### 布尔值：`snez` / `seqz`

RISC-V 没有 condition code，比较结果必须写进通用寄存器。`icmp ne 0` 通常选 `snez`：

```llvm
define i64 @bool(i64 %a) {
  %c = icmp ne i64 %a, 0
  %r = zext i1 %c to i64
  ret i64 %r
}
```

实测：

```asm
bool:
	snez	a0, a0
	ret
```

`zext i1` 没有单独的指令：`snez` 的结果已经是 0/1。这是 SelectionDAG 的 `setcc` lowering 和 `zext` combine 一起做的。

### 调度后果

- `lui + addi` 是两条指令的常量物化链。如果这个常量在循环里不变，scheduler/LICM 可能把它提到循环外；如果每次都要算，就多两条指令的延迟。
- `sltiu` 一条指令，`sltu + materialize` 两条以上；比较结果的可用时间不同，影响分支/`select` 的调度。
- 常量物化常常被 hoist 到基本块开头；你会在汇编里看到 `lui` 离使用点很远，这是 scheduler 在填充延迟。

## 约束三：没有 condition code，`select` 要么分支要么 `czero`

```llvm
define i64 @sel(i1 %c, i64 %a, i64 %b) {
  %r = select i1 %c, i64 %a, i64 %b
  ret i64 %r
}
```

没有 `Zicond`：

```asm
sel:
	andi	a3, a0, 1
	mv	a0, a1
	bnez	a3, .LBB0_2
	mv	a0, a2
.LBB0_2:
	ret
```

有 `Zicond`：

```asm
sel:
	andi	a0, a0, 1
	czero.nez	a2, a2, a0
	czero.eqz	a0, a1, a0
	or	a0, a0, a2
	ret
```

命令：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -O2 sel.ll -o -
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zicond -O2 sel.ll -o -
```

源码落点：

- `RISCVISelLowering.cpp` 约 10470 行：`const bool HasZicond = ... Subtarget.hasStdExtZicond();`
- 同文件约 10689 行：`if (Subtarget.hasStdExtZicond() && VT.isScalarInteger())`，然后生成 `CZERO_EQZ` / `CZERO_NEZ`。
- `RISCVInstrInfoZicond.td`：`CZERO_EQZ` / `CZERO_NEZ` 的定义和 `CondOpsPat` pattern。
- `RISCVInstrInfo.td` 的 `SELECT` 相关 pattern：没有 Zicond 时，`select` 可能被 lowering 成 `BRCOND` + `PHI` 或条件分支。

### 调度后果

- 分支版本把函数切成多个基本块。scheduler 只能在基本块内调度；跨基本块的指令不能随便移。
- 直线版本（`czero`）让 scheduler 可以在一个基本块里交错，但多了 `czero.nez`、`czero.eqz`、`or` 三条指令，寄存器压力和依赖链都变了。
- `czero` 的输入依赖条件 `a0` 和两个值 `a1/a2`，关键是「条件先算好」；分支版本的关键是「分支预测」，编译器模型里用 `MispredictPenalty` 表示。
- 所以「branchless 一定更快」是错的。短条件、可预测分支，分支版本可能更好；长依赖、不可预测条件，`czero` 可能更好。第 21 章会展开 if-conversion。

## 约束四：扩展指令改变选择

同一个 `andn` 语义：

```llvm
define i64 @andn(i64 %a, i64 %b) {
  %n = xor i64 %b, -1
  %r = and i64 %a, %n
  ret i64 %r
}
```

没有 Zbb：

```asm
andn:
	not	a1, a1
	and	a0, a0, a1
	ret
```

有 Zbb：

```asm
andn:
	andn	a0, a0, a1
	ret
```

命令：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -O2 andn.ll -o -
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zbb -O2 andn.ll -o -
```

源码落点：`RISCVInstrInfoZb.td` 里的 `andn` pattern 和 `HasStdExtZbb` predicate。

### 扩展选择表

| 扩展 | 典型指令 | 替代的基线序列 | 选择影响 | 调度影响 |
|---|---|---|---|---|
| `Zba` | `sh1add` / `sh3add` | `slli + add` | 2→1 条 | 地址链缩短 |
| `Zbb` | `andn` / `orn` / `xnor` | `not + and/or/xor` | 2→1 条 | 依赖链缩短 |
| `Zbb` | `rol` / `ror` / `rori` | `slli + srli + or` | 3→1 条 | 关键路径大幅缩短 |
| `Zbb` | `clz` / `ctz` / `cpop` | 循环或位技巧 | 多→1 条 | 从循环变直线 |
| `Zicond` | `czero.eqz` / `czero.nez` | 分支 + `mv` | 分支→直线 | 基本块结构变化 |
| `Zbs` | `bset` / `bclr` / `binv` | `li + or/and/xor` | 2→1 条 | 常量物化消失 |
| `M` | `mul` / `div` / `rem` | `__muldi3` 等 libcall | 调用→指令 | 调用边界消失，调度空间变大 |

注意最后一行的 `M`：没有 `M` 时，`mul` 变成 `__muldi3` 调用。调用是调度边界，scheduler 不能把调用前后的指令随便移。所以「有没有 M」不只是 instruction selection，它直接改变了 scheduling 的 region。

## 约束五：压缩指令不在 SelectionDAG

RISC-V 的 `C` 扩展把 32-bit 指令压成 16-bit `c.*`。但你在 `llc` 的汇编输出里通常看到的是 `addi`、`add`、`ld`，不是 `c.addi`。原因是：

- SelectionDAG/GlobalISel 选的是 32-bit 指令。
- 压缩发生在 **MC 层**：`RISCVBaseInfo.cpp` 约 182 行 include 了 `RISCVGenCompressInstEmitter.inc`，`compressInst()` 在编码阶段尝试把指令换成压缩形式。
- `llc` 输出汇编时，通常保留 32-bit 形式；`llvm-mc -filetype=obj` 或 object emission 才会真正编码成 16-bit。

对 scheduling 的影响：

- `MachineScheduler` 看到的是 32-bit 指令和 `MCSchedModel`；压缩不改变 MI 的 `SchedRW`。
- 压缩会影响取指带宽、代码大小、分支目标对齐，但这些不在 `MachineScheduler` 的模型里（至少不是主要因素）。
- 如果面试问「压缩指令对调度有什么影响」，正确答法是「MI 调度基本看不到；它影响的是 frontend/fetch 和代码大小，`llvm-mca` 也主要按 32-bit 模型算」。

## 一个完整的对照：同一段 IR，四种 feature

```bash
for attrs in "" "+zba" "+zbb" "+zicond"; do
  echo "===== mattr=$attrs ====="
  "$LLC" -mtriple=riscv64 -mcpu=generic -mattr=$attrs -O2 feat.ll -o - \
    | sed -n '/^sel:/,/^\.Lfunc_end/p;/^idx:/,/^\.Lfunc_end/p;/^andn:/,/^\.Lfunc_end/p'
done
```

实测（节选）：

```asm
===== mattr= =====
sel:
	andi	a3, a0, 1
	mv	a0, a1
	bnez	a3, .LBB0_2
	mv	a0, a2
.LBB0_2:
	ret

idx:
	slli	a1, a1, 3
	add	a0, a0, a1
	ld	a0, 0(a0)
	ret

andn:
	not	a1, a1
	and	a0, a0, a1
	ret

===== mattr=+zba =====
idx:
	sh3add	a0, a1, a0
	ld	a0, 0(a0)
	ret

===== mattr=+zbb =====
andn:
	andn	a0, a0, a1
	ret

===== mattr=+zicond =====
sel:
	andi	a0, a0, 1
	czero.nez	a2, a2, a0
	czero.eqz	a0, a1, a0
	or	a0, a0, a2
	ret
```

这张表就是本章的总结：**同一个 IR，feature 一变，selection 就变；selection 一变，依赖链和基本块结构就变；这些都会改变 scheduler 的输入。**

## 踩雷集锦

1. **以为 `-mcpu` 只影响调度**：`-mcpu` 同时决定 feature。`sifive-u74` 带 `M/A/F/D/C`，`generic-rv64` 只带 `I`。不固定 `-mattr` 就对比调度，等于同时改了两个变量。

2. **以为 `getelementptr` 总能 folded 进 load**：只有常量偏移且在 `simm12` 范围内才行。变量索引、超出范围的偏移都要先算地址。

3. **以为 `select` 一定是分支**：有 `Zicond` 时会变成 `czero` 直线代码。反过来，以为 `select` 一定 branchless 也是错的；没有 `Zicond` 就是分支。

4. **把 `lui+addi` 当成两条独立指令**：它们是一条常量物化链，`addi` 依赖 `lui`。scheduler 可以把 `lui` 提前，但不能把 `addi` 提到 `lui` 前面。

5. **在 `llc` 汇编里找 `c.addi`**：压缩在 MC 层，`llc` 通常输出 32-bit 形式。要看压缩，用 `llvm-mc -filetype=obj -show-encoding` 或反汇编。

6. **忽略 `M` 扩展对调用边界的影响**：没有 `M` 时，`mul/div/rem` 变成 libcall；调用是调度边界，不能只把它当成「多一条指令」。

7. **把 `snez` 当成 `setcc` 的单独指令**：`snez` 同时完成 compare 和 zext；`zext i1` 不会再多一条。

## 动手练习

1. 写一个 `i32` 版本的 `idx`，用 `-mattr=+zba` 和没有 Zba 各跑一次。`sh1add` 还是 `sh3add`？为什么 scale 不同？

2. 把 `big` 的常量改成 `0x7fffffff` 和 `0x80000000`，各跑一次。`lui` 的立即数是多少？`addi` 的立即数是正还是负？解释 signed 12-bit 的影响。

3. 把 `sel` 的条件改成 `icmp slt`，再跑 `+zicond`。`czero` 序列有变化吗？为什么？

4. 用 `-mattr=+zbb` 写一个 `rotl`/`rotr` 的 IR，看它选成 `rol`/`ror` 还是 `slli+srli+or`。用 `llvm-mca` 比较两种序列的关键路径。

5. 用 `-mattr=+m` 和没有 `M` 跑同一个 `mul` 函数，比较最终汇编和 `-print-before/after=machine-scheduler`。调用边界对调度区域有什么影响？

6. 用 `llvm-mc -triple=riscv64 -mattr=+c -show-encoding` 汇编一条 `addi a0, a0, 1`，看 encoding 是不是 2 字节；再试 `addi a0, a0, 1000`，看 encoding 是不是 4 字节。注意汇编文本仍然写 `addi`，压缩体现在 encoding 上。

## 本章重点整理

- RISC-V 是 load/store 架构，没有 indexed addressing；变量索引要先算地址，常量偏移可以 folded 进 load/store。
- `addi` 是 signed 12-bit immediate；大常量要 `lui + addi`，形成两条指令的物化链。
- 没有 condition code；比较结果用 `slt/sltu/snez/seqz` 写进寄存器，`zext i1` 通常被合并。
- `select` 默认是分支；有 `Zicond` 时变成 `czero` 直线代码。两者对 scheduling 的影响完全不同。
- 扩展（`Zba`/`Zbb`/`Zicond`/`M`）会改变指令数量、依赖链、调用边界，从而改变 scheduler 的输入。
- 压缩指令在 MC 层，不在 SelectionDAG；MI scheduler 看到的是 32-bit 指令。

## 自我检核

- [ ] 能解释为什么 `idx` 在无 Zba 时是三条指令，有 Zba 时是两条
- [ ] 能算出 `lui` / `addi` 的立即数，并解释 12-bit signed 范围
- [ ] 能说出 `select` 在有无 `Zicond` 时的两种 lowering，以及各自的调度后果
- [ ] 能说出至少四个扩展指令及其替代的基线序列
- [ ] 能解释压缩指令为什么不在 SelectionDAG 里
- [ ] 能解释 `M` 扩展如何通过「消除 libcall」改变 scheduling region

## 延伸阅读

### 官方文档

- **[RISC-V ISA Specification](https://riscv.org/technical/specifications/)**
  - **读哪里**：RV64I 的整数指令、`lui`/`addi`/`sltiu` 的立即数编码；`Zba`/`Zbb`/`Zicond` 扩展章节。
  - **和本章的关联**：本章所有指令语义和 immediate 范围都以 spec 为准。

- **[LLVM RISC-V Usage](https://llvm.org/docs/RISCVUsage.html)**
  - **读哪里**：扩展支持表、`-mattr` 的写法、`-mcpu` 的 CPU 列表。
  - **和本章的关联**：确认 `+zba`、`+zbb`、`+zicond` 在当前 LLVM 的拼写和支持状态。

### 源码

- **`llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`**
  - **读哪里**：`SelectAddrRegImm()`（约 3630 行）、`SelectAddrRegRegScale()`（约 3977 行）。
  - **和本章的关联**：常量偏移和变量索引的匹配逻辑。

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**
  - **读哪里**：约 10470 行的 `HasZicond` 判断、约 10689 行的 `CZERO_EQZ`/`CZERO_NEZ` 生成。
  - **和本章的关联**：`select` 的两种 lowering。

- **`llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`**
  - **读哪里**：`CSImm12MulBy8Pat`（约 741 行）、`Sh1AddPat`（约 809 行）、`Sh3AddPat`（约 819 行）。
  - **和本章的关联**：`Zba` 的 pattern。

- **`llvm/lib/Target/RISCV/RISCVInstrInfoZicond.td`**
  - **读哪里**：`CZERO_EQZ` / `CZERO_NEZ` 定义、`CondOpsPat`。
  - **和本章的关联**：`Zicond` 的指令和 pattern。

- **`llvm/lib/Target/RISCV/MCTargetDesc/RISCVBaseInfo.cpp`**
  - **读哪里**：约 182 行的 `RISCVGenCompressInstEmitter.inc`、`compressInst()`。
  - **和本章的关联**：压缩发生在 MC 层，不在 SelectionDAG。

### 文章 / 演讲

- **"RISC-V Code Size Reduction"**（RISC-V 相关 LLVM 演讲/博客）
  - **这讲什么**：压缩指令、链接器 relaxation、代码大小优化。
  - **为什么值得看**：理解为什么 `C` 扩展的收益主要在 fetch/代码大小，而不是 MI 调度。

- **"RISC-V Bit Manipulation Extensions"**（`Zba`/`Zbb`/`Zbs` 的 spec 和 LLVM 实现）
  - **这讲什么**：位操作的指令语义和 pattern。
  - **和本章的关联**：`andn`、`sh3add`、`rol/ror` 的来源。

下一章开始拆 SelectionDAG 本身：`SDNode`、chain、glue、`TokenFactor`。你会看到为什么 scheduler 不能随便移动一条 load，以及为什么 `select` 的 pattern 会生成带 glue 的多指令组。

→ [Ch 4 SelectionDAG 的数据结构：chain、glue、token](./04-selectiondag-data-structures.md)
