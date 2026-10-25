# Ch 6 常量折叠与代数化简：把图变小，而不是变魔术

> **目标**：在 FX 图上实现四个基础 pass：常量折叠、代数化简、死代码消除（DCE）、公共子表达式消除（CSE）；能解释每个 pass 的语义前提，知道 `x * 0` 为什么不能随便折，并用真实前后图和数值对齐证明改写没有改变结果。

## 为什么需要这个？

图级优化的第一反应通常是「融合」，但融合之前还有更便宜的一类收益：把编译期已知的计算提前算掉，把无用的节点删掉，把重复的表达式合并。它们不改变算子边界，却能让图更小、后续 pass 更容易匹配，也能直接减少运行时工作量。

这一章的四件事经常一起出现：

| pass | 做什么 | 典型收益 | 主要风险 |
|---|---|---|---|
| 常量折叠 | 两个常量参与 `add`/`mul` 时，编译期算出结果 | 少一次运行、少一个常量节点 | 大常量、dtype 提升、编译期变慢 |
| 代数化简 | `x*1`、`x+0` 这种恒等式 | 直接删节点 | `x*0` 在 NaN/Inf 下不成立 |
| DCE | 删除没有用户的非 output 节点 | 清理死分支、缩小图 | 误删有副作用的节点 |
| CSE | 合并 target 和参数相同的节点 | 减少重复计算 | 参数不可哈希、顺序敏感 |

四个 pass 共享一个前提：**改写必须保持可观察语义**。不是「数学上相等」就够了，还要考虑浮点特殊值、dtype、副作用和图的结构。

## 先建立直觉：四种「删或合」

```text
常量折叠：  c1 + c2            ->  c3
代数化简：  x * 1              ->  x
DCE：       dead = x + 1       ->  （没有用户，删掉）
CSE：       a = x + 1; b = x + 1  ->  a = x + 1; b 复用 a
```

前两个是「算掉/化简一个节点」，后两个是「删除/合并节点」。它们都不需要知道 kernel 怎么调度，只依赖图上的 use-def 关系和少量常量信息。

## 核心概念一：常量折叠是「编译期求值」

在 FX 图里，常量通常以两种形式出现：`get_attr` 节点（模块上的 buffer/parameter）和 `call_function` 参数里的 Python 标量。折叠的逻辑很直接：

1. 如果一个 `call_function` 节点的所有参数都能求出常量值；
2. 且 target 是纯函数（这里只允许 `add`/`mul`/`sub`）；
3. 就调用 `target(*args)` 算出结果；
4. 把结果注册成新的 buffer，用 `get_attr` 节点替换原节点；
5. 删掉原节点，让 DCE 清理不再被使用的常量。

注意第 4 步：不要直接把结果塞进 `node.args` 当 Python 常量。大张量会变成巨大的字面量，代码生成、序列化和设备迁移都会变麻烦；注册成 buffer 更接近真实编译器的做法。

嵌套常量需要多轮或工作列表。比如 `(c1 + c2) + c3`：第一轮只能折 `c1 + c2`，第二轮才能折 `_folded_0 + c3`。如果只跑一遍，图里会留下一个「已经部分折叠」的中间节点。收敛条件是「这一轮没有任何 pass 改动图」。

## 核心概念二：代数化简只认安全的恒等式

最安全的两条是：

```text
x * 1  ->  x
x + 0  ->  x
```

`x * 1` 对 NaN/Inf 也成立：`NaN * 1 = NaN`，`Inf * 1 = Inf`，`-0.0 * 1 = -0.0`。`x + 0` 在数值上成立，但对有符号零不保真：`-0.0 + 0.0 = +0.0`。如果下游用 `signbit` 或位模式做判断，这就是可观察差异。

`x * 0` 看起来更简单，但对浮点不成立：

```python
xs = torch.tensor([float("nan"), float("inf"), -float("inf"), 2.0])
print(xs * 0.0)   # tensor([nan, nan, nan, 0.])
```

实测输出：

```text
xs       : tensor([nan, inf, -inf, 2.])
xs * 0.0 : tensor([nan, nan, nan, 0.])
xs * 1.0 : tensor([nan, inf, -inf, 2.])
xs + 0.0 : tensor([nan, inf, -inf, 2.])
neg zero + 0: tensor([0.]) signbit tensor([False])
```

