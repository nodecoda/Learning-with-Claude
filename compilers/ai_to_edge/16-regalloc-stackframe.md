# Ch 16 — 寄存器分配与栈帧

> **目标**：搞清虚拟寄存器怎么落到物理寄存器、装不下时怎么办、以及栈帧是怎么排出来的。学完这章，你能从一段汇编反推出「这个函数用了几个参数寄存器、有没有 spill、栈帧多大」。

> **环境**：LLVM 14.0.0、`clang-14 --target=aarch64-linux-gnu`、`llvm-objdump-14`

## 为什么需要这个？

指令选择完成后，每条指令都用**虚拟寄存器**（`%27`、`%24`）。而 CPU 只有物理寄存器：

- AArch64：31 个 64 位通用寄存器
- Cortex-M3：16 个 32 位寄存器（其中低 8 个可用性受限）

寄存器分配的答案很直接：**不够就放栈上**。但代价巨大：

```
寄存器访问：0 周期延迟
栈访问（L1 命中）：4–5 周期
栈访问（L2）：10–20 周期
```

一个 spill 可能让一段代码慢一倍。这是后端优化最激烈的地方。

## 先建立直觉

把寄存器分配想成**工位安排**：

```
虚拟寄存器 = 100 个员工
物理寄存器 = 31 个工位
栈         = 仓库

分配策略：
  ① 谁同时在场，谁占工位（活跃区间分析）
  ② 过去的人腾工位给下一个人
  ③ 工位满了 → 有人临时去仓库等（spill）
  ④ 需要时再从仓库取回（reload）
```

关键概念：**活跃区间（live interval）**。一个值从「被定义」到「最后一次被使用」的这段时间里，必须占着一个工位。

两个值的活跃区间不重叠 → 可以共用同一个工位。这就把 100 个值压到 31 个工位变得可行。

## 实测：寄存器压力与栈访问

造一个 12 个参数的函数：

```c
int g(int a,int b,int c,int d,int e,int f,int h,int i,int j,int k,int l,int m) {
  int s1=a*b+c, s2=d*e+f, s3=h*i+j, s4=k*l+m;
  int t1=s1*s2, t2=s3*s4;
  int u1=t1+s1, u2=t2+s2, u3=t1*t2;
  return u1+u2+u3+s3+s4;
}
```

编译并统计栈访问：

```bash
clang --target=aarch64-linux-gnu -O2 -S press.c -o press.s
grep -cE 'ldr.*\[sp|str.*\[sp|ldp.*\[sp|stp.*\[sp' press.s
```

实测：**4**

函数体（实测，完整）：

```
g:                                      // @g
	.cfi_startproc
// %bb.0:
	ldr	w8, [sp, #24]        ← 第 11 个参数（m），从栈读
	madd	w12, w1, w0, w2
	ldr	w9, [sp, #8]         ← 第 10 个参数（l），从栈读
	madd	w13, w4, w3, w5
	ldr	w10, [sp]            ← 第 9 个参数（k），从栈读
	ldr	w11, [sp, #16]       ← 加上第 12 个（在 #24 之外的另一个）
	madd	w10, w7, w6, w10
	madd	w8, w11, w9, w8
	add	w11, w13, w12
	mul	w9, w13, w12
	add	w11, w11, w9
	mul	w12, w8, w10
	add	w10, w11, w10
	add	w8, w10, w8
	add	w8, w8, w12
	madd	w0, w12, w9, w8
	ret
.Lfunc_end0:
```

**读出来的三件事**：

### ① 4 个参数在栈上

```
ldr	w8,  [sp, #24]     ← 偏移 24
ldr	w9,  [sp, #8]      ← 偏移 8
ldr	w10, [sp]          ← 偏移 0
ldr	w11, [sp, #16]     ← 偏移 16
```

**四个 `ldr` 读的都是 `sp`**——这些不是 spill，是**参数传递**：12 个参数，AArch64 的整数参数寄存器只有 8 个（`x0`–`x7`），第 9 个及之后必须由调用者放在栈上。

这正是 Ch 21 要讲的 ABI 规则。偏移 0/8/16/24 依次对应第 9/10/11/12 个参数（每个 4 字节，但对齐到 8 字节）。

### ② 没有额外 spill

函数体里所有中间值 `s1..u3` 全部落在寄存器里（`w8`–`w13`）。**没有一条 `str` 写回栈**。

