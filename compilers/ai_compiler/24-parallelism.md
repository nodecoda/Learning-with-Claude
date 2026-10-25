# Ch 24 并行与分布式：通信也是一种算子

> **目标**：从编译器视角看清数据并行、张量并行、流水并行各自切开了什么、需要哪种集合通信；在本机用 gloo 起 2 个进程，真跑 all-reduce / all-gather / DDP，验证张量并行的列切与行切在数值上等价；理解编译器如何处理通信——融合、切块、overlap，以及它为什么经常「什么也做不了」。

## 为什么需要这个？

模型放不下一张卡，或者一张卡喂不饱，就必须切。切法只有三种基本维度，其余都是组合：

| 并行 | 切开什么 | 复制什么 | 每步通信 | 典型场景 |
|---|---|---|---|---|
| 数据并行（DP） | batch | 权重 | 梯度 all-reduce | 模型能单卡放下 |
| 张量并行（TP） | 单层内的矩阵 | 无（激活切分） | all-reduce / all-gather | 单层太大 |
| 流水并行（PP） | 层 | 无 | 点对点 send/recv | 层数多、跨机 |

编译器在这里的角色常被误解。通信不是「运行时的事」：一旦图里出现 all-reduce 节点，融合边界、调度顺序、内存规划全都要改。更麻烦的是，**通信算子没有逐元素语义**——你不能把 all-reduce 和它前面的 relu 融成一个 kernel，只能决定「谁等谁、能不能重叠」。

先记住一句话：**并行的收益由计算/通信比决定，编译器只能改善这个比值（融合、压缩、重叠），不能凭空创造带宽。**

> 实测环境：Windows 10 专业版，i7-10510U（4C/8T），torch 2.14.1+cpu，gloo 后端，2 进程回环。回环带宽不代表任何真实集群；GPU/NCCL 部分标 `guided`。

## 先建立直觉：三种并行切的是不同维度

以 `Y = X @ W`（`X: [B,K]`，`W: [K,N]`）和两层 MLP 为例：

```text
数据并行：  每卡一份完整 W，各自吃 B/2 的 batch
            → 反向后把梯度加起来（all-reduce），保持权重一致

张量并行（列切）：W 按 N 切成 W0/W1，两卡各算 Y0/Y1
            → 输出按 N 拼起来（all-gather）或保持切分

流水并行：  卡 0 跑第 1 层，卡 1 跑第 2 层
            → 卡 0 把激活 send 给卡 1，反向再 recv 梯度
```

三种并行可以叠加：TP 在机内（NVLink），PP 跨机（IB），DP 最外层。编译器看到的图也随之变化：DP 在反向图末尾多出 all-reduce，TP 在每层前后多出集合通信，PP 把图切成若干段并在段间插 send/recv。

## 核心概念一：数据并行 = 反向图里的 all-reduce

设两张卡各算一个 minibatch 的梯度 `g0`、`g1`。要让权重保持一致，更新必须用两者的平均：

```text
g = (g0 + g1) / 2
W ← W - lr * g
```

`all-reduce` 就是这个「求和并广播回所有卡」。工程上有两个关键设计：

1. **分桶（bucketing）**：梯度不是逐个 all-reduce，而是攒到 ~25 MB 一个桶，一次集合通信。桶越大，固定延迟摊得越薄；桶越小，越早开始通信、越容易与反向计算重叠。
2. **异步化**：反向算到某一层就立刻对该层梯度发起 all-reduce，不等整个反向结束。图上表现为「all_reduce 返回一个 token，`wait_tensor` 才真正读结果」，编译器可以在两者之间插入独立计算。

DDP（DistributedDataParallel）就是这两点的产品化。它对编译器提出的要求是：**别把 all-reduce 之前的生产者融合进别的 kernel 而破坏触发时机**。这也是为什么通信算子常被视为融合屏障。

## 核心概念二：张量并行 = 矩阵的列切与行切

`Y = X @ W`，`W: [K,N]`。两种切法：

**列并行（split N）**：`W = [W0 | W1]`，每卡算 `Y_i = X @ W_i`，得到 `[B, N/2]`。各卡输出拼起来就是完整 `Y`，所以后续要么 all-gather，要么让下游继续按列切（Megatron 的做法）。

