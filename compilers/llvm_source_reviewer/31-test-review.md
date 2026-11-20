# Ch 31 — 测试 review：这条 lit 测试真的会在改动前失败吗？

> **目标**：把「有测试就行」换成三个可验证的问题：这条测试跑得动吗？它在旧代码上会失败吗？它锁住的是改动真正影响的行为吗？学完你能在 review 里指出一条测试为什么什么都没锁住，并给出最小改法。

> **环境**：LLVM 24.0.0git，构建 `/home/ubuntu/llvm/builds/pr-lane`，对应 worktree `/home/ubuntu/llvm/trees/pr-lane`。本章的 FileCheck 与 lit 输出都来自这两个路径的真实运行。

## 为什么需要这个？

LLVM 的测试不是附件，是**唯一可执行的正确性证据**。`DeveloperPolicy.md` 明确要求 bug fix 和 new feature 都带测试，并且测试要 targeted、能用 FileCheck 锁住行为。

但「有测试」和「测试有效」是两件事。一个测试可以通过，却完全没有区分能力：

- 它在改动前也能通过（没有锁住新行为）。
- 它只检查 `ret` 存在，不检查返回值怎么算出来。
- 它用了 `CHECK-NOT`，但位置放错，实际上什么都没排除。
- 它是自动生成的，把当前（可能有 bug 的）行为全部 bless 了。

Reviewer 的价值在于：**在合入前证明这条测试会失败**。如果做不到，测试就只是装饰。

## 先建立直觉

一条有效的回归测试要同时满足三层：

```
第 1 层：跑得动
  RUN 行正确、工具存在、输入合法、lit 能找到它

第 2 层：会失败
  把实现回退到修复前，测试必须 fail

第 3 层：锁对东西
  fail 的原因必须是修复真正改变的行为，而不是无关的格式/顺序
```

多数 review 只检查第 1 层（CI 绿了）。第 2 层需要主动做「回退实验」。第 3 层需要读 CHECK 行和实现的对应关系。

## 31.1 LLVM 的测试类型

| 类型 | 位置 | 适用 | 例子 |
| --- | --- | --- | --- |
| lit + FileCheck | `llvm/test/**` | IR 变换、后端输出、诊断 | `opt ... | FileCheck %s` |
| MIR 测试 | `llvm/test/CodeGen/**/*.mir` | 机器 pass、regalloc、调度 | `llc -run-pass=...` |
| 单元测试 | `llvm/unittests/**` | 库/ADT/Support 等无工具入口的代码 | gtest |
| Clang 诊断测试 | `clang/test/**` | 前端诊断 | `clang -verify` |
| 集成测试 | `llvm-test-suite` | 端到端、性能 | 不在 `llvm/test` |

`llvm/test` 下有 48,400 个 `.ll` 文件（本机 `find llvm/test -name '*.ll' | wc -l` 实测）。这个规模决定了 LLVM 的测试哲学：**每个测试尽量小、尽量 targeted**，而不是放一个大程序。`DeveloperPolicy.md` 明确说，大输入要先用 `llvm-reduce` 缩小，不允许把整个失败程序丢进 `llvm/test`。

## 31.2 FileCheck 的语义：先读懂，再判断强弱

一条典型测试：

```llvm
; RUN: opt < %s -passes=instcombine -S | FileCheck %s
define i32 @f(i32 %a, i32 %b) {
; CHECK-LABEL: define i32 @f
; CHECK: %z = add i32 %a, %b
; CHECK: ret i32 %z
  %x = add i32 %a, 0
  %y = mul i32 %x, 1
  %z = add i32 %y, %b
  ret i32 %z
}
```

指令含义：

| 指令 | 作用 | 常见误用 |
| --- | --- | --- |
| `CHECK` | 从当前位置向后找第一个匹配 | 匹配范围太宽，跨过多个函数 |
| `CHECK-NEXT` | 必须紧接下一行 | 对 IR 打印格式过度敏感，稍改格式就崩 |
| `CHECK-SAME` | 必须和上一条在同一行 | 用于匹配同一行的后半段 |
| `CHECK-LABEL` | 分割匹配区域，通常按函数名 | 漏写会导致跨函数误匹配 |
| `CHECK-DAG` | 顺序无关的匹配组 | 用于指令顺序不固定的场景 |
| `CHECK-NOT` | 在两条 CHECK 之间不允许出现 | 放在末尾只检查最后一段，常被误解 |

