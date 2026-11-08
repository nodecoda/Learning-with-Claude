# Ch 2 — lit 全景架构：一次调用经过哪些对象

> **目标**：能在脑子里画出 `lit tests` 这一条命令的完整对象流水线：谁解析参数、谁发现 suite、谁创建 `Test`、谁在哪个进程里执行命令、结果怎么回到主进程。以后看任何 lit 行为，你都能定位到「这是哪个阶段的事」。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码位于 `/home/ubuntu/llvm/llvm-project/llvm/utils/lit`。本章引用的函数名与行号都以该 commit 为准。

---

## 2.1 一张图先看完

```
$ lit tests
   │
   ├─ lit/main.py :: main()                         ← CLI 入口
   │    ├─ cl_arguments.parse_args()                ← 参数、--filter、-j、reports
   │    ├─ LitConfig(...)                           ← 全局配置（一份）
   │    ├─ discovery.find_tests_for_inputs()        ← 发现 suite 与测试
   │    │    ├─ getTestSuite()                      ← 向上找 lit.cfg / lit.site.cfg
   │    │    ├─ TestingConfig.fromdefaults()        ← 目录级配置（每个目录一份）
   │    │    ├─ config.load_from_path()             ← 执行 Python 配置
   │    │    └─ test_format.getTestsInDirectory()   ← 后缀过滤 → Test 对象
   │    ├─ determine_order / filter / shard         ← 选择与排序
   │    ├─ mark_xfail / mark_unsupported / mark_excluded
   │    └─ run_tests()
   │         ├─ Run(tests, lit_config, workers, ...)
   │         └─ Run.execute()
   │              ├─ ProcessPoolExecutor             ← 每个 worker 一个进程
   │              │    └─ lit/worker.py :: execute(test)
   │              │         └─ test.config.test_format.execute(test, lit_config)
   │              │              └─ ShTest → TestRunner.executeShTest()
   │              │                   ├─ parseIntegratedTestScript()
   │              │                   ├─ applySubstitutions()
   │              │                   └─ executeScriptInternal() / executeScript()
   │              └─ 结果通过 pickle 回到主进程
   │    ├─ record_test_times()                      ← .lit_test_times.txt
   │    ├─ print_results() / display                ← PASS/FAIL 行与摘要
   │    └─ reports (xUnit/JSON/ResultDB)            ← 可选
   └─ exit 0 / 1 / 2
```

本章就沿着这张图走一遍。你不需要记住所有函数名，但要记住**阶段边界**：发现、配置、选择、执行、报告。90% 的 lit 问题都能归到其中一个阶段。

## 2.2 入口：`main.py` 只做编排

入口在 `llvm/utils/lit/lit/main.py` 的 `main()`（第 25 行）。它本身不执行任何测试命令，只做六件事：

1. **解析参数**：`lit.cl_arguments.parse_args()`。
2. **建全局配置**：`lit.LitConfig.LitConfig(...)`。
3. **发现测试**：`lit.discovery.find_tests_for_inputs(lit_config, opts.test_paths)`。
4. **选择/排序/标记**：`determine_order`、`filter`、`filter_failed`、`shard`、`max_tests`、`mark_xfail`、`mark_unsupported`、`mark_excluded`。
5. **执行**：`run_tests(selected_tests, lit_config, opts, discovered_tests)`。
6. **报告与退出码**：`print_results`、`reports`、`sys.exit(0/1/2)`。

一个容易忽略但很重要的细节：**`discovered_tests` 和 `selected_tests` 是两个列表**。

- `discovered_tests` 是发现阶段的全集，报告里的 `Total Discovered Tests` 用它。
- `selected_tests` 是被 `--filter`、`--max-tests`、shard 等筛选后的子集，真正执行的是它。

所以 `--filter` 之后摘要里出现大量 `Excluded`，不是 bug，是设计。Ch 16 会详细讲。

## 2.3 `LitConfig`：全局只有一份

