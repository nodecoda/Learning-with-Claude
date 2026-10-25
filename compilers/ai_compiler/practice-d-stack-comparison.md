# 练习 D：同一个 MLP，三种栈（TVM / ONNX Runtime / TorchInductor）

> **目标**：用一个固定的 2 层 MLP、固定的权重和输入，在 TVM（默认 lowering）、ONNX Runtime（CPU EP）、TorchInductor（CPU）上做端到端对比。练习的重点不是“谁最快”，而是**把测量边界、首次成本、数值对齐、差异来源**说清楚。

## 为什么需要这个？

单看一张 benchmark 表，很容易得出“A 比 B 快”的结论；但换一台机器、换一个 batch、把首次编译算进去，排序就可能翻转。这个练习训练的是**可复现的比较方法**：固定权重与输入、明确计时边界、分开报告首次与稳态成本、用数值对齐证明比较的是同一件事。

做完之后，你应该能拿到一个新模型，独立判断：该用哪条栈、瓶颈可能在哪、哪些数字不能信。

## 任务规格

### 模型

```text
输入:  x   [64, 784]  float32
层 1:  Linear(784 → 256) → ReLU
层 2:  Linear(256 → 10)
输出:  y   [64, 10]   float32
```

权重用固定种子生成，三层实现共用同一份 numpy 数组。参考输出由 PyTorch eager 计算。

### 必须报告的量

| 量 | 定义 |
|---|---|
| 首次成本 | 从“开始编译”到“第一次拿到正确输出”的墙钟时间 |
| 稳态耗时 | 预热 20 次后，连续 200 次的 **median** 与 **min** |
| 数值误差 | `max(abs(out - ref))`，ref 为 eager 输出 |
| 线程数 | torch 4 线程、ORT `intra_op_num_threads=4`、TVM 默认调度（单线程） |
| 重复次数 | 至少 2 个独立进程，报告两次结果 |

### 验收标准

1. 三种栈的 `maxerr` 都小于 `1e-4`；
2. 每个数字都注明“含不含首次编译/session 创建/张量转换”；
3. 至少给出一个“为什么这个栈快/慢”的机制解释，而不是只贴表格；
4. 明确标注哪些格子是 `guided`（GPU/TensorRT/调优后的 TVM）。

## 先建立直觉：三种“编译”发生的时刻不同

同样是“把模型变成可执行代码”，三者的时间点完全不同：

```text
TVM:
  写 IR → tvm.relax.build → 得到 module → VM 加载 → 第一次调用（分配/初始化）
  编译发生在 build，运行前完成。

ONNX Runtime:
  export ONNX → InferenceSession(...) → 图优化 + EP 分区 + kernel 选择
  编译发生在 session 创建，运行前完成。

TorchInductor:
  torch.compile(model) → 第一次调用才触发 Dynamo 抓图 + Inductor codegen + cl.exe
  编译发生在第一次调用，运行时“顺手”完成。
```

所以“首次成本”这一列，三者测的其实不是同一件事：

- TVM 的 `build` 是纯编译，VM 加载是另一段；
- ORT 的 session 创建包含图优化和 kernel 选择；
- Inductor 的首次调用包含抓图、编译、链接、加载。

练习要求你把它们分开报告，而不是混成一个“启动时间”。

## 核心概念一：公平比较的四个条件

### 1. 同一份权重与输入

不要各自 `torch.randn`。先生成一次，存成 `npz`，三边都读同一份：

```python
rng = np.random.default_rng(0)
x  = rng.standard_normal((64, 784), dtype=np.float32)
w1 = (rng.standard_normal((256, 784)) * 0.05).astype(np.float32)
b1 = (rng.standard_normal(256) * 0.01).astype(np.float32)
w2 = (rng.standard_normal((10, 256)) * 0.1).astype(np.float32)
b2 = (rng.standard_normal(10) * 0.01).astype(np.float32)
```

注意 PyTorch `Linear.weight` 的形状是 `[out_features, in_features]`，TVM 的 `R.linear` 也是这个约定（计算 `x @ W^T + b`），所以可以直接复用。

### 2. 同一精度

全部 float32。不要一边 fp32 一边 TF32 或 fp16，否则数值误差会掩盖实现差异。

### 3. 同一线程数

