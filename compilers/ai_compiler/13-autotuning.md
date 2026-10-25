# Ch 13 自动调优：meta_schedule、搜索空间与实测

> **目标**：理解自动调优在搜什么、cost model 预测什么、实测反馈为什么不可替代；在本机用 20 秒以内的预算跑一次真实的网格搜索，拿到最佳 schedule、baseline 对比和调优成本；同时看清「自动调优不是银弹」的边界。

## 为什么需要这个？

Ch 11 手工试了 tiling、vectorize、parallel；Ch 12 知道了这些原语怎么改 TIR。但真实的 schedule 空间是组合爆炸的：

- `k` 切 8 还是 32？
- `j` 向量化 8 还是 16？
- `i` 并行 4 还是 8？
- `i/j` 要不要再切 2D tile？
- 循环顺序有几十种合法排列。

每个维度乘起来就是成百上千个候选。手工试只能覆盖很小一部分，而且很容易过拟合到「我刚才测的那一个 shape」。

自动调优（auto-tuning）要解决的就是：**在巨大的合法 schedule 空间里，用尽量少的实测，找到对目标 shape/硬件足够好的 schedule**。

## 先建立直觉：调优是「带噪声的优化」

把调优想成一个黑盒函数：

```text
f(schedule_config) -> 运行时间（带噪声）
```

它有三个特点：

1. **定义域是离散的、组合的**：不是连续超参数，不能求导。
2. **评估很贵**：每次都要编译 + 运行 + 计时。
3. **评估有噪声**：CPU 频率、后台负载、线程调度都会让同一个 config 测出不同时间。

所以自动调优不是「找一个数学最优解」，而是「在预算内找一个统计上够好的解」。这也解释了为什么 cost model 只能**排序候选**，不能替代实测。

## 核心概念一：搜索空间

搜索空间（search space / design space）是「所有允许的 schedule 配置」。它由两部分决定：

- **模板**：哪些循环可以 split、哪些可以 reorder、哪些可以 vectorize/parallel。
- **参数范围**：每个 split factor、vector width、parallel extent 取哪些值。

本章的手工网格搜索用三个维度：

| 维度 | 取值 | 含义 | 主要影响 |
|---|---|---|---|
| `bk` | 8, 16, 32 | k 方向的块大小，同时作为 unroll 因子 | 局部性 vs 代码体积 |
| `vj` | 8, 16 | j 方向的向量宽度 | SIMD 利用率 |
| `par` | 1, 4, 8 | i 方向的并行块数 | 线程数 vs 带宽 |

`3 × 2 × 3 = 18` 个配置。真实 meta_schedule 的 design space 比这大得多：它会自动生成 `split/reorder/parallel/vectorize/unroll/cache_read/...` 的组合，并去掉不合法的候选。

## 核心概念二：cost model 与实测反馈

自动调优系统通常有两张嘴：

- **cost model**：看 schedule 的特征（循环结构、tile 大小、向量宽度、buffer 访问），预测「这个候选大概多快」。它便宜，但不准。
- **runner**：真的编译、真的跑、真的计时。它准，但贵。

典型循环是：

```text
采样候选 -> cost model 排序 -> 选 top-k 实测 -> 用实测结果更新 cost model -> 再采样
```

cost model 的作用是**减少实测次数**，不是取消实测。冷启动时它没有数据，只能随机采样；随着实测记录增加，它才逐渐有用。如果实测噪声很大，cost model 也会学到噪声。

AutoTVM 的思路是「模板 + 配置空间 + tuner（grid/random/GA）+ cost model」；meta_schedule 把「模板」升级成「schedule 规则生成的 design space」，并用 evolutionary search 在空间里进化。两者共同的底层信念是：**性能必须用目标硬件上的实测来定义**。

### 探索与利用

自动调优也在做 explore/exploit 的权衡：

- 前期多随机采样（explore），避免一开始就被一个不准的 cost model 带偏。
- 后期多利用模型预测（exploit），把预算花在更可能变快的区域。
- 只利用不探索会卡在局部最优；只探索不利用就退化成随机搜索。

本机的 18 配置网格没有 cost model，属于纯 explore；它在 5 秒内覆盖了整个小空间，所以不需要模型。真实空间大到无法穷举时，模型才有价值。

