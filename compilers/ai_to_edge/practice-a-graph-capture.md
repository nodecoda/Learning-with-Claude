# 练习 A — 把一张图抓下来并读懂它

> **目标**：从同一个 `nn.Module` 出发，用 `torch.export`、`torch.fx.symbolic_trace`、Dynamo 三条路各抓一次图，回答五个问题：节点数、算子名风格、`range_constraints` 内容、guard 形态、graph break 行号。做完之后，看到任何一张 FX Graph 都能说出「这是哪条路产出的、它还保留了什么、已经丢掉了什么」。

> **环境**：torch 2.9.1+cpu、Python 3.10、x86_64（AVX2）、Ubuntu 22.04。解释器用 `/home/ubuntu/.venvs/learn/bin/python`。

## 背景与动机

Ch 4 讲的是 Dynamo 怎么抓图，Ch 6 讲的是 Inductor 怎么把图变成循环。这两章之间有一个容易被跳过的动作：**把图打印出来，逐节点读懂它**。

这一步不能跳，因为三条路径产出的图**长得不一样**，而这些差异正是后续所有问题的根源：

- `torch.export` 的图带 `aten.*` 名字、带 `range_constraints`、带输入参数标记，给 AOT 编译用。
- `torch.fx.symbolic_trace` 的图带 `operator.*` 名字，没有 shape 信息，给人调试用。
- Dynamo 在 `torch.compile` 内部抓的图带 `f32[4][1]cpu` 形状注解，还会在每个变量用完时插 `= None`，给 Inductor 用。

混为一谈就会遇到这些问题：`export` 是三个节点，为什么 `symbolic_trace` 输出里找不到 `aten.matmul`？写了 `dynamic_shapes`，为什么 `range_constraints` 还是空的？模型能跑，为什么 `torch.compile` 日志里有 graph break？

这个练习就是把三张图并排放，把差异一条条量出来。它出现得很频繁：编译后变慢，先看断成几段；部署失败，先看有没有动态 shape；融合没生效，先数节点数。**不会读图，后面所有调试都是猜。**

## 任务规格

### 输入

自己写一个 `model.py`，包含四个 `nn.Module`：

| 类名 | 内容 | 用途 |
|---|---|---|
| `FusionDemo` | `forward(x) = relu(x @ w + 1.0)`，`w` 为 `Parameter(torch.randn(8, 4))` | 三条路径都能抓图 |
| `TensorBranch` | `y = relu(x @ w + 1.0)`，再由 `if x.sum() > 0:` 选 `y*2` 或 `y*0.5` | `symbolic_trace` 抛异常 |
| `FloatBranch` | 同 `TensorBranch`，条件写成 `s = float(y.sum()); if s > 0:` | Dynamo graph break |
| `FixedNet` | 同 `FloatBranch` 的语义，分支改为 `torch.where(y.sum() > 0, y * 2.0, y * 0.5)` | 第 6 块的改写目标 |

输入统一用 `torch.randn(4, 8)`，脚本开头 `torch.manual_seed(0)`。

### 输出

一个可执行脚本 `inspect_graph.py`，运行后打印报告，满足下面七项要求（下文按输出顺序分成八块讲解，其中 graph break 的日志单独成块）：

1. **`torch.export` 静态导出**：完整 graph、`total_nodes`、`compute_nodes`（`op == "call_function"`）、逐节点 `num_users`、`range_constraints`。
2. **加 `dynamic_shapes` 后的 `range_constraints`**：用 `torch.export.Dim("batch", min=1, max=64)` 标 `x` 第 0 维。
3. **裸 `torch.fx.symbolic_trace` 的图**：完整节点列表与各自 `target`。
4. **失败形态对照**：`TensorBranch` 与 `FloatBranch` 在 `symbolic_trace` 与 `export` 下的异常类型名。
5. **`torch.cond` 别名问题**：复现 `UncapturedHigherOrderOpError`，用 `.clone()` 修好，打印节点数。
6. **无 graph break 版本**：`FixedNet` 用 `torch.where(y.sum() > 0, y * 2.0, y * 0.5)` 替掉 Python 分支，确认 `compute_nodes=8`、`total_nodes=11`、graph break 条数为 0。
7. **`torch.compile` 与 eager 对齐**：`FloatBranch` 与 `FixedNet` 各一次。

### 验收标准

