# Ch 25 — 给上游 lit 提交改动

> **目标**：把一个 lit 的 bug/需求，走完「最小复现 → 定位源码 → 改代码 → 加自测 → 改文档/Release Notes → 提 PR」的完整路径。学完你知道 lit 的 patch 会被 review 什么，以及哪些改动 upstream 不会收。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，hub commit `3fa53fc0e1b2`。本章的 lit 自测在一份 `/tmp` 副本上真实运行（避免把 `Output/` 写进源码树），工具来自 `/home/ubuntu/llvm/builds/pr-lane/bin`。

---

## 25.1 lit 的维护面有多大

先看数字，别把它当「一个小脚本」：

```bash
find llvm/utils/lit/lit -name '*.py' | wc -l
find llvm/utils/lit/tests -name '*.py' | wc -l
```

本机真实输出（hub `3fa53fc0e1b2`）：

```
27
152
```

- `llvm/utils/lit/lit` 下 27 个 `.py`，约 10,071 行。
- `llvm/utils/lit/tests` 下 152 个 `.py`，是 lit 的可执行规格。

lit 的 issue 入口是 LLVM GitHub 的 **`tools:llvm-lit`** 标签：

```
https://github.com/llvm/llvm-project/labels/tools%3Allvm-lit
```

`llvm/utils/lit/README.rst` 也明确写了这个入口。选 issue 的标准：

- 能用一个最小 suite 复现，不依赖某个大型 target。
- 行为在 `lit` 层，而不是某个项目的 `lit.cfg` 层。
- 修复不需要改 CLI 语义，或能用向后兼容的方式加选项。
- 你愿意补自测和文档。

## 25.2 第一步永远是「最小复现」

不要直接改源码。先建一个 `/tmp` 下的小 suite，把问题缩到最小：

```
repro/
├── lit.cfg.py
└── tests/
    └── case.test
```

例如怀疑「`REQUIRES` 里的正则 feature 不生效」，就只留：

```text
# REQUIRES: {{^la[bx]$}}
# RUN: echo ok
```

然后：

```bash
python3 llvm/utils/lit/lit.py --debug tests
python3 llvm/utils/lit/lit.py --show-used-features tests
python3 llvm/utils/lit/lit.py -v tests
```

三个问题的答案决定了改动落点：

1. **测试被发现了吗？** 没有 → `discovery.py` / `TestingConfig.py` / `suffixes`。
2. **directive 解析对了吗？** 没有 → `TestRunner.py` 的 parser。
3. **命令执行/替换对了吗？** 没有 → `TestRunner.py` / `ShUtil.py` / `ShellEnvironment.py`。
4. **结果码/报告错了吗？** → `Test.py` / `display.py` / `reports.py`。
5. **CLI 选项没生效？** → `cl_arguments.py` / `LitConfig.py`。

> **纪律**：复现里不要出现「我们公司的大 suite」「某个 target 的 900 行 lit.cfg」。reviewer 不会跑你的私有环境；最小复现是 patch 的一部分。

## 25.3 开发环境与自测

### 自测怎么跑

官方 README 的命令是：

```bash
utils/lit/lit.py --path /path/to/your/llvm/build/bin utils/lit/tests
```

`--path` 指向包含 `FileCheck`、`not`、`split-file` 的目录。lit 的自测本身也是 lit 测试，依赖这些工具。

> **不要直接在源码树跑**：`llvm/utils/lit/tests/lit.cfg` 把 `test_exec_root` 设成 `test_source_root`，会往源码树写 `Output/`。我通常复制一份到 `/tmp`：

```bash
cp -a /home/ubuntu/llvm/llvm-project/llvm/utils/lit /tmp/lit-selftest-src
cd /tmp/lit-selftest-src
python3 lit.py --path /home/ubuntu/llvm/builds/pr-lane/bin -j 8 tests
```

本机真实输出（尾部）：

```
PASS: lit :: verbosity.py (94 of 95)
PASS: lit :: windows-pools.py (95 of 95)

1 warning(s) in tests

Testing Time: 7.01s

Total Discovered Tests: 95
  Unsupported:  9 (9.47%)
  Passed     : 86 (90.53%)
```

有一个 warning 是环境导致的，真实内容：

```
warning: Setting a timeout per test not supported. Requires the Python psutil
module but it could not be found. ... Some tests will be skipped and the
--timeout command line argument will not work.
```

