# Final Project：把一个真实模型编译到 AArch64 与 Cortex-M 并跑通

> **目标**：不借任何推理框架，把一个 PyTorch 训练出来的 2 层 MLP 亲手变成两条可执行产物：AArch64 用户态静态二进制，和 Cortex-M3 裸机固件。两条路线共用同一份 C 前向、同一份权重、同一个输入，最后用同一个容差和同一套体积口径验收。做完这一题，前 29 章的每一层你都能指出它在哪一行发生。

> **环境**：Ubuntu 22.04 / x86_64、PyTorch 2.9.1+cpu（`/home/ubuntu/.venvs/learn/bin/python`）、`aarch64-linux-gnu-gcc` 11.4、`arm-none-eabi-gcc` 10.3、`qemu-aarch64-static` 6.2、`qemu-system-arm` 6.2（`mps2-an385`，Cortex-M3）、LLVM/Clang 14。下面所有终端输出都是本机实跑；Cortex-M3 的**真实周期数与功耗**未实测（QEMU 的 DWT 计数器返回 0），只报模拟器墙钟。

## 任务背景

Ch 1 说一个模型要经过七次变身才变成一条指令。前面每一章只让你看一次变身的一个切片：Ch 4 抓图、Ch 7 融合、Ch 10 降到循环、Ch 15 选指令、Ch 21 对 ABI、Ch 22 排内存、Ch 24 写运行时、Ch 25 对数值、Ch 26 量性能。切片看多了容易产生一种错觉——以为这些层是彼此独立的。

这一题把切片串回去。你会遇到的坑都是真实工程里的：

- `torch.export` 的图里 `linear` 是一个算子，到 C 里必须展开成两层循环，权重按 `[out][in]` 行主序摆放；
- Cortex-M3 没有 FPU，`float` 乘法变成 `bl __aeabi_fmul`，忘了 `-lgcc` 就链接失败；
- AArch64 不加 `-static`，QEMU 里连动态加载器都找不到；
- 权重转置写错，`max_abs` 涨到 0.85，但 `argmax` 仍是 2——只看分类结果会以为对了；
- 静态链接后 850 KB，同样功能的裸机固件 208 KB，差距不在你的代码，在 libc。

这些就是本课的收尾考试。题目不难，难在每一步都要有数字。

## 完整规格

### 模型

```
输入 x:  [1, 784]  float32
fc1:     Linear(784, 64) + ReLU
fc2:     Linear(64, 10)
输出:    [1, 10]   float32 logits
```

`64 × 784 = 50,176` 个权重 float 约 196 KB，足以让「权重放 Flash 还是 RAM」成为真问题；第二层只有 640 个权重，`hidden` 的 256 B 就是激活内存的主体。两个 Linear 加一个 ReLU 覆盖「矩阵向量乘 + 逐元素 + 融合边界」，单 batch 固定 shape 则对应 Ch 5 与 Ch 29 决策 1 的静态档。

### 两条路线

| | 路线 A | 路线 B |
| --- | --- | --- |
| 目标 | AArch64 Linux 用户态 | Cortex-M3 裸机 |
| 编译器 | `aarch64-linux-gnu-gcc` | `arm-none-eabi-gcc` |
| 链接 | `-static` | `-nostdlib -Wl,-T,link.ld` |
| 浮点 | 硬件 FP/NEON | 软件浮点（`__aeabi_*`） |
| 输出 | stdout 10 行 logits | UART 10 行定点小数 + `pred=` |
| 运行 | `qemu-aarch64-static` | `qemu-system-arm -M mps2-an385` |
| 内存 | 进程地址空间 | 64 KB RAM，权重留在 Flash |

### 验收标准

| 项目 | 上界 | 依据 |
| --- | --- | --- |
| 数值误差 `max_abs` | ≤ `1e-5` | 实测 `4.172e-07`，留 20 倍余量；Ch 25 的融合实验证明同序运算可以逐位相同，但跨编译器不保证 |
| 分类结果 | 与 PyTorch 一致 | 参考 `argmax = 2` |
| 路线 A 二进制 | ≤ 1 MB | 实测 850,224 B |
| 路线 B `text + data` | ≤ 256 KB（Flash 上限） | 实测 208,646 B |
| 路线 B `bss` | ≤ 1 KB | 实测 0 B；激活全在栈上 |
| 路线 B 单次推理 | ≤ 10 ms（QEMU 模拟时间） | 实测约 3.9 ms/次（斜率法），真实硬件需另测 |
| 路线 A 单次推理 | 只记录，不设上限 | QEMU 计时抖动大（443–918 µs），不能当硬件性能 |

