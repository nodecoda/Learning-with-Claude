# Ch 25 稀疏与低精度：用更少的位换更多的吞吐

> **目标**：把 BF16、FP8、INT4 的数值契约写清楚；在本机真跑 BF16 matmul 与 FP32 的误差/耗时对比、INT4 的 pack/unpack/dequant、2:4 稀疏的掩码与稠密等价验证；理解编译器在低精度路径上的四个接口——dtype 传播、混合精度、累加器位宽、量化算子融合；知道本机为什么跑不出 FP8 的加速。

## 为什么需要这个？

前面大部分章节都在 float32 上做优化（Ch 9 量化除外）：融合、tiling、并行。但推理的成本大头往往不是「算得慢」，而是「搬得慢」。一个 7B 模型用 fp32 存是 28 GB，bf16 是 14 GB，int4 是 3.5 GB——**位宽直接决定内存带宽和显存占用**。这就是低精度的第一性原理：先用更少的位把数据搬到离计算更近的地方，再想办法让计算单元吃这种格式。

稀疏是同一逻辑的另一面：2:4 稀疏让一半的权重为 0，硬件可以跳过这些乘法，等效算力翻倍。它和量化经常叠加使用（W4A16 + 2:4）。

但低精度不是「换个 dtype」这么简单。它会改三件事：

1. **数值契约**：能表示多大、多小、多精细，误差上界是多少。
2. **数据布局**：int4 两个值挤一个字节，2:4 稀疏要额外的 metadata，kernel 必须按这种布局读。
3. **编译接口**：dtype 怎么传播、在哪插入 cast、累加器多宽、量化算子能不能融进 matmul。

本章逐个拆。先给结论：**低精度的收益来自硬件支持，误差来自位宽，编译器负责把两者对上。**

> 实测环境：Windows 10 专业版，i7-10510U（4C/8T，AVX2，无 AVX512-BF16/AMX），torch 2.14.1+cpu。GPU/FP8 加速部分标 `guided`；本机的 FP8 只能走软件路径，实测数字会说明这一点。

## 先建立直觉：位预算是怎么花的

浮点数的位分三块：符号、指数、尾数。指数决定动态范围，尾数决定相对精度。

| 格式 | 总位宽 | 指数位 | 尾数位 | 最大可表示 | `eps`（相邻间隔/值） |
|---|---:|---:|---:|---:|---:|
| fp32 | 32 | 8 | 23 | ~3.4e38 | 1.19e-07 |
| bf16 | 16 | 8 | 7 | ~3.4e38 | 0.0078125 |
| fp16 | 16 | 5 | 10 | 65504 | ~9.77e-04 |
| fp8 e4m3 | 8 | 4 | 3 | 448 | 0.125 |
| fp8 e5m2 | 8 | 5 | 2 | 57344 | 0.25 |
| int8 | 8 | — | — | ±127 | 1 个量化步长 |
| int4 | 4 | — | — | ±7（对称） | 1 个量化步长 |

两个直觉：

- **bf16 用范围换精度**：指数位和 fp32 一样（不会溢出），尾数只剩 7 位，所以 `1 + 0.001` 在 bf16 里还是 `1`。它的 `eps=0.0078`，大约是 2–3 位十进制有效数字。
- **fp8 e4m3 用精度换范围**：最大只有 448，超出就饱和（实测 `500 → 448`），`eps=0.125`，相对误差可达 6% 量级。

同一次实测里的 cast 误差（`linspace(-3, 3, 100001)` 转 bf16 再转回）：

```text
bf16 cast maxerr 0.007810115814208984 meanerr 0.002170119434595108
```

这就是 bf16 的「分辨率」：绝对值 3 附近，误差约 0.0078。**所有低精度误差分析都可以从这张表推出来，不需要背。**

## 核心概念一：三种格式，三种数值契约

**BF16：训练/推理的通用半精度。** 动态范围和 fp32 一样，几乎不会溢出，所以训练里可以直接替换 fp32 的激活与权重（master weight 仍留 fp32）。代价是尾数短，累加必须用 fp32，否则误差迅速累积。

