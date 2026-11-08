# Ch 21 — Test/Result 模型：状态码的语义

> **目标**：把 `PASS/FAIL/XFAIL/XPASS/UNSUPPORTED/EXCLUDED/SKIPPED/TIMEOUT/UNRESOLVED/FLAKYPASS/FIXED` 放回它们产生的阶段，读懂 `setResult()` 的唯一一次改写，并能从一个混合 suite 的摘要反推每个测试发生了什么。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。本章所有摘要与退出码均为本机实跑；`FIXED` 依赖 test updater，未实测，文中标注。

---

## 21.1 `ResultCode`：不是字符串，是单例对象

定义在 `llvm/utils/lit/lit/Test.py:12`。每个状态是一个 `ResultCode` 实例，构造参数只有三个：

```python
class ResultCode:
    def __init__(self, name, label, isFailure):
        self.name = name          # 机器名：PASS/FAIL/...
        self.label = label        # 人类标签：Passed/Failed/...
        self.isFailure = isFailure
```

`__new__` 用 `_instances` 做单例池（第 26 行），所以 `lit.Test.PASS is lit.Test.PASS` 永远为真；pickle 跨进程后也能还原成同一个对象（`__getnewargs__`）。所有状态按声明顺序登记在 `ResultCode._all_codes`，摘要的列顺序就来自这里。

```python
# Successes
EXCLUDED   = ResultCode("EXCLUDED",   "Excluded",           False)
SKIPPED    = ResultCode("SKIPPED",    "Skipped",            False)
UNSUPPORTED= ResultCode("UNSUPPORTED","Unsupported",        False)
PASS       = ResultCode("PASS",       "Passed",             False)
FLAKYPASS  = ResultCode("FLAKYPASS",  "Passed With Retry",  False)
FIXED      = ResultCode("FIXED",      "Passed After Update",False)
XFAIL      = ResultCode("XFAIL",      "Expectedly Failed",  False)
# Failures
UNRESOLVED = ResultCode("UNRESOLVED", "Unresolved",         True)
TIMEOUT    = ResultCode("TIMEOUT",    "Timed Out",          True)
FAIL       = ResultCode("FAIL",       "Failed",             True)
XPASS      = ResultCode("XPASS",      "Unexpectedly Passed",True)
```

（第 45–57 行，本章为对齐做了排版。）

关键结论：

- **非失败**：`PASS/XFAIL/UNSUPPORTED/EXCLUDED/SKIPPED/FLAKYPASS/FIXED`。
- **失败**：`FAIL/XPASS/TIMEOUT/UNRESOLVED`。
- `isFailure` 只影响「lit 是否以 1 退出」和报告里的失败分组，不决定摘要是否显示该行；摘要会显示所有出现过的 code。
- `XPASS` 的 `isFailure` 是 `True`。这不是笔误：测试声明「我预期失败」，结果却通过了，说明 `XFAIL` 条件过期或修复没有被记录，必须让人看到。

真实对照：本章的混合 suite 里，`XPASS` 被单独列在 `Unexpectedly Passed Tests`，并且进程退出码是 1。

## 21.2 `Test.setResult()`：唯一一次状态改写

`setResult()` 在 `Test.py:297`。它在 worker 进程里、`execute()` 返回之后调用：

```python
def setResult(self, result):
    assert self.result is None, "result already set"
    assert isinstance(result, Result), "unexpected result type"
    try:
        expected_to_fail = self.isExpectedToFail()
    except ValueError as err:
        result.code = UNRESOLVED
        result.output = str(err)
    else:
        if expected_to_fail:
            if result.code is PASS:
                result.code = XPASS
            elif result.code is FAIL:
                result.code = XFAIL
    self.result = result
```

这张表必须背下来：

| 原始结果 | `expected_to_fail` | 最终结果 |
|---|---|---|
| `PASS` | `False` | `PASS` |
| `PASS` | `True` | `XPASS` |
| `FAIL` | `False` | `FAIL` |
| `FAIL` | `True` | `XFAIL` |
| `TIMEOUT` | 任意 | `TIMEOUT`（不改写） |
| `UNRESOLVED` | 任意 | `UNRESOLVED`（不改写） |
| `UNSUPPORTED` | 任意 | `UNSUPPORTED`（不改写） |
| `EXCLUDED` | 任意 | `EXCLUDED`（不改写） |
| `SKIPPED` | 任意 | `SKIPPED`（不改写） |

