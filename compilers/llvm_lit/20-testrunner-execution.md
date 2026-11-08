# Ch 20 — TestRunner II：脚本如何被执行

> **目标**：把 Ch 19 得到的 directive 列表真正跑起来：临时路径怎么拼、替换按什么顺序生效、内置 shell 如何把命令变成进程、`ScriptFatal`/超时/重试分别把结果改写成什么。学完你能从 `-a` 输出反推每一步执行。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。所有输出均为本机实跑；`FIXED` 与 per-test coverage 未实测，文中会标注。

---

## 20.1 一条命令的流水线

`executeShTest()` 在 `TestRunner.py:2277`。它是一条很短的流水线，但每一步都可能改变最终结果：

```text
executeShTest(test, litConfig, useExternalSh)
  │
  ├─ test.config.unsupported?           → UNSUPPORTED（不读文件）
  ├─ parseIntegratedTestScript()        → UNRESOLVED / UNSUPPORTED / script
  ├─ litConfig.noExecute?               → PASS（不建临时文件）
  ├─ getTempPaths()                     → tmpDir, tmpBase
  ├─ getDefaultSubstitutions()          → 替换表（含 %s/%t/%basename_t...）
  ├─ applySubstitutions()               → 展开后的命令字符串
  ├─ _runShTest()
  │    ├─ runOnce() × attempts
  │    │    ├─ executeScriptInternal()  ← 默认，内置 shell
  │    │    └─ executeScript()          ← 外部 bash/sh
  │    ├─ ScriptFatal                  → UNRESOLVED
  │    ├─ exitCode == 0                → PASS
  │    ├─ timeoutInfo != None          → TIMEOUT
  │    └─ 其它非零                      → FAIL
  └─ 重试命中                          → FLAKYPASS / FIXED
```

对应源码骨架（省略参数细节）：

```python
def executeShTest(test, litConfig, useExternalSh, extra_substitutions=[], preamble_commands=[]):
    if test.config.unsupported:
        return lit.Test.Result(Test.UNSUPPORTED, "Test is unsupported")

    script = list(preamble_commands)
    script = [buildPdbgCommand(f"preamble command line", ln) for ln in script]

    parsed = parseIntegratedTestScript(test, require_script=not script)
    if isinstance(parsed, lit.Test.Result):
        return parsed
    script += parsed

    if litConfig.noExecute:
        return lit.Test.Result(Test.PASS)

    tmpDir, tmpBase = getTempPaths(test)
    substitutions = list(extra_substitutions)
    substitutions += getDefaultSubstitutions(
        test, tmpDir, tmpBase,
        normalize_slashes=useExternalSh
        or litConfig.params.get("use_normalized_slashes", False),
    )
    conditions = {feature: True for feature in test.config.available_features}
    script = applySubstitutions(
        script, substitutions, conditions,
        recursion_limit=test.config.recursiveExpansionLimit,
    )
    return _runShTest(test, litConfig, useExternalSh, script, tmpBase)
```

注意 `preamble_commands`：某些 format（例如需要先 `cd` 或设置环境）可以传一组前置命令。它们也会被 `buildPdbgCommand` 包一层，所以 `-a` 里会看到 `preamble command line`。`require_script=not script` 的意思是：只有完全没有前置命令时，才强制文件里必须有 `RUN:`。

## 20.2 `getTempPaths()`：`%t` 到底是什么

函数在 `TestRunner.py:1333`：

```python
def getTempPaths(test):
    execpath = test.getExecPath()
    execdir, execbase = os.path.split(execpath)
    tmpDir = os.path.join(execdir, "Output")
    tmpBase = os.path.join(tmpDir, execbase)
    return tmpDir, tmpBase
```

`getExecPath()` 是 `test_exec_root + path_in_suite`。所以对一个位于 `tests/foo.test` 的测试：

