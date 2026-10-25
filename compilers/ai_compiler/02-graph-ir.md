# Ch 2 图 IR 对照：FX、torch.export、ONNX、Relax、StableHLO

> **目标**：把同一个模型分别抓成 FX Graph、ATen Graph（`torch.export`）、ONNX Graph、Relax、StableHLO，逐项对照它们的节点、参数表示、常量和形状信息，知道每种 IR 适合做什么、不适合做什么。

## 为什么需要这个？

「图 IR」不是一种东西，而是一族东西。它们看起来都是「节点 + 边」，但节点粒度、类型系统、参数绑定方式、序列化能力完全不同。选错 IR 的后果很具体：

- 用 FX 做部署，会发现参数还在 module 里，换台机器就找不到。
- 用 ONNX 做图级优化，会发现它缺少 SSA 作用域和类型推导，做数据流分析很别扭。
- 用 ATen Graph 对接自定义后端，会发现算子太细、overload 太多，实现成本高。
- 用 StableHLO 做 PyTorch 原型，会发现导出链路根本不在这里。

这一章把五个 IR 放在同一张桌子上，用同一张 `x @ W + 1 → relu` 的图对照。**目标不是记住所有 API，而是看到一种 IR 时能快速判断：它保留了哪些信息、丢了哪些信息、适合哪一层。**

## 先建立直觉：同一道菜，五种写法

同一张计算图，可以用五种「字母表」写出来：

| 写法 | 节点长什么样 | 参数在哪 | 给谁看 |
|---|---|---|---|
| FX Graph | `operator.matmul`、`torch.relu` | `get_attr` 节点 | PyTorch 开发者、自定义 pass |
| ATen Graph | `torch.ops.aten.matmul.default` | `placeholder`（`p_w`） | PyTorch 后端、编译器 |
| ONNX | `MatMul`、`Add`、`Relu` | `initializer` | 跨框架推理引擎 |
| Relax | `R.matmul`、`R.add`、`R.nn.relu` | 常量/函数参数 | TVM 的图级优化 |
| StableHLO | `stablehlo.dot_general`、`stablehlo.add`、`stablehlo.maximum` | 常量/函数参数 | XLA/MLIR 生态 |

五种写法描述的是同一个数学函数，但**「谁是输入」「常量存在哪」「形状写不写进类型」**这三个问题的答案不同。这三个差异，决定了每种 IR 的优化能力和部署方式。

## 核心概念一：FX Graph 是「Python 算子级」的图

FX 的节点目标（`node.target`）是 Python 可调用对象：

- `operator.matmul`、`operator.add`、`operator.mul`
- `torch.relu`、`torch.sum`
- 用户自定义函数（如果 `symbolic_trace` 能追踪）

FX 的节点类型只有几种，源码注释写得很直接：

```text
# the kind of operation = placeholder|call_method|call_module|call_function|get_attr
```

源码依据：`torch/fx/node.py:291-292`（torch 2.14.1）。`Graph` 类和四个构造函数在 `torch/fx/graph.py:1397`、`:1766`、`:1799`、`:1959`、`:2424`。

FX 的优点是**轻、可读、好改**。你可以在 `Graph` 上直接插节点、删节点、替换 target，不需要理解 C++ 的 dispatcher。缺点是：

- 参数通过 `get_attr` 挂在 module 上，图本身不包含权重。
- 节点粒度是 Python 函数，同一个 `add` 可能在不同 dtype 下走不同实现，但图里看不出来。
- 动态控制流、原地修改、别名关系容易丢。

所以 FX 适合**教学、原型、自定义图变换**，不适合直接当部署格式。

## 核心概念二：`torch.export` 是「ATen 级」的契约

`torch.export.export` 产出 `ExportedProgram`，它的图有几个硬性约定：

1. 参数和 buffer 被提升为 `placeholder`（名字通常带 `p_` 前缀）。
2. 算子使用 `torch.ops.aten.*` 命名空间，并带 overload 名（如 `.default`、`.Tensor`）。
3. 返回值统一成 tuple。
4. 图带 `graph_signature`，明确哪些输入是参数、哪些是用户输入。

这使它更像「可执行契约」而不是「源码」：后端可以不知道 Python 的 `+` 是什么，但必须实现 `aten.add.Tensor`。

源码依据：`torch.export.export` 定义在 `torch/export/__init__.py:59`（torch 2.14.1）。

