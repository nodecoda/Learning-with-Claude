# Ch 13 — New Pass Manager：`PassBuilder`、`AnalysisManager`、pipeline 文本

> **目标**：把 `opt -passes='...'` 从一个黑盒开关拆成四层可检查的对象：pass、pass manager、analysis manager、pipeline parser。学完你能在 review 里判断「这个 pass 能不能放在这个 pipeline 层级」「这个 analysis 为什么会被复用」「这条 `-passes` 文本实际展开成什么」，并知道报错来自哪一层。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验构建 `/home/ubuntu/llvm/builds/pr-lane`（`RelWithDebInfo`，assertions ON）。本章所有 `opt` 输出都在这个构建上真实运行。

## 为什么需要这个？

读一个 pass 的 PR，最先要回答的不是「算法对不对」，而是「它站在哪个 IR 层级上」。同样一段逻辑，写成 `FunctionPass` 还是 `ModulePass`，决定了它能不能看到别的函数、能不能安全地并行、analysis 结果按什么粒度缓存。New Pass Manager（New PM）把这几个问题编码进类型：`ModulePassManager` 的 `addPass` 只接受 Module 级 pass，`FunctionPassManager` 只接受 Function 级 pass；function pass 由 parser 自动套 adaptor，跨层 analysis 访问必须经过 proxy。

第二件要回答的是「这条 pipeline 文本到底解析成了什么」。review 里常见的误判是：

- 看到 `module(instcombine)` 就认为写错了，因为 `instcombine` 是 function pass。
- 看到 `function(globalopt)` 报错，就以为 `globalopt` 不存在。
- 跑完 `-passes=instcombine` 看到最后有 `verify`，以为是 pass 自己加的。

这三个判断都能用一条命令推翻。本章先把 New PM 的对象模型立起来，再用真实输出逐条校准。

## 先建立直觉

把 `-passes='module(function(instcombine))'` 想成一棵嵌套的盒子：

```
ModulePassManager
└── ModuleToFunctionPassAdaptor
    └── FunctionPassManager
        └── InstCombinePass
```

外层的 `module(...)` 没有改变 IR 单位，它只是新建一个 `ModulePassManager`；`function(...)` 也不是注释，它要求 parser 构造一个 `FunctionPassManager`，再套上「对 Module 里每个 Function 跑一遍」的 adaptor。真正执行 `instcombine` 的最内层对象，只认识 `Function &`。

Analysis 的盒子是另一棵树，方向和 IR 包含关系一致：

```
ModuleAnalysisManager (MAM)
   │ FunctionAnalysisManagerModuleProxy
   ▼
FunctionAnalysisManager (FAM)
   │ LoopAnalysisManagerFunctionProxy
   ▼
LoopAnalysisManager (LAM)
```

一个 Module pass 想拿 Function 的 analysis，不能直接持有 `FunctionAnalysisManager&` 去 `getResult`；它要先从 MAM 取 proxy，再由 proxy 把请求转发给 FAM。这个限制不是形式主义：proxy 的 result 是 move-only RAII 对象，生命周期结束时负责清掉内层缓存，防止外层 IR 被替换后内层还留着悬垂指针。

## Legacy PM 与 New PM：两套对象模型

Legacy 的入口是 `llvm/include/llvm/Pass.h:97` 的 `class Pass`。它有三个特征：

- 用虚函数分派：`FunctionPass::runOnFunction`、`ModulePass::runOnModule`，返回 `bool` 表示「有没有改 IR」。
- 用 `getAnalysisUsage(AnalysisUsage&)` 声明依赖和保留（`Pass.h:166`，`PassAnalysisSupport.h:48`）。pass 运行时通过 `getAnalysis<T>()` 拿结果，注册靠 `INITIALIZE_PASS`（`PassSupport.h:56`）或 `RegisterPass<T>`。
- pass manager 是单独的一套类：`legacy::PassManager` 在 `llvm/include/llvm/IR/LegacyPassManager.h:53`，`add(Pass*)` 接管裸指针所有权，`run(Module&)` 返回 bool。

