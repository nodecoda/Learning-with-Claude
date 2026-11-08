# Ch 17 — 输出与报告

> **目标**：能按需控制 lit 的控制台噪声，读懂 JSON、xUnit、ResultDB、Chrome trace、WTT 五种输出的结构与用途，并解释 `--time-tests`、`.lit_test_times.txt`、`Output/`、`LIT_PRESERVES_TMP` 之间的关系。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`。实验在 `/tmp/litlab-a1` 中真实运行，Ubuntu 22.04 x86_64、Python 3.10.12。所有输出均为本机实跑；长文件只贴结构。

---

## 17.1 输出层级：`--test-output` 与 `--print-result-after`

lit 把「结果行」和「测试执行的命令/输出」分成两个独立层级，两者都用同一组值：

| 值 | 含义 |
|---|---|
| `off` | 不打印 |
| `failed` | 只打印失败测试 |
| `failed-or-flaky` | 失败测试 + 重试过的测试 |
| `all` | 所有测试 |

`--test-output` 控制**命令与输出**；`--print-result-after` 控制**结果行**。`cl_arguments.py` 第 118–133 行用 `TestOutputAction` 维护一个不变式：`print_result_after >= test_output`。如果你把 `--test-output` 调到比当前 `--print-result-after` 高，后者会被抬高；反过来把 `--print-result-after` 调低，`--test-output` 会被压低。这样不会出现「打印了输出但没打印结果行」的错位。

默认值：`--test-output=off`，`--print-result-after=all`。

### 别名展开

`cl_arguments.py` 第 153–215 行：

| 别名 | 展开 |
|---|---|
| `-q` / `--quiet` | `diagnostic_level=error` + `test_output=off` + `print_result_after=failed` + `terse_summary=True` |
| `-s` / `--succinct` | `useProgressBar=True` + `print_result_after=failed` |
| `-v` / `--verbose` | `test_output=failed` |
| `-vv` / `--echo-all-commands` | 已废弃，等价 `-v` |
| `-a` / `--show-all` | `test_output=all` |

真实对比。`--print-result-after=failed`：

```
-- Testing: 2 tests, 2 workers --
FAIL: LitLab :: tests/fail/f01-false.test (1 of 2)
********************
Failed Tests (1):
  LitLab :: tests/fail/f01-false.test

Total Discovered Tests: 2
  Passed: 1 (50.00%)
  Failed: 1 (50.00%)
```

通过测试的结果行不打印，但摘要仍然统计 `Passed: 1`。`-q`：

```
-- Testing: 2 tests, 2 workers --
FAIL: LitLab :: tests/fail/f01-false.test (1 of 2)
********************
Failed Tests (1):
  LitLab :: tests/fail/f01-false.test

Total Discovered Tests: 2
  Failed: 1 (50.00%)
```

`-q` 连 `Passed` 统计也去掉了，只保留 failure 相关行。`-s` 在非 tty 下退化成简单进度条：

```
-- Testing: 2 tests, 2 workers --
Testing:  0.. 10.. 20.. 30.. 40.. 50.. 60.. 70.. 80.. 90..

Testing Time: 0.61s
```

`-a` 会给通过的测试也打印 `Exit Code: 0`、`# RUN:`、`# executed command:` 和输出。

### `-v` 不打印 FLAKYPASS

`display.py` 第 85–92 行的 `shouldPrintInfo()`：

```python
if infoOption == "all":
    return True
if test.isFailure():
    return infoOption == "failed" or infoOption == "failed-or-flaky"
if test.result.attempts > 1:
    return infoOption == "failed-or-flaky"
return False
```

`FLAKYPASS` 不是 failure，只是 `attempts > 1`。所以 `-v`（`failed`）不会显示它的输出；要看得用 `--test-output=failed-or-flaky` 或 `-a`。真实实验：

```bash
python3 .../lit.py --test-output=failed-or-flaky tests/misc/t06-retry.test
```