`llvm/utils/lit/lit/LitConfig.py` 的 `LitConfig` 是整个 lit 进程的**全局配置**，从命令行参数构造。它保存：

| 字段 | 作用 |
|---|---|
| `progname` | 程序名，用于错误前缀 |
| `path` | 额外的工具搜索路径（`--path`） |
| `params` | `-D/--param` 与内置参数（如 `config_map`） |
| `config_prefix` | 配置文件前缀，默认 `lit` → `lit.cfg` / `lit.site.cfg` |
| `config_names` | `["lit.cfg.py", "lit.cfg"]` |
| `site_config_names` | `["lit.site.cfg.py", "lit.site.cfg"]` |
| `local_config_names` | `["lit.local.cfg.py", "lit.local.cfg"]` |
| `maxIndividualTestTime` | 单测试超时 |
| `maxRetriesPerTest` | 全局重试次数 |
| `parallelism_groups` | `parallelism_group` 的并发上限 |
| `noExecute` | `--no-execute`，只解析不执行 |

注意配置文件名是**两个**：`.cfg.py` 和 `.cfg`。LLVM 自己主要用 `lit.cfg.py` / `lit.local.cfg.py`，但历史上有大量 `lit.cfg`，所以两者都支持。`--config-prefix foo` 会把前缀换成 `foo`，于是找 `foo.cfg.py` / `foo.site.cfg.py` / `foo.local.cfg.py`。Ch 3 会用到。

`LitConfig` 还会在构造时检查 `maxIndividualTestTime` 的平台支持（依赖 `psutil` 或进程树 kill 能力），并准备 `config_names` 等派生字段。它不是 suite 配置；真正每个目录的配置是下一个对象。

## 2.4 `TestingConfig`：每个目录一份的「配置快照」

`llvm/utils/lit/lit/TestingConfig.py` 的 `TestingConfig` 是目录级配置。它的生命周期分三步：

```
TestingConfig.fromdefaults(lit_config)   ← 默认值
        ↓
config.load_from_path(path, lit_config)  ← 执行 lit.cfg.py，填字段
        ↓
config.finish(lit_config)                ← 类型归一化、CLI 覆盖
```

`fromdefaults()` 已经把一些东西准备好了：

- `environment`：`PATH` 包含 `lit_config.path`，并设置 `LLVM_DISABLE_CRASH_REPORT=1`。
- 一组**允许透传**的环境变量：`LD_LIBRARY_PATH`、`ASAN_OPTIONS`、`FILECHECK_OPTS`、`TMPDIR`、`HOME` 等（完整列表见源码第 25–80 行）。**不是父进程的所有环境变量都会传给测试**，这是安全与可复现性设计。
- `pipefail=True`。
- `available_features` 初始为空（除非 `--vg`）。
- `suffixes=set()`、`test_format=None`、`excludes=[]`。

`load_from_path()` 会 `exec` 你的 Python 配置，并在全局命名空间里预置两个名字：

```python
config       # 当前 TestingConfig
lit_config   # 全局 LitConfig
```

所以 `lit.cfg.py` 里可以直接写 `config.name = ...`，也可以读 `lit_config.params`。配置执行完后调用 `finish()`：

- 把 `suffixes`、`excludes` 转成 `set`。
- 把 `test_source_root` / `test_exec_root` 转成 `str`。
- 如果命令行给了 `--max-retries-per-test` 且 suite 没设 `config.test_retry_attempts`，写入。
- 如果命令行给了 `--timeout`，覆盖 suite 的 `maxIndividualTestTime`，并打印一条 note。

> **关键认知**：`config` 不是全局单例。每个目录都会从父配置 `deepcopy` 一份，再加载该目录的 `lit.local.cfg.py`。所以你在子目录里改 `config.suffixes`，不会污染兄弟目录。Ch 22 会看 `getLocalConfig()` 的复制逻辑。

## 2.5 Discovery：向上找 suite，向下找测试

`llvm/utils/lit/lit/discovery.py` 负责两件事：

### 向上：找到输入属于哪个 suite

