# Ch 11 循环优化：tiling、reorder、unroll、vectorize、parallel

> **目标**：把「同一个 matmul，为什么换个循环顺序就快十几倍」讲清楚；用 TVM 0.27 真跑 baseline、tiling、vectorize、unroll、parallel，拿到本机可复现的耗时，并知道每种优化的收益边界在哪里。
## 为什么需要这个？

图级优化回答「哪些算子可以合并、哪些常量可以提前算」；循环优化回答另一个问题：**同一个算子，循环到底怎么写**。

`C = A @ B` 在图上只是一个节点，但落到 CPU 上有无数种写法：

```c
for (i = 0; i < N; i++)
  for (j = 0; j < N; j++)
    for (k = 0; k < N; k++)
      C[i][j] += A[i][k] * B[k][j];
```

你可以按 `k` 切块、按 `i/j` 切块、把最内层换成 16 个 float 的 SIMD、把外层丢给 8 个线程、把 `k` 循环展开。语义完全一样，性能可以差一个数量级。

这一章的目标不是背 schedule 原语，而是建立三个判断：

1. **这条循环的瓶颈是什么**：cache、指令吞吐、依赖链，还是线程数？
2. **这个优化改了什么**：working set、ILP、SIMD 宽度，还是并行度？
3. **收益什么时候消失**：tile 太大、线程太多、向量太宽、unroll 把指令缓存撑爆。

> 证据约定：本章所有数字都是本机 `实测`；TVM 源码引用标 `源码依据`；没有 GPU 端到端结论，CUDA 相关内容在 Ch 14 单独处理。
## 先建立直觉：循环是一个可重排的迭代空间

把三重循环想成一个三维的迭代空间：`(i, j, k)` 每个点做一次乘加。循环顺序决定**你以什么顺序走过这个空间**，而 CPU 的 cache 只对「最近走过的地址」友好。

对 `C[i][j] += A[i][k] * B[k][j]`：

- 固定 `i`、`j`，沿 `k` 走：`A` 按行连续，`B` 按列跳 `N` 个 float。`B` 的每一次访问都可能是一次新的 cache line。
- 固定 `i`、`k`，沿 `j` 走：`A[i][k]` 不变，`B[k][j]` 连续，`C[i][j]` 连续。这是最友好的方向。
- 固定 `j`、`k`，沿 `i` 走：`A[i][k]` 跳行，`B[k][j]` 不变，`C[i][j]` 跳行。

所以「reorder」不是 cosmetic：它直接改变每条 cache line 被用了几次。tiling 则是在这个空间里切出一个小立方体，让一个小立方体的 A/B 数据能留在 L1/L2 里被反复使用。
## 核心概念一：局部性不是玄学，是 working set

一个 k-block 的 working set 可以粗略写成：

```text
(BM + BN) * BK * 4 bytes
```

其中 `BM`、`BN` 是输出 tile 的行/列，`BK` 是 k 方向的块大小，float32 每个 4 字节。

本机 cache 规模（Windows `Win32_CacheMemory` 报告的总量）：

| 层级 | 容量 | 说明 |
|---|---:|---|
| L1 | 256 KB | 4 个核心共享报告值，单核更小 |
| L2 | 1 MB | 每核约 256 KB |
| L3 | 8 MB | 全芯片共享 |

如果 `BM = BN = 256`、`BK = 8`，working set 是 `(256+256)*8*4 = 16 KB`，稳稳落在 L1。把 `BK` 加到 128，就是 256 KB，已经顶到 L1；加到 256，就是 512 KB，必须去 L2/L3 取。

这就是 tiling 的第一条边界：**tile 不是越大越好，关键是「每个 k-block 的 A/B 足迹」是否还装得下**。
## 核心概念二：三层并行，别混为一谈

循环优化经常把三件不同的事混在一起说：