`1e-5` 不是拍脑袋：参考 logits 量级在 0.6 左右，`1e-5` 对应约 1e-5 的相对误差；而 Ch 25 实测的重结合误差在 `9.5e-7` 量级，单次 matmul 的 float32 舍入在 `1e-7` 量级。取 `1e-5` 能容纳「正常的顺序差异」，又能抓住「权重布局写错」这类真错误（后者会到 `1e-1` 量级）。

## 目录结构

所有文件放在一个目录里，构建产物进 `out/`：

```
mlp-edge/
├── Makefile
├── export_model.py      # 阶段 1：导出图、权重、参考输出
├── gen_c.py             # 阶段 2：权重 → C 数组
├── model.h              # 阶段 3：共享接口与维度
├── model.c              # 阶段 3：手写前向
├── model_data.c         # 阶段 2 生成，不手改
├── host_main.c          # 阶段 4：路线 A 驱动
├── bench_a64.c          # 阶段 4：路线 A 计时
├── check.py             # 阶段 4：数值对拍
├── startup.c            # 阶段 5：Cortex-M 启动 + UART（来自 Ch 19）
├── m3_main.c            # 阶段 5：路线 B 驱动
├── link.ld              # 阶段 5：FLASH 256K / RAM 64K
└── out/
    ├── a64.txt
    └── bug.txt
```

`model.c` 与 `model_data.c` 被两条路线共用：**保证两条路线跑的是同一份数学**。各维护一份的话，数值不一致时你无法判断是后端问题还是实现问题。

## 分阶段实施

### 阶段 1：静态导出与参考输出

先固定 shape，再导出。不要先写 C，否则你会在调试 C 的时候同时怀疑 PyTorch。

```bash
mkdir -p mlp-edge/out && cd mlp-edge
python export_model.py
```

`export_model.py` 的核心只有四行：

```python
model = MLP().eval()
x = torch.randn(1, 784, dtype=torch.float32)
ep = torch.export.export(model, (x,))
ref = model(x)
```

实测输出（图部分，省略四个参数占位符）：

```
    %linear : [num_users=1] = call_function[target=torch.ops.aten.linear.default](args = (%x, %p_fc1_weight, %p_fc1_bias), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.ops.aten.relu.default](args = (%linear,), kwargs = {})
    %linear_1 : [num_users=1] = call_function[target=torch.ops.aten.linear.default](args = (%relu, %p_fc2_weight, %p_fc2_bias), kwargs = {})
```

三个算子、四个参数占位符、一个输入。`linear` 没在导出期拆成 `matmul + add`——它是 ATen 的复合算子，到 C 里必须自己拆。这正是 Ch 9「代码生成的三个出口」的分界点：PyTorch 有 `aten::linear` 的 kernel，你没有。

参考输出（实测）：

```
ref shape: (1, 10) dtype: torch.float32
ref logits: [0.1383427, -0.1545875, 0.5907449, 0.1624002, -0.527999, 0.0528441, 0.2443738, -0.1383042, -0.1396458, 0.2066318]
ref argmax: 2
```

把这两个数字记住：`argmax = 2`，最大 logit 约 `0.5907`。后面所有对拍都以它们为锚。

### 阶段 2：权重导出成 C 数组

`gen_c.py` 负责把 `.npz` 变成 `const float[]`（完整代码见参考实现）。三个细节决定成败：

1. **`reshape(-1)` 的顺序就是 PyTorch 的内存顺序**。`nn.Linear.weight` 形状是 `[out, in]`，行主序展开后第 `o * in + i` 个元素就是 `W[o][i]`。C 侧用 `&W1[o * IN_DIM]` 取行首，两者对齐。
2. **`float32` 而不是 Python float（float64）**。如果用默认精度生成，C 里的 `const float` 会二次舍入，误差虽然小，但会让「逐位相同」永远不成立。
3. **`%.9e` 足够**：float32 需要 9 位有效十进制才能无损往返，6 位会在 `0.138342678` 这种值上引入 `1e-8` 级偏差。

实测输出：

```
weights: 50890 floats = 203560 bytes
input:   784 floats = 3136 bytes
rodata:  206696 bytes
```

`50,890 = 64×784 + 64 + 10×64 + 10`。这 206,696 B 就是路线 B 固件体积的主体，也是 Ch 22 说的「权重是静态的，应该进 Flash 而不是 RAM」。

### 阶段 3：把算子降到循环

`model.h` 定义接口，`model.c` 是唯一一份前向：

```c
void mlp_forward(const float *x, float *logits) {
  float hidden[HID_DIM];

  for (int o = 0; o < HID_DIM; ++o) {
    float acc = B1[o];
    const float *row = &W1[o * IN_DIM];
    for (int i = 0; i < IN_DIM; ++i) acc += row[i] * x[i];
    hidden[o] = acc > 0.0f ? acc : 0.0f;
  }

  for (int o = 0; o < OUT_DIM; ++o) {
    float acc = B2[o];
    const float *row = &W2[o * HID_DIM];
    for (int i = 0; i < HID_DIM; ++i) acc += row[i] * hidden[i];
    logits[o] = acc;
  }
}
```