## 核心概念三：ONNX 是「可序列化的算子图」

ONNX 的节点是标准算子（`MatMul`、`Add`、`Relu`），权重和常量放在 `initializer` 里，图输入只保留用户输入。它有三个关键属性：

- **opset 版本**：同一张图在不同 opset 下可能有不同算子/属性。本机导出用 opset 18。
- **静态形状为主**：默认把 shape 写死；动态维度需要显式 `dynamic_axes`。
- **跨框架**：PyTorch、TensorFlow、JAX 都能导出 ONNX，ONNX Runtime 用 EP 决定在哪个后端执行。

ONNX 不是为「图级优化」设计的 IR：它没有 MLIR/Relax 那种带作用域的类型系统和类型推导，也没有控制流的统一表示（`If`/`Loop` 是特殊算子）。它的强项是**交换和部署**。

## 核心概念四：Relax 与 StableHLO 是「带类型的函数式张量 IR」

Relax 是 TVM 的图级 IR。它的函数有明确的参数类型和返回类型，计算写在 `R.dataflow()` 块里，每个中间结果绑定到一个 `Var`。它支持：

- 形状和 dtype 写进类型（`R.Tensor((4, 8), dtype="float32")`）。
- 数据流块（dataflow block）和普通控制流。
- 算子可以是高层 `R.matmul`，也可以是 `call_tir` 调用的底层 TIR。

StableHLO 是 XLA HLO 的可移植子集，风格更「函数式」：

- 一切皆值，SSA 形式，没有原地修改。
- broadcast 必须显式写成 `stablehlo.broadcast_in_dim`。
- matmul 统一成 `stablehlo.dot_general`，用 `contracting_dims`/`batching_dims` 描述。
- 作为 MLIR 生态的入口，可以继续 lower 到 linalg/memref。

两者都比 FX/ONNX 更「编译器友好」：类型完整、语义明确、可组合。代价是**写起来啰嗦**，而且从 PyTorch 过去的转换链路更重。

## 底层机制：抓图到底保留了什么？

把「抓图」拆成三个动作，就能看清差异：

1. **记录调用**：FX 用 `__torch_function__`/代理对象记录 Python 层调用；`torch.export` 用 TorchDynamo 抓 ATen 调用；ONNX 导出器先走 `torch.export`，再翻译成 ONNX 算子。
2. **绑定参数**：FX 把 `self.w` 记成 `get_attr`；`torch.export` 把它提升成 `placeholder` 并存进 `state_dict`；ONNX 把它写成 `initializer`；Relax 默认把它冻结成 `GenericConst`。
3. **补类型**：FX 的 shape/dtype 在 `node.meta["tensor_meta"]` 里（可选）；`torch.export` 有 `graph_signature` 和 `SymInt`；Relax 把类型写进 `Var`；StableHLO 把 shape 写进 tensor 类型。

抓图不是「拍照」，而是「按某种契约重写」。**契约越强，后端越好写；契约越弱，前端越容易抓。**

## 示例一：FX Graph

模型与 Ch 1 相同：

```python
import torch

class Block(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 8))

    def forward(self, x):
        y = x @ self.w
        y = y + 1.0
        return torch.relu(y)

torch.manual_seed(0)
m = Block().eval()
x = torch.randn(4, 8)
g = torch.fx.symbolic_trace(m)
print(g.graph)
```

实测输出：

```text
graph():
    %x : [num_users=1] = placeholder[target=x]
    %w : [num_users=1] = get_attr[target=w]
    %matmul : [num_users=1] = call_function[target=operator.matmul](args = (%x, %w), kwargs = {})
    %add : [num_users=1] = call_function[target=operator.add](args = (%matmul, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.relu](args = (%add,), kwargs = {})
    return relu
```

读法：`%x` 是外部输入，`%w` 是 module 属性，三个 `call_function` 是算子，`return relu` 是输出。常量 `1.0` 直接内联在 `args` 里。

## 示例二：`torch.export` 的 ATen Graph

```python
ep = torch.export.export(m, (x,))
print(ep.graph)
print("inputs:", ep.graph_signature.input_specs)
print("state_dict:", list(ep.state_dict.keys()))
```

实测输出：

