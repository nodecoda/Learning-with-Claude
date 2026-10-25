# Ch 4 算子与融合边界：代价模型

> **目标**：建立一套能落地的算子代价模型——elementwise、reduction、matmul、conv 各自的时间下限在哪里；用算术强度和实测数字判断「该不该融合」；并给出一个「不该融合」的真实反例。

## 为什么需要这个？

融合是 AI 编译器最常做的优化，也是最容易被滥用的优化。很多人凭直觉认为「融合一定更快」，于是：

- 把两个 matmul 融成一个 kernel，结果重复计算，慢了一倍。
- 把一个 reduction 融进 elementwise，结果并行度掉了，反而更慢。
- 把所有中间结果都留在寄存器里，结果寄存器溢出，性能崩掉。

融合不是免费的。它换来的是「少读写一次中间结果」，付出的是「更多的活跃值、更复杂的循环、更少的并行机会」。**判断该不该融合，靠的是代价模型，不是口号。**

这一章先用算术强度解释「瓶颈在算还是在读写」，再用本机实测比较 eager 与融合 kernel，最后给一个真实的反例。

> **机器说明**：Intel i7-10510U（4 核 8 线程），Windows 11，Python 3.11.12，torch 2.14.1+cpu，TVM 0.27.0.post1，`torch.set_num_threads(4)`。笔记本频率和负载波动很大，下面的数字都是**同机、同进程、交错测量**的中位数，只用于相对比较，不代表硬件上限。

## 先建立直觉：时间花在算还是花在读？

一个算子的执行时间，粗略由两部分决定：

```text
time ≈ max( FLOPs / 峰值算力 , bytes / 有效带宽 )
```

- 如果 `FLOPs / 峰值算力` 更大，它是**compute-bound**（算力瓶颈）。
- 如果 `bytes / 有效带宽` 更大，它是**memory-bound**（带宽瓶颈）。

两者的比值叫**算术强度**（arithmetic intensity）：

```text
AI = FLOPs / bytes        # 单位：flop/byte
```

算术强度越高，越接近 compute-bound；越低，越接近 memory-bound。融合之所以有用，主要是因为它**降低了 bytes**，从而把 memory-bound 的算子推向 compute-bound。

## 核心概念一：四类算子的代价模型

### elementwise

`y = f(x)`，逐元素，无跨元素依赖。

```text
FLOPs ≈ c * N          # c 是每个元素的浮点运算数
bytes ≈ (d_in + d_out) * N * sizeof(dtype)
```

对 float32、单输入单输出：`bytes = 8N`，`AI ≈ c/8`。即使 `c=4`，AI 也只有 0.5——**几乎总是 memory-bound**。

### reduction

`y = sum(x, axis)`，读 N 个元素，写少量输出。

```text
FLOPs ≈ N
bytes ≈ N * sizeof(dtype) + output_bytes
AI ≈ 1/4 = 0.25          # float32
```

也是 memory-bound，但比 elementwise 多一个限制：**并行度**。沿最内层求和需要跨 lane 归约；沿最外层求和则可以先并行再合并。轴的选择会显著影响 kernel 结构。

### matmul

`C[M,N] = A[M,K] @ B[K,N]`。

```text
FLOPs = 2 * M * N * K
bytes ≈ (M*K + K*N + M*N) * sizeof(dtype)
```

方阵 `M=N=K=n` 时：

```text
AI ≈ 2n³ / (12n²) = n/6
```

`n=512` 时 AI ≈ 85，`n=1024` 时 AI ≈ 171。**matmul 通常是 compute-bound**，所以它值得用 tiling、向量化、多线程去榨算力；融合它的 epilogue（bias/relu）主要是省掉输出的一次读写，收益不如 elementwise 融合那么夸张。

### conv

直接卷积的 FLOPs：

```text
FLOPs = 2 * N * C_out * C_in * K_h * K_w * H_out * W_out
```

它的算术强度取决于**数据复用**：一个输入像素会被多个输出通道/多个窗口复用。实际实现通常转成 implicit GEMM（im2col 的思想），把卷积变成 matmul。小 conv 的 FLOPs 很小，容易被框架开销和内存访问支配；大 conv 才逐渐接近 matmul 的效率。

