# Ch 14 — 工具替换：lit.llvm、ToolSubst、FindTool

> **目标**：能解释一个 `RUN: opt ...` 里的 `opt` 是怎样变成 `/build/bin/opt` 的；能自己写 `ToolSubst`、控制找不到工具时的行为，并说清 PATH 与 substitution 各自解决什么问题。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。实验用 `/home/ubuntu/llvm/builds/pr-lane/bin` 里的 `opt`、`clang`、`ld.lld` 等真实二进制；本章所有命令输出均在本机实跑。

---

## 14.1 三条路径：一个工具名怎样变成可执行命令

测试里写 `opt --version`，最后能跑起来，可能经过三条完全不同的路：

```
RUN: opt --version
        │
        ├─(1) substitution：配置里的 (regex, replacement)
        │       opt  ->  /home/.../build/bin/opt
        │       文本在进 shell 之前就被改写
        │
        ├─(2) PATH：环境变量里包含 /home/.../build/bin
        │       文本不变，shell 在运行时按 PATH 查找
        │
        └─(3) 直接写绝对路径：/home/.../build/bin/opt
                最笨、最确定，但不可移植
```

lit 的 substitution 阶段发生在 `applySubstitutions()`（`lit/TestRunner.py:1651`），**早于** shell 解析。PATH 是 shell 运行时的事。两者都能让 `opt` 跑起来，但失败时的表现完全不同：

- substitution 命中 → `# executed command:` 里是绝对路径；
- 只有 PATH → `# executed command:` 里仍是 `opt`；
- 两者都没有 → `'opt': command not found`，退出码 127。

LLVM 主 suite 两者都做：`llvm/test/lit.cfg.py:99` 把 `llvm_tools_dir` 加进 PATH，`:492` 又调用 `add_tool_substitutions(tools, config.llvm_tools_dir)`。原因在 14.12 展开。

## 14.2 `lit.llvm.initialize()` 与 `LLVMConfig`

`lit/llvm/__init__.py` 只有 9 行，核心是一个模块级全局：

```python
# lit/llvm/__init__.py:3
llvm_config = None

# lit/llvm/__init__.py:6
def initialize(lit_config, test_config):
    global llvm_config
    llvm_config = config.LLVMConfig(lit_config, test_config)
```

所以 `lit.llvm.llvm_config` 是**进程级单例**，由 `initialize()` 创建。配置里必须按这个顺序写：

```python
import lit.llvm

lit.llvm.initialize(lit_config, config)
lit.llvm.llvm_config.add_tool_substitutions([...])
```

如果跳过 `initialize()`，`llvm_config` 仍是 `None`，下一行就是 `AttributeError: 'NoneType' object has no attribute ...`。LLVM 自己的 `lit/utils/lit/tests/lit.cfg` 用 `if llvm_config:` 做保护，因为它要支持没有构建目录的 standalone 模式。

`LLVMConfig`（`lit/llvm/config.py:27`）不是「工具替换器」，它是一层 **LLVM 构建环境适配**，职责至少包括：

| 职责 | 入口 |
|---|---|
| 注入平台/triple/sanitizer/symlinks 等 feature | `__init__`，`config.py:27-210` |
| 管理测试环境变量（PATH、LD_LIBRARY_PATH、sanitizer 选项） | `with_environment` / `with_system_environment` |
| 查询 `llvm-config` 的能力 | `feature_config`，`config.py:311` |
| 单工具查找 | `use_llvm_tool`，`config.py:528` |
| 批量工具替换 | `add_tool_substitutions`，`config.py:441` |
| 默认替换（FileCheck、`\| count`、`%python`） | `use_default_substitutions`，`config.py:503` |
| clang / lld 全家桶 | `use_clang` / `use_lld`，`config.py:726` / `834` |

> 如果你还没建立「配置对象 vs 全局配置」的分层，先回看 [Ch 10 — 配置对象](./10-config-objects.md) 的 `LitConfig` / `TestingConfig` 对照表。

