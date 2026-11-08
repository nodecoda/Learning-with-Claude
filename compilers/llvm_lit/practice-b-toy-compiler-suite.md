# 练习 B — 给一个玩具编译器搭 lit suite

> **目标**：不依赖 LLVM 构建目录，从零给一个 Python 玩具编译器搭出可运行的 lit suite：`lit.cfg.py`、`lit.site.cfg.py`、feature、`ToolSubst`、7 个覆盖正常/错误/条件/预期失败/多文件的测试，并接上 xUnit 与 CI 入口。

> **环境**：LLVM `24.0.0dev` 的 lit（`/home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py`），Python 3.10.12，Ubuntu 22.04。不需要构建 LLVM，但需要能 import `lit` 包。本练习所有命令与输出都在 `/tmp` 实跑；只有标注「未实测」的部分是模板/示意。

---

## B.1 任务

你要为一个叫 `toyc` 的玩具编译器搭 suite。验收标准：

1. `lit tests` 发现 7 个测试，默认全绿（允许 1 个 `XFAIL`）。
2. 覆盖正常/JSON/两类错误/`REQUIRES`/`XFAIL`/`%t` 多文件。
3. `toyc` 必须经 `ToolSubst("toyc", ...)` 变成绝对命令，不靠 PATH。
4. 关掉 `toy-opt` 后 `opt-fold.toy` 变 `UNSUPPORTED`、`known-bug.toy` 从 `XFAIL` 变 `FAIL`；`--xunit-xml-output` 能产出 XML。

## B.2 玩具语言与 CLI 规格

`toyc` 读 `.toy` 文件，按行编译成三地址码（three-address code，TAC）。语言故意很小：

```text
# 注释到行尾
x = 1 + 2 * 3      # 赋值：name = expr
print x            # 打印变量

expr := INT | NAME | expr + expr | expr - expr
      | expr * expr | expr / expr | (expr) | -expr
```

规则：

- 变量必须先定义后使用；未定义报 `undefined variable '<name>'`。
- 只支持整数；`/` 向零截断。
- 每条语句一行；空行和 `#` 注释忽略；临时值 `t0`、`t1`…… 跨语句递增。

CLI：

```text
toyc [--opt] [--emit=text|json] FILE
```

| 选项 | 含义 |
|---|---|
| `--opt` | 常量折叠：两个整数字面量直接算出结果，不生成临时变量 |
| `--emit=json` | 输出 `{"code": ["...", ...]}` |

退出码：

| 情况 | 退出码 |
|---|---|
| 成功 | 0 |
| 文件读不到、语法错、未定义变量、除零 | 1 |

手工跑一次，先确认编译器本身是对的：

```bash
$ python3 toyc.py tests/basic-arith.toy
t0 = 2 * 3
t1 = 1 + t0
x = t1
t2 = x - 4
y = t2
print y

$ python3 toyc.py --opt tests/basic-arith.toy
x = 7
t0 = x - 4
y = t0
print y

$ python3 toyc.py --emit=json tests/basic-arith.toy
{"code": ["t0 = 2 * 3", "t1 = 1 + t0", "x = t1", "t2 = x - 4", "y = t2", "print y"]}

$ printf 'x = 1\nprint y\n' > /tmp/err.toy
$ python3 toyc.py /tmp/err.toy; echo $?
toyc: error: line 2: undefined variable 'y'
1
```

注意最后一条：`toyc` 的 line number 是**物理行号**，注释和 `RUN:` 行都算。写错误测试时不要断言一个想当然的行号。

## B.3 目录结构

```text
toy-lit-suite/
├── lit.cfg.py
├── lit.site.cfg.py
├── toyc.py
├── expect_error.py
├── tests/
│   ├── *.toy           ← 7 个测试
│   ├── *.expected      ← diff 的期望输出
│   └── *.toypart       ← 多文件测试的输入片段
└── Output/             ← lit 生成的执行目录，加进 .gitignore
```

两个命名决定值得说明：

- 输入片段用 `.toypart`，不用 `.toy`。`config.suffixes = [".toy"]` 会把所有 `.toy` 当测试；如果 `multi-a.toy` 没有 `RUN:`，它会变成 `UNRESOLVED`。
- `.expected` 不受 `suffixes` 影响，用 `%S/xxx.expected` 引用。

## B.4 参考实现：`toyc.py`

完整实现 79 行（为控制篇幅省略了部分 PEP 8 空行）：

