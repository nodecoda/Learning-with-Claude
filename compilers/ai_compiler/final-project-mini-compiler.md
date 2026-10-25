# Final Project：实现一个 mini AI 编译器（FX → 自有 IR → C → DLL）

> **目标**：把抓图、IR、常量折叠、算子融合、调度、代码生成与数值对齐串成一条真实链路：输入 `nn.Module`，生成 C，用 `cl` 编成 DLL，用 `ctypes` 调用，并给出与 PyTorch eager 的 `maxerr`。文中代码、命令和输出均为 **实测**。

## 目标

- 用 FX 抓图并降到自定义 DAG IR，实现常量折叠、DCE 与 elementwise 融合，并说清融合边界。
- 做调度：elementwise 一个扁平循环，matmul 三重循环 + tiling；生成 C、编成 DLL、用 `ctypes` 调用。
- 用 `maxerr` / 相对误差验收，并让不支持的 op 与 C 编译错误带真实信息失败。

## 需求规格

| 算子 | 形式 | 约束 | 输出 |
|---|---|---|---|
| `matmul` | `x @ w` | 2-D，`(M,K) @ (K,N)` | `(M,N)` |
| `add` / `mul` | `x + b`、`x * s` | rank ≤ 2，支持广播 | 广播后形状 |
| `relu` | `torch.relu(x)` | 2-D | 同输入 |
| `sum` | `x.sum()` | 只做全归约，不支持 `dim` | 标量 `()` |

- **IR**：`Graph{values, ops, inputs, outputs}`；`Value(id, shape, role, const, cname)`，`Op(name, inputs, out, attrs)`，边是 value id，节点按拓扑序。
- **图优化**：常量折叠 + elementwise 融合，另做 DCE 清理折叠后的孤点。
- **调度**：`ewfused` 一个扁平循环；matmul 三重循环 + 8/16 tiling；sum 单循环归约。
- **后端与验收**：生成带 `__declspec(dllexport)` 的 C，`cl /LD /O2 /W3` 编 DLL；用 eager vs DLL 的 `maxerr`（大张量加相对误差）验收。
- **失败**：不支持的 op 抛 `UnsupportedOpError`；`cl` 非零抛 `BuildError`，保留原始输出。

边界：float32、静态 shape、单输出、权重内嵌、单线程、无 SIMD/BLAS；`sum` 结果只作为输出。扩展方向见文末。

## 架构分层

```text
nn.Module
   │ fx.symbolic_trace
FX Graph
   │ trace_module：target 映射、get_attr 常量化、shape 推导
自有 IR：Graph{Value, Op}
   │ fold_constants → fuse_elementwise
优化后的 IR
   │ schedule → emit_c
kernel.c
   │ cl /LD /O2 /W3
kernel.dll
   │ ctypes：inputs 在前，outputs 在后
float32 输出 + maxerr
```

四层职责：**前端**只翻译，遇到未知 target 立即失败；**中端**只改图，每个 pass 保持拓扑序和 shape；**后端**只认调度结果，分配 C 名、常量和临时 buffer；**runtime** 只认 ABI，全部参数是 `float*`。

两个取舍：FX 图带 Python target/meta，不适合做 C 名分配和调度标注，所以另设 IR；权重内嵌为 `static const float[]`，DLL 只收真实输入，代价是权重变则重编。

## 分阶段实现

6 个 milestone 的代码按顺序拼成 `compiler.py` 和 `run_demo.py`；代码块为排版去掉了空行，逻辑与仓库文件一致。

### M1：FX 抓图与自有 IR

`trace_module` 给 placeholder 填 shape，把 `get_attr` 变常量，对 call 节点做 target 映射和 shape 推导。`sum` 只接受全归约，传 `dim` 会明确报错。

