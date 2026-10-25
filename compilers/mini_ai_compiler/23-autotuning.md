# 第 23 章 自动调优：分块（tile）搜索、成本模型与测量噪声

> **目标**：读懂 `examples/autotune_matmul.py` 的 8 组分块（tile）搜索；能解释 `ti/tj/tk` 如何影响 缓存（cache）工作集、循环开销和边界分支；能用真实测量说明「同一脚本、同一机器、两次运行排名不同」；能写出一个粗成本模型并指出它的局限；能用 median/min/max、交错测量和 1.5 倍阈值判断一次调优结果是否可信。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本章两组 8 配置结果、tile=24 边界结果和耗时均来自本机实际运行；更早一次运行的结果单独标注来源。

## 为什么需要自动调优

Ch 18 讲过 matmul 的三重循环和 tiling。那时 tile 大小是手选的。手选的问题是：你不知道 16 还是 32 更好，也不知道换一个 shape、换一台 CPU 后结论是否还成立。

分块（tile）大小同时影响三件事：

1. **缓存（cache）工作集**：一个 tile 需要读 A 的 `ti x tk`、B 的 `tk x tj`、写 C 的 `ti x tj`。tile 太大，工作集超过 L1/L2，cache miss 增加；tile 太小，循环开销和重复加载增加。
2. **循环开销**：tile 越多，外层循环、边界判断、地址计算越多。8x8x8 有 512 个 tile 迭代，64x64x64 只有 1 个。
3. **边界分支**：当维度不能被 tile 整除时，每个 tile 都要算 `min(ii+ti, 64)`，多出比较和分支。整除时这些分支仍然存在，只是结果固定。

自动调优（autotuning）把「选哪个 tile」变成一个搜索问题：枚举候选配置，编译、运行、测时间、验正确性，再按某个统计量排序。它不保证找到全局最优，但能把「凭感觉」变成「有数据、有边界、可复现」的决策。

本课的搜索很小：8 个配置，固定 64x64x64，单线程，C 后端。它足够展示完整流程，也足够暴露测量噪声。真正重要的是方法：怎么定义搜索空间、怎么测、怎么判断赢家、什么时候承认「这组数据不足以区分」。

## 先建立直觉

一次 autotune 的流程：

```text
候选 tile 列表
  (64,64,64) (32,32,32) (16,16,16) (32,64,32)
  (64,32,32) (16,32,16) (32,16,32) (8,8,8)
        |
        |  for each config
        v
生成 C kernel（tile 参数写进源码）
        |
        |  compile_c -> cl /LD /O2 /fp:fast
        v
DLL + build.log
        |
        |  load_c_function
        v
运行一次，和 NumPy 的 a @ b 比较 maxerr
        |
        |  maxerr <= 阈值?
        v
benchmark(warmup=3, repeat=20) -> median/min/max
        |
        v
按 median 排序 -> 选最小
```

搜索空间是一张表：

```text
ti x tj x tk        外层 tile 数          单 tile 工作集（float32）
64 x 64 x 64        1 x 1 x 1 = 1         3 * 4096 * 4 = 49152 B
32 x 32 x 32        2 x 2 x 2 = 8         3 * 1024 * 4 = 12288 B
16 x 16 x 16        4 x 4 x 4 = 64        3 *  256 * 4 =  3072 B
 8 x  8 x  8        8 x 8 x 8 = 512       3 *   64 * 4 =   768 B
```

工作集按 `(ti*tk + tk*tj + ti*tj) * 4` 估算。它只算一个 tile 的 A/B/C 三块，不含索引和临时变量。64 的 tile 工作集 48 KB，超过典型 32 KB L1；16 的 tile 3 KB，能放进 L1；8 的 tile 768 B，放得更稳，但 tile 迭代数是 16 的 8 倍。

直觉上，16 或 8 应该比 64 快，因为它们改善了 cache 复用、减少了 B 的重复扫描。实际测量里，8x8x8 在两次运行中都是最快，但更早一次运行里 16x16x16 最快。排名不稳定，正是本章后半部分的主题。

## 核心内容

### 1. kernel 模板：tile 参数如何进入 C

`autotune_matmul.py` 用 f-string 生成 C：

