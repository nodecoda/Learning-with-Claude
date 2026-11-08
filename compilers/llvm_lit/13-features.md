# Ch 13 — Features 与条件测试

> **目标**：能把「这个测试该不该跑、跑了该不该失败」从口号变成可执行的判断：读懂 `available_features` 的来源，写出正确的布尔条件，并解释 `REQUIRES`、`UNSUPPORTED`、`XFAIL`、`limit_to_features` 与 `%if` 各自在哪个阶段起作用。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。本章所有 lit 命令输出都在本机实跑；涉及真实 LLVM suite 的 feature 列表来自 `builds/pr-lane/bin/llvm-lit` 与 `trees/pr-lane`（commit `1bfdad8e5c16`）。已核对 `lit/llvm/config.py`、`lit/llvm/subst.py`、`lit/BooleanExpression.py`、`lit/Test.py` 与 hub `3fa53fc0e1b2` 完全一致；`TestRunner.py` 有差异，涉及它的行号一律以 hub 为准。

---

## 13.1 心智模型：feature 是 suite 贴给测试环境的标签

先不要记语法，先记**四个阶段**。feature 在这四个阶段里被读，但用途完全不同：

```
                    ┌──────────── test 文件 ────────────┐
                    │ REQUIRES: fast && !slow            │
                    │ UNSUPPORTED: system-windows        │
                    │ XFAIL: known-bug                   │
                    └────────────────────────────────────┘
                                  │
                  available_features = {fast, system-linux}
                                  │
      REQUIRES 全为真? ───否───> UNSUPPORTED（不执行，不进 shell）
                                  │是
      UNSUPPORTED 任一为真? ──是──> UNSUPPORTED（不执行）
                                  │否
      XFAIL 任一为真? ───是───> 执行；失败 => XFAIL，通过 => XPASS
                                  │否
                                  执行；失败 => FAIL，通过 => PASS
```

四句话钉死：

- **`REQUIRES`**：逗号分隔的每个表达式都要为真，测试才继续。任一为假 → `UNSUPPORTED`。
- **`UNSUPPORTED`**：逗号分隔的表达式任一为真，测试就跳过 → `UNSUPPORTED`。
- **`XFAIL`**：逗号分隔的表达式任一为真，测试**仍然执行**，但「失败」变成 `XFAIL`，「通过」变成 `XPASS`。
- **`XFAIL: *`** 是唯一的通配写法；`*` 在 `REQUIRES` / `UNSUPPORTED` 里没有特殊含义。

`XPASS` 不是成功。源码里 `ResultCode("XPASS", "Unexpectedly Passed", True)` 的第三个参数表示 failure，所以一次 `XPASS` 会让 lit 返回 1。

> 如果你还不清楚「`UNSUPPORTED` 是在执行前判定的」，先回看 [Ch 2 — lit 全景架构](./02-lit-architecture.md) 的「2.5 Discovery」与「2.7 执行」两节。

## 13.2 `available_features` 是一个 set

`TestingConfig.fromdefaults()` 把它初始化成 `set`（`lit/TestingConfig.py:204`）：

```python
self.available_features = set(available_features)
```

`lit.cfg.py` / `lit.local.cfg.py` 里最常见的三种写法：

```python
config.available_features.add("fast")
config.available_features.update({"avx512", "linux"})
config.available_features.discard("slow")   # 子目录里撤销父配置的标签
```

因为它是 `set`，重复添加没有副作用，顺序也不保留。`--show-suites` 打印时按字母序展示。

最小 suite 里不调用 `lit.llvm.initialize()` 时，**不会有任何自动 feature**。下面这个 suite 只手动加了三个：

```python
# /tmp/lit-ch13-xxxx/lit.cfg.py
import os
import lit.formats

config.name = "FeatureLab"
config.test_format = lit.formats.ShTest()
config.suffixes = [".test"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")

config.available_features.update({"fast", "la"})
```

