# Ch 16 — PreservedAnalyses 与失效：pass 说「没坏」之前，先证明

> **目标**：把 New PM 的 analysis 缓存当成一份契约来读。学完你能判断一个 pass 返回的 `PreservedAnalyses` 是否与它实际做的修改一致，能解释 `getResult`/`getCachedResult` 背后发生了什么，并能用 `-debug-pass-manager` 与 `-verify-each` 把「缓存复用」和「失效漏报」变成可复现的证据。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（`RelWithDebInfo` + assertions ON）。构建的 CMake 源树是 `/home/ubuntu/llvm/trees/pr-lane/llvm` @ `a5eaf194d8b6`；本章的 `opt` 输出与 out-of-tree 插件实验都在这个构建上真实运行。插件源码在 `/tmp/llvm-ch16`，不在课程仓库内。

## 为什么需要这个？

Ch 13 讲了 New PM 的 pipeline 文本，Ch 14 讲了 DominatorTree/LoopInfo 这些分析本身。本章讲它们之间的接缝：**一个 pass 改完 IR，凭什么告诉框架「我的 analysis 还能用」？**

这个接缝的 failure mode 很隐蔽：

- pass 删了一个 basic block，却返回 `PreservedAnalyses::all()`；后面的 pass 拿到指向已删除 block 的 `DominatorTree`，结果可能是错误答案，也可能是断言。
- pass 改了 CFG，却只 `preserveSet<CFGAnalyses>()`；`DominatorTree` 和 `LoopInfo` 都认为 CFG 没变，继续用旧节点。
- pass 更新了 `DominatorTree`，却忘了 `preserve<DominatorTreeAnalysis>()`；结果只是多算一次，不会错，但性能证据会骗你。
- pass 删了 block，却忘了让 `LoopInfo` 失效；`Loop` 对象里还挂着已删除的 block。

这些都不是「API 用错」这么简单。`PreservedAnalyses` 是一句**可验证的断言**：pass 说「这个 analysis 在修改后仍然正确」。Reviewer 的职责是检查这句话有没有证据。

```
   pass.run(IR, AM)
        │
        │ 修改 IR
        ▼
   返回 PreservedAnalyses
        │
        ▼
   PassManager::run
        │  AM.invalidate(IR, PassPA)
        ▼
   对每个缓存 result 调 invalidate()
        │
        ├─ 保留 → 继续用缓存
        └─ 失效 → 删除 result，下次 getResult() 重算
```

## 16.1 直觉：analysis cache 是一张借条

把 `AnalysisManager` 想成一个借书处。每个 analysis result 是借出去的书，`getResult` 是借书，`PreservedAnalyses` 是还书时说的「我没弄坏」。

- 你**没动**书，说 `all()`，借书处保留记录。
- 你**动了**书但**修好了**，说 `preserve<那本书>()`，借书处保留记录。
- 你**动了**书，说 `none()`，借书处把记录删掉，下次重借。
- 你**动了**书却说 `all()`，借书处不会检查；下一个借书的人拿到的是坏书。

框架不会替你验证「书有没有坏」。它只相信 `PreservedAnalyses`。这就是为什么 `all()` 是一个强承诺，而不是「我懒得列」的默认值。

## 16.2 `PreservedAnalyses` API

先纠正一个常见位置错误：`PreservedAnalyses` 的定义在 `llvm/include/llvm/IR/Analysis.h:112`，不在 `PassManager.h`；`PassManager.h` 通过 include 把它带进来。读源码时直接打开 `Analysis.h`。

核心 API：

| API | 位置 | 语义 |
| --- | --- | --- |
| `PreservedAnalyses::none()` | `Analysis.h:115` | 空集；所有 analysis 都可能失效 |
| `PreservedAnalyses::all()` | `Analysis.h:118` | 保留所有；设置 `PreservesAll` 与 `AllAnalysesKey` |
| `allInSet<SetT>()` | `Analysis.h:126` | 只保留一个 set |
| `preserve<AnalysisT>()` | `Analysis.h:134` | 显式保留一个 analysis（按 `AnalysisT::ID()`） |
| `preserveSet<SetT>()` | `Analysis.h:152` | 保留一个 analysis set（按 set ID） |
| `abandon<AnalysisT>()` | `Analysis.h:176` | 显式标记失效；`abandon` 优先于任何 set |
| `getChecker<T>().preserved()` | `Analysis.h:253` | 查询单个 analysis 是否被保留 |
| `allAnalysesInSetPreserved<SetT>()` | `Analysis.h:300` | 查询整个 set；框架的快路径用它 |

