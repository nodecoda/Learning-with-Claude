# Ch 3 前端：FX 到自有 IR

> 目标：理解 `torch.fx.symbolic_trace` 抓到的是什么，为什么默认 tracer 会把 `nn.Linear` 留成 `call_module`，`_InlineTracer` 又如何把它展开成 `get_attr + call_function`；最终能读懂 `from_torch` 如何把 FX 节点逐条翻译成 Ch 2 的 `Graph`，并复现三类失败路径。

> 环境：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；numpy 2.4.6。命令在 `D:\Learning-with-Claude` 下执行，设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"`。

## 为什么需要

前端决定“哪些模型能进来，以及进来后语义是否还正确”。如果前端把 `nn.Linear` 当成一个黑盒，IR 里就没有 `matmul` 和 `add`，后面的融合、内存规划、C 代码生成都无从谈起。如果前端默默跳过不支持的算子，你会得到一个能编译但算错的模型。

`frontend.py` 选择了一条很窄但清楚的路：

- 用 FX（PyTorch 的符号追踪工具）抓图，拿到一张由 `placeholder`、`get_attr`、`call_function`、`output` 组成的图。
- 用 `_InlineTracer` 把 `nn.Linear` 展开成基本算子。
- 用 `ShapeProp` 给每个节点补上 shape。
- 只接受白名单里的 target，其他一律 `IRError`。
- 最后调用 `Graph.validate()`，保证交给下游的图是合法的。

本章的重点不是记住 FX API，而是理解“一个 Python 模块如何变成一张静态图”，以及这条路上哪些信息会丢、哪些会保留。

## 先建立直觉

```text
nn.Sequential(Linear(4,8), ReLU(), Linear(8,2))
        │
        │ _InlineTracer().trace(model)
        v
FX Graph:
  placeholder input_1
  get_attr 0.weight       ─┐
  get_attr 0.bias          │ nn.Linear 被展开
  call_function linear     │ target = torch._C._nn.linear
  call_function relu      ─┘
  get_attr 2.weight
  get_attr 2.bias
  call_function linear_1
  output linear_1
        │
        │ ShapeProp(traced).propagate(x)
        v
每个 node.meta["tensor_meta"] 有 shape
        │
        │ for node in traced.graph.nodes
        v
env: {fx.Node -> ir.Value}
        │
        v
Graph(inputs=[x0], ops=[matmul, add, relu, matmul, add], outputs=[linear_1])
```

两个映射要记住：

- FX 的 `placeholder` 变成 IR 的 `input`。
- FX 的 `get_attr` 变成 IR 的 `constant`；`call_function` 变成 IR 的 `Op`。

`env` 是整条翻译过程的粘合剂。每处理完一个 FX 节点，就把它的 IR `Value` 放进 `env`；后面的节点通过 `env[arg]` 找到输入。

## 核心内容

### 3.1 默认 symbolic_trace 会留下 call_module

先看默认行为：

```python
import torch
import torch.fx

model = torch.nn.Sequential(
    torch.nn.Linear(4, 8), torch.nn.ReLU(), torch.nn.Linear(8, 2)
).eval()
print(torch.fx.symbolic_trace(model).graph)
```

实际输出：

```text
graph():
    %input_1 : [num_users=1] = placeholder[target=input]
    %_0 : [num_users=1] = call_module[target=0](args = (%input_1,), kwargs = {})
    %_1 : [num_users=1] = call_module[target=1](args = (%_0,), kwargs = {})
    %_2 : [num_users=1] = call_module[target=2](args = (%_1,), kwargs = {})
    return _2
```

`nn.Linear` 被当成叶子模块，整块记成 `call_module`。这对“保留模块边界”很有用，但 `frontend.from_torch` 没有 `call_module` 分支，所以不能直接吃这张图。

### 3.2 `_InlineTracer` 把子模块展开

```python
class _InlineTracer(torch.fx.Tracer):
    def is_leaf_module(self, m, qualname):
        return False
```

`is_leaf_module` 返回 `False` 后，tracer 会进入 `nn.Linear.forward`，把它内部的 `F.linear` 暴露出来：

```text
graph():
    %input_1 : [num_users=1] = placeholder[target=input]
    %_0_weight : [num_users=1] = get_attr[target=0.weight]
    %_0_bias : [num_users=1] = get_attr[target=0.bias]
    %linear : [num_users=1] = call_function[target=torch._C._nn.linear](args = (%input_1, %_0_weight, %_0_bias), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.nn.functional.relu](args = (%linear,), kwargs = {inplace: False})
    %_2_weight : [num_users=1] = get_attr[target=2.weight]
    %_2_bias : [num_users=1] = get_attr[target=2.bias]
    %linear_1 : [num_users=1] = call_function[target=torch._C._nn.linear](args = (%relu, %_2_weight, %_2_bias), kwargs = {})
    return linear_1
```