```python
"""Mini AI compiler: PyTorch FX -> DAG IR -> C -> DLL (float32, 2-D tensors)."""
from __future__ import annotations
import ctypes
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any
import numpy as np
import torch
import torch.fx as fx
SUPPORTED = ("matmul", "add", "mul", "relu", "sum")
ELEMENTWISE = ("add", "mul", "relu")
VCVARS64 = r"D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
class CompileError(RuntimeError):
    pass
class UnsupportedOpError(CompileError):
    pass
class BuildError(CompileError):
    pass
@dataclass
class Value:
    id: str
    shape: tuple[int, ...]
    role: str = "temp"  # input | const | temp | output
    const: np.ndarray | None = None
    cname: str = ""
    @property
    def numel(self) -> int:
        return int(np.prod(self.shape)) if self.shape else 1
@dataclass
class Op:
    name: str
    inputs: list[str]
    out: str
    attrs: dict[str, Any] = field(default_factory=dict)
@dataclass
class Graph:
    values: dict[str, Value]
    ops: list[Op]
    inputs: list[str]
    outputs: list[str]
def _base(node: fx.Node) -> str:
    if node.op == "call_method":
        return str(node.target)
    return getattr(node.target, "__name__", str(node.target))
def _attr(gm: fx.GraphModule, target: str) -> np.ndarray:
    obj: Any = gm
    for part in target.split("."):
        obj = getattr(obj, part)
    if isinstance(obj, torch.Tensor):
        obj = obj.detach().cpu().numpy()
    return np.array(obj, dtype=np.float32, order="C", copy=True)
def _bcast(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    if len(a) > 2 or len(b) > 2:
        raise UnsupportedOpError(f"rank > 2: {a} vs {b}")
    rank = max(len(a), len(b))
    out = []
    for k in range(rank):
        da = a[len(a) - rank + k] if len(a) - rank + k >= 0 else 1
        db = b[len(b) - rank + k] if len(b) - rank + k >= 0 else 1
        if da != db and da != 1 and db != 1:
            raise CompileError(f"cannot broadcast {a} with {b}")
        out.append(db if da == 1 else da)
    return tuple(out)
def _infer(name: str, shapes: list[tuple[int, ...]], node: fx.Node) -> tuple[int, ...]:
    if name == "matmul":
        a, b = shapes
        if len(a) != 2 or len(b) != 2:
            raise UnsupportedOpError(f"matmul needs 2-D operands: {a} @ {b}")
        if a[1] != b[0]:
            raise CompileError(f"matmul inner dims mismatch: {a} @ {b}")
        return (a[0], b[1])
    if name == "relu":
        return shapes[0]
    if name in ("add", "mul"):
        return _bcast(shapes[0], shapes[1])
    if name == "sum":
        return ()
    raise UnsupportedOpError(f"unknown op {name} at {node.name}")
def trace_module(module: torch.nn.Module, example_inputs: list[torch.Tensor]) -> Graph:
    gm = fx.symbolic_trace(module)
    g = Graph({}, [], [], [])
    env: dict[fx.Node, str] = {}
    n_in = n_c = n_v = 0
    for node in gm.graph.nodes:
        if node.op == "placeholder":
            shape = tuple(int(d) for d in example_inputs[n_in].shape)
            if len(shape) != 2:
                raise UnsupportedOpError(f"input must be 2-D, got {shape}")
            g.values[f"in{n_in}"] = Value(f"in{n_in}", shape, "input")
            g.inputs.append(f"in{n_in}")
            env[node] = f"in{n_in}"
            n_in += 1
        elif node.op == "get_attr":
            arr = _attr(gm, node.target)
            g.values[f"c{n_c}"] = Value(f"c{n_c}", tuple(arr.shape), "const", arr)
            env[node] = f"c{n_c}"
            n_c += 1
        elif node.op in ("call_function", "call_method"):
            base = _base(node)
            if base not in SUPPORTED:
                raise UnsupportedOpError(
                    f"unsupported op {base!r} at node {node.name!r}; supported: {', '.join(SUPPORTED)}")
            args = list(node.args)
            if base == "sum":
                if len(args) > 1 or node.kwargs.get("dim") is not None:
                    raise UnsupportedOpError(f"sum with dim at {node.name!r}; only full reduction")
                args = args[:1]
            ins = []
            for a in args:
                if isinstance(a, fx.Node):
                    ins.append(env[a])
                elif isinstance(a, (int, float, np.integer, np.floating)) and not isinstance(a, bool):
                    g.values[f"c{n_c}"] = Value(f"c{n_c}", (), "const", np.asarray(float(a), np.float32))
                    ins.append(f"c{n_c}")
                    n_c += 1
                else:
                    raise UnsupportedOpError(f"argument {a!r} at {node.name!r}")
            shape = _infer(base, [g.values[i].shape for i in ins], node)
            g.values[f"v{n_v}"] = Value(f"v{n_v}", shape)
            g.ops.append(Op(base, ins, f"v{n_v}"))
            env[node] = f"v{n_v}"
            n_v += 1
        elif node.op == "output":
            res = node.args[0]
            if isinstance(res, (tuple, list)):
                if len(res) != 1:
                    raise UnsupportedOpError("only one graph output is supported")
                res = res[0]
            g.values[env[res]].role = "output"
            g.outputs.append(env[res])
    return g
```

