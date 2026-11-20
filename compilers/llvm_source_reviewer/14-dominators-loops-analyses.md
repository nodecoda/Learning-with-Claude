# Ch 14 — 支配树、循环与 CFG 分析：`DominatorTree`、`LoopInfo`、`CycleInfo`

> **目标**：把「支配关系」「自然循环」「任意环」拆成三种可验证的 CFG 分析，并说清楚它们的失效条件。学完你能在 review 里判断一次 CFG 修改是否必须更新 `DominatorTree`、`getLoopLatch` 为什么会返回 null、以及 `renumberBlocks` 为什么会让 `CycleInfo` 在 assertions 构建里直接断言失败。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验构建 `/home/ubuntu/llvm/builds/pr-lane`（`RelWithDebInfo`，assertions ON）。本章的 `opt` 输出和 C++ 复现都在这个构建上真实运行。

## 为什么需要这个？

CFG 分析是 review 里最容易「看起来对」的一类改动。一个 pass 可能只加了一条边、删了一个块，代码仍然编译、`opt` 也不报错，但缓存的支配树已经和 CFG 不一致。后面的 pass 用这棵旧树做 `dominates` 查询，会把一个不支配某点的定义当成支配，从而删掉不该删的指令。

三种分析回答三个不同问题：

| 分析 | 回答的问题 | 关键限制 |
| --- | --- | --- |
| `DominatorTree` | 从入口到某块的所有路径是否都经过 A | 不可达块不在树里；IR 变更后必须更新或失效 |
| `LoopInfo` | 哪些块构成自然循环，header/latch/preheader 是谁 | 不识别不可规约（irreducible）控制流 |
| `CycleInfo` | CFG 里有哪些环，包括不可规约环 | 用 block number 做索引，换号后缓存失效 |

`LoopInfo` 和 `CycleInfo` 的差别不是「新旧 API」：前者服务循环优化，只表示自然循环；后者需要任意环，包括不可规约的情况。把它们混用，会在不可规约 CFG 上得到空结果。

## 先建立直觉

看一个三块函数：

```
entry
  │
  ├──► a ──► b
  └──► c ──► b
```

`DominatorTree` 记录的是「必经点」：`entry` 支配所有块；`b` 被 `entry` 支配，但不被 `a` 或 `c` 单独支配，因为两条路径都能到 `b`。用树表示：

```
entry
 ├── a
 ├── b
 └── c
```

树里 `a` 和 `b` 是兄弟，说明 `a` 不支配 `b`。如果 pass 删掉 `c -> b` 却没更新 DT，旧树会说 false（假阴性）；如果从只有 `a -> b` 的状态加上 `c -> b` 却没更新，旧树会说 true（假阳性）。假阳性更危险：后面的变换会据此把 `%x` 搬到 `b` 之外。

循环的直觉是「回边」：

```
entry
  │
  ▼
header ◄── latch
  │         │
  ▼         │
body ───────┘
```

`header` 是回边的目标，`latch` 是回边的来源，`preheader` 是 header 在循环外的唯一前驱。`LoopInfo` 找的是这种自然循环；如果控制流有多个入口互相跳，`LoopInfo` 不会把它识别成循环，`CycleInfo` 会。

## `DominatorTreeBase`、`DominatorTree`、`PostDominatorTree`

通用实现是 `DominatorTreeBase<NodeT, IsPostDom>`（`llvm/include/llvm/Support/GenericDomTree.h:263`）。节点类型 `DomTreeNodeBase`（`:57`）保存 `TheBB`、`IDom`、`Level`、子节点链表和 DFS 编号。IR 层的 `DominatorTree` 是 `DominatorTreeBase<BasicBlock, false>`（`llvm/include/llvm/IR/Dominators.h:122`）；后向支配树 `PostDominatorTree` 在另一个头文件 `llvm/include/llvm/Analysis/PostDominators.h:29`，不要因为名字相似就以为它也在 `Dominators.h`。

常用查询：

- `getNode(BB)`（`GenericDomTree.h:398`）返回节点，不可达块返回 null；`getRootNode()`（`:418`）返回入口节点。`Dominators.h:115-120` 的注释明确说：构造时不可达的块不会进树。
- `dominates(A, B)`（`:505`）判断块支配；IR 层还有 `dominates(const Value*, const Use&)`（`Dominators.h:151`），它处理 PHI 时会看 incoming block，而不是 PHI 所在块（`lib/IR/Dominators.cpp:80-87`）。这一点在写变换时经常决定查询结果。
- `recalculate(F)`（`:809`）重建整棵树。