**FP8：为矩阵乘设计的 8 位浮点。** 两种变体：e4m3（精度优先，范围 ±448）与 e5m2（范围优先，精度更差）。它几乎总是配合 **per-tensor scaling** 使用：先算 `scale = max|x| / 448`，把张量压进可表示范围，再量化。没有 scale 的裸 FP8 在真实激活上会大量饱和。

**INT4：极致压缩，误差最大。** 对称 int4 只有 `-8..7` 共 16 个码点，必须配 per-group scale（如每 32 或 128 个元素一个 scale）。它主要用于**权重**（W4A16：权重 int4、激活 bf16/fp16），因为权重分布稳定、可以离线量化；激活做 int4 通常不可接受。

| 格式 | 谁适合 | 典型方案 | 误差来源 |
|---|---|---|---|
| BF16 | 激活、权重、梯度 | 全 bf16 + fp32 master | 尾数短，累加顺序 |
| FP8 | 大矩阵乘的输入 | per-tensor scale + e4m3 | 尾数 3 位 + 饱和 |
| INT4 | 权重（离线） | per-group scale，W4A16 | 16 个码点 + group 边界 |
| 2:4 稀疏 | 权重 | 剪枝 + metadata | 剪枝本身的信息损失 |

## 核心概念二：量化、打包与粒度

量化的核心公式没变（Ch 9）：

```text
q = clamp(round(x / s), qmin, qmax)
x̂ = q * s
```

变的是**存储**。int4 不能单独存，必须两个值挤进一个 `uint8`：

```text
packed = (q0 & 0xF) | ((q1 & 0xF) << 4)      # q0 在低 4 位，q1 在高 4 位
q0 = sign_extend(packed & 0x0F)               # 0..15 → -8..7
q1 = sign_extend((packed >> 4) & 0x0F)
```

解包时的符号扩展最容易写错：`0xF` 表示 15，在 int4 里是 `-1`，不是 `15`。正确做法是 `q > 7 ? q - 16 : q`。

粒度决定误差。per-tensor 只有一把尺子，一个 outlier 就能把整张量压扁；per-group 每 32/128 个元素一把尺子，误差显著下降，但要多存 scale。实测（256×256 高斯权重，8×256 激活）：

```text
group=None: W_rel=0.19519 y_rel=0.19402 packed_bytes=32768 n_scales=1
group=128:  W_rel=0.11811 y_rel=0.11501 packed_bytes=32768 n_scales=512
group=32:   W_rel=0.09787 y_rel=0.09605 packed_bytes=32768 n_scales=2048
```

打包后的数据都是 32768 字节（int4 的理论值），但 scale 的元数据不同：group=128 是 `512×4=2048` 字节，总计 34816；group=32 是 `2048×4=8192` 字节，总计 40960。相对 fp32 的 262144 字节，分别是 7.5× 和 6.4× 压缩。**粒度越细，误差越小，元数据越贵**——这是一个明确的工程折中。

## 核心概念三：2:4 结构化稀疏

2:4 稀疏规定：每 4 个连续元素里恰好保留 2 个非零。它不是「随便稀疏」，而是硬件能识别的固定模式——NVIDIA Ampere 起的稀疏 tensor core 就吃这种格式。

表示方式：

```text
values:   每 4 个元素保留 2 个，按 (K/4, 2, N) 存
metadata: 每个 group 两个 2-bit 索引，指出保留的是 0..3 中的哪两个
```

为什么是 2:4 而不是「50% 随便稀疏」？因为固定模式让硬件能用**固定长度的移位/选择网络**解压，不需要查表或变长解码。代价是剪枝时必须按组选 top-2，不能全局选 50%。

精度影响完全来自剪枝本身：被扔掉的两个值不会回来。实测在随机高斯权重上保留 top-2/4，输出相对误差 **36.7%**——随机权重没有冗余，剪一半当然伤。经过蒸馏/微调后，文献中报告的 2:4 精度损失通常在 1% 量级，但那是「训练换来的」，不是稀疏格式自带的。

## 底层机制：编译器在低精度路径上的四个接口

