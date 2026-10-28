# Ch 4 — 图捕获：Dynamo 的符号追踪与 guard

> **目标**：搞清 Dynamo 在「跑一遍你的代码」时到底记录了什么、放弃了什么、以及它凭什么保证之后重用这张图是安全的。学完这章，你能读懂 `TORCH_LOGS=graph_breaks` 的输出，并判断一段代码能不能被编译。

## 为什么需要这个？

Ch 3 说的是「图长什么样」，这章说的是「图怎么来的」。

这个区别很重要，因为：

> **图不是写出来的，是「跑」出来的。**

Dynamo 不解析 AST、不看源码。它**真的执行**你的 `forward`，只是把 tensor 换成了会记录操作的代理对象。所以：

- 代码里没被执行的路径 → 不在图里
- 执行了但 Dynamo 看不懂的操作 → **graph break**，图被切断

理解这个机制，才能解释「为什么我的模型编译后更慢了」。

## 先建立直觉

把 Dynamo 想成一个**跟拍的记录员**：

```
你： 「我开始做饭了」
记录员：「第 1 步：切菜（记录）」
你： 「第 2 步：开火（记录）」
你： 「尝一口，如果淡了加盐」    ← 记录员看不懂「尝」这个动作
记录员：「停！这里我记不了，前半段先交出去执行」
          （执行到这里，用真实数据算出咸淡）
记录员：「好，结果知道了，继续记录后半段」
```

这就是 **graph break**：追踪在「Dynamo 不理解的操作」处被打断，切出一张图，然后 eager 执行那一步，再开一张新图。

## 符号追踪（symbolic tracing）

核心机制是**代理对象**。Dynamo 把你的输入 tensor 包成一个 `TensorVariable`，你对它做的每个操作都被拦截并记录。

```python
def f(x):
    y = x * 2
    return y + 1
```

追踪过程（伪代码）：

```
x 是 TensorVariable（不是真 tensor）
y = x * 2        → 记录: mul(x, 2)  → y 是新 TensorVariable
  y + 1          → 记录: add(y, 1)  → 返回新 TensorVariable
```

**关键点：整个过程没有真正计算。**`x * 2` 不会真的产生一个 tensor 的数值，只产生一个「这个操作发生过」的记录。

这带来一个直接后果：**任何需要真实数值的地方都会失败**。

```python
def f(x):
    if x.sum() > 0:     # 需要真实数值判断真假 → 失败
        ...
```

和 Ch 3 的 `symbolic_trace` 一样，Dynamo 也过不去——但它的处理方式不同：`symbolic_trace` 直接抛异常，Dynamo 会**切图继续**。

## Graph break：实测

```python
import torch

def f(x):
    y = x * 2
    s = float(y.sum())      # 转成 Python float → 图断在这里
    if s > 0:
        y = y + 1
    return y

x = torch.randn(8)
cf = torch.compile(f)
print(cf(x))
```

用日志开关看 Dynamo 怎么解释：

```bash
TORCH_LOGS=graph_breaks ~/.venvs/learn/bin/python gb.py
```

实测输出（torch 2.9.1+cpu，节选）：

```
Graph break from `Tensor.item()`, consider setting:
Graph break: from user code at:
    s = float(y.sum())      # 强制 graph break：转成 Python float
[__graph_breaks] Graph break in user code at /tmp/probe/gb.py:4
[__graph_breaks] Graph Break Reason: Unsupported Tensor.item() call with capture_scalar_outputs=False
[__graph_breaks]   Explanation: Dynamo does not support tracing `Tensor.item()` with config.capture_scalar_outputs=False.
[__graph_breaks]   Hint: Set `torch._dynamo.config.capture_scalar_outputs = True` or `export TORCHDYNAMO_CAPTURE_SCALAR_OUTPUTS=1` to include these operations in the captured graph.
[__graph_breaks]  For more details about this graph break, please visit: https://meta-pytorch.github.io/compile-graph-break-site/gb/gb0124.html
```

这段日志把三件事讲清楚了，**这是本课最值得学会的调试技能之一**：

1. **在哪断的**：`/tmp/probe/gb.py:4`
2. **为什么断**：`Unsupported Tensor.item() call with capture_scalar_outputs=False`
3. **怎么修**：设置 `capture_scalar_outputs=True` 或环境变量 `TORCHDYNAMO_CAPTURE_SCALAR_OUTPUTS=1`

