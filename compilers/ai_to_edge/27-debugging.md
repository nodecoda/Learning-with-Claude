# Ch 27 调试：从错误现象反推出错层

> **目标**：把「出错了怎么办」变成可执行流程。拿到任何现象——静默卡死、数值不对、链接报错、编译通过却没效果——先定位到**哪一层**（图 / MLIR / LLVM IR / 指令选择 / 链接 / 运行时），再决定查什么。本章 9 条诊断全部在本机复现，每条给出现象、层级、判定命令、根因。

> **环境**：LLVM/MLIR 14.0.0、torch 2.9.1+cpu、`arm-none-eabi-gcc` 10.3、`aarch64-linux-gnu-gcc` 11.4、`qemu-system-arm` 6.2（`mps2-an385` / `cortex-m3`）。

## 为什么需要这个？

前 26 章把链路拆成六层，每层单独看都清楚。但真实出错时，你手上只有一个现象：「跑起来没输出」「数字差了 1e-6」「报错说找不到符号」「改了优化级别，产物一模一样」。

**现象和原因之间隔着五层。** 凭直觉猜通常猜错，而猜错的代价是改错地方：把好的代码改坏，问题更复杂。

这一章给的是**反查路线**：从现象出发，用最小的命令确认它在哪一层，然后只在那层里找。价值不在于记住这 9 个 bug，而在于养成「先定位层级，再定位代码」的习惯。

## 先建立直觉

六层链路，每层有各自的症状类型：

```
① 图捕获      Dynamo / export      症状：图里少节点、graph break
② 图优化      融合 / 常量折叠       症状：结果对但慢，或数值差一点
③ MLIR 降级   linalg → llvm        症状：parse / convert / translate 报错
④ LLVM IR     opt / 分析           症状：优化没生效、IR 异常
⑤ 后端        isel / regalloc      症状：汇编不对、模式不对、类型不支持
⑥ 链接/运行   ld / 硬件            症状：符号缺失、卡死、体积暴涨
```

关键洞察：**每一层的报错都「指向它自己」**。MLIR 报「接口没注册」，其实是你的 pass 没走完；链接器报「找不到 `memcpy`」，其实是编译器把你的循环改成了库调用。**报错的位置几乎从不是出错的位置。**

先给速查表，后面逐条展开。

| # | 现象 | 层级 | 判定命令 |
|---|---|---|---|
| 1 | qemu 启动后什么都不输出 | ⑥ 外设寄存器 | `-d in_asm` 看 PC 是否停在小循环 |
| 2 | `func.func` 不认识 | ③ MLIR 版本 | `mlir-opt --version` |
| 3 | `missing LLVMTranslationDialectInterface` | ③ 降级链未走完 | `grep -c unrealized_conversion_cast` |
| 4 | `opt -O2` 后 IR 没变 | ④ 函数属性 | `grep optnone` |
| 5 | `llc -march` 产物不符预期 | ⑤ triple 与 features | 看 `.s` 里 `.code 16/32` |
| 6 | `undefined reference to '__aeabi_fmul'` | ⑥ 运行时库 | `arm-none-eabi-nm -u` |
| 7 | `__int128 is not supported` | ⑤ 目标能力 | 换架构对照 |
| 8 | 量化「成功」但精度没变 | ①② API 静默失效 | 查类型 / dtype / `_packed_params` |
| 9 | 静态链接体积暴涨 | ⑥ libc 静态链接 | `size` + 对比 `libc.a` |

## 底层机制

### 诊断 1：静默卡死——外设寄存器布局

**现象**：`qemu-system-arm` 启动后没有任何输出，也不报错，只能 `Ctrl-C`。

**真因**：MPS2-AN385 的 UART0 是 **CMSDK APB UART**，不是 PL011：

```
CMSDK APB UART @0x40004000: DR=0x00  STATE=0x04 (bit0=TX full)  CTRL=0x08 (bit0 TX en, bit1 RX en)
PL011         @0x40004000: DR=0x00  FR=0x18    (bit5=TXFF)      CR=0x30   (bit0 UARTEN, bit8 TXE)
```

