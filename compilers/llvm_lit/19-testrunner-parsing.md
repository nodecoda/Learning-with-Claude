# Ch 19 — TestRunner I：从注释到脚本

> **目标**：把「一个测试文件」拆成 lit 内部的数据结构：字节如何被扫描成 `(行号, 关键字, 内容)`，八个内置 parser 各自产出什么，`RUN:` 的续行如何合并，`END.` 为什么能提前终止，以及 `parseIntegratedTestScript()` 在什么情况下直接返回 `UNRESOLVED`。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码根 `/home/ubuntu/llvm/llvm-project`，Python 3.10.12。本章所有命令与输出都在本机实跑；源码行号以该 commit 为准，引用前已用 `grep -n` 核对。

---

## 19.1 先纠正一个直觉：lit 不认「注释」

大多数测试文件长这样：`# RUN: echo hello`、`# REQUIRES: x86`、`# XFAIL: windows`。

`#` 只是**人类约定的注释符**，lit 完全不理解它。lit 的扫描器在文件里找的是关键字字节串 `RUN:`、`XFAIL:`、`REQUIRES:`……出现在哪一行、哪个位置，它不关心前面有没有 `#`，也不关心这行是不是合法的 shell 注释。

```text
文件字节
   │  regex: (RUN:|XFAIL:|REQUIRES:|...)(.*)\n
   ▼
(line_number, keyword, rest)   ← 每个关键字一次 yield
   │  按 keyword 分派给 parser
   ▼
parser 的 value（list / int / bool / directives）
```

所以 `# RUN:` 能工作，只是因为 `RUN:` 真的在字节里；字符串里的 `RUN:` 同样会被看到；把 `#` 换成 `//` 也照样工作，因为 lit 根本没看 `#`。

但「能看到」不等于「每次都能看到」。扫描用的正则是 `(keyword)(.*)\n`，`.*` 是贪婪的、不跨行。**一行里第一个关键字会把这一行剩下的内容全部吃掉**；只有出现在它之前的同 line 关键字才可能被单独匹配。这个细节决定了下面两个实验的结果不同。

## 19.2 `parseIntegratedTestScriptCommands()`：字节级扫描

函数在 `llvm/utils/lit/lit/TestRunner.py:1283`：

```python
def parseIntegratedTestScriptCommands(source_path, keywords):
    keywords_re = re.compile(
        b"(%s)(.*)\n" % (b"|".join(re.escape(k.encode("utf-8")) for k in keywords),)
    )

    f = open(source_path, "rb")
    try:
        data = f.read()
        if not data.endswith(b"\n"):
            data = data + b"\n"

        line_number = 1
        last_match_position = 0
        for match in keywords_re.finditer(data):
            match_position = match.start()
            line_number += data.count(b"\n", last_match_position, match_position)
            last_match_position = match_position

            keyword, ln = match.groups()
            yield (
                line_number,
                keyword.decode("utf-8"),
                ln.decode("utf-8").rstrip("\r"),
            )
    finally:
        f.close()
```

逐点拆开：

| 代码 | 为什么这样做 |
|---|---|
| `open(source_path, "rb")` | 测试文件可能不是合法 UTF-8；按字节扫描不会在读取阶段就抛 `UnicodeDecodeError` |
| `if not data.endswith(b"\n")` | 让最后一行也能匹配 `(.*)\n`；否则没有换行的文件会漏掉最后一条指令 |
| `keywords_re`（第 1294 行） | 关键字来自调用方传入的 key 集合，不是硬编码；`re.escape` 保证 `END.` 里的 `.` 不是通配符 |
| `finditer`，不是 `splitlines()` | 扫描的是整个文件，行号要靠新行计数补出来 |
| `line_number += data.count(b"\n", last, pos)` | 只数「上一次匹配位置」到「本次匹配位置」之间的换行；因此行号是 O(n) 总量，不是每行重扫 |
| `.rstrip("\r")` | 二进制模式不会把 Windows 的 `\r\n` 转成 `\n`，这里手动去掉行尾 `\r` |
| `keyword.decode("utf-8")` | 关键字一定是我们自己写进去的 ASCII，安全；内容行才可能包含任意字节 |

