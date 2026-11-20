# Ch 19 — `SimplifyCFG` 与 CFG 变换：`DomTreeUpdater` 的正确用法

> **目标**：把「删一个空块」看成一次同时修改 CFG、phi、支配树、循环元数据和调试位置的事务。学完你能读懂 `SimplifyCFGPass` 的迭代结构，并在 review 中指出某次块合并漏了哪条更新、哪个 phi incoming 没迁移，以及 `blockaddress` 为什么让某些变换必须退让。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project` @ `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`（assertions ON）。本章命令与输出都在这个构建上真实运行。行号会漂，符号和路径更稳定。

## 为什么需要这个？

CFG 变换的 bug 很少表现为「这一行算错了」，而是：

```
pass 删掉一个块
   → 某个 phi 还引用旧前驱
   → 某个 blockaddress 指向已删除的标签
   → DomTree 仍认为旧边存在
   → 三个 pass 之后，Verifier 或某个 analysis 才炸
```

diff 里往往只差一行 `DTU->applyUpdates`，或 `removePredecessor` 调晚了一步。按「CFG 修改事务」逐条检查，问题会自己浮出来。

## 先建立直觉

```
CFG 修改（IR 先变）
   ├── phi：删 incoming / 改前驱 / 折叠单入口 phi
   ├── DomTreeUpdater：提交 Insert/Delete 边
   ├── blockaddress：拒绝删除，或 RAUW 到新块
   ├── llvm.loop：搬到新 latch，或拒绝合并
   └── DebugLoc：合并成 common location
   ↓
Verifier + DT->verify() 复查
```

## 19.1 `SimplifyCFGPass` 与 `SimplifyCFGOptions`

入口在 `llvm/include/llvm/Transforms/Scalar/SimplifyCFG.h:30`：

```cpp
class SimplifyCFGPass : public OptionalPassInfoMixin<SimplifyCFGPass> {
  SimplifyCFGOptions Options;
public:
  SimplifyCFGPass();
  SimplifyCFGPass(const SimplifyCFGOptions &PassOptions);
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM);
};
```

默认构造函数创建 **canonical IR**，不做「可能更快但让其他分析更难」的变换（同文件 `:36`）。所以 `opt -passes=simplifycfg` 与 `default<O2>` 里的同名 pass 可能行为不同。

选项在 `llvm/include/llvm/Transforms/Utils/SimplifyCFGOptions.h:24`：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `HoistCommonInsts` | `false` | 共同指令上提到父块 |
| `SinkCommonInsts` | `false` | 共同指令下沉到汇合块 |
| `ConvertSwitchToLookupTable` | `false` | switch 转查表 |
| `NeedCanonicalLoop` | `true` | 保持可分析的循环结构 |
| `SpeculateBlocks` | `true` | 允许安全指令投机 |

命令行覆盖由 `parseSimplifyCFGOptions` 处理（`llvm/lib/Passes/PassBuilder.cpp:1222`），支持 `simplifycfg<no-speculate-blocks;hoist-common-insts>`。后面的对照实验用它，避免依赖旧 flag 的默认值。

## 19.2 pass 骨架：两层循环

`SimplifyCFGPass::run` 在 `llvm/lib/Transforms/Scalar/SimplifyCFGPass.cpp:340`。`simplifyFunctionCFGImpl`（`:234`）的顺序是：

1. `removeUnreachableBlocks`（`:241`）
2. `tailMergeBlocksWithSimilarFunctionTerminators`（`:242`）
3. `iterativelySimplifyCFG`（`:243`）
4. 若仍有变化，在「迭代化简」和「删不可达」之间循环到不动点（`:258`）

顺序不是随意的：删不可达会让更多块变成单前驱，tail merge 会制造汇合块，块内化简又可能让循环退出块变成死块。`iterativelySimplifyCFG`（`:191`）对每个块调 `simplifyCFG`，并带一个保险丝 `assert(IterCnt++ < 2000 && "Iterative simplification didn't converge!")`，防止不收敛。

单块内部还有一层重试，`SimplifyCFGOpt::run` 在 `llvm/lib/Transforms/Utils/SimplifyCFG.cpp:9510`：

```cpp
do {
  Resimplify = false;
  Changed |= simplifyOnce(BB);
} while (Resimplify);
```

`requestResimplify()` 表示「刚改过，可能又有新机会」。读代码要区分：**外层跨块，内层跨同一块的连续化简**。

## 19.3 每次改 CFG 都要过 `DomTreeUpdater`

`SimplifyCFG` 用 Eager 策略（`SimplifyCFGPass.cpp:239`）：

```cpp
DomTreeUpdater DTU(DT, DomTreeUpdater::UpdateStrategy::Eager);
```

Eager 表示 `applyUpdates` 提交后立即刷新 DomTree。这里选 Eager，是因为 pass 在每个块上反复查询支配关系，并希望 full verification 尽早抓住错误。`SimplifyCFGPass.cpp:273` 和 `:279` 两次检查：

```cpp
assert((!RequireAndPreserveDomTree ||
        (DT && DT->verify(DominatorTree::VerificationLevel::Full))) &&
       "Failed to maintain validity of domtree!");
