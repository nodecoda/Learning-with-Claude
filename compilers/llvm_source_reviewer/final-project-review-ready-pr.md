# Final Project — 从 issue 到 review-ready PR

> **目标**：交付一个**可以被真实 reviewer 审阅**的 LLVM patch：有可复现的问题、最小修复、会在旧代码上失败的测试、诚实的风险说明，以及一份你自己写的预审报告。这个项目不是「再做一个 pass」，而是把 Ch 0–32 的方法压成一个完整工作流。

> **环境**：`/home/ubuntu/llvm/llvm-project`（hub，只读）→ 在 `/home/ubuntu/llvm/trees/<task>` 建 worktree → 用 `/home/ubuntu/llvm/builds/<task>` 构建。工具链入口 `source /home/ubuntu/llvm/toolset/env.sh`。具体构建/测试/PR 规则以 `/home/ubuntu/admin/AGENTS.md` 为准；本文件只给项目结构和验收标准。

## 这个项目要解决什么

前 32 章分别训练了：读源码、IR 不变量、pass/analysis、中端/后端、测试、review 写作。真实的上游贡献要求这些能力**同时**出现：

```
一个 issue / 可疑行为
   │  1. 复现：最小 IR / 最小 C / 最小 MIR
   ▼
根因定位
   │  2. 从现象反推到 pass / analysis / target 代码
   ▼
最小修复
   │  3. 只改必要的地方；不顺手重构；不扩大 API
   ▼
测试证明
   │  4. baseline（旧代码失败）+ 回归（修复后通过）
   ▼
自审
   │  5. 用 Ch 29–31 的检查表找自己的漏洞
   ▼
PR + 预审报告
   │  6. 描述、风险、拆分、F1–F7 预判
   ▼
回应 reviewer
   │  7. 逐条 ACK、给证据、该拆就拆、该 revert 就 revert
```

Final 的评分不看「改动多大」，而看**证据链是否闭合**。一个 5 行修复配一个会在旧代码上失败的测试，比 500 行新功能没有回归测试更接近 reviewer 的要求。

## 选择你的 track

### Track A（推荐）：真实上游 issue

从 LLVM GitHub issue 里选一个你区域内的 bug，优先满足：

- 有公开复现（IR/C++/MIR）；
- 影响 `llvm/lib/Transforms`、`llvm/lib/Analysis`、`llvm/lib/CodeGen` 或某个 target；
- 不是「加一个新功能」，而是「修一个错误行为」；
- 你能在 2–3 天内做出最小修复。

筛选命令（需要 `gh` 登录；本机 `gh` 指向 fork，查上游要显式 `-R`）：

```bash
gh -R llvm/llvm-project issue list --label "good first issue" --limit 30
gh -R llvm/llvm-project issue list --label "bug" --limit 30
```

选好后，先在 issue 下确认没有人已经在做，再开 worktree：

```bash
source /home/ubuntu/llvm/toolset/env.sh
"$LLVM_HOME"/toolset/new-task.sh fix/<short-name> <task>
llvm-use <task>
```

### Track B（自包含）：修复一个 seeded bug

如果找不到合适 issue，或你想完全离线，使用下面这个 seeded bug。它模拟一个真实的中端分析失效问题，不依赖网络。

#### B.1 种下 bug

在 worktree 里，把 `CoroSplit` 的 `renumberBlocks` 调用加回去（这正是 `81e0dbceab0d` 修复的反向操作）。先定位：

```bash
cd "$LLVM_PROJECT"   # 或你的 worktree
rg -n "setSplittedCoroutine" llvm/lib/Transforms/Coroutines/CoroSplit.cpp
```

在 `F.setSplittedCoroutine();` 之后插入：

```cpp
    // SEEDED BUG: renumber blocks without invalidating GenericCycleInfo.
    F.renumberBlocks();
```

#### B.2 复现

构建后，用 assertions + profcheck 跑 coroutine 测试：

```bash
"$LLVM_HOME"/toolset/build.sh <task>
llvm-lit -v "$LLVM_PROJECT"/llvm/test/Transforms/Coroutines --filter='coro' 2>&1 | tail -40
```

