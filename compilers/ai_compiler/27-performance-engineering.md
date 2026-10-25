# Ch 27 性能工程：profiling、瓶颈定位与基准回归

> **目标**：把「快了多少」变成可复现的数字。会写带 warmup / repeat / 分位数的 harness，会用 cProfile 和 torch.profiler 分层定位，会用 Roofline 判断 memory-bound 还是 compute-bound，并能识破「花时间做算子融合，瓶颈其实在 Python 调用」这类方向性错误。

## 为什么需要这个？

先看三个本机实测的数字，它们都是「什么都没改」：

- 同一个 1M 元素加法，第一次测 `median=1.437 ms`，隔几分钟再测 `median=0.914 ms`——差 1.57×。
- 同一个 kernel 连续测 10 个 session，median 在 `0.680` 到 `1.109 ms` 之间（1.63×），min 在 `0.705` 到 `0.932 ms` 之间（1.32×）。
- 一次没 warmup 的连续采样：`1.055, 1.023, 0.950, 1.611, 3.430, 2.358, 1.184, 1.091 ms`。同一份代码，最快的和最慢的差 3.6×。

如果你只跑一次、只报一个数，你报的是**噪声**，不是性能。这一章解决的就是「怎么让测出来的数字能用于决策」。

## 先建立直觉：性能问题分三层，工具也分三层

```text
第 3 层  算法/图      融合哪些算子、要不要 tiling、算术强度      -> Roofline、读写量估算
第 2 层  kernel       单个 kernel 的执行时间、带宽、FLOPs        -> torch.profiler、time_evaluator
第 1 层  调用/调度    每次算子调用的固定开销、Python 循环         -> cProfile、perf_counter 分段
```

| 层 | 单次成本量级 | 典型症状 | 用什么测 |
|---|---|---|---|
| 调用/调度 | 1–20 µs / op | 算子很多、每个都很小、GPU/CPU 利用率低 | `cProfile`、手动分段计时 |
| kernel | 10 µs – 100 ms | 单个 kernel 慢，profile 里某一行占比高 | `torch.profiler`、`f.time_evaluator` |
| 算法/图 | — | 优化了融合/向量化，端到端没变 | Roofline、字节数与 FLOPs 估算 |

**先判层，再选优化。** 这一章后半段会给出两个真实案例：一个真正的问题是第 1 层（Python 调用开销），另一个看似是第 1 层、其实在第 2 层（GEMM）。

## 核心概念一：benchmark harness 的五个要素

### 1. warmup：第一次调用永远最慢

```text
冷启动第一次: 1.770 ms
稳态（warmup 后）: 0.92 ms
```

差 1.9×。原因包括：内存分配器首次建池、线程池唤醒、算子库首次选择算法（oneDNN/MKL 的 heuristic 缓存）、CPU 频率从 idle 爬升。**warmup 不是可选项**，通常 5–20 次就够。

### 2. repeat：一次测量没有分布

上面 8 次采样里最小 `0.950`、最大 `3.430`。取一次等于随机抽一个样本。至少 50 次，想要 p99 就 200 次以上。

### 3. 隔离：把变量按住

- 固定线程数（`torch.set_num_threads(4)`），否则线程池大小随负载变。
- 固定输入（同一个 tensor，别在计时循环里 `randn`）。
- 不在计时循环里做 `print` / `.item()` / 转 numpy——这些会同步并计入时间。
- 关掉占 CPU 的其它进程。本机做实验时的漂移主要来自后台负载和频率调节。

### 4. 统计量：报 median + p25/p75 + min，不报 mean

一个真实的分布：

```text
add 1M (repeat=50)：median=1.437  p25=1.085  p75=2.739  min=0.922  max=10.313 (ms)
IQR / median = 115%
```

IQR 是 median 的 1.15 倍，说明分布严重右偏（偶发的长尾）。这种情况下 `mean` 会被长尾拖走；`median` 描述「典型情况」，`min` 描述「这台机器最好的时候」，`p75` 描述「坏的时候有多坏」。**三个一起报，读者才判断得出差异是不是真的。**

