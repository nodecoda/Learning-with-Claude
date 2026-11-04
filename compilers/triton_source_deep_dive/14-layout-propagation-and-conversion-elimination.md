# Ch 14 — 布局传播与转换消除

> `ttg.convert_layout` 不是免费的“类型转换”。它要么重排寄存器，要么做 warp shuffle，要么把整块数据绕一圈 shared memory；编译器的大部分布局工作，就是在决定“这个转换该不该存在”。

## 目标

读完本章，你应该能：

- 说清楚 `ttg.convert_layout` 的三种 lowering 路径，以及为什么 shared memory 路径最贵；
- 解释 `RemoveLayoutConversions` 的四个阶段：anchor、forward propagation、conflict resolution、rewrite；
- 看懂 backward rematerialization 的代价模型，知道它什么时候会拒绝重算；
- 给定一段 TTGIR，判断两个首尾相接的 `convert_layout` 能不能被消掉；
- 解释 `tt.load` 的布局如何反向传播到 `tt.splat` / `tt.addptr`；
- 说出 layout 冲突的三种解法（改 producer、改 consumer、插 convert），以及为什么“少一个 convert”不一定更快；
- 知道 `Coalesce` 为什么会主动插入 `convert_layout`，以及 `LayoutPropagationUtility` 的作用。

本章的主线是 `lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp`。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 3.9.0。
- 机器：Tesla T4，`sm_75`；本章的 pipeline 顺序和 Hopper/Blackwell 的例子标 **guided**。
- 本章源码行号以该 commit 为准，使用 `nl -ba` 核对。
- **实测**：本章没有伪造 TTGIR 输出。带 `CHECK` 的片段来自真实测试文件，标 **源码依据**；示意性的 before/after 明确标 **guided（示意，不是 dump）**。
- **源码依据**：pass 结构、anchor 判定、传播逻辑、代价模型、canonicalization 规则都来自源码。
- **guided**：T4 上无法验证的 Hopper/Blackwell 路径、真实 GPU 上的性能数字。

## 为什么需要这个

TTGIR 里每个 tensor 都带 encoding。两个 op 相接时，如果 encoding 不同，MLIR 类型就不匹配，必须插 `ttg.convert_layout`。但转换很贵：

- **寄存器重排**：最便宜，只是把同一线程的寄存器顺序换一下，通常零成本；
- **warp shuffle**：用 `shfl` 在 lane 之间搬数据，有指令开销，但不用 shared memory；
- **shared memory**：store 到 shared，barrier，load 回来；有 shared 容量、bank conflict、同步开销。

`ConvertLayoutOpToLLVM` 在 `lib/Conversion/TritonGPUToLLVM/ConvertLayoutOpToLLVM.cpp:46-66` 按 `minimalCvtLayout` 的结果选择路径：

```cpp
if (conversion.getNumInDims() == 0) -> 直接替换
if (conversion.getNumInDims() == 1) -> transferWithinThread
if (cvtNeedsWarpShuffle(op))        -> transferWithinWarp
else                                -> transferSwizzlingLocalMem
```

`源码依据`：`ConvertLayoutOpToLLVM.cpp:46-66`；`cvtReordersRegisters` / `cvtNeedsWarpShuffle` / `cvtNeedsSharedMemory` 在 `lib/Analysis/Utility.cpp:1118-1157`。

所以“消除一个 convert”可能省下几微秒，也可能只是省了一条无意义的寄存器 move；反过来，为了消一个 convert 而重算一大段 producer，可能更慢。`RemoveLayoutConversions` 的核心就是在这两者之间做取舍。

## 先建立直觉

先看一段示意 TTGIR（**guided，不是 dump**）：

```mlir
%a = ... : tensor<64x64xf32, #blocked0>
%b = ttg.convert_layout %a : tensor<64x64xf32, #blocked0> -> tensor<64x64xf32, #blocked1>
%c = ttg.convert_layout %b : tensor<64x64xf32, #blocked1> -> tensor<64x64xf32, #blocked2>
```

两个 convert 首尾相接。如果 `#blocked0 -> #blocked1 -> #blocked2` 可以直接合成 `#blocked0 -> #blocked2`，那中间那个就没必要。MLIR 的 canonicalization 里有一条很直接的规则：