| 层次 | 手段 | 主要收益 | 典型边界 |
|---|---|---|---|
| 指令级并行 ILP | unroll、重排独立累加 | 填满流水线、隐藏延迟 | 依赖链、寄存器压力 |
| 数据级并行 SIMD | vectorize | 一条指令算 4/8/16 个 float | 对齐、tail、频率下降 |
| 线程级并行 | parallel | 用多核 | 物理核数、内存带宽、调度开销 |

本机是 Intel i7-10510U：4 个物理核、8 个逻辑线程，支持 AVX2（256-bit，8 个 float），没有 AVX-512。这个事实后面会反复用到：

- `vectorize(16)` 在 LLVM IR 里是 `<16 x float>`，但后端通常必须把它 legalize 成两条 256-bit 操作；所以 16 不会比 8 快一倍。
- `parallel(8)` 拿到的是 8 个逻辑线程，不是 8 个物理核；浮点 matmul 又受内存带宽限制，所以 8 线程很难线性加速。
## 核心概念三：调度只改「怎么算」，不改「算什么」

TVM 的 schedule 原语都遵守同一条契约：**变换后的 IR 必须和原 IR 数值等价**。这也是为什么每次实验都要做数值对齐：

```python
maxerr = np.abs(tc.numpy() - a @ b).max()
```

本机所有变体的 `maxerr` 都是 `7.63e-05`（N=256，float32）。这不是「误差可以忽略」，而是「不同累加顺序带来的正常浮点误差」；如果某个 schedule 把误差推到 `1e-2`，那不是优化，是编译错了。
## 底层机制：TVM 0.27 里一次 schedule 如何落到 TIR

TVM 0.27 的入口和旧教程不同。旧写法是 `te.create_schedule(C.op)`，0.27 改成「先建 PrimFunc，再建 Schedule」：

```python
import numpy as np
import tvm
from tvm import te, s_tir

N = 256
A = te.placeholder((N, N), name="A", dtype="float32")
B = te.placeholder((N, N), name="B", dtype="float32")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

func = te.create_prim_func([A, B, C])   # TE -> 可调度的 TIR PrimFunc
sch = s_tir.Schedule(func)              # 0.27：Schedule 接收 PrimFunc/IRModule
blk = sch.get_sblock("C")               # 0.27：get_sblock，不是 get_block
i, j, kk = sch.get_loops(blk)           # 0.27：get_loops 要传 block
```

`sch` 内部维护一个 IRModule；每次 `split/reorder/vectorize/...` 都会改写 `sch.mod`。所以调试 schedule 最有效的方法不是猜，而是每一步都打印：

```python
print(sch.mod.script())
```

`script()` 是 TVM 自己的 TIR 文本格式，能看到循环、block、axis、buffer 访问。Ch 12 会逐行拆这个文本；本章只看循环结构。
## 示例一：先跑一个诚实的 baseline

baseline 就是不做任何 schedule。下面这段可以直接复制运行：

```python
import numpy as np
import tvm
from tvm import te, s_tir

N = 256
A = te.placeholder((N, N), name="A", dtype="float32")
B = te.placeholder((N, N), name="B", dtype="float32")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

func = te.create_prim_func([A, B, C])
sch = s_tir.Schedule(func)
f = tvm.build(sch.mod, target="llvm")

rng = np.random.default_rng(0)
a = rng.random((N, N), dtype=np.float32)
b = rng.random((N, N), dtype=np.float32)
ta = tvm.runtime.tensor(a, tvm.cpu())
tb = tvm.runtime.tensor(b, tvm.cpu())
tc = tvm.runtime.empty((N, N), "float32", tvm.cpu())

f(ta, tb, tc)
print("maxerr:", np.abs(tc.numpy() - a @ b).max())

ft = f.time_evaluator(f.entry_name, tvm.cpu(), number=10, repeat=3)
r = ft(ta, tb, tc)
print("median ms:", r.median * 1e3, "min ms:", r.min * 1e3)
```

本机一次典型输出：

```text
maxerr: 7.6293945e-05
median ms: 38.054
min ms: 36.809
```

