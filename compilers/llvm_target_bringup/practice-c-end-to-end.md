# 练习 C — 端到端跑通一个程序

> **目标**：用同一份 LLVM IR 让 MiniRV 与 RISC-V 各生成一个 ELF，链接、运行、比较退出码，并解释静态指令数为什么不同。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`，链接器 `/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld`，模拟器 `/usr/bin/qemu-riscv64-static`。

## 背景与动机

前面章节的测试大多停在“`llc` 输出的汇编长什么样”。这只证明选指、寄存器分配和汇编打印各自看起来对，还没有证明**目标文件、重定位、链接布局、调用约定和进程退出**这一整条链真的闭合。一个后端最容易出问题的地方，恰恰是这些层之间的接缝：栈参数放在哪里、`main` 的返回值最后落在哪个寄存器、全局地址用哪种重定位、大栈帧会不会被拆成两条 `addi`。

端到端测试回答的是另一个问题：**把程序交给真实链接器和真实 CPU 模拟器，它还能不能算出同一个结果？**

MiniRV 没有自己的 libc、链接器和模拟器，也不需要造一套。MiniRV 的目标文件仍然是 `elf64-littleriscv`、`EM_RISCV`，指令编码也是 RV64IM 的子集，因此可以直接借 RISC-V 的 `ld.lld` 和 `qemu-riscv64-static` 来跑。参考后端 `riscv64` 在这里充当“语义 oracle”：同一份 IR，如果 MiniRV 和 RISC-V 都退出同一个码，至少说明两边的 ABI 级行为一致。

为什么不用 C？因为 C 前端会引入头文件、libc 调用、可变参数、浮点 ABI 等一大串变量。我们想把变量锁在“后端生成的机器码”这一层，所以直接写 LLVM IR；IR 里只允许出现 `main`、全局量、`alloca`、循环、调用和整数运算，不依赖任何外部符号。

仓库里的 `llvm/lib/Target/MiniRV/dev/e2e/run.sh` 已经用同样的模式跑了 17 个程序。本练习把它缩小到两个程序，但要求你把每个退出码和指令数都解释清楚，而不是只看脚本打印 `OK`。

## 先建立直觉

整条流水线只有四个观察点：

```
                 +-- llc -mtriple=minirv64 -O2 ----------> base.m.o --+
base.ll ---------+                                                       +-- ld.lld --no-relax --> base.m.elf --+
                 +-- llc -mtriple=riscv64 -mattr=+m -O2 --> base.r.o --+                                          +--> qemu --> exit
                                                                                                                  |
start.s -- llvm-mc -triple=riscv64 --> start.o ----------------------------------------------------------------+
```

`start.s` 是脚手架，不参与比较：

```asm
	.text
	.globl _start
_start:
	jal	ra, main          # a0 = main()
	addi	a7, zero, 93      # a7 = SYS_exit
	ecall                     # 以 a0 的低 8 位退出