| 编号 | 验收点 | 通过条件 |
|---|---|---|
| A1 | `FusionDemo` 的 `compute_nodes` | 等于 `3`，且三个节点依次是 matmul、add、relu |
| A2 | 静态导出的 `range_constraints` | 等于 `{}` |
| A3 | 加 `dynamic_shapes` 后的 `range_constraints` | 非空，输出的键是 `s` 开头的符号，值形如 `VR[1, 64]` |
| A4 | `symbolic_trace` 的算子名 | 出现 `matmul`、`add`、`relu`，且**不含** `aten` 字样 |
| A5 | `TensorBranch` 的异常类型 | `torch.fx.proxy.TraceError` |
| A6 | `FloatBranch` 在 Dynamo 下的 graph break 行号 | 与源文件里 `float(` 那一行的行号一致 |
| A6b | graph break 的原因字符串 | 含 `Unsupported Tensor.item() call with capture_scalar_outputs=False` |
| A7 | `torch.cond` 的修复 | 修复前抛 `UncapturedHigherOrderOpError`，修复后导出成功 |
| A8 | `max abs diff` | `0.0`（CPU 同精度 elementwise） |
| A9 | `FixedNet` 的无 graph break | 导出成功，`compute_nodes=8`、`total_nodes=11`，`graph_breaks` 日志条数为 0，`max abs diff=0.0` |

### 禁止与限制

- 不要用 `torch.jit.trace` 代替任何一条路径：它记录实际执行的操作，不是符号图，节点数没有可比性。
- 不要把 `range_constraints` 的键（`s77` 这类）硬编码进断言，符号编号每次运行都可能变。
- 不要改 `FusionDemo` 的表达式。`x @ w + 1.0` 就是三个节点；换成 `torch.addmm` 是两个，那属于另一个实验。

## 期望输出范例

以下是本机真实输出（torch 2.9.1+cpu，去掉日志前缀）。

### 第一块：静态导出

```
=== 1. torch.export（静态）===
--- export(FusionDemo) ---
total_nodes=6 compute_nodes=3
  placeholder   p_w                                  users=1
  placeholder   x                                    users=1
  call_function aten.matmul.default                  users=1
  call_function aten.add.Tensor                      users=1
  call_function aten.relu.default                    users=1
  output        output                               users=0
range_constraints={}
```

**`total_nodes` 是 6，`compute_nodes` 是 3**。多出的三个不是计算：两个 `placeholder`（`p_w` 是参数，`x` 是输入）、一个 `output`。不区分 `op` 去数节点，就会得到 6 这个没有意义的数字。`p_w` 的 `p_` 前缀是 `export` 提升参数的标记；`symbolic_trace` 不这么做，它把参数变成 `get_attr` 节点留在图里。

### 第二块：动态 shape

```
=== 2. 同一张图，显式 dynamic_shapes ===
range_constraints={s77: VR[1, 64]}
（符号编号每次运行可能不同，不要写进断言）
```

`VR[1, 64]` 是取值范围（Value Range）。`s77` 是本次运行分配的符号名，换个进程可能是 `s20`、`s21`。

这解释了 Ch 5 的一个困惑：**静态导出不需要 range 约束，因为 shape 全是常量**；只有显式声明「这一维可变」，才会有约束。

### 第三块：`symbolic_trace`

```
=== 3. 裸 torch.fx.symbolic_trace ===
  placeholder   x                                    users=1
  get_attr      w                                    users=1
  call_function matmul                               users=1
  call_function add                                  users=1
  call_function relu                                 users=1
  output        output                               users=0
```

三个 `call_function` 的 `target` 分别是内置的 `operator.matmul`、`operator.add`、`torch.relu`，打印时被压成短名。用 `node.target` 打出来会看到 `<built-in function matmul>` 之类，不是 `torch.ops.aten.matmul.default`。

### 第四块：失败形态

```
=== 4. 失败形态 ===
  TensorBranch symbolic_trace: torch.fx.proxy.TraceError
  FloatBranch  symbolic_trace: builtins.TypeError
  TensorBranch export: GuardOnDataDependentSymNode
  FloatBranch  export: GuardOnDataDependentSymNode
```

`TensorBranch` 走 `symbolic_trace` 时抛 `TraceError`，消息原文：

```
symbolically traced variables cannot be used as inputs to control flow
```

`FloatBranch` 抛 `TypeError`，消息：

```
float() argument must be a string or a real number, not 'Proxy'
```

