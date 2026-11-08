# Ch 22 — Discovery 与 TestingConfig 源码深潜

> **目标**：把「lit 怎么找到 suite、怎么复制配置、缓存什么、什么时候退出 2」拆到函数级：`getTestSuite()` 的向上搜索、`dirContainsTestSuite()` 的 site 优先、`getLocalConfig()` 的 `deepcopy` 与缓存键、`getTestsInSuite()` 的嵌套 suite 判定，以及 `TestingConfig` 的三段式生命周期。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。所有实验在本机实跑；源码行号以 `grep -n` 核对。

---

## 22.1 发现的两条轴：向上找 suite，向下找测试

`discovery.py` 只有 286 行，但它决定了「你输入的路径属于哪个 suite」和「这个 suite 里有哪些测试」。两条轴：

```text
向上（getTestSuite）
  input path
    → 当前目录有 lit.site.cfg / lit.cfg？
    → 没有就 parent，直到根
    → 找到后 TestingConfig.fromdefaults + load_from_path
    → TestSuite(name, source_root, exec_root, cfg)

向下（getTestsInSuite）
  suite root
    → getLocalConfig(dir)
    → test_format.getTestsInDirectory()
    → 递归子目录
    → 子目录里有嵌套 suite？切换 suite
```

先记住三个对象：

| 对象 | 生命周期 | 作用 |
|---|---|---|
| `LitConfig` | 整个进程一份 | CLI 参数、全局开关、`--debug`、`--timeout` |
| `TestingConfig` | 每个 suite 根一份；每个有 `lit.local.cfg` 的目录再复制一份 | 实际被测试读取的配置 |
| `TestSuite` | 每个 suite 根一份 | `name/source_root/exec_root/config/test_times` |

`Test` 对象只持有 `suite`、`path_in_suite`、`config` 和可选的 `file_path/gtest_json_file`。它不复制配置；同一个目录下的所有测试共享同一个 `TestingConfig` 对象（这一点在第 22.3 节用 `id()` 验证）。

## 22.2 `getTestSuite()`：向上搜索与 site 优先

`getTestSuite()` 在 `discovery.py:28`。核心是 `search1(path)`：

```python
cfgpath = dirContainsTestSuite(path, litConfig)
if not cfgpath:
    parent, base = os.path.split(path)
    if parent == path:
        return (None, ())
    ts, relative = search(parent)
    return (ts, relative + (base,))
```

`dirContainsTestSuite()` 在 `discovery.py:21`：

```python
cfgpath = chooseConfigFileFromDir(path, lit_config.site_config_names)
if not cfgpath:
    cfgpath = chooseConfigFileFromDir(path, lit_config.config_names)
return cfgpath
```

顺序是 **site config 优先**：

```text
lit.site.cfg.py  >  lit.site.cfg  >  lit.cfg.py  >  lit.cfg
```

同一目录里同时有 `lit.site.cfg.py` 和 `lit.cfg.py` 时，前者胜出。真实实验：在 `prio/` 放两个配置，分别命名 `PlainCfg` 和 `SiteCfg`，运行：

```bash
lit --show-suites prio
```

真实输出：

```text
-- Test Suites --
  SiteCfg - 1 tests
    Source Root: /tmp/lit22.vQh637/prio
    Exec Root  : /tmp/lit22.vQh637/prio/Output
```

`PlainCfg` 完全没被加载。这是 LLVM 的标准布局：`lit.cfg.py` 是源码里的通用配置，`lit.site.cfg.py` 是构建目录生成的、带绝对路径的配置；site 必须覆盖前者。

### 22.2.1 `config_map`：私有参数，用于路径翻译

找到 `cfgpath` 后：

```python
config_map = litConfig.params.get("config_map")
if config_map:
    cfgpath = util.abs_path_preserve_drive(cfgpath)
    target = config_map.get(os.path.normcase(cfgpath))
    if target:
        cfgpath = target
```

（第 57–62 行。）