```python
def kernel(ti: int, tj: int, tk: int) -> str:
    return f'''#include <stddef.h>
...
MINI_EXPORT int mini_forward(const float* A, const float* B, float* C) {{
    for (int i = 0; i < 64; ++i) for (int j = 0; j < 64; ++j) C[i*64+j] = 0.0f;
    for (int ii = 0; ii < 64; ii += {ti}) {{
        for (int jj = 0; jj < 64; jj += {tj}) {{
            for (int kk = 0; kk < 64; kk += {tk}) {{
                int i_end = ii + {ti} < 64 ? ii + {ti} : 64;
                int j_end = jj + {tj} < 64 ? jj + {tj} : 64;
                int k_end = kk + {tk} < 64 ? kk + {tk} : 64;
                for (int i = ii; i < i_end; ++i) {{
                    for (int j = jj; j < j_end; ++j) {{
                        float acc = C[i*64+j];
                        for (int k = kk; k < k_end; ++k) acc += A[i*64+k] * B[k*64+j];
                        C[i*64+j] = acc;
                    }}
                }}
            }}
        }}
    }}
    return 0;
}}
'''
```

这段 kernel 有四个特点：

1. **固定 shape 64x64x64**。所有循环上界写死，编译器能做常量传播。
2. **C 先清零**。因为 tile 循环按 k 分块累加，每个 `acc` 从 `C[i*64+j]` 读回上一块的累加结果。
3. **边界保护**。`i_end/j_end/k_end` 用 `min` 形式，即使 tile 不整除 64 也能正确执行。搜索列表里的 tile 都整除 64，所以分支结果固定，但代码仍然保留。
4. **单线程、无向量化提示**。MSVC 的 `/O2` 可能自动向量化内层循环，但没有手写 SIMD、没有 `restrict`、没有对齐假设。

ti/tj/tk 分别控制 i、j、k 三个维度的块大小。i 是输出行，j 是输出列，k 是归约维。tk 决定每次把多少 A 的列和 B 的行带进 cache；ti/tj 决定输出块的大小。

### 2. 搜索与测量循环

主循环：

```python
configs = [(64, 64, 64), (32, 32, 32), (16, 16, 16), (32, 64, 32),
           (64, 32, 32), (16, 32, 16), (32, 16, 32), (8, 8, 8)]
results = []
for ti, tj, tk in configs:
    build = Path(".scratch/mini-ai-autotune") / f"t{ti}_{tj}_{tk}"
    result = compile_c(kernel(ti, tj, tk), build, name="matmul")
    fn = load_c_function(result.dll_path, 2, 1)
    out = fn([a, b], [(64, 64)])[0]
    err = float(np.max(np.abs(out - ref)))
    stats = benchmark(lambda: fn([a, b], [(64, 64)]), warmup=3, repeat=20)
    results.append(((ti, tj, tk), stats["median_ms"], err))
    print(f"tile=({ti:2d},{tj:2d},{tk:2d}) median_ms={stats['median_ms']:.4f} maxerr={err:.3e}")
best = min(results, key=lambda x: x[1])
```

每个配置都走一遍「编译 → 加载 → 正确性 → 计时」。几个工程细节：

- `load_c_function` 只加载一次 DLL，返回的 `fn` 在 `benchmark` 闭包里复用。这比 demo 里每次 `run_c_graph` 重新加载更接近 kernel 时间。
- `fn([a, b], [(64, 64)])` 每次都会在 `run` 内部创建输出数组、调用 `_f32` 做 `ascontiguousarray`。计时包含这部分 Python/ctypes 开销。
- 先跑一次 `out` 做正确性检查，再 `benchmark` 做 warmup=3、repeat=20。正确性检查那次也会预热 cache 和分支预测。
- 排序键是 `median_ms`，不是 `min_ms`。这是本章后面要强调的选择。
- `maxerr` 和耗时一起记录，避免「快但错」的配置混进排名。

`benchmark` 的实现：

```python
def benchmark(fn, *, warmup=5, repeat=30):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "repeat": float(repeat),
    }
```

它返回三个统计量。median 是主指标，min 是最佳情况，max 用于诊断噪声。只看 median 会漏掉长尾；只看 min 会把一次幸运采样当成稳定性能。

