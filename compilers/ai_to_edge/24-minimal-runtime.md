# Ch 24 最小运行时

> **目标**：看清一个 289 字节的 `firmware.bin` 为什么能跑，以及往它上面加一个真实模型时，必须补哪些东西：启动代码、`memcpy`、分配器、输出、错误处理。学完能自己写 `reset_handler`、能判断 `-nostdlib` 之后哪些符号会缺、能解释 elf 与 bin 差了两个数量级的原因。
>
> **环境**：`arm-none-eabi-gcc` 10.3 / `qemu-system-arm` 6.2（`-M mps2-an385 -cpu cortex-m3`）/ Ubuntu 22.04

## 为什么需要这个？

Ch 22 讲了 arena，Ch 23 讲了算子和库。把这两样放进一个裸机设备，还差最后一层胶水。

这层胶水在桌面上由操作系统提供：加载器放程序、C 运行时初始化全局变量、libc 提供 `memcpy`、shell 提供 `printf`。裸机上这些全没有。

本课实测的最小固件（`cd code/05_cortexm && ./build.sh`）：

```
   text	   data	    bss	    dec	    hex	filename
    289	      0	      0	    289	    121	firmware.elf
== qemu-system-arm (mps2-an385) ==
cortex-m3 alive
42
```

`firmware.elf` 66632 B，`firmware.bin` 289 B。289 字节里装下了向量表、初始化 UART 的代码、一个循环累加函数、一个整数打印函数。它为什么这么小？因为它**没有运行时**：没有一个字节花在「让 C 语言正常工作」上。一旦需要 `.data`、`memcpy`、堆，你就得自己把运行时一块块搭出来。这一章就搭这一层。

## 先建立直觉

把上电想成**搬进一间空房子**：

| 步骤 | 做什么 | 谁负责 |
|---|---|---|
| 1 | 站到正确的位置（设置 SP） | 硬件读向量表第 0 项 |
| 2 | 按清单摆好家具（`.data` Flash → RAM） | 你的 `reset_handler` |
| 3 | 腾空房间（`.bss` 清零） | 你的 `reset_handler` |
| 4 | 开门营业（进 `main`） | 你的 `reset_handler` |

硬件只做第 1 步的一半：读地址 0 的 4 字节装进 SP，读地址 4 的 4 字节跳过去。**其余全是你的事。**

`firmware.bin` 前 64 字节（`od -An -tx4 -N64 firmware.bin`）：

```
 20010000 0000006d 0000005d 0000005d
 0000005d 0000005d 0000005d 0000005d
*
```

第 0 个字 `20010000` 是初始 SP，即 `0x20010000`（RAM 顶端 `0x20000000 + 64K`）；第 1 个字 `0000006d` 是 `reset_handler` 地址（0x6c | 1，Thumb 位）；后面 14 个字全是 `0000005d`，即 `default_handler`（0x5c | 1）。16 项向量表只有前两项有意义，其余全指向同一个「打印 FAULT 然后死循环」的函数。这就是「最小」：**不为不会发生的事预留代码**。

## 底层机制

### 1. `reset_handler` 的四件事

```c
extern char _sdata[], _edata[], _sidata[], _sbss[], _ebss[];

void reset_handler(void) {
  /* SP 已由硬件从向量表 [0] 装好，这里不用管 */
  UART_CTRL = 3u;                                       /* 先让输出可用 */
  memcpy(_sdata, _sidata, (unsigned)(_edata - _sdata));  /* .data: FLASH → RAM */
  memset(_sbss, 0, (unsigned)(_ebss - _sbss));           /* .bss 清零 */
  main();
}
```

四个符号全部来自链接脚本：

```ld
.data : { _sdata = .; *(.data*) _edata = .; } > RAM AT > FLASH
_sidata = LOADADDR(.data);
.bss  : { _sbss = .; *(.bss*) *(COMMON) _ebss = .; } > RAM
```

`_sdata`/`_edata` 是 `.data` 在 RAM 的起止；`_sidata` 是初值在 Flash 的位置（`LOADADDR` 是关键）；`_sbss`/`_ebss` 是 `.bss` 起止。顺序有讲究：先初始化 UART 再拷贝，看似违反直觉（拷贝期间出错就没法打印），但反过来更糟——UART 初始化若依赖还在未清零 `.bss` 里的全局变量，会得到随机波特率。

