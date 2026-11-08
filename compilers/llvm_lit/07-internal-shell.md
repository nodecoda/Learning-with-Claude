# Ch 7 — lit 内置 shell：词法、管道、重定向

> **目标**：能看着一条 `RUN:` 行，准确说出 lit 会把它切成哪些 token、展开哪个 glob、在哪个位置重定向、以谁的退出码收尾；并且能一眼认出「这是 bash 写法，lit 不支持」。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`。本章所有输出都在本机 `/tmp` 的临时 suite 里真实跑出；工具用 `/home/ubuntu/llvm/builds/pr-lane/bin/{opt,FileCheck,not,count,split-file}`。

---

## 7.1 先纠正一个致命直觉：这里没有 bash

很多人第一次读 lit 测试，会把 `RUN:` 行当成 shell 脚本：

```text
# RUN: export FOO=bar
# RUN: echo $FOO
```

在 bash 里这打印 `bar`。在 lit 的内置 shell（internal shell）里，它打印：

```text
# RUN: at line 2
echo $FOO
# executed command: echo '$FOO'
# .---command stdout------------
# | $FOO
# `-----------------------------
```

原因不是 `export` 没生效，而是**根本没有变量展开这一步**。lit 的 shell 是一个很小的、自己实现的解释器：

```
RUN: 行文本
   │
   ├─ ShLexer       把字符流切成 token（单词 / glob / 操作符 / 重定向）
   ├─ ShParser      把 token 组装成 Command / Pipeline / Seq 树
   └─ _executeShCmd 遍历这棵树：内置命令就地执行，外部命令走 subprocess.Popen
```

它没有 `eval`，没有变量表，没有 `if`，没有命令替换。它只做三件事：**分词、拼管道、执行并收集退出码**。

> 从 LLVM 23 起 `execute_external=True` 已废弃，LLVM 24 默认就是内置 shell。你看到的 `RUN:` 行为，99% 是本章这套代码，而不是 `/bin/sh`。

## 7.2 源码地图：四个文件就够

| 文件 | 关键对象 | 行号（本 commit） | 职责 |
|---|---|---|---|
| `llvm/utils/lit/lit/ShUtil.py` | `ShLexer` | 7 | 字符 → token |
| 同上 | `ShParser` | 204 | token → 命令树 |
| `llvm/utils/lit/lit/ShCommands.py` | `Command` / `GlobItem` / `Pipeline` / `Seq` | 1 / 39 / 64 / 94 | 命令树的节点 |
| `llvm/utils/lit/lit/TestRunner.py` | `_executeShCmd` | 545 | 执行命令树 |
| 同上 | `inproc_builtins` / `pipeline_builtins` | 588 / 601 | 哪些命令不走进程 |
| 同上 | 退出码聚合 | 946–951 | pipeline 的最终退出码 |

`ShLexer.lex_one_token()`（161 行）先认单字符操作符，再退回 `lex_arg()`（158 行）。`lex_arg()` 有一条快速路径 `lex_arg_fast()`（31 行）：只要一个空白分隔的块里没有 `| & < > ' " ; \`，就直接当普通单词；有 `*` 或 `?` 才包成 `GlobItem`。慢路径 `lex_arg_slow()`（51 行）处理引号、转义和 fd 前缀。

## 7.3 词法：引号与反斜杠

先看一组真实输入与真实输出（`echo` 是内置命令）：

```text
# RUN: echo 'single  quoted'
# RUN: echo "double  quoted"
# RUN: echo a\ b
# RUN: echo "a\"b"
# RUN: echo 'a\b'
# RUN: echo "a\\b"
# RUN: echo 'it'\''s'
```

真实执行结果：

```text
# executed command: echo 'single  quoted'
# | single  quoted
# executed command: echo 'double  quoted'
# | double  quoted
# executed command: echo 'a b'
# | a b
# executed command: echo 'a"b'
# | a"b
# executed command: echo 'a\b'
# | a\b
# executed command: echo 'a\b'
# | a\b
# executed command: echo 'it'"'"'s'
# | it's
```

规则来自 `lex_arg_quoted()`（113 行）：

