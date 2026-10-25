# Ch 21 性能工程：先让结论站得住，再谈快

> **目标**：建立一套能被别人复现的性能结论：先看生成代码的结构，再用子进程隔离测时间，用 warmup + 中位数 + 重复测量处理噪声；能区分编译时间与运行时间，知道这台机器上 20%~50% 的漂移意味着什么。
> **环境**：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43；本机默认 4 线程。命令都在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

这个实验约一分钟，会起四个子进程，每个子进程独立编译一个变体。

## 为什么

在这台机器上，同一个二进制、同一份输入、同一个变体，两次测量的差距可以到 20%~50%。这不是测量代码写错了，而是桌面 CPU 的常态：频率调节、后台进程、缓存状态、调度抖动都在影响结果。

于是出现一个尴尬的比例：

- 你辛苦把某个 kernel 优化了 15%，落在噪声里，无法证明。
- 你换了编译器参数，慢了 30%，也可能只是这一轮风扇转得慢。

性能工程的第一目标因此不是「更快」，而是**让结论可信**。可信的结论需要三样东西：

1. **结构证据**：生成代码里到底是 8 元素向量还是标量，有没有 omp pragma，融合成了一个 kernel 还是三个。这些是确定性的，不随机器抖动。
2. **隔离的测量**：每个变体一个进程、一个 cache 目录，避免状态串扰。
3. **统计量**：warmup、多次采样、中位数、重复一轮估噪声。

这一章用 `lab_d_perf.py` 把这三件事串起来。它比较同一份数学 `y = relu(sin(x) * 2 + y)` 在 4M 个 float32 上的三个变体：

- `vec`：默认，AVX2 向量化，4 线程；
- `scalar`：`cpp.vec_isa_ok = False`，退回标量；
- `threads1`：`torch.set_num_threads(1)`，不再生成 omp pragma。

## 主线

### 1. 证据等级：结构 > 时间 > 直觉

| 证据 | 确定性 | 获取成本 | 能回答什么 |
| --- | --- | --- | --- |
| 生成代码结构（step / omp / tmp / kernel 数 / extern） | 高，同输入必同结果 | 低，读产物即可 | 优化有没有生效、为什么快/慢 |
| 中位数耗时（隔离进程） | 中，同机同轮可比 | 中，要跑多次 | 哪个变体更快、快多少 |
| 最小值 | 中偏高，受离群值影响小 | 低，同一批数据 | 理论下界、判断是否被干扰 |
| 单次耗时 | 低 | 最低 | 几乎什么都不能回答 |
| 跨机器/跨会话数字 | 低 | 高 | 只能作量级参考 |

课程里的所有性能结论都先落到第一行，再看第二行。`lab_d` 的断言顺序也是这个逻辑：先断言 `step == 8`、`omp threads is None`，再断言 `scalar > 1.5 * vec`。

### 2. 本机实测：三组结构，三段时间

同一份数学在 4M 个 float32 上，结构差异是确定的：

| 变体 | 向量化 | 步长 | omp pragma | 说明 |
| --- | --- | --- | --- | --- |
| `vec` | 是 | 8 | `num_threads(4)` | 默认路径，AVX2 |
| `scalar` | 否 | 1 | `num_threads(4)` | `cpp.vec_isa_ok=False` |
| `threads1` | 是 | 8 | 无 | 单线程，仍向量化 |

时间区间（本机多次运行观察到的范围，单次结果可能落在区间外）：

| 变体 | 中位耗时 | 相对 `vec` |
| --- | ---: | --- |
| `vec` | 约 8–10 ms | 1.0x |
| `scalar` | 约 25–28 ms | 约 3x |
| `threads1` | 约 12–17 ms | 约 1.5x |

三个结论可以直接从表里读出来：

- 标量比向量慢一个量级附近（约 3 倍），和「一次处理 8 个 float」的预期方向一致，但不到 8 倍，因为标量路径还有 omp 并行、内存带宽和其他开销在分担。
- 单线程仍然向量化，所以只比 4 线程慢一截（约 1.5 倍），而不是 4 倍。说明这个 kernel 在这台机器上不是纯计算受限。
- **不要把这些数字当成常数**。它们只对「这台机器、这次会话、这组输入」负责。换机器、换 torch 版本、换输入大小，都要重新测。

`lab_d` 还会把一个变体重跑一次（`vec_again`），并打印漂移：

```text
[INFO] vec 重复测量漂移: NN% (A ms vs B ms) —— 这台机器上 30%~50% 漂移是常态，别用单次结果下结论
```

上面这行是格式示意，具体百分比每次不同。漂移落在 20%~50% 区间时属于正常；`lab_d` 只在漂移超过 100%（量级异常）时判失败。

