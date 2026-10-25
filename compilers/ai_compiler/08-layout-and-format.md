# Ch 8 布局与格式：NCHW、NHWC 与 channels_last

> **目标**：把「layout」从一个字母顺序问题，变成一份可验证的契约：谁按什么顺序读内存、谁为此付出拷贝、编译器为什么要把 `permute` 推到图的边界。

## 为什么需要这个？

同一个卷积、同一组权重、同一台机器，只把输入和权重换成 `channels_last`，本机实测约 **1.66–1.68×**（同一段代码两轮，绝对值随 CPU 状态漂移）；换一个三层网络，排序又会翻转。布局不是「写法风格」，它直接决定算子库选哪个 kernel、kernel 怎么读 cache、一次搬运多少字节。

更麻烦的是，布局错误往往不报错：

- `permute` 只改 stride，不搬数据；后面一个 `view` 才炸。
- `reshape` 发现 stride 不兼容，会**悄悄复制**一份，内存和带宽都翻倍。
- 编译器在算子之间插入 `contiguous()`，数值完全正确，性能却掉一半。

这一章要建立三个判断：这个 layout 是谁的契约、转换发生在哪里、这次转换值不值。

## 先建立直觉：内存是一维的，shape 只是视图

一个 tensor 由两块信息描述：

```text
storage: [x0, x1, x2, x3, x4, x5, ...]      # 一维字节数组
shape:   (2, 3, 4)                          # 逻辑坐标
stride:  (12, 4, 1)                         # 每个坐标走一步，跳过几个元素
```

取 `t[n, c, h]` 时，真实下标是：

```text
offset = n * stride[0] + c * stride[1] + h * stride[2]
```

**shape 决定「看到什么」，stride 决定「怎么走内存」**。同一个 storage，可以有多套完全合法的 `(shape, stride)`。所谓 NCHW / NHWC，说的就是 stride 的排列，而不是 storage 里字节的顺序。

本机实测一个最小例子（`torch 2.14.1+cpu`）：

```python
import torch
a = torch.arange(24).reshape(2, 3, 4)
p = a.permute(0, 2, 1)
print(a.shape, a.stride(), a.is_contiguous())
print(p.shape, p.stride(), p.is_contiguous())
```

```text
torch.Size([2, 3, 4]) (12, 4, 1) True
torch.Size([2, 4, 3]) (12, 1, 4) False
```

`p` 的 shape 从 `(2,3,4)` 变成 `(2,4,3)`，storage 一个字节没动；`is_contiguous()` 变成 `False`，因为按行优先走一遍 `(2,4,3)` 时，内存不再连续。

## 核心概念一：NCHW 与 NHWC 是两套 stride 契约

以 `(N, C, H, W) = (2, 3, 4, 5)` 为例：

| 格式 | 逻辑 shape | stride | 谁喜欢它 |
|---|---|---|---|
| NCHW | `(2, 3, 4, 5)` | `(60, 20, 5, 1)` | 传统 CPU 卷积、部分 oneDNN kernel |
| NHWC | `(2, 4, 5, 3)` | `(60, 15, 3, 1)` | GPU Tensor Core、移动端、部分 CPU kernel |

注意 NHWC 的**逻辑 shape 也变了**：通道跑到最后。这一点和 PyTorch 的 `channels_last` 不同，后者只改 stride，不改逻辑 shape（下一节展开）。

为什么卷积在乎这个？一次 `3×3` 卷积要在 `C` 维上做乘加。NHWC 下相邻通道在内存里相邻，一条 SIMD 指令可以一次取多个通道；NCHW 下相邻通道隔着 `H*W` 个元素，只能跨大步长 gather。反过来，如果 kernel 按「每个通道一张 feature map」组织，NCHW 的 `H×W` 连续块又更友好。

**没有绝对更快的格式，只有和 kernel 匹配的格式。**

## 核心概念二：channels_last 不是 NHWC 的别名