这套 API 没有名为 `isPreserved()` 的单一函数；等价的查询是 `getChecker<T>().preserved()`（单个 analysis）或 `allAnalysesInSetPreserved<SetT>()`（一整个 set）。

`preserve<T>()` 只插入 ID，不会检查 T 是否真的被算过；`abandon<T>()` 会从 `PreservedIDs` 删除并插入 `NotPreservedAnalysisIDs`。`areAllPreserved()` 在 `:293`，条件是「没有 abandoned 且 `PreservesAll` 为真」。

### 常用组合

| 你做了什么 | 应该返回 |
| --- | --- |
| 完全没改 IR | `PreservedAnalyses::all()` |
| 改了 IR，但只改了指令、没改 CFG，且没有 analysis 依赖具体指令 | `preserveSet<CFGAnalyses>()` |
| 改了 CFG，且自己更新了 DT | `preserve<DominatorTreeAnalysis>()`（还要考虑 LoopInfo 等） |
| 改了 CFG，没更新任何分析 | `none()` |
| 只改了 loop 结构，更新了 LoopInfo | `preserve<LoopAnalysis>()`（DT 是否仍然有效要单独判断） |
| 删了 block，LoopInfo 没更新 | 不能 preserve LoopInfo；至少要 `none()` |

`preserveSet<CFGAnalyses>()` 的语义是「CFG 没变」，不是「我改了 CFG 但修好了」。这一点由使用方决定：`DominatorTree::invalidate`（`llvm/lib/IR/Dominators.cpp:70`）和 `LoopInfo::invalidate`（`llvm/lib/Analysis/LoopInfo.cpp:935`）都检查 `PAC.preservedSet<CFGAnalyses>()`。你 preserve 了这个 set，它们就相信 CFG 没变。

## 16.3 `AnalysisManager` 内部：`getResult` 与失效

`AnalysisManager` 的关键成员：

- `getResult<PassT>(IR)`：`PassManager.h:440`。有缓存就返回，没有就运行 analysis。
- `getCachedResult<PassT>(IR)`：`PassManager.h:459`。只查缓存，不运行。
- `getResultImpl`：`PassManagerImpl.h:131`。真正插入缓存、运行 analysis、处理递归查询。
- `AnalysisResultModel`：`PassManagerInternal.h:232`。把 `PassT::Result` 包成 type-erased 的 result，并保存 `invalidate` 回调。
- `Invalidator`：`PassManager.h:318`。让 analysis result 在失效时递归查询依赖。
- `AnalysisManager::invalidate`：`PassManagerImpl.h:169`。遍历缓存，调用每个 result 的 `invalidate`，删除失效项。

### pass 返回 PA 之后发生了什么

`PassManager::run` 在 `PassManagerImpl.h:28`。每跑完一个 pass：

```cpp
PreservedAnalyses PassPA = Pass->run(IR, AM, ExtraArgs...);
AM.invalidate(IR, PassPA);
PI.runAfterPass<IRUnitT>(*Pass, IR, PassPA);
PA.intersect(std::move(PassPA));
```

`AM.invalidate` 的流程（`:169-210`）：

1. 如果 `PA.allAnalysesInSetPreserved<AllAnalysesOn<IRUnitT>>()`，直接返回。
2. 取出该 IR unit 的 result 列表；为每个 result 建一个 `ResultState`。
3. 对每个 result 调 `Invalidator::invalidateResult`。
4. `invalidateResult` 先看状态缓存；然后调用 `Result.invalidate(IR, PA, Inv)`。
5. `AnalysisResultModel` 的 `invalidate` 回调（`PassManagerInternal.h:232-258`）有两种路径：
   - 如果 `ResultT` 自己定义了 `invalidate(IR, PA, Inv)`，调用它；result 可以通过 `Inv.invalidate<OtherAnalysis>(IR, PA)` 递归让依赖失效。
   - 否则，用 `PA.getChecker<PassT>()`：`preserved()` 或 `preservedSet<AllAnalysesOn<IRUnitT>>()` 为真就保留，否则失效。
6. 所有标记为 invalid 的 result 被删除，`AnalysisResults` 映射同步清理。