构造算法不是「从入口做一次 DFS」。`GenericDomTreeConstruction.h:24` 的注释说，完整构造走 SLT（Simple Lengauer-Tarjan），增量更新走 SemiNCA（`runSemiNCA` 在 `:270`）。完整重建是 O(N) 级图遍历；增量更新依赖你提交的边变化，提交错误的 update 会让树以「看起来合法」的方式错掉。

`DominatorTreeAnalysis`（`Dominators.h:241`）是 New PM 的包装，`DominatorTreePrinterPass`（`:254`）负责 `print<domtree>`。printer pass 返回 `PreservedAnalyses::all()`，因为它只读。

## `DomTreeUpdater`：改 CFG 时的正确入口

`DomTreeUpdater`（`llvm/include/llvm/Analysis/DomTreeUpdater.h:32`）继承 `GenericDomTreeUpdater`，同时管理 `DominatorTree` 和 `PostDominatorTree`。它有两种策略（`GenericDomTreeUpdater.h:31`）：

- `Eager`：每次 `applyUpdates` 立即改树。
- `Lazy`：先排队，`flush()` 时统一处理；适合一次变换里提交大量边变化。

关键 API 是 `applyUpdates`（`:150`）、`getDomTree()`（`:192`）、`flush()`（`:202`）。析构会 `flush()`（`DomTreeUpdater.h:43`），基类析构再断言没有残留更新（`GenericDomTreeUpdater.h:54`）；Lazy 模式真正危险的是在 `flush()` 之前查询，此时树还是旧状态，而不是析构会 abort。

提交的 update 是 `cfg::Update<BasicBlock*>`，形如 `{DominatorTree::Insert, From, To}` 或 `{DominatorTree::Delete, From, To}`。规则是「你实际删了哪条边、加了哪条边，就报哪条」。如果先把 terminator 删掉再重建同样的边，也要先报 Delete 再报 Insert；只报 Insert 会让更新器以为旧边还在，树的结构就会和 CFG 分叉。

不想维护 update 时，退路是 `DT.recalculate(F)` 或让 pass 返回 `PreservedAnalyses::none()`，由 manager 丢掉缓存。代价是性能；`DomTreeUpdater` 存在的意义是避免在热路径上反复全量重建。Ch 19 会看 `SimplifyCFG` 怎么用它。

## `LoopInfo` 与 `Loop`

`Loop` 是 `LoopBase<BasicBlock, Loop>`（`llvm/include/llvm/Analysis/LoopInfo.h:40`），`LoopInfo` 是 `LoopInfoBase<BasicBlock, Loop>`（`:437`）。`LoopAnalysis::run`（`lib/Analysis/LoopInfo.cpp:1026`）构造 `LoopInfo`，并在需要时通过 `AM.getResult<DominatorTreeAnalysis>` 拿 DT：

```cpp
LoopInfo LI;
LI.analyze(&F, [&]() -> const DominatorTree & {
  return AM.getResult<DominatorTreeAnalysis>(F);
});
return LI;
```

源码注释说 DT 现在只在不可规约 CFG 上才需要；这和你记忆中「LoopInfo 一定要 DT」的旧模型不同，读这个版本的代码要以注释和实现为准。

`LoopInfo` 只找自然循环。`GenericLoopInfo.h:187-190` 的注释明确说：它不检测不可规约控制流，只检测自然循环；因此「最内层循环」内部或「最外层循环」周围仍可能存在未被表示的环。

三个最常用的结构查询：

- `getLoopPreheader()`（`GenericLoopInfo.h:310`）：header 在循环外只有一个前驱，且该前驱只有一个后继。实现还会检查「是否允许把指令 hoist 进去」（`GenericLoopInfoImpl.h:211-232`）。任一条件不满足就返回 null。
- `getLoopLatch()`（`:320`）：header 在循环内只有一个前驱。多个回边就返回 null（`GenericLoopInfoImpl.h:257-270`）。
- `getExitingBlock()`（`:276`）：循环内有且只有一个块能跳出循环。多个 exiting block 就返回 null。

