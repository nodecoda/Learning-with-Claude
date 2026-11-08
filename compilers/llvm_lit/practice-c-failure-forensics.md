# 练习 C — 从一份失败报告定位根因

> **目标**：用 5 份真实失败报告训练同一套动作：先判断实际执行了什么，再定位失败阶段，给出根因与最小修复。做完你应该能在不重跑的情况下，仅凭日志复述问题和修复方案。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`。所有报告都在本机 `/tmp/litlab-a1`、`/tmp/litrace-b1` 中真实构造并运行，Ubuntu 22.04 x86_64、Python 3.10.12。日志为节选，但关键行未改动。

---

## C.0 实验 suite 与做题方法

公共 `lit.cfg.py`：

```python
import os
import lit.formats

config.name = "LitLab"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.substitutions.append(("%python", "python3"))

lit_config.parallelism_groups["serial"] = 1
```

测试放在 `tests/` 下，lit 入口：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py <args> tests
```

每份报告都按同一套问题做：

1. **实际执行了什么？** 看 `# executed command:`，不是源文件里的 `RUN:`。
2. **失败在哪个阶段？** 发现/配置/选择/解析/执行/判定。
3. **根因是什么？** 命令、输入、断言、环境、并发、结果码。
4. **最小修复是什么？** 改一行、加一个参数、还是改架构。

先自己写答案，再展开 `<details>` 对答案。

---

## C.1 报告一：退出码 127

### 测试文件

```text
# RUN: definitely-not-a-command --version
```

### 运行

```bash
cd /tmp/litlab-a1
python3 .../lit.py -v tests/fail/f02-missing-cmd.test; echo $?
```

### 日志

```
FAIL: LitLab :: tests/fail/f02-missing-cmd.test (1 of 1)
******************** TEST 'LitLab :: tests/fail/f02-missing-cmd.test' FAILED ********************
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

--

********************
```

退出码 `1`（有测试失败）。

### 问题

- 实际执行了什么？
- 失败在哪个阶段？
- 根因是什么？
- 最小修复是什么？

<details>
<summary>参考解答</summary>

- 实际执行：`definitely-not-a-command --version`。没有替换错位，也没有引号问题。
- 阶段：执行阶段，命令启动失败。
- 根因：命令不在 `PATH`。127 是 shell 的「command not found」，不是被测程序返回的业务错误。
- 最小修复：安装/构建该工具，或把工具目录加进 `--path`；如果是替换写错，修 `config.substitutions`。

补充：FileCheck 也会以同样方式报 127。真实对照：

```
# | 'FileCheck': command not found
# error: command failed with exit status: 127
```

加 `--path /home/ubuntu/llvm/builds/pr-lane/bin` 后才进入真正的 FileCheck 断言失败。**127 先查环境，不要查代码。**

</details>

---

## C.2 报告二：FileCheck 不匹配

### 测试文件

```text
# RUN: echo "actual value" > %t.txt
# RUN: FileCheck %s --input-file=%t.txt
# CHECK: expected value
```

### 运行

```bash
cd /tmp/litlab-a1
python3 .../lit.py -v --path /home/ubuntu/llvm/builds/pr-lane/bin \
  tests/fail/f03-filecheck.test; echo $?
```

### 日志

```
FAIL: LitLab :: tests/fail/f03-filecheck.test (1 of 1)
Exit Code: 1

Command Output (stdout):
--
# RUN: at line 1
echo "actual value" > .../f03-filecheck.test.tmp.txt
# executed command: echo 'actual value'
# RUN: at line 2
FileCheck /tmp/litlab-a1/tests/fail/f03-filecheck.test --input-file=.../f03-filecheck.test.tmp.txt
# executed command: FileCheck /tmp/litlab-a1/tests/fail/f03-filecheck.test --input-file=.../f03-filecheck.test.tmp.txt
# .---command stderr------------
# | /tmp/litlab-a1/tests/fail/f03-filecheck.test:3:10: error: CHECK: expected string not found in input
# | # CHECK: expected value
# |          ^
# | /tmp/litlab-a1/Output/tests/fail/Output/f03-filecheck.test.tmp.txt:1:1: note: scanning from here
# | actual value
# | ^
# | ...
# | error: no match found in search range
# `-----------------------------
# error: command failed with exit status: 1

