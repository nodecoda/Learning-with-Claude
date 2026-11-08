# Ch 10 — RewritePattern 与 canonicalize

> **目标**：把 `canonicalize` 读成一个**固定点循环**，而不是“一堆简化规则”：知道 fold 与 pattern 谁先跑、pattern 之间按什么排序、`max-iterations` 打满时发生了什么、为什么 `markAllAnalysesPreserved` 只能放在“确实没改”的分支里。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，assertions ON（`-debug-only` 可用）。
>
> **前置**：Ch 08（`PatternRewriter` 与安全改 IR）、Ch 09（pass manager 与 analysis 失效）。

---

## 10.1 为什么维护者要读 greedy driver

MLIR 里最容易被“顺手改一下”的代码就是 canonicalization pattern。它们的 bug 形态高度集中：

1. **无限重写**。两个 pattern 互相把对方的结果改回去，`canonicalize` 跑满 `max-iterations` 就停，IR 停在中间态，测试里的 `CHECK` 时好时坏。
2. **fold 与 pattern 打架**。有人把本该写在 `fold()` 里的逻辑写成 pattern，结果“有时生效有时不生效”——因为 fold 先跑，op 已经被折掉了，pattern 根本没机会。
3. **root 被 erase 之后继续用**。pattern 里 `rewriter.eraseOp(op)` 之后又访问 `op->getOperand(0)`，assertions 打开时直接崩。
4. **benefit 误用**。以为 benefit 高就“优先匹配”，于是把 benefit 设成 10 想抢在别人前面；实际语义是“同一个 op 上按 benefit 降序试”，而且 benefit 为 0 意味着**永不匹配**（`impossibleToMatch`）。
5. **listener 缺失**。自己拿 `IRRewriter` 改 IR 却没挂 driver 的 listener，worklist 不知道新 op 存在，于是“改了一半”。

所以这一章的主线是：**driver 的循环结构 → fold 的优先级 → pattern 的排序 → 收敛判定**。维护者改 `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp` 的时机，基本都在这个循环里加“跳过”条件（不可达块、死 op、常量 CSE）。

## 10.2 先建立直觉：一次 `canonicalize` 的内部循环

```text
Canonicalizer::runOnOperation()                     [Canonicalizer.cpp:86]
        │  patterns = 所有已加载方言 + 所有已注册 op 的 canonicalization patterns
        ▼
applyPatternsGreedily(region, patterns, config)     [GreedyPatternRewriteDriver.cpp:1327]
        │
        ▼
  RegionPatternRewriteDriver::simplify()            [:1228]   ← 外层迭代
        │
        │  iteration = 1..max-iterations（超过就 break，不报错）
        │
        ├─ ① 清空 worklist，按 bottom-up/top-down 填满整个 region
        ├─ ② eraseUnreachableBlocks()
        ├─ ③ processWorklist()                      [:818]     ← 内层循环
        │       │
        │       │  pop 一个 op
        │       ├─ 不可达?        → 跳过
        │       ├─ trivially dead? → eraseOp
        │       ├─ 可 fold?        → op->fold()  ← 优先于 pattern！
        │       │       └─ 成功 → materialize 常量 / replaceAllUsesWith
        │       └─ 否则 → PatternApplicator::matchAndRewrite  [:132]
        │                        └─ 按 benefit 降序试 pattern
        │                            ├─ 成功 → 新 op 入 worklist
        │                            └─ 全失败 → 记 "all patterns failed to match"
        ├─ ④ simplifyRegions()（可选，region-simplify=normal/aggressive）
        └─ ⑤ 可选 CSE between iterations
        │
        ▼
  changed 为真 → 下一轮；一整轮没有任何改动 → 收敛，退出
```

三个“顺序”决定行为，维护时必须同时看：

| 顺序 | 谁先 | 后果 |
|---|---|---|
| fold 先于 pattern | `op->fold()` 在 `processWorklist` 里先跑（`:883-885`） | 能被 fold 的 op 永远不会走到 pattern |
| pattern 按 benefit 降序 | `PatternApplicator::applyCostModel`（`:58`） | benefit 相同则保持注册顺序（`stable_sort`） |
| 外层迭代先于内层重扫 | 每轮重建 worklist（`:1239-1255`） | 只有“上一轮还有改动”才会再扫一遍 |

