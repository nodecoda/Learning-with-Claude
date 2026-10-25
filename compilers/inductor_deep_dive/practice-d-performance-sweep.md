# 练习 D：布局变体下的性能扫掠与报告

> **目标**：在 `lab_d_perf.py` 的“向量化 / 标量 / 单线程”三变体上，再加入连续、转置、切片等布局条件，做成一个多轮测量的小型扫掠；能区分“配置差异”和“布局差异”，能从生成代码里找到 copy kernel 或带 stride 的 load，能写出一份带条件、带噪声区间、可复现的性能报告。
>
> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43，CPU 仅 AVX2（`valid_vec_isa_list()` 返回 `['VecAVX2']`）。命令在仓库根目录 `D:\Learning-with-Claude` 执行，统一入口为 `powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本>`。`lab_d_perf.py` 已提供三变体与子进程隔离；本练习要求你新建 `code\labs\practice_d_layout.py`，不要在参考脚本上直接改。

## 为什么

`lab_d_perf.py` 回答的是“向量化和线程数如何改变生成代码”。它固定了输入布局：`torch.randn(N, N)` 连续存放。真实模型里，张量经常是转置视图、切片、广播结果或 `permute` 之后的非连续布局。布局一变，Inductor 可能：

- 生成带 stride 的 load，向量化步长和访存模式随之改变；
- 插入一个 copy kernel，把非连续输入先变成连续 buffer；
- 放弃向量化，退回标量循环；
- 改变 cache 局部性，让同样的指令数跑出不同的时间。

如果只比较“连续 + 默认 config”和“连续 + 标量 config”，你测到的是 config 的影响，不是布局的影响。本练习要求把这两个维度拆开：**同一 config 下比较布局，同一布局下比较 config**。这样报告里的每一行差异才有归因。

另一个问题是噪声。`lab_d_perf.py` 已经提醒：这台机器上重复测量的漂移可达 20%–50%，单次结果不能下结论。本练习要求每个变体跑多轮（例如 5 轮 × 20 次），报告中位数、最小/最大值和漂移比例；如果两个变体的区间重叠，就写“不可区分”，不要硬排名。

最后是报告。性能工程的价值不在于“我跑了一个更快的数”，而在于别人能按你的命令复现、能理解你控制了哪些变量、能判断结论的边界。本练习的交付物是一份 Markdown 报告，不是一张截图。

## 主线

### 1. 变量设计：把 config 和 layout 拆成两个轴

参考 `lab_d_perf.py` 的三变体：

| 变体 | config | 线程 | 布局 |
|---|---|---|---|
| `vec` | 默认 | 4 | 连续 |
| `scalar` | `cpp.vec_isa_ok=False` | 4 | 连续 |
| `threads1` | 默认 | 1 | 连续 |

本练习在此基础上加布局轴：

| 变体 | config | 线程 | 布局 | 对照目的 |
|---|---|---|---|---|
| `contig` | 默认 | 4 | `(2048, 2048)` 连续 | 基线 |
| `scalar` | 关闭向量化 | 4 | 连续 | config 影响 |
| `threads1` | 默认 | 1 | 连续 | 并行影响 |
| `transpose` | 默认 | 4 | `x.t()`，shape 不变、stride 交换 | 非连续、同元素数 |
| `contig_half` | 默认 | 4 | `(2048, 1024)` 连续 | 半尺寸对照 |
| `slice` | 默认 | 4 | `x[:, ::2]`，`(2048, 1024)`、列 stride=2 | 非连续、半尺寸 |

`transpose` 与 `contig` 的元素数相同，可以直接比时间；`slice` 只有一半元素，不能和 `contig` 直接比绝对毫秒数，要和 `contig_half` 比，或者报告 `ns/element`。这条规则很重要：**不同元素数的两个变体，绝对耗时没有可比性。**

布局的生成方式：

