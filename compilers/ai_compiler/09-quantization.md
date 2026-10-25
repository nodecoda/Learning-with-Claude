# Ch 9 量化：把浮点契约换成整数契约

> **目标**：理解 scale / zero-point 如何定义一张「整数网格」，分清 per-tensor 与 per-channel 的误差边界，并在真实模型上走完「BN 折叠 → 算子融合 → 量化卷积」这条链。

## 为什么需要这个？

量化把 float32 换成 int8，理论上内存降到 1/4、整数吞吐更高。但它不是「压缩一下」这么简单：它把算子之间的浮点契约换成了**带 scale 的整数契约**，每一个边界都要重新谈判。

本机实测两个数字，先放在这里：

- 一个 `conv+bn+relu` 量化后，输出相对误差 **7.77%**（随机输入、8 批校准）。
- 另一组 `conv+bn+relu`（3→32、输入 224×224），float 推理 151.93 ms，int8 路径 430.63 ms——**慢了 2.8×**。

第二个数字不是 bug，而是提醒：量化是否加速，取决于硬件、kernel、以及你有没有把 quantize/dequantize 留在热路径上。**量化首先是一门误差管理，其次才是性能优化。**

## 先建立直觉：用一把尺子量所有数

把浮点数映射到整数，只需要两个参数：

```text
q = clamp(round(x / s) + zp, qmin, qmax)
x̂ = s * (q - zp)
```

- `s`（scale）是刻度间距，决定一个整数代表多大的实数。
- `zp`（zero-point）是实数 0 对应的整数，决定网格的偏移。
- `qmin/qmax` 是整数范围，int8 通常是对称 `[-127, 127]` 或非对称 `[0, 255]`。

以 `x ∈ [-1, 1]`、uint8 `[0, 255]` 为例：

```text
s  = (1 - (-1)) / 255 = 0.007843
zp = round(0 - (-1) / s) = 128
x = 0.3  → q = round(0.3 / 0.007843) + 128 = 166
x̂ = (166 - 128) * 0.007843 = 0.298
```

误差上界是 `s/2`（未饱和时）。这个界很朴素，但它解释了一切：**scale 越大，误差越大**；而 scale 由「这一组数里的最大绝对值」决定。一个 outlier 就能把整组数的 scale 拉大。

## 核心概念一：对称、非对称与 zero-point

| 方案 | 整数范围 | zero-point | 适合谁 | 代价 |
|---|---|---|---|---|
| 对称（symmetric） | `[-127, 127]` | 固定 0 | 权重、均值接近 0 的激活 | 表示 `[0, max]` 时浪费一半码点 |
| 非对称（affine） | `[0, 255]` | 由校准确定 | ReLU 后非负激活 | 多一个 zero-point，kernel 稍复杂 |

本机 `get_default_qconfig("onednn")` 的组合很典型：**权重 per-channel 对称 int8，激活 per-tensor 非对称 uint8**。权重做对称是因为乘法核心里 `zp=0` 可以省掉一次减法；激活做非对称是因为 ReLU 之后分布偏斜，硬套对称会浪费码点。

## 核心概念二：per-tensor 与 per-channel 是粒度问题

per-tensor 是「整个张量共用一把尺子」，per-channel 是「每个通道一把尺子」。

| 粒度 | scale 数量 | 优点 | 缺点 |
|---|---:|---|---|
| per-tensor | 1 | 元数据小、kernel 简单 | 一个 outlier 拖垮全张量 |
| per-channel（权重） | 输出通道数 | 隔离通道间尺度差异 | 需要按通道重标定，kernel 稍复杂 |
| per-row / per-column（激活） | batch 或特征维 | 隔离样本/特征差异 | 元数据随 shape 增长 |

对 matmul `C = A @ B`，常见做法是 A 按行、B 按列量化：A 的每一行有自己的 scale，B 的每一列有自己的 scale，累加仍走 int32，最后再乘回 `s_a * s_b`。

## 核心概念三：PTQ 与 QAT

| 阶段 | 做什么 | 需要什么 | 典型误差 |
|---|---|---|---|
| PTQ（post-training quantization） | 用校准数据统计 min/max，直接算 scale | 几百张无标签样本 | 中等，遇 outlier 会崩 |
| QAT（quantization-aware training） | 训练时插入 fake-quant，让模型适应量化噪声 | 训练数据、反向传播、调参 | 通常更低，但不保证 |

