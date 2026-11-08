# Ch 11 — DataFlow 与分析框架

> **目标**：能读任何一个 sparse/dense data-flow analysis 的 `initialize` / `visitOperation` / `setToEntryState`，判断它“乐观还是悲观、前向还是后向、有没有覆盖 region 分支”，并用 `mlir-opt` 的 test pass 把结论跑成可复现输出。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，assertions ON（`DATAFLOW_DEBUG`/`LDBG` 可用）。
>
> **前置**：Ch 04（SSA 与 dominance）、Ch 09（analysis 的缓存与失效）。

---

## 11.1 为什么维护者要读 data-flow 框架

MLIR 的“分析”有两种完全不同的东西，混起来就会写出错的 bug 报告：

1. **一次性的查询**（`DominanceInfo`、`AliasAnalysis`、`DataLayout`）。构造即算，构造函数里跑一遍，之后只读。维护者常改它们是因为**缓存粒度**：挂在 module 上还是 func 上，决定它会不会被跨 pass 复用成脏数据。
2. **固定点求解**（`DataFlowSolver` + child analyses）。你声明 lattice 和 transfer function，框架负责把 worklist 跑到不动点。维护者常改它们是因为**边界条件**：region 分支、call、loop 的 back-edge 有没有连上。

真实 issue 里最常见的三句话：

- “这个值明明一定是 4 的倍数，为什么分析说是 1？” → transfer function 没覆盖某个 op，或者 loop 结果被保守地重置了（`IntegerDivisibilityAnalysis.cpp:60-61` 明确这么做）。
- “dead code 分析把可达的 block 判成 dead。” → `DeadCodeAnalysis` 的“悲观/乐观”初始状态被搞反，或 region 分支的 successor inputs 没接上。
- “同一个分析在两个 pass 里结果不一样。” → 其中一个 pass 没声明 preserve，框架重建了实例；或者 IR 变了但分析不知道（没有 `propagateIfChanged`）。

所以本章的主线是：**框架的四个概念（ProgramPoint / AnalysisState / DataFlowAnalysis / Worklist）→ 两类分析的写法差异 → 三张真实 lattice 的行为**。

## 11.2 先建立直觉：program point 网格上的不动点

```text
                 ┌──────────── DataFlowSolver ────────────┐
                 │  childAnalyses: [DeadCode, Liveness…]  │
   IR:           │  worklist: [(point, analysis), …]      │
   func @f() {   │                                        │
  ┌────────────┐ │  ProgramPoint 是“网格坐标”：           │
  │ ^bb0       │ │    (op 之前) (op 之后) (block 开头)    │
  │  %c = 4    │ │                                        │
  │  %d = %c*2 │ │  AnalysisState 是“网格上的值”：        │
  │  br ^bb1   │ │    Lattice<T>（sparse）                │
  ├────────────┤ │    DenseLattice（dense，按 block）     │
  │ ^bb1       │ │                                        │
  │  use %d    │ │  DataFlowAnalysis 是“传播规则”：       │
  └────────────┘ │    initialize(top)                     │
                 │    visit(point) → propagateIfChanged   │
                 └────────────────┬───────────────────────┘
                                  │ 直到 worklist 为空
                                  ▼
                       查询：solver.lookupState<T>(point)
```

三条必须记住的规则：

1. **一个 analysis 只在自己的 program point 上被调用**。`visit(point)` 里必须自己把前驱状态 join 过来；框架不会替你搬数据。
2. **只有 `ChangeResult::Change` 才会重新入队**（`DataFlowFramework.cpp:160-171`）。返回 `NoChange` 但实际改了 lattice，就是静默不收敛。
3. **未初始化 ≠ 悲观值**。文档明确（`DataFlowFramework.h:506-515`）：未初始化的 state 表示“还不知道”，查询前必须先跑到不动点；把 `nullptr` 当成“dead/false”是最常见的误用。

## 11.3 框架骨架：四个类型 + 一次运行

| 类型 | 定义位置 | 职责 |
|---|---|---|
| `ProgramPoint` | `DataFlowFramework.h`（`getProgramPointBefore/After`，`:380-405`） | 网格坐标，可锚在 op 或 block 上 |
| `AnalysisState` | `DataFlowFramework.h:530-560` | 值 + `onUpdate` 钩子（把自己重新入队） |
| `DataFlowAnalysis` | `DataFlowFramework.cpp:178-190` | `initialize(top)` / `visit(point)` |
| `DataFlowSolver` | `DataFlowFramework.cpp:112` | 装 child analyses、跑到不动点 |

