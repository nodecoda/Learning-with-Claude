# Ch 0 — 环境、版本与第一个 lit suite

> **目标**：不构建 LLVM，只用一个 Python 和 20 行配置，跑起你的第一个 lit suite；然后故意让它失败，读懂 lit 的每一条输出与退出码。

> **环境**：本章在 LLVM `24.0.0git` / lit `24.0.0dev`、Ubuntu 22.04 x86_64、Python 3.10.12 上实跑。你不需要相同的 commit，但需要 Python 3.8+；LLVM 24 的行为差异会单独标注。

---

## 0.1 先建立心智模型：lit 到底在跑什么？

先别急着写配置。想象你有一个装测试文件的目录：

```
tests/
├── hello.test
└── math.test
```

lit 看到这个目录时，会问三个问题：

1. **这是不是一个 test suite？** —— 目录里有没有 `lit.cfg` / `lit.cfg.py` / `lit.site.cfg` / `lit.site.cfg.py`。
2. **哪些文件算测试？** —— suite 配置里的 `config.suffixes` 决定，比如 `[".test", ".ll"]`。
3. **每个测试怎么跑、算不算通过？** —— `config.test_format` 决定，最常见的是 `lit.formats.ShTest()`：把文件里的 `RUN:` 行抽出来，拼成一个 shell 脚本执行，**退出码为 0 就是 PASS**。

所以最小可用结构只有三样东西：

```
lit.cfg.py      ← 告诉 lit 这个 suite 叫什么、认哪些后缀、用哪种格式
hello.test      ← 一个带 RUN: 行的文件
RUN: ...        ← 真正被执行的命令
```

> **关键认知**：lit 本身不知道 `echo` 是什么，也不知道 `FileCheck` 是什么。它只负责把 `RUN:` 行变成命令、执行命令、看退出码。断言是命令自己做的。这一点会在 Ch 9 展开，现在先记住。

## 0.2 拿到 lit 的三种方式

### 方式一：直接跑 LLVM 源码里的入口（本章用这个）

LLVM 源码树自带 lit，不需要安装，也不需要构建：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --version
```

真实输出：

```
lit 24.0.0dev
```

`llvm/utils/lit/lit.py` 只是一个很薄的启动器，真正实现在 `llvm/utils/lit/lit/` 包里。读源码时从 `lit/main.py` 开始。

### 方式二：用构建目录里的 `llvm-lit`

如果你已经构建过 LLVM，构建目录会有一个包装脚本：

```bash
/home/ubuntu/llvm/builds/pr-lane/bin/llvm-lit --version
```

真实输出同样是：

```
lit 24.0.0dev
```

`llvm-lit` 和 `lit.py` 是同一套代码的不同入口。区别在于：`llvm-lit` 会带上构建时写死的 Python 解释器与 `--path`，更适合跑 LLVM 自己的测试。

### 方式三：安装到你的 Python 环境（自建 suite / CI 用）

`llvm/utils/lit` 是一个标准的 setuptools 包：

```bash
python3 -m pip install -e /home/ubuntu/llvm/llvm-project/llvm/utils/lit
lit --version
```

> **版本提醒**：如果你在 PyPI 上装 `lit`，拿到的不一定是 LLVM 24 的代码。课程里说的 `%{s:stem}`、`%if`、`%{readfile:}` 等较新特性，旧版可能没有。自建项目要在 `requirements.txt` 里 pin 住版本或直接 vendor 一份。

## 0.3 最小 suite：三个文件

新建一个空目录，比如 `~/lit-hello`，写入下面三个文件。

### 文件一：`lit.cfg.py`

```python
import os
import lit.formats

