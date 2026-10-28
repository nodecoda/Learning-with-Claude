# Ch 2 — 边缘设备的真实约束

> **目标**：搞清边缘设备与服务器在哪些**硬性维度**上不同，以及这些差异如何一级一级地限制编译器能做的选择。学完这章，你能拿到一块板子就说出「哪些优化在这里不可用」。

## 为什么需要这个？

「把模型部署到边缘」听起来像一句配置改动，实际是一次**约束条件全面收紧**。

服务器上有：

- 无限量的 DRAM（几百 GB）
- 操作系统（`malloc` 随便用，虚拟内存、缺页中断都有人兜底）
- 硬件浮点除法（`fdiv` 一条指令）
- 多级 cache（几十 MB）
- 动态链接器（缺库就装上）

边缘设备上这些**全部可能没有**。而编译器的每个优化决定，都隐含假设了其中至少一项成立。

## 先建立直觉

把「编译」想成「给客人备餐」。

```
服务器 = 中央厨房
  冰箱很大（内存随便用）
  什么调料都齐（算子库、libc、fp 单元）
  有洗碗工（OS 回收内存）
  → 你可以随便多做几道菜再倒掉

边缘设备 = 露营野炊
  一个背包（几十 KB 到几 MB）
  只有带了的调料（静态链接、手写 kernel）
  没人洗碗（没有 OS 回收）
  → 每一克重量都要算，做多了就得自己吃掉
```

**编译器要做的事，就是根据「背包里有什么」重新安排整张菜单。**这就是为什么同一个模型，服务器版和边缘版要编两次。

## 约束一：内存

这是最硬的约束，也是最多失败发生的地方。

| 设备类别 | 典型 RAM | 典型 Flash/ROM | 能装下的模型 |
|---|---|---|---|
| Cortex-M0/M3 MCU | 8–64 KB | 64–512 KB | 极小的关键词识别、简单传感器融合 |
| Cortex-M7 / 高端 MCU | 256 KB–1 MB | 1–8 MB | MobileNet 级别的分类（极限量化） |
| Cortex-A + Linux（树莓派级） | 512 MB–8 GB | SD/eMMC | ResNet、小型 LLM 推理 |
| 手机 SoC | 4–16 GB | 128 GB+ | 7B 级量化的 LLM |

本机实测的 Cortex-M 固件（Ch 19 的 `compute` 例子）：

```
   text	   data	    bss	    dec	    hex	filename
    289	      0	      0	    289	    121	firmware.elf
```

289 字节——因为它只算 `6*7`。换成真实模型，`bss`（未初始化全局区）会立刻爆炸。原因是：

> **AI 编译器的中间结果（activation）默认都放在堆或全局区。**

中间 tensor 有生命周期，理论上可以复用。但「复用」需要编译器做**内存规划**（liveness analysis + buffer 复用），这是 Ch 22 的主题。不做规划的编译器，每层 activation 都单独分配，内存占用是理论下界的数倍。

## 约束二：没有操作系统

在 Cortex-M 上，`malloc` 不存在。不是「慢」，是**链接失败**：

```
undefined reference to `malloc'
```

这直接影响 AI 编译器，因为很多代码生成器默认输出这样的结构：

```c
float* tmp = (float*)malloc(4 * 1024 * 4);
```

Ch 12 会实测到：MLIR 降到 LLVM IR 时，**即使你的 IR 里没有 `malloc`，`mlir-translate` 也会插进 `declare i8* @malloc(i64)`**：

```llvm
declare i8* @malloc(i64)
declare void @free(i8*)
```

在 Linux 上有人帮你实现；在裸机上必须自己提供，或者用静态分配绕开。

三种标准解法：

1. **静态分配**：编译器算出最大缓冲，用一个全局 `static` 数组（arena）
2. **栈分配**：小 buffer 放栈上（风险：栈溢出，Cortex-M 默认栈只有几 KB）
3. **自定义分配器**：实现一个 bump allocator，配合 `-Wl,--wrap=malloc`

## 约束三：浮点能力

这一条最容易被忽略。

| 架构 | 硬件浮点 | 对编译器的影响 |
|---|---|---|
| x86-64（服务器） | SSE/AVX，全精度 | 向量化宽度 8/16，`fdiv` 一条指令 |
| Cortex-A（arm64） | NEON/FP，全精度 | 向量化宽度 4，`fdiv` 一条指令 |
| Cortex-M4F | 单精度 FPU | 只有 `f32` 硬件化，`f64` 走软件 |
| Cortex-M3（无 F 后缀） | **无 FPU** | **所有浮点走软件模拟** |

实测：同一段语义编到 Cortex-M3，整数和浮点的结果完全不同。

**整数版本**（`a * b + 1`）：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -c fint.c -o fint.o
arm-none-eabi-objdump -d fint.o
```

