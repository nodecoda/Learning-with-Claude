# Ch 32 — Review 写作与真实 PR 复盘：从 comment 到 LGTM

> **目标**：把前五 Part 的知识变成可发布的 review：一条 comment 说清「哪里、断言什么、证据是什么、最小改法是什么、严重性如何」。学完你能对一个真实 diff 产出 blocker → nit → optional 排序的 review 报告，并判断最可能被卡住的点。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（LLVM 24.0.0git，assertions ON）。构建缓存的 worktree 是 `a5eaf194d8b6`，与源码 hub 不是同一 commit；行号以 7c7a 为准。

> **素材**：本章的形态统计来自本机 `llvm-reviewer-patterns` skill 的底稿（755 条评审文本、128 个 PR，聚焦 GlobalISel/X86 的两位高频 reviewer），属于**样本分析**，不是 LLVM 官方规范。制度部分以 `llvm/docs/CodeReview.md` 为准。案例用本机 `llvm-project` 的四个真实 commit。

## 为什么需要这个？

知道「哪里可能错」不等于能写出有效的 review。常见的失败形态：

- **长文分析**：写了 800 字，作者读完不知道你要他改哪一行。
- **只说不好，不说怎么好**：`This seems wrong.` 作者无法行动。
- **把 nit 写成 blocker**：命名问题挡住一个正确性修复，消耗信任。
- **一次提十个问题**：作者改完三个，剩下七个在下一轮又出现，review 变成拉锯。
- **不敢下判断**：明明看到 `preserveSet<CFGAnalyses>()` 但没有 CFG 更新，却只写「maybe check this?」。

LLVM 的高频 reviewer 不是靠长文获胜的。本机统计的形态是：中位数 60（arsenm）/ 68（RKSimon）字符，33% / 29% 的评论不超过 40 字符，一条评论只针对一个问题，收尾常常只有 `LGTM - cheers`。这不是说短就好，而是说**每条评论要能落到一行 diff 上，并且只解决一个问题**。

## 先建立直觉

一条有效的 review comment 有五个槽位：

```
[位置]  llvm/lib/Transforms/.../Foo.cpp:123
[断言]  这里在 X 情况下会产生非法 IR / 丢失 poison / 多算一次
[证据]  opt -passes=... 的实际输出 / 源码不变量 / 测试缺口
[改法]  最小修复：加 freeze / 用 DomTreeUpdater / 补 CHECK-LABEL
[级别]  blocker / nit / question / optional
```

缺任何一个槽位，评论就会变形：

- 缺位置：作者不知道改哪。
- 缺断言：变成「我觉得」。
- 缺证据：reviewer 和作者各说各话。
- 缺改法：作者可能改错方向。
- 缺级别：作者不知道这条是否阻塞合入。

## 32.1 七种评论形态（样本统计）

本机 skill 把高频 reviewer 的评论分成七类。它们不是官方分类，但对写作很有用：

| 形态 | 作用 | 真实语气 | 你该提前做什么 |
| --- | --- | --- | --- |
| F1 单句短问 | 指出一个具体点 | `v16s32 and v8s64?` | 在每处「我做了选择」写一句反问 |
| F2 必要性质疑 | 问「为什么需要这个」 | `Why do you need the separate paths?` | 先答「没有它会怎样」 |
| F3 缺失枚举 | 问漏掉的组合 | `what about 8i32/16i32?` | 列宽度×元素×feature 矩阵 |
| F4 删除指令 | 要求删冗余 | `unnecessary forward declarations` | 逐行问「删了还过不过」 |
| F5 指向现成机制 | 要求复用 | `We have widening patterns in ...` | 先 grep 现成实现 |
| F6 直接给补丁块 | 用 suggestion 替换 | suggestion 块 | 能被 3 行替换的先自己写 |
| F7 流程短句 | rebase、LGTM | `please fix merge failure` | 提交前 rebase、同步描述 |

一轮 review 的典型节奏是：第 1 轮问设计（F2/F5）→ 第 2 轮问覆盖（F3）→ 第 3 轮问格式（F4/F1）→ LGTM。**第一轮被问「为什么需要这个」，后面补测试救不回来。**

## 32.2 四条技术立场（样本归纳）

形态决定「怎么说」，立场决定「要求什么」。本机 skill 归纳出四条高频立场：

| 立场 | 判据 | 反例 |
| --- | --- | --- |
| P1 正确性来自结构 | 合法性由 legalizer/verifier 保证，selector 不做特判 | 在 selector 里 skip 某类值 |
| P2 机制优先 | 已有 pattern/analysis/helper 先用 | 新加 gate 掩盖缺 pattern |
| P3 最小表面积 | 删 > 改 > 加；抽象要付租金 | 新 pseudo、新 helper、magic number |
| P4 可逐 commit 审阅 | baseline 在前，一 commit 一件事 | 测试排在代码后、PR 混两件事 |

