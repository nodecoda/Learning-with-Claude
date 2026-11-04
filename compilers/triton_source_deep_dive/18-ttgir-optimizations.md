# Ch 18 — TTGIR 优化

> TTGIR 的第一版 layout 只是占位符；优化 pass 的工作，是把它变成硬件愿意执行的形状。

## 目标

读完本章，你应该能回答：

- `make_ttgir` 里 Coalesce、AccelerateMatmul、OptimizeDotOperands、ReduceDataDuplication、OptimizeAccumulatorInit 各自改什么、为什么必须按这个顺序跑？
- 一条 `tt.load` 的 `#ttg.blocked` 编码是怎么从 axis info 的 contiguity 推出来的？`order` 和 `sizePerThread` 分别由谁决定？
- `tt.dot` 在什么条件下变成 MMAv2、MMAv3、MMAv5，什么条件下退回 FMA？为什么本机 T4（sm_75）拿到的是 FMA？
- 为什么 `ttg.convert_layout` 既是优化对象，又是必须保留的边界？
- accumulator init 优化到底省了什么，为什么它在本机不生效？

本章的主线是 `third_party/nvidia/backend/compiler.py` 的 `make_ttgir`：它是一张有序的 pass 清单，每个 pass 都在回答"这段数据应该以什么布局存在"。读完之后，你看到一段 TTGIR，应该能反推它是被哪个 pass 写成这样的。

## 环境/版本

**实测**（在本机执行）：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | 3.9.0（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5 |
| `python3 -c "import triton"` | `ModuleNotFoundError: No module named 'triton'` |
| 仓库内 `.venv` 的 `import torch` | `ModuleNotFoundError: No module named 'torch'` |
| 本地 `triton-opt` | 不存在（没有构建产物），无法在 T4 上直接跑 pass |

**证据等级**：本章所有 `文件:行号` 都用 `nl -ba` 现场核对过；IR 片段来自仓库自带的 lit 测试期望值（`test/TritonGPU/*.mlir`），标 `源码依据`；涉及 cuda:80/90/100 的编码形态标 `guided`，因为本机无法生成。

**一个必须先纠正的前提**：T4 在本 commit 上**不会**得到 MMAv2。`BlockedToMMA::matchAndRewrite` 在 `lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp:516-521` 里对 `computeCapability < 80` 直接 `return failure()`，并打印 "has been deprecated. It falls back to the FMA path."。lit 测试 `test/TritonGPU/accelerate-matmul.mlir:951-964` 就是这颗用例，函数名叫 `dot_fall_back_fma_before_ampere`。所以本章的 MMA 例子用 `cuda:80`/`cuda:90` 的测试期望值，**在 T4 上不可复现**，一律视为 `guided`。

## 为什么需要这个

TTIR 里没有 layout。`tensor<128x64xf16>` 只是一块逻辑数据，谁都不知道它落在哪些线程的哪些寄存器上。TTGIR 引入 encoding 之后，第一个版本是 `TritonToTritonGPU` 给的默认 `#ttg.blocked`：它能让 IR 合法，但几乎肯定不是最优的。

不跑这些优化，你会得到三种典型损失：

1. **访存不合并**：默认 blocked 的 `order` 可能让相邻线程访问相隔 128 字节的地址，一个 128 字节的 cache line 只用上 4 字节。
2. **convert_layout 泛滥**：每个 op 都用自己的布局，中间全靠 `ttg.convert_layout` 连接；跨 warp 的 convert 要走 shared memory，等于把数据搬两遍。
3. **dot 用不上张量核心**：`tt.dot` 的操作数不是 `dot_op` 编码，就无法降成 `mma.sync` / `wgmma` / `tcgen05`，只能一条条 FMA。

所以 TTGIR 优化的目标函数可以写成三项，同时受两个预算约束：

```text
minimize   (convert_layout 的字节数 × 跨 warp 程度)
         + (全局访存的 transaction 数)
         - (MMA 吞吐)
subject to shared memory ≤ 每 CTA 上限
           registers/thread ≤ 255
```

这三个目标经常互相拉扯：Coalesce 想让 load 的 `order` 跟着内存走，AccelerateMatmul 想让 dot 的布局跟着指令走，两者不一致时就必须插 convert。后面的 RemoveLayoutConversions（`compiler.py:322/324/328`，跑了三次）就是专门来消这些 convert 的。

## 先建立直觉

先记住三个数字，它们决定了 90% 的 TTGIR 形态：

| 字段 | 含义 | 谁决定 |
|---|---|---|
| `order` | 哪一维在内存/线程里最连续，`order[0]` 是连续维 | Coalesce 按 pointer 的 contiguity 排 |
| `sizePerThread` | 每个线程沿各维拿几个元素 | Coalesce 按向量宽度和元素数/线程取 min |
| `warpsPerCTA` | warp 在 tile 上怎么摊 | `num_warps` + AccelerateMatmul 的 `warpsPerTile` |

