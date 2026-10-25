# 练习 A：扩展图 IR 与解释器，做随机图差分测试

> **目标**：给 mini IR 增加 `neg`、`tanh`、`transpose` 三个算子，同时扩展形状推导与解释器；写一个形状感知的随机图生成器；用 PyTorch 作为独立参考端，对 200 张随机图做逐中间值差分测试；再注入一个已知 bug，证明测试能抓到它。完成后你应该能独立评估“我的测试到底有没有牙齿”。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；`torch 2.14.1+cpu`；`numpy 2.4.6`；虚拟环境 `.scratch/aic-env`。参考解答保存为 `compilers/mini_ai_compiler/code/practice_a.py`，从仓库根目录运行：
>
> ```powershell
> $env:PYTHONIOENCODING = "utf-8"
> & ".scratch/aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\practice_a.py"
> ```
>
> 脚本所在目录会被 Python 自动加入 `sys.path`，因此不需要额外设置 `PYTHONPATH`。

## 背景

第 5 章的解释器只覆盖 `ir.py` 里已有的算子。真实编译器扩展算子集时，改动会散落在三处：类型推导、执行语义、以及所有依赖这些算子的 pass。漏掉任何一处，表现都不同：

- 漏掉形状推导：`Graph.add_op` 直接报 `unsupported op`，图建不出来。
- 漏掉解释器：图建得出来，跑到该算子时抛 `IRError`。
- 漏掉 `passes.py` 里的绑定：解释器能跑，常量折叠却炸。这是本练习最容易踩的坑，下面用实跑证明。

差分测试（differential testing）是验证扩展没有破坏语义的常用手段：随机生成合法图，分别用两套独立实现求值，比较结果。它的价值取决于两件事：生成器覆盖了多少形状与算子组合，以及比较粒度是“只看输出”还是“看每个中间值”。本练习两者都要求。

## 任务规格

### A.1 新增算子

| 算子 | 输入个数 | attrs | 输出形状 | NumPy 语义 |
| --- | --- | --- | --- | --- |
| `neg` | 1 | 无 | 与输入相同 | `-x` |
| `tanh` | 1 | 无 | 与输入相同 | `np.tanh(x)` |
| `transpose` | 1 | `perm: tuple[int, ...]`，缺省为逆序 | `tuple(shape[p] for p in perm)` | `np.transpose(x, perm)` |

约束：

- 全部输出 `float32`。`np.tanh` 对 float32 输入返回 float32，仍显式 `np.asarray(..., dtype=np.float32)`。
- `neg`、`tanh` 的输入个数不是 1 时抛 `IRError`，消息包含算子名和实际个数。
- `transpose` 的 `perm` 必须是 `range(rank)` 的一个排列；长度不对、重复、越界都抛 `IRError`。
- 不修改仓库源码也可以完成：运行时替换 `mini_ai.ir.infer_output_type`、`mini_ai.interpreter.apply_op`、`mini_ai.passes.apply_op` 三个模块属性。若你选择直接改源码，改完要重跑全部 12 个单元测试。

### A.2 扩展形状推导

`Graph.add_op` 在 `ir.py` 内部调用的是模块级函数 `infer_output_type`，因此替换 `ir_mod.infer_output_type` 就能影响后续所有建图：

```python
import mini_ai.ir as ir_mod

_ORIG_INFER = ir_mod.infer_output_type


def ext_infer(kind, inputs, attrs):
    if kind in {"neg", "tanh"}:
        if len(inputs) != 1:
            raise IRError(f"{kind} expects 1 input, got {len(inputs)}")
        return inputs[0]
    if kind == "transpose":
        if len(inputs) != 1:
            raise IRError(f"transpose expects 1 input, got {len(inputs)}")
        shape = inputs[0].shape
        perm = tuple(attrs.get("perm", tuple(range(len(shape) - 1, -1, -1))))
        if sorted(perm) != list(range(len(shape))):
            raise IRError(f"transpose needs a permutation of {list(range(len(shape)))}, got {perm}")
        return TensorType(tuple(shape[p] for p in perm))
    return _ORIG_INFER(kind, inputs, attrs)


ir_mod.infer_output_type = ext_infer
```