## 14.3 `config.llvm_tools_dir` 不是一个可选装饰

`add_tool_substitutions()` 的第一件事就是决定搜索目录（`config.py:441-446`）：

```python
def add_tool_substitutions(self, tools, search_dirs=None):
    if not search_dirs:
        search_dirs = [self.config.llvm_tools_dir]
```

注意：是 `self.config.llvm_tools_dir`，不是 `getattr(..., None)`。没设这个属性时，Python 直接在配置加载阶段抛 `AttributeError`。实测：

```text
lit.py: .../TestingConfig.py:168: fatal: unable to parse config file '/tmp/lit-ch14-nodir-xxxx/lit.cfg.py', traceback: Traceback (most recent call last):
  File ".../TestingConfig.py", line 157, in load_from_path
    exec(compile(data, path, "exec"), cfg_globals, None)
  File "/tmp/lit-ch14-nodir-xxxx/lit.cfg.py", line 13, in <module>
    lit.llvm.llvm_config.add_tool_substitutions([ToolSubst("opt")])
  File ".../lit/llvm/config.py", line 443, in add_tool_substitutions
    search_dirs = [self.config.llvm_tools_dir]
AttributeError: 'TestingConfig' object has no attribute 'llvm_tools_dir'
```

三个结论：

1. **属性必须存在**。`config.llvm_tools_dir = ""` 也算存在，但空目录找不到工具，随后进入 `unresolved` 分支。
2. **路径最好真实存在**。属性存在但目录不存在时，`FindTool.resolve()` 的 `lit.util.which()` 返回 `None`，行为由 `unresolved` 决定。
3. **可以绕过**：给 `add_tool_substitutions(tools, search_dirs=[...])` 传非空列表，就不会读 `llvm_tools_dir`。自建 suite 没有 LLVM 构建目录时，这是正确写法。

## 14.4 `ToolSubst` 的参数表

`ToolSubst.__init__`（`lit/llvm/subst.py:38`）的默认值：

```python
ToolSubst(key, command=None, pre=r".-^/\<", post="-.",
          verbatim=False, unresolved="warn",
          extra_args=None, launcher=None)
```

| 参数 | 作用 | 默认/注意 |
|---|---|---|
| `key` | 要匹配的文本 | 通常是工具名，如 `"opt"` |
| `command` | 替换成什么 | `None` → `FindTool(key)`；字符串 → 原样使用；`FindTool` → 去搜索 |
| `pre` | 匹配点前一个字符若在此集合中，则拒绝 | `r".-^/\<"`，**不含 `%`** |
| `post` | 匹配点后一个字符若在此集合中，则拒绝 | `"-."`，所以 `opt.foo` 不会命中 |
| `verbatim` | `key` 直接当正则 | 仍要能被 `ToolSubst.resolve()` 解析出工具名 |
| `unresolved` | 找不到工具时怎么办 | `warn` / `fatal` / `break` / `ignore` |
| `extra_args` | 追加参数列表 | 拼在命令后面 |
| `launcher` | 命令前缀 | 拼在命令前面 |

非 `verbatim` 时，正则这样拼（`subst.py:103`）：

```python
self.regex = not_in(pre, "<") + wordify(key) + not_in(post)
```

`opt` 实际生成的正则（`--show-suites` 原样输出）：

```text
(?<!(\.|\-|\^|/|\\|<))\bopt\b(?!(\-|\.))
```

逐段读：

- `(?<![.\-^/\<])`：前一个字符不能是 `. - ^ / \ <`；
- `\bopt\b`：整词匹配；
- `(?![.\-])`：后一个字符不能是 `.` 或 `-`。

**`%` 不在 `pre` 里**。这就是下一节所有麻烦的根源。

## 14.5 真实实验：`opt`、`%opt` 与「二次替换」

配置（`/tmp/lit-ch14-basic-xxxx/lit.cfg.py`）：