举个具体例子。一个 `tensor<64x128xf32>`、4 warps、`order=[1,0]`（行主序，最后一维连续）、`sizePerThread=[1,4]`、`threadsPerWarp=[1,32]`、`warpsPerCTA=[4,1]`：

```text
64 行 / 4 个 warp = 每个 warp 负责 16 行
每行 128 列 = 32 个线程 × 每线程 4 个 f32，刚好铺满，没有取模回绕
每个线程拿 4 个连续 f32 = 16 字节；8 个相邻线程拼成一条 128 字节 cache line
```

这就是"coalesced"的直观含义：**一条 cache line 被同一 warp 的相邻线程连续覆盖，一次事务拿满**。如果换成 `order=[0,1]`，相邻线程沿 M 维排开，地址相差一整行（128 × 4 = 512 字节），每个线程只用到 4 字节，一条 128 字节的事务里 124 字节是浪费。

再记一个反向直觉：**MMA 的布局不是按内存连续性设计的**，而是按 `instrShape`（例如 `[16, 8]`、`[16, 16, 16]`）把元素分给固定 lane。所以 `tt.dot` 的 A/B 操作数必须被转成 `#ttg.dot_op<{opIdx, parent, kWidth}>`，这个编码描述的是"在 parent MMA 布局下，第 opIdx 个操作数的切法"。Coalesce 和 AccelerateMatmul 一个管"内存怎么读"，一个管"dot 怎么算"，它们之间的缝就是 convert。

## 核心概念

### encoding 家族

| encoding | 描述什么 | 典型出现位置 |
|---|---|---|
| `#ttg.blocked` | 通用分块分布：`sizePerThread`/`threadsPerWarp`/`warpsPerCTA`/`order` | load/store 的默认布局、elementwise |
| `#ttg.dot_op` | dot 操作数在 parent 布局下的切法，带 `opIdx`、`kWidth` | `tt.dot` 的 A/B |
| `#ttg.nvidia_mma` | MMA 结果布局：`versionMajor`/`versionMinor`/`warpsPerCTA`/`instrShape` | `tt.dot` 的 D |
| `#ttg.swizzled_shared` | shared memory 的 swizzle 参数：`vec`/`perPhase`/`maxPhase`/`order` | `ttg.local_alloc` |
| `#ttg.nvmma_shared` | Hopper/Blackwell 的 128B swizzle，给 TMA/WGMMA 用 | `!ttg.memdesc` |

**源码依据**：`include/triton/Dialect/TritonGPU/IR/TritonGPUAttrDefs.td`（属性定义）；`#ttg.nvidia_mma` 的 verifier 在 `lib/Dialect/TritonGPU/IR/Dialect.cpp:1570-1580`，其中 `versionMajor = 1`（Volta）会直接报 "deprecated and no longer supported"（`Dialect.cpp:1576`）。

### 五个 pass 的分工

| pass | 输入 | 输出 | 在 `make_ttgir` 的位置 |
|---|---|---|---|
| Coalesce | mem op 的默认 blocked | 按 contiguity 重排的 blocked | `compiler.py:319` |
| AccelerateMatmul | blocked/dot_op 的 `tt.dot` | `nvidia_mma` + `dot_op`，或 FMA | `compiler.py:326` |
| OptimizeDotOperands | dot 操作数的 convert/trans | swizzled shared + memdesc 视图 | `compiler.py:329`、`364` |
| ReduceDataDuplication | 需要 smem 的 `cvt → dot_op` | `local_alloc` + `local_load` | `compiler.py:374` |
| OptimizeAccumulatorInit | 带零初始化的 MMA 循环 | `useC`/`useAccumulator = false` | `compiler.py:346`（仅 sm100+） |

注意最后一行：`add_optimize_accumulator_init` 只出现在 `capability // 10 >= 10` 的分支里（`compiler.py:342-346`）。这不是遗漏，而是因为该 pass 的主要模式匹配 `TMEMAllocOp`（`OptimizeAccumulatorInit.cpp:17-55`），TMEM 是 Blackwell 才有的。

### anchor op 与"布局锚点"

`RemoveLayoutConversions` 的算法注释写得很清楚（`lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp:44-60`）：

1. 找出所有 anchor op（布局必须保留的 op）；
2. 把 anchor 的布局向后代传播；
3. 冲突时插入 convert 解决；
4. 按 dominance 顺序重写。

Coalesce 和 AccelerateMatmul 做的事，本质上是**制造更好的 anchor**：前者把 load/store 变成"按内存连续性"的锚点，后者把 dot 变成"按 MMA 指令"的锚点。中间的算术 op 没有强布局偏好，跟着锚点走就行。

## 底层机制

### 1. Coalesce：从 contiguity 到 blocked layout

