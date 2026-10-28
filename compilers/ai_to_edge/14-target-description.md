# Ch 14 — Triple、DataLayout 与 TargetMachine

> **目标**：搞清后端入口的三份「说明书」——target triple、data layout、target features——以及它们出错时会出现什么怪异现象。学完这章，你能诊断「同一份 IR 编到不同架构就崩」这类问题。

> **环境**：LLVM 14.0.0、`clang-14`（交叉编译）、`llc-14`、`llvm-objdump-14`

## 为什么需要这个？

进入后端的第一件事不是生成指令，而是**确定目标机器的规格**：

```
这段代码要跑在什么 CPU 上？  → target triple
指针多宽？对齐要求？           → data layout
有没有 FPU、NEON、原子指令？    → target features
```

这三样任何一样错了，产物都会以**看似无关的方式**失败：段错误、数据错位、链接错误、性能骤降。

## 先建立直觉

把后端想成一家**外包工厂**，你要先填一张订单：

```
┌─────────────────────────────────────┐
│ 订单                               │
├─────────────────────────────────────┤
│ ① triple:   谁来执行？             │
│    aarch64-linux-gnu                │
│    thumbv7m-none-eabi               │
│                                     │
│ ② datalayout: 内存长什么样？        │
│    i64:64  指针 64 位、8 字节对齐    │
│    p:32:32 指针 32 位、4 字节对齐    │
│                                     │
│ ③ features: 有哪些特殊能力？        │
│    +neon  +fp-armv8  -neon  +soft-float │
└─────────────────────────────────────┘
```

三样都填对，工厂才能开工。填错了，工厂不会报错——它会**忠实地按错的规格生产**，问题留到运行时。

## Triple 的解剖

`aarch64-unknown-linux-gnu` 拆开：

```
aarch64  -  unknown  -  linux  -  gnu
   ↑          ↑           ↑        ↑
架构        厂商        操作系统   运行库/ABI
```

实际会用到的组合：

| triple | 架构 | OS | ABI | 典型设备 |
|---|---|---|---|---|
| `aarch64-unknown-linux-gnu` | AArch64 | Linux | glibc | 树莓派、ARM 服务器 |
| `thumbv7m-none-eabi` | Thumb-2 (Cortex-M3) | 无 | bare-metal | STM32、nRF52 |
| `riscv64-unknown-linux-gnu` | RV64 | Linux | glibc | 赛昉、K230 |
| `x86_64-pc-linux-gnu` | x86-64 | Linux | glibc | 你的开发机 |

> **最容易搞错的一处**：`aarch64` 和 `thumbv7m` 都是 ARM，但**完全没有可比性**。前者有 MMU、glibc、NEON；后者连 `printf` 都没有。把 `aarch64-linux-gnu` 的 IR 拿去 `thumbv7m` 上跑，会得到一堆未定义符号。

## DataLayout：内存的物理规格

这是最容易被忽略、也最容易造成诡异 bug 的部分。

实测四种 target 的 datalayout（`clang --emit-llvm` 的真实输出）：

```
x86_64-pc-linux-gnu       : e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128
aarch64-unknown-linux-gnu : e-m:e-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128
riscv64-unknown-linux-gnu : e-m:e-p:64:64-i64:64-i128:128-n64-S128
thumbv7m-none-unknown-eabi: e-m:e-p:32:32-Fi8-i64:64-v128:64:128-a:0:32-n32-S64
```

逐个解释几个关键字段：

| 字段 | 含义 | 本机实测值 |
|---|---|---|
| `e` | 小端（little endian） | 全部是 `e` |
| `m:e` | mangling 方式 | ELF |
| `p:32:32` | 指针 32 位宽、32 位对齐 | 只有 thumbv7m |
| `p:64:64` | 指针 64 位宽、64 位对齐 | aarch64 / riscv64 |
| `i64:64` | i64 按 8 字节对齐 | 全部 |
| `i8:8:32` | i8 自然对齐 8 位，**首选对齐 32 位** | aarch64 |
| `n32:64` | 原生整型宽度（i32..i64） | aarch64 / thumbv7m |
| `n64:128` | 原生整型加宽到 i128 | riscv64 |
| `S128` | 栈自然对齐 128 位 | x86_64 / aarch64 / riscv64 |
| `S64` | 栈自然对齐 64 位 | thumbv7m |

