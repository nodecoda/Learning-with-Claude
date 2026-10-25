# Ch 22 ONNX Runtime 与 TensorRT：图优化级别、Execution Provider 与量化

> **目标**：搞清楚 ONNX Runtime（ORT）在 session 初始化时做了什么、四个图优化级别各改了什么、Execution Provider（EP）如何切图与回退，以及量化为什么可能“又慢又准不了”。本机 CPU-only（onnxruntime 1.31.0 / onnx 1.23.2）：ORT 全部实测，TensorRT 与 GPU EP 标 `guided`。

## 为什么需要这个？

训练框架关心“能算出来”，推理运行时关心“算得快、占得少、能部署”。ONNX Runtime 正好卡在这两者之间：

- 它读 ONNX 图，做**图级优化**（常量折叠、算子融合、布局变换）；
- 它把图切成若干段，交给不同的 **Execution Provider**（CPU、CUDA、TensorRT、OpenVINO…）；
- 它负责内存分配、kernel 调度、量化算子的执行。

很多人把 ORT 当成“ONNX 解释器”，只关心能不能跑。真正影响性能的是三件事：

1. **图优化级别**：常量折叠和融合能省掉多少计算；
2. **EP 选择与回退**：哪段在 GPU、哪段掉回 CPU、边界拷贝多少；
3. **量化**：权重精度、校准数据、EP 是否支持 INT8 kernel。

本章用两个模型把这三件事拆开：一个 2 层 MLP（看端到端耗时），一个手工构造的“常量 matmul”图（把图优化的收益放大到可测量）。

## 先建立直觉：ONNX 是契约，ORT 是执行器 + 改写器

ONNX 只定义**算子语义和文件格式**，不定义怎么执行。同一个 `.onnx` 文件：

- ORT 可以在 CPU 上用 MLAS 跑；
- 可以在 CUDA 上用 cuBLAS/cuDNN 跑；
- 可以转成 TensorRT engine 跑；
- 可以被 TVM/Inductor 当输入重新编译。

一次 `InferenceSession` 初始化大致做：

```text
加载模型 → ONNX checker → shape 推断 → 图优化（按级别）
        → 按 EP 能力切分图 → 每个 EP 编译自己的子图
        → 分配内存 → 返回可调用的 session
```

注意：**图优化发生在 session 创建时，不是每次 run 时**。所以“优化后模型”可以直接落盘复用。

## 核心概念一：四个图优化级别

ORT 的 `GraphOptimizationLevel` 有四级（官方定义）：

| 级别 | 做什么 | 典型收益 |
|---|---|---|
| `ORT_DISABLE_ALL` | 关闭图优化 pass | 基线，便于对比 |
| `ORT_ENABLE_BASIC` | 常量折叠、冗余节点消除、identity 删除 | 减少计算与节点数 |
| `ORT_ENABLE_EXTENDED` | basic + 算子融合（如 Gemm+Relu）、NCHWc 等 | 减少 kernel launch 与内存往返 |
| `ORT_ENABLE_ALL` | extended + 更多布局/节点融合 | 依赖硬件，收益不稳定 |

`DISABLE_ALL` 关的是**图优化 pass**，不是模型加载、内存规划或 kernel 选择。所以它仍然能跑，只是图更“原始”。

一个关键事实：**优化级别不是越高越好**。`ENABLE_ALL` 可能引入硬件相关的布局变换（比如 NCHWc），换一台机器或换一个 EP 就跑不了。本机保存优化模型时 ORT 直接警告（真实日志，为排版换行）：

```text
Serializing optimized model with Graph Optimization level greater than ORT_ENABLE_EXTENDED
and the NchwcTransformer enabled. The generated model may contain hardware specific
optimizations, and should only be used in the same environment the model was optimized in.
```

这句话应该贴在每个“保存优化模型”的脚本旁边。

## 核心概念二：Execution Provider 与回退

EP 是 ORT 的“后端插件”。本机可用列表（实测）：

```python
import onnxruntime as ort
print(ort.get_available_providers())
```

```text
['AzureExecutionProvider', 'CPUExecutionProvider']
```

没有 CUDA/TensorRT/DirectML。典型 GPU 机器上会看到：

```text
['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']
```

EP 的工作方式是**按能力切图**：

