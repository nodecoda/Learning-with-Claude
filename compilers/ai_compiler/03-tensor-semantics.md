# Ch 3 张量语义：shape、stride、broadcast 与 layout

> **目标**：把「张量」从「一个多维数组」还原成「一块线性内存 + shape + stride + offset + dtype」，用真实实验看清 `view`/`reshape`/`permute`/`contiguous` 的差别、broadcast 的 stride=0 语义，以及这些语义如何决定编译器的布局选择与 copy 插入。

## 为什么需要这个？

图 IR 里一个节点只写 `matmul`，但真正生成 kernel 时，编译器必须知道：这个 tensor 在内存里是怎么排的？它的 stride 是多少？读一个元素要跳几步？

如果忽略这些，就会出现两类典型 bug：

- **语义 bug**：把 `permute` 当成「换个 shape」，结果 `view` 报错，或者读出来的数完全错位。
- **性能 bug**：以为 `transpose` 一定会慢，于是无脑插 `contiguous()`；结果 copy 的成本比布局惩罚还高。

这一章的所有结论都来自本机实测（torch 2.14.1+cpu / numpy 2.4.6）。每个实验都能复制运行；失败示例保留报错原文。

## 先建立直觉：张量不是「多维数组」

一个 float32 的 `(2, 3, 4)` 张量，在内存里是一段连续的 24 个 float。shape 只是「怎么解释这段内存」，真正决定寻址的是 stride：

```text
offset(i, j, k) = base + i * stride[0] + j * stride[1] + k * stride[2]
```

对于标准的行优先（C order）张量：

```text
shape  = (2, 3, 4)
stride = (12, 4, 1)
```

`stride[0]=12` 表示「i 每加 1，指针跳 12 个元素」；`stride[2]=1` 表示最内层连续。`permute` 不搬数据，只改 stride；`contiguous` 才真的按新顺序复制一份。

**记住这个公式，后面所有现象都能推出来。**

## 核心概念一：shape、dtype、layout、stride

四个词经常被混用，先把定义钉死：

| 词 | 含义 | 例子 |
|---|---|---|
| shape | 每个维度的大小 | `(2, 3, 4)` |
| dtype | 每个元素的类型和宽度 | `float32`（4 字节） |
| stride | 每个维度前进一步跨多少个元素 | `(12, 4, 1)` |
| layout / memory format | 维度的内存排列约定 | 行优先、`channels_last` |

注意：PyTorch 的 `tensor.layout` 属性（`torch.strided`/`torch.sparse_coo`）是另一层概念；本章说的 layout 主要是 **memory format**，也就是「哪个维度在最内层」。

两个张量可以 shape 相同、dtype 相同，但 stride 不同——它们的内存布局不同，kernel 的访存模式也不同。编译器做 layout 选择，选的就是这个 stride 顺序。

## 核心概念二：view、reshape、permute、contiguous

这四个操作的区别，用一句话概括：

- `permute`：只改 stride，返回 view，不复制。
- `view`：要求新 shape 能用**现有 stride** 表示，否则报错；成功时返回 view。
- `reshape`：先尝试 view，不行就复制一份再 view；**可能复制，也可能不复制**。
- `contiguous`：按当前逻辑顺序复制出一份行优先张量；已经是连续的就原样返回。

关键点：`view` 是「零成本但有前提」，`reshape` 是「保证成功但可能付费」。很多教程说「reshape 会复制」，这不准确；**连续张量的 reshape 不会复制**（下面有实测）。

## 核心概念三：broadcast 是 stride=0

broadcast 不是「把数据复制成更大的张量」，而是「让某个维度的 stride 等于 0」：

```text
a.shape  = (1, 3)
a.stride = (3, 1)
b = a.expand(4, 3)
b.stride = (0, 1)
```

`stride[0]=0` 意味着「i 怎么变，地址都不动」——四行共享同一份数据。这样既省内存，又让 kernel 可以只读一次、广播使用。

但 stride=0 带来两个后果：

1. **写操作有歧义**：`b[0,0] = 5` 会改到所有行共享的那个元素。
2. **不能随便 in-place**：`b.add_(1)` 会让同一个地址被写多次，PyTorch 直接拒绝。

编译器处理 broadcast 时，要么在 kernel 里用「读同一个地址」实现，要么显式 materialize 一份。选哪个，取决于后续算子是否需要写、是否需要连续布局。

## 核心概念四：memory format 与 dtype promotion