两套布局只有 `DR` 碰巧相同。用 PL011 的 `CR@0x30` 使能，CMSDK 的 `CTRL@0x08` 永远是 0，TX 根本没开。

**我实测的四种组合**（同一份源码，只改 UART 访问方式）：

| 组合 | 是否使能 TX | 轮询哪里 | 结果 |
|---|---|---|---|
| A | 只写 PL011 `CR@0x30` | `FR@0x18` bit5 | 无输出，PC 跑到末尾死循环 |
| B | 只写 PL011 `CR@0x30` | `STATE@0x04` bit0 | **无输出，PC 永久停在轮询** |
| C | 写 CMSDK `CTRL@0x08=3` | `STATE@0x04` bit0 | `hello`（正常输出） |
| D | 写 CMSDK `CTRL@0x08=3` | `FR@0x18` bit5 | `hello`（QEMU 里 0x18 恒读 0） |

**这里要修正证据包的一条归因。** 证据包说「用 PL011 的 `FR@0x18` bit5 会永久等待」，但我实测 `0x18` 在 QEMU 里**恒读 0**：

```
before-enable: 0x18=0x00000000 0x04=0x00000000  |  after-enable: 0x18=0x00000000
```

所以 A 形态不是卡在轮询：它把字符串逐字符写进了没使能的 `DR`，输出被静默丢弃，然后正常走到死循环。**真正会永久等待的是 B**：位定义正确（`STATE` bit0 = TX full），但 TX 没使能，这一位恒为 1。

**判定命令**：`-d in_asm` 看 PC 是否停在两三条指令的小循环。

```bash
timeout 2 qemu-system-arm -M mps2-an385 -cpu cortex-m3 -kernel fw.elf \
  -nographic -serial mon:stdio -d in_asm 2>&1 | tail -20
```

B 形态实测尾部：

```
0x00000052:  6853       ldr	r3, [r2, #4]      ← 读 STATE
0x00000054:  07db       lsls	r3, r3, #31      ← 取 bit0
0x00000056:  d4fc       bmi.n	0x52            ← 回到读
```

`bmi.n 0x52` 指回自己上方两行，就是「等一个永不为 0 的条件」。A 形态的尾部则是 `b.n 0x62`——自己跳自己，说明**代码跑完了**。

> 判据：**PC 在动 = 数据被丢弃；PC 不动 = 在等条件**。「没输出」和「卡住」是两件事。

### 诊断 2：`func.func` 不认识——MLIR 版本

```
ff.mlir:1:1: error: custom op 'func.func' is unknown
func.func @add(%a: i32, %b: i32) -> i32 {
^
```

MLIR 14 的写法是 `func`（即 `builtin.func`），`func.func` 是 15+ 才有的。同一段内容改成 `func` 写，14 直接接受。判定 `mlir-opt --version` → `Ubuntu LLVM version 14.0.0`。

**普适教训**：MLIR 的方言语法在版本间反复搬家（`std` → `arith`/`math`、`func` → `func.func`、旗标去掉 `mlir-` 前缀）。**网上教程多数对不上你手上的版本。** 先 `--version`，再照版本查。

### 诊断 3：`missing LLVMTranslationDialectInterface`——降级链没走完

```
x.mlir:11:10: error: cannot be converted to LLVM IR: missing
  `LLVMTranslationDialectInterface` registration for dialect for op:
  builtin.unrealized_conversion_cast
```

真因不是「接口没注册」，而是降级链末尾漏了 `--reconcile-unrealized-casts`，残留 6 个 `builtin.unrealized_conversion_cast`：

```bash
mlir-opt matmul.mlir --convert-linalg-to-loops --lower-affine \
  --convert-scf-to-std --convert-std-to-llvm \
  --convert-arith-to-llvm --convert-memref-to-llvm > x.mlir
grep -c unrealized_conversion_cast x.mlir     # 6

mlir-opt matmul.mlir ...同上... --reconcile-unrealized-casts > y.mlir
grep -c unrealized_conversion_cast y.mlir     # 0
mlir-translate --mlir-to-llvmir y.mlir > y.ll && wc -l y.ll   # 152
```