## 核心概念二：算术强度与 Roofline

把四类算子放在同一张「AI 轴」上：

| 算子 | 典型 FLOPs | 典型 bytes | 算术强度（估算） | 瓶颈 |
|---|---|---|---|---|
| elementwise 链（3 op，融合） | 3N | 8N | 0.375 | 带宽 |
| elementwise 链（3 op，不融合） | 3N | 24N | 0.125 | 带宽 |
| sum reduction | N | 4N | 0.25 | 带宽/并行度 |
| matmul 512² | 268M | 3.1MB | ~85 | 算力 |
| conv 3→32, 64², k3 | 7.1M | ~0.58MB | ~12 | 混合 |

读法：AI 越低，越应该优先**减少内存流量**（融合、原地复用、layout）；AI 越高，越应该优先**提高算力利用率**（tiling、向量化、并行）。

Roofline 模型就是把这两条上限画在一起：

```text
performance = min(peak_flops, AI * bandwidth)
```

本机没有做峰值算力标定，所以这里不写「理论峰值」。但实测的有效带宽大约在 5–9 GB/s（见下文），这个数字足以解释 elementwise 的时间。

## 核心概念三：融合边界

融合不是「相邻就能合」。合法性看三件事：**迭代空间是否一致、有没有跨迭代依赖、融合后会不会重复计算**。

### elementwise：最容易融合

`add`、`mul`、`relu`、`sigmoid` 都是逐元素的，迭代空间都是 `0..N`。融合后：

```text
for i in range(N):
  t = x[i] * 2 + 1
  y[i] = max(t, 0)
```

一次读、一次写，中间值留在寄存器。合法、简单、收益稳定。

### reduction：要看轴和消费者

- **producer 是 elementwise，consumer 是 reduction**：可以把 elementwise 折进 reduction 的读循环，少写一次中间结果。常见且安全。
- **producer 是 reduction，consumer 是 elementwise**：可以把 consumer 放进 reduction 的 epilogue（归约完马上算），但要注意 reduction 的输出通常很小，收益有限。
- **两个 reduction 轴不同**：`sum(dim=1)` 和 `sum(dim=0)` 的迭代空间不同，不能简单合成一个循环；要么分两趟，要么重排。
- **reduction 的 consumer 需要完整结果**：比如 softmax 需要先算 max/sum 再逐元素除，必须分阶段；强行融合会重复计算。

### matmul：epilogue 是常客，producer 要小心

matmul 的输出 tile 在寄存器里，bias/relu/量化这类 **epilogue** 可以顺手做掉，省一次 `C` 的写回和再读。这是 Inductor/TVM 都很常见的融合。

但**两个 matmul 不能随便融**：第二个 matmul 需要完整的 K 维归约，而第一个 matmul 的输出 tile 只覆盖部分 K。除非做专门的“matmul + matmul”融合（如 FlashAttention 式的分块），否则会重复计算或需要中间 materialize。

### conv：epilogue 融合最常见

conv + bias + BN + relu 是推理图的标配。把 BN 折进 conv 的权重/偏置（常量折叠），再把 relu 放进 epilogue，能省掉多次整张特征图的读写。这是 Ch 7 和 Ch 9 的内容。

## 核心概念四：融合的代价

融合的收益是「少读写」，代价有三类：

1. **重复计算**：一个 producer 有多个 consumer，如果每个 consumer 都把它融进去，producer 会被算多次。算术强度越高，重复计算越贵。
2. **寄存器压力**：融合后同时活跃的中间值变多，寄存器不够就 spill 到栈，性能反而下降。
3. **并行度下降**：把一个大的 elementwise 和一个小的 reduction 融合，可能导致线程块变小、负载不均。

第 1 条可以用真实数字量化（见示例三）；第 2、3 条需要 profiler 和具体硬件，本章标 `guided`。

## 底层机制：融合在 IR 上到底改了什么

以 `y = relu(x * 2 + 1)` 为例，未融合的图有三个算子：

```text
%a = mul(%x, 2.0)
%b = add(%a, 1.0)
%y = relu(%b)
```

