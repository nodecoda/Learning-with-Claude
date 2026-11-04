# Ch 12 — TritonGPU 布局代数

> 到了 TTGIR，tensor 不再只是“形状 + 类型”，每个值还背着一个编译期映射函数：哪个 CTA、哪个 warp、哪个 lane、第几个寄存器，负责哪一个逻辑元素。

## 目标

读完本章，你应该能：

- 说清楚 `#ttg.blocked` 的 `sizePerThread`、`threadsPerWarp`、`warpsPerCTA`、`order` 各自控制什么；
- 给定一个 128×128 的 f32 tensor 和 4 个 warp，手算出某个线程持有哪几个元素；
- 解释为什么 `order=[1,0]` 的 load 是 coalesced，而 `order=[0,1]` 会把一次 128B 访问拆成 32 个 32B sector；
- 看懂 `#ttg.nvidia_mma` 的 lane/register 分布，知道它和 blocked 布局的本质差别；
- 说出 `#ttg.dot_op`、`#ttg.slice`、`#ttg.swizzled_shared`、`#ttg.nvmma_shared`、`#ttg.linear`、CGA 各自解决什么问题；
- 判断一个 shared memory 布局在什么访问模式下会 bank conflict，以及 swizzle/padding 为什么能缓解。

本章只讲“布局是什么、怎么算”。布局之间的等价判断、组合、求逆，以及 `convert_layout` 的消除，放到 Ch 13 和 Ch 14。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 3.9.0。
- 机器：Tesla T4，compute capability 7.5（`sm_75`），驱动 535.216.01，CUDA 12.4。
- 本章源码行号以该 commit 为准，使用 `nl -ba` 核对。
- **实测**：本章的线程/元素坐标表、bank conflict 计数，来自一个独立 Python 脚本。它按 `BlockedEncodingAttr::toLinearLayout`、`nvidiaMmaTile`、`SwizzledSharedEncodingAttr::toLinearLayout` 的公式复算，不是 Triton 工具输出，也不是 GPU 运行结果。
- **源码依据**：布局字段语义、verifier、默认布局、CGA 构造来自源码。
- **guided**：`num_ctas > 1` 的 CGA 例子、`#ttg.nvmma_shared` 的 WGMMA/TMA 用法、T4 上的 f32 tensor-core 行为。T4 不支持 Hopper/Blackwell 的这些路径，不能把结论直接套到本机。

## 为什么需要这个

TTIR 里的 `tensor<128x128xf32>` 只说明“有 16384 个 f32”。TTGIR 里的 `tensor<128x128xf32, #blocked>` 还要回答一个更物理的问题：

> 这 16384 个元素，分别躺在哪个线程的哪个寄存器里？

这个问题直接决定性能：

- global load/store 是否 coalesced；
- `tt.dot` 的 operand 是否匹配 MMA 指令；
- shared memory 是否 bank conflict；
- `tt.reduce` 的跨 lane 通信走 shuffle 还是 shared memory；
- 两个 op 之间要不要插 `ttg.convert_layout`。

所以 TTGIR 的 encoding 不是“注释”，而是类型的一部分。两个 tensor 形状和 dtype 相同、encoding 不同，就不能直接相接；编译器必须插入转换，或者改写 producer/consumer 的布局。

本章的主线是：**把 encoding 读成一个映射函数**。只要你能把 `(reg, lane, warp, block)` 映射到 `(i, j)`，后面所有 pass 的行为都能解释。

## 先建立直觉

先看一个最小的 blocked encoding：

```mlir
#blocked = #ttg.blocked<{
  sizePerThread = [1, 1],
  threadsPerWarp = [1, 32],
  warpsPerCTA = [1, 4],
  order = [1, 0]
}>
```

把它读成一句话：

- 每个线程持有 1×1 个元素（`sizePerThread`）；
- 一个 warp 的 32 个 lane 沿第 1 维（列）排开（`threadsPerWarp=[1,32]`）；
- 4 个 warp 继续沿第 1 维排开（`warpsPerCTA=[1,4]`）；
- 第 1 维是最快变化的维度（`order=[1,0]`，`order[0]=1`）。

于是 128 个线程覆盖了 128 列；第 0 维（行）没有线程覆盖，只能靠“每个线程多拿几个寄存器”来重复。对一个 128×128 tensor，每个线程最终持有 128 个元素：固定列 `c = 32*warp + lane`，行从 0 到 127。

这就是 blocked 布局的核心：**线程铺一层 tile，tensor 比 tile 大就重复（wrap），比 tile 小就广播（broadcast）**。源码把这件事写得很清楚：`DistributedEncoding` 的说明在 `include/triton/Dialect/TritonGPU/IR/TritonGPUAttrDefs.td:611-661`。