```text
test_exec_root = /tmp/litlab/Output
execpath       = /tmp/litlab/Output/tests/foo.test
execdir        = /tmp/litlab/Output/tests
tmpDir         = /tmp/litlab/Output/tests/Output      ← 再套一层 Output
tmpBase        = /tmp/litlab/Output/tests/Output/foo.test
%t             = tmpBase + ".tmp"
               = /tmp/litlab/Output/tests/Output/foo.test.tmp
```

真实证据来自本章的重试实验：

```text
# RUN: at line 2
python3 /tmp/lit20.jQDlEm/tests/flaky.py /tmp/lit20.jQDlEm/Output/tests/Output/flaky.test.tmp.once
# executed command: python3 ... /tmp/lit20.jQDlEm/Output/tests/Output/flaky.test.tmp.once
```

路径里确实有两个 `Output`：第一个来自 `test_exec_root`，第二个是 `getTempPaths()` 追加的。这不是 bug，是布局约定；只要你知道 `%t` 在 `execdir/Output/` 下，就不会把它和源码目录混在一起。

### 20.2.1 `%t` 是文件路径，不是目录

`%t` 的值是 `tmpBase + ".tmp"`，一个**文件路径**。lit 不创建它，也不会保证它不存在。实验：

```text
# RUN: echo x > %t
# RUN: touch %t/inside
```

真实输出（截取）：

```text
# executed command: echo x
# RUN: at line 2
touch .../tdir.test.tmp/inside
# .---command stderr------------
# | touch: cannot touch '.../tdir.test.tmp/inside': Not a directory
# `-----------------------------
# error: command failed with exit status: 1
```

所以需要目录时，标准写法是先删再建：

```text
# RUN: rm -rf %t.dir && mkdir -p %t.dir
# RUN: touch %t.dir/a
```

这也是 Ch 4 里反复出现的模式。`%t` 本身适合放临时文件；`%t.dir` 才是你显式创建的目录。

### 20.2.2 `%t` 不会自动删除

`getTempPaths()` 只拼路径，不做清理。`_runShTest()` 只保证 `tmpBase` 的父目录存在（`pathlib.Path(tmpBase).parent.mkdir(parents=True, exist_ok=True)`），不会删除上次运行的 `%t`。

真实后果：本章的 flaky 测试第一次运行留下 `%t.once`，第二次单独运行时直接 `PASS`，因为标记文件还在。要复现 `FLAKYPASS`，必须保证每次从干净状态开始，或者在测试里自己清理。

| 替换 | 值 | 备注 |
|---|---|---|
| `%s` | 源文件绝对路径 | `test.getSourcePath()` |
| `%S` | 源文件目录 | `os.path.dirname(%s)` |
| `%p` | 源文件目录 | 历史别名，和 `%S` 同值 |
| `%t` | `tmpBase + ".tmp"` | 文件路径，不自动创建/删除 |
| `%T` | 不存在 | LLVM 24 已移除；原样保留，不报错、不展开 |
| `%basename_t` | `basename(tmpBase)` | 不含 `.tmp` |
| `%{t:stem}` | `basename(tmpBase)` | 与 `%basename_t` 相同 |
| `%{s:basename}` | 源文件名 | 含后缀 |
| `%{s:stem}` | 源文件名去后缀 | 不含目录 |
| `%{pathsep}` | `os.pathsep` | `:` 或 `;` |

`%T` 的坑在 Ch 0 和 Ch 6 已强调：源码里没有这个替换，写了不会报错，只会把字面量 `%T` 传给命令。需要「整个临时目录」时，用 `%t.dir` 并自己 `rm -rf && mkdir`。

## 20.3 `applySubstitutions()`：顺序、正则与条件

函数在 `TestRunner.py:1651`。它的输入是一个混合列表（`CommandDirective`、`SubstDirective`、字符串），输出是**纯字符串列表**。

```python
def processLine(ln):
    ln = substituteIfElse(escapePercents(ln))
    for a, b in substitutions:
        b = b.replace("\\", "\\\\")
        ln = _caching_re_compile(a).sub(str(b), escapePercents(ln))
    return ln.strip()

