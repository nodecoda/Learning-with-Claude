# Ch 6 — TorchInductor 全链路实测

> **目标**：把 `torch.compile` 这一条黑箱打开，逐段看清它生成的东西。学完这章，你能从缓存目录里的产物反推出「调度器做了什么决定」，并判断某段代码有没有真的被编译。

> **环境**：torch 2.9.1+cpu、Python 3.10、x86_64（AVX2）、Ubuntu 22.04。GPU 版本（Triton 后端）不在本课范围，Ch 9 会说明差异。

## 为什么需要这个？

`torch.compile` 是 AI 编译器里最容易「看起来工作了」的一个：

```python
cf = torch.compile(f)
out = cf(x)      # 没报错，好像成功了
```

但「没报错」可能意味着：

- 真的编译成了一整张图（快）
- 断成 20 张小图（可能更慢）
- 直接 fallback 回 eager（完全没编）

**只有看产物才能确定。**这一章教你怎么看。

## 先建立直觉

把 Inductor 想成一个**流水线工厂**：

```
FX Graph（原料：算子列表）
   │
   ├─① GraphLowering：算子 → 中间表示（IR）
   │     决定：每个算子用什么算法实现
   │
   ├─② 调度（Scheduler）：IR → 循环结构
   │     决定：循环怎么切、向量化多宽、是否并行
   │
   ├─③ 代码生成：循环 → C++ / Triton 源码
   │     决定：具体语法、内存访问方式
   │
   └─④ 编译：C++ → .so
         交给 g++/clang
```

每段的产物都落在缓存目录里，可以逐个检查。

## 打开缓存

默认缓存位置是 `/tmp/torchinductor_<user>/`。显式指定更可控：

```bash
cd code/03_inductor
TORCHINDUCTOR_CACHE_DIR=./cache ~/.venvs/learn/bin/python demo.py
```

实测输出：

```
torch: 2.9.1+cpu
max abs diff: 0.0
shape: (128, 64)
```

看缓存里有什么：

```bash
find cache -type f | head -20
```

实测：

```
cache/pb/cpb3b42cykxxfduf2aumx24dihcbubk37akgstq3mcewkae5jnha.so
cache/pb/cpb3b42cykxxfduf2aumx24dihcbubk37akgstq3mcewkae5jnha.cpp
cache/aotautograd/aq6carhhzmco752wi77rdalubvqe6bc6ktuprrzkiqywjurtpfm6/kmk3577yvv4rzzdz2sx4z5ehaeim5hmdbdotccwmmn2wkimtbor
cache/4l/c4l3uhy3bsqllwexjmcgxgun2x3f2fiu5a27tc3yxmtdx7l745en.main.so
cache/4l/c4l3uhy3bsqllwexjmcgxgun2x3f2fiu5a27tc3yxmtdx7l745en.main.cpp
cache/locks/cpb3b42cykxxfduf2aumx24dihcbubk37akgstq3mcewkae5jnha.lock
cache/6b/c6bxg5rhhtkposbdqtc2ewy6mjjkyszvbo2772vhmrfbfooxtg4g.py
cache/fxgraph/iv/...
```

四类文件，对应四个阶段：

| 文件 | 阶段 | 内容 |
|---|---|---|
| `*.py` | 调度 + 代码生成 | 生成的 Python 封装 + 内嵌 C++ kernel |
| `*.cpp` | 代码生成 | 纯 C++ kernel 源码 |
| `*.so` | 编译 | 编译产物，被 Python 直接 `ctypes` 加载 |
| `fxgraph/` | 图级 | Dynamo 输出的原始 FX Graph |
| `aotautograd/` | 反向图 | AOTAutograd 处理后的图 |

## 阶段① 图级：看算子怎么被分组

```bash
ls cache/fxgraph/*/*/ 2>/dev/null | head
```

这些是序列化后的 FX Graph。更直观的方式是看生成的 `.py` 文件头部：

```bash
head -45 cache/6b/*.py
```

实测（节选）：