```python
config.llvm_tools_dir = "/home/ubuntu/llvm/builds/pr-lane/bin"
lit.llvm.initialize(lit_config, config)
lit.llvm.llvm_config.add_tool_substitutions([ToolSubst("opt", unresolved="fatal")])
```

**裸 `opt`**（`# RUN: opt --version`）真实输出：

```text
PASS: SubstLab :: tests/bare-opt.test
/home/ubuntu/llvm/builds/pr-lane/bin/opt --version
# executed command: /home/ubuntu/llvm/builds/pr-lane/bin/opt --version
# | LLVM version 24.0.0git
```

文本在进 shell 前就被换成了绝对路径。

显式给 `command` 则完全跳过搜索：

```python
ToolSubst("opt", command="/bin/echo", unresolved="fatal")
```

`# RUN: opt hello` 的真实执行行是 `/bin/echo hello`，输出 `hello`；`unresolved` 根本没有机会触发。

**同一配置下写 `%opt`**：

```text
FAIL: SubstLab :: tests/percent-opt.test
Exit Code: 127
# RUN: at line 1
%/home/ubuntu/llvm/builds/pr-lane/bin/opt --version
# | '%/home/ubuntu/llvm/builds/pr-lane/bin/opt': command not found
```

机制只有一步：

```text
%opt ──(\bopt\b 命中 "opt")──> % + /abs/bin/opt = %/abs/bin/opt（非法命令）
```

这不是 lit 做了两轮替换，而是**一次替换只吃掉了 `opt`，把 `%` 留在原地**。`%/...` 恰好长得像 `%/t`、`%/s` 这类替换，但它不在表里（`%/h` 不存在），所以原样进 shell。

**把 key 写成 `"%opt"`**：

```python
ToolSubst("%opt", command=os.path.join(config.llvm_tools_dir, "opt"),
          unresolved="fatal")
```

`wordify("%opt")` 得到 `%\bopt\b`，`%opt` 变成正确的绝对路径；同一测试里的裸 `opt` 反而 `command not found`。结论：`ToolSubst("opt")` 与 `ToolSubst("%opt")` 是两个不同替换，**同一工具只选一种拼写**。LLVM 的约定是裸命令走 `add_tool_substitutions`，`%clang`/`%python` 这类带百分号的用专门替换。

## 14.6 `pre` / `post` / `verbatim` / `extra_args` / `launcher`

```python
lit.llvm.llvm_config.add_tool_substitutions([
    ToolSubst("opt"),
    ToolSubst("myecho", command="/bin/echo",
              extra_args=["EXTRA"], launcher="/usr/bin/env"),
    ToolSubst(r"\bWC\b", command="/usr/bin/wc", verbatim=True),
])
```

```text
# RUN: echo opt.foo      # -> echo opt.foo（post 挡住 "."）
# RUN: echo opt          # -> echo /home/.../bin/opt
# RUN: myecho hello      # -> /usr/bin/env /bin/echo EXTRA hello -> EXTRA hello
# RUN: echo hi | WC -l   # -> echo hi | /usr/bin/wc -l -> 1
```

- `post="-."` 阻止匹配 `opt.foo`；`pre`/`post` 防的是文件名、后缀、`-flavor` 这类误伤。
- `launcher` 在最前，`extra_args` 在最后：`launcher + " " + command + " " + extra_args`。
- `verbatim=True` 时 `key` 直接当正则，`r"\bWC\b"` 生效。
- **`verbatim` 不等于任意正则**。我第一次写 `ToolSubst(r"@WC@", ..., verbatim=True)`，替换完全没发生。因为 `ToolSubst.resolve()`（`subst.py:105`）还要用固定 `expr`（`subst.py:6`）从正则里抠工具名；`@WC@` 不匹配，`resolve()` 返回 `None`。想用 verbatim，正则必须长成 `\bname\b` 或 `| \bname\b`。