Legacy 的失效模型是「pass 在 `getAnalysisUsage` 里列出自己保留哪些 analysis」。这个列表是字符串/ID 级的粗粒度声明，和 pass 实际做了什么没有强制关系；写错不会在编译期被发现。

New PM 换成了值语义和 CRTP：

- pass 继承 `PassInfoMixin<Derived>`（`llvm/include/llvm/IR/PassManager.h:82`），提供 `static StringRef name()` 和 `printPipeline`，但**不规定** `run` 的签名。签名由 pass manager 在 `addPass` 时用 concept 检查。
- `run` 返回 `PreservedAnalyses`，不是 bool。pass manager 拿到返回值后调用 `AM.invalidate(IR, PassPA)`（`PassManagerImpl.h:76-80`），再与聚合结果求交（`:88`）。
- analysis 用 `AnalysisKey*` 标识（`AnalysisInfoMixin::ID()`），结果按 `(AnalysisKey*, IRUnit*)` 缓存。
- 注册和解析共用一份 X-macro 表 `PassRegistry.def`，parser 和 `PassBuilder` 都 `#include` 它，只是每次换一组宏定义。

| 维度 | Legacy PM | New PM |
| --- | --- | --- |
| pass 基类 | `Pass` / `FunctionPass` / `ModulePass` | `PassInfoMixin<Derived>` |
| 是否改 IR | `bool runOnX` | `PreservedAnalyses run(...)` |
| 分析依赖 | `getAnalysisUsage` + `getAnalysis<T>` | `AnalysisManager::getResult<T>` |
| 分析失效 | `addPreserved` / `setPreservesAll` | `PreservedAnalyses` 返回值 |
| 注册 | `INITIALIZE_PASS` / `RegisterPass` | `PassRegistry.def` + 回调 |
| pipeline 表达 | 命令行 flag 组合 | `-passes=` 文本 |

Legacy PM 没有被删除。CodeGen 里仍有大量 legacy pass（Ch 23–25 会看到），所以读源码时要先确认这个 pass 属于哪套框架；两套 manager 的 `run` 语义和 analysis 生命周期不同，不能混着推理。

## IR 单位与 manager：四个层级

New PM 的 IR 单位是 `Module → CGSCC → Function → Loop`。CGSCC 是 `LazyCallGraph::SCC`，一个强连通分量；Loop 是 `LoopInfo` 里的自然循环对象。

manager 的位置需要说清楚，因为「都在 `PassManager.h`」是一个常见简化：

| manager | 定义位置 | 说明 |
| --- | --- | --- |
| `ModulePassManager` | `llvm/include/llvm/IR/PassManager.h:263` | `PassManager<Module>` |
| `FunctionPassManager` | `llvm/include/llvm/IR/PassManager.h:272` | `PassManager<Function>` |
| `CGSCCPassManager` | `llvm/include/llvm/Analysis/CGSCCPassManager.h:143` | 需要 `LazyCallGraph`，属于 Analysis 层 |
| `LoopPassManager` | `llvm/include/llvm/Transforms/Scalar/LoopPassManager.h:173` | 需要 `LoopStandardAnalysisResults&` 和 `LPMUpdater&`，属于 Transforms 层 |

通用的 `PassManager<IRUnitT, AnalysisManagerT, ExtraArgTs...>` 模板确实在 `PassManager.h:192`，但 Loop 和 CGSCC 的别名依赖更高层的类型，所以分别落在 `Analysis` 和 `Transforms/Scalar`。这条分层信息在 review 里有用：一个 `FunctionPass` 想直接拿 `LoopPassManager`，会把 `Transforms` 依赖拖进 `IR` 层，Ch 12 的 CMake 检查会拒绝。