`getTestSuite(item, lit_config, cache)` 从输入路径开始向上走，直到找到一个目录包含：

- `lit.site.cfg.py` / `lit.site.cfg`，或
- `lit.cfg.py` / `lit.cfg`。

找到后：

```python
cfg = TestingConfig.fromdefaults(litConfig)
cfg.load_from_path(cfgpath, litConfig)
source_root = cfg.test_source_root or path
exec_root   = cfg.test_exec_root or path
return Test.TestSuite(cfg.name, source_root, exec_root, cfg, litConfig)
```

注意 `test_source_root` / `test_exec_root` 的默认值就是**配置文件所在目录**。这也是为什么最小 suite 不写这两个字段也能跑。

### 向下：递归发现测试

`getTestsInSuite()` 对一个目录：

1. 取该目录的 local config（`getLocalConfig`）。
2. 如果 `standalone_tests` 为真，跳过发现。
3. 否则调用 `lc.test_format.getTestsInDirectory(ts, path_in_suite, litConfig, lc)`。
4. 再递归子目录；遇到嵌套 suite 就切过去。

`FileBasedTest.getTestsInDirectory()` 的逻辑非常简单：

```python
for filename in os.listdir(source_path):
    if not os.path.isdir(filepath):
        for t in self.getTestsForPath(...):
            yield t
```

`getTestsForPath()` 才是后缀过滤的地方：

```python
if filename.startswith(".") or filename in localConfig.excludes:
    return
if any(filename.endswith(suffix) for suffix in localConfig.suffixes):
    yield lit.Test.Test(testSuite, path_in_suite, localConfig)
```

三个结论：

- **点文件永远不发现**（`.hidden.test` 不会跑）。
- `excludes` 是**文件名**排除，不是 glob；要排除目录，目录会在 `getTestsInSuite` 里被跳过。
- `suffixes` 是 `endswith`，所以 `.test` 也会匹配 `foo.notatest` 吗？不会——`foo.notatest` 以 `.test` 结尾，确实会匹配。后缀匹配是字符串匹配，不是扩展名语义。写 `suffixes = [".test"]` 时，`foo.notatest` 也会被当成测试，这是个小陷阱。

### 缓存

`find_tests_for_inputs()` 维护 `testSuiteCache` 和 `localConfigCache`，避免同一个 suite/目录被重复加载。发现完成后它会把 `suite.test_times` 置空：

```python
for k, suite in test_suite_cache.items():
    if suite[0]:
        suite[0].test_times = None
```

源码注释说得很直白：这些数据不再需要，留着会造成严重的性能问题。发现阶段结束后如果 `lit_config.numErrors` 非零，直接 `sys.exit(2)`。

## 2.6 选择：不是所有发现的测试都会跑

`main()` 在发现之后做一串选择：

```
determine_order(discovered_tests, opts.order)
        ↓
selected = [t for t in discovered if filter.search(full_name) and not filter_out.search(full_name)]
        ↓
if --filter-failed: selected = [t for t in selected if t.previous_failure]
        ↓
if --num-shards/--run-shard: filter_by_shard(...)
        ↓
selected = selected[:--max-tests]
        ↓
mark_xfail / mark_unsupported
        ↓
mark_excluded(discovered, selected)   ← 没选中的标成 EXCLUDED
```

几个容易混的点：

- `--filter` 匹配的是 `Test.getFullName()`，即 `SuiteName :: path/inside/suite`，不是绝对路径。
- `mark_excluded()` 把「发现了但没选中」的测试设成 `EXCLUDED`，所以摘要里会有 `Excluded` 一栏。
- `mark_xfail()` 可以在命令行强制把某些测试标成 XFAIL（`--xfail`），也可以取消（`--xfail-not`）。
- `--filter-failed` 依赖上一次运行留下的失败记录，不是重新执行后再筛。

## 2.7 `Run` 与 worker：跨进程边界

`llvm/utils/lit/lit/run.py` 的 `Run` 负责并行调度。核心参数：