**同一个「分支」概念，两种异常。**前者是「拿 Proxy 当 if 条件」，后者是「拿 Proxy 去调 `float()`」，修复方向不同。

### 第五块：Dynamo 的 graph break

对 `FloatBranch` 跑 `TORCH_LOGS=graph_breaks`（去掉时间戳与日志前缀）：

```
[__graph_breaks] Graph break in user code at /tmp/finalA/demo_break.py:11
[__graph_breaks] Graph Break Reason: Unsupported Tensor.item() call with capture_scalar_outputs=False
```

`demo_break.py` 第 11 行正是 `s = float(y.sum())`。日志给的是**源文件行号**，不是编译后代码的行号。

Dynamo 还打印了更早一段提示指出修法：

```
Graph break from `Tensor.item()`, consider setting:
    torch._dynamo.config.capture_scalar_outputs = True
```

这是 Ch 4 的开关。打开后 graph break 消失，但图里会出现依赖运行时数值的节点，对 AOT 到边缘设备仍不友好。

### 第六块：`torch.cond` 与 `.clone()`

```
=== 5. torch.cond 修好之后 ===
  compute_nodes=7
  source_nodes=12
```

修好之前 `export` 抛：

```
torch._dynamo.exc.UncapturedHigherOrderOpError
Cond doesn't work unless it is captured completely with torch.compile. Got Encountered aliasing during higher order op tracing
```

关键在最后两个词：**aliasing**（别名）。两个分支直接 `return a`，输入输出是同一块内存，编译器无法证明分支内不会原地改写，于是拒绝捕获。修法写在提示里：

```
Hint: Replace `return input` with `return input.clone()` to avoid aliasing.
```

### 第七块：改写成无 graph break 的版本

把 `FloatBranch` 的 Python 分支换成张量选择：

```python
class FixedNet(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        return torch.where(y.sum() > 0, y * 2.0, y * 0.5)
```

实测（脚本第 6 块输出）：

```
=== 6. 无 graph break 版本 ===
--- export(FixedNet) ---
total_nodes=11 compute_nodes=8
  placeholder   p_w                                  users=1
  placeholder   x                                    users=1
  call_function aten.matmul.default                  users=1
  call_function aten.add.Tensor                      users=1
  call_function aten.relu.default                    users=3
  call_function aten.sum.default                     users=1
  call_function aten.gt.Scalar                       users=1
  call_function aten.mul.Tensor                      users=1
  call_function aten.mul.Tensor                      users=1
  call_function aten.where.self                      users=1
  output        output                               users=0
range_constraints={}
  shifted_to_where=True
```

`relu` 的 `num_users` 是 **3**（`sum`、`mul(*2.0)`、`mul(*0.5)` 各取一次）。这是「一个值被多处使用」在图上的可见形态——练习 B 的融合 pass 遇到这类节点必须放弃删除。

`TORCH_LOGS=graph_breaks` 下 `Graph break in user code` 出现 **0 次**。

代价是节点数从 3 涨到 8：`where` 要求两个分支都算出来，于是 `mul` 出现两次，再各接一次 `sum` / `gt` / `where`。换来「整张图一次编译」，付出「两条路径都执行」。服务器上这个交换常常划算（避免断图调度开销）；MCU 上要重新算，两倍计算量对没有 cache 的核是实打实的。

### 第八块：对齐

```
=== 7. compile 与 eager 对齐 ===
  FloatBranch  max_abs_diff=0.0
  FixedNet     max_abs_diff=0.0
```

这里比的是「同一个模型，compile 过和没 compile 过」。graph break 不影响数值正确性，只影响性能——很多人看到它就以为结果错了。

## 测试用例

第三列是**你必须自己跑出来**的结果，不是答案；对不上时先怀疑自己的模型写错了，再怀疑环境。