`PassManager::addPass` 对同类型嵌套 manager 有一个特殊行为：直接把内部 passes 搬过来，不执行嵌套 manager（`PassManager.h:231-242`）。源码注释给的理由是避免同类型嵌套带来的 invalidation 复杂度。跨类型才需要 adaptor，例如 `createModuleToFunctionPassAdaptor`。

## `AnalysisManager` 与 proxy

`AnalysisManager` 的核心是一个三元组（`PassManager.h:279` 起）：

```cpp
using AnalysisResultListT =
    SmallVector<std::pair<AnalysisKey *, ResultConceptT::unique_ptr>, 8>;
using AnalysisResultListMapT = DenseMap<IRUnitT *, AnalysisResultListT>;
using AnalysisResultMapT =
    DenseMap<std::pair<AnalysisKey *, IRUnitT *>, ResultConceptT *>;
```

`getResult<T>(IR)`（`PassManager.h:440`）先断言这个 analysis 已注册，然后进入 `getResultImpl`（`PassManagerImpl.h:132`）：

```cpp
auto [RI, Inserted] =
    AnalysisResults.try_emplace(std::make_pair(ID, &IR), nullptr);
if (Inserted) {
  auto &P = this->lookUpPass(ID);
  // ... instrumentation ...
  auto Result = P.run(IR, *this, ExtraArgs...);
  // ... 放进 AnalysisResultLists / AnalysisResults ...
}
return *RI->second;
```

这段代码就是「缓存」的全部含义：第一次 `getResult` 跑 analysis，之后同一个 `(Key, IR*)` 直接命中。`getCachedResult` 只查不跑，适合「有就用、没有就算」的场景。

proxy 解决的是跨层访问。`InnerAnalysisManagerProxy<AnalysisManagerT, IRUnitT>`（`PassManager.h:619`）是「外层单位上的一个 analysis，结果是内层 manager 的引用」；`OuterAnalysisManagerProxy`（`:750`）方向相反。IR 层有两个常用别名：

- `FunctionAnalysisManagerModuleProxy`（`PassManager.h:707`）：MAM 上的 analysis，暴露 FAM。
- `ModuleAnalysisManagerFunctionProxy`（`PassManager.h:861`）：FAM 上的 analysis，暴露 MAM。

`PassBuilder::crossRegisterProxies`（`lib/Passes/PassBuilder.cpp:2672`）把 MAM/FAM/CGAM/LAM 四边的 proxy 一次注册好。自己搭 manager 时如果漏了这一步，`getResult<FunctionAnalysisManagerModuleProxy>` 会在注册检查处断言失败，而不是返回空。

proxy 的 result 是 move-only，析构时如果没收到 invalidate，会调用内层 manager 的 `clear()`（`PassManager.h:631-643`）。这解释了为什么不能把 proxy result 拷贝出去长期持有：它的生命周期就是「外层 pass 使用内层 analysis 的窗口」。

### 实测：analysis 只算一次

连续放两个 printer，第二个仍然请求同一个 analysis：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt -passes='function(print<domtree>,print<domtree>)' \
  -debug-pass-manager -disable-output loop.ll 2>&1
