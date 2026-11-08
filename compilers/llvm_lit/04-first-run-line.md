# Ch 4 — 第一条 RUN: 行

> **目标**：把一条 `RUN:` 从「文件里的一行文本」追到「shell 真正执行的命令」，并能在失败时独立读出 `Exit Code`、`# RUN: at line N`、`# executed command`、`# error` 四层信息；同时分清 lit 的 `RUN:` 与 FileCheck 的 `CHECK:`。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。本章所有输出都在 `/tmp/lit-ch4lab.XzgqPC` 这个临时 suite 里实跑；LLVM IR 例子使用 `/home/ubuntu/llvm/builds/pr-lane/bin/{opt,FileCheck}`。

---

## 4.1 先建立一个最小的心智模型

一条 `RUN:` 不是「注释」，也不是「断言」。它是一段会被 lit 抽出来、拼成脚本、交给 lit 内置 shell 执行的命令。

```
源文件里的文本
    │
    │  parseIntegratedTestScriptCommands()   ← 在整个文件里按关键字切
    ▼
[(line_number, "RUN:", " echo hi")]
    │
    │  applySubstitutions()                  ← %s/%t/... 在这里展开
    ▼
["echo hi"]
    │
    │  多条 RUN 用 && 串起来
    ▼
内置 shell 解析 → 执行 → 得到退出码
    │
    ▼
退出码 0 = PASS，非 0 = FAIL
```

这四步分别对应 `TestRunner.py` 里的四个位置：

| 阶段 | 函数 | 本章关心什么 |
|---|---|---|
| 抽取 | `parseIntegratedTestScriptCommands()`，第 1283 行 | 哪些行会被当成 `RUN:` |
| 解析 | `parseIntegratedTestScript()`，第 2105 行 | 有没有 `RUN:`、有没有语法错误 |
| 替换 | `applySubstitutions()`，第 1651 行 | `%s`/`%t` 变成什么 |
| 执行 | `executeShTest()` → `_runShTest()`，第 2277 / 2165 行 | 退出码从哪来 |

> 现在只需要记住：**lit 只看命令退出码，不读 `CHECK:`**。`CHECK:` 是 FileCheck 的输入，不是 lit 的语法。Ch 9 会把这条边界彻底讲清。

## 4.2 最小的一行：`# RUN: echo hi`

准备一个只有三个文件的 suite：

```text
lit-ch4lab/
├── lit.cfg.py
└── tests/
    └── hello.test
```

`lit.cfg.py`：

```python
import os, lit.formats
config.name = "Ch4Lab"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test", ".ll"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
```

`tests/hello.test`：

```text
# RUN: echo hi
```

跑：

```bash
cd /tmp/lit-ch4lab.XzgqPC
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -a tests
```

真实输出（节选）：

```text
PASS: Ch4Lab :: tests/hello.test (1 of 1)
Exit Code: 0
--
# RUN: at line 1
echo hi
# executed command: echo hi
# | hi
...
Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

这里有三个容易看漏的点：

1. `Exit Code: 0` 是**整个测试脚本**的退出码，不是某条命令的。
2. `# RUN: at line 1` 是 lit 自己插入的调试标记，不是你的文件内容。实现位置是 `buildPdbgCommand()`（第 82 行）配合 `_handleCommand()` 里的 `f"{keyword} at line {line_number}"`（第 1952 行）。
3. `# executed command: echo hi` 是**替换之后**真正交给 shell 的命令（`executeScriptInternal()` 第 1105 行）。排查替换问题时，先看这一行。

`-a` 是 `--test-output=all`，通过的测试也打印执行轨迹。只想看失败时用 `-v`（`--test-output=failed`）；对通过的测试跑 `-v`，只有 `PASS` 行和摘要，没有任何命令轨迹（实测）。

## 4.3 多条 `RUN:` 如何拼接，失败如何短路

同一个文件里的多条 `RUN:` 会被收集成一个有序命令列表。`executeScriptInternal()` 在第 1057–1063 行用平衡二叉树把它们用 `&&` 串起来：

```python
def make_tree(cmds):
    if len(cmds) == 1:
        return cmds[0]
    split = len(cmds) // 2
    return ShUtil.Seq(make_tree(cmds[:split]), "&&", make_tree(cmds[split:]))
```