### 2. UART：CMSDK APB，不是 PL011

MPS2-AN385 的串口在 `0x40004000`，是 CMSDK APB UART：`DR@0x00`（写即发送）、`STATE@0x04`（bit0 = TX full，bit1 = RX full）、`CTRL@0x08`（bit0 = TX enable，bit1 = RX enable）。

```c
#define UART0_BASE 0x40004000u
#define UART_DR    (*(volatile uint32_t *)(UART0_BASE + 0x00))
#define UART_STATE (*(volatile uint32_t *)(UART0_BASE + 0x04))
#define UART_CTRL  (*(volatile uint32_t *)(UART0_BASE + 0x08))

static void putc_(char c) {
  while (UART_STATE & 0x1u) { }   /* 等 TX 不满 */
  UART_DR = (uint32_t)c;
}
```

它**不是 PL011**（PL011 的 `FR` 在 `0x18`、bit5 是 TXFF）。搞混的后果不是立刻报错，而是「碰巧能跑」或「永远卡住」，全看你写的极性。

先看这块 APB 外设在 `0x18` 上到底返回什么。写一个探针，读 `STATE`、`0x40004018`、`0x4000401c` 并打印（实测）：

```
STATE=0x00000000
READ18=0x00000000
READ1c=0x00000000
bit5_of_18=0x00000000
```

`0x18` 读回 **0**，bit5 是 0。于是两种 PL011 写法结果相反：

```c
/* 写法 A：PL011 惯用的「等 TXFF 清零」——bit5 恒 0，条件恒假，直接跳过 */
while (UART_FR & (1u << 5)) { }
```

```c
/* 写法 B：把极性写反——bit5 恒 0，!0 为真，永远循环 */
while (!(UART_FR & (1u << 5))) { }
```

写法 A 实测照样打出 `cortex-m3 alive` 和 `42`，**因为循环一次都没进**；写法 B 实测 3 秒零输出：

```
qemu-system-arm: terminating on signal 15 from pid 1497002 (timeout)
```

**这才是最阴的地方：错误的寄存器写法在 QEMU 上可能完全正常。** 每次发送都不等硬件，靠 QEMU 的 FIFO 够深侥幸过关；换成真实芯片或更高主频就是丢字符或死等。`bit5_of_18=0` 是判据：真正的 PL011 在 TX FIFO 不满时 bit5 也是 0，但 `FR` 还有 bit0、bit4 等有意义位；CMSDK APB 的 `0x18` 读回全 0，说明这个地址根本不是状态寄存器。**不要用「跑通了」证明外设寄存器用对了，要看地址和位定义对上没有。**

### 3. 链接脚本：三个必须成立的条件

```ld
MEMORY {
  FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 64K
}
ENTRY(reset_handler)
SECTIONS {
  .text : {
    KEEP(*(.vectors))
    *(.text*)
    *(.rodata*)
  } > FLASH
  .data : { *(.data*) } > RAM AT > FLASH
  .bss  : { *(.bss*) *(COMMON) } > RAM
}
```

1. **`KEEP(*(.vectors))` 必须在 `.text` 最前面。** 向量表要落在地址 0，而 `--gc-sections` 会把它当「没人引用」删掉。
2. **`.data > RAM AT > FLASH`。** 运行在 RAM，初值在 Flash。
3. **`.bss` 清零是启动代码的责任。** C 标准要求未初始化全局变量为 0，编译器不生成这段。

第 1 条漏掉会怎样？实测把 `.vectors` 挪到 `*(.text*)` 之后：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -Wl,-T,bad.ld \
  -Wl,--gc-sections -O2 startup.c compute.c -o fw_badvec.elf
od -An -tx4 -N16 fw_badvec.bin
```

```
 b1497801 2240f04f 07db6853 6011d4fc
```

前 8 字节变成了指令编码（`b1497801` 是某条 `ldr`/`str`），不是 `20010000 0000006d`。上电后硬件把指令当 SP、把下一条当入口，行为不可预测。实测运行：**一个字都没输出**，3 秒超时。

### 4. elf 与 bin 差在哪

```
firmware.elf   66632 B
firmware.bin      289 B
```

差 66343 字节。`arm-none-eabi-objdump -h firmware.elf` 只列出三个有内容的段：

```
Idx Name          Size      VMA       LMA       File off  Algn
  0 .text         00000121  00000000  00000000  00010000  2**2
  1 .comment      00000033  00000000  00000000  00010121  2**0
  2 .ARM.attributes 0000002d  00000000  00000000  00010154  2**0