```python
cpp_fused_add_relu_0 = async_compile.cpp_pybinding(['float*'], '''
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C"  void  kernel(float* in_out_ptr0)
{
    {
        for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(8192L); x0+=static_cast<int64_t>(8L))
        {
            {
                if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(8192L)))
                {
                    auto tmp0 = at::vec::Vectorized<float>::loadu(in_out_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                    auto tmp1 = static_cast<float>(1.0);
                    auto tmp2 = at::vec::Vectorized<float>(tmp1);
                    auto tmp3 = tmp0 + tmp2;
                    auto tmp4 = at::vec::clamp_min(tmp3, decltype(tmp3)(0));
                    tmp4.store(in_out_ptr0 + static_cast<int64_t>(x0));
                }
            }
        }
    }
}
''')
```

**函数名就是信息**：`cpp_fused_add_relu_0`

- `cpp` = 后端是 C++（不是 Triton）
- `fused` = 融合过的
- `add_relu` = 融合了 `add` 和 `relu` 两个算子
- `_0` = 第 0 个 kernel

一个名字就把阶段①的决定说完了。

## 阶段② 调度：循环结构暴露一切

看这行：

```cpp
for (int64_t x0 = 0; x0 < 8192; x0 += 8) {
```

调度器的三个决定全在这：

| 决定 | 值 | 依据 |
|---|---|---|
| **总工作量** | 8192 | 128 × 64 个元素 |
| **向量化宽度** | 8 | AVX2 的 256 位 = 8 个 float |
| **循环层数** | 1 | elementwise，不需要 tiling |

### 为什么是 8 而不是 4 或 16

AVX2 是 256 位寄存器。`float` 是 32 位：

```
256 / 32 = 8
```

换 AVX-512 就是 16。换 NEON（128 位）就是 4。

> **重要**：这个 8 是**调度器根据目标 CPU 的特性查出来的**，不是你写的。它调用了 `torch.cpu.get_cpu_capability()` 之类的探测。

验证一下——强制不同向量化宽度很难直接做，但可以看它如何随架构变化。用本机 `lscpu` 确认 AVX2：

```bash
lscpu | grep -o 'avx2\|avx512f' | head -2
```

实测：

```
avx2
```

只有 `avx2`，没有 `avx512f` → 宽度 8。一致。

### 条件判断：`C10_LIKELY`

```cpp
if (C10_LIKELY(x0 >= 0 && x0 < 8192)) {
```

这个检查看起来是冗余的（循环条件已经是 `x0 < 8192`）。它存在的原因：

- 编译器要在「有边界检查」的前提下做优化
- `C10_LIKELY` 提示分支预测器：这个条件几乎总是真

这是一个**保守但正确的选择**：加一次比较，换编译器不越界的保证。

## 边界情况：非 8 倍数会怎样

这是本章最有价值的部分。输入 1001 个元素（不是 8 的倍数）：

```bash
~/.venvs/learn/bin/python -c "
import torch, os
os.environ['TORCHINDUCTOR_CACHE_DIR']='/tmp/ind5'
def f(x): return torch.relu(x*2+1)
torch.compile(f)(torch.randn(1001))
"
grep -E 'for\(int64_t|1001|< 1000|Vectorized<float>::loadu|store\(' /tmp/ind5/*/*.py | head -14
```

实测输出：

```
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(1001L); x0+=static_cast<int64_t>(8L))
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
tmp7.store(out_ptr0 + static_cast<int64_t>(x0));
if(C10_UNLIKELY(x0 >= static_cast<int64_t>(1000L) && x0 < static_cast<int64_t>(1001L)))
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(1L));
tmp7.store(out_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(1L));
```

调度器生成了**两套代码**：

1. **主循环**：`Vectorized<float>::loadu(..., 8)`，一次处理 8 个
2. **尾部处理**：`C10_UNLIKELY(x0 >= 1000 && x0 < 1001)`，`loadu(..., 1)`，一次处理 1 个

这就是 Ch 5 说的「动态 shape 让代码体积翻倍」的**具体形态**。

对比 1000 个元素（8 的倍数）：

```
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(1000L); x0+=static_cast<int64_t>(8L))
...
if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(1000L)))
```

**没有尾部代码**。1000 = 125 × 8，正好整除。

> **边缘部署的直接推论**：把 tensor 尺寸对齐到向量宽度（8 / 4 / 16），能省掉整套尾部代码。在 Flash 只有 512KB 的 MCU 上，这不是微优化，是能不能装下的问题。

