# Ch 18 — 失败诊断学

> **目标**：拿到一份 lit 失败日志，能在 60 秒内回答四个问题：实际执行了什么？失败在哪个阶段？根因属于哪一类？最小修复是什么？不再看到 `FAIL` 就重跑，也不再把 FileCheck 的错误当成 lit 的错误。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`。案例在 `/tmp/litlab-a1`、`/tmp/litrace-b1`、`/tmp/litshell-c1` 中真实构造并运行，Ubuntu 22.04 x86_64、Python 3.10.12。

---

## 18.1 60 秒决策树

先看退出码，再看日志。不要从头读。

```
echo $?
├─ 0  → 没有 failure。若你觉得「应该失败」，检查 filter/shard/xfail/ignore-fail
├─ 1  → 至少一个测试 failure
│      ├─ 找 "FAIL: Suite :: path"
│      ├─ 看 "# RUN: at line N"          → 源文件第 N 行
│      ├─ 看 "# executed command:"       → 替换后真正跑的命令
│      ├─ 看最后一条 "# error:"          → 退出码/超时
│      └─ 判断：
│           command not found (127)      → 命令/替换/ PATH
│           command failed with status N → 被测程序失败
│           FileCheck error              → 断言不匹配，不是 lit 错
│           command reached timeout      → 单测试超时
│           Reached timeout of N seconds → TIMEOUT
└─ 2  → lit 自己出错
       ├─ did not discover any tests     → 路径/配置
       ├─ filter did not match any tests → filter 写错
       ├─ shard ... warning + exit 0     → 空 shard（不是错）
       ├─ worker process crashed         → segfault/OOM/kill
       └─ fatal: unable to parse config  → lit.cfg 语法/异常
```

然后再看结果码：

| 结果码 | 含义 | 常见原因 |
|---|---|---|
| `FAIL` | 测试脚本退出码非 0 | 命令失败、FileCheck 不匹配、断言失败 |
| `TIMEOUT` | 单测试超时 | `--timeout`/`config.maxIndividualTestTime` 太小，测试 hang |
| `UNRESOLVED` | lit 无法判定 | 没有 `RUN:`、脚本解析错误、worker 内异常、配置错误 |
| `XPASS` | 预期失败但通过了 | `XFAIL` 条件过宽，或 bug 已修复 |
| `SKIPPED` | 没执行 | `--max-failures`/`--max-time` 中止、worker 崩溃 |
| `UNSUPPORTED` | 环境不满足 | `REQUIRES`/`UNSUPPORTED` feature 不匹配 |
| `EXCLUDED` | 没被选中 | `--filter`、`--max-tests`、`--exclude-xfail` |

`FAIL`、`TIMEOUT`、`UNRESOLVED`、`XPASS` 都是 failure（`Test.py` 第 54–57 行），所以退出码 1。`SKIPPED`/`UNSUPPORTED`/`EXCLUDED` 不是。

## 18.2 日志行是谁生成的

| 日志行 | 生成位置 | 作用 |
|---|---|---|
| `# RUN: at line N` | `TestRunner._handleCommand()` 第 1952 行 `buildPdbgCommand(f"{keyword} at line {line_number}", line)` | 把源文件行号带进执行日志 |
| `# executed command: ...` | `executeScriptInternal()` 第 1105–1116 行 | 替换之后的真实命令，参数已 shell quote |
| `# .---command stdout---` | `formatOutput()` 第 980–1006 行 | 给 stdout/stderr 加边框与 `# |` 前缀 |
| `# note: command had no output...` | 第 1140–1141 行 | 命令无输出时的提示 |
| `# error: command failed with exit status: N` | 第 1144–1151 行 | 命令退出码非 0 |
| `# error: command reached timeout: ...` | 第 1152–1155 行 | 单命令超时 |
| `# shell parser error on RUN: at line N` | 第 1047–1050 行 `ScriptFatal` | 内置 shell 解析失败 → `UNRESOLVED` |