这意味着两件事：

- **失效是「拉」的，不是「推」的**：pass 不需要列出所有受影响的 analysis；每个 analysis result 自己声明依赖，框架递归处理。
- **`getCachedResult` 可能返回 nullptr**：如果前一个 pass 没有 preserve 它，缓存项已经被删除。很多 pass 用 `getCachedResult` 做「有就复用，没有就算」的优化，必须处理空指针。

### 依赖失效的例子

`AAResults::invalidate`（`llvm/lib/Analysis/AliasAnalysis.cpp:68`）是一个好例子：

```cpp
auto PAC = PA.getChecker<AAManager>();
if (!PAC.preservedWhenStateless())
  return true;
for (AnalysisKey *ID : AADeps)
  if (Inv.invalidate(ID, F, PA))
    return true;
return false;
```

`AAResults` 本身是无状态的聚合，所以 `AAManager` 默认被视为 preserved；但它依赖 `BasicAA`、`TypeBasedAA` 等具体分析。只要有一个依赖失效，`AAResults` 也失效。这就是 `Invalidator` 存在的意义：没有它，每个聚合分析都要自己维护一张全局依赖图。

## 16.4 四种常见错误

| 错误 | 现象 | 正确做法 |
| --- | --- | --- |
| 改了 IR 却返回 `all()` | 下一个 pass 拿到旧 DT/LoopInfo/AA | 返回 `none()` 或精确列出仍然有效的分析 |
| 改了 CFG 却返回 `preserveSet<CFGAnalyses>()` | DT/LoopInfo 相信 CFG 没变 | CFG 变了就不能 preserve 这个 set；返回 `none()` 或自己更新并逐个 preserve |
| 更新了 DT 却忘了 `preserve<DominatorTreeAnalysis>()` | 多算一次，结果正确但性能差 | 显式 preserve；同时检查 LoopInfo/MemorySSA 等 |
| 删了 block 却没让 LoopInfo 失效 | `Loop` 里挂着已删除 block，遍历时崩溃/误判 | `none()` 或调用 `LoopInfo` 的更新接口并 preserve |
| `renumberBlocks()` 后没处理 block number 依赖 | `GenericCycleInfo` 的 epoch 断言 | 不 renumber，或同步失效/更新依赖 block number 的分析 |

`renumberBlocks` 是特例中的特例。`Function::renumberBlocks`（`llvm/lib/IR/Function.cpp:64`）会重排 block number 并递增 `BlockNumEpoch`；`Function.h:827` 的注释明确警告：

> Be aware that other data structures and analyses (e.g., DominatorTree) may depend on the value numbers and need to be updated or invalidated.

`GenericCycleInfo` 就把 block number 当索引，并在 `verifyBlockNumberEpoch`（`llvm/include/llvm/ADT/GenericCycleInfo.h:158-161`）里断言 epoch 没变。16.6 会复现这个断言。

## 16.5 实测一：观察 analysis 的计算与复用

先准备两段 IR。`sum.ll` 就是 15.2 的 `@sum`；`change.ll` 如下：

```llvm
define i32 @f(i32 %x) {
entry:
  %a = add i32 %x, 0
  %b = mul i32 %a, 1
  ret i32 %b
}
```

先看「同一个 analysis 被 require 两次」：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt -passes='function(require<domtree>,require<domtree>)' \
  -debug-pass-manager -disable-output sum.ll
```

实测：

```text
Running analysis: InnerAnalysisManagerProxy<llvm::AnalysisManager<llvm::Function>, llvm::Module> on [module]
Running pass: RequireAnalysisPass<llvm::DominatorTreeAnalysis, llvm::Function> on sum (8 instructions)
Running analysis: DominatorTreeAnalysis on sum
Running pass: RequireAnalysisPass<llvm::DominatorTreeAnalysis, llvm::Function> on sum (8 instructions)
Running pass: VerifierPass on [module]
Running analysis: VerifierAnalysis on [module]
```

第二个 `require<domtree>` **没有**再打印 `Running analysis`：缓存命中。

再看「一个会改 IR 的 pass 之后」：

```bash
$B/opt -passes='function(instcombine,require<domtree>)' \
  -debug-pass-manager -disable-output change.ll