**1）dtype 传播与类型规则。** 图里每个节点的输出 dtype 由算子规则决定：`aten.matmul` 要求两个输入同 dtype（或可提升），`aten.add` 会把 bf16 与 fp32 提升到 fp32。编译器要做的是**在图上做类型推断**，并在类型不匹配处插入 cast。关键陷阱：`x.to(bf16) + y` 会把结果提升回 fp32，如果你没注意，后面整条链都退化成 fp32，量化白做。

**2）混合精度：算用低位，存用高位。** 训练的标准做法是「fp32 master weight + bf16 计算 + fp32 累加」。编译器要保证三件事：前向的 cast 只插一次、反向的梯度 cast 回 fp32、optimizer 更新用 fp32。这对应图上的 `to_copy`/`_to_copy` 节点；多余的 cast 会被 DCE（Ch 6）删掉，但**语义上必需的 cast 不能删**。

**3）累加器位宽。** 这是最容易被忽略、也最影响正确性的接口：

| 输入 | 累加器 | 结果 | 说明 |
|---|---|---|---|
| int8 × int8 | int32 | int32 → 反量化 | 128×128 次乘加也不溢出 |
| int4 × int4 | int32 | int32 → 反量化 | 同上，但解包有额外开销 |
| fp16 × fp16 | fp32 | fp16/fp32 | fp16 累加会溢出/失精，必须 fp32 |
| bf16 × bf16 | fp32 | bf16/fp32 | 取决于 kernel，通常 fp32 累加 |
| fp8 × fp8 | fp32 | fp16/fp32/bf16 | 由 kernel 决定 |

编译器如果把累加器写成 bf16，误差会随 `K` 增大而放大。实测 bf16 matmul 的 `rel_fro=2.88e-3` 是 torch CPU 路径的结果（该路径通常用 fp32 累加）；把累加也降到 bf16 只会更差，这正是累加器位宽必须写进类型系统的原因。

**4）量化算子融合与布局。** 推理图里常见的模式是：

```text
x → quantize → dequantize → matmul(W) → ...
```

编译器要把 `quantize/dequantize` 折叠进 matmul：权重的 scale 可以离线折进 W，激活的 scale 只能运行期乘。对 int4，还要把「解包」也融进 kernel——否则会多一次全量内存往返。

稀疏的接口更硬：2:4 的 metadata 必须按硬件规定的位布局生成。values 与 metadata 分两块存，kernel 按固定 stride 读；具体位布局由硬件/库规定。编译器如果按普通稠密张量布局分配，稀疏 tensor core 直接不能用。

## 示例一：BF16 vs FP32 的误差与耗时（实测）

```python
import time, statistics, torch
torch.manual_seed(0)
M = K = N = 1024
a64, b64 = torch.randn(M, K, dtype=torch.float64), torch.randn(K, N, dtype=torch.float64)
ref = a64 @ b64

def bench(op, n=10):
    for _ in range(3): op()
    s = []
    for _ in range(n):
        t0 = time.perf_counter(); op(); s.append(time.perf_counter() - t0)
    return statistics.median(s), min(s), max(s)

for name, dt in [("fp32", torch.float32), ("bf16", torch.bfloat16)]:
    a, b = a64.to(dt), b64.to(dt)
    out = (a @ b).float().double()
    err = (out - ref).abs()
    med, mn, mx = bench(lambda: a @ b)
    print(name, "maxabs=%.6g rel_fro=%.3g median=%.4f ms min=%.4f max=%.4f gflops=%.1f" % (
        float(err.max()), float(torch.linalg.norm(out - ref) / torch.linalg.norm(ref)),
        med * 1e3, mn * 1e3, mx * 1e3, 2 * M * K * N / med / 1e9))
```

实测（1024³，10 次取中位数）：

```text
fp32 maxabs=5.26155e-05 rel_fro=2.52e-07 median=24.1009 ms min=21.5235 max=32.6235 gflops=89.1
bf16 maxabs=0.595673   rel_fro=0.00288  median=10381.9647 ms min=6548.7465 max=14796.6643 gflops=0.2
```