## 核心概念三：数据库与可复现性

调优的结果不是一个 `Schedule` 对象，而是一堆「配置 → 实测时间」的记录，通常存在数据库里（JSON、内存、或自定义 backend）。有了数据库，你可以：

- 复现「为什么选这个 config」。
- 在同一台机器上增量调优。
- 把调优结果导出成日志，做回归比较。

代价是：数据库、work_dir、日志、缓存都要管理；换机器/换 TVM 版本/换 shape 后，旧记录可能不再适用。

### 为什么 shape 要分桶

同一个 schedule 在 N=256 和 N=257 上的表现可能差一个数量级（本章后面会看到 29× vs 3.1×）。所以数据库通常按 target、shape、dtype、layout 分桶；跨桶复用记录要做 transfer learning，而不是直接把时间复制过去。

## 底层机制：meta_schedule 的五个组件

TVM 0.27 的 `tvm.s_tir.meta_schedule` 把调优拆成五个可替换组件（`源码依据`：`s_tir/meta_schedule/`）：

| 组件 | 作用 | 本机可用性 |
|---|---|---|
| `SpaceGenerator` | 从 PrimFunc 生成 design space | 可用，但初始化需要 psutil |
| `SearchStrategy` | 在空间里采样/进化（`evolutionary` 等） | 可用 |
| `CostModel` | 预测候选性能（默认 `xgb`） | 需要 xgboost |
| `Runner` | 编译并实测候选 | 可用（本地 CPU） |
| `Database` | 存实测记录（默认 JSON） | 可用 |

入口是 `ms.tune_tir`（`tir_integration.py:45`），签名里默认 `cost_model="xgb"`、`database="json"`、`strategy="evolutionary"`。它返回一个 `Database`，不是 `Schedule`。

## 示例一：本机 meta_schedule 为什么跑不起来

先按官方思路写：

```python
import time
import tvm
from tvm import te, s_tir
from tvm.s_tir import meta_schedule as ms

N = 128
A = te.placeholder((N, N), name="A", dtype="float32")
B = te.placeholder((N, N), name="B", dtype="float32")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
func = te.create_prim_func([A, B, C])

t0 = time.perf_counter()
db = ms.tune_tir(
    mod=func, target="llvm", work_dir="ms_work",
    max_trials_global=8, num_trials_per_iter=8,
    cost_model="random", num_tuning_cores=2, seed=0,
)
print("seconds:", time.perf_counter() - t0)
```

本机真实报错（`实测`）：

```text
RuntimeError: psutil is required by the meta schedule search. Install it with: pip install psutil
```

调用栈的关键位置是 `TuneContext.__init__` → `_ffi_api.TuneContextInitialize` → `tvm.s_tir.meta_schedule.utils._cpu_count_impl`。源码在 `s_tir/meta_schedule/utils.py:57-62`：

```python
try:
    import psutil
except ImportError as err:
    raise ImportError(
        "psutil is required by the meta schedule search. Install it with: pip install psutil"
    ) from err
```

也就是说，**即使你显式传了 `num_tuning_cores=2`，C++ 侧的初始化仍然会调用 CPU 计数函数**；这个 Python 包没有装 `psutil`，所以直接失败。

即使装了 `psutil`，默认的 `cost_model="xgb"` 还需要 `xgboost`：`cost_model/xgb_model.py:41` 有 `import xgboost as xgb`。本机 `xgboost` 也没有安装（`importlib.util.find_spec("xgboost")` 返回 `None`）。可以改成 `cost_model="random"` 绕过 xgboost，但 psutil 仍然是硬依赖。

另外，`tvm.autotvm` 在 0.27 的 pip 包里已经不存在（`hasattr(tvm, "autotvm") == False`）。旧教程的 AutoTVM 代码不能直接跑；要用 meta_schedule，或者自己写网格搜索。

> **证据状态**：完整的 meta_schedule 调优在本机标 `guided`。缺的不是 TVM 本体，而是 `psutil` 和 `xgboost` 两个 Python 包；按任务约束不在主环境里安装。下面用等价的「手工网格搜索」做真实实验。

## 示例二：18 个配置的手工网格搜索

网格搜索不依赖 psutil/xgboost，逻辑透明：枚举配置、编译、实测、记录。核心 schedule：