```

它做三件事：调用 `main`、把 `main` 的返回值留在 `a0`、发 `exit` 系统调用。因此 `main` 返回 `i32` 的约定是否实现正确，会直接反映在 qemu 的退出码上。

为什么不比较 stdout？这个环境没有 libc；要打印就得自己发 `write` 系统调用、把整数转成十进制字符串，还要处理缓冲和换行。这些工作会把“后端是否正确”与“格式化代码是否正确”混在一起。退出码是进程边界上最小的可观察量：它只经过 `main` 的返回寄存器、`_start` 的 `ecall` 和 qemu 的退出状态，正好覆盖本练习想验证的 ABI 链。代价是它只有 8 位，所以期望码必须控制在 `0..255`。

四个观察点对应四个问题：

| 观察点 | 问题 | 失败时的典型层 |
|---|---|---|
| `llc` 两次都成功 | 选指/合法化/寄存器分配是否能处理这些 IR | CodeGen |
| `ld.lld` 两次都成功 | 重定位、符号、栈参数布局是否自洽 | relocation / ABI |
| 两个退出码相同 | 返回值寄存器和调用链是否一致 | ABI / frame |
| 指令数可解释 | 差异来自代码生成，而不是链接器偷偷改写 | codegen / link |

另外，`llc` 不会替你跑 IR 级的 inlining 或常量传播；这里的 `-O2` 是 codegen 优化级别。因此不要指望 `base.ll` 被折成一个 `ret 0`，循环和调用会保留到机器码里。

## 任务规格

### 输入

写两个 IR 程序，都定义 `i32 @main()`，都不引用 libc：

1. `base.ll`：覆盖**循环、调用、栈数组、全局量、i32/i64 边界**，成功时返回 `0`。
2. `stack9.ll`：覆盖**9 个整数参数、3200 字节大栈帧、全局量、循环、i64→i32 截断**，成功时返回 `42`。

`base.ll` 的期望值可以手算：

- 数组 `buf[i] = i * 5`，`i = 0..15`，和为 `5 * 120 = 600`。
- `mix(x, 3) = x * 3 + g_scale`，其中 `g_scale = 3`，所以总和是 `3 * 600 + 16 * 3 = 1848`。
- 加上 `g_bias = 7` 得到 `1855`。
- `1855 + (-1856) = -1`，即 i32 的 `0xFFFFFFFF`；它同时等于有符号的 `-1` 和无符号的 `4294967295`。

`stack9.ll` 的期望值：

- `sum9(1..9) = 45`，第 9 个参数走栈。
- 数组 `frame[i] = i + g_seed`，`i = 0..9`，和为 `45 + 10 * 11 = 155`。
- 两者相加为 `200`；成功返回 `42`，任何自检失败返回 `1`。

### 约束

- 只写 LLVM IR，不写 C；不使用内联汇编、异常、浮点、向量、TLS。
- `main` 必须自己定义，不能是 `declare`。
- 不使用 `printf`、`write`、`malloc` 等外部符号；不要依赖 `argc/argv`。
- 退出码保持在 `0..255`；这是后面要解释的 8 位截断边界。
- 两个目标都用 `-O2 -filetype=obj`；RISC-V 侧必须带 `-mattr=+m`。
- `start.s` 必须用 `-triple=riscv64` 汇编，不能交给 MiniRV 汇编器。
- 链接必须带 `-m elf64lriscv --no-relax`，两个目标各链接一次。

### 验收

1. 两个 `.ll` 都能被 `llc -mtriple=minirv64` 和 `llc -mtriple=riscv64 -mattr=+m` 编成目标文件。
2. 两个目标文件都能和同一个 `start.o` 链接成 ELF。
3. 在 qemu 下，`base` 的两个退出码都是 `0`，`stack9` 的两个退出码都是 `42`。
4. 用 `llvm-objdump -d` 记录静态指令数，并解释 MiniRV 多出来的指令来自哪里。
5. 至少验证一个大栈帧/9 参数的边界，以及一个故意写错期望值的失败路径。

## 期望输出

最终的核心结果应当长这样（下面数字是实测值，不是占位符）：

| 程序 | MiniRV 退出码 | riscv64 退出码 | MiniRV 指令数 | riscv64 指令数 |
|---|---:|---:|---:|---:|
| `base` | 0 | 0 | 47 | 39 |
| `stack9` | 42 | 42 | 67 | 59 |

```console
base     MiniRV=47  riscv64=39
stack9   MiniRV=67  riscv64=59
base minirv64 exit=0
base riscv64 exit=0
stack9 minirv64 exit=42
stack9 riscv64 exit=42
```

注意：**退出码相同不要求指令数相同**。本练习比较指令数是为了建立代码体积基线，而不是要求两个后端生成逐条一致的代码。

## 卡住了的提示

1. **`main` 的返回值怎么变成退出码？** 先看 `start.s`：`jal ra, main` 之后 `a0` 没有被改写，紧接着 `a7=93; ecall`。如果你的程序退出码是随机值，先检查 `main` 是否真的按 `i32` 返回，以及 `_start` 是否排在最前面。
2. **`stack9` 返回 1，但两个后端都一样。** 这通常不是后端 bug，而是你的期望值算错了。`0..9` 的和是 `45`，不是 `55`；`45 + 110 = 155`，不是 `165`。自检返回 `1` 恰好说明代码路径跑通了。
3. **`llvm-objdump` 把 `mul` 显示成 `<unknown>`。** MiniRV 目标文件不带 `+m` 的架构属性，默认按 RV64I 解码就会这样。加 `--triple=minirv64`，或显式 `--mattr=+m`。
4. **链接报 `undefined symbol: __muldi3`。** 你忘了给 RISC-V 侧加 `-mattr=+m`。generic `riscv64` 默认没有 M 扩展，`mul i64` 会被降成编译器运行时调用。
5. **`stack9` 的 prologue 有两条 `addi sp, sp, -...`。** 这是大栈帧的有意拆分：12 位立即数装不下 3200 多字节，MiniRV 和 RISC-V 都会拆成两段，以便中间保存 callee-saved 寄存器。

## 分段实作步骤

### Step 1：搭一个一次性的实验目录

不要把实验文件写进仓库；用 `/tmp/practice-c`，并在开头固定所有工具路径：

```console
$ mkdir -p /tmp/practice-c && cd /tmp/practice-c
$ export TREE=/home/ubuntu/llvm/trees/dsl-tablegen
$ export B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ export LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld
$ export QEMU=/usr/bin/qemu-riscv64-static
```

把 `start.s` 抄进来。它故意只用标准 RISC-V 的别名和 `ecall`，因为 MiniRV 汇编器只实现自己的指令子集。

### Step 2：写 `base.ll`

按“数据 → 填充 → 归约 → 自检”四段写：

- 数据段：`@g_scale: i64 = 3`、`@g_bias: i32 = 7`、`%buf = alloca [16 x i64]`。
- 填充循环：`buf[i] = i * 5`，用 `phi` 和 `icmp ult` 控制。
- 归约循环：对每个元素调用 `@mix`，累加到 i64。
- 自检：把 i64 结果 `trunc` 到 i32，加 `g_bias`，再分别用有符号 `-1` 和无符号 `4294967295` 比较同一个 i32 位型；全部成立才返回 `0`。

这里故意保留了一个 i32/i64 的接缝：`acc` 是 i64，`g_bias` 是 i32，`trunc` 之后的信息量变小；这正是后端最容易在符号扩展上出错的边界。

### Step 3：写 `stack9.ll`

在 `base.ll` 的结构上加两个压力点：

- `define i64 @sum9(i64 %a, ..., i64 %i)` 九个 i64 参数。按 RISC-V ABI，前八个进 `a0..a7`，第九个放在调用者的 `0(sp)`。
- `%frame = alloca [400 x i64]`，3200 字节，强迫后端处理超出 12 位立即数范围的栈调整。

自检仍然返回小整数：成功 `42`，失败 `1`。这样你可以用退出码区分“逻辑失败”和“进程崩溃/链接失败”。

### Step 4：双编译、汇编脚手架、链接

```console
$ $B/llvm-mc -triple=riscv64 -filetype=obj start.s -o start.o