```

`objcopy -O binary` 只导出有 `LOAD` 属性的 `.text`（`0x121 = 289` 字节）。其余按 `readelf -S` 拆开：

| 部分 | 字节 |
|---|---|
| 文件头 + 1 个程序头 | 52 + 32 = 84 |
| 对齐填充（`Offset = 0x010000`） | 65452 |
| `.text` | 289 |
| `.comment` + `.ARM.attributes` | 51 + 45 = 96 |
| `.symtab` + `.strtab` + `.shstrtab` | 288 + 79 + 58 = 425 |
| 节间填充 + 节头表（7 × 40） | 6 + 280 = 286 |
| **合计** | **66632** |

填充来自程序头的 `Offset`：

```
  Type    Offset   VirtAddr   PhysAddr   FileSiz  MemSiz   Flg Align
  LOAD    0x010000 0x00000000 0x00000000  0x00121  0x00121  R E 0x10000
```

**`Offset = 0x010000`。** `.text` 从 `0x00000000` 开始、段对齐是 `0x10000`，链接器为满足对齐在文件头后填了 64 KB 空洞。所以 elf 里 **65452 B 是填充，289 B 是代码，其余 891 B 是 elf 头与元数据**。烧到 Flash 的是 bin，不是 elf。

### 5. 优化级别：-O0 469 B / -O2 289 B / -Os 240 B

同一份 `startup.c` + `compute.c`，`.text` 实测：`-O0 469 B` / `-O2 289 B` / `-Os 240 B`，data 与 bss 都是 0。`-O0`→`-O2` 省 180 字节（38%），主要是栈帧 `push/pop` 和冗余 `ldr`；`-O2`→`-Os` 再省 49 字节（17%）。289 字节时像噪声，但比例会放大：推理二进制 200 KB 时可能省 60 KB，而 Flash 只有 256 KB。**在边缘上，优化级别是容量决策，不是性能决策。**

### 6. `-nostdlib` 之后缺什么

`-nostdlib` 关掉的不只是 `printf`，还有编译器**自动生成**的库调用。写一个只做赋值和清零的文件：

```c
typedef struct { uint32_t a[16]; } Blob;
void copy_blob(Blob *dst, const Blob *src) { *dst = *src; }
void zero_blob(Blob *b) { for (int i = 0; i < 16; i++) b->a[i] = 0; }
```

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -O2 -c nostd2.c -o nostd2.o
arm-none-eabi-nm -u nostd2.o
```

```
         U memset
```

`-O0` 没有未定义符号（逐字节循环），`-O2`/`-Os` 都把清零循环识别成 `memset`。链接时：

```
nostd2.c:(.text+0x30): undefined reference to `memset'
```

**你没有写 `memset`，是编译器替你写的。** 同样的机制会生成 `memcpy`（结构体拷贝）、`memmove`（重叠拷贝）、`__aeabi_*`（除法、浮点、64 位）。Ch 15 实测过 `i64 /` 生成 `bl __aeabi_ldivmod`。这些符号必须自己提供——但注意，自己写的版本会被同样的机制再咬一次，见例子二。

### 7. 最小运行时清单

把上面这些拼起来，一个能跑模型的裸机运行时包含五层：

| 层 | 内容 | 本章状态 |
|---|---|---|
| 启动代码 | 向量表、SP、`.data` 拷贝、`.bss` 清零 | 完整实现 |
| 内存分配器 | arena（Ch 22）或 `_sbrk` + 自己的堆 | 只需 arena |
| 算子实现 | 手写循环或 CMSIS-NN（Ch 23） | 不在本章范围 |
| 错误处理 | HardFault / `default_handler` | 只打印 `FAULT` |
| 输出 | UART 轮询发送 | 已实现，无缓冲 |

`-nostdlib` 之后要自己提供的符号，按「谁会生成它」分类：

| 符号 | 谁生成 | 什么时候出现 |
|---|---|---|
| `memcpy` | 编译器（结构体拷贝、大块赋值） | `-O2` 以上，或显式调用 |
| `memset` | 编译器（清零循环） | `-O2` 以上 |
| `memmove` | 编译器（可能重叠的拷贝） | 不确定来源的指针 |
| `__aeabi_ldivmod` | 编译器（64 位除法） | Ch 15 实测 |
| `__aeabi_fmul` | 编译器（软浮点乘法） | Cortex-M3 无 FPU |

前四个在 `arm-none-eabi-nm -u` 里看得到；最后两个只有在真正用到时才会被拉进来。

## 例子一：给固件加上 `.data` 和 `.bss`

原版固件 `data 0 / bss 0`，变量都在寄存器或栈上。加三个全局变量：

```c
volatile int g_init = 123;                        /* → .data */
volatile int g_zero;                              /* → .bss */
volatile int g_arr[8] = {1,2,3,4,5,6,7,8};        /* → .data */
```

**不写启动代码**（只初始化 UART 再打印）：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib \
  -Wl,-T,link.ld -Wl,--gc-sections -O2 ex1.c -o nofix.elf
```

