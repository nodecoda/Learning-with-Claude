# Ch 30 端到端复盘：一个 transformer block 的完整链路

> **目标**：把前 29 章串起来。亲手构造一个最小 transformer block（batch=1、seq=16、d=64），依次导出 FX 图、做常量折叠与融合、用 TVM 编译 attention 与 MLP 的关键 kernel，最后把结果拼回整块并做数值对齐。每一步都贴真实 IR/图变化。

## 为什么需要这个？

前面的章节是“分科教学”：Ch 2 讲图 IR，Ch 7 讲融合，Ch 12 讲 TIR，Ch 18 讲 Inductor，Ch 21 讲 MLIR。真实项目里这些知识必须同时出现：

- 一个 `nn.Linear` 在图里是一个节点，在 TIR 里是几百次乘加，在 runtime 里是一次 buffer 传参。
- 一个 `softmax` 在图里是一个节点，在编译器里是 max/sum/exp 三个 reduction 块。
- 一个 `residual add` 看起来无害，但拼错一次，数值就会差 0.25。

这一章不再引入新概念，只做一件事：**拿一个真实的 block，走完整条链路，看看每个概念落在哪**。实验全部在本机 CPU 上真跑，数字是实测。

## 先建立直觉：一个 block 的五个边界

我们用的 block 是标准 pre-norm transformer：

```text
x ──► LayerNorm ──► QKV Linear ──► Attention ──► Output Linear ──► + x
 │                                                                  │
 └──────────────────────── residual ───────────────────────────────┘
 │
 └──► LayerNorm ──► FC1 ──► GELU ──► FC2 ──► + x
```

五个边界，每个边界都是一次“编译器决策”：

| 边界 | 张量形状 | 编译器要决定什么 |
|---|---|---|
| LayerNorm | `[1,16,64]` → `[1,16,64]` | reduction 怎么切、mean/var 能不能融合 |
| QKV | `[1,16,64]` → `[1,16,192]` | 一个 GEMM 还是三个；权重怎么排 |
| Attention | `[1,4,16,16]` | `QK^T`/softmax/`AV` 的融合边界 |
| Output proj + residual | `[1,16,64]` | epilogue 能不能吃掉 residual |
| MLP | `[1,16,128]` | matmul + GELU 的 epilogue 融合 |

注意：**不是每个边界都能融**。softmax 是 reduction，residual 是 elementwise；它们的融合规则完全不同。这就是本章要观察的东西。

## 核心概念一：QKV 是一个 Linear，不是三个

模型里通常写：

```python
self.qkv = nn.Linear(d, 3 * d, bias=False)
```

它在 FX 图里只有一个 `call_module` 节点，输出 `[1,16,192]`。然后 `chunk(3, dim=-1)` 切成 q/k/v，每个 `[1,16,64]`。

到了 kernel 层，有三种落法：

1. **一个 GEMM**：`[16,64] @ [64,192]`，输出三块连续内存。
2. **三个 GEMM**：q/k/v 各一个 `[16,64] @ [64,64]`，权重是同一块 `[192,64]` 的三个切片。
3. **batched GEMM**：把三个切片当成 batch=3。

本实验的 TVM 部分没有编译 QKV（它留在 torch 里），但 attention 的 `bmm` 展示了同一类 kernel 的形状：`[4,16,16] @ [4,16,16]`。**图里“一个 Linear”不等于 kernel 里“一个 GEMM”**，这是图 IR 与循环 IR 的第一处信息丢失。

另一个细节：`nn.Linear` 的权重布局是 `[out_features, in_features]`，即 `[192,64]`。所以 QKV 的 GEMM 实际是 `x @ W.T`。FX 图里看不到这个转置，它藏在 `call_module` 的实现里；到了 TVM，你必须显式决定 B 的布局（本章用 `B[h,j,k]`，即已经转置好的 `[N,K]`）。

## 核心概念二：Attention 是两个 matmul 加一个 reduction

Attention 的数学：

```text
scores = (Q @ K^T) * scale          # [1,4,16,16]
probs  = softmax(scores, dim=-1)    # [1,4,16,16]
ctx    = probs @ V                  # [1,4,16,16]
```

