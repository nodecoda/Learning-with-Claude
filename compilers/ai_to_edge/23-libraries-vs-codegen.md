# Ch 23 算子库还是代码生成

> **目标**：面对同一个 `matmul`，能在「调库（BLAS / oneDNN / cuBLAS）」和「自己生成循环」之间做出有依据的选择；能读懂 torch 的 Inductor 输出里「哪部分走了库、哪部分走了生成代码」；能说清小 shape 下为什么调库会输。
>
> **环境**：Ubuntu 22.04 / x86_64 AVX2（无 AVX-512）/ gcc 11 / torch 2.9.1+cpu（MKL 后端）/ `torch.utils.benchmark`

## 为什么需要这个？

Ch 22 把张量摆进了 arena，接下来要让它们真的算起来。

这里的岔路口很早就出现了，而且两个方向都有人在用：

- **调库**：PyTorch 的 `mm` 走 MKL，TFLite 的卷积走 XNNPACK，MCU 上走 CMSIS-NN。这些库是几十人年调出来的汇编。
- **代码生成**：Inductor 把 `relu(x*2+1)` 编成一段 `extern "C"` 的 C++ 循环，TVM / Halide / XLA 各自有类似的路径。

初学者常问「哪个快」。这个问题问错了。**同一个模型里，这两条路同时存在**：矩阵乘交给库，elementwise 交给生成代码。真正要判断的是「这个算子的这个形状，该走哪条路」，以及「我的目标平台上那条路存不存在」。

最后一点在边缘上是决定性的：很多 MCU 上没有 BLAS，也没有操作系统。这不是性能问题，是有没有的问题。

## 先建立直觉

把调库想成**去餐厅点菜**，代码生成想成**自己下厨**：

| | 点菜（调库） | 下厨（代码生成） |
|---|---|---|
| 好吃程度 | 大厨水平，但你只能点菜单上的 | 取决于你的手艺，通常不如大厨 |
| 等多久 | 点菜快，上菜慢（launch + dispatch） | 备菜慢（编译），上菜快 |
| 能改配方 | 不能。你说「少放盐」它不听 | 想怎么改怎么改 |
| 两个人吃 | 也按整桌的流程走一遍，性价比差 | 正好做两份 |
| 厨房没开 | 饿着 | 还能自己煮 |

表格里最重要的两行是「能改配方」和「两个人吃」。

「能改配方」在编译器里叫**融合**：`relu(x*2+1)` 如果调三个库函数，要写三次内存、读三次内存；生成一个循环，读一次写一次。这个差距跟算力无关，纯粹是内存带宽。

「两个人吃」在编译器里叫**小 shape**：库的启动开销（参数检查、dispatch、可能还有线程池唤醒）是固定的，矩阵小了摊不薄。

## 底层机制

### 1. 库是怎么变快的

MKL / OpenBLAS / oneDNN 的矩阵乘不是一个朴素三重循环。它们做的是：

1. **分块（blocking）**：把 1024×1024 切成能塞进 L1/L2 的小块，让每个元素在被换出 cache 之前被尽量多用几次。
2. **打包（packing）**：把 B 的块重新排成连续布局，让内层循环能用连续加载。
3. **微内核（micro-kernel）**：用汇编写一个 `MR × NR` 的小块乘加，寄存器分配手工排到极致，用 FMA 打满。
4. **运行时分派**：启动时用 CPUID 判断有没有 AVX2 / AVX-512 / FMA，选对应的微内核。

这四件事加起来的差距是数量级的。本机证据包（单线程，`torch.set_num_threads(1)`，`torch.mm`，MKL 后端）：

| n | 耗时 | GFLOP/s |
|---|---|---|
| 64 | 0.01 ms | 35.65 |
| 256 | 0.41 ms | 82.16 |
| 512 | 3.08 ms | 87.04 |
| 1024 | 22.20 ms | 96.71 |