`perm` 的缺省值在 `attrs.get` 的默认参数里算出来，不能在函数定义时算，因为 rank 要到运行时才知道。

### A.3 扩展解释器

```python
import mini_ai.interpreter as interp_mod
import mini_ai.passes as passes_mod

_ORIG_APPLY = interp_mod.apply_op


def ext_apply(kind, arrays, attrs):
    if kind == "neg":
        return np.asarray(-arrays[0], dtype=np.float32)
    if kind == "tanh":
        return np.asarray(np.tanh(arrays[0]), dtype=np.float32)
    if kind == "transpose":
        perm = tuple(attrs.get("perm", ()))
        return np.asarray(np.transpose(arrays[0], perm), dtype=np.float32)
    return _ORIG_APPLY(kind, arrays, attrs)


interp_mod.apply_op = ext_apply
passes_mod.apply_op = ext_apply   # 这行不能漏，原因见下
```

为什么必须同时改 `passes_mod.apply_op`：`passes.py` 顶部写的是

```python
from .interpreter import apply_op
```

这是导入时绑定。之后再把 `interpreter.apply_op` 指向新函数，`passes` 模块里的名字仍指向旧函数。后果不是理论问题，本机实跑：

```text
fold without patching passes: interpreter does not support op: neg
```

图里有一个“常量 `neg`”，`constant_fold` 调用的还是旧 `apply_op`，于是失败。修法就是上面那行 `passes_mod.apply_op = ext_apply`，或者直接修改源码让 `passes` 通过模块属性调用。

### A.4 随机图生成器

生成器必须保证“建出来的图合法”，否则测的是 verifier 而不是解释器。推荐结构：

```text
random_case(seed)
  ├── 2 个输入：rank 1..3，维度 1..4，值 uniform(-1, 1)，float32
  ├── 4..8 个算子，按当前可用值的 rank 选择候选
  │     ├── 二元：从可广播的值对里抽，div 的分母用常量 [0.5, 1.5]
  │     ├── 一元：relu / exp / neg / tanh
  │     ├── transpose：仅当存在 rank >= 2 的值
  │     ├── sum / softmax：仅当存在 rank >= 1 的值，axis 在合法范围内
  │     └── matmul：仅当存在 rank == 2 的值，右侧权重形状 (k, n) 新建
  ├── 1..2 个输出（最后两个值）
  └── ref：与 IR 同步构建的 PyTorch 张量字典，键为 Value.id
```

两个细节：

- `matmul` 只接受二维。生成权重时用 `(v.type.shape[1], n)`，别把 `(m, k)` 当成权重形状。
- `div` 的分母用 `[0.5, 1.5]` 的常量，避免接近零导致输出爆炸，把容差问题伪装成正确性问题。

### A.5 差分测试与负向对照

比较分两层：

1. `evaluate_graph` 的输出与 `ref` 中对应输出比较。
2. 逐 op 重放：用 `interp_mod.apply_op` 算出每个中间值，与 `ref[op.output.id]` 比较。

第二层是关键。只看输出会漏掉“错误值被后续算子抵消”的情况，也会漏掉“某个中间值错了但没流到输出”。本练习的负向对照会展示另一半问题：即使逐中间值比较，退化形状也能让错误不可见。

负向对照（negative control）的做法：在 `ext_apply` 的 `transpose` 分支注入一个必然改变排列的 bug（把 `perm` 的每个元素加 1 再取模），重跑同一批种子，统计有多少张图被抓到。一个测试套件如果抓不到已知 bug，就没有资格证明“200 张全过”意味着正确。

### A.6 验收标准

- 200 张随机图全部通过，`worst_abs_error <= 1e-5`。
- 负向对照能抓到所有“包含 `transpose` 且输出依赖它”的图；抓不到的要能解释原因。
- 三类错误路径（坏 `perm`、`neg` 参数个数错、未知算子）都有明确报错。
- 原有 12 个单元测试仍通过。
- 报告里给出种子范围、图数量、容差、最大误差、负向对照命中数。

