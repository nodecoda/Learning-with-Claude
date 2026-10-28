# Ch 20 — RISC-V 边缘后端

> **目标**：把上一章那条 `int add(int, int)` 原样喂给 RISC-V 64 后端，看清 `mulw` 与 `mul` 的语义差别、`lp64d` 与压缩指令怎么影响产物，以及 `-march=rv64gc` 打开了什么。学完这章，你能判断一份 RV64 汇编里哪些指令来自基础 ISA、哪些来自扩展，也能解释“软浮点目标文件链接硬浮点库”的失败。

> **环境**：Ubuntu 22.04 / x86_64；LLVM 14.0.0（`/usr/lib/llvm-14/bin`）；`riscv64-linux-gnu-gcc` 11.4；`qemu-riscv64-static`

## 为什么需要这个？

AArch64 是“买得到的标准”：ARM 设计核，厂商授权，生态成熟。RISC-V 走另一条路：**开放指令集**，任何人都能实现，也能按需裁剪。两条路在边缘设备上正面相遇：同样一颗低功耗核，一边 Cortex-A，一边 RV64GC。

对做 AI 编译的人，RISC-V 有三个具体影响：

1. **后端不是“又一个 target”**。RV64 的 32 位运算语义和 AArch64 不同，`mulw` 自带符号扩展。搞错这一点，`int` 到 `long` 的转换会悄悄出错。
2. **扩展决定指令可用性**。没有 M 扩展，乘法变成 `__muldi3` 库调用；没有 C 扩展，代码密度差一截；没有 D 扩展，浮点走软浮点。
3. **ABI 是链接期的硬约束**。`lp64d` 和 `lp64` 不能混链，错误信息很难懂。本章实测一次失败并解释成因。

上一章证明了“一条 C 函数能在五层产物里对上”；这一章换目标，看哪些结论不变、哪些必须重学。

## 先建立直觉

RISC-V 的 ISA 是一组可拼的**积木**：

```
RV64I        基础整数指令集（64 位地址、整数运算、load/store、分支）
  + M        整数乘除（mul / mulw / div / rem）
  + A        原子操作（lr / sc / amoswap ...）
  + F        单精度浮点
  + D        双精度浮点
  + C        压缩指令（16 位编码，汇编里带 c. 前缀）
  ─────────────────────────────────────
  = RV64GC   G = IMAFD 的简写，再加 C
```

`g` 是 `i` + `m` + `a` + `f` + `d` 的合称，所以 `rv64gc` 展开就是 `rv64imafdc`，GCC 和 LLVM 都接受两种写法。和 AArch64 对照：AArch64 指令集基本固定，靠可选扩展（NEON、SVE、PAuth）加功能；RISC-V 基础 ISA 极小，功能几乎全靠扩展拼，同一份源码可以编译到只有 I 的微控制器，也可以编译到 IMAFDC 的应用核。

直接后果是：**“RISC-V 汇编”不是确定的东西**。同一段 C，`-march=rv64i` 和 `-march=rv64gc` 出来的指令可能完全不同，读汇编前先确认 `-march`。主角仍是 `int add(int a, int b) { return a * b + 1; }`。

## 底层机制一：同一条 add 的三后端对照

AArch64（`llc -march=aarch64 -O0`，实测）：

```asm
add:                                    // @add
	mul	w8, w1, w0
	add	w0, w8, #1
	ret
```

RV64（`llc -march=riscv64`，实测）：

```asm
add:                                    # @add
	mulw	a0, a1, a0
	addiw	a0, a0, 1
	ret
```

Cortex-M3（`arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -Os`，实测）：

```asm
add:
	muls	r0, r1, r0
	adds	r0, r0, #1
	bx	lr
```

