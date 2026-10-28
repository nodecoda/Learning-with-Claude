# Ch 18 — AArch64 后端实战

> **目标**：把同一条 `int add(int, int)` 从 LLVM IR 一路推到 qemu 里跑出的数字，每一层都留下可核对的产物：IR、`.s`、`.o`、可执行文件、运行结果。学完这章，你能看着一段 AArch64 汇编说出它是哪一层决定的——IR 优化、指令选择、寄存器分配、栈帧布局，还是链接。

> **环境**：Ubuntu 22.04 / x86_64；LLVM 14.0.0（`/usr/lib/llvm-14/bin`）；`aarch64-linux-gnu-gcc` 11.4；`qemu-aarch64-static` 6.2

## 为什么需要这个？

Ch 12–17 把每一层单独拆开讲过：MLIR 怎么降级、LLVM pass 怎么排、指令怎么选、寄存器怎么分、重定位怎么填。但真实排错时，问题常常跨层，而且现象很相似：

- 汇编里出现 `mul w8, w1, w0`，是 IR 就这么写的，还是指令选择改的？
- 换成 `-O2` 之后变成 `madd`，是哪一步做的融合？
- 二进制里的 `add` 和 `add.s` 里的 `add` 为什么不是同一条指令？
- 叶函数没有 `stp x29, x30`，是不是编译器漏了保存返回地址？

这些问题没法靠背指令回答。**最可靠的办法是把同一函数在每一层的产物都打出来，逐个对上。** 本章就做这件事：一个两行 C 函数，五层产物，全部实测。

选 AArch64，因为它在边缘设备上最常见（手机、树莓派、Jetson、大量 NPU 主控核），汇编也规整：32 位定长指令、三操作数、零寄存器 `wzr`、乘加融合 `madd`。

## 先建立直觉

把整条链想成**五次改写的流水线**，每次改写都留下一个文件：

```
add.c     源码
  │ clang -O2 -S -emit-llvm
  ▼
add.ll    LLVM IR：虚拟寄存器 %0 %1 %3 %4，没有物理寄存器
  │ llc -march=aarch64
  ▼
add.s     AArch64 汇编：物理寄存器 w0 w1 w8
  │ llc -filetype=obj
  ▼
add.o     目标文件：机器码 + 符号表 + 重定位
  │ aarch64-linux-gnu-gcc -static
  ▼
add_a64   可执行文件：地址已分配，重定位已填
  │ qemu-aarch64-static
  ▼
add(3,4) = 13
```

关键心态：**每层都能独立验证，不要只看最后一行。** 输出 `13` 只证明“没崩”，证明不了中间某层就是你以为的那条指令。主角是两行 C：

```c
int add(int a, int b) { return a * b + 1; }
```

它小到能完整打印，又包含乘法、加法、立即数、参数传递、返回值五件事。

## 底层机制一：IR 里没有寄存器

先看第一层产物。命令：

```bash
cd compilers/ai_to_edge/code/04_aarch64
/usr/lib/llvm-14/bin/clang --target=aarch64-linux-gnu -O2 -S -emit-llvm add.c -o add.ll
```

实测 `add.ll` 的函数体（节选，保留原始编号）：

```llvm
define dso_local i32 @add(i32 noundef %0, i32 noundef %1) local_unnamed_addr #0 {
  %3 = mul nsw i32 %1, %0
  %4 = add nsw i32 %3, 1
  ret i32 %4
}
```

三个细节值得停一下：

1. **`%0`、`%1`、`%3`、`%4` 是 SSA 值编号，不是寄存器。** 它们可以被分到任何物理寄存器，也可以被完全消除；虚拟寄存器的数量上限远大于 31。
2. **`%3` 的操作数顺序是 `%1, %0`，不是 `%0, %1`。** 乘法可交换，前端或优化器能随意换序；语义不变，但逐字比对汇编的人容易误以为参数传反。
3. **`nsw` 是“no signed wrap”**：承诺有符号溢出不会发生。有了它，优化器才能自由重排、融合；去掉它，某些优化会被禁止。

