# Ch 9 — 代码生成的三条出口

> **目标**：看清 `torch.compile` 最终把图交给谁：C++ 后端给 CPU，Triton 后端给 GPU，以及为什么没有「直接生成汇编」这条路。学完这章，你能从缓存目录里的一堆产物说清每个文件是谁写的、谁编译的，以及为什么边缘部署必须关心 `.so` 的链接方式。

> **环境**：torch 2.9.1+cpu、Python 3.10、x86_64（AVX2）、Ubuntu 22.04。Triton 与 GPU 路径未在本机运行，相关描述标注为「按设计如此」。MLIR 路线的对照引用 Ch 12–13，本章不展开。

## 为什么需要这个？

到 Ch 8 为止，我们一直在「编译器内部」看问题：图怎么改、循环怎么排。这一章换个视角——**编译器把成果交给谁**。

这个问题看起来只是实现细节，其实决定了三件工程事实：

1. 你在产物里能看到什么（可调试性）
2. 换一个 CPU 架构要重做什么（可移植性）
3. 边缘设备上能不能跑（链接方式、运行时依赖）

一个常见的误解是「AI 编译器自己生成机器码」。实际不是。Inductor 生成的是 **C++ 源码**，然后调用系统上的 `g++` / `clang` 编译它。这个分工不是偷懒，是整个设计里最关键的取舍之一。

## 先建立直觉

把 AI 编译器想成一个**写菜谱的人**，传统编译器是**厨师**。

```
AI 编译器（Inductor）              传统编译器（g++ / clang）

  「先切块，再按 8 个一组下锅，    「用什么刀、火开到几档、
    每组的顺序是……」                先切还是先热锅……」
        │                                  │
        └────── 菜谱（C++ 源码）──────────▶┘
                                            │
                                            ▼
                                       成品（机器码）
```

写菜谱的人不必知道灶台的具体型号，知道「这是中式灶还是电磁炉」（目标特性）就够了。换成另一个厨房，菜谱大部分还能用。这就是可移植性的来源。

```
        ┌──────────────────────────────────────────┐
        │  FX Graph（算子 + 形状 + 布局）            │
        └───────────────────┬──────────────────────┘
                            │  GraphLowering + 调度
                            ▼
        ┌──────────────────────────────────────────┐
        │  中间表示：循环 + 内存访问 + 向量化意图     │
        └───────┬───────────────────────┬──────────┘
                │                       │
         CPU 路径│                GPU 路径│
                ▼                       ▼
        ┌───────────────┐       ┌───────────────┐
        │ C++ 源码       │       │ Triton 源码    │
        │ + Vectorized<> │       │ (DSL)         │
        └───────┬───────┘       └───────┬───────┘
                │                       │
                ▼                       ▼
        ┌───────────────┐       ┌───────────────┐
        │ g++ / clang    │       │ Triton 编译器  │
        │ → .so          │       │ → PTX → cubin │
        └───────────────┘       └───────────────┘
```

**注意两个出口的共同点**：都停在「某种源码或中间语言」，都不直接产机器码。差别只在交给谁做最后一步。

## 底层机制：缓存目录里的产物

Ch 6 把这里的东西分成四类；下表从「谁写的」角度重看一遍，`fxgraph/` 与 `aotautograd/` 分两行列出（合起来算第四类「图级产物」）。实测的目录内容：

```
cache/pb/cpb3b42cykxxfduf2aumx24dihcbubk37akgstq3mcewkae5jnha.so
cache/pb/cpb3b42cykxxfduf2aumx24dihcbubk37akgstq3mcewkae5jnha.cpp
cache/aotautograd/aq6carhhz.../kmk3577yvv4rzz...
cache/4l/c4l3uhy3bsqllwexjmcgxgun2x3f2fiu5a27tc3yxmtdx7l745en.main.so
cache/4l/c4l3uhy3bsqllwexjmcgxgun2x3f2fiu5a27tc3yxmtdx7l745en.main.cpp
cache/6b/c6bxg5rhhtkposbdqtc2ewy6mjjkyszvbo2772vhmrfbfooxtg4g.py
cache/fxgraph/...
```