| 输入 | 预期输出 | 说明 |
|---|---|---|
| `export(FusionDemo(), (randn(4,8),))` | `compute_nodes=3`，依次 `matmul` / `add` / `relu` | 融合机会的原始形态：三个独立节点，还没被融合 |
| 同上，看 `range_constraints` | `{}` | 静态导出不产生约束 |
| 同上，但 `x` 换成 `randn(16,8)` | 仍然 `compute_nodes=3`，`range_constraints` 仍为 `{}` | 换 batch 不影响静态图；每条路径独立编译一份 |
| 同上，但 `x` 换成 `randn(1,8)` | 仍然 `compute_nodes=3` | 边界：batch=1 是边缘部署的默认形态 |
| 同上，但 `x` 换成 `randn(4,9)` | `RuntimeError`，消息含 `same reduction dim` | **非法输入**：`4x9 @ 8x4` 维度不匹配，导出期就报错 |
| `export(..., dynamic_shapes={"x": {0: Dim("batch",min=1,max=64)}})` | `range_constraints` 非空，值形如 `VR[1, 64]` | 键名（`s77`）每次都变，不要断言具体键 |
| `symbolic_trace(FusionDemo())` | 节点 `target` 是 `operator.matmul` / `operator.add` / `torch.relu` | 看不到任何 `aten` 字样 |
| `symbolic_trace(FusionDemo())` 后数 `get_attr` | 恰好 1 个，名字是 `w` | 参数留在图内，与 `export` 不同 |
| `symbolic_trace(TensorBranch())` | `torch.fx.proxy.TraceError` | 消息：`symbolically traced variables cannot be used as inputs to control flow` |
| `symbolic_trace(FloatBranch())` | `builtins.TypeError` | 消息：`float() argument must be ... not 'Proxy'` |
| `export(TensorBranch(), (x,))` | `torch.fx.experimental.symbolic_shapes.GuardOnDataDependentSymNode` | 导出期无法对数据依赖的值做 guard |
| `export(FloatBranch(), (x,))` | 同为 `GuardOnDataDependentSymNode` | 与上一行同因不同形 |
| `TORCH_LOGS=graph_breaks` + `compile(FloatBranch())` | 日志含 `Graph break in user code at <file>:<line>`，行号指向 `float(` | 原因 `Unsupported Tensor.item() call with capture_scalar_outputs=False` |
| `TORCH_LOGS=guards` + `compile(lambda x: x*2)` | `TENSOR_MATCH` 含 `size=[4, 8], stride=[8, 1]` | guard 记录了 shape 与 stride 两件事 |
| `export(CondAlias(), (x,))` | `torch._dynamo.exc.UncapturedHigherOrderOpError` | 消息含 `aliasing` |
| `export(CondFixed(), (x,))` | 成功，`compute_nodes=7`、`source_nodes=12` | 分支里 `.clone()` 之后别名消失 |
| `compile(FloatBranch())(x)` vs `FloatBranch()(x)` | `max_abs_diff=0.0` | graph break 不改数值 |
| `export(FixedNet(), (x,))` | `compute_nodes=8`、`total_nodes=11` | 分支改张量选择后的完整形态 |
| `TORCH_LOGS=graph_breaks` + `compile(FixedNet())` | 匹配 `Graph break in user code` 的行数为 `0` | 改写成功 |
| `compile(FixedNet())(x)` vs `FixedNet()(x)` | `max_abs_diff=0.0` | 改写不改数值 |

### 关于「非法输入」

这里的「非法输入」指**你给工具的输入违反了它的前提**。形状不匹配（`4x9` 乘 `8x4`）时 `export` 在追踪期做假张量传播，立刻抛 `RuntimeError`，不等运行时；用 `symbolic_trace` 抓带数据依赖分支的模型则直接抛异常，是**硬失败**，而 Dynamo 是「切图继续」。这决定了调试策略：硬失败看异常类型，切图看日志行号。

## 实作步骤建议

### Step 1：把四个模型写进独立的 `model.py`

目标产出：一个能被 import 的文件，含 `FusionDemo` / `TensorBranch` / `FloatBranch` / `FixedNet` 四个类。`FusionDemo.forward` 只写一行 `return torch.relu(x @ self.w + 1.0)`。

先只跑 `FusionDemo` 的静态导出并打印整张 graph。验收：不查文档就能说出 6 个节点里哪三个是计算。

```python
import torch
from model import FusionDemo

torch.manual_seed(0)
ep = torch.export.export(FusionDemo().eval(), (torch.randn(4, 8),))
print(ep.graph)
print(len(ep.graph.nodes))
```

### Step 2：加 `dynamic_shapes`，观察 `range_constraints` 从空变非空

先打印静态版本，再打印动态版本，用一句话解释为什么静态导出不需要 range。再**连跑两次**验证「键名不可硬编码」：两次都是 `s77` 是巧合，换模型结构或导出顺序编号就会变。

### Step 3：换 `symbolic_trace`，把 `target` 打出来

