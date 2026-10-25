# Ch 2 图 IR 设计

> 目标：读懂 `mini_ai/ir.py` 的四个核心类型 `TensorType`、`Value`、`Op`、`Graph`，理解 SSA（Static Single Assignment，静态单赋值）、拓扑序、`validate()` 与 `dump()` 的关系；能自己建图、故意破坏不变量，并从错误信息定位问题。

> 环境：Windows 10/11 x86_64；Python 3.11.12；numpy 2.4.6。命令在 `D:\Learning-with-Claude` 下执行，设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"`，Python 用 `.scratch\aic-env\Scripts\python.exe`。

## 为什么需要

图 IR（Intermediate Representation，中间表示）是前端、Pass（优化遍）、内存规划、后端之间唯一的契约。没有它，前端会把 PyTorch 对象直接塞给 C 代码生成器，Pass 会依赖某个后端的私有数据结构，内存规划也无法知道一个值从哪来、到哪去。

一个可用的图 IR 至少要回答五个问题：

1. 每个值是什么类型、什么 shape？
2. 每个 op 读哪些值、写哪些值？
3. 值的定义是否先于使用？
4. 哪些值是输入、常量、临时值、输出？
5. 一张图是否合法，能否被安全地改写和执行？

本章的 `Graph` 用很小的代码回答这五个问题。它不支持控制流，也不支持动态 shape，但足以支撑后面的融合、生命周期分析和两条后端。

## 先建立直觉

### 对象关系

```text
Graph
  inputs:  [Value x, Value y]
  values:  [Value x, Value y, Value c, Value t0, Value out]
  ops:     [Op add, Op relu]
  outputs: [Value out]

Value x        Value c            Value t0           Value out
 kind=input     kind=constant      kind=temp          kind=output
 producer=None  producer=None      producer=Op add    producer=Op relu
 users=[add]    users=[add]        users=[relu]       users=[]

Op add                         Op relu
 kind="add"                     kind="relu"
 inputs=[x, c]                  inputs=[t0]
 outputs=[t0]                   outputs=[out]
 attrs={}                       attrs={}
```

每个 `Value` 知道“谁生产我”（`producer`）和“谁使用我”（`users`）。每个 `Op` 知道“我读什么、写什么”。`Graph` 只是把这些对象按顺序装起来，并维护输入、输出列表。

### SSA 与拓扑序

```text
%x   = input
%c   = constant
%t0  = add(%x, %c)      # 每个值只定义一次
%out = relu(%t0)        # 使用之前必须已定义
return %out
```

SSA 的核心约束是：**每个值只有一个定义点**。`Graph.validate()` 会记录已经见过的 `Value`，第二次遇到同一个对象就报 `value ... is defined more than once`。

拓扑序的核心约束是：**op 只能使用前面 op 已经产出的值，或者输入/常量**。`validate()` 在扫描第 `i` 个 op 时，只把前 `i` 个 op 放进 `op_index`，所以“使用后面的值”会被拒绝。

## 核心内容

### 2.1 TensorType：类型就是 shape 加 dtype

```python
@dataclass(frozen=True)
class TensorType:
    shape: tuple[int, ...]
    dtype: str = "float32"

    def __post_init__(self) -> None:
        if self.dtype != "float32":
            raise IRError(f"only float32 is supported, got {self.dtype}")
        if any(not isinstance(d, int) or d <= 0 for d in self.shape):
            raise IRError(f"invalid shape: {self.shape}")
```

`frozen=True` 让 `TensorType` 不可变，适合作为字典键的一部分，也避免某个 Pass 偷偷改 shape。`numel`、`rank`、`strides` 都是由 `shape` 推出来的属性。

实际运行：

```python
from mini_ai.ir import TensorType

t = TensorType((2, 3))
print(t.strides, t.numel, t.rank, str(t))
```

输出：

```text
(3, 1) 6 2 tensor<2, 3>
```

`strides` 是行主序（row-major）的步长，单位是元素而不是字节。`(2, 3)` 的 strides 是 `(3, 1)`：沿第 0 维走一步跳过 3 个元素，沿第 1 维走一步跳过 1 个元素。Ch 4 会展开讲。

`TensorType` 只接受 `float32`。传入 `float64` 会立即失败：

```text
IRError: only float32 is supported, got float64
```

### 2.2 Value：名字、类型、角色、数据