以对称量化为例，fake-quant 的前向是「量化再反量化」：

```text
x → round(x/s)*s → 继续前向
```

反向传播时，`round` 的梯度几乎处处为 0，所以实践里用 straight-through estimator（STE）：**前向用量化值，反向把梯度原样传过去**。这也是为什么 QAT 能在浮点优化器里训练一个「假装被量化」的模型。

关键点：QAT 不是免费午餐。本机的一个 toy Linear 上，PTQ 的 MSE 是 `0.001695`，QAT 调了三组学习率/步数分别是 `0.001774`、`0.001822`、`0.001990`——**都没有赢**。原因在示例四展开。

## 核心概念四：量化融合把边界吃掉

浮点图里 `conv → bn → relu` 是三个算子；量化图里它应该变成一个 `quantized conv`：

```text
浮点:   x_fp32 → conv → bn → relu → y_fp32
量化:   x_u8 --(s_x,zp_x)--> quantized_conv_relu --(s_y,zp_y)--> y_u8
                              ↑ 权重 int8 + per-channel scale
                              ↑ bias 在 int32 累加后加一次（具体表示由后端决定）
```

融合分两步：

1. **BN 折叠进 conv**：推理时 BN 是仿射变换，可以并入 conv 的权重和 bias。
2. **ReLU 吸收进 quantized op**：ReLU 在整数域就是 `clamp(q, zp_y, 255)`，不需要单独一个 kernel。

本机验证 BN 折叠的数值误差是 `5.96e-07`，`fuse_modules` 后的输出与浮点模型同样只差 `5.96e-07`。这说明融合在数学上是等价的，误差来自后面的量化，不是融合本身。

源码依据：`torch/ao/quantization/fuse_modules.py:129` 的 `fuse_modules` 只支持 `conv+bn`、`conv+bn+relu`、`conv+relu`、`linear+relu`、`bn+relu` 这几种序列（见该文件 138–145 行的 docstring）；`torch/ao/nn/intrinsic/modules/fused.py:59` 定义了 `ConvReLU2d`，`:163` 定义了 `ConvBnReLU2d`。

## 底层机制：量化信息为什么必须进图

如果 scale 只存在于 Python 变量里，编译器看到的仍然是 `conv(float) → relu(float)`，它无法：

- 判断这个 conv 能不能换成 int8 kernel；
- 把 `dequant → conv → quant` 匹配成一个量化算子；
- 在融合后重新计算输出 scale；
- 把 fake-quant 节点折叠掉，避免 runtime 真的做一次 round。

所以量化信息必须以**图里的属性或节点**存在：

```text
x → quantize(scale_x, zp_x) → conv(weight_int8, scale_w) → requantize(scale_out) → y
```

编译器要做的匹配大致是：

```text
dequantize(x_q) ─┐
                 ├→ conv ─→ relu ─→ quantize(y_q)
weight_q ────────┘
        ↓ 匹配
quantized_conv_relu(x_q, weight_q, bias_i32, s_x, s_w, s_y, zp_x, zp_y)
```

fake-quant 节点必须能折叠，原因是它只是**训练期的模拟**。推理图里如果还留着 `round → mul → round`，不但多几个 kernel，还会让后面的融合模式匹配失败。量化编译器通常在 `eval()` + `convert()` 之后把 fake-quant 消掉，只留下 qparams。

## 示例一：手工 int8 matmul 的误差

环境同上（torch 2.14.1+cpu）。做 `A(64×256) @ B(256×512)`，分别用 per-tensor 和 per-channel，最后用 int32 累加验证。