```
FLAKYPASS: LitLab :: tests/misc/t06-retry.test (1 of 1, 3 of 3 attempts)
Exit Code: 0
...
# | attempt 3
```

### `--diagnostic-level`

`cl_arguments.py` 第 134–139 行，取值 `error|warning|note`，默认 `note`。它控制 `lit_config.error/warning/note` 这类诊断（`LitConfig.py` 第 263–275 行）；`debug` 消息只由 `--debug` 打开，不受这个级别影响。直接写 `sys.stderr` 的提示（例如空 shard 的 warning）也不受它控制。

## 17.2 `--show-*`：只影响摘要里的「分组列表」

`cl_arguments.py` 第 251–261 行为所有**成功结果码**生成 `--show-<code>`：

```python
success_codes = [c for c in lit.Test.ResultCode.all_codes() if not c.isFailure]
for code in success_codes:
    format_group.add_argument(
        "--show-{}".format(code.name.lower()),
        dest="shown_codes",
        action="append_const",
        const=code,
        default=[],
    )
```

包括 `--show-excluded`、`--show-skipped`、`--show-unsupported`、`--show-pass`、`--show-flakypass`、`--show-fixed`、`--show-xfail`。`main.print_group()` 第 348–352 行：

```python
if not tests:
    return
if not code.isFailure and code not in shown_codes:
    return
```

失败结果码**总是**打印；成功结果码只有出现在 `shown_codes` 里才打印分组列表。摘要的计数不受影响：`print_summary()` 第 360–377 行始终遍历所有 code。

真实对比。默认（无 `--show-pass`）：

```
-- Testing: 2 tests, 2 workers --
PASS: LitLab :: tests/pass/p02.test (1 of 2)
PASS: LitLab :: tests/pass/p01.test (2 of 2)

Testing Time: 0.61s

Total Discovered Tests: 2
  Passed: 2 (100.00%)
```

加 `--show-pass`：

```
PASS: LitLab :: tests/pass/p02.test (1 of 2)
PASS: LitLab :: tests/pass/p01.test (2 of 2)
********************
Passed Tests (2):
  LitLab :: tests/pass/p01.test
  LitLab :: tests/pass/p02.test

Total Discovered Tests: 2
  Passed: 2 (100.00%)
```

`--show-excluded` 同理：它把 `Excluded Tests (26): ...` 列表打出来，但摘要里的 `Excluded: 26` 本来就在。`--show-*` 不改变任何测试的结果，只改变打印。

## 17.3 `-o results.json`：JSON schema

`-o/--output` 的类型是 `lit.reports.JsonReport`（`cl_arguments.py` 第 223–229 行）。`reports.py` 第 47–101 行：

```python
data = {}
data["__version__"] = lit.__versioninfo__
data["elapsed"] = elapsed
data["tests"] = tests_data = []
for test in tests:
    test_data = {
        "name": test.getFullName(),
        "code": test.result.code.name,
        "output": test.result.output,
        "elapsed": test.result.elapsed,
    }
```

真实输出（只跑 `f01`、`p01`、`p02`，节选）：

```json
{
  "__version__": [
    24,
    0,
    0
  ],
  "elapsed": 0.6127502918243408,
  "tests": [
    {
      "code": "FAIL",
      "elapsed": 0.004121303558349609,
      "name": "LitLab :: tests/fail/f01-false.test",
      "output": "Exit Code: 1\n\nCommand Output (stdout):\n--\n# RUN: at line 1\necho \"before\"\n..."
    },
    {
      "code": "PASS",
      "elapsed": 0.30153632164001465,
      "name": "LitLab :: tests/pass/p02.test",
      "output": "Exit Code: 0\n\nCommand Output (stdout):\n--\n..."
    }
  ]
}
```

要点：

