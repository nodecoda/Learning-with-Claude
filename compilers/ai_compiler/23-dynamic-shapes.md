# Ch 23 动态 shape 与控制流：把「形状」从常量变成变量

> **目标**：看清 `torch.export` 的 `SymInt` 以什么形态存在；用 `tvm.tirx.Var` 建出真正的动态维度并观察生成代码的差异；亲手制造 `symbolic_trace` 与 `export` 对 `if x.sum() > 0` 的失败，再用 `torch.cond` 与 `torch.where` 各修一次。最后能判断何时特化、何时动态、何时 bucketing。

## 为什么需要这个？

前 22 章默认 shape 是常量，编译器因此能折叠下标、按固定 tile 切循环、静态分配 buffer、写死向量化宽度。但真实推理里 batch 随请求量变化，`seq_len` 运行期才知道，检测框的数量甚至依赖图像内容。编译器只认常量时，你只有三条路：

| 策略 | 做法 | 代价 | 适合 |
|---|---|---|---|
| 每 shape 特化 | 每个新 shape 编译一份 | 编译时间 × shape 数 | shape 集合小且稳定 |
| 完全动态 | 维度用符号变量，一份产物通吃 | 优化受限、需守卫/掩码 | shape 长尾 |
| Bucketing | 归到若干桶，桶内 padding | 少量浪费 + 少量编译 | 服务端最常见 |

先给结论：**动态 shape 不是「关掉优化」，而是把编译期决策换成运行期检查**。理解这句话，你就知道为什么动态版有时几乎不掉性能、有时掉一半。

> 实测环境：Windows 10 专业版，Intel i7-10510U（4 核 8 线程，AVX2），Python 3.11.12，torch 2.14.1+cpu，tvm 0.27.0.post1。计时均为单次会话样本，只看量级与趋势。

## 先建立直觉：一个 `n` 从 Python 走到机器码

```text
Python      x.shape[0] == 8            具体整数，可参与 Python 控制流
export IR   s77 : VR[1, 128]           符号整数 + 取值范围
TIR         n = T.int32()              入口处从 tensor shape 读出
LLVM/C      for (i = 0; i < n; ++i)    寄存器里的比较，每次都执行
```

静态时每一环都是常量：`i * 128 + j` 可折叠，tile 可写死，buffer 可静态分配，向量化可假设对齐。动态时 `n` 是变量，编译器仍能生成正确循环，但必须：

1. 在入口校验实参 shape 的一致性（`C.shape[0] == A.shape[0]`）；
2. 用 `n` 参与下标计算，乘法折叠不掉；
3. 分块时用 `ceildiv(n, tile)` 作上界，并在块内加谓词；
4. 放弃依赖「长度是常量」的优化，例如整段展开与静态对齐假设。

## 核心概念一：SymInt —— 形状的符号化

`torch.export` 用 `SymInt` 表示「还不知道、但受约束的整数」，约束由 `Dim` 声明。`Dim("batch", min=1, max=128)` 里的 `min`/`max` 是契约，不是建议：导出的程序只保证在这个区间内正确。区间越窄，编译器能做的假设越多。

注意 `forward` 里的 `h.view(x.shape[0], -1)`——它不是装饰，是逼出符号维的关键：

```python
import torch, torch.nn as nn
from torch.export import Dim

class M(nn.Module):
    def __init__(self):
        super().__init__()
        self.w = nn.Parameter(torch.randn(16, 32))
    def forward(self, x):
        h = torch.relu(x @ self.w)
        return h.view(x.shape[0], -1)       # 输出 shape 依赖符号 B

m = M().eval()
batch = Dim("batch", min=1, max=128)
ep = torch.export.export(m, (torch.randn(8, 16),),
                         dynamic_shapes={"x": {0: batch}})
print(ep.graph_module.code)
print("range_constraints:", ep.range_constraints)
for n in ep.graph_module.graph.nodes:
    print(f"{n.op:16s} {str(n.target):40s} val={n.meta.get('val')}")
```

实测（torch 2.14.1+cpu，节点列表有删节）：

