# Ch 3 — 测试发现与配置级联

> **目标**：说清 `lit.cfg`、`lit.site.cfg`、`lit.local.cfg` 三者的分工与加载顺序；能用 `--config-prefix`、嵌套 suite、`lit.local.cfg` 精确控制「哪些文件被哪些配置管」。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`。本章实验在 `/tmp` 下自建 suite 实跑；LLVM 真实布局以 `llvm/test/lit.cfg.py` 与 `llvm/test/lit.site.cfg.py.in` 为准。

---

## 3.1 三种配置文件，三种作用域

lit 的配置文件名不是随便定的。`LitConfig` 在构造时写死了三组名字（`llvm/utils/lit/lit/LitConfig.py`）：

```python
self.suffixes = ["cfg.py", "cfg"]
self.config_names       = ["%s.%s" % (prefix, x) for x in suffixes]
self.site_config_names  = ["%s.site.%s" % (prefix, x) for x in suffixes]
self.local_config_names = ["%s.local.%s" % (prefix, x) for x in suffixes]
```

默认 `prefix = "lit"`，于是：

| 文件 | 作用域 | 谁写 | 典型内容 |
|---|---|---|---|
| `lit.cfg.py` / `lit.cfg` | **一个 suite** | 项目作者（源码树） | suite 名、test_format、suffixes、substitutions、features |
| `lit.site.cfg.py` / `lit.site.cfg` | **一个 suite 的构建实例** | 构建系统生成（构建树） | 绝对路径、工具目录、编译选项、从 `*.in` 模板生成 |
| `lit.local.cfg.py` / `lit.local.cfg` | **一个目录及其子目录** | 项目作者（源码树） | 某目录专属 suffixes、excludes、unsupported、额外 substitutions |

先记住一句话：

> `lit.cfg` 定义「这个 suite 是什么」；`lit.site.cfg` 定义「这次构建的这个 suite 在哪」；`lit.local.cfg` 定义「这个子目录有什么例外」。

## 3.2 发现顺序：site 优先于 cfg

`discovery.py` 的 `dirContainsTestSuite()` 先找 site config，再找普通 config：

```python
def dirContainsTestSuite(path, lit_config):
    cfgpath = chooseConfigFileFromDir(path, lit_config.site_config_names)
    if not cfgpath:
        cfgpath = chooseConfigFileFromDir(path, lit_config.config_names)
    return cfgpath
```

为什么 site 优先？因为 LLVM 是 out-of-tree 构建：

- 源码树里有 `llvm/test/lit.cfg.py`。
- 构建树里有 CMake 生成的 `build/test/lit.site.cfg.py`。
- 你通常从**构建树**运行 `llvm-lit build/test`，所以 lit 先看到 site cfg。site cfg 再 `load_config()` 回源码树的 `lit.cfg.py`，把构建参数注入进去。

`llvm/test/lit.site.cfg.py.in` 的最后两行就是这个回加载：

```python
import lit.llvm
lit.llvm.initialize(lit_config, config)

# Let the main config do the real work.
lit_config.load_config(
    config, os.path.join(config.llvm_src_root, "test/lit.cfg.py"))
```

所以真实加载顺序是：

```
build/test/lit.site.cfg.py       ← 先加载（生成物，含绝对路径）
        ↓ lit_config.load_config()
llvm/test/lit.cfg.py             ← 再加载（源码，定义 suite 行为）
        ↓
各目录的 lit.local.cfg.py        ← 最后按目录加载（局部覆盖）
```

> **常见误解**：「site cfg 是给 CI 用的额外配置」。不是。它是构建树里的**主入口**；源码树的 `lit.cfg.py` 反而是被它加载的。

## 3.3 suite 与 local config：两种「向下」

发现阶段有两套向下机制，别混：

### 机制一：嵌套 suite（新的 `lit.cfg`）

递归到子目录时，如果子目录里有 `lit.cfg.py` 或 `lit.site.cfg.py`，lit 会**新建一个 TestSuite**：

```python
subpath = path_in_suite + (filename,)
if dirContainsTestSuite(file_execpath, litConfig):
    sub_ts, subpath_in_suite = getTestSuite(file_execpath, litConfig, testSuiteCache)
