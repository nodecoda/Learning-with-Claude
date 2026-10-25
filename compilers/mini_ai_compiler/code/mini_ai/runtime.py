"""Runtime helpers for compiled kernels."""

from __future__ import annotations

import ctypes
import statistics
import time
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .ir import Graph


def _f32(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float32)


def load_c_function(dll_path: str | Path, n_inputs: int, n_outputs: int, *, symbol: str = "mini_forward"):
    lib = ctypes.CDLL(str(Path(dll_path)))
    fn = getattr(lib, symbol)
    ptr = ctypes.POINTER(ctypes.c_float)
    fn.argtypes = [ptr] * (n_inputs + n_outputs)
    fn.restype = ctypes.c_int

    def run(inputs: Sequence[np.ndarray], output_shapes: Sequence[tuple[int, ...]]):
        if len(inputs) != n_inputs:
            raise ValueError(f"expected {n_inputs} inputs, got {len(inputs)}")
        ins = [_f32(a) for a in inputs]
        outs = [np.zeros(s, dtype=np.float32) for s in output_shapes]
        args = [a.ctypes.data_as(ptr) for a in ins]
        args += [a.ctypes.data_as(ptr) for a in outs]
        rc = fn(*args)
        if rc != 0:
            raise RuntimeError(f"{symbol} returned error code {rc}")
        return outs

    run.lib = lib  # keep the shared library alive
    return run


def run_c_graph(graph: Graph, feed: dict[str, np.ndarray], dll_path: str | Path) -> list[np.ndarray]:
    inputs = [feed[v.name] for v in graph.inputs]
    shapes = [v.type.shape for v in graph.outputs]
    fn = load_c_function(dll_path, len(inputs), len(shapes))
    return fn(inputs, shapes)


def benchmark(fn: Callable[[], object], *, warmup: int = 5, repeat: int = 30) -> dict[str, float]:
    """Return median/min/max milliseconds.

    Median is the headline number.  The min is useful for noise diagnosis, but
    should not be used to claim a speedup on its own.
    """
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "repeat": float(repeat),
    }


def assert_close(actual: np.ndarray, expected: np.ndarray, *, atol: float = 1e-5, rtol: float = 1e-5) -> float:
    a = _f32(actual)
    b = _f32(expected)
    if a.shape != b.shape:
        raise AssertionError(f"shape mismatch: {a.shape} vs {b.shape}")
    err = float(np.max(np.abs(a - b))) if a.size else 0.0
    if not np.allclose(a, b, atol=atol, rtol=rtol):
        raise AssertionError(f"values differ: max_abs_error={err:.6g}, atol={atol}, rtol={rtol}")
    return err