两个结论必须分开读：

1. **精度**：bf16 的 `rel_fro=2.88e-3`，与「尾数 7 位」的理论预期一致；fp32 是 `2.5e-7`。误差差四个数量级，符合位宽差。
2. **性能**：bf16 慢了约 **430 倍**。这不是 bf16 的普遍规律，而是**本机没有 BF16 硬件**：CPU 只有 AVX2，没有 BF16 指令，torch 只能走软件转换/回退路径，外加转换开销。`torch.backends.cpu.get_cpu_capability()` 实测返回 `AVX2`。

所以「bf16 一定更快」是错的。**低精度的加速来自硬件指令（AVX512-BF16、AMX、tensor core），不是来自 dtype 本身。** 在没有这些指令的机器上，低精度甚至可能更慢；这也是本课把 GPU 部分标 `guided` 的原因。

顺便给一个可复现的对照：同一台机器，512³ 的 fp32 matmul 中位数 3.86 ms，bf16 中位数 1439.6 ms——同样是一到两个数量级的退化，趋势一致。

## 示例二：INT4 的 pack/unpack/dequant（实测）

```python
import numpy as np

def q_int4_sym(w, group=None):
    w = w.astype(np.float32)
    if group is None:
        s = np.abs(w).max() / 7.0
        return np.clip(np.rint(w / s), -8, 7).astype(np.int8), s, None
    shape = w.shape
    w2 = w.reshape(-1, group)
    s = np.abs(w2).max(axis=1, keepdims=True) / 7.0
    q = np.clip(np.rint(w2 / s), -8, 7).astype(np.int8)
    return q.reshape(shape), s.reshape(-1), group

def pack_int4(q):
    flat = q.reshape(-1).astype(np.uint8) & 0xF
    if flat.size % 2:
        flat = np.concatenate([flat, np.zeros(1, np.uint8)])
    return (flat[0::2] | (flat[1::2] << 4)).astype(np.uint8)

def unpack_int4(packed, numel):
    out = np.empty(packed.size * 2, dtype=np.int8)
    out[0::2] = (packed & 0x0F).astype(np.int8)
    out[1::2] = ((packed >> 4) & 0x0F).astype(np.int8)
    out = out[:numel]
    return np.where(out > 7, out - 16, out).astype(np.int8)

def dequant(q, s, group, shape):
    if group is None:
        return q.astype(np.float32) * s
    return (q.reshape(-1, group).astype(np.float32) * s[:, None]).reshape(shape)

rng = np.random.default_rng(0)
W = rng.standard_normal((256, 256)).astype(np.float32)
x = rng.standard_normal((8, 256)).astype(np.float32)
ref = x @ W

for group in (None, 128, 32):
    q, s, g = q_int4_sym(W, group)
    packed = pack_int4(q)
    un = unpack_int4(packed, q.size).reshape(q.shape)
    Wq = dequant(un, s, g, W.shape)
    print(f"group={group}: roundtrip_exact={np.array_equal(un, q)} packed={packed.nbytes}B "
          f"W_rel={np.linalg.norm(Wq-W)/np.linalg.norm(W):.5f} "
          f"y_rel={np.linalg.norm(x@Wq-ref)/np.linalg.norm(ref):.5f} "
          f"n_scales={1 if g is None else s.size}")
```

实测：

```text
group=None: roundtrip_exact=True packed=32768B W_rel=0.19519 y_rel=0.19402 n_scales=1
group=128:  roundtrip_exact=True packed=32768B W_rel=0.11811 y_rel=0.11501 n_scales=512
group=32:   roundtrip_exact=True packed=32768B W_rel=0.09787 y_rel=0.09605 n_scales=2048
```

三行要一起读：

- **`roundtrip_exact=True`**：pack/unpack 是无损的，误差全部来自量化，不是实现 bug。验收低精度 kernel 时，先测这一条。
- **压缩是 8×**：`256×256` 的 fp32 是 262144 字节，int4 是 32768 字节。
- **粒度收益递减**：从 per-tensor 到 group=128，误差从 19.5% 降到 11.8%；再到 group=32 只降到 9.8%。继续缩小 group，收益会被 scale 元数据和 kernel 复杂度吃掉。
- 别忘了元数据：group=128 的 scale 是 512 个 fp32（2048 B），group=32 是 2048 个（8192 B）。实际部署常把 scale 也转成 fp16/bf16 以再省一半。