- `__version__` 是 `lit.__versioninfo__`，实际是**数组** `[24, 0, 0]`，不是字符串 `24.0.0dev`。
- `elapsed` 是整次运行的秒数（浮点）。
- `tests[]` 的 `code` 是结果码名，`name` 是全名，`output` 是完整失败/执行日志，`elapsed` 是单测试秒数。
- `JsonReport` 会过滤掉 `EXCLUDED` 和 `SKIPPED`（`unexecuted_codes`）；`UNSUPPORTED` 不在这个集合里，仍会出现在 JSON 中。所以 JSON 不是「只包含执行过的测试」，而是「不包含 excluded/skipped」。
- 有 metrics/microResults 时还会多出 `metrics` 字段或按 `name:key` 展开的条目。

`-o` 是 JSON，不是文本日志；想要人读的日志用 `-v/-a` 的输出。

## 17.4 `--xunit-xml-output`：给 CI 的 JUnit XML

`XunitReport` 在 `reports.py` 第 118–190 行。真实结构（节选）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<testsuites time="0.61">
<testsuite name="LitLab" tests="28" failures="1" skipped="25" time="0.61">
<testcase classname="LitLab.tests/fail" name="f01-false.test" time="0.00">
  <failure><![CDATA[Exit Code: 1
...
]]></failure>
</testcase>
<testcase classname="LitLab.tests/fail" name="f02-missing-cmd.test" time="0.00">
  <skipped message="Test not selected (--filter, --max-tests)"/>
</testcase>
...
<testcase classname="LitLab.tests/pass" name="p01.test" time="0.30"/>
</testsuite>
</testsuites>
```

层级：`<testsuites>` → 每个 suite 一个 `<testsuite>` → 每个测试一个 `<testcase>`。

- `tests` / `failures` / `skipped` / `time` 是 suite 级统计。
- 失败测试写 `<failure><![CDATA[...]]></failure>`；CDATA 里的 `]]>` 会被拆开，控制字符会被移除（`remove_invalid_xml_chars`）。
- `<testcase>` 的 `classname` 是 `suite.path`，`name` 是文件名，`time` 是秒。
- `XunitReport.skipped_codes = {EXCLUDED, SKIPPED, UNSUPPORTED}`。**未选中的测试也会出现在 XML 里，标成 skipped**，并带 `Test not selected (--filter, --max-tests)` 消息。这是与 JSON 最大的差别。

如果你在 CI 里分片跑 lit，再把多个 XML 合并，未选中测试会被重复计入 skipped；`main.py` 第 100–104 行的注释明确说「多 shard 时不要把 skipped 放进 xunit，否则合并会重复」，所以 shard 模式下 `tests_for_report` 只取 `selected_tests`（第 135 行）。

## 17.5 其他三种报告：ResultDB、Chrome trace、WTT

### `--resultdb-output`

`ResultDBReport` 在 `reports.py` 第 233–274 行。真实输出（节选）：

```json
{
  "__version__": [24, 0, 0],
  "elapsed": 0.6127502918243408,
  "tests": [
    {
      "testId": "LitLab :: tests/fail/f01-false.test",
      "start_time": "2026-11-08T23:16:31.070587Z",
      "duration": "0.004121304s",
      "summary_html": "<p><text-artifact artifact-id=\"artifact-content-in-request\"></p>",
      "artifacts": {
        "artifact-content-in-request": {
          "contents": "RXhpdCBDb2RlOiAxCgpDb21tYW5kIE91dHB1dCAoc3Rkb3V0KToKLS0K..."
        }
      },
      "expected": false,
      "status": "FAIL"
    }
  ]
}
```

`contents` 是测试输出的 base64。`status` 的映射在 `gen_resultdb_test_entry()` 第 213–229 行：PASS/XPASS/FLAKYPASS/FIXED → `PASS`，FAIL/XFAIL → `FAIL`，UNSUPPORTED/SKIPPED/EXCLUDED → `SKIP`，UNRESOLVED/TIMEOUT → `ABORT`。

### `--time-trace-output`

`TimeTraceReport` 第 277–305 行输出 Chrome tracing 的 `traceEvents`：

```json
{
  "traceEvents": [
    {
      "pid": 1102182,
      "tid": 1,
      "ph": "X",
      "ts": 0,
      "dur": 304314,
      "name": "LitLab :: tests/pass/p06.test"
    }
  ]
}
```

`ph: "X"` 是 complete event；`ts`/`dur` 单位是微秒；`pid` 是 worker 进程号，所以可以在 Chrome `chrome://tracing` 里按 worker 看并行时间线。