融合后变成一个「复合算子」或一个 `call_tir`：

```text
%y = fused_mul_add_relu(%x)
```

在循环层，融合把三个独立的循环 nest 合成一个：

```text
# 未融合：三次遍历，三次读写
for i: a[i] = x[i] * 2
for i: b[i] = a[i] + 1
for i: y[i] = max(b[i], 0)

# 融合：一次遍历，一次读写
for i:
  t = x[i] * 2 + 1
  y[i] = max(t, 0)
```

合法性条件：三个循环的边界相同（都是 `N`），且没有跨迭代依赖。只要有一个条件不满足，就不能直接合并。

## 示例一：elementwise 链，eager vs TVM 融合

先写 eager 版本：

```python
import torch

def eager(x):
    return torch.relu(x * 2.0 + 1.0)
```

再用 TVM 写一个融合 kernel：

```python
import tvm
from tvm import te

N = 8 * 1024 * 1024
X = te.placeholder((N,), name="X")
Y = te.compute(
    (N,),
    lambda i: te.max(
        X[i] * te.const(2.0, "float32") + te.const(1.0, "float32"),
        te.const(0.0, "float32"),
    ),
    name="Y",
)
func = te.create_prim_func([X, Y])
sch = tvm.s_tir.Schedule(func)
f = tvm.build(sch.mod, target="llvm")
```

这不是让 TVM 自动融合 eager 图，而是**手工把三个算子写进一个 `te.compute`**，模拟「融合后的 kernel 应该长什么样」。TVM 的自动融合在 Ch 7/12 展开；本章只关心融合前后的时间差。源码依据：`te.create_prim_func` 在 `tvm/te/operation.py:538`，`te.compute` 在 `:60`（tvm 0.27.0.post1）。

实测 TIR（tvm 0.27.0.post1，省略 import 注释）：

```text
@T.prim_func(s_tir=True)
def main(X: T.Buffer((8388608,), "float32"), Y: T.Buffer((8388608,), "float32")):
    T.func_attr({"tirx.noalias": True})
    # with T.sblock("root"):
    for i in range(8388608):
        with T.sblock("Y"):
            v_i = T.axis.spatial(8388608, i)
            T.reads(X[v_i])
            T.writes(Y[v_i])
            Y[v_i] = T.max(X[v_i] * T.float32(2.0) + T.float32(1.0), T.float32(0.0))
```

只有一个循环、一次读、一次写。eager 版本则是三个独立 kernel，每个都要读一遍、写一遍。

测量方法：同一进程内交错执行 eager 和 TVM，预热 5 次，测 40 组，取每组的中位数：

```python
import time, statistics
import numpy as np

x = torch.randn(N)
tx = tvm.runtime.tensor(x.numpy(), tvm.cpu())
ty = tvm.runtime.empty((N,), "float32", tvm.cpu())
f(tx, ty)
print("maxerr", float(np.abs(ty.numpy() - eager(x).numpy()).max()))

for _ in range(5):
    eager(x); f(tx, ty)

te_, tf_ = [], []
for _ in range(40):
    t0 = time.perf_counter(); eager(x); t1 = time.perf_counter()
    f(tx, ty); t2 = time.perf_counter()
    te_.append((t1 - t0) * 1e3)
    tf_.append((t2 - t1) * 1e3)

print("eager med %.2f  min %.2f  p25 %.2f  p75 %.2f" % (
    statistics.median(te_), min(te_), np.percentile(te_, 25), np.percentile(te_, 75)))
print("tvm   med %.2f  min %.2f  p25 %.2f  p75 %.2f" % (
    statistics.median(tf_), min(tf_), np.percentile(tf_, 25), np.percentile(tf_, 75)))
ratios = [a / b for a, b in zip(te_, tf_)]
print("paired ratio med %.2f  p25 %.2f  p75 %.2f" % (
    statistics.median(ratios), np.percentile(ratios, 25), np.percentile(ratios, 75)))
```

实测结果（N=8M，float32，i7-10510U，4 线程；下面是**一次**运行，重跑会漂移）：

```text
eager med 37.07 ms  min 22.66  p25 29.37  p75 53.31
tvm   med 12.42 ms  min  7.32  p25 10.07  p75 14.03
paired ratio med 3.35  p25 2.08  p75 4.79
```