所以本章的 pass 明确不折 `x * 0`。只有在你能用范围分析证明 `x` 不含 NaN/Inf，或者 `x` 是整数 dtype 时，才能把它换成 `zeros_like(x)`。「整数安全」也不是无条件的：整数乘法可能溢出，但 `x * 0` 的结果仍然是 0；真正的风险在浮点特殊值和有符号零。

## 核心概念三：DCE 看用户数，CSE 看结构键

DCE 的规则是：一个节点不是 `output`，且 `len(node.users) == 0`，就删除它。标准实现从图末尾往前扫：删除一个死节点后，它的输入可能也变成死节点，倒序能在一轮里连锁清理；本章代码用「反复扫描直到没有死节点」实现同样的效果，只是可能多跑几轮。FX 自带的 `Graph.eliminate_dead_code()` 也是这个思路（源码依据：`torch/fx/graph.py:2690`）。

但「没有用户」不等于「可以删」。`call_function` 在 FX 里默认被视为纯函数，实际可能包着 `print`、写文件、改全局状态。生产 pass 应该只对已知纯算子做 DCE，或者维护一份副作用白名单；本章的图只含算术节点，所以可以直接删。

CSE 的键至少包含 `op`、`target`、`args`、`kwargs`。两个 `x + 1` 节点键相同，可以合并；`x + 1` 和 `1 + x` 键不同，除非你先做交换律规范化。不要小看这一点：不做规范化会漏掉很多重复，做了规范化又可能引入新的浮点结合顺序问题。

## 底层机制：一遍 pass 不够，顺序也有讲究

本章使用的顺序是：

```text
fold -> simplify -> DCE -> CSE -> DCE
```

原因很实际：

- 折叠会产生新的 `get_attr`，旧的常量节点变成死节点，需要 DCE；
- 化简会把 `x*1` 直接替换成 `x`，被删节点的输入可能变成死节点；
- CSE 合并重复节点后，被合并的那个节点变成死节点，需要再跑一次 DCE；
- CSE 合并后可能让两个常量节点变成同一个，下一轮折叠又有机会。

最终用「本轮是否有改动」判断收敛。不要用固定轮数硬编码，也不要把互相反向的改写放在同一个循环里，否则可能振荡。幂等性检验很简单：再跑一轮，所有计数都应该是 0。

## 示例一：搭一张带常量的 FX 图

`symbolic_trace` 会把 `self.c1 + self.c2` 直接 eager 执行掉，图里看不到这两个常量。要构造「图里有常量节点」的例子，最稳的是手写 `fx.Graph`，用 `get_attr` 表示 buffer：

```python
import operator
import torch
import torch.fx as fx

def build_gm():
    g = fx.Graph()
    x = g.placeholder("x")
    c1 = g.get_attr("c1")
    c2 = g.get_attr("c2")
    c3 = g.get_attr("c3")
    s1 = g.call_function(operator.add, (c1, c2))
    s = g.call_function(operator.add, (s1, c3))
    a = g.call_function(operator.add, (x, s))
    b = g.call_function(operator.add, (x, s))
    m1 = g.call_function(operator.mul, (a, 1.0))
    m2 = g.call_function(operator.add, (a, 0.0))
    z = g.call_function(operator.mul, (a, 0.0))
    g.output((m1, m2, z, b))
    root = torch.nn.Module()
    for name, value in [("c1", 2.0), ("c2", 3.0), ("c3", 4.0)]:
        root.register_buffer(name, torch.tensor(value))
    gm = fx.GraphModule(root, g)
    gm.recompile()
    return gm
```

实测的折叠前图（`gm.code` 的核心部分）：

```text
c1 = self.c1
c2 = self.c2
c3 = self.c3
add = c1 + c2
add_1 = add + c3
add_2 = x + add_1
add_3 = x + add_1
mul = add_2 * 1.0
add_4 = add_2 + 0.0
mul_1 = add_2 * 0.0
return (mul, add_4, mul_1, add_3)
```

这张图同时包含嵌套常量、重复子表达式、`x*1`、`x+0` 和危险的 `x*0`，正好覆盖本章四个 pass。

## 示例二：实现四个 pass 并跑到不动点

完整脚本见 `.scratch/ai-compiler/ch6/folding.py`。核心函数如下（省略 import 和打印）：

