# Ch 5 FX 变换：解释、替换与子图匹配

> **目标**：能在 FX 图上做三种真实改写：用 `Interpreter` 遍历执行、用 `Transformer` 逐节点替换、用 `replace_pattern` 匹配子图；知道改完为什么必须 `recompile()`、`lint()` 在查什么，以及 `num_users` 如何决定一次匹配能不能成立。

## 为什么需要这个？

模型代码是命令式的 Python，图是数据流 DAG。图级优化不能靠改 `forward` 源码：你要在不重新 trace 的情况下，把 `add(x, 1.0)` 改成 `add(x, 2.0)`，把 `relu` 换成 `clamp`，或者把一小段固定模式替换成更便宜的等价实现。FX 提供了三种粒度：

| 方式 | 你在做什么 | 产物 | 典型用途 |
|---|---|---|---|
| `fx.Interpreter` | 按拓扑顺序逐节点执行 | 运行结果 / 分析数据 | 调试、数值对齐、统计节点 |
| `fx.Transformer` | 逐节点重建一张新图 | 新的 `GraphModule` | 精确控制某个 target 的改写 |
| `fx.replace_pattern` | 用一小张图去匹配大图 | 原位替换 + `Match` 列表 | 算子替换、子图重写 |

这三种方式都不是「编译到机器码」，它们只改图。真正的 lowering 在后面的章节。

## 先建立直觉：走一遍、重建一遍、匹配一块

```text
Interpreter:      图 ──逐节点执行──▶ 结果（图不变）
Transformer:      图 ──逐节点重建──▶ 新图（旧图不变）
replace_pattern:  图 ──子图匹配────▶ 原位替换（图变了）
```

`Interpreter` 关心「每个节点算出什么」，`Transformer` 关心「每个节点要不要换一种写法」，`replace_pattern` 关心「哪几个节点连起来长得像我的模板」。先选粒度，再写代码；不要拿 `replace_pattern` 去做需要逐节点状态判断的事，也不要拿 `Transformer` 去做大范围子图匹配。

## 核心概念一：Interpreter 是「走一遍」，不是「改图」

`fx.Interpreter` 的核心是 `run()`：它按 `graph.nodes` 的顺序遍历，把每个节点的结果放进 `self.env`，再把结果传给下游节点。`run_node()` 根据 `node.op` 分派到 `placeholder`、`get_attr`、`call_function`、`call_method`、`call_module`、`output`（源码依据：`torch/fx/interpreter.py:52` 定义 `Interpreter`，`:148` 定义 `run`，`:357` 定义 `call_function`）。

```python
import torch
import torch.fx as fx
import torch.nn as nn

class M(nn.Module):
    def forward(self, x):
        return torch.relu(x + 1.0)

gm = fx.symbolic_trace(M())
torch.manual_seed(0)
x = torch.randn(4)

class CountingInterpreter(fx.Interpreter):
    def run_node(self, n):
        print(f"{n.op:>16} target={str(n.target):<30} users={len(n.users)}")
        return super().run_node(n)

print(CountingInterpreter(gm).run(x))
```

实测输出（本机 torch 2.14.1+cpu；`target` 里的对象地址随进程变化，下面用 `...` 省略）：

```text
      placeholder target=x                              users=1
   call_function target=<built-in function add>         users=1
   call_function target=<built-in method relu ...>      users=1
          output target=output                          users=0
tensor([2.5410, 0.7066, 0.0000, 1.5684])
```

注意两件事。第一，`Interpreter` 执行的是图，不是 `forward` 的 Python 字节码；图里没有的节点它不会执行。第二，`run_node` 的返回值会进入 `env`，所以你可以在这里做常量传播、打印形状、统计 FLOPs，甚至做部分求值（给 `run(..., initial_env=...)` 预填某些节点的值）。它适合分析，不适合产出优化后的图。

## 核心概念二：Transformer 是「逐节点重建」

`fx.Transformer` 继承自 `Interpreter`，但它不执行算子，而是把每个节点重新写进一张新图。你覆盖 `call_function`、`call_method`、`call_module` 等钩子，决定每个节点是原样保留还是换写法；最后 `transform()` 返回新的 `GraphModule`（源码依据：`torch/fx/interpreter.py:518` 定义 `Transformer`，`:641` 定义 `call_function`）。

