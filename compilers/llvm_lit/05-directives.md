# Ch 5 — Directives：RUN、REQUIRES、UNSUPPORTED、XFAIL、ALLOW_RETRIES

> **目标**：把 ShTest 的 directive 语言一次学完：lit 怎样在不认识注释的情况下找到关键字、行继续与 `END.` 怎样工作、`REQUIRES`/`UNSUPPORTED`/`XFAIL` 的布尔表达式到底在判断什么、`ALLOW_RETRIES` 怎样产生 `FLAKYPASS`，以及 `DEFINE`/`REDEFINE` 如何配合 `%(line)`。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。实验 suite 在 `/tmp/lit-ch4lab.XzgqPC`；`config.available_features = {"foo", "bar", "baz"}`。所有输出均为本机实跑。

---

## 5.1 lit 不认识注释，它只认识正则

先从最容易建立错误直觉的地方开始。打开 `TestRunner.py` 第 1283 行：

```python
def parseIntegratedTestScriptCommands(source_path, keywords):
    ...
    keywords_re = re.compile(
        b"(%s)(.*)\n" % (b"|".join(re.escape(k.encode("utf-8")) for k in keywords),)
    )
    f = open(source_path, "rb")
    data = f.read()
    ...
    for match in keywords_re.finditer(data):
        match_position = match.start()
        line_number += data.count(b"\n", last_match_position, match_position)
```

三个事实直接从代码里读出来：

1. 文件以 **bytes** 读取，整个文件一次性扫完。
2. 正则是 `(关键字)(.*)\n`，关键字包括 `RUN:`、`XFAIL:`、`REQUIRES:`、`UNSUPPORTED:`、`ALLOW_RETRIES:`、`END.`、`DEFINE:`、`REDEFINE:`。
3. 它不检查这一行是不是注释。`#`、`//`、`;` 都只是普通字符。

所以下面这个文件能跑：

```text
RUN: echo bare-directive
```

真实输出（节选）：

```text
PASS: Ch4Lab :: tests/bare-run.test (1 of 2)
--
# RUN: at line 1
echo bare-directive
# executed command: echo bare-directive
# | bare-directive
```

`#` 只是 LLVM 测试文件的惯例，让 directive 看起来像注释。它不是 lit 的语法前提。

### 字符串里的 `RUN:` 也会被解析

下面这个文件的第一行是合法的 `RUN:`，第二行是 Python 风格的字符串，里面又出现了一个 `RUN:`：

```text
# RUN: echo before
echo "RUN: echo injected"
```

真实结果（节选）：

```text
UNRESOLVED: Ch4Lab :: tests/string-run2.test (2 of 2)
******************** TEST 'Ch4Lab :: tests/string-run2.test' FAILED ********************
Exit Code: 1

Command Output (stdout):
--
# shell parser error on RUN: at line 2: echo injected"

--
```

为什么？正则从第 2 行的 `RUN:` 开始匹配，捕获到行尾的内容 ` echo injected"`。lit 把它当成一条命令，引号不配对，`ShParser` 抛 `ScriptFatal`，最终 `UNRESOLVED`（`_runShTest()` 第 2205 行把 `ScriptFatal` 映射成 `UNRESOLVED`）。

一个细节：如果一行本身以 `# RUN:` 开头，`(.*)\n` 会贪婪吃到行尾，同一个行内后面的 `RUN:` 不会成为第二条 directive。例如：

```text
# RUN: echo "RUN: echo x"
```

这里真正的命令是 `echo "RUN: echo x"`，内层 `RUN:` 只是字符串内容。危险的是那些**看起来不是 directive、但包含关键字**的行，比如上面第二行没有 `# RUN:` 前缀的字符串。

> 结论：写测试时不要把 `RUN:`/`XFAIL:` 等关键字放进普通说明文字或字符串。lit 不会理解你的意图。

## 5.2 ParserKind：每种 directive 的解析类型

`parseIntegratedTestScriptCommands()` 只负责「切出行号和文本」，真正的解析在 `_parseKeywords()`（第 2019 行）。它给每个关键字配一个 `IntegratedTestKeywordParser`，类型由 `ParserKind`（第 1813 行）决定：