```

实测：

```text
Running analysis: InnerAnalysisManagerProxy<llvm::AnalysisManager<llvm::Function>, llvm::Module> on [module]
Running pass: DominatorTreePrinterPass on sum (10 instructions)
Running analysis: DominatorTreeAnalysis on sum
Running pass: DominatorTreePrinterPass on sum (10 instructions)
Running pass: VerifierPass on [module]
Running analysis: VerifierAnalysis on [module]
```

第二个 `DominatorTreePrinterPass` 之前没有新的 `Running analysis: DominatorTreeAnalysis`，因为 `getResult` 命中了缓存。这条输出是 reviewer 判断「两个 pass 是否共享 analysis」的最小证据。

不要把这个结论推广成「LoopAnalysis 一定触发 DT」。`LoopAnalysis::run`（`lib/Analysis/LoopInfo.cpp:1026-1037`）虽然写了 `AM.getResult<DominatorTreeAnalysis>`，但源码注释说明 DT 只在不可规约 CFG 上需要，lambda 不一定被调用：

```cpp
LoopInfo LI;
LI.analyze(&F, [&]() -> const DominatorTree & {
  return AM.getResult<DominatorTreeAnalysis>(F);
});
return LI;
```

对一个可规约循环跑 `print<loops>`，看不到 DT 的 `Running analysis`，可能只是因为它没查 DT，而不是缓存命中。判断共享关系时，把 pass 源码和 debug 输出一起看。

## `PassBuilder` 与 `PassRegistry.def`

`PassBuilder` 定义在 `llvm/include/llvm/Passes/PassBuilder.h:125`，构造函数在 `:151`。它持有 `LLVMContext&`、可选的 `TargetMachine*`、`PipelineTuningOptions`、`PGOOptions` 和 `PassInstrumentationCallbacks*`。它的职责不是「运行 pass」，而是：

1. 把 analysis 注册进四个 manager：`registerModuleAnalyses`（`PassBuilder.cpp:720`）、`registerCGSCCAnalyses`（`:729`）、`registerFunctionAnalyses`（`:738`）、`registerLoopAnalyses`（`:770`）。
2. 把 pass 名字和构造函数绑定，供 parser 使用；入口是 `registerPipelineParsingCallback` 的五个重载（`PassBuilder.h:633-653`）。
3. 构造默认 pipeline：`buildPerModuleDefaultPipeline` 等。

`registerFunctionAnalyses` 的实现值得完整看一遍：

```cpp
void PassBuilder::registerFunctionAnalyses(FunctionAnalysisManager &FAM) {
  FAM.registerPass([&] { return buildDefaultAAPipeline(); });

#define FUNCTION_ANALYSIS(NAME, CREATE_PASS)                                   \
  if constexpr (std::is_constructible_v<                                       \
                    std::remove_reference_t<decltype(CREATE_PASS)>,            \
                    const TargetMachine &>) {                                  \
    if (TM)                                                                    \
      FAM.registerPass([&] { return CREATE_PASS; });                           \
  } else {                                                                     \
    FAM.registerPass([&] { return CREATE_PASS; });                             \
  }
#include "PassRegistry.def"

  for (auto &C : FunctionAnalysisRegistrationCallbacks)
    C(FAM);
}
```

两个细节：

- `FAM.registerPass` 在注册时构造 analysis pass 对象，但不执行它的 `run`；第一次 `getResult` 才产生结果，所以注册本身不触发 analysis 计算。
- 需要 `TargetMachine` 的 analysis 只有在 `PassBuilder` 持有 TM 时才注册；这也是一部分 pass 在 `opt` 里报 `requires TargetMachine` 的来源。

`PassRegistry.def` 是一张被多次包含的表。`PassBuilder.cpp` 在不同位置定义不同的 `FUNCTION_ANALYSIS` / `FUNCTION_PASS` / `MODULE_PASS_WITH_PARAMS` 宏，再 `#include` 同一个文件：

```cpp
FUNCTION_ANALYSIS("domtree", DominatorTreeAnalysis())      // PassRegistry.def:368
FUNCTION_ANALYSIS("loops", LoopAnalysis())                 // :375
FUNCTION_ANALYSIS("cycles", CycleAnalysis())               // :363
FUNCTION_PASS("print<domtree>", DominatorTreePrinterPass(errs()))  // :524
FUNCTION_PASS("print<loops>", LoopPrinterPass(errs()))     // :528
```

解析侧则把同名宏展开成 `if (Name == NAME) { ... }`。这就是「注册和解析不会漂移」的机制：它们读的是同一行表项。

## Pipeline 文本怎么解析

