# Ch 9 — FileCheck 与 lit 的边界

> **目标**：看到一条 `opt ... | FileCheck %s` 失败时，能立刻判断「是 opt 没产出、还是 FileCheck 没匹配、还是 shell 把中间命令的失败吞了」；并且知道 `CHECK` 不是 lit 语法，`FileCheck` 的退出码 1 也不等于「被测程序错了」。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，`FileCheck`、`opt`、`not`、`count`、`split-file` 来自 `/home/ubuntu/llvm/builds/pr-lane/bin`。本章输出全部在 `/tmp` 临时 suite 实跑；FileCheck 细节只讲与诊断相关的部分，不展开成 FileCheck 全书。

---

## 9.1 四层责任图

一条典型的 LLVM 测试：

```text
; RUN: opt -S -passes=instcombine %s -o - | FileCheck %s
```

它其实有四层，各自负责完全不同的事：

```text
lit
 ├─ 发现 .ll/.test 文件（suffixes）
 ├─ 解析 RUN:/REQUIRES:/XFAIL:（directive）
 ├─ 把 RUN: 行交给内置 shell
 ├─ 收集 shell 的最终退出码
 └─ 判定 PASS/FAIL/UNRESOLVED，打印报告

内置 shell
 ├─ 分词、glob、重定向
 ├─ 按 | 建 Pipeline
 ├─ 起 opt 和 FileCheck 两个进程
 └─ 按 pipefail 规则给出 pipeline 退出码

被测程序（opt/llc/clang）
 ├─ 读 IR
 ├─ 跑 pass
 └─ 把结果写 stdout；自己的错误写 stderr；退出码表示成功/失败

FileCheck
 ├─ 从 stdin 读「被测程序的输出」
 ├─ 从 %s（check 文件）读 CHECK 模式
 ├─ 匹配成功 → 0；匹配失败/用法错误 → 1/2
 └─ 它不执行 opt，也不改 opt 的退出码
```

一句话：**lit 只看整条命令树的最终退出码；FileCheck 只看文本匹配；被测程序只看自己有没有跑成功。** 三者互不替代。

## 9.2 一条真实 PASS

测试文件 `fc-pass.test`：

```text
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s

define i32 @f(i32 %x) {
  %a = add i32 %x, 0
  ret i32 %a
}

; CHECK: define i32 @f
; CHECK-NEXT: ret i32 %x
```

真实运行（`-a`）：

```text
-- Testing: 1 tests, 1 workers --
PASS: LitLab :: tests/fc-pass.test (1 of 1)
Exit Code: 0

# executed command: /home/ubuntu/llvm/builds/pr-lane/bin/opt -S -passes=instcombine /tmp/.../fc-pass.test -o -
# executed command: /home/ubuntu/llvm/builds/pr-lane/bin/FileCheck /tmp/.../fc-pass.test
```

注意 trace 里没有输出块，因为两个命令都成功、也没往 stderr 写东西。`instcombine` 把 `add i32 %x, 0` 消掉，`ret` 变成 `ret i32 %x`，FileCheck 的 `CHECK-NEXT` 正好匹配下一行。

## 9.3 同一条命令的真实失败

把 `CHECK-NEXT` 改成当前输出里不存在的 `ret i32 %a`：

```text
; RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s
...
; CHECK: define i32 @f
; CHECK-NEXT: ret i32 %a
```

真实输出（截取关键部分）：