`config_map` 不是 CLI `-D` 能直接传的字符串，而是一个 dict，通常由包装脚本通过 `main(builtin_parameters)` 注入。lit 自测里有一个最小驱动：

```python
from lit.main import main

main_config = lit.util.abs_path_preserve_drive(sys.argv[1])
main_config = os.path.normcase(main_config)
config_map = {main_config: sys.argv[2]}
builtin_parameters = {"config_map": config_map}
sys.argv = [sys.argv[0]] + sys.argv[3:] + [os.path.dirname(main_config)]
main(builtin_parameters)
```

真实运行（把 `outer/lit.cfg.py` 映射到 `lit.alt.cfg.py`）：

```text
-- Test Suites --
  ConfigMap - 1 tests
    Source Root: /tmp/lit22.vQh637/cmtests
    Exec Root  : /tmp/lit22.vQh637/cmtests/Output
-- Available Tests --
  ConfigMap :: one.txt
```

stderr 的 debug 行显示实际加载的是 `lit.alt.cfg.py`：

```text
loading suite config '/tmp/lit22.vQh637/lit.alt.cfg.py'
resolved input '/tmp/lit22.vQh637/outer' to 'ConfigMap'::()
```

`config_map` 的用途是「源码路径与构建路径不一致时，让 lit 用另一份配置」。LLVM 的 CMake 集成不靠它，但在测试 lit 自身、或做路径重写实验时很好用。

### 22.2.2 `TestSuite` 与 `test_times`

创建 suite 的代码（第 67–72 行）：

```python
cfg = TestingConfig.fromdefaults(litConfig)
cfg.load_from_path(cfgpath, litConfig)
source_root = util.abs_path_preserve_drive(cfg.test_source_root or path)
exec_root = util.abs_path_preserve_drive(cfg.test_exec_root or path)
return Test.TestSuite(cfg.name, source_root, exec_root, cfg, litConfig), ()
```

`TestSuite.__init__`（`Test.py:220` 起）会立刻读 `.lit_test_times.txt`：

```python
self.test_times = read_test_times(self, lit_config)
```

这份数据用于排序（`--order=smart`）和 `previous_failure`。`Test.__init__` 会查 `suite.test_times["/".join(path_in_suite)]`，设置 `previous_elapsed` 和 `previous_failure`。注意：**测试对象在创建时就把时间抄走了**，所以后面清空 `suite.test_times` 不影响已经创建的 `Test`。

## 22.3 `getLocalConfig()`：缓存键与 `deepcopy`

`getLocalConfig()` 在 `discovery.py:98`：

```python
def search1(path_in_suite):
    if not path_in_suite:
        parent = ts.config
    else:
        parent = search(path_in_suite[:-1])

    source_path = ts.getSourcePath(path_in_suite)
    cfgpath = chooseConfigFileFromDir(source_path, litConfig.local_config_names)
    if not cfgpath:
        return parent

    config = copy.deepcopy(parent)
    litConfig.dbg("loading local config %r" % cfgpath)
    config.load_from_path(cfgpath, litConfig)
    return config

def search(path_in_suite):
    key = (ts, path_in_suite)
    res = cache.get(key)
    if res is None:
        cache[key] = res = search1(path_in_suite)
    return res
```

四个关键点：

1. **缓存键是 `(ts, path_in_suite)`**，不是字符串目录。不同 suite 的同名相对路径不会串。
2. **没有 local cfg 就返回父对象本身**，不是副本。所以 `sub/` 和 `sub/deeper/` 在没有各自的 `lit.local.cfg` 时会共享同一个 `TestingConfig`。
3. **有 local cfg 才 `deepcopy`**。复制是深拷贝：`environment`、`substitutions`、`available_features` 都是新的容器；改子配置不会影响父配置。
4. **`config.parent` 不会被设置**。`deepcopy(parent)` 复制的是父对象的 `parent` 字段；而 `TestingConfig.fromdefaults()` 传入的 `parent` 是 `None`，`getLocalConfig()` 也没有 `config.parent = parent`。所以局部变量 `parent` 只是用来复制，别在配置里指望 `config.parent.name`。