## 示例三：2:4 稀疏的掩码与稠密等价（实测）

先按「每组 4 个保留绝对值最大的 2 个」剪枝，再压缩成 values + metadata，最后解压回稠密并与剪枝后的矩阵逐位比较。

```python
import numpy as np

def prune_2of4(W):
    K, N = W.shape
    g = W.reshape(K // 4, 4, N)
    idx = np.sort(np.argsort(-np.abs(g), axis=1, kind="stable")[:, :2, :], axis=1)
    out = np.zeros_like(W)
    rows, cols = np.arange(K // 4)[:, None], np.arange(N)[None, :]
    for s in range(2):
        out[rows * 4 + idx[:, s, :], cols] = g[rows, idx[:, s, :], cols]
    return out, idx

def pack_2bit(idx):
    # idx: (G, 2, N)，每个 group 打成一个字节（低 4 位有效）
    return (idx[:, 0, :] | (idx[:, 1, :] << 2)).astype(np.uint8)

def decompress(vals, packed, shape):
    G, _, N = vals.shape
    W = np.zeros(shape, dtype=np.float32)
    for gg in range(G):
        for j in range(N):
            b = packed[gg, j]
            W[gg * 4 + (b & 0x3), j] = vals[gg, 0, j]
            W[gg * 4 + ((b >> 2) & 0x3), j] = vals[gg, 1, j]
    return W

rng = np.random.default_rng(1)
K, N = 128, 64
W = rng.standard_normal((K, N)).astype(np.float32)
x = rng.standard_normal((16, K)).astype(np.float32)

Wsp, idx = prune_2of4(W)
idx3 = np.arange(K // 4)[:, None, None]
vals = W.reshape(K // 4, 4, N)[idx3, idx, np.arange(N)[None, None, :]]
packed = pack_2bit(idx)
Wrec = decompress(vals, packed, W.shape)

print("sparsity:", float((Wsp == 0).mean()))
print("decompress exact == pruned:", bool(np.array_equal(Wrec, Wsp)))
print("y_sp == y_rec:", bool(np.array_equal(x @ Wsp, x @ Wrec)))
print("prune rel error vs dense:", float(np.linalg.norm(x @ Wsp - x @ W) / np.linalg.norm(x @ W)))
print("bytes:", vals.nbytes, packed.nbytes, "dense:", W.nbytes)
```

实测：

```text
sparsity: 0.5
decompress exact == pruned: True
y_sp == y_rec: True
prune rel error vs dense: 0.3667420446872711
bytes: 16384 2048 dense: 32768
```

四点解读：

1. **`sparsity=0.5`**：每 4 个恰好 2 个非零，符合 2:4 定义。
2. **`decompress exact=True` 且 `y_sp == y_rec`**：压缩/解压本身无损。稀疏带来的误差**全部**来自剪枝，不是格式转换。
3. **`prune rel error=36.7%`**：随机高斯权重没有冗余，剪掉一半就是伤筋动骨。真实模型要靠蒸馏/再训练恢复，别把随机权重上的数字当成 2:4 的普遍精度损失。
4. **存储**：values 16384 B（2 个 fp32/组）+ metadata 2048 B（1 B/组，实际只用了 4 bit）。按 2 bit/组 打包时，4 个 group 可装进 1 字节，metadata 降到 512 B，总计 16896 B，相对 32768 B 是 1.94× 压缩。注意：**2:4 稀疏单独用只省一半权重，收益要和「省下的乘法」一起算**。

再补一个「等价性」的边界：`x @ Wsp` 与 `x @ Wrec` 逐位相等，但如果你的稀疏 matmul kernel 改变了累加顺序（跳过零元素、按压缩顺序累加），结果可能和稠密版本差 `1e-5` 量级。所以验收要区分「格式无损」与「数值路径一致」。