```python
full_x = torch.randn(2048, 2048)
full_y = torch.randn(2048, 2048)

contig = full_x                                   # stride (2048, 1)
transpose = full_x.t()                            # stride (1, 2048)，非连续
contig_half = full_x[:, :1024].contiguous()       # stride (1024, 1)
slice_view = full_x[:, ::2]                       # stride (2048, 2)，非连续
```

不要写 `full_x.t().contiguous()`：那会复制成连续张量，你测的就不再是转置布局。用 `x.is_contiguous()` 和 `x.stride()` 在子进程里记录实际布局，报告里附上，防止“以为测了转置，其实测了拷贝”。

### 2. 脚本骨架：子进程隔离 + 多轮采样 + Markdown 报告

把下面的代码保存为 `compilers\inductor_deep_dive\code\labs\practice_d_layout.py`。它复用 `labs/common.py` 的产物定位和结构解析，不修改参考实验。

```python
"""练习 D 参考骨架：布局变体 + 多轮测量 + Markdown 报告。"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import torch

from labs import common

N = 2048
CASES = {
    "contig":      {"layout": "contig",      "patches": {}, "threads": 4},
    "scalar":      {"layout": "contig",      "patches": {"cpp.vec_isa_ok": False}, "threads": 4},
    "threads1":    {"layout": "contig",      "patches": {}, "threads": 1},
    "transpose":   {"layout": "transpose",   "patches": {}, "threads": 4},
    "contig_half": {"layout": "contig_half", "patches": {}, "threads": 4},
    "slice":       {"layout": "slice",       "patches": {}, "threads": 4},
}


def f(x, y):
    return torch.relu(torch.sin(x) * 2.0 + y)


def make_inputs(layout: str):
    g = torch.Generator().manual_seed(1234)
    fx = torch.randn(N, N, generator=g)
    fy = torch.randn(N, N, generator=g)
    if layout == "contig":
        return fx, fy
    if layout == "transpose":
        return fx.t(), fy
    if layout == "contig_half":
        return fx[:, : N // 2].contiguous(), fy[:, : N // 2].contiguous()
    if layout == "slice":
        return fx[:, ::2], fy[:, ::2]
    raise ValueError(layout)


def split_kernels(src: str):
    starts = [m.start() for m in re.finditer(r'extern "C" __declspec\(dllexport\)', src)]
    ends = starts[1:] + [len(src)]
    return [src[a:b] for a, b in zip(starts, ends)]


def bench_rounds(fn, rounds: int = 5, iters: int = 20, warmup: int = 8):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for _ in range(iters):
            fn()
        samples.append((time.perf_counter() - t0) / iters * 1e3)
    return samples


def child(name: str) -> None:
    import torch._inductor.config as cfg
    import torch._inductor.metrics as metrics

    spec = CASES[name]
    torch.manual_seed(0)
    torch.set_num_threads(spec["threads"])
    x, y = make_inputs(spec["layout"])
    ref = f(x, y)

    torch._dynamo.reset()
    metrics.reset()
    t0 = time.perf_counter()
    with cfg.patch(spec["patches"]):
        before = common.modules_snapshot()
        fn = torch.compile(f, fullgraph=True)
        out = fn(x, y)          # 第一次调用必须在 patch 内
        mods = common.modules_since(before)
        if not mods:
            raise SystemExit("没有捕获到生成的模块")
        src = common.read_text_any(mods[-1].__file__)
    compile_s = time.perf_counter() - t0

    bodies = split_kernels(src)
    nk = len(bodies)
    stats = [common.kernel_stats(body) for body in bodies]
    payload = {
        "case": name,
        "layout": spec["layout"],
        "threads": spec["threads"],
        "shape": list(x.shape),
        "stride": list(x.stride()),
        "contiguous": x.is_contiguous(),
        "numel": x.numel(),
        "max_err": (out - ref).abs().max().item(),
        "compile_s": compile_s,
        "generated_kernels": metrics.generated_kernel_count,
        "vec_kernels": metrics.generated_cpp_vec_kernel_count,
        "kernel_count": nk,
        "kernels": stats,
        "rounds": bench_rounds(lambda: fn(x, y)),
    }
    print("RESULT " + json.dumps(payload))


def run_child(name: str) -> dict:
    env = dict(os.environ)
    cache = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "practice_d" / name
    if cache.exists():
        shutil.rmtree(cache)
    env["TORCHINDUCTOR_CACHE_DIR"] = str(cache)
    proc = subprocess.run(
        [sys.executable, __file__, "--case", name],
        env=env, capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:])
    raise SystemExit(f"{name} 子进程失败")


def summarize(r: dict) -> dict:
    rounds = r["rounds"]
    med = statistics.median(rounds)
    lo, hi = min(rounds), max(rounds)
    return {
        **r,
        "median_ms": med,
        "min_ms": lo,
        "max_ms": hi,
        "drift": (hi - lo) / med if med else 0.0,
        "ns_per_elem": med * 1e6 / r["numel"] if r["numel"] else 0.0,
        "tmp_total": sum(k["tmp_ops"] for k in r["kernels"]),
        "vec_any": any(k["vectorized"] for k in r["kernels"]),
        "steps": "/".join(str(k["step"]) for k in r["kernels"]),
        "omp": "/".join("none" if k["threads"] is None else str(k["threads"]) for k in r["kernels"]),
    }


def main() -> None:
    if "--case" in sys.argv:
        child(sys.argv[sys.argv.index("--case") + 1])
        return

    common.require_msvc()
    results = {}
    for name in CASES:
        print(f"[run] {name} ...", flush=True)
        results[name] = summarize(run_child(name))

    common.section("结构")
    rows = []
    for name, r in results.items():
        rows.append([
            name, str(r["kernel_count"]), str(r["vec_any"]), r["steps"], r["omp"],
            str(r["tmp_total"]), str(r["contiguous"]), str(r["stride"]),
        ])
    common.table(rows, ["case", "kernels", "vec", "step", "omp", "tmp", "contig", "stride"])

    common.section("时间（5 轮 x 20 次）")
    rows = []
    for name, r in results.items():
        rows.append([
            name, f"{r['median_ms']:.2f}", f"{r['min_ms']:.2f}",
            f"{r['max_ms']:.2f}", f"{r['drift']:.0%}", f"{r['ns_per_elem']:.1f}",
        ])
    common.table(rows, ["case", "median ms", "min", "max", "drift", "ns/elem"])

    common.section("断言")
    for name, r in results.items():
        common.check(f"{name} 数值", r["max_err"] <= 1e-5, f"{r['max_err']:.2e}")
    common.check("contig 是连续布局", results["contig"]["contiguous"])
    common.check("transpose 是非连续布局", not results["transpose"]["contiguous"])
    common.check("slice 的列 stride 为 2", results["slice"]["stride"][1] == 2,
                 str(results["slice"]["stride"]))

    report = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "practice_d_report.md"
    lines = [
        "# 练习 D 报告：布局变体性能扫掠",
        "",
        "| case | kernels | vec | step | omp | tmp | contig | stride | median ms | min | max | drift | ns/elem | max err |",
        "|---|---:|---|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, r in results.items():
        lines.append(
            f"| {name} | {r['kernel_count']} | {r['vec_any']} | {r['steps']} | {r['omp']} "
            f"| {r['tmp_total']} | {r['contiguous']} | {r['stride']} "
            f"| {r['median_ms']:.2f} | {r['min_ms']:.2f} | {r['max_ms']:.2f} "
            f"| {r['drift']:.0%} | {r['ns_per_elem']:.1f} | {r['max_err']:.2e} |"
        )
    lines += [
        "",
        "- 条件：torch 2.14.1+cpu，Windows x86_64，AVX2，每变体 5 轮 x 20 次，独立子进程与独立 cache。",
        "- `transpose` 与 `contig` 元素数相同，可直接比；`slice` 与 `contig_half` 元素数相同。",
        "- 漂移 = (max - min) / median；区间重叠时不要宣称差异。",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print("已写入:", report)
    print(report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
```