> **实测 bug**：如果这次运行包含 `EXCLUDED` 测试（例如用了 `--filter`），`TimeTraceReport._write_results_to_file()` 第 282 行会对 `t.result.start` 取 `min`，而 excluded 的 `start` 是 `None`，直接抛 `TypeError`。本机一次 `--filter` 运行的完整末行是：
>
> ```
> TypeError: '<' not supported between instances of 'NoneType' and 'NoneType'
> ```
>
> 具体类型组合取决于 `tests` 的排列顺序：如果第一个元素是已执行测试，可能变成 `'NoneType' and 'float'`。规避：生成 time trace 时避免 `--filter`，或用 shard 让 `tests_for_report` 只包含 selected tests。本机实测，在包含 excluded 的 filter 运行中该报告会崩。

### `--wtt-output`

`WttReport`（第 324 行起；属性转义辅助函数 `_wtt_attr` 在 308 行）写 UTF-16 的 WTT XML。`file` 实测：

```
results-pass.wtl: XML 1.0 document, Unicode text, UTF-16, little-endian text
```

结构：

```xml
<?xml version="1.0" encoding="utf-16"?>
<WTT-Logger>
<RTI ID="" Machine="" ProcessName="lit" ProcessID="1102173" ThreadID="0" BaseTime="..." Frequency="1" />
<CTX ID="1" Current="WTTLOG" Parent="ROOT" />
<CTX ID="" Current="LitLab :: tests/pass/p01.test" Parent="WTTLOG" />
<StartTest Title="LitLab :: tests/pass/p01.test" TUID="" CA="0" LA="0">...</StartTest>
<Msg UserText="Exit Code: 0 ..." CA="0" LA="0">...</Msg>
<EndTest Title="LitLab :: tests/pass/p01.test" TUID="" Result="Pass" ...>...</EndTest>
</WTT-Logger>
```

UNSUPPORTED 会被写成 `Pass`（附消息说明「not applicable」）；EXCLUDED/SKIPPED 直接省略。

## 17.6 报告范围与摘要格式

### `--report-failures-only`

`main.py` 第 135–141 行：

```python
tests_for_report = selected_tests if opts.shard else discovered_tests
if opts.report_failures_only:
    tests_for_report = [t for t in tests_for_report if t.isFailure()]
for report in opts.reports:
    report.write_results(tests_for_report, elapsed)
```

真实实验：`--report-failures-only -o failonly.json --xunit-xml-output failonly.xml`，JSON 里只剩 `FAIL` 那一条；XML 里 `<testsuite tests="1" failures="1" skipped="0">`，通过测试完全不出现。

### `--terse-summary` 与 `--no-terse-summary`

源码里有个反直觉点：`cl_arguments.py` 第 140–152 行虽然帮助文本写「default on」，但第 152 行是：

```python
parser.set_defaults(terse_summary=False)
```

实际默认是**非 terse**。`print_summary()` 第 360–377 行把 `quiet` 解释成「只显示 failure 计数 + 不显示 Testing Time」。真实对比：

```
$ lit --terse-summary ...
FAIL: LitLab :: tests/fail/f01-false.test (1 of 2)
PASS: LitLab :: tests/pass/p01.test (2 of 2)
********************
Failed Tests (1):
  LitLab :: tests/fail/f01-false.test

Total Discovered Tests: 2
  Failed: 1 (50.00%)
```

