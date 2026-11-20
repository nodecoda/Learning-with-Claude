# Ch 27 — LLVM review 制度：GitHub PR、reviewer 职责、LGTM、revert、RFC

> **目标**：把 LLVM 的 review 从「礼貌地提意见」还原成一套有明确权限、时限、回滚规则和共识机制的工程制度。学完你能判断一个 PR 现在处在流程的哪一步、谁的话才算数、什么时候该要求拆 PR、什么时候该 revert 而不是继续修。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（LLVM 24.0.0git，assertions ON）。构建缓存的 worktree 是 `a5eaf194d8b6`，与源码 hub 不是同一 commit；行号以 7c7a 为准。

> **依据**：`llvm/docs/CodeReview.md`、`llvm/docs/DeveloperPolicy.md`，源码树 `7c7a9072f736`。本章引用的制度条文都能在这两份文件里查到；社区实践部分会明确标「实践」而不是「规定」。

## 为什么需要这个？

很多人把 LLVM review 理解成「等一个大佬说 LGTM」。这会带来两个错误：

- **把 reviewer 的意见当成建议**。实际上，review 是准入门槛：没有明确 approval，patch 不应该合入；作者对 post-commit 反馈也有持续义务。
- **把 revert 当成惩罚**。LLVM 的制度明确把 revert 当成保持 tip-of-tree 健康的常规动作，「revert to green」优先于「fix forward」。不理解这一点，你会把一次正常回滚当成职业事故。

更现实的一点：你想达到 reviewer 水平，先要知道**reviewer 的权限和责任边界**。你能不能说「这个必须拆」？能不能在别人已经 LGTM 之后继续提 blocker？能不能自己 revert 别人的 patch？这些都有成文或稳定的社区惯例。

## 先建立直觉

一个 LLVM PR 的生命周期不是「提交 → 等 → 合入」，而是下面这条带分支的路径：

```
作者开 PR
   │
   ├─ 首次贡献？──► premerge CI 需要 maintainer 先点一次
   │
   ▼
reviewer 阅读（可能多轮）
   │
   ├─ 设计层面有异议 ──► 要求 RFC / 拆 PR / 先落 baseline
   │
   ├─ 测试不足 ────────► 要求补 lit/MIR/unit test
   │
   ▼
明确 approval（通常含 "LGTM"）
   │
   ├─ 有 required reviewer ──► 必须全部 approval
   │
   ▼
作者合入
   │
   ├─ buildbot 红 / 发现 miscompile ──► revert（常见且正常）
   │
   ▼
post-commit review 继续
   │
   └─ 实质问题 ──► 再 revert，线下修，重新 review
```

这张图里有两个和「写代码」无关、但决定你能否成为 reviewer 的点：

1. **approval 是权限行为，不是情绪表达**。说 LGTM 的人要为「所有反馈都已处理、社区共识足够」负责。
2. **revert 是维护动作，不是对作者的否定**。制度甚至鼓励作者自己 revert。

## 谁能 review，谁必须 review

`CodeReview.md` 把 reviewer 分成两类，措辞很直接：

- **非专家也应该 review**：你不需要是某个领域的专家才能提问。「如果这段代码你看不懂，你很可能不是唯一一个。」提问本身会促使作者改善可读性、补注释或补测试。
- **专家被期待 review**：如果你是被改动区域的 maintainer，而没有其他专家在 review，你**必须**要么自己 review，要么安排一个专家来 review。

这条规则对学习者的意义：**你可以从「非专家提问」开始积累 reviewer 经验**，不需要等到「完全懂」才发言。但你的 comment 要区分「我不理解，能否解释」和「这里错了」。前者永远安全，后者需要证据。

`DeveloperPolicy.md` 的 Maintainers 一节还定义了 maintainer 的职责：确保 commit 得到高质量 review、确认和评论 issue、调解 review 分歧、参与 RFC、协助 backport、作为贡献者的联系人。换句话说，maintainer 不是「代码写得最多的人」，而是**对某个区域的 review 质量负责的人**。

## approval 的规则

### 一个 approval 就够，除非有 required reviewers

`CodeReview.md` 的规则：