```python
Run(tests, lit_config, workers, display.update, opts.max_failures, opts.timeout)
```

`main.run_tests()` 先算：

```python
workers = min(len(tests), opts.workers)
```

所以测试数少于 worker 数时不会开多余进程。然后 `Run.execute()`：

1. 建 `ProcessPoolExecutor`（Windows 有 60 worker/pool 限制，会拆多个 pool）。
2. 用 `parallelism_group` 建 `BoundedSemaphore`，控制同一组的并发。
3. 把测试分批提交（`LIT_BATCH_SIZE`，默认多 worker 时 2），收集结果。
4. 支持 `--max-time` 的全局 deadline、`--max-failures` 的提前停止。
5. worker 崩溃时抛 `WorkerCrashError`。

`worker.py` 的边界很关键：

```python
def initialize(lit_config, parallelism_semaphores):
    global _lit_config, _parallelism_semaphores
    _lit_config = lit_config
    _parallelism_semaphores = parallelism_semaphores
```

`lit_config` 被复制到每个 worker 进程。然后：

```python
def execute(test):
    with _get_parallelism_semaphore(test):
        result = _execute(test, _lit_config)
    test.setResult(result)
    return test
```

`_execute()` 里：

```python
result = test.config.test_format.execute(test, lit_config)
```

> **关键认知**：测试命令不是主进程执行的，是在 worker 子进程里执行的。所以：
> - 测试里的 `cd`、`export`、`rm` 不会影响主进程。
> - 测试写出的文件在 worker 的文件系统里（同一台机器，所以你能看到，但主进程的内存状态看不到）。
> - 测试里 `sys.exit()` 或 segfault 不会杀死 lit 主进程（会变成 worker 崩溃/超时）。
> - 结果对象通过 pickle 回传，所以 `Result` 里的字段必须可序列化。

## 2.8 单个测试：`ShTest` → `TestRunner`

`worker` 调的是 `test.config.test_format.execute()`。对 `ShTest` 来说，就是 `llvm/utils/lit/lit/formats/shtest.py` 的：

```python
def execute(self, test, litConfig):
    return lit.TestRunner.executeShTest(
        test,
        litConfig,
        self.execute_external,
        self.extra_substitutions,
        self.preamble_commands,
    )
```

`executeShTest()`（`TestRunner.py` 第 2277 行）是单测试执行的总入口：

```python
if test.config.unsupported:
    return Result(UNSUPPORTED, "Test is unsupported")

parsed = parseIntegratedTestScript(test, require_script=not script)
if isinstance(parsed, Result):
    return parsed

if litConfig.noExecute:
    return Result(PASS)

tmpDir, tmpBase = getTempPaths(test)
substitutions = ... + getDefaultSubstitutions(test, tmpDir, tmpBase, ...)
script = applySubstitutions(script, substitutions, conditions, ...)

return _runShTest(test, litConfig, useExternalSh, script, tmpBase)
```

顺序很重要：

1. **先看 `config.unsupported`**：整个目录被标 unsupported 时，连 RUN 行都不解析。
2. **解析 directive**：`RUN`、`REQUIRES`、`UNSUPPORTED`、`XFAIL`、`ALLOW_RETRIES` 等。解析失败会返回 `UNRESOLVED`。
3. **`--no-execute` 直接 PASS**：只发现/解析，不跑命令。
4. **算 `%t` 路径**：`getTempPaths()` 把临时文件放在 `test_exec_root` 下的 `Output/`。
5. **应用替换**：配置里的 substitutions + 默认替换，按顺序 regex 替换。
6. **执行**：内置 shell 或外部 shell。

`_runShTest()` 再包一层重试逻辑：

- `ALLOW_RETRIES: N` 或 `config.test_retry_attempts` 决定最多跑几次。
- 第一次失败、重试成功 → `FLAKYPASS`。
- 所有尝试都失败 → `FAIL`。
- 超时 → `TIMEOUT`。

## 2.9 结果：`Result` 与 `ResultCode`