**套路**：看报错提到的 op 名，`grep` 它。报错指向机制，`grep` 才指向原因。

### 诊断 4：`opt -O2` 后 IR 没变——`optnone` 属性

`-O0` 的 IR 带 `optnone`，`opt` 见到直接跳过这个函数。实测（`int f(int x){return x*x+3*x+7;}`）：

```bash
clang-14 -O0 -S -emit-llvm one.c -o one0.ll
grep -c optnone one0.ll            # 2   （注释行 + 属性组各一次）
opt-14 -passes=mem2reg -S one0.ll -o one1.ll
grep -c alloca one1.ll             # 1   ← 没变！

clang-14 -O0 -Xclang -disable-O0-optnone -S -emit-llvm one.c -o oneb.ll
grep -c optnone oneb.ll            # 0
opt-14 -passes=mem2reg -S oneb.ll -o oneb1.ll
grep -c alloca oneb1.ll            # 0   ← 生效了
```

`grep -c optnone` 会连注释行一起数（2 而不是 1），**看属性组那行更准**：`attributes #0 = { noinline nounwind optnone uwtable ... }`。

### 诊断 5：`llc -march` 产物不符预期——triple 与 features 的分工

实测三组，同一份 `add` 函数：

```
1) thumbv7m 的 IR + -march=arm                                  → .code 16, muls r0, r1, r0
2) aarch64 的 IR + -march=arm -mtriple=thumbv7m-none-eabi -mcpu=cortex-m3
                                                                 → .code 32, mul r2, r1, r0
3) thumbv7m 的 IR + -mtriple=x86_64-linux-gnu                    → LLVM ERROR: 64-bit code
                                                                    requested on a subtarget
                                                                    that doesn't support it!
```

**结论比「`-march` 覆盖 triple」更精确**：

- 选**架构**的是 `-march`（arm / aarch64 / x86-64）。
- 选 **ARM 还是 Thumb** 的是 IR 里的 `target-features`（`+thumb-mode`），不是 `-mtriple`。所以第 2 组给了 `thumbv7m` 的 triple，产物仍是 `.code 32`。
- 用错架构会直接崩溃（第 3 组），或者刷一堆 `'x86-64' is not a recognized processor for this target` 警告然后继续用错误的处理器。

**判定**：不看命令行，看产物。`grep '\.code' out.s`，以及 `.cpu` / `.eabi_attribute 6` 那几行。跨目标编译时用 `clang --target=<triple> -mcpu=<cpu>` 从头生成 IR，别把 x86 的 IR 丢给 ARM 的 `llc`。

### 诊断 6：`undefined reference to '__aeabi_fmul'`——运行时库缺失

```
ld: fmul.o: in function `scale': fmul.c:(.text+0x6): undefined reference to `__aeabi_fmul'
ld: fmul.o: in function `divmod_l': fmul.c:(.text+0xe): undefined reference to `__aeabi_ldivmod'
```

Cortex-M3 没有硬件浮点（`+soft-float`），`float * float` 降级成 `__aeabi_fmul` 调用；`int64_t / int64_t` 走 `__aeabi_ldivmod`。`-nostdlib` 把 `libgcc` 也排除了，这两个符号没人提供。

```bash
arm-none-eabi-nm -u fmul.o
#          U __aeabi_fmul
#          U __aeabi_ldivmod
```

修复是显式链接 `-lgcc`：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -O2 -nostdlib fmul.o compute.c \
  -Wl,-T,link.ld -lgcc -o ok.elf
arm-none-eabi-nm -u ok.elf        # 只剩 U reset_handler（我没提供启动文件）
```

同理 `memcpy` / `memset` 也会因循环识别被生成出来——`grep` 源码找不到，只能看 `nm -u` 或汇编。