```cpp
// cvt(cvt(x, type1), type2) -> cvt(x, type2)
if (auto cvt = dyn_cast<ConvertLayoutOp>(arg)) {
  rewriter.replaceOpWithNewOp<ConvertLayoutOp>(
      op, op->getResultTypes().front(), cvt.getSrc());
}
```

`源码依据`：`lib/Dialect/TritonGPU/IR/Ops.cpp:391-396`，属于 `CanonicalizeConvertFromConvert`（`342-396`）。

这条规则只处理“直接嵌套”的 convert。更多时候，convert 不是直接嵌套，而是被 elementwise、broadcast、reduce、scf 隔开；这时就需要 `RemoveLayoutConversions` 的传播和重算。

## 核心概念

### 1. `convert_layout` 是类型不匹配的产物

TTGIR 是强类型的：`tensor<64x64xf32, #blocked0>` 和 `tensor<64x64xf32, #blocked1>` 是不同类型。一个 op 的 operand 要求 `#blocked1`，你手里是 `#blocked0`，就必须转换。

转换本身不改变逻辑值，只改变“哪个线程持有哪个元素”。所以它理论上可以被：

- **删掉**：如果 producer 能直接产出目标布局；
- **合并**：如果两个转换能合成一个；
- **推迟/提前**：如果转换能穿过 elementwise/broadcast，减少转换的数据量；
- **保留**：如果两边都不能改，或者转换很便宜。

### 2. 三种解法

| 解法 | 改谁 | 典型手段 | 代价 |
|---|---|---|---|
| 改 producer | 让 producer 直接产出目标布局 | backward rematerialization、改 load 的 layout | 可能重算 producer |
| 改 consumer | 让 consumer 接受 producer 的布局 | forward propagation、改 reduce/dot 的输入布局 | 可能让 consumer 变慢 |
| 插 convert | 两边都不改 | conflict resolution、`getValueAs` | 一次转换的代价 |

`源码依据`：pass 的注释在 `RemoveLayoutConversions.cpp:44-61` 描述了“anchor → 传播 → 解决冲突 → 重写”的流程；`getValueAs` 在 `474-487` 在需要时插入 `ConvertLayoutOp`。

### 3. anchor：布局的“锚点”

不是所有 op 的布局都能随便改。有些 op 的布局是性能关键，甚至是正确性要求：

- `tt.dot`：operand/accumulator 必须是 MMA 布局；
- 昂贵的 `tt.load` / `tt.store`：global access 的布局决定 coalescing；
- `DescriptorOpInterface`：TMA descriptor 的布局有硬件要求；
- 带 `efficient_layout` 的 op：producer 明确要求某个布局。

这些 op 的结果布局被当作“锚点”，pass 从它们出发，把布局向前/向后传播。

`源码依据`：`isLayoutAnchor` 在 `RemoveLayoutConversions.cpp:223-237`：

```cpp
if (isa<DescriptorOpInterface>(op)) return true;
if (isa<LoadOp, StoreOp>(op)) return isExpensiveLoadOrStore(op);
if (isa<DotOpInterface, AtomicOpInterface, TMEMLoadOp>(op)) return true;
if (hasEfficientLayout(op)) return true;
```

注意两个细节：

1. **store 不总是 anchor**。`isExpensiveLoadOrStore`（`lib/Dialect/TritonGPU/Transforms/Utility.cpp:641-654`）在 tensor 元素数小于 `numWarps * threadsPerWarp` 时返回 false；小 store 的布局可以随便改。
2. **reduce 不是硬 anchor**。`isLayoutAnchor` 没有列 `ReduceOp`；但 `propagateToUsers` 会把布局透传给 reduce（`344-349`），reduce 的 lowering 又对布局有强假设，所以它经常成为“事实上的锚点”。

### 4. forward propagation：从 anchor 向 users 推

`LayoutPropagation::propagateLayout` 是一个 worklist 算法：

1. 把 anchor 的 encoding 放进 `layouts[value]`；
2. 从队列里取一个 value，把它的 encoding 推给所有 users；
3. 如果 user 的 encoding 变了，把 user 的结果加回队列；
4. 直到不动点。

`源码依据`：`propagateLayout` 在 `RemoveLayoutConversions.cpp:355-376`；`propagateToUsers` 在 `286-353`。

`propagateToUsers` 处理这些情况：