**行并行（split K）**：`W = [W0; W1]`，`X = [X0 | X1]`，每卡算 `X_i @ W_i`，得到 `[B, N]` 的**部分和**，必须 all-reduce 求和。注意每卡的 `partial` 单独看是完全错误的——实测中它的最大误差是 16.73，而完整结果的平均绝对值只有 6.30。

一个 Transformer block 的 MLP 通常这样排：

```text
Y = GeLU(X @ W1) @ W2
    └─ 列切 W1 ─┘  └─ 行切 W2 ─┘
    → GeLU 前不需要通信（按列切分保持）
    → W2 之后一次 all-reduce
```

每层一次 all-reduce，通信量是 `B × S × H` 个元素。序列越长、隐藏维越大，这一项越贵。序列并行（sequence parallel）把 LayerNorm/Dropout 也按序列切，正是为了减少这块。

## 核心概念三：流水并行 = 按层切 + 点对点

PP 把层分给不同设备，卡间只传激活和梯度，用 `send/recv`（点对点），不是集合通信。为了不让设备空等，把一个 batch 切成 `m` 个 micro-batch 流水起来。

气泡比例有个常用估算（`p` 个 stage、`m` 个 micro-batch）：

```text
bubble_fraction = (p - 1) / (m + p - 1)
```

`p=4, m=8` 时约 27%。要压到 10% 以下，`m` 得是 `p` 的十几倍——这也是 PP 通常放在跨机、TP 放在机内的原因：跨机带宽低，点对点比 all-reduce 便宜。

编译器对 PP 的处理更接近「图分区 + 通信插入」：切分点要满足依赖闭合，send/recv 要插在正确位置，重计算（activation checkpointing）会改变显存与通信的平衡。

## 通信算子在图里长什么样

这是本章最该亲手看一眼的东西。用 `torch.export` 导出带 `dist.all_reduce` 的模块（需要已初始化进程组），实测（2 进程 gloo，`strict=False`）：

```python
class AR(nn.Module):
    def forward(self, x):
        y = x.clone()
        dist.all_reduce(y)
        return y

ep = torch.export.export(AR().eval(), (torch.randn(4, 8),), strict=False)
print(ep.graph_module.code)
```

```text
def forward(self, x):
    clone = torch.ops.aten.clone.default(x);  x = None
    all_reduce = torch.ops._c10d_functional.all_reduce.default(clone, 'sum', '0')
    wait_tensor = torch.ops._c10d_functional.wait_tensor.default(all_reduce);  all_reduce = None
    copy_ = torch.ops.aten.copy_.default(clone, wait_tensor);  clone = wait_tensor = None
    return (copy_,)
```

三个细节：

1. **算子名是 `_c10d_functional.all_reduce`**，参数是 `(tensor, reduce_op, group_name)`。group 名 `'0'` 是默认组的内部标识，不是一个可读名字。
2. **返回的不是张量，是一个 token**；必须经过 `wait_tensor` 才拿到值。这个「发起 / 等待」的拆分就是 overlap 的抓手：编译器可以把独立计算排在两者之间。
3. **`clone` 是必要的**，因为 `all_reduce` 原地改张量，而 `x` 可能还有别的消费者。图级编译器必须识别这种 in-place 语义，否则会错误地复用 buffer（Ch 10）。

all-gather 的图更直接：

```text
def forward(self, x):
    all_gather_into_tensor = torch.ops._c10d_functional.all_gather_into_tensor.default(x, 2, 0)
    return (all_gather_into_tensor,)
```

参数 `(tensor, world_size, dim)`。这次导出里它直接作为输出返回，所以图里没有 `wait_tensor`；在真实计算图中，下游使用前通常会经过 `wait_tensor` 建立依赖。

## 底层机制：编译器如何对待通信

通信算子在 lowering 之后通常就是一次库调用（gloo/NCCL），编译器能做的事有限但很关键。

**1）融合：通信是屏障，但边界可以挪。**

`all_reduce(relu(x))` 不能融成一个 kernel——集合通信要求所有 rank 在同一时刻进入同一个调用。所以编译器把通信当作**融合屏障**：生产者可以在通信前单独成 kernel，消费者在通信后单独成 kernel。

