"""Small, stage-by-stage demos for the course chapters."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mini_ai import evaluate_graph, standard_pipeline
from mini_ai.codegen_c import compile_graph_c, generate_c
from mini_ai.codegen_llvm import LLVMJIT, llvm_ir
from mini_ai.ir import Graph
from mini_ai.memory import plan_memory
from mini_ai.runtime import run_c_graph


def toy_graph() -> tuple[Graph, dict[str, np.ndarray]]:
    g = Graph()
    x = g.add_input("x", (2, 3))
    b = g.add_const("bias", np.array([1.0, 2.0, 3.0], np.float32))
    y = g.add_op("add", [x, b])
    r = g.add_op("relu", [y])
    s = g.add_op("sum", [r], {"axis": 1})
    g.add_output(s)
    return g, {"x": np.array([[-1, 0, 1], [2, -3, 4]], np.float32)}


def show_ir() -> None:
    g, _ = toy_graph()
    print(g.dump())


def show_passes() -> None:
    g, feed = toy_graph()
    print("before:")
    print(g.dump())
    standard_pipeline().run(g, verbose=True)
    print("after:")
    print(g.dump())
    print("result:", evaluate_graph(g, feed)[0])


def show_memory() -> None:
    g, _ = toy_graph()
    print(plan_memory(g).summary())


def show_c() -> None:
    g, feed = toy_graph()
    print(generate_c(g))
    result = compile_graph_c(g, ".scratch/mini-ai-stages/c", name="toy")
    print("dll:", result.dll_path)
    print("result:", run_c_graph(g, feed, result.dll_path)[0])


def show_llvm() -> None:
    g, feed = toy_graph()
    text = llvm_ir(g)
    lines = text.splitlines()
    print("\n".join(lines[:40]))
    print(f"... ({len(lines)} lines total)")
    jit = LLVMJIT(g)
    print("result:", jit.run(feed)[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["ir", "passes", "memory", "c", "llvm"])
    args = ap.parse_args()
    {"ir": show_ir, "passes": show_passes, "memory": show_memory, "c": show_c, "llvm": show_llvm}[args.stage]()


if __name__ == "__main__":
    main()