# Ch 1 — 为什么是 lit：LLVM 测试哲学与工具边界

> **目标**：学完你能说清 LLVM 四层测试各测什么、lit 与 FileCheck / `not` / `split-file` 的边界，并在看到一条红色 FAIL 时判断「该怪谁」。

> **环境**：本章的 lit 行为在 LLVM `24.0.0git` / lit `24.0.0dev` 上实测。`opt`、`FileCheck`、`llvm-lit` 来自 `/home/ubuntu/llvm/builds/pr-lane/bin`（构建源码树 `1bfdad8e5c16`）；引用的两个测试文件在 hub（`3fa53fc0e1b2`）与构建树中逐字节相同。`llvm/test` 的文件计数在 hub 上统计。还没跑过最小 suite 的话，先回看 [Ch 0 — 环境、版本与第一个 lit suite](./00-environment-setup.md)。

---

## 1.1 先看一个真实测试：同一份文件，三种角色

定义先放一边。打开 `llvm/test` 里的一个真实文件：

`llvm/test/Transforms/BDCE/dead-void-ro.ll`

```llvm
; RUN: opt -S -passes=bdce < %s | FileCheck %s

target triple = "x86_64-unknown-linux-gnu"

define void @PR34211(ptr %p) {
; CHECK-LABEL: @PR34211
  %not_demanded_but_not_dead = load volatile i16, ptr %p
  call void @no_side_effects_so_dead(i16 %not_demanded_but_not_dead)
  ret void

; CHECK: %not_demanded_but_not_dead = load volatile i16, ptr %p
; CHECK-NEXT: ret void
}

declare void @no_side_effects_so_dead(i16) #0

attributes #0 = { nounwind readnone willreturn }
```

这份文件同时干三件事：

1. **输入**：`< %s` 把文件自身喂给 `opt`。
2. **脚本**：第一行的 `RUN:` 告诉 lit 要执行什么命令。
3. **断言**：`FileCheck %s` 又从同一个文件里读 `CHECK*` 行，拿它们去匹配 `opt` 的输出。

先手动跑一遍，确认它真的成立：

```bash
cd /home/ubuntu/llvm/llvm-project/llvm/test/Transforms/BDCE
/home/ubuntu/llvm/builds/pr-lane/bin/opt -S -passes=bdce < dead-void-ro.ll > /tmp/bdce-out.ll
echo "opt exit: $?"
cat /tmp/bdce-out.ll
/home/ubuntu/llvm/builds/pr-lane/bin/FileCheck dead-void-ro.ll < /tmp/bdce-out.ll
echo "FileCheck exit: $?"
```

真实输出：先看 `cat` 出来的文件内容：

```llvm
; ModuleID = '<stdin>'
source_filename = "<stdin>"
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target triple = "x86_64-unknown-linux-gnu"

define void @PR34211(ptr %p) {
  %not_demanded_but_not_dead = load volatile i16, ptr %p, align 2
  ret void
}

; Function Attrs: nounwind willreturn memory(none)
declare void @no_side_effects_so_dead(i16) #0

attributes #0 = { nounwind willreturn memory(none) }
```

```
opt exit: 0
FileCheck exit: 0
```

（上面两行是两条 `echo` 的输出。）

`bdce`（Bit-Tracking Dead Code Elimination）删掉了对 `no_side_effects_so_dead` 的调用，但保留了 `volatile` load——因为 volatile 访问有可观察副作用。测试的 `CHECK-NEXT: ret void` 还额外断言：load 之后紧跟着就是 `ret`，中间没有残留的 call。**实现意图、触发条件、预期结果，全在这 20 行里。**

再让 lit 跑同一个文件：

```bash
/home/ubuntu/llvm/builds/pr-lane/bin/llvm-lit -v \
  /home/ubuntu/llvm/trees/pr-lane/llvm/test/Transforms/BDCE/dead-void-ro.ll 2>/dev/null
```

真实输出：

