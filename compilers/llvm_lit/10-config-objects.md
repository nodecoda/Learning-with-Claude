# Ch 10 — lit_config 与 config：两个对象的一生

> **目标**：能说清 `LitConfig` 与 `TestingConfig` 各自活多久、谁创建谁、字段从哪来；能读懂 `fromdefaults() → load_from_path() → finish()` 这条链，并用 `--debug` 证明「每个目录一份 `config`」不是比喻。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码位于 `/home/ubuntu/llvm/llvm-project/llvm/utils/lit`。本章实验在 `/tmp` 自建 suite 实跑；所有行号都在该 commit 上用 `grep -n` 核对过。

---

## 10.1 先把两个名字分开

lit 里有两条完全不同的「配置生命线」：

```
一次 lit 进程
│
├─ LitConfig        ← 全局，只有一份；来自命令行
│    progname / path / params / config_names / diagnostic_level
│    maxIndividualTestTime / maxRetriesPerTest / noExecute ...
│
└─ 每个测试目录
     TestingConfig  ← 每个目录一份；来自 lit.cfg.py / lit.local.cfg.py
       name / test_format / suffixes / environment
       substitutions / available_features / test_exec_root ...
```

| 维度 | `LitConfig` | `TestingConfig` |
|---|---|---|
| 数量 | 整个进程一份 | 每个目录一份（local cfg 是副本） |
| 谁创建 | `main.py::main()` 从 CLI 构造 | `discovery.py` 为每个 suite/目录创建 |
| 管什么 | 进程级开关、参数、配置文件名、诊断级别 | 这一个目录的测试长什么样、怎么跑 |
| 配置里叫什么 | `lit_config` | `config` |
| 典型字段 | `params`、`path`、`maxIndividualTestTime` | `suffixes`、`test_format`、`environment` |
| 能否被测试目录改写 | 一般只读；`params` 可读 | 可任意改写，local cfg 就是来改它的 |

一句最容易记的话：

> `lit_config` 管「这一次 lit 怎么跑」；`config` 管「这个目录里的测试怎么跑」。

`LitConfig` 的类注释说它「shared across all tests」，并且「always passed in as the global variable `lit`」（`LitConfig.py:14-22`）。但注意：当前 `load_from_path()` 实际注入的名字是 `config`、`lit_config`、`__file__`（`TestingConfig.py:151-155`），**没有注入 `lit`**。配置文件里写 `lit.formats.ShTest()` 而不 `import lit.formats`，会得到 `NameError: name 'lit' is not defined`（实测）。类注释在这里滞后于实现。

## 10.2 `LitConfig`：全局只有一份

构造函数在 `LitConfig.py:24-101`。挑关键字段看：

```python
# LitConfig.py:47-73（节选）
self.progname = progname
self.path = [str(p) for p in path]
self.params = dict(params)
self.pass_env = list(pass_env)
self.config_prefix = config_prefix or "lit"
self.suffixes = ["cfg.py", "cfg"]
self.config_names = ["%s.%s" % (self.config_prefix, x) for x in self.suffixes]
self.site_config_names = [
    "%s.site.%s" % (self.config_prefix, x) for x in self.suffixes
]
self.local_config_names = [
    "%s.local.%s" % (self.config_prefix, x) for x in self.suffixes
]
```

这几行解释了 Ch 3 的三组文件名：`lit.cfg.py`/`lit.cfg`、`lit.site.cfg.py`/`lit.site.cfg`、`lit.local.cfg.py`/`lit.local.cfg`。`--config-prefix asan` 只是把 `config_prefix` 换成 `asan`，三组名字一起变。

`LitConfig` 还保存 `noExecute`（`--no-execute`）、`debug`（`--debug`）、`maxIndividualTestTime`（`--timeout`）、`maxRetriesPerTest`（`--max-retries-per-test`）、`parallelism_groups`（`worker.py:72` 会读 `test.config.parallelism_group` 来找它）等进程级开关。