### 5. 交错 A/B：对抗时间漂移

如果先测 A 再测 B，两次之间机器状态变了，差异会混进「A vs B」的结论里。正确做法是同一轮里交替测：

```python
ratios = []
for _ in range(6):
    a = once(fn_a); b = once(fn_b)   # 紧挨着测，共享同一时刻的机器状态
    ratios.append(a / b)
```

本机实测：A 的 sequential 比值是 `2.16×`，交错后逐轮比值 median 是 `2.52×`。两个都指向「A 慢一倍多」，但交错法的每轮比值更稳（1.42–2.86），因为它消掉了慢漂移。

## 核心概念二：profile 先分层，再看 hotspot

两个 profiler 看的东西不一样：

| | `cProfile` | `torch.profiler` |
|---|---|---|
| 视角 | Python 函数帧 | aten 算子 / kernel |
| 能看到 | `forward`、`_call_impl`、Python 循环 | `aten::addmm`、`aten::bmm`、`aten::copy_` |
| 看不到 | 算子里面的时间（都被记在 `torch._C._nn.linear` 上） | Python 层的调用栈 |
| 开销 | 明显（本机 +23%） | 中等 |

本机实测 cProfile 的开销：同一模型 `9.692 ms/iter` → `11.895 ms/iter`，慢 23%。**profile 里的绝对时间不能直接当性能数字用**，它只用来定位「谁占大头」，最终数字要回到不带 profiler 的 harness 上取。

## 核心概念三：Roofline 判断 bound

Roofline 的核心是一个比值：**算术强度（arithmetic intensity, AI）**

```text
AI = FLOPs / Bytes        # 单位：flop/byte
```

再算机器的**拐点（ridge point）**：

```text
ridge = 峰值算力(FLOP/s) / 峰值带宽(Byte/s)
```

- `AI < ridge`：**memory-bound**，优化方向是少读写（融合、复用、量化）。
- `AI > ridge`：**compute-bound**，优化方向是少算（更好的算法、更宽的 SIMD、更强的指令）。

三个算子的 AI（Ch 16 的公式，这里直接用）：

| 算子 | FLOPs | Bytes | AI (flop/byte) | 判定 |
|---|---|---|---|---|
| `y = x + w`（N 元素 float32） | N | 12N | 0.083 | 极度 memory-bound |
| `C = A @ B`（N×N） | 2N³ | 12N² | N/6（N=512 → 85.3） | compute-bound |
| 64 元素小算子 | 64 | 768 | 0.083 | 名义 memory-bound，实际 overhead-bound |

**注意第三行**：AI 说它 memory-bound，但 Roofline 预测它只要 0.044 µs，实测 4.68 µs——差 106 倍。Roofline 只描述「带宽/算力上限」，不描述「固定开销下限」。当 kernel 小到固定开销占主导，Roofline 就失效了，这一层要回到第 1 层工具去测。

## 底层机制：64 个元素的算子为什么要 4.7 微秒

实测同一算子（`torch.mul(x, k, out=o)`）在不同尺寸下的单次成本：

```text
64 个元素   : 4.68 us/op
1024 个元素 : 4.38 us/op
1M 个元素   : 702.47 us/op
```

64 和 1024 差了 16 倍数据量，时间几乎一样。说明这段时间里几乎没在搬数据，而是在做**和尺寸无关的固定工作**：

1. Python → C++ 的调用与参数解析；
2. TensorIterator 的构造：推断 shape、stride、dtype 提升、分配迭代器；
3. 线程池唤醒 / 任务切分（本机 4 线程，小 tensor 也会走一遍）；
4. 如果没给 `out=`，还有一次分配。