按「生产者」分类：

| 产物 | 谁写的 | 内容 | 你能从里面看出什么 |
|---|---|---|---|
| `fxgraph/` | Dynamo | 序列化后的 FX Graph | 图断在哪里、算子是什么 |
| `aotautograd/` | AOTAutograd | 前向/反向图、分区 | 训练时反向图长什么样 |
| `*.py` | Inductor 调度 + 包装层 | Python 封装 + 内嵌 C++ 字符串 | 调度决策、buffer 分配、guard |
| `*.cpp` | Inductor C++ 代码生成 | 纯 kernel 源码 | 循环结构、SIMD 抽象、地址计算 |
| `*.so` | g++ / clang | 动态库 | 最终机器码，被 `ctypes` 加载 |

`.py` 里同时有调度和 codegen 两层信息，这是它最有用也最容易被误读的地方。看这段实测产物：

```python
cpp_fused_add_relu_0 = async_compile.cpp_pybinding(['float*'], '''
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C"  void  kernel(float* in_out_ptr0)
{
    ...
}
''')
```

这是 Ch 6 那个原地 kernel 的形式：签名表里只有一个 `float*`，读写同址。三输入的版本（来自 `code/06_elemwise/fused_3in.py`）多三个只读指针：

```python
cpp_fused_add_mul_relu_sub_0 = async_compile.cpp_pybinding(
    ['const float*', 'const float*', 'const float*', 'float*'], '''
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C"  void  kernel(const float* in_ptr0,
                       const float* in_ptr1,
                       const float* in_ptr2,
                       float* out_ptr0)
{
    ...
}
''')
```

`async_compile.cpp_pybinding` 这一行做了三件事：

1. 登记「用 C++ 后端编译这段字符串」
2. 声明 Python 侧调用签名（`['float*']` 是一个指针，三输入版本是四个）
3. 异步提交编译任务（注意函数名里的 `async`）

所以 `.py` 文件**不是**可以独立运行的 kernel，它是「编译任务清单 + 调用封装」。真正的计算在 `.cpp` 和 `.so` 里。

### 一次完整链路

```
① 图改写（Ch 7）
      ↓
② 调度：循环、tile、向量宽度（Ch 8）
      ↓
③ 代码生成：把循环写成 C++ 字符串，用 Vectorized<float> 表达 SIMD
      ↓
④ async_compile 把字符串交给 g++/clang，带上一组编译选项（优化级别、目标特性、include 路径）
      ↓
⑤ 产出 .so，Python 用 ctypes.CDLL 加载，直接调用 kernel
```

第 ⑤ 步的产物实测为：

```
cache/4l/....main.so: ELF 64-bit LSB shared object, x86-64, ... dynamically linked
```

三个词都要读：

- `ELF 64-bit`：目标格式是 Linux 64 位
- `x86-64`：目标架构，说明这份产物**不能**拿到 ARM 上跑
- `dynamically linked`：动态链接。它依赖系统上的 libc / libstdc++ / libtorch 符号

第三条对边缘部署是硬伤，后面单独说。

## 实测：`Vectorized<float>` 到底是什么

生成代码里反复出现：

```cpp
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + x0, 8);
auto tmp3 = tmp0 + tmp2;
tmp3.store(out_ptr0 + x0);
```

`at::vec::Vectorized<float>` 是 PyTorch 的 SIMD 抽象层，定义在 `c10/util/vec` 一带。它提供：

| 成员 | 作用 |
|---|---|
| `loadu(ptr, n)` | 从**非对齐**地址一次读 n 个元素 |
| `store(ptr, n)` | 写 n 个元素 |
| `operator+ - * /` | 逐元素运算，映射到对应 SIMD 指令 |
| `clamp_min` | 映射到 `max` 类指令（`relu` 就是这么来的） |