```
00000000 <fint>:
   0:	fb01 f000 	mul.w	r0, r1, r0
   4:	3001       	adds	r0, #1
   6:	4770       	bx	lr
```

自给自足，8 字节，不需要任何外部例程。

**浮点版本**（`a * b + 1.0f`）：

```
00000000 <fflt>:
   0:	b508       	push	{r3, lr}
   2:	f7ff fffe 	bl	0 <__aeabi_fmul>     ← 软件浮点乘法
   6:	f04f 517e 	mov.w	r1, #1065353216      ← 1.0f 的位模式
   a:	f7ff fffe 	bl	0 <__aeabi_fadd>     ← 软件浮点加法
   e:	bd08       	pop	{r3, pc}
```

16 字节——但这只是**引用**，不是实现。裸链接（`-nostdlib`）直接失败：

```
ld: fflt.c:(.text+0x2): undefined reference to `__aeabi_fmul'
ld: fflt.c:(.text+0xa): undefined reference to `__aeabi_fadd'
```

同样的驱动，整数版链接后只有 28 字节：

```
   text	   data	    bss	    dec	    hex	filename
     28	      4	      0	     32	     20	l_int.elf
```

> **这是本课最重要的一条实测结论**：Cortex-M3 上没有 FPU，`float` 运算不是「慢一点」，而是**链接期就缺符号**。你必须提供软件浮点实现（`-lgcc` 或自己写），而它们会把代码体积和延迟推高一到两个数量级。
>
> 对 AI 编译器最直接的后果：模型里的 `float32` 权重在 Cortex-M3 上要么转成定点（int8），要么每个乘加都调用一次软件浮点函数。这使量化从「可选优化」变成**前提条件**。

这就是为什么 MCU 上的推理几乎总是**量化**的（Ch 11）。不是「为了更准的精度」，是**没有别的基础设施**。

## 约束四：内存层次

服务器和 MCU 的存储层次差了一个数量级还多：

```
服务器                           Cortex-M
┌──────────┐                    ┌──────────┐
│ L1  32KB │                    │ Flash    │ 512KB   ← 代码和常量在这
├──────────┤                    │   ↕      │
│ L2   2MB │                    ├──────────┤
├──────────┤                    │ SRAM     │  64KB   ← 所有数据在这
│ L3  32MB │                    └──────────┘
├──────────┤                          ↑
│ DRAM 256G│                    没有 cache
└──────────┘                    没有 MMU
```

这带来一个反直觉的结论：

> **在 MCU 上，「数据搬家」比「多算几次」贵得多。**

服务器上你可以用寄存器缓存随便折腾；MCU 上每次 `load` 都可能直接打到 Flash（几十 ns）或 SRAM。所以 MCU 编译器的调度策略常常是「宁可重算，不要多占 buffer」——和服务器完全相反。

## 约束五：没有动态链接

服务器：

```bash
$ ldd ./app
    libtorch.so => /usr/lib/libtorch.so
    libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6
```

边缘固件必须**全静态**：

```bash
$ file firmware.elf
firmware.elf: ELF 32-bit LSB executable, ARM, EABI5 version 1 (SYSV), statically linked
```

后果：所有算子库、runtime、数学函数都被塞进同一个二进制。这直接改变了「算子库还是代码生成」的取舍（Ch 23）。

## 约束是如何逐级传导的

这是本章最重要的一节。约束不是独立起作用的，它们会**级联**：