这四条不是 LLVM 官方文件里的条文，而是从真实 review 样本归纳出的稳定偏好。你在写 comment 时可以直接用它们做判断依据，但不要把「P1 说」当成引用官方规范；要落到具体不变量或源码位置。

## 32.3 严重性：blocker、nit、question、optional

| 级别 | 含义 | 作者必须做什么 | 例子 |
| --- | --- | --- | --- |
| blocker | 不修不能合入 | 修，或给出反驳证据 | 非法 IR、miscompile、缺回归测试 |
| question | 我需要理解，未必是错 | 回答；如果成立再改 | 「这个分支在 X 下会怎样？」 |
| nit | 风格/命名/格式 | 可改可不改，不应阻塞 | 变量名、注释错字 |
| optional | 建议，超出本 PR 范围 | 可以拒绝 | 「以后可以考虑合并这两个 helper」 |

写作时最好显式标级别，尤其是 nit：

```text
nit: 这个变量名可以叫 `Inserted`，但不影响正确性。
```

不标级别会让作者把 nit 当 blocker，或者把 blocker 当 nit。LLVM 的 `CodeReview.md` 也提醒：如果你 review 了但不想阻塞，要明确说；如果你给了 suggestion 但不想让它被当成强制要求，也要说清楚。

## 32.4 案例一：baseline 测试 PR 怎么 review

`25e91b6837aa` 是一个纯测试的 precommit：

```text
[Attributor][NFC] Precommit tests for attributes derived from non-exact definitions (#243247)
```

Review 这个 PR，不需要懂 Attributor 的全部实现，但需要问三个问题：

1. **它真的是 NFC 吗？** 测试在当前代码上通过才是 NFC；如果失败，它就是「期望行为变更」，标题不该写 NFC。
2. **它锁的是现状还是期望？** commit message 说「showing that the Attributor derives ... although another definition might be used instead」，这听起来是在记录一个可疑行为，为后续修复做 baseline。Reviewer 应该要求作者明确：后续 PR 会让这些 CHECK 翻转吗？
3. **测试是否最小？** 96 行、两个文件、多个函数。要检查每个函数是否对应一个独立条件（`linkonce_odr` + load / `linkonce_odr` + assume / 调用者传播），而不是重复堆样例。

一条合格的 comment 可以写成：

```text
[question] llvm/test/Transforms/Attributor/nonnull.ll:1640
这些 CHECK 锁的是当前推导结果。后续修复会让它们翻转吗？
如果是，建议在 commit message 里写明「baseline for #N」，避免后人误以为这是期望行为。
```

这条评论满足了五个槽位：位置、断言（锁现状）、证据（commit message 的措辞）、改法（写明 baseline）、级别（question）。它不冒犯，也不放过关键歧义。

## 32.5 案例二：一个高质量 bug fix 怎么 review

`9ac5159b3c46` 是一个接近教科书式的 PR：

- 11 行实现，131 行测试；
- commit message 给出语义理由（use flags 只在使用的点成立）；
- 保守方向明确（return false，保留 `umin_seq`）；
- 两个测试文件覆盖不同 exit 形态。

Review 这种 PR，重点不是找错，而是**验证它是否真的覆盖了声明的边界**：

1. `isCanonical()` 的 false 分支有测试吗？
2. predicated exit 的测试是否覆盖「early exit taken first」的情况？
3. 保守返回 false 会不会让某个既有优化退化？有没有 compile-time/performance 证据？
4. 注释是否解释了「为什么 use flags 特殊」，而不只是「保守返回 false」？

一条有价值的 comment：

```text
[question] llvm/lib/Analysis/ScalarEvolution.cpp:4011
`!isCanonical()` 直接返回 false 是保守的。能不能在注释里补一句：
use flags 只在 use 点成立，提升到 preheader 后前提不再成立？
现在读者只能从 commit message 得到这个理由。
```

这是 nit 级，但它把「commit message 里的知识」搬进了代码，符合 LLVM 对注释的要求。对高质量 PR，reviewer 的价值往往在于**补上可维护性**，而不是找 bug。

## 32.6 案例三：2 行 assert 修复怎么 review

`81e0dbceab0d` 删了两行：

```cpp
-    // Renumber basic blocks so that they are dense for crossing value analysis.
-    F.renumberBlocks();
```

commit message 的理由是：