所以「多条 `RUN:`」在语义上接近：

```text
cmd1 && cmd2 && cmd3
```

不是 `cmd1; cmd2; cmd3`。这意味着前一条失败后，后面的命令不会执行。

写一个失败例子：

```text
# RUN: echo before
# RUN: false
# RUN: echo after
```

`-v` 真实输出（节选）：

```text
FAIL: Ch4Lab :: tests/fail.test (1 of 1)
Exit Code: 1
--
# RUN: at line 1
echo before
# executed command: echo before
# | before
# RUN: at line 2
false
# executed command: false
# error: command failed with exit status: 1
```

`after` 没有出现。这就是短路：lit 根本没执行第 3 条。

### 行继续：一条 `RUN:` 可以跨多行

`\` 结尾表示续行。下面三条 `RUN:` 实际合并成一条命令：

```text
# RUN: echo one \
# RUN:   two \
# RUN:   three
# RUN: echo four
```

真实输出（节选）：

```text
# RUN: at line 1
echo one    two    three
# executed command: echo one two three
# | one two three
# RUN: at line 4
echo four
# executed command: echo four
```

`CommandDirective.add_continuation()`（第 1516 行）把上一行末尾的 `\` 去掉再拼接，所以调试打印里会留下多个空格；shell 词法分析把连续空白折叠成一个分隔。最终执行的是 `echo one two three`。

### `;`、`&&`、`|` 的退出码不一样

这是 Chapter 4 最值得亲手打一次的实验。四个文件，每个只有一条 `RUN:`：

```text
# RUN: false; true
# RUN: true; false
# RUN: false | true
# RUN: true | false
```

真实结果（节选）：

| 测试内容 | 结果 | Exit Code |
|---|---|---|
| `false; true` | PASS | 0 |
| `true; false` | FAIL | 1 |
| `false \| true` | FAIL | 1 |
| `true \| false` | FAIL | 1 |

`false; true` 的完整轨迹很反直觉：

```text
PASS: Ch4Lab :: tests/seq1.test (1 of 4)
Exit Code: 0
--
# RUN: at line 1
false; true
# executed command: false
# error: command failed with exit status: 1
# executed command: true
```

明明打印了 `# error: command failed with exit status: 1`，测试却是 PASS。原因是：`;` 序列的最终状态取**最后一条命令**的状态，`true` 把整体拉回 0。很多教程把它概括成「最后一个非零退出码」，但在 lit 内置 shell 里更准确的说法是：

- `cmd1; cmd2`：整体退出码是 `cmd2` 的退出码；
- `cmd1 && cmd2`：`cmd1` 非 0 就停，整体非 0；
- `cmd1 | cmd2`：默认 `pipefail=True`，任一 stage 非 0 就非 0。

`pipefail` 的默认值在 `TestingConfig.py` 第 130 行：

```python
pipefail=True,
```

内置 shell 在 `ShParser` 构造时接收它（`ShUtil.py` 第 205 行），所以 `false | true` 会 FAIL。`true | false` 也 FAIL，因为最后一段本身失败。

> **实战规则**：一条 `RUN:` 里尽量只做一件事。需要「前一步成功才做下一步」就用 `&&` 或拆成多条 `RUN:`；不要用 `;` 把失败吞掉。

## 4.4 `%s`、`%S`、`%p`、`%t` 到底展开成什么

写一个测试把四个都打印出来：

```text
# RUN: echo "s=%s"
# RUN: echo "S=%S"
# RUN: echo "p=%p"
# RUN: echo "t=%t"
```

真实输出（`-a`，节选）：

```text
# RUN: at line 1
echo "s=/tmp/lit-ch4lab.XzgqPC/tests/paths.test"
# executed command: echo s=/tmp/lit-ch4lab.XzgqPC/tests/paths.test
# RUN: at line 2
echo "S=/tmp/lit-ch4lab.XzgqPC/tests"
# RUN: at line 3
echo "p=/tmp/lit-ch4lab.XzgqPC/tests"
# RUN: at line 4
echo "t=/tmp/lit-ch4lab.XzgqPC/Output/tests/Output/paths.test.tmp"
# executed command: echo t=/tmp/lit-ch4lab.XzgqPC/Output/tests/Output/paths.test.tmp
```