| 写法 | 结果 | 源码依据 |
|---|---|---|
| `'a\b'` | `a\b` | 单引号内所有字符原样保留，只有 `'` 能结束 |
| `"a\"b"` | `a"b` | 双引号内 `\"` → `"`，`\\` → `\` |
| `"a\\b"` | `a\b` | 同上 |
| `"a\qb"` | `a\qb` | 双引号内反斜杠只对 `"` 和 `\` 特殊，其它原样保留（`\$` 也特殊，见下） |
| `a\ b` | 一个参数 `a b` | 引号外 `\` 转义任意字符（`lex_arg_slow` 第 83–91 行） |
| `'it'\''s'` | `it's` | 先结束单引号，再转义一个 `'`，再开单引号 |

双引号内还有一个 LLDB 遗留特例：`"a\$b"` 会得到 `a$b`（`lex_arg_quoted` 第 121–123 行），因为 LLDB 的全局变量名以 `$` 开头。这不是变量展开，只是不让反斜杠留下。

> **注意**：`# executed command:` 里的引号是 lit 为了**显示**做的安全引用（`shlex.quote`），不是它真正看到的引号。第 7.4 节会看到，`ls *.txt` 会显示成 `ls '*.txt'`，但 glob 其实已经展开了。

## 7.4 glob：只认 `*` 和 `?`，而且展开成绝对路径

`GlobItem.resolve()`（`ShCommands.py` 第 52 行）直接调用 Python 的 `glob.glob()`，传入的是 `os.path.join(cwd, pattern)`，所以结果全是**绝对路径**：

```text
# RUN: mkdir -p %t.d
# RUN: cd %t.d
# RUN: touch a.txt b.txt c.log
# RUN: ls *.txt
# RUN: echo "*.txt"
# RUN: echo '*.txt'
# RUN: echo ?.txt | cat
```

真实输出（路径已按本机实际值展示）：

```text
# executed command: ls '*.txt'
# | /tmp/litlab.ZyXTya/Output/tests/Output/glob.test.tmp.d/a.txt
# | /tmp/litlab.ZyXTya/Output/tests/Output/glob.test.tmp.d/b.txt

# executed command: echo '*.txt'
# | *.txt

# executed command: echo '*.txt'
# | *.txt

# executed command: echo '?.txt'
# executed command: cat
# | /tmp/litlab.ZyXTya/Output/tests/Output/glob.test.tmp.d/a.txt /tmp/litlab.ZyXTya/Output/tests/Output/glob.test.tmp.d/b.txt
```

四个要点：

1. **无匹配时保留原样**：`resolve()` 在 `glob` 结果为空时返回 `[self.pattern]`，所以 `ls nomatch*.txt` 会真的把 `nomatch*.txt` 传给 `ls`，由 `ls` 报错。
2. **引号里的 glob 不展开**：`"*.txt"` 与 `'*.txt'` 都是普通字符串。
3. **引号内外混用会触发断言**：`lex_arg_slow()` 末尾有 `assert not (quoted_glob_char and unquoted_glob_char)`（第 109 行），例如 `a"*"b*` 属于未定义边界，别写。
4. **glob 只对「外部命令」和重定向展开**。内置 `echo` 不调用 `expand_glob_expressions`，拿到的是 `GlobItem` 对象，然后：

```text
# RUN: echo *.txt
```

实测结果是 **UNRESOLVED**，traceback 末尾是：

```text
TypeError: string argument expected, got 'GlobItem'
```

这不是你写错了测试语义，而是本版本内置 `echo` 的一个真实缺陷。想打印文件名，用 `ls`，或者把 `echo` 放进管道（管道里的 `echo` 会走外部 `/usr/bin/echo`，见 7.7）。

## 7.5 重定向：语法支持，但语义比 bash 窄

`lex_one_token()`（161 行）认识这些操作符：

| 写法 | token | `processRedirects` 是否实现 | 含义 |
|---|---|---|---|
| `>` | `(">",)` | 是 | stdout 覆盖写 |
| `>>` | `(">>",)` | 是 | stdout 追加 |
| `<` | `("<",)` | 是 | stdin 读 |
| `2>` | `(">", 2)` | 是 | stderr 覆盖写 |
| `2>>` | `(">>", 2)` | 是 | stderr 追加 |
| `>&`（无 fd） | `(">&",)` | 是 | stdout 和 stderr 一起写文件 |
| `&>` | `("&>",)` | 是 | 同上 |
| `n>&m` | `(">&", n)` | 是（仅 m ∈ `0/1/2`） | fd 复制 |
| `<&` | `("<&",)` | 否 | 解析得出，执行时报 Unsupported |
| `<<` | 见下 | 否 | **不是 heredoc** |

真实 PASS 样例：