- `scf.for` / `scf.while` / `scf.yield` / `scf.condition`：把布局推到 iter_arg、result、after_arg；
- elementwise / `SameOperandsAndResultEncoding`：直接透传；
- `BroadcastOp`、`ReduceOp`、`ExpandDimsOp`、`ReshapeOp`、`TransOp`、`JoinOp`、`SplitOp`、`ConvertLayoutOp`：调用 `inferDstEncoding` 推导；
- `GatherOp`：只通过 indices 传播（`332-340`）；
- 带 `efficient_layout` 的 reshape：不传播（`341-343`）。

`源码依据`：`286-353`。

### 5. conflict resolution：一个 value 有多个候选布局

一个 value 可能有多个祖先 anchor，每个 anchor 都想把自己的布局传下来。于是 `layouts[value].encodings` 可能有多个候选。

`resolveConflicts` 的启发式很朴素：

- 默认取第一个；
- 如果 value 是 load/store/atomic，优先 `BlockedEncodingAttr`；
- 否则优先 `MmaEncodingTrait`。

`源码依据`：`RemoveLayoutConversions.cpp:378-397`。

这不是最优解，只是一个“够用”的启发式。真正的取舍发生在后面的 remat 和 rewrite。

### 6. rewrite：按决定的布局重写 IR

`rewriteRegion` 按 dominance 顺序遍历 region：

- 如果 op 的结果已经有目标 encoding，跳过；
- 如果需要改，调用 `rewriteOp`；
- 如果不需要改，但仍然要 remap operands，用 `getValueAs` 插入必要的 convert。

`源码依据`：`rewriteRegion` 在 `RemoveLayoutConversions.cpp:423-472`；`rewriteOp` 在 `648-669`；`getValueAs` 在 `474-487`。

`getValueAs(value, encoding)` 的逻辑很关键：

```cpp
if (value 的 encoding == encoding) return value;
// 否则在 value 后面插一个 ConvertLayoutOp
```

也就是说，**冲突无法通过改 producer/consumer 解决时，最终会在这里落成一个 `convert_layout`**。

## 底层机制

### 1. pass 的整体顺序

`runOnOperation` 的五个阶段：

```cpp
// 1. 从 anchor 出发，forward propagate，解决冲突，重写
layoutPropagation.initAnchorLayout();
layoutPropagation.propagateLayout();
layoutPropagation.resolveConflicts();
layoutPropagation.rewrite();

cleanupConvertOps();

// 2. 对剩下的 convert，尝试 backward remat
do {
  changed = backwardRematerialization(m, disableRematSplitting);
  cleanupConvertOps();
} while (changed && ++numIterations < 20);

// 3. hoist convert above cast/ext/broadcast
hoistConvert(m, disableRematSplitting);

// 4. 清理 dead iter args
runDeadIterArgElimination(m);

// 5. canonicalize convert 和 scf
```

`源码依据`：`RemoveLayoutConversions.cpp:1580-1648`。

这个顺序说明：**先尽量改 consumer（forward），再改 producer（backward remat），最后才保留 convert**。hoist 是“转换还在，但让它更便宜”的优化。

### 2. 例一：`cvt(cvt(x))` 被 canonicalize 消掉

示意（**guided**）：

```mlir
// before
%1 = ttg.convert_layout %0 : #blocked0 -> #blocked1
%2 = ttg.convert_layout %1 : #blocked1 -> #blocked2

// after
%2 = ttg.convert_layout %0 : #blocked0 -> #blocked2
```

`源码依据`：`Ops.cpp:391-396`。这条规则不关心 `#blocked1` 是否“有用”；它只做类型层面的合并。合并后如果 `#blocked0 == #blocked2`，`CanonicalizeConvertFromConvert` 的第一条规则（`349-353`）会把它删掉。

**注意**：这条规则是 canonicalization，不是 `RemoveLayoutConversions`。它发生在 pass 的 cleanup 阶段，或者任何跑 canonicalizer 的地方。

### 3. 例二：load 的布局反向传播到 splat/addptr

看真实测试 `test/TritonGPU/remove-layout-conversions-backward-remat-reuse.mlir:11-25`：

