# Ch 16 内存层次与 Roofline：为什么 tile 大小能决定生死

> **目标**：建立"内存层次 + 算术强度 + Roofline"这套判断框架，能回答三个问题：这个 kernel 是访存受限还是计算受限？tile 该往哪个方向调？为什么同一份 schedule 改一个 factor 就能慢好几倍？

## 为什么需要这个？

Ch 15 讲的是"一条指令能干几份活"，那是 ALU 视角。但只要数据不在寄存器里，ALU 再宽也得等内存。现代 CPU 的存储层次跨度是**四个数量级**：

| 层级 | 本机容量 | 延迟量级 | 实测带宽（单线程） |
|---|---|---|---|
| L1d | 32 KB / 核 | ~4 cycle | ~60–75 GB/s |
| L2 | 256 KB / 核 | ~12 cycle | ~42–65 GB/s |
| L3 | 8 MB（共享） | ~40 cycle | ~28–40 GB/s |
| DRAM | 42 GB | ~200 cycle | ~8–11 GB/s |

（容量来自 i7-10510U 规格；带宽是本章下面那段 copy kernel 的实测。）

这意味着：**同一个 matmul，数据在 L1 里和在 DRAM 里，能差 8 倍带宽。** tile 的本质就是"把工作集压进下一层 cache"。但 tile 不是免费的——切得太碎，循环开销和代码膨胀会把收益吃光；切得不对，编译器连向量化都不做了（Ch 15 的坑）。

这一章要给的是一把尺子：**Roofline**。它告诉你"在给定算术强度下，这台机器的上限在哪"，你才知道自己离上限有多远、该往哪个方向优化。

## 先建立直觉：算术强度

算术强度（arithmetic intensity，AI）= 做的浮点运算数 ÷ 搬运的字节数，单位 FLOP/byte。

- 纯 elementwise `C[i] = A[i] * 2 + B[i]`：2 FLOP，搬 12 字节（读 A、读 B、写 C）→ **AI = 0.17**。
- 朴素 matmul（N×N）：2N³ FLOP，理想情况搬 3N² 个 float = 12N² 字节 → **AI = N/6**。N=512 时是 85，N=1024 时是 171。

差别是 500 倍。**AI 低 → 你在等内存；AI 高 → 你在等 ALU。** 这就是"访存受限（memory-bound）"和"计算受限（compute-bound）"的定量定义。

Roofline 模型把这件事画成一条折线：

```text
性能 (GFLOPS)
   ^
61 |            ┌────────────────  compute roof = 峰值算力
   |           /
   |          /   slope = 峰值带宽
   |         /
   +---------+-------------------------> 算术强度 (FLOP/byte)
            ridge = 峰值算力 / 峰值带宽
```

- 斜线部分：`性能 = AI × 带宽`，访存受限。
- 水平部分：`性能 = 峰值算力`，计算受限。
- 拐点（ridge）：本机约 `61 GFLOPS / 9 GB/s ≈ 6.8 FLOP/byte`。

matmul（AI=85）在拐点右边很远 → 计算受限，优化方向是**指令级**（向量化、FMA、寄存器分块）。
elementwise（AI=0.17）在拐点左边很远 → 访存受限，优化方向是**减少字节数**（融合、量化、复用）。

## 核心概念一：内存层次是"带宽阶梯"，不是"一个数"

先亲手把这条阶梯测出来。用一个向量化的 copy kernel，扫不同大小：

```python
import tvm, numpy as np
from tvm import te

def build_copy(n):
    A = te.placeholder((n,), name="A")
    C = te.compute((n,), lambda i: A[i] * 2.0, name="C2")
    sch = tvm.s_tir.Schedule(te.create_prim_func([A, C]))
    (i,) = sch.get_loops(sch.get_sblock("C2"))
    io, ii = sch.split(i, factors=[None, 8])
    sch.reorder(io, ii)
    sch.vectorize(ii)
    return tvm.build(sch.mod, target="llvm")

for kb in [4, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]:
    n = kb * 1024 // 4
    f = build_copy(n)
    a = tvm.runtime.tensor(np.random.rand(n).astype("float32"), tvm.cpu())
    c = tvm.runtime.empty((n,), "float32", tvm.cpu())
    tev = f.time_evaluator(f.entry_name, tvm.cpu(), number=max(20, int(2e7 / n)), repeat=5)
    t = tev(a, c).min
    print(f"{kb:6d} KB  {t*1e6:8.2f} us  {2*n*4/t/1e9:6.2f} GB/s")
```