实测 IR（`DemoNet`，seed 2026；整理成 op/形状表，共 7 个 op）：

```text
IR values=13 ops=7
  matmul   [(2, 3), (3, 4)] -> (2, 4)
  add      [(2, 4), (4,)] -> (2, 4)
  mul      [(2, 4), ()] -> (2, 4)
  relu     [(2, 4)] -> (2, 4)
  mul      [(), ()] -> ()
  add      [(2, 4), ()] -> (2, 4)
  sum      [(2, 4)] -> ()
```

`scale`、`shift` 是 0-D 常量，shape 保持 `()`；`b` 是 `(4,)`，在 `add` 里按最后一维广播。IR 里没有 Python 对象，只有形状和 value id。

### M2：常量折叠与 DCE

所有输入都是 `const` 的 op 直接用 numpy 算掉，结果写回输出 value 并删除 op；随后从 `outputs` 反向做 liveness，删掉不可达节点和 value。

```python
def _eval(op: Op, g: Graph) -> np.ndarray:
    args = [g.values[i].const for i in op.inputs]
    if op.name == "matmul":
        return np.matmul(*args).astype(np.float32)
    if op.name == "add":
        return np.add(*args).astype(np.float32)
    if op.name == "mul":
        return np.multiply(*args).astype(np.float32)
    if op.name == "relu":
        return np.maximum(args[0], 0).astype(np.float32)
    return np.asarray(args[0].sum(), dtype=np.float32)
def fold_constants(g: Graph) -> int:
    kept, n = [], 0
    for op in g.ops:
        if all(g.values[i].const is not None for i in op.inputs):
            v = g.values[op.out]
            v.const = np.array(_eval(op, g), dtype=np.float32, order="C", copy=True)
            v.shape = tuple(v.const.shape)
            if op.out not in g.outputs:
                v.role = "const"
            n += 1
        else:
            kept.append(op)
    g.ops = kept
    live = set(g.outputs) | set(g.inputs)
    for op in reversed(g.ops):
        if op.out in live:
            live.update(op.inputs)
    g.ops = [op for op in g.ops if op.out in live]
    g.values = {k: v for k, v in g.values.items() if k in live}
    return n
```

实测 `shift * 3.0` 被折叠：`folded=1`，op 从 7 个降到 6 个，后面的融合少一个节点。

### M3：elementwise 融合

条件是：生产者是 `add/mul/relu`、只有一个消费者、消费者也是 elementwise、两者输出 shape 相同、生产者不是图输出。满足就内联成表达式树，不满足就保留独立 kernel。

```python
def fuse_elementwise(g: Graph) -> int:
    users: dict[str, list[Op]] = {vid: [] for vid in g.values}
    for op in g.ops:
        for vid in op.inputs:
            users[vid].append(op)
    prod = {op.out: op for op in g.ops}
    outs = set(g.outputs)
    def inline(vid: str) -> bool:
        op = prod.get(vid)
        if op is None or op.name not in ELEMENTWISE or vid in outs or len(users[vid]) != 1:
            return False
        consumer = users[vid][0]
        return consumer.name in ELEMENTWISE and g.values[consumer.out].shape == g.values[vid].shape
    def expr(vid: str) -> tuple:
        v = g.values[vid]
        if v.const is not None and v.numel == 1:
            return ("const", float(v.const.reshape(-1)[0]))
        return op_expr(prod[vid]) if inline(vid) else ("input", vid)
    def op_expr(op: Op) -> tuple:
        if op.name == "relu":
            return ("relu", expr(op.inputs[0]))
        return (op.name, expr(op.inputs[0]), expr(op.inputs[1]))
    dead = {op.out for op in g.ops if op.name in ELEMENTWISE and inline(op.out)}
    ops = []
    for op in g.ops:
        if op.name not in ELEMENTWISE:
            ops.append(op)
        elif op.out not in dead:
            e, ext = op_expr(op), []
            def collect(x: tuple) -> None:
                if x[0] == "input" and x[1] not in ext:
                    ext.append(x[1])
                elif x[0] in ELEMENTWISE:
                    for y in x[1:]:
                        collect(y)
            collect(e)
            ops.append(Op("ewfused", ext, op.out, {"expr": e}))
    g.ops = ops
    return len(dead)
```

实测 `inlined=3`，四个 elementwise 节点合成一个 `ewfused`（整理）：