```python
#!/usr/bin/env python3
"""toyc: compile .toy assignments/print to three-address code."""
import argparse
import ast
import json
import re
import sys

OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
class ToyError(Exception):
    pass
class Compiler:
    def __init__(self, opt):
        self.opt, self.code, self.env, self.tmp = opt, [], {}, 0
    def newtmp(self):
        self.tmp += 1
        return f"t{self.tmp - 1}"
    def emit(self, op, left, right):
        if self.opt and isinstance(left, int) and isinstance(right, int):
            if op == "/":
                if right == 0:
                    raise ToyError("division by zero")
                return int(left / right)
            return {"+": left + right, "-": left - right, "*": left * right}[op]
        name = self.newtmp()
        self.code.append(f"{name} = {left} {op} {right}")
        return name
    def expr(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in self.env:
                raise ToyError(f"undefined variable '{node.id}'")
            return node.id
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return self.expr(node.operand)  # KNOWN BUG: negation is dropped
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return self.emit(OPS[type(node.op)], self.expr(node.left),
                             self.expr(node.right))
        raise ToyError("unsupported expression")

    def compile(self, text):
        for no, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                if line.startswith("print "):
                    name = line[6:].strip()
                    if name not in self.env:
                        raise ToyError(f"undefined variable '{name}'")
                    self.code.append(f"print {name}")
                else:
                    name, sep, expr = line.partition("=")
                    name, expr = name.strip(), expr.strip()
                    if not sep or not re.fullmatch(r"[A-Za-z_]\w*", name):
                        raise ToyError("expected 'name = expr'")
                    self.env[name] = self.expr(ast.parse(expr, mode="eval").body)
                    self.code.append(f"{name} = {self.env[name]}")
            except (ToyError, SyntaxError) as e:
                raise ToyError(f"line {no}: {e}") from None
        return self.code

def main():
    ap = argparse.ArgumentParser(prog="toyc")
    ap.add_argument("--opt", action="store_true", help="fold integer constants")
    ap.add_argument("--emit", choices=["text", "json"], default="text")
    ap.add_argument("file")
    args = ap.parse_args()
    try:
        code = Compiler(args.opt).compile(open(args.file).read())
    except (OSError, ToyError) as e:
        print(f"toyc: error: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"code": code}) if args.emit == "json" else "\n".join(code))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

实现要点：

- 表达式解析借 `ast.parse(expr, mode="eval")`，只接受 `Constant` / `Name` / `BinOp` / `USub`。
- `--opt` 的折叠在 `emit()`：两个 `int` 操作数直接算，不生成临时变量。
- **第 41 行是故意留的已知 bug**：一元负号被丢掉，用来给 `XFAIL` 一个真实失败理由；修掉后 `known-bug.toy` 会变 `XPASS`（failure）。
- 错误统一 `toyc: error: line N: ...`，退出码 1。

## B.5 配置：`lit.cfg.py` 与 `lit.site.cfg.py`

LLVM 的惯例是 **site cfg 负责「这次构建的实例值」，cfg 负责「suite 的通用规则」**。`lit.site.cfg.py` 会覆盖 `lit.cfg.py` 被发现的优先级，因此它必须显式 `load_config()` 回 cfg：

```python
# lit.site.cfg.py
import os

config.toyc = os.path.join(os.path.dirname(__file__), "toyc.py")
config.enable_toy_opt = True
config.enable_known_bug = True

lit_config.load_config(config, os.path.join(os.path.dirname(__file__), "lit.cfg.py"))
```

`lit.cfg.py` 的核心只有四件事：

```python
config.name = "ToyC"
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = [".toy"]
config.test_exec_root = os.path.join(os.path.dirname(__file__), "Output")

