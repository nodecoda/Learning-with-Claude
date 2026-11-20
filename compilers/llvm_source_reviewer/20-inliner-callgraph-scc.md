# Ch 20 — Inliner、CallGraph 与 SCC

> **目标**：把内联看成一次同时修改函数体、调用图、SCC 形状和函数属性的跨函数事务。学完你能读懂 CGSCC 的 post-order 遍历，能用 `InlineCost` / `InlineAdvisor` 解释一次内联决策，并在 review 中判断某个 `alwaysinline`、`noinline`、`optnone`、`comdat` 或 `available_externally` 场景会不会破坏调用图或属性。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project` @ `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`（assertions ON）。本章命令与输出都在这个构建上真实运行。

## 为什么需要这个？

内联是少数会在 pass 运行期间**改变 pass manager 遍历结构**的变换：

```
Inliner 处理 SCC C
   → 把 callee 的 body 复制进 caller
   → 删除 call 边，可能新增/删除函数
   → SCC 可能拆分、合并，RefSCC 可能重组
   → pass manager 必须知道「接下来该访问哪个 SCC」
```

这类 bug 在 diff 里可能只表现为「少了一次 `UR.UpdatedC` 赋值」或「删除 callee 前没更新 LazyCallGraph」。只读内联决策逻辑，很难发现；把「函数体、调用图、SCC、属性」四条线分开检查，问题会显形。

## 先建立直觉

调用图有三层：

```
Module
  └── RefSCC：由引用边（含函数指针）形成的强连通区域，是变更的单位
        └── SCC：由 call 边形成的环，是 InlinerPass 的工作单位
              └── Function
```

遍历顺序是 bottom-up：先叶子，再调用者。这样 callee 在被内联前已经优化过。SCC 内部是环，不能按拓扑序处理，所以 inliner 用工作列表并延迟处理「内联后新产生的传递调用边」，避免一个函数被增量撑爆。

## 20.1 CGSCC 层：`CGSCCPassManager` 与 `LazyCallGraph`

`CGSCCPassManager` 是 PassManager 的别名（`llvm/include/llvm/Analysis/CGSCCPassManager.h:143`）：

```cpp
using CGSCCPassManager =
    PassManager<LazyCallGraph::SCC, CGSCCAnalysisManager,
                LazyCallGraph &, CGSCCUpdateResult &>;