它自己不会读 `lit.cfg.py`。唯一一个「加载别的配置」的入口是：

```python
# LitConfig.py:167-172
def load_config(self, config, path):
    """load_config(config, path) - Load a config object from an alternate
    path."""
    self.dbg("load_config from %r" % path)
    config.load_from_path(path, self)
    return config
```

注意它没有新建 `TestingConfig`，只是把「已有的 config 对象」和「另一个文件路径」交给 `load_from_path()`。Ch 12 的 site cfg 就是靠这一行回加载源码树的 `lit.cfg.py`。

## 10.3 `TestingConfig`：每个目录一份

`TestingConfig` 的定义在 `TestingConfig.py:5-8`。它有三个关键类方法/属性：`fromdefaults()`、`load_from_path()`、`finish()`。

### 10.3.1 `fromdefaults()`：先造一个「空壳」

`discovery.py:67-68` 每发现一个 suite，都先做：

```python
cfg = TestingConfig.fromdefaults(litConfig)
cfg.load_from_path(cfgpath, litConfig)
```

`fromdefaults()` 在 `TestingConfig.py:10-133`。它先造环境：

```python
# TestingConfig.py:17-21
environment = {
    "PATH": os.pathsep.join(litConfig.path + [os.environ.get("PATH", "")]),
    "LLVM_DISABLE_CRASH_REPORT": "1",
}
```

这里有两个固定行为：

1. **PATH 是「`--path` 追加项 + 宿主 PATH」拼出来的**，不是原样复制。
2. **`LLVM_DISABLE_CRASH_REPORT=1` 无条件注入**。它跟宿主有没有设置无关，目的是让 LLVM 工具在测试里崩溃时不要弹 crash reporter、不要写崩溃报告，避免污染测试输出。

然后是白名单 `pass_vars`（`TestingConfig.py:23-70`），基础列表共 46 个名字（Windows/zos 还会追加）。按用途分组：

| 类别 | 代表变量 | 为什么放行 |
|---|---|---|
| 动态库/加载器 | `LIBRARY_PATH`、`LD_LIBRARY_PATH`、`LD_PRELOAD` | 让测试能找到刚构建的共享库 |
| sanitizer | `ASAN_OPTIONS`、`LSAN_OPTIONS`、`HWASAN_OPTIONS`、`MSAN_OPTIONS`、`RTSAN_OPTIONS`、`TSAN_OPTIONS`、`UBSAN_OPTIONS` | 保留用户对 sanitizer 行为的控制 |
| symbolizer | `LLVM_SYMBOLIZER_PATH`、`ASAN_SYMBOLIZER_PATH`、`HWASAN_SYMBOLIZER_PATH` 等 | 崩溃栈需要 symbolizer |
| 临时目录 | `TMPDIR`、`TMP`、`TEMP`、`TEMPDIR` | 测试可能把临时文件放到宿主指定位置 |
| 工具/设备 | `CLANG`、`LLDB`、`ADB`、`ANDROID_SERIAL`、`AVRLIT_BOARD`、`QEMU_CPU` | 交叉/设备测试需要 |
| 其他 | `TERM`、`HOME`、`FILECHECK_OPTS`、`SOURCE_DATE_EPOCH`、`GTEST_FILTER` | 终端、FileCheck、可复现构建、gtest 过滤 |

`--pass-env NAME` 会追加到白名单末尾（`TestingConfig.py:72-74`）：

```python
pass_vars += litConfig.pass_env
```

然后逐个从 `os.environ` 取值，**只放非空值**（`TestingConfig.py:104-109`）：

```python
for var in pass_vars:
    val = os.environ.get(var, "")
    if val:
        environment[var] = val
```

空字符串不会进 `config.environment`。这是有意的：某些平台上 `LD_PRELOAD=""` 会直接报错。

最后 `fromdefaults()` 返回一个默认对象（`TestingConfig.py:118-133`），关键默认值：