```python
def apply_schedule(sch, bk, vj, par):
    i, j, kk = sch.get_loops(sch.get_sblock("C"))
    jo, ji = sch.split(j, factors=[None, vj])
    ko, ki = sch.split(kk, factors=[None, bk])
    if par > 1:
        io, ii = sch.split(i, factors=[par, None])
        sch.reorder(io, jo, ko, ii, ki, ji)
        sch.parallel(io)
    else:
        sch.reorder(jo, ko, i, ki, ji)
    sch.vectorize(ji)
    sch.unroll(ki)
```

每个配置都做数值对齐（`maxerr = 7.63e-05`），然后 `number=5, repeat=2` 测一次。真实结果（`实测`，N=256，float32）：

| bk | vj | par | build (s) | median (ms) | min (ms) |
|---:|---:|---:|---:|---:|---:|
| 8 | 8 | 1 | 0.090 | 4.504 | 3.893 |
| 8 | 8 | 4 | 0.102 | 1.315 | 1.260 |
| 8 | 8 | 8 | 0.116 | 1.667 | 1.479 |
| 8 | 16 | 1 | 0.089 | 4.295 | 4.171 |
| 8 | 16 | 4 | 0.114 | 1.647 | 1.559 |
| 8 | 16 | 8 | 0.124 | 1.160 | 1.076 |
| 16 | 8 | 1 | 0.116 | 3.597 | 3.007 |
| 16 | 8 | 4 | 0.131 | 1.481 | 1.464 |
| 16 | 8 | 8 | 0.144 | 1.228 | 1.115 |
| 16 | 16 | 1 | 0.115 | 3.341 | 2.492 |
| 16 | 16 | 4 | 0.140 | 1.285 | 0.994 |
| 16 | 16 | 8 | 0.165 | 1.295 | 1.179 |
| 32 | 8 | 1 | 0.144 | 4.114 | 3.978 |
| 32 | 8 | 4 | 0.208 | 1.440 | 1.236 |
| 32 | 8 | 8 | 0.208 | 1.423 | 1.108 |
| 32 | 16 | 1 | 0.132 | 4.145 | 3.957 |
| 32 | 16 | 4 | 0.228 | 1.446 | 1.210 |
| 32 | 16 | 8 | 0.238 | 1.100 | 1.090 |

baseline 同批测得 `38.506 ms`。网格最佳是 `bk=32, vj=16, par=8`，单次 `1.100 ms`，看起来是 `35×`。

**调优成本**：18 个配置总共 `5.06 s`（不含 baseline），其中大部分时间花在编译和计时。这个数字很重要：它已经是「玩具规模」；真实 autotuning 动辄几千个 trial、几分钟到几小时。

## 示例三：最佳 schedule 与 baseline 的对比

网格的 `1.100 ms` 是一次 `number=5, repeat=2` 的结果，噪声很大。把最佳配置重新用 `number=10, repeat=3`、3 轮交替测，得到更稳的数字：

| 配置 | 中位数 (ms) | 范围 (ms) | 相对 baseline |
|---|---:|---:|---:|
| baseline | 42.263 | 40.01–45.41 | 1.00 |
| bk32 / vj16 / par8 | 1.436 | 1.26–1.63 | 0.034（约 29×） |

也就是说：**网格里看到的 35× 是乐观值；稳健复测是约 29×**。差的这 20% 不是小问题——它说明单次测量不足以宣布胜利。

最佳配置的 TIR（`sch.mod.script()`，N=256）：

```text
for i_0 in T.parallel(8):
    for j_0, k_0, i_1 in T.grid(16, 8, 32):
        for k_1 in T.unroll(32):
            for j_1 in T.vectorized(16):
                with T.sblock("C"):
                    v_i = T.axis.spatial(256, i_0 * 32 + i_1)
                    v_j = T.axis.spatial(256, j_0 * 16 + j_1)
                    v_k = T.axis.reduce(256, k_0 * 32 + k_1)
                    T.reads(A[v_i, v_k], B[v_k, v_j])
                    T.writes(C[v_i, v_j])
                    with T.init():
                        C[v_i, v_j] = T.float32(0.0)
                    C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
```

这个 schedule 的「搜索结论」其实很朴素：**k 方向切 32 并展开、j 方向向量化 16、i 方向并行 8**。真正带来数量级收益的是 vectorize + parallel；`bk` 在 8–32 之间只是微调。

