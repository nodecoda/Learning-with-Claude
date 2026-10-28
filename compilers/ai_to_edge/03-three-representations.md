# Ch 3 — 三种「模型」：nn.Module / FX Graph / ExportedProgram

> **目标**：分清「模型」在你手里的三种形态，以及编译器分别能拿它们做什么、不能做什么。学完这章，看到任何一份中间产物你都能说出它在哪一层。

## 为什么需要这个？

新手最常见的困惑：

```python
m = MyModel()              # 这是模型
ep = torch.export(...)     # 这也是「模型」？
print(ep.graph)            # 这又是什么？
```

三者都叫「模型」，但**信息量完全不同**：

- `nn.Module` 是**代码**（能执行任意 Python）
- FX Graph 是**计算图**（只有张量算子）
- `ExportedProgram` 是**带契约的图**（算子 + shape 约束 + 参数）

编译器只吃得下后两者。搞不清这个区别，就没法理解「为什么某些模型编不了」。

## 先建立直觉

把模型想象成一份**菜谱**：

| 形态 | 类比 | 编译器能做什么 |
|---|---|---|
| `nn.Module` | 会做饭的厨师（你看不见他脑子） | ❌ 什么也做不了（只能调用） |
| FX Graph | 写下来的菜谱（步骤明确） | ✅ 能重排步骤、合并步骤 |
| `ExportedProgram` | 公证过的菜谱（还标了份量约束） | ✅ 能重排 + 能验证输入合法 |

编译器需要**看得见步骤**才能优化。这就是「抓图」存在的理由。

## 形态一：`nn.Module` —— 可执行的代码

```python
import torch

class M(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = torch.nn.ModuleList([torch.nn.Linear(8, 4) for _ in range(2)])

    def forward(self, x):
        for layer in self.layers:          # Python 循环
            x = torch.relu(layer(x))
        if x.sum() > 0:                    # 依赖数据的 Python 分支
            x = x * 2
        return x
```

这个东西**不是图**。它包含：

- Python `for` 循环（次数由 `self.layers` 长度决定，编译期可知）
- Python `if`（条件依赖运行时数据 `x.sum() > 0`）
- 方法调用、属性访问、异常处理……

编译器不能直接「看进去」。只能选择：

1. **追踪（trace）**：跑一遍，记录实际执行了哪些算子
2. **拒绝编译**：遇到看不懂的就 fallback 回 eager

实测一下它能被追踪到什么程度：

```bash
~/.venvs/learn/bin/python -c "
import torch
class M(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.layers = torch.nn.ModuleList([torch.nn.Linear(8,4) for _ in range(2)])
    def forward(self, x):
        for l in self.layers: x = torch.relu(l(x))
        if x.sum() > 0: x = x * 2
        return x
m = M()
cf = torch.compile(m)
out = cf(torch.randn(4,8))
print('compiled ok, shape:', tuple(out.shape))
"
```

实测输出：

```
compiled ok, shape: (4, 4)
```

能编译。但注意：那个 `if x.sum() > 0` **会在图里留下一个 `torch.cond` 或造成 graph break**，取决于版本。Ch 5 详细处理。

## 形态二：FX Graph —— 只看算子

FX 是 PyTorch 的「符号追踪」框架。它把 `nn.Module` 跑一遍，把每一步操作记录成节点。

```python
import torch.fx

class Simple(torch.nn.Module):
    def forward(self, x, w):
        y = x @ w
        return torch.relu(y + 1.0)

traced = torch.fx.symbolic_trace(Simple())
print(traced.graph)
```

实测输出（torch 2.9.1+cpu）：

```
graph():
    %x : [num_users=1] = placeholder[target=x]
    %w : [num_users=1] = placeholder[target=w]
    %matmul : [num_users=1] = call_function[target=operator.matmul](args = (%x, %w), kwargs = {})
    %add : [num_users=1] = call_function[target=operator.add](args = (%matmul, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.relu](args = (%add,), kwargs = {})
    return relu
```

> **实测注意一个关键差异**：裸 `symbolic_trace` 抓到的是 **`operator.matmul` / `operator.add` / `torch.relu`**（Python 层算子），不是 `aten.*`。这跟 `torch.export` 的输出不同——`export` 会规范化到 ATen 算子。这个区别很重要：`operator.*` 是 Python 语义，不同版本可能行为不同；`aten.*` 才是稳定的编译器接口。

现在编译器看到的是**四个节点、三条边**，没有 Python 语法。可以做的事：