## 14.7 `unresolved` 的四种行为

| 值 | 行为 | 实测 |
|---|---|---|
| `warn`（默认） | 打印 note，**仍然添加**替换，路径是 `llvm_tools_dir/tool_name` | 配置期 note；执行期 127 |
| `fatal` | 立即 `lit_config.fatal` | 退出码 2 |
| `break` | 返回空命令，放弃**整批**替换 | 返回 `False` |
| `ignore` | 不添加替换，不打印 | 裸命令原样进 shell，127 |

```text
# warn
lit.py: .../subst.py:130: note: Did not find myopt in /home/ubuntu/llvm/builds/pr-lane/bin
# executed command: /home/ubuntu/llvm/builds/pr-lane/bin/myopt --version
# | '/home/ubuntu/llvm/builds/pr-lane/bin/myopt': command not found

# fatal
lit.py: .../subst.py:137: fatal: Did not find myopt in /home/ubuntu/llvm/builds/pr-lane/bin
（进程结束，退出码 2）

# break：整批作废
add_tool_substitutions -> False
# executed command: opt --version        ← 连本来找得到的 opt 也没被替换
# | 'opt': command not found

# ignore：静默跳过
# executed command: myopt --version
# | 'myopt': command not found
```

`warn` 最危险：它把「找不到工具」从配置期推迟到执行期，而且添加的是一个不存在的路径。真实 LLVM suite 在 `--show-suites` 时刷屏的 `Did not find ...` 就是它。`break` 的整批语义来自 `add_tool_substitutions` 的「先收集、最后一次性 extend」（`config.py:468-470`）。

## 14.8 `FindTool`：搜索顺序与边界

`FindTool` 只有 17 行（`subst.py:10-26`），顺序很明确：

```python
command = config.lit_config.params.get(self.name)   # 1) -D name=...
if command is None:
    command = lit.util.which(self.name, dirs)        # 2) 只在 dirs 里找
    if not command:
        return None
if self.name == "llc" and os.environ.get("LLVM_ENABLE_MACHINE_VERIFIER") == "1":
    command += " -verify-machineinstrs"
```

三个要点：

1. **`-D` 优先，且不做校验**。实测：

   ```bash
   lit.py tests -D opt=/bin/echo
   ```

   ```text
   # executed command: /bin/echo --version
   # | echo (GNU coreutils) 8.32
   ```

   `-D opt=...` 直接把 `opt` 指到别的程序。调试「到底跑了哪个 opt」时，这比改配置快。

2. **只在给定目录找，不自动回退 PATH**。`dirs` 是 `os.pathsep.join(search_dirs)`。把 `echo` 交给默认的 `llvm_tools_dir`：

   ```text
   lit.py: .../subst.py:130: note: Did not find echo in /home/ubuntu/llvm/builds/pr-lane/bin
   # executed command: /home/ubuntu/llvm/builds/pr-lane/bin/echo hello
   # | '/home/ubuntu/llvm/builds/pr-lane/bin/echo': command not found
   ```

   `echo` 明明在 `/usr/bin`，但 `FindTool` 不会去看 PATH。`use_llvm_tool(use_installed=True)` 才会额外查 `config.environment["PATH"]`（`config.py:553-555`），那是另一个函数。

3. **`llc` 有环境变量特判**：`LLVM_ENABLE_MACHINE_VERIFIER=1` 时自动追加 `-verify-machineinstrs`。这是 `FindTool` 里唯一的工具专属逻辑。

## 14.9 `add_tool_substitutions()` 与替换顺序

`use_llvm_tool()` 是**单工具查找器**（`config.py:528`），参数 `name` / `search_env` / `required` / `quiet` / `search_paths` / `use_installed`。它返回 `normpath` 后的路径并打印 `note: using <name>: <path>`；`required=True` 且找不到时 `fatal`。它**只返回路径，不添加替换**——`use_clang` / `use_lld` 拿到路径后再自己构造 `ToolSubst`。