### memory format

`channels_last` 是卷积网络里最常见的非行优先布局。它把通道维放到最内层：

```text
NCHW stride = (C*H*W, H*W, W, 1)
NHWC stride = (H*W*C, 1, W*C, C)
```

shape 仍然是 `(N, C, H, W)`，但 stride 变了。很多 CPU/GPU 卷积库对 NHWC 更友好；编译器可以选择「整张图用 NHWC」或「在卷积前后插转换」。

### dtype promotion

PyTorch 的 type promotion 规则决定 `int64 + float32` 的结果是 `float32`，`int64 + Python int` 的结果还是 `int64`。这对编译器很重要：

- 图里一个 `add` 的两个输入 dtype 不同，后端要么插入 cast，要么拒绝。
- TVM 的 TIR 通常要求显式 dtype；不会帮你隐式提升。
- 量化图里，`int8 + int32` 的累加类型必须显式指定，否则溢出。

## 底层机制：编译器怎么处理 layout

图编译器看到 `permute` 时，有三条路：

1. **索引重映射（index remap）**：不搬数据，生成 kernel 时把 `i/j/k` 的地址计算换成新的 stride。零拷贝，但访存可能不连续。
2. **插入 copy**：先 `contiguous()` 成标准布局，再调用只支持标准布局的 kernel。多一次读写，但后续 kernel 可能更快。
3. **布局传播**：把「这个张量是 NHWC」的属性沿图传下去，让相邻算子都接受 NHWC，避免来回转换。

选择哪条路，取决于代价模型：

```text
cost(copy) ≈ bytes_moved / effective_bandwidth
cost(remap) ≈ 原 kernel 时间 × 非连续访问惩罚
```

如果 `copy` 比「非连续访问惩罚」便宜，就插 copy；否则保留 stride。**没有全局最优，只有对当前 kernel 和当前硬件的比较。**

## 示例一：stride 与 `permute` 后的 `view` 失败

```python
import torch

x = torch.arange(24, dtype=torch.float32).reshape(2, 3, 4)
print("x:", tuple(x.shape), x.stride(), x.is_contiguous())

y = x.permute(0, 2, 1)
print("y:", tuple(y.shape), y.stride(), y.is_contiguous())

try:
    y.view(2, 12)
except Exception as e:
    print(type(e).__name__ + ":", e)
```

实测输出：

```text
x: (2, 3, 4) (12, 4, 1) True
y: (2, 4, 3) (12, 1, 4) False
RuntimeError: view size is not compatible with input tensor's size and stride (at least one dimension spans across two contiguous subspaces). Use .reshape(...) instead.
```

为什么失败？`y` 的逻辑顺序是「先 i，再 k，再 j」，但内存里 j 的 stride 是 4、k 的 stride 是 1。要把 `(2,4,3)` 看成 `(2,12)`，需要「一行 12 个元素在内存里连续」，而 `y` 的一行是 `x[0, :, 0]`、`x[0, :, 1]`… 这些元素在内存里跳着放，无法用单一 stride 表示。

`view` 的检查条件可以粗略理解为：**新 shape 的每个维度必须对应一段连续的、可合并的 stride**。`permute` 之后通常不满足。

## 示例二：`reshape` 复制，`view` 别名

接着上面的 `x`/`y`：

```python
z = y.reshape(2, 12)
print("z:", tuple(z.shape), z.stride(), z.is_contiguous())
print("z shares storage with x?", z.untyped_storage().data_ptr() == x.untyped_storage().data_ptr())
print("y shares storage with x?", y.untyped_storage().data_ptr() == x.untyped_storage().data_ptr())
print("z[0, :4] =", z[0, :4].tolist())
print("x[0, 0, :] =", x[0, 0, :].tolist())
```

实测输出：

```text
z: (2, 12) (12, 1) True
z shares storage with x? False
y shares storage with x? True
z[0, :4] = [0.0, 4.0, 8.0, 1.0]
x[0, 0, :] = [0.0, 1.0, 2.0, 3.0]
```

三件事：

1. `y` 是 view，和 `x` 共享 storage。
2. `z = y.reshape(...)` 复制了一份新的连续内存（storage 指针不同）。
3. `z` 的值是 `[0, 4, 8, 1, ...]`——这是按 `y` 的逻辑顺序（i, k, j）遍历的结果，不是 `x` 的原始顺序。

再对比连续张量的 `reshape`：