## 示例四：泛化与过拟合

调优只在 N=256 上做过。换 shape 会怎样？

| N | baseline (ms) | 最佳配置 | 最佳 (ms) | 加速 |
|---:|---:|---|---:|---:|
| 256 | 42.263 | bk32/v16/p8 | 1.436 | 29.4× |
| 384 | 109.582 | bk16/v16/p4 | 3.424 | 32.0× |
| 512 | 558.925 | bk32/v16/p8 | 10.119 | 55.2× |

N=256 选出的 `bk32/v16/p8` 在 N=512 仍然最好；N=384 的最佳是 `bk16/v16/p4`，但前三名只差约 3%。这说明在这个 workload 上，top 配置的排序有一定泛化性。

但换到**不能整除**的 shape，故事就变了：

| N | baseline (ms) | bk32/v16/p8 (ms) | 加速 | 说明 |
|---:|---:|---:|---:|---|
| 257 | 31.082 | 9.960 | 3.1× | 257 不是 16 的倍数，vectorize 产生 tail predicate |
| 250 | 31.802 | 4.300 | 7.4× | 250 不是 16 的倍数，但尾数更短 |

N=257 时，同样的 schedule 从 29× 掉到 3.1×。TIR 里能看到三个方向的 predicate：

```text
T.where(j_0 * 16 + j_1 < 257 and k_0 * 32 + k_1 < 257 and i_0 * 33 + i_1 < 257)
```

这不是「调优错了」，而是**调优结果只对训练 shape 附近负责**。如果你把 N=256 的最佳配置直接用到 257，绝对时间仍然比 baseline 快，但相对收益大幅缩水；如果业务 shape 是 257，应该把 257 放进搜索空间重新调，或者选一个不依赖整除的 `vj=1` 版本（本机 9.292 ms，略好于 9.960 ms）。

## 对比与取舍

| 方案 | 搜索成本 | 结果质量 | 泛化 | 本机可用性 |
|---|---|---|---|---|
| 手工试 3–5 个 | 分钟级 | 依赖经验 | 差 | 可用 |
| 手工网格 18 个 | 5 s | 中等 | 单 shape | 可用（本章） |
| meta_schedule 小预算 | 分钟级 | 较好 | 需多 shape | `guided`（缺 psutil） |
| 生产级 autotuning | 小时级 | 好 | 需数据库/多 shape | 本机不具备 |

自动调优的收益不是免费的：

- **搜索成本**：5 秒只是 18 个配置；真实空间是几千上万。
- **泛化**：N=256→257 的 29×→3.1× 就是反例。
- **过拟合**：只在一个 shape、一个 dtype、一个 layout 上调，换一个就可能失效。
- **噪声**：单次 1.10 ms vs 稳健 1.44 ms；cost model 会被噪声带偏。
- **编译/代码体积**：Ch 11 的扩展实验里，`bk=256` 的编译时间 0.62 s，是 `bk=8` 的 5 倍。
- **正确性**：每个候选都要做数值对齐；快的错解比慢的对解更糟。

## 踩雷集锦

1. **`ms.tune_tir(func=...)`**：参数名是 `mod=`，不是 `func=`；写错直接 `TypeError`。
2. **以为 `num_tuning_cores=2` 就不需要 psutil**：C++ 侧仍会调用 `cpu_count`；本机真实报错。
3. **默认 `cost_model="xgb"` 但没装 xgboost**：`xgb_model.py:41` 直接 import；要么装，要么改 `random`。
4. **把 `max_trials_global` 当时间预算**：它是 trial 数，不是秒数；每个 trial 的编译/测量时间可能差很多。
5. **只测一次就选最佳**：本机网格 1.10 ms，稳健复测 1.44 ms；单次结果会骗人。
6. **只在一个 shape 上调**：N=256→257 的加速从 29× 掉到 3.1×。
7. **不做数值对齐**：调优只优化时间，不保证正确；每个候选都要跑 `maxerr`。
8. **在测量机上同时跑调优**：调优线程会干扰计时；用 `num_tuning_cores` 或分开机器。

## 进阶：再往深一层