注意 `is` 比较：只有**恰好** `PASS`/`FAIL` 才改写。这解释了一个常见疑问：为什么 `XFAIL: *` 的测试如果连 `RUN:` 都没有，结果是 `UNRESOLVED` 而不是 `XFAIL`？因为 `parseIntegratedTestScript()` 在 `setResult()` 之前就返回了 `UNRESOLVED`，而 `UNRESOLVED` 不在改写表里。

真实实验：

```text
# XFAIL: *
# no run line
```

```text
UNRESOLVED: ResultLab :: tests/no-run-xfail.test
```

如果 `isExpectedToFail()` 自己抛 `ValueError`（`XFAIL:` 表达式语法错误），`setResult()` 把结果改成 `UNRESOLVED`，输出是错误文本。这是「测试声明的语法错误」与「测试执行失败」的区分：前者连「预期失败」都无法判断。

### 21.2.1 `xfail_not` 与 `exclude_xfail`

- `test.xfail_not = True`：`isExpectedToFail()` 直接返回 `False`（`Test.py:350`）。它来自 `--xfail-not`，用于临时取消某个测试的 XFAIL 标记。
- `test.exclude_xfail = True`：在 `parseIntegratedTestScript()` 里，如果 `isExpectedToFail()` 为真，直接返回 `EXCLUDED`（`TestRunner.py:2128`）。它来自 `--exclude-xfail`，用于「跳过所有预期失败」的快速验证。

两者方向相反：`xfail_not` 让 XFAIL 失效，`exclude_xfail` 让 XFAIL 变成「不跑」。

## 21.3 `Result` 的字段：结果不只是一个码

`Result` 在 `Test.py:153`：

```python
class Result:
    def __init__(self, code, output="", elapsed=None,
                 attempts=1, max_allowed_attempts=None,
                 test_updater_outputs=[]):
        self.code = code
        self.output = output
        self.elapsed = elapsed
        self.start = None
        self.pid = None
        self.metrics = {}
        self.microResults = {}
        self.attempts = attempts
        self.max_allowed_attempts = max_allowed_attempts
        self.test_updater_outputs = test_updater_outputs
```

| 字段 | 谁填 | 用途 |
|---|---|---|
| `code` | 各 format / `setResult()` | 最终状态 |
| `output` | format | 失败时打印的文本；`-v`/`-a` 的来源 |
| `elapsed` | `worker._execute()` | 墙钟时间，写入 `.lit_test_times.txt` |
| `start` | `worker._execute()` | 起始时间；GoogleTest 的 shard 结果会按它累加 |
| `pid` | `worker._execute()` | 执行该测试的 worker 进程号 |
| `attempts` | `_runShTest()` | 实际尝试次数 |
| `max_allowed_attempts` | `_runShTest()` | 允许的总次数（`allowed_retries + 1`） |
| `metrics` | `addMetric()` | 结构化指标，JSON 报告用 |
| `microResults` | `addMicroResult()` | 子测试结果（如 gtest 的单个 case） |
| `test_updater_outputs` | `_runShTest()` | 每次尝试的 updater 输出；用于 `FIXED` 判定 |

`metrics` 与 `microResults` 不是所有 format 都用。`Result.addMetric()` 同名重复会抛 `ValueError`；`addMicroResult()` 要求值是另一个 `Result`。GoogleTest 的 `post_process_shard_results()` 把 shard JSON 拆成多个 `Result`，但**没有**自动塞进 `microResults`；它直接生成独立的 `lit.Test.Test` 对象（见 Ch 23）。

## 21.4 `UNSUPPORTED`、`EXCLUDED`、`SKIPPED`：三个「没跑」的区别

这三个状态都不算失败，但产生阶段完全不同：