```python
x2 = torch.arange(6, dtype=torch.float32).reshape(2, 3)
r = x2.reshape(3, 2)
print("contiguous reshape shares:", r.untyped_storage().data_ptr() == x2.untyped_storage().data_ptr())
```

实测输出：

```text
contiguous reshape shares: True
```

所以正确说法是：**`reshape` 在能 view 时零拷贝，不能 view 时才复制**。判断依据是 stride，不是函数名。

`view` 成功时是真正的别名：

```python
xv = x.view(24)
xv[0] = 99
print(x[0, 0, 0].item())
```

实测输出：

```text
99.0
```

改 `xv` 就是改 `x`。编译器如果假设「view 产生独立 buffer」，就会做错优化（例如把写操作重排到读之前）。

## 示例三：broadcast 的 stride=0 与写语义

```python
a = torch.arange(3, dtype=torch.float32).reshape(1, 3)
b = a.expand(4, 3)
print("a:", tuple(a.shape), a.stride())
print("b:", tuple(b.shape), b.stride(), b.is_contiguous())

b[0, 0] = 5.0
print("a after write:", a)
print("b:", b)
```

实测输出：

```text
a: (1, 3) (3, 1)
b: (4, 3) (0, 1) False
a after write: tensor([[5., 1., 2.]])
b: tensor([[5., 1., 2.],
        [5., 1., 2.],
        [5., 1., 2.],
        [5., 1., 2.]])
```

只写了一行的一个元素，四行都变了——因为它们共享同一块内存。这不是 bug，是 stride=0 的直接后果。

in-place 算术会被拒绝：

```python
try:
    b.add_(1)
except Exception as e:
    print(type(e).__name__ + ":", e)
```

实测输出：

```text
RuntimeError: unsupported operation: more than one element of the written-to tensor refers to a single memory location. Please clone() the tensor before performing the operation.
```

NumPy 的 broadcast 也是同一套语义：

```python
import numpy as np
na = np.arange(3, dtype=np.float32).reshape(1, 3)
nb = np.broadcast_to(na, (4, 3))
print(nb.shape, nb.strides, nb.flags.writeable)
```

实测输出：

```text
(4, 3) (0, 4) False
```

NumPy 直接把结果标成不可写；PyTorch 允许单元素写但会别名扩散。**编译器不能假设「broadcast 后的张量可以随便写」。**

## 示例四：TVM 边界会把 stride 抹平

TVM 的 `tvm.runtime.tensor` 是「把 numpy 数据复制进 TVM tensor」的入口。源码里写得很清楚：

```text
return empty(arr.shape, arr.dtype, device, mem_scope).copyfrom(arr)
```

源码依据：`tvm/runtime/_tensor.py:336` 定义 `tensor()`，`:359` 是上面的 `copyfrom`（tvm 0.27.0.post1）。

实测非连续输入：

```python
import numpy as np, tvm

a = np.arange(12, dtype="float32").reshape(3, 4)
b = a[:, ::2]               # 非连续，stride=(16, 8)
print("b contig", b.flags.c_contiguous, "strides", b.strides)
t = tvm.runtime.tensor(b, tvm.cpu())
print("tensor ok", t.numpy(), t.strides)
```

实测输出：

```text
b contig False strides (16, 8)
tensor ok [[ 0.  2.]
 [ 4.  6.]
 [ 8. 10.]] (2, 1)
```

TVM 不保留 `(16, 8)` 的 stride，而是复制成紧凑的 `(2, 1)`。broadcast 数组也一样会被 materialize：

```python
nb = np.broadcast_to(np.arange(3, dtype="float32"), (4, 3))
print("nb strides", nb.strides, "writeable", nb.flags.writeable)
t2 = tvm.runtime.tensor(nb, tvm.cpu())
print("broadcast ok", t2.numpy(), t2.strides)
```

实测输出：

```text
nb strides (0, 4) writeable False
broadcast ok [[0. 1. 2.]
 [0. 1. 2.]
 [0. 1. 2.]
 [0. 1. 2.]] (3, 1)
```

这解释了为什么「runtime 边界」既是安全网也是性能陷阱：它保证 kernel 拿到紧凑 buffer，但**每次跨边界都可能多一次拷贝**。编译器如果能在图内保持 stride，就不要在边界上反复 materialize。

## 示例五：layout 对 matmul 的影响（不要想当然）

