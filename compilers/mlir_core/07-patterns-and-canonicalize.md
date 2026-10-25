# Ch 7 RewritePattern 与 canonicalize

> **目标**：把 `canonicalize` 从“一个会做优化的黑盒”拆成四个可解释的部件：pattern 怎么匹配、fold hook 什么时候顶替 pattern、greedy driver 什么时候停、以及为什么它删得掉死常量却删不掉死 store。学完本章，你能写出不会死循环、也不会误删副作用的 canonicalization pattern。

## 为什么需要这个？

`canonicalize` 是最容易被误解的 pass。它看起来什么都会：常量折叠、`x+0` 化简、死代码清理、控制流简化。于是很多人把它当成“万能优化器”，写完 pattern 就丢进去，期望它自动收敛。

真实情况是：`canonicalize` 只是一个**执行器**。它自己不认识 `arith.addi`，也不认识你的方言。它做三件事：

1. 收集所有已加载 dialect 注册的 canonicalization pattern；
2. 在 worklist 上反复“折叠 + 匹配 + 重写 + 简化区域”；
3. 直到某一轮没有任何变化，或者撞到迭代上限。

所以“canonicalize 能不能优化我的 op”取决于**你注册了什么**，而不是 pass 本身有多聪明。理解这一点，才能解释三种常见困惑：

- 为什么我写了 `fold`，`canonicalize` 却没反应？（可能没被收集，或者 op 不满足 fold 的前置条件）
- 为什么两个重复的 op，`canonicalize` 不合并，`cse` 却合并？（分工不同）
- 为什么我的 pattern 跑两次就停了，IR 还没到不动点？（撞到 `max-iterations`）

## 先建立直觉：greedy driver 不是“扫一遍”

把 driver 想成一个**带四个动作的 worklist 循环**。每一轮（iteration）：

```text
1. 删除不可达块
2. 从 worklist 取 op：
     a. 如果 trivially dead → erase
     b. 否则先试 fold hook
     c. fold 不成功 → 试 pattern
3. worklist 空了 → 简化区域（删死块参数、合并块）
4. 可选：跑一次 CSE
5. 以上任何一步改了 IR → 再来一轮
```

“收敛”的定义很严格：**一整轮下来 `continueRewrites == false`**（`mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp` 第 890–924 行）。不是“worklist 空了”就收敛——区域简化和 CSE 还可能改 IR。

这也解释了一个反直觉现象：很多简单的折叠在**第一轮**就全做完了，因为 worklist 会在重写后把受影响的 op 重新加回去。需要第二轮的情况，通常是“区域简化 / CSE 在 worklist 排空之后才制造了新机会”。

## 核心概念一：RewritePattern、RewritePatternSet、PatternRewriter

三个类型分工明确：

| 类型 | 角色 | 关键 API |
|---|---|---|
| `RewritePattern` | 一条“匹配 → 改写”规则 | `matchAndRewrite(op, rewriter)` |
| `RewritePatternSet` | 规则集合，按 op 名索引 | `add<T>(...)`、`freeze()` |
| `PatternRewriter` | 唯一允许改 IR 的句柄 | `replaceOp`、`eraseOp`、`modifyOpInPlace` |

（`mlir/include/mlir/IR/PatternMatch.h` 第 238、822、799 行。）

`RewritePattern` 的契约只有一句话，但必须严格遵守（同文件第 246–249 行）：

```cpp
/// Note: Implementations must modify the IR if and only if the function
/// returns "success".
virtual LogicalResult matchAndRewrite(Operation *op,
                                      PatternRewriter &rewriter) const = 0;
```

“返回 success 就必须改了 IR，返回 failure 就必须一点没改”。违反这条，greedy driver 的收敛判断就失效：它靠“这一轮有没有 changed”决定要不要再来一轮，你谎报成功会让它空转，谎报失败会让它提前停。

