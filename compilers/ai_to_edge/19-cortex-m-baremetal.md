# Ch 19 — Cortex-M 裸机后端：没有 OS 的世界

> **目标**：把一个函数编到一块没有操作系统、没有 libc、没有 `malloc`、连串口都不是标准件的芯片上，并让它真的打印出数字。学完这章，你能从零写出一份可启动的固件，能解释向量表里那 16 个指针各是什么、为什么入口地址是奇数、`.data` 为什么必须从 FLASH 拷到 RAM，以及 `-nostdlib` 之后编译器偷偷依赖了哪些函数。

> **环境**：`arm-none-eabi-gcc` 10.3.1、`qemu-system-arm` 6.2（`-M mps2-an385 -cpu cortex-m3`）、Ubuntu 22.04。全部在 `code/05_cortexm/`（最小版）与 `code/14_startup/`（完整版）可复现。

> **证据分级**：标「实测」的都是本机在 QEMU 里跑出的真实输出。Cortex-M 的**时序**（周期数、中断延迟）无法在 QEMU 里测，凡涉及处标「未实测」。

## 为什么需要这个？

Ch 18 的 AArch64 程序跑在 Linux 上。它能跑，是因为背后有一整套东西：

```
你的程序 → libc（printf/malloc）→ 系统调用 → Linux 内核 → 硬件
```

`add(3,4)` 那个 641 KB 的静态二进制里，你的 `add` 只占 12 字节，其余全是这套支撑。

Cortex-M 上这些**全都没有**。不是「被精简了」，是「从未存在」：

| Linux 上的东西 | MCU 上 |
|---|---|
| 进程、地址空间 | 只有一个地址空间，程序就是整个系统 |
| `main()` 被 libc 调用 | 复位后从向量表第 2 项开始执行，没有 `main` |
| 栈由内核建立 | 初始栈指针写在向量表第 1 项里，硬件加载 |
| `printf` 写 stdout | 没有 stdout，要自己往 UART 寄存器写字节 |
| `malloc` | 没有堆管理器，要么静态分配，要么自己写 arena |
| 缺页、段错误 | 没有 MMU，野指针直接读到随机值或触发 HardFault |
| `exit()` | 没有退出。函数返回无处可去，要么死循环，要么复位 |

这份清单的每一项都会在编译或运行阶段咬你一口。本章按「从复位到打印出 42」的顺序，逐项处理。

## 先建立直觉

裸机启动是一条**硬件把控制权交给你的约定**，不是函数调用。

```
上电 / 复位
    │
    ├─ 硬件读 0x00000000 处的 4 字节 → 装进 SP        ← 向量表第 1 项
    │
    ├─ 硬件读 0x00000004 处的 4 字节 → 装进 PC        ← 向量表第 2 项
    │       （这个值的最低位必须是 1，见下文）
    │
    ▼
reset_handler()          ← 你的代码从这里开始，没有任何人调用它
    │
    ├─ ① 把 .data 的初值从 FLASH 拷到 RAM
    ├─ ② 把 .bss 清零
    ├─ ③ 初始化要用到的外设（这里是 UART）
    ├─ ④ 跑应用
    └─ ⑤ 死循环（不能 return，return 的目标地址是垃圾）
```

关键差别：**没有调用者**。`reset_handler` 的返回地址寄存器 `lr` 里是复位前的随机值，所以它绝对不能 `return`。这就是为什么每个裸机 `main` 的最后都是一句 `for(;;){}`。

**为什么入口地址是奇数？** 这一点很反直觉，值得单独说。实测 `readelf -h` 的输出：

```
Entry point address:               0x6d
```

而 `nm` 给出的符号地址是：

```
0000006c T reset_handler
```

`0x6d = 0x6c | 1`。**最低位不是地址的一部分，是「这是 Thumb 代码」的标志位。** Cortex-M 只支持 Thumb 指令集（不支持 ARM 32 位定长指令），硬件在跳转时会检查这一位：是 1 就按 Thumb 解码，是 0 就触发 UsageFault。所以向量表里那个指针、函数指针、以及任何「跳到代码」的地方，地址的最低位都必须是 1。

编译器在取函数地址时会自动加 1（`(void*)reset_handler` 里已经加了）。你手写汇编或者从二进制里抠地址时，必须自己加。这是裸机开发的第一个陷阱。

## 底层机制一：向量表是 16 个指针的数组

