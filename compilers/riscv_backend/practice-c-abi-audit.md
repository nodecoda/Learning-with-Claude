# 练习 C：ABI 与栈帧审计

> 目标：给定六个函数签名，用 `clang -S` 和 `llc` 产出真实汇编，填出一张“参数落在哪、栈上放了什么、谁负责回收”的审计表。本练习不要求你背 ABI 条文，要求你能从寄存器名、栈偏移和 prologue/epilogue 里读出 ABI 行为。

> 环境：Windows 10 x86_64；LLVM 23.1.4；`clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d`。统一入口是 `compilers/riscv_backend/code/tools/run.ps1`。本练习的汇编全部由本机重新编译得到；需要重新构建 `llc` 才能验证的改动会标 `guided`。

## 为什么

ABI 是“编译器之间的合同”，但它在源文件里几乎看不见。C 代码只写 `long f(long a, ...)`，真正决定参数去 `a0`、`fa0` 还是 `0(sp)` 的，是 `RISCVCallingConv.cpp` 里的 `CC_RISCV`，以及 `RISCVISelLowering.cpp` 里对 `byval`、`sret`、`vararg` 的处理。只看 C 代码，很容易把“形参”与“寄存器/栈槽”混为一谈。

更常见的问题是“谁回收栈”。RISC-V 采用调用者清理（caller-cleans）的约定：调用者在调用前把栈参数放到 `sp` 指向的区域，调用返回后自己恢复 `sp`。被调用者只读取调用者留下的栈参数，不负责弹出它们。把这条搞反，会在手写汇编或调试栈破坏时浪费大量时间。

本练习用六个必做签名覆盖：多个整数、超过 8 个参数、大结构体、`sret`、浮点混合、变参；另用小结构体和手写 `byval` 作对照。每个签名都要求你填三列：参数位置、栈上内容、回收责任。答案不是“看起来像”，而是从汇编里逐条核对出来的。

## 主线

### C.1 六个签名与观察点

练习使用下面六个函数。为了便于对照，函数名就是签名类型：

```cpp
struct Small { long a; long b; };
struct Big { long a; long b; long c; long d; };

long ints8(long a, long b, long c, long d, long e, long f, long g, long h);
long ints9(long a, long b, long c, long d, long e, long f, long g, long h, long i);
long take_big(struct Big s);
struct Big make_big(long x);
double mixfp(float a, float b, double c, double d, float e);
long sumv(int n, ...);
```

六个签名分别观察：

| 签名 | 观察点 | 预期涉及 |
| --- | --- | --- |
| `ints8` | 8 个整数是否刚好用满 `a0`–`a7` | 整数参数寄存器 |
| `ints9` | 第 9 个参数去哪里 | 栈参数、调用者清理 |
| `take_big` | 32 字节结构体是否按值放栈 | 间接传递、调用者副本 |
| `make_big` | 返回值是否占参数寄存器 | `sret` 隐藏指针 |
| `mixfp` | `float`/`double` 是否共用 `fa0`–`fa7` | 浮点参数寄存器 |
| `sumv` | 变参的寄存器保存区与 `va_list` | varargs、`sp` 布局 |

### C.2 LP64D 的参数落点规则

在 `lp64d` 下，整数参数依次使用 `a0`–`a7`（`x10`–`x17`），浮点参数依次使用 `fa0`–`fa7`（`f10`–`f17`）。两类寄存器各有一套，不互相占用。整数寄存器用完之后，后续整数参数放到栈上；浮点寄存器用完之后，后续浮点参数也放到栈上，但栈槽仍按 8 字节对齐。

聚合体的规则更细。小于等于 2 个 XLEN 的聚合体会被拆成若干整数块，放进 `a0`、`a1`；大于 2 个 XLEN 的聚合体不再按值拆块，而是由调用者准备一份副本，把副本地址放进 `a0`，被调用者通过这个指针访问。`struct Small { long, long }` 是 16 字节，正好两个 XLEN；`struct Big { long, long, long, long }` 是 32 字节，超过两个 XLEN，走间接路径。

`sret` 是另一种隐藏参数：返回大结构体的函数，调用者先分配结果对象，把结果对象地址作为第 0 个参数（`a0`）传入；真正的用户参数从 `a1` 开始。被调用者把结果写进 `a0` 指向的对象，返回值通常仍把 `a0` 返回，便于链式调用。