### 3. 为什么必须子进程隔离

`lab_d` 的注释把原因写得很清楚，值得抄进自己的实验模板：

> 同一个进程里连续编译多个 config 变体时，Inductor/Dynamo 的缓存可能复用上一次的产物，导致「代码看起来是标量、跑起来仍是向量」。隔离进程 + 独立 cache 目录，才能保证测的就是刚生成的那份代码。

具体有四类状态会串：

1. **编译缓存**：第 19 章讲过，Dynamo 帧缓存、AOTAutograd 缓存、FxGraphCache、`PyCodeCache.modules` 都可能让第二次编译复用上一次的结果。
2. **线程数**：`torch.set_num_threads()` 是进程级全局状态。而且本机 `config.cpp.dynamic_threads` 默认是 `False`，线程数会被特化进 `#pragma omp parallel num_threads(N)`；改线程数等于改生成代码。
3. **内存分配器状态**：上一次实验留下的缓存块会影响下一次的分配路径。
4. **CPU 亲和性与频率状态**：长时间跑同一段代码会让频率进入不同的档位。

`lab_d` 的做法是双保险：

```python
def run_child(name: str, tag: str) -> dict:
    env = dict(os.environ)
    cache = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "perf" / tag
    if cache.exists():
        shutil.rmtree(cache)
    env["TORCHINDUCTOR_CACHE_DIR"] = str(cache)
    proc = subprocess.run([sys.executable, __file__, "--variant", name], env=env, ...)
    ...
```

子进程负责「编译 + 计时 + 把结果以 JSON 打到 stdout」，父进程只负责汇总和画表。这样每个变体的编译输入、线程数、cache 目录都是干净的。

代价是每个变体都要付一次完整编译（本机 5–15 秒），四个变体加起来约一分钟。这是必要成本：用一分钟换一个可信结论，比用十秒换一个错误结论划算。

### 4. 测量协议：warmup、中位数、交错、重复

`common.py` 里有两个计时函数，分别对应两种场景。

**单变体**用 `bench_ms`：

```python
def bench_ms(fn, iters=30, warmup=8):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples) * 1e3
```

- `warmup=8`：前几次调用包含页错误、缓存冷启动、可能的惰性初始化，不能计入。
- `iters=30`：单次测量噪声大，取一批。
- `median`：比均值抗离群值。一次被后台进程打断的 50 ms 不该代表整体。
- 单位统一成毫秒。

**多变量对比**用 `interleaved_ms`：

```python
def interleaved_ms(variants, rounds=7, iters=20):
    for fn in variants.values():
        for _ in range(5):
            fn()
    samples = {k: [] for k in variants}
    for _ in range(rounds):
        for name, fn in variants.items():
            t0 = time.perf_counter()
            for _ in range(iters):
                fn()
            samples[name].append((time.perf_counter() - t0) / iters * 1e3)
    return {name: {"median": statistics.median(v), "min": min(v)} for name, v in samples.items()}
```

交错的意思是：每一轮里 A、B、C 各跑一次，而不是先把 A 跑完再跑 B。这样 CPU 频率漂移对三个变体的影响大致相同，比较的是相对关系。

协议字段建议每次都写清楚：

| 字段 | 本课程取值 | 作用 |
| --- | --- | --- |
| warmup | 8 次 | 排除冷启动 |
| iters | 25 次（`lab_d`） | 单批采样量 |
| 统计量 | median | 抗离群 |
| 重复 | `vec_again` 一次 | 估计噪声 |
| 隔离 | 子进程 + 独立 cache | 排除状态串扰 |
| 线程 | 4 / 1 显式设置 | 排除全局状态 |
| 输入 | 2048×2048 float32 | 固定规模 |

### 5. 编译时间与运行时间要分开算

`lab_d` 在子进程里记录了 `compile_s`：

```python
t0 = time.perf_counter()
with cfg.patch(patches):
    before = common.modules_snapshot()
    fn = torch.compile(f)
    out = fn(x, y)
    mods = common.modules_since(before)
    src = common.read_text_any(mods[-1].__file__)
compile_s = time.perf_counter() - t0
```

注意计时的范围：`cfg.patch` 必须包住**第一次调用**，因为 `torch.compile` 是惰性的，真正编译发生在 `fn(x, y)`。只包住 `torch.compile(...)` 那一行，什么都不会编译。

编译时间与运行时间的关系可以写成：

```text
总时间(N 次调用) = 编译时间 + N × 单次运行时间 + 缓存命中后的启动开销
```

粗算盈亏平衡点：

```text
N* = 编译时间 / (eager 单次时间 - 编译后单次时间)
```

