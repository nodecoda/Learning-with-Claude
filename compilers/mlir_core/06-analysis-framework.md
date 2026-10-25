# Ch 6 分析框架与缓存

> **目标**：把“分析（analysis）”从“一个能算东西的函数”升级为“挂在某个 op 上、有生命周期、有失效契约的缓存对象”。学完本章，你能判断一段逻辑该写成 analysis 还是 pass，能说清 `DominanceInfo`、`DataFlowSolver`、`AliasAnalysis` 各自回答什么问题，以及为什么它们的失效条件完全不同。

## 为什么需要这个？

Ch 5 里我们反复说“analysis 会失效”，但没解释分析到底是什么。假设你要写一个 pass，把循环里反复计算的 `%x * %x` 提到循环外。你需要知道：

1. 这条指令是不是循环不变量？（支配关系 + 循环结构）
2. 它有没有副作用？能不能安全移动？（内存效应）
3. 它和循环里的写操作有没有冲突？（别名）

这三个问题分别属于三套分析。如果每个 pass 都自己从头算，会有两个后果：

- **慢**：支配树是 O(n) 甚至 O(n log n) 的，每个 pass 重算一遍，pipeline 的复杂度直接乘上 pass 数量。
- **不一致**：两个 pass 对“谁支配谁”的理解只要有一点不同，优化就会互相拆台。

所以 MLIR 把分析做成**共享缓存**：谁需要谁申请，框架负责算一次、缓存、在 pass 改完 IR 后失效。代价是引入了一个新契约——**你必须告诉框架“我改完之后哪些分析还成立”**。这个契约写错，不会立刻报错，只会在很远的地方产生错误结果。这是本章要训练的判断力。

## 先建立直觉：分析管理器是一棵和 IR 同构的树

`AnalysisManager` 不是全局单例，而是**每个 op 一个节点**：

```text
AnalysisManager @ builtin.module
├── AnalysisManager @ func.func @foo
│   └── AnalysisManager @ scf.for
└── AnalysisManager @ func.func @bar
```

每个节点里挂一张表：

```text
TypeID(DominanceInfo)        → DominanceInfo 实例
TypeID(DataLayoutAnalysis)   → DataLayoutAnalysis 实例
...
```

`getAnalysis<T>()` 的语义是：**在“当前 op”这个节点上，取 T 的实例；没有就现算一个并缓存**。

由此推出三条规则：

1. **同一个分析在不同 op 上是不同实例**。`func.func @foo` 的 `DominanceInfo` 和 `@bar` 的互不相干。
2. **父 op 的分析不能直接当子 op 的用**，反之亦然；需要跨层就用 `getCachedParentAnalysis` / `getChildAnalysis`。
3. **节点会随 pipeline 结束被清空**（Ch 5 的 `am.clear()`）。缓存不是永久的。

这套结构和 IR 的嵌套结构一一对应，所以叫它“和 IR 同构的树”并不夸张。

## 核心概念一：缓存键是 `(op, TypeID)`，不是 `TypeID`

看 `AnalysisMap::getAnalysisImpl` 的实现（`mlir/include/mlir/Pass/AnalysisManager.h` 第 195–215 行）：

```cpp
TypeID id = TypeID::get<AnalysisT>();
auto it = analyses.find(id);
if (analyses.end() == it) {          // miss: compute and insert
  std::tie(it, wasInserted) =
      analyses.insert({id, constructAnalysis<AnalysisT>(am, op)});
}
return static_cast<AnalysisModel<AnalysisT> &>(*it->second).analysis;
```

三个细节值得记住：

- **键是 `TypeID`**，所以每种分析类型在每个 `AnalysisMap` 里最多一份。你不能同时缓存“两棵不同配置的支配树”——要不同配置，就定义两个类型。
- **构造发生在第一次 `getAnalysis` 时**，不是 pass 开始时。没被用到的分析不会浪费编译时间。
- **构造可以拿 `AnalysisManager`**：`constructAnalysis` 有 `(OpT)` 与 `(OpT, AnalysisManager&)` 两个重载（第 217–231 行），后者让分析在构造时申请子分析。