按 roofline 算，64 元素要搬 768 字节，在 17.5 GB/s 下是 `0.044 µs`。实测 `4.68 µs` 里，**99% 不是带宽**。所以有效带宽只有 `768 B / 4.68 µs = 0.164 GB/s`，是峰值带宽的 1/107。

这条曲线在 1M 处交叉：`12.58 MB / 702 µs ≈ 17.9 GB/s`，正好是峰值带宽——**只有在工作集足够大时，kernel 才真的在做内存传输**。

这就是「融合 vs 调用开销」的物理基础：一个 64 元素的算子，融合掉另一个 64 元素算子，省下的是 4.7 µs 的固定开销里的一小部分；而把这些小算子合并成一个大 kernel，省下的是成百上千次固定开销。

## 示例一：一个能复现的 harness

```python
# .scratch\ai-compiler\ch27\exp1_harness.py
import time, statistics, torch

def bench(fn, warmup=10, repeat=100):
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1e3)      # 秒 -> 毫秒
    ts.sort()
    q = statistics.quantiles(ts, n=4)
    return {"n": repeat, "median": statistics.median(ts),
            "p25": q[0], "p75": q[2], "min": ts[0], "max": ts[-1]}

N = 1 << 20
a, b, out = torch.randn(N), torch.randn(N), torch.empty(N)
r = bench(lambda: torch.add(a, b, out=out), repeat=200)
print(f"n={r['n']} median={r['median']:.4f} p25={r['p25']:.4f} "
      f"p75={r['p75']:.4f} min={r['min']:.4f} max={r['max']:.4f}")
```

实测输出：

```text
n=200 median=0.9140 p25=0.7149 p75=1.4982 min=0.5246 max=2.8700
```

`max/min` 是 5.5×，`p75/p25` 是 2.1×——在这台机器上，**小于 1.5× 的差异不要下结论**。带宽换算也随统计量变：median 对应 `13.8 GB/s`，min 对应 `24.0 GB/s`。写报告时两个都值得给，但要标清楚是哪一个。

## 示例二：cProfile 和 torch.profiler 各看一层

模型：`Block = Attention(8 头, 逐头 Python 循环) + MLP`，输入 `(4, 128, 256)`（脚本：`.scratch\ai-compiler\ch27\exp2_profile.py`）。

### cProfile：谁在 Python 层耗时

```python
import cProfile, pstats, io
pr = cProfile.Profile(); pr.enable()
for _ in range(20):
    m(x)
pr.disable()
pstats.Stats(pr).sort_stats("tottime").print_stats(12)
```

```text
ncalls  tottime  cumtime  function
    80    0.200    0.200  {built-in method torch._C._nn.linear}     <- 62%
    20    0.054    0.146  exp2_profile.py:9(forward)                <- Attn.forward 自身
    20    0.020    0.020  {built-in method torch._C._nn.gelu}       <- 6%
    20    0.009    0.322  exp2_profile.py:26(forward)                <- Block.forward
    40    0.009    0.009  {built-in method torch.layer_norm}
   160    0.007    0.007  {method 'softmax' of 'torch._C.TensorBase'}
```
（总时间 0.323 s / 20 次迭代）

读法：`linear` 占 62%，是真正的大头；`Attn.forward` 自己的 `tottime` 是 17%——这 17% **不是算术**，是 Python 循环、切片、下标，以及那些没有独立 Python 帧的 `aten` 调用。

### torch.profiler：谁在算子层耗时

```python
from torch.profiler import profile, ProfilerActivity
with profile(activities=[ProfilerActivity.CPU]) as prof:
    for _ in range(20):
        a(x)
print(prof.key_averages().table(sort_by="self_cpu_time_total", row_limit=10))
```