注意两个「没有」：正则**没有 `^` 或单词边界**，`RUN:` 作为子串就匹配；也**没有按行切分后判断前缀**，所以 `#`、`//`、`;` 都一样。

### 实验 19-A：字符串里的 `RUN:`

先看一个会被「吃掉」的情况：

```text
# RUN: echo "RUN: echo x"
```

运行：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py tests/string-run.test
```

真实输出：

```text
PASS: LitLab :: tests/string-run.test (1 of 1)
  Passed: 1 (100.00%)
```

为什么没炸？因为这一行从第一个 `RUN:` 开始匹配，`.*\n` 把引号里的第二个 `RUN:` 也吞了。整个指令是：

```text
echo "RUN: echo x"
```

这是合法的 shell 命令，退出码 0。

把第二个 `RUN:` 放到**新的一行、且该行没有更早的关键字**：

```text
# RUN: echo start
echo "RUN: echo x"
```

再跑：

```text
-- Testing: 1 tests, 1 workers --
UNRESOLVED: LitLab :: tests/string-run.test (1 of 1)
******************** TEST 'LitLab :: tests/string-run.test' FAILED ********************
Exit Code: 1

Command Output (stdout):
--
# shell parser error on RUN: at line 2: echo x"

--

********************
```

第二行里第一个（也是唯一一个）可匹配的 `RUN:` 在引号内。它被当成关键字，剩下内容 `echo x"` 成为一条命令；引号不配对，内置 shell 在解析阶段抛 `ScriptFatal`，结果 `UNRESOLVED`。

同一个机制还能制造更隐蔽的 bug：

```text
# XRUN: echo embedded
```

真实输出是 `PASS`。因为 `XRUN:` 里包含子串 `RUN:`，而 `RUN:` 前面没有更早的匹配，扫描器就从 `R` 开始匹配。写自定义指令时，不要以为加个前缀字母就能「屏蔽」内置关键字。

### 实验 19-B：没有换行结尾的文件

`data + b"\n"` 的补丁不是摆设。若文件最后一行是 `# RUN: echo tail` 且没有换行，没有这一步就匹配不到。本章未单独跑这个用例（未实测），逻辑在源码第 1304 行；你可以用 `printf` 造一个无尾换行文件验证。

## 19.3 `_parseKeywords()`：八个内置 parser

`_parseKeywords()` 在 `TestRunner.py:2019`。它先建立 parser 字典，再逐条把扫描结果喂给对应 parser。

```python
builtin_parsers = [
    IntegratedTestKeywordParser("RUN:", ParserKind.COMMAND, initial_value=script),
    IntegratedTestKeywordParser("XFAIL:", ParserKind.BOOLEAN_EXPR),
    IntegratedTestKeywordParser("REQUIRES:", ParserKind.BOOLEAN_EXPR),
    IntegratedTestKeywordParser("UNSUPPORTED:", ParserKind.BOOLEAN_EXPR),
    IntegratedTestKeywordParser("ALLOW_RETRIES:", ParserKind.INTEGER),
    IntegratedTestKeywordParser("END.", ParserKind.TAG),
    IntegratedTestKeywordParser("DEFINE:", ParserKind.DEFINE, initial_value=script),
    IntegratedTestKeywordParser("REDEFINE:", ParserKind.REDEFINE, initial_value=script),
]
```

（第 2033–2041 行，`REDEFINE:` 的构造跨两行。）

`ParserKind` 是 `enum.IntEnum`（第 1813 行），每种 kind 规定关键字允许的后缀：