- 通常**一个** reviewer 的 approval 就够。
- 如果 PR 指定了 required reviewers，则必须**所有** required reviewers 都 approval。
- 不能把「没人反对」当成 approval，也不能用 deadline 逼别人表态。

### LGTM 不是口号

官方对「无保留 LGTM」的定义很重：reviewer 有责任确认**之前所有讨论和反馈都已处理**，并且其他 reviewer 几乎肯定会满意。如果不确定，应该给**有条件 approval**，例如「LGTM, but please wait for @someone」。

这条规则解释了一个常见现象：为什么有经验的 reviewer 会说「LGTM, but wait for X」。那不是客套，是在履行「我替整个社区做判断」的责任。

### 不要无意中阻塞

如果你 review 了但不想阻塞合入，要**明确说出来**。LLVM 的礼貌惯例是等所有 reviewer 满意再合入；如果你不打算再看，就要告诉作者，否则作者会一直等。

### 快速 approval 之后的等待

官方建议：非平凡 patch 即使很快拿到 approval，也值得等至少 24 小时再合入，尤其要考虑周末和节假日。原因是 LLVM 是全球社区，其他时区的人可能还没看到。

## 什么时候必须 RFC

`CodeReview.md` 明确列出需要先发 RFC 的情况，包括：

- 改变 LLVM Language Reference 的改动（例如新增 target-independent intrinsic）；
- 在 Clang 中新增语言扩展；
- 对用户或下游代码库有重大影响的改动，reviewer 可以要求先达成 RFC 共识。

RFC 不是「大改动才需要」的模糊规则，而是**改变公共契约**的改动需要。判断标准可以压成一句话：**这个改动会不会让树外的人必须跟着改？** 会，就先讨论。

## 测试是门槛，不是附件

`DeveloperPolicy.md` 的 Test Cases 一节把要求写得很硬：

- **任何 bug fix 和 new feature 都必须有测试**。
- 功能改动优先用 FileCheck + 最贴近被改代码的工具：IR 变换用 `opt`，后端用 `llc`，前端用 `clang`。
- 库改动（例如 Support）如果没有工具入口可观察，用 `unittests`。
- 测试必须**targeted**；大输入要先用 `llvm-reduce` 缩小，不允许把整个失败程序丢进 `llvm/test`。
- 不要放社区无法访问的链接（内部 bug tracker、公司文档）；测试里的注释要能独立提供上下文。

这四条直接变成 Ch 31 的 review checklist。现在先记住：**reviewer 问「测试呢」不是走流程，而是在检查这个改动未来能不能被保护住。**

## 质量标准与 revert

`DeveloperPolicy.md` 的 Quality 一节列出合入前的最低标准：

1. 遵守 LLVM Coding Standards；
2. 至少在一个平台上干净编译（无 error、无 warning）；
3. bug fix / new feature 带测试；
4. PR 要通过 premerge checks（首次贡献者需要 maintainer 先触发一次）；
5. 源码和测试里的链接必须公开可用，且只是补充信息，不能承载关键上下文。

合入之后，committer 仍要对后续问题负责：buildbot 红、test-suite 回归、编译时间退化、下游 target 的正确性/性能回归，以及由此产生的 GitHub issue。

### revert 的规范

LLVM 的 revert 文化和其他开源项目不同，官方原话是「we tend to make much heavier use of reverts」，并且「revert to green」优先于「fixing forward」。具体规则：

- 发现严重问题，**先 revert，再线下调查**，修好后可以重新提交，必要时再过一轮 review。
- 如果 buildbot 坏了且不能快速修，revert。
- 如果有人给出可复现的问题，revert 并线下调查。
- 收到实质性的 post-commit review 反馈，revert 后再处理。
- 其他贡献者要求你 revert，通常应该照做，然后线下讨论（除非 revert 会让树更不稳定）。
- revert 别人之前，要确信 revert 能改善 tip-of-tree；有时 revert 一个系列中的单个 patch 反而更糟。
- revert commit 必须说明原因；理想情况下附一个公开可复现的测试用例。
- revert 的时效：两小时前的改动可以直接 revert；两年前的改动不该直接 revert；模糊地带通常是几天内。