1. ORT 遍历图，问每个 EP：“这个节点你支持吗？”
2. 支持的节点归入该 EP 的子图；
3. 不支持的节点回退到下一个 EP（通常是 CPU）；
4. EP 子图之间插入数据拷贝。

所以“用了 GPU”不等于“整张图都在 GPU”。一个不支持的 op 会把图切成两段，中间多一次 device→host→device 拷贝，性能可能比纯 CPU 还差。

`providers` 的顺序就是优先级：

```python
sess = ort.InferenceSession(
    "model.onnx",
    providers=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
)
```

`TensorrtExecutionProvider` 排第一，意味着“能给 TensorRT 的都给它”；剩下的再给 CUDA，最后 CPU 兜底。

## 核心概念三：量化在 ORT 里意味着什么

量化不是“把 float 换成 int 就完了”，它改变了三件事：

1. **权重存储**：fp32 → int8，模型变小；
2. **计算指令**：`MatMul` → `MatMulInteger` / `QLinearMatMul`；
3. **数值语义**：引入 scale/zero-point，误差不再可忽略。

ORT 提供两条路：

| 方式 | 是否需要校准数据 | 典型 API | 适用 |
|---|---|---|---|
| 动态量化 | 否 | `quantize_dynamic` | 权重量化，激活运行时量化 |
| 静态量化 | 是 | `quantize_static` + `CalibrationDataReader` | 激活也量化，收益更大 |

动态量化的代价是：每次推理都要对激活做 `DynamicQuantizeLinear`，多一次量化/反量化；小模型上这部分开销可能超过收益。本章会实测这个反例。

## 底层机制：一次 session 初始化做了什么

把 `InferenceSession(...)` 拆开：

```text
1. 读 protobuf，检查 opset / IR version
2. onnx shape inference（部分模型自带 value_info，可跳过）
3. 图优化：
     BASIC    → ConstantFolding, EliminateIdentity, RemoveRedundant...
     EXTENDED → FuseGemmActivation, NchwcTransformer, ...
     ALL      → 更多 layout / node fusion
4. 按 providers 顺序做 capability partition
5. 每个 EP 编译自己的子图（CPU: 选 kernel；TensorRT: build engine）
6. 内存规划（arena、复用）
7. 返回 session
```

第 3 步是本章的重点；第 5 步是 EP 的差异所在；第 6 步决定峰值内存。

`SessionOptions.optimized_model_filepath` 可以把第 3 步的结果存下来。注意：存的是**优化后的 ONNX 图**，可能含 ORT 私有 op（例如 `FusedGemm`），不一定能被别的 runtime 读。

## 示例一：导出一个模型并跑四个级别（实测）

模型：`Linear(64→64) → ReLU → LayerNorm → Linear(64→64) → 残差加`，batch 32（Windows 上先设 `$env:PYTHONIOENCODING='utf-8'`，否则导出器的 emoji 输出可能触发 GBK 编码错误）。

```python
import os, time, statistics, torch, torch.nn as nn
import onnxruntime as ort

torch.manual_seed(0)

class Block(nn.Module):
    def __init__(self, d=64):
        super().__init__()
        self.fc1 = nn.Linear(d, d)
        self.ln = nn.LayerNorm(d)
        self.fc2 = nn.Linear(d, d)
    def forward(self, x):
        h = self.ln(torch.relu(self.fc1(x)))
        return x + self.fc2(h)

m = Block().eval()
x = torch.randn(32, 64)
path = "block.onnx"
with torch.no_grad():
    torch.onnx.export(m, (x,), path, input_names=["x"], output_names=["y"], opset_version=18)

import onnx
mod = onnx.load(path)
print("nodes", len(mod.graph.node), [n.op_type for n in mod.graph.node])
```

本机真实输出：

```text
nodes 5 ['Gemm', 'Relu', 'LayerNormalization', 'Gemm', 'Add']
```

注意导出器已经替我们做了一部分融合：`LayerNorm` 直接变成一个 `LayerNormalization` 节点，而不是一堆 `ReduceMean/Sub/Pow/Sqrt`。所以后面的 ORT 图优化空间不大。

四个级别的耗时（200 次 run，20 次 warmup，median，单次测量）：