关键点：**这些是 C++ 重载运算符，不是内建函数**。`tmp0 + tmp2` 看起来像标量加法，实际编译出来是 `vaddps`。抽象是靠 C++ 类型系统做的，不是靠编译器识别特定模式。

为什么用 `loadu` 而不是 `load`？`u` 是 unaligned。Inductor 无法保证输入指针一定 32 字节对齐（Python 侧传来的 tensor 只保证到元素类型对齐），所以一律用非对齐版本。代价是：在老的 x86 上非对齐访问可能慢，在现代 CPU 上跨 cache line 时才有代价。

### `cpp_prefix.h` 是必需的吗

生成代码第一行：

```cpp
#include <torch/csrc/inductor/cpp_prefix.h>
```

这个头把 `at::vec`、`C10_LIKELY` 等符号带进来。**它意味着生成的 `.cpp` 不是独立自包含的**——编译它需要 PyTorch 的头文件路径。这也解释了为什么交叉编译到嵌入式目标很麻烦：你得把整套头文件带过去，还得有一个能编 C++17 的交叉编译器。

## 为什么没有「直接生成汇编」

这是本章的核心问题。三条理由，按重要性排。

### 理由一：指令选择的复杂度不成比例

从循环到机器码要做的：指令选择、寄存器分配、指令调度、窥孔优化、栈帧布局。Ch 15–17 会逐个讲这些。这些工作 Inductor 一件都不想做，因为它们**和模型无关**——一个 `add` 在 x86 上的最优指令序列，和模型是 ResNet 还是 Transformer 没有关系。

```
AI 编译器的价值：知道「这是 matmul + bias + relu，可以按 tile 组织」
传统编译器的价值：知道「这个循环在 AVX2 上该用什么指令、寄存器怎么分」
```

两边都在做优化，但优化的对象不同。一个团队重写一遍 LLVM 的后端，得到的是一份更差的后端。

### 理由二：可移植性

生成 C++ 后，换架构换的只是编译器：

```
同一份 .cpp ──[g++ -march=x86-64-v3]──▶ x86 机器码
            ──[aarch64-linux-gnu-g++ ]──▶ ARM 机器码
            ──[arm-none-eabi-g++      ]──▶ Cortex-M 机器码
```

一份源码，三个目标。如果 Inductor 直接生成 x86 汇编，上面这三行就要写三套代码生成器。

> 注意「同一份源码」是有条件的：`Vectorized<float>` 在 ARM 上会映射到 NEON，宽度 4 而不是 8；`cpp_prefix.h` 在裸机上不可用。所以更准确的说法是**同一套代码生成逻辑**，而不是同一份字节码。这个区别在 Ch 19（Cortex-M 裸机）会变成实际工作量。

### 理由三：可调试性

`.cpp` 是可以读的（Ch 6–8 我们一直在读）。汇编不行——至少不能快速读。

实际调试场景：

```
现象：编译后结果错了
路径 A（有 C++ 源码）：读循环边界 → 发现少算了最后 3 个元素 → 改断言
路径 B（只有汇编）：   objdump → 找循环 → 对寄存器编号 → 猜哪里差了一次迭代
```

路径 A 是读循环、加断言、重跑；路径 B 要先定位循环边界在指令流里的位置，再对着寄存器编号猜哪里少算了一次迭代。前者的反馈回路是「改一行、看输出」，后者是「改一处、反汇编、再猜」。`TORCH_LOGS=output_code` 这个开关存在的理由就在这里。

## 边界：把这份 `.cpp` 拿去交叉编译

正常路径是「本机 g++ 编本机」。边缘部署走的是另一条：把 `.cpp` 拿到另一个架构上编。这一步会暴露三处依赖，值得逐条自己验一遍。