```

它跑在 `LazyCallGraph::SCC` 上。`LazyCallGraph` 在 `llvm/include/llvm/Analysis/LazyCallGraph.h:109`，`SCC` 在 `:417`，`RefSCC` 在 `:540`。`RefSCC` 的注释（`:528`）说明它建模「引用边的强连通分量」，包含一个 SCC 的 DAG；SCC 才是 call 边的环。

模块到 CGSCC 的适配器是 `ModuleToPostOrderCGSCCPassAdaptor`（`:315`），实现在 `llvm/lib/Analysis/CGSCCPassManager.cpp:138`。关键步骤：

```cpp
CG.buildRefSCCs();
for (LazyCallGraph::RefSCC &RC : CG.postorder_ref_sccs()) {
  RCWorklist.insert(&RC);
  ...
  for (LazyCallGraph::SCC &C : llvm::reverse(*RC))
    CWorklist.insert(&C);
  ...
  C = CWorklist.pop_back_val();
```

位置在 `:177`、`:179`、`:209`、`:215`。`postorder_ref_sccs()` 给出 RefSCC 的 post-order；SCC 先按 `reverse(*RC)` 放进工作列表，再从尾部弹出，所以实际访问也是 post-order。`CGSCCUpdateResult` 的注释在 `CGSCCPassManager.h:233`，`CWorklist` 是 reverse post-order（`:245`）。

### 实测遍历顺序

三个函数 `leaf`、`mid`、`root`，调用链 `root -> mid -> leaf`：

```bash
$B/opt -passes='cgscc(inline)' -debug-only=cgscc -S chain.ll -o /dev/null
```

真实输出：

```text
Running an SCC pass across the RefSCC: [(leaf)]
Running an SCC pass across the RefSCC: [(mid)]
Deleting outgoing edge from 'mid' to 'leaf'
Running an SCC pass across the RefSCC: [(root)]
Deleting outgoing edge from 'root' to 'mid'
```

先 leaf，再 mid，最后 root。每个函数单独成一个 SCC；内联后 call 边消失，LazyCallGraph 删除对应边。

### SCC 变更协议

SCC pass 修改调用图后，要通过 `CGSCCUpdateResult` 告诉 pass manager：

- `UpdatedC`（`CGSCCPassManager.h:265`）：当前 SCC 被拆分/合并后的新 SCC
- `CWorklist`（`:245`）：需要重新访问的 SCC
- `InvalidatedSCCs`：已删除、应从工作列表跳过的 SCC
- `CrossSCCPA`（`:280`）：跨 SCC 的 preserved analyses

`LazyCallGraph` 提供变更原语：`switchInternalEdgeToCall`（`LazyCallGraph.h:683`）把 ref 边升级为 call 边，必要时合并 SCC；`switchInternalEdgeToRef`（`:713`）把 call 边降级为 ref 边，可能拆分 SCC。内联器在 `Inliner.cpp:459` 记录 `InlinedInternalEdges`，专门避免「SCC 拆分/合并导致无限重内联」。

## 20.2 两个内联器：`InlinerPass` 与 `ModuleInlinerPass`

`InlinerPass` 在 `llvm/include/llvm/Transforms/IPO/Inliner.h:36`，run 在 `llvm/lib/Transforms/IPO/Inliner.cpp:117`：

```cpp
PreservedAnalyses InlinerPass::run(LazyCallGraph::SCC &InitialC,
                                   CGSCCAnalysisManager &AM,
                                   LazyCallGraph &CG,
                                   CGSCCUpdateResult &UR);
```

它是 CGSCC 内联器：每次处理一个 SCC，调用列表来自该 SCC 的所有函数。源码 `Inliner.cpp:141` 解释了工作列表顺序：SCC 内部高度连接时，不能依赖拓扑序，所以先对 SCC 做一轮全量内联，再处理内联产生的传递边，避免单个函数超线性增长。

`ModuleInlinerPass` 在 `llvm/include/llvm/Transforms/IPO/ModuleInliner.h:28`，run 在 `llvm/lib/Transforms/IPO/ModuleInliner.cpp:87`。它不看 SCC，直接扫描整个 Module 的调用点，用 `getInlineOrder` 建优先级工作列表（`ModuleInliner.cpp:119`、`:128`）。注释明确说它「不需要 SCC 或 call graph 信息」（`ModuleInliner.cpp:10`），因此可以做全局的 inline order。

默认 pipeline 用哪个？`addModuleInlinerPass` 在 `llvm/lib/Passes/PassBuilderPipelines.cpp:353`：

```cpp
if (Opts.enable_module_inliner)
  MPM.addPass(ModuleInlinerPass(IP, Opts.enable_ml_inliner, Phase));
else
  MPM.addPass(ModuleInlinerWrapperPass(
      IP, /* MandatoryFirst */ true,
      InlineContext{Phase, InlinePass::CGSCCInliner}));
```

`enable-module-inliner` 默认 `false`（`llvm/lib/Passes/PassesOptions.td:69`），所以 `default<O2>` 默认走 `ModuleInlinerWrapperPass`，内部是 CGSCC inliner。`ModuleInlinerWrapperPass` 在 `Inliner.h:64`，构造时先放一个 `OnlyMandatory=true` 的 `InlinerPass`，再放普通的 `InlinerPass`（`Inliner.cpp:513`、`:526`、`:530`）。

## 20.3 成本模型：`InlineCost`、`InlineAdvisor`、`getInlineCost`

`InlineCost` 在 `llvm/include/llvm/Analysis/InlineCost.h:94`，核心判断只有一行（`:141`）：

```cpp
explicit operator bool() const { return Cost < Threshold; }
```

注意是严格小于。`Cost == Threshold` 不内联。`InlineCost` 还有两个哨兵：`getAlways`（`:128`）和 `getNever`（`:132`），分别对应 `INT_MIN` / `INT_MAX`，用来表达「属性强制」或「绝对不可行」。

`InlineParams` 在 `:210`，`DefaultThreshold` 默认 `-1`（`:212`）。实际值由 `getInlineParams` 计算（`llvm/lib/Analysis/InlineCost.cpp:3279`）：

```cpp
Params.DefaultThreshold = Opts.inline_threshold.value_or(Threshold);
```

默认阈值在 `llvm/lib/Analysis/AnalysisOptions.td:283`：`inlinedefault-threshold = 225`；O3 用 `OptAggressiveThreshold = 250`（`InlineCost.h:47`）。`-inline-threshold` 是 `OptionalValueField`（`AnalysisOptions.td:276`），显式给出时覆盖默认值。

`getInlineCost` 在 `InlineCost.cpp:3162`。它先调 `getAttributeBasedInliningDecision`（`:3087`）：

- `alwaysinline`：只要 `isInlineViable` 通过就返回 success（`:3112`）
- `flatten`：caller 带 `flatten` 时，可行调用全部内联（`:3132`）
- caller `optnone`：拒绝内联（`:3140`）
- callee `interposable`：拒绝（`:3144`）
- callee `noinline` / callsite `noinline`：拒绝（`:3148`、`:3152`）

属性决策返回 `nullopt` 时，才构造 `InlineCostCallAnalyzer`，遍历 callee 的指令估算 cost（`CallAnalyzer::analyze` 在 `:2809`）。分析在 `Cost >= Threshold` 时提前停止（`:1010`、`:1066`），所以 debug 输出的 cost 可能被截断。

### 阈值实测

生成一个 20 条 `add` 的 `big`，再加一个 `caller_big`：

```bash
{ echo 'define i32 @big(i32 %x) {'; for i in $(seq 1 20); do echo "  %v$i = add i32 %x, $i"; done; echo '  %r = add i32 %v20, %v19'; echo '  ret i32 %r'; echo '}'; echo; echo 'define i32 @caller_big(i32 %x) {'; echo '  %r = call i32 @big(i32 %x)'; echo '  ret i32 %r'; echo '}'; } > inline-big.ll
```

默认阈值：

```bash
$B/opt -passes='cgscc(inline)' -debug-only=inline -S inline-big.ll -o /dev/null
```

真实输出：

```text
Inlining calls in: caller_big
    Function size: 2
    Inlining (cost=70, threshold=337), Call:   %r = call i32 @big(i32 %x)
    Size after inlining: 22
Updated inlining SCC: (caller_big)
```

`threshold=337` 不是 225：`updateThreshold` 会加 single-BB bonus、乘 TTI multiplier，并加 last-call-to-static bonus（`InlineCost.cpp:1988`、`:2076`、`:2085`）。不要拿 `-inline-threshold` 的原始值当最终阈值。

对照：

```bash
$B/opt -passes='cgscc(inline)' -inline-threshold=0 -debug-only=inline -S inline-big.ll -o /dev/null
$B/opt -passes='cgscc(inline)' -inline-threshold=60 -debug-only=inline -S inline-big.ll -o /dev/null
```

真实输出：

```text
NOT Inlining (cost=0, threshold=0), Call:   %r = call i32 @big(i32 %x)
Inlining (cost=70, threshold=90), Call:   %r = call i32 @big(i32 %x)
```

第二行原文是 `%r = call i32 @big(i32 %x)`；这里只强调 cost/threshold。`threshold=0` 时 cost 打印成 0，是提前停止的欠计数，不是「真实成本为零」。同一调用在 threshold=60 时报告 70，说明 cost 只在越过阈值前被完整计算。

## 20.4 `InlineAdvisor` 与属性矩阵

`InlineAdvisor` 的 `getAdvice` 在 `llvm/include/llvm/Analysis/InlineAdvisor.h:180`，`DefaultInlineAdvisor`（`:231`）的 `getAdviceImpl` 在 `llvm/lib/Analysis/InlineAdvisor.cpp:149` 调 `getDefaultInlineAdvice`，再在 `:138` 调 `getInlineCost`。Inliner 不直接读 cost，而是问 advisor；这也是 ML inliner、replay advisor 能接入的原因。

属性矩阵实测。输入 `attrs.ll` 有 `al`（alwaysinline）、`ni`（noinline）、`on`（optnone + noinline）以及三个 caller：

```bash
$B/opt -passes='cgscc(inline)' -S attrs.ll
```

真实输出（节选）：

```llvm
define i32 @caller_al(i32 %x) {
  %a.i = add i32 %x, 1
  ret i32 %a.i
}

define i32 @caller_ni(i32 %x) {
  %r = call i32 @ni(i32 %x)
  ret i32 %r
}

define i32 @caller_on(i32 %x) {
  %r = call i32 @on(i32 %x)
  ret i32 %r
}
```

- `alwaysinline` 内联
- `noinline` 不内联
- `optnone` callee 同时带 `noinline`，不内联

`optnone` 的另一个方向是 caller：`optnone` 函数**不接收**普通内联。实测 `caller_optnone` 调 `plain` 时，debug 输出 `NOT Inlining (cost=never): optnone attribute`。

但 `alwaysinline` 在源码里先于 caller `optnone` 检查（`InlineCost.cpp:3112` 在 `:3140` 之前）。实测 `caller_optnone` 调 `al` 时仍然内联，debug 输出 `Inlining (cost=always): always inline attribute`。这是本 commit 的顺序行为；review 时看当前源码顺序，不要凭记忆。

## 20.5 内联后的属性、comdat 与 `available_externally`

### `function-attrs`

`PostOrderFunctionAttrsPass` 在 `llvm/include/llvm/Transforms/IPO/FunctionAttrs.h:51`，run 在 `llvm/lib/Transforms/IPO/FunctionAttrs.cpp:2279`。它按 post-order 对 SCC 推导属性（`deriveAttrsInPostOrder`，`:2307`），并 invalidate 被改函数及其直接调用者（`:2317`）。

实测：

```bash
$B/opt -passes='cgscc(function-attrs)' -S fa.ll
$B/opt -passes='cgscc(inline,function-attrs)' -S fa.ll
```

两次输出里 `caller` 都得到：

```llvm
; Function Attrs: mustprogress nofree norecurse nosync nounwind willreturn memory(none)
define i32 @caller(i32 %x) #0 {
```

第二次的 body 已经变成 `%a.i = add i32 %x, 1`。这个例子说明 `function-attrs` 的推导不依赖调用边界；内联后仍然要重跑，因为 caller 的 body 和 callee 的 use 都变了。

### comdat / ODR

comdat 组里的函数可能被链接器去重。删掉一个成员前，必须确认整个组都可删。`Inliner.cpp:468` 写得很直接：

```cpp
// We must ensure that we only delete functions with comdats if every function
// in the comdat is going to be deleted.
filterDeadComdatFunctions(DeadFunctionsInComdats);
```

`filterDeadComdatFunctions` 在 `llvm/lib/Transforms/Utils/ModuleUtils.cpp:325`。实测 `comdat.ll` 里 `f` 和 `g` 同属 `$c`，两个调用点都内联后：

```bash
$B/opt -passes='cgscc(inline)' -S comdat.ll
```

真实输出仍保留 `f`、`g` 和 `$c` 定义。再加上 `globaldce`（`$B/opt -passes='cgscc(inline),globaldce' -S comdat.ll`）后，真实输出只剩两个 caller，整个 comdat 组消失。内联器不会因为单个成员无 use 就拆组；组删除要等全局 DCE 确认所有成员都死。

### `available_externally`

`available_externally` 表示「定义可用于优化，但本编译单元不发射」。它在 `GlobalValue.h:455` 的分类里**不是** interposable，且在 `:468` 属于 discardable。实测：

```bash
$B/opt -passes='cgscc(inline)' -S ae.ll
```

真实输出只剩 `caller_ae`，body 是 `%a.i = add i32 %x, 4`；`ae` 被内联后整个函数消失。Review 时要区分：`available_externally` 可内联、可丢弃；`weak` / `linkonce`（非 ODR）是 interposable，默认不内联。`linkonce_odr` / `weak_odr` 不 interposable，但受 comdat 组删除规则约束。

## 20.6 实验：`cgscc(inline)`、`default<O2>`、`-print-after`

最小输入：

```llvm
define i32 @callee(i32 %x) {
  %a = add i32 %x, 1
  %b = mul i32 %a, 3
  %c = sub i32 %b, 2
  ret i32 %c
}

define i32 @caller(i32 %x) {
  %r = call i32 @callee(i32 %x)
  ret i32 %r
}
```

跑：

```bash
$B/opt -passes='cgscc(inline)' -S inline.ll
```

真实输出（节选）：

```llvm
define i32 @caller(i32 %x) {
  %a.i = add i32 %x, 1
  %b.i = mul i32 %a.i, 3
  %c.i = sub i32 %b.i, 2
  ret i32 %c.i
}
```

`callee` 仍保留，因为它是 external linkage，可能被其他 TU 使用；内联只复制 body，不删除原函数。

`-debug-only=inline`：

```bash
$B/opt -passes='cgscc(inline)' -debug-only=inline -S inline.ll -o /dev/null
```

真实输出：

```text
Inlining calls in: caller
    Function size: 2
    Inlining (cost=-20, threshold=337), Call:   %r = call i32 @callee(i32 %x)
    Size after inlining: 4
Updated inlining SCC: (caller)
```

cost 为负，是因为参数传递和 call/return 的开销被省掉，`getCallsiteCost` 给了负的起始成本。

`default<O2>` 加 `-print-after=inline`（节选）：

```bash
$B/opt -passes='default<O2>' -print-after=inline -S inline.ll -o /dev/null
```

真实输出：

```text
; *** IR Dump After InlinerPass on (callee) ***
define i32 @callee(i32 %x) local_unnamed_addr {
  %1 = mul i32 %x, 3
; *** IR Dump After InlinerPass on (caller) ***
define i32 @caller(i32 %x) local_unnamed_addr {
  %1 = mul i32 %x, 3
  %c.i = add i32 %1, 1
```

注意 print-after 按 SCC 打印：先 `(callee)`，再 `(caller)`。O2 pipeline 里 `callee` 已经被简化成 `mul` + `add`，内联把这份简化后的 body 复制进 caller。

## 20.7 失败例子：递归 `alwaysinline` 不会无限展开

```llvm
define i32 @rec(i32 %x) alwaysinline {
  %c = icmp eq i32 %x, 0
  br i1 %c, label %base, label %step
base:
  ret i32 0
step:
  %y = sub i32 %x, 1
  %r = call i32 @rec(i32 %y)
  ret i32 %r
}

define i32 @top(i32 %x) {
  %r = call i32 @rec(i32 %x)
  ret i32 %r
}
```

直觉是「`alwaysinline` 一定内联」，但递归调用会无限展开。LLVM 的实测行为：

```bash
$B/opt -passes='cgscc(inline)' -debug-only=inline -S rec.ll -o /dev/null
```

真实输出：

```text
Inlining calls in: rec
    NOT Inlining (cost=never): recursive call, Call:   %r = call i32 @rec(i32 %y)
Inlining calls in: top
    NOT Inlining (cost=never): recursive call, Call:   %r = call i32 @rec(i32 %x)
```

两个调用点都没有内联。源码判据在 `isInlineViable`（`InlineCost.cpp:3223`）：

```cpp
// Disallow recursive calls.
Function *Callee = Call->getCalledFunction();
if (&F == Callee)
  return InlineResult::failure("recursive call");
```

`CallAnalyzer` 也会在 `IsRecursiveCall && !AllowRecursiveCall` 时失败（`:2675`）。如果去掉这个检查，`rec` 会不断把自身复制进自身，函数体指数增长；即使有阈值，`alwaysinline` 也会绕过阈值。LLVM 还有一层 SCC 历史保护：`Inliner.cpp:459` 记录 `InlinedInternalEdges`，避免 SCC 拆分/合并后的无限重内联。

Review 时看到「放宽递归内联」的 diff，至少要问：

- 递归调用的深度是否有上限？
- 内联后 call 边变成 SCC 内部边，`LazyCallGraph` 的 SCC 是否被正确合并/拆分？
- 属性 `alwaysinline` 是否会让阈值失效，从而绕过成本保护？
- 测试是否覆盖「递归 + alwaysinline + 大函数体」三条同时成立的情况？

## 踩雷集锦

1. **「`alwaysinline` 保证一定内联」** → 错。递归、`blockaddress`、indirectbr、varargs 初始化、冲突 target features 都可能让 `isInlineViable` 失败。
2. **「内联只改 caller 的 IR」** → 错。它还删除 call 边、可能删除函数、拆分/合并 SCC，必须更新 `LazyCallGraph` 和 `CGSCCUpdateResult`。
3. **「cost 是精确值」** → 错。越过阈值后分析提前停止，debug 输出的 cost 可能被低估；同一调用换阈值会看到不同 cost。
4. **「`optnone` 阻止一切内联」** → 错。`alwaysinline` 在源码里先于 caller `optnone` 检查，实测仍会内联；顺序是版本相关的。
5. **「comdat 成员没 use 就可以单独删」** → 错。组删除要确认所有成员都死，`filterDeadComdatFunctions` 负责这件事。

## 动手练习

1. 把 `inline.ll` 的 `callee` 改成 `internal`，再跑 `cgscc(inline)`，观察 callee 是否被删除，并解释 `isDiscardableIfUnused` 的作用。
2. 把 `attrs.ll` 的 `al` 改成递归函数，确认 `alwaysinline` 不再内联；用 `-debug-only=inline` 找失败原因。
3. 给 `comdat.ll` 再加一个同组但仍有 use 的函数，跑 `cgscc(inline),globaldce`，确认整组不会被删。
4. 用 `-inline-threshold=0` 和 `-inline-threshold=60` 跑 `inline-big.ll`，比较 debug 输出的 cost/threshold，并解释为什么 cost 会变。

## 本章重点整理

- CGSCC 层以 `LazyCallGraph::SCC` 为单位，按 post-order 遍历；RefSCC 是变更单位。
- `InlinerPass` 是 SCC 内联器，`ModuleInlinerPass` 是模块级内联器；默认 pipeline 走前者。
- `InlineCost` 的判定是 `Cost < Threshold`；阈值会被 callsite、TTI、single-BB、last-call-to-static 等启发式调整。
- `alwaysinline` / `noinline` / `optnone` 不是单一开关，检查顺序和 caller/callee 方向都要看。
- 内联后要重跑 `function-attrs`；comdat 组删除要等全局 DCE；`available_externally` 可内联、可丢弃。

## 自我检核

- [ ] 不看笔记，画出 Module → RefSCC → SCC → Function 的层次，并说明哪一层是 InlinerPass 的工作单位
- [ ] 解释 `postorder_ref_sccs` 与 `CWorklist` 的 reverse post-order 如何配合得到 bottom-up 遍历
- [ ] 用自己的话解释 `Cost < Threshold` 为什么是严格小于，以及 cost 被截断意味着什么
- [ ] 给定一个 `alwaysinline` 递归函数，预测 `cgscc(inline)` 的行为并说出源码判据
- [ ] 解释 `optnone` 对 caller 和 callee 的两个方向，以及 `alwaysinline` 的顺序例外
- [ ] 说明 comdat 组删除为什么不能按单个函数做
- [ ] 说明 `available_externally`、`linkonce_odr`、`weak` 在内联决策上的区别
- [ ] 给出一次内联后必须更新 `CGSCCUpdateResult` 的三个字段，并说明各自作用

## 延伸阅读

- **[LLVM Programmer's Manual: The LLVM Pass Manager](https://llvm.org/docs/ProgrammersManual.html#the-llvm-pass-manager)**
  - **读哪里**：CGSCC pass manager 与 call graph 相关段落
  - **能学到什么**：把本章的 SCC 工作列表放回 New PM 的通用模型
  - **前提知识**：Ch 13 的 PassBuilder、Ch 16 的 invalidation
- **[LLVM Source: `LazyCallGraph.h`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/Analysis/LazyCallGraph.h)**
  - **读哪里**：`SCC`、`RefSCC`、`switchInternalEdgeToCall` 的注释
  - **能学到什么**：SCC 合并/拆分的精确语义，以及为什么变更要通过 RefSCC
  - **前提知识**：本章的 post-order 遍历
- **[LLVM Source: `InlineCost.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Analysis/InlineCost.cpp)**
  - **读哪里**：`getAttributeBasedInliningDecision`、`CallAnalyzer::analyze`、`updateThreshold`
  - **能学到什么**：本章没展开的 cost-benefit、hot/cold callsite、vector bonus
  - **前提知识**：Ch 20.3 的 cost/threshold 基础
内联的 review 难点不在「该不该内联」，而在「内联之后调用图、SCC、属性是否仍然自洽」。下一章转向冗余消除：`GVN`、`EarlyCSE`、`NewGVN` 如何在不同的代价模型下证明「这个值已经算过」。

→ [Ch 21 — `GVN` / `EarlyCSE` / `NewGVN`：冗余消除的三种代价模型](./21-gvn-earlycse.md)