在编译器里，这是三个不同的东西：

- `Q @ K^T`：batched matmul，4 个 head，每个 `16×16×16`。
- `softmax`：对最后一维做 max/sum/exp，是 **reduction**。
- `probs @ V`：又一个 batched matmul。

融合边界很敏感：

- 把 `scale` 融进 softmax 是安全的：`softmax(x * 0.25)` 和先乘再 softmax 等价（本章就是这么做的）。
- 把 softmax 融进下一个 matmul 通常不合适：softmax 的归一化需要完整的行，必须先物化 `probs`。
- 把 `QK^T` 和 softmax 融成一个 kernel 是可能的，但需要把 reduction 放进 matmul 的 epilogue；TVM 里要手写 TIR，Inductor 里要看 Scheduler 是否愿意。

本章的 TVM softmax 用了四个 block：`mx`、`ex`、`sm`、`Y`。这不是“实现啰嗦”，而是 reduction 的真实结构：先求行最大值，再 exp，再求和，最后除。Ch 12 的 `sblock/axis` 在这里全部用上了。

## 核心概念三：MLP 是 matmul 加 elementwise epilogue

MLP 部分：

```text
h = GELU(FC1(x))     # [1,16,64] -> [1,16,128] -> [1,16,128]
y = FC2(h) + x       # [1,16,128] -> [1,16,64]
```

`FC1` 是一个 `[16,64] @ [64,128]` 的 matmul，`GELU` 是 elementwise，`FC2` 是 `[16,128] @ [128,64]`。这里最适合演示 **epilogue 融合**：把 `GELU` 融进 `FC1` 的输出，省掉一次 `[1,16,128]` 的写回和读取。

FX 图里，`FC1` 是 `call_module`，`GELU` 是 `call_function`。我们的融合 pass 把它们替换成一个 `linear_gelu(x, weight, bias)` 调用。数值完全一致（都调用同一个 torch 实现），但图上的 kernel 边界少了一个。

## 底层机制：FX 到 TVM 的边界

FX 图是 torch 算子，TVM 吃的是 numpy/tensor + TIR。中间有几件事必须手动做：

| 边界 | 要做什么 | 为什么 |
|---|---|---|
| dtype | 全部 `float32` | TVM kernel 按 float32 编译 |
| 内存 | `np.ascontiguousarray` | TVM 假设连续 buffer |
| 布局 | q/k/v `[1,16,64] → [1,4,16,16]` | head 维在前，方便 bmm |
| B 的转置 | bmm 用 `B[h,j,k]` | kernel 里 K 是最后一维 |
| 输出 | `tvm.runtime.empty(shape, dtype, cpu)` | TVM 不返回新 tensor，写入传入 buffer |

本章只把 **attention 的 bmm/softmax、FC1、GELU** 交给 TVM；LayerNorm、output proj、FC2 留在 torch。这不是偷懒，而是刻意展示“部分编译”：真实系统也常常只把热点子图交给编译器，其余回退框架。

## 示例一：构造 block 并导出 FX 图

完整脚本在 `.scratch\ai-compiler\ch30\e2e.py`。核心模型：

