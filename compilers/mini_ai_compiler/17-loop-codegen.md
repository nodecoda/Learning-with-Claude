# 第 17 章 循环代码生成：逐元素（elementwise）、广播（broadcast）与归约（reduction）

> **目标**：把计算图里的逐元素（elementwise）运算、广播（broadcast）和归约（reduction）降到带正确索引的 C 循环；读完后你能从 `CCodegen` 的输出反推每个下标是怎么来的，并能判断一次索引生成在哪里会写错。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；numpy 2.4.6；MSVC 19.43（`cl` 14.43）。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行，先设置：

```powershell
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

本章的实现位于 [`code/mini_ai/codegen_c.py`](./code/mini_ai/codegen_c.py)，索引语义来自 [`code/mini_ai/ir.py`](./code/mini_ai/ir.py) 的 `broadcast_strides`。前一章是 [Ch 16 Lowering 到标量 C](./16-lower-to-c.md)，下一章是 [Ch 18 Matmul 与 tiling](./18-matmul-tiling.md)。

## 为什么需要这一层

解释器执行一张图时，每个算子都要做一次 Python 层分发，再交给 NumPy 处理。这个模型适合建立语义金标准，不适合回答“编译后的循环长什么样”。真正把图落到 C 时，编译器必须替每个算子决定四件事：

1. 输出张量有多少维，每一维的循环变量叫什么。
2. 从输入张量的哪个线性下标读数据。
3. 结果写到输出张量的哪个线性下标。
4. 归约类算子的累加器放在哪里，什么时候清零。

这四件事合起来叫循环代码生成（loop code generation）。它不涉及复杂的指令选择，却是 C 后端经常出现“数值看起来差不多、边界上错一位”的地方。广播把某个维度映射成 stride 0，归约把某个维度从输出里消掉；这两个动作只要有一个下标公式写反，结果就会在特定 shape 上错。

本章不讨论 tiling 和向量化，这两件事分别在 Ch 18 和 Ch 19 展开。这里先把“标量循环 + 正确索引”做扎实。

## 先建立直觉

一张图降到 C 以后，结构是固定的：先分配 arena（一次性内存池），再把每个 value 绑定到指针，然后按拓扑顺序逐 op 生成循环，最后把输出拷贝到调用者提供的 buffer。

```text
Graph (static shapes)                 Generated C
---------------------                 -----------
x: tensor<2,3>       --->             const float* v_0_x = x0;
b: tensor<3>         --->             const float* v_1_b = C_1;
add(x, b)            --->             for i0_0 in 0..2:
                                           for i0_1 in 0..3:
                                               v_2[i0_0*3+i0_1] = x[i0_0*3+i0_1] + b[i0_1]
relu(add)            --->             for i1_0 ...: v_3[...] = fmaxf(v_2[...], 0.0f)
sum(relu, axis=1)    --->             zero v_4; for i2_0, i2_1: v_4[i2_0] += v_3[i2_0*3+i2_1]
output               --->             for i in 0..2: out0[i] = v_4[i]
```

三个关键映射：

```text
row-major strides
shape (2, 3)  ->  strides (3, 1)
index (i, j)  ->  i*3 + j

broadcast input shape (3,) to output shape (2, 3)
padded shape   (1, 3)
padded strides (0, 1)      <- 前面补 0，表示这一维不前进
index (i, j)  ->  0*i + 1*j = j

reduction over axis=1
input  (2, 3), index (i, j)
output (2,),   index (i)       <- 丢掉 axis=1
```

广播不是复制数据，而是让某一维的 stride 变成 0。归约不是把输入搬走，而是让多个输入下标写到同一个输出下标。C 代码生成器要做的，就是把这两种“多对一”和“一对多”的关系写成循环。

## 核心内容

### 1. 线性下标：所有循环的底座

`codegen_c.py` 里有两个函数，先把 shape 和 stride 变成 C 表达式：

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


def _broadcast_index(shape: tuple[int, ...], out_shape: tuple[int, ...], vars_: list[str]) -> str:
    return _linear_index(out_shape, broadcast_strides(shape, out_shape), vars_)
```