| `ParserKind` | 允许的后缀 | 语义 | 内置关键字 |
|---|---|---|---|
| `TAG` | `.` | 无值标记；`END.` 用来提前结束扫描 | `END.` |
| `COMMAND` | `:` | 一条 shell 命令，可续行 | `RUN:` |
| `LIST` | `:` | 逗号分隔列表 | 无内置，供自定义 parser |
| `SPACE_LIST` | `:` | 空格分隔列表 | 无内置，供自定义 parser |
| `BOOLEAN_EXPR` | `:` | 逗号分隔的布尔表达式 | `XFAIL:`、`REQUIRES:`、`UNSUPPORTED:` |
| `INTEGER` | `:` | 单个整数 | `ALLOW_RETRIES:` |
| `CUSTOM` | `:` 或 `.` | 调用外部 parser | 由自定义 format 使用 |
| `DEFINE` | `:` | 新增替换定义 | `DEFINE:` |
| `REDEFINE` | `:` | 重定义已有替换 | `REDEFINE:` |

内置 parser 的注册在 `_parseKeywords()` 第 2033–2041 行：

```python
IntegratedTestKeywordParser("RUN:", ParserKind.COMMAND, initial_value=script),
IntegratedTestKeywordParser("XFAIL:", ParserKind.BOOLEAN_EXPR),
IntegratedTestKeywordParser("REQUIRES:", ParserKind.BOOLEAN_EXPR),
IntegratedTestKeywordParser("UNSUPPORTED:", ParserKind.BOOLEAN_EXPR),
IntegratedTestKeywordParser("ALLOW_RETRIES:", ParserKind.INTEGER),
IntegratedTestKeywordParser("END.", ParserKind.TAG),
IntegratedTestKeywordParser("DEFINE:", ParserKind.DEFINE, initial_value=script),
IntegratedTestKeywordParser("REDEFINE:", ParserKind.REDEFINE, initial_value=script),
```

几个直接后果：

- `END.` 必须以 `.` 结尾，`RUN`/`XFAIL` 等必须以 `:` 结尾；写成 `RUN echo` 不会被识别。
- `RUN:` 的命令列表和 `DEFINE:`/`REDEFINE:` 共享同一个 `script` 列表，所以替换定义和命令保持源文件顺序。
- `_parseKeywords()` 在没有 `RUN:` 且 `require_script=True` 时抛 `Test has no 'RUN:' line`（第 2070 行）。
- 续行没有结束时抛 `Test has unterminated 'RUN:' directive (with '\')`。
- `ALLOW_RETRIES:` 出现多条时抛 `Test has more than one ALLOW_RETRIES lines`（第 2097–2100 行）。

## 5.3 行继续、`END.` 与同类 directive 的合并

### 行继续 `\`

`CommandDirective.add_continuation()`（第 1516 行）只在上一行以 `\` 结尾、且下一行关键字相同的时候合并：

```text
# RUN: echo one \
# RUN:   two \
# RUN:   three
# RUN: echo four
```

真实输出（节选）：

```text
# RUN: at line 1
echo one    two    three
# executed command: echo one two three
# | one two three
# RUN: at line 4
echo four
# executed command: echo four
```

注意 `# RUN: at line 1` 只报起始行。续行属于同一条 `CommandDirective`；如果类型不同（比如 `RUN:` 续到 `DEFINE:`），`_parseKeywords()` 会认为 `RUN:` 没有结束并报 UNRESOLVED。

### `END.` 提前结束扫描

`END.` 的 parser 是 `_handleTag()`：

```python
@staticmethod
def _handleTag(line_number, line, output):
    return not line.strip() or output
```

第一次遇到 `END.` 时返回 `True`，`_parseKeywords()` 在 `command_type == "END." and parser.getValue() is True` 时 `break`。实验文件：

```text
# RUN: echo before-end
END.
# RUN: echo this-should-not-run
```

真实结果（节选）：

```text
PASS: Ch4Lab :: tests/end.test (2 of 3)
--
# RUN: at line 1
echo before-end
# executed command: echo before-end
# | before-end
```

`END.` 之后的 `RUN:` 根本没被收集。它适合「文件后半段是给别的工具看的」场景，比如 lit 自测里把大量 CHECK 放在 `END.` 之后。

### 同类 directive 的合并规则