```python
@dataclass(eq=False)
class Value:
    name: str
    type: TensorType
    kind: str = "temp"  # input | constant | temp | output
    data: Optional[np.ndarray] = None
    producer: Optional["Op"] = None
    users: list["Op"] = field(default_factory=list)
    id: int = -1
```

四个角色：

| kind | 含义 | 是否有 producer | 是否有 data |
| --- | --- | --- | --- |
| `input` | 图的输入 | 否 | 否，运行时由 feed 提供 |
| `constant` | 编译期常量 | 否 | 是，`np.ndarray` |
| `temp` | 中间结果 | 是 | 否，运行时写到 arena |
| `output` | 图的输出 | 通常是 | 否，运行时从 arena 拷贝 |

`is_constant` 只是 `kind == "constant"` 的快捷判断。注意 `data` 只对常量有意义；临时值和输出都没有编译期数据。

`eq=False` 很关键：它关闭 dataclass 自动生成的 `__eq__`，让 `Value` 使用对象身份比较。后面所有 `id(v)` 集合都依赖这一点。

### 2.3 Op：一个 op，一个输出

```python
@dataclass(eq=False)
class Op:
    kind: str
    inputs: list[Value]
    outputs: list[Value]
    attrs: dict[str, Any] = field(default_factory=dict)
    id: int = -1

    @property
    def output(self) -> Value:
        if len(self.outputs) != 1:
            raise IRError(f"op {self.kind} has {len(self.outputs)} outputs")
        return self.outputs[0]
```

当前设计是单输出。`outputs` 仍是列表，是为了将来扩展多输出；但 `output` 属性会强制检查长度。手动构造一个有两个输出的 `Op` 再访问 `.output`，会得到：

```text
IRError: op relu has 2 outputs
```

`attrs` 存放算子属性，例如 `sum` 的 `axis` 和 `keepdims`、`softmax` 的 `axis`。属性不参与 SSA，但会影响 shape 推导和代码生成。

### 2.4 Graph：容器与构造入口

```python
class Graph:
    def __init__(self) -> None:
        self.values: list[Value] = []
        self.ops: list[Op] = []
        self.inputs: list[Value] = []
        self.outputs: list[Value] = []
        self._next_value_id = 0
        self._next_op_id = 0
```

`add_input(name, shape)` 创建一个 `input` 值，同时加入 `values` 和 `inputs`。

`add_const(name, data)` 把数据转成 `float32`，检查 rank 不超过 4，然后创建 `constant` 值。注意它会 `arr.copy()`，所以外部修改原数组不会影响图。

`add_op(...)` 是核心入口（节选，签名省略类型标注）：

```python
def add_op(self, kind, inputs, attrs=None, *, name=None, shape=None, dtype="float32"):
    attrs = dict(attrs or {})
    types = [v.type for v in inputs]
    inferred = infer_output_type(kind, types, attrs)
    explicit = TensorType(tuple(shape), dtype) if shape is not None else inferred
    if explicit.dtype != "float32":
        raise IRError("only float32 is supported")
    if explicit.shape != inferred.shape:
        raise IRError(
            f"shape mismatch for {kind}: inferred {inferred.shape}, explicit {explicit.shape}"
        )
    out_name = name or f"{kind}_{self._next_op_id}"
    out = self._new_value(out_name, explicit, kind="temp")
    op = Op(kind=kind, inputs=list(inputs), outputs=[out], attrs=attrs, id=self._next_op_id)
    self._next_op_id += 1
    out.producer = op
    for v in inputs:
        v.users.append(op)
    self.ops.append(op)
    return out
```

这段代码做了六件事：

1. 复制 `attrs`，避免调用方后续修改影响 op。
2. 用 `infer_output_type` 从输入类型推导输出类型。
3. 如果调用方显式给了 `shape`，检查它与推导结果一致。
4. 创建输出 `Value`，`kind="temp"`，`producer` 指向新 op。
5. 给每个输入的 `users` 追加这个 op。
6. 把 op 追加到 `ops`，返回输出值。

显式 shape 不一致会立即失败：

```text
IRError: shape mismatch for add: inferred (2, 3), explicit (2, 2)
```

`add_output(value, name=None)` 做三件事：按需改名字、把 `kind` 改成 `output`（常量除外）、加入 `outputs`。它不创建新值，所以输出值仍然是原来那个对象。

### 2.5 建一张图并 dump

