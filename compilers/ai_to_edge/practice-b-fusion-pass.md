# 练习 B — 手写一个融合 pass

> **目标**：在 FX Graph 上写一个真正能用的融合 pass，把 `add` 后接 `relu` 的两节点模式折叠成一个自定义 `call_function` 节点；用逐位比较证明语义没有改变；再用 Inductor 的真实产物核对「融合到底省了什么」。

> **环境**：torch 2.9.1+cpu、Python 3.10、x86_64（AVX2）、Ubuntu 22.04。解释器用 `/home/ubuntu/.venvs/learn/bin/python`。

## 背景与动机

Ch 7 讲图级优化时，「融合」是第一个出现的词。Ch 6 里看到的 `cpp_fused_add_relu_0` 就是它的产物。但「知道有融合」和「写出一个融合 pass」之间隔着一段距离：

```
知道：add 和 relu 应该合并
    ↓
要回答：怎么在图上找到这个模式？
        找到之后怎么改图？
        改完怎么保证语义没变？
        改完怎么让新节点真的被执行？
```

这四个问题每一个都有具体的坑。第一个坑是模式匹配的边界——`relu(x + y)` 里的 `add` 可以融合，但 `relu(x - y)` 里的 `sub` 不行，名字像不代表语义像。第二个坑是图改写的基本纪律——删节点之前必须确认它没有别的消费者，否则 FX 会直接拒绝。第三个坑是「语义等价」的定义——是数值近似相等，还是逐位相等？这两者的差别在浮点世界里很大。第四个坑最隐蔽——改完图如果不 `recompile()`，你调用的仍然是旧代码。

这个练习把这四件事串起来。做完之后，你再看 `cpp_fused_add_relu_0` 这个名字，就知道它背后是一整套「匹配 → 改写 → 验证 → 重编译」的流程，而不是编译器的一个魔法开关。

## 任务规格

### 输入

一个 Python 文件 `fusion_pass.py`，导出一个函数：

```python
def fuse_add_relu(gm):
    """把 add -> relu 折叠成单个 call_function 节点，返回融合次数。"""
```

`gm` 是 `torch.fx.GraphModule`。调用约定：

- **输入**：一个 `GraphModule`，其图里可能有 `add → relu` 模式，也可能没有。
- **返回值**：整数，表示本次融合了多少处。
- **副作用**：原地修改 `gm.graph`；若有融合发生，必须调用 `gm.recompile()`。
- **幂等**：对同一个 `gm` 连续调用两次，第二次必须返回 `0`。

融合后的节点必须是一个 `call_function`，`target` 是一个普通 Python 函数（不是 `aten` op），语义为 `relu(a + b)`。命名建议 `fused_add_relu`。

### 必须处理的模式

| 图形态 | 期望行为 | 原因 |
|---|---|---|
| `relu(add(x, y))` | 融合，返回 1 | 核心模式 |
| `relu(add(x, 2.0))`，第二参数是标量 | 融合，返回 1 | `add` 的位置参数可以是常量 |
| `relu(add(x, t))`，`t` 是 `get_attr` 张量 | 融合，返回 1 | 常量折叠产出的张量常量也要支持 |
| `relu(relu(add(x,y)) + y)` | 融合 2 次 | 融合后内层模式重新出现，必须能连续匹配 |
| `add` 有两个用户（`relu` 和别处） | **不融合**，返回 0 | 删掉 `add` 会破坏另一个用户 |
| `relu` 有两个用户 | 融合，返回 1 | 替换 `relu` 时所有用户都会被重定向 |
| `relu(x)`，前面没有 `add` | 不融合，返回 0 | 模式不完整 |
| `relu(x - y)` | 不融合，返回 0 | `sub` 不是 `add` |
| `relu_(add(x,y))`（原地版本） | 不融合，返回 0 | `relu_` 会改写输入，语义不同 |
| `relu(torch.add(x, y, alpha=2.0))` | 不融合，返回 0 | `alpha` 让语义变成 `x + 2y`，不是 `x + y` |

### 验收标准

