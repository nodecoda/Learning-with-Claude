# Ch 11 — 从零写 lit.cfg

> **目标**：能从一份不到 10 行的 `lit.cfg.py` 开始，按需加上每个字段，最终得到一个可复制、可诊断、能进 CI 的 toy suite；并且知道每个字段写错时 lit 会怎么坏。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`。本章所有 suite 都在 `/tmp` 下真实创建并运行；输出里的路径是当次实验的真实路径。字段默认值以 `TestingConfig.py` 的 `fromdefaults()`/`__init__()` 为准。

---

## 11.1 先跑起来：8 行配置

一个 suite 的最小闭环只需要「配置 + 测试文件」。下面这份配置去掉空行正好 8 行：

```python
import os
import lit.formats

config.name = "Toy"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.excludes = ["Inputs"]
```

加一个 `tests/hello.test`：

```text
# RUN: echo "hello, lit"
```

跑：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py tests
```

真实输出：

```
-- Testing: 1 tests, 1 workers --
PASS: Toy :: tests/hello.test (1 of 1)

Testing Time: 0.01s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

这 8 行已经能工作，但它把所有东西都写死了：excludes 只有固定一项，环境、替换、重试、超时都还没有。下一步不是一次抄完 LLVM 的 900 行配置，而是按需求逐项加。

## 11.2 加到「生产可用」：一份完整 toy suite

下面这份配置覆盖了本章要讲的字段，可以直接复制运行。目录结构：

```
lit-toy/
├── lit.cfg.py
└── tests/
    ├── hello.test
    └── Inputs/
        └── input.txt
```

`lit.cfg.py`：

```python
import os
import lit.formats

config.name = "Toy"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test", ".toy"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.excludes = ["Inputs"]
config.substitutions.append(("%toy_echo", "echo toy"))
config.environment["TOY_ROOT"] = config.test_source_root
config.pipefail = True
config.standalone_tests = False
config.parallelism_group = None
config.test_retry_attempts = 0
config.maxIndividualTestTime = 0
config.unsupported = False
config.limit_to_features = set()
config.recursiveExpansionLimit = 10
config.available_features.add("toy")
```

`tests/hello.test`：

```text
# RUN: %toy_echo
# RUN: printenv TOY_ROOT
```

`tests/Inputs/input.txt` 放任意内容，它不应该被发现。

先看 lit 眼里的 suite：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --show-suites tests
```

真实输出：

```
-- Test Suites --
  Toy - 1 tests
    Source Root: /tmp/lit-ch11-final.e972kP
    Exec Root  : /tmp/lit-ch11-final.e972kP/Output
    Available Features: toy
    Available Substitutions: %toy_echo => echo toy
```

再跑：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -a tests
```

真实输出（节选）：

```
-- Testing: 1 tests, 1 workers --
PASS: Toy :: tests/hello.test (1 of 1)
Exit Code: 0

Command Output (stdout):
--
# RUN: at line 1
echo toy
# executed command: echo toy
# .---command stdout------------
# | toy
# `-----------------------------
# RUN: at line 2
printenv TOY_ROOT
# executed command: printenv TOY_ROOT
# .---command stdout------------
# | /tmp/lit-ch11-final.e972kP
# `-----------------------------
```

`Inputs/input.txt` 没有出现在 `1 tests` 里，说明 `excludes` 生效。`%toy_echo` 在 `# executed command` 里已经变成 `echo toy`，说明替换生效。

## 11.3 每个字段：默认值、何时必须写、写错的症状

先给总表，后面用实验展开其中几个。

### 11.3.1 必需字段

| 字段 | 默认值 | 什么时候必须写 | 写错的症状 |
|---|---|---|---|
| `name` | `"<unnamed>"` | 几乎总是 | 报告里全是 `<unnamed> :: ...`，多 suite 时无法区分 |
| `test_format` | `None` | 总是（除非 `standalone_tests=True`） | 目录扫描发现 0 个测试，退出码 2；直接传文件则 `UNRESOLVED` + `NoneType has no attribute 'execute'` |
| `suffixes` | `set()` | 用 `FileBasedTest` 系格式时总是 | 发现 0 个测试，`warning: input 'tests' contained no tests`，退出码 2 |
| `test_source_root` | `None` → 发现路径 | 源码树与构建树分离时 | 测试名/相对路径错位，或根本找不到测试 |
| `test_exec_root` | `None` → 发现路径 | 想保持源码树干净时 | `Output/`、`%t` 写进源码目录；多构建互相踩 |

### 11.3.2 行为字段

