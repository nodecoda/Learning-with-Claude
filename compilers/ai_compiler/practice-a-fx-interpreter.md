# 练习 A：FX 图解释器与数值对齐

> **目标**：不调用模型的 `forward`，只读 FX Graph，自己实现一个解释器，按拓扑顺序执行 `placeholder/get_attr/call_function/output` 四类节点，支持 `matmul/add/relu/mul/sum`，并用数值对齐验证结果；同时统计节点数、算子分布和死节点。

## 为什么需要这个？

会写 pass 不代表懂图。很多人第一次做图变换时，脑子里没有「执行」的概念：不知道一个节点的输入从哪来、输出到哪去、`num_users` 有什么用。写一个解释器是补上这一课最快的方式。

解释器在真实编译器里也有对应物：

- PyTorch FX 自带的 `Interpreter` 就是按节点顺序执行的参考实现。
- TVM Relax 的 VM 是一个字节码解释器；它按指令执行，算子则落到编译好的 kernel。
- ONNX Runtime 的解释器/EP fallback 也是逐节点执行。

更重要的是，解释器是**语义基准**。你后面写的融合、折叠、DCE 是否正确，最终都要和一个「逐节点执行」的结果对齐。先把这个基准写出来，后面的优化才有验收标准。

## 先建立直觉：执行图 = 拓扑遍历 + value map

一张 FX Graph 是**已经按拓扑序排好的节点链表**（`graph.nodes`）。执行它只需要两样东西：

1. 一个 `env: dict[Node, value]`，保存每个节点算出来的值。
2. 一个循环，按顺序处理每个节点，把结果写进 `env`。

伪代码：

```text
env = {}
for node in graph.nodes:
    if placeholder: env[node] = next(input)
    if get_attr:    env[node] = module 上的属性
    if call_function:
        args = [env[a] for a in node.args]
        env[node] = node.target(*args, **node.kwargs)
    if output:      return env[node.args[0]]
```

没有控制流、没有递归、没有调度——这就是「解释器」和「编译器」的区别：解释器按图走，编译器把图变成另一种东西。

## 核心概念一：四类节点

FX 的节点类型不止四种，但本练习只需要这四种：

| `node.op` | 含义 | 本练习怎么处理 |
|---|---|---|
| `placeholder` | 图的输入 | 按位置从 `run(*args)` 取 |
| `get_attr` | 从 module 取参数/buffer | 按 dotted name 逐级 `getattr` |
| `call_function` | 调用一个函数 | 查 handler 表，递归 materialize 参数 |
| `output` | 返回值 | 返回 `env[args[0]]`，可能是 tuple |

真实图里还会出现 `call_method`（`x.relu()`）、`call_module`（`self.linear(x)`）。本练习只支持 `call_function`，遇到其他类型要报**清晰的错误**，而不是静默返回 `None`。

源码依据：节点类型字段 `op` 的定义和注释在 `torch/fx/node.py:291-292`；四类节点的构造函数在 `torch/fx/graph.py:1766`、`:1799`、`:1959`、`:2424`（torch 2.14.1）。

## 核心概念二：value map 与拓扑顺序

`graph.nodes` 的顺序就是拓扑序：每个节点的输入一定在它之前出现。这是 FX 的保证，也是解释器能一遍扫完的原因。

但「拓扑序」不等于「没有多用户」。一个节点可以被多个消费者使用：

```text
%x : [num_users=2] = placeholder[target=x]
%mul : [num_users=0] = call_function[target=operator.mul](args = (%x, 2.0), kwargs = {})
%mul_1 : [num_users=1] = call_function[target=operator.mul](args = (%x, %scale), kwargs = {})
```

- `%x` 有 2 个用户：两个 `mul`。
- `%mul` 有 0 个用户：它是死节点（算出来没人用）。
- `%mul_1` 有 1 个用户：`add`。

`num_users` 是 DCE、融合、内存规划的基础。注意：**`output` 也算一个 user**。一个节点的结果如果直接返回，它的 `num_users` 至少是 1；统计「图内消费者」时要单独排除 output。