实测 `startup.c` 里的定义：

```c
__attribute__((section(".vectors"), used))
const void *vectors[16] = {
    (void *)0x20010000,   /* initial stack pointer */
    (void *)reset_handler,
    (void *)default_handler, (void *)default_handler,
    /* ... 共 14 个 default_handler ... */
};
```

烧进芯片后，前 32 字节的实测内容是：

```
00000000: 0000 0120 6d00 0000 5d00 0000 5d00 0000  ... m...]...]...
00000010: 5d00 0000 5d00 0000 5d00 0000 5d00 0000  ]...]...]...]...
```

逐项解：

| 偏移 | 字节 | 小端解释 | 含义 |
|---|---|---|---|
| `0x00` | `00 00 01 20` | `0x20010000` | 初始 SP（RAM 顶端） |
| `0x04` | `6d 00 00 00` | `0x0000006d` | Reset 处理函数（`0x6c \| 1`） |
| `0x08` | `5d 00 00 00` | `0x0000005d` | NMI（`default_handler` 在 `0x5c`） |
| `0x0c` | `5d 00 00 00` | `0x0000005d` | HardFault |
| … | … | … | 其余 12 个异常 |

**两个细节：**

**第一，SP 是 RAM 顶端 `0x20010000`，不是底端。** Cortex-M 的栈**向下生长**（`push` 先减 SP 再写），所以初始值必须是最高地址。`0x20000000 + 64K = 0x20010000`，正好对应链接脚本里的 `RAM : ORIGIN = 0x20000000, LENGTH = 64K`。**如果这里写成 `0x20000000`，第一次 `push` 就会写到 RAM 之外，直接 HardFault。**

**第二，14 个异常入口全指向同一个 `default_handler`。** 这是最小实现：任何异常都打印 `FAULT` 然后死循环。真实产品会为 HardFault 单独写一个处理器，打印故障状态寄存器（`CFSR`/`HFSR`/`MMFAR`/`BFAR`），否则调试时你只知道「挂了」，不知道「为什么挂」。Ch 27 会讲这套诊断方法。

**`__attribute__((section(".vectors"), used))` 的两个作用：**

- `section(".vectors")` 把这个数组放进名为 `.vectors` 的段
- `used` 阻止编译器把它当「没人引用的变量」删掉——因为**没有任何 C 代码引用它**，引用它的是硬件

链接脚本里那句 `KEEP(*(.vectors))` 是第三重保险：即使 `--gc-sections` 想回收，也保留这个段。**三重保险缺一不可**，少了任何一层，向量表就会消失，芯片复位后跳进随机地址。

## 底层机制二：为什么 `.data` 必须搬、`.bss` 必须清

这是裸机最容易出「莫名其妙」bug 的地方。实测对照（`code/14_startup/`，两个版本只差一段初始化代码）：

```
== 1) 正确版本：带 .data 拷贝与 .bss 清零 ==
start
data ok
bss ok

== 3) 去掉 .data 拷贝会怎样 ==
start
data BAD
bss ok
```

**去掉 `.data` 拷贝，`g_init` 就不是 1234 了。**

原因在链接脚本的 `> RAM AT > FLASH` 这半句：

```
 .data : { _sdata = .; *(.data*) _edata = .; } > RAM AT > FLASH
 _sidata = LOADADDR(.data);
```

两个地址要分清：

- **VMA（Virtual Memory Address，运行地址）**：变量在运行时住在哪。`.data` 的 VMA 在 RAM（`0x20000000`）
- **LMA（Load Memory Address，加载地址）**：初值存在哪。`.data` 的 LMA 在 FLASH（紧接 `.text` 之后）

实测 `objdump -h` 印证了这个分离：

```
  0 .text   00000181  00000000  00000000  ...     ← VMA 0x0, LMA 0x0
  1 .data   00000004  20000000  00000181  ...     ← VMA 0x20000000, LMA 0x181
  2 .bss    00000004  20000004  00000185  ...     ← 只有 VMA，没有 LMA
```

`.data` 那行：VMA `0x20000000`，LMA `0x181`。**同一个段，运行在 RAM，初值躺在 FLASH。** 硬件只会把 FLASH 的内容原样映射到 `0x0` 起始的地址空间，它**不会**自动把 `0x181` 处的 4 个字节搬到 `0x20000000`。这个搬运必须由启动代码做。