```python
import torch
torch.manual_seed(0)

def q_per_tensor(x, dtype_max=127):
    s = x.abs().max().item() / dtype_max
    q = torch.clamp(torch.round(x / s), -dtype_max, dtype_max)
    return q, s

def q_per_channel(x, dim, dtype_max=127):
    s = x.abs().amax(dim=dim, keepdim=True).clamp_min(1e-12) / dtype_max
    q = torch.clamp(torch.round(x / s), -dtype_max, dtype_max)
    return q, s

A = torch.randn(64, 256); B = torch.randn(256, 512); C = A @ B

def report(name, Cq):
    err = (Cq - C).abs()
    print("%-24s maxerr %.4f meanerr %.4f rel %.3f%%" % (
        name, err.max().item(), err.mean().item(),
        (err.norm() / C.norm()).item() * 100))

qa, sa = q_per_tensor(A); qb, sb = q_per_tensor(B)
report("per-tensor", (qa.float() @ qb.float()) * (sa * sb))

qar, sar = q_per_channel(A, 1); qbc, sbc = q_per_channel(B, 0)
report("per-channel", (qar.float() * sar) @ (qbc.float() * sbc))

acc = (qar.to(torch.int32) @ qbc.to(torch.int32)).float()
report("per-channel int32 acc", acc * sar * sbc)
```

实测：

```text
per-tensor               maxerr 0.9595  meanerr 0.1829  rel 1.422%
per-channel              maxerr 0.8552  meanerr 0.1262  rel 0.988%
per-channel int32 acc    maxerr 0.8552  meanerr 0.1262  rel 0.988%
```

三个观察：

1. per-channel 把相对误差从 1.42% 降到 0.99%，但**没有数量级改善**——因为这是高斯分布，没有极端 outlier。
2. int32 累加再统一乘 `sar * sbc`，结果与「先反量化再 matmul」完全一致；这正是量化 kernel 的做法：**整数累加，最后一次性 rescale**。
3. `maxerr ≈ 0.86` 听起来大，但输出量级也大；看相对误差和任务容差，不要只看绝对值。

## 示例二：一个 outlier 打崩 per-tensor

把 `B[0,0]` 改成 100，模拟权重里的极端值：

```python
B2 = B.clone(); B2[0, 0] = 100.0
    # 后续流程与示例一相同，参考值改用 A @ B2
```

实测：

| 场景 | per-tensor 输出相对误差 | per-channel 输出相对误差 | per-tensor 权重重建误差 | per-channel 权重重建误差 |
|---|---:|---:|---:|---:|
| 高斯 | 1.42% | 0.99% | 1.04% | 0.70% |
| B 有 outlier | **21.90%** | **1.31%** | **21.92%** | **1.20%** |
| A 有 outlier | 17.61% | 2.39% | 17.90%（A） | 2.35%（A） |

机制很直白：per-tensor 的 scale 被 `100` 拉到 `0.787`，原本 ±1 范围内的权重只剩下十几个码点，舍入误差相对值暴涨；per-channel 只让 outlier 所在的那一列变粗，其他列不受影响。

**结论**：per-channel 不是「更精确一点」的装饰，它是 outlier 隔离机制。权重几乎总该 per-channel；激活是否 per-channel 要看硬件和 kernel 支持。

## 示例三：融合一个真实的 conv+bn+relu

先折叠 BN，再走 PyTorch 的量化流程：

```python
import torch, warnings
warnings.filterwarnings("ignore")
torch.manual_seed(0)

class M(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.quant = torch.quantization.QuantStub()
        self.conv = torch.nn.Conv2d(3, 16, 3, padding=1)
        self.bn = torch.nn.BatchNorm2d(16)
        self.relu = torch.nn.ReLU()
        self.dequant = torch.quantization.DeQuantStub()

    def forward(self, x):
        return self.dequant(self.relu(self.bn(self.conv(self.quant(x)))))

m = M().eval()
x = torch.randn(4, 3, 32, 32)
with torch.inference_mode():
    ref = m(x)

m2 = torch.ao.quantization.fuse_modules(
    m, [["conv", "bn", "relu"]], inplace=False).eval()
m2.qconfig = torch.quantization.get_default_qconfig("onednn")
prepared = torch.quantization.prepare(m2)
with torch.inference_mode():
    for _ in range(8):
        prepared(torch.randn(4, 3, 32, 32))
q = torch.quantization.convert(prepared)
print(q)

with torch.inference_mode():
    out = q(x)
err = (out - ref).abs()
print("maxerr %.4f meanerr %.4f rel %.2f%%" % (
    err.max().item(), err.mean().item(),
    (err.norm() / ref.norm()).item() * 100))
```

实测模块结构（节选）：