| 编号 | 验收点 | 通过条件 |
|---|---|---|
| B1 | 核心模式融合 | `relu(x @ w + 1.0)` 的图从 6 节点变 5 节点，返回 1 |
| B2 | 语义等价 | 融合前后输出**逐位相等**（`torch.equal` 为 `True`），不是只比 `max abs diff` |
| B3 | 多用户保护 | `add` 有两个用户时返回 0，图完全不变 |
| B4 | 连续模式 | 链式模型返回 2，节点数从 7 降到 5 |
| B5 | 幂等 | 第二次调用返回 0，节点数不再变化 |
| B6 | 图合法 | 融合后 `gm.graph.lint()` 不抛异常 |
| B7 | 原地 relu 不参与 | `relu_` 版本的模型返回 0 |
| B8 | 非法输入 | 传非 `GraphModule`（普通 `nn.Module`）时抛 `AttributeError`，消息含 `graph` |
| B9 | 与 Inductor 对照 | 融合后的语义在 Inductor 里对应 `cpp_fused_add_relu_0` |

### 禁止与限制

- 不要用 `torch.fx.subgraph_rewriter` 或 `torch.fx.experimental.rewriter`。这个练习的目的是手写一遍 `erase_node` / `replace_all_uses_with` / `recompile` 的流程，用现成工具会跳过全部细节。
- 不要用 `torch.compile` 代替你的 pass。`torch.compile` 会在 Python 源码层重新 trace 一遍，你的图改写会被丢掉——这一点后面会实测给你看。
- 不要在 pass 里做任何重结合（reassociation）。`(x*2 + y*3) - z` 改成 `x*2 + (y*3 - z)` 会改变浮点结果，实测不是逐位相等。

## 期望输出范例

### 核心模式：6 节点变 5 节点

模型 `relu(x @ w + 1.0)`，`symbolic_trace` 之后是 6 个节点：

```
before nodes: 6
   placeholder x users= 1
   get_attr w users= 1
   call_function matmul users= 1
   call_function add users= 1
   call_function relu users= 1
   output output users= 0
fused: 1
after nodes: 5
   placeholder x users= 1
   get_attr w users= 1
   call_function matmul users= 1
   call_function fused_add_relu users= 1
   output output users= 0
```

少掉的那个节点是 `relu`——`add` 也被删了，但新节点顶替了它的位置，所以净减 1。

### 逐位相等

```
nodes: 6 -> 5 fused: 1
maxdiff: 0.0 bitwise_equal: True
```

`maxdiff: 0.0` 和 `bitwise_equal: True` 是两件事。前者说「最大差值恰好是 0」，后者说「每一个元素的二进制表示都一样」。在这个例子里两者同时成立，因为融合后的表达式顺序与原图完全一致（先 `a + b`，再 `relu`）。**顺序一变，`maxdiff` 就可能不是 0**，见下一节。

### 重结合会破坏逐位相等

这是本练习最重要的一组对照。同一个表达式，两种结合顺序：

```python
a = torch.relu((x*2 + y*3) - z)     # 左结合
b = torch.relu(x*2 + (y*3 - z))     # 重结合
```

实测：

```
max: 9.5367431640625e-07 n_differ: 36
```

256 个元素里有 36 个不同，最大差值 `9.5367431640625e-07`。这个数不是随便来的：`f32` 在这个量级的相邻两个可表示数之间的间隔（ULP）就是 `2^-20` 量级，而 `9.5367431640625e-07 = 2^-20`。

所以「融合 = 语义不变」这句话必须限定为：**保持运算顺序和结合顺序的融合**。只要 pass 改了结合顺序，它就从「等价改写」变成了「近似改写」，需要单独的数值验收流程（Ch 25 的主题）。

### 幂等：第二次返回 0

```
first: 1 second: 0
nodes: 4
diff: 0.0
```

第一次把 `relu(add(x, y))` 融合成 `fused_add_relu(x, y)`，第二次扫图时 `relu` 已经不存在了，自然匹配不到。节点数 4 是 `x`、`y`、`fused_add_relu`、`output`。

### 多用户保护：pass 主动放弃

`add` 有两个消费者的模型：

```python
def forward(self, x, y):
    s = x + y
    return torch.relu(s) + s
```

实测：