```python
name="<unnamed>",
suffixes=set(),
test_format=None,
environment=environment,
substitutions=[],
unsupported=False,
test_exec_root=None,
test_source_root=None,
excludes=[],
available_features=available_features,
pipefail=True,
standalone_tests=False,
maxIndividualTestTime=litConfig.maxIndividualTestTime,
```

这里就是本章要记住的三个默认值：

- `suffixes=set()`：不写 `suffixes`，一个测试也发现不了。
- `pipefail=True`：管道里任何一段失败都算失败；写成 `False` 会掩盖中间失败。
- `environment` 只有白名单 + `LLVM_DISABLE_CRASH_REPORT` + PATH，不是全量环境。

### 10.3.2 `load_from_path()`：把 Python 文件 `exec` 进当前对象

完整代码在 `TestingConfig.py:135-172`。核心只有几行：

```python
# TestingConfig.py:151-157
cfg_globals = dict(globals())
cfg_globals["config"] = self
cfg_globals["lit_config"] = litConfig
cfg_globals["__file__"] = path
try:
    exec(compile(data, path, "exec"), cfg_globals, None)
    litConfig.dbg("... loaded config %r" % path)
```

这解释了配置文件里为什么能直接写 `config.name = ...`：

- lit 先复制一份**当前模块的 globals**；
- 再塞进 `config`（就是 `self`，这个目录的 `TestingConfig`）；
- 再塞进 `lit_config`（全局 `LitConfig`）；
- 再塞进 `__file__`（配置文件自己的路径，所以 `os.path.dirname(__file__)` 可用）；
- 最后 `exec` 这个文件。

它不是 `import`，也不是 `include`。没有模块缓存，没有 `sys.modules` 条目，执行完就结束。同一个文件被 `load_config()` 调用两次，就会执行两次。

异常处理也值得看：

```python
# TestingConfig.py:159-171（节选）
except SystemExit:
    e = sys.exc_info()[1]
    if e.args:
        raise
except:
    import traceback
    litConfig.fatal(
        "unable to parse config file %r, traceback: %s" ...
    )
```

- 配置文件里 `sys.exit()` 且**没带参数**：lit 当成正常结束，继续。
- `sys.exit("message")` 或别的异常：`lit_config.fatal()`，进程退出码 2。
- 最后一行 `self.finish(litConfig)`（`TestingConfig.py:172`）一定会执行。

### 10.3.3 `finish()`：归一化 + 命令行覆盖

`finish()` 在 `TestingConfig.py:235-268`。它只做四类事。

**第一类：类型归一化。**

```python
# TestingConfig.py:238-250
self.name = str(self.name)
self.suffixes = set(self.suffixes)
self.environment = dict(self.environment)
self.substitutions = list(self.substitutions)
if self.test_exec_root is not None:
    self.test_exec_root = str(self.test_exec_root)
if self.test_source_root is not None:
    self.test_source_root = str(self.test_source_root)
self.excludes = set(self.excludes)
```

所以配置里写列表、元组、集合都能接受，`finish()` 之后统一成：

| 字段 | `finish()` 之后 |
|---|---|
| `name` | `str` |
| `suffixes` | `set` |
| `environment` | `dict` |
| `substitutions` | `list` |
| `test_exec_root` / `test_source_root` | `str` 或 `None` |
| `excludes` | `set` |

**第二类：重试次数的兜底。**

```python
# TestingConfig.py:251-255
if (
    litConfig.maxRetriesPerTest is not None
    and getattr(self, "test_retry_attempts", None) is None
):
    self.test_retry_attempts = litConfig.maxRetriesPerTest
```

注意条件：只有 `config.test_retry_attempts` **完全没写**时，才用命令行的 `--max-retries-per-test`。写了 `0` 也算写了，会压过命令行。这一点在 Ch 11 会用实验证明。

**第三类：单测试超时的命令行覆盖。**

```python
# TestingConfig.py:256-268
if litConfig.maxIndividualTestTime is not None:
    suite_timeout = self.maxIndividualTestTime
    if suite_timeout > 0 and suite_timeout != litConfig.maxIndividualTestTime:
        litConfig.note(...)
    self.maxIndividualTestTime = litConfig.maxIndividualTestTime
```