```text
def forward(self, p_w, x):
    sym_size_int_1 = torch.ops.aten.sym_size.int(x, 0)
    matmul = torch.ops.aten.matmul.default(x, p_w);  x = p_w = None
    relu = torch.ops.aten.relu.default(matmul);  matmul = None
    view = torch.ops.aten.view.default(relu, [sym_size_int_1, -1]);  relu = sym_size_int_1 = None
    return (view,)

range_constraints: {s77: VR[1, 128]}
placeholder      x                  val=FakeTensor(..., size=(s77, 16))
call_function    aten.sym_size.int   val=s77
call_function    aten.matmul.default val=FakeTensor(..., size=(s77, 32))
call_function    aten.view.default   val=FakeTensor(..., size=(s77, 32))
```

三件事值得逐条读：

1. **符号维叫 `s77`，不叫 `batch`。** `batch` 只是你起的名字，export 内部分配符号（编号随会话变化），`range_constraints` 记录 `VR[1, 128]`。
2. **`aten.sym_size.int` 是真实节点。** 只有算子需要「具体长度」时才出现，读的是运行期 `x.size(0)`。
3. **`FakeTensor(..., size=(s77, 32))` 是类型。** 第一维是一个表达式而不是数字，后续 shape 推导都在符号表达式上做。

守卫在运行期执行。合法 shape 都能跑，越界当场失败（实测）：

```python
print(ep.module()(torch.randn(17, 16)).shape)   # torch.Size([17, 32])
print(ep.module()(torch.randn(128, 16)).shape)  # torch.Size([128, 32])
ep.module()(torch.randn(256, 16))               # AssertionError: Guard failed: x.size()[0] <= 128
```

这不是 bug，是契约在生效：**正确性靠「符号 + 守卫」，不靠「跑一遍看看」。** 反直觉的是，如果图里没有算子需要具体长度，代码文本会和静态版一模一样，动态性只存在于 meta 与 `range_constraints` 里——判断是否真动态，看 meta，不要看代码文本。

## 核心概念二：动态维在 TIR 里就是一个 Var

TVM 0.27 把 TIR 拆成 `tvm.s_tir`（调度）与 `tvm.tirx`（新 TIR）。用 `tvm.tirx.Var` 建维度，直接塞进 `te.placeholder`：

```python
import tvm
from tvm import te
n = tvm.tirx.Var("n", "int32")          # 运行期整数
A = te.placeholder((n, 64), name="A")
B = te.placeholder((64, 128), name="B")
k = te.reduce_axis((0, 64), name="k")
C = te.compute((n, 128), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

func = te.create_prim_func([A, B, C])
print(func)
```

实测 `print(func)`（省略两行 import 注释；注意 `T.Buffer(("n", 64), ...)` 和函数体里的 `n = T.int32()`）：

```text
@T.prim_func(s_tir=True)
def main(A: T.Buffer(("n", 64), "float32"), B: T.Buffer((64, 128), "float32"), C: T.Buffer((n, 128), "float32")):
    n = T.int32()
    T.func_attr({"tirx.noalias": True})
    for i, j, k in T.grid(n, 128, 64):
        with T.sblock("C"):
            v_i, v_j, v_k = T.axis.remap("SSR", [i, j, k])
            with T.init():
                C[v_i, v_j] = T.float32(0.0)
            C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
```

对比静态 `n=64`：静态版是 `T.Buffer((64, 64))`、`T.grid(64, 128, 64)`，没有 `n = T.int32()`。**动态版多出的那一行就是运行期参数。**

C 源码的差异更直观（`target="c"` 只看源码，不执行）：

```text
静态：  for (int32_t i = 0; i < 64; ++i) {
动态：  for (int32_t i = 0; i < n; ++i) {
```

这一个字符带来三笔账：

- **下标乘法折叠不掉**：`A[i*64 + k]` 里 `i` 是变量（LLVM 可能做强度削减，但不保证）。
- **shape 一致性检查进入产物**。实测报错：