```
-- Testing: 1 tests, 1 workers --
PASS: LLVM :: Transforms/BDCE/dead-void-ro.ll (1 of 1)

Testing Time: 0.04s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

注意 lit 的输出里没有任何 IR、没有任何 `CHECK` 匹配细节。**lit 不知道 `CHECK` 是什么，也不读它。**它把 `RUN:` 行变成一条命令，执行，然后看退出码。是 `FileCheck` 的退出码 0 让整条 pipeline 变成 0，lit 只是把这个 0 翻译成 `PASS`。

> 这就是本章的主线：**lit 是执行与报告层，不是断言层。**后面所有「工具边界」的讨论，都是这句话的展开。

## 1.2 四层测试基础设施：被测对象不同，不是 runner 不同

`llvm/docs/TestingGuide.md` 的 Organization 一节把测试基础设施分成三大类：unit tests、regression tests、whole programs；随后又单列了 Debugging Information tests。把它们放到操作层面，就是四层：

| 层 | 写在哪里 | 用什么写 | 谁发现/调度 | 被测对象 | 典型目标 |
|---|---|---|---|---|---|
| unit tests | `llvm/unittests` | GoogleTest + GoogleMock | lit 的 `GoogleTest` format | C++ 类、函数、数据结构 | `check-llvm-unit` |
| regression tests | `llvm/test` | ShTest（`RUN:` + 断言工具） | lit | 单文件 IR / 汇编 / 诊断输出 | `check-llvm` |
| whole-program | `llvm-test-suite`（独立仓库） | C/C++ 程序 + lit 配置 | lit | 整个程序的输出与耗时 | 功能/性能基准 |
| debuginfo | `cross-project-tests/debuginfo-tests` | C/C++/IR + 调试器脚本 | lit（ShTest + `%test_debuginfo`） | 调试器看到的调试信息 | 跨项目调试测试 |

这张表里最容易看错的是第一行和第三行。

**第一行：GoogleTest 是「写测试」的框架，lit 是「跑测试」的调度器。**官方文档说 unit tests 用 GoogleTest/GoogleMock 写在 `llvm/unittests`，并且「unit tests are reserved for targeting the support library and other generic data structure」，IR 变换和分析优先用 regression tests。但在 LLVM 的构建里，`check-llvm-unit` 本身就是一个 lit suite：`llvm/test/Unit/lit.cfg.py` 把 `config.test_format` 设成 `lit.formats.GoogleTest`，`llvm/test/Unit/CMakeLists.txt` 用 `add_lit_testsuite` 定义 `check-llvm-unit`。所以「unit test 用 GoogleTest」说的是作者接口；「谁来调度这些 gtest 二进制」仍然可以是 lit。[Ch 23 — GoogleTest format](./23-googletest-format.md) 会拆开这个 format。

**第三行：test-suite 不是「另一种 runner」，而是「另一种被测对象」。**`TestSuiteGuide.md` 的 Quickstart 第一步就写：

> The lit test runner is required to run the tests.

它的示例输出长这样（文档示例，本章未在本机跑 test-suite）：

```
PASS: test-suite :: MultiSource/Applications/ALAC/decode/alacconvert-decode.test (1 of 474)
```

同一个 lit、同一套 `PASS/FAIL` 报告，只是测试文件变成「编译并运行一个完整程序」，比较的是程序输出和计时，而不是单个函数的 IR。回归测试回答「这个 pass 有没有改错一条指令」；test-suite 回答「整个编译器把一档程序编出来还能不能跑、跑得多快」。**对象不同，工具可以相同。**

**第四行：debuginfo 也不是新 runner。**`cross-project-tests/lit.cfg.py` 用的就是 `lit.formats.ShTest()`，suffixes 包含 `.c`、`.cpp`、`.test`，并注册了 `%test_debuginfo` 这个 `ToolSubst`，把测试交给调试器脚本执行。TestingGuide 对它的描述是：测试用 C 系语言或 LLVM 汇编写，编译后在调试器下运行，检查调试器输出。

画成一张图：

```
                  ┌──────────────────────────────┐
                  │ 被测对象（what）              │
                  ├───────────────┬──────────────┤
   unit           │ C++ 类/函数    │ GoogleTest   │
   regression     │ 单文件 IR/汇编 │ ShTest       │
   debuginfo      │ 调试器行为     │ ShTest+脚本  │
   whole-program  │ 整个程序/性能  │ lit + harness│
                  └───────────────┴──────────────┘
                           ▲
                           │ 共用：发现、配置、并行、超时、报告
                        lit