实测（单线程，每档取 min）：

```text
     4 KB      0.11 us   74.73 GB/s     ← L1
    16 KB      0.50 us   65.39 GB/s
    32 KB      1.09 us   60.07 GB/s     ← L1 边界（读+写 64KB > 32KB L1）
    64 KB      2.12 us   61.95 GB/s
   128 KB      4.46 us   58.82 GB/s
   256 KB     12.48 us   42.01 GB/s     ← L2 边界（读+写 512KB > 256KB L2）
   512 KB     27.05 us   38.76 GB/s
  1024 KB     51.58 us   40.66 GB/s
  2048 KB    104.49 us   40.14 GB/s
  4096 KB    294.82 us   28.45 GB/s
  8192 KB   1425.58 us   11.77 GB/s     ← L3 边界（读+写 16MB > 8MB L3）
 16384 KB   3296.69 us   10.18 GB/s     ← DRAM
 32768 KB   6935.74 us    9.68 GB/s
 65536 KB  14809.05 us    9.06 GB/s
```

三个拐点：32 KB（L1 边界）、256 KB（L2 边界）、8 MB（L3→DRAM）。拐点位置对应"读 + 写两份数据塞不进下一层"的容量。前两个拐点带宽只掉 20–30%，8 MB 处掉一半以上——那才是真的去 DRAM。**从 L1 到 DRAM，带宽差约 8 倍**——这就是 cache blocking 存在的全部理由。

## 核心概念二：tiling 把工作集压进 cache

朴素三重循环的问题是：对每个 `(i, j)`，都要把 `B[:, j]` 从头扫一遍。当 B 大于 cache，每一遍都从 DRAM 读。

分块之后，外层按 `(i_o, j_o, k_o)` 遍历块，内层在 `(i_i, j_i, k_i)` 里算一个小矩阵乘。一个 T×T 的块只需要 `3T²×4` 字节的工作集：

| tile | 工作集（3×T²×4B） | 落在哪层 |
|---|---|---|
| 8 | 768 B | L1 |
| 16 | 3 KB | L1 |
| 32 | 12 KB | L1 |
| 64 | 48 KB | L1 放不下 → L2 |

TVM 里就是三个 `split` 加一次 `reorder`：

```python
def matmul(N, tile):
    A = te.placeholder((N, N), name="A")
    B = te.placeholder((N, N), name="B")
    k = te.reduce_axis((0, N), name="k")
    C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

    sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))
    i, j, kk = sch.get_loops(sch.get_sblock("C"))
    i_o, i_i = sch.split(i, factors=[None, tile])
    j_o, j_i = sch.split(j, factors=[None, tile])
    k_o, k_i = sch.split(kk, factors=[None, tile])
    sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)   # 顺序就是分块语义
    return tvm.build(sch.mod, target="llvm")
```

注意 `reorder` 的顺序不是随便写的：`i_o, j_o, k_o` 在外，`i_i, j_i, k_i` 在内，读作"对每个块，做完整个 k 维累加，再搬到下一个块"。写反了就是另一套内存访问模式。

## 核心概念三：Roofline 怎么用

三步：

1. **算 AI**：数清 FLOP 和必搬的字节。
2. **查屋顶**：`min(峰值算力, AI × 峰值带宽)`。
3. **对照实测**：差多少？差在算力还是带宽？

本机的两个屋顶（实测）：

```text
峰值算力（单线程 BLAS）：61–63 GFLOPS（空闲时）；AVX2 FMA 理论值 8×2×2×2.3GHz ≈ 73.6
峰值带宽（DRAM 单线程）：8–9 GB/s
ridge = 61 / 9 ≈ 6.8 FLOP/byte
```

拿 matmul N=512 代入：AI = 512/6 ≈ 85 → 85 ≫ 6.8 → **计算受限**，屋顶是 61 GFLOPS。
拿 elementwise 代入：AI = 0.17 → 屋顶 = 0.17 × 9 ≈ 1.5 GFLOPS（等价于 9 GB/s 带宽）。

