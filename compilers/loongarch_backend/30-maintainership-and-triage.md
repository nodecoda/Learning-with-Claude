# Ch 30 — 长期维护：triage、选 issue 与 review 文化

> 本章目标：知道 LoongArch 的 issue 怎么分类、哪些能接、哪些别碰；知道 upstream 的标签与 review 规则；能制定一个半年的贡献节奏，而不是“修完一个 bug 就断线”。

---

## 30.1 标签与自动分诊

LLVM 的 PR 标签由 `.github/new-prs-labeler.yml` 按**改动路径**自动打。LoongArch 相关的那一段（`:1255-1267`）就是“什么算 LoongArch 工作”的官方定义：

```yaml
backend:loongarch:
  - changed-files:
    - any-glob-to-any-file:
      - llvm/include/llvm/IR/IntrinsicsLoongArch.td
      - llvm/test/MC/LoongArch/**
      - llvm/lib/Target/LoongArch/**
      - llvm/test/CodeGen/LoongArch/**
      - clang/lib/Basic/Targets/LoongArch*
      - clang/lib/Driver/ToolChains/Arch/LoongArch.*
      - clang/lib/CodeGen/Targets/LoongArch.cpp
      - clang/include/clang/Basic/BuiltinsLoongArch*
      - clang/include/clang/Sema/SemaLoongArch.h
      - clang/lib/Sema/SemaLoongArch.cpp
```

lld 侧走另一条规则（`:1150-1153`）：

```yaml
lld:elf:
  - changed-files:
    - any-glob-to-any-file:
      - lld/**/ELF/**
```

所以 **#239812 的 label 是 `lld:ELF` 而不是 `backend:loongarch`**：它改的是 `lld/ELF/Arch/LoongArch.cpp`。triage 时先看改动路径，不要只看标题里的 `[LoongArch]`。

GitHub 上实际可用的标签（`gh label list --search loongarch` 实测）：

```text
backend:loongarch    #1dd617
```

只有这一个 LoongArch 专属标签；其余靠 `lld:ELF`、`clang:driver`、`llvm:codegen` 这类组件标签。

---

## 30.2 当前 open issue 的实际分布

用 `gh issue list --search '[LoongArch] in:title state:open'` 在本树实测，前 10 条：

| # | 标题 | 归属 | 类型 |
|---|---|---|---|
| 233002 | load-load barrier missing for monotonic loads | 后端/原子 | correctness |
| 239067 | missing opt: `vfrintrm.s + vftintrz.w.s => vftintrm.w.s` | LSX pattern | missed opt |
| 222088 | assertion failure with loop vectorizer with BitInt | 后端/向量 legalization | crash |
| 239812 | TLS-LE relaxation uses a stale register | **lld** | miscompile |
| 237912 | `-fsanitize=function` code-model check compares raw spelling | driver | 诊断/选项 |
| 230915 | SIGBUS in `LegalizeTypes` on sext of shl (LSX only) | 后端/legalization | crash |
| 112693 | LLDB LoongArch support | lldb | 大型特性 |
| 224119 | inefficient `half`/`bfloat` identify on NaN-boxing targets | 后端/DAG | missed opt |
| 149616 | `X86SnippetRepetitorTest.Loop` fails | 测试基建 | 非 LoongArch 本体 |
| 236744 | vector `uitofp` double-rounded (LASX) | 后端/向量 | miscompile |

这张表本身就是 triage 教材：

- **`112693`（LLDB）不要碰**：那是另一个子项目的长期工程，不是“修一个 pass”。
- **`149616` 不是 LoongArch 后端问题**：标题带 LoongArch 但实际是 X86 snippet repetitor 的测试基建。
- **`239812` 是 lld 而不是 codegen**：即使现象是“生成的代码错了”。
- **`236744` 最容易误判为“浮点舍入模式”**：实际是 `VectorLegalizer::ExpandUINT_TO_FLOAT` 的双重舍入。

---

## 30.3 分诊决策树

拿到一个“LoongArch 结果不对”，按产物定位：

```text
生成的 IR 就不对？
  ├─ 是 → clang（Sema/CodeGen/ABI）。用 clang/test/CodeGen/LoongArch 的 %clang_cc1 复现
  └─ 否
      ├─ llc 汇编不对？ → 后端（isel / 机器 pass / 调度）
      │    ├─ 只在 -O2 → DAG combine 或机器 pass（用 -stop-after 二分）
      │    ├─ 只在 LA32 → i64 legalization / ABI（Ch 13）
      │    └─ 只在 +lsx/+lasx → 向量 lowering（Ch 17）
      ├─ .o 的重定位/编码不对？ → MC 层（Ch 06/07），用 llvm-mc + llvm-readobj
      └─ 只有最终 ELF 不对？ → lld（Ch 12/29），用 yaml2obj 精确构造输入
```

再加两条判据：

- **符号/重定位层面错** → 优先怀疑 lld 或 MC，不是指令选择。
- **只在链接后出现、`.o` 反汇编正确** → 一定是 lld relaxation/relocation，不是后端。

---

## 30.4 怎么选第一个 issue