```text
# RUN: echo hello > %t.out
# RUN: cat %t.out
# RUN: echo more >> %t.out
# RUN: cat %t.out
# RUN: cat < %t.out
# RUN: ls %t.does-not-exist 2> %t.err || true
# RUN: cat %t.err
# RUN: ls %t.does-not-exist >& %t.both || true
# RUN: cat %t.both
```

输出（保留关键行）：

```text
# executed command: cat /tmp/.../redirect.test.tmp.out
# | hello
# executed command: cat /tmp/.../redirect.test.tmp.out
# | hello
# | more
# executed command: cat
# | hello
# | more
# executed command: cat /tmp/.../redirect.test.tmp.err
# | ls: cannot access '/tmp/.../does-not-exist': No such file or directory
```

### 重定向在管道里的位置

`processRedirects()` 把每条命令的三个 fd 先初始化成 `(0,)(1,)(2,)`，再按 `cmd.redirects` 的顺序覆盖（`ShellEnvironment.py` 第 117–148 行）。所以**顺序有意义**：

```text
# RUN: sh -c 'echo out; echo err >&2' > %t.f 2>&1
# RUN: cat %t.f
# RUN: sh -c 'echo out; echo err >&2' 2>&1 > %t.g
# RUN: cat %t.g
```

真实结果：

```text
# 第一条：f 里同时有 out 和 err
# | out
# | err

# 第二条：err 进了 lit 的 stderr 捕获，g 里只有 out
# .---command stderr------------
# | err
# | out
```

第一条先把 stdout 指向文件，再把 stderr 复制到「当前 stdout」，所以两者都进文件；第二条先把 stderr 复制到「当前 stdout」（此时还是 lit 的管道），再把 stdout 改到文件，所以 err 留在外面。这与 bash 一致，但它是 lit 自己按顺序实现的。

### `2>` 与内置 `echo` 的冲突

`executeBuiltinEcho` 第 62–66 行明确拒绝 stdin/stderr 重定向：

```text
# RUN: echo hi 2> %t.err
```

真实输出：

```text
# | stdin and stderr redirects not supported for echo
# error: command failed with exit status: 127
```

注意退出码是 **127**，不是 1。`_executeShCmd` 抛 `InternalShellError`，`executeShCmd` 统一把它变成 127（`TestRunner.py` 第 172–190 行）。同理 `echo hi < file` 会直接抛 `FileNotFoundError`（如果文件不存在），整个测试变成 UNRESOLVED。

### `<<` 到底是什么

`lex_one_token()` 第 184–189 行写的是：

```python
if c == "<":
    if self.maybe_eat("&"):
        return ("<&",)
    if self.maybe_eat(">"):
        return ("<<",)
    return (c,)
```

注意第二个分支吃的是 `>`，所以 `("<<",)` 这个 token 名对应的是 `<>`（读写打开），**不是 heredoc**。真正的 `<<` 会被切成两个 `<`，解析器把第一个 `<` 当操作符、第二个 `<` 当文件名，最终在 `os.path.join(cwd, name)` 处炸掉：

```text
# RUN: cat << EOF
```

实测：

```text
TypeError: join() argument must be str, bytes, or os.PathLike object, not 'tuple'
```

结论：**lit 没有 heredoc**。要生成多行输入，用 `printf`、`echo >>` 多次追加，或 `split-file`（Ch 9）。

## 7.6 管道与 pipefail：默认「中间失败也算失败」

`parse_pipeline()`（256 行）把 `a | b | c` 变成 `Pipeline([a, b, c], negate=False, pipe_err=self.pipefail)`。`pipe_err` 直接来自 `config.pipefail`，默认是 `True`（`TestingConfig.py` 第 130 行）。

```text
# RUN: false | true
```

真实结果：

```text
-- Testing: 1 tests, 1 workers --
FAIL: LitLab :: tests/pipefail-on.test (1 of 1)
Exit Code: 1

# executed command: false
# error: command failed with exit status: 1
# executed command: true
```

聚合逻辑在 `TestRunner.py` 第 946–951 行：

```python
if cmd.pipe_err:
    # Take the last failing exit code from the pipeline.
    if not exitCode or res != 0:
        exitCode = res
else:
    exitCode = res
```

逐条解释：

- `pipefail=True`：遍历每条命令，**最后一个非 0 的退出码**成为 pipeline 的退出码。全部为 0 时是 0。
- `pipefail=False`：只保留最后一条命令的退出码，中间命令失败被忽略。