期望看到 `GenericCycleInfo` 的 epoch 断言。把真实报错、触发测试、命令记下来；如果一次跑整个目录太慢，用 `--filter` 缩小到具体测试。

> 如果本机构建没有启用 profcheck，先查 `llvm/test/lit.cfg.py` 的 `enable_profcheck` 条件，或在配置里打开对应选项。**不要伪造报错**；跑不出来就换一个 assert 类 bug，或在报告中说明环境限制。

#### B.3 修复

两种正确方向，选一个并说明理由：

1. **删掉 `renumberBlocks`**：如果没有任何代码依赖编号连续，这是最小修复（与上游 `81e0dbceab0d` 一致）。
2. **保留调用并正确失效 analysis**：如果确实需要重新编号，必须让 `GenericCycleInfo` 等依赖编号的 analysis 失效/重建。检查 `PreservedAnalyses` 与 `FunctionAnalysisManager` 的失效路径。

#### B.4 测试

- 先加 baseline 测试：在**未修复**的代码上跑，确认它会失败。
- 再加修复，确认测试通过。
- 测试要 targeted：最小 coroutine IR/MIR，带 `REQUIRES: asserts`（如果依赖断言），并说明触发条件。

#### B.5 交付

- patch（`git format-patch` 或 PR 分支）；
- 测试文件；
- 根因分析（≤1 页）；
- 预审报告（见下）。

## 交付物清单

无论 Track A 还是 B，最终提交一个目录（不要放进 LLVM 仓库）：

```
final-<your-name>/
├── README.md              # 问题、复现、修复、测试、风险
├── reproduce/             # 最小复现（.ll/.c/.mir + 命令）
├── patch/                 # git format-patch 或 diff
├── tests/                 # 新增/修改的测试
├── self-review.md         # 按 Ch 29–31 检查表自审
├── pre-review.md          # 按 Ch 32 的报告格式做预审
└── response.md            # 模拟 reviewer 的 5 条评论 + 你的回应
```

### 1. `README.md`（问题与修复）

必须回答：

- 问题是什么？影响哪些输入/目标？
- 最小复现是什么？命令、期望、实际。
- 根因在哪个文件/函数？为什么之前没被发现？
- 修复为什么最小？有没有考虑替代方案？
- 风险是什么？哪些行为可能变化？哪些测试覆盖了？
- 有没有需要拆分的部分？

### 2. `reproduce/`

- 最小 IR/C/MIR；
- 精确命令（含构建路径、flags）；
- 真实输出（修复前/后各一份）；
- 如果依赖 assertions/profcheck，写清楚。

### 3. `patch/`

- 一个 commit 一件事；baseline 测试单独 commit（如果适用）；
- commit message 按 LLVM 规范：标题简洁、body 说 why、关联 issue、必要时 `Assisted-by:`；
- 不要包含无关格式化/重命名。

### 4. `tests/`

- 至少一个回归测试，能在旧代码上失败；
- 测试 targeted，不把整个程序塞进去；
- 多函数测试用 `CHECK-LABEL`；
- 依赖断言时标 `REQUIRES: asserts`；
- 如果改了自动生成的 CHECK，说明哪些函数受影响、为什么。

### 5. `self-review.md`

用 Ch 29–31 的检查表逐条过：

```
[ ] IR 合法性：修复后所有路径都能通过 verify？
[ ] 支配关系：移动/新增的指令，def 支配所有 use？
[ ] Phi/CFG：改 CFG 的地方更新了 phi 和 DomTree？
[ ] Use-list：删除值前 use_empty()？RAUW 类型匹配？
[ ] 迭代器：边遍历边删用了 early-inc？下一个迭代器安全？
[ ] Analysis：PreservedAnalyses 诚实？依赖编号/CFG 的 analysis 失效了吗？
[ ] Poison/UB：新增/删除 flag 有证明？select/arm 的 poison 处理了吗？
[ ] 测试：旧代码会失败？弱 CHECK？CHECK-LABEL？REQUIRES？
[ ] 范围：有没有顺手重构/扩大 API/混入无关改动？
```