```
MultiUse fused= 0 maxdiff= 0.0
    ('placeholder', 'x')
    ('placeholder', 'y')
    ('call_function', 'add')
    ('call_function', 'relu')
    ('call_function', 'add')
    ('output', 'output')
```

`fused=0`，图一个节点都没动。这不是 pass 失效，是正确的保守行为：`add` 的结果还要给第二个 `add` 用，删掉它会让那个节点悬空。

如果去掉 `num_users` 检查会怎样？FX 直接拒绝：

```
RuntimeError: Tried to erase Node add but it still had 1 users in the graph: {add_1: None}!
```

这条报错是 `erase_node` 自带的保护。**它保护的是「删了会导致悬空引用」，不保护「删了会改变语义」**——后者要你自己判断。

### Inductor 的对照：融合产物长什么样

`relu(x + y)` 交给 `torch.compile`，缓存里生成的函数名是：

```
cpp_fused_add_relu_0
```

签名（实测，两输入）：

```cpp
extern "C"  void  kernel(const float* in_ptr0,
                       const float* in_ptr1,
                       float* out_ptr0)
```

三输入版本 `relu(x + y + z)`：

```cpp
extern "C"  void  kernel(const float* in_ptr0,
                       const float* in_ptr1,
                       const float* in_ptr2,
                       float* out_ptr0)
```

三输入各读一次、只写一次，中间结果不进内存——这就是融合省下的东西。你的 pass 做的事是「图上的节点合并」，Inductor 做的事是「合并之后生成一个循环」。两者是同一个决定的两端。

### 循环宽度与尾部：融合后的代码生成细节

`relu(x*2 + y*3 - z)`，8192 个元素：

```cpp
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(8192L); x0+=static_cast<int64_t>(8L))
```

`8192 / 8 = 1024` 次迭代，正好整除，没有尾部代码。

换成 1001 个元素：

```cpp
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(1001L); x0+=static_cast<int64_t>(8L))
if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(1000L)))
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
if(C10_UNLIKELY(x0 >= static_cast<int64_t>(1000L) && x0 < static_cast<int64_t>(1001L)))
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(1L));
```

1001 不是 8 的倍数，于是多出一整套尾部处理：`C10_UNLIKELY` 分支里用 `loadu(..., 1L)` 一次处理一个元素。

换成 1000 个元素，实测 `C10_UNLIKELY` 出现 **0 次**，只剩主循环：

```cpp
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(1000L); x0+=static_cast<int64_t>(8L))
if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(1000L)))
```

这条对照的意义：**你的 pass 减少的是节点数，Inductor 减少的是循环体里的内存往返。**两个层面都优化到位，才有完整收益。

## 测试用例

| 输入 | 预期输出 | 说明 |
|---|---|---|
| `Basic`：`relu(x @ w + 1.0)` | `fused=1`，节点 `6→5`，`bitwise=True` | 核心模式；`add` 的唯一用户是 `relu` |
| `MultiUse`：`relu(s) + s`，`s = x + y` | `fused=0`，节点 `6→6` | **边界**：`add` 有两个用户，删了会悬空 |
| `ReluOnly`：`relu(x)` | `fused=0`，节点 `3→3` | 模式不完整，只有 `relu` |
| `Chain`：`relu(relu(x + y) + y)` | `fused=2`，节点 `7→5` | 融合内层后外层模式重新成立 |
| `WithAlpha`：`relu(torch.add(x, y, alpha=2.0))` | `fused=0`，节点 `5→5` | **语义陷阱**：`alpha=2` 是 `x + 2y`，不是 `x + y` |
| `WithSub`：`relu(x - y)` | `fused=0`，节点 `5→5` | 名字相似但算子不同 |
| `Inplace`：`relu_(x + y)` | `fused=0`，节点 `5→5` | 原地版本会改写输入，不能当 `relu` 处理 |
| `TensorConst`：`relu(x + tensor(2.0))` | `fused=1`，节点 `5→4` | `add` 的第二参数是 `get_attr` 张量，也要支持 |
| 同一个 `gm` 连续调用两次 | 第二次 `fused=0` | 幂等 |
| `relu(x*2 + y*3 - z)` | `fused=0`，节点 `9→9` | **边界**：`relu` 的输入是 `sub`，不是 `add`。模式比看起来窄 |
| 传入普通 `nn.Module`（非 `GraphModule`） | `AttributeError`，消息含 `graph` | **非法输入**：没有 `.graph` 属性 |
| 手动构造 use-before-def 的图后 `lint()` | `RuntimeError`，消息含 `used before it has been defined` | **非法图**：拓扑序被破坏 |
| 删 `add` 时不检查多用户 | `RuntimeError: Tried to erase Node add but it still had 1 users` | **非法操作**：FX 自带的保护 |
| `torch.compile` 后再看你的 pass 效果 | 融合消失，图里仍有 `add` 与 `relu` | `compile` 重新 trace 源码，看不到手工改图 |

