# Ch 23 — GoogleTest format：另一种测试发现

> **目标**：理解 lit 如何把一个 gtest 可执行文件变成「虚拟 shard 测试」，再用 `GTEST_OUTPUT` 的 JSON 把结果拆回单个 case。学完你能解释为什么 gtest 测试没有 `RUN:`、shard 路径为什么不存在、以及为什么 JSON 缺失会被判 `FAIL`。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。本章用一个 Python 假 gtest 实跑发现与执行；真实 LLVM 构建目录的 `llvm-lit` 只作为对照，不需要构建 LLVM。

---

## 23.1 为什么需要另一种 format

ShTest 的模型是「一个文件 + 若干 `RUN:` 行」。gtest 的模型完全不同：

```text
一个可执行文件
  ├─ 内含 N 个测试 case
  ├─ 通过 --gtest_list_tests 自报家门
  ├─ 通过 --gtest_filter 选择子集
  ├─ 通过 GTEST_OUTPUT=json:<file> 输出结构化结果
  └─ 通过 GTEST_TOTAL_SHARDS / GTEST_SHARD_INDEX 分片
```

所以 lit 不能把它当 ShTest 解析：文件里没有 `RUN:`，测试名在执行前甚至不知道。`lit.formats.GoogleTest` 做的事是：

```text
发现阶段
  执行 <exe> --gtest_list_tests --gtest_filter=-*DISABLED_*
  数出 case 数 N
  把 N 个 case 切成 shard
  为每个 shard 生成一个 lit.Test（虚拟路径）

执行阶段
  设置 GTEST_OUTPUT / GTEST_TOTAL_SHARDS / GTEST_SHARD_INDEX
  运行 <exe>
  读 JSON

后处理
  把 shard 结果映射回 <TestCase>/<TestName>
  删除 JSON
```

构造函数在 `llvm/utils/lit/lit/formats/googletest.py:17`：

```python
def __init__(self, test_sub_dirs, test_suffix, run_under=[], test_prefix=None):
    self.test_sub_dirs = str(test_sub_dirs).split(";")
    exe_suffix = str(test_suffix)
    if kIsWindows:
        exe_suffix += ".exe"
    self.test_suffixes = {exe_suffix, test_suffix + ".py"}
    self.test_prefixes = {test_prefix} if test_prefix else None
    self.run_under = run_under
```

`test_sub_dirs` 支持分号分隔的多个子目录；`test_suffixes` 同时接受可执行后缀和 `<suffix>.py`，后者是给 lit 自测用的（用 Python 脚本假冒 gtest）。`run_under` 可以是字符串或列表，最终由 `prepareCmd()` 拼到命令前面。

### 23.1.1 LLVM 的真实用法

`llvm/test/Unit/lit.cfg.py`：

```python
config.name = "LLVM-Unit"
config.suffixes = []
config.test_exec_root = os.path.join(config.llvm_obj_root, "unittests")
config.test_source_root = config.test_exec_root
config.test_format = lit.formats.GoogleTest(
    config.llvm_build_mode,
    "Tests",
    run_under=config.gtest_run_under,
)
```

`config.llvm_build_mode` 通常是 `Debug` 或 `Release`，所以 gtest 可执行文件在 `unittests/Debug/` 或 `unittests/Release/` 下；`test_suffix="Tests"` 匹配 `*Tests` 可执行文件。`llvm.site.cfg.py.in` 把 `@LLVM_GTEST_RUN_UNDER@` 注入 `config.gtest_run_under`，交叉编译或模拟器场景会用到。

注意 `config.suffixes = []`：GoogleTest 不使用后缀发现，`suffixes` 留空；发现完全由 `getTestsInDirectory()` 驱动。这也意味着你不能靠 `config.suffixes` 限制 gtest 文件，得用 `test_prefix` 或 `test_sub_dirs`。

## 23.2 发现：`--gtest_list_tests` 与 shard 计算