| 后端 | 乘法 | 加法 | 返回 | 算术指令数 | 含返回总条数 |
|---|---|---|---|---|---|
| AArch64（`-O0`） | `mul w8, w1, w0` | `add w0, w8, #1` | `ret` | 2 | 3 |
| RV64（`rv64gc`） | `mulw a0, a1, a0` | `addiw a0, a0, 1` | `ret` | 2 | 3 |
| Cortex-M3（`-Os`） | `muls r0, r1, r0` | `adds r0, r0, #1` | `bx lr` | 2 | 3 |

三个后端都是“一乘一加，再接一条返回”，细节全不一样。常有人把 RV64 记成“3 条”，那是把 `ret` 算进了指令数；纯算术部分三个后端都是 2 条，含返回都是 3 条。

- **AArch64** 目的寄存器是独立的 `w8`，乘完再写回 `w0`。
- **RV64** 是两操作数风格：`mulw a0, a1, a0` 把结果写回 `a0`，`addiw a0, a0, 1` 再原地加一。
- **Cortex-M3** 的 `muls r0, r1, r0` 是 Thumb 的 16 位乘法，`adds` 会更新条件标志；`bx lr` 从 `lr` 返回并切指令集状态。

**为什么 RV64 不用 `madd`？** M 扩展的乘法只有 `mul`、`mulh`、`mulhsu`、`mulhu`（RV64 另有 `mulw` 这类 `w` 变体），**没有整数乘加融合**；浮点有 `fmadd.d`，整数没有。所以 `a*b+1` 天然是两条算术指令，无法像 AArch64 的 `madd` 那样合成一条。卷积内层循环里，AArch64 能用 `madd` 把“乘 + 累加”压成一条，RV64 必须 `mul` + `add` 两条，取指带宽和发射压力都会跟着变。

## 底层机制二：mulw 不是 mul

RV64 那行 `mulw a0, a1, a0` 里的 `w` 是 **word**，指 32 位。`mulw` 的语义是：

```
取 a1 的低 32 位 × a0 的低 32 位
  → 保留乘积的低 32 位
  → 把这个 32 位结果按有符号数扩展到 64 位
  → 写入 a0
```

最后那步“符号扩展”是重点：RV64 没有独立的 32 位寄存器文件，做 32 位运算时硬件负责把结果整理成 64 位的合法形态。`mulw` 选**符号扩展**，`mul` 则是完整的 64×64→64 乘法。

一段 C 就能实测这个区别：

```c
long from32(int a, int b) { return (long)(a * b); }
```

`a*b` 是 `int` 乘法（32 位），结果再转 `long`（64 位有符号）。RV64 实测：

```asm
from32:                                 # @from32
	mulw	a0, a1, a0
	ret
```

**一条 `mulw` 就够了**：它已把 32 位结果符号扩展到 64 位，正好等于 C 的语义。同样 C 在 AArch64 上实测：

```asm
from32:                                 // @from32
	mul	w8, w1, w0
	sxtw	x0, w8
	ret
```

**两条**：`mul w8, w1, w0` 只写 `w8`，而 AArch64 规定“写 32 位 W 寄存器会把对应 X 寄存器高 32 位清零”，这是零扩展。所以要补 `sxtw x0, w8` 把 bit 31 复制到高 32 位。

两条路径：

```
RV64   mulw：32 位乘积 ──符号扩展──► 64 位 a0        （一条完成）
AArch64 mul ：32 位乘积 ──零扩展──►  x8 的高 32 位清零
        sxtw：再把 bit31 铺满高 32 位 ──► x0          （两条完成）
```

**这就是“同一个 `mul`，语义不同”的地方。** RV64 的 `mul` 是 64 位乘积，和 `mulw` 不是一回事；AArch64 的 `mul w8, ...` 是零扩展的 32 位结果。比对时看到 `mul`，先问位宽和高位怎么填。

同理，`addiw` 的 `w` 是“32 位加法 + 符号扩展”，与 `addi` 的差别在高 32 位怎么来。RV64 做 32 位 `int` 运算时，`addw`/`addiw`/`mulw`/`sllw` 是配套的一组，不能换成 `add`/`addi`。

