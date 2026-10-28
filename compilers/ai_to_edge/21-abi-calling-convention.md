# Ch 21 — ABI 与调用约定：memref 为什么会变成七个参数

> **目标**：搞清 ABI 怎么把“一个参数”翻译成“哪个寄存器、哪个栈槽”，然后用它解释两件看起来很奇怪的事：为什么 MLIR 的 `matmul` 有 21 个参数，为什么 12 个 `int` 参数里后 4 个要从 `[sp]` 读。学完这章，你能拿到任意一份 AArch64 汇编，反推出调用双方各自把值放哪。

> **环境**：Ubuntu 22.04 / x86_64；`aarch64-linux-gnu-gcc` 11.4；LLVM 14.0.0（`/usr/lib/llvm-14/bin`）；`qemu-aarch64-static` 6.2；MLIR 14

## 为什么需要这个？

Ch 12 里 MLIR 把 `linalg.matmul` 降到 LLVM IR，签名变成这样：

```llvm
define void @matmul(float* %0, float* %1, i64 %2, i64 %3, i64 %4, i64 %5, i64 %6,
                    float* %7, float* %8, i64 %9, i64 %10, i64 %11, i64 %12, i64 %13,
                    float* %14, float* %15, i64 %16, i64 %17, i64 %18, i64 %19, i64 %20)
```

（为排版分成三行；`matmul.ll` 里是同一行。）

**21 个参数**，而源程序只有 3 个 `memref`：

```mlir
func @matmul(%A: memref<4x8xf32>, %B: memref<8x4xf32>, %C: memref<4x4xf32>)
```

3 变 21 是两件事叠加：**memref 是描述符结构体**，**ABI 把结构体拍平成标量再按规则分配寄存器**。任一步理解错，写驱动、对签名、查崩溃都会卡住。本章回答：7 个分量是什么；21 个参数怎么分寄存器与栈；参数多是否真的慢；生产编译器为什么不这么传。

## 先建立直觉

把 ABI 想成一套**邮局分拣规则**：调用者手里有一堆值要寄，被调用者有一排固定编号的柜子：

```
参数 1  ─────────► x0        参数 9  ──┐
参数 2  ─────────► x1        参数 10 ──┤ 柜子满了
...             ...           ...     ├──► 栈：[sp], [sp,8],
参数 8  ─────────► x7        参数 13 ──┘        [sp,16], ...
```

规则很死：

- 整数/指针参数依次用 `x0`–`x7`，用完进栈。
- 浮点参数依次用 `v0`–`v7`（`d0`–`d7` 是低 64 位视图），用完也进栈。
- **两排柜子互不占用**：`f(int, double, int, double)` 的整数参数仍从 `x0` 数起。
- 栈槽 8 字节一个，调用点 `sp` 必须 16 字节对齐。
- 返回值：整数/指针放 `x0`，浮点放 `v0`，大结构体用隐藏指针放 `x8`。

memref 不是“一个指针”，而是 7 字段结构体；3 个拍平就是 21 个标量，再按上面的规则分柜子。

## 底层机制一：memref 是描述符，不是指针

`memref<4x8xf32>` 降级时对应这个 LLVM 结构体（Ch 12 已出现）：

```llvm
!llvm.struct<(ptr<f32>, ptr<f32>, i64, array<2 x i64>, array<2 x i64>)>
```

按字段拆开：

| 序号 | 字段 | 含义 | 为什么需要 |
|---|---|---|---|
| 0 | `allocated ptr` | `alloc` 返回的基地址 | 释放时还给分配器 |
| 1 | `aligned ptr` | 实际访存地址 | 可能做过对齐调整 |
| 2 | `offset` | 下标 0 对应的偏移 | 支持 subview |
| 3–4 | `size[0..1]` | 各维长度 | 循环上界、边界检查 |
| 5–6 | `stride[0..1]` | 各维步长（元素数） | 非连续布局、转置 |

**为什么两个指针？** 因为“分配地址”和“视图起点”可以不同：`subview` 指向大缓冲区中间时，`aligned ptr` 是视图起点，`allocated ptr` 仍指向原始分配，`free` 必须用后者。**为什么 offset 和 stride 都要有？** 下标公式是：

```
address(i, j) = aligned_ptr + (offset + i*stride[0] + j*stride[1]) * sizeof(T)
```

