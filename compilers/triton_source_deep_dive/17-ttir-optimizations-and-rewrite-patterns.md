# Ch 17 — TTIR 优化与 rewrite pattern

> 优化不是“把 IR 变短”，而是“在正确的时机，用正确的合法性证明，换掉一小段 IR”。

## 目标

读完本章，你应该能：

- 说清 `PatternRewriter::matchAndRewrite` 的契约：什么时候可以改 IR，失败时必须保证什么；
- 解释 `PatternBenefit` 如何决定 pattern 的优先级，以及为什么不能依赖同 benefit 的先后顺序；
- 区分 canonicalizer 与专用 pass 的分工，知道一个 rewrite 应该放在哪一边；
- 读懂 `Combine.cpp` / `Combine.td`、`ReorderBroadcast.cpp`、`LoopUnroll.cpp`、`LoopInvariantCodeMotion.cpp`、`LoopAwareCSE.cpp`、`RewriteTensorDescriptorToPointer.cpp` 的核心 pattern；
- 用 TTIR 例子说明 `addptr(addptr)`、`select(load(splat))`、`dot+add`、`broadcast(elementwise)`、`scf.for` LICM/CSE 的前后形态；
- 判断一次“优化”为什么可能让 layout 更差，以及怎样用 TTGIR 验证。

本章的主线是 `lib/Dialect/Triton/Transforms/`。这些 pass 都在 TTIR 层工作，还没有 layout encoding；它们改的是“值和结构”，不是“线程映射”。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5 |
| 驱动 | 535.216.01 |
| CUDA | 12.4，`V12.4.131` |
| 系统 Python | 3.10.12 |

**源码依据**：

- `include/triton/Dialect/Triton/Transforms/Passes.td:6-80`：六个 TTIR pass 的声明。
- `lib/Dialect/Triton/Transforms/Combine.cpp:24-293`：`CombineAddPtr`、`CombineSelectMaskedLoad`、`CombineDotAdd`、`CombineBroadcastMulReduce`。
- `lib/Dialect/Triton/Transforms/Combine.td:11-21`：TableGen 版 `CombineAddPtrPattern`。
- `lib/Dialect/Triton/Transforms/ReorderBroadcast.cpp:39-229`：splat/broadcast 重排。
- `lib/Dialect/Triton/Transforms/LoopUnroll.cpp:24-57`：基于 `tt.loop_unroll_factor` 的展开。
- `lib/Dialect/Triton/Transforms/LoopInvariantCodeMotion.cpp:23-94`：LICM 与 load hoisting。
- `lib/Dialect/Triton/Transforms/LoopAwareCSE.cpp:27-153`：iter arg 等价类合并。
- `lib/Dialect/Triton/Transforms/RewriteTensorDescriptorToPointer.cpp:557-635`：descriptor 到指针的 dialect conversion。

**guided**：本机没有从该 commit 构建出的 `triton-opt`，所以 `triton-opt ...` 命令和“跑出来是什么样”的 IR 都标 `guided`。IR 片段是按源码和测试文件推导的形态，不是本机实测输出。

## 为什么需要这个

TTIR 优化看起来只是“删几条指令”，但它决定了后面 TTGIR 的起点：

- `addptr(addptr(p, i0), i1)` 合成一个 `addptr`，减少地址计算的中间值，也让 `tt.divisibility` 等属性更容易传播；
- `select(cond, load(ptrs, splat(cond), other), other2)` 折成 `load(ptrs, splat(cond), other2)`，消掉一次 select 和一份 mask；
- `dot(a, b, 0) + c` 折成 `dot(a, b, c)`，让 MMA 直接累加，少一次 add；
- `broadcast(elementwise(x))` 改成 `elementwise(broadcast(x))` 的反向，先把计算放在小 shape 上，再 broadcast，减少中间 tensor 的寄存器占用；
- LICM 把循环不变量（包括只读 load）提出循环，减少每轮重复计算；
- loop-aware CSE 合并“每轮都相等”的 iter arg，减少循环携带值。

这些变换都在 TTIR 层完成，此时还没有 `#blocked` / `#mma`，所以 pattern 只需要证明“值等价”，不需要证明“layout 合法”。但一旦进入 TTGIR，layout 的选择会反过来影响这些优化的收益。本章最后一节会讲这个取舍。

## 先建立直觉

一个 rewrite pattern 的最小骨架是：