--

********************
```

### 问题

- 实际执行了什么？
- 失败在哪个阶段？
- 根因是什么？
- 最小修复是什么？

<details>
<summary>参考解答</summary>

- 实际执行：`echo 'actual value'` 写 `%t.txt`，然后 `FileCheck` 读取同一个文件。
- 阶段：执行阶段；失败的是 `FileCheck` 这个被测工具，不是 lit。
- 根因：输入是 `actual value`，`CHECK` 要 `expected value`。断言与程序输出不一致。
- 最小修复：改测试输入或期望值，或修被测程序；不要改 lit。

日志里三个位置最关键：

1. `f03-filecheck.test:3:10: error: CHECK: expected string not found in input`：哪一行 CHECK 失败；
2. `# CHECK: expected value`：期望是什么；
3. `Input was: ... actual value`：实际是什么。

`Exit Code: 1` 是 FileCheck 的退出码，不是 lit 的。lit 只是把它映射成 `FAIL`。

</details>

---

## C.3 报告三：`REQUIRES` 语法错误导致 UNRESOLVED

### 测试文件

```text
# REQUIRES: x86_64 &&
# RUN: true
```

### 运行

```bash
cd /tmp/litlab-a1
python3 .../lit.py -v tests/fail/f08-requires-typo.test; echo $?
```

### 日志

```
UNRESOLVED: LitLab :: tests/fail/f08-requires-typo.test (1 of 1)
******************** TEST 'LitLab :: tests/fail/f08-requires-typo.test' FAILED ********************
expected: '!', '(', '{{', or identifier
have: <end of expression>
in expression: 'x86_64 &&'
in REQUIRES: directive on test line 1
********************

Unresolved Tests (1):
  LitLab :: tests/fail/f08-requires-typo.test

Testing Time: 0.01s

Total Discovered Tests: 1
  Unresolved: 1 (100.00%)
```

退出码 `1`。

### 问题

- 实际执行了什么？
- 失败在哪个阶段？
- 根因是什么？
- 最小修复是什么？
- 如果写成 `REQUIRES: x86_64` 但本机没有 `x86_64` feature，会是什么结果？

<details>
<summary>参考解答</summary>

- 实际执行：**没有执行任何命令**。`RUN: true` 根本没跑到。
- 阶段：解析阶段，`parseIntegratedTestScript()` 的 `_parseKeywords()` 抛 `ValueError`，被第 2122–2123 行转成 `UNRESOLVED`。
- 根因：`BooleanExpression` 无法解析 `x86_64 &&`；`&&` 后面缺右操作数。
- 最小修复：补全表达式（例如 `x86_64 && linux`），或删掉 `&&`。

最后一问：`REQUIRES: x86_64` 且 feature 不存在时，`getMissingRequiredFeatures()` 返回缺失项，`TestRunner.py` 第 2135–2142 行返回 `UNSUPPORTED`。**feature 缺失是 UNSUPPORTED，语法错误才是 UNRESOLVED。** 这两个结果码不能混。

</details>

---

## C.4 报告四：XFAIL 变 XPASS

### 测试文件

```text
# XFAIL: *
# RUN: true
```

### 运行

```bash
cd /tmp/litlab-a1
python3 .../lit.py -v tests/fail/f04-xpass.test; echo $?
```

### 日志