```text
Name                  Self CPU %   Self CPU   CPU time avg   # of Calls
aten::addmm               36.53%   52.311ms       1.468ms           40
aten::bmm                 16.90%   24.204ms      76.193us          320
aten::_softmax            11.03%   15.795ms      98.722us          160
aten::matmul                5.40%    7.737ms     135.999us          320
aten::copy_                 5.07%    7.265ms      36.327us          200
aten::div                   4.82%    6.906ms      66.557us          160
aten::slice                 2.97%    4.256ms       9.393us          560
aten::expand                2.73%    3.911ms       7.342us          680
aten::reshape               2.28%    3.268ms       8.686us          640
aten::cat                   2.02%    2.889ms     155.810us           20
Self CPU time total: 143.184ms
```

这张表比 cProfile 有用得多，因为它按算子排了序：

- `addmm 36.5%`：4 个 `nn.Linear`，真正的 compute-bound 部分。
- `bmm 16.9%`（320 次，每次 76 µs）：逐头 attention 的两个矩阵乘。
- `_softmax 11.0%`（160 次）。
- `div 4.8%`（160 次）：`/ sqrt(hd)` 这个缩放，单独成了一个 kernel——**这是可以融进 matmul 的 4.8%**。
- `copy_ 5.1%`（200 次）+ `slice 2.97%` + `expand 2.73%` + `reshape 2.28%`：由逐头切片、`cat`、`transpose` 产生的搬运。

`# of Calls` 列同样重要：560 次 `slice`、680 次 `expand`、640 次 `reshape`，平均每次 8–9 µs。它们是第 1 层的成本，加起来的 `2.97 + 2.73 + 2.28 = 8%`。

## 示例三：把三个 kernel 放到 Roofline 上

先测这台机器的两条天花板：

```python
# 峰值算力：大 matmul 的最小时间
def peak_gflops():
    best = 0
    for n in (256, 512, 1024, 2048):
        a, b = torch.randn(n, n), torch.randn(n, n)
        mn = bench(lambda: torch.mm(a, b), warmup=5, repeat=20)["min"]
        best = max(best, 2 * n**3 / (mn / 1e3) / 1e9)
    return best

# 峰值带宽：大 tensor 的 add
def peak_gbs():
    best = 0
    for n in (1 << 16, 1 << 18, 1 << 20):
        a, b, o = torch.randn(n), torch.randn(n), torch.empty(n)
        mn = bench(lambda: torch.add(a, b, out=o), repeat=50)["min"]
        best = max(best, 12 * n / (mn / 1e3) / 1e9)
    return best
```

实测（i7-10510U，4 核 8 线程，torch 2.14.1+cpu，4 线程，取各尺寸的最小时间）：

```text
matmul (min time)          add (min time)
n=128   96.0 GFLOPs        N=1K      1.48 GB/s   <- 工作集 12 KB，太小子测不准
n=256  123.7 GFLOPs        N=16K    12.44 GB/s
n=512  144.2 GFLOPs        N=256K   17.54 GB/s   <- 峰值
n=1024 111.5 GFLOPs        N=1M     16.92 GB/s
n=2048  95.5 GFLOPs        N=16M    11.95 GB/s   <- 工作集 192 MB，超出 L3
```

取 `144.2 GFLOPs` 和 `17.54 GB/s`，拐点：

```text
ridge = 144.2e9 / 17.54e9 ≈ 8.2 flop/byte
```

把三个 kernel 放上去，比较 Roofline 预测与实测：

| kernel | AI | 预测时间 | 实测（min） | 达成率 | 判定 |
|---|---|---|---|---|---|
| `add` 1M | 0.083 | `12.58 MB / 17.54 GB/s = 0.717 ms` | 0.744 ms | 96% | memory-bound |
| `add` 16M | 0.083 | `201.3 MB / 11.95 GB/s = 16.85 ms` | 16.84 ms | 100% | memory-bound（DRAM 带宽） |
| `matmul` 1024 | 170.7 | `2.147 GFLOP / 144.2 GFLOPs = 14.89 ms` | 19.264 ms | 77% | compute-bound |
| 64 元素小算子 | 0.083 | `768 B / 17.54 GB/s = 0.000044 ms` | 0.00468 ms | 0.9% | overhead-bound |