### 依赖一：`-march` 决定了 `Vectorized` 的宽度

```bash
# 在 x86 机器上，对生成出来的 .cpp 直接指定 ARM 目标
aarch64-linux-gnu-g++ -O3 -march=armv8-a -c <kernel>.cpp -o kernel.o
```

预期行为：如果 `Vectorized<float>` 的实现靠 `__ARM_NEON` 这类宏切换，那么编出来的 `loadu(..., 8)` 在 128 位 NEON 上会被拆成两条 4 元素操作，或者直接编译失败（取决于那段代码是否硬编码了宽度假设）。**产物里的步长字面量 8 不会自动变成 4**——它是 Inductor 在调度阶段写死的，交叉编译改变不了。

这条是很多「移植失败」的根因：代码能编过，但 SIMD 利用率只有一半，或者更糟——越界访问。自己不验证就部署，问题会出现在板子上而不是编译机上。

### 依赖二：`cpp_prefix.h` 需要整棵 PyTorch 头文件树

```cpp
#include <torch/csrc/inductor/cpp_prefix.h>
```

这个头往下会拉进 `c10`、`ATen` 的一大片头文件。交叉编译时要提供 `-I` 指向这些头，而它们里面还可能有平台相关的代码（`#ifdef _WIN32`、`__aarch64__` 之类的分支）。在裸机工具链（`arm-none-eabi-g++`）上，标准库头都未必齐全。

**这就是 Ch 19 要自己写 `Vectorized` 替代品的原因**：不是不想复用，是那套头在裸机上编不过。

### 依赖三：`.so` 的动态链接在目标机上未必成立

```bash
# 在本机看一份 .so 依赖了哪些共享库
ldd <kernel>.so
```

预期输出是一串 `libtorch.so`、`libstdc++.so.6`、`libc.so.6` 之类的名字。在开发机上这些都在；在一台只装了 busybox 的嵌入式 Linux 上，可能一个都找不到，报错形如：

```
error while loading shared libraries: libtorch.so: cannot open shared object file
```

在裸机上连动态加载器都没有，`dlopen` 不存在，这类错误会提前到链接阶段变成 `undefined reference`。三条出路：

| 出路 | 做法 | 代价 |
|---|---|---|
| 静态链接 | `-static`，或链接 `.a` | 体积变大，且 libc 静态链接有已知限制 |
| 只带必需符号 | 裁掉 libtorch 依赖，自己实现 `Vectorized` 与 `clamp_min` | 工作量转移给你 |
| 不用 Inductor 产物 | 换 MLIR 路线直接降到目标架构（Ch 10 起） | 学习成本，但控制力最强 |

这个三选一，其实就是本课后半段的路线图。

## 对照：MLIR 路线走的是另一条路

Inductor 停在 C++，MLIR 路线会继续往下走，一直到 LLVM IR。两条路线的对照：

```
Inductor（本课 Ch 6–9）                 MLIR（本课 Ch 10–13）
────────────────────────                 ──────────────────────
FX Graph                                高层 dialect（linalg 等）
    ↓                                        ↓
循环 + 向量化意图（调度）                 linalg → loops
    ↓                                        ↓
C++ 源码（Vectorized<>）                 LLVM dialect
    ↓                                        ↓
g++ / clang                              llc / llvm backend
    ↓                                        ↓
机器码                                   机器码
```

关键差别在**中间停在哪**：

| 维度 | Inductor（C++ 出口） | MLIR（LLVM 出口） |
|---|---|---|
| 交给谁来优化 | 系统 C++ 编译器 | LLVM 自己的优化管线 |
| 能控制到多细 | 循环与向量化意图 | 到 LLVM IR 的每条指令 |
| 边缘后端支持 | 依赖 GCC/Clang 的 target 支持 | 可自写 dialect 与 lowering |
| 上手成本 | 低（读 C++ 即可） | 高（要学 dialect 与 pass 体系） |