真实对照（同一份测试，只改 `config.pipefail`）：

| 命令 | `pipefail=True` | `pipefail=False` |
|---|---|---|
| `false \| true` | FAIL，Exit Code 1 | PASS，Exit Code 0 |
| `true \| false` | FAIL，Exit Code 1 | FAIL，Exit Code 1 |
| `true \| false \| true` | FAIL，Exit Code 1 | PASS |
| `sh -c 'exit 4' \| cat` | FAIL，Exit Code 4 | PASS |

`pipefail=False` 的 suite 用 `lit.local.cfg.py` 关掉：

```python
# tests/pipefail-off/lit.local.cfg.py
config.pipefail = False
```

**但注意**：即使 `pipefail=False` 让测试 PASS，trace 里仍然会打印 `# error: command failed with exit status: 1`。这是每条命令自己的结果注解，不是 lit 的最终判定。诊断时不要被它吓到，也不要忽略它——它正是「中间命令失败了」的唯一线索。

## 7.7 序列操作符：`;`、`&&`、`||`、`&`

`ShParser.parse()`（265 行）用一个循环把 pipeline 串成 `Seq`，注释里写着 `# FIXME: Operator precedence!!`——它**严格从左到右**建树，没有 bash 的优先级概念。

```text
# RUN: echo a ; echo b
# RUN: false ; echo after-semicolon
# RUN: false && echo not-printed ; echo after-and
# RUN: true || echo not-printed
# RUN: false || echo after-or
# RUN: echo x && false || echo recovered
```

真实输出（节选）：

```text
# | a
# | b
# | after-semicolon
# | after-and
# | after-or
# | x
# | recovered
```

语义在 `_executeShCmd` 第 551–572 行：

| 操作符 | 行为 | 返回码 |
|---|---|---|
| `;` | 先跑左边，**无条件**跑右边 | 右边的退出码 |
| `&&` | 左边为 0 才跑右边 | 左边失败则左边码；否则右边码 |
| `\|\|` | 左边非 0 才跑右边 | 左边成功则 0；否则右边码 |
| `&` | 解析器接受，执行器直接报错 | 见下 |

`&` 的执行路径是：

```python
if cmd.op == "&":
    raise InternalShellError(cmd, "unsupported shell operator: '&'")
```

本版本实测结果不是干净的 FAIL，而是 **UNRESOLVED**，traceback 末尾：

```text
AttributeError: 'Seq' object has no attribute 'args'
```

原因是 `InternalShellError` 携带的 `command` 是 `Seq` 节点，而 `executeScriptInternal` 在打印 trace 时假设它有 `.args`。这是一个真实的实现缺口，不是你的测试写错。记住结论即可：**`&` 后台执行不支持**。

### 与 bash 的差异

- **尾部操作符**：`echo a ;`、`echo a &&` 在 bash 里合法，在 lit 里是 `shell parser error` → UNRESOLVED。`parse()` 在取到操作符后会检查右边是否还有 token。
- **前导重定向**：`> f echo hi` 在 bash 里合法，lit 的 `parse_command()` 看到第一个 token 是元组就报 `syntax error near unexpected token`。
- **没有子 shell / 分组**：`( a ; b )` 里的 `(` 是普通字符，会被当成命令名。
- **`&&` 与 `;` 的优先级**：lit 一律左结合；bash 的 `;` 分隔 AND-OR 列表。大多数平铺写法结果相同，但不要依赖复杂混排。真正需要分组时，拆成多条 `RUN:`。

### `not` 与 `!`

内置 shell 在 `_executeShCmd` 第 642–654 行**特殊识别**命令前缀 `not` 和 `!`：

- 出现一次：执行后面的命令，退出码取反（0 ↔ 非 0）。
- 出现两次：再取反回来（`not_count % 2`）。
- `not --crash`：**不**在内部处理，会把 `not --crash ...` 原样交给外部 `not` 程序（需要 PATH 里有 `not`；LLVM suite 用 `| not` 的替换，见 Ch 9）。

真实结果：

```text
# RUN: not false        → PASS
# RUN: not true         → FAIL, Exit Code 1
# RUN: ! true           → FAIL, Exit Code 1
# RUN: not sh -c 'exit 1' | cat   → PASS
```

最后一条很能说明问题：`not` 只翻转它修饰的那一条命令，`cat` 仍然正常执行，pipeline 的退出码是 `not` 的 0。