```
   text	   data	    bss	    dec	    hex	filename
    354	     36	      4	    394	    18a	nofix.elf
```

QEMU 实测输出：

```
g_init=0
g_zero=0
g_arr[7]=0
```

**`g_init` 应该是 123，`g_arr[7]` 应该是 8。** `.data` 初值还在 Flash（`_sidata`），链接器把 LMA 放那儿、VMA 放 RAM，中间没有硬件会搬运——Cortex-M 没有 MMU，也没有 loader。QEMU 的 RAM 上电为 0，所以「碰巧对了」；真实硬件上是残留随机值，`g_zero=0` 同理。**模拟器通过、硬件随机崩**，是这一类里最危险的。

补上启动代码。这里故意用字节循环而不是 `memcpy`，原因在例子二：

```c
volatile char *d = _sdata;
const char *f = _sidata;
while (d < (volatile char *)_edata) *d++ = *f++;          /* .data: FLASH → RAM */
for (volatile char *b = _sbss; b < (volatile char *)_ebss; b++) *b = 0;  /* .bss 清零 */
```

`volatile` 不是装饰：去掉它，GCC 会把这个循环识别成 `memcpy` 模式换成库调用，而 `-nostdlib` 下没有那个库。实测输出：

```
g_init=123
g_zero=0
g_arr[7]=8
```

`size` 变成 `text 414 / data 36 / bss 4`：text 多 60 字节（354 → 414）。两条十几字节的循环加几条装载指令而已；**拷贝 36 字节的 `.data`，手写循环比调库便宜**。`readelf -l` 也看到两个 LOAD 段：

```
LOAD 0x010000 0x00000000 0x00000000 0x0019e 0x0019e R E 0x10000
LOAD 0x020000 0x20000000 0x0000019e 0x00024 0x00028 RW  0x10000
```

第二个段 `VirtAddr = 0x20000000`（RAM）、`PhysAddr = 0x0000019e`（Flash，紧跟代码）。这是 `AT > FLASH` 在程序头里的样子：**运行地址与加载地址分开**。判断标准不是「跑出结果了吗」，而是「初始化路径存在吗」。

## 例子二：`memset` 把自己递归了

```c
void *memset(void *d, int c, unsigned int n) {
  unsigned char *dd = d;
  while (n--) *dd++ = (unsigned char)c;
  return d;
}
```

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -Wl,-T,link.ld \
  -Wl,--gc-sections -O2 ex2.c -o rt3.elf
```

反汇编（`text 463 / data 36 / bss 0`）：

```asm
000000cc <memset>:
  cc:  b510       push  {r4, lr}
  ce:  4604       mov   r4, r0
  d0:  b112       cbz   r2, d8 <memset+0xc>
  d2:  b2c9       uxtb  r1, r1
  d4:  f7ff fffa  bl    cc <memset>     ← 调用自己
  d8:  4620       mov   r0, r4
  da:  bd10       pop   {r4, pc}
