# 练习 B：在 FX 图上实现 pass pipeline

> **目标**：把 Ch 5–10 的图级优化串成一条可运行的流水线：常量折叠 → 代数化简 → DCE → elementwise 融合；用测试锁住语义，并让流水线跑两遍结果不变。

## 为什么需要这个？

单看每一章，pass 都不难：折叠几个常量、删几个死节点、把 `add + relu + sigmoid` 合成一个模块。真正容易翻车的是**组合**：

- 折叠产生的常量没人清理，DCE 留下 `get_attr`；
- 化简把节点替换成已有 Node，FX 的 `Transformer` 直接抛 `NotImplementedError`；
- 融合从错误的方向遍历，只合了一半；
- 忘了 `recompile()`，图改了但执行的还是旧代码，而且**不报错**。

这个练习的目标不是写出最强的优化器，而是写出一个**顺序正确、语义可验证、跑两遍不变**的 pipeline。能做到这三点，你就具备了往图编译器里加 pass 的基本功。

## 任务规格

### 输入与输出

- 输入：一个 `torch.fx.GraphModule`，由 `symbolic_trace` 或手工 `Graph` 构建。
- 输出：同一个（或新的）`GraphModule`，图已优化、`recompile()` 过、可直接调用。
- 入口函数：`optimize(gm) -> GraphModule`。

### 必须实现的 4 个 pass

| 顺序 | Pass | 职责 | 不能做什么 |
|---:|---|---|---|
| 1 | 常量折叠 | 把所有输入都是常量的纯算子算掉，结果注册为 buffer | 不能折叠依赖 placeholder 的节点 |
| 2 | 代数化简 | `x*1`、`x+0`、`x-0`、`x/1` → `x` | 不能做 `x-x→0`、`x*0→0` 这类对 NaN/Inf 不安全的改写 |
| 3 | DCE | 删除无用户的纯节点，包括失效的 `get_attr` | 不能删 output / placeholder / 有副作用的节点 |
| 4 | elementwise 融合 | 把「二元头 + 一元链」合成一个 `call_module` | 不能跨多用户节点融合，不能改数值语义 |

### 约束

- 不引入新依赖，只用 `torch` / `torch.fx`。
- 融合后的模块必须可被 `GraphModule` 正确注册和调用。
- 流水线必须幂等：连续跑两遍，图的签名与输出一致。
- 数值验收：`torch.allclose(out, ref, atol=1e-6)`。

## 先建立直觉：pass 是图上的重写规则

每个 pass 都在做同一件事：

```text
匹配一个子图模式 → 用等价的新节点替换 → 修正 use-def → 删掉旧节点
```

顺序不是随便排的：

```text
fold  → simplify → DCE → fuse
```

- 先 fold，才能把常量暴露给 simplify；
- 先 simplify，`x*1` 消失后它的上游可能变成死节点，DCE 才有东西可删；
- 最后 fuse，拿到的是最干净的链，模式匹配不会被无用节点干扰。

如果先 fuse 再 simplify，融合模块里会包着 `mul by 1`，既浪费又难匹配。**pass 顺序本身就是优化决策。**

## 测试用例

| # | 用例 | 构造 | 断言 |
|---:|---|---|---|
| 1 | 常量折叠 | 手工 Graph：`x + (c1 @ c2)` | 图中无 `matmul`，只剩 1 个 `get_attr`，输出对齐 |
| 2 | 代数化简 | `x*1 + 0` | 图中无 `call_function`，输出与输入逐元素相等 |
| 3 | DCE | `y = x*3` 但 `y` 未使用 | `mul` 节点被删除 |
| 4 | 融合 | `sigmoid(relu(x + bias))` | 恰好 1 个 `call_module`，无 `relu/sigmoid` 节点 |
| 5 | 幂等性 | 对用例 4 连跑两遍 | 图签名相同，输出相同 |