## 7.8 不支持清单：用真实实验钉死

| bash 写法 | lit 内置 shell | 真实证据 |
|---|---|---|
| `$FOO` / `${FOO}` | 字面量 | `echo $FOO` → `$FOO`；`echo ${FOO}` → `${FOO}` |
| `$(date)` | 字面量 | `echo $(date)` → `$(date)` |
| `` `date` `` | 字面量 | 输出 `` `date` `` |
| `$?` | 不是退出码 | `echo $?` → **UNRESOLVED**：`?` 被当成 glob，内置 echo 收到 `GlobItem` |
| `if/for/while` | 当外部命令 | `if true; then ...` → `'if': command not found`，127 |
| `set -e` | 当外部命令 | `'set': command not found`，127 |
| `FOO=bar cmd` | 当外部命令 | `'FOO=bar': command not found`，127 |
| `export A=1 B=2` | 只接受一个参数 | `'export' supports only one argument`，127 |
| heredoc `<<` | 不支持 | `TypeError: join() ... not 'tuple'` |
| `&` 后台 | 不支持 | `unsupported shell operator: '&'` → UNRESOLVED |
| shebang / 函数 | 不解析 | `#!` 只是普通注释行；`f() { ...; }` 会被当命令 |

> **`$?` 的坑值得单独记**：`?` 是 glob 字符。`echo $?` 里 `$?` 没有匹配文件时本应原样保留，但内置 echo 不处理 `GlobItem`，于是直接 TypeError。即使换成外部命令，`$?` 也只会被 glob 展开或原样传递，永远不会变成上一条命令的退出码。

## 7.9 一条 RUN 行的完整生命周期

把上面的碎片拼起来，`RUN: ls *.txt > out 2>&1` 的真实处理顺序是：

```
ShLexer
  "ls"          普通单词
  "*.txt"       GlobItem("*.txt")
  ">"           (" >",)
  "out"         普通单词
  "2>&1"        lex_arg_slow 先读到 "2"，发现下一个是 '>'，
                转成 (" >", 2)，再 lex 出 (">&",)，arg 为 "1"
        ↓
ShParser
  Command(args=["ls", GlobItem("*.txt")],
          redirects=[((">",), "out"), ((">&", 2), "1")])
        ↓
_executeShCmd
  1. 解析可执行文件：PATH 中找 ls
  2. 展开 glob：expand_glob_expressions → 绝对路径列表
  3. processRedirects：按顺序把 fd 1 指向 out，把 fd 2 复制到 fd 1
  4. Popen(ls, [绝对路径...], cwd=shenv.cwd, env=shenv.env)
  5. 等退出码，写进 results
        ↓
pipeline 退出码 = ls 的退出码（只有一条命令）
```

注意第 2 步在第 3 步之前：**glob 用的是执行命令时的 cwd，与重定向目标无关**。`cd` 改变的是 `shenv.cwd`，所以 `cd %t.d && ls *.txt` 能工作，但 `ls *.txt > %t.d/out` 里的 glob 仍然相对于当前 cwd。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`RUN:` 行就是 bash」 | 它是 lit 自己的 `ShLexer` + `ShParser` + `_executeShCmd`，没有变量展开、命令替换、`if`、`set -e`。 |
| 「`export FOO=bar` 之后 `$FOO` 会展开」 | 内置 shell 不做变量展开；`echo $FOO` 输出字面量 `$FOO`。外部程序能看到 `FOO`，因为 `export` 改的是传给子进程的 env。 |
| 「引号里的 `*` 也会展开」 | 单双引号内的 glob 字符都是字面量；只有未加引号的 `*`/`?` 才变成 `GlobItem`。 |
| 「`&` 是后台执行」 | 解析器接受，执行器直接 `unsupported shell operator: '&'`；本版本还会因错误对象是 `Seq` 而变成 UNRESOLVED。 |
| 「`2>&1` 就是把 stderr 扔掉」 | 它是「把 fd 2 复制到当前 fd 1」，位置决定结果；`> f 2>&1` 与 `2>&1 > f` 完全不同。 |
| 「`echo hi 2> f` 能写 stderr」 | 内置 echo 明确拒绝 stdin/stderr 重定向，报 `stdin and stderr redirects not supported for echo`，退出码 127。 |
| 「`false \| true` 应该 PASS」 | 默认 `pipefail=True`，中间失败会让 pipeline 失败；只有 `config.pipefail=False` 才只看最后一条。 |
| 「trace 里的 `# error: command failed` 一定意味着测试 FAIL」 | 它是每条命令的结果注解；`\|\| true` 恢复后测试仍可 PASS，但这条注解仍会打印。 |
| 「`echo $?` 能看上一条命令的退出码」 | `?` 是 glob 字符；`$?` 不是变量，`echo $?` 在本版本直接 UNRESOLVED。 |
| 「`<<` 是 heredoc」 | lit 没有 heredoc；`<<` 被切成两个 `<`，最终在重定向路径上抛 TypeError。 |

