"""A tiny, readable AI compiler used by the course."""

from .ir import Graph, Op, TensorType, Value
from .interpreter import evaluate_graph, evaluate_named, max_abs_error
from .frontend import from_torch
from .passes import PassManager, standard_pipeline
from .memory import MemoryPlan, plan_memory
from .codegen_c import compile_graph_c, generate_c
from .codegen_llvm import LLVMJIT, jit_run, llvm_ir
from .runtime import assert_close, benchmark, load_c_function, run_c_graph

__all__ = [
    "Graph",
    "Op",
    "TensorType",
    "Value",
    "evaluate_graph",
    "evaluate_named",
    "max_abs_error",
    "from_torch",
    "PassManager",
    "standard_pipeline",
    "MemoryPlan",
    "plan_memory",
    "generate_c",
    "compile_graph_c",
    "LLVMJIT",
    "jit_run",
    "llvm_ir",
    "benchmark",
    "assert_close",
    "load_c_function",
    "run_c_graph",
]