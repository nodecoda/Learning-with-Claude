# Ch 4 张量语义：shape、dtype、stride 与 broadcast（广播）

> 目标：把“张量”从“一个多维数组”细化成编译器能检查的契约：shape（形状）决定循环边界，dtype（数据类型）决定元素宽度，stride（步长）决定线性地址，broadcast（广播）决定零步长读取，view（视图）与 copy（拷贝）决定何时共享内存。学完本章，你能解释为什么一个转置后的输入会让 C 后端算出错误结果，而解释器仍然正确。

> 环境：Windows 10/11 x86_64；Python 3.11.12；numpy 2.4.6。命令在 `D:\Learning-with-Claude` 下执行，设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"`。

## 为什么需要

同一份数值，放在不同的 shape、dtype 或 stride 下，可能有完全不同的地址布局。编译器如果只看“元素个数”，不看布局，就会生成读写错位的代码。

张量语义同时约束三个下游：

- **解释器**：NumPy 自己理解 stride，能正确执行非连续数组，所以它容易“掩盖”布局问题。
- **C 后端**：只拿到一个裸指针，按行主序和静态 shape 自己算下标。布局不对，结果就错。
- **内存规划**：按 `numel * 4` 字节分配，按 64 字节对齐切偏移。shape 错了，字节数也错。

本章用一个很小的 `2x3` 矩阵贯穿所有例子。先把地址算清楚，再看广播和 view/copy，最后复现一个“不报错但算错”的边界案例。

## 先建立直觉

### 行主序的线性地址

```text
shape = (2, 3)
strides = (3, 1)          # 单位：元素，不是字节

逻辑坐标        线性下标
(0,0) (0,1) (0,2)   ->   0  1  2
(1,0) (1,1) (1,2)   ->   3  4  5

offset(i, j) = i * 3 + j * 1
```

C 后端生成的循环就是按这个公式写下标：

```c
v_out[(i) * 3 + (j)] = ...
```

如果 shape 是 `(2, 3, 4)`，strides 是 `(12, 4, 1)`，`offset(i,j,k) = i*12 + j*4 + k`。

### broadcast 的零步长

```text
A: (2, 3)        B: (3,)
strides A: (3, 1)
strides B 读进 (2,3) 循环: (0, 1)
                              ^
                              ｜
                    第 0 维步长为 0，重复读同一行

for i in 0..2:
  for j in 0..3:
    out[i,j] = A[i,j] + B[0* i + 1* j]
```

零步长不是“没有 stride”，而是“沿这个维度移动时不换地址”。这正好表达“把一行复制到多行”。

### view 与 copy

```text
a = [0, 1, 2, 3, 4, 5]        shape (6,)
v = a.reshape(2, 3)           view，共享内存
t = v.T                       view，shape (3,2)，strides (1,3) 元素
t[0,0] = 99                   a[0] 也变成 99

c = t.copy(order="C")         copy，新内存，strides (2,1)
```

view 改的是“怎么解释这块内存”，copy 改的是“换一块内存”。编译器必须知道当前值是哪一种，否则优化会破坏别名关系。

## 核心内容

### 4.1 TensorType 的五个属性

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

`frozen=True` 保证类型对象不可变。`shape` 的每一维必须是正整数；长度为 0 的维度和负维都被拒绝。空元组 `()` 表示标量，`numel` 为 1。

```python
from mini_ai.ir import TensorType

for shape in [(2, 3), (2, 3, 4), ()]:
    t = TensorType(shape)
    print(shape, t.strides, t.numel, t.rank, str(t))
```

实际输出：

```text
(2, 3) (3, 1) 6 2 tensor<2, 3>
(2, 3, 4) (12, 4, 1) 24 3 tensor<2, 3, 4>
() () 1 0 tensor<scalar>
```

### 4.2 dtype 只有 float32

`TensorType` 只接受 `float32`：

```text
IRError: only float32 is supported, got float64
```

但 `Graph.add_const` 会把输入强制转成 `float32`：

```python
arr = np.asarray(data, dtype=np.float32)
```

这意味着传入 `float64` 常量不会报错，而是静默降精度。对权重来说，这通常可以接受；对需要高精度的累加常量来说，这是一个隐蔽的误差来源。类型系统的约束只在 `TensorType` 层，不在 `add_const` 的入口。

### 4.3 行主序 strides 的递推

```python
out: list[int] = []
acc = 1
for d in reversed(self.shape):
    out.append(acc)
    acc *= d
return tuple(reversed(out))
```

从最后一维往前：

- 最后一维的 stride 是 1。
- 倒数第二维的 stride 是最后一维的长度。
- 再往前一维的 stride 是后面所有维度长度的乘积。