变参有两条额外规则。第一，变参部分不使用浮点寄存器（即使 ABI 是 `lp64d`），因为调用者不知道被调用者会用哪种类型读取；第二，被调用者需要一块“寄存器保存区”，把 `a1`–`a7` 等可能被 `va_arg` 读取的寄存器先存到栈上，`va_list` 指向这块区域和后续栈参数。`sumv` 的 112 字节帧就是这件事的现场。

源码依据：`RISCVCallingConv.cpp` 的 `RISCV::getArgGPRs` 返回 `X10`–`X17`（`ILP32E`/`LP64E` 只返回 `X10`–`X15`）；`getArgFPRs` 在软浮点 ABI 或没有 `F` 扩展时返回空；`CC_RISCV_Impl` 里处理 `f16`/`f32`/`f64` 的 FPR 分配、变参的寄存器对齐、以及“超过 2*XLEN 的聚合体走 `Indirect`”的分支。`RISCVCallingConv.td` 只定义 callee-saved 集合，注释明确说调用约定由 `RISCVISelLowering.cpp` 的 `CC_RISCV` 自定义代码处理。

### C.3 产出证据的命令

在仓库根目录运行练习脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 labs/practice-c-abi-audit.ps1
```

脚本会在 `%TEMP%\riscv-course-practiceC` 生成 `abi.c`、`abi-O0.s`、`abi-O0.ll` 和 `byval.ll`，然后编译。核心命令是：

```powershell
clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O0 -S abi.c -o abi-O0.s
clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O0 -S -emit-llvm abi.c -o abi-O0.ll
llc -mtriple=riscv64 -O0 byval.ll -o byval-O0.s
```

使用 `-O0` 是有意的：优化会内联调用、消除临时对象，把 ABI 痕迹抹掉。审计 ABI 时先看 `-O0`，再看 `-O2` 是否仍然保持同样的参数落点。`-O2` 下 `call_make` 可能被内联成直接写结果，这不代表 ABI 消失，只代表调用被优化掉了。

### C.4 审计表模板

每个签名填一行，列的含义固定：

| 列 | 要写什么 |
| --- | --- |
| 参数位置 | 每个形参在入口时对应的寄存器或栈偏移 |
| 栈上内容 | 调用者/被调用者在栈上放了什么，偏移与大小 |
| 回收责任 | 谁恢复 `sp`、谁释放副本、谁负责结果对象 |
| 证据 | 汇编里的关键指令，例如 `sd a0, 56(sp)`、`call`、`addi sp, sp, 96` |
| 边界 | 该签名里最容易误判的一点 |

填写时不要用“参数在栈上”这种模糊说法。要写“第 9 个参数在入口时位于 `0(sp)`；被调用者 prologue 之后位于 `80(sp)`”。栈偏移的基准是 `sp`，而 `sp` 在 prologue 后会变，两个数字都对，但含义不同。

### C.5 示范：ints9 的九个整数

`ints9` 的 `-O0` 汇编开头是：

```asm
ints9:                                  # @ints9
# %bb.0:
	addi	sp, sp, -80
	sd	ra, 72(sp)
	sd	s0, 64(sp)
	addi	s0, sp, 80
	ld	t0, 80(sp)
	sd	a0, 56(sp)
	sd	a1, 48(sp)
	sd	a2, 40(sp)
	sd	a3, 32(sp)
	sd	a4, 24(sp)
	sd	a5, 16(sp)
	sd	a6, 8(sp)
	sd	a7, 0(sp)
```

前 8 个参数在入口时分别位于 `a0`–`a7`；第 9 个参数在入口时位于 `0(sp)`。prologue 把 `sp` 拉下 80 之后，第 9 个参数变成 `80(sp)`，所以第一条读取是 `ld t0, 80(sp)`。后面的 `sd a0, 56(sp)` … `sd a7, 0(sp)` 是 `-O0` 把传入寄存器保存到局部槽，不是 ABI 要求；它们只是让调试器/后续代码能重新读取形参。

回收责任：第 9 个参数的栈空间由调用者分配，`ints9` 自己不弹出；`ints9` 只负责 `addi sp, sp, 80` 恢复自己的帧。调用者一侧见 `call_ints9`：

```asm
call_ints9:                             # @call_ints9
# %bb.0:
	addi	sp, sp, -96
	sd	ra, 88(sp)
	sd	s0, 80(sp)
	addi	s0, sp, 96
	ld	t0, 96(sp)
	...
	ld	t0, 96(sp)
	mv	t1, sp
	sd	t0, 0(t1)
	call	ints9
	ld	ra, 88(sp)
	ld	s0, 80(sp)
	addi	sp, sp, 96
	ret