最后一行有实测依据。对已融合的 `GraphModule` 再套一层 `torch.compile`，`TORCH_LOGS=graph_code` 打出来的图是：

```
def forward(self, L_self_parameters_w_: "f32[8, 4][4, 1]cpu", L_x_: "f32[4, 8][8, 1]cpu"):
    matmul: "f32[4, 4][4, 1]cpu" = l_x_ @ l_self_parameters_w_;  l_x_ = l_self_parameters_w_ = None
    add: "f32[4, 4][4, 1]cpu" = matmul + 1.0;  matmul = None
    fused_add_relu: "f32[4, 4][4, 1]cpu" = torch.relu(add);  add = None
```

变量名还叫 `fused_add_relu`，但它绑定的操作是 `torch.relu(add)`——Dynamo 顺着你生成的 `forward` 重新走了一遍，把自定义函数**内联展开**回 `add` 与 `relu`。你是改过图，但改的那张图在 `compile` 眼里只是「可以被再次 trace 的 Python 代码」。

### 关于「逐位相等」这条验收

`max abs diff == 0.0` 与 `torch.equal(...) == True` 在本练习的正常路径上同时成立，但**它们不是同一个断言**：

- `max abs diff` 是「差值数组的最大值」。它可以是 `0.0` 而两边仍有一个元素是 `-0.0` 与 `0.0`（数值相等，位模式不同）。
- `torch.equal` 比较数值，`-0.0 == 0.0` 也为 `True`，所以它同样不检查位模式。
- 真正检查位模式要用 `torch.equal(a.view(torch.int32), b.view(torch.int32))`。本练习不需要这么严格，但你要知道 `0.0` 的语义边界在哪。

对本练习更重要的是反面对照：重结合之后 `max abs diff` 变成 `9.5367431640625e-07`，36/256 个元素不同。**任何改动结合顺序的「融合」都必须重新做数值验收**，不能沿用「融合本身不改数值」这句话。

## 实作步骤建议

### Step 1：把三个核心模型和图打印出来

先建 `models.py`，放三个类——`Basic`（`relu(x @ w + 1.0)`）、`MultiUse`（`add` 有两个用户）、`Chain`（`relu(relu(x+y)+y)`）；Step 5 会把用例扩到八个。对每个类跑 `symbolic_trace`，打印 `op`、`target`、`num_users`。（参考解答把这八个类直接写在 `verify.py` 里，效果相同。）

验收：不看代码就能说出 `Basic` 有 6 个节点、其中 3 个是计算；`MultiUse` 的 `add` 有 2 个用户。

```python
import torch
import torch.fx as fx

gm = fx.symbolic_trace(Basic().eval())
for n in gm.graph.nodes:
    print(n.op, n.target, len(n.users))
```

### Step 2：只写匹配，不改图

先写两个判定函数 `_is_add` / `_is_relu`，然后遍历图，打印「找到了几处 `add → relu`」。这一步不改任何节点，纯观察。

验收：`Basic` 打印 1，`MultiUse` 也打印 1——此时还没加 `num_users` 检查，所以 `MultiUse` 会被误匹配。这正是下一步要修的。

### Step 3：改写，并加上三道保险

加三道检查：`len(src.users) == 1`、`src.kwargs` 与 `node.kwargs` 为空、`len(src.args) == 2`。然后执行改写三件套：

