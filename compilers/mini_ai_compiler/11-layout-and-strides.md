# 第 11 章 布局与 stride：shape 是视图，stride 才是内存

> **目标**：把布局（layout）拆成三个可以验证的问题：形状怎么变成 stride、广播怎么用 stride 0 表达、非连续数据在哪一层被强制拷贝。读完后你能解释 `TensorType.strides` 为什么永远是行主序、`broadcast_strides` 的 0 是怎么进到 C 下标里的，以及为什么把 F-order 数组直接喂给 DLL 会读到错误数据。

> **环境**：Windows 10/11 x86_64，Python 3.11.12，numpy 2.4.6，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

“非连续输入”与“权重转置”两节要编译 DLL，需要 MSVC；`code/mini_ai/codegen_c.py` 会自动查找 `vcvars64.bat`。除标注了导入的片段外，本章 Python 代码块按顺序在同一个会话中执行。

## 为什么需要这个？

第 4 章 已经建立了“shape 是逻辑坐标、stride 是内存步长”的直觉。这一章要把它落到本仓库的代码上，因为这里的 IR 做了一个非常重要的简化选择：

```python
@dataclass(frozen=True)
class TensorType:
    shape: tuple[int, ...]
    dtype: str = "float32"
```

**没有 `strides` 字段。** 每个 `TensorType` 的 stride 都是从 shape 现推的行主序值。这意味着：

- IR 里所有中间值都被承诺为连续（contiguous），布局契约是隐式的；
- 广播不用额外表示，靠 `broadcast_strides` 在代码生成时算出 0；
- 非连续数据只能停在图的边界，进 IR 之前必须被拷贝。

这三条带来一个直接后果：**布局错误不会在 IR 层报错**。传一个 F-order 数组进来，`shape` 对得上，`validate()` 通过，解释器甚至能算对（NumPy 自己认 stride），但 C 后端按下标平铺读，读到的元素位置全错。本章实测误差 `20.0`。

还有一个反向的例子：`nn.Linear` 的权重是 `(out, in)`，matmul 需要 `(in, out)`。前端在 trace 时做了一次真实的布局转换（`.T.copy()`），这次拷贝的成本被算进了“编译期”，不在推理路径上。理解这次转换发生在哪，才能判断哪些拷贝是必要的、哪些是可以延后的。

## 先建立直觉：一维存储，三个坐标

```text
storage:  [ 0  1  2  3  4  5 ]         # 一维 float32 数组
shape:    (2, 3)
strides:  (3, 1)                        # 元素为单位

取 [i, j] 的地址：offset = i*3 + j*1
  [0,0]=0  [0,1]=1  [0,2]=2
  [1,0]=3  [1,1]=4  [1,2]=5
```

广播不是“复制数据”，而是“把某一维的步长设成 0”：

```text
bias:     shape (3,)          strides (1,)
输出:     shape (2, 3)
对齐后:   bias 视作 (1, 3)     strides (0, 1)
         第 0 维步长 0  →  两个 i 都读到同一行
取 [i, j]：offset = i*0 + j*1 = j     ← 与 i 无关
```

标量更极端：`shape ()`，`strides ()`，在 `(2, 3)` 的循环里两个维度都给 0，下标恒为 `0`。

```text
广播的三种形态
  (3,)   -> (2,3)   strides (0,1)   按列复用
  (2,1)  -> (2,3)   strides (1,0)   按行复用
  ()     -> (2,3)   strides (0,0)   全部复用
```

## 核心概念一：`strides` 是推导出来的

`TensorType.strides` 是一段只依赖 shape 的计算：