`getTestsInDirectory()` 在 `googletest.py:50`。它遍历 `test_sub_dirs` 下的文件，对每个候选执行：

```python
def get_num_tests(self, path, litConfig, localConfig):
    list_test_cmd = self.prepareCmd(
        [path, "--gtest_list_tests", "--gtest_filter=-*DISABLED_*"]
    )
    try:
        out = subprocess.check_output(list_test_cmd, env=localConfig.environment)
    except subprocess.CalledProcessError as exc:
        litConfig.warning("unable to discover google-tests in %r: %s. Process output: %s"
                          % (path, sys.exc_info()[1], exc.output))
        return None
    return sum(map(lambda line: line.startswith(b"  "), out.splitlines(False)))
```

三个细节：

- `--gtest_filter=-*DISABLED_*`：排除名字里带 `DISABLED_` 的 case。注意这是 gtest 的过滤器语义，不是 lit 的。
- 计数方式是「以两个空格开头的行」。gtest 的列表格式是 suite 名顶格、case 名缩进两格；所以只数 case。
- 失败时返回 `None`，不是抛异常。上层会生成一个名为 `failed_to_discover_tests_from_gtest` 的占位测试，让失败在报告里可见。

占位测试的路径（第 135–143 行）：

```python
testPath = path_in_suite + (subdir, fn, "failed_to_discover_tests_from_gtest")
yield lit.Test.Test(testSuite, testPath, localConfig, file_path=execpath)
```

它的 `gtest_json_file` 是 `None`，`execute()` 第一行就返回 `lit.Test.FAIL, ""`（第 145–146 行）。

### 23.2.1 shard 计算：512 起步，按核数对折

```python
init_shard_size = 512
core_count = lit.util.usable_core_count()
...
if litConfig.gtest_sharding:
    shard_size = init_shard_size
    nshard = int(math.ceil(num_tests / shard_size))
    while nshard < core_count and shard_size > 1:
        shard_size = shard_size // 2
        nshard = int(math.ceil(num_tests / shard_size))
```

逻辑：

1. 先按每个 shard 512 个测试算 `nshard`。
2. 如果 shard 数少于可用核数，就把 shard 大小减半，重新算，直到 shard 数够多或 shard 大小降到 1。
3. 最终 `nshard` 个虚拟测试，每个负责 `GTEST_SHARD_INDEX=idx`、`GTEST_TOTAL_SHARDS=nshard`。

本章机器 `usable_core_count()` 是 32，假 gtest 只有 3 个 case：

```text
ceil(3/512)=1 → 1<32，512→256 → 1 → ... → 1，nshard=ceil(3/1)=3
```

所以最终是 3 个 shard，每个 1 个 case。真实 `lit --show-tests`：

```text
-- Available Tests --
  GTestLab :: gtest/fake_gtest.py/0/3
  GTestLab :: gtest/fake_gtest.py/1/3
  GTestLab :: gtest/fake_gtest.py/2/3
```

虚拟路径是 `<subdir>/<filename>/<shard_index>/<nshard>`。它们**不对应磁盘上的文件**；`test.getSourcePath()` 拼出来的路径并不存在。`execute()` 必须先把路径剥回可执行文件：

```python
testPath, testName = os.path.split(test.getSourcePath())
while not os.path.exists(testPath):
    testPath, namePrefix = os.path.split(testPath)
    testName = namePrefix + "/" + testName
```

这段循环处理两种情况：shard 路径、以及 gtest 参数化测试名里带 `/` 的情况。最终 `testPath` 是可执行文件，`testName` 是 `idx/nshard`，再拆成 `shard_idx` 和 `total_shards`。

JSON 文件名也带上了这些信息：

```python
json_file = "-".join([execpath, testSuite.config.name, str(os.getpid()),
                      str(idx), str(nshard)]) + ".json"
```

所以并发运行多个 lit 实例不会互相覆盖。

## 23.3 执行：环境变量、过滤与 JSON

`execute()` 在 `googletest.py:144`：

