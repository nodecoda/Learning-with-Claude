# Ch 30 — 维护者视角：triage、review 与 revert

> **目标**：把"维护者每天怎么判断一个补丁"变成你提交前的自检流程。学完你能：画出一个 issue 的 triage 漏斗；用 `clang -###`、`llc -debug-only`、`llvm-reduce`、`git bisect` 产出可复跑的 reproducer；用 10 个 review 问题反推自己的测试；读懂真实 revert 的时机与提交信息；判断什么时候必须写 release note、什么时候会碰 ABI。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`，完整历史 607,142 个提交）；构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。本章对源码树只做只读操作，所有"实测"都来自这棵树与这个构建；未实测项明确标注。

> **前置**：读完 [Ch 29 — 上游贡献流程](./29-contribution-workflow.md)。Ch 29 讲"一个补丁怎么走完流程"，本章讲"维护者在这条流程的每个关口看什么证据"。

---

## 30.1 为什么"维护者视角"能提高你的补丁命中率

维护者视角不是猜某位 reviewer 的个人喜好，而是把上游每天重复做的证据检查，提前到你自己提交之前。RISC-V 后端的提交节奏很快（**实测**）：

```bash
$ cd /home/ubuntu/llvm/trees/ct-instr-trunk
$ git log --oneline --since='6 months ago' -- llvm/lib/Target/RISCV | wc -l
817
$ git log --oneline --since='12 months ago' -- llvm/lib/Target/RISCV | wc -l
1545
```

12 个月 1,545 个提交，约每天 4.2 个；6 个月 817 个，约每天 4.5 个。这个量级下，reviewer 不会从零理解你的补丁，他只会对照一套固定问题快速扫：**能不能复现？测试在哪？有没有碰 ABI？要不要 release note？** 你提前答完，review 就从"补作业"变成"确认"。

revert 也不是罕见事故。近 12 个月有 20 个 `Revert` 提交触及 `llvm/lib/Target/RISCV`（**实测**，见 30.9 E2）。`DeveloperPolicy.md:393-394` 明确说 "it is normal and healthy to have patches reverted"，所以维护者不会把 revert 当成对人的否定，只会把它当成让 trunk 回到 green 的常规动作。理解这一点，你才不会在收到 revert 时第一反应是"我的补丁失败了"，而是去看"哪条证据我没提前给出"。

把下面三件事在提交前做完，能消掉大部分往返：

1. **复现证据**：一个别人能直接跑的最小输入 + 一条完整命令。
2. **测试形状**：positive、negative、`-mattr` 矩阵、ABI 边界，至少覆盖你声称改变的行为。
3. **用户可见性**：新扩展、新选项、诊断、ABI/`e_flags` 变化，是否要 release note 或下游通知。

---

## 30.2 先建立直觉：一个 issue 的 triage 漏斗

```text
┌─────────────┐
│  新 issue   │  证据: 版本、triple、-march/-mabi、最小输入、期望/实际
└──────┬──────┘
       ▼
┌─────────────┐  否
│ 是否可复现? │──────▶ 要 reproducer；标 needs-info / worksforme
└──────┬──────┘
       │ 是
       ▼
┌─────────────┐  是
│  是否重复?  │──────▶ 关 duplicate，指向原 issue；补缺失证据
└──────┬──────┘
       │ 否
       ▼
┌─────────────────────┐  证据: backend:RISC-V、CodeGen/MC/Clang、
│ 归类(组件/严重度)   │        crash/miscompile/wrong-code/quality、
└──────┬──────────────┘        confirmed、good first issue
       ▼
┌─────────────────────┐  证据: Maintainers.md:357-366 的 4 位
│   指派 reviewer     │        RISC-V maintainer；git log 作者分布
└──────┬──────────────┘
       ▼
┌─────────────┐  证据: 最小 diff、root cause、Fixes #N、
│    修复     │        是否只改一个不变量
└──────┬──────┘
       ▼
┌─────────────┐  证据: lit PASS、negative test、-mattr/-mabi 矩阵、
│  回归测试   │        -verify-machineinstrs、GlobalISel 对称性
└──────┬──────┘
       ▼
┌─────────────┐  证据: commit 是否关闭 issue、release note、backport
│    关闭     │
└─────────────┘
```

这张图的关键不是顺序，而是**每一步都要留下别人能复跑的产物**：最小 reproducer、issue/commit 链接、`backend:RISC-V` 标签、最小 diff、lit 结果、`Fixes #N`。维护者判断"这个 issue 能不能往下走"，看的不是你的解释有多长，而是这些产物是否齐全。

`BugLifeCycle.md:40-82` 把 triage 的目标写得很直白：让新 issue 进入 "good, actionable state"，并逐条问"行为真的错吗？能从报告复现吗？和已有 bug 相关吗？字段填对了吗？"——这就是维护者的检查表，也应该是你提交前的检查表。

---

## 30.3 triage 实操清单

### 30.3.1 复现：先把 driver 的账算清

