# 第 19 章 向量化（vectorization）与编译选项：让 MSVC 真正生成 SIMD

> **目标**：理解 `/O2`、`/fp:fast`、`/arch:AVX2` 和对齐各自控制什么；会用 `/Qvec-report`、`/FAs` 和反汇编验证 MSVC 是否把循环向量化，并能区分“代码能跑”和“代码真的用了 SIMD”。

> **环境**：Windows 10/11 x86_64；MSVC 19.43（`cl` 14.43，x64 Native Tools）；Python 3.11.12；numpy 2.4.6。命令在仓库根目录执行：

```powershell
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
$vc = "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
```

本章使用 [`code/mini_ai/codegen_c.py`](./code/mini_ai/codegen_c.py) 的 `compile_c` 和实际生成的 C。上一章是 [Ch 18 Matmul 与 tiling](./18-matmul-tiling.md)，下一章是 [Ch 20 Runtime ABI 与 DLL](./20-runtime-abi-and-dll.md)。

## 为什么需要这一层

C 代码里的 `for` 循环不等于标量指令。现代编译器会把连续的、无依赖的循环改写成 SIMD（Single Instruction, Multiple Data，单指令多数据）指令，一次处理 4 个或 8 个 float。这个变换叫自动向量化（auto-vectorization）。

问题在于：编译器不保证向量化。循环里有函数调用、可能的别名、无法证明的 trip count、非连续的访存、浮点语义限制，都会让它退回标量。你看到的源码是 `for`，生成的机器码可能仍然是一条一条 `addss`；性能差异可以到数倍。

这一章要建立一条验证链：

```text
源码循环
  -> 编译器报告：是否向量化，原因码
  -> 汇编：有没有 xmm/ymm 向量指令
  -> 正确性：向量化前后结果是否在容差内
  -> 计时：median 是否真的下降
```

只做其中一步都不够。报告说向量化，不代表没有正确性问题；汇编里有向量指令，不代表这条循环是热点；计时变快，也不代表差异超过噪声。

## 先建立直觉

标量加法和 SIMD 加法的区别：

```text
标量 SSE2（128-bit，4 个 float）
xmm0 = [a0, a1, a2, a3]
xmm1 = [b0, b1, b2, b3]
addps xmm0, xmm1
结果 = [a0+b0, a1+b1, a2+b2, a3+b3]

AVX2（256-bit，8 个 float）
ymm0 = [a0 .. a7]
ymm1 = [b0 .. b7]
vaddps ymm0, ymm0, ymm1
结果 = [a0+b0 .. a7+b7]
```

一次指令做 4 或 8 次浮点加。前提是数据在内存里连续，且循环的每次迭代互不依赖。

对齐（alignment）是另一个维度：

```text
地址 0x1000 是 64 字节对齐：
0x1000 + 0   +16  +32  +48
| float x16 | ... | ... | ... |

地址 0x1004 不是 16 字节对齐：
0x1004 + 0   +16 ...
| 跨缓存行/未对齐起始 | ... |
```

未对齐访问在 x86 上通常能执行，但可能跨缓存行、降低吞吐。编译器生成 `movups`（未对齐）还是 `movaps`（对齐）取决于它能否证明指针对齐。不能证明时，它选择安全的 `movups`。

## 核心内容

### 1. 当前 C 后端的编译命令

`compile_c` 实际写出的批处理是：

```bat
@echo off
call "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 2
cl /nologo /LD /O2 /fp:fast /Fe:kernel.dll kernel.c > build.log 2>&1
exit /b %errorlevel%
```

逐个看：

| 选项 | 作用 | 代价/边界 |
| --- | --- | --- |
| `/LD` | 生成 DLL，而不是 exe | 需要导出符号，否则 `ctypes` 找不到 |
| `/O2` | 最大化速度，启用内联、循环优化、自动向量化 | 编译更慢，调试信息可能被优化掉 |
| `/fp:fast` | 放宽浮点语义，允许重排、合并等变换 | 结果可能与严格 IEEE 不同，NaN/Inf 行为要重新验证 |
| `/Fe:name.dll` | 指定输出文件名 | 不改变代码语义 |
| 默认 x64 | 基线 SSE2，128-bit 向量 | 不会自动使用 AVX2 |