lit.llvm.initialize(lit_config, config)
lit.llvm.llvm_config.add_tool_substitutions(
    [ToolSubst("toyc", command=f"{sys.executable} {config.toyc}", unresolved="fatal")],
    search_dirs=[os.path.dirname(config.toyc)],
)
```

四个关键点：

1. **`search_dirs` 必须传。** 没有 `config.llvm_tools_dir` 时，不传会在 `add_tool_substitutions` 第一行 `AttributeError`（Ch 14.3）；显式 `command` 也躲不掉。
2. **`ToolSubst("toyc")` 只匹配裸 `toyc`。** 写 `%toyc` 会得到 `%/usr/bin/python3 ...`（Ch 14.5）。
3. **feature 从 site 值来。** 改 `toy-opt` / `toy-known-bug` 只需改 site cfg。
4. **`parallelism_group = "toyc"`** 把同组测试串行化；细节在 Ch 15。

本练习用手写 `lit.site.cfg.py`，不需要生成步骤。真实项目通常用 `lit.site.cfg.py.in` + CMake `configure_file` 注入 `@TOYC@`，原理相同。

## B.6 测试设计：7 个测试各证明什么

| # | 文件 | 类型 | 断言 | 依赖 |
|---|---|---|---|---|
| 1 | `basic-arith.toy` | 正常 | 输出与 `basic-arith.expected` 逐字节一致 | 无 |
| 2 | `emit-json.toy` | 正常 | JSON 可解析，且含 `"print x"` | 无 |
| 3 | `error-undefined.toy` | 错误 | 退出码 1，stderr 含 `undefined variable 'y'` | `%expect` |
| 4 | `error-syntax.toy` | 错误 | 退出码 1，stderr 含 `expected 'name = expr'` | `%expect` |
| 5 | `opt-fold.toy` | 条件 | `--opt` 输出与 `opt-fold.expected` 一致 | `REQUIRES: toy-opt` |
| 6 | `known-bug.toy` | 预期失败 | 正确的 TAC 与当前输出不一致 | `XFAIL: toy-known-bug` |
| 7 | `multi-file.toy` | 多文件 | 两个 `.toypart` 合并后编译，`%t` 隔离 | 无 |

三个设计选择：

- **正常测试用 `diff`，不用 `grep`。** `diff %t %S/xxx.expected` 能发现多一行、少一行、空格变化；`grep` 只证明某一行存在。`diff` 是 lit 内置命令，不依赖 LLVM。
- **错误测试用 `%expect` helper。** 直接写 `toyc %s; test $? -eq 1` 会踩 lit 内置 shell 的 `$?` 展开坑；`|| true` 又会吞掉退出码。helper 同时断言退出码和 stderr，完整代码在 B.10 的参考解答里。测试写 `python3 %expect 1 "undefined variable 'y'" %{compiler} %s`；`%{compiler}` 直接替换成 `config.toyc`，不经过 `ToolSubst("toyc")`。
- **`known-bug.toy` 的 expected 写「正确结果」。** 当前实现丢掉一元负号，输出 `t0 = 2 + 3`；expected 写 `t0 = 0 - 2`。`diff` 失败 → `XFAIL`。等 bug 修好，diff 通过 → `XPASS`，suite 变红，提醒你把 `XFAIL` 删掉。

## B.7 跑起来

```bash
cd toy-lit-suite
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py -j1 tests
```

真实输出：

```text
-- Testing: 7 tests, 1 workers --
PASS: ToyC :: tests/emit-json.toy (1 of 7)
PASS: ToyC :: tests/error-undefined.toy (2 of 7)
PASS: ToyC :: tests/error-syntax.toy (3 of 7)
PASS: ToyC :: tests/basic-arith.toy (4 of 7)
PASS: ToyC :: tests/opt-fold.toy (5 of 7)
PASS: ToyC :: tests/multi-file.toy (6 of 7)
XFAIL: ToyC :: tests/known-bug.toy (7 of 7)

Testing Time: 0.26s

Total Discovered Tests: 7
  Passed           : 6 (85.71%)
  Expectedly Failed: 1 (14.29%)
```

`-j1` 让输出可复现；`Expectedly Failed: 1` 不会让退出码变 1。

看 suite 的 feature 与替换：

```bash
python3 .../lit.py --show-suites tests
```

```text
-- Test Suites --
  ToyC - 7 tests
    Source Root: /tmp/toy-lit-suite
    Exec Root  : /tmp/toy-lit-suite/Output
    Available Features: host=None non-root-user symlinks system-linux target=None toy-known-bug toy-opt
    Available Substitutions: %expect => /tmp/toy-lit-suite/expect_error.py
                             %{compiler} => /tmp/toy-lit-suite/toyc.py
                             (?<!(\.|\-|\^|/|\\|<))\btoyc\b(?!(\-|\.)) => /usr/bin/python3 /tmp/toy-lit-suite/toyc.py