```
XPASS: LitLab :: tests/fail/f04-xpass.test (1 of 1)
******************** TEST 'LitLab :: tests/fail/f04-xpass.test' FAILED ********************
Exit Code: 0

Command Output (stdout):
--
# RUN: at line 2
true
# executed command: true
# note: command had no output on stdout or stderr

--

********************

Unexpectedly Passed Tests (1):
  LitLab :: tests/fail/f04-xpass.test

Testing Time: 0.01s

Total Discovered Tests: 1
  Unexpectedly Passed: 1 (100.00%)
```

退出码 `1`。

### 问题

- 实际执行了什么？
- 失败在哪个阶段？
- 根因是什么？
- 最小修复是什么？
- 为什么 `Exit Code: 0` 还会失败？

<details>
<summary>参考解答</summary>

- 实际执行：`true`，退出码 0。
- 阶段：结果判定阶段，不是执行阶段。
- 根因：`XFAIL: *` 声明「这个测试应该失败」，但它通过了；`Test.setResult()` 第 301–313 行把 `PASS` 改写成 `XPASS`。
- 最小修复：bug 已修复就删掉 `XFAIL`；只在特定配置下失败就把 `*` 收窄成具体 feature 表达式。
- `Exit Code: 0` 是**命令**的退出码；结果码由 lit 在 `setResult()` 里根据 `XFAIL` 改写。`XPASS` 是 failure（`ResultCode("XPASS", "Unexpectedly Passed", True)`），所以 lit 退出 1。

这类日志最容易被误判，因为没有任何 `# error:`。看到「全绿但结果码红」，先找 `XFAIL`/`XPASS`。

</details>

---

## C.5 报告五：并行共享文件导致 flaky

### suite 配置

```python
# lit.cfg.py（节选）
config.name = "RaceLab"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_exec_root = os.path.join(config.test_source_root, "Output")
```

4 个 writer、4 个 reader，共享 `%S/shared.txt`：

```text
# w1.test（w2/w3/w4 相同）
# RUN: %python -c "open(r'%S/shared.txt','w').close(); import time; time.sleep(0.15); open(r'%S/shared.txt','a').write('x\n'*1000)"

# r1.test（r2/r3/r4 相同）
# RUN: %python -c "import pathlib; lines=pathlib.Path(r'%S/shared.txt').read_text().splitlines(); assert len(lines)==1000, len(lines)"
```

### 运行

```bash
cd /tmp/litrace-b1
python3 .../lit.py -j 8 -v tests/race; echo $?
```

### 日志（节选，4 个 reader 都类似）

```
FAIL: RaceLab :: tests/race/r1.test (1 of 8)
Exit Code: 1

Command Output (stdout):
--
# RUN: at line 1
python3 -c "import pathlib; lines=pathlib.Path(r'/tmp/litrace-b1/tests/race/shared.txt').read_text().splitlines(); assert len(lines)==1000, len(lines)"
# executed command: python3 -c '...'
# .---command stderr------------
# | Traceback (most recent call last):
# |   File "<string>", line 1, in <module>
# | AssertionError: 0
# `-----------------------------
# error: command failed with exit status: 1

--

********************
...
Failed Tests (4):
  RaceLab :: tests/race/r1.test
  RaceLab :: tests/race/r2.test
  RaceLab :: tests/race/r3.test
  RaceLab :: tests/race/r4.test

Testing Time: 0.34s

Total Discovered Tests: 8
  Passed: 4 (50.00%)
  Failed: 4 (50.00%)
```

同一 suite 串行跑：

```bash
python3 .../lit.py -j 1 --order=lexical tests/race; echo $?
```

真实输出：

```
-- Testing: 8 tests, 1 workers --
PASS: RaceLab :: tests/race/r1.test (1 of 8)
PASS: RaceLab :: tests/race/r2.test (2 of 8)
PASS: RaceLab :: tests/race/r3.test (3 of 8)
PASS: RaceLab :: tests/race/r4.test (4 of 8)
PASS: RaceLab :: tests/race/w1.test (5 of 8)
...
  Passed: 8 (100.00%)