三条读出三件事：

1. `add` 1M 达到了峰值带宽的 96%——没有优化空间了，想更快只能少搬字节（融合、量化）。
2. `add` 16M 必须用 DRAM 带宽（11.95）算，用峰值 17.54 算会以为它只发挥了 68%，其实它已经跑满。
3. `matmul` 1024 只达到峰值算力的 77%，有优化空间（分块、更好的 GEMM 库），但它是 compute-bound，**减内存流量没用**。

**工作集大小决定用哪个带宽。** 上表里 `add` 的 12KB/16K/256K/1M/16M 五档，带宽从 1.48 涨到 17.54 再跌回 11.95，这条曲线就是 cache 层次（L1/L2/L3/DRAM）。写 roofline 表时如果把「峰值带宽」和「DRAM 带宽」混用，结论会差 40%。

## 示例四：优化错了方向

### 案例 A：把时间花在融合上，瓶颈是 Python 调用

模型：一个 40 步的链 `y = y * 1.001`，每步作用在 64 个元素上（典型的小 tensor 推理/后处理）。

```python
# .scratch\ai-compiler\ch27\final_overhead.py
row, ks, o64 = torch.randn(64), torch.tensor(1.001), torch.empty(64)

def chain(kind, steps=40):
    y = row
    for _ in range(steps):
        if kind == "py":   y = y * 1.001                  # Python float 标量
        elif kind == "ts": y = torch.mul(y, ks)           # tensor 标量
        else:              torch.mul(y, ks, out=o64); y = o64
    return y
```

实测（同一进程、同一时刻，取 min）：

| 做法 | 总时间 | 单位成本 | 相对基线 |
|---|---|---|---|
| 基线：40 次 `y * 1.001`（Python float） | 656.8 µs | 16.42 µs/op | 1.00× |
| 换 tensor 标量 `torch.mul(y, ks)` | 330.0 µs | 8.25 µs/op | **2.0×** |
| 再加 `out=` 复用 buffer | 240.0 µs | 6.00 µs/op | **2.7×** |
| 批量化：同样 40 个 op 作用在 `(256,64)` | 632.3 µs | 2.47 µs/行 | **266×/行** |

第四行是全表最重要的一行：**数据量 ×256，总时间几乎没变**（656.8 → 632.3 µs）。因为 40 次调用的固定开销和 tensor 大小无关，256 行数据在同一个 kernel 里被顺带算完了。

现在算这个 workload 的 Roofline 位置：40 × 64 × 12 B = 30.7 KB，按 17.54 GB/s 应该是 `1.75 µs`。实测 656.8 µs，是预测的 **375 倍**。达成率 0.27%，AI 判据在这里彻底失效——它根本不是 memory-bound。

所以「融合」这个方向能给你什么？

- 时间与 op 数严格线性：本机实测 5/10/20/40 个 op 分别是 `10.96 / 10.42 / 10.36 / 10.41 µs/op`。
- 融合把 40 个 op 变成 20 个，收益上限 2×；变成 1 个，上限 40×——但那已经是「整条链编进一个 kernel」，属于编译器的活，不是手写 fusion。
- 而上面换一个**标量重载**（完全不动 kernel）就拿到 2.0×，加 `out=` 拿到 2.7×，批量化拿到 266×。

结论：**在这种 shape 下，「融合」和「换个 API 写法」的收益是同一量级，而真正的量级在批量化/编译化那一侧。** 先测每 op 成本与 tensor 大小的关系，再决定要不要动 kernel。

### 案例 B：看似 Python 循环，其实是 GEMM

同一个模型里的 8 头 attention，写成逐头 Python 循环。看 profile 的 `slice 560 次`、`expand 680 次`，第一反应一定是「把循环改成 bmm」。改了之后：

```text
逐头 Python 循环: median = 11.813 ms
一次 bmm 批量化   : median = 11.336 ms
加速比            : 1.04x        （maxdiff = 0.0，语义完全一致）
```