```text
FAIL: LitLab :: tests/fc-fail.test (1 of 1)
Exit Code: 1

# executed command: .../opt -S -passes=instcombine /tmp/.../fc-fail.test -o -
# executed command: .../FileCheck /tmp/.../fc-fail.test
# .---command stderr------------
# | /tmp/.../fc-fail.test:9:15: error: CHECK-NEXT: expected string not found in input
# | ; CHECK-NEXT: ret i32 %a
# |               ^
# | <stdin>:4:14: note: scanning from here
# | define i32 @f(i32 %x) {
# |              ^
# | <stdin>:5:2: note: possible intended match here
# |  ret i32 %x
# |  ^
# `-----------------------------
# error: command failed with exit status: 1
```

这里有三个不同层次的信号，必须分开读：

| 信号 | 含义 | 谁的 |
|---|---|---|
| `Exit Code: 1` | 整个测试脚本的最终退出码 | lit 收集到的 shell 退出码 |
| `error: command failed with exit status: 1` | **这一条命令**（FileCheck）退出 1 | FileCheck |
| `CHECK-NEXT: expected string not found` | 文本没匹配上 | FileCheck 的诊断 |

`opt` 没有报错，它的退出码是 0；`FileCheck` 因为没匹配上退出 1；pipeline 的最终退出码是 1；lit 因此判 FAIL。**所以「FileCheck 退出 1」只说明「输出不匹配」，不说明 opt 错了。**

## 9.4 退出码归因：谁决定了 `Exit Code`

| 场景 | 典型输出 | 最终 `Exit Code` |
|---|---|---|
| 全部成功 | 无 error | 0 |
| FileCheck 匹配失败 | `CHECK-NEXT: expected string not found` | 1（FileCheck） |
| FileCheck 输入为空或用法错误 | `FileCheck error: '<stdin>' is empty.` | 2（FileCheck） |
| 命令不存在 | `'foo': command not found` | 127（lit 的 `InternalShellError`） |
| 被测程序崩溃 | `error: command failed with exit status: -11` | -11（信号） |
| `count` 行数不符 | `Expected 2 lines, got 1.` | 1（count） |
| pipeline 中间命令失败 | `exit status: 4` + 后面命令仍执行 | 4（pipefail 取最后一个非 0） |
| `not cmd`，cmd 失败 | 无输出 | 0（not 取反） |

### pipeline 中间失败：默认不会被吞

```text
# RUN: sh -c 'exit 4' | cat
```

```text
FAIL: LitLab :: tests/midfail.test (1 of 1)
Exit Code: 4

# executed command: sh -c 'exit 4'
# error: command failed with exit status: 4
# executed command: cat
```

`cat` 成功了，但 `pipefail=True`（默认）让 pipeline 记住最后一个非 0 的退出码 4。只有在 `config.pipefail=False` 时，这条测试才会 PASS。**「`|` 后只看最后一条」是 bash 的默认行为，不是 lit 的默认行为。**

### 崩溃：负退出码

```text
# RUN: sh -c 'kill -SEGV $$' | cat
```

```text
FAIL: LitLab :: tests/crash.test (1 of 1)
Exit Code: -11