下面把 `add(x, 1.0)` 改成 `add(x, 2.0)`：

```python
import operator
import torch
import torch.fx as fx
import torch.nn as nn

class M(nn.Module):
    def forward(self, x):
        return torch.relu(x + 1.0)

class AddTwo(fx.Transformer):
    def call_function(self, target, args, kwargs):
        if target is operator.add and len(args) == 2 and args[1] == 1.0:
            args = (args[0], 2.0)
        return super().call_function(target, args, kwargs)

x = torch.randn(4)
gm = fx.symbolic_trace(M())
gm2 = AddTwo(gm).transform()

print("old:", gm.code)
print("new:", gm2.code)
print("maxerr:", (gm2(x) - torch.relu(x + 2.0)).abs().max().item())
```

实测的关键输出：

```text
old: add = x + 1.0
new: add = x + 2.0
maxerr: 0.0
```

这里有两个容易忽略的点。`AddTwo(gm)` 的参数必须是 `GraphModule`，不能直接传 `nn.Module`；传错会得到 `AttributeError: 'M' object has no attribute 'graph'`。另外，`args[1] == 1.0` 只是在匹配 Python 常量，没有检查 `x` 的 dtype；如果 `x` 是整型，`x + 2.0` 会走浮点提升，语义和 `x + 1` 不同。图级改写必须自己对 dtype/shape 负责，FX 不会替你验算。

## 核心概念三：replace_pattern 是「子图匹配」

`replace_pattern(gm, pattern, replacement)` 把 `pattern` 和 `replacement` 都当成小图：先用 `symbolic_trace` 把函数 trace 成 `GraphModule`，再按 use-def 关系在大图里找非重叠匹配。匹配成功后，原匹配子图被删掉，替换子图插到锚点位置；返回的 `Match` 列表告诉你匹配到了哪里（源码依据：`torch/fx/subgraph_rewriter.py:96`）。

把 `relu(x)` 换成 `x.clamp(min=0)`：

```python
import torch
import torch.fx as fx
import torch.nn as nn

class M(nn.Module):
    def forward(self, x):
        return torch.relu(x + 1.0)

def pattern(x):
    return torch.relu(x)

def replacement(x):
    return x.clamp(min=0)

gm = fx.symbolic_trace(M())
matches = fx.replace_pattern(gm, pattern, replacement)
print("matches:", len(matches))
print(gm.code)
```

实测：

```text
matches: 1
def forward(self, x):
    add = x + 1.0;  x = None
    clamp = add.clamp(min = 0);  add = None
    return clamp
```

匹配是按 use-def 关系，不是按变量名；`pattern` 里的 `x` 可以匹配大图里的 `add` 节点。`return` 只按值匹配，所以模式不必延伸到整张图的输出。替换函数的参数个数必须和模式一致，哪怕某个参数在替换里没用到；不一致会直接报错：

```text
AssertionError: Placeholder count mismatch: 1 vs 2
```

这是实测的报错原文。另一个更隐蔽的失败是「目标写错」：`pattern` 用 `torch.nn.functional.relu`，而图里是 `torch.relu`，匹配数是 `0`，图完全不变，也不会抛异常。pattern matcher 只看 target、参数值和 use-def，不检查 dtype/shape，也不做语义等价推断。

### num_users 决定「能不能匹配」

`num_users` 不是一个抽象指标，它直接决定子图匹配的边界。看这个双分支：

```python
class Branch(nn.Module):
    def forward(self, x):
        y = torch.relu(x)
        return y + 1.0, y * 2.0

gm = fx.symbolic_trace(Branch())
y = [n for n in gm.graph.nodes if n.target is torch.relu][0]
print([str(u) for u in y.users])   # ['add', 'mul']
```

`relu` 有 2 个用户。此时用 `pattern(x) = torch.relu(x) + 1.0` 去匹配，结果是 `0` 个匹配，而不是替换掉 `add` 这一支：

```text
matches: 0
after:
    relu = torch.relu(x);  x = None
    add = relu + 1.0
    mul = relu * 2.0;  relu = None
    return (add, mul)
```

