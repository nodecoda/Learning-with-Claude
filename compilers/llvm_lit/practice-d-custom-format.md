# 练习 D — 给 lit 加一个自定义 format

> **目标**：不修改 lit 源码，只写一个 `lit.formats.TestFormat` 子类，让 lit 发现并执行一种全新的测试文件（JSON）。你会亲手实现发现、执行、UNSUPPORTED、超时、stdout 比较，并用一个独立脚本验证它。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。本练习的所有代码与输出均已在本机实跑；lit 入口为 `python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py`。

> **前置**：Ch 2（发现流程）、Ch 20（执行与临时路径）、Ch 23（另一种 format 的完整案例）。如果你还没读过 `lit/formats/base.py`，先花五分钟看 `TestFormat` 与 `FileBasedTest` 的接口。

---

## D.1 任务：JSON 测试格式

我们要支持的测试文件长这样：

```json
{
  "name": "echo-pass",
  "command": "/bin/sh -c 'echo hello-json > %t && cat %t'",
  "expected_exit": 0,
  "expected_stdout": "hello-json"
}
```

字段：

| 字段 | 必填 | 含义 |
|---|---|---|
| `name` | 否 | 人类可读名字；不参与发现，只用于文档 |
| `command` | 是 | 要执行的命令字符串；支持 `%s`、`%t` 等 lit 替换 |
| `expected_exit` | 否 | 期望退出码，默认 `0` |
| `expected_stdout` | 否 | 期望 stdout（忽略末尾换行）；不写则不比较 |
| `timeout` | 否 | 单测试超时秒数；不写则用 `config.maxIndividualTestTime` |
| `requires` | 否 | 布尔表达式列表，映射到 `Test.requires` |
| `unsupported` | 否 | 布尔表达式列表，映射到 `Test.unsupported` |
| `xfail` | 否 | 布尔表达式列表，映射到 `Test.xfails` |

验收标准：

1. `.json` 文件能被发现，`--show-tests` 能看到它们。
2. 退出码不符 -> `FAIL`；stdout 不符 -> `FAIL`；两者都对 -> `PASS`。
3. `unsupported` 命中当前 feature -> `UNSUPPORTED`，命令不执行。
4. `timeout` 到点 -> `TIMEOUT`。
5. 至少写 5 个测试，覆盖上述分支。
6. 有一个独立验证脚本，能在 CI 里跑，并且断言退出码与关键输出。

## D.2 先想清楚接口

`lit.formats.TestFormat`（`llvm/utils/lit/lit/formats/base.py`）只有三个可覆盖的方法：

```python
class TestFormat:
    def getTestsForPath(self, testSuite, path_in_suite, litConfig, localConfig):
        yield lit.Test.Test(testSuite, path_in_suite, localConfig)

    # FileBasedTest 提供：
    def getTestsInDirectory(self, testSuite, path_in_suite, litConfig, localConfig):
        ...
```

我们直接继承 `TestFormat`，自己实现 `getTestsInDirectory()`：

- 对目录里的每个文件，检查后缀、点文件、`excludes`；
- `yield lit.Test.Test(testSuite, path_in_suite + (filename,), localConfig)`。

执行侧需要 `execute(test, litConfig)`。它有两种合法返回：

```python
return lit.Test.PASS, "output"                  # (ResultCode, str)
return lit.Test.Result(lit.Test.FAIL, "output") # 完整 Result
```

`worker.py` 的 `_adapt_result()` 会把 tuple 包成 `Result`。本练习用 tuple 形式，输出更直观。

`parseIntegratedTestScript` 不是 `TestFormat` 的必需方法，但 `ShTest`/`GoogleTest` 会在执行前解析 directive。我们用它做三件事：

- 读 JSON，语法错误 -> `UNRESOLVED`；
- 把 `requires/unsupported/xfail` 填进 `Test` 对象；
- feature 不满足 -> `UNSUPPORTED`，直接短路。

这样 `execute()` 只需要关心命令、退出码、stdout。

## D.3 目录结构

```text
jsontest-lab/
├── lit.cfg.py
├── lit_json_format.py
├── verify.py
└── cases/
    ├── pass.json
    ├── fail-exit.json
    ├── fail-stdout.json
    ├── unsupported.json
    └── timeout.json
```

`lit.cfg.py`：

```python
import os
import sys

import lit.formats

sys.path.insert(0, os.path.dirname(__file__))
import lit_json_format

config.name = "JSONLab"
config.test_format = lit_json_format.JSONTest()
config.suffixes = [".json"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.available_features = {"linux"}
```