```mlir
%0 = tt.splat %arg1 : i32 -> tensor<64x1xi32, #blocked1>
%2 = tt.broadcast %0 : tensor<64x1xi32, #blocked1> -> tensor<64x2xi32, #blocked1>
%3 = ttg.convert_layout %2 : tensor<64x2xi32, #blocked1> -> tensor<64x2xi32, #blocked>
...
%7 = tt.splat %arg0 : !tt.ptr<f32> -> tensor<64x2x!tt.ptr<f32>, #blocked>
%8 = tt.addptr %7, %6 : tensor<64x2x!tt.ptr<f32>, #blocked>, tensor<64x2xi32, #blocked>
%9 = ttg.convert_layout %8 : tensor<64x2x!tt.ptr<f32>, #blocked> -> tensor<64x2x!tt.ptr<f32>, #blocked1>
%10 = tt.load %9 : tensor<64x2x!tt.ptr<f32>, #blocked1>
%11 = ttg.convert_layout %10 : tensor<64x2xf32, #blocked1> -> tensor<64x2xf32, #blocked>
```

`源码依据`：`test/TritonGPU/remove-layout-conversions-backward-remat-reuse.mlir:11-25`。

这里 `%9` 是一个“为了 load 而转换指针布局”的 convert。pass 看到 `%10` 的 load 是 anchor（`isExpensiveLoadOrStore` 为真，因为 64×2 > 128 个线程？这里 128 个元素，4 warps×32=128，条件 `numElements < 128` 为假，所以是 expensive），于是把 `#blocked1` 反向传播：

- `%9` 被删掉；
- `%8` 的 addptr 改成 `#blocked1`；
- `%7` 的 splat 改成 `#blocked1`；
- `%6` 的 convert 也可能被重算/复用。

最终 CHECK 是：

```text
// CHECK-COUNT-2: tt.broadcast
// CHECK-COUNT-1: ttg.convert_layout
// CHECK-NOT: ttg.convert_layout
```

`源码依据`：`test/...backward-remat-reuse.mlir:3-6`。

两个 broadcast 的 convert 都被消掉，只剩下 `%11`（load 结果从 `#blocked1` 转回 `#blocked`）这一个 convert。为什么 `%11` 不能消？因为函数返回值要求 `#blocked`，而 load 必须是 `#blocked1`；两边的布局冲突，只能保留一个 convert。

这个例子也说明：**convert 不一定能全消**。pass 的目标不是“零 convert”，而是“把 convert 放在最便宜的位置”。

### 4. 例三：convert 穿过 transpose

真实测试 `test/TritonGPU/canonicalize.mlir:243-255`：

```mlir
#linear = #ttg.linear<{register = [[0, 1], [8, 0], [16, 0]], lane = [[0, 2], [0, 4], [1, 0], [2, 0], [4, 0]], warp = [[0, 8], [0, 16]], block = []}>
#blocked = #ttg.blocked<{sizePerThread = [1, 2], threadsPerWarp = [8, 4], warpsPerCTA = [1, 4], order = [1, 0]}>
#blocked_trans = #ttg.blocked<{sizePerThread = [2, 1], threadsPerWarp = [4, 8], warpsPerCTA = [4, 1], order = [0, 1]}>

tt.func @infer_trans(%arg0: tensor<32x32xf32, #linear>) -> tensor<32x32xf32, #blocked_trans> {
  // CHECK-NOT: ttg.convert_layout
  %0 = ttg.convert_layout %arg0 : tensor<32x32xf32, #linear> -> tensor<32x32xf32, #blocked>
  %1 = tt.trans %0 {order = array<i32: 1, 0>} : tensor<32x32xf32, #blocked> -> tensor<32x32xf32, #blocked_trans>
  tt.return %1 : tensor<32x32xf32, #blocked_trans>
}
```

`源码依据`：`test/TritonGPU/canonicalize.mlir:243-255`；`CanonicalizeConvertFromTranspose` 在 `Ops.cpp:203-228`。

这里 convert 不是被删掉，而是“穿过 transpose”：`trans(convert(x))` 变成 `convert(trans(x))`，然后 transpose 直接消费 `#linear`，产出 `#blocked_trans`。最终 `CHECK-NOT: ttg.convert_layout`，因为 convert 被吸收进了 transpose 的布局推导。

这个例子说明：**convert 的位置比数量更重要**。把 convert 推到 transpose 前面/后面，可能让它消失，也可能让它变便宜。

### 5. backward rematerialization：重算 producer