这张图里已经没有 `call_module`。每个 `nn.Linear` 变成三个节点：两个 `get_attr` 加一个 `call_function`。`target` 是 `torch._C._nn.linear`，它的 `__name__` 是 `linear`，所以 frontend 用名字判断（节选）：

```python
target_name = getattr(target, "__name__", str(target))
if target_name == "linear":
    ...
```

用名字而不是对象身份，是为了兼容不同 PyTorch 版本里 `F.linear` 的实际绑定位置。

### 3.3 ShapeProp 补 shape

FX 只记录数据流，不记录 shape。`ShapeProp` 会拿示例输入真的执行一遍模型，把每个节点的 `tensor_meta` 写进 `node.meta`：

```python
ShapeProp(traced).propagate(*example_inputs)
```

frontend 读取：

```python
def _shape_of(node: torch.fx.Node) -> tuple[int, ...] | None:
    meta = node.meta.get("tensor_meta")
    if meta is None:
        return None
    shape = getattr(meta, "shape", None)
    return tuple(int(d) for d in shape) if shape is not None else None
```

这就是为什么 `from_torch` 必须接收 `example_inputs`。没有示例输入，shape 推不出来；有示例输入，模型就会被真实执行一次。对含随机数、外部状态或副作用的模型，这一点要特别注意。

### 3.4 逐节点翻译

`from_torch` 的主循环按 FX 图的顺序遍历节点（节选）：

```python
for node in traced.graph.nodes:
    if node.op == "placeholder":
        shape = _shape_of(node) or tuple(example_inputs[placeholder_index].shape)
        env[node] = graph.add_input(names[placeholder_index], shape)
        placeholder_index += 1
        continue
    if node.op == "get_attr":
        raw = _resolve_attr(traced, str(node.target))
        env[node] = _as_const(graph, raw, str(node.target).replace(".", "_"))
        continue
    if node.op == "call_function":
        ...
    if node.op == "output":
        ...
```

三个细节：

- `placeholder` 的 shape 优先用 `ShapeProp` 的结果，拿不到才退回示例输入。
- `get_attr` 通过 `traced.get_parameter` / `get_buffer` 取参数，取不到再按属性路径走。
- `_as_const` 统一做 `detach().cpu().numpy().astype(np.float32)`，所以权重进入 IR 时已经是 `float32` 的 NumPy 数组。

`env` 只在当前翻译过程里有效。它不进入 IR，也不进入 Pass；翻译结束后，图里只剩下 `Value` 和 `Op`。

### 3.5 nn.Linear 的降级

frontend 里的 linear 分支：

```python
if target_name == "linear":
    vals = read_args(node)
    if len(vals) not in {2, 3}:
        raise IRError(f"{node.name}: linear expects x, weight, [bias]")
    if not vals[1].is_constant or vals[1].data is None:
        raise IRError(f"{node.name}: only constant linear weights are supported")
    w_t = graph.add_const(
        f"{node.name}_weight_t", vals[1].data.T.copy()
    )
    out = graph.add_op("matmul", [vals[0], w_t], name=f"{node.name}_matmul")
    if len(vals) == 3:
        out = graph.add_op("add", [out, vals[2]], name=node.name)
    env[node] = out
    continue
```

逐步看：

1. `read_args(node)` 把 FX 参数翻译成 IR `Value`。weight 和 bias 是 `get_attr` 节点，已经在 `env` 里，所以这里拿到的是常量。
2. 检查 weight 必须是常量。动态权重会让图依赖运行时参数，当前 IR 不支持。
3. 把 weight 转置，创建新常量 `linear_weight_t`。
4. 插入 `matmul(x, weight_t)`。
5. 如果有 bias，再插入 `add(matmul, bias)`。
6. 把最终的 `Value` 放回 `env`，供后续节点使用。

### 3.6 为什么要转置权重

PyTorch 的 `F.linear(x, W, b)` 语义是：

```text
y = x @ W.T + b
```

IR 的 `matmul(a, b)` 语义是普通的矩阵乘：

```text
C[i, j] = sum_k A[i, k] * B[k, j]
```

所以要让 `matmul(x, B)` 等于 `x @ W.T`，必须令 `B = W.T`。`nn.Linear(4, 8)` 的 `weight` shape 是 `(8, 4)`，转置后是 `(4, 8)`，刚好匹配 `x(3,4) @ B(4,8) -> (3,8)`。