用户说 `-march=rv64gcv`，不等于后端只看到 `+v`。`clang -###` 会打印真正传给 `-cc1` 的参数（**实测**）：

```bash
$ $T/bin/clang -### --target=riscv64 -march=rv64gcv -mabi=lp64d -O2 -c t.c 2>&1 | tail -1
... "-target-cpu" "generic-rv64" "-target-feature" "+i" ... "+v" ... "+zvl128b" ... "-b" "-e" "-h" "-q" ...
$ $T/bin/clang -### --target=riscv64 -march=rv64gcv -mabi=lp64d -O2 -c t.c 2>&1 \
    | grep -o -- '-target-feature' | wc -l
286
```

286 个 `-target-feature` 里既有 `+v`、`+zvl128b`，也有 `-b`、`-e`、`-h`、`-q` 这类负向开关。issue 里只写 `-march`，维护者无法判断你实际跑的是哪套 feature；把 `clang -###` 的 cc1 行（或至少完整 driver 命令）贴出来，复现成本立刻下降。

`llc` 侧用 `-debug-only` 看 pass 内部决策。下面这个例子来自 `riscv-opt-w-instrs`（**实测**，assertions build）：

```bash
$ $T/bin/llc -mtriple=riscv64 -mattr=+zbb -debug-only=riscv-opt-w-instrs < w.ll -o /dev/null
Replacing %1:gpr = SLLIW %0:gpr, 1
     with %1:gpr = SLLI %0:gpr, 1
```

输入就是一个 `mul i32 %a, 3` 的函数；`-debug-only` 把 `SLLIW → SLLI` 的替换过程打出来。

注意 `-debug-only` 只在 assertions build 有效；release build 会静默忽略，别把"我本地没输出"当成"pass 没跑"。

真正要贴进 issue 的是**最小输入**，不是整棵项目的构建日志。`llvm-reduce` 的用法有一个反直觉点：**测试脚本返回 0 表示"仍然 interesting"**，和 `git bisect` 的 0=good 正好相反（**实测**）：

```bash
$ cat interesting.sh
#!/bin/sh
/home/ubuntu/llvm/builds/ct-instr-trunk/bin/llc "$1" -mtriple=riscv64 -o /dev/null >/dev/null 2>&1
exit $((1 - $?))          # llc 失败 -> 返回 0 -> interesting
$ $T/bin/llvm-reduce --test=./interesting.sh crash.ll -o reduced.ll
...
Done reducing! Reduced testcase: reduced.ll
$ cat reduced.ll
define swiftcc void @f() {
  ret void
}
```

`HowToSubmitABug.md:146-168` 对后端 bug 的要求是：先用 `-emit-llvm -c -o foo.bc` 留下 `.bc`，再试 `llc foo.bc`、`llc foo.bc -relocation-model=pic`、`llc foo.bc -relocation-model=static`；如果 `llc` 能复现，就用 `llvm-reduce`，测试脚本里把 `opt` 换成 `llc`。

### 30.3.2 最小 reproducer 的 6 条标准

1. **单文件**：一个 `.ll`、`.mir` 或 `.c`，不依赖外部头文件、不依赖未提交的本地改动。
2. **单命令**：`llc` 能独立复现，不要只说"clang 编大项目时崩"。
3. **完整目标参数**：`-mtriple`、`-mattr`（逐个列出）、`-target-abi`、`-mcpu`、`-O0/-O2/-Os`。
4. **完整语义开关**：涉及机器 pass 时加 `-verify-machineinstrs`；涉及 GISel 时写 `-global-isel` 和 `-global-isel-abort`。
5. **期望 vs 实际**：是 crash、assert、wrong code，还是 code size 退化？wrong code 要附运行输入/输出和 qemu 命令。
6. **可缩减**：附上 `llvm-reduce` 的 interestingness 脚本；维护者能自己再缩一遍。

### 30.3.3 bisect：在本树怎么跑

`git bisect` 的第一条纪律是**干净树**。本树当前就有一个未跟踪文件（**实测**）：

```bash
$ git status --porcelain
?? llvm/test/CodeGen/LoongArch/bittest.s
```

在这种树上直接 `git bisect start` 会把你的工作区切来切去，未跟踪文件还可能挡住 checkout。正确做法是单独开一个 worktree：

```bash
$ git worktree add /tmp/riscv-bisect <good-commit>
$ cd /tmp/riscv-bisect
$ git bisect start <bad-commit> <good-commit>
$ git bisect run /tmp/ch30/bisect.sh
$ git bisect log
$ git bisect reset
```

`bisect.sh` 要把"崩溃"归一化成 bisect 能理解的退出码：

```bash
#!/bin/sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cmake --build "$T" --target llc -j16 >/dev/null || exit 125   # 125 = skip
"$T/bin/llc" /tmp/repro.ll -mtriple=riscv64 -mattr=+v -o /dev/null 2>/dev/null \
  && exit 0 || exit 1   # 0 = good；1 = bad，别让 134/139 直接冒出去
```