```

`enum class UpdateStrategy { Eager, Lazy }` 在 `llvm/include/llvm/Analysis/GenericDomTreeUpdater.h:31`。Eager 立即刷；Lazy 先排队，等 `flush()` / `getDomTree()` 再刷。Lazy 适合连续提交大量更新、最后统一查询；`llvm/include/llvm/Analysis/DomTreeUpdater.h:66` 说明 Lazy 下 `deleteBB` 会排队，且「Assert if any instruction of DelBB is modified while awaiting deletion」。等待期间不能再改块内指令，这是生命周期契约。

> Lazy 的删除时机来自 `DomTreeUpdater.h:66` 与 `GenericDomTreeUpdater.h:117` 的源码契约；本章没有构造自定义 C++ pass 实测队列时序，标为「未实测，源码契约」。

`applyUpdates` 的三条契约在 `GenericDomTreeUpdater.h:142`：

1. IR 必须先改，再提交边更新；更新器读当前 CFG 判断边是否合法。
2. 不能重复提交同一条边：不能插入已存在的边，也不能删除不存在的边。
3. 同一条边按 CFG 变化顺序提交；不同边之间没有顺序限制。

所以 review 看到「先 `applyUpdates`、后 `setSuccessor`」就是 blocker。正确顺序是改 IR，再提交 `Delete Pred->BB` / `Insert Pred->NewSucc`。

常量分支的标准入口是 `llvm::ConstantFoldTerminator`（`llvm/lib/Transforms/Utils/Local.cpp:133`）。它把「改 successor、更新 phi、搬 metadata、提交 DTU」绑在一起：`OldDest->removePredecessor(BB)`（`:175`）、`copyMetadata` 搬运 `MD_loop`/`MD_dbg`/`MD_annotation`（`:182`）、提交 `Delete BB->OldDest`（`:184`）。绕过它手写「只改 successor」的快捷路径，就要自己补齐同样的三件事。

## 19.4 变换目录

| 变换 | 入口 | 关键不变量 |
| --- | --- | --- |
| 空块合并 | `TryToSimplifyUncondBranchFromEmptyBlock`，`Local.cpp:1185` | 非 entry、非自环、phi 不冲突、循环元数据 |
| 不可达删除 | `removeUnreachableBlocks`，`Local.cpp:2961` | 从 entry 做可达性，连 phi 一起清 |
| switch 化简 | `simplifySwitch`，`SimplifyCFG.cpp:8695` | 死 case、重复目标、范围 |
| 条件分支合并 | `simplifyCondBranch`，`:9069` | 前驱已知值、`a && b` |
| 投机 | `speculativelyExecuteBB`，`:3303` | 安全、无副作用、成本预算 |
| hoist / sink | `:1891` / `:2463` | 后继无 phi、无 `blockaddress`；phi incoming 映射 |

几个细节：

- `eliminateDeadSwitchCases`（`:6456`）用 `computeKnownBits` / `collectPossibleValues` 推断死 case，不只看常量。
- `simplifyDuplicateSwitchArms`（`:8668`）合并相同目标。
- 投机最多一条指令，且要过 `isSafeToSpeculativelyExecute`（`:3376`）和 `PHINodeFoldingThreshold * TCC_Basic` 预算（`:3311`）。
- hoist 遇到 `Succ->hasAddressTaken()` 直接放弃（`:1909`）：从地址跳进来时，被 hoist 的指令原本不会执行。
- hoist/sink 默认关闭；但 `AllInstsEqOnly=true` 时，「整块完全相同」的 hoist 仍会发生。

## 19.5 实验：空块、switch、DomTree

输入（省略了中间的 `@sw`，完整文件里它在 `@empty` 与 `@deadcase` 之间）：

```llvm
define i32 @empty(i1 %c) {
entry:
  br i1 %c, label %then, label %else
then:
  br label %merge
else:
  br label %merge
merge:
  ret i32 42
}