`/fp:fast` 不是“向量化开关”。它控制的是浮点运算能否重排、能否假设没有 NaN/Inf、能否把 `a*b+c` 合成 FMA 等。一个没有浮点依赖的加法循环，在 `/O2` 下就可能向量化；一个需要保持严格舍入顺序的归约，即使加了 `/fp:fast` 也未必向量化。把这两件事分开，调优时才不会乱加选项。

### 2. 对齐：arena 对齐不等于输入对齐

生成的 C 用 64 字节对齐分配 arena：

```c
const size_t arena_bytes = 8192;
#if defined(_MSC_VER)
float* arena = (float*)_aligned_malloc(arena_bytes, 64);
#else
float* arena = (float*)aligned_alloc(64, arena_bytes);
#endif
if (!arena) return 1;
```

内存规划器给出的 offset 都是 64 的倍数，所以 arena 内的每个临时 buffer 都按 64 字节对齐。`arena_bytes` 也被对齐到 64 的倍数，满足 `aligned_alloc` 对 size 的要求。

但输入和输出指针来自调用者：

```c
MINI_EXPORT int mini_forward(const float* x0, float* out0)
```

`x0` 和 `out0` 的对齐由 NumPy 或调用方决定，生成代码没有对 `x0` 做对齐假设。这是正确的保守选择：如果编译器假设了 64 字节对齐，而调用方传了一个 4 字节对齐的指针，程序可能直接崩溃。要做对齐优化，先在运行时检查 `((uintptr_t)x0 % 64) == 0`，再走特化路径，或者只对 arena 内部的数据使用对齐假设。

### 3. 自动向量化报告：先看真实结果

先建实验目录，再用 `generate_c` 生成一个 `(1024,)` 的 `add + relu` 图：

```powershell
New-Item -ItemType Directory -Force .scratch\mini-ai-ch19 | Out-Null
```

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.codegen_c import generate_c

g = Graph()
x = g.add_input("x", (1024,))
c = g.add_const("c", np.array([2.0], np.float32))
y = g.add_op("add", [x, c])
r = g.add_op("relu", [y])
g.add_output(r)
open(".scratch/mini-ai-ch19/ew.c", "w", encoding="utf-8").write(generate_c(g))
```

用当前 `cl` 加上报告和汇编输出：

```powershell
Push-Location .scratch\mini-ai-ch19
cmd /c "call `"$vc`" >nul && cl /nologo /LD /O2 /fp:fast /Qvec-report:2 /FAs /Fe:ew_vec.dll /Fo:ew_vec.obj ew.c"
Pop-Location
```

实际输出（节选）：

```text
ew.c(33) : info C5002: 由于“1203”，循环未向量化
ew.c(38) : info C5002: 由于“1200”，循环未向量化
```

两条循环都没有向量化。打开 `ew.asm`，relu 循环里能看到：

```asm
movss   xmm0, DWORD PTR [rdi-4096]
xorps   xmm1, xmm1
call    fmaxf
movss   DWORD PTR [rdi], xmm0
```

`fmaxf` 是一次函数调用，不是一条可向量化的 `maxps`。即使 `/fp:fast` 打开了，MSVC 也没有把这个调用内联成向量指令。add 循环同样被展开成标量 `movss`/`addss`。

作为对照，手写一个等价的、表达式更直接的 kernel：

```c
#include <stddef.h>
#define MINI_EXPORT __declspec(dllexport)

MINI_EXPORT int add_kernel(const float* x, float* y) {
    for (int i = 0; i < 1024; ++i) y[i] = x[i] + 2.0f;
    return 0;
}

MINI_EXPORT int relu_kernel(const float* x, float* y) {
    for (int i = 0; i < 1024; ++i) y[i] = x[i] > 0.0f ? x[i] : 0.0f;
    return 0;
}
```

用同样的 `/O2 /fp:fast /Qvec-report:2 /FAs` 编译：

```text
probe.c(4) : info C5001: 循环已向量化
probe.c(8) : info C5001: 循环已向量化
```

`C5001` 表示循环已向量化，`C5002` 表示未向量化并给出原因码。原因码的具体含义随 MSVC 版本变化，遇到不认识的码先查当前版本的文档，不要凭记忆解释。

### 4. 汇编验证：不要只信报告

`probe.asm` 里能看到 128-bit 的 SSE 指令：

```asm
movups  xmm0, XMMWORD PTR [rcx+rax-16]
movups  xmm1, XMMWORD PTR [rcx+rax]
maxps   xmm0, xmm2
maxps   xmm1, xmm2
movups  XMMWORD PTR [rax-80], xmm0
```