原因是匹配到的子图里，`relu` 的输出还被 `mul` 使用；如果直接替换，`mul` 就失去输入。`replace_pattern` 只接受「内部节点没有外部用户」的匹配。把模型改成只有 `add` 一个用户，同样的模式立刻匹配成功：

```text
matches: 1
    clamp = x.clamp(min = 0);  x = None
    add_1 = clamp + 2.0;  clamp = None
```

这就是 `num_users` 的影响：它是图改写的安全边界，不是性能指标。2.14.1 的 `Node` 没有 `num_users` 属性，只有 `users` 槽位；源码里格式化节点时也用 `len(self.users)`（`torch/fx/node.py:664`）。写 pass 时用 `len(node.users)`，不要照抄旧教程的 `node.num_users`。

### SubgraphRewriter 在 2.14.1 里已经不在

老教程常写：

```python
from torch.fx.subgraph_rewriter import SubgraphRewriter
rw = SubgraphRewriter()
rw.register_pattern("relu_to_clamp", pattern, replacement)
rw.apply(gm)
```

本机实测 `torch.fx.subgraph_rewriter` 只导出 `replace_pattern`、`replace_pattern_with_filters`、`Match` 等，没有 `SubgraphRewriter` 类；`fx.SubgraphRewriter` 也不存在。`replace_pattern` 就是它的函数式后继。不要为了「看起来像教程」去伪造一个类，直接用 `replace_pattern`；需要额外过滤条件时用 `replace_pattern_with_filters`（源码依据：同文件 `:226`）。

## 底层机制：recompile、lint 与节点顺序

### 改图不等于改代码

`GraphModule` 在构造时会把 `graph` 生成一份 Python `forward` 源码，并编译成函数。你之后手动改 `graph`（改 `target`、插节点、删节点），这份源码不会自动更新。`gm.recompile()` 才是「从当前 graph 重新生成并编译 forward」的入口（源码依据：`torch/fx/graph_module.py:925`）。

实测一个「忘了 recompile」的坑：

```python
import operator
import torch
import torch.fx as fx
import torch.nn as nn

class M(nn.Module):
    def forward(self, x):
        return torch.relu(x + 1.0)

gm = fx.symbolic_trace(M())
add = [n for n in gm.graph.nodes if n.target is operator.add][0]
add.target = operator.mul

print(gm.code)          # 仍然是 add = x + 1.0
print(gm(x))            # 仍然按旧代码执行
gm.recompile()
print(gm.code)          # 变成 add = x * 1.0
```

关键输出：

```text
before recompile: tensor([2.5410, 0.7066, 0.0000, 1.5684])
after recompile:  tensor([1.5410, 0.0000, 0.0000, 0.5684])
expected relu(x*1): tensor([1.5410, 0.0000, 0.0000, 0.5684])
```

`gm.code` 在 recompile 前仍是旧字符串，但 `gm(x)` 已经暴露了问题：图里是 `mul`，跑出来却是 `add` 的结果。任何手动图手术之后，先 `recompile()`，再验证数值；不要只看 `gm.code` 或 `print(gm.graph)` 就宣布成功。

### lint 查的是图的结构，不是数值

`Graph.lint()` 检查节点归属、拓扑顺序、target 是否存在、`call_module` 是否指向 `nn.Module` 等（源码依据：`torch/fx/graph.py:2610`）。它不检查 shape/dtype，也不保证你的改写语义正确。

```python
g = fx.Graph()
a = g.placeholder("x")
r = g.call_function(torch.relu, (a,))
o = g.output(r)
gm = fx.GraphModule(torch.nn.Module(), g)
gm.graph.lint()          # OK

r.args = (o,)            # 让 relu 使用后面的 output 节点
try:
    gm.graph.lint()
except RuntimeError as e:
    print(str(e).splitlines()[0])
```

实测报错：

```text
Argument 'output' of Node 'relu' was used before it has been defined! Please check that Nodes in the graph are topologically ordered
```

再比如把 `call_function` 的 target 改成字符串：

```text
ValueError: Node relu target not_callable has type str but a Callable is expected
```