```text
ValueError: Mismatched C.shape[0] on argument #2 when calling:
  `main(A: Tensor([n, 64], float32), B: Tensor([64, 128], float32), C: Tensor([n, 128], float32))`,
  expected to match A.shape[0]
```

- **调度器必须接受符号 extent**：`sch.split` 生成 `ceildiv` 和谓词，而不是常量因子。

纠正一个误解：动态不要求所有维都动态。上例 `K=64`、`N=128` 仍是常量，reduce 轴照常展开、tile、向量化。生产里通常只让 batch 动态——性价比最高。

## 核心概念三：图 IR 里没有 if，只有 cond 与 where

图是 DAG，没有「按值跳转」。Python 的 `if` 不能直接进图，四种处理方式命运完全不同。

**A. `symbolic_trace` 直接失败。** Proxy 一进 `if` 就触发布尔求值：

```python
from torch.fx import symbolic_trace
class Cond(nn.Module):
    def forward(self, x):
        if x.sum() > 0: return x * 2
        return x - 1
symbolic_trace(Cond())
```

```text
torch.fx.proxy.TraceError: symbolically traced variables cannot be used as inputs to control flow
```

**B. `torch.export` 也失败，`strict=False` 救不了。** 这是本章最值得记的坑：

```text
=== strict=True ===
torch._dynamo.exc.Unsupported: Data-dependent branching
  Explanation: Detected data-dependent branching (e.g. `if my_tensor.sum() > 0:`).
  Dynamo does not support tracing dynamic control flow.
  Hint: Use `torch.cond` to express dynamic control flow.

=== strict=False ===
torch.fx.experimental.symbolic_shapes.GuardOnDataDependentSymNode:
Could not guard on data-dependent expression Eq(u0, 1) (unhinted: Eq(u0, 1)).
```

`x.sum() > 0` 是**数据相关**的布尔量：由张量内容决定，不是 shape。要靠它选分支，编译器必须先「知道」这个值。两种模式的拒绝方式不同：`strict=True` 直接判定「不支持数据相关分支」；`strict=False` 试图加 guard，失败在 `Eq(u0, 1)` 这个「需要它为真」的条件上。本质相同——**编译器拒绝在没有证据时假设分支方向**，换成 `strict=False` 只是换了一种失败方式。

**C. `torch.cond` 把控制流变成高阶算子。** 两个分支各自被捕获成子图：

```python
class CondMod(nn.Module):
    def forward(self, x):
        return torch.cond(x.sum() > 0, lambda y: y * 2.0, lambda y: y - 1.0, (x,))

ep = torch.export.export(CondMod().eval(), (torch.randn(4, 8),))
print(ep.graph_module.code)
```

```text
def forward(self, x):
    sum_1 = torch.ops.aten.sum.default(x)
    gt = torch.ops.aten.gt.Scalar(sum_1, 0);  sum_1 = None
    cond = torch.ops.higher_order.cond(gt, self.true_graph_0, self.false_graph_0, (x,));  gt = x = None
    getitem = cond[0];  cond = None
    return (getitem,)
```

两个 `get_attr` 是真正的子 `GraphModule`：后端分别编译，运行期只执行一个——**真正的跳转**。

**D. `torch.where` 无分支改写。** 分支都很便宜时，都算出来再选：

```python
class WhereMod(nn.Module):
    def forward(self, x):
        return torch.where(x.sum() > 0, x * 2.0, x - 1.0)
```

`symbolic_trace` 实测图：`sum → gt → mul → sub → where`，纯数据节点，到处能跑。代价是**两条分支都会执行**。分支是逐元素小算子用 `where`，含 matmul/attention 用 `cond`。

## 底层机制：守卫、特化与 bucketing

把三块拼起来，动态 shape 的编译流程是：

```text
声明 Dim(min,max) → export 生成符号 s77 并记录 VR[1,128]
  → 图里出现 sym_size / view / cond 等节点 → 后端按符号 extent 生成代码
  → 运行期先过守卫 → 通过跑通用 kernel，不通过报错或重编译
```