注意这里的时间是 `time_evaluator` 的 `median`，单位是秒，乘以 `1e3` 才是毫秒。`number=10` 表示每个 repeat 里连续跑 10 次，`repeat=3` 表示外层重复 3 次；`median` 是 3 个 repeat 的中位数。`number` 太小会被单次抖动带偏，`repeat` 太少会被 CPU 频率带偏。

### 本机测量条件

| 项 | 值 |
|---|---|
| CPU | Intel Core i7-10510U，4C/8T，1.8 GHz 基频 |
| 内存 | 约 40 GB |
| OS | Windows 10/11 x86_64 |
| Python | 3.11.12 |
| TVM | 0.27.0.post1，LLVM backend |
| 形状 | N=256，float32 |
| 计时 | `number=10, repeat=3`，5 轮，轮间交替顺序，取每轮中位数的中位数 |

**绝对时间不可跨机器比较，也不可跨时段比较**。同一台机器在冷启动、插电、后台负载不同时，baseline 可以在 29–55 ms 之间漂。下面所有表都同时给「中位数」和「同轮 baseline 比值」；比值比绝对值可靠。
## 示例二：k tiling 的真相

最直觉的优化是按 `k` 切块：

```python
sch = s_tir.Schedule(te.create_prim_func([A, B, C]))
i, j, kk = sch.get_loops(sch.get_sblock("C"))
ko, ki = sch.split(kk, factors=[16, None])  # k = ko*16 + ki
sch.reorder(i, j, ko, ki)
```

`split` 把 `k` 拆成外层 `ko` 和内层 `ki`，`reorder` 把 `k` 的两个循环放到 `i,j` 里面。预期是：固定 `i,j` 后，`ki` 只走 16 个元素，A 的一段和 B 的一段能留在 L1。

下面四张表来自不同批次，**只比较表内比值**；跨表的绝对时间会因 CPU 频率漂移而不同。

本机实测（5 轮，同轮比值）：

| 变体 | 中位数 (ms) | 范围 (ms) | 同轮 baseline 比值 |
|---|---:|---:|---:|
| baseline | 47.11 | 42.91–55.40 | 1.00 |
| k8 | 50.53 | 44.89–54.57 | 0.99–1.16 |
| k16 | 48.76 | 44.51–54.88 | 0.87–1.16 |
| k32 | 48.92 | 46.71–52.97 | 0.90–1.23 |
| k64 | 48.84 | 38.04–56.06 | 0.69–1.31 |
| k128 | 50.61 | 38.57–52.07 | 0.70–1.18 |
| k256 | 48.05 | 38.30–54.43 | 0.69–1.16 |

结论可能和旧教程相反：**在这台机器、这个标量 matmul 上，单独按 k 切块没有可靠收益**。比值在 0.69–1.31 之间跳，中位数基本是 1.0。

为什么？因为 baseline 的瓶颈不是「B 的 cache line 没命中」这么单一。`C[i][j] += A[i][k] * B[k][j]` 有一个**循环携带依赖**：下一次加法的输入依赖上一次写回的 `C[i][j]`。标量代码里，这个依赖链的延迟（load-add-store，约 4–5 个周期）比 cache 命中与否更致命。k tiling 只改了循环边界，没有打破依赖链，也没有生成 SIMD；它当然可能没用。

这不是说 tiling 是错的，而是说：**tiling 的收益要和其他优化一起看**。当你把内层变成 16 路 SIMD，依赖链被 16 个独立输出摊开，tiling 才有意义。
## 示例三：2D tiling + vectorize + parallel

把 `i,j` 也切块，再把 `j` 的最内层向量化：

```python
def schedule_tiled(sch, bk=8, vj=16, par=8):
    i, j, kk = sch.get_loops(sch.get_sblock("C"))
    jo, ji = sch.split(j, factors=[None, vj])   # j = jo*vj + ji
    ko, ki = sch.split(kk, factors=[None, bk])  # k = ko*bk + ki
    io, ii = sch.split(i, factors=[par, None])  # i = io*par + ii
    sch.reorder(io, jo, ko, ii, ki, ji)
    sch.parallel(io)
    sch.vectorize(ji)
    sch.unroll(ki)
```