```cpp
struct MyPattern : public OpRewritePattern<SomeOp> {
  LogicalResult matchAndRewrite(SomeOp op, PatternRewriter &rewriter) const override {
    // 1. 检查所有前提；不满足就 return failure()，此时绝不能改 IR
    if (!precondition(op))
      return failure();

    // 2. 用 rewriter 创建/替换/删除
    auto newOp = rewriter.create<OtherOp>(op.getLoc(), ...);
    rewriter.replaceOp(op, newOp.getResult());
    return success();
  }
};
```

把它想成“事务”：

```text
match 阶段：只读，收集证据
rewrite 阶段：原子地改，改完通知 rewriter
失败：IR 必须和进入时一模一样
```

`applyPatternsGreedily` 会反复把 pattern 应用到 worklist，直到没有 pattern 能匹配。它不保证“全局最优”，只保证“局部收敛”。

## 核心概念

### 1. `RewritePattern` 的三要素

| 要素 | 作用 | 例子 |
|---|---|---|
| root op | pattern 从哪个 op 开始匹配 | `arith.select`、`tt.reduce`、`scf.for` |
| benefit | 多个 pattern 匹配同一 root 时谁先试 | `CombineSelectMaskedLoadPattern` 用 3 |
| generated ops | 这个 pattern 可能创建哪些 op | `tt.load`、`arith.addi` |

`CombineSelectMaskedLoadPattern` 的构造函数（`Combine.cpp:72-76`）把三要素写得很清楚：`RewritePattern(arith::SelectOp::getOperationName(), 3, context, {LoadOp::getOperationName()})`。root 是 `arith.select`，benefit 是 3，generated op 是 `tt.load`。generated ops 用于依赖跟踪/调试，不是匹配条件。

### 2. `PatternRewriter` 的契约

必须遵守的几条：

1. **失败不改 IR。** `matchAndRewrite` 返回 `failure()` 时，IR 必须和进入时一致。很多 bug 是“先 `op->erase()` 再发现条件不满足”。
2. **用 rewriter，不要用裸 `op->erase()`。** `rewriter.eraseOp(op)` 会通知 driver 更新 worklist；裸 erase 会让 driver 持有悬空指针。
3. **原地修改用 `modifyOpInPlace`。** 比如 `CombineSelectMaskedLoadPattern` 只改 load 的 `other` operand（`Combine.cpp:108-110`）。
4. **替换用 `replaceOp` / `replaceOpWithNewOp`。** `replaceOp` 会替换所有 uses 并删除旧 op；`replaceAllUsesWith` 只换 uses，旧 op 留给 DCE。
5. **不要在 match 阶段创建 op。** 创建 op 也是修改 IR；如果后面失败，IR 已经脏了。

`CombineDotAddPattern` 是一个“先检查、后原地改”的例子（`Combine.cpp:232-260`）：先检查 dot 是否只有一个 use、累加器是否为零、`maxNumImpreciseAcc` 是否为 0，全部通过后才 `modifyOpInPlace` 改 dot 的 `c`，再 `moveBefore`，最后 `replaceAllUsesWith`。

### 3. benefit 与优先级

`PatternBenefit` 是“尝试顺序”，不是“正确性”：benefit 高的先试；相同则顺序不保证；greedy driver 每次只应用一个 pattern 再重新入队，所以两个 pattern 可能交替生效。`Combine.cpp` 里 select 折叠 benefit = 3（`75`），`CombineBroadcastMulReducePattern` = 1（`138`），TableGen `Pat` 和 `OpRewritePattern` 默认 = 1（`Combine.td:17-21`、`Combine.cpp:227-230`）。所以 select 先试，但失败后 driver 会试下一个。

### 4. canonicalizer 与专用 pass 的分工

| 维度 | canonicalizer | 专用 pass（combine/reorder/LICM/CSE/unroll） |
|---|---|---|
| 注册方式 | 每个 op 的 `getCanonicalizationPatterns` | `Passes.td` + `GEN_PASS_DEF` |
| 作用域 | 单个 op 及其直接 operands | 整个 module/loop |
| 利润假设 | 几乎总是“更规范” | 依赖代数、循环、属性 |
| 分析 | 不用 analysis | LICM/CSE 需要 dominance/equivalence |
| 运行时机 | 任何需要规范化的地方 | pipeline 的固定位置 |

`make_ttir` 的顺序是 `canonicalizer → combine → reorder-broadcast → CSE → loop-unroll`（`compiler.py:297-302`）。canonicalizer 先把 IR 收敛到规范形态，combine 才能稳定匹配；combine 之后再 CSE，才能合并新产生的重复表达式。经验法则：“op 自己简化自己” → canonicalizer；“两个 op 的代数关系” → combine；“需要看整个循环/函数” → LICM/CSE/unroll；“会改变 shape/控制流” → 专用 pass。