**结论先行**：本章所有 matmul 实验，瓶颈都不在内存，而在"每个周期能做多少次 FMA"。所以优化方向是向量化、FMA、寄存器分块，而不是"再多切一层 cache"。

## 核心概念四：tile 只是第一级，寄存器分块才是第二级

tile 把块压进 L1，但 L1 到寄存器还有一段路。BLAS 的做法是三级：

```text
i_o, j_o, k_o    →  L3 / L2 级：整块调度（本节的 tile）
  i_i, j_i, k_i  →  L1 级：小矩阵乘
    unroll + 寄存器累加  →  寄存器级：一个 8×8 的 C 子块常驻寄存器
```

寄存器级的关键是**让每个加载进来的 A/B 元素被复用多次**：一个 8×8 的 C 累加器（64 个寄存器）配 k 方向一次加载 8 个 A、8 个 B，就能做 64 次 FMA。这正是 BLAS 和手写 TVM schedule 之间那个数量级差距的来源。

TVM 里对应的原语是 `cache_read` / `cache_write`（把子块搬进 `local` 作用域），但它们在 0.27 里对 block 名称和插入位置有要求，写不对会抛 `ScheduleError`。本章不展开这套写法——Practice C 用"tile + vectorize + parallel"逼近，读到 BLAS 的差距时请记住这一级的存在。

## 底层机制：一次 tile 之后，硬件在做什么

以 tile=16 的 `C[i,j] += A[i,k]*B[k,j]` 为例，内层 `k_i` 的 16 次迭代里：

- `A[i, k]`：i 固定，k 连续 → 16 个 float 连续，可以一次预取进 L1；
- `B[k, j]`：k 变化、j 固定 → **跨行访问**，每次跳 N 个 float。这是分块 matmul 的天然弱点；
- `C[i, j]`：整个 k 循环里只读写一次（累加在寄存器里）。

所以编译器真正想要的内层顺序是"j 连续"——这样 B 和 C 都是顺序访问，A 退化成标量广播。这也是为什么很多高性能 kernel 把 j 放在最内层并向量化（Practice C 会做这件事）。

一个反直觉但重要的现象：**tile 大小不只影响 cache，还影响编译器的优化决策。** 后面示例二会给出证据。

## 示例一：规模-性能表（实测）

下面这段可以整段复制运行。三个版本共用同一份 `te.compute`，只改 schedule：

```python
import tvm, numpy as np, statistics, time
from tvm import te

def matmul(N, tile=None):
    A = te.placeholder((N, N), name="A")
    B = te.placeholder((N, N), name="B")
    k = te.reduce_axis((0, N), name="k")
    C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
    sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))
    if tile:
        i, j, kk = sch.get_loops(sch.get_sblock("C"))
        i_o, i_i = sch.split(i, factors=[None, tile])
        j_o, j_i = sch.split(j, factors=[None, tile])
        k_o, k_i = sch.split(kk, factors=[None, tile])
        sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)
    return tvm.build(sch.mod, target="llvm")

rng = np.random.default_rng(0)
for N in (64, 128, 256, 512, 1024):
    a = rng.random((N, N), dtype="float32")
    b = rng.random((N, N), dtype="float32")
    ta = tvm.runtime.tensor(a, tvm.cpu())
    tb = tvm.runtime.tensor(b, tvm.cpu())
    tc = tvm.runtime.empty((N, N), "float32", tvm.cpu())

    out = {}
    for label, f in (("naive", matmul(N)), ("tile16", matmul(N, 16))):
        samples = []
        for _ in range(3):
            tev = f.time_evaluator(f.entry_name, tvm.cpu(),
                                   number=max(2, int(1e9 / (2 * N**3))), repeat=1)
            samples.append(tev(ta, tb, tc).min)
        out[label] = 2 * N**3 / statistics.median(samples) / 1e9

    # BLAS 对照：必须关掉多线程，否则是 1 核算力 vs 8 核算力
    reps = max(2, int(5e8 / (2 * N**3)))
    t0 = time.perf_counter()
    for _ in range(reps):
        a @ b
    out["blas1t"] = 2 * N**3 / ((time.perf_counter() - t0) / reps) / 1e9

    print(f"N={N:5d} AI={N/6:4.0f}  naive={out['naive']:5.2f}  tile16={out['tile16']:5.2f}  blas1t={out['blas1t']:5.2f}")
```