## 10.3 fold、pattern、canonicalization 三者的分工

MLIR 的“简化”有三条通道，不是一条：

| 通道 | 写在哪 | 谁调用 | 适合什么 |
|---|---|---|---|
| `fold()` | `Op::fold` 覆盖（ODS 里 `hasFolder = 1`） | greedy driver 的每个 op、`OperationFolder` | **无副作用、结果可完全由 operands 决定**的恒等式，如 `addi x, 0 → x` |
| `RewritePattern` | C++ 或 DRR/PDLL | `PatternApplicator` | 需要改结构、加/删 op、跨 op 重写 |
| canonicalization 注册 | `getCanonicalizationPatterns`（Dialect 或 op） | `Canonicalizer::initialize`（`Canonicalizer.cpp:54`） | 该 op 的“标准形” |

`Canonicalizer::initialize` 的收集方式是**全量**的：

```cpp
// Canonicalizer.cpp:74-81（节选）
    for (auto *dialect : context->getLoadedDialects())
      if (isAllowed(dialect))
        dialect->getCanonicalizationPatterns(owningPatterns);
    for (RegisteredOperationName op : context->getRegisteredOperations())
      if (isAllowed(&op.getDialect()))
        op.getCanonicalizationPatterns(owningPatterns, context);
```

维护含义：只要方言被加载，它的 canonicalization pattern 就全部进入同一个 `FrozenRewritePatternSet`。所以“我的 pattern 会不会和别人的冲突”这个问题，答案是**会**，而且冲突表现为“谁先匹配谁赢”，不是编译错误。`filter-dialects`（`Canonicalizer.cpp:47-51`）就是为此提供的隔离手段。

## 10.4 `processWorklist`：fold 优先，然后才是 pattern

核心片段（`GreedyPatternRewriteDriver.cpp:871-890` 附近）：

```cpp
    // If the operation is trivially dead - remove it.
    if (deadnessListener.isOpTriviallyDead(op)) {
      rewriter.eraseOp(op);
      changed = true;
      continue;
    }

    // Try to fold this op. Do not fold constant ops. ...
    if (config.isFoldingEnabled() && !op->hasTrait<OpTrait::ConstantLike>()) {
      SmallVector<OpFoldResult> foldResults;
      if (succeeded(op->fold(foldResults))) {
        ...
        if (foldResults.empty()) {
          // Folding modified the op without using the rewriter.
          deadnessListener.notifyOperationModified(op);
```

四个维护要点：

1. **常量 op 被显式排除**（`!op->hasTrait<OpTrait::ConstantLike>()`）。注释里写了原因：常量 fold 成 `Attribute` 后会立刻被重新 materialize 成常量，形成无限循环。
2. **`foldResults.empty()` 不等于“没折”**。它表示 `fold` 就地改了 op（in-place），所以只需要 `notifyOperationModified`，不需要替换。
3. **materialize 走方言钩子**。`op->getDialect()->materializeConstant(...)`，失败时会清理已经生成的替换 op（`:930-960`）。维护含义：自定义方言如果不实现 `materializeConstant`，fold 返回 `Attribute` 时会在这里失败。
4. **fold 不走 `PatternRewriter`**。所以 driver 必须手动补 listener 通知，这就是 `deadnessListener.notifyOperationModified` 存在的原因。

## 10.5 benefit 与 cost model：排序的真实语义

`PatternBenefit` 的极值是 `impossibleToMatch()`（`PatternMatch.h:43-44`）：

```cpp
  static PatternBenefit impossibleToMatch() { return PatternBenefit(); }
  bool isImpossibleToMatch() const { return *this == impossibleToMatch(); }
```

也就是说 **benefit 0 = 这个 pattern 被丢弃**，不是“最低优先级”。`applyCostModel` 里两次过滤：