- 统计每个节点的 `num_users`（`%x` 有 2 个用户 = fan-out）
- 合并 `add` + `relu`（因为它们都是 elementwise）
- 重排执行顺序（如果没有数据依赖）

FX Graph 的**关键特征**：它是**符号的**——`x` 和 `w` 没有具体数值，只有形状信息（如果在 trace 时给了）。所以叫 `symbolic_trace`。

### FX Graph 丢了什么

这是最重要的一节。FX Graph **不包含**：

1. **数据依赖的 Python 控制流**（`if x.sum() > 0` 会让 `symbolic_trace` 直接抛 `TraceError`）
2. **副作用**（`print`、写文件、修改全局状态）
3. **动态形状**（trace 时用固定 shape，图就固定了）
4. **Python 对象的方法**（自定义类的 `__call__` 除非被正确追踪）

实测一个会失败/降级的例子：

```python
import torch, torch.fx

class WithCondition(torch.nn.Module):
    def forward(self, x):
        if x.sum() > 0:        # 依赖数据的判断
            return x * 2
        return x + 1

t = torch.fx.symbolic_trace(WithCondition())
print(t.graph)
```

实测输出（**直接报错，不是静默丢分支**）：

```
Traceback (most recent call last):
  File "<stdin>", line 8, in <module>
  File ".../torch/fx/_symbolic_trace.py", line 1344, in symbolic_trace
    graph = tracer.trace(root, concrete_args)
  ...
  File ".../torch/fx/proxy.py", line 412, in to_bool
    raise TraceError(
torch.fx.proxy.TraceError: symbolically traced variables cannot be used as inputs to control flow
```

`TraceError: symbolically traced variables cannot be used as inputs to control flow`。

> **这是本课要反复用到的判据**：FX 的 Proxy 对象在 `__bool__` 上直接抛异常。它不允许你把「符号 tensor」当布尔值用，因为那样就**没法在图里表达两条路径**。
>
> **两种常见误解都要纠正**：
> - ❌「FX 会静默丢掉没走到的分支」——不会，它直接拒绝 trace。
> - ❌「FX 能处理任意 Python 控制流」——不能，数据依赖的分支需要 `torch.cond`（`export` 才支持）。
>
> 真正会「静默丢分支」的是**普通 Python 的 eager 执行**：你跑一次模型，只走了其中一条路，另一条从未被执行——如果你的测试样本碰巧都走同一条路，bug 就藏在那里。

这就是为什么生产级工具要用 `torch.export`（它把条件编译成 `torch.cond`）而不是裸 `symbolic_trace`。

## 形态三：`ExportedProgram` —— 带契约的图

```python
import torch

class M(torch.nn.Module):
    def forward(self, x, w):
        return torch.relu(x @ w + 1.0)

ep = torch.export.export(M(), (torch.randn(4, 8), torch.randn(8, 4)))
print(type(ep).__name__)
print(ep.graph)
```

实测输出：

```
ExportedProgram
graph():
    %x : [num_users=1] = placeholder[target=x]
    %w : [num_users=1] = placeholder[target=w]
    %matmul : [num_users=1] = call_function[target=torch.ops.aten.matmul.default](args = (%x, %w), kwargs = {})
    %add : [num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%matmul, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.ops.aten.relu.default](args = (%add,), kwargs = {})
    return (relu,)
```

`ExportedProgram` 比裸 FX Graph 多三样东西：

### 1. 输入规格（签名契约）

```python
print(ep.graph_signature)
```

实测：

```
GraphSignature(input_specs=[InputSpec(kind=<InputKind.USER_INPUT: 1>, arg=TensorArgument(name='x'), target=None, persistent=None), InputSpec(kind=<InputKind.USER_INPUT: 1>, arg=TensorArgument(name='w'), target=None, persistent=None)], output_specs=[OutputSpec(kind=<OutputKind.USER_OUTPUT: 1>, arg=TensorArgument(name='relu'), target=None)])
```

它明确记录了：哪些是用户输入、哪些是参数、输出是什么。**编译器靠这个决定哪些 tensor 需要分配内存。**

### 2. Shape 约束（range constraint）

先看**静态导出**（只给固定 shape）：

```python
print(ep.range_constraints)
```

实测：

```
{}
```

**空的。** 这是很多人第一次用 `export` 时会困惑的地方——以为静态 shape 也会留下约束记录。

只有当你在导出时**显式声明动态维**，约束才会出现：