```

`host=None` / `target=None` 是因为这个 suite 没设 `host_triple` / `target_triple`。Ch 13 的自动 feature 里 `host=`/`target=` 仍然会被加，只是值是 `None`——这是个无害但值得知道的细节。

看一条测试的真实展开：

```bash
python3 .../lit.py tests/basic-arith.toy -a
```

```text
# 路径用 ... 缩写；其余原样
# executed command: /usr/bin/python3 /tmp/toy-lit-suite/toyc.py .../basic-arith.toy
# executed command: diff .../basic-arith.toy.tmp /tmp/toy-lit-suite/tests/basic-arith.expected
```

`%t` 落在 `Output/tests/Output/...`，因为 `test_exec_root` 是 suite 根的 `Output`，而测试路径里还有一层 `tests`。这不影响隔离：每个测试的 `%t` 都带自己的文件名。

## B.8 故意让它红：关掉 feature

把 `lit.site.cfg.py` 的两行改成 `False`：

```python
config.enable_toy_opt = False
config.enable_known_bug = False
```

再跑（节选，路径与未变化行省略）：

```text
-- Testing: 7 tests, 1 workers --
...
UNSUPPORTED: ToyC :: tests/opt-fold.toy (4 of 7)
FAIL: ToyC :: tests/known-bug.toy (7 of 7)
********************
Failed Tests (1):
  ToyC :: tests/known-bug.toy
```

`opt-fold.toy` 的 `REQUIRES` 不满足 → `UNSUPPORTED`；`known-bug.toy` 的 `XFAIL` 不命中 → 照跑、`diff` 失败 → `FAIL`。这就是 `XFAIL` 与 skip 的区别：**它只在 feature 命中时「接受失败」，不会阻止执行。** 改回 `True` 后恢复绿色。

## B.9 卡点

按踩到的顺序：

1. **`lit.site.cfg.py` 存在时，`lit.cfg.py` 不会自动加载。** 必须 `lit_config.load_config(config, ...)`，否则 `config.name`、`suffixes` 全是默认值，一个测试都发现不了。
2. **`add_tool_substitutions` 需要 `search_dirs`。** 没有 `config.llvm_tools_dir` 的 suite 会直接 `AttributeError`。传 `search_dirs=[...]` 是最小修法。
3. **`.toy` 输入片段会被当测试。** 多文件测试的片段改用 `.toypart`，或者放进 `Inputs/` 并用 `config.excludes` 处理。
4. **错误测试的退出码不能用 `|| true` 吞。** 它让整条 RUN 变 0；用 helper 同时断言退出码和 stderr。`$?` 在 lit 内置 shell 里不可靠（实测 `false; echo $?` 触发了 `GlobItem` 异常），不要依赖。
5. **`toyc` 的 line number 包含 `RUN:` 行。** 错误测试别断言 `line 2`，只断言消息片段；否则加一行注释就红。
6. **`diff` 对结尾换行敏感。** `toyc` 用 `print()` 输出，末尾有换行；expected 文件也必须以换行结尾。用 `printf` 生成 expected 时容易漏。
7. **`%t` 是同一个测试内的同一个路径基名。** 区分输入/输出时用 `%t.toy`、`%t`、`%t.rc`。修好 bug 后 `XFAIL` 会变 `XPASS`（failure），同一提交要删掉 `XFAIL` 行。

## B.10 参考解答

<details>
<summary>展开：完整 lit.cfg.py、site cfg、helper 与 7 个测试</summary>

`lit.cfg.py`：

```python
import os
import sys

import lit.formats
import lit.llvm
from lit.llvm.subst import ToolSubst

config.name = "ToyC"
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = [".toy"]
config.test_source_root = os.path.dirname(__file__)
config.test_exec_root = os.path.join(config.test_source_root, "Output")
config.parallelism_group = "toyc"

# lit.site.cfg.py 提供这两个值；没有 site cfg 时回退到同目录。
config.toyc = getattr(config, "toyc", os.path.join(config.test_source_root, "toyc.py"))
config.enable_toy_opt = getattr(config, "enable_toy_opt", True)
config.enable_known_bug = getattr(config, "enable_known_bug", True)

config.substitutions.append(
    ("%expect", os.path.join(config.test_source_root, "expect_error.py"))
)
config.substitutions.append(("%{compiler}", config.toyc))

lit.llvm.initialize(lit_config, config)
lit.llvm.llvm_config.add_tool_substitutions(
    [ToolSubst("toyc", command=f"{sys.executable} {config.toyc}", unresolved="fatal")],
    search_dirs=[os.path.dirname(config.toyc)],
)

