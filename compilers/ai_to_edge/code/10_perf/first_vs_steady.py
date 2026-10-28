#!/usr/bin/env python3
"""Ch 26 例子 2：冷启动 vs 稳态
运行：/home/ubuntu/.venvs/learn/bin/python first_vs_steady.py
"""
import time

import torch

torch.manual_seed(0)
torch.set_num_threads(1)


class M(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.a = torch.nn.Linear(128, 128)
        self.b = torch.nn.Linear(128, 128)

    def forward(self, x):
        return self.b(torch.relu(self.a(x)))


m = M().eval()
x = torch.randn(64, 128)
cm = torch.compile(m)


def t():
    t0 = time.perf_counter()
    cm(x)
    return (time.perf_counter() - t0) * 1e3


first = t()
print("first  = %.1f ms" % first)
ts = [t() for _ in range(20)]
ts_sorted = sorted(ts)
print("second = %.2f ms" % ts[0])
print("steady median = %.2f ms  min = %.2f ms" % (ts_sorted[10], min(ts)))
print("first / steady = %.0fx" % (first / ts_sorted[10]))