## 期望输出

参考解答在本机的完整输出：

```text
differential: graphs=200 passed=200 failed=0 worst_abs_error=2.861e-06
negative control (rotated perm): graphs=200 with_transpose=99 caught=94
missed transpose shapes: [(1, 1), (1, 1), (1, 1), (1, 1), (1, 1), (1, 1), (1, 1)]
bad perm: transpose needs a permutation of [0, 1], got (0, 0)
neg arity: neg expects 1 input, got 2
```

读法：

- 200 张图全部通过，最大误差 2.861e-06，低于 1e-5。误差主要来自 PyTorch 与 NumPy 在 `exp`、`softmax`、矩阵乘上的实现差异，以及 float32 舍入。
- 负向对照覆盖 99 张含 `transpose` 的图，抓到 94 张。剩下 5 张图里有 7 个 `transpose`，输入形状全是 `(1, 1)`。这种形状下任何排列都得到同一个结果，bug 在数值上不可见。这说明“生成器覆盖了算子”不等于“覆盖了能暴露 bug 的形状”。
- 计数由种子决定，换机器应保持一致；`worst_abs_error` 可能因 BLAS 线程数有末位差异。

## 卡点提示

1. **只改了解释器，没改 `passes`**。症状：解释器跑得通，`constant_fold` 报 `interpreter does not support op: neg`。原因见 A.3 的导入绑定。
2. **`Graph.add_op` 仍然报 `unsupported op`**。说明 `ir_mod.infer_output_type` 没替换成功，或者你在 `from mini_ai.ir import infer_output_type` 之后才替换模块属性。`add_op` 查的是 `ir` 模块的全局名字，不是调用方导入的名字。
3. **`perm` 校验写成了 `set(perm) == set(range(rank))`**。集合会丢掉重复信息，`(0, 0, 1)` 与 `(0, 1, 2)` 在某些 rank 下会被误判。用 `sorted(perm) == list(range(rank))`。
4. **PyTorch 参考端用错轴**。`torch.sum(x, dim=axis, keepdim=...)`、`torch.softmax(x, dim=axis)`、`x.permute(*perm)`，三者的轴语义与 NumPy 一致，但参数名不同；`permute` 接收可变参数，不是元组。
5. **随机生成器产生非法组合**。`transpose` 需要 rank >= 2，`matmul` 需要二维，`sum`/`softmax` 的 axis 必须小于 rank。用“候选菜单 + 条件加入”的方式，别先随机算子再补救形状。
6. **退化形状让 bug 隐身**。`(1, 1)` 的转置与恒等不可区分；`(1, 1, 1)` 同理。负向对照要统计“含目标算子的图”和“被抓到的图”，差值要能解释。
7. **比较时没统一 float32**。PyTorch 张量默认 float32，NumPy 生成时也要 `.astype(np.float32)`；否则参考端是 float64，误差会虚高。

## 实现建议

1. **先跑基线**。执行 `python -m unittest discover -s compilers\mini_ai_compiler\code\tests -v`，记下 12 个测试全部通过。扩展后重跑，确认没有回归。
2. **写形状推导与执行语义**。先只加 `neg`，用 `Graph.add_op` 建图并 `evaluate_graph` 验证 `-x`；再加 `tanh`；最后加 `transpose`，并把坏 `perm` 的错误路径测出来。每加一个算子就手写一个小例子，别等随机测试。
3. **写生成器，先固定种子打印图**。用 `g.dump()` 检查前 3 张图，确认算子、形状、输出都合理，再放开到 200 张。生成器有 bug 时，200 张全过会给你虚假信心。
4. **接 PyTorch 参考端，先比输出，再比中间值**。中间值比较要用 `Value.id` 索引 `ref`，不要用名字（名字可能重复）。先跑 20 张，确认没有形状错误，再扩到 200 张。
5. **注入 bug 做负向对照**。把 `transpose` 的 `perm` 旋转一位，统计命中；再把 bug 撤掉，确认 200 张仍然全过。最后写报告，解释没抓到的退化形状。

