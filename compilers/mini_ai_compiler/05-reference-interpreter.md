# 第 5 章 参考解释器：用 NumPy 建立语义金标准

> **目标**：读完本章，你能独立读懂并修改 `code/mini_ai/interpreter.py`；能解释 `apply_op` 与 `evaluate_graph` 的分工；能说明 float32 边界、广播规则与容差选择对测试结论的影响；能把一个真实 MLP 的输出与 PyTorch 对齐，并在缺输入、错形状、错算子三类失败中快速定位。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；`torch 2.14.1+cpu`；`numpy 2.4.6`。命令在仓库根目录 `D:\Learning-with-Claude` 执行，使用已有虚拟环境 `.scratch/aic-env`：
>
> ```powershell
> $env:PYTHONPATH = "compilers\mini_ai_compiler\code"
> $env:PYTHONIOENCODING = "utf-8"
> & ".scratch/aic-env\Scripts\python.exe" -c "import mini_ai; print(mini_ai.__file__)"
> ```

## 为什么需要参考解释器

编译器里最容易出错的地方，不是“生成代码”，而是“判断生成得对不对”。当你把同一张计算图交给 NumPy、C 编译出的 DLL、LLVM JIT 三条路径时，必须有一条路径先被定义为“正确”。这条路径就是参考解释器（reference interpreter），它的输出是语义金标准（golden reference）。

参考解释器承担四个角色：

1. **后端的对照物**。C 后端、LLVM 后端、未来的 GPU 后端，全部与它比较。第 16 章到第 22 章的误差数字，都以它为准。
2. **常量折叠的执行器**。`passes.constant_fold` 直接调用 `apply_op`，因此“编译期算出的常量”和“运行期算出的临时值”走同一份算子实现，不会出现两套语义。
3. **调试的放大镜**。你可以逐 op 打印中间值，看到第几个 op 开始偏离。编译后的 DLL 没有这种粒度。
4. **前端抓图的验收标准**。`from_torch` 生成的图是否正确，第一步就是拿解释器跑一遍，与 PyTorch eager 对齐；这一步在 `tests/test_mini_ai.py` 里叫 `test_frontend_matches_torch`。

它不负责性能。用 NumPy 跑一张 4x8 的 MLP 花费的是微秒级，但和 C 后端的差距会随规模迅速拉开。把解释器当“尺子”，不要当“产品”。

## 先建立直觉：一张图、一个环境、一个循环

解释器的全部工作可以压缩成下面这张图。图是有向无环图（DAG, directed acyclic graph），环境（environment）是从 `Value` 到 NumPy 数组的映射，循环按拓扑序执行每个算子。

```text
 feed = {"x0": ndarray}
        |
        v
 +-------------------+       constants (Value.data)
 |  env: Value -> ndarray | <-----------+
 +-------------------+                  |
        |                               |
        |  for op in graph.ops:         |
        |      in  = [env[v] for v in op.inputs]
        |      out = apply_op(kind, in, attrs)
        |      check out.shape == op.output.type.shape
        |      env[op.output] = out
        v
 outputs = [env[v] for v in graph.outputs]
```

关键点只有三个：

- 执行顺序由 `graph.ops` 的列表顺序决定，这个顺序在构造时就是拓扑序（见第 2 章）。
- 值不靠名字查找，靠 `Value` 对象身份查找。名字只用于打印和 `feed`。
- 每个算子的输入输出都经过 `_as_float32`，解释器内部不存在 float64 的中间结果。

## 核心内容

### 5.1 五个入口

`interpreter.py` 对外的接口很小，先记住各自的职责，再看实现。

| 函数 | 输入 | 输出 | 典型用途 |
| --- | --- | --- | --- |
| `apply_op(kind, arrays, attrs)` | 算子名、NumPy 数组列表、属性字典 | 一个 NumPy 数组 | 单算子求值；常量折叠也用它 |
| `evaluate_op(op, env)` | `Op`、环境 | 一个数组 | 单步执行，便于写调试器 |
| `evaluate_graph(graph, feed, check_shapes=True)` | 图、按输入名索引的 feed | 按 `graph.outputs` 顺序的数组列表 | 全图求值，测试与对齐 |
| `evaluate_named(graph, feed)` | 同上 | `{output_name: array}` | 多输出图的可读结果 |
| `max_abs_error(a, b)` | 两个数组 | float | 误差度量，形状不同直接报错 |