```text
发现/选择阶段
  ├─ --filter / --filter-out / shard 外        → EXCLUDED（mark_excluded）
  └─ --exclude-xfail 且 XFAIL 命中              → EXCLUDED（parse）
解析/执行前
  ├─ config.unsupported                         → UNSUPPORTED
  ├─ REQUIRES 缺 feature                        → UNSUPPORTED
  ├─ UNSUPPORTED 命中 feature                   → UNSUPPORTED
  └─ limit_to_features 不满足                   → UNSUPPORTED
运行阶段
  ├─ gtest 报告 SKIPPED                         → SKIPPED（post_process）
  └─ --max-failures / --max-time 中止后未执行    → SKIPPED（run.execute finally）
```

### 21.4.1 `EXCLUDED`

两个来源：

1. **选择阶段**：`main.py` 先得到 `discovered_tests`，再按 `--filter`、`--filter-out`、shard、`--max-tests` 得到 `selected_tests`，然后 `mark_excluded(discovered_tests, selected_tests)` 把差集标成 `EXCLUDED`（`main.py:254`）。
2. **解析阶段**：`--exclude-xfail` 命中 `XFAIL` 时，`parseIntegratedTestScript()` 直接返回 `EXCLUDED`。

真实实验（8 个测试，只选 `pass.test`）：

```text
-- Testing: 2 of 8 tests, 2 workers --
XPASS: ResultLab :: tests/xpass.test (1 of 2)
PASS: ResultLab :: tests/pass.test (2 of 2)

Total Discovered Tests: 8
  Excluded           : 6 (75.00%)
  Passed             : 1 (12.50%)
  Unexpectedly Passed: 1 (12.50%)
```

注意 `--filter='pass\.test'` 也匹配了 `xpass.test`；这是正则子串匹配，不是精确文件名。`Total Discovered Tests` 仍是 8，分母不变。

### 21.4.2 `UNSUPPORTED`

来源最多，但都发生在执行之前：

- suite 配置 `config.unsupported = True`（整个 suite 不支持）。
- `REQUIRES:` 的条件在当前 `available_features` 下不成立。
- `UNSUPPORTED:` 的条件成立（语义是「这些 feature 下不要跑」）。
- `limit_to_features` 不满足（见 Ch 19）。

真实实验：

```text
# UNSUPPORTED: linux
# RUN: echo never
```

```text
UNSUPPORTED: ResultLab :: tests/unsupported.test
```

`UNSUPPORTED` 不算失败，但它会拉低「通过率」：摘要里的百分比分母是 `Total Discovered Tests`，不是「实际执行数」。所以一个 PR 如果让大量测试变成 UNSUPPORTED，即使退出码是 0，覆盖率也会下降。这一点在 Ch 13 讲 feature 时反复出现。

### 21.4.3 `SKIPPED`

两个来源：

1. **GoogleTest**：gtest 报告 `SKIPPED` 的 case，`post_process_shard_results()` 映射成 `lit.Test.SKIPPED`（`googletest.py:346`）。
2. **运行中止**：`Run.execute()` 的 `finally` 会把所有 `result is None` 的测试标成 `SKIPPED`（`run.py:93`）。触发条件包括 Ctrl-C、`--max-failures`、`--max-time`、worker 崩溃。

真实实验（1 个失败 + 2 个慢测试，`--workers=1 --max-failures=1`）：

```text
-- Testing: 3 tests, 1 workers --
FAIL: SkipLab :: t/a_fail.test (1 of 3)
warning: reached maximum number of test failures, skipping remaining tests

Total Discovered Tests: 3
  Skipped: 2 (66.67%)
  Failed : 1 (33.33%)
```

`SKIPPED` 与 `EXCLUDED` 都不算失败，但语义不同：`EXCLUDED` 是「用户选择不跑」，`SKIPPED` 是「本该跑但没跑成」。xUnit 把 `EXCLUDED/SKIPPED/UNSUPPORTED` 都放进 `<skipped>`，但 skip reason 不同（`reports.py:118`、`reports.py:184-190`）。

## 21.5 `FLAKYPASS` 与 `FIXED`

两者都由 `_runShTest()` 在重试后产生（`TestRunner.py:2233`）：