```

### 问题

- 实际执行了什么？
- 失败在哪个阶段？
- 根因是什么？
- 最小修复是什么？
- 为什么 `-j 8` 失败、`-j 1` 通过？

<details>
<summary>参考解答</summary>

- 实际执行：reader 读 `%S/shared.txt`，测试文件本身的路径；writer 先 truncate 再 sleep 0.15 秒后写入。
- 阶段：执行阶段；失败在 reader 的 Python 断言，不在 lit。
- 根因：测试之间共享 `%S/shared.txt`。`-j 8` 时 reader 在 writer truncate 之后、写完之前的窗口读到空文件，`AssertionError: 0`。
- 最小修复：
  1. 每个测试用自己的 `%t`，不共享源目录文件；
  2. 或把 writer/reader 放进同一个 `parallelism_group`，上限 1。
- `-j 1` 时执行顺序是 `r1..r4` 然后 `w1..w4`（lexical），reader 先读完整的初始文件，所以通过。`-j 8` 时顺序不再保证，竞争窗口出现。

最小修复的配置形式：

```python
# tests/race/lit.local.cfg.py
config.parallelism_group = "serial"
```

```python
# lit.cfg.py
lit_config.parallelism_groups["serial"] = 1
```

但更好的修复是消除共享：`shared.txt` 改为每个测试的 `%t.shared`。`parallelism_group` 只是把 flaky 变成慢，不是根治。

</details>

---

## C.6 诊断清单（贴在显示器旁）

拿到任何失败报告，按顺序打勾：

- [ ] **退出码**：0 / 1 / 2？2 先看 lit 自身错误（路径、配置、filter、worker crash）。
- [ ] **结果码**：`FAIL` / `TIMEOUT` / `UNRESOLVED` / `XPASS` / `SKIPPED` / `UNSUPPORTED` / `EXCLUDED`？
- [ ] **实际命令**：`# executed command:` 是什么？里面的 `%t`/工具路径是否合理？
- [ ] **源位置**：`# RUN: at line N` 对应文件第几行？读那一行和上下文。
- [ ] **最后一条 `# error`**：退出码是多少？127=命令没找到，其他=被测程序/断言，timeout=超时。
- [ ] **断言输出**：FileCheck 的 `error:` 在哪一行？expected vs actual 各是什么？
- [ ] **解析错误**：有没有 `Test has no 'RUN:' line`、`shell parser error`、`in REQUIRES: directive`？
- [ ] **预期失败**：有没有 `XFAIL`/`XPASS`？`Exit Code: 0` 但结果红，优先查这里。
- [ ] **并发**：`-j 1` 是否还失败？测试是否共享文件/端口/全局状态？
- [ ] **现场**：`%t`、`Output/`、`# executed command` 里的路径是否还在？先复制再清理。
- [ ] **环境**：`--path`、`PATH`、feature、shell（内置/外部）、`ulimit -u` 是否与 CI 一致？
- [ ] **修复验证**：最小修复后，用同一条命令重跑；不要用 `--ignore-fail` 掩盖。

### 四类根因的快速映射

| 日志特征 | 根因 | 最小修复 |
|---|---|---|
| `command not found` + 127 | 工具不在 PATH / 替换错 | 加 `--path`，修 substitution |
| `FileCheck ... error: CHECK` | 断言与输出不匹配 | 改输入/期望，或修被测程序 |
| `Test has no 'RUN:'` / `shell parser error` / `in REQUIRES:` | 解析失败 | 补 RUN/引号/布尔表达式 |
| `XPASS` + `Exit Code: 0` | XFAIL 条件过宽 | 删 XFAIL 或收窄条件 |
| 只在 `-j > 1` 失败 | 共享文件/资源竞争 | 改 `%t` 或 `parallelism_group` |
| `worker process crashed` | segfault/OOM/kill | 复现崩溃，查 core/ASan/ulimit |

