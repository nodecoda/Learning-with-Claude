"""PyTorch FX frontend.

The frontend is deliberately narrow: it accepts a traced module whose tensor
operations are in the supported set.  Unsupported operations fail loudly rather
than silently producing a wrong graph.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np
import torch
import torch.fx
from torch.fx.passes.shape_prop import ShapeProp

from .ir import Graph, IRError, Value

_SUPPORTED_BINARY = {
    torch.add: "add",
    torch.sub: "sub",
    torch.mul: "mul",
    torch.div: "div",
}
_SUPPORTED_UNARY = {
    torch.relu: "relu",
    torch.nn.functional.relu: "relu",
    torch.exp: "exp",
}


def _shape_of(node: torch.fx.Node) -> tuple[int, ...] | None:
    meta = node.meta.get("tensor_meta")
    if meta is None:
        return None
    shape = getattr(meta, "shape", None)
    return tuple(int(d) for d in shape) if shape is not None else None


def _resolve_attr(traced: torch.fx.GraphModule, target: str) -> Any:
    for getter in (traced.get_parameter, traced.get_buffer):
        try:
            return getter(target)
        except AttributeError:
            pass
    obj: Any = traced
    for part in target.split("."):
        obj = getattr(obj, part)
    return obj


def _as_const(graph: Graph, value: Any, name: str) -> Value:
    if isinstance(value, torch.Tensor):
        arr = value.detach().cpu().numpy().astype(np.float32, copy=False)
    else:
        arr = np.asarray(value, dtype=np.float32)
    return graph.add_const(name, arr)


def _node_value(graph: Graph, env: dict[torch.fx.Node, Value], arg: Any, node: torch.fx.Node, idx: int) -> Value | None:
    if isinstance(arg, torch.fx.Node):
        if arg not in env:
            raise IRError(f"node {arg.name} is used before it is available")
        return env[arg]
    if isinstance(arg, (int, float, bool)) and not isinstance(arg, bool):
        return _as_const(graph, float(arg), f"scalar_{node.name}_{idx}")
    if isinstance(arg, bool):
        return _as_const(graph, float(arg), f"scalar_{node.name}_{idx}")
    if arg is None:
        return None
    raise IRError(f"unsupported argument {arg!r} in node {node.name}")


class _InlineTracer(torch.fx.Tracer):
    def is_leaf_module(self, m, qualname):
        return False


def from_torch(
    model: torch.nn.Module,
    example_inputs: Sequence[torch.Tensor],
    *,
    input_names: Sequence[str] | None = None,
) -> Graph:
    """Trace a module and lower it to the mini IR.

    The module is traced with ``torch.fx.symbolic_trace`` and shapes are
    propagated with the input examples.  Only the supported operator subset is
    accepted.
    """
    model = model.eval()
    tracer = _InlineTracer()
    traced = torch.fx.GraphModule(model, tracer.trace(model))
    ShapeProp(traced).propagate(*example_inputs)
    graph = Graph()
    env: dict[torch.fx.Node, Value] = {}
    names = list(input_names or [f"x{i}" for i in range(len(example_inputs))])
    placeholder_index = 0

    def read_args(node: torch.fx.Node) -> list[Value]:
        vals: list[Value] = []
        for i, arg in enumerate(node.args):
            v = _node_value(graph, env, arg, node, i)
            if v is not None:
                vals.append(v)
        return vals

    for node in traced.graph.nodes:
        if node.op == "placeholder":
            shape = _shape_of(node) or tuple(example_inputs[placeholder_index].shape)
            env[node] = graph.add_input(names[placeholder_index], shape)
            placeholder_index += 1
            continue
        if node.op == "get_attr":
            raw = _resolve_attr(traced, str(node.target))
            env[node] = _as_const(graph, raw, str(node.target).replace(".", "_"))
            continue
        if node.op == "call_function":
            target = node.target
            if target in _SUPPORTED_BINARY:
                vals = read_args(node)
                if len(vals) != 2:
                    raise IRError(f"{node.name}: binary op needs 2 tensor args, got {len(vals)}")
                kind = _SUPPORTED_BINARY[target]
                env[node] = graph.add_op(kind, vals, shape=_shape_of(node), name=node.name)
                continue
            if target in _SUPPORTED_UNARY:
                vals = read_args(node)
                if len(vals) != 1:
                    raise IRError(f"{node.name}: unary op needs 1 tensor arg, got {len(vals)}")
                env[node] = graph.add_op(_SUPPORTED_UNARY[target], vals, shape=_shape_of(node), name=node.name)
                continue
            if target is torch.matmul:
                vals = read_args(node)
                if len(vals) != 2:
                    raise IRError(f"{node.name}: matmul needs 2 tensor args")
                env[node] = graph.add_op("matmul", vals, shape=_shape_of(node), name=node.name)
                continue
            if target is torch.sum:
                vals = read_args(node)
                if len(vals) != 1:
                    raise IRError(f"{node.name}: sum needs 1 tensor arg")
                dim = node.args[1] if len(node.args) > 1 else node.kwargs.get("dim", None)
                keepdim = bool(node.kwargs.get("keepdim", False))
                if isinstance(dim, (list, tuple)):
                    if len(dim) != 1:
                        raise IRError(f"{node.name}: only a single sum axis is supported")
                    dim = int(dim[0])
                if dim is not None:
                    dim = int(dim)
                env[node] = graph.add_op(
                    "sum",
                    vals,
                    {"axis": dim, "keepdims": keepdim},
                    shape=_shape_of(node),
                    name=node.name,
                )
                continue
            if target is torch.softmax:
                vals = read_args(node)
                if len(vals) != 1:
                    raise IRError(f"{node.name}: softmax needs 1 tensor arg")
                dim = int(node.args[1]) if len(node.args) > 1 else int(node.kwargs.get("dim", -1))
                env[node] = graph.add_op(
                    "softmax", vals, {"axis": dim}, shape=_shape_of(node), name=node.name
                )
                continue
            target_name = getattr(target, "__name__", str(target))
            if target_name == "linear":
                vals = read_args(node)
                if len(vals) not in {2, 3}:
                    raise IRError(f"{node.name}: linear expects x, weight, [bias]")
                if not vals[1].is_constant or vals[1].data is None:
                    raise IRError(f"{node.name}: only constant linear weights are supported")
                w_t = graph.add_const(
                    f"{node.name}_weight_t", vals[1].data.T.copy()
                )
                out = graph.add_op("matmul", [vals[0], w_t], name=f"{node.name}_matmul")
                if len(vals) == 3:
                    out = graph.add_op("add", [out, vals[2]], name=node.name)
                env[node] = out
                continue
            raise IRError(f"unsupported call_function target in {node.name}: {target}")

        if node.op == "output":
            out_arg = node.args[0]
            if isinstance(out_arg, torch.fx.Node):
                graph.add_output(env[out_arg], out_arg.name)
            elif isinstance(out_arg, (tuple, list)):
                for i, item in enumerate(out_arg):
                    if not isinstance(item, torch.fx.Node):
                        raise IRError(f"unsupported output item: {item!r}")
                    graph.add_output(env[item], item.name if len(out_arg) == 1 else f"out{i}")
            else:
                raise IRError(f"unsupported output form: {out_arg!r}")
            continue

        raise IRError(f"unsupported FX node op: {node.op} ({node.name})")

    if not graph.outputs:
        raise IRError("traced graph has no outputs")
    graph.validate()
    return graph