所以 MLIR 路线的价值不是「更快」，是**可控制的深度**。当你要给一个 LLVM 没覆盖的加速器生成代码时，C++ 出口给不了你那个控制点。这是 Ch 10 之后整段的动机。

## 取舍表：三条出口

| | C++（CPU） | Triton（GPU） | 直接生成机器码（不存在） |
|---|---|---|---|
| **编译速度** | 快（小 kernel 通常秒级以内，具体值自己计时） | 中（要走 Triton 编译链） | 理论上最快（跳过 C++ 编译），但要自己实现后端 |
| **峰值性能** | 依赖 g++/clang 的优化质量，通常接近手写 | 高（针对 GPU 内存层次设计） | 取决于实现，通常不如成熟后端 |
| **可移植性** | 高（换编译器即可） | 低（绑定 NVIDIA/AMD 的 GPU 生态） | 低（每个 ISA 一套） |
| **可调试性** | 高（源码可读、可加断言） | 中（Triton 源码可读，但映射到硬件不直观） | 低（汇编） |
| **边缘可用性** | 需要能跑 C++ 工具链；裸机要替换 `cpp_prefix.h` | 不可用（多数 MCU 无 GPU） | 理论可用，但等于自建后端 |

表格里「编译速度」一栏没有给具体秒数——这个数字强依赖 kernel 大小与机器，凭记忆写一个数就是编造。要测就自己计时：包住第一次 `torch.compile` 调用，用 `time.perf_counter()`。注意要把第一次调用单独计时，第二次开始命中缓存，数字没有意义。

## 踩雷集锦

1. **以为 Inductor 生成汇编**：它生成 C++ 字符串，交给 `g++`/`clang`。你在缓存里找不到 `.s` 或 `.S` 文件，因为没有这一步。要汇编就自己 `g++ -S` 编那个 `.cpp`。
2. **以为 `Vectorized<float>` 宽度固定是 8**：8 来自 AVX2。ARM NEON 上是 4，AVX-512 上是 16。类型名里没有宽度，宽度藏在 `loadu(..., 8)` 那个参数和目标架构里。写性能模型时弄错这一条，所有估算都偏。
3. **忽略 `.so` 是动态链接**：`file` 输出里的 `dynamically linked` 意味着运行时需要 libc、libstdc++、libtorch 的符号。边缘设备上这些可能没有，或者版本不匹配。裸机必须静态链接，且要替换掉 `cpp_prefix.h` 依赖的那部分。
4. **以为 `.py` 文件就是 kernel**：`.py` 是编译任务清单 + 调用封装，真正的计算在 `.cpp` / `.so` 里。只读 `.py` 会以为「代码就这么点」，看不到实际循环。
5. **以为换架构只要改个 flag**：`-march` 换了，`Vectorized` 的宽度、可用指令、甚至头文件可用性都变了。Cortex-M 上还要处理没有 OS、没有 libc 的问题（Ch 19）。「改个 flag」的预期会在第一次交叉编译时被打碎。

## 本章重点整理

- 三条出口：C++（CPU，实测）、Triton（GPU，按设计如此）、以及不存在的「直接机器码」。
- 缓存四类产物各有生产者：Dynamo 写 `fxgraph/`，AOTAutograd 写 `aotautograd/`，Inductor 写 `.py` 与 `.cpp`，系统编译器写 `.so`。
- `async_compile.cpp_pybinding` 是「登记 + 声明签名 + 异步提交编译」三合一。
- `at::vec::Vectorized<float>` 是 C++ 重载运算符实现的 SIMD 抽象，靠类型系统而非模式匹配。
- 不直接生成汇编的三个理由：指令选择复杂度、可移植性、可调试性。
- 核心分工：**AI 编译器管「怎么组织计算」，传统编译器管「怎么变成指令」**。
- `.so` 是动态链接的 ELF，边缘部署要换成静态链接。

## 自我检核