### 3. 8 组真实结果

运行命令：

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& ".scratch\aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\autotune_matmul.py"
```

第一次运行（在 `.scratch\autotune-rerun` 工作目录执行，build 路径相对该目录）：

```text
tile=(64,64,64) median_ms=0.5436 maxerr=5.722e-06
tile=(32,32,32) median_ms=0.4625 maxerr=5.722e-06
tile=(16,16,16) median_ms=0.4792 maxerr=5.722e-06
tile=(32,64,32) median_ms=0.4465 maxerr=5.722e-06
tile=(64,32,32) median_ms=0.4909 maxerr=5.722e-06
tile=(16,32,16) median_ms=0.4298 maxerr=5.722e-06
tile=(32,16,32) median_ms=0.4051 maxerr=5.722e-06
tile=( 8, 8, 8) median_ms=0.2780 maxerr=5.722e-06
best: (8, 8, 8) median_ms= 0.278
```

第二次运行（另一个工作目录，独立编译）：

```text
tile=(64,64,64) median_ms=0.5220 maxerr=5.722e-06
tile=(32,32,32) median_ms=0.4999 maxerr=5.722e-06
tile=(16,16,16) median_ms=0.5010 maxerr=5.722e-06
tile=(32,64,32) median_ms=0.4605 maxerr=5.722e-06
tile=(64,32,32) median_ms=0.2889 maxerr=5.722e-06
tile=(16,32,16) median_ms=0.2464 maxerr=5.722e-06
tile=(32,16,32) median_ms=0.2773 maxerr=5.722e-06
tile=( 8, 8, 8) median_ms=0.2421 maxerr=5.722e-06
best: (8, 8, 8) median_ms= 0.2421
```

把两次并排：

| tile (ti,tj,tk) | 运行 A median ms | 运行 B median ms | 变化 | maxerr |
| --- | ---: | ---: | ---: | ---: |
| (64,64,64) | 0.5436 | 0.5220 | -4.0% | 5.722e-06 |
| (32,32,32) | 0.4625 | 0.4999 | +8.1% | 5.722e-06 |
| (16,16,16) | 0.4792 | 0.5010 | +4.5% | 5.722e-06 |
| (32,64,32) | 0.4465 | 0.4605 | +3.1% | 5.722e-06 |
| (64,32,32) | 0.4909 | 0.2889 | -41.1% | 5.722e-06 |
| (16,32,16) | 0.4298 | 0.2464 | -42.7% | 5.722e-06 |
| (32,16,32) | 0.4051 | 0.2773 | -31.5% | 5.722e-06 |
| (8,8,8) | 0.2780 | 0.2421 | -12.9% | 5.722e-06 |

两次的最优都是 8x8x8，但多个配置的耗时变化超过 40%。这不是 kernel 改了，也不是编译器选项改了，而是测量条件变了：CPU 频率、后台进程、内存状态、DLL 加载、Windows  Defender 扫描都可能影响。更重要的是，两次运行里 8x8x8 相对 16x16x16 的优势分别是 1.72 倍和 2.07 倍，都超过了 1.5 倍阈值；而 16x16x16 和 32x32x32 的差异只有几个百分点，不能据此排序。

项目 brief 记录的更早一次运行里，8 组结果是：

```text
tile=(64,64,64) 0.4267 ms maxerr=5.722e-06
tile=(32,32,32) 0.2677 ms
tile=(16,16,16) 0.2132 ms  <- best
tile=(32,64,32) 0.3561 ms
tile=(64,32,32) 0.2826 ms
tile=(16,32,16) 0.2289 ms
tile=(32,16,32) 0.3157 ms
tile=( 8,8,8)   0.2251 ms
```

那一次的最优是 16x16x16 0.2132 ms，8x8x8 是 0.2251 ms，相差 5.6%，低于 1.5 倍阈值。也就是说，在噪声范围内，这两个配置无法区分。课程保留这组数据，是因为它正好说明：**同一份代码、同一台机器，不同时间的最优配置可以不同。** 只报告「best: (16,16,16)」而不报告测量条件和重复次数，会误导读者。

### 4. 成本模型：先算工作集，再算循环开销

一个粗成本模型可以写成：

```text
cost(ti,tj,tk) =
    alpha * tile_count
  + beta  * cache_miss_estimate(ti,tj,tk)
  + gamma * boundary_branch_count
  + delta * flop_count