## 核心概念三：数值对齐与错误面

解释器的正确性不能靠「看起来对」。用：

```python
torch.allclose(out, ref, atol=1e-6, rtol=1e-5)
maxerr = (out - ref).abs().max()
```

两个指标一起看：`allclose` 给结论，`maxerr` 给量级。float32 matmul 的 `maxerr` 在 `1e-6` 量级正常；如果到了 `1e-2`，先怀疑实现错了，而不是「精度问题」。

错误面同样要设计：

- 不支持的算子 → 抛出 `UnsupportedOpError`，消息里带 target 和 node name。
- 缺少输入 → 抛出 `ValueError`，消息里带 placeholder 名字。
- 未知 `node.op` → 抛出 `UnsupportedOpError`，不要 fallback 成 `None`。

**一个安静返回错误结果的解释器，比一个会报错的解释器危险得多。**

## 任务规格

实现 `GraphInterpreter`，满足：

1. 支持 `placeholder`、`get_attr`、`call_function`、`output` 四类节点。
2. 支持算子：`operator.matmul`、`operator.add`、`operator.mul`、`torch.relu`、`torch.sum`。
3. `run(*args)` 按顺序接收 placeholder 的输入，返回图输出。
4. 不支持的 `call_function` 抛 `UnsupportedOpError`，消息包含 target、node name 和支持列表。
5. 实现 `graph_stats(gm)`，返回：
   - `num_nodes`：总节点数
   - `by_op`：每种 `node.op` 的数量
   - `by_target`：每个 `call_function` target 的数量
   - `max_users`：最大的 `num_users`
   - `unused`：`num_users == 0` 的 `call_function` 节点名
6. 数值验收：`allclose(atol=1e-6, rtol=1e-5)`。

## 测试用例

### 用例 1：两层 MLP

```python
class MLP(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 8))
        self.b = torch.nn.Parameter(torch.randn(8))

    def forward(self, x):
        return torch.relu(x @ self.w + self.b)
```

期望：`maxerr 0.0`，`allclose True`；`by_op` 含 1 个 placeholder、2 个 get_attr、3 个 call_function、1 个 output。

### 用例 2：混合算子 + 死节点

```python
class Mixed(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.scale = torch.nn.Parameter(torch.randn(8))

    def forward(self, x):
        dead = x * 2.0
        y = x * self.scale
        y = torch.relu(y + 1.0)
        return torch.sum(y, dim=1)
```

期望：输出 shape `(4,)`，`maxerr 0.0`；`unused` 包含 `mul`（死节点）；`x` 的 `num_users=2`。

### 用例 3：不支持的算子

```python
class Unsupported(torch.nn.Module):
    def forward(self, x):
        return torch.sigmoid(x * 2.0)
```

期望：抛 `UnsupportedOpError`，消息类似：

```text
unsupported call_function target: 'sigmoid' (node 'sigmoid'); supported: add, matmul, mul, relu, sum
```

### 用例 4（附加）：缺少输入

对用例 1 调 `run()` 不传参，期望 `ValueError: missing input for placeholder 'x'`。

## 分步骤实现

### 步骤 1：读图，列出节点

先不执行，只打印每个节点的 `op/target/args/users`：

```python
for node in gm.graph.nodes:
    print(node.op, node.target, node.args, len(node.users))
```

确认三件事：placeholder 的顺序、get_attr 的 dotted name、call_function 的 target 是否都在支持列表里。

### 步骤 2：建 value map

`env = {}`，key 是 `Node` 对象，value 是 tensor/标量/参数。用 `Node` 而不是 `node.name` 做 key，避免重名问题。

### 步骤 3：处理 placeholder 与 get_attr

placeholder 按位置取 `args`；get_attr 用 dotted path：

```python
def _resolve_attr(root, dotted):
    obj = root
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj
```

嵌套 module 的 `get_attr` 可能是 `block.weight`，逐级 `getattr` 才能拿到。