PyTorch 的 `torch.channels_last` 是一个 **memory format**，不是新的 tensor 类型。它保持逻辑 shape 仍是 `(N, C, H, W)`，只把 stride 改成「通道最内层」：

```python
x = torch.randn(16, 3, 224, 224)
xcl = x.to(memory_format=torch.channels_last)
print(x.stride())
print(xcl.stride())
print(xcl.shape, xcl.is_contiguous())
print(xcl.is_contiguous(memory_format=torch.channels_last))
```

实测输出：

```text
(150528, 50176, 224, 1)
(150528, 1, 672, 3)
torch.Size([16, 3, 224, 224]) False
True
```

两个判断同时成立：`is_contiguous()` 是 `False`，`is_contiguous(channels_last)` 是 `True`。所以「非连续」不等于「坏布局」，它可能只是**另一种连续**。

卷积权重也一样。`Conv2d.weight` 的逻辑 shape 是 `(O, I, kH, kW)`，转成 channels_last 后 stride 从 `(27, 9, 3, 1)` 变成 `(27, 1, 9, 3)`：

```python
conv = torch.nn.Conv2d(3, 32, 3, padding=1).eval()
print(conv.weight.stride())
print(conv.weight.to(memory_format=torch.channels_last).stride())
```

```text
(27, 9, 3, 1)
(27, 1, 9, 3)
```

只转输入不转权重，算子库通常要现场重排权重，或者退回较慢的路径；只转权重不转输入，同理。**布局是输入、权重、输出三方共同签订的契约**，少签一方就要付转换费。

## 核心概念三：view 要 stride 兼容，reshape 会替你复制

`view` 的语义是「换一个 shape 看同一块 storage」，因此要求新 shape 能由原 stride 表达。`permute` 之后往往不满足：

```python
p = torch.arange(24).reshape(2, 3, 4).permute(0, 2, 1)
try:
    p.view(-1)
except RuntimeError as e:
    print(type(e).__name__, str(e).splitlines()[0])
```

实测报错原文：

```text
RuntimeError view size is not compatible with input tensor's size and stride (at least one dimension spans across two contiguous subspaces). Use .reshape(...) instead.
```

`reshape` 不会报错，因为它允许复制：

```python
r = p.reshape(-1)
print(r.shape, r.data_ptr() == p.data_ptr())
```

```text
torch.Size([24]) False
```

`p.data_ptr()` 和原 `a.data_ptr()` 相同（`permute` 是零拷贝），但 `r` 的指针不同——`reshape` 在这里分配了新内存并重排。**stride 不兼容时，一次 reshape 就是一次 O(n) 的拷贝**，编译器如果把它放在热循环里，代价会非常真实。

实测拷贝成本（i7-10510U，4 线程；`64×128×56×56` float32，约 102.76 MB；warmup 2、5 iter × 3 组取中位）：

| 操作 | 中位时间 | 说明 |
|---|---:|---|
| `permute(0,2,3,1).contiguous()` | 40.05 ms | 读 102.8 MB + 写 102.8 MB |
| `clone()` | 34.49 ms | 纯拷贝，不换 layout |

两者量级接近，说明 `permute+contiguous` 的瓶颈就是内存带宽，不是索引计算。按 205.5 MB / 40.05 ms 估算，有效带宽约 **5.1 GB/s**（按中位数估算，受笔记本负载影响）。

## 核心概念四：layout 是契约，不是建议

把三方约束拆开看：

1. **算子库**：oneDNN / cuDNN 对每种 layout 注册了不同 kernel；不支持的 layout 要么 fallback，要么内部重排。
2. **kernel**：向量化宽度、cache line 利用率、warp 内线程访问是否合并，都依赖 stride。
3. **内存带宽**：转换一次 layout 就是一次完整读写；在带宽受限的层，这笔钱可能比计算还贵。