IR 层的结论只有一句：**这一层还没有任何 AArch64 知识**。同一份 `add.ll` 可以喂给 `llc -march=riscv64` 或 `-march=x86-64`，目标相关的决定全在下一层。

## 底层机制二：优化级别直接改变指令选择

这里有个容易踩的坑：**`llc` 的默认优化级别不是 `-O0`**。同一个 `add.ll`，不同 `-O` 给出的指令不一样。

`-O0`（实测）：

```bash
/usr/lib/llvm-14/bin/llc -march=aarch64 -O0 add.ll -o -
```

```asm
add:                                    // @add
	.cfi_startproc
// %bb.0:
	mul	w8, w1, w0
	add	w0, w8, #1
	ret
```

`-O2`（实测）：

```bash
/usr/lib/llvm-14/bin/llc -march=aarch64 -O2 add.ll -o -
```

```asm
add:                                    // @add
	.cfi_startproc
// %bb.0:
	orr	w8, wzr, #0x1
	madd	w0, w1, w0, w8
	ret
```

两条路径都正确，差别在指令选择与窥孔优化：

- `mul` + `add` 是最直译的形态：先算乘积放 `w8`，再加 1 放回 `w0`。
- `-O2` 把 `a*b+1` 识别成乘加，用一条 `madd w0, w1, w0, w8` 完成 `w0 = w1*w0 + w8`。前一条不是“加法”，而是**准备常数 1**：`orr w8, wzr, #0x1` 把零寄存器 `wzr` 与立即数 1 做按位或。`mov w8, #1` 在 AArch64 上本来就是这条 ORR 的别名。

所以“`a*b+1` 是几条指令”没有唯一答案，必须先说优化级别。

同一个 `-O2` 让 `clang` 直接从 C 出汇编（`clang --target=aarch64-linux-gnu -O2 -S add.c -o -`），实测同样是 `orr w8, wzr, #0x1` + `madd w0, w1, w0, w8` + `ret`。说明 `madd` 不是 `llc -O2` 的专属行为：优化在 IR 之后就决定了乘加形态，`llc` 只是把它选成指令。

还有一层更容易混淆：`add_a64` 里的 `add` 并不是 `add.s` 里的 `add`。看 `build.sh` 最后一行：

```bash
aarch64-linux-gnu-gcc -static -O2 add.c drv.c -o add_a64
```

它链接的是 **gcc 重新编译的 `add.c`**，不是 `llc` 产出的 `add.s`。实测二进制符号：

```bash
aarch64-linux-gnu-objdump -d add_a64 | grep -A4 '<add>:'
```

```text
0000000000400720 <add>:
  400720:	1b017c00 	mul	w0, w0, w1
  400724:	11000400 	add	w0, w0, #0x1
  400728:	d65f03c0 	ret
  40072c:	d503201f 	nop
```

注意操作数顺序也变了：gcc 写的是 `mul w0, w0, w1`，llc 写的是 `mul w8, w1, w0`。语义相同，寄存器选择不同。**“汇编文件里是什么”和“二进制里是什么”是两件事**，这条区别会在踩雷集锦第 2 条再展开。

## 底层机制三：寄存器分配器眼里的世界

指令选择之后，每条指令用的还是虚拟寄存器。想看这一步，得用 `-print-before` / `-print-after`，而且要选对 pass 名字。

实测 `-print-before=greedy`：

```bash
/usr/lib/llvm-14/bin/llc -march=aarch64 add.ll -print-before=greedy -o /dev/null
```

```text
# *** IR Dump Before Greedy Register Allocator (greedy) ***:
# Machine code for function add: NoPHIs, TracksLiveness, TiedOpsRewritten, TracksDebugUserValues
Function Live Ins: $w0 in %0, $w1 in %1

0B	bb.0 (%ir-block.2):
	  liveins: $w0, $w1
16B	  %1:gpr32 = COPY $w1
32B	  %0:gpr32 = COPY $w0
48B	  %4:gpr32common = ORRWri $wzr, 0
64B	  %3:gpr32common = MADDWrrr %1:gpr32, %0:gpr32, %4:gpr32common
80B	  $w0 = COPY %3:gpr32common
96B	  RET_ReallyLR implicit $w0
```