入口在 `Coalesce.cpp:77-120`：

```cpp
ModuleAxisInfoAnalysis axisInfoAnalysis(moduleOp);
CoalescedEncodingAnalysis coalescedEncodingAnalysis(axisInfoAnalysis);
llvm::MapVector<Operation *, Attribute> layoutMap;
moduleOp.walk([&](Operation *curr) {
  Value ptr = getMemAccessPtr(curr);
  if (!ptr) return;
  // 只处理 tensor<tt.ptr<>> 的 load/store
  ...
  auto layout = coalescedEncodingAnalysis.getEncoding(
      curr, numWarps, threadsPerWarp, cgaLayout, shapePerCTA);
  layoutMap[curr] = layout;
});
for (auto &kv : layoutMap)
  convertDistributedOpEncoding(kv.second, kv.first);
```

**源码依据**：`Coalesce.cpp:87-105` 是收集阶段，`Coalesce.cpp:110-119` 的注释把改写拆成五步——建 L2 布局、把操作数从 L1 转成 L2、用 L2 建新 mem op、把结果转回 L1、替换旧 op。

真正决定布局的是 `CoalescedEncodingAnalysis::getEncoding`（`CoalesceUtils.cpp:125-170`）：

```cpp
auto contiguity = axisInfoAnalysis.getAxisInfo(ptr)->getContiguity();
SmallVector<unsigned> order = getOrderFromContiguity(contiguity);
...
perThread = std::min<int64_t>(it->second, shapePerCTA[order[0]]);
perThread = std::min<int>(perThread, std::max(numElems / numThreads, 1));
SmallVector<unsigned> sizePerThread(refTensorType.getRank(), 1);
sizePerThread[order[0]] = perThread;
return BlockedEncodingAttr::get(..., sizePerThread, order, numWarps, threadsPerWarp, cgaLayout);
```

三个关键点：

- `getOrderFromContiguity` 把维度按 contiguity **降序**稳定排序（`Utility.cpp:94-101`），所以 `order[0]` 就是指针走得最连续的那一维。
- `perThread` 被 `shapePerCTA[order[0]]` 和 `numElems/numThreads` 双重夹住，不会超过"每线程平均元素数"。
- `sizePerThread` 只在 `order[0]` 上非 1，其余维都是 1——这是 blocked 布局最朴素的形态，向量化只发生在连续维。

**例子 1（源码依据）**：`test/TritonGPU/coalesce.mlir:11-20` 的 transpose 用例。输入是 `#blocked1 = sizePerThread=[1,1], threadsPerWarp=[32,1], warpsPerCTA=[4,1], order=[0,1]`，也就是"沿第 0 维连续、每线程 1 个元素"的默认布局。Coalesce 之后：

```mlir
#row = #ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [2, 16], warpsPerCTA = [4, 1], order = [1, 0]}>
#col = #ttg.blocked<{sizePerThread = [4, 1], threadsPerWarp = [16, 2], warpsPerCTA = [1, 4], order = [0, 1]}>

%p = ttg.convert_layout %ptr  : ... -> tensor<64x64x!tt.ptr<f32>, #row>
%v = tt.load %p, %m, %o      : tensor<64x64x!tt.ptr<f32>, #row>
%s = ttg.convert_layout %v   : ... -> tensor<64x64xf32, #col>
tt.store %sp, %s, %sm        : tensor<64x64x!tt.ptr<f32>, #col>
```

（上面的 SSA 名字为了可读性做了简化，编码和 op 顺序与 CHECK 行一致。）

同一个 kernel 里，load 用 `order=[1,0]`、store 用 `order=[0,1]`，这不是 bug：load 的指针沿 N 维连续，store 的指针沿 M 维连续（transpose 语义）。**两个 mem op 各自拿到自己最优的布局，中间用一次 convert 交换**。这正是"锚点冲突用 convert 解决"的教科书案例。

Coalesce 还处理 descriptor load/store，但限制更严：最多 16 字节向量化（`Coalesce.cpp:30-51`，`maxVectorSize = 128 / bitwidth`），因为 TMA 的目标布局会影响 shared memory 的读写形态，而不是 L1 合并访问。

### 2. AccelerateMatmul：选版本、建编码、换 op

版本选择在 `getMMAVersionSafe`（`AccelerateMatmul.cpp:46-87`）：

```cpp
if (computeCapability < 75)        versionsSupported = {1};
else if (computeCapability < 90)   versionsSupported = {2};
else if (computeCapability < 100)  versionsSupported = {3, 2};
else if (computeCapability < 120)  versionsSupported = {5, 2};
else if (computeCapability < 130)  versionsSupported = {2};
```

然后逐个用 `supportMMA(op, version)` 过滤（`lib/Analysis/Utility.cpp:1015-1093`）。关键约束：