```python
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

从最后一维往前累乘，再反转回来。实测：

```text
(3,)      numel= 3  strides= (1,)      rank= 1
(2, 3)    numel= 6  strides= (3, 1)    rank= 2
(2, 3, 4) numel= 24 strides= (12, 4, 1) rank= 3
()        numel= 1  strides= ()        rank= 0
```

这个属性的两个后果：

1. **同一 shape 只有一套 stride。** 你不能用 `TensorType((2, 3))` 表示一个 F-order 的 `(2, 3)`。想表达列主序，只能换 shape 或换一层表示。
2. **stride 永远与 shape 自洽。** 不会出现“shape 是 `(2, 3)` 但 stride 是 `(1, 2)`”这种非连续状态，所以 `_linear_index` 可以放心用 `TensorType(shape).strides`，不必从别处取。

`numel` 也一样是推导值（所有维度相乘），`Value.data` 只对常量有意义，其余值的实际存储由 `plan_memory` 决定（第 12–13 章）。布局与内存规划是两个正交的问题：一个决定“怎么算下标”，一个决定“buffer 放在哪”。

## 核心概念二：广播是形状规则，不是数据搬运

形状推断用 `broadcast_shape`：

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

低秩 shape 在**前面**补 1（不是后面），这是 NumPy 的规则：`(3,)` 与 `(2, 3)` 对齐成 `(1, 3)` 与 `(2, 3)`。补在前面意味着最后一维始终对齐，bias 这类“按最后一维”的参数才能自然广播。

真正给代码生成用的是 `broadcast_strides`：

```python
def broadcast_strides(in_shape: tuple[int, ...], out_shape: tuple[int, ...]) -> tuple[int, ...]:
    """Strides (in elements) for reading an input into an output-shaped loop.

    A stride of zero means the input dimension is broadcast.
    """
    if len(in_shape) > len(out_shape):
        raise IRError(f"cannot broadcast rank {len(in_shape)} to rank {len(out_shape)}")
    in_strides = TensorType(in_shape).strides
    padded_shape = (1,) * (len(out_shape) - len(in_shape)) + in_shape
    padded_strides = (0,) * (len(out_shape) - len(in_shape)) + in_strides
    out: list[int] = []
    for d, s, out_d in zip(padded_shape, padded_strides, out_shape):
        if d == out_d:
            out.append(s)
        elif d == 1:
            out.append(0)
        else:
            raise IRError(f"cannot broadcast {in_shape} to {out_shape}")
    return tuple(out)
```

实测六组输入输出：

```text
(3,)    -> (2, 3): (0, 1)
(1, 3)  -> (2, 3): (0, 1)
(2, 1)  -> (2, 3): (1, 0)
(2, 3)  -> (2, 3): (3, 1)
()      -> (2, 3): (0, 0)
(1, 1)  -> (2, 3): (0, 0)
```

两点值得注意：

- `(2, 1) -> (2, 3)` 得到 `(1, 0)`：第 0 维步长 1，第 1 维步长 0。也就是说同一个 `i` 行里，`j` 怎么变都读 `x[i, 0]`。
- `(2, 3) -> (2, 3)` 得到 `(3, 1)`，与 `TensorType((2, 3)).strides` 一致 —— 无广播时它就是普通行主序。

## 核心概念三：stride 0 直接吃掉下标项

C 后端用一个函数把 `(shape, strides, 变量名)` 变成下标表达式：

```python
def _linear_index(shape: tuple[int, ...], strides: tuple[int, ...], vars_: list[str]) -> str:
    terms: list[str] = []
    for d, s, var in zip(shape, strides, vars_):
        if s == 0:
            continue
        if s == 1:
            terms.append(var)
        else:
            terms.append(f"({var})*{s}")
    return " + ".join(terms) if terms else "0"
```

三种情况：步长 0 的维度**整项丢弃**，步长 1 的维度只写下标名（省掉 `*1`），其余写乘法。全部丢弃时返回 `"0"`，这就是标量下标。

拿 `add(x (2,3), b (3,))` 举例，生成的循环体是：

```c
    for (int i0_0 = 0; i0_0 < 2; ++i0_0) {
        for (int i0_1 = 0; i0_1 < 3; ++i0_1) {
            v_2_y[(i0_0)*3 + i0_1] = v_0_x[(i0_0)*3 + i0_1] + v_1_b[i0_1];
        }
    }
```

`b` 的 stride 是 `(0, 1)`：第 0 维被丢掉，第 1 维步长是 1 所以只留 `i0_1`。最终就是 `v_1_b[i0_1]`，没有任何显式广播代码。

换成列向量 `col (2, 1)`：

```c
            v_2_y[(i0_0)*3 + i0_1] = v_0_x[(i0_0)*3 + i0_1] + v_1_col[i0_0];
```

stride `(1, 0)`：第 1 维被丢掉，只剩 `i0_0`。

换标量 `s ()`：

```c
            v_2_y2[(i0_0)*3 + i0_1] = v_0_x[(i0_0)*3 + i0_1] * v_1_s[0];