## 参考解答

<details>
<summary>展开完整脚本（242 行，已在本机跑通）</summary>

```python
"""Practice A reference solution.

Extends the mini IR with ``neg``, ``tanh`` and ``transpose``, then runs a
random differential test: NumPy interpreter vs PyTorch reference.
"""

from __future__ import annotations

import random
from typing import Any

import numpy as np
import torch

import mini_ai.ir as ir_mod
import mini_ai.interpreter as interp_mod
import mini_ai.passes as passes_mod
from mini_ai import evaluate_graph
from mini_ai.interpreter import max_abs_error
from mini_ai.ir import Graph, IRError, TensorType, Value

_ORIG_APPLY = interp_mod.apply_op
_ORIG_INFER = ir_mod.infer_output_type
BUGGY_TRANSPOSE = False


def ext_apply(kind: str, arrays: list[np.ndarray], attrs: dict) -> np.ndarray:
    if kind == "neg":
        return np.asarray(-arrays[0], dtype=np.float32)
    if kind == "tanh":
        return np.asarray(np.tanh(arrays[0]), dtype=np.float32)
    if kind == "transpose":
        perm = tuple(attrs.get("perm", ()))
        if BUGGY_TRANSPOSE:
            perm = tuple((p + 1) % arrays[0].ndim for p in perm)
        return np.asarray(np.transpose(arrays[0], perm), dtype=np.float32)
    return _ORIG_APPLY(kind, arrays, attrs)


def ext_infer(kind: str, inputs: list[TensorType], attrs: dict[str, Any]) -> TensorType:
    if kind in {"neg", "tanh"}:
        if len(inputs) != 1:
            raise IRError(f"{kind} expects 1 input, got {len(inputs)}")
        return inputs[0]
    if kind == "transpose":
        if len(inputs) != 1:
            raise IRError(f"transpose expects 1 input, got {len(inputs)}")
        shape = inputs[0].shape
        perm = tuple(attrs.get("perm", tuple(range(len(shape) - 1, -1, -1))))
        if sorted(perm) != list(range(len(shape))):
            raise IRError(f"transpose needs a permutation of {list(range(len(shape)))}, got {perm}")
        return TensorType(tuple(shape[p] for p in perm))
    return _ORIG_INFER(kind, inputs, attrs)


def install() -> None:
    ir_mod.infer_output_type = ext_infer
    interp_mod.apply_op = ext_apply
    passes_mod.apply_op = ext_apply


BINARY = {"add": torch.add, "sub": torch.sub, "mul": torch.mul, "div": torch.div}


def torch_apply(kind: str, a: list[torch.Tensor], attrs: dict) -> torch.Tensor:
    if kind in BINARY:
        return BINARY[kind](a[0], a[1])
    if kind == "relu":
        return torch.relu(a[0])
    if kind == "exp":
        return torch.exp(a[0])
    if kind == "neg":
        return -a[0]
    if kind == "tanh":
        return torch.tanh(a[0])
    if kind == "transpose":
        return a[0].permute(*attrs["perm"])
    if kind == "sum":
        return torch.sum(a[0], dim=attrs["axis"], keepdim=attrs["keepdims"])
    if kind == "softmax":
        return torch.softmax(a[0], dim=attrs["axis"])
    if kind == "matmul":
        return torch.matmul(a[0], a[1])
    raise AssertionError(kind)


def compatible_pairs(values: list[Value]) -> list[tuple[Value, Value]]:
    pairs = []
    for i, a in enumerate(values):
        for b in values[i:]:
            try:
                ir_mod.broadcast_shape(a.type.shape, b.type.shape)
            except IRError:
                continue
            pairs.append((a, b))
    return pairs


def random_case(seed: int) -> tuple[Graph, dict[str, np.ndarray], dict[int, torch.Tensor]]:
    rng = random.Random(seed)
    g = Graph()
    values: list[Value] = []
    feed: dict[str, np.ndarray] = {}
    ref: dict[int, torch.Tensor] = {}

    def add_const(data: np.ndarray) -> Value:
        v = g.add_const(f"c_{len(values)}", data)
        ref[v.id] = torch.from_numpy(data.astype(np.float32, copy=True))
        return v

    for i in range(2):
        rank = rng.randint(1, 3)
        shape = tuple(rng.randint(1, 4) for _ in range(rank))
        arr = np.random.default_rng(seed * 100 + i).uniform(-1, 1, shape).astype(np.float32)
        v = g.add_input(f"x{i}", shape)
        values.append(v)
        feed[v.name] = arr
        ref[v.id] = torch.from_numpy(arr.copy())

    def step(kind: str, ins: list[Value], attrs: dict) -> None:
        out = g.add_op(kind, ins, attrs)
        values.append(out)
        ref[out.id] = torch_apply(kind, [ref[v.id] for v in ins], attrs)

    for _ in range(rng.randint(4, 8)):
        menu = ["binary", "binary", "unary", "unary"]
        if any(v.type.rank >= 2 for v in values):
            menu.append("transpose")
        if any(v.type.rank >= 1 for v in values):
            menu += ["sum", "softmax"]
        if any(v.type.rank == 2 for v in values):
            menu.append("matmul")
        choice = rng.choice(menu)
        if choice == "binary":
            kind = rng.choice(list(BINARY))
            if kind == "div":
                den = add_const(np.random.default_rng(seed + 7).uniform(0.5, 1.5, (1,)).astype(np.float32))
                step(kind, [rng.choice(values), den], {})
            else:
                a, b = rng.choice(compatible_pairs(values))
                step(kind, [a, b], {})
        elif choice == "unary":
            step(rng.choice(["relu", "exp", "neg", "tanh"]), [rng.choice(values)], {})
        elif choice == "transpose":
            v = rng.choice([v for v in values if v.type.rank >= 2])
            perm = list(range(v.type.rank))
            rng.shuffle(perm)
            step("transpose", [v], {"perm": tuple(perm)})
        elif choice == "sum":
            v = rng.choice([v for v in values if v.type.rank >= 1])
            step("sum", [v], {"axis": rng.randrange(v.type.rank), "keepdims": rng.random() < 0.5})
        elif choice == "softmax":
            v = rng.choice([v for v in values if v.type.rank >= 1])
            step("softmax", [v], {"axis": rng.randrange(v.type.rank)})
        else:
            v = rng.choice([v for v in values if v.type.rank == 2])
            w = add_const(np.random.default_rng(seed + 11).uniform(-0.5, 0.5, (v.type.shape[1], rng.randint(1, 4))).astype(np.float32))
            step("matmul", [v, w], {})

    outs = [values[-1]] if rng.random() < 0.5 else [values[-2], values[-1]]
    for i, v in enumerate(outs):
        g.add_output(v, f"out{i}")
    return g, feed, ref


def middle_errors(g, feed, ref) -> float:
    """Compare every intermediate value, not only the graph outputs."""
    env = {v: np.asarray(v.data, dtype=np.float32) for v in g.values if v.kind == "constant"}
    for v in g.inputs:
        env[v] = np.asarray(feed[v.name], dtype=np.float32)
    worst = 0.0
    for op in g.ops:
        out = interp_mod.apply_op(op.kind, [env[v] for v in op.inputs], op.attrs)
        env[op.output] = out
        worst = max(worst, max_abs_error(out, ref[op.output.id].numpy()))
    return worst


def run_suite(n: int, *, tolerance: float = 1e-5) -> tuple[int, int, float]:
    passed = failed = 0
    worst = 0.0
    for seed in range(n):
        g, feed, ref = random_case(seed)
        try:
            outs = evaluate_graph(g, feed)
            errs = [max_abs_error(o, ref[v.id].numpy()) for o, v in zip(outs, g.outputs)]
            err = max([middle_errors(g, feed, ref), *errs])
            worst = max(worst, err)
            if err <= tolerance:
                passed += 1
            else:
                failed += 1
                print(f"seed {seed}: max_abs_error {err:.3e}")
        except Exception as exc:  # shape mismatch, unsupported op, ...
            failed += 1
            print(f"seed {seed}: {type(exc).__name__}: {exc}")
    return passed, failed, worst


def main() -> None:
    install()
    cases = 200
    passed, failed, worst = run_suite(cases)
    print(f"differential: graphs={cases} passed={passed} failed={failed} worst_abs_error={worst:.3e}")

    global BUGGY_TRANSPOSE
    BUGGY_TRANSPOSE = True
    caught = with_transpose = 0
    missed_shapes = []
    for seed in range(200):
        g, feed, ref = random_case(seed)
        tshapes = [op.inputs[0].type.shape for op in g.ops if op.kind == "transpose"]
        if tshapes:
            with_transpose += 1
        try:
            evaluate_graph(g, feed)
            bad = middle_errors(g, feed, ref) > 1e-5
        except Exception:
            bad = True
        if bad:
            caught += 1
        elif tshapes:
            missed_shapes.extend(tshapes)
    BUGGY_TRANSPOSE = False
    print(f"negative control (rotated perm): graphs=200 with_transpose={with_transpose} caught={caught}")
    print("missed transpose shapes:", missed_shapes)

    # error path: bad perm
    g = Graph()
    x = g.add_input("x", (2, 3))
    try:
        g.add_op("transpose", [x], {"perm": (0, 0)})
    except IRError as exc:
        print("bad perm:", exc)
    try:
        g.add_op("neg", [x, x])
    except IRError as exc:
        print("neg arity:", exc)


if __name__ == "__main__":
    main()
```