```python
class TinyBlock(nn.Module):
    def __init__(self, d=64, h=4, ffn=128):
        super().__init__()
        self.d, self.h, self.dh, self.ffn = d, h, d // h, ffn
        self.qkv = nn.Linear(d, 3 * d, bias=False)
        self.proj = nn.Linear(d, d, bias=False)
        self.ln1_w = nn.Parameter(torch.ones(d))
        self.ln1_b = nn.Parameter(torch.zeros(d))
        self.fc1 = nn.Linear(d, ffn)
        self.fc2 = nn.Linear(ffn, d)
        self.ln2_w = nn.Parameter(torch.ones(d))
        self.ln2_b = nn.Parameter(torch.zeros(d))
        self.register_buffer("d_tensor", torch.tensor(float(self.dh)))

    def norm(self, x, w, b):
        mu = x.mean(-1, keepdim=True)
        var = ((x - mu) ** 2).mean(-1, keepdim=True)
        xn = (x - mu) * torch.rsqrt(var + 1e-5)
        return xn * w + b

    def forward(self, x):
        h = self.norm(x, self.ln1_w, self.ln1_b)
        qkv = self.qkv(h)
        q, k, v = qkv.chunk(3, dim=-1)
        B, T, _ = q.shape
        q = q.reshape(B, T, self.h, self.dh).permute(0, 2, 1, 3)
        k = k.reshape(B, T, self.h, self.dh).permute(0, 2, 1, 3)
        v = v.reshape(B, T, self.h, self.dh).permute(0, 2, 1, 3)
        scale = torch.rsqrt(self.d_tensor)
        att = (q @ k.transpose(-2, -1)) * scale
        att = torch.softmax(att, dim=-1)
        att = att @ v
        att = att.permute(0, 2, 1, 3).reshape(B, T, self.d)
        x = x + self.proj(att)
        h = self.norm(x, self.ln2_w, self.ln2_b)
        h = torch.nn.functional.gelu(self.fc1(h))
        x = x + self.fc2(h)
        return x
```

`fx.symbolic_trace(m)` 的真实图（节选）：

```python
mean = x.mean(-1, keepdim = True)
sub = x - mean
pow_1 = sub ** 2
mean_1 = pow_1.mean(-1, keepdim = True)
add = mean_1 + 1e-05
rsqrt = torch.rsqrt(add)
...
qkv = self.qkv(add_1)
chunk = qkv.chunk(3, dim = -1)
getattr_1 = getitem.shape
getitem_3 = getattr_1[0]
reshape = getitem.reshape(getitem_3, getitem_4, 4, 16)
...
matmul = permute @ transpose
_tensor_constant0 = self._tensor_constant0
mul_2 = matmul * _tensor_constant0
softmax = torch.softmax(mul_2, dim = -1)
matmul_1 = softmax @ permute_2
...
fc1 = self.fc1(add_4)
gelu = torch._C._nn.gelu(fc1)
fc2 = self.fc2(gelu)
add_5 = add_2 + fc2
```

本机实测节点数：

```text
FX nodes before passes: 55 (compute nodes: 48)
```

注意 `_tensor_constant0`：FX 在 trace 时已经把 `torch.rsqrt(self.d_tensor)` 算成了 `tensor(0.2500)`。这是 **tracer 自带的常量折叠**，不是我们写的 pass。`head_dim=16`，`1/sqrt(16)=0.25`，这个常量后面会直接进 TVM softmax。

## 示例二：常量折叠与融合

我们的 pass 做两件事：

1. `fold_static_metadata`：把 `shape` 查询和 `getitem` 折成字面量。
2. `fuse_scaled_softmax` / `fuse_linear_gelu`：把两个子图替换成单节点。

真实输出：

```text
folded nodes: [('getattr_1', (1, 16, 64)), ('getitem_3', 1), ('getitem_4', 16), ('getitem_5', 64)]
FX nodes after folding: 51
fused scaled-softmax: 1
fused linear+gelu: 1
FX nodes after fusion: 51 (compute nodes: 42)
FX passes vs eager: maxerr=0.000e+00
```

为什么总节点数还是 51？因为 `linear_gelu` 融合引入了两个 `get_attr`（weight/bias），而 `call_module + gelu` 被删掉两个，净变化为 0。看 **compute nodes** 更准确：48 → 42。图节点数不是融合收益的唯一指标。

折叠后的关键变化：

```python
# before
reshape = getitem.reshape(getitem_3, getitem_4, 4, 16)
# after
reshape = getitem.reshape(1, 16, 4, 16)
```

融合后的关键变化：

```python
scaled_softmax = __main___scaled_softmax(matmul, _tensor_constant0, dim = -1)
...
fc1_weight = self.fc1.weight
fc1_bias = self.fc1.bias
linear_gelu = __main___linear_gelu(add_4, fc1_weight, fc1_bias)
```

（函数名前缀取决于模块名；直接跑脚本时是 `__main___...`，被 import 时可能是 `e2e_...`。）

融合函数本身很短：