这段代码有两个值得注意的选择。

第一，stride 为 0 的项被直接跳过。对 `(3,)` 广播到 `(2,3)`，`broadcast_strides` 返回 `(0,1)`，生成的表达式只有 `i0_1`，不会出现 `0*i0_0` 这种噪声。第二，stride 为 1 的项不加乘法，生成 `i0_1` 而不是 `(i0_1)*1`，让 C 编译器少做一次无意义改写。

`TensorType.strides` 的定义在 `ir.py`：

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

以 `(2,3)` 为例，从最后一维往前：先放 1，再乘 3，得到 `(3,1)`。以 `(4,1,3)` 为例，得到 `(3,3,1)`。C 后端始终按这个布局读写，所以输入必须已经是 row-major contiguous；需要转置或换布局时，应该在 IR 层插入显式的布局转换，而不是在代码生成里猜。

### 2. elementwise：输出 shape 决定循环层数

`CCodegen._emit_elementwise` 先生成输出的每一层循环，再在循环体里发一条赋值：

```python
def _emit_elementwise(self, op: Op, lines: list[str]) -> None:
    out_shape = op.output.type.shape
    rank = len(out_shape)
    vars_ = [f"i{op.id}_{d}" for d in range(rank)]
    indent = "    "
    if rank == 0:
        self._emit_expr(op, lines, indent, [], "0")
        return
    for d, n in enumerate(out_shape):
        lines.append(f"{indent}for (int {vars_[d]} = 0; {vars_[d]} < {n}; ++{vars_[d]}) {{")
        indent += "    "
    out_idx = _linear_index(out_shape, TensorType(out_shape).strides, vars_)
    self._emit_expr(op, lines, indent, vars_, out_idx)
    for _ in out_shape:
        indent = indent[:-4]
        lines.append(f"{indent}}}")
```

循环变量带 `op.id` 前缀，例如 `i0_0`、`i0_1`。这样同一张图里多个 op 的循环变量不会重名，生成器不需要做作用域分析。循环层数等于输出 rank，而不是输入 rank；这是广播能工作的前提：输入可以被“拉长”到输出形状，输出不会为了某个输入降维。

`_emit_expr` 负责把每个输入映射到自己的下标，再拼出 C 表达式：

```python
def _emit_expr(self, op: Op, lines: list[str], indent: str, vars_: list[str], out_idx: str) -> None:
    dst = self.ptr(op.output)
    ins = [self.ptr(v) for v in op.inputs]
    idx = [self._expr_input_index(v, op.output.type.shape, vars_) for v in op.inputs]
    k = op.kind
    if k == "relu":
        expr = f"fmaxf({ins[0]}[{idx[0]}], 0.0f)"
    elif k == "exp":
        expr = f"expf({ins[0]}[{idx[0]}])"
    elif k == "add":
        expr = f"{ins[0]}[{idx[0]}] + {ins[1]}[{idx[1]}]"
    elif k == "sub":
        expr = f"{ins[0]}[{idx[0]}] - {ins[1]}[{idx[1]}]"
    elif k == "mul":
        expr = f"{ins[0]}[{idx[0]}] * {ins[1]}[{idx[1]}]"
    elif k == "div":
        expr = f"{ins[0]}[{idx[0]}] / {ins[1]}[{idx[1]}]"
    elif k == "add_relu":
        expr = f"fmaxf({ins[0]}[{idx[0]}] + {ins[1]}[{idx[1]}], 0.0f)"
    else:
        raise IRError(f"not an elementwise op: {k}")
    lines.append(f"{indent}{dst}[{out_idx}] = {expr};")
```

注意 `_expr_input_index` 用的是**输出** shape：