`.bss` 那行更彻底：`LMA` 列显示的是下一个可用位置，而 `.bss` 在文件里**不占任何字节**（`ALLOC` 但没有 `CONTENTS`）。它的语义是「运行时在 RAM 里占 4 字节，初值全是 0」。既然文件里没存，就只能靠启动代码写 0。

所以启动代码那两段循环不是「习惯」，是**硬性契约**：

```c
/* ① .data: FLASH(_sidata) → RAM(_sdata.._edata) */
uint32_t *src = &_sidata;
uint32_t *dst = &_sdata;
while (dst < &_edata) *dst++ = *src++;

/* ② .bss: 清零 */
dst = &_sbss;
while (dst < &_ebss) *dst++ = 0;
```

**为什么 `.bss` 那次实测通过了？** 因为 QEMU 启动时把 RAM 清零了。真机上电后 RAM 里是**随机值**（有些芯片是上一次复位前的残留），不清 `.bss` 就会出现「全局变量初值是垃圾」的现象。**QEMU 上通过不代表真机上正确**，这是本章最值得记住的一句。

## 底层机制三：串口不是标准件

要把 42 打印出来，得有输出设备。开发板上通常是一个 UART，但**寄存器的布局每个芯片都不一样**。

MPS2-AN385 板子上是 **CMSDK APB UART**，基址 `0x40004000`：

| 偏移 | 名称 | 作用 |
|---|---|---|
| `0x00` | `DR` | 数据寄存器（写入即发送） |
| `0x04` | `STATE` | bit0 = TX buffer full，bit1 = RX buffer full |
| `0x08` | `CTRL` | bit0 = TX enable，bit1 = RX enable |

`putc` 的全部实现就三行：

```c
static void putc_(char c) {
  while (UART_STATE & 0x1u) { }   /* 等 TX 不忙 */
  UART_DR = (uint32_t)c;
}
```

**这里踩过一个真实的坑，值得展开。** 同一块板子上常常有好几个 UART，而 ARM 的另一类常用串口控制器是 **PL011**（多见于 Versatile Express、树莓派等板子）。PL011 的寄存器布局完全不同：

| 偏移 | PL011 | CMSDK APB |
|---|---|---|
| `0x00` | `DR`（数据） | `DR`（数据） |
| `0x04` | `RSRECR`（错误状态） | **`STATE`** |
| `0x08` | — | **`CTRL`** |
| `0x18` | **`FR`（标志）bit5 = TX FIFO full** | （无此寄存器） |

如果按 PL011 写：

```c
/* 错的：这条 while 在 CMSDK APB UART 上永远为真 */
while (*(volatile uint32_t *)(BASE + 0x18) & (1u << 5)) { }
```

会发生什么？`0x18` 在 CMSDK APB UART 的寄存器表里**不存在**。读一个未映射的地址通常触发总线错误（HardFault）；但很多实现会返回一个固定值，如果那个值恰好的 bit5 是 1，**`while` 永远出不来**。

（这一段是机制推演，**未在本机逐个寄存器验证**：我没有把 `0x18` 的读回值打出来。你可以在自己手上验证——把 `putc_` 的状态检查临时改成读 `0x18`，看 QEMU 是 HardFault 还是挂死。）

**现象是：QEMU 启动了、没有报错、没有输出、也不退出，`Ctrl-C` 才停。** 没有崩溃、没有异常、没有任何线索。这就是嵌入式最难受的一类故障——**不是「错了」，是「什么都不发生」**。

诊断方法和修法见 Ch 27；这里的结论是：**裸机开发里「地址 + 位定义」是从数据手册抄来的，不能靠猜，也不能靠「看起来像」。**

## 底层机制四：`-nostdlib` 之后，编译器还在偷偷依赖函数

`-nostdlib` 的意思是不链接启动文件和标准库。但**编译器仍然会生成对某些库函数的调用**，因为它认为这些函数「反正总有」。

实测，去掉自己写的 `memcpy`/`memset` 后链接直接失败：

```
ld: t2.c:(.text+0x36): undefined reference to `memcpy'
ld: t2.c:(.text+0x4e): undefined reference to `memset'
```

这两处调用不是我在 C 里写的。是编译器把 `.data` 拷贝循环识别成 `memcpy`、把清零循环识别成 `memset`（这叫 **loop idiom recognition**），然后生成了库调用。**在 `-O2` 下这件事一定会发生**，因为 GCC 认为用库函数比展开循环更快。