运行前设置环境变量（PowerShell）：

```powershell
$env:OPENBLAS_NUM_THREADS = "1"
```

实测（GFLOPS，多轮中位数；本机同时有其他负载，绝对值偏低）：

| N | AI = N/6 | 朴素 | tile=16 | BLAS（1 线程） |
|---|---|---|---|---|
| 64 | 11 | 1.55 | 8.46 | 22.40 |
| 128 | 21 | 0.94 | 5.10 | 19.82 |
| 256 | 43 | 0.93 | 6.79 | 31.50 |
| 512 | 85 | 0.48 | 4.81 | 27.21 |
| 1024 | 171 | 0.42 | 5.55 | 37.82 |

三个观察：

1. **朴素实现随 N 增大而变慢**（1.55 → 0.42）。因为它对每个 (i,j) 都重新扫 B，N 越大，cache 越装不下。
2. **tile=16 基本持平在 5–8.5 GFLOPS**，不随 N 崩。工作集固定 3KB，永远在 L1。
3. **BLAS 高一个数量级**（20–38，空闲时 61）。差距不在 cache，在寄存器分块、FMA 打包和微内核展开。

同一份代码在机器空闲时能到 14–15 GFLOPS（tile=16，N=512），BLAS 单线程能到 61–63。**记录数字时必须写清机器状态**，否则会把负载波动当成优化效果。

## 示例二：tile 的拐点，以及一个真实的"越切越慢"

固定 N=512，只改 tile：

```text
tile=  8:  10.30 GFLOPS
tile= 16:  14.23 GFLOPS
tile= 32:   1.38 GFLOPS     ← 悬崖
tile= 64:   1.21 GFLOPS
```

16 → 32 掉了 4–10 倍（本次重跑 7.81 → 1.68，此前一次是 14.23 → 1.38）。按 cache 理论这不该发生：32×32 的工作集是 12KB，仍然塞得进 32KB 的 L1。**所以原因不是 cache。** 去看生成的 IR 就明白了：

```text
tile=16: LLVM IR 1222 行，含 " x float>" 的行 173 条，fmuladd 65 处
tile=32: LLVM IR  986 行，含 " x float>" 的行   0 条，fmuladd  3 处
```

tile=16 时 LLVM 把内层 j 循环自动向量化了（173 条向量指令、65 条 FMA）；tile=32 时它**一条向量指令都没生成**。这就是数倍差距的来源——和 Ch 15 的结论接上了：**向量化决策由编译器做，tile 大小会改变它的成本模型判断。**

为什么会翻转？一个可检验的假设是循环体变大后，LLVM 的成本模型认为向量化的收益不足以抵消寄存器压力和展开开销。要确认得看 `-debug-only=loop-vectorize` 的日志（本机是 release 包，属 `guided`），或者手动加 `vectorize` 强制验证——Practice C 会做后者，结果是"强制向量化能把 tile=32 拉回来，但仍不如 tile=16"。

**这就是"盲目 tiling 反而变慢"的真实版本**：不是 cache 不够，是代码生成路径变了。教科书上"tile 取 32 或 64"的建议，换一台机器、换一个编译器版本就可能失效。

## 示例三：把 kernel 放到 Roofline 上

用本章的测量值画几个"点"：

| kernel | AI (FLOP/B) | 屋顶 | 实测 | 占屋顶 |
|---|---|---|---|---|
| elementwise `A*2+B` | 0.17 | 1.5 GFLOPS / 9 GB/s | 5.9–18 GB/s | ~65%–200%（受 L2 影响） |
| matmul N=512，朴素 | 85（理想） | 61 GFLOPS | 0.48 GFLOPS | 0.8% |
| matmul N=512，tile16 | 85 | 61 GFLOPS | 4.8–15.1 GFLOPS | 8%–25% |
| matmul N=512，BLAS 1t | 85 | 61 GFLOPS | 27–63 GFLOPS | 45%–100% |

读法：

- elementwise 已经贴着带宽屋顶跑，**再怎么优化指令都没用**，只能减少字节（融合、原地写、低精度）。
- matmul 离算力屋顶还差 4–12 倍，**瓶颈是每条指令干的活太少**（没有寄存器分块、FMA 没打满）。
- 如果哪天你把 matmul 优化到 55 GFLOPS，AI 还是 85，说明你已经在屋顶上；再往上只能换硬件或多线程。