- `version == 5`：要求 rank 2、`numWarps` 是 4 或 8、`K >= 256/bitwidth`、M 是 64 的倍数、N 是 8 的倍数、不支持 F64。
- `version == 3`：`K >= 256/bitwidth`、rank 2（batch matmul 回退 v2）、`numWarps % 4 == 0`、M 是 64 的倍数、N 是 8 的倍数。
- 通用：`fp32 x fp32` 且 `inputPrecision != tf32` 直接不支持；fp8 会被当成可提升到 fp16 而放行。

但注意 `BlockedToMMA` 在调用它之前就挡住了 < 80：

```cpp
if (computeCapability < 80) {
  dotOp.emitRemark()
      << "Dot op using MMA for compute capability " << computeCapability
      << " has been deprecated. It falls back to the FMA path.";
  return failure();
}
```

**源码依据**：`AccelerateMatmul.cpp:516-521`。所以 `getMMAVersionSafe` 里 `< 75 → {1}` 这条分支在本 commit 已经不可达（Volta 也被 verifier 拒绝），而 75–79 虽然理论上"支持 v2"，实际被上层短路。

**例子 2（源码依据）**：`test/TritonGPU/accelerate-matmul.mlir` 的期望值。

`cuda:90`（`:3-5`）：

```mlir
#mma  = #ttg.nvidia_mma<{versionMajor = 3, versionMinor = 0, warpsPerCTA = [4, 1], instrShape = [16, 16, 16]}>
#mma1 = #ttg.nvidia_mma<{versionMajor = 3, versionMinor = 0, warpsPerCTA = [4, 1], instrShape = [16, 64, 16]}>
```

`cuda:80`（`:50`）：

```mlir
#mma = #ttg.nvidia_mma<{versionMajor = 2, versionMinor = 0, warpsPerCTA = [1, 8], instrShape = [16, 8]}>
```

**例子 3（源码依据，T4 相关）**：`accelerate-matmul.mlir:951-964`，`ttg.target = "cuda:75"`：

```mlir
// CHECK:   %[[EXT0:.*]] = arith.extf %arg0
// CHECK:   %[[EXT1:.*]] = arith.extf %arg1
// CHECK:   %[[DOT:.*]] = tt.dot %[[EXT0]], %[[EXT1]]
// CHECK:   tt.store %arg2, %[[DOT]]
```

没有任何 `#ttg.nvidia_mma`。dot 保持 blocked 布局，操作数被提升到 f32，最后在 LLVM 层展开成 FMA 循环。这就是本机跑 matmul 时的真实路径——**T4 有 Turing 的 `mma.sync` 硬件，但 Triton 3.9 不再为 sm75 生成 MMAv2**。

编码构造在 `createMMAEncodingForDot`（`AccelerateMatmul.cpp:454-486`）：

```cpp
int versionMinor = computeCapability == 75 ? 1 : 0;
auto instrShape = mmaVersionToInstrShape(versionMajor, retShapePerCTA, eltType, numWarps);
auto warpsPerTile = getWarpsPerTile(dotOp, retShapePerCTA, versionMajor, numWarps, instrShape);
auto mmaEnc = NvidiaMmaEncodingAttr::get(ctx, versionMajor, versionMinor, warpsPerTile, cgaLayout, instrShape);
auto newAcc = ConvertLayoutOp::create(rewriter, loc, newRetType, dotOp.getOperand(2));
```

`versionMinor = computeCapability == 75 ? 1 : 0` 这行说明编码里曾经区分 Turing 和 Ampere 的 MMAv2；现在 sm75 在进入这个函数之前就被挡住了（`AccelerateMatmul.cpp:516-521`），所以这条分支对 `BlockedToMMA` 已无实际作用，但 `versionMinor` 字段仍保留在 `nvidia_mma` 编码里。

之后分两条路（`AccelerateMatmul.cpp:553-599`）：

- **v3**：A（如果不是来自 load）和 B 都被搬进 shared memory，用 `getSharedMemoryMMAOperand` 生成 memdesc，然后建 `ttng.warp_group_dot`。
- **v2**：把 A/B 转成 `dot_op`（`convertDotOperandForMMA`，`AccelerateMatmul.cpp:489-498`），建新的 `tt.dot`。`kWidth` 由 `minBitwidth` 推出：`kWidth = max(32/minBitwidth, 1)`，并保证 `4 * kWidth <= K`（`AccelerateMatmul.cpp:574-591`）。

无论哪条路，结果都会再套一层 convert 转回原来的类型（`AccelerateMatmul.cpp:601-602`）：

```cpp
rewriter.replaceOpWithNewOp<ConvertLayoutOp>(dotOp, dotOp.getType(), newDot->getResult(0));
```

这一步看似浪费，实际上给后面的 RemoveLayoutConversions 留了自由度：如果 dot 的消费者也能接受 MMA 布局，这层 convert 会被消掉；如果消费者是 store，则保留。