两点解释：

- `sys.path.insert(0, os.path.dirname(__file__))`：配置文件是 `exec` 的，不是普通 import，Python 不会自动把 suite 根加进 `sys.path`。不加这一行，`import lit_json_format` 可能失败。
- `config.suffixes = [".json"]`：我们的 `getTestsInDirectory()` 自己检查后缀，但保留 `suffixes` 让 `--show-suites` 和文档更清楚；`standalone_tests` 场景下它也有意义。

## D.4 参考实现（折叠）

先自己写一遍，再展开对照。下面的代码是**完整可运行**版本，已在本机验证。

<details>
<summary>点击展开：lit_json_format.py</summary>

```python
import json
import os
import shlex

import lit.Test
import lit.TestRunner
import lit.formats
import lit.util


class JSONTest(lit.formats.TestFormat):
    """A small format: each .json file describes one command and its oracle."""

    def __init__(self, suffix=".json"):
        self.suffix = suffix

    def getTestsInDirectory(self, testSuite, path_in_suite, litConfig, localConfig):
        source_path = testSuite.getSourcePath(path_in_suite)
        for filename in sorted(os.listdir(source_path)):
            if filename.startswith(".") or not filename.endswith(self.suffix):
                continue
            if filename in localConfig.excludes:
                continue
            yield lit.Test.Test(
                testSuite, path_in_suite + (filename,), localConfig
            )

    def _load(self, test):
        with open(test.getSourcePath(), encoding="utf-8") as f:
            spec = json.load(f)
        if not isinstance(spec, dict):
            raise ValueError("top-level JSON value must be an object")
        if "command" not in spec:
            raise ValueError("missing 'command'")
        return spec

    def parseIntegratedTestScript(self, test):
        """Map JSON feature fields onto lit's Test object.

        Returning an empty list means "this format has no shell script".
        A Result return value short-circuits execution (UNSUPPORTED/UNRESOLVED).
        """
        try:
            spec = self._load(test)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return lit.Test.Result(lit.Test.UNRESOLVED, "invalid JSON test: %s" % e)

        test.xfails += spec.get("xfail", [])
        test.requires += spec.get("requires", [])
        test.unsupported += spec.get("unsupported", [])

        missing = test.getMissingRequiredFeatures()
        if missing:
            return lit.Test.Result(
                lit.Test.UNSUPPORTED,
                "missing required features: %s" % ", ".join(missing),
            )
        unsupported = test.getUnsupportedFeatures()
        if unsupported:
            return lit.Test.Result(
                lit.Test.UNSUPPORTED,
                "unsupported features: %s" % ", ".join(unsupported),
            )
        return []

    def execute(self, test, litConfig):
        if test.config.unsupported:
            return lit.Test.UNSUPPORTED, "Test is unsupported"

        parsed = self.parseIntegratedTestScript(test)
        if isinstance(parsed, lit.Test.Result):
            return parsed

        spec = self._load(test)
        tmpDir, tmpBase = lit.TestRunner.getTempPaths(test)
        os.makedirs(tmpDir, exist_ok=True)
        substitutions = lit.TestRunner.getDefaultSubstitutions(
            test, tmpDir, tmpBase
        )
        conditions = {f: True for f in test.config.available_features}
        (command_line,) = lit.TestRunner.applySubstitutions(
            [spec["command"]], substitutions, conditions,
            recursion_limit=test.config.recursiveExpansionLimit,
        )
        cmd = shlex.split(command_line)
        if not cmd:
            return lit.Test.UNRESOLVED, "empty command"

        timeout = spec.get("timeout", test.config.maxIndividualTestTime) or 0
        try:
            out, err, exitCode = lit.util.executeCommand(
                cmd,
                cwd=os.path.dirname(test.getSourcePath()),
                env=test.config.environment,
                timeout=timeout,
            )
        except lit.util.ExecuteCommandTimeoutException as e:
            return lit.Test.TIMEOUT, "Reached timeout of %s seconds\n%s" % (
                timeout, e.out,
            )

        expected_exit = spec.get("expected_exit", 0)
        expected_stdout = spec.get("expected_stdout")
        problems = []
        if exitCode != expected_exit:
            problems.append("exit code: expected %d, got %d" % (expected_exit, exitCode))
        if expected_stdout is not None and out.rstrip("\n") != expected_stdout.rstrip("\n"):
            problems.append(
                "stdout mismatch:\n--- expected\n%s\n--- actual\n%s"
                % (expected_stdout.rstrip("\n"), out.rstrip("\n"))
            )

        output = "Command: %s\nExit Code: %d\n" % (command_line, exitCode)
        if out:
            output += "--- stdout\n%s\n" % out
        if err:
            output += "--- stderr\n%s\n" % err
        if problems:
            return lit.Test.FAIL, output + "\n".join(problems)
        return lit.Test.PASS, output
```