这个循环顺序的意思是：外层 `io` 交给线程；每个线程负责 `ii` 行；对每个 `j` tile、每个 `k` block，先展开 `ki`，再向量化 `ji`。`ii` 在 `ko` 里面，是为了让同一个线程在固定 `j` tile 下反复使用 B 的那一小块。

本机实测（N=256，同一批 5 轮，交替顺序）：

| 变体 | 中位数 (ms) | 范围 (ms) | 同轮 baseline 比值 |
|---|---:|---:|---:|
| baseline | 38.05 | 36.81–40.50 | 1.00 |
| k16（仅 k tile） | 39.82 | 36.00–42.79 | 0.96–1.16 |
| ij32 + k8 | 29.99 | 26.61–31.07 | 0.66–0.82 |
| ij64 + k8 | 33.05 | 24.15–36.63 | 0.61–0.97 |
| ij128 + k8 | 27.85 | 26.17–36.21 | 0.65–0.96 |
| vec16（vectorize j，unroll k8） | 3.78 | 3.67–4.30 | 0.09–0.11 |
| par8（仅并行 i） | 10.32 | 9.93–11.72 | 0.25–0.31 |
| par8 + vec8 | 2.15 | 1.98–2.32 | 0.05–0.06 |

三个层次各拿走了不同的收益：

- **2D tiling + k8**：约 0.66–0.82× baseline，也就是快 1.2–1.5 倍。它改善的是 B tile 的复用和循环开销，但仍然是标量代码。
- **vectorize**：约 0.10× baseline，快约 10 倍。这是数量级变化，因为它把 16 个独立的 `C[i][j]` 放进了 SIMD 通道，打破了标量依赖链。
- **parallel(8)**：约 0.27× baseline，快约 3.7 倍。8 个逻辑线程只有 4 个物理核，且 matmul 受内存带宽限制，不可能 8 倍。
- **parallel + vectorize**：约 0.06× baseline，快约 16 倍。两个优化叠乘，但离 4×10=40 倍还很远，原因同上：带宽和线程开销。
## 示例四：vector width、unroll 与「tile 太大」

### vector width

固定 `k=8`、`unroll(ki)`，只改 `vj`：

| 变体 | 中位数 (ms) | 同轮 baseline 比值 | 说明 |
|---|---:|---:|---|
| baseline | 42.63 | 1.00 | 标量 |
| v4 | 10.00 | 0.21–0.26 | 128-bit |
| v8 | 6.41 | 0.12–0.16 | 256-bit，一个 AVX2 寄存器 |
| v16 | 4.76 | 0.10–0.11 | 两个 AVX2 寄存器 |
| v32 | 4.72 | 0.10–0.12 | 四个 AVX2 寄存器，收益饱和 |
| v16，不 unroll | 3.76 | 0.07–0.10 | 本批差 21%，复核落在噪声内（见下） |

`v8 → v16` 还有约 1.3 倍收益，`v16 → v32` 基本没有。原因不是「16 是魔法数」，而是：

1. LLVM IR 里 `v16` 是 `<16 x float>`；i7-10510U 只有 AVX2，后端必须把它拆成两条 256-bit 操作。
2. `v8` 一条 AVX2 就能算完，但循环体里的 load/fma/store 仍有延迟；`v16` 给编译器更多独立指令去填流水线。
3. `v32` 继续加宽只会增加寄存器压力和代码体积，硬件执行宽度没变。

### unroll

`unroll(ki)` 展开的是 reduction 方向的 8 次迭代。它能让编译器把 8 次乘加排成更紧的指令序列，但 `C[i][j]` 的累加依赖仍然存在；所以 unroll 的收益小，而且可能被代码体积抵消。

