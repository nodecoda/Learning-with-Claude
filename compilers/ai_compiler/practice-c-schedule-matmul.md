# 练习 C：手写 schedule 跑赢朴素实现

> **目标**：从朴素三重循环出发，用 TVM 0.27 的 schedule 原语把 N=256/512 的 matmul 提速一个数量级，并用数值对齐 + 耗时两把尺子验收；最后和 BLAS 对比，看清自己离工业级还差什么。

## 任务背景：为什么拿 matmul 练手

matmul 是练习 schedule 的最佳靶子：

- **算术强度高**（AI = N/6），不会被内存带宽掩盖掉指令级优化（Ch 16 已证明 N=512 时是计算受限）；
- **优化路径清晰**：tiling → 向量化 → 并行，每一步都有可解释的收益；
- **有绝对参照物**：`numpy` 背后的 BLAS 是几十年手工调优的结果，差距能说明你缺的是哪一环。

这不是"造一个比 BLAS 快的库"——在 15W 笔记本上做不到。任务是**把 Ch 11–16 的概念串成一条可测量的优化链**，并学会判断"收益来自哪里"。

## 任务规格

| 项目 | 要求 |
|---|---|
| 实现语言 | Python + TVM 0.27（`s_tir.Schedule`），单文件 |
| 输入 | float32 的 `A[N,N]`、`B[N,N]`，N ∈ {256, 512} |
| 输出 | `C = A @ B`，`C[N,N]` float32 |
| 档位 1 | **baseline**：`te.compute` + 默认 schedule，不做任何变换 |
| 档位 2 | **tiled**：`split` + `reorder`，tile 因子自己选 |
| 档位 3 | **tiled + vectorized**：在 tiled 基础上加 `vectorize` |
| 加练 | **parallel**：在档位 3 上加 `parallel`（多核） |
| 参照 | `numpy`（BLAS），单线程（`OPENBLAS_NUM_THREADS=1`）与多线程各记一次 |
| 交付 | 可运行脚本 + 一张"档位 × N × GFLOPS"表 + 误差 |

**验收标准（两条都要满足）**：

1. **数值对齐**：与 `a @ b` 的 `maxerr ≤ 1e-3`（float32 累加误差在 `1e-4` 量级；若到 `1e-2` 说明算错了，不是"精度问题"）。
2. **性能**：
   - 档位 2 至少比档位 1 快 **5×**；
   - 档位 3 的 tile 因子必须是你自己扫出来的，并能解释为什么选它；
   - 加练档位在档位 3 之上至少再快 **1.5×**。

> 性能数字必须注明机器和负载状态。本练习的所有实测都来自：Intel i7-10510U（4C/8T，AVX2）、Windows 11、TVM 0.27.0.post1，测量时机器上有其他负载。

## 先建立直觉：四档的循环结构长什么样

```text
baseline                tiled                     vectorized / parallel
----------------------- ------------------------- ---------------------------
for i in 0..N           for i_o in 0..N/T         for i_o     (parallel 可选)
  for j in 0..N           for j_o in 0..N/T         for j_o
    for k in 0..N           for k_o in 0..N/T         for k_o
                              for i_i in 0..T          for i_i
                                for j_i in 0..T          for k_i
                                  for k_i in 0..T          for j_v  (vector 8)
                                                             for j_r
```

四档的区别只有三件事：**循环怎么切、按什么顺序排、内层怎么执行**。语义完全不变，性能差 20 倍。

## 测试用例与验收标准

先把"怎么判断对错、怎么测时间"固定下来，后面每一档都套同一把尺子：