| Kind | 允许后缀 | 例子 | 解析结果 |
|---|---|---|---|
| `TAG` | `.` | `END.` | 空行 -> `True`；非空行不改变值 |
| `COMMAND` | `:` | `RUN:` | `CommandDirective` 列表 |
| `LIST` | `:` | 自定义 | 逗号切分的字符串列表 |
| `SPACE_LIST` | `:` | 自定义 | 空格切分的字符串列表 |
| `BOOLEAN_EXPR` | `:` | `XFAIL:`、`REQUIRES:`、`UNSUPPORTED:` | 逗号切分的布尔表达式列表 |
| `INTEGER` | `:` | `ALLOW_RETRIES:` | 整数列表（注意仍是 list） |
| `CUSTOM` | `:` 或 `.` | 扩展 | 调用方提供的 parser |
| `DEFINE` | `:` | `DEFINE:` | `SubstDirective` |
| `REDEFINE` | `:` | `REDEFINE:` | `SubstDirective` |

### 19.3.1 COMMAND：`RUN:` 与续行

`_handleCommand()`（第 1942 行）做两件事：

1. 先展开行号表达式 `%(line)`、`%(line+N)`、`%(line-N)`（`_substituteLineNumbers`，第 1930 行）。
2. 尝试把当前行接到上一条 `CommandDirective` 后面；接不上就新建一条，并在命令前插入 `%dbg(RUN: at line N)`。

真实实验文件：

```text
# RUN: echo one \
# RUN:   two \
# RUN:   three
# DEFINE: %{msg} = hello \
# DEFINE:   world
```

用 lit 自己的模块直接解析：

```python
import lit.TestRunner as TR
r = TR._parseKeywords("tests/cont.test")
for d in r["RUN:"]:
    if isinstance(d, TR.CommandDirective):
        print(repr(d.command), d.start_line_number, d.end_line_number)
print([(d.name, d.value) for d in r["DEFINE:"] if isinstance(d, TR.SubstDirective)])
```

真实输出（只保留关键部分）：

```text
'%dbg(RUN: at line 1)  echo one    two    three' 1 3
[('%{msg}', 'hello world')]
```

更值得记住的是：`DEFINE:` parser 的 `initial_value` 也是 `script`，和 `RUN:` 是**同一个 list 对象**。所以 `parsed["DEFINE:"]` 里不只有 `SubstDirective`，还有前面的 `CommandDirective`；`parseIntegratedTestScript()` 直接断言了这一点：

```python
script = parsed["RUN:"] or []
assert parsed["DEFINE:"] == script
assert parsed["REDEFINE:"] == script
```

这也解释了为什么 `DEFINE`/`REDEFINE` 必须按出现顺序参与替换：它们和 `RUN` 在同一个序列里，`applySubstitutions()` 顺序遍历，遇到 `SubstDirective` 就修改替换表，遇到 `CommandDirective` 就展开命令。

续行的判定在 `CommandDirective.needs_continuation()`（第 1523 行）：

```python
return self.command[-1] == "\\"
```

`add_continuation()` 只在**关键字相同**且上一行以 `\` 结尾时才接：`self.command = self.command[:-1] + line.rstrip()`。所以 `# RUN: a \` 后跟 `# REQUIRES: x86` 不会续行，`RUN` 的 `\` 留在原地并最终报错；换成 `# RUN: b` 才会合并。合并时只去掉一个 `\`，后续行前导空格保留，因此会出现多个连续空格——lit 不替你规整。

### 实验 19-C：未终止的续行

```text
# RUN: echo hello \
# REQUIRES: x86
```

真实输出：

```text
UNRESOLVED: LitLab :: tests/unterminated.test (1 of 1)
Test has unterminated 'RUN:' directive (with '\') at line 1
```

检查在 `_parseKeywords()` 第 2076–2084 行：遍历所有 directive，只要 `needs_continuation()` 还是真，就抛 `ValueError`。`parseIntegratedTestScript()` 捕获后转成 `UNRESOLVED`。

### 19.3.2 BOOLEAN_EXPR：`XFAIL:` / `REQUIRES:` / `UNSUPPORTED:`

`_handleBooleanExpr()`（第 1989 行）按逗号切分，并支持一种续行：

