# Ch 5 — 动态 shape 与控制流

> **目标**：搞清「动态」在 AI 编译器里到底意味着什么、为什么它和边缘部署天然冲突，以及生产工具用什么机制在两者之间找平衡。学完这章，你能判断一个模型适不适合上边缘设备。

## 为什么需要这个？

Ch 2 的约束级联已经给出结论：

```
没有 OS → 不能 malloc → 内存必须静态规划 → shape 必须编译期已知
```

但真实模型几乎都有动态性：

- NLP：序列长度 1 到 4096 不等
- 检测：输入图片分辨率可变
- 推荐：用户历史长度不定
- LLM：生成时每步多一个 token

所以「动态 shape」不是理论问题，是**部署时必须做的第一个决定**。

## 先建立直觉

把编译一个模型想成**订做一个书架**：

```
静态 shape（固定 4×8）
  → 书架尺寸确定，木料精确下料，零浪费
  → 但换一批书就装不下，要重做

动态 shape（1..64）
  → 按最大尺寸 64 下料
  → 装什么都行，但小书也占大格子（内存浪费）
  → 而且每次都要现场量尺寸（运行时开销）

「先按最大做，再内部调整」
  → 复杂，但省料
```

这就是三种策略的本质取舍。边缘设备内存少，所以**浪费不起**；但重新编译（重做书架）成本又高。这个矛盾是本章的主线。

## 静态 vs 动态：实测差异

### 静态导出

```python
import torch

class M(torch.nn.Module):
    def forward(self, x, w):
        return torch.relu(x @ w)

ep = torch.export.export(M(), (torch.randn(4, 8), torch.randn(8, 4)))
print(ep.range_constraints)
```

实测：

```
{}
```

空的（Ch 3 已经确认过）。**静态导出不留约束，因为压根没有符号维**。

### 动态导出

```python
from torch.export import Dim

db = Dim("db", min=1, max=64)
ep = torch.export.export(
    M(), (torch.randn(4, 8), torch.randn(8, 4)),
    dynamic_shapes={"x": {0: db}, "w": {1: db}})
print("ok")
```

实测：

```
dynamic ok
```

导出成功。注意这里 `x` 的第 0 维和 `w` 的第 1 维**绑到同一个 `Dim`**——这是表达「这两个维度必须相等」的方式（矩阵乘的收缩维）。

如果忘了绑（或者绑错），会在运行期算出形状不匹配。**动态导出把一部分 shape 检查从编译期推到了运行期**，这是它的核心代价。

## 动态维度的三种开销

### 开销一：内存按上界分配

```python
db = Dim("db", min=1, max=64)
```

编译器看到 `max=64`，就必须为「最大可能」预留：

| 实际 batch | 必需内存 | 按 max 预留 | 浪费 |
|---|---|---|---|
| 1 | 1× | 64× | 98.4% |
| 8 | 8× | 64× | 87.5% |
| 32 | 32× | 64× | 50% |
| 64 | 64× | 64× | 0% |

在 512 KB SRAM 的 MCU 上，这意味着原本能跑 batch=8 的模型，现在因为要支持到 64 而**完全放不下**。

> **这就是边缘部署最常见的「动态 shape 陷阱」**：服务器上 `Dim(min=1, max=64)` 很合理（反正内存多），搬到 MCU 上直接让模型超内存。

### 开销二：运行时断言

这是本章最有价值的实测。用 `torch.cond` 表达控制流并导出：

```python
import torch

class C(torch.nn.Module):
    def forward(self, x, w):
        pred = x.sum() > 0
        return torch.cond(pred,
                          lambda a: torch.relu(a @ w),
                          lambda a: a.clone(),
                          (x,))

ep = torch.export.export(C(), (torch.randn(4, 8), torch.randn(8, 4)))
print(ep.graph)
```

实测输出：

```
graph():
    %x : [num_users=2] = placeholder[target=x]
    %w : [num_users=1] = placeholder[target=w]
    %sum_1 : [num_users=1] = call_function[target=torch.ops.aten.sum.default](args = (%x,), kwargs = {})
    %gt : [num_users=1] = call_function[target=torch.ops.aten.gt.Scalar](args = (%sum_1, 0), kwargs = {})
    %true_graph_0 : [num_users=1] = get_attr[target=true_graph_0]
    %false_graph_0 : [num_users=1] = get_attr[target=false_graph_0]
    %cond : [num_users=1] = call_function[target=torch.ops.higher_order.cond](args = (%gt, %true_graph_0, %false_graph_0, (%x, %w)), kwargs = {})
    %getitem_1 : [num_users=2] = call_function[target=operator.getitem](args = (%cond, 0), kwargs = {})
    %sym_size_int_1 : [num_users=4] = call_function[target=torch.ops.aten.sym_size.int](args = (%getitem_1, 1), kwargs = {})
    %sym_constrain_range_for_size_default : ... = call_function[target=torch.ops.aten.sym_constrain_range_for_size.default](args = (%sym_size_int_1,), kwargs = {})
    %ge_2 : ... = call_function[target=operator.ge](args = (%sym_size_int_1, 4), kwargs = {})
    %_assert_scalar_default : ... = call_function[target=torch.ops.aten._assert_scalar.default](args = (%ge_2, Runtime assertion failed for expression u0 >= 4 on node 'ge_2'), kwargs = {})
    %le : ... = call_function[target=operator.le](args = (%sym_size_int_1, 8), kwargs = {})
    %_assert_scalar_default_1 : ... = call_function[target=torch.ops.aten._assert_scalar.default](args = (%le, Runtime assertion failed for expression u0 <= 8 on node 'le'), kwargs = {})
    %ge_1 : ... = call_function[target=operator.ge](args = (%sym_size_int_1, 2), kwargs = {})
    %_assert_scalar : ... = call_function[target=torch.ops.aten._assert_scalar.default](args = (%ge_1, Runtime assertion failed for expression u0 >= 2 on node 'ge'), kwargs = {})
    return (getitem_1,)
```