```

实测：

```text
Running pass: InstCombinePass on f (3 instructions)
Running analysis: DominatorTreeAnalysis on f
...
Running pass: RequireAnalysisPass<llvm::DominatorTreeAnalysis, llvm::Function> on f (1 instruction)
```

`InstCombinePass` 在运行中查询了 DT（打印在它下面）；它返回的 PA 保留了 CFG 相关分析，所以后面的 `require<domtree>` 直接复用。即使它改了 IR，只要 CFG 没变，DT 仍然可以保留。这里要区分两件事：**DT 被计算过**不等于**DT 被 preserve**；是否复用由 `AM.invalidate` 根据 `PassPA` 决定。

`-verify-each` 在每跑完一个 pass 后插入验证点：

```bash
$B/opt -passes='function(instcombine)' -verify-each \
  -debug-pass-manager -disable-output sum.ll
```

实测多出一行：

```text
Verifying function sum
```

这就是「验证点」的位置：它在 `InstCombinePass` 之后、`VerifierPass` 之前。如果某个 pass 产生了非法 IR，`-verify-each` 会在第一个违规 pass 处停下，而不是等到 pipeline 末尾。

## 16.6 实测二：故意漏报 PA 的 out-of-tree pass

这一节写两个最小插件。它们不在课程仓库里，源码在 `/tmp/llvm-ch16`；构建命令用了当前 `pr-lane` 构建的 `llvm-config`。插件 API 在这个版本里是 `llvm/Plugins/PassPlugin.h` 与 `RequiredPassInfoMixin`（`PassManager.h:103`）；旧的 `PassInfoMixin` 已经退到 `detail` 命名空间。

### 实验 A：`renumberBlocks` 与 `GenericCycleInfo` epoch

插件核心：

```cpp
struct RenumberAll : llvm::RequiredPassInfoMixin<RenumberAll> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
    F.renumberBlocks();
    return PreservedAnalyses::all(); // 漏报
  }
};

struct QueryCycles : llvm::RequiredPassInfoMixin<QueryCycles> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM) {
    (void)AM.getResult<CycleAnalysis>(F).getCycle(&F.getEntryBlock());
    return PreservedAnalyses::all();
  }
};
```

构建：

```bash
$B/clang++ -fPIC -shared -std=c++17 $($B/llvm-config --cxxflags) \
  Renumber.cpp -o Renumber.so \
  $($B/llvm-config --ldflags) \
  $($B/llvm-config --libs passes analysis support irreader) \
  $($B/llvm-config --system-libs) -Wl,-rpath,$B/../lib
```

跑：

```bash
$B/opt -load-pass-plugin=./Renumber.so \
  -passes='function(print<cycles>,renumber-all-lab,query-cycles-lab)' \
  -disable-output loop.ll
```

实测（截取关键行）：

```text
CycleInfo for function: sum
    depth=1: entries(loop)
opt: .../GenericCycleInfo.h:159: ... Assertion `BlockNumberEpoch == GraphTraits<const FunctionT *>::getNumberEpoch(Fn) && "CycleInfo used with outdated block number epoch"' failed.
exit=134
```

`print<cycles>` 先把 `CycleInfo` 算进缓存；`renumberAll` 改了 block number 却返回 `all()`；`QueryCycles` 拿到旧 `CycleInfo`，`getCycle` 调用 `verifyBlockNumberEpoch`，断言。

把返回值改成 `none()`：

```cpp
struct RenumberNone : llvm::RequiredPassInfoMixin<RenumberNone> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
    F.renumberBlocks();
    return PreservedAnalyses::none();
  }
};
```

实测：

```text
Running pass: (anonymous namespace)::RenumberNone on sum (8 instructions)
Invalidating analysis: CycleAnalysis on sum
Running pass: (anonymous namespace)::QueryCycles on sum (8 instructions)
Running analysis: CycleAnalysis on sum
```

`Invalidating analysis: CycleAnalysis` 是 `-debug-pass-manager` 打出来的；`QueryCycles` 重新计算，没有断言。

还有一个容易忽略的变体：**在同一个 pass 内**先 renumber 再查询。此时 `none()` 也救不了，因为 PA 只在 pass 边界生效：

```cpp
struct RenumberAndQuery : llvm::RequiredPassInfoMixin<RenumberAndQuery> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM) {
    F.renumberBlocks();
    (void)AM.getResult<CycleAnalysis>(F).getCycle(&F.getEntryBlock());
    return PreservedAnalyses::none(); // 太晚
  }
};
```