```text
ops=3: matmul, ewfused, sum
expr=('add', ('relu', ('mul', ('add', ('input', 'v0'), ('input', 'c1')), ('const', 2.0))), ('const', 3.0))
```

`scale` 和折叠后的 `3.0` 直接变成 C 字面量；`b` 保留为外部输入，codegen 时按广播下标读取。matmul 和 sum 没有被卷进融合，这是有意的边界。

### M4：调度与 C 代码生成

调度把 IR 变成 kernel 列表；codegen 分配 C 名、展开常量、按 kernel 顺序生成调用和临时 buffer。matmul 的循环顺序是 `ii → kk → jj → i → k → j`，`T=8` 是 `max(M,N,K) < 16` 的结果；elementwise 只生成一个 `for (int p...)`，二维坐标用 `i = p / D1, j = p % D1` 还原。

```python
def _f32(x: float) -> str:
    x = float(np.float32(x))
    if np.isnan(x):
        return "NAN"
    if np.isinf(x):
        return "INFINITY" if x > 0 else "-INFINITY"
    s = format(x, ".9g")
    return (s if "e" in s or "." in s else s + ".0") + "f"
def _index(v: Value, out_shape: tuple[int, ...]) -> str:
    if v.numel == 1:
        return "0"
    m, n = out_shape
    if len(v.shape) == 2:
        vm, vn = v.shape
        if (vm, vn) == (m, n):
            return f"i*{n}+j"
        if vm == 1 and vn == n:
            return "j"
        if vm == m and vn == 1:
            return "i"
    elif len(v.shape) == 1 and v.shape[0] == n:
        return "j"
    raise CompileError(f"cannot broadcast {v.shape} to {out_shape}")
def _expr(e: tuple, g: Graph, out_shape: tuple[int, ...]) -> str:
    if e[0] == "const":
        return _f32(e[1])
    if e[0] == "input":
        v = g.values[e[1]]
        return f"{v.cname}[{_index(v, out_shape)}]"
    if e[0] == "relu":
        return f"fmaxf({_expr(e[1], g, out_shape)}, 0.0f)"
    left, right = _expr(e[1], g, out_shape), _expr(e[2], g, out_shape)
    return f"({left} {'+' if e[0] == 'add' else '*' } {right})"
@dataclass
class Kernel:
    name: str
    kind: str
    inputs: list[str]
    out: str
    attrs: dict[str, Any] = field(default_factory=dict)
def schedule(g: Graph) -> list[Kernel]:
    ks = []
    for i, op in enumerate(g.ops):
        if op.name == "matmul":
            m, k = g.values[op.inputs[0]].shape
            n = g.values[op.inputs[1]].shape[1]
            tile = 16 if max(m, n, k) >= 16 else 8
            ks.append(Kernel(f"k{i}_matmul", "matmul", op.inputs, op.out, {"tile": tile}))
        elif op.name == "ewfused":
            ks.append(Kernel(f"k{i}_ew", "ew", op.inputs, op.out, dict(op.attrs)))
        else:
            ks.append(Kernel(f"k{i}_sum", "sum", op.inputs, op.out))
    return ks
def assign_names(g: Graph) -> list[str]:
    for i, vid in enumerate(g.inputs):
        g.values[vid].cname = f"in{i}"
    for i, vid in enumerate(g.outputs):
        g.values[vid].cname = f"out{i}"
    n = 0
    for op in g.ops:
        v = g.values[op.out]
        if v.role != "output":
            v.cname = f"v{n}"
            n += 1
    consts = []
    for op in g.ops:
        for vid in op.inputs:
            if g.values[vid].role == "const" and vid not in consts:
                consts.append(vid)
    for i, vid in enumerate(consts):
        g.values[vid].cname = f"c{i}"
    return consts
def emit_c(g: Graph, ks: list[Kernel]) -> str:
    consts = assign_names(g)
    L = ["/* generated by mini compiler */", "#include <math.h>", "#include <stdlib.h>", ""]
    for vid in consts:
        v = g.values[vid]
        L.append(f"static const float {v.cname}[{v.numel}] = "
                 f"{{{', '.join(_f32(x) for x in v.const.reshape(-1))}}};")
    if consts:
        L.append("")
    for k in ks:
        out = g.values[k.out]
        args = ", ".join(f"const float* __restrict {g.values[v].cname}" for v in k.inputs)
        L.append(f"static void {k.name}({args}, float* __restrict {out.cname}) {{")
        if k.kind == "matmul":
            a, b = g.values[k.inputs[0]], g.values[k.inputs[1]]
            m, kk = a.shape
            n, t = b.shape[1], k.attrs["tile"]
            L += [f"  const int M={m}, N={n}, K={kk}, T={t};",
                  f"  for (int i=0;i<M;++i) for (int j=0;j<N;++j) {out.cname}[i*N+j]=0.0f;",
                  "  for (int ii=0;ii<M;ii+=T) for (int kk=0;kk<K;kk+=T) for (int jj=0;jj<N;jj+=T) {",
                  "    int ie=ii+T<M?ii+T:M, ke=kk+T<K?kk+T:K, je=jj+T<N?jj+T:N;",
                  "    for (int i=ii;i<ie;++i) for (int p=kk;p<ke;++p) {",
                  f"      float a={a.cname}[i*K+p];",
                  f"      for (int j=jj;j<je;++j) {out.cname}[i*N+j]+=a*{b.cname}[p*N+j];",
                  "    }", "  }"]
        elif k.kind == "ew":
            shape = tuple(out.shape)
            L += [f"  const int N={out.numel}, D1={shape[1]};",
                  "  for (int p=0;p<N;++p) {", "    int i=p/D1, j=p%D1;",
                  f"    {out.cname}[p]={_expr(k.attrs['expr'], g, shape)};", "  }"]
        else:
            src = g.values[k.inputs[0]]
            L += ["  float acc=0.0f;", f"  for (int p=0;p<{src.numel};++p) acc+={src.cname}[p];",
                  f"  {out.cname}[0]=acc;"]
        L += ["}", ""]
    ins = ", ".join(f"const float* __restrict {g.values[v].cname}" for v in g.inputs)
    outs = ", ".join(f"float* __restrict {g.values[v].cname}" for v in g.outputs)
    L.append(f"__declspec(dllexport) void aic_run({', '.join(x for x in (ins, outs) if x)}) {{")
    temps = [g.values[op.out] for op in g.ops if g.values[op.out].role == "temp"]
    for v in temps:
        L += [f"  float* {v.cname}=(float*)malloc({v.numel}*sizeof(float));",
              f"  if (!{v.cname}) return;"]
    for k in ks:
        L.append(f"  {k.name}({', '.join(g.values[v].cname for v in k.inputs)}, {g.values[k.out].cname});")
    for v in reversed(temps):
        L.append(f"  free({v.cname});")
    L.append("}")
    return "\n".join(L) + "\n"
```

