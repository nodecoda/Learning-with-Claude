# Ch 0 环境与最小闭环：从 PyTorch 图到 TVM 生成的机器码

> **目标**：装好一套 CPU 可跑的 AI 编译器实验环境，亲手走完「PyTorch 模型 → 图 IR → TVM 编译 → 执行 → 数值对齐」这条链路，并知道每一步的产物是什么。

## 为什么需要这个？

学 AI 编译器最容易掉进的坑，是把它当成「另一个编译器」来读文档：看了一堆 pass 名字、schedule 原语、lowering 阶段，却从没看过一张真实的图长什么样，也没听过自己生成的 kernel 跑出声音。

这一章先把闭环打通。闭环一旦通了，后面每一章都只是往这条链路上加一个环节：

```text
nn.Module
   │  torch.fx.symbolic_trace / torch.export
   ▼
图 IR（FX Graph / ATen Graph / ONNX / Relax）
   │  图级 pass：折叠、融合、布局、量化
   ▼
优化后的图
   │  调度：tile / reorder / vectorize / parallel
   ▼
TIR（Tensor IR）
   │  codegen
   ▼
LLVM IR / C / CUDA
   │  build
   ▼
可执行 kernel + runtime 调用
```

这条链路里，**每一层都可能丢信息，也都可能赚性能**。本课要练的就是：知道每一层在做什么选择，以及选择错了会怎样。

## 先建立直觉：三次「换语言」

一次模型编译，至少发生三次表达方式的切换：

1. **Python 对象 → 图**：`nn.Module` 里的 `forward` 是命令式代码，有 `if`、循环、Python 副作用。抓图就是把它变成一张**只包含算子与数据依赖**的 DAG。
2. **图 → 调度后的 IR**：图只说明「算什么」，不说明「怎么算」。同一个 matmul，可以是三重循环、可以是 tiled + vectorized、也可以是调库。调度就是在这里做选择。
3. **IR → 机器码**：把循环和内存访问翻译成 LLVM IR，再交给 LLVM 生成 x86 指令。

三次切换的共同点：**每换一次，就离「语义」远一点，离「机器」近一点**。AI 编译器的价值，就是在还有语义信息的时候做出正确的优化决策。

## 核心概念一：图 IR 不是代码

PyTorch 有两种常用抓图方式，产物不一样：

| 方式 | 产物 | 特点 |
|---|---|---|
| `torch.fx.symbolic_trace` | FX Graph，节点是 Python 层算子（`operator.matmul`、`torch.relu`） | 轻量、可读、适合教学与自定义 pass；对动态控制流支持有限 |
| `torch.export.export` | ATen Graph，节点是 `torch.ops.aten.*` | 更接近真实执行、支持 dynamic shape；适合对接后端 |

两者都是**数据流图**：节点是算子，边是 tensor。它们不是 Python 字节码，也不是 C 代码——只是「算什么」的声明。

## 核心概念二：调度决定性能

图 IR 只描述计算，不描述循环结构。以 `C = A @ B` 为例，图里只有一个 `matmul` 节点；但底层可以是：

```c
for (i = 0; i < N; i++)
  for (j = 0; j < N; j++)
    for (k = 0; k < N; k++)
      C[i][j] += A[i][k] * B[k][j];
```

也可以是按 `k` 切块、按 `j` 向量化、把 `i` 并行化。**语义完全相同，性能差几十倍**。调度就是把这些选择显式写出来。

## 核心概念三：数值对齐是验收标准

编译器优化不能改变语义。验收方式不是「看起来对」，而是：

```python
np.abs(out - reference).max() <= atol + rtol * np.abs(reference).max()
```

float32 的 matmul 用不同累加顺序，误差在 `1e-5` 量级是正常的；如果误差到了 `1e-2`，那不是「精度问题」，是**编译错了**。后面每一章的例子都会给数值对齐结果。

## 底层机制：一次编译到底发生了什么

以 TVM 为例，`tvm.build` 大致做四件事：

1. **Lower TIR**：把高层 schedule 展开成带具体循环边界、buffer 访问、alloc 的 TIR。
2. **Codegen**：把 TIR 翻译成目标语言。`target="llvm"` 时生成 LLVM IR。
3. **Module 组装**：把 host 代码、device 代码、常量打包成 runtime module。
4. **加载**：runtime 把模块映射进进程，暴露成可调用函数。