```
$ lit --no-terse-summary ...
...
Testing Time: 0.31s

Total Discovered Tests: 2
  Passed: 1 (50.00%)
  Failed: 1 (50.00%)
```

`--terse-summary` 隐藏了 `Testing Time` 和 `Passed`；`--no-terse-summary` 显示全部。`-q` 会强制 `terse_summary=True`。

### `--time-tests`

`cl_arguments.py` 第 538–549、600–613 行先预处理这个参数：裸 `--time-tests` 默认显示最慢 20 个；`--time-tests=N` 限制 N；`--time-tests=all` 全显示。值必须用 `=`。真实输出：

```
Slowest Tests (3 of 8):
--------------------------------------------------------------------------
0.31s: LitLab :: tests/pass/p04.test
0.30s: LitLab :: tests/pass/p06.test
0.30s: LitLab :: tests/pass/p03.test

Test Times (8):
--------------------------------------------------------------------------
[    Range    ] :: [               Percentage               ] :: [Count]
--------------------------------------------------------------------------
[0.300s,0.320s) :: [****************************************] :: [8/8]
[0.280s,0.300s) :: [                                        ] :: [0/8]
...
--------------------------------------------------------------------------
```

直方图由 `util.printHistogram()` 生成：按最大值选一个「nice」的桶高，至少 10 个桶；每行是 `[下界,上界) :: [百分比条] :: [count/total]`。

## 17.7 时间文件、`Output/` 与 `LIT_PRESERVES_TMP`

`.lit_test_times.txt` 的位置：`TestTimes._get_test_times_path()` 第 4–8 行优先 `suite.exec_root/.lit_test_times.txt`，不存在时回退到 `suite.source_root/.lit_test_times.txt`。格式是「科学计数法时间 + 空格 + suite 内路径」，每行一个（下面是一次真实快照；耗时数值每次运行都会变）：

```
3.016412e-01 tests/pass/p02.test
3.017399e-01 tests/pass/p03.test
-4.189014e-03 tests/fail/f01-false.test
```

负号表示上次失败（`record_test_times()` 第 45–48 行）。`--skip-test-time-recording` 跳过写入；它与 `--time-tests` 互斥（`cl_arguments.py` 第 551–554 行直接 `parser.error`）。

`Output/` 的结构由 `%t` 公式决定：

```
Output/
└── tests/
    ├── fail/
    │   └── Output/
    │       └── f03-filecheck.test.tmp.txt
    ├── pass/
    │   └── Output/
    └── serial/
        └── Output/
```

注意二级 `Output/`：`test_exec_root` 已经是 `.../Output`，`getTempPaths()` 又在测试所在执行目录下建一个 `Output`。`%t` 文件不会被 lit 自动删除，很多测试自己 `rm -rf %t`。

`LIT_PRESERVES_TMP` 控制的是 `main.execute_in_tmp_dir()` 创建的 `lit-tmp-*` 包装目录（第 290–318 行），不是 `Output/`。真实实验：默认 `TMPDIR=/tmp/lit-tmp-hziu84hq`；设置 `LIT_PRESERVES_TMP=1` 后 `TMPDIR=<unset>`。它适合观察临时文件泄漏，但不会保留 `Output/` 里的 `%t`。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`-o` 是文本日志」 | `-o` 是 `JsonReport`，输出 JSON；文本日志用 `-v/-a`。 |
| 「`-v` 会打印所有通过测试的输出」 | `-v` = `--test-output=failed`，只打印失败测试；全部用 `-a`。 |
| 「`--show-pass` 会改变 Passed 计数」 | 它只把成功测试加进分组列表；摘要计数本来就统计所有 code。 |
| 「xUnit 只包含选中的测试」 | 未选中的测试也会出现，标成 `<skipped message="Test not selected ...">`。 |
| 「`--time-tests` 可以和 `--skip-test-time-recording` 一起用」 | 互斥，argparse 直接报错退出 2。 |
| 「`LIT_PRESERVES_TMP` 会保留 `%t`」 | 它保留的是 `lit-tmp-*` 包装目录；`Output/` 本来就不由它管理。 |
| 「JSON 里有 skipped 测试」 | `JsonReport` 过滤 `EXCLUDED`/`SKIPPED`，只写真正执行过的测试。 |