`offset` 管“视图从哪开始”，`stride` 管“每维跨多远”；只有 size 没 stride 就无法表达转置或 padding。只传 `float*` 会丢形状、步长和原始分配地址。kernel 签名总是“指针 + 一堆 i64”，不是不会打包，而是**降级到 LLVM 方言时结构体参数被拍平成了标量**。

## 底层机制二：21 个参数怎么落到寄存器和栈

按 7 个一组切开：`%0`–`%6` 是 `%A`，`%7`–`%13` 是 `%B`，`%14`–`%20` 是 `%C`；组内顺序是 `allocated, aligned, offset, size0, size1, stride0, stride1`。对照 `matmul.ll` 的 `insertvalue`：

```llvm
%22 = insertvalue { float*, float*, i64, [2 x i64], [2 x i64] } undef, float* %0, 0
%23 = insertvalue ... %22, float* %1, 1
%24 = insertvalue ... %23, i64 %2, 2
%25 = insertvalue ... %24, i64 %3, 3, 0
%26 = insertvalue ... %25, i64 %5, 4, 0
%27 = insertvalue ... %26, i64 %4, 3, 1
%28 = insertvalue ... %27, i64 %6, 4, 1
```

`3, 0` / `3, 1` 是 `size`，`4, 0` / `4, 1` 是 `stride`。注意第 5、6 行：`insertvalue` 的**执行顺序**是先把 `stride[0]=%5` 塞进 `4, 0`，再把 `size[1]=%4` 塞进 `3, 1`，和字段的最终排列顺序不同；写成 `%26`（`%5`）在前、`%27`（`%4`）在后才是文件里的真实顺序。**看参数编号不要顺着文本序号猜字段，要看末尾的字段下标。** 字段固定为 `size[0]=%3`、`size[1]=%4`、`stride[0]=%5`、`stride[1]=%6`。前 8 个参数进寄存器：

| 参数 | 值 | 寄存器 |
|---|---|---|
| `%0` | A.allocated | `x0` |
| `%1` | A.aligned | `x1` |
| `%2` | A.offset | `x2` |
| `%3` | A.size0 | `x3` |
| `%4` | A.size1 | `x4` |
| `%5` | A.stride0 | `x5` |
| `%6` | A.stride1 | `x6` |
| `%7` | B.allocated | `x7` |

**`x0`–`x7` 到这里满了**，剩下 13 个参数全部进栈：

| 参数 | 值 | 栈位置 |
|---|---|---|
| `%8`–`%13` | B.aligned/offset/size0/size1/stride0/stride1 | `[sp]` 到 `[sp, 40]` |
| `%14`–`%20` | C 的 7 个字段 | `[sp, 48]` 到 `[sp, 96]` |

13 个槽 × 8 字节 = **104 字节**传出参数区。实测汇编能验证这张表，`llc -march=aarch64 -O2 matmul.ll` 的函数开头是：

```asm
matmul:                                 // @matmul
	.cfi_startproc
// %bb.0:
	mov	x8, xzr
	ldr	x9, [sp, #56]
	ldr	x10, [sp]
	b	.LBB0_2
```

`[sp, #56]` 正是 `%15`（C.aligned），`[sp]` 是 `%8`（B.aligned）。**两个都是 aligned ptr**——循环体访存用 aligned，allocated 只在释放时用，而这个 kernel 不释放。

## 底层机制三：12 个 int 实测，第 9 个开始从栈读

21 个参数混着指针和 i64，不容易看清寄存器何时用完，换一个纯整数对照组：

```c
int32_t twelve(int32_t a,int32_t b,int32_t c,int32_t d,int32_t e,int32_t f,
               int32_t g,int32_t h,int32_t i,int32_t j,int32_t k,int32_t l) {
  return a+b+c+d+e+f+g+h+i+j+k+l;
}
```

`aarch64-linux-gnu-gcc -O1 -S` 实测：

```asm
twelve:
	.cfi_startproc
	add	w0, w0, w1
	add	w0, w0, w2
	add	w0, w0, w3
	add	w0, w0, w4
	add	w0, w0, w5
	add	w0, w0, w6
	add	w0, w0, w7
	ldr	w1, [sp]
	add	w0, w0, w1
	ldr	w1, [sp, 8]
	add	w0, w0, w1
	ldr	w1, [sp, 16]
	add	w0, w0, w1
	ldr	w1, [sp, 24]
	add	w0, w0, w1
	ret
```