实测仍然断言。结论：**PA 是 pass 边界的契约，不是 pass 内部的同步机制。** 如果 pass 内部要查一个依赖 block number 的分析，必须在 renumber 之前查完，或手动更新/清除该分析，不能指望返回 `none()`。

上游 `81e0dbceab0d` 的选择是「不 renumber」，而不是「正确失效」。16.7 会讨论这个取舍。

### 实验 B：stale DominatorTree

插件核心：

```cpp
struct StaleDTAll : llvm::RequiredPassInfoMixin<StaleDTAll> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM) {
    (void)AM.getResult<DominatorTreeAnalysis>(F); // 先缓存 DT
    BasicBlock &Entry = F.getEntryBlock();
    SplitBlock(&Entry, Entry.getTerminator()); // 改 CFG，不更新 DT
    return PreservedAnalyses::all();           // 漏报
  }
};
```

构建时把 `--libs` 换成 `passes analysis transformutils support irreader`，其余相同。跑：

```bash
$B/opt -load-pass-plugin=./StaleDT.so \
  -passes='function(require<domtree>,stale-dt-all-lab,verify<domtree>)' \
  -disable-output change.ll
```

实测：

```text
DominatorTree is different than a freshly computed one!
	Current:
  [1] %entry {4294967295,4294967295} [0]
	Freshly computed tree:
  [1] %entry {4294967295,4294967295} [0]
    [2] %entry.split {4294967295,4294967295} [1]
LLVM ERROR: verify<domtree> detected an invalid dominator tree
```

`SplitBlock` 新建了 `%entry.split`，旧 DT 里没有这个节点。`verify<domtree>` 把缓存 DT 和「刚算的 DT」对比，直接报错。换成 `none()` 后：

```text
Running pass: (anonymous namespace)::StaleDTNone on f (4 instructions)
Invalidating analysis: DominatorTreeAnalysis on f
Running pass: DominatorTreeVerifierPass on f (4 instructions)
Running analysis: DominatorTreeAnalysis on f
```

没有 `LLVM ERROR`。这也说明：`verify<domtree>` 本身会通过 `getResult` 取 DT；如果前一个 pass 正确失效，它拿到的是新算的树。

再看 `print<domtree>` 的差异。stale 版本只打印 `%entry`，fresh 版本打印 `%entry` 和 `%entry.split`：

```text
  [1] %entry {4294967295,4294967295} [0]
    [2] %entry.split {4294967295,4294967295} [1]
```

这不是「打印格式」问题，而是缓存里真的少了一个基本块。任何依赖 DT 的支配判断都会在这个新块上得到缺省答案（`getNode` 返回 null 时，`DominatorTree::dominates` 会把 use 当成 unreachable，见 `llvm/lib/IR/Dominators.cpp:105-108`）。

## 16.7 真实案例 `81e0dbceab0d`：`renumberBlocks` 之后 epoch 断言

commit：

```text
[coro] Remove renumberBlocks to fix assert (#244573)

GenericCycleInfo was not invalidated, so asserted when profcheck is
enabled that the block number epoche is outdated.

Remove the `renumberBlocks` call to fix this.
```

diff 很小：

```diff
     F.setSplittedCoroutine();
-    // Renumber basic blocks so that they are dense for crossing value analysis.
-    F.renumberBlocks();
 
     // Query BFI and populate SuspendFreqs right before splitting.
```

对应 `llvm/lib/Transforms/Coroutines/CoroSplit.cpp:2345` 附近；当前源码里 `F.setSplittedCoroutine();` 后面直接是 BFI 查询，`renumberBlocks` 已经删除。`CoroSplitPass::run` 在 `:2401` 返回 `PreservedAnalyses::none()`，但这**救不了**：断言发生在 pass 内部，`none()` 要等 pass 返回后才生效。

复现路径和 16.6 的实验 A 一样：

1. `CycleInfo` 先被算进缓存（`print<cycles>`，或 CoroSplit 内部的某个查询）。
2. `F.renumberBlocks()` 改变 block number，`BlockNumEpoch++`（`Function.cpp:64-69`）。
3. 同一个 pass 内，BFI 或后续查询通过 `CycleAnalysis` 拿到旧 `CycleInfo`。
4. `GenericCycleInfo::getCycle`（`:209-211`）调用 `verifyBlockNumberEpoch`，断言 `BlockNumberEpoch == getNumberEpoch(Fn)`。

