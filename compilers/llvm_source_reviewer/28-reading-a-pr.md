# Ch 28 — 读一个 PR 的顺序：diff 形态、baseline commit、拆分

> **目标**：把「打开 PR 从第一行 diff 开始读」换成一套固定顺序：先判形态，再读测试，再读实现，最后看影响面与历史。学完你能在 10 分钟内说出一个 PR 改了什么、声称为什么安全、证据够不够，以及最可能被卡在哪里。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（LLVM 24.0.0git，assertions ON）。构建缓存的 worktree 是 `a5eaf194d8b6`，与源码 hub 不是同一 commit；行号以 7c7a 为准。

> **素材**：本章用本机 `llvm-project` 的四个真实 commit 做案例（`25e91b6837aa`、`9ac5159b3c46`、`81e0dbceab0d`、`2b3f0f084f77`）。所有 `git show` 输出都来自这棵树；PR 编号与 commit message 原文保留。

## 为什么需要这个？

diff 是按文件、按行排列的，但 review 需要的信息不是这个顺序：

- **动机**在 PR 描述里，不在 diff 里。
- **行为契约**在测试里，通常先于实现出现。
- **风险**在调用点、target 覆盖、API 兼容性里，往往不在被改的文件里。
- **历史**在 commit message 和后续 revert 里，diff 本身不会告诉你「这个改动以前被回滚过」。

从第一行 diff 开始读，最容易被格式、命名、局部逻辑带走，读完 500 行却说不出「这个 PR 到底改变了哪个不变量」。固定顺序的价值是：**每一轮只回答一个问题**，答不上来就停，不要往下滑。

## 先建立直觉

把 PR 想成一份带附件的变更申请。附件是 diff，但申请本身包含六样东西：

```
1. 动机（title + body）        —— 为什么现在要改？
2. 形态（stat / 文件分布）      —— 这是修 bug、加功能、重构，还是 revert？
3. 测试（test diff）           —— 它锁住了什么行为？在旧代码上会失败吗？
4. 实现（lib diff）            —— 改动是否恰好实现它声称的事？
5. 影响面（callers / targets）  —— 谁会因此改变行为？
6. 历史（git log / revert）     —— 这个区域以前踩过什么坑？
```

读的顺序就是 1 → 2 → 3 → 4 → 5 → 6。**测试先于实现**，因为测试定义了你判断实现的标准；先读实现，你会不自觉地接受作者的框架。

## Step 1：读标题和描述，写出三句话

任何 PR 读完描述后，先写：

1. 它改了什么（一句话，动词开头）。
2. 它声称为什么安全（一句话，引用作者的理由）。
3. 它用什么证明（一句话，测试/基准/形式化论证）。

写不出来，说明描述不合格，或者你还没读懂。这时最该做的不是继续读 diff，而是在 PR 里问「能否用一两句话说明动机和安全性论证」。这不是刁难：LLVM 的 commit message 规范明确要求「强调 why，而不只是 what」。

看一个合格的例子（`9ac5159b3c46`）：

```text
[SCEV] Account for use flags in impliesPoison. (#244268)

Use flags only hold where the expression is used, so an exit count with
use flags may be poison when expanded in the preheader, even if the
earlier exit is taken first. Conservatively return false from
impliesPoison for non-canonical uses, keeping them in the umin_seq.
```

三句话：

1. 改了什么：`impliesPoison` 对带 use flags 的 SCEV 保守返回 false。
2. 为什么安全：use flags 只在使用的那个点成立，提前到 preheader 可能变成 poison，所以保守是正确方向。
3. 用什么证明：两个新 lit 测试文件，覆盖普通 exit 和 predicated exit。

这段描述没有一句「优化了代码」「修复了问题」的空话。它直接给出**语义理由**和**保守方向**，reviewer 可以顺着它去验证。

## Step 2：看 stat，判断 diff 形态