```

所以「LLVM 有四套测试基础设施」更准确的说法是：**四类被测对象，共用一套调度器。** [Ch 12 — `lit.site.cfg` 与 CMake](./12-site-cfg-and-cmake.md) 会讲这套调度器怎么接进构建树。

## 1.3 2009：lit 为什么被造出来

lit 的第一版提交可以精确到 commit：

```
commit a213a427aa19260bb8145ea59f485ea01653c0bb
Author: Daniel Dunbar <daniel@zuster.org>
Date:   Tue Sep 8 05:31:18 2009 +0000

    Add 'lit' testing tool.
```

这次提交改了 15 个文件、插入 2,623 行，已经包含 `lit.py`、`TestRunner.py`、`TestingConfig.py`、`ProgressBar.py`，以及两个 test format：`ShTest.py` 和 `TclTest.py`。换句话说，**lit 从第一天起就不是「一个只跑 shell 脚本的小工具」，而是「一个可插多种测试格式的调度器」。**

今天的 `llvm/utils/lit/README.rst` 把设计目标列为四条：

| README 目标 | 2009 年的对应机制 | 今天的落点 |
|---|---|---|
| Portable | Python 实现；`ShTest(execute_external=False)` 默认走内置执行 | `TestRunner.executeScriptInternal` |
| Flexible test discovery | suite 由配置定义，按目录/后缀发现 | `discovery.py`、`TestingConfig.py` |
| Parallel test execution | `-j`，首版用线程池 | `run.py` + `worker.py`（进程池） |
| Multiple test formats | `ShTest` + `TclTest` | `ShTest` + `GoogleTest` |

把这四条放回 2009 年，可以这样理解（这是解释，不是文档原话）：

- **Portable**：LLVM 要在 Linux、macOS、Windows 的构建机上跑同一批测试。把测试执行器写成 Python，并在 Python 里实现一个足够用的 shell 子集，比要求每台机器都有同款 `bash` 更可控。首版 `ShTest` 的默认参数就是内置执行；今天的内置 shell 在 `TestRunner.executeScriptInternal()`，[Ch 7 — lit 内置 shell](./07-internal-shell.md) 会逐段读。**再强调一次：它不是 bash。**
- **Flexible test discovery**：测试散在 `llvm/test` 的几十个目录里，Clang 还有自己的一套。用「配置 + 后缀 + 排除」描述一个 suite，比在每个目录写 Makefile 规则更容易扩展。
- **Parallel**：测试数量增长后，串行跑完整套要很久；`-j` 从首版就有。首版用 `threading.Thread`，今天用 `multiprocessing` / `ProcessPoolExecutor`（`run.py`），因为编译器测试是 CPU 密集且大量起子进程；调度细节见 [Ch 15 — 并行执行模型](./15-parallel-execution.md)。
- **Multiple test formats**：首版同时带 `ShTest` 和 `TclTest`，说明它要接不止一种测试形态。DejaGNU/Tcl 的具体机制本章不展开；可查证的结局是，LLVM 在 2012 年从 Makefile 移除最后的 DejaGNU 支持（`256d3a9eaad7`），2013 年清掉文档里的相关提及（`09a682ab5a59`）。

这也解释了一个常见误会：lit 不是「LLVM 版 pytest」。它诞生时的第一诉求是**把异质的测试目录统一调度起来**，而不是提供一套新的断言 API。

## 1.4 为什么回归测试是「一个文件 = 输入 + 脚本 + 断言」

xUnit 风格的测试（JUnit、pytest 都属这一类）通常长这样：一个函数、一组 fixture、若干 `assert`。LLVM 的回归测试反过来：**一个文件就是全部**。这不是偷懒，而是为编译器测试做的取舍。

四个直接好处：

1. **可复现**：把文件拿走，照着 `RUN:` 行敲一遍就能复现。没有隐藏的 `conftest.py`、没有测试类的初始化顺序、没有「要先跑哪个 fixture」。
2. **最小**：IR 可以从真实程序里蒸馏，只留下触发 bug 的那几条指令。reviewer 不需要在几百行测试代码里找「到底在测什么」。
3. **没有框架样板**：新增一个测试 = 新增一个文件。作者不需要 import、不需要写函数、不需要记住断言库 API；C、`.ll`、`.mir`、`.s`、`.yaml` 都能用同一种方式测。
4. **review 时一眼可见**：diff 里同时出现「输入怎么变」和「预期输出怎么变」。如果实现改了行为，`CHECK` 的改动就在同一个 hunk 旁边。

代价也要说清楚：每个测试是一条独立的命令 pipeline，没有共享 fixture；断言是行匹配，不适合表达复杂的数据结构关系；复杂的前置文件要靠 `split-file` 或 `Inputs/` 目录。**它是为「大量、小型、外部进程」优化的，不是通用测试框架。**

把同一个 BDCE 回归用 pytest 写一遍，差异就很直观（**示意，未实测；这段代码没有在本机运行**）：

```python
# 示意，未实测：pytest 版的同一个 BDCE 回归
import subprocess
from pathlib import Path