`AnalysisMap` 属于某个具体的 op（第 134 行），`AnalysisManager` 只是它的轻量句柄（第 292 行）。所以 `getAnalysisManager()` 按值返回并不昂贵，它拷贝的是一个指针。

### 缓存查询的三种姿势

| API | 语义 | 什么时候用 |
|---|---|---|
| `getAnalysis<T>()` | 有则取，无则算 | 默认 |
| `getCachedAnalysis<T>()` | 只取，不算 | 想知道“之前有没有人算过” |
| `getChildAnalysis<T>(child)` | 对子 op 算/取 | 父 pass 需要子函数的信息 |
| `getCachedParentAnalysis<T>(parent)` | 对父 op 只取 | 子 pass 复用父级已算好的结果 |

（`mlir/include/mlir/Pass/Pass.h` 第 241–295 行。）

最后一行是实战里最常用的：模块级 pass 已经算过 `DataLayoutAnalysis`，函数级 pass 用 `getCachedParentAnalysis` 直接拿，避免重复构造。

## 核心概念二：`PreservedAnalyses` 是一张“我保证没动”的收据

分析失效的核心类型是 `PreservedAnalyses`（`AnalysisManager.h` 第 30–79 行），内部只有一个集合和四个操作：

```cpp
SmallPtrSet<TypeID, 2> preservedIDs;
void preserveAll();                  // 插入 AllAnalysesType 哨兵
bool isAll() const;                  // 有没有这个哨兵
bool isNone() const;                 // 集合是不是空的
template <typename AnalysisT> void preserve();
```

默认构造出来是**空集**，也就是 `isNone() == true`。PassManager 在每个 pass 跑完后调用：

```cpp
am.invalidate(pass->passState->preservedAnalyses);
```

（`mlir/lib/Pass/Pass.cpp` 第 618–619 行。）

空集的含义是：**这个 op 上所有分析全部作废，子 op 的分析缓存直接 `clear()`**（`Pass.cpp` 第 1183–1185 行）。这是最保守、也最安全的默认值。

### 分析可以自定义失效规则

分析可以实现 `isInvalidated(pa)` 钩子覆盖默认判断（`AnalysisManager.h` 第 83–95 行）：

```cpp
if constexpr (llvm::is_detected<has_is_invalidated, AnalysisT>::value)
  return analysis.isInvalidated(pa);
return !pa.isPreserved<AnalysisT>();
```

这把“我是否仍然有效”的知识放回分析内部：即使被标记保留，分析也可以因更细的条件判定自己失效（第 106–109 行）。

### 一个真实例子：CSE 保留支配信息

`mlir/lib/Transforms/CSE.cpp` 第 49–56 行：

```cpp
if (!changed)
  return markAllAnalysesPreserved();
// Only deletes redundant ops, never moves ops across blocks:
markAnalysesPreserved<DominanceInfo, PostDominanceInfo>();
```

这段代码是“失效契约”最好的教材：没改 IR → 全部保留；改了 IR，但只删 op、不移动 op → 支配树不变，保留支配分析，常量格/别名分析等不保留。

**保留分析不是“我觉得没事”，而是“我能证明这个不变量没被破坏”**。证明不了就老实让它失效。

## 核心概念三：`DominanceInfo` 回答“谁一定先执行”

`DominanceInfo` 的定义在 `mlir/include/mlir/IR/Dominance.h` 第 143 行，核心 API 是两组谓词：

```cpp
bool properlyDominates(Operation *a, Operation *b) const;  // 156
bool dominates(Operation *a, Operation *b) const {          // 161
  return a == b || properlyDominates(a, b);
}
bool dominates(Value a, Operation *b) const;                // 171
bool dominates(Block *a, Block *b) const;                   // 177
```

`properlyDominates` 是严格的“先于”；`dominates` 允许相等。对 SSA 来说，`dominates(Value, Operation)` 是最常用的形式：这个值定义在 b 之前吗？b 能用它吗？

它的失效条件很明确：**只要 op 在块之间移动，或者块结构变化，支配关系就可能变**。删 op、改 operand、原地折叠都不影响支配树。这就是 CSE 敢保留它的原因。