```bash
git show --stat --oneline 9ac5159b3c46
```

实测：

```text
9ac5159b3c46 [SCEV] Account for use flags in impliesPoison. (#244268)
 llvm/lib/Analysis/ScalarEvolution.cpp              | 11 +++-
 .../ScalarEvolution/exit-value-nowrap-flags.ll     | 65 +++++++++++++++++++++
 .../exit-value-nowrap-flags-predicated-exits.ll    | 66 ++++++++++++++++++++++
 3 files changed, 139 insertions(+), 3 deletions(-)
```

形态一眼可见：**11 行实现 + 131 行测试**。这是一个高信号比例：实现很小，测试很大，说明作者在证明边界条件，而不是在堆功能。

把常见形态列成表，先判形态再决定读法：

| 形态 | 文件分布特征 | 第一轮该问什么 |
| --- | --- | --- |
| test-only / precommit | 只有 `test/`，标题带 `NFC` 或 `Precommit` | 这些测试锁的是「现有行为」还是「期望行为」？ |
| bug fix | `lib/` 小改 + `test/` 回归 | 测试在旧代码上会失败吗？根因是什么？ |
| NFC refactor | 只有 `lib/`，无行为变化声明 | 真的 NFC 吗？API/迭代顺序/调试输出变了吗？ |
| new feature | 新文件 + 大量测试 + 文档 | 公共契约是什么？要不要 RFC？ |
| infra / TableGen | `utils/TableGen`、`.td`、生成器 | 影响哪些 target？生成物 diff 看了吗？ |
| revert | 标题 `Revert "..."` | 原 patch 什么问题？有没有复现？ |
| API / IR change | 头文件 + 多调用点 + LangRef | 下游会不会被迫改？bitcode 兼容吗？ |

形态不是标签游戏。它决定你第一轮问「设计」还是「覆盖」：bug fix 先问根因和回归测试；new feature 先问契约和 RFC；NFC 先问「真的没有行为变化吗」。

## Step 3：先读测试，理解它锁住了什么

打开 `25e91b6837aa`，这是一个纯测试的 precommit commit：

```text
[Attributor][NFC] Precommit tests for attributes derived from non-exact definitions (#243247)

Adding tests showing that the Attributor derives `align` for the
arguments and `nonnull` for the return value of `linkonce_odr` functions
from their bodies (uses and assumptions), and propagates them to the
callers, although another definition might be used instead.
```

它的 diff 只有两个测试文件、96 行新增。看其中一段：

```llvm
; The definition of a linkonce_odr function might be replaced by another one,
; e.g. one without the load or the assumption, so the attributes of its
; arguments and return value must not be derived from its body.
define linkonce_odr ptr @nonnull_return_from_load_non_exact(ptr %p) {
; CHECK-LABEL: define {{[^@]+}}@nonnull_return_from_load_non_exact
; CHECK-SAME: (ptr align 4 [[P:%.*]]) {
; CHECK-NEXT:    [[V:%.*]] = load i32, ptr [[P]], align 4
; CHECK-NEXT:    ret ptr [[P]]
;
  %v = load i32, ptr %p, align 4
  ret ptr %p
}
```

这段测试的 CHECK 行写的是**当前行为**：Attributor 给 `linkonce_odr` 函数的参数推了 `align 4`，尽管另一个定义可能没有这个 load。注释直接点出风险：「must not be derived from its body」。

这就是 baseline commit 的作用：**先把现状固定下来，再让后续修复产生一个可读的 diff**。如果修复和测试放在同一个 commit，reviewer 很难分清「测试是因为修复才通过」还是「测试本来就会通过」。

Baseline 测试的 review 要点：

- 它是 `NFC` 吗？如果测试在当前代码上通过，是；如果失败，它就不是 NFC，而是「预期行为变更」。
- 它锁的是**现有行为**还是**期望行为**？两者都可以，但必须说清楚。前者用于后续 diff 对照，后者用于 TDD。
- 测试是否会在旧代码上**真的失败**？Ch 31 会给验证方法（`git stash` 实现、只回退实现、跑 lit）。