```python
def scaled_softmax(x, scale, dim=-1):
    return torch.softmax(x * scale, dim=dim)

def linear_gelu(x, weight, bias):
    return torch.nn.functional.gelu(torch.nn.functional.linear(x, weight, bias))
```

数值对齐 `maxerr=0.000e+00` 是因为融合前后调用的是同一组 torch 算子；这证明**图改写没有改变语义**，但不证明“融合会更快”。速度快不快，要看 TVM 生成的是不是一个 kernel。

## 示例三：TVM 编译三个 kernel

### 1. batched matmul

```python
def build_bmm(H, M, N, K):
    A = te.placeholder((H, M, K), name="A", dtype="float32")
    B = te.placeholder((H, N, K), name="B", dtype="float32")
    kk = te.reduce_axis((0, K), name="k")
    C = te.compute((H, M, N), lambda h, i, j: te.sum(A[h, i, kk] * B[h, j, kk], axis=kk), name="C")
    func = te.create_prim_func([A, B, C])
    sch = s_tir.Schedule(func)
    blk = sch.get_sblock("C")
    loops = sch.get_loops(blk)
    ko, ki = sch.split(loops[3], factors=[4, None])
    sch.reorder(loops[0], loops[1], loops[2], ko, ki)
    sch.vectorize(loops[2])
    return tvm.build(sch.mod, target="llvm"), func, sch
```

调度后的 TIR（实测）：

```python
for h, i in T.grid(4, 16):
    for j in T.vectorized(16):
        for k_0, k_1 in T.grid(4, 4):
            with T.sblock("C"):
                v_h, v_i, v_j = T.axis.remap("SSS", [h, i, j])
                v_k = T.axis.reduce(16, k_0 * 4 + k_1)
                T.reads(A[v_h, v_i, v_k], B[v_h, v_j, v_k])
                T.writes(C[v_h, v_i, v_j])
                with T.init():
                    C[v_h, v_i, v_j] = T.float32(0.0)
                C[v_h, v_i, v_j] = C[v_h, v_i, v_j] + A[v_h, v_i, v_k] * B[v_h, v_j, v_k]
```

`j` 被向量化，`k` 被 split 成 `4×4`。注意：**不能向量化 reduction 轴**。如果把 `vectorize` 指向 `k_0`，TVM 会报 `ScheduleError`，因为 reduction 轴不是 data-parallel。这个错误在 Ch 12 讲过，本章再遇到一次。

### 2. softmax

```python
def build_softmax(H, T, scale=0.25):
    X = te.placeholder((H, T, T), name="X", dtype="float32")
    r1 = te.reduce_axis((0, T), name="r1")
    mx = te.compute((H, T), lambda h, i: te.max(X[h, i, r1], axis=r1), name="mx")
    ex = te.compute((H, T, T), lambda h, i, j: te.exp(X[h, i, j] * scale - mx[h, i]), name="ex")
    r2 = te.reduce_axis((0, T), name="r2")
    sm = te.compute((H, T), lambda h, i: te.sum(ex[h, i, r2], axis=r2), name="sm")
    Y = te.compute((H, T, T), lambda h, i, j: ex[h, i, j] / sm[h, i], name="Y")
    func = te.create_prim_func([X, Y])
    return tvm.build(func, target="llvm"), func
```

真实 TIR（节选）：

```python
for h, i, r1 in T.grid(4, 16, 16):
    with T.sblock("mx"):
        ...
for h, i, j in T.grid(4, 16, 16):
    with T.sblock("ex"):
        ...
        ex[v_h, v_i, v_j] = T.exp(X[v_h, v_i, v_j] * T.float32(0.25) - mx[v_h, v_i])
for h, i, r2 in T.grid(4, 16, 16):
    with T.sblock("sm"):
        ...
for h, i, j in T.grid(4, 16, 16):
    with T.sblock("Y"):
        ...
```

四个 block 对应四步：求最大值、exp、求和、归一化。`T.float32(0.25)` 就是 FX 折叠出来的 scale，直接进了 TIR。

### 3. GELU