```text
M(
  (quant): Quantize(scale=tensor([0.0332]), zero_point=tensor([128]), dtype=torch.quint8)
  (conv): QuantizedConvReLU2d(3, 16, kernel_size=(3, 3), stride=(1, 1),
          scale=0.009197819046676159, zero_point=0, padding=(1, 1))
  (bn): Identity()
  (relu): Identity()
  (dequant): DeQuantize()
)
```

三个算子变成了一个 `QuantizedConvReLU2d`，BN 和 ReLU 都是 `Identity()`。权重是 `torch.qint8`，`qscheme()` 为 `torch.per_channel_affine`，16 个通道各有自己的 scale，`int_repr()` 范围是 `[-128, 127]`。

误差（同一输入，与浮点模型比）：

```text
int8 conv+bn+relu maxerr 0.6158 meanerr 0.0069 rel 7.77%
```

7.77% 不算低。原因不是融合错了，而是：随机输入、只校准 8 批、激活 per-tensor、输出层没有后续补偿。真实模型要在验证集上看任务指标，而不是只看这一层的相对误差。

一个会踩的坑：per-channel 权重量化后调用 `w.q_scale()` 会报错：

```text
RuntimeError: Expected quantizer->qscheme() == kPerTensorAffine to be true, but got false.
```

因为 `q_scale()` 只适用于 per-tensor。per-channel 要用 `w.q_per_channel_scales()` 和 `w.q_per_channel_zero_points()`。

## 示例四：PTQ vs QAT，QAT 不一定赢

用一个线性目标 `y = x @ W + b + noise`，先训练浮点模型，再做 PTQ 和 QAT：

```python
import copy, torch, warnings
warnings.filterwarnings("ignore"); torch.manual_seed(0)

class Net(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.q = torch.quantization.QuantStub()
        self.fc = torch.nn.Linear(16, 8)
        self.dq = torch.quantization.DeQuantStub()
    def forward(self, x):
        return self.dq(self.fc(self.q(x)))

W = torch.randn(16, 8) * 0.5; b = torch.randn(8) * 0.1
def data(n):
    x = torch.randn(n, 16)
    return x, x @ W + b + 0.01 * torch.randn(n, 8)
xtr, ytr = data(512); xte, yte = data(128)

def train(model, steps, lr):
    model.train()
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    for _ in range(steps):
        opt.zero_grad()
        torch.nn.functional.mse_loss(model(xtr), ytr).backward()
        opt.step()

m = Net(); train(m, 300, 0.02)
def mse(model):
    model.eval()
    with torch.inference_mode():
        return torch.nn.functional.mse_loss(model(xte), yte).item()
print("float %.6f" % mse(m))

ptq = copy.deepcopy(m).eval()
ptq.qconfig = torch.quantization.get_default_qconfig("onednn")
torch.quantization.prepare(ptq, inplace=True)
with torch.inference_mode():
    for _ in range(8): ptq(xtr)
torch.quantization.convert(ptq, inplace=True)
print("ptq   %.6f" % mse(ptq))

qat = copy.deepcopy(m).train()
qat.qconfig = torch.quantization.get_default_qat_qconfig("onednn")
torch.quantization.prepare_qat(qat, inplace=True)
train(qat, 100, 0.005); qat.eval()
torch.quantization.convert(qat, inplace=True)
print("qat   %.6f" % mse(qat))
```

实测：

```text
float 0.000103
ptq   0.001695
qat   0.001774
```

同一脚本换三组 QAT 超参的实测（每组的 float/PTQ 基线相同）：

| QAT 配置 | 测试集 MSE |
|---|---:|
| lr=0.005, 100 steps | 0.001774 |
| lr=0.001, 200 steps | 0.001822 |
| lr=0.0005, 300 steps | 0.001990 |

QAT 没有赢。这不是「QAT 没用」，而是这个 toy 任务没有给 QAT 发挥空间：

- 单层 Linear、无非线性，PTQ 的 per-channel 权重已经接近最优。
- 训练数据只有 512 条，QAT 的 fake-quant 梯度噪声反而让权重偏离。
- 没有学习率衰减、没有更长的 schedule；真实 QAT 通常要这些。

**判断**：PTQ 先上，误差不达标再考虑 QAT；QAT 需要训练管线、时间和调参预算，不是 convert 一下就能变好。

## 再补一个性能反例