OPT = "/home/ubuntu/llvm/builds/pr-lane/bin/opt"

def test_volatile_load_survives_and_dead_call_is_removed():
    ir = Path("dead-void-ro.ll").read_text()
    out = subprocess.run(
        [OPT, "-S", "-passes=bdce"],
        input=ir, capture_output=True, text=True, check=True,
    ).stdout
    body = out.split("define void @PR34211", 1)[1]
    assert "load volatile i16, ptr %p" in body
    assert "call void @no_side_effects_so_dead" not in body
    assert "\n  ret void\n}" in body
```

| 维度 | pytest 版（示意） | lit 版（本章实测） |
|---|---|---|
| 测试入口 | Python 函数 | 一个 `.ll` 文件 |
| 断言 | Python `assert` | FileCheck 的 `CHECK*` 行 |
| 顺序约束 | 自己写字符串/正则逻辑 | `CHECK-NEXT` 直接表达相邻 |
| 跳过条件 | `skipif` + 自己维护的条件 | `REQUIRES:` / `UNSUPPORTED:` |
| 新增测试成本 | 建模块、写函数、配 discovery | 新建文件、写 `RUN:` |
| 并行 | 需要 `pytest-xdist` 等插件 | 内置 `-j` |
| 适合 | 纯 Python 项目、复杂 fixture | 编译器/工具链、跨平台子进程 |

**诚实结论**：如果项目是纯 Python，pytest 更好——fixture、参数化、`assert` 重写、生态都更强。lit 的取舍是明确的：它把「每个测试是一个独立的外部命令」作为第一假设，用这个假设换来了跨平台、低样板和高并行度。

## 1.5 工具边界：一条 `RUN:` 行上的五个角色

把 1.1 的 pipeline 拆开：

```
RUN: opt -S -passes=bdce < %s | FileCheck %s
     └───────┬────────┘   └────┬────┘   └──┬───┘
          被测程序           输入        断言工具
             └──────────────────┬─────────────┘
                     lit 执行整条 pipeline
                     只看退出码，不看内容