`源码依据`：blocked 的四个字段和“warp 拥有连续区域”的说明在 `TritonGPUAttrDefs.td:752-915`；默认 blocked 的构造在 `lib/Dialect/TritonGPU/IR/Dialect.cpp:742-755`。

## 核心概念

### 1. encoding 是类型元数据，不是运行时对象

`tensor<128x128xf32, #blocked>` 里的 `#blocked` 是 MLIR attribute。它不占寄存器，也不生成指令；它只告诉 lowering：“如果你要访问这个 tensor 的第 (i,j) 个元素，请用这个公式算出它在哪个线程的哪个寄存器。”

到了 `TritonGPUToLLVM`，`toLinearLayout` 会把 encoding 变成线性布局，再生成索引算术。dispatcher 在 `lib/Dialect/TritonGPU/IR/LinearLayoutConversions.cpp:1193-1227`，按 `DistributedEncodingTrait`、`SharedEncodingTrait`、TMEM 分支。

所以布局是**编译期映射函数**：同一颗 kernel 在编译时选定一种映射，运行时没有“布局分支”。

### 2. blocked 的四个字段

| 字段 | 含义 | 常见误解 |
|---|---|---|
| `sizePerThread` | 每个线程持有的寄存器 tile 形状 | 不是“线程数”，也不是“向量化宽度” |
| `threadsPerWarp` | 一个 warp 内 lane 在各维的分布 | 乘积通常等于 32，但 verifier 只检查每项是 2 的幂 |
| `warpsPerCTA` | 一个 CTA 内 warp 在各维的分布 | 不是 grid 的 block 数；CTA 之间由 CGA 管 |
| `order` | 维度从最快到最慢的排列 | `order[0]` 是最快变化的维度，不是“内存行优先”的口号 |

verifier 要求四个数组 rank 相同、每项是 2 的幂、`order` 是 `0..rank-1` 的排列，并且 CGA rank 匹配。源码在 `lib/Dialect/TritonGPU/IR/Dialect.cpp:705-740` 和 `604-613`。

### 3. 默认 blocked 长什么样

`getDefaultBlockedEncoding` 的规则非常朴素：

- `sizePerThread` 全 1；
- `order` 是 `reverse(0..rank-1)`，也就是最后一维最快；
- `threadsPerWarp` / `warpsPerCTA` 由 shape 和 `numWarps` 推出来。

源码：`lib/Dialect/TritonGPU/IR/Dialect.cpp:742-755`。

对一个 128×128 tensor、4 warps、32 threads/warp，得到的就是本章开头那个：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [1, 32], warpsPerCTA = [1, 4], order = [1, 0]}>
```

注意它只覆盖 1×128 的 tile；行方向的 128 个元素靠 register 重复。`源码依据`：`BlockedEncodingAttr` 的 builder 在 `TritonGPUAttrDefs.td:845-909`（主 builder `846-881`），它从 `order[0]` 开始分配 lane/warp，最后把剩余 lane/warp 塞给 `order[rank-1]`。

### 4. 布局函数：tile、wrap、broadcast

把 blocked 写成函数：

```text
tile[d] = sizePerThread[d] * threadsPerWarp[d] * warpsPerCTA[d]
if shape[d] > tile[d]:  register 位继续沿 d 维展开（wrap/repeat）
if shape[d] < tile[d]:  多出来的 lane/warp 位映射到 0（broadcast）
```

这就是 `DistributedEncoding` 描述里的“wrapped around”和“broadcasted”两种行为（`TritonGPUAttrDefs.td:611-661`）。

`#ttg.slice` 也是同一个思路：把 parent 的某一维“挤掉”，让 rank 减一。源码在 `TritonGPUAttrDefs.td:1363-1412`，`toLinearLayout` 在 `LinearLayoutConversions.cpp:1037-1049`。

## 底层机制

### 1. 例一：128×128 f32，4 warps，默认 blocked