`parsePipelineText` 先把 `module(function(instcombine))` 解析成 `PipelineElement{Name="module", InnerPipeline=[{Name="function", InnerPipeline=[{Name="instcombine"}]}]}`。然后 `parseModulePass`（`PassBuilder.cpp:2072`）按名字分派：

- `Name == "module"`：新建 `ModulePassManager`，递归 `parseModulePassPipeline`（`:2696`）。
- `Name == "cgscc"`：新建 `CGSCCPassManager`，套 `createModuleToPostOrderCGSCCPassAdaptor`。
- `function` / `function<eager-inv>`：新建 `FunctionPassManager`，套 `createModuleToFunctionPassAdaptor`。
- 其余名字落到 `PassRegistry.def` 生成的 `MODULE_PASS` / `FUNCTION_PASS` 分支。

关键在 `FUNCTION_PASS` 宏（`PassBuilder.cpp:2164-2176`）：

```cpp
#define FUNCTION_PASS(NAME, CREATE_PASS)                                       \
  if (Name == NAME) {                                                          \
    ...
    MPM.addPass(createModuleToFunctionPassAdaptor(CREATE_PASS));               \
    return Error::success();                                                   \
  }
```

它位于 `parseModulePass` 内部。也就是说，**module pipeline 本来就接受 function pass 名字**，parser 会自动套 adaptor。这解释了一个容易误判的现象：

```bash
$B/opt -passes='module(instcombine)' --print-pipeline-passes -disable-output min.ll
function(instcombine<max-iterations=1;verify-fixpoint>),verify
```

它和下面这条的输出逐字相同：

```bash
$B/opt -passes='module(function(instcombine))' --print-pipeline-passes -disable-output min.ll
function(instcombine<max-iterations=1;verify-fixpoint>),verify
```

所以 `module(instcombine)` 不是错误写法，而是 parser 接受并由 adaptor 适配的写法。反过来不成立：`parseFunctionPass` 里没有「把 module pass 降级」的宏，`function(globalopt)` 会直接报 unknown function pass。

`default<O2>` 也是同一张表里的条目（`PassRegistry.def:267-272`）：

```cpp
MODULE_PASS_WITH_PARAMS(
    "default", "", [&](OptimizationLevel L) {
      setupOptionsForPipelineAlias(PTO, L);
      return buildPerModuleDefaultPipeline(L);
    },
    parseOptLevelParam, "O0;O1;O2;O3")
```

`parseOptLevel` 在 `PassBuilder.cpp:550` 把 `O0`/`O1`/`O2`/`O3` 映射成枚举；`buildPerModuleDefaultPipeline` 在 `PassBuilderPipelines.cpp` 里拼出完整 pipeline。`--print-pipeline-passes` 打印的是拼完后的结果，不是别名本身：

```bash
$B/opt -passes='default<O2>' --print-pipeline-passes -disable-output min.ll | wc -c
```

实测输出 `4783`（含换行）。这一行 4.7KB 的文本就是 O2 的真实形状；它包含多个 `function<eager-inv>(...)`、`cgscc(...)`、`loop(...)` 和最后的 `verify`。

### 实测：`-debug-pass-manager` 看执行顺序

```bash
$B/opt -passes='function(instcombine)' -debug-pass-manager -disable-output min.ll
```

实测：

```text
Running analysis: InnerAnalysisManagerProxy<llvm::AnalysisManager<llvm::Function>, llvm::Module> on [module]
Running pass: InstCombinePass on f (4 instructions)
Running analysis: LastRunTrackingAnalysis on f
Running analysis: AssumptionAnalysis on f
Running analysis: TargetIRAnalysis on f
Running analysis: DominatorTreeAnalysis on f
Running analysis: TargetLibraryAnalysis on f
Running analysis: OptimizationRemarkEmitterAnalysis on f
Running analysis: AAManager on f
Running analysis: BasicAA on f
Running analysis: ScopedNoAliasAA on f
Running analysis: TypeBasedAA on f
Running analysis: OuterAnalysisManagerProxy<llvm::AnalysisManager<llvm::Module>, llvm::Function> on f
Running pass: VerifierPass on [module]
Running analysis: VerifierAnalysis on [module]
```