```
没有 OS
  └─► 不能 malloc
        └─► activation 必须静态规划
              └─► 编译器必须知道每层的 tensor 大小
                    └─► shape 必须完全静态
                          └─► 动态 shape 不支持
                                └─► 不能用「运行时才决定」的优化
                                      └─► 调度必须编译期全定

没有 FPU
  └─► float32 太慢
        └─► 必须量化到 int8
              └─► 需要量化算子 + requantize 逻辑
                    └─► 图优化里多出一类「量化-反量化」折叠
```

看第二条链的实际证据（Ch 19 实测的 Cortex-M 反汇编）——整数循环 `compute`：

```asm
00000008 <reset_handler>:
   8: 2168        movs  r1, #104
   a: f04f 2240   mov.w r2, #1073758208
   e: 4805        ldr   r0, [pc, #20]
  10: 6993        ldr   r3, [r2, #24]
  12: 069b        lsls  r3, r3, #26
  14: d4fc        bmi.n 10 <reset_handler+0x8>
  16: 6011        str   r1, [r2, #0]
```

全是整数指令，没有一条浮点。如果这里有个 `float` 乘法，你会看到 `bl __aeabi_fmul`（软件浮点函数调用）——彻底改变性能特征。

## 一个完整的对照实验

同一段源码，三个目标，看编译器各自做了什么选择。

源码（`code/01_hello_llvm/add.c`）：

```c
int add(int a, int b) { return a * b + 1; }
```

### AArch64（有硬件乘加）

```bash
/usr/lib/llvm-14/bin/llc -march=aarch64 add.ll -o add_a64.s
```

实测：

```
	orr	w8, wzr, #0x1
	madd	w0, w1, w0, w8
	ret
```

3 条指令：准备常量 1 → 乘加融合 → 返回。

### RISC-V 64（有乘法扩展）

```bash
/usr/lib/llvm-14/bin/llc -march=riscv64 add.ll -o add_rv.s
```

实测：

```
	mulw	a0, a1, a0
	addiw	a0, a0, 1
	ret
```

3 条指令，但**没有**融合成一条 FMA——RV64I 基础指令集没有乘加融合；有 `F`/`D` 扩展的浮点才有 `fmadd`。整数乘加要两条。

### Cortex-M3（Thumb-2，实测是两条）

```bash
/usr/lib/llvm-14/bin/clang --target=thumbv7m-none-eabi -mcpu=cortex-m3 \
  -O2 -ffreestanding -c add.c -o add_cm3.o
/usr/lib/llvm-14/bin/llvm-objdump -d --triple=thumbv7m-none-eabi add_cm3.o
```

实测：

```
00000000 <add>:
       0: 48 43        	muls	r0, r1, r0
       2: 01 30        	adds	r0, #1
       4: 70 47        	bx	lr
```

> **实测纠正**：Thumb-2 确实有 `mla`（乘加）指令，但 LLVM 在这里**没有**生成它——`+1` 是立即数，`mla` 的第三操作数需要是寄存器，而多搬一次常量并不划算。换 `arm-none-eabi-gcc` 结果相同（`mul.w` + `adds`）。
>
> 这类「手册里有这条指令 ≠ 编译器一定用」的差别，是 Ch 15（指令选择）要讲的核心：**指令选择是一个代价模型问题，不是查表问题**。

> **教学点**：同一个语义，三个架构给出三种指令序列。这不是编译器"水平不同"，是**目标 ISA 的能力边界不同**。AI 编译器在后端的任务，就是把这个边界用到位。

## 踩雷集锦

1. **以为「支持 ARM」就够**：`arm64`（Cortex-A，有 MMU/FPU/libc）和 `thumbv7m`（Cortex-M，裸机）是完全不同的世界。target triple、链接脚本、启动代码、可用库全不一样。
2. **在 MCU 上依赖 `malloc`**：链接期就失败，而不是运行期。错误信息是 `undefined reference to 'malloc'`，不是段错误。
3. **以为量化只是为了精度**：在 Cortex-M3 上没有 FPU，`float` 会变成函数调用。量化首先是**性能必需**，其次才是精度取舍。
4. **忽略 Flash 也是瓶颈**：把权重打进 `.rodata`，运行时从 Flash 读。Flash 读取延迟比 SRAM 高，且没有 cache（多数 MCU），所以**权重布局会直接影响速度**。
5. **认为服务器优化策略可以平移**：服务器上「用内存换时间」，MCU 上必须反过来。直接把服务器的 tiling 参数搬过去，几乎一定 OOM。