```text
graph():
    %p_w : [num_users=1] = placeholder[target=p_w]
    %x : [num_users=1] = placeholder[target=x]
    %matmul : [num_users=1] = call_function[target=torch.ops.aten.matmul.default](args = (%x, %p_w), kwargs = {})
    %add : [num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%matmul, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.ops.aten.relu.default](args = (%add,), kwargs = {})
    return (relu,)
inputs: [InputSpec(kind=<InputKind.PARAMETER: 2>, arg=TensorArgument(name='p_w'), target='w', persistent=None), InputSpec(kind=<InputKind.USER_INPUT: 1>, arg=TensorArgument(name='x'), target=None, persistent=None)]
state_dict: ['w']
```

三个差异一眼可见：

1. `w` 从 `get_attr` 变成 `placeholder[p_w]`，并且排在 `x` 前面。
2. 算子从 Python 函数变成 `aten` 命名空间，`add` 带 `.Tensor` overload。
3. 返回值是 tuple：`return (relu,)`。

`graph_signature` 还告诉你：`p_w` 是 `PARAMETER`，`x` 是 `USER_INPUT`，`state_dict` 的 key 是 `w`。这就是「可执行契约」的含义——后端知道哪些输入要用户给，哪些从权重文件加载。

## 示例三：ONNX Graph

```python
import io, onnx

buf = io.BytesIO()
torch.onnx.export(
    m, (x,), buf,
    input_names=["x"], output_names=["y"],
    opset_version=18,
)
model = onnx.load_model_from_string(buf.getvalue())
print("opset", [(o.domain, o.version) for o in model.opset_import])
for n in model.graph.node:
    print(n.op_type, "| in:", list(n.input), "| out:", list(n.output))
print("initializer", [(i.name, list(i.dims)) for i in model.graph.initializer])
```

实测输出（省略导出器日志）：

```text
opset [('', 18)]
MatMul | in: ['x', 'w'] | out: ['matmul']
Add | in: ['matmul', 'val_0'] | out: ['add']
Relu | in: ['add'] | out: ['y']
initializer [('w', [8, 8]), ('val_0', [])]
```

ONNX 的节点名是算子类型（`MatMul`/`Add`/`Relu`），边是字符串名字，`w` 和标量 `1.0` 都在 `initializer`。图输入只有 `x`，输出只有 `y`——参数不在图输入里，这是和 `torch.export` 最大的结构差异。

再用 ONNX Runtime 跑一遍，确认它是可执行契约：

```python
import onnxruntime as ort
sess = ort.InferenceSession(buf.getvalue(), providers=["CPUExecutionProvider"])
out = sess.run(None, {"x": x.numpy()})[0]
print("maxerr", float(abs(out - m(x).detach().numpy()).max()))
print("providers", sess.get_providers())
```

实测输出：

```text
maxerr 4.76837158203125e-07
providers ['CPUExecutionProvider']
```

`maxerr` 不是 0：ONNX Runtime 的 MatMul 实现和 PyTorch 的累加顺序不同。对 float32 来说，`4.8e-07` 完全正常。

把三种图的节点逐项排在一起（同一模型、同一输入，实测）：

| 位置 | FX Graph | ATen Graph | ONNX Graph |
|---|---|---|---|
| 图输入 | `x` | `p_w`, `x` | `x` |
| 权重 | `get_attr w` | `placeholder p_w` | `initializer w` |
| matmul | `operator.matmul` | `aten.matmul.default` | `MatMul` |
| add | `operator.add`（`1.0` 内联） | `aten.add.Tensor`（`1.0` 内联） | `Add`（`val_0` initializer） |
| relu | `torch.relu` | `aten.relu.default` | `Relu` |
| 输出 | `relu` | `(relu,)` | `y` |

差异不在「有几个算子」，而在**参数归属**和**命名空间**：FX 的 `w` 在 module 上，ATen 的 `p_w` 是图输入，ONNX 的 `w` 是 initializer。

## 示例四：Relax

TVM 0.27 自带 PyTorch frontend，可以直接从 `ExportedProgram` 转 Relax：

```python
from tvm.relax.frontend.torch import from_exported_program
mod = from_exported_program(ep)
print(mod.script())
```

实测输出：

```text
# from tvm.script import ir as I
# from tvm.script import relax as R

@I.ir_module
class Module:
    @R.function
    def main(x: R.Tensor((4, 8), dtype="float32")) -> R.Tuple(R.Tensor((4, 8), dtype="float32")):
        with R.dataflow():
            lv: R.Tensor((4, 8), dtype="float32") = R.matmul(x, metadata["ir.GenericConst"][0], out_dtype="float32")
            lv1: R.Tensor((4, 8), dtype="float32") = R.add(lv, R.const(1.0, "float32"))
            lv2: R.Tensor((4, 8), dtype="float32") = R.nn.relu(lv1)
            gv: R.Tuple(R.Tensor((4, 8), dtype="float32")) = (lv2,)
            R.output(gv)
        return gv

# Metadata omitted. Use show_meta=True in script() method to show it.
```