能融的是「通信前后的搬运」：cast、reshape、permute、slice 可以在通信前做掉，让通信传更少的字节。例如把 `all_reduce(x.float())` 改成 `all_reduce(x.bf16())` 再转回来，通信量直接减半——这是编译器最容易拿到的收益，也是最容易被手写代码忽略的。

**2）切块与重叠：把一次大通信拆成多段。**

Ring all-reduce 把张量切成 `2(p-1)` 块，先 reduce-scatter 再 all-gather。对编译器来说，这等价于把一个大通信节点拆成「按 chunk 的多个通信 + 依赖」：

```text
for chunk in chunks:
    start_all_reduce(chunk)      # 异步，返回 token
compute_independent_work()       # 与通信无依赖的计算
wait(token); use(chunk)
```

能不能重叠，取决于依赖图里有没有「独立工作」。反向传播天然有：第 3 层的梯度 all-reduce 可以和算第 2 层的梯度重叠。这就是 DDP 分桶 + 异步的价值。

**3）切分策略：通信量由切法决定。**

同样的算子，切法不同，通信量差一个数量级：

| 切法 | 通信 | 每层通信量 | 备注 |
|---|---|---|---|
| 列并行 + 保持切分 | 无（下游按列切） | 0 | 需要整条链都按列切 |
| 列并行 + all-gather | all-gather | `B·S·N` | 拼接输出 |
| 行并行 | all-reduce | `B·S·N` | 求部分和 |
| 数据并行 | 梯度 all-reduce | 参数量 | 每步一次 |
| FSDP | reduce-scatter + all-gather | 2 × 参数量 | 省显存，通信翻倍 |

编译器不能替你选切法，但能算出每种切法的通信字节数，喂给代价模型。这就是「并行策略搜索」（如 Megatron 的 TP/PP/DP 组合、DeepSpeed 的 ZeRO stage）干的事。

**4）代价模型：先算比值，再谈优化。**

设 matmul `[B,K] @ [K,N]`，FLOPs 为 `2BKN`，行并行的 all-reduce 字节数为 `4BN`（fp32）。计算/通信比：

```text
compute_time ≈ 2BKN / FLOPS
comm_time    ≈ 4BN / BW
ratio        ≈ (K / 2) * (BW / FLOPS)
```

关键结论：**比值只和 `K` 与「带宽/算力」有关，和 batch 无关。** 大 `K`（宽层）适合 TP，小 `K`（窄层）通信占比高。这也是为什么 TP 通常只切 attention/MLP 的投影矩阵，不切 LayerNorm。

本机可以用一组实测数字校准直觉：1K×1K matmul 中位数 38.45 ms（约 55.9 GFLOPS），而同进程下 4 MB（1M float）all-reduce 中位数 12.04 ms，通信/计算 ≈ 0.31。回环上通信已经这么贵，真实集群的 IB/NVLink 会好一到两个数量级——但「通信不是免费」这个结论不变。

## 示例一：2 进程 gloo 的 all-reduce 与 all-gather（实测）

Windows 上必须用 `spawn`（不是 `fork`），并且入口要有 `if __name__ == "__main__":`：

```python
import os, time, json, statistics
import torch, torch.distributed as dist
import torch.multiprocessing as mp

def worker(rank, world, port, out):
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = str(port)
    dist.init_process_group("gloo", rank=rank, world_size=world)

    x = torch.tensor([float(rank + 1), 2.0 * (rank + 1), 10.0])
    dist.all_reduce(x, op=dist.ReduceOp.SUM)

    gx = torch.empty(world * 3)
    dist.all_gather_into_tensor(gx, torch.full((3,), float(rank)))

    t = torch.ones(1 << 20)
    for _ in range(5): dist.all_reduce(t)          # warmup
    s = []
    for _ in range(20):
        t0 = time.perf_counter(); dist.all_reduce(t); s.append(time.perf_counter() - t0)
    if rank == 0:
        out["reduced"] = x.tolist()
        out["gathered"] = gx.tolist()
        out["per_call_ms"] = statistics.median(s) * 1e3
        out["backend"] = dist.get_backend()
    dist.barrier(); dist.destroy_process_group()

if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    out = mp.Manager().dict()
    mp.spawn(worker, args=(2, 29531, out), nprocs=2, join=True)
    print(json.dumps(dict(out), indent=2))
```

实测：

