# 第 6 章 Verifier（验证器）与 shape/dtype 推导

> **目标**：读完本章，你能说清 `Graph.validate` 到底检查什么、不检查什么；能解释静态形状推导发生在建图的哪一步；能复现十种常见非法图的报错；能写一个比内置 `validate` 更严格的类型复检器；能说明为什么在跑 pass 之前必须先验证，以及为什么“验证过”不等于“永远正确”。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；`numpy 2.4.6`；`torch 2.14.1+cpu`。本章只用标准库、NumPy 和本仓库代码：
>
> ```powershell
> $env:PYTHONPATH = "compilers\mini_ai_compiler\code"
> $env:PYTHONIOENCODING = "utf-8"
> & ".scratch/aic-env\Scripts\python.exe" -c "from mini_ai.ir import Graph; print(Graph())"
> ```

## 为什么需要 Verifier

一张图从 `from_torch` 出来，会经过常量折叠、代数化简、CSE、融合，再进入内存规划和代码生成。后面每一步都建立在一组假设上：

- 每个值只定义一次（SSA，static single assignment）。
- 算子只能使用在它之前定义的值（拓扑序）。
- 每个值的类型与产生它的算子一致。
- 图至少有一个输出，输出属于这张图。

如果这些假设被破坏，后面的代码不会给出“图不合法”这种清晰错误。它会给出越界写、错误的 buffer 复用、或者一条看起来能跑但数值错误的 C 代码。验证器（verifier）的职责，是在这些假设被破坏的那一刻就把错误拦住，并给出能定位的报错。

本仓库没有单独的 `Verifier` 类。验证由两个机制分担：

1. **构造期推导**：`Graph.add_op` 调用 `infer_output_type`，算出输出类型，并与显式传入的形状比对。
2. **事后结构校验**：`Graph.validate` 检查 SSA、拓扑序、归属与输出存在性。

理解这两者的分工，比记住某个函数名更重要。

## 先建立直觉：两道门，不是一道

```text
 from_torch / 手工建图
        |
        |  add_op(kind, inputs, attrs, shape=...)
        |    infer_output_type(...)  ---> inferred
        |    compare(inferred, explicit)
        v
   Graph (values / ops / inputs / outputs)
        |
        |  validate()        <-- 第一道结构门
        v
   PassManager.run
        |    pass 1 -> validate()
        |    pass 2 -> validate()
        |    ...
        v
   plan_memory / codegen
        |
        |  解释器或后端执行时还有形状检查
        v
     数值结果
```

三个观察：

- 类型门在**建图时**关闭。一个非法 `add` 根本进不了 `graph.ops`。
- 结构门在**使用前**关闭。`evaluate_graph` 第一行就是 `graph.validate()`；`PassManager` 在每个 pass 之后调用它。
- 两道门都不检查数值语义。`relu` 实现成 `abs`，形状全对，验证器不会说话。数值只能靠解释器与金标准比较。

## 核心内容

### 6.1 TensorType 的不变量

类型定义很短，但把几个边界钉死了（`rank` 与 `__str__` 省略）：

```python
@dataclass(frozen=True)
class TensorType:
    shape: tuple[int, ...]
    dtype: str = "float32"

    def __post_init__(self) -> None:
        if self.dtype != "float32":
            raise IRError(f"only float32 is supported, got {self.dtype}")
        if any(not isinstance(d, int) or d < 0 for d in self.shape):
            raise IRError(f"invalid shape: {self.shape}")

    @property
    def numel(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n

    @property
    def strides(self) -> tuple[int, ...]:
        """Row-major (C-order) strides in elements."""
        out: list[int] = []
        acc = 1
        for d in reversed(self.shape):
            out.append(acc)
            acc *= d
        return tuple(reversed(out))
```

要点：