为什么？因为编译器把表达式**重排**了：先算完 `s1`、`s2` 就立刻合并成 `t1`，不需要同时在场的值不多。

### ③ `madd` 大量使用

```
madd w12, w1, w0, w2     ← w1*w0 + w2
madd w13, w4, w3, w5     ← w4*w3 + w5
madd w10, w7, w6, w10    ← w7*w6 + w10
madd w8,  w11, w9, w8    ← w11*w9 + w8
```

四次 `a*b+c` 全部融合成 `madd`。这是 AArch64 的优势（Ch 15 对比过 Cortex-M3 用不上 `mla`）。

> **这一节的价值**：**不是所有栈访问都是 spill**。参数超限的传递、调用者保存的寄存器、局部数组，都会产生栈访问。诊断性能问题时要先分清是哪种——LLVM 的 dump 里，`fixed-stack.N` 是参数区，`stack.N` 才是分配器的 spill 槽。

## 栈帧长什么样

AArch64 上典型的非叶函数栈帧：

```
高地址
┌─────────────────┐
│ 调用者的栈帧      │
├─────────────────┤ ← 进入时 sp 指着这里
│ 返回地址 (lr)    │  ← 叶函数可以不保存
│ 保存的 fp (x29)  │  ← 用到时保存
│ 保存的 callee-saved 寄存器 │
│ 局部变量 / spill 槽 │
├─────────────────┤ ← 新的 sp
│ 传给被调函数的参数 │  ← 第 9+ 个参数
└─────────────────┘
低地址
```

实测一个需要建立栈帧的函数（有函数调用，必须保存返回地址）：

```c
extern int helper(int);
int caller(int x) { return helper(x) + 1; }
```

```bash
clang --target=aarch64-linux-gnu -O1 -S caller.c -o caller.s
sed -n '/^caller:/,/Lfunc_end/p' caller.s
```

实测输出（完整，含 LLVM 自己的注释）：

```
caller:                                 // @caller
	.cfi_startproc
// %bb.0:
	stp	x29, x30, [sp, #-16]!           // 16-byte Folded Spill
	mov	x29, sp
	.cfi_def_cfa w29, 16
	.cfi_offset w30, -8
	.cfi_offset w29, -16
	bl	helper
	add	w0, w0, #1
	ldp	x29, x30, [sp], #16             // 16-byte Folded Reload
	ret
.Lfunc_end0:
```

三件事值得注意：

1. **`stp x29, x30, [sp, #-16]!`**：一次存两个寄存器（帧指针 + 返回地址），`!` 是前索引写回——相当于 `sub sp, sp, #16` 与存两个寄存器的合并。
2. **LLVM 自己把它标成 `16-byte Folded Spill`**：这里的 "spill" 是泛指「值被放到栈上」，不是寄存器压力导致的溢出。术语在不同语境下含义不同。
3. **`mov x29, sp` 建立帧指针**：这是为了方便调试与栈回溯（`.cfi_def_cfa w29, 16` 告诉调试器怎么算调用者的栈位置）。

> **对比**：本机 `-O2` 编的 `sum` 函数**没有** `stp x29, x30` 序列——它是叶函数（不调用别人），返回地址 `x30` 不需要保存。这就是「叶函数可以省掉整套帧建立」的具体表现。

- `stp` = store pair（一次存两个寄存器）
- `[sp, #-16]!` = 前索引寻址 + 写回（`!` 表示更新 sp）
- `ldp` = load pair
- `[sp], #16` = 后索引寻址（先取值，再更新 sp）

**配对存取（`ldp`/`stp`）是 AArch64 的重要优化**：一次内存事务搬两个寄存器，比两次 `ldr` 快。

## 寄存器分配算法

LLVM 用的是**线性扫描（linear scan）** 的变种加上若干特殊处理：

```
① 计算每个虚拟寄存器的活跃区间
② 按起始位置排序
③ 依次分配：找一个当前不活跃的物理寄存器
④ 找不到 → spill 区间最远的那个
```

关键决策是第 ④ 步：**spill 谁**。策略是「spill 活跃区间最远的」——因为它的下次使用最晚，reload 的代价能摊薄。

### 观察分配结果

`llc` 有调试开关，可以看分配**前后**的对比：

```bash
llc -march=aarch64 -print-before=greedy press.ll -o /dev/null
llc -march=aarch64 -print-after=greedy  press.ll -o /dev/null
```

实测 `-print-before=greedy` 输出（节选）：

