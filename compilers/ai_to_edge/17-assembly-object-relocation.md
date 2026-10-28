# Ch 17 — 汇编、目标文件与重定位

> **目标**：搞清 `.s` → `.o` → 可执行文件这条最后的链路，尤其是重定位——它解释了为什么「一个地址」在目标文件里是「一个待填的空」。学完这章，你能读懂 `llvm-readobj` 的重定位表，并判断一个链接错误属于哪一类。

> **环境**：LLVM 14.0.0、`llvm-readobj-14`、`llvm-objdump-14`、`aarch64-linux-gnu-gcc`、`ld.lld`

## 为什么需要这个？

到这里代码已经是机器指令了。但**还不能运行**，因为：

```
call helper     ← helper 在哪？地址是多少？
ldr  w0, [x1]   ← x1 指向 gvar，gvar 的地址是多少？
```

**目标文件里的地址是「待填的空」**，因为编译器不知道其他模块的东西会放在哪。填这些空就是**重定位（relocation）**。

## 先建立直觉

把目标文件想成一份**带填空题的试卷**：

```
考试时（编译期）：
  「第 3 题答案见 ______ 页」     ← 空着，因为不知道另一本书在哪

交卷后（链接期）：
  把所有书装订成一册，算出实际页码
  「第 3 题答案见 42 页」          ← 填空完成
```

重定位表就是**所有的空白清单**，每一条记录：

- 空在哪个位置（offset）
- 要填什么类型的东西（`R_AARCH64_CALL26` = 26 位跳转偏移）
- 指向哪个符号（`ext_fn`）

## 汇编：文本形态的机器码

```bash
llc -march=aarch64 sum.ll -o sum.s
```

`.s` 是**汇编文本**，还不是二进制。它的作用是：

- 人可读、可 diff、可 review
- 可以用 `as` 汇编成 `.o`，也可以人工修改后重汇编

编译到 `.s` 再汇编，和直接生成 `.o`，结果一样：

```bash
llc -march=aarch64 -filetype=obj sum.ll -o sum_direct.o
llc -march=aarch64 sum.ll -o sum.s && clang --target=aarch64-linux-gnu -c sum.s -o sum_via_s.o
```

两条路径产物等价（除了 `.file` 注释等元数据差异）。

## 实测：一个完整的重定位表

造一个包含三种引用的源文件：

```c
extern int ext_var;            // 外部变量（走 GOT）
extern int ext_fn(int);        // 外部函数（跳转）
int gvar = 42;                 // 本模块全局变量
static int local = 7;          // 静态（内部链接）

int use(void) { return ext_var + ext_fn(local) + gvar; }
int* addr(void) { return &gvar; }
```

编译成目标文件并看重定位：

```bash
clang --target=aarch64-linux-gnu -O1 -c reloc.c -o reloc.o
llvm-readobj -r reloc.o
```

实测输出（完整）：

```
Relocations [
  Section (3) .rela.text {
    0xC  R_AARCH64_ADR_GOT_PAGE        ext_var 0x0
    0x14 R_AARCH64_LD64_GOT_LO12_NC    ext_var 0x0
    0x1C R_AARCH64_CALL26              ext_fn  0x0
    0x20 R_AARCH64_ADR_PREL_PG_HI21    gvar    0x0
    0x2C R_AARCH64_LDST32_ABS_LO12_NC  gvar    0x0
    0x3C R_AARCH64_ADR_PREL_PG_HI21    gvar    0x0
    0x40 R_AARCH64_ADD_ABS_LO12_NC     gvar    0x0
  }
  Section (8) .rela.eh_frame {
    0x1C R_AARCH64_PREL32  .text 0x0
    0x38 R_AARCH64_PREL32  .text 0x3C
  }
]
```

七条重定位，分三类。

### 类别一：外部变量的 GOT 访问

```
0xC  R_AARCH64_ADR_GOT_PAGE     ext_var
0x14 R_AARCH64_LD64_GOT_LO12_NC ext_var
```

**为什么外部变量要走 GOT（全局偏移表）？**因为 `ext_var` 在别的共享库里，它的地址**编译期不可知**，运行时才知道。所以代码不直接寻址，而是：

```
① ADRP x0, :got:ext_var     ← 取 GOT 表项所在的页
② LDR  x0, [x0, :got_lo12:ext_var]  ← 从表项里取出真实地址
③ LDR  w0, [x0]             ← 再解引用
```

**多一次内存访问**。这就是共享库里的全局变量比静态链接慢的原因。

### 类别二：函数调用

```
0x1C R_AARCH64_CALL26  ext_fn
```

`CALL26` = 26 位相对偏移（`bl` 指令的立即数字段是 26 位，左移 2 位后覆盖 ±128MB）。

**为什么是相对偏移而不是绝对地址？**因为位置无关代码（PIC）：模块可以被加载到任意地址，绝对地址就失效了。相对偏移不受加载地址影响。

> **限制**：±128MB。这就是为什么大型程序需要 `-fPIC` + PLT 桩——直接 `bl` 只能在 128MB 范围内跳。