真实输出：

```text
$ python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --show-suites /tmp/lit-ch13-xxxx/tests
-- Test Suites --
  FeatureLab - 9 tests
    Source Root: /tmp/lit-ch13-xxxx
    Exec Root  : /tmp/lit-ch13-xxxx/Output
    Available Features: fast la
    Available Substitutions:
```

只有 `fast la`，没有 `system-linux`、没有 `host=...`。这不是 bug：自动 feature 由 `lit.llvm` 的 `LLVMConfig` 注入，Ch 14 会打开那个开关。**先用 `--show-suites` 确认 feature 到底有没有，再谈条件写没写对。**

## 13.3 布尔表达式：语法只有一页

实现是 `lit/BooleanExpression.py`。文件开头第 5–17 行就是完整语法：

```text
expr         :: or_expr
or_expr      :: and_expr ('||' and_expr)*
and_expr     :: not_expr ('&&' not_expr)*
not_expr     :: '!' not_expr
                '(' or_expr ')'
                match_expr
match_expr   :: braced_regex
                identifier
                braced_regex match_expr
                identifier match_expr
identifier   :: [-+=._a-zA-Z0-9]+
braced_regex :: '{{' python_regex '}}'
```

| 写法 | 含义 | 备注 |
|---|---|---|
| `fast` | `available_features` 里有 `fast` | 大小写敏感 |
| `!slow` | 没有 `slow` | `!` 优先级最高 |
| `fast && !slow` | 两个条件都成立 | `&&` 高于 `\|\|` |
| `fast \|\| slow` | 任一成立 | |
| `(fast \|\| slow) && !x` | 括号改结合 | |
| `true` | 恒真 | `BooleanExpression.__init__` 自动加入 |
| `{{^la[bx]$}}` | 任一 feature 被 Python 正则 **fullmatch** | 锚点可写但非必需 |
| `fast, slow` | 两个独立表达式 | 组合规则由 directive 决定 |

三个容易忽略的细节：

1. **正则是 `fullmatch`，不是 `search`。** `parseMATCH()`（`lit/BooleanExpression.py:103`）最后做 `regex.fullmatch(var)`。`{{la}}` 只匹配名字恰好是 `la` 的 feature，不会匹配 `lab`。想要前缀匹配就写 `{{la.*}}`。
2. **`{{}}` 是语法错误。** tokenizer 要求 `{{.+?}}`，空正则直接 `couldn't parse text: '{{}}'`。
3. **逗号在解析前就被切开了。** `_handleBooleanExpr`（`lit/TestRunner.py:1991`）用 `line.split(",")` 切分，再逐段交给 `BooleanExpression`。所以正则里不要放逗号：`{{a,b}}` 会被切成两段。用 `{{a|b}}`。

### 组合规则不是一种

同一行里的逗号、以及多行 directive，组合方式取决于关键字：

| 关键字 | 同一行 `a, b` | 多行 `REQUIRES:` | 结果 |
|---|---|---|---|
| `REQUIRES` | 都要真（AND） | 所有行都要真（AND） | 任一假 → `UNSUPPORTED` |
| `UNSUPPORTED` | 任一真（OR） | 任一行任一真（OR） | 真 → `UNSUPPORTED` |
| `XFAIL` | 任一真（OR） | 任一行任一真（OR） | 真 → 预期失败 |

源码位置：`Test.getMissingRequiredFeatures()`（`lit/Test.py:410`）遍历 `requires` 列表，全部要 `evaluate` 为真；`getUnsupportedFeatures()`（`lit/Test.py:421`）收集任一为真的项；`isExpectedToFail()`（`lit/Test.py:339`）遇任一为真就返回 `True`。

### `*` 只属于 `XFAIL`

`isExpectedToFail()` 第 358 行有唯一一处特判：

```python
if item == "*":
    return True
```