三个可读信息：

1. `InstCombinePass` 标了 `(4 instructions)`，这是 `PassInstrumentation` 在进入 pass 前统计的 IR 规模，不是 pass 自己打印的。
2. 它请求了 DT、AA、TargetLibrary 等 analysis；这些名字来自 analysis 的注册名，不是 pass 源码里的字符串。
3. 最后多出一个 `VerifierPass`。这是 `opt` 驱动加的，不是 `-passes` 里的内容；`NewPMDriver.cpp:529-530` 在解析完 pipeline 后按 `VerifierKind` 追加。

## `isRequired`、`printPipeline`、`PreservedAnalyses`

这三个 API 经常被混在一起，其实回答三个不同问题：

| API | 回答的问题 | 关键源码 |
| --- | --- | --- |
| `isRequired()` | 这个 pass 能不能被 instrumentation / opt-bisect 跳过 | `PassManager.h:103`、`PassInstrumentation.h:206-231` |
| `printPipeline()` | 这个 pass 在 pipeline 文本里叫什么 | `PassManager.h:83`、`:209` |
| `run` 返回的 `PreservedAnalyses` | 这个 pass 跑完后哪些 analysis 还能用 | `PassManagerImpl.h:76-88` |

`RequiredPassInfoMixin` 返回 `true`，`OptionalPassInfoMixin` 返回 `false`（`PassManager.h:103-116`）。`PassInstrumentation::runBeforePass` 只对 optional pass 调 `ShouldRunOptionalPassCallbacks`（`PassInstrumentation.h:231`），所以 `-opt-bisect-limit` 不会把 verifier 或 printer 跳掉。`isRequired` 与「pass 是否注册」「analysis 是否保留」都无关。

`printPipeline` 默认打印 `DerivedT::name()`，再经过 `MapClassName2PassName` 映射成 `-passes` 里的名字。`PassManager::printPipeline`（`PassManager.h:209`）用逗号连接子 pass；这就是 `--print-pipeline-passes` 的输出格式。类名和 pass 名的映射由 `PassBuilder` 构造时注册的回调从 `PassRegistry.def` 填入 `PassInstrumentationCallbacks`（`PassBuilder.cpp:574-624`，回调在第一次查询时执行），所以 `InstCombinePass` 会打印成 `instcombine`，而不是 C++ 类名。

`PreservedAnalyses` 是 pass 对 manager 的承诺。`PassManagerImpl.h:76-88` 的循环是：

```cpp
PreservedAnalyses PassPA = Pass->run(IR, AM, ExtraArgs...);
AM.invalidate(IR, PassPA);
PI.runAfterPass<IRUnitT>(IR, PassPA);
PA.intersect(std::move(PassPA));
```

manager 不会检查 pass 是否真的遵守承诺；它只按 `PassPA` 决定删哪些缓存。具体哪些 analysis 在什么条件下被删、`CFGAnalyses` 集合怎么用，留给 Ch 16。本章要记住：**返回值是契约，不是日志**。

## 失败例子：三种 pipeline 错误

### 1. pass 名不存在

```bash
$B/opt -passes='function(no-such-pass)' -disable-output min.ll
```

实测：

```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: unknown function pass 'no-such-pass'
```

退出码是 `1`。这条错误来自 `parseFunctionPass` 的末尾（`PassBuilder.cpp:2487`），不是来自 `PassRegistry.def` 的注册检查。换句话说，名字拼错会在解析期失败，不会静默跳过。

### 2. pipeline 语法错误

```bash
$B/opt -passes='function(instcombine' -disable-output min.ll
```

实测：