`add_tool_substitutions` 的骨架（`config.py:441-471`）：

```python
tools = [x if isinstance(x, ToolSubst) else ToolSubst(x) for x in tools]
substitutions = []
for tool in tools:
    match = tool.resolve(self, search_dirs)
    if not match:
        continue
    subst_key, tool_pipe, command = match
    if not command:
        return False
    substitutions.append((subst_key, tool_pipe + command))
self.config.substitutions.extend(substitutions)
return True
```

- 字符串会自动包成 `ToolSubst`，所以 `add_tool_substitutions(["opt", "llc"])` 等价于两个默认 `ToolSubst`。
- `tool_pipe` 来自 `| count` / `| not` 这种模式；普通工具是空串。
- 替换被 **append** 到 `config.substitutions` 末尾。
- 整批成功才 extend；`break` 会丢掉整批。

替换顺序在 `applySubstitutions()` 里是**列表顺序**（`TestRunner.py:1766`）：

```python
for a, b in substitutions:
    ln = _caching_re_compile(a).sub(str(b), escapePercents(ln))
```

先加的规则先执行。所以：

- 顺序决定「规则 A 的替换结果能否被规则 B 再改写」。实测：`ToolSubst("tool", command="mytool")` 在前、`ToolSubst("mytool", command="/bin/echo")` 在后，`tool hello` 最终执行 `/bin/echo hello`；把两条顺序调换，`tool` 只变成 `mytool`，而后一条已经执行过，结果是 `mytool: command not found`。
- `ToolSubst("opt")` 与 `ToolSubst("%opt")` 同时存在时，先命中的那条会改变后一条能否匹配；所以同一工具只该保留一种拼写。（实测 `%opt` 先命中后，`/path/opt` 里的 `opt` 被 `pre` 中的 `/` 挡住，不会二次展开。）
- LLVM 的 `use_default_substitutions()` 先 append `%python`，再调用 `add_tool_substitutions`，顺序是有意设计的。

**规则：同一个工具只保留一种拼写。** 要么裸 `opt`，要么 `%opt`，不要两条规则都加。

## 14.10 `use_default_substitutions()`

`config.py:503-526` 一次性加上 LLVM 测试最常用的几样：

| 替换 | 值 | 说明 |
|---|---|---|
| `FileCheck` | `FindTool("FileCheck")`，`fatal` | 裸 `FileCheck` 变绝对路径 |
| `\| count` | `FindTool("count")`，`verbatim`，`fatal` | 只匹配管道里的 `\| count` |
| `\| not` | `FindTool("not")`，`verbatim`，`fatal` | 只匹配管道里的 `\| not` |
| `%python` | `'"%s"' % sys.executable` | **带引号**的 Python 路径 |
| `%errc_ENOENT` 等 | `os.strerror(...)` 或 site cfg 的 `errc_messages` | 值本身带单引号 |

`--show-suites` 的替换表（节选，真实输出）：

```text
%errc_EACCES => 'Permission denied'
%python => "/usr/bin/python3"
(?<!(\.|\-|\^|/|\\|<))\bFileCheck\b(?!(\-|\.)) => /home/ubuntu/llvm/builds/pr-lane/bin/FileCheck
\| \bcount\b => | /home/ubuntu/llvm/builds/pr-lane/bin/count
\| \bnot\b => | /home/ubuntu/llvm/builds/pr-lane/bin/not
```

跑 `%python -c "print('py ok')"`、`echo hello | count 1`、`not false` 的真实输出：

```text
# RUN: at line 1
"/usr/bin/python3" -c "print('py ok')"
# executed command: /usr/bin/python3 -c 'print('"'"'py ok'"'"')'
# RUN: at line 2
echo hello | /home/ubuntu/llvm/builds/pr-lane/bin/count 1
# executed command: echo hello
# executed command: /home/ubuntu/llvm/builds/pr-lane/bin/count 1
# RUN: at line 3
not false
# executed command: not false
```