第 4 点是本章实验里最反直觉的发现。真实 `lit.local.cfg.py`：

```python
config.suffixes.add(".extra")
config.available_features.add("subfeature")
config.substitutions.append((
    "%cfginfo",
    "id=%d parent=%s name=%s features=%s" % (
        id(config),
        "None" if config.parent is None else str(id(config.parent)),
        config.name,
        " ".join(sorted(config.available_features))),
))
```

运行 `lit -a outer/sub`，真实输出：

```text
# RUN: at line 1
echo id=139910856231280 parent=None name=Outer features=linux subfeature
# | id=139910856231280 parent=None name=Outer features=linux subfeature
```

`sub/local.test` 和 `sub/deeper/deeper.test` 打印的 `id` 完全相同：`deeper` 没有自己的 local cfg，直接复用了 `sub` 的配置对象。`parent=None` 则证明 `config.parent` 不是目录父配置。

> 如果你需要「子配置相对父配置改了什么」，应该在 local cfg 里自己记录，例如 `config.inherited_features = set(config.available_features)`，然后再修改。不要依赖 `config.parent`。

这也解释了另一个常见 bug：在 local cfg 里 `config.name = "Sub"` 不会改变 suite 名。`Test.getFullName()` 用的是 `test.suite.config.name`，而 suite 的 config 是根配置；local cfg 改的是目录副本的 `name`，只影响读取它的测试。

## 22.4 `getTestsInSuite()`：向下递归与嵌套 suite

`getTestsInSuite()` 在 `discovery.py:149`。它先拿当前目录的 local config，再决定怎么发现：

```python
lc = getLocalConfig(ts, path_in_suite, litConfig, localConfigCache)

if lc.standalone_tests:
    if lc.suffixes or lc.excludes:
        litConfig.warning("standalone_tests set in LIT config but suffixes or excludes are also set")
    return

if lc.test_format is not None:
    for res in lc.test_format.getTestsInDirectory(ts, path_in_suite, litConfig, lc):
        yield res
```

`standalone_tests=True` 表示「这个目录不通过后缀发现测试，测试由外部驱动」。它通常和 `test_format=None` 搭配；如果同时设置了 `suffixes`/`excludes`，lit 会警告，因为那些字段不会被使用。本章未单独构造 `standalone_tests` 实验（未实测），结论来自源码第 179–185 行。

递归子目录时，先跳过三个硬编码目录和 `excludes`：

```python
if filename in ("Output", ".svn", ".git") or filename in lc.excludes:
    continue
```

然后检查嵌套 suite：

```python
subpath = path_in_suite + (filename,)
file_execpath = ts.getExecPath(subpath)
if dirContainsTestSuite(file_execpath, litConfig):
    sub_ts, subpath_in_suite = getTestSuite(file_execpath, litConfig, testSuiteCache)
elif dirContainsTestSuite(file_sourcepath, litConfig):
    sub_ts, subpath_in_suite = getTestSuite(file_sourcepath, litConfig, testSuiteCache)
else:
    sub_ts = None
```

顺序是 **exec path 优先，source path 其次**。原因是 out-of-tree 构建：嵌套 suite 的 `lit.site.cfg.py` 可能只存在于构建目录（`build/test/Unit`），源码目录里只有 `lit.cfg.py`；先看 exec path 才能找到构建时生成的 site 配置。

还有一个防环检查：

```python
if sub_ts is ts:
    continue
```

如果 exec root 位于 source root 内部，递归可能绕回当前 suite；这一步直接跳过，避免无限循环。

真实嵌套实验：`outer/lit.cfg.py`（Outer，`.test`）里有一个 `outer/nested/lit.cfg.py`（Nested，`.deep`）。运行：

```bash
lit --debug --show-tests --show-suites outer
```

stdout：