```

| 工具 | 负责 | 不负责 | 在本章例子里 |
|---|---|---|---|
| `lit` | 发现测试、读配置、调度、执行、超时、汇总、报告 | 判断输出内容对不对 | 把 `RUN:` 变成命令，把退出码 0 变成 `PASS` |
| `FileCheck` | 按 `CHECK*` 行扫描输入，匹配失败时给出行号 | 执行被测程序 | 检查 volatile load 还在、call 没了 |
| `not` | 反转被调命令的退出码 | 检查输出；解释为什么失败 | `not opt ...` 把「预期报错」变成成功 |
| `split-file` | 把一个文件拆成多个临时文件 | 跑测试、做断言 | 一个测试需要多份 IR 时用 |
| `opt` / `llc` | 被测程序本身：优化、代码生成 | 知道 `CHECK` 是什么 | 产生被 FileCheck 检查的 IR/汇编 |

两个必须记牢的语法归属：

- **`RUN:` 是 lit 的语法。**TestingGuide 明确说：`RUN` lines「are interpreted by `lit`」，即使看起来像 shell script，「they are not」。
- **`CHECK:` 不是 lit 的语法。**它是 FileCheck 的指令。lit 只把 `%s` 原样传给 `FileCheck`，从不解析 `CHECK`。分不清这一点，就会在「测试为什么失败」上永远绕圈。[Ch 9 — FileCheck 与 lit 的边界](./09-filecheck-boundary.md) 专门处理这条边界。

`not` 值得单独看，因为它最容易被误解成「断言工具」。它只做一件事：把退出码取反。

```bash
/home/ubuntu/llvm/builds/pr-lane/bin/not false
echo "not false exit: $?"
/home/ubuntu/llvm/builds/pr-lane/bin/not true
echo "not true exit: $?"
```

真实输出：

```
not false exit: 0
not true exit: 1
```

`not true` 没有任何 stderr——当前实现里，命令「意外成功」时它只是返回 1，不打印原因（`llvm/utils/not/not.cpp` 的 `return Result == 0;`）。放到 lit 里，你能看到的也只有：

```
# RUN: at line 1
not true
# executed command: not true
# note: command had no output on stdout or stderr
# error: command failed with exit status: 1
```

这正是「lit 不解释语义」的现场：它忠实报告「这条命令退出码是 1」，至于为什么，要你自己知道 `not` 的契约。

再看一个真正的断言失败。把 1.1 的文件复制到临时 suite，只改错一个 `CHECK`（把 `load volatile` 写成 `add`），并让 FileCheck 少打印 input dump：

```llvm
; RUN: opt -S -passes=bdce < %s | FileCheck --dump-input=never %s
...
; CHECK: %not_demanded_but_not_dead = add i16, ptr %p
```

用 lit 跑这个临时 suite，真实输出（节选，保留了完整失败块）：

```
-- Testing: 3 tests, 3 workers --
PASS: Ch1Demo :: dead-void-ro.ll (1 of 3)
FAIL: Ch1Demo :: dead-void-ro-broken.ll (2 of 3)
******************** TEST 'Ch1Demo :: dead-void-ro-broken.ll' FAILED ********************
Exit Code: 1

Command Output (stdout):
--
# RUN: at line 1
opt -S -passes=bdce < /tmp/ch1-demo/dead-void-ro-broken.ll | FileCheck --dump-input=never /tmp/ch1-demo/dead-void-ro-broken.ll
# executed command: opt -S -passes=bdce
# executed command: FileCheck --dump-input=never /tmp/ch1-demo/dead-void-ro-broken.ll
# .---command stderr------------
# | /tmp/ch1-demo/dead-void-ro-broken.ll:11:10: error: CHECK: expected string not found in input
# | ; CHECK: %not_demanded_but_not_dead = add i16, ptr %p
# |          ^
# | <stdin>:6:21: note: scanning from here
# | define void @PR34211(ptr %p) {
# |                     ^
# | <stdin>:7:2: note: possible intended match here
# |  %not_demanded_but_not_dead = load volatile i16, ptr %p, align 2
# |  ^
# `-----------------------------
# error: command failed with exit status: 1

--
```

读这段输出时，责任链是清楚的：

- `# executed command: FileCheck ...` 告诉你真正失败的是第二个进程。
- `# | ...:11:10: error: CHECK: expected string not found` 是 FileCheck 的报错，指向 `.ll` 的第 11 行。
- `# error: command failed with exit status: 1` 是 lit 的结论：pipeline 退出码非 0。

lit 没有说「IR 错了」，也没有说「CHECK 写错了」；它只说「这条 pipeline 失败了」。**判断责任在读者，工具只提供证据。**这就是上面那张边界表的全部意义。