两个容易忽略的点：

- `%python` 的替换值**包含双引号**（`'"%s"'`），所以 `# RUN:` 回显里是 `"/usr/bin/python3"`；但内置 shell 会把命令解析成 argv 再打印 `# executed command:`，那里没有引号。别因为两处长得不一样就以为替换错了。
- `| count` / `| not` 是 verbatim 正则，只匹配「管道 + 空格 + 工具名」，不会换掉普通的 `count` 单词；管道在 `# executed command:` 里会被拆成两条。

## 14.11 `use_clang()` 与 `use_lld()`

`use_clang()`（`config.py:726`）做三件事：清环境变量、`use_llvm_tool("clang", search_env="CLANG", ...)`、加 `%clang` / `%clangxx` / `%clang_cc1` / `%clang_cpp` / `%clang_cl` / `%clang_dxc` / `%clang_analyze_cc1` 与 feature `clang`。

它依赖 site cfg 的 `config.target_triple`。没设就直接炸：

```text
File ".../lit/llvm/config.py", line 668, in clang_setup
    if self.config.target_triple:
AttributeError: 'TestingConfig' object has no attribute 'target_triple'
```

补上 `config.host_triple` / `config.target_triple` 后，`%clang`、`%clangxx`、`%clang_cc1` 的真实执行行：

```text
/home/ubuntu/llvm/builds/pr-lane/bin/clang --version
/home/ubuntu/llvm/builds/pr-lane/bin/clang --driver-mode=g++ --version
/home/ubuntu/llvm/builds/pr-lane/bin/clang -cc1 -internal-isystem /home/ubuntu/llvm/builds/pr-lane/lib/clang/24/include -nostdsysteminc -version
```

`%clang_cc1` 自动补 `-cc1 -internal-isystem ... -nostdsysteminc`；`%clangxx` 是 `--driver-mode=g++`。`%clang_cc1` 认 `-version` 不认 `--version`（`error: unknown argument '--version'`），这是 clang CLI，不是 lit 的坑。

`use_lld()`（`config.py:834`）找四个二进制，找到哪个就加同名 feature 和替换：

| 二进制 | feature | 实测 |
|---|---|---|
| `ld.lld` | `ld.lld` | `LLD 24.0.0 (...)` |
| `lld-link` | `lld-link` | `LLD 24.0.0 (...)` |
| `ld64.lld` | `ld64.lld` | `LLD 24.0.0 (...)` |
| `wasm-ld` | `wasm-ld` | `LLD 24.0.0 (...)` |

`--show-suites` 的 feature 行（真实输出是一行）：

```text
Available Features: clang host=x86_64-unknown-linux-gnu ld.lld ld64.lld lld-link
  native non-root-user symlinks system-linux target-x86_64
  target=x86_64-unknown-linux-gnu wasm-ld x86_64-linux
```

`use_lld()` 的正则比 `ToolSubst("ld.lld")` 更严格（`config.py:910`）：多了 `(?<!-flavor\s)` 和 `(?<!=)`，避免匹配 `-flavor ld.lld`、`var=ld.lld`；`ld.lld` 的 `.` 被 `re.escape` 转义，`verbatim=True` 原样使用。

## 14.12 PATH 与 substitution 的分工

只加 PATH、不加替换：

```python
lit.llvm.llvm_config.with_environment("PATH", config.llvm_tools_dir, append_path=True)
```

`# RUN: opt --version` 的真实输出：

```text
PASS: PathOnlyLab :: tests/path-opt.test
# executed command: opt --version
```

能跑，但日志里只有相对名，无法确认跑的是构建目录的 `opt` 还是系统 `opt`。两者解决不同问题：