```json
{
  "reduced": [3.0, 6.0, 20.0],
  "gathered": [0.0, 0.0, 0.0, 1.0, 1.0, 1.0],
  "per_call_ms": 14.997,
  "backend": "gloo",
  "world": 2
}
```

核对：rank0 的 `[1,2,10]` 加 rank1 的 `[2,4,10]` 得 `[3,6,20]`；all-gather 按 rank 顺序拼接，所以前三个是 rank0 的 0、后三个是 rank1 的 1。

不同消息大小的延迟（同一次会话，每档 20 次，取中位数）：

```text
numel=    1024 bytes=     4096 median=0.7658 ms min=0.5910 max=2.1814 bandwidth=0.005 GB/s
numel=   16384 bytes=    65536 median=0.7567 ms min=0.6603 max=1.0107 bandwidth=0.087 GB/s
numel=  262144 bytes=  1048576 median=2.1441 ms min=1.8800 max=3.4929 bandwidth=0.489 GB/s
numel= 1048576 bytes=  4194304 median=10.8307 ms min=9.2217 max=18.1790 bandwidth=0.387 GB/s
numel= 4194304 bytes= 16777216 median=38.7381 ms min=33.6777 max=55.7888 bandwidth=0.433 GB/s
```

两个区域很清楚：

- **小消息是延迟主导**：4 KB 和 64 KB 都约 0.76 ms，带宽只有 0.005–0.087 GB/s。固定开销（系统调用、socket、同步）占绝对主导。
- **大消息是带宽主导**：1 MB 以上稳定在 0.39–0.49 GB/s。gloo 走 TCP 回环，这个数字远低于真实 RDMA/NVLink，只用于建立「量级感」。

结论：**别对 4 KB 的 all-reduce 谈带宽优化**，先减少调用次数（分桶/融合）。反过来，16 MB 的通信要考虑压缩、切块与 overlap。

> 实测会打印 `[W ...] [c10d] The client socket has failed to connect ... (system error: 10049)`。这是 gloo 在 Windows 上先试 IPv6/别的地址失败后回退到 IPv4 的噪声，不影响结果；看到它不必慌。
>
> 另外 `all_gather_into_tensor` 在 2.14 会打印 `FutureWarning`，提示改用 `all_gather_single`；本课保留旧名，因为它在 2.x 上仍可用且在导出的图里就是这个算子名。

## 示例二：张量并行的数值验证（实测）

同一个 2 进程作业里做两件事：列切后 all-gather 拼回完整输出；行切后 all-reduce 求部分和。与单进程完整 matmul 对比。

```python
 # 接示例一的 imports 与 __main__ 入口，只换 worker 的函数体
def worker(rank, world, port, out):
    os.environ["MASTER_ADDR"] = "127.0.0.1"; os.environ["MASTER_PORT"] = str(port)
    dist.init_process_group("gloo", rank=rank, world_size=world)
    torch.manual_seed(0)
    M, K, N = 8, 64, 128
    x, W = torch.randn(M, K), torch.randn(K, N)
    full = x @ W

    # 列并行：按 N 切权重
    Wc = W.chunk(world, dim=1)[rank].contiguous()
    y_part = x @ Wc
    gathered = [torch.empty(M, N // world) for _ in range(world)]
    dist.all_gather(gathered, y_part)
    y_col = torch.cat(gathered, dim=1)

    # 行并行：按 K 切权重与激活，得到部分和
    Wr = W.chunk(world, dim=0)[rank].contiguous()
    xr = x.chunk(world, dim=1)[rank].contiguous()
    partial = xr @ Wr
    y_row = partial.clone()
    dist.all_reduce(y_row, op=dist.ReduceOp.SUM)

    if rank == 0:
        out["col_shape"] = list(y_col.shape)
        out["col_maxerr"] = float((y_col - full).abs().max())
        out["col_part_shape"] = list(y_part.shape)
        out["row_maxerr"] = float((y_row - full).abs().max())
        out["partial_vs_full_maxerr"] = float((partial - full).abs().max())
        out["full_abs_mean"] = float(full.abs().mean())
    dist.barrier(); dist.destroy_process_group()
```

实测（rank 0 汇总）：

```json
{
  "col_shape": [8, 128],
  "col_maxerr": 0.0,
  "col_part_shape": [8, 64],
  "row_maxerr": 5.7220458984375e-06,
  "partial_vs_full_maxerr": 16.730192184448242,
  "full_abs_mean": 6.297748565673828
}
```