| 字段 | 默认值 | 什么时候必须写 | 写错的症状 |
|---|---|---|---|
| `excludes` | `set()` | 有 `Inputs/`、`CMakeLists.txt` 等辅助文件 | 辅助文件被当测试；写 glob（`*.tmp`）不会排除任何东西 |
| `substitutions` | `[]` | 测试里要用 `%tool` 简写 | `%tool` 原样进 shell，通常 `command not found` |
| `environment` | 白名单 + PATH + `LLVM_DISABLE_CRASH_REPORT=1` | 测试需要自定义变量 | 测试里 `printenv` 失败或拿到空值 |
| `pipefail` | `True` | 一般不要改 | 写成 `False` 后 `false \| true` 会 PASS，中间失败被掩盖 |
| `standalone_tests` | `False` | 目录只接受显式单文件运行 | 与 `suffixes`/`excludes` 同时设置会 warning，且不发现任何测试 |
| `parallelism_group` | `None` | 需要限制某组测试并发 | 组名没在 `lit_config.parallelism_groups` 注册时退化为不限制 |
| `test_retry_attempts` | 未设置 → `0` | 需要全局重试 flaky 测试 | 写 `0` 会压过 `--max-retries-per-test`；`ALLOW_RETRIES:` 又压过它 |
| `maxIndividualTestTime` | `0` | 需要防单个测试挂死 | 超时太小会把慢测试打成 `TIMEOUT`；`--timeout` 会覆盖它 |
| `unsupported` | `False` | 整个 suite 在当前配置下不适用 | 设成 `True` 后所有测试直接 `UNSUPPORTED` |
| `limit_to_features` | `set()` | 只想跑带某 feature 的测试 | 非空时，不 `REQUIRES` 这些 feature 的测试全部 `UNSUPPORTED` |
| `recursiveExpansionLimit` | `None` | 防止替换无限递归 | 负数或非整数直接 `ValueError`；太小会让合法嵌套替换提前报错 |

### 11.3.3 `test_source_root` / `test_exec_root` 为什么必须分开

LLVM 的做法：

```python
# llvm/test/lit.cfg.py:93-96
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.llvm_obj_root, "test")
```

- `test_source_root`：**读**测试文件的地方。
- `test_exec_root`：**写** `%t`、`Output/`、临时文件的地方。

写反的典型症状：`--show-suites` 里 `Source Root` 指向构建目录，lit 去那里找 `.test`，结果 0 个测试；或者 `Exec Root` 指向源码树，跑一次测试后 `git status` 里多出一堆 `Output/`。

## 11.4 五个真实翻车实验

### 11.4.1 忘写 `suffixes`

把 `config.suffixes` 改成 `[]`，再跑：

```
lit.py: .../discovery.py:273: warning: input 'tests' contained no tests
error: did not discover any tests for provided path(s)
EXIT=2
```

原因在 `FileBasedTest.getTestsForPath()`（`formats/base.py:23-36`）：

```python
if filename.startswith(".") or filename in localConfig.excludes:
    return
if any(filename.endswith(suffix) for suffix in localConfig.suffixes):
    yield lit.Test.Test(...)
```

`any(...)` 对空集合永远是 `False`，所以一个文件都不会 yield。注意它是 `endswith`，不是扩展名语义：`suffixes=[".test"]` 也会匹配 `foo.notatest`。

### 11.4.2 忘写 `test_format`

把 `config.test_format` 改成 `None`，按目录跑：

```
warning: input 'tests' contained no tests
error: did not discover any tests for provided path(s)
EXIT=2
```

直接传文件则会「发现」它，因为 `discovery.py:167-168` 对显式文件有特判：

```python
tests = [Test.Test(ts, path_in_suite, lc)] if lc.test_format is None or lc.standalone_tests else \
        lc.test_format.getTestsForPath(...)
```

执行时在 worker 里炸：

```
UNRESOLVED: Toy :: tests/hello.test (1 of 1)
...
AttributeError: 'NoneType' object has no attribute 'execute'
EXIT=1
```

这两个症状不同：目录扫描是「发现阶段 0 个测试」，显式文件是「执行阶段 UNRESOLVED」。

### 11.4.3 把 `excludes` 当 glob

配置写：

```python
config.excludes = ["*.tmp"]
config.suffixes = [".test", ".tmp"]
```

放一个 `tests/should-be-excluded.tmp`，跑 `--show-tests`：

```
-- Available Tests --
  Toy :: tests/hello.test
  Toy :: tests/should-be-excluded.tmp
```

`excludes` 只做 `filename in excludes`（`formats/base.py:33`），不是 `fnmatch`。要排除后缀，靠 `suffixes` 不包含它；要排除固定文件名，写完整名字；要排除目录，写目录名（递归时 `discovery.py:195` 用 `filename in lc.excludes` 判断目录）。