对照 Ch 10 的 `linalg.matmul → scf.for`：这里的两层循环就是 `scf.for` 的 C 等价物。ReLU 被融进了第一层的写回（`acc > 0 ? acc : 0`），这正是 Ch 7 的 elementwise 融合，只是这里由人手写而不是 pass 完成。

`hidden[64]` 在栈上，256 B。这对应 Ch 22：shape 静态、大小已知、生命周期只在本函数内，栈是最合适的分配器。写成 `malloc` 的话路线 B 立刻失败——裸机没有堆。

**ABI 与内存预算**（Ch 21、Ch 22）。`mlp_forward` 只有两个参数：`const float *x` 和 `float *logits`，按 AAPCS64 走 `x0`/`x1`，按 AAPCS32 走 `r0`/`r1`，两边都不涉及栈传参。参数顺序固定为「先输入、后输出」，调用方不能交换。本课两条路线都是 C，不需要 `extern "C"`；如果驱动用 C++ 写，头文件里必须写 `extern "C" void mlp_forward(const float *, float *);`，否则 C++ 会对函数名做 name mangling，链接时报未定义符号——这是 Ch 21 讲的「同一份声明，两种语言，两个符号」。

RAM 预算：`hidden` 256 B + `logits` 40 B + 保存寄存器与对齐，实测 `sub sp, #268`。MPS2-AN385 给 64 KB RAM（`0x20000000`–`0x20010000`），栈从 `0x20010000` 向下长，268 B 只占 0.4%。**够用，但前提是权重留在 Flash**：206,696 B 的常量如果被拷到 RAM，64 KB 直接放不下。

Ch 15 的指令选择在这里也能看到：x86 上 clang 把内层循环向量化成 `<4 x float>` 的 `load`/`fadd`，而 Cortex-M3 上没有 NEON，同样的源码降到标量 `muls` 加 `bl __aeabi_fmul`。**同一份 C，两个后端，两种机器码**——这就是 Ch 14 到 Ch 19 的全部内容。

### 阶段 4：路线 A，AArch64 用户态

编译、运行、对拍三步：

```bash
aarch64-linux-gnu-gcc -O2 -Wall -Wextra -static -I. \
  host_main.c model.c model_data.c -o mlp_a64
qemu-aarch64-static ./mlp_a64 > out/a64.txt
python check.py out/a64.txt
```

实测输出：

```
max_abs   = 4.172e-07
max_rel   = 4.018e-06
bitwise   = False
argmax    = 2 (ref 2)
pass@1e-5 = True
```

注意 `bitwise = False`。这不是 bug，是 Ch 25 的结论在跨编译器场景下的再现：PyTorch 的 `aten::linear` 调用的是 MKL/oneDNN 的分块 kernel，累加顺序和你的顺序循环不同；AArch64 上 GCC 又可能把内层循环向量化。**顺序不同，舍入就不同**，但差异在 `4e-7` 量级，远小于 `1e-5` 的验收线。

体积（实测）：

```
   text	   data	    bss	    dec	    hex	filename
 719874	  22560	  22256	 764690	   bab12	mlp_a64
```

文件本身 850,224 B。`text` 里有 719,874 B，而你的 `mlp_forward` 只有几百字节——剩下的是静态 libc 的 `printf`、启动代码、以及 206,696 B 的权重（`.rodata` 被并进 `text`）。作为对照，不加 `-static` 的版本只有 218,200 B，但 QEMU 直接报错：

```
qemu-aarch64-static: Could not open '/lib/ld-linux-aarch64.so.1': No such file or directory
```

这正是 Ch 17 的结论：静态链接在边缘设备上是必需，代价是 4 倍体积（850,224 / 218,200 ≈ 3.9）。

计时（实测，`bench_a64`，20,000 次）：

```
per_call_us=458.62
```

同一份代码在 x86 原生上跑是 45–48 µs。QEMU 的 AArch64 计时在多次运行间抖动很大（本机测到 443–918 µs），因为它是动态二进制翻译，不是硬件。**这个数字只能用来确认「能跑、量级合理」，不能写进性能报告**——Ch 26 已经讲过，测量必须在目标硬件上做。

### 阶段 5：路线 B，Cortex-M3 裸机

裸机需要三样东西：启动代码、链接脚本、一个能输出数字的驱动。`startup.c` 与 `link.ld` 直接来自 Ch 19，只改了最后调用的函数：

```c
void reset_handler(void) {
  uart_init();
  puts_("cortex-m3 alive\n");
  model_main();
  for (;;) { }
}
```

