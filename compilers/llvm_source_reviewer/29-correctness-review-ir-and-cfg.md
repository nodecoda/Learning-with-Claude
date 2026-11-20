# Ch 29 — 正确性 review 一：IR 合法性、支配关系、迭代器、生命周期

> **目标**：把「这段代码看起来没问题」换成一组可逐条检查的不变量：SSA 支配、CFG 完整性、use-list 一致性、迭代器有效性、analysis 有效性。学完你能在 diff 里指出具体哪一行破坏了哪条不变量，而不是泛泛地说「可能有 bug」。

> **环境**：LLVM 24.0.0git，源码 `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`（assertions ON）。本章所有源码片段和报错都来自这棵树。

## 为什么需要这个？

中端 pass 的 bug 有一个共同形态：**它们不是「算错了」，而是「改坏了 IR 的结构，后面某个 pass 才炸」**。常见链路是：

```
pass A 删除一条指令，但忘了替换它的 uses
   → IR 里出现悬垂 use
   → pass B 读到已释放的 Value
   → 崩在离现场很远的地方，甚至只在 -O2 的某个 target 上崩
```

这种 bug 在 diff 里往往只表现为「少了一行 RAUW」或「erase 写在循环里」。如果你 review 时只顺着作者的业务逻辑读，很难发现；如果你按不变量逐条扫，它会自己跳出来。

本章讲四类不变量：

1. **IR 合法性**：Verifier 能查的结构约束。
2. **支配关系**：定义必须支配使用，phi 必须与前驱匹配。
3. **生命周期 / use-list**：删除一个值之前，所有 use 必须已经消失。
4. **迭代器与 analysis 有效性**：修改容器/CFG 后，旧迭代器和旧分析结果不能再用。

## 先建立直觉

把每个 pass 想成一个「临时拥有 IR 修改权」的租客。租约有五条：

```
1. 你写出的 IR 必须能通过 Verifier
2. 你插入的每条指令，其操作数必须支配它
3. 你删除的每个 Value，必须已经没有 use
4. 你修改容器/CFG 后，不能再碰旧迭代器
5. 你改了什么，就必须在 PreservedAnalyses 里如实报告
```

违反第 1 条，`-verify-each` 会当场抓住；违反第 2–4 条，可能要到几个 pass 之后才崩；违反第 5 条，通常表现为「开了某个 analysis 才错」的诡异 bug。Review 的顺序就是从最便宜的第 1 条开始，逐层往上。

## 29.1 Verifier 能查什么，不能查什么

Verifier 是结构检查器，不是语义证明器。它能查：

- SSA 支配（`Instruction does not dominate all uses!`）
- phi 的前驱数量与 CFG 一致
- 类型匹配、terminator 存在、block 名唯一
- 属性使用的基本约束（例如 `musttail` 的位置）
- metadata 的结构

它不能查：

- poison/undef 是否传播到了不该到的地方（Ch 30）
- 优化是否改变了程序的可观察行为
- alias analysis 的假设是否成立
- 这个 transform 是否「值得」

所以 review 时不要把「verifier 过了」当成「正确」。Verifier 只是最低门槛。反过来，**verifier 报错一定是 blocker**，没有讨论空间。

一个真实例子：`81e0dbceab0d` 的 commit message 说

```text
GenericCycleInfo was not invalidated, so asserted when profcheck is
enabled that the block number epoche is outdated.
```

这不是 Verifier 抓的，是 `GenericCycleInfo` 自己的 epoch 断言抓的。它说明「analysis 的有效性」是独立于 IR 合法性的另一层不变量，Ch 16 会专门讲。

## 29.2 支配关系：最常见的结构性错误

支配（dominance）的定义：如果从函数入口到使用点的每条路径都经过定义点，则定义支配使用。SSA 要求每个 use 都被它的 def 支配。

一个最小违规例子：

```llvm
define i32 @g(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  %x = add i32 1, 2
  br label %b
b:
  %y = add i32 %x, 3
  ret i32 %y
}
```

`%x` 只在 `a` 定义，但 `entry` 可以直接到 `b`，所以 `%x` 不支配 `%y` 里的使用。`opt -passes=verify` 的真实输出：

```text
Instruction does not dominate all uses!
  %x = add i32 1, 2
  %y = add i32 %x, 3
/home/ubuntu/llvm/builds/pr-lane/bin/opt: bad-dom.ll: error: input module is broken!
```

源码判据在 `llvm/lib/IR/Verifier.cpp:5637`：