只要命令行给了 `--timeout`，它一定赢；如果 suite 自己也写了不同的非零值，lit 会打一条 note。真实输出：

```
lit.py: .../TestingConfig.py:260: note: The test suite 'Toy' configuration requested an individual test timeout of 1 seconds but a timeout of 5 seconds was requested on the command line. Forcing timeout to be 5 seconds.
```

**第四类：默认值已经不在 `finish()` 里了。**

`pipefail`、`standalone_tests`、`parallelism_group`、`limit_to_features`、`recursiveExpansionLimit` 都在构造函数里初始化（`TestingConfig.py:174-233`），`finish()` 不会重新赋值。所以「`finish()` 每次访问字段时都会跑」是错的：它只在配置加载末尾跑一次。

## 10.4 生命周期：一个 suite 一份，每个目录再复制

`discovery.py` 的真实顺序：

```
输入路径
  │
  ├─ getTestSuite() 向上找 lit.cfg / lit.site.cfg
  │    ├─ TestingConfig.fromdefaults(litConfig)     ← 空壳
  │    ├─ cfg.load_from_path(cfgpath, litConfig)    ← 执行 suite 配置
  │    └─ Test.TestSuite(name, source_root, exec_root, cfg, litConfig)
  │
  └─ getTestsInSuite() 向下递归
       ├─ getLocalConfig() 每层找 lit.local.cfg
       │    ├─ 没有：直接复用父 config 对象
       │    └─ 有：config = copy.deepcopy(parent)
       │             config.load_from_path(cfgpath, litConfig)
       └─ 用该目录的 config 生成 Test 对象
```

对应源码：

```python
# discovery.py:67-71
cfg = TestingConfig.fromdefaults(litConfig)
cfg.load_from_path(cfgpath, litConfig)
source_root = util.abs_path_preserve_drive(cfg.test_source_root or path)
exec_root = util.abs_path_preserve_drive(cfg.test_exec_root or path)
return Test.TestSuite(cfg.name, source_root, exec_root, cfg, litConfig), ()
```

```python
# discovery.py:110-119
if not cfgpath:
    return parent

config = copy.deepcopy(parent)
litConfig.dbg("loading local config %r" % cfgpath)
config.load_from_path(cfgpath, litConfig)
return config
```

所以：

- **suite 根目录**：`fromdefaults()` 造新对象，`load_from_path()` 执行 `lit.cfg.py`。
- **没有 local cfg 的子目录**：直接复用父对象，`id(config)` 不变。
- **有 local cfg 的子目录**：`deepcopy` 出一份新对象，`id(config)` 变，执行 `lit.local.cfg.py`。
- 每个 `Test` 保存的是它所在目录的 `config`（`Test.py:250`），所以同一个测试文件在不同目录可以有不同的 suffixes/features/environment。

### 10.4.1 实验：打印 `id(config)`

在 suite 根 `lit.cfg.py` 写：

```python
lit_config.note(
    "ROOT id=%d name=%r parent=%r root_id=%d params=%r"
    % (id(config), config.name, config.parent, id(config.root), lit_config.params)
)
```