本机是 CPU-only，所以 `target="llvm"` 走的是 x86-64 后端；`target="cuda"` 会直接报错（Ch 14 会演示）。

## 环境安装与目录约定

### 建一个独立 venv

不要往系统 Python 或别的项目 venv 里装 TVM：它的 DLL、LLVM 版本、numpy ABI 都可能和现有环境冲突。本课统一用：

```powershell
cd D:\Learning-with-Claude
python -m venv .scratch\aic-env
$py = ".scratch\aic-env\Scripts\python.exe"
```

装依赖（CPU 版 torch 必须走 PyTorch 的 CPU index，否则会拉 CUDA wheel）：

```powershell
& $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
& $py -m pip install apache-tvm numpy onnx onnxruntime onnxscript
```

实测版本（2026-10）：

```text
python 3.11.12
torch 2.14.1+cpu
tvm 0.27.0.post1
onnx 1.23.2
onnxruntime 1.31.0
```

`onnxscript` 不是可选项：torch 2.14 的 ONNX 导出器依赖它，缺了会在 `torch.onnx.export` 里报 `ModuleNotFoundError`。

### 目录约定

本课所有实验放在 `.scratch\ai-compiler\` 下，按章分目录：

```text
.scratch\ai-compiler\
  ch0\
    probe.py
    probe.out.txt
  ch5\
  ch12\
  ...
```

这样做的好处：出错时能整目录删掉重来，不会污染教材仓库；每章的产物也能直接对照。

### 一个必须记住的环境变量

Windows 控制台默认是 GBK。torch 的 ONNX 导出器会打印 `✅`，直接触发：

```text
UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'
```

每次开新终端先设：

```powershell
$env:PYTHONIOENCODING = "utf-8"
```

这不是「编码玄学」，是控制台代码页与 Python 输出编码不一致；设了就稳定。

## 示例一：用 FX 抓一张图
先定义一个小模型：

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
```

抓图并打印：

```python
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

读这张图，注意三件事：

1. `placeholder` 是输入，`get_attr` 是参数，`call_function` 是算子，`output` 是返回值。
2. 每个节点都有 `num_users`，这是后面做 DCE 和融合的基础。
3. 图里**没有** Python 的 `+`、`@`、`relu` 调用栈，只剩数据依赖。

换成 `torch.export`：

```python
ep = torch.export.export(m, (x,))
print(ep.graph)
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
```

差别很关键：`export` 把参数也变成了 `placeholder`，算子换成了 `aten` 命名空间。**FX 图更像「源码」，ATen 图更像「可执行契约」**。

## 示例二：TVM 编译 matmul 并运行

TVM 0.27 的 API 与旧教程不同。下面这段是本机实测可跑的写法：

```python
import tvm
import numpy as np
from tvm import te