process = processLine if recursion_limit is None else processLineToFixedPoint
output = []
for directive in script:
    if isinstance(directive, SubstDirective):
        directive.adjust_substitutions(substitutions)
    else:
        line = directive.command if isinstance(directive, CommandDirective) else directive
        output.append(unescapePercents(process(line)))
return output
```

关键点：

1. **`%%` 先被藏起来**。`escapePercents()` 把 `%%` 换成 `#_MARKER_#`，替换完再换回 `%`。否则 `%%` 会被后续替换误伤。
2. **替换表顺序敏感**。`for a, b in substitutions` 按列表顺序做正则替换。`ToolSubst` 之类的长模式通常排在前面，避免 `%t` 先吃掉 `%t.foo` 的一部分。
3. **替换值是正则替换串**。`b.replace("\\", "\\\\")` 是为了让替换值里的反斜杠不被 `re.sub` 当成转义。替换模式 `a` 本身是完整正则，不是字面量。
4. **`recursion_limit`**。默认 `None` 表示**不做递归替换**：一轮 `for` 结束就停。配置了 `config.recursiveExpansionLimit` 后，`processLineToFixedPoint()` 反复处理，直到结果不再变化或达到上限；超限抛 `ValueError`。
5. **`%if` 在替换前处理**。`substituteIfElse()` 先解析 `%if cond %{...%} %else %{...%}`，条件来自 `conditions`（`executeShTest` 传入的是 `available_features` 的集合）。
6. **`%(line)` 不在 `applySubstitutions()` 里**。它在 Ch 19 的 `_handleCommand()` 阶段就已经替换成行号；到这里已经是普通数字。

真实实验文件：

```text
# DEFINE: %{x} = one
# RUN: echo %{x}
# REDEFINE: %{x} = two
# RUN: echo %{x}
# RUN: echo %(line)
# RUN: echo %(line+2)
# RUN: echo %if linux %{on-linux%} %else %{elsewhere%}
```

`-a` 的真实输出（截取命令与 stdout）：

```text
# RUN: at line 2
echo one
# | one
# RUN: at line 4
echo two
# | two
# RUN: at line 5
echo 5
# | 5
# RUN: at line 6
echo 8
# | 8
# RUN: at line 7
echo on-linux
# | on-linux
```

这里同时验证了：

- `DEFINE`/`REDEFINE` 的顺序生效，且只影响本测试的替换表。
- `%(line)` 是当前 `RUN` 所在行；`%(line+2)` 是 8。
- `%if linux` 在 `available_features` 含 `linux` 时选 `on-linux`。同一文件在 feature 为空时会输出 `elsewhere`（我们第一次运行就是这个结果）。

### 20.3.1 `DEFINE` 的副本语义

`executeShTest()` 里 `substitutions = list(extra_substitutions)` 然后 `+= getDefaultSubstitutions(...)`。每个测试都新建一份列表，`SubstDirective.adjust_substitutions()` 改的是这份列表。

结论：

- 测试 A 里的 `DEFINE:` 不会泄漏到测试 B。
- 同一个测试内，`DEFINE` 之后的所有 `RUN` 都能看到它；`DEFINE` 之前的 `RUN` 看不到。
- `REDEFINE` 必须能找到**精确同名**的已有替换（`substitutions[i][0] != self.name` 会报错），不能只靠「模式包含这个名字」。
- `DEFINE` 不允许和已有替换的模式重叠；`REDEFINE` 不允许匹配到多个已有替换。

这些错误都在 `SubstDirective.adjust_substitutions()`（`TestRunner.py:1607`）里抛出，最终由 `applySubstitutions()` 的调用方转成 `UNRESOLVED`。

## 20.4 `_runShTest()`：一次还是多次

`_runShTest()` 在 `TestRunner.py:2165`。它定义了一个内嵌 `runOnce()`，然后按 `allowed_retries + 1` 次循环：

