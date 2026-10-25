"""实验 B：写一个真正改变生成代码的 post-grad pass。

变换：relu(x) + relu(-x)  ->  abs(x)
基线需要 load + clamp_min + neg + clamp_min + add 五条向量指令，
改写后只剩 load + abs。两者在 float32 上逐元素完全相等。

运行：
    .\code\tools\run.ps1 code\labs\lab_b_pass.py
"""
from __future__ import annotations

import torch
import torch._inductor.config as cfg
from torch._inductor.utils import fresh_inductor_cache
from torch._inductor.codecache import PyCodeCache

from labs import common

aten = torch.ops.aten
SEEN: dict[str, list[str]] = {}


def relu_pair_to_abs(graph):
    """post_grad_custom_pre_pass 收到的是 torch.fx.Graph，不是 GraphModule。"""
    names = [str(getattr(n, "target", n.op)) for n in graph.nodes]
    SEEN.setdefault("graphs", []).append(names)

    for node in list(graph.nodes):
        if node.op != "call_function" or node.target is not aten.add.Tensor:
            continue
        a, b = node.args[:2]
        if not (isinstance(a, torch.fx.Node) and isinstance(b, torch.fx.Node)):
            continue
        if a.op != "call_function" or a.target is not aten.relu.default:
            continue
        if b.op != "call_function" or b.target is not aten.relu.default:
            continue
        neg = b.args[0]
        if not (isinstance(neg, torch.fx.Node) and neg.op == "call_function"
                and neg.target is aten.neg.default):
            continue
        if neg.args[0] is not a.args[0]:
            continue
        with graph.inserting_before(node):
            new = graph.call_function(aten.abs.default, (a.args[0],))
        node.replace_all_uses_with(new)

    graph.eliminate_dead_code()
    graph.lint()
    return graph


def make_model():
    def f(x):
        return torch.relu(x) + torch.relu(-x)
    return f


x = torch.randn(1024, 1024)
ref = torch.relu(x) + torch.relu(-x)


def compile_variant(patches: dict) -> tuple[object, str]:
    torch._dynamo.reset()
    with fresh_inductor_cache(), cfg.patch(patches):
        before = common.modules_snapshot()
        fn = torch.compile(make_model(), fullgraph=True)
        fn(x)  # 预热必须发生在 patch 作用域内，否则会按默认 config 编译
        mods = common.modules_since(before)
        if not mods:
            raise SystemExit("没有捕获到本次编译生成的模块")
        src = common.read_text_any(mods[-1].__file__)
    return fn, src


base_fn, base_src = compile_variant({})
opt_fn, opt_src = compile_variant({"post_grad_custom_pre_pass": relu_pair_to_abs})

common.section("1) pass 看到的图")
print("  基线调用次数:", len(SEEN.get("graphs", [])))
print("  节点:", " -> ".join(SEEN["graphs"][-1]))

common.section("2) 基线 kernel 的向量指令")
base_body = common.extract_kernel(base_src)
for line in base_body.splitlines():
    if "auto tmp" in line:
        print("  " + line.strip())

common.section("3) 改写后 kernel 的向量指令")
opt_body = common.extract_kernel(opt_src)
for line in opt_body.splitlines():
    if "auto tmp" in line:
        print("  " + line.strip())

common.section("4) 数字与结论")
y_base = base_fn(x)
y_opt = opt_fn(x)
common.assert_close("基线数值", y_base, ref, tol=0.0)
common.assert_close("改写后数值", y_opt, ref, tol=0.0)
b = common.kernel_stats(base_src)
o = common.kernel_stats(opt_src)
common.check("指令数下降", o["tmp_ops"] < b["tmp_ops"], f"{b['tmp_ops']} -> {o['tmp_ops']}")
common.check("使用 abs", "abs(" in opt_body, str(o["uses_abs"]))
print()
print(f"kernel 行数：{b['cpp_lines']} -> {o['cpp_lines']}")