布局：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [1, 32], warpsPerCTA = [1, 4], order = [1, 0]}>
```

**实测**（Python 复算）得到的线性布局是：

```text
#ttg.linear<{
  register = [[1, 0], [2, 0], [4, 0], [8, 0], [16, 0], [32, 0], [64, 0]],
  lane     = [[0, 1], [0, 2], [0, 4], [0, 8], [0, 16]],
  warp     = [[0, 32], [0, 64]],
  block    = []
}>
```

读法：register 的 7 个 bit 沿第 0 维（行）展开，lane 的 5 个 bit 沿第 1 维（列）展开，warp 的 2 个 bit 继续沿列展开。于是：

```text
thread t = 32*warp + lane
element(r, c) = (r, 32*warp + lane),  r = 0..127
```

**实测**的坐标表：

| 线程 | warp | lane | 前 3 个寄存器坐标 |
|---:|---:|---:|---|
| 0 | 0 | 0 | (0,0), (1,0), (2,0) |
| 1 | 0 | 1 | (0,1), (1,1), (2,1) |
| 31 | 0 | 31 | (0,31), (1,31), (2,31) |
| 32 | 1 | 0 | (0,32), (1,32), (2,32) |
| 64 | 2 | 0 | (0,64), (1,64), (2,64) |
| 127 | 3 | 31 | (0,127), (1,127), (2,127) |

每个线程有 128 个寄存器。warp 0 负责第 0–31 列，warp 1 负责第 32–63 列，依此类推。

这个布局对 row-major 的 global load 是友好的：warp 0 的 32 个 lane 在同一行 `r` 上访问 `c=0..31`，连续 128B。一次 warp 访问可以合并成 4 个 32B sector（或者更少，取决于对齐和 vectorization）。

### 2. 例二：`sizePerThread` 不是线程数

`sizePerThread=[2,2]` 表示每个线程先持有一个 2×2 的寄存器块，而不是“2×2 个线程”。如果 tile 只覆盖 2 行 2 列，剩下的行列会由 lane/warp 展开或 register 重复。对 4×4 tensor、`sizePerThread=[2,2]`、`threadsPerWarp=[2,1]`，**实测**的 LL 是 `register=[[0,1],[1,0],[0,2]], lane=[[2,0]]`：lane 负责行，register 的前两个 bit 负责 2×2 块，第三个 bit 负责列重复。每个线程最终 8 个寄存器，不是 4 个。

### 3. 例三：`order` 如何影响 coalescing

同样 128×128、4 warps，把 `order` 从 `[1,0]` 改成 `[0,1]`：

```mlir
#row = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [1, 32], warpsPerCTA = [1, 4], order = [1, 0]}>
#col = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [32, 1], warpsPerCTA = [4, 1], order = [0, 1]}>
```

**实测**的线性布局：

```text
#row: register = [[1,0]...[64,0]], lane = [[0,1]...[0,16]], warp = [[0,32],[0,64]]
#col: register = [[0,1]...[0,64]], lane = [[1,0]...[16,0]], warp = [[32,0],[64,0]]
```

坐标：

| 线程 | `#row` 前 2 个寄存器 | `#col` 前 2 个寄存器 |
|---:|---|---|
| 0 | (0,0), (1,0) | (0,0), (0,1) |
| 1 | (0,1), (1,1) | (1,0), (1,1) |
| 31 | (0,31), (1,31) | (31,0), (31,1) |
| 32 | (0,32), (1,32) | (32,0), (32,1) |

`#row` 的 warp 0 在行 0 上访问列 0–31：连续 128B，合并成 4 个 32B sector。

`#col` 的 warp 0 在列 0 上访问行 0–31：相邻 lane 的地址相差 `128*4=512B`，每个 lane 落在不同的 32B sector，32 个 lane 需要 32 个 sector。**guided**：具体 sector 数还受 L1/L2、对齐、mask 和 vectorization 影响，但“跨行访问比跨列访问多出接近 8 倍事务”这个方向是确定的。

所以 `order` 不是“行优先/列优先”的标签，而是“哪个维度由相邻 lane 连续覆盖”。`order[0]` 是 lane 最快变化的维度，也是 coalescing 最敏感的维度。

### 4. 例四：同一 tensor 换成 `#ttg.nvidia_mma`

MMA 布局不是“每个线程拿连续一段”，而是“按 tensor core 的 accumulator 形状切片”。对 128×128 f32、4 warps：

```mlir
#mma = #ttg.nvidia_mma<{versionMajor = 2, versionMinor = 0, warpsPerCTA = [2, 2], instrShape = [16, 8]}>
```

`源码依据`：v2 的 warp tile 是 16×8，说明在 `TritonGPUAttrDefs.td:1287-1362`；`toLinearLayout` 在 `LinearLayoutConversions.cpp:963-988`，tile 构造在 `922-961`。

**实测**的线性布局：

```text
#ttg.linear<{
  register = [[0, 1], [8, 0], [32, 0], [64, 0], [0, 16], [0, 32], [0, 64]],
  lane     = [[0, 2], [0, 4], [1, 0], [2, 0], [4, 0]],
  warp     = [[0, 8], [16, 0]],
  block    = []
}>
```

读法：

- lane 的低 2 个 bit 沿列展开（列 +1、+2），高 3 个 bit 沿行展开（行 +1、+2、+4）；
- register 的第 1 个 bit 让列 +1，第 2 个 bit 让行 +8；后面的 register bit 是 32/64 行、16/32/64 列的重复；
- warp 的第 1 个 bit 让列 +8，第 2 个 bit 让行 +16。