`isLoopSimplifyForm()`（`LoopInfo.h:328`）的定义在 `lib/Analysis/LoopInfo.cpp:505-509`：

```cpp
return getLoopPreheader() && getLoopLatch() && hasDedicatedExits();
```

也就是说，simplify form 要求 preheader、单 latch、dedicated exits 同时成立。很多循环 pass 在 `run` 开头断言这个条件；`isLoopSimplifyForm()` 失败时，先查这三个分量。

`Loop` 还提供 `getSubLoops()`、`getLoopDepth()`、`getHeader()`、`getBlocks()`；`getExitingBlock` 只统计当前循环的块，不会把内层循环的出口算进来。

## `CycleInfo` 与 block number epoch

`GenericCycleInfo<ContextT>`（`llvm/include/llvm/ADT/GenericCycleInfo.h:80`）是任意环的通用实现，IR 层的 `CycleInfo` 是 `GenericCycleInfo<SSAContext>`（`llvm/include/llvm/IR/CycleInfo.h:26`）。它的存储和 `LoopInfo` 不同：

- 环用 `CycleRef` 句柄表示（`GenericCycleInfo.h:55`），不是裸指针。
- 所有环按 preorder 存在一个数组里，`cycles()` 遍历句柄。
- `BlockMap` 按 block number 索引，记录每个块最内层所属的环；`getCycle(BB)`（`:210`）先 `verifyBlockNumberEpoch`，再 `BlockMap[Number]`。

epoch 的来源是 `Function::renumberBlocks()`（`lib/IR/Function.cpp:64-71`）：它把块编号压紧并从 0 重排，然后 `BlockNumEpoch++`。`Function.h:825-828` 警告：其他分析和数据结构可能依赖这些编号，需要更新或失效。`DominatorTreeBase` 也保存 `BlockNumberEpoch`（`GenericDomTree.h:305`）并提供 `updateBlockNumbers()`（`:814`），但 `renumberBlocks()` 不会自动调用它。

`CycleInfo` 在计算时记下 epoch（`GenericCycleImpl.h:565-569`）：

```cpp
BlockNumberEpoch = GraphTraits<FunctionT *>::getNumberEpoch(&F);
BlockMap.assign(GraphTraits<FunctionT *>::getMaxNumber(&F), CycleRef());
```

之后每次按块查询都检查 epoch（`GenericCycleInfo.h:158-160`）：

```cpp
assert(BlockNumberEpoch ==
           GraphTraits<const FunctionT *>::getNumberEpoch(Fn) &&
       "CycleInfo used with outdated block number epoch");
```

`LoopInfo` 有同样的检查（`GenericLoopInfo.h:630-637`，错误文本是 `loop info used with outdated block numbers`）。所以 `renumberBlocks` 不是「整理一下编号」这么无害的操作；它让所有按编号建索引的缓存同时过期。

### 真实 commit：`81e0dbceab0d`

```bash
cd /home/ubuntu/llvm/llvm-project
git show --stat --oneline 81e0dbceab0d
```

实测：

```text
81e0dbceab0d [coro] Remove renumberBlocks to fix assert (#244573)
 llvm/lib/Transforms/Coroutines/CoroSplit.cpp | 2 --
 1 file changed, 2 deletions(-)
```

commit message 说：`GenericCycleInfo` 没有被失效，所以在 profcheck 开启时断言 block number epoch 过期；修复是删掉 `renumberBlocks` 调用。diff 删除的两行是：

```cpp
// Renumber basic blocks so that they are dense for crossing value analysis.
F.renumberBlocks();
```

修复方式不是「补一次 invalidation」，而是直接取消 renumber：`GenericCycleInfo` 已经把块编号当索引，编号本身足够密，为「更密」而重排的收益不足以抵消让所有编号缓存失效的风险。一个看起来只是整理数据的调用，实际改变了多个 analysis 的隐含前提。

## 实测：三种 printer

先确认当前构建里的 pass 名，不要凭记忆写：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt --print-passes | rg '^  print<(domtree|loops|cycles)>$'
```

实测：

```text
  print<cycles>
  print<domtree>
  print<loops>