对 reviewer 的启示：**你在 review 时放过的设计问题，可能变成别人明天要 revert 的理由。** 第一轮问「为什么需要这个」比第三轮补测试更重要。

## 沟通渠道与节奏

官方列出的渠道：

| 渠道 | 用途 | 注意 |
| --- | --- | --- |
| GitHub PR | pre-commit review 的主战场 | 所有实质讨论应留在 PR，方便回溯 |
| Discourse | RFC、设计讨论、跨项目共识 | 大改动先在这里达成方向 |
| Discord | 快速问「谁适合 review」「这个区域谁熟」 | 不替代 PR 上的正式结论 |
| GitHub Issue | bug 报告与复现 | 修 bug 的 PR 应关联 issue |

官方给的加速 review 建议也很实用：

- **先 review 别人的 patch**。你帮别人，别人才更愿意帮你；「goodwill is our currency」。
- **ping 的频率**：紧急的每几天一次并说明理由；不紧急的一周一次。
- **拆小 patch**，系列 patch 用 `[N/M]` 标顺序。
- 需要帮助时去 Discord 问「谁适合 review」，不要随机 @ 人。

## 成为 reviewer 的实际路径

制度上没有「reviewer 资格考试」。可操作的路径是：

1. **从非专家问题开始**：在你读得懂的区域提「这段为什么这样写」「这个测试锁住了什么」。这类问题不会冒犯，且能建立信誉。
2. **在一个区域形成判断力**：连续读某个子系统（例如 InstCombine、SelectionDAG、某 target）的 PR，积累「这类改动通常错在哪」的模式。
3. **先 review 自己熟悉的 diff 类型**：test-only、NFC、小 bug fix，再进入设计层。
4. **用证据说话**：能指出具体不变量、具体测试缺口、具体下游影响，而不是「我觉得不好」。
5. **承担后续**：你 approval 的 patch 出问题，你要参与定位。这是 maintainer 责任的前置。

`CodeReview.md` 还有一条对新人很重要的提醒：**review 时明确说「我只是提问，不阻塞」是完全可以的**，并且新贡献者通过 review 来学习代码库是被鼓励的。

## 一个真实 PR 的制度面拆解

下面是一个抽象但常见的场景，用来练「先看流程，再看代码」：

> 一个 PR 同时做了三件事：修一个 InstCombine 的 miscompile、重构 `ValueTracking` 的一个 helper、把三个测试文件改成新的 CHECK 格式。

从制度面看，这个 PR 至少有三个问题：

1. **跨关注点**：bug fix 和重构混在一起。一旦 revert，无法只回滚有问题的部分。reviewer 会要求拆成「baseline 测试」「bug fix」「NFC 重构」。
2. **测试顺序**：如果 baseline 测试是在修复之后才加的，它无法证明修复前会失败。正确顺序是先加一个在旧代码上失败的测试，再加修复。
3. **review 负担**：三个测试文件的格式改动会淹没真正的语义 diff。除非格式改动是机械的且单独一个 commit，否则应拆出。

这三条都不需要你懂 InstCombine 的细节就能提。它们是**流程层 blocker**，也是新 reviewer 最容易建立价值的地方。

## 权限矩阵：谁的话算数

制度里混着三种身份，权限不同。把它们分开，你才知道一条评论是「建议」还是「门槛」：

| 身份 | 能做什么 | 不能做什么 | 典型约束 |
| --- | --- | --- | --- |
| 任何贡献者 | 提 issue、开 PR、在 PR 下提问、review 并给反馈 | 不能给最终 approval | 非专家提问被明确鼓励 |
| reviewer（有 commit access） | 给 approval / 有条件 approval、要求拆 PR、要求补测试 | 不能绕过 required reviewers | 要确认所有反馈已处理 |
| maintainer | 确保区域得到高质量 review、调解分歧、参与 RFC、协助 backport | 不能单方面推翻社区共识 | 对自己区域的 review 质量负责 |

注意「有 commit access」不是形式条件：`CodeReview.md` 说最终 approval 的 reviewer 应该有 commit access。这也是为什么新贡献者的 LGTM 不算数，但提问和反馈永远有价值。

