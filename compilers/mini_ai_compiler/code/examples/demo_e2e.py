"""End-to-end demo used by the course and the final project."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mini_ai import evaluate_graph, from_torch, standard_pipeline
from mini_ai.codegen_c import compile_graph_c, generate_c
from mini_ai.codegen_llvm import LLVMJIT, llvm_ir
from mini_ai.memory import plan_memory
from mini_ai.runtime import benchmark, run_c_graph


class MLP(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.fc1 = torch.nn.Linear(8, 16)
        self.fc2 = torch.nn.Linear(16, 4)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(torch.relu(self.fc1(x)))


def main() -> None:
    torch.manual_seed(7)
    model = MLP().eval()
    x = torch.randn(4, 8, dtype=torch.float32)
    with torch.no_grad():
        expected = model(x).numpy()

    graph = from_torch(model, (x,))
    print("=== raw graph ===")
    print(graph.dump())
    print("=== pass pipeline ===")
    standard_pipeline().run(graph, verbose=True)
    print("=== optimized graph ===")
    print(graph.dump())
    plan = plan_memory(graph)
    print("=== memory plan ===")
    print(plan.summary())

    build = Path(".scratch/mini-ai-demo")
    result = compile_graph_c(graph, build, name="mlp")
    print("=== C ===")
    print("source:", result.code_path)
    print("dll:", result.dll_path)
    c_out = run_c_graph(graph, {"x0": x.numpy()}, result.dll_path)[0]
    print("max_abs_error:", float(np.max(np.abs(c_out - expected))))

    print("=== LLVM ===")
    ir_text = llvm_ir(graph)
    print("ir_lines:", len(ir_text.splitlines()))
    jit = LLVMJIT(graph)
    llvm_out = jit.run({"x0": x.numpy()})[0]
    print("max_abs_error:", float(np.max(np.abs(llvm_out - expected))))

    print("=== benchmark ===")
    c_stats = benchmark(lambda: run_c_graph(graph, {"x0": x.numpy()}, result.dll_path), warmup=5, repeat=30)
    llvm_stats = benchmark(lambda: jit.run({"x0": x.numpy()}), warmup=5, repeat=30)
    torch_stats = benchmark(lambda: model(x), warmup=5, repeat=30)
    print("C median ms:", round(c_stats["median_ms"], 4))
    print("LLVM median ms:", round(llvm_stats["median_ms"], 4))
    print("PyTorch median ms:", round(torch_stats["median_ms"], 4))


if __name__ == "__main__":
    main()