`RUN:` 行在解析时被改写成 `%dbg(RUN: at line N) command`（`TestRunner.py` 第 79–87 行），执行时再展开成 `@echo '# RUN: at line N' && ...`。所以 `# RUN: at line N` 不是 lit 事后猜的，而是解析阶段就写进脚本的标记。

`# executed command:` 里的命令已经完成替换：`%s`、`%t`、`%python`、`%{readfile:...}` 都变成了实际值；参数用 `shlex.quote()` 包过。排查「替换错位」时，这一行是第一手证据。

## 18.3 `FAIL` vs `UNRESOLVED` vs `TIMEOUT` vs `XPASS`

`TestRunner._runShTest()` 第 2207–2215 行决定 `FAIL`/`TIMEOUT`：

```python
out, err, exitCode, timeoutInfo, test_update_output = res
if exitCode == 0:
    status = Test.PASS
else:
    if timeoutInfo is None:
        status = Test.FAIL
    else:
        status = Test.TIMEOUT
```

`UNRESOLVED` 来自三类地方：

1. `parseIntegratedTestScript()` 捕获 `ValueError`（第 2118–2123 行）：没有 `RUN:`、`REQUIRES` 布尔表达式写错、`ALLOW_RETRIES` 不合法、指令行未终止。
2. `ShellEnvironment`/`ShParser` 解析失败：`ScriptFatal` → `UNRESOLVED`（第 2203–2205 行）。
3. worker 里 test format 抛异常：`worker._execute_test_handle_errors()` 第 96–102 行把它包成 `UNRESOLVED`，输出 `Exception during script execution:` + traceback。

`XPASS` 不是执行阶段产生的，而是 `Test.setResult()` 第 301–313 行在「预期失败 + 实际 PASS」时改写的：

```python
if expected_to_fail:
    if result.code is PASS:
        result.code = XPASS
    elif result.code is FAIL:
        result.code = XFAIL
```

所以 `XPASS` 的日志里 `Exit Code: 0`，但结果码是 failure。不要看到 `Exit Code: 0` 就以为成功了。

## 18.4 案例一：命令没找到（127）

测试：

```text
# RUN: definitely-not-a-command --version
```

真实日志：

```
FAIL: LitLab :: tests/fail/f02-missing-cmd.test (1 of 1)
Exit Code: 127

Command Output (stdout):
--
# RUN: at line 1
definitely-not-a-command --version
# executed command: definitely-not-a-command --version
# .---command stderr------------
# | 'definitely-not-a-command': command not found
# `-----------------------------
# error: command failed with exit status: 127
```

诊断：

- 实际执行：`definitely-not-a-command --version`，没有发生替换错位。
- 阶段：执行阶段，命令启动失败。
- 根因：命令不在 `PATH`；常见于工具替换写错、构建目录没加 `--path`、CI 没装依赖。
- 最小修复：确认工具存在并把目录加进 `--path`，或修正 `config.substitutions` 里的工具名。

真实对照：同一个 FileCheck 测试，没有 `--path` 时也是 127：

```
# | 'FileCheck': command not found
# error: command failed with exit status: 127
```

加 `--path /home/ubuntu/llvm/builds/pr-lane/bin` 后才进入真正的 FileCheck 失败。**127 不是断言失败，是环境问题。**

## 18.5 案例二：FileCheck 不匹配（被测程序失败 + 断言失败）

测试：

```text
# RUN: echo "actual value" > %t.txt
# RUN: FileCheck %s --input-file=%t.txt
# CHECK: expected value
```

真实日志（关键段）：

```
FAIL: LitLab :: tests/fail/f03-filecheck.test (1 of 1)
Exit Code: 1