运行流程（`DataFlowSolver::initializeAndRun`，`DataFlowFramework.cpp:112-157`）：

```cpp
  // Initialize equivalent lattice anchors.
  for (DataFlowAnalysis &analysis : llvm::make_pointee_range(childAnalyses)) {
    if (!shouldInitialize(analysis))
      continue;
    analysis.initializeEquivalentLatticeAnchor(top);
  }

  // Initialize the analyses.
  for (DataFlowAnalysis &analysis : llvm::make_pointee_range(childAnalyses)) {
    if (!shouldInitialize(analysis))
      continue;
    DATAFLOW_DEBUG(LDBG() << "Priming analysis: " << analysis.debugName);
    if (failed(analysis.initialize(top)))
      return failure();
  }

  // Run the analysis until fixpoint.
  while (!worklist.empty()) {
    auto [point, analysis] = dequeue();
    DATAFLOW_DEBUG(LDBG() << "Invoking '" << analysis->debugName
                          << "' on: " << *point);
    if (failed(analysis->visit(point)))
      return failure();
  }
```

维护要点：

- `initializeEquivalentLatticeAnchor(top)` 先跑；自定义 analysis 若重写它，必须调基类。
- `analysisFilter` 允许只重跑部分 analysis（`initializeAndRun(top, filter)`），是增量更新入口。

## 11.4 sparse forward analysis 的生命周期

以 `AbstractSparseForwardDataFlowAnalysis`（`SparseAnalysis.h:183`）为例：

```cpp
// SparseAnalysis.cpp:55-66
LogicalResult
AbstractSparseForwardDataFlowAnalysis::initialize(Operation *top) {
  // Initialize the entry block of the region.
  ...
      setToEntryState(getLatticeElement(argument));
  ...
  return initializeRecursively(top);
}
```

四个虚函数的契约：

| 函数 | 位置 | 你必须做什么 |
|---|---|---|
| `initialize(top)` | `SparseAnalysis.cpp:55` | 给 entry block 的 block argument 设初值；遍历嵌套 region 用 `initializeRecursively`（`:69`） |
| `setToEntryState(lattice)` | 纯虚（`SparseAnalysis.h:230`） | 决定“乐观”还是“悲观”：注入 `unknown/overdefined`，或直接 `markLive()` 之类 |
| `visitOperation(op, operands, results)` | `SparseAnalysis.h:312` | 读 operands 的 lattice，写 results 的 lattice |
| `join(lhs, rhs)` | `SparseAnalysis.cpp:373-375` | 默认实现就是 `propagateIfChanged(lhs, lhs->join(rhs))` |

`visitOperation` 的默认实现（`SparseAnalysis.cpp:116-155`）会先 `getLatticeElementFor(point, operand)` 把 operands 取出来，再调 `visitOperationImpl`。维护含义：**你重写 `visitOperationImpl` 而不是 `visitOperation`**，否则要自己重复取 lattice 的逻辑。

`propagateIfChanged` 是唯一的“通知框架我改了”的通道：

```cpp
// DataFlowFramework.cpp:160-171
void DataFlowSolver::propagateIfChanged(AnalysisState *state,
                                        ChangeResult changed) {
  assert(isRunning &&
         "DataFlowSolver is not running, should not use propagateIfChanged");
  if (changed == ChangeResult::Change) {
    DATAFLOW_DEBUG(LDBG() << "Propagating update to " << state->debugName
                          << " of " << state->anchor << "\n"
                          << "Value: " << *state);
    state->onUpdate(this);
  }
}
```

`assert(isRunning)` 说明：**不能在 `initializeAndRun` 之外改 lattice**。pass 里想“读结果再补一刀”是合法的，但必须新起一次 `initializeAndRun`。

## 11.5 dense / sparse、前向 / 后向：四种组合