```python
def _expr_input_index(self, v: Value, out_shape: tuple[int, ...], vars_: list[str]) -> str:
    return _broadcast_index(v.type.shape, out_shape, vars_)
```

如果这里误用输入 shape，广播维度就不会被补成 stride 0，生成的循环变量个数也会和输出对不上。这个函数是 elementwise 正确性的核心。

### 3. 正常路径：add + relu + sum 的真实生成结果

用下面的图实际调用 `generate_c`：

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.codegen_c import generate_c

g = Graph()
x = g.add_input("x", (2, 3))
b = g.add_const("b", np.array([1, 2, 3], np.float32))
y = g.add_op("add", [x, b])
r = g.add_op("relu", [y])
s = g.add_op("sum", [r], {"axis": 1})
g.add_output(s)
print(generate_c(g))
```

生成的关键部分如下（只保留 MSVC 分支，省略了文件头、常量数组和 `#else`）：

```c
MINI_EXPORT int mini_forward(const float* x0, float* out0) {
    const size_t arena_bytes = 128;
    float* arena = (float*)_aligned_malloc(arena_bytes, 64);
    if (!arena) return 1;

    const float* v_0_x = x0;
    const float* v_1_b = C_1;
    float* v_2_add_0 = (float*)((char*)arena + 0);
    float* v_3_relu_1 = (float*)((char*)arena + 64);
    float* v_4_sum_2 = (float*)((char*)arena + 0);

    /* add: add_0 */
    for (int i0_0 = 0; i0_0 < 2; ++i0_0) {
        for (int i0_1 = 0; i0_1 < 3; ++i0_1) {
            v_2_add_0[(i0_0)*3 + i0_1] = v_0_x[(i0_0)*3 + i0_1] + v_1_b[i0_1];
        }
    }

    /* relu: relu_1 */
    for (int i1_0 = 0; i1_0 < 2; ++i1_0) {
        for (int i1_1 = 0; i1_1 < 3; ++i1_1) {
            v_3_relu_1[(i1_0)*3 + i1_1] = fmaxf(v_2_add_0[(i1_0)*3 + i1_1], 0.0f);
        }
    }

    /* sum: sum_2 */
    for (int z = 0; z < 2; ++z) v_4_sum_2[z] = 0.0f;
    for (int i2_0 = 0; i2_0 < 2; ++i2_0) {
        for (int i2_1 = 0; i2_1 < 3; ++i2_1) {
            v_4_sum_2[i2_0] += v_3_relu_1[(i2_0)*3 + i2_1];
        }
    }

    for (int i = 0; i < 2; ++i) out0[i] = v_4_sum_2[i];

    _aligned_free(arena);
    return 0;
}
```

逐段看：

- `v_2_add_0` 和 `v_4_sum_2` 都从 arena 偏移 0 开始。这不是越界，而是内存规划器发现 `v_2_add_0` 在 `sum` 开始前已经死亡，允许 `v_4_sum_2` 复用它的 64 字节块。`v_3_relu_1` 仍然活着，所以放在偏移 64。
- add 的 `b` 是 `(3,)` 广播到 `(2,3)`，输入下标只剩 `i0_1`，输出下标是 `i0_0*3 + i0_1`。
- relu 是单输入，输入和输出下标完全相同。
- sum 先把输出清零。arena 来自 `_aligned_malloc`，内容未初始化；不清零就会把垃圾值累加进去。
- sum 的输入下标是二维，输出下标只有 `i2_0`。`i2_1` 被归约掉。
- 最后统一把输出 value 拷到 `out0`，而不是让 `out0` 直接参与计算。这样输出 buffer 的别名和生命周期由 ABI 层控制，C 代码内部只依赖 arena。

### 4. 广播的边界：stride 0 不等于 stride 1

再生成一张 `(4,1)+(1,3)` 的图：

