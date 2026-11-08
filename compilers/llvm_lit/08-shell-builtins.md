# Ch 8 — 内置命令与执行环境

> **目标**：能说清 `RUN:` 行里的 `cd`、`export`、`echo`、`mkdir`、`rm`、`umask`、`ulimit` 是 lit 内部实现还是外部程序，各自接受什么参数、失败时返回几，以及 `cwd`/`env` 的作用范围到哪为止。

> **环境**：LLVM `24.0.0git` / lit `24.0.0dev`，源码 `/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`。工具用 `/home/ubuntu/llvm/builds/pr-lane/bin/{opt,FileCheck,not,count}`。本章输出全部来自 `/tmp` 临时 suite 的实跑。

---

## 8.1 `ShellEnvironment`：一个测试一个可变环境

`llvm/utils/lit/lit/ShellEnvironment.py` 第 55 行：

```python
class ShellEnvironment:
    __slots__ = ("cwd", "env", "umask", "dirStack", "ulimit", "normalize_slashes")
```

| 字段 | 谁改它 | 作用范围 |
|---|---|---|
| `cwd` | `cd` / `pushd` / `popd` | 之后所有 `Popen(cwd=...)` 和相对路径 glob |
| `env` | `export` / `env` | 之后所有子进程的 `env=` |
| `umask` | `umask` | 只包住外部进程的 `Popen` |
| `dirStack` | `pushd` / `popd` | 仅当前测试 |
| `ulimit` | `ulimit` | 之后所有外部进程（通过包装器） |
| `normalize_slashes` | 配置 | Windows 路径归一化 |

这个对象在 `executeScriptInternal()`（`TestRunner.py` 第 1019 行）里创建，**每个测试一份**，不是每个 suite 一份，也不是全局一份。一个测试里的多条 `RUN:` 会被 `make_tree()` 用 `&&` 串起来，共享同一个 `shenv`，所以 `cd` 对后续 `RUN:` 有效，但不会影响别的测试，更不会影响 lit 主进程。

> 源码 docstring 还写着 “Environment variables are not implemented”，但 `env` 字典从构造到 `Popen` 都在用；这是注释滞后。

## 8.2 哪些命令是内置的

`TestRunner.py` 第 588–604 行有两张表：

```python
inproc_builtins = {
    "cd", "export", "echo", "@echo", "mkdir", "popd",
    "pushd", "rm", "ulimit", "umask", ":",
}
pipeline_builtins = {"cat": ..., "diff": ...}
```

- `inproc_builtins` 在 **lit 进程内**执行，能直接改 `shenv`（`cd`、`export`、`umask` 必须这样）。
- `pipeline_builtins` 也在进程内执行，但通过 `InProcessPipe`，可以参与管道。
- 不在表里的（`true`、`false`、`test`、`printf`、`ls`、`sh`、`touch`、`chmod`、`stat`、`which`）都是**外部程序**，从 `PATH` 解析。

`echo` 有额外规则（第 680–681 行）：**只有独立成一条命令的 `echo` 才用内置实现**；一旦进入管道，`echo` 走外部 `/usr/bin/echo`。

## 8.3 逐个内置命令

### `cd`：只接受一个参数，而且永远成功

`InprocBuiltins.py` 第 21–30 行：参数个数不是 2 就报 `'cd' supports only one argument`；否则 `shenv.change_dir()`，返回 0。注释写得很直白：目录不存在时，**下一条** `Popen` 才会失败。

```text
# RUN: cd %t.nope
# RUN: touch f
```

```text
# executed command: cd /tmp/.../cwd2.test.tmp.nope
# executed command: touch f
# | Could not create process (/usr/bin/touch) due to [Errno 2] No such file or directory: '/tmp/.../cwd2.test.tmp.nope'
# error: command failed with exit status: 127
```

退出码是 127：`OSError` 被包装成 `InternalShellError`，再统一映射到 127。

### `pushd` / `popd`：目录栈

`pushd DIR` 把当前 `cwd` 压栈再切换；`popd` 不接受参数，栈空时报 `popd: directory stack empty`，127。

