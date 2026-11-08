# Ch 09 — PassManager 与 analysis

> **目标**：把 `PassManager` 当成一个**有状态、多线程、带缓存**的执行引擎来读：知道 `pass-pipeline` 文本如何变成嵌套 `OpPassManager`、一次 pass 运行前后 analysis 何时失效、为什么 `--mlir-print-ir-module-scope` 会要求你先关线线程，以及“pass 跑了但什么都没变”通常不是 pass 的错。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，LLVM/MLIR `24.0.0git`，assertions ON（`-debug-only` 可用）。
>
> **前置**：Ch 04（Operation/Block/Region）、Ch 08（安全改 IR）；`mlir-opt` 能跑通 `-canonicalize`。

---

## 09.1 为什么维护者要读 PassManager

在 MLIR 的 issue 里，一大类“bug”最后都被归到 pass manager，而不是某个 pass 内部：

1. **“我的 pass 没跑”**。多数情况是锚点（anchor）不对：把 `func.func` 的 pass 放进了 `builtin.module` 的 pipeline，或者 `nest` 的层级少了一层。`mlir-opt` 会直接给你一条错误，但你得知道它来自哪里。
2. **“结果被回滚了 / 跑了两遍”**。`OpToOpPassAdaptor` 会为每个 `func.func` 各跑一遍 pipeline；`-debug-only=pass-manager` 能看到 `Collected 2 operations for async execution`。
3. **“analysis 过期，结果不对或直接崩”**。`getAnalysis<T>()` 拿到的可能是上一轮缓存的实例；pass 改完 IR 却没有正确声明 `preservedAnalyses`，下一次查询就拿到脏数据。
4. **“测试里输出顺序是乱的”**。默认多线程执行，`llvm::outs()` 的写入会交错；`--mlir-disable-threading` 解决的是**观测**问题，不是正确性问题。

所以这一章不教你怎么写 `runOnOperation`（Ch 08 已经做过），而是教你读**执行引擎**。维护者真正改 `mlir/lib/Pass/` 的时机，通常是：加 instrumentation、改 pipeline 解析、改 crash 恢复、改初始化代际（initialization generation）。

## 09.2 先建立直觉：三层结构与三个缓存

```text
mlir-opt -pass-pipeline='builtin.module(func.func(canonicalize,cse))'
        │
        ▼  PassPipelineCLParser::addToPipeline
  PassManager(anchor = "builtin.module")
        │
        ├── OpToOpPassAdaptor              ← 把嵌套 manager 变成“对每个子 op 跑一遍”
        │        └── OpPassManager("func.func")
        │                 ├── CanonicalizerPass
        │                 └── CSEPass
        │
        ▼  PassManager::run(op)                       [Pass.cpp:1031]
   ┌────────────────────────────────────────────────────────────┐
   │ 1. 检查 anchor 是否匹配 op->getName()                       │
   │ 2. getDependentDialects → appendDialectRegistry → load      │
   │ 3. finalizePassList：合并相邻 adaptor、校验每个 pass 可调度  │
   │ 4. initialize(generation+1)：只在 registry/pipeline 变时才做 │
   │ 5. 建 ModuleAnalysisManager                                  │
   │ 6. runPasses → OpToOpPassAdaptor::runPipeline                │
   │ 7. exitMultiThreadedExecution + 可选 dumpStatistics          │
   └────────────────────────────────────────────────────────────┘
        │
        ├── AnalysisManager 树（每个 op 一个 AnalysisMap）        ← 缓存 1
        ├── pass 的 initializationGeneration                     ← 缓存 2
        └── asyncExecutors（每线程一组 manager 副本）             ← 缓存 3
```

三个缓存对应三类经典故障：

| 缓存 | 存在哪 | 何时失效 | 典型症状 |
|---|---|---|---|
| analysis | `AnalysisMap`（`AnalysisManager.h:134`） | pass 结束后按 `preservedAnalyses` 失效 | 拿到旧 dominance/alias 结果 |
| pass 初始化 | `initializationGeneration` | `registryHash` 或 `pipelineKey` 变化 | 改选项后行为没变 |
| 线程 executor | `asyncExecutors` | manager 数量与首个 executor 不匹配时重建 | 加 pass 后输出顺序变了 |