于是 32 个 lane 覆盖一个 16×8 的 warp tile，每个 lane 持 4 个元素：

| warp | lane | 4 个寄存器坐标 |
|---:|---:|---|
| 0 | 0 | (0,0), (0,1), (8,0), (8,1) |
| 1 | 0 | (0,8), (0,9), (8,8), (8,9) |
| 2 | 0 | (16,0), (16,1), (24,0), (24,1) |
| 3 | 0 | (16,8), (16,9), (24,8), (24,9) |
| 0 | 1 | (0,2), (0,3), (8,2), (8,3) |
| 0 | 4 | (1,0), (1,1), (9,0), (9,1) |
| 0 | 31 | (7,6), (7,7), (15,6), (15,7) |

对比 blocked：

- blocked 的线程 0 拿的是 `(0,0),(1,0),(2,0)...`，一整列；
- MMA 的线程 0 拿的是 `(0,0),(0,1),(8,0),(8,1)`，两行两列的小块；
- blocked 的 32 个 lane 覆盖 1×32 或 32×1；MMA 的 32 个 lane 覆盖 16×8；
- MMA 的 register 分布是“按 mma.16816 的 accumulator 规则”，不是“连续”。

这就是为什么 `tt.dot` 的 operand 需要 `#ttg.dot_op`：它的寄存器顺序必须匹配 MMA 指令，而不是匹配 global memory。

**guided**：T4 是 `sm_75`，可以表示 v2 的 `#ttg.nvidia_mma`，但 f32 点积不会走 TF32 tensor core（TF32 需要 SM80+）。不要把本节当成 T4 的 dot 性能结论；它只是布局练习。

### 5. `#ttg.dot_op` 与 `#ttg.slice`

`#ttg.dot_op` 描述 dot 的 A/B operand：

```mlir
#dot_a = #ttg.dot_op<{opIdx = 0, parent = #mma, kWidth = 2}>
#dot_b = #ttg.dot_op<{opIdx = 1, parent = #mma, kWidth = 2}>
```

- `opIdx=0` 是 A，逻辑形状 `[M,K]`；`opIdx=1` 是 B，逻辑形状 `[K,N]`；
- `parent` 是 accumulator 的 MMA 布局；
- `kWidth` 是每个线程沿 K 维连续持有的元素数。对 f16，`kWidth = 32/16 = 2`；对 f32，`kWidth = 32/32 = 1`。

`源码依据`：`DotOperandEncodingAttr` 的说明在 `TritonGPUAttrDefs.td:1413-1477`；`kWidth` 的默认 builder 在 `1459-1470`；verifier 在 `lib/Dialect/TritonGPU/IR/Dialect.cpp:2961-3013`。Ampere/Hopper 的 parent 必须给非零 `kWidth`；blocked parent 必须为 0。

`#ttg.slice` 则是“把 parent 的某一维挤掉”：

```mlir
#slice1 = #ttg.slice<{dim = 1, parent = #mma}>
```

它常用于 `tt.reduce` 的结果：reduce 掉一维后，结果的 rank 比输入少一维，但线程分布仍然继承 parent。`源码依据`：`TritonGPUAttrDefs.td:1363-1412`；`toLinearLayout` 在 `LinearLayoutConversions.cpp:1037-1049`，它先把 parent 的 shape 在 `dim` 处插一个 1，再 `removeStandardDim`，最后删掉 register 的零基。

### 6. shared memory 编码：swizzle、padding、partition、linear、NVMMA

shared memory 的 encoding 不是“线程 → 元素”，而是“shared offset → 元素”。它要解决的核心问题是 **bank conflict**。

#### 6.1 `#ttg.swizzled_shared`

```mlir
#smem = #ttg.swizzled_shared<{vec = 1, perPhase = 1, maxPhase = 8, order = [1, 0]}>
```

`源码依据`：字段和例子在 `TritonGPUAttrDefs.td:28-229`；`toLinearLayout` 在 `LinearLayoutConversions.cpp:53-99`。

对二维布局，`toLinearLayout` 先给列方向放 identity 基：

```text
col: 1, 2, 4, 8, ...
row: (row, vec * ((row / perPhase) % maxPhase))
```

也就是说，第 `row` 行会 XOR 一个列偏移 `vec * ((row/perPhase) % maxPhase)`。`perPhase` 控制“多少行共享同一个 phase”，`maxPhase` 控制 XOR 的最大值。

**实测**：32×32 f32、按列访问（固定 `c=7`，`r=0..31`）的 bank 分布：