在 `tests/sub/lit.local.cfg.py` 和 `tests/sub/deep/lit.local.cfg.py` 各写一行类似的 note。跑：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --debug -Dfoo=bar tests
```

真实输出（去掉无关的 PASS 行）：

```
lit.py: .../lit.cfg.py:10: note: ROOT id=140266596386128 name='Ch10' parent=None root_id=140266596386128 params={'foo': 'bar'}
lit.py: .../tests/sub/lit.local.cfg.py:1: note: LOCAL id=140266597493744 name='Ch10' parent=None root_id=140266597493744 root_name='Ch10'
lit.py: .../tests/sub/deep/lit.local.cfg.py:1: note: DEEP id=140266596155200 parent=None root_id=140266596155200
```

三个 `id` 互不相同：根、`sub`、`sub/deep` 是三个对象。这证明了「每个目录一份 `config`」。

但这里有一个必须说清的实测事实：

> **在当前 commit，`config.parent` 是 `None`，`config.root` 就是 `config` 自己。**

为什么？`TestingConfig.__init__` 确实有 `parent` 参数（`TestingConfig.py:174-194`），`root` 属性也确实是沿着 `parent` 向上找（`TestingConfig.py:270-276`）。但 `discovery.py` 在 `getTestSuite()` 里调 `fromdefaults()`，传入的 `parent` 是 `None`；`getLocalConfig()` 用 `copy.deepcopy(parent)` 复制后，也**没有**把副本的 `parent` 指向被复制的对象。于是整条链上没有任何地方设置非空 `parent`。

这带来两个结论：

1. 不要写 `config.parent.name` 来拿 suite 名。本版本会得到 `AttributeError: 'NoneType' object has no attribute 'name'`。要拿 suite 名，用 `config.name`；它就是 suite 名，local cfg 复制后仍是同一个字符串。
2. `config.root.targets` 仍然能用，但原因不是「root 沿父链找到根」，而是 **`deepcopy` 把根配置的所有字段都复制到了 local 副本**。`root` 恰好返回自身，所以 `config.root.targets` 等价于 `config.targets`。LLVM 的 `lit.local.cfg` 大量这么写（例如 `llvm/test/CodeGen/NVPTX/lit.local.cfg` 的 `config.root.targets`），它依赖的是字段被复制，不是对象链。

这是「读源码优先于读教程」的又一个例子：教程里的 `parent` 语义在别的版本或别的调用方式下可能成立，但当前发现路径没有用它。

## 10.5 LLVM site cfg 里那四个常用字段从哪来

读 LLVM 的 `lit.local.cfg` 时，最常看到 `config.root.targets`、`config.available_features`、`config.substitutions`、`config.environment`。它们的来源可以逐条落到文件：

| 字段 | 来源 | 证据 |
|---|---|---|
| `config.targets` | site cfg 的 `config.targets_to_build`，在 `lit.cfg.py` 里 `split()` 成 frozenset | `llvm/test/lit.site.cfg.py.in:31`、`llvm/test/lit.cfg.py:496` |
| `config.available_features` | `llvm.initialize()` 加平台/三元组/sanitizer；`lit.cfg.py` 再加 target、字节序、插件等 | `llvm/utils/lit/lit/llvm/config.py:76-105`、`llvm/test/lit.cfg.py:499` |
| `config.substitutions` | `lit.cfg.py` 先 `use_default_substitutions()`，再追加 site 专属项和工具路径 | `llvm/test/lit.cfg.py:143`、`146-150`、`492` |
| `config.environment` | `fromdefaults()` 的白名单 + `llvm_config.with_environment()` 追加 PATH/LD_LIBRARY_PATH 等 | `TestingConfig.py:18-109`、`llvm/test/lit.cfg.py:99`、`102` |

**`config.targets`**：site cfg 先写 `config.targets_to_build = "@TARGETS_TO_BUILD@"`（`lit.site.cfg.py.in:31`），`TARGETS_TO_BUILD` 由 `configure_lit_site_cfg` 从 `LLVM_TARGETS_TO_BUILD` 拼出（`AddLLVM.cmake:2216-2219`）。源码 `lit.cfg.py` 再把它变成集合：

```python
# llvm/test/lit.cfg.py:496-499
config.targets = frozenset(config.targets_to_build.split())
for arch in config.targets_to_build.split():
    config.available_features.add(arch.lower() + "-registered-target")