```

`call_ints9` 的第 9 个参数从自己的调用者那里位于 `96(sp)`，它把这个值取到 `t0`，再写到自己的 `0(sp)`，作为给 `ints9` 的栈参数。`ints9` 返回后，`call_ints9` 用 `addi sp, sp, 96` 一次性回收自己的帧和这块出参空间。这里没有单独的 `addi sp, sp, 8`，因为出参空间被包含在 96 字节帧里。

### C.6 结构体：small、big 与 byval 的区别

先看对照签名：

```cpp
long take_small(struct Small s);
```

`take_small` 接收 16 字节结构体。`-O0` 汇编开头：

```asm
take_small:                             # @take_small
# %bb.0:
	addi	sp, sp, -32
	sd	ra, 24(sp)
	sd	s0, 16(sp)
	addi	s0, sp, 32
	sd	a1, 8(sp)
	sd	a0, 0(sp)
	ld	a0, 0(sp)
	ld	a1, 8(sp)
	add	a0, a0, a1
```

入口时 `s.a` 在 `a0`，`s.b` 在 `a1`；结构体被拆成两个整数块，没有栈参数。返回值在 `a0`。IR 里对应 `define i64 @take_small([2 x i64] %0)`，这也印证了“按两个 XLEN 块传递”。

`take_big` 接收 32 字节结构体。`-O0` 汇编开头：

```asm
take_big:                               # @take_big
# %bb.0:
	addi	sp, sp, -32
	sd	ra, 24(sp)
	sd	s0, 16(sp)
	addi	s0, sp, 32
	mv	a1, a0
	sd	a1, 8(sp)
	ld	a0, 0(a1)
	ld	a2, 8(a1)
	add	a0, a0, a2
	ld	a2, 16(a1)
	add	a0, a0, a2
	ld	a1, 24(a1)
	add	a0, a0, a1
```

入口时 `a0` 不是 `s.a`，而是指向结构体副本的指针。四条 `ld` 分别读 `0(a1)`、`8(a1)`、`16(a1)`、`24(a1)`，说明结构体在内存里连续 32 字节。IR 里对应 `define i64 @take_big(ptr noundef align 8 dead_on_return %0)`，没有 `byval` 属性。

调用者 `call_take_big` 负责副本：

```asm
call_take_big:                          # @call_take_big
# %bb.0:
	addi	sp, sp, -96
	sd	ra, 88(sp)
	sd	s0, 80(sp)
	addi	s0, sp, 96
	sd	a0, 72(sp)
	ld	a0, 72(sp)
	sd	a0, 40(sp)
	...
	ld	a0, 64(sp)
	sd	a0, 32(sp)
	...
	ld	a0, 40(sp)
	sd	a0, 8(sp)
	addi	a0, sp, 8
	call	take_big
	ld	ra, 88(sp)
	ld	s0, 80(sp)
	addi	sp, sp, 96
	ret
```

副本放在 `sp + 8` 到 `sp + 39`（32 字节），`addi a0, sp, 8` 把副本地址作为第一个参数。被调用者没有复制这 32 字节，只读指针；调用者在 `addi sp, sp, 96` 时回收副本。

手写 `byval` 是另一种表示。下面的 IR 显式写了 `ptr byval(%Big) align 8`：

```llvm
%Big = type { i64, i64, i64, i64 }
define i64 @byval_big(ptr byval(%Big) align 8 %p) {
  %a = load i64, ptr %p
  %q = getelementptr i64, ptr %p, i64 3
  %d = load i64, ptr %q
  %r = add i64 %a, %d
  ret i64 %r
}
```

`llc -mtriple=riscv64 -O0` 的调用者仍然是“先复制、再传指针”：

```asm
call_byval:                             # @call_byval
# %bb.0:
	addi	sp, sp, -80
	sd	ra, 72(sp)
	sd	a0, 40(sp)
	li	a0, 1
	sd	a0, 48(sp)
	li	a0, 2
	sd	a0, 56(sp)
	li	a0, 3
	sd	a0, 64(sp)
	ld	a0, 64(sp)
	sd	a0, 32(sp)
	ld	a0, 56(sp)
	sd	a0, 24(sp)
	ld	a0, 48(sp)
	sd	a0, 16(sp)
	ld	a0, 40(sp)
	sd	a0, 8(sp)
	addi	a0, sp, 8
	call	byval_big
	ld	ra, 72(sp)
	addi	sp, sp, 80
	ret