## 底层机制三：ABI lp64d、软浮点默认与一次真实链接失败

RISC-V 64 位有两个常见 ABI：

| ABI | 浮点参数怎么传 | 适用 |
|---|---|---|
| `lp64` | 浮点参数走整数寄存器或栈（软浮点） | 无 F/D 扩展的核 |
| `lp64d` | 浮点参数走 `fa0`–`fa7`（硬浮点，双精度） | 有 F+D 扩展的核 |

`d` 指双精度浮点。`code/06_riscv/build.sh` 里这条命令有坑：

```bash
"$LLVM_BIN/llc" -march=riscv64 -filetype=obj add.ll -o add_rv.o
```

反汇编（实测）：

```text
0000000000000000 <add>:
   0:	02a5853b          	.4byte	0x2a5853b
   4:	2505                	.2byte	0x2505
   6:	8082                	.2byte	0x8082
```

**全是 `.4byte` / `.2byte`，因为 objdump 按 ELF 的架构标记解码**，而这个文件标记 `rv64i`，不认 M/C。同一份字节配上正确标记就能解码：

```text
0000000000000000 <add>:
   0:	02a5853b          	mulw	a0,a1,a0
   4:	2505                	addiw	a0,a0,1
   6:	8082                	ret
```

两个文件的 `.text` 段逐字节相同（`3b 85 a5 02 05 25 82 80`），只是标记不同。而默认文件的 ELF 属性是：

```text
Tag_RISCV_arch: "rv64i2p0"
Flags: 0x0
```

（`readelf -A` 原输出还有一行 `Tag_RISCV_stack_align: 16-bytes`，与本题无关，这里略去。）**代码用了 M 和 C，元数据却声明只有 `rv64i`，ABI 还是软浮点。** 这些 feature 来自 `clang` 写进 IR 的函数属性，不是 `-march=riscv64` 给的：

```llvm
attributes #0 = { ... "target-features"="+64bit,+a,+c,+d,+f,+m" }
```

`llc` 用这些 feature 生成指令，但输出元数据时用的是 `-march=riscv64` 的默认 subtarget（`rv64i`）和默认 `-target-abi`（`lp64`）。**指令和元数据来自两套默认值**。拿它链接默认 `lp64d` 的 gcc 运行库，实测：

```text
/usr/lib/gcc-cross/riscv64-linux-gnu/11/../../../../riscv64-linux-gnu/bin/ld: add_rv.o: can't link soft-float modules with double-float modules
/usr/lib/gcc-cross/riscv64-linux-gnu/11/../../../../riscv64-linux-gnu/bin/ld: failed to merge target specific data of file add_rv.o
collect2: error: ld returned 1 exit status
```

**链接器看元数据，不看反汇编。** 两种 ABI 对“浮点参数放哪”的约定不同，混链会让双方对寄存器的理解错位。更值得注意的是 `build.sh` 的写法：

```bash
riscv64-linux-gnu-gcc -static -O1 drv.c add_rv.o -o add_rv 2>/dev/null \
  || riscv64-linux-gnu-gcc -static -O2 add.c drv.c -o add_rv
```

第一行失败后，`||` 用 gcc 重编，**链接成功，`add(3,4) = 13` 照常打印**，但 `add_rv.o` 根本没进最终二进制。`bash -x` 实测：

```text
+ riscv64-linux-gnu-gcc -static -O1 drv.c add_rv.o -o add_rv
+ riscv64-linux-gnu-gcc -static -O2 add.c drv.c -o add_rv
+ qemu-riscv64-static ./add_rv
add(3,4) = 13
```

**回退分支把错误藏起来了**：产物在、程序能跑、数字正确，但验证的已不是 `llc` 那条路径。修法是让元数据与代码对齐，实测：