| 布局 | bank 分布 | 最坏 conflict |
|---|---|---|
| row-major，stride 32 | 全部落在 bank 7 | 32-way |
| padding，stride 33 | 32 个 bank 各 1 个 | 无冲突 |
| swizzle `maxPhase=2` | 2 个 bank 各 16 个 | 16-way |
| swizzle `maxPhase=4` | 4 个 bank 各 8 个 | 8-way |
| swizzle `maxPhase=8` | 8 个 bank 各 4 个 | 4-way |
| swizzle `maxPhase=32` | 32 个 bank 各 1 个 | 无冲突 |

对 4 字节元素，硬件常见的 128B swizzle 只有 8 个 phase，所以 32 行的列访问仍然有 4-way conflict；要彻底消除，需要改访问模式（比如每个 lane 读 4 个连续元素）或 padding。这个结论是 **guided** 的：具体 bank 数、phase 上限和访问粒度跟架构有关。

#### 6.2 `#ttg.padded_shared`

padding 的思路更直接：每 `interval` 个元素插 `pad` 个空洞，把行 stride 从 32 改成 33。

```mlir
#padded = #ttg.padded_shared<[32:+1], {order = [1, 0], shape = [32, 32]}>
```

`源码依据`：说明和例子在 `TritonGPUAttrDefs.td:230-353`。地址公式是：

```text
addr(i) = i + sum_k (i / interval_k) * pad_k
```

`interval` 和 `pad` 必须是 2 的幂。对 32×32 f32，`[32:+1]` 让每行多 1 个元素，列访问的 bank 变成 `(r*33+c) % 32 = (r+c) % 32`，32 行正好覆盖 32 个 bank。**实测**见表中“padding，stride 33”。

#### 6.3 `#ttg.partitioned_shared`

`#ttg.partitioned_shared` 把 tensor 沿 `partitionDim` 切成 `numPartitions * numGroups` 个 piece，分到不同物理 buffer。对 `[128,32]`、2 partitions、4 groups，piece 是 `[16,32]`；Partition 0 拿 `P0,P2,P4,P6`，Partition 1 拿 `P1,P3,P5,P7`，用来分散 shared partition 压力。`源码依据`：`TritonGPUAttrDefs.td:354-420`；`toLinearLayout` 在 `LinearLayoutConversions.cpp:1147-1190`。**guided**：T4 上不一定触发。

#### 6.4 `#ttg.shared_linear`

`shared_linear` 直接把 `LinearLayout` 作为 shared 布局：

```mlir
#shared = #ttg.shared_linear<{offset = [[0, 1], [0, 2], [1, 0], [2, 2]]}, alignment = 16>
```

`源码依据`：定义在 `TritonGPUAttrDefs.td:421-453`；`toLinearLayout` 在 `Dialect.cpp:2140-2150`；这个例子来自 `test/TritonGPU/ops.mlir:515-516`。

`offset` 的 4 个基向量分别映射到 `(dim0,dim1)`：

```text
offset bit0 -> (0,1)
offset bit1 -> (0,2)
offset bit2 -> (1,0)
offset bit3 -> (2,2)
```

最后一项 `(2,2)` 就是一个 swizzle：offset 的高位同时影响行和列。`shared_linear` 让“shared 布局”也能用线性布局表达，便于组合和比较。

#### 6.5 `#ttg.nvmma_shared`

`#ttg.nvmma_shared<{swizzlingByteWidth = 128, transposed = false, elementBitWidth = 16}>` 是 Hopper/Blackwell 的 WGMMA/TMA shared 布局，按 PTX 的 128B/64B/32B swizzle 组织。`源码依据`：`TritonGPUAttrDefs.td:454-508`；`toLinearLayout` 在 `LinearLayoutConversions.cpp:358-400`。**guided**：T4 没有 WGMMA/TMA。

#### 6.6 CGA：CTA 之间怎么分

CGA 把 `block` 映射到逻辑维：`#ttg.cga_encoding<[[1,0]]>` 表示 block bit0 沿 dim0 加 1。对 2 CTA、128×128，CTA0 拿行 0–63，CTA1 拿行 64–127；`[[1,0],[0,1]]` 是 2×2 CTA。`源码依据`：`CGAEncodingAttr.td:14-42`；构造在 `Dialect.cpp:643-703`；verifier 在 `615-640`；`combineCtaCgaWithShape` 在 `LinearLayoutConversions.cpp:1335-1363`。**guided**：T4 的 `num_ctas>1` 在 `third_party/nvidia/backend/compiler.py:244-247` 被拒绝。

### 7. `#ttg.linear`：把布局写成矩阵

`#ttg.linear` 是 Triton 3.x 逐渐推广的“显式线性布局”：

```mlir
#linear = #ttg.linear<{
  register = [[2, 0], [0, 2]],
  lane     = [[0, 1], [1, 0]],
  warp     = [],
  block    = []
}>
```

