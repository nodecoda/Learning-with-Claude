# Ch 6 — 替换系统全景

> **目标**：把 `getDefaultSubstitutions()` 注册的每一条替换、它的真实值和适用场景列全；理解替换是「按顺序的正则替换」而不是字符串插值，从而能解释 `%foo → %s`、`%bar → AAAA → BBBB`、`ToolSubst("opt")` 把 `%opt` 换坏、递归展开上限这些现象。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12。实验 suite 在 `/tmp/lit-ch4lab.XzgqPC` 与 `/tmp/lit-ch6lab.cGdLKT`；所有输出为本机实跑。Windows 行为只依据源码标注，未实测。

---

## 6.1 心智模型：一张有序的 `(pattern, replacement)` 表

`executeShTest()` 里，替换发生在命令执行之前：

```python
substitutions = list(extra_substitutions)
substitutions += getDefaultSubstitutions(test, tmpDir, tmpBase, ...)
script = applySubstitutions(script, substitutions, conditions, ...)
```

`getDefaultSubstitutions()`（`TestRunner.py` 第 1351 行）先做：

```python
substitutions = []
substitutions.extend(test.config.substitutions)
```

然后才追加内置替换。也就是说，最终列表的顺序是：

```text
config.substitutions 里的条目（按你 append 的顺序）
  ↓
%{pathsep}、%basename_t、%{s:basename}、...
  ↓
%s、%/s、%{s:real}、%{/s:real}、%{/s:regex_replacement}、%:s
  ↓
%S、%p、%t 的同组变体
```

`applySubstitutions()`（第 1651 行）对每条命令按这个列表**逐条**做 `re.sub(pattern, replacement, line)`。它不是一次性扫描，也不是变量插值；前一条替换的结果会被后一条继续匹配。

这解释了本章后面三个反直觉现象：

- 你 append 的 `%foo → %s` 会被后面的内置 `%s` 规则再次替换；
- 你 append 的 `%bar → AAAA` 会被下一条 `AAAA → BBBB` 再替换；
- `ToolSubst("opt")` 的 pattern 只要求词边界，不排除前一个字符是 `%`，所以 `%opt` 里的 `opt` 也会被换掉。

## 6.2 `getDefaultSubstitutions()` 的完整清单

先看路径家族。源码第 1405–1440 行对 `s`、`S`、`p`、`t` 四个字母循环，每个字母生成六个变体：

