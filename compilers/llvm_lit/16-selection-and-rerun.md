# Ch 16 — 选择、排序、分片与重跑

> **目标**：把 `lit tests` 里的「tests」拆成可预测的集合：知道 `--filter` 匹配什么、shard 怎么切、`--max-tests/--max-time/--max-failures` 各在什么阶段生效、`XFAIL/UNSUPPORTED/--ignore-fail` 如何改变结果与退出码，以及重试的优先级到底谁覆盖谁。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`。实验在 `/tmp/litlab-a1` 中真实运行，Ubuntu 22.04 x86_64、Python 3.10.12。所有退出码都用 `echo $?` 直接测，不经过 pipe。

---

## 16.1 选择流水线：先过滤，再切片，最后标记

`main.py` 的 `main()` 在选择阶段按固定顺序执行：

```
discovered_tests
  ├─ determine_order(opts.order)                 # main.py:74
  ├─ filter / filter-out（匹配 getFullName()）   # main.py:76-81
  ├─ --filter-failed                             # main.py:83-84
  ├─ 空集合检查 → --allow-empty-runs             # main.py:86-98
  ├─ shard（--num-shards/--run-shard）           # main.py:100-110
  ├─ --max-tests                                 # main.py:112
  ├─ mark_xfail / mark_unsupported                # main.py:114-115
  ├─ mark_excluded                               # main.py:117
  └─ run_tests(selected_tests, ...)              # main.py:119-120
```

`discovered_tests` 是发现全集（摘要里的 `Total Discovered Tests`）；`selected_tests` 是筛出来的执行子集。`mark_excluded()`（第 254–258 行）把差集标成 `EXCLUDED`，所以 `--filter` 之后摘要里出现 `Excluded` 不是 bug。

## 16.2 `--filter` / `--filter-out`：匹配全名，不是文件名

`cl_arguments.py` 第 445–458 行定义两个参数，默认值分别来自 `LIT_FILTER`（`.*`）和 `LIT_FILTER_OUT`（`^$`）。`main.py` 第 76–81 行：

```python
selected_tests = [
    t for t in discovered_tests
    if opts.filter.search(t.getFullName())
    and not opts.filter_out.search(t.getFullName())
]
```

三个关键点：

1. 正则是**大小写不敏感**的（`_case_insensitive_regex` 用 `re.IGNORECASE`）。
2. 匹配 `Test.getFullName()`（`Test.py` 第 320–321 行）：`Suite 名 + " :: " + "/".join(path_in_suite)`，例如 `LitLab :: tests/pass/p01.test`。
3. `--filter-out` 在 `--filter` 之后应用，默认什么都不排除。

真实实验：

```bash
cd /tmp/litlab-a1
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py \
  --filter 'pass/p0[12]' --show-excluded tests
```

真实输出（节选）：

```
-- Testing: 2 of 28 tests, 2 workers --
PASS: LitLab :: tests/pass/p01.test (1 of 2)
PASS: LitLab :: tests/pass/p02.test (2 of 2)
********************
Excluded Tests (26):
  LitLab :: tests/fail/f01-false.test
  ...
Total Discovered Tests: 28
  Excluded: 26 (92.86%)
  Passed  :  2 (7.14%)
```

退出码 `0`。用绝对路径匹配同一文件：

```bash
python3 .../lit.py --filter '/tmp/litlab-a1/tests/pass/p01.test' tests; echo $?
```

真实输出：

```
error: filter did not match any tests (of 28 discovered).  Use '--allow-empty-runs' to suppress this error.
2
```

因为全名里没有 `/tmp/litlab-a1`。`--filter 'p01.test'` 能命中，但带上 suite 内路径更稳，避免同名文件误伤。

`--filter-out` 真实示例：`--filter 'tests/pass' --filter-out 'p0[3-8]'` 只剩 `p01`、`p02`，其余全 `EXCLUDED`。

## 16.3 排序：`lexical`、`random`、`smart`

`cl_arguments.py` 第 11–15 行的 `TestOrder` 只有三个值，默认 `smart`（第 427–431 行）。`main.determine_order()` 第 194–208 行：

```python
if enum_order == TestOrder.RANDOM:
    random.shuffle(tests)