`xmm` 是 128-bit 寄存器，`maxps` 一次处理 4 个 float。默认 x64 基线是 SSE2，所以宽度是 4，不是 8。

加上 `/arch:AVX2` 再编译：

```powershell
cmd /c "call `"$vc`" >nul && cl /nologo /LD /O2 /fp:fast /arch:AVX2 /Qvec-report:2 /FAs /Fe:probe_avx2.dll /Fo:probe_avx2.obj probe.c"
```

实际汇编里出现：

```asm
vmovups ymm0, YMMWORD PTR [rcx+rax-32]
vmaxps  ymm1, ymm0, ymm2
vmovups ymm0, YMMWORD PTR [rcx+rax-128]
vmovups YMMWORD PTR [rax-160], ymm1
```

`ymm` 是 256-bit 寄存器，一次 8 个 float。`v` 前缀表示 VEX 编码，常见于 AVX/AVX2 指令。

这里有一个需要辨别的点：`/fp:fast` 在两组实验里都开着，但生成宽度从 `xmm` 变成 `ymm` 是靠 `/arch:AVX2`。不要看到“加了 fast 就变快”就把原因归给浮点选项；先看汇编里到底用了什么指令。

### 5. 验证链：正确性先于速度

向量化改变的是指令，不是语义目标。正确的验证顺序是：

1. **差分测试**：用解释器或 PyTorch 作为参考，比较 `max_abs_error`。当前 `run_c_graph` 和 `load_c_function` 已经做了 float32 转换和错误码检查。
2. **向量化报告**：确认目标循环是 `C5001`，不是 `C5002`。
3. **汇编检查**：确认热点循环里有 `addps`/`maxps`/`vaddps`/`vmaxps`，而不是 `addss`/`maxss` 或函数调用。
4. **计时**：用 `benchmark` 的 median，不用单次最小值；固定 warmup/repeat 和环境。
5. **可移植性**：确认目标 CPU 支持所用指令集；AVX2 不是所有机器都有。

一个真实的例子：`demo_e2e.py` 的 MLP 用 `/O2 /fp:fast` 编译后，`/Qvec-report:2` 给出：

```text
mlp.c(71) : info C5002: 由于“1203”，循环未向量化
mlp.c(69) : info C5002: 由于“1106”，循环未向量化
mlp.c(68) : info C5002: 由于“1106”，循环未向量化
mlp.c(84) : info C5002: 由于“1203”，循环未向量化
mlp.c(82) : info C5002: 由于“1106”，循环未向量化
mlp.c(81) : info C5002: 由于“1106”，循环未向量化
```

这个 MLP 的矩阵只有 `4x8`、`4x16`，trip count 很小，循环展开和标量执行可能比向量化更合适。报告说“没有向量化”不等于代码有问题；它只说明编译器判断这条循环不值得或无法向量化。性能判断仍然要靠计时。

## 底层机制

### 循环向量化的三个前提

编译器要把一个循环变成 SIMD，通常要证明：

1. **可计数**：循环次数在进入循环前可知，或者能用向量化余数循环处理。
2. **无跨迭代依赖**：第 `i` 次迭代不依赖第 `i-1` 次的结果。归约 `sum += x[i]` 有依赖，但编译器可以用多个部分和再合并，前提是浮点语义允许重排。
3. **访存可向量化**：连续、对齐（或不影响正确性的未对齐）、没有无法证明的别名。

当前生成的 elementwise 循环满足 1 和 3 的形式，但 relu 的 `fmaxf` 是一个函数调用，破坏了 2/3 的可见性；add 循环的指针来自 `_aligned_malloc` 和外部输入，编译器不能总证明它们不别名。于是它选择标量。

### 别名与 `restrict`

如果 `x` 和 `y` 可能指向同一块内存，编译器不能随意把 `y[i] = x[i] + c` 重排成向量写，因为写 `y` 可能改变后续要读的 `x`。C99 的 `restrict` 告诉编译器“这两个指针在函数生命周期内不重叠”：

```c
MINI_EXPORT int add_kernel(const float* restrict x, float* restrict y) {
    for (int i = 0; i < 1024; ++i) y[i] = x[i] + 2.0f;
    return 0;
}
```

`restrict` 是承诺，不是检查。调用方传了重叠指针，行为未定义。生成器只有在 ABI 契约明确“输入和输出不重叠”时才能加；当前 `mini_forward` 的输入和输出由调用方分别提供，通常不重叠，但生成器没有把这个契约写进 C。

### `fmaxf` 与 `maxps`

`fmaxf(a, b)` 的语义要处理 NaN、 signed zero 等边界，编译器不能随便把它换成一条 `maxps`。`maxps` 的 NaN 行为与 `fmaxf` 不完全一致。要让 relu 向量化，可以：

- 用 `x > 0 ? x : 0`，在 `/fp:fast` 下让编译器选择 `maxps`；
- 用 `_mm_max_ps` intrinsic，明确接受它的 NaN 语义；
- 保留 `fmaxf`，接受标量调用，或者在生成器里内联一个自定义的 `max` 宏。

三种方案的正确性边界不同，不能只看速度。当前后端选择 `fmaxf`，优先避免额外的分支和 intrinsic 依赖；要注意 `fmaxf(NaN, 0)` 返回 0，而 PyTorch 的 `relu(NaN)` 返回 NaN，NaN 边界并不一致。差分测试要单独覆盖 NaN/Inf，不能只用有限值输入。

### 对齐假设与 `__assume`

如果确定指针 64 字节对齐，可以用 MSVC 的 `__assume` 把条件写成优化器假设：

```c
#include <stdint.h>
__assume(((uintptr_t)ptr & 63) == 0);
```

`__assume` 是语句，不是转换；它告诉优化器“这个条件为真”，编译器不会在运行时检查。条件一旦不成立，行为未定义，可能崩溃或读到错误数据。它是否改变加载宽度或向量化决定，仍要看 `/Qvec-report` 和汇编。当前生成器只对 arena 内部的偏移有把握，对 `x0`/`out0` 没有，所以不使用这个假设。

### 指令集与运行时分发

`/arch:AVX2` 生成的 DLL 需要 CPU 支持 AVX2。把这样的 DLL 发到只支持 SSE2 的机器上，执行到 AVX2 指令会触发非法指令。生产环境常见做法是：

```text
运行时检查 CPU 特性
  -> 支持 AVX2：加载 avx2 版本
  -> 只支持 SSE2：加载 sse2 版本