甚至给了文档链接 `gb0124.html`。**先读日志，再改代码。**

### 修一下看看

```bash
TORCHDYNAMO_CAPTURE_SCALAR_OUTPUTS=1 TORCH_LOGS=graph_breaks \
  ~/.venvs/learn/bin/python gb.py
```

设置后 `Tensor.item()` 能被捕获进图（以 `item` 节点的形式），graph break 消失。但注意：这会让图里出现「依赖运行时数值」的节点，对 AOT 编译到边缘设备**仍然不友好**（Ch 5 会讲原因）。

## Guard：图能被重用的理由

这是 Dynamo 设计里最精妙的部分。

问题：编译一次很贵。如果把图缓存起来，第二次调用直接用，**凭什么保证结果正确**？

答案：**guard（守卫）**。Dynamo 在编译时记录一系列「前提条件」，每次调用前检查。

```python
import torch

def f(x):
    return x * 2

cf = torch.compile(f)
print("第一次：", cf(torch.randn(4, 8)).shape)
torch._dynamo.reset()
print("第二次：", cf(torch.randn(16, 8)).shape)
```

在编译后的代码对象里，guard 长这样（用 `TORCH_LOGS=guards` 可以看到）：

```bash
TORCH_LOGS=guards ~/.venvs/learn/bin/python -c "
import torch
@torch.compile
def f(x): return x * 2
x = torch.randn(4, 8)
f(x)
"
```

实测输出（本机原文，去掉日志前缀）：

```
GUARDS:
| | +- TENSOR_MATCH: check_tensor(L['x'], Tensor, DispatchKeySet(CPU, BackendSelect, ADInplaceOrView, AutogradCPU), torch.float32, device=None, requires_grad=False, size=[4, 8], stride=[8, 1])  #   # <string>:4 in f
```

`TENSOR_MATCH` 一次性检查了七件事：

| 检查项 | 实测值 | 变化会怎样 |
|---|---|---|
| 是否 Tensor | `Tensor` | 换成别的类型 → 重编译 |
| DispatchKeySet | `CPU, BackendSelect, ...` | 换 device（CPU→CUDA）→ 重编译 |
| dtype | `torch.float32` | 换成 float16 → 重编译 |
| requires_grad | `False` | 开启梯度 → 重编译 |
| **size** | **`[4, 8]`** | **batch 变 16 → 重编译** |
| stride | `[8, 1]` | 非连续内存 → 重编译 |

注意 **`stride=[8, 1]`** 也在 guard 里——这意味着把一个 `transpose` 过的 tensor 传进来会触发重编译，即使 shape 完全一样。

### 这意味着什么

```python
cf(torch.randn(4, 8))    # ✅ 命中缓存，直接用编译好的图
cf(torch.randn(16, 8))   # ❌ shape 不符，guard 失败 → 重新编译
```

**每次 shape 变化都会触发重编译。** 这就是为什么动态 shape 的模型用 `torch.compile` 会「越跑越慢」——它在不停地编译新版本。

> **对边缘部署的关键含义**：边缘设备通常 batch=1 固定 shape。这反而让 guard 完全命中，**编译一次跑到底**。这是边缘场景少有的优势。

### 看 guard 失败

```bash
TORCH_LOGS=recompiles ~/.venvs/learn/bin/python -c "
import torch
@torch.compile
def f(x): return x * 2
f(torch.randn(4, 8))
f(torch.randn(16, 8))
"
```

实测（节选）：

```
[__recompiles] Recompiling function f in /tmp/...:123
[__recompiles] triggered by the following guard failure(s):
[__recompiles]     - 0/0: tensor 'L['x']' size mismatch at index 0. expected 4, actual 16
```

`expected 4, actual 16`——精确告诉你哪个维度变了。

## 三层结构：Dynamo / AOTAutograd / Inductor

现在可以看清整个 AI 编译器的第一段了：

```
你的 forward
    │  ① Dynamo：符号追踪 → FX Graph（前向）
    ▼
FX Graph（前向）
    │  ② AOTAutograd：自动微分 → 反向图 + 前向图规范化
    ▼
规范化的前向 + 反向图
    │  ③ Inductor：调度 + 代码生成
    ▼
C++ kernel / Triton kernel
```

实测看到这三层（`TORCH_LOGS=graph_code` 会打印 FX Graph 的 Python 代码）：