**1.04×，在噪声范围内。** 因为 profile 说得很清楚：`addmm 36.5% + bmm 16.9% + _softmax 11.0%`，循环产生的 `slice/expand/reshape` 加起来只有 8%。Python 循环确实存在，但它不是瓶颈。

这张表里真正值得动的是：

| 目标 | 占比 | 手段 |
|---|---|---|
| `addmm` / `bmm` | 53% | 换后端、调 tile、半精度——第 2 层的活 |
| `_softmax` | 11% | 融合进 attention kernel，或换更快的 softmax |
| `copy_` | 5.1% | 一次性转成连续布局，避免每个 bmm 都做隐式拷贝 |
| `div` | 4.8% | 把 `/sqrt(hd)` 折进 q 的缩放（或折进 matmul 的 epilogue） |

两个案例合起来给出同一条方法论：**先用量级判断是第几层的问题，再选这一层的手段。** 反过来的顺序（先想好一个优化，再去测它）会让你在 1.04× 上花掉一天。

## 示例五：回归基线

性能回归的门禁不需要复杂框架，20 行够用：

```python
# .scratch\ai-compiler\ch27\exp5_regression.py
import json, os, statistics, time, torch

def bench(fn, warmup=10, repeat=100):
    for _ in range(warmup): fn()
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter(); fn(); ts.append((time.perf_counter() - t0) * 1e3)
    ts.sort(); q = statistics.quantiles(ts, n=4)
    return {"median": round(statistics.median(ts), 4), "min": round(ts[0], 4),
            "p25": round(q[0], 4), "p75": round(q[2], 4)}

torch.set_num_threads(4)
x, w = torch.randn(1 << 20), torch.randn(1 << 20)
cases = {"add_1m": lambda: torch.add(x, w), "tanh_1m": lambda: torch.tanh(x)}
BASE = ".scratch/ai-compiler/ch27/baseline.json"

if os.path.exists(BASE):
    baseline = json.load(open(BASE))
else:
    baseline = {k: bench(f)["median"] for k, f in cases.items()}
    json.dump(baseline, open(BASE, "w"), indent=2)

for name, fn in cases.items():
    r = bench(fn); base = baseline[name]; ratio = r["median"] / base
    print(f"{name:8s} base={base:.3f} now={r['median']:.3f} ratio={ratio:.2f}x "
          f"-> {'PASS' if ratio <= 1.20 else 'FAIL'}")
```

实测输出：

```text
baseline: {'add_1m': 0.8626, 'tanh_1m': 1.3805}
add_1m   base=0.863ms now=0.727ms ratio=0.84x min=0.545 p25=0.610 p75=1.139 -> PASS
tanh_1m  base=1.381ms now=1.435ms ratio=1.04x min=1.067 p25=1.342 p75=1.762 -> PASS
gate: PASS
```

注意 `add_1m` 的 `ratio=0.84x`——**代码一行没改，测出来「快了 16%」**。这不是优化，是漂移。所以：

1. **阈值必须大于噪声。** 本机 session 间漂移是 1.32×（min）到 1.63×（median），`1.2×` 的门禁在这台机器上既会漏报 16% 的真回归，也会误报 10% 的假回归。要么把阈值放到 1.5×，要么换更稳的统计量。
2. **用 min 或 p25 做门禁**，不用 median。min 的漂移（1.32×）明显小于 median（1.63×），因为它剥掉了后台负载那一层噪声。
3. **同一 session 里跑一个 control case**（一个你绝不会改的算子），用 `case / control` 的比值做归一化，能消掉当天的机器状态。
4. **baseline 必须带环境信息**：CPU 型号、线程数、torch 版本、是否插电。换机器、换线程数、换电源模式都会让整份 baseline 失效。

## 对比与取舍