## 边界：FP8 在本机（实测 + guided）

先看 dtype 本身。torch 2.14.1+cpu 支持 fp8 的存储与 cast：

```python
import torch
vals = torch.tensor([0.0, 1e-3, 1e-2, 0.1, 1.0, 10.0, 100.0, 448.0, 500.0])
q = vals.to(torch.float8_e4m3fn).float()
print(list(zip(vals.tolist(), q.tolist())))
print(torch.finfo(torch.float8_e4m3fn).max, torch.finfo(torch.float8_e4m3fn).eps)
```

实测：

```text
[(0.0, 0.0), (0.001, 0.001953125), (0.01, 0.009765625), (0.1, 0.1015625),
 (1.0, 1.0), (10.0, 10.0), (100.0, 96.0), (448.0, 448.0), (500.0, 448.0)]
448.0 0.125
```

`100 → 96`、`500 → 448`（饱和）说明了 3 位尾数的分辨率。注意 448 是 e4m3 的上界，超过就夹住，不是 inf。

再看 matmul。**本机可以跑，但没有加速**：

```text
fp32  (512³): median=3.8627 ms
bf16  (512³): median=1439.6349 ms
fp8   (512³): median=3338.6670 ms  rel_fro=0.04596
torch._scaled_mm(fp8, fp8, out_dtype=fp32): median=3.5795 ms  rel_fro=0.03756
```

读法：

- `af @ bf` 返回的 dtype 是 `torch.float8_e4m3fn`，它是**软件模拟**（转 fp32 算再转回），所以比 fp32 慢近三个数量级，精度也差（`rel_fro=4.6%`）。
- `torch._scaled_mm` 快得多（3.58 ms，与 fp32 持平），但它并没有用上 FP8 算力——本机没有 FP8 指令，内部只能做 scale + 反量化 + fp32 计算。**持平就是没有收益。**
- 这与 BF16 的结论一致：**dtype 支持 ≠ 硬件加速**。CPU 上 `torch.float8_e4m3fn` 可用，但 AVX2 没有 FP8 点积指令。

**guided：FP8 真正加速需要什么硬件。** 本机无 CUDA，以下无法实测：

- **NVIDIA Hopper（H100）或 Ada（L40S/RTX 40）**：E4M3/E5M2 tensor core，`torch._scaled_mm` 走 CUTLASS/CUBLASLt，需要 fp32 累加器与 per-tensor scale。
- **AMD MI300 系列**：支持 FP8（E4M3/E5M2），通过 rocBLAS/hipBLASLt。
- **CPU 侧的 FP8**：普通 AVX2 桌面 CPU 不支持；需要具备 FP8 指令的新一代服务器 CPU，且编译器/库要真正调用这些指令。
- **软件栈**：PyTorch 2.x + CUDA 12+，`torch.backends.cuda.matmul.allow_fp8_reduce` 等开关；没有这些，`_scaled_mm` 会回退到模拟路径。

想看真实数字，在 GPU 上跑同一段 benchmark，并把 `torch.cuda.get_device_capability()` 打到日志里（Hopper 是 `(9,0)`，Ada 是 `(8,9)`）。**不要在没有对应硬件的机器上宣称 FP8 加速。**

## 对比与取舍

| 方案 | 压缩率 | 精度（本机实测） | 硬件要求 | 编译器要做的事 |
|---|---|---|---|---|
| BF16 | 2× | `rel_fro=2.9e-3` | AVX512-BF16 / AMX / tensor core | cast 插入、fp32 累加 |
| INT4（group=32） | 8×（含 scale 约 6.4×） | `y_rel=9.6%` | int4 点积或解包后 int8 | 解包融合、scale 布局 |
| 2:4 稀疏 | 1.94×（含 metadata） | 剪枝误差 36.7%（随机权重） | Ampere+ 稀疏 tensor core | metadata 生成、稀疏 kernel |
| FP8 e4m3 | 4× | `rel_fro=3.8e-2`（模拟） | Hopper/Ada/MI300 | scale 折叠、fp32 累加 |