### 5. TableGen pattern

`Combine.td:17-21` 定义了一个最简单的 TableGen pattern：

```tablegen
def CombineAddPtrPattern : Pat<
        (TT_AddPtrOp:$src (TT_AddPtrOp $ptr, $idx0), $idx1),
        (TT_AddPtrOp:$dest $ptr, (Arith_AddIOp $idx0, $idx1, DefOverflow)),
        [(Constraint<CPred<"isAddPtrOffsetCombinable($0, $1)">> $idx0, $idx1)],
        [(CopyDiscardableAttrs $src, $dest)]>;
```

它匹配 `addptr(addptr(ptr, idx0), idx1)`，生成 `addptr(ptr, addi(idx0, idx1))`，并带两个额外动作：

- `Constraint`：调用 `isAddPtrOffsetCombinable`，证明偏移可合并；
- `CopyDiscardableAttrs`：把 `tt.divisibility`、`tt.contiguity`、`tt.constancy`、`tt.pointee_type` 等属性从旧 addptr 复制到新 addptr（`Combine.td:13-15`）。

TableGen 生成的 pattern 默认 benefit = 1。它在 `Combine.cpp:68` 被 `#include "TritonCombine.inc"` 引入，然后在 `CombineOpsPass` 里 `patterns.add<CombineAddPtrPattern>(context)`（`286`）。

## 底层机制

### 1. `CombineAddPtr`：地址计算的合并

**Before（guided）**：

```mlir
%p1 = tt.addptr %p, %i0 : !tt.ptr<f32>, i32
%p2 = tt.addptr %p1, %i1 : !tt.ptr<f32>, i32
```

**After（guided）**：

```mlir
%idx = arith.addi %i0, %i1 : i32
%p2 = tt.addptr %p, %idx : !tt.ptr<f32>, i32
```

合法性来自 `isAddPtrOffsetCombinable`（`Combine.cpp:24-63`）：

- 如果两个偏移类型相同且元素位宽是 64，直接允许（`48-51`）；
- 否则，只有两个偏移都是常量，且 `sadd_ov` 证明不溢出，才允许（`53-60`）；
- 其余情况返回 false，pattern 不匹配。

这意味着 `addptr(p, %i0)` + `%i1`（两个 32 位非常量）不会被合并。不是 pattern 漏了，而是它不能证明 `i0 + i1` 不溢出。指针算术溢出是未定义行为，编译器不能随便重结合。

### 2. `CombineSelectMaskedLoad`：`tt.load` + `tt.splat` 的折叠

**Before（guided）**：

```mlir
%m = tt.splat %cond : i1 -> tensor<256xi1>
%v = tt.load %ptr, %m, %other : tensor<256x!tt.ptr<f32>>
%r = arith.select %cond, %v, %other2 : tensor<256xf32>
```

**After（guided）**：

```mlir
%v2 = tt.load %ptr, %m, %other2 : tensor<256x!tt.ptr<f32>>
```

pattern 的条件（`Combine.cpp:84-106`）：

- select 的 true value 必须由 `tt.load` 定义；
- load 必须有 mask；
- mask 必须由 `tt.splat` 定义，且 splat 的源等于 select 的 condition；
- load 的结果只能有一个 use；
- `other2` 必须 properly dominate load（否则把 `other2` 移进 load 会改变求值时机）。

满足后，它把 load 的 `other` 改成 `other2`，然后用 load 的结果替换 select（`108-111`）。语义是：当 `cond` 为真时 load 返回真实值，select 选它；当 `cond` 为假时 load 返回 `other`，select 选 `other2`。折叠后 load 在假分支返回 `other2`，等价。

这个 pattern 也说明了“为什么先看 dominance”：`other2` 可能是一个在 select 之后才计算的值，直接移进 load 会提前求值，可能引入副作用或未定义值。

### 3. `CombineDotAdd`：把 add 折进 MMA 累加器

**Before（guided）**：`%d = tt.dot %a, %b, %zero : tensor<128x64xf16> * tensor<64x64xf16> -> tensor<128x64xf32>`，然后 `%r = arith.addf %d, %c`。
**After（guided）**：`%r = tt.dot %a, %b, %c : tensor<128x64xf16> * tensor<64x64xf16> -> tensor<128x64xf32>`。

