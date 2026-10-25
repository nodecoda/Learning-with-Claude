# Ch 7 算子融合：少一次内存往返，还是多一份寄存器压力

> **目标**：理解纵向、横向、reduction 和 matmul epilogue 四类融合；亲手用 TVM 把一个 `relu(x @ W + b)` 从三次 kernel 改成一次 kernel，拿到真实时间；同时知道融合什么时候会变慢、什么时候根本不该做。

## 为什么需要这个？

AI 编译器最常被提到的优化就是 fusion。它的收益很直观：少一次 kernel launch，少写一块中间结果，少读一次内存。但融合不是免费的：它可能增加寄存器压力、降低并行度、迫使编译器重复计算，或者把原本能调库的 matmul 变成一个手写 schedule。

这一章把 fusion 拆成四类，每类都给一个可运行的最小例子：

| 类型 | 形状 | 典型例子 | 主要收益 | 主要代价 |
|---|---|---|---|---|
| 纵向融合 | 生产者 → 消费者 | `relu(exp(x*2+1))` | 少中间张量、少 launch | 寄存器/局部内存压力 |
| 横向融合 | 同 shape 多分支 | `a=x*2+1, b=x*3-1` | 共享输入读取、少 launch | 掩码、分支 divergence、布局 |
| reduction 融合 | elementwise + reduce | `sum(relu(x), dim=1)` | 不落盘中间结果 | 并行度下降、累加顺序变化 |
| matmul epilogue | matmul + bias/激活 | `relu(x @ W + b)` | 省两次全量读写 | 调度变复杂、可能变慢 |

## 先建立直觉：融合改的是「谁和谁在同一个 kernel 里」

```text
纵向：  x -> mul -> add -> exp -> relu -> y
        └────────── 一个 kernel ──────────┘

横向：  x ─┬─> branch A ─> a
           └─> branch B ─> b
        └──── 一个 kernel，两个输出 ────┘

reduction：x -> relu -> sum(dim=1) -> r
           └──── 一个 kernel，边算边累加 ────┘

epilogue：A @ B -> C；C + bias -> D；relu(D) -> E
          └──── matmul 内直接写 E ────┘
```

图上看只是「把节点圈在一起」，真正的代价在 kernel 里：中间值放在寄存器、共享内存还是全局内存？循环怎么排？并行度够不够？这些是 Ch 11–16 的内容，本章先把「融合是否值得」判断清楚。

## 核心概念一：纵向融合把生产者塞进消费者

纵向融合（vertical fusion）是最常见的一种：生产者只被一个消费者使用，且两者都是 elementwise，编译器可以把中间值留在寄存器里，循环融合成一个。

用 TVM 构造一条链：

```python
import tvm
from tvm import te

N = 1 << 20
x = te.placeholder((N,), name="x")
y = te.compute(
    (N,),
    lambda i: tvm.tirx.max(
        tvm.tirx.exp(x[i] * 2.0 + 1.0),
        tvm.tirx.const(0.0, "float32"),
    ),
    name="y",
)
func = te.create_prim_func([x, y])
sch = tvm.s_tir.Schedule(func)
sch.parallel(sch.get_loops(sch.get_sblock("y"))[0])
f = tvm.build(sch.mod, target="llvm")
```

不融合时，同样一条链要四个 kernel：`mul`、`add`、`exp`、`relu`，每个都读写一遍 4 MB 的中间张量。本机实测（Windows 10，i7-10510U，4 核 8 线程，TVM 默认线程池；`time_evaluator`，`number=20`，`repeat=5`，取中位数）：

```text
unfused mul   median=6.3729 ms   min=1.7088  max=9.0041
unfused add   median=2.4504 ms   min=1.4475  max=3.8884
unfused exp   median=19.3066 ms  min=16.5590 max=22.3674
unfused relu  median=1.3014 ms   min=0.8666  max=1.9342
unfused sum   median=29.4314 ms
fused   fused median=16.2499 ms  min=14.3139 max=18.8359
chain maxerr fused: 0.0009765625
```