本机原始那批数据里，`v16` 加 unroll 是 4.76 ms、不加是 3.76 ms（差 21%）。但 21% 已经接近本机噪声，所以我做了独立复核：6 轮、`number=20, repeat=5`、交替顺序，只比同轮比值。结果是 `v16` 加/不加 unroll 的同轮中位比值 **1.04**（单轮 0.91–1.14），`v8` 是 **0.97**（0.88–1.03）。

**结论：在这台机器、这个 matmul 上，reduction unroll 的效应落在噪声内**——既不保证收益，也不必然变慢。理论解释仍然成立：`C[i][j]` 的累加依赖让展开后的 8 次乘加无法真正并行，所以别指望它像 vectorize 那样带来数量级收益。要判断 unroll 有没有用，必须像这样同批交替 A/B；跨批次比绝对值只会看到 CPU 频率漂移。

### tile 太大：理论上的悬崖与实测的平地

按前面的公式，`BM=BN=256` 时：

| BK | working set | 预期 |
|---:|---:|---|
| 8 | 16 KB | L1 |
| 16 | 32 KB | L1 |
| 32 | 64 KB | L1/L2 边界 |
| 64 | 128 KB | L2 |
| 128 | 256 KB | L1 容量上限 |
| 256 | 512 KB | 必须去 L2/L3 |

但实测（vectorize 16，unroll ki）：

| BK | 中位数 (ms) | 编译时间 (s) |
|---:|---:|---:|
| 8 | 4.12 | 0.13 |
| 16 | 4.11 | 0.11 |
| 32 | 3.60 | 0.15 |
| 64 | 3.47 | 0.23 |
| 128 | 3.49 | 0.39 |
| 256 | 3.89 | 0.62 |
| 256，不 unroll | 3.42 | 0.10 |

**没有出现教科书式的悬崖**。BK 从 8 到 128 基本是平的，256 只慢约 11%；真正明显的是编译时间从 0.11 s 涨到 0.62 s（约 5.6 倍），以及代码体积。

这给我们一个更准确的结论：

- 「tile 太大导致寄存器/缓存抖动」是**机制上真实存在**的风险，但它是否发生，取决于循环顺序、向量化方式、硬件预取和编译器。
- 在这台 CPU、这个 matmul 上，`BK=256` 还没有大到让性能崩掉；它先让编译变慢、代码变大。
- 真正危险的是**同时**把 `BM/BN/BK` 都放大、又展开很多层，让寄存器和指令缓存一起爆。测量时不要只看运行时间，也记录编译时间和生成代码大小。
## 对比与取舍

| 优化 | 本机收益（N=256） | 主要机制 | 什么时候失效 |
|---|---:|---|---|
| k tiling（单独） | ~1.0× | 改善 B 的 cache 复用 | 标量依赖链是瓶颈；tile 太小/太大 |
| 2D tiling + k block | 1.2–1.5× | A/B tile 复用、减少循环开销 | tile 足迹超过 L1/L2；并行度被切碎 |
| vectorize | ~10× | SIMD 打破标量依赖链 | 宽度超过硬件、tail 不对齐 |
| unroll | 0.9–1.1× | 填流水线、减少循环开销 | 依赖链、寄存器压力、指令缓存 |
| parallel | ~3.7× | 多核 | 物理核数、带宽、线程开销 |
| parallel + vectorize | ~16× | 多核 × SIMD | 带宽、NUMA、线程 oversubscription |

一句话：**先找瓶颈，再选优化**。如果代码是标量依赖链受限，先 vectorize；如果已经向量化但只有单核在跑，再 parallel；tiling 是让前两者更有效的「配套」，不是单独的银弹。
## 踩雷集锦