```text
-- Test Suites --
  Nested - 1 tests
    Source Root: /tmp/lit22.vQh637/outer/nested
    Exec Root  : /tmp/lit22.vQh637/outer/nested/Output
  Outer - 4 tests
    Source Root: /tmp/lit22.vQh637/outer
    Exec Root  : /tmp/lit22.vQh637/outer/Output
    Available Features: linux
-- Available Tests --
  Nested :: deep.deep
  Outer :: sub/deeper/deeper.test
  Outer :: sub/local.extra
  Outer :: sub/local.test
  Outer :: top.test
```

stderr 的 debug 行：

```text
debug: loading suite config '.../outer/lit.cfg.py'
debug: ... loaded config '.../outer/lit.cfg.py'
debug: resolved input 'outer' to 'Outer'::()
debug: loading suite config '.../outer/nested/lit.cfg.py'
debug: ... loaded config '.../outer/nested/lit.cfg.py'
debug: loading local config '.../outer/sub/lit.local.cfg.py'
debug: ... loaded config '.../outer/sub/lit.local.cfg.py'
```

注意：

- `Nested` 是独立 suite，`Nested :: deep.deep` 的 `path_in_suite` 是 `("deep.deep",)`，不是 `("nested","deep.deep")`。
- Outer 的测试列表里没有 `nested/deep.deep`，也没有 `root.extra`；前者被嵌套 suite 接管，后者因为根配置的 `suffixes` 只有 `.test`。
- `Outer - 4 tests` 不含 Nested 的 1 个；总发现数是 5，但分属两个 suite。

## 22.5 `find_tests_for_inputs()`：缓存、清空与退出 2

`find_tests_for_inputs()` 在 `discovery.py:250`：

```python
tests = []
test_suite_cache = {}
local_config_cache = {}
for input in inputs:
    prev = len(tests)
    tests.extend(getTests(input, lit_config, test_suite_cache, local_config_cache)[1])
    if prev == len(tests):
        lit_config.warning("input %r contained no tests" % input)

for k, suite in test_suite_cache.items():
    if suite[0]:
        suite[0].test_times = None

if lit_config.numErrors:
    sys.stderr.write("%d errors, exiting.\n" % lit_config.numErrors)
    sys.exit(2)
```

三个结论：

1. **缓存是每次调用新建的**。`find_tests_for_inputs()` 内部创建 `test_suite_cache` 和 `local_config_cache`，所以跨进程或跨调用不会复用旧配置；编辑 `lit.cfg.py` 后重新运行会重新加载。
2. **发现结束后清空 `suite.test_times`**。注释写得很直白：这些数据不再需要，留着会造成严重的性能问题。前面已经说明，`Test` 对象在创建时抄走了 `previous_elapsed/previous_failure`，所以清空不影响排序和重跑。
3. **发现阶段有 error 就 `sys.exit(2)`**。`numErrors` 由 `litConfig.error()` 累加；配置解析失败、输入没有 suite 等都会走这条路。退出码 2 是「lit 内部错误」，不是「测试失败」。

真实探针（用 `find_tests_for_inputs` 直接发现 `outer`）：

```text
tests: 5
Outer :: top.test suite.test_times= None previous_elapsed= 0.0
Nested :: deep.deep suite.test_times= None previous_elapsed= 0.0
Outer :: sub/local.extra suite.test_times= None previous_elapsed= 0.0007250309
Outer :: sub/local.test suite.test_times= None previous_elapsed= 0.003240585
```

`suite.test_times` 已经是 `None`，`previous_elapsed` 仍有值。这正是「先抄后清」的证据。

`getTests()`（第 131 行）负责把输入解析成 `(suite, path_in_suite)`：

```python
ts, path_in_suite = getTestSuite(path, litConfig, testSuiteCache)
if ts is None:
    litConfig.warning("unable to find test suite for %r" % path)
    return (), ()
return ts, getTestsInSuite(ts, path_in_suite, litConfig, testSuiteCache, localConfigCache)
```

直接传一个文件时，`path_in_suite` 会包含文件名。真实 debug：