三条经验：

1. **量化先看粒度，再看位宽。** int4 + group=32 的误差（9.6%）可能比 int8 + per-tensor 更可控；位宽不是唯一变量。
2. **稀疏看硬件。** 没有稀疏 tensor core 时，2:4 只省内存，不省计算，甚至因为解压变慢。
3. **FP8 必须有 scale。** 裸 fp8 在真实激活上会饱和；per-tensor scale 是标配，per-channel 更好但 kernel 更复杂。

## 踩雷集锦

1. **以为 bf16 一定更快**：本机实测慢 430×，因为无 BF16 指令；低精度加速是硬件特性。
2. **用 bf16 累加**：`K` 一大误差就失控；累加器必须 fp32（或 int32）。
3. **int4 解包忘了符号扩展**：`0xF` 当 15 用，结果整体偏移 16。
4. **pack 时元素个数为奇数**：必须补一个 0，否则解包数量对不上。
5. **只验证 `roundtrip_exact` 就认为 kernel 对**：还要验证 dequant 后的 matmul 与参考实现的误差。
6. **把 2:4 剪枝误差当成格式误差**：格式无损，误差来自剪枝；两者要分开测。
7. **FP8 不做 scale**：`500 → 448` 的饱和会静默发生；上线前必须看激活的最大值。
8. **在 CPU 上用 `_scaled_mm` 得出「FP8 没收益」的普遍结论**：本机没有 FP8 硬件，结论只适用于本机。

## 进阶：再往深一层

- **读 oneDNN 的 bf16 路径**：`torch.backends.mkldnn` 打开时，matmul 走 oneDNN，累加默认 fp32；可用 `DNNL_VERBOSE=1` 看选了哪个 kernel（Windows 上部分日志可用）。
- **W4A16 的 kernel 结构**：权重解包 + bf16 激活 + fp32 累加；解包通常和 ldmatrix 一起做，避免单独的解包 kernel。
- **2:4 的 metadata 布局**：NVIDIA 的 sparse tensor core 要求每 4 个 value 配 2 bit 索引，按特定顺序打包；cuSPARSELt 文档有图。
- **量化感知训练（QAT）**：fake-quant 节点在反向用 STE（straight-through estimator），编译器要识别并保留这些节点，不能当普通 cast 折叠。
- **混合精度的数值稳定性**：softmax/layernorm 用 fp32，matmul 用 bf16；这不是编译器能自动决定的，需要框架给出策略（如 `torch.autocast` 的 op 白名单）。

## 动手练习

### 练习 1：测出你机器上的 BF16 拐点

**任务**：对 `M=K=N ∈ {128, 256, 512, 1024}` 分别测 fp32 与 bf16 matmul 的中位数，画出（或用文字描述）比值随规模的变化。

**验收**：在没有 BF16 指令的机器上，bf16 始终慢；在支持 AVX512-BF16 的机器上，规模越大 bf16 越占优。能解释「为什么小矩阵上转换开销占比更高」。

<details>
<summary>参考解答</summary>

复用示例一的 `bench`，把 `M/K/N` 换成变量。小矩阵的计算量小，dtype 转换与调用开销占比高，bf16 更难赢；大矩阵计算主导，硬件指令的吞吐优势才显现。**常见卡点**：忘了 warmup，第一次调用包含 kernel 选择/JIT；用单次计时而不是中位数。
</details>

### 练习 2：把 int4 的 group 改成 16 并算总存储

**任务**：用示例二的代码跑 `group=16`，记录 `W_rel`、`y_rel`、`n_scales`，并算出「数据 + scale」的总字节数与压缩率。

**验收**：`W_rel` 应低于 group=32 的 0.09787；总字节数 = 32768 + `n_scales*4`，压缩率相对 262144 计算。

<details>
<summary>参考解答</summary>

`256×256/16 = 4096` 个 scale，元数据 16384 B，总计 49152 B，压缩率 5.33×。误差通常降到 8% 左右，但元数据已占数据的一半。**常见卡点**：只算 packed 字节，忘了 scale；把 `n_scales` 当成元素数而不是组数。
</details>

