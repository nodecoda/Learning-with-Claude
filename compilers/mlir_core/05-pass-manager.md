# Ch 5 PassManager 与 pass 管线

> **目标**：把 pass 从“一条命令”升级为“一棵带锚点、带选项、带失效契约的树”。学完本章，你看到任何 `--pass-pipeline` 都能说出三件事：每个 pass 锚在哪一层、它能不能放在那里、它跑完之后哪些分析会失效。

## 为什么需要这个？

前面四章我们一直在“看 IR”，从这一章开始要“改 IR”。直接写一个大函数读文件、遍历、替换、写回，玩具项目可以，真实编译器不行，原因有三个：

1. **变换要能组合**。你希望 `canonicalize` 之后接 `cse`，再接 `convert-scf-to-cf`，顺序可换、可裁剪、可复用。硬编码的遍历做不到。
2. **变换要能分层**。函数级变换只该看一个函数，模块级变换才该看符号表。层级错了，要么看不到想改的 op，要么把整个模块拖进一次遍历。
3. **变换要能共享分析**。支配树、别名信息、常量格，算一次很贵。如果每个 pass 各算一遍，pipeline 会慢得离谱；如果缓存了却不失效，结果会错得离谱。

PassManager 就是为这三件事存在的。它不是“pass 的数组”，而是**一棵与被处理 IR 同构的树**：树的每个节点绑定一种 op（锚点），节点里挂一串 pass，节点下面还能再挂子节点。

## 先建立直觉：pipeline 是一棵锚点树

先看一条真实 pipeline：

```text
builtin.module(
  func.func(
    convert-scf-to-cf,
    canonicalize
  ),
  convert-func-to-llvm
)
```

它的结构不是“四个 pass 依次跑”，而是：

```text
PassManager @ builtin.module
├── OpPassManager @ func.func
│   ├── convert-scf-to-cf      （每个函数跑一次）
│   └── canonicalize           （每个函数跑一次）
└── convert-func-to-llvm       （整个模块跑一次）
```

三个直接后果：

- `convert-scf-to-cf` 会被调用 **N 次**（N = 函数个数），而不是一次。它拿到的 `getOperation()` 是单个 `func.func`。
- `convert-func-to-llvm` 只被调用一次，拿到的是 `builtin.module`。
- 两个函数之间没有顺序保证：函数级 pass 默认并行跑。这一点后面用 dump 顺序验证。

所以读 pipeline 的第一件事，是数括号：**每一层括号就是一个锚点**。括号写错，pass 要么报错，要么在错误的粒度上跑。

## 核心概念一：Pass 只有一种泛型，三种锚点

老教程常说 `ModulePass`、`FuncPass`、`OperationPass` 三兄弟。**在 MLIR 23.1.4 的 C++ API 里，前两个已经不存在了**：全树搜索 `ModulePass` 只在 C API 注释里留了一个词，`FuncPass` 一次都没出现。现代写法只有一个模板：

```cpp
template <typename OpT = void>
class OperationPass : public Pass;
```

按 `OpT` 分三种用法：

| 写法 | 锚点 | 能调度到 | 典型例子 |
|---|---|---|---|
| `OperationPass<ModuleOp>` | `builtin.module` | 只此一种 | `convert-func-to-llvm`、`convert-cf-to-llvm` |
| `OperationPass<func::FuncOp>` | `func.func` | 只此一种 | `affine-loop-invariant-code-motion` |
| `OperationPass<void>` | 任意 op | 任何 `IsolatedFromAbove` op | `canonicalize`、`cse` |

源码依据：`mlir/include/mlir/Pass/Pass.h` 第 367 行定义 `OperationPass`，第 384 行的 `canScheduleOn` 用 `opName == OpT::getOperationName()` 做精确匹配；第 412 行是 `OperationPass<void>` 的特化，第 425 行对任意已注册 op 返回 true。

TableGen 里的 `Pass<"name", "ModuleOp">` 最终就是展开成 `OperationPass<ModuleOp>`（`mlir/include/mlir/Pass/PassBase.td` 第 95–96 行）。这也解释了为什么 pass 的锚点在 `.td` 文件里一眼可见：