---

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「127 是被测程序返回的错误」 | 127 是 shell 的 command not found；先查 PATH、`--path`、工具替换。 |
| 「FileCheck 报错说明 lit 有 bug」 | FileCheck 是被测工具；lit 只把它的退出码映射成 FAIL。 |
| 「UNRESOLVED 就是测试失败」 | UNRESOLVED 是 lit 无法判定：没有 RUN、解析错误、worker 异常、配置错误。 |
| 「XPASS 是成功」 | XPASS 是 failure，退出码 1；`Exit Code: 0` 只说明命令成功。 |
| 「并行失败就加 `-j 1` 绕过」 | `-j 1` 通过只能证明存在顺序依赖；根因通常是共享文件/端口/全局状态。 |
| 「失败后先删 `Output/` 清理环境」 | 那是现场；先复制 `%t` 和日志，再清理。 |

## 重点整理

- 先看退出码，再看结果码，再看 `# executed command`，最后看 `# error`。
- `FAIL` 是脚本退出码非 0；`TIMEOUT` 是单测试超时；`UNRESOLVED` 是 lit 无法判定；`XPASS` 是预期失败却通过。
- 127、FileCheck `error:`、shell parser error、`REQUIRES` 语法错分别属于环境、断言、解析、解析四类问题。
- `REQUIRES: foo`（feature 缺失）→ `UNSUPPORTED`；`REQUIRES: foo &&`（语法错）→ `UNRESOLVED`。
- `-j 1` 通过、`-j 8` 失败，优先怀疑共享文件/资源竞争，而不是被测程序。
- 最小修复必须能解释「为什么原来的日志会变成现在这样」，否则只是碰巧不报错。

## 自我检核

不看笔记，试着回答：

- [ ] 给你一份只有 `FAIL` 和 `Exit Code: 127` 的日志，你的前三个检查动作是什么？
- [ ] 为什么 FileCheck 的 `error:` 不应该被归类为「lit 错误」？
- [ ] `UNRESOLVED` 与 `FAIL` 的执行阶段差别是什么？各举一个例子。
- [ ] `XPASS` 的日志里为什么可能完全没有 `# error:`？
- [ ] 一份报告在 `-j 8` 下 4 个 reader 失败、`-j 1` 下全过，你会怎么证明根因是共享文件？
- [ ] 失败现场里哪三类东西必须保留？为什么 `LIT_PRESERVES_TMP` 不能替代 `Output/`？

## 延伸阅读

- **[`llvm/utils/lit/lit/TestRunner.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：`executeScriptInternal()`、`_runShTest()`、`parseIntegratedTestScript()`。
  - 能学到：日志行与结果码的真实生成位置，支撑本练习的每一个判断。
  - 前提：读完 Ch 18 的 18.2–18.3。
- **[`llvm/utils/lit/lit/run.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/run.py)**
  - 读什么：`Run.execute()`、`_dispatch_and_wait()`、`_increase_process_limit()`。
  - 能学到：并行、超时、失败上限、worker 崩溃如何影响结果状态。
  - 前提：读过 Ch 15。
- **[`llvm/utils/lit/tests/Inputs/`](https://github.com/llvm/llvm-project/tree/main/llvm/utils/lit/tests/Inputs)**
  - 读什么：`shtest-timeout/`、`allow-retries/`、`max-retries-per-test/`、`parallelism-groups/`。
  - 能学到：官方构造失败与边界条件的最小 suite；可以照着改成本练习的额外题目。
  - 前提：能读 `lit.cfg` 与 `RUN:` 行。

## C.7 下一步

这 5 份报告覆盖了执行、解析、判定、并发四类问题。下一章开始读 `TestRunner` 的解析路径：`RUN:` 行如何被切成命令树，`%dbg` 如何插入行号，`DEFINE/REDEFINE` 与续行如何处理。

→ [Ch 19 TestRunner I：从注释到脚本](./19-testrunner-parsing.md)
