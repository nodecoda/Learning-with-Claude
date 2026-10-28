# Ch 8 — 调度：为什么融合之后还要 tiling

> **目标**：搞清「融合」和「调度」解决的是两个不同问题：前者决定有几个 kernel，后者决定一个 kernel 内部循环怎么排。学完这章，你能看懂生成代码里的循环边界与步长是怎么来的，能估算一个 tile 大小对 cache 意味着什么，并知道为什么调度参数不能跨平台照抄。

> **环境**：torch 2.9.1+cpu、Python 3.10、x86_64（AVX2）、Ubuntu 22.04。对照数据涉及 ARM NEON 与 AVX-512，标注为按位宽推算，未在本机实测。

## 为什么需要这个？

Ch 7 结束时，我们把 5 个 elementwise 算子压成了 1 个 kernel。看起来优化到底了。

但 fusion 只回答了「有几个 kernel」。它没有回答：

- 这个 kernel 里，循环从哪一层开始？
- 一次处理多少元素？
- 中间结果放在寄存器、栈，还是重新读一遍内存？

这三个问题合起来叫**调度（scheduling）**。它决定的是**同一个 kernel 的实际耗时**，而融合只决定 launch 次数。

一个具体差距。两个实现，都只启动一个 kernel，都算 `C = A @ B`（512×512 的 `float32`）：

| 实现 | 循环结构 | 预期表现 |
|---|---|---|
| 朴素三重循环 | i, j, k 顺序，每次读 A[i][k]、B[k][j] | 通常比高度优化的库慢一个数量级以上 |
| 分块 + 重排 | 外层按 64×64 分块，内层 k 连续 | 量级上接近 BLAS |

第二列的「数量级」是按访存次数推算的量级判断，**本章没有做这个基准测试**。想自己验证，用 `perf stat -e cache-misses` 分别跑两个版本，比时间更直接——时间会受频率和噪声影响，miss 计数不会。

差别不在指令数量——两者的乘加次数完全一样（512³ = 1.34 亿次）。差别全在**数据从哪一层存储取**。这就是调度。

## 先建立直觉

把调度想成搬家时往柜子里塞东西。

```
你的柜子（cache）        你的仓库（DRAM）

  ┌──────────┐            ┌────────────────────┐
  │  L1 32KB │            │  几 GB，容量大       │
  │  L2 256KB│            │  但每次取一件       │
  │  L3 8MB  │            │  要走很远           │
  └──────────┘            └────────────────────┘

 调度 = 决定「一次搬多少、按什么顺序搬」

 搬太少：      来回跑的次数多（循环开销、地址计算）
 搬太多：      柜子塞不下，刚放进去的又被挤出来（capacity miss）
 顺序不对：    每次要用的东西不在手上，白跑
```

关键约束是**工作集（working set）**：一个循环体内同时活跃的数据量。工作集塞得进某一层 cache，那一层的带宽才用得上；塞不进，就退化到下一层。

## 底层机制：三种循环变换

调度器对一张循环嵌套图做变换，本质上只有三类动作。理解了这三类，任何生成代码都能读。

```
原始循环（elementwise，一维）：
  for x0 in 0..N:
      out[x0] = f(in[x0])

变换一：向量化（vectorize）
  for x0 in 0..N step 8:
      out[x0:x0+8] = f(in[x0:x0+8])      ← 一条 SIMD 指令处理 8 个

变换二：分块（tile / blocking）
  for x0_outer in 0..N step T:
      for x0_inner in x0_outer..x0_outer+T:
          ...                             ← 让 T 份数据留在 cache

变换三：重排（reorder / interchange）
  for k 在外、i 在内  ──▶  for i 在外、k 在内
                                          ← 改变访问的局部性
```

真实调度器会把三者组合，并按目标硬件挑参数。下面逐个拆。

## 机制一：向量化宽度从哪来

### 实测：这个 8 是哪来的

Inductor 为 8192 个元素生成的循环是：

```cpp
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(8192L); x0+=static_cast<int64_t>(8L))
```

步长 8。来源是一次位宽除法：

```
AVX2 的 YMM 寄存器 = 256 bit
float = 32 bit
256 / 32 = 8
```

本机 `lscpu` 只报 `avx2`，没有 `avx512f`，所以是 8。三档对照：