```

其中：

- `tile_count = (64/ti) * (64/tj) * (64/tk)`。
- `cache_miss_estimate` 可以用工作集是否超过 L1/L2 做分段函数，也可以用更细的 reuse distance 模型。
- `boundary_branch_count` 在维度整除时为 0 或固定开销；不整除时随 tile 数增长。
- `flop_count = 2 * 64^3 = 524288`，与 tile 无关，是常数项；它决定下限，不决定排序。

用工作集算算术强度（arithmetic intensity）：

```text
AI(ti,tj,tk) = 2 * ti * tj * tk / ((ti*tk + tk*tj + ti*tj) * 4)   FLOP/byte
```

代入几组：

| tile | 工作集字节 | AI (FLOP/B) | tile 数 |
| --- | ---: | ---: | ---: |
| 64x64x64 | 49152 | 10.67 | 1 |
| 32x32x32 | 12288 | 5.33 | 8 |
| 16x16x16 | 3072 | 2.67 | 64 |
| 8x8x8 | 768 | 1.33 | 512 |

单看 AI，64 的 tile 最高：每字节内存支撑更多计算，理论上更适合带宽受限的场景。但 48 KB 工作集超过 32 KB L1，实际会落到 L2；而 8x8x8 的 768 B 完全在 L1，虽然 AI 低，但每次 tile 的数据几乎都能命中。实测 8x8x8 更快，说明在这个单线程、小矩阵、MSVC 自动向量化的场景里，L1 命中比理论 AI 更重要。

成本模型的用途不是精确预测毫秒，而是**剪枝**：如果某个 tile 的工作集远超 L2，或者 tile 数大到循环开销明显，就可以先排除，不必每个都编译测量。模型排序和实测排序不一致时，不要强行拟合；把差异记下来，作为下一轮搜索的先验。

### 5. 测量噪声：同一配置的分布

用 tile=(24,24,24) 做一次边界实验。24 不整除 64，`i_end/j_end/k_end` 会真正走到 `min` 分支。实际输出：

```text
tile=(24,24,24) median_ms=0.4750 maxerr=5.722e-06
stats: {'median_ms': 0.475, 'min_ms': 0.2908, 'max_ms': 0.6502, 'repeat': 20.0}
```

同一个配置，20 次采样的最小值 0.2908 ms，最大值 0.6502 ms，相差 2.24 倍。median 0.4750 ms 落在中间，但如果只报告 min，你会以为它比 8x8x8 的 0.2421 ms 只慢 20%；如果只报告 max，你会以为它慢 2.7 倍。两种结论都不完整。

噪声来源：

1. **CPU 频率变化**：短时间突发负载会触发 turbo，随后降频；20 次采样跨越了频率变化。
2. **后台进程**：浏览器、杀毒、系统更新、其它 Python 进程会抢核心。
3. **内存状态**：首次访问的 page fault、cache 冷启动、DLL 重定位。
4. **测量开销**：`fn` 内部创建输出数组、`ascontiguousarray` 检查、ctypes 调用，这些不在 kernel 里，但在计时区间里。
5. **编译与加载顺序**：先测的配置可能享受冷 cache，后测的配置可能被前面的内存布局影响。

降低噪声的做法：

- **warmup**：先跑若干次，让频率、cache、分支预测稳定。当前 warmup=3 偏少，MLP demo 用 5。
- **repeat**：至少 20–30 次；小 kernel 可以 100 次。
- **交错测量**：不要把一个配置的 30 次跑完再跑下一个；按轮次交错，减少频率漂移对单个配置的偏置。
- **多轮取中位数**：跑 3–5 轮，每轮算 median，再对 median 取中位数。
- **报告分布**：至少给 median、min、max；有条件时给 quartile 或 bootstrap 置信区间。
- **固定环境**：关掉后台任务、固定电源计划、固定线程数、避免同时编译。
- **阈值**：差异小于 1.5 倍时，不宣称赢家；先增加重复次数或换测量方法。

为什么是 1.5 倍？这是一个工程阈值，不是统计定理。它来自经验：在普通笔记本上，短 kernel 的 run-to-run 波动经常到 20%–50%，1.5 倍能过滤掉大部分噪声，又不会要求不切实际的 10 倍差异。更严谨的做法是算置信区间；课程用 1.5 倍作为最低门槛。

### 6. 正确性门槛：先验错，再比快

8 组配置的 `maxerr` 全部是 `5.722e-06`。这个值来自 float32 的 64 长度点积，和 NumPy 的 `a @ b` 比较。不同 tile 的累加顺序不同，理论上误差会有细微差别；这组数据里恰好一致到打印精度。

调优流程必须先过正确性门槛：

```python
if err > 1e-4:
    print("reject", (ti, tj, tk), err)
    continue