## 本章重点整理

- 边缘设备的约束会**级联**：没有 OS → 不能 malloc → shape 必须静态 → 调度必须编译期定。
- 无 FPU 让 `float32` 变成软件模拟，这使量化从「可选优化」变成「前提条件」。
- 同一段语义在 AArch64 / RV64 / Cortex-M3 上生成三种不同指令序列，这是 ISA 能力差异，不是编译器水平差异。
- 服务器「用内存换时间」，MCU 必须「用时间换内存」。

## 自我检核

- [ ] 不看表格，说出五类边缘约束，以及它们如何互相传导
- [ ] 能解释「没有 OS」为什么会推到「shape 必须静态」
- [ ] 知道 Cortex-M3 上 `float` 乘法发生了什么，以及为什么这决定了量化策略
- [ ] 看到 `madd` / `muls+adds` / `mulw+addiw` 三种序列，能说出各自的 ISA 原因与选择理由
- [ ] 能说出至少一个「照搬服务器优化到 MCU 会失败」的具体场景

## 延伸阅读

### 官方文档

- **[ARM Cortex-M3 Technical Reference Manual](https://developer.arm.com/documentation/100165/)**
  - **读哪里**：Chapter 2（Programmer's Model）与 Chapter 3（Memory Map）
  - **为什么值得读**：本章说「没有 MMU、内存映射固定」，这两章给出权威细节
  - **前提**：不需要先读 ARM 汇编，看寄存器表即可

- **[ARMv7-M Architecture Reference Manual](https://developer.arm.com/documentation/ddi0403/)**
  - **读哪里**：Appendix A（Instruction Set Encoding）里的 `MLA` 说明
  - **和本章的关联**：本章最后那个 `mla` 例子的依据

- **[RISC-V Unprivileged ISA Specification](https://riscv.org/technical/specifications/)**
  - **读哪里**：Chapter 7（M Extension，乘法）与 Chapter 11（F/D，浮点 `fmadd`）
  - **和本章的关联**：解释为什么 RV64 整数乘加要两条指令

### 论文

- **[TinyML: Machine Learning with TensorFlow Lite on Arduino and Ultra-Low-Power Microcontrollers](https://arxiv.org/abs/2011.00743)** — Warden & Situnayake, 2020
  - **核心贡献**：系统描述 MCU 上推理的实测约束（内存、功耗、精度）
  - **读哪里**：Chapter 3（"The Constraints of TinyML"）与 Chapter 5
  - **和本章的关联**：本章「约束级联」的工程实证，Ch 22 与 Ch 29 的前提

- **[MCUNet: Tiny Deep Learning on IoT Devices](https://arxiv.org/abs/2007.10319)** — Lin et al., NeurIPS 2020
  - **核心贡献**：证明在 320KB SRAM 上可以跑 ImageNet 级模型，关键就是**编译器与内存规划的协同设计**
  - **读哪里**：Section 3（Tiny Engine）——它把内存规划做进编译器
  - **和本章的关联**：本章「内存是最硬约束」的最佳反驳/印证：约束不变，但可以靠编译期规划突破

### 博客 / 工程文章

- **[Interrupt — Arm Mbed / CMSIS 文档](https://arm-software.github.io/CMSIS_5/Core/html/index.html)**
  - **这系列说什么**：Cortex-M 启动流程、向量表、栈布局的权威说明
  - **读哪里**：`Startup` 与 `SystemInit` 部分
  - **为什么值得读**：Ch 19 要自己写向量表，这里的定义是事实来源

### 书籍

- **《The Definitive Guide to Arm Cortex-M3 and Cortex-M4 Processors》** — Joseph Yiu（第 3 版，2013）
  - **这书的定位**：Cortex-M 体系结构最完整的一本
  - **读哪几章**：第 2 章（架构）、第 4 章（指令集）、第 7 章（异常与中断）
  - **注意**：Cortex-M7 的 cache 与 TCM 在第 3 版覆盖有限，需要时补厂商文档

知道了边缘设备有多苛刻，下一章我们回到模型侧：编译器第一眼看到的，到底是什么东西。

→ [Ch 3 三种「模型」：nn.Module / FX Graph / ExportedProgram](./03-three-representations.md)