## 示例四：计算受限时，多线程是另一根杠杆

既然 matmul 是计算受限，把外层 i 分块并行就能吃到多核：

```python
i_o, i_i = sch.split(i, factors=[None, 16])
j_o, j_i = sch.split(j, factors=[None, 16])
k_o, k_i = sch.split(kk, factors=[None, 16])
sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)
sch.parallel(i_o)          # 只加这一行
```

N=512 的实测（两轮，负载不同，每轮内两个版本相邻测量）：

```text
第一轮: tile=16  9.72  →  +parallel 26.09   (2.7×)
第二轮: tile=16  5.39  →  +parallel 17.02   (3.2×)
```

8 线程只换来 2.7–3.2 倍，原因有两个：这是一台 4 核 8 线程的 15W 笔记本，超线程共享执行单元；而且并行之后每个核仍要读同一份 B，共享 L3 带宽开始成为新瓶颈。**这也说明 Roofline 不是一次性的判断——并行化会把瓶颈从 ALU 推到内存，需要重新画一条线。**

## 对比与取舍

| 判断 | 该做什么 | 不该做什么 |
|---|---|---|
| AI ≪ ridge（访存受限） | 融合、减少中间张量、量化、改 layout | 加 unroll、换更宽向量 |
| AI ≫ ridge（计算受限） | 向量化、FMA、寄存器分块、多线程 | 继续切 cache（收益递减） |
| 工作集刚好跨 cache 边界 | 调 tile 让 3T² 落进 L1/L2 | 盲抄"32×32" |
| tile 调大后突然变慢 | 先看 IR 里向量指令还在不在 | 归因于"cache 不够" |
| 单线程已到屋顶 | 多线程 / 换 BLAS / 换硬件 | 继续抠指令 |
| 并行后扩展比很差 | 检查共享带宽和 false sharing | 盲目加线程 |

## 踩雷集锦

1. **把 tile 当万能药。** tile=32 在 N≥512 时比 tile=16 慢 4–10 倍，因为 LLVM 不向量化了。
2. **用 GFLOPS 单点比较不同时间跑的结果。** 本机共享负载，同一份代码能差 2–3 倍；必须同轮交错测、取中位数。
3. **忘了 BLAS 是多线程的。** 不设 `OPENBLAS_NUM_THREADS=1`，你是在拿 1 核比 8 核。
4. **把 "AI=N/6" 当成永远成立。** 它假设每个矩阵只从 DRAM 读一次；不分块且矩阵大于 cache 时，真实 AI 会低得多。
5. **只看总时间，不看工作集。** 先算 3T²×4 字节，判断它该在哪层 cache，再决定 tile。
6. **以为 `time_evaluator` 的 `number` 越大越准。** 大 `number` 会被系统调度打断；`min` 比 `mean` 更能反映 kernel 本身。

## 进阶：再往深一层

- 给 matmul 加 `sch.parallel(i_o)`，实测能到 18.7–26.1 GFLOPS，看扩展比为什么不到 8×。
- 试 `sch.cache_read("A", 0, "local")` 把子块搬进栈；0.27 里对 block 名称和插入位置有要求，写错会抛 `ScheduleError`，先在 `sch.mod.script()` 里确认插入的 block 再继续。
- 把 dtype 换成 `float64`，AI 不变但带宽翻倍、算力减半，看 roofline 的拐点怎么移动。
- 用 `numpy.show_config()` 确认 BLAS 后端（OpenBLAS/MKL），不同后端的单线程峰值能差 20%。
- 读 Ansor 论文的 cost model 部分，理解自动调优为什么也要先做 tiling 再做向量化。

## 动手练习

### 练习 1：画出你自己的内存阶梯

用本节 copy kernel，在 4KB–64MB 之间取 8 个点，画出 GB/s 曲线，标出 L1/L2/L3/DRAM 的边界，并和 CPU 规格对照。

<details>
<summary>参考思路</summary>

曲线会在 32KB、256KB、2–8MB 附近出现明显下降。注意工作集要算"输入 + 输出"两份（本例是 2n×4 字节）。如果你的机器 L3 更大，第三级台阶会右移。

</details>

### 练习 2：找出你机器上的 tile 悬崖