1. **照抄旧教程的 `te.create_schedule(C.op)`**：0.27 没有这个函数。正确入口是 `te.create_prim_func([A,B,C])` + `s_tir.Schedule(func)`。
2. **`sch.get_block("C")`**：0.27 叫 `get_sblock`。旧名字直接 `AttributeError`。
3. **`sch.get_loops(func)`**：`get_loops` 要的是 block（`SBlockRV`）或 block 名字，不是 PrimFunc。先 `get_sblock("C")`。
4. **把 `split` 的 factors 顺序写反**：`factors=[16, None]` 是外层 16、内层 `N/16`；`factors=[None, 16]` 是外层 `N/16`、内层 16。tiling 通常要后者（内层是 tile）。
5. **用 `parallel` 并行一个带 reduction 的循环**：如果多个线程同时写同一个 `C[i][j]`，结果会错。并行要并的是输出空间（`i` 或 `i,j`），不是 reduction 的 `k`。
6. **`vectorize` 一个不能整除的 loop**：N 不是向量宽度的倍数时，TVM 会加 predicate 或退化；性能可能骤降（Ch 13 的 N=257 例子）。
7. **只看一次 `time_evaluator`**：本机 baseline 在 29–55 ms 之间漂；一次测量可能把 1.2 倍看成 0.9 倍。至少 3–5 轮，交替顺序，看比值。
8. **把 `maxerr` 当成「越小越好」**：float32 matmul 的 `7.6e-05` 是正常累加误差；调度改变的只是加法顺序，不应该追求 0。
## 进阶：再往深一层

- **看 LLVM IR**：`f.inspect_source()` 能看到 `<16 x float>`、`align 64`、`fadd <16 x float>`。确认 vectorize 真的生成了向量指令，而不是被后端拆回标量。Ch 14 会贴完整片段。
- **看 TIR**：`sch.mod.script()` 能看到 `T.vectorized(16)`、`T.unroll(8)`、`T.parallel(8)`。如果这些标记没出现，说明 schedule 没生效。
- **测不同 N**：baseline 在 N=256→384→512 时会超线性变慢（38→110→559 ms），而调好的 schedule 接近线性（1.4→3.4→10.1 ms）。这是 cache 层级最直观的证据，Ch 13 会给完整表。
- **测线程数**：把 `parallel` 的 factor 从 2 改到 8，观察是否在 4 线程后饱和；这比「线程越多越好」更有信息量。
- **测对齐**：把输入数组偏移一个 float（`a[1:]` 这种 view），看 vectorize 是否因为非对齐而变慢；这是 Ch 15 的主题。
## 动手练习

### 练习 1：复现 vector width 曲线

**任务**：用同一个 schedule，把 `vj` 设为 4、8、16、32，各跑 3 轮 `number=10, repeat=3`，记录中位数和同轮 baseline 比值。

**测试用例**：N=256，float32，`maxerr` 必须小于 `1e-4`；每个变体的 `maxerr` 应与 baseline 一致（同一累加顺序下）。

**步骤**：

1. 复制本章的 `schedule_tiled`，把 `vj` 参数化。
2. 固定 `bk=8`、`par=1`，只改 `vj`。
3. 每轮先跑 baseline，再跑四个变体；轮间倒序。
4. 把结果写成 `vj,median_ms,ratio` 三列。

<details>
<summary>参考解答</summary>

```python
import statistics
import numpy as np
import tvm
from tvm import te, s_tir

N = 256
A = te.placeholder((N, N), name="A", dtype="float32")
B = te.placeholder((N, N), name="B", dtype="float32")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
rng = np.random.default_rng(0)
a = rng.random((N, N), dtype=np.float32)
b = rng.random((N, N), dtype=np.float32)
ta = tvm.runtime.tensor(a, tvm.cpu())
tb = tvm.runtime.tensor(b, tvm.cpu())
tc = tvm.runtime.empty((N, N), "float32", tvm.cpu())

def build(vj):
    sch = s_tir.Schedule(te.create_prim_func([A, B, C]))
    i, j, kk = sch.get_loops(sch.get_sblock("C"))
    jo, ji = sch.split(j, factors=[None, vj])
    ko, ki = sch.split(kk, factors=[None, 8])
    sch.reorder(i, jo, ko, ki, ji)
    sch.vectorize(ji)
    sch.unroll(ki)
    return tvm.build(sch.mod, target="llvm")

funcs = {"base": tvm.build(te.create_prim_func([A, B, C]), target="llvm")}
funcs.update({f"v{vj}": build(vj) for vj in (4, 8, 16, 32)})
res = {name: [] for name in funcs}
for rnd in range(3):
    order = list(funcs) if rnd % 2 == 0 else list(reversed(funcs))
    for name in order:
        f = funcs[name]
        f(ta, tb, tc)
        r = f.time_evaluator(f.entry_name, tvm.cpu(), number=10, repeat=3)(ta, tb, tc)
        res[name].append(r.median * 1e3)
for name, xs in res.items():
    print(name, statistics.median(xs), xs)
```