```python
attempts = test.allowed_retries + 1
for i in range(attempts):
    res = runOnce(execdir)
    out, err, exitCode, timeoutInfo, status, test_update_output = res
    test_updates.append(test_update_output)
    if status != Test.FAIL:
        break

if i > 0 and status == Test.PASS:
    if any(test_updates):
        status = Test.FIXED
    else:
        status = Test.FLAKYPASS
```

四个判定：

| 条件 | 结果 |
|---|---|
| `exitCode == 0` | `PASS` |
| `exitCode != 0` 且 `timeoutInfo is None` | `FAIL` |
| `exitCode != 0` 且 `timeoutInfo` 非空 | `TIMEOUT` |
| `runOnce()` 抛 `ScriptFatal` | `UNRESOLVED` |

重试只对 `FAIL` 生效。`TIMEOUT`、`UNRESOLVED` 会立刻 `break`，不会再试。`FLAKYPASS` 要求「至少第二次尝试」且最终 `PASS`；如果中途有 test updater 输出，则记 `FIXED`。`runOnce()` 每次复制一份 `script`：

```python
scriptCopy = script[:]
```

原因是 per-test coverage 和 `%dbg` 展开会原地修改命令；重试时重复修改会累积污染。这个注释在源码第 2170 行写得很清楚，`tests/allow-retries.py` 里也有专门的回归测试。

### 实验 20-A：`-a` 看真实命令

```text
# RUN: echo %greet
# RUN: echo "two words"
```

`-a` 输出（截取）：

```text
# RUN: at line 1
echo hello
# executed command: echo hello
# .---command stdout------------
# | hello
# `-----------------------------
# RUN: at line 2
echo "two words"
# executed command: echo 'two words'
# | two words
```

两个细节：

- `# RUN: at line N` 来自 `%dbg`；`# executed command:` 是**替换后**的命令。
- 内置 shell 在展示时会安全引用参数：`echo "two words"` 显示成 `echo 'two words'`。这不是命令被改写，是诊断输出的引用形式。

### 实验 20-B：`ScriptFatal` → `UNRESOLVED`

```text
# RUN: echo "unterminated
```

真实输出：

```text
UNRESOLVED: ExecLab :: tests/fatal.test (1 of 1)
******************** TEST 'ExecLab :: tests/fatal.test' FAILED ********************
Exit Code: 1

Command Output (stdout):
--
# shell parser error on RUN: at line 1: echo "unterminated

--

********************
```

`executeScriptInternal()` 捕获 `ShParser` 的异常并抛 `ScriptFatal`（第 1048 行）；`runOnce()` 捕获后返回 `Test.UNRESOLVED`。注意 `Exit Code: 1` 是 lit 为了展示填的，不是真实进程退出码——进程根本没启动。

`ScriptFatal` 的语义是「没有重试价值」：引号不配对、重定向语法错误，再跑一百次也一样。

### 实验 20-C：超时 → `TIMEOUT`

```text
# RUN: sleep 2
```

用 `--timeout=1`，真实输出（截取，省略了“no output”提示行）：

```text
TIMEOUT: ExecLab :: tests/timeout.test (1 of 1)
******************** TEST 'ExecLab :: tests/timeout.test' FAILED ********************
Exit Code: -9
Timeout: Reached timeout of 1 seconds

Command Output (stdout):
--
# RUN: at line 1
sleep 2
# executed command: sleep 2
# error: command failed with exit status: -9
# error: command reached timeout: True

--
```

要点：

测试进程的 `Exit Code` 是 `-9`（被信号杀死），lit 整体退出码仍是 1。`timeoutInfo` 非空才是 `TIMEOUT` 的判定依据；只看非零退出码会把超时误判成 `FAIL`。`--timeout` 会覆盖 suite 的 `maxIndividualTestTime`，`TestingConfig.finish()` 会打印 note（Ch 22）。

### 实验 20-D：`ALLOW_RETRIES` → `FLAKYPASS`

测试文件：

```text
# ALLOW_RETRIES: 2
# RUN: python3 %S/flaky.py %t.once
```