| 目标 | 向量寄存器位宽 | 每指令 float 数 | 依据 |
|---|---|---|---|
| ARM NEON（AArch64 基线） | 128 bit | 4 | 128 / 32 |
| x86 AVX2 | 256 bit | 8 | 实测 |
| x86 AVX-512 | 512 bit | 16 | 512 / 32 |

后两行的数字是按位宽推算的，NEON 为 4、AVX-512 为 16 这两个值没有在本机跑出来——本机没有这两种硬件。你若在树莓派上跑同一个函数，产物里的步长会变成 4，这是可以直接验证的。

> 注意宽度不等于「一定快 8 倍」。向量化的收益受限于内存带宽：数据从 DRAM 来的时候，瓶颈在搬运，不在 ALU。这就是为什么 Ch 7 的融合（减少搬运）和本章的调度（利用 cache）必须一起看。

### 实测：非整除时的两套代码

1001 个元素，不是 8 的倍数。生成代码里出现两套：

```cpp
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(1001L); x0+=static_cast<int64_t>(8L))
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
    ...
    if(C10_UNLIKELY(x0 >= static_cast<int64_t>(1000L) && x0 < static_cast<int64_t>(1001L)))
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(1L));
        tmp7.store(out_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(1L));
```

- 主循环：从 0 到 1001，步长 8，每次 `loadu(..., 8)` 和 `store(..., 8)`
- 尾部：条件 `x0 >= 1000 && x0 < 1001`，`loadu(..., 1L)` + `store(..., 1L)`

为什么主循环敢跑到 1001？因为每次迭代都有 `x0 < 1001` 的检查，最后一步 `x0 = 1000` 时只剩 1 个有效元素，交给尾部处理。

对比 1000 个元素（8 的倍数）：

```
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(1000L); x0+=static_cast<int64_t>(8L))
...
if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(1000L)))
```

**没有尾部代码**。1000 = 125 × 8。

代价量化：尾部代码不是「多一个 if」那么轻。它带来第二套 load/store 序列、额外的分支、以及分支预测器里一个几乎不成立的方向。在 x86 上这是几百字节机器码和几个周期；在 Flash 512 KB 的 MCU 上这是**能不能装下**的问题。

### 推论：尺寸对齐是调度的一部分

```
N % 8 == 0   → 一套向量循环
N % 8 != 0   → 主循环 + 尾部，代码体积与分支都增加
```

所以模型里的维度（channel 数、hidden size、序列长度）设计成 4/8/16 的倍数，不只是审美。BERT 的 hidden 768、GPT 的 4096 都是这种考虑的产物。

## 机制二：tile 大小与 cache 层次

### 用数字算一次

假设 L1 cache 32 KB（典型 x86 与 Cortex-A 的取值，具体看型号）。一个 `float` 4 字节：

```
32 KB = 32768 字节
32768 / 4 = 8192 个 float
```

一个 8192 元素的向量正好填满 L1。如果 kernel 要同时读 3 个输入向量（Ch 7 的三输入融合），工作集是 3 × 8192 × 4 = 96 KB，**超了 L1 三倍**。

这就引出一个具体问题：三输入融合省下了 DRAM 往返，但会不会反而把 L1 挤爆？

在 256 元素的小例子里不会：工作集 3 × 1 KB = 3 KB，L1 里躺得很舒服。放到 100 万元素上就要重新算：

```
总数据 3 × 1e6 × 4 = 12 MB
L1 32 KB 装不下  →  分块
一次处理 T 个元素，工作集 3 × T × 4 字节
要求 12T ≤ 32768  →  T ≤ 2730
取 8 的倍数且留余量：T = 2048
```

于是循环变成两层：

```
for x0_outer in 0 .. N step 2048:
    for x0_inner in 0 .. 2048 step 8:      ← 12 KB 工作集，留在 L1
        load 三个输入各 8 个
        算
        store 8 个
```

第一层负责「把数据搬进 cache 附近」，第二层负责「在 cache 里榨干它」。**tiling 的本质就是这个两层结构**：外层切出能装下的块，内层在块内做向量化。

### 为什么 tile 不能随便设

| tile 偏小 | tile 偏大 |
|---|---|
| 外层迭代次数多，循环开销与地址计算占比升高 | 工作集超过 L1，退化到 L2（延迟高一个量级） |
| 每次块内只做很少的活，流水线填不满 | 超过 L2 就退化到 L3 / DRAM |
| 但 cache 命中率高，行为可预测 | 出现 capacity miss，且难以预测 |