一个常见误解是「测试越多越好」。这段测试的价值不在行数，而在它把 `linkonce_odr` + 非精确定义 + 属性推导这三个条件组合固定住了。**好的回归测试是最小条件组合，不是大段 IR。**

## Step 4：读实现，逐条对照声称

回到 `9ac5159b3c46`，实现只有 11 行：

```cpp
-/// Return true if V is poison given that AssumedPoison is already poison.
-static bool impliesPoison(const SCEV *AssumedPoison, const SCEV *S) {
+/// Return true if S is poison given that AssumedPoison is already poison.
+static bool impliesPoison(SCEVUse AssumedPoison, const SCEV *S) {
+  // AssumedPoison may be poison due to its use flags alone.
+  if (!AssumedPoison.isCanonical())
+    return false;
+
   // First collect all SCEVs that might result in AssumedPoison to be poison.
```

读这段时，不要停在「加了一个 early return」。要问四个问题：

1. **`SCEVUse` 和 `SCEV*` 的区别是什么？** 看 `llvm/include/llvm/Analysis/ScalarEvolution.h:124` 的 `SCEVUseT`：它把指针和 2 bit 的 use flags 打包在一起；`isCanonical()` 的定义是「所有 operand 都没有 use flags」。
2. **为什么 `!isCanonical()` 就可以直接返回 false？** 因为 use flags 只在「使用点」成立，SCEV 被提升到 preheader 后，原来的 flag 不再保证；此时「AssumedPoison 是 poison」这个前提本身不成立，不能拿它去证明 S 是 poison。
3. **保守返回 false 会不会丢优化？** 会，作者在描述里明说「keeping them in the umin_seq」。这是有意的正确性优先。
4. **测试覆盖了哪些边界？** 两个文件：普通 exit 和 predicated exit。要看它们是否分别覆盖「canonical」和「non-canonical」两条路径。

这里的关键不是读懂 11 行 C++，而是**把描述里的语义理由映射到代码的具体条件**。如果代码只写了 `if (!AssumedPoison.isCanonical()) return false;`，但描述说的是「exit count with use flags may be poison when expanded in the preheader」，你要能指出这两者是同一件事。

## Step 5：看影响面，而不是只看被改文件

`81e0dbceab0d` 是一个极端的例子：2 行删除，没有测试。

```text
[coro] Remove renumberBlocks to fix assert (#244573)

GenericCycleInfo was not invalidated, so asserted when profcheck is
enabled that the block number epoche is outdated.

Remove the `renumberBlocks` call to fix this.
Given that e.g. `GenericCycleInfo` already uses the numbers as block
indices, they should still be sufficiently dense.
```

diff：

```cpp
     F.setSplittedCoroutine();
-    // Renumber basic blocks so that they are dense for crossing value analysis.
-    F.renumberBlocks();
 
     // Query BFI and populate SuspendFreqs right before splitting.
```

第一眼：删两行，修一个 assert，简单。但 reviewer 至少要问：

- **为什么删掉 `renumberBlocks` 是安全的？** 作者的理由是「GenericCycleInfo 已经把 block number 当索引，应该仍然足够密集」。这是一个**不变量声明**，需要验证：`renumberBlocks` 的调用者是否依赖「编号连续」？`GenericCycleInfo` 的 epoch 检查在 `llvm/include/llvm/ADT/GenericCycleInfo.h:158`，它比较的是 `BlockNumberEpoch` 与函数当前的 block number epoch。
- **为什么没有测试？** 可能是 `profcheck` + 现有 coroutine 测试已经覆盖，也可能是漏了。reviewer 应该要求作者说明「哪个现有测试会在删除前触发 assert」，而不是接受「assert 修了」。
- **影响面**：`CoroSplit` 在 coroutine 拆分路径上，所有 coroutine 都受影响；这不是一个局部 target 改动。