</details>

三个设计决定值得说明：

1. **复用 `getDefaultSubstitutions()` 与 `applySubstitutions()`**。不要自己实现 `%t`；这两个函数已经处理了 `%s/%S/%t/%basename_t/%{s:stem}` 和 `%if`。注意 `applySubstitutions()` 接受「指令或字符串」列表，所以我们传 `[spec["command"]]`，取出单元素结果。
2. **`executeCommand(..., timeout=0)` 表示不超时**。`lit.util.executeCommand` 内部判断 `if timeout > 0:`；传 `None` 会 `TypeError`（我们第一次实现就踩了这个坑）。
3. **stdout 比较忽略末尾换行**。`out.rstrip("\n")` 与 `expected_stdout.rstrip("\n")` 比较；`echo` 的末尾换行不应该导致误报。中间的空行仍然敏感。

## D.5 五个测试文件

`cases/pass.json`：

```json
{
  "name": "echo-pass",
  "command": "/bin/sh -c 'echo hello-json > %t && cat %t'",
  "expected_exit": 0,
  "expected_stdout": "hello-json"
}
```

`cases/fail-exit.json`：

```json
{
  "name": "wrong-exit",
  "command": "/bin/sh -c 'exit 3'",
  "expected_exit": 0
}
```

`cases/fail-stdout.json`：

```json
{
  "name": "wrong-stdout",
  "command": "/bin/echo actual",
  "expected_stdout": "expected"
}
```

`cases/unsupported.json`：

```json
{
  "name": "unsupported-here",
  "command": "/bin/echo never",
  "unsupported": ["linux"]
}
```

`cases/timeout.json`：

```json
{
  "name": "too-slow",
  "command": "/bin/sleep 2",
  "timeout": 1
}
```

`pass.json` 同时验证了 `%t` 替换：命令把内容写进临时文件再读回来。`unsupported.json` 依赖 suite 的 `available_features={"linux"}`；如果把那一行删掉，它会变成 `PASS`，因为 `UNSUPPORTED: linux` 不再命中。

## D.6 运行：发现与执行

```bash
cd jsontest-lab
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --show-tests cases
```

真实输出：

```text
-- Available Tests --
  JSONLab :: cases/fail-exit.json
  JSONLab :: cases/fail-stdout.json
  JSONLab :: cases/pass.json
  JSONLab :: cases/timeout.json
  JSONLab :: cases/unsupported.json
```

执行：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -v cases
```

真实输出：

```text
-- Testing: 5 tests, 5 workers --
FAIL: JSONLab :: cases/fail-stdout.json (1 of 5)
******************** TEST 'JSONLab :: cases/fail-stdout.json' FAILED ********************
Command: /bin/echo actual
Exit Code: 0
--- stdout
actual

stdout mismatch:
--- expected
expected
--- actual
actual
********************
UNSUPPORTED: JSONLab :: cases/unsupported.json (2 of 5)
PASS: JSONLab :: cases/pass.json (3 of 5)
TIMEOUT: JSONLab :: cases/timeout.json (4 of 5)
******************** TEST 'JSONLab :: cases/timeout.json' FAILED ********************
Reached timeout of 1 seconds
********************
FAIL: JSONLab :: cases/fail-exit.json (5 of 5)
******************** TEST 'JSONLab :: cases/fail-exit.json' FAILED ********************
Command: /bin/sh -c 'exit 3'
Exit Code: 3
exit code: expected 0, got 3
********************

Total Discovered Tests: 5
  Unsupported: 1 (20.00%)
  Passed     : 1 (20.00%)
  Timed Out  : 1 (20.00%)
  Failed     : 2 (40.00%)
```

退出码是 1：

```bash
echo $?
# 1
```

只跑通过的测试：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --filter='pass\.json' cases
```

真实输出：

```text
-- Testing: 1 of 5 tests, 1 workers --
PASS: JSONLab :: cases/pass.json (1 of 1)

Total Discovered Tests: 5
  Excluded: 4 (80.00%)
  Passed  : 1 (20.00%)
```

## D.7 独立验证脚本

把验收标准写成脚本，CI 里直接跑 `python3 verify.py`：