$ for n in base stack9; do
>   $B/llc -mtriple=minirv64 -O2 -filetype=obj $n.ll -o $n.m.o
>   $B/llc -mtriple=riscv64 -mattr=+m -O2 -filetype=obj $n.ll -o $n.r.o
>   $LDLLD -m elf64lriscv --no-relax start.o $n.m.o -o $n.m.elf
>   $LDLLD -m elf64lriscv --no-relax start.o $n.r.o -o $n.r.elf
> done
```

如果这一步有任何一个命令非零退出，先不要跑 qemu。链接错误通常已经把问题定位到了重定位或 ABI 层。

### Step 5：运行并记录指令数

```console
$ for n in base stack9; do
>   $QEMU $n.m.elf; echo "$n minirv64 exit=$?"
>   $QEMU $n.r.elf; echo "$n riscv64 exit=$?"
> done
```

再统计静态指令数：

```console
$ for n in base stack9; do
>   printf '%-8s MiniRV=%s  riscv64=%s\n' "$n" \
>     "$($B/llvm-objdump -d --triple=minirv64 $n.m.o | grep -cE '^[[:space:]]+[0-9a-f]+:')" \
>     "$($B/llvm-objdump -d --triple=minirv64 $n.r.o | grep -cE '^[[:space:]]+[0-9a-f]+:')"
> done
```

只要退出码对、指令数记录在案，就可以进入参考解答对照。

## 完整参考解答

<details>
<summary>点开看完整 .ll、命令、真实输出与“为什么一致”的解释</summary>

### 1. `start.s`

```asm
	.text
	.globl _start
_start:
	jal	ra, main
	addi	a7, zero, 93
	ecall
```

### 2. `base.ll`

```llvm
; base.ll — 回环 + 调用 + 栈数组 + 全局量 + i32/i64 边界，成功返回 0。
target triple = "minirv64"

@g_scale = global i64 3
@g_bias  = global i32 7

define i64 @mix(i64 %x, i64 %y) {
  %m = mul i64 %x, %y
  %g = load i64, ptr @g_scale
  %r = add i64 %m, %g
  ret i64 %r
}

define i32 @main() {
entry:
  %buf = alloca [16 x i64], align 8
  br label %fill
fill:
  %i  = phi i64 [ 0, %entry ], [ %i1, %fill ]
  %p  = getelementptr inbounds [16 x i64], ptr %buf, i64 0, i64 %i
  %v  = mul i64 %i, 5
  store i64 %v, ptr %p
  %i1 = add i64 %i, 1
  %c  = icmp ult i64 %i1, 16
  br i1 %c, label %fill, label %sum
sum:
  br label %loop
loop:
  %j    = phi i64 [ 0, %sum ], [ %j1, %loop ]
  %acc  = phi i64 [ 0, %sum ], [ %acc1, %loop ]
  %q    = getelementptr inbounds [16 x i64], ptr %buf, i64 0, i64 %j
  %w    = load i64, ptr %q
  %m    = call i64 @mix(i64 %w, i64 3)
  %acc1 = add i64 %acc, %m
  %j1   = add i64 %j, 1
  %c2   = icmp ult i64 %j1, 16
  br i1 %c2, label %loop, label %done
done:
  %b    = load i32, ptr @g_bias
  %t    = trunc i64 %acc1 to i32          ; i64 -> i32：这里安全，值小于 2^31
  %e    = add i32 %t, %b                  ; 1855
  %neg  = add i32 %e, -1856               ; i32 的 -1，即 0xFFFFFFFF
  %ok64 = icmp eq i64 %acc1, 1848
  %ok32 = icmp eq i32 %e, 1855
  %okn  = icmp eq i32 %neg, -1            ; 有符号视角
  %oknu = icmp eq i32 %neg, 4294967295    ; 无符号视角：同一个位型
  %a1   = and i1 %ok64, %ok32
  %a2   = and i1 %okn, %oknu
  %ok   = and i1 %a1, %a2
  %r    = select i1 %ok, i32 0, i32 1
  ret i32 %r
}
```

### 3. `stack9.ll`

```llvm
; stack9.ll — 9 个参数 + 3200 字节大栈帧 + 全局量 + 回环，成功返回 42。
target triple = "minirv64"