| 测量手段 | 精度 | 额外开销 | 适用 |
|---|---|---|---|
| `time.perf_counter` + repeat | 中 | 极小 | 端到端、多方案 A/B |
| `torch.utils.benchmark.Timer` | 高 | 小 | 需要自动选 repeat 次数时 |
| `f.time_evaluator`（TVM） | 高 | 小 | TVM kernel，自带 number/repeat |
| `cProfile` | 低（相对占比可信） | 大（+23%） | 找 Python 层热点 |
| `torch.profiler` | 中 | 中 | 找 aten 算子热点 |
| Roofline 估算 | 数量级 | 零 | 判断该往哪个方向优化 |

| 优化手段 | 收益上限 | 什么时候无效 |
|---|---|---|
| 算子融合 | 省下的字节 / 总字节 | 工作集小于 cache、固定开销主导 |
| 批量化 / 编译成单 kernel | 少掉 N-1 次固定开销 | 已经是单 kernel |
| 换更快的 GEMM 后端 | 到峰值算力的差距 | 本来就是 memory-bound |
| 减少 Python 调用（`out=`、tensor 标量） | 每次调用固定开销的一部分 | 单次调用已经够大 |
| 量化 / 半精度 | 带宽减半或算力翻倍 | 精度不允许，或本来就 overhead-bound |

## 踩雷集锦

1. **只测一次**：本机同一 kernel 单次采样能差 3.6×，报单次等于报噪声。
2. **不 warmup**：冷启动 `1.770 ms` vs 稳态 `0.92 ms`，差 1.9×。
3. **在计时循环里 `print` / `.item()` / 转 numpy**：会同步、会格式化，把 IO 时间算进 kernel。
4. **用 mean 报性能**：右偏分布下 mean 被长尾拖走，median 才描述典型值。
5. **跨 session 比较绝对值**：本机 median 漂移 1.63×，A 的今天和 B 的昨天不能直接比。
6. **把 Roofline 用在 64 元素的算子上**：预测 0.044 µs、实测 4.68 µs，差 106×；小算子由固定开销主导。

## 进阶：再往深一层

- **Bootstrap 置信区间**：把 repeat 得到的样本重采样 1000 次，取 median 的 2.5%/97.5% 分位。两个方案的置信区间不重叠，才算有差异。比「看比值」严谨得多。
- **ABBA 交错**：A/B/B/A 的顺序能同时消掉「先测的吃亏」和「后测的吃亏」两种系统性偏差，是 A/B 测试的标准做法。
- **cache-aware Roofline**：把单条斜线换成 L1/L2/L3/DRAM 四条斜线，每个工作集尺寸对应不同上限。本章的带宽表就是这条多级 roofline 的实测切片。
- **尾部延迟**：推理服务关心 p99 而不是 median。本章的 `p75` 已经能看到尾部，生产环境要看 p99/p999，并且要区分「kernel 慢」和「排队等线程」。

## 动手练习

### 练习 1：给 harness 加置信区间

任务：写一个 `bootstrap_ci(samples, n=1000)`，返回 median 的 95% 置信区间；用它判断示例四里「tensor 标量 2.0×」是否显著。

<details>
<summary>参考解答</summary>

```python
import random, statistics
def bootstrap_ci(xs, n=1000, lo=2.5, hi=97.5):
    meds = []
    for _ in range(n):
        s = [random.choice(xs) for _ in xs]
        meds.append(statistics.median(s))
    meds.sort()
    return meds[int(n*lo/100)], meds[int(n*hi/100)]
```

两组各 30 个样本，若区间不重叠则可判定显著。2.0× 的差异远大于本机噪声，区间几乎必然分离；而 1.04× 的 attention 改写，区间大概率重叠——那就不能说「更快」。

</details>

### 练习 2：用 Roofline 判断 `tanh` 属于哪一类

任务：`tanh` 对 1M 个 float32 的实测中位数是 `1.381 ms`。算它的有效带宽，判断它是 memory-bound 还是 compute-bound。