elif enum_order == TestOrder.LEXICAL:
    tests.sort(key=lambda t: t.getFullName())
else:
    tests.sort(
        key=lambda t: (not t.previous_failure, -t.previous_elapsed, t.getFullName())
    )
```

- `lexical`：按全名字典序，稳定可复现。
- `random`：每次随机，适合发现顺序依赖。
- `smart`：失败优先，其次慢测试优先，最后按全名兜底。

`smart` 的数据来自 `.lit_test_times.txt`。`Test.__init__` 第 292–295 行把负数解释成上次失败：

```python
time = suite.test_times["/".join(path_in_suite)]
self.previous_elapsed = abs(time)
self.previous_failure = time < 0
```

真实实验：先让 `f01-false.test` 失败一次，再按 smart 跑 `f01`、`p01`、`p02`：

```
-- Testing: 3 tests, 1 workers --
FAIL: LitLab :: tests/fail/f01-false.test (1 of 3)
PASS: LitLab :: tests/pass/p01.test (2 of 3)
PASS: LitLab :: tests/pass/p02.test (3 of 3)
```

失败测试被排到最前面。`--filter` 之后 shard 也基于这个顺序切。

### 已废弃的两个选项

`cl_arguments.py` 第 432–444 行：

```python
"--shuffle"   -> const=TestOrder.RANDOM   # DEPRECATED: use --order=random
"-i/--incremental" -> action="store_true" # DEPRECATED: use --order=smart
```

`--shuffle` 等价于 `--order=random`；`-i` 不再改变行为，只打印：

```
WARNING: --incremental is deprecated. Failing tests now always run first.
```

## 16.4 分片：`--num-shards M` / `--run-shard N`

`cl_arguments.py` 第 503–518 行定义参数，第 562–569 行校验「必须一起用」「1 ≤ run ≤ shards」。分配逻辑在 `main.filter_by_shard()` 第 211–227 行：

```python
test_ixs = range(run - 1, len(tests), shards)
selected_tests = [tests[i] for i in test_ixs]
```

`run` 是 1-based，索引是 0-based。所以 `--num-shards 3 --run-shard 2` 选索引 `1, 4, 7`，即第 2、5、8 个测试。真实实验：

```bash
python3 .../lit.py --num-shards 3 --run-shard 2 tests/pass
```

真实输出（节选）：

```
lit.py: .../main.py:226: note: Selecting shard 2/3 = size 3/8 = tests #(3*k)+2 = [2, 5, 8]
-- Testing: 3 of 8 tests, 3 workers --
...
Total Discovered Tests: 8
  Excluded: 5 (62.50%)
  Passed  : 3 (37.50%)
```

note 里的 `[2, 5, 8]` 是 1-based 展示；实际变量是 `[1, 4, 7]`。

### 空 shard 不是失败

`main.py` 第 102–110 行：shard 后为空时打印 `warning: shard does not contain any tests.`，然后 `sys.exit(0)`。真实实验：

```bash
python3 .../lit.py --num-shards 100 --run-shard 100 tests/pass; echo $?
```

真实输出：

```
warning: shard does not contain any tests.  Consider decreasing the number of shards.
0
```

CI 分片时某些 shard 为空是正常现象；只有 `--filter` 匹配为空才是退出码 2（见 16.7）。

## 16.5 三个上限：`--max-tests`、`--max-time`、`--max-failures`

### `--max-tests`

`main.py` 第 112 行 `selected_tests = selected_tests[: opts.max_tests]`，在 filter、filter-failed、shard **之后**执行。真实实验：

```bash
python3 .../lit.py -j 1 --max-tests=2 --show-excluded tests/pass
```

```
-- Testing: 2 of 8 tests, 1 workers --
PASS: LitLab :: tests/pass/p04.test (1 of 2)
PASS: LitLab :: tests/pass/p01.test (2 of 2)
...
  Excluded: 6