`feed` 的键是输入 **名字**，不是位置。这一点在第 5.6 节会用一个失败例子展开。

### 5.2 apply_op：算子语义的唯一实现

`apply_op` 负责单算子求值。下面是核心分支的节选，`sub`、`mul`、`div`、`exp` 与 `add`、`relu` 同构，`add_relu` 是 `add` 后接 `relu`：

```python
def _as_float32(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float32)


def apply_op(kind: str, arrays: list[np.ndarray], attrs: dict) -> np.ndarray:
    a = [_as_float32(x) for x in arrays]
    if kind == "add":
        return _as_float32(a[0] + a[1])
    if kind == "relu":
        return _as_float32(np.maximum(a[0], np.float32(0.0)))
    if kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
        if a[0].ndim != 2 or a[1].ndim != 2:
            raise IRError(f"{kind} expects 2-D inputs, got {a[0].shape} and {a[1].shape}")
        out = _as_float32(a[0] @ a[1])
        if kind in {"matmul_bias", "matmul_bias_relu"}:
            out = _as_float32(out + a[2])
        if kind == "matmul_bias_relu":
            out = _as_float32(np.maximum(out, np.float32(0.0)))
        return out
    if kind == "sum":
        axis = attrs.get("axis", None)
        keepdims = bool(attrs.get("keepdims", False))
        return _as_float32(np.sum(a[0], axis=axis, keepdims=keepdims))
    if kind == "softmax":
        axis = int(attrs.get("axis", -1))
        x = a[0]
        shifted = x - np.max(x, axis=axis, keepdims=True)
        e = np.exp(shifted)
        return _as_float32(e / np.sum(e, axis=axis, keepdims=True))
    raise IRError(f"interpreter does not support op: {kind}")
```

逐条读，有几个设计决定值得记住：

- **入口和出口都做 float32 规整**。`a = [...]` 规整输入，返回值再规整一次。对 float32 数组 `np.asarray` 不复制；对 float64 输入会复制并降精度。
- **`relu` 用 `np.maximum(a[0], np.float32(0.0))`**。写 `0.0` 也可以，但显式 `np.float32(0.0)` 把常量精度钉死，避免 NumPy 在某些路径上提升到 float64。
- **`matmul` 家族是手动组合的**。`matmul_bias` 先矩阵乘、再加偏置；`matmul_bias_relu` 再套一层 ReLU。第 8 章会看到 `constant_fold` 直接复用这条路径，第 10 章会看到它成为融合目标。
- **`sum` 读 attrs，不读位置参数**。`axis=None` 表示全归约，`keepdims` 决定保留维度。形状推导必须与这里保持同一套规则，否则解释器会在 5.9 节的第二道检查里报错。
- **`softmax` 先减最大值**。这是数值稳定性处理：`exp` 的自变量不超过 0，避免 `exp(100)` 溢出成 `inf`。数学上等价，浮点上更稳。
- **未知算子直接抛 `IRError`**。这句报错是后端扩展时最常见的信号：新增了 IR 算子，却忘了补解释器。

`evaluate_op` 是把 `Op` 和环境拼到 `apply_op` 上的一层薄封装：

```python
def evaluate_op(op: Op, env: Mapping[Value, np.ndarray]) -> np.ndarray:
    return apply_op(op.kind, [env[v] for v in op.inputs], op.attrs)
```

### 5.3 evaluate_graph：从 feed 到 outputs

全图求值的实现如下：

```python
def evaluate_graph(
    graph: Graph,
    feed: Mapping[str, np.ndarray],
    *,
    check_shapes: bool = True,
) -> list[np.ndarray]:
    """Run a graph and return arrays in graph.outputs order.

    `feed` is keyed by input *name*.  The function does not mutate the graph.
    """
    graph.validate()
    env: dict[Value, np.ndarray] = {}
    for v in graph.values:
        if v.kind == "constant":
            assert v.data is not None
            env[v] = _as_float32(v.data)
    for v in graph.inputs:
        if v.name not in feed:
            raise KeyError(f"missing input {v.name!r}; got {sorted(feed)}")
        arr = _as_float32(feed[v.name])
        if check_shapes and arr.shape != v.type.shape:
            raise ValueError(f"input {v.name}: expected {v.type.shape}, got {arr.shape}")
        env[v] = arr
    for op in graph.ops:
        out = evaluate_op(op, env)
        expected = op.output.type.shape
        if check_shapes and out.shape != expected:
            raise ValueError(
                f"op {op.kind} produced {out.shape}, IR says {expected}"
            )
        env[op.output] = out
    return [env[v] for v in graph.outputs]
```