所以编译器不能「每个算子选自己最舒服的 layout」。它要做一个全局选择：**主干用一种 layout，只在图边界转换**。这就是 layout propagation。

## 底层机制：layout propagation 与 permute 下沉

一张图里，`permute` 有两种命运：

```text
输入(NHWC) → permute → conv → relu → conv → permute → 输出(NHWC)
```

- **坏情况**：每个算子都要求自己的 layout，编译器在每两个算子之间插入 `contiguous()`。转换次数 = 边数。
- **好情况**：识别出 `conv → relu → conv` 可以全部在 channels_last 下执行，只在输入和输出各转一次。转换次数 = 边界数。

判断能不能「推」的条件很硬：

- 这个算子是否支持目标 layout（查算子库能力表，不能猜）。
- 转换是否改变数值语义（layout 转换本身不改值，但 `view` 类算子依赖 stride，不能随意改）。
- 融合后的 kernel 是否仍成立（跨 layout 的融合通常要放弃）。

一个实用的编译器策略是：

1. 给每个 value 标一个 `layout` 属性（NCHW / NHWC / blocked）。
2. 从「锚点算子」（conv、matmul）出发，向后传播它偏好的 layout。
3. 遇到冲突时，在**分叉最少、张量最小**的位置插入转换。
4. 把转换尽量推到 placeholder / output，因为那里通常只发生一次。

把上面的策略写成伪代码，结构会更清楚（这是策略描述，不是 PyTorch API）：

```text
for value in graph.values:
    value.layout = UNKNOWN

for anchor in graph.nodes where anchor in {conv, matmul}:
    preferred = backend.preferred_layout(anchor)
    propagate(anchor, preferred)          # 向后：输出、后继算子

for value in graph.values:
    if value.layout == UNKNOWN:
        value.layout = DEFAULT

for edge in graph.edges:
    if edge.src.layout != edge.dst.layout:
        insert_convert(edge, from=edge.src.layout, to=edge.dst.layout)
```

真正难的是 `propagate` 和 `insert_convert`：前者要知道每个算子接受哪些 layout，后者要在多个候选边上选转换代价最小的位置。生产编译器通常还会做一次「反向传播 + 冲突消解」，而不是单向遍历。

本机实测「每层都断开」的代价（`8×3×112×112`，三层 conv，每层后强制 `contiguous()` 回 NCHW）：

```text
NCHW 全链       134.40 ms
channels_last   191.55 ms
每层断开        238.40 ms
单次 layout 转换  0.93 ms
```

这组数字有两个信息：断开确实更慢；但 channels_last 在这条链上并没有赢 NCHW。下一节解释为什么不能把单次测量当结论。

## 示例一：单层卷积，NCHW vs channels_last

环境：Intel Core i7-10510U（4 线程），Windows 10，torch 2.14.1+cpu，CPU-only。输入 `(16, 3, 224, 224)`，`Conv2d(3, 32, 3, padding=1)`，warmup 3 次，每次测 10 iter，重复 5 组取中位数。

```python
import time, statistics, torch
torch.manual_seed(0); torch.set_num_threads(4)
conv = torch.nn.Conv2d(3, 32, 3, padding=1).eval()
x = torch.randn(16, 3, 224, 224)
xcl = x.to(memory_format=torch.channels_last)
conv_cl = torch.nn.Conv2d(3, 32, 3, padding=1).eval()
conv_cl.load_state_dict(conv.state_dict())
conv_cl = conv_cl.to(memory_format=torch.channels_last)

def bench(fn, warmup=3, iters=10, repeats=5):
    with torch.inference_mode():
        for _ in range(warmup): fn()
    ts = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        with torch.inference_mode():
            for _ in range(iters): fn()
        ts.append((time.perf_counter() - t0) / iters * 1000)
    return statistics.median(ts), min(ts), max(ts)

with torch.inference_mode():
    print("NCHW", bench(lambda: conv(x)))
    print("CL  ", bench(lambda: conv_cl(xcl)))
    print("maxdiff", (conv(x) - conv_cl(xcl)).abs().max().item())
```