```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: invalid pipeline 'function(instcombine'
```

括号不配对时，`parsePipelineText` 返回 `std::nullopt`，`parsePassPipeline` 在 `PassBuilder.cpp:2713` 生成 `invalid pipeline`。这类错误不会告诉你「少了一个右括号」，只会回显整条文本；排查时先数括号，再看嵌套层级。

### 3. 在 function pipeline 里放 module pass

```bash
$B/opt -passes='function(globalopt)' -disable-output min.ll
```

实测：

```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: unknown function pass 'globalopt'
```

`globalopt` 只在 module 级的 `MODULE_PASS` 表里注册；`parseFunctionPass` 找不到它，报的是「unknown function pass」，而不是「这是 module pass」。错误文本容易让人以为名字写错。正确的写法是 `module(globalopt)`，或者如果确实想对每个函数跑，换成对应的 function pass。

这个例子也说明为什么 `module(instcombine)` 能过：方向是「module 容器接受 function pass 并加 adaptor」，不是「function 容器接受 module pass」。parser 的适配方向是单向的。

## 踩雷集锦

1. **以为 function pass 只能写在 `function(...)` 里**。`module(instcombine)` 会被 `FUNCTION_PASS` 宏自动包成 `createModuleToFunctionPassAdaptor`，`--print-pipeline-passes` 的输出与 `module(function(instcombine))` 相同。判断依据是输出，不是名字层级。
2. **以为 `-passes` 只包含你写的 pass**。`opt` 会在末尾追加 `VerifierPass`（`NewPMDriver.cpp:529-530`）。如果你用 `--print-pipeline-passes` 对齐测试的 `RUN:` 行，记得这条额外的 `verify`。
3. **把 `-debug-pass-manager` 的 `Running analysis` 当成每个 pass 都重算**。同一个 `(Key, IR*)` 只打印一次；第二次是 `getResult` 缓存命中。要确认缓存，拿两个都需要 DT 的 pass 串起来看。
4. **把 `isRequired()` 当成「必须注册」**。它只影响 instrumentation 是否允许跳过这个 pass；没注册的 pass 在 `getResult` 的断言处就会失败，和 `isRequired` 无关。
5. **在 out-of-tree pass 里直接抓 `FunctionAnalysisManager&` 存成员**。manager 的生命周期由 `PassBuilder`/driver 持有，pass 只应在 `run` 期间通过参数使用它；跨层访问走 proxy，不要自己造全局指针。

## 动手实验

1. 用 `--print-pipeline-passes` 对比 `module(instcombine)`、`module(function(instcombine))`、`function(instcombine)` 三条文本，解释为什么前两条相同、第三条少了 module 层。
2. 把 `loop.ll` 的 pipeline 改成 `function(print<domtree>,print<loops>,print<cycles>)`，用 `-debug-pass-manager` 数 `DominatorTreeAnalysis` 出现几次，并说明每次由谁请求。
3. 在 `PassRegistry.def` 里找到 `print<cycles>` 的表项（`:518`），再在 `PassBuilder.cpp` 里找到消费它的宏，画出「表项 → parser 分支 → pass 对象」的路径。
4. 故意把 `function(globalopt)` 改成 `module(globalopt)`，确认能跑；再把 `module(globalopt)` 改成 `function(globalopt)`，记录完整报错和退出码。
5. 用 `-passes='default<O1>' --print-pipeline-passes` 找出 `cgscc(` 和 `loop(` 各出现几次，并解释它们为什么不能直接写在 `function(...)` 里面。

## 本章重点整理