### 步骤 4：逐节点执行 call_function

先查 handler，再递归 materialize 参数：

```python
call_args = [_materialize(a, env) for a in node.args]
call_kwargs = {k: _materialize(v, env) for k, v in node.kwargs.items()}
env[node] = fn(*call_args, **call_kwargs)
```

`_materialize` 要处理 `Node`、tuple、list、dict；只处理顶层 Node 会在 `output` 是 tuple 时出错。

### 步骤 5：返回 output 并做数值对齐

`output` 节点的 `args[0]` 可能是单个 Node，也可能是 tuple。统一用 `_materialize` 处理，返回后再和 eager 结果比 `allclose`。

### 步骤 6：统计

`by_op` 用 `Counter(n.op)`；`by_target` 只看 `call_function`；`unused` 只看 `num_users == 0` 的 `call_function`。不要把所有 `num_users=0` 的节点都算成死节点——`output` 本身没有用户。

## 参考解答

<details>
<summary>完整可跑代码（点击展开）</summary>

```python
"""Practice A reference solution: a tiny FX graph interpreter."""
from __future__ import annotations

import operator
from collections import Counter

import torch


def _resolve_attr(root: torch.nn.Module, dotted: str):
    obj = root
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def _materialize(value, env):
    if isinstance(value, torch.fx.Node):
        return env[value]
    if isinstance(value, tuple):
        return tuple(_materialize(v, env) for v in value)
    if isinstance(value, list):
        return [_materialize(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _materialize(v, env) for k, v in value.items()}
    return value


class UnsupportedOpError(NotImplementedError):
    pass


class GraphInterpreter:
    """Execute a traced FX Graph without calling the original forward()."""

    def __init__(self, graph_module: torch.fx.GraphModule):
        self.gm = graph_module
        self.handlers = {
            operator.matmul: lambda a, b: a @ b,
            operator.add: lambda a, b: a + b,
            operator.mul: lambda a, b: a * b,
            torch.relu: torch.relu,
            torch.sum: lambda x, dim=None, **kw: torch.sum(x, dim=dim, **kw),
        }

    def run(self, *args):
        env = {}
        arg_iter = iter(args)

        for node in self.gm.graph.nodes:
            if node.op == "placeholder":
                try:
                    env[node] = next(arg_iter)
                except StopIteration:
                    raise ValueError(
                        f"missing input for placeholder '{node.target}'"
                    ) from None
            elif node.op == "get_attr":
                env[node] = _resolve_attr(self.gm, node.target)
            elif node.op == "call_function":
                fn = self.handlers.get(node.target)
                if fn is None:
                    target = getattr(node.target, "__name__", str(node.target))
                    supported = ", ".join(
                        sorted(getattr(t, "__name__", str(t)) for t in self.handlers)
                    )
                    raise UnsupportedOpError(
                        f"unsupported call_function target: {target!r} "
                        f"(node '{node.name}'); supported: {supported}"
                    )
                call_args = [_materialize(a, env) for a in node.args]
                call_kwargs = {k: _materialize(v, env) for k, v in node.kwargs.items()}
                env[node] = fn(*call_args, **call_kwargs)
            elif node.op == "output":
                return _materialize(node.args[0], env)
            else:
                raise UnsupportedOpError(
                    f"unsupported node op: {node.op!r} (node '{node.name}')"
                )

        raise RuntimeError("graph has no output node")


def graph_stats(graph_module: torch.fx.GraphModule) -> dict:
    nodes = list(graph_module.graph.nodes)
    by_op = Counter(n.op for n in nodes)
    by_target = Counter(
        getattr(n.target, "__name__", str(n.target))
        for n in nodes
        if n.op == "call_function"
    )
    users = Counter()
    for n in nodes:
        users[n.name] = len(n.users)
    return {
        "num_nodes": len(nodes),
        "by_op": dict(by_op),
        "by_target": dict(by_target),
        "max_users": max(users.values(), default=0),
        "unused": [
            n.name
            for n in nodes
            if n.op == "call_function" and users[n.name] == 0
        ],
    }


class MLP(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 8))
        self.b = torch.nn.Parameter(torch.randn(8))

    def forward(self, x):
        return torch.relu(x @ self.w + self.b)


class Mixed(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.scale = torch.nn.Parameter(torch.randn(8))

    def forward(self, x):
        dead = x * 2.0
        y = x * self.scale
        y = torch.relu(y + 1.0)
        return torch.sum(y, dim=1)


class Unsupported(torch.nn.Module):
    def forward(self, x):
        return torch.sigmoid(x * 2.0)


def main():
    torch.manual_seed(0)

    print("=== test 1: MLP ===")
    m1 = MLP().eval()
    x1 = torch.randn(4, 8)
    g1 = torch.fx.symbolic_trace(m1)
    out1 = GraphInterpreter(g1).run(x1)
    with torch.no_grad():
        ref1 = m1(x1)
    print("maxerr", float((out1.detach() - ref1).abs().max()))
    print("allclose", torch.allclose(out1, ref1, atol=1e-6, rtol=1e-5))
    print("stats", graph_stats(g1))

    print("=== test 2: mixed ===")
    m2 = Mixed().eval()
    x2 = torch.randn(4, 8)
    g2 = torch.fx.symbolic_trace(m2)
    out2 = GraphInterpreter(g2).run(x2)
    with torch.no_grad():
        ref2 = m2(x2)
    print("out shape", tuple(out2.shape), "maxerr", float((out2.detach() - ref2).abs().max()))
    print("allclose", torch.allclose(out2, ref2, atol=1e-6, rtol=1e-5))
    print("graph", g2.graph)
    print("stats", graph_stats(g2))

    print("=== test 3: unsupported ===")
    m3 = Unsupported().eval()
    x3 = torch.randn(2, 8)
    g3 = torch.fx.symbolic_trace(m3)
    try:
        GraphInterpreter(g3).run(x3)
    except UnsupportedOpError as e:
        print("caught", type(e).__name__)
        print(e)

    print("=== test 4: missing input ===")
    try:
        GraphInterpreter(g1).run()
    except ValueError as e:
        print("caught", type(e).__name__, e)


if __name__ == "__main__":
    main()
```
</details>