举例说明量级（不是本机实测的精确值）：如果编译要 10 秒，每次能省 2 ms，那么要 5000 次调用才回本。这个量级意味着：

- **离线批处理、长服务**：编译时间可以摊薄，值得优化运行时间。
- **一次性脚本、冷启动敏感**：编译时间占主导，应该优先减少编译，而不是追求最后 10% 的运行时间。
- **短请求高 QPS**：两者都要看，通常用预热把编译移到启动阶段。

本机 `lab_d` 的四个变体各自独立编译，所以 `compile_s` 也是四份。把编译时间加进「变体间比较」是常见的口径错误：`scalar` 并不因为编译慢而运行慢。

`torch._inductor.metrics` 可以在不读产物的情况下拿到编译质量指标：

```python
import torch._inductor.metrics as metrics
metrics.reset()
...  # 编译并调用一次
print(metrics.generated_kernel_count, metrics.generated_cpp_vec_kernel_count)
```

`final_project.py` 就是这么记录 baseline/optimized 的 kernel 数与向量 kernel 数的。指标适合做趋势监控，不能代替结构阅读：`generated_kernel_count = 1` 不代表这唯一一个 kernel 就是最优形态。

### 6. 从结构到动作：一张决策表

看到不同的结构特征，下一步动作不同：

| 观察 | 含义 | 下一步 |
| --- | --- | --- |
| `vectorized=False`、`step=1` | 向量化没生效 | 查 `cpp.vec_isa_ok`、`cpp.simdlen`、CPU ISA 探测 |
| 没有 `#pragma omp` | 单线程代码 | 查 `torch.get_num_threads()`、`cpp.threads`、`cpp.dynamic_threads` |
| `extern_kernels.addmm/mm/bmm` | 计算在外部库里 | 优化点在库/布局，不在生成 kernel |
| kernel 数 > 1 | 融合被切断 | 查 `ir_post_fusion.txt` 的依赖与融合边界 |
| `tmp_ops` 偏多 | 表达式没被改写/融合 | 看第 7、8 章的 pass 与 pattern |
| 数值误差超过预期 | 正确性问题优先 | 先做差分测试，再谈性能 |

动作之后必须重新测。判断标准不是「数字变小了」，而是「结构变了，并且变化幅度超过本次测量的漂移」。如果结构没变，时间变了，优先怀疑噪声。

## 可跑实验

### 实验 1：三变体结构与时间

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

预期输出三段：

1. 结构表：`vec` 的 `vectorized=yes, step=8, omp=4`；`scalar` 的 `no, 1, 4`；`threads1` 的 `yes, 8, none`。
2. 时间表：`vec` 最小，`threads1` 居中，`scalar` 最大。
3. 断言与 `[INFO]` 漂移行。

读数时按这个顺序：

- 先确认三个变体的结构行符合预期。结构不对，后面的时间没有意义。
- 再看 `vs vec` 的倍数。倍数在 1.2 以内时，不要下结论。
- 最后看 `[INFO]` 的漂移百分比。漂移接近或超过你要证明的收益时，结论不成立。

### 实验 2：重复一轮，比较漂移

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

再跑一次整条命令。比较两次的 `vec` 中位数，你会看到 20%~50% 的漂移是常态。两次的「scalar 是 vec 的几倍」比两次的绝对毫秒数更稳定，这也是课文强调相对关系的原因。

### 实验 3：环境与编译自检

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

它打印 Python、torch、`cl.exe`、线程数、cache 目录、CPU ISA。写性能报告时，这些字段应该原样附上，否则别人无法判断你的数字属于哪个环境。

### 实验 4：把编译期改造也纳入对比

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py
```

这个实验比较 baseline 与 optimized 两个变体，同时记录 `kernels`、`vec_kernels`、`tmp_ops`、`median_ms`、`max_err`，并生成 `code/.cache/final_report.md`。它演示了「结构指标 + 时间指标 + 正确性指标」三件套怎么写进同一份报告。

## 源码落点

| 位置 | 读什么 | 为什么读 |
| --- | --- | --- |
| `code/labs/common.py` 的 `bench_ms` | warmup、iters、median | 单变体计时的最小实现 |
| `code/labs/common.py` 的 `interleaved_ms` | rounds、每轮交错、median/min | 多变体对比的正确姿势 |
| `code/labs/common.py` 的 `kernel_stats` | step、vectorized、threads、tmp_ops | 结构证据的提取器 |
| `code/labs/lab_d_perf.py` 的 `child` / `run_child` | 独立进程、独立 cache、JSON 回传 | 隔离模板 |
| `torch/_inductor/utils.py` 的 `parallel_num_threads` | `config.cpp.threads` 与 `torch.get_num_threads()` | 线程数如何进入生成代码 |
| `torch/_inductor/config.py` 的 `class cpp` | `threads=-1`、`dynamic_threads=False` | 为什么改线程数会改代码 |
| `torch/_inductor/metrics.py` | `generated_kernel_count`、`generated_cpp_vec_kernel_count`、`reset` | 编译质量指标 |
| `torch/_inductor/codegen/cpp.py` 的 `CppKernel` | `num_threads` 参数 | omp pragma 的来源 |

`parallel_num_threads()` 的实现只有三行，但它解释了一个常见困惑：

```python
def parallel_num_threads() -> int:
    threads = config.cpp.threads
    if threads < 1:
        threads = torch.get_num_threads()
    return threads