define i32 @deadcase(i32 %x) {
entry:
  %m = and i32 %x, 1
  switch i32 %m, label %default [
    i32 0, label %even
    i32 1, label %odd
    i32 2, label %dead
  ]
even:
  ret i32 0
odd:
  ret i32 1
dead:
  ret i32 99
default:
  ret i32 7
}
```

跑：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt -passes=simplifycfg -S empty-switch.ll
```

真实输出（节选）：

```llvm
define i32 @empty(i1 %c) {
entry:
  ret i32 42
}

define i32 @deadcase(i32 %x) {
entry:
  %m = and i32 %x, 1
  %switch.selectcmp = icmp eq i32 %m, 0
  %switch.select = select i1 %switch.selectcmp, i32 0, i32 1
  ret i32 %switch.select
}
```

`then` / `else` / `merge` 都是空块，最终折成一条 `ret`；`dead` case 被 known bits 证明不可能，switch 变成 `select`。

DomTree 前后：

```bash
$B/opt -passes='print<domtree>,simplifycfg,print<domtree>' -S dom.ll
```

真实输出：

```text
DominatorTree for function: f
  [1] %entry {4294967295,4294967295} [0]
    [2] %then {4294967295,4294967295} [1]
    [2] %merge {4294967295,4294967295} [1]
    [2] %else {4294967295,4294967295} [1]
DominatorTree for function: f
  [1] %entry {4294967295,4294967295} [0]
```

前一张图三个块都是 `entry` 的孩子，后一张只剩 `entry`。如果某个 diff 只删 DT 节点、不处理孩子，这种前后对比会立刻暴露。

### `-debug-only=simplifycfg`

对嵌套条件分支：

```llvm
declare void @f()
define void @nested(i1 %a, i1 %b) {
entry:
  br i1 %a, label %then, label %exit
then:
  br i1 %b, label %body, label %exit
body:
  call void @f()
  br label %exit
exit:
  ret void
}
```

跑：

```bash
$B/opt -passes=simplifycfg -debug-only=simplifycfg -S condmerge.ll -o /dev/null
```

真实输出（节选）：

```text
FOLDING BRs:
entry:
  br i1 %a, label %then, label %exit
AND:
then:
  br i1 %b, label %body, label %exit
...
INTO:
entry:
  %a.not = xor i1 %a, true
  %b.not = xor i1 %b, true
  %brmerge = select i1 %a.not, i1 true, i1 %b.not
  br i1 %brmerge, label %exit, label %body
```

打印的是 `select` 而不是 `and`，后面的 `InstCombine` 可能再规范成 `and`。`-debug-only` 看到的是当前 pass 的中间形态。另外，`MergeBlockIntoPredecessor` 的 `LLVM_DEBUG` 使用另一个 `DEBUG_TYPE`，所以 `-debug-only=simplifycfg` 看不到块合并日志。

## 19.6 实验：投机与 hoist

输入 `sp` 在 `%then` 里有一条安全 `add i32 %x, 5`，另一条路径直接到 `join`。跑：

```bash
$B/opt -passes=simplifycfg -S spec.ll
```

真实输出：

```llvm
define i32 @sp(i1 %c, i32 %x) {
entry:
  %y = add i32 %x, 5
  %p = select i1 %c, i32 %y, i32 0
  ret i32 %p
}
```

`-passes='simplifycfg<no-speculate-blocks>'` 则保留分支，`join` 的 phi incoming 变成 `[ %y, %then ], [ 0, %entry ]`。只移动 `add`、不改 phi，Verifier 会报 `PHINode should have one entry for each predecessor`。

hoist 输入 `h2`：两个后继各有 `%y = add i32 %x, 1`，但 store 目标不同。打开 hoist 后真实输出：

```llvm
define void @h2(i1 %c, i32 %x, ptr %p, ptr %q) {
entry:
  %y = add i32 %x, 1
  br i1 %c, label %a, label %b
a:
  store i32 %y, ptr %p
  br label %exit
b:
  store i32 %y, ptr %q
  br label %exit
exit:
  ret void
}
```

关掉 hoist 时两个 `add` 仍在各自块中。sink 的机制相同，只是方向相反：它先把已有 phi 的 incoming 收进 `PHIOperands`，再把多个前驱的共同指令下沉到汇合块，必要时用 `select` 合并不同常量。

## 19.7 phi、blockaddress、`llvm.loop`、debug location

### phi

删前驱的标准入口是 `BasicBlock::removePredecessor`（`llvm/lib/IR/BasicBlock.cpp:434`），它对每个 phi 调 `removeIncomingValue`（`:446`），单前驱时尝试折成常量（`:455`）。