forward 只能改 consumer；如果 consumer 是 anchor，就只能改 producer：用目标布局把 producer 重算一遍。`backwardRematerialization` 取 src 的 backward slice，用 `canBeRematerialized` 过滤，估算成本，克隆 slice，并复用 `existingRemats`。`源码依据`：`RemoveLayoutConversions.cpp:1181-1219`、`906-921`；`canBeRematerialized` 在 `Utility.cpp:686-700`，拒绝写副作用、dot、atomic、`efficient_layout`、`scf.while` 和昂贵 load/store。

### 6. 代价模型：convert vs remat

convert：寄存器重排成本 0；否则 `32 * getByteCount(src, 32, 32)`，即 shared store/load 各 8×bytes，再乘 2 算同步（`RemoveLayoutConversions.cpp:1005-1023`）。

remat：constant 0；普通算术 1×bytes，`isExpensiveMathOp`（div/exp/log/sin/sqrt...）8×bytes；每线程元素数变多乘 `getCostFactor`；只在 slice 内使用时 `factor -= 1`；load 8×bytes；reduce 用 intra-warp + 8×inter-warp 唯一数据，非结合 reduce 直接拒绝；最后比较 `convertLayoutCost >= rematerialisationCost`。`源码依据`：`1026-1032`、`1112-1178`。这是启发式，不是精确周期模型。

### 7. remat 的复用：两个 broadcast 只重算一次

`%3` 和 `%6` 的 convert 都来自同一个 `%0`。分别重算会克隆两个 broadcast；pass 用 `existingRemats` 复用第一次的 remat，所以最终只有两个 broadcast（原始 + remat），而不是四个。`源码依据`：`RemoveLayoutConversions.cpp:166-199`、`694-860`；测试 CHECK 在 `3-6`。

### 8. 为什么某些 op 是 anchor

| op | 是否硬 anchor | 原因 | 例外 |
|---|---|---|---|
| `tt.dot` | 是 | MMA operand/accumulator 布局必须匹配指令 | 无 |
| 昂贵 `tt.load` | 是 | global coalescing 决定带宽 | 元素数 < 线程数时不是 |
| 昂贵 `tt.store` | 是 | 同上 | 小 store 不是 |
| `tt.atomic` | 是 | 原子操作的布局影响正确性/性能 | 无 |
| `tt.reduce` | 否（软） | lowering 对布局有强假设，通常保留 | 标量 reduce 可以改 src |
| 带 `efficient_layout` 的 op | 是 | producer 明确要求 | 无 |
| 函数参数 | 是 | 方便测试/边界 | 无 |

`源码依据`：`RemoveLayoutConversions.cpp:223-259`；`reduceToScalar` 在 `417-421`。

`reduce` 的“软锚点”很值得注意：它不是 `isLayoutAnchor` 的成员，但 `propagateToUsers` 允许布局穿过 reduce（`344-349`），而 reduce 的 lowering 又通常要求 blocked/slice 布局。所以实际效果是“reduce 附近的布局很少被大改”。

## 进一步用法/重要变体

### 1. `disable-remat-splitting`

pass 有一个选项：

```bash
triton-opt %s -tritongpu-remove-layout-conversions='disable-remat-splitting=true'
```

默认允许 remat 只重算 slice 的一部分；开启后，如果 slice 里的值还被 slice 外使用，就拒绝重算。

`源码依据`：`RemoveLayoutConversions.cpp:1107-1110`；测试 `test/TritonGPU/remove-layout-conversions-disable-remat.mlir:1-22`。

测试里 `nested_convert_single_use` 和 `nested_convert_multi_use` 展示了两种行为的差异：默认会把 convert 拆成两个（一个给 slice 内，一个给 slice 外），`disable-remat-splitting` 则保留原来的 convert。

### 2. hoist convert

`hoistConvert` 做三件事：

- `hoistConvertOnTopOfExtOrBroadcast`：把 convert 推到 `ext`/`broadcast` 上面，减少转换的数据量；
- `hoistConvertIntoConditionals`：把 convert 推进 `scf.if`，避免在条件外转换；
- `hoistConvertDotOperand`：把 dot operand 的 convert 推到合适位置。

`源码依据`：`RemoveLayoutConversions.cpp:128-155`；`1341-1420`；`1221-1339`。

这些优化的共同思路是：**convert 还在，但让它处理更少的数据，或者放在更冷的分支**。

### 3. `force_warp_shuffle`