`flaky.py` 第一次创建标记并 `exit 1`，第二次看到标记 `exit 0`。真实输出：

```text
FLAKYPASS: ExecLab :: tests/flaky.test (1 of 1, 2 of 3 attempts)

Testing Time: 0.04s

Total Discovered Tests: 1
  Passed With Retry: 1 (100.00%)
```

`(2 of 3 attempts)` 来自 `Result.attempts=2` 和 `max_allowed_attempts=3`。第二次单独运行时，因为 `%t.once` 还在，直接 `PASS`；这正好证明 `%t` 不自动清理。

`FIXED` 分支（`any(test_updates)`）需要 test updater（例如 `update_test_checks.py`）产生输出；本章**未实测**，只依据 `_runShTest()` 的源码分支说明。

### 实验 20-E：`--no-execute` 仍然解析

```text
# RUN: false
# RUN: this_command_does_not_exist
```

```bash
lit --no-execute -v tests/noexec.test
```

真实输出：

```text
PASS: ExecLab :: tests/noexec.test (1 of 1)
```

`--no-execute` 在 `parseIntegratedTestScript()` 之后、`getTempPaths()` 之前返回 `PASS`。所以它会检查指令语法（无 `RUN`、坏 `XFAIL`、未终止 `\`），但不会做 shell 解析：

```text
# RUN: echo "unterminated
```

在 `--no-execute` 下同样是 `PASS`。这是一个容易误解的点：`--no-execute` 不是「只做 lit 语法检查」，它连 shell 语法都跳过了。

## 20.5 从 AST 到进程：内置 shell 与外部 shell

`executeShTest()` 的 `useExternalSh` 参数决定走哪条路：

```text
useExternalSh=False（默认）
  executeScriptInternal()
    → ShUtil.ShParser(...).parse()       # 字符串 → AST
    → _executeShCmd(...)                 # AST → 进程/内建命令
useExternalSh=True
  executeScript()
    → 写 tmpBase + ".script"
    → bash script 或 /bin/sh script
```

`ShParser` 在 `ShUtil.py:204`，它把 `|`、`&&`、`||`、`;`、重定向、引号解析成 `Pipeline`/`Seq` 等对象。`ShellEnvironment`（`ShellEnvironment.py:55`）保存 `cwd`、`env`、`umask`、目录栈和 `ulimit`；内建命令（`cd`、`export`、`echo` 等）直接改这个对象，不经过 `fork`。

### 20.5.1 `pipefail`

`TestingConfig.fromdefaults()` 把 `pipefail` 设为 `True`。内置 shell 把它传给 `ShParser`：

```python
ShUtil.ShParser(ln, litConfig.isWindows, test.config.pipefail).parse()
```

外部 shell 则在脚本开头写：

```python
if test.config.pipefail:
    f.write("set -o pipefail;")
```

真实实验：

```text
# RUN: false | true
```

真实输出（截取）：

```text
FAIL: ExecLab :: tests/pipefail.test (1 of 1)
# executed command: false
# error: command failed with exit status: 1
# executed command: true
```

最后一个命令 `true` 成功，但整个 pipeline 仍然是失败，因为 `false` 的退出码被 `pipefail` 保留。没有 `pipefail` 时，shell 只看最后一个命令，这个测试会 `PASS`——这是 lit 与「裸 sh」最容易踩的差异之一。

### 20.5.2 外部 shell 的 bash 路径

`executeScript()` 先问 `litConfig.getBashPath()`（`LitConfig.py:174`）：

```python
self.bashPath = lit.util.which("bash", os.pathsep.join(self.path))
if self.bashPath is None:
    self.bashPath = lit.util.which("bash")
if self.bashPath is None:
    self.bashPath = ""