`maxerr = 0.0`（两边都是 `max(x*2+1, 0)`，运算顺序一致）。

怎么读这组数字：

- eager 三次遍历，理论流量 `3 × 64MB = 192MB`；37.07 ms 对应约 **5.2 GB/s**。
- TVM 一次遍历，流量 `64MB`；12.42 ms 也对应约 **5.2 GB/s**。
- 两者都贴着同一个有效带宽，说明瓶颈确实是内存，不是浮点运算。
- 配对比值的中位数在 2.5–3.5 倍之间，正好对应「三次读写 → 一次读写」；具体落点取决于当次机器状态。

注意 p25–p75 很宽（29–53 ms），这是笔记本的常态；重跑的绝对值可能落到 24–40 ms。**不要拿单次 22 ms 或 53 ms 当结论**，也不要把某一次的 3.35 当成常数；配对比值比单独看两个中位数更稳。同一脚本在另一次运行里给过 `eager 23.82 / tvm 9.44 / ratio 2.51`，这不是代码变了，是机器状态变了。

参考：纯 `mul` 的中位数 11.40 ms，纯 `copy_` 9.35 ms（约 7.2 GB/s）。融合 kernel 12.42 ms 比纯 copy 只慢一点，说明它已经接近「一次读 + 一次写」的下限。

## 示例二：matmul / conv / reduction 的实测吞吐

### matmul

方阵 float32，20 次测量取中位数：

```text
n=256  med 0.86 ms  39.1 GFLOPS
n=512  med 7.05 ms  38.1 GFLOPS
n=1024 med 38.05 ms 56.4 GFLOPS
```

趋势：矩阵越大，越能摊薄调度和内存开销，GFLOPS 上升。`n=512` 的 AI ≈ 85，已经属于 compute-bound；它的性能主要由 BLAS 的 tiling/多线程决定，不是融合能救的。

### conv

`Conv2d(3, 32, 3, padding=1)`，输入 `64×64`，30 次测量取中位数：

```text
bs=1  med 0.563 ms  12.6 GFLOPS
bs=8  med 6.806 ms   8.3 GFLOPS
```

小 conv 的 FLOPs 只有 7.1M（bs=1），时间很大程度上被框架调度、im2col/权重准备、内存访问支配；bs=8 的 GFLOPS 反而下降，说明它没有进入稳定的 compute-bound 区间。**不要用这个小实验推断「conv 比 matmul 慢」**，它只说明「小算子的开销占比高」。

### reduction

```text
sum 8.4M:  med 7.55 ms  4.45 GB/s
sum 67.1M: med 31.98 ms 8.39 GB/s
```

reduction 的 AI 只有 0.25，时间几乎完全由读带宽决定。大数组的 GB/s 更高，是因为固定开销被摊薄。

## 示例三：不该融合——重复计算

构造一个 diamond：`h = A @ W` 被两个消费者使用。

```python
M = K = 512
A = torch.randn(M, K)
W = torch.randn(K, K)

def once():
    h = A @ W
    return h.sum(1)

def twice():
    h1 = A @ W
    s1 = h1.sum(1)
    h2 = A @ W
    return s1 + h2.sum(1)

def med(fn, iters=15):
    for _ in range(3):
        fn()
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter(); fn(); ts.append(time.perf_counter() - t0)
    return statistics.median(ts) * 1e3

print("matmul once med %.2f ms" % med(once))
print("matmul twice med %.2f ms" % med(twice))
```

如果编译器「按消费者各自融合」，`A @ W` 会被算两次。实测（15 组交错，中位数）：

```text
matmul once med 3.52 ms
matmul twice med 8.22 ms
ratio 2.34
```

重复一次 matmul，时间变成 2.3 倍左右（本机重跑落在 2.0–2.4 倍之间）。这不是精确的线性 2 倍，因为两次 matmul 之间可能有 cache 复用/调度重叠，但方向很清楚：**高算术强度的 producer 被重复计算，代价极高。**

正确做法：