**守卫失败时报错还是重编译，取决于入口。** `torch.export` 的产物是契约，越界直接 `AssertionError`；`torch.compile` 则默认给每个新 shape 生成一份特化图。用 Dynamo 计数器可以直接看到（`backend="eager"`，避免依赖本机缺失的 C++ 编译器）：

```python
import torch, torch._dynamo as dynamo
from torch._dynamo.utils import counters

def f(x, w): return torch.relu(x @ w)
W = torch.randn(32, 32)

def run(dynamic, sizes):
    dynamo.reset(); counters.clear()
    cf = torch.compile(f, dynamic=dynamic, backend="eager")
    for b in sizes: cf(torch.randn(b, 32), W)
    return {k: v for k, v in counters["stats"].items() if "graph" in k}

print(run(False, [8, 16, 24]))
print(run(True,  [8, 16, 24]))
```

```text
dynamic=False sizes 8,16,24: {'unique_graphs': 3}
dynamic=True  sizes 8,16,24: {'unique_graphs': 1}
dynamic=True  sizes 8,8,8   : {'unique_graphs': 1}
```

三个 shape、三份图；开动态后一份通吃。`dynamic=False` 下每个新尺寸都会重新捕获 + 编译，这是服务端 P99 抖动的常见来源。

**动态化的账要分开算：**

| 影响面 | 静态 | 动态 | 备注 |
|---|---|---|---|
| 下标表达式 | 编译期折叠 | 运行期计算 | 可做不变量提升 |
| 分块 / 向量化 | 常量 tile、假设对齐 | `ceildiv` + 谓词、运行期检查 | 尾块有掩码开销 |
| 内存规划 | 静态算峰值 | 按上界预留或动态分配 | 见 Ch 10 |
| 自动调优 | 每 shape 一套参数 | 按桶/上界调优 | 见 Ch 13 |
| 编译次数 | shape 数 | 1 | 单份产物可能更慢 |

TVM 的编译成本可量化。同一 matmul，静态编 6 个桶 vs 动态编 1 份（`tvm.build(target="llvm")`，单次会话）：

```text
static 6 builds total=487.4 ms, per-build avg=81.2 ms
dynamic 1 build      = 70.8 ms
```

6 个桶约 7 倍编译时间。运行期几乎没差别（同一 `n=8`，`number=200, repeat=7`）：

```text
static n=8: median=0.0949 ms min=0.0827 max=0.1103
dynamic:    median=0.0841 ms min=0.0779 max=0.1028
```

两者区间大面积重叠，**不要**把这解释成「动态更快」：小 kernel 上额外开销被访存和调用开销淹没，形状更大、循环更内层时差异才会显现。

这就是 bucketing 的动机：把无限多 shape 映射到有限个桶（1/2/4/8/16/32），桶内 padding。编译次数可控，kernel 仍拿常量 tile，代价是无效计算。经验规则：

- batch ≤ 8：逐 shape 特化，编译便宜、浪费为 0。
- batch 8–128：bucketing，桶间距 8 或 16。
- batch 大且长尾：动态维 + 按上界调优，接受通用 kernel。
- 序列长度：优先 bucketing，但 padding 有语义代价时要配 attention mask。

## 示例一：TVM 动态维度 + 动态 tiling（实测）

动态维一进调度，分块就必须带谓词。对 `i` 做 `split(factors=[None, 8])`：

```python
import tvm, numpy as np
from tvm import te

nb = tvm.tirx.Var("nb", "int32")
A = te.placeholder((nb, 64), name="A")
B = te.placeholder((64, 128), name="B")
k = te.reduce_axis((0, 64), name="k")
C = te.compute((nb, 128), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

func = te.create_prim_func([A, B, C])
sch = tvm.s_tir.Schedule(func)
i, j, kk = sch.get_loops(sch.get_sblock("C"))
io, ii = sch.split(i, factors=[None, 8])
sch.reorder(io, j, ii, kk)
f = tvm.build(sch.mod, target="llvm")

np.random.seed(3)
for m in (7, 19):
    a = tvm.runtime.tensor(np.random.randn(m, 64).astype("float32"), tvm.cpu())
    b = tvm.runtime.tensor(np.random.randn(64, 128).astype("float32"), tvm.cpu())
    c = tvm.runtime.empty((m, 128), "float32", tvm.cpu())
    f(a, b, c)
    print("n=", m, "maxerr", float(np.abs(c.numpy() - a.numpy() @ b.numpy()).max()))
```