if config.enable_toy_opt:
    config.available_features.add("toy-opt")
if config.enable_known_bug:
    config.available_features.add("toy-known-bug")
```

`lit.site.cfg.py`：

```python
import os

config.toyc = os.path.join(os.path.dirname(__file__), "toyc.py")
config.enable_toy_opt = True
config.enable_known_bug = True

lit_config.load_config(config, os.path.join(os.path.dirname(__file__), "lit.cfg.py"))
```

`expect_error.py`：

```python
import subprocess
import sys

expected, needle = int(sys.argv[1]), sys.argv[2]
proc = subprocess.run([sys.executable, *sys.argv[3:]], capture_output=True, text=True)
sys.stderr.write(proc.stderr)
sys.exit(0 if proc.returncode == expected and needle in proc.stderr else 1)
```

`tests/basic-arith.toy`：

```text
# RUN: toyc %s > %t
# RUN: diff %t %S/basic-arith.expected
x = 1 + 2 * 3
y = x - 4
print y
```

`tests/basic-arith.expected` 与 B.2 的 text 输出一致。

`tests/emit-json.toy`：

```text
# RUN: toyc --emit=json %s > %t
# RUN: python3 -m json.tool %t > /dev/null
# RUN: grep -q '"print x"' %t
x = 2 * 3
print x
```

`tests/error-undefined.toy`：

```text
# RUN: python3 %expect 1 "undefined variable 'y'" %{compiler} %s
x = 1
print y
```

`tests/error-syntax.toy`：

```text
# RUN: python3 %expect 1 "expected 'name = expr'" %{compiler} %s
x 1 + 2
```

`tests/opt-fold.toy`：

```text
# REQUIRES: toy-opt
# RUN: toyc --opt %s > %t
# RUN: diff %t %S/opt-fold.expected
x = 1 + 2 * 3
y = x - 4
print y
```

`tests/opt-fold.expected` 与 B.2 的 `--opt` 输出一致。

`tests/known-bug.toy`：

```text
# XFAIL: toy-known-bug
# RUN: toyc %s > %t
# RUN: diff %t %S/known-bug.expected
x = -2 + 3
print x
```

`tests/known-bug.expected`（正确语义；当前实现会差一行 `t0 = 2 + 3`）：

```text
t0 = 0 - 2
t1 = t0 + 3
x = t1
print x
```

`tests/multi-a.toypart` / `tests/multi-b.toypart`：

```text
# multi-a.toypart
a = 10
print a
# multi-b.toypart
b = a + 5
print b
```

`tests/multi-file.toy`：

```text
# RUN: cat %S/multi-a.toypart %S/multi-b.toypart > %t.toy
# RUN: toyc %t.toy > %t
# RUN: diff %t %S/multi.expected
```

`tests/multi.expected`：

```text
a = 10
print a
t0 = a + 5
b = t0
print b
```

</details>

## B.11 加分挑战

### xUnit 输出

```bash
python3 .../lit.py tests --xunit-xml-output report.xml
```

真实 XML 头部：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<testsuites time="0.10">
<testsuite name="ToyC" tests="7" failures="0" skipped="0" time="0.26">
<testcase classname="ToyC.tests" name="basic-arith.toy" time="0.03"/>
<testcase classname="ToyC.tests" name="emit-json.toy" time="0.05"/>
...
```

注意 `skipped="0"`：`XFAIL` 在 xUnit 里仍是一个普通 `<testcase>`，不会变成 `<skipped>`。报告侧不要假设 `XFAIL` 会被单独统计。

### `parallelism_group`

`config.parallelism_group = "toyc"` 把所有同组测试串行化，避免多个 worker 同时写同目录下的 `%t`。当前 7 个测试很快，看不出差别；细节在 Ch 15。

### CI 入口