```text
resolved input 'outer/sub/local.test' to 'Outer'::('sub', 'local.test')
loading local config '.../outer/sub/lit.local.cfg.py'
-- Available Tests --
  Outer :: sub/local.test
```

如果传的是目录，`path_in_suite` 是相对 suite 根的目录元组；如果传的是 suite 根，它是空元组。

## 22.6 `TestingConfig` 三段式：fromdefaults → load → finish

`TestingConfig` 在 `TestingConfig.py`，303 行。生命周期固定三步：

```text
fromdefaults(litConfig)        # 默认值 + 环境白名单
        ↓
load_from_path(path, litConfig) # exec 配置文件
        ↓
finish(litConfig)               # 类型归一化 + CLI 覆盖
```

### 22.6.1 `fromdefaults()`：环境不是全量继承

第 11 行开始。它创建一个**最小环境**：

```python
environment = {
    "PATH": os.pathsep.join(litConfig.path + [os.environ.get("PATH", "")]),
    "LLVM_DISABLE_CRASH_REPORT": "1",
}
```

然后只透传一个白名单：`LIBRARY_PATH`、`LD_LIBRARY_PATH`、`HOME`、`TMPDIR`、`FILECHECK_OPTS`、`ASAN_OPTIONS`、`GTEST_FILTER` 等（完整列表在 `TestingConfig.py:20-78`）。**父进程的所有环境变量不会自动传给测试**；这是可复现性与安全设计。

默认值还包括：

- `pipefail=True`。
- `available_features=[]`（除非 `--vg`）。
- `suffixes=set()`、`test_format=None`、`excludes=[]`。
- `maxIndividualTestTime=litConfig.maxIndividualTestTime`。

### 22.6.2 `load_from_path()`：`exec` 与 `cfg_globals`

第 135 行：

```python
data = None
try:
    with open(path) as f:
        data = f.read()
except OSError:
    litConfig.fatal("unable to load config file: %r" % (path,))

cfg_globals = dict(globals())
cfg_globals["config"] = self
cfg_globals["lit_config"] = litConfig
cfg_globals["__file__"] = path
try:
    exec(compile(data, path, "exec"), cfg_globals, None)
    litConfig.dbg("... loaded config %r" % path)
except SystemExit:
    e = sys.exc_info()[1]
    if e.args:
        raise
except:
    import traceback
    litConfig.fatal("unable to parse config file %r, traceback: %s" % (path, traceback.format_exc()))
self.finish(litConfig)
```

要点：

- `cfg_globals` 预置 `config`、`lit_config`、`__file__`，所以配置文件里可以直接写 `config.name = ...`，也可以用 `__file__` 定位自己。
- `exec(..., cfg_globals, None)` 的 locals 是 `None`，配置里的赋值直接写进 globals；这就是为什么 `config` 能被修改。
- `SystemExit` 是**允许**的：配置文件里 `sys.exit()` 且不带参数会静默返回；带参数才继续抛。`warning()` 不终止流程，只累加 `numWarnings`；`fatal()` 直接 `sys.exit(2)`，所以配置错误不会进入执行阶段。
- 其它任何异常都被 `litConfig.fatal()` 捕获，打印 traceback 并 `sys.exit(2)`。注意 `fatal` 不会返回。
- `finish()` 在 `load_from_path()` 末尾自动调用；你不需要手动调。

### 22.6.3 `finish()`：CLI 覆盖与类型归一化

第 235 行：

```python
self.name = str(self.name)
self.suffixes = set(self.suffixes)
self.environment = dict(self.environment)
self.substitutions = list(self.substitutions)
if self.test_exec_root is not None:
    self.test_exec_root = str(self.test_exec_root)
if self.test_source_root is not None:
    self.test_source_root = str(self.test_source_root)
self.excludes = set(self.excludes)
if litConfig.maxRetriesPerTest is not None and getattr(self, "test_retry_attempts", None) is None:
    self.test_retry_attempts = litConfig.maxRetriesPerTest
if litConfig.maxIndividualTestTime is not None:
    suite_timeout = self.maxIndividualTestTime
    if suite_timeout > 0 and suite_timeout != litConfig.maxIndividualTestTime:
        litConfig.note("The test suite ... Forcing timeout to be ...")
    self.maxIndividualTestTime = litConfig.maxIndividualTestTime
```