config.name = "HelloLit"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
```

逐行解释：

| 行 | 作用 | 不写会怎样 |
|---|---|---|
| `config.name` | suite 在报告里的名字 | 默认 `<unnamed>`，报告难读 |
| `config.test_format` | 用哪种格式跑测试 | 默认 `None`，不会发现测试 |
| `config.suffixes` | 哪些后缀算测试 | 默认空集合，一个也发现不了 |
| `config.test_source_root` | 测试源码根目录 | 默认是配置文件所在目录（通常也对） |
| `config.test_exec_root` | 测试执行/临时文件根目录 | 默认跟 source root 相同，会把 `Output/` 写进源码树 |

> **为什么要有 `test_exec_root`？** LLVM 是 out-of-tree 构建：源码在 `llvm/test`，临时文件写到 `build/test`。这样源码树保持干净，多个构建目录也不会互相踩。自建 suite 可以简单地把它设成 `source_root/Output`，但要记得把 `Output` 加进 `.gitignore`。

### 文件二：`tests/hello.test`

```text
# RUN: echo "hello, lit"
# RUN: test 2 -gt 1
```

两条 `RUN:` 会被拼成一个脚本，按顺序执行。任何一条退出码非 0，整个测试就 FAIL。

注意 `#` 不是 lit 的语法要求，只是这些文件通常用注释符号显得像注释。Ch 5 会展示一个反直觉的事实：**`RUN:` 出现在字符串里也会被解析**。

### 文件三：目录结构

```
lit-hello/
├── lit.cfg.py
└── tests/
    └── hello.test
```

现在跑：

```bash
cd ~/lit-hello
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py tests
```

真实输出：

```
-- Testing: 1 tests, 1 workers --
PASS: HelloLit :: tests/hello.test (1 of 1)

Testing Time: 0.01s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

退出码：

```bash
echo $?
# 0
```

## 0.4 逐行读懂这次运行

```
-- Testing: 1 tests, 1 workers --
```

- `1 tests` 是「本次选中多少个测试」。lit 不做单复数变形，所以会有 `1 tests` 这种语法。
- `1 workers` 是实际 worker 数。默认是 CPU 核数，但 `run_tests()` 里取了 `min(len(tests), opts.workers)`，所以只有一个测试时只开一个 worker。

```
PASS: HelloLit :: tests/hello.test (1 of 1)
```

格式是 `<结果码>: <suite 名> :: <suite 内相对路径> (第几个/共几个)`。这个格式从 `lit/display.py` 的 `Display.update()` 来，机器可解析。

```
Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

注意这里写的是 **Discovered**，不是「执行了 1 个」。`Total Discovered Tests` 包含被过滤（`EXCLUDED`）、`UNSUPPORTED`、`XFAIL` 的测试。Ch 16 会看到 `--filter` 后这个数字仍然是全集，只是多了 `Excluded` 一栏。

## 0.5 故意让它失败：`false` 与 `-v`

把 `hello.test` 改成：

```text
# RUN: echo "before"
# RUN: false
# RUN: echo "after"
```

再跑：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -v tests
```

真实输出（失败部分）：

```
-- Testing: 1 tests, 1 workers --
FAIL: HelloLit :: tests/hello.test (1 of 1)
******************** TEST 'HelloLit :: tests/hello.test' FAILED ********************
Exit Code: 1

Command Output (stdout):
--
# RUN: at line 1
echo "before"
# executed command: echo before
# .---command stdout------------
# | before
# `-----------------------------
# RUN: at line 2
false
# executed command: false
# note: command had no output on stdout or stderr
# error: command failed with exit status: 1

--

********************
```

这里有四个信息层，逐个看：

1. **`Exit Code: 1`**：整个测试脚本的退出码。它来自第一条失败的命令。
2. **`# RUN: at line 2`**：失败命令在源文件里的行号。这是 lit 在拼接脚本时插入的标记，直接告诉你去看哪一行。
3. **`# executed command: false`**：**替换之后**真正执行的命令。这是排查「替换错位」的第一手证据。
4. **`# error: command failed with exit status: 1`**：失败原因。

`-v` 的别名是 `--test-output=failed`：只对失败测试打印命令与输出。如果你想看**所有**测试执行了什么，用 `-a`（`--test-output=all`）：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -a tests/hello.test
```

真实输出（通过时也打印）：

```
-- Testing: 1 tests, 1 workers --
PASS: HelloLit :: tests/hello.test (1 of 1)
Exit Code: 0