`Test.py` 里定义了所有状态码。先记住三个分类：

| 分类 | 状态码 | `isFailure` |
|---|---|---|
| 通过/跳过 | `PASS`, `XFAIL`, `UNSUPPORTED`, `EXCLUDED`, `SKIPPED`, `FLAKYPASS`, `FIXED` | false |
| 失败 | `FAIL`, `XPASS`, `TIMEOUT`, `UNRESOLVED` | true |

`XPASS` 是 failure——测试标了 `XFAIL` 却通过了，说明「已知失败」的前提不再成立，必须有人去更新测试。Ch 21 会专门讲这个设计。

`Result` 还带：

- `output`：失败时打印的文本（`Exit Code`、`Command Output`）。
- `elapsed`：耗时，用于 `.lit_test_times.txt` 和 `--time-tests`。
- `attempts` / `max_allowed_attempts`：重试信息。
- `metrics` / `microResults`：给自定义 format 用的扩展点。

## 2.10 报告：先 display，再 reports

执行完后 `main()` 调 `print_results()`，它对每个 `ResultCode` 分组打印。`display.py` 负责逐测试的进度行（`PASS: ... (1 of 10)`），`reports.py` 负责文件输出：

- `JsonReport`：`-o results.json`
- `XunitReport`：`--xunit-xml-output`
- `ResultDBReport`：`--resultdb-output`
- `TimeTraceReport`：`--time-trace-output`
- `WttReport`：`--wtt-output`（Windows）

报告写完后才决定退出码：

```python
if lit_config.numErrors: sys.exit(2)
if any(t.isFailure() for t in discovered_tests):
    sys.exit(0 if opts.ignoreFail else 1)
```

注意它检查的是 `discovered_tests`，不是 `selected_tests`。所以被过滤掉的测试如果是 XFAIL/FAIL 状态，仍可能影响退出码？实际上 `mark_excluded` 会把它们设成 `EXCLUDED`（非 failure），所以不会。但如果某个测试在发现阶段就是 `UNRESOLVED`，它可能不在 selected 里却仍是 failure。这是 `--ignore-fail` 存在的原因之一。

## 2.11 用 `--debug` 看真实调用

`--debug` 不是「用调试器跑测试」，而是打开 lit 自己的内部日志。真实输出：

```bash
cd /tmp/ch0lab.SJx0tA
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --debug tests/hello.test
```

```
lit.py: .../discovery.py:65: debug: loading suite config '/tmp/ch0lab.SJx0tA/lit.cfg.py'
lit.py: .../TestingConfig.py:158: debug: ... loaded config '/tmp/ch0lab.SJx0tA/lit.cfg.py'
lit.py: .../discovery.py:138: debug: resolved input 'tests/hello.test' to 'HelloLit'::('tests', 'hello.test')
-- Testing: 1 tests, 1 workers --
PASS: HelloLit :: tests/hello.test (1 of 1)
```

这三行正好对应本章的三个阶段：

1. `loading suite config`：找到并加载 suite 配置。
2. `loaded config`：`TestingConfig.finish()` 完成。
3. `resolved input`：输入路径被解析成 `suite + path_in_suite`。

以后再遇到「为什么这个测试属于那个 suite」「为什么配置没生效」，先加 `--debug`，比猜快得多。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「lit 在主进程里一条条跑测试」 | 测试在 `ProcessPoolExecutor` 的 worker 子进程里跑；主进程只调度和收集结果。测试里的 `cd`/`export` 不影响主进程。 |
| 「`config` 是全局配置」 | `config` 是目录级 `TestingConfig`，每个目录从父配置 `deepcopy` 后加载 local cfg；`lit_config` 才是全局的。 |
| 「`--debug` 会用 gdb/pdb 跑测试」 | `--debug` 只打开 lit 自己的 debug 日志；要调试测试命令，用 `-v` 看 `# executed command:`。 |
| 「`Total Discovered Tests` 就是实际执行数」 | 它是发现全集；实际执行的是 `selected_tests`，差额变成 `EXCLUDED`。 |
| 「`excludes` 支持 glob」 | `excludes` 是文件名集合，`filename in excludes`；目录排除靠递归时的目录名判断，不是 glob。 |
| 「`suffixes` 是扩展名语义」 | 是 `filename.endswith(suffix)` 字符串匹配；`.test` 会匹配 `foo.notatest`。 |

