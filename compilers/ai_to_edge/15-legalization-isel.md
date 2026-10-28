# Ch 15 — 合法化与指令选择

> **目标**：搞清后端最难也最关键的两步——把「目标机不支持的 IR」改造成「它支持的 IR」（legalization），以及把 IR 操作映射成真实指令（instruction selection）。学完这章，你能解释为什么同一段 C 在不同架构上指令数差好几倍。

> **环境**：LLVM 14.0.0、`llc-14` 及其 `-print-after-isel` 调试开关

## 为什么需要这个？

每个源码里的操作，在真实 CPU 上未必存在对应指令：

```
源码：  a / b        （64 位除法）
x86-64：cqo; idivq   （有，但慢）
AArch64：sdiv        （有，快）
Cortex-M3：bl __aeabi_ldivmod  （没有 64 位除法，只能调库）
RISC-V 基础：没有除法指令（要靠 M 扩展）
```

编译器不能假设「IR 里的每条指令目标机都有」。它必须：

1. **合法化（Legalization）**：把不支持的 IR 操作拆成支持的组合
2. **指令选择（Instruction Selection）**：把合法 IR 映射到具体指令

这两步决定了代码质量的上限。

## 先建立直觉

把合法化想成**把食谱翻译成目标厨房能做的菜**：

```
你的食谱： 「用料理机打 128 位的泥」    ← 128 位乘法
厨房 A（AArch64）：有 128 位料理机 → 直接用
厨房 B（Cortex-M3）：只有 32 位刀     → 拆成 4 次切
厨房 C（RV32 无 M 扩展）：连乘法都没有 → 循环加法代替
```

指令选择则是**从厨房的菜谱库里挑最合适的那道做法**：

```
要「a*b + c」
AArch64：madd a, b, c      （有融合指令）
x86：lea 或 imul+add        （取决于参数）
Cortex-M3：muls + adds      （没有可用的融合）
```

**同一个数学式子，三种做法**。这就是 Ch 2 里实测到的现象。

## 合法化的三种手段

### ① 拆分成更小的操作

IR 里有 `i64` 乘法，目标是 32 位机器：

```
i64 mul  →
  hi = mulhi32(a_lo, b_lo)      // 高 32 位
  lo = mullo32(a_lo, b_lo)      // 低 32 位
  + 交叉项
```

结果变成 3–4 条 32 位指令 + 若干 `add`。

### ② 换成库调用

IR 里有 `i64` 除法，目标是 Cortex-M3（`+hwdiv` 只覆盖 32 位）：

**实测**：

```c
int64_t d(int64_t a, int64_t b)  { return a / b; }
int32_t d32(int32_t a, int32_t b){ return a / b; }
```

Cortex-M3 汇编：

```
	.globl	d
	bl	__aeabi_ldivmod      ← 合法化成库调用
	.globl	d32
	sdiv	r0, r0, r1       ← 32 位有硬件支持
```

AArch64 汇编（同样代码）：

```
	sdiv	x0, x0, x1
	sdiv	w0, w0, w1
```

**两条硬件指令**。同一份 IR，一个变库调用，一个变指令。

### ③ 直接报错

有些操作没法合法化，编译器只能拒绝：

**实测**：

```c
__int128 w(__int128 a, __int128 b) { return a * b; }
```

`clang --target=thumbv7m-none-eabi` 报：

```
wide.c:2:12: error: __int128 is not supported on this target
__int128 w(__int128 a, __int128 b) { return a * b; }
           ^
```

> AArch64 上同一份代码**正常编译**（`__int128` 原生支持）。

> **这是一条重要分界线**：`i64` 除法能降级（拆或调库），`__int128` 在 32 位目标上**直接不支持**。不是「慢」，是「没有这个类型」。

### ④ 软件模拟浮点

Ch 2/Ch 11 已经实测过：Cortex-M3 的 features 里有 `+soft-float`。

```c
float fflt(float a, float b) { return a * b + 1.0f; }
```

反汇编：

```
push	{r3, lr}
bl	__aeabi_fmul        ← 浮点乘法 → 库函数
mov.w	r1, #1065353216  ; 0x3f800000   （1.0f）
bl	__aeabi_fadd        ← 浮点加法 → 库函数
pop	{r3, pc}
```

每次浮点运算两三条指令 + 一次函数调用。而且**裸链接时直接失败**：

```
ld: undefined reference to `__aeabi_fmul'
ld: undefined reference to `__aeabi_fadd'
```

## 指令选择：看真实产物

`llc` 有一个调试开关，能看「ISel 刚做完、还没经过后续优化」的机器指令：

```bash
llc -march=aarch64 -print-after-isel t_o2.ll
```

实测输出（节选，`sum` 函数的入口块）：

```
bb.0 (%ir-block.2):
  successors: %bb.1(0x50000000), %bb.6(0x30000000); %bb.1(62.50%), %bb.6(37.50%)
  liveins: $x0, $w1
  %24:gpr32common = COPY $w1
  %23:gpr64common = COPY $x0
  %26:gpr32all = COPY $wzr
  %25:gpr32all = COPY %26:gpr32all
  %27:gpr32 = SUBSWri %24:gpr32common, 1, 0, implicit-def $nzcv
  Bcc 11, %bb.6, implicit $nzcv
  B %bb.1
```