实测输出（Python 3.11.12，torch 2.14.1+cpu）：

```text
=== test 1: MLP ===
maxerr 0.0
allclose True
stats {'num_nodes': 7, 'by_op': {'placeholder': 1, 'get_attr': 2, 'call_function': 3, 'output': 1}, 'by_target': {'matmul': 1, 'add': 1, 'relu': 1}, 'max_users': 1, 'unused': []}
=== test 2: mixed ===
out shape (4,) maxerr 0.0
allclose True
graph graph():
    %x : [num_users=2] = placeholder[target=x]
    %mul : [num_users=0] = call_function[target=operator.mul](args = (%x, 2.0), kwargs = {})
    %scale : [num_users=1] = get_attr[target=scale]
    %mul_1 : [num_users=1] = call_function[target=operator.mul](args = (%x, %scale), kwargs = {})
    %add : [num_users=1] = call_function[target=operator.add](args = (%mul_1, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.relu](args = (%add,), kwargs = {})
    %sum_1 : [num_users=1] = call_function[target=torch.sum](args = (%relu,), kwargs = {dim: 1})
    return sum_1
stats {'num_nodes': 8, 'by_op': {'placeholder': 1, 'call_function': 5, 'get_attr': 1, 'output': 1}, 'by_target': {'mul': 2, 'add': 1, 'relu': 1, 'sum': 1}, 'max_users': 2, 'unused': ['mul']}
=== test 3: unsupported ===
caught UnsupportedOpError
unsupported call_function target: 'sigmoid' (node 'sigmoid'); supported: add, matmul, mul, relu, sum
=== test 4: missing input ===
caught ValueError missing input for placeholder 'x'
```