```

`byval_big` 入口拿到的仍是 `a0` 指针，reads `0(a1)` 和 `24(a1)`。所以在本机这条路径上，`byval` 不等于“被调用者在栈上再复制一份”；它表示“调用者保证传进来的对象是一份可修改的私有副本”，具体由调用者准备副本、传指针。把 `byval` 直接解释成“参数按值放在调用者的栈参数区”会在审计表里写错列。

### C.7 sret：结果对象是谁的

`make_big` 返回 32 字节结构体。`-O0` 汇编：

```asm
make_big:                               # @make_big
# %bb.0:
	addi	sp, sp, -48
	sd	ra, 40(sp)
	sd	s0, 32(sp)
	addi	s0, sp, 48
	sd	a1, 8(sp)
	mv	a1, a0
	ld	a0, 8(sp)
	sd	a1, 24(sp)
	sd	a0, 16(sp)
	ld	a0, 16(sp)
	sd	a0, 0(a1)
	ld	a0, 16(sp)
	addi	a0, a0, 1
	sd	a0, 8(a1)
	ld	a0, 16(sp)
	addi	a0, a0, 2
	sd	a0, 16(a1)
	ld	a0, 16(sp)
	addi	a0, a0, 3
	sd	a0, 24(a1)
```

入口时 `a0` 是 `sret` 指针，`a1` 是用户参数 `x`。四条 `sd` 写 `0(a1)`、`8(a1)`、`16(a1)`、`24(a1)`，把 `{x, x+1, x+2, x+3}` 写进结果对象。IR 里对应 `define void @make_big(ptr ... sret(%struct.Big) align 8 %0, i64 noundef %1)`。

调用者 `use_make` 在自己的帧里准备结果槽：

```asm
use_make:                               # @use_make
# %bb.0:
	addi	sp, sp, -64
	sd	ra, 56(sp)
	sd	s0, 48(sp)
	addi	s0, sp, 64
	sd	a0, 40(sp)
	ld	a1, 40(sp)
	addi	a0, sp, 8
	call	make_big
	ld	a0, 8(sp)
	ld	a1, 32(sp)
	add	a0, a0, a1
	ld	ra, 56(sp)
	ld	s0, 48(sp)
	addi	sp, sp, 64
	ret
```

结果槽在 `sp + 8` 到 `sp + 39`。`addi a0, sp, 8` 把槽地址传给 `make_big`；返回后 `ld a0, 8(sp)` 和 `ld a1, 32(sp)` 读首、尾字段。结果对象属于调用者 `use_make`，由它的 `addi sp, sp, 64` 回收；`make_big` 只写，不释放。

### C.8 浮点混合：fa0–fa4

`mixfp(float a, float b, double c, double d, float e)` 的 `-O0` 汇编开头：

```asm
mixfp:                                  # @mixfp
# %bb.0:
	addi	sp, sp, -48
	sd	ra, 40(sp)
	sd	s0, 32(sp)
	addi	s0, sp, 48
	fsw	fa0, 28(sp)
	fsw	fa1, 24(sp)
	fsd	fa2, 16(sp)
	fsd	fa3, 8(sp)
	fsw	fa4, 4(sp)
	flw	fa5, 28(sp)
	flw	fa4, 24(sp)
	fadd.s	fa5, fa5, fa4
	fcvt.d.s	fa5, fa5
	fld	fa4, 16(sp)
	fadd.d	fa5, fa5, fa4
	fld	fa4, 8(sp)
	fadd.d	fa5, fa5, fa4
	flw	fa4, 4(sp)
	fcvt.d.s	fa4, fa4
	fadd.d	fa0, fa5, fa4
```

`float` 用 `fsw` 保存 `fa0`、`fa1`、`fa4`，`double` 用 `fsd` 保存 `fa2`、`fa3`；没有栈参数。最后结果在 `fa0` 返回。`float` 和 `double` 不各自从 `fa0` 重新编号，而是共用同一个 `fa0`–`fa7` 序列；类型决定用 `fsw` 还是 `fsd`，不决定寄存器编号。

如果你把 ABI 改成软浮点（`-mabi=lp64` 或不启用 `F`），这些参数会落到整数寄存器或栈上，`fadd.s`/`fadd.d` 也会变成 libcall。审计时先确认 `-mabi` 和 `-mattr`，再谈寄存器编号。

### C.9 varargs：sumv 的 112 字节帧

`sumv(int n, ...)` 的 `-O0` 汇编：

```asm
sumv:                                   # @sumv
# %bb.0:
	addi	sp, sp, -112
	sd	ra, 40(sp)
	sd	s0, 32(sp)
	addi	s0, sp, 48
	sd	a7, 104(sp)
	sd	a6, 96(sp)
	sd	a5, 88(sp)
	sd	a4, 80(sp)
	sd	a3, 72(sp)
	sd	a2, 64(sp)
	sd	a1, 56(sp)
	sw	a0, 28(sp)
	addi	a0, sp, 56
	sd	a0, 16(sp)
	li	a0, 0
	sd	a0, 8(sp)
	sw	a0, 4(sp)