`DominanceInfo` 还有个常被忽略的细节：它按需计算，并且对“同一块内的先后”用迭代器位置比较，而不是只看块级支配。所以 `dominates(Value, Operation)` 能正确处理同一块里“定义在使用之后”的非法情况（正常情况下 verifier 会拦，但 pass 中途可能出现）。

## 核心概念四：`DataFlowSolver` 回答“这个值可能是什么”

`DominanceInfo` 是结构分析，`DataFlowSolver` 是**格（lattice）上的不动点求解器**。它的入口在 `mlir/include/mlir/Analysis/DataFlowFramework.h` 第 324 行，核心方法是：

```cpp
LogicalResult initializeAndRun(
    Operation *top,
    llvm::function_ref<bool(DataFlowAnalysis &)> analysisFilter = {});
```

（第 342 行。）实现是一个标准 worklist 循环（`mlir/lib/Analysis/DataFlowFramework.cpp` 第 112–158 行）：

```cpp
// Run the analysis until fixpoint.
while (!worklist.empty()) {
  auto [point, analysis] = worklist.front();
  worklist.pop();
  if (failed(analysis->visit(point)))
    return failure();
}
```

用起来分三步：

1. `solver.load<SomeAnalysis>()` 注册分析；
2. `solver.initializeAndRun(op)` 跑到不动点；
3. 通过 `solver.lookupState<T>(value)` 读结果。

真实的消费者是 SCCP（`mlir/lib/Transforms/SCCP.cpp` 第 122–130 行）：

```cpp
DataFlowSolver solver;
solver.load<DeadCodeAnalysis>();
solver.load<SparseConstantPropagation>();
if (failed(solver.initializeAndRun(op)))
  return signalPassFailure();
rewrite(solver, op->getContext(), op->getRegions());
```

注意它同时装了两个分析：`DeadCodeAnalysis` 判断哪些块可达，`SparseConstantPropagation` 判断值是不是常量。两个分析共享一个 solver、共享一次不动点迭代——这就是“分析框架”相对“每个 pass 自己写循环”的价值。

`DataFlowSolver` 的失效条件比 `DominanceInfo` 苛刻得多：它依赖 SSA 图的**值**和**控制流**。改一个 operand、删一个分支、把常量折掉，都可能让格值变化。所以它几乎从不被保留。

## 核心概念五：`AliasAnalysis` 回答“这两块内存会不会重叠”

`AliasAnalysis` 是一个**聚合器**，不是具体算法。`mlir/include/mlir/Analysis/AliasAnalysis.h` 第 232 行的类里只有一个实现列表：

```cpp
SmallVector<std::unique_ptr<Concept>, 4> aliasImpls;
```

以及两个查询入口（第 273、280 行）：

```cpp
AliasResult alias(Value lhs, Value rhs);
ModRefResult getModRef(Operation *op, Value location);
```

职责边界非常清楚：

- `alias(lhs, rhs)`：两个值指向的内存**是否可能重叠**。保守答案是 `MayAlias`。
- `getModRef(op, location)`：这个 op 对这块内存**是读、写、还是都干**。保守答案是 `ModRef`。

它**不回答**“这个值是不是常量”（那是 DataFlow），也**不回答**“这个 op 会不会执行”（那是 Dominance + CFG）。它只回答内存关系。

为什么做成聚合器？因为 MLIR 没有统一的内存模型。`memref`、`llvm.ptr`、`tensor` 的别名规则不同，甚至同一个 dialect 里不同 op 的规则也不同。聚合器允许每个 dialect 通过接口注册自己的实现（`addAnalysisImplementation`，第 262–266 行），查询时依次询问，取最保守的结果。

头文件第 244–261 行把契约写得很直白：实现必须提供 `alias` 和 `getModRef`；不知道就返回 `MayAlias` / `ModRef`；只分配/释放内存的 op 对任何位置都没有 ModRef 行为。最后一句尤其重要——**不要把 alloc/free 当成读写**，否则很多合法重排会被误判为冲突。

## 底层机制：一次 `getAnalysis` 的完整路径

把前面几节串起来，一次分析查询的路径是：