这台机器是 AVX2、无 AVX-512。注意 64³ 那一行：**35.65 GFLOP/s，只有 1024³ 的 37%**。按「浮点运算量 ÷ 时间」算，小矩阵本该更快（数据全在 L1），慢的部分是固定开销。

复测说明：上表是证据包数字，本章一律原样引用。我自己在同一台机器上又跑了两轮，得到 49.51 / 91.64 / 95.89 / 91.13 与 42.94 / 71.85 / 78.84 / 71.28 GFLOP/s。绝对值每次都不同（turbo 频率、后台负载、绑哪个核），但**64³ 始终是四个点里最低的**。所以本章凡是「本机复测」的对照表，我都在同一次会话里用 `taskset` 绑同一个核交替测两条路径，只比同一轮的比值，不跨轮比绝对值。

### 2. 库的固定开销：小 shape 的分歧点

用 `torch.utils.benchmark` 量单元素 relu 和 64×64 relu：

```
relu(1 elem)   1.610 us
relu(64x64)    2.010 us
```

64×64 是 4096 个元素，工作量多 4095 倍，耗时只多 25%。**1.610 µs 就是固定的调用开销**：Python 到 C++ 的边界、TensorImpl 检查、dispatcher、kernel launch。

3 个算子串起来（`relu(x * 2.0 + 1.0)`，64×64）：

```
unfused 64x64 3 ops   11.353 us
```

三次调用三次开销，还没算中间张量的分配和三次额外的读写。

这个数量级解释了 64³ matmul 的 37%：约 52 万次浮点运算做完只用十几微秒，其中相当一部分是「不管矩阵多大都要付」的钱。

### 3. 融合省的是什么

`relu(x*2+1)`，n = 2^24（约 1678 万元素），单线程，实测：

```
unfused_ms=54.36  fused_ms=17.97
unfused_GBps=7.41  fused_GBps=7.47
max|diff|= 0.0
```

- 未融合：3 个算子，中间张量 `x*2` 和 `x*2+1` 各写一遍再读一遍，总流量 6 × 4 B/元素。
- 融合：读 x 一次、写结果一次，2 × 4 B/元素。
- 时间比 54.36 / 17.97 = **3.03 倍**，正好对上流量比 6 / 2 = 3。

`GBps` 那一列两边都在 7.4 左右，说明**单线程没饱和内存带宽**（Ch 26 会展开），时间几乎完全由流量决定。这就是融合的收益来源：不是算得更快，是搬得更少。

`max|diff| = 0.0` 也值得记一笔：融合本身不改数值。改变数值的是重结合（Ch 25 会看到 `relu(x*2 + (y*3 - z))` 与左结合差 9.5e-07）。

### 4. Inductor 在 CPU 上怎么分工

这是本章最值得亲手看的一段。跑 `torch.compile` 抓 `output_code`：

```python
import torch
torch.set_num_threads(1)
def f(a, b):
    return torch.relu(a @ b + 1.0)
a = torch.randn(64, 64); b = torch.randn(64, 64)
cf = torch.compile(f)
for _ in range(3): cf(a, b)
```

```bash
TORCH_LOGS=output_code python ind.py
```

生成的主干（实测输出，节选）：

```python
buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
# Topologically Sorted Source Nodes: [matmul], Original ATen: [aten.mm]
extern_kernels.mm(arg0_1, arg1_1, out=buf0)
del arg0_1
del arg1_1
buf1 = buf0; del buf0  # reuse
cpp_fused_add_relu_0(buf1)
return (buf1, )
```

而 `cpp_fused_add_relu_0` 的 kernel 本体是：

```cpp
extern "C"  void  kernel(float* in_out_ptr0)
{
    for(int64_t x0=0; x0<4096L; x0+=8L)
    {
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_out_ptr0 + x0, 8);
        auto tmp2 = at::vec::Vectorized<float>(static_cast<float>(1.0));
        auto tmp3 = tmp0 + tmp2;
        auto tmp4 = at::vec::clamp_min(tmp3, decltype(tmp3)(0));
        tmp4.store(in_out_ptr0 + x0);
    }
}
```