这正好说明：**lit 的某些能力依赖可选 Python 包**。你改 timeout 相关代码时，要同时考虑「有 psutil」和「没 psutil」两条路径。

在 LLVM 构建里，等价的 target 是 `check-lit`：

```bash
ninja -C build check-lit
```

`llvm/utils/lit/CMakeLists.txt` 里 `add_lit_testsuite(check-lit ...)` 依赖 `FileCheck`、`not`、`split-file`，并先跑 `prepare-check-lit` 把测试复制到构建目录。

### 自测的测试格式

lit 自测文件本身就是 `.py`，里面用 `RUN:` 行调用 `%{lit}` 跑 `Inputs/` 下的小 suite，再用 FileCheck 检查输出。`llvm/utils/lit/tests/lit.cfg` 里定义了：

```python
config.test_format = lit.formats.ShTest()
config.suffixes = [".py"]
config.substitutions.append(("%{inputs}", "Inputs"))
config.substitutions.append(("%{lit}", "%{lit-no-order-opt} --order=lexical"))
config.substitutions.append(("%{lit-no-order-opt}",
    "{env} %{{python}} {lit} -j1 -D use_normalized_slashes={...}".format(...)))
```

注意 `-j1` 和 `--order=lexical`：自测经常要逐行检查 lit 的输出，顺序必须稳定。你的测试如果检查多测试摘要，也要固定顺序。

一个典型的 lit 自测长这样（`llvm/utils/lit/tests/usage.py` 的骨架）：

```python
# RUN: %{lit} --help | FileCheck %s
# CHECK: usage: lit
# CHECK: --version
```

它不直接 import lit，而是把 lit 当黑盒跑。好处是 CLI/输出格式的回归能被抓住；坏处是慢、容易受环境影响。**改内部函数**时，可以加一个更直接的单元测试（`llvm/utils/lit/tests/unit/` 下已有先例）。

## 25.4 改动落点地图

按改动类型找文件：

| 你想改 | 主要文件 | 必须配套 |
|---|---|---|
| 加 CLI 选项 | `cl_arguments.py`、`LitConfig.py`、`main.py` | `lit.md` 的 OPTIONS、自测、Release Notes |
| 加 directive | `TestRunner.py`（parser、`_parseKeywords`、`parseIntegratedTestScript`） | `TestingGuide.md`、自测、Release Notes |
| 改替换 | `TestRunner.getDefaultSubstitutions()`、`applySubstitutions()` | `TestingGuide.md` 的 Substitutions、自测 |
| 改 shell 语法 | `ShUtil.py`、`ShCommands.py`、`InprocBuiltins.py`、`ShellEnvironment.py` | 自测（`shtest-shell.py` 等）、Release Notes |
| 加 test format | `lit/formats/*.py`、`lit/formats/__init__.py` | `lit.md` 的 LIT INFRASTRUCTURE、自测 |
| 改报告 | `reports.py`、`cl_arguments.py` | 自测、报告 schema 文档 |
| 改发现/配置 | `discovery.py`、`TestingConfig.py` | 自测、`lit.md` 的 TEST SUITES |
| 改并行/超时 | `run.py`、`worker.py`、`TestRunner.py` | 自测、Windows/无 psutil 路径 |

### 例：`%T` 是怎么被移除的

这是一个完整的上游改动样本，commit `7ff6973f1ae9`（2025-09-26，Aiden Grossman）：

- `llvm/utils/lit/lit/TestRunner.py`：移除 `%T` 的替换；先给一个明确的错误信息，后续 commit `fb2c5b1b2bd6`（2026-01-13）再把错误信息也移除。
- `llvm/utils/lit/tests/shtest-shell.py`：加/改测试，确认 `%T` 的行为。
- `llvm/docs/CommandGuide/lit.rst`：删掉文档。
- `llvm/docs/ReleaseNotes.md`：在 `Changes to the LLVM tools` 下加一条 `Support for %T in lit has been removed.`
- commit message：`[lit] Remove support for %T`，正文解释「为什么、迁移路径、何时移除错误信息」，并带 `Reviewers`/`Reviewed By`/`Pull Request`。

这个样本告诉你：**一个用户可见的 lit 改动，通常至少碰 4 个地方**：实现、测试、文档、Release Notes。

## 25.5 写一个能被接受的 patch

### 1) 先加测试，确认它会红