- **只有 float32**。dtype 是字符串，但只接受一个值。传 `"float64"` 会在构造时抛 `IRError: only float32 is supported, got float64`。
- **维度必须是非负整数**。`0` 合法，`-1` 非法，`2.0` 非法。空张量是合法形状，`numel == 0`。
- **rank 0 是标量**。`TensorType(())` 的 `numel` 是 1，字符串是 `tensor<scalar>`。
- **strides 是行主序、以元素为单位**。`(2, 3)` 的 strides 是 `(3, 1)`；`(4, 1, 5)` 是 `(5, 5, 1)`。第 11 章会用它做布局分析。
- **`frozen=True`**。类型对象不可变，因此可以作为字典键；`Value.type` 可以整体替换，但不能原地改 `shape` 元组的内容。

### 6.2 infer_output_type：按算子家族分派

形状推导是一张分派表。核心规则：

| 算子家族 | 输入约束 | 输出形状 | 错误点 |
| --- | --- | --- | --- |
| `add` / `sub` / `mul` / `div` / `add_relu` | 恰好 2 个 | 广播后的形状 | 不可广播 |
| `relu` / `exp` | 恰好 1 个 | 与输入相同 | 输入个数 |
| `matmul` / `matmul_bias` / `matmul_bias_relu` | 2 或 3 个，前两个二维 | `(a[0], b[1])` | 非二维、内维不等、偏置不可广播 |
| `sum` | 1 个 | 按 `axis` / `keepdims` 归约 | 轴越界、轴不是 int |
| `softmax` | 1 个 | 与输入相同 | 只检查输入个数，不检查轴 |

节选 `matmul` 与 `sum` 的实现：

```python
if kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
    if len(inputs) not in {2, 3}:
        raise IRError(f"{kind} expects 2 or 3 inputs, got {len(inputs)}")
    a, b = inputs[0].shape, inputs[1].shape
    if len(a) != 2 or len(b) != 2:
        raise IRError(f"{kind} currently supports 2-D matrices, got {a} and {b}")
    if a[1] != b[0]:
        raise IRError(f"matmul inner dimensions differ: {a} @ {b}")
    out_shape = (a[0], b[1])
    if len(inputs) == 3:
        # Bias must broadcast to the output shape.
        broadcast_shape(inputs[2].shape, out_shape)
    return TensorType(out_shape)
if kind == "sum":
    if len(inputs) != 1:
        raise IRError("sum expects 1 input")
    ...  # shape / axis / keepdims 的取值略
    if not isinstance(axis, int):
        raise IRError(f"sum axis must be int or None, got {axis!r}")
    if axis < 0:
        axis += len(shape)
    if not (0 <= axis < len(shape)):
        raise IRError(f"sum axis {axis} out of range for {shape}")
```

两个容易忽略的细节：

- `matmul` 的偏置不是“必须一维”。它走 `broadcast_shape`，所以 `(4, 16)` 的偏置也能配上 `(4, 16)` 的输出。前端生成的 `add` 偏置通常是 `(16,)`，但 IR 不强制。
- `sum` 的 `axis` 只接受 `int` 或 `None`。`(0, 1)` 这种元组会被拒绝，报 `sum axis must be int or None, got (0, 1)`。前端的 `torch.sum(x, dim=(0, 1))` 在 `frontend.py` 里就被限制为单轴。

`softmax` 的分支只有两行：检查输入个数，返回 `inputs[0]`。它没有读 `axis`，因此越界轴要等到执行时才报错。这是第 5 章 5.11 留下的缺口。

### 6.3 broadcast_shape：尾部对齐

广播规则集中在一个函数里：

```python
def broadcast_shape(*shapes: tuple[int, ...]) -> tuple[int, ...]:
    """Numpy-style broadcasting for static shapes."""
    if not shapes:
        return ()
    rank = max(len(s) for s in shapes)
    padded = [(1,) * (rank - len(s)) + s for s in shapes]
    out: list[int] = []
    for dims in zip(*padded):
        non_one = {d for d in dims if d != 1}
        if len(non_one) > 1:
            raise IRError(f"shapes are not broadcastable: {shapes}")
        out.append(non_one.pop() if non_one else 1)
    return tuple(out)
```

本机实跑（含常被记反的几组）：

```text
(4, 1) + (1, 3) -> (4, 3)
() + (2, 3)     -> (2, 3)
(0, 3) + (1, 3) -> (0, 3)
(2, 3) + (3,)   -> (2, 3)
(8, 4) + (4,)   -> (8, 4)
(2, 3) + (2,)   -> IRError: shapes are not broadcastable: ((2, 3), (2,))
(4, 8) + (4,)   -> IRError: shapes are not broadcastable: ((4, 8), (4,))
```