```python
if i > 0 and status == Test.PASS:
    if any(test_updates):
        status = Test.FIXED
    else:
        status = Test.FLAKYPASS
```

- `i > 0`：至少跑了第二次。
- `status == PASS`：最终通过。
- `any(test_updates)`：某次尝试产生了 test updater 输出（例如 `update_test_checks.py` 改写了 `CHECK` 行），记 `FIXED`。
- 否则记 `FLAKYPASS`。

真实 `FLAKYPASS` 输出：

```text
FLAKYPASS: ExecLab :: tests/flaky.test (1 of 1, 2 of 3 attempts)
  Passed With Retry: 1 (100.00%)
```

`FIXED` 本章**未实测**：需要一个会产生 `test_updater_outputs` 的 format/updater。源码分支清楚，但没有真实输出就不假装有。

## 21.6 一个 suite，所有状态：完整摘要

测试目录（`available_features = {"linux"}`）：

| 文件 | 内容 | 预期 |
|---|---|---|
| `pass.test` | `RUN: echo ok` | `PASS` |
| `xfail.test` | `XFAIL: linux` + `RUN: false` | `XFAIL` |
| `xpass.test` | `XFAIL: linux` + `RUN: echo ...` | `XPASS` |
| `unsupported.test` | `UNSUPPORTED: linux` | `UNSUPPORTED` |
| `requires-missing.test` | `REQUIRES: windows` | `UNSUPPORTED` |
| `timeout.test` | `RUN: sleep 2` + `--timeout=1` | `TIMEOUT` |
| `unresolved.test` | `RUN: echo "unterminated` | `UNRESOLVED` |
| `no-run-xfail.test` | `XFAIL: *` 且无 `RUN:` | `UNRESOLVED` |

为了同时看到 `EXCLUDED`，用 `--filter-out='requires-missing'` 排掉一个：

```bash
lit --timeout=1 --filter-out='requires-missing' tests
```

真实输出：

```text
-- Testing: 7 of 8 tests, 7 workers --
UNSUPPORTED: ResultLab :: tests/unsupported.test (1 of 7)
XPASS: ResultLab :: tests/xpass.test (2 of 7)
UNRESOLVED: ResultLab :: tests/no-run-xfail.test (3 of 7)
XFAIL: ResultLab :: tests/xfail.test (4 of 7)
PASS: ResultLab :: tests/pass.test (5 of 7)
TIMEOUT: ResultLab :: tests/timeout.test (6 of 7)
UNRESOLVED: ResultLab :: tests/unresolved.test (7 of 7)
********************
Unresolved Tests (2):
  ResultLab :: tests/no-run-xfail.test
  ResultLab :: tests/unresolved.test

********************
Timed Out Tests (1):
  ResultLab :: tests/timeout.test

********************
Unexpectedly Passed Tests (1):
  ResultLab :: tests/xpass.test


Testing Time: 1.02s

Total Discovered Tests: 8
  Excluded           : 1 (12.50%)
  Unsupported        : 1 (12.50%)
  Passed             : 1 (12.50%)
  Expectedly Failed  : 1 (12.50%)
  Unresolved         : 2 (25.00%)
  Timed Out          : 1 (12.50%)
  Unexpectedly Passed: 1 (12.50%)
```

逐栏解释：

- `Testing: 7 of 8`：选了 7 个，发现 8 个。差 1 个是 `EXCLUDED`。
- `Unsupported: 1`：`requires-missing` 被 filter-out 后剩下 1 个 `UNSUPPORTED: linux`。
- `Passed: 1`：`pass.test`。
- `Expectedly Failed: 1`：`xfail.test` 的 `FAIL` 被 `setResult()` 改写成 `XFAIL`。
- `Unresolved: 2`：一个 shell 语法错误，一个「没有 RUN」。后者即使写了 `XFAIL: *` 也不会变 XFAIL。
- `Timed Out: 1`：`sleep 2` 被 1 秒超时杀死，进程 `Exit Code: -9`。
- `Unexpectedly Passed: 1`：`XFAIL` 条件下却通过，`isFailure=True`，所以退出码 1。
- `Excluded: 1`：用户明确排除，不算失败，但计入总数。