- torch：`torch.set_num_threads(4)`
- ORT：`so.intra_op_num_threads = 4`
- TVM：默认 schedule **没有 parallel 原语**，实际是单线程；这一点必须写进结论，不能假装公平。

如果想公平比较“都开 4 线程”，TVM 需要手写 `parallel` 调度（Ch 12），本章没做，属于 `guided` 的扩展项。

### 4. 同一测量边界

统一测“一次前向，输入已就绪，输出已产生”：

- 不含模型加载/编译；
- 不含 `numpy → tensor` 转换（提前转好）；
- 含输出分配（三者都会分配输出）；
- 不含 `torch.no_grad()` 之外的 autograd 开销（推理模式）。

如果你把 `torch.from_numpy(x)` 放进计时循环，eager 会多出几十微秒，结论就变了。本练习的第一版就踩过这个坑：eager 从 0.27 ms 变成 0.69 ms。

## 核心概念二：首次成本 vs 稳态成本

两种成本要分开看，因为它们的优化手段完全不同：

| 成本 | 受什么影响 | 优化手段 |
|---|---|---|
| 首次（编译/构建） | 图大小、pass 数量、C++ 编译、缓存 | 缓存、AOT 编译、减少模板实例化 |
| 稳态（每次推理） | kernel 质量、融合、内存分配、launch 开销 | 调度、融合、量化、IO binding |

本机实测的首次成本量级（同一台机器，冷/热缓存不同）：

```text
TVM build:            0.32–0.44 s
ORT session create:   0.005–0.037 s
Inductor 首次调用:    12.7–27.9 s（新进程 + C++ 编译）
PyTorch eager:        ~0（无编译）
```

Inductor 的首次成本高得离谱，是因为它要调用 `cl.exe` 编译 C++ 并链接成 `.pyd`。这不是“编译器慢”，而是“把编译放到了运行时”。

## 核心概念三：数值对齐标准

参考实现是 PyTorch eager。判定标准：

```python
np.allclose(out, ref, atol=1e-5, rtol=1e-5)
maxerr = np.abs(out - ref).max()
```

本机实测：

```text
ORT (ENABLE_ALL):  maxerr 2.09e-06
TVM (default):     maxerr 3.87e-06
Inductor:          maxerr 0.0
```

三个都远小于 `1e-5`。`maxerr=0.0` 的原因和 Ch 18 一样：Inductor 最终调用同一个 BLAS `addmm`，累加顺序一致。TVM 默认 lowering 是标量循环，累加顺序不同，所以有 `1e-6` 量级差异。

**不要用 `==` 比较浮点输出**；也不要把 `maxerr=0.0` 当成“这个后端更准”。

## 底层机制：差异到底来自哪里

把一次 `y = relu(x @ W1^T + b1) @ W2^T + b2` 拆成四类成本：

| 成本 | 说明 | 谁受影响 |
|---|---|---|
| 矩阵乘 | 12.8M MAC（第一层）+ 0.16M MAC（第二层） | kernel 质量（BLAS vs 标量循环） |
| 融合 | ReLU 是否可以和 matmul 的 epilogue 合并 | ORT 的 FusedGemm、Inductor 的独立 kernel |
| 内存 | 中间结果 `[64,256]` 的写回与读取 | 融合程度、buffer 复用 |
| 调度/框架 | Python wrapper、VM 调用、session run | 小模型上可能占主导 |

本机的关键观察：

1. **TVM 默认 lowering 的 matmul 是标量三重循环**，没有 tiling/vectorize/parallel。单独用 TE 测 `64×784 @ 784×256`，median 约 **52.5 ms**，和 Relax 版本的 60+ ms 一致。这不是 TVM “慢”，而是“没调度的 TVM 慢”。
2. **ORT 用 MLAS 的 GEMM kernel**，并且 `ENABLE_ALL` 会把 `Gemm+Relu` 融合成 `FusedGemm`；小模型上 session run 的开销也很低。
3. **Inductor 把 matmul 交给 BLAS（extern addmm），ReLU 生成独立 C++ kernel**；稳态不差，但 Python wrapper + guards 在小模型上有固定开销。
4. **eager 本身已经很快**：它直接调 BLAS，没有编译开销。小 MLP 上，编译型后端的优势很难体现。

这就是为什么练习 D 的价值不在“排名”，而在“解释排名”。

## 示例：完整对比脚本（实测）

