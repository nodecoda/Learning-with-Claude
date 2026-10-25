"""课程实验共用工具（Windows + torch 2.14.1+cpu）。

放在 labs/common.py 的目的：把「找生成物」「计时」「断言」这些重复动作收敛到一处，
章节正文和练习脚本引用同一套函数，测出来的数字才可比较。
"""
from __future__ import annotations

import os
import re
import shutil
import statistics
import time
from pathlib import Path
from typing import Callable, Sequence


# ---------------------------------------------------------------- 环境检查

def require_msvc() -> None:
    """Inductor 在 Windows 上要调用 cl.exe 编译 C++ kernel。"""
    if os.name != "nt":
        return
    if shutil.which("cl") is None:
        raise SystemExit(
            "找不到 cl.exe。请用 code/tools/run.ps1 运行脚本，"
            "或先手动执行 vcvars64.bat。"
        )


def cache_dir() -> Path:
    raw = os.environ.get("TORCHINDUCTOR_CACHE_DIR")
    if not raw:
        raise SystemExit(
            "TORCHINDUCTOR_CACHE_DIR 未设置。请用 code/tools/run.ps1 运行脚本。"
        )
    return Path(raw)


# ---------------------------------------------------------------- 生成物定位

def newest_debug_dir() -> Path:
    """返回最近一次编译的 *.debug 目录（cfg.trace.enabled = True 时生成）。"""
    dirs = sorted(
        (p for p in cache_dir().rglob("*.debug") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    if not dirs:
        raise SystemExit(
            "没有找到 *.debug 目录。请先设置 torch._inductor.config.trace.enabled = True。"
        )
    return dirs[-1]


def newest_output_code() -> Path:
    """最近一次编译的 output_code.py。"""
    d = newest_debug_dir()
    p = d / "output_code.py"
    if not p.exists():
        raise SystemExit(f"{d} 里没有 output_code.py")
    return p


def modules_snapshot() -> int:
    """记录 PyCodeCache 当前模块数量，用于区分「本次编译新增的模块」。"""
    from torch._inductor.codecache import PyCodeCache

    mods = PyCodeCache.modules
    return len(mods) if isinstance(mods, list) else len(list(mods.values()))


def modules_since(before: int) -> list:
    from torch._inductor.codecache import PyCodeCache

    mods = PyCodeCache.modules
    items = mods if isinstance(mods, list) else list(mods.values())
    return [m for m in items[before:] if getattr(m, "__file__", None)]


# ---------------------------------------------------------------- 代码解析

_KERNEL_RE = re.compile(r'extern "C" __declspec\(dllexport\) void\s+(\w+)')


def extract_kernel(src: str, index: int = 0) -> str:
    """从 output_code.py 里截出第 index 个 C++ kernel 的函数体。"""
    starts = [m.start() for m in re.finditer(r'extern "C" __declspec\(dllexport\)', src)]
    if not starts:
        return ""
    begin = starts[min(index, len(starts) - 1)]
    end = src.find("// Python bindings", begin)
    if end < 0:
        end = len(src)
    return src[begin:end]


def kernel_stats(src: str, index: int = 0) -> dict[str, object]:
    """提取课程里反复用到的结构特征，而不是只看耗时。"""
    body = extract_kernel(src, index)
    name = _KERNEL_RE.search(body)
    step = re.search(r"x0\+=static_cast<int64_t>\((\d+)LL\)", body)
    omp = re.search(r"#pragma omp parallel num_threads\((\d+)\)", body)
    return {
        "name": name.group(1) if name else "?",
        "vectorized": "Vectorized<float>" in body,
        "step": int(step.group(1)) if step else None,
        "threads": int(omp.group(1)) if omp else None,
        "tmp_ops": len(re.findall(r"auto tmp\d+ =", body)),
        "uses_sin": "std::sin" in body,
        "uses_abs": "abs(" in body,
        "cpp_lines": len(body.splitlines()),
    }


def wrapper_extern_calls(src: str) -> list[str]:
    """output_code.py 里调用的 extern kernel（如 addmm/mm/bmm）。"""
    return sorted(set(re.findall(r"extern_kernels\.(\w+)\(", src)))


# ---------------------------------------------------------------- 计时

def bench_ms(fn: Callable[[], object], iters: int = 30, warmup: int = 8) -> float:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples) * 1e3


def interleaved_ms(
    variants: dict[str, Callable[[], object]],
    rounds: int = 7,
    iters: int = 20,
) -> dict[str, dict[str, float]]:
    """交错测量：同一轮里每个变体各跑一次，降低 CPU 频率漂移带来的偏差。"""
    for fn in variants.values():
        for _ in range(5):
            fn()
    samples: dict[str, list[float]] = {k: [] for k in variants}
    for _ in range(rounds):
        for name, fn in variants.items():
            t0 = time.perf_counter()
            for _ in range(iters):
                fn()
            samples[name].append((time.perf_counter() - t0) / iters * 1e3)
    return {
        name: {"median": statistics.median(v), "min": min(v)}
        for name, v in samples.items()
    }


# ---------------------------------------------------------------- 文件读取

def read_text_any(path) -> str:
    """生成的中间文件可能带本机默认编码（中文 Windows 上是 cp936），读取时兜底。"""
    data = Path(path).read_bytes()
    for enc in ("utf-8", "cp936", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 输出与断言

def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def snippet(text: str, lines: int = 30) -> str:
    return "\n".join(text.splitlines()[:lines])


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        raise SystemExit(f"实验断言失败：{name} {detail}")


def assert_close(name: str, got, ref, tol: float = 1e-5) -> float:
    err = (got - ref).abs().max().item()
    check(name, err <= tol, f"max_abs_err={err:.3e} tol={tol:g}")
    return err


def table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))