按“可复现性 × 影响面 × 你的时间”排序：

| 优先级 | 特征 | 例子 |
|---|---|---|
| 最优先 | 有 reduced testcase + 明确期望输出 + correctness | 239812 |
| 次优先 | 有 testcase 的 crash（assertion） | 222088、230915 |
| 可做 | missed optimization，有 IR 对照 | 239067、224119 |
| 谨慎 | 需要新 ABI/新扩展支持 | 112693 |
| 别碰 | 标题含 LoongArch 但实际是别的组件 | 149616 |

判断“有没有 reduced testcase”的方法：issue 里是否有可直接喂给 `llc`/`llvm-mc`/`yaml2obj` 的输入。没有的话，先自己写一个最小复现——**这本身就是有价值的贡献**（很多 issue 卡在没人做最小化）。

---

## 30.5 维护者的日常动作

### 1. 跟上游同步

```bash
git fetch upstream
git rebase upstream/main
```

本课这棵树 behind 19，每次开 PR 前都要做。rebase 后重跑：

```bash
$T/bin/llvm-lit -q llvm/test/CodeGen/LoongArch
$T/bin/llvm-lit -q llvm/test/MC/LoongArch
$T/bin/llvm-lit -q lld/test/ELF --filter=loongarch
```

### 2. NFC 清理

最近的 LoongArch 提交里有不少纯清理，例如：

```text
378916e7f7c7 LoongArch: Remove clearKillFlags calls on virtual registers (#239804)
c9ebe2a5fd40 [LoongArch] Remove duplicate addRequired in LoongArchDeadRegisterDefinitions (#236325)
```

这类提交门槛低、风险小，是熟悉代码的好入口。规则（`DeveloperPolicy.md:508-518`）：**格式/空白改动要与功能改动分开**，commit message 明确写 `NFC`。

### 3. Release notes

`llvm/docs/ReleaseNotes.md` 里，影响用户可见行为的改动（新指令、ABI 变化、默认值变化）应加一条。reviewer 会主动要求（`DeveloperPolicy.md:264`）。

### 4. 看 buildbot

后端改动最容易在非 x86 主机上炸（比如 LA32 的 i64 legalization）。提交后至少确认 LoongArch 相关 builder 是绿的。

---

## 30.6 review 文化与权限

- **commit-after-approval**：拿到 commit access 后，仍需批准才能提交，除非是“obvious fix”（构建修复、回滚、注释）（`DeveloperPolicy.md:500-518`）。
- **小 PR 优先**：24 行的 #239812 比“重构 relax 状态机”容易得多。
- **不要催**：LLVM reviewer 都是志愿者；等一周再 ping 是常态。
- **回应技术点，不回应情绪**：reviewer 说“这个 +8 是 magic number”，正确回应是“它是序列几何，我可以在注释里写清楚”，不是“你不懂 LoongArch”。
- **review 别人**：`DeveloperPolicy.md:518` 明确鼓励 review 他人补丁，不要求但计入信誉。

---

## 30.7 LoongArch 的高频坑清单（维护者版）

| 坑 | 症状 | 先查 |
|---|---|---|
| 成对重定位 | `.o` 正确、链接后错 | `R_LARCH_RELAX` 顺序（Ch 12/29） |
| LA32 i64 | 只在 `loongarch32` 错 | legalization / ABI（Ch 13） |
| LSX/LASX 依赖 | `-mno-lsx` 后 builtin 报错 | feature 依赖链（Ch 24/25） |
| ABI varargs | 参数错位、`fp128` 间接传递 | `LoongArchABIInfo`（Ch 09） |
| 代码模型 | `la.pcrel` 越界、`R_LARCH_B26` 溢出 | Medium/Large（Ch 11） |
| 伪指令展开 | MIR 里 `Pseudo*` 残留 | 两个 expand-pseudo 的顺序（Ch 18） |
| 调度模型缺项 | `llvm-mca` 结果异常 | `Sched<>` 是否漏加（Ch 23） |

---

## 30.8 半年节奏建议

| 阶段 | 目标 | 产出 |
|---|---|---|
| 第 1 月 | 读完后端全部 pass，跑通本课所有练习 | 一份自己的 pass 地图 |
| 第 2 月 | 接 1 个有 testcase 的 correctness issue | 1 个 merged PR |
| 第 3 月 | 做 2–3 个 NFC/测试改进 | 熟悉 review 流程 |
| 第 4 月 | 接 1 个 missed optimization（需要 llvm-mca 证据） | 1 个性能补丁 |
| 第 5 月 | 参与 review 别人的 LoongArch 补丁 | 建立信誉 |
| 第 6 月 | 申请 commit access（附 PR 列表与 triage 记录） | 长期维护资格 |

`DeveloperPolicy.md:536` 明确说：申请额外权限时要附“你之前做过什么”的证据。所以**从第一天就保留 issue/PR 链接**。

---

## 30.9 反直觉点