用例 1 为什么手工建图？因为 `symbolic_trace` 会在 trace 时**直接算掉**纯 Python 常量表达式。你写 `self.c @ self.c`，图里往往只剩一个 `_tensor_constant0`，根本轮不到你的 pass。手工 `Graph` 能稳定造出「`get_attr + matmul`」这个待折叠模式。真实的 ATen / Relax 图里，权重是 `get_attr`，常量折叠才有用武之地。

用例 4 的 `bias` 是 `nn.Parameter`，trace 后会变成 `get_attr`，正好覆盖「常量作为融合头输入」的情况。

## 分步骤实现

### 第 1 步：搭骨架

```python
import operator
import torch
import torch.fx as fx
import torch.nn as nn
from torch.fx import GraphModule, Node

def optimize(gm: GraphModule) -> GraphModule:
    fold_constants(gm)
    simplify(gm)
    dce(gm)
    fuse_elementwise(gm)
    gm.recompile()
    return gm
```

先让四个函数都是空实现，跑通测试骨架，再逐个填。不要一上来写 200 行。

### 第 2 步：常量折叠

两个判断：

1. 这个节点的所有参数都能解析成常量（`get_attr` 或 Python 标量）。
2. 结果必须是 tensor；标量结果先跳过，避免改变图的类型契约。

折叠后不要保留原节点，而是：

```text
register_buffer(name, value) → graph.get_attr(name) → replace_all_uses_with → erase_node
```

`register_buffer` 让常量跟着 `GraphModule` 走，序列化/搬家不会丢；直接用 Python 浮点替换会让下游 `get_attr` 解析失败。

### 第 3 步：代数化简

只做**浮点安全**的恒等式：

| 模式 | 结果 | 安全吗 |
|---|---|---|
| `x * 1` | `x` | 是（`-0.0 * 1` 仍是 `-0.0`） |
| `x + 0` | `x` | 基本安全（`-0.0 + 0.0` 会变 `0.0`，逐位比较会差） |
| `x - 0` | `x` | 是 |
| `x / 1` | `x` | 是 |
| `x - x` | `0` | **否**：`NaN - NaN = NaN`，`Inf - Inf = NaN` |
| `x * 0` | `0` | **否**：`NaN * 0 = NaN`，`Inf * 0 = NaN` |

实现时用 `replace_all_uses_with(lhs)` + `erase_node(node)`，不要走 `Transformer`。原因见「常见卡点」。

### 第 4 步：DCE

从后往前扫：

```python
for node in reversed(list(graph.nodes)):
    if node.op in ("output", "placeholder"):
        continue
    if len(node.users) == 0:
        graph.erase_node(node)
```

倒序很重要：删掉最外层死节点后，它的输入才可能变成新的死节点。正序扫一遍往往删不干净，需要反复迭代。

### 第 5 步：elementwise 融合

模式定义：

```text
binary_head(x, y) → unary_1 → unary_2 → ... → unary_n
```

约束：

- 只有**一个 user** 的中间节点才能被吸进融合体；多用户节点还要被别处读，不能删。
- 从**最外层**（图的后面）往前找，才能一次收完整条链；从里往外会先融掉 `add+relu`，把 `sigmoid` 留下。
- 新节点用 `call_module`，模块注册到 `gm` 上；这样第二遍不会再被当成可融合的 `call_function`，天然幂等。

融合模块的 `forward` 按顺序执行 op 列表：

```python
class FusedPointwise(nn.Module):
    def forward(self, *args):
        if self.has_binary_head:
            out = self.ops[0](*args)
            tail = self.ops[1:]
        else:
            out = args[0]
            tail = self.ops
        for op in tail:
            out = op(out)
        return out
```

注意：这里的「融合」是把链收成一个**融合组**（一个 `call_module`），不是直接生成一个 kernel。真正的循环融合发生在 codegen（Ch 7、Ch 14）；本练习验证的是图结构收缩和语义等价，不是性能。

