# Ch 10 内存规划：生命周期、复用与峰值

> **目标**：把「这个模型要多少内存」从一句感觉，变成可计算的 liveness、可复用的 buffer 和可验证的峰值；同时知道 in-place 什么时候省内存、什么时候改语义。

## 为什么需要这个？

推理引擎最先撞到的墙往往不是算力，是内存。峰值内存决定：

- 能不能放下更大的 batch；
- 能不能塞进边缘设备；
- 一个模型在 GPU 上会不会 OOM。

而峰值不是「所有中间张量加起来」。本机实测：20 个 4 MB 的中间结果，如果全部保留，RSS 峰值 **85.8 MB**；用两个 buffer 轮换复用，峰值 **10.2 MB**，差 8.4×。同一段计算，数学完全一样。

编译器在这里的角色很具体：算出每个 value 的活跃区间，把不重叠的区间映射到同一块 arena，并在语义允许时做 in-place。**内存规划不是「省一点」，是把不可运行变成可运行。**

## 先建立直觉：每个 tensor 有生有死

一张图按拓扑序执行。一个 value 在「被生产」时出生，在「最后一个用户读完」时死亡：

```text
t0:  a = matmul(x, w)       a 出生
t1:  b = relu(a)            a 仍活（后面还要用）
t2:  c = matmul(b, w2)      b 死亡
t3:  d = add(c, a)          a 最后一次使用，之后死亡
t4:  return d
```

活跃区间是 `[birth, last_use]`。两个区间不重叠，就可以共用同一块内存；重叠，就必须分开。

这个模型有三个容易忽略的点：

1. **输出节点也是用户**：一个 value 被 `output` 引用，生命周期要延到图末尾。
2. **区间是静态近似**：有分支/循环时，静态 liveness 通常取保守上界。
3. **in-place 会改变区间**：它让输入和输出共享一块内存，但也把「读」和「写」绑在一起。

## 核心概念一：liveness 与 first/last use

对每个节点记录两个下标：

```text
first[n] = n 在拓扑序里的位置
last[n]  = max(所有 user 的位置)
```

然后按拓扑序扫描：

```text
for n in nodes:
    free buffers whose last < current_index
    allocate buffer for n
```

这就是线性扫描分配器（linear scan allocator）。它不求全局最优（带任意大小/偏移的最优分配是 NP-hard，即动态存储分配），但在 DAG 上效果很好，工业编译器普遍先用它打底。

## 核心概念二：in-place 省的是带宽和峰值，不是「免费」

`relu_()`、`add_()`、`copy_()` 让输出复用输入的 storage。好处很直接：少一次分配、少一次读写。坏处是语义风险：

- **leaf 变量不能 in-place**：autograd 需要原始值算梯度。
- **被保存的中间值不能改**：改了就触发 version counter 检查。
- **view 共享 storage**：改 view 等于改 base，别名关系会传播。

本机实测的三种情况：

```python
import torch

x = torch.randn(3, 4, requires_grad=True)
x.add_(1)
```

```text
RuntimeError: a leaf Variable that requires grad is being used in an in-place operation.
```

```python
a = torch.randn(3, 4, requires_grad=True)
b = a.exp()
b.log_()
b.sum().backward()
```

```text
RuntimeError: one of the variables needed for gradient computation has been modified by an inplace operation: [torch.FloatTensor [3, 4]], which is output 0 of Exp, is at version 1; expected version 0 instead. Hint: enable anomaly detection to find the operation that failed to compute its gradient, with torch.autograd.set_detect_anomaly(True, check_nan=False).
```

但 in-place 不总是报错：

```python
a = torch.randn(3, 4, requires_grad=True)
b = a * a
b.add_(1)
b.sum().backward()      # 本机不报错
```

因为 `AddBackward` 不需要保存 `b` 的原始值。**autograd 只检查「被保存的 tensor 是否被改」，不检查「你是不是写了 in-place」**。这就是编译器必须做 alias analysis 的原因：不能靠运行时报错来兜底。