这就是 C 顺序（row-major）。`(2,3,4)` 的计算过程是：

```text
k: stride 1,  acc = 4
j: stride 4,  acc = 12
i: stride 12, acc = 24
```

所以 `strides = (12, 4, 1)`。

### 4.4 broadcast_shape：从右对齐，逐维合并

```python
def broadcast_shape(*shapes: tuple[int, ...]) -> tuple[int, ...]:
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

规则与 NumPy 一致：

1. 维度较少的 shape 在左边补 1。
2. 每一维如果只有一个非 1 值，就取它。
3. 如果有两个不同的非 1 值，报错。
4. 全是 1 时，结果为 1。

实际输出：

```text
bcast (4,1),(1,3) -> (4, 3)
bcast (), (2,3) -> (2, 3)
```

失败路径：

```text
IRError: shapes are not broadcastable: ((2, 3), (2, 4))
```

### 4.5 broadcast_strides：把输入映射到输出循环

```python
def broadcast_strides(in_shape: tuple[int, ...], out_shape: tuple[int, ...]) -> tuple[int, ...]:
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

实际输出：

```text
strides (3,) -> (2,3): (0, 1)
strides (2,1) -> (2,3): (1, 0)
```

`(3,)` 的第一维被补成 1，所以该维 stride 为 0，表示重复读同一行。`(2,1)` 的第二维是 1，所以该维 stride 为 0，表示重复读同一列。

失败路径：

```text
IRError: cannot broadcast (4,) to (2, 3)
```

### 4.6 C 后端如何使用 stride

`codegen_c.py` 里的 `_linear_index` 把 shape、strides 和循环变量拼成 C 下标表达式：

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

`_broadcast_index` 先调用 `broadcast_strides`，再交给 `_linear_index`。所以广播在生成代码里表现为“某些维度不出现”：

```c
/* add: y */
for (int i0_0 = 0; i0_0 < 2; ++i0_0) {
    for (int i0_1 = 0; i0_1 < 3; ++i0_1) {
        v_2_y[(i0_0)*3 + i0_1] = v_0_x[(i0_0)*3 + i0_1] + v_1_b[i0_1];
    }
}
```

`v_1_b[i0_1]` 里没有 `i0_0`，因为 `b` 的第 0 维 stride 是 0。这就是广播在代码生成层的落点。

### 4.7 view 与 copy 的真实行为

```python
import numpy as np

a = np.arange(6, dtype=np.float32)
v = a.reshape(2, 3)
t = v.T
print("v shares a:", np.shares_memory(a, v), "v strides", v.strides)
print("t shares v:", np.shares_memory(v, t), "t strides", t.strides,
      "C_CONTIGUOUS", t.flags["C_CONTIGUOUS"])
t[0, 0] = 99
print("after t[0,0]=99, a:", a.tolist())
c = t.copy(order="C")
print("copy shares t:", np.shares_memory(t, c), "strides", c.strides)
```

实际输出：

```text
v shares a: True v strides (12, 4)
t shares v: True t strides (4, 12) C_CONTIGUOUS False
after t[0,0]=99, a: [99.0, 1.0, 2.0, 3.0, 4.0, 5.0]
copy shares t: False strides (8, 4)
```

三个结论：

- `reshape` 和 `T` 都可以是 view，不复制数据。
- view 的 strides 变了，逻辑顺序也变了，但底层内存没变。
- `copy(order="C")` 才分配新内存，并把 strides 变成行主序。

### 4.8 runtime 的连续化

`runtime.py` 的 `_f32`：

```python
def _f32(a: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(a, dtype=np.float32)
```

`np.ascontiguousarray` 对已经是行主序连续的数组直接返回，对非连续数组复制一份。实际验证：

```python
cc = np.ascontiguousarray(t)
print("ascontiguousarray shares t:", np.shares_memory(t, cc), "strides", cc.strides)
```

```text
ascontiguousarray shares t: False strides (8, 4)
```

这一步是 C 后端的必要前提：`ctypes` 传的是 `a.ctypes.data_as(ptr)`，C 侧只知道“从这里开始的连续 float”。如果传入非连续数组而不复制，C 会按行主序读出一堆错位的数据。

### 4.9 边界错误：形状不检查会静默算错

解释器会检查输入 shape：

```python
g = Graph()
x = g.add_input("x", (2, 3))
y = g.add_op("relu", [x])
g.add_output(y)
evaluate_graph(g, {"x": np.zeros((3, 2), np.float32)})
```

实际报错：

```text
ValueError: input x: expected (2, 3), got (3, 2)
```

缺输入也会报：