```bash
TORCH_LOGS=graph_code ~/.venvs/learn/bin/python -c "
import torch
@torch.compile
def f(x): return torch.relu(x * 2 + 1)
f(torch.randn(4))
" 2>&1 | head -40
```

实测（节选）：

```python
def forward(self, L_x_: "f32[4][1]cpu"):
    mul: "f32[4][1]cpu" = l_x_ * 2;  l_x_ = None
    add: "f32[4][1]cpu" = mul + 1;  mul = None
    relu: "f32[4][1]cpu" = torch.relu(add);  add = None
    return (relu,)
```

三个值得注意的细节：

1. **参数名变成了 `L_x_`**，带类型注解 `"f32[4][1]cpu"`——这是 Dynamo 给 fx graph 加的形状/类型标注。
2. **源码里写的是 `x * 2`，这里显示的也是 `* 2`**，不是 `aten.mul.Tensor`。这个日志级别打印的是「以 Python 源码形式重现的图」。
3. **每个中间变量用完立刻 `= None`**——这是 Dynamo 主动插入的，为了让引用计数尽早归零，提前释放 activation 内存。这是边缘场景非常想要的行为（Ch 22 会再谈）。

对照一下前一层（真正的 FX graph）用的是 `aten.*` 还是 `operator.*`：

```bash
TORCH_LOGS=graph_code python -c "..."
```

这就是为什么「看清自己在看哪一层」很重要——同一个模型，`graph_code` 打印的是 Python 形态，`graph` 属性里存的是 `torch.ops.aten.*` 节点，而裸 `symbolic_trace` 给的是 `operator.*`。三层三种写法。

## 一个完整的可复现实验

`code/04_capture/capture.py`：

```python
"""观察 Dynamo 抓了什么、guard 检查了什么。"""
import torch

def simple(x):
    return torch.relu(x * 2 + 1)

def with_scalar(x):
    s = float(x.sum())      # 触发 graph break
    return x + s

def with_python_loop(x):
    y = x
    for i in range(3):      # 编译期可知的循环 → 会被展开，不是 graph break
        y = y * 2
    return y

for name, fn in [("simple", simple),
                 ("with_scalar", with_scalar),
                 ("with_python_loop", with_python_loop)]:
    cf = torch.compile(fn)
    out = cf(torch.randn(4))
    print(f"{name}: ok, shape={tuple(out.shape)}")
```

实测输出：

```
simple: ok, shape=(4,)
with_scalar: ok, shape=(4,)
with_python_loop: ok, shape=(4,)
```

三个都「能编译」——但内部差异巨大。加上日志开关：

```bash
TORCH_LOGS=graph_breaks python capture.py 2>&1 | grep "Graph break in user code"
```

实测输出：

```
Graph break in user code at /tmp/probe/cap/capture.py:12
```

**只有一处**，第 12 行——正是 `with_scalar` 里的 `s = float(x.sum())`。`simple` 和 `with_python_loop` 都编成了完整的一张图。

行号是最实用的定位手段：日志直接告诉你源码哪一行让图断了，不需要猜。

> **教学点**：`torch.compile` 不报错 ≠ 编译成功。**「能跑」和「真的编成一张图」是两件事**，必须看日志。

## Python 循环 vs 张量控制流

一个容易混淆的对照：

| 写法 | Dynamo 怎么处理 | 结果 |
|---|---|---|
| `for i in range(3): y = y*2` | 展开（循环次数编译期已知） | 一张图，3 个 `mul` |
| `for x in [t1, t2]: ...` | 展开（列表长度已知） | 一张图 |
| `while x.sum() < 10: ...` | 需要真实数值 → graph break | 多张图 |
| `if flag: ...`（`flag` 是 Python bool） | 展开成其中一个分支 | 一张图（另一个分支消失） |
| `if x.sum() > 0: ...` | 需要真实数值 → graph break | 多张图 |

关键判据：**这个条件在「追踪时」能不能决定？**

- 能（Python 常量、列表长度、`self.training`）→ 展开，图里只有一个分支
- 不能（依赖 tensor 数值）→ graph break

## 踩雷集锦