```

默认 `--order=smart`，前两个不是 lexical 的 `p01/p02`，而是按上次耗时排序后的前两个。要可复现加 `--order=lexical`。

### `--max-time`

`--max-time` 映射到 `opts.timeout`（`cl_arguments.py` 第 419–425 行），`run.py` 第 85–88 行把它变成全局 deadline。真实实验（8 个 0.3 秒测试，`-j 2`，上限 1 秒）：

```bash
python3 .../lit.py -j 2 --max-time=1 --show-skipped tests/pass; echo $?
```

真实输出（节选）：

```
-- Testing: 8 tests, 2 workers --
PASS: LitLab :: tests/pass/p01.test (1 of 8)
...
warning: reached timeout, skipping remaining tests
********************
Skipped Tests (4):
  LitLab :: tests/pass/p02.test
  ...
Total Discovered Tests: 8
  Skipped: 4 (50.00%)
  Passed : 4 (50.00%)
0
```

退出码 `0`：未执行的是 `SKIPPED`，不是 failure。`--max-time` 是整次运行上限；单测试超时用 `--timeout`。

### `--max-failures`

`run.py` 第 312–318 行在失败计数达到上限时抛 `MaxFailuresError`，`Run.execute()` 的 `finally` 把剩余测试标成 `SKIPPED`。真实实验：

```bash
python3 .../lit.py --max-failures 2 -j 1 --order=lexical --show-skipped \
  tests/fail/f01-false.test tests/fail/f02-missing-cmd.test tests/fail/f03-filecheck.test
```

真实输出（节选）：

```
FAIL: LitLab :: tests/fail/f01-false.test (1 of 3)
FAIL: LitLab :: tests/fail/f02-missing-cmd.test (2 of 3)
warning: reached maximum number of test failures, skipping remaining tests
********************
Skipped Tests (1):
  LitLab :: tests/fail/f03-filecheck.test
...
  Skipped: 1 (33.33%)
  Failed : 2 (66.67%)
```

退出码 `1`。加 `--order=lexical` 只是让「前两个是谁、剩下的谁」可复现；不加时 smart 顺序会随 `.lit_test_times.txt` 变化。

| 选项 | 作用域 | 到达上限后 | 退出码 |
|---|---|---|---|
| `--max-tests` | 选择阶段 | 只执行前 N 个，其余 `EXCLUDED` | 取决于执行结果 |
| `--max-time` | 整次运行 | 剩余测试 `SKIPPED` | 通常 0/1 |
| `--max-failures` | 全局失败数 | 剩余测试 `SKIPPED` | 1 |

## 16.6 `XFAIL` / `UNSUPPORTED` 的 CLI 覆盖

`mark_xfail()` / `mark_unsupported()` 在 `main.py` 第 230–251 行。匹配同时接受 `os.sep.join(path_in_suite)`（`tests/pass/p01.test`）和 `getFullName()`（`LitLab :: tests/pass/p01.test`）；列表用分号分隔。

| 选项 | 效果 | 作用点 |
|---|---|---|
| `--xfail LIST` | 标成预期失败 | `t.xfails += "*"` |
| `--xfail-not LIST` | 取消这些测试的 XFAIL | `t.xfail_not = True` |
| `--exclude-xfail` | 预期失败的测试直接 `EXCLUDED` | `parseIntegratedTestScript()` |
| `--unsupported LIST` | 标成 `UNSUPPORTED` | 加 `lit-unsupported-marker` feature |
| `--unsupported-not LIST` | 取消这些测试的 UNSUPPORTED | `t.unsupported_not = True` |

真实实验一：把一个通过的测试标成 XFAIL：

```bash
python3 .../lit.py --xfail 'LitLab :: tests/pass/p01.test' tests/pass/p01.test
```

```
XPASS: LitLab :: tests/pass/p01.test (1 of 1)
********************
Unexpectedly Passed Tests (1):
  LitLab :: tests/pass/p01.test