```

所以 `if "NVPTX" not in config.root.targets` 查的是构建时启用的后端列表，不是源码里有没有这个目录。

**`config.available_features`**：`llvm.initialize()` 先加平台特征 `system-linux`、`host=<triple>`、`target=<triple>`、`native` 和 sanitizer 特征（`llvm/config.py:76-105`）；`lit.cfg.py:499-508` 再加 `x86-registered-target`、`llvm-64-bits`、`host-byteorder-*-endian` 等。`REQUIRES:` / `UNSUPPORTED:` / `XFAIL:` 里的 token 就是在这个集合里查。Ch 13 会专门讲 feature 的传播。

**`config.substitutions`**：`llvm_config.use_default_substitutions()` 加的是 `%python`、`FileCheck`、`| count`、`| not` 和一组 `%errc_*`（`llvm/config.py:503-526`）。之后 `lit.cfg.py:146-150` 追加 `%llvmshlibdir`、`%shlibext`、`%pluginext`、`%exeext`、`%llvm_src_root`；`lit.cfg.py:492` 的 `add_tool_substitutions(tools, config.llvm_tools_dir)` 把 `opt`、`llc`、`FileCheck` 等解析成绝对路径。`config.substitutions` 是**有序列表**，先匹配的赢；Ch 14 会讲 `ToolSubst` 的优先级。

**`config.environment`**：起点是 `fromdefaults()` 的白名单；`lit.cfg.py:99` 用 `with_environment("PATH", config.llvm_tools_dir, append_path=True)` 把构建工具目录放到 PATH 前面并去重；`lit.cfg.py:102` 用 `with_system_environment` 只从宿主拿 `HOME`、`INCLUDE`、`LIB`、`TMP`、`TEMP`。它们和 `config.environment["X"] = ...` 的差别在 Ch 11 展开。

## 10.6 `lit_config.params` 与 `-D/--param`

`main.py` 在构造 `LitConfig` 前先把参数合并：

```python
# main.py:25-29
def main(builtin_params={}):
    opts = lit.cl_arguments.parse_args()
    params = create_params(builtin_params, opts.user_params)
```

```python
# main.py:161-167
def create_params(builtin_params, user_params):
    def parse(p):
        return p.split("=", 1) if "=" in p else (p, "")

    params = dict(builtin_params)
    params.update([parse(p) for p in user_params])
    return params