同一台机器（i7-10510U，4 线程），`conv(3→32)+bn+relu`，输入 `(16,3,224,224)`，warmup 3、10 iter × 5 组取中位。float 与 int8 全流程（含 quant/dequant）：

| 路径 | 中位时间 | 范围 |
|---|---:|---:|
| float | 151.93 ms | 139.47–153.10 ms |
| int8（含 QuantStub/DeQuantStub） | 430.63 ms | 416.85–469.25 ms |

int8 慢 2.8×。原因可能包括：oneDNN 在这组 shape 上选了不适合的量化 kernel、quant/dequant 本身有开销、线程数只有 4。**不要仅凭「用了 int8」就宣布加速**；要在目标硬件上量，且尽量让相邻层保持在量化域，减少边界转换。

## 对比与取舍

| 选择 | 收益 | 代价 | 什么时候选 |
|---|---|---|---|
| per-tensor | 元数据少、kernel 简单 | outlier 敏感 | 激活、硬件只支持 per-tensor |
| per-channel | 隔离通道尺度 | 元数据与 kernel 复杂度 | 权重默认 |
| PTQ | 无需训练、落地快 | 误差受校准数据影响 | 先试 |
| QAT | 可适应量化噪声 | 要训练、要调参 | PTQ 不达标 |
| 量化融合 | 少 kernel、少 requant | 依赖后端模式匹配 | 推理主干 |
| int8 | 带宽/缓存收益 | 精度与硬件约束 | 有量化 kernel 时 |

## 踩雷集锦

1. **对 per-channel 权重调 `q_scale()`**：直接报 qscheme 错误，要用 `q_per_channel_scales()`。
2. **忘了 `QuantStub`**：把 float 输入喂给 quantized module，会报 dispatch 错误（`quantized::conv2d_relu` 找不到 CPU 后端），因为输入根本不是 quantized tensor。
3. **per-tensor 激活遇到 outlier**：相对误差可以从 1.4% 跳到 21.9%，先看分布再选粒度。
4. **用训练集/错误分布校准**：scale 由校准数据决定，校准集偏了，部署就偏。
5. **在 `train()` 模式下折叠 BN**：BN 还在更新 running stats，折叠公式不成立；必须先 `eval()`。
6. **只比较单层误差**：一层 7.77% 不代表端到端 7.77%，误差会传播也会被后续层放大/抵消。
7. **把 int8 当无条件加速**：本机实测慢 2.8×；硬件和 kernel 不支持时，量化只增加开销。
8. **fake-quant 留在推理图里**：多出 round/mul 节点，还可能阻断后续融合。

## 进阶：再往深一层

- **Q/DQ 图**：ONNX 用 `QuantizeLinear` / `DequantizeLinear` 表达量化边界，TensorRT 直接在 Q/DQ 图上做 pattern matching；这与本章的「量化信息要进图」是同一件事。
- **outlier 处理**：SmoothQuant 把激活的 outlier 迁移到权重；GPTQ/AWQ 针对 LLM 权重做逐列/逐组量化。它们都在解决「一把尺子量所有数」的粒度问题。
- **int4 / FP8**：粒度从 per-channel 走向 per-group（如 128 个元素一组），误差和元数据开销的权衡更极端（Ch 25）。
- **量化 kernel**：int8 matmul 的核心是 `int32` 累加 + requantize；GPU 上还有 `dp4a` / Tensor Core 的整数路径，本机无 CUDA，属于 `guided`。

## 动手练习

### 练习 1：实现任意轴的 per-channel

把示例一的 `q_per_channel(x, dim)` 改成支持 `dim=0/1/-1`，用 `(64, 256)` 和 `(256, 512)` 验证 scale 的 shape 分别是 `(64,1)`、`(1,512)`，并解释为什么乘回去时能 broadcast。

<details>
<summary>参考思路</summary>

`x.abs().amax(dim=dim, keepdim=True)` 保留被量化那一维，其余维压成 1。`(64,1)` 与 `(64,512)` 相乘时沿列广播，`(1,512)` 沿行广播。注意 `dim=-1` 时要先归一化到正索引，否则 `keepdim` 的行为容易看错。

</details>

### 练习 2：校准集对误差的影响

把示例三的校准批数从 8 改成 1、8、32，记录 int8 输出的 `maxerr` 和相对误差。解释为什么不是「校准越多越好」。