`CHECK-LABEL` 是最容易被忽略的。没有它，FileCheck 会从文件开头一直往后找，可能匹配到另一个函数里同名的指令。一个测试如果有多个函数，几乎都应该用 `CHECK-LABEL` 分区。

## 31.3 实验：弱 CHECK 与强 CHECK

拿 Ch 0 的 `min.ll`，写两个检查文件。

弱检查：

```text
CHECK: define i32 @f
CHECK: ret i32
```

强检查：

```text
CHECK-LABEL: define i32 @f
CHECK: %z = add i32 %a, %b
CHECK: ret i32 %z
```

先对**未优化**的 IR 跑：

```bash
$B/opt -passes=verify -S min.ll | $B/FileCheck weak.chk
$B/opt -passes=verify -S min.ll | $B/FileCheck strong.chk
```

实测：弱检查 `PASS`，强检查失败：

```text
strong.chk:2:8: error: CHECK: expected string not found in input
CHECK: %z = add i32 %a, %b
       ^
<stdin>:8:2: note: possible intended match here
 %z = add i32 %y, %b
```

再对**优化后**的 IR 跑：

```bash
$B/opt -passes=instcombine -S min.ll | $B/FileCheck weak.chk   # PASS
$B/opt -passes=instcombine -S min.ll | $B/FileCheck strong.chk # PASS
```

结论很清楚：**弱检查在改动前后都通过，它没有区分能力**。这不是说弱检查永远没用（有时候你只想确认函数还在），但如果这个 PR 声称「修复了常量折叠」，弱检查不能证明任何事。

这条实验是 Ch 31 的核心动作。Review 时，把测试的 CHECK 行拿出来，问自己：**如果把实现的修复回退，这一行会失败吗？** 如果不会，测试就没有锁住修复。

## 31.4 回退实验：证明测试会在旧代码上失败

有三种常见做法，按可靠性排序：

### 做法 A：baseline commit（最可靠）

如果 PR 按「先加测试、再加修复」组织，baseline commit 的测试在当前代码上就应该**通过**（锁现状）或**失败**（TDD，锁期望）。修复 commit 之后，测试状态翻转。

真实例子 `25e91b6837aa`：标题 `[Attributor][NFC] Precommit tests ...`，只改两个测试文件，96 行新增。它的 CHECK 行锁的是**当前行为**：

```llvm
; The definition of a linkonce_odr function might be replaced by another one,
; e.g. one without the load or the assumption, so the attributes of its
; arguments and return value must not be derived from its body.
define linkonce_odr ptr @nonnull_return_from_load_non_exact(ptr %p) {
; CHECK-NEXT:    [[R:%.*]] = call nonnull ptr @nonnull_return_from_load_non_exact(ptr align 4 [[P]])
```

注意：这段测试写的是「当前确实会推出 nonnull」，而不是「应该推出」。它的作用是给后续修复提供一个可对照的 baseline。Review baseline 测试时，要问清楚它锁的是现状还是期望；两者都可以，但必须说明。

### 做法 B：只回退实现 hunk

如果测试和修复在同一个 commit：

```bash
git show <commit> -- llvm/lib/... > impl.patch
git apply -R impl.patch          # 只回退实现
llvm-lit -v <test-file>          # 期望 fail
git apply impl.patch             # 恢复
```

注意只回退实现，不要回退测试。如果回退后测试仍然 pass，说明测试没有覆盖修复。

### 做法 C：`git stash` / checkout 父提交

```bash
git checkout <commit>^ -- llvm/lib/Analysis/ScalarEvolution.cpp
llvm-lit -v llvm/test/Analysis/ScalarEvolution/exit-value-nowrap-flags.ll
git checkout <commit> -- llvm/lib/Analysis/ScalarEvolution.cpp
```

这种做法适合确认「整个测试文件在旧代码上是否失败」，但会把工作区弄脏，做完要恢复。

## 31.5 lit 的路径陷阱

本机实测：从 `pr-lane` 构建跑 hub 源码里的测试，会失败：

```bash
$B/llvm-lit -v /home/ubuntu/llvm/llvm-project/llvm/test/Transforms/InstCombine/add.ll
```

真实报错：

```text
fatal: unable to parse config file '/home/ubuntu/llvm/llvm-project/llvm/test/lit.cfg.py'
AttributeError: 'TestingConfig' object has no attribute 'enable_profcheck'
```