```python
import statistics
import time

import numpy as np
import tvm
from tvm import te


def make_inputs(N, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((N, N), dtype="float32"),
            rng.random((N, N), dtype="float32"))


def to_tvm(a, b):
    return (tvm.runtime.tensor(a, tvm.cpu()),
            tvm.runtime.tensor(b, tvm.cpu()),
            tvm.runtime.empty(a.shape, "float32", tvm.cpu()))


def check(f, N, atol=1e-3):
    a, b = make_inputs(N)
    ta, tb, tc = to_tvm(a, b)
    f(ta, tb, tc)
    err = float(np.abs(tc.numpy() - a @ b).max())
    assert err <= atol, f"数值不达标: maxerr={err:.3e} > {atol}"
    return err


def gflops(f, N, rounds=5):
    a, b = make_inputs(N)
    ta, tb, tc = to_tvm(a, b)
    number = max(3, int(5e8 / (2 * N ** 3)))     # 小矩阵多跑几次，大矩阵少跑
    samples = []
    for _ in range(rounds):
        tev = f.time_evaluator(f.entry_name, tvm.cpu(), number=number, repeat=1)
        samples.append(tev(ta, tb, tc).min)
    t = statistics.median(samples)               # 中位数抗负载抖动
    return 2 * N ** 3 / t / 1e9


def blas_gflops(N, rounds=3):
    a, b = make_inputs(N)
    a @ b                                        # 预热
    reps = max(2, int(5e8 / (2 * N ** 3)))
    best = float("inf")
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(reps):
            a @ b
        best = min(best, (time.perf_counter() - t0) / reps)
    return 2 * N ** 3 / best / 1e9
```

三个设计决定，每个都有理由：

- `number` 随 N 变：N=256 一次只要几十毫秒，多跑几次才有统计意义；N=512 的 naive 一次要半秒，跑多了只是浪费时间。
- 取 `min` 再取中位数：`min` 过滤掉被系统调度打断的样本，中位数抵抗整段时间的负载漂移。
- `check` 先跑再测：保证你测的是**正确**的 kernel，不是一段跑得快但算错的代码。

## 分步骤

### Step 0：骨架与基线

一份 `te.compute`，两个档位只差 schedule：

```python
def build_matmul(N, kind="baseline"):
    A = te.placeholder((N, N), name="A")
    B = te.placeholder((N, N), name="B")
    k = te.reduce_axis((0, N), name="k")
    C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
    sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))

    if kind == "baseline":
        return tvm.build(sch.mod, target="llvm")

    bi, bj, bk = sch.get_loops(sch.get_sblock("C"))
    i_o, i_i = sch.split(bi, factors=[None, 16])
    j_o, j_i = sch.split(bj, factors=[None, 16])
    k_o, k_i = sch.split(bk, factors=[None, 16])
    sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)
    return tvm.build(sch.mod, target="llvm")
```

跑一下：

```python
for kind in ("baseline", "tiled"):
    f = build_matmul(256, kind)
    print(kind, check(f, 256), gflops(f, 256))
```

实测：

```text
baseline 6.87e-05 0.95
tiled    6.87e-05 9.81
```

tiling 一步就是 **10.3×**。误差完全一样（`6.87e-05`）——因为累加顺序没变，只是执行顺序变了。

### Step 1：先对齐，再提速

很多人跳过这一步，直接调参，最后拿到一个"很快但答案错"的 kernel。正确顺序是：

1. `check(f, N)` 通过；
2. 再 `gflops(f, N)`。

如果 `maxerr` 到了 `1e-2` 量级，按这个顺序排查：

- 是不是把 `k` 的 reduce 轴切了却没写回（`T.init()` 只在第一次迭代执行）；
- 是不是 `reorder` 后 `k_i` 跑到 `i_i` 外面，导致每个 i 重复累加；
- 是不是传错了缓冲区顺序（`A, B, C` vs `A, C, B`）。

### Step 2：理解 `reorder` 的顺序

`sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)` 读作：

```text
对每个 (i 块, j 块):
    对每个 k 块:
        在块内做 T×T×T 的乘加
```