条件（`Combine.cpp:232-253`）：add 的某一侧由 dot 定义；dot 只有一个 use；dot 的累加器是零（`isZero`，`20-22`）；浮点 dot 还要求 `maxNumImpreciseAcc == 0`（`249-253`）。最后一条表示“不允许近似累加”，改了累加器可能破坏数值语义，所以 pattern 选择放弃。

### 4. `CombineBroadcastMulReduce`：`sum(x[:,:,None] * y[None,:,:], 1) -> dot`

**Before（guided）**：`x` 从 `64x32x1` broadcast 到 `64x32x128`，`y` 从 `1x32x128` broadcast 到同形状，`mulf` 后 `tt.reduce` axis 1。
**After（guided）**：`%r = tt.dot %x, %y, %zero : tensor<64x32xf32> * tensor<32x128xf32> -> tensor<64x128xf32>`。

条件（`Combine.cpp:145-174`）：

- reduce 的 rank 必须是 3，axis 必须是 1；
- combiner 必须是 f32 的 add；
- reduce 的 operand 必须是 `arith.mulf`；
- mul 的两边都必须由 `tt.broadcast` 定义；
- 左边 broadcast 只沿 axis 2，右边只沿 axis 0；
- broadcast 的源 shape 在 M/N 方向至少 16（`173-174`）。

这个 pattern 是“优化改变结构”的典型：它把 elementwise + reduce 变成 `tt.dot`，性能通常大幅提升，但也把后面的 layout 从普通 blocked 推向 dot operand。M/N 小于 16 时它拒绝匹配，因为 MMA 的 tile 太小，收益不够。

### 5. `ReorderBroadcast`：先算小 shape，再广播

`MoveSplatAfterElementwisePattern`（`39-101`）把 `mulf(splat(x), splat(y))` 变成 `splat(mulf(x, y))`；`MoveBroadcastAfterElementwisePattern`（`106-206`）处理：

**Before（guided）**：

```mlir
%a = tt.broadcast %x : tensor<1x64xf32> -> tensor<128x64xf32>
%c = tt.splat %s : f32 -> tensor<128x64xf32>
%r = arith.addf %a, %c : tensor<128x64xf32>
```

**After（guided）**：

```mlir
%cs = tt.splat %s : f32 -> tensor<1x64xf32>
%t = arith.addf %x, %cs : tensor<1x64xf32>
%r = tt.broadcast %t : tensor<1x64xf32> -> tensor<128x64xf32>
```

为什么能减少寄存器：Before 的 `arith.addf` 在 `128x64` 上算，每个线程要持有大 tile 的中间值；After 在 `1x64` 上算，再广播。广播本身是布局操作，通常比“大 tile 的逐元素计算 + 大 tile 的中间值”便宜。

限制（`121-141`）：

- 所有 operand 必须是 broadcast 或 splat（或 splat 常量）；
- 只能有一个不同的 broadcast 源 shape；多个不同源 shape 会拒绝；
- 至少要有一个 broadcast，否则 pattern 不匹配。

`ReorderBroadcastPass` 还加了 `BroadcastOp` / `ExpandDimsOp` / `ReshapeOp` 的 canonicalization patterns（`218-220`），然后 `applyPatternsGreedily`（`226`）。所以它不只是“重排”，还会顺手清理重排产生的 view op。

### 6. `LoopInvariantCodeMotion`：把不变量提出循环

`triton-licm` 在 `make_ttgir` 里出现多次（`compiler.py:335`、`345`、`359`）。它的核心是 MLIR 的 `moveLoopInvariantCode`，但加了 Triton 特有的 load 处理（`LoopInvariantCodeMotion.cpp:48-95`）：

- 从最内层循环开始（`49-52`），这样内层 hoist 到外层后还能继续 hoist；
- 如果循环带 `llvm.loop.licm.disable`，跳过（`23-35`）；
- 非 load op：必须 `isSpeculatable` 且 `isMemoryEffectFree`（`63-64`）；
- load op：load 本身必须是只读效果，且整个循环必须是 memory-effect-free 或只读（`65-69`）；
- hoist load 时，给它加一个“循环至少执行一次”的 mask：`lb < ub`（`78-91`）。

**Before（guided）**：

```mlir
%p = tt.addptr %base, %c0 : !tt.ptr<f32>, i32
%r = scf.for %i = %lb to %ub step %c1 iter_args(%acc = %zero) -> f32 {
  %v = tt.load %p, %mask : !tt.ptr<f32>
  %s = arith.addf %acc, %v : f32
  scf.yield %s : f32
}
```

**After（guided）**：