实测调度结果：

```text
k0_matmul: matmul tile=8
k1_ew: ew tile=-
k2_sum: sum tile=-
```

下标不是猜的：`_index` 按输入 shape 生成 `i*N+j`、`j` 或 `i`，把 `(4,)` 的 bias 映射成 `c1[j]`。

### M5：构建 DLL、ctypes 调用与数值对齐

`build_dll` 写 `kernel.c` 并执行 `cmd /c "call vcvars64.bat && cl ..."`；`run_dll` 用 `ctypes.CDLL` 取 `aic_run`，参数全部声明为 `POINTER(c_float)`，数组先 `reshape(-1)` 保证连续。

```python
def build_dll(csrc: str, workdir: str, broken: bool = False) -> tuple[str, str]:
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    cpath, dll = os.path.join(workdir, "kernel.c"), os.path.join(workdir, "kernel.dll")
    if os.path.exists(dll):
        os.remove(dll)
    if broken:
        csrc = csrc.replace("void aic_run(", "int broken = ;\nvoid aic_run(", 1)
    with open(cpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(csrc)
    cmd = f'call "{os.environ.get("AIC_VCVARS", VCVARS64)}" >nul && cl /nologo /LD /O2 /W3 "{cpath}" /Fe:"{dll}"'
    p = subprocess.run(cmd, cwd=workdir, shell=True, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    log = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0 or not os.path.exists(dll):
        raise BuildError(f"cl exited with {p.returncode}\n{log}")
    return dll, log
def run_dll(path: str, g: Graph, arrays: list[np.ndarray]) -> list[np.ndarray]:
    fn = ctypes.CDLL(os.path.abspath(path)).aic_run
    outs = [np.zeros(g.values[v].numel, np.float32) for v in g.outputs]
    fn.argtypes = [ctypes.POINTER(ctypes.c_float)] * (len(arrays) + len(outs))
    fn.restype = None
    ptr = lambda a: a.reshape(-1).ctypes.data_as(ctypes.POINTER(ctypes.c_float))
    fn(*[ptr(a) for a in arrays + outs])
    return [o.reshape(g.values[v].shape) for o, v in zip(outs, g.outputs)]
def compile_module(module, example_inputs, workdir, broken=False):
    g = trace_module(module, example_inputs)
    n_fold = fold_constants(g)
    n_inline = fuse_elementwise(g)
    ks = schedule(g)
    csrc = emit_c(g, ks)
    dll, log = build_dll(csrc, workdir, broken)
    return g, ks, csrc, dll, log, n_fold, n_inline
```

