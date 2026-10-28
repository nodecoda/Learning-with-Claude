#!/usr/bin/env python3
"""Ch 26：matmul 的 min/median/max 统计
环境：torch 2.9.1+cpu，单线程
运行：/home/ubuntu/.venvs/learn/bin/python matmul_stats.py
"""
import statistics
import time

import torch

torch.set_num_threads(1)
print(f"{'case':24s} {'min':>8s} {'med':>8s} {'max':>8s} {'GFLOP/s':>9s}")
for M, N, K in [(64, 64, 64), (256, 256, 256), (512, 512, 512), (1024, 1024, 1024)]:
    a = torch.randn(M, K)
    b = torch.randn(K, N)
    for _ in range(5):
        a @ b
    xs = []
    iters = 200 if M <= 256 else 20
    for _ in range(iters):
        t = time.perf_counter()
        a @ b
        xs.append((time.perf_counter() - t) * 1e3)
    xs.sort()
    med = statistics.median(xs)
    gf = 2 * M * N * K / (med / 1e3) / 1e9
    print(f"matmul {M}x{N}x{K}".ljust(24) + f"{xs[0]:8.3f} {med:8.3f} {xs[-1]:8.3f} {gf:9.2f}")