执行分四段：

1. `graph.validate()`：先做结构校验。它检查 SSA、拓扑序、值的归属和输出是否存在，不检查数值。第 6 章展开。
2. 常量入环境：遍历 `graph.values`，把所有 `kind == "constant"` 的值放进 `env`。常量来自前端的权重和偏置，也来自常量折叠的产物。
3. 输入入环境：按 `graph.inputs` 的顺序读 `feed`，名字缺失就报错，形状不符就报错。
4. 逐 op 执行：算出结果后，先与 `op.output.type.shape` 比对，再写入环境。最后按 `graph.outputs` 的顺序取值。

`evaluate_graph` 的不变量：函数不修改图，不修改 `feed` 里的数组。例外情况只有一个：如果 `feed` 里的数组已经是 float32 且算子实现是纯函数式的（当前全部是），它们只被读，不被写。

### 5.4 正常路径一：MLP 与 PyTorch 对齐

用第 3 章的 `from_torch` 抓一个两层 MLP，再用解释器跑一遍，与 PyTorch eager 比较：

```python
import numpy as np
import torch

from mini_ai import evaluate_graph, evaluate_named, from_torch
from mini_ai.interpreter import max_abs_error


class MLP(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.fc1 = torch.nn.Linear(8, 16)
        self.fc2 = torch.nn.Linear(16, 4)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(torch.relu(self.fc1(x)))


torch.manual_seed(7)
model = MLP().eval()
x = torch.randn(4, 8)
with torch.no_grad():
    expected = model(x).numpy()

graph = from_torch(model, (x,))
out = evaluate_graph(graph, {"x0": x.numpy()})[0]
print("dtype:", out.dtype, "shape:", out.shape)
print("max_abs_error:", max_abs_error(out, expected))
print("named keys:", list(evaluate_named(graph, {"x0": x.numpy()}).keys()))
print("first row interp:", np.array2string(out[0], precision=6))
print("first row torch :", np.array2string(expected[0], precision=6))
```

本机输出（种子 7）：

```text
interp dtype: float32 shape: (4, 4)
max_abs_error: 2.9802322387695312e-08
named keys: ['linear_1']
first row interp: [-0.012895  0.023677  0.095844  0.279658]
first row torch : [-0.012895  0.023677  0.095844  0.279658]
```

这个例子说明三件事：

- 前端抓出来的图在语义上与 PyTorch 一致，误差 2.98e-08 远小于 float32 的机器精度量级（约 1.19e-07 的相对误差）。
- 输出名字来自 FX 节点名（`linear_1`），不是 `out0`；多输出时才会出现 `out0`、`out1`。打印精度 6 位时两边一样，但结论要来自误差比较，不是“看起来相同”。

### 5.5 正常路径二：广播加归约

第二个例子覆盖广播（broadcasting）和归约（reduction），这两类语义在 C 后端里最容易写错：

```python
import numpy as np

from mini_ai import evaluate_graph
from mini_ai.ir import Graph

g = Graph()
x = g.add_input("x", (2, 3))
b = g.add_const("b", np.array([1, 2, 3], np.float32))
y = g.add_op("add", [x, b])
r = g.add_op("relu", [y])
s = g.add_op("sum", [r], {"axis": 1})
g.add_output(s)

print(g.dump())
out = evaluate_graph(g, {"x": np.array([[-1, 0, 1], [2, -3, 4]], np.float32)})[0]
print("out:", out, out.dtype)
```

本机输出：

```text
inputs:
  x: tensor<2, 3>
constants:
  b: tensor<3> numel=3
ops:
  00 %add_0 = add(x, b) : tensor<2, 3>
  01 %relu_1 = relu(add_0) : tensor<2, 3>
  02 %sum_2 = sum(relu_1) {'axis': 1} : tensor<2>
outputs:
  sum_2: tensor<2>
out: [ 6. 10.] float32
```