```text
DISABLE_ALL      median 0.100 ms  min 0.090  p90 0.131
ENABLE_BASIC     median 0.102 ms  min 0.095  p90 0.141
ENABLE_EXTENDED  median 0.106 ms  min 0.083  p90 0.144
ENABLE_ALL       median 0.104 ms  min 0.085  p90 0.138
```

节点数变化（保存优化模型后重新加载）：

```text
DISABLE_ALL  nodes 5 ['Gemm', 'Relu', 'LayerNormalization', 'Gemm', 'Add']
ENABLE_ALL   nodes 4 ['FusedGemm', 'LayerNormalization', 'Gemm', 'Add']
```

结论要说清楚：

- EXTENDED/ALL 确实把 `Gemm + Relu` 融合成了 `FusedGemm`（节点 5→4）；
- 但端到端耗时没有可测差异——模型太小，kernel launch 和 Python 调用开销占主导；
- 单次测量噪声（p90 比 median 高 30%–40%）大于优化收益。**不要用一个数字下结论。**

## 示例二：把图优化收益放大（实测）

小模型看不出差别，就构造一个“常量 subgraph”很大的图：

```text
C = MatMul(W1, W2)        # 512x512，两个 initializer
b = ReduceSum(C, axis=0)  # 512
y = Relu(MatMul(x, W3) + b)
```

`W1/W2/W3` 都是 initializer。`DISABLE_ALL` 下，`C` 每次 run 都要重算（512³ ≈ 0.27 GFLOP）；`BASIC` 会把整个常量分支折成 `b` 一个常量。

构造代码（`onnx.helper`）：

```python
import numpy as np, onnx
from onnx import helper, TensorProto, numpy_helper

rng = np.random.default_rng(0)
D = 512
W1 = rng.standard_normal((D, D), dtype=np.float32) * 0.02
W2 = rng.standard_normal((D, D), dtype=np.float32) * 0.02
W3 = rng.standard_normal((D, D), dtype=np.float32) * 0.02

nodes = [
    helper.make_node("MatMul", ["W1", "W2"], ["C"], name="const_matmul"),
    helper.make_node("ReduceSum", ["C", "axes"], ["b"], name="const_reduce", keepdims=0),
    helper.make_node("MatMul", ["x", "W3"], ["h"], name="dyn_matmul"),
    helper.make_node("Add", ["h", "b"], ["s"], name="add_const"),
    helper.make_node("Relu", ["s"], ["y"], name="relu"),
]
graph = helper.make_graph(
    nodes, "const_fold_demo",
    [helper.make_tensor_value_info("x", TensorProto.FLOAT, [32, D])],
    [helper.make_tensor_value_info("y", TensorProto.FLOAT, [32, D])],
    initializer=[numpy_helper.from_array(W1, "W1"), numpy_helper.from_array(W2, "W2"),
                 numpy_helper.from_array(W3, "W3"),
                 numpy_helper.from_array(np.array([0], dtype=np.int64), "axes")],
)
model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)])
model.ir_version = 10
onnx.checker.check_model(model)
onnx.save(model, "const_fold.onnx")
```

本机实测结果：

```text
constfold DISABLE_ALL  median 3.896 ms  min 2.920 ms
constfold BASIC        median 0.263 ms  min 0.218 ms
constfold ALL          median 0.309 ms  min 0.243 ms

DISABLE_ALL nodes 5 ['MatMul', 'ReduceSum', 'MatMul', 'Add', 'Relu'] inits 4
BASIC       nodes 2 ['Gemm', 'Relu'] inits 2
ALL         nodes 1 ['FusedGemm'] inits 2
```

这次差异是 **15 倍**（3.9 ms → 0.26 ms），同一次测量、同一台机器、同样的 200 次 run。原因很直接：常量分支被折掉了，每次 run 少了一次 512³ matmul 和一个 ReduceSum。

这就是“图优化级别对性能的影响”的正确看法：

- 对**已经很小、已经融合**的图，级别影响接近噪声；
- 对**含常量子图、冗余节点、可融合算子**的图，级别影响可能是数量级；
- 要看**节点数 + 耗时 + 测量边界**三个证据，不能只看级别名字。

## 示例三：shape 推断与优化模型（实测）

`onnx.shape_inference.infer_shapes` 会补齐中间值的 shape：

```python
import onnx
m = onnx.load("block.onnx")
mi = onnx.shape_inference.infer_shapes(m)
print("value_info:", [(v.name) for v in mi.graph.value_info])
```