固定 N=512，扫 tile=8/16/24/32/48/64（不能整除的会被谓词包住），记录 GFLOPS 和 IR 里的向量指令数。找出悬崖位置，并检查它是否对应"向量指令消失"。

<details>
<summary>参考思路</summary>

本机悬崖在 16→32。判断方法是 `str(f.inspect_source()).count(" x float>")`。如果悬崖处向量指令仍在，那才轮到怀疑 cache；否则是 codegen 决策。

</details>

### 练习 3：用 Roofline 判断优化方向

对 `C = A @ B + bias`（N=512），算 AI，查屋顶，判断该优化哪一头；然后实测朴素版和融合版的差距。

<details>
<summary>参考思路</summary>

matmul 部分 AI≈85，bias 加法只增加 2N² FLOP 和 N² 读写，几乎不改变 AI → 仍然计算受限。融合 bias 的收益主要在省掉一次 1MB 的中间张量往返（约 0.2ms），相对 matmul 本身（>10ms）很小；这也说明"融合不是永远重要"。

</details>

### 练习 4：验证"AI=N/6"的失效

把 N 调到 4096（每个矩阵 64MB，远超 L3），比较朴素实现的实际 GFLOPS 和按 AI=683 算出的屋顶，解释差距。

<details>
<summary>参考思路</summary>

不分块时 B 会被反复从 DRAM 读，实际 AI 降到 O(1) 量级，屋顶也随之降到带宽级别；而按"理想 AI"算出的屋顶高得离谱。这个反例说明：**AI 必须按真实流量算，不是按公式套。**

</details>

## 本章重点整理

- 内存层次是带宽阶梯：L1 ~60–75 GB/s、L2 ~42–65、L3 ~28–40、DRAM ~8–11（本机单线程实测）。
- 算术强度 AI = FLOP / 字节；matmul 理想值 N/6，elementwise 约 0.17。
- Roofline 屋顶 = `min(峰值算力, AI × 峰值带宽)`；本机 ridge ≈ 6.8 FLOP/byte。
- matmul 在这些规模上是计算受限，瓶颈在指令级效率，不在带宽。
- tiling 把工作集压到 3T²×4 字节，但 tile 变大会改变编译器的向量化决策——本机 16→32 掉 4–10 倍，IR 里向量指令从 173 条变 0 条。
- tile 之上还有寄存器分块，这是 BLAS 领先一个数量级的关键。
- 并行会把瓶颈从 ALU 推到共享带宽，扩展比达不到线数。
- 性能数字必须标注机器状态；共享机器上单点测量没有意义。

## 自我检核

- [ ] 我能背出本机四级存储的容量和实测带宽量级
- [ ] 我能算出一个 kernel 的算术强度，并判断它在 ridge 的哪一侧
- [ ] 我能用 Roofline 说出"这个 kernel 该优化算力还是带宽"
- [ ] 我知道 tile=32 变慢的真实原因，并能用 IR 证据证明
- [ ] 我能解释为什么 BLAS 比自己写的 TVM kernel 快一个数量级
- [ ] 我知道 `OPENBLAS_NUM_THREADS=1` 对公平对比的意义
- [ ] 我能说出并行之后瓶颈会转移到哪里

## 延伸阅读

- **Williams, Waterman, Patterson, "Roofline: An Insightful Visual Performance Model" (CACM 2009)**：读第 2–3 节。与本章的关联：Roofline 的原始定义和拐点推导。
- **TVM 文档 "Schedule Primitives"**（https://tvm.apache.org/docs/）：看 `split`/`reorder`/`cache_read` 的语义。与本章的关联：tiling 的 API 表达。
- **BLIS / OpenBLAS 的 micro-kernel 设计文档**：读"register blocking"一节。与本章的关联：解释 BLAS 为什么能贴住算力屋顶。
- **Agner Fog, "Optimizing subroutines in assembly"**：读 cache 与预取章节。与本章的关联：为什么 k 内层跨行访问是 matmul 的天敌。
- **Ansor 论文 (OSDI 2020) 第 3 节**：看搜索空间如何组织 tile/vectorize/parallel。与本章的关联：手工调 tile 的经验如何变成自动搜索。

---

→ [Ch 17 运行时：从 `.so` 到 VM](./17-runtime.md)