**guided（Hopper/Blackwell）**：v3 的 `ttng.warp_group_dot` 在 SM90 上降成 `wgmma.mma_async`，操作数必须来自 shared memory（或寄存器 A）；v5 的 `tcgen05.mma` 操作数在 TMEM，`useAccumulator` 标志控制是否累加。T4 两条都没有。

### 3. OptimizeDotOperands：让 dot 的 shared 形态正确

这个 pass 只有三个 pattern（`OptimizeDotOperands.cpp:324-330`），但每个都在修一类"布局对不上"的问题：

1. `SwizzleShmemConvert`（`OptimizeDotOperands.cpp:24-83`）：匹配 `dot(convert(trans(inner)))`，把内层 convert 的编码换成按 transpose 后形状计算的 `SwizzledSharedEncodingAttr`，并插入 `memdesc_trans`。注释解释了为什么 `needTrans=true`：不修正的话 `vec/maxPhase` 会按错误的 m/n/k 算，生成错误的 swizzle。
2. `FuseTransMMAV3Plus`（`OptimizeDotOperands.cpp:91-133`）：只在消费者是 `WarpGroupDotOp` 或 `MMAv5OpInterface` 时生效，把 `alloc(trans(x))` 改成 `trans(alloc(x))`。MMAv3/v5 能在指令里折叠转置，所以转置应该发生在 shared 视图上，而不是数据上。
3. `ReshapeMemDesc`：把 `alloc(reshape)` 变成 `memdesc_reshape(alloc)`，同理。

一句话总结：**这个 pass 不改变"数据在哪"，只改变"shared memory 的描述方式"，让后续 lowering 能直接吃 MMA 指令要求的 swizzle 和视图**。

### 4. ReduceDataDuplication：把跨线程重排换成一次 shared 往返

源码很短（`ReduceDataDuplication.cpp:28-64`）：

```cpp
mod.walk([&](ConvertLayoutOp cvtOp) {
  if (isa<SharedEncodingTrait>(srcEncoding)) return;
  auto dstDotOp = dyn_cast<DotOperandEncodingAttr>(dstType.getEncoding());
  if (!dstDotOp) return;
  if (!cvtNeedsSharedMemory(cvtOp)) return;
  auto tmpType = MemDescType::get(..., SwizzledSharedEncodingAttr::get(..., dstDotOp, ...), ...);
  auto tmp = LocalAllocOp::create(builder, loc, tmpType, cvtOp.getSrc());
  auto newConvert = LocalLoadOp::create(builder, loc, dstType, tmp);
  cvtOp.replaceAllUsesWith(newConvert.getResult());
  cvtOp.erase();
});
```

它只处理"目标编码是 `dot_op` 且必须过 shared"的 convert。直接做 register→register 的跨线程重排，硬件上只能用 shuffle 一条条搬，或者在每个线程里复制一份数据再丢弃大部分；改写成 `local_alloc + local_load` 之后，数据只写一次 shared、读一次 shared，swizzle 由 `dstDotOp` 反推。

为什么放在 `compiler.py:374`（很靠后）？因为前面的 pipeline、warp specialization 会大量改写 loop 和 buffer，只有在这些结构稳定之后，才能判断一个 convert 是否真的需要 shared。早做会被后续 pass 反复打断。

### 5. OptimizeAccumulatorInit：省掉第一拍的清零

`mma` 的语义是 `D = A*B + C`。如果 `C` 是零，完全可以告诉硬件"这一拍不累加"，省掉读 accumulator 的带宽和一次清零写。

pass 先筛出支持 flag 的 op（`OptimizeAccumulatorInit.cpp:57-70`）：

```cpp
if (auto wgDotOp = dyn_cast<WarpGroupDotOp>(op))
  return !wgDotOp.needsPartialAccumulator();
if (isa<MMAv5OpInterface>(op)) return true;
return false;
```

然后找零初始化（`OptimizeAccumulatorInit.cpp:151-193`），支持三种形态：

- 直接是常量零（`isConstantZeroTensor`）；
- 是循环 block arg，且 `forOp.getInitArgs()[...]` 是零；
- 是 `arith.select` 或 `scf.if` 的一支为零（另一支必须定义在 if 外面）。

命中后设 `useC = false` / `useAccumulator = false`（`OptimizeAccumulatorInit.cpp:241-243`）。Hopper 上这对应 `wgmma` 的 `useC` 立即数；Blackwell 上对应 `tcgen05.mma` 的 `enable-input-d`。**T4 没有这两种指令，pass 也不在 sm75 的 pipeline 里**，所以本机看不到任何效果。

### 6. 顺序为什么是这样

把 `make_ttgir` 的关键顺序抽出来（`compiler.py:315-379`）：