本机真实输出：

```text
value_info: ['fc1.weight', 'fc1.bias', 'ln.weight', 'ln.bias',
             'fc2.weight', 'fc2.bias', 'linear', 'relu', 'layer_norm', 'linear_1']
```

每个中间值的 shape 都能推出来（本机实际都是 `[32, 64]`，权重是 `[64, 64]` / `[64]`）。这对 ORT 很重要：图优化需要知道 shape 才能判断融合是否合法；量化也需要 shape 来决定 per-channel 轴。

保存优化模型：

```python
so = ort.SessionOptions()
so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
so.optimized_model_filepath = "block_opt.onnx"
sess = ort.InferenceSession("block.onnx", so, providers=["CPUExecutionProvider"])
```

再加载 `block_opt.onnx`，会看到 `FusedGemm`。这个 op 是 ORT 私有的，标准 ONNX checker 不一定认；它只适合在**同一环境**里复用。

## 示例四：用 onnxruntime.transformers 做一次图优化（实测）

`big.onnx` 是一个 4 层、宽度 1024 的 MLP，导出方式与示例一相同：

```python
class Big(nn.Module):
    def __init__(self, d=1024, layers=4):
        super().__init__()
        self.fcs = nn.ModuleList([nn.Linear(d, d) for _ in range(layers)])
        self.out = nn.Linear(d, 256)
    def forward(self, x):
        for fc in self.fcs:
            x = torch.relu(fc(x))
        return self.out(x)

m = Big().eval()
x = torch.randn(128, 1024)
with torch.no_grad():
    torch.onnx.export(m, (x,), "big.onnx", input_names=["x"], output_names=["y"], opset_version=18)
```

`onnxruntime.transformers.optimizer.optimize_model` 是 BERT 系列的优化器，但也能调用 ORT 的通用优化：

```python
from onnxruntime.transformers import optimizer
om = optimizer.optimize_model(
    "big.onnx",
    model_type="bert",
    num_heads=4,
    hidden_size=1024,
    opt_level=2,
    only_onnxruntime=True,
)
om.save_model_to_file("big_tfopt.onnx")
```

本机真实结果（模型是 4 层 MLP，不是 BERT）：

```text
opt_level 1 → nodes 9  ['Gemm','Relu','Gemm','Relu','Gemm','Relu','Gemm','Relu','Gemm']
opt_level 2 → nodes 5  ['FusedGemm','FusedGemm','FusedGemm','FusedGemm','Gemm']
opt_level 3 → AssertionError
```

三个教训：

1. `opt_level=1` 只做 basic，融合不生效；
2. `opt_level=2` 才把 `Gemm+Relu` 合成 `FusedGemm`；
3. `opt_level=3` 会尝试 BERT 专属融合（attention/layernorm），在非 BERT 模型上直接 `AssertionError`。**这个工具是给 BERT 系列用的，不是通用优化器。**

## 示例五：动态量化（实测，含反例）

先造一个 `MatMul + Add` 的小模型：

```python
class MM(nn.Module):
    def __init__(self):
        super().__init__()
        self.w = nn.Parameter(torch.randn(10, 784) * 0.05)
        self.b = nn.Parameter(torch.zeros(10))
    def forward(self, x):
        return torch.matmul(x, self.w.t()) + self.b

m = MM().eval()
with torch.no_grad():
    torch.onnx.export(m, (torch.randn(64, 784),), "mm.onnx",
                      input_names=["x"], output_names=["y"], opset_version=18)
```

对一个 `MatMul + Add` 的小模型做动态量化：

```python
from onnxruntime.quantization import quantize_dynamic, QuantType
quantize_dynamic("mm.onnx", "mm_int8.onnx", weight_type=QuantType.QInt8)
```

本机真实结果：

```text
fp32: nodes ['MatMul', 'Add'],  size 2049 bytes
int8: nodes ['DynamicQuantizeLinear','Mul','MatMulInteger','Cast','Mul','Add'], size 9890 bytes

fp32 median 0.0625 ms  min 0.0427 ms
int8 median 0.1017 ms  min 0.0770 ms
maxerr 3.43e-02
```

这是一个很好的反例：