融合版比未融合版快约 1.81×。`exp` 本身很贵，所以收益不是「4 倍」；但省掉的 3 次中间读写仍然真实存在。`maxerr=9.77e-4` 是 `exp` 在 float32 下不同实现/顺序的正常误差，相对值约 `4e-8`，不是编译错误。

## 核心概念二：横向融合共享输入，但要小心形状

横向融合（horizontal fusion）把同 shape 的多个分支放进一个 kernel。下面的横向和 reduction 时间同样在本机测得，`time_evaluator(number=20, repeat=5)`，取中位数并给出 min/max。比如：

```python
a = x * 2.0 + 1.0
b = x * 3.0 - 1.0
```

两条分支都读 `x`、都写一个同 shape 的输出。理想情况下融合后一个 kernel 只读一次 `x`、写两个输出。实测用的是 stacked 输出 `(2, N)`，循环里每个 `i` 仍会读两次 `x`（`j=0` 和 `j=1` 各一次），所以收益主要来自少一次 launch 和更好的缓存局部性，而不是输入只读一次。实测（N=1,048,576）：

```text
a median=1.6634 ms  min=1.5319 max=2.0500
b median=1.7845 ms  min=1.3911 max=1.9017
separate sum=3.4479 ms
fused median=2.5606 ms min=2.0532 max=4.1523
fused vs separate: 1.35x
```

这组数说明横向融合有收益，但没有纵向那么夸张：输入读取省了一次，两个输出的写仍然要做；如果分支形状不同，还要加掩码或 padding，收益会被吃掉。

横向融合的三个前提：

1. **shape 兼容**：不同 shape 要么 padding 到同一循环域，要么分开处理尾部；padding 会做无用功。
2. **分支一致**：如果分支里有 `if`/数据相关控制流，融合后会出现 divergence 或 masked store。
3. **输出布局**：把两个输出拼成 `(2, N)` 是一种布局选择，可能让下游多一次 `permute`/copy；Ch 8 会讲布局传播。

## 核心概念三：reduction 融合省的是中间结果落盘

`sum(relu(x), dim=1)` 不融合时是两步：先写一个和 `x` 同 shape 的 `relu(x)`，再读它做 reduction。融合后在 reduction 的循环里直接算 `max(x,0)`，中间结果不落全局内存。

实测（M=4096，K=256）：

```text
unfused relu  median=2.2574 ms  min=1.4278 max=9.3945
unfused sum   median=1.9684 ms  min=1.8459 max=2.3907
unfused sum   median=4.2258 ms
fused   fused_sum_relu median=2.5693 ms min=2.0392 max=3.9047
reduction maxerr: 6.866455e-05
```

融合版约 1.64×。代价是 reduction 的累加顺序可能改变，浮点结果和逐行 `sum` 不完全一致；`6.87e-5` 的误差在 float32 下正常，但如果你的验收是 bitwise 相等，就会失败。

reduction 融合还会降低并行度：未融合时 relu 可以按元素大规模并行，融合后每个输出行只有一个 reduction 任务。输出行数很少、reduction 很长时，融合可能让 GPU/CPU 吃不满。这时可以考虑 `rfactor` 拆分 reduction、或用两阶段 reduction。

## 核心概念四：matmul epilogue 是收益最大也最容易翻车的一类

`y = relu(x @ W + b)` 在 eager 下是三次 kernel：

```text
C = x @ W          # 写 C
D = C + b          # 读 C，写 D
E = relu(D)        # 读 D，写 E
```

融合后，matmul 的累加结果还在寄存器/局部 buffer 里，就直接加 bias、做 relu，只写一次 E。TVM 里可以用 `compute_at` 把中间的 `C` 块放进 `D` 的循环里：