```

如果某个 tile 因为边界写法错误导致结果错，但速度很快，直接按 median 排序会把它选为最优。正确性检查要放在计时之前，并且用固定随机种子、固定输入，保证不同配置比较的是同一道题。

更严格的检查包括：

- 用多组输入（随机、全零、全一、含负数、含 NaN）跑。
- 对每个输出元素比较，而不只是 maxabs；maxabs 可能被一个大误差主导。
- 用 `np.testing.assert_allclose` 的相对+绝对容差，避免大值掩盖小值错误。
- 保存每个配置的 `maxerr` 和 `build.log`，报告里附上。

### 7. 边界例子：不整除的 tile

搜索列表里的 tile 都整除 64，所以边界分支的结果固定。把 24 加进去，才能看到真实边界路径：

```text
tile=(24,24,24) median_ms=0.4750 maxerr=5.722e-06
stats: {'median_ms': 0.475, 'min_ms': 0.2908, 'max_ms': 0.6502, 'repeat': 20.0}
```

64 = 2*24 + 16，所以 i/j/k 三个方向各有 3 个 tile，最后一个 tile 只处理 16 行/列。`i_end = ii + 24 < 64 ? ii + 24 : 64` 在最后一个 tile 取 64，内层循环范围缩小。结果正确，但每个 tile 都多一次比较和一次三元选择；tile 数从 8x8x8 的 512 变成 3x3x3=27，单 tile 工作集 `3*24*24*4=6912` 字节，介于 16 和 32 之间。它的 median 0.4750 比 8x8x8 慢，但不能把原因只归给边界分支：工作集、tile 数、频率噪声都在里面。

这个例子的教学价值是：**tile 不整除时，代码仍然正确，但性能模型要多一项。** 如果你只测整除的 tile，部署到 100x100 的输入时会遇到完全不同的边界开销。生产 autotuner 会把 shape 作为搜索维度，或者为每个 shape 缓存最优配置。

## 底层机制：一次 tile 迭代的数据流

以 `ti=tj=tk=16` 为例，一次 `(ii,jj,kk)` 迭代做的是：

```text
for i in [ii, ii+16):
  for j in [jj, jj+16):
    acc = C[i,j]                    <- 读上一轮 k 块的累加结果
    for k in [kk, kk+16):
      acc += A[i,k] * B[k,j]        <- 读 A 的一行、B 的一列
    C[i,j] = acc                    <- 写回