逐条解释：

- `(4, 1)` 与 `(1, 3)`：同 rank，逐维取非 1 值，得到 `(4, 3)`。
- `()` 与 `(2, 3)`：标量前导补成 `(1, 1)`，结果 `(2, 3)`。
- `(0, 3)` 与 `(1, 3)`：`0` 不是 `1`，保留 `0`。空张量可以参与广播，结果 `numel == 0`。
- `(2, 3)` 与 `(3,)`：`(3,)` 补成 `(1, 3)`，最后一维相同，合法。
- `(8, 4)` 与 `(4,)`：`(4,)` 补成 `(1, 4)`，最后一维是 `4`，合法。
- `(2, 3)` 与 `(2,)`：`(2,)` 补成 `(1, 2)`，最后一维 `3` 与 `2` 冲突，失败。
- `(4, 8)` 与 `(4,)`：`(4,)` 补成 `(1, 4)`，最后一维 `8` 与 `4` 冲突，失败。偏置要写 `(8,)`，不是 `(4,)`。

这组例子说明：广播看的是**最后一维**，不是行数。写文档或测试前先跑一行 `broadcast_shape` 确认，别凭直觉。

### 6.4 Graph.validate：六项结构检查

`validate` 的源码如下：

```python
def validate(self) -> None:
    """Check SSA, ownership, topological order, and output existence."""
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

它检查六件事：

1. 每个 op 的输入值属于 `graph.values`。
2. 非 input、非 constant 的输入值，其 producer 已经出现在更早的 op 里。
3. 每个输出值只被定义一次。
4. 输出值的 `producer` 指针指向当前 op（不是克隆前的旧 op）。
5. 图至少有一个输出。
6. 每个输出值属于 `graph.values`。

本机复现的报错：

| 构造方式 | 报错 |
| --- | --- |
| 用未定义的值 | `op exp uses relu_0 before it is defined` |
| 同一个 op 重复出现 | `value relu_0 is defined more than once` |
| 没有输出 | `graph has no outputs` |
| 输出的 producer 被置空 | `value relu_0 has a stale producer` |
| 输出不属于图 | `graph output ... does not belong to the graph` |

注意报错里用的是值的**名字**和算子的**种类**，没有 op 下标。图小的时候够用，图大时需要自己补上下文。

### 6.5 add_op 的构造期检查

`add_op` 不只是“追加一个节点”，它同时是类型门：

```python
attrs = dict(attrs or {})
inferred = infer_output_type(kind, [v.type for v in inputs], attrs)
explicit = TensorType(tuple(shape), dtype) if shape is not None else inferred
if explicit.shape != inferred.shape:
    raise IRError(f"shape mismatch for {kind}: inferred {inferred.shape}, explicit {explicit.shape}")
...
```

三个真实报错：

```text
explicit: shape mismatch for relu: inferred (2, 3), explicit (2, 4)
arity: add expects 2 inputs, got 1
bias: shapes are not broadcastable: ((5,), (2, 2))
```

这三条分别对应：前端给的 shape 与推导不一致、参数个数不对、偏置无法广播到 matmul 输出。它们都在建图时抛出，坏图不会被追加到 `graph.ops`。

为什么要同时保留“推导”和“显式 shape”？前端用 `ShapeProp` 拿到真实形状，推导用 IR 规则再算一遍。两者不一致，说明前端映射或 IR 规则至少有一处错。让它们互相校验，比单方面相信任何一方更安全。

### 6.6 常见非法图的复现清单

下面这些例子都可以在 REPL 里复现。建议亲手跑一遍，记住报错文本：

```python
import numpy as np
from mini_ai.ir import Graph, TensorType

g = Graph()
x = g.add_input("x", (2, 3))

g.add_op("add", [x, g.add_const("b", np.zeros((4,), np.float32))])
# IRError: shapes are not broadcastable: ((2, 3), (4,))

