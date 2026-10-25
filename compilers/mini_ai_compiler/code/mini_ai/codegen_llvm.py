"""LLVM backend built on llvmlite.

The backend is deliberately simple: static shapes are fully unrolled, so the
generated IR has no loop PHI nodes.  The C backend is the place where loops and
tiling are taught; this backend isolates LLVM IR construction and JIT ABI.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np
from llvmlite import binding, ir

from .ir import Graph, IRError, Op, TensorType, Value, broadcast_strides
from .memory import MemoryPlan, plan_memory

_initialized = False


def _init_binding() -> None:
    global _initialized
    if _initialized:
        return
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    _initialized = True


def _flat(shape: tuple[int, ...], idx: tuple[int, ...]) -> int:
    acc = 0
    for d, i in zip(shape, idx):
        acc = acc * d + i
    return acc


def _broadcast_flat(in_shape: tuple[int, ...], out_shape: tuple[int, ...], idx: tuple[int, ...]) -> int:
    strides = broadcast_strides(in_shape, out_shape)
    return sum(i * s for i, s in zip(idx, strides))


class LLVMCodegen:
    def __init__(self, graph: Graph, plan: MemoryPlan | None = None, *, function_name: str = "mini_forward_llvm") -> None:
        self.graph = graph
        self.plan = plan or plan_memory(graph)
        self.function_name = function_name
        self.float_ty = ir.FloatType()
        self.i32 = ir.IntType(32)
        self.module = ir.Module(name="mini_ai")
        try:
            self.module.triple = binding.get_default_triple()
        except Exception:
            pass
        self.input_index = {id(v): i for i, v in enumerate(graph.inputs)}
        self.output_index = {id(v): i for i, v in enumerate(graph.outputs)}
        self.const_globals: dict[int, ir.GlobalVariable] = {}
        self.func: ir.Function | None = None
        self.builder: ir.IRBuilder | None = None
        self.ws_arg: ir.Argument | None = None
        self.expf: ir.Function | None = None

    def build(self) -> ir.Module:
        self.graph.validate()
        for v in self.graph.values:
            if v.kind == "constant":
                assert v.data is not None
                flat = np.asarray(v.data, dtype=np.float32).reshape(-1)
                if flat.size == 0:
                    flat = np.zeros(1, dtype=np.float32)
                arr_ty = ir.ArrayType(self.float_ty, len(flat))
                gv = ir.GlobalVariable(self.module, arr_ty, name=f"C_{v.id}")
                gv.linkage = "internal"
                gv.global_constant = True
                gv.initializer = ir.Constant(arr_ty, [float(x) for x in flat])
                self.const_globals[id(v)] = gv
        n_args = len(self.graph.inputs) + len(self.graph.outputs) + 1
        fnty = ir.FunctionType(ir.VoidType(), [ir.PointerType(self.float_ty)] * n_args)
        self.func = ir.Function(self.module, fnty, name=self.function_name)
        entry = self.func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry)
        self.ws_arg = self.func.args[-1]
        for op in self.graph.ops:
            self._emit_op(op)
        self._copy_outputs()
        self.builder.ret_void()
        return self.module

    def _c(self, n: int) -> ir.Constant:
        return ir.Constant(self.i32, int(n))

    def _ptr(self, v: Value) -> ir.Value:
        assert self.builder is not None and self.func is not None and self.ws_arg is not None
        if id(v) in self.input_index:
            return self.func.args[self.input_index[id(v)]]
        if v.kind == "constant":
            gv = self.const_globals[id(v)]
            return self.builder.gep(gv, [self._c(0), self._c(0)], inbounds=True)
        if v in self.plan.allocations:
            off = self.plan.offset_of(v)
            if off % 4 != 0:
                raise IRError(f"{v.name}: byte offset {off} is not divisible by 4")
            return self.builder.gep(self.ws_arg, [self._c(off // 4)], inbounds=True)
        raise IRError(f"value {v.name} has no storage")

    def _load(self, v: Value, idx: int) -> ir.Value:
        assert self.builder is not None
        return self.builder.load(self.builder.gep(self._ptr(v), [self._c(idx)], inbounds=True))

    def _store(self, v: Value, idx: int, value: ir.Value) -> None:
        assert self.builder is not None
        self.builder.store(value, self.builder.gep(self._ptr(v), [self._c(idx)], inbounds=True))

    def _zero(self) -> ir.Constant:
        return ir.Constant(self.float_ty, 0.0)

    def _relu(self, x: ir.Value) -> ir.Value:
        assert self.builder is not None
        cond = self.builder.fcmp_ordered(">", x, self._zero())
        return self.builder.select(cond, x, self._zero())

    def _emit_op(self, op: Op) -> None:
        if op.kind in {"add", "sub", "mul", "div", "relu", "exp", "add_relu"}:
            self._emit_elementwise(op)
            return
        if op.kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
            self._emit_matmul(op)
            return
        if op.kind == "sum":
            self._emit_sum(op)
            return
        raise IRError(f"LLVM backend does not support op: {op.kind}")

    def _emit_elementwise(self, op: Op) -> None:
        assert self.builder is not None
        out_shape = op.output.type.shape
        for idx in np.ndindex(out_shape):
            idx = tuple(int(i) for i in idx)
            out_idx = _flat(out_shape, idx)
            ins_idx = [_broadcast_flat(v.type.shape, out_shape, idx) for v in op.inputs]
            vals = [self._load(v, i) for v, i in zip(op.inputs, ins_idx)]
            k = op.kind
            if k == "add":
                value = self.builder.fadd(vals[0], vals[1])
            elif k == "sub":
                value = self.builder.fsub(vals[0], vals[1])
            elif k == "mul":
                value = self.builder.fmul(vals[0], vals[1])
            elif k == "div":
                value = self.builder.fdiv(vals[0], vals[1])
            elif k == "relu":
                value = self._relu(vals[0])
            elif k == "add_relu":
                value = self._relu(self.builder.fadd(vals[0], vals[1]))
            elif k == "exp":
                if self.expf is None:
                    fnty = ir.FunctionType(self.float_ty, [self.float_ty])
                    self.expf = ir.Function(self.module, fnty, name="expf")
                value = self.builder.call(self.expf, [vals[0]])
            else:
                raise IRError(f"unsupported elementwise op: {k}")
            self._store(op.output, out_idx, value)

    def _emit_matmul(self, op: Op) -> None:
        assert self.builder is not None
        a, b = op.inputs[0], op.inputs[1]
        m, k = a.type.shape
        k2, n = b.type.shape
        if k != k2:
            raise IRError(f"matmul shape mismatch: {a.type.shape} @ {b.type.shape}")
        bias = op.inputs[2] if len(op.inputs) == 3 else None
        for i in range(m):
            for j in range(n):
                acc = self._zero()
                for kk in range(k):
                    av = self._load(a, i * k + kk)
                    bv = self._load(b, kk * n + j)
                    acc = self.builder.fadd(acc, self.builder.fmul(av, bv))
                if bias is not None:
                    bias_idx = _broadcast_flat(bias.type.shape, (m, n), (i, j))
                    acc = self.builder.fadd(acc, self._load(bias, bias_idx))
                if op.kind == "matmul_bias_relu":
                    acc = self._relu(acc)
                self._store(op.output, i * n + j, acc)

    def _emit_sum(self, op: Op) -> None:
        assert self.builder is not None
        x = op.inputs[0]
        out = op.output
        axis = op.attrs.get("axis", None)
        keepdims = bool(op.attrs.get("keepdims", False))
        for idx in np.ndindex(out.type.shape):
            self._store(out, _flat(out.type.shape, idx), self._zero())
        if axis is None:
            for idx in np.ndindex(x.type.shape):
                idx = tuple(int(i) for i in idx)
                self._store(out, 0, self.builder.fadd(self._load(out, 0), self._load(x, _flat(x.type.shape, idx))))
            return
        if axis < 0:
            axis += len(x.type.shape)
        for idx in np.ndindex(x.type.shape):
            idx = tuple(int(i) for i in idx)
            if keepdims:
                out_idx = _flat(out.type.shape, tuple(0 if d == axis else i for d, i in enumerate(idx)))
            else:
                out_idx = _flat(out.type.shape, tuple(i for d, i in enumerate(idx) if d != axis))
            self._store(out, out_idx, self.builder.fadd(self._load(out, out_idx), self._load(x, _flat(x.type.shape, idx))))

    def _copy_outputs(self) -> None:
        assert self.builder is not None and self.func is not None
        for i, v in enumerate(self.graph.outputs):
            dst = self.func.args[len(self.graph.inputs) + i]
            for idx in range(max(1, v.type.numel)):
                self.builder.store(self._load(v, idx), self.builder.gep(dst, [self._c(idx)], inbounds=True))


def build_llvm_module(graph: Graph, plan: MemoryPlan | None = None):
    plan = plan or plan_memory(graph)
    return LLVMCodegen(graph, plan).build(), plan


def llvm_ir(graph: Graph, plan: MemoryPlan | None = None) -> str:
    module, _ = build_llvm_module(graph, plan)
    return str(module)


class LLVMJIT:
    """A reusable JIT engine.  Compilation happens once in __init__."""

    def __init__(self, graph: Graph, *, plan: MemoryPlan | None = None) -> None:
        _init_binding()
        module, self.plan = build_llvm_module(graph, plan)
        backing = binding.parse_assembly(str(module))
        backing.verify()
        target = binding.Target.from_default_triple()
        self.tm = target.create_target_machine()
        self.engine = binding.create_mcjit_compiler(backing, self.tm)
        self.engine.finalize_object()
        self.engine.run_static_constructors()
        addr = self.engine.get_function_address("mini_forward_llvm")
        if not addr:
            raise RuntimeError("LLVM JIT did not find mini_forward_llvm")
        self.graph = graph
        self.ptr_ty = ctypes.POINTER(ctypes.c_float)
        self.cfn = ctypes.CFUNCTYPE(
            None,
            *([self.ptr_ty] * (len(graph.inputs) + len(graph.outputs) + 1)),
        )(addr)

    def run(self, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        inputs = [np.ascontiguousarray(feed[v.name], dtype=np.float32) for v in self.graph.inputs]
        outputs = [np.zeros(v.type.shape, dtype=np.float32) for v in self.graph.outputs]
        ws = np.zeros(max(1, self.plan.arena_bytes // 4), dtype=np.float32)
        args = [a.ctypes.data_as(self.ptr_ty) for a in inputs]
        args += [a.ctypes.data_as(self.ptr_ty) for a in outputs]
        args.append(ws.ctypes.data_as(self.ptr_ty))
        self.cfn(*args)
        return outputs


def jit_run(graph: Graph, feed: dict[str, np.ndarray], *, plan: MemoryPlan | None = None) -> list[np.ndarray]:
    return LLVMJIT(graph, plan=plan).run(feed)
