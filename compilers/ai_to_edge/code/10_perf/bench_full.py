#!/usr/bin/env python3
"""Ch 26 主脚本：warmup + min/median/max 统计
环境：torch 2.9.1+cpu，单线程
运行：/home/ubuntu/.venvs/learn/bin/python bench_full.py
"""
import statistics
import time

import torch

torch.manual_seed(0)
torch.set_num_threads(1)


def bench(fn, warmup=5, reps=30):
    for _ in range(warmup):
        fn()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1e3)
    return min(ts), statistics.median(ts), max(ts)


print(f"{'case':22s} {'min/ms':>8s} {'med/ms':>8s} {'max/ms':>8s} {'GFLOP/s':>8s}")
for M in (64, 256, 512, 1024):
    a = torch.randn(M, M)
    b = torch.randn(M, M)
    mn, md, mx = bench(lambda: a @ b, reps=200 if M <= 256 else 20)
    print(f"{'matmul %d^3' % M:22s} {mn:8.3f} {md:8.3f} {mx:8.3f} {2*M**3/(md/1e3)/1e9:8.2f}")

n = 1 << 24
x = torch.randn(n)
mn, md, mx = bench(lambda: torch.relu(x * 2 + 1), warmup=3, reps=15)
print(f"{'elementwise n=2^24':22s} {mn:8.2f} {md:8.2f} {mx:8.2f} {'-':>8s}  "
      f"{(n*4*2)/(md/1e3)/1e9:.2f} GB/s")