g.add_op("matmul", [x, g.add_const("w", np.zeros((4, 5), np.float32))])
# IRError: matmul inner dimensions differ: (2, 3) @ (4, 5)

g.add_op("sum", [x], {"axis": (0, 1)})
# IRError: sum axis must be int or None, got (0, 1)

g.add_op("neg", [x])
# IRError: unsupported op: neg

TensorType((2,), "float64")
# IRError: only float32 is supported, got float64

```

还有两类不是靠单个 `add_op` 触发的：

- **拓扑序被破坏**：建好图后 `g.ops.reverse()`，再 `validate()`，得到 `op exp uses relu_0 before it is defined`。
- **类型被事后篡改**：把 `op.output.type` 改成另一个形状，`validate()` 仍然通过，解释器在输出检查处报 `op relu produced (2, 3), IR says (2, 4)`。

第二类是本节的转折点：验证器不是“重新推导所有类型”，它信任 `Value.type` 已经正确。

### 6.7 validate 挡不住什么

三类缺口，按危害从大到小：

**缺口一：不重新推导类型。** 篡改 `Value.type` 后 `validate` 通过。真实输出：

```text
validate after type mutation -> no error
interpreter after type mutation -> ValueError: op relu produced (2, 3), IR says (2, 4)
```

**缺口二：不检查输出是否还有活的 producer。** 下面这个坏 pass 直接删掉最后一个 op，输出值仍指向被删的 op：

```python
g = build_graph()              # x -> relu -> exp，输出 exp
def bad_pass(graph, **_):
    graph.ops.pop()
    return 1
PassManager().add("bad", bad_pass).run(g)
```

本机结果：`no error`，输出仍打印为 `Value(relu_1, tensor<4>, output)`。原因是 `validate` 只检查输出值属于 `values`，没有检查它的 `producer` 是否还在 `ops` 里。这类图进入代码生成后会出现“找不到生产者”的崩溃，或者更糟，读到未初始化的 buffer。

**缺口三：不校验 attrs 的语义。** `softmax` 的越界 `axis` 就是例子。`sum` 校验了轴，`softmax` 没有。attrs 校验目前是“各算子自己负责”，没有统一 schema。

补上第一类缺口的最小实现是“类型复检器”：

```python
from mini_ai.ir import IRError, infer_output_type


def strict_verify(graph):
    graph.validate()
    for i, op in enumerate(graph.ops):
        inferred = infer_output_type(op.kind, [v.type for v in op.inputs], op.attrs)
        if inferred != op.output.type:
            raise IRError(f"op #{i} {op.kind}: declared {op.output.type}, inferred {inferred}")
