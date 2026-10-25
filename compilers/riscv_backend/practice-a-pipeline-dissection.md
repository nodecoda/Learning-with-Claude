# 练习 A：一次编译的六段解剖

> 目标：给定一段同时含 `mul`、`add`、`select`、`call` 的 IR，产出六段可以互相印证的证据：IR、isel 后 MIR、RA 后 MIR、最终汇编、`llvm-objdump` 反汇编、`llvm-readobj` 重定位。你要能用这六段证据解释“为什么最终汇编长这样”，而不是只看最后一段。

> 环境：LLVM 23.1.4 预编译版；命令在仓库根目录 `D:\Learning-with-Claude` 下执行。所有 `llc`、`llvm-objdump`、`llvm-readobj` 输出都来自本机实测。

## 为什么

单看最终汇编，你只能看到结果；看到六段，你才能看到结果是怎么一步步变成的。这个练习的价值在于建立“证据链”：

- IR 告诉你源语义：乘法、选择、调用、加法。
- isel 后 MIR 告诉你目标指令的雏形：`MUL`、`Select_GPR_Using_CC_GPR`、`PseudoCALL`。
- RA 后 MIR 告诉你物理寄存器、分支、栈帧、调用约定。
- 最终汇编告诉你 AsmPrinter 如何把 MIR 变成文本（别名、CFI、伪指令展开）。
- `objdump` 告诉你目标文件里的字节如何被反汇编。
- `readobj` 告诉你哪些位置还需要链接器/汇编器修补。

六段之间任何一段对不上，都是一个可定位的问题。比如最终汇编有 `call g`，但 `readobj` 没有 `R_RISCV_CALL_PLT`，说明重定位在某一站丢了；比如 isel 后 MIR 有 `MUL`，RA 后 MIR 变成 `__muldi3`，说明特性或 legalizer 在中间变了。

本练习不要求你改 LLVM 源码，只要求你把六段证据收集齐，并用它们回答三个问题：

1. `select` 为什么变成分支，而不是条件移动？
2. `call g` 为什么要 16 字节栈帧，`ra` 存在哪里？
3. 对象文件里的 `auipc ra, 0` / `jalr ra` 为什么目标地址看起来是 0？

## 主线

### A.1 输入：一段含四类操作的 IR

把下面的内容保存为 `pa.ll`：

```llvm
target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
target triple = "riscv64-unknown-elf"

declare i64 @g(i64)

define i64 @kernel(i64 %a, i64 %b, i1 %c) {
entry:
  %m = mul i64 %a, %b
  %s = select i1 %c, i64 %m, i64 7
  %call = call i64 @g(i64 %s)
  %r = add i64 %call, 3
  ret i64 %r
}
```

这段 IR 故意保留四类操作：

- `mul i64`：在 RV64 + M 下应该选成 `mul`。
- `select i1`：没有 `+zicond` 时通常走分支 + `mv`/`li`。
- `call i64 @g(i64)`：外部调用，必须保存 `ra`，并产生调用重定位。
- `add i64 ... , 3`：调用之后的返回值加法，应该选成 `addi`。

`@g` 只有声明没有定义，这样调用不会被内联或删除，重定位也会保留。

### A.2 六段证据各自回答什么

| 段 | 产物 | 回答的问题 | 关键标记 |
| --- | --- | --- | --- |
| 1 | IR | 源语义是什么？ | `mul`、`select`、`call`、`add` |
| 2 | isel 后 MIR | 选成了哪些目标指令？ | `MUL`、`Select_GPR_Using_CC_GPR`、`PseudoCALL` |
| 3 | RA 后 MIR | 用了哪些物理寄存器？栈帧多大？ | `$x10`、`$x1`、`$x2`、`%stack.0`、`BEQ` |
| 4 | 最终汇编 | AsmPrinter 如何打印？ | `mul`、`bnez`、`call g`、`sd ra, 8(sp)` |
| 5 | `objdump` | 字节如何反汇编？ | `bnez`/`j` 目标未解析、`auipc`+`jalr` |
| 6 | `readobj` | 哪些位置需要重定位？ | `R_RISCV_BRANCH`、`R_RISCV_JAL`、`R_RISCV_CALL_PLT`、`R_RISCV_RELAX` |