这段骨架的关键点：

- **每个 case 一个子进程、一个独立 cache**，避免 `lab_d_perf.py` 注释里说的“代码看起来是标量、跑起来仍是向量”；
- **第一次调用在 `cfg.patch` 内**，否则 config 不生效；
- **记录 `stride` 和 `is_contiguous()`**，报告里的布局是可验证的，不是口头声明；
- **`bench_rounds` 返回每轮中位数**，父进程再取中位数、最小、最大，避免把一轮抖动当结论；
- **解析所有 kernel**，不只 `kernel_stats(src, 0)`；布局变体可能先插入 copy kernel。

### 3. 多轮测量：先看区间，再看中位数

单轮 `bench_ms` 给出一个中位数，适合快速对照；本练习要回答“两个变体的差异是否超过噪声”，至少需要多轮。推荐：

- `rounds=5`，`iters=20`，`warmup=8`；
- 每轮内部连续调用，减少 Python 调度和计时器开销；
- 报告 `median`、`min`、`max` 和 `drift=(max-min)/median`；
- 如果两个变体的 `[min, max]` 区间重叠，结论写成“本轮不可区分”，不要写“A 比 B 快 3%”；
- 如果要更严格，重复整个子进程 2–3 次，比较“每轮中位数”的分布，而不是在同一进程里重复。

