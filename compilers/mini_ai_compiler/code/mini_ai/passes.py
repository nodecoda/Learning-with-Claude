"""Graph passes and a small pass manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .interpreter import apply_op
from .ir import Graph, IRError, Op, Value


def constant_fold(graph: Graph, *, max_numel: int = 4096) -> int:
    """Replace operators whose inputs are all constants with constants."""
    folded = 0
    for op in list(graph.ops):
        if not op.inputs or not all(v.kind == "constant" for v in op.inputs):
            continue
        if op.output.type.numel > max_numel:
            continue
        arrays = [v.data for v in op.inputs]
        assert all(a is not None for a in arrays)
        value = apply_op(op.kind, arrays, op.attrs)
        const = graph.add_const(f"{op.output.name}_folded", value)
        graph.replace_all_uses(op.output, const)
        graph.remove_ops([op])
        folded += 1
    graph.dce()
    return folded


def common_subexpression_elimination(graph: Graph) -> int:
    """Remove duplicate pure operators with identical inputs and attrs."""
    seen: dict[tuple, Value] = {}
    removed: list[Op] = []
    for op in graph.ops:
        key = (
            op.kind,
            tuple(id(v) for v in op.inputs),
            repr(sorted(op.attrs.items())),
            op.output.type.shape,
        )
        if key in seen:
            graph.replace_all_uses(op.output, seen[key])
            removed.append(op)
        else:
            seen[key] = op.output
    graph.remove_ops(removed)
    graph.dce()
    return len(removed)


def _const_is(v: Value, value: float) -> bool:
    return v.kind == "constant" and v.data is not None and bool(np.all(v.data == np.float32(value)))


def algebraic_simplify(graph: Graph) -> int:
    """Apply a small, deliberately conservative set of algebraic identities."""
    changed = 0
    for op in list(graph.ops):
        out = op.output
        if op.kind in {"add", "sub"} and len(op.inputs) == 2:
            a, b = op.inputs
            if _const_is(b, 0.0):
                graph.replace_all_uses(out, a)
                graph.remove_ops([op])
                changed += 1
                continue
            if op.kind == "add" and _const_is(a, 0.0):
                graph.replace_all_uses(out, b)
                graph.remove_ops([op])
                changed += 1
                continue
        if op.kind == "mul" and len(op.inputs) == 2:
            a, b = op.inputs
            if _const_is(b, 1.0):
                graph.replace_all_uses(out, a)
                graph.remove_ops([op])
                changed += 1
                continue
            if _const_is(a, 1.0):
                graph.replace_all_uses(out, b)
                graph.remove_ops([op])
                changed += 1
                continue
            if _const_is(a, 0.0) or _const_is(b, 0.0):
                zero = graph.add_const(f"{out.name}_zero", np.zeros(out.type.shape, dtype=np.float32))
                graph.replace_all_uses(out, zero)
                graph.remove_ops([op])
                changed += 1
                continue
        if op.kind == "div" and len(op.inputs) == 2 and _const_is(op.inputs[1], 1.0):
            graph.replace_all_uses(out, op.inputs[0])
            graph.remove_ops([op])
            changed += 1
            continue
        if op.kind == "relu" and len(op.inputs) == 1:
            inner = op.inputs[0]
            if inner.producer is not None and inner.producer.kind == "relu":
                graph.replace_all_uses(out, inner)
                graph.remove_ops([op])
                changed += 1
                continue
    graph.dce()
    return changed


def _new_fused_op(graph: Graph, kind: str, inputs: list[Value], out_type, name: str) -> Op:
    out = graph._new_value(name, out_type, kind="temp")
    op = Op(kind=kind, inputs=inputs, outputs=[out], attrs={}, id=graph._next_op_id)
    graph._next_op_id += 1
    out.producer = op
    return op


def fuse_ops(graph: Graph) -> int:
    """Fuse common producer chains.

    Supported patterns:
      matmul + add + relu -> matmul_bias_relu
      matmul + add        -> matmul_bias
      add + relu          -> add_relu
    """
    graph.recompute_users()
    users = {id(v): list(v.users) for v in graph.values}
    consumed: set[int] = set()
    replacements: dict[int, Value] = {}
    insert_at: dict[int, Op] = {}
    count = 0

    # Highest priority: matmul -> add -> relu.
    for relu in graph.ops:
        if relu.kind != "relu" or id(relu) in consumed:
            continue
        add = relu.inputs[0].producer
        if add is None or add.kind != "add" or id(add) in consumed:
            continue
        matmul_candidates = [(i, v) for i, v in enumerate(add.inputs) if v.producer is not None and v.producer.kind == "matmul"]
        if len(matmul_candidates) != 1:
            continue
        mm_idx, mm_out = matmul_candidates[0]
        mm = mm_out.producer
        assert mm is not None
        if len(users[id(mm_out)]) != 1 or len(users[id(add.output)]) != 1:
            continue
        if len(users[id(relu.output)]) == 0 and relu.output not in graph.outputs:
            continue
        bias = add.inputs[1 - mm_idx]
        fused = _new_fused_op(
            graph,
            "matmul_bias_relu",
            [mm.inputs[0], mm.inputs[1], bias],
            relu.output.type,
            f"{relu.output.name}_fused",
        )
        replacements[id(relu.output)] = fused.output
        consumed.update({id(mm), id(add), id(relu)})
        insert_at[id(relu)] = fused
        count += 1

    # matmul -> add, when the add was not already folded into a relu.
    for add in graph.ops:
        if add.kind != "add" or id(add) in consumed:
            continue
        matmul_candidates = [(i, v) for i, v in enumerate(add.inputs) if v.producer is not None and v.producer.kind == "matmul"]
        if len(matmul_candidates) != 1:
            continue
        mm_idx, mm_out = matmul_candidates[0]
        mm = mm_out.producer
        assert mm is not None
        if id(mm) in consumed:
            continue
        if len(users[id(mm_out)]) != 1:
            continue
        # Leave the add alone if a relu consumes it; the first pattern may still
        # be rejected because the matmul has other users.
        if any(u.kind == "relu" for u in users[id(add.output)]):
            continue
        bias = add.inputs[1 - mm_idx]
        fused = _new_fused_op(
            graph,
            "matmul_bias",
            [mm.inputs[0], mm.inputs[1], bias],
            add.output.type,
            f"{add.output.name}_fused",
        )
        replacements[id(add.output)] = fused.output
        consumed.update({id(mm), id(add)})
        insert_at[id(add)] = fused
        count += 1

    # add -> relu for non-matmul chains.
    for relu in graph.ops:
        if relu.kind != "relu" or id(relu) in consumed:
            continue
        add = relu.inputs[0].producer
        if add is None or add.kind != "add" or id(add) in consumed:
            continue
        if any(v.producer is not None and v.producer.kind == "matmul" for v in add.inputs):
            continue
        if len(users[id(add.output)]) != 1:
            continue
        fused = _new_fused_op(
            graph,
            "add_relu",
            [add.inputs[0], add.inputs[1]],
            relu.output.type,
            f"{relu.output.name}_fused",
        )
        replacements[id(relu.output)] = fused.output
        consumed.update({id(add), id(relu)})
        insert_at[id(relu)] = fused
        count += 1

    if not consumed:
        return 0

    for op in graph.ops:
        if id(op) in consumed:
            continue
        op.inputs = [replacements.get(id(v), v) for v in op.inputs]
    for fused in insert_at.values():
        fused.inputs = [replacements.get(id(v), v) for v in fused.inputs]
    graph.outputs = [replacements.get(id(v), v) for v in graph.outputs]
    new_ops: list[Op] = []
    for op in graph.ops:
        if id(op) in consumed:
            if id(op) in insert_at:
                new_ops.append(insert_at[id(op)])
        else:
            new_ops.append(op)
    graph.ops = new_ops
    graph.recompute_users()
    graph.dce()
    return count


@dataclass
class PassRecord:
    name: str
    changed: int


class PassManager:
    def __init__(self) -> None:
        self.passes: list[tuple[str, Callable[..., int], dict]] = []

    def add(self, name: str, fn: Callable[..., int], **kwargs) -> "PassManager":
        self.passes.append((name, fn, kwargs))
        return self

    def run(self, graph: Graph, *, verbose: bool = False) -> list[PassRecord]:
        records: list[PassRecord] = []
        for name, fn, kwargs in self.passes:
            before = len(graph.ops)
            changed = int(fn(graph, **kwargs))
            graph.validate()
            records.append(PassRecord(name, changed))
            if verbose:
                print(f"{name:32s} ops {before:3d} -> {len(graph.ops):3d}  changed={changed}")
        return records


def standard_pipeline() -> PassManager:
    pm = PassManager()
    pm.add("constant_fold", constant_fold)
    pm.add("algebraic_simplify", algebraic_simplify)
    pm.add("cse", common_subexpression_elimination)
    pm.add("fuse_ops", fuse_ops)
    pm.add("dce", lambda g, **_: g.dce())
    return pm