三条信息同时出现在这一段里：

1. **`extern_kernels.mm` 是库调用**。Inductor 没有为矩阵乘生成循环，它调 ATen 的 `mm`，最终进 MKL。
2. **`cpp_fused_add_relu_0` 是生成代码**。`add` 和 `relu` 被融进一个循环，而且原地写在 `buf0` 上。
3. **`buf1 = buf0` 是 Ch 22 讲的内存复用**。整条链只申请了一块 64×64。

这就是「混合策略」的具体样子，不是折中口号，是同一份生成代码里两行相邻的语句。

### 5. 库的四个隐性成本

调库不只是「快或慢」，还有四个成本经常被忽略。

**体积。** 本机 `libtorch_cpu.so` 是 432269632 B（约 432 MB），整个 `torch/lib` 目录 445 MB。静态链接 BLAS 到 MCU 上是不可能的事。即使是桌面端的静态链接，Ch 22 实测的 `add_a64` 也有 641112 B，而它只做一次 `printf`。

**算子覆盖。** 库只实现它想实现的算子。量化模型里的 `grouped conv`、`dequant → matmul → requant` 组合、带 per-channel scale 的卷积，库的支持矩阵很不一样。本章实测（`groups=4` 的卷积 vs `groups=1`）：

```
conv g=1 ms=2.598   g=4 ms=0.927  ratio=0.36
```

`g=4` 反而更快，因为参数量和计算量都降到 1/4。但如果某个库没有针对 group 的特化路径，它会退化成循环调用小卷积，性能就不是这个比例了。

**布局假设。** 库往往固定一种 layout。PyTorch 的 CPU 卷积默认 NCHW，XNNPACK 偏好 NHWC。转换的代价可以直接量：

```
NCHW(1,256,56,56) -> NHWC copy   0.586 ms   (3.2 MB 读 + 3.2 MB 写, 约 10.96 GB/s)
```

一次 802816 元素的 layout 转换要 0.586 ms。为了调一个库在入口转一次、出口转回来，就是 1.2 ms 的固定税——很多边缘模型单帧预算才 10–20 ms。

**无法融合。** 库函数的边界就是融合的边界。`relu(matmul(x, w) + b)` 里，`+b` 和 `relu` 不可能进 MKL 的 matmul，只能另外两次遍历。

### 6. 代码生成的成本

反方向也有账要算。

**峰值性能难超手工库。** 本章实测的朴素三重循环（`gcc -O3 -march=native -funroll-loops`，i-k-j 顺序），对比 MKL：

| n | 朴素循环 GFLOP/s（本机） | MKL GFLOP/s（证据包） | 比值 |
|---|---|---|---|
| 64 | 19.42 | 35.65 | 1.8× |
| 256 | 23.89 | 82.16 | 3.4× |
| 512 | 22.12 | 87.04 | 3.9× |
| 1024 | 23.42 | 96.71 | 4.1× |

差 1.8–4.1 倍。这已经是编译器自动向量化过的版本，差距来自分块、打包和手工微内核。**想靠「生成一个普通循环」追上 BLAS 是不现实的。**

但注意这个倍数在预算里意味着什么。如果 matmul 只占单帧 5%，把 4 倍差距补上只换来约 3.8% 的总提升；如果 elementwise 占 60% 且能融合掉 2/3 的流量，收益是 40%。**优化该往哪投，取决于算子占比，不取决于单项差距。**

**编译时间长。** 本机实测：一个 64×64 的 `relu(a@b+1)` 走 `torch.compile`，整段脚本（import + 3 次 warmup + 输出）7.62 秒，其中绝大部分是首次编译。TVM / Halide 的 auto-scheduling 迭代通常在几十分钟到几小时量级——**未实测，理论预期行为**，按官方文档与社区报告估计。嵌入式开发里这会让「改一行 → 看效果」的循环变慢。