`m3_main.c` 里没有 `printf`。浮点格式化在裸机上要么依赖 `newlib` 的 `printf`（体积大、还要 `_sbrk`），要么自己写。这里选自己写：

```c
static void put_fixed6(float v) {
  if (v < 0.0f) { putc_('-'); v = -v; }
  int32_t scaled = (int32_t)(v * 1000000.0f + 0.5f);
  int32_t ip = scaled / 1000000;
  int32_t fp = scaled % 1000000;
  ...
}
```

`scaled` 的算法是「先放大 1e6 再转整数」，四舍五入靠 `+ 0.5f`。它在 logits 量级（±1）上安全；如果值超过约 2147（`INT32_MAX / 1e6`），`(int32_t)` 会溢出。这是本函数的前置条件，不是通用实现。

编译命令：

```bash
arm-none-eabi-gcc -mcpu=cortex-m3 -mthumb -nostdlib \
  -Wl,-T,link.ld -Wl,--gc-sections -O2 -I. \
  startup.c model.c model_data.c m3_main.c -lgcc -o firmware.elf
```

`-lgcc` 不能省。Cortex-M3 没有 FPU，`float` 的乘加会降到 `__aeabi_fmul` / `__aeabi_fadd`，它们由 libgcc 提供。漏掉就是：

```
undefined reference to `__aeabi_fmul'
undefined reference to `__aeabi_fadd'
undefined reference to `__aeabi_fcmpgt'
```

运行：

```bash
timeout 10 qemu-system-arm -M mps2-an385 -cpu cortex-m3 \
  -kernel firmware.elf -nographic -serial mon:stdio | head -14
```

实测输出：

```
cortex-m3 alive
mlp on cortex-m3
0.138343
-0.154588
0.590745
0.162400
-0.527999
0.052844
0.244374
-0.138304
-0.139646
0.206632
pred=2
```

十条 logits 与 PyTorch 参考在 6 位小数上一致，`pred=2`。最后那行 `terminating on signal 15` 是 `for(;;)` 被超时杀掉，不是固件错误。

体积（实测）：

```
   text	   data	    bss	    dec	    hex	filename
 208646	      0	      0	 208646	  32f06	firmware.elf
```

`data = 0`、`bss = 0`：启动代码不用拷 `.data`、不用清 `.bss`，所有常量在 Flash、所有变量在栈上。`firmware.bin` 同样是 208,646 B。

符号表能直接看出体积花在哪（`nm -S --size-sort` 的尾部）：

```
0000041c 00000164 T __aeabi_fsub
0000041c 00000164 T __subsf3
000002ac 00000168 W __aeabi_fmul
000002ac 00000168 W __mulsf3
00000414 0000016c T __aeabi_frsub
000013ec 00000a00 T W2
00000784 00000c40 T INPUT
00001eec 00031000 T W1
```

`W1` 占 `0x31000 = 200,704 B`，`INPUT` 占 `3,136 B`，`W2` 占 `2,560 B`，合计 206,400 B，加上 `B1/B2` 的 296 B，正好是 206,696 B。代码加软浮点库不到 2 KB。**208,646 B 里 99% 是权重**——这个比例决定了优化方向：压缩权重（量化）的收益远大于优化循环。

反汇编确认软浮点路径：`ae: b0c3 sub sp, #268`、`2e: f7ff fffe bl __aeabi_fmul`、`36: f7ff fffe bl __aeabi_fadd`。`sub sp, #268` 就是 `hidden[64]`（256 B）加 12 B 对齐/保存区；栈指针初值 `0x20010000`，64 KB RAM 里用掉 268 B。

计时用「多次推理取斜率」。QEMU 的 DWT 周期计数器返回 0（实测），不能用 `DWT_CYCCNT`；改用主机侧墙钟比较 `REPS=1/21/101` 的总耗时：`29 / 106 / 415 ms`。斜率 `(415 - 29) / 100 ≈ 3.86 ms` 每次推理，其中约 25 ms 是 QEMU 启动与 UART 初始化的固定开销。这仍是模拟时间，真实 Cortex-M3 会完全不同——但「约 4 ms 量级」足以说明 5 万次乘加在软件浮点下并不轻松。

### 阶段 6：量化再跑一遍（加分主线）

fp32 固件 208,646 B，其中 99% 是权重。按 Ch 29 决策 3，W8A8 应该把权重压到 1/4。做法：

1. 权重 per-channel 量化：每行一个 scale，`q = round(w / s)`，`s = max|w_row| / 127`；
2. 输入 per-tensor 量化：一个全局 scale；
3. 第一层 `int8 × int8 → int32` 累加，再乘 `S1[o] * sx` 回到 float，加 bias、ReLU；
4. 第二层把 float 的 `hidden` 重新量化到 int8（模拟真实 pipeline 的 requantize），再同样累加；
5. 权重用 `int8_t[]`，scale 用 `float[]`。