预期趋势：v4 最慢，v8 明显快，v16 略快于 v8，v32 与 v16 接近。绝对值会漂，趋势不会。

</details>

**常见卡点**：`vj` 必须整除 N（256 可以）；如果 N=250，`vj=16` 会产生 tail，趋势会被 predication 打乱。

### 练习 2：parallel 的 scaling

**任务**：固定 `vj=8`、`bk=8`，把 `par` 设为 1、2、4、8，测四组时间，算相对 `par=1` 的加速比。

**测试用例**：结果必须与 baseline 数值一致；`maxerr < 1e-4`。

**步骤**：

1. 在 `schedule_tiled` 里让 `par` 控制 `io` 的 extent。
2. `par=1` 时不要调用 `parallel`，否则 TVM 会生成一个单线程的并行循环。
3. 记录每组的线程数、时间、加速比。
4. 解释为什么 8 线程拿不到 8 倍。

<details>
<summary>参考解答</summary>

关键代码：

```python
if par > 1:
    io, ii = sch.split(i, factors=[par, None])
    sch.reorder(io, jo, ko, ii, ki, ji)
    sch.parallel(io)
else:
    sch.reorder(jo, ko, i, ki, ji)
```

本机参考：`par=1` 约 4.5 ms，`par=4` 约 1.3 ms，`par=8` 约 1.2–1.7 ms。8 线程相比 4 线程没有明显收益，因为只有 4 个物理核，且 matmul 在内层向量化后已经接近内存带宽上限。

</details>

**常见卡点**：`parallel` 的循环必须是输出空间；如果并行 reduction 的 `k`，多个线程会写同一个 `C[i][j]`，结果错误。

### 练习 3：unroll 输出循环 vs reduction 循环

**任务**：写两个 schedule：A 展开 `ki`（reduction），B 展开 `ji`（输出）。比较两者的时间和 TIR。

**测试用例**：`maxerr < 1e-4`；打印 `sch.mod.script()`，确认 `T.unroll` 出现的位置不同。

**步骤**：

1. schedule A：`sch.unroll(ki)`。
2. schedule B：`jo, ji = split(j, [None, 8])` 之后直接 `sch.unroll(ji)`，不要 vectorize。
3. 比较两者的循环结构和时间。
4. 解释哪个更容易暴露 ILP。

<details>
<summary>参考解答</summary>

reduction 的 `ki` 展开后，8 次加法仍然依赖同一个 `C[i][j]`，只是把循环变成了 8 条顺序指令；输出的 `ji` 展开后，8 个不同的 `C[i][j]` 可以交错累加，依赖链更短。后者通常更容易填满流水线。

```python
# A: reduction unroll
sch.unroll(ki)

# B: output unroll（不 vectorize）
jo, ji = sch.split(j, factors=[None, 8])
sch.unroll(ji)
```

注意：B 不 vectorize，所以绝对性能不一定超过 A 的 vectorize 版本；这个练习的目的是看「独立性」而不是追求最快。

</details>

**常见卡点**：`split` 之后的 RV 顺序容易搞混；每次都打印 `script()`，看 `T.unroll` 到底落在哪个循环上。

### 练习 4：制造一个坏 schedule

**任务**：把 `reorder` 改成 `reorder(kk, i, j)`（k  outermost），或者把 `bk` 设为 256 且 `unroll` 全部展开，测时间并解释。

**测试用例**：数值仍然正确（`maxerr < 1e-4`），但性能应明显差于合理 schedule；记录差多少。