把下面的脚本保存到 `.scratch\ai-compiler\practice-d\bench_all.py`，在加载了 MSVC 环境的终端里运行：

```powershell
cmd /c "call D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat && set PYTHONIOENCODING=utf-8 && .scratch\aic-env\Scripts\python.exe .scratch\ai-compiler\practice-d\bench_all.py"
```

<details>
<summary>参考脚本（点击展开）</summary>

```python
import os, time, statistics, numpy as np, torch, torch.nn as nn
os.environ["TORCHINDUCTOR_CACHE_DIR"] = r".\inductor_cache_final"
import onnxruntime as ort
import tvm
from tvm.script import relax as R

torch.set_num_threads(4)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

d = np.load("data.npz")
ref = np.load("ref.npy")
x, w1, b1, w2, b2 = d["x"], d["w1"], d["b1"], d["w2"], d["b2"]

m = nn.Sequential(nn.Linear(784, 256), nn.ReLU(), nn.Linear(256, 10)).eval()
with torch.no_grad():
    m[0].weight.copy_(torch.from_numpy(w1)); m[0].bias.copy_(torch.from_numpy(b1))
    m[2].weight.copy_(torch.from_numpy(w2)); m[2].bias.copy_(torch.from_numpy(b2))
xt = torch.from_numpy(x)


def stats(fn, warm=20, n=200):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter(); fn(); ts.append(time.perf_counter() - t0)
    return statistics.median(ts) * 1e3, min(ts) * 1e3


with torch.no_grad():
    med, mn = stats(lambda: m(xt))
    print(f"eager          median {med:.3f} ms  min {mn:.3f}")

#ONNX Runtime
p = "mlp.onnx"
for nm, lvl in [("ort-disable", ort.GraphOptimizationLevel.ORT_DISABLE_ALL),
                ("ort-all", ort.GraphOptimizationLevel.ORT_ENABLE_ALL)]:
    so = ort.SessionOptions()
    so.graph_optimization_level = lvl
    so.intra_op_num_threads = 4
    t0 = time.perf_counter()
    s = ort.InferenceSession(p, so, providers=["CPUExecutionProvider"])
    create = time.perf_counter() - t0
    inp = s.get_inputs()[0].name
    out = s.run(None, {inp: x})[0]
    med, mn = stats(lambda: s.run(None, {inp: x}))
    print(f"{nm:14s} median {med:.3f} ms  min {mn:.3f}  create {create:.3f}s  "
          f"maxerr {np.abs(out - ref).max():.2e}")

#TorchInductor
with torch.no_grad():
    t0 = time.perf_counter()
    cm = torch.compile(m, backend="inductor")
    out = cm(xt)
    first = time.perf_counter() - t0
    med, mn = stats(lambda: cm(xt))
    print(f"inductor       median {med:.3f} ms  min {mn:.3f}  compile+first {first:.2f}s  "
          f"maxerr {np.abs(out.numpy() - ref).max():.2e}")

#TVM（TVMScript，默认 lowering）
@tvm.script.ir_module
class MLP:
    @R.function
    def main(x: R.Tensor((64, 784), "float32"),
             w1: R.Tensor((256, 784), "float32"), b1: R.Tensor((256,), "float32"),
             w2: R.Tensor((10, 256), "float32"), b2: R.Tensor((10,), "float32")):
        with R.dataflow():
            h = R.linear(x, w1, b1)
            h2 = R.nn.relu(h)
            o = R.linear(h2, w2, b2)
            R.output(o)
        return o

t0 = time.perf_counter()
ex = tvm.relax.build(MLP, target="llvm")
build = time.perf_counter() - t0
vm = tvm.relax.VirtualMachine(ex, tvm.cpu())
args = [tvm.runtime.tensor(a, tvm.cpu()) for a in (x, w1, b1, w2, b2)]
t0 = time.perf_counter(); out = vm["main"](*args).numpy(); first = time.perf_counter() - t0
med, mn = stats(lambda: vm["main"](*args))
print(f"tvm-default    median {med:.3f} ms  min {mn:.3f}  build {build:.2f}s  "
      f"first {first*1e3:.1f}ms  maxerr {np.abs(out - ref).max():.2e}")
```

</details>