本机实测（fp32 与 int8 固件；int8 的 NumPy 对照见下）：

```
int8 权重：50,816 B（fp32 的 1/4），scale 296 B
Cortex-M3 固件：text 54,686 B
W8A8 固件：max_abs 5.47e-03，max_rel 2.22e-02，argmax 2（与参考一致）
纯 NumPy 模拟（不 requantize hidden）：max_abs 4.996e-03
只量化权重（激活 fp32）：max_abs 1.85e-3
```

固件从 208,646 B 降到 54,686 B，约 3.8×。int8 层内是 32 位整数乘加，内层循环不再调软浮点；但反量化与 `put_fixed6` 仍是浮点，`nm` 里还有 18 个 `__aeabi_*` 符号（fp32 版是 17 个，int8 版多出的一个是 `__aeabi_fdiv`，来自 `sh = hmax / 127.0f`）。**量化没有消灭浮点，只是把它从内层循环赶到了外层**——想彻底去掉，就得把 requantize 改成定点乘加。精度从 `4.17e-07` 掉到 `5.47e-03`，仍远小于 logits 间距（最大 0.59，次大 0.24，间距 0.35），所以 top-1 不变。这就是「量化收益取决于任务裕度」的具体样子：分类任务的裕度大，回归任务可能就没有这个空间。

注意 `max_abs 5.47e-3` 已经超过 `1e-5` 的验收线；NumPy 模拟值 `4.996e-3` 更小，因为它省掉了 hidden 的 requantize——**模拟与固件必须分别报数**，这是 Ch 25 的老规矩。**加分路线要单独定验收标准**：top-1 一致 + `max_rel < 5%`，而不是沿用 fp32 的容差。

### 阶段 7：全量自检与记录

```bash
make clean && make all
make run-a64        # 必须 pass@1e-5 = True
make run-m3         # 必须出现 pred=2
make size           # 记录 text/data/bss
qemu-aarch64-static ./bench_a64 | head -1
```

`make clean` 会删掉 `model_data.c` 和 `.npz`，强制从导出重跑。这是故意的：**生成链路只要有一环不可复现，数字就不可信**。

## 参考实现

下面是关键文件的完整内容。`model_data.c` 由脚本生成，不在此列出。

<details>
<summary>model.h</summary>

```c
#ifndef MODEL_H
#define MODEL_H

#define IN_DIM 784
#define HID_DIM 64
#define OUT_DIM 10

extern const float W1[HID_DIM * IN_DIM];
extern const float B1[HID_DIM];
extern const float W2[OUT_DIM * HID_DIM];
extern const float B2[OUT_DIM];
extern const float INPUT[IN_DIM];

void mlp_forward(const float *x, float *logits);

#endif
```

</details>

<details>
<summary>model.c</summary>

```c
#include "model.h"

/* 权重布局与 PyTorch 的 Linear.weight 一致：W[out][in]，行主序。
 * hidden 放在栈上：64 * 4 = 256 B，不需要堆。 */
void mlp_forward(const float *x, float *logits) {
  float hidden[HID_DIM];

  for (int o = 0; o < HID_DIM; ++o) {
    float acc = B1[o];
    const float *row = &W1[o * IN_DIM];
    for (int i = 0; i < IN_DIM; ++i) acc += row[i] * x[i];
    hidden[o] = acc > 0.0f ? acc : 0.0f;
  }

  for (int o = 0; o < OUT_DIM; ++o) {
    float acc = B2[o];
    const float *row = &W2[o * HID_DIM];
    for (int i = 0; i < HID_DIM; ++i) acc += row[i] * hidden[i];
    logits[o] = acc;
  }
}
```

</details>

<details>
<summary>export_model.py</summary>

```python
import numpy as np
import torch
from torch import nn


class MLP(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1, self.fc2 = nn.Linear(784, 64), nn.Linear(64, 10)

    def forward(self, x):
        return self.fc2(torch.relu(self.fc1(x)))


torch.manual_seed(0)
model = MLP().eval()
x = torch.randn(1, 784)
print(torch.export.export(model, (x,)).graph)
ref = model(x)
np.savez("weights.npz",
         w1=model.fc1.weight.detach().numpy(), b1=model.fc1.bias.detach().numpy(),
         w2=model.fc2.weight.detach().numpy(), b2=model.fc2.bias.detach().numpy())
np.save("input.npy", x.numpy())
np.save("ref_logits.npy", ref.detach().numpy())
print("ref argmax:", int(ref.argmax(dim=1).item()))
```

</details>

<details>
<summary>gen_c.py</summary>