### 第 6 步：验收

三件事必须同时成立：

1. 数值：`torch.allclose(out, ref, atol=1e-6)`。
2. 结构：目标节点确实消失，融合节点确实出现。
3. 幂等：第二遍 `optimize` 的图签名与第一遍相同。

## 完整参考实现

<details>
<summary>点击展开：passes.py（含 5 个测试，已在本机跑通）</summary>

```python
"""Practice B reference implementation."""
import operator
import torch
import torch.fx as fx
import torch.nn as nn
from torch.fx import GraphModule, Node

FOLDABLE = {
    operator.matmul, operator.add, operator.mul, operator.sub, operator.truediv,
    torch.matmul, torch.add, torch.mul, torch.sub, torch.div,
}
UNARY = {torch.relu, torch.sigmoid, torch.tanh, torch.neg, torch.exp}
BINARY = {operator.add, operator.mul, operator.sub, operator.truediv,
          torch.add, torch.mul, torch.sub, torch.div}


def fold_constants(gm: GraphModule) -> int:
    graph = gm.graph
    env: dict[Node, torch.Tensor] = {}
    folded = 0
    for node in list(graph.nodes):
        if node.op == "get_attr":
            value = getattr(gm, node.target)
            if isinstance(value, torch.Tensor):
                env[node] = value
            continue
        if node.op != "call_function" or node.target not in FOLDABLE:
            continue
        args, ok = [], True
        for arg in node.args:
            if isinstance(arg, Node):
                if arg not in env:
                    ok = False
                    break
                args.append(env[arg])
            elif isinstance(arg, (int, float, bool)):
                args.append(arg)
            else:
                ok = False
                break
        if not ok or not any(isinstance(a, torch.Tensor) for a in args):
            continue
        with torch.no_grad():
            value = node.target(*args)
        if not isinstance(value, torch.Tensor):
            continue
        name = f"_folded_{folded}"
        folded += 1
        gm.register_buffer(name, value)
        with graph.inserting_before(node):
            new_node = graph.get_attr(name)
        node.replace_all_uses_with(new_node)
        graph.erase_node(node)
        env[new_node] = value
    return folded


def simplify(gm: GraphModule) -> int:
    graph = gm.graph
    changed = 0
    for node in list(graph.nodes):
        if node.op != "call_function" or len(node.args) != 2:
            continue
        lhs, rhs = node.args
        if not isinstance(lhs, Node):
            continue
        hit = (
            node.target in (operator.mul, torch.mul) and rhs == 1
            or node.target in (operator.add, torch.add) and rhs == 0
            or node.target in (operator.sub, torch.sub) and rhs == 0
            or node.target in (operator.truediv, torch.div) and rhs == 1
        )
        if hit:
            node.replace_all_uses_with(lhs)
            graph.erase_node(node)
            changed += 1
    return changed


def dce(gm: GraphModule) -> int:
    graph = gm.graph
    removed = 0
    for node in reversed(list(graph.nodes)):
        if node.op in ("output", "placeholder"):
            continue
        if len(node.users) == 0:
            graph.erase_node(node)
            removed += 1
    return removed


class FusedPointwise(nn.Module):
    def __init__(self, ops):
        super().__init__()
        self.ops = list(ops)
        self.has_binary_head = self.ops[0] in BINARY

    def forward(self, *args):
        if self.has_binary_head:
            out = self.ops[0](*args)
            tail = self.ops[1:]
        else:
            out = args[0]
            tail = self.ops
        for op in tail:
            out = op(out)
        return out


def fuse_elementwise(gm: GraphModule) -> int:
    graph = gm.graph
    fused = 0
    used: set[Node] = set()
    for node in reversed(list(graph.nodes)):
        if (node in used or node.op != "call_function"
                or node.target not in UNARY or len(node.args) != 1
                or not isinstance(node.args[0], Node)):
            continue
        chain, cur, head = [node], node, None
        while True:
            prev = cur.args[0]
            if not isinstance(prev, Node):
                break
            if prev.op == "call_function" and prev.target in UNARY and len(prev.users) == 1:
                chain.append(prev)
                cur = prev
                continue
            if prev.op == "call_function" and prev.target in BINARY and len(prev.users) == 1:
                head = prev
            break
        if head is None and len(chain) < 2:
            continue
        ops = ([head.target] if head else []) + [n.target for n in reversed(chain)]
        name = f"_fused_{fused}"
        fused += 1
        gm.add_module(name, FusedPointwise(ops))
        anchor = head if head else chain[-1]
        with graph.inserting_before(anchor):
            new_node = graph.call_module(name, anchor.args, anchor.kwargs)
        node.replace_all_uses_with(new_node)
        for old in chain + ([head] if head else []):
            used.add(old)
            graph.erase_node(old)
    return fused


def optimize(gm: GraphModule) -> GraphModule:
    fold_constants(gm)
    simplify(gm)
    dce(gm)
    fuse_elementwise(gm)
    gm.recompile()
    return gm


def signature(gm: GraphModule):
    return [(n.op, getattr(n.target, "__name__", str(n.target)))
            for n in gm.graph.nodes]


def build_const_graph():
    g = fx.Graph()
    x = g.placeholder("x")
    c1 = g.get_attr("c1")
    c2 = g.get_attr("c2")
    mm = g.call_function(operator.matmul, (c1, c2))
    out = g.call_function(operator.add, (x, mm))
    g.output(out)
    root = nn.Module()
    root.register_buffer("c1", torch.randn(4, 4))
    root.register_buffer("c2", torch.randn(4, 4))
    gm = GraphModule(root, g)
    gm.recompile()
    return gm, root


def test_constant_folding():
    gm, root = build_const_graph()
    x = torch.randn(4, 4)
    with torch.inference_mode():
        ref = x + root.c1 @ root.c2
    optimize(gm)
    assert "matmul" not in str(signature(gm))
    assert [n.op for n in gm.graph.nodes].count("get_attr") == 1
    with torch.inference_mode():
        assert torch.allclose(gm(x), ref, atol=1e-6)
    return len(signature(gm))


class Alg(nn.Module):
    def forward(self, x):
        return x * 1 + 0


class Dead(nn.Module):
    def forward(self, x):
        y = x * 3
        return torch.relu(x)


class Fuse(nn.Module):
    def __init__(self):
        super().__init__()
        self.bias = nn.Parameter(torch.randn(4, 4))

    def forward(self, x):
        return torch.sigmoid(torch.relu(x + self.bias))


def test_algebraic():
    gm = fx.symbolic_trace(Alg()).eval()
    x = torch.randn(4, 4)
    with torch.inference_mode():
        ref = gm(x)
    optimize(gm)
    assert [n for n in gm.graph.nodes if n.op == "call_function"] == []
    with torch.inference_mode():
        assert torch.equal(gm(x), ref)
    return len(signature(gm))


def test_dce():
    gm = fx.symbolic_trace(Dead()).eval()
    optimize(gm)
    assert "mul" not in str(signature(gm))
    return len(signature(gm))


def test_fusion():
    gm = fx.symbolic_trace(Fuse()).eval()
    x = torch.randn(4, 4)
    with torch.inference_mode():
        ref = gm(x)
    optimize(gm)
    assert len([n for n in gm.graph.nodes if n.op == "call_module"]) == 1
    assert not any(n.target in UNARY for n in gm.graph.nodes if n.op == "call_function")
    with torch.inference_mode():
        assert torch.allclose(gm(x), ref, atol=1e-6)
    return len(signature(gm))


def test_idempotent():
    gm = fx.symbolic_trace(Fuse()).eval()
    x = torch.randn(4, 4)
    optimize(gm)
    sig1 = signature(gm)
    with torch.inference_mode():
        out1 = gm(x).clone()
    optimize(gm)
    assert signature(gm) == sig1
    with torch.inference_mode():
        assert torch.allclose(out1, gm(x))
    return len(sig1)


if __name__ == "__main__":
    torch.manual_seed(0)
    for name, fn in [
        ("constant_folding", test_constant_folding),
        ("algebraic", test_algebraic),
        ("dce", test_dce),
        ("fusion", test_fusion),
        ("idempotent", test_idempotent),
    ]:
        print(f"PASS {name:18s} final_nodes={fn()}")

    gm, _ = build_const_graph()
    print("\nconst graph before:", signature(gm))
    optimize(gm)
    print("const graph after :", signature(gm))

    gm2 = fx.symbolic_trace(Fuse()).eval()
    print("\nfuse before:", signature(gm2))
    optimize(gm2)
    print("fuse after :", signature(gm2))
    print(gm2.graph)
```