手算核对：第一行 `[-1+1, 0+2, 1+3] = [0, 2, 4]`，ReLU 后不变，按行求和得 `6`；第二行 `[2+1, -3+2, 4+3] = [3, -1, 7]`，ReLU 后 `[3, 0, 7]`，求和得 `10`。解释器的结果与手算一致，说明广播沿最后一维对齐、`axis=1` 按行归约。

### 5.6 失败路径：缺输入、错形状、错算子

解释器的报错要足够具体，才能省掉一轮猜测。下面四个失败例子都来自本机实跑。

四个失败例子都来自本机实跑。缺输入：

```python
evaluate_graph(graph, {})
# KeyError: "missing input 'x0'; got []"
```

`got []` 是当前 `feed` 的键集合；传错名字时会显示差异，例如 `got ['x']`。

输入形状不符：

```python
evaluate_graph(graph, {"x0": np.zeros((2, 8), np.float32)})
# ValueError: input x0: expected (4, 8), got (2, 8)
```

报错包含输入名、期望形状、实际形状。输入形状是静态的，喂进来的数组必须完全一致，不涉及广播。

`apply_op` 层面的两个失败：

```python
from mini_ai.interpreter import apply_op

apply_op("matmul", [np.ones((3,), np.float32), np.ones((3, 2), np.float32)], {})
# IRError: matmul expects 2-D inputs, got (3,) and (3, 2)

apply_op("neg", [np.ones((2,), np.float32)], {})
# IRError: interpreter does not support op: neg
```

一维 matmul 在形状推导阶段也会被拒绝，报错是 `matmul currently supports 2-D matrices, got (3,) and (3, 2)`。两处检查重复是故意的：推导阶段防止建坏图，执行阶段防止绕过 `add_op` 手工拼图。未知算子会在第 8 章常量折叠时再次出现。

## 底层机制

### 5.7 float32 边界到底在哪

代码里 `_as_float32` 出现在三个位置：`apply_op` 的输入、`apply_op` 的每个返回值、`evaluate_graph` 的常量与输入。效果是：

- 前端传进来的 float64 数组会被静默降级。`evaluate_graph(graph, {"x0": np.zeros((4, 8), np.float64)})` 不会报错，输出仍是 float32。
- 常量在 `add_const` 时已经是 float32（`np.asarray(data, dtype=np.float32)`），`evaluate_graph` 再规整一次是幂等的。
- 中间结果不会“偷偷”升到 float64。NumPy 的 `float32 + Python float` 在某些版本会得到 float64，这里通过统一入口规避。

静默降级有代价：测试用 float64 造输入、用 float64 算期望，再比较解释器输出，会看到 1e-8 级别的差异。测试里显式写 `x.numpy().astype(np.float32)`，期望值也 `astype(np.float32)`。

### 5.8 环境为什么用 Value 作键

`Value` 是 `@dataclass(eq=False)`，因此它的 `__hash__` 和 `__eq__` 都来自对象身份。好处是：

- 两个同名但不同的 `Value` 不会互相覆盖。克隆图保留名字但产生新对象；按名字索引会让克隆图和原图串值。`replace_all_uses` 之后，用户指向新的 `Value`，环境查找也跟着走。

代价是：如果你手工构造一个“内容相同”的 `Value` 去查环境，查不到。解释器的所有入口都以图里的对象为准，这个限制不会影响正常使用。

### 5.9 两道形状检查，以及它们挡不住什么

第一道在 `evaluate_graph` 的输入循环：`arr.shape != v.type.shape`。第二道在每个 op 执行后：`out.shape != op.output.type.shape`。

第二道检查的价值在于捕获“算子实现与 IR 类型不一致”。正常情况下 `Graph.add_op` 已经用 `infer_output_type` 算过类型，二者一致。但如果你手工改过 `Value.type`，或者某个 pass 生成了错误类型，解释器会在第一个不匹配的 op 上停下：

```text
ValueError: op relu produced (2, 3), IR says (2, 4)
```

这个例子的构造方式是：建一张 `(2, 3) -> relu` 的图，然后把输出值的 `type` 改成 `TensorType((2, 4))`。`graph.validate()` 不会发现（它不重新推导类型），解释器会发现。第 6 章会把这个差异展开成“verifier 的能力边界”。