| 机制 | 解决的问题 | 失败表现 |
|---|---|---|
| substitution | 把测试文本**钉死**到绝对路径 | 找不到时按 `unresolved` 处理；命中后日志可见绝对路径 |
| PATH | 让裸命令、以及工具**内部再调用的兄弟工具**可用 | 运行时 127；日志里只有相对名 |

LLVM 两者都做：`llvm/test/lit.cfg.py:99` 把 `llvm_tools_dir` 加进 PATH，`:492` 再用 `add_tool_substitutions` 把常用工具钉成绝对路径；`use_clang()` / `use_lld()` 内部也 `with_environment("PATH", paths)`，保证 clang 能找到同目录的 `ld.lld`。自建 suite 至少做 substitution；工具会调兄弟程序时再加 PATH。只做 PATH 的 suite 在 CI 上容易因 PATH 顺序跑错二进制。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`ToolSubst("opt")` 也会处理 `%opt`」 | 会「处理」，但结果是 `%/abs/path`，不是 `/abs/path`。`%` 不在 `pre` 集合里，被留在原地。要么用裸 `opt`，要么单独写 `ToolSubst("%opt")`。 |
| 「没设 `llvm_tools_dir` 也能用，反正 command 是显式路径」 | `add_tool_substitutions` 在解析 `command` 之前就读取 `self.config.llvm_tools_dir`。显式路径也必须传 `search_dirs=[...]` 或设置该属性。 |
| 「`unresolved="warn"` 只是警告，测试会正常跳过」 | 它会**添加一个不存在的路径**，测试执行时 127。想跳过应该用 `fatal`/`break`，或者用 feature + `REQUIRES`。 |
| 「`break` 只跳过当前这一个工具」 | 它让 `add_tool_substitutions` 返回 `False`，整批替换都不添加。 |
| 「`FindTool` 会像 shell 一样查 PATH」 | 只在传入的 `dirs` 里查。要查 PATH 用 `use_llvm_tool(use_installed=True)`，或自己把 PATH 放进 `search_dirs`。 |
| 「`verbatim=True` 可以写任意正则」 | `resolve()` 还要用固定 `expr` 抠工具名。`@WC@` 这种形状会静默失败；用 `\bWC\b` 或 `\| \bWC\b`。 |
| 「`%python` 就是 `python3`」 | 它是**带双引号**的 `sys.executable` 绝对路径。日志里看到 `"/usr/bin/python3"` 是正常的。 |
| 「替换顺序无所谓」 | 严格按 `config.substitutions` 列表顺序执行。同一工具加两种拼写会互相污染，出现 `%/path` 或 `/path//path`。 |
| 「`use_clang()` 只要设置 `llvm_tools_dir` 就行」 | 还需要 `config.target_triple`（`clang_setup` 第 668 行会读），否则配置加载阶段 AttributeError。 |

## 重点整理

- `lit.llvm.initialize()` 创建模块级 `llvm_config`；之后所有 `add_tool_substitutions` / `use_*` 都挂在它上面。
- `LLVMConfig` 负责 feature、环境变量、工具查找与替换，不是单纯的 substitution 容器。
- `config.llvm_tools_dir` 在 `add_tool_substitutions` 里被无条件读取；没有就 AttributeError，显式 `search_dirs` 可以绕过。
- `ToolSubst` 的关键是 `key` 与 `pre`/`post`；默认 `pre` 不含 `%`，所以 `%opt` 会被 `ToolSubst("opt")` 污染成 `%/path`。
- `unresolved` 四态：`warn` 加坏路径、`fatal` 退出 2、`break` 整批作废、`ignore` 静默跳过。
- `FindTool` 顺序：`-D` 参数覆盖 → `which` 在给定目录找；不自动查 PATH。
- `add_tool_substitutions` 先收集再 `extend`，顺序就是 `config.substitutions` 的顺序。
- `use_default_substitutions` 提供 `FileCheck`、`| count`、`| not`、`%python`、`%errc_*`；`%python` 带引号。
- `use_clang` 加 `%clang` 系列与 `clang` feature，依赖 `target_triple`；`use_lld` 加四个链接器与同名 feature。
- PATH 让裸命令可运行，substitution 让命令文本变成绝对路径；LLVM 两者都做。