`PatternRewriter` 不是可选的便利工具，而是**通知机制**。你调用 `replaceOp` / `eraseOp` / `modifyOpInPlace`（第 518、543、644 行），rewriter 会通知 listener，listener 再把受影响的 op 重新放进 worklist、更新 use-list。直接 `op->erase()` 会绕过这一切，留下悬垂 use。Ch 9 会专门讲安全修改 IR 的规则。

### benefit 决定“先试哪条”

`PatternBenefit`（第 34 行）是一个 `unsigned short`，越大越优先。`PatternApplicator::matchAndRewrite` 每次从“op 专属 pattern / 任意 op pattern / PDL pattern”三路里挑 benefit 最高的试（`mlir/lib/Rewrite/PatternApplicator.cpp` 第 156–180 行）。

关键点：**没有回溯**。第一条成功的 pattern 直接生效，后面的不再尝试（第 242–244 行 `if (matched) break;`）。所以：

- 想让“更具体的规则”优先，就把 benefit 调高；
- 两条规则都能匹配同一 op 时，结果取决于 benefit，而不是注册顺序；
- pattern 内部不要假设“我会和其他 pattern 一起作用”。

## 核心概念二：fold hook 与 canonicalization pattern 是两条路

这是本章最重要的区分。`canonicalize` 的每一次 worklist 处理，先试 fold，再试 pattern（`GreedyPatternRewriteDriver.cpp` 第 494–500 行、第 620–629 行）：

```cpp
// Try to fold this op. Do not fold constant ops...
if (config.isFoldingEnabled() && !op->hasTrait<OpTrait::ConstantLike>()) {
  SmallVector<OpFoldResult> foldResults;
  if (succeeded(op->fold(foldResults))) {
```

两者的差别：

| 维度 | fold hook | canonicalization pattern |
|---|---|---|
| 写在哪 | op 的 `fold()` 方法 | `getCanonicalizationPatterns()` |
| 返回什么 | `OpFoldResult`（值/属性） | `LogicalResult`（成功/失败） |
| 能不能新建 op | 通常不建，只复用已有值 | 可以，通过 rewriter |
| 计数 | 不算进 `max-num-rewrites` | 算（第 629 行 `++numRewrites`） |
| 典型例子 | `x + 0 → x` | `(x + 1) + 2 → x + 3` |

`arith.addi` 的两个行为正好各占一边：

```cpp
// mlir/lib/Dialect/Arith/IR/ArithOps.cpp 第 419–423 行
OpFoldResult arith::AddIOp::fold(FoldAdaptor adaptor) {
  // addi(x, 0) -> x
  if (matchPattern(adaptor.getRhs(), m_Zero()))
    return getLhs();
```

```cpp
// 同文件第 439–443 行
void arith::AddIOp::getCanonicalizationPatterns(RewritePatternSet &patterns,
                                                MLIRContext *context) {
  patterns.add<AddIAddConstant, AddISubConstantRHS, AddISubConstantLHS,
               AddIMulNegativeOneRhs, AddIMulNegativeOneLhs>(context);
}
```

所以 `x + 0` 是 **fold**，`(x + 1) + 2` 是 **pattern**（`AddIAddConstant`）。这也解释了一个常见困惑：为什么给 `canonicalize` 加 `max-num-rewrites=1` 仍能折掉一长串 `x+0`——因为 fold 不计数。

`canonicalize` 收集 pattern 的方式在 `mlir/lib/Transforms/Canonicalizer.cpp` 第 74–83 行：

```cpp
RewritePatternSet owningPatterns(context);
for (auto *dialect : context->getLoadedDialects())
  if (isAllowed(dialect))
    dialect->getCanonicalizationPatterns(owningPatterns);
for (RegisteredOperationName op : context->getRegisteredOperations())
  if (isAllowed(&op.getDialect()))
    op.getCanonicalizationPatterns(owningPatterns, context);
```