</details>

## 测试用例表

| 编号 | 输入 / 操作 | 期望 | 为什么测这个 |
| --- | --- | --- | --- |
| T1 | `neg` 作用于 `(2, 3)` 输入 | 形状 `(2, 3)`，值逐元素取负，dtype float32 | 一元算子的形状保持与精度 |
| T2 | `tanh` 作用于 `(-2, 0, 2)` | 值在 `(-1, 1)`，单调递增，0 处为 0 | 语义与值域，防止实现成 `sigmoid` |
| T3 | `transpose` 作用于 `(2, 3)`，`perm=(1, 0)` | 形状 `(3, 2)`，`out[i, j] == x[j, i]` | 排列方向容易写反 |
| T4 | `transpose` 作用于 `(2, 3, 4)`，`perm=(2, 0, 1)` | 形状 `(4, 2, 3)` | 高维排列与形状推导一致性 |
| T5 | `transpose` 缺省 `perm` | 形状逆序，值等于全轴反转 | 缺省参数在运行时计算 |
| T6 | `transpose` `perm=(0, 0)` | `IRError: transpose needs a permutation of [0, 1], got (0, 0)` | 重复轴必须拦截 |
| T7 | `transpose` `perm=(0,)` 作用于 `(2, 3)` | `IRError`（长度不是 rank） | 长度错误与重复错误分开 |
| T8 | `neg` 传两个输入 | `IRError: neg expects 1 input, got 2` | 参数个数校验 |
| T9 | 200 张随机图，种子 0..199 | `passed=200 failed=0`，误差 <= 1e-5 | 主验收 |
| T10 | 注入 `perm` 旋转 bug，种子 0..199 | `caught=94`，未命中的 7 个 `transpose` 输入均为 `(1, 1)` | 测试灵敏度与覆盖盲区 |
| T11 | 未替换 `passes.apply_op` 时折叠常量 `neg` | `IRError: interpreter does not support op: neg` | 导入绑定的坑 |
| T12 | 原有 12 个单元测试 | 全部通过 | 扩展没有破坏原语义 |