```tablegen
def ConvertControlFlowToLLVMPass : Pass<"convert-cf-to-llvm", "ModuleOp"> {
def AffineLoopInvariantCodeMotion
    : Pass<"affine-loop-invariant-code-motion", "func::FuncOp"> {
```

（`mlir/include/mlir/Conversion/Passes.td` 第 384 行；`mlir/include/mlir/Dialect/Affine/Transforms/Passes.td` 第 180–181 行。）

记住这张表，后面两个报错就能一眼读懂。

## 核心概念二：两种嵌套模式，决定了你该怎么写命令

`OpPassManager` 有两种 nesting（`mlir/include/mlir/Pass/PassManager.h` 第 49–57 行）：

- **Explicit**（显式）：加进来的 pass 必须支持当前锚点，否则直接报错。`--pass-pipeline` 用的就是这种。
- **Implicit**（隐式）：加进来的 pass 锚点不匹配时，自动为它开一个子 pass manager。`mlir-opt` 顶层那些 `--canonicalize`、`--cse` 老式开关走的是这种。

`mlir-opt` 构造主 PassManager 时写死了 `Nesting::Implicit`（`mlir/lib/Tools/mlir-opt/MlirOptMain.cpp` 第 585 行）：

```cpp
PassManager pm(op.get()->getName(), PassManager::Nesting::Implicit);
```

这不是实现细节，而是你天天会遇到的行为差异。同一个 func 级 pass：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"

# 写法 A：老式开关，隐式嵌套，能跑
& "$mlir\mlir-opt.exe" basic.mlir --affine-loop-invariant-code-motion

# 写法 B：显式 pipeline，没写 func.func，直接报错
& "$mlir\mlir-opt.exe" basic.mlir --pass-pipeline='builtin.module(affine-loop-invariant-code-motion)'

# 写法 C：显式 pipeline，写对层级，能跑
& "$mlir\mlir-opt.exe" basic.mlir --pass-pipeline='builtin.module(func.func(affine-loop-invariant-code-motion))'
```

实测：写法 A 和 C 正常输出；写法 B 得到：

```text
<unknown>:0: error: Can't add pass 'AffineLoopInvariantCodeMotion' restricted to 'func.func' on a PassManager intended to run on 'builtin.module', did you intend to nest?
failed to add `affine-loop-invariant-code-motion` with options ``
```

这句报错值得背下来。它同时告诉你三件事：pass 的**类名**（`AffineLoopInvariantCodeMotion`）、它的**锚点**（`func.func`）、当前 pass manager 的**锚点**（`builtin.module`），最后还给了修法（nest）。

## 核心概念三：pass 选项是文本协议的一部分

每个 pass 可以声明选项，命令行用花括号传：`canonicalize{max-iterations=3 top-down=false}`。两种写法语义相同：

```powershell
# 老式开关：选项用 = 连接
& "$mlir\mlir-opt.exe" canon.mlir --canonicalize="max-iterations=3"

# 显式 pipeline：选项跟着 pass 名写
& "$mlir\mlir-opt.exe" canon.mlir --pass-pipeline='builtin.module(func.func(canonicalize{max-iterations=3}))'
```

`--canonicalize{max-iterations=3}` 不合法，会被当成带花括号的命令行参数：

```text
mlir-opt.exe: Unknown command line argument '--canonicalize{max-iterations=1}'.
```

花括号只在 `--pass-pipeline` 的文本语法里有效。

### 让工具把 pipeline 归一化给你看

```powershell
& "$mlir\mlir-opt.exe" basic.mlir `
  --pass-pipeline='builtin.module(func.func(canonicalize,cse),canonicalize{max-iterations=3})' `
  --dump-pass-pipeline
