"""实验 A：把一次编译拆成可观察的 6 个阶段。

运行：
    .\code\tools\run.ps1 code\labs\lab_a_pipeline.py
"""
from __future__ import annotations

import re

import torch
import torch._inductor.config as cfg

from labs import common

common.require_msvc()
cfg.trace.enabled = True  # 让 Inductor 把中间产物写进 cache 的 *.debug 目录


def model(x, w, b):
    h = torch.relu(x @ w + b)          # addmm（extern kernel）+ relu（生成 kernel）
    return h.sum(dim=1) / h.shape[1]   # reduction（另一个生成 kernel）


def main() -> None:
    torch.manual_seed(0)
    x = torch.randn(64, 64)
    w = torch.randn(64, 64)
    b = torch.randn(64)

    compiled = torch.compile(model)
    out = compiled(x, w, b)
    common.assert_close("数值与 eager 一致", out, model(x, w, b))

    debug_dir = common.newest_debug_dir()
    code_path = debug_dir / "output_code.py"
    src = common.read_text_any(code_path)

    common.section("1) 编译产物位置")
    print(debug_dir)
    for name in ["fx_graph_readable.py", "ir_pre_fusion.txt", "ir_post_fusion.txt", "output_code.py"]:
        p = debug_dir / name
        print(f"  {name:22s} {p.stat().st_size:7d} B")

    common.section("2) AOTAutograd 之后的 core ATen 图（节选）")
    print(common.snippet(common.read_text_any(debug_dir / "fx_graph_readable.py"), 18))

    common.section("3) 调度前 IR：SchedulerNode 与 loop body（节选）")
    pre = common.read_text_any(debug_dir / "ir_pre_fusion.txt")
    print(common.snippet(pre, 34))

    common.section("4) 生成的 C++ kernel 结构")
    stats = common.kernel_stats(src)
    for k, v in stats.items():
        print(f"  {k:12s} {v}")

    common.section("5) wrapper 侧的调用（节选）")
    call_start = src.find("def call(self, args)")
    call_end = src.find("runner = Runner", call_start)
    print(src[call_start:call_end].strip())

    common.section("6) 结构断言")
    extern = common.wrapper_extern_calls(src)
    print("  extern kernels:", extern)
    common.check("存在生成的 C++ kernel", stats["name"] != "?", str(stats["name"]))
    common.check("matmul 走 extern kernel", any(k in extern for k in ("addmm", "mm", "bmm")), str(extern))
    common.check("调度 IR 里有 SchedulerNode", "SchedulerNode" in pre)
    common.check("kernel 被向量化", bool(stats["vectorized"]), f"step={stats['step']}")

    print()
    print("下一步：打开上面 4 个文件，按 Ch 1 的『同一段语义、四种表示』表格逐项对照。")


if __name__ == "__main__":
    main()