1. **`backend:loongarch` 只覆盖 `llvm/` 和 `clang/` 的一部分路径**，lld 改动拿的是 `lld:ELF`。
2. **标题带 `[LoongArch]` 不等于 LoongArch 后端问题**（149616）。
3. **“生成代码错”可能归 lld**，只要 `.o` 是对的。
4. **最小的补丁不一定最容易过**：不变量说不清楚，再小也会被打回。
5. **NFC 提交也有价值**，是建立 reviewer 信任的低风险方式。
6. **不要接 LLDB 支持类 issue 当第一个**。
7. **申请权限要证据链**，不是“我很感兴趣”。

---

## 30.10 本章证据

| 结论 | 来源 |
|---|---|
| `backend:loongarch` 路径清单 | `.github/new-prs-labeler.yml:1255-1267` |
| lld 标签规则 | 同文件 `:1150-1153` |
| 唯一 LoongArch 标签 | `gh label list --search loongarch` 实测 |
| 10 条 open issue | `gh issue list --search '[LoongArch] in:title state:open'` 实测 |
| commit 权限规则 | `llvm/docs/DeveloperPolicy.md:500-518` |
| NFC 与格式分离 | 同文件 `:508-518` |
| release note 要求 | 同文件 `:238-264` |
| 申请权限要证据 | 同文件 `:536` |
| 近期 NFC 提交 | `git log` 的 `378916e7f7c7`、`c9ebe2a5fd40` |

---

## 30.11 练习

1. 从 30.2 的表里选一个 issue，写出“归属判断 + 复现命令 + 预期产物”三行，不要写代码。
2. 用 `gh issue view <N>` 读 236744，画出 `uitofp` 的双重舍入路径（提示：Ch 13 的 `ExpandUINT_TO_FLOAT`）。
3. 找一个 LoongArch 的 merged PR，按 29.6 的四段结构拆解它的 commit message。
4. 给你自己的一个改动写 `NFC` 版本与功能版本两个补丁，练习拆分。
5. 列出你未来 6 个月想做的 3 个 issue，并说明每个的“最小可验证产物”。

---

## 30.12 延伸阅读

1. **Maintainer 的职责与权限路径** — `llvm/docs/DeveloperPolicy.md`
   - 读什么：`### Maintainers`、`### Obtaining Commit Access`、`### Obtaining Other Access or Permissions`、`### Patch reversion policy` 四节。
   - 学到什么：30.6/30.8 的“commit-after-approval”“申请权限要证据链”在政策中的原文；补上 maintainer 的具体职责（review、确认 issue、参与 RFC、协助 release backport）以及退出/恢复机制。
   - 前提：读过 30.6、30.8。

2. **AI 时代的 triage 边界** — `llvm/docs/AIToolPolicy.md`
   - 读什么：`## Details`（human-in-the-loop、禁止用 AI 工具修 `good first issue`）、`## Extractive Contributions`、`## Handling Violations`。
   - 学到什么：为什么“让 agent 自动扫 issue 并提 PR”会变成 extractive contribution；triage 时如何判断一个 AI 生成补丁是否值得 review。30.4 讲“怎么选第一个 issue”，这里补上政策约束。
   - 前提：读过 30.4、30.6；知道 `good first issue` 标签的含义。

3. **把模糊报告缩成可 triage 的输入** — `llvm/docs/CommandGuide/llvm-reduce.md`
   - 读什么：`## DESCRIPTION`、`## GENERIC OPTIONS` 的 `--test`/`--test-arg`/`-x={ir|mir}`、`## EXAMPLE`。
   - 学到什么：30.4 说“没有 reduced testcase 就先自己写一个”；这份文档给出自动化路径——用 interestingness test 驱动 `llvm-reduce`，把大 IR/MIR 缩到最小复现。补上手工最小化之外的工具化方法。
   - 前提：有一个稳定失败命令；读过 27.7 或 28.8 的 lit/MIR 基础。

4. **后端组件边界地图** — `llvm/docs/CodeGenerator.md`
   - 读什么：`## Introduction`、`### Required components in the code generator`、`### The high-level design of the code generator`。
   - 学到什么：30.3 的决策树把问题分给 clang/后端/MC/lld；这份文档补上后端内部的对象边界（`TargetMachine`、`TargetLowering`、`TargetRegisterInfo`、`TargetInstrInfo`、`TargetFrameLowering`、`TargetSubtarget`），帮助判断问题更可能在 legalization、isel 还是 frame lowering。
   - 前提：读过 30.3；对 Ch 03 的 codegen pipeline 有印象。

## 30.13 结课

到这里，你应该能做到：

- 在 `llvm/lib/Target/LoongArch` 里定位任意一条指令的生成路径；
- 用 `llc`/`llvm-mc`/`llvm-readobj`/`ld.lld`/`qemu-loongarch64` 产出可对照的证据；
- 用 `-stop-after`/`-run-pass`/`-stats`/`-debug-only` 定位到具体 pass；
- 写一个能被 `yaml2obj` 精确控制的回归测试；
- 把修复整理成符合 LLVM 惯例的 commit + PR 描述。

剩下的不是知识，是**持续做**。下一步：打开 `llvm/test/CodeGen/LoongArch`，挑一个你读不懂的测试，把它拆成 MIR，跑一遍。这就是维护者的日常。