```

内存访问模式：

```text
A: 第 i 行连续 16 个 float  -> 1 条 cache line 左右（64 B = 16 float）
B: 第 j 列跨 16 行，每行 1 个 float -> 16 条 cache line
C: 第 i 行连续 16 个 float -> 1 条 cache line
```

B 的列访问是跨步的，步长 64 个 float（256 字节）。每次读 `B[k*64+j]` 都会跳到下一条 cache line，空间局部性差。tiling 的作用是把 B 的一个 16x16 块反复使用：对 16 个 i 值，B 的同一列被读 16 次；如果块留在 L1，后续 15 次命中。tk 越大，B 块越大，L1 压力越高；tk 越小，A/B 的块被反复换入换出，重复加载增加。

MSVC 的 `/O2` 可能把内层 k 循环向量化。它需要：

- 连续访问：`A[i*64+k]` 随 k 连续，适合向量化。
- 对齐：A 的基址和行首不一定 64 字节对齐；MSVC 可能生成对齐检查或使用 unaligned load。
- 别名：A、B、C 都是 `float*`，编译器不能假设它们不重叠；如果开启 `/fp:fast` 和合适的 restrict，向量化机会更大。当前 kernel 没有 `restrict`，这是一个可改进点。

## 进一步用法：搜索策略的变体

**穷举（grid search）。** 当前脚本就是穷举 8 个点。优点是直接、可复现；缺点是维度一多就爆炸。ti/tj/tk 各 8 个候选就是 512 组，每组编译一次，时间不可接受。

**随机搜索。** 从候选集合随机采样 N 组，成本固定，覆盖比手工列表广。适合候选空间大、没有强先验的场景。缺点是可能错过窄的最优点，需要固定随机种子。

**坐标下降。** 先固定 tj/tk 搜 ti，再固定 ti/tk 搜 tj，再搜 tk，迭代几轮。成本低，容易陷入局部最优。

**模型引导。** 用成本模型或机器学习模型预测候选的性能，只测预测最好的若干组。Ansor 一类工作会用采样 + 代价模型 + 进化搜索。模型不准时，仍然需要实测校验。

**多目标。** 同时考虑运行时间、编译时间、代码体积、数值误差。小 tile 可能运行快但代码大、编译慢；大 tile 编译快但运行慢。最终选择取决于部署约束。

**按 shape 缓存。** 对每个 `(M,N,K)` 存一份最优配置。shape 变化时重新搜索或用邻近 shape 的配置热启动。生产 autotuner 通常把「搜索」和「缓存」分开。

## 对比与取舍

| 方法 | 成本 | 优点 | 缺点 | 适用 |
| --- | --- | --- | --- | --- |
| 手工启发式 | 最低 | 快、可解释 | 换机器/shape 可能失效 | 教学、固定部署 |
| 穷举 | 中 | 结果可复现 | 维度爆炸 | 小搜索空间 |
| 随机搜索 | 可控 | 覆盖广 | 不保证最优 | 中等空间 |
| 坐标下降 | 低 | 维度可扩展 | 局部最优 | 有强先验 |
| 模型引导 | 高 | 样本效率高 | 模型可能偏 | 大规模生产 |
| 纯 min 排序 | 低 | 看起来快 | 被幸运采样误导 | 不推荐 |
| median 排序 | 低 | 稳健 | 仍受多峰分布影响 | 课程默认 |

另一个取舍是「测多久」。repeat=20 对 0.2–0.5 ms 的 kernel 约 4–10 ms，成本很低；但对 100 ms 的大 kernel，20 次就是 2 秒，搜索 100 组就是 200 秒。生产环境要设编译/测量预算，先粗筛再精测。

## 踩雷集锦

1. **单次采样或单次脚本排名就下结论。** 一次运行无法区分 5% 的差异和噪声，同一脚本三次运行也可能给出不同的 best。至少 warmup + 20 次 repeat 并多轮重复；报告带机器、shape、编译选项、repeat 和统计量。
2. **用 min 作为主指标。** min 是「最幸运的一次」，会掩盖长尾和频率波动。用 median 做主指标，min/max 做诊断。
3. **不验正确性就排序。** 一个边界写错、速度很快的 tile 会被选为最优。先过 maxerr 门槛，再比时间。
4. **把编译时间算进运行时间。** `compile_c` 应该放在计时循环外；否则测到的是 cl 的启动和磁盘 I/O。反过来，报告总部署成本时要单独列编译时间。
5. **忽略测量闭包的内容。** `fn([a,b], ...)` 每次都分配输出数组、做 contiguous 检查。不同实现的闭包工作不同，绝对耗时不可直接比较。

## 进阶

- 把 `benchmark` 改成多轮交错：每轮按随机顺序测所有配置，记录每轮 median，最后对 median 取中位数。
- 用 bootstrap 重采样 20 个样本，给每个配置算 median 的 95% 置信区间；比较区间是否重叠。
- 用 `perf`/`VTune`/Windows Performance Recorder 看 L1/L2 miss、分支误预测，验证成本模型的假设。
- 把 tile 搜索扩展到 128x128x128 和 256x256x256，观察最优 tile 是否随 shape 变化。
- 给 kernel 加 `restrict` 和对齐提示，比较 MSVC 自动向量化前后的耗时；注意这会影响数值结果吗（通常不会，但 FMA 会）。
- 实现一个两阶段搜索：先用成本模型筛掉工作集超过 L2 的配置，再对剩余配置精测。
- 把搜索并行化，每个配置在独立进程编译和测量，避免 DLL 锁和相互干扰；记录进程亲和性。

## 动手练习

1. **复现 8 组。** 跑 `autotune_matmul.py` 两次，记录完整输出。比较两次的 best 和每个配置的变化，指出哪些差异超过 20%。
2. **加 tile=24。** 用 `kernel(24,24,24)` 编译并测量，记录 median/min/max 和 maxerr。解释为什么它能正确处理 64 不被 24 整除的情况。
3. **改统计量。** 把排序键从 median 改成 min，再改成 max，观察 best 是否变化。写一段话说明哪种排序更适合部署。
4. **交错测量。** 写一个循环：3 轮，每轮随机打乱 8 个配置，每个配置测 20 次。最后对每轮的 median 取中位数，比较和单轮结果的差异。
5. **成本模型。** 用工作集和 tile 数写一个简化打分函数，给 8 个配置排序。对比模型排序和实测排序，指出不一致的地方并解释。
6. **换 shape。** 把 kernel 的 64 改成 96，重新跑 8 组（候选里能整除 96 的是 32/16/8；24 也整除 96，可以加进去）。观察边界和最优配置如何变化。

## 本章重点

- autotune 的流程是：生成候选 kernel → 编译 → 正确性检查 → benchmark → 排序。
- tile 同时影响 缓存（cache）工作集、循环开销和边界分支；没有单一维度能解释全部性能。
- 8 组实测的 maxerr 都是 5.722e-06；两次运行 best 都是 8x8x8，但更早一次是 16x16x16。
- 同一配置的 20 次采样可以相差 2.24 倍（tile=24 的 min 0.2908 / max 0.6502）。
- median 是主指标，min/max 用于诊断；差异小于 1.5 倍时不要宣称赢家。
- 成本模型用于剪枝和解释，不用于替代测量；模型和实测不一致是正常现象。
- 正确性门槛必须先于性能排序；快而错的配置没有价值。

## 自我检核

- [ ] 我能解释 ti/tj/tk 各自控制哪个维度，以及它们如何影响工作集。
- [ ] 我能手算 8x8x8、16x16x16、64x64x64 的 tile 数和工作集。
- [ ] 我能说明为什么 64 的 tile 算术强度高但实测不一定最快。
- [ ] 我能解释 warmup、repeat、median、min、max 各自的作用。
- [ ] 我知道为什么差异小于 1.5 倍时不能下结论。
- [ ] 我能复现 tile=24 的边界结果，并说明 `i_end` 的作用。
- [ ] 我能列出至少四个噪声来源，并给出对应的控制方法。
- [ ] 我知道 autotune 的排序必须建立在正确性检查之后。

## 延伸阅读

- **Ansor 论文，OSDI 2020**（https://www.usenix.org/conference/osdi20/presentation/zheng）：读 Section 4–5 的搜索空间、代价模型和采样策略；对照本章的 8 点穷举，理解生产级 autotuner 多了哪些层次。
- **TVM 文档，AutoTVM 与 meta_schedule**（https://tvm.apache.org/docs/）：读模板、搜索空间、测量和日志格式；本章的 `kernel(ti,tj,tk)` 是它的极小化版本。
- **《Computer Architecture: A Quantitative Approach》第 2 章与附录 B**：读 cache 组织、局部性、miss 分类；用这些概念解释为什么 B 的列访问和 tile 工作集会决定性能。
- **Google Benchmark 用户指南**（https://github.com/google/benchmark/blob/main/docs/user_guide.md）：读 warmup、重复、统计量和噪声处理；本章的 `benchmark` 是它的简化版。
- **LLVM `opt` 的 `-passes` 与 `-print-after-all` 文档**（https://llvm.org/docs/CommandGuide/opt.html）：读 pass 流水线和 IR 打印；为 Ch 24 的调试和 Ch 22 的优化 pass 做铺垫。

---

[下一章：Ch 24 调试与差分测试](./24-debugging-and-differential-testing.md)