端到端驱动：

```python
import os
import numpy as np
import torch
import compiler as c
class DemoNet(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(3, 4))
        self.b = torch.nn.Parameter(torch.randn(4))
        self.scale = torch.nn.Parameter(torch.tensor(2.0))
        self.shift = torch.nn.Parameter(torch.tensor(1.0))
    def forward(self, x):
        h = x @ self.w
        h = torch.relu((h + self.b) * self.scale)
        return (h + self.shift * 3.0).sum()
def main():
    torch.manual_seed(0)
    model, x = DemoNet(), torch.randn(2, 3)
    here = os.path.dirname(os.path.abspath(__file__))
    broken = os.environ.get("AIC_BROKEN") == "1"
    g, ks, _, dll, log, nf, ni = c.compile_module(model, [x], os.path.join(here, "build"), broken)
    print("[graph]", [(o.name, [g.values[v].shape for v in o.inputs], g.values[o.out].shape) for o in g.ops])
    print(f"[passes] folded={nf} inlined={ni} kernels={[k.kind for k in ks]}")
    print("[cl]", log.strip() or "(no output)")
    got = c.run_dll(dll, g, [x.numpy()])[0]
    ref = model(x).detach().numpy()
    print(f"[numeric] compiled={got.item():.7f} torch={ref.item():.7f} maxerr={abs(got.item()-ref.item()):.3e}")
    print("[artifact]", os.path.abspath(dll))
if __name__ == "__main__":
    main()
```

实测输出（seed 0）：

```text
[graph] [('matmul', [(2, 3), (3, 4)], (2, 4)), ('ewfused', [(2, 4), (4,)], (2, 4)), ('sum', [(2, 4)], ())]
[passes] folded=1 inlined=3 kernels=['matmul', 'ew', 'sum']
[numeric] compiled=31.7705345 torch=31.7705345 maxerr=0.000e+00
[artifact] D:\Learning-with-Claude\.scratch\ai-compiler\final\build\kernel.dll
```

`maxerr=0` 只说明这个小例子在两种累加顺序下恰好一致；更大的 matmul 会出现 `1e-6` 量级的相对误差，见测试矩阵。

### M6：失败路径与回归

把 `forward` 换成 `torch.sigmoid`，前端在翻译阶段就拒绝，不生成错误的 C：

```python
import torch, compiler as c
class Bad(torch.nn.Module):
    def forward(self, x):
        return torch.sigmoid(x)
c.trace_module(Bad(), [torch.randn(2, 3)])
```

实测：

```text
UnsupportedOpError: unsupported op 'sigmoid' at node 'sigmoid'; supported: matmul, add, mul, relu, sum
```

在 PowerShell 里设 `$env:AIC_BROKEN='1'` 会在 `aic_run` 前注入 `int broken = ;`，实测 `cl` 的原始报错被完整带出：

```text
BuildError: cl exited with 2
kernel.c
D:\Learning-with-Claude\.scratch\ai-compiler\final\build\kernel.c(34): error C2059: 语法错误:“;”
```

形状错误也在前端解决：`CompileError: matmul inner dims mismatch: (2, 3) @ (4, 5)`。

## 完整代码清单

```text
.scratch/ai-compiler/final/
├─ compiler.py       # M1–M5 按顺序拼接（去空行后约 350 行）
├─ run_demo.py       # M5 端到端驱动
├─ build.ps1         # 独立构建脚本
├─ tests.py          # 5 个回归用例
└─ build/kernel.c    # M4 生成物
```

`compiler.py` 没有隐藏尾部：M1 + M2 + M3 + M4 + M5 五个 Python 块依次拼接就是完整文件；`run_demo.py` 单独一个文件。`tests.py` 只是循环调用 `compile_module` / `run_dll`，不是编译器的一部分。

构建脚本：