```text
Pass::getAnalysis<T>()
  → AnalysisManager::getAnalysis<T>()            (AnalysisManager.h:315)
  → AnalysisMap::getAnalysis<T>(pi, am)          (AnalysisManager.h:152)
  → getAnalysisImpl: TypeID 查表                 (AnalysisManager.h:195)
      ├─ 命中 → 返回引用
      └─ 未命中 → instrument before → construct → insert → instrument after
```

`PassInstrumentation` 的 `runBeforeAnalysis` / `runAfterAnalysis`（第 203–212 行）意味着：**分析构造也是可观测的**。你可以在 instrumentation 里统计“哪个分析被算了几次”，这是排查“为什么 pipeline 这么慢”的第一手数据。

失效路径则相反：

```text
pass 跑完
  → am.invalidate(preservedAnalyses)             (Pass.cpp:619)
  → NestedAnalysisMap::invalidate                (Pass.cpp:1172)
      ├─ pa.isAll()  → 直接返回
      ├─ pa.isNone() → childAnalyses.clear()
      └─ 否则 → 逐个 child 递归 invalidate
```

最后一条分支说明：**部分保留比全部作废更贵**。全部作废是一次 `clear()`；部分保留要遍历所有子节点，逐个判断。所以“多保留几个分析”不一定更快，要看保留集合的大小和子节点数量。

## 示例一：Optimized build 里没有 test pass

MLIR 上游有大量 `test-*` pass 用来直接观察分析结果，比如 `test-dominance`、`test-dataflow-solver`。本机是 conda 的 Optimized build，这些 pass 不存在：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" basic.mlir --test-dominance
```

实测：

```text
mlir-opt.exe: Unknown command line argument '--test-dominance'.  Try: 'D:\Learning-with-Claude\.scratch\mlir-env\Library\bin\mlir-opt.exe --help'
mlir-opt.exe: Did you mean '--tosa-to-linalg'?
```

`--test-dataflow-solver` 同样不存在：

```text
mlir-opt.exe: Unknown command line argument '--test-dataflow-solver'.  Try: '...\mlir-opt.exe --help'
mlir-opt.exe: Did you mean '--sbvec-allow-files'?
```

这不是安装问题，而是构建选项：test pass 只在 `MLIR_INCLUDE_TESTS` 打开时注册。本机 `--help` 里能看到的 `--test-*` 只有零星的几个（例如 `--test-arm-sme-tile-allocation`、`--test-bufferization-analysis-only`），不足以直接打印支配树或数据流状态。

所以本章的实测只能走两条路：

1. **通过消费者的行为反推分析**（下一节的 CSE）。
2. **通过 verifier / dump 观察 pass 边界**（再下一节）。

其余结论一律标“源码依据”，不伪造输出。

## 示例二：用 CSE 观察 `DominanceInfo` 的行为

CSE 在 `runOnOperation()` 一开始就 `getAnalysis<DominanceInfo>()`（`CSE.cpp` 第 37 行）。所以 CSE 的合并行为就是支配信息的一个可观测投影。

准备两个函数：

```mlir
func.func @dom(%cond: i1, %a: i32) -> i32 {
  %0 = arith.addi %a, %a : i32
  cf.cond_br %cond, ^bb1, ^bb2
^bb1:
  %1 = arith.addi %a, %a : i32
  %2 = arith.addi %1, %0 : i32
  return %2 : i32
^bb2:
  return %0 : i32
}