有一条经验规律值得记住：**tile 调小的损失是线性的（多几次迭代），tile 调大的损失是阶跃的（跨过 cache 容量就跳一档）**。所以调参时从保守的小值往上加，比从大值往下减安全。

### 矩阵乘为什么必须分块

`C = A @ B`，三者都是 N×N。朴素实现的内层是：

```
for i: for j: for k: C[i][j] += A[i][k] * B[k][j]
```

访问 B 的时候，`B[k][j]` 沿 k 方向走是**跨行**的——每走一步跳 N×4 字节。N = 512 时一次跳 2 KB，每次都 miss。

分块后：

```
for i0 in step Ti: for k0 in step Tk: for j0 in step Tj:
    for i in i0..: for k in k0..: for j in j0..:
        C[i][j] += A[i][k] * B[k][j]
```

内层三个循环遍历的是一个 Ti×Tk 与 Tk×Tj 的小块，两块都能留在 cache。这就是「矩阵乘的性能来自分块，不来自指令」的含义。如果你在 Ch 15 看到指令选择讨论 FMA 的收益，记住：FMA 省的是指令条数，分块省的是内存等待，后者通常大一个量级。

## 机制三：循环顺序

同样的三重循环，六种排列（i/j/k 的 3! 种）性能可以差几倍，因为它们决定了两件事：哪个数组被顺序访问、以及累加器 `C[i][j]` 能不能一直留在寄存器里。

| 顺序（外→内） | 内层变量 | 连续访问 | 跨步访问 | 累加器位置 | 结论 |
|---|---|---|---|---|---|
| i, j, k | k | A | B（每步跳 N×4 字节） | 寄存器 `C[i][j]` | 累加器省了，但 B 每次 miss；需分块补救 |
| i, k, j | j | B、C | 无（`A[i][k]` 每轮只读一个标量） | 寄存器 `acc` | 分块后的常见选择 |
| k, i, j | j | B、C | 无 | 内存 `C[i][j]`（k 每变一次都要重读重写） | 访存连续，但累加器落内存，通常更差 |

**没有一个顺序永远最好。**它取决于矩阵的行列主序（C 是行主序，Fortran/BLAS 是列主序）、cache 大小、以及是否分块。这就是为什么调度是一个搜索问题。

## 调度是搜索问题，不是公式

如果调度只是「按位宽除以 32」，那就不需要编译器了。真实情况是参数空间很大：

```
搜索空间（一个矩阵乘的调度）
  循环顺序        : 6 种（更多层时更多）
  tile 大小       : Ti × Tj × Tk 的组合，每个 16..256，几十到上百种
  向量化宽度      : 4 / 8 / 16，以及是否对某个维度做
  展开（unroll）  : 1 / 2 / 4 / 8
  并行切分        : 线程数、切哪一维
  ─────────────────────────────────
  组合数很容易到 1e4 以上
```

Inductor 的两条应对路线：

| 路线 | 机制 | 代价 |
|---|---|---|
| 启发式（默认） | 按规则与模板直接定参数 | 编译快，参数不一定最优 |
| 自动调优（`max_autotune`） | 真的编译多个候选，跑一遍挑最快 | 编译慢几十倍，且需要目标硬件在场 |

第二条路在边缘部署里有个硬问题：**目标硬件可能不在编译机上**。在 x86 服务器上给 Cortex-M 调 tile 参数，测出来的是 x86 的时间。交叉编译场景下，只能用代价模型（cost model）估算，或者上板测量后回填。

> 这也是 TVM 的 AutoTVM / Ansor 这条线的核心问题：如何用少量实测样本拟合出一个能预测未见配置的模型。Ch 7 延伸阅读里那篇 *Learning to Optimize Tensor Programs* 就是起点。

## 实测：一个 8192 元素 kernel 的完整调度解读

把 Ch 6 的 `cpp_fused_add_relu_0` 逐行拆开，每个数字归因到某个决定：

```cpp
for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(8192L); x0+=static_cast<int64_t>(8L))
{
    if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(8192L)))
    {
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_out_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
        auto tmp3 = tmp0 + tmp2;
        auto tmp4 = at::vec::clamp_min(tmp3, decltype(tmp3)(0));
        tmp4.store(in_out_ptr0 + static_cast<int64_t>(x0));
    }
}
```