`REQUIRES: *` 不会被解析器拒绝（`_handleBooleanExpr` 第 2001 行把 `*` 跳过语法检查），但到了判定阶段 `BooleanExpression.evaluate("*", features)` 会抛 `ValueError`，结果是 `UNRESOLVED`。实测：

```text
UNRESOLVED: FeatureLab :: tests/star-requires.test
ValueError: Error in REQUIRES list:
couldn't parse text: '*'
in expression: '*'
```

这也解释了为什么 `--show-used-features` 要过滤 `expr != "*"`（`lit/Test.py:463`）：通配符不是 feature token。

## 13.4 真实实验：一个 9 个测试的 feature 矩阵

建一个最小 suite，只手动加 `fast`、`la`，然后放 9 个测试覆盖四条路径：

```text
tests/requires-both.test          REQUIRES: fast && !slow       RUN: true
tests/requires-and-missing.test   REQUIRES: fast && slow        RUN: true
tests/unsupported-fast.test       UNSUPPORTED: fast             RUN: true
tests/xfail-fast.test             XFAIL: fast                   RUN: false
tests/xfail-slow-fails.test       XFAIL: slow                   RUN: false
tests/xfail-fast-passes.test      XFAIL: fast                   RUN: true
tests/regex-hit.test              REQUIRES: {{^la[bx]$}}        RUN: true
tests/regex-miss.test             REQUIRES: {{^lc$}}            RUN: true
tests/param-feature.test          REQUIRES: toy-feature         RUN: true
```

注意 `regex-hit.test` 要求 `la[bx]`，也就是 `lab` 或 `lax`；光有 `la` 不匹配。这就是 fullmatch 的直接后果。

跑：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py /tmp/lit-ch13-xxxx/tests
```

真实输出：

```text
-- Testing: 9 tests, 9 workers --
UNSUPPORTED: FeatureLab :: tests/param-feature.test (1 of 9)
UNSUPPORTED: FeatureLab :: tests/regex-hit.test (2 of 9)
UNSUPPORTED: FeatureLab :: tests/regex-miss.test (3 of 9)
UNSUPPORTED: FeatureLab :: tests/requires-and-missing.test (4 of 9)
XPASS: FeatureLab :: tests/xfail-fast-passes.test (5 of 9)
XFAIL: FeatureLab :: tests/xfail-fast.test (6 of 9)
FAIL: FeatureLab :: tests/xfail-slow-fails.test (7 of 9)
PASS: FeatureLab :: tests/requires-both.test (8 of 9)
UNSUPPORTED: FeatureLab :: tests/unsupported-fast.test (9 of 9)
********************
Failed Tests (1):
  FeatureLab :: tests/xfail-slow-fails.test

********************
Unexpectedly Passed Tests (1):
  FeatureLab :: tests/xfail-fast-passes.test


Testing Time: 0.02s

Total Discovered Tests: 9
  Unsupported        : 5 (55.56%)
  Passed             : 1 (11.11%)
  Expectedly Failed  : 1 (11.11%)
  Failed             : 1 (11.11%)
  Unexpectedly Passed: 1 (11.11%)
```

这一屏值得逐条读：

| 测试 | 判定 | 原因 |
|---|---|---|
| `requires-both` | PASS | `fast` 有、`slow` 没有，`fast && !slow` 为真 |
| `requires-and-missing` | UNSUPPORTED | `fast && slow` 为假；不会执行 `true` |
| `unsupported-fast` | UNSUPPORTED | `UNSUPPORTED: fast` 命中 |
| `xfail-fast` | XFAIL | 条件命中，`false` 失败被接受 |
| `xfail-slow-fails` | FAIL | `slow` 不存在，XFAIL 不生效；失败就是失败 |
| `xfail-fast-passes` | XPASS | 条件命中却通过了，算 failure |
| `regex-hit` | UNSUPPORTED | suite 里只有 `la`；`la[bx]` 需要 `lab` 或 `lax`，fullmatch 不认 `la` |
| `regex-miss` | UNSUPPORTED | `{{^lc$}}` 不匹配任何 feature |
| `param-feature` | UNSUPPORTED | `toy-feature` 还没注入 |

补上 `lab`（`config.available_features.update({"fast", "la", "lab"})`）再跑，`--show-suites` 会显示 `Available Features: fast la lab`，`regex-hit` 变为 PASS。失败原因也能从 `-a` 直接读到：

```text
UNSUPPORTED: FeatureLab :: tests/requires-and-missing.test
Test requires the following unavailable features: fast && slow