func.func @noDom(%cond: i1, %a: i32) -> i32 {
  cf.cond_br %cond, ^bb1, ^bb2
^bb1:
  %0 = arith.addi %a, %a : i32
  cf.br ^bb3(%0 : i32)
^bb2:
  %1 = arith.addi %a, %a : i32
  cf.br ^bb3(%1 : i32)
^bb3(%r: i32):
  return %r : i32
}
```

跑：

```powershell
& "$mlir\mlir-opt.exe" dom.mlir --cse
```

实测：

```mlir
module {
  func.func @dom(%arg0: i1, %arg1: i32) -> i32 {
    %0 = arith.addi %arg1, %arg1 : i32
    cf.cond_br %arg0, ^bb1, ^bb2
  ^bb1:  // pred: ^bb0
    %1 = arith.addi %0, %0 : i32
    return %1 : i32
  ^bb2:  // pred: ^bb0
    return %0 : i32
  }
  func.func @noDom(%arg0: i1, %arg1: i32) -> i32 {
    cf.cond_br %arg0, ^bb1, ^bb2
  ^bb1:  // pred: ^bb0
    %0 = arith.addi %arg1, %arg1 : i32
    cf.br ^bb3(%0 : i32)
  ^bb2:  // pred: ^bb0
    %1 = arith.addi %arg1, %arg1 : i32
    cf.br ^bb3(%1 : i32)
  ^bb3(%2: i32):  // 2 preds: ^bb1, ^bb2
    return %2 : i32
  }
}
```

逐行读：

- `@dom` 里，`%0` 在入口块，支配 `^bb1`；`^bb1` 里的 `%1` 与它结构等价，于是被替换成 `%0`，下游的 `addi %1, %0` 变成 `addi %0, %0`。
- `@noDom` 里两处 `addi` 位于互不支配的分支，CSE **不能**把 `%1` 换成 `%0`，否则 `^bb2` 会引用只在 `^bb1` 路径上定义的值。

这就是支配分析的实际作用：**它不是“优化”，而是“允许优化的前提”**。没有它，CSE 只能做块内合并。

## 示例三：用 dump 与 verifier 观察 pass 边界

`--verify-each` 默认就是开的（Ch 5 已给源码：`MlirOptMain.cpp` 第 189–192 行，`cl::init(true)`）。它保证每个 pass 跑完、分析失效之后，IR 仍然合法。

先看一次带 dump 的 CSE：

```powershell
& "$mlir\mlir-opt.exe" dom.mlir `
  --pass-pipeline='builtin.module(func.func(cse))' `
  --mlir-print-ir-after-all --mlir-disable-threading 2> dump.txt
```

实测头与第一段：

```text
// -----// IR Dump After CSEPass: cse //----- //
func.func @dom(%arg0: i1, %arg1: i32) -> i32 {
  %0 = arith.addi %arg1, %arg1 : i32
  cf.cond_br %arg0, ^bb1, ^bb2
^bb1:  // pred: ^bb0
  %1 = arith.addi %0, %0 : i32
  return %1 : i32
^bb2:  // pred: ^bb0
  return %0 : i32
}

// -----// IR Dump After CSEPass: cse //----- //
func.func @noDom(%arg0: i1, %arg1: i32) -> i32 { ... }
```

dump 的位置在“pass 跑完 + 分析失效 + verifier 通过”之后。所以你看到的每一段，都是**已验证的、分析缓存已经按契约更新过的**状态。这就是为什么 dump 适合定位“哪一步开始不对”：它给的是每个 pass 的稳定快照。

再看 verifier 的报错形态。喂一段 `%a: i32`、`%b: i64` 却写 `arith.addi %a, %b : i32` 的 IR，加 `--verify-each` 跑：

```powershell
& "$mlir\mlir-opt.exe" bad.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' --verify-each
```

实测：

```text
bad.mlir:2:23: error: use of value '%b' expects different type than prior uses: 'i32' vs 'i64'
  %0 = arith.addi %a, %b : i32
                      ^