```

输入 `loop.ll`：

```llvm
define i32 @sum(i32 %n) {
entry:
  br label %header
header:
  %i = phi i32 [ 0, %entry ], [ %i.next, %latch ]
  %acc = phi i32 [ 0, %entry ], [ %acc.next, %latch ]
  %cmp = icmp slt i32 %i, %n
  br i1 %cmp, label %body, label %exit
body:
  %acc.next = add i32 %acc, %i
  br label %latch
latch:
  %i.next = add i32 %i, 1
  br label %header
exit:
  ret i32 %acc
}
```

```bash
$B/opt -passes='function(print<domtree>)' -disable-output loop.ll
$B/opt -passes='function(print<loops>)' -disable-output loop.ll
$B/opt -passes='function(print<cycles>)' -disable-output loop.ll
```

实测输出：

```text
DominatorTree for function: sum
=============================--------------------------------
Inorder Dominator Tree: DFSNumbers invalid: 0 slow queries.
  [1] %entry {4294967295,4294967295} [0]
    [2] %header {4294967295,4294967295} [1]
      [3] %body {4294967295,4294967295} [2]
        [4] %latch {4294967295,4294967295} [3]
      [3] %exit {4294967295,4294967295} [2]
Roots: %entry

Loop info for function 'sum':
Loop at depth 1 containing: %header<header><exiting>,%body,%latch<latch>

CycleInfo for function: sum
    depth=1: entries(header) body latch
```

`print<loops>` 的标记直接对应查询：`<header>` 是 `getHeader()`，`<latch>` 是 `getLoopLatch()`，`<exiting>` 表示这个块有边跳出循环。`print<cycles>` 的 `entries(header)` 表示这个环只有一个入口，也就是可规约环。

再看不可规约 CFG `irr.ll`：

```llvm
define void @irr(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  br i1 %c, label %cb, label %exit
b:
  br label %cb
cb:
  br i1 %c, label %a, label %exit
exit:
  ret void
}
```

```bash
$B/opt -passes='function(print<loops>)' -disable-output irr.ll
$B/opt -passes='function(print<cycles>)' -disable-output irr.ll
```

实测：

```text
Loop info for function 'irr':

CycleInfo for function: irr
    depth=1: entries(cb a)
```

`LoopInfo` 一行都没有，`CycleInfo` 报 `entries(cb a)`（两个入口）。这不是工具坏了，而是两种分析的契约不同。review 里看到某个 pass 在不可规约图上「找不到循环」，先确认它用的是 `LoopInfo` 还是 `CycleInfo`。

最后看多个 exiting block 的情况。把 `loop.ll` 的 `body` 改成 `br i1 %brk, label %exit, label %cont`，加一个 `cont: br label %latch`，存成 `multi.ll`，然后跑同一个 printer：

```bash
$B/opt -passes='function(print<loops>)' -disable-output multi.ll
```

实测：

```text
Loop info for function 'sum':
Loop at depth 1 containing: %header<header><exiting>,%body<exiting>,%cont,%latch<latch>
```

两个 `<exiting>` 说明 `getExitingBlock()` 会返回 null；代码必须用 `getExitingBlocks()` 拿列表。只判断 `getExitingBlock() != nullptr` 的 pass，在这类循环上会静默走另一条路径。

## 失败例子：改 CFG 不更新 DT

下面这段程序在 `pr-lane` 上真实编译运行。它先建一棵正确的 DT，然后加一条绕过 `a` 的路径，故意不更新 DT。

```cpp
#include "llvm/AsmParser/Parser.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Dominators.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

int main() {
  LLVMContext C;
  SMDiagnostic Err;
  auto M = parseAssemblyString(R"(
define i32 @f(i1 %c) {
entry:
  br label %a
a:
  %x = add i32 1, 2
  br label %b
b:
  %y = add i32 %x, 0
  ret i32 %y
}
)",
                               Err, C);
  if (!M) {
    Err.print("dt_stale", errs());
    return 1;
  }
  Function &F = *M->getFunction("f");
  BasicBlock &Entry = F.getEntryBlock();
  auto It = F.begin();
  ++It;
  BasicBlock &A = *It++;
  BasicBlock &B = *It++;

  DominatorTree DT(F);
  Instruction *X = &A.front();
  Use &U = B.front().getOperandUse(0);
  errs() << "before mutation: a dominates use of x in b = "
         << DT.dominates(X, U) << "\n";

  // Add entry -> side -> b, so a no longer dominates b.
  BasicBlock *Side = BasicBlock::Create(C, "side", &F, &B);
  Entry.getTerminator()->eraseFromParent();
  CondBrInst::Create(&*F.getArg(0), &A, Side, &Entry);
  UncondBrInst::Create(&B, Side);

  errs() << "after mutation (DT not updated): a dominates use of x in b = "
         << DT.dominates(X, U) << "\n";
  errs() << "DT.verify() = " << DT.verify() << "\n";
  return 0;
}
```

编译命令：

```bash
B=/home/ubuntu/llvm/builds/pr-lane
$B/bin/clang++ $($B/bin/llvm-config --cxxflags) dt_stale.cpp -o dt_stale \
  $($B/bin/llvm-config --ldflags) $($B/bin/llvm-config --libs core irreader analysis support) \
  $($B/bin/llvm-config --system-libs) -Wl,-rpath,$B/lib
