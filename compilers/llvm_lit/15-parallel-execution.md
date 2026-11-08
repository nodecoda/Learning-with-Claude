# Ch 15 — 并行执行模型

> **目标**：能说清 lit 从「选中一批测试」到「多个 worker 进程同时执行」的完整路径；能解释 `-j`、`LIT_MAX_WORKERS`、`parallelism_group`、`%t` 隔离与 worker 崩溃各自发生在哪一层，并在自己的 suite 里复现一次串行/并行对比。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`（2026-10-30）。本章实验在 `/tmp/litlab-a1` 与 `/tmp/litrace-b1` 中真实运行，Ubuntu 22.04 x86_64、Python 3.10.12。所有输出均为本机实跑，未实测的点会单独标出。

---

## 15.1 心智模型：主进程只调度，worker 才执行

lit 的并行单位是**进程**，不是线程：

```
lit 主进程
  ├─ 发现、配置、选择、排序（单线程）
  ├─ Run.execute()
  │    ├─ ProcessPoolExecutor #1 ── worker 进程 A ── 执行 test 1, test 2, ...
  │    ├─ ProcessPoolExecutor #2 ── worker 进程 B
  │    └─ ...
  ├─ 收集每个 test 的 Result（pickle 回主进程）
  └─ 打印结果、写报告、决定退出码
```

这条边界决定三件事：

- 测试里的 `cd`、`export`、`ulimit` 只影响 worker 子进程，不影响 lit 主进程。
- 测试之间共享的是**文件系统**，不是 Python 变量。共享文件是并行 flaky 的第一大来源。
- worker 崩溃（segfault、OOM kill、`kill -9`）不是 `FAIL`，而是 `WorkerCrashError`，lit 把整次运行标成内部错误。

本章沿 `main.run_tests()` → `run.Run` → `worker.py` 往下走。

## 15.2 worker 数：`min(len(tests), opts.workers)`

入口在 `main.py` 的 `run_tests()`（第 261 行）：

```python
def run_tests(tests, lit_config, opts, discovered_tests):
    workers = min(len(tests), opts.workers)          # main.py:262
    display = lit.display.create_display(opts, tests, discovered_tests, workers)
    run = lit.run.Run(
        tests, lit_config, workers, display.update, opts.max_failures, opts.timeout
    )
```

所以「我写了 `-j 32`，为什么只显示 8 workers？」答案很简单：只有 8 个测试。`min` 保证不会为不存在的测试开 worker。

`opts.workers` 的默认值在 `cl_arguments.py` 第 91–100 行：

```python
parser.add_argument(
    "-j", "--threads", "--workers",
    dest="workers",
    metavar="N",
    type=_positive_int,
    default=os.getenv("LIT_MAX_WORKERS", lit.util.usable_core_count()),
)
```

三个事实：

1. `-j`、`--threads`、`--workers` 是同一个参数。
2. 默认是 `usable_core_count()`，不是 `os.cpu_count()`。`util.py` 第 34–45 行优先用 `os.sched_getaffinity(0)`，也就是当前进程能用的 CPU 集合；容器/`taskset` 下比核数更准。
3. 环境变量 `LIT_MAX_WORKERS` 改默认值；显式 `-j` 仍然覆盖它。

本机 `nproc` 与 `len(os.sched_getaffinity(0))` 都是 `32`。用 `LIT_MAX_WORKERS=2` 跑 3 个测试：

```bash
cd /tmp/litlab-a1
LIT_MAX_WORKERS=2 python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py \
  tests/pass/p01.test tests/pass/p02.test tests/pass/p03.test
```

真实输出（节选）：

```
-- Testing: 3 tests, 2 workers --
PASS: LitLab :: tests/pass/p02.test (1 of 3)
PASS: LitLab :: tests/pass/p03.test (2 of 3)
PASS: LitLab :: tests/pass/p01.test (3 of 3)
```

`3 tests, 2 workers`，不是 3 workers。`min(3, 2)`。

### 真实实验：`-j 1` 与 `-j 4`

实验 suite 的 `lit.cfg.py`：

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

8 个测试，每个先 `sleep 0.3` 再 `echo`：

```text
# RUN: sleep 0.3
# RUN: echo pass-1
```

`-j 1` 与 `-j 4` 的真实结果：

```
$ lit -j 1 tests/pass
-- Testing: 8 tests, 1 workers --
...
Testing Time: 2.42s