### 类别三：本模块全局变量的 PC 相对访问

```
0x20 R_AARCH64_ADR_PREL_PG_HI21   gvar
0x2C R_AARCH64_LDST32_ABS_LO12_NC gvar
0x3C R_AARCH64_ADR_PREL_PG_HI21   gvar
0x40 R_AARCH64_ADD_ABS_LO12_NC    gvar
```

**一对一对出现**，这是 AArch64 的标志性模式：

```
ADRP x0, gvar        ← 取 gvar 所在页的基址（PC 相对，页对齐）
ADD  x0, x0, :lo12:gvar   ← 加上页内偏移（低 12 位）
```

为什么分两步？因为 AArch64 的**单条指令放不下 64 位地址**（指令本身只有 32 位）。所以拆成：

- `ADRP`：算出 4KB 页基址（21 位立即数 × 4096 = ±4GB 范围）
- `ADD`/`LDR`：加上页内偏移（12 位 = 0..4095）

这就是 `_HI21`（高 21 位）和 `_LO12`（低 12 位）的含义。

> **这是 AArch64 汇编里出现频率最高的模式**。看到 `adrp` 就找配对的 `add` 或 `ldr`，否则地址是错的。

## 目标文件的段布局

```bash
llvm-readobj -S reloc.o
```

实测（节选）：

```
Name: .text (6)
  Size: 72
Name: .rela.text (1)
  Size: 168
  EntrySize: 24
Name: .data (119)
  Size: 4
Name: .note.GNU-stack (46)
  Size: 0
```

| 段 | 内容 | 本实测大小 |
|---|---|---|
| `.text` | 机器指令 | 72 字节 |
| `.rela.text` | 重定位表 | 168 字节 |
| `.data` | 已初始化全局变量（`gvar = 42`） | **4 字节** |
| `.note.GNU-stack` | 标记栈不可执行 | 0 |

**注意重定位表比代码还大**（168 > 72）。这在小型目标文件里很常见——重定位条目的开销是每条 24 字节。

`.data` 只有 4 字节，因为 `gvar` 是 `int`。`local` 是 `static`，也被放进了 `.data`（编译器可能把它折叠成常量）。

## 链接：填空

```bash
# 静态链接（边缘设备用这个）
aarch64-linux-gnu-gcc -static reloc.o main.o -o prog

# 或者用 lld
ld.lld -static reloc.o main.o -o prog
```

链接器做的事：

```
① 把所有 .o 的 .text 拼起来，分配地址
② 把所有 .data / .bss 拼起来，分配地址
③ 遍历每条重定位：算出实际值，写回指令
④ 解析符号：ext_var 在哪个 .o 里定义？
⑤ 生成可执行文件头（ELF header + program headers）
```

第 ③ 步的具体动作：`R_AARCH64_CALL26` 要求在 `bl` 指令的 bit[25:0] 填入 `(target - pc) >> 2`。

## 边缘设备：为什么必须静态链接

```bash
# 动态链接
$ ldd ./prog
    libc.so.6 => /lib/aarch64-linux-gnu/libc.so.6

# 静态链接
$ file ./prog
prog: ELF 64-bit LSB executable, ARM aarch64, statically linked
```

三个原因：

1. **没有动态链接器**：裸机没有 `ld-linux-aarch64.so.1`
2. **没有文件系统**：加载不到 `.so`
3. **确定性与启动时间**：静态链接不需要运行时符号解析

代价：**二进制变大**。本课前面实测的 AArch64 例子：

```bash
aarch64-linux-gnu-gcc -static -O2 add.c drv.c -o t_a64
ls -la t_a64
```

实测：**641112 字节**（约 626 KB）。而 `add.c` 只有一行代码。

**这 626 KB 几乎全是 glibc 的静态版本**。这就是 Ch 23「算子库还是代码生成」的核心矛盾——静态链接把整个库都塞进去了。

> **嵌入式实践**：用 `-Os` 优化体积，用 `-Wl,--gc-sections` 删除未引用段，或者干脆不用 glibc（用 newlib / picolibc）。

## Cortex-M 的链接脚本

裸机没有默认链接器脚本，必须自己写：

```ld
MEMORY {
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}
ENTRY(reset_handler)
SECTIONS {
  .text : {
    KEEP(*(.vectors))     ← 向量表必须在最前面（地址 0）
    *(.text*)
    *(.rodata*)
  } > FLASH
  .data : { *(.data*) } > RAM AT > FLASH   ← 运行时在 RAM，但初值存在 FLASH
  .bss  : { *(.bss*) *(COMMON) } > RAM
}
```

三个关键点：

1. **`KEEP(*(.vectors))`**：向量表必须在 Flash 起始地址。`KEEP` 防止 `--gc-sections` 把它删掉（它没被任何代码引用）。
2. **`AT > FLASH`**：`.data` 的初值存在 Flash 里，启动代码负责拷贝到 RAM。
3. **`.bss` 清零**：启动代码负责把 `.bss` 区清零（C 标准要求未初始化全局变量为 0）。