```text
# RUN: mkdir -p %t.d/sub
# RUN: cd %t.d
# RUN: pwd
# RUN: pushd sub
# RUN: pwd
# RUN: popd
# RUN: pwd
```

```text
# | /tmp/.../cwd1.test.tmp.d
# | /tmp/.../cwd1.test.tmp.d/sub
# | /tmp/.../cwd1.test.tmp.d
```

`pwd` 是外部程序，读到的是 `Popen(cwd=shenv.cwd)`，不是 lit 打印的。

### `export`：一次只能一个 `KEY=VALUE`

`InprocBuiltins.py` 第 51–56 行要求 `len(cmd.args) == 2`：

```text
# RUN: export A=1 B=2
# | 'export' supports only one argument
# error: command failed with exit status: 127
```

`updateEnv()`（`ShellEnvironment.py` 第 87 行）其实支持 `KEY=VALUE`、`-u NAME`、`-i`，但那些是给 `env` 命令用的；`export` 只放行一个参数，所以 `export -u FOO` 也会失败。

### `echo`：只支持 `-e`、`-n`，而且不能组合

`executeBuiltinEcho()` 第 87–96 行只循环识别 `("-e", "-n")`：

| 写法 | 内置 echo（独立命令） | 外部 echo（管道里） |
|---|---|---|
| `echo -e "a\nb"` | 两行 `a`、`b` | 同左（GNU echo） |
| `echo -n "x"` | 不换行 | 同左 |
| `echo -ne "x\ny"` | 字面量 `-ne x\ny` | 解释转义，输出 `x`、`y` |

真实对照：

```text
# RUN: echo -ne "x\ny"          →  | -ne x\ny
# RUN: echo -ne "x\ny" | cat    →  | x
                                   | y
```

同一个写法两条路径两个结果：`-ne` 不在 `("-e","-n")` 里，内置 echo 当普通参数；管道里的 GNU echo 认 `-ne`。

还有两条硬限制（第 62–66 行）：不能有 stdin 重定向（`echo hi < f`），也不能有 stderr 重定向（`echo hi 2> f`）：

```text
# | stdin and stderr redirects not supported for echo
# error: command failed with exit status: 127
```

### `mkdir`：只认 `-p`，「已存在」算成功

第 116–150 行用 `getopt.gnu_getopt(args, "p")`。非 `-p` 分支用 `dir.mkdir(exist_ok=True)`，所以：

```text
# RUN: mkdir d
# RUN: mkdir d     ← 仍返回 0，与 GNU mkdir 不同
# RUN: test -d d
```

目标是非目录文件才失败：

```text
# | Error: 'mkdir' command failed, [Errno 17] File exists: '/tmp/.../f'
# error: command failed with exit status: 1
```

### `rm`：支持 `-f`/`-r`/`-R`/`--recursive`，但有个 POSIX 缺口

| 命令 | 结果 |
|---|---|
| `rm missing` | FAIL 1，`Error: 'rm' command failed, [Errno 2] ...` |
| `rm -f missing` | PASS，静默跳过 |
| `rm dir`（不带 `-r`） | **打印 `Error: ... is a directory`，退出 1，但目录仍被删除** |
| `rm -r dir` | PASS，递归删除 |
| `rm`（无参数） | FAIL 127，`Error: 'rm' is missing an operand` |

第三条来自第 190–252 行的缩进：`if not recursive` 只写错误和置退出码，**没有 `continue`，`rmtree` 也没有放进 `else`**。实测：

```text
# RUN: mkdir -p d
# RUN: touch d/f
# RUN: rm d || true
# RUN: test -e d || echo gone
# | Error: /tmp/.../d is a directory
# error: command failed with exit status: 1
# | gone
```

不要依赖「`rm dir` 失败就说明目录还在」。

### `umask`：只影响外部子进程

第 264 行要求恰好一个八进制参数，且只在 POSIX 上可用。真正应用在 `TestRunner.py` 第 816–826 行：只在 `subprocess.Popen` 前后临时 `os.umask()`。