```

本课的 DLL 是单版本，编译时选定指令集。教学环境固定，先接受这个限制；跨机器分发时要补运行时分发。

### 验证记录：三个对象的对比

把本章实际跑过的三个对象放在同一张表里，差别就很清楚：

| 对象 | 编译选项 | `/Qvec-report` | 汇编证据 | 结论 |
| --- | --- | --- | --- | --- |
| `ew.c`（生成器输出） | `/O2 /fp:fast` | `C5002`（1203、1200） | `movss`、`addss`、`call fmaxf` | 没有向量化 |
| `probe.c` 的 add | `/O2` | `C5001` | `movups`、`addps` | 128-bit SSE |
| `probe.c` 的 relu | `/O2` | `C5001` | `movups`、`maxps` | 128-bit SSE |
| `probe.c` 的 relu | `/O2 /arch:AVX2` | `C5001` | `vmovups`、`vmaxps`、`ymm` | 256-bit AVX2 |
| `mlp.c`（生成器输出） | `/O2 /fp:fast` | `C5002`（1203、1106） | 标量循环 | trip count 小，未向量化 |

这张表说明两件事。第一，生成器输出和手写 kernel 的向量化结果可以完全不同；第二，`/fp:fast` 不是决定项，`probe.c` 在只有 `/O2` 时也能得到 `C5001`。

复现表里第一行和最后一行时，先确认 `.asm` 文件和源文件同名。`cl /FAs` 默认把汇编写到源文件同目录、同基名；如果连续编译 `ew.c` 和 `probe.c`，应分别检查 `ew.asm` 和 `probe.asm`，不要拿错文件。

### 为什么 add 循环也可能被拒绝

`ew.c` 的 add 循环看起来是标准的连续读写：

```c
for (int i0_0 = 0; i0_0 < 1024; ++i0_0) {
    v_2_add_0[i0_0] = v_0_x[i0_0] + v_1_c[0];
}
```

它仍然没有得到 `C5001`。一个合理的方向是别名：`v_2_add_0` 指向 arena，`v_0_x` 指向调用者传入的 `x0`，编译器无法仅凭函数签名证明两者不重叠。如果它们重叠，向量化后的批量读写在语义上可能与逐元素执行不同。`restrict` 是把这个契约明确写给编译器的工具，但前提是调用方真的保证不重叠。

另一个方向是循环形态。`v_1_c[0]` 是一个静态常量数组的读取，编译器可能先做标量替换、再做展开，最后没有留下它认为值得向量化的循环。不同 MSVC 版本对这类小循环的决策可能不同，所以不要用“这个写法一定向量化”作为设计前提；把报告和汇编当作每次构建的检查项。

### 用 `dumpbin` 检查最终 DLL

`/FAs` 看的是编译过程中的汇编，`dumpbin /disasm` 看的是 DLL 里实际落地的代码：

```powershell
cmd /c "call `"$vc`" >nul && dumpbin /disasm .scratch\mini-ai-ch19\probe_avx2.dll" | Select-String -Pattern "vmaxps|vmovups|maxps|movups"
```