噪声来源：

| 来源 | 表现 | 处理 |
|---|---|---|
| CPU 频率/温度 | 同一变体前后漂移 | 多轮、交错（在可隔离的前提下）、报告区间 |
| 后台进程 | 某一轮明显变慢 | 记录环境，重复测量 |
| 线程调度 | 4 线程变体抖动更大 | 固定 `torch.set_num_threads`，报告线程数 |
| 首次编译/加载 | 第一轮很慢 | `warmup` 放在计时外 |
| 缓存命中 | 后续轮次异常快 | 明确 warmup 次数，不把编译时间混进 runtime |
| 元素数不同 | 绝对时间不可比 | 用 `ns/elem` 或同尺寸对照 |

`lab_d_perf.py` 的 `vec_again` 是一个好设计：同一配置重复一次，用漂移估计噪声。本练习把“重复一次”升级为“每变体多轮”，并在报告里保留漂移列。

### 4. 报告结构：让别人能复现你的结论

一份可用的报告至少包含：

1. **问题**：比较什么布局、什么 config、什么线程数；不做什么。
2. **环境**：OS、CPU、torch 版本、ISA、MSVC 版本、`torch.get_num_threads()`。
3. **变体表**：每个 case 的 shape、stride、是否连续、config patch、线程数。
4. **正确性**：每个 case 的 `max_err` 和容差；布局变化不应改变数值。
5. **结构**：kernel 数、是否向量化、step、omp 线程、tmp 指令、是否有 copy kernel。
6. **时间**：median/min/max/drift/ns-per-element；明确 rounds 和 iters。
7. **结论**：哪些差异超过噪声，哪些不可区分；布局影响是否伴随结构变化。
8. **限制**：只测了哪些 shape/布局；没有覆盖动态 shape、其他 dtype、其他机器。
9. **复现**：完整命令和报告路径。

报告模板：

```markdown
# 练习 D 报告：<你的标题>

## 条件
- OS / CPU / torch / ISA / threads
- 每个 case 的 rounds、iters、warmup

## 变体
| case | shape | stride | contiguous | config | threads |

## 正确性
| case | max_err | tolerance | 结论 |

## 结构
| case | kernels | vec | step | omp | tmp | 备注 |

## 时间
| case | median ms | min | max | drift | ns/elem |

## 结论
- 结构差异：
- 时间差异（是否超过噪声）：
- 未覆盖：
```

### 5. 结果判读：布局变体可能出现的四种现象

不预设具体数值，但可以预设**判读规则**：

