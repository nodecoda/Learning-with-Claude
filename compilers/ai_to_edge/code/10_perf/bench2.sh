#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "== warmup 效应：同一个 matmul 连续 5 次 =="
/home/ubuntu/.venvs/learn/bin/python - <<'PY'
import time, torch
torch.set_num_threads(1)
a = torch.randn(512,512); b = torch.randn(512,512)
for i in range(5):
    t=time.perf_counter(); a@b; print(f"  call {i}: {(time.perf_counter()-t)*1e3:.3f} ms")
PY
echo
echo "== 中位数 vs 均值（100 次 1024 matmul） =="
/home/ubuntu/.venvs/learn/bin/python - <<'PY'
import time, statistics, torch
torch.set_num_threads(1)
a = torch.randn(1024,1024); b = torch.randn(1024,1024)
xs=[]
for _ in range(100):
    t=time.perf_counter(); a@b; xs.append((time.perf_counter()-t)*1e3)
xs.sort()
print(f"  min={xs[0]:.2f} p50={statistics.median(xs):.2f} mean={statistics.mean(xs):.2f} p99={xs[98]:.2f} max={xs[-1]:.2f} ms")
PY