1. **以为「不报错就是编译成功了」**：`with_scalar` 能跑，但它断成了多张图。必须看 `TORCH_LOGS=graph_breaks`。
2. **把 `print()` 当无害**：`print(tensor)` 会触发 `Tensor.__repr__`，Dynamo 不理解，产生 graph break。调试用的 print 在性能测试前要删掉。
3. **用 `.item()` / `.tolist()` / `float()` 读数值**：全部是 graph break 来源。需要时用 `capture_scalar_outputs=True`，但要理解这会引入运行时依赖。
4. **忽略 guard 失败的成本**：每次 shape 变化都重编译。动态 shape 场景下，重编译开销可能超过优化收益。
5. **以为 `self.training` 是动态的**：Dynamo 把 `self.training` 当常量 guard。如果在同一张图上切 train/eval，会触发重编译。

## 本章重点整理

- 图不是解析出来的，是**跑一遍记录**出来的。
- 符号追踪的本质：tensor 被换成代理对象，操作被记录但不执行。
- 任何需要**真实数值**的地方都会 graph break。
- guard 是「图能安全重用」的依据；shape 变化 → guard 失败 → 重编译。
- `torch.compile` 不报错 ≠ 一张图。用 `TORCH_LOGS=graph_breaks` 验证。

## 自我检核

- [ ] 不看笔记，解释符号追踪为什么「不真正计算」
- [ ] 能说出 `float(x.sum())` 为什么会 graph break，以及日志里哪一行告诉你答案
- [ ] 知道 guard 检查了哪些东西，以及为什么 shape 变化会导致重编译
- [ ] 能区分「Python 循环」和「张量控制流」在 Dynamo 下的不同命运
- [ ] 能说出为什么「不报错」不足以证明编译成功

## 延伸阅读

### 官方文档

- **[torch.compiler 文档 — Troubleshooting](https://pytorch.org/docs/stable/torch.compiler_troubleshooting.html)**
  - **读哪里**：「Graph Breaks」与「Recompilations」两节
  - **为什么值得读**：本章日志解读的官方依据，还列了常见 graph break 模式
  - **前提**：先跑过本章例子，否则读不懂那些日志片段

- **[torch.compiler 文档 — Guards](https://pytorch.org/docs/stable/torch.compiler_guards.html)**
  - **读哪里**：`TENSOR_MATCH`、`SHAPE_ENV` 等 guard 类型的定义
  - **和本章的关联**：本章 guard 一节的权威说明

### 源码依据

- **[torch/_dynamo/](https://github.com/pytorch/pytorch/tree/main/torch/_dynamo)**
  - **读哪里**：`symbolic_convert.py` 里 `generic_jump` / `graph_break` 的处理；`guards.py`
  - **为什么值得读**：想知道 graph break 的精确判定条件，源码是唯一完整来源
  - **注意**：文件很大，配合 `TORCH_LOGS=graph_breaks` 的 traceback 定位到具体行

### 论文

- **[Torch.FX: Practical Program Capture and Transformation for Deep Learning in Python](https://arxiv.org/abs/2112.08429)** — Reed et al., MLSys 2022
  - **核心贡献**：符号追踪 + Python 级 IR 的设计论证
  - **读哪里**：Section 4（Symbolic Tracing）
  - **和本章的关联**：Dynamo 的追踪思路直接继承自 FX

- **[TorchDynamo: A Compiler Frontend for PyTorch](https://dev-discuss.pytorch.org/t/torchdynamo-an-experimental-pytorch-compiler-frontend/440)** — Ansel et al., 2022
  - **核心贡献**：首次系统公开 Dynamo 的「guard + 部分编译」设计
  - **读哪里**：整篇（不长），重点看 guard 与 graph break 的设计动机
  - **和本章的关联**：本章全部概念的原始出处

### 博客 / 工程文章

- **[PyTorch — Compile Graph Break 案例库](https://meta-pytorch.github.io/compile-graph-break-site/)**
  - **这系列说什么**：每个 `gbXXXX` 编号对应一种 graph break 模式，附修法
  - **读哪里**：搜你日志里出现的编号（本章例子是 `gb0124`）
  - **为什么值得读**：这是官方维护的、按现象索引的手册，比翻源码快得多

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版）
  - **读哪几章**：第 4 章（中间表示）——理解「图」作为一种 IR 的位置
  - **注意**：书里讲的是传统 IR（三地址码、SSA），不是 DL 计算图；用来建立 IR 的一般直觉

抓到了图，下一个问题是：**动态 shape 怎么办**。边缘设备最怕的就是运行时才知道的尺寸。

→ [Ch 5 动态 shape 与控制流](./05-dynamic-shape.md)