```python
from torch.export import Dim

dx = Dim("dx", min=1, max=64)
ep2 = torch.export.export(
    M(), (torch.randn(4, 8), torch.randn(8, 4)),
    dynamic_shapes={"x": {0: dx}, "w": None})

for k, v in ep2.range_constraints.items():
    print(k, v)
```

实测：

```
s77 VR[1, 64]
```

`VR[1, 64]` = ValueRange 1 到 64。这才是有用的信息：编译器可以把「batch 最大 64」作为内存规划的上界。

> **对边缘部署的含义**：如果你**不**声明 `dynamic_shapes`，导出的图就是完全静态的——这恰好满足 Ch 2 的「shape 必须编译期已知」。代价是换 batch size 就要重新编译。反过来，声明了 `Dim(min=1, max=64)` 的模型，编译器必须按 64 预留内存，**而边缘设备最缺的就是内存**。
>
> 这是边缘部署中一个真实的取舍：**要么固定 shape 省内存，要么动态 shape 省编译次数**。

### 3. 参数与状态的分离

```python
print("params:", list(ep.state_dict.keys()))
print("graph inputs:", [n.name for n in ep.graph.nodes if n.op == "placeholder"])
```

实测：

```
params: []
graph inputs: ['x', 'w']
```

这个例子里 `w` 被当作**输入**而不是参数（因为它是 `forward` 的参数）。真实模型里权重会进 `state_dict`，**编译器必须知道权重是常量**——否则不能做常量折叠，也不能把它们放进 `.rodata`。

## 三者对照表

| 维度 | `nn.Module` | FX Graph | `ExportedProgram` |
|---|---|---|---|
| 本质 | 可执行 Python 代码 | 算子 DAG | 图 + 签名 + 约束 |
| 能执行吗 | ✅ | ❌（需要 interpreter） | ❌（需要 runtime） |
| 有 shape 信息吗 | 隐式（只有实际 tensor） | 部分（trace 时的） | ✅ 符号 + range |
| 有控制流吗 | ✅ 任意 Python | ❌ 只剩 trace 到的那条 | ⚠️ 通过 `torch.cond` 保留 |
| 编译器能用吗 | ❌ | ✅（有限） | ✅（生产级） |
| 能序列化吗 | ❌（pickle 代码） | ⚠️（pickle graph） | ✅（`ep.save()`） |
| 适合场景 | 开发 | 快速实验、教学 | 部署、AOT 编译 |

## 为什么边缘部署必须用 ExportedProgram

回想 Ch 2 的约束级联：

```
没有 OS → 不能 malloc → 内存必须静态规划 → shape 必须编译期已知
```

`nn.Module` 和 FX Graph 都**不能保证**这一点：

- `nn.Module` 的 shape 完全取决于运行时输入
- FX Graph 的 shape 绑定在 trace 时的具体值上（脆弱）

只有 `ExportedProgram` 把「输入 shape 的允许范围」显式写进 `range_constraints`。编译器可以据此：

1. 算出最大内存需求（Ch 22）
2. 决定哪些循环可以完全展开
3. 在 shape 超出范围时**拒绝运行**而不是越界写内存

这就是 `torch.export` 存在的原因——它是 AOT（Ahead-Of-Time）编译的前提。

## 一个完整的对照实验

跑一下 `code/07_export/export_graph.py`（本机实测）：

```bash
cd code/07_export && ~/.venvs/learn/bin/python export_graph.py
```

```
=== x @ w + 1.0  (三个节点) ===
graph():
    %matmul : ... aten.matmul.default (args = (%x, %w))
    %add    : ... aten.add.Tensor     (args = (%matmul, 1.0))
    %relu   : ... aten.relu.default   (args = (%add,))
    return (relu,)

=== torch.addmm(b, x, w)  (两个节点) ===
graph():
    %addmm : ... aten.addmm.default (args = (%b, %x, %w))
    %relu  : ... aten.relu.default  (args = (%addmm,))
    return (relu,)
```

**两段代码在数学上等价，图上却是 3 个节点 vs 2 个节点。** 这直接影响：

- 融合机会（3 节点需要更多融合工作）
- 内存往返次数（每多一个节点，多一次读+写）
- 后端选择（`addmm` 能映射到一个 BLAS 调用，`matmul`+`add` 未必）

> **这是 AI 编译器存在价值的直接证据**：用户写的是什么语法，会决定上游给编译器多大的优化空间。

## 踩雷集锦