在 `llvm/utils/lit/tests` 下加一个 `.py` 测试，或在现有测试里加 `RUN:` 段。先跑一次，确认在未修复时失败：

```bash
cd /tmp/lit-selftest-src
python3 lit.py --path /home/ubuntu/llvm/builds/pr-lane/bin -v tests/your-new-test.py
```

### 2) 最小实现

不要顺手重构。lit 的代码历史长、平台多，reviewer 最怕「一个 bugfix 带 500 行 reformat」。改一个函数就只改一个函数。

### 3) 错误信息要可行动

lit 的错误信息是用户界面。好的错误信息：

```text
error: Test has no 'RUN:' line
error: Test has more than one ALLOW_RETRIES lines
error: Error in REQUIRES list:
couldn't parse text: ...
```

好的错误信息要包含：**哪个文件/行、期望什么、实际什么、怎么修**。不要只写 `invalid test`。

### 4) 兼容性

- **CLI**：不要删/改现有选项的含义；新选项要有默认值，旧行为不变。
- **Python**：`llvm/docs/TestingGuide.md` 说 Python 3.8+；核心文件用了 `from __future__ import annotations`。不要引入只有 3.12 才有的语法，除非项目明确升级。
- **平台**：Windows 路径、`os.pathsep`、`\.exe`、大小写、无 `psutil`、无 `fork`。CI 会跑 Windows/macOS。
- **输出格式**：`PASS: Suite :: path (1 of N)` 这种格式被 buildbot/CI 解析，别随便改。
- **性能**：lit 要跑几万个测试；不要在热路径加 O(N²) 或每测试一个子进程。

### 5) 文档与 Release Notes

- CLI 选项 → `llvm/docs/CommandGuide/lit.md` 的 OPTIONS。
- directive/替换/测试写法 → `llvm/docs/TestingGuide.md`。
- 用户可见的行为变化 → `llvm/docs/ReleaseNotes.md` 的 `Changes to the LLVM tools`。
- 新增 format → `lit.md` 的 LIT INFRASTRUCTURE + 示例。

### 6) 自测要覆盖「失败路径」

不只测「新功能能用」，还要测：

- 参数缺失/非法值。
- 文件不存在/格式错误。
- 旧行为仍然不变。
- Windows 路径（如果逻辑涉及路径）。
- 无 psutil / 无外部 shell 的降级路径。

## 25.6 PR 流程

```bash
# 1) 从 upstream/main 开分支
git switch -c fix/lit-requires-regex upstream/main

# 2) 改代码 + 测试 + 文档
git add llvm/utils/lit llvm/docs
git commit -m "[lit] Fix REQUIRES regex feature matching" -m "
The parser ... 

Fixes #12345.

Reviewers: ...
"

# 3) 本地自测
cp -a llvm/utils/lit /tmp/lit-selftest-src
(cd /tmp/lit-selftest-src && python3 lit.py --path <build>/bin -j 8 tests)

# 4) 提 PR
gh pr create --title "[lit] Fix REQUIRES regex feature matching" --body "..."
```

commit message 风格：

- 标题：`[lit] <祈使句>`，不超过 ~70 字符。
- 正文：为什么、改了什么、兼容性/迁移、测试命令。
- 结尾：`Fixes #...`、`Reviewers:`、`Pull Request:`（LLVM 的惯例，`git log` 里到处都是）。

PR 描述要包含：

1. **问题**：最小复现 + 实际/期望行为。
2. **方案**：改了哪几个文件、为什么选这个方案。
3. **测试**：跑了哪些命令、结果。
4. **兼容性**：有没有行为变化、需不需要 Release Notes。
5. **范围**：明确没做什么（避免 reviewer 以为你漏了）。

## 25.7 Review 会看什么

把 reviewer 的检查表提前跑一遍：