### 两种修复的取舍

| 修复 | 做法 | 适用条件 | 代价 |
| --- | --- | --- | --- |
| 删除 `renumberBlocks` | 不改变 block number | block number 已经足够 dense；没有分析依赖「必须从 0 连续」 | 失去 dense 编号，crossing value analysis 可能慢一点 |
| 正确失效 | renumber 后清掉/更新 `CycleInfo`、DT 等 | 必须在**查询之前**完成，或把查询移到 pass 边界之后 | 需要精确知道哪些分析依赖 block number；容易漏 |

上游选了前者，理由在 commit message 里：「GenericCycleInfo already uses the numbers as block indices, they should still be sufficiently dense.」这是一个基于现状的判断，不是普遍规律。如果将来有分析真的要求 dense 编号，删除就不是正确修复，必须改成「renumber + 在同一个 pass 内同步更新所有依赖」。

Review 这类 diff 时，先查 `renumberBlocks` 的调用点：

```bash
rg -n 'renumberBlocks' llvm/lib llvm/include
```

再看调用点之后有没有查询 `CycleAnalysis`/`DominatorTreeAnalysis`/`LoopAnalysis`。如果有，删除调用是最小修复；如果没有，也要确认返回的 `PreservedAnalyses` 没有把依赖 block number 的分析错误地 preserve。

## 16.8 判据：什么时候可以 preserve

把问题拆成三句话：

1. **我改了哪些 IR 单元？** 指令、基本块集合、边、loop 结构、block number，分别影响不同的分析。
2. **这些分析有没有 `invalidate` 钩子？** `DominatorTree::invalidate`、`LoopInfo::invalidate`、`AAResults::invalidate` 都检查 `PA`；没有钩子的 result 走默认 checker。
3. **我有没有在 pass 内部查询旧结果？** 如果有，PA 来不及生效；必须在查询前更新，或不要做这个修改。

一个实用的 review 模板：

```text
这个 pass 改了 ____。
它返回 ____。
受影响的分析有 ____。
其中 ____ 有 invalidate 钩子，检查的是 ____。
测试是否覆盖了「修改后查询该分析」的路径？
```

如果第四行答不出来，就不要 LGTM。

## 踩雷集锦

1. **「我没改多少」就返回 `all()`**：`all()` 的语义是「所有 analysis 仍然正确」，不是「我觉得影响不大」。改了任何 IR 就要重新评估。
2. **把 `preserveSet<CFGAnalyses>()` 当成「CFG 分析都修好了」**：它表示「CFG 没变」。DT/LoopInfo 的 `invalidate` 正是这么理解的；改了 CFG 还 preserve，就是把它们骗过去。
3. **`preserve<DominatorTreeAnalysis>()` 但没更新 DT**：preserve 不是「我希望它有效」，是「它已经有效」。没有 `DTU`/`DomTreeUpdater` 的更新，就不要 preserve。
4. **删 block 后只处理 DT，忘了 LoopInfo**：`LoopInfo` 的 `Loop` 对象持有 block 指针；DT 正确不代表 LoopInfo 正确。
5. **以为返回 `none()` 能修复同一个 pass 内的旧查询**：PA 在 pass 边界才被 `AM.invalidate` 消费；pass 内部的 `getResult` 仍然拿缓存。
6. **`getCachedResult` 不判空**：前一个 pass 失效后，缓存项已经删除；`getCachedResult` 返回 `nullptr`，解引用就是崩溃。

## 动手练习

1. 用 `-passes='function(require<domtree>,require<domtree>)' -debug-pass-manager` 证明第二次 require 没有重算；再把中间插入 `instcombine`，观察 DT 是否重算，并解释原因。
2. 把 16.6 实验 A 的 `RenumberAll` 改成返回 `none()`，确认断言消失；再把查询移回同一个 pass，确认 `none()` 失效。
3. 把实验 B 的 `StaleDTAll` 改成在 `SplitBlock` 后调用 `AM.getResult<DominatorTreeAnalysis>(F)` 并 `preserve<DominatorTreeAnalysis>()`；观察 `verify<domtree>` 是否仍然报错，解释为什么。
4. 用 `-verify-each` 跑一个会产生非法 IR 的 pipeline（例如故意删除一个仍被使用的 block），确认它在哪个 pass 后停下。
5. 读 `LoopInfo::invalidate`（`LoopInfo.cpp:935`）与 `DominatorTree::invalidate`（`Dominators.cpp:70`），列出它们检查的三种保留条件，并解释 `CFGAnalyses` 在其中的作用。