$ lit -j 4 tests/pass
-- Testing: 8 tests, 4 workers --
PASS: LitLab :: tests/pass/p01.test (1 of 8)
PASS: LitLab :: tests/pass/p02.test (2 of 8)
PASS: LitLab :: tests/pass/p07.test (3 of 8)
PASS: LitLab :: tests/pass/p08.test (4 of 8)
...
Testing Time: 0.61s
```

2.42s → 0.61s，约 4 倍。sleep/IO 密集型测试的墙钟时间由 worker 数决定；CPU 密集型测试超过物理核数只会互相抢 CPU。

## 15.3 `run.py`：`Run`、进程池、批次与提交窗口

`Run` 的构造与执行在 `run.py`（节选）：

```python
class Run:
    def __init__(self, tests, lit_config, workers, progress_callback,
                 max_failures, timeout):
        self.tests = tests
        self.workers = workers
        self.max_failures = max_failures
        self.timeout = timeout
        assert workers > 0
```

`execute()`（第 65 行）做两件事：

1. 算全局 deadline：`deadline = time.time() + (self.timeout or one_week)`（第 85–88 行）。没有 `--max-time` 时给一周，避免 Windows 上 `inf` 出问题。
2. 无论成功、失败、超时还是异常，`finally` 把所有还没有结果的测试标成 `SKIPPED`（第 90–96 行）。这就是 `--max-failures` / `--max-time` 之后摘要里出现 `Skipped` 的来源。

### 进程池与 worker 初始化

`_execute()`（第 141 行）先创建 `parallelism_group` 的信号量，再建 `ProcessPoolExecutor`：

```python
semaphores = {
    k: multiprocessing.BoundedSemaphore(v)
    for k, v in self.lit_config.parallelism_groups.items()
    if v is not None
}
executors = [
    ProcessPoolExecutor(
        max_workers=pool_size,
        initializer=lit.worker.initialize,
        initargs=(self.lit_config, semaphores),
    )
    for pool_size in workers_per_pool_list
]
```

`worker.initialize`（`worker.py` 第 23–33 行）把 `lit_config` 和信号量表复制进每个 worker，然后**忽略 SIGINT**（Ctrl-C 由主进程处理，避免所有 worker 同时抢键盘中断）。worker 收到的是 `Test` 副本，副作用不回传；真正回传的是 `Result`。

### `LIT_BATCH_SIZE`：默认 2

`_dispatch_and_wait()` 第 241–243 行：

```python
default_batch = 2 if self.workers > 1 else 1
batch_size = int(os.getenv("LIT_BATCH_SIZE", default_batch))
```

- 默认 `batch_size=2`（单 worker 时为 1）；`LIT_BATCH_SIZE` 可覆盖。
- `batch_size <= 1` 走 `worker.execute`；否则走 `worker.execute_batch`。
- 批处理减少 IPC 与序列化次数，不改变「测试之间仍然可能并行」的事实。

### `LIT_SUBMISSION_WINDOW`：别一次提交所有测试

第 230–236 行：

```python
window = int(
    os.getenv("LIT_SUBMISSION_WINDOW", SUBMISSION_WINDOW_PER_WORKER * self.workers)
) or len(self.tests)
```

`SUBMISSION_WINDOW_PER_WORKER = 8`（第 30 行），默认窗口 `8 * workers`。源码注释解释：一次性把所有测试 `submit()`，在 Python ≤ 3.11.5 上可能把 executor 的 wakeup pipe 塞满并死锁（CPython issue 105829）。窗口必须大于 `workers + call-queue fetch`，才能让每个 worker 都忙。

`LIT_SUBMISSION_WINDOW=0` 会被 `or len(self.tests)` 解释成「不限制」。除非做对照实验，否则不要这么设。

### 失败上限与超时

每收到一个结果就更新失败计数（第 312–318 行）：

```python
if remote_test.isFailure():
    self.failures += 1
    if self.failures == self.max_failures:
        raise MaxFailuresError()