读这堆节点：

1. **`higher_order.cond`**：控制流终于被保留在图里了（对比 Ch 4 的 graph break）。
2. **`true_graph_0` / `false_graph_0`** 是**子图**，作为属性挂在主图上。
3. **六七个 `_assert_scalar`**：每个都是一个**运行时要执行的断言**。

最后一条最关键：

```mlir
args = (%ge_1, Runtime assertion failed for expression u0 >= 2 on node 'ge')
```

> **这行断言的意思是**：每次推理，程序都会检查「输出第 1 维 ≥ 2」。检查本身不贵，但它揭示了动态 shape 的本质——**把编译期能证明的事，推迟到运行期检查**。

在服务器上这没问题。在 MCU 上，这些断言：

- 增加代码体积（Flash 有限）
- 增加运行时分支（没有分支预测器）
- 失败时**没有异常机制**可以报错（没有 OS）

所以边缘部署的第一选择通常是：**把动态消掉**。

### 开销三：无法完全展开循环

静态 shape 下，编译器知道循环次数：

```
for i in 0..8192      → 可以按 8 路向量化，8192/8 = 1024 次精确迭代
```

动态 shape 下：

```
for i in 0..n         → 需要处理尾部（n % 8 != 0）
                      → 需要运行时计算向量化次数
                      → 可能生成「主循环 + 尾部处理」两份代码
```

代码体积翻倍，这在 MCU 的 Flash 上很致命。

## `torch.cond` 的实测踩坑

我中途试过一个更自然的写法：

```python
return torch.cond(pred, lambda a: torch.relu(a @ w), lambda a: a, (x,))
```

报错：

```
UncapturedHigherOrderOpError: Cond doesn't work unless it is captured completely with torch.compile.
Got Encountered aliasing during higher order op tracing
  Explanation: Higher order ops do not support aliasing. Found in cond
  Hint: Replace `return input` with `return input.clone()` to avoid aliasing.
```

**`lambda a: a` 直接把输入返回，算「aliasing」（输入输出同一块内存），高阶算子不支持。**加 `.clone()` 才过。

> **教训**：`torch.cond` 的两个分支必须返回**新分配的** tensor，不能直接返回输入。这是高阶算子的通用约束（编译器需要保证两个分支的输出内存布局一致）。

## 边缘部署的四种策略

| 策略 | 做法 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| **完全静态** | 一个 shape 编一份 | 内存最优、代码最快 | 换 shape 要重编 | batch=1 的固定输入 |
| **分桶（bucketing）** | 按几个离散 shape 各编一份 | 兼顾灵活与性能 | 存储多份，切换有开销 | NLP 序列长度 |
| **最大填充（padding）** | 全按 max shape | 一份二进制 | 短输入浪费算力/内存 | 输入分布集中 |
| **真动态** | 符号 shape + 运行时断言 | 一份二进制 | 内存按上界、代码膨胀 | 服务器，或 RAM 充足 |

实测证据（本章 `torch.cond` 导出）证明第四种在边缘上代价很高：你得到了一堆 `_assert_scalar`，还有按上界分配的内存。

> **工程结论**：边缘部署的默认选择是**分桶**。LLM 推理框架（llama.cpp、MNN、TFLite）全部这么做——预编译几个常见长度，超出就回退。

## 把动态变静态：实测

```python
# 动态版
ep_dyn = torch.export.export(M(), (x, w), dynamic_shapes={"x": {0: db}, "w": {1: db}})

# 静态版：把动态维固定
ep_static = torch.export.export(M(), (torch.randn(4, 8), torch.randn(8, 4)))
```

用脚本对比两者的图数量与约束（本机实测）：