它和本章例二的 4×4 blocked 是同一个映射：

```text
register bit0 -> (2,0)
register bit1 -> (0,2)
lane bit0     -> (0,1)
lane bit1     -> (1,0)
```

`源码依据`：`LinearEncodingAttr` 定义在 `TritonGPUAttrDefs.td:679-721`；打印/解析在 `Dialect.cpp:1207-1232`；verifier 要求去掉 broadcast 基后是 permutation matrix（`Dialect.cpp:1182-1194`）。

`#ttg.linear` 的细节、矩阵运算、求逆、组合，是 Ch 13 的主题。本章只需要记住：**blocked/MMA/slice 最终都能转成 linear，linear 是布局的“共同语言”**。

## 进一步用法/重要变体

### 1. 用 `triton-tensor-layout` 看真实映射

构建出 `triton-tensor-layout` 后，可以直接打印：

```bash
triton-tensor-layout \
  -l "#ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [4, 8], warpsPerCTA = [4, 1], order = [1, 0]}>" \
  -t "tensor<16x16xf16>"
```

`源码依据`：`bin/triton-tensor-layout.cpp` 的用法说明在 `1-40`；真实期望输出在 `test/Tools/tensor_layout_print.mlir:17-25`。

第一行输出是：

```text
T0:0|  T4:0,   T0:1|  T4:1,   T0:2|  T4:2,   T0:3|  T4:3, ...
```

读法：

- `T0:0` 表示线程 0 的寄存器 0；
- `T0:0|T4:0` 表示同一个逻辑元素同时出现在线程 0 和线程 4 的寄存器 0；
- 这不是错误，而是 broadcast：`threadsPerWarp=[4,8]` 在 16 列的 tensor 上覆盖了 32 列，超出的 lane 位被映射到 0，导致两个线程持有同一元素。

这个例子很好地说明：**layout 不一定是单射**。同一个逻辑元素可以出现在多个线程里；`removeZeroBasesAlongDim("register")` 就是用来在需要“唯一元素”时删掉这些重复基（Ch 13 详述）。

### 3. `dot_op` 的 `kWidth` 随 dtype 变

| dtype | `kWidth` | 原因 |
|---|---:|---|
| f16/bf16 | 2 | 32 bit / 16 bit |
| f32 | 1 | 32 bit / 32 bit |
| fp8 | 4 | 32 bit / 8 bit |

`源码依据`：`TritonGPUAttrDefs.td:1466-1469` 的 `std::max(32 / bitwidth, 1u)`。

## 对比与取舍

| encoding | 映射方向 | 典型用途 | 代价/限制 |
|---|---|---|---|
| `#ttg.blocked` | thread/reg → 元素 | global load/store、elementwise | 需要 `order` 匹配内存；不匹配 MMA |
| `#ttg.nvidia_mma` | thread/reg → accumulator | `tt.dot` 输出 | 形状固定 16×8/16×16；T4 上 f32 不走 TF32 |
| `#ttg.dot_op` | thread/reg → A/B operand | MMA operand | `kWidth` 和 parent 必须匹配 |
| `#ttg.slice` | parent 去一维 | reduce/expand_dims 的逆 | parent rank 必须 ≥2 |
| `#ttg.swizzled_shared` | offset → 元素 | shared staging、MMA operand | swizzle 参数影响 bank conflict |
| `#ttg.padded_shared` | offset → 元素 | 避免 bank conflict | padding 浪费 shared 容量 |
| `#ttg.partitioned_shared` | offset/partition → 元素 | 多 buffer、partition 分散 | 较新，后端支持有限 |
| `#ttg.shared_linear` | offset → 元素 | 显式 shared 布局 | 需要手工给基 |
| `#ttg.nvmma_shared` | offset → 元素 | Hopper/Blackwell WGMMA/TMA | T4 不支持 |
| `#ttg.linear` | reg/lane/warp/block → 元素 | 统一表示、组合、求逆 | verifier 约束强 |
| CGA | block → 元素 | 多 CTA 切分 | T4 `num_ctas=1` |

取舍的核心：

- **blocked 优先 coalescing**，但和 MMA 不兼容；
- **MMA 优先指令匹配**，但 global access 不 coalesced；
- **shared 优先 bank conflict**，但要额外的 store/load/barrier；
- **linear 优先可计算性**，但需要后端支持，且 verifier 更严格。

## 踩雷集锦

1. **把 `order` 当成“内存行优先”。**
   `order[0]` 是 lane 最快变化的维度。`order=[1,0]` 表示列最快，适合 row-major；`order=[0,1]` 表示行最快，适合 column-major。写反了不会报错，但 global access 会变成跨行。