# RUN: at line 2
FileCheck /tmp/litlab-a1/tests/fail/f03-filecheck.test --input-file=.../f03-filecheck.test.tmp.txt
# executed command: FileCheck ... --input-file=.../f03-filecheck.test.tmp.txt
# .---command stderr------------
# | /tmp/litlab-a1/tests/fail/f03-filecheck.test:3:10: error: CHECK: expected string not found in input
# | # CHECK: expected value
# |          ^
# | ... note: scanning from here
# | actual value
# | ...
# | error: no match found in search range
# `-----------------------------
# error: command failed with exit status: 1
```

诊断：

- 实际执行：`echo` 成功写文件，`FileCheck` 读同一个 `%t.txt`。
- 阶段：执行阶段；失败的是 FileCheck 这个**被测工具**，不是 lit。
- 根因：输入是 `actual value`，`CHECK` 要 `expected value`；断言与程序输出不一致。
- 最小修复：改测试输入/期望，或修被测程序；不要改 lit。

`FileCheck` 的 `error:` 行会带源码文件、行号、`CHECK:` 行和扫描位置。先读这三行，比看整段 stdout 快。

## 18.6 案例三：UNRESOLVED（没有 RUN / 解析错误 / REQUIRES 拼错）

### 没有 `RUN:`

测试只有注释：

```text
# This test intentionally has no RUN line.
# CHECK: nothing
```

真实日志：

```
UNRESOLVED: LitLab :: tests/fail/f05-unresolved-no-run.test (1 of 1)
******************** TEST '...' FAILED ********************
Test has no 'RUN:' line
```

根因：`_parseKeywords()` 第 2070 行 `raise ValueError("Test has no 'RUN:' line")`，`parseIntegratedTestScript()` 把它转成 `UNRESOLVED`。最小修复：加 `RUN:`，或确认这个文件根本不该被 `suffixes` 发现。

### shell 解析错误

```text
# RUN: echo "unclosed
```

真实日志：

```
UNRESOLVED: LitLab :: tests/fail/f06-unresolved-paren.test (1 of 1)
Exit Code: 1

# shell parser error on RUN: at line 1: echo "unclosed
```

根因：内置 shell 的 `ShParser` 解析失败；`ScriptFatal` 被 `_runShTest()` 转成 `UNRESOLVED`。最小修复：补引号，或改用不需要转义的写法。注意：这不是 `FAIL`，因为脚本根本没开始执行。

### `REQUIRES` 布尔表达式写错

```text
# REQUIRES: x86_64 &&
# RUN: true
```

真实日志：

```
UNRESOLVED: LitLab :: tests/fail/f08-requires-typo.test (1 of 1)
expected: '!', '(', '{{', or identifier
have: <end of expression>
in expression: 'x86_64 &&'
in REQUIRES: directive on test line 1
```

根因：`BooleanExpression` 解析 `x86_64 &&` 失败。注意这**不是**「feature 不存在」；feature 不存在会得到 `UNSUPPORTED`（`TestRunner.py` 第 2135–2142 行）。`REQUIRES` 的语法错误才是 `UNRESOLVED`。最小修复：补全表达式（`x86_64 && linux`）或删掉 `&&`。

> 区分：`REQUIRES: foo`（feature 不存在）→ `UNSUPPORTED`；`REQUIRES: foo &&`（语法错）→ `UNRESOLVED`。

## 18.7 案例四：XFAIL 变 XPASS

测试：

```text
# XFAIL: *
# RUN: true
```

真实日志：

```
XPASS: LitLab :: tests/fail/f04-xpass.test (1 of 1)
******************** TEST '...' FAILED ********************
Exit Code: 0