```mlir
%cond = arith.cmpi slt, %lb, %ub : i32
%m2 = arith.andi %mask, %cond : i1   // 标量 mask 的情况
%v = tt.load %p, %m2 : !tt.ptr<f32>
%r = scf.for %i = %lb to %ub step %c1 iter_args(%acc = %zero) -> f32 {
  %s = arith.addf %acc, %v : f32
  scf.yield %s : f32
}
```

注意 `%m2` 的形态取决于 mask 是标量还是 tensor：`getPredMask` 会把 trip-count 条件和原 mask 做 `and`，tensor mask 还要 broadcast 条件。上面的标量形态只是示意。

为什么不能无条件 hoist load？如果循环体里有 store 到同一块内存，load 可能读到不同值；如果循环可能执行 0 次，hoist 后的 load 会多读一次。LICM 用“循环只读”和 trip-count mask 分别解决这两个问题。

### 7. `LoopAwareCSE`：合并等价的 iter arg

普通 CSE 不能跨循环迭代合并值，因为 `%a` 和 `%b` 在每轮可能是不同的 SSA 值。`LoopAwareCSE` 用递归等价性证明“两个 iter arg 每轮都相等”（`LoopAwareCSE.cpp:27-41`）：

- 初始值必须相同；
- 递归假设它们相等（`argStack` 断环）；
- 再检查 yield 的值是否等价。

`areEqualInLoop`（`43-87`）的检查：

- 类型相同；
- 都在同一个 loop body；
- 都是 block argument 或都不是；
- 都不是 induction variable；
- block argument 比较对应的 iter arg；
- op result 比较 result number，且两个 op 都 memory-effect-free、没有 region；
- 最后用 `OperationEquivalence::isEquivalentTo` 递归比较 operands。

**Before（guided）**：

```mlir
%r:2 = scf.for %i = %c0 to %n step %c1 iter_args(%a = %init, %b = %init) -> (f32, f32) {
  %x = arith.addf %a, %c : f32
  %y = arith.addf %b, %c : f32
  scf.yield %x, %y : f32, f32
}
```

**After（guided）**：

```mlir
%r = scf.for %i = %c0 to %n step %c1 iter_args(%a = %init) -> f32 {
  %x = arith.addf %a, %c : f32
  scf.yield %x : f32
}
```

`loopCSE` 用 `EquivalenceClasses` 把等价的 iter arg 分组，保留一个 leader，替换其余 uses，并把 yield 改成短路值（`90-125`）。pass 的顺序是：先普通 CSE，再 `loopCSE`，再普通 CSE，最后 `scf.for` canonicalizer（`132-153`）。普通 CSE 负责“循环外的重复”，loopCSE 负责“循环携带的重复”，最后 canonicalizer 清掉短路产生的死值。

### 8. `LoopUnroll`：只对带属性的循环生效

`LoopUnroll.cpp` 非常克制（`24-57`）：

- 读取 `tt.loop_unroll_factor`，没有就返回 1（`24-31`）；
- 只收集 factor > 1 的 `scf.for`（`40-44`）；
- 删除属性，调用 `loopUnrollByFactor`（`49-51`）；
- 如果产生 epilogue loop，给它设 `tt.num_stages = 1`，避免 epilogue 被 pipelining（`52-57`）。

这意味着：**Triton 不会自动 unroll 所有循环**。`tl.range(..., loop_unroll_factor=N)` 会在 codegen 里设置 `tt.loop_unroll_factor`（`python/triton/compiler/code_generator.py:1371-1372`）。这也是为什么普通 kernel 的 TTIR 里很少看到 unroll——除非用户显式传了这个参数。

### 9. `RewriteTensorDescriptorToPointer`：descriptor 的 dialect conversion

它用 `applyPartialConversion`（`RewriteTensorDescriptorToPointer.cpp:631-633`），把 `tt.tensordesc` 拆成 `pointer + rank 个 shape i64 + rank 个 stride i64 + padding i1 + roundF32ToTF32 i1`（`582-594`），pattern 覆盖 make/load/store/gather/scatter/reduce（`622-625`）。用 conversion pattern 是因为 descriptor 可能是函数参数、if yield、循环 iter arg，普通 pattern 很难跟踪这些边界（`551-555`）。NVIDIA 后端只在 `capability // 10 < 9` 时运行它（`compiler.py:295-296`），所以 T4 上 descriptor 在 TTIR 就消失，sm_90+ 才保留给 TMA。

## 进一步用法/重要变体

### 用 `triton-opt` 单跑一个 pass