如果源码、汇编和 DLL 三处对不上，优先怀疑构建目录里混入了旧文件。`compile_c` 每次覆盖同名 `.c`、`.dll`，但手工实验会在同一个目录留下上一次的 `.obj` 或 `.asm`。验证时用干净的目录，或者把配置写进输出文件名。

把这三层证据固定成脚本，比记住某次编译结果可靠。下一节讨论这些选项之间的取舍，以及什么时候应该接受“没有向量化”。

## 进一步用法与变体

### 用 `/arch:AVX2` 做局部实验

对同一个 kernel 分别编译 SSE2 和 AVX2 版本，跑同一组输入，比较：

- `max_abs_error` 是否仍在容差内；
- `/Qvec-report` 是否从 `C5002` 变成 `C5001`；
- 汇编里是 `xmm` 还是 `ymm`；
- median 是否稳定下降。

如果 AVX2 版本只在某一次运行快，换一轮就反超，说明差异在噪声范围内。不要用一次运行给指令集下结论。

### 用 Clang/GCC 交叉验证

Clang 的 `-Rpass=loop-vectorize`、GCC 的 `-fopt-info-vec` 能给出类似的报告。交叉验证的价值是发现“某个编译器特有的标量化”：MSVC 没向量化，Clang 可能向量化；反过来也可能。比较时仍然要固定优化级别和目标架构。

### 用 intrinsic 替代自动向量化

如果自动向量化不听话，可以手写 `_mm256_add_ps`、`_mm256_max_ps`。代价是可移植性下降、代码量上升、尾部处理要自己写。intrinsic 适合已经确认的热点，不适合一开始就手写整个后端。

### 对齐分配与缓存行

`_aligned_malloc(arena_bytes, 64)` 让 arena 从 64 字节边界开始，正好覆盖一个缓存行。这样连续访问不会跨界。对多线程程序，64 字节对齐还能减少 false sharing；本课的 kernel 是单线程，收益主要来自对齐加载和缓存行利用率。

## 对比与取舍

| 选项/方案 | 收益 | 代价 | 何时使用 |
| --- | --- | --- | --- |
| `/O2` | 内联、展开、自动向量化 | 编译慢，调试难 | 发布构建的默认起点 |
| `/fp:fast` | 允许重排、合并、FMA | 结果可能变化，NaN/Inf 语义放宽 | 能接受容差的推理 kernel |
| `/arch:AVX2` | 256-bit，理论 2 倍宽度 | 目标 CPU 必须支持 | 固定硬件的部署 |
| `restrict` | 帮助别名分析 | 调用方必须保证不重叠 | ABI 契约明确时 |
| `__assume` 对齐假设 | 可能改善加载和向量化 | 假设错误会崩溃，效果依版本而定 | 指针对齐可证明时 |
| 手写 intrinsic | 完全控制指令 | 可移植性和维护成本 | 已确认的热点 |

本课的默认组合是 `/O2 /fp:fast`，不默认开 `/arch:AVX2`。原因是教学机器固定，但读者可能在更老的 CPU 上运行；先把验证链走通，再决定是否为某台机器开更宽的指令集。

## 踩雷集锦