逐个对上：

- 前 7 条 `add` 用 `w0`–`w7`，对应参数 `a`–`h`。**`w0` 既是第一个参数也是累加器**，所以第一条是 `add w0, w0, w1`。
- 第 9 个参数 `i` 从 `[sp]` 读，第 10 个 `j` 从 `[sp, 8]`，第 11 个 `k` 从 `[sp, 16]`，第 12 个 `l` 从 `[sp, 24]`。
- **步进 8 字节，不是 4 字节。** 参数虽是 `int32_t`，栈槽仍按 8 字节对齐：低 4 字节放值，高 4 字节填充。
- 读出的值都放进 `w1` 再相加，`w0` 保持累加器角色；`w1` 已空出，复用省一次寄存器选择。

调用者那一侧对应实测（另一个编译单元里写 `twelve(1,2,...,12)`，`aarch64-linux-gnu-gcc -O1 -S`）：

```asm
	mov	w0, 12
	str	w0, [sp, 24]
	mov	w0, 11
	str	w0, [sp, 16]
	mov	w0, 10
	str	w0, [sp, 8]
	mov	w0, 9
	str	w0, [sp]
	mov	w7, 8
	mov	w6, 7
	mov	w5, 6
	mov	w4, 5
	mov	w3, 4
	mov	w2, 3
	mov	w1, 2
	mov	w0, 1
	bl	twelve
```

**栈上参数的存放顺序和寄存器参数的装载顺序相反**：先把 12、11、10、9 从高地址往低地址存（`[sp,24]` → `[sp]`），再 `mov w7..w0`，最后 `bl`。先做“往内存写”的事再统一准备寄存器，能减少依赖停顿。

栈帧大小也对得上：`sub sp, sp, #48`，其中 `[sp, 32]`、`[sp, 40]` 存 `x29`/`x30`，`[sp]`–`[sp, 24]` 是 4 个传出参数槽。48 = 16（保存区）+ 32（参数区），保持 16 字节对齐。

这段调用能保留下来，前提是 `twelve` 在**另一个编译单元**，gcc 看不到函数体。同一文件里 gcc 会把整段折叠成 `mov w2, #0x4e`，见可跑例子 1。

## 底层机制四：浮点走另一排柜子

`double two_d(double, double)` 实测：

```asm
two_d:
	.cfi_startproc
	fmul	d0, d0, d1
	ret
```

两个参数在 `d0`、`d1`，结果也放 `d0`，**完全没碰 `x0`/`x1`**。`double mixed(int a, double b, int c, double d)` 里，`a` 在 `w0`、`b` 在 `d0`、`c` 在 `w1`、`d` 在 `d1`——两排各自从 0 数。`v0`–`v7` 是 128 位向量寄存器，`d0` 是低 64 位，`s0` 是低 32 位；同一物理寄存器按类型选视图。

返回值同样分两排：`int`/指针 → `x0`（`w0` 是低 32 位视图）；`double` → `d0`；`struct` 看大小，小的拆进 `x0`/`x1` 或 `d0`/`d1`，大的走隐藏指针。

## 底层机制五：sret 隐藏指针

大结构体不能塞进寄存器，ABI 规定由**调用者分配空间**，把地址作为隐藏参数传进去，放在 `x8`，叫 **sret**（structure return）。实测：

```c
struct big { long a,b,c,d; };
struct big make_big(long x) { struct big s = {x, x+1, x+2, x+3}; return s; }
```

```asm
make_big:
	.cfi_startproc
	str	x0, [x8]
	add	x1, x0, 1
	str	x1, [x8, 8]
	add	x1, x0, 2
	str	x1, [x8, 16]
	add	x0, x0, 3
	str	x0, [x8, 24]
	ret
```

`x8` 是调用者给的返回缓冲区地址，`x0` 才是 `x` 参数；四条 `str` 写进调用者栈空间，`ret` 前**不设 `x0`**。调用者一侧实测：

```asm
	sub	sp, sp, #64
	stp	x29, x30, [sp, 48]
	add	x29, sp, 48
	...
	add	x8, sp, 8
	bl	make_big
	ldr	x0, [sp, 8]
	ldr	x1, [sp, 32]
	add	x0, x0, x1
```