```python
import numpy as np
from mini_ai.ir import Graph

g = Graph()
x = g.add_input("x", (2, 3))
b = g.add_const("b", np.array([1, 2, 3], np.float32))
y = g.add_op("add", [x, b], name="y")
s = g.add_op("sum", [y], {"axis": 1}, name="s")
g.add_output(s, "out")
print(g.dump())
```

实际输出：

```text
inputs:
  x: tensor<2, 3>
constants:
  b: tensor<3> numel=3
ops:
  00 %y = add(x, b) : tensor<2, 3>
  01 %out = sum(y) {'axis': 1} : tensor<2>
outputs:
  out: tensor<2>
```

`dump()` 的格式是刻意设计的：

- 输入和输出分段，方便看边界。
- 常量只打印 `numel`，不打印数据，避免大权重刷屏。
- 每个 op 前有序号，对应 `Graph.ops` 的下标。
- 输出值用 `%name = kind(inputs) attrs : type`，接近 LLVM 的阅读习惯。

`g.topo_names()` 返回 `['y', 'out']`，是 op 输出名的拓扑序列表。

### 2.6 validate 检查什么

```python
def validate(self) -> None:
    value_ids = {id(v) for v in self.values}
    seen: set[int] = set()
    op_index: dict[int, int] = {}
    for i, op in enumerate(self.ops):
        op_index[id(op)] = i
        for v in op.inputs:
            if id(v) not in value_ids:
                raise IRError(f"op {op.kind} uses a value outside the graph: {v}")
            if v.kind not in {"input", "constant"}:
                if v.producer is None or id(v.producer) not in op_index:
                    raise IRError(f"op {op.kind} uses {v.name} before it is defined")
        for v in op.outputs:
            if id(v) in seen:
                raise IRError(f"value {v.name} is defined more than once")
            seen.add(id(v))
            if v.producer is not op:
                raise IRError(f"value {v.name} has a stale producer")
    if not self.outputs:
        raise IRError("graph has no outputs")
    for v in self.outputs:
        if id(v) not in value_ids:
            raise IRError(f"graph output {v} does not belong to the graph")
```

它检查五类不变量：

1. 输入值必须属于这张图。
2. 非输入、非常量的值必须有 producer，而且 producer 必须在当前 op 之前出现。
3. 每个输出值只能被定义一次（SSA）。
4. 输出值的 `producer` 必须指向当前 op，不能是过期的。
5. 图必须有输出，且输出属于这张图。

它不检查后端是否支持某个 op、内存是否越界，也不检查 attrs 的全部语义；这些由后续层负责。

### 2.7 三条真实失败路径

第一条，广播失败，在建图阶段就报：

```python
x = g.add_input("x", (2, 3))
g.add_op("add", [x, g.add_const("bad", np.zeros((4,), np.float32))])
```

```text
IRError: shapes are not broadcastable: ((2, 3), (4,))
```

第二条，没有输出：

```python
Graph().validate()
```

```text
IRError: graph has no outputs
```

第三条，拓扑序被破坏。先建 `relu -> relu`，再手工把 `ops` 反转：

```python
g2 = Graph()
xg = g2.add_input("x", (2, 3))
yg = g2.add_op("relu", [xg])
zg = g2.add_op("relu", [yg])
g2.add_output(zg)
g2.ops.reverse()
g2.validate()
```

```text
IRError: op relu uses relu_0 before it is defined
```

这条错误说明 `validate()` 的 `op_index` 是增量构建的：扫描到第二个 op 时，第一个 op 已经被记录，但被反转后，使用者的位置跑到生产者前面，所以 producer 不在 `op_index` 里。

## 底层机制

### 2.8 身份、哈希与 `id()`

`Value` 和 `Op` 都是 `@dataclass(eq=False)`。这意味着：

- `v1 == v2` 比较的是对象身份，不是字段。
- `Value` 默认按对象身份哈希（`eq=False` 不生成字段相等的 `__eq__`，保留 `object.__hash__`）。
- 代码里大量使用 `id(v)` 作为键，避免自定义 `__hash__` 带来的歧义。

如果误用字段相等，两个 shape 相同、名字相同的值会被当成同一个，SSA 检查和用户列表都会出错。读这段代码时，看到 `id(v)` 就要意识到：它在做对象身份比较。

### 2.9 producer 与 users 的双向关系

`add_op` 同时维护两边：

