"""观察 Dynamo 抓了什么：三种写法在 graph break 上的差异。
运行：TORCH_LOGS=graph_breaks ~/.venvs/learn/bin/python capture.py
"""
import torch


def simple(x):
    return torch.relu(x * 2 + 1)


def with_scalar(x):
    s = float(x.sum())      # 触发 graph break
    return x + s


def with_python_loop(x):
    y = x
    for _ in range(3):      # 编译期可知的循环 -> 展开，不是 graph break
        y = y * 2
    return y


if __name__ == "__main__":
    for name, fn in [("simple", simple),
                     ("with_scalar", with_scalar),
                     ("with_python_loop", with_python_loop)]:
        cf = torch.compile(fn)
        out = cf(torch.randn(4))
        print(f"{name}: ok, shape={tuple(out.shape)}")