这段文本把两套命名放在了一起：

- `%1:gpr32`、`%3:gpr32common` 是**虚拟寄存器**，`%` 前缀，带寄存器类别（`gpr32` 是 32 位通用寄存器）。
- `$w1`、`$w0`、`$wzr` 是**物理寄存器**，`$` 前缀。
- `COPY $w1` 是因为 ABI 规定第一个参数在 `w0`、第二个在 `w1`，而 IR 参数编号是 `%0`、`%1`，要显式搬运。
- `MADDWrrr` 是 `madd` 的机器指令描述符，三个 `r` 是三个寄存器操作数。

分配之后（`-print-after=greedy`），实测输出里虚拟寄存器仍在——因为 greedy 分配器输出的是**分配结果映射**，真正把 `%` 换成 `$` 的是后面的 Virtual Register Rewriter。实测这一步：

```bash
/usr/lib/llvm-14/bin/llc -march=aarch64 add.ll -print-after=virtregrewriter -o /dev/null
```

```text
# *** IR Dump After Virtual Register Rewriter (virtregrewriter) ***:
# Machine code for function add: NoPHIs, TracksLiveness, NoVRegs, TiedOpsRewritten, TracksDebugUserValues
Function Live Ins: $w0, $w1

0B	bb.0 (%ir-block.2):
	  liveins: $w0, $w1
48B	  renamable $w8 = ORRWri $wzr, 0
64B	  renamable $w0 = MADDWrrr killed renamable $w1, killed renamable $w0, killed renamable $w8
96B	  RET_ReallyLR implicit $w0
```

看到 `NoVRegs` 和全部 `$` 前缀，才算真正完成“虚拟 → 物理”。这里还有一个更细的坑：**`-print-after=regalloc` 没有任何输出**，实测字节数为 0。LLVM 14 的 AArch64 后端里，这个 pass 名不存在（或没有注册打印），正确的名字是 `greedy`、`virtregrewriter` 这类具体 pass。

## 底层机制四：叶函数与非叶函数

AArch64 的返回地址在 `x30`（LR）。函数不调用别的函数（叶函数）时，`x30` 不会被覆盖，**不需要保存**。实测叶函数：

```c
int leaf(int a, int b) { return a * b + 1; }
```

```asm
leaf:                                   // @leaf
	.cfi_startproc
// %bb.0:
	orr	w8, wzr, #0x1
	madd	w0, w1, w0, w8
	ret
.Lfunc_end0:
	.size	leaf, .Lfunc_end0-leaf
	.cfi_endproc
```

没有 `stp`，没有 `sub sp`，没有 `ldp`，栈指针一动没动。同一文件里放一个非叶函数：

```c
extern int ext(int);
int f(int a) { return ext(a) + 1; }
```

实测：

```asm
f:                                      // @f
	.cfi_startproc
// %bb.0:
	stp	x29, x30, [sp, #-16]!           // 16-byte Folded Spill
	mov	x29, sp
	.cfi_def_cfa w29, 16
	.cfi_offset w30, -8
	.cfi_offset w29, -16
	bl	ext
	add	w0, w0, #1
	ldp	x29, x30, [sp], #16             // 16-byte Folded Reload
	ret
```

逐个成分拆开：
- `stp x29, x30, [sp, #-16]!`：**pre-index**。先把 `sp` 减 16，再把 `x29`（帧指针）和 `x30`（返回地址）成对存入；感叹号表示写回 `sp`。
- 为什么是 16 不是 8？AAPCS64 要求调用时 `sp` 16 字节对齐。两个 8 字节寄存器刚好 16 字节，一次 `stp` 完成，比两条 `str` 省一条。
- `mov x29, sp`：建立帧指针，方便回溯和变长栈帧。
- `bl ext` 覆盖 `x30`，所以先存；叶函数没有 `bl`，不用存。
- `ldp x29, x30, [sp], #16`：**post-index**，先读再 `sp += 16`，与开头对称。