## 核心概念三：别名分析决定「能不能复用」

两个 value 可能指向同一块 storage：

```python
base = torch.arange(6.).reshape(2, 3)
v = base[0]
print(v.data_ptr() == base.data_ptr())   # True
v.add_(100)
print(base)
```

实测：

```text
True
tensor([[100., 101., 102.],
        [  3.,   4.,   5.]])
```

改 `v` 就是改 `base`。如果编译器把 `v` 的 buffer 复用给另一个 value，而 `base` 之后还要读，就会静默算错。

alias analysis 要回答的问题：

- 这两个 value 是否一定不别名（no-alias）？
- 是否可能别名（may-alias）？
- 这个 op 是否读/写它的某个参数（read/write set）？

只有拿到 `no-alias`，或者能证明旧值已经死亡，才能安全复用。view、slice、transpose、in-place op 都是别名的高发区。

## 核心概念四：三种「峰值」不要混着说

| 口径 | 含义 | 本机例子（不同实验） |
|---|---|---|
| 逻辑峰值 | 所有同时活跃 tensor 的 `numel * itemsize` 之和 | 1.50 MB |
| 分配器/arena 峰值 | 预留的地址空间高水位（含碎片） | 1.50 MB |
| 进程 RSS 峰值 | 操作系统看到的实际物理内存 | 85.8 MB（未复用） |

三者来自不同 workload，数值不能横向比较，只说明「同一份计算可以有三种差很大的口径」：

- arena 有对齐和碎片，通常 ≥ 逻辑峰值。
- RSS 含 Python 解释器、torch 运行时、分配器缓存，通常远大于单次计算的逻辑峰值。
- `tracemalloc` 只跟踪 Python 分配器，**不跟踪 torch 的 C++ tensor 分配**。

最后一条本机实测很典型：20 个中间 tensor 的逻辑峰值是 83.9 MB，`tracemalloc` 报告的峰值只有 **0.002 MB**。不是它坏了，是它看不到那条分配路径。

## 底层机制：线性扫描 + best-fit

一个够用的分配器可以写成：

```text
free_list = []                  # (offset, size)
arena_top = 0
for node in topological_order:
    for buf in allocated:
        if last[buf] < node.index:
            free_list.append(allocated.pop(buf))

    need = align(nbytes(node), 64)
    pick = smallest free block with size >= need
    if pick:
        offset = pick.offset
        split leftover back into free_list
    else:
        offset = arena_top
        arena_top += need

    allocated[node] = (offset, need)
```

两个细节：

- **先释放再分配**：当前节点的输入可能在这一步之后才死亡，顺序反了会多占一块。
- **对齐**：SIMD / DMA 通常要求 16/64/256 字节对齐；`align` 会放大 arena，但这是必须的。

线性扫描的局限是它按固定顺序做决定，可能留下碎片。更好的做法是 best-fit + 生命周期排序，或者对热点子图做整数规划；但先有一个正确的线性扫描，才有资格谈优化。

## 示例一：RSS 峰值，naive vs buffer 复用

环境：Windows 10，Intel i7-10510U，torch 2.14.1+cpu，单线程，`N=1024`，20 步 matmul/relu 交替。每步产生一个 4 MB 的中间 tensor。

本机 venv 没有 `psutil`，用 Win32 `GetProcessMemoryInfo` 读 WorkingSet（Linux/macOS 可换 `resource.getrusage`）。每个模式在**独立进程**里跑，避免分配器缓存污染。