```

**`bl cc <memset>`：`memset` 的第三条有效指令就是调用 `memset`。** GCC 在 `-O2` 下把 `while (n--) *dd++ = c;` 识别成 memset 模式，函数体被替换成一次 `memset` 调用；函数名恰好也叫 `memset`，于是调用自己，反复进栈直到 HardFault。运行现象：

```
g_init=123
g_arr[7]=8
A
B
C
qemu-system-arm: terminating on signal 15 from pid 1490107 (timeout)
```

`g_init` 和 `g_arr[7]` 靠例子一的启动拷贝，与 `memset` 无关，所以先打出来；`A B C` 也出来了，因为 UART 不依赖 RAM。到 `D` 之前调用 `memset` 立刻自递归，栈长到 RAM 顶端触发 HardFault，`D` 永远不出现。修法：`-fno-builtin`。

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -fno-builtin \
  -Wl,-T,link.ld -Wl,--gc-sections -O2 ex2.c -o rt4.elf
```

```asm
000000cc <memset>:
  cc:  b132       cbz   r2, dc <memset+0x10>
  ce:  4603       mov   r3, r0
  d0:  b2c9       uxtb  r1, r1
  d2:  4402       add   r2, r0
  d4:  f803 1b01  strb.w r1, [r3], #1
  d8:  4293       cmp   r3, r2
  da:  d1fb       bne.n d4 <memset+0x8>
  dc:  4770       bx    lr
  de:  bf00       nop
```

现在是真的逐字节循环。实测输出：

```
g_init=123
g_arr[7]=8
A
B
C
D
g_arr[7] after memset=0
```

`size` 从 `text 463 / data 36 / bss 0` 变成 `text 467 / data 36 / bss 0`：只多 4 字节，省掉的那次 `push {r4, lr}` 恰好抵消了循环体扩张。通用形式：**你实现的标准库函数会被编译器当成标准库函数优化**，`memcpy`、`memset`、`memmove`、`strlen` 都有这个问题。解法是 `-fno-builtin`，或只关掉这一个变换（`-fno-tree-loop-distribute-patterns`）。

## 踩雷集锦

1. **`-nostdlib` 之后以为只要不调用 `printf` 就没有库依赖。**
   `-O2` 下编译器会自动生成 `memset`（清零循环）、`memcpy`（结构体拷贝）、`__aeabi_ldivmod`（64 位除法）。实测 `nm -u nostd2.o` 输出 `U memset`，链接报 `undefined reference to 'memset'`（源码只做了赋值和清零，从没调用过它）。
   **纠正**：每次改优化级别后跑 `arm-none-eabi-nm -u *.o`，把所有 `U` 符号逐个提供或消除。

2. **自己实现 `memset`，然后被优化成自我递归。**
   `while (n--) *dd++ = c;` 在 `-O2` 下被 GCC 识别为 memset 模式，替换成 `bl memset`，正好指向自己。实测反汇编 `bl cc <memset>`，`A B C` 之后卡死。
   **纠正**：用 `-fno-builtin`，或用 `-fno-tree-loop-distribute-patterns` 只关掉这一个变换。改完必须看反汇编确认没有自调用。

3. **忘了清 `.bss`，并且以为「QEMU 跑对了就没问题」。**
   QEMU 的 RAM 上电为 0，所以漏掉 `.bss` 清零在模拟器上完全看不出来。真实硬件上电残留值随机，同一个固件可能「第一次跑正常，复位后崩」。
   **纠正**：`reset_handler` 里必须显式清零 `_sbss` 到 `_ebss`。验证方法是让 `.bss` 里放一个非零初值再跑，看它是否变成 0。

4. **`.data` 没从 Flash 拷到 RAM。**
   实测漏掉 `.data` 拷贝时，`g_init` 打印 0（应为 123）、`g_arr[7]` 打印 0（应为 8）。Cortex-M 没有 loader，链接器也不生成搬运代码。
   **纠正**：链接脚本写 `> RAM AT > FLASH` + `_sidata = LOADADDR(.data)`，启动代码里从 `_sidata` 拷到 `_sdata`。用 `readelf -l` 确认第二个 LOAD 段 `Vaddr` 在 RAM、`Paddr` 在 Flash。

5. **向量表没放段首。**
   实测把 `KEEP(*(.vectors))` 挪到 `*(.text*)` 之后，bin 前 8 字节变成 `b1497801 2240f04f`（指令，不是 SP 和入口地址），运行一个字都不输出。
   **纠正**：`.text` 里 `KEEP(*(.vectors))` 必须是第一条；`KEEP` 不能省，否则 `--gc-sections` 会删掉它。验证方法：`od -An -tx4 -N8 firmware.bin`，前两个字应该是 SP 和奇数入口地址。