# RUN: at line 2
true
# executed command: true
# note: command had no output on stdout or stderr
```

诊断：

- 实际执行：`true`，退出码 0。
- 阶段：结果判定阶段。
- 根因：`XFAIL: *` 说「这个测试应该失败」，但它通过了；`Test.setResult()` 把 `PASS` 改成 `XPASS`。
- 最小修复：如果 bug 已修复，删掉 `XFAIL`；如果只是某些配置下失败，把 `*` 收窄成具体 feature 表达式。

`XPASS` 的退出码是 1。它是最容易被误判的一类，因为日志里全是成功迹象（`Exit Code: 0`、无 error），只有结果码是红的。

## 18.8 案例五：TIMEOUT 与 `--timeout` / `--max-time`

测试：

```text
# RUN: sleep 5
```

用 `--timeout=1`（单测试超时）运行：

```
TIMEOUT: LitLab :: tests/fail/f07-timeout.test (1 of 1)
Exit Code: -9
Timeout: Reached timeout of 1 seconds

# RUN: at line 1
sleep 5
# executed command: sleep 5
# note: command had no output on stdout or stderr
# error: command failed with exit status: -9
# error: command reached timeout: True
```

诊断：

- 实际执行：`sleep 5` 被杀，退出码 `-9`（SIGKILL）。
- 阶段：执行阶段；`TimeoutHelper` 到期后杀进程。
- 根因：单测试超过 `config.maxIndividualTestTime`（`--timeout`）。
- 最小修复：减小测试工作量，或按需提高 `--timeout`；不要把 `--max-time` 当成单测试超时。

`--max-time` 是整次运行的 deadline，触发后是 `warning: reached timeout, skipping remaining tests`，剩余测试 `SKIPPED`，不是 `TIMEOUT`。两者在 `run.py`（全局）和 `TestRunner.py`（单测试）分别实现。

## 18.9 案例六：worker 崩溃不是 FAIL

测试：

```text
# RUN: %python -c "import os, signal; os.kill(os.getppid(), signal.SIGKILL)"
```

真实日志：

```
-- Testing: 1 tests, 1 workers --
lit.py: .../main.py:283: error: a worker process crashed: A process in the process pool was terminated abruptly while the future was running or pending.

1 error(s) in tests

Testing Time: 0.03s

Total Discovered Tests: 1
  Skipped: 1 (100.00%)
```

退出码 2。诊断：

- 实际执行：测试杀掉了自己的父 worker。
- 阶段：worker 执行阶段；`BrokenProcessPool` → `WorkerCrashError`。
- 根因：segfault、OOM kill、abort、测试自己 `kill`。
- 最小修复：先复现崩溃本身（`-j 1`、`--debug`、core dump/ASan），而不是改 lit。

`--debug` 会让 worker 里的异常不被捕获（`worker.py` 第 93–98 行），所以你可能看到 Python traceback 而不是 `UNRESOLVED`。这是调试 worker 异常的正确姿势（该行为从源码确认；未单独构造异常做对照实测）。

## 18.10 内置 shell 与外部 shell：本机过、CI 挂

LLVM 24 默认用 lit 的**内置 shell**，不是 bash。`ShTest.__init__` 在 `formats/shtest.py` 第 21–33 行：

```python
def __init__(self, execute_external=False, ..., force_execute_external=False):
    if execute_external and not force_execute_external:
        raise ValueError(
            "execute_external=True is deprected as of LLVM-23 and the option will "
            "be removed in LLVM-24. ... set force_execute_external=True"
        )
```

真实实验对比同一段测试：

```text
# RUN: export FOO=bar
# RUN: echo $FOO
```

内置 shell 的输出：

```
# executed command: export FOO=bar
# executed command: echo '$FOO'
# | $FOO
```

外部 shell（`ShTest(execute_external=True, force_execute_external=True)`）的输出：

```
bar
```

差异原因：内置 shell 的 `echo` 是 builtin，不做变量展开；外部 bash 会展开 `$FOO`。所以一个测试如果依赖 `$FOO` 展开，在本机外部 shell 下过、在 CI 的内置 shell 下就会挂（或反过来）。

另外，`LIT_USE_INTERNAL_SHELL` 在 `llvm/utils/lit/lit/llvm/config.py` 第 77–84 行被读取：

```python
lit_shell_env = os.environ.get("LIT_USE_INTERNAL_SHELL")
if lit_shell_env:
    self.use_lit_shell = lit.util.pythonize_bool(lit_shell_env)