```python
if test.gtest_json_file is None:
    return lit.Test.FAIL, ""

testPath = test.getSourcePath()
use_shuffle = TestOrder(litConfig.order) == TestOrder.RANDOM
shard_env = {
    "GTEST_OUTPUT": "json:" + test.gtest_json_file,
    "GTEST_SHUFFLE": "1" if use_shuffle else "0",
}
if litConfig.gtest_sharding:
    ...  # 从虚拟路径拆出 shard_idx/total_shards
    shard_env.update({
        "GTEST_TOTAL_SHARDS": os.environ.get("GTEST_TOTAL_SHARDS", total_shards),
        "GTEST_SHARD_INDEX": os.environ.get("GTEST_SHARD_INDEX", shard_idx),
    })
test.config.environment.update(shard_env)

cmd = [testPath]
cmd = self.prepareCmd(cmd)
if litConfig.useValgrind:
    cmd = litConfig.valgrindArgs + cmd
if litConfig.noExecute:
    return lit.Test.PASS, ""
```

几个容易忽略的点：

- `GTEST_OUTPUT` 固定用 `json:`；gtest 也支持 XML，但 lit 不读 XML。`GTEST_SHUFFLE` 跟随 `--order=random`。如果 shuffle 开启，JSON 里的 `random_seed` 会被写回 `GTEST_RANDOM_SEED`，让失败可以复现。
- `GTEST_TOTAL_SHARDS`/`GTEST_SHARD_INDEX` 优先读**父进程环境**；如果用户已经设置了，就尊重用户的值。这允许手工复现单个 shard。
- `noExecute` 在构造完命令后直接返回 `PASS`，不会运行。
- `prepareCmd()` 在命令最前面加解释器或 `run_under`。

### 23.3.1 `prepareCmd()`：`.py` 用 `sys.executable`

```python
def prepareCmd(self, cmd):
    if cmd[0].endswith(".py"):
        cmd = [sys.executable] + cmd
    if self.run_under:
        if isinstance(self.run_under, list):
            cmd = self.run_under + cmd
        else:
            cmd = shlex.split(self.run_under) + cmd
    return cmd
```

真实输出里能看到解释器：

```text
Script(shard):
--
GTEST_OUTPUT=json:/tmp/lit23.../fake_gtest.py-GTestLab-1106108-1-3.json GTEST_SHUFFLE=0 GTEST_TOTAL_SHARDS=3 GTEST_SHARD_INDEX=1 /usr/bin/python3 /tmp/lit23.../gtest/fake_gtest.py
--
```

`/usr/bin/python3` 就是 `sys.executable`。在 Windows 上 shebang 不可靠，所以这个处理是必需的；`run_under` 则是交叉执行（qemu、模拟器）的入口。

### 23.3.2 JSON 不存在：`FAIL`，不是 `UNRESOLVED`

```python
if not os.path.exists(test.gtest_json_file):
    errmsg = f"shard JSON output does not exist: %s" % (test.gtest_json_file)
    stream_msg = f"\n{out}\n--\nexit: {exitCode}\n--\n"
    return lit.Test.FAIL, shard_header + stream_msg + errmsg
```

真实实验：一个假 gtest 正常列出测试，但执行时故意不写 JSON，`exit 0`：

```text
FAIL: GTestLab :: gtest/broken_gtest.py/0/1
GTEST_OUTPUT=json:.../broken_gtest.py-GTestLab-1106547-0-1.json ... /usr/bin/python3 .../broken_gtest.py
pretending to run but never writing JSON
exit: 0
shard JSON output does not exist: .../broken_gtest.py-GTestLab-1106547-0-1.json
```

即使 `exit 0`，也判 `FAIL`。原因是 lit 需要 JSON 来拆分结果；没有 JSON 就不知道哪个 case 通过，只能把整个 shard 标失败。这是一个刻意的严格选择。

### 23.3.3 解析 JSON 与提取失败输出

`exitCode == 0` 直接 `PASS`；否则读 JSON：