第二道检查挡不住的情况：

- 类型声明和实现都错，但错得一致。例如 `sum` 的 `axis` 在推导和执行里都写错，结果形状自洽，数值错。
- 精度问题。形状对、值有 1e-3 的偏差，解释器不会报错，只能靠与金标准比较。别名问题在当前实现里不存在（全部纯函数式），第 14 章引入 arena 后才需要处理。

### 5.10 容差怎么定

`max_abs_error(a, b)` 有三个行为要记住：

- 形状不同直接抛 `ValueError`，不返回 `inf`。形状错是结构错，不该混进数值误差。
- 空张量返回 `0.0`。这会让“空输出”轻松通过测试，所以测试里要另外断言形状和 `numel`。
- 含 `NaN` 时返回 `nan`。`nan <= tolerance` 为 `False`，测试失败，但报错只会显示 `nan`，不指出位置。

容差选择没有统一答案，取决于比较对象和数值范围：

| 比较对象 | 本机实测最大绝对误差 | 建议容差 | 说明 |
| --- | --- | --- | --- |
| 解释器 vs PyTorch（4x8 MLP） | 2.98e-08 | 1e-5 | 测试 `test_frontend_matches_torch` 使用 1e-5 |
| C 后端 vs PyTorch（同图） | 4.47e-08 | 1e-5 | 第 16 章起使用 |
| LLVM JIT vs PyTorch（同图） | 4.47e-08 | 1e-5 | 第 22 章 |
| 大数值中间结果 | 随量级增长 | 绝对 + 相对 | 用 `np.allclose(atol, rtol)` |

绝对容差适合输出在 `[-1, 1]` 附近的分类头；回归任务或未归一化的中间值，应该同时看相对误差。`runtime.assert_close` 的默认值是 `atol=1e-5, rtol=1e-5`，它先算 `max_abs_error` 再交给 `np.allclose`，两个信息都会出现在异常里。

### 5.11 归约与 softmax 的细节

`sum` 的 `axis` 允许 `None`、非负整数、负整数。形状推导会把负轴归一化后再算（`axis += len(shape)`），解释器则把原始 attrs 交给 NumPy，NumPy 自己也支持负轴。两条路径在合法输入上一致。

`softmax` 的形状推导只检查输入个数，直接返回输入类型，不校验 `axis` 是否越界。于是会出现这种图：

```python
g = Graph()
u = g.add_input("u", (2, 3))
sm = g.add_op("softmax", [u], {"axis": 5})
g.add_output(sm)
evaluate_graph(g, {"u": np.zeros((2, 3), np.float32)})
```

本机结果：

```text
AxisError: axis 5 is out of bounds for array of dimension 2
```

图能建出来，`validate()` 也通过，直到解释器执行才失败。这是第 6 章“shape 推导不等于完整校验”的一个具体缺口。修法有两个：在 `infer_output_type` 里校验 axis 范围，或者在 verifier 里统一检查 attrs。当前实现选择了前者能覆盖的部分（`sum`），漏掉了 `softmax`。

## 进一步用法与变体

### 逐 op 追踪

调试数值问题时，最有用的变体是把 `evaluate_graph` 的循环打开，打印每个 op 的形状和值域：

```python
import numpy as np

from mini_ai.interpreter import apply_op


def evaluate_with_trace(graph, feed):
    graph.validate()
    env = {v: np.asarray(v.data, np.float32) for v in graph.values if v.kind == "constant"}
    for v in graph.inputs:
        env[v] = np.asarray(feed[v.name], np.float32)
    for i, op in enumerate(graph.ops):
        out = apply_op(op.kind, [env[v] for v in op.inputs], op.attrs)
        print(f"{i:02d} {op.kind:12s} {str(out.shape):10s} min={out.min():8.4f} max={out.max():8.4f}")
        env[op.output] = out
    return [env[v] for v in graph.outputs]
```

在本章 5.5 的图上运行，本机输出：

```text
00 add          (2, 3)     min= -1.0000 max=  7.0000
01 relu         (2, 3)     min=  0.0000 max=  7.0000
02 sum          (2,)       min=  6.0000 max= 10.0000
[array([ 6., 10.], dtype=float32)]
```

