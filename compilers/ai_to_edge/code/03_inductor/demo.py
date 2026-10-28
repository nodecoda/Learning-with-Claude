"""torch.compile 到 Inductor：真编译、真对比、真看产物。

环境：torch 2.9.1+cpu / Python 3.10 / x86_64
运行：TORCHINDUCTOR_CACHE_DIR=./cache python demo.py
"""
import os
import torch

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", CACHE)


def f(x, y):
    return torch.relu(x @ y + 1.0)


def main():
    torch.manual_seed(0)
    x = torch.randn(128, 256)
    y = torch.randn(256, 64)

    ref = f(x, y)                      # eager 参考
    cf = torch.compile(f, backend="inductor")
    out = cf(x, y)                     # 编译后

    print("torch:", torch.__version__)
    print("max abs diff:", (out - ref).abs().max().item())
    print("shape:", tuple(out.shape))


if __name__ == "__main__":
    main()