```python
def is_const(gm, node):
    if node.op == "get_attr":
        obj = gm
        for atom in str(node.target).split("."):
            obj = getattr(obj, atom)
        return True, obj
    return False, None

def eval_const(gm, node):
    if node.op == "get_attr":
        return is_const(gm, node)
    if node.op != "call_function":
        return False, None
    vals = []
    for arg in node.args:
        ok, v = is_const(gm, arg) if isinstance(arg, fx.Node) else (True, arg)
        if not ok:
            return False, None
        vals.append(v)
    if node.kwargs:
        return False, None
    try:
        return True, node.target(*vals)
    except Exception:
        return False, None

def fold_once(gm, counter):
    changed = 0
    for node in list(gm.graph.nodes):
        if node.op != "call_function":
            continue
        if node.target not in (operator.add, operator.mul, operator.sub,
                               torch.add, torch.mul):
            continue
        ok, value = eval_const(gm, node)
        if not ok:
            continue
        name = f"_folded_{counter[0]}"
        counter[0] += 1
        gm.register_buffer(name, torch.as_tensor(value))
        with gm.graph.inserting_before(node):
            new = gm.graph.get_attr(name)
        node.replace_all_uses_with(new)
        gm.graph.erase_node(node)
        changed += 1
    return changed

def simplify(gm):
    changed = 0
    for node in list(gm.graph.nodes):
        if node.op != "call_function" or len(node.args) != 2:
            continue
        a, b = node.args
        if not isinstance(a, fx.Node):
            continue
        if node.target is operator.mul and b == 1.0:
            node.replace_all_uses_with(a)
            gm.graph.erase_node(node)
            changed += 1
        elif node.target is operator.add and b == 0.0:
            node.replace_all_uses_with(a)
            gm.graph.erase_node(node)
            changed += 1
    return changed

def dce(gm):
    removed = 0
    while True:
        dead = [n for n in gm.graph.nodes if n.op != "output" and len(n.users) == 0]
        if not dead:
            return removed
        for n in dead:
            gm.graph.erase_node(n)
            removed += 1

def cse(gm):
    def freeze(v):
        if isinstance(v, fx.Node):
            return ("node", v.name)
        if isinstance(v, (tuple, list)):
            return tuple(freeze(i) for i in v)
        if isinstance(v, dict):
            return tuple(sorted((k, freeze(x)) for k, x in v.items()))
        if isinstance(v, torch.Tensor):
            return ("tensor", id(v))
        return (type(v).__name__, repr(v))

    seen = {}
    merged = 0
    for node in list(gm.graph.nodes):
        if node.op in ("placeholder", "output", "get_attr"):
            continue
        key = (node.op, str(node.target), freeze(node.args), freeze(node.kwargs))
        if key in seen:
            node.replace_all_uses_with(seen[key])
            gm.graph.erase_node(node)
            merged += 1
        else:
            seen[key] = node
    return merged

gm = build_gm()
counter = [0]
for round_no in range(1, 5):
    nf = fold_once(gm, counter)
    ns = simplify(gm)
    nd = dce(gm)
    nc = cse(gm)
    nd += dce(gm)
    print(f"round {round_no}: fold={nf} simplify={ns} dce={nd} cse={nc}")
    if nf == ns == nc == 0:
        break
gm.recompile()
```

实测的收敛过程：

```text
round 1: fold=2 simplify=2 dce=4 cse=1
round 2: fold=0 simplify=0 dce=0 cse=0
```

折叠后图变成：

```text
_folded_1 = self._folded_1
add_2 = x + _folded_1;  x = _folded_1 = None
mul_1 = add_2 * 0.0
return (add_2, add_2, mul_1, add_2)
```

`c1 + c2 + c3` 被折成 `9.0`；`x + 9.0` 的两个重复节点被 CSE 合并；`x*1` 和 `x+0` 被化简掉；`x*0` 保留。数值对齐：

```text
before out[0:3]: [tensor([10., 11., 6.]), tensor([10., 11., 6.]), tensor([0., 0., 0.]), tensor([10., 11., 6.])]
after  out[0:3]: [tensor([10., 11., 6.]), tensor([10., 11., 6.]), tensor([0., 0., 0.]), tensor([10., 11., 6.])]
allclose(equal_nan=True): True
```