```

实测（stderr，只留 pipeline 部分）：

```text
Pass Manager with 2 passes:
builtin.module(
  func.func(
    canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true},
    cse
  ),
  canonicalize{cse-between-iterations=false    max-iterations=3 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true}
)
```

三个观察：

1. 没写的选项也被展开成默认值，这是确认“我到底跑的是什么”的最快方式。
2. 两层 `canonicalize` 各有一份选项，互不影响。
3. `Pass Manager with 2 passes` 数的是顶层 pass（`func.func` 适配器算一个），不是总 pass 数。

写错选项名或 pass 名，报错层次不同：

```text
<unknown>:0: error: <Pass-Options-Parser>: no such option max-iteration
<unknown>:0: error: MLIR Textual PassPipeline Parser:1:11: error: 'canonicalze' does not refer to a registered pass or pass pipeline
```

前者是 pass 的选项解析器，后者是 pipeline 解析器。看到 `no such option` 先查拼写，看到 `does not refer to` 先查 pass 是否注册。

## 核心概念四：分析失效是 pass 的契约，不是框架的魔法

Pass 通过 `getAnalysis<T>()` 拿分析结果。第一次调用会**计算并缓存**，之后的调用直接返回缓存（`mlir/include/mlir/Pass/AnalysisManager.h` 第 195–215 行：`analyses.find(id)`，没找到才 `constructAnalysis` 并插入）。

缓存带来的问题是：pass 改了 IR，缓存就可能是错的。框架不会自动知道你改了什么，所以每个 pass 跑完后，PassManager 会做一件事：

```cpp
// Invalidate any non preserved analyses.
am.invalidate(pass->passState->preservedAnalyses);
```

（`mlir/lib/Pass/Pass.cpp` 第 618–619 行。）

关键在于 `preservedAnalyses` 的默认值：**空集**。也就是说，除非 pass 主动声明“我保留了某某分析”，否则**这个 op 上的所有分析全部作废**。这是一个非常保守、也非常安全的默认值。

pass 的声明方式有三种（`mlir/include/mlir/Pass/Pass.h` 第 249–260 行）：

```cpp
markAllAnalysesPreserved();                 // 我什么都没改，或者改了但不影响任何分析
markAnalysesPreserved<DominanceInfo>();     // 只保留指定分析
// 什么都不写 = 全部失效
```

真实的例子是 CSE（`mlir/lib/Transforms/CSE.cpp` 第 49–56 行）：

```cpp
// If there was no change to the IR, we mark all analyses as preserved.
if (!changed)
  return markAllAnalysesPreserved();