```cpp
Check(DT.dominates(Op, U), "Instruction does not dominate all uses!", Op, &I);
```

### Review 时怎么用

看到一段 pass 代码「把指令移到另一个 block」，先问：

- 移动后的位置是否仍然支配所有 uses？
- 如果指令有多个 uses 分布在不同 block，是否需要插入新指令或使用 phi？
- 如果移动的是 terminator 之前的指令，是否跨过了 phi 的 use？

常见反例：把一个循环不变量从 loop body 提到 preheader，但它的某个操作数是在 loop 内定义的。`LICM` 会检查 `isSafeToSpeculativelyExecute` 和 `isLoopInvariant`；手写 pass 如果只检查「操作数在循环外」而漏了支配关系，就会产生非法 IR。

### Phi 是支配关系的特殊战场

phi 的操作数来自前驱 block，它的「定义点」在 block 开头，但语义上依赖前驱。Verifier 会检查 phi 的 entry 数量与前驱数量一致：

```text
PHINode should have one entry for each predecessor of its parent basic block!
```

Review 时，任何修改 CFG 的 diff 都要问：

- 新增/删除前驱后，phi 的 incoming list 更新了吗？
- `splitBasicBlock` 后，phi 应该留在哪一半？
- 如果删除一个前驱，phi 的对应 entry 删了吗？

`BasicBlock::splitBasicBlock` 在 `llvm/include/llvm/IR/BasicBlock.h:609`；它会新建一个 block 并把后半段指令移过去，但**不会自动帮你修 phi**。这是 review 里值得专门看一眼的地方。

## 29.3 生命周期：删除一个 Value 之前，use 必须为空

`Value` 的析构在 assertions 构建里会检查残留 use。`llvm/lib/IR/Value.cpp` 的实现：

```cpp
if (!materialized_use_empty()) {
  dbgs() << "While deleting: " << *VTy << " %" << getName() << "\n";
  for (auto *U : users())
    dbgs() << "Use still stuck around after Def is destroyed:" << *U << "\n";

  llvm_unreachable("Uses remain when a value is destroyed!");
}
```

`Instruction` 的析构还会检查它是否仍挂在 block 上（`llvm/lib/IR/Instruction.cpp:59`）：

```cpp
Instruction::~Instruction() {
  assert(!getParent() && "Instruction still linked in the program!");
```

这两条断言对应 review 里两个高频问题：

1. **删除指令前忘了 RAUW**：如果还有 use，`eraseFromParent()` 之后析构会炸。正确做法通常是先 `replaceAllUsesWith(...)`，或确认 `use_empty()`。
2. **删除指令前忘了从 block 摘除**：`delete I` 而不是 `I->eraseFromParent()`，析构时 `getParent()` 非空，断言炸。

### RAUW 的类型约束

`replaceAllUsesWith` 要求新旧值类型兼容。类型不匹配时，assertions 构建会报错；release 构建可能产生非法 IR。Review 时看到 RAUW，检查：

- 新旧类型是否完全相同（包括 vector 的 scalable 属性）？
- 如果新值是 `poison`，是否可以接受（Ch 30）？
- 如果旧值被 metadata 使用（`!dbg`、`!tbaa`），RAUW 会不会破坏 metadata？

### 一个常见误区

「这条指令没有 use，所以可以删」——在 SSA 里，没有 use 只说明它**当前**没有使用者，不说明删除它安全。它可能有副作用（store、call）、可能被 `llvm.assume`/metadata 引用、可能是 `musttail` 链的一环。判断删除安全要同时看：

- `isSafeToRemove()` / `wouldInstructionBeTriviallyDead()`
- 是否有 side effect（`mayHaveSideEffects()`）
- 是否被 debug metadata 使用（`isUsedByMetadata()`）

## 29.4 迭代器：边遍历边删是经典崩点

看这段看起来自然的代码：

```cpp
for (Instruction &I : BB) {
  if (isDead(I))
    I.eraseFromParent();
}
```

`eraseFromParent()` 会让当前迭代器失效，循环行为未定义。LLVM 提供的标准写法是：

```cpp
for (Instruction &I : make_early_inc_range(BB)) {
  if (isDead(I))
    I.eraseFromParent();
}
```

`make_early_inc_range` 的定义在 `llvm/include/llvm/ADT/STLExtras.h:649`，注释把约束写得很清楚：

```text
The underlying iterator will be incremented immediately after it is
dereferenced, allowing deletion of the current node or insertion of nodes to
not disrupt iteration provided they do not invalidate the *next* iterator --
the current iterator can be invalidated.
```