- 如果 producer 的结果被多个消费者使用，且 producer 很贵，**materialize 一次**，让消费者读同一个 buffer。
- 如果 producer 很便宜（比如 `x + 1`），重复计算可能比 materialize 更划算——这时才考虑复制融合。
- 决策依据是「重复计算的 FLOPs × 次数」 vs 「materialize 的 bytes」。

### 寄存器压力（`guided`）

把很多条独立分支融进同一个 kernel，会让同时活跃的中间值变多。寄存器不够时，编译器会 spill 到栈，每次读写都变成内存访问，性能可能比不融合更差。

本机没有 GPU profiler，也没有稳定的寄存器压力基准，所以这里不给数字。判断方法：

- 看生成代码的寄存器使用/ spill 报告（LLVM `-Rpass-analysis`、CUDA `ptxas -v`）。
- 看融合后 kernel 的 occupancy（GPU）或 cache miss（CPU）。
- 用「融合 vs 不融合」的 A/B 实测，而不是只看 IR 大小。

## 对比与取舍

| 融合类型 | 合法性 | 典型收益 | 典型风险 |
|---|---|---|---|
| elementwise + elementwise | 迭代空间相同、无依赖 | 高（减少 2/3 流量） | 几乎无 |
| elementwise → reduction | 读循环可合并 | 中（省中间写回） | 归约并行度下降 |
| reduction → elementwise epilogue | 结果可逐元素处理 | 低–中 | 需要等归约完成 |
| matmul + epilogue | 输出 tile 在寄存器 | 中（省 C 的读写） | epilogue 太长会增寄存器压力 |
| matmul + matmul | 需要分块/特殊算法 | 视算法而定 | 重复计算、复杂度高 |
| 多消费者 producer | 需保证只算一次 | 负（重复计算） | 高算术强度下严重变慢 |

## 踩雷集锦

1. **「融合一定更快」**：diamond 反例实测慢 2 倍以上；高 AI 的 producer 不能随便复制。
2. **把 reduction 和 elementwise 无脑合**：轴不同、需要完整结果时合不了；强行合会重复计算或串行化。
3. **忽略 epilogue 的寄存器成本**：matmul + 很长的 epilogue 可能让 tile 放不下，spill 后更慢。
4. **只看 FLOPs 不看 bytes**：elementwise 的 FLOPs 很少，但 bytes 很大；优化方向是省流量，不是省乘法。
5. **拿小 conv 的 GFLOPS 推断全局**：小算子被开销支配，数字没有代表性。
6. **用单次计时下结论**：本机 p25–p75 可以差 2 倍；至少给中位数、样本数、波动范围。
7. **把「融合后 kernel 数变少」当成「一定更快」**：kernel 数少不等于流量少；如果每个 kernel 更大但访存更差，可能更慢。
8. **忘记数值对齐**：融合改变运算顺序/精度（尤其 reduction），必须用 `allclose` 验收；本章 elementwise 例子 `maxerr=0`，reduction 融合通常不是 0。

## 进阶：再往深一层

- 用 TVM 的 `sch.fuse` 把多层循环合并，观察 TIR 和生成代码的变化；再对比不融合的版本（Ch 12）。
- 用 `torch.profiler` 看 eager 三个 kernel 的时间分布，验证「每个 kernel 都接近 copy 带宽」的假设。
- 把 elementwise 链加长到 10 个算子，测融合收益是否线性；如果不线性，找出原因（寄存器/指令 cache/并行度）。
- 读 Roofline 模型，给你机器上的 matmul/conv 画一条 roofline，看看实测点离哪条上限更近。

## 动手练习

### 练习 1：算算术强度

对 `y = relu(x * 2 + 1)`（float32，N 个元素），分别计算「不融合」和「融合」的 bytes、FLOPs、算术强度，并解释为什么融合能快 2.5–3.5 倍。

<details>
<summary>参考思路</summary>

不融合：3 个 kernel，每个读 4N 字节、写 4N 字节，共 24N 字节；FLOPs=3N；AI=0.125。融合：读 4N、写 4N，共 8N 字节；AI=0.375。两者都是 memory-bound，时间比约 24N/8N=3。
</details>

### 练习 2：换 dtype