## 重点整理

- lit 的 shell 是**解释器**，不是 bash：分词 → 建树 → 执行，三步都在 Python 里。
- `ShLexer` 只认 `| & ; < >`、引号、反斜杠、`*`/`?`；其余字符都是普通单词的一部分。
- 单引号全字面；双引号只特殊处理 `\"`、`\\`、`\$`；引号外 `\` 转义任意字符。
- glob 只对未加引号的 `*`/`?` 生效，展开结果是**排序后的绝对路径**；无匹配保留原模式。
- 重定向支持 `>` `>>` `<` `2>` `2>>` `>&` `&>`，以及 `n>&m`（m 仅 0/1/2）；`<&`、`<<` 不支持。
- `pipefail` 默认 True：pipeline 的退出码是**最后一个非 0 的退出码**；关掉后只看最后一条。
- `Seq` 严格左结合，`;` 无条件执行右边，`&&`/`||` 短路；`&` 不支持；尾部操作符是语法错误。
- 没有变量展开、命令替换、`if`/`for`/函数、`set -e`、`FOO=bar cmd`、heredoc；`$?` 还会撞上 glob。

## 自我检核

不看笔记，试着回答：

- [ ] 为什么 `echo *.txt` 在内置 shell 里会 UNRESOLVED，而 `echo *.txt | cat` 却能正常输出？两条路径分别走到哪段代码？
- [ ] `> f 2>&1` 与 `2>&1 > f` 的结果差在哪里？请用 fd 复制的顺序解释。
- [ ] `pipefail=True` 时，`true | false | true` 的退出码是多少？`false | false` 呢？为什么？
- [ ] 一条 `RUN:` 行里出现 `;`、`&&`、`||` 混排时，lit 的树是什么形状？它和 bash 的解析在哪一点上不同？
- [ ] 你能举出三种「bash 合法但 lit 直接 parser error / command not found」的写法吗？
- [ ] `not` 和 `!` 在 lit 里是外部程序还是内置语法？`not --crash` 呢？

## 延伸阅读

- **[`llvm/utils/lit/lit/ShUtil.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/ShUtil.py)**
  - 读什么：`ShLexer.lex_arg_slow()`、`lex_arg_quoted()`、`lex_one_token()`、`ShParser.parse_command()`。
  - 能学到：本章所有 token 规则的唯一权威实现；特别注意 fd 前缀与 `<>` 的分支。
  - 前提：能读 Python 的 `while`/`if` 和生成器；约 280 行，半小时可读完。
- **[`llvm/utils/lit/lit/ShCommands.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/ShCommands.py)**
  - 读什么：`Command`、`GlobItem.resolve()`、`Pipeline`、`Seq`，全文 113 行。
  - 能学到：命令树的最小数据结构；为什么 `GlobItem` 要延迟到执行期才展开。
  - 前提：读完本章 7.2。
- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)**
  - 读什么：`TEST FORMATS` 里 ShTest 的段落，以及 `RUN` 行相关的说明。
  - 能学到：官方对内置 shell 的定位；对照本章看哪些行为文档没写。
  - 前提：先跑过 Ch 0 的最小 suite。
- **[LLVM Testing Infrastructure Guide](https://llvm.org/docs/TestingGuide.html)**
  - 读什么：`Writing a test` 与 `Extra tools` 两节。
  - 能学到：真实测试里 `RUN:` 的常见组合方式，以及 `not`/`count`/`split-file` 的官方用法。
  - 前提：知道 `opt`/`FileCheck` 的基本调用。

> 下一章把镜头拉近到「命令内部」：`cd`、`export`、`echo`、`mkdir`、`rm`、`umask`、`ulimit` 这些内置命令各自接受什么参数、失败时返回什么，以及工作目录和环境变量到底影响了谁。

→ [Ch 8 内置命令与执行环境](./08-shell-builtins.md)