2. **以为 `sizePerThread` 越大越好。**
   `sizePerThread` 增加每线程寄存器数，可能提高 vectorization，也可能降低 occupancy。对 128×128、4 warps，`sizePerThread=[1,1]` 已经让每线程 128 个寄存器；再乘 4 就是 512 个，直接爆寄存器。

3. **`sizePerThread`/`threadsPerWarp`/`warpsPerCTA` 不是 2 的幂。**
   verifier 在 `Dialect.cpp:705-740` 直接拒绝。不要写 `[3,1]` 这种值。

4. **`order` 不是排列。**
   `verifyLayoutOrder` 在 `Dialect.cpp:604-613` 要求它是 `0..rank-1` 的排列。`[1,1]`、`[2,0]` 都会报错。

5. **`#ttg.nvidia_mma` 的 `versionMajor=1` 被拒绝。**
   `NvidiaMmaEncodingAttr::verify` 在 `Dialect.cpp:1570-1583` 只接受 2（Ampere）和 3（Hopper）。Volta 已移除。

6. **`#ttg.dot_op` 的 `kWidth` 给错。**
   Ampere/Hopper parent 必须非零（`Dialect.cpp:2971-2977`）；blocked parent 必须为 0（`3008-3012`）。f16 用 2，f32 用 1，fp8 用 4。

7. **`#ttg.slice` 的 `dim` 越界。**
   `SliceEncodingAttr::verify` 在 `Dialect.cpp:1867-1877` 要求 parent rank ≥2 且 `dim < rank`。

8. **在 T4 上写 `num_ctas=2` 或 `CGALayout`。**
   `third_party/nvidia/backend/compiler.py:244-247` 要求 SM90+。T4 只能 `num_ctas=1`，所有 CGA 例子都是 guided。

9. **以为 shared swizzle 一定能消掉 bank conflict。**
   `maxPhase` 受元素宽度和硬件模式限制。32×32 f32 的列访问在 `maxPhase=8` 下仍有 4-way conflict；要彻底消除，得改访问粒度或 padding。

10. **以为 encoding 对了就一定 coalesced。**
    coalescing 还取决于指针连续性、mask、对齐、`tt.divisibility` 和 vectorization。layout 只是必要条件之一。

## 进阶

- 读 `BlockedEncodingAttr` 的 builder（`TritonGPUAttrDefs.td:845-909`，主 builder `846-881`），自己推一遍 64×64、8 warps 的 `threadsPerWarp`/`warpsPerCTA`。
- 用 `triton-tensor-layout -use-hw-view` 看同一个布局的“tensor 视角”和“hardware 视角”差异。
- 读 `SwizzledSharedEncodingAttr` 的 5 个例子（`TritonGPUAttrDefs.td:38-130`），手算 `perPhase`/`maxPhase`/`vec` 对 XOR 的影响。
- 在 Hopper 上（或模拟）读 `#ttg.nvmma_shared` 和 `ttng.warp_group_dot` 的 shared 布局要求。
- 读 `LinearLayoutConversions.cpp` 的 `combineCtaCgaWithShape`（`1335-1363`），理解“先扩到 shapePerCTA，再乘 CGA”的顺序。

## 动手练习

1. **默认 blocked**：对 64×64 f32、4 warps，写出默认 `#ttg.blocked`，并列出线程 0、1、31、32 的前 4 个元素坐标。**guided**：用本章的公式手算，或用 `triton-tensor-layout` 验证。

2. **order 对比**：把练习 1 的 `order` 改成 `[0,1]`，重新算线程 0 的前 4 个坐标，并解释为什么 global load 会变慢。**guided**。

3. **MMA 分布**：对 32×32 f32、`#ttg.nvidia_mma<{versionMajor=2, versionMinor=0, warpsPerCTA=[1,1], instrShape=[16,8]}>`，列出 lane 0、1、4、31 的 4 个寄存器坐标。**guided**：按 `nvidiaMmaTile` 的公式算。

4. **bank conflict**：对 32×32 f32 的列访问，分别计算 row-major、`[32:+1]` padding、`maxPhase=8` swizzle 的 bank 分布。**实测**：可以写一个 20 行 Python 脚本复算。

5. **CGA**：对 128×128、4 个 CTA 排成 2×2，写出 `CGALayout`，并说明 CTA(1,1) 的行列范围。**guided**：T4 不能跑。

6. **读真实输出**：打开 `test/Tools/tensor_layout_print.mlir:17-25`，解释为什么第一行是 `T0:0|T4:0` 而不是 `T0:0`。**源码依据**。

## 本章重点

