# Ch 26 — Reduce/scan/gather/histogram 与 convert_layout

> 归约不是一条指令，而是一棵树、几次 shuffle、一次 shared memory 往返，外加一个 barrier。

## 目标

读完本章，你应该能回答：

- `tl.sum` 在 TTGIR 里只是一条 `tt.reduce`，它怎样变成“线程内树形加法 → warp 内 `shfl.bfly` → 跨 warp shared memory → 再来一轮 warp 归约”？
- 为什么跨 warp 的归约必须经过 shared memory 和 barrier，而不能只用 shuffle？
- `tl.cumsum` 的 scan 为什么不是树形归约？`reverse=True` 为什么要先把 lane 翻转？
- `tl.gather` 的索引怎样变成“哪个 lane、哪个寄存器”？`tl.histogram` 什么时候用 `vote.ballot + popc`，什么时候用 shared memory 原子加？
- `ttg.convert_layout` 什么时候只重排寄存器、什么时候用 shuffle、什么时候必须落 shared memory？代价差多少？

本章的主线是“数据在 `register / lane / warp / block` 四个维度上怎么移动”。reduce、scan、gather、histogram 是四种不同的移动模式；`convert_layout` 是把任意一种 layout 变成另一种 layout 的通用搬运工。把这条主线抓住，TTGIR 里那些 `#blocked`、`#linear`、`convert_layout` 就不再是黑盒。

## 环境/版本

**实测**环境：

| 项目 | 值 |
|---|---|
| Triton 源码 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 源码版本 | 3.9.0（`python/triton/__init__.py:2`） |
| 运行用 wheel | 3.9.0，位于 `/home/ubuntu/aicomp/triton/.venv/lib/python3.12/site-packages/triton` |
| Python | 3.12.16（venv）；系统 Python 3.10.12 没有 `triton` |
| PyTorch | 2.5.1+cu124 |
| GPU | Tesla T4，compute capability 7.5（`sm_75`） |
| 驱动 / CUDA | 535.216.01 / 12.4 |

本章的 IR、PTX 和计数都来自上面的 wheel 在 T4 上的真实编译与运行；涉及 sm_80+ 的 `redux.sync`、sm_90+ 的 `stmatrix`、多 CTA 的 cluster barrier 标为 **guided**，没有在 T4 上伪造输出。

**源码依据**：

- `include/triton/Dialect/Triton/IR/TritonOps.td:856-881` 定义 `tt.reduce`：变长输入、`axis`、一个 combine region。
- `include/triton/Dialect/Triton/IR/TritonOps.td:893-912` 定义 `tt.scan`：比 reduce 多一个 `reverse`。
- `include/triton/Dialect/Triton/IR/TritonOps.td:1025-1044` 定义 `tt.histogram`：输入是整数 tensor，可选 mask，输出 bin 数等于结果维度。
- `include/triton/Dialect/Triton/IR/TritonOps.td:1049-1072` 定义 `tt.gather`：单轴 gather，带 `efficient_layout` 属性。
- `include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:119-133` 定义 `ttg.convert_layout`，可选 `force_warp_shuffle`。

**实测**的六个小实验，后文会逐一拆开：

| 实验 | 配置 | shared | 关键指令计数 |
|---|---|---:|---|
| 1D sum | 1024 f32，`num_warps=8` | 32 B | 8 `shfl.bfly`，1 `bar.sync` |
| 2D sum | 32×64 f32，axis=1，`num_warps=4` | 128 B | 16 `shfl.bfly`，1 `bar.sync` |
| cumsum | 1024 f32，`num_warps=8` | 32 B | 6 `shfl.up`，1 `bar.sync` |
| gather | 32×64 src，32×32 idx，axis=1，`num_warps=4` | 4096 B | 32 `shfl.idx`，10 `bar.sync`（多数来自 convert_layout） |
| histogram | 1024 i32，B=2，`num_warps=1` | 0 B | 64 `vote.ballot`，64 `popc` |
| histogram | 1024 i32，B=8，`num_warps=4` | 128 B | 8 `atom.shared.add`，2 `bar.sync` |

## 为什么需要这个

elementwise 的 lowering 是一个元素一个元素地算；reduce/scan/gather/histogram 的输入输出不是一一对应的，必须搬数据：reduce 把一根轴合成一个值，scan 保留每个前缀，gather 按运行时索引去别的 lane/warp 取数，histogram 把每个值投递到 bin，convert_layout 则可能搬整块 layout。它们最容易出现“看起来对、性能差很多”的问题：多了一次 `bar.sync`、shared memory 从 32 B 变成 16 KiB、本该 warp-local 的 gather 掉进了 shared memory。只看 Python 源码看不出来，必须看 TTGIR 的 layout 和 LLVM/PTX 的搬运指令。

## 先建立直觉

GPU 的数据通路可以按“谁和谁能直接通信”排成四级：

| 层级 | 通信方式 | 典型指令 | 需要 barrier？ |
|---|---|---|---|
| 线程内 | 寄存器 | `add`、`select` | 否 |
| warp 内 | lane 间 shuffle | `shfl.sync.bfly/up/idx` | 否（warp 同步） |
| CTA 内跨 warp | shared memory | `st.shared` / `ld.shared` | 是 |
| 跨 CTA | distributed shared / cluster | `clusterBarrier`、`ld.shared::cluster` | 是（guided） |

T4 的 warp 是 32 lane。`shfl.sync` 只能在一个 warp 内换数据；一旦归约轴跨过 warp 边界，硬件没有“跨 warp shuffle”这种指令，只能把每个 warp 的部分结果写进 shared memory，`bar.sync` 之后再读出来。

四种操作的移动模式可以先用一句话记住：