| 基类 | 头文件 | state 挂在哪 | 适合什么 |
|---|---|---|---|
| `SparseForwardDataFlowAnalysis` | `SparseAnalysis.h:300` | SSA value | 值级属性：常量、整除性、范围 |
| `AbstractSparseBackwardDataFlowAnalysis` | `SparseAnalysis.cpp:389` | SSA value | 从 use 反推 def（liveness） |
| `DenseForwardDataFlowAnalysis` | `DenseAnalysis.h:204` | block（前后边界） | 数据流事实在 CFG 上传播（如 last-writer） |
| `AbstractDenseBackwardDataFlowAnalysis` | `DenseAnalysis.h:357` | block | 后向 CFG 事实 |

选择判据很简单：**你要描述的对象是“值”还是“点上的一组事实”？** 值 → sparse；点/边 → dense。

一个例外要记住：`LivenessAnalysis` 是 sparse **backward**，但它复用了 forward 基类的 `visitOperation` 形态（`LivenessAnalysis.cpp:88-100`），因为它关心的是“operand 会不会被用”，方向由 `visitOperation` 里的传播决定，而不是由基类方向决定。

## 11.6 三个真实 lattice 的行为

### `DeadCodeAnalysis`：悲观起点 + 常量驱动

`-test-dead-code-analysis` 打印每个 block 和每条 CFG edge 的 `Executable` 状态。它的语义是“**除非证明可达，否则不可达**”（悲观）：

```mlir
func.func @test_dead(%cond: i1) -> i32 attributes {tag = "dead"} {
  %true = arith.constant true
  cf.cond_br %true, ^bb1, ^bb2
...
```

实测（完整命令见 11.8 实验 1）：

```text
dead:
 region #0
  ^bb0 = live
  ^bb1 = live
   from ^bb0 = live
  ^bb2 = dead
   from ^bb0 = dead
```

`^bb2` 是 dead，因为条件恒为 true；`from ^bb0 = dead` 说明**边**也被单独标记。同一个文件里换成非常量条件（`tag = "cfg"`），`^bb2` 与 `from ^bb0` 都变成 live——两条边都会走。

维护含义：`Executable` 是 lattice，`setToLive()`（`DeadCodeAnalysis.cpp:41`）只会单向变 live；所以它是**单调**的，不会来回抖。region 分支与 call 的处理分别在 `:490`（`visitRegionBranchOperation`）和 `:410`（`visitCallOperation`），这两处是修“误判可达”的第一现场。

### `LivenessAnalysis`：从 use 反向传播

`-test-liveness-analysis` 用 `tag` 标出要打印的 op（完整输出见实验 2）。`zero` 活着是因为它被 `memref.store` 用了（memory effect）；`dead` 常量没有任何 use。关键实现是 `wouldOpBeTriviallyDead(op)`（`LivenessAnalysis.cpp:94-100`）：**有副作用或 return-like 的 op，其 operands 一律标 live**。

### `IntegerDivisibilityAnalysis`：保守重置 loop 值

```text
%1 = "test.int_divisibility"(%0) {divisibility = "udiv = 4, sdiv = 4"}
%1 = "test.int_divisibility"(%0) {divisibility = "udiv = 1, sdiv = 1"}
```

（两行来自两个不同函数，见实验 3。）`muli %arg0, 4` 得到 4；两个未知 index 相加得到 1（即“无信息”）。原因在源码：

```cpp
    // Catch loop results with loop-variant divisibility and conservatively
    // set them to divisibility 1 (no information) so we don't ratchet
```

维护含义：**“分析不够强”通常不是 bug，是刻意的保守**。想加强它，要么改 transfer function，要么补 `InferIntDivisibilityOpInterface` 实现（Ch 12）。

## 11.7 既是查询又是分析的三件套

| 名字 | 类型 | 入口 | 维护热点 |
|---|---|---|---|
| `DominanceInfo` | 一次性查询 | `-test-print-dominance` | `Nearest` 表的正确性、region 嵌套 |
| `AliasAnalysis` | 一次性查询 + 可扩展 | `-test-alias-analysis` | 新 op 没有 alias 规则 → 一律 `MayAlias` |
| `ValueBoundsConstraintSet` | 查询（Presburger） | `-test-affine-reify-value-bounds`、`computeConstantBound` | stop condition、整数类型放宽 |

`AliasAnalysis` 的实测输出（实验 4）：