**形状特化带来代码膨胀。** 每个 shape 一份 kernel。一个支持 5 种输入分辨率、3 种 batch 的模型，就是 15 份代码。MCU 的 Flash 只有 256 KB（Ch 24 的链接脚本），装不下这种膨胀。

## 例子一：同一个 matmul，两条路的实测对比

先把朴素版本写出来，确认它的上限在哪：

```c
static void mm_ikj(int n, const float *A, const float *B, float *C) {
  memset(C, 0, (size_t)n * n * sizeof(float));
  for (int i = 0; i < n; i++)
    for (int k = 0; k < n; k++) {
      float a = A[i * n + k];
      const float *b = B + k * n;
      float *c = C + i * n;
      for (int j = 0; j < n; j++) c[j] += a * b[j];
    }
}
```

`i-k-j` 顺序是为了让内层对 C 和 B 都连续。`gcc -O3 -march=native -funroll-loops` 之后，实测：

```
n=64 reps=2000 ms=0.0270 gflops=19.42 checksum=120.980
n=256 reps=300 ms=1.4043 gflops=23.89 checksum=487.540
n=512 reps=40 ms=12.1369 gflops=22.12 checksum=980.862
n=1024 reps=6 ms=91.6956 gflops=23.42 checksum=1962.924
```

和 MKL 的差距表已经列过：1.8–4.1 倍。但这里有个更值得看的东西：**朴素版本在大 shape 上效率反而更高**（19.42 → 23.42），和 MKL 的趋势一致。说明固定开销在两边都存在，只是绝对值差了一个量级。

换成 `i-j-k` 顺序（内层跨行访问 B，cache 不友好），同一台机器实测：

```
n=64  ms=0.1700 gflops=3.08
n=256 ms=21.7291 gflops=1.54
n=512 ms=292.2193 gflops=0.92
n=1024 ms=5070.7368 gflops=0.42
```

大 shape 上掉到 0.42 GFLOP/s，比 `i-k-j` 慢 56 倍，比 MKL 慢 230 倍。**循环顺序这一个决定，比「用不用库」影响更大。** 这也是 Ch 8 讲 tiling 的原因：先让访问连续，再谈其他。

### 小 shape 上，库可能输

把 torch 的 eager `f = relu(a@b + 1)` 和 `torch.compile` 后的版本对比（单线程，`torch.utils.benchmark` 中位数）：

| n | eager（MKL + 3 次独立 kernel） | compiled（MKL + 1 次融合 kernel） |
|---|---|---|
| 64 | 16.59 µs | 66.00 µs |
| 256 | 389.20 µs | 447.99 µs |

**小 shape 上 `torch.compile` 更慢**，64×64 慢了 4.0 倍。两侧数值完全一致（`max|diff| = 0.000e+00`），所以不是算法差异，是开销差异：compiled 版本多了一层 wrapper（guards 检查、CUDA graph 之类的前置逻辑在这个后端不一定有，但 Python 层的 `__call__` 路径更深）。

这个结果要小心解读：**它是这个版本、这个后端、这个测量方式下的结论**。`torch.compile` 的收益在 elementwise 大头、或者能消掉大量中间张量的模型上（前面 3.03 倍的融合收益就是证据）。小 matmul 上它的固定成本压不住。

**实践含义**：不要因为「编译过就一定快」而盲目 `torch.compile` 整个模型。按算子类型和 shape 分桶测量。

## 例子二：布局要求带来的转换税

一个真实的坑：手里有 NCHW 的中间张量，想调一个只吃 NHWC 的库。

```python
x = torch.randn(1, 256, 56, 56)          # NCHW
y = x.permute(0, 2, 3, 1).contiguous()    # NHWC，真实拷贝
```