```text
GenericCycleInfo was not invalidated, so asserted when profcheck is
enabled that the block number epoche is outdated.

Remove the `renumberBlocks` call to fix this.
Given that e.g. `GenericCycleInfo` already uses the numbers as block
indices, they should still be sufficiently dense.
```

Review 这种 diff，最该问的是「为什么删是安全的」，而不是「为什么只删两行」：

```text
[blocker?] llvm/lib/Transforms/Coroutines/CoroSplit.cpp:2346
删除 `renumberBlocks` 后，还有谁依赖 block number 连续？
`GenericCycleInfo` 用编号当索引，但如果编号出现空洞，`getCycle` 的数组访问还安全吗？
能否在 commit message 或注释里给出这个不变量的依据？
```

以及测试缺口：

```text
[question] 这个 assert 只在 profcheck + assertions 下触发。
哪个现有测试在删除前会失败？如果没有，能否加一个最小 coroutine 测试，
在 `-enable-profcheck` 下覆盖这条路径？
```

注意语气：不是「你删错了」，而是「请给出不变量依据 / 测试证据」。即使最后证明作者是对的，这两条评论也提高了 patch 的可维护性。

## 32.7 案例四：被 revert 的 NFC

`2b3f0f084f77`：

```text
Revert "[CodeGen][NFC] Factor MachineBlockPlacement::alignBlocks into reusable helper (#241995)" (#243994)

This reverts commit 6447d4dda5706f5d7946f86c1225fd964abfb39e (#241995)
at a reviewer's request while concerns about the change are addressed.
```

这个案例的教学点不是「怎么 revert」，而是**review 时如何避免走到这一步**：

- NFC 重构也要说明「为什么现在做」「影响哪些调用点」「为什么不会改变行为」。
- 如果重构跨了多个文件/多个 target，reviewer 有理由要求拆成「先加 helper（NFC）+ 再迁移调用者」。
- 如果 reviewer 有设计顾虑，作者应在合入前解决，而不是合入后再补。

一条预防性 comment：

```text
[blocker] 这个 NFC 改了 198 行 MachineBlockPlacement.cpp，但没有任何行为等价的测试证据。
能否拆成两个 commit：先引入 helper 并保持调用点不变，再逐个迁移？
这样 revert 时不会把整个重构一起回滚。
```

## 32.8 一份完整的 review 报告

把上面的方法压成一份可发布的报告。格式如下（这是本机 `llvm-reviewer-patterns` skill 使用的结构，不是 LLVM 官方模板）：

```text
[F2 / P3] llvm/lib/Transforms/Foo/Foo.cpp:123  blocker
他会问: "Why do you need this new helper?"（样本形态 F2）
为什么触发: 新增了一个只被调用一次的 helper，且已有 `getFooOrNull` 能做同样的事
最小改法: 删掉 helper，调用点直接用 `getFooOrNull`；如果确实需要，说明它比现有 API 多保证什么

[F3 / P1] llvm/test/Transforms/Foo/basic.ll:45  blocker
他会问: "what about the i8 case?"（样本形态 F3）
为什么触发: 新逻辑按 bitwidth 分支，但测试只覆盖 i32/i64
最小改法: 补 i8 的 RUN/CHECK，或在代码里明确 i8 走 fallback 并加注释

[F4] llvm/lib/Transforms/Foo/Foo.cpp:12  nit
他会问: "unnecessary include?"
为什么触发: 新增 include 但没有使用对应符号
最小改法: 删除

最可能被卡在: 新增 helper 的必要性（P3 最小表面积）
```

报告纪律：

- 每条必须能落到 diff 的某一行；落不到的降级成 question。
- 只列有证据的问题；「我觉得」不要写成 blocker。
- 排序：blocker → nit → optional。
- 最后一行给出「最可能被卡住的点」，帮助作者 prioritise。

## 32.9 语气与协商

LLVM 的 review 文化偏直接，但直接不等于粗鲁。几条实用规则：

1. **对代码不对人**：说「这个分支会产生 poison」，不说「你没考虑 poison」。
2. **承认不确定**：`I'm not sure this is safe; can you explain?` 比假装确定更好。
3. **给出可行动的改法**：能写 suggestion 就写，能指出现成机制就指。
4. **不要扩大范围**：本 PR 没改的问题，标 optional 或另开 issue。
5. **该夸就夸**：测试覆盖好、commit message 清楚，可以直接说。这不是客套，是在强化好实践。
6. **LGTM 要具体**：`LGTM, thanks for the tests` 比单字 `LGTM` 更有信息量；如果不确定，用有条件 approval。