构造 `A @ B`、`A @ B.t()`、以及「先 contiguous 再 matmul」三种情况，交错测量 30 次，取中位数（i7-10510U，4 线程，torch 2.14.1+cpu）：

```text
A@B contiguous: med 5.93 ms  p25 5.06  p75 8.18
A@Bt transposed: med 6.05 ms  p25 5.31  p75 7.17
A@Bc copy-then-matmul: med 5.39 ms  p25 4.92  p75 7.17
copy med 1.26 ms
```

（`n=512`，float32，30 次交错测量；笔记本负载波动大，绝对数字只做同机比较。）

结论有点反直觉：

- `transposed` 版本并没有明显变慢。BLAS 对 `A @ B.T` 有专门的转置路径，不会傻乎乎地按列跳着读。
- 「先 copy 再 matmul」也没有显著更快；copy 本身要 1.26 ms，省下的时间不足以抵消。
- 三个数字的 p25–p75 有重叠，**别把 5.39 vs 5.93 当成稳定结论**。

所以「非连续一定要 contiguous」是错的。正确做法是：**看后端 kernel 是否支持该 stride；用实测比较 copy 成本和布局惩罚；不要凭直觉插 copy。**

## 对比与取舍

| 操作 | 是否复制 | 结果 stride | 适用场景 |
|---|---|---|---|
| `permute` | 否 | 重排后的 stride | 只想换逻辑维度，后续 kernel 支持任意 stride |
| `view` | 否（否则报错） | 原 stride 的合并 | 连续张量、 reshape 前后元素顺序一致 |
| `reshape` | 可能 | 连续或原 stride | 通用场景；不确定时用它 |
| `contiguous` | 是（若非连续） | 行优先 | 后端只接受连续输入，且 copy 成本可接受 |
| `expand` / `broadcast_to` | 否 | 含 0 | 广播读；不要写 |
| `to(memory_format=...)` | 是（若格式不同） | NHWC/NCHW | 卷积等对 layout 敏感的算子 |

## 踩雷集锦

1. **`permute` 后直接 `view`**：报 `view size is not compatible...`；用 `reshape` 或先 `contiguous`。
2. **以为 `reshape` 一定复制**：连续张量的 `reshape` 返回 view；判断依据是 stride，不是函数名。
3. **对 `expand` 的结果做 in-place**：`add_` 会报「more than one element ... single memory location」；单元素写会别名扩散。
4. **把 broadcast 当成 materialize**：stride=0 是 view；如果 kernel 需要写，必须先 clone/materialize。
5. **只看 shape 不看 stride**：两个 `(N,C,H,W)` 张量可能一个是 NCHW、一个是 NHWC；kernel 性能完全不同。
6. **无脑 `contiguous()`**：copy 有成本；本章实测 transposed matmul 并没有明显更慢。
7. **在 TVM 边界传非连续数组**：`tvm.runtime.tensor` 会静默复制成紧凑 buffer，stride 信息丢失；需要 stride 的 kernel 必须在图内处理。
8. **忽略 dtype promotion**：`int64 + float32` 在 PyTorch 里变 `float32`，但 TVM 不会自动提升；跨界要显式 cast。

## 进阶：再往深一层

- 用 `torch.Tensor.untyped_storage().data_ptr()` 和 `_base` 追踪 view 链，画出一张「谁和谁共享内存」的图。
- 用 `torch.as_strided` 手工构造一个 stride=0 或重叠 stride 的张量，观察 conv/matmul 是否还正确（这类张量在编译器测试里叫「非标准布局」）。
- 在 TVM 里用 `te.placeholder(..., strides=...)` 声明带 stride 的 buffer，看 TIR 的 `Buffer` 是否保留 stride；再对比 `tvm.runtime.tensor` 的紧凑化行为。
- 读 PyTorch 的 `channels_last` 文档，理解 `is_contiguous(memory_format=...)` 为什么对同一个张量可能同时为 True/False。

## 动手练习

### 练习 1：手算 stride

给 `x = torch.arange(60).reshape(3, 4, 5)`，写出 `x`、`x.permute(2, 0, 1)`、`x.transpose(1, 2)` 的 shape 和 stride，并判断哪些能直接 `view` 成 `(3, 20)`。

<details>
<summary>参考思路</summary>

`x`: shape `(3,4,5)`, stride `(20,5,1)`。`permute(2,0,1)`: `(5,3,4)`, stride `(1,20,5)`。`transpose(1,2)`: `(3,5,4)`, stride `(20,1,5)`。只有 `x` 能直接 view 成 `(3,20)`；另外两个都跨了非连续维度，`view` 会报错，`reshape` 会复制。
</details>