```python
g = Graph()
x = g.add_input("x", (4, 1))
y = g.add_input("y", (1, 3))
z = g.add_op("add", [x, y])
g.add_output(z)
print(generate_c(g))
```

实际循环体是：

```c
for (int i0_0 = 0; i0_0 < 4; ++i0_0) {
    for (int i0_1 = 0; i0_1 < 3; ++i0_1) {
        v_2_add_0[(i0_0)*3 + i0_1] = v_0_x[i0_0] + v_1_y[i0_1];
    }
}
```

`x` 的 shape 是 `(4,1)`，广播到 `(4,3)` 后 strides 是 `(1,0)`，所以 `x` 的下标只保留 `i0_0`；`y` 的 shape 是 `(1,3)`，strides 是 `(0,1)`，下标只保留 `i0_1`。两边都没有复制数据，循环每转一次，`x` 的值在同一行内重复使用，`y` 的值在同一列内重复使用。

如果错误地把 stride 0 改成 1，`x` 会变成 `x[i0_0*3 + i0_1]`，在 `(4,1)` 的输入上越界读。这个错误在小 shape 上可能读到相邻内存，结果看起来“像那么回事”，但差分测试会抓住它。广播的 0 不是“跳过这个维度”这么模糊，它精确表示“这个维度不增加地址”。

### 5. 归约：axis、keepdims 与清零

`_emit_sum` 的完整逻辑：

```python
def _emit_sum(self, op: Op, lines: list[str]) -> None:
    x = op.inputs[0]
    out = op.output
    axis = op.attrs.get("axis", None)
    keepdims = bool(op.attrs.get("keepdims", False))
    shape = x.type.shape
    rank = len(shape)
    vars_ = [f"i{op.id}_{d}" for d in range(rank)]
    lines.append(f"    for (int z = 0; z < {max(1, out.type.numel)}; ++z) {self.ptr(out)}[z] = 0.0f;")
    if axis is None:
        lines.append(f"    for (int z = 0; z < {max(1, x.type.numel)}; ++z) {self.ptr(out)}[0] += {self.ptr(x)}[z];")
        return
    if axis < 0:
        axis += rank
    indent = "    "
    for d, dim in enumerate(shape):
        lines.append(f"{indent}for (int {vars_[d]} = 0; {vars_[d]} < {dim}; ++{vars_[d]}) {{")
        indent += "    "
    in_idx = _linear_index(shape, TensorType(shape).strides, vars_)
    if keepdims:
        out_vars = [v if d != axis else "0" for d, v in enumerate(vars_)]
        out_shape = out.type.shape
    else:
        out_vars = [v for d, v in enumerate(vars_) if d != axis]
        out_shape = out.type.shape
    out_idx = _linear_index(out_shape, TensorType(out_shape).strides, out_vars)
    lines.append(f"{indent}{self.ptr(out)}[{out_idx}] += {self.ptr(x)}[{in_idx}];")
    for _ in shape:
        indent = indent[:-4]
        lines.append(f"{indent}}}")
```

这段代码有几个需要留意的细节。

第一，`axis is None` 走单独的快路径：输出只有一个元素，直接把输入压平相加。输出 shape 是 `()`，`max(1, out.type.numel)` 保证仍然发一次清零。

第二，`keepdims=True` 时，输出仍然保留被归约的维度，但该维长度是 1，所以对应循环变量被替换成常量 `"0"`。`keepdims=False` 时，这个变量从 `out_vars` 里删掉。两种情况的 `out_idx` 都重新按输出 shape 的 strides 计算，不能沿用输入下标。

第三，负数 axis 先加 rank。比如 `axis=-1` 在 rank 2 上变成 1。`infer_output_type` 在 `add_op` 阶段已经检查了 axis 范围；如果绕过它手工构造 `Op`，`_emit_sum` 里的 `axis += rank` 可能仍然越界，随后 `out_vars` 的推导会产生错误结果。验证器应该在建图时就挡住这种情况。