节点顺序不是「打印好看」的问题。FX 的代码生成按 `graph.nodes` 顺序逐行生成；一个节点只能引用它前面已经定义的节点。`replace_pattern` 会把替换子图插在锚点位置，所以它天然维护拓扑顺序；你手动 `graph.create_node` 时，必须用 `inserting_before` / `inserting_after` 或保证追加顺序正确。

## 示例一：用 Transformer 改常量并验证

把前面的 `AddTwo` 接到真实模型上，完整脚本见 `.scratch/ai-compiler/ch5/fx_basics.py`。核心验证只有三行：

```python
gm2 = AddTwo(fx.symbolic_trace(M())).transform()
assert torch.allclose(gm2(x), torch.relu(x + 2.0))
print(gm2.code)
```

实测：

```text
def forward(self, x):
    add = x + 2.0;  x = None
    relu = torch.relu(add);  add = None
    return relu

maxerr: 0.0
```

验收标准是「和手算参考对齐」，不是「图里出现了 2.0」。如果输入换成整型，`x + 2.0` 会产生浮点结果；这类改写要在 pass 里显式检查 dtype。

## 示例二：用 replace_pattern 做算子替换

同样，完整脚本见 `.scratch/ai-compiler/ch5/fx_basics.py`。关键是检查匹配数、锚点和改写后的代码：

```python
gm = fx.symbolic_trace(M())
matches = fx.replace_pattern(gm, pattern, replacement)
print(len(matches), [str(m.anchor) for m in matches])
print(gm.code)
```

实测：

```text
1 ['relu']
    add = x + 1.0;  x = None
    clamp = add.clamp(min = 0);  add = None
    return clamp
```

`matches[0].anchor` 是原图里被匹配的 `relu` 节点。替换后节点名变成 `clamp`，下游仍正确接到 `add`。多个非重叠 `relu` 会得到多个 `Match`；重叠匹配只替换拓扑序最靠前的那一个。

## 示例三：匹配失败与 lint 失败

### 失败 1：target 写错，静默无变化

```python
import torch.nn.functional as F

def wrong_pattern(x):
    return F.relu(x)

def wrong_replacement(x):
    return x.clamp(min=0)

gm = fx.symbolic_trace(M())
before = gm.code
matches = fx.replace_pattern(gm, wrong_pattern, wrong_replacement)
print(len(matches))          # 0
print(gm.code == before)     # True
```

实测：

```text
0
True
```

这类失败最危险：没有异常，图也没变，只有 `len(matches) == 0`。生产 pass 应该把「匹配数为 0」当成失败信号记录，而不是默默继续。

### 失败 2：replacement 参数数量不对

```python
def bad_replacement(x, y):
    return x.clamp(min=0)

fx.replace_pattern(fx.symbolic_trace(M()), pattern, bad_replacement)
```

实测：

```text
AssertionError: Placeholder count mismatch: 1 vs 2
```

### 失败 3：lint 抓拓扑错误

前面的 `r.args = (o,)` 例子已经给出真实报错。修复方式是恢复 `r.args = (a,)`，或者用 `graph.node_copy` 的 `arg_transform` 正确重映射节点；不要用「把节点重新 append 一遍」来掩盖顺序问题。

## 对比与取舍

| 方式 | 适合 | 不适合 | 主要风险 |
|---|---|---|---|
| `Interpreter` | 调试、统计、数值对齐、部分求值 | 产出优化图 | 逐节点 Python 开销大 |
| `Transformer` | 按 target/参数精确改写 | 大范围模糊匹配 | 重建整图，容易漏掉分支 |
| `replace_pattern` | 固定子图、算子替换 | 需要 shape/dtype 推理的匹配 | target 必须精确、外部用户会阻塞 |
| 手动改 `graph` | 插入/删除少量节点 | 复杂重写 | 必须 `recompile()` + `lint()` |

一个实用顺序是：先用 `Interpreter` 确认图和数值，再用 `replace_pattern` 做能表达成模板的替换，剩下的边角用 `Transformer` 或手动图手术。每步都跑 `lint()` 和数值对齐；不要等所有 pass 写完再一起调。

## 踩雷集锦