```

`a0` 是具名参数 `n`，`a1`–`a7` 是可能的变参。prologue 把 `a1`–`a7` 依次存到 `sp + 56` 到 `sp + 104`，形成 56 字节的寄存器保存区；`va_list` 的游标放在 `sp + 16`，初始化为 `sp + 56`。`s` 在 `sp + 8`，循环变量在 `sp + 4`。栈帧 112 字节，包含保存区、`va_list`、局部变量和 `ra`/`s0`。

回收责任：调用者把变参放进 `a1`–`a7` 或栈上；`sumv` 只读，不弹出；返回时 `addi sp, sp, 112` 恢复自己的帧。若变参数量超过寄存器可容纳的数量，超出的部分在调用者的栈参数区，`va_list` 会在寄存器保存区用完后转向栈指针；本练习的六个签名不展开这部分，但审计表要写清“寄存器保存区”和“栈参数区”是两块不同的内存。

### C.10 边界：`-O2` 会把 ABI 痕迹优化掉

把 `abi.c` 用 `-O2` 编译，`call_take_big` 可能被内联成 `slli a0, a0, 2; addi a0, a0, 6; ret`，`call_make` 也可能直接写调用者的结果槽。此时你仍然能审计 `take_big`、`make_big` 本身的入口约定，但不能从被内联的调用点推断“没有栈副本”。审计顺序建议是：

1. 先看 `-O0`，确认 ABI 落点。
2. 再看 `-O2`，确认优化没有改变入口约定。
3. 若调用被内联，回到 `-O0` 或给调用者加 `__attribute__((noinline))` 重新编译。

这条边界也解释了为什么本章脚本坚持 `-O0`：它保留调用边界，便于观察“谁放参数、谁回收”。

## 可跑实验

实验一：生成六个必做签名（连带对照）的 `-O0` 汇编，填表。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 labs/practice-c-abi-audit.ps1
```

在输出里核对：`ints9` 的 `ld t0, 80(sp)`；`take_small` 的 `sd a1, 8(sp)` / `sd a0, 0(sp)`；`take_big` 的四条 `ld`；`make_big` 的四条 `sd`；`mixfp` 的 `fsw fa0`/`fsd fa2`；`sumv` 的 `sd a7, 104(sp)`。

实验二：对照 IR 属性。

```powershell
$LLVM = 'D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin'
$w = Join-Path $env:TEMP 'riscv-course-practiceC'
Set-Location $w
& "$LLVM\clang.exe" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O0 -S -emit-llvm abi.c -o abi-O0.ll
```

预期看到：

```text
define dso_local i64 @take_small([2 x i64] %0)
define dso_local i64 @take_big(ptr noundef align 8 dead_on_return %0)
define dso_local void @make_big(ptr dead_on_unwind noalias writable sret(%struct.Big) align 8 %0, i64 noundef %1)
define dso_local i64 @sumv(i32 noundef signext %0, ...)
```

这四行分别对应“拆块”“间接指针”“sret”“变参”。如果 IR 里没有 `byval`，不要在手写答案里硬加；C 前端的 `struct Big` 走的是间接指针，不是 `byval`。

实验三：手写 `byval` IR，观察调用者副本。

```powershell
$LLVM = 'D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin'
$w = Join-Path $env:TEMP 'riscv-course-practiceC'
Set-Location $w
& "$LLVM\llc.exe" -mtriple=riscv64 -O0 byval.ll -o byval-O0.s
```

预期：`byval_big` 用 `a0` 指针读 `0(a1)`/`24(a1)`；`call_byval` 在 `sp + 8` 到 `sp + 39` 建副本，`addi a0, sp, 8` 后 `call`，最后 `addi sp, sp, 80` 回收。

失败/边界例子：把 `take_big` 的入口 `a0` 当成 `s.a`，会得到“参数在 a0、a1、a2、a3”的错误答案。真实汇编第一条就是 `mv a1, a0`，随后 `ld a0, 0(a1)`；`a0` 是指针，四个字段都在内存里。这个错误在审计表里会同时写错“参数位置”和“栈上内容”两列。

### 参考答案（先做完实验再看）

<details><summary>参考答案</summary>

#### 总表

六个必做签名是 `ints8`、`ints9`、`take_big`、`make_big`、`mixfp`、`sumv`；`take_small` 和 `byval_big` 是对照项，不计入这六项。