```

- 找到 bash：`command = [bashPath, script]`。
- 找不到：`command = ["/bin/sh", script]`。

外部 shell 还会写 `set -x`、导出 `DYLD_*`、把 `%dbg` 展开成 `echo ... >&2`。路径归一化由 `normalize_slashes=useExternalSh or params["use_normalized_slashes"]` 控制；Windows 上这是必需的，Linux 上通常看不出差别。

当前 LLVM 24 的 `ShTest` 默认 `execute_external=False`；`execute_external=True` 自 LLVM 23 起废弃，只有 `force_execute_external=True` 才能强行打开。外部 shell 分支本章未实测，结论依据源码。新代码不要再依赖外部 shell。

### 20.5.3 `%dbg` 与 per-test coverage

`%dbg(ARG)` 是 lit 插入的**伪命令**，正则 `kPdbgRegex` 在 `TestRunner.py:79`：

```python
kPdbgRegex = "%dbg\\(([^)'\"]*)\\)((?:.|\\n)*)"
```

`buildPdbgCommand()`（第 82 行）把 `ARG` 和命令拼成 `%dbg(ARG) command`。执行前：

- 内置 shell：`executeScriptInternal()` 把 `%dbg` 变成 `@echo '# ARG' && @echo 'command' && command`（第 1035–1040 行）。
- 外部 shell：`executeScript()` 变成 `{ set +x; } ... && echo ... >&2 && { set -x; } ...`，把诊断写到 stderr。
- `-a` 里看到的 `# RUN: at line N` 就是 `ARG`。

per-test coverage（`litConfig.per_test_coverage`）在 `runOnce()` 里给每条命令加：

```python
profile = f"{test_case_name}-%p-%m{coverage_index}.profraw"
command = f"export LLVM_PROFILE_FILE={profile}; {command}"
```
`%p` 是 pid，`%m` 是二进制签名，`coverage_index` 是命令序号，避免同一测试的多条命令共享 profraw。本章**未实测** coverage，只读源码；要验证可以看 `llvm/utils/lit/tests/per-test-coverage.py`。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`%t` 是临时目录」 | `%t` 是 `tmpBase + ".tmp"` 的文件路径。需要目录就 `rm -rf %t.dir && mkdir -p %t.dir`；直接 `touch %t/x` 会得到 `Not a directory` |
| 「lit 会清理 `%t`」 | 只保证父目录存在，不删除旧文件。重跑时 `%t` 及其派生文件会保留，可能让「失败一次、成功一次」的实验直接变成 `PASS` |
| 「`--no-execute` 只跳过执行，仍会检查 shell 语法」 | 它的返回点在 shell 解析之前；连引号不配对都算 `PASS`。它只保证 lit directive 解析过了 |
| 「超时也是非零退出码，所以会重试」 | 重试条件只有 `status == FAIL`；`TIMEOUT`、`UNRESOLVED` 立即停止。超时进程的 `Exit Code` 是 `-9` |
| 「`false \| true` 应该 PASS，因为最后是 true」 | 默认 `pipefail=True`，pipeline 里任一命令失败就失败；内置 shell 和外部 shell 都如此 |
| 「`-a` 里的 `# executed command` 是原始命令」 | 它是替换后的命令，而且参数被安全引用；`echo "two words"` 会显示成 `echo 'two words'` |
| 「外部 shell 会自动处理 Windows 路径」 | 只有 `useExternalSh=True` 或 `-Duse_normalized_slashes=1` 时才会归一化；内置 shell 保留原分隔符 |
| 「`DEFINE` 会污染其它测试」 | 每个测试的 `substitutions` 都是新建列表；`DEFINE` 只在本测试内、且只在其后的 `RUN` 生效 |
| 「超时能杀掉所有子孙进程」 | lit 只能杀它启动并记录在 `TimeoutHelper` 里的进程；脱离进程组/自行 daemon 化的孙进程可能存活（平台相关，未实测） |

## 重点整理