每条写「通过/不适用/有风险」，有风险的给出证据或补测计划。

### 6. `pre-review.md`

按 Ch 32 的格式，至少 6 条：

```
[F? / P?] path:line  级别
他会问: "..."（引用形态，不要伪造具体人名）
为什么触发: ...
最小改法: ...

最可能被卡在: ...
```

要求：

- 至少 2 条 blocker（可以是「可能被问」的设计问题）；
- 至少 2 条 nit；
- 至少 1 条测试缺口；
- 每条落到具体行；
- 不用「我觉得」当证据。

### 7. `response.md`

模拟 reviewer 给你 5 条评论（你可以基于 `pre-review.md` 自问自答），逐条回应：

- 接受：说明怎么改、改完的证据；
- 反驳：给出反例/源码/测试，不用情绪；
- 拆分：说明拆成哪几个 commit/PR，顺序是什么；
- 不修：说明为什么超出范围，另开 issue。

## 分阶段执行计划（建议 5 天）

### Day 1：选问题与复现

1. 选 Track A 或 B；
2. 建 worktree，确认构建干净；
3. 写出最小复现，保存命令与输出；
4. 用 `opt -passes=verify` / `llc` / `llvm-lit` 确认现象稳定。

**验收**：另一个人按 `reproduce/README` 能复现。

### Day 2：根因定位

1. 用 `-print-after` / `-debug-only` / `-verify-each` 缩小到 pass；
2. 用 `rg` 找到源码位置，画出调用链；
3. 写一段「根因假设」，再用实验证伪/证实；
4. 如果发现是多个问题，拆成独立 issue/PR。

**验收**：能指到具体 `file:line`，并解释为什么之前的代码会错。

### Day 3：最小修复

1. 先写 baseline 测试（旧代码失败）；
2. 再做最小修复；
3. 跑 targeted lit + 相关目录；
4. 如果修复跨两个关注点，拆 commit。

**验收**：`git diff` 里没有无关改动；测试在旧代码失败、新代码通过。

### Day 4：自审与补测

1. 过 `self-review.md` 的九项；
2. 补边界测试（空输入、非法输入、不同 target/feature）；
3. 用 `-verify-each` 跑一遍；
4. 检查 commit message 是否说清 why。

**验收**：自审表没有「有风险但无证据」的项。

### Day 5：预审与回应

1. 写 `pre-review.md`；
2. 隔 24 小时再读一遍自己的 diff（降低作者偏见）；
3. 写 `response.md`；
4. 如果 Track A，按真实 PR 模板提交（不要直接 push 到 upstream）。

**验收**：预审报告能被另一个学习者读懂，并指出「最可能被卡的点」。

## 评分标准（100 分）

| 维度 | 分值 | 满分标准 |
| --- | --- | --- |
| 问题与复现 | 15 | 最小、稳定、命令可复制 |
| 根因定位 | 20 | 指到具体源码位置，解释因果，不是「大概是这里」 |
| 修复质量 | 20 | 最小、符合现有机制、无无关改动 |
| 测试 | 20 | 旧代码失败、新代码通过、targeted、边界覆盖 |
| 自审 | 10 | 九项检查完整，风险有证据 |
| 预审报告 | 10 | 6 条以上、分级正确、落到行、最后一行有 prioritisation |
| 回应与拆分 | 5 | ACK/反驳/拆分清晰，有证据 |
| 可维护性 | 0（加分） | 注释解释 why、commit message 清楚、无 magic number |

低于 70 分，说明证据链有断点：回到对应章节补，不要靠「再多写点代码」掩盖。

## 核心概念覆盖表（至少 70%）