| 替换 | 含义 | 本例的值 |
|---|---|---|
| `%s` | 测试源文件的绝对路径 | `/tmp/lit-ch4lab.XzgqPC/tests/paths.test` |
| `%S` | `%s` 所在目录 | `/tmp/lit-ch4lab.XzgqPC/tests` |
| `%p` | 当前源码里与 `%S` 相同的目录 | `/tmp/lit-ch4lab.XzgqPC/tests` |
| `%t` | 临时文件基名（**不是目录**） | `/tmp/lit-ch4lab.XzgqPC/Output/tests/Output/paths.test.tmp` |

`%p` 容易让人误会。`getDefaultSubstitutions()` 第 1406–1410 行把 `s`、`S`、`p`、`t` 放进同一张表：`s` 用 `sourcepath`，`S`、`p` 都用 `sourcedir`，`t` 用 `tmpName`。所以在当前源码里 `%p` 和 `%S` 完全相同；不要假设 `%p` 是「父目录」。

### `%t` 的路径是怎么拼出来的

任务提示里的「约 1327 行」是旧估计；`grep -n 'def getTempPaths' llvm/utils/lit/lit/TestRunner.py` 确认实际在 1333 行：

```python
def getTempPaths(test):
    execpath = test.getExecPath()
    execdir, execbase = os.path.split(execpath)
    tmpDir = os.path.join(execdir, "Output")
    tmpBase = os.path.join(tmpDir, execbase)
    return tmpDir, tmpBase
```

而 `%t` 不是 `tmpBase`，是 `tmpBase + ".tmp"`（`getDefaultSubstitutions()` 第 1364 行的 `tmpName`）：

```python
tmpName = tmpBase + ".tmp"
```

本例里 `test.getExecPath()` 是 `/tmp/lit-ch4lab.XzgqPC/Output/tests/paths.test`，所以 `execdir` 是 `.../Output/tests`，`tmpDir` 再拼一个 `Output`，最终 `%t` 是 `.../Output/tests/Output/paths.test.tmp`。看起来有两个 `Output`，不是 bug：第一个 `Output` 是 `config.test_exec_root`，第二个是 `getTempPaths()` 为每个测试目录再加的隔离层。

### `%t` 是文件基名，不是目录

下面这个测试很常见：

```text
# RUN: echo data > %t
# RUN: echo child > %t/child
```

真实结果（节选）不是 FAIL，而是 UNRESOLVED，因为第二个重定向把 `%t` 当目录用，触发了 Python 异常：

```text
UNRESOLVED: Ch4Lab :: tests/t-not-dir.test (1 of 1)
NotADirectoryError: [Errno 20] Not a directory:
'/tmp/lit-ch4lab.XzgqPC/Output/tests/Output/t-not-dir.test.tmp/child'
```

正确做法：

- 想要一个临时文件：`> %t`、`> %t.out`；
- 想要一个临时目录：`mkdir -p %t.dir`，然后用 `%t.dir/...`；
- 千万不要把 `%t` 本身当目录。

### 版本钉子：`%T` 与 `%b` 都不要再写

同一个 suite 里跑：

```text
# RUN: echo "T=%T"
# RUN: echo "b=%b"
```

真实输出（节选）：

```text
# RUN: at line 1
echo "T=%T"
# executed command: echo T=%T
# | T=%T
# RUN: at line 2
echo "b=%b"
# executed command: echo b=%b
# | b=%b
```

它们不报错，也不展开，原样传给 shell。`%T` 的移除提交已经确认：

```console
$ cd /home/ubuntu/llvm/llvm-project
$ git log -1 --format='%h %ad %s' --date=short 7ff6973f1ae9
7ff6973f1ae9 2025-09-26 [lit] Remove support for %T
```

`%b` 在当前 `TestRunner.py` 里根本不存在；替代品是 `%{s:basename}`、`%{s:stem}`、`%{t:stem}`、`%basename_t`。Ch 6 会给出完整替换表。

## 4.5 失败输出四层解剖