### 11.4.4 `pipefail=False` 掩盖中间失败

测试：

```text
# RUN: false | true
```

`config.pipefail = True`（默认）：

```
FAIL: Toy :: tests/pipe.test (1 of 1)
...
# executed command: false
# error: command failed with exit status: 1
# executed command: true
EXIT=1
```

改成 `False`：

```
PASS: Toy :: tests/pipe.test (1 of 1)
...
# executed command: false
# error: command failed with exit status: 1
# executed command: true
EXIT=0
```

注意 `-a` 里仍然打印 `error: command failed`，但整体结果变成 PASS。`pipefail` 在 `TestingConfig.py:205` 保存，内置 shell 在 `TestRunner.py:1045` 用它解析管道；外部 shell 则在 `TestRunner.py:1250-1251` 生成 `set -o pipefail`。

### 11.4.5 `test_retry_attempts` 与 `ALLOW_RETRIES` 的优先级

写一个「第一次失败、第二次成功」的脚本 `tests/retry.py`：

```python
import os, sys
state = sys.argv[1]
if os.path.exists(state):
    print("second attempt: pass")
    sys.exit(0)
open(state, "w").write("x")
print("first attempt: fail")
sys.exit(1)
```

配置 `config.test_retry_attempts = 3`，测试里写 `ALLOW_RETRIES: 0`：

```
FAIL: Toy :: tests/retry.test (1 of 1)
# | first attempt: fail
Failed: 1
```

去掉 `ALLOW_RETRIES: 0`，同样的配置：

```
PASS: Toy :: tests/retry.test (1 of 1)
# | second attempt: pass
Passed: 1
```

优先级从高到低：

1. 测试文件的 `ALLOW_RETRIES:`（`TestRunner.py:2132-2133` 直接覆盖 `test.allowed_retries`）。
2. `config.test_retry_attempts`（`Test.py:281` 用它初始化 `allowed_retries`）。
3. 命令行 `--max-retries-per-test`（`TestingConfig.py:251-255` 只在配置没写时兜底）。
4. 默认 `0`。

再验证第 2 条压过第 3 条：配置写 `test_retry_attempts = 0`，命令行给 `--max-retries-per-test=2`，测试仍然只跑一次并 `FAIL`。这与 `cl_arguments.py:361-367` 的帮助文字一致：「config option and ALLOWED_RETRIES keyword always take precedence」。

### 11.4.6 两个「整组不跑」的开关

`standalone_tests=True` 且同时保留了 `suffixes`/`excludes`：

```
warning: standalone_tests set in LIT config but suffixes or excludes are also set
warning: input 'tests' contained no tests
EXIT=2
```

`limit_to_features={"foo"}`，两个测试分别有/没有 `REQUIRES: foo`：

```
UNSUPPORTED: Toy :: tests/foo.test (1 of 2)
Test requires the following unavailable features: foo
UNSUPPORTED: Toy :: tests/plain.test (2 of 2)
Test does not require any of the features specified in limit_to_features: foo
```

`limit_to_features` 的检查在 `TestRunner.py:2153-2161`。它和 `unsupported=True` 的区别：前者按测试的 `REQUIRES` 过滤，后者整个 suite 直接短路（`TestRunner.py:2280-2281`）。

最后，`recursiveExpansionLimit = -1` 在配置加载阶段就崩：

```
ValueError: recursiveExpansionLimit must be a non-negative integer (got <-1>)
EXIT=2
```

setter 在 `TestingConfig.py:219-233`，非 `int` 或负数都拒绝。

## 11.5 `lit.formats.ShTest()` 的四个参数

源码在 `formats/shtest.py:19-35`：

```python
def __init__(
    self,
    execute_external=False,
    extra_substitutions=[],
    preamble_commands=[],
    force_execute_external=False,
):
    if execute_external and not force_execute_external:
        raise ValueError(
            "execute_external=True is deprected as of LLVM-23 ..."
        )
```

逐项说：

| 参数 | 默认 | 作用 | 注意 |
|---|---|---|---|
| `execute_external` | `False` | 用真正的 bash 跑脚本 | LLVM 23 起废弃；单独设为 `True` 直接抛 `ValueError` |
| `force_execute_external` | `False` | 迁移期逃生门，允许 `execute_external=True` | 只在确实需要外部 shell 时用；内置 shell 才是默认路径 |
| `extra_substitutions` | `[]` | 在默认替换之前追加的替换表 | 适合给某个 format 实例加私有替换 |
| `preamble_commands` | `[]` | 每个测试执行前先跑的命令 | 有 preamble 时，测试文件可以没有 `RUN:` 也算通过 |