退出码：

```bash
echo $?
# 1
```

原因是 `XPASS`、`UNRESOLVED`、`TIMEOUT` 都是 failure；`main.py:150` 用 `any(t.isFailure() for t in discovered_tests)` 决定是否 `sys.exit(1)`。

## 21.7 `--ignore-fail`：只改退出码，不改结果

同一个 suite 加 `--ignore-fail`：

```text
Total Discovered Tests: 8
  Unsupported        : 2 (25.00%)
  Passed             : 1 (12.50%)
  Expectedly Failed  : 1 (12.50%)
  Unresolved         : 2 (25.00%)
  Timed Out          : 1 (12.50%)
  Unexpectedly Passed: 1 (12.50%)
```

退出码变成 0，但摘要一行没变，`stderr` 会多一句：

```text
Exiting with status 0 instead of 1 because '--ignore-fail' was specified.
```

实现只在 `main.py:150-158`：

```python
has_failure = any(t.isFailure() for t in discovered_tests)
if has_failure:
    if opts.ignoreFail:
        sys.stderr.write("\nExiting with status 0 instead of 1 because '--ignore-fail' was specified.\n")
    else:
        sys.exit(1)
```

所以：

- CI 里用 `--ignore-fail` 只适合「先收集数据、不阻塞流水线」的场景。
- 它不会把 `FAIL` 变成 `PASS`，不会影响报告（xUnit/JSON 里仍是失败）。
- 真正要「跳过已知失败」应该用 `XFAIL` 或 `--filter-out`，而不是 `--ignore-fail`。

## 21.8 `XPASS` 为什么必须是 failure

`XPASS` 的含义是「测试声明在当前配置下会失败，但它通过了」。三种典型原因：

1. 修了 bug，但忘了删 `XFAIL:`。此时测试已经能过，XFAIL 变成噪声；如果不报错，没人会去清理。
2. XFAIL 条件写得太宽（例如 `XFAIL: *`），在某个平台上误命中。
3. 测试本身变弱了：以前真的失败，现在因为断言被删/被跳过而「通过」。

无论哪种，都需要人看一眼。因此 `XPASS` 的 `isFailure=True`，摘要单独列 `Unexpectedly Passed Tests`，退出码 1。

反过来，`XFAIL` 不是 skip：测试**真的执行了**，只是失败被接受。它仍然消耗时间、仍然可能产生副作用。把 `XFAIL` 当「不跑」会在 CI 时间里吃亏，也会掩盖 `UNRESOLVED`（后者不会被 XFAIL 改写）。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`XFAIL` 就是 skip，不会执行」 | `XFAIL` 会正常执行；只有原始结果是 `FAIL` 时才被改写成 `XFAIL`。`TIMEOUT`/`UNRESOLVED`/`UNSUPPORTED` 不会被改写 |
| 「`XPASS` 是好事，说明修好了」 | `XPASS` 的 `isFailure=True`，退出码 1。它提醒你清理过期 XFAIL 或检查测试是否变弱 |
| 「`UNSUPPORTED` 不算失败，所以无所谓」 | 它不计入失败，但计入 `Total Discovered Tests`；大量 UNSUPPORTED 会让通过率下降，CI 覆盖率失真 |
| 「`--filter` 只是少跑几个，摘要数字会变」 | `Total Discovered Tests` 不变，被过滤的测试进 `Excluded`；分母仍是全集 |
| 「`TIMEOUT` 的退出码是 1」 | 测试进程的 `Exit Code` 是 `-9`；lit 整体退出码才是 1。判定靠 `timeoutInfo`，不是退出码 |
| 「`--ignore-fail` 把失败变通过」 | 只把 lit 退出码改成 0；`Result.code`、摘要、报告都不变 |
| 「`XFAIL: *` 能兜住所有失败」 | 它只兜 `FAIL`。语法错误、超时、没有 `RUN` 仍然是 `UNRESOLVED`/`TIMEOUT` |
| 「`SKIPPED` 和 `EXCLUDED` 是一回事」 | `EXCLUDED` 是用户选择不跑；`SKIPPED` 是 gtest 主动跳过或运行中止后没跑成。报告分桶不同 |