如果函数还需要跨调用保存一个局部值，栈帧会再大一点。实测这个版本：

```c
extern int ext(int);
int caller(int a, int b) { return ext(a) + b; }
```

```asm
caller:                                 // @caller
	.cfi_startproc
// %bb.0:
	stp	x29, x30, [sp, #-32]!           // 16-byte Folded Spill
	str	x19, [sp, #16]                  // 8-byte Folded Spill
	mov	x29, sp
	...
	mov	w19, w1
	bl	ext
	add	w0, w0, w19
	ldr	x19, [sp, #16]                  // 8-byte Folded Reload
	ldp	x29, x30, [sp], #32             // 16-byte Folded Reload
	ret
```

`x19` 是 callee-saved：被调用者要用它，就得自己保存并在返回前恢复。`b` 在 `bl` 之后还要用，所以从 `w1` 挪到 `w19` 跨调用存活，栈帧从 16 涨到 32 字节——仍是 16 的倍数。

**读栈帧三步**：看 `stp x29, x30`（有没有 `bl` 决定要不要）、看 `sub sp` 或 pre-index 的立即数（帧多大）、看中间有没有 `str x19` 这类 callee-saved 保存。

## 底层机制五：目标文件、链接与体积

`llc` 可以直接出目标文件，跳过汇编文本（实测）：

```bash
/usr/lib/llvm-14/bin/llc -march=aarch64 -filetype=obj add.ll -o add_lab.o
aarch64-linux-gnu-objdump -d add_lab.o
```


```text
0000000000000000 <add>:
   0:	320003e8 	orr	w8, wzr, #0x1
   4:	1b002020 	madd	w0, w1, w0, w8
   8:	d65f03c0 	ret
```

三条指令 12 字节。`.text` 里没有重定位——不引用外部符号，也不访问全局变量；但整个文件并非完全没有重定位，实测 `aarch64-linux-gnu-readelf -r add_lab.o` 还留着一条 `R_AARCH64_PREL32`，位于 `.rela.eh_frame`，只是告诉 CFI（调用帧信息）描述的是哪段代码，与指令本身无关。**“代码里没有重定位”和“文件里没有重定位”是两个判断**，看 `readelf -r` 要先看 section 名。链接后地址被分配，指令不变。真正让体积膨胀的是**静态链接的 libc**。实测 `add_a64`：

```bash
ls -l add_a64
size add_a64
```

```text
-rwxrwxr-x 1 ubuntu ubuntu 641112 add_a64
   text	   data	    bss	    dec	    hex	filename
 512834	  22520	  22256	 557610	  8822a	add_a64
```

四个数字要分开看：

- **文件大小 641112 字节**，不等于 `text`。
- **`text` 512834 字节**是可执行代码，`add` 只占 12 字节。
- **`data` 22520、`bss` 22256** 是静态数据和零初始化数据。`bss` 在文件里不占字节，运行时占内存。

两行 C 变成 641112 字节，是因为 `-static` 把用到的 libc 都拉了进来；`libc.a` 单文件就是 **4939918 字节**。本章没有做逐符号归因，**未实测，理论预期行为**是绝大部分来自启动代码、`printf` 和系统调用桩。

## 可跑例子 1：一条命令跑完全程

脚本把五层串起来了：

```bash
cd compilers/ai_to_edge/code/04_aarch64
./build.sh
```

实测输出（完整）：

```text
== qemu ==
add(3,4) = 13
```

脚本内部四步：

```bash
"$LLVM_BIN/clang" --target=aarch64-linux-gnu -O2 -S -emit-llvm add.c -o add.ll
"$LLVM_BIN/llc" -march=aarch64 add.ll -o add.s
aarch64-linux-gnu-gcc -static -O2 add.c drv.c -o add_a64
qemu-aarch64-static ./add_a64
```

对着这四行逐层验收：

```bash
grep -n 'mul\|add' add.ll               # IR 里是 %3/%4
sed -n '/^add:/,/End function/p' add.s   # 汇编里是 w8/w0
file add_a64                             # 看 ELF 头：ARM aarch64, statically linked
```