@g_seed = global i64 11

define i64 @sum9(i64 %a, i64 %b, i64 %c, i64 %d, i64 %e,
                 i64 %f, i64 %g, i64 %h, i64 %i) {
  %s1 = add i64 %a, %b
  %s2 = add i64 %c, %d
  %s3 = add i64 %e, %f
  %s4 = add i64 %g, %h
  %s5 = add i64 %s1, %s2
  %s6 = add i64 %s3, %s4
  %s7 = add i64 %s5, %s6
  %r  = add i64 %s7, %i
  ret i64 %r
}

define i32 @main() {
entry:
  %frame = alloca [400 x i64], align 8     ; 3200 字节，超过 12 位立即数范围
  %seed  = load i64, ptr @g_seed           ; 11
  %s     = call i64 @sum9(i64 1, i64 2, i64 3, i64 4, i64 5,
                          i64 6, i64 7, i64 8, i64 9)   ; 45，第 9 个走栈
  br label %fill
fill:
  %i  = phi i64 [ 0, %entry ], [ %i1, %fill ]
  %p  = getelementptr inbounds [400 x i64], ptr %frame, i64 0, i64 %i
  %v  = add i64 %i, %seed
  store i64 %v, ptr %p
  %i1 = add i64 %i, 1
  %c  = icmp ult i64 %i1, 10
  br i1 %c, label %fill, label %reduce
reduce:
  br label %loop
loop:
  %j    = phi i64 [ 0, %reduce ], [ %j1, %loop ]
  %acc  = phi i64 [ 0, %reduce ], [ %acc1, %loop ]
  %q    = getelementptr inbounds [400 x i64], ptr %frame, i64 0, i64 %j
  %w    = load i64, ptr %q
  %acc1 = add i64 %acc, %w
  %j1   = add i64 %j, 1
  %c2   = icmp ult i64 %j1, 10
  br i1 %c2, label %loop, label %done
done:
  ; 0..9 各加 11：45 + 110 = 155；再加 sum9 = 200
  %wide = add i64 %s, %acc1
  %lo   = trunc i64 %wide to i32          ; 200，i64 -> i32 截断
  %hi   = lshr i64 %wide, 32              ; 高 32 位必须为 0
  %oklo = icmp eq i32 %lo, 200
  %okhi = icmp eq i64 %hi, 0
  %okw  = icmp eq i64 %wide, 200
  %a1   = and i1 %oklo, %okhi
  %ok   = and i1 %a1, %okw
  %r    = select i1 %ok, i32 42, i32 1
  ret i32 %r
}
```

### 4. 完整命令

```console
$ mkdir -p /tmp/practice-c && cd /tmp/practice-c
$ export B=/home/ubuntu/llvm/builds/dsl-tablegen/bin
$ export LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld
$ export QEMU=/usr/bin/qemu-riscv64-static

$ $B/llvm-mc -triple=riscv64 -filetype=obj start.s -o start.o
$ for n in base stack9; do
>   $B/llc -mtriple=minirv64 -O2 -filetype=obj $n.ll -o $n.m.o
>   $B/llc -mtriple=riscv64 -mattr=+m -O2 -filetype=obj $n.ll -o $n.r.o
>   $LDLLD -m elf64lriscv --no-relax start.o $n.m.o -o $n.m.elf
>   $LDLLD -m elf64lriscv --no-relax start.o $n.r.o -o $n.r.elf
> done
```

### 5. 真实输出

先确认输出文件的机器号。MiniRV 没有发明新的 ELF machine：

```console
$ $B/llvm-readobj -h base.m.o | grep -E 'Format|Arch|Machine'
Format: elf64-littleriscv
Arch: riscv64
  Machine: EM_RISCV (0xF3)