1. `executeShTest()` 的顺序是：unsupported → parse → noExecute → temp paths → substitutions → `_runShTest`；每一步的短路点决定了哪些错误根本不会被执行到。
2. `%t` 是 `execdir/Output/<basename>.tmp` 的文件路径，`%T` 不存在；`%t` 不自动创建、不自动删除，重跑会看到上次的残留。
3. `applySubstitutions()` 按替换表顺序做正则替换，先藏 `%%`，再处理 `%if`，最后 strip；默认不递归，除非配置 `recursiveExpansionLimit`。
4. `%(line)` 在解析阶段展开，`%if` 在执行前展开，两者不在同一个函数里。
5. `_runShTest()` 用 `exitCode + timeoutInfo` 判定 `PASS/FAIL/TIMEOUT`；`ScriptFatal` 单独映射到 `UNRESOLVED`。
6. 重试只针对 `FAIL`；第二次成功且没有 updater 输出是 `FLAKYPASS`，有 updater 输出是 `FIXED`。
7. 默认走内置 shell：`ShParser` 生成 AST，`ShellEnvironment` 维护 cwd/env/umask；外部 shell 只在显式 force 时使用。
8. `pipefail` 默认开启，`false | true` 是失败；`-a` 的 `# executed command` 是替换后的命令，不是原文。

## 自我检核

- [ ] 不看源码，写出 `executeShTest()` 的七步流水线，并指出哪一步会把「引号不配对」变成 `UNRESOLVED`。
- [ ] 给一个测试位于 `tests/sub/foo.test`、`test_exec_root=/build/test`，写出 `%t` 的完整路径；解释为什么路径里可能出现两个 `Output`。
- [ ] 解释 `%t` 不自动删除会如何影响「第一次失败、第二次成功」的重试实验；你会怎么让实验可重复？
- [ ] 比较 `applySubstitutions()` 默认模式与 `recursiveExpansionLimit` 模式的行为差异，并说明 `%%` 为什么需要先转义。
- [ ] 一个 pipeline `false | true` 在 lit 里失败，在裸 `sh` 里可能成功；说出两个原因（`pipefail`、内置 shell 的 pipeline 判定）。
- [ ] `--no-execute` 对一个含未配对引号的文件返回什么？为什么它和含未终止 `\` 的文件结果不同？

## 延伸阅读

- **`llvm/utils/lit/lit/TestRunner.py:1019-1180`**
  - 读哪里：`executeScriptInternal()` 与 `executeScript()`。
  - 能学到什么：内置 shell 与外部 shell 的完整差异、`%dbg` 展开、`pipefail` 写入、`ScriptFatal` 的抛出点。
  - 前提知识：本章 20.5；Ch 7–8 的 shell 基础。
- **`llvm/utils/lit/lit/ShUtil.py:204-330` 与 `lit/ShellEnvironment.py:55-110`**
  - 读哪里：`ShParser`、`Pipeline`、`ShellEnvironment`。
  - 能学到什么：字符串如何变成 AST、内建命令如何改 cwd/env、重定向如何被处理。
  - 前提知识：会读递归下降 parser；不要求能手写。
- **`llvm/utils/lit/tests/allow-retries.py` 与 `tests/Inputs/allow-retries/`**
  - 读哪里：`ALLOW_RETRIES` 的成功、失败、重复声明、非整数四类用例。
  - 能学到什么：官方如何测试重试与 `FLAKYPASS`；可以直接改造成自己的回归测试。
  - 前提知识：本章 20.4。
- **`llvm/utils/lit/tests/Inputs/shtest-shell/` 与 `tests/Inputs/shtest-timeout/`**
  - 读哪里：管道、重定向、引号、超时的边界用例。
  - 能学到什么：内置 shell 到底支持哪些语法；哪些写法会触发 `ScriptFatal`。
  - 前提知识：Ch 7–8。

下一章我们不再看「怎么跑」，而是看「跑完如何记账」：`PASS` 与 `XFAIL` 怎么互转、`XPASS` 为什么是失败、`EXCLUDED/UNSUPPORTED/SKIPPED` 分别在哪一步产生。

→ [Ch 21 Test/Result 模型：状态码的语义](./21-result-model.md)