T6 的实际报错来自本机：

```text
bad perm: transpose needs a permutation of [0, 1], got (0, 0)
neg arity: neg expects 1 input, got 2
```

## 延伸挑战

1. **再加一个带属性的二元算子**。例如 `pow(x, y)`（逐元素幂），或者 `slice(x, start, stop, axis)`。要求：形状推导读 attrs、越界报错、随机生成器覆盖、参考端用 `torch.pow` / 切片实现。切片会让“输出形状 = 输入形状”的假设失效，是下一个难度台阶。
2. **让生成器覆盖零尺寸维度**。`shape` 里允许 0，例如 `(0, 3)`。NumPy 与 PyTorch 都支持空张量，但 `max_abs_error` 对空张量返回 0.0，测试会假通过。要求：空张量时改为比较形状与 `numel`。
3. **做测试用例最小化**。当某张随机图失败时，自动删减算子或缩小维度，直到得到仍失败的最小图。这是编译器 bug 报告的常见需求，第 24 章会再展开。
4. **把差分测试接到 `standard_pipeline` 之后**。先跑随机图，再跑 `constant_fold` / CSE，比较优化前后的输出。要求先修好 A.3 的绑定问题，否则新算子无法折叠。
5. **支持批量 matmul**。把 `matmul` 从二维扩展到 `(B, M, K) @ (B, K, N)`，同步修改 `infer_output_type`、`apply_op`、C 后端与 LLVM 后端。这个改动会影响第 16 到 22 章，先只做 IR 与解释器，确认差分测试通过。
6. **把负向对照做成参数**。用环境变量或命令行参数注入不同类型的 bug（错排列、错轴、少加偏置），统计每类 bug 的命中率。命中率低的类别说明生成器覆盖不足。