这就是「影响面」和「diff 大小」不成正比的例子。2 行删除可以是高风险，200 行测试可以是低风险。

## Step 6：看历史，尤其是 revert

`2b3f0f084f77` 是一个被 revert 的 NFC 重构：

```text
Revert "[CodeGen][NFC] Factor MachineBlockPlacement::alignBlocks into reusable helper (#241995)" (#243994)

This reverts commit 6447d4dda5706f5d7946f86c1225fd964abfb39e (#241995)
at a reviewer's request while concerns about the change are addressed.
```

注意三点：

1. **NFC 不等于免 review**。这个 PR 自称 NFC，仍然被 revert。
2. **revert 的理由是「reviewer 有顾虑」**，不是「测试挂了」。说明设计层的顾虑可以在合入后继续，而且 revert 是正常手段。
3. **revert commit 直接引用原 commit hash 和 PR 编号**，方便追溯。你读一个陌生区域时，`git log --grep='Revert' -- <path>` 能告诉你哪些看起来安全的重构曾经出过事。

读 PR 的最后一步，建议固定跑：

```bash
git log --oneline --grep='Revert' -- <被改的目录> | head
git log --oneline -20 -- <被改的文件>
```

如果这个区域近期有 revert，先读 revert 的理由，再读当前 PR。历史会告诉你 reviewer 在这个区域最在意什么。

## 一张可复用的 PR 阅读工作表

把下面这张表复制到你的笔记里，每读一个 PR 填一次。填不满，就不要开始写 comment：

| 项 | 你要写的内容 | 证据来源 |
| --- | --- | --- |
| 改了什么 | 一句动词开头的话 | title + body |
| 为什么现在改 | 触发问题/需求 | body、issue、discussion |
| 安全性论证 | 作者声称不变量 | body、代码注释 |
| 形态 | test-only / bugfix / NFC / feature / infra / revert | `--stat` |
| 测试是否会在旧代码失败 | 是/否/未知 | 测试 diff + 回退实验 |
| 实现是否匹配声称 | 是/部分/否 | lib diff |
| 影响面 | callers、targets、API、bitcode | `rg`、`git grep` |
| 历史风险 | 近期 revert / 相关 issue | `git log --grep` |
| 最可能被卡的点 | 一句话 | 以上综合 |

## 反模式：四种常见的错误读法

1. **从第一行 diff 开始读**：你会先看到格式、命名、局部实现，最后才看到测试。动机和契约被放到最后，判断已经被锚定。
2. **只看 lib diff，不看 test diff**：你会不知道作者认为哪些行为重要，也无法判断测试是否真的覆盖了改动。
3. **把「测试通过」当成「行为正确」**：测试可能只覆盖 happy path，甚至在旧代码上也会通过。Ch 31 会讲怎么验证。
4. **把 NFC 当成「不用看」**：`2b3f0f084f77` 就是 NFC 被 revert 的例子。NFC 只承诺「不改变语义」，不承诺「没有 API 影响、没有编译时间影响、没有下游依赖变化」。

## 踩雷集锦

1. **把 precommit 测试当成「已经修好了」**：`25e91b6837aa` 的测试锁的是当前（可能有问题的）行为。它本身不是修复，后续 PR 才会改行为。
2. **看到 11 行实现就以为简单**：`9ac5159b3c46` 的难点在 `SCEVUse` 的 flags 语义和 `umin_seq` 的 poison 传播，不在行数。小 diff 可能改变一个全局不变量。
3. **看到 2 行删除就跳过影响面**：`81e0dbceab0d` 删的是 coroutine 路径上的 `renumberBlocks`，影响所有 coroutine，不是局部清理。
4. **忽略 commit message 里的「should still be sufficiently dense」**：这类措辞是作者的不变量假设，正是 reviewer 该验证的地方。不要把它当成结论。
5. **不查历史就评价设计**：一个区域可能刚因为类似改动被 revert。先 `git log --grep='Revert'`，再决定要不要提「为什么不沿用 X」。