实验：

```python
config.test_format = lit.formats.ShTest(
    extra_substitutions=[("%extra", "echo EXTRA")],
    preamble_commands=["echo PREAMBLE"],
)
```

`tests/extra.test` 只有 `# RUN: %extra`，`tests/norun.test` 没有 `RUN:`。`-a` 真实输出（节选）：

```
PASS: ShTestLab :: tests/extra.test (1 of 2)
# preamble command line
echo PREAMBLE
# | PREAMBLE
# RUN: at line 1
echo EXTRA
# | EXTRA
PASS: ShTestLab :: tests/norun.test (2 of 2)
# preamble command line
echo PREAMBLE
# | PREAMBLE
```

`preamble` 先执行，然后才是 `RUN:`。`norun.test` 能 PASS，是因为 `executeShTest()` 在 `require_script=not script` 时允许脚本为空（`TestRunner.py:2283-2287`）。

单独写 `execute_external=True` 的真实报错：

```
ValueError: execute_external=True is deprected as of LLVM-23 and the option will
be removed in LLVM-24. Please move to using the internal shell
(execute_external=False). If you still need to force external execution to allow
time for migration, set force_execute_external=True
EXIT=2
```

## 11.6 `config.environment` 与 `with_environment`

`config.environment` 是普通 dict。直接赋值就是覆盖：

```python
config.environment["TOY_ROOT"] = config.test_source_root
```

`llvm_config.with_environment()` 是另一个东西（`llvm/config.py:239-270`）：当 `append_path=True` 时，它把值按 `os.pathsep` 拆开、规范化、去重，再把新路径放到**最前面**。

实验：

```python
from lit.llvm.config import LLVMConfig
llvm_config = LLVMConfig(lit_config, config)
config.environment["PATH"] = "/a:/b"
llvm_config.with_environment("PATH", ["/b", "/c"], append_path=True)
lit_config.note("PATH=%r" % config.environment["PATH"])
```

真实输出：

```
lit.py: .../lit.cfg.py:13: note: PATH='/b:/c:/a'
```

`/b` 被去重并提到最前，`/c` 插在它后面，`/a` 留在末尾。直接赋值不会做这些：

```python
config.environment["FLAT"] = "one"
config.environment["FLAT"] = "two"
# FLAT='two'
```

什么时候用哪个：

- 单个变量、明确要覆盖：`config.environment["X"] = ...`。
- PATH/LD_LIBRARY_PATH 这类路径列表，想保留原值并加目录：`llvm_config.with_environment(..., append_path=True)`。
- 想从宿主拿变量：`llvm_config.with_system_environment(["HOME", "TMP"])`。

> 警告：实验里把 PATH 改成 `/a:/b` 后，连 `true` 都找不到了（因为 `true` 是外部命令，内置 shell 不内建它），测试报 `'true': command not found`。改 PATH 前先想清楚你在替换什么。

## 11.7 进 CI 前的检查清单

1. `--show-suites` 的 `Source Root` / `Exec Root` 是你要的两个目录。
2. `--show-tests` 的数量与预期一致；`Inputs/` 没有被算进去。
3. `suffixes` 用 `endswith` 语义检查一遍，没有把 `foo.notatest` 误收。
4. `excludes` 里没有 glob；目录名和文件名都写的是完整名字。
5. `pipefail` 没有被改成 `False`。
6. `test_retry_attempts`、`ALLOW_RETRIES`、`--max-retries-per-test` 三者关系清楚。
7. `%t` 落在 `test_exec_root` 下，源码树 `git status` 干净。
8. 环境变量要么在白名单，要么显式写入，要么用 `--pass-env`。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「文件放进去就会被发现」 | 只有 `suffixes` 匹配、且不在 `excludes` 里的文件才会被发现。`suffixes` 是 `endswith`，不是扩展名。 |
| 「`test_format` 不写也能跑」 | 不写就是 `None`：目录扫描 0 个测试；显式传文件会 `UNRESOLVED` + `AttributeError`。 |
| 「`source_root` 和 `exec_root` 差不多」 | 一个读、一个写。写反会导致找不到测试，或把 `Output/` 写进源码树。 |
| 「`excludes` 支持 `*.tmp`」 | 只做 `filename in excludes`。glob 不生效。 |
| 「`pipefail` 只是 shell 细节」 | `False` 会让 `false \| true` 通过，直接掩盖流水线中间失败。 |
| 「`test_retry_attempts` 最大」 | 测试里的 `ALLOW_RETRIES:` 最大；其次是 `config.test_retry_attempts`；最后才是 `--max-retries-per-test`。 |
| 「`standalone_tests` 只是少扫目录」 | 它和 `suffixes`/`excludes` 同时设置会 warning，并且目录下什么都不发现。 |
| 「`parallelism_group` 写个名字就能限流」 | 还要在 `lit_config.parallelism_groups` 里注册上限；否则 `worker.py:72-76` 拿不到信号量，退化为不限制。 |