```python
import numpy as np

def emit(name, arr):
    flat = np.asarray(arr, dtype=np.float32).reshape(-1)
    body = [", ".join(f"{v:.9e}f" for v in flat[i:i + 6]) for i in range(0, flat.size, 6)]
    return f"const float {name}[{flat.size}] = {{\n  " + ",\n  ".join(body) + ",\n};"

d = np.load("weights.npz")
x = np.load("input.npy").reshape(-1)
arrays = [("W1", d["w1"]), ("B1", d["b1"]), ("W2", d["w2"]), ("B2", d["b2"]), ("INPUT", x)]
with open("model_data.c", "w") as f:
    f.write('#include "model.h"\n\n' + "\n\n".join(emit(n, a) for n, a in arrays) + "\n")
n_w = sum(a.size for _, a in arrays[:-1])
print(f"weights: {n_w} floats = {n_w * 4} bytes")
print(f"rodata:  {(n_w + x.size) * 4} bytes")
```

</details>

<details>
<summary>host_main.c 与 check.py</summary>

```c
#include <stdio.h>
#include "model.h"

int main(void) {
  float logits[OUT_DIM];
  mlp_forward(INPUT, logits);
  for (int i = 0; i < OUT_DIM; ++i) printf("%.9g\n", logits[i]);
  return 0;
}
```

```python
import sys
import numpy as np

ref = np.load("ref_logits.npy").reshape(-1).astype(np.float32)
got = np.loadtxt(sys.argv[1], dtype=np.float64).astype(np.float32)
diff = np.abs(got - ref)
print(f"max_abs   = {diff.max():.3e}")
print(f"max_rel   = {(diff / np.maximum(np.abs(ref), 1e-12)).max():.3e}")
print(f"bitwise   = {np.array_equal(got, ref)}")
print(f"argmax    = {int(got.argmax())} (ref {int(ref.argmax())})")
print(f"pass@1e-5 = {bool(diff.max() <= 1e-5)}")
```

</details>

<details>
<summary>startup.c（与 Ch 19 相同，仅改调用目标）</summary>

```c
extern void model_main(void);

void reset_handler(void) {
  uart_init();
  puts_("cortex-m3 alive\n");
  model_main();
  for (;;) { }
}
```

向量表、UART 寄存器（CMSDK APB UART @ `0x40004000`）与 `default_handler`
与 Ch 19 的 `code/05_cortexm/startup.c` 一致；把 `compute(6, 7)` 换成
`model_main()` 即可。`putc_`/`puts_` 需去掉 `static`，供 `m3_main.c` 调用。

</details>

<details>
<summary>link.ld</summary>

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

</details>

## 验收与自测清单

按顺序打勾。任何一项不通过，先修它再往下走。

**阶段 1–2：导出与权重**

- [ ] 图里恰好是 `linear → relu → linear`，`ref_logits.npy` 形状 `(1, 10)`、dtype `float32`
- [ ] `argmax` 记录为 2
- [ ] `gen_c.py` 打印 `weights: 50890 floats = 203560 bytes`，`W1` 长度 50,176

**阶段 3：前向**

- [ ] x86 上能编译，输出与参考 `max_abs` 在 `1e-6` 量级
- [ ] 源码里没有 `malloc`，`hidden` 在栈上

**阶段 4：AArch64**

- [ ] `-static` 编译成功，`qemu-aarch64-static` 能直接跑
- [ ] `pass@1e-5 = True`、`argmax = 2`，且 `max_abs < 1e-6`（不是 1e-1）
- [ ] 二进制 ≤ 1 MB，记录 `size` 三项

**阶段 5：Cortex-M**

- [ ] 链接带 `-lgcc`，UART 输出 `cortex-m3 alive`、10 行 logits、`pred=2`
- [ ] `data = 0`、`bss = 0`，`text + data ≤ 256 KB`
- [ ] `nm -S --size-sort` 里 `W1` 约 200 KB，确认权重在 Flash

**阶段 6：量化（加分）**

- [ ] int8 固件 ≤ 80 KB，`argmax` 一致，`max_rel < 5%`

## 卡点提示

### 卡点 1：`Guard failed: x.size()[0] == 1`

你在 batch=1 上静态导出，然后喂了 batch≠1 的输入。这是 Ch 5 的 guard 在起作用，不是 bug。要么改回 batch=1，要么用 `dynamic_shapes={"x": {0: Dim("batch")}}` 重新导出——但注意本课路线 B 是按 batch=1 手写的，动态 batch 需要重写内存规划。

### 卡点 2：数值差了 0.8，但分类还是对的

大概率是权重布局。检查两件事：

- C 侧是不是 `W1[o * IN_DIM + i]`，而不是 `W1[o + i * HID_DIM]`；
- `gen_c.py` 是不是对 `model.fc1.weight`（形状 `[64, 784]`）直接 `reshape(-1)`，而不是先转置。