```

对篡改类型的图运行，本机输出：

```text
strict: op #0 relu: declared tensor<2, 4>, inferred tensor<2, 3>
```

它多了 op 下标和两侧类型，定位比内置报错直接。代价是每个 op 重新推导一次，复杂度 O(n)，对几百个 op 的图可以忽略。

补第二类缺口要检查输出的 producer：对所有非 input/constant 输出，断言 `v.producer is not None` 且 `id(v.producer) in {id(op) for op in graph.ops}`。第 7 章的 7.7 节会再看到这个缺口。

第三类缺口需要给每个算子写 attrs 校验，或者把校验并入 `infer_output_type`。第 6.2 节的 `sum` 分支就是这种做法；`softmax` 应该补上同样的检查。

### 6.8 为什么在 pass 之前先验证

Pass 的所有算法都假设图是合法的。举三个具体依赖：

- `common_subexpression_elimination` 用输入值的 `id` 做键。如果输入值不属于图，键仍然能算出来，但两个本应不同的值可能指向同一个对象，导致错误合并。
- `dce` 反向扫描 `graph.ops`，依赖 `producer` 指针判断“这个值是否由前面的 op 产生”。拓扑序乱了，存活集合就错了。
- `fuse_ops` 通过 `producer` 找 `matmul -> add -> relu` 链。producer 指针过期，匹配会静默失败，或者匹配到错误的链。

本机实验：把 `relu -> exp` 的图反转 op 顺序，直接跑 `standard_pipeline()`：

```text
pipeline on reversed graph: op exp uses relu_0 before it is defined
```

错误是在第一个 pass 之后由 `PassManager.run` 的 `graph.validate()` 抛出的，不是在 pass 之前。`constant_fold` 在坏图上什么也没做（没有全常量输入），然后验证器才发现问题。这个顺序说明：`PassManager` 保证“每个 pass 之后图合法”，但不保证“进入第一个 pass 时图合法”。调用方要在 `run` 之前自己 `validate()`。

推荐先 `graph.validate()`，再 `standard_pipeline().run(graph)`，不要依赖 `PassManager` 替你检查入口。

如果 pass 可能引入新类型错误（比如手工构造 `Op` 而不走 `add_op`），在 `run` 之后再用 `strict_verify`。`PassManager` 内置的 `validate` 只覆盖结构，不覆盖类型复检。

## 底层机制

### 6.9 为什么用 id() 而不是 ==

`Value` 和 `Op` 都是 `@dataclass(eq=False)`，`==` 退化为对象身份。验证器进一步显式用 `id(v)` 建集合，原因是：

- 克隆图后，新旧值的字段可能完全相同（同名、同类型），但它们是不同的 SSA 值。用 `==` 会把它们混为一谈。
- `id()` 是 O(1) 哈希，集合操作快；代价是 `id` 只在对象存活期间有效，验证器不跨调用保存这些集合。

如果要把验证结果缓存到磁盘，不能存 `id`；要存名字加类型，或者给值分配稳定编号。

### 6.10 producer 与 users 的维护

`add_op` 在追加 op 时做三件事：设置 `out.producer = op`、给每个输入的 `users` 追加当前 op、把 op 追加到 `ops`。

`users` 是冗余索引，方便 pass 判断“这个值还有没有人用”。它不是验证器的检查项，但 `dce` 依赖它。任何手工修改 `ops` 或 `inputs` 的代码，都要调用 `graph.recompute_users()`，否则 `users` 会过期。验证器不检查 `users` 的一致性，这是另一个“validate 不是万能”的例子。

### 6.11 静态形状的边界

当前 IR 是静态形状（static shape）：

- 没有符号维度，没有 `-1`，没有动态 batch。
- 所有形状在 `add_op` 时确定，之后不能改。
- `sum(axis=None)` 得到 rank 0；`keepdims=True` 得到全 1 形状。
- 常量 rank 上限是 4。这个限制来自前端和 codegen 的简化，不是数学限制。超过时 `add_const` 报 `constants with rank > 4 are not supported`。
- 零尺寸维度合法，`numel == 0`；内存规划会分配 0 字节或按对齐后 0 处理，第 13 章会看到。

动态形状需要另一套类型系统（符号表达式、约束求解），本课程不展开。理解静态边界有助于判断哪些 bug 是“当前设计必然存在”的，而不是实现疏忽。

### 6.12 错误定位的三个层次

1. **算子级**：报错包含 `op.kind` 和值名，例如 `op exp uses relu_0 before it is defined`。
2. **索引级**：`strict_verify` 加上 `#i`，能直接跳到 `graph.ops[i]`。
3. **pass 级**：`PassManager` 知道当前 pass 名，但 `validate` 的异常不带 pass 名。在 `run` 里用 `try/except IRError` 包住 pass 与 `validate`，重新抛出 `IRError(f"pass {name} produced an invalid graph: {exc}")`，报错会变成 `pass fuse_ops produced an invalid graph: ...`。

## 进一步用法与变体

- **收集全部错误而不是遇到第一个就抛**。把 `validate` 改写成返回 `list[str]` 的 `collect_errors`，适合批量检查一堆图。注意：有些错误会级联（一个值未定义会导致后续所有使用者都报错），需要去重或只报根因。
- **给 attrs 建 schema**。为每个算子声明允许的键、类型、默认值和范围，验证器统一检查。这样 `softmax` 的轴越界不会漏。
- **验证前置到前端出口**。`from_torch` 已经在最后调用 `graph.validate()`；可以再加 `strict_verify`，把“ShapeProp 与 IR 推导不一致”的错误提前到抓图阶段。
- **把类型复检放进测试**。每次跑 pass 测试时调用 `strict_verify`，比只比较输出更容易发现“数值碰巧对、类型已错”的情况。
- **对克隆图做 dry run**。`graph.clone()` 保留名字与类型，但产生全新的 `Value`/`Op` 对象。在克隆上跑实验性 pass，再用 `strict_verify` 检查，不会污染原图。