```

这正是它能被 `ld.lld -m elf64lriscv` 和 qemu 接受的原因。接下来运行：

```console
$ for n in base stack9; do
>   $QEMU $n.m.elf; echo "$n minirv64 exit=$?"
>   $QEMU $n.r.elf; echo "$n riscv64 exit=$?"
> done
base minirv64 exit=0
base riscv64 exit=0
stack9 minirv64 exit=42
stack9 riscv64 exit=42
```

静态指令数：

```console
$ for n in base stack9; do
>   printf '%-8s MiniRV=%s  riscv64=%s\n' "$n" \
>     "$($B/llvm-objdump -d --triple=minirv64 $n.m.o | grep -cE '^[[:space:]]+[0-9a-f]+:')" \
>     "$($B/llvm-objdump -d --triple=minirv64 $n.r.o | grep -cE '^[[:space:]]+[0-9a-f]+:')"
> done
base     MiniRV=47  riscv64=39
stack9   MiniRV=67  riscv64=59
```

`stack9` 的 prologue 和调用点能直接看到两个边界：3200 多字节栈帧被拆成 `-2032` 和 `-1200` 两段；第 9 个参数在设置 `a0..a7` 之前先存到 `0(sp)`：

```console
$ $B/llvm-objdump -d --triple=minirv64 stack9.m.o | sed -n '/<main>:/,/jal/p'
0000000000000028 <main>:
      28: 13 01 01 81  	addi	x2, x2, -2032
      2c: 23 34 11 7e  	sd	x1, 2024(x2)
      30: 23 30 81 7e  	sd	x8, 2016(x2)
      34: 13 01 01 b5  	addi	x2, x2, -1200
      38: 97 01 00 00  	auipc	x3, 0
      3c: 93 81 01 00  	addi	x3, x3, 0
      40: 03 b4 01 00  	ld	x8, 0(x3)
      44: 93 01 90 00  	addi	x3, x0, 9
      48: 23 30 31 00  	sd	x3, 0(x2)
      4c: 13 05 10 00  	addi	x10, x0, 1
      50: 93 05 20 00  	addi	x11, x0, 2
      54: 13 06 30 00  	addi	x12, x0, 3
      58: 93 06 40 00  	addi	x13, x0, 4
      5c: 13 07 50 00  	addi	x14, x0, 5
      60: 93 07 60 00  	addi	x15, x0, 6
      64: 13 08 70 00  	addi	x16, x0, 7
      68: 93 08 80 00  	addi	x17, x0, 8
      6c: ef 00 00 00  	jal	x1, 0
```

重定位表也能说明两边选择的地址模型不同：

```console
$ $B/llvm-readobj -r base.m.o | sed -n '/Relocations \[/,/^\]/p'
Relocations [
  Section (3) .rela.text {
    0x4 R_RISCV_PCREL_HI20 g_scale 0x0
    0x8 R_RISCV_PCREL_LO12_I .L0  0x0
    0x64 R_RISCV_JAL mix 0x0
    0x74 R_RISCV_PCREL_HI20 g_bias 0x0
    0x78 R_RISCV_PCREL_LO12_I .L0  0x0
  }
  Section (7) .rela.eh_frame {
    0x1C R_RISCV_32_PCREL .L0  0x0
    0x30 R_RISCV_32_PCREL .L0  0x0
  }
]
```

MiniRV 走 `PCREL_HI20/PCREL_LO12_I` 对，调用是 `JAL`。RISC-V 参考对象则走另一套地址模型：

```console
$ $B/llvm-readobj -r base.r.o | grep -E 'HI20|LO12|CALL_PLT'
    0x4 R_RISCV_HI20 g_scale 0x0
    0x8 R_RISCV_LO12_I g_scale 0x0
    0x54 R_RISCV_CALL_PLT mix 0x0
    0x68 R_RISCV_HI20 g_bias 0x0
    0x6C R_RISCV_LO12_I g_bias 0x0