```text
Testing : "simple"
alloca_1#0 <-> alloca_2#0: NoAlias
alloca_1#0 <-> func.region0#0: NoAlias
alloca_2#0 <-> func.region0#0: NoAlias
alloca_1#0 <-> func.region0#1: NoAlias
alloca_2#0 <-> func.region0#1: NoAlias
func.region0#0 <-> func.region0#1: MayAlias
```

两个 `alloca` 之间 `NoAlias`（不同分配），两个函数参数之间 `MayAlias`（可能是同一个 memref）。维护含义：**参数永远退化成 MayAlias 是设计选择**，不是精度缺陷；想要更准只能靠 `modref` 或 bufferization 的 alias 信息。

## 11.8 源码落点

| 位置 | 内容 | 维护相关性 |
|---|---|---|
| `mlir/lib/Analysis/DataFlowFramework.cpp:112` | `initializeAndRun` | 初始化 + 不动点主循环 |
| `mlir/lib/Analysis/DataFlowFramework.cpp:133` | `initializeEquivalentLatticeAnchor` | 自定义 analysis 必须调基类 |
| `mlir/lib/Analysis/DataFlowFramework.cpp:160` | `propagateIfChanged` | 唯一通知通道，带 `isRunning` 断言 |
| `mlir/include/mlir/Analysis/DataFlowFramework.h:325` | `class DataFlowSolver` | API 入口 |
| `mlir/include/mlir/Analysis/DataFlowFramework.h:506-515` | 未初始化语义 | “不知道”≠“假” |
| `mlir/lib/Analysis/DataFlow/SparseAnalysis.cpp:55` | `initialize` | entry state + 递归 |
| `mlir/lib/Analysis/DataFlow/SparseAnalysis.cpp:116` | `visitOperation` | 取 operands 再调 `visitOperationImpl` |
| `mlir/lib/Analysis/DataFlow/SparseAnalysis.cpp:373` | `join` | 默认 `propagateIfChanged(lhs, lhs->join(rhs))` |
| `mlir/include/mlir/Analysis/DataFlow/SparseAnalysis.h:183/:300` | forward 基类 | sparse 分析的落点 |
| `mlir/lib/Analysis/DataFlow/DeadCodeAnalysis.cpp:138/:323/:410/:490` | `initialize`/`visit`/call/region branch | 可达性误判的四个现场 |
| `mlir/lib/Analysis/DataFlow/LivenessAnalysis.cpp:36/:42/:95` | `markLive`/`meet`/memory-effect 分支 | liveness 判定核心 |
| `mlir/lib/Analysis/DataFlow/IntegerDivisibilityAnalysis.cpp:60-61` | loop 结果保守重置 | “分析太弱”的根因 |
| `mlir/lib/Analysis/DataFlow/DenseAnalysis.cpp` | dense 基类实现 | block 级事实 |

核对命令：

```bash
grep -n 'initializeAndRun\|propagateIfChanged' mlir/lib/Analysis/DataFlowFramework.cpp
grep -n 'setToEntryState\|visitOperationImpl\|::join' mlir/lib/Analysis/DataFlow/SparseAnalysis.cpp | head
grep -n 'visitRegionBranchOperation\|visitCallOperation\|::initialize' mlir/lib/Analysis/DataFlow/DeadCodeAnalysis.cpp
```

## 11.9 可跑实验

所有输入放 `/tmp/mw/`。

### 实验 1：悲观可达性 vs 常量驱动（实测）

```bash
cat > /tmp/mw/ch11-dead2.mlir <<'EOF'
func.func @test_cfg(%cond: i1) -> i32 attributes {tag = "cfg"} {
  %c1 = arith.constant 1 : i32
  cf.cond_br %cond, ^bb1, ^bb2
^bb1:
  return %c1 : i32
^bb2:
  %c2 = arith.constant 2 : i32
  return %c2 : i32
}
func.func @test_dead(%cond: i1) -> i32 attributes {tag = "dead"} {
  %true = arith.constant true
  cf.cond_br %true, ^bb1, ^bb2
^bb1:
  %c1 = arith.constant 1 : i32
  return %c1 : i32
^bb2:
  %c2 = arith.constant 2 : i32
  return %c2 : i32
}
EOF
$T/bin/mlir-opt /tmp/mw/ch11-dead2.mlir -test-dead-code-analysis
```