实测：

| 布局 | 第 1 轮中位（范围） | 第 2 轮中位（范围） |
|---|---:|---:|
| NCHW | 61.49 ms（49.56–67.53） | 46.78 ms（44.15–47.34） |
| channels_last | 36.98 ms（29.92–44.48） | 27.89 ms（24.01–35.30） |

数值对齐：两种布局输出 `maxdiff = 7.15e-07`，属于 float32 正常范围。

这条结论只对**这个 shape、这个 kernel、这台机器**成立。它说明 layout 能带来约 1.66–1.68× 的差异，不说明 channels_last 永远更快。

## 示例二：三层卷积链，排序会翻转

同一台机器，输入 `(8, 3, 112, 112)`，三层 `3×3` conv（3→32→32→64，每层后 ReLU），重复测两轮（第 1 轮 5 组×5 iter，第 2 轮 7 组×10 iter）：

| 轮次 | NCHW 中位（范围） | channels_last 中位（范围） |
|---|---:|---:|
| 第 1 轮 | 134.40 ms（113.85–424.65） | 191.55 ms（165.54–258.09） |
| 第 2 轮 | 155.01 ms（112.46–177.32） | 132.67 ms（100.54–148.57） |

**排序翻转了**。原因不是代码变了，而是：

- 笔记本 CPU 有 turbo / 降频，单轮测量受温度和后台负载影响，最大值能到中位数的 3 倍。
- oneDNN 会按 shape、线程数、layout 选择不同 kernel；三层链的每一层偏好可能不同。
- 线程调度和内存带宽竞争让长链测量噪声更大。

逐层拆开看第 2 轮：

| 层 | NCHW | channels_last |
|---|---:|---:|
| conv1（3→32） | 11.44 ms | 8.76 ms |
| conv2（32→32） | 55.58 ms | 52.75 ms |
| conv3（32→64） | 116.38 ms | 60.27 ms |

单层看 channels_last 都更快，但整链的中位数差距被噪声压缩。**结论要写成「在这个环境下，这组 shape 的测量范围是……」，不能写成「channels_last 快 1.5 倍」。**

## 示例三：非连续 NCHW 也可能是 channels_last

构造一个逻辑上是 NCHW、storage 却是 NHWC 的 tensor：

```python
xnc = torch.randn(16, 224, 224, 3).permute(0, 3, 1, 2)
print(xnc.shape, xnc.stride())
print(xnc.is_contiguous(), xnc.is_contiguous(memory_format=torch.channels_last))
```

实测：

```text
torch.Size([16, 3, 224, 224]) (150528, 1, 672, 3)
False True
```

`is_contiguous()` 为 `False`，但 `is_contiguous(channels_last)` 为 `True`。卷积直接吃这个输入：

| 输入 | 时间 |
|---|---:|
| 直接传 `xnc`（channels_last stride） | 39.79 ms |
| 先 `xnc.contiguous()` 再卷积 | 97.09 ms |

直接传反而快了 2.4×，因为「连续化」把 channels_last 重排成了 NCHW，多付了一次约 9.6 MB 的拷贝，还让 kernel 换回了较慢的路径。**不要看到 `is_contiguous() == False` 就条件反射地 `.contiguous()`**；先问它是不是另一种合法布局。

## 对比与取舍

| 方案 | 优点 | 代价 | 适用场景 |
|---|---|---|---|
| 全图 NCHW | 通用、调试直观、老 kernel 多 | GPU / Tensor Core 不吃香 | CPU 传统卷积、教学 |
| 全图 channels_last | CPU/GPU 的 conv kernel 常有优化 | 部分逐元素算子要处理非连续 stride | CNN 推理主干 |
| 每层转换 | 每个算子都拿最舒服的 layout | 每次转换一次全量读写 | 几乎总是坏主意 |
| 边界转换 + 传播 | 转换次数最少 | 需要算子能力表与全局分析 | 编译器默认策略 |
| blocked（NCHWc） | cache 与 SIMD 最友好 | 布局私有、可移植性差 | oneDNN / 特定硬件 |