### 诊断 7：`__int128 is not supported on this target`

```
i128.c:1:1: error: '__int128' is not supported on this target
    1 | __int128 big(__int128 a, __int128 b){ return a * b; }
      | ^~~~~~~~
```

实测 `-mcpu=cortex-m3`、`-mcpu=cortex-m4`、`-march=armv8-m.main` **三种都报同样的错**，说明这不是某个核的缺陷，而是这套 32 位目标描述共有的限制。同源代码给 aarch64 就能编译，生成 `umulh` + `mul` + `ret`。

**这类报错是好消息**：编译器明确说目标不支持，比静默算错好得多。修法是改算法（拆成两个 64 位）或换目标，不是加编译选项。

### 诊断 8：量化「成功」了，但精度一点没变

```python
import torch, torch.nn as nn
torch.manual_seed(0)
lin = nn.Linear(128, 64).eval()
x = torch.randn(4, 128)
with torch.no_grad():
    y = lin(x)
q = torch.ao.quantization.quantize_dynamic(lin, {nn.Linear}, dtype=torch.qint8).eval()
with torch.no_grad():
    yq = q(x)
print(type(q))                       # <class 'torch.nn.modules.linear.Linear'>
print(q.weight.dtype)                # torch.float32
print(hasattr(q, "_packed_params"))  # False
print(torch.equal(y, yq))            # True
print((y - yq).abs().max().item())   # 0.0
```

三项全不对，**而且没有任何报错**。切 `x86` / `fbgemm` / `qnnpack` / `onednn` 四个引擎，结果完全一样。课程里对应脚本是 `code/08_quant/dynq2.py`，可直接跑：

```bash
cd compilers/ai_to_edge/code/08_quant
/home/ubuntu/.venvs/learn/bin/python dynq2.py
```

**判据**：量化是否生效，查**数据结构**，不能看「跑通了没报错」。三项里任何一项不对，量化就没发生。`torch.ao.quantization` 在 2.9 已标 deprecated，官方迁移到 `torchao`。

### 诊断 9：静态链接体积暴涨

```bash
printf '#include <stdio.h>\nint main(void){puts("hi");return 0;}\n' > hello.c
aarch64-linux-gnu-gcc -O2 -static  hello.c -o hello_a64
aarch64-linux-gnu-gcc -O2          hello.c -o hello_a64_dyn
stat -c '%n %s' hello_a64 hello_a64_dyn
# hello_a64      641000      ← -static
# hello_a64_dyn    8872
aarch64-linux-gnu-size hello_a64
#    text    data     bss     dec     hex
#  513100   22520   22256  557876   88334
stat -c%s /usr/aarch64-linux-gnu/lib/libc.a    # 4939918
```

动态版只要 8872 B——**体积的 98.6% 是静态 libc**。课程里那个 `add_a64` 是 641112 B、`text` 512834 B，而 `add` 本身只有十几条指令。

**判定**：`size binary` 看段分布，`nm --size-sort` 看最大符号，和 `libc.a` 大小对一下。

## 可跑例子 1：复现静默卡死并用 `-d in_asm` 定位

把 UART 访问方式参数化，一个文件生成四种固件：

```c
/* uart.c —— 改 POLL_REG / POLL_BIT 与初始化那一行，得到四种组合 */
#include <stdint.h>
#define UART0_BASE 0x40004000u
#define DR    (*(volatile uint32_t *)(UART0_BASE + 0x00))
#define STATE (*(volatile uint32_t *)(UART0_BASE + 0x04))
#define CTRL  (*(volatile uint32_t *)(UART0_BASE + 0x08))
#define FR    (*(volatile uint32_t *)(UART0_BASE + 0x18))   /* PL011 写法 */

#define POLL_REG  STATE      /* 换成 FR 就是 PL011 的写法 */
#define POLL_BIT  0          /* 换成 5 就是 PL011 的写法 */

static void putc_(char c) { while (POLL_REG & (1u << POLL_BIT)) { } DR = (uint32_t)c; }
static void puts_(const char *s) { while (*s) putc_(*s++); }

void reset_handler(void);
void default_handler(void);
__attribute__((section(".vectors"), used))
const void *vectors[16] = {
    (void *)0x20010000, (void *)reset_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler,
};
void default_handler(void) { for (;;) { } }
void reset_handler(void) {
  CTRL = 0x3u;                  /* 改成写 0x30 的 PL011 CR，就复现 A/B 形态 */
  puts_("hello\n");
  for (;;) { }
}
```