| 章节 | 在 Final 里怎么体现 |
| --- | --- |
| Ch 0–3 源码地图/证据 | 复现命令、`file:line`、`-print-after` |
| Ch 4–9 IR 不变量 | `self-review.md` 的合法性/支配/use-list 项 |
| Ch 10–12 ADT/TableGen/CMake | 修复是否复用现有 helper；是否引入新依赖 |
| Ch 13–17 Pass/Analysis | 根因是否在 pass/analysis；invalidation 是否正确 |
| Ch 18–22 中端变换 | 若 Track A 选中端 bug，fold/CFG/循环判断 |
| Ch 23–26 CodeGen/MC | 若选后端 bug，MIR/RA/MC 证据 |
| Ch 27–28 流程/读 PR | commit 拆分、baseline 顺序、PR 描述 |
| Ch 29–30 正确性 | poison/UB/dominance 检查 |
| Ch 31 测试 | 旧代码失败证明、CHECK 强度 |
| Ch 32 写作 | pre-review/response 的形态与级别 |

## 复现的最小化方法

「能复现」不够，reviewer 要的是**最小复现**。最小化不是把文件删到能跑，而是保留触发 bug 的必要条件，去掉一切无关结构。常用手段按成本排序：

| 手段 | 适用 | 命令/做法 | 注意 |
| --- | --- | --- | --- |
| 手工切片 | IR 只有几十行 | 删函数/参数/指令，每次重跑 | 保留 `target triple`、必要 attributes |
| `llvm-reduce` | 大 IR/大模块 | `llvm-reduce --test=./test.sh reduced.ll` | 先写一个「bug 仍存在则返回 0」的脚本 |
| `opt -passes=` 单点 | 中端 pass | 只跑可疑 pass，不用 `default<O2>` | 确认单 pass 也能触发 |
| `-O0` vs `-O2` | 优化相关 | 对比两档输出 | 差异不一定是 bug，可能是合法优化 |
| target/feature 矩阵 | 后端/target | `-mtriple`、`-mcpu`、`-mattr` 逐项变 | 记录哪个组合触发 |
| `bugpoint` | 崩溃/误编译 | `bugpoint -O2 repro.ll` | 老工具，输出可能不稳定 |

一个合格的最小复现应该满足：

- 单文件、单命令（或两条：生成 + 检查）；
- 不依赖私有头文件、内部数据、特定机器；
- 输出稳定（不依赖时间、地址、hash 顺序）；
- 能明确写出「期望 vs 实际」。

如果 bug 只在 `-O2` 出现，先用 `opt -passes='default<O2>' -print-after=...` 找出第一个产生差异的 pass，再把该 pass 单独跑。**不要把 `default<O2>` 当成黑盒复现**；reviewer 无法据此定位。

### 最小复现的模板

```llvm
; RUN: opt -passes=<suspect> -S %s | FileCheck %s
; 期望：...
; 实际：...
target triple = "x86_64-unknown-linux-gnu"

define i32 @repro(i32 %x) {
  ...
}
```

把「期望/实际」写在文件头，比放在 PR 描述里更不容易丢。如果 bug 是崩溃，附上 assert 文本和调用栈的前 5 帧；如果是 miscompile，附上 `opt` 输出和期望的语义解释。

## 修复的最小化：四种决策

reviewer 对「最小修复」的判断，通常按这个优先级：

```
1. 删除（delete）        —— 能不能删掉错误的代码/多余的机制？
2. 复用（reuse）         —— 有没有现成 helper/analysis/pattern？
3. 泛化（generalize）    —— 能不能让现有机制覆盖这个 case？
4. 新增（add）           —— 最后才考虑新机制/新 API
```

对应到 diff：

| 决策 | 典型 diff | review 会问 |
| --- | --- | --- |
| 删除 | 删掉一个错误特判/gate | 删了之后其他 case 还过吗？ |
| 复用 | 调用现有 `getFooOrNull` | 现有 API 的语义完全匹配吗？ |
| 泛化 | 放宽 predicate/参数化 helper | 会不会让不合法输入也进来？ |
| 新增 | 新 helper/pseudo/flag | 为什么现有机制不够？谁来维护？ |

如果你的第一版修复是「新增」，先停一下：能不能用一个更小的删除/复用达到同样效果？LLVM reviewer 的 P3 立场（最小表面积）就是从这类样本里归纳出来的。新增抽象要付租金：它需要有第二个使用者、或者明确的边界收益。