```python
import torch.fx as fx
gm = fx.symbolic_trace(FusionDemo().eval())
for n in gm.graph.nodes:
    print(n.op, n.target, len(n.users))
```

验收：三个 `call_function` 的 `target` 不含 `aten`，且多了一个 `get_attr`。写下观察：`export` 里 `w` 去哪了？

### Step 4：四种失败组合 + graph break 定位

让 `TensorBranch` 与 `FloatBranch` 各走一次 `symbolic_trace`，记下异常类型与消息。再把 `FloatBranch` 交给 `torch.compile`，用 `TORCH_LOGS=graph_breaks` 找断点行号，并**用 `cat -n` 核对那一行确实是 `float(y.sum())`**。

再跑 `TORCH_LOGS=guards` 读 `TENSOR_MATCH` 里的 `size` 与 `stride`：形状相同、布局不同的 tensor，guard 会分开对待。实测对照：

```python
x = torch.randn(4, 8)          # shape (4, 8), stride (8, 1)
y = torch.randn(8, 4).t()      # shape (4, 8), stride (1, 4)
```

两者的 `shape` 都是 `(4, 8)`，`stride` 不同。第二个传进去触发重编译，`TORCH_LOGS=recompiles` 给出的原因原文：

```
- 0/0: tensor 'x' stride mismatch at index 0. expected 8, actual 1
```

报错说的是 **stride mismatch**，不是 size mismatch——这就是 `stride` 字段存在的理由。

### Step 5：改写成无 graph break 版本，再修 `torch.cond`，最后对齐

三件事按这个顺序做：

1. **改写**。新增 `FixedNet`，`forward` 里不出现 Python `if`，用 `torch.where(y.sum() > 0, y * 2.0, y * 0.5)`。导出后确认 `compute_nodes=8`、`total_nodes=11`；再跑 `TORCH_LOGS=graph_breaks`，确认 `Graph break in user code` 为 0 次。
2. **修 `torch.cond`**。先写分支直接 `return a` 的版本，确认抛 `UncapturedHigherOrderOpError`；再写两个分支都 `.clone()` 的版本，确认导出成功。
3. **对齐**。对 `FloatBranch` 与 `FixedNet` 各比一次 `compile` 与 eager 的 `max abs diff`，都应是 `0.0`。

验收不是「跑通了」，而是能回答两个问题：**graph break 存在时数值为什么仍完全一致？**（提示：断点前的图仍被编译，断点后回退 eager，两者数值语义相同。）**节点数从 3 涨到 8 为什么反而是进步？**（提示：目标不是节点数，是「能不能一次编译」。）

## 完整参考解答

**自己写完再看。**下面的代码在本机跑通，输出与上一节「期望输出范例」一致。

<details>
<summary>点开参考实作</summary>

`model.py`：

```python
import torch


class FusionDemo(torch.nn.Module):
    """只有融合机会：matmul + add + relu。"""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        return torch.relu(x @ self.w + 1.0)


class TensorBranch(torch.nn.Module):
    """分支条件是张量布尔值，symbolic_trace 直接抛 TraceError。"""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        if x.sum() > 0:
            return y * 2.0
        return y * 0.5


class FloatBranch(torch.nn.Module):
    """分支条件靠 float() 取真实数值，Dynamo 在这里 graph break。"""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        s = float(y.sum())
        if s > 0:
            y = y * 2.0
        return y


class FixedNet(torch.nn.Module):
    """同 FloatBranch 的语义，分支改成张量选择，不产生 graph break。"""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        return torch.where(y.sum() > 0, y * 2.0, y * 0.5)
```

`inspect_graph.py`：

