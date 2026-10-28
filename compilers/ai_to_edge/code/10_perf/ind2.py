#!/usr/bin/env python3
"""Ch 26：区分「编译时间 / 预热 / 稳态」
环境：torch 2.9.1+cpu，单线程
运行：/home/ubuntu/.venvs/learn/bin/python ind2.py
"""
import statistics
import time

import torch

torch.set_num_threads(1)


def build():
    class M(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.w = torch.nn.Parameter(torch.randn(64, 64))

        def forward(self, x):
            return torch.relu(x @ self.w + 1.0)

    m = M().eval()
    return torch.compile(m), torch.randn(4, 64)


def main():
    cf, x = build()
    t = time.perf_counter()
    with torch.no_grad():
        cf(x)
    first = (time.perf_counter() - t) * 1e3

    t = time.perf_counter()
    with torch.no_grad():
        cf(x)
    second = (time.perf_counter() - t) * 1e3

    xs = []
    for _ in range(30):
        t = time.perf_counter()
        with torch.no_grad():
            cf(x)
        xs.append((time.perf_counter() - t) * 1e3)

    print(f"first  = {first:.1f} ms")
    print(f"second = {second:.2f} ms")
    print(f"steady median = {statistics.median(xs):.2f} ms  min = {min(xs):.2f} ms")
    print(f"first / steady = {first / statistics.median(xs):.0f}x")


if __name__ == "__main__":
    main()