`umask 077` 之后实测：

| 操作 | 谁创建文件 | 实际权限 |
|---|---|---|
| `touch f`（外部） | `/usr/bin/touch` | `600`（`666 & ~077`） |
| `mkdir d`（内置） | `pathlib.Path.mkdir`，在 lit 进程内 | `775`（吃 lit 进程自己的 umask） |
| `echo x > f`（重定向） | `processRedirects`，在 lit 进程内 `open()` | `664`（同样不吃 `shenv.umask`） |

所以 `umask` 不是「整个测试的 umask」，只是「下一条外部命令的 umask」。

### `ulimit`：通过包装器设置，`-v`/`-s` 以 KB 计

第 278 行支持 `-v`（`RLIMIT_AS`，KB）、`-n`（`RLIMIT_NOFILE`）、`-s`（`RLIMIT_STACK`，KB）、`-f`（`RLIMIT_FSIZE`，字节），值可以是整数或 `unlimited`。真正设置限值的是包装器 `builtin_commands/_launch_with_limit.py`（`TestRunner.py` 第 807–814 行），因为调低后无法在不提权的情况下恢复。

```text
# RUN: ulimit -n 64
# RUN: sh -c 'ulimit -n'
# | 64
```

Windows 上直接报 `'ulimit' not supported on this system`；`umask` 同理。跨平台 suite 要加 `UNSUPPORTED: system-windows`。

### `:`：丢弃参数，返回 0

只有一行实现：构造退出码 0 的 `ShellCommandResult`。常用于占位：`: ignored arguments`。

## 8.4 环境变量：`export` 改的是子进程的 env，不是文本

`updateEnv()`（`ShellEnvironment.py` 第 87 行）：遍历参数，`-u NAME` 删除，`-i` 清空，`KEY=VALUE` 写入，遇到第一个不含 `=` 的参数就停下，把它和后面的参数作为「要执行的命令」返回。

一个测试同时证明「env 生效」和「不展开」：

```text
# RUN: export FOO=bar
# RUN: echo $FOO
# RUN: printenv FOO
# RUN: sh -c 'echo $FOO'
# RUN: env | grep '^FOO=bar$'
```

```text
# executed command: echo '$FOO'
# | $FOO
# executed command: printenv FOO
# | bar
# executed command: sh -c 'echo $FOO'
# | bar
# | FOO=bar
```

- `echo $FOO`：lit 不展开，内置 echo 原样打印。
- `printenv`：外部程序从 env 读到 `bar`。
- `sh -c 'echo $FOO'`：**外部 sh 自己**展开，不是 lit 的功劳。
- `env | grep`：env 里确实有 `FOO=bar`。

### 每条命令的 env 副本

`TestRunner.py` 第 623–641 行对 `env` 有特殊处理：复制一份 `ShellEnvironment`，后续 `KEY=VALUE`/`-u`/`-i` 只影响这条命令。

```text
# RUN: env FOO=1 BAR=2 printenv FOO
# RUN: env -u FOO printenv FOO || echo unset-ok
# RUN: env -i printenv PATH || echo PATH-unset
# | unset-ok
# | PATH-unset
```

`env -i env` 打印空（bare env 由 `_make_env_print_fn` 在进程内输出）。注意 `env -i sh -c 'echo $PATH'` 可能仍有默认 PATH，那是 sh 自己补的，不是 lit 加回来的。

两个限制（第 680–697 行）：

- `env FOO=1 echo hi` → `Error: 'env' cannot call 'echo'`，127。lit 不允许 `env` 包内置命令。
- `cd %t | cat` → `Unsupported: 'cd' cannot be part of a pipeline`，127。内置命令不能进管道（`echo` 除外，它会转为外部）。

## 8.5 外部命令怎么被找到

`TestRunner.py` 第 770–784 行：

```python
if args[0].startswith("."):
    exe_in_cwd = os.path.join(cmd_shenv.cwd, args[0])
    if os.path.isfile(exe_in_cwd): executable = exe_in_cwd
if not executable:
    path = cmd_shenv.env["PATH"] if "PATH" in cmd_shenv.env else shenv.env["PATH"]
    executable = lit.util.which(args[0], path)
if not executable:
    raise InternalShellError(j, "%r: command not found" % args[0])
```