```python
def build_gelu(B, T, Fd):
    X = te.placeholder((B, T, Fd), name="X", dtype="float32")
    Y = te.compute(
        (B, T, Fd),
        lambda b, t, f: 0.5 * X[b, t, f] * (1.0 + te.erf(X[b, t, f] / te.sqrt(2.0))),
        name="Y",
    )
    return tvm.build(te.create_prim_func([X, Y]), target="llvm")
```

真实 TIR：

```python
for b, t, f in T.grid(1, 16, 128):
    with T.sblock("Y"):
        Y[v_b, v_t, v_f] = T.float32(0.5) * X[v_b, v_t, v_f] * (
            T.float32(1.0) + T.erf(X[v_b, v_t, v_f] / T.sqrt(T.float32(2.0)))
        )
```

这是一条纯 elementwise 链，没有 reduction，所以一个 block 就够。

### 数值结果

本机实测（float32，CPU，单次运行）：

```text
TVM bmm vs torch:       maxerr=4.768e-07
TVM softmax vs torch:   maxerr=3.725e-08
TVM ctx vs torch:       maxerr=1.192e-07
TVM fc1 matmul vs torch:maxerr=3.576e-07
TVM gelu vs torch:      maxerr=4.172e-07
```

都远小于 `1e-5`。误差来源是累加顺序不同（TVM 标量/向量循环 vs torch 的 BLAS/融合实现），不是语义错误。

## 示例四：拼回整个 block

把 TVM 的 attention 结果和 FC1/GELU 拼回 torch 的其余部分：

```python
ctx_t = torch.from_numpy(ctx.numpy()).permute(1, 0, 2).reshape(1, T, 64)
x2 = x + m.proj(ctx_t)
h2 = m.norm(x2, m.ln2_w, m.ln2_b)
fc1 = tvm_fc1(h2)
g = tvm_gelu(fc1 + m.fc1.bias)
out_tvm = x2 + m.fc2(torch.from_numpy(g.numpy()))
```

本机实测：

```text
full block (TVM attn+fc1+gelu) vs torch: maxerr=2.980e-07
```

这就是“部分编译”的完整闭环：**TVM 只负责它能负责的 kernel，其余回退 torch；数值对齐用 `allclose` 判定，不用 `==`。**

这里有一个真实踩过的坑：第一版把 `out_tvm` 写成 `x + m.fc2(g)`，漏掉了 `x2 = x + proj(...)` 里的 residual，误差 0.2596。残差流是 `add_2 + fc2`，不是 `x + fc2`。**transformer 的 residual 不是装饰，拼错一次数值就废。**

## 复盘表：每个概念落到什么

| 概念 | 数学 | FX 图里的样子 | TVM 落点 | 优化/边界 |
|---|---|---|---|---|
| QKV | `x @ W_qkv` | 一个 `call_module` + `chunk` | 本实验留在 torch；可用一个 `[16,64]@[64,192]` GEMM | 图里一个算子，kernel 里可能三个 |
| Attention | `QK^T` | `matmul` + `transpose` | bmm TIR：`4×16×16×16`，`j` 向量化，`k` split | 不能向量化 reduction |
| Softmax | `exp/sum` | `softmax` 一个节点 | 四个 block：max/exp/sum/div | reduction 边界，scale 可融合 |
| Context | `probs @ V` | `matmul` | 同一个 bmm，B 用 `[N,K]` 布局 | 传错布局就错 |
| MLP FC1 | `h @ W1^T + b1` | `call_module fc1` | bmm：`1×16×128×64`，再加 bias | bias 可在 torch 加，也可融进 epilogue |
| GELU | `0.5x(1+erf(x/√2))` | `call_function gelu` | 单 block elementwise | 可融进 FC1 epilogue |
| Residual | `x + f(x)` | `add` | 本实验留在 torch | 可融进 matmul epilogue |
| LayerNorm | `(x-μ)/σ*w+b` | mean/sub/pow/mean/rsqrt/mul/add | 本实验留在 torch | reduction + elementwise，融合要小心 |

一个结论：**概念在编译器里不会保持原名**。`attention` 变成两个 bmm + 一个 softmax；`MLP` 变成两个 matmul + 一个 elementwise；`residual` 变成 add。看懂这张表，才算把 Ch 2–17 串起来。