```

- `--max-failures` 是**全局失败数**，不是「每个测试最多失败几次」。
- 抛出 `MaxFailuresError` 后，`Run.execute()` 的 `finally` 把剩余测试标成 `SKIPPED`；主进程在 `main.py` 第 278–287 行设置并打印 `warning: reached maximum number of test failures, skipping remaining tests`。
- 超时是 `completed.get(timeout=...)` 等不到结果时抛 `run.TimeoutError`，主进程打印 `warning: reached timeout, skipping remaining tests`。
- `--max-time` 是**整次运行**的墙钟上限；单测试超时是 `--timeout`（`config.maxIndividualTestTime`），两者不要混。

### worker 崩溃：`WorkerCrashError`

worker 被信号杀死、OOM、abort 时，future 抛 `BrokenProcessPool`；`run.py` 第 321–322 行把它转成 `WorkerCrashError`（定义在第 45–48 行）。它不是某个测试的 `FAIL`，而是整次运行的内部错误。真实实验（测试杀掉自己的父 worker）：

```text
# RUN: %python -c "import os, signal; os.kill(os.getppid(), signal.SIGKILL)"
```

真实输出：

```
-- Testing: 1 tests, 1 workers --
lit.py: .../main.py:283: error: a worker process crashed: A process in the process pool was terminated abruptly while the future was running or pending.

1 error(s) in tests

Testing Time: 0.03s

Total Discovered Tests: 1
  Skipped: 1 (100.00%)
```

退出码是 `2`。看到 `WorkerCrashError`，不要去找「哪个 CHECK 失败了」；先查测试是否杀了 worker、OOM、或 `ulimit` 太低。`main.py` 第 282–283 行用 `lit_config.error()` 记录，`numErrors > 0` 最终让 lit 以 2 退出。

## 15.4 `worker.py`：一个测试在子进程里怎么跑

`worker.py` 只有 112 行，核心是：

```python
def execute(test):
    with _get_parallelism_semaphore(test):
        result = _execute(test, _lit_config)
    test.setResult(result)
    return test

def execute_batch(tests):
    for test in tests:
        with _get_parallelism_semaphore(test):
            result = _execute(test, _lit_config)
        test.setResult(result)
    return tests
```

`_execute()` 记录 `elapsed`、`start`、`pid`，再调用 `test.config.test_format.execute(test, lit_config)`；对 ShTest 就是 `TestRunner.executeShTest()`。

异常处理在 `_execute_test_handle_errors()`（第 88–102 行）：

- `TestUpdaterException` → `UNRESOLVED`；
- 其他任何异常 → `UNRESOLVED`，输出 `Exception during script execution:` + traceback；
- 如果 `lit_config.debug` 为真，**不捕获**，直接让异常冒泡。这时看到的不是 `UNRESOLVED`，而是 worker 崩溃/进程池异常。这是 `--debug` 的副作用。

### `parallelism_group`：给一组测试装一个信号量

`run.py` 把 `lit_config.parallelism_groups` 里的每个组变成 `BoundedSemaphore`。worker 执行前（`worker.py` 第 71–75 行）：

```python
def _get_parallelism_semaphore(test):
    pg = test.config.parallelism_group
    if callable(pg):
        pg = pg(test)
    return _parallelism_semaphores.get(pg, NopSemaphore())
```

`config.parallelism_group` 有两种写法：

1. **字符串**：`config.parallelism_group = "serial"`，对应 `lit_config.parallelism_groups["serial"] = 1`。
2. **callable**：`config.parallelism_group = fn`，worker 对每个测试调用 `fn(test)`，返回组名或 `None`。

字符串最常用。让 `tests/serial` 下 4 个测试同属上限为 1 的组：

```python
# tests/serial/lit.local.cfg.py
config.parallelism_group = "serial"
# lit.cfg.py
lit_config.parallelism_groups["serial"] = 1
```

每个测试仍 `sleep 0.3`。`-j 4` 跑这 4 个测试：

```
-- Testing: 4 tests, 4 workers --
PASS: LitLab :: tests/serial/s01.test (1 of 4)
PASS: LitLab :: tests/serial/s02.test (2 of 4)
PASS: LitLab :: tests/serial/s03.test (3 of 4)
PASS: LitLab :: tests/serial/s04.test (4 of 4)

Testing Time: 1.22s
```

4 个 0.3 秒的测试串行化了，总时间约 1.22 秒。对比 `tests/pass` 的 8 个测试在 `-j 4` 下 0.61 秒：**worker 有 4 个，但信号量只允许 1 个同组测试同时进入。**

> **callable 的坑（实测）**：把函数定义在 `lit.local.cfg.py` 里再赋给 `config.parallelism_group`，worker 会尝试 pickle 它并失败：`PicklingError: Can't pickle <function parallelism_group ...>`。配置文件的 `exec` globals 不是可导入模块；callable 必须定义在 worker 能 import 的模块里（或使用可 pickle 的顶层对象），不能是局部函数/lambda。日常优先用字符串组名。