```python
"""练习 A 参考实作：三条路径抓图 + 一份可核对的报告。

环境：torch 2.9.1+cpu / Python 3.10 / x86_64
运行：~/.venvs/learn/bin/python inspect_graph.py
"""
import torch
import torch.fx as fx
from torch.export import Dim

from model import FixedNet, FloatBranch, FusionDemo, TensorBranch


class CondAlias(torch.nn.Module):
    """torch.cond 的分支直接返回输入，产生别名。"""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        return torch.cond(x.sum() > 0, lambda a: a, lambda a: a, (y,))


class CondFixed(torch.nn.Module):
    """修法：两个分支都返回 clone()。"""

    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        return torch.cond(x.sum() > 0,
                          lambda a: a.clone() * 2.0,
                          lambda a: a.clone() * 0.5,
                          (y,))


def short(node):
    """把 target 压成便于对照的短名，不改变语义。"""
    t = node.target
    if node.op != "call_function":
        return str(t)
    name = getattr(t, "__name__", None)
    if name in ("matmul", "add", "relu", "mul", "sum", "where"):
        return name
    return str(t).replace("torch.ops.aten.", "").replace("torch.ops.higher_order.", "")


def report(ep, title):
    """打印一张 ExportedProgram 的完整体检报告。"""
    nodes = list(ep.graph_module.graph.nodes)
    compute = [n for n in nodes if n.op == "call_function"]
    print(f"--- {title} ---")
    print(f"total_nodes={len(nodes)} compute_nodes={len(compute)}")
    for n in nodes:
        print(f"  {n.op:13s} {short(n):36s} users={len(n.users)}")
    print(f"range_constraints={ep.range_constraints}")
    return ep


def main():
    torch.manual_seed(0)
    x = torch.randn(4, 8)

    print("=== 1. torch.export（静态）===")
    ep = report(torch.export.export(FusionDemo().eval(), (x,)), "export(FusionDemo)")
    # A1 / A2：三个计算节点，且没有 range 约束
    assert len([n for n in ep.graph.nodes if n.op == "call_function"]) == 3
    assert ep.range_constraints == {}

    print()
    print("=== 2. 同一张图，显式 dynamic_shapes ===")
    ep_dyn = torch.export.export(
        FusionDemo().eval(), (x,),
        dynamic_shapes={"x": {0: Dim("batch", min=1, max=64)}},
    )
    print(f"range_constraints={ep_dyn.range_constraints}")
    print("（符号编号每次运行可能不同，不要写进断言）")

    print()
    print("=== 3. 裸 torch.fx.symbolic_trace ===")
    gm = fx.symbolic_trace(FusionDemo().eval())
    for n in gm.graph.nodes:
        print(f"  {n.op:13s} {short(n):36s} users={len(n.users)}")
        # A4：symbolic_trace 的 target 不是 aten 命名空间
        assert "aten" not in str(n.target)

    print()
    print("=== 4. 失败形态 ===")
    for cls in (TensorBranch, FloatBranch):
        try:
            fx.symbolic_trace(cls())
            print(f"  {cls.__name__:12s} symbolic_trace: 通过")
        except Exception as e:
            print(f"  {cls.__name__:12s} symbolic_trace: "
                  f"{type(e).__module__}.{type(e).__name__}")
    for cls in (TensorBranch, FloatBranch):
        try:
            torch.export.export(cls().eval(), (x,))
            print(f"  {cls.__name__:12s} export: 通过")
        except Exception as e:
            print(f"  {cls.__name__:12s} export: {type(e).__name__}")

    print()
    print("=== 5. torch.cond 修好之后 ===")
    ep_cond = torch.export.export(CondFixed().eval(), (x,))
    print(f"  compute_nodes="
          f"{len([n for n in ep_cond.graph.nodes if n.op == 'call_function'])}")
    print(f"  source_nodes={len(list(ep_cond.graph.nodes))}")

    print()
    print("=== 6. 无 graph break 版本 ===")
    ep_fixed = report(torch.export.export(FixedNet().eval(), (x,)), "export(FixedNet)")
    print(f"  shifted_to_where="
          f"{any('where' in str(n.target) for n in ep_fixed.graph.nodes)}")

    print()
    print("=== 7. compile 与 eager 对齐 ===")
    for cls in (FloatBranch, FixedNet):
        m = cls().eval()
        with torch.no_grad():
            compiled = torch.compile(m)(x)
            eager = m(x)
        print(f"  {cls.__name__:12s} max_abs_diff="
              f"{(compiled - eager).abs().max().item()}")


if __name__ == "__main__":
    main()
```

单独定位 graph break 的小脚本（`demo_break.py`，第 11 行是 `float(y.sum())`）：

```python
import torch


class Net(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        y = torch.relu(x @ self.w + 1.0)
        s = float(y.sum())
        if s > 0:
            y = y * 2.0
        return y


if __name__ == "__main__":
    print(torch.compile(Net().eval())(torch.randn(4, 8)).shape)
```

```bash
TORCH_LOGS=graph_breaks /home/ubuntu/.venvs/learn/bin/python demo_break.py
```

**解答说明**：三个设计点。

第一，`report()` 把 `total_nodes` 与 `compute_nodes` 分开数。混着数是 6，而 6 里有两个 `placeholder` 和一个 `output`，不对应任何计算。