```cpp
// PatternApplicator.cpp:71-80（节选）
    for (const RewritePattern *pattern : it.second) {
      if (pattern->getBenefit().isImpossibleToMatch())
        LLVM_DEBUG(logImpossibleToMatch(*pattern));
      else
        patterns[it.first].push_back(pattern);
    }
```

然后按 benefit 降序 `stable_sort`（`:102-125`）：

```cpp
  auto cmp = [&benefits](const Pattern *lhs, const Pattern *rhs) {
    return benefits[lhs] > benefits[rhs];
  };
```

维护含义：

- 同 benefit 时**注册顺序**决定优先级（`stable_sort`）。所以“我的 pattern 应该先于框架里已有的那个”这种需求，光调 benefit 不够，还要看收集顺序（方言遍历顺序）。
- benefit 只影响“同一 op 上试 pattern 的顺序”，不影响 fold；fold 永远先跑。
- `impossibleToMatch` 常用于“编译期开关关闭某个 pattern”，写成 `PatternBenefit(0)` 而不是条件注册。

### 10.5.1 `canonicalize` 的完整选项面（实测 help）

维护时先看这个 pass 到底暴露了哪些旋钮，再猜行为：

```bash
$T/bin/mlir-opt --help 2>&1 | grep -A13 -E '^\s+--canonicalize\s'
```

```text
      --canonicalize                                         -   Canonicalize operations
        --cse-between-iterations                             - Run full CSE between each pattern-application iteration. CSE-driven changes trigger extra iterations, so this may push the iteration count up to max-iterations and affect convergence under test-convergence.
        --disable-patterns=<string>                          - Labels of patterns that should be filtered out during application
        --enable-patterns=<string>                           - Labels of patterns that should be used during application, all other patterns are filtered out
        --filter-dialects=<string>                           - If non-empty, only collect canonicalization patterns from the dialects with the given namespaces. The listed dialects are force-loaded into the context as dependent dialects.
        --max-iterations=<long>                              - Max. iterations between applying patterns / simplifying regions
        --max-num-rewrites=<long>                            - Max. number of pattern rewrites within an iteration
        --region-simplify=<value>                            - Perform control flow optimizations to the region tree
    =disabled                                          -   Don't run any control-flow simplification.
    =normal                                            -   Perform simple control-flow simplifications (e.g. dead args elimination).
    =aggressive                                        -   Perform aggressive control-flow simplification (e.g. block merging).
        --test-convergence                                   - Test only: Fail pass on non-convergence to detect cyclic pattern
        --top-down                                           - Seed the worklist in general top-down order
```

四个必须记住的语义差：

- `max-iterations` 限制**外层轮次**；`max-num-rewrites` 限制**单轮内 pattern 成功次数**。两者互不替代。
- `max-num-rewrites` **不限制 fold**（见实验 8）。
- `disable-patterns`/`enable-patterns` 按 pattern 的 **label** 过滤（`Pattern::addDebugLabels`，`PatternMatch.h:146-153`），不是按类名。本树 `mlir/lib/Dialect/` 里 `addDebugLabels` 出现 **0** 次（`grep -rn addDebugLabels mlir/lib/Dialect/ | wc -l`），所以这两个选项对内置 dialect 的 canonicalization 基本无效，只对你自己打了 label 的 pattern 有效。
- `region-simplify` 默认 `normal`，`aggressive` 会合并 block，代价更高但能解锁更多 fold。

## 10.6 收敛、`max-iterations` 与 `test-convergence`

外层循环（`GreedyPatternRewriteDriver.cpp:1228-1315`）：

```cpp
  do {
    // Check if the iteration limit was reached.
    if (++iteration > config.getMaxIterations() &&
        config.getMaxIterations() != GreedyRewriteConfig::kNoLimit)
      break;
```

`break` 之后：

```cpp
  // Whether the rewrite converges, i.e. wasn't changed in the last iteration.
  return success(!continueRewrites);
```

所以“打满 `max-iterations` 时还有改动” → `applyPatternsGreedily` 返回 failure。但 `Canonicalizer` **默认不把它当错**：