```bash
triton-opt input.ttir --triton-combine -o out.ttir   # guided
triton-opt input.ttir --triton-reorder-broadcast -o out.ttir
triton-opt input.ttir --triton-licm -o out.ttir
triton-opt input.ttir --triton-loop-aware-cse -o out.ttir
```

加 `--mlir-print-ir-after-all` 可以看每个 pass 前后的 IR；加 `-verify-each` 可以在每个 pass 后验证。

### 写一个自定义 pattern

最小步骤：

1. 继承 `OpRewritePattern<RootOp>` 或 `RewritePattern`；
2. 实现 `matchAndRewrite`，先检查、后改；
3. 在 pass 的 `runOnOperation` 里 `patterns.add<MyPattern>(context)`；
4. `applyPatternsGreedily(getOperation(), std::move(patterns))`；
5. 用 lit 测试锁行为。

如果 pattern 会创建新 dialect 的 op，记得在 `Passes.td` 的 `dependentDialects` 里加对应 dialect（Ch 16）。

### `applyPatternsGreedily` vs `applyOpPatternsAndFold`

- `applyPatternsGreedily`：对整个 op（通常是 module）反复应用，直到不动点；适合 pass 级重写。
- `applyOpPatternsAndFold`：对单个 op 应用 pattern 并折叠；适合局部清理。
- `GreedyRewriteConfig` 可以控制是否 fold、是否允许 region simplification、迭代上限。

### 变体：`RewritePattern` 的 generated ops

`RewritePattern` 构造函数的第四个参数是 generated op 列表。它主要用于：

- MLIR 的 pattern 调试/统计；
- 某些 driver 的依赖分析；
- 文档化“这个 pattern 会创建什么”。

它不是匹配条件，也不会自动帮你注册 dialect。写 `CombineSelectMaskedLoadPattern` 时列 `tt.load`，只是告诉框架“我可能创建 load”。

## 对比与取舍

| 优化 | 主要收益 | 主要风险 | 什么时候值得 |
|---|---|---|---|
| `addptr` 合并 | 减少地址计算、属性传播 | 32 位非常量偏移不能证明不溢出 | 常量/64 位偏移 |
| `select(load)` 折叠 | 少一次 select、少一份 mask | 依赖 dominance，可能提前求值 | mask 是 splat(cond) |
| `dot+add` 折叠 | MMA 直接累加 | 浮点近似累加语义 | 累加器为零、无 imprecise acc |
| broadcast 重排 | 小 shape 计算，省寄存器 | 可能改变 layout，增加转换 | 大 tile + 单 broadcast |
| LICM | 减少循环内重复 | 延长 live range、增加 mask | 循环只读、load 不变量 |
| loop-aware CSE | 减少 iter arg | 只合并可证明等价的值 | 初始化相同、yield 等价 |
| unroll | 减少循环开销、暴露 ILP | 代码膨胀、寄存器压力 | factor 明确且循环体小 |
| descriptor→pointer | 让非 TMA 后端可用 | 丢失 descriptor 语义 | sm_90 以下 |

### 为什么“优化”可能让 layout 更差

TTIR 优化只看值，不看 layout。但 TTGIR 的 layout 分配会受 IR 结构影响：

- `ReorderBroadcast` 把 elementwise 改到 `1x64` 上，再 broadcast。TTGIR 可能给 `1x64` 一个 slice layout，给 `128x64` 一个 blocked layout；边界处可能需要 `ttg.convert_layout`。如果原来 `128x64` 的 elementwise 正好和消费者同 layout，重排反而多一次转换。
- `CombineBroadcastMulReduce` 创建 `tt.dot`，dot 的 operand 需要 `#dot_op`，累加器需要 `#mma`。原来 elementwise 的 blocked layout 可能和 dot 不兼容，pass 会插入 `convert_layout`。大多数情况下 dot 的收益远大于转换，但小 M/N 时不一定。
- LICM 把 load 提出循环，值在循环外定义，live range 变长；如果它占用的寄存器/共享内存导致 occupancy 下降，性能可能回退。
- CSE 合并值后，layout propagation 的“锚点”可能改变，原来不需要转换的边界可能需要转换。CSE 本身不跨类型合并，但它改变了 use 关系。

验证方法：dump TTGIR，数 `ttg.convert_layout` 的数量和位置；对比优化前后的 `ttg.shared`、`num_warps`、register usage；不要只看 TTIR 变短了。

## 踩雷集锦

1. **在 `matchAndRewrite` 里先改 IR，再检查条件。**
   失败返回时 IR 已经脏了，greedy driver 可能无限重试或 verifier 报错。正确做法是把所有检查放在最前面，改 IR 只放在最后。