| 代码元素 | 决定 | 依据 |
|---|---|---|
| `8192` | 总工作量 | 128 × 64，形状在编译期已知 |
| `+= 8` | 向量宽度 | AVX2 256 bit / 32 bit |
| 只有一层 `x0` | 不分块 | 原地更新，工作集只有一块 8192 × 4 = 32 KB，正好压在 L1 边界；顺序流式访问不产生复用，分块无收益 |
| `C10_LIKELY` | 边界检查保留 | 正确性优先，代价一次比较 |
| `in_out_ptr0` 读写同址 | 原地更新 | 输入输出同 buffer，省一次分配 |

最后一行值得注意：这个 kernel 只有 **一个** 指针参数，`in_out_ptr0` 既读又写。因为 `relu(x + 1)` 的输出和输入同形状、且输入不再需要，调度器选择了原地（in-place）执行。这一条同时省了内存分配和一次 cache 污染。

**elementwise 为什么不需要 tiling**：它没有数据复用。每个元素读一次、写一次、算一次，不留任何中间状态。分块的价值来自「同一份数据被多次使用」，elementwise 里这个前提不存在。所以你在 elementwise 产物里看不到两层循环，这是对的，不是编译器偷懒。

## 边界：什么时候调度会失手

### 例一：tile 太大，跨过 cache

```c
/* 假设 L1 = 32 KB，每个 tile 读 A、B 两块 */
#define T 128
for (i0 = 0; i0 < N; i0 += T)
  for (j0 = 0; j0 < N; j0 += T)
    for (i = i0; i < i0+T; i++)
      for (j = j0; j < j0+T; j++)
        ... A[i][k] * B[k][j] ...

工作集 = T*T*4*2 = 128*128*4*2 = 131072 字节 = 128 KB
L1 只有 32 KB  → 必然溢出到 L2
```

把 T 降到 32：`32*32*4*2 = 8 KB`，舒服地留在 L1。但这时外层迭代次数变成 16 倍，每块只做 1024 次乘加，循环开销开始显眼。**这就是「太小」与「太大」之间的实际取舍**，没有唯一答案，要靠测量。

### 例二：抄服务器的参数到 MCU

服务器上 `T = 128` 分块，工作集 128 KB，L2 里跑得很好。搬到 Cortex-M7（L1 数据 cache 通常 16 KB 或 32 KB，SRAM 几百 KB）上：

```
工作集 128 KB > 可用 SRAM 的一块
→ 要么编译不过（静态分配失败）
→ 要么链接过了但栈溢出，运行时 HardFault
```

更隐蔽的一种：链接过了，也没崩，但每次 tile 访问都 miss，性能比不分块还差。**这类问题不会报错**，只会慢。

### 例三：向量化宽度写死

```c
/* 从 x86 版本抄过来的 */
for (int i = 0; i < n; i += 8) { ... }
```

在 NEON 上编译能过（`float32x4` 确实存在，你只是每次只用了一半寄存器），但有一半的 SIMD 能力浪费。在 Cortex-M4F 上，`float32x4_t` 不存在，编译直接失败。**宽度必须从目标特性来，不能从记忆里来。**

## 踩雷集锦

1. **以为融合了就优化完了**：融合减少 kernel 数与 DRAM 往返，调度决定 cache 命中与指令效率。一个融合完美的 kernel 如果 tile 设错，可能比不融合还慢。
2. **以为 tile 越大越好**：tile 增大的收益到 cache 容量处**断崖**。32 KB 的 L1 装 8192 个 float，三个输入向量就只能各分到 2730 个元素。超过这个数，收益反转。
3. **以为向量宽度是常数 8**：8 是 AVX2 的结果。NEON 是 4，AVX-512 是 16，标量目标上是 1。写死 8 的性能模型在所有非 AVX2 平台上都是错的。
4. **以为 elementwise 也需要分块**：分块的收益来自数据复用。elementwise 每个元素只用一次，分块只会增加循环开销。看到生成代码里没有 tiling，先确认是不是这个原因。
5. **以为调度参数可以跨平台复制**：服务器的 `T=128` 到 MCU 上是 OOM 或 cache 全 miss。调度参数是（硬件, 形状）的函数，不是常量。

## 本章重点整理