## 与 MLIR 的 linalg/attention 视角对照

同样一个 block，MLIR 视角会写成 `linalg.matmul` → `linalg.generic`（softmax 的 max/sum/exp）→ `linalg.matmul`，再 bufferize 成 `memref` + `scf.for`；TVM 视角是 bmm TIR + 四个 softmax block + bmm TIR。两者都在 reduction 边界切开，只是 MLIR 用 indexing maps/iterator types 表达，TVM 用 `sblock/axis` 表达。`../mlir_core/` 课程里的 linalg 例子可以直接和本章的 TIR 对照看。

## 全课回顾：37 个文件 → 6 个 Part → 一条主线

课程按 6 个 Part 组织：

| Part | 文件 | 数量 | 主线 |
|---|---|---|---|
| Part 0 | Ch 0–4 + 练习 A | 6 | 心智模型与最小闭环 |
| Part 1 | Ch 5–10 + 练习 B | 7 | 图级优化：折叠、融合、布局、量化、内存 |
| Part 2 | Ch 11–17 + 练习 C | 8 | 调度、codegen、runtime |
| Part 3 | Ch 18–22 + 练习 D | 6 | 生产栈对照 |
| Part 4 | Ch 23–27 | 5 | 动态 shape、并行、低精度、正确性、性能 |
| Part 5 | Ch 28–30 + Final | 4 | 源码、设计取舍、端到端复盘 |

全课共 **37 个 Markdown**：README + 31 章（Ch 0–30）+ 4 个练习 + Final。上表的 36 个是章节/练习/Final，README 单列。

一条主线：

```text
语义（Ch 1–4）
  → 图 IR（Ch 2）
  → 图级改写（Ch 5–10）
  → 调度与 TIR（Ch 11–13）
  → codegen 与 runtime（Ch 14–17）
  → 生产栈对照（Ch 18–22）
  → 专项边界（Ch 23–27）
  → 源码与设计（Ch 28–29）
  → 端到端复盘（Ch 30）
  → mini compiler（Final）
```

如果只记一句话：**AI 编译器不是“把模型翻译成机器码”，而是“在每一层选择保留什么信息、放弃什么信息，并为这个选择负责”。**

## 对比与取舍

这个实验证明了什么：

- FX 图能承载一个真实的 transformer block，节点级改写（折叠/融合）可验证、可回退。
- TVM 能编译其中的 matmul/softmax/elementwise，数值对齐在 `1e-5` 以内。
- 部分编译是可行的：不需要把整个 block 都交给 TVM。

这个实验没有证明什么：

- **没有证明 TVM 比 torch 快**。本实验没有做性能对比；小 shape 上 kernel launch/包装开销可能占主导。
- **没有证明融合一定更快**。`scaled_softmax` 在 FX 层是一个节点，但 TVM 里仍是四个 block；真正的融合要看 TIR schedule。
- **没有覆盖动态 shape、量化、GPU**。这些在 Ch 9、Ch 23、Ch 19 单独处理，GPU 路径标 `guided`。

## 踩雷集锦

1. **residual 加错**：`x + fc2` 而不是 `x2 + fc2`，误差 0.2596。先画 residual 流再写代码。
2. **bmm 的 B 布局**：`build_bmm` 用 `B[h,j,k]`，传 `v` 时要转置成 `[N,K]`；传原布局会得到错误结果但不报错。
3. **softmax 的 scale**：TVM softmax 里已经乘了 0.25，torch 参考也要乘；否则 maxerr 看起来很大。
4. **以为 FX 没做常量折叠**：`torch.rsqrt(buffer)` 在 trace 时已经变成 `_tensor_constant0`；你的 pass 只需要处理剩下的。
5. **`torch.from_numpy` 共享内存**：TVM 输出 buffer 被复用时，torch 侧可能看到变化；需要复制时用 `.clone()`。
6. **用 `==` 比较浮点**：用 `allclose(atol=1e-5, rtol=1e-5)`，并报告 maxerr。

## 进阶：再往深一层