`add x8, sp, 8` 把缓冲区地址放进 `x8`，返回后从 `[sp, 8]`、`[sp, 32]` 读第 1、第 4 个字段，**返回值根本没过寄存器**。`memref` 不用 sret 也是同理：字段全是标量，拍平后按普通参数传比“传指针再解引用”更直接，MLIR 选**拍平**。

## 可跑例子 1：12 个参数，调用者与被调用者分别验证

```bash
mkdir -p /tmp/abilab && cd /tmp/abilab
cat > twelve.c <<'EOF'
#include <stdint.h>
int32_t twelve(int32_t a,int32_t b,int32_t c,int32_t d,int32_t e,int32_t f,
               int32_t g,int32_t h,int32_t i,int32_t j,int32_t k,int32_t l) {
  return a+b+c+d+e+f+g+h+i+j+k+l;
}
EOF
cat > drv12.c <<'EOF'
#include <stdio.h>
#include <stdint.h>
int32_t twelve(int32_t,int32_t,int32_t,int32_t,int32_t,int32_t,
               int32_t,int32_t,int32_t,int32_t,int32_t,int32_t);
int main(void) {
  int32_t r = twelve(1,2,3,4,5,6,7,8,9,10,11,12);
  printf("twelve(1..12) = %d\n", r);
  return 0;
}
EOF
aarch64-linux-gnu-gcc -O1 -c twelve.c -o twelve.o
aarch64-linux-gnu-gcc -static -O1 drv12.c twelve.o -o drv12
qemu-aarch64-static ./drv12
```

实测运行结果：

```text
twelve(1..12) = 78
```

`1+2+...+12 = 78`，说明栈上那 4 个参数确实被正确读到；如果以为步进是 4 字节，这行会变成别的数或直接崩。调用点汇编就是上一节那段 `str w0, [sp, 24]` … `bl twelve`。

**为什么 `twelve` 要单独一个编译单元？** 跨编译单元时 gcc 看不到函数体，无法内联或折叠 12 次加法。把两个函数放进同一文件再编译，实测 `main` 里只剩 `mov w2, #0x4e`（78）再接一次 `printf`，调用点完全消失——有没有 `volatile` 不是关键，**独立编译单元**才是。**观察 ABI 必须先阻止内联。**

## 可跑例子 2：21 个参数的 matmul，看调用者压栈与 callee 实际读取

```bash
cd compilers/ai_to_edge/code/02_mlir_lowering && ./build.sh
```

实测输出（末尾五行）：

```text
== 5) run under qemu ==
11.0 18.0 10.0 11.0 
12.0 20.0 19.0 12.0 
13.0 17.0 18.0 13.0 
14.0 14.0 17.0 14.0 
```

数字留给 Ch 25，这里只看调用约定。驱动 `mdrv.c` 的调用是：

```c
matmul(A,A,0, 4,8, 8,1,
       B,B,0, 8,4, 4,1,
       C,C,0, 4,4, 4,1);
```

每个 memref 传 7 个值，A 是 `allocated=A`、`aligned=A`、`offset=0`、`size0=4`、`size1=8`、`stride0=8`、`stride1=1`——正是 `memref<4x8xf32>` 的行主序布局。调用者压 13 个栈参数（实测）：

```asm
	str	x6, [sp, 96]        // C.stride1
	mov	x3, 4
	str	x3, [sp, 88]        // C.stride0
	str	x3, [sp, 80]        // C.size1
	str	x3, [sp, 72]        // C.size0
	str	xzr, [sp, 64]       // C.offset = 0
	add	x20, sp, 120
	str	x20, [sp, 56]       // C.aligned
	str	x20, [sp, 48]       // C.allocated
	str	x6, [sp, 40]        // B.stride1
	str	x3, [sp, 32]        // B.stride0
	str	x3, [sp, 24]        // B.size1
	mov	x4, 8
	str	x4, [sp, 16]        // B.size0
	str	xzr, [sp, 8]        // B.offset
	str	x7, [sp]            // B.aligned
	mov	x5, x4
	mov	x2, 0
	mov	x1, x0
	bl	matmul
```

`[sp]`–`[sp, 96]` 共 13 个槽，和上一节的表完全对应；`xzr` 就是 offset 0。callee 侧：

```asm
matmul:
	mov	x8, xzr
	ldr	x9, [sp, #56]
	ldr	x10, [sp]
	b	.LBB0_2
```