编译并跑（`link.ld` 见 Ch 19）：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib -Wl,-T,link.ld \
  -Wl,--gc-sections -O2 uart.c -o uart.elf
timeout 3 qemu-system-arm -M mps2-an385 -cpu cortex-m3 -kernel uart.elf \
  -nographic -serial mon:stdio
```

四种组合的实测输出：

```
C) CTRL@0x08 + STATE bit0   → hello
D) CTRL@0x08 + FR bit5      → hello
A) CR@0x30   + FR bit5      → （空，PC 走到 b.n 0x62 自旋）
B) CR@0x30   + STATE bit0   → （空，PC 卡在 bmi.n 0x52）
```

只有 B 会给出「PC 停在小循环」的证据：

```bash
timeout 2 qemu-system-arm -M mps2-an385 -cpu cortex-m3 -kernel uart.elf \
  -nographic -serial mon:stdio -d in_asm -D t.log
tail -5 t.log
```

```
0x00000052:  6853       ldr	r3, [r2, #4]
0x00000054:  07db       lsls	r3, r3, #31
0x00000056:  d4fc       bmi.n	0x52
```

## 可跑例子 2：`-print-after=` 看后端每一步

```bash
printf 'int add(int a,int b){return a*b+1;}\n' > t.c
clang-14 -O2 -S -emit-llvm -target aarch64-linux-gnu t.c -o t.ll
llc-14 -march=aarch64 -print-after=finalize-isel t.ll -o /dev/null 2>&1 | head -14
```

实测：

```
# *** IR Dump After Finalize ISel and expand pseudo-instructions (finalize-isel) ***:
# Machine code for function add: IsSSA, TracksLiveness
Function Live Ins: $w0 in %0, $w1 in %1

bb.0 (%ir-block.2):
  liveins: $w0, $w1
  %1:gpr32 = COPY $w1
  %0:gpr32 = COPY $w0
  %2:gpr32common = nsw MADDWrrr %1:gpr32, %0:gpr32, $wzr
  %3:gpr32sp = nsw ADDWri killed %2:gpr32common, 1, 0
  $w0 = COPY %3:gpr32sp
  RET_ReallyLR implicit $w0
```

读法：`MADDWrrr` 是乘加的选择结果，第三个操作数是 `$wzr`，所以 `a*b+1` 被拆成 `a*b+0` 再 `+1`——AArch64 的 `madd` 没有「乘加立即数」形式。**想知道「这条指令是谁生成的」，就在 `finalize-isel` 处 dump，而不是猜。**

MLIR 侧对应旗标是 `--print-ir-after-all`（14 上**没有** `--mlir-` 前缀）。在 `code/02_mlir_lowering/` 下跑：

```bash
cd compilers/ai_to_edge/code/02_mlir_lowering
/usr/lib/llvm-14/bin/mlir-opt matmul.mlir --convert-linalg-to-loops \
  --print-ir-after-all 2>&1 | head -6
```

```
// -----// IR Dump After LinalgLowerToLoops //----- //
func @matmul(%arg0: memref<4x8xf32>, %arg1: memref<8x4xf32>, %arg2: memref<4x4xf32>) {
```

注意 14 上不认 `--mlir-print-ir-after-all`：

```
mlir-opt: Unknown command line argument '--mlir-print-ir-after-all'.
mlir-opt: Did you mean '--print-ir-after-all'?
```

## 四个通用方法

**方法 1：二分——找第一个错的产物。** 每层都留中间文件（`.graph` → `.mlir` → `.ll` → `.s` → `.o` → 运行输出），从上游往下比。第一个不对的产物就是出错的层：上游都对不用查，下游都是被它影响的也不用单独查。**别用管道把中间结果吃掉**，出问题时你手上就没证据了。

**方法 2：最小复现。** 把问题从大程序缩到一个函数、再缩到几行。本章 9 条里至少 6 条靠最小复现定位——`__int128` 缩成空函数，`optnone` 缩成三行 C。它不只是好查，更是**证明假设**：认为原因是 X，就构造只含 X 的程序，看它是否重现。

**方法 3：单变量对照。** 改 target 就只改 target，改优化级别就只改优化级别。诊断 4 的唯一变量是 `-disable-O0-optnone`，`alloca` 从 1 变 0，结论无歧义；诊断 1 的四种组合同样只改一个因素。

**方法 4：读中间产物，而不是读报错。** 报错是框架层写的，它不知道你的意图。链接报「找不到符号」就看 `nm -u`；`mlir-translate` 报「接口缺失」就 `grep` 那个 op；运行不输出就用 `-d in_asm` 看 PC。**中间产物不会骗你。**

## 踩雷集锦

1. **只看最后一行报错**
   错误直觉：最后一行就是根因。实测 `fmul.o` 一次报出 `__aeabi_fmul` 与 `__aeabi_ldivmod` 两个符号，只修一个仍然失败。纠正：从**第一条**错误开始读；`make 2>&1 | head -20`。

2. **相信「没报错就是成功」**
   错误直觉：返回 0 就是对的。实测 `quantize_dynamic` 零报错、类型仍是 `Linear`、`weight.dtype` 仍是 `float32`、`forward` 逐比特相同——完全没量化。纠正：**功能正确性靠输出比对，不靠退出码**。

3. **把「没输出」当成「卡住」**
   错误直觉：不打印就是死循环。实测两种形态都存在：B 形态 PC 真的停在 `bmi.n 0x52`；A 形态程序跑完了，只是写进了没使能的 `DR`。纠正：**先看 trace**。PC 在动说明数据被丢弃，PC 不动才是等条件。

4. **在 qemu 上调时序问题**
   错误直觉：能跑就能调。qemu 模拟语义不模拟时序：中断延迟、DMA 竞争、flash 等待周期、cache 一致性都不存在或以完全不同形式出现。纠正：时序问题只能用真机；qemu 只用于功能与寄存器验证。

5. **忘了工具版本差异**
   错误直觉：文档怎么写我就怎么写。实测 14 上 `func.func` 直接报未知 op，`--mlir-print-ir-after-all` 也不存在。纠正：报错先 `--version`，把版本写进问题描述，再用该版本的旗标。

## 本章重点整理

- 调试第一步是**定位层级**，不是改代码。六层的最小验证手段：图 → `.mlir` → `.ll` → `.s` → `.o` → 运行输出。
- 「静默不输出」多在外设/寄存器层（诊断 1）；「符号缺失」在链接层（诊断 6）；「改了没效果」在属性层（诊断 4）；「数值差一点」在重结合/向量化层。
- 报错信息常指向**机制**而非**原因**：`missing LLVMTranslationDialectInterface` 的真因是 6 个残留 `unrealized_conversion_cast`。顺 op 名 `grep`。
- `optnone` 让 `opt` 跳过整个函数：同一份 IR，`mem2reg` 前后 `alloca` 都是 1；去掉属性后变 0。
- `-march` 选架构，**Thumb/ARM 模式由 IR 的 `target-features` 决定**；aarch64 的 IR 加 `-march=arm -mtriple=thumbv7m` 仍产出 `.code 32`；架构不匹配会直接 `LLVM ERROR`。
- 裸机上 `float` 乘法与 64 位除法要 `libgcc`：`-nostdlib` 下不自动链接，报 `__aeabi_fmul` / `__aeabi_ldivmod`，加 `-lgcc` 解决。
- 「全绿但错」最危险：量化空操作、输出被静默丢弃，都通过编译链接、无警告。
- 静态链接的 641000 B 里 98.6% 是 libc（`libc.a` 单文件 4939918 B），动态版只要 8872 B。

## 自我检核

- [ ] 不看诊断表，说出「没输出」「符号缺失」「改了没效果」「量化没生效」四类现象各属哪一层，以及每层判定命令
- [ ] 你跑了 `opt -O2` 但 IR 没变，写出会执行的两条命令，并说明看哪一行才算数
- [ ] `mlir-translate` 报 `missing LLVMTranslationDialectInterface`，说出真实原因、判定命令与修法
- [ ] 给定一份 thumbv7m 的 IR 和一份 aarch64 的 IR，分别加 `-march=arm`，预测产物是 `.code 16` 还是 `.code 32`，并说明依据
- [ ] 一个裸机程序编译链接都通过、运行没有任何输出，列出你的前三步排查，并说明如何区分「卡住」与「输出被丢弃」
- [ ] 解释为什么「量化没生效」比「量化报错」更难发现，以及用什么方法能抓住它

## 延伸阅读

- **[LLVM — How to Debug](https://llvm.org/docs/HowToDebug.html)**
  - **读哪里**：`Debugging the LLVM IR` 与 `Debugging generated code` 两节
  - **学什么**：`-print-after`、`-debug-only`、`-verify-each` 的用法与副作用
  - **和本章关联**：可跑例子 2 的官方依据；诊断 4、5 的判定手段出自这里。前提是跑过 Ch 13 的 `opt`/`llc`

- **[MLIR — Debugging and Diagnostics](https://mlir.llvm.org/getting_started/Debugging/)**
  - **读哪里**：`--verify-each`、`--print-ir-after-all`、`--print-op-generic` 三段
  - **学什么**：多层 IR 怎么逐 pass 观察，`unrealized_conversion_cast` 的调试思路
  - **和本章关联**：诊断 2、3 的官方手段。注意 14 上旗标没有 `mlir-` 前缀。前提是写过一个能跑通的 `.mlir`

- **[GCC — Options That Control Optimization](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html)**
  - **读哪里**：搜 `-fno-builtin`、`-ffp-contract`、`-nostdlib` 附近的说明
  - **学什么**：哪些「优化」会改变可观测行为（递归、浮点收缩、循环识别成 libcall），怎么关掉
  - **和本章关联**：诊断 6 的 `-nostdlib`、诊断 7 的 `__int128` 支持范围，以及 Ch 25 的 `-ffast-math` 开关都在这里。前提是会用 `gcc -Q --help=optimizers` 查当前生效的开关。

- **[ARM — Cortex-M3 Technical Reference Manual](https://developer.arm.com/documentation/ddi0337/latest/)**
  - **读哪里**：`Debug` 一章中断点、观察点与 fault 状态寄存器（`CFSR`/`HFSR`/`BFAR`）几节
  - **学什么**：HardFault 时怎么从寄存器定位出错地址，怎么用 `BFAR`/`MMFAR` 找非法访问
  - **和本章关联**：诊断 1 的进阶版——从「PC 停在哪」到「为什么停」。前提是读过 Ch 19

- **[Brendan Gregg — The USE Method](https://www.brendangregg.com/usemethod.html)**
  - **读哪里**：整页（很短）
  - **学什么**：对每个资源检查「利用率、饱和度、错误」，把「凭感觉猜」换成「按清单查」
  - **和本章关联**：本章的六层表就是它在编译链路上的实例：每层问「这层有没有出错、有没有被跳过」，而不是从最后的报错倒着猜。前提是接受「先分类再深入」这种看起来慢、实际快的方法。

到这里技术内容走完了。下一章把五个工业框架放在一起对照，看它们各自怎么解决这条链路上的同一批问题。

→ [Ch 28 生产工具链对照：ExecuTorch / TVM / IREE / ORT / TFLite](./28-production-stacks.md)