```python
with open(test.gtest_json_file, encoding="utf-8") as f:
    jf = json.load(f)
    for testcase in jf["testsuites"]:
        for testinfo in testcase["testsuite"]:
            result = testinfo["result"]
            if result in ("SUPPRESSED", "SKIPPED"):
                continue
            if "failures" in testinfo:
                ... get_test_stdout(testname); for fail in testinfo["failures"]: ...
            elif result != "COMPLETED":
                output += "unresolved test result\n"
```

`get_test_stdout()` 用文本标记定位：

```python
header = "[ RUN      ] " + test_name; footer = "[  FAILED  ] " + test_name
```

它从 gtest 的 stdout 里截取两个标记之间的内容。如果 gtest 改了输出格式，这里会 `assert False`。这是 lit 与 gtest 文本输出的一个隐式契约。

## 23.4 `post_process_shard_results()`：把 shard 拆回 case

执行阶段每个 shard 是一个 `lit.Test`，结果是一个 `Result`。但我们想要的是「每个 case 一个结果」。`post_process_shard_results()`（`googletest.py:285`）在 `main.py` 的 `run_tests()` 之后调用：

```python
def remove_gtest(tests):
    return [t for t in tests if t.gtest_json_file is None]
discovered_tests = remove_gtest(discovered_tests)
gtests = [t for t in selected_tests if t.gtest_json_file]
selected_tests = remove_gtest(selected_tests)
for test in gtests:
    if not os.path.exists(test.gtest_json_file):
        selected_tests.append(test); discovered_tests.append(test); continue
```

它先把 shard 测试从两个列表里移除，再读每个 JSON，为每个 case 创建 `lit.Test.Test`：

```python
testPath = test.path_in_suite[:-2] + (testcase["name"], testinfo["name"])
subtest = lit.Test.Test(test.suite, testPath, test.config, test.file_path)
```

路径映射：

| 模式 | 原 `path_in_suite` | `[:-2]` | 新路径 |
|---|---|---|---|
| sharding | `(subdir, file, idx, nshard)` | `(subdir, file)` | `(subdir, file, Case, Name)` |
| no sharding | `(subdir, file)` | `()` | `(Case, Name)` |

所以 `--no-gtest-sharding` 下，展开后的测试名会**丢掉可执行文件前缀**：

```text
GTestLab :: AlphaTest/FailOne
```

而不是 `GTestLab :: gtest/fake_gtest.py/AlphaTest/FailOne`。这不是 bug，是 `path_in_suite[:-2]` 对二元组的自然结果；但它会让 `--filter` 的行为在两种模式下不一致。

### 23.4.1 状态映射

```python
if opts.filter_out.search(subtest.getFullName()):
    returnCode = lit.Test.EXCLUDED
elif testinfo["result"] == "SKIPPED":
    returnCode = lit.Test.SKIPPED
elif "failures" in testinfo:
    returnCode = lit.Test.XFAIL if test.isExpectedToFail() else lit.Test.FAIL
elif testinfo["result"] == "COMPLETED":
    returnCode = lit.Test.PASS
else:
    returnCode = lit.Test.UNRESOLVED
```

注意：

- `--filter-out` 在**后处理**才应用，因为发现阶段只知道 shard，不知道 case 名。
- `XFAIL` 只在 `"failures" in testinfo` 时考虑；`test.isExpectedToFail()` 继承 shard 的 `xfails`。
- `SUPPRESSED` 的 case 在更早的循环里被 `continue` 跳过，不会生成 subtest。
- `elapsed_time = float(testinfo["time"][:-1])`：`time` 带 `s` 后缀，去掉再转 float；每个 subtest 的 `start` 从 shard 的 `start` 累加，`pid` 继承 shard。
- 处理完删除 JSON：`os.remove(test.gtest_json_file)`。

### 23.4.2 sanitizer 下的「全过但 shard 失败」

最后一段：

