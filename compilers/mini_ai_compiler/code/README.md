# mini_ai reference implementation

This directory contains the runnable implementation used by the course.

```text
mini_ai/
  ir.py            graph IR, types, shape inference, validation
  frontend.py      PyTorch FX -> mini IR
  interpreter.py   NumPy reference interpreter
  passes.py        constant folding, DCE, CSE, algebraic, fusion
  memory.py        liveness + arena memory planner
  codegen_c.py     C code generation + MSVC DLL build
  codegen_llvm.py  LLVM IR generation + llvmlite JIT
  runtime.py       ctypes loading and benchmark helpers
examples/
  demo_e2e.py      end-to-end MLP demo
  demo_stages.py   ir / passes / memory / c / llvm stage demos
  autotune_matmul.py
tests/
  test_mini_ai.py
```

Run from the repository root:

```powershell
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& ".scratch\aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\demo_e2e.py"
& ".scratch\aic-env\Scripts\python.exe" -m unittest discover -s "compilers\mini_ai_compiler\code\tests" -v
```

The C backend finds MSVC `vcvars64.bat` automatically. Set `VCVARS64` if your installation is elsewhere.