```python
with g.inserting_after(node):
    new = g.call_function(fused_add_relu, tuple(src.args))
node.replace_all_uses_with(new)
g.erase_node(node)
g.erase_node(src)
```

验收：`Basic` 从 6 变 5，`MultiUse` 保持 6 不变。

### Step 4：收尾三件事

`g.eliminate_dead_code()` 清掉因删节点而失去消费者的节点；`g.lint()` 检查拓扑序与引用完整性；`gm.recompile()` 让新图生效。

验收：`lint()` 不抛异常；连续调用两次，第二次返回 0。顺带做一个反向实验——故意不调 `recompile()`，观察自定义函数**根本没被调用**：

```
[3] 未 recompile 调用：
[3] recompile 之后：
  [fused] 被调用
```

### Step 5：三组验证

第一组：逐位比较。`torch.equal(gm(x), ref(x))` 必须是 `True`，单看 `max abs diff` 不够。

第二组：八组边界用例（见「测试用例」一节），每一组都要同时检查「融合次数」与「逐位相等」。

第三组：Inductor 对照。把 `relu(x + y)` 交给 `torch.compile`，从缓存目录 `grep` 出 `cpp_fused_add_relu_0`，核对签名里的输入个数。

## 完整参考解答

**自己写完再看。**下面的代码在本机跑通，输出与上一节「期望输出范例」一致。

<details>
<summary>点开参考实作</summary>

`fusion_pass.py`：

```python
"""练习 B 参考解答：在 FX Graph 上把 add -> relu 融合成一个节点。"""
import torch
import torch.fx as fx


def fused_add_relu(a, b):
    """融合后的语义：relu(a + b)。"""
    return torch.relu(a + b)


def _match(node, targets, names):
    """同时按 target 对象和函数名匹配。

    裸 symbolic_trace 给的是 operator.add / torch.relu（内置函数），
    export 给的是 aten.add.Tensor / aten.relu.default。
    两者都要能认出来。
    """
    if node.op != "call_function":
        return False
    if node.target in targets:
        return True
    return getattr(node.target, "__name__", None) in names


def _is_add(n):
    return _match(n, {torch.add, torch.ops.aten.add.Tensor}, {"add"})


def _is_relu(n):
    # relu_ 是原地版本，会改写输入，语义不同，不参与融合
    return _match(n, {torch.relu, torch.ops.aten.relu.default}, {"relu"})


def fuse_add_relu(gm):
    """把 add -> relu 折叠成单个 call_function 节点，返回融合次数。

    前置条件：gm 必须有 .graph，也就是 GraphModule。
    """
    g = gm.graph
    fused = 0
    for node in list(g.nodes):
        if not _is_relu(node):
            continue
        src = node.args[0] if node.args else None
        if not isinstance(src, fx.Node) or not _is_add(src):
            continue
        if src.kwargs or node.kwargs:
            continue                      # alpha=... 之类，语义不同
        if len(src.args) != 2:
            continue                      # add 需要两个位置参数
        if len(src.users) != 1:
            continue                      # add 还有其他消费者，不能删
        with g.inserting_after(node):
            new = g.call_function(fused_add_relu, tuple(src.args))
        node.replace_all_uses_with(new)
        g.erase_node(node)
        g.erase_node(src)
        fused += 1
    if fused:
        g.eliminate_dead_code()
        g.lint()
        gm.recompile()
    return fused
```

`verify.py`（八组用例，逐位比较）：