## 本章重点整理

- `PreservedAnalyses` 定义在 `llvm/IR/Analysis.h:112`；`all()` 是强承诺，`none()` 是安全默认。
- `preserve<T>()` 按 `AnalysisT::ID()` 标记；`preserveSet<CFGAnalyses>()` 的语义是「CFG 没变」。
- `PassManager::run` 在 `PassManagerImpl.h:80` 调用 `AM.invalidate(IR, PassPA)`；失效是拉式的，由每个 result 的 `invalidate` 钩子决定。
- `AnalysisResultModel` 默认走 `getChecker`；有 `invalidate` 成员的 result 自己处理依赖，并通过 `Invalidator` 递归。
- `DominatorTree`/`LoopInfo` 都把 `preserveSet<CFGAnalyses>()` 当成「CFG 未变」的证据。
- `renumberBlocks` 会改变 block number epoch；`GenericCycleInfo` 在 `getCycle` 时断言 epoch。
- 返回 `none()` 只能修复 pass 边界之后的查询，不能修复同一个 pass 内的旧缓存。

## 自我检核

- [ ] 能说出 `all()`、`none()`、`preserve<T>()`、`preserveSet<CFGAnalyses>()` 各自的语义差异
- [ ] 能解释为什么 `preserveSet<CFGAnalyses>()` 在改了 CFG 之后是错的
- [ ] 能画出 `PassManager::run` → `AM.invalidate` → `result.invalidate` 的调用链
- [ ] 能说出 `getResult` 与 `getCachedResult` 的区别，以及后者什么时候返回 `nullptr`
- [ ] 能解释 `AnalysisResultModel` 在没有 `invalidate` 成员时如何决定保留
- [ ] 能复述 `81e0dbceab0d` 的断言路径，以及「删除 renumber」与「正确失效」的取舍
- [ ] 能解释为什么同一个 pass 内返回 `none()` 不能修复旧查询
- [ ] 能用 `-debug-pass-manager` 区分「analysis 被计算」与「analysis 被 preserve」
- [ ] 能用 `-verify-each` / `verify<domtree>` 证明一个 pass 的 PA 漏报
- [ ] 能列出修改 CFG 后至少三个需要重新评估的分析（DT、LoopInfo、MemorySSA）

## 延伸阅读

- **[Writing an LLVM New PM Pass](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)**
  - **读哪里**：`Preserving Analyses` 一节，以及最小 pass 的 `run` 返回值示例
  - **能学到什么**：官方对 `PreservedAnalyses::all()` 与 `none()` 的推荐用法；本章 16.2 的表格以此为基础
  - **前提知识**：读过 Ch 13 的 New PM pipeline 文本与 `PassBuilder`
- **[The New Pass Manager](https://llvm.org/docs/NewPassManager.html)**
  - **读哪里**：`Analysis Management`、`Invalidation` 相关段落
  - **能学到什么**：AnalysisManager 的缓存模型与 invalidation 的设计目标；解释为什么失效是拉式的
  - **前提知识**：知道 `AnalysisKey`、`AnalysisInfoMixin` 的基本形式
- **[LLVM `Analysis.h` 源码](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/Analysis.h)**
  - **读哪里**：`PreservedAnalyses` 类（`:112` 起）与 `PreservedAnalysisChecker`
  - **能学到什么**：`preserve`/`abandon`/`allInSet` 的精确实现；`abandon` 为什么优先于 set
  - **前提知识**：能读 C++ 模板与 `SmallPtrSet`
- **[LLVM `PassManagerImpl.h` 源码](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/PassManagerImpl.h)**
  - **读哪里**：`PassManager::run`（`:28`）、`AnalysisManager::invalidate`（`:169`）、`getResultImpl`（`:131`）
  - **能学到什么**：PA 返回后框架到底做了什么；缓存项何时被删除、何时被重算
  - **前提知识**：读过本章 16.3 的调用链，知道 `Invalidator` 的作用

→ [Ch 17 — 验证与可观测性：把「我认为」变成「我测到」](./17-verification-and-observability.md)