本课实测的错误版本（把行首寻址写成列寻址）得到：

```
max_abs   = 8.455e-01
argmax    = 2 (ref 2)
```

**top-1 正确不能证明实现正确。** 验收必须看 `max_abs`，不能只看 `pred=`。

### 卡点 3：`undefined reference to '__aeabi_fmul'`

Cortex-M3 没有 FPU，浮点运算由 libgcc 的软件实现提供。在链接命令末尾加 `-lgcc`。如果加了还报错，检查是不是把 `-lgcc` 放在了 `-nostdlib` 前面——链接器按顺序解析，库必须在使用它的目标文件之后。

### 卡点 4：QEMU 里没有任何输出

三个常见原因，按概率排序：

1. UART 基址写错。MPS2-AN385 用的是 CMSDK APB UART（`0x40004000`），不是 PL011。用 PL011 的 `FR@0x18` 判断会永久等待。
2. 向量表被 `--gc-sections` 删了。链接脚本里必须有 `KEEP(*(.vectors))`。
3. 栈指针初值越过 RAM。`0x20010000` 是 64 KB RAM 的顶端，写大了会一进来就 fault。

### 卡点 5：AArch64 二进制在 QEMU 里报找不到 `ld-linux-aarch64.so.1`

忘了 `-static`。边缘设备通常没有目标架构的动态加载器，静态链接是默认选择。

### 卡点 6：改了 `model.c` 但固件没变

`make` 依赖没写全，或你跑了旧的 `firmware.elf`。先 `make clean` 再 `make all`。

## 加分挑战

### 挑战 1：量化到 int8 并跑通

要求：权重 per-channel、激活 per-tensor、int32 累加、requantize 后接第二层。验收：固件 ≤ 80 KB，`max_rel < 5%`，`argmax` 不变。本课参考：`54,686 B`、`max_abs 5.47e-03`、`max_rel 2.22e-02`。进一步把 requantize 改成定点乘加（`(acc * M) >> S`），省掉每次推理的浮点除法，但要重新测舍入误差。

### 挑战 2：加第二个 hidden layer

改成 `784 → 64 → 32 → 10`。检查三件事：新增权重 `(32×64 + 32 + 320 + 10) × 4 ≈ 9.6 KB`；激活从 256 B 变成 384 B，栈仍够；`max_abs` 是否随层数累积变大。

### 挑战 3：换 RISC-V

本机有 `riscv64-linux-gnu-gcc` 与 `qemu-riscv64-static`：

```bash
riscv64-linux-gnu-gcc -O2 -static -I. host_main.c model.c model_data.c -o mlp_rv
qemu-riscv64-static ./mlp_rv
```

本机实测：RISC-V 版与 AArch64 版输出**逐位相同**（`max_abs = 0.0`），对 PyTorch 参考仍是 `4.172e-07`；静态二进制 `text 601,951 B`、`data 22,128 B`、`bss 22,184 B`。若换到别的编译器版本后差异突然变大，先查它是否把归约顺序改了。

### 挑战 4：把 `-ffast-math` 当开关来测

本课实测：`-O2` 生成标量 `fmadd` 循环（68 条指令），`-O3 -ffast-math` 生成 4 lane `fmla` 向量循环加水平归约（104 条指令）。QEMU 计时噪声（443–918 µs）盖过差异，不能拿来下结论。要求：在真实 AArch64 硬件上量两者，并对 100 组随机输入比较 `max_abs` 分布，给出「是否值得开」的结论。

## 全课回顾：30 章如何拼成这一题