第二，`short()` 只影响打印，不参与断言。断言看 `"aten" not in str(n.target)`，`symbolic_trace` 的算子名风格因此被真正**验证**了，而不是被打印格式掩盖。

第三，`CondFixed` 两个分支都加了 `.clone()`。只加一个，另一个仍与输入共享存储。

</details>

## 如果你卡住了

1. **`range_constraints` 一直是 `{}`**：确认传了 `dynamic_shapes=`，且键名是 `"x"` 而不是 `"arg0"`。它按**函数参数名**索引，不按位置；有 `self` 时用除 `self` 外的那个名字。
2. **`symbolic_trace` 的 `target` 看着像 `aten`**：你可能在 `export` 的 `GraphModule` 上又调了一次 `symbolic_trace`，或者把两个脚本的输出贴混了。裸 `symbolic_trace` 给的是 `operator` 命名空间下的内置函数。
3. **graph break 行号对不上源文件**：日志路径是 import 时的路径。用 `-c` 内联执行会变成 `<string>`。把模型放进独立 `.py` 再跑。
4. **`torch.cond` 修完还报 aliasing**：两个分支的 lambda 是否**都**返回了新 tensor？任一侧与输入共享存储就算别名。
5. **`max abs diff` 不是 0**：确认两边是同一个 `eval()` 状态、同一个 `w`、同一个 `x`。每个类各有自己的 `randn(8,4)`，跨模型比没有意义。

## 延伸挑战（加分）

1. **数出 guard 的完整内容**。用 `TORCH_LOGS=guards` 抓 `f(x) = x @ w + 1.0` 的 guard，分类成 size/stride、dtype、dispatch key 三组。再传 `torch.randn(8, 4).t()`，看哪条 guard 先失配。实测 reason 是 `tensor 'x' stride mismatch at index 0. expected 8, actual 1`——注意不是 size mismatch。
2. **对比 `addmm`**。把 `FusionDemo` 改成 `torch.relu(torch.addmm(b, x, w))` 再数节点，实测只有 **2 个**计算节点（`aten.addmm.default` + `aten.relu.default`）。解释 `x @ w + 1.0` 为什么不自动变成它（提示：`addmm` 的 `beta * C` 要矩阵，`1.0` 是标量）。
3. **让 graph break 消失**。对 `FloatBranch` 设 `TORCHDYNAMO_CAPTURE_SCALAR_OUTPUTS=1` 再跑 `TORCH_LOGS=graph_breaks`，确认条数变 0。打印导出图，找出新出现的 `item` 节点，解释它为什么挡住 AOT 到边缘设备。
4. **量 `where` 的代价**。`FixedNet` 用 `where` 消掉 graph break，但 `relu` 的 `num_users` 从 1 变成 3。改成 `y * torch.where(cond, 2.0, 0.5)` 只用一个 `mul`，比较两种写法的 `compute_nodes`，说出你更愿意在 MCU 上跑哪个。
5. **量一次 recompile 代价**。依次用 `(4,8)`、`(16,8)`、`(4,8)` 调 `torch.compile` 过的函数，用 `TORCH_LOGS=recompiles` 数编译次数，解释第三个为什么不触发。

## 自我检核

- [ ] 不看代码，说出 `export` 与 `symbolic_trace` 在**参数表示**上的差异（`placeholder p_w` vs `get_attr w`），以及它对后续 pass 的影响
- [ ] 能解释为什么 `range_constraints` 静态导出是 `{}`，而加上 `dynamic_shapes` 才有内容
- [ ] 能说出 `TensorBranch` 与 `FloatBranch` 在 `symbolic_trace` 下抛**两种不同异常**的原因，而不是只背异常名
- [ ] 不看日志，凭「哪一行代码」就能推断 graph break 的位置
- [ ] 能解释 `torch.cond` 的 aliasing 报错，以及为什么两个分支都要 `.clone()`
- [ ] 能说出 graph break 存在时 `max abs diff` 仍为 `0.0` 的原因
- [ ] 拿到任何一张 FX Graph，能立刻判断它是 `export`、`symbolic_trace` 还是 Dynamo 产出的

图读懂之后，接着看编译器拿这张图做了什么：Inductor 从缓存里的产物到生成的循环，每一步都能反过来验证你今天读到的节点。

→ [Ch 6 TorchInductor 全链路实测](./06-inductor-pipeline.md)