`git bisect run` 的约定是：`0` = good，`1-127` = bad，`125` = skip，其他退出码会中止 bisect。所以脚本必须把 segfault/abort 统一成 `1`。

每一步都重编 `llc` 很贵。真实维护者通常用 ccache、增量 `ninja llc`，或者先 `git rev-list --bisect` 算出中点，只对中点做一次构建（**实测**）：

```bash
$ git rev-list --bisect f37bf0ce65b1^..af03f5e2196e -- llvm/lib/Target/RISCV
8107c71511b38f6fecf2c91676e5ed7b55a29b47
$ git show -s --format='%h %ad %s' --date=short 8107c71511b3
8107c71511b3 2026-03-02 [RISCV] Put Large Code Model Constant Pools in .text (#151393)
```

这个区间有 300 个触及 RISC-V 后端的提交，中点把区间分成 150/150。先构建并测试 `8107c71511b3`：good 则坏提交在后半，bad 则在前半。这样一次只花一个构建，不用让 `git bisect` 自动切 8 次。

---

## 30.4 review 关注点：从真实提交反推的 10 个问题

从 `git log --grep='\[RISCV\]'` 里挑 5 个主样本（`216a14a209d9`、`27a2da4d2716`、`d4d37b824b91`、`dc21109c01ad`、`8d8e089226f0`），再补若干对照（`1b3d79d2b933`、`eb856510459d`、`776971a11fdc`、`885f35214738`、`4dfef0be79de`、`c7fa25f0b239`、`fbdefa771094`），可以归纳出 reviewer 最常问的 10 个问题：

| # | reviewer 会问 | 你应准备的证据 | 本树样例 |
|---|---|---|---|
| 1 | 这个 bug 能脱离 clang 复现吗？ | 最小 `.ll`/`.mir` + 一条 `llc` 命令 | `216a14`：3 条 `not llc` + 1 条正向 RUN |
| 2 | negative test 在哪？ | `not ... \| FileCheck`，覆盖非法/互斥组合 | `xqccmi-invalid.s`、`reserved-reg-errors-alias.ll` |
| 3 | 碰 ABI / `e_flags` / 调用约定了吗？ | LangRef/psABI、`llvm-readelf -h`、下游通知 | `27a2da` 改 LangRef + note；`c7fa25` 测 RVC bit |
| 4 | code size 真的降了吗？ | `-O2/-Os`、VL/LMUL 矩阵、指令/字节数 | `1b3d79`：186 行 RVV 测试 |
| 5 | 这真是 NFC 吗？ | 零行为变化论证；必要时只动结构 | `885f352` +9/−2；`4dfef0` enum class |
| 6 | release note 写了吗？ | `ReleaseNotes.md` RISC-V 小节一行 | `dc21109` +1 行；`27a2da` +1 行 |
| 7 | `-mattr` / `-march` 组合覆盖了吗？ | `+zba/-zba`、`+xthead*`、互斥扩展 | `8d8e089`：1 行源码、无新测试 |
| 8 | GlobalISel 与 SelectionDAG 对称吗？ | `.mir` legalizer + `.ll` 选择 + fallback | `d4d37b`：rv32/rv64 两套 |
| 9 | RVV 的 VL/SEW/LMUL/tail/mask 呢？ | 多 VL、VLEN、mask/undef/passthru | `eb8565`、`1b3d79` |
| 10 | 调度/tune 改动有性能证据吗？ | llvm-mca/基准；没有就别改默认 | `fbdefa` 重命名测试；`776971` 因回归被 revert |

三个形状值得单独看：

- **`216a14a209d9`**（+84/−20）：5 个源码文件只配 37 行测试，但测试里有 3 条 `not llc` + 1 条正向 `llc`，覆盖 `reserve-x10`、`reserve-x11`、RV32/RV64、`Zhinx/Zfinx/Zdinx` 的别名寄存器。reviewer 问 "negative test?" 时，这个形状就是答案。
- **`27a2da4d2716`**（+63/−70）：1 行 `RISCVCallingConv.td` 的改动，带 128 行测试重写、LangRef 一行、release note 一行。凡是"从 preserve 列表里拿掉一个寄存器"这种 ABI 邻近改动，文档和测试都不是可选项。
- **`8d8e089226f0`**（+1/−1）：只把 `hasShlAdd(3)` 换成 `hasStdExtZba()`，没有新测试。提交信息自己承认 "I think this code is only used by vector spill/reload. I don't know if any Andes or T-Head cores support vector..."——这是诚实的，但也是 review 风险最高的形状：reviewer 必须替你把 `-mattr` 组合的空白补上。

反过来，`d4d37b824b91` 是"对称性"的好样本：它同时更新 `legalize-cttz-rv32.mir`、`legalize-cttz-rv64.mir`，再加 `rv32xtheadbb.ll`、`rv64xtheadbb.ll`，既证明 legalizer 的行为，又证明最终选择到 `th.ff1`。`eb856510459d` 则是 RVV 的最小形状：1 行 `RISCVInstrInfoV.td` 改 `DestEEW`，14 行测试覆盖 `vmv.v.v` peephole。