## 对比与取舍

| 机制 | 触发时机 | 检查内容 | 成本 | 盲区 |
| --- | --- | --- | --- | --- |
| `add_op` 推导 | 建图时 | 算子 arity、广播、matmul 内维、sum 轴 | 每 op 一次 | 不检查事后篡改 |
| `Graph.validate` | 使用前 / 每 pass 后 | SSA、拓扑序、归属、输出存在 | O(values + ops) | 类型、attrs、users、输出 producer 存活 |
| `strict_verify` | 任意时刻 | 上述 + 类型复检 | O(ops × 推导) | 语义、数值 |
| 解释器形状检查 | 执行时 | 输入形状、每 op 输出形状 | 每 op 一次 | 声明与实现一起错 |
| 后端运行检查 | 执行时 | 依赖具体后端 | 视实现 | 可能根本没有 |

取舍的核心是“在哪一层失败”：

- 只在建图时检查，坏图可以通过 `clone`、手工 `Op`、pass 绕过。
- 只在执行时检查，错误离源头太远，定位成本高。
- 每步都做最严格的检查，编译时间上升；对 5 个 op 的图无所谓，对上千 op 的图要算账。

本课程的选择是：构造期推导 + 结构验证 + 解释器形状检查，`strict_verify` 作为可选加强。这个组合覆盖了绝大多数前端与 pass 错误，同时保持实现可读。

## 踩雷集锦

1. **`validate` 通过就以为类型正确**。它不重新推导。手工改 `Value.type`，或 pass 用 `_new_value` 直接造值，都能骗过它。用 `strict_verify` 补。
2. **删 op 但没修输出**。`graph.ops.pop()` 后 `validate` 可能通过，因为输出值还在 `values` 里。进入 codegen 才炸。检查输出 producer 是否存活。
3. **以为 `(2, 3)` 和 `(3,)` 不能广播**。它们可以；不能的是 `(2, 3)` 和 `(2,)`。写文档或测试前先跑 `broadcast_shape`，别凭直觉。
4. **以为 `softmax` 的 axis 会被校验**。当前不会。`add_op` 成功、`validate` 成功、解释器抛 `AxisError`。给新算子写推导时，把 attrs 校验一起写。
5. **在 pass 里手工改 `inputs` 后忘记 `recompute_users()`**。`dce` 会看到过期的 users，删错或漏删。验证器不查这个不一致，属于“结构合法但索引过期”的隐形错误。

## 进阶

- **实现 `collect_errors(graph) -> list[str]`**。要求：不抛异常；每条错误带 op 下标；对同一根因只报一次。写 10 个坏图做测试。
- **给 `infer_output_type` 补 attrs 校验**。先补 `softmax` 的轴范围，再考虑把 `sum` 的 `keepdims` 类型也钉死（当前 `bool(...)` 会接受任意真值）。
- **实现 `verify_fixed_point(graph)`**。在 `standard_pipeline` 的每个 pass 后跑 `strict_verify`，找出哪个 pass 最先引入类型不一致。
- **比较 `ShapeProp` 与 IR 推导的差异**。写一个前端测试，遍历 FX 节点，把 `tensor_meta.shape` 与 `Value.type.shape` 对比，报告第一处不同。这能在抓图阶段发现映射 bug。
- **给错误加源码位置**。当前 `Value.name` 来自 FX 节点名，可以把它和 `node.meta` 里的调试信息关联起来，报错时显示原始 Python 行号。

## 动手练习