`13` 本身说明不了什么（手算也是 13），价值在**闭环**：IR 语义、汇编指令、链接地址、qemu 执行指向同一结果。

## 可跑例子 2：把 `.s` 换进链接，体积差 40 字节

`build.sh` 生成了 `add.s` 却没拿它链接。手动换一次，能看到两个真实差异：

```bash
cd compilers/ai_to_edge/code/04_aarch64
aarch64-linux-gnu-gcc -static -O2 drv.c add.s -o /tmp/add_from_s
qemu-aarch64-static /tmp/add_from_s
ls -l /tmp/add_from_s
aarch64-linux-gnu-objdump -d /tmp/add_from_s | grep -A4 '<add>:'
```

实测：

```text
add(3,4) = 13
-rwxr-xr-x 1 ubuntu ubuntu 641152 /tmp/add_from_s
0000000000400714 <add>:
  400714:	320003e8 	orr	w8, wzr, #0x1
  400718:	1b002020 	madd	w0, w1, w0, w8
  40071c:	d65f03c0 	ret
```

两处差异：

1. **指令变了**：`mul` + `add` → `orr` + `madd`，因为 `add.s` 来自 `llc` 默认优化级别。
2. **体积变了**：641112 → 641152，多 40 字节。

40 字节不能直接归因成代码差——两版 `add` 都是 12 字节。**未实测，理论预期行为**：差值来自链接布局、对齐填充或符号表/调试信息的位置，本章没有做段级逐项归因。**看到体积差，先 `size` 再看段。** 再做一次对照：用 `llc -O0 -filetype=obj` 出目标文件再链接，实测二进制里同样是 12 字节，但指令形态回到 `mul w8, w1, w0` + `add w0, w8, #0x1`。**指令选择改的是内容，不一定改长度。**

## 踩雷集锦

1. **以为 `llc -march=aarch64` 默认产出 `mul` + `add`。**
   纠正：默认优化级别下实测是 `orr w8, wzr, #0x1` + `madd w0, w1, w0, w8`；要直译形态得显式加 `-O0`。比对反汇编前先把 `-O` 固定住。

2. **把 `add.s` 当成二进制里跑的那段代码。**
   纠正：`build.sh` 链接的是 gcc 重新编译的 `add.c`，二进制里是 `mul w0, w0, w1` + `add w0, w0, #0x1`；换成链接 `add.s` 才变成 `orr` + `madd`，大小从 641112 变成 641152。只对最终二进制 `objdump -d`。

3. **用 `-print-after=regalloc` 看分配结果，得到空输出。**
   纠正：实测该 flag 输出 0 字节。LLVM 14 的 AArch64 后端要用 `-print-before=greedy` / `-print-after=greedy`，或者 `-print-after=virtregrewriter` 看虚拟寄存器真正被替换的一刻。`NoVRegs` 标志出现，才说明替换完成。

4. **看到叶函数没有 `stp x29, x30` 就以为栈帧一定是 0。**
   纠正：叶函数不用保存 LR（没人覆盖 `x30`），但仍可能因局部数组、callee-saved 寄存器或对齐调整 `sp`。非叶函数**必须**保存 `x30`，实测最小形态是 `stp x29, x30, [sp, #-16]!` 加对应 `ldp`。判断依据是有没有 `bl`。

5. **把 641112 当成“这段代码的体积”。**
   纠正：`size` 显示 `text` 512834、`data` 22520、`bss` 22256，`add` 只有 12 字节；体积主体是 libc，`libc.a` 单文件就有 4939918 字节。要区分“文件大小”“可执行段”“运行时内存”。

## 本章重点整理