实际建图后的 dump：

```text
inputs:
  x0: tensor<3, 4>
constants:
  0_weight: tensor<8, 4> numel=32
  0_bias: tensor<8> numel=8
  linear_weight_t: tensor<4, 8> numel=32
  2_weight: tensor<2, 8> numel=16
  2_bias: tensor<2> numel=2
  linear_1_weight_t: tensor<8, 2> numel=16
ops:
  00 %linear_matmul = matmul(x0, linear_weight_t) : tensor<3, 8>
  01 %linear = add(linear_matmul, 0_bias) : tensor<3, 8>
  02 %relu = relu(linear) : tensor<3, 8>
  03 %linear_1_matmul = matmul(relu, linear_1_weight_t) : tensor<3, 2>
  04 %linear_1 = add(linear_1_matmul, 2_bias) : tensor<3, 2>
outputs:
  linear_1: tensor<3, 2>
```

注意 `0_weight` 和 `2_weight` 仍留在常量列表里，但没有被任何 op 使用。它们来自 `get_attr` 节点，后面又被转置常量替代。`standard_pipeline()` 里的 `dce` 会把它们清掉；如果只看 `from_torch` 的原始图，它们还在。

### 3.7 失败路径一：不支持的算子

```python
class Bad(torch.nn.Module):
    def forward(self, x):
        return torch.sigmoid(x)

from_torch(Bad().eval(), (torch.randn(2, 3),))
```

实际报错：

```text
IRError: unsupported call_function target in sigmoid: <built-in method sigmoid of type object at 0x...>
```

frontend 不会跳过 `sigmoid`，也不会近似成别的算子。它直接拒绝。这是正确的默认值：未知语义不能猜。

### 3.8 失败路径二：`x * 2` 的 target 不是 `torch.mul`

```python
class MulScalar(torch.nn.Module):
    def forward(self, x):
        return x * 2.0

from_torch(MulScalar().eval(), (torch.randn(2, 3),))
```

实际报错：

```text
IRError: unsupported call_function target in mul: <built-in function mul>
```

原因是 `x * 2.0` 在 FX 里抓到的 target 是内建 `mul`，不在 `_SUPPORTED_BINARY` 里。写成 `torch.mul(x, 2.0)` 就能通过：

```python
class MulScalar(torch.nn.Module):
    def forward(self, x):
        return torch.mul(x, 2.0)

g = from_torch(MulScalar().eval(), (torch.randn(2, 3),))
```

实际 dump：

```text
inputs:
  x0: tensor<2, 3>
constants:
  scalar_mul_1: tensor<scalar> numel=1
ops:
  00 %mul = mul(x0, scalar_mul_1) : tensor<2, 3>
outputs:
  mul: tensor<2, 3>
```

`2.0` 被 `_node_value` 转成标量常量，名字是 `scalar_mul_1`。这说明前端支持标量参数，但只对白名单里的 target 生效。

### 3.9 失败路径三：元组输出里带输入

```python
class Two(torch.nn.Module):
    def forward(self, x):
        a = torch.relu(x)
        return a, torch.mul(a, 2.0)

g = from_torch(Two().eval(), (torch.randn(2, 3),))
```

这个例子能通过，dump 是：

```text
inputs:
  x0: tensor<2, 3>
constants:
  scalar_mul_1: tensor<scalar> numel=1
ops:
  00 %out0 = relu(x0) : tensor<2, 3>
  01 %out1 = mul(out0, scalar_mul_1) : tensor<2, 3>
outputs:
  out0: tensor<2, 3>
  out1: tensor<2, 3>
```

但如果直接返回输入：

```python
class Two(torch.nn.Module):
    def forward(self, x):
        return x, torch.mul(x, 2.0)
```

实际报错：

```text
IRError: op mul uses out0 before it is defined
```

原因是 `add_output` 把输入 `x0` 的 `kind` 改成了 `output`，并重命名为 `out0`。`mul` 再使用它时，`validate()` 看到的既不是 `input` 也不是 `constant`，又没有 producer，于是判定“使用未定义值”。这是当前实现的边界，不是 FX 的问题。

## 底层机制

### 3.10 `_node_value` 的参数规则