- 融合决定「几个 kernel」，调度决定「一个 kernel 多快」。两者解决不同问题，缺一不可。
- 三种循环变换：向量化（宽度 = 寄存器位宽 / 元素位宽）、分块（让工作集进 cache）、重排（决定谁被顺序访问）。
- 向量宽度：AVX2 → 8（实测），NEON → 4，AVX-512 → 16（按位宽推算）。
- 尺寸不对齐会生成「主循环 + 尾部」两套代码。1001 与 1000 的差别就是这个。
- L1 32 KB 装 8192 个 float。三输入融合时，tile 上界由 `3 × T × 4 ≤ 32768` 给出。
- 调度是搜索问题：参数空间上万种组合，靠启发式或 autotune，没有闭式解。
- elementwise 不需要 tiling，因为没有数据复用——这是判断「生成代码是否合理」的一个快速检查点。

## 自我检核

- [ ] 不看笔记，说出融合与调度各自解决什么问题，并各举一个「只做其中一个会失败」的场景
- [ ] 给你一个 1001 元素的 elementwise 函数，你能说出产物里会有几套 load/store 序列，以及为什么
- [ ] 推导：L1 32 KB、三输入融合、`float32`，tile 上界是多少？换成 `int8` 呢？
- [ ] 解释为什么 elementwise 不需要 tiling，而矩阵乘必须分块
- [ ] 说出「tile 太小」和「tile 太大」各自的代价，以及为什么后者更危险
- [ ] 为什么在 x86 上 autotune 出的参数不能直接给 Cortex-M 用？说出至少两个理由

## 延伸阅读

- **[Learning to Optimize Tensor Programs](https://arxiv.org/abs/1802.04799)** — Chen et al., NeurIPS 2018
  - **读哪里**：Section 3（cost model 设计）、Section 4（搜索算法）；实验部分可略读
  - **学什么**：用梯度提升树学习「配置 → 性能」，把调度搜索从暴力枚举降到几十次测量
  - **关联**：本章「调度是搜索问题」的完整解法；TVM AutoTVM 的理论基础
  - **前提**：知道循环变换的基本形态（本章前三节即可），不需要机器学习背景

- **[Ansor: Generating High-Performance Tensor Programs for Deep Learning](https://www.usenix.org/conference/osdi20/presentation/zheng)** — Zheng et al., OSDI 2020
  - **读哪里**：Section 4（sketch generation）、Section 5（fine-tuning）
  - **学什么**：先用草图定结构、再用进化搜索填参数，解决 AutoTVM 模板需手写的问题
  - **关联**：本章「搜索空间」那张清单的工业化版本，解释了为什么要分层搜
  - **前提**：读完上面那篇，或对 TVM 的 schedule 有概念

- **[TVM — Schedule Primitives](https://tvm.apache.org/docs/tutorials/schedule_primitives.html)**
  - **读哪里**：`split`、`reorder`、`tile`、`vectorize` 四个原语的示例
  - **学什么**：本章三种变换在真实 API 里的样子；`tile` 就是 `split` + `reorder` 的组合
  - **关联**：看懂这段，再读 Inductor 生成的 C++ 就有对照物
  - **前提**：能读 Python；不需要装 TVM，读示例即可

- **[Intel — Optimization Reference Manual](https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html)**
  - **读哪里**：Volume 1 的 cache 与 memory 章节，按微架构查 L1/L2 容量与 associativity
  - **学什么**：本章所有 cache 数字的权威来源；不同微架构的 L1 大小并不相同
  - **关联**：你算 tile 上界时用的 32 KB 从这里查，别用记忆
  - **前提**：无；按需查表，不要通读

- **《Computer Architecture: A Quantitative Approach》** — Hennessy & Patterson（第 6 版，2017）
  - **读哪里**：第 2 章（Memory Hierarchy Design）、第 4 章（Data-Level Parallelism）
  - **学什么**：cache 缺失的三种类型（compulsory / capacity / conflict），本章 tile 讨论全建立在这个分类上
  - **关联**：Ch 6 讲向量宽度、本章讲 tile，两章共用这份理论基础
  - **前提**：知道什么是 cache、什么是流水线；第 6 版的 GPU 章节与本章无关，可跳

调度解决了「循环怎么排」，但排完之后还得**变成某种东西**。下一章看 Inductor 的出口：它把调度结果写成什么，又交给了谁。

→ [Ch 9 代码生成的三条出口](./09-codegen-backends.md)