```cpp
// Canonicalizer.cpp:86-92
  void runOnOperation() override {
    bool changed = false;
    LogicalResult converged =
        applyPatternsGreedily(getOperation(), *patterns, config, &changed);
    // Canonicalization is best-effort. Non-convergence is not a pass failure.
    if (testConvergence && failed(converged))
      signalPassFailure();
    if (!changed)
      markAllAnalysesPreserved();
  }
```

维护含义，三条一起看：

- **默认 `max-iterations=10`、`max-num-rewrites=-1`（无限制）**（`GreedyPatternRewriteDriver.h:169-170`）。10 轮还不收敛，通常说明 pattern 集合里有环。
- **非收敛默认静默**。只有显式 `test-convergence=true` 才 `signalPassFailure()`。所以“CI 里 canonicalize 不稳定”不会自己暴露，必须加这个选项。
- **`markAllAnalysesPreserved` 只在 `!changed` 时调**。这是 Ch 09 那条“preserveAll 跳过 verifier”的正确用法：没改 IR → 保留 analysis → 顺带跳过 verifier，都是安全的。

### 10.6.1 strictness：哪些 op 会进 worklist

`GreedyRewriteStrictness` 三个值（`GreedyPatternRewriteDriver.h:23-31`）决定 driver 给谁发“入场券”：

| 模式 | 处理范围 | 典型用途 |
|---|---|---|
| `AnyOp` | 所有 op（默认） | `canonicalize`、`cse` 这类全局 pass |
| `ExistingAndNewOps` | 预先存在的 + 新插入的 | 想看到自己 pattern 造出来的 op |
| `ExistingOps` | 只有预先存在的 | 只折叠输入，不让新 op 扩散（`mlir-reduce` 的 reduction 就用了这个，见 `ReductionTreePass.cpp:62-66`） |

维护含义：如果你写了一个“制造新 op 再折它”的 pattern，在 `ExistingOps` 下只会折一轮，看起来像“pattern 只生效一次”。这不是 bug，是 strictness 在起作用。

## 10.7 源码落点

| 位置 | 内容 | 维护相关性 |
|---|---|---|
| `mlir/lib/Transforms/Canonicalizer.cpp:86` | `runOnOperation` | 收敛处理与 preserve 决策 |
| `mlir/lib/Transforms/Canonicalizer.cpp:54` | `initialize` | 全量收集 pattern、`filter-dialects` |
| `mlir/lib/Transforms/Canonicalizer.cpp:88-89` | `if (!changed) markAllAnalysesPreserved()` | preserve 与 verifier 的耦合点 |
| `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:818` | `processWorklist` | 内层循环：dead → fold → pattern |
| `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:871` | `isOpTriviallyDead` 分支 | 先删死 op |
| `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:883-885` | `isFoldingEnabled()` + `op->fold(...)` | fold 优先于 pattern |
| `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:1228` | `RegionPatternRewriteDriver::simplify` | 外层迭代与 `max-iterations` |
| `mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:1327` | `applyPatternsGreedily(Region&, ...)` | 公共入口 |
| `mlir/lib/Rewrite/PatternApplicator.cpp:58` | `applyCostModel` | benefit 过滤 + 降序排序 |
| `mlir/lib/Rewrite/PatternApplicator.cpp:132` | `matchAndRewrite` | 逐个 pattern 尝试并回调 |
| `mlir/include/mlir/IR/PatternMatch.h:34-48` | `PatternBenefit` / `impossibleToMatch` | benefit=0 的真实语义 |
| `mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h:43-175` | `GreedyRewriteConfig` 默认值 | `max-iterations=10` 等 |

核对命令：

```bash
grep -n 'processWorklist()\|op->fold(foldResults)\|max-iterations\|getMaxIterations()' mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp | head
grep -n 'applyCostModel\|stable_sort\|isImpossibleToMatch' mlir/lib/Rewrite/PatternApplicator.cpp
grep -n 'markAllAnalysesPreserved\|applyPatternsGreedily' mlir/lib/Transforms/Canonicalizer.cpp
```

## 10.8 可跑实验

准备两个输入：