```text
convert_to_ttgpuir            # 给所有 op 一个默认 blocked 编码
coalesce                     # ① 先定"内存怎么读"
f32_dot_tc / assign_cga_layouts
remove_layout_conversions    # 消掉 ① 制造的 convert
optimize_cta_locality
remove_layout_conversions
optimize_thread_locality
accelerate_matmul            # ② 再定"dot 怎么算"
check_matmul_two_cta
remove_layout_conversions    # 消掉 ② 制造的 convert
optimize_dot_operands        # ③ 修 dot 操作数的 shared 形态
...
[sm80/90: assign_latencies → schedule_loops → pipeline]
[sm100:  optimize_accumulator_init → ... → warp_specialize → pipeline]
...
optimize_dot_operands        # ③ 再来一次（pipeline 之后形态变了）
coalesce_async_copy
...
remove_layout_conversions
reduce_data_duplication      # ④ 最后把跨线程 convert 换成 shared
reorder_instructions
lower_mma                    # 把 dot 降成具体指令
```

三条规律：

1. **先 Coalesce 后 AccelerateMatmul**：Coalesce 只看指针连续性，不需要知道 dot 用什么 MMA；反过来则要先猜 MMA 再改内存布局，容易来回震荡。
2. **每改一次布局就跟一次 RemoveLayoutConversions**：布局是全局约束，局部改动会产生新的 convert，必须立刻消。
3. **ReduceDataDuplication 和 lower_mma 放最后**：它们依赖前面所有结构（loop、buffer、partition）已经定型。

## 进一步用法/重要变体

### `num_warps` 的影响

`warpsPerTileV2`（`AccelerateMatmul.cpp:89-125`）和 `warpsPerTileV3`（`:126-152`）决定 warp 怎么摊到 M/N 上。v2 走 `getMmaV2WarpsPerCTA`；如果检测到链式 dot（flash attention 那种），会优先 `{numWarps, 1}` 或 `{1, numWarps}`，让 reduction 留在同一个 warp 里。v3 从 `{4, 1}` 起步（MMAv3 的最小 warp 形状），按 `shape[0] > 16*ret[0]` 决定扩 M 还是扩 N。

改 `num_warps` 会同时改 blocked 的 `warpsPerCTA`、MMA 的 `warpsPerTile`、以及 shared memory 的分配，**不是免费的并行度旋钮**。

### 环境变量开关

- `DISABLE_MMA_V3=1`：`supportMMA(..., 3)` 直接返回 false（`Utility.cpp:1060-1061`），强制回 v2。
- `DISABLE_MMA_V5=1`：同理（`Utility.cpp:1025-1026`）。

这两个变量适合做 A/B：同一份 kernel，只改环境变量，看 TTGIR 里 `versionMajor` 和后续 PTX 的差异。

### 用 IR override 手改编码

`TRITON_KERNEL_OVERRIDE=1` + `TRITON_OVERRIDE_DIR` 可以替换某个 stage 的 IR（Ch 0 讲过）。改 TTGIR 里的 `#ttg.blocked` 参数是做布局实验最快的方式，但要注意：改完之后 verifier 会检查 `sizePerThread` 的乘积是否覆盖 shape，不合法的编码会直接报错。

### 和 descriptor/TMA 的关系

descriptor load/store 不走 `Coalesce` 的通用路径，而是 `pickDescriptorLoadStoreLayout`（`Coalesce.cpp:30-69`），向量宽度上限 16 字节。原因是 TMA 的源/目标是 shared memory，布局影响的是 shared 的 bank 冲突和 swizzle，而不是 L1 合并。

### Gluon 的显式布局

Ch 29 会讲 Gluon：它把这套 encoding 直接暴露给用户，你可以手写 `#ttg.blocked` 和 `#ttg.nv_mma_shared`，跳过 Coalesce 的猜测。理解本章的 pass 顺序，才能判断手写布局会不会被后续 pass 改掉。

## 对比与取舍

| pass | 收益 | 代价/风险 | 什么时候不划算 |
|---|---|---|---|
| Coalesce | 访存事务数下降、向量化变宽 | 插入 convert；load/store 布局可能冲突 | 指针连续性差（gather/scatter）、descriptor 小 tile |
| AccelerateMatmul | 用上张量核心，吞吐数量级提升 | 需要 shared memory 和特定 shape；v3 要 4 warp 起步 | shape 太小、K 不够、fp32 非 tf32、sm<80 |
| OptimizeDotOperands | 消掉 trans/reshape 的数据搬运 | 只对 v3/v5 有效；swizzle 算错会静默出错 | v2 路径基本无事可做 |
| ReduceDataDuplication | 跨线程 convert 只搬一次 | 多一块 shared buffer；多一次 barrier | convert 本来就能用 shuffle 完成 |
| OptimizeAccumulatorInit | 省一次 accumulator 读+清零 | 只对 wgmma/tcgen05 有效 | sm<90；partial accumulator 场景 |