Command Output (stdout):
--
# RUN: at line 1
echo "hello, lit"
# executed command: echo 'hello, lit'
# .---command stdout------------
# | hello, lit
# `-----------------------------
# RUN: at line 2
test 2 -gt 1
# executed command: test 2 -gt 1

--
```

注意 `# executed command: echo 'hello, lit'` 里给参数加了单引号，这是 lit 内置 shell 在做安全引用，不是你的原文变了。

## 0.6 第一个大坑：测试文件根本没被发现

把 `hello.test` 改名成 `hello.ll`，配置里的 `suffixes` 仍然是 `[".test"]`，再跑：

```
lit.py: /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit/discovery.py:273: warning: input 'tests' contained no tests
error: did not discover any tests for provided path(s)
exit=2
```

两个信息：

- **warning** 来自 `discovery.py`：这个输入路径下没有发现任何测试。
- **exit 2**：不是「测试失败」，是 **lit 自己没法开始**。退出码语义见下表。

| 退出码 | 含义 | 典型原因 |
|---|---|---|
| `0` | 所有选中测试都通过（或 `--ignore-fail`） | 正常 |
| `1` | 至少一个测试失败 | `FAIL` / `XPASS` / `TIMEOUT` / `UNRESOLVED` |
| `2` | lit 内部错误 / 没有发现测试 / 过滤为空 | 配置写错、路径给错、`--filter` 没匹配 |

> **踩坑直觉**：CI 里看到 exit 2，不要先怀疑代码。先看 warnings，通常是 suite 配置或路径问题。

## 0.7 看 lit 眼里的 suite 和测试

两个诊断命令，写配置时非常有用：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --show-suites tests
```

真实输出（suite 里有两个测试时）：

```
-- Test Suites --
  HelloLit - 2 tests
    Source Root: /tmp/ch0lab.SJx0tA
    Exec Root  : /tmp/ch0lab.SJx0tA/Output
    Available Features: 
    Available Substitutions: 
```

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --show-tests tests
```

真实输出：

```
-- Available Tests --
  HelloLit :: tests/hello.test
  HelloLit :: tests/fail.test
```

这两个命令都会在发现完测试后直接退出（`sys.exit(0)`），不会执行任何测试。配置明明写了 `suffixes` 却一个测试都没有时，先跑 `--show-suites`，确认 lit 找到的是不是你以为了那个 `lit.cfg.py`。

## 0.8 版本钉子：`%T` 已经没了

这是本课第一个「文档与源码冲突」的例子，值得在第一天就记住。

`llvm/docs/TestingGuide.md` 仍然写着：

```
%T
:   Directory of %t. Deprecated. ...
```

但在 LLVM 24 的 lit 源码里，`%T` 的支持已经被移除：

```bash
cd /home/ubuntu/llvm/llvm-project
git log -1 --format='%h %ad %s' --date=short 7ff6973f1ae9
# 7ff6973f1ae9 2025-09-26 [lit] Remove support for %T
```

实测把 `%T` 写进 `RUN:`，lit 不会报错，也不会展开，而是**原样传给 shell**：

```text
# RUN: echo "tmpdir=%T"
# executed command: echo tmpdir=%T
# | tmpdir=%T
```

同一个版本里，`%b` 也不存在（当前源码只有 `%{s:basename}`、`%{s:stem}`、`%{t:stem}`、`%basename_t` 等新形式）。所以：

> **规则**：本课涉及的每个替换，都以 `llvm/utils/lit/lit/TestRunner.py` 的 `getDefaultSubstitutions()` 为准。文档滞后时，源码是唯一事实。

Ch 6 会把当前替换表完整过一遍。现在你只需要记住：**不要用 `%T`，临时目录用 `%t` 或 `rm -rf %t && mkdir %t`。**

## 0.9 本章的最小闭环

到这一步，你已经完成了 lit 的最小闭环：

```
写 lit.cfg.py
   ↓
写带 RUN: 的测试文件
   ↓
lit 发现（suffixes / excludes）
   ↓
ShTest 解析 RUN: 行
   ↓
执行命令
   ↓
退出码 0 → PASS，非 0 → FAIL
   ↓
汇总 + 退出码 0/1/2
```