elif dirContainsTestSuite(file_sourcepath, litConfig):
    sub_ts, subpath_in_suite = getTestSuite(file_sourcepath, litConfig, testSuiteCache)
```

注意顺序：先看 **exec path**，再看 **source path**。这是为了 out-of-tree 构建：构建树里的 site cfg 优先于源码树里的 cfg。

嵌套 suite 是**硬边界**：父 suite 的 `suffixes`、`substitutions`、`features` 不再自动适用于子 suite。子 suite 有自己的 `config.name`、`test_source_root`、`test_exec_root`。

真实例子：`llvm/test` 下只有两个 `lit.cfg`：

```bash
find llvm/test -name 'lit.cfg*'
# llvm/test/Unit/lit.cfg.py
# llvm/test/lit.cfg.py
```

`llvm/test/Unit` 是嵌套 suite，因为它跑的是 GoogleTest 单元测试，格式和 `llvm/test` 的 ShTest 不同。Clang 有 3 个，也是同样原因。

### 机制二：local config（同一 suite 内的目录覆盖）

如果子目录里没有 `lit.cfg`，但有 `lit.local.cfg.py`，lit 会：

```python
config = copy.deepcopy(parent)
config.load_from_path(cfgpath, litConfig)
return config
```

也就是「复制父配置 + 执行局部配置」。它仍然是**同一个 suite**，只是这个目录的 `TestingConfig` 多了/改了一些字段。子目录继续继承这个副本（除非再遇到更深的 local cfg）。

关键点：

- local cfg 是 **copy-on-write**，不是 `include`。你在里面写 `config.suffixes = [...]` 是覆盖，写 `config.suffixes.add(...)` 是追加（因为父配置的 `suffixes` 已经是 `set`）。
- local cfg 里可以访问 `config.root`，即 suite 根配置。LLVM 的 target 检查都这么写：`if not "X86" in config.root.targets: config.unsupported = True`。
- local cfg 的改动只影响该目录及其子目录，不影响兄弟目录。

## 3.4 实验：嵌套 suite 的真实边界

建一个最小目录：

```
cfg/
├── lit.cfg.py              # RootSuite, suffixes=[".test"]
└── tests/
    ├── a.test
    ├── parent.test
    ├── sub/
    │   ├── lit.cfg.py      # SubSuite, suffixes=[".sub"]
    │   ├── b.sub
    │   └── c.test
    └── local/
        ├── lit.local.cfg.py
        └── d.test