```python
"""八组用例：每组同时检查融合次数与逐位相等。"""
import torch
import torch.fx as fx

from fusion_pass import fuse_add_relu


class Basic(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 4))

    def forward(self, x):
        return torch.relu(x @ self.w + 1.0)


class MultiUse(torch.nn.Module):
    def forward(self, x, y):
        s = x + y
        return torch.relu(s) + s


class ReluOnly(torch.nn.Module):
    def forward(self, x):
        return torch.relu(x)


class Chain(torch.nn.Module):
    def forward(self, x, y):
        return torch.relu(torch.relu(x + y) + y)


class WithAlpha(torch.nn.Module):
    def forward(self, x, y):
        return torch.relu(torch.add(x, y, alpha=2.0))


class WithSub(torch.nn.Module):
    def forward(self, x, y):
        return torch.relu(x - y)


class Inplace(torch.nn.Module):
    def forward(self, x, y):
        return torch.relu_(x + y)


class TensorConst(torch.nn.Module):
    def forward(self, x):
        return torch.relu(x + torch.tensor(2.0))


CASES = [
    ("matmul+add+relu", Basic(), (torch.randn(4, 8),), 1),
    ("multi-use add", MultiUse(), (torch.randn(4), torch.randn(4)), 0),
    ("relu only", ReluOnly(), (torch.randn(4),), 0),
    ("chain", Chain(), (torch.randn(4), torch.randn(4)), 2),
    ("alpha kwarg", WithAlpha(), (torch.randn(4), torch.randn(4)), 0),
    ("sub", WithSub(), (torch.randn(4), torch.randn(4)), 0),
    ("inplace relu_", Inplace(), (torch.randn(4), torch.randn(4)), 0),
    ("tensor const", TensorConst(), (torch.randn(4),), 1),
]


def main():
    torch.manual_seed(0)
    for name, mod, args, expect in CASES:
        mod = mod.eval()
        gm = fx.symbolic_trace(mod)
        n0 = len(gm.graph.nodes)
        k = fuse_add_relu(gm)
        k2 = fuse_add_relu(gm)          # 幂等检查
        with torch.no_grad():
            out, ref = gm(*args), mod(*args)
        bit = torch.equal(out, ref)
        ok = (k == expect) and bit and k2 == 0
        print(f"{name:18s} fused={k} expect={expect} again={k2} "
              f"nodes={n0}->{len(gm.graph.nodes)} bitwise={bit} "
              f"{'OK' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
```

本机输出：

```
matmul+add+relu    fused=1 expect=1 again=0 nodes=6->5 bitwise=True OK
multi-use add      fused=0 expect=0 again=0 nodes=6->6 bitwise=True OK
relu only          fused=0 expect=0 again=0 nodes=3->3 bitwise=True OK
chain              fused=2 expect=2 again=0 nodes=7->5 bitwise=True OK
alpha kwarg        fused=0 expect=0 again=0 nodes=5->5 bitwise=True OK
sub                fused=0 expect=0 again=0 nodes=5->5 bitwise=True OK
inplace relu_      fused=0 expect=0 again=0 nodes=5->5 bitwise=True OK
tensor const       fused=1 expect=1 again=0 nodes=5->4 bitwise=True OK
```

在 `export` 出的图上跑同一个 pass（用 `ep.module()` 拿到可调用对象）：

```python
ep = torch.export.export(Basic().eval(), (torch.randn(256, 8),))
gm = ep.module()
print("export compute:", len([n for n in gm.graph.nodes if n.op == "call_function"]))
print("fused:", fuse_add_relu(gm))
print("after compute:", len([n for n in gm.graph.nodes if n.op == "call_function"]))
```

本机输出：

```
export compute: 3
fused: 1
after compute: 2
```

**解答说明**：四个设计点。

第一，`_match` 同时认 `target` 对象与 `__name__`。这不是保险起见——`symbolic_trace` 与 `export` 产出的 `target` 是两类不同的对象，只认一种就会有一半场景匹配不到。

第二，三道保险的顺序是「先检查、后改写」。`src.users != 1` 的检查必须在 `erase_node` 之前，否则 FX 会在 `erase_node` 里抛错，事务性也没有了。

第三，新节点插在 `node` 之后而不是 `src` 之前。插图位置错了会得到 `used before it has been defined` 的 `lint` 错误。

第四，`if fused:` 才收尾。没有融合时调 `recompile()` 是浪费，而且会让「第二次调用返回 0」的语义变得不清晰。

</details>

## 如果你卡住了