所以裸机工程必须自己提供这两个函数。但这里还有第二个坑，更深：

### 自己写的 `memset` 会调用自己

第一版启动代码加了自定义 `memset` 之后，固件编译通过、链接通过，但**跑起来一片死寂**。反汇编：

```
00000120 <memset>:
 120:	b510      	push	{r4, lr}
 122:	4604      	mov	r4, r0
 124:	b112      	cbz	r2, 12c <memset+0xc>
 126:	b2c9      	uxtb	r1, r1
 128:	f7ff fffa 	bl	120 <memset>      ← 调用的是自己
 12c:	4620      	mov	r0, r4
 12e:	bd10      	pop	{r4, pc}
```

`0x128` 处的 `bl 120 <memset>` 目标就是 `memset` 自己。**无限递归，每次 `push {r4, lr}` 吃 8 字节栈，很快就撞到 RAM 顶端，然后 HardFault 或者原地打转。**

原因：GCC 认识 `memset` 这个**名字**（builtin 函数）。它看到你的循环，判断「这段可以换成更快的 `memset`」，于是生成了一次对 `memset` 的调用。而那个 `memset` 就是你正在编译的这个函数。**编译器不知道自己在给自己挖坑。**

修法是编译时加 `-fno-builtin`：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -fno-builtin ...
```

加了之后：

```
start
data ok
bss ok
```

**这个 bug 的教学价值在于：它每一步都「正常」。** 编译没警告、链接没报错、代码逻辑看起来完全正确、反汇编里也确实是「调用 memset」——只有跑起来什么都不发生。

排查这类问题的通用方法：**怀疑递归就看 `bl` 的目标地址**。除了尾调用优化，`bl` 的目标等于当前函数起始地址，基本就是无限递归。

## 底层机制五：代码可以小到什么程度

实测三种优化级别下的固件体积（`code/05_cortexm/`，只做 UART 输出与一个乘法循环）：

| 优化 | `.text` |
|---|---|
| `-O0` | 469 B |
| `-O2` | 289 B |
| `-Os` | 240 B |

`-Os` 比 `-O2` 再省 17%。在 FLASH 只有 256 KB 的芯片上，这种比例值得在意。

完整的 `startup_full`（含 `.data` 拷贝、`.bss` 清零、`memcpy`、`memset`）实测：

```
   text	   data	    bss	    dec	    hex	filename
    385	      4	      4	    393	    189	startup_full.elf
bin size: 389 bytes
```

**ELF 385 字节 text，bin 389 字节**（bin = text 385 + data 4，因为 `.data` 的初值也要烧进 FLASH）。注意这跟 Ch 18 里 641112 字节的 AArch64 静态二进制是同一个量级的程序——**差 1600 倍**。

**但 ELF 文件本身是 66632 字节。** 差在哪？

```
firmware.elf: 66632 B
firmware.bin: 289 B
```

`.elf` 里有段表、符号表（`nm` 能列出 `vectors`/`putc_`/`compute` 的名字，就是因为有它）、`.comment`（编译器版本号）、`.ARM.attributes`（ABI 属性）。**这些全是给调试器和工具链看的，芯片不需要。** `objcopy -O binary` 把「芯片真正要的那部分」剥出来，66632 → 289。

**烧录用的是 `.bin`，调试用的是 `.elf`。** 这两个文件不能混。用 `.bin` 调试时你没有任何符号信息，崩溃时只能看到一个地址。

## 实测：完整链路

`code/05_cortexm/build.sh` 的全部内容：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib \
  -Wl,-T,link.ld -Wl,--gc-sections \
  -O2 startup.c compute.c -o firmware.elf

arm-none-eabi-size firmware.elf
arm-none-eabi-objcopy -O binary firmware.elf firmware.bin

timeout 5 qemu-system-arm -M mps2-an385 -cpu cortex-m3 \
  -kernel firmware.elf -nographic -serial mon:stdio 2>&1 | head -5
```

实测输出：

```
   text	   data	    bss	    dec	    hex	filename
    289	      0	      0	    289	    121	firmware.elf
cortex-m3 alive
42
```

四个参数值得逐个说：