`MergeBlockIntoPredecessor`（`llvm/lib/Transforms/Utils/BasicBlockUtils.cpp:237`）先处理单入口 phi，移动指令，最后 `BB->replaceAllUsesWith(PredBB)`（`:354`）。这一行把「跳到 BB」的所有 use（包括 phi incoming 和 blockaddress）改成 PredBB，不是可选收尾。

空块折叠走 `redirectValuesFromPredecessorsToPhi`（`Local.cpp:1103`，调用在 `:1366`）。它处理「BB 的前驱被重定向到 Succ 时，Succ 的 phi 收到哪些值」。`replacePhiUsesWith`（`BasicBlock.cpp:567`）只改 phi 里的前驱标签；块分裂时用 `replaceSuccessorsPhiUsesWith`（`:578`）避免误改非 phi use。

### blockaddress

`blockaddress(@f, %bb)` 是常量表达式里的操作数，不是普通指令。删除 `%bb` 时，所有引用必须 RAUW 或拒绝变换。两个真实保护：

1. `MergeBlockIntoPredecessor` 开头检查 `BB->hasAddressTaken()`（`BasicBlockUtils.cpp:242`）。
2. `hoistCommonCodeFromSuccessors` 检查每个后继（`SimplifyCFG.cpp:1909`）。

实验：两个函数 CFG 相同，只有第一个的 `%a` 被 `blockaddress` 引用。跑：

```bash
$B/opt -passes='simplifycfg<no-speculate-blocks;hoist-common-insts>' -S ba-hoist.ll
```

真实输出（节选；分号注释是作者标注，`...` 省略参数）：

```llvm
define void @ba2(...) {
entry:
  br i1 %c, label %a, label %b
a:
  %x = add i32 1, 2        ; 仍在 a 里
...
define void @noba2(...) {
entry:
  %x = add i32 1, 2        ; 已提到 entry
  ret void
}
```

`ba2` 保留分支和两个 `add`；`noba2` 被完全折叠。地址被取不等于绝对不能删：若块只剩到后继的分支，删掉它并把 blockaddress RAUW 到后继仍等价。真实输出里 `blockaddress(@ba, %target)` 会变成 `blockaddress(@ba, %exit)`。

### `llvm.loop`

循环元数据挂在 latch 的 terminator 上。空块合并可能把两个 latch 合成一个，导致内层循环的元数据丢失。`TryToSimplifyUncondBranchFromEmptyBlock` 在 `Local.cpp:1316` 检查两个 latch 是否都带非调试循环元数据；是则直接放弃。只有一个带时，`:1389` 把 `MD_loop` 搬到每个前驱的新 terminator 上。`hasNonDebugLocLoopMetadata` 在 `llvm/lib/IR/Instruction.cpp:497`，它排除纯 `!dbg` 元数据。

### debug location

合并不同源码位置的指令时，不能随便选一个 loc。`SimplifyCFG` 在 `SimplifyCFG.cpp:2179` 对多个 loc 调 `getMergedLocations`，在 `:2988` 合并 invoke 时逐个调 `getMergedLocation`；具体实现在 `llvm/lib/IR/DebugLoc.cpp:186`。Review 看到 `setDebugLoc` 只取其中一个操作数的 loc，要问：合并后的指令代表哪条源码语句？

## 19.8 失败例子：phi incoming 没更新

```llvm
define i32 @badphi(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  br label %merge
b:
  br label %merge
merge:
  %p = phi i32 [ 1, %a ]
  ret i32 %p
}
```

跑：

```bash
$B/opt -passes=verify -disable-output bad-phi.ll
```

真实输出：

```text
PHINode should have one entry for each predecessor of its parent basic block!
  %p = phi i32 [ 1, %a ]
/home/ubuntu/llvm/builds/pr-lane/bin/opt: bad-phi.ll: error: input module is broken!
```

正确做法是补上 `[ 2, %b ]`，或在合并前把 `%b` 重定向到 `%a`，让 `merge` 只剩一个前驱。

第二个失败例子：删掉 `%target` 却留下 blockaddress。

```llvm
@table = global [1 x ptr] [ptr blockaddress(@ba, %target)]

define void @ba() {
entry:
  ret void
}
```

```bash
$B/opt -passes=verify -disable-output ba-broken.ll
```

真实输出：

```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: ba-broken.ll:1:50: error: use of undefined value '%target'
@table = global [1 x ptr] [ptr blockaddress(@ba, %target)]
                                                 ^
```