**栈上只读 2 个槽**：`[sp]` 是 B.aligned，`[sp, #56]` 是 C.aligned；其余 11 个栈参数一条没读。加上寄存器里的 A.aligned（`x1`），整个函数只用 3 个字段——`memref` 是**静态形状**，上界 4/4/8 和 stride 8/1 编译期已知。

`matmul` 是叶子函数，**没有 `stp x29, x30`**，也没有 `sub sp`；`[sp]` 就是调用者栈顶，所以偏移是正的。

### 生产编译器为什么不这么传

21 个参数不是“最终形态”，而是 MLIR 降级路径的中间产物。上设备时参数形态会变成：

| 形态 | 参数个数 | 代价 | 适用 |
|---|---|---|---|
| 拍平描述符（实测） | 3 × 7 = 21 | 寄存器压力大，13 个进栈 | 简单 kernel、调试 |
| 结构体指针 | 3 | callee 多几次 `ldr` | 通用库、反复调用 |
| 全局常量描述符 | 0–3 | 元数据编译期内联 | 静态形状、单模型 |

打包成结构体指针后，签名从 21 个降到 3 个。实测 `get(desc *d, long i, long j)`：

```asm
get:
	ldp	x4, x3, [x0, 40]
	mul	x2, x2, x3
	ldr	x3, [x0, 16]
	madd	x1, x1, x4, x2
	ldr	x0, [x0, 8]
	add	x1, x1, x3
	ldr	s0, [x0, x1, lsl 2]
	ret
```

在热循环里，拍平形态每次调用都要由调用者重复那 13 次 `str`，callee 再按需 `ldr` 取出；这就是它的真实代价——不是参数个数，而是每次调用固定多出的栈流量。链条是：**21 个活值挤占 `x0`–`x7` → 装不下的进栈 → callee 每条 `ldr` 一次访存**；如果 callee 还要在这些值之上做计算，寄存器不够用时编译器会再产生 spill，栈访问次数继续增加。

`x0` 是描述符指针，`i`/`j` 在 `x1`/`x2`。**3 条 load 指令取 4 个字段**（`ldp` 取两个 stride，两次 `ldr` 取 offset 和 aligned ptr）换掉 13 个栈参数：只调一次时拍平更划算，循环里反复调用时指针形态的寄存器压力小得多。

再进一步是**全局常量**：形状和步长编译期已知，`offset`、`stride` 直接内联成立即数，连描述符都不用传。这就是 Ch 22 内存规划要做的事——把描述符元数据在编译期消掉，只留裸指针。

**判断标准**：看 callee 实际用了几份数据。实测 `matmul` 只碰了三个 aligned ptr（`x1` 是 A.aligned，`[sp]` 给 B.aligned，`[sp, #56]` 给 C.aligned），21 个字段里 18 个是死数据。成熟编译器一般在 lower 阶段就做这项分析，而不是等寄存器分配完才发现；这一步属于工程实践描述，**未实测**。

## 踩雷集锦

1. **以为 memref 就是“一个指针”。**
   纠正：它是 7 字段描述符。只传 `float*` 会丢形状、步长和原始分配地址；驱动必须按 7 个值一组传，少一个后面全错位。实测 `matmul` 的 21 个参数就是 3 × 7。

2. **以为参数多就一定慢。**
   纠正：要看 callee 实际读多少、能不能内联。实测 `matmul` 压了 13 个栈槽，callee 只 `ldr` 了 2 个；同一编译单元内 `twelve(1..12)` 直接折叠成 `mov w2, #78`。代价是“没被优化掉的栈流量”，不是参数个数。

3. **以为 12 个 `int` 都能进寄存器，或者栈上按 4 字节紧凑排。**
   纠正：`x0`–`x7` 只有 8 个。实测第 9–12 个从 `[sp]`、`[sp, 8]`、`[sp, 16]`、`[sp, 24]` 读，**步进 8 字节**；`int32_t` 只占低 4 字节，高 4 字节是填充。

4. **以为浮点参数也占 `x0`–`x7`。**
   纠正：浮点走 `v0`–`v7`，与整数是两排。实测 `two_d` 只用 `d0`、`d1`；`f(int, double, int, double)` 里两个 `int` 仍从 `x0`、`x1` 开始。

5. **以为大结构体返回值在 `x0`。**
   纠正：大结构体走 sret，隐藏指针在 `x8`。实测 `make_big` 用 `str x0, [x8]` 写调用者缓冲区，`ret` 前不设返回值寄存器。