</details>

## 运行结果

```text
PASS constant_folding   final_nodes=4
PASS algebraic          final_nodes=2
PASS dce                final_nodes=3
PASS fusion             final_nodes=4
PASS idempotent         final_nodes=4

const graph before: [('placeholder', 'x'), ('get_attr', 'c1'), ('get_attr', 'c2'), ('call_function', 'matmul'), ('call_function', 'add'), ('output', 'output')]
const graph after : [('placeholder', 'x'), ('get_attr', '_folded_0'), ('call_function', 'add'), ('output', 'output')]

fuse before: [('placeholder', 'x'), ('get_attr', 'bias'), ('call_function', 'add'), ('call_function', 'relu'), ('call_function', 'sigmoid'), ('output', 'output')]
fuse after : [('placeholder', 'x'), ('get_attr', 'bias'), ('call_module', '_fused_0'), ('output', 'output')]
```

融合后的图像这样：

```text
graph():
    %x : [num_users=1] = placeholder[target=x]
    %bias : [num_users=1] = get_attr[target=bias]
    %_fused_0 : [num_users=1] = call_module[target=_fused_0](args = (%x, %bias), kwargs = {})
    return _fused_0
```

三个结构性结果：

- 常量图从 6 个节点降到 4 个，`matmul` 和两个原始 `get_attr` 都没了。
- `x*1 + 0` 被化简到只剩 placeholder + output（2 个节点）。
- `add + relu + sigmoid` 变成一个 `call_module`；第二遍不再产生新节点。