## 重点整理

1. `ResultCode` 是带 `name/label/isFailure` 的单例；败者只有 `FAIL/XPASS/TIMEOUT/UNRESOLVED`。
2. `setResult()` 只改写两种组合：`PASS + expected → XPASS`、`FAIL + expected → XFAIL`；其它状态原样保留。
3. `XFAIL:` 语法错误会把结果改成 `UNRESOLVED`；`xfail_not` 取消预期，`exclude_xfail` 直接产生 `EXCLUDED`。
4. `Result` 不只承载状态码：`output/elapsed/start/pid/attempts/max_allowed_attempts/metrics/microResults/test_updater_outputs` 都会进入诊断或报告。
5. `EXCLUDED` 来自选择阶段（filter）或解析阶段（`--exclude-xfail`）；`UNSUPPORTED` 全部发生在执行前；`SKIPPED` 来自 gtest 或运行中止。
6. `FLAKYPASS` 是「第二次及以后才通过且没有 updater 输出」；有 updater 输出则是 `FIXED`。
7. `XPASS` 是 failure，因为「预期失败却通过」意味着 XFAIL 声明过期或测试变弱，必须人工处理。
8. `--ignore-fail` 只改进程退出码；摘要、报告、`Result.code` 全部不变。

## 自我检核

- [ ] 不看源码，写出 `setResult()` 的改写表，并解释为什么 `XFAIL: *` + 无 `RUN` 仍然是 `UNRESOLVED`。
- [ ] 给一个 `UNSUPPORTED: linux` 的测试，说明它在哪个函数返回、是否计入 `Total Discovered Tests`、是否影响退出码。
- [ ] 区分 `EXCLUDED`、`SKIPPED`、`UNSUPPORTED` 的产生阶段，并各举一个真实触发方式。
- [ ] 解释为什么 `XPASS` 的 `isFailure` 是 `True`；如果把它改成 `False`，CI 会失去什么信号？
- [ ] 一个测试跑了 3 次，第 2 次通过且有 updater 输出，最终是什么状态？如果没有 updater 输出呢？
- [ ] `--ignore-fail` 之后，xUnit 报告里的失败数会变吗？为什么？

## 延伸阅读

- **`llvm/utils/lit/lit/Test.py:12-70` 与 `Test.py:297-330`**
  - 读哪里：`ResultCode` 定义、`setResult()`。
  - 能学到什么：状态码的单例实现、XFAIL 的唯一改写点、`is` 比较的边界。
  - 前提知识：本章 21.1–21.2。
- **`llvm/utils/lit/lit/run.py:75-95`**
  - 读哪里：`Run.execute()` 的 `finally`。
  - 能学到什么：`SKIPPED` 如何在运行中止时被批量设置；为什么未执行的测试不会 `result is None` 漏到报告里。
  - 前提知识：Ch 15 的并行调度。
- **`llvm/utils/lit/lit/reports.py:200-230`**
  - 读哪里：ResultDB/JSON 报告的状态映射。
  - 能学到什么：`XPASS/FLAKYPASS/FIXED` 在外部报告里如何被折叠成 PASS/FAIL/SKIP；为什么 `XFAIL` 在 JSON 里是 `FAIL` 但 `expected=true`。
  - 前提知识：本章 21.1；Ch 17 的报告格式。
- **`llvm/utils/lit/tests/xfail-cl.py`、`tests/ignore-fail.py`、`tests/allow-retries.py`**
  - 读哪里：官方对 XFAIL/XPASS、`--ignore-fail`、重试状态的断言。
  - 能学到什么：如何用 lit 自测锁定状态码语义；可以直接改造成自己的回归测试。
  - 前提知识：本章 21.2、21.5、21.7。

下一章进入发现与配置的源码：`getTestSuite()` 如何向上找 suite、`getLocalConfig()` 的缓存与 `deepcopy`、`TestingConfig.finish()` 的 CLI 覆盖，以及 `test_times` 为什么在发现后被清空。

→ [Ch 22 Discovery 与 TestingConfig 源码深潜](./22-config-internals.md)