```text
KeyError: "missing input 'x'; got []"
```

但 `run_c_graph` 只检查输入个数，不检查 shape。假设 `dll_path` 是你用 `compile_graph_c(g, ...)` 为这张 relu 图生成的 DLL：

```python
orig = np.array([[1, 2, 3], [4, 5, 6]], np.float32)
out = run_c_graph(g, {"x": orig.T}, dll_path)[0]
```

实际输出：

```text
transposed feed output:
 [[1. 4. 2.]
 [5. 3. 6.]]
numpy relu(orig):
 [[1. 2. 3.]
 [4. 5. 6.]]
```

没有异常，没有错误码，数值就是错的。原因是 `orig.T` 是 `(3,2)` 的非连续 view，`_f32` 把它按逻辑顺序复制成 6 个连续元素 `[1,4,2,5,3,6]`，C 再按 `(2,3)` 行主序解释成 `[[1,4,2],[5,3,6]]`。这个例子说明：**运行时的 shape 契约必须由调用方保证，或者由封装层显式检查。**

其他边界：

```text
IRError: constants with rank > 4 are not supported: (1, 1, 1, 1, 1)
IRError: invalid shape: (2, 0, 3)
IRError: sum axis 3 out of range for (2, 3)
```

## 底层机制

### 4.10 stride 是“逻辑坐标到线性地址”的函数

对任意 rank，地址公式是：

```text
offset(i0, i1, ..., in) = sum_k i_k * stride_k
```

行主序只是选择了一组特定的 stride。编译器可以用不同的 stride 表达转置、切片、广播，而不复制数据。代价是后端必须真的按 stride 算地址；如果后端假设 stride 固定为行主序，就必须在入口做连续化。

### 4.11 broadcast 是“右对齐 + 零步长”

`broadcast_shape` 负责决定输出每一维多大，`broadcast_strides` 负责决定输入每一维走多快。两者分开的原因是：

- shape 只关心“输出长什么样”。
- stride 只关心“输入怎么读”。

一个输入可以在某个维度上长度为 1，但输出该维度大于 1；此时 stride 为 0。反过来，如果输入该维度不是 1 且不等于输出，就无法广播。

### 4.12 为什么 C 后端不直接支持任意 stride

当前 `Graph` 的 `Value` 只带 `TensorType`，没有“运行时 stride”字段。C 后端默认所有值都是行主序连续，输入在 `runtime._f32` 里被强制连续化。这样做的好处是代码生成简单，代价是无法零拷贝地接受转置输入。

如果要支持任意 layout，需要：

- 在 `TensorType` 或运行时增加 stride。
- 代码生成按动态 stride 读。
- 内存规划考虑非连续输出。
- 运行时校验 stride 与 shape 一致。

这属于 Ch 11 的布局转换主题，本章只建立“stride 决定地址”的直觉。

## 进一步用法与变体

### 4.13 保留维度

`sum` 的 `keepdims=True` 会保留被归约的维度为 1：

```python
s = g.add_op("sum", [x], {"axis": 1, "keepdims": True})
```

对 `(2,3)` 沿 axis=1 求和，`keepdims=False` 得到 `(2,)`，`keepdims=True` 得到 `(2,1)`。后者可以直接参与 `(2,3)` 的广播，不需要手动 reshape。

### 4.14 负数轴

`infer_output_type` 会把负轴归一：

```python
if axis < 0:
    axis += len(shape)
```

`axis=-1` 表示最后一维。越界仍然报错：

```text
IRError: sum axis 3 out of range for (2, 3)
```

### 4.15 布局转换的扩展方向

当前没有 `transpose` 或 `contiguous` 算子。要支持它们，可以：

- 增加 `transpose` op，输出新的 `TensorType`，后端按交换后的 stride 读。
- 增加 `contiguous` op，在运行时真正复制成行主序。

这些都会把“view vs copy”从注释变成 IR 里的显式语义。

## 对比与取舍

| 维度 | 行主序（C order） | 列主序（Fortran order） |
| --- | --- | --- |
| 最后一维 stride | 1 | 第一维 stride 为 1 |
| C 后端友好度 | 高 | 需要改索引 |
| NumPy 默认 | 是 | 否 |
| 转置成本 | view 免费，后端要处理 stride | 同理 |

| 维度 | view | copy |
| --- | --- | --- |
| 分配新内存 | 否 | 是 |
| 修改是否影响原数组 | 是 | 否 |
| strides 可否变化 | 是 | 通常变成连续 |
| 编译器别名风险 | 高 | 低 |