```text
cfg:
 region #0
  ^bb0 = live
  ^bb1 = live
   from ^bb0 = live
  ^bb2 = live
   from ^bb0 = live
op_preds: predecessors: (none)
dead:
 region #0
  ^bb0 = live
  ^bb1 = live
   from ^bb0 = live
  ^bb2 = dead
   from ^bb0 = dead
op_preds: predecessors: (none)
```

**注意实验前提**：`-test-dead-code-analysis` 只打印带 `tag` 属性的 op（`TestDeadCodeAnalysis.cpp:25-28`）。不加 `tag` 时它一声不响——这不是分析没跑。

### 实验 2：liveness 的 live / not live（实测）

```bash
cat > /tmp/mw/ch11-live.mlir <<'EOF'
func.func @live(%arg0: memref<i32>) {
  %c0 = arith.constant {tag = "zero"} 0 : i32
  memref.store %c0, %arg0[] : memref<i32>
  %dead = arith.constant {tag = "dead"} 42 : i32
  return
}
EOF
$T/bin/mlir-opt /tmp/mw/ch11-live.mlir -test-liveness-analysis
```

```text
test_tag: zero:
 result #0: live
test_tag: dead:
 result #0: not live
module {
  func.func @live(%arg0: memref<i32>) {
    %c0_i32 = arith.constant {tag = "zero"} 0 : i32
    memref.store %c0_i32, %arg0[] : memref<i32>
    %c42_i32 = arith.constant {tag = "dead"} 42 : i32
    return
  }
}
```

### 实验 3：整除性分析的“有信息/无信息”（实测）

```bash
cat > /tmp/mw/ch11-div.mlir <<'EOF'
func.func @muli_constant(%arg0: index) -> index {
  %c4 = arith.constant 4 : index
  %0 = arith.muli %arg0, %c4 : index
  %1 = "test.int_divisibility"(%0) : (index) -> index
  return %1 : index
}
func.func @add_unknown(%arg0: index, %arg1: index) -> index {
  %0 = arith.addi %arg0, %arg1 : index
  %1 = "test.int_divisibility"(%0) : (index) -> index
  return %1 : index
}
EOF
$T/bin/mlir-opt /tmp/mw/ch11-div.mlir --test-int-divisibility-analysis
```

```text
module {
  func.func @muli_constant(%arg0: index) -> index {
    %c4 = arith.constant 4 : index
    %0 = arith.muli %arg0, %c4 : index
    %1 = "test.int_divisibility"(%0) {divisibility = "udiv = 4, sdiv = 4"} : (index) -> index
    return %1 : index
  }
  func.func @add_unknown(%arg0: index, %arg1: index) -> index {
    %0 = arith.addi %arg0, %arg1 : index
    %1 = "test.int_divisibility"(%0) {divisibility = "udiv = 1, sdiv = 1"} : (index) -> index
    return %1 : index
  }
}
```

`udiv = 1` 表示“只能被 1 整除”，等价于“没有信息”。

### 实验 4：alias 的 NoAlias / MayAlias（实测）

```bash
cat > /tmp/mw/ch11alias.mlir <<'EOF'
func.func @simple(%arg: memref<2xf32>, %arg1: memref<2xf32>) attributes {test.ptr = "func"} {
  %0 = memref.alloca() {test.ptr = "alloca_1"} : memref<8x64xf32>
  %1 = memref.alloca() {test.ptr = "alloca_2"} : memref<8x64xf32>
  return
}
EOF
$T/bin/mlir-opt /tmp/mw/ch11alias.mlir -pass-pipeline='builtin.module(func.func(test-alias-analysis))'
```

```text
Testing : "simple"
alloca_1#0 <-> alloca_2#0: NoAlias
alloca_1#0 <-> func.region0#0: NoAlias
alloca_2#0 <-> func.region0#0: NoAlias
alloca_1#0 <-> func.region0#1: NoAlias
alloca_2#0 <-> func.region0#1: NoAlias
func.region0#0 <-> func.region0#1: MayAlias
```

`test.ptr` 是给 `-test-alias-analysis` 认名字用的（`mlir/test/Analysis/test-alias-analysis.mlir:23-28`）。