这类追踪在定位“第几个 op 开始出现 `inf` 或全零”时比断点更快。代价是要维护一份与 `evaluate_graph` 平行的循环，注意别让它和正式实现分叉。更好的做法是给 `evaluate_graph` 加一个可选 `trace` 回调，练习里会做。

### 作为差分测试的参考端

解释器最正式的用法是差分测试（differential testing）：随机生成图，分别用解释器和另一套实现求值，比较所有中间值。练习 A 会把这件事做完整。这里先给出最小骨架：

```python
from mini_ai import evaluate_graph
from mini_ai.interpreter import max_abs_error

for seed in range(100):
    graph, feed, expected = make_random_case(seed)  # 你的生成器
    actual = evaluate_graph(graph, feed)
    for a, e in zip(actual, expected):
        assert max_abs_error(a, e) < 1e-5, seed
```

生成器要保证形状合法，否则测的是 verifier，不是解释器。这个区分在练习 A 里会反复出现。

## 对比与取舍

| 方案 | 语义来源 | 单次开销 | 可调试性 | 在本课程中的角色 |
| --- | --- | --- | --- | --- |
| NumPy 解释器 | 本仓库 `apply_op` | 毫秒级以下 | 逐 op、逐值 | 金标准 |
| PyTorch eager | ATen kernel | 毫秒级 | 逐 op，但难插入 IR 值 | 前端抓图的对照 |
| C DLL | 生成的 C + MSVC | 微秒级 | 需要额外打印或调试器 | 性能后端 |
| LLVM JIT | 生成的 LLVM IR | 微秒级 | 可 dump IR，难查数值 | 第二后端 |

取舍的核心不是“谁快”，而是“谁能定义正确”：

- 用 PyTorch 当唯一金标准，会让“编译器是否忠于 IR 语义”这个问题消失。PyTorch 的融合、内存格式、线程数都可能改变末位，你无法区分“我的 lowering 错了”和“PyTorch 换 kernel 了”。
- 用 C 后端当金标准，会把 `/fp:fast` 的浮点重排带进语义定义（第 19 章），误差可能比解释器大。
- 解释器的语义是“这张 IR 按顺序、按 float32 执行”。它慢，但定义清楚；后端与它的偏差才是需要解释的对象。

## 踩雷集锦

1. **把 `feed` 当成位置参数**。`evaluate_graph(graph, {"x0": ...})` 里用的是字符串键 `"x0"`；前端默认命名是 `x0`、`x1`，但如果你在 `from_torch(..., input_names=["input"])` 里改过名字，就必须用新名字。报错 `missing input 'x0'; got ['input']` 会直接告诉你差异。
2. **以为 NumPy 会把 `(2, 3)` 和 `(3,)` 广播成 `(2, 3)`**。NumPy 的规则是尾部对齐，`(2, 3)` 与 `(3,)` 对齐后是 `(2, 3)` 与 `(1, 3)`，这是合法的；真正不合法的是 `(2, 3)` 与 `(2,)`（尾部 `3` 与 `2` 冲突）。本章 5.5 的 `(3,)` 偏置能配上 `(2, 3)`（尾部对齐）；`(4,)` 配不上 `(4, 8)`，但能配上 `(8, 4)`。写测试时先手算一遍广播结果。
3. **用 float64 造输入，却用 float32 的解释器输出比较**。解释器会静默降级，误差看起来像 1e-8 的“编译器 bug”，实际是精度口径不一致。统一 `astype(np.float32)` 后再比较。
4. **把 `max_abs_error == 0.0` 当成“完全正确”**。空张量会返回 0.0；两个全零输出也会返回 0.0。测试要同时断言形状、`numel`，并在非空时检查至少一个非零元素或与参考值比较。
5. **假设 `softmax` 的 `axis` 在建图时被校验**。当前实现不校验，越界轴会拖到运行时才炸（本章 5.11 的 `AxisError`）。给 IR 加新算子时，把 attrs 校验放在 `infer_output_type` 里，别留给解释器。

## 进阶

- **给 `evaluate_graph` 加 trace 回调**。签名可以是 `trace: Callable[[int, Op, np.ndarray], None] | None = None`，在写入环境前调用。要求：默认路径零额外开销；trace 里抛异常要能定位到 op index。
- **实现严格 dtype 模式**。当前 float64 会被静默降级；加一个 `strict_dtype=True` 参数，在输入和常量入环境时检查 `dtype == np.float32`，否则抛 `ValueError`。这会让“测试用错精度”更早暴露。
- **比较中间值而不是只比较输出**。输出对齐不代表中间对；一个错误的融合可能碰巧在最后一层抵消。练习 A 的 `middle_errors` 是一个可复用的写法。