```
# *** IR Dump Before Greedy Register Allocator (greedy) ***:
# Machine code for function g: NoPHIs, TracksLiveness, TiedOpsRewritten, TracksDebugUserValues
Frame Objects:
  fi#-4: size=4, align=8, fixed, at location [SP+24]
  fi#-3: size=4, align=16, fixed, at location [SP+16]
  fi#-2: size=4, align=8, fixed, at location [SP+8]
  fi#-1: size=4, align=16, fixed, at location [SP]
Function Live Ins: $w0 in %0, $w1 in %1, $w2 in %2, $w3 in %3, $w4 in %4, $w5 in %5, $w6 in %6, $w7 in %7

0B	bb.0 (%ir-block.12):
	  liveins: $w0, $w1, $w2, $w3, $w4, $w5, $w6, $w7
16B	  %7:gpr32 = COPY $w7
32B	  %6:gpr32 = COPY $w6
...
144B	  %8:gpr32 = LDRWui %fixed-stack.0, 0 :: (load (s32) from %fixed-stack.0, align 8)
160B	  %9:gpr32 = LDRWui %fixed-stack.2, 0 :: (load (s32) from %fixed-stack.2, align 8)
192B	  %11:gpr32 = LDRWui %fixed-stack.3, 0 :: (load (s32) from %fixed-stack.3, align 16)
200B	  %10:gpr32 = LDRWui %fixed-stack.1, 0 :: (load (s32) from %fixed-stack.1, align 16)
208B	  %13:gpr32 = MADDWrrr %1:gpr32, %0:gpr32, %2:gpr32
```

这段 dump 把本章前面说的三件事全部量化了：

1. **`Function Live Ins: $w0 ... $w7`**——只有 **8 个**参数寄存器能用
2. **`%0`–`%7` 来自 `$w0`–`$w7`**（`COPY` 指令），剩下 4 个参数从 `%fixed-stack.0`–`.3` 读
3. **`Frame Objects` 里 4 个固定栈槽**的偏移是 `[SP]`、`[SP+8]`、`[SP+16]`、`[SP+24]`，和前面看到的 `ldr [sp, #24]` 完全对应

`%8`、`%9`、`%11`、`%10` 是**虚拟寄存器**（`%` 前缀），`$w0` 是**物理寄存器**（`$` 前缀）。分配器的任务就是把前者换后者。

> **诊断技巧**：这个 dump 里如果出现 `%fixed-stack.*` 之外的栈槽（例如 `%stack.0`），那才是真正的 spill。`fixed-stack` 是 ABI 规定的参数传递区，不是分配器造成的。

## 为什么 AI 编译器关心这个

回想 Ch 6：Inductor 生成的 C++ 里，融合后的 kernel 会把多个操作放进一个循环：

```cpp
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + x0, 8);
auto tmp1 = at::vec::Vectorized<float>::loadu(in_ptr1 + x0, 8);
auto tmp2 = at::vec::Vectorized<float>::loadu(in_ptr2 + x0, 8);
auto tmp3 = tmp0 + tmp1;
auto tmp4 = tmp3 * tmp2;
auto tmp5 = clamp_min(tmp4, 0);
tmp5.store(out_ptr0 + x0);
```

这里有 6 个同时活跃的值，每个是 256 位（AVX2）= 8 个 float：

```
6 个向量值 × 8 float = 48 个 float 的寄存器需求
AVX2 只有 16 个 YMM 寄存器
```

**融合越深，寄存器压力越大**。这是 Ch 7 那条「融合不是越多越好」的量化依据：

- 融合 2 个算子 → 3–4 个活跃值，安全
- 融合 6 个算子 → 可能超过 16 个 YMM → spill 到栈

在 AVX2 上 spill 一个 256 位向量 = 32 字节的写 + 32 字节的读。融合省下的内存往返，可能被 spill 又抵消掉。

> **边缘设备的特殊之处**：Cortex-M3 只有 16 个 32 位寄存器，其中 `r0`–`r7` 是低寄存器（可用于所有 Thumb 指令），`r8`–`r12` 受限。实际可用于计算的只有**个位数**。所以 MCU 上的融合深度必须更保守。

## 踩雷集锦