- 模型太小，`DynamicQuantizeLinear` + `Cast` 的开销超过了 int8 matmul 省下的时间；
- 权重是随机小值，量化误差 `3.4e-2` 很大；
- 文件反而变大（小模型里量化元数据占比高）。

量化要在大模型、大 matmul、支持 INT8 kernel 的 EP 上才有意义。**不要在小模型上验证量化收益。**

## 对比与取舍

| 维度 | ONNX Runtime | TorchInductor | TVM |
|---|---|---|---|
| 输入 | ONNX 图 | PyTorch 模型 | Relax/TE |
| 图优化 | session 初始化时，按级别 | 编译时，Scheduler 自动 | 编译时，pass + schedule |
| 后端选择 | EP（CPU/CUDA/TRT…） | CPU C++ / GPU Triton | LLVM/CUDA/… |
| 首次成本 | session 创建（毫秒~秒） | 编译（秒~分钟） | build（秒） |
| 动态 shape | 原生支持 | SymInt + 重编译 | 有限 |

TensorRT 的位置：

- 它是 NVIDIA 的推理编译器 + runtime，通过 `TensorrtExecutionProvider` 接入 ORT；
- 它在 session 初始化时 **build engine**（可能几十秒到几分钟），把子图编译成针对特定 GPU 的 plan；
- 支持 FP16/INT8、层融合、kernel 自动选择；
- engine 与 GPU 型号、TensorRT 版本、CUDA 版本绑定，不能跨机器复用。

本机没有 NVIDIA GPU、没有 TensorRT，以上全部 `guided`。真实的 TensorRT 流程至少需要：

```text
NVIDIA GPU (Compute Capability 7.0+)
+ 匹配版本的 CUDA / cuDNN / TensorRT
+ onnxruntime-gpu
+ providers=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
```

INT8 还需要校准数据集和 `CalibrationDataReader`；没有校准就开 INT8，精度会崩。

## 踩雷集锦

1. **以为 `ORT_DISABLE_ALL` 等于“不优化、纯解释”**：它只关图优化 pass，kernel 选择和内存规划照旧。
2. **用一次测量比较优化级别**：小模型上噪声远大于收益；至少 200 次 run + 多次重复。
3. **保存 `ENABLE_ALL` 的优化模型跨机器用**：可能含 NCHWc 等硬件相关布局，ORT 自己都会警告。
4. **把 `FusedGemm` 当标准 ONNX op**：它是 ORT contributed op，别的 runtime 可能不认。
5. **在非 BERT 模型上调用 `optimize_model(..., opt_level=3)`**：本机实测直接 `AssertionError`。
6. **在小模型上做动态量化并期待加速**：本机实测 int8 比 fp32 慢 60%，误差 3.4e-2。
7. **忽略 EP 回退**：一个不支持的 op 会把图切段，device↔host 拷贝可能吃掉全部收益。
8. **导出 ONNX 时不设 `PYTHONIOENCODING=utf-8`**：Windows 控制台的 GBK 编码会让导出器打印的 emoji 直接崩掉。

## 进阶：再往深一层

- **IO Binding**：把输入/输出绑定到预分配 device buffer，避免每次 run 的拷贝；GPU 场景收益明显。
- **EP 分区可视化**：`sess.get_profiling_start_time_ns` / profiling JSON 能看到每个节点落在哪个 EP。
- **静态量化**：`quantize_static` + 校准数据，激活也变成 int8；收益比动态量化大，但需要代表性数据。

## 动手练习

### 练习 1：复现四个级别的节点数

用本章的 `Block` 模型，分别以四个级别创建 session，并保存 `optimized_model_filepath`。用 `onnx.load` 统计节点数。回答：哪一级开始出现 `FusedGemm`？

<details>
<summary>参考思路</summary>

本机实测：`BASIC` 仍是 5 个节点；`EXTENDED` 和 `ALL` 变成 4 个，出现 `FusedGemm`。注意保存优化模型时 ORT 会打印硬件相关警告，这是预期行为。
</details>

### 练习 2：构造常量折叠图

用 `onnx.helper` 构造本章的 `const_fold.onnx`，分别在 `DISABLE_ALL` 和 `BASIC` 下测 200 次。回答：节点数从几变到几？耗时差多少倍？

<details>
<summary>参考思路</summary>

本机实测：5 → 2，3.896 ms → 0.263 ms，约 15 倍。关键是要确认 `W1/W2` 都是 initializer，否则常量折叠不会触发。
</details>