```python
import ctypes, os, sys, tracemalloc
from ctypes import wintypes
import torch

class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]

handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, os.getpid())

def rss_mb():
    c = PMC(); c.cb = ctypes.sizeof(c)
    ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 1e6, c.PeakWorkingSetSize / 1e6

torch.set_num_threads(1); torch.manual_seed(0)
mode = sys.argv[1]; N, STEPS = 1024, 20
x, w = torch.randn(N, N), torch.randn(N, N)
base, _ = rss_mb()

with torch.inference_mode():
    if mode == "naive":
        kept, h = [], x
        for i in range(STEPS):
            h = h @ w if i % 2 == 0 else torch.relu(h)
            kept.append(h)
        current, peak = rss_mb()
        logical = sum(t.numel() * t.element_size() for t in kept) / 1e6
        print(f"naive: rss {current-base:.1f} MB, peak {peak-base:.1f} MB, logical {logical:.1f} MB")
    elif mode == "reuse":
        b0, b1 = torch.empty(N, N), torch.empty(N, N)
        h, which = x, 0
        for i in range(STEPS):
            dst = b1 if which == 0 else b0
            if i % 2 == 0:
                torch.matmul(h, w, out=dst)
            else:
                torch.clamp(h, min=0, out=dst)
            h, which = dst, which ^ 1
        current, peak = rss_mb()
        print(f"reuse: rss {current-base:.1f} MB, peak {peak-base:.1f} MB")
    elif mode == "tracemalloc":
        tracemalloc.start()
        kept, h = [], x
        for i in range(STEPS):
            h = h @ w if i % 2 == 0 else torch.relu(h)
            kept.append(h)
        _, peak = tracemalloc.get_traced_memory()
        logical = sum(t.numel() * t.element_size() for t in kept) / 1e6
        print(f"tracemalloc peak {peak/1e6:.3f} MB, logical {logical:.1f} MB")

ctypes.windll.kernel32.CloseHandle(handle)
```

实测（三个独立进程）：

```text
naive: rss 85.8 MB, peak 85.8 MB, logical 83.9 MB
reuse: rss 10.2 MB, peak 10.2 MB
tracemalloc peak 0.002 MB, logical 83.9 MB
```

三点解释：

1. naive 的 85.8 MB ≈ 20 个中间 tensor 的 83.9 MB + 运行时开销；输入/权重在基线之前已分配，不计入这个 delta。
2. reuse 只保留两个 4 MB buffer，实测 10.2 MB，接近 2 × 4 MB + 固定开销。
3. `tracemalloc` 报 0.002 MB，因为它只看到 Python 对象；**测 torch 内存不要用它**。

注意：RSS 是进程级指标，包含解释器和 allocator 缓存；重复跑同一个进程，第二次可能因为缓存看起来更低。要比较方案，就像这里一样用独立进程。

## 示例二：在 FX 图上做生命周期与分配

模型是一条带残差的四层 MLP：

```python
import torch, torch.fx as fx
from torch.fx.passes.shape_prop import ShapeProp

torch.manual_seed(0)
MB = 1024 * 1024

class Chain(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.l1 = torch.nn.Linear(1024, 1024)
        self.l2 = torch.nn.Linear(1024, 1024)
        self.l3 = torch.nn.Linear(1024, 1024)
        self.l4 = torch.nn.Linear(1024, 1024)

    def forward(self, x):
        a = torch.relu(self.l1(x))
        b = torch.relu(self.l2(a))
        c = torch.relu(self.l3(b))
        return torch.sigmoid(self.l4(c)) + a

model = Chain().eval()
gm = fx.symbolic_trace(model)
ShapeProp(gm).propagate(torch.randn(128, 1024))
nodes = list(gm.graph.nodes)
index = {n: i for i, n in enumerate(nodes)}
```

计算每个节点的字节数与生命周期：

```python
def shape_of(node):
    meta = node.meta.get("tensor_meta")
    if isinstance(meta, (tuple, list)):
        meta = meta[0]
    if meta is None:
        return None
    return meta.shape if hasattr(meta, "shape") else meta

def nbytes(node):
    shape = shape_of(node)
    if shape is None:
        return 0
    meta = node.meta["tensor_meta"]
    if isinstance(meta, (tuple, list)):
        meta = meta[0]
    numel = 1
    for dim in shape:
        numel *= int(dim)
    return numel * getattr(meta, "dtype", torch.float32).itemsize

first, last = {}, {}
for i, node in enumerate(nodes):
    if nbytes(node) > 0:
        first[node] = i
    for user in node.users:
        last[node] = max(last.get(node, i), index[user])
for node in nodes:
    if node.op == "output":
        for used in node.all_input_nodes:
            last[used] = max(last.get(used, index[node]), index[node])
```