三个新东西：

1. **类型写在签名里**：`x: R.Tensor((4, 8), dtype="float32")`，返回也是 `R.Tuple(...)`。
2. **`R.dataflow()` 块**：块内每个 `lv` 都是 SSA 绑定，块尾 `R.output(gv)` 标记输出。
3. **参数被冻结成常量**：`metadata["ir.GenericConst"][0]` 就是权重。默认 `keep_params_as_input=False`；传 `True` 可以把它变回函数参数。

源码依据：`from_exported_program` 定义在 `tvm/relax/frontend/torch/exported_program_translator.py:2352`，`keep_params_as_input` 的默认值在 `:2355`；Relax 的 `Function` 在 `tvm/relax/expr.py:357`（tvm 0.27.0.post1）。

传 `keep_params_as_input=True` 后，签名会变成两个输入（实测）：

```text
def main(x: R.Tensor((4, 8), dtype="float32"), p_w: R.Tensor((8, 8), dtype="float32")) -> R.Tuple(R.Tensor((4, 8), dtype="float32")):
    R.func_attr({"num_input": 1, "params": [metadata["ffi.Tensor"][0]]})
```

`num_input=1` 表示第一个参数是用户输入，`p_w` 是参数。**同一个前端，换个开关，参数的归属就变了**——这也是为什么「图能不能独立执行」必须先问清楚。

## 示例五：StableHLO 的函数式写法

> `guided`：本机没有 `torch_xla`/JAX，无法真的从 PyTorch 导出 StableHLO。下面这段是按 StableHLO 规范手写的等价形式，用来对照语法，不是实测输出。

```mlir
func.func @main(%x: tensor<4x8xf32>, %w: tensor<8x8xf32>) -> tensor<4x8xf32> {
  %c1 = stablehlo.constant dense<1.000000e+00> : tensor<f32>
  %c0 = stablehlo.constant dense<0.000000e+00> : tensor<f32>
  %b1 = stablehlo.broadcast_in_dim %c1, dims = [] : (tensor<f32>) -> tensor<4x8xf32>
  %b0 = stablehlo.broadcast_in_dim %c0, dims = [] : (tensor<f32>) -> tensor<4x8xf32>
  %mm = stablehlo.dot_general %x, %w,
        contracting_dims = [1] x [0] : (tensor<4x8xf32>, tensor<8x8xf32>) -> tensor<4x8xf32>
  %add = stablehlo.add %mm, %b1 : tensor<4x8xf32>
  %relu = stablehlo.maximum %add, %b0 : tensor<4x8xf32>
  return %relu : tensor<4x8xf32>
}
```

注意三件事：

- 标量 `1.0` 不能直接加到 `tensor<4x8xf32>` 上，必须先 `broadcast_in_dim`。
- `matmul` 统一成 `dot_general`，用 `contracting_dims` 描述「哪个轴和哪个轴相乘」。
- 没有 `get_attr`、没有 `initializer`，权重是函数参数 `%w`，常量是 SSA 值。

这就是「函数式」的含义：**每个值都有定义点，没有隐式状态，没有原地修改**。对编译器来说，这比 ONNX 好分析得多。

## 五列对照表

| 维度 | FX Graph | torch.export (ATen) | ONNX | Relax | StableHLO |
|---|---|---|---|---|---|
| 节点粒度 | Python 函数（`operator.matmul`） | ATen op + overload（`aten.matmul.default`） | 标准算子（`MatMul`） | Relax op（`R.matmul`） | StableHLO op（`dot_general`） |
| 参数表示 | `get_attr` 挂 module | `placeholder[p_w]` + `state_dict` | `initializer` | 默认冻结为 `GenericConst`，可设为参数 | 函数参数 |
| 常量 | 内联在 `args` | 内联在 `args` | `initializer`（如 `val_0`） | `R.const` | `stablehlo.constant` |
| shape/dtype | 可选 `node.meta` | `graph_signature` + `SymInt` | 静态 shape（可动态轴） | 写进 `Var` 类型 | 写进 tensor 类型 |
| 控制流 | 抓图时容易丢 | 支持 `cond`/`while_loop`（有限） | `If`/`Loop` 特殊算子 | 原生 `if`/`while` + dataflow | 结构化控制流 |
| 主要用途 | 原型、pass、教学 | 后端编译、导出 | 跨框架部署 | TVM 图优化 | XLA/MLIR 编译 |
| 本机实测 | 是 | 是 | 是（含 ORT） | 是 | 否（`guided`） |