## 常见卡点

### 1. 遍历方向错了，只融合一半

从前往后遍历时，先遇到 `relu`，会把它和 `add` 融掉，`sigmoid` 留在外面：

```text
fuse before: ... add, relu, sigmoid, output
fuse after : ... call_module(_fused_0), sigmoid, output
```

必须从后往前（`reversed(list(graph.nodes))`），先抓最外层 `sigmoid`，才能把整条链收进来。

### 2. 忘记 `recompile()`，而且不报错

改完图但没 recompile，`gm.code` 仍是旧代码：

```text
def forward(self, x):
    bias = self.bias
    add = x + bias
    relu = torch.relu(add)
    sigmoid = torch.sigmoid(relu)
    return sigmoid
```

此时调用 `gm(x)` **不会报错**，只是继续跑旧图，你的融合等于没做。`gm.graph` 变了、`gm.code` 没变，是最隐蔽的失败模式。每次改图后都调 `gm.recompile()`。

### 3. `get_attr` 与 buffer 的生命周期

常量折叠必须把结果注册成 buffer，再插 `get_attr`：

```python
gm.register_buffer(name, value)
with graph.inserting_before(node):
    new_node = graph.get_attr(name)
```

如果直接 `node.replace_all_uses_with(value)` 会类型错误；如果把常量塞进 Python 闭包，`GraphModule` 重新序列化后就找不到。DCE 还要负责清掉不再被引用的旧 `get_attr`，否则图里会留一堆无用常量。