## 制度背后的两个设计目标

LLVM 的 review 规则看起来比很多项目严格，原因是它在两个目标之间做平衡：

1. **tip-of-tree 必须随时可用**。LLVM 有大量 buildbot、下游项目和 nightly 测试；一个坏 commit 会阻塞所有人。所以制度偏向「先 revert 再修」，而不是「让作者慢慢修」。
2. **共识比效率优先，但不无限期等待**。重大设计要先 RFC；review 可以多轮；但官方也明确反对用 deadline 逼表态，反对无意义地拉长 review。判断标准是「是否改变公共契约」，不是「改动行数」。

这两个目标解释了很多看似矛盾的现象：为什么小 patch 可以快速合入，为什么大 patch 即使测试全绿也可能被要求拆，为什么 revert 不需要经过作者同意。

## Incremental development：为什么大 patch 必须拆

`DeveloperPolicy.md` 的 Incremental Development 一节把「大分支开发」的缺点列得很具体：

- 分支要定期 merge mainline，冲突解决成本高；
- 社区倾向于忽略分支上的工作；
- 分支合并回来时是一个巨大的 diff，极难 review；
- 分支不经过 nightly 测试；
- 大改动在全部完成前往往不能用，拆小可以提高「至少一部分能落地」的概率。

官方给的做法是：

- 先做前置的 API cleanup、独立 bug fix；
- 把相关工作拆成互不依赖的集合；
- 每个改动尽可能小，能独立 review；
- 常见模式是「先加新 API，逐步迁移调用者，再替换底层实现」。

这条规则直接变成 reviewer 的第一轮问题：**这个 PR 能不能拆？baseline 能不能先落？** 它不是刁难，而是 LLVM 对「可审阅性」的硬要求。

## IR 兼容性与 API 影响：reviewer 的额外检查

如果 PR 改了 IR 或 C API，review 多一层：

- **文本 IR 不承诺向后兼容**，但改动应反映到 `llvm/test/Bitcode/compatibility.ll`。
- 当前 LLVM 能加载 3.0 以来的 bitcode；每个 X.Y release 后要复制 `compatibility-X.Y.ll` 并用该版本构建出 `.bc`。
- 新版本可以忽略旧版本的 feature，但**不能 miscompile**。
- Debug metadata 在升级时会被丢弃；非 debug metadata 定义为可安全丢弃。
- C API 是「best effort」稳定；改 C API 必须在 release notes 里写清楚，并带测试。

Reviewer 看到 IR 改动时，应该问的不是「这个 flag 名字好不好」，而是：**旧 bitcode 还能不能正确加载？丢弃这个 metadata 会不会改变语义？下游手写 IR 的人会不会被打断？**

## post-commit review 怎么处理

合入不等于结束。`CodeReview.md` 明确说 post-commit review 是被鼓励的，作者有义务及时回应；如果问题严重到「pre-commit 时也会提」，社区成员可以要求 revert。

实际操作顺序通常是：

1. 在 commit thread 或新 PR 里给出**可复现**的问题（IR、命令、期望/实际输出）。
2. 如果问题会导致 miscompile 或 buildbot 红，先 revert，再讨论修法。
3. 作者修好后重新提交，commit message 要说明「之前为什么坏、这次怎么修」。
4. 如果原 commit 已经很久，优先开新 PR，而不是在原 thread 上追加。

这条流程对 reviewer 的要求是：**post-commit 反馈的门槛不比 pre-commit 高**。你事后看到的问题，仍然值得提；但要用证据，而不是「我早就觉得不对」。

## 踩雷集锦

