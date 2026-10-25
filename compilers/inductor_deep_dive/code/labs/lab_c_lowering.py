"""实验 C：给自定义算子写 lowering，让它可以被融合进生成的 kernel。

同一份数学（y = relu(x * a + b)），两个自定义算子：
  1. mylab.affine_naive：不注册 lowering —— Inductor 只能把调用留在 wrapper 里，
     逐 op 走 eager（隐式 fallback），relu 也只能单独成一个 kernel；
  2. mylab.affine：注册 lowering —— 算子被翻译成 IR 的 mul/add，
     和 relu 融进同一个 loop nest、同一个 kernel。

注意：lowering 要在该算子第一次出现在图里之前注册好；已编译过的图不会自动失效。

运行：
    .\code\tools\run.ps1 code\labs\lab_c_lowering.py
"""
from __future__ import annotations

import torch
import torch._inductor.config as cfg
import torch._inductor.lowering as L
from torch._inductor.lowering import register_lowering
from torch._inductor.utils import fresh_inductor_cache

from labs import common

lib = torch.library.Library("mylab", "DEF")
lib.define("affine_naive(Tensor x, float a, float b) -> Tensor")
lib.define("affine(Tensor x, float a, float b) -> Tensor")


def _impl(x, a, b):
    return x * a + b


for name in ("affine_naive", "affine"):
    torch.library.impl(lib, name, "CPU")(_impl)
    torch.library.impl(lib, name, "Meta")(lambda x, a, b: torch.empty_like(x))


@register_lowering(torch.ops.mylab.affine, type_promotion_kind=None)
def _lower_affine(x, a, b):
    """把自定义算子翻译成已有的 pointwise IR 算子。"""
    return L.add(L.mul(x, a), b)


def model_naive(x):
    return torch.relu(torch.ops.mylab.affine_naive(x, 2.0, 1.0))


def model_lowered(x):
    return torch.relu(torch.ops.mylab.affine(x, 2.0, 1.0))


x = torch.randn(256, 256)
ref = torch.relu(x * 2.0 + 1.0)


def compile_once(fn, label: str) -> tuple[object, str]:
    torch._dynamo.reset()
    with fresh_inductor_cache():
        before = common.modules_snapshot()
        cf = torch.compile(fn, fullgraph=True)
        cf(x)
        mods = common.modules_since(before)
        if not mods:
            raise SystemExit("没有捕获到本次编译生成的模块")
        src = common.read_text_any(mods[-1].__file__)
    print(f"[compile] {label}: {len(mods)} module(s)")
    return cf, src


naive_fn, naive_src = compile_once(model_naive, "fallback")
lowered_fn, lowered_src = compile_once(model_lowered, "lowering")

common.section("1) 未注册 lowering：调用留在 wrapper 里，无法融合")
for line in naive_src.splitlines():
    if "mylab" in line.strip() and not line.strip().startswith("#"):
        print("  " + line.strip())

common.section("2) 注册 lowering：算子展开成 IR，与 relu 同 kernel")
body = common.extract_kernel(lowered_src)
for line in body.splitlines():
    s = line.strip()
    if s.startswith("for(int64_t") or s.startswith("auto tmp") or "clamp_min" in s:
        print("  " + s)

common.section("3) 断言")
common.assert_close("fallback 数值", naive_fn(x), ref)
common.assert_close("lowering 数值", lowered_fn(x), ref)
common.check("fallback 保留自定义算子调用", "torch.ops.mylab.affine_naive" in naive_src)
common.check("lowering 后 wrapper 不再调用自定义算子", "torch.ops.mylab" not in lowered_src)
common.check("lowering 后只有一个 kernel",
             lowered_src.count('extern "C" __declspec(dllexport)') == 1,
             str(lowered_src.count('extern "C" __declspec(dllexport)')))
common.check("affine 的乘法/加法进入 kernel",
             "static_cast<float>(2.0)" in body or "* tmp" in body,
             f"tmp_ops={common.kernel_stats(lowered_src)['tmp_ops']}")

print()
print("结论：lowering 的本质不是『实现算子』，而是把算子翻译成 IR；")
print("      翻译之后，调度器才可能把它和邻居融进同一个 loop nest。")