注意 `getLoadedDialects()`：**没加载的 dialect，它的 pattern 不会被收集**。这是“我写了 canonicalization 却没生效”的第一大原因（Ch 16 会讲 dialect 加载）。

## 核心概念三：GreedyRewriteConfig 与收敛条件

配置项在 `mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h` 第 43 行起，默认值在第 166–170 行：

```cpp
bool useTopDownTraversal = false;
GreedySimplifyRegionLevel regionSimplificationLevel =
    GreedySimplifyRegionLevel::Aggressive;
int64_t maxIterations = 10;
int64_t maxNumRewrites = kNoLimit;
```

但 **pass 的默认值不一定等于 config 的默认值**。`canonicalize` 的 pass 选项把 `region-simplify` 默认成 `normal`，把 `top-down` 默认成 `true`（`mlir/include/mlir/Transforms/Passes.td` 第 19–57 行；`--dump-pass-pipeline` 能看到实际值）。这是两层默认值叠加的典型坑。

driver 的主循环（`GreedyPatternRewriteDriver.cpp` 第 842–930 行）可以简化为：

```cpp
do {
  // ++iteration > maxIterations 就 break
  worklist.clear();
  // 按 top-down / bottom-up 播种 worklist
  continueRewrites  = eraseUnreachableBlocks(...);
  continueRewrites |= processWorklist();       // fold + pattern
  continueRewrites |= simplifyRegions(...);    // 区域简化
  if (cseBetweenIterations) continueRewrites |= eliminateCommonSubexpressions(...);
} while (continueRewrites);

return success(!continueRewrites);   // 没变化才算收敛
```

四个结论：

1. **`max-iterations` 限制的是“轮数”**，不是“处理的 op 数”。一轮内部可以处理很多 op。
2. **`max-num-rewrites` 只数 pattern 成功次数**，fold 不数（第 465–466 行的循环条件 + 第 629 行的 `++numRewrites`）。
3. **不收敛不是失败**。`Canonicalizer.cpp` 第 89 行注释写得很清楚：“Canonicalization is best-effort. Non-convergence is not a pass failure.” 只有 `test-convergence=true` 时才会 `signalPassFailure()`。
4. **`cse-between-iterations` 会改变收敛行为**：CSE 在 worklist 排空后制造新机会，需要额外一轮；这也是它默认关闭的原因之一。

## 核心概念四：为什么 canonicalize 不是死代码消除器

先看它删什么。worklist 里每个 op 先过一道 `isOpTriviallyDead`（`GreedyPatternRewriteDriver.cpp` 第 485–492 行）：

```cpp
if (isOpTriviallyDead(op)) {
  rewriter.eraseOp(op);
  changed = true;
  continue;
}
```

`isOpTriviallyDead` 的定义是 `use_empty() && wouldOpBeTriviallyDead(op)`（`mlir/lib/Interfaces/SideEffectInterfaces.cpp` 第 36 行）。也就是说，必须同时满足：

- **没有 use**；
- **没有不可忽略的副作用**。

`wouldOpBeTriviallyDeadImpl` 的判定逻辑（同文件第 43–95 行）很讲究：

- 有 `IsTerminator` trait 或 `SymbolOpInterface` → 不删；
- 实现了 `MemoryEffectOpInterface`：只有“读”和“自己分配且未被使用的结果”可以忽略；
- 有 `Write`、`Free`、或未知 effect → 不删；
- 没有任何内存效应接口、也没有 `HasRecursiveMemoryEffects` → 视为未知，不删。

这就是为什么：

- 未使用的 `arith.constant`、`arith.addi` 会被删（纯计算）；
- 未使用的 `memref.alloc` 也可能被删（分配结果没人用，分配本身不可观测）；
- 未使用的 `memref.store`、`func.call` **不会**被删（写内存 / 未知副作用）。