这张表最该记住的是「参数表示」和「shape/dtype」两行：它们决定了后端拿到图之后，还要不要额外问前端要信息。FX 需要 module 才能跑，ATen 需要 `state_dict` 才能跑，ONNX 自带 initializer，Relax 默认把参数冻结进图，StableHLO 把参数当函数入参。**同一张图「能不能独立执行」，差别就在这两行。**

「控制流」一行是对能力的描述，本机没有实测 `cond`/`while_loop` 的导出；本章的实测只覆盖直线图。

## 对比与取舍

选择 IR 的判断顺序：

1. **你要改图还是部署？** 改图选 FX（最容易写 pass）；部署选 ONNX 或 ATen（契约清楚）。
2. **你要跨框架吗？** 跨框架只有 ONNX 现实；同框架内 ATen 更完整。
3. **你要做 layout/fusion 吗？** Relax/StableHLO 的类型和 SSA 更适合；ONNX 做这些很别扭。
4. **你的后端是什么？** TVM 吃 Relax/TIR；XLA 吃 HLO/StableHLO；自定义后端吃 ATen 或 ONNX。

一个常见的误区是「一张 IR 走天下」。真实生产栈通常至少两层：PyTorch → ATen → 某个后端 IR。**IR 之间的转换点，就是信息和性能的取舍点。**

## 踩雷集锦

1. **以为 `torch.export` 和 `symbolic_trace` 等价**：前者是 ATen 契约，后者是 Python 算子图；参数绑定、overload、返回值都不同。
2. **在 FX 图里找权重**：权重在 `get_attr` 指向的 module 属性里，不在图节点里；`state_dict` 也不会自动跟着图走。
3. **把 ONNX 的 `initializer` 当图输入**：ORT 会自己加载 initializer；你只需要喂 `graph.input` 里的用户输入。
4. **忽略 opset 版本**：同一个 `Add` 在不同 opset 下属性可能不同；导出时写明版本，别用默认值碰运气。
5. **以为 Relax 的 `GenericConst` 是 bug**：它是默认「冻结参数」策略；要当参数用就传 `keep_params_as_input=True`。
6. **把 StableHLO 的标量加法直接写上去**：必须显式 `broadcast_in_dim`，否则类型不匹配。
7. **拿不同 IR 的输出直接 `==` 比较**：不同后端的累加顺序不同，用 `allclose`/`maxerr`；本章实测 `maxerr` 在 `1e-7` 量级。
8. **以为 ONNX 能保留 Python 控制流**：`if`/`for` 在导出时会被特化或展开；动态控制流要显式建 `If`/`Loop` 或换 IR。
9. **把 `state_dict` 当成图的一部分**：`ep.state_dict` 和 `ep.graph` 是两个对象；只存图不存权重，模型跑不起来。

## 进阶：再往深一层

- 用 `torch.export.export(..., dynamic_shapes=...)` 导出动态 batch，观察 `SymInt` 出现在 `graph_signature` 和节点参数里的位置。
- 把 `from_exported_program(ep, keep_params_as_input=True)` 跑一遍，对比 Relax 的 `main` 签名和 `R.matmul` 的第二个参数。
- 用 `torch.fx.graph_module.GraphModule` 手写一个节点替换 pass，把 `torch.relu` 换成 `torch.nn.functional.relu`，观察 FX 图的可塑性（Ch 5 展开）。
- 读 ONNX 的 operator schema（https://onnx.ai/onnx/operators/），对照 `MatMul`/`Add`/`Relu` 的属性；再看 StableHLO 规范里 `dot_general` 的 `contracting_dims`。

## 动手练习

### 练习 1：数节点，解释差异

对同一模型，分别统计 FX、ATen、ONNX 的 `call_function`/节点数量，并解释为什么 ATen 多一个 `placeholder`、ONNX 少一个图输入。

<details>
<summary>参考思路</summary>