## 本章重点整理

- 硬件只做两件事：地址 0 读 SP、地址 4 读入口。其余初始化是 `reset_handler` 的责任：初始化输出、`memcpy` 搬 `.data`、`memset` 清 `.bss`、进 `main`。
- MPS2-AN385 的 UART 是 CMSDK APB（`DR@0x00` / `STATE@0x04` bit0 / `CTRL@0x08`），不是 PL011。实测 `0x40004018` 读回 0：按 PL011 的「等 bit5 清零」写法会一次都不等，极性写反则永久等待。
- 链接脚本三个条件：`KEEP(*(.vectors))` 在最前、`.data > RAM AT > FLASH`、`.bss` 交给启动代码清。实测把向量表挪走，bin 前 8 字节变成指令编码，运行无输出。
- elf 66632 B vs bin 289 B：65452 B 是 `Offset = 0x010000` 的对齐填充，289 B 是代码，其余 891 B 是节头表/符号表/注释。
- `-O0 469 B / -O2 289 B / -Os 240 B`。在边缘上优化级别是容量决策。
- `-nostdlib` 之后编译器自动生成的 `memset` / `memcpy` / `__aeabi_*` 全要自己提供。自己写的 `memset` 会被 `-O2` 识别成 memset 模式并自我递归（实测 `bl cc <memset>`），必须 `-fno-builtin`。

## 自我检核

- [ ] 能不看书写出 `reset_handler` 的四个步骤，并说明每一步漏掉会出现什么现象
- [ ] 能解释为什么 QEMU 上「忘了清 `.bss`」看不出来，以及怎么设计一个能看出来的验证
- [ ] 能说出 CMSDK APB 与 PL011 的寄存器差异，以及用错之后的运行现象
- [ ] 能解释 elf 66632 B 与 bin 289 B 之间的 66343 字节分别是什么
- [ ] 能用 `nm -u` 找出 `-nostdlib` 下的所有缺失符号，并说明它们为什么会凭空出现
- [ ] 能解释自己写的 `memset` 为什么会在 `-O2` 下变成自我递归，以及两种修法

## 延伸阅读

- **[Memfault — From Zero to main(): Bare metal C](https://interrupt.memfault.com/blog/zero-to-main-1)**
  - **读哪里**：`The Reset Handler`、`Copying .data`、`Zeroing .bss` 三节
  - **学什么**：启动代码的标准写法，以及为什么每一步都不能省
  - **和本章关联**：本章 `reset_handler` 的对照实现

- **[GNU ld — Linker Scripts](https://sourceware.org/binutils/docs/ld/Scripts.html)**
  - **读哪里**：`MEMORY`、`SECTIONS`、`AT`、`LOADADDR` 四节
  - **学什么**：VMA 与 LMA 的区别，`AT > FLASH` 改了什么
  - **和本章关联**：`.data > RAM AT > FLASH` 与 `_sidata` 的依据

- **[QEMU — Arm MPS2 boards](https://www.qemu.org/docs/master/system/arm/mps2.html)**
  - **读哪里**：`mps2-an385` 一节的内存映射与外设列表
  - **学什么**：UART 基址 `0x40004000`、RAM 起始 `0x20000000` 的官方来源
  - **和本章关联**：本章所有地址的出处，也是判断「是不是 PL011」的入口

- **[Armv7-M Architecture Reference Manual](https://developer.arm.com/documentation/ddi0403/latest/)**
  - **读哪里**：B1.5.5（Exception entry）、B3.2（`VTOR`）、A2.3（Thumb 位）
  - **学什么**：为什么向量表里的函数地址最低位是 1，复位时硬件读哪两个地址
  - **和本章关联**：`0000006d` 这个奇数值的架构依据

- **[newlib 文档](https://sourceware.org/newlib/)**
  - **读哪里**：`Reentrancy`、`System Calls` 两节，以及 `--specs=nosys.specs` 的说明
  - **学什么**：一个完整 C 运行时需要哪些 syscall stub（`_sbrk`、`_write`、`_close`…）
  - **和本章关联**：本章手写的是最小子集，这里能看到完整版

到这里，模型已经能在裸机上跑起来。可它跑出来的数字对不对？下一章处理浮点、归约顺序与量化误差。

→ [Ch 25 数值正确性：逐层对齐](./25-numerical-correctness.md)