2. **用 `op->erase()` 而不是 `rewriter.eraseOp(op)`。**
   driver 的 worklist 还持有这个 op，裸 erase 会造成 use-after-free。所有删除都走 rewriter。

3. **`replaceAllUsesWith` 后忘了旧 op 还活着。**
   `CombineDotAddPattern` 用 `replaceAllUsesWith(addOp, dotOp.getResult())`（`Combine.cpp:259`），旧 add 变成 dead op，依赖后续 canonicalizer/DCE 清理。如果你在 pass 里也这么写，确认后面有 DCE；否则用 `replaceOp`。

4. **以为 `addptr(addptr)` 一定会合并。**
   32 位非常量偏移不会合并，因为 `isAddPtrOffsetCombinable` 不能证明不溢出（`Combine.cpp:48-62`）。看到没合并，先看偏移类型和常量性。

5. **以为 `tt.load` 会被 LICM 无条件提出循环。**
   循环里只要有 store（非只读），load 就不会 hoist（`LoopInvariantCodeMotion.cpp:65-69`）。循环可能 0 次时，hoist 的 load 还要加 trip-count mask（`78-91`）。

6. **以为 loop-aware CSE 会合并所有重复计算。**
   它只合并“初始化相同、yield 等价”的 iter arg（`LoopAwareCSE.cpp:27-41`）；普通重复计算由前面的普通 CSE 处理。它也不会合并有 region 或副作用 op（`79-81`）。

7. **以为 `triton-loop-unroll` 会自动展开。**
   没有 `tt.loop_unroll_factor` 就 factor=1，pass 直接跳过（`LoopUnroll.cpp:24-33`）。不要指望它像 LLVM 的 `-loop-unroll` 那样启发式展开。

8. **以为 reorder-broadcast 总能减少寄存器，或把 TTIR 优化当成 layout 优化。**
   它只处理“所有 operand 都是 broadcast/splat、且只有一个 broadcast 源 shape”的情况（`ReorderBroadcast.cpp:121-141`）；多个源 shape 会拒绝匹配。更重要的是 TTIR 没有 encoding，combine/reorder 只保证值等价，可能让 TTGIR 的 layout 更好也可能更差，必须 dump TTGIR 验证。

## 进阶

- 读 `TritonCombine.inc` 的生成结果（构建后位于 build 目录），对照 `Combine.td` 看 TableGen 到底生成了什么 `matchAndRewrite`。
- 读 `GreedyPatternRewriteDriver` 的实现，理解 worklist、fold、dead op elimination 和迭代上限。
- 读 `OperationEquivalence::isEquivalentTo` 的文档，理解 `LoopAwareCSE` 如何做结构化比较；自己写一个“两个 op 是否等价”的小测试。
- 给 `CombineOpsPass` 加一个统计计数器，记录每个 pattern 的匹配次数；用真实 kernel 看哪个 pattern 最常触发。

## 动手练习

1. **addptr 合并**：写一个 TTIR，包含 `addptr(addptr(%p, %c0), %c1)`，跑 `--triton-combine`，确认变成 `addptr(%p, addi(%c0, %c1))`。再换成两个非常量 32 位偏移，确认不合并。**guided**。
2. **select(load) 折叠**：写 `select(%cond, load(%ptr, splat(%cond), %other), %other2)`，跑 combine，确认 load 的 `other` 变成 `%other2`，select 消失。**guided**。
3. **broadcast 重排**：写 `broadcast(1x64 -> 128x64) + splat`，跑 `--triton-reorder-broadcast`，确认 elementwise 在 `1x64` 上，结果再 broadcast。**guided**。
4. **LICM**：写一个循环，循环体只读、load 的指针不变，跑 `--triton-licm`，确认 load 被提出并加上 `lb < ub` 的 mask。**guided**。
5. **loop-aware CSE**：写两个 init 相同、yield 等价的 iter arg，跑 `--triton-loop-aware-cse`，确认合并成一个。**guided**。
6. **TableGen pattern**：在 `Combine.td` 加一个 `addptr(ptr, 0) -> ptr` 的 pattern（需要 `Pat` 和 constraint），重新生成并跑 lit。**guided**（需要构建）。
7. **layout 对比**：对同一个 kernel，分别开/关 `triton-reorder-broadcast`，dump TTGIR，数 `ttg.convert_layout` 和 `ttg.shared`，解释差异。**guided**。

## 本章重点

