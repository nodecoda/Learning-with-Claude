# 练习 E — 定位一个中端 miscompile 的引入 pass

> **目标**：练「从现象到 pass」的定位链：先用 `-print-changed`/`-print-after` 找出第一个改变 IR 的 pass，再用 `git show` 还原修复前的代码，最后用 `llvm-reduce` 把复现缩到最小。产出是一份能贴进 review 的根因分析，不是一段猜测。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project` @ `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`。本章用两个真实 commit（`9ac5159b3c46`、`81e0dbceab0d`）做考古对象；所有命令输出都来自本机实测。

## 规则

1. 先自己做，再看 `<details>`。
2. 每个结论都要有命令或源码行支撑；「我觉得是 GVN」不算。
3. 不要求你重新构建 LLVM；本章的定位用 `opt` + `git show` 完成。需要重建的步骤会明确标出。
4. 如果行号漂了，用符号重新定位，并在答案里说明。

## E1：从 commit 还原一个 poison bug

对象：`9ac5159b3c46`。

先看它的形状：

```bash
cd /home/ubuntu/llvm/llvm-project
git show --stat --oneline 9ac5159b3c46
```

实测：

```text
9ac5159b3c46 [SCEV] Account for use flags in impliesPoison. (#244268)
 llvm/lib/Analysis/ScalarEvolution.cpp              | 11 +++-
 .../ScalarEvolution/exit-value-nowrap-flags.ll     | 65 +++++++++++++++++++++
 .../ScalarEvolution/exit-value-nowrap-flags-predicated-exits.ll | 66 ++++++++++++++++++++++
 3 files changed, 139 insertions(+), 3 deletions(-)
```

看实现 diff：

```bash
git show 9ac5159b3c46 -- llvm/lib/Analysis/ScalarEvolution.cpp
```

核心是：

```cpp
-static bool impliesPoison(const SCEV *AssumedPoison, const SCEV *S) {
+static bool impliesPoison(SCEVUse AssumedPoison, const SCEV *S) {
+  // AssumedPoison may be poison due to its use flags alone.
+  if (!AssumedPoison.isCanonical())
+    return false;
```

**任务**：

1. 用三句话概括这个 PR（改了什么/为什么安全/怎么证明）。
2. `SCEVUse` 比 `SCEV*` 多了什么信息？在 `llvm/include/llvm/Analysis/ScalarEvolution.h:124` 附近找。
3. `isCanonical()` 的精确含义是什么？为什么 `!isCanonical()` 时不能继续做 poison 推理？
4. 这个 bug 属于哪个 analysis？它会影响哪些 transform？
5. 两个测试文件分别覆盖什么路径？如果只保留一个，会漏掉什么？

**提示**：

- 先读 `SCEVUseT` 的 `isCanonical()` 注释（`ScalarEvolution.h:146`）。
- `umin_seq` 的 poison 传播规则很特殊：只有第一个 operand 的 poison 会传播。
- 这个修复是「保守返回 false」，不是「修好推理」；想清楚为什么保守是正确的。

**验收**：能解释「use flags 只在 use 点成立，提升到 preheader 后前提不成立」，并指出这是 analysis 层而不是 transform 层的修复。

<details>
<summary>参考解答</summary>

1. 三句话：`impliesPoison` 原本把带 use flags 的 SCEV 当成普通 SCEV 来推理；但 use flags 只在原来的使用点成立，SCEV 被提升到 preheader 后可能变成 poison；因此对 non-canonical 的 `SCEVUse` 直接返回 false，保留 `umin_seq`。测试是两个 lit 文件，覆盖普通 exit 和 predicated exit。

2. `SCEVUseT`（`ScalarEvolution.h:124`）把 `const SCEV *` 和 2 bit 的 use-specific no-wrap flags 打包在 `PointerIntPair` 里。`isCanonical()`（`:146`）的定义是「所有 operand 都没有 use flags」；`hasUseFlags()` 则判断这个 use 自身是否带 flags。

3. 非 canonical 意味着这个 SCEV 表达式在「当前使用点」才成立；一旦被移到 preheader/其他位置，flag 不再保证。`impliesPoison` 的前提是「AssumedPoison 是 poison」，如果前提本身依赖于使用点，就不能拿它去证明另一个 SCEV 是 poison。保守返回 false 是唯一不需要额外证明的选择。

4. 属于 `ScalarEvolution` analysis（`llvm/lib/Analysis/ScalarEvolution.cpp`）。它影响所有使用 SCEV 的 transform：循环向量化、indvars、LSR、循环删除、`LoopVectorize` 的 trip count 推理等。

5. `exit-value-nowrap-flags.ll` 覆盖普通 exit 的 use flags；`exit-value-nowrap-flags-predicated-exits.ll` 覆盖有前置条件的 exit（predicated exits）。只保留一个会漏掉「early exit taken first」的组合，也就是这个 bug 最关键的场景。

</details>

## E2：分析失效导致的 assert

对象：`81e0dbceab0d`。

```bash
git show --stat --oneline 81e0dbceab0d
git show 81e0dbceab0d -- llvm/lib/Transforms/Coroutines/CoroSplit.cpp
```

实测 diff 只有两行删除：

```cpp
     F.setSplittedCoroutine();
-    // Renumber basic blocks so that they are dense for crossing value analysis.
-    F.renumberBlocks();
```

commit message：

```text
GenericCycleInfo was not invalidated, so asserted when profcheck is
enabled that the block number epoche is outdated.
```

**任务**：

1. 这个 bug 是 IR 非法、语义错误，还是 analysis 失效？给出判据。
2. `renumberBlocks()` 改变了什么？为什么会和 `GenericCycleInfo` 的 epoch 检查冲突？
3. 作者选择「删调用」而不是「补 invalidation」。这两个方案各自的代价是什么？
4. 这个 commit 没有加测试。作为 reviewer，你会问哪三个问题？
5. 用 `rg -n "renumberBlocks" llvm/` 找所有调用点，判断删除它是否会影响其他路径。

**提示**：

- `GenericCycleInfo` 的 epoch 检查在 `llvm/include/llvm/ADT/GenericCycleInfo.h:158`。
- `renumberBlocks` 会改变 block 的编号；`GenericCycleInfo` 用编号作为索引。
- 「删调用」意味着不再需要重新编号；「补 invalidation」意味着保留重新编号并让 analysis 重算。

**验收**：能说出「这是 analysis invalidation，不是 IR 合法性」，并给出至少一个 reviewer 该问的测试问题。

<details>
<summary>参考解答</summary>

1. Analysis 失效。IR 仍然合法（verifier 能过），错的是缓存的 `GenericCycleInfo` 认为 block number epoch 没变，实际 `renumberBlocks()` 改了编号。assertions + profcheck 下，epoch 检查触发。

2. `renumberBlocks()` 重新分配 `BasicBlock` 的编号（`getNumber()`），让它们连续。`GenericCycleInfo` 用 block number 作为内部数组索引，并记录 `BlockNumberEpoch`；函数编号变化后 epoch 不匹配，`verifyBlockNumberEpoch` 断言失败。

3. 删调用：最小、避免 invalidation 复杂度，但前提是「没有代码依赖编号连续」。补 invalidation：保留重新编号的收益（如果有），但必须让所有依赖编号的 analysis 失效/重算，风险是漏掉某个 analysis。对 coroutine 路径，删调用是更低风险的修复。

4. 三个问题：
   - 哪个现有测试在删除前会触发这个 assert？如果没有，能否加一个 `REQUIRES: asserts` 的最小 coroutine 测试？
   - 删除后，是否有代码假设 block number 连续（例如数组索引、`getNumber()` 差值）？
   - 这个 assert 只在 profcheck 下触发；普通 assertions 构建能覆盖吗？如果不能，测试如何确保未来不回归？

5. `rg` 会显示 `renumberBlocks` 在 `BasicBlock`/`Function` 的调用点；重点看 coroutine 路径和其他 pass 是否依赖「编号连续」。如果没有依赖，删除安全；如果有，必须改调用方或补 invalidation。

</details>

## E3：找出第一个改变 IR 的 pass

用 `min.ll`（Ch 0 的三行函数）：

```llvm
define i32 @f(i32 %a, i32 %b) {
entry:
  %x = add i32 %a, 0
  %y = mul i32 %x, 1
  %z = add i32 %y, %b
  ret i32 %z
}
```

跑：

```bash
$B/opt -passes='default<O1>' -print-changed=quiet -disable-output min.ll
```

实测输出（节选）：

```text
*** IR Dump After EarlyCSEPass on f ***
define i32 @f(i32 %a, i32 %b) {
entry:
  %z = add i32 %a, %b
  ret i32 %z
}
*** IR Dump After GlobalOptPass on [module] ***
...
*** IR Dump After ReassociatePass on f ***
entry:
  %z = add i32 %b, %a
...
*** IR Dump After PostOrderFunctionAttrsPass on (f) ***
; Function Attrs: mustprogress nofree norecurse nosync nounwind willreturn memory(none)
```

**任务**：

1. 哪个 pass 第一次把 `add i32 %a, 0` 和 `mul i32 %x, 1` 折叠掉？
2. 哪个 pass 改变了 `add` 的操作数顺序？
3. 哪个 pass 加上了 `nofree norecurse nosync nounwind willreturn memory(none)` 属性？
4. 如果只看最终输出，你会把这三个变化分别归因给谁？为什么会错？

**提示**：

- `-print-changed=quiet` 只打印「IR 发生变化」的 pass，不打印每个 pass。
- `EarlyCSE` 会做常量折叠和冗余消除；`Reassociate` 会重排结合律表达式；`PostOrderFunctionAttrs` 推断函数属性。
- 想确认某个 pass，单独跑 `-passes='function(early-cse)'` 对比。

**验收**：能说出「最终输出是多个 pass 叠加的结果」，并给出每个变化的第一个责任 pass。

<details>
<summary>参考解答</summary>

1. `EarlyCSEPass`。它在第一次 dump 里已经把 `%z = add i32 %a, %b` 算出来，说明 `add 0`/`mul 1` 在它这里被折叠（EarlyCSE 内部会做常量折叠/简化）。
2. `ReassociatePass`。它把 `add i32 %a, %b` 改成 `add i32 %b, %a`，这是规范化操作数顺序，不是语义变化。
3. `PostOrderFunctionAttrsPass`。它根据函数体推断出 `nofree norecurse nosync nounwind willreturn memory(none)`。
4. 只看最终输出，最容易把三者都归因给 `InstCombine`（因为它最出名）。实际上 `EarlyCSE` 先折叠，`Reassociate` 改顺序，`PostOrderFunctionAttrs` 加属性。Review 时如果只看最终 IR，就会把「谁改的」搞错，进而把 bug 归因到错误的 pass。

</details>

## E4：用 `llvm-reduce` 缩小复现（以及弱谓词的陷阱）

先造一个包含三个函数的 `big.ll`（E3 的 `min.ll` + 两个无关函数）。写一个「interestingness test」：只要 `opt -passes=instcombine` 后还包含 `define i32 @a` 就算「仍然有趣」。

弱谓词脚本：

```sh
#!/bin/sh
$B/opt -passes=instcombine -S "$1" 2>/dev/null | grep -q 'define i32 @a'
```

跑：

```bash
$B/llvm-reduce --test=./interesting.sh big.ll -o reduced.ll
```

实测结果：

```llvm
define i32 @a() {
  ret i32 0
}
```

这个结果「仍然有趣」，但已经把你要研究的 `add` 折叠没了。原因：谓词只要求函数 `@a` 存在，没要求 bug 的关键结构存在。

换成强谓词：

```sh
#!/bin/sh
$B/opt -passes=instcombine -S "$1" 2>/dev/null | grep -q 'add i32 %x, %y'
```

再跑，实测结果：

```llvm
define i32 @a(i32 %x, i32 %y) {
  %q = add i32 %x, %y
  ret i32 %q
}
```

**任务**：

1. 为什么弱谓词会把 `add` 折没？这说明了 reduction 的什么性质？
2. 强谓词保留了哪些必要条件？它是否还可能删掉「触发 bug 的 flag/attribute」？
3. 如果 bug 依赖 `nsw`，谓词应该怎么写？
4. 用 `llvm-reduce` 时，如何避免「reduce 出一个仍然通过但不再触发 bug」的假复现？

**提示**：

- interestingness test 是 reduction 的目标函数；它定义了什么算「bug 还在」。
- 谓词要检查「bug 的关键特征」，不是「文件还能编译」。
- 对 miscompile，谓词应该是「输出仍然错误」，而不是「输出仍然包含某个字符串」。

**验收**：能解释「reduction 只会保留谓词要求的东西」，并写出一个检查 `nsw` 的谓词。

<details>
<summary>参考解答</summary>

1. 弱谓词只要求 `@a` 存在；reduction 可以把函数体删到 `ret i32 0`，仍然满足谓词。说明 reduction 不是「保留语义」，而是「保留谓词」；谓词写弱了，结果就没有诊断价值。
2. 强谓词保留了 `@a`、两个参数、`add i32 %x, %y` 这个结构。它仍可能删掉 `nsw`/`nuw`/`inbounds` 等 flag，因为谓词没要求它们；如果 bug 依赖 flag，需要把 flag 写进谓词。
3. 例如：`grep -q 'add nsw i32'`，或者更精确地用 `FileCheck` 检查 `add nsw i32 %x, %y`。
4. 谓词必须编码「bug 的可观察特征」：miscompile 就检查错误输出/退出码；崩溃就检查非零退出/assert 文本；IR 变换错误就检查关键指令/flag 仍在。每次 reduce 后，手工重跑一次完整复现，确认 bug 仍然存在。

</details>

## E5：写成 review 报告

选 E1 或 E2，按 Ch 32 的格式写一份 5 条评论的预审报告：

```text
[F? / P?] path:line  级别
他会问: "..."
为什么触发: ...
最小改法: ...

最可能被卡在: ...
```

要求：

- 至少 1 条 blocker（设计/正确性）；
- 至少 1 条测试缺口；
- 至少 1 条 nit；
- 每条落到具体行；
- 不用「我觉得」当证据。

## 定位链模板：从现象到 pass 的五步

把本章的方法压成一个可复用流程。每次遇到「这段 IR 怎么变成这样」或「这个 miscompile 是谁引入的」，按顺序走：

| 步骤 | 动作 | 命令/证据 | 常见错误 |
| --- | --- | --- | --- |
| 1. 固定现象 | 写出最小输入、期望、实际 | `.ll` + `opt`/`llc` 命令 | 用大程序复现，无法定位 |
| 2. 找第一个差异 | 看哪个 pass 第一次改变 IR | `-print-changed=quiet`、`-print-after=<pass>` | 只看最终输出，归因给最出名的 pass |
| 3. 单 pass 复现 | 只跑可疑 pass | `-passes='function(<pass>)'` | 用 `default<O2>` 黑盒，无法隔离 |
| 4. 读源码/历史 | 对照 pass 的实现与近期 commit | `rg`、`git log -S`、`git show` | 只看当前代码，不知道修复/回归 |
| 5. 最小化 | 用 `llvm-reduce` 或手工切片 | 强 interestingness test | 谓词太弱，reduce 出假复现 |

这张表的关键是第 2 步和第 5 步：**第一个差异**决定你去读哪个 pass，**强谓词**决定 reduction 是否保留 bug。两者都做错，后面的源码阅读再认真也没有用。

### 定位失败时的三个检查点

1. **差异不在你以为的层**：`opt` 输出没变，但 `llc` 的汇编变了 → 问题在 CodeGen；`opt` 输出变了但语义没变 → 可能是合法的 canonicalization。
2. **差异被后续 pass 掩盖**：第一个 pass 改了，第二个 pass 又改回去；用 `-print-changed=quiet` 看全部变化点，不要只看第一次。
3. **复现依赖 flag/attribute**：`nsw`、`inbounds`、`nnan`、`!prof`、`!tbaa` 可能在 reduction 中被删；谓词要显式保留它们。

## E2.5：读 commit 的检查表

E1/E2 都要求你从 commit 反推 bug。读一个修复 commit 时，固定问：

| 问题 | 看哪里 | 为什么重要 |
| --- | --- | --- |
| 改的是 analysis 还是 transform？ | 文件路径、类名 | 决定影响面和测试类型 |
| 修复是「删」还是「加」？ | diff 行数/形状 | 删通常是机制修复，加可能是 workaround |
| 为什么之前会错？ | commit body、注释 | 没有这句，测试只是补丁 |
| 测试在旧代码上会失败吗？ | 测试 diff、`git show <commit>^` | 不会失败的测试没有回归价值 |
| 有没有替代方案？ | commit body、review 讨论 | 判断修复是否最小 |
| 影响哪些下游？ | callers、targets、API | 决定要不要拆/加 release note |

对 `81e0dbceab0d`，这张表会立刻暴露两个 review 问题：没有测试、没有解释「为什么编号可以变得不连续」。对 `9ac5159b3c46`，它显示修复是「加一个保守 early return」，测试比例高，属于低风险高证据的 PR。

## 评分标准

| 项 | 分值 | 满分标准 |
| --- | --- | --- |
| 现象固定 | 15 | 最小输入 + 命令 + 期望/实际 |
| 第一个差异 | 20 | 能用 `-print-changed` 指出责任 pass |
| 根因 | 25 | 指到具体 analysis/不变量，解释因果 |
| 历史还原 | 15 | 能用 `git show` 说明修复前为什么错 |
| 最小化 | 15 | 强谓词 + 可复现的 reduced case |
| review 报告 | 10 | 5 条评论、分级正确、落到行 |

### E5 示例（以 E2 为对象）

```text
[F2 / P3] llvm/lib/Transforms/Coroutines/CoroSplit.cpp:2346  blocker
他会问: "Why is it safe to remove renumberBlocks?"（形态 F2，必要性质疑）
为什么触发: commit body 只说「编号应该仍然足够密集」，没有给出依赖编号连续性的调用方清单
最小改法: 在 commit message 或代码注释里列出 `getNumber()` 的消费者，并说明它们不要求连续

[F3 / P1] llvm/test/Transforms/Coroutines/...  blocker
他会问: "Which existing test fails before this change?"（形态 F3，缺失覆盖）
为什么触发: 修复针对 assertions + profcheck 下的 epoch 断言，但 PR 没有新增测试
最小改法: 加一个 `REQUIRES: asserts` 的最小 coroutine 测试，在删除前触发 assert

[F4] llvm/lib/Transforms/Coroutines/CoroSplit.cpp:2345  nit
他会问: "Is the comment above still accurate?"（形态 F4）
为什么触发: 删除了调用，但附近注释可能仍在描述 renumber 的目的
最小改法: 同步删除/改写注释

最可能被卡在: 「为什么编号可以不连续」这个不变量没有证据（P1/P3）
```

这张示例的作用是让你看到：即使是一个 2 行删除，也能写出 2 条 blocker + 1 条 nit。**blocker 不一定对应大改动，而是对应未证明的不变量。**

## 自我检核

- [ ] 能用 `-print-changed=quiet` 找出第一个改变 IR 的 pass
- [ ] 能解释「最终 IR 是多个 pass 叠加」，不会把功劳/锅归给单一 pass
- [ ] 能区分 IR 非法、语义错误、analysis 失效
- [ ] 能说出 `SCEVUse::isCanonical()` 的语义和它与 poison 推理的关系
- [ ] 能解释 `renumberBlocks` 与 `GenericCycleInfo` epoch 的冲突
- [ ] 能写出强/弱两种 interestingness test，并说明差异
- [ ] 能把根因写成 review 报告，而不是一段散文
- [ ] 知道哪些结论需要重建 LLVM 才能验证（本章未要求）

## 延伸阅读

- **[LLVM Debugging Guide](https://llvm.org/docs/DebuggingLLVM.html)**
  - **读哪里**：`-print-after`、`-print-changed`、`-debug-pass-manager`、assertions 构建
  - **和本练习的关联**：E3 的全部观测手段
- **[llvm-reduce Documentation](https://llvm.org/docs/CommandGuide/llvm-reduce.html)**
  - **读哪里**：`--test` 的契约、interestingness test 的写法
  - **为什么值得读**：E4 的谓词设计直接决定 reduction 的价值
- **[LLVM Language Reference: Poison Values](https://llvm.org/docs/LangRef.html#poison-values)**
  - **读哪里**：poison 的传播与 immediate UB
  - **和本练习的关联**：E1 的 use flags 推理前提
- **[LLVM ScalarEvolution](https://llvm.org/docs/Analysis/LoopTerminology.html)**
  - **读哪里**：SCEV 的基本概念与循环术语
  - **为什么值得读**：E1 的背景；更深的 SCEV 细节可以看仓库的 `ssa_optimizations` 课程

→ [Ch 23 — CodeGen 流水线源码入口：ISel → Machine IR → RA → MC](./23-codegen-pipeline.md)