```

它改的是 LLVM 配置对象的 `use_lit_shell`；真正决定 ShTest 走哪条执行路径的，还是 `ShTest.execute_external`。在 LLVM 24 的 `llvm/test/lit.cfg.py` 里是 `ShTest(extra_substitutions=...)`（内置），要强制外部执行必须显式 `force_execute_external=True`，并且会收到弃用警告的约束。

`execute_external=True` 但不加 `force_execute_external` 的真实结果：

```
lit.py: .../TestingConfig.py:168: fatal: unable to parse config file '.../lit.cfg.py', traceback: ...
ValueError: execute_external=True is deprected as of LLVM-23 ...
```

退出码 2。这是配置错误，不是测试失败。

## 18.11 保留现场：`%t`、`Output/`、`LIT_PRESERVES_TMP`、`--debug`、ulimit、资源竞争

### `%t` 与 `Output/`

失败后先看 `# RUN: at line N` 里的路径和 `%t` 的实际值。例如：

```
# executed command: cat .../Output/tests/misc/Output/t02-tmp-not-dir.test.tmp/inside
# | [Errno 20] Not a directory: ...
```

现场在 `Output/` 下，`%t` 是文件不是目录。lit 不会自动清理 `%t`，所以失败后文件通常还在。不要一看到失败就 `rm -rf Output`；先把 `%t` 复制出来。

### `LIT_PRESERVES_TMP`

它保留的是 `lit-tmp-*` 包装目录（`main.execute_in_tmp_dir()` 第 290–318 行），不是 `Output/`。真实对比：

```
默认:        TMPDIR=/tmp/lit-tmp-hziu84hq
LIT_PRESERVES_TMP=1: TMPDIR=<unset>
```

排查「测试把临时文件写到系统 TMP 但没删」时用它；排查 `%t` 残留时直接看 `Output/`。

### `--debug`

`--debug` 打开 lit 自己的调试日志（配置加载、输入解析），并让 worker 异常不被捕获。它不会用 gdb/pdb 跑测试。真实输出：

```
lit.py: .../discovery.py:65: debug: loading suite config '/tmp/litlab-a1/lit.cfg.py'
lit.py: .../discovery.py:138: debug: resolved input 'tests/pass/p01.test' to 'LitLab'::('tests', 'pass', 'p01.test')
```

### ulimit 与资源竞争

`run.py` 第 336–362 行的 `_increase_process_limit()` 会把 `RLIMIT_NPROC` 软限制提高到 `workers * usable_core_count() * 2`，但目标值会先被截到硬限制。测试 fork 大量线程/进程时，先看 `ulimit -u`：

```bash
ulimit -u
# 253649
```

资源竞争的真实例子：4 个 writer 先 truncate 共享文件再 sleep 0.15 秒写入，4 个 reader 同时读：

```
FAIL: RaceLab :: tests/race/r1.test (1 of 8)
# | AssertionError: 0
...
Failed: 4 (50.00%)
```

同一 suite 用 `-j 1 --order=lexical` 时全部 PASS。根因不是被测程序，而是**测试之间共享 `%S/shared.txt`**。最小修复：改成 `%t`，或把 writer/reader 放进同一个 `parallelism_group`（上限 1）。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「看到 FAIL 就先重跑」 | 先读 `# RUN: at line`、`# executed command`、最后一条 `# error`；重跑只会让 flaky 更随机。 |
| 「FileCheck 报错是 lit 的问题」 | FileCheck 是被测工具；它的 `error:` 是断言不匹配，lit 只负责把退出码变成 FAIL。 |
| 「`# executed command` 不重要」 | 它是替换后的真实命令，能立刻发现 `%t`/工具替换错位。 |
| 「UNRESOLVED 就是测试失败」 | UNRESOLVED 是 lit 无法判定：没有 RUN、解析错误、worker 异常、配置错误。 |
| 「失败后先删 `%t`/`Output/`」 | 那是现场；先复制，再清理。 |
| 「XPASS 是成功」 | XPASS 是 failure，退出码 1。 |
| 「`--max-time` 和 `--timeout` 是一回事」 | 前者是整次运行，后者是单测试。 |
| 「内置 shell 就是 bash」 | 不是。变量展开、echo、管道语义都可能不同；这是本机过/CI 挂的常见根因。 |