## 自我检核

- [ ] 我能说出扩展一个算子需要改哪三个位置，以及漏掉 `passes` 时的具体报错。
- [ ] 我能解释 `Graph.add_op` 为什么能找到我替换的 `infer_output_type`。
- [ ] 我的 `perm` 校验能同时拒绝重复、越界、长度错误。
- [ ] 我的随机生成器不会产生非法广播、非法 matmul 或越界 axis。
- [ ] 我的差分测试比较了每个中间值，不只比较输出。
- [ ] 我能解释负向对照为什么只抓到 94/99，以及剩下的 7 个 `transpose` 为什么不可见。
- [ ] 我能给出 200 张图的最大误差，并说明它为什么低于 1e-5。
- [ ] 我重跑了原有 12 个单元测试，确认没有回归。
- [ ] 我的报告包含种子范围、图数量、容差、最大误差、命中数，而不是只有一句“测试通过”。

## 延伸阅读

- **Hypothesis 文档**（<https://hypothesis.readthedocs.io/>）：读“What is property-based testing?”与 shrinking 章节。关联本练习的随机图生成与最小化挑战；把“手写生成器”升级为“属性测试 + 自动收缩”。
- **Csmith 论文**（Yang et al., “Finding and Understanding Bugs in C Compilers”, PLDI 2011）：读差分测试与随机程序生成的设计。关联本练习的“两套实现 + 随机输入 + 已知 bug 对照”，以及第 24 章的 miscompile 二分。
- **PyTorch `torch.permute` 文档**（<https://pytorch.org/docs/stable/generated/torch.permute.html>）：读参数与示例，确认 `permute(*dims)` 与 `np.transpose(a, axes)` 的对应关系。关联 A.1 的语义表。
- **NumPy `transpose` 文档**（<https://numpy.org/doc/stable/reference/generated/numpy.transpose.html>）：读 `axes` 参数与返回值是视图的说明。关联“transpose 不复制数据”的直觉，以及第 11 章的 stride。
- **`code/mini_ai/passes.py` 顶部导入**：读 `from .interpreter import apply_op` 这一行，再对照 A.3 的实跑报错，理解 Python 的导入时绑定。关联第 8 章的常量折叠。

---

下一章回到主线：在扩展后的 IR 上，先建立 verifier 与 shape/dtype 推导的完整认知，再谈优化。

[第 6 章 Verifier（验证器）与 shape/dtype 推导](./06-verifier-and-shape-inference.md)