---

## 30.5 真实 revert 分析

先看规模（**实测**）：

```bash
$ git log --oneline --grep='Revert' --grep='RISCV' --all-match | wc -l
275
$ git log --oneline --grep='Revert' -- llvm/lib/Target/RISCV | wc -l
238
$ git log --oneline --since='12 months ago' --grep='Revert' -- llvm/lib/Target/RISCV | wc -l
20
```

275 是"提交信息同时含 Revert 与 RISCV"的提交，238 是实际触及 RISC-V 后端目录的 revert，20 是近 12 个月触及该目录的 revert。275 里包含跨项目、BOLT、Clang 的样本，所以**先路径过滤，再读 diff**，和 Ch 29 的纪律一致。

### 30.5.1 案例 A：BitInt 功能先 revert、70 天后再 reland

```bash
$ git show --stat --format='%h %ad %s%n%b' --date=short f37bf0ce65b1
f37bf0ce65b1 2026-01-29 Revert "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#178311)

This reverts commit e3156c531da5aa4ec604605ed4e19638879d773c.

We need to resolve a crash on trunk and LLVM 22. Reverting makes it
easier to backport.

Fixes #176637.
 llvm/test/CodeGen/RISCV/fpclamptosat.ll            | 5392 ++++++-------
 6 files changed, 887 insertions(+), 7791 deletions(-)
```

原提交 `e3156c531da5` 是 2026-01-13，+7791/−887；revert 是 01-29，正好 +887/−7791，**相隔 16.32 天**。revert 的理由不是"功能不要了"，而是"trunk 和 LLVM 22 上有 crash，先回滚让 backport 变简单"，并且用 `Fixes #176637` 把根因 issue 留在提交信息里。

70.48 天后，`af03f5e2196e` 以 `Reland "[RISCV] Support RISCV BitInt larger than 128"` 重新落地，正文写明 "Reverted due to #176637 and be fixed after #178690"，并补了 `fp128.ll` 等测试。用 `git log -S` 能把这个符号的完整生命周期串起来（**实测**）：

```bash
$ git log -S 'getMaxBitIntWidth' --oneline --all -- clang/lib/Basic/Targets/RISCV.h
af03f5e2196e Reland "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#190188)
0985ac8f5966 Revert "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#178311)
f37bf0ce65b1 Revert "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#178311)
e3156c531da5 [RISCV] Support RISCV BitInt larger than 128 (#175515)
```

四个事件：新增、两次同标题 revert、reland。`-S` 不是"搜提交信息"，而是"搜某个字符串的出现次数变化"，所以它能抓到一个函数/选项在历史里被加回来又删掉的全过程。

### 30.5.2 案例 B：buildbot 坏了，2 小时内 revert

```bash
$ git show --stat --format='%h %ad %s%n%b' --date=iso 3060f65b84f2
3060f65b84f2 2026-05-27 17:09:16 +0800
Revert "[RISCV][CodeGen] Use vzip.vv for e64 interleave shuffles with Zvzip" (#199899)

Reverts llvm/llvm-project#199512

LLVM Buildbot has detected a build error for this PR.
 llvm/lib/Target/RISCV/RISCVISelLowering.cpp        | 13 +++-------
 .../rvv/fixed-vectors-shuffle-int-interleave.ll    | 25 +++++++++----
 .../rvv/vector-interleave-fixed.ll                 | 26 +++++++++----
 3 files changed, 41 insertions(+), 23 deletions(-)
```

原提交 `a4b1361f3313` 与 revert 是同一天，时间戳相差 **7,797 秒 = 2 小时 10 分**。理由只有一句"Buildbot 检测到 build error"——这就够了：build breakage 会阻塞所有人，属于 `DeveloperPolicy.md:407-408` 的 "If you break a buildbot in a way which can't be quickly fixed, please revert."

`git show 3060f65b84f2 -- llvm/lib/Target/RISCV/RISCVISelLowering.cpp` 的关键 hunk 里最醒目的两行是：`- // use the zip2a/vzip instruction at e64.` 变成 `+ // use the zip2a instruction at e64.`，同时删掉 `hasStdExtZvzip()` 分支——revert 把 e64 interleave 的 Zvzip 路径整个拿掉，只保留 XRivosVizip。

### 30.5.3 两个对照：不写原因的 revert，和回滚一串提交

`8ad22e3d2cd4` 回滚 Andes 的 unaligned access 建模，正文只有：

```text
This reverts commit 358cafe41326e1e543b04048d5a200cf6f16ac8a.
```