## 自我检核

不看笔记，试着回答：

- [ ] 一个测试写 `%opt`，配置里只有 `ToolSubst("opt")`。最终执行的命令字符串是什么？为什么会多出一个 `%`？
- [ ] 为什么 `add_tool_substitutions([ToolSubst("opt")])` 在没设 `llvm_tools_dir` 时会报 `AttributeError`，而不是返回 `False` 或打印 warning？
- [ ] `unresolved="warn"` 和 `"ignore"` 在「找不到工具」时的两个差异是什么？各自适合什么场景？
- [ ] `FindTool("opt")` 会去 PATH 找吗？如果不会，`use_llvm_tool(use_installed=True)` 多了哪一步？
- [ ] 为什么 `verbatim=True` 的 key 仍然要写成 `\bWC\b`，而不能写 `@WC@`？源码里哪一行导致了这个限制？
- [ ] 一个 suite 只把工具目录加进 PATH，另一个只做 substitution。从 `-a` 的 `# executed command:` 能看出哪个是哪个吗？各自的失败模式是什么？

## 延伸阅读

- **[`lit/llvm/subst.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/llvm/subst.py)**
  - 读什么：全文 152 行。重点 `FindTool.resolve()`（第 14–26 行）、`ToolSubst.__init__`（第 38–103 行）、`ToolSubst.resolve()`（第 105–152 行）。
  - 能学到：正则如何拼、`unresolved` 四态的真实分支、`break` 为什么返回空命令。
  - 前提：会读 Python 正则的 lookbehind/lookahead。
- **[`lit/llvm/config.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/llvm/config.py)**
  - 读什么：`add_tool_substitutions`（第 441–471 行）、`use_default_substitutions`（第 503–526 行）、`use_llvm_tool`（第 528–573 行）、`use_clang`（第 726–800 行）、`use_lld`（第 834–916 行）。
  - 能学到：批量替换的收集/extend 结构、`use_installed` 的 PATH 回退、clang/lld 的 feature 与替换如何成对出现。
  - 前提：读完本章 14.4–14.11。
- **[`llvm/test/lit.cfg.py`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.cfg.py)**
  - 读什么：第 90–105 行（PATH）、第 430–495 行（工具列表与 `add_tool_substitutions`）。
  - 能学到：LLVM 主 suite 到底替换了哪些工具；哪些工具故意留给 PATH。
  - 前提：知道 `config.llvm_tools_dir` 来自 site cfg。
- **[`llvm/utils/lit/tests/Inputs/lld-features/`](https://github.com/llvm/llvm-project/tree/main/llvm/utils/lit/tests/Inputs/lld-features)**
  - 读什么：`lit.cfg` 与四个 `*.txt`，每个只有 `REQUIRES: <linker>` 和 `RUN: true`。
  - 能学到：`use_lld()` 加的 feature 如何被测试直接使用；这是「feature 与替换成对出现」的最小示例。
  - 前提：读完本章 14.11 与 Ch 13 的 `REQUIRES`。
- **[`llvm/utils/lit/tests/lld-features.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/lld-features.py)**
  - 读什么：全文 6 行，重点 `CHECK: Passed: 4`。
  - 能学到：lit 自测如何用一个期望值锁住「四个链接器 feature 都被识别」。
  - 前提：会跑 `lit` 自测（Ch 25）。

> 在进入并行调度之前，先把 Ch 13 的 feature 和本章的工具替换拼成一个完整项目：练习 B 从零搭一个玩具编译器的 lit suite，包含 `lit.cfg.py`、site cfg、`ToolSubst("toyc")`、7 个测试与 CI 入口。

→ [练习 B — 给一个玩具编译器搭 lit suite](./practice-b-toy-compiler-suite.md)