这六段不是随便选的。它们覆盖了三次表示切换：IR → MIR（第 2 段）、MIR → 汇编（第 4 段）、汇编/对象 → 字节（第 5/6 段）。每一段都能独立回答一个问题，合起来能回答“为什么”。

### A.3 评分表

总分 20 分。每一项按“缺失/错误 0 分、部分正确 1 分、完整正确满分”评分。

| 项目 | 分值 | 满分标准 |
| --- | --- | --- |
| IR 识别 | 2 | 指出 `mul`、`select`、`call`、`add` 四类操作，并说明 `select` 的两个值 |
| isel 后 MIR | 4 | 给出 `kernel` 函数体，指出虚拟寄存器、`MUL`、`Select_GPR_Using_CC_GPR`、`PseudoCALL` |
| RA 后 MIR | 4 | 给出物理寄存器、`BEQ`、`%stack.0`、`PseudoCALL` 的 `csr_ilp32_lp64` |
| 最终汇编 | 4 | 逐段对应 MIR，指出 `bnez`、`li 7`、`mul`、`call g`、`sd/ld ra` |
| `objdump` | 2 | 指出分支目标未解析、`auipc ra, 0` + `jalr ra` 是未重定位的调用 |
| `readobj` | 2 | 列出 `.rela.text` 的五条重定位，并解释 `R_RISCV_RELAX` |
| 解释题 | 2 | 回答 `select` 分支化、16 字节栈帧、`ra` 保存位置三个问题 |

只贴输出、不解释，最多拿一半分。反过来，解释清楚但漏一段输出，也按缺失项扣分。

## 可跑实验

### 实验 1：采集六段证据

目的：把六段产物一次性生成，后续参考答案和评分都以此为准。

命令：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$dir = Join-Path $env:TEMP "riscv-practice-a"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$ll  = Join-Path $dir "pa.ll"

$lines = @(
  'target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"'
  'target triple = "riscv64-unknown-elf"'
  ''
  'declare i64 @g(i64)'
  ''
  'define i64 @kernel(i64 %a, i64 %b, i1 %c) {'
  'entry:'
  '  %m = mul i64 %a, %b'
  '  %s = select i1 %c, i64 %m, i64 7'
  '  %call = call i64 @g(i64 %s)'
  '  %r = add i64 %call, 3'
  '  ret i64 %r'
  '}'
)
[IO.File]::WriteAllText($ll, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))

& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+m,+relax" -O2 $ll -o (Join-Path $dir "pa.s")
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+m,+relax" -O2 -stop-after=riscv-isel $ll -o (Join-Path $dir "pa-isel.mir")
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+m,+relax" -O2 -stop-after=prolog-epilog $ll -o (Join-Path $dir "pa-ra.mir")
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=+m,+relax" -O2 -filetype=obj $ll -o (Join-Path $dir "pa.o")

Get-Content -LiteralPath (Join-Path $dir "pa.s")
& "$bin\llvm-objdump.exe" -d --no-show-raw-insn (Join-Path $dir "pa.o")
& "$bin\llvm-readobj.exe" --relocations (Join-Path $dir "pa.o")
```

预期输出：`pa.s` 是 4 字节对齐的函数，含 `bnez` 分支、`mul`、`call g`、`sd/ld ra`；`pa-isel.mir` 含虚拟寄存器和 `PseudoCALL`；`pa-ra.mir` 含物理寄存器、`%stack.0` 和 `BEQ`；`objdump` 里有未解析的分支目标和 `auipc`+`jalr`；`readobj` 的 `.rela.text` 有五条记录。

失败排查：

- `pa.s` 里没有 `mul`：检查 `-mattr` 是否被 PowerShell 的逗号拆开。用 `"-mattr=+m,+relax"` 这种整串写法。
- `pa-isel.mir` 里没有 `kernel`：`-stop-after=riscv-isel` 的 MIR 会包含模块头和多个函数块，用 `Select-String -Pattern '^name:\s+kernel'` 定位。
- `readobj` 没有 `R_RISCV_CALL_PLT`：检查 `@g` 是否被内联或删除。声明外部函数、不提供定义，才能保留调用。

### 实验 2：关掉 M，比较代码形状

目的：看特性开关如何改变 select、调用和栈帧。

命令：

```powershell
& "$bin\llc.exe" -mtriple=riscv64 "-mattr=-m,+relax" -O2 $ll -o -
```

真实输出（节选）：

```asm
kernel:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	sd	s0, 0(sp)
	andi	s0, a2, 1
	call	__muldi3
	bnez	s0, .LBB0_2
	li	a0, 7