- `matchAndRewrite` 是事务：失败不改 IR；修改只用 `PatternRewriter`；删除用 `eraseOp`，替换用 `replaceOp`。
- benefit 决定尝试顺序，不决定正确性；同 benefit 的顺序不保证。
- canonicalizer 做“op 自己简化自己”，专用 pass 做跨 op/循环的代数与调度变换。
- `CombineAddPtr` 只在能证明偏移不溢出时合并；32 位非常量偏移通常不合并。
- `CombineSelectMaskedLoad` 把 `select(cond, load(ptrs, splat(cond), other), other2)` 折成 `load(..., other2)`，依赖 dominance。
- `CombineDotAdd` 把 `dot(a,b,0)+c` 折成 `dot(a,b,c)`，浮点还要检查 `maxNumImpreciseAcc`。
- `ReorderBroadcast` 先在小 shape 上算，再 broadcast；限制是所有 operand 必须是 broadcast/splat，且源 shape 一致。
- LICM 只 hoist 只读 load 到只读循环，并加 trip-count mask；loop-aware CSE 只合并可证明等价的 iter arg。
- 优化可能改变 TTGIR 的 layout，必须 dump TTGIR 验证，不能只看 TTIR 变短。

## 自我检核

1. `matchAndRewrite` 返回 `failure()` 时，IR 必须满足什么条件？为什么不能先 `eraseOp` 再检查？
2. `PatternBenefit` 高的 pattern 一定先成功吗？同 benefit 的顺序为什么不能依赖？
3. 什么时候应该用 canonicalizer，什么时候应该写专用 pass？
4. `addptr(addptr(p, i0), i1)` 在什么情况下不会合并？为什么？
5. `select(cond, load(ptrs, splat(cond), other), other2)` 折叠成 load 需要哪些条件？dominance 检查防止了什么？
6. `CombineBroadcastMulReduce` 为什么要求 M/N >= 16？它创建 `tt.dot` 后，layout 会发生什么变化？
7. LICM 为什么给 hoist 的 load 加 `lb < ub` 的 mask？如果循环可能执行 0 次，不加会怎样？
8. loop-aware CSE 和普通 CSE 的分工是什么？为什么它不能替代普通 CSE？
9. `triton-loop-unroll` 为什么默认不展开？`tt.loop_unroll_factor` 从哪里来？

## 延伸阅读

1. **`lib/Dialect/Triton/Transforms/Combine.cpp:24-293`**
   - **读哪里**：`isAddPtrOffsetCombinable`、`CombineSelectMaskedLoadPattern`、`CombineBroadcastMulReducePattern`、`CombineDotAddPattern`、`CombineOpsPass`。
   - **学什么**：一个 pass 如何组织多个 pattern，以及每个 pattern 的合法性条件。
   - **与本章关系**：这是 TTIR 代数优化的主样本；先能逐条解释每个 `return failure()` 的原因。

2. **`lib/Dialect/Triton/Transforms/Combine.td:11-21`**
   - **读哪里**：`DefOverflow`、`CopyDiscardableAttrs`、`CombineAddPtrPattern`。
   - **学什么**：TableGen `Pat` 的匹配/生成/约束/额外动作写法。
   - **与本章关系**：对比手写 `RewritePattern` 和声明式 pattern 的差异。

3. **`lib/Dialect/Triton/Transforms/LoopInvariantCodeMotion.cpp:23-94`**
   - **读哪里**：`isLICMDisabled`、`isMemoryEffectFreeOrOnlyRead`、load hoist 的 mask 构造。
   - **学什么**：LICM 的合法性条件，尤其是“只读循环”和 trip-count mask。
   - **与本章关系**：把“循环不变量”从概念落到可验证的 IR 变换。

4. **`lib/Dialect/Triton/Transforms/LoopAwareCSE.cpp:27-153`**
   - **读哪里**：`areIterArgsEqual`、`areEqualInLoop`、`loopCSE`、pass 的三段式。
   - **学什么**：如何用递归等价性证明合并 iter arg，以及 `OperationEquivalence` 的用法。
   - **与本章关系**：这是“跨迭代 CSE”的完整案例，也是 analysis 复用的好例子。

## 下一章

→ [Ch 18 TTGIR 优化](./18-ttgir-optimizations.md)：本章的优化都在没有 layout 的 TTIR 上；下一章进入 TTGIR，看 `coalesce`、`remove-layout-conversions`、`optimize-dot-operands` 如何在这个 encoding 空间里继续优化，以及它们为什么会和本章的“值优化”产生冲突。