没有 why。它确实指名了原提交，满足最低可追溯性，但维护者无法从提交信息判断这是 correctness、performance 还是 build 问题。对比之下，`af5506c5c12d` 回滚 SP/FP 的两条提交，正文写 "This change is causing a miscompile in zstd, see the discussion under ...#193962"，并列出两个被回滚的 SHA；它改了 25 个文件、+1477/−1488，属于"回滚一串提交"的正确写法：**范围清楚、原因可复现、讨论链接可追**。

### 30.5.4 政策原文：不是 "revert first, ask later"，而是 "reverting first, investigating offline"

`DeveloperPolicy.md:383-389` 先说明 LLVM 比其他项目更频繁地使用 revert：

> As a community, we strongly value having the tip of tree in a good state ... we tend to make much heavier use of reverts to keep the tree healthy than some other open source projects.

真正接近 "revert first, ask later" 的原文在 `DeveloperPolicy.md:402-406`：

> Any time you learn of a serious problem with a change, you should revert it. We strongly encourage "revert to green" as opposed to "fixing forward". We encourage reverting first, investigating offline, and then reapplying the fixed patch - possibly after another round of review if warranted.

同节 `414-416` 还说：如果别的 contributor 要求你 revert，就先 revert，再 offline 讨论。`437-438` 要求 message **解释为什么**；`442-446` 要求尽量附公开 reproducer；`455-461` 给出时效判断：两小时前的提交可以直接 revert，两年前的提交不应该。压成一句话：**revert 是让 trunk 回到 green 的工具，不是对作者的评价；但 revert 必须带原因、带范围、尽量带 reproducer。**

---

## 30.6 release notes 与 ABI

`llvm/docs/ReleaseNotes.md:378` 是 RISC-V 小节，格式是"一个 `###` 标题 + 一组 bullet"，每条一句话说清用户能感知的变化（**实测**，当前树）：

```markdown
### Changes to the RISC-V Backend

* Added experimental MC support for the ACLIC v0.20 extensions: `Smidctrl`, ...
* Adds experimental assembler/CodeGen support for the `Zilx` ... extension.
* Removed veyron-v1 processor definition and tuning model.
* Added `-mcpu=tt-ascalon-xg` for the Tenstorrent Ascalon XG processor, ...
* Removed X5 from the preserve_most register list.
```

`DeveloperPolicy.md:240-245` 的判据是 "user-facing, or that users may wish to know about"，并且 "preferably as part of the commit landing the changes"——**不是 merge 之后再补**。对 RISC-V，下面这些必须写：

- 新增/删除/修改扩展，或改变扩展的实验状态（`Zicfiss`、`Zvzip`、`Xqccmi`）。
- 新增/删除 `-mcpu`、`-mtune`、`-march` 语法或 profile（`RVA23P1S64`）。
- 新增/删除/改变诊断行为（例如保留寄存器冲突的报错）。
- ABI、调用约定、`e_flags`、`.attribute arch` 的用户可见变化。
- 有大范围影响的 codegen 行为变化（例如默认 unroll、默认 tune）。

`27a2da4d2716` 是"ABI 邻近改动"的模板：它把 X5 从 `CSR_RT_MostRegs` 拿掉，同时改 `llvm/docs/LangRef.md` 的 preserve_most 描述、加一行 release note、重写 128 行测试。`c7fa25f0b239` 则是 `e_flags` 的模板：`RISCVELFStreamer.cpp` 在 `finish()` 里根据 `hasRVC()` 设置 `EF_RISCV_RVC`，测试 `llvm/test/MC/RISCV/elf-flags.s` 同时覆盖 `+c` 与 `+zca` 两条路径。凡是改到 `e_flags` 的补丁，reviewer 都会问：**`.attribute arch`、`-mattr`、ELF header 三处是否一致？下游 binutils/glibc/qemu 是否需要同步？**

如果 ABI 变化可能破坏现有二进制，除了 release note，还应走 psABI 讨论/ Discourse RFC，并在 PR 描述里明确"谁需要升级、旧对象文件会怎样"。只在 merge 后补一句 note，等于让下游自己发现。

---

## 30.7 CI 与机器人

先看真实文件名（**实测**，`ls .github/workflows` 的部分结果）：

```text
premerge.yaml          pr-code-format.yml     pr-code-lint.yml
new-issues.yml         new-prs.yml            merged-prs.yml
issue-release-workflow.yml                    docs.yml
check-ci.yml           version-check.yml      llvm-bugs.yml
```