```python
if not has_failure_in_shard and test.isFailure():
    selected_tests.append(test)
    discovered_tests.append(test)
```

含义：如果 JSON 里没有任何 case 失败，但 shard 进程本身非零退出（例如 ASan 报内存错误、进程崩溃），就把 shard 测试重新加回列表。这样失败不会因为「所有 case 都 PASS」而被吞掉。

这是源码行为；本章未构造 sanitizer 场景（未实测），只验证了普通失败。注释写得很直接：

```text
In some situations, like running tests with sanitizers, all test passes but
the shard could still fail due to memory issues.
```

## 23.5 真实实验：一个 Python 假 gtest

为了不构建 LLVM，我们写一个最小的假 gtest，支持：

- `--gtest_list_tests`；
- `GTEST_OUTPUT=json:<file>`；
- `GTEST_TOTAL_SHARDS` / `GTEST_SHARD_INDEX`；
- 失败时打印 `[ RUN ]` / `[ FAILED ]`。

套件配置：

```python
import os
import lit.formats

config.name = "GTestLab"
config.test_format = lit.formats.GoogleTest("gtest", ".py")
config.suffixes = []
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
```

假 gtest 的关键部分（完整文件在实验目录里）：

```python
TESTS = [
    ("AlphaTest", "PassOne", "COMPLETED", None),
    ("AlphaTest", "FailOne", "FAILED", "expected 1 but got 2"),
    ("BetaTest", "SkipOne", "SKIPPED", None),
]

if "--gtest_list_tests" in sys.argv:
    for case in dict.fromkeys(c for c, _, _, _ in TESTS):
        print(case + ".")
        for c, name, _, _ in TESTS:
            if c == case:
                print("  " + name)
    sys.exit(0)

total = int(os.environ.get("GTEST_TOTAL_SHARDS", "1"))
index = int(os.environ.get("GTEST_SHARD_INDEX", "0"))
selected = [(c, n, r, f) for i, (c, n, r, f) in enumerate(TESTS)
            if i % total == index]

# ... 写 GTEST_OUTPUT JSON，失败时退出 1 ...
```

### 23.5.1 发现

```bash
lit --show-tests .
```

真实输出（`broken_gtest.py` 来自 23.3.2；若未创建它，这里只有 3 行 `fake_gtest.py/...`。文件枚举顺序由 `os.listdir` 决定，不同文件系统上可能不同；`idx/nshard` 本身是稳定的）：

```text
-- Available Tests --
  GTestLab :: gtest/broken_gtest.py/0/1
  GTestLab :: gtest/fake_gtest.py/0/3
  GTestLab :: gtest/fake_gtest.py/1/3
  GTestLab :: gtest/fake_gtest.py/2/3
```

### 23.5.2 执行

```bash
lit --filter='fake_gtest' -v .
```

真实输出（截取；`--filter` 把 `broken_gtest.py` 排除在选中集之外）：

```text
-- Testing: 3 of 4 tests, 3 workers --
PASS: GTestLab :: gtest/fake_gtest.py/2/3 (1 of 3)
PASS: GTestLab :: gtest/fake_gtest.py/0/3 (2 of 3)
FAIL: GTestLab :: gtest/fake_gtest.py/1/3 (3 of 3)
******************** TEST 'GTestLab :: gtest/fake_gtest.py/1/3' FAILED ********************
Script(shard):
--
GTEST_OUTPUT=json:.../fake_gtest.py-GTestLab-1106108-1-3.json GTEST_SHUFFLE=0 GTEST_TOTAL_SHARDS=3 GTEST_SHARD_INDEX=1 /usr/bin/python3 .../gtest/fake_gtest.py
--

Script:
--
/usr/bin/python3 .../gtest/fake_gtest.py --gtest_filter=AlphaTest.FailOne
--
expected 1 but got 2
********************

Total Discovered Tests: 3
  Skipped: 1 (33.33%)
  Passed : 1 (33.33%)
  Failed : 1 (33.33%)
```

