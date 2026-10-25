"""Reference interpreter for the mini IR.

The interpreter is the semantic oracle.  Every backend is tested against it,
and constant folding uses the same operator implementations.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np

from .ir import Graph, IRError, Op, Value


def _as_float32(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float32)


def apply_op(kind: str, arrays: list[np.ndarray], attrs: dict) -> np.ndarray:
    """Execute one operator on NumPy arrays."""
    a = [_as_float32(x) for x in arrays]
    if kind == "add":
        return _as_float32(a[0] + a[1])
    if kind == "sub":
        return _as_float32(a[0] - a[1])
    if kind == "mul":
        return _as_float32(a[0] * a[1])
    if kind == "div":
        return _as_float32(a[0] / a[1])
    if kind == "relu":
        return _as_float32(np.maximum(a[0], np.float32(0.0)))
    if kind == "exp":
        return _as_float32(np.exp(a[0]))
    if kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
        if a[0].ndim != 2 or a[1].ndim != 2:
            raise IRError(f"{kind} expects 2-D inputs, got {a[0].shape} and {a[1].shape}")
        out = _as_float32(a[0] @ a[1])
        if kind in {"matmul_bias", "matmul_bias_relu"}:
            out = _as_float32(out + a[2])
        if kind == "matmul_bias_relu":
            out = _as_float32(np.maximum(out, np.float32(0.0)))
        return out
    if kind == "add_relu":
        return _as_float32(np.maximum(a[0] + a[1], np.float32(0.0)))
    if kind == "sum":
        axis = attrs.get("axis", None)
        keepdims = bool(attrs.get("keepdims", False))
        return _as_float32(np.sum(a[0], axis=axis, keepdims=keepdims))
    if kind == "softmax":
        axis = int(attrs.get("axis", -1))
        x = a[0]
        shifted = x - np.max(x, axis=axis, keepdims=True)
        e = np.exp(shifted)
        return _as_float32(e / np.sum(e, axis=axis, keepdims=True))
    raise IRError(f"interpreter does not support op: {kind}")


def evaluate_op(op: Op, env: Mapping[Value, np.ndarray]) -> np.ndarray:
    return apply_op(op.kind, [env[v] for v in op.inputs], op.attrs)


def evaluate_graph(
    graph: Graph,
    feed: Mapping[str, np.ndarray],
    *,
    check_shapes: bool = True,
) -> list[np.ndarray]:
    """Run a graph and return arrays in graph.outputs order.

    `feed` is keyed by input *name*.  The function does not mutate the graph.
    """
    graph.validate()
    env: dict[Value, np.ndarray] = {}
    for v in graph.values:
        if v.kind == "constant":
            assert v.data is not None
            env[v] = _as_float32(v.data)
    for v in graph.inputs:
        if v.name not in feed:
            raise KeyError(f"missing input {v.name!r}; got {sorted(feed)}")
        arr = _as_float32(feed[v.name])
        if check_shapes and arr.shape != v.type.shape:
            raise ValueError(f"input {v.name}: expected {v.type.shape}, got {arr.shape}")
        env[v] = arr
    for op in graph.ops:
        out = evaluate_op(op, env)
        expected = op.output.type.shape
        if check_shapes and out.shape != expected:
            raise ValueError(
                f"op {op.kind} produced {out.shape}, IR says {expected}"
            )
        env[op.output] = out
    return [env[v] for v in graph.outputs]


def evaluate_named(graph: Graph, feed: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {v.name: a for v, a in zip(graph.outputs, evaluate_graph(graph, feed))}


def max_abs_error(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch: {a.shape} vs {b.shape}")
    return float(np.max(np.abs(_as_float32(a) - _as_float32(b)))) if a.size else 0.0