## 阶段③ 代码生成：三种后端

Inductor 的代码生成有三条出口：

| 后端 | 触发条件 | 产物 | 适合 |
|---|---|---|---|
| **C++** | CPU（x86/ARM） | `at::vec::Vectorized` + 手写循环 | CPU |
| **Triton** | CUDA / XPU | Triton DSL → PTX | GPU |
| **Halide / C++ 变体** | 特定配置 | 类似 C++ | 实验性 |

本章看到的是 C++ 后端（`async_compile.cpp_pybinding`）。这也解释了为什么第 8 行是 `Vectorized<float>`——那是 PyTorch 的 SIMD 抽象层，在 x86 上映射到 AVX，在 ARM 上映射到 NEON。

**关键洞察**：Inductor 不做「指令级代码生成」，它生成的是**带 SIMD 抽象的 C++**，然后**交给 g++/clang**。真正的指令选择发生在编译器里（Ch 15）。

```
Inductor  →  C++ + Vectorized<>  →  g++  →  机器码
             （循环与向量化）      （指令选择、寄存器分配）
```

这个分工很重要：AI 编译器管「怎么组织计算」，传统编译器管「怎么变成指令」。

## 阶段④ 编译：产物是什么

```bash
file cache/4l/*.main.so
```

实测：

```
cache/4l/c4l3uhy3bsqllwexjmcgxgun2x3f2fiu5a27tc3yxmtdx7l745en.main.so: ELF 64-bit LSB shared object, x86-64, ... dynamically linked
```

这是一个共享库，Python 用 `ctypes.CDLL` 加载，直接调用 `kernel` 函数。没有 Python 解释器开销——这是加速的主要来源之一。

## 完整实测：一个小函数的全链路

`code/03_inductor/demo.py` 用的是 `x @ y + 1.0`。换一个纯 elementwise 的，看融合最彻底的样子：

```bash
~/.venvs/learn/bin/python -c "
import torch, os
os.environ['TORCHINDUCTOR_CACHE_DIR']='/tmp/ind6'
def f(x, y, z): return torch.relu(x * 2 + y * 3 - z)
x,y,z = [torch.randn(256) for _ in range(3)]
print('diff:', (torch.compile(f)(x,y,z) - f(x,y,z)).abs().max().item())
"
grep -E 'extern \"C\"|for\(int64_t|loadu|store' /tmp/ind6/*/*.py | sed 's/^ *//' | head -12
```

实测输出：

```
diff: 0.0
```

生成的 kernel（节选）：

```cpp
extern "C"  void  kernel(const float* in_ptr0,
                         const float* in_ptr1,
                         const float* in_ptr2,
                         float* out_ptr0)
{
    for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(256L); x0+=static_cast<int64_t>(8L))
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
        auto tmp4 = at::vec::Vectorized<float>::loadu(in_ptr1 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
        auto tmp9 = at::vec::Vectorized<float>::loadu(in_ptr2 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
        ...
        tmp11.store(out_ptr0 + static_cast<int64_t>(x0));
}
```

注意签名里 **三个 `const float*`**：Inductor 知道这三个输入是只读的，加了 `const`。这对后端优化有实际意义——编译器可以据此判断不需要写回、可以做更激进的寄存器复用。

**四路融合成一个循环**：

- 3 个输入各 load 一次
- `x*2 + y*3 - z` 再加 `relu`，全在一个循环体内
- 1 个输出 store 一次

如果不融合：3 次 kernel 调用，每次都要把中间结果写回内存再读出来。这里是 **1 次读 + 1 次写**。

## 踩雷集锦

1. **不确定有没有编，只看速度**：速度受缓存、频率、噪声影响，不能作为判据。看缓存目录、看 `TORCH_LOGS`。
2. **以为 `max abs diff: 0.0` 一定成立**：CPU + 同精度 + elementwise 时成立。涉及 `matmul`（不同 BLAS 路径）、`fast-math`、或跨架构时会变。
3. **忽略尾部代码的成本**：`1001` vs `1000` 的差别是「多一整套尾部处理」。MCU 上这是几百字节 Flash。
4. **以为 Inductor 直接生成机器码**：它生成 C++，交给 g++/clang。指令选择是那个编译器做的。
5. **把 `Vectorized<float>` 当成固定宽度**：宽度 8 来自 AVX2。ARM NEON 是 4，AVX-512 是 16。写死这个数字会算错性能模型。