Total Discovered Tests: 1
  Unexpectedly Passed: 1 (100.00%)
```

退出码 `1`。`XPASS` 是 failure（`Test.py` 第 57 行：`ResultCode("XPASS", "Unexpectedly Passed", True)`）。XFAIL 不是跳过：测试仍然执行，通过了反而报错。

真实实验二：同一个测试既 `--xfail` 又 `--xfail-not`：

```bash
python3 .../lit.py --xfail 'LitLab :: tests/pass/p01.test' \
  --xfail-not 'LitLab :: tests/pass/p01.test' tests/pass/p01.test; echo $?
```

```
PASS: LitLab :: tests/pass/p01.test (1 of 1)
...
  Passed: 1 (100.00%)
0
```

`Test.isExpectedToFail()` 第 350–351 行在 `xfail_not` 为真时直接 `return False`，优先级最高。

真实实验三：`--exclude-xfail`：

```bash
python3 .../lit.py --xfail 'LitLab :: tests/pass/p01.test' \
  --exclude-xfail --show-excluded tests/pass/p01.test
```

```
EXCLUDED: LitLab :: tests/pass/p01.test (1 of 1)
  Excluded: 1 (100.00%)
0
```

实现在 `TestRunner.parseIntegratedTestScript()` 第 2128–2129 行：

```python
if test.exclude_xfail and test.isExpectedToFail():
    return lit.Test.Result(Test.EXCLUDED, "excluding XFAIL tests")
```

帮助文本提醒：不调用这个函数、也不自己实现该选项的 test format 不受影响。

真实实验四：`--unsupported` 得到 `UNSUPPORTED`（退出 0）；再加 `--unsupported-not` 则 `PASS`（退出 0）。优先级：`--xfail-not` / `--unsupported-not` 高于指令行和正向 CLI；`--exclude-xfail` 只在 `isExpectedToFail()` 为真时生效。

## 16.7 空运行与忽略失败

### `--allow-empty-runs`

`main.py` 第 86–98 行：filter 后为空默认退出 2；加 `--allow-empty-runs` 则提示并退出 0。

```bash
python3 .../lit.py --filter 'no-such-test' --allow-empty-runs tests; echo $?
```

```
error: filter did not match any tests (of 28 discovered).  Suppressing error because '--allow-empty-runs' was specified.
0
```

不加时输出 `Use '--allow-empty-runs' to suppress this error.`，退出 `2`。

### `--ignore-fail`

`main.py` 第 150–158 行：有 failure 时，`--ignore-fail` 打印 `Exiting with status 0 instead of 1 ...`，否则 `sys.exit(1)`。真实实验：

```bash
python3 .../lit.py -v --ignore-fail tests/fail/f01-false.test; echo $?
```

```
FAIL: LitLab :: tests/fail/f01-false.test (1 of 1)
...
# error: command failed with exit status: 1
...
Exiting with status 0 instead of 1 because '--ignore-fail' was specified.
...
  Failed: 1 (100.00%)