UNSUPPORTED: FeatureLab :: tests/unsupported-fast.test
Test does not support the following features and/or targets: fast
```

这两句来自 `parseIntegratedTestScript()` 的 `TestRunner.py:2135` 与 `2144`，是排查 `REQUIRES` 写错时最快的信息。

### 用 `--param` 注入 feature

lit 没有「`-D` 直接加 feature」的开关。`-D` 只是往 `lit_config.params` 放键值（`create_params()`，`lit/main.py:161`），要不要变成 feature 由你的 `lit.cfg.py` 决定：

```python
if lit_config.params.get("toy_feature"):
    config.available_features.add("toy-feature")
```

于是（这里的 suite 已经按上一段补上 `lab`，`regex-hit` 会一起跑）：

```bash
python3 .../lit.py /tmp/lit-ch13-xxxx/tests -D toy_feature=on
```

真实输出（节选）：

```text
PASS: FeatureLab :: tests/param-feature.test (8 of 9)
...
  Passed             : 3 (33.33%)
  Unsupported        : 3 (33.33%)
```

`-D toy_feature`（不带 `=value`）得到的是空字符串，`params.get(...)` 为假，feature 不会加。写 `-D toy_feature=1`、`=on`、`=true` 都可以，只要值非空。

## 13.5 自动 feature 从哪来

自动 feature 分两层，别混：

```
lit.llvm.initialize(lit_config, config)
        └─ LLVMConfig.__init__          → 平台 / triple / sanitizer / symlinks ...
lit.cfg.py 显式调用
        ├─ llvm_config.feature_config() → asserts / debug
        └─ config.available_features.add() → 项目自己的 feature
```

### 第一层：`LLVMConfig.__init__`（`lit/llvm/config.py:27`）

| 来源 | feature | 源码 |
|---|---|---|
| 操作系统 | `system-linux` / `system-darwin` / `system-windows` / `system-freebsd` / `system-netbsd` / `system-aix` / `system-solaris` / `system-zos` / `system-cygwin` | `config.py:104-122` |
| triple | `host=<triple>`、`target=<triple>` | `config.py:126-129` |
| host 与 target 相同 | `native` | `config.py:130-131` |
| sanitizer | `asan` / `hwasan` / `msan` / `ubsan` / `tsan` | `config.py:133-148` |
| 压缩库 | `zlib` / `zstd` / `lzma` | `config.py:149-155` |
| 参数 | `-D run_long_tests=1` → `long_tests` | `config.py:161-163` |
| target triple 前缀 | `target-x86` / `target-x86_64` / `target-aarch64` / `target-arm` / `target-riscv64` / `target-loongarch64` | `config.py:166-189` |
| target 平台 | `x86_64-linux`、`x86_64-apple`、`target=powerpc64le-linux` | `config.py:167-188` |
| 非 root | `non-root-user` | `config.py:197` |
| 非 Windows | `symlinks` | `config.py:210` |

### 第二层：LLVM 自己的 `llvm/test/lit.cfg.py`

| feature | 来源 | 源码 |
|---|---|---|
| `<arch>-registered-target` | `config.targets_to_build.split()` 的每个后端名小写 | `lit.cfg.py:496-499` |
| `asserts` | `llvm-config --assertion-mode` 输出 `ON` | `lit.cfg.py:782-788` |
| `debug` | `llvm-config --build-mode` 输出匹配 `[Dd][Ee][Bb][Uu][Gg]` | 同上 |
| `expensive_checks` | site cfg 的 `config.expensive_checks` | `lit.cfg.py:841-842` |

拿真实构建验证：

```bash
/home/ubuntu/llvm/builds/pr-lane/bin/llvm-lit --show-suites \
  /home/ubuntu/llvm/trees/pr-lane/llvm/test/Transforms/InstCombine