`split-file` 也属于「提供证据/准备输入」的一侧，不属于断言。TestingGuide 给的形态是：

```llvm
; RUN: split-file %s %t
; RUN: llvm-link -S %t/a.ll %t/b.ll | FileCheck %s

; CHECK: ...

;--- a.ll
...
;--- b.ll
...
```

（这是文档片段，本章未运行。）它把一个文件按 `;--- <part>` 拆成多个临时文件，让「一个测试文件」仍然能携带多份输入。

## 1.6 lit 不做什么

- **不是断言库**：不比较输出、不提供 `assert`；通过与否主要看命令退出码。
- **不是构建系统**：不编译被测程序；工具路径由 `lit.site.cfg` / CMake / 环境提供（Ch 12）。
- **不是 benchmark harness**：能跑基准，但不负责统计、对比、画图；test-suite 的 `utils/compare.py` 是另一层。
- **不是 unit test 框架**：没有 fixture、mock、参数化；`GoogleTest` format 只是把已有 gtest 二进制接进来（Ch 23）。
- **不是 CI**：不触发流水线、不上传结果；CI 调用 lit，消费它的退出码或 JSON 报告（Ch 17）。

## 1.7 真实数字锚点

这些数字可以自己复算：

```bash
cd /home/ubuntu/llvm/llvm-project
find llvm/test -type f | wc -l
find llvm/test -type f -name '*.ll' | wc -l
find llvm/test -type f -name '*.mir' | wc -l
find llvm/utils/lit/tests -type f | wc -l
```

2026-11-08 在本机 hub 上的真实输出：

```
71921
47531
5615
647
```

读法：

- `llvm/test` 下 **71,921 个文件**，其中 **47,531 个 `.ll`**、**5,615 个 `.mir`**。
- `llvm/utils/lit/tests` 下 **647 个文件**，这是 lit 自己的可执行规格（Ch 19–25 会反复用）。

**不要把 71,921 当成「lit 发现的测试数」或「通过的测试数」。**`find` 统计的是文件，包含 `Inputs/` 里的辅助文件、`CMakeLists.txt`、`README.txt` 等；lit 还会按 `config.suffixes` 和 `config.excludes` 过滤。本章不提供通过率，也不该从文件数推算通过率。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「lit 是断言库，`CHECK` 是 lit 语法」 | `CHECK` 是 FileCheck 的指令；lit 只解析 `RUN`/`REQUIRES`/`XFAIL` 等 directive，然后把命令交给 shell 执行。 |
| 「`llvm-test-suite` 是另一套 lit」 | 是同一个 lit，另一种被测对象：整个程序的输出与耗时，而不是单文件 IR 回归。 |
| 「unit tests 不经过 lit」 | 写测试用 GoogleTest；在 LLVM 构建里 `check-llvm-unit` 是 lit suite，`llvm/test/Unit/lit.cfg.py` 用 `lit.formats.GoogleTest` 调度 gtest 二进制。 |
| 「`RUN:` 行就是 bash」 | 它是 lit 内置 shell 的子集（LLVM 24 默认），不是 bash；`export`、变量展开、管道语义都有差异。 |
| 「`not` 会告诉我为什么失败」 | `not` 只反转退出码。当前实现在命令意外成功时静默返回 1，原因要自己推断。 |
| 「FAIL 一定是被测程序错了」 | 可能是 FileCheck 模式错、工具缺失（UNRESOLVED）、超时或配置错；先看 `# executed command` 和最后一条 `# error`。 |

## 重点整理