注意 `fold=2` 发生在同一轮：`fold_once` 遍历的是 `list(gm.graph.nodes)` 快照，先把 `c1+c2` 替换成 `_folded_0`，后面的 `s1+c3` 节点参数已经被 `replace_all_uses_with` 改成 `_folded_0`，于是同轮继续折成 `_folded_1`。这说明「快照遍历 + 立即替换」可以在一轮里处理一部分级联；但不能保证所有级联，所以仍要外层循环。

## 示例三：NaN/Inf、幂等性与「折叠变慢」

### `x*0` 的边界

实测：

```text
xs * 0.0 : tensor([nan, nan, nan, 0.])
zeros_like(xs): tensor([0., 0., 0., 0.])
```

如果 pass 把 `x*0` 换成 `zeros_like(x)`，含 NaN/Inf 的输入会从「NaN」变成「0」，这是语义错误，不是精度问题。本章的 pass 因此保留 `mul_1`。

### 如果一定要折 `x*0`：先用 dtype 元信息把关

整数图没有 NaN/Inf，`x*0 -> zeros_like(x)` 是安全的；浮点图不行。实现时不要看「参数是不是 0」就动手，要先拿到 dtype。FX 可以用 `ShapeProp` 给节点写入 `meta["tensor_meta"]`：

```python
import operator
import torch
import torch.fx as fx
from torch.fx.passes.shape_prop import ShapeProp

def fold_zero_mul_int(gm):
    changed = 0
    for node in list(gm.graph.nodes):
        if node.op != "call_function" or node.target is not operator.mul:
            continue
        a, b = node.args
        if not isinstance(a, fx.Node) or b != 0:
            continue
        tm = a.meta.get("tensor_meta")
        if tm is None or tm.dtype.is_floating_point:
            continue
        with gm.graph.inserting_before(node):
            z = gm.graph.call_function(torch.zeros_like, (a,))
        node.replace_all_uses_with(z)
        gm.graph.erase_node(node)
        changed += 1
    return changed

class IntMulZero(torch.nn.Module):
    def forward(self, x):
        return x * 0

gm = fx.symbolic_trace(IntMulZero())
ShapeProp(gm).propagate(torch.randint(0, 5, (3,)))
print(gm.code)
print(fold_zero_mul_int(gm))
gm.recompile()
print(gm.code)
print(gm(torch.tensor([1, -2, 0])))
```

实测（`x` 为 `int64`）：

```text
def forward(self, x):
    mul = x * 0;  x = None
    return mul

1

def forward(self, x):
    zeros_like = torch.zeros_like(x);  x = None
    return zeros_like
```

`out` 为 `tensor([0, 0, 0])`。反过来，本章那张 float32 图里 `tm.dtype.is_floating_point` 为真，守卫会拒绝改写，`mul_1` 保留。没有 `tensor_meta` 时也应该保守跳过：**不知道 dtype 就不折**。

### 幂等性

第二轮所有计数为 0，说明 pass 达到不动点。你可以把「再跑一轮计数全 0」写进测试；如果某个 pass 每轮都改图，说明它没有向「更小/更规范」的方向收敛。

### 折叠为什么可能变慢

- **大常量**：把两个 2048×2048 的常量矩阵折成一个，编译期要分配并写入 16 MB buffer；模型加载、序列化、设备拷贝都会变慢。
- **dtype 提升**：`float16` 常量在 CPU 上参与某些运算可能被提升到 `float32`，折叠结果以更高精度 buffer 存下来，后续 kernel 的 dtype 和你预期的不一样。
- **编译期换运行期**：如果模型只跑一次，编译期算一个大 matmul 可能比运行时算更亏；折叠适合「多次推理、常量不变」的场景。
- **失去惰性**：折叠让计算在加载时发生，可能拉长首帧延迟；对交互式服务，首帧延迟和吞吐是两回事。
- **数值结合顺序**：折叠 `(c1+c2)+c3` 和运行时按图顺序算，浮点舍入可能不同；对精度敏感的模型要保留原顺序或做误差评估。

## 对比与取舍