```python
#!/usr/bin/env python3
"""Independent acceptance check for the JSON test format."""
import os
import subprocess
import sys

LIT = ["python3", "/home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py"]
HERE = os.path.dirname(os.path.abspath(__file__))


def run(args, expect):
    p = subprocess.run(LIT + args, cwd=HERE, capture_output=True, text=True)
    print("$ lit " + " ".join(args))
    print(p.stdout.rstrip())
    if p.returncode != expect:
        print("expected exit %d, got %d" % (expect, p.returncode), file=sys.stderr)
        sys.exit(1)
    return p.stdout


discovered = run(["--show-tests", "cases"], 0)
for name in ("pass.json", "fail-exit.json", "fail-stdout.json",
             "unsupported.json", "timeout.json"):
    assert name in discovered, name
assert discovered.count("JSONLab ::") == 5

full = run(["-v", "cases"], 1)
for code in ("PASS:", "FAIL:", "UNSUPPORTED:", "TIMEOUT:"):
    assert code in full, code

run(["--filter=pass\\.json", "cases"], 0)
print("\nall checks passed")
```

真实输出（节选）：

```text
$ python3 verify.py
$ lit --show-tests cases
-- Available Tests --
  JSONLab :: cases/fail-exit.json
  JSONLab :: cases/fail-stdout.json
  JSONLab :: cases/pass.json
  JSONLab :: cases/timeout.json
  JSONLab :: cases/unsupported.json
$ lit -v cases
...
  Failed     : 2 (40.00%)
$ lit --filter=pass\.json cases
...
  Passed  : 1 (20.00%)

all checks passed
```

脚本的断言点：

- `--show-tests` 必须退出 0，且恰好 5 个以 `JSONLab ::` 开头的测试。
- 全量运行必须退出 1，且输出里同时出现 `PASS:`、`FAIL:`、`UNSUPPORTED:`、`TIMEOUT:`。
- 过滤后必须退出 0。

这比肉眼检查可靠：即使某天 lit 的摘要格式变了，只要状态码和测试名还在，断言就仍然有意义。

## D.8 想再进一步：lit 自测风格

`llvm/utils/lit/tests/` 里的测试通常长这样（示意，不是本练习必须完成的部分）：

```text
# RUN: %{lit} %{inputs}/my-format --show-tests | FileCheck %s
# CHECK: MyFormat :: one.json
```

如果你要把这个 format 贡献给上游，应该：

1. 在 `llvm/utils/lit/lit/formats/` 下新增实现；
2. 在 `formats/__init__.py` 导出；
3. 在 `llvm/utils/lit/tests/Inputs/` 放测试数据；
4. 在 `llvm/utils/lit/tests/` 写一个 `.py`，用 `%{lit}` + FileCheck 断言；
5. 跑 `llvm/utils/lit/tests/lit.cfg` 下的自测。

本练习的 `verify.py` 是「外部验证」的简化版；它不依赖 FileCheck，适合自建项目。两种方式都真实有效，选一种做到位即可。

## D.9 可以继续做的扩展

- **支持 `command` 为列表**：`["/bin/echo", "hello world"]`，避免 `shlex.split` 对带空格路径的误切。
- **支持 `expected_stderr`**：与 stdout 同样的比较逻辑。
- **支持 `env` 字段**：在 `executeCommand` 前合并到 `test.config.environment` 的副本。
- **支持 `files` 字段**：在临时目录里生成输入文件，测试结束后清理。
- **避免重复解析**：`execute()` 现在调用 `parseIntegratedTestScript()` 后又 `_load()` 一次；可以把 spec 缓存到 `test` 上（注意 `Test` 会被 pickle 到 worker，缓存必须是可序列化的）。
- **`getTestsForPath`**：当前没有覆盖，所以 `lit cases/pass.json` 走基类实现，直接创建一个 `Test`（已实测为 `PASS`），`execute()` 仍能工作。代价是单文件输入会绕过 `getTestsInDirectory()` 的后缀/`excludes` 检查；要完全一致就实现 `getTestsForPath`。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`timeout=None` 表示不超时」 | `executeCommand` 用 `if timeout > 0` 判断；传 `None` 会 `TypeError`。不超时传 `0`，这也是我们第一次运行的失败原因 |
| 「`config.available_features` 会自动包含平台名」 | 不会。`unsupported.json` 能变 `UNSUPPORTED`，是因为 suite 显式写了 `config.available_features={"linux"}` |
| 「`applySubstitutions()` 接受单个字符串」 | 它接受指令/字符串的**列表**，返回列表；传字符串会按字符迭代。我们传 `[spec["command"]]` 再解包 |
| 「`getTestsInDirectory` 返回文件名就够」 | 必须 `yield lit.Test.Test(...)`；只返回字符串，lit 在 `worker` 里会找不到 `.config` |
| 「`execute()` 抛异常也会显示成 FAIL」 | `worker._execute_test_handle_errors()` 会把未捕获异常转成 `UNRESOLVED`，不是 `FAIL`。要 FAIL 就显式返回 `lit.Test.FAIL` |
| 「`sys.path` 自动包含 suite 根」 | 配置文件是 `exec` 的，不是 import；要 `sys.path.insert(0, os.path.dirname(__file__))` |
| 「stdout 必须逐字节相等」 | 我们忽略末尾换行；如果你需要严格相等，去掉 `rstrip("\n")`，并在文档里写清楚 |