## 踩雷集锦

1. **把 channels_last 当成 NHWC shape**：PyTorch 的逻辑 shape 仍是 `(N,C,H,W)`，只是 stride 变了。按 NHWC 去索引会读错数据。
2. **`permute` 后直接 `view`**：报 `view size is not compatible...`。要么 `reshape`（可能复制），要么先 `contiguous()`（一定复制）。
3. **无条件 `x = x.contiguous()`**：如果 `x` 本来就是 channels_last，这一步会把它重排成 NCHW，既花钱又可能选到更慢的 kernel。
4. **只转输入不转权重**：卷积要么内部重排权重，要么走 fallback。输入、权重、输出要一起看。
5. **一次测量就下结论**：本机同一段代码两轮排序翻转；必须给 warmup、样本数、范围。
6. **在融合块中间插 `permute`**：跨 layout 的融合通常不成立，编译器只能放弃融合，损失比转换更大。
7. **忽略 `reshape` 的隐式复制**：它不报错，但在峰值内存和带宽上都会体现，尤其是大 tensor。
8. **把 `is_contiguous()` 当唯一健康检查**：它只回答「是否 C-contiguous」，不回答「是否适合这个 kernel」。

## 进阶：再往深一层

- **blocked layout**：oneDNN 内部会把 NCHW 重排成 `NChw16c` 之类的分块格式，让通道维按 16 对齐。你在 PyTorch 层看不到它，但它解释了为什么「转一次 channels_last」有时还不够。
- **layout assignment**：XLA 在 HLO 层做 layout assignment，TVM 用 `transform_layout` / `LayoutRewrite`；它们都在回答本章的问题：哪个 value 用什么 layout、转换插在哪里。
- **GPU 上的 Tensor Core**：NHWC 让通道维连续，便于 `ldmatrix` 合并访问；这部分本机无 CUDA，属于 `guided`，不要照搬 CPU 结论。
- **动态 shape 下的布局**：batch 变化会改变 stride 常量，编译器要么特化，要么保留符号 stride；Ch 23 会展开。

## 动手练习

### 练习 1：找 crossover

固定 `Conv2d(3, 32, 3, padding=1)`，把 batch 从 1 调到 32，分别测 NCHW 与 channels_last，画出两条曲线。回答：在哪个 batch 之后 channels_last 稳定领先？如果换成本机三层链，结论还成立吗？

<details>
<summary>参考思路</summary>

用本章的 `bench` 函数，batch 取 `[1, 2, 4, 8, 16, 32]`，每个点重复 5 组。小 batch 时启动开销和线程同步占比高，layout 差异会被噪声淹没；大 batch 时计算/带宽占主导，差异才稳定。三层链的结论可能不同，因为中间层的 shape 和通道数变了。

</details>

### 练习 2：制造一次「假非连续」

构造 `xnc = torch.randn(16, 224, 224, 3).permute(0, 3, 1, 2)`，分别测：

1. `conv(xnc)`
2. `conv(xnc.contiguous())`
3. `conv(xnc.to(memory_format=torch.channels_last))`

解释三者的差异，并说明哪一个发生了复制。

<details>
<summary>参考思路</summary>

`xnc` 本身已经是 channels_last stride，第 1 条走 channels_last kernel。第 2 条把它重排成 NCHW，发生一次复制。第 3 条对已经是 channels_last 的 tensor 调用 `to` 通常直接返回原 tensor（或等价视图），不复制。用 `data_ptr()` 验证。

</details>

### 练习 3：在 FX 图上数转换