| 选择 | 收益 | 代价 | 建议 |
|---|---|---|---|
| 折叠所有常量 | 图最小 | 编译期/模型体积/精度风险 | 只折纯函数、可控大小的常量 |
| 折 `x*0` | 少一个节点 | NaN/Inf 语义错误 | 默认不折；有范围证明再折 |
| CSE 合并 | 少重复计算 | 键构造/内存生命周期变复杂 | 先做交换律规范化，再合并 |
| DCE 激进 | 图最干净 | 可能删副作用 | 纯算子白名单 |
| 固定跑 1 轮 | 实现简单 | 留下部分折叠的图 | 用「无改动」收敛 |

## 踩雷集锦

1. **在 `forward` 里写 `torch.tensor(...)` 就以为图里有常量**：trace 时会 eager 执行，图里可能只有一个结果常量，根本没有 `add` 节点。要构造常量图，用手写 `Graph` 或 `get_attr`。
2. **先建 `GraphModule` 再注册 buffer**：构造时 `get_attr` 找不到目标会报 `AttributeError`。先把 buffer 注册到 root module，再 `GraphModule(root, graph)`。
3. **边遍历边删节点**：`for n in gm.graph.nodes: erase_node(n)` 会破坏迭代。用 `list(...)` 快照，DCE 从后往前。
4. **CSE 键只用 target**：`add(x,1)` 和 `add(y,1)` 会被错误合并；必须把 args/kwargs 一起入键。
5. **CSE 不处理交换律**：`x+1` 和 `1+x` 不会合并；要么接受漏匹配，要么先规范化参数顺序。
6. **把 `x*0` 折成 0**：NaN/Inf 输入直接错；本章用实测输出证明。
7. **忘了 `recompile()`**：图变了但 `gm(x)` 还是旧代码；见 Ch 5 的 stale demo。
8. **DCE 删掉 `print`/随机数节点**：FX 不知道副作用；生产 pass 要维护纯算子集合。

## 进阶：再往深一层

- **范围分析**：如果你能证明 `x` 有限（例如 `x` 来自 `relu` 且输入有限），`x*0` 才能安全折叠。真实编译器会在 IR 上做区间/NaN 传播，而不是靠猜。
- **常量折叠在导出图里**：`torch.export` 的 ATen 图、Relax、StableHLO 都有各自的常量折叠 pass；语义边界（NaN、dtype、溢出）是共同问题。
- **CSE 的等价性**：浮点 `a+b` 和 `b+a` 在 IEEE 下通常相等，但 `a+(b+c)` 和 `(a+b)+c` 不一定；CSE 只能合并结构完全相同的表达式，重结合要单独评估。
- **pass 管理器**：真实编译器会把 fold/simplify/DCE/CSE 注册成有序 pass，并记录每轮改动；本章的手写循环是它的最小版本。
- **源码对照**：读 `torch/fx/graph.py` 的 `eliminate_dead_code`，看它如何从输出反向标记可达节点；这比「只看 users」更适合处理复杂图。

## 动手练习

### 练习 1：折一条三级常量链

任务：把示例图改成 `((c1 + c2) * c3) + c4`，其中 `c1=2, c2=3, c3=4, c4=5`。要求输出 `25.0`，并记录 `fold_once` 需要几轮（或同一轮折了几个）。

测试用例：`x = tensor([0., 1.])`，期望输出 `[25., 26.]`。

<details>
<summary>参考解答</summary>

先折 `c1+c2=5`，再折 `5*c3=20`，最后折 `20+c4=25`。如果每轮只折一层，需要 3 轮；如果快照遍历 + 立即替换，可能在同一轮折掉多层。用 `round` 计数打印验证；最后跑 `gm(x)` 与 `x+25` 对齐。

常见卡点：`eval_const` 只接受 `get_attr` 和全常量 `call_function`，不要把 `x` 误判为常量。

</details>

### 练习 2：实现 `x - 0` 和 `x / 1`

任务：在 `simplify` 里增加 `sub(x, 0) -> x` 和 `truediv(x, 1) -> x`。测试 `x = tensor([-0.0, 1.0, float("inf")])`，比较改写前后的位模式。

<details>
<summary>参考解答</summary>

`x - 0` 对 `-0.0` 会得到 `-0.0`（减法保留符号），`x / 1` 也保留；但 `x + 0` 会把 `-0.0` 变成 `+0.0`。用 `torch.signbit` 或 `x.view(torch.int32)` 检查位模式。不要因为「数值相等」就认为位模式相同。