1. **把 `/fp:fast` 当成向量化开关。** 它放宽浮点语义，不保证生成 SIMD。报告和汇编才是证据。
2. **只看 `C5001` 不看正确性。** 向量化后的归约顺序可能变化，`max_abs_error` 必须重新测；`/fp:fast` 下 NaN/Inf 的行为也要单独验证。
3. **对输入指针假设 64 字节对齐。** `x0`/`out0` 来自调用方，只有 arena 内部能保证对齐。错误的 `__assume` 会让程序在特定调用方式下崩溃。
4. **在 AVX2 机器上编译，到 SSE2 机器上运行。** DLL 里的指令集是编译时固定的，缺少运行时分发就会非法指令。
5. **用一次计时判断向量化收益。** 标量版本和向量版本的差异可能小于机器噪声；用 median、多轮重复和固定环境。

## 进阶

- 把生成的 relu 从 `fmaxf` 改成 ternary，重新编译，比较 `/Qvec-report` 和 `max_abs_error`。
- 给 `add_kernel` 加 `restrict`，观察 MSVC 是否改变向量化决定。
- 用 `dumpbin /disasm` 检查 DLL 的 `.text` 段，确认热点循环里的指令宽度。
- 写一个运行时 CPU 特性检查，在 AVX2 和 SSE2 两个 DLL 之间选择。
- 对 `64x64x64` matmul 开启 `/arch:AVX2`，比较 tiled 版本的 median 和 maxerr。

## 动手练习

1. 生成 `(1024,)` 的 add+relu C，用 `/Qvec-report:2` 编译，记录两条循环的报告。
2. 把 relu 改成 `x > 0 ? x : 0`，再编译一次，确认是否出现 `C5001`，并检查汇编里有没有 `maxps`。
3. 用 `/arch:AVX2` 编译同一个 kernel，确认汇编里出现 `ymm` 和 `vmaxps`。
4. 用 `benchmark` 比较 SSE2 和 AVX2 版本的 median，重复三轮，判断差异是否稳定。
5. 故意传一个未对齐的 NumPy 切片给 DLL，确认当前代码不会崩溃；再尝试加 `__assume` 对齐假设，观察风险和汇编变化。

## 本章重点

- `/O2` 启用优化和自动向量化；`/fp:fast` 放宽浮点语义；`/arch:AVX2` 决定指令宽度。三者不是同一个开关。
- `C5001` 表示已向量化，`C5002` 表示未向量化；原因码要结合当前 MSVC 文档解释。
- `xmm`/`maxps` 是 128-bit，`ymm`/`vmaxps` 是 256-bit；默认 x64 基线是 SSE2。
- arena 内部 64 字节对齐，输入/输出指针不假设对齐；`__assume` 的对齐条件必须有运行时保证。
- 验证顺序是正确性、报告、汇编、计时、可移植性，不能只看其中一项。

## 自我检核

- [ ] 我能解释 `/O2`、`/fp:fast`、`/arch:AVX2` 各自控制什么。
- [ ] 我能用 `/Qvec-report:2` 读出 `C5001`/`C5002`，并知道原因码要查文档。
- [ ] 我能从汇编里区分 `addss` 和 `addps`、`xmm` 和 `ymm`。
- [ ] 我能说明为什么 `fmaxf` 会阻止当前 relu 循环向量化。
- [ ] 我知道输入/输出指针不能默认按 64 字节对齐，`__assume` 的对齐假设风险在哪里。
- [ ] 我会用 median 和多轮重复验证向量化收益，而不是单次最小值。

## 延伸阅读

- **MSVC `/O2` 文档**：读哪里——`/O1`、`/O2`、`/Ox` 的差异；学什么——优化级别如何影响内联和循环优化；关联——本章编译命令的第一项。
- **MSVC `/fp:fast` 文档**：读哪里——浮点模型和 `/fp` 系列选项；学什么——重排、NaN/Inf、FMA 的语义边界；关联——为什么不能把 fast 当向量化开关。
- **MSVC 自动向量化与 `/Qvec-report` 文档**：读哪里——C5001/C5002 和原因码表；学什么——如何解读报告；关联——本章的真实编译输出。
- **Intel Intrinsics Guide**：读哪里——`_mm_add_ps`、`_mm256_max_ps`、load/store；学什么——SIMD 指令的语义和宽度；关联——手写 intrinsic 的进阶。
- **`code/mini_ai/codegen_c.py` 的 `compile_c`**：读哪里——实际批处理和选项；学什么——生成、编译、DLL 的完整链路；关联——练习 D 的构建步骤。

下一章：[Ch 20 Runtime ABI 与 DLL](./20-runtime-abi-and-dll.md)