```bash
~/.venvs/learn/bin/python -c "
import torch
from torch.export import Dim
class M(torch.nn.Module):
    def forward(self, x, w): return torch.relu(x @ w)
db = Dim('db', min=1, max=64)
e1 = torch.export.export(M(), (torch.randn(4,8), torch.randn(8,4)))
e2 = torch.export.export(M(), (torch.randn(4,8), torch.randn(8,4)),
                         dynamic_shapes={'x': {0: db}, 'w': {1: db}})
print('static rc:', len(e1.range_constraints))
print('dynamic rc:', len(e2.range_constraints), dict(e2.range_constraints))
"
```

实测：

```
static rc: 0
dynamic rc: 1
{s21: VR[1, 64]}
```

静态版**零约束**（编译器知道一切），动态版有一个 `VR[1, 64]`（编译器必须按 64 预留）。

> **注意符号名会变**：这里写 `s21`，你跑出来可能是 `s77`、`s20` 或别的编号——符号名的分配顺序取决于导出过程。**不要把这个编号写进断言或脚本**，只看 `VR[min, max]` 那部分。下面这个坑很常见：教程里抄一个 `s0`，自己跑出来报 `KeyError: 's0'`。

## 踩雷集锦

1. **以为 `dynamic_shapes` 不写就是动态**：不写 = 静态。动态是**显式声明**的，而且必须给出 `min`/`max`。
2. **`Dim(min=1, max=64)` 在 MCU 上直接 OOM**：内存按 64 预留。边缘场景优先用 `min=max=固定值` 或分桶。
3. **`torch.cond` 分支返回输入**：报 aliasing 错误。必须 `.clone()`。
4. **忽略 `_assert_scalar` 节点**：它们会成为运行时检查。MCU 上没有异常机制，检查失败只能静默算错或死循环。
5. **以为「编译器会自己优化掉动态」**：不会。`Dim` 一旦声明，约束就进图，编译器按上界分配。想去掉必须重新导出。

## 本章重点整理

- 动态 shape 的三种代价：按上界分配内存、运行时断言、循环无法完全展开。
- `range_constraints` 只有**显式声明 `dynamic_shapes`** 时才非空。
- `torch.cond` 把控制流保留在图里，但要求分支返回新 tensor。
- 边缘部署默认策略是**分桶**：几个固定 shape 各编一份。
- 「把动态变静态」是最有效的边缘优化，没有之一。

## 自我检核

- [ ] 能说出动态 shape 的三类开销，以及为什么每类在 MCU 上更严重
- [ ] 知道静态导出时 `range_constraints` 为空，动态导出才有
- [ ] 能解释 `torch.cond` 为什么不让分支返回输入（aliasing）
- [ ] 看到图里的 `_assert_scalar` 节点，知道它意味着运行期要做什么
- [ ] 能说出「分桶」策略为什么在边缘比「真动态」更合适

## 延伸阅读

### 官方文档

- **[torch.export — Dynamic Shapes](https://pytorch.org/docs/stable/export.html#expressing-dynamic-shapes)**
  - **读哪里**：`Dim` / `Dim.AUTO` / `dynamic_shapes` 的说明
  - **为什么值得读**：本章 `Dim(min, max)` 的权威定义，以及绑定多个维度的语法
- **[torch.cond 文档](https://pytorch.org/docs/stable/generated/torch.cond.html)**
  - **读哪里**：限制条件那一段（aliasing、返回类型一致性）
  - **和本章的关联**：本章 aliasing 踩坑的官方说明

### 论文

- **[PyTorch/XLA — Dynamic Shapes 设计文档](https://github.com/pytorch/xla/blob/master/docs/dynamic_shapes.md)**
  - **核心内容**：把动态维建模为「有界的符号维度」（bounded symbolic dimension），正是本章 `Dim(min, max)` 的思想来源
  - **读哪里**：「Symbolic Dimension」与「Bounded Dynamism」两节
  - **和本章的关联**：解释了为什么动态 shape 必须是「有界」的——无界就无法做内存规划
  - **说明**：动态 shape 目前没有单一权威论文，各编译器的设计文档是一手来源

### 博客 / 工程文章

- **[PyTorch — Compile Graph Break 案例库](https://meta-pytorch.github.io/compile-graph-break-site/)**
  - **这系列说什么**：数据依赖控制流导致的 graph break 案例
  - **读哪里**：搜 `control flow`，对照本章 `torch.cond` 的做法
  - **为什么值得读**：官方维护，按现象索引

- **[llama.cpp — batch 与 context 处理](https://github.com/ggml-org/llama.cpp)**
  - **读哪里**：`examples/` 下关于 context size 与 batch 的参数处理
  - **和本章的关联**：本章「分桶」策略的真实工程实现，看它怎么在有限内存里支持变长输入

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版）
  - **读哪几章**：第 9 章（数据流分析）——理解「内存上界」如何由数据流分析算出
  - **注意**：书里讲的是通用编译器，动态 shape 的内存问题在 AI 编译器里被放大了

到这里，模型侧的基本问题都清楚了。下一章进入 AI 编译器的核心：Inductor 到底把图变成了什么。

→ [Ch 6 TorchInductor 全链路实测](./06-inductor-pipeline.md)