.LBB0_2:
	call	g
	addi	a0, a0, 3
	ld	ra, 8(sp)
	ld	s0, 0(sp)
	addi	sp, sp, 16
	ret
```

解读：关掉 M 后，乘法变成 `__muldi3` libcall；因为 libcall 会破坏 `a0`/`a1`，原来的 select 条件必须先保存到 `s0`；于是多了一次 `sd s0, 0(sp)`，栈帧仍然是 16 字节但内容更满。调用顺序也变了：libcall 被提到 `bnez` 之前，因为乘法结果两边都要用。

失败排查：如果关掉 M 后仍然看到 `mul`，检查 `.ll` 里是否有函数级 `target-features` 或模块 flag 把 M 又打开。

### 实验 3（边界）：对象文件里的分支目标为什么是 0

目的：理解 `objdump` 与 `readobj` 的分工。对象文件不是可执行文件，分支和调用的目标还没有最终地址。

命令：

```powershell
& "$bin\llvm-objdump.exe" -d --no-show-raw-insn (Join-Path $dir "pa.o")
& "$bin\llvm-readobj.exe" --relocations (Join-Path $dir "pa.o")
```

真实反汇编：

```text
0000000000000000 <kernel>:
       0:      	andi	a2, a2, 0x1
       4:      	bnez	a2, 0x4 <kernel+0x4>
       8:      	li	a0, 0x7
       c:      	j	0xc <kernel+0xc>
      10:      	mul	a0, a0, a1
      14:      	addi	sp, sp, -0x10
      18:      	sd	ra, 0x8(sp)
      1c:      	auipc	ra, 0x0
      20:      	jalr	ra <kernel+0x1c>
      24:      	addi	a0, a0, 0x3
      28:      	ld	ra, 0x8(sp)
      2c:      	addi	sp, sp, 0x10
      30:      	ret
```

解读：`bnez a2, 0x4` 和 `j 0xc` 的目标看起来指向自己，`jalr ra` 也没有偏移。这不是编码错误，而是目标地址还在重定位里。链接器会根据 `R_RISCV_BRANCH`、`R_RISCV_JAL`、`R_RISCV_CALL_PLT` 填入最终值。

失败排查：如果 `readobj` 里没有对应的 `.rela.text`，说明 `llc` 已经把目标解析成了绝对地址，或者你反汇编的是链接后的可执行文件，而不是 `.o`。
<details>
<summary>参考答案</summary>

### 答案 1：IR

```llvm
target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
target triple = "riscv64-unknown-elf"

declare i64 @g(i64)

define i64 @kernel(i64 %a, i64 %b, i1 %c) {
entry:
  %m = mul i64 %a, %b
  %s = select i1 %c, i64 %m, i64 7
  %call = call i64 @g(i64 %s)
  %r = add i64 %call, 3
  ret i64 %r
}
```

关键点：`%c` 为真时选 `%m`，为假时选常量 7；`@g` 是外部声明，调用必须保留；返回值加 3。

### 答案 2：isel 后 MIR

命令：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+relax" -O2 -stop-after=riscv-isel pa.ll -o pa-isel.mir
```

`kernel` 的真实函数体：