```

两种写法都能被同一个链接器解析，但生成的指令序列不同：MiniRV 用 `auipc+addi` 构造 PC 相对地址，RISC-V 用 `lui` 加 `LO12`。

### 6. 为什么退出码一致

两边共享同一套 RISC-V psABI 级约定：

- `MiniRVCallingConv.td` 的 `CC_MiniRV` 把前八个 i64 参数放在 `x10..x17`，其余放栈，i64 返回值放 `x10`。这与 RISC-V 的整数调用约定在本练习用到的子集上一致。
- `MiniRVFrameLowering::getFirstSPAdjustAmount` 和 `emitPrologue` 负责把大栈帧拆成两段，并在中间保存 `ra`/callee-saved 寄存器；`RISCVFrameLowering` 同名逻辑给出相同的外部可观察布局。
- 两者输出的 ELF 都是 `elf64-littleriscv`、`EM_RISCV`，`start.o` 里的 `jal`/`ecall` 对两者一视同仁。
- 程序只使用 RV64IM 的整数指令和内存指令；MiniRV 没有引入不同的算术语义。

所以 `main` 的返回值最终都落在 `a0`，`_start` 都用同一个 `ecall` 退出。指令选择可以不同，**可观察结果必须相同**。

### 7. 为什么指令数不同

`base` 的 `main` 是 41 条（MiniRV）对 34 条（RISC-V），`mix` 是 6 对 5；`stack9` 的 `sum9` 两边都是 10 条，`main` 是 57 对 49。逐项拆开，MiniRV 多出来的部分主要来自：

| 来源 | MiniRV | RISC-V | 差值 |
|---|---|---|---:|
| 循环结束指针占用 `s3`，多一对 save/restore | `sd/ld x19` | 复用已有的 callee-saved 寄存器 | +2 |
| 循环不变量 `3` 提升到 `s0`，循环内再 `mv` | `addi + mv` | 循环内重新 `li` | +1 |
| 常量比较用 `addi` 物化再 `xor` | `addi + xor` ×2 | `xori` 立即数 ×2 | +2 |
| 两个条件分别 `sltu` 再 `or` | 两条 `sltu` | 合并成一条 | +1 |
| 全局地址用 `auipc+addi` 对 | 3 条（含 load） | `lui+load` 2 条 | +1 |
| 直接调用用 `jal` | 1 条 | `auipc+jalr` 2 条 | −1 |

合计约 `+7`，与 `41 - 34 = 7` 对得上。`stack9` 的差值 `+8` 来自同一组原因，外加更多地址/比较常量的物化；它的 `sum9` 是纯算术叶函数，所以两边都是 10 条。

这说明指令数差异是**后端成熟度**的度量，不是正确性判据。RISC-V 后端有更多合并立即数、复用寄存器的优化；MiniRV 先保证语义正确，再逐步把这些缺口补上。

### 8. 四个命令/设计选择

| 选择 | 不这样做会怎样 | 它固定了什么 |
|---|---|---|
| RISC-V 侧加 `-mattr=+m` | generic `riscv64` 没有 M 扩展，`mul i64` 降成 `__muldi3`，链接报未定义符号 | 参考侧与 MiniRV 都按 RV64IM 生成 |
| 链接加 `--no-relax` | 链接器可能按 `R_RISCV_RELAX` 记录删除/改写指令；本次对象还没有 RELAX，所以暂时看不出差异 | “静态指令数 = `llc` 输出”，两边使用同一链接策略 |
| `start.s` 用 `-triple=riscv64` 汇编 | MiniRV 汇编器不认 `ra/a7/zero`，也没有 `ecall`，脚手架直接编不过 | 脚手架不是被测变量，问题只可能出在 `main` 的目标文件 |
| 比较退出码而不是 stdout | 需要自己发 `write`、做整数格式化，输出还会受缓冲和 qemu 版本影响 | 只测 `main` 返回寄存器到进程退出状态这条 ABI 链 |

### 9. 三列对照

| 层 | RISC-V 参考 | MiniRV | 你的目标 |
|---|---|---|---|
| 整数参数 | `CC_RISCV`：前八个进 `a0..a7`，其余走栈 | `CC_MiniRV`：同样前八个进 `x10..x17`，其余走栈 | 先对齐 psABI，再谈优化 |
| 大栈帧 | `RISCVFrameLowering::getFirstSPAdjustAmount` 负责拆分 | `MiniRVFrameLowering::getFirstSPAdjustAmount` 做同样的事 | 12 位立即数装不下时拆两段 |
| 全局地址 | `HI20/LO12_I` 加 `CALL_PLT` | `PCREL_HI20/PCREL_LO12_I` 加 `JAL` | 选地址模型，并在 MC 层发出对应重定位 |
| ELF | `EM_RISCV` / `elf64-littleriscv` | 复用同一个 machine 与格式 | 能复用就复用，别发明新格式 |

</details>

## 测试与边界

### 边界 1：成功路径返回 0

`base` 的所有自检成立时返回 `0`。这验证了 i64 累加、i32 截断、`g_bias` 的 i32 加载，以及 i32 的 `-1 == 4294967295` 位型判断。

### 边界 2：成功路径返回非零

`stack9` 成功返回 `42`。非零退出码同样能被 qemu 如实传回，说明不是“只有 0 才能跑通”的假象。

### 边界 3：自检失败

把 `base.ll` 里的 `1855` 故意改成 `1854`，重新编译链接：

```console
$ sed 's/icmp eq i32 %e, 1855/icmp eq i32 %e, 1854/' base.ll > wrong.ll
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj wrong.ll -o wrong.m.o
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 -filetype=obj wrong.ll -o wrong.r.o
$ $LDLLD -m elf64lriscv --no-relax start.o wrong.m.o -o wrong.m.elf
$ $LDLLD -m elf64lriscv --no-relax start.o wrong.r.o -o wrong.r.elf
$ $QEMU wrong.m.elf; echo "wrong minirv64 exit=$?"
wrong minirv64 exit=1
$ $QEMU wrong.r.elf; echo "wrong riscv64 exit=$?"
wrong riscv64 exit=1
```

两边都返回 `1`。这提醒我们：**差分测试只能发现“两个后端不一致”，不能发现“两个后端一起错”**。所以自检的期望值必须自己算对，或者再引入一个独立 oracle（例如在 `lli` 能跑的 IR 子集上用解释器交叉验证）。

### 边界 4：退出码只保留低 8 位

`main` 返回 `i32`，但 `exit` 状态在 POSIX/qemu 层面只有 8 位：

```console
$ cat > wrap.ll <<'LL'
target triple = "minirv64"
define i32 @main() {
  ret i32 256
}
LL
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj wrap.ll -o wrap.m.o
$ $LDLLD -m elf64lriscv --no-relax start.o wrap.m.o -o wrap.m.elf
$ $QEMU wrap.m.elf; echo "exit=$?"
exit=0
```

返回 `256` 观察到 `0`；返回 `-1` 则观察到 `255`。因此本练习的所有期望码都控制在 `0..255`，并且不要把“退出码相同”误当成“完整的 32 位返回值相同”。

### 边界 5：大栈帧与栈参数

`stack9` 同时覆盖两条最容易断的路径：

- 栈帧 `3232` 字节（`400 * 8` 加对齐/保存区），超过 12 位有符号立即数的 `2047` 上限，必须拆成两条 `addi`。
- 第 9 个 i64 参数不在寄存器里，而是在 `jal` 之前写入 `0(sp)`；被调用方从 `0(sp)` 读取。

如果这两个边界之一错了，通常表现为 qemu 直接崩溃、返回随机值，或者 `stack9` 在 MiniRV/RISC-V 上给出不同的退出码。

## 故障排查表

先按“症状”定位层，再只跑对应的一条命令；不要一上来就改 CodeGen。

| 症状 | 最可能层 | 先用哪条命令定位 |
|---|---|---|
| `base` 两边都是 0，但 `stack9` 两边退出码不同 | ABI（栈参数/对齐） | `$B/llvm-objdump -d --triple=minirv64 stack9.m.o`，对照 `sum9` 参数从 `0(sp)` 读取的位置 |
| qemu 直接崩溃或返回随机值 | frame（prologue/epilogue/sp） | `$B/llc -mtriple=minirv64 -O2 -print-after=prolog-epilog stack9.ll -o /dev/null` |
| 链接报 `relocation R_RISCV_JAL out of range` | branch range | `$B/llvm-readobj -r f.o` 找 `R_RISCV_JAL`，再看长函数的分支距离；必要时用 `-O2` 重编 |
| 链接报 `undefined symbol: __muldi3` | ABI / libcall（缺 M 扩展） | 给 RISC-V 侧补 `-mattr=+m` 后重链；`$B/llc -mtriple=riscv64 -mattr=+m ...` |
| 链接报 `undefined symbol: main` 或找不到 `_start` | link（目标文件/顺序/入口） | `$B/llvm-readobj --symbols start.o` 确认 `_start`；确认 `start.o` 在命令行里 |
| `llvm-objdump` 显示 `<unknown>` | relocation/decode（不是链接层） | `$B/llvm-objdump -d --triple=minirv64 f.o`，或加 `--mattr=+m` |
| 两条 `addi sp, sp, -...` 看起来像重复调整 | frame（大栈帧拆分） | 看 `MiniRVFrameLowering::getFirstSPAdjustAmount`，再用 `-print-after=prolog-epilog` 对照 |
| 源码没改，指令数却变了 | codegen vs link | 用 `diff` 比较两次 `$B/llvm-objdump -d` 的输出；检查重定位里是否出现 `R_RISCV_RELAX` |

## 反直觉与踩雷

1. **错误直觉**：MiniRV 是“新目标”，应该有新的 ELF machine、新的链接器和新的模拟器。  
   **正确认识**：MiniRV 复用 RISC-V 的 ELF 机器号和 RV64IM 编码，`llvm-readobj -h` 显示 `EM_RISCV`，所以同一个 `ld.lld` 和 qemu 就能跑。它新的是**代码生成后端**，不是二进制生态。

2. **错误直觉**：退出码一样，说明两边生成的指令序列一样。  
   **正确认识**：`base` 的 MiniRV 47 条、RISC-V 39 条，`stack9` 是 67 对 59。退出码只证明可观察语义一致，指令数差异反映的是寄存器分配、立即数合并和地址模型的成熟度差异。

3. **错误直觉**：`llvm-objdump -d base.m.o` 能直接反汇编 MiniRV 的 M 扩展指令。  
   **正确认识**：MiniRV 目标文件不带 `+m` 架构属性，默认按 RV64I 解码会把 `mul` 打成 `<unknown>`。用 `--triple=minirv64` 或 `--mattr=+m` 才能得到 `mul x3, x10, x11`。

4. **错误直觉**：`start.s` 也是 RISC-V 指令，交给 MiniRV 汇编器应该也行。  
   **正确认识**：MiniRV 的汇编器只实现自己的指令子集，不认 `ra`、`a7`、`zero` 这些 ABI 别名，也没有 `ecall`。脚手架必须用 `-triple=riscv64` 汇编。

5. **错误直觉**：`--no-relax` 只是保守选项，去掉也无所谓。  
   **正确认识**：本次两个对象都没有 `R_RISCV_RELAX`，去掉确实也能跑；但固定 `--no-relax` 让“指令数 = llc 输出”成为实验不变量。否则未来 MC 层一旦开始发 RELAX 记录，链接器可能删除/改写指令，基线会在没人改 CodeGen 的情况下漂移。

## 加分挑战

1. 再写一个 10 个 i64 参数的程序，让第 9、10 个参数都走栈，并把两者相加后返回 `0`；用 `llvm-objdump` 确认两个 `sd` 都落在 `0(sp)` 和 `8(sp)`。
2. 让 `main` 返回 `255` 和 `-1` 两个版本，确认 qemu 都观察为 `255`；再返回 `256`，确认观察到 `0`，把 8 位截断写成表格。
3. 对同一个 `base.ll` 比较 `-O0` 与 `-O2` 的静态指令数，找出多出来的 spill/reload 集中在哪个函数。
4. 去掉 `--no-relax` 重新链接，逐字节比较两次的 `.text`，确认本次实验里链接器没有改写代码；再解释为什么未来不能依赖这个巧合。
5. 写一个超过 4 KiB 的函数，故意让某个条件分支落到 `R_RISCV_JAL` 的范围之外，观察链接器报错，并回到 `MiniRVInstrInfo` 的分支分析/`f597d37` 的 relax 提交去理解修复思路。
6. 把两个程序的退出码和指令数写进一个 shell 脚本，任何一项漂移就 `exit 1`；这就是最小可用的 code-size 回归门禁。

## 本章重点整理

- 端到端测试的闭环是：`llc` → 目标文件 → `ld.lld` → qemu → 退出码；缺任何一环都不能声称“后端能跑程序”。
- MiniRV 复用 RISC-V 的 ELF 机器号、RV64IM 编码、`ld.lld` 和 qemu，因此差分测试的参考后端可以直接是 `riscv64`。
- `-mattr=+m` 让 RISC-V 参考侧使用硬件乘除，避免 `__muldi3` 这类运行时调用；MiniRV 本身就是 RV64IM。
- `start.s` 用 `riscv64` 汇编，是因为 MiniRV 汇编器不提供 `ecall` 和 ABI 别名；脚手架不属于被测对象。
- 退出码比较测的是 ABI 级可观察行为；静态指令数比较测的是代码生成质量，两者不能互相替代。
- 大栈帧会被拆成多条 `addi`，第 9 个整数参数会落到 `0(sp)`；这两个边界最容易暴露 frame/ABI 层的问题。

## 自我检核

- [ ] 不看笔记，画出 `base.ll` 到 qemu 退出码的完整流水线，并标出 `start.o` 插入的位置。
- [ ] 解释为什么 `main` 返回 `i32` 最终只影响退出码的低 8 位；返回 `256` 时 qemu 看到什么？
- [ ] 解释 `-mattr=+m` 缺失时，RISC-V 侧为什么会链接出 `__muldi3`，而 MiniRV 侧不会。
- [ ] 解释 `--no-relax` 在本次实验中是否改变了结果；它真正固定的是什么不变量？
- [ ] 给定 `base` 的 47 vs 39，至少说出三条 MiniRV 多出指令的具体来源。
- [ ] 如果 `stack9` 在 MiniRV 上返回 42、在 RISC-V 上返回 1，你会先查 ABI、frame 还是链接层？给出两条命令。

## 延伸阅读

- **[RISC-V 后端全貌](../riscv_backend/README.md)** — 本仓库的 RISC-V 后端课程
  - **读哪里**：调用约定、frame lowering、`RISCVISelLowering::LowerCall` 与 `RISCVFrameLowering::emitPrologue` 相关章节。
  - **能学到什么**：为什么参考后端会多用 `xori`、`lui`、`CALL_PLT`，以及这些选择如何影响指令数。
  - **前提**：先完成本练习，能看懂 `llvm-objdump` 的整数指令序列。

- **[ELF 与链接](../elf_linking/README.md)** — 本仓库的 ELF/重定位课程
  - **读哪里**：RISC-V 的 `HI20/LO12`、`PCREL_HI20/PCREL_LO12`、`JAL`、`CALL_PLT` 重定位，以及链接器 relaxation 的触发条件。
  - **能学到什么**：为什么 `--no-relax` 能固定指令流，以及 `R_RISCV_RELAX` 缺失时链接器为什么不会改写代码。
  - **前提**：知道重定位表是什么；可以先跑本练习的 `llvm-readobj -r`。

- **[LLVM ISel 与调度（RISC-V）](../llvm_isel_sched_riscv/README.md)** — 本仓库的选指/调度课程
  - **读哪里**：SelectionDAG 的常量折叠、立即数合并、寄存器分配对指令数的影响。
  - **能学到什么**：把本练习里“为什么 MiniRV 多 8 条”从现象追到具体的选择与调度决策。
  - **前提**：能读懂 `llc -stop-after=finalize-isel` 的输出。

- **[lit 与 FileCheck](../llvm_lit/README.md)** — 本仓库的测试基础设施课程
  - **读哪里**：如何把端到端脚本、退出码和指令数基线接进 `llvm-lit`。
  - **能学到什么**：把本练习的手工命令变成可重复的 CI 门禁。
  - **前提**：完成本练习并保存好两份 `.ll` 与 `start.s`。

→ [练习 D — 用 CodeGen 回归测试锁住后端行为](./practice-d-codegen-regression.md)