| 类型 | 合并方式 | 例子 |
|---|---|---|
| `RUN:` | 按出现顺序追加成命令列表 | 多条 `RUN:` 用 `&&` 串起来 |
| `REQUIRES:` | 追加布尔表达式；**全部**必须为真 | 两条 `REQUIRES: foo` / `REQUIRES: bar` 都要满足 |
| `UNSUPPORTED:` | 追加布尔表达式；**任一**为真就跳过 | `UNSUPPORTED: missing, baz` 因 `baz` 成立而跳过 |
| `XFAIL:` | 追加布尔表达式；**任一**为真就预期失败 | `XFAIL: missing, baz` 因 `baz` 成立而预期失败 |
| `ALLOW_RETRIES:` | 只允许一条 | 两条直接 UNRESOLVED |
| `DEFINE:`/`REDEFINE:` | 按源文件顺序插入 `script` | 后面的命令能看到前面的定义 |

两条 `REQUIRES:` 的真实结果（节选）：

```text
PASS: Ch4Lab :: tests/requires-two.test (2 of 2)
--
# RUN: at line 3
echo two-requires
# executed command: echo two-requires
```

把第二条改成 `missing`：

```text
UNSUPPORTED: Ch4Lab :: tests/requires-two-missing.test (1 of 2)
Test requires the following unavailable features: missing
```

注意消息里只列没满足的那一条，不是整张表。

## 5.4 `REQUIRES`、`UNSUPPORTED`、`XFAIL`：同一套布尔语言

三者的表达式语法来自 `BooleanExpression.py` 开头：

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

可用的 token：

| 写法 | 含义 |
|---|---|
| `foo` | feature 集合里有 `foo` |
| `foo && bar` | 两者都真 |
| `foo \|\| bar` | 任一为真 |
| `!foo` | `foo` 不在 feature 集合里 |
| `(foo \|\| bar) && baz` | 括号改变优先级 |
| `{{^x86}}` | 用 Python 正则匹配任意 feature 名（`re.search`） |
| `*` | 只对 `XFAIL:` 有特殊含义，表示「总是预期失败」 |

`REQUIRES:` 的语义是「全部表达式为真才跑」。`Test.getMissingRequiredFeatures()`（`Test.py` 第 410 行）遍历 `self.requires`，任一表达式为假就返回缺失列表，`parseIntegratedTestScript()` 把它变成 `UNSUPPORTED` 结果。

`UNSUPPORTED:` 的语义相反：`Test.getUnsupportedFeatures()`（第 421 行）遍历 `self.unsupported`，任一表达式为真就返回命中列表，测试被跳过。

`XFAIL:` 是「任一表达式为真就预期失败」，然后由 `Test.setResult()`（第 298–315 行）反转结果：

```python
if expected_to_fail:
    if result.code is PASS:
        result.code = XPASS
    elif result.code is FAIL:
        result.code = XFAIL
```

### 真实实验：REQUIRES、UNSUPPORTED、XFAIL

`REQUIRES: missing`：

```text
UNSUPPORTED: Ch4Lab :: tests/requires-missing.test (1 of 2)
Test requires the following unavailable features: missing
```

`UNSUPPORTED: foo`（`foo` 是可用 feature）：

```text
UNSUPPORTED: Ch4Lab :: tests/unsupported.test (2 of 5)
Test does not support the following features and/or targets: foo
```

`UNSUPPORTED: missing` 则照常执行，真实输出是 `PASS: Ch4Lab :: tests/unsupported-missing.test`，并打印 `echo ran-anyway` 的 stdout。

正则在 feature 上的真实效果：

```text
# REQUIRES: {{^ba.$}}
```

`bar`、`baz` 都匹配 `^ba.$`，所以 PASS；改成 `{{^qux$}}` 就 UNSUPPORTED：

```text
Test requires the following unavailable features: {{^qux$}}
```

`!baz` 在 `baz` 可用时为假，因此 UNSUPPORTED：

```text
Test requires the following unavailable features: !baz
```

注意 `*` 只对 `XFAIL:` 特殊。`REQUIRES: *` 会让布尔解析器报 `couldn't parse text: '*'`，最终 UNRESOLVED；不要把它当通配符。

### XFAIL 不是跳过，XPASS 是失败

`# XFAIL: foo` + `# RUN: false`：

```text
XFAIL: Ch4Lab :: tests/xfail-fails.test (4 of 5)
Exit Code: 1
--
# RUN: at line 2
false
# executed command: false
# error: command failed with exit status: 1
```

测试**确实执行了**，只是失败被预期吞掉，结果码从 `FAIL` 反转成 `XFAIL`。`XFAIL` 不是 `UNSUPPORTED`：加 `-a` 仍然能看到 `# executed command`，它只是把失败重新解释成预期结果。