如果把 `k_o` 放到 `j_o` 前面，就是"对每个 i 块、每个 k 块，遍历所有 j 块"——C 的同一块会被反复换入换出，性能立刻掉下来。**顺序不是风格问题，是内存访问模式问题。**

验证方法：打印 TIR，确认 `for` 的嵌套层次和你脑子里的一致。

```python
print(sch.mod.script())
```

### Step 3：扫 tile 因子，找到你机器上的拐点

固定 N=512，扫 8/16/32/64：

```text
tile=  8:  10.30 GFLOPS
tile= 16:  14.23 GFLOPS
tile= 32:   1.38 GFLOPS     ← 悬崖
tile= 64:   1.21 GFLOPS
```

16→32 掉 10 倍。原因不是 cache（32×32×4B×3 = 12KB，仍能进 32KB 的 L1），而是 **LLVM 不再自动向量化**：

```text
tile=16: IR 里含 " x float>" 的行 173 条，fmuladd 65 处
tile=32: IR 里含 " x float>" 的行   0 条，fmuladd  3 处
```

**结论：tile 因子必须在你的机器、你的 TVM 版本上实测。** 抄"32 或 64"可能正好踩在悬崖上。

### Step 4：加向量化

把 j 切成 64，内层再切出 8 宽做向量化：

```python
    bi, bj, bk = sch.get_loops(sch.get_sblock("C"))
    i_o, i_i = sch.split(bi, factors=[None, 16])
    j_o, j_i = sch.split(bj, factors=[None, 64])
    k_o, k_i = sch.split(bk, factors=[None, 16])

    j_v, j_r = sch.split(j_i, factors=[None, 8])
    sch.reorder(i_o, j_o, k_o, i_i, k_i, j_v, j_r)
    sch.vectorize(j_r)
```

注意 `reorder` 里 **`j_r` 在最内层、`k_i` 在它外面**：这样 `B[k, j]` 和 `C[i, j]` 都是连续访问，`A[i, k]` 退化成标量广播。这是矩阵乘微内核的标准形状。

实测（N=512）：

```text
tiled      10.37 GFLOPS
vectorized  7.05 GFLOPS
```

**显式向量化反而更慢。** 这不是 bug，而是本练习最值得记住的一课：tile=16 的 tiled 版本里，LLVM 已经把 j 循环自动向量化了（173 条向量指令）；你手动改写的循环顺序（k 进到 j 里面）虽然让访存更"教科书"，却破坏了自动向量化原本的展开和调度。

**但这样比是不公平的**——这一档同时改了 `j` 的 tile（16 → 64）。要判断 `vectorize` 本身的收益，必须固定其他因子。把两档都设成 `j tile = 64`，同一轮内交错测两次：

```text
                tiled(j=64)      vectorized(j=64)
N=256        16.17 / 16.88        14.51 / 15.06
N=512        14.73 / 14.76         8.14 / 11.73
```

这两行来自同一时段的配对测量；绝对值比上表高，是机器负载不同，**只能横向比较同一行**。

结论和直觉相反：**即使固定了 j tile，显式 `vectorize` 仍然没有赢**——N=256 慢 8–11%，N=512 慢 20–45%。原因是 `reorder` 把 `k_i` 挪到了 `j_r` 外面，虽然访存更"教科书"，却打乱了 LLVM 对 16×16 tile 的自动向量化与调度；手动写的 `T.vectorized(8)` 反而把更宽的自动向量化挡掉了。

**这不是失败，是负结果。** 它说明"tiled + vectorize"不是递进关系：在本机这套 schedule 形状下，tile 已经让 LLVM 把该做的向量化做完了，再加一层显式向量化只会改变循环结构、增加约束。想验证向量化本身，得换一个 LLVM 不会自动向量化的形状（比如非连续访存），或者用 Ch 15 的 `+avx2,+fma` target 重新编译两档对比。