实测生命周期（每个 value 都是 128×1024 float32 = 0.50 MB）：

| 节点 | first | last | 说明 |
|---|---:|---:|---|
| `x` | 0 | 1 | 输入 |
| `l1` | 1 | 2 | 第一层输出 |
| `relu` | 2 | 9 | 残差，活到最后的 add |
| `l2` | 3 | 4 | |
| `relu_1` | 4 | 5 | |
| `l3` | 5 | 6 | |
| `relu_2` | 6 | 7 | |
| `l4` | 7 | 8 | |
| `sigmoid` | 8 | 9 | |
| `add` | 9 | 10 | 输出 |

`a`（`relu`）活到第 9 步，所以峰值不是 1 个 buffer，而是「当前主链 + 残差 + 当前输出」= 3 个 buffer = 1.50 MB。这与实测的 `max concurrent = 1.50 MB` 一致。

实际分配器与统计（与本章开头伪代码同构，补上对齐和 best-fit）：

```python
total = sum(nbytes(n) for n in nodes if n.op != "output")
events = []
for node in nodes:
    if nbytes(node) and node.op != "output":
        events.append((first[node], 1, nbytes(node)))
        events.append((last[node] + 1, -1, nbytes(node)))
live = peak = 0
for _, delta, size in sorted(events, key=lambda e: (e[0], e[1])):
    live += delta * size
    peak = max(peak, live)
print(f"naive={total/MB:.2f} MB, max live={peak/MB:.2f} MB")

free, allocated = [], {}
arena = live = peak_live = 0
for i, node in enumerate(nodes):
    for done in [n for n in allocated if last[n] < i]:
        offset, size = allocated.pop(done)
        free.append((offset, size))
        live -= size
    size = nbytes(node)
    if size == 0 or node.op in ("placeholder", "get_attr", "output"):
        continue
    size = (size + 63) // 64 * 64          # 64-byte alignment
    free.sort(key=lambda block: block[1])
    pick = next((j for j, (_, cap) in enumerate(free) if cap >= size), None)
    reused = pick is not None
    if reused:
        offset, cap = free.pop(pick)
        if cap > size:
            free.append((offset + size, cap - size))
    else:
        offset, arena = arena, arena + size
    allocated[node] = (offset, size)
    live += size
    peak_live = max(peak_live, live)
    print(f"{node.name:8s} off={offset:7d} size={size/MB:5.2f} MB "
          f"live={live/MB:5.2f} MB {'reuse' if reused else 'new'}")
print(f"\narena = {arena/MB:.2f} MB, peak live = {peak_live/MB:.2f} MB, naive = {total/MB:.2f} MB")
```

线性扫描 + best-fit 的结果：

```text
naive=5.00 MB, max live=1.50 MB

l1       off=      0 size= 0.50 MB live= 0.50 MB new
relu     off= 524288 size= 0.50 MB live= 1.00 MB new
l2       off=      0 size= 0.50 MB live= 1.00 MB reuse
relu_1   off=1048576 size= 0.50 MB live= 1.50 MB new
l3       off=      0 size= 0.50 MB live= 1.50 MB reuse
relu_2   off=1048576 size= 0.50 MB live= 1.50 MB reuse
l4       off=      0 size= 0.50 MB live= 1.50 MB reuse
sigmoid  off=1048576 size= 0.50 MB live= 1.50 MB reuse
add      off=      0 size= 0.50 MB live= 1.50 MB reuse

arena = 1.50 MB, peak live = 1.50 MB, naive = 5.00 MB
```