```python
parts = [s.strip() for s in line.split(",") if s.strip() != ""]
if output and output[-1][-1] == "\\":
    output[-1] = output[-1][:-1] + parts[0]
    del parts[0]
```

它会对每个表达式调用 `BooleanExpression.evaluate(s, [])`。这里的目的不是求值，而是**提前验证语法**。所以下面这种错误在解析阶段就会炸：

```text
# RUN: echo hi
# XFAIL: ((
```

真实探针输出（`_parseKeywords`，`require_script=True`）：

```text
ValueError: expected: '!', '(', '{{', or identifier
have: <end of expression>
in expression: '(('
in XFAIL: directive on test line 2
```

注意 `ValueError` 里带上了「哪个关键字、第几行」。格式来自 `IntegratedTestKeywordParser.parseLine()`（第 1911 行）的统一包装。

多条同关键字会**累加**，不是覆盖：

```text
# XFAIL: a
# XFAIL: b
```

得到 `["a", "b"]`。`isExpectedToFail()` 对任意一条为真就返回真（OR 语义）。这与下一节的 `ALLOW_RETRIES` 形成对比。

### 19.3.3 INTEGER：为什么重复 `ALLOW_RETRIES` 会报错

`_handleSingleInteger()`（第 1973 行）把每行转成 `int` 后 **append 到列表**。它不检查长度；长度检查在 `_parseKeywords()` 末尾（第 2097–2100 行）：

```python
allowed_retries = keyword_parsers["ALLOW_RETRIES:"].getValue()
if allowed_retries and len(allowed_retries) > 1:
    raise ValueError("Test has more than one ALLOW_RETRIES lines")
```

实验文件：

```text
# RUN: echo ok
# ALLOW_RETRIES: 2
# ALLOW_RETRIES: 3
```

真实输出：

```text
UNRESOLVED: LitLab :: tests/two-retries.test (1 of 1)
******************** TEST 'LitLab :: tests/two-retries.test' FAILED ********************
Test has more than one ALLOW_RETRIES lines
********************
```

这是显式设计：重试次数是全局策略，不允许文件里出现两个矛盾声明。非整数同样会报 `INTEGER parser requires the input to be an integer`。

### 19.3.4 TAG 与 `END.`

`END.` 的 parser 是 `_handleTag()`（第 1925 行）：

```python
return not line.strip() or output
```

- `# END.` 后面没有内容 -> 返回 `True`。
- `# END. blah` -> 返回之前的 `output`（初始是 `None`），不触发终止。

`_parseKeywords()` 的循环（第 2060–2064 行）：

```python
for line_number, command_type, ln in parseIntegratedTestScriptCommands(
    sourcepath, keyword_parsers.keys()
):
    parser = keyword_parsers[command_type]
    parser.parseLine(line_number, ln)
    if command_type == "END." and parser.getValue() is True:
        break
```

实验：

```text
# RUN: echo before
# END.
# RUN: false
```

用 `-a` 运行：

```text
PASS: LitLab :: tests/end-tag.test (1 of 1)
# RUN: at line 1
echo before
# executed command: echo before
# | before
```

`false` 没有出现在输出里，也没有把测试变红。`END.` 是**停止扫描**，不是「注释掉下一行」。它的常见用途是：文件后半段是给别的工具看的样例文本，里面可能包含 `RUN:` 字样，提前终止可以避免误解析。

## 19.4 `parseIntegratedTestScript()`：从 directive 到可执行脚本

函数在 `TestRunner.py:2105`。核心流程：

```python
try:
    parsed = _parseKeywords(test.getSourcePath(), additional_parsers, require_script)
except ValueError as e:
    return lit.Test.Result(Test.UNRESOLVED, str(e))

script = parsed["RUN:"] or []
assert parsed["DEFINE:"] == script
assert parsed["REDEFINE:"] == script
test.xfails += parsed["XFAIL:"] or []
if test.exclude_xfail and test.isExpectedToFail():
    return lit.Test.Result(Test.EXCLUDED, "excluding XFAIL tests")
test.requires += parsed["REQUIRES:"] or []
test.unsupported += parsed["UNSUPPORTED:"] or []
if parsed["ALLOW_RETRIES:"]:
    test.allowed_retries = parsed["ALLOW_RETRIES:"][0]
```