- **读 `tune_tir` 源码**：`tir_integration.py:45` 起，看它如何把 `mod` 转成 `TuneContext`、如何创建 `Database`、如何调度 `TaskScheduler`。
- **读 `SpaceGenerator`**：`s_tir/meta_schedule/space_generator/` 下的 `post_order_apply.py` 展示了 schedule 规则如何组合成 design space。
- **读 `EvolutionarySearch`**：`search_strategy/evolutionary_search.py` 里的采样、交叉、变异，和遗传算法同源。
- **读 cost model 的训练数据格式**：`cost_model/xgb_model.py` 的 `TrainingRecord` 把 schedule 特征和实测时间配对；理解特征工程，就知道它为什么会过拟合。
- **做多 shape 调优**：把 N=256、257、384 放进同一个 database，观察最佳配置是否变化；这是从「单点调优」走向「shape 泛化」的第一步。

## 动手练习

### 练习 1：把网格缩到 6 个配置

**任务**：只保留 `bk ∈ {8, 32}`、`vj=16`、`par ∈ {1, 4, 8}`，共 6 个配置，重跑网格，记录调优时间和最佳时间。

**测试用例**：`maxerr < 1e-4`；调优时间应明显小于 18 配置的 5.06 s。

**步骤**：

1. 复制本章 `apply_schedule`。
2. 把 `itertools.product([8,16,32], [8,16], [1,4,8])` 改成 `product([8,32], [16], [1,4,8])`。
3. 记录每个配置的 build/measure 时间和总时间。
4. 比较 6 配置的最佳值与 18 配置的 1.44 ms。

<details>
<summary>参考解答</summary>

```python
for bk, vj, par in itertools.product([8, 32], [16], [1, 4, 8]):
    ...
```

预期：调优时间约 2 秒；最佳仍是 `bk=32, vj=16, par=8` 或 `bk=8, vj=16, par=8`，差距在噪声范围内。这个练习说明：**搜索空间缩小 3 倍，最佳值不一定变差**，但你可能错过中间的最优。

</details>

**常见卡点**：配置少了，更容易被单次噪声骗；仍然要 3 轮复测。

### 练习 2：用 N=256 调，测 N=257

**任务**：用 N=256 选出的最佳配置，在 N=257 上编译、运行、计时，和 N=257 baseline 比较。

**测试用例**：`maxerr < 1e-4`；记录加速比，并解释为什么不是 29×。

**步骤**：

1. 复用 `apply_schedule(sch, 32, 16, 8)`。
2. 把 N 改成 257。
3. 打印 `sch.mod.script()`，找到 `T.vectorized(16)` 外层的 predicate。
4. 比较 N=257 baseline 与 tuned。

<details>
<summary>参考解答</summary>

本机结果（`number=3, repeat=2` 快速测，绝对值会漂，趋势可信）：N=257 baseline 31.082 ms，tuned 9.960 ms，约 3.1×。257 不是 16 的倍数，`split(j, [None,16])` 产生 17 个外层块，最后一块只有 1 个有效元素；vectorize 需要 predicate/掩码，SIMD 效率大幅下降。

```python
# 观察 predicate
print(sch.mod.script())
```

</details>

**常见卡点**：不要用 `N=257` 直接套 `vj=16` 就宣称「调优无效」；应该把 257 放进搜索空间，或选择 `vj=1` 的保守版本。

### 练习 3：加一个 i/j tile 维度

**任务**：在现有 `bk/vj/par` 之外，加 `bi ∈ {32, 64}`（i 的 tile 大小），把搜索空间从 18 扩到 36，测调优时间和最佳值。

**测试用例**：`maxerr < 1e-4`；记录 36 配置的总时间，并与 18 配置的 5.06 s 比较。

**步骤**：

1. 在 `apply_schedule` 里先 `split(i, [bi, None])`，再 `split` 出 `par` 的并行块。
2. 注意：`par` 和 `bi` 都是 i 方向的切分，顺序会影响循环结构。
3. 跑 36 个配置，记录最佳。
4. 讨论：搜索空间翻倍，最佳值提升了多少？值得吗？

<details>
<summary>参考解答</summary>

一种写法是先按 `bi` 切 tile，再在 tile 内按 `par` 并行；`par` 的 extent 最好整除 `bi`，否则 predicate 增多。更简单的做法是把 `bi` 当作 `par` 的别名，观察重复配置——这会提醒你：**搜索空间不是越大越好，维度之间可能相关**。