<details>
<summary>参考解答</summary>

读 4 MB、写 4 MB，共 8.39 MB。`8.39 MB / 1.381 ms ≈ 6.1 GB/s`，只有峰值带宽（17.54 GB/s）的 35%。如果是 memory-bound，它应该跑到 0.48 ms 左右。所以瓶颈不在带宽，在 `tanh` 本身的计算（超越函数，向量化程度决定吞吐）。这类算子的优化方向是换近似（`tanh` 的 polynomial 近似）或换更低精度，而不是减少读写。

</details>

### 练习 3：给 profile 里的算子分层

任务：用示例二的表，把 top-5 算子分别归到「调用层 / kernel 层 / 算法层」，并为每个写一句「该怎么优化」。

<details>
<summary>参考解答</summary>

`addmm`（36.5%）→ kernel/算法层，换 GEMM 后端或调 tile；`bmm`（16.9%）→ kernel 层，注意 stride 导致的隐式 copy；`_softmax`（11.0%）→ kernel 层，融合或换实现；`copy_`（5.1%）→ 调用层（布局转换），提前把 tensor 变成连续布局；`div`（4.8%）→ 算法层，把标量缩放折进 matmul。注意「算法层」的两个收益都不需要改后端。

</details>

## 本章重点整理

- 一次测量等于一次抽样；本机同一 kernel 单次采样波动可达 3.6×。
- harness 五要素：warmup、repeat、隔离、分位数、交错 A/B。
- 报 `median + p25/p75 + min + n`；不报 mean，不报单次。
- 本机 session 间漂移：median 1.63×、min 1.32×；小于 1.5× 的差异不要下结论。
- cProfile 看 Python 层，torch.profiler 看 aten 层；两者都有开销（cProfile +23%）。
- Roofline 用 `AI = FLOPs/Bytes` 与 `ridge = 峰值算力/峰值带宽` 判断 bound。
- 工作集大小决定用哪个带宽：本机 12KB→1.48、256K→17.54、16M→11.95 GB/s。
- 小算子（64 元素）达成率 0.9%，由固定开销主导，Roofline 不适用。
- 优化前先判层：本机 40 个 64 元素 op 的链，时间与 op 数线性（约 10.4 µs/op），换标量重载 2.0×、加 `out=` 2.7×、批量化 266×/行。
- 回归门禁要带环境信息、用 min、设大于噪声的阈值。

## 自我检核

- [ ] 我能写出带 warmup/repeat/分位数的 `bench()`
- [ ] 我能解释为什么不能只跑一次、为什么不能报 mean
- [ ] 我知道 cProfile 和 torch.profiler 分别能定位哪一层
- [ ] 我能用 `AI` 和 `ridge` 判断一个 kernel 是 memory-bound 还是 compute-bound
- [ ] 我能说出「融合」的收益上限由什么决定

## 延伸阅读

- **Williams, Waterman, Patterson, Roofline: An Insightful Visual Performance Model (2009)**（https://dl.acm.org/doi/10.1145/1498765.1498785）：读第 2–3 节，看 AI 与拐点怎么推。与本章的关联：示例三的全部公式。
- **PyTorch `torch.profiler` 文档**（https://pytorch.org/docs/stable/profiler.html）：读 `key_averages()` 与 `self_cpu_time_total` 的含义。与本章的关联：示例二的算子表。
- **TVM `time_evaluator` 文档**（https://tvm.apache.org/docs/）：读 `number`/`repeat`/`min_repeat_ms` 参数。与本章的关联：TVM kernel 的标准计时方式。
- **Ch 16 内存层次与 Roofline**：本章的带宽表和 Roofline 判定都建立在那里的 cache 模型上；想理解 12KB→17.54→11.95 这条曲线，回去读那一章的层次结构。

---

→ [Ch 28 读 TVM 源码：从 Python wrapper 到 FFI 边界](./28-reading-tvm-source.md)