然后依次检查：

1. `getMissingRequiredFeatures()` 非空 -> `UNSUPPORTED`。
2. `getUnsupportedFeatures()` 非空 -> `UNSUPPORTED`。
3. `not test.isWithinFeatureLimits()` -> `UNSUPPORTED`。
4. 否则返回 `script`（一个可能包含 `CommandDirective` 和 `SubstDirective` 的混合列表）。

### 19.4.1 `require_script=` 到底控制什么

它只控制一件事：**没有 `RUN:` 时是否报错**。

```python
if require_script and not any(
    isinstance(directive, CommandDirective) for directive in script
):
    raise ValueError("Test has no 'RUN:' line")
```

（第 2067–2071 行。）

实验文件 `no-run.test`：

```text
# This file has no RUN line.
# XFAIL: *
```

分别用 `require_script=True/False` 调用 `_parseKeywords()`，真实输出：

```text
tests/no-run.test require_script=True ValueError: Test has no 'RUN:' line
tests/no-run.test require_script=False OK script=[] xfails=['*']
```

所以 `require_script=False` 适合「脚本可选」的自定义 format（比如 GoogleTest 的虚拟测试）。但它**没有**关闭语法检查：

```text
tests/bad-expr.test require_script=True  ValueError: ... in XFAIL: directive on test line 2
tests/bad-expr.test require_script=False ValueError: ... in XFAIL: directive on test line 2
```