</details>

**常见卡点**：`split` 的顺序会改变 `io/ii` 的语义；每次都用 `script()` 确认，不要靠猜。

### 练习 4（guided）：在一次性 venv 里跑 meta_schedule

**任务**：新建一个临时 venv，安装 `psutil`、`xgboost`，再跑 `ms.tune_tir`，观察真实调优日志和数据库。

**测试用例**：能跑完 `max_trials_global=8`；把最佳 schedule 的 TIR 和 `maxerr` 记录下来。

**步骤**：

1. 在 `.scratch` 下建临时 venv，不要动 `aic-env`。
2. `pip install psutil xgboost`（以及本机已装的 `apache-tvm`）。
3. 复制示例一的 `tune_tir` 调用，保持 `max_trials_global=8`。
4. 如果仍然失败，记录完整报错；不要伪造结果。

<details>
<summary>参考解答</summary>

这一步是 `guided`：本课主环境不装这两个包，所以不提供伪造的调优曲线。你需要观察的是：`work_dir` 下是否出现 `database.json`、`logs/`；`db.get_best()` 返回的 schedule 是否真的比 baseline 快；以及调优耗时是否远大于 5 秒的网格搜索。

</details>

**常见卡点**：`xgboost` 在 Windows 上可能需要编译工具；如果安装失败，直接用 `cost_model="random"`，只验证流程，不比较质量。

## 本章重点整理

- 自动调优 = 搜索空间 + cost model + 实测反馈 + 数据库；cost model 只排序，不替代测量。
- 本机 meta_schedule 因缺 `psutil` 无法初始化；默认 `xgb` 还缺 `xgboost`；完整流程标 `guided`。
- 手工网格 18 个配置，调优成本 5.06 s；最佳 `bk32/vj16/par8` 单次 1.10 ms，稳健复测 1.44 ms。
- baseline 42.26 ms，最佳 1.44 ms，约 29×；其中大部分收益来自 vectorize + parallel，不是 `bk`。
- 单 shape 调优会过拟合：N=256 的 29× 到 N=257 只剩 3.1×。
- 自动调优的边界：搜索成本、泛化、噪声、编译时间、正确性。
- 任何候选都要做数值对齐；快而错不如慢而对。

## 自我检核

- [ ] 我能画出「搜索空间 → cost model → 实测 → 数据库」的循环
- [ ] 我能解释 cost model 为什么不能替代 runner
- [ ] 我知道 meta_schedule 在本机缺哪两个 Python 包
- [ ] 我能写出 `tune_tir(mod=..., target=..., work_dir=...)` 的最小调用
- [ ] 我能说出 18 配置网格的三个维度和总调优时间
- [ ] 我能解释 N=257 的加速为什么从 29× 掉到 3.1×
- [ ] 我能列出至少四个「自动调优不是银弹」的理由
- [ ] 我知道调优后必须做 `maxerr` 数值对齐

## 延伸阅读

- **TVM meta_schedule 文档**（https://tvm.apache.org/docs/reference/api/python/meta_schedule.html）：读 `tune_tir`、`TuneContext`、`SpaceGenerator`、`CostModel`。与本课关联：本章 API 的官方说明。
- **Ansor 论文 (OSDI 2020)**（https://www.usenix.org/conference/osdi20/presentation/zheng）：读搜索空间生成和 cost model 两节。与本课关联：meta_schedule 的思想来源。
- **AutoTVM 论文 (NeurIPS 2018)**（https://arxiv.org/abs/1805.07440）：读模板、配置空间、transfer learning。与本课关联：旧一代调优器的设计。
- **TVM 源码 `s_tir/meta_schedule/`**（https://github.com/apache/tvm/tree/main/python/tvm/s_tir/meta_schedule）：读 `tir_integration.py`、`utils.py`、`cost_model/xgb_model.py`。与本课关联：本机报错和行号的依据。
- **MLPerf Tiny / TVM autotuning 实践**（https://mlcommons.org/benchmarks/）：看工业界如何把调优成本摊到多 shape、多设备。与本课关联：调优的工程边界。

---

→ [Ch 14 后端代码生成：LLVM IR、C 与 CUDA](./14-codegen-backends.md)