**方法论结论：一次只改一个变量。** 想证明"vectorize 有用"，就固定 tile；想证明"tile 有用"，就固定 schedule 原语。混在一起改，你只能得到一个无法归因的数字。

### Step 5：加并行（加练）

在档位 3 基础上只加一行：

```python
    sch.parallel(i_o)
```

实测（N=512）：

```text
vectorized 7.05 GFLOPS
parallel  18.71 GFLOPS     ← 2.7×（本次；重跑会落到 1.4–2.7×）
```

8 线程只换来 1.4–2.7 倍（重跑时向量化基线在 7–12 GFLOPS 之间波动），原因：4 核 8 线程共享执行单元；每个核还要读同一份 B，L3 带宽成为新瓶颈。**这就是 Ch 16 说的"并行会把瓶颈从 ALU 推到内存"。**

### Step 6：和 BLAS 对比

```python
print("blas(1t)", blas_gflops(512))
```

实测（N=512，单线程）：

```text
blas(1t) 28.75 GFLOPS
```

差距约 1.5–4 倍（取决于机器负载）。你缺的那部分，就是 Ch 16 说的**寄存器分块**：BLAS 让一个 8×8 的 C 子块常驻寄存器，一个 A 元素复用于 8 次 FMA；TVM 的这套 schedule 没有做这一步。

## 参考实现

<details>
<summary>完整脚本（可直接运行，输出见下）</summary>

```python
"""练习 C 参考实现：matmul 的 baseline / tiled / vectorized / parallel 四档。"""
import statistics
import time

import numpy as np
import tvm
from tvm import te


def build_matmul(N, kind="baseline"):
    A = te.placeholder((N, N), name="A")
    B = te.placeholder((N, N), name="B")
    k = te.reduce_axis((0, N), name="k")
    C = te.compute(
        (N, N),
        lambda i, j: te.sum(A[i, k] * B[k, j], axis=k),
        name="C",
    )
    sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))

    if kind == "baseline":
        return tvm.build(sch.mod, target="llvm")

    bi, bj, bk = sch.get_loops(sch.get_sblock("C"))
    i_o, i_i = sch.split(bi, factors=[None, 16])
    # tiled 用 16；vectorized/parallel 用 64，给 8 宽向量留出整除空间
    j_o, j_i = sch.split(bj, factors=[None, 64 if kind != "tiled" else 16])
    k_o, k_i = sch.split(bk, factors=[None, 16])

    if kind == "tiled":
        sch.reorder(i_o, j_o, k_o, i_i, j_i, k_i)
    else:  # vectorized / parallel
        j_v, j_r = sch.split(j_i, factors=[None, 8])
        sch.reorder(i_o, j_o, k_o, i_i, k_i, j_v, j_r)
        sch.vectorize(j_r)
        if kind == "parallel":
            sch.parallel(i_o)

    return tvm.build(sch.mod, target="llvm")


def make_inputs(N, seed=0):
    rng = np.random.default_rng(seed)
    a = rng.random((N, N), dtype="float32")
    b = rng.random((N, N), dtype="float32")
    return a, b


def to_tvm(a, b):
    return (
        tvm.runtime.tensor(a, tvm.cpu()),
        tvm.runtime.tensor(b, tvm.cpu()),
        tvm.runtime.empty(a.shape, "float32", tvm.cpu()),
    )


def check(f, N, atol=1e-3):
    a, b = make_inputs(N)
    ta, tb, tc = to_tvm(a, b)
    f(ta, tb, tc)
    err = float(np.abs(tc.numpy() - a @ b).max())
    assert err <= atol, f"数值不达标: maxerr={err:.3e} > {atol}"
    return err


def gflops(f, N, rounds=5):
    a, b = make_inputs(N)
    ta, tb, tc = to_tvm(a, b)
    number = max(3, int(5e8 / (2 * N ** 3)))
    samples = []
    for _ in range(rounds):
        tev = f.time_evaluator(f.entry_name, tvm.cpu(), number=number, repeat=1)
        samples.append(tev(ta, tb, tc).min)
    t = statistics.median(samples)
    return 2 * N ** 3 / t / 1e9


def blas_gflops(N, rounds=3):
    a, b = make_inputs(N)
    a @ b
    reps = max(2, int(5e8 / (2 * N ** 3)))
    best = float("inf")
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(reps):
            a @ b
        best = min(best, (time.perf_counter() - t0) / reps)
    return 2 * N ** 3 / best / 1e9


if __name__ == "__main__":
    kinds = ["baseline", "tiled", "vectorized", "parallel"]
    for N in (256, 512):
        print(f"=== N={N} ===")
        for kind in kinds:
            f = build_matmul(N, kind)
            err = check(f, N)
            g = gflops(f, N)
            print(f"  {kind:<12} {g:6.2f} GFLOPS   maxerr={err:.2e}")
        print(f"  {'blas(1t)':<12} {blas_gflops(N):6.2f} GFLOPS")
```