1. **`matched` 永远是 0**：先用 `symbolic_trace` 打印每个节点的 `target`。裸 `symbolic_trace` 给出的是 `operator.add` 与 `torch.relu`（内置函数），不是 `aten.add.Tensor`。如果你的 `_is_add` 只判断 `aten`，就会全部匹配失败。稳妥写法是同时判断 `target` 对象与 `__name__`。
2. **`erase_node` 报「still had N users」**：说明你删的节点还有消费者。先 `replace_all_uses_with` 再 `erase_node`，顺序反了必然报错。另外记得检查 `num_users`，多用户的 `add` 不该进融合分支。
3. **图改完了，但调用结果还是旧的**：`GraphModule` 的 `forward` 是生成出来的 Python 代码，改 `graph` 只是改了数据结构。必须调用 `gm.recompile()` 才会重新生成 `forward`。实测：不 `recompile` 时自定义函数根本没被调用，输出仍然由旧代码产生。
4. **`lint()` 报「used before it has been defined」**：新节点插入的位置在它依赖的节点之前。用 `g.inserting_after(node)` 插在 `relu` 之后，而不是插在 `add` 之前。报错原文：`Argument 'add' of Node 'relu_1' was used before it has been defined!`
5. **`torch.compile` 之后融合消失了**：`torch.compile` 会重新 trace 你的 Python 源码，它看不到你手工改过的图。想验证 pass 的效果，要用 `gm(x)` 直接调用 `GraphModule`，或者把 pass 接到 `torch.compile` 的 backend 钩子上——那是 Ch 9 的内容。

## 延伸挑战（加分）

1. **把 pass 接到 `torch.compile` 的 backend 上**。写一个自定义 backend：先调 `torch._dynamo` 默认的图捕获拿到 `GraphModule`，跑你的 `fuse_add_relu`，再交给 `torch._inductor.compile`。用 `TORCH_LOGS=graph_code` 确认融合节点真的进入了 Inductor 的输入图，而不是被重新 trace 掉。
2. **把模式从两节点扩到一条链**。让 pass 能识别 `mul → add → sub → relu` 这条完整链，把它们收进一个节点。注意：链上的 `mul` 有标量参数、`sub` 的操作数顺序不能反。做完后确认逐位相等——这条链的顺序必须保持原样。
3. **加一个「不融合」的反向 pass**。写 `unfuse_add_relu`，把 `fused_add_relu` 拆回 `add` + `relu`。然后验证 `unfuse(fuse(g))` 与 `g` 在节点数上完全一致。这是检验你的融合规则是否丢失信息的直接方法——如果拆不回去，说明融合时丢了参数。
4. **测一次真实的性能差**。用 `time.perf_counter` 对同一个 8192 元素输入跑 1000 次，比较「`gm` 未融合」与「`gm` 融合后」的耗时。注意 FX 的 `GraphModule` 在 Python 层解释执行，融合减少的是 Python 函数调用次数，不是内存往返——所以这里的加速比会远小于 Inductor 的产物。说出这个差距来自哪一层。
5. **审计 `alpha` 的处理**。`torch.add(x, y, alpha=2.0)` 被你的 pass 跳过了，但它其实可以融合成 `relu(x + 2.0*y)`。实现这个扩展，并证明它仍然逐位相等（提示：先把 `alpha * y` 算出来再相加，加法顺序与原 aten 实现一致）。

## 自我检核

- [ ] 不看代码，说出融合 pass 的四步流程（匹配 → 检查 → 改写 → 收尾），以及每一步少做会出什么错
- [ ] 能解释为什么 `src.users != 1` 时必须放弃，而不是「先删了再说」
- [ ] 能说出 `recompile()` 不做会发生什么，以及你实测看到的症状
- [ ] 能解释 `max abs diff == 0.0` 与 `bitwise_equal` 的区别，以及为什么重结合之后前者不再成立
- [ ] 拿到一个新的融合想法（比如 `mul + add`），能说出你要检查哪几个语义前提
- [ ] 能解释为什么 `torch.compile` 看不到你手工改的图，以及正确的验证路径是什么

到这一步，你已经能在图上做局部改写了。但图改写只解决「少几次函数调用」，真正决定边缘设备能不能跑的，是这些节点最终变成什么指令。下一章把 MLIR 降到 LLVM IR，看 `memref` 在 LLVM 里长什么样。

→ [Ch 12 从 MLIR 到 LLVM IR：方言降级的真实样子](./12-mlir-to-llvm-ir.md)