```text
body:             |
  bb.0.entry:
    liveins: $x10, $x11, $x12

    %2:gpr = COPY $x12
    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %3:gpr = ANDI %2, 1
    %4:gpr = MUL %0, %1
    %5:gpr = ADDI $x0, 7
    %6:gpr = Select_GPR_Using_CC_GPR killed %3, $x0, 1, killed %4, killed %5
    ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
    $x10 = COPY %6
    PseudoCALL target-flags(riscv-call) @g, csr_ilp32_lp64, implicit-def dead $x1, implicit $x10, implicit-def $x2, implicit-def $x10
    ADJCALLSTACKUP 0, 0, implicit-def dead $x2, implicit $x2
    %7:gpr = COPY $x10
    %8:gpr = ADDI %7, 3
    $x10 = COPY %8
    PseudoRET implicit $x10
```

读法：

- `%0`-`%8` 是虚拟寄存器，`gpr` 是寄存器类。
- `ANDI %2, 1` 把 `i1` 条件掩成 0/1。
- `MUL` 是目标乘法；`ADDI $x0, 7` 把常量 7 放进一个虚拟寄存器。
- `Select_GPR_Using_CC_GPR` 是选择伪指令，参数里带条件码（`1`）和两个候选值。
- `PseudoCALL` 带 `target-flags(riscv-call)` 和调用约定 `csr_ilp32_lp64`。
- `ADJCALLSTACKDOWN/UP 0` 说明当前调用不需要额外参数栈空间，但保留了 adjust 结构。
- `PseudoRET` 还没展开。

### 答案 3：RA 后 MIR

命令：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+relax" -O2 -stop-after=prolog-epilog pa.ll -o pa-ra.mir
```

`kernel` 的真实函数体：

```text
body:             |
  bb.0.entry:
    successors: %bb.2(0x40000000), %bb.1(0x40000000)
    liveins: $x10, $x11, $x12, $x1

    renamable $x12 = ANDI killed renamable $x12, 1
    BEQ killed renamable $x12, $x0, %bb.2

  bb.1:
    successors: %bb.3(0x80000000)
    liveins: $x10, $x11, $x1

    renamable $x10 = MUL killed renamable $x10, killed renamable $x11
    PseudoBR %bb.3

  bb.2.entry:
    successors: %bb.3(0x80000000)
    liveins: $x1

    renamable $x10 = ADDI $x0, 7

  bb.3.entry:
    liveins: $x10, $x1

    $x2 = frame-setup ADDI $x2, -16
    frame-setup CFI_INSTRUCTION def_cfa_offset 16
    frame-setup SD killed $x1, $x2, 8 :: (store (s64) into %stack.0)
    frame-setup CFI_INSTRUCTION offset $x1, -8
    PseudoCALL target-flags(riscv-call) @g, csr_ilp32_lp64, implicit-def dead $x1, implicit $x10, implicit-def $x2, implicit-def $x10
    renamable $x10 = ADDI killed renamable $x10, 3
    $x1 = frame-destroy LD $x2, 8 :: (load (s64) from %stack.0)
    frame-destroy CFI_INSTRUCTION restore $x1
    $x2 = frame-destroy ADDI $x2, 16
    frame-destroy CFI_INSTRUCTION def_cfa_offset 0
    PseudoRET implicit $x10
```

读法：

- 虚拟寄存器全部消失，`%a`/`%b`/`%c` 分别是 `$x10`/`$x11`/`$x12`。
- `Select_GPR_Using_CC_GPR` 被拆成 `BEQ` + 两个分支：假分支 `bb.2` 放 `ADDI $x0, 7`，真分支 `bb.1` 放 `MUL`，两路汇合到 `bb.3`。
- `$x2 = frame-setup ADDI $x2, -16` 建立 16 字节栈帧；`SD $x1, 8` 把 `ra` 存到 `%stack.0`。
- `PseudoCALL` 的 `implicit-def dead $x1` 表示调用破坏 `ra`；`csr_ilp32_lp64` 是调用约定。
- `frame-destroy` 指令恢复 `ra` 和 `sp`，然后 `PseudoRET`。

### 答案 4：最终汇编

命令：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+relax" -O2 pa.ll -o pa.s
```

真实汇编：