```python
import tvm
from tvm import te

M = N = 512
K = 32
A = te.placeholder((M, K), name="A")
B = te.placeholder((K, N), name="B")
bias = te.placeholder((N,), name="bias")
rk = te.reduce_axis((0, K), name="k")

C = te.compute((M, N), lambda i, j: te.sum(A[i, rk] * B[rk, j], axis=rk), name="C")
D = te.compute((M, N), lambda i, j: tvm.tirx.max(
    C[i, j] + bias[j], tvm.tirx.const(0.0, "float32")), name="D")

func = te.create_prim_func([A, B, bias, D])
sch = tvm.s_tir.Schedule(func)
loops = sch.get_loops(sch.get_sblock("D"))
io, ii = sch.split(loops[0], factors=[None, 16])
sch.reorder(io, ii, loops[1])
sch.compute_at(sch.get_sblock("C"), ii)
sch.parallel(io)
fused = tvm.build(sch.mod, target="llvm")
```

生成的 TIR 结构是「一个并行外层，里面先算 C 的一段，再算 D 的一段」：

```text
for i_0 in T.parallel(32):
  for i_1 in range(16):
    for ax0, ax1 in T.grid(512, 32):
      with T.sblock("C"):
        C[...] = C[...] + A[...] * B[...]
    for j in range(512):
      with T.sblock("D"):
        D[...] = T.max(C[...] + bias[j], 0.0)
```

注意 `C` 仍然是 `T.sblock_alloc_buffer((512, 512))`：TVM 没有把它缩成 16×512 的 tile，融合消掉的是「全局中间张量 + 额外 kernel launch」，不是所有临时内存。这是后面讲寄存器/局部内存压力的伏笔。

### 真实对比：K=32 与 K=256

同一台机器，M=N=512、K=32（更偏访存/启动开销）。TVM 的 matmul 用 `number=20`、add/relu 用 `number=50`、`repeat=5`；PyTorch 用 `blocked_autorange` 的 9 个样本：

```text
PyTorch eager (3 kernels): 1.1618 ms  (IQR 0.0754, 9 samples, 4 threads)
TVM unfused:  mm 16.3539 + add 0.1729 + relu 0.1023 = 16.6291 ms
TVM fused:    11.3767 ms (min 8.6529, max 13.7693)
fused / unfused: 1.46x
maxerr: 7.63e-06
```

M=N=256、K=256（更偏计算）：

```text
PyTorch eager (3 kernels): 1.2179 ms  (IQR 0.0810, 9 samples, 4 threads)
TVM unfused:  mm 19.5886 + add 0.0441 + relu 0.0413 = 19.6740 ms
TVM fused:    22.4490 ms (min 16.2370, max 29.7553)
fused / unfused: 0.88x
maxerr: 3.81e-05
```

未融合 matmul 的单次波动范围：K=32 为 min 12.169 / median 16.354 / max 24.368 ms；K=256 为 min 16.629 / median 19.589 / max 25.425 ms。第一组融合赢了 1.46×；第二组融合的中位数是未融合的 0.88×（约慢 14%），但 fused 的最小值（16.24 ms）和 unfused matmul 的最小值（16.63 ms）几乎一样，说明差异在测量波动范围内。**不能据此说「K=256 融合一定更慢」，但可以说：融合没有带来预期收益，调度/缓存效应把 epilogue 省下的那点流量吃掉了。** 这就是 fusion 的边界：它优化的是内存流量，不是 matmul 的 FLOPs；当 matmul 本身占绝对主导时，epilogue 融合的收益很小，甚至被 schedule 开销抵消。

## 底层机制：融合在 IR 里到底改了什么

未融合时，三份全局内存流量：

```text
matmul: 读 A, B；写 C
add:    读 C, bias；写 D
relu:   读 D；写 E
```

融合后：

```text
fused:  读 A, B, bias；写 E；C 只在 kernel 内部临时存在
```

以 K=32、M=N=512 为例，C/D/E 各 1 MB；未融合多出 `C 写 + C 读 + D 写 + D 读 = 4 MB` 流量。把 4 MB 从总流量里去掉，对访存占比高的形状就是实打实的收益。换到 K=256、M=N=256，C/D/E 各只有 256 KB，融合省掉约 1 MB；相对 33.5 MFLOP 的 matmul，这部分流量就不那么显眼。

融合的代价则体现在：