- **pre-merge CI**：`premerge.yaml`（workflow 名 "CI Checks"）在 PR opened/synchronize/reopened 时触发；先用 `.ci/compute_projects.py` 按 changed files 算出要构建的项目，再在 Linux/Windows/macOS 的 self-hosted runner 上 build + test。`GitHub.md:316-335` 说明这些检查"不是严格 mandatory"，但**不能 force-merge 明确与你改动相关的 test failure**。
- **格式与 lint**：`pr-code-format.yml` 只取 `HEAD~1..HEAD` 的 changed files，调用 `code-format-helper.py`（C/C++ 用 `git-clang-format`），只检查你改动的行；`pr-code-lint.yml` 只在 clang-tidy 路径触发，不是通用 lint。所以"历史文件有格式债"不会挡住你，"你新加的行没格式化"会。
- **issue/PR 机器人**：`new-issues.yml` 按标题正则给新 issue 打 `clang`/`lld`/`mlir` 等标签；`new-prs.yml` 给第一次贡献者发欢迎评论，并按 `new-prs-labeler.yml:1133` 的路径 glob 打 `backend:RISC-V`。本树 `.github/` 下没有 `ISSUE_TEMPLATE/` 或 PR 模板文件，结构化主要靠这些 workflow + `BugLifeCycle.md`。
- **buildbot**：`.ci/buildbot/README.md` 描述的是 post-commit 的 ScriptedBuilder：Buildbot master（lab.llvm.org）轮询 main，把 commit 分发给各 worker，worker 在 `llvm.src` 上跑预定义脚本。它覆盖 premerge 之外的更多构建配置和 test-suite，所以 `DeveloperPolicy.md:285-309` 把"在所有稳定 buildbot 上编译并通过测试"写成 committer 的持续责任。

本机能复现的检查：

```bash
$T/bin/llvm-lit -sv llvm/test/MC/RISCV/elf-flags.s
$T/bin/clang-format --dry-run --Werror /tmp/ch30/RISCVRegisterInfo.cpp
git clang-format --diff <base> <head> -- llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp
```

最后一条是 CI 真正做的事：只检查 diff 里的新行。**实测**中，把 `216a14a209d9` 的 `RISCVRegisterInfo.cpp` 整文件导出后跑 `clang-format --dry-run --Werror`，有 39 行报错（13 处 violation）、退出码 1；但 `git clang-format --diff 216a14^ 216a14 -- <file>` 退出码 0，因为该提交新增的行是干净的。不要把这两种结果混为一谈。

---

## 30.8 文档与源码落点