- 同一条 `add` 在 AArch64 上有两种实测形态：`-O0` 是 `mul w8, w1, w0` + `add w0, w8, #1`，`-O2` 是 `orr w8, wzr, #0x1` + `madd w0, w1, w0, w8`。
- IR 里只有 SSA 值和虚拟寄存器，目标相关的决定全部在 `llc` 里发生。
- `-print-before=greedy` 能同时看到虚拟 `%1:gpr32` 和物理 `$w1`；`-print-after=regalloc` 实测无输出，要用 `greedy` 或 `virtregrewriter`。
- 叶函数不保存 `x30`；非叶函数用 `stp x29, x30, [sp, #-16]!` 保存帧指针和返回地址，返回时 `ldp` 恢复，栈保持 16 字节对齐。
- `add_a64` 为 641112 字节，`size` 为 text 512834 / data 22520 / bss 22256；换成链接 `add.s` 后是 641152 字节，差 40 字节。
- 验证后端要对**最终二进制**做 `objdump`，不能只看中间 `.s`。

## 自我检核

- [ ] 能说出 `-O0` 与 `-O2` 下这条 `add` 的指令差异，并解释 `orr w8, wzr, #0x1` 在做什么
- [ ] 能解释 `%1:gpr32` 和 `$w1` 分别属于哪个阶段，以及为什么需要 `COPY`
- [ ] 知道 `-print-after=regalloc` 为什么没有输出，以及应该换成哪个 pass 名
- [ ] 能从一段 AArch64 汇编判断函数是不是叶函数，依据是哪条指令
- [ ] 能解释 `stp x29, x30, [sp, #-16]!` 里 pre-index 和 16 字节对齐各自的理由
- [ ] 能区分文件大小 641112、`text` 512834、`data` 22520、`bss` 22256 四个口径

## 延伸阅读

### 官方文档

- **[ARM Architecture Reference Manual for A-profile (ARMv8-A)](https://developer.arm.com/documentation/ddi0487/latest/)**
  - **读哪里**：`A64 Instruction Set Encoding` 与 `Load/Store`，重点看 `STP` 的 pre-index/post-index 寻址
  - **学什么**：`stp x29, x30, [sp, #-16]!` 为什么一条指令能写两个寄存器和更新基址
  - **和本章关联**：栈帧部分的权威依据；读完后能自己解码 `a9be7bfd`
- **[AAPCS64 (Procedure Call Standard for the Arm 64-bit Architecture)](https://github.com/ARM-software/abi-aa/blob/main/aapcs64/aapcs64.rst)**
  - **读哪里**：`Register Roles` 与 `Stack Alignment`
  - **学什么**：`x0`–`x7` 传参、`x30` 存返回地址、`sp` 16 字节对齐
  - **和本章关联**：解释非叶函数为什么必须存 `x30`；Ch 21 展开成完整调用约定

### 源码依据

- **[LLVM AArch64 后端源码](https://github.com/llvm/llvm-project/tree/main/llvm/lib/Target/AArch64)**
  - **读哪里**：`AArch64InstrInfo.td` 的 `MADD` 模式匹配；`AArch64RegisterInfo.cpp` 的 prologue/epilogue
  - **学什么**：`a*b+1` 怎么被 TableGen 识别成 `madd`，`stp` 怎么由 prologue 插入器生成
  - **和本章关联**：把“指令从哪来”落到具体实现文件

### 工具文档

- **[QEMU User Mode Emulation](https://www.qemu.org/docs/master/user/main.html)**
  - **读哪里**：`Command line options` 与 `-L`
  - **学什么**：`qemu-aarch64-static` 怎么跑 AArch64 二进制，何时需要 `-L`
  - **和本章关联**：本章全部运行结果都来自它

### 博客 / 工程文章

- **[Eli Bendersky — LLVM 后端系列](https://eli.thegreenplace.net/tag/llvm)**
  - **读哪里**：instruction selection、register allocation、stack frame 几篇
  - **学什么**：五个阶段怎么串起来，尤其是 SelectionDAG 到 MachineInstr 的过渡
  - **和本章关联**：本章偏“看产物”，这系列偏“看内部数据结构”

AArch64 这条链跑通了：IR、汇编、目标文件、链接、运行，五层都能对上。但 `qemu-aarch64-static` 背后还有一个 Linux 内核在提供系统调用。下一章把这个靠山拿掉，让同一类代码在 Cortex-M3 上从复位向量开始跑。

→ [Ch 19 Cortex-M 裸机后端：没有 OS 的世界](./19-cortex-m-baremetal.md)