## Commit message 模板与真实对照

一个可用的模板：

```text
[Component] Imperative summary under ~70 chars

What was wrong, in one or two sentences. Include the user-visible
symptom if there is one.

Why the old code was wrong: the specific invariant, flag, or analysis
assumption that was violated.

How this patch fixes it, and why this is the minimal fix. Mention
alternatives only if they were seriously considered.

Fixes: https://github.com/llvm/llvm-project/issues/NNNNN
```

对照真实 commit `9ac5159b3c46`：

```text
[SCEV] Account for use flags in impliesPoison. (#244268)

Use flags only hold where the expression is used, so an exit count with
use flags may be poison when expanded in the preheader, even if the
earlier exit is taken first. Conservatively return false from
impliesPoison for non-canonical uses, keeping them in the umin_seq.

PR: https://github.com/llvm/llvm-project/pull/243812
```

它没有「优化了代码」「修复了问题」这类空话；第一段说 why，第二段说 fix 的方向和代价。你的 Final commit message 至少要做到同样具体。

## 模拟 review 的三轮脚本

真实 review 通常不是一轮结束。给自己设计三轮，每轮只关注一个层面：

### 第 1 轮：设计与必要性（F2/F5/P1–P3）

问自己：

- 这个修复能不能用现有机制完成？
- 有没有更小的删除/复用？
- 是否跨了职责边界（generic vs target、legalizer vs selector）？
- 需要拆成几个 commit/PR？

示例评论：

```text
[F2 / P3] llvm/lib/.../Foo.cpp:88  blocker
为什么要新增 `isFooSafe`？现有 `isSafeToSpeculativelyExecute` 已经覆盖
这个 case；如果它不够，能否先扩展它并单独 review？
最小改法：删掉新 helper，调用点用现有 API；如果确实需要，说明它多保证什么。
```

### 第 2 轮：覆盖与边界（F3/P1）

问自己：

- 宽度/元素类型/feature/target 组合覆盖了吗？
- poison/undef/溢出/空输入/非法输入？
- 测试是否会在旧代码失败？
- analysis invalidation 是否覆盖了所有修改路径？

示例评论：

```text
[F3 / P1] llvm/test/.../basic.ll:45  blocker
新逻辑按 bitwidth 分支，但测试只覆盖 i32/i64。i8 会走哪条路径？
如果不能走新路径，请在代码里明确 fallback 并加注释；如果能，补 i8 的 RUN/CHECK。
```

### 第 3 轮：格式与流程（F1/F4/F7）

问自己：

- 有没有多余 include/forward declaration/调试输出？
- commit 是否 rebase 到最新 `upstream/main`？
- PR 描述是否与最终 diff 一致？
- 测试命名/位置是否符合目录惯例？

示例评论：