| 章节 | 在这一题里的位置 |
| --- | --- |
| Ch 0 环境搭建 | 阶段 1 之前：确认 `torch`、两个 GCC、两个 QEMU 可用 |
| Ch 1 全景 | 整题的骨架：导出 → 降级 → 代码生成 → 链接 → 运行 |
| Ch 2 边缘约束 | 决定路线 B 的存在：无 OS、无 FPU、64 KB RAM |
| Ch 3 三种表示 | 阶段 1：`nn.Module` 与 `ExportedProgram` 的差别 |
| Ch 4 图捕获 | 阶段 1：`torch.export` 抓出三个算子的图 |
| Ch 5 动态 shape | 阶段 1：为什么固定 batch=1；卡点 1 的报错来源 |
| Ch 6 Inductor | 阶段 3 的对照：Inductor 生成 Triton/C++ kernel，这里手写 C |
| Ch 7 图优化 | 阶段 3：ReLU 融进第一层写回 |
| Ch 8 调度 | 阶段 3：内层循环的顺序；为什么没有 tiling（形状太小） |
| Ch 9 代码生成三出口 | 阶段 3 的选择：不调库、不生成 IR，直接手写 |
| Ch 10 MLIR 降级 | 阶段 3：`linalg.matmul → scf.for` 的 C 等价物 |
| Ch 11 量化 | 阶段 6：W8A8、per-channel scale、requantize |
| Ch 12 MLIR → LLVM IR | 阶段 3/4：`model.ll` 里的 `<4 x float>`、`fmul` 与 `fadd` |
| Ch 13 LLVM 优化管线 | 阶段 4：`-O2` 的 `fmadd` 循环 vs 挑战 4 的 4 lane `fmla` |
| Ch 14 Triple/DataLayout | 阶段 4/5：`aarch64-unknown-linux-gnu` vs `thumbv7m` |
| Ch 15 合法化与指令选择 | 阶段 5：`float` 乘加降到 `__aeabi_fmul` |
| Ch 16 寄存器分配与栈帧 | 阶段 5：`sub sp, #268` 的来源 |
| Ch 17 汇编/目标文件/重定位 | 阶段 4：静态链接 850 KB；`ld-linux` 缺失 |
| Ch 18 AArch64 后端 | 阶段 4 全部 |
| Ch 19 Cortex-M 裸机 | 阶段 5：向量表、UART、链接脚本 |
| Ch 20 RISC-V 后端 | 挑战 3 |
| Ch 21 ABI 与调用约定 | 阶段 3：`mlp_forward(const float*, float*)` 两个指针参数 |
| Ch 22 内存规划 | 阶段 3/5：`hidden` 在栈上，`.bss = 0` |
| Ch 23 算子库 vs 代码生成 | 阶段 3 的决策：3 个算子、形状固定 → 手写 |
| Ch 24 最小运行时 | 阶段 5：没有 libc，只有 UART 与 `for(;;)` |
| Ch 25 数值正确性 | 阶段 4：`max_abs 4.17e-07`，`bitwise = False` |
| Ch 26 性能测量 | 阶段 4/5：QEMU 计时抖动、斜率法、不能当硬件数据 |
| Ch 27 调试 | 卡点 1–6：从现象反推出错层 |
| Ch 28 生产工具链 | 整题的「如果换 ExecuTorch/TVM/IREE 会怎样」对照 |
| Ch 29 取舍与决策表 | 阶段 1/3/6 的六个决策：静态、保守融合、W8A8、手写、Cortex-M、裸机 |

练习 A–D 分别对应：抓图（阶段 1）、融合 pass（阶段 3）、读指令来历（阶段 4/5）、换后端（挑战 3）。

## 延伸阅读

### 官方文档

- **[PyTorch `torch.export`](https://docs.pytorch.org/docs/stable/export.html)** — 读 `Export IR` 与 `Constraints`；对应阶段 1 的图形态与 guard 行为。
- **[Arm Runtime ABI (`__aeabi_*`)](https://github.com/ARM-software/abi-aa/blob/main/rtabi32/rtabi32.rst)** — 读 `Floating-point helper functions`；`__aeabi_fmul` 的语义是阶段 5 链接错误的根源。
- **[GNU ld 链接脚本](https://sourceware.org/binutils/docs/ld/Scripts.html)** — 读 `MEMORY`、`SECTIONS`、`AT`；`link.ld` 每一行的依据。

### 源码依据

- **[libgcc ARM 软浮点](https://github.com/gcc-mirror/gcc/tree/master/libgcc/config/arm)** — 读 `ieee754-sf.S` 类实现；理解一次 `float` 乘法为何要几十个周期。
- **[PyTorch `aten::linear`](https://github.com/pytorch/pytorch/blob/main/aten/src/ATen/native/Linear.cpp)** — 读 `linear` 的分发与 `addmm` 路径；解释为什么 PyTorch 与手写循环的累加顺序不同。

### 论文

- **[Quantization and Training of Neural Networks for Efficient Integer-Arithmetic-Only Inference](https://arxiv.org/abs/1712.05877)** — Jacob et al., CVPR 2018；读第 3 节量化方案与第 4 节实验，对应阶段 6 的 requantize。
- **[TVM: An Automated End-to-End Optimizing Compiler for Deep Learning](https://www.usenix.org/conference/osdi18/presentation/chen)** — Chen et al., OSDI 2018；第 4 节比较手写 kernel 与调度生成，对应阶段 3 的决策。

做完这一题，你已经走完了 Ch 1 画的那条七次变身的链。下一步不是再学一个框架，而是把你自己的模型换进来：改 `IN_DIM`/`HID_DIM`，重跑阶段 1–7，看哪一步先崩。崩在哪，就是你的下一个学习目标。

← [Ch 29 取舍与决策表](./29-tradeoffs.md) ｜ [返回课程地图](./README.md)