1. **把 `nn.Module` 传给 `Transformer`**：报 `AttributeError: 'M' object has no attribute 'graph'`。先 `symbolic_trace`。
2. **用 `node.num_users`**：2.14.1 没有这个属性；用 `len(node.users)`。
3. **pattern 的 target 写成 `F.relu`**：图里是 `torch.relu` 时匹配数为 0，不报错。
4. **replacement 参数比 pattern 多/少**：直接 `AssertionError: Placeholder count mismatch`。
5. **忽略外部用户**：`relu` 有两个消费者时，包含 `relu` 的子图模式匹配失败；这是正确行为，不是 bug。
6. **手动改图后忘了 `recompile()`**：`gm.code` 和图不一致，运行结果仍是旧代码。
7. **只跑 `lint()` 就认为改写正确**：lint 不检查 shape/dtype/数值；必须跑参考实现。
8. **在 `call_function` 里用 `==` 比较张量常量**：会得到张量布尔值或歧义错误；匹配常量时先确认它是 Python 标量，或用 `torch.equal`。

## 进阶：再往深一层

- **带过滤条件的匹配**：`replace_pattern_with_filters` 可以在匹配后加自定义过滤，适合「只替换 float32 的 relu」这类需求。匹配器本身不做 dtype 推断，过滤条件要自己读 `node.meta` 或 `ShapeProp` 的结果。
- **ShapeProp**：`torch.fx.passes.shape_prop.ShapeProp` 能给节点写入 `meta["tensor_meta"]`，之后你的 pass 才能检查 shape/dtype。Ch 6 的常量折叠也会用到类似的「节点上有没有可用元信息」。
- **部分求值**：给 `Interpreter.run(initial_env={...})` 预填常量节点，可以只执行依赖这些常量的子图；这是常量传播的雏形。
- **pass 顺序**：`replace_pattern` 可能让原本不可达的节点变成死节点；下一步接 DCE（Ch 6），否则图会越改越大。
- **源码阅读**：`torch/fx/subgraph_rewriter.py` 的 `_replace_pattern` 里能看到「锚点、节点映射、重叠匹配」的实际处理；读完后你对 `Match.anchor` 的理解会比看文档更牢。

## 动手练习

### 练习 1：把 `mul(x, 3.0)` 改成 `mul(x, 4.0)`

任务：实现 `MulFour(fx.Transformer)`，只改写第二个参数为 `3.0` 的 `operator.mul`。测试输入 `x = tensor([1., 2., -1.])`，期望输出 `[4., 8., -4.]`。

步骤：
1. 用 `symbolic_trace` 抓一个 `forward` 返回 `x * 3.0` 的模型。
2. 在 `call_function` 里判断 `target is operator.mul` 且 `args[1] == 3.0`。
3. `transform()` 后打印 `gm.code`，再比较 `gm(x)` 与 `x * 4.0`。

<details>
<summary>参考解答</summary>

```python
class MulFour(fx.Transformer):
    def call_function(self, target, args, kwargs):
        if target is operator.mul and len(args) == 2 and args[1] == 3.0:
            args = (args[0], 4.0)
        return super().call_function(target, args, kwargs)

x = torch.tensor([1., 2., -1.])
gm = MulFour(fx.symbolic_trace(M())).transform()
assert torch.allclose(gm(x), x * 4.0)
```

常见卡点：`x * 3.0` 在 FX 里是 `operator.mul`，不是 `torch.mul`；用 `is` 比较 target，不要用字符串。

</details>

### 练习 2：用 Interpreter 统计 `call_function` 数量

任务：继承 `fx.Interpreter`，统计图中 `call_function` 节点数量，并打印每个节点的 target。测试模型包含 `add`、`relu`、`mul` 三个算子，期望计数为 3。

<details>
<summary>参考解答</summary>

```python
class Counter(fx.Interpreter):
    def __init__(self, module):
        super().__init__(module)
        self.n = 0
    def run_node(self, node):
        if node.op == "call_function":
            self.n += 1
        return super().run_node(node)

c = Counter(gm)
c.run(x)
print(c.n)
```

常见卡点：不要覆盖 `run()`；覆盖 `run_node()` 才能保证 `env` 正常填充。

</details>

### 练习 3：解释双分支为什么匹配失败

任务：构造 `y = relu(x); return y + 1, y * 2`，用 `pattern(x) = relu(x) + 1` 匹配，记录 `len(matches)`。然后把 `y * 2` 改成 `relu(x) * 2`（独立分支），再匹配一次，记录结果。