`ttg.convert_layout` 可以带 `force_warp_shuffle`：

```mlir
%1 = ttg.convert_layout %0 {force_warp_shuffle} : ... -> ...
```

verifier 要求转换只在 warp 内，不能涉及 warp/block 维：

```cpp
if (llvm::is_contained(dims, warp) || llvm::is_contained(dims, block))
  return emitOpError("force_warp_shuffle requires a conversion within one warp");
```

`源码依据`：`Ops.cpp:425-436`。

这是给测试/性能实验用的开关，不是常规写法。

### 4. `Coalesce` 为什么会插入 convert

`Coalesce` 的目标是让 global load/store coalesced。它用 axis info 的 contiguity 选 `order`，用 slice 分析选 `sizePerThread`，然后：

1. 给 memory op 选一个 coalesced layout；
2. 把 operand 从原布局转到新布局；
3. 新建 memory op；
4. 把结果转回原布局；
5. 替换所有 uses。

`源码依据`：`lib/Dialect/TritonGPU/Transforms/Coalesce.cpp:77-119`；`CoalescedEncodingAnalysis::getEncoding` 在 `CoalesceUtils.cpp:127-170`。

所以 `Coalesce` 是“convert 的生产者”：它为了 coalescing 主动插 convert。`RemoveLayoutConversions` 随后尝试把这些 convert 消掉/挪走。两者是一对：一个为了内存布局牺牲转换，一个为了转换重新安排布局。

**纠正一个常见说法**：`Coalesce.cpp` 并不直接调用 `getDefaultBlockedEncoding`。它用的是 `CoalescedEncodingAnalysis::getEncoding`（`CoalesceUtils.cpp:127-170`）。`getDefaultBlockedEncoding` 主要用在 `Dialect.cpp:3479-3481` 的 reshape no-op 判断和 `Pipeliner/LowerLoops.cpp:304` 的 1D 布局。

### 5. `LayoutPropagationUtility`

`LayoutPropagationUtility.h` 提供一个工具：

```cpp
std::optional<std::pair<triton::LoadOp, LinearLayout>>
inferSourceLoadLayout(const LinearLayout &dstLayout, Operation *defOp);
```

它从 `defOp` 沿 def chain 往上走，遇到 `convert_layout` 就跳过，遇到单 operand op 就 `inferSrcEncoding`，直到找到 `tt.load`，返回 load 和它应该用的 layout。

`源码依据`：`include/triton/Dialect/TritonGPU/Transforms/LayoutPropagationUtility.h:10-18`；实现 `LayoutPropagationUtility.cpp:14-43`。

这个工具被 AMD 的 `AccelerateAMDMatmul.cpp:457` 用来推断 scale 的 load layout。它不是 `RemoveLayoutConversions` 的主路径，但它是“布局反向传播”思想的一个可复用封装。

## 对比与取舍

### 三种解法的取舍

| 解法 | 适用 | 优点 | 风险 |
|---|---|---|---|
| 改 producer | producer 便宜、可重算 | 彻底删 convert | 重算可能复制 load/math |
| 改 consumer | consumer 对布局不敏感 | 不增加计算 | consumer 可能变慢 |
| 插 convert | 两边都是 anchor | 简单、正确 | shared/shuffle 开销 |

### 为什么“少一个 convert”不一定更快

1. **convert 可能是免费的**。`cvtReordersRegisters` 返回 true 时，成本是 0；删掉它没有收益，反而可能让 remat 复制算术。
2. **remat 可能复制昂贵 op**。代价模型对 `exp/log/div` 乘 8，对 load 按 8×bytes；但真实 GPU 上 L1 命中率、寄存器压力、occupancy 都会影响结果。
3. **改布局可能破坏 coalescing/MMA**。一个 shared convert 可能比“让 load 变成跨行访问”更便宜。
4. **convert 可能被流水线隐藏**。在循环里，convert 可能和 load 重叠；把它改成循环内的重算，可能打断流水线。
5. **寄存器压力**。remat 增加同时活跃的值，可能降低 occupancy；convert 反而只占临时 shared。

所以正确的指标不是“convert 数量”，而是“关键路径上的 shared/shuffle 次数 + global transaction 数 + occupancy”。

## 踩雷集锦

1. **只数 `ttg.convert_layout` 的个数。**
   一个寄存器重排的 convert 成本 0；一个 shared convert 可能很贵。数量不等于成本，先看 `minimalCvtLayout` 的分类。