- **reduce**：每个线程先把自己手里的元素树形合并；warp 内用 butterfly shuffle 合并 32 个 lane；跨 warp 用 shared memory 收 partial。
- **scan**：线程内顺序累加（保持前缀顺序）；warp 内用 `shfl.up` 做 Hillis-Steele 前缀；跨 warp 把每个 warp 的“最后一个前缀”写 shared，再顺序补到后面的 warp。
- **gather**：如果索引列和源列在同一个 warp，就把“目标 lane / 目标寄存器”算出来，用 `shfl.idx` 取；否则整块源数据落 shared，再按索引读。
- **histogram**：bin 很少时，每个 lane 对每个 bin 做 `ballot`，再 `popc` 数 1 的个数；bin 多时，直接在 shared 里对 bin 做原子加。

## 核心概念

### 1. 四个 op 的语义约束

`tt.reduce` 的 combine region 输入是 `2N` 个标量、输出 `N` 个标量；`tl.sum` 只是塞一个 `arith.addf`，`tl.argmax` 会塞更复杂的 region，多操作数 reduce 在 lowering 里对应 `accs` 的每一“列”。`tt.scan` 共享同样的 region 形状，但语义是前缀；`reverse` 决定从哪一端开始。

**源码依据**：`tt.gather` 在语义层检查：索引必须是整数、rank 必须和源相同、非 gather 维必须完全匹配（`python/triton/language/semantic.py:1839-1855`），但不检查越界。`tt.histogram` 更窄：只支持 1D、32 位整数输入，mask 必须是 bool（`semantic.py:1892-1902`）；bin 宽度固定为 1，从 0 开始。

### 2. 用 LinearLayout 看“哪些位在动”

**源码依据**：Triton 3.9 的 layout 都可以写成 `LinearLayout`：输入维是 `register / lane / warp / block`，输出维是 tensor 的 `dim0 / dim1 / ...`。一个 lane id 的二进制位，如果 basis 在归约轴上有非零分量，就说明“这个 lane 位参与归约”；整个 basis 全零则只是广播。

**源码依据**：`ReduceOpToLLVM.cpp:355-370` 正是这样算 mask 的：

```cpp
for (unsigned bit = 0; bit < laneBases.size(); ++bit) {
  if (laneBases[bit][op.getAxis()] != 0)
    reduceLaneIdMask |= 1u << bit;
  else if (llvm::all_of(laneBases[bit], [](int32_t x) { return x == 0; }))
    broadcastLaneIdMask |= 1u << bit;
}
```

这解释了为什么同一个 `tl.sum`，在不同 layout 下会生成不同数量的 shuffle：不是所有 32 个 lane 都参与归约。

### 3. reduce 的三段式

**源码依据**：`ReduceOpToLLVM.cpp:36-105` 的 `matchAndRewrite` 把 reduce 拆成：

1. `reduceWithinThreads`：把同一线程寄存器里的元素按树形合并（`:266-350`）。
2. `reduceWithinWarps`：warp 内按 `reduceLaneIdMask` 做 butterfly shuffle（`:355-418`）。
3. 如果归约轴还大于 1，就循环 `convertLayoutValues + reduceWithinWarps`，把跨 warp / 跨 CTA 的 partial 收掉（`:73-88`）。

树形合并本身在 `treeReduce`（`:115-138`）：每次把 `arity` 个值合成一个，直到只剩一个。T4 的 `getReductionTreeArity` 返回 2（`TargetInfo.cpp:771-804` 对 `computeCapability < 90` 直接返回 2），所以是二叉树的 `(a+b)+(c+d)`；sm_90+ 的整数 min/max、sm_100+ 的 f32 min/max 在满足 PTX 版本时返回 3，让 LLVM 折成三元指令（guided）。

### 4. convert_layout 的三条路径

**源码依据**：`ConvertLayoutOpToLLVM.cpp:36-67` 按顺序判断：

```cpp
if (conversion.getNumInDims() == 0) { replaceOp(src); }          // 什么都不用做
if (conversion.getNumInDims() == 1) { transferWithinThread(...); } // 只重排寄存器
if (cvtNeedsWarpShuffle(op)) { transferWithinWarp(...); }         // 只用 shuffle
transferSwizzlingLocalMem(...);                                   // 落 shared memory
```

`cvtNeedsWarpShuffle` 的条件在 `lib/Analysis/Utility.cpp:1126-1153`：源和目标都必须是 permutation matrix layout，转换不能跨 warp/block，并且 `getWarpLayoutConvertDecomposition(...).mixedTranspositions.size() < 2`。不满足就退回 shared。

`cvtNeedsSharedMemory` 就是“不是纯寄存器重排，也不是 warp shuffle”（`Utility.cpp:1155-1158`）。

### 5. gather / histogram 的路径选择

**源码依据**：gather 看 `GatherLoweringHelper::isWarpLocal()`（`lib/Analysis/Utility.cpp:946-1007`）：如果 gather 轴的所有元素都在同一个 warp 里，就走 `emitWarpLocalGather`；否则走 `emitGatherInShared`。源码入口在 `GatherOpToLLVM.cpp:34-49`。

**源码依据**：histogram 看 `canUseWarpBallotHistogram`（`lib/Analysis/Utility.cpp:28-33`）：

```cpp
return !hasCrossCTAScratch(op) && numBins <= 2 &&
       numBins * gpu::lookupNumWarps(op) <= 4;
```

只有 bin 很少、warp 很少时才用 ballot；否则用 shared 原子加。这个阈值在 T4 上的效果可以直接用后面的实验验证。

## 底层机制

### 1. `tl.sum`：TTGIR → LLVM → PTX

先看 kernel：

```python
@triton.jit
def ksum(x_ptr, o_ptr, N: tl.constexpr):
    offs = tl.arange(0, N)
    x = tl.load(x_ptr + offs)
    s = tl.sum(x, axis=0)
    tl.store(o_ptr, s)
```