运行（PowerShell）：

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:OPENBLAS_NUM_THREADS = "1"
& .scratch\aic-env\Scripts\python.exe practice_c.py
```

本机实测输出：

```text
=== N=256 ===
  baseline       0.95 GFLOPS   maxerr=6.87e-05
  tiled          9.81 GFLOPS   maxerr=6.87e-05
  vectorized     8.33 GFLOPS   maxerr=6.87e-05
  parallel      22.07 GFLOPS   maxerr=6.87e-05
  blas(1t)      33.75 GFLOPS
=== N=512 ===
  baseline       0.82 GFLOPS   maxerr=1.83e-04
  tiled         10.37 GFLOPS   maxerr=1.83e-04
  vectorized     7.05 GFLOPS   maxerr=1.83e-04
  parallel      18.71 GFLOPS   maxerr=1.83e-04
  blas(1t)      28.75 GFLOPS
```

</details>

## 实测结果与解读

| 档位 | N=256 | N=512 | 相对 baseline |
|---|---|---|---|
| baseline | 0.95 | 0.82 | 1× |
| tiled (16³) | 9.81 | 10.37 | **~12×** |
| vectorized | 8.33 | 7.05 | ~9× |
| parallel | 22.07 | 18.71 | **~25×** |
| BLAS（1 线程） | 33.75 | 28.75 | ~35× |

四条结论：

1. **tiling 是最大的一档收益**（10×），因为它把工作集从 MB 级压到 KB 级。
2. **显式 vectorize 在这套 schedule 上是负收益**：tiled 已经被 LLVM 自动向量化，固定 `j tile=64` 后显式向量化仍慢 8–45%。归因之前先固定变量，并接受负结果。
3. **parallel 是第二档收益**（2–2.7×），受限于物理核数和共享带宽。
4. **和 BLAS 的 1.5–4 倍差距**来自寄存器分块和微内核展开，不是 schedule 写错了。

## 常见卡点

**1）`split` 的 factor 不整除会插入谓词**

N=256、tile=48 时，TIR 会变成：

```text
for i_0, j_0, k_0, i_1, j_1, k_1 in T.grid(6, 6, 6, 48, 48, 48):
    T.where(i_0 * 48 + i_1 < 256 and j_0 * 48 + j_1 < 256 and k_0 * 48 + k_1 < 256)