bad.mlir:1:25: note: prior use here
func.func @bad(%a: i32, %b: i64) -> i32 {
                        ^
```

这条诊断在 parse 阶段就出现了，还没轮到 pass。它说明的是 verifier 的**报错格式**：主诊断指向 use 点，`note` 指向 prior use，`^` 精确到列。同一个 verifier 会在每个 pass 之后再跑一遍；如果错误是某个 pass 引入的，你会看到同样的格式，只是位置指向被改坏的那一行。

> 本机是 Optimized build，没有 `-debug-only`，所以看不到 `Running pass ...` 或分析构造日志。要看这些，需要自建 assertions 版本（Ch 0 给了构建配方）。

## 对比与取舍

| 分析 | 回答的问题 | 数据结构 | 典型失效条件 | 典型消费者 |
|---|---|---|---|---|
| `DominanceInfo` | 谁一定先于谁执行 | 支配树 + 块内位置 | op/块移动、CFG 改变 | CSE、LICM、任何跨块替换 |
| `DataFlowSolver` | 这个值可能是什么 | 格 + worklist | 改 operand、删分支、折常量 | SCCP、常量传播、可达性 |
| `AliasAnalysis` | 两块内存会不会重叠 | 接口聚合 | 内存相关 op 变化 | bufferization、依赖分析 |

三个实践判断：

- **能用结构分析就别上数据流**。支配关系便宜且稳定；数据流贵且脆弱。
- **别名分析永远当保守结果用**。`MayAlias` 是常态，不要写“如果不是 MayAlias 就一定不重叠”的代码——反过来才对。
- **自定义分析优先实现 `isInvalidated`**，而不是让所有 pass 记住“要保留我”。前者把知识放在分析里，后者把知识散落在每个 pass 里。

## 踩雷集锦

1. **以为 `getAnalysis<T>()` 是纯函数。** 它可能构造、可能缓存、可能触发 instrumentation；同一个类型在同一 op 上只构造一次。

2. **在 pass 里存 `AnalysisT&` 成员。** pipeline 结束会 `am.clear()`；下一个 pass 拿到的是悬垂引用。需要跨 pass 的信息，应该在每个 pass 里重新 `getAnalysis`（缓存会命中，不会重算）。

3. **`markAllAnalysesPreserved()` 之后又改了 IR。** 除了分析错误，还会跳过这次 pass 之后的 verifier（`Pass.cpp` 第 639 行）。两个后果叠加，bug 会非常难查。

4. **把 `AliasAnalysis` 当“精确别名”。** 默认答案是 `MayAlias`；它的价值在于“能证明不重叠时返回 NoAlias”，而不是“总能给出确定答案”。

5. **在 Optimized build 里找 `-debug-only=analysis`。** 没有。用 dump + 消费者行为反推，或者自建 assertions 版本。

## 进阶：再往深一层

- **分析也可以有依赖**：`DataFlowSolver` 里的分析通过 `getOrCreateState` 互相引用；`AliasAnalysis` 的实现可以调用 `DataFlowSolver` 的结果。依赖关系是隐式的，所以失效必须保守。
- **不是所有分析都走 `DataFlowSolver`**。`Liveness` 就是传统算法：`Liveness(Operation *op)` 构造，按块给出 live-in/live-out；头文件注释明确写着 “uses a traditional algorithm”（`mlir/include/mlir/Analysis/Liveness.h` 第 9–17 行，类在第 47 行）。别看到“分析”就去找格。
- **`SliceAnalysis` 是 use-def 遍历**：`getBackwardSlice` / `getForwardSlice`（`mlir/include/mlir/Analysis/SliceAnalysis.h` 第 145–153 行），回答“谁定义了这个值 / 谁用了它”。它不建格、不跑不动点。

## 动手练习

**任务规格**：给定一段带两个分支的 IR，判断 CSE 会在哪里合并、哪里不合并；然后用 `DominanceInfo` 的 API 写一个 20 行以内的小 pass（guided，需要 C++ 环境），打印每个 `arith.addi` 的支配关系。

**测试用例（至少三个）**：

1. 入口块定义 + 后继块重复（应合并）。
2. 两个互斥分支各定义一次（不应合并）。
3. 循环体里定义、循环外重复（注意：循环头不支配循环体的情况）。

**分段步骤**：

1. 先只跑 `--cse`，记录输出。
2. 加 `--mlir-print-ir-after-all --mlir-disable-threading`，确认 dump 的数量与顺序。
3. 把 `@noDom` 的两个分支改成“一个分支嵌套在另一个的支配范围内”，再跑一次，观察合并发生。
4. （guided）用 `getAnalysis<DominanceInfo>()` 写 pass，对每对 `arith.addi` 调 `dominates(a, b)`，把结果 `llvm::errs()` 出来。

**参考解答**：

<details>
<summary>展开看关键结论</summary>

- 用例 1 合并：`%0` 支配 `%1`，CSE 把 `%1` 替换成 `%0`。
- 用例 2 不合并：`^bb1` 与 `^bb2` 互不支配，替换会破坏 SSA 作用域。
- 用例 3 取决于循环结构：循环头支配循环体，循环体不支配循环头；`scf.for` 的 region 还涉及 `IsolatedFromAbove` 之外的作用域规则，不能只看块。
- `dominates(a, b)` 对同一 op 返回 true（`a == b || properlyDominates`），写 pass 时要注意排除自己。

</details>

**另外三题**：

2. 把 `@dom` 的 `%0` 移到 `^bb1` 内部，确认 CSE 不再合并，并用支配关系解释。
3. 用 `--verify-each=false` 重跑示例三，观察输出是否变化；再解释为什么 verifier 默认要开。
4. 把 `CSE.cpp` 的 `markAnalysesPreserved<DominanceInfo>` 改成 `markAllAnalysesPreserved()`，说明为什么这是错的（guided，需改源码）。

**常见卡点**：

- 把 `--cse` 跑在 module 层：它会在每个 `IsolatedFromAbove` op 上跑，结果一样，但 dump 粒度不同。
- 忘记 `--mlir-disable-threading`，dump 顺序错乱，误判“CSE 先处理了 @noDom”。
- 用 `getCachedAnalysis` 拿不到就以为分析不存在；第一次要用 `getAnalysis`。

**自我检核**：你能解释“为什么 CSE 保留 DominanceInfo，却不保留常量格”吗？

## 本章重点整理

- 分析是 per-op 缓存，键是 `(op, TypeID)`；第一次 `getAnalysis` 才构造。
- `PreservedAnalyses` 默认空集 = 全部失效；`preserveAll` 是哨兵，`preserve<T>` 是精确保留。
- 失效从当前 op 向下递归；`isNone` 直接 `clear()`，部分保留要遍历子节点。
- `DominanceInfo` 是结构分析，失效条件是 op/块移动；CSE 删除冗余 op 后仍可保留它。
- `DataFlowSolver` 是格上的 worklist 不动点；SCCP 同时装 `DeadCodeAnalysis` 与 `SparseConstantPropagation`。
- `AliasAnalysis` 是接口聚合器，只回答 `alias` 与 `getModRef`，默认保守。
- Optimized build 没有 test pass；用消费者行为 + dump + verifier 观察。

## 自我检核

- [ ] 我能画出 `AnalysisManager` 与 IR 的树形对应关系
- [ ] 我能解释为什么分析默认“全部失效”而不是“全部保留”
- [ ] 我能说出 `markAllAnalysesPreserved()` 的两个副作用
- [ ] 我能解释 CSE 为什么只保留支配分析
- [ ] 我知道 `DominanceInfo`、`DataFlowSolver`、`AliasAnalysis` 各自的职责边界
- [ ] 我能用 dump 定位“哪一步开始不对”

## 延伸阅读

- **[MLIR 文档：Analysis Management](https://mlir.llvm.org/docs/PassManagement/#analysis-management)**
  - **读哪里**：Analysis Management 与 Preserving Analyses 两节。
  - **学什么**：官方对缓存、失效、保留集合的定义。
  - **和本章的关联**：本章的契约描述以此为准。

- **[mlir/include/mlir/Pass/AnalysisManager.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Pass/AnalysisManager.h)**
  - **读哪里**：第 30–130 行的 `PreservedAnalyses` / `AnalysisModel`，第 190–230 行的构造逻辑。
  - **学什么**：缓存与失效的真实实现。
  - **和本章的关联**：核心概念一、二的逐行出处。

- **[mlir/include/mlir/Analysis/DataFlowFramework.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Analysis/DataFlowFramework.h)**
  - **读哪里**：`DataFlowSolver`（第 324 行起）与 `DataFlowAnalysis`（第 603 行起）。
  - **学什么**：格、状态、worklist 的抽象。
  - **和本章的关联**：核心概念四的完整定义。

---

下一章把分析换成“改写”：pattern 怎么匹配、fold 和 canonicalize 怎么分工、为什么收敛条件决定了你写的 pattern 会不会死循环。

→ [Ch 7 RewritePattern 与 canonicalize](./07-patterns-and-canonicalize.md)