这是解析期错误：标签都不存在，IR 建不起来。blockaddress 是对块名的硬引用，删块时必须同步处理。

## 19.9 Review 清单

1. IR 先改了吗？`applyUpdates` 是否都在 successor 改写之后？
2. 边更新完整吗？删 `Pred->BB` 时是否补了 `Pred->NewSucc`？
3. phi 谁负责？走 `removePredecessor`、`FoldSingleEntryPHINodes`，还是手写循环？
4. blockaddress 呢？目标块 `hasAddressTaken()` 时拒绝还是 RAUW？

## 踩雷集锦

1. **「删块只要 `eraseFromParent`」** → 错。块可能还有 phi use、blockaddress、DT 节点、循环元数据；删除是事务。
2. **「`applyUpdates` 可以提前提交」** → 错。更新器读当前 CFG 判断边是否合法；IR 没改就提交会命中非法边断言。
3. **「`-debug-only=simplifycfg` 没打印合并，说明没合并」** → 错。`MergeBlockIntoPredecessor` 用另一个 `DEBUG_TYPE`。
4. **「blockaddress 只是常量，不影响优化」** → 错。它让 hoist/merge 退让，也会在 RAUW 时改变目标块。

## 动手练习

1. 把 `deadcase` 改成 `%m = and i32 %x, 3`，观察 case `2` 是否还死，并解释 known bits 的变化。
2. 构造 `blockaddress` 指向 `%a` 的函数，打开 hoist，确认 `%a` 没被 hoist；删掉 blockaddress 再跑一次。
3. 给 `condmerge.ll` 的两个分支加 `!llvm.loop` 元数据，观察空块合并是否拒绝。

## 本章重点整理

- `SimplifyCFGPass` 默认生成 canonical IR；激进变换由 `SimplifyCFGOptions` 和 pipeline 位置控制。
- 骨架是「删不可达 → tail merge → 迭代化简 → 再删不可达」的不动点循环。
- 改 CFG 要满足三条契约：IR 先改、边不重复、同边按顺序。

## 自我检核

- [ ] 不看笔记，画出 `SimplifyCFGPass::run` 到 `SimplifyCFGOpt::run` 的两层循环，并说明每层解决什么问题
- [ ] 解释 Eager 与 Lazy 的取舍，以及 `SimplifyCFG` 为什么选 Eager
- [ ] 说出 `applyUpdates` 的三条契约，并各举一个违反后的症状
- [ ] 给定一个块合并 diff，指出 phi、DTU、blockaddress、loop metadata 四个检查点
- [ ] 解释 hoist 遇到 `hasAddressTaken()` 为什么要直接返回
- [ ] 解释 `replaceAllUsesWith` 为什么会同时更新 phi incoming 和 blockaddress
- [ ] 判断一个 `setDebugLoc` 该用单条 loc 还是 `getMergedLocation`
- [ ] 给出一个默认 simplifycfg 不做的变换，并说明它由哪个选项控制

## 延伸阅读

- **[LLVM Programmer's Manual: Dominator Tree](https://llvm.org/docs/ProgrammersManual.html#the-llvm-dominator-tree)**
  - **读哪里**：`DominatorTree` 与 `DomTreeUpdater` 的 API 段
  - **能学到什么**：把 `applyUpdates` 契约放回官方 API 语境，补上 `UpdateType` 的定义
  - **前提知识**：Ch 14 的支配关系、Ch 16 的 invalidation
- **[LLVM LangRef: `blockaddress`](https://llvm.org/docs/LangRef.html#blockaddress)**
  - **读哪里**：`blockaddress` 与 `indirectbr` 的语义段
  - **能学到什么**：为什么块地址会让控制流分析变复杂
  - **前提知识**：Ch 7 的常量、Ch 9 的 IR 不变量
- **[LLVM LangRef: Loop Metadata](https://llvm.org/docs/LangRef.html#llvm-loop)**
  - **读哪里**：`llvm.loop` 的 attach 位置与子节点
  - **能学到什么**：为什么元数据挂在 latch terminator 上，合并 latch 为什么会丢信息
  - **前提知识**：Ch 8 的 `MDNode`、Ch 14 的 `LoopInfo`

CFG 变换的 correctness，不是「优化想得对不对」，而是「每次改结构有没有把依赖结构的东西一起改」。下一章把视角抬到调用图：内联器如何在 SCC 上做跨函数决策。

→ [Ch 20 — Inliner、CallGraph 与 SCC](./20-inliner-callgraph-scc.md)