| 维度 | NumPy 解释器 | C 后端 |
| --- | --- | --- |
| 非连续输入 | 直接支持 | 必须先连续化 |
| shape 检查 | `evaluate_graph` 检查 | `run_c_graph` 不检查 |
| 地址计算 | NumPy 内部 | 生成的 `_linear_index` |
| 错误表现 | 抛异常 | 可能静默算错 |

## 踩雷集锦

1. **把转置输入直接喂给 `run_c_graph`。** 它不检查 shape，`_f32` 复制逻辑顺序，C 按行主序读，结果错但不报错。上面的 `[[1,4,2],[5,3,6]]` 就是实例。
2. **以为 `add_const` 会保留 `float64`。** 它会强制转 `float32`；需要高精度时，必须在 IR 之外处理。
3. **以为维度大小可以为 0。** `TensorType` 要求每一维 `> 0`，`(2,0,3)` 直接报 `invalid shape`。
4. **广播方向搞反。** `(3,)` 广播到 `(2,3)` 是合法的，strides 为 `(0,1)`；`(4,)` 广播到 `(2,3)` 不合法。
5. **把 view 当成 copy。** 修改转置视图会改到原数组；如果编译器假设独立缓冲区，就会破坏别名安全。

## 进阶

- 给 `TensorType` 增加可选的 `strides` 字段，让 IR 能表达非连续布局。
- 实现 `transpose` 的代码生成：输出 shape 交换，输入按交换后的 stride 读，不复制数据。
- 在 `run_c_graph` 里增加 shape 校验，比较 `feed[v.name].shape` 与 `v.type.shape`，复现本章的静默错误并让它显式失败。
- 用 `np.lib.stride_tricks.as_strided` 构造一个零步长视图，观察它与 `broadcast_strides` 的对应关系。

## 动手练习

1. **手算 stride。** 不运行代码，写出 `(3,4,5)` 的 strides 和 `offset(2,1,3)`，再用 `TensorType` 验证。
2. **验证广播。** 计算 `broadcast_shape((4,1),(1,3))` 和 `broadcast_strides((3,),(2,3))`，解释每个 0 的来源。
3. **观察 view。** 用 `np.shares_memory` 检查 `reshape`、`T`、`copy` 三种情况，记录 strides 和 `C_CONTIGUOUS`。
4. **复现静默错误。** 用 `(2,3)` 的 relu 图和 `orig.T` 调用 `run_c_graph`，确认输出是 `[[1,4,2],[5,3,6]]`。
5. **修正静默错误。** 在调用前加 `assert feed["x"].shape == (2,3)`，或者先 `np.ascontiguousarray(orig)` 再传，观察结果变化。

## 本章重点

- `TensorType` 是 shape + dtype 的不可变描述，shape 每一维必须是正整数。
- 行主序 strides 由 shape 递推，单位是元素；C 后端用 `i*stride` 算地址。
- `broadcast_shape` 从右对齐合并维度，`broadcast_strides` 用 0 表示重复读。
- view 共享内存，copy 分配新内存；C 后端要求连续，`runtime._f32` 负责复制。
- `evaluate_graph` 检查 shape，`run_c_graph` 不检查；转置输入会静默算错。
- dtype 只有 `float32`，`add_const` 会静默转换 `float64`。

## 自我检核

- [ ] 我能手算 `(2,3,4)` 的 strides 和任意坐标的线性下标。
- [ ] 我能解释 `broadcast_strides((3,), (2,3)) == (0,1)`。
- [ ] 我知道 view 和 copy 在 `np.shares_memory` 下的区别。
- [ ] 我能复现转置输入导致 C 后端算错的过程。
- [ ] 我知道 `TensorType` 为什么拒绝零长度维度和负维。
- [ ] 我能说出 `keepdims=True` 对广播的影响。

## 延伸阅读

- **NumPy 文档 “Broadcasting”**：读广播规则和零步长示例；学 shape 合并与 stride 的关系；关联 4.4 和 4.5。
- **NumPy 文档 “Internal memory layout of an ndarray”**：读 strides、C order、view 的说明；学 ndarray 如何用 stride 解释内存；关联 4.3 和 4.7。
- **`code/mini_ai/ir.py`**：读 `TensorType.strides`、`broadcast_shape`、`broadcast_strides`；学静态 shape 的实现；关联本章所有公式。
- **`code/mini_ai/codegen_c.py`**：读 `_linear_index`、`_broadcast_index`、`_emit_elementwise`；学 stride 如何变成 C 下标；关联 4.6。
- **`code/mini_ai/runtime.py`**：读 `_f32` 与 `run_c_graph`；学连续化和 shape 契约的缺口；关联 4.8 和 4.9。

[下一章：参考解释器](./05-reference-interpreter.md)