1. 复现 6.6 的七条报错，把每条报错归类为“arity / 广播 / 轴 / 未知算子 / dtype / rank 限制”。哪一类是 `add_op` 抛的，哪一类是 `TensorType` 抛的？
2. 构造一张合法图，篡改中间值的 `type`，分别运行 `validate()`、`strict_verify()`、`evaluate_graph()`，记录三者的行为差异。
3. 写一个坏 pass，删掉一个仍被下游使用的 op，确认 `PassManager` 能抓到；再写一个坏 pass，删掉最后一个 op 但不改输出，确认 `PassManager` 抓不到。解释两者的区别。
4. 给 `strict_verify` 加输出 producer 存活检查，合并成 `verify_all(graph)`，并用 6.7 的两个坏图验证。
5. 把 `softmax` 的轴校验补进 `infer_output_type`（可以只在练习副本里改），验证 `axis=5` 在 `add_op` 阶段就被拒绝，并写一条回归测试。
6. 用 `graph.clone()` 复制一张图，在克隆上跑 `strict_verify`，确认原图的 `id` 与克隆图不同、但输出数值一致。

## 本章重点

- 验证由两部分组成：`add_op` 的构造期类型推导，以及 `Graph.validate` 的事后结构检查。
- `TensorType` 固定 float32、非负整数维度、行主序 strides；rank 0 是标量，零尺寸合法。
- `broadcast_shape` 尾部对齐，`1` 可扩张；`(2, 3)` 与 `(3,)` 合法，与 `(2,)` 不合法。
- `validate` 检查六项：值归属、拓扑序、重复定义、producer 指针、输出存在、输出归属。
- `validate` 不重新推导类型，不检查 attrs 语义，不检查输出 producer 是否存活，不检查 users 一致性。
- `strict_verify` 用 `infer_output_type` 复检每个 op，能抓住类型篡改，并给出 op 下标。
- 在跑 pass 之前手动 `validate()`；`PassManager` 只在每个 pass 之后验证。
- 形状与 dtype 错误越早发现越便宜；数值错误只能靠金标准比较。

## 自我检核

- [ ] 我能背出 `validate` 的六项检查，并各举一个反例。
- [ ] 我能解释 `add_op` 为什么同时做推导和显式形状比对。
- [ ] 我能说出 `validate` 通过但解释器失败的一种构造方式。
- [ ] 我能写出 `strict_verify`，并解释它比 `validate` 多检查了什么。
- [ ] 我知道 `(2, 3)` 与 `(3,)` 可以广播，与 `(2,)` 不可以。
- [ ] 我能解释为什么 `PassManager` 不能替代进入流水线前的手动验证。
- [ ] 我知道 `softmax` 的 axis 越界目前不会被建图阶段拦截。
- [ ] 我能说出至少两个 `validate` 不检查、但会导致后端出错的隐形不变量。

## 延伸阅读

- **MLIR Verifier 文档**（<https://mlir.llvm.org/docs/Verifier/>）：读“Verifier”与“Diagnostics”两节，理解工业级 IR 如何把验证拆成 op 级与 region 级。关联本章的两道门，以及“验证器不重新推导业务语义”的边界。
- **LLVM `Verifier.cpp`**（<https://github.com/llvm/llvm-project/blob/main/llvm/lib/IR/Verifier.cpp>）：读文件头的检查清单与 `visit*` 函数组织方式。关联本章 6.4 的六项检查，看看完整版多了多少不变量（dominator、PHI、类型合法性）。
- **NumPy Broadcasting**（<https://numpy.org/doc/stable/user/basics.broadcasting.html>）：读规则与反例。关联 6.3 的 `broadcast_shape`，特别是尾部对齐与前导补 1。
- **PyTorch FX `ShapeProp`**（<https://pytorch.org/docs/stable/fx.html#torch.fx.passes.shape_prop.ShapeProp>）：读它如何把示例输入的形状写回 `node.meta`。关联 6.5 的“显式 shape 与推导互相校验”。
- **《Engineering a Compiler》**（Cooper & Torczon）第 4 章“Semantic Analysis”：读符号表、类型检查与属性文法。关联本章的“构造期推导 vs 事后验证”分工，以及为什么编译器通常在 IR 构建时就固定类型。

---

下一章把验证器接进流水线：`PassManager` 如何组织 pass、如何定义“改变”、如何跑到固定点，以及一个 pass 必须满足哪些不变量。

[第 7 章 Pass 基础设施：PassManager、顺序、固定点与不变量](./07-pass-manager.md)