```bash
cat > /tmp/mw/ch10.mlir <<'EOF'
func.func @fold_demo(%a: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %c1 = arith.constant 1 : i32
  %0 = arith.addi %a, %c0 : i32
  %1 = arith.muli %0, %c1 : i32
  %2 = arith.subi %1, %1 : i32
  return %2 : i32
}
EOF

cat > /tmp/mw/ch10pat.mlir <<'EOF'
func.func @pat_demo(%a: i32) -> i32 {
  %c8 = arith.constant 8 : i32
  %0 = arith.muli %a, %c8 : i32
  return %0 : i32
}
EOF
```

### 实验 1：`-canonicalize` 的净效果（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch10.mlir -canonicalize
```

```text
module {
  func.func @fold_demo(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    return %c0_i32 : i32
  }
}
```

`addi x,0`、`muli x,1` 被 fold；`subi %1,%1` 被折成 `0`；最后只剩一个被 `return` 使用的常量。注意 `%c0_i32` 还在——它被 return 用了，不是死 op。

### 实验 2：fold 失败后才会试 pattern（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch10pat.mlir -canonicalize --debug-only=pattern-application
```

```text
[pattern-application PatternApplicator.cpp:212 1] Trying to match "{anonymous}::MulIMulIConstant"
[pattern-application PatternApplicator.cpp:227 1]  -> matchAndRewrite failed
module {
  func.func @pat_demo(%arg0: i32) -> i32 {
    %c8_i32 = arith.constant 8 : i32
    %0 = arith.muli %arg0, %c8_i32 : i32
    return %0 : i32
  }
}
```

这条输出同时证明三件事：`arith.muli` **没有** fold（否则不会走到 applicator）、pattern 名字是 `{anonymous}::MulIMulIConstant`、该 pattern 在这个 op 上匹配失败（`muli %a, 8` 不是“乘常数再乘常数”）。

### 实验 3：`max-iterations=0` 让 canonicalize 变成 no-op（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch10.mlir \
  --pass-pipeline='builtin.module(func.func(canonicalize{max-iterations=0}))'
```

```text
module {
  func.func @fold_demo(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %c1_i32 = arith.constant 1 : i32
    %0 = arith.addi %arg0, %c0_i32 : i32
    %1 = arith.muli %0, %c1_i32 : i32
    %2 = arith.subi %1, %1 : i32
    return %2 : i32
  }
}
```

IR 完全没变：`++iteration > 0` 立刻 `break`，连一轮都没跑。这是排查“到底哪条规则改的”的有用手段——从 0 往上加，找到第一次出现变化的迭代数。

### 实验 4：只有真的改了才打印（`--mlir-print-ir-after-change`，实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir \
  --pass-pipeline='builtin.module(func.func(cse,canonicalize))' --mlir-print-ir-after-change
```

```text
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    return %0 : i32
  }
  func.func @mul(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.muli %arg0, %arg1 : i32
    return %0 : i32
  }
}
```

没有任何 `IR Dump After ...` 头 —— 因为在 CSE 之后 `canonicalize` 没改东西（`!changed` → `markAllAnalysesPreserved`）。把输入换成有可折内容时才会出现 dump 头。这条开关是判断“是哪个 pass 改的”的第一步，比 `-after-all` 噪声小得多。

