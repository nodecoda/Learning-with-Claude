"""实验 D：把「生成代码的差异」换算成可复现的时间差异。

三个变体，同一份数学 y = relu(sin(x) * 2 + y)：
  vec      : 默认，AVX2，4 线程
  scalar   : cpp.vec_isa_ok = False，退回标量
  threads1 : torch.set_num_threads(1)，kernel 不再生成 omp pragma

为什么每个变体单独起一个子进程：
  同一个进程里连续编译多个 config 变体时，Inductor/Dynamo 的缓存可能复用上一次
  的产物，导致「代码看起来是标量、跑起来仍是向量」。隔离进程 + 独立 cache 目录，
  才能保证测的就是刚生成的那份代码。这是本实验最重要的工程教训之一。

运行：
    .\code\tools\run.ps1 code\labs\lab_d_perf.py
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from labs import common

N = 2048  # 4M 元素
VARIANTS = {
    "vec": ({}, 4),
    "scalar": ({"cpp.vec_isa_ok": False}, 4),
    "threads1": ({}, 1),
}


def child(name: str) -> None:
    """子进程：只编译并测量一个变体，把结果以 JSON 打到 stdout。"""
    import torch
    import torch._inductor.config as cfg

    patches, threads = VARIANTS[name]
    torch.set_num_threads(threads)

    def f(x, y):
        return torch.relu(torch.sin(x) * 2.0 + y)

    x = torch.randn(N, N)
    y = torch.randn(N, N)
    ref = torch.relu(torch.sin(x) * 2.0 + y)

    t0 = time.perf_counter()
    with cfg.patch(patches):
        before = common.modules_snapshot()
        fn = torch.compile(f)
        out = fn(x, y)
        mods = common.modules_since(before)
        if not mods:
            raise SystemExit("没有捕获到生成的模块")
        src = common.read_text_any(mods[-1].__file__)
    compile_s = time.perf_counter() - t0

    err = (out - ref).abs().max().item()
    median = common.bench_ms(lambda: fn(x, y), iters=25, warmup=8)
    payload = {
        "variant": name,
        "median_ms": median,
        "compile_s": compile_s,
        "max_err": err,
        "stats": common.kernel_stats(src),
    }
    print("RESULT " + json.dumps(payload))


def run_child(name: str, tag: str) -> dict:
    env = dict(os.environ)
    cache = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "perf" / tag
    if cache.exists():
        shutil.rmtree(cache)
    env["TORCHINDUCTOR_CACHE_DIR"] = str(cache)
    proc = subprocess.run(
        [sys.executable, __file__, "--variant", name],
        env=env,
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:])
    raise SystemExit(f"{name} 子进程没有返回结果")


def main() -> None:
    if "--variant" in sys.argv:
        child(sys.argv[sys.argv.index("--variant") + 1])
        return

    common.require_msvc()
    results = {}
    for name in VARIANTS:
        print(f"[run] {name} ...", flush=True)
        results[name] = run_child(name, name)
    print("[run] vec (重复一次，用于估计噪声) ...", flush=True)
    results["vec_again"] = run_child("vec", "vec_again")

    common.section("1) 生成代码的结构差异")
    rows = []
    for name, r in results.items():
        st = r["stats"]
        rows.append([
            name,
            "yes" if st["vectorized"] else "no",
            str(st["step"]),
            str(st["threads"] if st["threads"] else "none"),
            f"{r['compile_s']:.2f}s",
        ])
    common.table(rows, ["variant", "vectorized", "step", "omp threads", "compile"])

    common.section("2) 运行时间（每项 25 次，取中位数）")
    base = results["vec"]["median_ms"]
    rows = []
    for name, r in results.items():
        rows.append([name, f"{r['median_ms']:.2f}", f"{base / r['median_ms']:.2f}x"])
    common.table(rows, ["variant", "median ms", "vs vec"])

    common.section("3) 断言")
    vec = results["vec"]
    sc = results["scalar"]
    t1 = results["threads1"]
    again = results["vec_again"]
    common.check("vec 走 8 元素向量", vec["stats"]["vectorized"] and vec["stats"]["step"] == 8,
                 f"step={vec['stats']['step']}")
    common.check("scalar 退化为 1 元素步长",
                 (not sc["stats"]["vectorized"]) and sc["stats"]["step"] == 1,
                 f"step={sc['stats']['step']}")
    common.check("threads1 不再生成 omp pragma", t1["stats"]["threads"] is None,
                 str(t1["stats"]["threads"]))
    common.check("标量明显慢于向量", sc["median_ms"] > 1.5 * vec["median_ms"],
                 f"{sc['median_ms']:.2f} vs {vec['median_ms']:.2f} ms")
    common.check("单线程明显慢于四线程", t1["median_ms"] > 1.3 * vec["median_ms"],
                 f"{t1['median_ms']:.2f} vs {vec['median_ms']:.2f} ms")
    drift = abs(again["median_ms"] - vec["median_ms"]) / vec["median_ms"]
    print(f"[INFO] vec 重复测量漂移: {drift:.0%} "
          f"({vec['median_ms']:.2f} vs {again['median_ms']:.2f} ms) "
          "—— 这台机器上 30%~50% 漂移是常态，别用单次结果下结论")
    common.check("vec 重复测量未出现量级异常（< 2x）", drift < 1.0, f"{drift:.0%}")
    for r in results.values():
        common.check(f"{r['variant']} 数值", r["max_err"] <= 1e-5, f"{r['max_err']:.2e}")

    print()
    print("结论：先看结构（step / omp / 指令），再看时间；")
    print("      时间只对『同一台机器、同一轮测量』内的相对关系负责。")
    print("      变体之间必须进程隔离，否则缓存复用会让 benchmark 说谎。")


if __name__ == "__main__":
    main()