调度后的 TIR 与 C（节选）：

```text
for i_0, j, i_1, k in T.grid((nb + 7) // 8, 128, 8, 64):
    with T.sblock("C"):
        v_i = T.axis.spatial(nb, i_0 * 8 + i_1)
        T.where(i_0 * 8 + i_1 < nb)

for (int32_t i_0 = 0; i_0 < ((nb + 7) >> 3); ++i_0) {
  for (int32_t j = 0; j < 128; ++j) {
    for (int32_t i_1 = 0; i_1 < 8; ++i_1) {
      if (((i_0 * 8) + i_1) < nb) {
        for (int32_t k = 0; k < 64; ++k) { ... }
```

数值验证：

```text
n= 7 maxerr 1.1444091796875e-05
n= 19 maxerr 5.7220458984375e-06
```

三点解读：

1. **外层上界 `(nb + 7) >> 3`** 即 `ceildiv(nb, 8)`，编译期算不出，只能运行期算。
2. **内层块有谓词**。`n=7` 时 8 个槽位有 1 个被跳过，最坏浪费 `tile-1` 行。
3. **谓词在 `k` 循环外**，只付一次判断。这也是动态 tiling 通常只慢几个百分点的原因。

若再给 `split` 加 `unroll`，迭代次数不再是常量，就没法整段展开。这是动态 shape 最典型的「优化被拿走」：不是不能展开，而是展开后必须带谓词，收益抵不过代码膨胀。

## 示例二：`if x.sum() > 0` 的四种命运（实测）

```python
import torch, torch.nn as nn
class Cond(nn.Module):
    def forward(self, x):
        if x.sum() > 0:
            return x * 2
        return x - 1
class CondMod(nn.Module):
    def forward(self, x): return torch.cond(x.sum() > 0, lambda y: y*2.0, lambda y: y-1.0, (x,))
class WhereMod(nn.Module):
    def forward(self, x): return torch.where(x.sum() > 0, x*2.0, x-1.0)
 # [1] symbolic_trace：失败
try: torch.fx.symbolic_trace(Cond())
except Exception as e: print("trace:", type(e).__name__, e)
 # [2] export strict=True / False：都失败
for strict in (True, False):
    try: torch.export.export(Cond().eval(), (torch.randn(4, 8),), strict=strict)
    except Exception as e: print("export strict=%s:" % strict, type(e).__name__)
 # [3] torch.cond：成功，图里有 higher_order.cond
ep = torch.export.export(CondMod().eval(), (torch.randn(4, 8),))
print([str(n.target) for n in ep.graph_module.graph.nodes])
 # [4] torch.where：成功，纯数据图
ep2 = torch.export.export(WhereMod().eval(), (torch.randn(4, 8),))
print([str(n.target) for n in ep2.graph_module.graph.nodes])
```

实测关键输出：

```text
trace: TraceError symbolically traced variables cannot be used as inputs to control flow
export strict=True: Unsupported
export strict=False: GuardOnDataDependentSymNode
['x', 'aten.sum.default', 'aten.gt.Scalar', 'true_graph_0', 'false_graph_0', 'cond', '<built-in function getitem>', 'output']
['x', 'aten.sum.default', 'aten.gt.Scalar', 'aten.mul.Tensor', 'aten.sub.Tensor', 'aten.where.self', 'output']
```

`torch.cond` 与动态 batch 可以共存：

```python
batch = Dim("batch", min=1, max=64)
ep3 = torch.export.export(CondMod().eval(), (torch.randn(4, 8),),
                          dynamic_shapes={"x": {0: batch}})
print(ep3.module()(torch.randn(13, 8)).shape)   # torch.Size([13, 8])
ep3.module()(torch.randn(99, 8))                # AssertionError: Guard failed: x.size()[0] <= 64
```