更重要的是：**canonicalize 没有活跃性分析**。它不会问“这个值在后面还会不会被用”，只做局部的“无 use + 无副作用”。真正的 DCE 需要 CFG 活跃性、调用图、内存模型，那是另一类 pass 的工作。canonicalize 会顺带删除不可达块（第 892–899 行），但这仍然不是“全局死代码消除”。

一句话：**canonicalize 的删除是重写过程的副产品，不是它的目标**。

## 底层机制：PatternApplicator 的匹配循环

`PatternApplicator::matchAndRewrite`（`mlir/lib/Rewrite/PatternApplicator.cpp` 第 132–249 行）做四件事：

1. 先跑 PDL bytecode 匹配（如果有），拿到候选；
2. 把 op 专属 pattern、任意 op pattern、PDL 候选按 benefit 归并；
3. 依次尝试，`canApply` 过滤掉被禁用的（`--disable-patterns` 等）；
4. 第一个成功者生效，调用 `onSuccess`；全部失败调用 `onFailure` 并返回 failure。

两个实现细节值得记住：

- **`rewriter.setInsertionPoint(op)` 在调用 pattern 之前**（第 199 行）。所以 pattern 里新建的 op 默认插在原 op 前面；要插到别处，自己设 insertion point。
- **失败必须无副作用**。driver 在 failure 时不会回滚；pattern 里改了 IR 却返回 failure，会直接破坏收敛判断（`MLIR_ENABLE_EXPENSIVE_PATTERN_API_CHECKS` 构建会抓这个问题，本机 Optimized build 不抓）。

## 示例一：三条 fold，一次跑完