### 练习 2：验证 `reshape` 的复制时机

构造一个连续张量和一个 `permute` 后的张量，分别 `reshape`，用 `data_ptr()` 判断是否复制，并解释结果。

<details>
<summary>参考思路</summary>

连续张量 reshape 后 storage 指针相同（view）；permute 后 reshape 指针不同（copy）。原因是 reshape 先尝试 `view`，失败才复制。
</details>

### 练习 3：broadcast 写语义

构造 `a = torch.arange(3).reshape(1,3)`，`b = a.expand(4,3)`。分别执行 `b[0,0]=5` 和 `b.add_(1)`，记录结果/报错，并解释为什么不同。

<details>
<summary>参考思路</summary>

单元素写会落到共享地址，四行同时变；`add_` 需要写所有元素，PyTorch 检测到同一地址会被写多次，直接报错。两者都说明 broadcast 是 view，不是独立 buffer。
</details>

### 练习 4：给编译器写一条 copy 规则

假设后端 kernel 只接受连续输入，输入是一个 `permute` 后的张量。写出你的决策规则：什么时候插 `contiguous()`，什么时候保留 stride？用本章的 matmul 数字举例。

<details>
<summary>参考思路</summary>

先估 `copy` 成本（bytes / 有效带宽），再估非连续访问的惩罚（用同一 kernel 在连续/非连续输入上的实测时间差）。如果 `copy + 连续 kernel` < `非连续 kernel`，就插 copy；否则保留。本章 `n=512` 的例子中，copy 1.26 ms，而 transposed 与 contiguous 的 matmul 差异在噪声内，所以不值得插 copy。
</details>

## 本章重点整理

- 张量 = 线性内存 + shape + stride + offset + dtype；寻址公式是 `base + Σ index[d] * stride[d]`。
- `permute`/`view`/`expand` 是 view，`reshape` 可能复制，`contiguous` 一定按行优先复制（除非已连续）。
- broadcast 的本质是 stride=0；它省内存，但写操作有别名语义，in-place 可能被拒绝。
- NCHW/NHWC 是 memory format，shape 相同、stride 不同；编译器需要 layout 选择和传播。
- 编译器处理 layout 有三条路：索引重映射、插 copy、布局传播；选择取决于代价模型。
- `tvm.runtime.tensor` 会把非连续输入复制成紧凑 buffer，stride 在 runtime 边界被抹平。
- 「非连续一定慢」是错的；本章实测 transposed matmul 没有明显惩罚，copy 不一定划算。

## 自我检核

- [ ] 我能用 stride 公式手算任意 `permute` 后的元素地址
- [ ] 我能解释为什么 `permute` 后 `view` 会报错，而 `reshape` 不会
- [ ] 我知道 `reshape` 什么时候复制、什么时候不复制
- [ ] 我能解释 broadcast 的 stride=0，以及为什么 `add_` 会报错
- [ ] 我能说出 NCHW 与 NHWC 的 stride 差异
- [ ] 我能列出编译器处理 layout 的三种策略
- [ ] 我知道 `tvm.runtime.tensor` 会复制并紧凑化
- [ ] 我不会再无条件地给所有非连续张量插 `contiguous()`

## 延伸阅读

- **PyTorch Tensor Views 文档**（https://pytorch.org/docs/stable/tensor_view.html）：读 view/reshape/permute 的官方定义。与本章关联：语义基线。
- **PyTorch Memory Format 文档**（https://pytorch.org/docs/stable/tensor_attributes.html#torch-memory-format）：读 `channels_last` 的 stride 约定。与本章关联：layout 选择。
- **NumPy Broadcasting**（https://numpy.org/doc/stable/user/basics.broadcasting.html）：读 stride=0 的图示。与本章关联：broadcast 语义。
- **TVM `runtime.tensor` 源码**（`tvm/runtime/_tensor.py:336`）：看 `copyfrom` 如何把任意 numpy 数组变成紧凑 buffer。与本章关联：runtime 边界。
- **Roofline 论文/教程**（https://dl.acm.org/doi/10.1145/1498765.1498785）：理解为什么访存模式比算术更重要。与本章关联：Ch 4 的代价模型。

---

→ [Ch 4 算子与融合边界：代价模型](./04-operators-and-fusion.md)