```python
out.producer = op
for v in inputs:
    v.users.append(op)
```

`recompute_users()` 可以从头重建 users：

```python
for v in self.values:
    v.users = []
for op in self.ops:
    for v in op.inputs:
        v.users.append(op)
```

当 Pass 修改了 `op.inputs`，必须调用 `replace_all_uses` 或 `recompute_users`，否则 users 会过期。`remove_ops` 删除 op 后也会重算 users。

`replace_all_uses(old, new)` 会替换所有 op 的输入和图的输出，然后重算 users。它是常量折叠、CSE（Common Subexpression Elimination，公共子表达式消除）、代数化简的共同基础设施。

### 2.10 add_output 的重命名与重标记

`add_output` 不只是把值塞进列表：

```python
if name is not None:
    value.name = name
if value.kind != "constant":
    value.kind = "output"
```

这解释了两个现象：

- Ch 0 里 `r` 在 dump 中变成 `out`，因为输出名覆盖了原名字。
- 输出不再标记为 `temp`，代码生成可以用 `kind` 区分输入、常量和 arena 值。

但这里有一个边界：如果同一个 `Value` 既是输入又被某个 op 使用，`add_output` 把它的 `kind` 改成 `output` 后，`validate()` 会认为这个 op 的输入既不是 `input` 也不是 `constant`，又没有 producer，于是报“使用未定义值”。Ch 3 的元组输出例子会复现这个限制。

### 2.11 clone 与 id 重映射

`Graph.clone()` 创建一张新图，重新分配所有 `Value` 和 `Op` 的 id。它通过 `mapping: dict[int, Value]` 把旧值映射到新值（核心循环）：

```python
for op in self.ops:
    ins = [mapping[id(v)] for v in op.inputs]
    out = g.add_op(op.kind, ins, dict(op.attrs), name=op.output.name, shape=op.output.type.shape)
```

`clone()` 的用途是给 Pass 做“改动前快照”。它不会复制 `users`，新图的 users 由 `add_op` 重新建立。克隆后旧图和新图完全独立，修改一边不会影响另一边。

### 2.12 DCE（Dead Code Elimination，死代码删除）的反向活跃传播

`dce()` 从输出开始反向扫描（节选）：

```python
live = {id(v) for v in self.outputs}
for op in reversed(self.ops):
    needed = any(id(v) in live or v.users for v in op.outputs)
```

一个 op 被保留的条件是：它的输出是图输出，或者还有用户。保留后，把它的输入加入 live。反向扫描结束后，反转 `keep` 恢复拓扑序。

`prune_dead_values()` 再清理不再被任何 op、输入、输出引用的 `Value`。这解释了为什么常量折叠后，旧常量和旧 op 的输出会从 `dump()` 里消失。

## 进一步用法与变体

### 2.13 attrs 的用法

`sum` 的属性：

```python
s = g.add_op("sum", [y], {"axis": 1, "keepdims": False}, name="s")
```

`axis` 可以是负数，`infer_output_type` 会把它归一到 `[0, rank)`。`keepdims=True` 时保留长度为 1 的维度。属性会原样出现在 dump 里：

```text
%out = sum(y) {'axis': 1} : tensor<2>
```

### 2.14 用 dump 做差分调试

`dump()` 不包含常量数据，但包含 op 顺序、输入名、输出名、attrs 和类型。它适合做：

- Pass 前后对比。
- 两个前端（FX、手写图）产出的图对比。

如果怀疑常量值错了，单独打印 `v.data`；不要指望 `dump()` 显示权重。

## 对比与取舍

| 设计 | 本课程 | 真实编译器 |
| --- | --- | --- |
| 控制流 | 无，扁平拓扑序 | CFG（Control Flow Graph，控制流图）、region、block |
| 值表示 | Python 对象 + `id()` | SSA 值编号、use-def 链 |
| 多输出 | 不支持 | 常见，如 `tuple`、`call` |
| 类型 | 静态 shape、float32 | 动态 shape、多种 dtype、layout |
| 验证 | 结构与顺序 | dominance、类型系统、side effect |
| 序列化 | `dump()` 文本 | 二进制/文本 IR、版本兼容 |

扁平图的好处是每个组件都能在一屏内读懂；代价是无法表达分支、循环和动态形状。Ch 21 的 LLVM IR 会展示 SSA + CFG 的完整形态。