`CodeReview.md` 明确说，如果你 review 了但不想阻塞，要说出来；如果你给了 suggestion 但不想强制，也要说。**级别和意图要说清楚，不要让作者猜。**

## 踩雷集锦

1. **把 nit 写成 blocker**：命名/格式问题不应挡住正确性修复；标级别，别让作者猜。
2. **一条评论塞三个问题**：作者改了两个，第三个在下一轮又冒出来；一条评论一个可执行动作。
3. **只说「这里不对」**：没有证据和最小改法，作者只能猜；review 变成拉锯。
4. **用「你为什么不……就行了」开头**：反问语气暗示对方愚蠢；改成「能否用现成的 X？」。
5. **对 NFC 放松警惕**：`2b3f0f084f77` 证明 NFC 也会被 revert；行为等价需要证据。
6. **把统计当规范**：F1–F7/P1–P4 是样本归纳，不是官方条文；引用时要落到具体 diff 和不变量。
7. **LGTM 时没有确认所有反馈**：无保留 LGTM 意味着你确认了所有 review 线程都已处理；不确定就给有条件 approval。

## 动手练习

1. 对 `25e91b6837aa` 写三条评论：一条问 NFC 状态、一条问 baseline 意图、一条问测试最小性；每条都带级别和最小改法。
2. 对 `9ac5159b3c46` 写一条 nit：把 commit message 里的语义理由搬进代码注释；要求给出具体插入位置。
3. 对 `81e0dbceab0d` 写一条 blocker 或 question：要求作者证明删除 `renumberBlocks` 后没有代码依赖编号连续；再写一条测试缺口。
4. 找一个最近合入的 LLVM PR，按 32.8 的格式写完整报告；限时 20 分钟，最后一行写出「最可能被卡住的点」。
5. 把你写的报告和真实 reviewer 的评论对照，统计：你漏了哪些形态（F1–F7）、哪些立场（P1–P4）、哪些级别标错。

## 本章重点整理

- 一条有效评论有五个槽位：位置、断言、证据、最小改法、级别。
- 高频 reviewer 的评论短、单点、可行动；长文分析往往不如一条精确的 blocker。
- F1–F7 是形态，P1–P4 是立场；形态决定怎么说，立场决定要求什么。
- 严重性要显式标：blocker / question / nit / optional；不要让作者猜。
- baseline 测试要说明锁的是现状还是期望；NFC 也要证明行为等价。
- 高质量 PR 的 review 重点在覆盖与可维护性，不只是找 bug。
- 报告最后要给出「最可能被卡住的点」，帮助作者 prioritise。

## 自我检核

- [ ] 能说出有效评论的五个槽位，并检查自己写的每条评论是否齐全
- [ ] 能区分 F1–F7 形态，并在真实 PR 里各找一个例子
- [ ] 能解释 P1–P4 立场，并知道它们不是官方规范
- [ ] 能正确标级别，尤其是 nit 和 blocker 的边界
- [ ] 能对一个 baseline 测试 PR 问出「锁现状还是期望」
- [ ] 能对一个 2 行删除的 diff 问出不变量依据和测试缺口
- [ ] 知道 NFC 为什么也会被 revert，以及如何预防
- [ ] 能产出 blocker → nit → optional 排序的完整报告，并写出最可能被卡的点

## 延伸阅读

- **[LLVM Code Review Policy](https://llvm.org/docs/CodeReview.html)**
  - **读哪里**：`Acknowledge All Reviewer Feedback`、`LGTM`、`Splitting Requests`
  - **为什么值得读**：本章的级别/协商规则以官方措辞为准；尤其是「明确说不阻塞」的要求
- **[LLVM Developer Policy: Commit messages](https://llvm.org/docs/DeveloperPolicy.html#commit-messages)**
  - **读哪里**：整节；重点看「why 比 what 重要」「标题简洁」
  - **和本章的关联**：32.5 的「把 commit message 的知识搬进注释」来自这里
- **[llvm-reviewer-patterns skill](../../.codex/skills/llvm-reviewer-patterns/SKILL.md)**
  - **读哪里**：SKILL.md 的 F1–F7、P1–P4，以及 `references/` 下的样本
  - **为什么值得读**：本章的形态统计来自这里；它是样本分析，适合做写作检查表，不适合当官方规范
- **[LLVM GitHub PRs](https://github.com/llvm/llvm-project/pulls)**
  - **读哪里**：找 3 个已合入、3 个被 revert 的 PR，读完整评论线程
  - **和本章的关联**：真实线程的节奏比任何模板都更能训练你的评论长度和级别判断

→ [Final Project — 从 issue 到 review-ready PR](./final-project-review-ready-pr.md)