逐行读：

| 机器指令 | 来源 IR | 说明 |
|---|---|---|
| `COPY $w1` → `%24` | `%1`（参数 n） | 参数搬运 |
| `COPY $x0` → `%23` | `%0`（参数 a） | 指针参数 |
| `SUBSWri %24, 1, 0` | `icmp sgt i32 %1, 0` | **比较被融合进带标志位的减法** |
| `Bcc 11, %bb.6` | `br i1 %3, ...` | 条件分支，用上一条设置的条件码 |

**关键洞察**：IR 里是两条独立操作（`icmp` + `br`），ISel 把它们**融合成两条机器指令**（`SUBSWri` 设条件码 + `Bcc` 用它）。

`SUBSWri` 的含义：**SUB**tract, Set flags, **W**ord, **r**egister/**i**mmediate。它一次做两件事：减法 + 设置 `nzcv` 标志。

### 从 ISel 到最终汇编

ISel 之后还有更多 pass。看最终汇编（同一函数）：

```
sum:
	.cfi_startproc
// %bb.0:
	cmp	w1, #1
	b.lt	.LBB0_3
// %bb.1:
	mov	w9, w1
	cmp	w1, #7
	b.hi	.LBB0_4
// %bb.2:
	mov	x10, xzr
	mov	w8, wzr
	b	.LBB0_7
```

注意变化：

- ISel 阶段是 `SUBSWri`（减法+标志），最终是 **`cmp`**（只比较）
- 因为后续 pass 发现减法结果没被使用，只是要标志 → 换成 `cmp`

> **这是「ISel 之后还有很多优化」的直接证据**。`-print-after-isel` 看到的不等于最终结果。

## 指令选择是一个代价模型问题

最容易的误解是「手册里有这条指令，编译器就该用」。

**实测反例**（Ch 2 已经出现过）：

```c
int add(int a, int b) { return a * b + 1; }
```

Cortex-M3 的 Thumb-2 **有** `mla`（乘加）指令。但实测生成：

```
	muls	r0, r1, r0
	adds	r0, #1
	bx	lr
```

**没用 `mla`**。

原因：`mla` 的第三个操作数必须是寄存器，`+1` 是立即数。要用 `mla` 得先 `mov r2, #1` 再 `mla r0, r1, r0, r2`——三条指令，比 `muls`+`adds` 两条更差。

> **正确认识**：指令选择不是「查表匹配」，而是**在目标机约束下最小化代价**。代价包括指令数、延迟、寄存器压力、代码体积。用 `arm-none-eabi-gcc` 编同一段，结果相同（`mul.w` + `adds`）。

### 对比 AArch64 为什么能用 `madd`

```
	orr	w8, wzr, #0x1
	madd	w0, w1, w0, w8
```

同样三条指令，但 `madd` 把乘法、加法、和常数准备**融合**成一条。差别在于：

- AArch64 的 `madd` 能接受寄存器操作数，且 `orr w8, wzr, #1` 是「用零寄存器 + 立即数」的廉价写法
- Thumb-2 的 `mla` 没有对应的廉价常数准备路径

## SelectionDAG 与 GlobalISel

LLVM 有两套指令选择实现：

| | SelectionDAG | GlobalISel |
|---|---|---|
| 成熟度 | 成熟，所有后端支持 | 部分后端（AArch64 较完善） |
| 工作方式 | IR → DAG → 模式匹配 → 机器 DAG | IR → gMIR → 逐阶段降级 |
| 优势 | 全局优化机会多 | 编译速度快、易于增量支持 |
| 调试 | `-print-after-isel` | `-print-after=legalizer` 等 |

本课用 LLVM 14 的默认路径（SelectionDAG）。想强制用 GlobalISel：

```bash
llc -march=aarch64 -global-isel input.ll -o out.s
```

**实测对比**（LLVM 14，同一个 `sum` 函数）：

```bash
llc -march=aarch64 t_o2.ll -o sel.s            # 默认：SelectionDAG
llc -march=aarch64 -global-isel t_o2.ll -o gi.s  # 强制 GlobalISel
wc -l sel.s gi.s
```

实测：

```
  57 sel.s
  59 gi.s
  116 total
```

**两者都能成功编译，产物规模几乎一样**（58 vs 57 行，差 2 行）。所以 LLVM 14 的 AArch64 GlobalISel 对这段代码是**可用**的，不是只能 fallback。

> 但这不代表两者总是一样好：GlobalISel 在 LLVM 14 仍在完善中，复杂代码上可能出现性能差异。想深入这条路径，仓库里已有 `globalisel_instruction_select` 与 `globalisel_regbankselect` 两门课，专门讲 gMIR、`Legalizer`、`RegBankSelect` 与 TableGen matcher。

## 在 AI 编译流程里的位置

```
Inductor 生成的 C++ / MLIR 降级出的 LLVM IR
                    ↓
              llc / clang -O2
                    ↓
           Legalization（本章上半）
            不支持的操作 → 拆分/调库/报错
                    ↓
           Instruction Selection（本章下半）
            IR 操作 → 目标指令（代价最优）
                    ↓
           寄存器分配（Ch 16）
```

**对 AI 编译器的直接影响**：

- 如果图里有 `i64` 索引运算（memref 的 offset/size/stride 都是 i64），在 32 位 MCU 上会大量触发合法化
- 如果图里有浮点，在无 FPU 的 MCU 上会全部变成库调用
- 如果用了 `i128`（某些量化累加），在 32 位目标上**直接编译失败**

这三条都是 Ch 2「约束级联」在后端的具体表现。

## 踩雷集锦

1. **以为 `i64` 在 32 位机器上「慢一点」**：不是慢，是「拆成多条指令」或「变成库调用」，且库可能根本不存在（裸链接失败）。
2. **以为手册有 `mla` 就会用**：代价模型决定不用。实测 `muls`+`adds` 优于 `mla` + 常数搬运。
3. **以为 `-print-after-isel` 是最终结果**：后续 pass 还会改（`SUBSWri` → `cmp`）。
4. **在 32 位目标上用 `__int128`**：直接编译报错，不是降级。
5. **忽略 `+strict-align`**：Cortex-M3 不允许非对齐访问，编译器不会合并相邻的 `i16` 读写，性能模型要相应调整。

## 本章重点整理

- 合法化三种手段：拆分、库调用、直接报错；浮点还可能整体软件模拟。
- `i64` 除法在 Cortex-M3 上变成 `__aeabi_ldivmod`，`i32` 用硬件 `sdiv`。
- `__int128` 在 32 位目标上**不支持**（编译错误），不是降级。
- 指令选择是代价模型问题，不是查表；`mla` 存在但可能不用。
- `-print-after-isel` 是 ISel 阶段快照，不是最终产物。

## 自我检核

- [ ] 能说出合法化的三种手段，各举一个本章实测的例子
- [ ] 能解释为什么 `i64 / i64` 在 Cortex-M3 上是库调用，而 `i32 / i32` 是 `sdiv`
- [ ] 知道 `__int128` 与 `i64` 除法在 32 位目标上的处理差异（拒绝 vs 降级）
- [ ] 能解释为什么 Thumb-2 有 `mla` 却没被用上
- [ ] 能区分 `-print-after-isel` 的输出与最终汇编的差别

## 延伸阅读

### 官方文档

- **[LLVM — SelectionDAG](https://llvm.org/docs/CodeGenerator.html#selectiondag)**
  - **读哪里**：`SelectionDAG` 与 `Legalization` 两节
  - **为什么值得读**：本章合法化三种手段的权威说明，含 type legalization 与 operation legalization 的区别
- **[LLVM — GlobalISel](https://llvm.org/docs/GlobalISel/index.html)**
  - **读哪里**：`IRTranslator` → `Legalizer` → `RegBankSelect` → `InstructionSelect` 四阶段
  - **和本章的关联**：另一条指令选择路径；仓库有专门课程深入

### 源码依据

- **[llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp)**
  - **读哪里**：`ExpandNode` 与 `LegalizeOp`
  - **为什么值得读**：想知道「为什么这个操作被拆成那样」时的唯一权威

### 论文

- **[LLVM: A Compilation Framework for Lifelong Program Analysis & Transformation](https://llvm.org/pubs/2004-01-LLVM-IR.html)** — Lattner & Adve, CGO 2004
  - **核心贡献**：LLVM 的分层设计，包括「IR 与目标解耦」这一选择
  - **读哪里**：Section 5（Code Generation）
  - **和本章的关联**：解释为什么合法化是必需的一层

- **[Engineering the SelectionDAG](https://llvm.org/devmtg/2012-11/slides/Might-SelectionDAG.pdf)** — Might, LLVM Dev Meeting 2012
  - **核心贡献**：SelectionDAG 的设计与代价模型
  - **读哪里**：整份 slides
  - **和本章的关联**：本章「代价模型而非查表」的原始论述

### 博客 / 工程文章

- **[ARM — Cortex-M3 Technical Reference Manual, Instruction Set](https://developer.arm.com/documentation/100165/)**
  - **这系列说什么**：Thumb-2 指令集完整定义，含 `MLA` / `MULS` 的编码与限制
  - **读哪里**：Chapter 3 的 `Multiply instructions`
  - **为什么值得读**：验证「为什么 `mla` 需要寄存器操作数」这一类判断

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版）
  - **读哪几章**：第 8 章（指令选择）——尤其是树模式匹配与动态规划
  - **和本章的关联**：SelectionDAG 的理论祖先（树覆盖问题）

指令选完了，但虚拟寄存器数量不限，物理寄存器只有 30 来个。下一章讲这个瓶颈怎么解决。

→ [Ch 16 寄存器分配与栈帧](./16-regalloc-stackframe.md)