## 本章重点整理

- Inductor 四阶段：图级分组 → 调度 → 代码生成 → 编译。
- 缓存目录的四类文件对应四个阶段，都在那里可以查。
- 函数名 `cpp_fused_add_relu_0` 直接编码了融合决策。
- 向量化宽度由**目标 CPU 能力**决定（AVX2 → 8）。
- 非对齐尺寸会生成「主循环 + 尾部处理」两套代码。
- Inductor 生成带 SIMD 抽象的 C++，真正的指令选择交给 g++/clang。

## 自我检核

- [ ] 能说出缓存目录里 `.py` / `.cpp` / `.so` / `fxgraph/` 各对应哪个阶段
- [ ] 看到 `cpp_fused_add_relu_0` 能解释出三件事
- [ ] 能解释为什么向量化宽度是 8，以及换成 ARM NEON 会变成多少
- [ ] 能说明「1001 个元素」比「1000 个元素」多生成了什么代码
- [ ] 知道 Inductor 与 g++/clang 的分工边界在哪

## 延伸阅读

### 源码依据

- **[torch/_inductor/](https://github.com/pytorch/pytorch/tree/main/torch/_inductor)**
  - **读哪里**：`scheduler.py`（调度决策）、`codegen/cpp.py`（C++ 生成）、`graph.py`（GraphLowering）
  - **为什么值得读**：本章所有结论的一手来源；配合缓存产物对照读效率最高
  - **注意**：代码量很大，按「缓存里看到什么 → 找生成它的代码」的顺序读，不要通读

- **[torch/_inductor/config.py](https://github.com/pytorch/pytorch/blob/main/torch/_inductor/config.py)**
  - **读哪里**：`cpp` 相关配置项，尤其是向量化与 `max_autotune` 部分
  - **和本章的关联**：向量化宽度的选择逻辑在这里可调

### 官方文档

- **[torch.compiler — Inductor 后端](https://pytorch.org/docs/stable/torch.compiler_inductor_provenance.html)**
  - **读哪里**：provenance tracking（追踪哪个算子生成了哪段代码）
  - **为什么值得读**：比手动翻缓存更系统的定位手段
- **[torch.compile 性能调优指南](https://pytorch.org/tutorials/recipes/torch_compile_backend_tutorial.html)**
  - **读哪里**：如何开启 `TORCH_LOGS`、如何读生成的代码
  - **注意**：部分内容针对 GPU，CPU 部分选择性读

### 论文

- **[TorchInductor: A PyTorch-native Compiler with Definite and Dynamic Shapes](https://dev-discuss.pytorch.org/t/torchinductor-a-pytorch-native-compiler-with-definite-and-dynamic-shapes/1056)** — 2022
  - **核心贡献**：Inductor 的设计动机（为什么不用现成的 TVM/XLA）、调度与 codegen 的分工
  - **读哪里**：整篇（是设计文档不是论文，可读性强）
  - **和本章的关联**：本章四阶段划分的原始说明

### 博客 / 工程文章

- **[PyTorch — Inductor 源码走读系列](https://dev-discuss.pytorch.org/)**
  - **这系列说什么**：社区维护的 Inductor 内部机制解析
  - **读哪里**：搜 `inductor` 标签下带「walkthrough」「internals」的帖子
  - **为什么值得读**：比读源码快，比文档深

### 书籍

- **《Computer Architecture: A Quantitative Approach》** — Hennessy & Patterson（第 6 版）
  - **读哪几章**：第 4 章（向量化、SIMD 架构）
  - **和本章的关联**：理解为什么向量化宽度是 8 而不是任意值，需要知道 SIMD 寄存器的硬件约束

知道了 Inductor 怎么把图变成循环，下一章往上看：在生成循环之前，图本身被怎么改写了。

→ [Ch 7 图级优化：融合、常量折叠与 layout](./07-graph-optimizations.md)