注意报告里最终显示的是 `GTestLab :: gtest/fake_gtest.py/AlphaTest/FailOne`（后处理后的名字），而不是 `1/3`。`Total Discovered Tests` 也变成 3：shard 被移除，3 个 case 被加入。

### 23.5.3 `--no-gtest-sharding`

```bash
lit --no-gtest-sharding --filter='fake_gtest' -v .
```

真实输出（截取）：

```text
-- Testing: 1 of 2 tests, 1 workers --
FAIL: GTestLab :: gtest/fake_gtest.py (1 of 1)
...
Failed Tests (1):
  GTestLab :: AlphaTest/FailOne

Total Discovered Tests: 3
  Skipped: 1 (33.33%)
  Passed : 1 (33.33%)
  Failed : 1 (33.33%)
```

这里 `AlphaTest/FailOne` 丢掉了 `gtest/fake_gtest.py` 前缀，原因就是 23.4 的 `path_in_suite[:-2]`。如果你用 `--filter` 按完整路径筛 case，两种模式的匹配串不一样，需要分别验证。

### 23.5.4 直接把可执行文件当输入：空 `FAIL`

```bash
lit -v gtest/fake_gtest.py
```

真实输出：

```text
FAIL: GTestLab :: gtest/fake_gtest.py (1 of 1)
******************** TEST 'GTestLab :: gtest/fake_gtest.py' FAILED ********************

********************
```

为什么？`getTestsInSuite()` 对「输入直接是文件」走的是 `test_format.getTestsForPath()`；`GoogleTest` 没有覆盖这个方法，继承的 `TestFormat.getTestsForPath()` 只生成一个普通 `Test`，没有 `gtest_json_file`。`execute()` 第一行：

```python
if test.gtest_json_file is None:
    return lit.Test.FAIL, ""
```

所以得到空 `FAIL`。**gtest 可执行文件必须通过目录（或 `--filter`）发现，不能作为文件参数直接传给 lit。**

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「gtest 测试也是 ShTest，只是没有 RUN」 | 完全不同的 format：不解析源文件，靠 `--gtest_list_tests` 发现、靠 JSON 回传结果。`config.suffixes=[]` 是正常的 |
| 「shard 路径是真实文件」 | `<exe>/<idx>/<nshard>` 是虚拟路径，磁盘上不存在；`execute()` 用剥路径的方式找回可执行文件 |
| 「`.py` 假 gtest 需要自己加 `python3`」 | `prepareCmd()` 检测 `.py` 后自动加 `sys.executable`；`run_under` 再拼在最前面 |
| 「gtest 退出 0 就 PASS」 | 没有 JSON 仍判 `FAIL`；JSON 不存在是硬错误，因为无法拆分结果 |
| 「所有 case 都 PASS，shard 一定 PASS」 | sanitizer/崩溃可能让 shard 非零退出；`post_process` 会把 shard 加回失败列表 |
| 「`--no-gtest-sharding` 只是不切片，测试名不变」 | 后处理用 `path_in_suite[:-2]`，二元组会得到 `Case/Name`，丢掉可执行文件前缀 |
| 「可以直接 `lit path/to/gtest_binary`」 | 文件输入走 `getTestsForPath()`，没有 `gtest_json_file`，得到空 `FAIL`；必须传目录或用 `--filter` |
| 「`--filter-out` 对 gtest 无效」 | 它在后处理阶段按真实 case 名应用；发现阶段只知道 shard，所以不能在发现时过滤 case |

## 重点整理