第四，归约循环的嵌套顺序沿用输入 shape 的维度顺序。对 `(2,3)` 按 axis=1 归约，内层是 `i2_1`，输入地址连续，输出地址不变。对 axis=0 归约，输出 shape 是 `(3,)`，输出下标只由 `i2_1` 决定；外层每换一个 `i2_0`，整个输出向量会被再读写一遍。两种顺序都正确，缓存行为不同。

### 6. 失败路径：C 后端没有 softmax

当前 `_emit_op` 对 softmax 直接报错：

```python
if kind == "softmax":
    raise IRError("C backend does not implement softmax yet; use sum/exp/div")
```

实际运行：

```text
IRError: C backend does not implement softmax yet; use sum/exp/div
```

这不是“忘了写一个循环”，而是语义没有对齐。softmax 需要按 axis 求最大值、做指数、求和、再逐元素相除；其中最大值是为了数值稳定。只生成 `exp(x)/sum(exp(x))` 在数值上会溢出，不能算实现了 softmax。正确做法是先用现有算子把 softmax 分解成 `max`（当前 IR 没有）、`sub`、`exp`、`sum`、`div`，或者在 C 后端增加一个带稳定化逻辑的复合 op，并在解释器里保持同样的数值路径。

另一个边界是广播不合法：

```text
IRError: shapes are not broadcastable: ((2, 3), (4,))
```

这个错误在 `add_op` 调用 `infer_output_type` 时就抛出，不会走到代码生成。把错误尽量前移，能让 C 后端只处理已经通过形状检查的图。

## 底层机制

### 静态 shape 换来了什么

`TensorType` 只支持 `float32` 和静态 shape。因此 `CCodegen` 可以把每个循环上界写成整数字面量：

```c
for (int i0_1 = 0; i0_1 < 3; ++i0_1)
```

这带来三个后果：

- C 编译器能看到完整的 trip count，能做展开、向量化和别名分析。Ch 19 会利用这一点。
- 生成的 DLL 不携带 shape 元数据。调用者必须按建图时的 shape 传数组，传错不会在 C 层被检查。
- 动态 shape 需要另一套代码生成策略，例如把维度作为运行时参数传入，或者为每个 shape 生成一个特化版本。本课不展开。

### SSA 与 buffer 复用

IR 是静态单赋值（SSA，Static Single Assignment）形式：每个 `Value` 只由一个 `Op` 定义。C 代码里每个 value 也先绑定一个指针，然后只在该 op 的循环里写一次。归约看起来像“多次写同一个输出”，但它在 IR 里仍然是一个 op 的一次定义，循环内部的 `+=` 是这个 op 的实现细节，不违反 SSA。

内存规划器允许不同 value 复用同一块 arena，条件是前一个 value 的 live interval 在新区间开始前已经结束。`v_2_add_0` 和 `v_4_sum_2` 复用了偏移 0，因为 add 的结果在 sum 开始后不再被读。反过来，如果某个 op 同时读写同一个 buffer，复用就会破坏语义；`plan_memory` 用 `end < start` 的严格条件避免这一点。Ch 12 和 Ch 13 已经讨论过这个边界。

### 循环变量命名与代码可读性

`i{op.id}_{d}` 的命名让每个 op 的循环变量全局唯一。它不参与优化，只服务于两点：生成器不需要维护作用域栈；出错时能从变量名直接定位到 op。代价是名字比 `i`、`j`、`k` 长，C 编译器不在乎，调试时反而有用。

### 归约的数值顺序

`sum` 用 `float` 累加，按输入的内存顺序逐个加。PyTorch 的 CPU 归约可能使用分块或成对求和，浮点加法不满足结合律，所以两者的结果可能有末位差异。差分测试应该用 `atol/rtol`，而不是要求逐位相等。当前测试里 `assert_close` 的默认 `atol=1e-5, rtol=1e-5` 就是为这种差异留的余量。

## 进一步用法与变体

