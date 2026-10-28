#!/usr/bin/env python3
"""Ch 26 陷阱 6：分配开销盖过计算
环境：torch 2.9.1+cpu，单线程，n=2^24（64 MB 的 float32）
运行：/home/ubuntu/.venvs/learn/bin/python alloc_cost.py
"""
import statistics
import time

import torch

torch.set_num_threads(1)
n = 1 << 24
x = torch.randn(n)
buf = torch.empty(n)
tmp = torch.empty(n)


def med(fn, it=15):
    fn()
    fn()
    xs = []
    for _ in range(it):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1e3)
    return statistics.median(xs)


def chain_prealloc():
    torch.mul(x, 2, out=buf)
    torch.add(buf, 1, out=tmp)
    torch.clamp_min(tmp, 0, out=buf)


rows = [
    ("x.mul_(1.0)     原地", lambda: x.mul_(1.0)),
    ("add(x,1,out=)   预分配", lambda: torch.add(x, 1, out=buf)),
    ("mul(x,2,out=)   预分配", lambda: torch.mul(x, 2, out=buf)),
    ("x * 2           每次新分配", lambda: x * 2),
    ("x + 1           每次新分配", lambda: x + 1),
    ("relu(x)         每次新分配", lambda: torch.relu(x)),
    ("relu(x*2+1)     3 次新分配", lambda: torch.relu(x * 2 + 1)),
    ("三段链（全预分配）", chain_prealloc),
]

print(f"{'写法':32s} {'中位数 ms':>10s} {'GB/s':>8s}")
print("-" * 54)
for name, fn in rows:
    ms = med(fn)
    # 按「读一次 + 写一次」估算，每段链按 2 段算
    factor = 2 if "三段链" in name else 1
    gbs = n * 4 * 2 * factor / (ms / 1e3) / 1e9
    print(f"{name:32s} {ms:10.2f} {gbs:8.2f}")

nat = med(lambda: torch.relu(x * 2 + 1))
pre = med(chain_prealloc)
print(f"\n自然写法 / 预分配 = {nat / pre:.1f}x")