### 那个 `i8:8:32` 是做什么的

AArch64 上 `char` 的自然对齐是 1 字节，但**首选对齐（preferred alignment）**是 4 字节。这不影响 `sizeof`，但影响：

- 全局变量怎么排（可能插 padding）
- 编译器能否把多个 `char` 合并为一次 32 位访问

x86_64 的 datalayout 里**没有** `i8:8:32`（只有 `i8:8:32` 在 aarch64 出现）。这个差异会导致**同一份全局数组在两种架构上的内存布局不同**。

> **边缘部署的实用结论**：如果你把一个二进制权重 buffer 直接 `mmap`/`memcpy` 到结构体上，**x86 导出的布局在 ARM 上可能是错的**。要么用专门的序列化格式（带显式 padding），要么在目标架构上重新导出。

### 指针宽度决定一切

`p:32:32`（thumbv7m）和 `p:64:64`（aarch64）的差别不只是「指针占几个字节」：

```
32 位指针 → 地址空间 4 GB → 所有 size_t 都是 32 位
        → memref 的 offset/size/stride 都是 i32（不是 i64）
        → 同一个 memref 从 7 个 i64 变成 7 个 i32 参数
```

回想 Ch 12 的 21 个参数（3 memref × 7）。在 Cortex-M3 上它们是：

```
ptr(32) ptr(32) i32 i32 i32 i32 i32   × 3 = 21 个参数，但每个更窄
```

这直接影响 Ch 21 讲的调用约定——**参数多到寄存器装不下，只能走栈**。

## Target Features：能力开关

实测四种 target 的 features（`attributes #0` 里的真实值）：

**aarch64**：
```
"target-cpu"="generic" "target-features"="+neon,+outline-atomics,+v8a"
```

**thumbv7m（Cortex-M3）**：
```
"target-cpu"="cortex-m3" "target-features"="+armv7-m,+hwdiv,+soft-float,+strict-align,+thumb-mode,
-aes,-bf16,-d32,-dotprod,-fp-armv8,-fp-armv8d16,-fp-armv8d16sp,-fp-armv8sp,-fp16,-fp16fml,
-fp64,-fpregs,-fullfp16,-mve,-mve.fp,-neon,-sha2,-vfp2,-vfp2sp,-vfp3,-vfp3d16,-vfp3d16sp,
-vfp3sp,-vfp4,-vfp4d16,-vfp4d16sp,-vfp4sp" "use-soft-float"="true"
```

**riscv64**：
```
"target-features"="+64bit,+a,+c,+d,+f,+m" "target-abi" = "lp64d"
```

读出三件事：

1. **Cortex-M3 的 `-neon`、`-fpregs`、`+soft-float`**：明确关掉了所有浮点硬件。这就是 Ch 2/Ch 11 里 `float` 变成 `__aeabi_fmul` 的原因——**不是编译器不想用 FPU，是这个 target 明确声明没有**。
2. **`+strict-align`**：Cortex-M3 不允许非对齐访问。这意味着编译器不能把两个 `i16` 合并成一个 `i32` 读（除非能证明对齐）。这是一个**性能上的硬约束**。
3. **`+hwdiv`**：Cortex-M3 **有**硬件除法（`sdiv`/`udiv`）。这解释了一个容易混淆的点——实测 `d32(int32_t a, int32_t b)` 生成 `sdiv r0, r0, r1`，而 `d(int64_t a, int64_t b)` 生成 `bl __aeabi_ldivmod`。

### 实测：`+hwdiv` 的边界

```c
int64_t d(int64_t a, int64_t b)  { return a / b; }
int32_t d32(int32_t a, int32_t b){ return a / b; }
```

实测 Cortex-M3 汇编：