FX：1 个 `placeholder`（x）+ 1 个 `get_attr`（w）+ 3 个 `call_function`。ATen：2 个 `placeholder`（p_w、x）+ 3 个 `call_function`，因为参数被提升为输入。ONNX：3 个节点 + 2 个 initializer，图输入只有 x，因为 w 和标量都进了 initializer。
</details>

### 练习 2：把常量改成参数

把 `y + 1.0` 改成 `y + self.b`（`b` 是 `nn.Parameter`），重新导出三种 IR，观察常量/参数表示怎么变。

<details>
<summary>参考思路</summary>

FX 多一个 `get_attr`；ATen 多一个 `placeholder[p_b]` 和 `state_dict` 里的 `b`；ONNX 多一个 `initializer`（形状 `[8]`，如果广播）和 `Add` 的第二个输入。Relax 则多一个 `GenericConst` 或参数。
</details>

### 练习 3：写一个最小的 ONNX 检查器

用 `onnx.load` 读一个导出文件，打印每个节点的 `op_type`、输入、输出，以及所有 initializer 的名字和形状。要求：不依赖 ORT。

<details>
<summary>参考思路</summary>

遍历 `model.graph.node`，用 `list(n.input)`/`list(n.output)`；initializer 用 `i.name`、`list(i.dims)`。再检查 `model.opset_import` 的版本。这个脚本是 Ch 22 分析 ONNX 图的基础。
</details>

### 练习 4：手写 StableHLO 片段

把本章的 `x @ W + 1 → relu` 改写成 StableHLO，要求显式 broadcast，并用 `dot_general` 的 `contracting_dims` 表示 matmul。

<details>
<summary>参考思路</summary>

参考本章示例五：先 `stablehlo.constant` 造标量，再 `broadcast_in_dim` 到 `4x8`，然后 `dot_general`、`add`、`maximum`。注意 `contracting_dims = [1] x [0]` 对应 `(4x8) @ (8x8)`。
</details>

## 本章重点整理

- FX 是 Python 算子图，参数在 `get_attr`，适合原型和自定义 pass。
- `torch.export` 是 ATen 契约，参数变 `placeholder`，算子带 overload，返回 tuple。
- ONNX 是可序列化算子图，权重进 `initializer`，靠 opset 版本约束语义，适合部署。
- Relax 把 shape/dtype 写进类型，用 `R.dataflow()` 做 SSA 绑定，默认冻结参数。
- StableHLO 是函数式、显式 broadcast、`dot_general` 统一的 IR，适合 XLA/MLIR 生态。
- 抓图是有损的：参数绑定、控制流、别名、动态形状，每换一种 IR 都要重新确认。

## 自我检核

- [ ] 我能画出同一模型的 FX/ATen/ONNX 三种图，并指出参数在哪
- [ ] 我能解释 `get_attr`、`placeholder`、`initializer`、`GenericConst` 的区别
- [ ] 我能说出 ATen 的 `aten.add.Tensor` 里 `.Tensor` 是什么意思
- [ ] 我知道 ONNX 的 opset 版本为什么重要
- [ ] 我能解释 Relax 的 `R.dataflow()` 和 `R.output()` 各自做什么
- [ ] 我能写出 StableHLO 的显式 broadcast，不把标量直接加在 tensor 上
- [ ] 我知道本机为什么跑不了 StableHLO 导出（`guided`）
- [ ] 我能根据「改图/部署/跨框架/后端」四个问题选 IR

## 延伸阅读

- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：读 `Graph`/`Node`/`symbolic_trace`，对照本章节点类型。与本章关联：FX 层的 API 基础。
- **torch.export 文档**（https://pytorch.org/docs/stable/export.html）：读「Export IR」和 `graph_signature`。与本章关联：ATen 契约与动态形状。
- **ONNX Operators**（https://onnx.ai/onnx/operators/）：查 `MatMul`/`Add`/`Relu` 的输入输出与属性。与本章关联：ONNX 算子集。
- **StableHLO 规范**（https://openxla.org/stablehlo）：读 `dot_general`、`broadcast_in_dim`、`maximum`。与本章关联：函数式 IR 的写法。
- **TVM Relax 文档**（https://tvm.apache.org/docs/relax/）：读 `R.dataflow` 与 `R.call_tir`。与本章关联：Relax 如何从图级降到 TIR。

---

→ [Ch 3 张量语义：shape、stride、broadcast 与 layout](./03-tensor-semantics.md)