| 签名 | 参数位置（入口） | 栈上内容 | 回收责任 |
| --- | --- | --- | --- |
| `ints8` | `a0`–`a7` 依次对应 8 个 `long` | 无 ABI 栈参数；`-O0` 会把这些寄存器保存到局部槽，但那是编译器 spill，不是调用者放的参数 | 调用者不分配栈参数；被调用者用 `addi sp, sp, 80` 回收自己的帧 |
| `ints9` | `a0`–`a7` 对应前 8 个；第 9 个在入口 `0(sp)` | 调用者放 8 字节栈参数；被调用者 prologue 后它在 `80(sp)` | 调用者分配并回收出参空间；`ints9` 只恢复自己的 `sp` |
| `take_big` | `a0` = 指向 32 字节副本的指针 | 调用者副本在 `call_take_big` 的 `sp + 8`–`sp + 39`；被调用者只读 | 调用者建副本并 `addi sp, sp, 96` 回收；被调用者不复制、不释放 |
| `make_big` | `a0` = `sret` 结果指针，`a1` = `x` | 调用者结果槽在 `use_make` 的 `sp + 8`–`sp + 39`；被调用者写 32 字节 | 调用者提供并回收结果对象；被调用者只写 |
| `mixfp` | `fa0` = `a`，`fa1` = `b`，`fa2` = `c`，`fa3` = `d`，`fa4` = `e` | 无栈参数；`-O0` 用 `fsw`/`fsd` 保存到局部槽 | 调用者不分配；被调用者恢复 48 字节帧；结果在 `fa0` |
| `sumv` | `a0` = `n`；`a1`–`a7` = 可能的变参 | 寄存器保存区 `sp + 56`–`sp + 104`；`va_list` 游标 `sp + 16`；超出寄存器的变参在调用者栈参数区 | 调用者清理栈参数；`sumv` 用 `addi sp, sp, 112` 回收自己的帧 |
| `byval_big`（手写 IR） | `a0` = 指向调用者私有副本的指针 | `call_byval` 副本在 `sp + 8`–`sp + 39`；`byval_big` 读 `0(a1)`/`24(a1)` | 调用者建副本并 `addi sp, sp, 80` 回收；被调用者不复制 |

#### 逐签名解释

**ints8。** 8 个 `long` 刚好用满 `a0`–`a7`。`-O0` 汇编里出现的 `sd a0, 56(sp)` 等是把传入寄存器保存到局部槽，便于调试和后续读取；它们不是调用者放的栈参数。第 9 个参数才会触发栈参数。

**ints9。** 第 9 个参数在入口时位于 `0(sp)`。`ints9` 的 prologue 把 `sp` 拉下 80，因此代码用 `ld t0, 80(sp)` 读取它。调用者 `call_ints9` 把第 9 个参数写到自己的 `0(sp)`，再用 `addi sp, sp, 96` 回收整个帧。被调用者不执行 `addi sp, sp, 8` 来弹出这个参数。

**take_small。** 16 字节结构体等于两个 XLEN，按两个整数块传递。入口 `a0` 是 `s.a`，`a1` 是 `s.b`；`-O0` 先 `sd` 到 `0(sp)`/`8(sp)`，再读回相加。没有调用者栈参数。

**take_big。** 32 字节结构体超过两个 XLEN，按间接方式传递。入口 `a0` 是指针，四个字段在 `0(a0)`、`8(a0)`、`16(a0)`、`24(a0)`。调用者 `call_take_big` 在 `sp + 8` 建 32 字节副本，把 `sp + 8` 放进 `a0`，调用后用自己的 `addi sp, sp, 96` 回收。被调用者不复制这 32 字节。

**make_big。** `sret` 把结果对象地址放在 `a0`，用户参数 `x` 放在 `a1`。`make_big` 用 `sd` 写 `0(a1)`、`8(a1)`、`16(a1)`、`24(a1)`（这里的 `a1` 是保存下来的结果指针，不是第二个参数）。调用者 `use_make` 在 `sp + 8` 准备结果槽，调用后读 `8(sp)` 和 `32(sp)`。结果对象属于调用者，由 `addi sp, sp, 64` 回收。

**mixfp。** `float` 和 `double` 共用 `fa0`–`fa7` 序列：`a`/`b` 用 `fa0`/`fa1`，`c`/`d` 用 `fa2`/`fa3`，`e` 用 `fa4`。`-O0` 用 `fsw` 保存三个 `float`，`fsd` 保存两个 `double`；最终 `fadd.d fa0, ...` 把结果留在 `fa0`。没有栈参数。