```bash
/usr/lib/llvm-14/bin/llc -march=riscv64 -mattr=+m,+a,+f,+d,+c \
  -target-abi=lp64d -filetype=obj add.ll -o add_full.o
riscv64-linux-gnu-gcc -static -O1 drv.c add_full.o -o add_full
qemu-riscv64-static ./add_full
```

产出属性与结果：

```text
Tag_RISCV_arch: "rv64i2p0_m2p0_a2p0_f2p0_d2p0_c2p0"
Flags: 0x5, RVC, double-float ABI
add(3,4) = 13
```

`-mattr` 决定**代码能用什么指令**，`-target-abi` 决定**元数据声明哪种调用约定**，两者必须一致。细节：`llc` 不认 `-mabi=lp64d`，实测报错并提示 `--meabi=lp64d`；它用的是 `-target-abi`。

如果 IR 里**没有** `target-features`（比如手写 IR），`llc -march=riscv64` 就真的只有 `rv64i`，乘法退化成库调用。实测（节选，省略调用前三条搬运参数的 `mv`）：

```asm
	addi	sp, sp, -16
	sd	ra, 8(sp)
	call	__muldi3@plt
	addiw	a0, a0, 1
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

**同一个 `-march=riscv64`，输出取决于 IR 带不带 feature 属性。**

## 底层机制四：压缩指令与代码密度

C 扩展把常见指令编码成 16 位，汇编里带 `c.` 前缀，但**反汇编器默认可能显示成别名**。同一条 `add`，用 `riscv64-linux-gnu-gcc -march=rv64gc -mabi=lp64d -O2` 编译（gcc 的乘数顺序与前面 llc 相反，语义相同），实测：

```text
0000000000000000 <add>:
   0:	02b5053b          	mulw	a0,a0,a1
   4:	2505               	addiw	a0,a0,1
   6:	8082               	ret
```

只占 **8 字节**：`mulw` 32 位（4 字节），`addiw` 和 `ret` 各压成 16 位（2 字节）。不带 C 扩展的 `riscv64-linux-gnu-gcc -march=rv64imafd -mabi=lp64d -O2` 实测：

```text
0000000000000000 <add>:
   0:	02b5053b          	mulw	a0,a0,a1
   4:	0015051b          	addiw	a0,a0,1
   8:	00008067          	ret
```

三条都是 32 位，占 **12 字节**；`size` 实测也一致（`add_gc.o` text 8，`add_noc.o` text 12）。放大到循环更有意义：带循环和乘加的函数（`f(int *p, int n)`，内含 `p[i] * 3 + 1` 累加），rv64imafd 的 `text` 是 **72**，rv64gc 是 **50**，减少约 31%。循环里的 `lw`、`addi`、`addw`、`addiw`、`mv`、`jr` 都命中了压缩形式，每条省 2 字节；上例的 `bne a4, a2` 仍是 32 位，因为 C 扩展只有 `c.beqz`/`c.bnez`（与零比较），没有两寄存器比较分支的压缩形式。

要看清哪些指令真的被压缩，用 `riscv64-linux-gnu-objdump -d -M no-aliases dens_rv64gc.o`，实测节选：

```text
  14:	872a                	c.mv	a4,a0
  1a:	4314                	c.lw	a3,0(a4)
  1c:	0711                	c.addi	a4,4
  24:	2785                	c.addiw	a5,1
  2c:	8082                	c.jr	ra