1. **把「没有人反对」当成 LGTM**：官方明确说不能假设沉默 approval，也不能用 deadline 征集反对。没有明确 approval 就不该合入。
2. **在有 required reviewers 时只拿到一个 approval 就合入**：required reviewers 必须全部 approval，这条没有例外。
3. **用「LGTM」结束一个还有未处理评论的 PR**：无保留 LGTM 意味着你确认了所有反馈都已处理；不确定就给有条件 approval。
4. **把 revert 理解成对作者的人身否定**：制度把 revert 定义为维护 tip-of-tree 的常规动作，作者甚至可以自己 revert。正确反应是感谢并线下修。
5. **在 PR 里讨论 RFC 级设计**：改变 LangRef、Clang 语言扩展、重大下游影响的改动，应该先有 RFC 共识。在 PR 评论里拉锯几十轮，通常是因为方向没先定。
6. **要求补测试但不说「测试要在旧代码上失败」**：这是测试 review 最常见的漏洞，Ch 31 会展开。

## 动手练习

1. 打开 `llvm/docs/CodeReview.md`，找出「什么情况下需要 RFC」的三类例子，并各举一个 LLVM 历史上的真实改动（可以用 `git log --grep` 搜 `RFC`）。
2. 找一个最近合入的 LLVM PR，按本章的生命周期图标注它经过了哪些步骤：首次贡献？required reviewers？几轮 review？有没有 revert 记录？
3. 找一个被 revert 的 commit（`git log --grep='Revert'`），读 revert commit message，判断它属于本章哪一条 revert 规则。
4. 假设你要提交一个改变 `llvm.assume` 语义的 patch，列出你在开 PR 前必须做的三件事（RFC、LangRef、测试）。
5. 写一段 5 行以内的 review comment，指出一个 PR 混了两个关注点，要求拆分，但不评价代码好坏。

## 本章重点整理

- review 是准入门槛：明确 approval 才能合入；沉默不是同意。
- 一个 approval 通常够，但 required reviewers 必须全部通过；LGTM 意味着你确认了所有反馈。
- 改变公共契约（LangRef、Clang 扩展、下游影响）先走 RFC。
- 测试是合入门槛：bug fix 必须有能在旧代码上失败的回归测试。
- revert 是常规维护动作，「revert to green」优先；revert 要说明原因并尽量给公开复现。
- 新 reviewer 的价值可以从流程层开始：拆 PR、baseline 顺序、测试缺口。

## 自我检核

- [ ] 能说出「无保留 LGTM」和「有条件 approval」的区别，以及各自的责任
- [ ] 知道 required reviewers 与普通 reviewer 在 approval 规则上的差异
- [ ] 能判断一个改动是否需要 RFC，并说出判断依据
- [ ] 能复述 revert 的五种典型触发条件和时效惯例
- [ ] 知道非专家 review 的合法边界：提问 vs 断言
- [ ] 能解释为什么「先加 baseline 测试，再加修复」比反过来更有说服力
- [ ] 知道 premerge CI 对首次贡献者的特殊流程
- [ ] 能说出成为 reviewer 的三步实际路径，而不是「等资历」

## 延伸阅读

- **[LLVM Code-Review Policy and Practices](https://llvm.org/docs/CodeReview.html)**
  - **读哪里**：`General Policies`、`Code-Review Workflow`、`Who Can/Should Review Code?`、`LGTM`
  - **为什么值得读**：本章的制度条文全部来自这里；原文比二手总结更准确，尤其是「qualified approval」的措辞
- **[LLVM Developer Policy](https://llvm.org/docs/DeveloperPolicy.html)**
  - **读哪里**：`Code Reviews`、`Maintainers`、`Test Cases`、`Quality`、`Patch reversion policy`
  - **和本章的关联**：Ch 31 的测试标准和本章的 revert 规则都以这份文件为准
- **[LLVM RFC Process](https://llvm.org/docs/RFCProcess.html)**
  - **读哪里**：整篇；重点看什么类型的改动需要 RFC、RFC 的生命周期
  - **为什么值得读**：避免在 PR 里讨论本应在 Discourse 达成共识的设计
- **[LLVM GitHub Pull Requests](https://github.com/llvm/llvm-project/pulls)**
  - **读哪里**：按 label 过滤 `bug`、`miscompile`、`tablegen`，各读 5 个已合入的 PR
  - **和本章的关联**：把制度条文对照真实 PR 的评论节奏，比背规则有效

→ [Ch 28 — 读一个 PR 的顺序：diff 形态、baseline commit、拆分](./28-reading-a-pr.md)