`# XFAIL: foo` + `# RUN: true` 则是：

```text
XPASS: Ch4Lab :: tests/xfail-passes.test (5 of 5)
******************** TEST 'Ch4Lab :: tests/xfail-passes.test' FAILED ********************
```

`XPASS` 在 `Test.py` 第 57 行定义为 failure：

```python
XPASS = ResultCode("XPASS", "Unexpectedly Passed", True)
```

`# XFAIL: *` 同理：只要测试通过就 XPASS：

```text
Unexpectedly Passed Tests (2):
  Ch4Lab :: tests/xfail-passes.test
  Ch4Lab :: tests/xfail-star.test
```

### `--exclude-xfail`：把 XFAIL 变成 EXCLUDED

对两个 XFAIL 测试加 `--exclude-xfail`：

```text
EXCLUDED: Ch4Lab :: tests/xfail-passes.test (1 of 2)
excluding XFAIL tests
EXCLUDED: Ch4Lab :: tests/xfail-fails.test (2 of 2)
excluding XFAIL tests

Total Discovered Tests: 2
  Excluded: 2 (100.00%)
```

实现位置在 `parseIntegratedTestScript()` 第 2128–2129 行：

```python
if test.exclude_xfail and test.isExpectedToFail():
    return lit.Test.Result(Test.EXCLUDED, "excluding XFAIL tests")
```

它在 `REQUIRES`/`UNSUPPORTED` 检查之前发生。被 EXCLUDED 的测试不执行，也不算失败；lit 退出码是 0。

## 5.5 `ALLOW_RETRIES`：只有 FAIL 会重试

`# ALLOW_RETRIES: N` 表示最多再跑 N 次，总尝试次数是 `N + 1`。写一个第一次失败、第二次成功的测试：

```text
# ALLOW_RETRIES: 1
# RUN: python3 -c "import os,sys; p='%t.counter'; n=int(open(p).read()) if os.path.exists(p) else 0; open(p,'w').write(str(n+1)); sys.exit(0 if n else 1)"
```

真实结果（节选；复现前先清掉 `Output/`，counter 文件会保留）：

```text
FLAKYPASS: Ch4Lab :: tests/flaky.test (1 of 2, 2 of 2 attempts)
Exit Code: 0
```

`FLAKYPASS` 是「重试后通过」的独立结果码（`Test.py` 第 50 行），不计为失败，但摘要里单独统计。实现逻辑在 `_runShTest()`：

```python
for i in range(attempts):
    res = runOnce(execdir)
    ...
    if status != Test.FAIL:
        break
if i > 0 and status == Test.PASS:
    ...
    status = Test.FLAKYPASS
```

关键点：只有 `FAIL` 会触发下一次尝试；`TIMEOUT`、`UNRESOLVED` 不会。因为循环条件是 `status != Test.FAIL: break`，而 `runOnce()` 只在超时信息为空时才把非 0 映射成 `FAIL`。

多条 `ALLOW_RETRIES:` 的真实结果（节选）：

```text
UNRESOLVED: Ch4Lab :: tests/retries-twice.test (2 of 2)
******************** TEST 'Ch4Lab :: tests/retries-twice.test' FAILED ********************
Test has more than one ALLOW_RETRIES lines
```

`config.test_retry_attempts` 可以给整个 suite 设默认重试次数；文件里的 `ALLOW_RETRIES:` 会覆盖它（lit 自测 `allow-retries.py` 明确检查了这一点）。

## 5.6 `DEFINE` / `REDEFINE` 与 `%(line)`

`DEFINE:` 新增一个替换，`REDEFINE:` 修改已有的替换。它们不是 shell 变量，而是**替换表里的条目**。实验：

```text
# DEFINE: %{greet} = hello
# RUN: echo "%{greet} at line %(line)"
# REDEFINE: %{greet} = bye
# RUN: echo "%{greet} at line %(line)"
# RUN: echo "minus1=%(line-1) plus2=%(line + 2)"
```

真实输出（节选）：

```text
# RUN: at line 2
echo "hello at line 2"
# executed command: echo 'hello at line 2'
# | hello at line 2
# RUN: at line 4
echo "bye at line 4"
# executed command: echo 'bye at line 4'
# | bye at line 4
# RUN: at line 5
echo "minus1=4 plus2=7"
# executed command: echo 'minus1=4 plus2=7'
# | minus1=4 plus2=7
```

`%(line)` 的替换发生在 `IntegratedTestKeywordParser._substituteLineNumbers()`（第 1930 行），支持：