对本章的三层 conv 模型做 `torch.fx.symbolic_trace`，统计图里 `permute` / `contiguous` / `to` 节点的数量。然后写一个 pass：把连续的 `permute` 合并，或者把 `permute` 推到 `output` 前。跑一遍数值对齐。

<details>
<summary>参考思路</summary>

FX 里 `permute` 会表现为 `call_method`，`contiguous` 也是。先打印 `[(n.op, n.target) for n in graph.nodes]` 建立基线。合并规则：`permute(p2) ∘ permute(p1) = permute(p2[p1])`；推到边界的前提是中间算子对 stride 不敏感。改完必须 `gm.recompile()`，并用 `torch.allclose` 对齐。

</details>

### 练习 4：量化一次转换的成本

对一个 `(64, 128, 56, 56)` 的 float32 tensor，分别测 `permute(0,2,3,1).contiguous()`、`clone()`、`to(memory_format=torch.channels_last)`，估算有效带宽（读+写字节数 / 时间）。

<details>
<summary>参考思路</summary>

`permute+contiguous` 和 `clone` 都是读一遍写一遍；`to(channels_last)` 若源是 NCHW 也等价于一次重排。用 `numel * 4 * 2 / 时间` 估算 GB/s。数值会受机器负载影响，重点看三者是否同量级——同量级说明都受带宽限制。

</details>

## 本章重点整理

- layout 的本质是 `(shape, stride)` 契约；NCHW/NHWC 是两套 stride，不是两种 dtype。
- `channels_last` 保持逻辑 NCHW shape，只把通道放到最内层；`is_contiguous()` 为 `False` 不代表布局坏。
- `permute` 是 O(1) 元数据操作；`view` 要求 stride 兼容；`reshape` 不兼容时会隐式复制。
- 单层 conv 实测 channels_last 快 1.66–1.68×，但三层链两轮排序翻转：布局收益依赖 shape、kernel 和机器状态。
- 布局转换是带宽受限的 O(n) 操作；编译器应把转换推到图边界，而不是每个算子之间。
- 判断要不要 `.contiguous()`，先看目标 kernel 是否已经支持当前 stride。

## 自我检核

- [ ] 我能用 shape + stride 手算出 `permute` 后任意元素的 offset
- [ ] 我能解释为什么 `channels_last` 的 `is_contiguous()` 是 `False` 而 `is_contiguous(channels_last)` 是 `True`
- [ ] 我能复现 `view` 的报错，并说清 `reshape` 为什么可能复制
- [ ] 我能说出至少三个影响 layout 收益的因素
- [ ] 我能解释「layout 是算子库、kernel、带宽三方契约」
- [ ] 我知道 layout propagation 的目标是把转换压到图的边界
- [ ] 我不会拿一次 benchmark 的中位数当绝对结论
- [ ] 我知道 GPU 上的 NHWC/Tensor Core 结论在本机属于 `guided`

## 延伸阅读

- **PyTorch `torch.channels_last` 文档**（https://pytorch.org/docs/stable/tensor_attributes.html#torch-memory-format）：读 memory format 的定义与 `is_contiguous` 的重载。与本章关联：channels_last 的官方语义。
- **oneDNN 文档：Memory Format**（https://oneapi-src.github.io/oneDNN/dev_guide_understanding_memory_formats.html）：读 `nchw`、`nhwc`、`nChw16c` 的区别。与本章关联：blocked layout 与 kernel 选择。
- **TVM `transform_layout` / Relax layout rewriting**（https://tvm.apache.org/docs/）：读 layout 重写 pass 的输入输出。与本章关联：编译器怎么做 layout propagation。
- **XLA Layout Assignment**（https://openxla.org/xla/layout_assignment）：读 layout 约束与转换插入策略。与本章关联：Ch 20 的 HLO layout 分配。
- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：读 `Graph` 的节点操作。与本章关联：练习 3 要在 FX 图上数转换节点。

---

→ [Ch 9 量化：从浮点到整数](./09-quantization.md)