| 替换 | 值 | 说明 |
|---|---|---|
| `%s` | `sourcepath` | 测试源文件绝对路径 |
| `%S` | `sourcedir` | 源文件目录 |
| `%p` | `sourcedir` | 当前源码里与 `%S` 相同 |
| `%t` | `tmpName` | `tmpBase + ".tmp"`，临时文件基名 |
| `%/s`、`%/S`、`%/p`、`%/t` | 把 `\` 换成 `/` 的版本 | 跨平台路径 |
| `%{s:real}`、`%{S:real}`、`%{p:real}`、`%{t:real}` | `os.path.realpath(path)` | 解析符号链接后的真实路径 |
| `%{/s:real}` 等同组 | realpath 再把 `\` 换成 `/` | 跨平台 realpath |
| `%{/s:regex_replacement}` 等同组 | slash 化并转义 `@`、`&` | 塞进 `s@@@...@@@` 这类正则替换时用 |
| `%:s`、`%:S`、`%:p`、`%:t` | `colonNormalizePath(path)` | 去掉前导 `/`；Windows 去掉盘符冒号 |

`S`/`p`/`t` 的同组变体没有逐条打印，值由同一段循环源码推出；`s` 组已在 6.3 实测。另外还有这些单件：

| 替换 | 值 | 实测 |
|---|---|---|
| `%{pathsep}` | `os.pathsep` | Linux 上是 `:` |
| `%basename_t` | `basename(tmpBase)` | `subs.test` |
| `%{s:basename}` | `basename(sourcepath)` | `subs.test` |
| `%{s:stem}` | `splitext(basename)[0]` | `subs` |
| `%{t:stem}` | `basename(tmpBase)` | `subs.test` |
| `%{fs-src-root}` | `Path(sourcedir).anchor` | Linux 上是 `/` |
| `%{fs-tmp-root}` | `Path(tmpBase).anchor` | Linux 上是 `/` |
| `%{fs-sep}` | `os.path.sep`（或归一化后的 `/`） | `/` |
| `%/et` | `tmpName`，每个反斜杠替换成四个反斜杠 | POSIX 上与 `%t` 相同 |

两个容易误解的点：

1. `%{t:stem}` 在当前源码里返回的是 `tmpBaseName`，**不是**去掉扩展名的 stem。实验里它和 `%basename_t` 都输出 `subs.test`。想要去扩展名用 `%{s:stem}`。
2. `%p` 不是「父目录」。`getDefaultSubstitutions()` 里 `S` 和 `p` 都绑定 `sourcedir`；当前版本二者完全相同。

### 还有三类不在 `getDefaultSubstitutions()` 里的「替换」

| 写法 | 实现在哪 | 说明 |
|---|---|---|
| `%(line)`、`%(line+N)`、`%(line-N)` | `_substituteLineNumbers()`，第 1930 行 | 在 `RUN:`/`DEFINE:`/`REDEFINE:` 解析时先替换，不在默认替换表里 |
| `%{readfile:path}` | `_expandLateSubstitutions()`，第 198 行 | 命令分词之后的 late substitution；内部 shell 与外部 shell 各有一份实现 |
| `%if cond %{...%} %else %{...%}` | `applySubstitutions()` 的 `substituteIfElse()`，第 1671 行 | 条件来自 `available_features`，不是环境变量 |

把这三类误当成 `config.substitutions` 条目，是读源码时最常见的偏差。

## 6.3 一次打印所有替换的真实值

测试文件 `subs.test`：

```text
# RUN: echo "s=%s"
# RUN: echo "S=%S"
# RUN: echo "p=%p"
# RUN: echo "t=%t"
# RUN: echo "slash-s=%/s"
# RUN: echo "colon-s=%:s"
# RUN: echo "real-s=%{s:real}"
# RUN: echo "regex-s=%{/s:regex_replacement}"
# RUN: echo "pathsep=%{pathsep}"
# RUN: echo "fs-src-root=%{fs-src-root}"
# RUN: echo "fs-tmp-root=%{fs-tmp-root}"
# RUN: echo "fs-sep=%{fs-sep}"
# RUN: echo "basename_t=%basename_t"
# RUN: echo "s-basename=%{s:basename}"
# RUN: echo "s-stem=%{s:stem}"
# RUN: echo "t-stem=%{t:stem}"
```

`-a` 轨迹里的真实 stdout（逐行抽取，未改动）：

```text
s=/tmp/lit-ch4lab.XzgqPC/tests/subs.test
S=/tmp/lit-ch4lab.XzgqPC/tests
p=/tmp/lit-ch4lab.XzgqPC/tests
t=/tmp/lit-ch4lab.XzgqPC/Output/tests/Output/subs.test.tmp
slash-s=/tmp/lit-ch4lab.XzgqPC/tests/subs.test
colon-s=tmp/lit-ch4lab.XzgqPC/tests/subs.test
real-s=/tmp/lit-ch4lab.XzgqPC/tests/subs.test
regex-s=/tmp/lit-ch4lab.XzgqPC/tests/subs.test
pathsep=:
fs-src-root=/
fs-tmp-root=/
fs-sep=/
basename_t=subs.test
s-basename=subs.test
s-stem=subs
t-stem=subs.test
```

`%:s` 把开头的 `/` 去掉了：`/tmp/...` → `tmp/...`。这不是相对路径，只是 `colonNormalizePath()` 在 POSIX 上的实现（第 1343 行）：

```python
def colonNormalizePath(path):
    if kIsWindows:
        return re.sub(r"^(.):", r"\1", path.replace("\\", "/"))
    else:
        assert path[0] == "/"
        return path[1:]
```

### `%{s:real}` 真的会解析符号链接

建一个符号链接 `tests/link.test -> real-target.test`，跑 `link.test`：

```text
s=/tmp/lit-real-lab.SFSbl2/tests/link.test
real=/tmp/lit-real-lab.SFSbl2/tests/real-target.test
```

`%s` 保留你调用的路径，`%{s:real}` 走 `os.path.realpath()` 得到真实文件。需要判断「这个测试实际来自哪个文件」时用它。

### `%{/s:regex_replacement}` 会转义 `@` 和 `&`

在路径含 `&` 的 suite 里跑：

```text
# RUN: echo "s=%s"
# RUN: echo "regex=%{/s:regex_replacement}"
```

真实输出（节选）：

```text
s=/tmp/lit-amp&lab.xWueuP/tests/amp.test
regex=/tmp/lit-amp\&lab.xWueuP/tests/amp.test
```

`regex_escape()`（第 1400 行）只处理两个字符：

```python
def regex_escape(s):
    s = s.replace("@", r"\@")
    s = s.replace("&", r"\&")
    return s