## 15.5 `%t` 与 `Output/`：为什么并行不会互相覆盖

每个测试的临时路径由 `test_exec_root + path_in_suite + 文件名` 唯一决定。

`Test.getExecPath()`（`Test.py` 第 336–337 行）调用 `TestSuite.getExecPath()`（第 238–239 行）：

```python
def getExecPath(self, components):
    return os.path.join(self.exec_root, *components)
```

`TestRunner.getTempPaths()`（第 1333–1340 行）：

```python
def getTempPaths(test):
    execpath = test.getExecPath()
    execdir, execbase = os.path.split(execpath)
    tmpDir = os.path.join(execdir, "Output")
    tmpBase = os.path.join(tmpDir, execbase)
    return tmpDir, tmpBase
```

`%t` 的拼法由 `getDefaultSubstitutions()` 组装：第 1364 行 `tmpName = tmpBase + ".tmp"`，第 1409 行 `("t", tmpName)`：

```python
tmpName = tmpBase + ".tmp"
...
substitutions.append(("t", tmpName)),
```

合起来：

```
test_exec_root = /tmp/litlab-a1/Output
path_in_suite = ("tests", "misc", "t01-tmp-file.test")
execpath       = .../Output/tests/misc/t01-tmp-file.test
tmpDir         = .../Output/tests/misc/Output
tmpBase        = .../Output/tests/misc/Output/t01-tmp-file.test
%t             = .../t01-tmp-file.test.tmp
```

`t01` 和 `t02` 的 `%t` 天然不同，哪怕同时跑。真实实验（两个测试都写自己的 `%t`）：

```
PASS: LitLab :: tests/misc/t03-isolation-a.test (1 of 2)
# | A
PASS: LitLab :: tests/misc/t04-isolation-b.test (2 of 2)
# | B
```

### `%t` 是文件路径，不是目录

测试：

```text
# RUN: echo hello > %t
# RUN: cat %t/inside
```

真实失败：

```
# executed command: cat .../t02-tmp-not-dir.test.tmp/inside
# .---command stderr------------
# | [Errno 20] Not a directory: '.../t02-tmp-not-dir.test.tmp/inside'
# `-----------------------------
# error: command failed with exit status: 1
```

`%t` 默认指向一个**不存在的文件路径**。`echo > %t` 会把它创建成普通文件；此时 `%t/inside` 当然失败。要目录就自己建：`mkdir -p %t.dir`，或按 LLVM 常见写法 `rm -rf %t && mkdir %t`（此时 `%t` 是目录，不能再当文件用）。

### `%t` 在同一个测试的多次 RUN 中复用

同一个测试的 `%t` 在所有 `RUN:` 行里是同一个值：

```text
# RUN: echo first > %t
# RUN: echo second >> %t
# RUN: cat %t
```

真实输出：

```
# | first
# | second
```

所以 `%t` 适合放本测试的中间产物；但如果需要每步一个文件，用 `%t.step1`、`%t.step2`。

### `LIT_PRESERVES_TMP` 保留的是哪个临时目录？

`main.py` 的 `execute_in_tmp_dir()`（第 290–318 行）在整次运行开始时创建 `lit-tmp-*`，把 `TMP/TMPDIR/TEMP/TEMPDIR` 指向它，结束后删除。如果环境里已有 `LIT_PRESERVES_TMP`，lit **不创建也不改写**这些变量：

```python
if "LIT_PRESERVES_TMP" not in os.environ:
    tmp_dir = tempfile.mkdtemp(prefix="lit-tmp-")
    tmp_dir_envs = {k: tmp_dir for k in ["TMP", "TMPDIR", "TEMP", "TEMPDIR"]}
    os.environ.update(tmp_dir_envs)  # 同时写进每个 cfg.environment