## 动手练习

1. 对 `25e91b6837aa` 跑 `git show --stat` 和 `git show -- <测试文件>`，回答：它锁的是现有行为还是期望行为？你怎么验证？
2. 对 `9ac5159b3c46`，不看 commit message，只读 11 行实现，写出你认为它修复的语义问题；再对照 message，看你漏了什么。
3. 对 `81e0dbceab0d`，用 `rg` 找 `renumberBlocks` 的所有调用点，判断删除它是否会影响其他路径。
4. 对 `2b3f0f084f77`，找到被 revert 的原 commit `6447d4dda570`，读它的 diff，列出至少两个 reviewer 可能担心的点。
5. 任选一个最近的 LLVM PR，用上面的工作表填一遍，限时 10 分钟；填不满的格子就是你要问作者的问题。

## 本章重点整理

- 读 PR 的顺序：动机 → 形态 → 测试 → 实现 → 影响面 → 历史。
- 测试先于实现读，因为测试定义了「什么算对」，也暴露作者认为重要的边界。
- `--stat` 先判形态；不同形态的第一轮问题完全不同。
- baseline/precommit 测试锁的是现状，不是修复；它的价值是让后续 diff 可读。
- 小 diff 不等于低风险；2 行删除可能影响所有 coroutine。
- NFC 不是免 review 牌；历史上 NFC 重构也会被 revert。

## 自我检核

- [ ] 能在读完 title/body 后用三句话概括一个 PR，并区分「声称」与「证据」
- [ ] 能根据 `--stat` 判断 diff 形态，并说出该形态第一轮该问什么
- [ ] 能解释 baseline/precommit 测试的作用，以及它和修复 commit 的区别
- [ ] 能对一个 11 行实现说出它改变了哪个不变量，而不只是「加了 early return」
- [ ] 能从 2 行删除的 diff 里识别出影响面问题
- [ ] 知道 NFC 为什么仍可能被 revert
- [ ] 会用 `git log --grep='Revert'` 查区域历史
- [ ] 有一张固定工作表，读 PR 时不会从第一行 diff 开始

## 延伸阅读

- **[LLVM Developer Policy: Commit messages](https://llvm.org/docs/DeveloperPolicy.html#commit-messages)**
  - **读哪里**：整节；重点看「强调 why」「标题简洁」「系列 patch 标 `[N/M]`」
  - **和本章的关联**：Step 1 的三句话标准来自这里，不是个人偏好
- **[LLVM Code Review: Splitting Requests](https://llvm.org/docs/CodeReview.html#splitting-requests-and-conditional-acceptance)**
  - **读哪里**：整节；注意「条件接受」和「拆成独立 patch」的措辞
  - **为什么值得读**：Ch 28 的 baseline/拆分判断，官方给了明确授权
- **[LLVM Developer Policy: Incremental Development](https://llvm.org/docs/DeveloperPolicy.html#incremental-development)**
  - **读哪里**：整节；重点看「大分支的五个缺点」和「先加 API 再迁移」的模式
  - **和本章的关联**：解释了为什么 reviewer 会要求把一个 PR 拆成多个可独立 review 的 commit
- **[llvm-project commit history](https://github.com/llvm/llvm-project/commits/main)**
  - **读哪里**：按目录过滤 `llvm/lib/Analysis`、`llvm/lib/Transforms`，各读 10 个 commit message
  - **为什么值得读**：真实 commit message 的密度和措辞，比任何模板都更能训练你的「三句话」能力

→ [Ch 29 — 正确性 review 一：IR 合法性、支配关系、迭代器、生命周期](./29-correctness-review-ir-and-cfg.md)