### 4. `num_users` 不是装饰

融合只能吸收单用户节点：

```python
if prev.op == "call_function" and prev.target in UNARY and len(prev.users) == 1:
    chain.append(prev)
```

如果 `prev` 有两个用户，你把它删了，另一个用户就悬空。这类错误在 `erase_node` 时才会炸出来。

### 5. 删除顺序：从外到内

直接按 `[head] + chain` 删，会先删 `add`，但 `relu` 还在用它：

```text
RuntimeError: Tried to erase Node add but it still had 1 users in the graph: {relu: None}!
```

正确顺序是 `chain + [head]`：先删最外层 `sigmoid`，再 `relu`，最后 `head`。源码依据：`torch/fx/graph.py:1675` 的 `erase_node` 在 1685 行检查 `len(to_erase.users) > 0`，有用户就抛错。

### 6. `Transformer` 不适合「替换成已有 Node」

在 `fx.Transformer.run_node` 里直接 `return lhs`（一个已有 Node），会触发：

```text
NotImplementedError: argument of type: <class 'torch.fx.node.Node'>
```

因为 `Transformer` 会把返回值当成新节点的参数去序列化。替换成已有 Node 这类改写，用 `graph.replace_all_uses_with` + `erase_node` 更直接。`Transformer` 适合「生成新节点」，不适合「指向旧节点」。

### 7. 幂等性靠设计，不靠运气

如果融合生成的是 `call_function`，第二遍它仍可能匹配到融合规则，导致重复包裹。这里用 `call_module`，第二遍的 `FusedPointwise` 不在 `UNARY/BINARY` 集合里，自然不会再次融合。幂等性要在设计 pass 时就考虑，而不是最后补测试。

### 8. 遍历时改图

不要直接 `for node in graph.nodes` 然后 `erase_node`；用 `list(graph.nodes)` 或 `reversed(list(...))` 拿快照。FX 的迭代器在删除节点后行为不可靠，轻则漏节点，重则死循环。

## 自我检核

- [ ] 我能解释四个 pass 为什么必须是 fold → simplify → DCE → fuse
- [ ] 我能手写一个常量折叠，把结果注册成 buffer 并替换 uses
- [ ] 我能说出哪些代数恒等式对 NaN/Inf 不安全
- [ ] 我知道 DCE 为什么要倒序扫，以及为什么要跳过 output/placeholder
- [ ] 我能解释融合为什么必须从最外层开始、只吸收单用户节点
- [ ] 我遇到过（或能复现）「忘记 recompile 但程序不报错」
- [ ] 我能让 pipeline 跑两遍，图签名和输出都不变
- [ ] 我能用 `torch.allclose` 而不是 `==` 做数值验收

## 延伸阅读

- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：读 `Graph`、`Interpreter`、`Transformer` 三节。与本章关联：`replace_all_uses_with`、`erase_node`、`call_module` 的官方语义。
- **PyTorch FX 源码 `torch/fx/graph.py`**（本机路径 `.scratch/aic-env/Lib/site-packages/torch/fx/graph.py`）：读 `erase_node`（1675）、`inserting_before`（1716）、`get_attr`（1799）、`call_module`（1867）。与本章关联：卡点 5 的行号依据。
- **TVM Relax 的 pass 基础设施**（https://tvm.apache.org/docs/）：读 `relax.transform` 下的 `FoldConstant`、`DeadCodeElimination`、`FuseOps`。与本章关联：同一组 pass 在工业编译器里的实现。
- **MLIR Canonicalizer / CSE**（https://mlir.llvm.org/docs/Canonicalization/）：读 pattern rewrite 与 DCE 的通用设计。与本章关联：把本章的手写规则扩展到 pattern-based 重写。

---

→ [Ch 11 循环优化：tiling、reorder、unroll、vectorize](./11-loop-optimizations.md)