### 实验 5：greedy-rewriter 的完整决策轨迹（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch10.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --debug-only=greedy-rewriter 2>&1 | head -30
```

```text
[greedy-rewriter:1] 
[greedy-rewriter:1] //===-------------------------------------------===//
[greedy-rewriter:1] Processing operation : 'arith.constant'(0x561c4b94a850) {
[greedy-rewriter:1]   %0 = "arith.constant"() <{value = 0 : i32}> : () -> i32
[greedy-rewriter:1] 
[greedy-rewriter:1] } -> failure : all patterns failed to match
...
[greedy-rewriter:1] Processing operation : 'arith.addi'(0x561c4b94ae50) {
[greedy-rewriter:1]   %2 = "arith.addi"(%arg0, %0) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
[greedy-rewriter:1] 
[greedy-rewriter:1] } -> success : operation was folded
[greedy-rewriter:1] ** Replace : 'arith.addi'(0x561c4b94ae50)
[greedy-rewriter:1] ** Modified: 'arith.muli'(0x561c4b94b3e0)
[greedy-rewriter:1] ** Erase   : 'arith.addi'(0x561c4b94ae50)
[greedy-rewriter GreedyPatternRewriteDriver.cpp:199 1] // *** IR Dump After Successful Folding ***
```

读法：`-> failure : all patterns failed to match`（常量 op 没 fold）→ `-> success : operation was folded`（addi 被 fold）→ `** Replace/Modified/Erase` 三行是 listener 通知序列。dump 的源码行号 `:199` 就是 `logSuccessfulFolding`。

### 实验 6：`-test-greedy-patterns` 限制迭代（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir -test-greedy-patterns='max-iterations=1'
```

```text
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    return %0 : i32
  }
  func.func @mul(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.muli %arg0, %arg1 : i32
    return %0 : i32
  }
}
```

一轮就够，所以和默认结果一致。这个 pass 的价值不在结果，而在它可以配 `--dump-notifications` 观察 listener 事件顺序（`-test-greedy-patterns{...}` 的选项有 `cse-constants`/`dump-notifications`/`fold`/`max-iterations`/`top-down`）。

### 实验 7：benchmark 之外的“未实测”项

- **benefit 相同时的具体胜者**：需要两个注册顺序已知、benefit 相同的 pattern 才能从外部观测，本树没有一个 CLI 能直接打印“最终排序后的 pattern 列表”（`patterns` 是 `PatternApplicator` 的私有成员）。故**未实测**，本文只按 `stable_sort` 的语义陈述。
- **`max-iterations` 打满 + `test-convergence=true` 的失败路径**：需要一个真正成环的 pattern 对；本树没有现成 CLI 开关能构造，故**未实测**，只给源码依据（`Canonicalizer.cpp:88-90`）。

### 实验 8：`max-num-rewrites` 不限制 fold（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch10.mlir \
  --pass-pipeline='builtin.module(func.func(canonicalize{max-num-rewrites=1}))'