- `%(line)` → 当前 directive 起始行号；
- `%(line+N)` → 行号加 N；
- `%(line-N)` → 行号减 N；
- `%(line + 2)` 这样的空格也接受。

它只在 `_handleCommand()`（`RUN:`）和 `_handleSubst()`（`DEFINE:`/`REDEFINE:`）里调用；`REQUIRES:`、`UNSUPPORTED:`、`XFAIL:` 不处理 `%(line)`。

### 名字规则与作用域

`SubstDirective._parse_body()`（第 1567 行）要求名字匹配：

```text
%{[_a-zA-Z][-_:0-9a-zA-Z]*}
```

也就是必须以 `%{` 开头、`}` 结尾，里面首字符是字母或下划线，其余只能是字母、数字、`-`、`_`、`:`。值取第一个 `=` 之后的内容并 strip。

`adjust_substitutions()`（第 1607 行）的规则：

- `DEFINE:` 把 `(name, value)` **插到列表最前面**；如果已有 pattern 包含这个名字，报错。
- `REDEFINE:` 必须找到唯一一个已有条目，且 pattern 必须完全等于 name；否则报错。

真实错误：

```text
ValueError: Substitution whose pattern contains '%{x}' is already defined before 'DEFINE:' directive at line 2
ValueError: No substitution for '%{nope}' is defined before 'REDEFINE:' directive at line 1
```

两者都是 UNRESOLVED。

作用域只限当前测试。`define-scope-a.test` 定义 `%{only}` 并输出 `visible-in-a`；`define-scope-b.test` 直接使用 `%{only}`，真实 stdout 是字面量 `%{only}`。b 里没有被替换。因为 `executeShTest()` 每个测试都重新构造 `substitutions` 列表，`DEFINE` 只改这份副本，不影响其他测试，也不回写 `config.substitutions`。

## 5.7 判定顺序：先跳过，再执行，最后反转

把 `executeShTest()` 和 `parseIntegratedTestScript()` 的顺序串起来：

```text
executeShTest()
  │
  ├─ test.config.unsupported ? ──→ UNSUPPORTED
  ├─ parseIntegratedTestScript()
  │    ├─ _parseKeywords()            语法错误 / 没有 RUN → UNRESOLVED
  │    ├─ XFAIL 条件
  │    ├─ --exclude-xfail ?            → EXCLUDED
  │    ├─ REQUIRES 条件
  │    ├─ 缺失 feature ?               → UNSUPPORTED
  │    ├─ UNSUPPORTED 条件命中 ?       → UNSUPPORTED
  │    ├─ limit_to_features 不满足 ?   → UNSUPPORTED
  │    └─ 返回脚本
  ├─ 替换、执行                        → PASS / FAIL / TIMEOUT
  └─ Test.setResult()                  → XFAIL / XPASS 反转
```

这个顺序解释了三件事：

1. `REQUIRES` 写错不是 FAIL，是 UNRESOLVED——错误发生在 `_parseKeywords()`，还没进入执行。
2. `REQUIRES` 缺 feature 不是 FAIL，是 UNSUPPORTED，lit 退出码可能是 0。
3. `XFAIL` 在最后才反转，所以它必须先真的执行；`XFAIL` 不是「跳过」。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`RUN:` 必须写在 `#` 后面」 | lit 用 bytes 正则扫整个文件，注释符号不是语法；`RUN: echo hi` 单独一行也能跑。 |
| 「字符串里的 `RUN:` 不会被解析」 | 会。`echo "RUN: echo injected"` 会被当成 `RUN:` 命令 `echo injected"`，引号不配对 → UNRESOLVED。 |
| 「`REQUIRES` 写错只是跳过」 | 布尔语法错误是 UNRESOLVED；缺 feature 才是 UNSUPPORTED。前者是配置错误，后者是环境不满足。 |
| 「`XFAIL` 等于跳过」 | `XFAIL` 会执行测试并把 FAIL 反转成预期失败；通过则变 XPASS，而 XPASS 是 failure。 |
| 「多条 `ALLOW_RETRIES` 取最大值」 | 只允许一条；多条直接 `Test has more than one ALLOW_RETRIES lines` → UNRESOLVED。 |
| 「`%(line)` 在哪里都能用」 | 只在 `RUN:`/`DEFINE:`/`REDEFINE:` 里由 `_substituteLineNumbers()` 处理；`CHECK:` 里的 `%(line)` 是 FileCheck 的事。 |
| 「`DEFINE` 会改变全局替换表」 | 只改当前测试的副本；别的测试看不到，`config.substitutions` 也不变。 |