```asm
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
	.file	"pa.ll"
	.text
	.globl	kernel                          # -- Begin function kernel
	.p2align	2
	.type	kernel,@function
kernel:                                 # @kernel
	.cfi_startproc
# %bb.0:                                # %entry
	andi	a2, a2, 1
	bnez	a2, .LBB0_2
# %bb.1:                                # %entry
	li	a0, 7
	j	.LBB0_3
.LBB0_2:
	mul	a0, a0, a1
.LBB0_3:                                # %entry
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	call	g
	addi	a0, a0, 3
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
.Lfunc_end0:
	.size	kernel, .Lfunc_end0-kernel
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

逐段对应：

- `andi a2, a2, 1` + `bnez a2, .LBB0_2` 对应 RA MIR 的 `ANDI` + `BEQ`。
- `li a0, 7` 对应假分支的 `ADDI $x0, 7`；`li` 是伪指令，汇编器会选 `addi`。
- `j .LBB0_3` 对应 `PseudoBR`。
- `mul a0, a0, a1` 对应真分支的 `MUL`。
- `addi sp, sp, -16` / `sd ra, 8(sp)` / `ld ra, 8(sp)` / `addi sp, sp, 16` 对应 `frame-setup`/`frame-destroy`。
- `call g` 对应 `PseudoCALL`；AsmPrinter 把它打印成汇编伪指令，不是最终的 `auipc`+`jalr`。
- `ret` 对应 `PseudoRET`；汇编器把它编码成 `jalr x0, 0(ra)`，但 `.s` 里保留别名 `ret`。

### 答案 5：objdump

命令：

```powershell
llvm-objdump -d --no-show-raw-insn pa.o
```

真实输出：

```text
0000000000000000 <kernel>:
       0:      	andi	a2, a2, 0x1
       4:      	bnez	a2, 0x4 <kernel+0x4>
       8:      	li	a0, 0x7
       c:      	j	0xc <kernel+0xc>
      10:      	mul	a0, a0, a1
      14:      	addi	sp, sp, -0x10
      18:      	sd	ra, 0x8(sp)
      1c:      	auipc	ra, 0x0
      20:      	jalr	ra <kernel+0x1c>
      24:      	addi	a0, a0, 0x3
      28:      	ld	ra, 0x8(sp)
      2c:      	addi	sp, sp, 0x10
      30:      	ret