**步骤**：

1. 复制本章 baseline，先 `split` k，再 `reorder(ko, ki, i, j)`。
2. 跑 `number=10, repeat=3`。
3. 打印 TIR，确认 k 在最外层。
4. 解释为什么 B 的访问模式变差。

<details>
<summary>参考解答</summary>

k 在最外层时，对每个 k，整个 `C` 都要被扫一遍；`C[i][j]` 的写回在 k 维度上反复横跳，A/B 的 cache line 也不能在短时间窗口内复用。这个 schedule 通常比 baseline 还慢，或者至少没有收益。

```python
ko, ki = sch.split(kk, factors=[16, None])
sch.reorder(ko, ki, i, j)
```

</details>

**常见卡点**：`reorder` 只检查合法性，不检查性能；合法但慢的 schedule 很常见。
## 本章重点整理

- 循环优化改的是**迭代顺序和并行方式**，不改变数学语义；每次都要做数值对齐。
- tiling 的核心是 working set：`(BM+BN)*BK*4` 要装得进 L1/L2；tile 太大可能抖动，但在本机上先体现为编译变慢。
- 单独的标量 k tiling 不一定有收益；本机中位数比值约 1.0，因为瓶颈是标量依赖链，不是单纯的 cache。
- vectorize 是数量级优化：本机 v16 约 0.10× baseline；宽度超过硬件会饱和。
- unroll 要作用在有独立性的循环上；本机 reduction unroll 的同轮比值在 0.97–1.04 之间，收益落在噪声内。
- parallel 受物理核数和带宽限制：本机 8 线程约 3.7×，不是 8×。
- 组合优化（parallel + vectorize）能到约 16×，但仍然受带宽约束。
- 计时必须多轮、交替顺序、看比值；本机 baseline 在 29–55 ms 之间漂是正常的。
## 自我检核

- [ ] 我能写出 TVM 0.27 的 `create_prim_func` + `s_tir.Schedule` 最小例子
- [ ] 我能解释 `split(k, factors=[16, None])` 和 `[None, 16]` 的区别
- [ ] 我能说出 tiling 的 working set 公式，并估算 256×256、BK=8 的足迹
- [ ] 我能解释为什么本机的标量 k tiling 没有可靠收益
- [ ] 我能解释 vectorize 为什么能打破标量依赖链
- [ ] 我知道 i7-10510U 只有 AVX2，`<16 x float>` 会被拆成两条 256-bit 操作
- [ ] 我能设计一个 3–5 轮的计时实验，避免把频率漂移当成优化收益
- [ ] 我能说出至少三个「照抄旧教程会炸」的 TVM API
## 延伸阅读

- **TVM Schedule Primitives 教程**（https://tvm.apache.org/docs/tutorials/language/schedule_primitives.html）：读 `split`、`reorder`、`vectorize`、`parallel` 四节；注意对照 0.27 的 `get_sblock`/`get_loops`。与本课关联：本章所有原语的官方版本。
- **TVM 论文 (OSDI 2018)**（https://www.usenix.org/conference/osdi18/presentation/chen）：读第 3–4 节，理解「计算与调度分离」为什么能覆盖不同硬件。与本课关联：tiling/vectorize 的设计动机。
- **Ansor 论文 (OSDI 2020)**（https://www.usenix.org/conference/osdi20/presentation/zheng）：读搜索空间和 cost model 部分。与本课关联：Ch 13 的自动调优。
- **Agner Fog: Optimizing subroutines in assembly language**（https://www.agner.org/optimize/）：读 cache、SIMD、instruction scheduling 三章。与本课关联：解释为什么 unroll 和 vector width 有边界。
- **Intel 64 and IA-32 Architectures Optimization Manual**（https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html）：查 AVX2 宽度、L1/L2 延迟、prefetch 行为。与本课关联：本机 i7-10510U 的硬件边界。

---

→ [Ch 12 TVM TIR 与 schedule：0.27 的新 API](./12-tvm-tir-schedule.md)