1. **把所有 `[sp]` 访问当 spill**：实测那个 12 参数函数有 4 个 `ldr [sp]`，全是**参数传递**，不是 spill。
2. **以为寄存器越多越好**：AArch64 有 31 个，但参数寄存器只有 8 个。第 9 个参数就走栈。
3. **忽略 `ldp`/`stp`**：AArch64 的一次搬两个寄存器是性能关键，自己写汇编时不用会慢一倍。
4. **融合时不看寄存器压力**：6 个 256 位向量值就超过 AVX2 的 16 个 YMM。融合的收益可能被 spill 吃掉。
5. **以为 spill 只影响速度**：还会增加栈帧大小。MCU 上栈只有几 KB，spill 多了直接栈溢出（没有 OS 保护，行为是静默破坏内存）。

## 本章重点整理

- 寄存器分配的核心是**活跃区间**：区间不重叠就能共用寄存器。
- AArch64 的整数参数寄存器只有 8 个（`x0`–`x7`），第 9 个参数走栈。
- 栈访问不一定都是 spill：参数传递、callee-saved 保存、局部数组都会用到栈。
- `stp`/`ldp` 配对存取是 AArch64 的关键优化。
- 融合深度受寄存器压力限制：AVX2 只有 16 个 YMM，Cortex-M3 可用的通用寄存器更少。

## 自我检核

- [ ] 能解释「活跃区间」以及它为什么能让多个虚拟寄存器共用一个物理寄存器
- [ ] 看到 `ldr w8, [sp, #24]` 能判断这是 spill 还是参数传递（以及怎么验证）
- [ ] 知道 AArch64 有几个整数参数寄存器，第 9 个参数去哪
- [ ] 能说出「融合 6 个算子」在 AVX2 上为什么会 spill
- [ ] 知道 Cortex-M3 上 spill 多的后果与服务器有什么不同

## 延伸阅读

### 官方文档

- **[LLVM — Register Allocation](https://llvm.org/docs/CodeGenerator.html#register-allocation)**
  - **读哪里**：`Register Allocation` 整节，含 basic/greedy 分配器说明
  - **为什么值得读**：本章「活跃区间 + spill 最远」策略的权威描述
- **[AArch64 ABI — Procedure Call Standard](https://github.com/ARM-software/abi-aa/blob/main/aapcs64/aapcs64.rst)**
  - **读哪里**：`Parameter passing` 一节，尤其是「前 8 个整数参数用 x0–x7」的规则
  - **和本章的关联**：解释实测里 4 个 `ldr [sp]` 的来历

### 源码依据

- **[llvm/lib/CodeGen/RegAllocGreedy.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/RegAllocGreedy.cpp)**
  - **读哪里**：`selectOrSplit` 与 spill 权重计算
  - **为什么值得读**：想知道「为什么 spill 了这个而不是那个」时的唯一答案

### 论文

- **[Linear Scan Register Allocation](https://dl.acm.org/doi/10.1145/330249.330250)** — Poletto & Sarkar, TOPLAS 1999
  - **核心贡献**：线性扫描算法的原始论文，用活跃区间做快速分配
  - **读哪里**：Section 2（Algorithm）与 Section 3（Spilling）
  - **和本章的关联**：LLVM greedy 分配器的算法祖先

- **[Register Allocation via Graph Coloring](https://dl.acm.org/doi/10.1145/1168857.1168906)** — Chaitin et al., 1981
  - **核心贡献**：把寄存器分配建模为图着色问题（经典方法）
  - **读哪里**：Section 2
  - **和本章的关联**：理解「为什么寄存器分配是 NP-hard」以及近似算法的取舍

### 博客 / 工程文章

- **[Eli Bendersky — Stack frame layout on x86-64](https://eli.thegreenplace.net/2011/09/06/stack-frame-layout-on-x86-64)**
  - **这系列说什么**：栈帧布局的逐步推导（x86-64 版本）
  - **读哪里**：整篇
  - **为什么值得读**：栈帧原理跨架构通用，读完再看 AArch64 的 `stp x29, x30` 会很快
  - **前提**：知道 `push`/`pop` 语义

### 书籍

- **《Computer Architecture: A Quantitative Approach》** — Hennessy & Patterson（第 6 版）
  - **读哪几章**：第 3 章（指令级并行）、附录 B（寄存器分配）
  - **和本章的关联**：理解「为什么寄存器访问比内存快几个数量级」的硬件基础

寄存器分配完成，代码就变成了真正的机器指令。下一章看它怎么变成可链接的目标文件。

→ [Ch 17 汇编、目标文件与重定位](./17-assembly-object-relocation.md)