注意最后半句：**它只保证当前迭代器可以被失效，不保证下一个迭代器有效**。如果循环体删除了不止当前元素（例如删了下一个 block），early-inc 也救不了你。

### Review 检查清单

看到 `eraseFromParent` / `erase` / `removeFromParent`，逐条问：

- 循环是否用了 `make_early_inc_range`，或手动 `I++` 后再 erase？
- 是否在遍历 `users()` 时 RAUW？RAUW 会修改 use-list，正在遍历的 iterator 会失效。常见修法是先收集到 `SmallVector`，再处理。
- 是否在遍历 `BasicBlock` 时删除/分裂 block？`Function::iterator` 会失效。
- 是否在遍历 `DenseMap` 时插入？rehash 会让迭代器和指针失效（Ch 10）。

## 29.5 CFG 修改：DomTreeUpdater 不是可选装饰

如果你增删 CFG 边，DominatorTree 必须更新。LLVM 的 `DomTreeUpdater`（`llvm/include/llvm/Analysis/DomTreeUpdater.h`）提供 `applyUpdates` / `insertEdge` / `deleteEdge`。它有两种模式： eager（立刻更新）和 lazy（批量后更新）。

Review 时看到 `splitBasicBlock`、`removePredecessor`、`addSuccessor`、`eraseFromParent`（删除 block），要检查：

- 对应的 edge update 是否提交了？
- 是批量更新还是即时更新？批量更新是否在读取 DT 之前 flush？
- 如果 pass 声明 `preserveSet<CFGAnalyses>()`，它是否真的没有改变 CFG？

`InstCombinePass::run` 的结尾是一个正面例子（`llvm/lib/Transforms/InstCombine/InstructionCombining.cpp:6360`）：

```cpp
  // Mark all the analyses that instcombine updates as preserved.
  PreservedAnalyses PA;
  LRT.update(&ID, /*Changed=*/true);
  PA.preserve<LastRunTrackingAnalysis>();
  PA.preserveSet<CFGAnalyses>();
  return PA;
```

InstCombine 可能改变 CFG（例如把 `select` 变成分支），但它声明保留 CFG analyses。这个声明是否成立，取决于它的实现是否在改 CFG 时正确维护了 DT。Review 时不能只看这行 `preserveSet`，还要看改 CFG 的路径有没有 `DomTreeUpdater`。

## 29.6 Analysis 失效也是正确性

`81e0dbceab0d` 是一个真实案例：`CoroSplit` 调用了 `F.renumberBlocks()`，但没有让 `GenericCycleInfo` 失效，于是开启 `profcheck` 时 epoch 断言触发。作者的处理是删掉 `renumberBlocks`，而不是补 invalidation。

Review 这类 diff 时，问题不是「删了这两行会不会少优化」，而是：

- 删除后，哪些 analysis 的假设仍然成立？
- 原来依赖「block number 连续」的代码是否存在？
- 有没有测试能在删除前触发断言？

`GenericCycleInfo` 的 epoch 检查在 `llvm/include/llvm/ADT/GenericCycleInfo.h:158`：

```cpp
void verifyBlockNumberEpoch(const FunctionT *Fn) const {
  assert(BlockNumberEpoch == Fn->getBlockNumberEpoch() && ...
```

它说明「block number 变了」是一个需要被 analysis 感知的事件。任何修改 block 编号/顺序的 pass，都必须考虑 analysis invalidation。

## 一张正确性 review 检查表

| 检查项 | 触发信号 | 该问什么 |
| --- | --- | --- |
| 支配 | 移动指令、跨 block 复用值 | 新位置支配所有 uses 吗？ |
| Phi | 改 CFG、split/merge block | incoming list 与前驱一致吗？ |
| Use-list | `eraseFromParent`、`delete` | 删除前 `use_empty()` 吗？RAUW 类型对吗？ |
| 迭代器 | 循环内 erase/insert | 用了 early-inc 吗？下一个元素会失效吗？ |
| CFG 更新 | `addSuccessor`、`removePredecessor` | DT/PostDT 更新了吗？ |
| Analysis | 改 block 编号/CFG/IR | `PreservedAnalyses` 诚实吗？ |
| 副作用 | 删除「无用」指令 | 有 store/call/musttail/metadata 吗？ |

## 踩雷集锦