### keepdims 变体

把上面的图改成 `{"axis": 1, "keepdims": True}`，输出 shape 从 `(2,)` 变成 `(2,1)`。生成的 C 里，输出下标会从 `i2_0` 变成 `i2_0 + 0`，循环仍然遍历完整的 `(2,3)` 输入。这个变体适合接 broadcasting：后续 op 想按行广播时，保留长度为 1 的维度比手动 reshape 更直接。

### axis=None 与全图归约

`axis=None` 时，输出是标量，C 代码只发一个 `for z` 循环，把输入压平累加到 `out[0]`。这条路径不检查输入是否为空；空 tensor 的 `numel` 是 0，循环不执行，输出保持清零后的 0。这个语义要和解释器、PyTorch 对齐，不能靠“空输入不会出现”来回避。

### 多输入 elementwise 与融合

`add_relu` 是 `add` 和 `relu` 的融合结果。`_emit_expr` 里它被写成一条 `fmaxf(a+b, 0.0f)`，循环层数仍然由输出 shape 决定。融合减少了中间 buffer 和一次循环，但要求两个 op 的输入输出 shape 完全一致，并且中间值没有其他用户。`fuse_ops` 在 Ch 10 里已经实现了这些合法性检查。

### 用不同编译器验证生成的 C

生成 C 以后，可以用 MSVC 编译成 DLL，也可以用 `clang` 或 `gcc` 做语法和语义交叉检查。跨编译器验证时要注意：`fmaxf`、`expf` 的精度和向量化行为可能不同，比较结果仍然要用容差。Ch 19 会给出 MSVC 的具体命令和汇编写法。

## 对比与取舍

| 方案 | 优点 | 代价 | 适用场景 |
| --- | --- | --- | --- |
| 解释器逐 op 执行 | 实现快，易调试，天然支持动态 shape | Python 分发开销，难做循环级优化 | 语义金标准、调试、随机图测试 |
| 通用 C 循环 + 运行时 shape | 代码量小，支持多 shape | 循环上界是变量，优化空间受限 | 动态 shape 的快速后端 |
| 静态 shape 特化 C | 编译期常量，易展开/向量化 | 每个 shape 生成一份代码 | 本课的固定 shape 模型 |
| 广播 stride 0 | 不复制数据，索引直接 | 内层循环可能反复读同一地址 | 大多数 elementwise |
| 广播前物化 | 内层循环连续，易向量化 | 额外内存和一次拷贝 | 广播维度很大且被多次使用 |

本课选择“静态 shape 特化 + stride 0 广播”。它让生成器保持小，也让读者能直接看到下标公式。真正做产品时，是否物化广播要看广播维度的复用次数和内存带宽，不能只凭代码短就下结论。

## 踩雷集锦

1. **把广播 stride 0 写成 1。** `(4,1)` 广播到 `(4,3)` 时，`x` 的下标应该是 `i0_0`，不是 `i0_0*3+i0_1`。后者会越界读，且在小 shape 上不一定立刻崩溃。
2. **归约前忘记清零。** arena 是 `_aligned_malloc` 出来的未初始化内存。`sum` 的第一条语句必须是清零，否则结果会随上一次调用残留的数据变化。
3. **在同一个 op 内复用输入和输出 buffer。** 例如把 `relu` 的输出直接写到输入指针上。如果输入还有别的用户，后续 op 会读到被覆盖的值；内存规划器的 `end < start` 条件就是为这个边界设的。
4. **负数 axis 不归一化。** `axis=-1` 必须先加 rank，再参与输出下标推导；直接把 `-1` 当维度索引会静默取到错误维度。
5. **只比较最终输出，不比较中间值。** 归约或广播的索引错误可能被后续 op 掩盖。调试时先单独验证每个 op 的 C 输出，再做整图对比。

## 进阶