```

stride `(0, 0)`：两项都被丢弃，表达式退化成常量下标 `0`。

这三段是同一个函数在三种 stride 下的输出。**广播在代码里没有任何运行时开销**：没有分支、没有拷贝，只有下标的形状不同。代价是编译器必须在生成代码时就知道每个输入的 stride —— 这也是为什么动态 shape 会让事情复杂一个量级。

## 核心概念四：边界处的 contiguous 契约

IR 没有 strides 字段，所以运行时必须在入口把数据变成行主序连续。两处都做了这件事：

```python
def _f32(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float32)
```

```python
    def run(self, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        inputs = [np.ascontiguousarray(feed[v.name], dtype=np.float32) for v in self.graph.inputs]
```

`ascontiguousarray` 对已经连续的数组返回原对象（零拷贝），对不连续的数组做一次真实拷贝。这层保护是必要的，但**它会安静地掩盖错误**：如果调用方以为“传 F-order 也能跑”，测试会通过，性能会变差，而他不知道差在哪。

先看绕过保护会怎样。直接拿 `ctypes` 调 DLL，不经过 `run_c_graph`：

```python
import ctypes
import numpy as np
from pathlib import Path
from mini_ai.ir import Graph
from mini_ai.codegen_c import compile_graph_c
from mini_ai.interpreter import evaluate_graph, max_abs_error
from mini_ai.runtime import run_c_graph

g = Graph()
x = g.add_input("x", (2, 3))
c = g.add_const("c", np.float32(10.0))
y = g.add_op("mul", [x, c], name="y")
g.add_output(y)
res = compile_graph_c(g, Path(".scratch/mini-ai-tests") / "f_order_raw", name="f_order_raw")

base = np.arange(6, dtype=np.float32).reshape(2, 3)
f_order = np.asfortranarray(base)

lib = ctypes.CDLL(str(res.dll_path))
fn = getattr(lib, "mini_forward")
ptr = ctypes.POINTER(ctypes.c_float)
fn.argtypes = [ptr, ptr]
fn.restype = ctypes.c_int
out = np.zeros((2, 3), np.float32)
rc = fn(f_order.ctypes.data_as(ptr), out.ctypes.data_as(ptr))
print("base:")
print(base)
print("f_order strides:", f_order.strides, "C_CONTIGUOUS:", f_order.flags["C_CONTIGUOUS"])
print("rc:", rc, "raw f_order out:")
print(out)
ref = evaluate_graph(g, {"x": base})[0]
print("expected:")
print(ref)
print("max err (raw):", max_abs_error(out, ref))
print("wrapped run err:", max_abs_error(run_c_graph(g, {"x": f_order}, res.dll_path)[0], ref))
```

真实输出：

```text
base:
[[0. 1. 2.]
 [3. 4. 5.]]
f_order strides: (4, 8) C_CONTIGUOUS: False
rc: 0 raw f_order out:
[[ 0. 30. 10.]
 [40. 20. 50.]]
expected:
[[ 0. 10. 20.]
 [30. 40. 50.]]
max err (raw): 20.0
wrapped run err: 0.0
```

`f_order` 的内存顺序是 `[0, 3, 1, 4, 2, 5]`，kernel 按 `[0,1,2,3,4,5]` 去读，于是每个位置都拿错。`rc: 0` 表示调用成功 —— 没有崩溃，没有异常，只有错误答案。

这道例子说明：**布局契约必须在 API 边界上写清楚**。本仓库的做法是“运行时统一拷贝”，代价是每次调用都可能多一次 4 MB 的搬运。对 `(512, 512)` 的 `add` 实测：

```text
contig   median ms: 1.1268   min: 0.9074
f_order  median ms: 2.2904   min: 1.8196
ratio: 2.033
```

同一个 kernel、同一组数值，只因为输入内存顺序不同，慢了一倍。多出来的时间全是 `ascontiguousarray` 的拷贝。这不是 kernel 慢，是边界契约在收费。

## 核心概念五：trace 时的布局转换

`nn.Linear` 的权重形状是 `(out_features, in_features)`，行主序存储；matmul 需要的右操作数是 `(in_features, out_features)`。前端在 trace 时做了一次显式转换：

```python
                w_t = graph.add_const(
                    f"{node.name}_weight_t", vals[1].data.T.copy()
                )
                out = graph.add_op("matmul", [vals[0], w_t], name=f"{node.name}_matmul")
```

`.T.copy()` 里的 `.copy()` 把视图变成真正的新内存：`W.T` 只是交换了 stride 的视图，不连续；`copy()` 产出一块连续数组。用 PyTorch 侧的三行检查：

```python
import torch

m = torch.nn.Linear(4, 3)
W = m.weight.detach().numpy()
print("linear weight shape:", W.shape, "contiguous:", W.flags["C_CONTIGUOUS"])
print("weight_t shape:", W.T.shape, "contiguous:", W.T.copy().flags["C_CONTIGUOUS"], "view contiguous:", W.T.flags["C_CONTIGUOUS"])
```

```text
linear weight shape: (3, 4) contiguous: True
weight_t shape: (4, 3) contiguous: True view contiguous: False
```

`W.T.copy()` 是 C-contiguous，`W.T` 视图不是。需要说明的是，即使这里不写 `.copy()`，`add_const` 内部也会做一次 `arr.copy()`：

```python
        arr = np.asarray(data, dtype=np.float32)
        ...
        v = self._new_value(name, TensorType(tuple(arr.shape)), kind="constant", data=arr.copy())
```

所以常量最终还是连续的。`.T.copy()` 的价值在于把“这里发生了一次布局转换”写在调用点上，而不是藏在 `add_const` 的第三行。读代码的人一眼能看到：`(3, 4)` 的权重在这里被转成了 `(4, 3)`，形状与 matmul 的右操作数一致。

trace 完的图里还留着原始权重：

```text
constants:
  0_weight: tensor<3, 4> numel=12
  0_bias: tensor<3> numel=3
  linear_weight_t: tensor<4, 3> numel=12
ops:
  00 %linear_matmul = matmul(x0, linear_weight_t) : tensor<2, 3>
  01 %linear = add(linear_matmul, 0_bias) : tensor<2, 3>
```

`0_weight` 在 `ops` 里没有任何用户，但它还在 `graph.values` 里。`from_torch` 结尾只调用 `validate()`，不调用 `dce()`，所以这份 12 个 float 的“死常量”会一直留到 `standard_pipeline` 的 `dce` 才被 `prune_dead_values` 清掉。布局转换的中间产物（原权重）与最终产物（转置后的权重）同时存在，是 trace 阶段的正常现象。

## 底层机制：布局决定了哪一层循环连续

下标公式本身不复杂：

```text
offset(shape, strides, idx) = Σ idx[d] * strides[d]
```

复杂的是它对性能的影响。以 `matmul` 为例，C 后端生成的访存是：

```c
                acc += v_0_a[(i)*K + (k)] * v_1_b[(k)*N + (j)];
```

- `a[i, k]`：固定 `i` 时，`k` 连续 → 沿内层循环连续访问 `a` 的一行，cache 友好。
- `b[k, j]`：固定 `k` 时，`j` 连续 → 沿内层循环连续访问 `b` 的一行，cache 友好。
- 但 `b` 的访问跨 `k` 时步长是 `N * 4` 字节，一行一行地跳。

如果 `b` 是列主序（`strides=(1,K)`），上面那段代码会变成“内层循环跨行跳”，每次访问都可能 miss。这就是 BLAS 里 `sgemm` 的 `trans_a/trans_b` 参数存在的原因：**布局不是表示偏好，它直接决定循环能不能向量化**。

本仓库选择了“一种布局 + 边界拷贝”的策略，理由是可预测：代码生成器只面对一种 stride 模式，广播靠 0 步长处理，其余情况在入口拷贝一次。代价是失去了“零拷贝转置”的能力 —— 想要 `transpose` 不花钱，就得让 IR 支持任意 stride，后面的所有 pass（尤其内存规划与别名分析）都要跟着变复杂。

## 进一步用法与变体

**变体 1：给 `TensorType` 加 `strides` 字段。** 这是最直接的扩展：`TensorType(shape, strides)`，`broadcast_strides` 改用输入自己的 stride。代价是所有构造点都要传 strides，`__post_init__` 要校验“shape 与 strides 自洽”，并且 `plan_memory` 的 `numel*4` 不再等于真实占用（非连续视图可能共享存储）。

**变体 2：加一个显式 `transpose` op。** 语义上是“换 stride 的视图”，但要落成代码就必须决定“视图还是拷贝”。视图要求后端支持任意 stride；拷贝则等价于一次 `copy` op，成本可以测量。本仓库没有这个 op，所以布局转换只能发生在前端。

**变体 3：分块布局（tiled layout）。** 把 `(M, N)` 存成 `(M/T, N/T, T, T)` 的嵌套块，让 tile 内部连续。这是第 18 章 tiling 的存储侧对应物。收益是 cache 命中率，代价是所有下标计算都要多一层。

**变体 4：把布局作为 pass。** 生产编译器会有“layout propagation” pass：从算子库的偏好出发，向前后传播布局要求，在冲突点插入转换。PyTorch 的 `channels_last` 传播就是这个思路（见 `compilers/ai_compiler/08-layout-and-format.md`）。本仓库没有这一步，因为只支持一种布局。

## 对比与取舍

| 方案 | 优点 | 缺点 |
|---|---|---|
| 单一布局 + 边界拷贝（本章） | 后端实现统一、可预测、广播规则统一 | 非连续输入每次调用都付费 |
| IR 支持任意 stride | 零拷贝转置、视图免费 | 每个 pass 都要处理 stride，别名分析变复杂 |
| 布局传播 + 转换点 | 兼顾性能与通用性 | 需要代价模型决定在哪转换 |

再看“拷贝发生在哪”的取舍：

| 位置 | 成本 | 影响面 |
|---|---|---|
| 前端 trace 时（权重转置） | 一次性，编译期 | 只影响常量 |
| 运行时入口（`ascontiguousarray`） | 每次调用 | 所有非连续输入 |
| 算子内部（按 stride 读） | 每次访问，可能 cache miss | 所有 op，最贵 |

本仓库把成本放在前两处，第三处完全避开。这也是为什么 `_linear_index` 敢直接假设行主序。

## 踩雷集锦

1. **以为 `TensorType.strides` 记录了输入的真实 stride**：它永远按 shape 现算行主序。传一个 F-order 的 `(2, 3)`，IR 看到的仍是 `(3, 1)`。真实 stride 只在 `np.ndarray.flags` 里，进不了 IR。
2. **绕过 `run_c_graph` 直接调 DLL**：本章实测 `max err 20.0`，且 `rc=0`。没有崩溃提示，只有错误数值。任何自写的 ctypes 调用都要自己先 `ascontiguousarray`。
3. **以为 `W.T` 视图会原样进 IR**：`W.T` 的 `C_CONTIGUOUS` 是 `False`，常量最终是连续的（`add_const` 内部 `arr.copy()`）。真正需要记住的是形状：`(3, 4)` 的权重被转成 `(4, 3)`，原始权重作为死常量留在 `graph.values` 里，直到 `dce` 才被清掉。
4. **以为广播要生成复制循环**：广播只改 stride，`_linear_index` 把 0 步长的维度整项丢掉，循环体里没有额外分支。看到生成的 C 里 `v_1_b[i0_1]` 这种下标，不要去找“广播代码”，它已经被编译掉了。
5. **忘了低秩 shape 补在前面**：`(3,)` 与 `(2, 3)` 对齐成 `(1, 3)`，不是 `(3, 1)`。补错方向会让 bias 广播到错误的维度，`broadcast_shape` 可能仍然通过（例如 `(3,)` 与 `(3, 2)`），但数值全错。

## 进阶：再往深一层

- **stride 与别名**：两个连续 buffer 不可能重叠，但两个任意 stride 的视图可以。一旦 IR 支持 stride，`MemoryPlan.validate` 的“区间重叠即冲突”就不再充分，需要真实的 offset 区间分析。
- **对齐与向量化**：`_aligned_malloc(arena_bytes, 64)` 保证 arena 起点 64 字节对齐，但 arena 内部的 offset 只保证 `alignment`（默认 64）对齐。一行 `(N,)` 的起始地址是否 32 字节对齐，取决于 `N*4` 与前一行的 offset。第 17–19 章会看到向量化对这些细节的敏感度。
- **广播与归约的组合**：`sum(add(x, b), axis=1)` 里 `b` 只沿最后一维广播，归约又消掉最后一维。生成的代码会先算出 `out[j] += x[i,j] + b[j]`，b 的读取在内层，连续。把 `b` 换成 `(M, 1)` 的列向量，b 的读取就变成外层一次、内层重复，cache 行为反转。
- **源码对照**：读 `code/mini_ai/ir.py` 的 `TensorType`、`broadcast_shape`、`broadcast_strides` 三段，再读 `codegen_c.py` 的 `_linear_index` 与 `_broadcast_index`。四段代码合起来就是本章的全部机制。

## 动手练习

### 练习 1：手算 stride 与下标

不运行代码，先手算下面三组：

1. `(4, 1, 5)` 的 strides；
2. `(5,) -> (4, 5)` 的 broadcast strides；
3. `(4, 1, 5) -> (4, 3, 5)` 的 broadcast strides。

然后用 `TensorType(...).strides` 与 `broadcast_strides(...)` 验证。第 3 组的答案里应该恰好有一个 0，并解释它落在哪一维。

### 练习 2：制造一次“静默错误”

把本章的 F-order 例子改成 `(3, 2)` 的输入（形状不是方阵），直接调 DLL，记录输出与误差；再用 `run_c_graph` 跑一次。要求：

- 直接调用时 `max_abs_error > 0`；
- 包装调用时 `max_abs_error == 0.0`；
- 解释为什么方阵 `(2, 2)` 时误差可能看起来“只是转置”，而非方阵时误差更乱。

### 练习 3：测量边界拷贝的成本

对 `(N, N)` 的 `add(x, 1.0)`，分别用连续输入与 F-order 输入测 `benchmark`，扫 `N = 128, 256, 512, 1024`。画出（或打印）比值随 N 的变化，解释为什么 N 越大比值越接近 2（提示：拷贝本身也是 O(N²)，两边的耗时都在同一量级）。

### 练习 4：给 `TensorType` 加 stride 字段

设计一个 `TensorType(shape, strides=None)` 的版本：

- `strides=None` 时仍按行主序推导；
- 校验 `strides` 与 `shape` 的长度一致；
- 让 `broadcast_strides` 在输入带 stride 时使用输入自己的 stride。

不需要改后端，只要求 `dump()` 能打印出非连续类型，并写一个测试证明 `(2, 3)` 配 `(1, 2)` 的 stride 能通过校验、配 `(3, 3)` 会失败。

## 本章重点

- `TensorType` 没有 stride 字段，`strides` 永远按 shape 现算行主序；布局契约是隐式的。
- 广播用 stride 0 表达，`broadcast_strides` 对低秩 shape 在**前面**补 1。
- `_linear_index` 把 stride 0 的维度整项丢掉，所以广播在生成的 C 里没有运行时开销。
- 非连续数据在边界被 `ascontiguousarray` 强制拷贝；绕过它直接调 DLL 会得到错误数值（实测 `max err 20.0`）。
- 边界拷贝不是免费的：`(512, 512)` 的 F-order 输入实测慢 `2.03x`。
- `nn.Linear` 的权重转置发生在 trace 时（`.T.copy()`），是一次编译期的布局转换，原始权重会作为死常量留到 DCE。

## 自我检核

- [ ] 我能手算任意 shape 的行主序 strides，并解释为什么是“从后往前累乘”
- [ ] 我能写出 `(2, 1) -> (2, 3)` 的 broadcast strides 并说明 0 落在哪一维
- [ ] 我能解释 `_linear_index` 为什么可以直接丢弃 stride 0 的项
- [ ] 我能复现 F-order 直接调 DLL 的错误输出，并说明 `rc=0` 为什么危险
- [ ] 我知道 `W.T` 与 `W.T.copy()` 在连续性上的区别，以及为什么后者是必要的
- [ ] 我能说出布局如何影响 matmul 内层循环的访存模式
- [ ] 我能列出“拷贝发生在前端 / 边界 / 算子内部”三种方案的代价差异

## 延伸阅读

- **`code/mini_ai/ir.py` 的 `TensorType` / `broadcast_shape` / `broadcast_strides`**：通读这三段。与本章的关联：stride 推导与广播规则的全部实现。
- **`code/mini_ai/codegen_c.py` 的 `_linear_index` / `_broadcast_index` / `_emit_elementwise`**：看 stride 如何变成 C 下标。与本章的关联：三种广播形态的真实代码生成结果。
- **NumPy `ascontiguousarray` 文档**（https://numpy.org/doc/stable/reference/generated/numpy.ascontiguousarray.html）：读“Return a contiguous array in memory (C order)”与返回值语义。与本章的关联：边界拷贝的零拷贝条件。
- **`compilers/ai_compiler/08-layout-and-format.md`**：读 NCHW/NHWC 与 `channels_last` 的传播。与本章的关联：同一组 stride 概念在真实框架里的布局契约与转换点。
- **BLAS `sgemm` 的 `trans_a` / `trans_b`**（https://www.netlib.org/blas/）：读参数说明。与本章的关联：为什么工业 matmul 把“布局”做成参数而不是拷贝。

---

下一章：[第 12 章 生命周期分析：谁在什么时候还活着](./12-liveness-analysis.md)