**这三件事（拷 `.data`、清 `.bss`、设栈指针）没有编译器会替你做**，是启动代码的责任。Ch 19 会完整实现。

本课实测的 Cortex-M 固件大小：

```
   text	   data	    bss	    dec	    hex	filename
    289	      0	      0	    289	    121	firmware.elf
```

289 字节——因为只有 `compute(6,7)`。真实模型会有几十 KB 的 `.rodata`（权重）。

## 踩雷集锦

1. **`adrp` 不配对**：看到 `adrp x0, sym` 必须找到 `add x0, x0, :lo12:sym` 或 `ldr ... :lo12:`。少了它，地址是页对齐的，偏低 0–4095 字节。
2. **以为 `bl` 能跳任意远**：`CALL26` 只有 ±128MB。大型程序靠 PLT 桩间接跳转。
3. **动态链接上裸机**：链接期就失败（找不到 `ld-linux-aarch64.so.1`）。
4. **静态链接不管体积**：实测 626 KB 二进制里，绝大部分是没用的 libc。
5. **裸机忘了 `KEEP(*(.vectors))`**：`--gc-sections` 会把向量表删掉，运行立刻跑飞（reset 后读到的栈指针是垃圾值）。

## 本章重点整理

- 目标文件里的地址是「待填的空」，重定位表记录所有空位。
- 外部变量走 GOT（多一次内存访问），函数调用走相对偏移（±128MB 限制）。
- AArch64 访问全局变量必须 `ADRP` + `ADD/LDR` 配对，因为单条指令放不下 64 位地址。
- 边缘设备必须静态链接，代价是二进制膨胀（实测 626 KB）。
- 裸机链接脚本要处理三件事：向量表位置、`.data` 初值拷贝、`.bss` 清零。

## 自我检核

- [ ] 能解释「重定位」是什么，以及为什么目标文件里的地址不能直接填
- [ ] 看到 `R_AARCH64_ADR_PREL_PG_HI21` 会去找配对的哪条重定位
- [ ] 知道外部变量为什么比本模块变量多一层间接（GOT）
- [ ] 能说出静态链接在边缘设备上必需的三条原因
- [ ] 知道裸机链接脚本必须做哪三件事，以及漏掉会怎样

## 延伸阅读

### 官方文档

- **[AArch64 ELF ABI](https://github.com/ARM-software/abi-aa/blob/main/aaelf64/aaelf64.rst)**
  - **读哪里**：`Relocation Codes` 一节，`R_AARCH64_ADR_PREL_PG_HI21` / `LDST32_ABS_LO12_NC` / `CALL26` 的精确定义
  - **为什么值得读**：本章重定位类型的权威来源，包含位域布局
- **[GNU ld — Linker Scripts](https://sourceware.org/binutils/docs/ld/Scripts.html)**
  - **读哪里**：`MEMORY`、`SECTIONS`、`AT` 三节
  - **和本章的关联**：Cortex-M 链接脚本的语法依据

### 源码依据

- **[LLVM AArch64 重定位实现](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AArch64/MCTargetDesc/AArch64ELFObjectWriter.cpp)**
  - **读哪里**：`getRelocType` 里的 switch
  - **为什么值得读**：想知道「这个符号访问会生成哪条重定位」时，这里定义了规则

### 论文

- **[ELF: Executable and Linkable Format](https://refspecs.linuxfoundation.org/elf/elf.pdf)** — System V ABI 规范
  - **核心贡献**：ELF 文件格式的完整定义
  - **读哪里**：Chapter 4（Object Files）、Chapter 5（Program Loading）
  - **和本章的关联**：`.text` / `.data` / `.rela` 这些段的定义来源

- **[Linkers and Loaders](https://www.iecc.com/linker/)** — John R. Levine, 2000
  - **核心贡献**：链接器工作原理的经典著作
  - **读哪里**：Chapter 3（Object Files）、Chapter 5（Relocation）
  - **和本章的关联**：本章「填空」类比的系统化展开

### 博客 / 工程文章

- **[MaskRay — AArch64 relocation 详解](https://maskray.me/blog/2021-03-14-the-arm-32-bit-abi)**
  - **这系列说什么**：ELF 重定位的实战细节，作者是 LLVM/LLD 维护者
  - **读哪里**：搜 `AArch64` 相关文章；`adrp` 与 `:lo12:` 的解释尤其清楚
  - **为什么值得读**：比规范更好读，且作者就是实现者

### 书籍

- **《Linkers and Loaders》** — John R. Levine（2000）
  - **读哪几章**：第 3、5、6 章
  - **注意**：书成于 2000 年，ELF 细节仍适用，但现代（PIE、GOT、PLT 的细节）需要补文档

后端走完了：代码变成了可链接的目标文件。下一章开始进入具体的边缘后端实战，从最常见的 AArch64 开始。

→ [Ch 18 AArch64 后端实战](./18-aarch64-backend.md)