两类行为：

1. **类型归一化**：`suffixes/excludes` 转 set，`environment` 复制 dict，`substitutions` 复制 list，`name/test_exec_root/test_source_root` 转 str。配置文件里写 list、tuple、str 都能被接受。
2. **CLI 覆盖**：`--max-retries-per-test` 只在 suite 没设 `test_retry_attempts` 时写入；`--timeout` **总是**覆盖 suite 的 `maxIndividualTestTime`，并在两者冲突时打印 note。

`finish()` 每次 `load_from_path()` 都调用；local cfg 复制父配置后再 `finish()` 一次，所以类型归一化会重复执行，但幂等。

### 22.6.4 字段在对象上的位置

`TestingConfig.__init__`（第 174 行）的字段很多，按用途分组：

| 字段 | 默认 | 谁读 |
|---|---|---|
| `name` | `<unnamed>` | `Test.getFullName()`、报告 |
| `suffixes` | `set()` | `FileBasedTest.getTestsForPath()` |
| `test_format` | `None` | `getTestsInSuite()`、`worker` |
| `environment` | 白名单 + PATH | `lit.util.executeCommand` |
| `substitutions` | `[]` | `getDefaultSubstitutions()` |
| `available_features` | `set()` | `REQUIRES/UNSUPPORTED/XFAIL`、`%if` |
| `pipefail` | `True` | `ShParser`、外部 shell |
| `standalone_tests` | `False` | `getTestsInSuite()` |
| `limit_to_features` | `set()` | `Test.isWithinFeatureLimits()` |
| `parallelism_group` | `None` | `worker._get_parallelism_semaphore()` |
| `maxIndividualTestTime` | `0` | `executeCommand(timeout=...)` |
| `test_retry_attempts` | 可能不存在 | `Test.allowed_retries` 的初始化 |

`parallelism_group` 可以是字符串或 callable（`worker.py` 里 `pg = pg(test)`），用于把共享资源的测试串行化。`maxIndividualTestTime` 是 **suite 级默认**，单个测试可以用 `--timeout` 覆盖，`ShTest` 的 `_runShTest` 也会读它。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「改了 `lit.cfg.py`，正在跑的 lit 会立刻生效」 | 配置在发现阶段加载一次，`Test` 对象持有同一个 `TestingConfig`；运行中改文件不会重新加载。重新起进程才会读新配置 |
| 「`config.parent` 指向目录父配置」 | 当前实现里 `fromdefaults()` 传 `parent=None`，`getLocalConfig()` 只 `deepcopy` 不写回 `parent`；`config.parent` 实际是 `None`。要记录继承关系就自己存字段 |
| 「没有 local cfg 的子目录会得到一份新配置」 | `getLocalConfig()` 在没有 local cfg 时直接返回父对象；`sub/` 与 `sub/deeper/` 共享同一个 `TestingConfig`（`id()` 相同） |
| 「`deepcopy` 后改子配置会影响父配置」 | 深拷贝后 `environment/substitutions/available_features` 都是新容器；改子配置不影响兄弟目录或父 suite |
| 「嵌套 suite 先看 source path」 | 先看 **exec path**，再看 source path；out-of-tree 构建的 site cfg 常常只在构建目录里 |
| 「`test_times` 在发现后还能用来排序」 | `find_tests_for_inputs()` 结束前把 `suite.test_times = None`；排序/重跑信息已经在创建 `Test` 时抄走 |
| 「发现阶段出错也会继续跑」 | `lit_config.numErrors` 非零时直接 `sys.exit(2)`；退出码 2 是 lit 内部错误，不是测试失败 |
| 「父进程环境变量会自动传给测试」 | `fromdefaults()` 只透传白名单；要传额外变量用 `--pass-env` 或 `config.environment[...] = ...` |