测试用例：`x = tensor([-1., 2.])`。第一次期望 `0`，第二次期望 `1`（或按你的图结构得到 1 个匹配）。

<details>
<summary>参考解答</summary>

第一次 `relu` 有 `add` 和 `mul` 两个用户，模式内部节点有外部用户，匹配器拒绝替换。第二次两个 `relu` 各自只有一个用户，第一个 `relu + 1` 可以匹配。用 `len(n.users)` 检查，不要用 `n.num_users`。

</details>

### 练习 4：制造并修复 lint 错误

任务：构造 `x -> relu -> output` 的图，把 `relu.args` 改成 `(output_node,)`，运行 `gm.graph.lint()` 并记录报错；再恢复正确参数，确认 lint 通过。

<details>
<summary>参考解答</summary>

报错应为 `Argument 'output' of Node 'relu' was used before it has been defined!`。修复不是「再 append 一个 relu」，而是恢复 `relu.args = (x,)`，或用 `graph.inserting_after` 在正确位置插入新节点。

</details>

### 练习 5：验证 recompile 的必要性

任务：抓一张 `x + 1.0 -> relu` 的图，把 `add.target` 改成 `operator.mul`；分别在 `recompile()` 前后调用 `gm(x)`，记录两次输出并解释差异。

<details>
<summary>参考解答</summary>

recompile 前仍按旧的 `add` 代码执行，输出 `relu(x+1)`；recompile 后按新图执行 `relu(x*1)`。实测输入 `[1.5410, -0.2934, -2.1788, 0.5684]` 时，前者是 `[2.5410, 0.7066, 0.0000, 1.5684]`，后者是 `[1.5410, 0.0000, 0.0000, 0.5684]`。

</details>

## 本章重点整理

- `Interpreter` 逐节点执行，适合分析和调试；它不产出新图。
- `Transformer` 逐节点重建，适合按 target/参数精确改写；参数必须是 `GraphModule`。
- `replace_pattern` 按 use-def 匹配子图，target 必须精确，内部节点不能有外部用户。
- `num_users` 是匹配的安全边界；2.14.1 用 `len(node.users)`。
- `GraphModule` 缓存生成代码；手动改图后必须 `recompile()`。
- `Graph.lint()` 只查结构合法性，不查数值和 dtype。
- 任何改写都要用参考实现做数值对齐，并检查匹配数是否为 0。

## 自我检核

- [ ] 我能说出 `Interpreter` 和 `Transformer` 的核心区别
- [ ] 我能写出一个把 `add(x, 1.0)` 改成 `add(x, 2.0)` 的 Transformer
- [ ] 我能解释 `replace_pattern` 为什么按 use-def 而不是变量名匹配
- [ ] 我知道 `num_users` 如何阻止包含外部用户的子图匹配
- [ ] 我知道 2.14.1 里 `SubgraphRewriter` 已不存在，替代 API 是什么
- [ ] 我能复现「改图后忘记 recompile」的错误结果
- [ ] 我能说出 `Graph.lint()` 检查的三类问题
- [ ] 我会在改写后跑参考实现并报告 `maxerr`

## 延伸阅读

- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：读 `Interpreter`、`Transformer`、`replace_pattern` 三节。与本章的关联：三种改写方式的官方定义。
- **`torch/fx/subgraph_rewriter.py`**（本机路径 `.scratch/aic-env/Lib/site-packages/torch/fx/subgraph_rewriter.py`）：读 `_replace_pattern` 的匹配循环和 `Match` 结构。与本章的关联：外部用户、重叠匹配的真实实现。
- **`torch/fx/graph.py` 的 `lint()`**：读 `Graph.lint` 的拓扑检查。与本章的关联：为什么改图后必须验证节点顺序。
- **`torch/fx/node.py`**：读 `users`、`all_input_nodes` 和 `format_node`。与本章的关联：`num_users` 的真实来源。
- **Ch 6 常量折叠**：本章的改写是「换写法」，下一章是「删节点、合并节点」；两章合起来才是一套完整的图级 pass。

---

→ [Ch 6 常量折叠与代数化简](./06-constant-folding.md)