**sumv。** 具名参数 `n` 在 `a0`，变参从 `a1` 开始。`-O0` 把 `a1`–`a7` 存到 `sp + 56`–`sp + 104`，把 `va_list` 游标指向 `sp + 56`。`sumv` 的 112 字节帧包含保存区、`va_list`、局部变量和 `ra`/`s0`。调用者负责清理栈参数，`sumv` 只恢复自己的帧。

**byval_big。** 手写 IR 的 `byval` 让调用者准备一份私有副本并传指针。`call_byval` 在 `sp + 8` 建副本，`byval_big` 入口 `a0` 指向它；被调用者读 `0(a1)` 和 `24(a1)`。`byval` 不表示“被调用者在自己的栈帧里再复制一次”，至少在本机这条 lowering 路径上不是。

#### 评分表（40 分）

| 项目 | 分值 | 评分标准 |
| --- | --- | --- |
| 六个签名的参数位置 | 12（每签名 2） | 寄存器/栈偏移全部正确；只写“寄存器”不写具体名字扣 1 |
| 六个签名的栈上内容 | 12（每签名 2） | 能区分 ABI 栈参数、编译器 spill、调用者副本、`sret` 结果槽 |
| 回收责任 | 6（每签名 1） | 明确写“调用者清理”还是“被调用者恢复自己的帧” |
| 证据引用 | 5 | 每个签名至少引用一条真实指令或 IR 属性 |
| 边界说明 | 5 | 能指出 `s0` 非帧指针、`byval` 非栈按值、`-O2` 内联等至少两个边界 |

#### 常见错误

1. **把 `a0` 同时当成第一个参数和返回值。** 入口时 `a0` 是参数；返回时 `a0` 是返回值。`take_big` 的 `a0` 在入口是指针，在返回时是 `s.a+s.b+s.c+s.d`。
2. **认为 `float` 从 `fa0` 重新编号，`double` 从 `fa0` 重新编号。** 在 LP64D 下它们共用 `fa0`–`fa7`；`mixfp` 的第五个参数是 `fa4`，不是 `fa0`。
3. **说被调用者“弹出”栈参数。** RISC-V 是 caller-cleans；`ints9` 不会 `addi sp, sp, 8`，它只恢复自己的 80 字节帧。
4. **把 `sret` 当成普通返回值。** `make_big` 的 `a0` 是结果对象指针，用户参数从 `a1` 开始；结果对象由调用者提供和回收。
5. **把 `byval` 写成“参数在调用者栈参数区按值排列”。** 本机 lowering 是“调用者建副本、传指针”；被调用者通过 `a0` 读副本。
6. **把 `-O0` 的 `sd a0, 56(sp)` 当成 ABI 栈参数。** 这是编译器把传入寄存器 spill 到局部槽，不是调用者放的参数。判断 ABI 栈参数要看入口时的 `sp` 偏移，而不是 prologue 之后的局部槽。
7. **忽略变参的寄存器保存区。** `sumv` 的 `sp + 56`–`sp + 104` 是 `a1`–`a7` 的副本，`va_list` 从这里开始读；把它当成普通局部数组会解释错 `va_arg` 的游标。
8. **用 `-O2` 的内联结果否定 ABI。** 内联会消除调用点和副本，但不会改变 `take_big`/`make_big` 本身的入口约定。审计时先看 `-O0`。

</details>

## 源码落点

| 文件 | 符号 | 用途 |
| --- | --- | --- |
| `lib/Target/RISCV/RISCVCallingConv.cpp` | `RISCV::getArgGPRs` | `a0`–`a7` 的参数寄存器表；E ABI 只有 6 个 |
| 同上 | `RISCV::getArgFPRs` | 软浮点/无 F 时返回空；LP64D 下 `fa0`–`fa7` |
| 同上 | `CC_RISCV_Impl` | 浮点分配、变参对齐、超过 2*XLEN 的 `Indirect` |
| `lib/Target/RISCV/RISCVCallingConv.td` | `CSR_ILP32D_LP64D` | callee-saved 集合；注释说明调用约定由 C++ 处理 |
| `lib/Target/RISCV/RISCVISelLowering.cpp` | `LowerFormalArguments` / `LowerCall` | 形参与调用点的实际 lowering；`byval`、`sret`、`vararg` 的分支 |
| `lib/Target/RISCV/RISCVFrameLowering.cpp` | `determineCalleeSaves`、`emitPrologue` | 保存 `ra`/`s0`、varargs 保存区、栈对齐 |
| `include/llvm/IR/IntrinsicsRISCV.td` | 无 | 本练习不需要 intrinsic；但第 16 章会从同一套 IR 约定继续 |