## 重点整理

1. `dirContainsTestSuite()` 的优先级是 site config > config；同一目录同时存在两者时，site 胜出。
2. `getTestSuite()` 从输入路径向上走，找到配置后 `TestingConfig.fromdefaults()` + `load_from_path()`，再用 `test_source_root/test_exec_root` 或配置目录创建 `TestSuite`。
3. `getLocalConfig()` 的缓存键是 `(TestSuite, path_in_suite)`；没有 local cfg 时返回父对象本身，有才 `deepcopy`。
4. `config.parent` 在当前实现里是 `None`；不要用它做目录继承。
5. `getTestsInSuite()` 先看 exec path 的嵌套 suite，再看 source path；`sub_ts is ts` 时跳过，防止 exec root 在 source root 内造成环。
6. `find_tests_for_inputs()` 每次调用新建缓存，发现后清空 `suite.test_times`，`numErrors` 非零则退出 2。
7. `TestingConfig` 生命周期是 `fromdefaults → load_from_path → finish`；`finish()` 做类型归一化和 CLI 覆盖。
8. 环境变量按白名单透传，`standalone_tests/limit_to_features/parallelism_group/maxIndividualTestTime` 都在 `TestingConfig` 对象上，不在 `LitConfig` 上。

## 自我检核

- [ ] 画出 `getTestSuite()` 的向上搜索路径，说明 site cfg 与普通 cfg 的优先级，以及 `config_map` 在哪一步介入。
- [ ] 给定 `sub/` 和 `sub/deeper/`，判断它们是否共享同一个 `TestingConfig`，并说明 `id()` 会相同还是不同。
- [ ] 解释 `config.parent` 为什么是 `None`；如果要在 local cfg 里记录继承链，你会怎么写？
- [ ] 说明嵌套 suite 为什么先检查 exec path；如果只检查 source path，out-of-tree 构建会漏掉什么？
- [ ] 解释 `find_tests_for_inputs()` 为什么在返回前清空 `test_times`，以及为什么这不会破坏 `previous_elapsed`。
- [ ] `--timeout` 与 suite 的 `maxIndividualTestTime` 冲突时，谁赢？`finish()` 会做什么额外动作？

## 延伸阅读

- **`llvm/utils/lit/lit/discovery.py:21-130`**
  - 读哪里：`dirContainsTestSuite()`、`getTestSuite()`、`getLocalConfig()`。
  - 能学到什么：向上搜索、site 优先、缓存键、`deepcopy` 的精确位置。
  - 前提知识：Ch 3 的配置级联；本章 22.2–22.3。
- **`llvm/utils/lit/lit/discovery.py:149-290`**
  - 读哪里：`getTestsInSuite()`、`find_tests_for_inputs()`。
  - 能学到什么：嵌套 suite 的 exec/source 优先级、`standalone_tests`、`test_times` 清空、退出 2。
  - 前提知识：本章 22.4–22.5。
- **`llvm/utils/lit/lit/TestingConfig.py`（全文 303 行）**
  - 读哪里：`fromdefaults()`、`load_from_path()`、`finish()`、`__init__` 字段。
  - 能学到什么：环境白名单、`cfg_globals` 的注入、CLI 覆盖、类型归一化。
  - 前提知识：本章 22.6；Ch 10 的配置对象。
- **`llvm/utils/lit/tests/discovery.py` 与 `tests/Inputs/discovery/`、`tests/Inputs/config-map-discovery/`**
  - 读哪里：官方对嵌套 suite、local cfg、`config_map` 的断言。
  - 能学到什么：如何用 `--debug --show-tests --show-suites` 做发现层回归测试。
  - 前提知识：本章 22.2–22.5。

下一章换一种测试发现方式：GoogleTest 不解析文件里的 `RUN:`，而是执行 `--gtest_list_tests`，把可执行文件切成虚拟 shard，再用 JSON 把结果映射回单个 case。

→ [Ch 23 GoogleTest format：另一种测试发现](./23-googletest-format.md)