2. **以为 store 一定是 anchor。**
   `isLayoutAnchor` 只在 `isExpensiveLoadOrStore` 为真时把 store 当 anchor；小 store 不是。源码在 `RemoveLayoutConversions.cpp:228-229`、`Utility.cpp:641-654`。

3. **以为 reduce 是硬 anchor。**
   `isLayoutAnchor` 没有列 `ReduceOp`；它是“软锚点”。不要用“reduce 是 anchor”解释所有布局保留。

4. **以为 remat 一定划算。**
   代价模型会拒绝“重算 exp/log/非结合 reduce/昂贵 load”。看 `isRematBeneficial` 的 DEBUG 输出（`1173-1176`）。

5. **开 `disable-remat-splitting` 后行为变了。**
   这个选项会拒绝“slice 外还有 use”的重算；测试 `remove-layout-conversions-disable-remat.mlir` 展示了差异。

6. **对 `force_warp_shuffle` 的转换涉及 warp/block。**
   verifier 在 `Ops.cpp:425-436` 直接拒绝。它只能用于 warp 内转换。

7. **把 `cvt(cvt)` 的 canonicalization 当成 `RemoveLayoutConversions`。**
   前者在 `Ops.cpp:391-396`，是局部 pattern；后者是跨 op 的传播/重算。两者都会消 convert，但机制不同。

8. **为了消 convert 删掉 `tt.trans`。**
   transpose 是语义的一部分，不是布局转换。`CanonicalizeConvertFromTranspose` 是把 convert 穿过 transpose，不是删 transpose。

9. **忽略 shared convert 的 barrier。**
   `transferSwizzlingLocalMem` 需要 store/barrier/load；代价模型乘 2 算同步（`1020-1023`）。在循环里，这会打断流水线。

10. **把 T4 的结论套到 Hopper。**
    Hopper 有 WGMMA/TMA，很多 convert 会走 shared/TMA 路径；T4 没有。pipeline 顺序也不同（`third_party/nvidia/backend/compiler.py:319-328` vs `342-357`）。

## 进阶

- 用 `triton-opt -tritongpu-remove-layout-conversions -debug-only=tritongpu-remove-layout-conversions` 看每个 convert 的传播/remat 决策；DEBUG 输出在 `1173-1176`。
- 读 `rewriteSlice`（`694-860`），理解 remat 如何克隆 op、如何处理 `scf.yield` 的重复 operand。
- 读 `hoistConvertOnTopOfExtOrBroadcast`（`1341-1420`），理解“减少转换字节数”的优化。
- 构造一个“convert 在循环里”的 kernel，比较 `disable-remat-splitting` 开/关的 TTGIR。
- 读 `CoalesceUtils.cpp:127-170`，理解 `order` 和 `perThread` 如何从 axis info 推出来。

## 动手练习

1. **数 convert**：写一个 transpose+scale kernel，dump TTGIR，数 `ttg.convert_layout`；然后去掉 `tl.trans`，再数一次。解释差异。**guided**：需要构建 Triton。

2. **cvt(cvt)**：构造 `convert_layout(convert_layout(x))` 的 TTGIR，跑 `triton-opt -canonicalize`，确认合并成一条。**guided**：需要 `triton-opt`。

3. **remat**：对 `test/TritonGPU/remove-layout-conversions-backward-remat-reuse.mlir` 跑 pass，确认 `CHECK-COUNT-2 broadcast`、`CHECK-COUNT-1 convert`。**guided**。

4. **anchor**：把测试里的 `tensor<64x2xf32>` 改成 `tensor<1xf32>`，观察 load 是否还是 anchor（元素数 < 线程数）。**源码依据**：`Utility.cpp:641-654`。

5. **disable-remat-splitting**：对 `remove-layout-conversions-disable-remat.mlir` 跑默认和 `disable-remat-splitting=true`，对比 `nested_convert_multi_use` 的输出。**guided**。

6. **代价模型**：读 `isRematBeneficial`，手算一个 `convert`（64×64 f32）和一个 `exp` remat 的成本，判断哪个更贵。**源码依据**：`1005-1023`、`1112-1178`。

## 本章重点