## 重点整理

- 最小配置 8 行就能跑：`name`、`ShTest()`、`suffixes`、两个 root。
- 生产配置按需加：`excludes`、`substitutions`、`environment`、`pipefail`、`standalone_tests`、`parallelism_group`、`test_retry_attempts`、`maxIndividualTestTime`、`unsupported`、`limit_to_features`、`recursiveExpansionLimit`。
- 最常见的三种「0 tests」：`suffixes` 为空、`test_format` 为 `None`、`source_root` 指错。
- `excludes` 是精确名字集合；`suffixes` 是 `endswith`；两者都不是 glob。
- `pipefail=True` 是默认值，也是正确值；改 `False` 只会让失败更难发现。
- 重试优先级：`ALLOW_RETRIES:` > `config.test_retry_attempts` > `--max-retries-per-test` > `0`。
- `ShTest` 默认内置 shell；`execute_external=True` 已废弃，必须配 `force_execute_external=True` 才能用。
- `config.environment` 是直接赋值；`with_environment(..., append_path=True)` 才是「加路径、去重、放前面」。

## 自我检核

不看笔记，试着回答：

- [ ] 你的 suite 跑出 `warning: input 'tests' contained no tests`，按可能性从高到低列出三个检查点。
- [ ] 为什么 `test_format=None` 时，按目录跑是 0 个测试，直接传文件却是 `UNRESOLVED`？两条路径分别在源码哪一行分叉？
- [ ] `excludes=["Inputs"]` 和 `excludes=["*.tmp"]` 分别会发生什么？递归代码里哪一行判断目录、哪一行判断文件？
- [ ] `false | true` 在 `pipefail=True/False` 下的结果分别是什么？`-a` 输出里哪一行不会变？
- [ ] 配置写 `test_retry_attempts=3`，测试写 `ALLOW_RETRIES: 0`，命令行给 `--max-retries-per-test=5`，最终跑几次？为什么？
- [ ] 什么场景该用 `config.environment["PATH"] = ...`，什么场景该用 `with_environment(..., append_path=True)`？各有什么风险？

## 延伸阅读

- **[`llvm/utils/lit/lit/TestingConfig.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestingConfig.py)**
  - 读什么：`fromdefaults()` 的默认值、`__init__()` 的字段、`finish()` 的归一化，全文 303 行。
  - 能学到：本章每个默认值的出处，以及 `test_retry_attempts`/`maxIndividualTestTime` 的覆盖顺序。
  - 前提：读完本章 11.3；知道 Python 的 `set`/`dict`/`list`。
- **[`llvm/utils/lit/lit/formats/base.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/formats/base.py)**
  - 读什么：`FileBasedTest.getTestsForPath()` 与 `getTestsInDirectory()`（约 23–50 行）。
  - 能学到：`suffixes` 的 `endswith` 语义、`excludes` 的精确匹配、为什么点文件永远不发现。
  - 前提：读完本章 11.4.1–11.4.3。
- **[`llvm/utils/lit/lit/formats/shtest.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/formats/shtest.py)**
  - 读什么：整个文件（40 行左右）。
  - 能学到：四个构造参数的签名、`execute_external` 的废弃检查、`execute()` 如何转到 `TestRunner.executeShTest()`。
  - 前提：知道 `ShTest` 是「一个文件 = 一个测试」。
- **[`llvm/test/lit.cfg.py`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.cfg.py)**
  - 读什么：前 100 行（name/format/suffixes/excludes/roots/PATH），再跳到 490 行附近的 targets/tool substitutions。
  - 能学到：一个成熟 suite 怎么把本章字段组合起来；哪些字段 LLVM 自己也不写。
  - 前提：读完本章 11.2–11.3。

> 下一章回答一个本章故意绕开的问题：为什么 LLVM 不直接在源码树跑 `lit.cfg.py`，而要从构建树的 `lit.site.cfg.py` 启动？`@VAR@`、`configure_file`、`check-llvm` 是怎么接上的？

→ [Ch 12 lit.site.cfg 与 CMake：构建树怎么接进来](./12-site-cfg-and-cmake.md)