`XFAIL:` 的括号错误、`REQUIRES:` 的非法操作符、未终止的 `\`、重复的 `ALLOW_RETRIES:`，在两种模式下都会抛。不要把 `require_script=False` 当成「宽松模式」。

### 19.4.2 `exclude_xfail` 与 `limit_to_features`

`exclude_xfail` 来自 `--exclude-xfail`（或测试对象上的同名属性）。解析到 `XFAIL:` 且当前 feature 命中时，测试直接变成 `EXCLUDED`，连命令都不展开：

```text
EXCLUDED: ResultLab :: tests/xfail.test
```

`limit_to_features` 的语义比名字绕：它要求测试**满足 REQUIRES**，并且**在移除受限 feature 后不再满足**。换句话说，它只跑「确实依赖这些 feature」的测试。源码在 `Test.isWithinFeatureLimits()`（`Test.py:370`）：

它先检查 `getMissingRequiredFeatures()`，再把 `limit_to_features` 从 `available_features` 里删掉后重新检查；若删除后 REQUIRES 仍然满足，说明测试并不依赖这些 feature，返回 `False`。这不在 `_parseKeywords()` 里，而是在 `parseIntegratedTestScript()` 的最后一关；理解它需要先理解 `REQUIRES:` 的布尔表达式，Ch 13 已展开。

## 19.5 `getUsedFeatures()`：`--show-used-features` 的兜底实现

`Test.getUsedFeatures()` 在 `Test.py:444`。它不读缓存，而是**重新调用 `_parseKeywords(..., require_script=False)`**，只取三个关键字：

内部先 `_parseKeywords(..., require_script=False)`，把 `UNSUPPORTED:`/`REQUIRES:`/`XFAIL:` 的值串起来，对每个表达式（`*` 除外）做 `BooleanExpression.tokenize()`，最后只保留 `isMatchExpression` 的 token。

实验文件：

```text
# RUN: echo hi
# REQUIRES: x86 && linux
# UNSUPPORTED: windows, target=arm
# XFAIL: broken-tool || slow
```

运行：

```bash
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --show-used-features tests/features.test
```

真实输出：

```text
broken-tool linux slow target=arm windows x86
```

注意几点：

- `&&`、`||` 不是 token，被丢掉。
- `target=arm` 整个保留；它不是 feature 名字，而是匹配表达式（match expression）。
- `*` 被显式排除，否则每个 `XFAIL: *` 都会污染结果。
- 它会把**所有** `XFAIL/REQUIRES/UNSUPPORTED` 行重新解析一遍；如果文件里有重复 `ALLOW_RETRIES:` 这种**别的**语法错误，`--show-used-features` 也会连带失败。真实复现：在一个含两条 `ALLOW_RETRIES:` 的目录上运行，lit 直接抛 `ValueError: Test has more than one ALLOW_RETRIES lines`，而不是打印 feature 列表。
- `main.py` 在收集 feature 时跳过 `gtest_json_file` 非空的测试（GoogleTest 虚拟测试），因为那些不是真实源文件。

它就是 Ch 13 里 `--show-used-features` 的底层实现：发现时每个测试已解析过一次，展示时为了拿 token 又解析一次；七万个测试上这是额外 I/O，所以只在显式传 flag 时发生。

## 19.6 一次完整的解析：把碎片拼起来

假设文件：

```text
# DEFINE: %{flag} = -O2
# RUN: cc %{flag} %s -o %t \
# RUN:   && %t
# XFAIL: windows
# END.
# RUN: echo this-is-ignored
```

解析结果：

```text
script = [
  SubstDirective(DEFINE:, name="%{flag}", value="-O2"),
  CommandDirective(RUN:, command="%dbg(RUN: at line 2) cc %{flag} %s -o %t   && %t"),
]
xfails = ["windows"]; requires = []; unsupported = []; allowed_retries = None
```

三点要注意：`DEFINE` 与 `RUN` 同列表，`SubstDirective` 的位置就是替换生效的位置；续行合并后 `%dbg(...)` 只在第一条命令前，行号是 `2` 而不是 `3`；`END.` 后面的 `RUN:` 从未进入列表。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「lit 只扫描 `#` 注释里的指令」 | lit 扫描的是字节子串，`#` 只是惯例。`echo "RUN: x"` 独占一行时，引号内的 `RUN:` 会被当成关键字，导致 shell parser error / `UNRESOLVED` |
| 「一行里所有 `RUN:` 都会被解析」 | 正则 `(keyword)(.*)\n` 贪婪吃掉行尾；一行里只有**第一个**关键字能开启匹配，后面的同 line 关键字被吞掉 |
| 「`END.` 是注释，后面的内容一律忽略」 | `END.` 是 TAG parser，只有其后为空时才触发 `break`；`# END. blah` 不终止。它终止扫描，不影响已经收集的指令 |
| 「重复 `XFAIL:` 会覆盖前一条」 | `BOOLEAN_EXPR` 是 append，多条是 OR。真正禁止重复的是 `ALLOW_RETRIES:`，出现两条直接 `ValueError` |
| 「`require_script=False` 表示不检查语法」 | 它只跳过「必须有一条 RUN」的检查。`XFAIL`/`REQUIRES` 的布尔表达式语法、未终止的 `\`、重复 `ALLOW_RETRIES` 仍然报错 |
| 「`XRUN:` 不会被识别成 `RUN:`」 | 正则没有单词边界，`RUN:` 作为子串就匹配。自定义关键字要么避开内置关键字子串，要么用 `END.` 截断 |
| 「文件最后一行没有换行会漏掉」 | 源码会补一个 `\n`；漏掉的是「补丁之前」的实现，不是当前 lit |
| 「`\r` 会留在命令里」 | `ln.decode(...).rstrip("\r")` 会去掉行尾回车；但 `\r` 出现在行中间不会处理 |

## 重点整理

1. `parseIntegratedTestScriptCommands()` 是**字节级、全文件**扫描；行号靠匹配位置之间的换行计数得到，不依赖 `splitlines()`。
2. 关键字正则没有锚点，`#` 不是语法；`XRUN:` 也会匹配 `RUN:`。但同一行只有第一个关键字有效。
3. 八个内置 parser：`RUN:`(COMMAND)、`XFAIL`/`REQUIRES`/`UNSUPPORTED`(BOOLEAN_EXPR)、`ALLOW_RETRIES`(INTEGER)、`END.`(TAG)、`DEFINE`/`REDEFINE`(Subst)。
4. `DEFINE`/`REDEFINE` 与 `RUN` 共享同一个 list，替换顺序就是文件出现顺序。
5. 续行要求**同关键字**且上一行以 `\` 结尾；跨关键字不会续行，最后会报 unterminated。
6. `parseIntegratedTestScript()` 把 `ValueError` 统一转成 `UNRESOLVED`；feature 不满足转成 `UNSUPPORTED`；`exclude_xfail` 命中转成 `EXCLUDED`。
7. `require_script=False` 只放宽「没有 RUN」这一条，不关闭任何语法检查。
8. `getUsedFeatures()` 重新解析源文件，抽出布尔表达式里的 token；`*` 被排除，`&&`/`||` 不是 token。

## 自我检核

- [ ] 不看源码，画出 `parseIntegratedTestScriptCommands()` 的输入、输出和行号计算方式；解释为什么它用二进制模式打开文件。
- [ ] 给一个包含两个 `RUN:` 的文件，说明哪个会被匹配、哪个会被 `.*` 吞掉；再构造一个会触发 `UNRESOLVED` 的字符串场景。
- [ ] 解释 `# RUN: a \` 后面接 `# REQUIRES: x86` 会发生什么，错误信息里的行号指向哪一行。
- [ ] 说出 `END.` 触发 `break` 的精确条件，并解释 `# END. trailing` 为什么不终止。
- [ ] 为什么重复 `XFAIL:` 合法而重复 `ALLOW_RETRIES:` 非法？两者分别用哪种 `ParserKind`，值是什么类型？
- [ ] 用 `require_script=False` 解析一个没有 `RUN:` 但有非法 `XFAIL:` 的文件，预测返回值；再解释 `--show-used-features` 为什么也会被无关的 `ALLOW_RETRIES` 错误拖垮。