从「每个输出一块」的 5.00 MB 降到 1.50 MB，3.3×。这不是靠 in-place，纯粹是把死亡 buffer 还给 free list。

两个工程注意点：

- 这里 `l1`…`l4` 是 `call_module`，权重不在图里；真实编译器的常量池要单独算，且通常全程常驻。
- 分配顺序按拓扑序；如果把 `add` 提前，`relu` 的生命周期会算错，峰值也会错。

## 示例三：in-place 的四种命运

| 写法 | 结果 | 原因 |
|---|---|---|
| leaf `x.add_(1)` | 报错 | leaf 的原始值要用于梯度 |
| `b = a.exp(); b.log_()` | 报错 | `ExpBackward` 保存了输出，版本号变了 |
| `b = a * a; b.add_(1)` | 不报错 | `AddBackward` 不需要保存 `b` |
| `v = base[0]; v.add_(1)` | 静默改 `base` | view 与 base 共享 storage |

**最危险的是第三、第四行**：没有异常，但语义已经变了。编译器做 in-place 优化时，不能只问「这个 op 能不能 in-place」，还要问：

- 这个 buffer 之后还有人读吗？
- 有没有 view / alias 指向它？
- 这个 op 的 backward 是否保存了输入？
- 如果开了 autograd，版本计数器会不会炸？

推理场景通常关闭 autograd，风险小很多；但只要还有「同一个 buffer 被两个消费者读」或「view 别名」，就不能盲目复用。

## 对比与取舍

| 策略 | 峰值 | 语义风险 | 适用 |
|---|---|---|---|
| 每个 value 独立分配 | 最高（naive sum） | 无 | 调试、基线 |
| 生命周期复用（arena） | 接近 max live | 无（不改值） | 推理默认 |
| in-place | 更低 | 高（autograd/别名） | 推理、无别名、已证明安全 |
| 重计算（recompute） | 低 | 中（多算） | 训练、显存换计算 |
| 分块/流式 | 最低 | 中（改调度） | 大激活、边缘设备 |

## 踩雷集锦

1. **用 `tracemalloc` 测 torch 内存**：本机逻辑 83.9 MB，它只报 0.002 MB。用 RSS / 分配器统计。
2. **在一个进程里比较两种方案**：allocator 缓存会让第二次测量偏低；用独立进程或重置 allocator。
3. **忘记 output 也是 user**：最后一个 value 的生命周期会被算短，导致非法复用。
4. **复用还被读的 buffer**：`h = buf` 之后下一轮又写 `buf`，读到的是新值。
5. **对 leaf 做 in-place**：直接报错；训练图里更常见。
6. **忽略 view 别名**：改 view 等于改 base，复用会静默污染。
7. **不做对齐**：arena 起点按 1 字节排，SIMD 加载可能崩溃或变慢。

## 进阶：再往深一层

- **TVM 的 storage rewrite**：Relax/TIR 在 lowering 后做 buffer 复用与 storage 合并；核心也是 liveness + 对齐 + 别名约束。
- **TorchInductor 的内存规划**：Inductor 在调度后为每个 buffer 分配 `MemoryPlan`，并尽量把中间结果放进复用池；它与本章的线性扫描同源，但还要考虑 kernel 的读写集。
- **CUDA caching allocator**：GPU 上的 `memory_allocated` / `memory_reserved` 是两套口径，和 RSS / arena 的差别同理；本机无 CUDA，属 `guided`。
- **重计算**：训练时用「多算一遍」换显存，典型是 activation checkpointing；它把 liveness 问题变成了计算图重写问题。

## 动手练习

### 练习 1：把对齐加进分配器

示例二的分配器已按 64 字节对齐（`(size + 63) // 64 * 64`）。把 `64` 改成 256，重跑，记录 arena 是否变化；再用一个非对齐的 shape 观察对齐带来的放大。

<details>
<summary>参考思路</summary>