把本章的 elementwise 实验改成 float16（如果 CPU 支持），重新测 eager/TVM 的时间。带宽和计算时间怎么变？为什么？

<details>
<summary>参考思路</summary>

float16 每元素 2 字节，流量减半；如果仍 memory-bound，时间应接近减半。但 CPU 的 fp16 可能走软件模拟或转换，实际不一定更快。先测 `copy_` 的有效带宽，再判断瓶颈。
</details>

### 练习 3：给 diamond 选策略

`h = A @ W` 被 `sum` 和 `mul` 两个消费者使用。分别估算「materialize 一次」和「每个消费者各算一次」的成本，给出你的选择。

<details>
<summary>参考思路</summary>

materialize：一次 matmul + 写 h + 两个消费者各读 h；重复计算：两次 matmul + 不写 h。若 matmul 的 FLOPs 远大于 h 的读写（本章 512² 就是），materialize 更便宜。用实测：once 3.52 ms vs twice 8.22 ms。
</details>

### 练习 4：设计一个不该融合的 reduction

构造一个 reduction + elementwise 的场景，说明为什么融合会重复计算或降低并行度，并给出替代方案。

<details>
<summary>参考思路</summary>

例如 `s = x.sum(dim=1)`，然后 `y = x / s`（softmax 的一步）。如果想把 `sum` 融进逐元素除法，每个元素都要重新求和，变成 O(N²)；正确做法是先算 `s`（一次 reduction），再逐元素除。这就是 softmax 要分 stage 的原因。
</details>

## 本章重点整理

- 时间下限由 `max(FLOPs/算力, bytes/带宽)` 决定；算术强度是判断瓶颈的核心指标。
- elementwise/reduction 通常 memory-bound，matmul 通常 compute-bound，conv 取决于复用和实现。
- elementwise 融合最安全，收益来自「多次读写 → 一次读写」；本章实测在 2.5–3.5 倍量级。
- matmul 的 epilogue 融合很常见；matmul + matmul 需要特殊算法，不能无脑合。
- 融合的代价：重复计算、寄存器压力、并行度下降；高 AI 的 producer 被复制最贵。
- 本章 diamond 反例：重复一次 matmul，时间变成 2 倍以上（记录值 2.34 倍）。
- 性能数字必须带机器、样本数、波动范围；本机噪声大，配对测量比单次计时可靠。

## 自我检核

- [ ] 我能写出 elementwise/reduction/matmul/conv 的 FLOPs 和 bytes 估算
- [ ] 我能用算术强度判断一个算子是 memory-bound 还是 compute-bound
- [ ] 我能解释为什么 elementwise 融合通常能省 2/3 流量
- [ ] 我能说出 reduction 融合的三个限制（轴、依赖、并行度）
- [ ] 我能解释 matmul epilogue 为什么常见，而 matmul+matmul 为什么难
- [ ] 我能复现 diamond 反例，并用成本模型解释 2.34 倍
- [ ] 我知道寄存器压力需要 profiler，不能只靠 IR 判断
- [ ] 我给出的性能数字都带机器、样本数和波动范围

## 延伸阅读

- **Roofline: An Insightful Visual Performance Model**（https://dl.acm.org/doi/10.1145/1498765.1498785）：读第 2 节，理解 AI 与带宽/算力上限的关系。与本章关联：代价模型的理论基础。
- **TVM 调度教程**（https://tvm.apache.org/docs/tutorials/）：读 schedule 的 split/reorder/fuse 示例。与本章关联：融合在 TIR 层怎么落地。
- **PyTorch Profiler**（https://pytorch.org/docs/stable/profiler.html）：用 `torch.profiler` 看每个 kernel 的时间和内存。与本章关联：验证瓶颈。
- **TorchInductor 论文/设计文档**（https://dev-discuss.pytorch.org/）：搜索 fusion 的决策逻辑。与本章关联：生产级融合策略。
- **FlashAttention 论文**（https://arxiv.org/abs/2205.14135）：看 matmul+softmax 如何通过分块融合避免 materialize。与本章关联：复杂融合的边界。

---

→ [练习 A：FX 图解释器与数值对齐](./practice-a-fx-interpreter.md)