1. `GoogleTest(test_sub_dirs, test_suffix, run_under, test_prefix)` 发现的是**可执行文件**，不是测试文件；`suffixes` 通常为空。
2. `get_num_tests()` 执行 `--gtest_list_tests --gtest_filter=-*DISABLED_*`，数缩进两行的 case；失败时生成 `failed_to_discover_tests_from_gtest` 占位测试。
3. shard 初始大小 512，按可用核数对折，直到 shard 数够多或大小为 1；虚拟路径是 `<subdir>/<file>/<idx>/<nshard>`。
4. 执行时设置 `GTEST_OUTPUT=json:<file>`、`GTEST_SHUFFLE`、`GTEST_TOTAL_SHARDS`、`GTEST_SHARD_INDEX`；`.py` 由 `prepareCmd()` 加 `sys.executable`。
5. JSON 不存在直接 `FAIL`；`exitCode==0` 直接 `PASS`；否则解析 `testsuites[].testsuite[]`，用 `[ RUN ]`/`[ FAILED ]` 提取失败输出。
6. `post_process_shard_results()` 把 shard 拆成 `Case/Name`；`--filter-out` 在这一步才生效；`XFAIL` 继承 shard 的预期。
7. `--no-gtest-sharding` 下 `path_in_suite[:-2]` 会丢掉可执行文件前缀，导致测试名与 sharding 模式不同。
8. sanitizer 场景（未实测，源码注释所述）：JSON 里没有失败但 shard 非零退出，shard 会被重新加回失败列表。

## 自我检核

- [ ] 不看源码，画出 gtest 从「一个可执行文件」到「3 个 shard 测试」再到「3 个 case 结果」的路径。
- [ ] 解释 shard 大小为什么从 512 开始对折，以及在本机 32 核、3 个 case 时最终是多少个 shard。
- [ ] 为什么 `GTEST_OUTPUT` 的 JSON 不存在时判 `FAIL` 而不是 `UNRESOLVED`？如果改成 `UNRESOLVED` 会丢失什么信息？
- [ ] 比较 sharding 与 `--no-gtest-sharding` 下后处理生成的 `path_in_suite`，解释测试名为什么不同。
- [ ] 一个 gtest case 返回 `SKIPPED`，它最终在 lit 里是什么状态？`SUPPRESSED` 呢？
- [ ] 直接把 gtest 可执行文件作为 lit 输入会发生什么？从 `getTestsForPath()` 与 `gtest_json_file` 的角度解释。

## 延伸阅读

- **`llvm/utils/lit/lit/formats/googletest.py`（全文 376 行）**
  - 读哪里：`get_num_tests()`、`getTestsInDirectory()`、`execute()`、`post_process_shard_results()`。
  - 能学到什么：发现/执行/后处理三段式，shard 计算与虚拟路径，JSON schema 的实际用法。
  - 前提知识：本章 23.2–23.4；Ch 2 的发现流程。
- **`llvm/test/Unit/lit.cfg.py` 与 `llvm/test/Unit/lit.site.cfg.py.in`**
  - 读哪里：真实 LLVM 如何配置 `GoogleTest(config.llvm_build_mode, "Tests", run_under=...)`。
  - 能学到什么：`llvm_build_mode`、`gtest_run_under`、`test_exec_root` 的来源；交叉编译时 `run_under` 的作用。
  - 前提知识：Ch 12 的 site cfg。
- **`llvm/utils/lit/tests/googletest-format.py`、`googletest-no-sharding.py`、`googletest-timeout.py`**
  - 读哪里：官方对 shard、no-sharding、超时、过滤的断言。
  - 能学到什么：如何用 `Inputs/googletest-*` 下的假可执行文件做回归测试；可以照抄成自己的验证脚本。
  - 前提知识：本章 23.5。
- **`llvm/utils/lit/tests/Inputs/googletest-format/`**
  - 读哪里：假 gtest 的 Python/C++ 实现与 JSON 样例。
  - 能学到什么：`GTEST_OUTPUT`、`--gtest_list_tests`、`[ RUN ]`/`[ FAILED ]` 的最小契约。
  - 前提知识：会读 Python 与 JSON。

下一章把前面所有零件组装成一个可发布的 suite：目录布局、配置分层、feature 设计、CI 集成与升级策略；练习 D 则先让你亲手实现一个 format。

→ [练习 D — 给 lit 加一个自定义 format](./practice-d-custom-format.md)