**控制流与动态 shape 是两个正交维度**，可以各自成功或失败，也可以组合。排错时先分清是「符号维不满足」还是「数据相关分支无法捕获」——修法完全不同。

## 对比与取舍

| 方案 | 图里长什么样 | 运行期 | 编译次数 | 适用 |
|---|---|---|---|---|
| 静态特化 | 常量 shape | 无守卫 | 每 shape 一次 | shape 集合小 |
| 动态维 | `sym_size` + 符号 meta | shape 守卫 | 1 | batch 长尾 |
| Bucketing | 桶内常量 | padding + 可能重编译 | 桶数 | 服务端主流 |
| `torch.where` | `aten.where` | 两分支都算 | 1 | 分支是逐元素 |
| `torch.cond` | `higher_order.cond` | 只跑一个分支 | 1（两子图） | 分支含大算子 |

三条经验：

1. **先量 shape 分布。** 95% 请求 batch ≤ 16 时，1/2/4/8/16 的桶就够，动态化是过度设计。
2. **只动态真正变化的维度。** batch 动态、`K/N` 静态，是多数模型的甜点；优先 `where`，不得已才 `cond`。
3. **把守卫当 API 契约写进测试。** 至少测 `min`、`max`、`max+1`，最后一个必须失败。

## 踩雷集锦

1. **以为 `strict=False` 能救数据相关 `if`**：实测照样 `GuardOnDataDependentSymNode`。
2. **`dynamic_shapes` 的 key 写错**：`{"x": ...}` 必须匹配 `forward` 形参名；用元组形式时索引要对齐。
3. **`Dim(max=...)` 写成理论最大值**：等于放弃静态内存规划；按 P99 写，超出走 fallback。
4. **只跑 `ep.module()` 就宣布支持动态**：那是解释执行，编译产物（Inductor/TVM）可能有额外限制。
5. **在 `forward` 里写 `if x.shape[0] > 1`**：这是 shape 相关控制流，很多后端不支持符号分支；能改 `where` 就改。
6. **TVM 里写 `tvm.tir.Var`**：0.27 已拆成 `tvm.tirx.Var`，照抄旧教程会踩 API 不存在的坑。

## 进阶：再往深一层

- **看 Inductor 的符号表达式**：`TORCH_LOGS="dynamic"` 打印 guard 与求解过程；本机 Inductor 代码生成不可用（实测 `InvalidCxxCompiler: Compiler: cl is not found.`），Dynamo 日志仍可用。
- **动态 shape 下的自动调优**：含常量 tile 的搜索空间只能按桶调优；要调符号 extent，tile 也得参数化（Ch 13）。
- **Padding 的语义代价**：attention 里 pad 到桶边界会改变 softmax 归一化，必须配 mask，编译器不会替你修。

## 动手练习

### 练习 1：测出你机器上的 recompile 曲线

**任务**：用 `torch.compile(backend="eager")` 跑 batch ∈ {1,2,4,8,16,32}，记录 `dynamic=False/True` 的 `unique_graphs`；再跑一组重复 shape（8,8,8）确认不增长。

**验收**：`False` 得 6，`True` 得 1；重复 shape 不增加计数。

<details>
<summary>参考解答</summary>

```python
import torch, torch._dynamo as dynamo
from torch._dynamo.utils import counters

def f(x, w): return torch.relu(x @ w)
W = torch.randn(32, 32)
def run(dynamic, sizes):
    dynamo.reset(); counters.clear()
    cf = torch.compile(f, dynamic=dynamic, backend="eager")
    for b in sizes: cf(torch.randn(b, 32), W)
    return counters["stats"].get("unique_graphs")

print(run(False, [1,2,4,8,16,32]))   # 6
print(run(True,  [1,2,4,8,16,32]))   # 1
print(run(False, [8,8,8]))           # 1
```

</details>

### 练习 2：给动态 matmul 加 tile 并验证尾块

**任务**：把示例一的 `factors=[None, 8]` 改成 `[None, 16]`，用 `n ∈ {16, 17, 31, 33}` 验证数值，并算出 `n=17` 浪费多少行。