```

所以 `lit_config.params` 里有两类东西：

1. **内置参数**：由嵌入 lit 的 Python 代码通过 `main(builtin_params)` 传入。当前源码里唯一被消费的内置参数是 `config_map`（`discovery.py:57-62`），用于把「发现到的配置路径」映射到另一个路径。它不是给命令行用的；`lit -Dconfig_map=x` 会得到一个字符串，随后 `config_map.get(...)` 直接抛 `AttributeError`（实测）。
2. **用户参数**：`-Dfoo=bar` / `--param foo=bar`，通过 `create_params()` 进 `params`。

配置文件里读参数有两种方式：

```python
mode = lit_config.params.get("build_mode", ".")      # 直接读
path = lit_config.substitute("%(build_mode)s")       # 插值；缺参数会 fatal
```

`substitute()` 的实现就是 `string % self.params`（`LitConfig.py:246-254`）。真实缺参数输出：

```
lit.py: .../LitConfig.py:252: fatal: unable to find 'missing' parameter, use '--param=missing=VALUE'
```

实验：`-Dfoo=bar` 后，`lit_config.params` 是 `{'foo': 'bar'}`，`lit_config.substitute("foo=%(foo)s")` 返回 `'foo=bar'`。但把 `%(foo)s` 写进 `RUN:` 行不会展开：

```
# RUN: at line 3
echo "%(foo)s"
# executed command: echo '%(foo)s'
# | %(foo)s
```

`params` 是给配置文件用的，不是给测试脚本用的。测试脚本要用替换，得在配置里 `config.substitutions.append(...)`。

## 10.7 实验：环境白名单与 `--pass-env`

测试里写 `printenv FOO` 与 `printenv LLVM_DISABLE_CRASH_REPORT`，宿主设置 `FOO=secret`。不传 `--pass-env` 时：

```
-- Testing: 1 tests, 1 workers --
FAIL: Ch10 :: tests/env.test (1 of 1)
...
# executed command: printenv FOO
# note: command had no output on stdout or stderr
# error: command failed with exit status: 1
```

加 `--pass-env=FOO` 后：

```
-- Testing: 1 tests, 1 workers --
PASS: Ch10 :: tests/env.test (1 of 1)
...
# | secret
...
# | 1
```

两个结论：环境变量不是全量透传，要透传用 `--pass-env=NAME` 或在 `lit.cfg.py` 里显式写 `config.environment["FOO"] = ...`；`LLVM_DISABLE_CRASH_REPORT=1` 确实自动存在，不需要你设置。

## 10.8 一张生命周期总表

| 时刻 | 发生什么 | 关键源码 |
|---|---|---|
| 进程启动 | `main()` 解析 CLI，构造唯一 `LitConfig` | `main.py:25-49` |
| 发现 suite | `fromdefaults()` 造空壳，白名单环境，`suffixes=set()`、`pipefail=True` | `TestingConfig.py:10-133` |
| 加载 suite 配置 | `exec` 文件，注入 `config`/`lit_config`/`__file__`，然后 `finish()` 归一化 | `TestingConfig.py:135-172`、`235-268` |
| 进入子目录 | 无 local cfg 复用父对象；有则 `deepcopy` + `exec` + `finish` | `discovery.py:98-128` |
| 创建测试 | `Test(testSuite, path_in_suite, localConfig)`，测试持有目录级 `config` | `Test.py:245-252` |
| 执行测试 | worker 里 `test.config.test_format.execute(test, lit_config)` | `worker.py:90` |

记住最后一行的分离：**测试执行时同时需要目录级 `config` 和全局 `lit_config`**。前者告诉它怎么解析/替换，后者提供超时、重试、`noExecute` 等进程级开关。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`config` 是全局配置」 | `config` 是目录级 `TestingConfig`；全局的是 `lit_config`。每个 local cfg 目录拿到的是 `deepcopy` 出来的新对象。 |
| 「`lit.local.cfg` 是 include 父配置」 | 不是 include，是 `copy.deepcopy(parent)` 后 `exec` 局部文件；局部文件可以读写父配置的全部字段。 |
| 「`finish()` 在每次访问字段时调用」 | `finish()` 只在 `load_from_path()` 末尾调用一次，负责类型归一化和 CLI 覆盖；之后字段就是普通属性。 |
| 「`config.parent` 能拿到父目录配置」 | 当前 commit 的 `discovery.py` 从不设置非空 `parent`；实测 local cfg 里 `config.parent is None`，`config.root is config`。`config.root.targets` 能用是因为 `deepcopy` 复制了字段，不是因为父链。 |
| 「`--param` 会写进 `config`」 | 参数只在 `lit_config.params`；配置里用 `lit_config.params["x"]` 或 `lit_config.substitute("%(x)s")` 读。`RUN:` 行不会自动展开 `%(x)s`。 |
| 「环境变量会全量传给测试」 | 只有 46 个白名单变量 + `--pass-env` + 配置里显式写入的变量。宿主 `FOO=secret` 默认不可见。 |
| 「`LLVM_DISABLE_CRASH_REPORT` 是宿主决定的」 | `fromdefaults()` 无条件写成 `"1"`，与宿主无关。 |
| 「`config_map` 是普通 `-D` 参数」 | 它是嵌入 lit 时通过 `main(builtin_params)` 传的 dict；命令行 `-Dconfig_map=x` 会得到字符串并在 `discovery.py:60` 抛 `AttributeError`。 |
| 「`lit` 是配置文件的全局名」 | 当前 `load_from_path()` 只注入 `config`、`lit_config`、`__file__`；裸写 `lit.formats` 会 `NameError`，必须自己 `import lit.formats`。 |

## 重点整理

- `LitConfig` 全局一份，管 CLI、参数、配置文件名、诊断级别；`TestingConfig` 每目录一份，管测试行为。
- 三步链：`fromdefaults()` 造默认对象 → `load_from_path()` `exec` 配置并注入 `config`/`lit_config` → `finish()` 归一化并应用 CLI 覆盖。
- `fromdefaults()` 的三个硬默认：`suffixes=set()`、`pipefail=True`、环境只有白名单 + `LLVM_DISABLE_CRASH_REPORT=1`。
- `finish()` 只在加载末尾跑一次；`--max-retries-per-test` 只在配置没写 `test_retry_attempts` 时生效；`--timeout` 总是覆盖 suite 值；suite 值非零且与命令行不同才打 note。
- local cfg 是 `deepcopy` 后的新对象；`parent` 在当前发现路径上始终为 `None`，`root` 退化为自身，`config.root.targets` 靠字段复制工作。
- LLVM 的 `targets`/`available_features`/`substitutions`/`environment` 都有明确来源：site cfg、`llvm.initialize()`、`lit.cfg.py` 的追加。
- `params` 属于 `lit_config`，不是 `config`；`config_map` 是嵌入 API 的内置参数，不是 CLI 参数。

## 自我检核

不看笔记，试着回答：

- [ ] 一个 suite 根目录、一个没有 local cfg 的子目录、一个有 local cfg 的子目录，三者的 `id(config)` 分别是什么关系？为什么？
- [ ] `finish()` 在什么时候被调用？它做的四类事各是什么？哪一类会打 note？
- [ ] 为什么 `config.root.targets` 在 local cfg 里能用，但 `config.parent.name` 会崩？这两个现象背后的机制有什么不同？
- [ ] 宿主设置 `FOO=secret`，测试里 `printenv FOO` 为什么可能失败？两种修复方式分别是什么？
- [ ] `-Dfoo=bar` 之后，`config` 和 `lit_config` 各能看到什么？`RUN:` 里的 `%(foo)s` 为什么原样输出？
- [ ] 如果 `lit.cfg.py` 里写 `sys.exit()` 和 `sys.exit("bad")`，lit 的行为分别是什么？源码在哪一行区分？

## 延伸阅读

- **[`llvm/utils/lit/lit/LitConfig.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/LitConfig.py)**
  - 读什么：`__init__` 到 `load_config()`（约 24–172 行），再看 `substitute()` 与 `_write_message()`。
  - 能学到：全局配置到底存了什么、配置文件名的三组列表怎么拼、诊断消息怎么带文件行号。
  - 前提：读完本章 10.2；知道 Python 的 `dict`/`list` 基本操作。