后面 25 章，都是在这条链上把每一段拆开：

- Ch 3：发现阶段还能怎么配。
- Ch 4–9：`RUN:` 行与 shell 的完整语言。
- Ch 10–14：`lit.cfg.py` 里还能放什么。
- Ch 15–18：执行、并行、报告、诊断。
- Ch 19–23：这条链在源码里到底长什么样。
- Ch 24–25：把它工程化，并提交回上游。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「文件放在目录里就会被跑」 | 只有后缀在 `config.suffixes` 里、且没被 `excludes` 排除的文件才会被发现。改名/换后缀后一定要跑 `--show-tests`。 |
| 「lit 返回 1 就是测试失败」 | 返回 `2` 是 lit 自己出错（配置/路径/过滤为空），跟测试代码无关。先看 warning。 |
| 「`-v` 会打印所有命令」 | `-v` 只对**失败**测试打印；看全部要用 `-a`。 |
| 「`RUN:` 必须在注释里」 | lit 的正则是在整个文件里找 `RUN:`，注释只是惯例。字符串里的 `RUN:` 也会被当成 directive。 |
| 「`%T` 还能用」 | LLVM 24 已移除，写了不报错但也不展开。用 `%t`。 |

## 重点整理

- 一个 suite 的最小要素：`lit.cfg.py`（name / test_format / suffixes）+ 带 `RUN:` 的测试文件。
- `ShTest` 的判定极简：脚本退出码 0 = PASS，非 0 = FAIL。
- 退出码 `0/1/2` 分别对应「全过」「有测试失败」「lit 内部错误」。
- `--show-suites` / `--show-tests` 是配置阶段最快的诊断工具。
- `-v` 看失败细节，`-a` 看所有执行细节；`# executed command:` 是替换后的真实命令。
- 文档会滞后，`%T` 是第一个证据；源码 `getDefaultSubstitutions()` 是权威。

## 自我检核

不看笔记，试着回答：

- [ ] 如果 `lit` 返回 2，你会先检查哪三件事？
- [ ] `config.test_exec_root` 和 `config.test_source_root` 为什么要分开？不分开会有什么后果？
- [ ] `-v` 和 `-a` 的区别是什么？分别在什么场景用？
- [ ] 为什么 `PASS: HelloLit :: tests/hello.test` 里的路径是 `tests/hello.test`，而不是绝对路径？
- [ ] 你能说出一条 `RUN:` 行从「文件里的文本」到「真正执行的命令」经过哪几步吗？说不全没关系，Ch 19–20 会补。

## 延伸阅读

- **[lit Command Guide — DESCRIPTION](https://llvm.org/docs/CommandGuide/lit.html#description)**
  - 读什么：开头到 `GENERAL OPTIONS` 之前，约 40 行。
  - 能学到：lit 的设计目标（轻量、可移植、并行）与「测试由 suite 定义」的核心模型。
  - 前提：无。读完再回看本章 0.1 的三问。
- **[LLVM Testing Infrastructure Guide — Quick start](https://llvm.org/docs/TestingGuide.html#quick-start)**
  - 读什么：`Quick start` 与 `Unit and Regression tests` 两节。
  - 能学到：LLVM 官方怎么跑 `check-llvm`、怎么单独跑一个测试；对照本章的 `lit.py tests`。
  - 前提：知道 `cmake`/`ninja` 的基本用法，不需要自己构建。
- **[lit README — Contributing to lit](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/README.rst)**
  - 读什么：`Contributing to lit` 一节，约 20 行。
  - 能学到：lit 自测怎么跑、依赖哪些 LLVM 工具（`not`、`FileCheck`）；这是 Ch 25 的入口。
  - 前提：能读英文技术文档。

> 下一章回答一个更根本的问题：LLVM 已经有单元测试、有 `test-suite`，为什么还需要 lit？它的边界到底在哪？

→ [Ch 1 为什么是 lit：LLVM 测试哲学与工具边界](./01-why-lit.md)