- TTGIR 的 encoding 是编译期映射函数，不是运行时对象。
- `#ttg.blocked` 由 `sizePerThread`、`threadsPerWarp`、`warpsPerCTA`、`order` 四个字段决定；tensor 比 tile 大就重复，比 tile 小就广播。
- `order[0]` 是 lane 最快变化的维度，直接决定 coalescing。
- `#ttg.nvidia_mma` 按 tensor core accumulator 分布元素；它和 blocked 的本质差别是“线程拿连续段”还是“线程拿 16×8 tile 的散点”。
- `#ttg.dot_op` 的 `kWidth` 必须匹配 dtype 和 parent；`#ttg.slice` 是 reduce/expand_dims 的逆。
- shared 布局解决 bank conflict：swizzle 用 XOR，padding 用空洞，partition 用多 buffer，NVMMA 用硬件模式。
- `#ttg.linear` 是布局的共同语言；CGA 管 CTA 之间的切分。

## 自我检核

1. 对 `#ttg.blocked<{sizePerThread=[1,1], threadsPerWarp=[1,32], warpsPerCTA=[1,4], order=[1,0]}>` 和 `tensor<128x128xf32>`，线程 70 的第一个元素坐标是什么？
2. 为什么 `order=[0,1]` 的 128×128 load 会比 `order=[1,0]` 多出很多 memory transaction？
3. `sizePerThread=[2,2]` 是否意味着每个线程一定只有 4 个寄存器？为什么？
4. `#ttg.nvidia_mma` 的 32 个 lane 覆盖多大的 tile？每个 lane 持几个 f32？
5. `#ttg.dot_op` 的 `kWidth` 对 f16 和 f32 分别是多少？为什么？
6. `#ttg.slice<{dim=1, parent=#blocked}>` 的 rank 是多少？它和 `tt.reduce` 的 axis 有什么关系？
7. 32×32 f32 的列访问，row-major、padding、swizzle 的 bank conflict 各是多少？
8. `CGALayout=[[1,0]]` 表示几个 CTA、沿哪一维切分？T4 能跑吗？

## 延伸阅读

- **`include/triton/Dialect/TritonGPU/IR/TritonGPUAttrDefs.td`**
  - **读哪里**：`28-229`（SwizzledShared）、`230-353`（PaddedShared）、`354-420`（PartitionedShared）、`421-453`（SharedLinear）、`454-508`（NVMMAShared）、`611-661`（DistributedEncoding）、`679-721`（LinearEncoding）、`752-915`（Blocked）、`1287-1362`（NvidiaMma）、`1363-1412`（Slice）、`1413-1477`（DotOperand）。
  - **学什么**：每个布局的字段、语义、例子和 verifier 约束。
  - **与本章关系**：这是本章的“字典”；先读 description，再读 builder。
- **`lib/Dialect/TritonGPU/IR/LinearLayoutConversions.cpp`**
  - **读哪里**：`53-99`（SwizzledShared）、`869-880`（Blocked）、`922-988`（NvidiaMma）、`1026-1049`（DotOperand/Slice）、`1193-1227`（dispatcher）、`1335-1363`（combineCtaCgaWithShape）。
  - **学什么**：布局如何变成线性布局；shape 扩展/截断的顺序。
  - **与本章关系**：本章的坐标表就是这些函数的输出。
- **`lib/Dialect/TritonGPU/IR/Dialect.cpp`**
  - **读哪里**：`604-740`（verifier）、`742-755`（getDefaultBlockedEncoding）、`643-703`（CGA）、`1570-1583`（NvidiaMma verify）、`2961-3013`（DotOperand verify）。
  - **学什么**：哪些布局组合合法，默认布局怎么选。
  - **与本章关系**：踩雷集锦里的错误都能在这里找到对应的 `emitError`。
- **`test/Tools/tensor_layout_print.mlir`**
  - **读哪里**：`1-25`。
  - **学什么**：`triton-tensor-layout` 的真实输出格式，`T0:0|T4:0` 的 broadcast 读法。
  - **与本章关系**：把“公式”变成“可读的表”。
- **PTX ISA 的 `mma.16816` / `wgmma` 章节**
  - **读哪里**：`mma.16816` 的 accumulator layout；`wgmma` 的 shared memory layout。
  - **学什么**：为什么 MMA 的 lane/register 分布是 16×8、每线程 4 个 f32。
  - **与本章关系**：`#ttg.nvidia_mma` 和 `#ttg.nvmma_shared` 的硬件依据。
- **CUTLASS CuTe 文档**
  - **读哪里**：`00_quickstart.md` 的 layout/composition 部分。
  - **学什么**：另一种“布局即函数”的表达方式，和 Triton LL 的异同。
  - **与本章关系**：`LinearLayout.h:300-320` 有专门的 CuTe 对比。

## 下一章

→ [Ch 13 — LinearLayout：把布局变成可计算的矩阵](./13-linear-layout.md)