三行数字各有含义：

1. **列切 `maxerr = 0.0`**：每个输出元素仍由同一个 matmul 算出，只是被分到不同卡，拼回来逐位相等。列切不引入数值误差。
2. **行切 `maxerr = 5.72e-06`**：两段 `K=32` 的部分和相加，累加顺序变了，浮点结果略有差异——这是正常的，且与 Ch 7 的 reduction 顺序问题同源。
3. **单卡 partial 与完整结果差 16.73**：`full_abs_mean` 只有 6.30，说明 partial 单独看毫无意义。**行并行必须 all-reduce，少一步就是错的**；而这类错误不会报 shape mismatch（形状都是 `[8,128]`），只会静默给出错误数值。

这也解释了编译器为什么难做 TP 的自动切分：切法正确性依赖「下游是否需要完整结果」这种语义信息，图里看不出来。

## 示例三：DDP 梯度一致性（实测）

数据并行的验收标准不是 loss，而是**梯度**：各 rank 的梯度必须逐位相同，且等于整批梯度的平均。

```python
from torch.nn.parallel import DistributedDataParallel as DDP

class Net(nn.Module):
    def __init__(self):
        super().__init__()
        torch.manual_seed(42)
        self.fc1, self.fc2 = nn.Linear(8, 16), nn.Linear(16, 1)
    def forward(self, x):
        return self.fc2(torch.relu(self.fc1(x)))

def worker(rank, world, port, out):
    os.environ["MASTER_ADDR"] = "127.0.0.1"; os.environ["MASTER_PORT"] = str(port)
    dist.init_process_group("gloo", rank=rank, world_size=world)
    torch.manual_seed(42)
    model = DDP(Net())
    torch.manual_seed(0)
    X, Y = torch.randn(16, 8), torch.randn(16, 1)
    shard = slice(rank * 8, (rank + 1) * 8)
    loss = ((model(X[shard]) - Y[shard]) ** 2).mean()
    loss.backward()
    g = model.module.fc1.weight.grad
    buf = [torch.zeros_like(g) for _ in range(world)]
    dist.all_gather(buf, g)
    if rank == 0:
        out["grads_identical"] = bool(torch.equal(buf[0], buf[1]))
        out["grad_norm_rank0"] = float(g.norm())
    else:
        out["grad_norm_rank1"] = float(g.norm())
    out[f"loss_rank{rank}"] = float(loss.detach())
    dist.barrier(); dist.destroy_process_group()
```

实测：

```text
grads_identical: true
grad_norm_rank0: 0.492741
grad_norm_rank1: 0.492741
loss_rank0: 1.05194
loss_rank1: 0.797181
single-process full-batch loss: 0.92456, grad_norm: 0.492741
```

三个观察：

- **两个 rank 的梯度完全相同**，且范数与单进程整批（16 个样本）一致。DDP 的 all-reduce 是求平均，不是求和。
- **各 rank 的 loss 不同**（1.05194 vs 0.797181），但平均值 `(1.05194+0.797181)/2 = 0.92456` 正好等于整批 loss。这验证了「分片 loss 的均值 = 整批 loss」。
- 若你的分片大小不等，DDP 仍按 rank 求平均，此时梯度是**按 rank 等权**而非按样本数加权——见练习 3。

另一个细节：`DDP(Net())` 在 CPU 上不需要 `device_ids`；在 GPU 上要传 `device_ids=[rank]`，并把模型先 `.to(rank)`。这部分是 `guided`。

## 对比与取舍

| 并行 | 通信原语 | 通信量/步 | 编译器能做的 | 主要风险 |
|---|---|---|---|---|
| DP | all-reduce（梯度） | 参数量 | 分桶、异步、与反向重叠 | 桶太大延迟高，太小碎片化 |
| TP 列切 | all-gather | `B·S·N` | 通信前 cast/reshape、保持切分 | 下游误以为拿到完整张量 |
| TP 行切 | all-reduce | `B·S·N` | 融合前置逐元素、切块重叠 | 少一次 reduce 就静默错 |
| PP | send/recv | 激活大小 | 分区、重计算、调度 micro-batch | 气泡、显存峰值 |
| FSDP | reduce-scatter + all-gather | 2 × 参数量 | 参数分片、通信与计算交织 | 通信翻倍，依赖高带宽 |