生成 `data.npz` 和 `mlp.onnx` 的准备工作（在 `.scratch\ai-compiler\practice-d\` 目录下运行）：

```python
import numpy as np, torch, torch.nn as nn
rng = np.random.default_rng(0)
x = rng.standard_normal((64, 784), dtype=np.float32)
w1 = (rng.standard_normal((256, 784)) * 0.05).astype(np.float32)
b1 = (rng.standard_normal(256) * 0.01).astype(np.float32)
w2 = (rng.standard_normal((10, 256)) * 0.1).astype(np.float32)
b2 = (rng.standard_normal(10) * 0.01).astype(np.float32)
np.savez("data.npz", x=x, w1=w1, b1=b1, w2=w2, b2=b2)

m = nn.Sequential(nn.Linear(784, 256), nn.ReLU(), nn.Linear(256, 10)).eval()
with torch.no_grad():
    m[0].weight.copy_(torch.from_numpy(w1)); m[0].bias.copy_(torch.from_numpy(b1))
    m[2].weight.copy_(torch.from_numpy(w2)); m[2].bias.copy_(torch.from_numpy(b2))
    np.save("ref.npy", m(torch.from_numpy(x)).numpy())
    torch.onnx.export(m, (torch.from_numpy(x),), "mlp.onnx",
                      input_names=["x"], output_names=["y"], opset_version=18)
```

## 实测结果

机器：Windows 10/11 x86_64，CPU-only，4 线程，20 次 warmup + 200 次计时。两个独立进程各跑一次（同一台机器，负载不同）。

| 栈 | 首次成本 | 稳态 median（run 1 / run 2） | min（run 1 / run 2） | maxerr |
|---|---|---|---|---|
| PyTorch eager | ~0 | **0.270 / 0.839 ms** | 0.183 / 0.620 | 0（参考） |
| ORT DISABLE_ALL | session 0.026 / 0.018 s | **0.228 / 0.339 ms** | 0.162 / 0.283 | 2.09e-06 |
| ORT ENABLE_ALL | session 0.005 / 0.037 s | **0.239 / 0.354 ms** | 0.227 / 0.310 | 2.09e-06 |
| TorchInductor | 27.9 / 12.7 s | **0.524 / 1.024 ms** | 0.424 / 0.483 | 0.0 |
| TVM（默认 lowering） | build 0.32 / 0.44 s | **64.7 / 61.8 ms** | 33.3 / 48.2 | 3.87e-06 |

TVM 的 `first`（VM 首次调用）另计：42.4 / 52.1 ms，包含输出分配与 lazy 初始化。

表中所有数字都是 CPU 实测；GPU、TensorRT、以及“TVM + 手写 schedule”这几格没有实测，属 `guided`，不要拿它们和上表直接比较。

补充测量：单独用 TE 写 `64×784 @ 784×256`、不写任何 schedule，`tvm.build(target="llvm")` 的 median 是 **52.5 ms**（20 次调用）。这验证了“TVM 慢在默认调度，不慢在编译”。

同一脚本第三次运行（热缓存）：eager 0.894 ms、ORT 0.342/0.371 ms、Inductor 0.411 ms（compile+first 5.58 s）、TVM 52.7 ms。sub-ms 项目的排序会随机器负载翻转，这正是本练习要求多次重复的原因。

## 分析：差异来自哪里

### 1. TVM 的 60 ms：没有调度

TE 路径的默认 lowering 生成的是标量三重循环（Relax 默认 lowering 的性能同量级）：

```text
for i in 0..64:
  for j in 0..256:
    for k in 0..784:
      C[i,j] += A[i,k] * B[k,j]