- **[`llvm/utils/lit/lit/TestingConfig.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestingConfig.py)**
  - 读什么：`fromdefaults()`、`load_from_path()`、`finish()`、`root` 四个块，全文 303 行。
  - 能学到：默认值、环境白名单、`exec` 注入、类型归一化、CLI 覆盖的真实顺序。
  - 前提：读完本章 10.3–10.4；知道 `exec` 和 `copy.deepcopy` 的差别。
- **[`llvm/utils/lit/lit/discovery.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/discovery.py)**
  - 读什么：`getTestSuite()` 与 `getLocalConfig()`（约 28–128 行）。
  - 能学到：suite 配置和 local 配置的创建/复制时机；为什么 `parent` 在当前路径上是 `None`。
  - 前提：读过 Ch 3 的配置级联，或本章 10.4。
- **[`llvm/test/lit.site.cfg.py.in`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.site.cfg.py.in)**
  - 读什么：前 31 行与最后 6 行，重点看 `@VAR@` 和 `lit_config.load_config()`。
  - 能学到：本章四个字段在真实 LLVM suite 里的来源；为 Ch 12 的 CMake 生成做铺垫。
  - 前提：知道 `configure_file` 的 `@ONLY` 替换大概是什么。

> 下一章把这些对象落到一份可复制的 `lit.cfg.py`：从 8 行最小配置开始，逐个字段加上去，并且故意写错几次，看 lit 怎么报错。

→ [Ch 11 从零写 lit.cfg](./11-lit-cfg-from-scratch.md)