## 重点整理

- `--test-output` 管命令/输出，`--print-result-after` 管结果行；两者维持 `print_result_after >= test_output`。
- `-v` 只失败、`-a` 全部、`-s` 进度条 + 失败结果行、`-q` 最安静；`-vv` 已废弃。
- `--show-*` 只加成功结果码的分组列表，不改结果、不改摘要计数。
- `-o` 是 JSON：`__version__`（数组）、`elapsed`、`tests[].code/name/output/elapsed`；过滤 excluded/skipped。
- xUnit 是 CI 格式：`testsuites/testsuite/testcase`，failure 用 CDATA，未选中测试是 skipped。
- ResultDB 用 base64 artifact + status 映射；Chrome trace 用 `traceEvents`（微秒）；WTT 是 UTF-16 XML。
- `--report-failures-only` 同时作用于所有 report；`--terse-summary` 实际是「只显示 failure」。
- `.lit_test_times.txt` 负数表示失败；`Output/` 有二级目录；`LIT_PRESERVES_TMP` 管的是 `lit-tmp-*`。

## 自我检核

不看笔记，试着回答：

- [ ] `--test-output` 与 `--print-result-after` 分别控制什么？为什么源码要维持两者的大小关系？
- [ ] `-v`、`-a`、`-s`、`-q` 各展开成哪些设置？`FLAKYPASS` 为什么用 `-v` 看不到输出？
- [ ] `-o` 的 `__version__` 是什么类型？为什么 JSON 里没有 `EXCLUDED` 测试？xUnit 里未选中的测试又长什么样？
- [ ] `--time-tests` 的直方图每行三个部分分别是什么？它和 `--skip-test-time-recording` 为什么互斥？
- [ ] `LIT_PRESERVES_TMP` 与 `Output/` 的关系是什么？`%t` 文件由谁清理？
- [ ] Chrome trace 的 `ts`/`dur` 单位是什么？`pid` 为什么能区分 worker？

## 延伸阅读

- **[`llvm/utils/lit/lit/reports.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/reports.py)**
  - 读什么：`JsonReport`、`XunitReport`、`ResultDBReport`、`TimeTraceReport`、`WttReport`。
  - 能学到：五种输出的精确 schema 与过滤规则。
  - 前提：知道 JSON 与 XML 的基本结构。
- **[`llvm/utils/lit/lit/display.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/display.py)**
  - 读什么：`shouldPrintInfo()`、`Display.update()`、`print_result()`。
  - 能学到：输出层级如何影响每条结果行与测试日志。
  - 前提：读过本章 17.1。
- **[`llvm/utils/lit/lit/TestTimes.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestTimes.py)**
  - 读什么：全文 64 行。
  - 能学到：时间文件的读写、负数约定、malformed 数据的 fatal。
  - 前提：知道 `os.path.join` 与浮点格式化。
- **[`llvm/utils/lit/tests/time-tests.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/time-tests.py)**
  - 读什么：全文与 `Inputs/time-tests/`。
  - 能学到：`--time-tests` 的官方断言，包括 `=1`、`=all`、非法值。
  - 前提：能读 lit 自测的 `RUN:` + `FileCheck` 写法。

> 下一章把输出变成诊断：先看退出码，再看 `# RUN: at line`、`# executed command`、最后一条 `# error`，判断到底是命令没找到、被测程序失败、FileCheck 不匹配、超时，还是 UNRESOLVED/XPASS。

→ [Ch 18 失败诊断学](./18-failure-forensics.md)