```

`config.cpp.threads` 默认 `-1`，所以实际取 `torch.get_num_threads()`。本机是 4，于是生成 `num_threads(4)`。把它设成 1，或者调用 `torch.set_num_threads(1)`，生成代码里就没有 omp pragma 了。

## 常见坑

1. **用单次耗时下结论。** 这台机器的漂移是 20%~50%，单次结果连自己都复现不了。至少取一批样本的中位数，并重复一轮看漂移。

2. **在同一进程里比较多个变体。** 缓存复用、线程数、分配器状态都会串。像 `lab_d` 一样，一个变体一个进程、一个 cache 目录。

3. **把编译时间算进运行时间。** `fn(x, y)` 的第一次调用包含编译；`bench_ms` 的 warmup 正是为了把它排除。报告里要把 `compile_s` 和 `median_ms` 分列。

4. **只包住 `torch.compile(...)` 就以为在计时编译。** 它是惰性的，真正的编译发生在第一次调用。`cfg.patch` 也必须包住第一次调用，否则 config 不生效。

5. **用均值。** 一次被后台进程打断的样本会把均值拉高。用中位数，同时报告最小值作为下界参考。

6. **忽略线程数的全局性。** `torch.set_num_threads()` 影响整个进程，而且会被特化进生成代码。比较线程数必须在编译前设置，并且隔离进程。

7. **跨机器、跨会话比数字。** 课程里的 8–10 ms / 25–28 ms / 12–17 ms 只属于这台机器。换环境要重测，只保留「相对关系」这个结论。

8. **优化了噪声，却改坏了结构。** 为了少几条 `tmp` 引入了额外 kernel 或 graph break，时间可能短期好看、长期更差。每次改动都要同时看结构、时间、正确性三项。

9. **忘了 `metrics.reset()`。** 计数器是累计的；不重置会把上一次编译的 kernel 数算进来。

## 自我检查

- [ ] 我能说出这台机器上 `vec`、`scalar`、`threads1` 三段中位耗时的区间。
- [ ] 我能解释为什么标量只慢约 3 倍，而不是 8 倍。
- [ ] 我能列出至少四类必须靠子进程隔离的状态。
- [ ] 我能写出 `total = compile + N × runtime`，并估算一个盈亏平衡点。
- [ ] 我知道 `bench_ms` 的 warmup、iters、统计量分别解决什么问题。
- [ ] 我能解释交错测量为什么能降低频率漂移的影响。
- [ ] 我能从 `kernel_stats` 的四个字段推断出下一步该查什么。
- [ ] 我知道报告里必须写清环境、输入、线程数、warmup、iters、统计量和隔离方式。
- [ ] 我能解释为什么「结构没变但时间变了」时应该先怀疑噪声。

## 延伸阅读

- **`code/labs/common.py`**：读哪里——`bench_ms`、`interleaved_ms`、`kernel_stats`；学什么——计时与结构提取的最小实现；关联——本章全部实验。
- **`code/labs/lab_d_perf.py`**：读哪里——`VARIANTS`、`child`、`run_child`、断言段；学什么——隔离 benchmark 的完整模板；关联——第 19 章的缓存隔离。
- **`torch/_inductor/config.py` 的 `class cpp`**：读哪里——`threads`、`dynamic_threads`、`simdlen`、`vec_isa_ok`；学什么——哪些开关会改变生成代码；关联——第 15、16 章。
- **`torch/_inductor/metrics.py`**：读哪里——`generated_kernel_count`、`generated_cpp_vec_kernel_count`、`reset`；学什么——编译质量指标；关联——`final_project.py` 的报告。
- **`code/labs/final_project.py`**：读哪里——子进程、指标、报告生成；学什么——把结构、时间、正确性写进同一份报告；关联——课程收尾。

[上一章：Ch 20 动态 shape 与 guards](./20-dynamic-shapes-and-guards.md) | [下一章：Ch 22 正确性与 fallback](./22-correctness-and-fallbacks.md)