<details>
<summary>参考思路</summary>

MinMax 观察器记录校准集上的全局 min/max。批数太少，范围偏窄，部署时遇到更大值会饱和；批数太多，范围可能被极端样本拉宽，普通值的分辨率下降。正确做法是用有代表性的校准集，并考虑 percentile / KL 校准，而不是盲目加数据。

</details>

### 练习 3：手推 BN 折叠

给定 `conv` 的 `W, b` 和 BN 的 `gamma, beta, mean, var, eps`，推导 `W'`、`b'`，并用 PyTorch 验证 `conv_bn(x) == conv(x; W', b')` 的 `maxdiff`。

<details>
<summary>参考思路</summary>

BN 在推理时是 `y = gamma * (x - mean) / sqrt(var + eps) + beta`。把缩放乘进卷积权重：`W' = W * gamma / sqrt(var + eps)`（按输出通道广播），`b' = (b - mean) * gamma / sqrt(var + eps) + beta`。本机实测 `maxdiff = 5.96e-07`。

</details>

### 练习 4：找一个「量化不加速」的 shape

固定量化模型，改变 batch 和通道数，测 float 与 int8 的时间。找出 int8 开始领先的配置，或者证明在这台机器上始终不领先。

<details>
<summary>参考思路</summary>

复用本章的 `bench`， batch 取 `[1, 4, 16, 64]`，通道取 `[3, 16, 64]`。注意把 quant/dequant 计入总时间；如果你的部署路径能保持量化域，应该单独测「纯 quantized conv」而不是整条 Python 模块。

</details>

## 本章重点整理

- 量化的本质是 `q = round(x/s) + zp`，误差上界是 `s/2`；scale 由极值决定，所以 outlier 是核心风险。
- 权重通常 per-channel 对称 int8，激活通常 per-tensor 非对称 uint8；粒度是误差与 kernel 复杂度的权衡。
- PTQ 靠校准，QAT 靠 fake-quant + STE；QAT 不保证更好，本机 toy 任务就没赢。
- `conv+bn+relu` 先折叠再量化，最终应匹配成一个 quantized op；BN/ReLU 变成 Identity。
- 量化信息必须进图（qparams 或 Q/DQ 节点），否则编译器无法融合、无法重算 scale。
- int8 不等于加速；本机 CPU 上含 quant/dequant 的路径慢了 2.8×。

## 自我检核

- [ ] 我能写出 affine 量化的正反变换，并解释 `s` 和 `zp` 各自控制什么
- [ ] 我能说出 per-tensor 在 outlier 下崩溃的机制
- [ ] 我能解释为什么 int32 累加后只需要一次 rescale
- [ ] 我能区分 PTQ 与 QAT 的流程和代价
- [ ] 我能解释 `conv+bn+relu` 为什么要先折叠 BN 再量化
- [ ] 我知道 per-channel 权重不能用 `q_scale()`
- [ ] 我能说出至少两个「量化不加速」的原因
- [ ] 我知道量化信息为什么必须出现在图里，而不是 Python 变量里

## 延伸阅读

- **PyTorch Quantization 文档**（https://pytorch.org/docs/stable/quantization.html）：读 PTQ、QAT、per-channel 与 `fuse_modules` 的官方流程。与本章关联：示例三、四的 API 依据。
- **ONNX Quantization / QDQ 表示**（https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html）：读 `QuantizeLinear` / `DequantizeLinear` 的图表示。与本章关联：量化信息进图的标准形式。
- **SmoothQuant 论文**（https://arxiv.org/abs/2211.10438）：读激活 outlier 迁移到权重的动机。与本章关联：outlier 粒度问题的进阶解法。
- **TensorRT Developer Guide: Quantization**（https://docs.nvidia.com/deeplearning/tensorrt/developer-guide/）：读 Q/DQ 图的 pattern matching 与校准。与本章关联：后端如何消费量化图（GPU 部分属 `guided`）。
- **TVM Relax 量化**（https://tvm.apache.org/docs/）：读 `qnn` 算子与 quantize/dequantize 的 lowering。与本章关联：编译器侧的量化接口。

---

→ [Ch 10 内存规划：生命周期、复用与峰值](./10-memory-planning.md)