- LLVM 的四层测试是「四类被测对象」，不是「四个 runner」；lit 可以调度 ShTest，也可以调度 GoogleTest。
- `test-suite` 由 lit 驱动，但测的是完整程序的输出与性能；它和 `llvm/test` 的区别在对象，不在执行器。
- lit 诞生于 2009-09-08（`a213a427aa19`），首版就带 `ShTest` 和 `TclTest`，四个设计目标是 portable、flexible discovery、parallel、multiple formats。
- integrated test 的核心是「一个文件同时是输入、脚本、断言源」；这让测试可复现、最小、低样板、review 友好。
- `RUN:` 是 lit 语法，`CHECK:` 是 FileCheck 语法；`not` 只管退出码，`split-file` 只管拆文件，`opt`/`llc` 才是被测程序。
- lit 的 FAIL 只说明「pipeline 退出码非 0」；定位要顺着 `RUN: at line N`、`# executed command`、最后一条 `# error` 往回读。
- 本机 `llvm/test` 有 71,921 个文件（47,531 个 `.ll`、5,615 个 `.mir`），`llvm/utils/lit/tests` 有 647 个文件；这些是文件数，不是通过率。

## 自我检核

不看笔记，试着回答：

- [ ] 用一句话解释：为什么 `llvm-test-suite` 不是「另一个 lit」？再举一个它和 `llvm/test` 在「被测对象」上的具体差别。
- [ ] 给出 `RUN: opt ... | FileCheck ...` 这条命令，分别指出哪部分是 lit 语法、哪部分是 FileCheck 语法、哪部分是被测程序。
- [ ] unit tests 用 GoogleTest 写，为什么仍可能由 lit 调度？「写测试的框架」和「跑测试的调度器」在这里各自是谁？
- [ ] 一条测试 FAIL 时，列出至少三种可能的责任方，并说明你会先看输出里的哪一行来缩小范围。
- [ ] 用本章的 `dead-void-ro.ll` 说明「输入、脚本、断言」三重角色；如果删掉 `CHECK-NEXT`，这个测试会失去什么约束？
- [ ] 2009 年的四个设计目标里，哪一个最能解释「lit 不用 bash」？它的代价是什么？

## 延伸阅读

- **[LLVM Testing Infrastructure Guide — Organization](https://llvm.org/docs/TestingGuide.html#llvm-testing-infrastructure-organization)**
  - 读哪里：从 `LLVM Testing Infrastructure Organization` 到 `Debugging Information tests`，约 80 行；再读 `Extra files` 一节。
  - 能学到：四层测试的官方定义、unit/regression 的分工、`split-file` 的推荐用法。
  - 前提：无；读完回看本章 1.2 的表格。
- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)**
  - 读哪里：`DESCRIPTION` 与 `lit-infrastructure` 两节。
  - 能学到：lit 的定位（轻量、可移植、并行）、测试发现与 format 的抽象；这是 Ch 2 的官方对照物。
  - 前提：跑过 Ch 0 的最小 suite。
- **[LLVM Test Suite Guide — Quickstart](https://llvm.org/docs/TestSuiteGuide.html#quickstart)**
  - 读哪里：Quickstart 的前 5 步与示例 `PASS: test-suite :: ...` 输出。
  - 能学到：test-suite 为什么需要 lit、它和回归测试在对象上的区别。
  - 前提：知道 CMake 的基本用法；不需要真的下载 test-suite。
- **[lit README — Features / Contributing to lit](https://github.com/llvm/llvm-project/blob/3fa53fc0e1b2/llvm/utils/lit/README.rst)**
  - 读哪里：`Features` 四条与 `Contributing to lit`。
  - 能学到：四个设计目标的原文、lit 自测怎么跑、依赖哪些 LLVM 工具。
  - 前提：能读英文技术文档；想动手改 lit 时再回来看。
- **[First lit commit a213a427aa19](https://github.com/llvm/llvm-project/commit/a213a427aa19260bb8145ea59f485ea01653c0bb)**
  - 读哪里：commit message 与文件列表，重点看 `ShTest.py`、`TclTest.py`、`TestRunner.py`。
  - 能学到：lit 第一版的功能边界，以及「多 format」不是后来才加的。
  - 前提：会用 `git show`。

> 下一章把这条链拆成对象：一次 lit 调用到底创建了什么、按什么顺序调用、每个对象归谁所有。

→ [Ch 2 lit 全景架构：一次调用经过哪些对象](./02-lit-architecture.md)