```text
[F4] llvm/lib/.../Foo.cpp:12  nit
`#include "llvm/ADT/SmallSet.h"` 没有使用，删掉即可。
```

三轮结束后，写一句总结：**最可能被卡在 X**。如果 X 是设计层问题，不要急着改测试；先回答设计问题。

## 回应 reviewer 的四种动作

| 动作 | 什么时候用 | 怎么写 | 反例 |
| --- | --- | --- | --- |
| 接受 | 对方说得对 | 「Fixed in <commit>. 现在 X 走 Y 路径，测试 Z 覆盖。」 | 只写「fixed」 |
| 反驳 | 你有证据 | 「我查了 LangRef 的 X 节，这里不会产生 poison，因为…；新增测试 Y 证明。」 | 「我觉得没问题」 |
| 拆分 | 对方要求拆 | 「拆成 3 个 commit：baseline test / NFC helper / behavior fix；顺序是…」 | 「太大了不想拆」 |
| 不修 | 超出范围 | 「这是另一个已有问题，已开 issue #N；本 PR 只修 X。」 | 沉默或无视 |

回应时尽量给**可验证的证据**：commit hash、测试名、命令输出、源码位置。LLVM 的 review 线程很长，reviewer 不会记得你上一轮说了什么；每条回应都要能独立读懂。

## 评分示例

### 85 分：证据链闭合

- 5 行修复，删掉一个错误 gate；
- baseline 测试在旧代码上 assert，修复后通过；
- `self-review.md` 九项都有结论，其中一项标「不适用」并说明原因；
- `pre-review.md` 有 2 条 blocker、3 条 nit、1 条测试缺口；
- `response.md` 对 5 条评论分别给出接受/反驳/拆分；
- commit message 说明了「为什么旧 gate 是错的」，并关联 issue。

扣分点：没有跑 `-verify-each`，只在最终输出上验证。

### 60 分：能跑，但证据不足

- 修复 100 行，顺手重构了两个 helper；
- 测试在旧代码上也通过（只检查函数存在）；
- 自审表只写了「已检查」，没有证据；
- 预审报告只有格式 nit，没有设计层问题；
- commit message 是「Fix bug」。

这种提交在真实 review 里通常会被要求拆 PR + 补 baseline 测试 + 说明根因。分数低不是因为代码烂，而是因为**无法证明它对**。

## 常见失败模式

1. **改动很大，但没有最小复现**：reviewer 无法判断修复是否针对根因。
2. **测试在旧代码上也通过**：说明没有锁住行为；回到 Ch 31 做回退实验。
3. **修复顺手重构**：review 负担翻倍，revert 时无法只回滚修复。
4. **只改 target，不改 generic**：如果 bug 是通用机制问题，target workaround 会被要求拆/重做。
5. **`PreservedAnalyses` 照抄**：没有验证 analysis 是否真的保留，assertions 构建会抓。
6. **预审报告只挑格式**：没有 blocker，说明你没读到设计层；回到 Ch 28 的六步读法。
7. **回应 reviewer 时只写「fixed」**：没有证据、没有 commit 链接、没有说明拆法；按 Ch 32 的槽位写。

## 提交前的最后检查

```bash
# 1. 工作区干净、分支基于 upstream/main
git status -sb
git log --oneline upstream/main..HEAD

# 2. 每个 commit 只做一件事
git log --stat upstream/main..HEAD

# 3. 格式化与测试
"$LLVM_HOME"/toolset/pre-pr-check.sh <task> --issue <N>   # 若脚本可用
llvm-lit -v <你的测试文件>

# 4. 复现命令从干净构建再跑一次
```

## 自我检核

- [ ] 我的最小复现能让另一个人独立跑出来
- [ ] 我能把根因指到具体 `file:line`，并解释因果
- [ ] 我的修复没有无关改动，且优先复用现有机制
- [ ] 我的测试在旧代码上失败、在新代码上通过
- [ ] 我的自审表九项都有结论，风险项有证据
- [ ] 我的预审报告至少 6 条，分级正确，最后一行指出最可能被卡的点
- [ ] 我能对 5 条模拟评论给出 ACK/反驳/拆分/不修的分类回应
- [ ] 我知道这个 patch 如果需要拆，应该按什么顺序拆

## 延伸阅读

- **[LLVM Developer Policy](https://llvm.org/docs/DeveloperPolicy.html)**
  - **读哪里**：`Making and Submitting a Patch`、`Test Cases`、`Commit messages`、`Incremental Development`
  - **为什么值得读**：Final 的交付标准以这份文件为准，不是课程自创
- **[LLVM Code Review Policy](https://llvm.org/docs/CodeReview.html)**
  - **读哪里**：`Splitting Requests`、`LGTM`、`Acknowledge All Reviewer Feedback`
  - **和 Final 的关联**：`pre-review.md` 与 `response.md` 的格式依据
- **[LLVM Testing Guide](https://llvm.org/docs/TestingGuide.html)**
  - **读哪里**：lit/FileCheck、`REQUIRES`、测试组织
  - **为什么值得读**：测试交付物的最低标准
- **[llvm-project issue tracker](https://github.com/llvm/llvm-project/issues)**
  - **读哪里**：按 label `bug`、`good first issue`、`miscompile` 筛选
  - **和 Final 的关联**：Track A 的题库

→ 回到 [课程首页](./README.md)