## 延伸阅读

- **`llvm/utils/lit/lit/TestRunner.py:1283-1330`**
  - 读哪里：`parseIntegratedTestScriptCommands()` 全文。
  - 能学到什么：字节扫描、行号计算、CRLF 处理；这是所有 directive 的共同入口。
  - 前提知识：Python 正则 `finditer` 与 `bytes` 基础。
- **`llvm/utils/lit/lit/TestRunner.py:1813-2103`**
  - 读哪里：`ParserKind`、`IntegratedTestKeywordParser`、`_parseKeywords()`。
  - 能学到什么：每种 kind 的解析规则、`%(line)` 展开、续行合并、重复检查。
  - 前提知识：本章 19.3；先读 `CommandDirective` 与 `SubstDirective` 的类定义（第 1445–1640 行）。
- **`llvm/utils/lit/tests/unit/TestRunner.py` 与 `tests/Inputs/allow-retries/`**
  - 读哪里：lit 自测里对 `_parseKeywords()`、`ALLOW_RETRIES`、`DEFINE` 的直接断言。
  - 能学到什么：官方如何测试解析器；可以照抄成你自己的回归测试。
  - 前提知识：会跑 `python3 -m unittest` 或 lit 自测（Ch 25）。
- **`llvm/utils/lit/tests/shtest-run-at-line.py` 与 `tests/Inputs/shtest-run-at-line/`**
  - 读哪里：`%(line)`、`%dbg`、多行 RUN 的行号断言。
  - 能学到什么：行号在错误信息里如何呈现，以及 `-a` 输出为什么带 `# RUN: at line N`。
  - 前提知识：本章 19.3.1。

下一章我们把这份 `script` 列表送进执行器：临时路径怎么拼、替换按什么顺序做、进程怎么被创建、失败怎么变成 `FAIL/TIMEOUT/FLAKYPASS`。

→ [Ch 20 TestRunner II：脚本如何被执行](./20-testrunner-execution.md)