1. `PATH` 来自当前 `shenv.env`，所以 `export PATH=...` 立刻影响下一条命令。
2. `.` 开头的命令先用 `shenv.cwd` 拼路径，支持 `./tool`。
3. 找不到就抛 `InternalShellError` → **127**，消息 `'name': command not found`。

```text
# RUN: mkdir -p %t.bin
# RUN: printf '#!/bin/sh\necho tool-ran\n' > %t.bin/mytool
# RUN: chmod +x %t.bin/mytool
# RUN: export PATH=%t.bin
# RUN: mytool
# | tool-ran
```

这里 `printf`、`chmod` 都是外部命令；`export PATH` 之后 `mytool` 从新 PATH 被找到。

## 8.6 工作目录与 `%t`

默认 cwd 不是 suite 根，也不是 `%t`，而是 `test_exec_root/<测试文件在 suite 里的目录部分>`。`getTempPaths()`（第 1333 行）：

```python
execpath = test.getExecPath()
execdir, execbase = os.path.split(execpath)
tmpDir = os.path.join(execdir, "Output")
tmpBase = os.path.join(tmpDir, execbase)
```

真实输出（suite 根 `/tmp/litlab.ZyXTya`）：

```text
# RUN: pwd          → /tmp/litlab.ZyXTya/Output/tests
# RUN: echo %t      → /tmp/litlab.ZyXTya/Output/tests/Output/cwd0.test.tmp
# RUN: echo %s      → /tmp/litlab.ZyXTya/tests/cwd0.test
```

如果测试在 `tests/sub/` 下，cwd 变成 `.../Output/tests/sub`，`%t` 也跟着变成 `.../Output/tests/sub/Output/...`。所以：

- `cwd` = `Output/tests`（源码目录在 exec_root 下的镜像）。
- `%t` = `cwd/Output/<basename>.tmp`，**比 cwd 多一层 `Output/`**。
- `%s` 永远指向源码文件，不在 exec_root 下。

`cd` 只改当前测试的 `shenv.cwd`；LLVM 测试常见的 `rm -rf %t && mkdir %t && cd %t` 就是在这个前提下工作的。

## 8.7 `not`：退出码取反，但「崩溃」是另一回事

`llvm/utils/not/not.cpp` 只有 83 行：

| 场景 | 返回值 |
|---|---|
| `not cmd`，`cmd` 退出 0 | 1 |
| `not cmd`，`cmd` 退出非 0（未崩溃） | 0 |
| `not cmd`，`cmd` 被信号杀死 | 1 |
| `not --crash cmd`，`cmd` 崩溃 | 0 |
| `not --crash cmd`，`cmd` 正常退出（含非 0） | 1 |
| `not` 找不到 `cmd` | 1，打印 `unable to find 'cmd' in PATH` |
| `not`（无子命令） | 1，静默 |

关键代码：

```cpp
if (argc == 0) return 1;
auto Program = sys::findProgramByName(argv[0]);
if (!Program) { ...; return 1; }
int Result = sys::ExecuteAndWait(...);
if (Result < 0) {              // 被信号杀死
  if (ExpectCrash) return 0;
  return 1;
}
if (ExpectCrash) return 1;
return Result == 0;
```

`--crash` 还会设置 `LLVM_DISABLE_CRASH_REPORT`、`LLVM_DISABLE_SYMBOLIZATION` 并禁用 core dump（第 34–48 行）。

真实结果：

```text
# RUN: not true                                  → FAIL 1
# RUN: not nonexistent-command-xyz               → 127，command not found
# RUN: %not --crash sh -c 'kill -SEGV $$'        → PASS，stderr 有 error: Segmentation fault
# RUN: %not --crash true                         → FAIL 1
```

注意：在 lit 内置 shell 里，`not` 前缀由 lit 处理，所以「子命令找不到」是 lit 的 127，不是 `not.cpp` 的 1。直接跑 `/path/to/not missing` 才会看到 `error: unable to find 'missing' in PATH` 和退出码 1。