再看 MMA 版本的选择（前两列是本机能验证的，后两列 `guided`）：

| 路径 | 指令 | 操作数位置 | 本机 sm75 |
|---|---|---|---|
| FMA | `fma.rn.f32` 循环 | 寄存器 | ✅ 实际走这条 |
| MMAv2 | `mma.sync.aligned.m16n8k*` | 寄存器 | ❌ 本 commit 被禁用 |
| MMAv3 | `wgmma.mma_async` | shared（A 可寄存器） | ❌ 需要 sm90 |
| MMAv5 | `tcgen05.mma` | TMEM | ❌ 需要 sm100 |

## 踩雷集锦

1. **"T4 有 Turing 的 mma.sync，所以 TTGIR 里应该有 `nvidia_mma`。"**
   错。本 commit 的 `BlockedToMMA` 对 `< 80` 直接失败（`AccelerateMatmul.cpp:516-521`），测试用例名就叫 `dot_fall_back_fma_before_ampere`（`accelerate-matmul.mlir:951-964`）。你在 T4 上看到的 matmul TTGIR 是 blocked + `tt.dot`，PTX 里是 FMA 循环。想验证 MMA 路径，必须用 sm80+ 的机器或 `triton-opt` 跑 lit 测试。

2. **"Coalesce 会把所有 op 的布局都改一遍。"**
   它只改 mem op（`tt.load`/`tt.store`/atomic/async copy）和 descriptor op 的布局，其余 op 靠 RemoveLayoutConversions 传播。`Coalesce.cpp:91-96` 明确只处理 `tensor<tt.ptr<>>`。

3. **"看到 `ttg.convert_layout` 就删掉，性能一定更好。"**
   convert 是布局边界的合法表达。删掉它意味着让某个 op 接受它不擅长的布局（比如让 store 用 MMA 布局），通常更慢。正确做法是改 producer 的布局，让 convert 变成 no-op，或者让它落到 shared 的一次往返上。

4. **"ReduceDataDuplication 是在去重相同的 tensor 值。"**
   它不比较值。它只把"目标为 `dot_op` 且需要 shared 的 convert"改写成 `local_alloc + local_load`（`ReduceDataDuplication.cpp:34-62`），减少的是跨线程重排时的数据复制，不是 SSA 层面的重复。

5. **"accumulator init 优化能加速我的 T4 matmul。"**
   `add_optimize_accumulator_init` 只在 sm100+ 的 pass 列表里（`compiler.py:342-346`），匹配的是 `TMEMAllocOp` 和 `MMAv5OpInterface`（`OptimizeAccumulatorInit.cpp:17-55`）。T4 上这个 pass 根本没跑。

6. **"改了 `num_warps`，dump 出来的 TTGIR 却没变。"**
   先确认不是 cache hit。`compile` 在 `compiler.py:267-279` 命中缓存就直接返回，不跑 stage；加 `TRITON_ALWAYS_COMPILE=1`。其次确认 `num_warps` 进了 cache key（它在 `options` 里，会参与 hash）。

## 进阶

- **用 linear layout 读 CoalescedEncoding**：Ch 13 讲过 `toLinearLayout`。把 `#ttg.blocked` 转成 linear layout 后，"相邻线程是否连续"就变成基向量的排列问题，比看 `order` 更本质。
- **自己实现一个 layout anchor**：在 `Coalesce.cpp` 里给某类 op 加一条 `layoutMap` 记录，观察 RemoveLayoutConversions 如何传播。这是理解 anchor 机制最快的方法。
- **读 `getMmaV2WarpsPerCTA` 和 `mmaVersionToInstrShape`**：它们决定了 `instrShape` 和 `warpsPerTile` 的组合空间，也是"为什么我的 128x128 tile 用了 8 warps 还是慢"的答案来源。
- **对比 `RemoveLayoutConversions` 的三次调用**（`compiler.py:322/324/328`）：分别在 Coalesce 后、CTA locality 后、AccelerateMatmul 后。试着删掉中间那次，看 convert 数量怎么变（需要能构建的环境）。

## 动手练习

> 本机没有 `triton-opt`，以下命令都需要一个构建过的源码树（`cmake --build` 产物在 `build/` 或 wheel 的 `bin/`）。没有构建环境时，可以直接读测试文件的 CHECK 行做静态推演。