```powershell
param([string]$Source = "kernel.c", [string]$Output = "kernel.dll")
$ErrorActionPreference = "Stop"
$vcvars = "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
$src = (Resolve-Path $Source).Path
$dll = Join-Path (Get-Location) $Output
cmd /c "call `"$vcvars`" >nul && cl /nologo /LD /O2 /W3 `"$src`" /Fe:`"$dll`""
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "built $dll"
```

生成的 `kernel.c`（seed 0，全文）：

```c
/* generated by mini compiler */
#include <math.h>
#include <stdlib.h>
static const float c0[12] = {1.54099607f, -0.293428898f, -2.17878938f, 0.568431258f, -1.08452237f, -1.39859545f, 0.403346837f, 0.838026345f, -0.719257593f, -0.403343529f, -0.596635342f, 0.182036489f};
static const float c1[4] = {-0.856674612f, 1.10060418f, -1.07118738f, 0.122701243f};
static void k0_matmul(const float* __restrict in0, const float* __restrict c0, float* __restrict v0) {
  const int M=2, N=4, K=3, T=8;
  for (int i=0;i<M;++i) for (int j=0;j<N;++j) v0[i*N+j]=0.0f;
  for (int ii=0;ii<M;ii+=T) for (int kk=0;kk<K;kk+=T) for (int jj=0;jj<N;jj+=T) {
    int ie=ii+T<M?ii+T:M, ke=kk+T<K?kk+T:K, je=jj+T<N?jj+T:N;
    for (int i=ii;i<ie;++i) for (int p=kk;p<ke;++p) {
      float a=in0[i*K+p];
      for (int j=jj;j<je;++j) v0[i*N+j]+=a*c0[p*N+j];
    }
  }
}
static void k1_ew(const float* __restrict v0, const float* __restrict c1, float* __restrict v1) {
  const int N=8, D1=4;
  for (int p=0;p<N;++p) {
    int i=p/D1, j=p%D1;
    v1[p]=(fmaxf(((v0[i*4+j] + c1[j]) * 2.0f), 0.0f) + 3.0f);
  }
}
static void k2_sum(const float* __restrict v1, float* __restrict out0) {
  float acc=0.0f;
  for (int p=0;p<8;++p) acc+=v1[p];
  out0[0]=acc;
}
__declspec(dllexport) void aic_run(const float* __restrict in0, float* __restrict out0) {
  float* v0=(float*)malloc(8*sizeof(float));
  if (!v0) return;
  float* v1=(float*)malloc(8*sizeof(float));
  if (!v1) return;
  k0_matmul(in0, c0, v0);
  k1_ew(v0, c1, v1);
  k2_sum(v1, out0);
  free(v1);
  free(v0);
}
```

`aic_run` 是唯一导出符号，两个参数分别对应输入 `x` 和标量输出；C 里没有 Python 依赖，也没有 `stdio.h`。

## 构建与运行

```powershell
$env:PYTHONIOENCODING = "utf-8"
cd D:\Learning-with-Claude\.scratch\ai-compiler\final
$py = "D:\Learning-with-Claude\.scratch\aic-env\Scripts\python.exe"
& $py run_demo.py
```

`build.ps1` 用同样的 `call vcvars64.bat && cl ...`；单独重编时实测输出 `built ...\kernel_ps.dll`。

`cl` 路径写在 `VCVARS64`；换机器只改这一处，或用 `AIC_VCVARS` 覆盖。

## 测试用例与真实输出

`tests.py` 每个用例构造前 `torch.manual_seed(2026)`，覆盖 2-D matmul、bias 广播、纯 elementwise、大矩阵和两次 matmul。实测：

```text
demo    kernels=['matmul', 'ew', 'sum']          fold=1 inline=3 ref=     39.919529 maxerr=0.000e+00 relerr=0.00e+00
mlp     kernels=['matmul', 'ew', 'sum']          fold=0 inline=1 ref=     24.368500 maxerr=0.000e+00 relerr=0.00e+00
ew      kernels=['ew']                           fold=0 inline=2 ref=     -1.413864 maxerr=0.000e+00 relerr=0.00e+00
big64   kernels=['matmul', 'ew', 'sum']          fold=0 inline=1 ref=  13602.249023 maxerr=1.172e-02 relerr=8.62e-07
two_mm  kernels=['matmul', 'ew', 'matmul', 'sum'] fold=0 inline=0 ref=     -1.997047 maxerr=1.907e-06 relerr=9.55e-07
```

`big64` 的 `1.17e-02` 绝对误差对应 `1.36e+04` 量级，相对误差 `8.6e-07`，是 float32 累加顺序差异；`two_mm` 的两层累积仍在 `1e-6` 量级。