### 内置 shell 对 `not` 的特殊处理

`_executeShCmd` 第 642–654 行在解析阶段吃掉命令前缀 `not` 和 `!`：

- `not cmd` / `! cmd`：不启动外部 `not`，执行 `cmd` 后取反退出码。
- `not --crash cmd`：把 `not --crash` 放回参数列表，交给**外部** `not`。

所以：

```text
# RUN: not --crash sh -c 'kill -SEGV $$'
```

如果 `not` 不在 PATH 里，报错来自 lit：`'not': command not found`，127。LLVM suite 能直接写 `not --crash`，是因为 `llvm_tools_dir` 在 PATH 里；自建 suite 要么把 `not` 放进 PATH，要么用 `%not` 替换。

> `not --crash` 不能包**内置**命令。`not --crash echo hi`（裸 `not`）实测：
>
> ```text
> # | Error: 'not --crash' cannot call 'echo'
> # error: command failed with exit status: 127
> ```
>
> 写成 `%not --crash echo hi` 时，lit 看到的是外部路径，限制不触发；它会让外部 `not --crash` 去跑外部 `/usr/bin/echo`，正常退出后返回 1。

## 8.8 `true` / `false` / `test` / `cat` 到底是谁

不要猜，用实验：

```text
# RUN: type true
# | 'type': command not found
# error: command failed with exit status: 127
```

`type` 是 bash 内置命令，lit 没有；它被当外部程序，PATH 里也没有，所以 127。这条同时证明「lit 不是 bash」。

```text
# RUN: which cat       → /usr/bin/cat
# RUN: which true      → /usr/bin/true
# RUN: test 1 -eq 1
# RUN: test ! -e %t.nope
```

- `true` / `false` / `test`：**外部程序**，来自 `/usr/bin`。
- `cat`：**lit 的 pipeline builtin**（第 601–604 行），在进程内执行。`which cat` 只能说明磁盘上有 `/usr/bin/cat`，不能说明 lit 用了它；真正的证据是错误信息：

```text
# RUN: cat %t.missing
# | [Errno 2] No such file or directory: '/tmp/.../catbuiltin.test.tmp.missing'
```

GNU cat 会说 `cat: /path: No such file or directory`；这里是 Python 的 `[Errno 2]`，因为 `builtin_commands/cat.py` 用 `open()` 读文件并把异常字符串写进 stderr。`diff` 同理，也是 pipeline builtin（`diff.py` 348 行）。`cat` 只支持 `-v`/`--show-nonprinting`，不是 GNU cat 的完整替代。

## 踩雷集锦

| 错误直觉 | 正确认识 |
|---|---|
| 「`cd` 失败会返回非 0」 | `cd` 总是返回 0；目录不存在时下一条外部命令的 `Popen` 才失败，退出 127。 |
| 「`cd` 会改变整个 lit 进程的工作目录」 | 只改当前测试的 `ShellEnvironment.cwd`；每个测试一份，互不影响。 |
| 「`export` 之后 `$FOO` 就能展开」 | shell 不做变量展开；`export` 只是把 `FOO` 放进子进程 env。 |
| 「`export A=1 B=2` 和 bash 一样」 | 只接受一个参数，多一个就 127。批量设置用 `env A=1 B=2 cmd`。 |
| 「内置 `echo -e` 和 GNU 一样」 | 只支持 `-e`、`-n` 且不能组合；管道里的 echo 才是 GNU echo。 |
| 「`mkdir` 已存在会失败」 | 非 `-p` 分支用 `exist_ok=True`，目录已存在仍返回 0。 |
| 「`rm dir` 失败说明目录还在」 | POSIX 上本版本会报错（退出 1）但**仍然删除**，这是实现缺口。 |
| 「`umask` 对整个测试生效」 | 只包住外部进程的 `Popen`；内置 `mkdir` 和 shell 重定向用 lit 进程自己的 umask。 |
| 「`type` 能查命令类型」 | `type` 是 bash builtin，lit 没有；用 `which` 或直接实验。 |