n = 128
A = te.placeholder((n, n), name="A")
B = te.placeholder((n, n), name="B")
k = te.reduce_axis((0, n), name="k")
C = te.compute((n, n), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

func = te.create_prim_func([A, B, C])
sch = tvm.s_tir.Schedule(func)

blk = sch.get_sblock("C")
loops = sch.get_loops(blk)
ko, ki = sch.split(loops[2], factors=[8, None])
sch.reorder(loops[0], loops[1], ko, ki)

f = tvm.build(sch.mod, target="llvm")
```

执行并验收：

```python
a = np.random.rand(n, n).astype("float32")
b = np.random.rand(n, n).astype("float32")

ta = tvm.runtime.tensor(a, tvm.cpu())
tb = tvm.runtime.tensor(b, tvm.cpu())
tc = tvm.runtime.empty((n, n), "float32", tvm.cpu())

f(ta, tb, tc)
out = tc.numpy()
print("maxerr", float(np.abs(out - a @ b).max()))
```

实测输出：

```text
maxerr 2.288818359375e-05
```

这个 `2.29e-05` 是 float32 的正常累加误差，不是 bug。换成 `float64` 会降到 `1e-13` 量级；换一个随机种子，数字会变，但量级不变。

再看生成的 LLVM IR 开头：

```python
src = str(f.inspect_source())
print("\n".join(src.splitlines()[:8]))
```

实测输出：

```text
; ModuleID = 'TVMMod'
source_filename = "TVMMod"
target datalayout = "e-m:w-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-windows-msvc"

@.str = private constant [10 x i8] c"TypeError\00", align 1
@.str.1 = private constant [10 x i8] c"Expected \00", align 1
@.str.2 = private constant [2 x i8] c"3\00", align 1
```

注意 `target triple` 是 `x86_64-pc-windows-msvc`——TVM 用的是本机 ABI。这就是 Ch 14 要展开的「后端选择」。

## 示例三：ONNX 导出与 ORT 对照

同一个小模型导出 ONNX，再用 ONNX Runtime 跑：

```python
import torch
import onnxruntime as ort

class M(torch.nn.Module):
    def forward(self, x):
        return torch.relu(x @ x.T + 1.0)

m = M().eval()
x = torch.randn(4, 4)

torch.onnx.export(
    m, (x,), "m.onnx",
    input_names=["x"], opset_version=18,
)

sess = ort.InferenceSession("m.onnx", providers=["CPUExecutionProvider"])
print(sess.run(None, {"x": x.numpy()})[0].shape)
```

实测输出：

```text
(4, 4)
```

两个坑必须先说：

1. **opset 要 ≥ 18**。torch 2.14 的导出器对 17 会警告并自动升版；直接写 18 更省事。
2. **Windows 控制台默认 GBK**，导出器的 `✅` 会触发 `UnicodeEncodeError`。先设 `$env:PYTHONIOENCODING = "utf-8"`。

## 对比与取舍

| 维度 | FX | torch.export | ONNX | TVM Relax/TIR |
|---|---|---|---|---|
| 抽象层次 | Python 算子 | ATen 算子 | 标准算子集 | 计算图 + 调度 |
| 可读性 | 高 | 中 | 中 | 低 |
| 动态 shape | 有限 | 支持 | 支持 | 支持（需显式） |
| 适合做什么 | 教学、自定义 pass | 对接后端 | 跨框架交换 | 编译与优化 |
| 本课用途 | Ch 5–10 | Ch 2、Ch 23 | Ch 22 | Ch 12–17 |

一句话：**FX 用来理解图，export 用来对接真实后端，ONNX 用来跨栈对比，TVM 用来做优化实验**。

## 踩雷集锦

1. **照抄旧 TVM 教程**：`te.create_schedule` 在 0.27 已不存在，会报 `AttributeError`。用 `te.create_prim_func` + `tvm.s_tir.Schedule`。
2. **`sch.get_block` 不存在**：0.27 叫 `sch.get_sblock`；`get_loops` 要传 block，不是 `PrimFunc`。
3. **`tvm.nd.array` 不存在**：用 `tvm.runtime.tensor(...)`；要拿输出用 `tvm.runtime.empty(...)` 再 `.numpy()`。
4. **`from_dlpack` 报 64 字节对齐**：numpy 小数组默认不满足；用 `runtime.tensor` 复制一份最稳。
5. **`get_source()` 不存在**：0.27 用 `inspect_source()`。
6. **ONNX 导出 GBK 崩溃**：设 `PYTHONIOENCODING=utf-8`，并把 opset 提到 18。
7. **以为 `tvm.build(target="cuda")` 能跑**：本机无 CUDA，会报 target 不可用；GPU 内容本课标 `guided`。
8. **用 `==` 比较浮点输出**：必然失败；用 `np.allclose` 或 `maxerr`。

## 进阶：再往深一层

- 打开 `TVM_LOG_DEBUG=1` 看 lowering 各阶段（本机是 release 包，部分日志可能没有，属于 `guided`）。
- 用 `f.time_evaluator(f.entry_name, tvm.cpu(), number=10)` 测 kernel 时间，和 `numpy` 对比。
- 把 `split` 的 factor 从 8 改成 4、16、32，观察时间变化——这是 Ch 11 的预演。
- 用 `torch.export` 的 `dynamic_shapes` 导出一个动态 batch 的图，观察 `SymInt` 出现在哪里（Ch 23 展开）。

## 动手练习

### 练习 1：改模型，读图

给 `Block` 加一个 bias（`self.b = nn.Parameter(...)`），重新抓 FX 图，回答：多出了哪个节点？`get_attr` 的数量变了吗？

<details>
<summary>参考思路</summary>

会多一个 `get_attr`（bias）和一个 `operator.add`。原来的 `+ 1.0` 也会变成 `add`，所以 `add` 节点从 1 个变 2 个。用 `for n in g.graph.nodes: print(n.op, n.target)` 逐行核对。
</details>

### 练习 2：改 schedule，测时间

把 Ch 0 的 matmul 例子改成 `n=256`，分别用 `split` factor 4 / 8 / 16，跑 `time_evaluator`，记录三组时间。

<details>
<summary>参考思路</summary>

```python
ft = f.time_evaluator(f.entry_name, tvm.cpu(), number=20)
print(ft(ta, tb, tc))
```

时间会随机器负载波动，重点看趋势而不是绝对值。factor 太大或太小都可能变慢——这是 Ch 16 的 cache 问题。
</details>

### 练习 3：FX vs export

同一模型分别用 `symbolic_trace` 和 `torch.export.export` 抓图，列出两个图的节点 `target` 差异。

<details>
<summary>参考思路</summary>

FX 是 `operator.matmul` / `torch.relu`；export 是 `torch.ops.aten.matmul.default` / `torch.ops.aten.relu.default`。export 还把参数变成 `placeholder`。
</details>

### 练习 4：制造一个失败

故意把 TVM 的 `target` 改成 `"cuda"`，运行并记录完整报错；再改回 `"llvm"`。

<details>
<summary>参考思路</summary>

本机没有 CUDA runtime，`tvm.build` 会在 target 检查或 module 加载阶段失败。把报错原文记下来——Ch 14 会解释「target 可用性」和「codegen 与 runtime 是两件事」。
</details>

### 练习 5：数值对齐

用 `float64` 重跑 matmul，比较 `maxerr` 与 `float32` 的差异，并解释原因。

<details>
<summary>参考思路</summary>

`float64` 的 `maxerr` 会小几个数量级。原因是累加精度不同，不是编译器「更准」；两种精度下的结果都应对得上 `np.allclose`。
</details>

## 本章重点整理

- 一次 AI 编译至少三次换语言：Python → 图 → 调度 IR → 机器码。
- FX 图是 Python 算子级 DAG；`torch.export` 是 ATen 级契约；ONNX 是跨框架交换格式。
- 图只说明「算什么」，调度才决定「怎么算」，性能差异主要来自调度。
- TVM 0.27 的 API 与旧教程差异很大，必须以本机实测为准。
- 验收标准是数值对齐（`maxerr` / `allclose`），不是「看起来对」。
- CPU-only 环境下 `target="llvm"` 可用，`target="cuda"` 不可用。

## 自我检核

- [ ] 我能不看笔记说出 FX 图和 ATen 图的三个区别
- [ ] 我能解释 `placeholder` / `get_attr` / `call_function` / `output` 各自的含义
- [ ] 我能写出 TVM 0.27 的 `create_prim_func` + `s_tir.Schedule` 最小例子
- [ ] 我知道 `tvm.runtime.tensor` 和 `tvm.runtime.empty` 的分工
- [ ] 我能解释 `maxerr=2e-05` 为什么是正常的
- [ ] 我能说出至少三个「照抄旧教程会炸」的 API
- [ ] 我能把同一个模型分别导出 FX、ATen、ONNX 并说清用途
- [ ] 我知道本机为什么跑不了 CUDA target

## 延伸阅读

- **TVM 官方 Getting Started**（https://tvm.apache.org/docs/get_started/tutorials/）：读「Compile and Deploy」一节，对照本机的 0.27 API 看差异。与本章的关联：最小闭环的官方版本。
- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：重点读 `symbolic_trace`、`Interpreter`、`Transformer` 三节。与本章的关联：Ch 5 的 API 基础。
- **torch.export 文档**（https://pytorch.org/docs/stable/export.html）：读「Export IR」与 `dynamic_shapes`。与本章的关联：Ch 2 与 Ch 23。
- **ONNX Runtime Execution Providers**（https://onnxruntime.ai/docs/execution-providers/）：读 CPU EP 与图优化级别。与本章的关联：Ch 22 的对照栈。
- **TVM 论文 (OSDI 2018)**（https://www.usenix.org/conference/osdi18/presentation/chen）：读第 3–4 节，理解为什么要把「计算」和「调度」分开。与本章的关联：Ch 11–13 的理论基础。

---

→ [Ch 1 AI 编译器是什么：四层分工](./01-what-is-ai-compiler.md)