- 给 `sum` 增加 `max` 归约，并在解释器里实现同样的数值路径；用随机 shape 做差分测试。
- 把 softmax 分解成 `max`、`sub`、`exp`、`sum`、`div`，比较“先减最大值”和“直接 exp”在 `x=100` 附近的差异。
- 为 elementwise 循环加 `#pragma omp simd` 或 `restrict`，观察 Ch 19 的向量化报告如何变化。
- 让 C 后端支持 rank 0 的标量输出，并用 `add` 两个常量构造一个不被常量折叠的图。
- 统计生成 C 的循环嵌套深度和代码行数，和解释器的 op 数做对比，理解“特化”带来的代码膨胀。

## 动手练习

1. 用本章的 `generate_c` 生成 `(4,1)+(1,3)` 的 C，编译成 DLL，用 `run_c_graph` 与 NumPy 结果对比。把 `x` 的 stride 手动改成 1，观察错误。
2. 把 `sum` 的 `keepdims` 改成 `True`，重新生成 C，确认输出 shape 和下标都变了。
3. 构造一个 `(2,3,4)` 按 `axis=1` 归约的图，手算一个元素的下标，再和生成的 C 对照。
4. 给 `sum` 增加 `axis` 越界的显式检查，并写一个 `unittest` 断言 `IRError`。
5. 用 `cl /Wall` 编译生成的 C，记录所有警告；判断哪些警告是生成器可以避免的。

## 本章重点

- elementwise 的循环层数由输出 shape 决定，输入通过 `broadcast_strides` 映射到输出下标。
- stride 0 表示广播维度不前进；跳过 stride 0 的项是正确化简，不是省略。
- 归约先清零，再遍历输入，把多个输入下标映射到同一个输出下标；`keepdims` 只改变输出下标，不改变输入遍历。
- 静态 shape 让循环上界成为常量，代价是 DLL 不携带 shape 检查。
- SSA 保证每个 value 只定义一次；buffer 复用必须满足严格的 live interval 条件。

## 自我检核

- [ ] 我能写出 `(2,3)` 的 row-major strides，并解释 `i*3+j` 的来源。
- [ ] 我能说明 `(3,)` 广播到 `(2,3)` 时，为什么输入下标只剩 `j`。
- [ ] 我能指出 `_emit_elementwise` 里哪一行决定了循环层数。
- [ ] 我能解释归约为什么必须先清零，以及 `keepdims` 改变了什么。
- [ ] 我能说出 softmax 当前失败的原因，并给出一个稳定的分解方案。
- [ ] 我能从生成的 C 里找到一个 buffer 复用，并说明它为什么安全。

## 延伸阅读

- **`code/mini_ai/codegen_c.py` 的 `_linear_index`、`_broadcast_index`、`_emit_elementwise`、`_emit_sum`**：读哪里——直接读函数体和生成结果；学什么——shape/stride 如何变成 C 表达式；关联——本章所有例子的来源。
- **`code/mini_ai/ir.py` 的 `TensorType.strides` 与 `broadcast_strides`**：读哪里——两个函数的完整实现；学什么——row-major 布局和 stride 0 的定义；关联——解释器、C 后端、LLVM 后端共用这套语义。
- **`code/mini_ai/memory.py` 的 `plan_memory`**：读哪里——live interval 和 `end < start` 的复用条件；学什么——为什么归约输出可以复用已死亡输入的 buffer；关联——本章生成的 arena 偏移。
- **NumPy 文档 “Broadcasting”**：读哪里——broadcasting rules 和 stride 的解释；学什么——形状对齐和长度为 1 的维度如何扩展；关联——`broadcast_shape` 与 `broadcast_strides` 的语义基准。
- **《Engineering a Compiler》循环优化章节**：读哪里——loop nesting、loop-invariant code motion、unrolling；学什么——循环结构如何影响优化；关联——Ch 18 的 tiling 和 Ch 19 的向量化。

下一章：[Ch 18 Matmul 与 tiling](./18-matmul-tiling.md)