`guided`：如果你要给某个签名加 `__attribute__((noinline))` 或改变 `-mabi`，重新编译 `clang`/`llc` 后重跑本练习，观察参数落点是否变化。实际后端代码不需要改；这是驱动层/ABI 选择层的实验，标 `guided` 是因为它依赖一次完整的工具链重编译。

## 常见坑

1. **把 `a0`–`a7` 与 `fa0`–`fa7` 当成同一条编号序列。** 它们是两套独立的寄存器堆；`mixfp` 同时用了 `fa0`–`fa4`，整数参数仍然从 `a0` 开始。
2. **把 `struct Big` 当成 `byval`。** C 前端在 LP64D 下把大于 2*XLEN 的聚合体降成间接指针；IR 里没有 `byval`。手写 `byval` 才会出现 `byval(%Big)` 属性。
3. **把 `sret` 结果槽算进被调用者的帧。** `make_big` 的结果槽在 `use_make` 的 `sp + 8`，不在 `make_big` 的 48 字节帧里；`make_big` 只通过 `a0` 写。
4. **把 varargs 的寄存器保存区当成普通局部变量。** `sumv` 的 `sp + 56`–`sp + 104` 是为 `va_arg` 准备的 `a1`–`a7` 副本；`va_list` 游标从 `sp + 56` 开始。
5. **忽略 `-O0` 的 spill。** `ints8` 的 `sd a0, 56(sp)` 不是 ABI 栈参数；它在 prologue 之后，入口时并不存在。
6. **把“调用者清理”理解成“调用者在 callee 返回后执行 `addi sp, sp, N` 弹出参数”。** 实际上调用者的 `sp` 恢复通常一次性覆盖自己的整个帧，出参空间是帧的一部分；不需要单独弹出。
7. **用 `lp64d` 的结论套 `lp64`。** 软浮点 ABI 下 `float`/`double` 不进 FPR，`mixfp` 的 `fa0`–`fa4` 结论会全部失效。
8. **把 `-O2` 的内联结果当成 ABI 变化。** 入口约定不变，变的是调用点是否还存在；审计要回到 `-O0` 或 `noinline`。

## 自我检查

- [ ] 我能不看答案，写出 `ints9` 第 9 个参数在入口和 prologue 后的两个偏移。
- [ ] 我能解释 `take_small` 为什么用 `a0`/`a1`，而 `take_big` 只用 `a0` 指针。
- [ ] 我能指出 `make_big` 的 `sret` 指针和用户参数分别在哪两个寄存器。
- [ ] 我能从 `mixfp` 的 `fsw`/`fsd` 序列读出五个浮点参数各在哪个 `fa` 寄存器。
- [ ] 我能解释 `sumv` 的 112 字节帧里，寄存器保存区、`va_list` 游标、局部变量各占哪一段。
- [ ] 我能说清 `byval` 与 C 结构体间接传递的区别，并指出本机 `call_byval` 的副本偏移。
- [ ] 我能判断一个栈参数是“调用者放的”还是“`-O0` spill 的”。
- [ ] 我能说明为什么审计 ABI 要先看 `-O0`，以及 `-O2` 内联后还剩下什么证据。
- [ ] 我能指出 `sret` 结果对象的回收者，并说明它不在被调用者的帧里。
- [ ] 我能给出至少两个“把 ABI 结论写错”的具体反例。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVCallingConv.cpp`：`CC_RISCV_Impl` 是本章的总规则；重点读 FPR 分配、变参对齐、`Indirect` 分支。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`：`LowerFormalArguments` 与 `LowerCall`，看 `byval`、`sret`、`vararg` 如何变成栈槽和寄存器。
- `llvm/lib/Target/RISCV/RISCVFrameLowering.cpp`：`determineCalleeSaves`、`emitPrologue`，把本章的栈偏移与第 13 章的帧布局对上。
- `llvm/test/CodeGen/RISCV/`：搜索 `byval`、`sret`、`vararg`、`struct` 相关测试，观察不同 ABI 下的 CHECK 行。
- RISC-V ELF psABI：整数/浮点参数寄存器、聚合体拆块、栈对齐、caller-cleans 的规范来源。
- `clang -cc1 -triple riscv64-unknown-elf -mabi lp64d -emit-llvm`：从 C 源码直接看 IR 属性，比从汇编反推更可靠。

[上一章：栈帧](./13-frame-lowering.md) | [下一章：合法化](./14-legalization.md)