```

真实实验（测试打印 `TMPDIR`）：

```
# 默认
# | TMPDIR=/tmp/lit-tmp-hziu84hq
# LIT_PRESERVES_TMP=1
# | TMPDIR=<unset>
```

注意：`LIT_PRESERVES_TMP` 管的是这个 `lit-tmp-*` 包装目录，**不是 `Output/`**。`Output/` 下的 `%t` 文件本来就不会被 lit 自动删除；很多测试自己 `rm -rf %t`。所以设置它不会让 `Output/` 多出东西。

## 15.6 排序：`--order=smart` 与负的失败时间

默认 `--order=smart`（`cl_arguments.py` 第 427–431 行）。`main.determine_order()` 第 194–208 行：

```python
elif enum_order == TestOrder.LEXICAL:
    tests.sort(key=lambda t: t.getFullName())
else:
    assert enum_order == TestOrder.SMART, "Unknown TestOrder value"
    tests.sort(
        key=lambda t: (not t.previous_failure, -t.previous_elapsed, t.getFullName())
    )
```

`previous_failure` 和 `previous_elapsed` 来自 suite 的 `.lit_test_times.txt`。`Test.__init__` 第 292–295 行：

```python
if suite.test_times and "/".join(path_in_suite) in suite.test_times:
    time = suite.test_times["/".join(path_in_suite)]
    self.previous_elapsed = abs(time)
    self.previous_failure = time < 0
```

失败时间为什么记负数？`TestTimes.record_test_times()` 第 45–48 行：

```python
# Mark the elapsed time for failed tests as negative so LIT can distingiush failed from
# successful test runs just based on the time value. For this heuristic to work for tests
# whose elapsed time is '0', we set it to a small negative constant.
time = min(-t.result.elapsed, -1.0e-6) if t.isFailure() else t.result.elapsed
```

一个数字同时编码两件事：绝对值 = 上次耗时（排序/进度预测），符号 = 上次是否失败（`--filter-failed` 和 smart 排序）。

真实实验：先让 `f01-false.test` 失败一次，再按 smart 顺序跑：

```
-- Testing: 3 tests, 1 workers --
FAIL: LitLab :: tests/fail/f01-false.test (1 of 3)
PASS: LitLab :: tests/pass/p01.test (2 of 3)
PASS: LitLab :: tests/pass/p02.test (3 of 3)
```

失败测试被排到最前面。`.lit_test_times.txt` 里对应行是负数（下面是一次真实快照；耗时数值每次运行都会变）：

```text
-4.189014e-03 tests/fail/f01-false.test
```

`--time-tests=3` 会基于这些耗时打印直方图：

```
Slowest Tests (3 of 8):
--------------------------------------------------------------------------
0.31s: LitLab :: tests/pass/p04.test
0.30s: LitLab :: tests/pass/p06.test
0.30s: LitLab :: tests/pass/p03.test