```

真实输出（第一行是 suite 信息，后面是 feature）：

```text
-- Test Suites --
  LLVM - 1915 tests
    Source Root: /home/ubuntu/llvm/trees/pr-lane/llvm/test
    Exec Root  : /home/ubuntu/llvm/builds/pr-lane/test
    Available Features: aarch64-registered-target ansi-escapes asserts backtrace
      can-execute cxx-shared-library debug_frame default_triple have_opt_viewer_modules
      host-byteorder-little-endian host-unwind-supports-jit host=x86_64-unknown-linux-gnu
      libxml2 llvm-64-bits loongarch-registered-target lzma native non-root-user
      object-emission ondisk_cas plugins riscv-registered-target symlinks system-linux
      target-byteorder-little-endian target-x86_64 target=x86_64-unknown-linux-gnu
      thread_support unix-sockets x86-registered-target x86_64-linux zlib zstd
```

（为可读性手动换行，真实输出是一行。）

对照 site cfg 就能解释每个值：

```python
# /home/ubuntu/llvm/builds/pr-lane/test/lit.site.cfg.py
config.host_triple = "x86_64-unknown-linux-gnu"
config.target_triple = "x86_64-unknown-linux-gnu"
config.targets_to_build = " X86 AArch64 RISCV LoongArch"
config.native_target = "X86"
config.enable_assertions = 1
config.expensive_checks = 0
```

- `host=` 与 `target=` 相同 → `native`。
- `asserts` 出现，因为构建开了 assertions，`llvm-config --assertion-mode` 报 `ON`。
- `expensive_checks` **没有**出现，因为值是 0。别看到别人测试里写 `REQUIRES: expensive_checks` 就以为自己的构建一定有。
- `x86-registered-target`、`aarch64-registered-target`、`riscv-registered-target`、`loongarch-registered-target` 来自 `targets_to_build`。

## 13.6 `target=` 与 `X86` 不是一回事

同一个构建里同时存在四种「target」写法，含义完全不同：

| 写法 | 例子 | 来自 | 回答的问题 |
|---|---|---|---|
| 后端名（大写） | `X86`、`AArch64` | `config.targets_to_build` | 这个构建编了哪些后端 |
| 注册 feature（小写） | `x86-registered-target` | `arch.lower() + "-registered-target"` | 后端是否注册进 `llc`/`opt` |
| 默认 triple | `target=x86_64-unknown-linux-gnu` | `config.target_triple` | 这个构建默认给谁生成代码 |
| triple 架构 | `target-x86_64`、`target-aarch64` | `target_triple` 的正则分支 | 默认 triple 的架构族 |

所以：

- `REQUIRES: X86` **永远不成立**。`X86` 只是 `config.targets` 集合里的字符串（`lit.cfg.py:496`），不会自动进 `available_features`。要判断后端是否可用，写 `REQUIRES: x86-registered-target`。
- `REQUIRES: target=x86_64` 也不成立。`config.py:129` 加的是完整 triple `target=x86_64-unknown-linux-gnu`，fullmatch 不认前缀。想匹配前缀就用正则：`REQUIRES: {{target=x86_64.*}}`。
- 只关心架构、不关心 vendor/OS 时，用 `target-x86_64`。
- 在子目录 `lit.local.cfg.py` 里访问 suite 根的值，用 `config.root.targets`；`config.targets` 是根配置上的属性，子配置是它的副本。

> 这也是为什么测试里常见 `REQUIRES: x86-registered-target` 而不是 `REQUIRES: X86`：前者回答「这个 lit 构建能不能跑 X86 后端」，后者根本不是 feature。

## 13.7 `--show-used-features`：测试到底用了哪些 feature

`--show-used-features` 在发现完测试后直接打印并退出（`lit/main.py:62-72`）。它扫描每个测试的 `XFAIL` / `UNSUPPORTED` / `REQUIRES`，把 token 去重、排序、空格连接成一行：

```bash
python3 .../lit.py --show-used-features /tmp/lit-ch13-xxxx/tests
```

真实输出：

```text
fast slow toy-feature {{^la[bx]$}} {{^lc$}}
```

要点：

- 它包含**所有**三个 directive 里出现的 token，不只是当前会命中的。
- 操作符 `!`、`&&`、`||`、括号、`true` 不会出现；`*` 被显式过滤。
- 正则 token 原样保留 `{{...}}`，不会展开成匹配到的 feature。
- 输出是一行；用 FileCheck 检查时，多条 `CHECK:` 可以在同一行里顺序匹配。

这个命令最适合回答两个问题：「我新加的 feature 名字有没有拼错？」「这个 suite 到底依赖哪些平台标签？」

## 13.8 `limit_to_features`：只跑需要某些 feature 的测试

`config.limit_to_features` 不是 CLI 开关，只能在配置里设：

```python
config.available_features.update({"fast"})
config.limit_to_features = ["fast"]
```

它的语义比名字绕，源码注释写得很准确（`lit/Test.py:370-398`）：一个测试要「在限制内」，必须同时满足

1. 当前 `REQUIRES` 全部满足；
2. 把 `limit_to_features` 从 `available_features` 里拿掉之后，`REQUIRES` **不再**全部满足。

翻成白话：**测试必须明确要求列表里的至少一个 feature**。没有 `REQUIRES` 的测试会被判 `UNSUPPORTED`，而不是「不筛选、照跑」。

实测 suite：`available_features={"fast"}`、`limit_to_features=["fast"]`，4 个测试：

```text
PASS: LimitLab :: tests/requires-fast.test (1 of 4)
UNSUPPORTED: LimitLab :: tests/no-requires.test (2 of 4)
Test does not require any of the features specified in limit_to_features: fast
PASS: LimitLab :: tests/requires-fast-not-slow.test (3 of 4)
UNSUPPORTED: LimitLab :: tests/requires-slow.test (4 of 4)
Test requires the following unavailable features: slow
```

注意最后一条：它先因为 `slow` 不可用被判 `UNSUPPORTED`，根本轮不到 `limit_to_features` 的判定。判定顺序在 `TestRunner.py:2135-2160` 是固定的：`REQUIRES` → `UNSUPPORTED` → `limit_to_features`。

还要注意：被 `limit_to_features` 挡掉的测试计为 **`UNSUPPORTED`**，不是 `EXCLUDED`。摘要里会进入 `Unsupported` 一栏，退出码仍是 0。

## 13.9 `%if` 改命令文本，`REQUIRES` 决定跑不跑

这是本章最容易混的一对。

| | `REQUIRES:` | `%if ... %{...%} %else %{...%}` |
|---|---|---|
| 作用阶段 | 执行前判定 | 替换阶段，改 `RUN:` 行的文本 |
| 不满足时 | 整个测试 `UNSUPPORTED`，一条命令都不跑 | 测试照跑，只是选中另一个分支 |
| 语法 | 布尔表达式 | `%if <布尔表达式> %{ <文本> %} %else %{ <文本> %}` |
| 能嵌套 | 否 | 是 |
| 典型用途 | 平台/后端/构建选项限制 | 同一个测试里切换命令行参数 |

实现是 `substituteIfElse()`（`lit/TestRunner.py:1671`），条件同样交给 `BooleanExpression.evaluate(cond, conditions)`，其中 `conditions` 是 `available_features` 的字典（`TestRunner.py:2307`）。

实测：

```text
# RUN: echo pre-%if fast %{FAST%} %else %{SLOW%}-post
# RUN: echo %if slow %{not-this%} %else %{fallback%}
```

`-a` 输出：

```text
# RUN: at line 1
echo pre-FAST-post
# executed command: echo pre-FAST-post
# | pre-FAST-post
# RUN: at line 2
echo fallback
# executed command: echo fallback
# | fallback
```

两条命令都执行了，只是文本被改写。如果写成 `REQUIRES: slow`，整个测试会直接 `UNSUPPORTED`，连 `echo` 都不会出现。

语法细节（源码 `tryParseIfCond` 第 1676 行）：

- `%if` 后面**必须有空格**：`%if feature`。写成 `%iffeature` 不会被识别。
- `%{` 和 `%}` 必须成对；缺一个就是 `ValueError`，测试变 `UNRESOLVED`。
- `%else` 可省略；省略时等价于空分支。
- 花括号里的空格不会被忽略：`%{ 3 %}` 会原样留下前后空格。
- `%%if` 转义成字面量 `%if`。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`REQUIRES` 写错都会变 UNRESOLVED」 | 拼错 feature 名（`REQUIRES: fst`）是合法表达式，只是不成立 → **UNSUPPORTED**；只有语法错（`*`、`&&` 结尾、括号不配对、`{{}}`）才是 **UNRESOLVED**。两者排查方向完全不同。 |
| 「feature 名不区分大小写」 | 区分。`fast` 与 `Fast` 是两个 feature；`X86` 不是 `x86-registered-target`。 |
| 「`target=x86_64` 能匹配 `x86_64-unknown-linux-gnu`」 | 不能。`target=` 后面是完整 triple，表达式用 fullmatch；要前缀匹配写 `{{target=x86_64.*}}`。 |
| 「`XFAIL` 等于 skip」 | 不等于。`XFAIL` 条件不命中时测试照跑，失败就是 FAIL。条件命中时测试也照跑，只是失败被接受；通过反而变 XPASS（failure）。 |
| 「`XFAIL: *` 和 `REQUIRES: *` 一样」 | `*` 只在 `XFAIL` 有特判。`REQUIRES: *` 会在求值阶段抛 `ValueError`，结果 `UNRESOLVED`。 |
| 「`limit_to_features` 把测试标成 EXCLUDED」 | 标成 `UNSUPPORTED`，理由是 `Test does not require any of the features specified in limit_to_features`。 |
| 「`%if` 可以替代 `REQUIRES`」 | `%if` 只改命令文本，测试仍然执行；需要「整条测试不跑」时必须用 `REQUIRES` / `UNSUPPORTED`。 |
| 「`UNSUPPORTED: fast, slow` 要两个都成立」 | 任一成立就跳过。`UNSUPPORTED` 是 OR，`REQUIRES` 才是 AND。 |

## 重点整理

- `available_features` 是 `set`；`--show-suites` 是确认它内容的第一工具。
- `REQUIRES` 全真才跑；`UNSUPPORTED` 任一真就跳过；`XFAIL` 任一真就把失败视为预期。
- 布尔表达式支持 `&&`、`||`、`!`、括号、`true`、`{{python_regex}}`；逗号在解析前被切开。
- 正则是 `fullmatch`；`{{^...$}}` 的锚点可写但非必需。
- `*` 只在 `XFAIL` 有特殊语义；`REQUIRES: *` → UNRESOLVED。
- 自动 feature 由 `lit.llvm.initialize()` 的 `LLVMConfig` 注入；不调用就没有 `system-*` / `host=` / `target=` / `symlinks`。
- `asserts`、`expensive_checks` 来自 `llvm/test/lit.cfg.py` 与 site cfg，不在 `LLVMConfig` 里。
- 四种 target 写法各司其职：`X86`（后端名）、`x86-registered-target`（可用性）、`target=<triple>`（默认 triple）、`target-x86_64`（架构族）。
- `limit_to_features` 要求测试显式依赖列表中的 feature，否则 `UNSUPPORTED`。
- `%if` 改文本，`REQUIRES` 决定是否执行；两者不能互相替代。

## 自我检核

不看笔记，试着回答：

- [ ] 一个测试写了 `REQUIRES: fast && !slow`，suite 有 `fast` 和 `slow`。它会 PASS、FAIL 还是 UNSUPPORTED？为什么？
- [ ] `REQUIRES: *` 和 `REQUIRES: totally-made-up` 的结果分别是什么？两者的错误信息差别在哪？
- [ ] 为什么 `REQUIRES: X86` 不成立，而 `REQUIRES: x86-registered-target` 可能成立？这两个名字分别来自哪里？
- [ ] 一个测试 `XFAIL: broken`，但 suite 没有 `broken` feature，测试里的命令又真的失败了。最终状态是什么？如果命令通过了又是什么？
- [ ] `limit_to_features=["fast"]` 时，一个完全没有 `REQUIRES` 的测试会怎样？为什么源码要实现「移除限制后不再满足」这个反向条件？
- [ ] 同一个测试里，`REQUIRES: fast` 和 `%if fast %{--enable%} %else %{%}` 能互相替代吗？举一个必须用前者的场景。

## 延伸阅读

- **[`lit/BooleanExpression.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/BooleanExpression.py)**
  - 读什么：第 5–17 行的语法注释、`parseMATCH` / `parseNOT` / `parseAND` / `parseOR` 四个函数，以及文件末尾的单元测试。
  - 能学到：为什么 `*` 不是语法的一部分、正则为什么是 fullmatch、错误信息长什么样。
  - 前提：读完本章 13.3；会读递归下降解析器。