```bash
#!/bin/sh
# 未实测：CI 模板；本地等价于本练习跑过的命令
set -eu
lit -j"$(nproc)" tests --xunit-xml-output report.xml
```

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「site cfg 和 lit.cfg 都存在时，两个都会自动加载」 | site cfg 优先被发现，且不会自动加载 cfg；必须 `lit_config.load_config(config, ...)`。 |
| 「`XFAIL` 会让测试不跑」 | 只有 `REQUIRES`/`UNSUPPORTED` 会阻止执行；`XFAIL` 照跑，只是失败被接受。feature 关掉后它会变 FAIL。 |
| 「`\|\| true` 能测试错误退出码」 | 它把整条命令的退出码变成 0，测试不再验证「应该失败」。用 helper 同时断言退出码和消息。 |
| 「`.expected` 文件会被当测试」 | 只要它不在 `config.suffixes` 里就不会；本练习只认 `.toy`。反过来，`.toy` 输入片段必须改名或换后缀。 |
| 「`add_tool_substitutions` 的 `command` 是绝对路径就不用管搜索目录」 | 函数在解析 command 前就读取 `llvm_tools_dir`；没有就 AttributeError。传 `search_dirs`。 |
| 「`ToolSubst("toyc")` 也会处理路径里的 `toyc`」 | `post="-."` 会挡住 `toyc.py` 里的 `toyc`；但 `%toyc` 会被换成 `%/path`。同一工具只保留一种拼写。 |
| 「`%t` 是一个目录」 | `%t` 是**文件名基**；`%T` 在 LLVM 24 已移除。`diff` 假红时先看 `-a` 的实际命令与 stdout，再查末尾换行。 |

## 重点整理

- 一个可发布 suite 的最小闭环：site cfg 实例值 → cfg 通用规则 → suffixes 发现 → `ToolSubst` 定位工具 → feature 条件 → 测试断言。
- `lit.site.cfg.py` 必须 `load_config()` 回 `lit.cfg.py`；没有 `llvm_tools_dir` 时用 `search_dirs` 绕过。
- `.toy` 只给测试，输入片段用 `.toypart`。
- 正常输出用 `diff` 对 expected；错误路径用 helper 同时断言退出码与 stderr。
- `REQUIRES` 决定跑不跑，`XFAIL` 只决定失败是否被接受；关掉 feature 是验证两者区别的最快实验。
- `%t` 是文件基名，多文件测试用 `%t.toy` / `%t` 区分输入输出。
- xUnit 把 `XFAIL` 当普通 testcase，不会标 `skipped`；报告侧要自己处理。
- `parallelism_group` 是声明「这些测试不能并发」的地方，比事后调 `-j1` 更可维护。

## 自我检核

- [ ] 如果删掉 `lit.site.cfg.py` 的 `load_config` 行，`lit tests` 会发生什么？为什么？
- [ ] 为什么 `add_tool_substitutions` 在 `search_dirs` 缺失时会 AttributeError，而不是静默失败？这跟 Ch 14.3 的哪一行对应？
- [ ] `known-bug.toy` 在 `toy-known-bug` 开启和关闭时分别是什么状态？如果 bug 被修好，又是什么状态？
- [ ] `%expect` 为什么要同时检查 `returncode` 和 `needle`？只检查其中一个会漏掉什么？
- [ ] 多文件测试为什么用 `.toypart` 而不是 `.toy`？如果把片段改成 `.toy`，lit 会给什么结果？

## 延伸阅读

- **[lit Command Guide — TEST SUITES / RUNNING TESTS](https://llvm.org/docs/CommandGuide/lit.html#test-suites)**
  - 读什么：suite 定义、`lit.cfg`、`lit.site.cfg`、`--xunit-xml-output` 几节。
  - 能学到：本练习的配置在官方文档里的对应位置；哪些行为是 lit 核心、哪些是 LLVM 扩展。
  - 前提：完成 Ch 3 与 Ch 11–12。
- **[`llvm/utils/lit/tests/lit.cfg`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/lit.cfg)**
  - 读什么：全文，重点看 `llvm_config` 为空时的 fallback、`config.excludes = ["Inputs"]`、`%{lit}` 替换。
  - 能学到：lit 自己如何搭一个既能 standalone 又能接 LLVM 构建的 suite。
  - 前提：读完 Ch 14 的 `initialize` 与 `ToolSubst`。
- **[`llvm/test/lit.site.cfg.py.in`](https://github.com/llvm/llvm-project/blob/main/llvm/test/lit.site.cfg.py.in)**
  - 读什么：全文约 80 行，重点看 `@TARGETS_TO_BUILD@`、`config.llvm_tools_dir`、最后的 `load_config()`。
  - 能学到：真实项目的 site cfg 模板长什么样；哪些值由 CMake 注入。
  - 前提：知道 `configure_file` 的基本用法。

> 下一章进入调度器：`-j` 开了几个进程、`parallelism_group` 如何限制并发、`%t` 为什么能在并行下保持隔离。

→ [Ch 15 并行执行模型](./15-parallel-execution.md)