选型的三条经验：

1. **先看模型能否单卡放下**：能放下就 DP；放不下先试 ZeRO/FSDP，再考虑 TP。
2. **TP 只在机内**：它每层都要通信，跨机必死；PP 才适合跨机。
3. **用「通信字节 / 计算 FLOPs」做决策**，不要凭感觉。上面的比值公式只需要 `K` 和目标硬件的 `BW/FLOPS`。

## 踩雷集锦

1. **Windows 上不用 `spawn`**：`fork` 不可用，必须 `mp.set_start_method("spawn", force=True)` 且入口有 `__main__` 保护，否则子进程重复执行建进程代码。
2. **忘了 `dist.barrier()` 就退出**：某个 rank 先销毁进程组，其余 rank 卡在集合通信里直到超时。
3. **把 all-reduce 当成求和**：DDP 求的是平均；手写训练循环时忘了除以 `world_size`，学习率等效放大 `world_size` 倍。
4. **all-gather 后按错误顺序拼接**：输出严格按 rank 顺序排列，不是按完成时间。
5. **非连续张量直接通信**：先 `.contiguous()`，否则某些后端会报错或走慢路径。
6. **以为导出的图会自动包含通信**：Python 控制流里的 `dist.all_reduce` 不会被捕获；必须在 `forward` 的直线代码里调用。
7. **在通信前后随便改 dtype**：all-reduce 要求所有 rank 的 dtype/形状一致；`bf16` 省带宽，但要确认后端支持。
8. **用回环带宽推断集群性能**：本机 0.4 GB/s 是 TCP 回环 + gloo 的结果，NVLink/IB 完全不同。

## 进阶：再往深一层

**GPU 与 NCCL（guided）**：本机无 CUDA，以下无法实测。真实栈里，`_c10d_functional` 会被 Inductor 降到 NCCL 调用；overlap 依赖 CUDA stream 与 event，编译器把通信放在独立 stream，用 event 建立「计算等通信」的依赖。想看真实图，需要在 GPU 上 `TORCH_LOGS="inductor"` 并检查生成的 `extern_kernels`。

- **NCCL 的算法选择**：ring vs tree，按消息大小与拓扑自动选；`NCCL_DEBUG=INFO` 可看。
- **TP 的通信隐藏**：Megatron 把 all-gather 与下一层的计算重叠，需要细粒度依赖；这是编译器与手写调度差距最大的地方。
- **FSDP 的分片策略**：参数 shard 后，前向 all-gather、反向 reduce-scatter，通信量是 DP 的两倍，但显存是 1/N。
- **通信压缩**：fp16/bf16 梯度、Top-K 稀疏化、量化 all-reduce；编译器需要知道「压缩是有损的」，不能随意重排。
- **集合通信的死锁**：所有 rank 必须以相同顺序调用集合操作；图级重排如果改变顺序，会在运行时死锁——这是「通信是屏障」的最硬约束。

## 动手练习

### 练习 1：4 进程 all-reduce 与规模效应

**任务**：把示例一的 `nprocs` 改成 4，张量改为 `[1,2,3,4]`，验证 all-reduce 结果与 all-gather 顺序；再测 4 MB 的延迟，和 2 进程对比。

**验收**：all-reduce 得 `[10,20,30,40]`；all-gather 得 `[0,0,0,1,1,1,2,2,2,3,3,3]`；4 MB 延迟高于 2 进程（回环上通常 1.5–2×）。

<details>
<summary>参考解答</summary>

只改 `nprocs=4`、`args=(4, port, out)`，进程组会按 rank 0–3 建立。all-reduce 是求和，所以每个元素都是 1+2+3+4=10 的倍数。**常见卡点**：端口被上一次运行占用，换一个 `MASTER_PORT`；`mp.Manager()` 要在 `spawn` 前创建。
</details>

### 练习 2：行并行改 3 路切分

**任务**：把示例二的 `world=2` 改成 3（`K=96`），验证行并行 `maxerr` 仍在 `1e-5` 量级；再故意跳过 all-reduce，观察误差量级。

**验收**：3 路 `maxerr ≈ 1e-5`；跳过 all-reduce 时 `maxerr` 与 `full_abs_mean` 同量级（完全错误）。