- **寄存器压力**：matmul 的累加器、bias、epilogue 中间值同时活着；tile 太大就 spill 到栈/局部内存。
- **重复计算**：一个生产者有多个消费者时，如果每个消费者都把它融进去，生产者会被算多次。编译器通常选择「融合一个消费者，另一个消费者读物化结果」。
- **并行度下降**：把 reduction 塞进 elementwise，可能把大量小任务变成一个长循环；把多个分支塞进一个 kernel，可能让循环体变大、占用率下降。
- **代码体积/编译时间**：每个融合变体都要生成/编译一份 kernel，autotuning 空间也会变大。

## 示例一：eager 三次 kernel 的时间

```python
import torch
from torch.utils.benchmark import Timer

N = 256
torch.manual_seed(0)
x = torch.randn(N, N)
w = torch.randn(N, N)
b = torch.randn(N)

t = Timer("torch.relu(x @ w + b)", globals={"torch": torch, "x": x, "w": w, "b": b})
m = t.blocked_autorange(min_run_time=1.0)
print(m.median * 1e3, m.iqr * 1e3, len(m.times), torch.get_num_threads())
```

实测：

```text
1.2179369997465983 0.0810179999098182 9 4
```

这是 PyTorch eager 的三次 kernel，走的是 MKL/oneDNN 的 matmul；它比本章手写 TVM schedule 快，不代表「融合没用」，而是说明「库的 matmul + 独立 elementwise」已经很强。融合要和同等级的 matmul 实现比，才有意义。

## 示例二：TVM 手写融合 matmul epilogue

完整脚本见 `.scratch/ai-compiler/ch7/matmul_k32.py` 和 `tvm_bench.py`。核心就是上面的 `compute_at`；验证分两步：

```python
fused(tA, tB, tBias, tD)
out = tD.numpy()
ref = np.maximum(a @ w + b, 0)
print(np.abs(out - ref).max())
```

实测 `maxerr=7.63e-06`（K=32）和 `3.81e-05`（K=256），都在 float32 的正常范围。时间表见上一节。

这里要特别说明：`torch.compile` 在本机跑不了。实测报错：

```text
InductorError
InvalidCxxCompiler: Compiler: cl is not found.
```

原因是 Windows CPU Inductor 需要 MSVC 的 `cl.exe` 做 C++ codegen，本机没装。这个结论标 `guided`：不是融合不可行，而是本机缺工具链。本章因此改用 TVM 手写融合，不伪造 `torch.compile` 的输出。

## 示例三：不该融合的真实反例

第一，K=256 的 matmul epilogue 融合中位数变慢（0.88×），原因见上：matmul 计算占主导，epilogue 流量占比小，融合没有带来足够收益。第二，TVM 的 `fuse_reduction_epilogue` 对 `BiasReLU` 有语义陷阱：

```python
sch.fuse_reduction_epilogue("C", "D")   # D = max(C + bias, 0)
```

实测（n=16）：

```text
maxerr 4.5045
ref[0,:4] [0.        2.2945943 2.2096086 4.7043023]
out[0,:4] [3.9860845 3.3259542 2.3651147 4.704303 ]
```

它把 ReLU 应用到每次 reduction 迭代上，而不是「先求和、再加 bias、最后 ReLU」。对非线性 epilogue，这种融合会改变语义。TVM 的文档也明确警告了这一点。正确做法是用 `compute_at` 保持「先完整 reduction，再 epilogue」，或者只用线性的 Bias 融合。

## 对比与取舍

| 融合 | 什么时候值得 | 什么时候不值得 |
|---|---|---|
| 纵向 elementwise | 生产者单消费者、链长、张量大 | 中间值很小、launch 开销可忽略 |
| 横向同 shape | 分支多、输入读取占主导 | 分支形状差异大、需要大量掩码 |
| reduction | 中间张量大、reduction 不太长 | 输出行少、reduction 很长、并行度不够 |
| matmul epilogue | K 较小、epilogue 流量占比高 | K 很大、matmul 计算主导、调度变差 |
| 多消费者生产者 | 只融合一个消费者，其他读物化结果 | 每个消费者都重复算生产者 |

