from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mini_ai import evaluate_graph, from_torch, standard_pipeline
from mini_ai.codegen_c import compile_graph_c, generate_c
from mini_ai.codegen_llvm import LLVMJIT
from mini_ai.interpreter import max_abs_error
from mini_ai.ir import Graph, IRError, broadcast_shape
from mini_ai.memory import plan_memory
from mini_ai.passes import (
    algebraic_simplify,
    common_subexpression_elimination,
    constant_fold,
    fuse_ops,
)
from mini_ai.runtime import run_c_graph


class TestIR(unittest.TestCase):
    def test_broadcast_shape(self) -> None:
        self.assertEqual(broadcast_shape((4, 1), (1, 3)), (4, 3))
        self.assertEqual(broadcast_shape((), (2, 3)), (2, 3))
        with self.assertRaises(IRError):
            broadcast_shape((2, 3), (2, 4))

    def test_validate_rejects_wrong_shape(self) -> None:
        g = Graph()
        x = g.add_input("x", (2, 3))
        with self.assertRaises(IRError):
            g.add_op("add", [x, g.add_const("b", np.zeros((4,), np.float32))])

    def test_interpreter_broadcast_and_sum(self) -> None:
        g = Graph()
        x = g.add_input("x", (2, 3))
        b = g.add_const("b", np.array([1, 2, 3], np.float32))
        y = g.add_op("add", [x, b])
        r = g.add_op("relu", [y])
        s = g.add_op("sum", [r], {"axis": 1})
        g.add_output(s)
        out = evaluate_graph(g, {"x": np.array([[-1, 0, 1], [2, -3, 4]], np.float32)})[0]
        np.testing.assert_allclose(out, np.array([6.0, 10.0], np.float32))


class TestFrontendAndPasses(unittest.TestCase):
    def make_graph(self):
        torch.manual_seed(0)
        model = torch.nn.Sequential(
            torch.nn.Linear(4, 8), torch.nn.ReLU(), torch.nn.Linear(8, 2)
        ).eval()
        x = torch.randn(3, 4)
        return model, x, from_torch(model, (x,))

    def test_frontend_matches_torch(self) -> None:
        model, x, g = self.make_graph()
        out = evaluate_graph(g, {"x0": x.numpy()})[0]
        expected = model(x).detach().numpy()
        self.assertLess(max_abs_error(out, expected), 1e-5)

    def test_constant_fold_and_dce(self) -> None:
        g = Graph()
        x = g.add_input("x", (2,))
        c1 = g.add_const("c1", np.array([2.0, 2.0], np.float32))
        c2 = g.add_const("c2", np.array([3.0, 3.0], np.float32))
        y = g.add_op("add", [c1, c2])
        z = g.add_op("mul", [x, y])
        g.add_output(z)
        folded = constant_fold(g)
        self.assertEqual(folded, 1)
        self.assertEqual([op.kind for op in g.ops], ["mul"])

    def test_cse_and_algebraic(self) -> None:
        g = Graph()
        x = g.add_input("x", (2,))
        a = g.add_op("relu", [x])
        b = g.add_op("relu", [x])
        c = g.add_op("add", [a, b])
        g.add_output(c)
        self.assertEqual(common_subexpression_elimination(g), 1)
        self.assertEqual(len(g.ops), 2)

        g2 = Graph()
        x2 = g2.add_input("x", (2,))
        one = g2.add_const("one", np.array([1.0, 1.0], np.float32))
        y = g2.add_op("mul", [x2, one])
        g2.add_output(y)
        self.assertEqual(algebraic_simplify(g2), 1)
        self.assertEqual(len(g2.ops), 0)
        self.assertIs(g2.outputs[0], x2)

    def test_fusion(self) -> None:
        _, _, g = self.make_graph()
        before = len(g.ops)
        changed = fuse_ops(g)
        self.assertEqual(changed, 2)
        self.assertEqual([op.kind for op in g.ops], ["matmul_bias_relu", "matmul_bias"])
        self.assertLess(len(g.ops), before)


class TestMemory(unittest.TestCase):
    def test_reuse_dead_buffer(self) -> None:
        g = Graph()
        x = g.add_input("x", (4,))
        y = g.add_input("y", (4,))
        z = g.add_input("z", (4,))
        a = g.add_op("relu", [x])
        b = g.add_op("relu", [y])
        c = g.add_op("add", [a, b])
        d = g.add_op("relu", [z])
        out = g.add_op("add", [c, d])
        g.add_output(out)
        plan = plan_memory(g)
        self.assertEqual(plan.offset_of(a), plan.offset_of(d))
        plan.validate()

    def test_no_alias_for_live_values(self) -> None:
        g = Graph()
        x = g.add_input("x", (8,))
        a = g.add_op("relu", [x])
        b = g.add_op("relu", [a])
        g.add_output(b)
        plan = plan_memory(g)
        self.assertNotEqual(plan.offset_of(a), plan.offset_of(b))


class TestBackends(unittest.TestCase):
    def make_graph(self):
        g = Graph()
        x = g.add_input("x", (2, 3))
        b = g.add_const("b", np.array([1, 2, 3], np.float32))
        y = g.add_op("add", [x, b])
        r = g.add_op("relu", [y])
        s = g.add_op("sum", [r], {"axis": 1})
        g.add_output(s)
        return g, {"x": np.array([[-1, 0, 1], [2, -3, 4]], np.float32)}

    def test_c_backend(self) -> None:
        g, feed = self.make_graph()
        ref = evaluate_graph(g, feed)[0]
        d = Path(".scratch/mini-ai-tests") / "c_backend"
        result = compile_graph_c(g, d)
        out = run_c_graph(g, feed, result.dll_path)[0]
        np.testing.assert_allclose(out, ref, atol=1e-6, rtol=1e-6)
        self.assertIn("mini_forward", generate_c(g))

    def test_llvm_backend(self) -> None:
        g, feed = self.make_graph()
        ref = evaluate_graph(g, feed)[0]
        jit = LLVMJIT(g)
        out = jit.run(feed)[0]
        np.testing.assert_allclose(out, ref, atol=1e-6, rtol=1e-6)

    def test_end_to_end_mlp(self) -> None:
        torch.manual_seed(11)
        model = torch.nn.Sequential(
            torch.nn.Linear(8, 16), torch.nn.ReLU(), torch.nn.Linear(16, 4)
        ).eval()
        x = torch.randn(4, 8)
        g = from_torch(model, (x,))
        standard_pipeline().run(g)
        expected = model(x).detach().numpy()
        c_out = None
        d = Path(".scratch/mini-ai-tests") / "e2e_mlp"
        result = compile_graph_c(g, d)
        c_out = run_c_graph(g, {"x0": x.numpy()}, result.dll_path)[0]
        jit = LLVMJIT(g)
        llvm_out = jit.run({"x0": x.numpy()})[0]
        self.assertLess(max_abs_error(c_out, expected), 1e-5)
        self.assertLess(max_abs_error(llvm_out, expected), 1e-5)


if __name__ == "__main__":
    unittest.main()