./dt_stale
```

实测输出（`DT.verify()` 的树形打印有省略）：

```text
before mutation: a dominates use of x in b = 1
after mutation (DT not updated): a dominates use of x in b = 1
DT.verify() = DominatorTree is different than a freshly computed one!
	Current:              [1] entry -> [2] a -> [3] b
	Freshly computed:     [1] entry -> {[2] a, [2] b, [2] side}
0
```

错误在第二行：加边之后 `a` 已不支配 `b`，旧 DT 仍返回 `1`。真实 pass 可能据此把 `%x` 视为「在 `b` 处可用」并生成非法 IR；verifier 要到运行 `verify` 时才抓到 `Instruction does not dominate all uses!`。

正确做法是让 `DomTreeUpdater` 知道每条边变化。文件顶部补 `#include "llvm/Analysis/DomTreeUpdater.h"`，把构造 `Side` 之后的代码改成：

```cpp
DomTreeUpdater DTU(DT, DomTreeUpdater::UpdateStrategy::Eager);
Entry.getTerminator()->eraseFromParent();
CondBrInst::Create(&*F.getArg(0), &A, Side, &Entry);
UncondBrInst::Create(&B, Side);
DTU.applyUpdates({{DominatorTree::Delete, &Entry, &A},
                  {DominatorTree::Insert, &Entry, &A},
                  {DominatorTree::Insert, &Entry, Side},
                  {DominatorTree::Insert, Side, &B}});
```

同一程序补上这段后的实测输出：

```text
before mutation: a dominates use of x in b = 1
after mutation (DTU applied): a dominates use of x in b = 0
DT.verify() = 1
```

`DT.verify()` 返回 true，说明当前树和重新计算的树一致。注意 Delete + Insert 的顺序：我们确实先删除了 `entry -> a` 的无条件边，再加回条件边；只报 Insert 会让更新器把旧边和新边当成同一条，树的结构就会错。

## 失效的直觉

三种分析都有各自的 `invalidate`，判断条件形状相同：

```cpp
auto PAC = PA.getChecker<DominatorTreeAnalysis>();
return !(PAC.preserved() || PAC.preservedSet<AllAnalysesOn<Function>>() ||
         PAC.preservedSet<CFGAnalyses>());
```

`Dominators.cpp:71-78`、`LoopInfo.cpp:935-942`、`CycleAnalysis.cpp:37-43` 是同一模式。直觉是：**只要 pass 承诺保留了 CFG，这些分析就可以留下；否则 manager 删掉缓存，下次 `getResult` 重建**。

这不等于「保留 CFG 就一定安全」：`renumberBlocks` 不改边，却让按编号建索引的分析过期，`CycleInfo` 的 epoch 断言就是为这种情况准备的。`PreservedAnalyses` 的 API 细节和 `CFGAnalyses` 的构造留给 Ch 16。

## 踩雷集锦

