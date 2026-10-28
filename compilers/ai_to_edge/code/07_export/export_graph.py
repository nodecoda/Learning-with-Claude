"""抓图：三种写法在图上的差异。本机实测 torch 2.9.1+cpu。

运行：~/.venvs/learn/bin/python export_graph.py
"""
import torch


class ThreeNodes(torch.nn.Module):
    def forward(self, x, w):
        return torch.relu(x @ w + 1.0)


class Addmm(torch.nn.Module):
    def forward(self, x, w, b):
        return torch.relu(torch.addmm(b, x, w))


def show(name, mod, args):
    ep = torch.export.export(mod, args)
    print(f"=== {name} ===")
    print(ep.graph)
    print()


def main():
    show("x @ w + 1.0  (三个节点)",
         ThreeNodes(), (torch.randn(4, 8), torch.randn(8, 4)))
    show("torch.addmm(b, x, w)  (两个节点)",
         Addmm(), (torch.randn(4, 8), torch.randn(8, 4), torch.randn(4)))


if __name__ == "__main__":
    main()