## 重点整理

- `parseIntegratedTestScriptCommands()` 是 bytes 正则扫描，不认注释；关键字出现在字符串里也会被切出来。
- `ParserKind` 决定后缀和解析方式：`TAG` 用 `.`，其余内置类型用 `:`；`RUN` 是 `COMMAND`，三个条件 directive 是 `BOOLEAN_EXPR`，`ALLOW_RETRIES` 是 `INTEGER`。
- `\` 续行只在关键字相同且上一行以 `\` 结尾时合并；`END.` 之后的内容不再扫描。
- `REQUIRES:` 全部为真才跑；`UNSUPPORTED:` 任一为真就跳过；`XFAIL:` 任一为真就预期失败。
- 布尔表达式支持 `&&`、`||`、`!`、括号、`{{regex}}`；`*` 只对 `XFAIL:` 特殊。
- `XFAIL` 不是跳过；`XPASS` 是 failure；`--exclude-xfail` 在解析阶段产生 `EXCLUDED`。
- `ALLOW_RETRIES: N` 只重试 `FAIL`，总尝试 `N+1`；重试成功是 `FLAKYPASS`，多条 directive 直接 UNRESOLVED。
- `DEFINE:` 插到替换表最前，`REDEFINE:` 必须命中已有条目；`%(line)` 只在命令和替换 directive 里生效。

## 自我检核

不看笔记，试着回答：

- [ ] 为什么 `echo "RUN: echo injected"` 会让测试变 UNRESOLVED？lit 实际把哪一段当成了命令？
- [ ] `REQUIRES: foo, bar` 与 `REQUIRES: foo && bar` 在语义上一样吗？`UNSUPPORTED` 的逗号又是什么语义？
- [ ] 一个测试标了 `XFAIL: foo`，但实际通过了，结果码是什么？为什么这算失败？
- [ ] `ALLOW_RETRIES: 2` 最多跑几次？`TIMEOUT` 会重试吗？为什么？
- [ ] `DEFINE` 和 `REDEFINE` 对替换表的修改为什么不会影响同 suite 的另一个测试？
- [ ] `%(line+1)` 在 `RUN:` 和 `REQUIRES:` 里分别会发生什么？为什么？

## 延伸阅读

- **[LLVM Testing Infrastructure Guide — Constraining test execution](https://llvm.org/docs/TestingGuide.html#constraining-test-execution)**
  - 读什么：`REQUIRES`、`UNSUPPORTED`、`XFAIL`、`XPASS` 相关段落。
  - 能学到：LLVM 官方对这三个 directive 的推荐用法；对照本章的布尔表达式与反转逻辑。
  - 前提：先读本章 5.4，知道 `XFAIL` 不是跳过。
- **[`llvm/utils/lit/lit/BooleanExpression.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/BooleanExpression.py)**
  - 读什么：文件开头的 grammar 注释与 `evaluate()`；也可以跑 `python3 -m lit.BooleanExpression` 看自测。
  - 能学到：`&&`/`||`/`!`/`{{regex}}` 的精确优先级和 token 规则。
  - 前提：会一点正则；不需要懂 lit 的调度。
- **[`llvm/utils/lit/tests/shtest-keyword-parse-errors.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/shtest-keyword-parse-errors.py)**
  - 读什么：整个文件，只有几十行。
  - 能学到：lit 官方对「没有 RUN」「多条 ALLOW_RETRIES」「未结束续行」的错误信息规格。
  - 前提：能读 FileCheck 的 `CHECK`。
- **[`llvm/utils/lit/tests/allow-retries.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/allow-retries.py)**
  - 读什么：前 60 行，尤其是 `Passed With Retry` 与 4 次尝试的输出断言。
  - 能学到：`FLAKYPASS` 的显示格式、`ALLOW_RETRIES` 覆盖 `test_retry_attempts` 的行为。
  - 前提：读过本章 5.5。

> 下一章把替换系统完整展开：`getDefaultSubstitutions()` 到底注册了哪些 pattern、顺序为什么重要、`%%` 与 `%if` 怎样工作，以及 `ToolSubst("opt")` 为什么会把 `%opt` 换坏。

→ [Ch 6 替换系统全景](./06-substitutions.md)