一个实用判断：先算「融合能省多少字节」，再算「这个形状的算术强度」。省下来的流量占总流量比例很小，就不要期待大收益；算出来的 kernel 还要和库实现比，而不是只和「未融合的同一套 schedule」比。

## 踩雷集锦

1. **以为 `torch.compile` 在 Windows CPU 一定能跑**：本机缺 `cl.exe`，报 `InvalidCxxCompiler: Compiler: cl is not found.`；要么装 MSVC，要么换 TVM，不要编造结果。
2. **在 TE 里把 reduction 嵌进普通 compute**：会报 `Reductions are only allowed at the top level of compute`；先建 `C`，再用 epilogue/`compute_at`。
3. **滥用 `fuse_reduction_epilogue` 的 BiasReLU**：实测 `maxerr=4.5045`，因为 ReLU 被按迭代应用；非线性 epilogue 要验证语义。
4. **把 `sch.mod["main"].inspect_source()` 当 API**：0.27 的 `PrimFunc` 没有这个方法；打印 TIR 用 `sch.mod.script()`，构建后的 module 才用 `inspect_source()`。
5. **写 `tvm.tirx.float32(0)`**：0.27 没有这个属性；用 `tvm.tirx.const(0.0, "float32")`。
6. **只看 kernel 数，不看时间**：融合成一个 kernel 也可能更慢；K=256 的实测就是例子。
7. **忽略 C buffer 的分配**：`compute_at` 后 TVM 仍分配完整 C；大 shape 下这是实打实的内存压力。
8. **用 bitwise 相等验收 reduction 融合**：累加顺序变了，`allclose` 才合理；容差要按 dtype 和规模定。

## 进阶：再往深一层

- **`cache_read` / `cache_write`**：把 matmul 的 A/B tile 或 C 输出放进局部缓存，控制融合后的内存层次；这是 Ch 11–12 的 schedule 内容。
- **`rfactor`**：把 reduction 拆成两阶段（部分和 + 归约），恢复并行度；适合「输出少、reduction 长」的形状。
- **`decompose_reduction`**：把 reduction 的 init/update 拆开，便于把 epilogue 融到正确位置。
- **Inductor/XLA 的 fusion**：它们用 scheduler 决定哪些节点能融合，考虑的是读写量、tile 大小、并行度和代码体积；本章的手写 `compute_at` 是这套决策的最小版本。
- **Roofline**：用算术强度判断融合收益上限；Ch 16 会把这组 matmul 数字放到 roofline 图上。

## 动手练习

### 练习 1：改 tile，测时间

任务：把 `compute_at` 例子里的 `factors=[None, 16]` 改成 8、16、32，分别测 fused 时间。测试 M=N=512、K=32，记录三组中位数。

<details>
<summary>参考解答</summary>

用同一套 `time_evaluator`，只改 tile。tile 太小会增加循环开销/调度次数，太大增加 C buffer 和寄存器压力；本机上 16 左右通常比较稳，但要以你的测量为准。不要只测一次，至少 `repeat=5` 看 min/max。

</details>

### 练习 2：融合 `tanh(x*2+1)`

任务：把纵向链的 `exp` 换成 `tanh`，比较 4 个未融合 kernel 和 1 个融合 kernel 的时间。测试 N=1,048,576。

<details>
<summary>参考解答</summary>

TIR 里写成 `tvm.tirx.tanh(x[i]*2.0+1.0)`。`tanh` 比 `exp` 便宜，融合收益主要来自省掉的中间读写；如果时间差距变小，说明计算占比下降、访存占比上升，符合预期。

</details>

### 练习 3：不同 shape 的横向融合

任务：`a` 的 shape 是 `(N,)`，`b` 的 shape 是 `(N//2,)`；设计一个带掩码的融合 kernel，比较「两个 kernel」和「一个 kernel + 掩码」的时间。测试 N=1,048,576。

<details>
<summary>参考解答</summary>

融合循环跑 `N` 次，`b` 只在 `i < N//2` 时写；用 `if_then_else` 或 `if` 保护。掩码会让一半迭代做无用功，且写不连续；如果 b 的分支很便宜，分开两个 kernel 往往更好。记录两种方案的时间，不要假设融合一定赢。

