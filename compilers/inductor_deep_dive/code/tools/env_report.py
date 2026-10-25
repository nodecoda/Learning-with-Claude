"""打印本机 TorchInductor 实验环境，并做一次最小编译自检。

运行：
    .\code\tools\run.ps1 code\tools\env_report.py
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import time


def main() -> None:
    import torch
    import torch._inductor as ind
    from torch._inductor.cpu_vec_isa import pick_vec_isa, valid_vec_isa_list

    print("=" * 60)
    print("Python    :", sys.version.split()[0], sys.executable)
    print("OS        :", platform.platform())
    print("torch     :", torch.__version__)
    print("inductor  :", os.path.dirname(ind.__file__))
    print("cl.exe    :", shutil.which("cl") or "NOT FOUND")
    print("threads   :", torch.get_num_threads())
    print("cache dir :", os.environ.get("TORCHINDUCTOR_CACHE_DIR", "(unset)"))
    print("CPU ISA   :", [type(v).__name__ for v in valid_vec_isa_list()],
          "-> picked", type(pick_vec_isa()).__name__)

    if shutil.which("cl") is None:
        raise SystemExit(
            "cl.exe 不在 PATH。请用 code/tools/run.ps1 运行，或先执行 vcvars64.bat。"
        )

    print("=" * 60)
    print("最小编译自检 ...")
    t0 = time.perf_counter()

    def f(x):
        return torch.relu(torch.sin(x) + 1.0) * 2.0

    cf = torch.compile(f)
    x = torch.randn(64, 64)
    out = cf(x)
    err = (out - f(x)).abs().max().item()
    print(f"first call : {time.perf_counter() - t0:.2f}s")
    print(f"max abs err: {err:.2e}")
    if err > 1e-5:
        raise SystemExit("编译结果与 eager 不一致")
    print("OK：本机可以运行本课程的实验。")


if __name__ == "__main__":
    main()