1. **以为不可达块也在支配树里**。`getNode` 对不可达块返回 null（`Dominators.h:115-120`）；`dominates` 对 null 节点的行为不是「false」，而是调用方必须先处理可达性。写变换前先 `isReachableFromEntry`。
2. **改了 CFG 只更新 DT，不更新 LoopInfo/CycleInfo**。三个分析各自缓存；`DomTreeUpdater` 只管 DT/PDT。循环结构变了要同时让 `LoopInfo` 失效，否则 `getLoopLatch` 会返回旧结果。
3. **把 `getLoopLatch()` 的 null 当成「没有循环」**。多个回边也会返回 null；用 `getLoopLatches()` 拿全部，再判断数量。
4. **认为 `renumberBlocks()` 只是整理编号**。它会 `BlockNumEpoch++`，让所有按编号索引的分析（`LoopInfo`、`CycleInfo`）在下次查询时断言失败；commit `81e0dbceab0d` 就是删掉一个这样的调用。
5. **在 Lazy `DomTreeUpdater` 下查询前忘了 `flush()`**。析构会自动 flush，所以程序不一定崩；但 flush 之前的 `getDomTree()` 仍然是旧树，查询结果会静默过期。

## 本章重点整理

- `DominatorTree` 对不可达块返回 null；`dominates` 的前提是双方可达，调用方要先做 `isReachableFromEntry`。
- 改 CFG 的唯一正确入口是 `DomTreeUpdater`：成对提交 Delete/Insert，顺序不能反；Lazy 模式下查询前要 `flush()`。
- `LoopInfo` 只描述自然循环；`getLoopLatch()` 在多回边时返回 null，要拿全部回边用 `getLoopLatches()`。
- `CycleInfo` 描述任意环，能处理不可规约 CFG；它按 block number 建索引，`renumberBlocks()` 递增 epoch 会让旧结果在断言构建里直接失败。
- 三种分析的 `invalidate` 形状相同：没保留 CFG 就丢缓存；但「保留了 CFG」不等于「结果还新鲜」，编号变化是反例。
- commit `81e0dbceab0d` 删掉 coro 里的 `renumberBlocks` 调用，正是为了让按编号索引的分析不被无声作废。

## 自我检核

- [ ] 能解释 `DominatorTree` 为什么对不可达块返回 null，以及这对 `dominates` 查询意味着什么
- [ ] 能画出 `entry -> {a,c} -> b` 的支配树，并判断 `a` 是否支配 `b`
- [ ] 能说出 `getLoopPreheader`、`getLoopLatch`、`getExitingBlock` 各自在什么条件下返回 null
- [ ] 能写出 `isLoopSimplifyForm()` 的三个组成条件，并在 `LoopInfo.cpp` 找到定义
- [ ] 能解释 `LoopInfo` 和 `CycleInfo` 在不可规约 CFG 上的输出差异，以及为什么
- [ ] 能说明 `renumberBlocks()` 如何让 `CycleInfo` 的 epoch 检查失败，并复述 commit `81e0dbceab0d` 的修复方式
- [ ] 能说出 `DomTreeUpdater` 的 Eager/Lazy 差异，以及 Lazy 模式忘记 `flush()` 会发生什么
- [ ] 给定一次「加了一条边」的 CFG 修改，能列出必须提交的 `Insert`/`Delete` update

## 延伸阅读

- **[LLVM Programmer's Manual: Dominator Trees](https://llvm.org/docs/ProgrammersManual.html#dominator-trees)**
  - **读哪里**：`DominatorTree` 查询接口与 `DomTreeUpdater` 段落
  - **能学到什么**：`findNearestCommonDominator` 等本章未展开的 API，以及批量更新的约束
  - **前提**：读完本章的 `DominatorTreeBase` 结构和两种更新策略
- **[LLVM Loop Terminology](https://llvm.org/docs/LoopTerminology.html)**
  - **读哪里**：header、latch、preheader、simplify form、dedicated exits 几节
  - **能学到什么**：本章 `isLoopSimplifyForm()` 背后的术语定义；读完再回看 `print<loops>` 的标记会更快
  - **前提**：知道基本块和 CFG 的前驱/后继；不要求读过循环优化 pass
- **[`llvm/include/llvm/ADT/GenericCycleInfo.h`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/ADT/GenericCycleInfo.h)**
  - **读哪里**：`CycleRef`、`BlockMap`、`verifyBlockNumberEpoch`、`getCycle` 四处
  - **能学到什么**：为什么环用句柄而不是指针、为什么 block number 是索引、epoch 断言保护的是什么
  - **前提**：读完本章的 `CycleInfo` 一节；知道 `Function::renumberBlocks` 会递增 epoch

→ [Ch 15 — 标量演化、别名与内存：`ScalarEvolution`、`AliasAnalysis`、`MemorySSA`](./15-scalarevolution-alias-memoryssa.md)