性能只做朴素对比，不宣称加速。机器：Intel i7-10510U（4C/8T，CPU-only），torch 2.14.1+cpu，`torch.set_num_threads(1)`；每次 3 次预热 + 5 轮 × 20 次取中位数，独立跑 3 次给范围；编译产物 `/O2` 单线程。

| n | 编译产物 | torch 单线程 | 耗时比 | maxerr（相对） |
|---|---|---|---|---|
| 128 | 2.13–3.02 ms | 0.15–0.28 ms | 11–15× | 7.03e-02（9.42e-07） |
| 256 | 15.9–20.9 ms | 0.76–1.27 ms | 16–22× | 1.531（3.63e-06） |

更慢是预期：没有 SIMD、BLAS 和多线程，tile 只有 16。这个数字的价值是把图优化之后的剩余差距量化出来，追赶手段见 Ch 11、Ch 15。

## 常见失败与排查

- **`UnsupportedOpError`**：看报错里的 node 和 target。新增算子要同时改 `SUPPORTED`、`_infer`、`_eval`、`emit_c` 四处，缺一处会在下一阶段炸。
- **`BuildError` 带 `C2059` / `C2065`**：先读 `kernel.c` 出错行，再看 `cl` 完整输出；`AIC_BROKEN=1` 就是故意注入语法错误，验证错误没有被吞。
- **`vcvars64.bat` 找不到**：`cl` 不在 PATH 时先 `call` 它；路径随 Visual Studio 版本变化，改 `VCVARS64` 或设 `AIC_VCVARS`。
- **matmul 内维不匹配**：`CompileError: matmul inner dims mismatch` 来自前端 shape 推导，检查示例输入与权重 `(K,N)`。
- **`maxerr` 看着大**：先看相对误差和输出量级；大 reduction 的绝对误差到 `1e-2` 不奇怪，相对误差到 `1e-2` 才是语义错。

## 扩展方向

- **更多算子**：`exp`、`tanh`、`softmax`、`conv2d`；softmax 正好练习 reduction + broadcast 融合（Ch 7）。
- **内存复用**：现在每个临时 buffer 单独 `malloc/free`；按 Ch 10 的 liveness 复用可降峰值。
- **epilogue 融合**：把 matmul 后的 bias/relu 写进累加循环，省一次中间结果落盘（Ch 7）。
- **向量化与并行**：给内层 `j` 加 SIMD、给 `ii` 加 OpenMP，或改 blocking；这是 11–22× 差距的主要来源（Ch 11、Ch 15）。
- **动态 shape**：把 shape 从编译期常量改成运行时参数，需要重设 buffer 和 kernel 签名。
- **CUDA 后端**：本文只做 CPU C；GPU 代码生成标 `guided`，本机无 CUDA toolchain，不伪造 kernel 或加速比。

## 验收清单

- [ ] `run_demo.py` 生成 `build/kernel.c` 并构建 `kernel.dll`
- [ ] `ctypes` 调用返回标量，demo 的 `maxerr=0`
- [ ] 至少两个 pass 真实改图：`folded=1`、`inlined=3`
- [ ] matmul 走 tiled 三重循环，elementwise 走单个扁平循环
- [ ] 不支持的 op 报 `UnsupportedOpError`，含 op 名和支持列表
- [ ] 故意写坏 C 时，`BuildError` 含 `cl` 原始错误行
- [ ] 5 个回归用例通过，`big64` 相对误差 < `1e-5`

## 延伸阅读

- [Ch 5 FX 变换](./05-fx-transformations.md)：读 Interpreter / Transformer / `replace_pattern`，对照本文的 `trace_module` 与融合 pass。
- [Ch 7 算子融合](./07-operator-fusion.md)：读纵向融合、epilogue、reduction 融合；本文只覆盖同形状 elementwise 链。
- [Ch 12 TVM TIR 与 schedule](./12-tvm-tir-schedule.md)：读 `split/reorder/fuse` 与 0.27 的 `s_tir.Schedule`，把手写循环和调度原语对照。
- [PyTorch FX 文档](https://pytorch.org/docs/stable/fx.html)：读 `symbolic_trace`、`Interpreter`、`Transformer`；target 映射和 `get_attr` 处理以此为准。
- [TVM OSDI 2018](https://www.usenix.org/conference/osdi18/presentation/chen)：读第 3–5 节，理解“计算与调度分离”为什么能支撑这种 mini 后端。

---

→ [返回课程首页](./README.md)