再看一次第 4.3 节的失败输出，把它拆成四层：

```text
Exit Code: 1
...
# RUN: at line 2
false
# executed command: false
# error: command failed with exit status: 1
```

| 层 | 例子 | 来源 | 怎么用 |
|---|---|---|---|
| 1. 整体退出码 | `Exit Code: 1` | `_runShTest()` 第 2240 行拼出的 `output` | 先确认失败的是脚本整体，不是某条命令 |
| 2. 源文件行号 | `# RUN: at line 2` | `buildPdbgCommand()` 第 82 行 + `_handleCommand()` 第 1952 行 | 直接跳到源文件第 2 行 |
| 3. 实际命令 | `# executed command: false` | `executeScriptInternal()` 第 1105 行 | 看替换后的命令，不要凭原文猜 |
| 4. 失败原因 | `# error: command failed with exit status: 1` | 第 1151 行 | 确认是命令失败，不是 lit 解析失败 |

如果是管道，第 3 层会出现多行 `# executed command:`（示意，路径省略）：

```text
# executed command: opt -S -passes=instcombine /tmp/.../instcombine.ll
# executed command: FileCheck /tmp/.../instcombine.ll
```

lit 把管道拆成两个 stage 分别报告。哪一段非 0，后面的 `# error` 就指向哪一段（`pipefail=True` 时）。

## 4.6 跑一个真实的 LLVM IR 测试

现在把 `FileCheck` 接进来。`tests/instcombine.ll`：

```llvm
; RUN: opt -S -passes=instcombine %s | FileCheck %s

define i32 @f(i32 %x) {
  %y = add i32 %x, 0
  ret i32 %y
}

; CHECK-LABEL: define i32 @f(i32 %x) {
; CHECK-NEXT: ret i32 %x
; CHECK-NEXT: }
```

为了让 `opt` / `FileCheck` 能被找到，suite 配置里把构建目录加进 `PATH`：

```python
import os
config.environment["PATH"] = "/home/ubuntu/llvm/builds/pr-lane/bin:" + os.environ["PATH"]
```

PASS 的真实输出（节选）：

```text
PASS: Ch4Lab :: tests/instcombine.ll (1 of 1)
Exit Code: 0
--
# RUN: at line 1
opt -S -passes=instcombine /tmp/lit-ch4lab.XzgqPC/tests/instcombine.ll | FileCheck /tmp/lit-ch4lab.XzgqPC/tests/instcombine.ll
# executed command: opt -S -passes=instcombine /tmp/lit-ch4lab.XzgqPC/tests/instcombine.ll
# executed command: FileCheck /tmp/lit-ch4lab.XzgqPC/tests/instcombine.ll
```

这里真正发生的事：

1. lit 只抽出了 `; RUN:` 这一条。
2. `%s` 被换成 `.ll` 源文件路径。
3. `opt` 把 IR 里的 `add i32 %x, 0` 优化成 `ret i32 %x`。
4. `FileCheck` 读取 `opt` 的输出，并拿同一个文件里的 `CHECK-*` 行做匹配。
5. 管道整体退出码 0 → PASS。

### FileCheck 匹配失败长什么样

把期望改错一个变量：

```llvm
; CHECK-LABEL: define i32 @f(i32 %x) {
; CHECK-NEXT: ret i32 %y
; CHECK-NEXT: }
```

`-v` 真实输出（节选）：

```text
FAIL: Ch4Lab :: tests/instcombine-fail.ll (1 of 1)
Exit Code: 1
--
# RUN: at line 1
opt -S -passes=instcombine /tmp/.../instcombine-fail.ll | FileCheck /tmp/.../instcombine-fail.ll
# executed command: FileCheck /tmp/.../instcombine-fail.ll
# | instcombine-fail.ll:9:15: error: CHECK-NEXT: expected string not found in input
# | ; CHECK-NEXT: ret i32 %y
# | <stdin>:5:2: note: possible intended match here
# |  ret i32 %x
# error: command failed with exit status: 1
```

（节选：绝对路径前缀简写为 `/tmp/...`；真实输出里 `opt` 与 `FileCheck` 两段 `# executed command` 都在。）

注意分工：