```
	.globl	d
	bl	__aeabi_ldivmod        ← 64 位除法：库调用
	.globl	d32
	sdiv	r0, r0, r1         ← 32 位除法：硬件指令
```

同一个 `/` 运算符，`i32` 有硬件支持，`i64` 没有。这是**合法化（legalization）** 的典型案例，Ch 15 展开。

对齐同样的代码编到 AArch64：

```
	sdiv	x0, x0, x1
	sdiv	w0, w0, w1
```

两条都是硬件指令——AArch64 原生支持 64 位除法。

## 用 llc 手工控制

`llc` 接受这些参数：

```bash
llc -march=aarch64 -mcpu=generic -mattr=+neon input.ll -o out.s
llc -march=thumb -mcpu=cortex-m3 -mattr=-neon input.ll -o out.s
```

看可用 CPU 列表：

```bash
llc -march=aarch64 -mcpu=help 2>&1 | head -8
```

实测（节选）：

```
Available CPUs for this target:

  a64fx           - Select the a64fx processor.
  apple-a10       - Select the apple-a10 processor.
```

### 实测：`-march` 到底会不会覆盖 IR 里的 triple

网上常见说法是「IR 里写死 triple 后 `-march` 就无效」。本机实测**不是这样**：

```llvm
; conflict.ll
target triple = "aarch64-unknown-linux-gnu"
define i32 @f(i32 %a) { %b = add i32 %a, 1
  ret i32 %b }
```

跑 `llc -march=riscv64 conflict.ll`：

```
	.text
	.attribute	4, 16
	.attribute	5, "rv64i2p0"        ← 确实是 RISC-V
	.file	"conflict.ll"
	.globl	f
```

**`-march` 覆盖生效了**，输出是 RISC-V 汇编，不是 AArch64。

> **正确认识**：`llc` 的命令行参数（`-march` / `-mtriple` / `-mcpu` / `-mattr`）**优先于 IR 里的声明**。IR 里的 `target triple` 是「默认值」，命令行是「显式指定」。
>
> **但仍要小心**：如果你只给 `-march` 不给 `-mtriple`，data layout 可能仍按 IR 里的 triple 推导，出现「指令是 RISC-V 但指针宽度按 aarch64 算」这种矛盾。稳妥做法是**两个一起给**：
>
> ```bash
> llc -mtriple=riscv64-unknown-linux-gnu -mcpu=generic input.ll -o out.s
> ```

### 三种 triple 的产物对照（实测）

| triple | 汇编头特征 |
|---|---|
| `aarch64-linux-gnu` | `.text` + `.globl f` + `.p2align 2` |
| `thumbv7m-none-eabi` | `.syntax unified` + `.eabi_attribute 67, "2.09"` |
| `riscv64-linux-gnu` | `.attribute 4, 16` + `.attribute 5, "rv64i2p0"` |

三种格式完全不同——这就是 triple 的直接体现。

## 在 AI 编译流程里的位置

```
MLIR 降级 → LLVM IR（带 triple + datalayout）
                    ↓
              llc -march=<目标>
                    ↓
         TargetMachine（triple + datalayout + features 的合体）
                    ↓
           Legalization（Ch 15）
                    ↓
         指令选择 → 寄存器分配（Ch 16）
```

**TargetMachine 是后端的「配置对象」**，它把三份说明书合到一起，后续所有 pass 都向它查询决策依据：

- `getPointerSize()` → 决定地址计算用几个字节
- `isLittleEndian()` → 决定多字节数据的字节序
- `hasFPU()` → 决定浮点是硬件还是库调用
- `getMinStackAlignment()` → 决定栈帧怎么排

## 踩雷集锦

1. **把 `aarch64` 当 `thumbv7m` 用**：两者都叫 ARM，但一个有 MMU/glibc，一个没有。链接期会大量未定义符号。
2. **忽略 `p:32:32`**：Cortex-M3 上指针、`size_t`、memref 的 size/stride 全是 32 位。按 64 位写驱动会读错参数。
3. **以为「ARM 有 NEON」是通用事实**：Cortex-M3 明确 `-neon`。向量化在它上面只能靠 `smlad` 这类 DSP 指令，且需要整数。
4. **用 x86 导出的二进制布局到 ARM 上**：`i8:8:32` 与对齐要求不同，结构体 padding 可能不一致。
5. **只给 `-march` 不给 `-mtriple`**：指令集变了但 data layout 可能仍按旧 triple 推导。两个一起给才稳妥。