```mlir
func.func @fold(%x: i32) -> i32 {
  %c6 = arith.constant 6 : i32
  %c7 = arith.constant 7 : i32
  %m = arith.muli %c6, %c7 : i32
  return %m : i32
}
func.func @addzero(%x: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %0 = arith.addi %x, %c0 : i32
  return %0 : i32
}
func.func @mulone(%x: i32) -> i32 {
  %c1 = arith.constant 1 : i32
  %0 = arith.muli %x, %c1 : i32
  return %0 : i32
}
```

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" canon.mlir --canonicalize
```

实测：

```mlir
module {
  func.func @fold(%arg0: i32) -> i32 {
    %c42_i32 = arith.constant 42 : i32
    return %c42_i32 : i32
  }
  func.func @addzero(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
  func.func @mulone(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}
```

三条都是 fold hook：

- `6 * 7` → `42`（常量折叠，`MulIOp::fold` 的默认分支）；
- `x + 0` → `x`（`AddIOp::fold` 第 420–422 行）；
- `x * 1` → `x`（`MulIOp::fold` 第 633–635 行）。

## 示例二：pattern、fold、CSE 三方对照

### 2a. `(x + 1) + 2` 是 pattern

```mlir
func.func @chain2(%x: i32) -> i32 {
  %c1 = arith.constant 1 : i32
  %c2 = arith.constant 2 : i32
  %0 = arith.addi %x, %c1 : i32
  %1 = arith.addi %0, %c2 : i32
  return %1 : i32
}
```

`--canonicalize` 实测（省略 `module` 外壳，下同）：

```mlir
func.func @chain2(%arg0: i32) -> i32 {
  %c3_i32 = arith.constant 3 : i32
  %0 = arith.addi %arg0, %c3_i32 : i32
  return %0 : i32
}
```

`--cse` 实测：完全不变。CSE 只找“结构相同的重复表达式”，这里没有重复，只有可结合的常量。

### 2b. 重复表达式：canonicalize 不管，CSE 管

```mlir
func.func @dup(%a: i32, %b: i32) -> i32 {
  %0 = arith.addi %a, %b : i32
  %1 = arith.addi %a, %b : i32
  %2 = arith.addi %0, %1 : i32
  return %2 : i32
}
```

`--canonicalize` 实测（`@dead` 与本例无关，省略）：三条 `addi` 原样保留。

`--cse` 实测：

```mlir
func.func @dup(%arg0: i32, %arg1: i32) -> i32 {
  %0 = arith.addi %arg0, %arg1 : i32
  %1 = arith.addi %0, %0 : i32
  return %1 : i32
}
```

这是两条完全不同的路径：

- canonicalize 走 **fold/pattern**：它不会主动找“另一个 op 算过同样的东西”；
- CSE 走 **支配树 + 表达式哈希**：它需要 `DominanceInfo`（Ch 6），把“已算过的表达式”映射到值。

想让“重复表达式 + 代数化简”都发生，正确做法是 `canonicalize,cse,canonicalize`，而不是指望一个 pass 全包。

## 示例三：max-iterations 边界

这是本章最值得亲手跑的实验。输入：

```mlir
func.func @dup_subs(%x: i32, %y: i32) -> i32 {
  %a = arith.subi %x, %y : i32
  %b = arith.subi %x, %y : i32
  %c = arith.subi %a, %b : i32
  return %c : i32
}
```

三次运行：

```powershell
& "$mlir\mlir-opt.exe" cseiter.mlir --canonicalize
& "$mlir\mlir-opt.exe" cseiter.mlir --canonicalize="cse-between-iterations=true"
& "$mlir\mlir-opt.exe" cseiter.mlir --canonicalize="cse-between-iterations=true max-iterations=1"
```

实测输出（依次；`//` 行是我加的标注）：

```mlir
// 1) 普通 canonicalize：三条 subi 原样保留
func.func @dup_subs(%arg0: i32, %arg1: i32) -> i32 {
  %0 = arith.subi %arg0, %arg1 : i32
  %1 = arith.subi %arg0, %arg1 : i32
  %2 = arith.subi %0, %1 : i32
  return %2 : i32
}
```

```mlir
// 2) cse-between-iterations=true（默认 10 轮）：折成常量 0
func.func @dup_subs(%arg0: i32, %arg1: i32) -> i32 {
  %c0_i32 = arith.constant 0 : i32
  return %c0_i32 : i32
}
```

```mlir
// 3) 同上，但 max-iterations=1：CSE 合并了，下游折叠没来得及发生
func.func @dup_subs(%arg0: i32, %arg1: i32) -> i32 {
  %0 = arith.subi %arg0, %arg1 : i32
  %1 = arith.subi %0, %0 : i32
  return %1 : i32
}
```

第三种输出精确地展示了“一轮”的边界：第 1 轮里，worklist 处理完两条重复 `subi` 后没发现可折的（`%c = subi %a, %b` 的两个操作数不同）；随后 `cse-between-iterations` 把 `%b` 合并成 `%a`，IR 变了，`continueRewrites=true`；但 `max-iterations=1` 让循环直接退出，所以 `subi %0, %0 → 0` 没机会发生。

上游测试 `mlir/test/Transforms/canonicalize-cse-between-iterations.mlir` 第 1–25 行就是这个场景，注释写得很直白：“CSE unifies the duplicates but the follow-up fold cannot fire”。我们的实测与它一致。

## 示例四：canonicalize 不是 DCE

```mlir
func.func private @sink(i32)
func.func @side(%m: memref<4xi32>, %v: i32, %i: index) {
  memref.store %v, %m[%i] : memref<4xi32>
  %unused = arith.addi %v, %v : i32
  func.call @sink(%v) : (i32) -> ()
  return
}
```

`--canonicalize` 实测：

```mlir
module {
  func.func private @sink(i32)
  func.func @side(%arg0: memref<4xi32>, %arg1: i32, %arg2: index) {
    memref.store %arg1, %arg0[%arg2] : memref<4xi32>
    call @sink(%arg1) : (i32) -> ()
    return
  }
}
```

`%unused` 被删了（纯计算、无 use），`store` 和 `call` 都保留（写内存 / 未知副作用）。如果 canonicalize 真是 DCE，它会进一步问“这个 store 还有人读吗”“这个 call 的结果有没有人用”，但它不做这些——它只判断单个 op 是否 trivially dead。

## 对比与取舍

| 维度 | `canonicalize` | `cse` | 真正的 DCE |
|---|---|---|---|
| 主要目标 | 局部规范化 + 折叠 | 消除重复表达式 | 删除不可达/无用代码 |
| 依赖分析 | 无（局部） | `DominanceInfo` | 活跃性、调用图 |
| 跨块能力 | 有限（区域简化） | 强（支配范围内） | 强 |
| 副作用处理 | 只删 trivially dead | 只合并纯 op | 需要内存模型 |

实践建议：

- **规范化用 `canonicalize`，去重用 `cse`，清死代码用专门的 DCE**。三件事别混在一个 pass 里。
- **写完 pattern 先跑 `--canonicalize` 两次**，确认第二次没有变化（幂等）。有变化说明你的 pattern 可能需要两轮才稳定，考虑调 benefit 或合并规则。

## 踩雷集锦

1. **pattern 返回 success 却没改 IR。** driver 会认为“变了”，继续迭代，最后撞到 `max-iterations`。本机 Optimized build 不会 assert，只会静默多跑几轮。

2. **pattern 改了 IR 却返回 failure。** 更糟：driver 不回滚，收敛判断被破坏，后续 pattern 可能在半改状态上匹配。

3. **直接 `op->erase()` 而不是 `rewriter.eraseOp(op)`。** 绕过 listener，use-list 和 worklist 都不更新，典型症状是“删完 op 后下游还引用它”。

4. **以为 `max-num-rewrites` 也限制 fold。** 它只数 pattern 成功次数（第 629 行）；一长串 `x+0` 仍然会被折完。

6. **pattern 里假设自己会和别的 pattern 一起跑。** `PatternApplicator` 第一条成功就 break，没有回溯；组合逻辑要写在一个 pattern 里。

## 进阶：再往深一层

- **PDL pattern**：`RewritePatternSet` 可以附加 PDL bytecode，pattern 用 `.pdl` 描述、在运行时匹配。`PatternApplicator` 第 140–143 行先跑 bytecode，再跑原生 pattern。
- **`FrozenRewritePatternSet`**：`freeze()` 之后 pattern 集合不可变，可以被多个线程共享。canonicalize 在 `initialize()` 里 freeze 一次，之后每个函数复用（`Canonicalizer.cpp` 第 82–83 行）。

## 动手练习

**任务规格**：写一个能把 `(x * 2) + x` 化简成 `x * 3` 的 canonicalization pattern（C++，guided），并证明它在 `canonicalize` 下幂等；再用纯 IR 实验验证 `x+0`、`x*1`、重复表达式的三方差异。

**测试用例（至少三个）**：

1. `(x * 2) + x` → `x * 3`（你的 pattern）。
2. `x + 0`、`x * 1`（fold hook，不需要你的 pattern）。
3. 两条相同的 `arith.addi`（canonicalize 不动，cse 合并）。

**分段步骤**：

1. 先跑本章示例一、二，确认输出与教材一致。
2. 把 `--canonicalize` 连跑两次，比较输出是否相同（幂等性）。
3. 用 `--canonicalize="max-iterations=1"` 跑示例三，解释为什么停在半路。
4. （guided）在 `getCanonicalizationPatterns` 里注册你的 pattern，benefit 设为 2，确认它优先于 `AddIMulNegativeOne`。
5. 用 `--mlir-print-ir-after-all` 看 pattern 生效前后的 IR。

**参考解答**：

<details>
<summary>展开看关键结论</summary>

- `(x * 2) + x` 可以匹配 `AddIOp`，检查一侧是 `MulIOp` 且乘数为常量 2，然后用 `rewriter.replaceOpWithNewOp<arith::MulIOp>(op, x, c3)`。
- 必须用 rewriter 建新 op，不能手写 `OpBuilder` 后直接 replace；否则 listener 收不到通知。
- 幂等性检查：第二次 `--canonicalize` 输出应与第一次完全相同。
- 示例三的 `max-iterations=1` 输出会停在 `subi %0, %0`，因为下游 fold 需要第二轮。

</details>

**另外三题**：

2. 把 `chain2.mlir` 的第二个常量改成 3，确认 pattern 仍然合并成 `x + 5`。
3. 用 `--canonicalize="max-num-rewrites=1"` 跑 `chain2.mlir`，确认结果不变，并解释原因。
4. 给 `dup.mlir` 加一个未使用的 `memref.load`，确认 canonicalize 保留它（读效应）。

**常见卡点**：

- 忘了在 pattern 里检查常量类型，`x * 2` 的 `2` 可能是 `index` 而不是 `i32`。
- 用 `getDefiningOp<arith::MulIOp>()` 时没判空。
- 返回 success 前没有真正 replace，导致 driver 空转。

**自我检核**：你的 pattern 在 `x * 1` 输入上会不会和 fold hook 冲突？谁先执行？

## 本章重点整理

- `canonicalize` 是执行器，不是优化器；能做多少取决于注册了哪些 fold/pattern。
- fold hook 与 canonicalization pattern 是两条路：前者返回值/属性，后者用 rewriter 改 IR。
- `PatternApplicator` 按 benefit 选第一条成功的 pattern，没有回溯。
- 收敛 = 一整轮 `continueRewrites == false`；`max-iterations` 限制轮数，`max-num-rewrites` 只数 pattern。
- `max-iterations=1` 能精确暴露“需要第二轮”的 pattern。
- canonicalize 只删 trivially dead，不做全局 DCE；`store`/`call` 不会被删。
- 重复表达式归 CSE 管，代数化简归 canonicalize 管。

## 自我检核

- [ ] 我能说出 `RewritePattern` 的成功/失败契约
- [ ] 我能区分 fold hook 和 canonicalization pattern
- [ ] 我能读懂 `max-iterations=1` 的半成品输出
- [ ] 我能解释 canonicalize 为什么不删未使用的 `store`

## 延伸阅读

- **[MLIR 文档：Canonicalization](https://mlir.llvm.org/docs/Canonicalization/)**
  - **读哪里**：pattern 定义、fold 与 canonicalization 的区别、greedy driver 的收敛。
  - **学什么**：官方对“什么该写成 fold、什么该写成 pattern”的建议。
  - **和本章的关联**：本章的核心概念一、二都以这篇为基准。

- **[mlir/lib/Transforms/Canonicalizer.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Transforms/Canonicalizer.cpp)**
  - **读哪里**：第 54–92 行的 `initialize` / `runOnOperation`。
  - **学什么**：pattern 如何被收集、config 如何从 pass 选项同步。
  - **和本章的关联**：示例一、三的入口。

- **[mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp)**
  - **读哪里**：第 443–520 行的 worklist 处理，第 842–930 行的迭代循环。
  - **学什么**：fold/pattern 的调用顺序、收敛判定、迭代上限。
  - **和本章的关联**：核心概念三、四的逐行出处。

- **[mlir/test/Transforms/canonicalize-cse-between-iterations.mlir](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/test/Transforms/canonicalize-cse-between-iterations.mlir)**
  - **读哪里**：前 30 行的 RUN 与 CHECK。
  - **学什么**：上游如何用 `max-iterations=1` 锁定“需要两轮”的行为。
  - **和本章的关联**：示例三的直接来源。

---

下一章换一个扩展点：如果你的 op 需要参与 canonicalize / bufferize / conversion，但不方便改 op 本身，怎么办？答案是 Dialect Interface 与 external model。

→ [Ch 8 Dialect Interface 与 external model](./08-dialect-interfaces.md)