- Legacy PM 的 `runOnFunction` 返回 `bool`（是否改动），New PM 的 `run` 返回 `PreservedAnalyses`；前者由 manager 决定失效，后者由 pass 自己声明。
- IR 单位有四层：Module、CGSCC、Function、Loop。manager 也分四层，向下嵌套靠 adaptor（`ModuleToFunctionPassAdaptor`、`FunctionToLoopPassAdaptor`），方向是单向的。
- `AnalysisManager` 以 `(AnalysisKey, IRUnit*)` 为键缓存结果；`-debug-pass-manager` 里同一个 analysis 只打印一次 `Running analysis`，第二次是 `getResult` 命中缓存。
- proxy（如 `FunctionAnalysisManagerModuleProxy`）把内层 manager 暴露给外层，proxy result 只在对应 `run` 期间有效，不能跨层保存。
- `PassRegistry.def` 是 pass 名的单一事实来源；parser 的宏由这张表生成，`--print-pipeline-passes` 是判断文本实际展开成什么的依据。
- `module(instcombine)` 合法是因为 adaptor 把 function pass 包进 module pipeline；`function(globalopt)` 报错是因为适配方向不可逆。
- `opt` 会在 pipeline 末尾追加 `VerifierPass`，对齐 `RUN:` 行时要把它算进去。

## 自我检核

- [ ] 不看源码，能说出 Legacy `Pass::runOnFunction` 和 New PM `run` 在返回值语义上的区别
- [ ] 能画出 `ModulePassManager → ModuleToFunctionPassAdaptor → FunctionPassManager → InstCombinePass` 的对象链
- [ ] 能解释 `FunctionAnalysisManagerModuleProxy` 解决什么问题，以及为什么 proxy result 不能长期持有
- [ ] 给定一段 `-debug-pass-manager` 输出，能判断某个 analysis 是首次计算还是缓存命中
- [ ] 能解释 `module(instcombine)` 为什么不报错，而 `function(globalopt)` 报错
- [ ] 能指出 `isRequired()`、`printPipeline()`、`PreservedAnalyses` 各自回答什么问题
- [ ] 能在 `PassRegistry.def` 里定位一个 pass 名，并找到消费它的 parser 宏
- [ ] 能解释 `opt` 为什么在 pipeline 末尾多打印一个 `verify`
- [ ] 能说出 `default<O2>` 的展开结果从哪里来，以及为什么 `--print-pipeline-passes` 的输出比别名长得多

## 延伸阅读

- **[Writing an LLVM New PM Pass](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)**
  - **读哪里**：`PassInfoMixin`、`run` 的签名、`PreservedAnalyses` 三节，以及最后的 out-of-tree pass 示例
  - **能学到什么**：把本章的对象模型落成一个能编译的 pass；重点看它为什么在 `run` 里返回 `PreservedAnalyses::all()` 或 `none()`
  - **前提**：读完本章的 `PassManager.h` 对象链，知道 `AnalysisManager::getResult` 的缓存语义
- **[LLVM New Pass Manager 设计文档](https://llvm.org/docs/NewPassManager.html)**
  - **读哪里**：pipeline 语法、analysis invalidation、CGSCC 与 loop adaptor 三节
  - **能学到什么**：本章省略的「为什么 New PM 要用 proxy 而不是全局 registry」；文档里的 invalidation 例子可以直接对照 Ch 16
  - **前提**：能读懂 `PassManager.h` 里的模板声明；不要求记住所有 adaptor 名字
- **[`llvm/lib/Passes/PassRegistry.def`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Passes/PassRegistry.def)**
  - **读哪里**：`FUNCTION_ANALYSIS`、`FUNCTION_PASS`、`MODULE_PASS_WITH_PARAMS("default", ...)` 三类表项
  - **能学到什么**：注册与解析共用一张表的实际写法；对照 `PassBuilder.cpp:620-710` 的宏定义，能看出每个表项被展开成什么
  - **前提**：知道 C 预处理的 `#include` 可以放在宏定义之间；不熟悉 X-macro 的话先看表项和宏的对应关系

→ [Ch 14 — 支配树、循环与 CFG 分析：`DominatorTree`、`LoopInfo`、`CycleInfo`](./14-dominators-loops-analyses.md)