- [ ] 不看笔记，画一遍从 FX Graph 到 `.so` 的链路，标出每一步的产物文件名
- [ ] 解释 `async_compile.cpp_pybinding` 这一行做了哪三件事
- [ ] 说出「不直接生成汇编」的三个理由，并按重要性排序，说明你的排序依据
- [ ] 为什么 `Vectorized<float>` 的宽度不能当成常量 8？给出三个平台的对应值
- [ ] `file` 输出的 `dynamically linked` 对边缘部署意味着什么？说出至少两个具体后果
- [ ] 用一句话说清 Inductor 与 g++ 的分工，并举一个「这个决定该由谁做」的例子
- [ ] 对照 MLIR 路线，说出「停在 C++」与「降到 LLVM IR」各自换来了什么

## 延伸阅读

- **[torch/_inductor/codegen/cpp.py](https://github.com/pytorch/pytorch/blob/main/torch/_inductor/codegen/cpp.py)**
  - **读哪里**：`CppKernel` 类里生成循环与 `Vectorized` 调用的部分（搜 `loadu`、`store`）
  - **学什么**：`loadu(ptr, 8)` 里的 8 是怎么算出来的；尾部代码在哪一段生成
  - **关联**：Ch 8 的向量宽度讨论在这里落地成代码
  - **前提**：读过 Ch 6，能定位缓存产物；否则不知道自己在读哪一段的输出

- **[torch/_inductor/async_compile.py](https://github.com/pytorch/pytorch/blob/main/torch/_inductor/async_compile.py)**
  - **读哪里**：`cpp_pybinding` 与 `CppCompiler` 相关类
  - **学什么**：提交给 g++/clang 的完整命令行（优化级别、`-march`、include 路径）；这决定了 C++ 出口给后端留了多少优化空间
  - **关联**：本章「一次完整链路」第 ④ 步的实现，也是「交叉编译会踩什么」的答案
  - **前提**：知道 include path、编译单元、`-O2` 与 `-O3` 的差别

- **[Triton — 官方文档](https://triton-lang.org/main/index.html)**
  - **读哪里**：Getting Started 与 `triton.language` 里 `program_id` / `tl.load` 的说明
  - **学什么**：GPU 出口为什么是「块级 DSL」而非逐线程代码；和 C++ 出口的抽象层次差在哪
  - **关联**：本章取舍表里 Triton 那一列的来源
  - **前提**：有 CUDA 的 block / grid 基本概念；没有的话先读 CUDA 入门，否则 `program_id` 的含义抓不住

- **[TorchInductor: A PyTorch-native Compiler with Definite and Dynamic Shapes](https://dev-discuss.pytorch.org/t/torchinductor-a-pytorch-native-compiler-with-definite-and-dynamic-shapes/1056)** — 2022
  - **读哪里**：设计动机与 codegen 两节
  - **学什么**：为什么选择「生成 C++/Triton 源码」而不是直接对接 LLVM——本章核心问题的官方回答
  - **关联**：本章「为什么不直接生成汇编」的第一手依据
  - **前提**：读过 Ch 6；这是设计文档不是论文，可读性高，不必准备理论

- **《Engineering a Compiler》** — Cooper & Torczon（第 3 版，2022）
  - **读哪里**：第 11 章（指令选择）、第 13 章（寄存器分配）
  - **学什么**：C++ 出口「跳过的那些工作」到底有多复杂；读完会理解为什么没人想重写一遍后端
  - **关联**：Ch 15–17 的预备读物
  - **前提**：知道基本的数据流分析与图着色概念；第 3 版改了章节编号，按标题找

到这里，`torch.compile` 这条线走到了尽头：图被改写、循环被调度、代码被生成、最后交给系统编译器。下一章换一条完全不同的路线——同一张图，如果走 MLIR，会变成什么样。

→ [Ch 10 MLIR：把「算子」变成「循环」](./10-mlir-lowering.md)