### 练习 3：验证 2:4 稀疏的「格式无损、剪枝有损」

**任务**：用示例三的代码，分别比较 `Wrec` vs `Wsp`、`x@Wsp` vs `x@W`，并构造一个「只保留每组前两个元素」的朴素掩码，比较两种剪枝策略的误差。

**验收**：`Wrec == Wsp` 逐位相等；top-2 策略的误差明显低于「保留前两个」。

<details>
<summary>参考解答</summary>

```python
naive = W.copy()
naive.reshape(K//4, 4, N)[:, 2:, :] = 0
print("naive rel:", np.linalg.norm(x @ naive - x @ W) / np.linalg.norm(x @ W))
print("top2  rel:", np.linalg.norm(x @ Wsp - x @ W) / np.linalg.norm(x @ W))
```

top-2 按幅值选，误差更小；朴素策略相当于随机丢一半。**常见卡点**：`reshape` 后直接改视图没写回原数组；用 `argsort` 时没加 `kind="stable"` 导致同值元素的顺序不稳定。
</details>

## 本章重点整理

- 位宽决定三件事：动态范围（指数位）、相对精度（尾数位）、内存/带宽占用。
- BF16 范围同 fp32、精度 7 位尾数；FP8 e4m3 最大 448、`eps=0.125`；INT4 只有 16 个码点，必须配 per-group scale。
- 低精度的加速来自硬件指令，不来自 dtype。本机 AVX2 上 bf16 慢约 430×，FP8 只有软件模拟。
- int4 的 pack/unpack 必须做符号扩展；量化误差随 group 变小而下降，但 scale 元数据成本上升。
- 2:4 稀疏的格式转换是无损的，误差全部来自剪枝；metadata 必须按硬件布局生成。
- 编译器的四个接口：dtype 传播、混合精度、累加器位宽、量化算子融合与布局。
- 累加器位宽是最容易错的正确性接口：int8/int4 → int32，fp16/bf16/fp8 → fp32。
- 评估低精度必须同时报「误差」与「耗时」，并注明硬件；缺一个都无法决策。

## 自我检核

- [ ] 我能说出 fp32/bf16/fp8-e4m3/int4 的指数位、尾数位与 `eps`
- [ ] 我能解释为什么 bf16 在本机比 fp32 慢，而在支持 AVX512-BF16 的机器上更快
- [ ] 我能手写 int4 的 pack/unpack 并正确处理符号扩展
- [ ] 我能解释 per-tensor 与 per-group 的误差/元数据权衡
- [ ] 我能验证 2:4 稀疏的「格式无损、剪枝有损」
- [ ] 我能说出 int8/int4/fp16/bf16/fp8 各自的累加器位宽
- [ ] 我能指出量化算子融合的常见模式（quantize/dequantize → matmul）
- [ ] 我知道 FP8 加速需要哪些硬件，并能在没有硬件时诚实标 `guided`

## 延伸阅读

- **PyTorch AMP 文档**（https://pytorch.org/docs/stable/amp.html）：读 autocast 的 op 白名单与 dtype 规则。与本章关联：混合精度的接口。
- **PyTorch float8 教程**（https://pytorch.org/tutorials/recipes/float8.html）：读 `_scaled_mm`、per-tensor scale、e4m3/e5m2 的选择。与本章关联：FP8 边界一节的 GPU 版。
- **NVIDIA 2:4 稀疏博客**（https://developer.nvidia.com/blog/structured-sparsity-in-the-nvidia-ampere-architecture-and-applications-in-search-engines/）：读 metadata 布局与稀疏 tensor core。与本章关联：核心概念三。
- **oneDNN 文档**（https://oneapi-src.github.io/oneDNN/）：读 bf16/int8 matmul 的累加器与量化属性。与本章关联：CPU 低精度路径。
- **GPTQ 论文**（https://arxiv.org/abs/2210.17323）：读 per-group 量化与误差补偿。与本章关联：int4 的工程化。

---

→ [Ch 26 正确性：golden、容差、模糊测试与数值稳定性](./26-correctness.md)