## 重点整理

- 先看退出码：0 无 failure，1 有 failure，2 lit 内部错误。
- 日志四件套：`# RUN: at line N` → `# executed command` → 输出边框 → 最后一条 `# error`。
- `FAIL` = 脚本退出码非 0；`TIMEOUT` = 单测试超时；`UNRESOLVED` = lit 无法判定；`XPASS` = 预期失败却通过。
- `UNRESOLVED` 的常见原因：无 `RUN:`、shell 解析错误、`REQUIRES` 布尔表达式语法错、worker 异常。
- `REQUIRES: foo`（feature 缺失）是 `UNSUPPORTED`，不是 `UNRESOLVED`。
- LLVM 24 默认内置 shell，不是 bash；`execute_external=True` 已弃用，必须 `force_execute_external=True`。
- 保留现场：`Output/` 与 `%t` 不要急着删；`LIT_PRESERVES_TMP` 管的是 `lit-tmp-*`。
- 资源竞争先降 `-j` 复现，再查共享文件/端口/全局状态。

## 自我检核

不看笔记，试着回答：

- [ ] 拿到 `FAIL`，你会按什么顺序看日志？`FAIL`、`UNRESOLVED`、`TIMEOUT`、`XPASS` 分别在哪个阶段产生？
- [ ] 为什么 `REQUIRES: foo` 是 `UNSUPPORTED`，而 `REQUIRES: foo &&` 是 `UNRESOLVED`？
- [ ] `# executed command:` 与源文件里的 `RUN:` 行为什么可能不同？举例说明。
- [ ] 内置 shell 与 bash 的一个具体行为差异是什么？它如何导致「本机过、CI 挂」？
- [ ] worker 崩溃时，测试结果码和 lit 退出码分别是什么？为什么不是 FAIL？
- [ ] 失败后哪些现场必须保留？`LIT_PRESERVES_TMP` 保留的是哪一个目录？

## 延伸阅读

- **[`llvm/utils/lit/lit/TestRunner.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：`executeScriptInternal()`（1019）、`_runShTest()`（2165）、`parseIntegratedTestScript()`（2105）。
  - 能学到：日志行、退出码、TIMEOUT/UNRESOLVED 的真实生成位置。
  - 前提：读过本章 18.2–18.3。
- **[`llvm/utils/lit/lit/worker.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/worker.py)**
  - 读什么：`_execute_test_handle_errors()`（88–102）。
  - 能学到：异常如何变成 UNRESOLVED，`--debug` 如何改变行为。
  - 前提：知道 Python 异常与 traceback。
- **[`llvm/utils/lit/lit/formats/shtest.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/formats/shtest.py)**
  - 读什么：全文 44 行。
  - 能学到：`execute_external` / `force_execute_external` 的弃用逻辑。
  - 前提：读过本章 18.10。
- **[LLVM Testing Guide — Extra tests](https://llvm.org/docs/TestingGuide.html)**
  - 读什么：`REQUIRES`、`UNSUPPORTED`、`XFAIL` 相关小节。
  - 能学到：官方对 feature 与预期失败的语义说明；对照本章 18.6–18.7。
  - 前提：能读英文技术文档。

> 下一章进入 `TestRunner` 的解析路径：`RUN:` 行如何被切成命令树，`%dbg` 如何插入行号，`DEFINE/REDEFINE` 与续行如何处理。

→ [练习 C — 从一份失败报告定位根因](./practice-c-failure-forensics.md)