1. **复现 Coalesce**：`triton-opt test/TritonGPU/coalesce.mlir --tritongpu-coalesce`，找到 `@transpose`，确认 load 和 store 拿到的 `order` 相反，并解释为什么。
2. **复现版本选择**：把 `accelerate-matmul.mlir` 里某个 `cuda:80` 的 module 改成 `cuda:75` 再跑，观察 `nvidia_mma` 消失、`arith.extf` 出现。
3. **数 convert**：对同一个 matmul kernel，分别在 `--tritongpu-coalesce` 之后和 `--tritongpu-accelerate-matmul` 之后 dump，数 `ttg.convert_layout` 的条数，解释增减。
4. **改编码**：用 IR override 把某个 `tt.load` 的 `#ttg.blocked` 的 `order` 反过来，观察 verifier 是否报错、PTX 的 `ld.global` 宽度是否变化（`guided`，需要 GPU）。
5. **读 accumulator 逻辑**：在 `OptimizeAccumulatorInit.cpp:151-193` 里找出"`scf.if` 的一支为零"的判定条件，构造一个不满足它的反例。

## 本章重点

- TTGIR 优化的目标函数是：减少 convert、提高访存合并、匹配 MMA，同时守住 shared memory 和寄存器预算。
- Coalesce 按 pointer 的 contiguity 定 `order`，按向量宽度和元素数/线程定 `sizePerThread`，只改 mem op。
- AccelerateMatmul 按 capability 和 shape 选 MMA 版本；本 commit 对 sm<80 直接回退 FMA，T4 没有 MMAv2。
- OptimizeDotOperands 只改 shared 的描述方式（swizzle、memdesc 视图），不改数据位置。
- ReduceDataDuplication 把"需要 shared 的 `cvt → dot_op`"显式写成 `local_alloc + local_load`，放在 pipeline 之后。
- OptimizeAccumulatorInit 只对 wgmma/tcgen05 生效，靠 `useC`/`useAccumulator` 省掉零初始化。
- 顺序的核心逻辑：先定内存布局，再定计算布局，每次改动后立刻消 convert。

## 自我检核

1. `CoalescedEncodingAnalysis::getEncoding` 里 `perThread` 被哪两个量夹住？如果 shape 是 `128x64`、4 warps、fp16，`order=[1,0]`，`perThread` 最大是多少？
2. 为什么 `BlockedToMMA` 在 `computeCapability < 80` 时直接失败，而不是生成 MMAv2？
3. `#ttg.dot_op` 的三个字段分别是什么含义？`kWidth` 和 K 的关系是什么？
4. `ReduceDataDuplication` 处理的是哪一类 convert？它为什么放在 `make_ttgir` 的末尾？
5. `OptimizeAccumulatorInit` 支持哪三种零初始化形态？为什么 `scf.if` 的另一支必须定义在 if 外面？

## 延伸阅读

1. **`lib/Dialect/TritonGPU/Transforms/Coalesce.cpp:77-120` 与 `CoalesceUtils.cpp:125-170`**
   - 读哪里：`runOnOperation` 的收集循环，以及 `getEncoding` 的 `order`/`perThread` 计算。
   - 学什么：布局不是猜的，是从 `AxisInfo` 的 contiguity 推出来的；`order[0]` 的选取规则。
   - 与本章关系：这是"内存怎么读"的答案，也是后面所有 convert 的来源。

2. **`lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp:46-152`、`454-605`**
   - 读哪里：`getMMAVersionSafe` 的版本表、`createMMAEncodingForDot`、v2/v3 两条分支。
   - 学什么：capability → 版本 → `instrShape`/`warpsPerTile` → 操作数编码的完整链条。
   - 与本章关系：解释 TTGIR 里 `nvidia_mma` 每个字段的来历，以及 T4 为什么没有它。

3. **`lib/Analysis/Utility.cpp:1015-1116`**
   - 读哪里：`supportMMA` 的三个重载。
   - 学什么：dtype、K、M/N 对齐、numWarps 如何共同决定"能不能用 MMA"。
   - 与本章关系：版本选择的下游约束；写 kernel 时的 tile 尺寸选择直接受它影响。

4. **`lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp:44-60`**
   - 读哪里：文件头的四步算法注释。
   - 学什么：anchor → 传播 → 冲突消解 → 重写的全局布局分配思路。
   - 与本章关系：Coalesce/AccelerateMatmul 制造 anchor，这个 pass 负责让 anchor 之间的 convert 最少。

5. **`test/TritonGPU/coalesce.mlir`、`accelerate-matmul.mlir`、`accumulator-init.mlir`**
   - 读哪里：每个用例的 `RUN:` 行和 `CHECK:` 行。
   - 学什么：pass 的精确输入输出形态；`cuda:75/80/90/100` 的差异。
   - 与本章关系：本机没有 GPU 构建时，这是唯一能"看到真实 IR"的途径。

## 下一章

布局定好之后，下一个问题是时间：一个 load→dot 循环里，load 的几百 cycle 延迟如何被藏起来。下一章进入软件流水线，看 `num_stages` 如何把 loop 拆成 prologue/steady/epilogue，以及 `ttg.async_copy` 和 `ttg.async_wait` 是在哪一步被插进去的。

→ [Ch 19 软件流水线](./19-software-pipelining.md)