- **[`lit/llvm/config.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/llvm/config.py)**
  - 读什么：`LLVMConfig.__init__` 第 27–210 行，重点看平台、triple、sanitizer、symlinks 四段。
  - 能学到：每个自动 feature 的确切来源与拼写；哪些 feature 只在特定平台出现。
  - 前提：知道 `platform.system()` 与 LLVM triple 的基本结构。
- **[`llvm/test/lit.cfg.py`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.cfg.py)**
  - 读什么：第 488–520 行（targets 与 byteorder）、第 780–790 行（asserts）、第 835–845 行（expensive_checks 等）。
  - 能学到：LLVM 主 suite 如何把构建配置翻译成 feature；为什么 `x86-registered-target` 是小写。
  - 前提：读完 Ch 11–12 的配置级联。
- **[`llvm/utils/lit/tests/shtest-format.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/shtest-format.py) 与 `tests/Inputs/shtest-format/`**
  - 读什么：`requires-present.txt`、`requires-missing.txt`、`requires-star.txt`、`requires-triple.txt` 及对应的 `CHECK` 行。
  - 能学到：lit 自己如何测试 `REQUIRES` 的四种结果；`REQUIRES: *` 的 UNRESOLVED 是官方预期。
  - 前提：会跑 `lit` 自测（Ch 25 会讲）。
- **[`llvm/utils/lit/tests/Inputs/shtest-if-else/test.txt`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/Inputs/shtest-if-else/test.txt)**
  - 读什么：全文 108 行，重点看嵌套、`%else` 省略、空格保留、`%%if` 转义。
  - 能学到：`%if` 的边界行为；哪些写法会报 `'%{' is missing`。
  - 前提：读完本章 13.9。

> 下一章把 feature 的另一半补上：`lit.llvm` 到底在 `initialize()` 里做了什么，`ToolSubst` 如何把 `opt` 变成绝对路径，以及为什么 `%opt` 会被替换成 `%/path`。

→ [Ch 14 工具替换：lit.llvm、ToolSubst、FindTool](./14-tool-substitutions.md)