0
```

它只改退出码，不改结果：错误仍然打印，摘要仍然显示 `Failed: 1`。不要用 `--ignore-fail` 掩盖 CI 红。

## 16.8 重试：`ALLOW_RETRIES`、`config.test_retry_attempts`、`--max-retries-per-test`

优先级从高到低：

1. 测试文件里的 `ALLOW_RETRIES: N`；
2. suite 配置里的 `config.test_retry_attempts = N`；
3. 命令行 `--max-retries-per-test=N`。

原因在源码：

- `Test.__init__`（`Test.py` 第 281 行）先取配置：`self.allowed_retries = getattr(config, "test_retry_attempts", 0)`。
- `TestingConfig.finish()`（第 251–255 行）只有在 config 没有定义 `test_retry_attempts` 时，才用 CLI 的 `maxRetriesPerTest` 填进去。
- `parseIntegratedTestScript()`（`TestRunner.py` 第 2132–2133 行）最后用 `ALLOW_RETRIES` 覆盖 `test.allowed_retries`。

所以 CLI 只是「给没配置的 suite 一个默认值」，不能覆盖测试或 suite 的显式设置。

### 真实实验：`ALLOW_RETRIES: 2`

测试第 1、2 次失败，第 3 次通过：

```text
# ALLOW_RETRIES: 2
# RUN: %python -c "...counter... raise SystemExit(0 if n >= 3 else 1)"
```

```bash
python3 .../lit.py -a tests/misc/t06-retry.test; echo $?
```

真实输出（节选）：

```
FLAKYPASS: LitLab :: tests/misc/t06-retry.test (1 of 1, 3 of 3 attempts)
...
# | attempt 3
...
  Passed With Retry: 1 (100.00%)