## 本章重点整理

- `memref<MxNxT>` 降级成 `{ptr, ptr, i64, [2 x i64], [2 x i64]}`，7 个字段：allocated、aligned、offset、size0、size1、stride0、stride1。
- 3 个 memref 拍平成 **21 个参数**；前 8 个进 `x0`–`x7`，其余 13 个进栈，占 104 字节。
- 12 个 `int` 实测：`w0`–`w7` 用完后，第 9–12 个从 `[sp]`、`[sp, 8]`、`[sp, 16]`、`[sp, 24]` 读，栈槽 8 字节步进。
- 浮点参数走 `v0`–`v7`，返回走 `v0`；整数/指针返回走 `x0`。
- 大结构体返回用 sret：隐藏指针放 `x8`，调用者分配缓冲区。
- 参数多不等于慢：`matmul` 的 callee 在 `-O2` 下只读了 13 个栈槽中的 2 个；同一编译单元内的常量调用会被完全折叠。
- 生产编译器会把描述符打包成结构体指针（3 个参数）或全局常量；用几次 `ldr` 换掉 13 个栈参数，寄存器压力小得多。

## 自我检核

- [ ] 能写出 memref 描述符的 7 个字段，并说明 allocated 与 aligned 为什么都要有
- [ ] 能解释 3 个 memref 为什么变成 21 个参数，而不是 3 个或 9 个
- [ ] 能说出 21 个参数里哪些进 `x0`–`x7`，第 9 个参数在栈的哪个偏移
- [ ] 能解释 12 个 `int32_t` 的栈槽为什么是 8 字节步进，而不是 4 字节
- [ ] 能说明浮点与整数各用哪排寄存器，返回值分别放哪，sret 的 `x8` 怎么认
- [ ] 能说出“拍平描述符”和“结构体指针”两种传参形态各自的代价

## 延伸阅读

### 官方文档

- **[AAPCS64 — Procedure Call Standard for the Arm 64-bit Architecture](https://github.com/ARM-software/abi-aa/blob/main/aapcs64/aapcs64.rst)**
  - **读哪里**：`Parameter Passing`、`Result Return`、`Stack`，重点看 `Stage B`
  - **学什么**：`x0`–`x7` / `v0`–`v7` 分配顺序、8 字节栈槽、sret 的 `x8`
  - **和本章关联**：本章全部实测现象的规定来源
- **[MLIR MemRef Dialect](https://mlir.llvm.org/docs/Dialects/MemRef/)**
  - **读哪里**：`Memory Layout` 与 `memref` 类型描述
  - **学什么**：offset/size/stride 的语义，为什么需要两个指针
  - **和本章关联**：解释 7 个字段各自解决什么问题

### 源码依据

- **[MLIR 到 LLVM 的 memref 降级](https://github.com/llvm/llvm-project/blob/main/mlir/lib/Conversion/MemRefToLLVM/MemRefToLLVM.cpp)**
  - **读哪里**：`MemRefDescriptor` 定义与 `getStridesAndOffset`
  - **学什么**：7 个字段怎么排、`populateFuncOpConversionPattern` 怎么拍平 struct
  - **和本章关联**：把“21 个参数”追溯到具体实现

### 工具文档

- **[LLVM LangRef — Calling Conventions](https://llvm.org/docs/LangRef.html#calling-conventions)**
  - **读哪里**：`ccc` 与参数属性 `signext`/`byval`/`sret`
  - **学什么**：IR 层怎么表达“参数放栈上”“返回值走隐藏指针”
  - **和本章关联**：`matmul.ll` 没有 `sret`，`make_big` 这类代码会用到

### 博客 / 工程文章

- **[Eli Bendersky — Stack frame layout and calling conventions](https://eli.thegreenplace.net/2011/02/04/stack-frame-layout-on-x86-64)**
  - **读哪里**：整篇，重点看参数区与返回地址的相对位置
  - **学什么**：调用者压栈、callee 读正偏移的通用模型（x86-64 例子）
  - **和本章关联**：理解 `ldr x9, [sp, #56]` 为什么是正偏移

ABI 讲完，参数的“形状”清楚了。但还有一个更实际的问题：这些 memref 指向的缓冲区从哪来？边缘设备没有 `malloc` 的自由，下一章进入内存规划。

→ [Ch 22 内存规划：边缘设备没有 malloc 的自由](./22-memory-planning.md)