1. **以为「没有 use」就可以删**：副作用、metadata、`musttail`、convergence 都可能让一条「无用」指令不能删。
2. **在 range-for 里 `eraseFromParent()`**：当前迭代器失效；要用 `make_early_inc_range`，并确认循环体不会让下一个迭代器失效。
3. **RAUW 时不检查类型**：类型不匹配在 release 构建可能悄悄产生非法 IR；assertions 构建才会炸。
4. **改了 CFG 但只更新 DT，不更新 PostDT/循环信息**：不同 analysis 有不同依赖，`preserveSet<CFGAnalyses>()` 不是万能免责声明。
5. **把 verifier 通过当成语义正确**：Verifier 不检查 poison、UB、alias 假设；Ch 30 会讲这些「合法但错误」的情况。
6. **忽略 block 编号/顺序变化的 analysis 影响**：`renumberBlocks` 这类操作会让依赖编号的 analysis 失效，`GenericCycleInfo` 的 epoch 断言就是例子。

## 动手练习

1. 把 29.2 的非法 IR 改成合法版本，跑 `opt -passes=verify` 确认；再故意把 `%x` 移回 `a`，观察报错是否一致。
2. 写一个 `for (Instruction &I : BB) I.eraseFromParent();` 的最小 C++ pass（或伪代码），解释为什么它会崩；再用 `make_early_inc_range` 改写。
3. 用 `opt -passes='default<O2>' -verify-each` 跑一段带循环的 IR，观察是否有 pass 产生非法 IR。如果没有，故意在 IR 里放一个 `musttail` 违规，看 verifier 的报错。
4. 找 `llvm/lib/Transforms` 里任意一个调用 `splitBasicBlock` 的 pass，检查它是否更新了 phi 和 DomTree。把你的发现写成一条 review comment。
5. 读 `81e0dbceab0d` 的 diff，列出至少三个「reviewer 应该问但 commit message 没回答」的问题。

## 本章重点整理

- 正确性 review 的核心是逐条检查不变量，而不是顺着业务逻辑读。
- Verifier 只查结构合法性；支配、phi、use-list、迭代器、analysis 有效性是四层独立风险。
- 删除 Value 前必须 `use_empty()`；删除 Instruction 前必须从 block 摘除。
- 边遍历边删要用 `make_early_inc_range`，并确认不会让下一个迭代器失效。
- 改 CFG 必须更新 DomTree/PostDomTree；`preserveSet<CFGAnalyses>()` 不是免责声明。
- 小 diff 也可能破坏 analysis 有效性；`renumberBlocks` 的两行删除就是例子。

## 自我检核

- [ ] 能解释支配关系的定义，并判断一段 IR 是否违反
- [ ] 知道 phi 的 incoming list 在 split/merge block 时该怎么处理
- [ ] 能说出 `Value` 和 `Instruction` 析构断言各自检查什么
- [ ] 能写出边遍历边删的安全写法，并解释 `make_early_inc_range` 的边界
- [ ] 知道改 CFG 后要更新哪些 analysis，以及 `PreservedAnalyses` 的诚实性要求
- [ ] 能区分「Verifier 能查」和「Verifier 不能查」的问题
- [ ] 能从一条 2 行删除的 diff 里看出 analysis invalidation 风险
- [ ] 有一个固定的六项检查表，review 时不会只读业务逻辑

## 延伸阅读

- **[LLVM Language Reference: SSA / Dominance](https://llvm.org/docs/LangRef.html#functions)**
  - **读哪里**：Function 结构、BasicBlock、phi 的语义说明
  - **为什么值得读**：支配和 phi 的权威定义；不要用编译原理课的简化版替代
- **[LLVM Programmer's Manual: Value and User](https://llvm.org/docs/ProgrammersManual.html#the-value-user-and-use-classes)**
  - **读哪里**：`Value`/`User`/`Use` 一节，以及 `replaceAllUsesWith` 的说明
  - **和本章的关联**：29.3 的生命周期规则来自这里
- **[LLVM Programmer's Manual: Iterating over instructions](https://llvm.org/docs/ProgrammersManual.html#iterating-over-the-instructions-in-a-function)**
  - **读哪里**：迭代器与 `make_early_inc_range` 相关段落
  - **为什么值得读**：官方明确讲了「边遍历边删」的约束
- **[LLVM Analysis: DominatorTree](https://llvm.org/docs/ProgrammersManual.html#the-dominatortree-class)**
  - **读哪里**：DominatorTree 的 API 与更新方式
  - **和本章的关联**：29.5 的 CFG 更新规则

→ [Ch 30 — 正确性 review 二：poison、undef、freeze、溢出、UB、fast-math](./30-correctness-review-poison-ub.md)