原因是构建的 lit 配置来自对应 worktree，而 hub 源码树的 `lit.cfg.py` 版本不同。正确做法是用**与构建匹配的源码树**：

```bash
$B/llvm-lit -v /home/ubuntu/llvm/trees/pr-lane/llvm/test/Transforms/InstCombine/add.ll
```

实测：

```text
-- Testing: 1 tests, 1 workers --
PASS: LLVM :: Transforms/InstCombine/add.ll (1 of 1)

Testing Time: 0.15s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

这个坑对 reviewer 很实际：你本地跑绿了，可能跑的是另一棵树的测试。**构建、源码树、测试文件必须来自同一套 worktree。**

## 31.6 自动生成的 CHECK：方便，但会掩盖变化

很多 LLVM 测试开头有：

```text
; NOTE: Assertions have been autogenerated by utils/update_test_checks.py
; RUN: opt < %s -passes=instcombine -S | FileCheck %s
```

`update_test_checks.py` 会根据当前输出重新生成 CHECK 行。它的价值是减少手写、保持格式一致；风险是：

- **整文件重生成**会顺带改掉无关函数的 CHECK，把真正的语义 diff 淹没。
- 它会把**当前行为**（包括 bug）bless 成期望行为。
- `CHECK-NEXT` 过多会让测试对指令顺序/格式过度敏感，稍有无关改动就失败。

Review 规则：

1. 如果 diff 里出现大量 CHECK 行变化，要求作者说明「哪些是预期变化，哪些是机械重生成」。
2. 优先要求只重生成受影响函数的 CHECK，而不是整个文件。
3. 如果新行为是 bug fix，确认新 CHECK 锁的是**修复后的正确行为**，不是「当前跑出来什么就写什么」。

## 31.7 真实案例：测试比例与缺口

`9ac5159b3c46` 的比例是 11 行实现 + 131 行测试。两个测试文件分别覆盖普通 exit 和 predicated exit。这种比例通常说明作者认真处理了边界，而不是「补一个 happy path 交差」。

反过来，`81e0dbceab0d` 是 2 行删除、**没有测试**。它修的是一个 assertions-only 的崩溃：

```text
GenericCycleInfo was not invalidated, so asserted when profcheck is
enabled that the block number epoche is outdated.
```

Review 这类「无测试的 assert 修复」时，至少问：

- 哪个现有测试在修复前会触发这个 assert？（`REQUIRES: asserts` 的测试？）
- 如果没有，能不能加一个最小 MIR/IR 测试，在 `-enable-profcheck` 下跑？
- 删除 `renumberBlocks` 是否会让其他依赖编号的代码退化？这个问题和测试无关，但同样重要。

「没有测试」不一定是 blocker（assertions-only 的修复有时难以稳定复现），但作者必须说明为什么，而不是默认放过。

## 31.8 测试 review checklist

| 检查项 | 该问什么 | 失败信号 |
| --- | --- | --- |
| RUN 行 | 工具、pass、输入都对吗？ | 跑了但没跑到目标 pass |
| 覆盖目标 | CHECK 行对应修复的哪一行？ | 只检查 `ret`/函数名 |
| 区分能力 | 回退实现后会 fail 吗？ | 改动前后都 pass |
| 分区 | 多函数测试有 `CHECK-LABEL` 吗？ | 跨函数误匹配 |
| 负向断言 | `CHECK-NOT` 放在正确区间了吗？ | 放在末尾，实际没排除 |
| 生成方式 | 是手工还是 `update_test_checks.py`？ | 整文件重生成，diff 淹没 |
| 输入规模 | 是否用 `llvm-reduce` 缩小？ | 整个程序塞进 `llvm/test` |
| 断言依赖 | 是否需要 `REQUIRES: asserts`？ | release 构建下静默跳过 |
| 平台/目标 | 是否需要 `REQUIRES:` 限定？ | 在别的 target 上必挂 |

## 踩雷集锦

1. **「CI 绿了」当成测试有效**：CI 只证明当前代码下通过；不证明旧代码下失败。回退实验才能证明区分能力。
2. **弱 CHECK 通过就放心**：`CHECK: ret i32` 在几乎所有 IR 上都通过；它没有锁住任何计算。
3. **多函数测试不用 `CHECK-LABEL`**：FileCheck 会跨函数匹配，测试可能在错误的函数上通过。
4. **`CHECK-NOT` 放在文件末尾**：它只检查最后一段，前面的违规内容不会被发现。
5. **整文件重生成 CHECK**：会把无关变化混进来，也可能把 bug bless 成期望。
6. **用 hub 源码跑 worktree 构建的 lit**：`enable_profcheck` 这类配置不匹配会让你得到假失败/假跳过。
7. **忘了 `REQUIRES: asserts`**：只在 assertions 构建下成立的测试，在 release 构建会 `UNSUPPORTED`，不是通过。

## 动手练习

1. 用本章的 `weak.chk` / `strong.chk` 对 `min.ll` 做实验，记录四个结果（原 IR/优化 IR × 弱/强），解释每个结果。
2. 找一个最近合入的 LLVM bug fix，按 31.4 的做法 B 只回退实现 hunk，跑对应 lit 测试，确认它 fail；记录命令和输出。
3. 找一个带 `update_test_checks.py` 注释的测试，手工删掉一条 `CHECK-NEXT`，看测试是否仍然通过；解释为什么。
4. 给 `81e0dbceab0d` 设计一个最小回归测试草案（IR + RUN + CHECK），要求它在启用 profcheck 的 assertions 构建下能触发原 assert。
5. 读 `9ac5159b3c46` 的两个测试文件，指出它们分别覆盖哪条路径；如果让你再加一个边界用例，你会加什么？

## 本章重点整理

- 有效测试要满足三层：跑得动、会失败、锁对东西。
- 弱 CHECK 在改动前后都通过，没有区分能力；强 CHECK 才能证明行为变化。
- 回退实验是证明「测试会在旧代码上失败」的唯一可靠方法。
- baseline/precommit 测试锁的是现状，不是修复；它让后续 diff 可读。
- `CHECK-LABEL` 分区、`CHECK-NOT` 位置、`REQUIRES: asserts` 是最常见的三个坑。
- 自动生成 CHECK 很方便，但会掩盖无关变化，也可能 bless bug。
- lit 的构建、源码树、测试文件必须匹配；hub 与 worktree 混用会得到假结果。

## 自我检核

- [ ] 能说出有效测试的三层标准，并解释每层怎么验证
- [ ] 能判断一条 CHECK 行是否会在旧代码上失败
- [ ] 知道 baseline/precommit 测试锁的是现状还是期望，以及怎么在 review 里问清楚
- [ ] 能解释 `CHECK`/`CHECK-NEXT`/`CHECK-SAME`/`CHECK-LABEL`/`CHECK-NOT` 的区别与误用
- [ ] 知道 `update_test_checks.py` 的风险，并能提出「只重生成受影响函数」的要求
- [ ] 能识别 `REQUIRES: asserts` 缺失导致的假通过
- [ ] 知道 lit 的 build/tree 必须匹配，能诊断 `enable_profcheck` 这类错误
- [ ] 能对一个「无测试的 assert 修复」提出具体的补测方案

## 延伸阅读

- **[LLVM Testing Guide](https://llvm.org/docs/TestingGuide.html)**
  - **读哪里**：`lit` 与 `FileCheck` 两节，以及 `REQUIRES`/`UNSUPPORTED` 的说明
  - **为什么值得读**：官方对测试组织、RUN 行、lit 配置的权威说明
- **[LLVM Developer Policy: Test Cases](https://llvm.org/docs/DeveloperPolicy.html#test-cases)**
  - **读哪里**：整节；重点看「targeted」「llvm-reduce」「unit test 适用场景」
  - **和本章的关联**：31.1/31.8 的标准来自这里
- **[FileCheck Documentation](https://llvm.org/docs/CommandGuide/FileCheck.html)**
  - **读哪里**：`CHECK-LABEL`、`CHECK-NOT`、`CHECK-DAG`、`--check-prefixes`
  - **为什么值得读**：FileCheck 的匹配规则比多数人以为的复杂；误用会产生「假通过」
- **[update_test_checks.py](https://github.com/llvm/llvm-project/blob/main/llvm/utils/update_test_checks.py)**
  - **读哪里**：脚本头部的 usage 与 `--function` 过滤选项
  - **和本章的关联**：31.6 的「只重生成受影响函数」可以用 `--function` 做到

→ [Ch 32 — Review 写作与真实 PR 复盘：从 comment 到 LGTM](./32-review-writing-and-case-studies.md)