### 1. 用 Relax 编译整个 block

本章只编译了子图。下一步是把 FX → Relax（Ch 2 的对照）→ `relax.build`，让 Relax 自己决定融合。你会看到 `FuseOps` 分组和 TIR 的对应关系。

### 2. 加动态 batch

把 `batch=1` 改成符号维，重复本章实验。观察哪些 kernel 需要重编译、哪些可以复用；这就是 Ch 23 的主题。

### 3. 做一次性能对比

用 `time_evaluator` 测 TVM kernel，用 `torch.utils.benchmark` 测 torch，分开报告首次/稳态。注意 Ch 27 的基准规范：多轮、交替、看比值。

## 动手练习

### 练习 1：把 seq 改成 32

改 `T=32`，重新跑整章。回答：哪些 shape 变了？softmax 的 reduction 长度变了吗？bmm 的哪个维度变了？

<details>
<summary>参考思路</summary>

`[1,32,64]`，heads 仍是 4，`dh=16`，scores `[1,4,32,32]`。bmm 的 M/N 变成 32，K 仍是 16；softmax reduction 长度变成 32。TIR 里的 `T.grid(4,32,32,16)` 会相应变化。
</details>

### 练习 2：解释“compute nodes 48 → 42”

数一数融合前后哪些节点消失了、哪些新增了。回答：为什么总节点数没变，但 compute nodes 少了 6 个？

<details>
<summary>参考思路</summary>

折叠删掉 4 个 shape 节点；`scaled_softmax` 融合删 2 加 1（净 -1）；`linear_gelu` 删 2 加 3（净 +1，因为新增两个 get_attr）。总节点 51；compute nodes 只数 call_function/call_module/call_method，不算 get_attr，所以从 48 降到 42。
</details>

## 本章重点整理

- 一个 transformer block 可以拆成五个边界：norm、QKV、attention、proj/residual、MLP。
- QKV 在图里是一个 Linear，在 kernel 里可能是一个 GEMM 或三个。
- Attention 是两个 bmm + 一个 reduction；softmax 不能随便融进下一个 matmul。
- MLP 适合做 matmul + elementwise epilogue 融合。
- FX 的常量折叠有两层：tracer 自带的（`rsqrt(buffer)`）和你写的 pass（shape 元数据）。
- TVM 部分编译是可行的；数值对齐用 `allclose`，误差在 `1e-6` 量级。
- residual 拼错是最容易犯的错；先画数据流。
- 全课主线：语义 → 图 → 融合 → 调度 → codegen → runtime → 生产栈 → 源码/设计。

## 自我检核

- [ ] 我能画出 block 的五个边界和张量形状。
- [ ] 我能解释 QKV 为什么在图里是一个节点、在 kernel 里可能是三个。
- [ ] 我知道 softmax 的四个 TIR block 各做什么。
- [ ] 我能说出 bmm 的 B 布局要求。
- [ ] 我能复现 `maxerr≈3e-07` 的整块对齐。
- [ ] 我能解释 compute nodes 48→42 的来源。
- [ ] 我知道本章没有证明“TVM 更快”。

## 延伸阅读

- **Ch 7《算子融合》**：读 elementwise/reduction 融合边界。学什么：为什么 softmax 不能随便融。与本课关联：本章 attention 的融合决策。
- **Ch 12《TVM TIR 与 schedule》**：读 `sblock/axis/split/vectorize`。学什么：TIR 的结构。与本课关联：本章 bmm/softmax 的 TIR。
- **Ch 18《TorchInductor》**：读 Inductor 如何处理同一个 block。学什么：Scheduler 的自动融合。与本课关联：本章的手写融合对照。
- **Ch 21《MLIR 在 AI 编译器》**：读 linalg.matmul / linalg.generic / bufferization。学什么：同一段计算的另一种表达。与本课关联：本章最后的 linalg 对照。
- **`../mlir_core/` 课程**：读 linalg 与 attention 的例子。学什么：indexing maps 和 iterator types。与本课关联：和本章 TIR 的 `axis.remap` 对照。

---

→ [Final Project：mini AI compiler](./final-project-mini-compiler.md)