```

去掉 `-M no-aliases` 后，同一段会显示成 `mv`、`lw`、`addi`、`addiw`、`ret`，16 位编码的信息消失，容易让人误判代码长度——**看密度要盯字节数。** 代价也要说清楚：C 扩展让**指令长度可变**（16 位和 32 位混排），取指和译码比定长指令集复杂；对编译器，这影响对齐和分支偏移计算。RISC-V 把它做成可选扩展，正是“可裁剪”的体现：微控制器可以只要 `rv32imc`，应用核才上 `rv64gc`。

## 底层机制五：模块化扩展的收益与代价

**收益**三条：

1. **可裁剪**。只有 `rv32imc` 的核不需要浮点单元、不需要原子扩展，面积和功耗都能压下来；ARM 的 Cortex-M0/M3/M7 是固定档位。
2. **无授权费**。学术核、开源核、自研核都能进同一套工具链。
3. **扩展可增量**。要向量加 V，要位操作加 Zbb/Zba，不必重做整个 ISA。

**代价**同样直接打到编译器上：

- **生态碎片化**。“RISC-V 能跑”不等于“这份二进制能在目标核上跑”。`-march` 少写一个扩展，可能编译期失败，也可能链接期才失败（本章的 soft-float/double-float 就是后者）。
- **支持差异**。`__int128` 在 RV64 上要多条指令拼，在 Cortex-M3 上直接编译失败。跨后端移植前先查数据宽度。
- **默认值陷阱**。LLVM 14 的指令 feature 和 ELF 元数据来自两套默认值，这类坑比 AArch64 多。

`__int128` 是一个具体例子：

```c
__int128 widen(long a) { return (__int128)a * 3; }
```

RV64 实测：

```asm
widen:                                  # @widen
	li	a1, 3
	mulh	a1, a0, a1
	slli	a2, a0, 1
	add	a0, a0, a2
	ret
```

`mulh` 取 64×64 乘积的高 64 位，`slli` + `add` 算低 64 位（`a*2 + a`），两条 64 位结果拼成 128 位，分别放 `a1:a0`。AArch64 实测：

```asm
widen:                                  // @widen
	mov	w8, #3
	smulh	x1, x0, x8
	add	x0, x0, x0, lsl #1
	ret
```

也是“取高位 + 移位加法”，但 AArch64 的 `smulh` 一条就拿到高位；Cortex-M3 根本编不过。模块化让“为边缘裁剪”成为可能，代价是“每换一个核都要重新确认扩展和 ABI”，部署时要当成一项显式检查。

## 可跑例子 1：跑通 build.sh 并看清它到底跑了什么

```bash
cd compilers/ai_to_edge/code/06_riscv && ./build.sh
```

实测输出（完整）：

```text
== 1) IR ==
== 2) RV64 汇编（看 mul / addi） ==
	mulw	a0, a1, a0
	addiw	a0, a0, 1
	ret
== 3) 链接 + qemu ==
add(3,4) = 13
```

逐段对应：

- 第 1 段生成 `add.ll`，triple 是 `riscv64-unknown-linux-gnu`，签名带 `signext`：`define dso_local signext i32 @add(i32 noundef signext %0, i32 noundef signext %1)`。它告诉后端“32 位参数在 64 位寄存器里按有符号扩展”，RV64 的 `mulw`/`addiw` 正好满足，不需要额外的 `sext.w`。
- 第 2 段用 `llc -march=riscv64` 出汇编，`grep` 只留 `mul|addi|li|ret`。
- 第 3 段出目标文件、链接、qemu 运行。**注意回退分支**：`add_rv.o` 因 ABI 不匹配链接失败时，脚本用 gcc 重编 `add.c`，`13` 仍然正确，但验证路径换了。用 `bash -x ./build.sh` 确认。

`add(3,4) = 13` 和 AArch64 一致——C 语义没变，变的是指令和 ABI。

## 可跑例子 2：自己对齐 march 与 ABI，并验证压缩指令

把“工具链默认值”变成可检查的东西：

```bash
mkdir -p /tmp/rvlab && cd /tmp/rvlab
cp /home/ubuntu/Learning-with-Claude/compilers/ai_to_edge/code/06_riscv/drv.c .
printf 'int add(int a, int b) { return a * b + 1; }\n' > add.c
/usr/lib/llvm-14/bin/clang --target=riscv64-linux-gnu -O2 -S -emit-llvm add.c -o add.ll