// We only delete redundant operations without moving any operation to a
// different block, so the dominance tree structure remains unchanged and
// DominanceInfo/PostDominanceInfo can be safely preserved.
markAnalysesPreserved<DominanceInfo, PostDominanceInfo>();
```

这段代码把“契约”讲得比任何文档都清楚：CSE 只删冗余 op、不移动 op，所以支配树不变，支配信息可以保留；但其它分析（比如常量格）可能因为删 op 而变化，所以不能 `preserveAll`。

失效的传播是分层的：`func.func` 上的 pass 只影响这个函数及其子节点；但如果保留集是空集，子节点缓存会被直接 `clear()`（`Pass.cpp` 第 1183–1185 行）。

还有一条生命周期规则：**嵌套 pipeline 跑完会 `am.clear()`**（`Pass.cpp` 第 673–679 行），注释说得很直白——“这些分析不会再用了，清掉可以降低内存占用”。所以 **analysis 引用只在当前 pass 的 `runOnOperation()` 里有效**；存进成员变量、传给下一个 pass 都是悬垂引用。

## 底层机制：一个 pass 的六步生命周期

把 `OpToOpPassAdaptor::run` 拆开看（`mlir/lib/Pass/Pass.cpp` 第 546–657 行），一个 pass 的执行是严格的六步：

```text
1. 检查 op 已注册            (553–557)
2. 检查 op 是 IsolatedFromAbove (558–561)
3. 检查 pass 能调度到该 op     (562–565)
4. 建 PassState，跑 runOnOperation (596–616)
5. 失效未保留的分析            (618–619)
6. 按需跑 verifier            (621–643)
```

第 2 步值得单独说：**pass 只能锚在 `IsolatedFromAbove` 的 op 上**。`func.func`、`builtin.module`、`gpu.module` 都有这个 trait；`scf.for`、`arith.addi` 没有。所以你不能写 `scf.for(canonicalize)`——不是语法不支持，而是 SSA 作用域不封闭，pass 看不到外部的定义。

第 6 步的 verifier 有一个优化，很多人不知道：

```cpp
// If the pass said that it preserved all analyses then it can't have
// permuted the IR.
#ifndef EXPENSIVE_CHECKS
runVerifierNow = !pass->passState->preservedAnalyses.isAll();
#endif
```

（`Pass.cpp` 第 631–640 行。）

也就是说：**`markAllAnalysesPreserved()` 不只是省分析，还会顺手跳过这次 pass 之后的 verifier**。在 `EXPENSIVE_CHECKS` 构建里这个优化被关掉。这是一个“契约换性能”的设计：你声明没改 IR，框架就信你，连验证都省了。声明错了，代价是错误可能延迟到很后面才暴露。

`--verify-each` 控制的就是第 6 步。它在 `mlir-opt` 里默认是 **true**（`mlir/lib/Tools/mlir-opt/MlirOptMain.cpp` 第 189–192 行，`cl::init(true)`），并通过 `pm.enableVerifier(...)` 传给 PassManager（同文件第 586 行）。PassManager 自己的默认值也是 true（`Pass.cpp` 第 1023 行）。

所以日常跑 `mlir-opt` 时，每个 pass 后面都已经验证过了。`--verify-each=false` 只在两种场景有意义：极度在意编译时间，或者在定位“IR 是在哪一步变坏的”——关掉它，让坏 IR 活到下一个 pass 再炸。

## 示例一：把 pass 放错层，三种报错方向

准备一段带 `cf` 的 IR：

```mlir
func.func @cond(%a: i32, %b: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %p = arith.cmpi sgt, %a, %c0 : i32
  cf.cond_br %p, ^bb1, ^bb2
^bb1:
  return %a : i32
^bb2:
  return %b : i32
}
```

### 方向一：module 锚点的 pass 放在 module 层（正确）

```powershell
& "$mlir\mlir-opt.exe" cf.mlir --pass-pipeline='builtin.module(convert-cf-to-llvm)'
```

实测：

```mlir
module {
  func.func @cond(%arg0: i32, %arg1: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %0 = arith.cmpi sgt, %arg0, %c0_i32 : i32
    llvm.cond_br %0, ^bb1, ^bb2
  ^bb1:  // pred: ^bb0
    return %arg0 : i32
  ^bb2:  // pred: ^bb0
    return %arg1 : i32
  }
}
```

两个容易误读的点：pass 锚在 `builtin.module`，却照样改到了函数体里的 `cf.cond_br`——**锚点决定“从哪开始遍历”，不决定“只能改哪一层”**；`func.func` 和 `func.return` 还在，因为这一步只降 `cf`。

### 方向二：module 锚点的 pass 放进 func 层（报错）

```powershell
& "$mlir\mlir-opt.exe" cf.mlir --pass-pipeline='builtin.module(func.func(convert-cf-to-llvm))'
```

实测：

```text
<unknown>:0: error: Can't add pass 'ConvertControlFlowToLLVMPass' restricted to 'builtin.module' on a PassManager intended to run on 'func.func', did you intend to nest?
failed to add `convert-cf-to-llvm` with options ``
failed to add `func.func` with options `` to inner pipeline
```

这就是“放错层”的标准形态。`did you intend to nest?` 是反问：它在说“你是不是把方向搞反了”。

### 方向三：func 锚点的 pass 放在 module 层（也报错）

```powershell
& "$mlir\mlir-opt.exe" basic.mlir --pass-pipeline='builtin.module(affine-loop-invariant-code-motion)'
```

实测：

```text
<unknown>:0: error: Can't add pass 'AffineLoopInvariantCodeMotion' restricted to 'func.func' on a PassManager intended to run on 'builtin.module', did you intend to nest?
failed to add `affine-loop-invariant-code-motion` with options ``
```

三次报错合起来看，规则就很清楚了：

| pass 锚点 | 放在 `builtin.module(...)` | 放在 `func.func(...)` |
|---|---|---|
| `ModuleOp` | 可以 | 报错（方向二） |
| `func::FuncOp` | 报错（方向三） | 可以 |
| `void`（任意 op） | 可以 | 可以 |

`convert-func-to-llvm` 也是 `ModuleOp` 锚点，写进 `func.func(...)` 会得到同一类错误（类名换成 `ConvertFuncToLLVMPass`）。

## 示例二：`--mlir-print-ir-after-all` 的 dump 头怎么读

```powershell
& "$mlir\mlir-opt.exe" basic.mlir `
  --pass-pipeline='builtin.module(func.func(convert-scf-to-cf,canonicalize),convert-func-to-llvm)' `
  --mlir-print-ir-after-all 2> dump.txt > out.txt