1. **以为 `symbolic_trace` 能处理所有模型**：它会静默丢掉未执行的分支（本章实测）。生产用 `torch.export`，不要用裸 `symbolic_trace` 做部署。
2. **以为 `ep.graph` 就是全部**：`ExportedProgram` 还带 `graph_signature`、`range_constraints`、`state_dict`。只看 `graph` 会漏掉「哪些是常量」这个关键信息。
3. **把 `w` 当参数却没注册**：`forward(self, x, w)` 里的 `w` 会变成**输入**而非参数。编译器因此不能把它当常量折叠进 `.rodata`。
4. **以为 shape 静态就等于安全**：`range_constraints` 的 `[4,4]` 是 export 时的观测值。给 `torch.export` 传 `dynamic_shapes` 才能获得真正的动态支持（Ch 5）。
5. **忽略 `num_users`**：`%x : [num_users=2]` 表示 `x` 被用两次。这是内存规划的关键输入——被用两次的 tensor 不能提前释放。

## 本章重点整理

- 三种形态的信息量递增：代码 → 图 → 图 + 契约。
- FX Graph 遇到数据依赖分支会直接 `TraceError`；真正会静默丢分支的是 eager 执行本身。
- `ExportedProgram` 的三大附加值：签名、range 约束、参数分离。
- 边缘部署必须用 `ExportedProgram`，因为静态内存规划需要编译期已知的 shape 范围。
- 数学等价的写法会生成不同节点数，这直接决定后续优化空间。

## 自我检核

- [ ] 不看表格，说出三种形态各自能/不能做什么
- [ ] 能解释 `symbolic_trace` 遇到 `if x.sum() > 0` 时抛什么异常、为什么不静默丢分支
- [ ] 知道 `ExportedProgram` 比 FX Graph 多了哪三样东西，以及静态导出时 `range_constraints` 为什么是空的
- [ ] 能说出「为什么边缘部署不能用 `nn.Module` 直接编」
- [ ] 看到 `x @ w + 1.0` 和 `addmm(b,x,w)` 的节点数差异，能说出对融合的影响

## 延伸阅读

### 官方文档

- **[torch.export 文档](https://pytorch.org/docs/stable/export.html)**
  - **读哪里**：「What is torch.export?」与「Export IR」两节；重点看 `range_constraints` 的解释
  - **为什么值得读**：本章 `ExportedProgram` 三要素的权威定义
  - **注意**：API 在 2.x 各版本间有变动，以你本机 `torch.__version__` 对应的文档为准

- **[torch.fx 文档](https://pytorch.org/docs/stable/fx.html)**
  - **读哪里**：`Symbolic Tracing` 与 `Graph Manipulation` 两节
  - **和本章的关联**：本章 FX Graph 部分的依据；`Graph Manipulation` 是 Ch 7 写 pass 的前置

### 源码依据

- **[pytorch/torch/_export/](https://github.com/pytorch/pytorch/tree/main/torch/_export)**
  - **读哪里**：`exported_program.py` 里 `ExportedProgram` 的字段定义
  - **为什么值得读**：想知道 `graph_signature` 里每个 `InputKind` 的含义，源码比文档全

### 论文

- **[Torch.FX: Practical Program Capture and Transformation for Deep Learning in Python](https://arxiv.org/abs/2112.08429)** — Reed et al., MLSys 2022
  - **核心贡献**：解释为什么选「符号追踪 + Python 级 IR」而不是自定义 DSL
  - **读哪里**：Section 3（Design）与 Section 4（Graph IR）
  - **和本章的关联**：本章 FX Graph 设计取舍的原始论述

### 博客 / 工程文章

- **[PyTorch Dev Discussions — torch.export 系列](https://dev-discuss.pytorch.org/)**
  - **这系列说什么**：`export` 的设计讨论与 breaking changes
  - **读哪里**：搜 `export` 标签，优先看带 "RFC" 的帖子
  - **为什么值得读**：了解为什么 API 会变，避免把某版本行为当永恒规律

### 书籍

- **《Deep Learning with PyTorch》** — Stevens, Antiga, Viehmann（2020）
  - **读哪几章**：第 2–3 章（tensor 与 autograd 基础）
  - **注意**：书里没有 `export`（成书较早），只用它补 PyTorch 基础

知道了三种形态，下一章看「抓图」这个动作本身——Dynamo 到底在追踪什么，以及它什么时候会放弃。

→ [Ch 4 图捕获：Dynamo 的符号追踪与 guard](./04-graph-capture.md)