# 默认：只有基础 ISA，软浮点
/usr/lib/llvm-14/bin/llc -march=riscv64 -filetype=obj add.ll -o add_default.o
riscv64-linux-gnu-readelf -A add_default.o

# 对齐：M/A/F/D/C 扩展 + lp64d
/usr/lib/llvm-14/bin/llc -march=riscv64 -mattr=+m,+a,+f,+d,+c \
  -target-abi=lp64d -filetype=obj add.ll -o add_full.o
riscv64-linux-gnu-readelf -h add_full.o | grep Flags
riscv64-linux-gnu-objdump -d add_full.o

riscv64-linux-gnu-gcc -static -O1 drv.c add_full.o -o add_full
qemu-riscv64-static ./add_full
```

实测输出（节选，`readelf -A` 略去 `Tag_RISCV_stack_align` 一行）：

```text
Tag_RISCV_arch: "rv64i2p0"

  Flags:                             0x5, RVC, double-float ABI

0000000000000000 <add>:
   0:	02a5853b          	mulw	a0,a1,a0
   4:	2505                	addiw	a0,a0,1
   6:	8082                	ret

add(3,4) = 13
```

四件事对上了：默认属性只有 `rv64i`；对齐后有 `RVC` 和 `double-float ABI`；`addiw`、`ret` 各 2 字节（地址差 0 → 4 → 6 → 8）；链接运行成功。换成 `add_default.o` 就会复现上一节那条链接错误。

## 踩雷集锦

1. **把 `mulw` 当成 `mul` 的别名。**
   纠正：`mulw` 是 32 位乘法再把结果**符号扩展**到 64 位；`mul` 是完整 64 位乘法。同一段 `(long)(a*b)`，RV64 一条 `mulw` 就够，AArch64 要 `mul` + `sxtw`，因为 AArch64 写 W 寄存器是**零扩展**。跨后端比对时先确认位宽和扩展方向。

2. **以为“反汇编里有 `mulw`”就等于“目标文件声明了 M 扩展”。**
   纠正：实测默认 `add_rv.o` 的代码是 `mulw` + 压缩指令，ELF 属性却写着 `rv64i2p0`、`Flags: 0x0`（软浮点 ABI）。指令来自 IR 的 `target-features`，元数据来自 `-march=riscv64` 的默认 subtarget，两套默认值不一致。要一致，显式加 `-mattr=+m,+a,+f,+d,+c -target-abi=lp64d`；`-march=rv64gc` 是驱动的写法，`llc` 不吃。

3. **看到 `add(3,4) = 13` 就认为 `llc` 产物验证过了。**
   纠正：目标文件链接因 ABI 不匹配失败，`||` 回退到 gcc 重编，输出照样是 13。用 `bash -x ./build.sh` 看实际命令，或检查最终二进制里 `add` 的字节。

4. **用助记符数量估算代码密度。**
   纠正：压缩指令默认显示成别名，`c.addi` 会显示为 `addi`。实测同一循环函数 `text` 是 rv64imafd 72 字节、rv64gc 50 字节；按字节算，并用 `-M no-aliases` 看 `c.` 前缀。

5. **认为 RISC-V 上 `__int128` 一定可用。**
   纠正：RV64 能编，实测用 `mulh` + `slli` + `add` 拼出 128 位结果；Cortex-M3 直接报 `__int128 is not supported on this target`。移植前先确认数据宽度。

## 本章重点整理

- RV64 的 `add` 实测是 `mulw a0, a1, a0` + `addiw a0, a0, 1` + `ret`，三条指令。
- `mulw` 是“32 位乘法 + 符号扩展”，AArch64 的 `mul w` 是“32 位乘法 + 零扩展”，所以 `(long)(a*b)` 在 AArch64 上还要补 `sxtw`。
- `llc -march=riscv64` 的默认元数据是 `rv64i` + 软浮点 ABI，即使 IR 的 `target-features` 让它生成了 `mulw` 和压缩指令；和 `lp64d` 运行库链接会报 `can't link soft-float modules with double-float modules`。
- 手写 IR 若没有 `target-features`，`llc -march=riscv64` 连乘法都退化成 `call __muldi3@plt`。
- `rv64gc` = `rv64imafdc`；C 扩展把高频指令压成 16 位，实测小函数 text 8 vs 12 字节，循环函数 50 vs 72 字节。
- `06_riscv/add_rv` 实测 503760 字节，`size` 为 text 394979 / data 22088 / bss 22184。
- RISC-V 的优势是可裁剪、无授权费、扩展可增量；代价是生态碎片化、默认值陷阱、`__int128` 这类支持差异。