**验收**：四个 `maxerr` 都在 `1e-5` 量级；能说出 `n=17` 浪费 15 行。

<details>
<summary>参考解答</summary>

`ceildiv(17,16)=2`，共 32 行槽位，有效 17 行，浪费 15 行。谓词 `i_0*16 + i_1 < n` 保证不越界。**常见卡点**：只测 `n=16`（谓词恒真）发现不了 bug；忘了 `sch.reorder` 导致循环顺序与预期不符。
</details>

### 练习 3：设计一套 bucketing 方案

**任务**：给定分布 `{1:40%, 2:25%, 4:15%, 8:10%, 16:5%, 32:5%}`，设计桶集合，使编译份数 ≤ 4 且加权 padding 浪费 < 15%。

**验收**：给出桶集合与计算；说明为什么不能只用 `{1, 32}`。

<details>
<summary>参考解答</summary>

浪费率 = `(桶 - 实际) / 桶 × 占比`。

- `{1, 2, 4, 32}`：8→32 浪费 75% × 10% = 7.5%；16→32 浪费 50% × 5% = 2.5%；合计 **10%**，达标。
- `{1, 2, 16, 32}`：4→16 浪费 75% × 15% = 11.25%；8→16 浪费 5%；合计 **16.25%**，超标。
- 只用 `{1, 32}`：2/4/8/16 全 pad 到 32，加权浪费约 **46.6%**，且小 batch 被 32 行的 kernel 拖慢——浪费按算力算，延迟按最大桶算。

32 必须单独成桶，否则只能走动态 fallback，「4 桶」的前提被破坏。
</details>

## 本章重点整理

- 动态 shape 的本质是**把编译期常量换成带约束的符号**，正确性由「符号 + 守卫」保证。
- export 里符号维表现为 `FakeTensor(size=(s77, ...))`、`aten.sym_size.int` 和 `range_constraints`；代码文本可能看不出动态性。
- TVM 0.27 用 `tvm.tirx.Var`，PrimFunc 多出 `n = T.int32()`，C 循环上界从常量变成 `n`，并自动生成 shape 一致性检查。
- 动态 `split` 生成 `ceildiv` 上界与块内谓词；谓词在 `k` 循环外，代价通常可控。
- `symbolic_trace` 无法处理数据相关 `if`；`export` 的 `strict=True` 抛 `Unsupported`，`strict=False` 抛 `GuardOnDataDependentSymNode`。
- 控制流两条正路：`torch.cond`（只跑一个分支）与 `torch.where`（两分支都算）。
- 动态化换「一次编译」，代价是下标计算、谓词、向量化与内存规划受限；bucketing 是工程折中。

## 自我检核

- [ ] 我能解释 `s77`、`VR[1, 128]`、`FakeTensor(size=(s77, 32))` 的关系
- [ ] 我能写出让图里出现 `sym_size.int` 的 `export` 代码
- [ ] 我知道动态维在 TVM 0.27 里用 `tvm.tirx.Var`，并能指出 PrimFunc 里的 `n = T.int32()`
- [ ] 我能解释动态 tiling 为什么生成 `(n + 7) >> 3` 与 `if (... < n)`
- [ ] 我能复现两种控制流失败并说出报错类型
- [ ] 我能用 `torch.cond` 和 `torch.where` 各改写一次，并说清代价

## 延伸阅读

- **torch.export — dynamic shapes**（https://pytorch.org/docs/stable/export.html）：读 `Dim`、`dynamic_shapes`、`range_constraints`。与本章关联：SymInt 的权威定义。
- **torch.cond 文档**（https://pytorch.org/docs/stable/generated/torch.cond.html）：读分支签名与同 shape/dtype 约束。与本章关联：控制流捕获的正确写法。
- **TVM 文档**（https://tvm.apache.org/docs/）：查 `tvm.tirx.Var` 与符号 `Buffer`。与本章关联：示例一的 API 依据。

---

→ [Ch 24 并行与分布式：通信也是一种算子](./24-parallelism.md)