## 重点整理

- 五个阶段：**发现 → 配置 → 选择 → 执行 → 报告**；先定位阶段，再定位函数。
- `main.py::main()` 只编排；`LitConfig` 全局一份，`TestingConfig` 每目录一份。
- `discovery.py` 向上找 suite，向下找测试；`FileBasedTest` 用 `suffixes`/`excludes` 过滤。
- `discovered_tests` 是全集，`selected_tests` 是实际执行集；`EXCLUDED` 是两者之差。
- 测试在 worker 子进程执行，结果 pickle 回主进程；`result = test.config.test_format.execute(test, lit_config)` 是执行边界。
- `executeShTest()` 的顺序：unsupported → 解析 → noExecute → `%t` → 替换 → 执行 → 重试。
- `XPASS` 是 failure；`XFAIL`/`UNSUPPORTED`/`EXCLUDED` 不是。
- `--debug` 打印配置加载与输入解析，是定位「测试没跑到」的第一工具。

## 自我检核

不看笔记，试着回答：

- [ ] 从 `lit tests` 到一条 `RUN:` 命令真正执行，会经过哪五个阶段？每个阶段的主角对象是谁？
- [ ] `lit_config` 和 `config` 的区别是什么？为什么 local cfg 要 `deepcopy` 父配置？
- [ ] 为什么测试里的 `cd /tmp` 不会改变 lit 主进程的工作目录？这个设计带来什么好处和什么限制？
- [ ] `Total Discovered Tests: 19` 但只执行了 5 个，另外 14 个去哪了？在摘要里对应哪一栏？
- [ ] `XPASS` 为什么算 failure？如果把它改成「通过」，会掩盖什么问题？
- [ ] 你能说出 `executeShTest()` 里 `config.unsupported` 检查为什么必须在解析 `RUN:` 之前吗？

## 延伸阅读

- **[lit Command Guide — LIT INFRASTRUCTURE](https://llvm.org/docs/CommandGuide/lit.html#lit-infrastructure)**
  - 读什么：`TEST SUITES`、`TEST DISCOVERY`、`LOCAL CONFIGURATION FILES` 三节。
  - 能学到：官方对 suite / discovery / local config 的抽象描述；对照本章 2.4–2.5。
  - 前提：读完本章 2.1 的图，否则容易陷进细节。
- **[`llvm/utils/lit/lit/main.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/main.py)**
  - 读什么：`main()` 全文（约 150 行）。
  - 能学到：选择/标记/报告的真实顺序；哪些逻辑在 `run_tests()` 之前，哪些之后。
  - 前提：知道 `argparse` 的基本用法。
- **[`llvm/utils/lit/lit/run.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/run.py)**
  - 读什么：`Run.execute()`、`_execute()`、`_dispatch_and_wait()`。
  - 能学到：多进程调度、`parallelism_group`、`max-failures`、`timeout` 的实现；为什么 worker 崩溃要单独处理。
  - 前提：读过本章 2.7，知道 `ProcessPoolExecutor` 是什么。
- **[`llvm/utils/lit/tests/discovery.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/discovery.py)**
  - 读什么：整个文件。
  - 能学到：lit 官方如何测试「发现」这件事；它本身就是一个可执行的规格。
  - 前提：能读 Python 测试脚本；不需要先懂 lit 内部。

> 下一章把「发现」和「配置」这两段拆开：`lit.cfg`、`lit.site.cfg`、`lit.local.cfg` 到底谁先谁后，`--config-prefix` 又在改什么。

→ [Ch 3 测试发现与配置级联](./03-discovery-and-config-files.md)