```

- 没有 tiling：每次访问都打 global memory；
- 没有 vectorize：一次一个 float；
- 没有 parallel：单线程。

`64×784×256 ≈ 12.8M` 次乘加，在 60 ms 量级意味着约 0.4 GFLOP/s，正好是“标量 + 无 cache 优化”的水平。Ch 11–13 的 tiling/vectorize/parallel 就是为这个差距准备的。

### 2. ORT 的 0.23–0.35 ms：GEMM + 融合

- CPU EP 用 MLAS 的 GEMM kernel，已经做了 blocking/vectorize/多线程；
- `ENABLE_ALL` 把 `Gemm+Relu` 融成 `FusedGemm`，少一次中间结果写回；
- session run 的 Python 开销很低。

`DISABLE_ALL` 和 `ENABLE_ALL` 在这张图上差异很小，因为模型只有 4 个节点，融合省下的那点内存流量被 launch 开销盖住了。这和 Ch 22 的结论一致。

### 3. Inductor 的 0.5–1.0 ms：BLAS + wrapper

- `addmm` 走 BLAS，和 eager 同源；
- ReLU 是独立的 C++ kernel（因为有 `del`/复用逻辑，没并进 addmm）；
- Python wrapper、guards、`empty_strided_cpu` 分配都有固定成本；
- 小模型上，这些固定成本比 kernel 本身还大。

所以 Inductor 在这张图上**没有赢 eager**。这不是 Inductor 的问题，是模型太小。换成 transformer block（大 matmul、长 elementwise 链）结论会不同（Ch 30）。

### 4. eager 的 0.27–0.84 ms：BLAS + Python

eager 没有编译，但每次调用都要走 Python → ATen → BLAS。模型小时，Python/调度开销占比高；两次测量差 3 倍，说明这台机器负载波动很大。

### 5. 跨 run 波动

run 1 和 run 2 的 sub-ms 数字差了 1.3–3 倍。原因：

- 共享机器上的 CPU 争用；
- 频率调节/温度；
- 后台进程；
- Python GC。

所以本练习要求**至少两次独立进程**，并报告 `min`。如果只看一次 median，很容易得出错误结论。TVM 的 60 ms 量级在两次 run 中稳定，是因为它远离噪声底。

## 对比与取舍

| 场景 | 推荐栈 | 理由 |
|---|---|---|
| PyTorch 模型、想快速加速 | TorchInductor | 一行接入，自动融合 |
| 部署到 C++/移动端、要独立 runtime | ONNX Runtime | 成熟 EP、量化工具链 |
| 要精确调度/特殊硬件/研究 | TVM | 显式 schedule + 自动调优 |
| NVIDIA GPU 推理、追求极致 | TensorRT（ORT EP） | 层融合 + INT8 + engine（`guided`） |
| 跨框架交换 | ONNX / StableHLO | 契约稳定 |

## 踩雷集锦

1. **各自随机初始化权重**：数值对不上，后面所有比较都失效。先存 `npz`。
2. **计时循环里做 `torch.from_numpy`**：eager 会被额外收费，结论反转。
3. **只跑一次 median**：本机 sub-ms 项目的跨 run 波动可达 3 倍。
4. **TVM 用默认 lowering 就下结论“TVM 慢”**：它只是没调度；Ch 11–13 的优化能改变量级。
5. **ORT 不预热**：第一次 run 包含内存分配和 kernel 初始化，必须 warmup。
6. **线程数不一致**：torch 默认可能用满所有核，ORT 默认也可能；显式设成 4。
7. **Inductor 不加载 vcvars**：直接 `InvalidCxxCompiler`（Ch 18）。
8. **把 `maxerr=0.0` 当普遍结论**：只是这条路径复用了同一个 BLAS。

## 进阶：再往深一层

- **TVM 加 schedule**：对 `linear` 手写 split/reorder/vectorize/parallel，观察 60 ms 能降到多少（`guided`，需要改 Relax 的 `call_tir`）。
- **ORT IO Binding**：预分配输入/输出，去掉每次 run 的拷贝。
- **Inductor `mode="max-autotune"`**：让 matmul 走模板搜索；CPU 收益有限，GPU 才明显（`guided`）。
- **批量扫描**：batch 从 1 到 1024，画“耗时 vs batch”曲线，观察谁先变成 compute-bound。
- **动态 shape**：同一模型用 `dynamic_shapes` 导出，观察 ORT/Inductor 的重编译行为（Ch 23）。

## 动手练习

### 练习 1：复现表格

按本章脚本跑两次独立进程，记录五行数字。回答：哪一行的两次差异最大？为什么？

<details>
<summary>参考思路</summary>

本机 eager 从 0.270 变 0.839 ms（3.1 倍），Inductor 从 0.524 变 1.024 ms（2 倍），ORT 约 1.5 倍；TVM 稳定在 60 ms 量级。sub-ms 项目更容易被调度噪声影响，TVM 因为远离噪声底而稳定。
</details>

### 练习 2：改线程数

把 torch 和 ORT 都改成 1 线程，重跑。回答：TVM 的相对位置变了吗？

<details>
<summary>参考思路</summary>

torch/ORT 会变慢，TVM 默认本来就是单线程，所以相对差距缩小，但 TVM 仍然慢一个数量级——因为差距主要来自 tiling/vectorize，不是线程数。这个练习帮你区分“并行度”和“单核效率”。
</details>

### 练习 3：换 batch

把 batch 从 64 改成 1024，重跑三种栈。回答：TVM 的 60 ms 会变成多少？比例是否线性？

<details>
<summary>参考思路</summary>

TVM 的标量循环是 O(batch)，大致线性增长到约 1 s 量级；ORT/Inductor 的 GEMM 更接近线性但常数小得多。具体数字需实测，不要照搬。重点观察“斜率”，它反映每行的单位成本。
</details>

### 练习 4：给 TVM 加一个简单 schedule（guided）

用 TE 写 matmul，加 `split` + `reorder` + `vectorize`，测 64×784×256。回答：能降到多少？还差 ORT 多少？

<details>
<summary>参考思路</summary>

参考 Ch 11–12 的 schedule：对 k 维 split、对 j 维 vectorize（宽度 8）、对 i 维 parallel。通常能从 50 ms 降到个位数 ms，但要接近 MLAS 还需要 cache blocking 和更细的 tile。具体数字依赖机器，属 `guided` 扩展，不写进主表。
</details>

### 练习 5：解释“Inductor 为什么没赢”

用 Ch 18 的生成代码证据，解释为什么这个 MLP 上 Inductor 的稳态可能比 eager 慢。列出至少两个固定开销。

<details>
<summary>参考思路</summary>

（1）Python wrapper + guards 的每次调用开销；（2）`empty_strided_cpu` 的输出分配；（3）ReLU 是独立 kernel，未与 addmm 融合；（4）小矩阵上 BLAS 调用本身很短，固定开销占比高。模型变大后，这些开销被摊薄，Inductor 才可能赢。
</details>

## 本章重点整理

- 三种栈的“编译时刻”不同：TVM 在 `build`，ORT 在 session 创建，Inductor 在第一次调用。
- 公平比较需要统一权重、输入、精度、线程数和测量边界。
- TVM 默认 lowering 是标量循环，60 ms 量级；这不是 TVM 的上限，是“没调度”的结果。
- ORT 用 GEMM + 图融合，小模型上 0.2–0.4 ms。
- Inductor 在小 MLP 上没有优势：BLAS + wrapper + 独立 ReLU kernel，固定开销占主导。
- 数值对齐全部在 `1e-5` 以内；`maxerr=0.0` 只说明路径相同，不说明更准。
- sub-ms 测量必须多次重复并报告 min/median；跨 run 波动可达 3 倍。

## 自我检核

- [ ] 我能说清三种栈各自的“首次成本”包含什么
- [ ] 我能解释为什么 TVM 默认 lowering 慢一个数量级
- [ ] 我知道 ORT 的 `FusedGemm` 在这张图上为什么收益不明显
- [ ] 我能列出 Inductor 在小模型上的至少两个固定开销
- [ ] 我能设计一个不偏袒任何一方的计时方案
- [ ] 我知道 `maxerr=0.0` 的真实原因
- [ ] 我能区分“并行度不足”和“单核效率不足”
- [ ] 我能标出哪些结论需要 GPU/TensorRT 才能验证（`guided`）

## 延伸阅读

- **TVM 调度教程**（https://tvm.apache.org/docs/how_to/tune_with_autoscheduler/）：读 matmul 的 schedule 与 autotune。与本章关联：把 TVM 从 60 ms 拉回可用区间。
- **ONNX Runtime 性能调优**（https://onnxruntime.ai/docs/performance/）：读 graph optimization、threading、IO binding。与本章关联：ORT 的测量边界。
- **PyTorch torch.compile 性能指南**（https://pytorch.org/docs/stable/torch.compiler_performance.html）：读 “Measuring performance” 与 “Cold start”。与本章关联：Inductor 的首次成本。
- **MLPerf Inference 规则**（https://mlcommons.org/benchmarks/inference-datacenter/）：读 latency/throughput 的测量规范。与本章关联：为什么不能只报一个 median。
- **Ch 27 性能工程**（后续章节）：profiling、瓶颈定位与回归。与本章关联：下一步怎么定位差异。

---

→ [Ch 23 动态 shape 与控制流](./23-dynamic-shapes.md)