- [ ] 有没有最小复现？是不是 lit 层的问题？
- [ ] 改动是否最小？有没有顺手重构/格式化？
- [ ] 新行为有没有测试？失败路径有没有测试？
- [ ] 旧行为是否保持不变？CLI 是否向后兼容？
- [ ] Windows/macOS/无 psutil 路径是否考虑？
- [ ] 错误信息是否包含文件/行/期望/实际？
- [ ] 文档（lit.md / TestingGuide.md）是否更新？
- [ ] 用户可见变化是否有 Release Notes？
- [ ] 自测是否在干净副本上跑过？有没有把 `Output/` 提交进去？
- [ ] commit message 是否解释「为什么」，而不只是「改了什么」？

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「本地 `lit tests` 过了就能提」 | 必须跑 lit 自测（`tests/`），并考虑 Windows/macOS/无 psutil 路径。 |
| 「只改代码，文档后面补」 | 用户可见改动不带文档/Release Notes 会被 review 打回；`%T` 的 commit 就是四件套。 |
| 「顺手 reformat 整个文件」 | 会让 diff 不可 review；lit 的历史代码风格不统一，改你碰的行即可。 |
| 「错误信息随便写」 | lit 的错误信息是用户唯一的诊断线索；要包含文件/行/期望/实际。 |
| 「加个 CLI 选项很容易」 | 要同时改 `cl_arguments.py`、`LitConfig`、`main`、文档、自测；还要考虑 `LIT_OPTS` 与 response file。 |
| 「`--timeout` 在所有机器都能用」 | 需要 psutil 或进程树 kill 能力；没有时 lit 会 warning 并跳过相关测试。 |

## 重点整理

- lit 是 27 个 `.py`、约 10k 行的基础设施；issue 入口是 `tools:llvm-lit`。
- 先做最小复现，再按「发现 / 解析 / 执行 / 结果 / CLI」定位层。
- 自测用 `lit.py --path <build>/bin tests`；不要在源码树直接跑（会写 `Output/`）。
- 一个用户可见改动通常要动：实现、`llvm/utils/lit/tests`、`lit.md`/`TestingGuide.md`、`ReleaseNotes.md`。
- patch 要最小、可回滚、向后兼容；错误信息要可行动。
- PR 描述写清问题、方案、测试、兼容性、范围；commit 用 `[lit] ...` 风格。
- Review 关注：最小复现、测试覆盖、跨平台、CLI 兼容、文档、Release Notes。

## 自我检核

- [ ] 你怀疑 `REQUIRES` 的正则 feature 不生效，按本章的顺序，先跑哪三个命令？分别能排除什么？
- [ ] 为什么 lit 自测要在 `/tmp` 副本跑，而不是直接在 `llvm/utils/lit/tests` 跑？
- [ ] 一个「给 lit 加 `--foo` 选项」的 patch，最少要改哪些文件？漏了哪个最容易被 review 指出？
- [ ] `%T` 的移除 commit 改了哪四类文件？这个模式能套到你自己的改动上吗？
- [ ] 你的改动在 Linux 过了，但 Windows CI 挂了；最可能的三类原因是什么？
- [ ] 为什么 lit 的错误信息比「测试失败」本身更重要？举一个本章的例子。

## 延伸阅读

- **[lit README — Contributing to lit](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/README.rst)**
  - 读什么：`Contributing to lit` 全部。
  - 能学到：官方自测命令、依赖 `not`/`FileCheck`、`check-llvm` 与 `llvm-lit` 的双重验证。
  - 前提：读过 Ch 24，知道怎么跑一个 suite。
- **[LLVM GitHub — tools:llvm-lit issues](https://github.com/llvm/llvm-project/labels/tools%3Allvm-lit)**
  - 读什么：open issues 里带 `good first issue` 或小范围 bug 的条目。
  - 能学到：真实问题的分布；哪些属于 lit 层，哪些属于某个项目的 `lit.cfg`。
  - 前提：能读 issue 里的复现步骤。
- **[Commit `7ff6973f1ae9` — [lit] Remove support for %T](https://github.com/llvm/llvm-project/commit/7ff6973f1ae9)**
  - 读什么：diff + commit message。
  - 能学到：一个完整的 lit 改动样本（实现/测试/文档/Release Notes/迁移策略）。
  - 前提：读过 Ch 6 的 `%T` 部分。
- **[LLVM Code Review Policy](https://llvm.org/docs/CodeReview.html)**
  - 读什么：`Review process` 与 `Patch reviews` 两节。
  - 能学到：LLVM 的 review 期望、多久 ping、如何回应意见；不是 lit 专属，但所有 lit patch 都适用。
  - 前提：有 GitHub 账号，读过任意一个 LLVM PR。

> 最后一章不再讲新知识，而是把整门课压成一个可交付项目：从零搭一个 suite、加一个自定义 format、生成报告、接 CI，并给 lit 上游交一个小 patch。

→ [Final Project：从零做一个可发布的 lit 测试框架](./final-project-lit-framework.md)