- **`-mcpu=cortex-m3 -mthumb`**：目标核心与指令集。Cortex-M 只支持 Thumb，`-mthumb` 是必须的（不加会报错或生成错误的代码）
- **`-nostdlib`**：不链接 `crt0.o` 和 libc。启动代码由我们自己提供
- **`-Wl,-T,link.ld`**：用自定义链接脚本。默认脚本会把代码放到 Linux 程序的默认地址，那在 MCU 上是错的
- **`-Wl,--gc-sections`**：回收未引用的段。配合 `KEEP(*(.vectors))` 使用

`-nographic -serial mon:stdio` 把 QEMU 的串口重定向到当前终端，`Ctrl-A C` 切换 monitor。板子型号 `mps2-an385` 对应 Cortex-M3。

`compute(6,7)` 返回 42，因为 `compute.c` 写的是「循环加 `a` 共 `b` 次」：

```c
int32_t compute(int32_t a, int32_t b) {
  int32_t acc = 0;
  for (int32_t i = 0; i < b; i++) acc += a;
  return acc;
}
```

`-O2` 把它优化成了：

```
000000fc <compute>:
  fc:	2900      	cmp	r1, #0
  fe:	bfcc      	ite	gt
 100:	4348      	mulgt	r0, r1
 102:	2000      	movle	r0, #0
 104:	4770      	bx	lr
```

**5 条指令，12 字节。** `ite gt`（if-then-else）是 Thumb-2 的条件执行结构，`mulgt` 只在 `b > 0` 时执行。循环被整个识别成乘法。

注意这里**没有**保存 `lr`。它是叶函数（不调用别人），所以 `lr` 保持「调用者的返回地址」，`bx lr` 直接返回。对比 Ch 18 里非叶函数的 `stp x29, x30`，逻辑是一样的：**只有调用别人的函数才需要保存返回地址**。

## 踩雷集锦

1. **向量表里放函数名而不加 `used` 属性。** 没有 `used` 也没有任何 C 代码引用这个数组，`--gc-sections` 会把它回收。现象是复位后立刻 HardFault 或跑飞。纠正：`__attribute__((section(".vectors"), used))` + 链接脚本 `KEEP(*(.vectors))`，两处都要。

2. **初始 SP 写成 RAM 起始地址。** 栈向下生长，起始值必须是顶端。实测值 `0x20010000` 对应 64 KB RAM 的顶端。写错就第一次 `push` 越界。

3. **忘了清 `.bss`，在 QEMU 上测试通过。** QEMU 启动时会清零 RAM，真机不会。这类 bug 只在真机上出现，或者只在「热复位」时出现（上一次运行的残留）。纠正：`.bss` 清零是硬性步骤，不能因为「测试通过了」就删掉。

4. **用 PL011 的寄存器布局访问 CMSDK APB UART。** 现象是静默卡死，没有异常、没有输出。纠正：查芯片/板子的参考手册确认 UART 型号，MPS2-AN385 是 CMSDK APB，基址 `0x40004000`，`STATE` 在 `0x04`。

5. **`-nostdlib` 后没提供 `memcpy`/`memset`。** 链接报 `undefined reference`。纠正：自己实现，并且编译加 `-fno-builtin`。

6. **自己写的 `memset` 无限递归。** 不加 `-fno-builtin` 时，GCC 会把你的循环改写成对自己的调用。现象是编译链接全通过、运行静默挂死。纠正：加 `-fno-builtin`；排查时看 `bl` 的目标地址是否等于函数自身起始地址。

7. **烧 `.elf` 而不是 `.bin`。** 有些烧录工具能吃 ELF（会自己提取），有些不会，会把整个 66 KB 包括符号表写进 FLASH，覆盖后面的数据。纠正：确认工具要哪种格式；`objcopy -O binary` 显式生成。

## 本章重点整理

- 裸机上没有调用者：复位时硬件从向量表第 1 项装 SP、第 2 项装 PC。`reset_handler` 绝不能 `return`。
- 向量表是指针数组，入口地址的最低位是 Thumb 标志（实测 `0x6d = 0x6c | 1`），不是地址本身。
- `.data` 的 VMA 在 RAM、LMA 在 FLASH（实测 `0x20000000` vs `0x181`），搬运必须由启动代码做；`.bss` 在文件里不占字节，必须显式清零。
- 串口寄存器布局因芯片而异。MPS2-AN385 是 CMSDK APB UART（`DR@0x00`、`STATE@0x04`、`CTRL@0x08`），不是 PL011；用错的表现是静默卡死。
- `-nostdlib` 不阻止编译器生成 `memcpy`/`memset` 调用；必须自己实现，且要加 `-fno-builtin`，否则会递归调用自己。
- 体积：最小固件 `.text` 289 B / bin 289 B；完整启动版 385 B / bin 389 B；而 ELF 是 66632 B，差在符号表和调试信息。
- 优化级别：`-O0` 469 B、`-O2` 289 B、`-Os` 240 B。MCU 上 `-Os` 通常值得。