每个 0.50 MB 的 buffer 本身就是 64/256 的倍数（524288），所以这组 shape 下 arena 不变。换一个非对齐的 shape（如 `(127, 1000)`）才能看到差异：对齐会把每块向上取整，碎片也随之变化。

</details>

### 练习 2：first-fit vs best-fit

把 `free.sort(key=size)` + 取第一个改成「取第一个足够大的块」（first-fit），比较 arena 和碎片。构造一个 size 参差的图（如 1 MB、0.3 MB、0.7 MB 交替），观察差异。

<details>
<summary>参考思路</summary>

best-fit 倾向留下大块，适合后续大分配；first-fit 更快但容易把大块切碎。用同一组生命周期跑两种策略，打印 `arena` 和 `len(free_list)`。没有绝对赢家，取决于分配序列。

</details>

### 练习 3：给 in-place 加安全条件

写一个 pass，把 `relu` 改成 `relu_`，但只在满足以下条件时：输入不是 leaf、输入只有一个 user、输入没有 view 别名、当前不在 autograd 模式。用本章的四种例子验证它会拒绝哪些。

<details>
<summary>参考思路</summary>

用 `node.args[0]` 找输入，检查 `len(input.users) == 1`、`input.meta` 里是否有 alias 信息（FX 默认没有，需要自己标）、`torch.is_grad_enabled()`。对 `a.exp(); b.log_()` 这种，静态 pass 很难知道 backward 保存了什么，所以推理场景才敢放开。

</details>

## 本章重点整理

- 每个 value 的生命周期是 `[first, last use]`；不重叠才能复用。
- 线性扫描 + best-fit 足够打底；先释放再分配、对齐、拓扑序是三个硬要求。
- 逻辑峰值、arena 峰值、RSS 峰值是三套口径，不能混着报。
- `tracemalloc` 看不到 torch 的 C++ 分配；本机实测 83.9 MB 逻辑只报 0.002 MB。
- in-place 省内存但改语义；autograd 只在「保存的 tensor 被改」时报错，不保证全覆盖。
- alias analysis 是安全复用的前提；view / slice / in-place 都是别名高发区。
- 本机 20 个中间 tensor：naive 85.8 MB，双 buffer 复用 10.2 MB。

## 自我检核

- [ ] 我能为一张 FX 图算出每个节点的 first/last use
- [ ] 我能解释为什么 `max live` 小于 `naive sum`
- [ ] 我能写出线性扫描分配器的核心循环
- [ ] 我能区分逻辑峰值、arena 峰值和 RSS
- [ ] 我知道 `tracemalloc` 为什么测不到 torch tensor
- [ ] 我能说出 in-place 的至少三种语义风险
- [ ] 我能解释 alias analysis 和 buffer 复用的关系

## 延伸阅读

- **TVM Relax / TIR 的 storage rewrite**（https://tvm.apache.org/docs/）：读 buffer 分配与复用 pass 的位置。与本章关联：工业级 liveness + arena。
- **TorchInductor 源码**（https://github.com/pytorch/pytorch/tree/main/torch/_inductor）：读 `memory_planning.py` / `scheduler.py` 里的 buffer 复用逻辑。与本章关联：本章分配器的生产版本。
- **PyTorch FX `ShapeProp`**（https://pytorch.org/docs/stable/fx.html）：读 `torch.fx.passes.shape_prop` 的用法。与本章关联：示例二拿 shape 的来源。
- **PyTorch Autograd 文档：in-place 语义**（https://pytorch.org/docs/stable/notes/autograd.html#in-place-operations-with-autograd）：读 version counter 与 saved tensor 的规则。与本章关联：in-place 风险的官方解释。
- **CUDA Caching Allocator**（https://pytorch.org/docs/stable/notes/cuda.html#memory-management）：读 allocated vs reserved 的区别。与本章关联：GPU 上的三套内存口径（本机无 CUDA，属 `guided`）。

---

→ [练习 B：融合、折叠与内存复用](./practice-b-graph-passes.md)