```

```text
module {
  func.func @fold_demo(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    return %c0_i32 : i32
  }
}
```

结果和默认完全一样：三个 op 的 fold（`addi x,0`、`muli x,1`、`subi x,x`）都发生了。原因是 `++numRewrites` 只在 pattern 匹配成功时自增（`GreedyPatternRewriteDriver.cpp:1015`），fold 路径不计数。所以 **`max-num-rewrites` 是 pattern 的刹车，不是 fold 的刹车**。

## 10.9 踩雷集锦

1. **错误直觉：pattern 的 benefit 越高越“优先执行”。** → 正确认识：benefit 只决定同一个 op 上试 pattern 的顺序，且 fold 永远在 pattern 之前。
2. **错误直觉：`PatternBenefit(0)` 是“最低优先级”。** → 正确认识：那是 `impossibleToMatch()`，pattern 会被直接丢弃（`PatternApplicator.cpp:71-80`）。
3. **错误直觉：fold 必须返回 `Attribute`。** → 正确认识：返回空 `foldResults` 表示“就地改了 op”；driver 只发 `notifyOperationModified`，不会替换结果。
4. **错误直觉：canonicalize 不收敛会报错。** → 正确认识：默认静默；只有 `test-convergence=true` 才 `signalPassFailure()`。CI 里的隐性不稳定就藏在这。
5. **错误直觉：`markAllAnalysesPreserved()` 是无害优化。** → 正确认识：它同时让 pass manager 跳过 verifier（Ch 09 的 `runVerifierNow`）；只在 `!changed` 时用。
6. **错误直觉：自己 new 一个 `IRRewriter` 在 pattern 里改 IR 没问题。** → 正确认识：driver 靠 listener 维护 worklist/可达性；重写必须用传入的 `PatternRewriter`（它挂着 driver 的 listener）。
7. **错误直觉：`-canonicalize` 和 `-cse` 等价可换。** → 正确认识：CSE 合并*相同*表达式，canonicalize 把表达式变成*标准形*；顺序不同结果可能不同（实验 4 里 CSE 先跑，canonicalize 无事可做）。
8. **错误直觉：`max-num-rewrites` 能兜住所有“改太多”的场景。** → 正确认识：它只数 pattern 成功次数（`:1015`），fold 不计入；要限制总工作量只能靠 `max-iterations`。另外 pattern 数量本身也是成本：全量收集（`Canonicalizer.cpp:74-81`）意味着每个 op 都要为每个 pattern 付匹配代价，`filter-dialects` 是官方隔离手段。

## 10.10 本章重点整理

- `canonicalize` 是“外层迭代（`max-iterations`，默认 10）+ 内层 worklist”的双循环；打满限制不报错，只返回 failure。
- 每个 op 的处理顺序是：不可达跳过 → 死 op 删除 → `fold()` → `PatternApplicator`。
- 常量 op 被排除在 fold 之外，防止“折成 Attribute → 重新 materialize”的死循环。
- pattern 按 benefit 降序、同值按注册顺序（`stable_sort`）；benefit 0 等于禁用。
- `Canonicalizer` 只在 `!changed` 时 `markAllAnalysesPreserved()`，这也顺带跳过 verifier。
- `-debug-only=greedy-rewriter` 给决策轨迹，`--debug-only=pattern-application` 给匹配尝试，两者互补。
- `max-iterations=0` 是二分定位“第几轮改的”的实用手段。

## 10.11 自我检核

- [ ] 能画出 `canonicalize` 的双循环，并指出 fold 在循环里的位置
- [ ] 能解释为什么常量 op 不参与 fold，并说出对应源码行
- [ ] 能说出 benefit=0 的真实语义，以及它和 `stable_sort` 的关系
- [ ] 能解释 `max-iterations` 打满后 `applyPatternsGreedily` 返回什么、`Canonicalizer` 又怎么处理
- [ ] 能复现 `-debug-only=greedy-rewriter` 的 `-> success : operation was folded` 与 `** Erase` 序列
- [ ] 能用 `--mlir-print-ir-after-change` 判断“是不是这个 pass 改的”
- [ ] 能说出 `filter-dialects` 解决的是什么问题
- [ ] 能解释为什么 pattern 里必须用 `PatternRewriter` 而不是自建 `IRRewriter`

## 10.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Canonicalization.md`**
  - 读哪里：`## General Design`（20 行）、`### When to use the fold method vs RewriterPatterns`（300 行）
  - 能学到什么：官方对 fold/pattern 分工的判据
  - 和本章关联：10.3 的表格来自这一节
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp`**
  - 读哪里：`818-960`（processWorklist 的 fold 分支）、`1228-1320`（simplify 的迭代控制）
  - 能学到什么：每个“跳过/重扫”条件背后的正确性理由
  - 和本章关联：10.4 与 10.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Rewrite/PatternApplicator.cpp`**
  - 读哪里：`58-125`（cost model 与排序）、`132-240`（逐个尝试）
  - 能学到什么：benefit 的真实使用方式
  - 和本章关联：10.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h`**
  - 读哪里：`43-175`（`GreedyRewriteConfig` 全部字段与默认值）
  - 能学到什么：`top-down`、`region-simplify`、`cse-between-iterations`、`strictness` 的语义
  - 和本章关联：10.6 的默认值表
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Transforms/greedy-cfg-reachability.mlir`**
  - 读哪里：全部 RUN 行与注释
  - 能学到什么：不可达块为什么必须先删，否则 pattern 会在非法 IR 上跑
  - 和本章关联：10.2 的 `eraseUnreachableBlocks`

greedy driver 只会“按规则改 IR”。它不知道值的语义，也不知道某个值是否可达。下一章进入 MLIR 的语义分析层：`DataFlowSolver` 如何用 lattice 和固定点迭代，把“这个值可能是常量/活的/可被 4 整除”算出来。

→ 下一章：[Ch 11 DataFlow 与分析框架](./11-dataflow-and-analyses.md)