`N=1024`、`num_warps=8` 时，TTGIR 只有一条 `tt.reduce`（**实测**）：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [4], threadsPerWarp = [32], warpsPerCTA = [8], order = [0]}>
%s = "tt.reduce"(%x_1) <{axis = 0 : i32}> ({
^bb0(%a: f32, %b: f32):
  %c = arith.addf %a, %b : f32
  tt.reduce.return %c : f32
}) : (tensor<1024xf32, #blocked>) -> f32
```

（节选，SSA 名简化）这个 layout 的含义：每个线程 4 个连续元素（`sizePerThread=[4]`），warp 内 32 个 lane，8 个 warp。于是 lowering 分三段。

**第一段：线程内。** 4 个寄存器做二叉树，3 次 `fadd`。LLIR 里就是（节选，省略 debug loc）：

```llvm
%19 = fadd float %15, %16
%20 = fadd float %17, %18
%21 = fadd float %19, %20
```

**第二段：warp 内。** `reduceLaneIdMask = 0b11111`，从 bit 4 到 bit 0 依次 `shfl.bfly`：

```llvm
%23 = tail call i32 @llvm.nvvm.shfl.sync.bfly.i32(i32 -1, i32 %22, i32 16, i32 31)
%27 = tail call i32 @llvm.nvvm.shfl.sync.bfly.i32(i32 -1, i32 %26, i32 8,  i32 31)
...
%39 = tail call i32 @llvm.nvvm.shfl.sync.bfly.i32(i32 -1, i32 %38, i32 1,  i32 31)
```

注意 f32 被 `bitcast` 成 i32 再 shuffle，shuffle 完再 `bitcast` 回来；`shfl.sync.bfly.b32` 只认 32 位寄存器。

**第三段：跨 warp。** 每个 warp 的 lane 0 把 partial 写进 shared，`bar.sync`，然后每个线程读出 8 个 warp 的 partial，再做一轮 `shfl.bfly 4,2,1`（因为 8 个 partial 只需要 3 位）：

```llvm
%42 = lshr i32 %5, 3          ; tid >> 3
%43 = and i32 %42, 28         ; warp_id * 4
%44 = getelementptr ... @global_smem, i32 %43
store float %41, ptr addrspace(3) %44
call void @llvm.nvvm.barrier.cta.sync.aligned.all(i32 0)
...
%49 = call i32 @llvm.nvvm.shfl.sync.bfly.i32(i32 -1, i32 %47, i32 4, i32 31)
%53 = call i32 @llvm.nvvm.shfl.sync.bfly.i32(i32 -1, i32 %52, i32 2, i32 31)
%57 = call i32 @llvm.nvvm.shfl.sync.bfly.i32(i32 -1, i32 %56, i32 1, i32 31)
```

PTX 与 LLIR 一一对应（**实测**）：

```ptx
shfl.sync.bfly.b32 %r12, %r11, 16, 31, -1;
st.shared.b32 [%r25], %r21;
bar.sync 0;
ld.shared.b32 %r28, [%r27];
shfl.sync.bfly.b32 %r29, %r28, 4, 31, -1;
```

把 `num_warps` 从 4 改到 16，计数呈规律变化（**实测**）：

| num_warps | shared | `shfl.bfly` | `bar.sync` |
|---:|---:|---:|---:|
| 4 | 16 B | 7 | 1 |
| 8 | 32 B | 8 | 1 |
| 16 | 64 B | 9 | 1 |

规律：shared = `num_warps * 4` 字节；shuffle = 5（warp 内）+ `log2(num_warps)`（跨 warp 收 partial）。这就是“树形 + 两级 warp 归约”的直接代价。

### 2. 2D reduce：轴不跨 warp 时没有 shared 归约

把 kernel 改成 32×64、`axis=1`、`num_warps=4`：

```python
r = tl.arange(0, 32)[:, None]
c = tl.arange(0, 64)[None, :]
x = tl.load(x_ptr + r * 64 + c)
s = tl.sum(x, axis=1)
```

TTGIR 的 layout 是（**实测**）：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [2, 16], warpsPerCTA = [4, 1], order = [1, 0]}>
```

axis=1 只落在 16 个 lane × 4 个寄存器上，`warpsPerCTA=[4,1]` 表示 axis=1 上没有第二个 warp。所以 PTX 里只有 16 条 `shfl.bfly`（4 个寄存器 × 4 个 stage：8、4、2、1），没有跨 warp 的 shared 归约；`shared=128 B` 来自最后把 `slice<dim=1>` 转成输出 layout 的 `convert_layout`（**实测**）。**不要看到 reduce 就以为一定有 barrier**，先看归约轴跨不跨 warp。

### 3. scan：顺序前缀 + `shfl.up` + shared partial

`tl.cumsum` 走 `tt.scan`，不是 reduce。kernel：

```python
@triton.jit
def kscan(x_ptr, o_ptr, N: tl.constexpr):
    offs = tl.arange(0, N)
    x = tl.load(x_ptr + offs)
    y = tl.cumsum(x, axis=0)
    tl.store(o_ptr + offs, y)
```

`N=1024`、`num_warps=8` 时，PTX 的关键序列（**实测**）：

```ptx
shfl.sync.up.b32 %r19, %r18, 1, 0, -1;
shfl.sync.up.b32 %r22, %r21, 2, 0, -1;
shfl.sync.up.b32 %r25, %r24, 4, 0, -1;
shfl.sync.up.b32 %r28, %r27, 8, 0, -1;
shfl.sync.up.b32 %r31, %r30, 16, 0, -1;
...
@%p1 st.shared::cta.b32 [%r5 + 0], %r6;
bar.sync 0;
ld.shared.v4.b32 {%r36, %r37, %r38, %r39}, [global_smem];
...
shfl.sync.up.b32 %r57, %r10, 1, 0, -1;
```

**源码依据**：对应源码三步（`ScanOpToLLVM.cpp:462-538`）：`scanThreadContiguousElements`（`:30-49`）在线程内顺序累加；`warpScan`（`:53-84`）用 `shfl.up` 和 `laneIdAxis >= i` 做 5 步 Hillis-Steele；`axisNumWarps > 1` 时（`:504-524`）`storeWarpAccumulator` 把每个 warp 的最后一个前缀写 shared，`bar.sync`，`AddPartialReduce` 读回所有 warp 的 partial 并按 `warpId` 顺序补上。

T4 上的计数（**实测**）：

| num_warps | shared | `shfl.up` | `bar.sync` |
|---:|---:|---:|---:|
| 1 | 0 B | 48 | 0 |
| 2 | 32 B | 24 | 1 |
| 4 | 32 B | 12 | 1 |
| 8 | 32 B | 6 | 1 |

`num_warps=1` 时没有跨 warp 阶段，shared 和 barrier 都是 0；`num_warps=8` 时只需要 32 B 存 8 个 warp 的 partial，但 barrier 省不掉。`reverse=True` 不是另写一套 scan，而是 `flip(scan(flip(x)))`（`ScanOpToLLVM.cpp:485-496`）：先翻转 chunk 和 lane（`shfl.bfly` 的 xor 掩码是 `warpSize-1`），scan 完再翻回来。**实测** `reverse=True` 时 `shfl.bfly 31` 出现 8 次，`shfl.up` 仍是 6 次。

### 4. gather：索引怎样变成 lane 和寄存器

kernel：32×64 的源，32×32 的索引，沿 axis=1 gather：

```python
@triton.jit
def kgather(x_ptr, idx_ptr, o_ptr, M: tl.constexpr, N: tl.constexpr, K: tl.constexpr):
    r = tl.arange(0, M)[:, None]
    c = tl.arange(0, N)[None, :]
    x = tl.load(x_ptr + r * N + c)
    ri = tl.arange(0, M)[:, None]
    ci = tl.arange(0, K)[None, :]
    idx = tl.load(idx_ptr + ri * K + ci)
    y = tl.gather(x, idx, axis=1)
    tl.store(o_ptr + ri * K + ci, y)
```

TTGIR 里 gather 之前的 layout 被 middle-end 改成了 gather-friendly 的 `#blocked2`（**实测**）：

```mlir
#blocked2 = #ttg.blocked<{sizePerThread = [1, 2], threadsPerWarp = [1, 32], warpsPerCTA = [4, 1], order = [1, 0]}>
%y_19 = tt.gather %y[%y_18] {axis = 1 : i32, efficient_layout}
    : (tensor<32x64xf32, #blocked2>, tensor<32x32xi64, #blocked2>) -> tensor<32x32xf32, #blocked2>
```

`threadsPerWarp=[1,32]` 表示 gather 轴完全落在 32 个 lane 上，每个 lane 2 个连续元素。这正是 warp-local gather 的条件。

PTX 里每个索引值先被转成 lane id（**实测**）：

```ptx
bfe.u32 %r135, %r76, 1, 5     ; idx >> 1，取低 5 位 = lane
shfl.sync.idx.b32 %r136, %r55, %r135, 31, -1;
shfl.sync.idx.b32 %r137, %r56, %r135, 31, -1;
...
selp.b32 %r156, %r136, %r137, %p4;
```

因为每个 lane 有 2 个元素，`idx` 的低 1 位决定选哪个寄存器，高 5 位决定从哪个 lane 取。**源码依据**：对应逻辑在 `GatherOpToLLVM.cpp:304-344`，用 `applyLinearLayout` 从索引布局算出 column，把 gather 维替换成运行时的 `idxVal`，用 `invSrcLayout` 反算出 `(srcLane, srcReg)`，再对 column 里每个源寄存器做 `shuffleIdx`、用 `srcRegIdx == srcReg` 做 `select`。代价是“每个索引寄存器 × 每个 column 寄存器”条 shuffle，注释里直接写了 “the number of index shuffles is quadratic”（`:247-250`）。所以 gather 的性能非常依赖 layout。

如果 `isWarpLocal()` 为假，`emitGatherInShared` 会把源 tile 写进 shared，`bar.sync`，再把目标索引的 gather 维替换成运行时索引，线性化后 `ld.shared`（`GatherOpToLLVM.cpp:66-140`）。这条路径的 shared 大小是整块源 tile，代价比 shuffle 高，但不受“必须同 warp”的限制。

### 5. histogram：ballot/popcount 还是 shared 原子加

`tl.histogram` 只有 1D int32 输入。kernel：

```python
@triton.jit
def khist(x_ptr, o_ptr, N: tl.constexpr, B: tl.constexpr):
    offs = tl.arange(0, N)
    x = tl.load(x_ptr + offs)
    h = tl.histogram(x, B)
    tl.store(o_ptr + tl.arange(0, B), h)
```

**路径 A：ballot。** B=2、`num_warps=1` 时，PTX 里是 64 组（**实测**）：

```ptx
vote.sync.ballot.b32 %r38, %p2, -1;
popc.b32 %r39, %r38;
```

每个线程有 32 个元素，每个元素对 2 个 bin 各做一次 ballot，所以 `32 × 2 = 64` 组。**源码依据**：见 `HistogramOpToLLVM.cpp:18-43`，对每个 `srcValues[i]` 和每个 bin，构造 `src == bin` 的谓词，`ballot` 得到 32 位掩码，`CtPopOp` 数 1 的个数。计数在每个 lane 上是复制的；如果 `numWarps > 1`，`reduceWarpHistograms` 会构造一个 `[numWarps, numBins]` 的临时 tensor，调用 `tt.reduce` 沿 warp 维求和，并复用 histogram 的 `allocation.offset`（`:46-95`）。

**路径 B：shared 原子加。** B=8、`num_warps=4` 时，PTX 里是（**实测**）：

```ptx
st.shared.b32 [%r3], 0;
bar.sync 0;
...
atom.relaxed.sys.shared.add.u32 %r25, [%r4], 1;
...
bar.sync 0;
ld.shared.b32 %r40, [%r3];
```

**源码依据**：见 `HistogramOpToLLVM.cpp:111-149`，先把 numBins 个 bin 清零（`numElementPerThread = ceil(numBins, threadsPerWarp*numWarps)`），`bar.sync`，每个元素在 `value < numBins` 时对 `shared[value]` 原子加 1，最后 `bar.sync` 后读回。**实测** shared 是 128 B（`max(numBins, 32) * 4`，见 `Allocation.cpp:120-127`）。

T4 上的路径选择（**实测**）：

| B | num_warps | shared | ballot | atom.shared | bar.sync |
|---:|---:|---:|---:|---:|---:|
| 2 | 1 | 0 B | 64 | 0 | 0 |
| 2 | 2 | 128 B | 32 | 0 | 1 |
| 2 | 4 | 128 B | 0 | 8 | 2 |
| 4 | 1 | 128 B | 0 | 32 | 2 |
| 8 | 4 | 128 B | 0 | 8 | 2 |

B=2、W=2 时 `numBins*numWarps=4`，刚好走 ballot；B=2、W=4 时是 8，超过阈值，掉到 shared 原子加。这个边界和 `canUseWarpBallotHistogram` 的 `<= 4` 完全对得上。

### 6. convert_layout：shared 路径 vs shuffle 路径

先看一个“大 transpose”：64×64 f32，`num_warps=4`。

```python
r = tl.arange(0, 64); c = tl.arange(0, 64)
x = tl.load(x_ptr + r[:, None] * 64 + c[None, :])
y = tl.trans(x)
tl.store(o_ptr + r[:, None] * 64 + c[None, :], y)
```

TTGIR 里 `tt.trans` 只是把 layout 从 `#blocked` 改成 `#blocked1`，真正的搬运是后面的 `convert_layout`（**实测**）：

```mlir
#blocked  = #ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [2, 16], warpsPerCTA = [4, 1], order = [1, 0]}>
#blocked1 = #ttg.blocked<{sizePerThread = [4, 1], threadsPerWarp = [16, 2], warpsPerCTA = [1, 4], order = [0, 1]}>
%y = tt.trans %x_9 {order = array<i32: 1, 0>} : ... #blocked -> ... #blocked1
%4 = ttg.convert_layout %y : ... #blocked1 -> ... #blocked
```

这个转换跨 warp，走 shared：`shared=16384 B`，PTX 里 32 条 `st.shared`、1 条 `bar.sync`、32 条 `ld.shared`，0 条 `shfl`（**实测**）；16 KiB 正好是 64×64×4 字节。再看一个小 transpose：8×8 f32，`num_warps=1`。TTGIR 的 layout 是：

```mlir
#blocked  = #ttg.blocked<{sizePerThread = [1, 2], threadsPerWarp = [8, 4], warpsPerCTA = [1, 1], order = [1, 0]}>
#blocked1 = #ttg.blocked<{sizePerThread = [2, 1], threadsPerWarp = [4, 8], warpsPerCTA = [1, 1], order = [0, 1]}>
```

这个转换只在一个 warp 内换 lane/寄存器，`cvtNeedsWarpShuffle` 为真，PTX 里 `shared=0`、`bar.sync=0`，只有 2 条 `shfl.idx` 和几条 `selp`（**实测**）：

```ptx
shfl.sync.idx.b32 %r17, %r16, %r15, 31, -1;
xor.b32 %r18, %r15, 4;
shfl.sync.idx.b32 %r19, %r11, %r18, 31, -1;
selp.b32 %r4, %r19, %r17, %p1;
```

**源码依据**：这正是 `transferWithinWarpSwapImpl` 里“一个 mixed transposition（寄存器位 i 和 lane 位 j 交换）”的 3 阶段实现：`selp → shfl → selp`（`ConvertLayoutOpToLLVM.cpp:394-408`）。`shfl.idx` 的第二个参数是目标 lane，`xor 4` 是交换 lane 位 2。

把 N 从 2 扫到 64（`num_warps=1`，**实测**）：N=2/4 是 0 shared、0 shuffle；N=8 是 0 shared、2 `shfl`；N=16 是 1024 B shared、8 `st.shared`；N=32/64 是 4096 B shared、8 `st.shared`。N=16 开始掉进 shared，N=64 是 4 KiB；前面 64×64、`num_warps=4` 是 16 KiB，因为每个线程要搬的元素更多。**transpose 的代价不是由“转置”这个名字决定的，而是由 layout 差异决定的。**

NVIDIA 后端还有一条更高优先级的 swizzling conversion（`third_party/nvidia/.../ConvertLayoutOpToLLVM.cpp:21-63`），会尝试 `ldmatrix/stmatrix`。T4 的 `supportLdMatrix()` 是 `cc >= 75`，`supportStMatrix()` 是 `cc >= 90`（`include/triton/Dialect/TritonNvidiaGPU/IR/TargetFeatures.h:42-44`），所以 T4 上“读”可以用 ldmatrix，“写”只能退回 `st.shared`。本章的 64×64 例子没有触发 ldmatrix，因为 layout 不是 8×8/16×16 的矩阵 tile；触发条件见 `Utility.cpp:276-288`。

## 进一步用法/重要变体

- **多操作数 reduce**：`tl.argmax` 同时归约值和索引。lowering 里 `accs` 是 `SmallVector<SmallVector<Value>>`，每个操作数一列；`applyCombineOp` 会返回多个结果（`ReduceOpToLLVM.cpp:140-150`、`:152-160`）。看 TTGIR 时会看到 combine region 有 4 个参数、2 个结果。
- **非交换 combine**：`shuffleAndCombine` 在最后用 `shuffleIdx` 把“所有归约位为 0 的 leader lane”的结果广播出去（`:451-460`），避免每一步都对两个操作数排序。自己写 `tl.reduce` 的 combine 时，不要假设编译器会保留你写的左右顺序。
- **scan 的 reverse**：`flip(scan(flip(x)))`，用 `shfl.bfly` 的 xor 掩码翻转 lane（`ScanOpToLLVM.cpp:440-458`）。`reverse=True` 不会多一次 shared 往返，但会多两组 flip shuffle。
- **gather 的 `efficient_layout`**：这是 middle-end 给 gather 选的“不要改”的 layout（`TritonOps.td:1061-1063`）。手改 TTGIR 时如果把它删掉，layout propagation 可能把 gather 推到跨 warp，从而掉进 shared 路径，甚至触发 `expected a warp-local gather` 的 assert（`GatherOpToLLVM.cpp:257-260`）。
- **histogram 的 mask**：`tl.histogram(x, B, mask=m)` 会把 mask 与每个元素的谓词相与（`HistogramOpToLLVM.cpp:27-33`）。mask 是 bool block，形状要能广播到输入。
- **`force_warp_shuffle`**：`ttg.convert_layout` 的可选属性（`TritonGPUOps.td:125`），测试里用它强制走 shuffle 路径（`python/test/gluon/test_lowerings.py:1839`）。正常 kernel 不要手加，除非你确定 layout 可分解。

## 对比与取舍

| 操作 | 线程内 | warp 内 | 跨 warp | shared（本章实测） | barrier | 主要风险 |
|---|---|---|---|---|---|---|
| reduce | 树形 `treeReduce` | `shfl.bfly` | shared partial + 再一轮 warp reduce | `num_warps*4` B | 1+ | 轴跨 warp 时多一次往返 |
| scan | 顺序累加 | `shfl.up` Hillis-Steele | shared partial + 顺序补 | 32 B（W=8） | 1 | 前缀顺序不能树形化 |
| gather | 线程内选择 | `shfl.idx` + `selp` | 不支持，必须落 shared | 整块 tile | 1 | 索引 shuffle 数量随寄存器平方增长 |
| histogram（ballot） | 无 | `vote.ballot` + `popc` | 临时 `tt.reduce` | 0 或 128 B | 0/1 | 只适合 bin 很少 |
| histogram（shared） | 无 | 无 | shared 原子加 | 128 B | 2 | 原子竞争、bin 多时更划算 |
| convert_layout（寄存器） | 重排 SSA | 无 | 无 | 0 | 0 | 几乎没有 |
| convert_layout（shuffle） | 无 | `shfl.idx` + `selp` | 无 | 0 | 0 | 只适用于 <2 个 mixed transposition |
| convert_layout（shared） | 无 | 无 | `st.shared`/`ld.shared` | tile 字节数 | 1+ | 大 tile 占满 shared，barrier 拖慢 |

取舍原则：

1. **能让归约轴留在 warp 内，就不要让它跨 warp。** 32×64 的 axis=1 例子是 16 条 shuffle；1024 的 8 warp 例子是 8 条 shuffle + 32 B shared + 1 个 barrier。后者不一定更慢，但多了一次全 CTA 会合。
2. **gather 优先选 warp-local layout。** shared gather 要写整块源 tile，再读目标；warp-local gather 只搬索引列。代价是后者对 layout 很挑。
3. **histogram 的 bin 少时用 ballot，bin 多时用 shared 原子。** 阈值是 `numBins <= 2 && numBins*numWarps <= 4`，不是“bin 少就一定 ballot”。
4. **convert_layout 的 shared 路径是“最后手段”。** 小 transpose 可以 0 shared、2 shuffle；大 transpose 直接 16 KiB + barrier。写 kernel 时尽量让相邻 op 的 layout 一致，减少 middle-end 插入的 conversion。
5. **T4 没有 `redux.sync`。** `matchReduxKind` 在 `cc < 80` 时直接返回 `nullopt`（`TargetInfo.cpp:98-103`），所以 T4 上所有 warp reduce 都是 shuffle 树；看到 PTX 没有 redux 不是 bug。

## 踩雷集锦

1. **在 T4 上找 `redux.sync`。** `TargetInfo.cpp:98-103` 明确 `computeCapability < 80` 返回 `nullopt`；`warpReduce` 于是走 `shuffleAndCombine`。T4 的 `tl.sum` PTX 里 redux 计数是 0（**实测**）。sm_80+ 的 redux 路径见 `test/Conversion/reduce_to_llvm.mlir:49-57`（guided）。
2. **以为 `tt.trans` 本身会搬数据。** `tt.trans` 只改 layout 属性；真正的 `convert_layout` 在 store 前才出现。8×8 的 transpose 是 0 shared、2 shuffle；64×64 是 16 KiB shared、1 barrier。看 TTGIR 时要找 `ttg.convert_layout`，不要只找 `tt.trans`。
3. **用 bitwise 相等验证 `tl.sum`。** reduce 的加法顺序由 layout 的树形结构决定，和 `torch.sum` 的串行顺序不同；f32 下结果可能差几个 ULP。用 `torch.allclose`，不要用 `torch.equal`。
4. **以为 `tl.histogram` 会统计越界值。** bin 范围是 `[0, num_bins)`；shared 路径有 `icmp_ult` 检查（`HistogramOpToLLVM.cpp:137-138`），ballot 路径用 `icmp_eq` 也不会匹配越界值。负数和 `>= num_bins` 的值直接被丢掉，不会报错。
5. **给 `tl.histogram` 传 2D 或 int64。** 语义层只接受 1D、32 位整数（`semantic.py:1892-1895`），否则直接 `ValueError`。需要统计 2D 数据时先 `tl.reshape` 成 1D。
6. **给 `tl.gather` 传错 rank 或越界索引。** 语义层要求 index 和 src 同 rank、非 gather 维完全相等（`semantic.py:1839-1852`），但不检查索引越界；越界会读到 shared/其他 lane 的脏数据，不会报错。
7. **以为 scan 一定有 barrier。** `num_warps=1` 时 `axisNumWarps == 1`，走 `AddPartialReduceOneWarp`，shared=0、barrier=0（**实测**）。只有跨 warp 的 scan 才需要 shared partial。
8. **手写 TTGIR 时删掉 `allocation.offset`。** reduce 的 `convertLayoutValues` 直接 `assert(baseOffsetAttr && "expected allocation.offset on reduce op")`（`ReduceOpToLLVM.cpp:489-490`）。这个属性由 Allocation 分析写入，不能省。
9. **以为 `tl.max` 的 NaN 行为和 `tl.sum` 一样简单。** `tl.max` 的 `propagate_nan` 决定 combine region；T4 的 `supportMaximumMinimum()` 返回 false（`cc >= 80` 才为真，`TargetFeatures.h:40`），`maximumf` 的 NaN 语义不能直接依赖一条原生指令。跨架构比较 max 结果时先固定 `propagate_nan`。
10. **在大 tile 上连续插多个 convert_layout。** 64×64 f32 一次就是 16 KiB；T4 每个 block 默认只有 48 KiB shared。多个 conversion 的 live range 重叠时 Allocation 不能复用，很容易把 occupancy 打到 1 个 CTA。

## 进阶

- **从 layout 推导 mask。** 取一个 `#ttg.linear`，把 `lane` 的 5 个 basis 列出来，对每个 bit 看它在 axis 维是否为 0，就能手算出 `reduceLaneIdMask` 和 `broadcastLaneIdMask`，再和 PTX 的 shuffle 数量对上。
- **读 `getInterWarpReductionLayout`。** `lib/Analysis/Utility.cpp:224-289` 把 warp/block basis 尽量塞进 lane 维；注释说最多两轮，因为 `warps*blocks <= 64*16 = 2^10`，而 warp 只有 5 位（`ReduceOpToLLVM.cpp:64-68`）。这是“为什么 reduce 最多两轮 warp 归约”的答案。
- **比较 `treeReduce` 的 arity。** T4 是 2；sm_90+ 的 min/max 可能是 3（`TargetInfo.cpp:771-804`）。把同一个 `tl.max` 编到 `cuda:90`，看 LLVM 是否出现三元 combine（guided）。
- **看 histogram 的 scratch 复用与 cluster reduce。** `reduceWarpHistograms` 把 `tt.reduce` 的 `allocation.offset` 指向 histogram 自己的 scratch（`HistogramOpToLLVM.cpp:85-88`）；`sync` 在 `crossCTA` 时调 `clusterBarrier`（`ReduceOpToLLVM.cpp:163-170`），但 T4 的 `num_ctas` 只能是 1（`third_party/nvidia/backend/compiler.py:244-247` 直接拒绝），这条路径要 Hopper+ 才能实测（guided）。

## 动手练习

### 练习 1：数 shuffle

用本章的 `ksum`，分别跑 `num_warps=4/8/16`，打印 `h.metadata.shared` 和 `h.asm['ptx'].count('shfl.sync.bfly')`。验证表里的 `shared = num_warps*4`、`shfl = 5 + log2(num_warps)`。再改成 `N=2048`，看每个线程的元素数变成 8 后，线程内加法从 3 次变成 7 次，但 warp shuffle 数量不变。

### 练习 2：找 convert_layout 的阈值

用 8×8、16×16、32×32、64×64 的 transpose，`num_warps=1`，记录 `shared`、`shfl`、`st.shared`。验证 N=8 是 0 shared、2 shuffle，N=16 开始落 shared。解释为什么 N=8 的 layout 只差一个 mixed transposition，而 N=16 不是。

### 练习 3：让 histogram 换路径

固定 `B=2`，把 `num_warps` 从 1 调到 2、4，记录 `vote.ballot` 和 `atom.shared.add` 的数量。验证 `numBins*numWarps <= 4` 的边界：W=2 还是 ballot，W=4 变成 shared 原子加。再把 `B` 改成 4、8，确认全部走 shared。

### 练习 4：scan 的 reverse

跑 `tl.cumsum(x, axis=0, reverse=True)`，统计 `shfl.sync.up` 和 `shfl.sync.bfly`。验证 reverse 多出的是 `shfl.bfly 31`（翻转 lane），不是额外的 shared 往返。再把 `num_warps` 改成 1，确认 shared 和 barrier 都变成 0。

### 练习 5（guided）：看 redux

在 sm_80+ 机器上跑同一个 `ksum`，或直接看 `test/Conversion/reduce_to_llvm.mlir:49-57` 的 CHECK：`llvm.nvvm.redux.sync.add`。对比 T4 的 `shfl.bfly` 序列，解释为什么 `matchReduxKind` 在 `cc < 80` 返回空。

## 本章重点

- `tt.reduce` 的 lowering 是“线程内树 → warp 内 butterfly → 跨 warp shared partial → 再一轮 warp 归约”，最多两轮跨 warp。
- 跨 warp 必须用 shared memory + barrier，因为 `shfl` 只在 warp 内有效；`num_warps=1` 或轴不跨 warp 时可以完全没有 shared 归约。
- `tt.scan` 不是树形：线程内顺序、warp 内 `shfl.up` 的 Hillis-Steele、跨 warp 用 shared 存每个 warp 的最后一个前缀。
- `tt.gather` 的 warp-local 路径把索引变成 `(srcLane, srcReg)`，用 `shfl.idx + selp` 取数；跨 warp 时退回 shared。
- `tt.histogram` 有两条路径：bin 少时 `ballot + popc`，bin 多时 shared 原子加；阈值是 `numBins <= 2 && numBins*numWarps <= 4`。
- `convert_layout` 有寄存器重排、warp shuffle、shared 三条路径；shared 路径的代价是 tile 字节数 + barrier。
- T4 没有 `redux.sync`、没有 `stmatrix`；看到 PTX 里全是 `shfl` 和 `st.shared` 是正常的。
- 看这类 op 的性能，先看 TTGIR 的 layout，再看 PTX 的 `shfl`/`bar.sync`/`st.shared` 计数，不要只看 Python。

## 自我检核

1. 1024 个 f32、8 个 warp 的 `tl.sum`，每个线程先做几次加法？warp 内几条 `shfl.bfly`？跨 warp 几条？
2. 为什么跨 warp 归约不能只用 `shfl.bfly`？shared memory 和 barrier 各自解决什么问题？
3. `reduceLaneIdMask` 和 `broadcastLaneIdMask` 分别怎么从 lane basis 算出来？
4. 32×64、axis=1、4 个 warp 的 reduce，为什么没有跨 warp 的 shared 归约？PTX 里为什么是 16 条 shuffle？
5. scan 的线程内阶段为什么不能改成树形？`shfl.up` 的谓词 `laneIdAxis >= i` 作用是什么？
6. scan 的 `reverse=True` 为什么要做两次 flip？flip 用的是什么 shuffle 掩码？
7. gather 的 `bfe.u32 %r, idx, 1, 5` 里的 `1` 和 `5` 分别代表什么？为什么低 1 位用来选寄存器？
8. histogram 的 ballot 路径为什么对每个 bin 都要做一次 `vote.ballot`？`popc` 数的是什么？
9. `canUseWarpBallotHistogram` 的三个条件分别防止什么情况？
10. `convert_layout` 的 `cvtNeedsWarpShuffle` 为什么要求 `mixedTranspositions.size() < 2`？不满足时走哪条路径？

## 延伸阅读

1. **`lib/Conversion/TritonGPUToLLVM/ReduceOpToLLVM.cpp:36-105`、`:115-138`、`:266-350`、`:355-460`**
   - 读哪里：`matchAndRewrite` 的三段式、`treeReduce`、`reduceWithinThreads`、`reduceWithinWarps`、`shuffleAndCombine`。
   - 学什么：reduce 怎样从 layout 推导 mask、怎样把线程内/ warp 内/跨 warp 串起来。
   - 与本章关系：本章实验 1、2 的直接源码。

2. **`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TargetInfo.cpp:98-141`、`:630-804`**
   - 读哪里：`matchReduxKind`、`warpReduce32`、`warpReduce`、`getReductionTreeArity`。
   - 学什么：T4 为什么没有 redux、i64 归约怎样拆成高低 32 位、arity 何时为 3。
   - 与本章关系：解释“T4 全是 shuffle”和“sm_90+ 可能用 redux/三元指令”。

3. **`lib/Conversion/TritonGPUToLLVM/ScanOpToLLVM.cpp:30-84`、`:92-245`、`:462-538`**
   - 读哪里：线程内顺序 scan、`warpScan`、`storeWarpAccumulator`、`AddPartialReduce`、`emitFastScan`。
   - 学什么：scan 的前缀顺序怎样在三级之间保持，reverse 为什么用 flip。
   - 与本章关系：本章实验 3 的直接源码。

4. **`lib/Conversion/TritonGPUToLLVM/GatherOpToLLVM.cpp:66-140`、`:193-350`**
   - 读哪里：`emitGatherInShared`、`emitWarpLocalGather`、`pseudoinvert`、`srcRegIdx` 的计算。
   - 学什么：gather 的两种路径、索引到 lane/reg 的映射、为什么 shuffle 数量是平方级。
   - 与本章关系：本章实验 4 的直接源码。

5. **`lib/Conversion/TritonGPUToLLVM/HistogramOpToLLVM.cpp:18-178`、`:233-250`；`lib/Analysis/Utility.cpp:28-33`**
   - 读哪里：`computeWarpHistogram`、`reduceWarpHistograms`、`accumulateSharedHistogram`、`canUseWarpBallotHistogram`。
   - 学什么：ballot/popc 与 shared 原子加的分界，跨 warp histogram 怎样复用 reduce。
   - 与本章关系：本章实验 5 的直接源码。

6. **`lib/Analysis/Utility.cpp:1118-1158`、`:1167-1190`；`lib/Conversion/TritonGPUToLLVM/ConvertLayoutOpToLLVM.cpp:36-92`、`:243-379`；`third_party/nvidia/.../ConvertLayoutOpToLLVM.cpp:21-63`**
   - 读哪里：`cvtNeedsWarpShuffle`、`cvtNeedsSharedMemory`、`transferWithinThread`、`transferWithinWarp`、`transferSwizzlingLocalMem`。
   - 学什么：三条路径的判定条件、swap/ship 的 shuffle 分解、NVIDIA 的 ldmatrix/stmatrix 分支。
   - 与本章关系：本章实验 6 的直接源码。

7. **`test/Conversion/reduce_to_llvm.mlir:20-57`、`test/Conversion/cvt_to_llvm.mlir:13-58`、`test/Conversion/scan_to_llvm.mlir`**
   - 读哪里：REDUX/TERNARY 的 CHECK、shuffle 路径的 CHECK、scan 的 CHECK。
   - 学什么：用 FileCheck 固定 lowering 行为，尤其是 sm_80/90/100 的差异。
   - 与本章关系：把 T4 观察扩展到其他架构（guided）。

## 下一章

本章反复提到 shared memory 的“谁分配、谁复用、谁插 barrier”。下一章就把这块补全：`ttg.local_alloc` 如何变成 `allocation.offset`，Allocation 如何用 liveness + interference graph 复用 shared，Membar 又如何把 RAW/WAR/WAW 变成 `bar.sync`。

→ [Ch 27 Shared Memory Allocation 与同步](./27-shared-memory-allocation-and-synchronization.md)