```

`dump.txt` 里五个 dump 头（单独抽出）：

```text
// -----// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //----- //
// -----// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //----- //
// -----// IR Dump After CanonicalizerPass: canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
// -----// IR Dump After CanonicalizerPass: canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
// -----// IR Dump After ConvertFuncToLLVMPass: convert-func-to-llvm{index-bitwidth=0 use-bare-ptr-memref-call-conv=false} //----- //
```

五个头 = 两个函数 × 两个 func 级 pass + 一个模块级 pass。每个头包含三段信息：

头里有 C++ 类名（`SCFToControlFlowPass`…）、命令行名（`convert-scf-to-cf`…）和完整选项串（连默认值都打出来）。

dump 的**粒度等于锚点**。func 级 pass 没有 `module` 外壳（节选）：

```text
// -----// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //----- //
func.func @loop(%arg0: index) -> i32 {
  ...
  cf.br ^bb1(%c0, %c0_i32 : index, i32)
^bb1(%0: index, %1: i32):  // 2 preds: ^bb0, ^bb2
  %2 = arith.cmpi slt, %0, %arg0 : index
  cf.cond_br %2, ^bb2, ^bb3
}
```

模块级 pass 的 dump 带 `module`，而且能看到“函数签名已降、函数体还没降”的混合态：

```text
// -----// IR Dump After ConvertFuncToLLVMPass: convert-func-to-llvm{index-bitwidth=0 use-bare-ptr-memref-call-conv=false} //----- //
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
  ...
}
```

这个 `llvm.func` 里还留着 `arith.addi`，不是 bug：`convert-func-to-llvm` 只负责签名和 `func.return`。

最后一条：**dump 顺序不是执行顺序，也不稳定**。同一份三函数 IR（源文件顺序 `@fold`、`@addzero`、`@mulone`），同一条命令连跑六次，func 级 pass 的 dump 头顺序出现了四种：`addzero,mulone,fold`、`fold,addzero,mulone`、`mulone,addzero,fold`、`addzero,fold,mulone`——函数之间是并行调度的，谁先跑完谁先打印。要稳定复现源顺序得加 `--mlir-disable-threading`（实测此时恒为 `fold,addzero,mulone`）；凡是要拿 dump 顺序做断言，先关线程。

> 想让每个 pass 都打完整模块，用 `--mlir-print-ir-module-scope`；它要求先加 `--mlir-disable-threading`，否则报 “IR print for module scope can't be setup ... without disabling multi-threading first”。

## 对比与取舍

| 写法 | 嵌套模式 | 适合 | 代价 |
|---|---|---|---|
| `--canonicalize` 等老式开关 | Implicit | 快速试验单 pass | 多个开关的先后顺序按命令行顺序拼，复杂 pipeline 不可读 |
| `--pass-pipeline='...'` | Explicit | 复现、文档、CI | 必须手写 `func.func(...)` 层级 |
| C++ 里 `pm.nest<func::FuncOp>()` | 由构造参数决定 | 嵌入式编译器 | 需要自己管 PassManager 生命周期 |
| `Pass::runPipeline` | 动态 | 一个 pass 内部按需跑子 pipeline | 只能跑当前 op 的子节点 |

三个实践建议：

1. **写文档、写测试、报 bug 一律用 `--pass-pipeline`**。老式开关的展开顺序依赖命令行顺序，别人很难复现。
2. **调 pass 参数用 `--dump-pass-pipeline` 自检**。它会把你没写的默认值也打出来，避免“我以为默认是 X”。
3. **看 IR 变化用 `--mlir-print-ir-after-all`，不要靠猜**。dump 头里的选项串是免费的“运行时配置快照”。

## 踩雷集锦

1. **以为 `convert-cf-to-llvm` 是 func 级 pass。** 它是 `ModuleOp` 锚点（`Conversion/Passes.td` 第 384 行），放进 `func.func(...)` 会直接报错。名字里带 dialect 不代表锚点就在函数上。

2. **把 `markAllAnalysesPreserved()` 当无副作用的礼貌声明。** 它会让框架跳过这次 pass 之后的 verifier（`Pass.cpp` 第 639 行）。声明错了，坏 IR 会活到后面才炸。

3. **跨 pass 保存 analysis 引用。** `am.clear()` 会在嵌套 pipeline 结束时清缓存（`Pass.cpp` 第 673–679 行）。成员变量里存 `DominanceInfo&` 是悬垂引用，典型症状是“Debug 正常、Release 随机崩”。

4. **用 `--mlir-print-ir-module-scope` 却没关线程。** 直接报 “can't be setup ... without disabling multi-threading first”。关掉之后每个函数都会打一份完整模块，输出量按“函数数 × pass 数”翻倍，先重定向到文件再看。

5. **把 dump 顺序当执行顺序。** 函数级 pass 并行跑，dump 顺序不稳定。要稳定顺序就 `--mlir-disable-threading`。


## 进阶：再往深一层

- **PassInstrumentation**：dump、timing、statistics 都是 instrumentation，不是 PassManager 的特判。你可以在 `runBeforePass` / `runAfterPass` 里插自己的 hook（`Pass.cpp` 第 1199 行之后）。Ch 10 会用它做自定义追踪。
- **并行 adaptor**：`OpToOpPassAdaptor::runOnOperationAsyncImpl`（`Pass.cpp` 第 913 行）按 op 切任务。它也是“analysis manager 必须 per-op”的根本原因。
- **动态 pipeline**：`Pass::runPipeline` 允许在 pass 内部对当前 op 的子树跑一段 pipeline，适合“先 inline 再规范化”这种条件式组合（`Pass.cpp` 第 572–595 行）。

## 动手练习

**任务规格**：写一个 `mix.mlir`，含两个函数（一个纯 `arith`，一个带 `scf.for`）；用三种 pipeline 跑它，解释差异。每条命令都要能复现，输出贴进答案。

**测试用例（至少三个）**：

```mlir
func.func @pure(%a: i32, %b: i32) -> i32 {
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
func.func @loop(%n: index) -> i32 {
  %c0 = arith.constant 0 : i32
  %c1 = arith.constant 1 : i32
  %z = arith.constant 0 : index
  %one = arith.constant 1 : index
  %r = scf.for %i = %z to %n step %one iter_args(%acc = %c0) -> (i32) {
    %next = arith.addi %acc, %c1 : i32
    scf.yield %next : i32
  }
  return %r : i32
}
// case 3: 把 convert-func-to-llvm 写进 func.func(...)，预期报错
```

**分段步骤**：

1. 用 `--dump-pass-pipeline` 确认 pipeline 被解析成什么。
2. 跑 `builtin.module(func.func(canonicalize))`，记录 dump 头数量。
3. 跑 `builtin.module(func.func(canonicalize,cse))`，比较头数量与顺序。
4. 故意制造一次层级错误，抄下完整报错。
5. 加 `--mlir-disable-threading` 重跑第 2 步，比较顺序。

**参考解答**：

<details>
<summary>展开看关键结论</summary>

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" mix.mlir `
  --pass-pipeline='builtin.module(func.func(canonicalize,cse))' `
  --mlir-print-ir-after-all --mlir-disable-threading 2> dump.txt
```

- dump 头数量 = 2 个 pass × 2 个函数 = 4。
- 关线程后顺序是 `@pure` → `@loop`，与源文件一致。
- 层级错误关键字：`restricted to 'builtin.module' ... intended to run on 'func.func'`。

</details>

**另外三题**：

2. 把 `canonicalize` 从 `func.func` 层提到 `builtin.module` 层，记录 dump 头数量的变化，并解释为什么少了。
3. 给 `convert-scf-to-cf` 传一个不存在的选项，抄下报错；再对比拼错 pass 名的报错。
4. 用 `--mlir-print-ir-after-all --mlir-print-ir-module-scope --mlir-disable-threading` 跑单函数文件，确认头里的 `('func.func' operation: @...)` 后缀。

**常见卡点**：忘了 `2>`（dump 走 stderr）；用 `Get-Content` 看长行时被终端折行误导，改用 `[System.IO.File]::ReadAllLines()`。

**自我检核**：你的答案要能指出“哪个 pass 在哪个 op 上跑了几次”，而不只是“输出了什么”。

## 本章重点整理

- Pass 只有 `OperationPass<OpT>` 一种泛型；`ModulePass` / `FuncPass` 是旧称，23.1.4 里没有这两个类型。
- 锚点决定 pass 能挂在哪一层，也决定 `runOnOperation()` 被调用几次。
- `--pass-pipeline` 是显式嵌套，必须自己写 `func.func(...)`；老式开关是隐式嵌套。
- pass 选项：pipeline 里用 `{k=v}`，老式开关用 `--pass="k=v"`。
- 分析默认全部失效；`markAllAnalysesPreserved()` / `markAnalysesPreserved<T...>()` 是显式契约。
- verifier 默认每个 pass 后都跑；`preservedAnalyses.isAll()` 会让它跳过。
- dump 头 = 类名 + 命令行名 + 完整选项串；粒度等于锚点。
- 函数级 pass 并行跑，dump 顺序不稳定。

## 自我检核

- [ ] 我能不看源码，说出 `OperationPass<ModuleOp>` 和 `OperationPass<void>` 的区别
- [ ] 我能解释为什么 `builtin.module(func.func(convert-cf-to-llvm))` 会报错
- [ ] 我知道 `--pass-pipeline` 和 `--canonicalize` 的嵌套模式不同
- [ ] 我能读懂 dump 头里的三段信息
- [ ] 我能说出 `markAllAnalysesPreserved()` 的两个作用
- [ ] 我知道 analysis 引用为什么不能跨 pass 保存

## 延伸阅读

- **[MLIR 文档：Pass Infrastructure](https://mlir.llvm.org/docs/PassManagement/)**
  - **读哪里**：Pass Manager、OpPassManager、Pass 注册与选项三节。
  - **学什么**：官方对锚点、嵌套、选项的规范定义。
  - **和本章的关联**：本章的报错与语法都能在这篇文档里找到出处。

- **[mlir/include/mlir/Pass/PassManager.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Pass/PassManager.h)**
  - **读哪里**：第 46–120 行的 `OpPassManager`，第 232–260 行的 `PassManager`。
  - **学什么**：`nest`、`addPass`、`run` 的真实签名与注释。
  - **和本章的关联**：显式/隐式嵌套的定义就在这里。

- **[mlir/lib/Pass/Pass.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Pass/Pass.cpp)**
  - **读哪里**：第 546–700 行的 `run/runPipeline`，第 1172–1198 行的失效传播。
  - **学什么**：一个 pass 的六步生命周期，以及分析失效如何递归。
  - **和本章的关联**：本章“底层机制”的逐行出处。

- **[mlir/include/mlir/Pass/AnalysisManager.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Pass/AnalysisManager.h)**
  - **读哪里**：`PreservedAnalyses`（第 30–79 行）与 `getAnalysisImpl`（第 195–215 行）。
  - **学什么**：缓存键、保留集合、自定义失效钩子。
  - **和本章的关联**：Ch 6 会把这里的每个概念展开。

---

下一章把“分析失效”这条线单独拎出来：分析结果到底长什么样、谁拥有它、什么时候该重算。

→ [Ch 6 分析框架与缓存](./06-analysis-framework.md)