```python
def _node_value(graph: Graph, env: dict[torch.fx.Node, Value], arg: Any, node: torch.fx.Node, idx: int) -> Value | None:
    if isinstance(arg, torch.fx.Node):
        if arg not in env:
            raise IRError(f"node {arg.name} is used before it is available")
        return env[arg]
    if isinstance(arg, (int, float, bool)) and not isinstance(arg, bool):
        return _as_const(graph, float(arg), f"scalar_{node.name}_{idx}")
    if isinstance(arg, bool):
        return _as_const(graph, float(arg), f"scalar_{node.name}_{idx}")
    if arg is None:
        return None
    raise IRError(f"unsupported argument {arg!r} in node {node.name}")
```

规则很窄：

- `Node` 必须在 `env` 里，否则说明 FX 图顺序不对或前端漏处理。
- 数字和布尔值变标量常量。
- `None` 返回 `None`，由调用方决定是否可省略。
- 其他类型（列表、字典、字符串、slice）一律拒绝。

`read_args` 会把 `None` 过滤掉，所以 `linear(x, w)` 没有 bias 时，`vals` 长度为 2，走 `len(vals) == 2` 的分支，不插入 `add`。

### 3.11 `_resolve_attr` 与 `_as_const`

`_resolve_attr` 先尝试 `get_parameter`，再尝试 `get_buffer`，最后按 `target` 的点分路径逐层 `getattr`。这覆盖了：

- `nn.Linear.weight` / `bias`：参数。
- BatchNorm 的 `running_mean`：buffer。
- 普通 Python 属性：例如一个预先存在的常量张量。

`_as_const` 对 `torch.Tensor` 做 `detach().cpu().numpy().astype(np.float32, copy=False)`，对 Python 数字做 `np.asarray(value, dtype=np.float32)`。所以：

- 需要梯度的参数会被 detach，图里不保留 autograd 关系。
- GPU 张量会被搬到 CPU。
- `float64` 会被转成 `float32`。

最后一点是隐式转换，可能丢精度。Ch 4 会把它列为踩雷项。

### 3.12 输出节点的处理

```python
if node.op == "output":
    out_arg = node.args[0]
    if isinstance(out_arg, torch.fx.Node):
        graph.add_output(env[out_arg], out_arg.name)
    elif isinstance(out_arg, (tuple, list)):
        for i, item in enumerate(out_arg):
            if not isinstance(item, torch.fx.Node):
                raise IRError(f"unsupported output item: {item!r}")
            graph.add_output(env[item], item.name if len(out_arg) == 1 else f"out{i}")
    else:
        raise IRError(f"unsupported output form: {out_arg!r}")
```

单输出直接用 FX 节点名；多输出统一命名 `out0`、`out1`，避免两个输出重名。输出必须是 FX 节点，不能是 Python 常量、列表嵌套或字典。

### 3.13 为什么最后要 validate

`from_torch` 最后调用 `graph.validate()`。前端可能因为 FX 图顺序、`env` 缺失、输出重命名等原因产生结构错误。把校验放在前端出口，可以保证 Pass 拿到的图一定满足 SSA 和拓扑序，减少下游的防御性代码。

这也意味着前端错误会在 `from_torch` 里暴露，而不是等到 C 代码生成。越早失败，定位成本越低。

## 进一步用法与变体

### 3.14 自定义输入名

```python
g = from_torch(model, (x,), input_names=["batch"])
```

`run_c_graph` 的 feed 键要跟着变成 `{"batch": ...}`。输入名只影响图的名字和运行时字典，不影响生成的 C 参数顺序。

### 3.15 多输入

`example_inputs` 的长度决定 `placeholder` 的数量，默认名字是 `x0`、`x1`。每个 placeholder 按出现顺序对应一个 `graph.add_input`。如果模型 forward 的参数顺序和输入顺序不一致，图不会帮你纠正。

### 3.16 sum 与 softmax

frontend 对 `torch.sum` 只支持单个轴：

```python
if isinstance(dim, (list, tuple)):
    if len(dim) != 1:
        raise IRError(f"{node.name}: only a single sum axis is supported")
```

`keepdim` 会写进 attrs。`torch.softmax` 的 `dim` 也写进 attrs。两者都能建图，但 `softmax` 在 C/LLVM 后端会显式失败，这一点在 Ch 1 已经说明。

## 对比与取舍

| 方案 | 优点 | 限制 | 本课程的选择 |
| --- | --- | --- | --- |
| FX symbolic trace | 轻量、可读、易改写 | 只记录 tensor 数据流，控制流会特化 | 使用 |
| TorchScript | 能保留更多控制流 | 语法和运行时约束多 | 不展开 |
| ONNX | 跨框架、生态广 | 算子集和版本兼容复杂 | 不展开 |
| torch.compile / Dynamo | 生产级、能处理动态图 | 内部机制庞大 | 作为工业对照 |
| 手写图 | 完全可控 | 无法接真实模型 | 仅用于测试 |