- `# RUN: at line 1` / `# executed command` 是 **lit** 的输出；
- `error: CHECK-NEXT: expected string not found` 是 **FileCheck** 的 stderr；
- `# error: command failed with exit status: 1` 是 lit 对「管道整体失败」的总结。

`CHECK:` 行完全不被 lit 解析。反过来验证：一个只有 `CHECK:`、没有 `RUN:` 的文件：

```llvm
; CHECK: define i32 @f
define i32 @f() {
  ret i32 0
}
```

真实结果（节选）：

```text
UNRESOLVED: Ch4Lab :: tests/check-only.ll (1 of 1)
******************** TEST 'Ch4Lab :: tests/check-only.ll' FAILED ********************
Test has no 'RUN:' line
```

`Test has no 'RUN:' line` 来自 `_parseKeywords()` 第 2070 行。lit 说得很清楚：它找不到自己的指令，`CHECK:` 不算。

## 4.7 两类失败：命令失败 vs 匹配失败

| 失败类型 | 例子 | 谁返回非 0 | lit 看到的结果 |
|---|---|---|---|
| 命令自身失败 | `# RUN: false` | `false` → 1 | FAIL，`# error: command failed with exit status: 1` |
| 匹配失败 | `FileCheck` 找不到 `CHECK` | `FileCheck` → 1 | FAIL，stderr 里是 FileCheck 的诊断 |
| 命令不存在 | `# RUN: no-such-command` | shell → 127 | FAIL，stderr 是 `command not found` |

命令不存在时，lit 不会把它升级成 UNRESOLVED：

```text
FAIL: Ch4Lab :: tests/notfound.test (1 of 1)
Exit Code: 127
--
# RUN: at line 1
definitely-not-a-real-command-xyz
# executed command: definitely-not-a-real-command-xyz
# | 'definitely-not-a-real-command-xyz': command not found
# error: command failed with exit status: 127
```

UNRESOLVED 的语义是「lit 没能把测试正常跑起来」，典型情况：

- 没有 `RUN:` 行；
- directive 语法错误（例如 `REQUIRES: foo &&`）；
- shell 解析错误（例如引号不配对）；
- 测试执行期间抛了 Python 异常（例如把 `%t` 当目录）。

它不是「命令退出码非 0」。这个区别决定了你排查时该看 lit 还是看命令本身。

## 4.8 分层排查顺序

看到红字时按这个顺序读：先看结果码（`PASS/FAIL/UNRESOLVED/TIMEOUT/XFAIL/XPASS`）；`UNRESOLVED` 查 `Test has no 'RUN:'`、parser error、Python traceback；`FAIL` 看 `Exit Code` 与 `# RUN: at line N`；再看替换后的 `# executed command`；最后分清 stderr 是命令自己的诊断还是 FileCheck 的诊断。本章只需要走到这一步，Ch 18 会把超时、flaky、`%t` 残留和 shell 差异接进来。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`%T` 是临时目录」 | LLVM 24 已移除（commit `7ff6973f1ae9`），写了不报错也不展开；临时路径用 `%t`，需要目录就 `mkdir -p %t.dir`。 |
| 「`%t` 是一个目录」 | `%t` 是文件基名，末尾带 `.tmp`；把它当目录会触发 `NotADirectoryError`，测试变 UNRESOLVED。 |
| 「`RUN:` 必须写在注释里」 | lit 用原始字节正则扫描整个文件，`#`/`;`/`//` 都不是语法前提；`RUN: echo hi` 单独一行也能跑。 |
| 「命令找不到会变成 UNRESOLVED」 | 命令找不到是退出码 127 的 FAIL；UNRESOLVED 是 lit 解析/执行框架层面的失败。 |
| 「`-v` 会打印所有命令」 | `-v` = `--test-output=failed`，只对失败测试打印；看全部用 `-a`。 |
| 「错误信息里有 `# error` 就一定是 FAIL」 | `false; true` 会打印错误但整体 Exit Code 0，测试 PASS；以最终退出码为准。 |

## 重点整理