0
```

`FLAKYPASS` 是成功结果（`Test.py` 第 50 行），表示「重试后通过」。`-v` 默认看不到它的输出，因为 `-v` 只对 failure 打印；要看 flaky 输出用 `--test-output=failed-or-flaky` 或 `-a`。

### 真实实验：`--max-retries-per-test`

测试没有 `ALLOW_RETRIES`，第 3 次才通过：

```bash
python3 .../lit.py --max-retries-per-test=3 tests/misc/t07-retry-cli.test
```

```
FLAKYPASS: LitLab :: tests/misc/t07-retry-cli.test (1 of 1, 3 of 4 attempts)
```

`3 of 4 attempts`：第 3 次成功，最多允许 1 + 3 = 4 次。

> **复现提示**：这两个测试用 `%t.counter` 跨重试计数，计数文件会留在 `Output/` 下。重跑前先删掉 `Output/tests/misc/Output/t06-retry.test.tmp.counter` 和 `t07-retry-cli.test.tmp.counter`；否则计数已经 ≥ 3，第一次执行就返回 0，你会看到 `PASS` 而不是 `FLAKYPASS`，`attempt` 数字也会从上次继续（4、5……）。

### `FIXED` 是什么

`TestRunner._runShTest()` 第 2231–2237 行：重试后通过时，如果 test updater 实际改了文件，状态是 `FIXED`，否则是 `FLAKYPASS`。`FIXED` 需要 updater 基础设施（例如 `--update-tests` 配合 FileCheck updater），本章的 sleep/echo 实验无法触发，**未实测**；`FLAKYPASS` 已实测。

> `--retry-failed` 不存在。当前组合是 `--filter-failed`（只跑上次失败）+ 重试配置；`--filter-failed` 依赖 `.lit_test_times.txt` 里的负数。没有该文件（或文件里没有负时间）时，所有 `previous_failure` 都是 False，`--filter-failed` 选不出任何测试，于是走 16.7 的空集合检查：默认退出 2，加 `--allow-empty-runs` 才退出 0。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`--filter` 匹配文件路径/文件名」 | 匹配 `Test.getFullName()`，即 `Suite :: suite/内/路径`；绝对路径不会命中。 |
| 「shard 为空说明 CI 配错了」 | 空 shard 打印 warning 并退出 0；只有 filter 为空才退出 2（除非 `--allow-empty-runs`）。 |
| 「`--max-failures` 之后剩下的测试是失败」 | 剩余测试是 `SKIPPED`，不是 `FAIL`；退出码仍是 1。 |
| 「`--ignore-fail` 会让错误消失」 | 它只把退出码改成 0；错误输出和 `Failed` 摘要照旧。 |
| 「`--retry-failed` 可以重跑失败测试」 | 没有这个选项；用 `--filter-failed` 选择上次失败，再配 `ALLOW_RETRIES`/`--max-retries-per-test`。 |
| 「CLI 的 `--max-retries-per-test` 覆盖 `ALLOW_RETRIES`」 | 反过来：`ALLOW_RETRIES` > `config.test_retry_attempts` > CLI。 |
| 「`--max-tests` 在 filter 之前生效」 | 它在 shard 之后、标记之前；顺序决定你拿到哪 N 个。 |

## 重点整理

- 选择顺序：order → filter/filter-out → filter-failed → 空检查 → shard → max-tests → marks。
- `--filter` 是大小写不敏感正则，匹配 `Suite :: path` 全名；`--filter-out` 在其后排除。
- shard 用 `range(run-1, len, shards)`，1-based 参数、0-based 索引；空 shard 退出 0。
- `--max-tests` 截断选择集；`--max-time` 是全局 deadline；`--max-failures` 是全局失败计数。后两者都把剩余测试标成 `SKIPPED`。
- `XPASS` 是 failure；`XFAIL`/`UNSUPPORTED`/`EXCLUDED` 不是。
- `--xfail-not` / `--unsupported-not` 优先级最高；`--exclude-xfail` 在 ShTest 解析阶段生效。
- `--allow-empty-runs` 把「filter 为空」从退出 2 改成 0；`--ignore-fail` 只改最终退出码。
- 重试优先级：`ALLOW_RETRIES` > `config.test_retry_attempts` > `--max-retries-per-test`；成功后是 `FLAKYPASS`（或 `FIXED`）。

## 自我检核

不看笔记，试着回答：

- [ ] `--filter 'tests/pass/p01.test'` 与 `--filter '/abs/path/p01.test'` 为什么一个命中、一个不命中？
- [ ] 8 个测试、`--num-shards 3 --run-shard 2` 具体选哪几个索引？参数是 1-based 还是 0-based？
- [ ] `--max-failures 2` 之后，第 3 个测试的结果码是什么？退出码是多少？`--max-time` 到达后剩余测试又是什么状态？
- [ ] 为什么 `--xfail` 一个通过的测试会得到 `XPASS` 而不是 `XFAIL`？`XPASS` 的退出码是多少？
- [ ] 说出重试的三个来源及优先级；`FLAKYPASS` 与 `FIXED` 的区别是什么？
- [ ] `--filter-failed` 依赖哪个文件的什么编码约定？没有该文件时会怎样？

## 延伸阅读

- **[`llvm/utils/lit/lit/main.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/main.py)**
  - 读什么：`main()` 第 74–158 行、`determine_order()`、`filter_by_shard()`、`mark_xfail()`、`mark_unsupported()`。
  - 能学到：选择流水线的真实顺序与每个阶段的边界。
  - 前提：读过本章 16.1。
- **[`llvm/utils/lit/lit/cl_arguments.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/cl_arguments.py)**
  - 读什么：`TestOrder`、`--filter`、`--num-shards`、`--xfail`、`--max-retries-per-test` 的定义与校验。
  - 能学到：哪些选项互相依赖、哪些默认值来自环境变量。
  - 前提：知道 `argparse` 的 `add_argument`。
- **[`llvm/utils/lit/tests/allow-retries.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/allow-retries.py)**
  - 读什么：全文与 `Inputs/allow-retries/`、`Inputs/max-retries-per-test/`。
  - 能学到：三种重试来源的官方组合测试，包括 `FLAKYPASS` 的 `n of m attempts` 格式。
  - 前提：能读 lit 自测的 `RUN:` + `FileCheck` 写法。

> 下一章看结果怎么被打印和导出：`-v/-a/-q` 到底改了什么，JSON/xUnit/ResultDB 的 schema 长什么样，以及 `--time-tests` 的直方图从哪来。

→ [Ch 17 输出与报告](./17-reports-and-output.md)