| 主题 | 路径:行号 | 读什么 |
|---|---|---|
| RISC-V maintainer 名单 | `llvm/Maintainers.md:357-366` | 4 位 reviewer：Alex Bradbury、Craig Topper、Philip Reames、Sam Elliott |
| triage 问题清单 | `llvm/docs/BugLifeCycle.md:40-82` | 可复现、重复、字段、组件/严重度标签 |
| 关闭 issue | `llvm/docs/BugLifeCycle.md:100-123` | `Fixes #N`、duplicate、invalid、wontfix、worksforme |
| review 政策 | `llvm/docs/CodeReview.md:91-110` | 必须显式 approval；不要默认沉默通过 |
| 补丁质量 | `llvm/docs/DeveloperPolicy.md:267-312` | 编码规范、编译干净、测试、premerge、buildbot 责任 |
| revert 政策 | `llvm/docs/DeveloperPolicy.md:383-463` | "revert to green"、message 要解释原因、reproducer、时效 |
| release notes | `llvm/docs/DeveloperPolicy.md:238-265` | 用户可见变化、随提交落地、reviewer 可要求 |
| RISC-V release notes | `llvm/docs/ReleaseNotes.md:378-401` | 当前条目的写法与粒度 |
| commit message | `llvm/docs/DeveloperPolicy.md:315-362` | why 优先、`[RISCV]` 惯例、关联 commit hash、`Fixes` |
| 后端 reproducer | `llvm/docs/HowToSubmitABug.md:146-168` | `.bc`、`llc` 三种 relocation model、`llvm-reduce` |
| PR landing | `llvm/docs/GitHub.md:131-172`、`316-335` | squash、PR 正文=commit message、premerge CI 边界 |
| issue/PR 自动化 | `.github/workflows/new-issues.yml`、`new-prs.yml`、`new-prs-labeler.yml:1133` | 自动标签、`backend:RISC-V`、first-time greeter |
| buildbot | `.ci/buildbot/README.md` | post-commit worker 流程、如何本地复现 build |
| `e_flags` | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFStreamer.cpp:113-126`、`llvm/test/MC/RISCV/elf-flags.s` | RVC/RVE/TSO/float ABI 位与测试 |

---

## 30.9 可跑实验

以下实验都在本树/本构建上真实跑过；输出做了截断，但命令可直接复跑。

### E1 — RISC-V 提交节奏

```bash
$ git log --oneline --since='6 months ago' -- llvm/lib/Target/RISCV | wc -l
817
$ git log --oneline --since='12 months ago' -- llvm/lib/Target/RISCV | wc -l
1545
```

817/182 ≈ 4.5 个/天，1545/365 ≈ 4.2 个/天；这也是为什么检索要用 `--grep` + 路径过滤。

### E2 — revert 的真实计数与样例

```bash
$ git log --oneline --grep='Revert' --grep='RISCV' --all-match | wc -l
275
$ git log --oneline --grep='Revert' -- llvm/lib/Target/RISCV | wc -l
238
$ git log --oneline --since='12 months ago' --grep='Revert' -- llvm/lib/Target/RISCV | wc -l
20
$ git log --oneline --grep='Revert' --grep='RISCV' --all-match -2
8ad22e3d2cd4 Revert "[RISCV] Andes: model fast unaligned accesses (#220756)" (#221178)
776971a11fdc Revert "[RISCV] Add TuneNoDefaultUnroll to generic CPUs (#135318)" (#212400)
```

### E3 — 看一个 revert 的完整 stat

```bash
$ git show --stat --format='%h %ad %s%n%b' --date=short f37bf0ce65b1
f37bf0ce65b1 2026-01-29 Revert "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#178311)
...
6 files changed, 887 insertions(+), 7791 deletions(-)
```

数字正好是原提交 `e3156c531da5` 的镜像（+7791/−887），说明这是一个干净的整提交回滚，不是"手工删几行"。

### E4 — 用 `git log -S` 追一个被 revert 的符号

```bash
$ git log -S 'getMaxBitIntWidth' --oneline --all -- clang/lib/Basic/Targets/RISCV.h
af03f5e2196e Reland "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#190188)
0985ac8f5966 Revert "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#178311)
f37bf0ce65b1 Revert "[RISCV] Support RISCV BitInt larger than 128 (#175515)" (#178311)
e3156c531da5 [RISCV] Support RISCV BitInt larger than 128 (#175515)
```

`-S` 看的是字符串出现次数变化，不是提交信息；它能抓到"新增 → revert → reland"的完整链条。

### E5 — 对导出的 RISC-V 源码跑 clang-format

```bash
$ git show 216a14a209d9:llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp > /tmp/ch30/RISCVRegisterInfo.cpp
$ cp .clang-format /tmp/ch30/       # 让 clang-format 用仓库风格
$ $T/bin/clang-format --dry-run --Werror /tmp/ch30/RISCVRegisterInfo.cpp > /tmp/ch30/cf.out 2>&1
$ echo $?
1
$ wc -l < /tmp/ch30/cf.out
39
```

39 行报错（13 处 violation）是**历史文件的格式债**，不是这个提交新增行的问题。对比 CI 的做法：

```bash
$ git clang-format --diff 216a14a209d9^ 216a14a209d9 -- llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp
$ echo $?
0
```

所以本地自检要跑第二条；整文件 `--Werror` 只适合"我准备重排整个文件"的场景。

### E6 — 脚本化 bisect（合成仓库，真跑）

```bash
$ git bisect start HEAD $FIRST
$ git bisect run sh -c 'test ! -f bad'
...
fe92bea76822d74c33e8e984e0b5cd6a0be4cd82 is the first bad commit
    c67
```

100 个提交、第 67 个引入 `bad` 文件，`git bisect` 用 7 次测试定位到 `c67`。这里 `test ! -f bad` 返回 0 表示 good、非 0 表示 bad，正好和 `llvm-reduce` 相反。

### E7 — 真实区间用 `git rev-list --bisect` 选中点

```bash
$ git rev-list --bisect f37bf0ce65b1^..af03f5e2196e -- llvm/lib/Target/RISCV
8107c71511b38f6fecf2c91676e5ed7b55a29b47
$ git show -s --format='%h %ad %s' --date=short 8107c71511b3
8107c71511b3 2026-03-02 [RISCV] Put Large Code Model Constant Pools in .text (#151393)
```

300 个提交，中点 150/150。真实全量 bisect 要在每个中点重编 `llc`，本章没有跑（见"未实测"）；`rev-list --bisect` 给出的是"下一次该构建哪个提交"的确定性答案。

### E8 — `llvm-reduce` 的最小化

```bash
$ $T/bin/llvm-reduce --test=./interesting.sh crash.ll -o reduced.ll
...
Done reducing! Reduced testcase: reduced.ll
$ cat reduced.ll
define swiftcc void @f() {
  ret void
}
```

`interesting.sh` 就是 30.3.1 里那个把 `llc` 失败翻成 0 的脚本。

`$T/bin/llc` 对 `swiftcc` 报 "Unsupported calling convention"，llvm-reduce 把它缩到 3 行。注意脚本的 `exit $((1 - $?))`：**0 = interesting**。

### E9 — `clang -###` 与 `llc -debug-only`

```bash
$ $T/bin/clang -### --target=riscv64 -march=rv64gcv -mabi=lp64d -O2 -c t.c 2>&1 \
    | grep -o -- '-target-feature' | wc -l
286
$ $T/bin/llc -mtriple=riscv64 -mattr=+zbb -debug-only=riscv-opt-w-instrs < w.ll -o /dev/null
Replacing %1:gpr = SLLIW %0:gpr, 1
     with %1:gpr = SLLI %0:gpr, 1
```

`-###` 证明 driver 实际启用了 286 个 feature；`-debug-only` 证明 pass 内部做了什么。两者一起，才能把"为什么这个 `-march` 下会这样"说清。

### E10 — 单文件 lit 复现 CI 检查

```bash
$ $T/bin/llvm-lit -sv llvm/test/MC/RISCV/elf-flags.s
Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

**未实测**：真实 LLVM 全量 `git bisect run`（每步增量构建 `llc`，代价过大）；真实 Buildbot 触发（需要上游 lab.llvm.org 权限）；`premerge.yaml` 的完整平台矩阵（需要 self-hosted runner）。本章用 `git rev-list --bisect`、合成仓库和单文件 lit 代替，并在文中标明。

---

## 30.10 踩雷集锦

1. **把 revert 当失败**。`DeveloperPolicy.md:393-394` 说 revert 是 "normal and healthy"；BitInt 功能 revert 后 70 天 reland，是正常迭代，不是项目否决。
2. **revert 不带原因**。`8ad22e3d2cd4` 只写 "This reverts commit ..."，维护者无法判断是 correctness 还是 performance；`DeveloperPolicy.md:437-438` 要求解释 why。
3. **release note 只在 merge 后补**。`DeveloperPolicy.md:240-244` 要求 "preferably as part of the commit landing the changes"；`27a2da` 就是同提交带 note 的样本。
4. **`git bisect` 在脏树上跑**。本树的 `?? llvm/test/CodeGen/LoongArch/bittest.s` 就是现成反例；用 `git worktree add` 开干净树，或至少先 `git status --porcelain`。
5. **issue 里贴不完整 reproducer**。只给 `-march=rv64gcv` 不够，要给 `clang -###` 的 cc1 行、`-mattr` 全列表、`-target-abi` 和最小 `.ll`。
6. **review 只看 diff 不看测试**。`216a14` 的价值在 3 条 `not llc`；`8d8e089` 只有 1 行源码、没有新测试，reviewer 只能替你补组合矩阵。
7. **ABI 变化不通知下游**。改 `e_flags`、preserve 列表、调用约定，要同步 LangRef/psABI、release note、测试；`27a2da` 和 `c7fa25` 是正例。
8. **bot 失败被忽略**。`3060f65` 因 Buildbot build error 在 2 小时内 revert；`DeveloperPolicy.md:407-408` 明确要求 buildbot 坏了且不能快速修就 revert。

---

## 30.11 本章重点整理

- **维护者视角 = 预 triage + 预 review**：提交前先问"能复现吗、测试在哪、碰 ABI 吗、要 note 吗"。
- **reproducer 的三条命令**：`clang -###` 看真实 feature，`llc -debug-only` 看 pass 决策，`llvm-reduce` 缩到最小；`llvm-reduce` 的 0=interesting 与 bisect 相反。
- **bisect 的纪律**：干净树、独立 worktree、脚本把崩溃归一化成 exit 1；`0=good`、`1-127=bad`、`125=skip`；贵的时候用 `git rev-list --bisect` 先选中点。
- **revert 政策**：LLVM 偏好 "revert to green"；revert 要写 why、给范围、尽量给公开 reproducer；revert 不是失败。
- **release note 的判据是"用户能否感知"**：新扩展、新 CPU、新选项、诊断、ABI/`e_flags` 必须写；纯 NFC/测试不需要。

---

## 30.12 自我检核

- [ ] 我能画出 triage 漏斗，并说出每一步维护者要看的那一件证据。
- [ ] 我能用 `clang -###` 证明某个 `-march` 实际启用了哪些 `+/-` feature。
- [ ] 我能写一个最小 `.ll` + `llc` 命令，并用 `llvm-reduce`（0=interesting）缩减它。
- [ ] 我能脚本化 `git bisect run`，并解释 0/1-127/125 三种退出码。
- [ ] 我能从 `git log --grep='Revert'` 找样本，并判断一条 revert message 是否合格。
- [ ] 我能引用 `DeveloperPolicy.md` 的行号说明 "reverting first, investigating offline"。
- [ ] 我能判断一个改动是否必须写 release note，以及是否碰 ABI/`e_flags`。
- [ ] 我能区分 premerge CI 与 buildbot 的角色，并知道本地怎么跑单文件 lit。
- [ ] 我能用 `clang-format --dry-run --Werror` 和 `git clang-format --diff` 做格式自检，并解释两者差异。

---

## 30.13 延伸阅读

- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/DeveloperPolicy.md`（revert: 383-463；release notes: 238-265；quality: 267-312；commit message: 315-362）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/BugLifeCycle.md`（triage: 40-82；closing: 100-123）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/CodeReview.md`（workflow: 91-110；who reviews: 205-236）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/HowToSubmitABug.md`（backend: 146-168）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/ReleaseNotes.md`（RISC-V: 378-401）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/GitHub.md`（landing: 131-172；premerge CI: 316-335）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/.ci/buildbot/README.md`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/Maintainers.md`（RISC-V: 357-366）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/27-lit-testing-and-update-scripts.md`
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/29-contribution-workflow.md`

→ 下一章：[Ch 31 — 高价值贡献地图](./31-contribution-map.md)