## 自我检核

- [ ] 能写出 RV64 上 `a*b+1` 的三条指令，并说明每条的作用
- [ ] 能解释 `mulw` 与 `mul` 的语义差别，以及它为什么让 `(long)(a*b)` 少一条指令
- [ ] 知道 `rv64gc` 展开成哪几个扩展，`g` 代表什么
- [ ] 能解释“反汇编有 `mulw`、ELF 却写 `rv64i`”的成因，以及 `can't link soft-float modules with double-float modules` 的修法
- [ ] 能用字节数而不是助记符数量比较 rv64gc 与 rv64imafd 的代码密度
- [ ] 能说出 RISC-V 在边缘设备上的两个收益和两个代价

## 延伸阅读

### 官方文档

- **[The RISC-V Instruction Set Manual, Volume I: Unprivileged ISA](https://github.com/riscv/riscv-isa-manual/releases)**
  - **读哪里**：Chapter 2（RV64I）、Chapter 7（M 扩展）、Chapter 12（C 扩展）
  - **学什么**：`mulw`/`addiw` 的符号扩展规则、压缩指令的编码约束
  - **和本章关联**：本章所有指令语义的权威来源
- **[RISC-V ELF psABI Specification](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)**
  - **读哪里**：`Integer Calling Convention`、`Floating-point Calling Convention`、`LP64D`
  - **学什么**：`lp64` 与 `lp64d` 的参数传递差异，为什么不能混链
  - **和本章关联**：解释本章那次链接失败的根因

### 源码依据

- **[LLVM RISC-V 后端](https://github.com/llvm/llvm-project/tree/main/llvm/lib/Target/RISCV)**
  - **读哪里**：`RISCVInstrInfoM.td` 的 `MULW` 定义、`RISCVSubtarget.cpp` 的默认 feature
  - **学什么**：`mulw` 的模式匹配怎么带出符号扩展，`llc` 默认 feature 与 ELF 标记从哪来
  - **和本章关联**：把“代码用 M、元数据写 rv64i”落到代码

### 工具文档

- **[GNU Binutils — RISC-V Options](https://sourceware.org/binutils/docs/as/RISC-V-Options.html)**
  - **读哪里**：`-march`、`-mabi`、`-mattr` 的取值表
  - **学什么**：扩展命名规则（`i2p0` 这类后缀），`-mabi` 与 `-march` 的配合
  - **和本章关联**：本章所有命令行参数的依据

### 博客 / 工程文章

- **[MaskRay — RISC-V 工具链笔记](https://maskray.me/blog/)**
  - **读哪里**：搜 RISC-V 文章，重点看 linker relaxation 和 ABI 标记
  - **学什么**：为什么目标文件要带 `Tag_RISCV_arch`，链接器怎么用它做兼容检查
  - **和本章关联**：本章 `readelf -A` 输出背后的机制

RV64 的指令和 ABI 都对齐了，但还有一个问题没回答：为什么 MLIR 降级出来的 `matmul` 会变成 21 个参数？下一章把 ABI 拉到显微镜下，逐个寄存器数给你看。

→ [Ch 21 ABI 与调用约定：memref 为什么会变成七个参数](./21-abi-calling-convention.md)