```

因为这两种字符在 `s@@@...@@@` 形式的正则替换里有特殊含义。它不转义 `.`、`*`、`[` 等正则元字符——它的用途是「作为替换文本」，不是「作为 pattern」。

### `%{readfile:...}` 是 late substitution

测试：

```text
# RUN: echo "%{readfile:%S/data.txt}"
```

`data.txt` 内容两行，真实输出（节选）：

```text
# RUN: at line 1
echo "payload-line-1
payload-line-2
"
# executed command: echo '%{readfile:/tmp/lit-ch4lab.XzgqPC/tests/data.txt}'
# | payload-line-1
# | payload-line-2
```

注意 `# executed command` 里仍然是 `%{readfile:...}`：它在命令分词之后才展开成参数内容，所以 lit 的调试打印看不到展开后的文本，但 stdout 是展开后的。

文件不存在时：

```text
# executed command: @echo 'echo "%{readfile:/tmp/lit-ch4lab.XzgqPC/tests/nope.txt}"'
# | File specified in readfile substitution does not exist: /tmp/lit-ch4lab.XzgqPC/tests/nope.txt
# error: command failed with exit status: 127
```

结果是 FAIL + 127，不是 UNRESOLVED。相对路径会按执行目录（`cwd`）解析，不是按源码目录；跨目录写测试时用 `%S/...` 更稳。

## 6.4 版本钉子：`%T` 已移除，`%b` 不存在

在 LLVM 24 里写：

```text
# RUN: echo "T=%T b=%b"
```

真实输出（节选）：

```text
# RUN: at line 1
echo "T=%T b=%b"
# executed command: echo 'T=%T b=%b'
# | T=%T b=%b
```

不报错、不展开、原样传给 shell。`%T` 的移除提交：

```console
$ cd /home/ubuntu/llvm/llvm-project
$ git log -1 --format='%h %ad %s' --date=short 7ff6973f1ae9
7ff6973f1ae9 2025-09-26 [lit] Remove support for %T
```

`llvm/docs/TestingGuide.md` 第 741 行仍保留 `%T` 的说明，这是文档滞后。`%b` 在当前 `TestRunner.py` 里没有定义，旧教程里的 `%b` 不要照抄；用 `%{s:basename}`、`%{s:stem}`、`%{t:stem}`、`%basename_t`。

## 6.5 顺序陷阱：先定义的替换会被后定义的再次匹配

在 `lit.cfg.py` 里按顺序 append 三条规则：

```python
config.substitutions.append(("%foo", "%s"))
config.substitutions.append(("%bar", "AAAA"))
config.substitutions.append(("AAAA", "BBBB"))
```

测试：

```text
# RUN: echo "foo=%foo"
# RUN: echo "bar=%bar"
# RUN: echo "opt=%opt"
```

真实输出（节选）：

```text
# RUN: at line 1
echo "foo=/tmp/lit-ch6lab.cGdLKT/tests/order.test"
# | foo=/tmp/lit-ch6lab.cGdLKT/tests/order.test
# RUN: at line 2
echo "bar=BBBB"
# | bar=BBBB
# RUN: at line 3
echo "/home/ubuntu/llvm/builds/pr-lane/bin/opt=%/home/ubuntu/llvm/builds/pr-lane/bin/opt"
# | /home/ubuntu/llvm/builds/pr-lane/bin/opt=%/home/ubuntu/llvm/builds/pr-lane/bin/opt
```

逐条解释：

1. `%foo → %s` 是用户规则，先执行；随后内置 `%s` 规则把结果换成源文件路径。
2. `%bar → AAAA` 先执行；下一条 `AAAA → BBBB` 再把 `AAAA` 换掉，所以最终是 `BBBB`。
3. `ToolSubst("opt")` 的 pattern 匹配到了 `%opt` 里的 `opt`，把 `%opt` 变成 `%<opt 的绝对路径>`。因为替换只做一遍，路径里第二个 `opt` 没有被再次匹配；但结果已经坏了。

### 为什么 `ToolSubst("opt")` 会匹配 `%opt`

`ToolSubst.__init__()`（`llvm/subst.py` 第 38 行，class 在第 29 行）默认 `pre=r".-^/\<"`、`post="-."`，然后：

```python
def wordify(word):
    match = wordifier.match(word)
    introducer = match.group(1)
    word = match.group(2)
    return introducer + r"\b" + word + r"\b"

self.regex = not_in(pre, "<") + wordify(key) + not_in(post)
```

`pre` 的意思是「如果词边界前一个字符属于这些字符，就不匹配」。`%` 不在 `.-^/\<` 里，`%` 与 `o` 之间又正好是词边界，所以 `\bopt\b` 命中 `%opt` 的 `opt` 部分。

这不是 bug，而是 `ToolSubst` 的设计：它假设你写的是裸工具名 `opt`。要定义自己的 `%opt`，应该在 `config.substitutions` 里用更具体的 pattern，或者把工具替换放到后面并确认没有同名占位符。Ch 14 会讲 `lit.llvm` 的完整工具替换机制。

## 6.6 `applySubstitutions()` 的内部顺序

第 1651 行的主循环可以简化成：

```python
def processLine(ln):
    ln = substituteIfElse(escapePercents(ln))
    for a, b in substitutions:
        b = b.replace("\\", "\\\\")
        ln = _caching_re_compile(a).sub(str(b), escapePercents(ln))
    return ln.strip()
```

四个细节：

1. **`%%` 先被藏起来**：`escapePercents()` 把 `%%` 换成 `#_MARKER_#`，循环结束后 `unescapePercents()` 再换回 `%`。所以 `%%s` 输出字面量 `%s`，不会被 `%s` 规则吃掉。实测：

   ```text
   # RUN: echo "100%% and %%s"
   # executed command: echo '100% and %s'
   # | 100% and %s
   ```

2. **替换值里的反斜杠会加倍**：`b = b.replace("\\", "\\\\")`，避免 `re.sub` 把替换文本里的 `\1` 当反向引用。

3. **每条规则都是正则**：pattern 不是字面量。`config.substitutions` 里写 `("%foo", ...)`，`%` 和 `f` 都不是特殊字符，所以看起来像字面量；但写 `("%s", ...)` 时 `%` 也安全，写 `("a.b", ...)` 就会匹配 `axb`。

4. **默认不做递归**：`recursion_limit=None` 时只按列表走一遍；只有配置了 `config.recursiveExpansionLimit` 才进入 `processLineToFixedPoint()`。

### 递归上限实验

一个永远无法稳定的替换：

```python
config.recursiveExpansionLimit = 5
config.substitutions.append(("%x", "%x!"))
```

测试 `# RUN: echo %x`，真实结果（节选）：

```text
UNRESOLVED: RecLab :: tests/rec.test (1 of 1)
ValueError: Recursive substitution of '%dbg(RUN: at line 1)  echo %x' did not complete in the provided recursion limit (5)
```

`recursiveExpansionLimit` 的默认值是 `None`（`TestingConfig.py` 第 213 行），属性 setter 只接受 `None` 或非负整数（第 216–233 行）。把 `None` 理解成「不递归」而不是「无限递归」。

## 6.7 `%if`：条件来自 feature，不是环境变量

`substituteIfElse()` 的语法是：

```text
%if cond %{ branch_if %} %else %{ branch_else %}
```

`cond` 交给 `BooleanExpression.evaluate(cond, conditions)`。`conditions` 在 `executeShTest()` 第 2302–2303 行构造：

```python
conditions = {feature: True for feature in test.config.available_features}
```

所以只有 `available_features` 里的名字为真。实验（suite 的 feature 是 `foo`、`bar`）：

```text
# RUN: echo "if-foo=%if foo %{YES%} %else %{NO%}"
# RUN: echo "if-PATH=%if PATH %{YES%} %else %{NO%}"
```

真实输出（节选）：

```text
if-foo=YES
if-PATH=NO
```

`PATH` 是环境变量，不是 feature，所以条件为假。想按环境变量分支，得先在 `lit.cfg.py` 里把对应 feature 加进 `config.available_features`。

## 6.8 `DEFINE`/`REDEFINE` 只改当前测试的副本

在 Ch 5 我们已经看过语法和 `%(line)`。这里补一个作用域实验：`define-scope-a.test` 定义 `%{only}` 并打印，真实 stdout 是 `visible-in-a`；`define-scope-b.test` 直接使用 `%{only}`，真实 stdout 是字面量 `%{only}`。b 没有被替换，原因是 `executeShTest()` 每次调用 `getDefaultSubstitutions()` 都构造新列表，`DEFINE` 的 `adjust_substitutions()` 只修改这个列表；`test.config.substitutions` 本身不变，其他测试也拿不到。

`%(line)` 同理，不是全局替换：它在 `_handleCommand()` / `_handleSubst()` 里先把 `%(line)` 换成数字，之后才进入 `applySubstitutions()`。所以 `%(line)` 只对 `RUN:`、`DEFINE:`、`REDEFINE:` 生效，且在替换表之前就完成。

## 6.9 路径里的空格、反斜杠与 Windows

### 空格

带空格的路径，替换本身没问题，问题是 shell 分词。`%S` 不加引号会被拆成多个参数：

```text
# RUN: ls %S
```

真实结果（路径 `/tmp/lit space lab.78DNoT`）：

```text
FAIL: SpaceLab :: tests/unquoted.test (1 of 1)
Exit Code: 2
# executed command: ls /tmp/lit space lab.78DNoT/tests
# | ls: cannot access '/tmp/lit': No such file or directory
# | ls: cannot access 'space': No such file or directory
# | ls: cannot access 'lab.78DNoT/tests': No such file or directory
# error: command failed with exit status: 2
```

加引号就正常：

```text
# RUN: echo "s=%s"
# executed command: echo 's=/tmp/lit space lab.78DNoT/tests/space.test'
```

### 反斜杠

在 POSIX 上，反斜杠可以是文件名字符；在 Windows 上它是分隔符。`%s` 保留原样，`%/s` 把 `\` 换成 `/`。实验路径含 `\`：

```text
s=/tmp/lit-back\slash.YrPeAW/tests/back.test
slash=/tmp/lit-back/slash.YrPeAW/tests/back.test
```

`%/s` 的结果在 POSIX 上指向另一个不存在的路径。所以「把 `\` 换成 `/`」不是通用的路径修复，只是为跨平台文本输出服务。写正则或 Windows 测试时再考虑它。

### Windows

未实测，源码行为（第 1343–1349、1376–1399 行）：

- `colonNormalizePath()` 在 Windows 上把 `C:\foo` 变成 `C/foo`（去掉盘符冒号，反斜杠转正斜杠）；
- `normalize_slashes` 在外部 shell 或 `use_normalized_slashes` 参数开启时生效；
- `%{fs-src-root}` / `%{fs-tmp-root}` 是 `Path.anchor`，Windows 上通常是 `C:\`；
- `%{fs-sep}` 是 `\`，除非归一化。

这些差异会影响 `%:s`、`%/s`、`%{fs-*}` 的字符串比较；写跨平台测试时不要假设它们和 Linux 一样。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「替换是字符串插值，一次扫描」 | 是有序的 `(pattern, replacement)` 列表，逐条 `re.sub`；前面的结果会被后面的规则继续匹配。 |
| 「`%T` 还能用」 | LLVM 24 已移除（commit `7ff6973f1ae9`），原样保留；`%b` 也不存在。 |
| 「`%{t:stem}` 是去扩展名的 stem」 | 当前源码返回 `basename(tmpBase)`，实验输出 `subs.test`；去扩展名用 `%{s:stem}`。 |
| 「`%t` 是目录」 | 它是文件基名，末尾 `.tmp`；先 `mkdir -p %t.dir` 再使用子路径。 |
| 「`%if` 能判断环境变量」 | 条件只来自 `available_features`；`%if PATH` 在 PATH 存在时也是假。 |
| 「替换值里的 `$`/反斜杠会原样进 shell」 | `processLine()` 会把替换值的反斜杠加倍；`%%` 先被标记再还原。需要字面量时按规则写。 |
| 「`ToolSubst("opt")` 只匹配裸 `opt`」 | 默认 `pre` 不含 `%`，`%opt` 里的 `opt` 也会被匹配，结果是 `%<绝对路径>`。 |
| 「`%(line)` 在所有 directive 里都能用」 | 只在 `RUN:`/`DEFINE:`/`REDEFINE:` 解析时由 `_substituteLineNumbers()` 处理；`REQUIRES:`/`UNSUPPORTED:`/`XFAIL:` 不展开。 |

## 重点整理

- `config.substitutions` 先于内置替换执行；最终替换表是有序列表，不是字典。
- `%s/%S/%p/%t` 各有 `%/`、`%{...:real}`、`%{/...:regex_replacement}`、`%:` 变体；`%p` 与 `%S` 在当前源码里相同。
- `%{t:stem}` 是 `basename(tmpBase)`，不是去扩展名；`%{s:stem}` 才是去扩展名。
- `%{pathsep}`、`%{fs-src-root}`、`%{fs-tmp-root}`、`%{fs-sep}` 是平台/文件系统常量；`%basename_t` 是临时基名。
- `%(line)`、`%{readfile:...}`、`%if` 都不在 `getDefaultSubstitutions()` 里：分别属于行号预处理、late substitution、条件替换。
- `%%` 通过临时标记保护，`%%s` 输出字面量 `%s`；替换值的反斜杠会被加倍。
- `config.recursiveExpansionLimit` 默认 `None`（不递归）；设成整数后才会迭代到不动点，超出上限报 ValueError。
- `DEFINE`/`REDEFINE` 只改当前测试的替换副本；`ToolSubst` 的词边界规则会让 `%opt` 被误替换。

## 自我检核

不看笔记，试着回答：

- [ ] 为什么 `%bar → AAAA` 后面再 append `AAAA → BBBB`，最终命令里是 `BBBB`？这和「递归展开」是一回事吗？
- [ ] `%{s:stem}`、`%{s:basename}`、`%{t:stem}`、`%basename_t` 在 `subs.test` 上分别输出什么？哪一个名字最容易误导？
- [ ] `%%s` 为什么不会展开成源文件路径？`escapePercents()` 在这中间做了什么？
- [ ] `%if foo` 为真、`%if PATH` 为假，判断依据分别是什么？环境变量怎样才会变成 feature？
- [ ] `ToolSubst("opt")` 为什么会命中 `%opt`？如果要定义自己的 `%opt`，你会怎么避开？
- [ ] `%{readfile:...}` 为什么在 `# executed command` 里仍是原文？它在哪个阶段展开？

## 延伸阅读

- **[lit Command Guide — SUBSTITUTIONS](https://llvm.org/docs/CommandGuide/lit.html#substitutions)**
  - 读什么：`SUBSTITUTIONS` 一节，先建立官方术语，再对照本章的源码清单。
  - 能学到：`%s`/`%t`/`%p` 等替换的官方定义；注意文档可能滞后于 `%T` 移除。
  - 前提：读完本章 6.1，知道替换是列表而不是插值。
- **[`llvm/utils/lit/lit/TestRunner.py` — `getDefaultSubstitutions()`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：第 1351–1441 行；重点看 `path_substitutions` 循环、`regex_escape()`、`colonNormalizePath()`。
  - 能学到：每个替换的真实绑定；为什么 `%{t:stem}` 返回 basename。
  - 前提：能读 Python 正则与 `os.path`。
- **[`llvm/utils/lit/lit/TestRunner.py` — `applySubstitutions()`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：第 1651–1809 行；按 `escapePercents` → `substituteIfElse` → `processLine` → `processLineToFixedPoint` 的顺序读。
  - 能学到：`%%`、`%if`、递归上限、替换反斜杠加倍的确切实现。
  - 前提：读过本章 6.6。
- **[`llvm/utils/lit/lit/llvm/subst.py` — `ToolSubst`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/llvm/subst.py)**
  - 读什么：`ToolSubst.__init__()` 与 `wordify()`，约 80 行。
  - 能学到：`pre`/`post` 字符集、词边界、`%opt` 被误替换的原因；Ch 14 会展开工具替换。
  - 前提：知道 Python 正则的 `\b` 与 lookaround。
- **[`llvm/utils/lit/tests/substitutions.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/tests/substitutions.py)**
  - 读什么：整个文件，只有十几行。
  - 能学到：lit 官方对 `%{s:basename}`、`%{s:stem}`、`%{t:stem}`、`%basename_t` 的可执行断言。
  - 前提：能读 FileCheck 的 `CHECK` 前缀。

> 下一章进入 lit 内置 shell：它和 bash 共享词汇表，但不是 bash。词法、管道、重定向、`cd`/`export` 的边界都会在那里逐条拆开。

→ [Ch 7 lit 内置 shell：词法、管道、重定向](./07-internal-shell.md)