1. **结构相同、时间接近**：布局没有触发 copy，load 仍能向量化；差异落在噪声内。结论写“本轮不可区分”，并说明 stride 已记录。
2. **结构相同、时间明显不同**：指令相同但访存模式不同，可能是 cache 局部性或对齐问题。检查 `stride`、`step`、是否有 tail loop；不要只归因于“转置更慢”。
3. **kernel 数增加**：很可能插入了 copy kernel。看 `output_code.py` 里新增 kernel 的读写；这不是错误，但会改变“一个 kernel”的结构断言，也会增加内存流量。
4. **向量化消失或 step 变小**：非连续访问让向量化条件不满足。看 `cpp.vec_isa_ok`、`step` 和 `Vectorized<float>`；如果退回标量，时间通常会明显变差，但仍要用区间判断。

一个常见误解是“转置一定更慢”。实际取决于访问模式和生成策略：如果 Inductor 先 copy 成连续再算，copy 的成本可能被后续的高效向量化抵消；如果直接带 stride load，可能避免 copy 但降低向量化效率。**不要用直觉替代测量，也不要用一次测量替代区间。**

## 可跑实验

先跑参考实验，确认本机结构和基线：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

预期看到：

- `vec`：`vectorized=yes`，`step=8`，`omp threads=4`；
- `scalar`：`vectorized=no`，`step=1`，`omp threads=4`；
- `threads1`：仍向量化，但没有 omp pragma（`threads` 为 `None`）；
- 标量明显慢于向量、单线程明显慢于四线程；`vec` 重复测量的漂移会打印出来，本机 30%–50% 属于常见范围；
- 所有变体数值通过，误差在 `1e-5` 以内。

然后把上面的骨架保存为 `code\labs\practice_d_layout.py`，运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\practice_d_layout.py
```

预期输出：

- 结构表里 `contig`、`scalar`、`threads1` 的 step/omp 与 `lab_d_perf.py` 一致；
- `transpose` 的 `contiguous=False`，`stride` 为 `[1, 2048]`；
- `slice` 的 `stride` 为 `[2048, 2]`，`numel` 是 `contig` 的一半；
- `contig_half` 与 `slice` 的 shape 相同，用于半尺寸对照；
- 报告写入 `compilers\inductor_deep_dive\code\.cache\practice_d_report.md`。

读报告：

```powershell
Get-Content -Encoding UTF8 compilers\inductor_deep_dive\code\.cache\practice_d_report.md
```

如果 `transpose` 的 `kernel_count` 大于 1，去 `code/.cache/practice_d/transpose/` 下找 `.debug` 或 `.py` 产物，确认多出来的 kernel 是不是 copy；如果 `slice` 的 `step` 不是 8，检查它是否退回标量，以及 `stride` 是否真的为 2。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/ir.py` | `FixedLayout`、`FlexibleLayout`、`TensorBox`、`StorageBox` | stride 如何表示，布局何时从 fixed 变 flexible |
| `torch/_inductor/graph.py` | `GraphLowering`、`GraphLowering.run` | 非连续输入在哪里被处理，是否插入 copy |
| `torch/_inductor/scheduler.py` | `SchedulerNode`、`FusedSchedulerNode`、`MemoryDep` | copy 作为调度节点如何参与融合和内存规划 |
| `torch/_inductor/codegen/cpp.py` | `CppKernel`、`CppVecKernel`、`CppKernelProxy`、`CppScheduling` | 向量化条件、tail loop、stride 对 load 的影响 |
| `torch/_inductor/loop_body.py` | `LoopBody` | 索引表达式如何从 IR 变量生成 |
| `torch/_inductor/ops_handler.py` | `OpOverrides`、`DefaultHandler` | `load`/`store` 如何映射到 C++ 表达式 |
| `torch/_inductor/memory.py` | `MemoryPlanningInfoForBuffer` / `MemoryPlanningState` | copy 和主 kernel 的 buffer 生命周期、峰值内存 |
| `compilers/inductor_deep_dive/code/labs/common.py` | `kernel_stats`、`bench_ms`、`interleaved_ms` | 课程侧的结构解析和计时工具；注意 `kernel_stats(src, index)` 的截取方式 |