</details>

### 练习 3：让 CSE 处理 `x+1` 和 `1+x`

任务：先实现参数规范化（把常量放到右边），再跑 CSE，验证两个节点被合并。测试图里同时有 `x+1` 和 `1+x`。

<details>
<summary>参考解答</summary>

在 CSE 前加一个 canonicalize pass：对 `operator.add`/`operator.mul`，如果 `args[0]` 是常量、`args[1]` 是 Node，就交换。然后 CSE 键自然相同。注意：交换会改变浮点加法的操作数顺序，通常 IEEE 加法可交换，但 NaN payload 等细节可能不同；对精度敏感场景要评估。

</details>

### 练习 4：DCE 与副作用

任务：构造一张图，包含一个没有用户的 `print` 节点和一个没有用户的 `add` 节点。先按本章 DCE 删除，观察 `print` 是否也被删；再设计一个「纯算子白名单」版本。

<details>
<summary>参考解答</summary>

本章 DCE 会把两个都删，因为只看 `len(users)==0`。白名单版本只删除 target 在 `{operator.add, operator.mul, operator.sub}` 里的节点，保留未知 `call_function`。这也是为什么生产编译器需要副作用信息，而不能只看数据流。

</details>

### 练习 5：验证 `x*0` 不能折

任务：用 `x = tensor([nan, inf, -inf, 2.])` 跑 `x*0` 和 `zeros_like(x)`，记录输出；再写一个「仅当 dtype 为整数时才折」的守卫。

<details>
<summary>参考解答</summary>

实测 `x*0` 是 `[nan, nan, nan, 0.]`，`zeros_like` 是 `[0., 0., 0., 0.]`。守卫可以是 `if x.dtype not in (torch.float16, torch.float32, torch.float64, torch.bfloat16)`，但还要考虑整数溢出语义；最稳的是不做这个折叠，除非有范围证明。

</details>

## 本章重点整理

- 常量折叠是编译期求值；嵌套常量需要多轮或工作列表。
- `x*1` 安全；`x+0` 数值安全但会改变 `-0.0` 的符号；`x*0` 对 NaN/Inf 不安全。
- DCE 用 `len(node.users) == 0`，倒序清理；要排除 output 和副作用节点。
- CSE 用 `op + target + args + kwargs` 做键；交换律需要额外规范化。
- pass 顺序 `fold -> simplify -> DCE -> CSE -> DCE`，用「本轮无改动」收敛。
- 折叠可能变慢：大常量、dtype 提升、编译期开销、模型体积、浮点结合顺序。
- 每个改写都要用参考实现和 `allclose(equal_nan=True)` 验证。

## 自我检核

- [ ] 我能解释为什么 `symbolic_trace` 可能看不到 `self.c1 + self.c2`
- [ ] 我能写出常量折叠的「全常量参数 -> 求值 -> get_attr」流程
- [ ] 我知道 `x*0` 在 NaN/Inf 下为什么不能折
- [ ] 我知道 `x+0` 对有符号零的影响
- [ ] 我能用 `len(node.users)` 实现 DCE 并解释倒序
- [ ] 我能构造 CSE 的键并说明交换律的坑
- [ ] 我能让 pass 跑到不动点并验证幂等性
- [ ] 我能说出至少三个「折叠变慢」的原因

## 延伸阅读

- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：读 `Graph`、`Node`、`GraphModule` 三节。与本章的关联：手写图和改图的基础 API。
- **`torch/fx/graph.py` 的 `eliminate_dead_code`**（本机路径 `.scratch/aic-env/Lib/site-packages/torch/fx/graph.py:2690`）：读它如何从输出反向标记可达节点。与本章的关联：DCE 的工业实现。
- **`torch/fx/node.py`**：读 `users`、`all_input_nodes`、`replace_all_uses_with`。与本章的关联：DCE/CSE 的节点操作。
- **TVM Relax 的常量折叠 pass**（https://tvm.apache.org/docs/）：搜 `FoldConstant`。与本章的关联：同一思想在另一套 IR 上的实现。
- **IEEE 754 浮点特殊值**：读 NaN/Inf/有符号零的传播规则。与本章的关联：`x*0`、`x+0` 的边界。

---

→ [Ch 7 算子融合](./07-operator-fusion.md)