实测：

```
NCHW(1,256,56,56) -> NHWC copy   0.586 ms   (3.2 MB 读 + 3.2 MB 写, 约 10.96 GB/s)
```

`1 × 256 × 56 × 56 = 802816` 个元素，单边 3.2 MB，读加写 6.4 MB。0.586 ms 对应约 10.96 GB/s——脚本里 `GBps` 就是按 `numel*4*2/时间` 算的，这是单线程做一次纯拷贝的合理带宽。

如果模型有 10 个这样的转换点，就是 5.9 ms 的纯开销。前面 `relu(x*2+1)` 在 2^24 元素上的融合收益是 36.4 ms（54.36 - 17.97）。**转换税的量级介于「可忽略」和「吃掉全部收益」之间，必须实测。**

实测三种状态（`x = torch.randn(1,256,56,56)`）：

```python
x.stride()              # (802816, 3136, 56, 1)     NCHW
y.stride()              # (802816, 56, 1, 3136)     permute 之后，仍是视图
y.contiguous().stride() # (802816, 14336, 256, 1)   NHWC，真实拷贝
```

`permute` 只改 stride，不搬数据；`contiguous()` 才搬。所以判断「这一行要不要花钱」看的是**有没有调用 `contiguous()`**，不是看 stride 变没变。下游 kernel 如果只接受连续布局，它会自己偷偷调一次 `contiguous()`——这就是转换税的藏身处。

## 踩雷集锦

1. **以为调库一定快。**
   64³ 的 MKL matmul 是 35.65 GFLOP/s，只有 1024³ 的 37%；而 `torch.compile` 在 64×64 上比 eager 慢 4.3 倍。库的启动开销在小 shape 上摊不薄。
   **纠正**：按 shape 分桶测量，不要用一个「库更快」的结论覆盖所有形状。小 shape 优先考虑融合和减少调用次数，而不是换库。

2. **以为生成代码一定慢。**
   融合 `relu(x*2+1)` 在 2^24 元素上快 3.03 倍（54.36 → 17.97 ms），流量从 6 降到 2 份。生成代码的优势不在单算子峰值，在省内存流量。
   **纠正**：评估生成代码时看「省了多少次遍历」，不要只看单算子 GFLOP/s。

3. **忽略库的 layout 要求。**
   NCHW → NHWC 一次转换 0.586 ms（802816 元素，读写各 3.2 MB）。10 个转换点就是 5.9 ms。库本身的 kernel 可能只快 1 ms。
   **纠正**：把 `contiguous()` 的调用点找出来，计入总账。能在图层面统一 layout（Ch 7 的 layout 优化）就不要在算子边界反复转。

4. **拿「库支持这个算子」当「库对这个 shape 快」。**
   `groups=4` 的卷积实测 0.927 ms，比 `groups=1` 的 2.598 ms 快，因为计算量本来就少 4 倍。但如果库对 group 没有特化，它会退化成循环，此时这个比例完全不成立。
   **纠正**：查库的 dispatch 表（XNNPACK / oneDNN 都有），确认这个 shape 和参数组合真的走特化路径，而不是 fallback。

5. **在 MCU 上假设 BLAS 存在。**
   裸机上没有 BLAS、没有 `malloc`、没有 `printf`。CMSIS-NN / Arm NN / XNNPACK 这类「为 MCU 手写的算子库」才是主流，它们的接口是 `arm_fully_connected_s8(...)` 这种，不认 `cblas_sgemm`。
   **纠正**：先确认目标平台的运行时清单（Ch 24），再决定「调库」这个词在这里指什么。

## 本章重点整理