## 本章重点整理

- 后端入口需要三份说明书：triple（谁执行）、datalayout（内存长什么样）、features（有什么能力）。
- `p:32:32` 与 `p:64:64` 决定了整个程序的指针/尺寸类型宽度，影响调用约定。
- Cortex-M3 的 `+soft-float`、`-neon`、`+strict-align` 是硬约束，直接决定了浮点和向量化策略。
- `+hwdiv` 只覆盖 32 位；64 位除法走 `__aeabi_ldivmod` 库调用。
- 三份说明书错了不会报错，问题会推迟到运行时。

## 自我检核

- [ ] 能拆解 `thumbv7m-none-eabi` 的四个字段，并说出与 `aarch64-linux-gnu` 的本质差异
- [ ] 看到 `p:32:32` 能说出它对 memref 参数宽度的具体影响
- [ ] 能解释为什么 Cortex-M3 上 `float` 处理会变成库调用（结合 features）
- [ ] 知道 `+hwdiv` 覆盖不到 i64 除法，以及由此产生的 `__aeabi_ldivmod`
- [ ] 知道 `-march` 实际会覆盖 IR 的 triple，以及为什么仍建议同时给 `-mtriple`

## 延伸阅读

### 官方文档

- **[LLVM DataLayout](https://llvm.org/docs/LangRef.html#data-layout)**
  - **读哪里**：整节；重点是 `p`（指针）、`i`（整型）、`n`（原生宽度）、`S`（栈对齐）
  - **为什么值得读**：本章那张表的权威定义，每个字段都有精确语义
- **[LLVM — Cross-compilation](https://llvm.org/docs/HowToCrossCompileLLVM.html)**
  - **读哪里**：`Choose Target` 与 CMake 参数部分
  - **和本章的关联**：解释为什么自建 LLVM 常常只有 X86 target（对应 Ch 0 的踩坑）

### 源码依据

- **[llvm/lib/Target/AArch64/AArch64.td](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AArch64/AArch64.td)**
  - **读哪里**：`FeatureFPARMv8`、`FeatureNEON` 等 feature 定义
  - **为什么值得读**：`-neon` / `-fpregs` 这些字符串的确切来源与含义

### 论文

- **[The RISC-V Instruction Set Manual, Volume I: Unprivileged ISA](https://riscv.org/technical/specifications/)**
  - **核心贡献**：定义 `+a`（原子）、`+c`（压缩）、`+d`（双精度）、`+f`（单精度）、`+m`（乘除）
  - **读哪里**：Chapter 1（Extensions 的命名与组合规则）
  - **和本章的关联**：riscv64 的 `"target-features"="+64bit,+a,+c,+d,+f,+m"` 就是这套规则的产物

### 博客 / 工程文章

- **[Eli Bendersky — LLVM Target and DataLayout](https://eli.thegreenplace.net/2013/02/25/a-crash-course-in-llvm-ir/)**
  - **这系列说什么**：IR 里各处出现的 triple/datalayout 及其作用
  - **读哪里**：`Target triple and data layout` 一节
  - **为什么值得读**：把抽象字段和实际现象连起来讲

### 书籍

- **《See MIPS Run》** — Dominic Sweetman（第 2 版）
  - **读哪几章**：第 2 章（内存布局与对齐）、第 4 章（ABI）
  - **和本章的关联**：嵌入式架构里「对齐就是硬约束」的经典论述，虽然讲的是 MIPS，原理通用

知道了目标机器的规格，下一章看后端如何把「这台机器不支持的 IR」改造成「它支持的 IR」。

→ [Ch 15 合法化与指令选择](./15-legalization-isel.md)