### 实验 5：多线程下 analysis 打印会串行失败（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Analysis/test-foo-analysis.mlir -test-foo-analysis 2>&1 | head -12
```

```text
function: @function: @test_fork
function: @test_default_initinitfunction: @
test_two_joinfunction: @ block visits -> 
test_simple_loopa
test_one_join block visits -> a
...（交错持续到最后）
```

这不是分析算错，是 `llvm::outs()` 被多个线程同时写。加 `--mlir-disable-threading`：

```text
function: @test_default_init
a block visits -> 1
a -> 0
function: @test_one_join
a block visits -> 1
a -> 0
b -> 1
function: @test_two_join
a block visits -> 1
a -> 0
b -> 1
c -> 3
```

`test_fork` 的 `a -> 3 / b -> 5 / end -> 7` 直接展示了 lattice 的 join 结果（多个前驱的值被合并累加）。

### 实验 6：dominance 查询（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Analysis/test-dominance.mlir \
  -pass-pipeline='builtin.module(func.func(test-print-dominance))' --mlir-disable-threading 2>&1 | head -14
```

```text
Testing : func_condBranch
--- DominanceInfo ---
Nearest(0, 0) = 0
Nearest(0, 4) = 4
Nearest(1, 1) = 1
Nearest(1, 4) = 4
```

（上面是实验 6 输出的节选，中间行已省略。）`Nearest(0, 4) = 4` 表示 BB0 到 BB4 的最近公共支配者是 BB4 自己。

### 实验 7：dense forward（last-modified）与 dense backward（next-access）（实测）

`-test-last-modified` 是 dense **forward**（`TestDenseForwardDataFlowAnalysis.cpp:230`）：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Analysis/DataFlow/test-last-modified.mlir -test-last-modified --mlir-disable-threading 2>&1 | head -12
```

```text
test_tag: test_simple_mod:
 operand #0
  - a
 operand #1
  - b
test_tag: test_simple_mod_overwrite_a:
 operand #0
  - <unknown>
 operand #1
  - a
```

`-test-next-access` 是 dense **backward**（`TestDenseBackwardDataFlowAnalysis.cpp:278`），它把结果直接写成 op 属性：

```bash
$T/bin/mlir-opt mlir/test/Analysis/DataFlow/test-next-access.mlir \
  -test-next-access --split-input-file --mlir-disable-threading 2>&1 | head -6
