"""Final Project 参考实现：给一个真实小模型做「可验证的编译期改造」。

模型（推理）：
    z = x @ w + b                # addmm -> extern kernel
    y = sum(relu(z) + relu(-z))  # 恒等于 sum(|z|)

改造：注册 post_grad_custom_pre_pass，把 relu(z) + relu(-z) 改写成 abs(z)，
把融合 kernel 的向量指令从 4 条降到 1 条；用差分测试 + 隔离 benchmark 出报告。

运行：
    .\code\tools\run.ps1 code\labs\final_project.py

产出：
    code/.cache/final_report.md
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import torch

from labs import common


def relu_pair_to_abs(graph):
    aten = torch.ops.aten
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


def model(x, w, b):
    z = x @ w + b
    return (torch.relu(z) + torch.relu(-z)).sum(dim=1)


def child(variant: str) -> None:
    import torch._inductor.config as cfg
    import torch._inductor.metrics as metrics

    torch.manual_seed(0)
    torch.set_num_threads(4)
    x = torch.randn(512, 512)
    w = torch.randn(512, 512)
    b = torch.randn(512)
    ref = model(x, w, b)

    patches = {"post_grad_custom_pre_pass": relu_pair_to_abs} if variant == "optimized" else {}
    torch._dynamo.reset()
    metrics.reset()
    with cfg.patch(patches):
        before = common.modules_snapshot()
        fn = torch.compile(model, fullgraph=True)
        out = fn(x, w, b)
        mods = common.modules_since(before)
        if not mods:
            raise SystemExit("没有捕获到生成的模块")
        src = common.read_text_any(mods[-1].__file__)

    median = common.bench_ms(lambda: fn(x, w, b), iters=30, warmup=8)
    stats = common.kernel_stats(src)
    payload = {
        "variant": variant,
        "median_ms": median,
        "max_err": (out - ref).abs().max().item(),
        "kernels": metrics.generated_kernel_count,
        "vec_kernels": metrics.generated_cpp_vec_kernel_count,
        "tmp_ops": stats["tmp_ops"],
        "cpp_lines": stats["cpp_lines"],
        "extern": common.wrapper_extern_calls(src),
    }
    print("RESULT " + json.dumps(payload))


def run_child(variant: str) -> dict:
    env = dict(os.environ)
    cache = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "final" / variant
    if cache.exists():
        shutil.rmtree(cache)
    env["TORCHINDUCTOR_CACHE_DIR"] = str(cache)
    proc = subprocess.run(
        [sys.executable, __file__, "--variant", variant],
        env=env, capture_output=True, text=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT "):])
    print(proc.stdout[-3000:])
    print(proc.stderr[-3000:])
    raise SystemExit(f"{variant} 子进程失败")


def main() -> None:
    if "--variant" in sys.argv:
        child(sys.argv[sys.argv.index("--variant") + 1])
        return

    common.require_msvc()
    print("[final] 编译 baseline ...", flush=True)
    base = run_child("baseline")
    print("[final] 编译 optimized ...", flush=True)
    opt = run_child("optimized")

    common.section("测量结果")
    rows = [
        ["baseline", str(base["kernels"]), str(base["vec_kernels"]),
         str(base["tmp_ops"]), f"{base['median_ms']:.2f}", f"{base['max_err']:.2e}"],
        ["optimized", str(opt["kernels"]), str(opt["vec_kernels"]),
         str(opt["tmp_ops"]), f"{opt['median_ms']:.2f}", f"{opt['max_err']:.2e}"],
    ]
    common.table(rows, ["variant", "kernels", "vec", "tmp ops", "median ms", "max err"])

    common.section("断言")
    # 512 项 fp32 求和的累加顺序与 eager 不同，1e-3 量级误差属正常；改写本身是精确的。
    common.check("数学等价（fp32 归约容差）", opt["max_err"] <= 5e-3, f"{opt['max_err']:.2e}")
    common.check("kernel 数不增加", opt["kernels"] <= base["kernels"],
                 f"{base['kernels']} -> {opt['kernels']}")
    common.check("融合 kernel 指令数下降", opt["tmp_ops"] < base["tmp_ops"],
                 f"{base['tmp_ops']} -> {opt['tmp_ops']}")
    common.check("matmul 仍走 extern kernel", "addmm" in base["extern"], str(base["extern"]))

    report = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "final_report.md"
    md = f"""# Final Project 报告：relu-pair -> abs

| variant | kernels | vectorized kernels | vector tmp ops | median ms | max abs err |
|---|---:|---:|---:|---:|---:|
| baseline | {base['kernels']} | {base['vec_kernels']} | {base['tmp_ops']} | {base['median_ms']:.2f} | {base['max_err']:.2e} |
| optimized | {opt['kernels']} | {opt['vec_kernels']} | {opt['tmp_ops']} | {opt['median_ms']:.2f} | {opt['max_err']:.2e} |

- extern kernels: {', '.join(base['extern'])}
- optimized / baseline: {opt['median_ms'] / base['median_ms']:.2f}x
- 说明：单机单轮测量，仅用于同一会话内的相对比较。
"""
    report.write_text(md, encoding="utf-8")
    common.section("报告")
    print(md)
    print("已写入:", report)


if __name__ == "__main__":
    main()