关于 `common.kernel_stats` 的一个细节：它默认从第 `index` 个 kernel 开始，一直截到 `// Python bindings`。对 `index=0` 没问题；对多 kernel 文件，`index=1` 会把后面的 kernel 也包含进来，导致 `tmp_ops` 偏大。练习骨架里的 `split_kernels` 先按 `extern "C" __declspec(dllexport)` 切段，再把单段传给 `kernel_stats`，这样每个 kernel 的统计是独立的。这个细节在多 kernel 的布局变体里很重要。

阅读顺序：先看 `ir.py` 的 layout 表示，再看 `graph.py` 是否插入 copy，最后看 `codegen/cpp.py` 的向量化条件。不要从耗时反推源码行为；耗时只告诉你“有差异”，结构才告诉你“差异在哪”。

## 常见坑

1. **用 `x.t().contiguous()` 当转置变体。** 这会复制成连续张量，测到的是 copy 后的布局。保留 `x.t()` 视图，并用 `x.is_contiguous()` 记录。
2. **拿半尺寸和全尺寸比绝对时间。** `slice` 是 `(2048, 1024)`，`contig` 是 `(2048, 2048)`；直接比毫秒会把“元素少一半”误判成“布局更快”。用 `contig_half` 对照或报告 `ns/elem`。
3. **只统计第一个 kernel。** 布局变体可能先插入 copy kernel，`kernel_stats(src, 0)` 统计的是 copy 而不是主 kernel，`tmp_ops` 和 `step` 都会误导。用 `split_kernels` 遍历全部。
4. **单轮测量就下结论。** 本机漂移可达 30%–50%；两个变体的区间重叠时，写“不可区分”，不要写“快 3%”。
5. **跨变体复用同一 cache。** 同进程连续编译不同 config/布局可能命中旧产物；每个 case 用独立子进程和独立 `TORCHINDUCTOR_CACHE_DIR`。
6. **把编译时间混进 runtime。** `compile_s` 单独记录；`bench_rounds` 的 warmup 放在计时外，报告里分别写“首次编译”和“稳态运行”。
7. **忘记固定线程数。** 子进程里 `torch.set_num_threads(spec["threads"])`，父进程不要假设默认值；`threads1` 仍然向量化，只是没有 omp pragma。
8. **把 structure 断言写成 timing 断言。** “标量必须慢 1.5 倍”在噪声大的机器上会偶发失败；结构（step、Vectorized、omp）是硬证据，时间是软证据。
9. **忽略 tail loop。** 长度是 8 的倍数时尾部可能不执行；换成 `2047` 或 `2049` 才能看到尾部代码。本练习的 2048 是主实验，不能替代边界测试。

## 提交要求

交付两项：

1. `compilers\inductor_deep_dive\code\labs\practice_d_layout.py`：能一次跑完 6 个 case，写出 `code\.cache\practice_d_report.md`；
2. `compilers\inductor_deep_dive\code\.cache\practice_d_report.md`：包含条件、变体表、正确性、结构、时间、结论、限制、复现命令。

报告至少要回答：

- `transpose` 与 `contig` 的元素数相同，时间差是否超过两者的漂移区间？
- `slice` 与 `contig_half` 的元素数相同，`ns/elem` 是否接近？如果差很多，结构上有什么不同？
- 有没有 case 的 `kernel_count` 大于 1？如果有，多出来的是 copy 还是别的？
- 哪个 case 的 `step` 不是 8？它是否同时失去 `Vectorized<float>`？
- 你的结论在哪些 shape/布局/dtype 上没有验证？

<details>
<summary>参考判读与评分要点</summary>

参考判读：

- 如果 `transpose` 的 `stride` 是 `[1, 2048]`、`contiguous=False`，说明布局确实是非连续的；如果 `stride` 仍是 `[2048, 1]`，你测的是拷贝。
- 如果 `slice` 的 `stride` 是 `[2048, 2]`，`numel` 是 `2048*1024`；和 `contig_half` 比 `ns/elem`，不要和 `contig` 比毫秒。
- 如果某个 case 的 `kernel_count=2`，先看第一个 kernel 是否只做 load/store；copy 会改变 `tmp_total` 和内存流量，但不改变最终数值。
- 如果 `scalar` 的 `step=1` 且 `vectorized=False`，说明 `cpp.vec_isa_ok=False` 生效；如果 `threads1` 仍有 `Vectorized<float>` 但没有 omp，说明线程和向量化是两个独立维度。
- 时间结论只对“同一台机器、同一轮测量、同一组 shape”负责。区间重叠就写“不可区分”，这是合格结论，不是失败。