```

```text
module {
  func.func @trivial(%arg0: memref<f32>, %arg1: f32) -> f32 {
    memref.store %arg1, %arg0[] {name = "store", next_access = ["unknown", ["load"]]} : memref<f32>
    %0 = memref.load %arg0[] {name = "load", next_access = ["unknown"]} : memref<f32>
    return %0 : f32
  }
```

`-test-written-to` 是 sparse **backward**（`TestSparseBackwardDataFlowAnalysis.cpp:174`），注意它必须 `--split-input-file`——这个测试文件里有重复符号名，整块解析会报 `redefinition of symbol named 'callee'`：

```bash
$T/bin/mlir-opt mlir/test/Analysis/DataFlow/test-written-to.mlir \
  -test-written-to --split-input-file --mlir-disable-threading 2>&1 | head -4
```

```text
test_tag: constant0:
 result #0: [a]
test_tag: constant1:
 result #0: [b]
```

### 实验 8：未实测项

- **`initializeAndRun(top, filter)` 的增量重跑**：没有 test pass 暴露 `analysisFilter`，我无法从 CLI 触发，故**未实测**。需要读 `TestDataFlowFramework.cpp` 自己加 pass 才能验证。

## 11.10 踩雷集锦

1. **错误直觉：`lookupState` 返回 `nullptr` 就是“不存在/假”。** → 正确认识：那是“还没跑到”（未初始化），语义见 `DataFlowFramework.h:506-515`；先确认 `initializeAndRun` 成功。
2. **错误直觉：改了 lattice 不需要通知框架。** → 正确认识：必须 `propagateIfChanged`；否则 worklist 不会重新入队，结果停在半路，而且不报错。
3. **错误直觉：`ChangeResult::NoChange` 可以随便返回。** → 正确认识：它是“没有变化”的契约；返回值与实际修改不一致会导致不收敛或提前收敛。
4. **错误直觉：分析太保守就是 bug。** → 正确认识：整除性对 loop 结果显式重置为 1（`IntegerDivisibilityAnalysis.cpp:60-61`），是防止“棘轮效应”的刻意设计。
5. **错误直觉：`func.func` 上的 solver 会跨函数分析。** → 正确认识：`isInterprocedural` 只在 top 是 `SymbolTable` 时生效（`DataFlowFramework.cpp:117-122`）。
6. **错误直觉：test pass 没输出就是分析没跑。** → 正确认识：`-test-dead-code-analysis`/`-test-liveness-analysis` 只打印带 `tag` 的 op。
7. **错误直觉：多线程下的乱序输出说明 lattice 坏了。** → 正确认识：那是 `outs()` 竞争；先 `--mlir-disable-threading`。
8. **错误直觉：`-test-cfg-loop-info` 可以挂 module。** → 正确认识：它是 op-agnostic pass，要写成 `builtin.module(any(test-cfg-loop-info))`；直接挂 module 会得到 `unable to schedule pass ... on 'builtin.module'`。

## 11.11 本章重点整理

- 四个概念：`ProgramPoint`（坐标）、`AnalysisState`（值）、`DataFlowAnalysis`（规则）、`DataFlowSolver`（不动点）。
- `initializeAndRun` 三步：登记等价锚 → `initialize` 每个 child → worklist 跑空；只有 `ChangeResult::Change` 触发重新入队，通知通道只有 `propagateIfChanged`。
- sparse 描述“值”，dense 描述“点/边”；forward/backward 是传播方向，不是 state 类型。
- `DeadCodeAnalysis` 悲观且单调（`Executable::setToLive`），region branch 与 call 是误判高发区；`LivenessAnalysis` 用 memory effect 判定“必须活”。
- 保守是设计：divisibility 对 loop 结果重置、alias 对参数退化，都是刻意的。

## 11.12 自我检核

- [ ] 能画出 program point 网格，并说清 sparse 与 dense 的 state 各挂在哪
- [ ] 能写出一个最小 sparse forward analysis 的四个虚函数，并说明 `setToEntryState` 为什么决定乐观/悲观
- [ ] 能解释 `propagateIfChanged` 的 `isRunning` 断言意味着什么
- [ ] 能复现 `-test-dead-code-analysis` 的 `^bb2 = dead` 并解释原因
- [ ] 能解释 liveness 为什么把 `memref.store` 的值标 live
- [ ] 能解释 `udiv = 1` 的含义，以及 loop 结果为什么被重置
- [ ] 能说出 `isInterprocedural` 的生效条件
- [ ] 能修好一个“test pass 没输出”的问题（`tag` 属性 / `--mlir-disable-threading` / `any(...)` 锚点）

## 11.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Tutorials/DataFlowAnalysis.md`**
  - 读哪里：`## Forward Dataflow Analysis`（12 行）、`### Lattices`（23 行）、`### ForwardDataflowAnalysis Driver`（182 行）
  - 能学到什么：官方对 lattice 单调性与 driver 职责的定义；和本章关联：11.3/11.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Analysis/DataFlowFramework.h`**
  - 读哪里：`307-560`（solver、state、未初始化语义）
  - 能学到什么：`onUpdate` 如何把自己重新入队；未初始化为何不能用 0 代替
  - 和本章关联：11.2/11.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Analysis/DataFlow/SparseAnalysis.h`**
  - 读哪里：`183-420`（forward 基类与 `visitOperationImpl`）
  - 能学到什么：哪些函数该重写、哪些只是模板转发
  - 和本章关联：11.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Analysis/DataFlow/DeadCodeAnalysis.cpp`**
  - 读哪里：`138-330`（initialize 与 visit）、`410-560`（call/region branch）
  - 能学到什么：可达性如何跨 region 与 call 传播
  - 和本章关联：11.6 第一段
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Analysis/DataFlow/TestLivenessAnalysis.cpp`**
  - 读哪里：`30-90`（pass + 结果打印）
  - 能学到什么：把 analysis 结果变成可断言文本（Practice B 模板）；和本章关联：实验 2

analysis 给出“这个值是什么”，interface 给出“这个 op 能做什么”。下一章进入 MLIR 的接口层：op/type/attr interface、external model，以及接口未注册时为什么会直接 abort。

→ 下一章：[Ch 12 Dialect interfaces 与 external models](./12-dialect-interfaces.md)