```

结果仍然正确（实测 `maxerr=7.6e-05`），但每次迭代都多一次谓词判断，而且会破坏向量化。**优先选能整除 N 的因子。**

**2）`reorder` 顺序写反**

把 `k_o` 放到 `j_o` 前面、或把 `k_i` 放到 `j_i` 里面，都会改变 C 的复用模式。症状是"误差对，但慢 5–10 倍"。用 `print(sch.mod.script())` 逐层核对。

**3）`time_evaluator` 用错对象**

```python
f.time_evaluator(f.entry_name, tvm.cpu(), number=10, repeat=3)   # Module：对
m["main"].time_evaluator(...)                                     # Function：没有这个方法
```

如果是从 `load_module` 拿到的模块，用 `m.time_evaluator("main", ...)`。另外 `number` 是"每次 repeat 内调用几次"，总调用次数是 `number × repeat`。

**4）用 Python 循环测时间**

每次 Python 调用有 0.4–0.5 us 固定开销（Ch 17）。对 N=256 的 kernel（约 3ms）可以忽略，但对小 kernel 会污染结果。**统一用 `time_evaluator`。**

**5）缓冲区顺序传错**

`f(ta, tb, tc)` 的顺序必须和 `te.create_prim_func([A, B, C])` 一致。传错不会崩，只会算出一个"看起来像随机数"的结果——所以必须先 `check` 再测速。

**6）忘了关 BLAS 多线程**

不设 `OPENBLAS_NUM_THREADS=1`，你是在拿 4 核 8 线程的 BLAS 比单线程 TVM，差距会被夸大 2–4 倍。

**7）在 tile=32 上调试一下午**

如果照抄"tile=32"发现比 tile=16 慢 10 倍，先数 IR 里的向量指令，而不是怀疑 cache。

## 进阶挑战

1. 把 `float32` 换成 `float64`，重跑四档，解释为什么相对 baseline 的加速比几乎不变、但绝对 GFLOPS 减半。
2. 在 tiled 版本上加 `sch.unroll(k_i)`，看它和 `vectorize(j_r)` 谁更有效。
3. 用 TVM 的 `mattr=["+avx2","+fma"]` target 重编译四档，记录每一档的增益（Ch 15 的方法）。
4. 把 i/j tile 设成不同值（如 8×64），扫一遍，画出二维热力图，找出比 16×64 更好的组合。
5. 把 N 提到 1024，观察 naive 版本为什么会慢到不可用（提示：B 的重复扫描）。

## 自我检核

- [ ] 我的 baseline 和 tiled 用的是同一份 `te.compute`，只改了 schedule
- [ ] 每一档都先通过 `check`（maxerr ≤ 1e-3）再测速
- [ ] 我能画出四档的循环嵌套结构，并解释每一层的作用
- [ ] 我的 tile 因子是实测扫出来的，不是抄的
- [ ] 我能解释为什么"显式 vectorize 更快还是更慢"取决于跟谁比（固定变量的对照实验）
- [ ] 我知道 parallel 的扩展比为什么不到 8×
- [ ] 我能说出和 BLAS 的差距来自哪一级优化
- [ ] 我的测量用了 `time_evaluator` + 多轮中位数，并注明了机器状态

## 延伸阅读

- **TVM 官方 "Schedule Primitives" 教程**（https://tvm.apache.org/docs/tutorials/）：对照 `split`/`reorder`/`vectorize`/`parallel` 的官方示例。与本章的关联：API 语义的权威版本（注意 0.27 的 `s_tir` 路径）。
- **BLIS: "Anatomy of High-Performance Matrix Multiplication" (Goto & van de Geijn, TOMS 2008)**：读第 2–4 节。与本章的关联：为什么寄存器分块能把 10 GFLOPS 变成 30+。
- **Ansor 论文 (OSDI 2020) 第 4 节**：看搜索空间如何枚举 tile/vectorize/parallel 的组合。与本章的关联：手工扫描的自动化版本。
- **OpenBLAS 的 `sgemm_kernel` 汇编**（https://github.com/OpenMathLib/OpenBLAS）：搜 `sgemm_kernel_8x8`。与本章的关联：BLAS 微内核的寄存器布局。
- **Ch 16 的 Roofline 一节**：与本章的关联：判断"还能不能更快"的尺子。

---

→ [Ch 18 TorchInductor：从 FX 到 Triton/C++](./18-torchinductor.md)