# executed command: sh -c 'kill -SEGV $$'
# error: command failed with exit status: -11
# executed command: cat
```

`Popen.wait()` 返回 `-11`（`SIGSEGV`），lit 原样保留。看到负数，先想「信号」，不要去找 exit code 11 的含义。

### 命令不存在：127

```text
# RUN: type true
```

```text
# | 'type': command not found
# error: command failed with exit status: 127
```

127 是 lit 自己给的（`InternalShellError` → `executeShCmd` 统一映射），和 bash 的 127 含义相同但来源不同。不要把它和被测程序的退出码混为一谈。

## 9.5 FileCheck 的诊断相关子集

只讲对「定位失败」最有用的六个指令。完整语义看 [FileCheck 文档](https://llvm.org/docs/CommandGuide/FileCheck.html)，这里只记它们失败时长什么样。

| 指令 | 作用 | 典型失败信息 |
|---|---|---|
| `CHECK` | 从上次匹配位置向后找 | `expected string not found in input` |
| `CHECK-LABEL` | 把输入切成块，块内重新开始匹配 | 找不到 label 时同 `CHECK` |
| `CHECK-NEXT` | 必须紧接上一匹配的下一行 | `is not on the line after the previous match` |
| `CHECK-SAME` | 必须与上一匹配在同一行 | `expected string not found in input` |
| `CHECK-DAG` | 一组顺序无关的匹配 | `expected string not found in input` |
| `CHECK-NOT` | 在区间内不能出现 | `excluded string found in input` |

### `CHECK-LABEL` + `CHECK-SAME` + `CHECK-DAG` 的真实 PASS

```text
; CHECK-LABEL: define i32 @a(
; CHECK: ret i32 %x
; CHECK-LABEL: define i32 @b(
; CHECK-SAME: i32 %y)
; CHECK-DAG: ret i32 %y
```

真实结果：PASS。注意 `CHECK-SAME` 的匹配起点是**上一个匹配结束的位置**：`CHECK-LABEL: define i32 @b(` 已经把 `(` 吃掉了，所以 `CHECK-SAME` 要写 `i32 %y)`，不能写 `(i32 %y)`。写错时的真实报错是：

```text
# | error: CHECK-SAME: expected string not found in input
# | ; CHECK-SAME: (i32 %y)
# | <stdin>:8:15: note: scanning from here
# | define i32 @b(i32 %y) {
```

### `CHECK-NEXT` 的真实失败

```text
# | error: CHECK-NEXT: is not on the line after the previous match
# | ; CHECK-NEXT: ret i32 %b
# | <stdin>:7:2: note: 'next' match was here
# |  ret i32 %b
# | <stdin>:4:14: note: previous match ended here
# | define i32 @g(i32 %x) {
# | <stdin>:5:1: note: non-matching line after previous match is here
# |  %a = shl i32 %x, 1
```

这条信息比 `CHECK` 的「找不到」更有用：它告诉你「我找到了，但不在正确的位置」，并指出中间多了哪一行。

### `CHECK-NOT` 的真实失败

```text
# | error: CHECK-NOT: excluded string found in input
# | ; CHECK-NOT: add i32
# | <stdin>:5:7: note: found here
# |  %a = add i32 %x, 1
```

`CHECK-NOT` 不是「全文不能出现」，而是「在两个相邻正匹配之间的区间内不能出现」。单独放在文件末尾时，区间是「上一个匹配之后到 EOF」。这也是为什么 `CHECK-NOT` 通常要和 `CHECK`/`CHECK-LABEL` 配对使用。

> 不要把 FileCheck 写成「全文 grep」。`CHECK` 是有状态的、顺序敏感的；`CHECK-DAG` 只是把一组匹配的顺序放宽，不是全文任意搜索。

## 9.6 输入转储与 `FILECHECK_OPTS`

FileCheck 的 `--dump-input` 默认是 `fail`：失败时自动把 stdin 和匹配位置画出来。上面每个失败例子里那段 `<<<<<< ... >>>>>>` 就是它。

| 取值 | 行为 |
|---|---|
| `fail`（默认） | 只在失败时转储 |
| `always` | 总是转储（PASS 也转储到 stderr） |
| `never` | 从不转储 |
| `help` | 打印帮助后退出 |

真实对照：

- `--dump-input=always` 时，PASS 的测试也会在 stderr 打出一大段 `Input was:`，但测试仍然 PASS（`Exit Code: 0`）。
- `FILECHECK_OPTS=--dump-input=never` 时，失败测试仍然 FAIL，但 `Input was:` 段消失，只剩 `expected string not found` / `scanning from here` / `possible intended match`。

`FILECHECK_OPTS` 能生效，是因为 `TestingConfig.py` 第 58 行把它放进了 `pass_vars`：宿主环境里的这个变量会传给测试进程。实测：

```bash
FILECHECK_OPTS=--dump-input=never \
  python3 .../lit.py tests/fc-fail.test -a
```

输出里不再有 `Input was:`。这是 CI 上压缩日志的常用手段。

## 9.7 pipeline 里的工具：`%t`、`split-file`、`not`、`count`

### `%t`：临时文件基名

`%t` 是 `cwd/Output/<basename>.tmp`（Ch 8 讲过）。它本身不是一个文件，而是一个**基名**：

```text
; RUN: %opt ... %s -o %t.ll
; RUN: %FileCheck %s < %t.ll
```

常见约定是 `%t.ll`、`%t.s`、`%t.out`。`%t` 所在的目录在测试开始前不一定存在，所以经常先 `rm -rf %t && mkdir %t` 或 `mkdir -p %t.d`。

### `split-file`：一个文件放多份输入

```text
; RUN: %split-file %s %t.split
; RUN: %opt -S -passes=instcombine %t.split/a.ll -o - | %FileCheck %s --check-prefix=CHECK-A

; CHECK-A: define i32 @a

;--- a.ll
define i32 @a(i32 %x) {
  %r = add i32 %x, 0
  ret i32 %r
}
;--- b.ll
define i32 @b(i32 %x) {
  ret i32 %x
}
```

真实结果：PASS。`split-file` 把 `%s` 按 `;--- <name>` 切开，写到 `%t.split/<name>`；`CHECK-A` 仍然在 `%s` 里。它解决的是「一个测试需要多份输入文件，但又不想在仓库里放一堆小文件」。

### `not`：把「预期失败」变成「测试成功」

典型写法：

```text
; RUN: not %opt -S -passes=doesnotexist %s 2>&1 | %FileCheck %s --check-prefix=ERR
...
; ERR: unknown pass
```

真实结果：PASS。拆开看：

- `%opt ...` 因为 pass 不存在而失败（非 0）。
- `not` 把它的退出码取反成 0。
- `2>&1` 把 opt 的 stderr 并进 stdout，喂给 FileCheck。
- FileCheck 检查 `ERR: unknown pass`，匹配成功返回 0。
- pipeline 两条命令都是 0，测试 PASS。

关键：**`not` 保护的是「被测程序允许失败」，不是「FileCheck 允许失败」。** 写成 `not FileCheck ...` 的语义是「FileCheck 必须失败」，跟「编译器输出错了」没有关系。LLVM suite 里还有 `| not` 的替换形式（`llvm/config.py` 第 516 行用 `FindTool("not")`），它把 `not` 作为管道的一级，而不是命令前缀。

### `count`：按行数判定

```text
# RUN: printf 'a\nb\n' | %count 2
```

PASS。行数不符时：

```text
# RUN: printf 'a\n' | %count 2
# | Expected 2 lines, got 1.
# error: command failed with exit status: 1
```

`count` 适合「只想确认输出了几行」的场景，比写一堆 `CHECK` 更直接。LLVM suite 里它通常以 `| count N` 的形式出现（同样是 `llvm/config.py` 的替换）。

## 9.8 四个真实归因案例

### 案例 1：命令找不到（127）

```text
# RUN: type true
# | 'type': command not found
# error: command failed with exit status: 127
```

归因：不是 FileCheck，也不是被测程序；是 lit 在 PATH 里找不到 `type`。修法：换成 `which` 或直接实验。

### 案例 2：被测程序崩溃（负退出码）

```text
# RUN: sh -c 'kill -SEGV $$' | cat
# error: command failed with exit status: -11
```

归因：`sh` 被信号杀死，pipeline 记住 -11。此时 FileCheck 根本没机会看到有效输入（如果它在管道里，会收到空输入并报 `<stdin> is empty`）。先修崩溃，不要先改 CHECK。

### 案例 3：FileCheck 不匹配（1）

```text
# | error: CHECK-NEXT: expected string not found in input
# error: command failed with exit status: 1
```

归因：opt 成功、输出存在，但文本和 CHECK 对不上。先看 `possible intended match here` 和 `Input was:`，确认是 pass 行为变了还是 CHECK 写错了。

### 案例 4：pipeline 中间命令失败（pipefail）

```text
# RUN: sh -c 'exit 4' | cat
# error: command failed with exit status: 4
```

归因：`cat` 成功，但中间命令退出 4。如果这是真实测试，先看中间命令的 stderr；不要因为最后一条 FileCheck 成功了就以为没事。

## 9.9 `%s` 的双重身份与注释语法

同一条 `RUN` 里，`%s` 出现两次，扮演两个角色：

```text
; RUN: opt ... %s -o - | FileCheck %s
              ↑                    ↑
         被测程序输入          FileCheck 的 check 文件
```

- 左边：`opt` 读 `%s` 里的 IR。
- 右边：`FileCheck` 从 `%s` 里读 `CHECK` 行，同时从 stdin 读 opt 的输出。

所以 `%s` 必须同时是「合法 IR」和「合法 check 文件」。`; CHECK:` 是 IR 注释，opt 会忽略；`# CHECK:` 不是 IR 注释，opt 会报错。我们实测过把 `# RUN:` 写进 `.ll`：

```text
# | /home/.../bin/opt: /tmp/.../fc-pass.test:1:1: error: expected top-level entity
# | # RUN: %opt -S -passes=instcombine %s -o - | %FileCheck %s
# | ^
```

修法很简单：`.ll` 用 `;`，`.test`/`.c` 用各自语言的注释符。lit 本身只认 `RUN:` 这几个字，不关心前面的注释符；但**被测程序关心**。

## 9.10 `%ProtectFileCheckOutput`：文档与源码的又一个裂缝

`llvm/docs/TestingGuide.md` 第 874 行仍然写着：

```text
%ProtectFileCheckOutput
:   This should precede a FileCheck call if and only if the call's textual
    output affects test results.
```

但在这个 commit 的源码树里：

```bash
grep -rn ProtectFileCheckOutput llvm/utils/lit llvm/test/lit.cfg.py
# 无输出
```

实测把它写进 `RUN:`：

```text
# RUN: %ProtectFileCheckOutput echo hi
# | '%ProtectFileCheckOutput': command not found
# error: command failed with exit status: 127
```

所以**本版本不能依赖它**。LLVM 自己的 `llvm/test/FileCheck/*.txt` 仍在用它，那些测试是否由别的配置注入这个替换，本章没有验证（构建目录的 lit 与源码树版本不一致，`llvm-lit` 跑 `llvm/test` 会因 `enable_profcheck` 缺失而失败）。结论按本课的证据约定标注：**源码树未定义、实测 127、上游用法未验证**。要控制 FileCheck 输出，用显式的 `--dump-input=never` 或 `FILECHECK_OPTS`。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`CHECK` 是 lit 的语法」 | `CHECK` 只被 FileCheck 读取；lit 只解析 `RUN:`/`REQUIRES:`/`XFAIL:` 等 directive。 |
| 「FileCheck 退出 1 = 被测程序错了」 | FileCheck 只判断文本匹配；opt 可能完全正常。先看 `error: command failed with exit status` 前面是哪条命令。 |
| 「`\|` 后只看最后一条」 | lit 默认 `pipefail=True`，中间命令失败也会让 pipeline 失败；只有 `config.pipefail=False` 才看最后一条。 |
| 「`not FileCheck` 是检查编译器错误」 | `not FileCheck` 的语义是「FileCheck 必须失败」；要检查编译器失败，用 `not opt ... \| FileCheck`。 |
| 「`%s` 只是一个路径」 | 它同时是被测程序输入和 FileCheck 的 check 文件；注释符必须匹配输入语言（`.ll` 用 `;`）。 |
| 「`CHECK-NOT` 是全文禁止」 | 它只在相邻正匹配之间的区间内生效；单独放末尾才是到 EOF。 |
| 「`--dump-input` 默认关闭」 | 默认是 `fail`，失败时自动转储；`always` 连 PASS 也转储。 |
| 「`%ProtectFileCheckOutput` 可以直接用」 | 本版本源码树未定义，实测 command not found 127；用 `--dump-input=never` 或 `FILECHECK_OPTS`。 |

## 重点整理

- 四层责任：lit 管发现/调度/最终判定，shell 管管道与退出码，被测程序管产出，FileCheck 管文本匹配。
- `Exit Code: 1` 通常是 FileCheck；`-11` 是信号；`127` 是命令找不到；`4` 可能是 pipeline 中间命令。
- `# error: command failed with exit status: N` 是**单条命令**的注解，不是最终判定；`|| true` 可以恢复测试但注解仍在。
- `pipefail` 默认 True，pipeline 取最后一个非 0 退出码；关掉后才只看最后一条。
- `CHECK`/`CHECK-LABEL`/`CHECK-NEXT`/`CHECK-SAME`/`CHECK-DAG`/`CHECK-NOT` 有状态、顺序敏感，不是 grep。
- `--dump-input` 默认 `fail`；`FILECHECK_OPTS` 会通过 `pass_vars` 传进测试。
- `%t` 是临时基名；`split-file` 切多份输入；`not` 翻转被测程序退出码；`count` 按行数判定。
- `%s` 同时是输入和 check 文件，注释符必须匹配语言；`%ProtectFileCheckOutput` 在本版本不可用。

## 自我检核

不看笔记，试着回答：

- [ ] `opt | FileCheck` 失败时，你如何用 trace 判断是 opt 失败、FileCheck 失败，还是两者都失败？
- [ ] `Exit Code: 4` 且最后一条命令是 `cat`，最可能发生了什么？默认 `pipefail` 在这里起了什么作用？
- [ ] `CHECK-NEXT` 与 `CHECK` 的失败信息差在哪？为什么 `CHECK-NEXT` 更适合锁定相邻指令？
- [ ] 为什么 `not opt ... | FileCheck` 能测试错误路径，而 `not FileCheck` 不能？
- [ ] `%s` 在一条 `RUN` 里出现两次，各自被谁读？如果把 `.ll` 的 `; RUN:` 写成 `# RUN:` 会发生什么？
- [ ] `--dump-input=fail`、`always`、`never` 分别什么时候用？`FILECHECK_OPTS` 为什么能影响测试进程？

## 延伸阅读

- **[FileCheck Command Guide](https://llvm.org/docs/CommandGuide/FileCheck.html)**
  - 读什么：`--dump-input`、`CHECK-NEXT`、`CHECK-SAME`、`CHECK-DAG`、`CHECK-NOT` 的官方定义。
  - 能学到：每个指令的精确匹配区间；对照本章 9.5 看诊断输出。
  - 前提：能读一条 `opt | FileCheck` 测试。
- **[LLVM Testing Infrastructure Guide](https://llvm.org/docs/TestingGuide.html)**
  - 读什么：`Writing a test`、`Extra tools`、`split-file` 相关段落。
  - 能学到：真实 LLVM 测试如何组织 `RUN`/`CHECK`、如何用 `split-file` 和多文件输入。
  - 前提：读过 Ch 4–5 的 directive 基础。
- **[`llvm/utils/lit/lit/TestRunner.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/TestRunner.py)**
  - 读什么：`_executeShCmd` 的退出码聚合（946–951 行）、`executeScriptInternal`（1019 行）、`getDefaultSubstitutions`（1351 行）。
  - 能学到：pipeline 退出码到底怎么算、`%t`/`%s` 怎么拼出来。
  - 前提：读过 Ch 2 的执行链路。
- **[`llvm/utils/lit/lit/llvm/config.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/llvm/config.py)**
  - 读什么：`use_default_substitutions()` 里的 `FileCheck`、`| count`、`| not` 替换（约 500–520 行）。
  - 能学到：为什么 LLVM suite 里可以直接写 `not`/`count`，而自建 suite 需要自己加替换。
  - 前提：知道 `ToolSubst` 是什么（Ch 14 会展开）。

> 下一章不再讲「怎么读测试」，而是「怎么自己写一组能诊断的测试」。你会为一个真实的 `opt` pass 搭一套 6 个用例的 suite，并亲手验证它真的会失败。

→ [练习 A 写一组能诊断的 LLVM IR 测试](./practice-a-diagnosable-ir-tests.md)