## 重点整理

1. 自定义 format 的最小接口是 `getTestsInDirectory()` + `execute()`；`parseIntegratedTestScript()` 可选，但用它处理 feature 最自然。
2. `execute()` 可以返回 `(ResultCode, output)` tuple，也可以返回 `lit.Test.Result`；`worker._adapt_result()` 统一包装。
3. 复用 `getTempPaths()`、`getDefaultSubstitutions()`、`applySubstitutions()`，不要自己实现 `%t` 和 `%if`。
4. `executeCommand(timeout=0)` 表示不超时；`ExecuteCommandTimeoutException` 要显式捕获并返回 `TIMEOUT`。
5. `UNSUPPORTED` 应在执行前短路；`parseIntegratedTestScript()` 返回 `Result` 时，`execute()` 直接把它透传。
6. 比较 stdout 时明确是否忽略末尾换行；把规则写进测试文档，避免「本机过、CI 不过」。
7. 独立验证脚本至少断言：发现数量、整体退出码、各状态码出现、过滤后的退出码。
8. 如果要把 format 上游化，还需要 `formats/__init__.py` 导出、`tests/Inputs` 数据、lit 自测与文档。

## 自我检核

- [ ] 不看参考实现，写出 `JSONTest` 的三个核心方法签名，并说明每个方法在发现/执行流程中的位置。
- [ ] 为什么 `timeout` 要传 `0` 而不是 `None`？从 `lit.util.executeCommand` 的实现解释。
- [ ] `unsupported.json` 在什么条件下会变成 `PASS`？需要改哪个配置字段？
- [ ] 你的 `execute()` 如果直接 `raise RuntimeError`，lit 最终显示什么状态？为什么不是 `FAIL`？
- [ ] 给 `expected_stdout` 增加「忽略行尾空格」的选项，你会改哪一行？会不会影响现有测试？
- [ ] 如何把这个 format 接进 CI，使它在测试失败时既返回非零退出码、又产出可读的失败原因？

## 延伸阅读

- **`llvm/utils/lit/lit/formats/base.py`**
  - 读哪里：`TestFormat`、`FileBasedTest`、`ExecutableTest`。
  - 能学到什么：最小接口、`getTestsForPath` 与 `getTestsInDirectory` 的分工、返回值约定。
  - 前提知识：本章 D.2；Ch 2 的发现流程。
- **`llvm/utils/lit/lit/formats/googletest.py`**
  - 读哪里：`execute()`、`prepareCmd()`、`post_process_shard_results()`。
  - 能学到什么：一个成熟 format 如何处理外部进程、结构化输出、超时与结果拆分。
  - 前提知识：Ch 23。
- **`llvm/utils/lit/lit/worker.py`**
  - 读哪里：`_execute_test_handle_errors()`、`_adapt_result()`。
  - 能学到什么：异常如何变成 `UNRESOLVED`、tuple 如何变成 `Result`、`elapsed/pid/start` 在哪里填。
  - 前提知识：本章 D.2、D.4。
- **`llvm/utils/lit/tests/Inputs/` 与 `llvm/utils/lit/tests/*.py`**
  - 读哪里：官方 format 测试的数据布局与断言方式。
  - 能学到什么：如何把「发现 + 执行 + 状态码」写成可回归的 lit 自测。
  - 前提知识：Ch 25 会讲 lit 自测的运行方式；本练习的 `verify.py` 是更轻量的替代。

下一章把「一个能跑的 format」升级成「一个能维护的 suite」：目录分层、配置复用、feature 设计、CI 矩阵、版本升级与弃用策略。

→ [Ch 24 自建 suite 的工程化](./24-production-suite.md)