Test Times (8):
[    Range    ] :: [               Percentage               ] :: [Count]
[0.300s,0.320s) :: [****************************************] :: [8/8]
```

`--time-tests` 不改变执行，只打印；它不能和 `--skip-test-time-recording` 同时用（`cl_arguments.py` 第 551–554 行直接 `parser.error`）。

> **实测过的并发坑**：同时启动两个 lit 进程跑同一个 suite，会竞争 `Output/.lit_test_times.txt`。我在并行做实验时把文件写成了末尾空行，下一次启动直接 `fatal: found malformed timing data`。这不是测试失败，是计时文件损坏；删掉它重新生成，并且不要把同一个 `Output/` 交给两个 lit 进程。

## 15.7 什么时候该用并行

| 场景 | 建议 | 原因 |
|---|---|---|
| 测试之间无共享 | `-j` 接近可用核数 | 吞吐最高 |
| 会写同一个全局文件/端口/设备 | 放进 `parallelism_group`，上限 1 | 否则 flaky |
| 会 fork 大量线程/进程 | 降低 `-j` 或提高 `ulimit -u` | `_increase_process_limit()` 只能尽力而为 |
| 调试单个失败 | `-j 1 -v` | 输出顺序稳定，现场不被干扰 |

`run.py` 第 336–362 行的 `_increase_process_limit()` 把 `RLIMIT_NPROC` 软限制提高到 `workers * usable_core_count() * 2`，但会把目标值截到硬限制，且 Windows/z/OS/Solaris/Cygwin 上会跳过或警告。它不能解决文件锁、端口占用、共享内存命名冲突。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`-j 32` 就一定会开 32 个 worker」 | `workers = min(len(tests), opts.workers)`；测试少时 worker 更少。 |
| 「`%t` 是临时目录」 | `%t` 是文件路径（`tmpBase + ".tmp"`）。要目录自己 `mkdir`，别写 `%t/foo` 除非先创建 `%t` 为目录。 |
| 「同一个测试的每条 `RUN:` 有独立的 `%t`」 | 同一个测试的所有 `RUN:` 共享同一个 `%t`；要分步文件用 `%t.step1`。 |
| 「worker 崩溃就是一个测试 FAIL」 | worker 崩溃是 `WorkerCrashError`，整次运行报内部错误，退出码 2；未完成测试显示 `SKIPPED`。 |
| 「`--max-time` 是单测试超时」 | `--max-time` 是整次运行上限；单测试用 `--timeout`（`config.maxIndividualTestTime`）。 |
| 「测试之间共享一个文件没关系，反正顺序固定」 | 并行下顺序不固定；共享文件必须放进 `parallelism_group` 或改用 `%t`。 |
| 「两个 lit 进程可以共用一个 `Output/`」 | 会竞争 `.lit_test_times.txt` 和 `%t`；实测可把计时文件写坏，下一次启动 fatal。 |

## 重点整理

- 并行单位是**进程**：`ProcessPoolExecutor` + `worker.execute(_batch)`；主进程只调度与收集。
- `workers = min(len(tests), opts.workers)`；默认 `usable_core_count()`，`LIT_MAX_WORKERS` 可改默认。
- `Run.execute()` 用 `deadline` 实现 `--max-time`，`finally` 把未完成测试标成 `SKIPPED`。
- 默认 `LIT_BATCH_SIZE=2`，提交窗口 `LIT_SUBMISSION_WINDOW=8*workers`；后者是 Python ≤ 3.11.5 的死锁规避。
- `parallelism_group` 是 `BoundedSemaphore`；字符串组名最稳，callable 必须可 pickle。
- `%t` 的唯一性来自 `test_exec_root + path_in_suite + 文件名 + ".tmp"`；并行不覆盖的根本原因在这里。
- `.lit_test_times.txt` 用负数记录失败，同时服务 smart 排序、进度预测和 `--filter-failed`。
- worker 崩溃、超时、失败上限都不是普通 `FAIL`；先看主进程的 warning/error 与退出码。

## 自我检核

不看笔记，试着回答：

- [ ] 为什么 `-j 32` 可能只显示 `8 workers`？`min` 的两个操作数分别是什么？
- [ ] 画出 `test_exec_root`、`path_in_suite`、`%t` 的拼接关系，并解释为什么 `t01` 与 `t02` 不会互相覆盖。
- [ ] `%t` 和 `%t.dir` 的区别是什么？如果测试写 `cat %t/foo` 会发生什么？
- [ ] `parallelism_group` 的信号量是在主进程创建还是 worker 创建？为什么上限 1 的组仍然可能占用多个 worker 进程？
- [ ] worker 崩溃时，lit 的退出码是多少？未完成的测试显示成什么？为什么不是 `FAIL`？
- [ ] `.lit_test_times.txt` 里 `-4.17e-03` 的负号同时表达了哪两件事？

## 延伸阅读

- **[`llvm/utils/lit/lit/run.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/run.py)**
  - 读什么：`Run.execute()`、`_execute()`、`_dispatch_and_wait()`、`_abort_executors()`。
  - 能学到：提交窗口、批次、失败上限、超时、worker 崩溃的完整控制流。
  - 前提：知道 `concurrent.futures.ProcessPoolExecutor` 的基本用法。
- **[`llvm/utils/lit/lit/worker.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/worker.py)**
  - 读什么：全文 112 行。
  - 能学到：worker 初始化、信号量、异常到 `UNRESOLVED` 的映射。
  - 前提：读过本章 15.4。
- **[`llvm/utils/lit/lit/TestRunner.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：`getTempPaths()`（1333）、`getDefaultSubstitutions()`（1351）、`_runShTest()`（2165）。
  - 能学到：`%t` 的真实拼法、重试循环、超时到 `TIMEOUT` 的映射。
  - 前提：知道 `os.path` 的 `join/split/basename`。
> 下一章把「选中哪些测试」拆开：`--filter` 到底匹配什么，shard 怎么切，`--max-failures` 之后为什么剩下的测试变成 `SKIPPED`，以及重试的优先级。

→ [Ch 16 选择、排序、分片与重跑](./16-selection-and-rerun.md)