- `RUN:` 是 lit directive；`CHECK:` 是 FileCheck directive。lit 只把 `RUN:` 变成命令，退出码才是 PASS/FAIL 的依据。
- 多条 `RUN:` 用 `&&` 串起来（`make_tree()` 第 1063 行），前一条失败会短路后面的命令。
- `\` 续行把多条 `RUN:` 拼成一条命令；调试打印会保留多余空格，shell 词法会把它们折叠。
- `%s` 是源文件，`%S` 和 `%p` 在当前源码里都是源目录，`%t` 是 `test_exec_root/.../Output/<basename>.tmp` 的文件基名。
- `%t` 的路径由 `getTempPaths()`（第 1333 行）拼出；出现两个 `Output` 是 exec root 与 per-test 隔离层叠加，不是 bug。
- `-v` 只打印失败测试；`# RUN: at line N`、`# executed command`、`# error` 分别来自 lit 的不同层。
- 命令失败、FileCheck 失败、命令不存在（127）都是 FAIL；UNRESOLVED 是 lit 没能正常解析或启动测试。
- `;` 序列取最后一条命令的状态；`&&` 短路；`|` 默认 `pipefail=True`。不要用「最后一个非零退出码」这种模糊规则做判断。

## 自我检核

不看笔记，试着回答：

- [ ] 一条 `# RUN: echo hi` 从文件文本到真正执行，中间经过哪四个函数？每个函数负责什么？
- [ ] 为什么 `false; true` 会 PASS，而 `false | true` 会 FAIL？`Exit Code` 分别来自哪里？
- [ ] `%t` 为什么不是目录？如果我要一个临时目录，正确写法是什么？
- [ ] 看到一个 `UNRESOLVED`，你会先检查哪三类原因？它和退出码 127 的 FAIL 有什么本质区别？
- [ ] 在一个 `opt | FileCheck` 的失败里，哪些行是 lit 打印的，哪些行是 FileCheck 打印的？你怎么一眼分清？
- [ ] `%p` 在当前源码里等于什么？为什么不能凭旧文档猜它的语义？

## 延伸阅读

- **[lit Command Guide — TEST DISCOVERY / RUN lines](https://llvm.org/docs/CommandGuide/lit.html)**
  - 读什么：`TEST DISCOVERY` 与 `TEST FORMATS` 两节，重点看 ShTest 如何把 `RUN:` 变成脚本。
  - 能学到：官方对 ShTest 的抽象描述；对照本章的 `parseIntegratedTestScriptCommands` / `executeShTest`。
  - 前提：读完本章 4.1 的流水线图，否则容易把它当成 FileCheck 文档。
- **[LLVM Testing Infrastructure Guide — Writing new regression tests](https://llvm.org/docs/TestingGuide.html#writing-new-regression-tests)**
  - 读什么：`Writing new regression tests` 与 `RUN lines` 相关段落。
  - 能学到：LLVM 官方推荐的 `RUN` + `CHECK` 写法；为什么一个测试要能失败。
  - 前提：看得懂最小 LLVM IR；不要求会写 pass。
- **[`llvm/utils/lit/lit/TestRunner.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：`parseIntegratedTestScriptCommands()`（1283）、`getTempPaths()`（1333）、`executeScriptInternal()`（1019）、`_runShTest()`（2165）。
  - 能学到：本章每个输出字符串的真实来源；`%t` 路径与命令短路的确切实现。
  - 前提：能读 Python 函数与正则；不需要先懂整个 lit。
- **[`llvm/utils/lit/tests/shtest-run-at-line.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/shtest-run-at-line.py)**
  - 读什么：整个文件，尤其是它对 `# RUN: at line N` 与续行的 CHECK。
  - 能学到：lit 自己如何测试「失败行号」这件事；这是本章 4.5 节的可执行规格。
  - 前提：能读 FileCheck 的 `CHECK-LABEL`/`CHECK-NEXT`。

> 下一章把 `RUN:` 之外的所有 directive 一次讲完：为什么 `REQUIRES` 写错会变 UNRESOLVED、`XFAIL` 为什么不是跳过、`ALLOW_RETRIES` 怎样产生 FLAKYPASS。

→ [Ch 5 Directives：RUN、REQUIRES、UNSUPPORTED、XFAIL、ALLOW_RETRIES](./05-directives.md)