- `convert_layout` 有三种 lowering：寄存器重排、warp shuffle、shared memory；成本依次升高。
- `RemoveLayoutConversions` 的顺序是：anchor → forward propagation → conflict resolution → rewrite → backward remat → hoist。
- anchor 包括 dot、昂贵的 load/store、atomic、descriptor、带 `efficient_layout` 的 op；reduce 是软锚点。
- forward propagation 改 consumer；backward remat 改 producer；冲突无法解决时 `getValueAs` 插 convert。
- remat 的代价模型：convert 32×bytes（寄存器重排为 0）；算术 1×/8×bytes；load 8×bytes；reduce 有单独成本。
- `cvt(cvt)` 由 canonicalization 合并；convert 穿过 transpose/broadcast 由 pattern 处理。
- `Coalesce` 为了 coalescing 主动插 convert，`RemoveLayoutConversions` 再尝试消掉/挪走。
- “少一个 convert”不等于更快；要看转换类型、关键路径、寄存器和 occupancy。

## 自我检核

1. `convert_layout` 的三种 lowering 路径分别是什么？哪种最贵？
2. `RemoveLayoutConversions` 的四个阶段是什么？为什么先 forward 再 backward？
3. 哪些 op 是硬 anchor？store 和 reduce 为什么特殊？
4. forward propagation 遇到 `scf.for` 怎么处理？遇到 `GatherOp` 呢？
5. `getValueAs` 什么时候插入 convert？它和 `resolveConflicts` 是什么关系？
6. remat 的代价模型如何估算 convert 和 arithmetic？为什么 `exp` 乘 8？
7. 为什么 `cvt(cvt)` 能被 canonicalize 消掉？这条规则在哪个文件？
8. `Coalesce` 为什么主动插 convert？它和 `RemoveLayoutConversions` 如何配合？
9. 为什么“少一个 convert”不一定更快？至少说三个原因。
10. `force_warp_shuffle` 的限制是什么？

## 延伸阅读

- **`lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp`**
  - **读哪里**：`44-61`（算法概述）、`223-259`（anchor）、`262-397`（传播/冲突）、`423-487`（rewrite/getValueAs）、`1005-1179`（代价模型）、`1181-1219`（backward remat）、`1580-1648`（pass 顺序）。
  - **学什么**：布局传播和转换消除的完整实现。
  - **与本章关系**：本章的主线。
- **`include/triton/Dialect/TritonGPU/Transforms/LayoutPropagationUtility.h`**
  - **读哪里**：`10-18`；实现 `LayoutPropagationUtility.cpp:14-43`。
  - **学什么**：如何从目标布局反推 load 布局。
  - **与本章关系**：反向传播的可复用封装。
- **`lib/Dialect/TritonGPU/Transforms/Coalesce.cpp` / `CoalesceUtils.cpp`**
  - **读哪里**：`Coalesce.cpp:77-119`；`CoalesceUtils.cpp:127-170`。
  - **学什么**：convert 是怎么被主动插入的。
  - **与本章关系**：理解“convert 的生产者”。
- **`test/TritonGPU/remove-layout-conversions-*.mlir`**
  - **读哪里**：`backward-remat-reuse.mlir`、`disable-remat.mlir`、`scf-cleanup.mlir`。
  - **学什么**：真实输入/期望输出，CHECK 计数。
  - **与本章关系**：把 pass 行为变成可验证的断言。
- **`lib/Dialect/TritonGPU/IR/Ops.cpp`**
  - **读哪里**：`203-228`（transpose）、`342-396`（convert canonicalization）。
  - **学什么**：局部 pattern 如何消 convert。
  - **与本章关系**：`cvt(cvt)` 和 `trans(convert)` 的规则。
- **`lib/Conversion/TritonGPUToLLVM/ConvertLayoutOpToLLVM.cpp`**
  - **读哪里**：`26-66`（路径选择）、`94-200`（shared 实现）。
  - **学什么**：convert 最终变成什么指令。
  - **与本章关系**：成本模型的硬件依据。
- **`third_party/nvidia/backend/compiler.py`**
  - **读哪里**：`291-390`。
  - **学什么**：NVIDIA pipeline 里 `remove-layout-conversions` 出现的位置。
  - **与本章关系**：为什么同一个 pass 在 TTGIR 阶段跑多次。

## 下一章

→ [Ch 15 — 内存层次、TMA 与屏障](./15-memory-hierarchy-tma-and-barriers.md)