### 练习 3：shape 推断

对任意一个导出的 ONNX 模型跑 `onnx.shape_inference.infer_shapes`，列出新增的 `value_info`。回答：哪些中间值的 shape 是导出时就有的，哪些是推断出来的？

<details>
<summary>参考思路</summary>

导出器通常只写输入/输出和部分关键中间值；`infer_shapes` 会补齐所有可推导的中间值。对比 `m.graph.value_info` 和 `mi.graph.value_info` 的数量即可。若推断失败，先检查模型是否能通过 `onnx.checker.check_model`。
</details>

### 练习 4：量化反例

对本章的 `mm.onnx` 做动态量化，比较 fp32/int8 的 median 与 maxerr。回答：为什么 int8 可能更慢？

<details>
<summary>参考思路</summary>

本机实测 int8 0.1017 ms vs fp32 0.0625 ms，maxerr 3.43e-2。原因是动态量化每次 run 都要对激活做 `DynamicQuantizeLinear`，小矩阵上这部分开销大于 matmul 的收益；随机小权重也让量化误差很大。
</details>

### 练习 5：EP 回退推演（guided）

假设图里有 `MatMul`、`LayerNormalization`、`NonMaxSuppression` 三个节点，providers 顺序是 `[TensorrtExecutionProvider, CPUExecutionProvider]`，而 TensorRT 不支持 `NonMaxSuppression`。画出分区结果，并说明数据拷贝发生在哪里。

<details>
<summary>参考思路</summary>

TensorRT 接管 `MatMul` 和 `LayerNormalization`（若都支持），`NonMaxSuppression` 回退 CPU；如果 NMS 的输入来自 TensorRT 子图，需要 device→host 拷贝，输出再回 device（如果后续还有 TRT 节点）。分段越多，拷贝越多。具体支持列表以所用 TensorRT/ORT 版本为准，`guided`。
</details>

## 本章重点整理

- ONNX 是契约，ORT 是执行器 + 图改写器；图优化发生在 session 初始化时。
- 四个优化级别：DISABLE_ALL / BASIC / EXTENDED / ALL；融合通常从 EXTENDED 开始。
- 图优化收益取决于图的结构：小图接近噪声，含常量子图/可融合算子的图可能差一个数量级。
- EP 按能力切图，顺序即优先级；不支持节点回退，跨 EP 边界有拷贝。
- `optimized_model_filepath` 存的是可能含私有 op、硬件相关布局的图，不适合跨环境分发。
- 动态量化在小模型上可能更慢；静态量化需要校准数据。
- TensorRT 通过 EP 接入，engine 与 GPU/版本绑定；本机无 GPU，全部 `guided`。

## 自我检核

- [ ] 我能说出四个图优化级别各做什么
- [ ] 我能解释为什么小模型上优化级别看不出差异
- [ ] 我知道 `FusedGemm` 不是标准 ONNX op
- [ ] 我能解释 EP 分区与回退的机制
- [ ] 我知道 `optimized_model_filepath` 的风险
- [ ] 我能解释动态量化为什么可能更慢
- [ ] 我能说出 TensorRT 至少三个前置条件
- [ ] 我能设计一个能放大图优化收益的实验

## 延伸阅读

- **ONNX Runtime 图优化文档**（https://onnxruntime.ai/docs/performance/model-optimizations/graph-optimizations.html）：读四个级别与每个 transformer 的作用。与本章关联：核心概念一。
- **ONNX Runtime Execution Providers**（https://onnxruntime.ai/docs/execution-providers/）：读 CPU/CUDA/TensorRT EP 的能力与配置。与本章关联：核心概念二。
- **ONNX Runtime 量化文档**（https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html）：读动态/静态量化与校准。与本章关联：核心概念三。
- **TensorRT 开发者指南**（https://docs.nvidia.com/deeplearning/tensorrt/developer-guide/）：读 engine build、INT8 calibration、dynamic shape profile。与本章关联：TensorRT 的 `guided` 部分。
- **ONNX 规范**（https://onnx.ai/onnx/）：读 operator schemas 与 opset 版本。与本章关联：为什么本章导出统一用 opset 18。

---

→ [练习 D：同一个模型，三种栈](./practice-d-stack-comparison.md)