## 自我检核

- [ ] 不看笔记，画出从上电到 `reset_handler` 第一条指令的完整过程，标出向量表两项各是什么
- [ ] 解释为什么入口地址是 `0x6d` 而符号地址是 `0x6c`，以及最低位为 0 会发生什么
- [ ] 说出 `.data` 与 `.bss` 的区别（各三点：初值、文件占位、启动时必须做什么）
- [ ] 你写了一段裸机代码，QEMU 跑起来没有任何输出也不报错——列出你会依次检查的三件事
- [ ] 解释 `-nostdlib` 之后为什么还会缺 `memcpy`，以及为什么自己写的 `memset` 会调用自己
- [ ] 一个 641112 字节的 AArch64 静态程序与一个 289 字节的 Cortex-M 固件，差的那 64 万字节是什么

## 延伸阅读

- **[ARMv7-M Architecture Reference Manual](https://developer.arm.com/documentation/ddi0403/latest/)** — Arm
  - **读哪里**：Chapter B1（Exception Model）的「Vector table」一节，以及 A2.3（Thumb 状态与指令集选择）
  - **学什么**：向量表的 16 项精确定义、异常优先级、以及「地址最低位 = Thumb 标志」的权威说明
  - **和本章的关联**：本章向量表那段与 `0x6d` 的来源，全部出自这两节
  - **前提**：能读汇编；不需要读完整个手册（一千多页），只读这两节就够

- **[CMSDK APB UART 技术参考手册](https://developer.arm.com/documentation/ddi0479/latest/)** — Arm（Cortex-M System Design Kit）
  - **读哪里**：APB UART 章节的寄存器映射表
  - **学什么**：`DR`/`STATE`/`CTRL` 三个寄存器的精确位定义，以及和 PL011 的差别
  - **和本章的关联**：本章 `putc_` 的三行代码就是这份寄存器表的直接翻译；踩雷第 4 条的权威依据
  - **前提**：知道什么是内存映射寄存器（`volatile` 指针解引用）

- **[GNU ld 手册 — Output Section LMA](https://sourceware.org/binutils/docs/ld/Output-Section-LMA.html)**
  - **读哪里**：整页（很短）；重点看 `AT` 与 `LOADADDR` 的说明
  - **学什么**：VMA 与 LMA 的区别，以及 `> RAM AT > FLASH` 这个写法到底在说什么
  - **和本章的关联**：本章 `.data` 那段的规范依据；你的链接脚本写错时，回来查这里最快
  - **前提**：读过本章的 `objdump -h` 输出，知道 VMA/LMA 在输出里的位置

- **[GCC 手册 — Options That Control Optimization](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html)**
  - **读哪里**：搜 `-fno-builtin`；顺带看 `-ffreestanding`（它隐含 `-fno-builtin`）
  - **学什么**：为什么编译器会替换你的循环、`-ffreestanding` 与 `-nostdlib` 的分工
  - **和本章的关联**：本章那个「`memset` 调用自己」的 bug，根因就在这个开关
  - **前提**：无；这条是查手册就能确认的事实，不需要额外背景

- **[QEMU 文档 — Arm System emulation](https://www.qemu.org/docs/master/system/target-arm.html)**
  - **读哪里**：`mps2-an385` / `mps2-an500` 两节
  - **学什么**：这块虚拟板子上有哪些外设、地址是多少、UART 是哪个型号
  - **和本章的关联**：本章的 `-M mps2-an385` 参数与 `0x40004000` 基址，出处在这里
  - **前提**：无；当参考手册用，不必通读

到这里，一个函数已经在三种芯片上跑起来了：AArch64（Ch 18）、Cortex-M3（本章）、RISC-V（下一章）。但 Ch 18 那个 memref 有 21 个参数，Cortex-M3 只有 4 个参数寄存器——**参数怎么传、栈怎么对齐，是一个独立的问题**。Ch 21 专门讲它。

→ [Ch 20 RISC-V 边缘后端](./20-riscv-backend.md)