注意最下面那条：`OpToOpPassAdaptor::runPipeline` 退出时会 `am.clear()`（`Pass.cpp:681`），**analysis 不会跨 pipeline 存活**。这条比“preserved”更强势：你在一个 `func.func(...)` 里算出的 analysis，到了下一个 `func.func(...)` 一定重新构造。

## 09.3 pass 类型、锚点与可调度性

维护者每天要判断的第一件事是“这个 pass 能挂在谁身上”。本树里四种写法：

| 写法 | 例子 | 能调度到 | 说明 |
|---|---|---|---|
| `OperationPass<ModuleOp>` | `-test-module-pass` | `builtin.module` | 最窄 |
| `OperationPass<func::FuncOp>` | `-test-function-pass` | `func.func` | 最常见 |
| `OperationPass<>`（op-agnostic） | `-test-dynamic-pipeline`、`-test-foo-analysis` | 任意 | 运行时 `dyn_cast` |
| `InterfacePass<FunctionOpInterface>` | `-test-interface-pass` | 任何实现该 interface 的 op | 见 Ch 12 |

锚点不匹配时，错误来自 `PassManager::run` 开头（`Pass.cpp:1040-1045`）。实测：

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='func.func(canonicalize)'
```

```text
/tmp/mw/ch09.mlir:1:1: error: can't run 'func.func' pass manager on 'builtin.module' op
module {
^
```

这句话里有两个信息：`func.func` 是 pipeline 的**期望锚点**，`builtin.module` 是 `mlir-opt` 实际交给 pass manager 的顶层 op（module）。修法不是加 `--allow-unregistered-dialect`，而是补上外层 `builtin.module(...)`。

第二类错误是“pass 类型对了但 manager 层级不对”，来自 `finalizePassList`（`Pass.cpp:306`、`:320`）：

```bash
$T/bin/mlir-opt /tmp/mw/ch12.mlir --pass-pipeline='builtin.module(test-interface-pass)'
```

```text
<unknown>:0: error: unable to schedule pass '{anonymous}::TestInterfacePass' on a PassManager intended to run on 'builtin.module'!
```

`{anonymous}::` 前缀说明这个 pass 没有导出符号，是测试 pass；真实 pass 会显示 `mlir::CanonicalizerPass` 这类名字。

## 09.4 `PassManager::run` 的七步，以及每一步的维护含义

`mlir/lib/Pass/Pass.cpp:1031` 起，`PassManager::run(Operation *op)` 做的事按顺序是：

1. **anchor 检查**（`:1041`）。`anchorOp && anchorOp != op->getName()` 直接报错。维护含义：`mlir-opt` 的顶层永远是 `builtin.module`，所以任何不带 `builtin.module` 外层的 pipeline 都会死在这里。
2. **注册 dependent dialects**（`:1049-1060`）。先 `getDependentDialects(registry)`，再 `context->appendDialectRegistry`，最后逐个 `getOrLoadDialect`。维护含义：pass 说自己依赖某个方言，但没在 `getDependentDialects` 里声明，就会在 `initialize` 里拿到 `nullptr`（多见 `getLoadedDialect` 返回 null 后 assert）。
3. **`finalizePassList`**（`:1063`）。合并相邻 adaptor（`Merging adjacent adaptors in pass list`，`:249`）、校验每个 pass 能否调度（`Verifying N passes for operation`，`:306`）。维护含义：你在 `addPass` 之后动态改 manager，必须重跑 finalize，否则拿到旧结构。
4. **初始化代际**（`:1072-1089`）。只有当 `context->getRegistryHash()` 或 `hash()` 变化时才 `initialize(context, generation+1)`，否则复用上一代。维护含义：**改了 pass 选项但没改 pipeline 文本时，`initialize` 可能不会重跑**；这就是“改选项没效果”的根因之一。
5. **建 `ModuleAnalysisManager`**（`:1091`）。注意命名：即使顶层不是 module，这个类名也这么叫。
6. **`runPasses`**（`:1122`）→ `OpToOpPassAdaptor::runPipeline`（`:663`）。真正的执行。
7. **`exitMultiThreadedExecution` + `dumpStatistics`**（`:1101-1107`）。`--mlir-pass-statistics` 的数据就是这里打的。

第 6 步里还有两个关键动作，写 pass 的人必须知道：

```cpp
// Pass.cpp:621-622
  // Invalidate any non preserved analyses.
  am.invalidate(pass->passState->preservedAnalyses);

// Pass.cpp:637-643
    //  1) If the pass said that it preserved all analyses then it can't have
    //     permuted the IR.
#ifndef EXPENSIVE_CHECKS
    runVerifierNow = !pass->passState->preservedAnalyses.isAll();
#endif
```

第二段是本章最容易被忽略的维护陷阱：**`markAllAnalysesPreserved()` 会连带跳过 verifier**。一个 pass 如果偷懒声明“我保留一切”，那么它写坏了 IR 也不会被当场抓住，而是在更晚的地方炸成别的问题。`Canonicalizer` 只在 `!changed` 时才这么做（`Canonicalizer.cpp:88-89`），这是正确用法。

## 09.5 pipeline 文本、嵌套与动态 pipeline

`-pass-pipeline` 的语法是“锚点(op 内 pipeline)”，可以任意嵌套：

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize,cse))'
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

`%c0`/`%c1` 消失了，说明两个 function 各自跑了 `canonicalize`。这里有个容易误读的点：`builtin.module(...)` 这一层**没有自己的 pass**，它只有一个 adaptor。`-debug-only=pass-manager` 会把这件事说得很清楚（见 09.9 实验 5）。

动态 pipeline 用 `-test-dynamic-pipeline`。注意本树它的选项名不是 `anchor`/`inner-pipeline`，而是 `dynamic-pipeline`/`op-name`；乱写会得到：

```text
<unknown>:0: error: <Pass-Options-Parser>: no such option anchor
<unknown>:0: error: failed to add `test-dynamic-pipeline` with options `anchor=func.func inner-pipeline=canonicalize`
```

正确用法需要一个实现了 `SymbolOpInterface` 的 op 作为 `op-name` 的筛选对象（`builtin.module` 本身不是 symbol）：

```bash
$T/bin/mlir-opt /tmp/mw/ch09dyn.mlir \
  --pass-pipeline='builtin.module(builtin.module(test-dynamic-pipeline{op-name=inner_add, dynamic-pipeline=func.func(canonicalize)}))' \
  --mlir-disable-threading
```

```text
Dynamic execute 'func.func(canonicalize)' on builtin.module
Dynamic execute 'func.func(canonicalize)' on builtin.module
dynamic-pipeline skip op name: inner_mul
module {
  module @inner_add {
    func.func @add(%arg0: i32, %arg1: i32) -> i32 {
      %0 = arith.addi %arg0, %arg1 : i32
      return %0 : i32
    }
  }
  module @inner_mul {
    func.func @mul(%arg0: i32, %arg1: i32) -> i32 {
      %c1_i32 = arith.constant 1 : i32
      %0 = arith.muli %arg0, %arg1 : i32
      %1 = arith.muli %0, %c1_i32 : i32
      return %1 : i32
    }
  }
}
```

`@inner_add` 里的 `%c0` 被消掉，`@inner_mul` 原样保留 —— 这就是“运行时决定跑不跑”的语义。维护含义：动态 pipeline 的筛选发生在**执行的每一层**，所以 `Dynamic execute` 打印了两次（外层 module 与内层 module 各一次）。

## 09.6 AnalysisManager：查、缓存、失效、保留

三个 API 决定一切（`mlir/include/mlir/Pass/AnalysisManager.h`）：

| API | 行号 | 行为 |
|---|---|---|
| `getAnalysis<T>()` | `:152`（`AnalysisMap::getAnalysis`） | 有缓存就复用，没有就构造 |
| `getCachedAnalysis<T>()` | `:170` | 只查不建，返回 `optional` |
| `invalidate(pa)` | `:198` | 按 `PreservedAnalyses` 逐个问“你是否失效” |

失效传播是**树形**的。`NestedAnalysisMap::invalidate`（`Pass.cpp:1168`）分三种情况：

- `pa.isAll()` → 直接返回，什么都不做。
- `pa.isNone()` → `childAnalyses.clear()`，整棵子树丢掉。
- 否则 → 深度优先遍历每个 child map，逐个 `invalidate(pa)`。

维护含义：`preserve<MyAnalysis>()` 只保护**当前 op 这一层**的同类型 analysis；子 op 的 analysis 仍然会按 `pa` 被清掉，除非它们也显式 preserve。很多人以为 preserve 是全局的，于是在嵌套 pipeline 里拿到重建后的新实例（地址变了、内部缓存的指针失效）。

`Pass::preserve` 的入口在 `Pass.h:248-259`：

```cpp
  /// Mark all analyses as preserved.
  void markAllAnalysesPreserved() {
    getPassState().preservedAnalyses.preserveAll();
  }
  /// Mark the provided analyses as preserved.
  template <typename... AnalysesT>
  void markAnalysesPreserved() {
    getPassState().preservedAnalyses.preserve<AnalysesT...>();
  }
```

最后一句维护建议：**没有基准测试就不要 preserve**。preserve 省下的时间通常远小于一次脏 analysis 的调试成本；而且如上所述，`preserveAll` 还会顺带跳过 verifier。

## 09.7 多线程执行与观测手段

`runOnOperationAsyncImpl`（`Pass.cpp:916`）会为每个线程复制一组 manager，然后按 op 派发。本机（32 线程）实测：

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --debug-only=pass-manager
```

```text
[pass-manager Pass.cpp:931 2] Creating 32 async executors
[pass-manager Pass.cpp:956 2] Collecting operations for async execution
[pass-manager Pass.cpp:982 2] Collected 2 operations for async execution
[pass-manager Pass.cpp:667 1] [pass-manager Pass.cpp:667 1] Running pipeline on operation 'Running pipeline on operation 'func.funcfunc.func  @@muladd((%arg0%: arg0i: 32i32, %, arg1%: arg1i: 32i)32 -> )i -> 32i32 {...}' with 1 passes, verifyPasses=1 pipeline: func.func(canonicalize {{...}cse-between-iterations' with =1false passes, verifyPasses= 1  pipeline:   func.func(max-iterations=canonicalize10 max-num-rewrites={-1cse-between-iterations= falseregion-simplify= normal  test-convergence = false max-iterationstop-down==10true} )max-num-rewrites
```

最后那行不是乱码，是**两个线程同时往 stderr 写**的结果。它不是 bug，但会让你误判“日志丢了”。加 `--mlir-disable-threading` 后同一命令的输出是串行、可读的（见实验 6）。

`--mlir-disable-threading` 的实现不在 pass manager 里，而在 `MLIRContext`（`MLIRContext.cpp:57-59` 定义、`:79` 查询）。所以它对**所有**工具生效，也是 lit 测试里最常见的“先关线程再比对输出”的原因。

另一个坑：`--mlir-print-ir-module-scope` 要求先关线程，否则：

```text
<unknown>:0: error: IR print for module scope can't be setup on a pass-manager without disabling multi-threading first.
```

原因是 module-scope 打印需要在每个 pass 前后抓整个顶层 op，这在并发执行下无法给出确定顺序。

## 09.8 源码落点

| 位置 | 内容 | 维护相关性 |
|---|---|---|
| `mlir/lib/Pass/Pass.cpp:549` | `OpToOpPassAdaptor::run(Pass *, Operation *, AnalysisManager, bool verifyPasses, unsigned)` | 单个 pass 的包装：先跑，再 invalidate，再 verify |
| `mlir/lib/Pass/Pass.cpp:621-622` | `am.invalidate(preservedAnalyses)` | analysis 失效唯一入口 |
| `mlir/lib/Pass/Pass.cpp:642` | `runVerifierNow = !preservedAnalyses.isAll()` | preserveAll 跳过 verifier |
| `mlir/lib/Pass/Pass.cpp:663` | `runPipeline` | 一个 pipeline 对一个 op；退出时 `am.clear()` |
| `mlir/lib/Pass/Pass.cpp:858` / `:916` | `runOnOperationImpl` / `runOnOperationAsyncImpl` | 同步/异步两条路径 |
| `mlir/lib/Pass/Pass.cpp:1031` | `PassManager::run` | 七步主流程 |
| `mlir/lib/Pass/Pass.cpp:1122` | `PassManager::runPasses` | 转发到 `runPipeline` |
| `mlir/lib/Pass/Pass.cpp:1168` | `NestedAnalysisMap::invalidate` | 三态失效传播 |
| `mlir/include/mlir/Pass/AnalysisManager.h:134/:152/:198` | `AnalysisMap` / `getAnalysis` / `invalidate` | 缓存实现 |
| `mlir/include/mlir/Pass/Pass.h:248-259` | `markAllAnalysesPreserved` / `markAnalysesPreserved` | pass 作者唯一需要调的保留 API |
| `mlir/lib/Pass/PassManagerOptions.cpp:24/:28/:36/:43/:47/:57` | crash reproducer、print-ir、module-scope、after-change | 全部 CLI 开关 |
| `mlir/lib/IR/MLIRContext.cpp:57-79` | `mlir-disable-threading` | 全局线程开关 |

核对命令：

```bash
grep -n 'PassManager::run(Operation\|PassManager::runPasses\|NestedAnalysisMap::invalidate' mlir/lib/Pass/Pass.cpp
grep -n 'getAnalysis(PassInstrumentor\|void invalidate(const PreservedAnalyses' mlir/include/mlir/Pass/AnalysisManager.h
grep -n 'markAllAnalysesPreserved\|markAnalysesPreserved' mlir/include/mlir/Pass/Pass.h
```

## 09.9 可跑实验

下面的输入文件在 `/tmp/mw/`，可复制。

```bash
cat > /tmp/mw/ch09.mlir <<'EOF'
module {
  func.func @add(%a: i32, %b: i32) -> i32 {
    %0 = arith.addi %a, %b : i32
    %c0 = arith.constant 0 : i32
    %1 = arith.addi %0, %c0 : i32
    return %1 : i32
  }
  func.func @mul(%a: i32, %b: i32) -> i32 {
    %c1 = arith.constant 1 : i32
    %0 = arith.muli %a, %b : i32
    %1 = arith.muli %0, %c1 : i32
    return %1 : i32
  }
}
EOF
```

### 实验 1：嵌套 pipeline 生效（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize,cse))'
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

### 实验 2：`--mlir-print-ir-after-all` 打的是**pass 作用域**的 IR（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --mlir-print-ir-after-all
```

```text
// -----// IR Dump After CanonicalizerPass: canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
func.func @add(%arg0: i32, %arg1: i32) -> i32 {
  %0 = arith.addi %arg0, %arg1 : i32
  return %0 : i32
}

// -----// IR Dump After CanonicalizerPass: canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
func.func @mul(%arg0: i32, %arg1: i32) -> i32 {
  %0 = arith.muli %arg0, %arg1 : i32
  return %0 : i32
}
```

两次 dump，每次只有**一个 function**：pass 是在 `func.func` 上跑的。要看 module，得加 `--mlir-print-ir-module-scope` 并关线程。

### 实验 3：`--mlir-print-ir-before=<pass-arg>` 只看指定 pass（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --mlir-print-ir-before=canonicalize
```

```text
// -----// IR Dump Before CanonicalizerPass: canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
func.func @add(%arg0: i32, %arg1: i32) -> i32 {
  %0 = arith.addi %arg0, %arg1 : i32
  %c0_i32 = arith.constant 0 : i32
  %1 = arith.addi %0, %c0_i32 : i32
  return %1 : i32
}
```

`<pass-arg>` 是 pass 的**命令行名**（`canonicalize`），不是 C++ 类名。写错会直接报错（实测）：

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --mlir-print-ir-before=CanonicalizerPass
```

```text
mlir-opt: for the --mlir-print-ir-before option: Cannot find option named 'CanonicalizerPass'!
```

所以看到这条错误时，问题在参数名，不在 pass 本身。

### 实验 4：module-scope 打印必须先关线程（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(cse))' \
  --mlir-print-ir-after-all --mlir-print-ir-module-scope
```

```text
<unknown>:0: error: IR print for module scope can't be setup on a pass-manager without disabling multi-threading first.
```

加上 `--mlir-disable-threading` 后：

```text
// -----// IR Dump After CSEPass: cse ('func.func' operation: @add) //----- //
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %c0_i32 = arith.constant 0 : i32
    %1 = arith.addi %0, %c0_i32 : i32
    return %1 : i32
  }
  func.func @mul(%arg0: i32, %arg1: i32) -> i32 { ... }
}
```

注意 dump 头里多了 `('func.func' operation: @add)`：module scope 只是把**内容**换成顶层 op，触发点仍然是那个 function pass。

### 实验 5：线程让 debug 日志交错（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --debug-only=pass-manager 2>&1 | head -25
```

关键三行（原文截取）：

```text
[pass-manager Pass.cpp:931 2] Creating 32 async executors
[pass-manager Pass.cpp:956 2] Collecting operations for async execution
[pass-manager Pass.cpp:982 2] Collected 2 operations for async execution
```

接着的两条 `Running pipeline on operation` 会互相穿插。`32` 来自本机线程池并发上限，换机器会变。

### 实验 6：关线程后日志可读（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize))' \
  --debug-only=pass-manager --mlir-disable-threading 2>&1 | tail -14
```

```text
[pass-manager Pass.cpp:730 4] Looking for pass manager that can handle operation 'func.func' among 1 managers
[pass-manager Pass.cpp:743 4] Found pass manager 'func.func' that can handle operation 'func.func'
[pass-manager Pass.cpp:885 2] Processing operation 'func.func @mul(%arg0: i32, %arg1: i32) -> i32 {...}' with pass manager 'func.func'
[pass-manager Pass.cpp:667 1] Running pipeline on operation 'func.func @mul(%arg0: i32, %arg1: i32) -> i32 {...}' with 1 passes, verifyPasses=1 pipeline: func.func(canonicalize{…})
[pass-manager Pass.cpp:552 1] Running pass 'CanonicalizerPass' on operation 'func.func @mul(%arg0: i32, %arg1: i32) -> i32 {...}' at loc("/tmp/mw/ch09.mlir":8:3)
[pass-manager Pass.cpp:902 1] Completed synchronous pass adaptor run, processed 2 operations
[pass-manager Pass.cpp:1109 2] PassManager run completed with result: success
```

`Completed synchronous pass adaptor run, processed 2 operations` 是“一个 adaptor 处理了两个 func”的直接证据。

### 实验 7：pass statistics 两种视图（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(canonicalize,cse))' \
  --mlir-pass-statistics
```

```text
===-------------------------------------------------------------------------===
                         ... Pass statistics report ...
===-------------------------------------------------------------------------===
'func.func' Pipeline
  CanonicalizerPass
  CSEPass
    (S) 0 num-cse'd - Number of operations CSE'd
    (S) 0 num-dce'd - Number of operations DCE'd
```

`0 num-cse'd` 是对的：`canonicalize` 已经把重复常量折掉了，CSE 无事可做。换成 `--mlir-pass-statistics-display=list` 会把同名 pass 合并：

```text
  CSEPass
    (S) 0 num-cse'd - Number of operations CSE'd
    (S) 0 num-dce'd - Number of operations DCE'd
```

### 实验 8：动态 pipeline 的 skip（实测）

见 09.5 的命令与输出；关键行是 `dynamic-pipeline skip op name: inner_mul`。

### 实验 9：analysis 只在一个 pipeline 内有效（源码依据，未实测运行时差异）

`runPipeline` 用 `llvm::scope_exit` 在退出时 `am.clear()`（`Pass.cpp:676-682`）。这条**没有可观测的 CLI 输出**，所以我把它标为“未实测运行时差异”，只给源码位置：

```bash
$T/bin/mlir-opt /tmp/mw/ch09.mlir --pass-pipeline='builtin.module(func.func(cse),func.func(cse))'
```

命令能跑（两个独立 `func.func` pipeline 各跑一次 CSE），但输出与你预期一致，无法从外部区分“重算”还是“复用”。要观测只能在 C++ 里打点，或读 `am.clear()` 的实现。

## 09.10 踩雷集锦

1. **错误直觉：`preserveAll()` 只是性能优化。** → 正确认识：它还让 `runVerifierNow = false`（`Pass.cpp:642`），等于跳过 IR 校验。写坏 IR 的 pass 加了这个会“成功”返回。
2. **错误直觉：analysis 在整次 `mlir-opt` 里只算一次。** → 正确认识：`runPipeline` 退出即 `am.clear()`；跨 pipeline 一定重算。
3. **错误直觉：`--mlir-print-ir-after-all` 打印整个 module。** → 正确认识：打印的是 pass 的作用域 op（通常是单个 `func.func`）；要 module 得加 `--mlir-print-ir-module-scope`，并且必须 `--mlir-disable-threading`。
4. **错误直觉：`-debug-only` 输出乱是日志 bug。** → 正确认识：默认多线程，`errs()` 交错；关线程再判断。
5. **错误直觉：改了 `Pass::Option` 就会重新 `initialize`。** → 正确认识：初始化按 `registryHash` + pipeline hash 判定；只改选项值、不改 pipeline 结构时可能复用旧代际（`Pass.cpp:1070-1085`）。
6. **错误直觉：`markAnalysesPreserved<MyAnalysis>()` 会保护子 op 的同名 analysis。** → 正确认识：失效是树形传播的（`Pass.cpp:1168`），preserve 只作用于当前层的 map；子层仍按 `pa` 处理。
7. **错误直觉：`-test-pass-manager` 存在。** → 正确认识：本树没有这个 pass。等价物是 `-test-module-pass`、`-test-function-pass`、`-test-interface-pass`、`-test-dynamic-pipeline`（`mlir/test/lib/Pass/TestPassManager.cpp`）。
8. **错误直觉：`builtin.module(...)` 里的第一个 op 名可以随便写。** → 正确认识：它就是锚点，必须等于 `op->getName()`；`mlir-opt` 顶层固定是 `builtin.module`。

## 09.11 本章重点整理

- `PassManager::run`（`Pass.cpp:1031`）是七步流程；anchor 检查、dependent dialect 加载、finalize、代际初始化、建 analysis manager、跑、收尾。
- `OpToOpPassAdaptor` 把嵌套 manager 变成“对每个匹配子 op 跑一遍”；`Collected 2 operations` 是它的行为指纹。
- analysis 的失效只有一条路径：`am.invalidate(preservedAnalyses)`（`Pass.cpp:621`），三态传播在 `NestedAnalysisMap::invalidate`。
- `preserveAll` 会跳过 verifier；只在“确实没改 IR”时用（`Canonicalizer` 是范本）。
- `am.clear()` 在每个 pipeline 结束时执行，analysis 不跨 pipeline 复用。
- 默认异步执行；`-debug-only` 与 `llvm::outs()` 的输出会交错，`--mlir-disable-threading` 是观测前提。
- `--mlir-print-ir-module-scope` 与多线程互斥，这是硬约束不是建议。

## 09.12 自我检核

- [ ] 能写出把 `canonicalize` 挂在两个 `func.func` 上的完整 `-pass-pipeline` 文本，并解释为什么需要外层 `builtin.module`
- [ ] 能说出 `PassManager::run` 七步中哪一步会加载 dependent dialect、哪一步会合并 adaptor
- [ ] 能解释 `preservedAnalyses.isAll()` 与 `runVerifierNow` 的关系，并说明为什么这是个维护陷阱
- [ ] 能复现 `--mlir-print-ir-module-scope` 的错误，并用 `--mlir-disable-threading` 修好
- [ ] 能读出 `-debug-only=pass-manager` 里的 `Creating 32 async executors` 和 `Collected N operations`
- [ ] 能解释 `-test-dynamic-pipeline` 的 `op-name` 为什么要求 `SymbolOpInterface`
- [ ] 能说出 analysis 在什么时候被清除、被保留、被重建
- [ ] 能判断一个“pass 没生效”的报错属于 anchor 错、调度错还是初始化代际问题

## 09.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/PassManagement.md`**
  - 读哪里：`## Analysis Management`（214 行起）、`### Preserving Analyses`（301 行起）、`## Dynamic Pass Pipelines`（459 行起）
  - 能学到什么：官方对“何时该 preserve”的表述，以及动态 pipeline 的设计意图
  - 和本章关联：09.6 与 09.5 的规范版本
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Pass/Pass.cpp`**
  - 读哪里：`549-700`（单 pass 包装 + runPipeline）、`1031-1129`（run/runPasses）
  - 能学到什么：verifier、invalidate、instrumentation 的确切顺序
  - 和本章关联：09.4 的每一步都在这里
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Pass/AnalysisManager.h`**
  - 读哪里：`120-215`（`AnalysisMap` 全貌）
  - 能学到什么：缓存 key 是 `TypeID`，失效靠 `AnalysisConcept::invalidate`
  - 和本章关联：09.6 的 API 表
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Pass/PassManagerOptions.cpp`**
  - 读哪里：全文 184 行
  - 能学到什么：所有 `-mlir-print-ir-*` / `-mlir-pass-*` 开关的真实名字与默认值
  - 和本章关联：09.7 与 09.9 的命令来源
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Pass/TestPassManager.cpp`**
  - 读哪里：`TestModulePass` / `TestFunctionPass` / `TestInterfacePass` 三个定义
  - 能学到什么：最小 pass 的写法，以及 interface pass 的调度约束
  - 和本章关联：09.3 的类型表、09.10 第 7 条

pass manager 保证“谁在什么时候跑”；下一章进入它最常驱动的引擎：`RewritePattern` 与 greedy driver —— 你写的 `canonicalize` 规则到底按什么顺序、按什么代价、在什么条件下收敛。

→ 下一章：[Ch 10 RewritePattern 与 canonicalize](./10-rewrite-patterns-and-canonicalize.md)