## 动手练习

1. 用 `Graph` 手工搭一张 `x(3, 4) -> mul(2.0) -> sum(axis=0) -> relu` 的图，先手算输出形状，再用 `dump()` 和 `evaluate_graph` 验证。要求写出三行：推导形状、实际形状、最大误差（与手算值比较）。
2. 故意把 `feed` 的键写成 `"x"` 而不是 `"x0"`，记录完整报错；再故意把输入形状改成 `(4, 3)`，记录报错。比较两条报错分别在哪一层被抛出。
3. 给 5.11 的 `softmax` 图加一个 `axis=1` 的版本，验证每行和为 1；再运行 `axis=5` 的版本，确认失败点。写出你认为应该在哪一层拦截这个错误，并说明理由。
4. 用 `evaluate_with_trace` 跑 5.4 的 MLP，找出绝对值最大的中间值出现在哪个 op。这个信息在后续选择容差时有什么用？

## 本章重点

- 参考解释器是语义金标准；它的价值在“定义正确”，不在“跑得快”。
- `apply_op` 是算子语义的唯一实现，常量折叠与运行时求值共用它。
- `evaluate_graph` 先 `validate()`，再装常量、装输入、逐 op 执行，并在输入和输出两处检查形状。
- 环境以 `Value` 对象身份为键，名字只用于 `feed` 和打印。
- float32 在入口、出口、常量三处统一；float64 会被静默降级。
- `max_abs_error` 对形状错误抛异常，对空张量返回 0.0，对 NaN 返回 NaN。
- 容差要匹配数值范围；本课程的小 MLP 用 1e-5，实测误差在 3e-08 到 5e-08。
- 解释器挡得住“实现与声明不一致”，挡不住“声明与实现一起错”。

## 自我检核

- [ ] 我能不看源码说出 `evaluate_graph` 的四个执行阶段。
- [ ] 我能解释为什么环境用 `Value` 而不是名字作键。
- [ ] 我能写出缺输入、错形状、未知算子三类报错的原文，并指出各自在哪一层抛出。
- [ ] 我能说明 `max_abs_error` 对空张量、形状不匹配、NaN 的三种行为。
- [ ] 我能解释为什么 float64 输入不会让解释器输出 float64。
- [ ] 我能用 `evaluate_with_trace` 定位第一个出现 `inf` 的 op。
- [ ] 我知道 `softmax` 的 `axis` 当前没有被形状推导校验，并知道这属于 verifier 的缺口。
- [ ] 我能把解释器输出与 PyTorch 对齐，并给出误差数字而不是“看起来一样”。

## 延伸阅读

- **NumPy Broadcasting**（<https://numpy.org/doc/stable/user/basics.broadcasting.html>）：读“General Broadcasting Rules”和示例部分，掌握尾部对齐与 `1` 维扩张。关联本章 5.5、5.9 与第 4 章的 `broadcast_shape`。
- **David Goldberg, “What Every Computer Scientist Should Know About Floating-Point Arithmetic”**（ACM Computing Surveys, 1991）：读舍入误差、ulp 与灾难性抵消三节。关联本章 5.10 的容差选择，以及第 19 章 `/fp:fast` 带来的重排。
- **PyTorch FX 文档**（<https://pytorch.org/docs/stable/fx.html>）：读 `symbolic_trace`、`GraphModule` 与 `ShapeProp` 的示例，理解 `from_torch` 的输入形状从哪里来。关联第 3 章与本章 5.4。
- **`code/mini_ai/interpreter.py` 源码**：逐行读 `apply_op`、`evaluate_graph`、`max_abs_error`，把本章的代码摘录与文件对照。重点看 `_as_float32` 出现的位置。

---

下一章是练习 A：你要把 `neg`、`tanh`、`transpose` 加进 IR 与解释器，并用随机图差分测试证明扩展没有破坏原有语义。

[练习 A：扩展图 IR 与解释器](./practice-a-ir-and-interpreter.md)