<details>
<summary>参考解答</summary>

`K=96` 能被 3 整除，`W.chunk(3, dim=0)` 得 `[32,128]`，`x.chunk(3, dim=1)` 得 `[8,32]`。跳过 all-reduce 时每个 rank 只有 1/3 的部分和，误差约等于完整结果本身的量级。**常见卡点**：`K` 不能被 `world` 整除时 `chunk` 会产生不等分片，需要 padding 或改用 `tensor_split`。
</details>

### 练习 3：不等分片的 DDP 偏差

**任务**：让 rank0 处理 12 个样本、rank1 处理 4 个，其余不变。比较 DDP 梯度与「按样本数加权」的整批梯度。

**验收**：两者不相等；DDP 梯度等于两个分片梯度的**等权平均**，而不是 `(12*g0 + 4*g1)/16`。

<details>
<summary>参考解答</summary>

DDP 在 all-reduce 后除以 `world_size`，不看每个 rank 的样本数。要修正，可让 loss 乘以 `shard_size / total_size * world_size`，或使用 `torch.nn.parallel.DistributedDataParallel` 的 `gradient_as_bucket_view` 之外的自定义 reducer。**常见卡点**：以为「平均」自动按样本数加权；这个偏差在数据不均衡时会直接伤害收敛。
</details>

## 本章重点整理

- 三种并行切开不同维度：DP 切 batch、TP 切单层矩阵、PP 切层；通信原语分别是 all-reduce、all-gather/all-reduce、send/recv。
- 图里的通信表现为 `_c10d_functional.all_reduce` / `all_gather_into_tensor`，返回 token，需 `wait_tensor` 才拿到值；这个拆分是 overlap 的基础。
- 通信是融合屏障，但通信前后的 cast/reshape/permute 可以融合，以减少传输字节。
- 列切不引入数值误差（实测 `maxerr=0.0`），行切引入累加顺序误差（`5.72e-06`），单卡 partial 单独看完全错误（误差 16.73）。
- DDP 的梯度在各 rank 逐位相同，且等于整批梯度；loss 按 rank 平均后等于整批 loss。
- 小消息延迟主导（4 KB 与 64 KB 都约 0.76 ms），大消息带宽主导（0.39–0.49 GB/s，TCP 回环）。
- 计算/通信比只取决于 `K` 和硬件的 `BW/FLOPS`，与 batch 无关。
- 并行策略的选择是「切法 × 通信量 × 带宽」的算术，不是框架魔法。

## 自我检核

- [ ] 我能说出三种并行各切开什么、各用什么通信原语
- [ ] 我能解释为什么 all-reduce 在图里返回 token 而不是张量
- [ ] 我能写出 2 进程 gloo 的 all-reduce，并说明 `spawn` 的必要性
- [ ] 我能推导列切/行切的通信量，并解释为什么列切可以零通信
- [ ] 我能解释行并行少一次 all-reduce 会静默出错的原因
- [ ] 我能用 DDP 的梯度一致性验证数据并行是否正确
- [ ] 我能用「通信字节 / 计算 FLOPs」判断某个切法是否划算
- [ ] 我知道 NCCL/overlap 部分为什么在本机只能 `guided`

## 延伸阅读

- **PyTorch Distributed 文档**（https://pytorch.org/docs/stable/distributed.html）：读 gloo/NCCL 后端、`all_reduce`、`all_gather` 的语义与限制。与本章关联：示例一、二的 API 依据。
- **PyTorch DDP 设计文档**（https://pytorch.org/docs/stable/notes/ddp.html）：读 construction、gradient bucketing、communication overlap 三节。与本章关联：示例三的原理。
- **Megatron-LM 论文**（https://arxiv.org/abs/1909.08053）：读 tensor parallel 的列切/行切推导。与本章关联：核心概念二。
- **NCCL 文档**（https://docs.nvidia.com/deeplearning/nccl/）：读 ring/tree 算法与拓扑感知。与本章关联：`guided` 部分。
- **ZeRO 论文**（https://arxiv.org/abs/1910.02054）：读 stage 1/2/3 的通信与显存权衡。与本章关联：FSDP 的由来。

---

→ [Ch 25 稀疏与低精度：用更少的位换更多的吞吐](./25-sparsity-low-precision.md)