三个值得注意的数字：

- 用例 1 的 `maxerr=0.0`：解释器执行的就是同一批 ATen 算子，运算顺序完全一致。
- 用例 2 的 `unused=['mul']`：`dead = x * 2.0` 被 `symbolic_trace` 记录下来了，但没人用，`num_users=0`。
- 用例 2 的 `max_users=2`：`x` 同时喂给两个 `mul`；这就是融合/内存规划时要看的「多用户」。

## 常见卡点

1. **参数不在输入里**：`self.w`/`self.b` 是 `get_attr`，不是 `placeholder`。`run(x)` 只传一个参数，别把权重也塞进去。
2. **dotted name 解析错**：嵌套 module 的 `get_attr` 可能是 `block.weight`，用 `getattr` 逐级解析，别直接 `gm.block.weight` 拼字符串。
3. **只 materialize 顶层 Node**：`output` 的 `args[0]` 可能是 tuple（多输出），`node.args` 里也可能嵌套 tuple。写递归 `_materialize`。
4. **`num_users` 把 output 算进去**：`len(node.users)` 包含 output 节点；统计「图内消费者」时要排除 output，统计「是否死节点」时不能只看 `num_users==0` 的 call_function 之外的东西。
5. **dtype 不匹配**：`x` 是 float32、`w` 是 float64 时，`x @ w` 会报 `expected m1 and m2 to have the same dtype, but got: float != double`；解释器不负责帮你 cast。
6. **`torch.sum` 的 dim 在 kwargs 里**：FX 图里是 `kwargs={dim: 1}`，handler 要接受 `dim=None` 关键字；只写 `lambda x: x.sum()` 会丢参数。
7. **`call_method`/`call_module` 没覆盖**：`x.relu()` 是 `call_method`，`self.linear(x)` 是 `call_module`；本练习只支持 `call_function`，遇到要报错而不是猜。
8. **原地修改没建模**：解释器直接调用 `node.target`，如果图里有 `add_` 这类 in-place 算子，会改到输入；练习范围外，但要知道这是解释器的语义盲区。
9. **把 dtype 提升当成跨后端通用规则**：PyTorch 里 `int64 + float32` 会提升成 `float32`，但这是 torch 算子的行为；TVM/ONNX 不会自动提升，跨后端前要显式 cast。

## 自我检核

- [ ] 我能不看代码写出「拓扑遍历 + value map」的伪代码
- [ ] 我能解释 `placeholder`/`get_attr`/`call_function`/`output` 各自的取值方式
- [ ] 我能处理 `get_attr` 的 dotted name
- [ ] 我能递归 materialize tuple/list/dict 里的 Node
- [ ] 我能让不支持的算子报出 target + node name
- [ ] 我能解释 `num_users=0` 和 `unused` 的关系
- [ ] 我用 `allclose` 而不是 `==` 做数值验收
- [ ] 我知道 `torch.sum` 的 `dim` 可能出现在 kwargs 里
- [ ] 我知道本练习不支持 `call_method`/`call_module`，遇到会报错

## 延伸阅读

- **PyTorch FX `Interpreter` 源码**（https://github.com/pytorch/pytorch/blob/main/torch/fx/interpreter.py）：看官方如何用 `run_node` 分派四类节点。与本章关联：参考实现。
- **PyTorch FX `Graph`/`Node` 文档**（https://pytorch.org/docs/stable/fx.html）：读 `nodes`、`users`、`args`、`kwargs` 的语义。与本章关联：数据结构。
- **TVM Relax VM**（https://tvm.apache.org/docs/relax/）：看 Relax 如何把图变成指令再解释执行。与本章关联：解释器在生产编译器里的形态。
- **ONNX Runtime 执行流程**（https://onnxruntime.ai/docs/）：看 EP 如何逐节点/逐子图执行。与本章关联：runtime 层的解释器。

---

→ [Ch 5 FX 变换：Interpreter、Transformer 与图重写](./05-fx-transformations.md)