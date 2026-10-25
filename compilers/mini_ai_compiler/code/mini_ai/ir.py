"""Core IR for the mini AI compiler.

The IR is intentionally small: static shapes, float32 tensors, and a flat list
of operators in topological order.  It is large enough to demonstrate real
compiler problems (fusion, liveness, code generation) but small enough to read
in one sitting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np


class IRError(ValueError):
    """Raised when a graph or operator is malformed."""


@dataclass(frozen=True)
class TensorType:
    shape: tuple[int, ...]
    dtype: str = "float32"

    def __post_init__(self) -> None:
        if self.dtype != "float32":
            raise IRError(f"only float32 is supported, got {self.dtype}")
        if any(not isinstance(d, int) or d <= 0 for d in self.shape):
            raise IRError(f"invalid shape: {self.shape}")

    @property
    def numel(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n

    @property
    def rank(self) -> int:
        return len(self.shape)

    @property
    def strides(self) -> tuple[int, ...]:
        """Row-major (C-order) strides in elements."""
        out: list[int] = []
        acc = 1
        for d in reversed(self.shape):
            out.append(acc)
            acc *= d
        return tuple(reversed(out))

    def __str__(self) -> str:
        dims = ", ".join(str(d) for d in self.shape)
        return f"tensor<{dims}>" if self.shape else "tensor<scalar>"


@dataclass(eq=False)
class Value:
    name: str
    type: TensorType
    kind: str = "temp"  # input | constant | temp | output
    data: Optional[np.ndarray] = None
    producer: Optional["Op"] = None
    users: list["Op"] = field(default_factory=list)
    id: int = -1

    @property
    def is_constant(self) -> bool:
        return self.kind == "constant"

    def __repr__(self) -> str:
        return f"Value({self.name}, {self.type}, {self.kind})"


@dataclass(eq=False)
class Op:
    kind: str
    inputs: list[Value]
    outputs: list[Value]
    attrs: dict[str, Any] = field(default_factory=dict)
    id: int = -1

    @property
    def output(self) -> Value:
        if len(self.outputs) != 1:
            raise IRError(f"op {self.kind} has {len(self.outputs)} outputs")
        return self.outputs[0]

    def __repr__(self) -> str:
        return f"Op({self.kind}, in={[v.name for v in self.inputs]}, out={[v.name for v in self.outputs]})"


def broadcast_shape(*shapes: tuple[int, ...]) -> tuple[int, ...]:
    """Numpy-style broadcasting for static shapes."""
    if not shapes:
        return ()
    rank = max(len(s) for s in shapes)
    padded = [(1,) * (rank - len(s)) + s for s in shapes]
    out: list[int] = []
    for dims in zip(*padded):
        non_one = {d for d in dims if d != 1}
        if len(non_one) > 1:
            raise IRError(f"shapes are not broadcastable: {shapes}")
        out.append(non_one.pop() if non_one else 1)
    return tuple(out)


def broadcast_strides(in_shape: tuple[int, ...], out_shape: tuple[int, ...]) -> tuple[int, ...]:
    """Strides (in elements) for reading an input into an output-shaped loop.

    A stride of zero means the input dimension is broadcast.
    """
    if len(in_shape) > len(out_shape):
        raise IRError(f"cannot broadcast rank {len(in_shape)} to rank {len(out_shape)}")
    in_strides = TensorType(in_shape).strides
    padded_shape = (1,) * (len(out_shape) - len(in_shape)) + in_shape
    padded_strides = (0,) * (len(out_shape) - len(in_shape)) + in_strides
    out: list[int] = []
    for d, s, out_d in zip(padded_shape, padded_strides, out_shape):
        if d == out_d:
            out.append(s)
        elif d == 1:
            out.append(0)
        else:
            raise IRError(f"cannot broadcast {in_shape} to {out_shape}")
    return tuple(out)


def infer_output_type(kind: str, inputs: list[TensorType], attrs: dict[str, Any]) -> TensorType:
    """Static shape/dtype inference for the supported operator set."""
    if kind in {"add", "sub", "mul", "div", "add_relu"}:
        if len(inputs) != 2:
            raise IRError(f"{kind} expects 2 inputs, got {len(inputs)}")
        return TensorType(broadcast_shape(inputs[0].shape, inputs[1].shape))
    if kind in {"relu", "exp"}:
        if len(inputs) != 1:
            raise IRError(f"{kind} expects 1 input, got {len(inputs)}")
        return inputs[0]
    if kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
        if len(inputs) not in {2, 3}:
            raise IRError(f"{kind} expects 2 or 3 inputs, got {len(inputs)}")
        a, b = inputs[0].shape, inputs[1].shape
        if len(a) != 2 or len(b) != 2:
            raise IRError(f"{kind} currently supports 2-D matrices, got {a} and {b}")
        if a[1] != b[0]:
            raise IRError(f"matmul inner dimensions differ: {a} @ {b}")
        out_shape = (a[0], b[1])
        if len(inputs) == 3:
            # Bias must broadcast to the output shape.
            broadcast_shape(inputs[2].shape, out_shape)
        return TensorType(out_shape)
    if kind == "sum":
        if len(inputs) != 1:
            raise IRError("sum expects 1 input")
        shape = inputs[0].shape
        axis = attrs.get("axis", None)
        keepdims = bool(attrs.get("keepdims", False))
        if axis is None:
            return TensorType((1,) * len(shape) if keepdims else ())
        if not isinstance(axis, int):
            raise IRError(f"sum axis must be int or None, got {axis!r}")
        if axis < 0:
            axis += len(shape)
        if not (0 <= axis < len(shape)):
            raise IRError(f"sum axis {axis} out of range for {shape}")
        if keepdims:
            return TensorType(tuple(1 if i == axis else d for i, d in enumerate(shape)))
        return TensorType(tuple(d for i, d in enumerate(shape) if i != axis))
    if kind == "softmax":
        if len(inputs) != 1:
            raise IRError("softmax expects 1 input")
        return inputs[0]
    raise IRError(f"unsupported op: {kind}")


class Graph:
    """A flat, topologically ordered graph."""

    def __init__(self) -> None:
        self.values: list[Value] = []
        self.ops: list[Op] = []
        self.inputs: list[Value] = []
        self.outputs: list[Value] = []
        self._next_value_id = 0
        self._next_op_id = 0

    def _new_value(self, name: str, type_: TensorType, kind: str = "temp", data=None) -> Value:
        v = Value(name=name, type=type_, kind=kind, data=data, id=self._next_value_id)
        self._next_value_id += 1
        self.values.append(v)
        return v

    def add_input(self, name: str, shape: Iterable[int]) -> Value:
        v = self._new_value(name, TensorType(tuple(shape)), kind="input")
        self.inputs.append(v)
        return v

    def add_const(self, name: str, data: np.ndarray | float | int) -> Value:
        arr = np.asarray(data, dtype=np.float32)
        if arr.ndim > 4:
            raise IRError(f"constants with rank > 4 are not supported: {arr.shape}")
        v = self._new_value(name, TensorType(tuple(arr.shape)), kind="constant", data=arr.copy())
        return v

    def add_op(
        self,
        kind: str,
        inputs: list[Value],
        attrs: Optional[dict[str, Any]] = None,
        *,
        name: Optional[str] = None,
        shape: Optional[Iterable[int]] = None,
        dtype: str = "float32",
    ) -> Value:
        attrs = dict(attrs or {})
        types = [v.type for v in inputs]
        inferred = infer_output_type(kind, types, attrs)
        explicit = TensorType(tuple(shape), dtype) if shape is not None else inferred
        if explicit.dtype != "float32":
            raise IRError("only float32 is supported")
        if explicit.shape != inferred.shape:
            raise IRError(
                f"shape mismatch for {kind}: inferred {inferred.shape}, explicit {explicit.shape}"
            )
        out_name = name or f"{kind}_{self._next_op_id}"
        out = self._new_value(out_name, explicit, kind="temp")
        op = Op(kind=kind, inputs=list(inputs), outputs=[out], attrs=attrs, id=self._next_op_id)
        self._next_op_id += 1
        out.producer = op
        for v in inputs:
            v.users.append(op)
        self.ops.append(op)
        return out

    def add_output(self, value: Value, name: Optional[str] = None) -> None:
        if value not in self.values:
            raise IRError("output value does not belong to this graph")
        if name is not None:
            value.name = name
        if value.kind != "constant":
            value.kind = "output"
        if value not in self.outputs:
            self.outputs.append(value)

    def validate(self) -> None:
        """Check SSA, ownership, topological order, and output existence."""
        value_ids = {id(v) for v in self.values}
        seen: set[int] = set()
        op_index: dict[int, int] = {}
        for i, op in enumerate(self.ops):
            op_index[id(op)] = i
            for v in op.inputs:
                if id(v) not in value_ids:
                    raise IRError(f"op {op.kind} uses a value outside the graph: {v}")
                if v.kind not in {"input", "constant"}:
                    if v.producer is None or id(v.producer) not in op_index:
                        raise IRError(f"op {op.kind} uses {v.name} before it is defined")
            for v in op.outputs:
                if id(v) in seen:
                    raise IRError(f"value {v.name} is defined more than once")
                seen.add(id(v))
                if v.producer is not op:
                    raise IRError(f"value {v.name} has a stale producer")
        if not self.outputs:
            raise IRError("graph has no outputs")
        for v in self.outputs:
            if id(v) not in value_ids:
                raise IRError(f"graph output {v} does not belong to the graph")

    def recompute_users(self) -> None:
        for v in self.values:
            v.users = []
        for op in self.ops:
            for v in op.inputs:
                v.users.append(op)

    def replace_all_uses(self, old: Value, new: Value) -> None:
        if old is new:
            return
        for op in self.ops:
            op.inputs = [new if v is old else v for v in op.inputs]
        self.outputs = [new if v is old else v for v in self.outputs]
        self.recompute_users()

    def remove_ops(self, ops: Iterable[Op]) -> None:
        doomed = {id(op) for op in ops}
        self.ops = [op for op in self.ops if id(op) not in doomed]
        self.recompute_users()

    def dce(self) -> int:
        """Remove operators whose results are neither outputs nor used."""
        removed = 0
        while True:
            self.recompute_users()
            live: set[int] = {id(v) for v in self.outputs}
            keep: list[Op] = []
            for op in reversed(self.ops):
                needed = any(id(v) in live or v.users for v in op.outputs)
                if needed:
                    keep.append(op)
                    for v in op.inputs:
                        live.add(id(v))
                else:
                    removed += 1
            keep.reverse()
            if len(keep) == len(self.ops):
                break
            self.ops = keep
        self.prune_dead_values()
        self.recompute_users()
        return removed

    def prune_dead_values(self) -> None:
        live = {id(v) for v in self.inputs}
        live.update(id(v) for v in self.outputs)
        for op in self.ops:
            live.update(id(v) for v in op.inputs)
            live.update(id(v) for v in op.outputs)
        self.values = [v for v in self.values if id(v) in live]

    def clone(self) -> "Graph":
        g = Graph()
        mapping: dict[int, Value] = {}
        for v in self.inputs:
            mapping[id(v)] = g.add_input(v.name, v.type.shape)
        for v in self.values:
            if v.kind == "constant":
                assert v.data is not None
                mapping[id(v)] = g.add_const(v.name, v.data)
        for op in self.ops:
            ins = [mapping[id(v)] for v in op.inputs]
            out = g.add_op(op.kind, ins, dict(op.attrs), name=op.output.name, shape=op.output.type.shape)
            mapping[id(op.output)] = out
        for v in self.outputs:
            g.add_output(mapping[id(v)], v.name)
        return g

    def topo_names(self) -> list[str]:
        return [op.output.name for op in self.ops]

    def dump(self) -> str:
        lines: list[str] = []
        lines.append("inputs:")
        for v in self.inputs:
            lines.append(f"  {v.name}: {v.type}")
        consts = [v for v in self.values if v.kind == "constant"]
        if consts:
            lines.append("constants:")
            for v in consts:
                lines.append(f"  {v.name}: {v.type} numel={v.type.numel}")
        lines.append("ops:")
        for i, op in enumerate(self.ops):
            ins = ", ".join(v.name for v in op.inputs)
            attrs = f" {op.attrs}" if op.attrs else ""
            lines.append(f"  {i:02d} %{op.output.name} = {op.kind}({ins}){attrs} : {op.output.type}")
        lines.append("outputs:")
        for v in self.outputs:
            lines.append(f"  {v.name}: {v.type}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Graph(inputs={len(self.inputs)}, ops={len(self.ops)}, outputs={len(self.outputs)})"


def constant_scalar(name: str, value: float) -> Value:
    """Convenience helper used by tests and examples."""
    return Value(name=name, type=TensorType(()), kind="constant", data=np.asarray(value, dtype=np.float32))