评分要点（满分 100）：

| 项目 | 分值 | 合格标准 |
|---|---:|---|
| 变量控制 | 20 | config 与 layout 两轴拆开；同元素数对照正确 |
| 正确性 | 20 | 每个 case 与 eager 对照；容差与 dtype 匹配 |
| 结构证据 | 20 | 记录 kernel 数、step、向量化、omp、tmp；解释 copy |
| 测量方法 | 20 | 子进程隔离、多轮、warmup、median/min/max/drift |
| 报告质量 | 10 | 条件、结论、限制、复现命令齐全 |
| 代码质量 | 10 | 复用 `common`，无明显复制粘贴，异常路径可读 |

扣分项：用 `.contiguous()` 伪造转置；拿不同元素数直接比毫秒；只统计第一个 kernel；单轮下结论；声称“显著加速”但区间重叠；没有记录 `stride`。

</details>

## 自我检查

- [ ] 我能解释 `lab_d_perf.py` 为什么要给每个变体起独立子进程和独立 cache。
- [ ] 我能写出连续、转置、切片三种布局，并验证它们的 `stride` 和 `is_contiguous()`。
- [ ] 我知道 `slice` 不能和全尺寸 `contig` 直接比绝对时间，应该用 `contig_half` 或 `ns/elem`。
- [ ] 我能用 `split_kernels` 遍历所有生成 kernel，而不是只看 `kernel_stats(src, 0)`。
- [ ] 我能区分“kernel 数增加”与“向量化消失”，并分别给出源码层面的解释。
- [ ] 我能设计 5 轮 × 20 次的测量，并报告 median/min/max/drift。
- [ ] 我知道区间重叠时应写“不可区分”，而不是强行排名。
- [ ] 我能把编译时间与稳态运行时间分开记录。
- [ ] 我能在报告里写清未覆盖的 shape、布局和 dtype。
- [ ] 我能用统一入口复现报告里的每一个数字。

## 延伸阅读

- **PyTorch 文档，Tensor views / strides / `is_contiguous`**：读 stride 的定义和 view 与 copy 的区别；本章的 `x.t()` 与 `x[:, ::2]` 都基于这些语义。
- **PyTorch 文档，`torch.compile` performance guide**：读动态 shape、graph break、benchmark 的注意事项；把本章的“结构优先、时间辅助”放回官方语境。
- **TorchInductor 源码，`torch/_inductor/codegen/cpp.py`**：读 `CppVecKernel` 的向量化条件和 `CppKernel` 的循环生成；对照 `step=8` 与 `step=1`。
- **TorchInductor 源码，`torch/_inductor/ir.py` 与 `scheduler.py`**：读 layout、`MemoryDep`、copy 节点如何进入调度；解释为什么布局变体会多一个 kernel。
- **`compilers/inductor_deep_dive/15-vectorization.md` 与 `16-parallelization.md`**：复习 `simdlen` 是位宽、`threads=1` 仍向量化；本练习是它们的横向扩展。
- **`compilers/mini_ai_compiler/19-vectorization-and-flags.md`**：读自己实现的后端如何做向量化和尾部循环；对照生产 Inductor 的生成代码，差异更容易看清。
- **Agner Fog, “Optimizing software in C++”**：读 cache、对齐、循环展开和向量化的工程细节；用来解释“结构相同但时间不同”的访存原因。

---

[上一章：Ch 23 改造 Inductor 与后续路线](./23-modify-inductor-and-next-steps.md) | [下一章：Final Project](./final-project-inductor-deep-dive.md)