```

`tests/sub/lit.cfg.py`：

```python
import os
import lit.formats
config.name = "SubSuite"
config.test_format = lit.formats.ShTest()
config.suffixes = [".sub"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
```

运行 `--show-suites`，真实输出：

```
-- Test Suites --
  RootSuite - 3 tests
    Source Root: /tmp/cfgcascade.oO9LeT
    Exec Root  : /tmp/cfgcascade.oO9LeT/Output
    Available Features: 
    Available Substitutions: 
  SubSuite - 1 tests
    Source Root: /tmp/cfgcascade.oO9LeT/tests/sub
    Exec Root  : /tmp/cfgcascade.oO9LeT/tests/sub/Output
    Available Features: 
    Available Substitutions: 
```

`--show-tests`：

```
-- Available Tests --
  RootSuite :: tests/a.test
  RootSuite :: tests/local/d.test
  RootSuite :: tests/parent.test
  SubSuite :: b.sub
```

三个观察：

1. **`tests/sub/c.test` 没有出现在任何 suite 里**。SubSuite 的 `suffixes` 只有 `.sub`，所以 `.test` 不被发现；父 suite 也不会「收回」这个文件，因为 `tests/sub` 已经是独立 suite。
2. **SubSuite 的测试名是 `SubSuite :: b.sub`**，不是 `SubSuite :: tests/sub/b.sub`。因为 `test_source_root` 指向 `tests/sub`，`path_in_suite` 从那里算起。
3. RootSuite 仍然发现 `tests/local/d.test`，因为 `tests/local` 没有 `lit.cfg`，只有 local cfg，属于同一个 suite。

> **设计判断**：嵌套 suite 适合「测试格式/运行环境根本不同」的场景（GoogleTest vs ShTest、不同 target 的独立套件）。只是想改 suffix 或加一个 feature，用 `lit.local.cfg.py`，不要新建 suite。

## 3.5 `--config-prefix`：换一套配置名

`--config-prefix NAME` 把前缀从 `lit` 换成 `NAME`，于是 lit 找：

- `NAME.cfg.py` / `NAME.cfg`
- `NAME.site.cfg.py` / `NAME.site.cfg`
- `NAME.local.cfg.py` / `NAME.local.cfg`

实验：复制一份 `lit.cfg.py` 为 `mylit.cfg.py`，把 `config.name` 改成 `PrefixSuite`：

```bash
lit.py --config-prefix mylit --show-suites tests
```

真实输出：

```
-- Test Suites --
  PrefixSuite - 4 tests
    Source Root: /tmp/cfgcascade.oO9LeT
    Exec Root  : /tmp/cfgcascade.oO9LeT/Output
```

对比默认前缀：

```
-- Test Suites --
  RootSuite - 3 tests
```

为什么变成 4 个测试？因为 `--config-prefix mylit` 之后，`tests/sub/lit.cfg.py` **不再被识别**（它叫 `lit.cfg.py`，不是 `mylit.cfg.py`），于是 `tests/sub` 退回 RootSuite 管辖；`tests/sub/c.test` 匹配 RootSuite 的 `.test` 后缀，被发现了。

这个实验说明两件事：

- `--config-prefix` 是**全局**的，会同时影响 suite、site、local 三种配置。
- 嵌套 suite 的身份完全取决于「那个目录里有没有当前前缀的 cfg 文件」。

真实用途：一个项目里同时存在多套测试配置（比如 `lit.cfg.py` 和 `asan.cfg.py`），用不同前缀跑不同变体。LLVM 自己很少用，但自建项目很实用。

## 3.6 LLVM 的真实布局

把 LLVM 的配置链路完整画出来：

```
llvm/test/lit.site.cfg.py.in          ← 源码模板（CMake configure 输入）
        │  configure_lit_site_cfg()
        ▼
build/test/lit.site.cfg.py            ← 构建树生成物
        │  lit_config.load_config()
        ▼
llvm/test/lit.cfg.py                  ← 源码 suite 配置
        │
        ├─ config.name = "LLVM"
        ├─ config.test_format = lit.formats.ShTest()
        ├─ config.suffixes = [".ll", ".c", ".test", ".txt", ".s", ".mir", ".yaml", ".spv"]
        ├─ config.excludes = ["Inputs", "CMakeLists.txt", "README.txt", "LICENSE.txt"]
        ├─ llvm_config.with_environment("PATH", config.llvm_tools_dir, append_path=True)
        └─ ...
        │
        ▼
llvm/test/CodeGen/LoongArch/lit.local.cfg
llvm/test/CodeGen/NVPTX/lit.local.cfg
llvm/test/Instrumentation/.../lit.local.cfg
```

几个真实细节：

- `llvm/test/lit.cfg.py` 的 `config.suffixes` 有 8 个后缀，包含 `.ll`、`.mir`、`.s`、`.yaml`、`.spv`。这就是为什么 LLVM 能在一个 suite 里同时跑 IR、MIR、汇编、YAML、SPIR-V 测试。
- `config.excludes` 排除了 `Inputs` 目录和几个元文件。`Inputs/` 是测试的辅助输入，不是测试本身。
- `config.test_source_root = os.path.dirname(__file__)`，所以 suite 根是 `llvm/test`。
- `config.test_exec_root = os.path.join(config.llvm_obj_root, "test")`，所以临时文件写到构建树的 `test/Output`。
- `llvm_config.with_environment("PATH", config.llvm_tools_dir, append_path=True)` 把构建出的 `opt`/`llc`/`FileCheck` 放到 PATH 最前。

真实 local cfg 三个例子（都在 `llvm/test` 下）：

**LoongArch**（`llvm/test/CodeGen/LoongArch/lit.local.cfg`）：

```python
import os

config.suffixes = [".ll", ".mir", ".test", ".txt"]

extract_section_path = os.path.join(config.llvm_src_root, "utils", "extract-section.py")
config.substitutions.append((
    "extract-section",
    "'%s' %s %s" % (config.python_executable, extract_section_path, "--bits-endian little"),
))

if not "LoongArch" in config.root.targets:
    config.unsupported = True
```

三件事一起做：覆盖 suffixes、加一个 substitution、按 target 把整个目录标 unsupported。

**NVPTX**：

```python
if not "NVPTX" in config.root.targets:
    config.unsupported = True
config.suffixes.add(".py")
config.excludes = ["fence.py", "cmpxchg.py", "atomicrmw.py", "load.py", "store.py"]
```

`add(".py")` 是追加（父配置 `suffixes` 是 set）；`excludes = [...]` 是覆盖，只排除列出的文件名。

**SystemZ/Large**：

```python
config.suffixes = [".ll", ".py"]

if "long_tests" not in config.available_features:
    config.unsupported = True

if not "SystemZ" in config.root.targets:
    config.unsupported = True
```

用 feature 而不是「默认关掉」，这样 `--param` 或配置打开 `long_tests` 时整个目录自动启用。

## 3.7 `suffixes`、`excludes`、`standalone_tests` 的细节

### `suffixes` 是字符串后缀

```python
if any(filename.endswith(suffix) for suffix in localConfig.suffixes):
```

不是 `os.path.splitext`。所以 `suffixes = [".test"]` 会匹配：

- `foo.test` ✅
- `foo.notatest` ✅（以 `.test` 结尾）
- `foo.test.bak` ❌

这不是 bug，是设计选择（简单、可预测）。如果你要严格扩展名，自己加判断或换 format。

### `excludes` 是文件名集合

```python
if filename.startswith(".") or filename in localConfig.excludes:
    return
```

- `excludes = ["Inputs"]` 排除的是**名为 `Inputs` 的文件**；目录 `Inputs` 是在递归时被 `if filename in lc.excludes: continue` 跳过的。
- 不支持 glob。`excludes = ["*.py"]` 不会按通配符工作（`*.py` 不是文件名）。
- 点文件永远不发现，无论 `excludes` 写什么。

### `standalone_tests`

```python
if lc.standalone_tests:
    if lc.suffixes or lc.excludes:
        litConfig.warning("standalone_tests set ... but suffixes or excludes are also set")
    return
```

当目录里的测试需要「由外部脚本自己跑」而不是由 lit 逐个发现时，设 `config.standalone_tests = True`。此时 lit 不发现该目录下的文件，但你仍然可以直接把单个测试路径传给 lit。LLVM 主测试套件基本不用，自建 suite 的「集成测试目录」可能会用。

## 3.8 诊断配置级联的三板斧

遇到「为什么这个测试没跑/跑错配置」，按顺序：

```bash
# 1) 看 suite 被识别成什么
lit.py --show-suites tests

# 2) 看测试属于哪个 suite
lit.py --show-tests tests

# 3) 看配置加载与输入解析
lit.py --debug tests
```

`--debug` 会打印：

```
debug: loading suite config '/path/to/lit.cfg.py'
debug: ... loaded config '/path/to/lit.cfg.py'
debug: loading local config '/path/to/lit.local.cfg.py'
debug: resolved input 'tests/foo.test' to 'SuiteName'::('tests', 'foo.test')
```

如果 `--show-suites` 里的 `Source Root` 不是你预期的目录，先检查 `config.test_source_root`；如果测试名里的路径少了前缀，通常是嵌套 suite 的 `test_source_root` 在起作用。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`lit.cfg` 和 `lit.site.cfg` 是二选一」 | site cfg 优先被找到，但它通常会 `load_config()` 回源码树的 `lit.cfg`；两者是**串联**关系。 |
| 「子目录有 `lit.local.cfg` 就是新 suite」 | 新 suite 只由 `lit.cfg`/`lit.site.cfg` 触发；`lit.local.cfg` 只是同 suite 内的配置副本。 |
| 「嵌套 suite 会继承父 suite 的 suffixes」 | 不会。嵌套 suite 是硬边界，子 suite 的 `suffixes` 完全由它自己的 `lit.cfg` 决定；父 suite 也不会收回不匹配的文件。 |
| 「`excludes` 支持 `*.py`」 | 它只做 `filename in excludes`，不是 glob。 |
| 「`suffixes=[".test"]` 只匹配 `.test` 扩展名」 | 是 `endswith`，`foo.notatest` 也会匹配。 |
| 「`--config-prefix` 只影响根配置」 | 它同时改 suite/site/local 三组名字；嵌套 suite 可能因此消失或出现。 |

## 重点整理

- 三种配置：`lit.cfg`（suite 定义）、`lit.site.cfg`（构建实例）、`lit.local.cfg`（目录覆盖）。
- 发现顺序：site 优先于 cfg；local cfg 最后按目录 deepcopy 加载。
- 嵌套 suite 由子目录的 `lit.cfg`/`lit.site.cfg` 触发，是硬边界；否则继续用 local cfg。
- `--config-prefix` 全局改前缀，会同时影响三种配置文件的识别。
- LLVM 的真实链路：`lit.site.cfg.py.in` → 构建树 `lit.site.cfg.py` → `load_config()` → `llvm/test/lit.cfg.py` → 各目录 `lit.local.cfg`。
- `suffixes` 是 `endswith`，`excludes` 是文件名集合，点文件永远不发现。
- 诊断顺序：`--show-suites` → `--show-tests` → `--debug`。

## 自我检核

- [ ] 为什么 LLVM 要从构建树的 `lit.site.cfg.py` 启动，而不是直接跑源码树的 `lit.cfg.py`？site cfg 多提供了什么？
- [ ] 一个子目录里同时有 `lit.cfg.py` 和 `lit.local.cfg.py`，会发生什么？哪个生效？
- [ ] 嵌套 suite 的 `test_source_root` 会怎样影响测试名？用本章的 `SubSuite :: b.sub` 解释。
- [ ] `--config-prefix asan` 之后，原来的 `tests/sub/lit.cfg.py` 还会被识别吗？为什么？
- [ ] 如果要让某个目录只在 `long_tests` feature 打开时运行，应该写 `lit.local.cfg` 还是新建 suite？为什么？
- [ ] `excludes = ["Inputs"]` 和「排除 `Inputs` 目录」是同一件事吗？递归代码里分别在哪一行生效？

## 延伸阅读

- **[lit Command Guide — TEST SUITES / TEST DISCOVERY / LOCAL CONFIGURATION FILES](https://llvm.org/docs/CommandGuide/lit.html#test-suites)**
  - 读什么：这三节连起来读，约 60 行。
  - 能学到：官方对 suite、嵌套、local config 的抽象；对照本章 3.2–3.3。
  - 前提：读完 Ch 2 的发现流程。
- **[`llvm/test/lit.cfg.py`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.cfg.py)**
  - 读什么：前 80 行（name/format/suffixes/excludes/source root/tools dir）。
  - 能学到：一个成熟 suite 的最小骨架；`llvm_config.with_environment("PATH", ...)` 为什么要放在这里。
  - 前提：知道 `os.path.dirname(__file__)` 的含义。
- **[`llvm/test/lit.site.cfg.py.in`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.site.cfg.py.in)**
  - 读什么：全文（约 80 行）。
  - 能学到：构建参数如何注入（`@LLVM_TOOLS_DIR@` 等）；最后两行为什么要 `load_config()` 回源码树。
  - 前提：读过本章 3.2。
- **[`llvm/test/CodeGen/NVPTX/lit.local.cfg`](https://github.com/llvm/llvm-project/blob/main/llvm/test/CodeGen/NVPTX/lit.local.cfg)**
  - 读什么：整个文件（4 行）。
  - 能学到：`suffixes.add`、`excludes` 覆盖、target 检查三个模式在同一文件里的组合。
  - 前提：知道 `config.root` 是 suite 根配置。

> 下一章开始写真正的测试：一条 `RUN:` 行里的 `%s`、`%t` 到底 expand 成什么，命令失败时 lit 打印的每一行从哪来。

→ [Ch 4 第一条 RUN: 行](./04-first-run-line.md)