FX 的取舍很清楚：它让你在几十行代码里完成“模型到图”，代价是只支持静态、无控制流的模型。对教学来说，这个边界刚好。

## 踩雷集锦

1. **直接用默认 `symbolic_trace` 喂给 `from_torch`。** 默认图里是 `call_module`，frontend 没有这个分支，会报 `unsupported FX node op: call_module (_0)`。
2. **写 `x * 2` 而不是 `torch.mul(x, 2)`。** 前者抓到内建 `mul`，不在白名单；后者才是 `torch.mul`。
3. **返回输入作为输出之一。** `add_output` 会把输入重标记为 `output`，后续 op 再使用它会触发 `validate()` 错误。
4. **以为 `ShapeProp` 不执行模型。** 它会用示例输入真的跑一遍；有随机性或副作用的模型会受影响。
5. **把动态权重传给 linear。** frontend 要求 weight 是常量，否则报 `only constant linear weights are supported`。

## 进阶

- 在 frontend 里增加 `call_module` 分支，直接处理 `nn.Linear`，不依赖 `_InlineTracer`。
- 把 `x * scalar` 的 target 归一化：识别内建 `mul` 并映射到 `torch.mul`。
- 增加 `sigmoid` 的 IR 语义和后端实现，走一遍 Ch 1 的“新增算子五处清单”。
- 给 `Value` 记录 FX 节点名，让 `dump()` 能显示来源，便于定位前端错误。

## 动手练习

1. **对比两种 trace。** 分别打印默认 `symbolic_trace` 和 `_InlineTracer` 的图，标出 `call_module` 与 `call_function` 的差异。
2. **验证转置。** 用 `model[0].weight.T.shape` 与 `linear_weight_t` 的 shape 对比，确认 `(8,4)` 变成 `(4,8)`。
3. **制造不支持算子。** 用 `torch.sigmoid` 复现错误，确认错误发生在 `from_torch` 而不是 C 后端。
4. **修好 `x * 2`。** 把模型改成 `torch.mul`，观察标量常量 `scalar_mul_1` 出现在 dump 里。
5. **复现元组输出边界。** 先返回两个计算值，再返回“输入 + 计算值”，对比两次 `validate()` 的结果。

## 本章重点

- 默认 `symbolic_trace` 把 `nn.Linear` 留成 `call_module`，`_InlineTracer` 把它展开成 `get_attr + call_function`。
- `ShapeProp` 用示例输入执行模型，为每个节点补 `tensor_meta`。
- `from_torch` 用 `env` 把 FX 节点映射到 IR `Value`，逐节点翻译。
- `nn.Linear` 降级为 `matmul(x, W.T)`，有 bias 时再加 `add`。
- 不支持的 target 一律 `IRError`，不静默跳过。
- 元组输出和“输入直接作为输出”有明确边界，当前实现会失败。

## 自我检核

- [ ] 我能解释 `call_module` 和 `call_function` 的区别。
- [ ] 我知道 `_InlineTracer` 只改了一个方法，为什么就能展开 `nn.Linear`。
- [ ] 我能手算 `nn.Linear(4, 8)` 的权重转置后 shape。
- [ ] 我能说出 `env` 在翻译过程中的作用，以及它为什么不进入 IR。
- [ ] 我能复现 `sigmoid` 和 `x * 2` 两条失败路径。
- [ ] 我能解释为什么 `ShapeProp` 需要真实输入。

## 延伸阅读

- **`code/mini_ai/frontend.py`**：读 `_InlineTracer`、`_node_value`、`from_torch` 的 linear 分支；学 FX 到 IR 的逐节点翻译；关联本章所有代码片段。
- **PyTorch FX 官方文档**：读 `symbolic_trace`、`Tracer.is_leaf_module`、`ShapeProp` 三节；学 FX 的节点模型和 shape 传播；关联 3.1 到 3.3。
- **`torch.fx.passes.shape_prop` 源码**：读 `ShapeProp.propagate` 如何执行并写 `tensor_meta`；学 shape 信息的来源；关联 3.3 和 Ch 6 的类型推导。
- **`code/mini_ai/ir.py`**：回看 `Graph.add_op` 与 `add_output`；学前端产出的图如何满足 SSA；关联 Ch 2 的 validate。
- **ONNX 算子文档的 `Gemm` / `MatMul`**：读线性层的标准分解；学不同图格式如何表达同一个 `Linear`；关联 3.5 的降级策略。

[下一章：张量语义](./04-tensor-semantics.md)