```

读法：

- `bnez` 和 `j` 的目标显示成自身附近，是因为目标还在重定位里，不是最终地址。
- `call g` 已经展开成 `auipc ra, 0` + `jalr ra` 两条指令；偏移由 `R_RISCV_CALL_PLT` 修补。
- `ret` 被反汇编成 `ret` 别名；编码上它是 `jalr x0, 0(ra)`。
- 指令地址间隔是 4 字节，没有压缩指令；因为 `+c` 没有开，且这条函数的操作数也不都在压缩寄存器子集里。

### 答案 6：readobj 重定位

命令：

```powershell
llvm-readobj --relocations pa.o
```

真实输出（文件路径已省略）：

```text
File: pa.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
Relocations [
  Section (3) .rela.text {
    0x4 R_RISCV_BRANCH .L0  0x0
    0xC R_RISCV_JAL .L0  0x0
    0xC R_RISCV_RELAX - 0x0
    0x1C R_RISCV_CALL_PLT g 0x0
    0x1C R_RISCV_RELAX - 0x0
  }
  Section (7) .rela.eh_frame {
    0x1C R_RISCV_32_PCREL .L0  0x0
    0x20 R_RISCV_ADD32 .L0  0x0
    0x20 R_RISCV_SUB32 .L0  0x0
    0x25 R_RISCV_SET6 .L0  0x0
    0x25 R_RISCV_SUB6 .L0  0x0
    0x2B R_RISCV_SET6 .L0  0x0
    0x2B R_RISCV_SUB6 .L0  0x0
  }
]
```

读法：

- `0x4 R_RISCV_BRANCH .L0` 对应 `bnez` 的分支目标，目标符号是本地标签 `.L0`（基本块标签）。
- `0xC R_RISCV_JAL .L0` 对应 `j .LBB0_3`。
- `0xC R_RISCV_RELAX` 允许链接器在放宽条件满足时把 `j` 压缩/缩短。
- `0x1C R_RISCV_CALL_PLT g` 对应 `call g` 的 `auipc`+`jalr` 对；`PLT` 表示可能走过程链接表。
- `0x1C R_RISCV_RELAX` 允许链接器把 `auipc`+`jalr` 放松成 `jal`。
- `.rela.eh_frame` 里的 `R_RISCV_32_PCREL`、`ADD32`、`SUB32`、`SET6`、`SUB6` 是 CFI/异常帧的重定位，不是调用链的一部分。读重定位时先看 `.rela.text`，再看其他 section。

### 答案 7：三个解释题

**问题 1：`select` 为什么变成分支，而不是条件移动？**

没有 `+zicond` 时，RISC-V 没有一条通用的“条件选寄存器”指令。`Select_GPR_Using_CC_GPR` 在 isel 后只是一个伪指令；到 RA 后，它被展开成 `BEQ` + 两个分支。真分支计算 `MUL`，假分支放 `ADDI $x0, 7`，两路汇合。若启用 `+zicond`，后端可能选 `czero.eqz` 或类似序列，代码形状会不同；这属于特性相关的选指差异。

**问题 2：`call g` 为什么要 16 字节栈帧，`ra` 存在哪里？**

`kernel` 调用外部函数 `g`，`ra` 会被调用破坏，所以必须在调用前保存。RISC-V psABI 要求栈对齐 16 字节；保存一个 8 字节 `ra` 需要 16 字节的栈帧（`addi sp, sp, -16`），`ra` 存在 `8(sp)`，即 `%stack.0`，偏移 -8。返回前 `ld ra, 8(sp)` 恢复，再把 `sp` 加回 16。栈帧大小不是“8 字节刚好”，而是对齐后的 16。

**问题 3：对象文件里的 `auipc ra, 0` / `jalr ra` 为什么目标地址看起来是 0？**

对象文件还没有最终地址。`call g` 被编码成 `auipc ra, %pcrel_hi(g)` + `jalr ra, %pcrel_lo(...)` 的形式，两个立即数位置留了 0，并分别由 `R_RISCV_CALL_PLT` 与 `R_RISCV_PCREL_LO12_I` 这类重定位修补。`llvm-objdump` 在未链接的 `.o` 上只能显示占位值；链接器或汇编器填充后，`jalr` 才会指向 `g`。`R_RISCV_RELAX` 进一步允许链接器把这对指令放松成一条 `jal`。

### 评分示例

一份满分答案通常包含：

- 六段输出齐全，且来自同一条命令链（同一 `pa.ll`、同一 `-mattr`）。
- 能指出 `Select_GPR_Using_CC_GPR` 到 `BEQ` 的展开。
- 能指出 `%stack.0` 与 `sd ra, 8(sp)` 的对应关系。
- 能指出 `call g` 在 `.s` 与 `.o` 中的两种形态。
- 能解释 `.rela.text` 与 `.rela.eh_frame` 的分工。

一份 12-14 分的答案通常只贴了输出，漏掉 `PseudoCALL` 到 `auipc`+`jalr` 的对应，或者把 `.rela.eh_frame` 的重定位误当成调用重定位。

</details>

## 源码落点

- `RISCVInstrInfo.td`：`Select_GPR_Using_CC_GPR` 的选择模式（23.1.4 第 1697 行附近）；`PseudoCALL`、`PseudoRET`、`PseudoBR` 的定义。模式由 TableGen 生成匹配器，不在 C++ 里手写。
- `RISCVISelDAGToDAG.cpp`：DAG → 机器指令的选择器 `RISCVDAGToDAGISel`，负责自定义节点与复杂操作数（`selectSETCC` 等）；`Select_GPR_Using_CC_GPR` 本身由上面的 TableGen 模式选出。
- `RISCVISelLowering.cpp`：`LowerCall`、`LowerSELECT`、调用约定 lowering；`RISCVTargetLowering::EmitInstrWithCustomInserter` 把 `Select_GPR_Using_CC_GPR` 交给 `emitSelectPseudo`，后者生成 `BEQ` + 两个候选块 + `PHI`。这一步在 `finalize-isel` 执行，所以 isel 快照里还能看到伪指令，RA 快照里已经变成分支。
- `RISCVInstrInfo.cpp`：`copyPhysReg`、分支分析、`PseudoBR` 相关处理。
- `RISCVRegisterInfo.cpp`：`ra`/`s0` 的 spill/restore 与 callee-saved 处理。
- `RISCVFrameLowering.cpp`：16 字节栈帧、`frame-setup`/`frame-destroy`、CFI 指令。
- `RISCVExpandPseudoInsts.cpp`：`PseudoCALL`、`PseudoRET` 等伪指令的最终展开。
- `RISCVAsmPrinter.cpp`：MIR → 汇编文本，`.cfi_*`、`.attribute`、`.option`。
- `MCTargetDesc/RISCVMCCodeEmitter.cpp`：`call` 的 fixup 与 `R_RISCV_CALL_PLT` 相关编码。
- `MCTargetDesc/RISCVELFStreamer.cpp`：ELF 重定位与 relax 标记。

## 常见坑

1. **把 `.s` 里的 `call g` 当成最终编码**。`call` 是汇编伪指令，对象文件里会展开成 `auipc`+`jalr`，并带 `R_RISCV_CALL_PLT`。
2. **把 `objdump` 的 `bnez a2, 0x4` 当成分支错误**。未链接对象的分支目标是重定位占位值，不是最终地址。
3. **把 `.rela.eh_frame` 当成调用重定位**。调用重定位在 `.rela.text`；`.eh_frame` 是 CFI/异常帧。
4. **忽略 `-mattr` 的作用域**。`+m` 关掉后，乘法变成 `__muldi3`，select 的条件要额外保存到 `s0`，代码形状完全不同。
5. **把 16 字节栈帧解释成“保存 ra 需要 16 字节”**。保存 `ra` 实际占 8 字节；16 是 psABI 的栈对齐要求。
6. **把 `Select_GPR_Using_CC_GPR` 当成最终指令**。它是伪指令，RA 后才会展开成 `BEQ` + 分支。
7. **用 `-stop-after=greedy` 取 RA 后 MIR**。`greedy` 后还有 `virtregrewriter`；用 `-stop-after=prolog-epilog` 或 `-stop-before=riscv-copyelim`。

## 自我检查

- [ ] 我能把 `pa.ll` 的四类操作（`mul`、`select`、`call`、`add`）与最终汇编逐条对应。
- [ ] 我能指出 isel 后 MIR 里的 `Select_GPR_Using_CC_GPR`、`PseudoCALL`、`ADJCALLSTACKDOWN/UP`。
- [ ] 我能指出 RA 后 MIR 里的 `BEQ`、`%stack.0`、`csr_ilp32_lp64`、`frame-setup`/`frame-destroy`。
- [ ] 我能解释 `select` 为什么在无 `+zicond` 时变成分支。
- [ ] 我能解释 16 字节栈帧与 `ra` 保存位置的关系。
- [ ] 我能解释 `objdump` 里 `auipc ra, 0` / `jalr ra` 的目标为什么未解析。
- [ ] 我能列出 `.rela.text` 的五条重定位，并区分 `.rela.eh_frame`。
- [ ] 我能用评分表给自己的答案打分，并指出缺失的证据段。

## 延伸阅读

- LLVM 官方文档：`llvm.org/docs/MIRLangRef.html`，MIR 语法与机器内存操作数。
- LLVM 官方文档：`llvm.org/docs/CommandGuide/llvm-objdump.html`、`llvm-readobj.html`。
- RISC-V psABI：栈对齐、`ra` 保存、调用约定。
- RISC-V ELF psABI：`R_RISCV_CALL_PLT`、`R_RISCV_BRANCH`、`R_RISCV_JAL`、`R_RISCV_RELAX` 的语义。
- 同版本源码：`RISCVInstrInfo.td`、`RISCVISelLowering.cpp`、`RISCVFrameLowering.cpp`、`RISCVExpandPseudoInsts.cpp`、`RISCVAsmPrinter.cpp`。

[上一章：流水线与 MIR](./03-pipeline-and-mir.md) | [下一章：TableGen 语言](./04-tablegen-language.md)