## 重点整理

- 每个测试一个 `ShellEnvironment`，`cd`/`export` 只在这个测试内持久。
- 内置命令：`cd`、`pushd`、`popd`、`export`、`echo`、`mkdir`、`rm`、`umask`、`ulimit`、`:`；pipeline builtin：`cat`、`diff`。
- `echo` 只有独立成命令时才内置；进管道后是外部 GNU echo，`-ne` 等行为随之改变。
- `export` 一次一个 `KEY=VALUE`；`env` 支持多条、`-u`、`-i`，但不能包内置命令。
- 外部命令按 `shenv.env["PATH"]` 解析，`.` 开头按 `shenv.cwd` 解析；找不到是 127。
- 默认 cwd 是 `test_exec_root/<测试目录>`；`%t` 是 `cwd/Output/<name>.tmp`；`%s` 永远在源码树。
- `umask`/`ulimit` 只作用于外部子进程。
- `not` 是外部程序，`not --crash` 靠退出码是否为负判断崩溃；内置 shell 只特殊处理不带 `--crash` 的 `not`/`!` 前缀。

## 自我检核

不看笔记，试着回答：

- [ ] 为什么 `cd /nonexistent` 本身退出 0，而下一条 `touch` 退出 127？这两步分别在哪里失败？
- [ ] `export FOO=bar` 之后，为什么 `echo $FOO` 是字面量，而 `sh -c 'echo $FOO'` 是 `bar`？谁做了展开？
- [ ] `echo -ne "x\ny"` 与 `echo -ne "x\ny" | cat` 的输出为什么不同？分派逻辑在哪一行？
- [ ] `umask 077` 之后，`touch f`、`mkdir d`、`echo x > f` 创建出来的权限分别是什么？为什么？
- [ ] `not cmd`、`not --crash cmd`、`! cmd` 三者的退出码规则各是什么？`not --crash` 为什么需要 PATH 里有 `not`？
- [ ] `cat` 是外部命令还是内置命令？你能用一条错误信息证明吗？

## 延伸阅读

- **[`llvm/utils/lit/lit/InprocBuiltins.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/InprocBuiltins.py)**
  - 读什么：`executeBuiltinCd`、`executeBuiltinExport`、`executeBuiltinEcho`、`executeBuiltinRm`、`executeBuiltinUmask`、`executeBuiltinUlimit`，全文 316 行。
  - 能学到：每个内置命令的参数校验与失败路径；`rm` 的缩进缺口就在这里。
  - 前提：读完本章 8.3。
- **[`llvm/utils/lit/lit/ShellEnvironment.py`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/lit/ShellEnvironment.py)**
  - 读什么：`ShellEnvironment`、`updateEnv()`、`processRedirects()`、`expand_glob()`。
  - 能学到：env/cwd/umask 的存储方式，以及重定向文件相对谁解析。
  - 前提：知道 `dict` 和 `os.path.join`。
- **[`llvm/utils/not/not.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/utils/not/not.cpp)**
  - 读什么：全文 83 行，重点是 `Result < 0`、`ExpectCrash`、`return Result == 0`。
  - 能学到：`not` 的精确语义，以及为什么 `--crash` 要禁用崩溃报告和 core dump。
  - 前提：知道 Unix 退出码与信号的基本区别。
- **[LLVM Testing Infrastructure Guide — Extra tools](https://llvm.org/docs/TestingGuide.html#extra-tools)**
  - 读什么：`not`、`count`、`split-file`、`FileCheck` 的官方段落。
  - 能学到：这些工具在真实测试里的推荐用法；对照本章看哪些细节文档没写。
  - 前提：跑过一条 `opt | FileCheck` 的测试。

> 下一章把 lit、shell、被测程序、FileCheck 四层拆开：一条 `opt | FileCheck` 失败时，退出码到底是谁的，`CHECK` 又该找谁负责。

→ [Ch 9 FileCheck 与 lit 的边界](./09-filecheck-boundary.md)