</details>

### 练习 4：GELU epilogue

任务：把 `relu(x @ W + b)` 换成 `gelu(x @ W + b)`，用 `compute_at` 融合。测试 K=32、M=N=512，和「matmul + 独立 gelu」比较。

<details>
<summary>参考解答</summary>

GELU 可以用 `0.5*x*(1+tanh(sqrt(2/pi)*(x+0.044715*x^3)))` 近似，或用 `tvm.tirx.erf`。`compute_at` 不关心 epilogue 是不是 ReLU，只要表达式能写出来；`fuse_reduction_epilogue` 的固定模式反而不支持 GELU，所以手写 `compute_at` 更灵活。验证 `maxerr` 时用同一 GELU 公式做参考。

</details>

### 练习 5：只用线性的 Bias 融合

任务：用 `fuse_reduction_epilogue("C","D")`，其中 `D = C + bias`（不加 ReLU），验证数值完全一致，再和 `compute_at` 版本比时间。

<details>
<summary>参考解答</summary>

线性 Bias 融合不会改变累加语义，`maxerr` 应该接近 0（浮点顺序可能带来 1e-6 量级差异）。比较时注意 `fuse_reduction_epilogue` 生成的循环结构和 `compute_at` 不同；用同一 matmul tile 才有可比性。

</details>

## 本章重点整理

- 纵向融合省中间张量和 launch；横向融合省输入读取和 launch；reduction 融合省中间结果落盘；epilogue 融合省 matmul 输出的全量读写。
- 融合的收益上限由「省下的字节 / 总字节」和算术强度决定，不是由 kernel 数量决定。
- matmul epilogue 在 K 小、epilogue 占比高时收益明显；K 大时可能被 schedule 开销抵消。
- 融合的代价：寄存器压力、重复计算、并行度下降、代码体积、数值顺序变化。
- `compute_at` 能保持「先 reduction 再 epilogue」；`fuse_reduction_epilogue` 的 BiasReLU 会按迭代应用 ReLU，语义会变。
- 本机 `torch.compile` 缺 MSVC `cl`，CPU Inductor 跑不了；本章用 TVM 手写融合，GPU 内容标 `guided`。
- 所有时间数字都要注明机器、样本数、波动范围；不要把一次测量当绝对值。

## 自我检核

- [ ] 我能画出纵向/横向/reduction/epilogue 四种融合的 before/after 图
- [ ] 我能解释融合为什么省内存流量，以及为什么不一定省时间
- [ ] 我能用 TVM `compute_at` 写出 matmul + bias + relu 的单 kernel
- [ ] 我知道 `fuse_reduction_epilogue` 的 BiasReLU 为什么会改变语义
- [ ] 我能说出至少三个融合的代价
- [ ] 我能解释 K=32 融合赢、K=256 融合没赢的原因
- [ ] 我知道本机 `torch.compile` 失败的真实原因
- [ ] 我会用 `time_evaluator` 报中位数和 min/max，而不是只报一个数

## 延伸阅读

- **TVM 官方文档**（https://tvm.apache.org/docs/）：读 schedule 原语中的 `compute_at`、`rfactor`、`decompose_reduction`。与本章的关联：融合的调度实现。
- **TorchInductor 设计讨论**（https://dev-discuss.pytorch.org/）：搜 fusion/scheduler。与本章的关联：生产编译器如何决定融合边界。
- **XLA/StableHLO fusion**（https://openxla.org/stablehlo）：读 fusion pass 的收益模型。与本章的关联：另一套融合决策。
- **TVM 论文 (OSDI 2018)**（https://www.usenix.org/conference/osdi18/presentation/chen）：读 schedule 与 lowering 部分。与本章的关联：计算与调度分离的理论基础。
- **Ch 16 内存层次与 Roofline**：本章的时间数字会在那里被解释成带宽/算力上限；融合只是改变流量，不改变硬件的峰值。

---

→ [Ch 8 布局与格式](./08-layout-and-format.md)