- 库的四项优势：分块/打包/手写微内核/运行时分派。实测 MKL 比 `-O3 -march=native` 的朴素 i-k-j 循环快 1.8–4.1 倍。
- 库的四项成本：体积（`libtorch_cpu.so` 432269632 B）、算子覆盖、layout 假设、无法融合。
- 生成代码的核心收益是省流量：融合 `relu(x*2+1)` 流量 6→2 份，实测快 3.03 倍，`GBps` 两边都在 7.4 左右，说明瓶颈在搬运。
- Inductor 在 CPU 上是混合策略：`extern_kernels.mm` 调库，`cpp_fused_add_relu_0` 是生成代码，两者在同一个函数里相邻。
- 小 shape 上固定开销主导：单元素 relu 1.610 µs vs 64×64 2.010 µs；64³ MKL 效率只有 1024³ 的 37%。
- layout 转换是真实成本：802816 元素的 NCHW→NHWC 拷贝要 0.586 ms。
- MCU 上「调库」指 CMSIS-NN / Arm NN / XNNPACK 这类手写算子库，不是 BLAS。

## 自我检核

- [ ] 能说出库变快的四个手段，并解释它们为什么难被普通代码生成复制
- [ ] 能复述 64³ 35.65 GFLOP/s 与 1024³ 96.71 GFLOP/s 的差距，并归因到固定开销
- [ ] 能解释融合 `relu(x*2+1)` 为什么是 3.03 倍，以及 7.4 GB/s 这个数字说明了什么
- [ ] 能在 Inductor 输出里指出哪一行是库调用、哪一行是生成代码
- [ ] 能算出一次 NCHW→NHWC 转换在整帧预算里占多少
- [ ] 能说明为什么 MCU 上不能假设 BLAS 存在，以及替代品是什么

## 延伸阅读

- **[Goto & van de Geijn — Anatomy of High-Performance Matrix Multiplication](https://www.cs.utexas.edu/~flame/pubs/GotoTOMS_revision.pdf)**
  - **读哪里**：第 2–4 节，GEPP 的分块与打包策略
  - **学什么**：为什么 1.8–4.1 倍差距来自分块和微内核，而不是指令选择
  - **和本章关联**：本章朴素循环 vs MKL 差距的理论解释

- **[oneDNN 文档](https://oneapi-src.github.io/oneDNN/)**
  - **读哪里**：`Primitives`、`Memory Format` 两节，以及 dev_guide 里的 `Convolution` 支持矩阵
  - **学什么**：库怎么选 layout、怎么做 dispatch、哪些参数组合会 fallback
  - **和本章关联**：踩雷 4「支持这个算子 ≠ 对这个 shape 快」的官方依据

- **[XNNPACK 仓库](https://github.com/google/XNNPACK)**
  - **读哪里**：README 的 operator list 与 `src/` 下的 micro-kernel 目录
  - **学什么**：移动端算子的特化粒度，为什么 NHWC 是它的默认
  - **和本章关联**：边缘「调库」的真实形态

- **[CMSIS-NN](https://github.com/ARM-software/CMSIS-NN)**
  - **读哪里**：`Include/arm_nnfunctions.h` 的 int8 算子签名，以及 `Documentation/` 里的 buffer size 约定
  - **学什么**：MCU 上的算子接口长什么样，为什么要调用者提供 scratch buffer
  - **和本章关联**：没有 `malloc` 的世界里，库和调用者的契约完全不同（Ch 24 展开）

- **[PyTorch Inductor 的 generated code 与 provenance](https://pytorch.org/docs/stable/torch.compiler_inductor_provenance.html)**
  - **读哪里**：`TORCH_LOGS=output_code` 与 `torch._inductor.config.trace.enabled` 相关章节
  - **学什么**：怎么把一行生成代码追回原始 ATen 算子
  - **和本章关联**：本章 `extern_kernels.mm` 与 `cpp_fused_add_relu_0` 的解读方法

库和生成代码两条路各就各位了，但真正把它们跑起来还需要一层东西：启动代码、分配器、`memcpy`、串口输出。下一章把这些拼成最小运行时。

→ [Ch 24 最小运行时](./24-minimal-runtime.md)