另一个取舍是“一个 op 一个输出”。它让 `Value.producer` 保持单值，简化了 SSA 检查；代价是像 `split`、`topk` 这样的多输出算子无法直接表达。

## 踩雷集锦

1. **手动构造 `Op` 却忘记设置 `producer`。** `validate()` 会报 `value ... has a stale producer` 或“使用未定义值”。用 `add_op`，不要手搓。
2. **反转 `ops` 或插入 op 到中间。** 拓扑序不是“看起来对”就行，`validate()` 只认扫描顺序。插入 op 后必须重新排序或重建。
3. **复用同一个 `Value` 作为两个 op 的输出。** 第二次会被 `seen` 捕获：`value y is defined more than once`。
4. **用 `==` 比较 `Value`。** `eq=False` 下这是身份比较，两个字段相同的值不相等；用 `is` 或 `id()` 明确表达意图。
5. **在 `add_output` 之后继续把该值当 `temp`。** 它的 `kind` 已经变成 `output`；如果再被其他 op 使用，`validate()` 可能把它当成“没有 producer 的非输入值”。

## 进阶

- 给 `Value` 加 `source: str` 字段，记录前端节点名，让报错能定位到 FX 节点。
- 写一个 `to_dot()`，把 `Value` 和 `Op` 画成 Graphviz 图，观察 use-def 关系。
- 给 `validate()` 增加“每个 op 的输出类型等于 `infer_output_type`”的二次检查，防止手动改类型。
- 实现一个简单的文本解析器，把 `dump()` 的输出读回 `Graph`，练习 IR 的往返一致性。

## 动手练习

1. **建图并 dump。** 构造 `x(2,3) -> add(b) -> relu -> sum(axis=1)`，确认 dump 的 op 序号、类型和 attrs。
2. **破坏 SSA。** 手动把同一个 `Value` 放进两个 op 的 `outputs`，运行 `validate()`，记录完整错误信息。
3. **破坏拓扑序。** 反转 `ops`，运行 `validate()`，解释 `op_index` 为什么抓得到。
4. **显式 shape 冲突。** 给 `add` 传 `shape=(2, 2)`，确认错误信息里的 inferred 和 explicit 两个 shape。
5. **观察 DCE。** 建一个带死分支的图，调用 `g.dce()`，打印返回值和新 dump，确认死 op 和死值都消失。

## 本章重点

- `TensorType` 是不可变的 shape + dtype 描述，`strides` 由 shape 推出。
- `Value` 的四种 `kind` 决定它在内存规划和代码生成中的角色。
- `Op` 当前单输出，`attrs` 承载 `axis`、`keepdims` 等属性。
- `add_op` 同时完成类型推导、SSA 输出创建、users 维护和拓扑追加。
- `validate()` 检查所有权、SSA、生产者顺序、输出存在。
- `dump()` 是 Pass 前后对比和排错的第一手材料。

## 自我检核

- [ ] 我能解释 `Value` 的 `producer` 和 `users` 分别由谁维护。
- [ ] 我能说出 `validate()` 的五类检查。
- [ ] 我知道为什么 `validate()` 用 `id(v)` 而不是 `v == other`。
- [ ] 我能复现“shape mismatch for add”和“graph has no outputs”。
- [ ] 我能解释 `add_output` 为什么会改名字和 `kind`。
- [ ] 我知道 `dce()` 为什么反向扫描，以及输出值为什么必须保留。

## 延伸阅读

- **`code/mini_ai/ir.py`**：读 `Value`、`Op`、`Graph.add_op`、`Graph.validate`、`Graph.dce`；学 IR 的最小实现；关联本章所有代码片段。
- **`code/mini_ai/interpreter.py`**：读 `evaluate_graph` 如何用 `Value` 作为 env 键；学 IR 如何被执行；关联 Ch 5 的语义金标准。
- **`code/mini_ai/memory.py`**：读 `plan_memory` 如何用 op index 和 `last_use` 算生命周期；学 IR 字段如何被下游消费；关联 Ch 12。
- **LLVM Language Reference 的 “SSA” 与 “Functions” 章节**：读 SSA 值和基本块的定义；学工业 IR 如何扩展本章模型；关联 Ch 21。
- **《Engineering a Compiler》第 4 章**：读中间表示与控制流图；学扁平图与 CFG 的差异；关联本章的“对比与取舍”。

[下一章：前端 FX](./03-frontend-fx.md)
