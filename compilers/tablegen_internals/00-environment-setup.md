# Ch 0 环境、版本与最小可复现实验

> **本章目标**：把 `llvm-tblgen` 变成你能随手改、随手验证的实验台；知道这个版本支持什么、不支持什么；掌握全书要反复用的四个观察手段（`-print-records`、`--dump-json`、`-time-phases`、`--write-if-changed`），并亲手复现一次崩溃，理解「TableGen 不是黑箱，它会崩」。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`），Ubuntu 24.04 x86_64，`/usr/bin/c++`（GCC），Release 构建，`LLVM_ENABLE_ASSERTIONS=OFF`。

---

## 0.1 为什么 TableGen 值得一套独立的学习环境？

LLVM 里绝大多数子系统要跑起来，得先有一个 target、一段 IR、一次 codegen。TableGen 不一样：**它是一个独立的、只有十几万行的编译器前端**，输入是 `.td` 文本，输出是任意文本。你不需要 clang，不需要 `llc`，不需要知道 AArch64 编码。

这对学习意味着什么？

| 学习对象 | 最小验证代价 | 能观察到什么 |
| --- | --- | --- |
| 改 `TGParser.cpp` 的语法 | 编 `llvm-tblgen` + 跑一个 10 行 `.td` | 错误信息、`-print-records` 的结构变化 |
| 改 `Record.cpp` 的求值 | 同上 | 字段值、folding 结果、assert 触发点 |
| 写一个新 backend | 同上 + 30 行 C++ | 生成的完整文本输出 |
| 加一条语言特性 | 同上 + lit 测试 | lexer/parser/Init/fold 四层是否一致 |

而改一个 codegen 相关的后端，验证一次往往要重新构建 `llc`、跑 MIR、比对汇编。**TableGen 是 LLVM 里性价比最高的源码阅读目标之一**，这也是本课选它的理由。

## 0.2 构建：只需要 `llvm-tblgen`

LLVM 的 CMake 允许你只构建一个可执行文件。本课的最小配置：

```bash
# 在 llvm-project 根目录
cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AArch64;X86" \
  -DLLVM_ENABLE_PROJECTS="" \
  -DLLVM_INCLUDE_TESTS=ON

ninja -C build llvm-tblgen
```

几个选项的理由：

- **`-DLLVM_ENABLE_ASSERTIONS=ON`**：TableGen 里有大量 `assert` 用来守护「内部不变量」。读源码时，assertion 构建会让违反不变量的代码立刻炸在出错点，而不是几万行之后产生错误输出。这是读实现时最有效的定位工具，**强烈建议开启**。
- **`-DLLVM_TARGETS_TO_BUILD="AArch64;X86"`**：只影响能跑哪些 target 的 `.td` 实验（本课用这两个），不影响 `llvm-tblgen` 本身。想省时间可以只留一个。
- **`-DLLVM_ENABLE_PROJECTS=""`**：不构建 clang/mlir。本课所有实验都在 `llvm-tblgen` 上完成。
- **`Release`**：`Debug` 也能用，但 `lib/TableGen` 的实现在 Debug 下慢 5–10 倍，跑 AArch64 全量 `.td` 会从 1 秒变成十几秒，影响「快速试错」的节奏。

构建完成后确认：

```console
$ ./build/bin/llvm-tblgen --version
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build.
```

> **注意**：`--version` 不会告诉你 assertions 开没开。用 `llvm-config` 确认：
>
> ```console
> $ ./build/bin/llvm-config --assertion-mode --build-mode --version
> OFF
> Release
> 24.0.0git
> ```
>
> 本机实测输出是 `OFF`——也就是说这台机器的构建**没有** assertions。这直接导致下面 0.4 节的 `-stats` 不可用，以及某类内部错误会静默变成「输出不对」而不是崩溃。**先确认这一点，否则你会在 Ch 20 的测试里困惑为什么某个测试被 skip。**

## 0.3 四个观察手段

### 0.3.1 `-print-records`：看「语义」，不看「文本」

这是 TableGen 的默认 backend。它把内存里的 `RecordKeeper` 重新打印成 `.td` 风格，但**所有表达式都已求值**：

```td
class Foo<int n> { int N = n; string S = "foo"; }
def A : Foo<3>;
```

```console
$ llvm-tblgen -print-records a.td
------------- Classes -----------------
class Foo<int Foo:n = ?> {
  int N = Foo:n;
  string S = "foo";
}
------------- Defs -----------------
def A {	// Foo
  int N = 3;
  string S = "foo";
}
```

读法有三个要点：

1. `class` 段打印的是**模板本体**，未绑定的参数显示为 `?`，并且名字被限定成 `Foo:n`。
2. `def` 段打印的是**实例**，`// Foo` 是它的直接父类。
3. **任何 `!add`、`!if`、`!foreach` 都已经折叠成常量**。这就是为什么它能当「求值结果断言的工具」——上游测试大量依赖它。

> 这就是 `-print-records` 的全部意义：它不保证输出是合法 `.td`（有 `?`、有 `// ` 注释），它保证的是**内部状态的忠实投影**。

### 0.3.2 `--dump-json`：看「结构」，用程序处理

当你需要写脚本分析、或者想让断言精确到「这个字段的名字和位置」时，用 JSON：

```console
$ llvm-tblgen --dump-json a.td
{"!instanceof":{"Foo":["A"]},"!tablegen_json_version":1,"A":{"!anonymous":false,
"!fields":[],"!locs":["a.td:2"],"!name":"A","!superclasses":["Foo"],"N":3,
"S":"foo"}}
```

（实际输出是一行，这里为可读性折行。）

关键字段：

| 字段 | 含义 | 为什么有用 |
| --- | --- | --- |
| `!locs` | 记录被实例化的位置 | 写诊断、定位「这条规则来自哪个 `defm`」 |
| `!superclasses` | 直接父类 | 判断继承链，比 `-print-records` 的注释精确 |
| `!fields` | 该记录自己定义的字段（不含继承） | 区分「继承来的」和「自己覆盖的」 |
| `!instanceof` | 类 → 实例列表 | 反查某个类的所有实例 |

这也是上游测试 `llvm/test/TableGen/*.td` 的常用断言方式（`--dump-json | FileCheck`）。

### 0.3.3 `-time-phases`：看「时间花在哪」

```console
$ llvm-tblgen -time-phases -I llvm/include -I llvm/lib/Target/AArch64 \
    -gen-instr-info -o /tmp/x.inc llvm/lib/Target/AArch64/AArch64.td
   ---User Time---   --System Time--   --User+System--   ---Wall Time---  --- Name ---
   0.4817 ( 68.7%)   0.0679 ( 77.6%)   0.5496 ( 69.6%)   0.5496 ( 69.6%)  Parse, build records
   0.1163 ( 16.6%)   0.0120 ( 13.7%)   0.1283 ( 16.3%)   0.1283 ( 16.3%)  Emit map table
   ...
   0.7016 (100.0%)   0.0876 (100.0%)   0.7892 (100.0%)   0.7892 (100.0%)  Total
real	0m10.361s
```

**这里有一个必须现在讲清楚的陷阱**：表格说总共 0.79 秒，`time` 说 10.36 秒。差的 9.5 秒去哪了？

答案是：**`-time-phases` 只覆盖它自己插桩的区段**。`InstrInfoEmitter` 的构造函数（`CDP(R, /*ExpandHwMode=*/false)`）要分析 AArch64 的全部 DAG pattern，这段时间在 backend timer 开始之前就发生了，表里完全看不到。同一台机器上 `--null-backend`（只解析、不发射）只要 0.61 秒，说明这 9.5 秒确实是 backend 对象构造与 pattern 分析的成本。

> 这条经验本课会反复用到：**profile 工具的盲区，比它显示的数字更重要**。Ch 21 会用完整方法把这段补上。

### 0.3.4 `--write-if-changed`：增量构建的基础

```console
$ llvm-tblgen -print-records -o out.inc a.td && stat -c '%y' out.inc
2026-10-29 14:53:04.213717652 +0800
$ sleep 1
$ llvm-tblgen -print-records --write-if-changed -o out.inc a.td
$ stat -c '%y' out.inc
2026-10-29 14:53:04.213717652 +0800     # mtime 未变
```

文件没被重写，mtime 保持不变，下游的 `ninja` 就不会重编。LLVM 的 `TableGen.cmake` 默认给所有调用加这个参数（Ch 15 详述）。**副作用是：你改了 backend 但输出恰好一致时，构建系统不会重编依赖它的 `.cpp`**——调试「我明明改了代码」时先想到这条。

## 0.4 这个版本支持什么？

### 0.4.1 列全部 backend

`llvm-tblgen --help` 的 `Action to perform:` 段就是后端注册表，本机 `llvm-tblgen` 有 48 个 action 选项（其中 42 个是 `--gen-*`）。最常用的：

| 选项 | 生成物 |
| --- | --- |
| `--print-records` | 记录全量打印（默认） |
| `--dump-json` | 记录的 JSON 表示 |
| `--gen-instr-info` | 指令枚举、`MCInstrDesc`、操作数信息 |
| `--gen-register-info` | 寄存器类与寄存器信息 |
| `--gen-dag-isel` | SelectionDAG 的 matcher table |
| `--gen-global-isel` | GlobalISel 的 matcher table |
| `--gen-asm-matcher` / `--gen-asm-writer` | 汇编解析/打印 |
| `--null-backend` | 什么都不做（测 parse 成本） |

> **注意**：`-print-records` 用单横线、`--gen-*` 用双横线都能工作（LLVM 的 `cl` 接受两者），但源码里注册的字符串是 `"gen-instr-info"` 和 `"print-records"`，不带横线。Ch 13 会讲这个注册机制。

### 0.4.2 这个版本的 bang operator 清单

TableGen 的语言在演进。判断「某个语法在你的版本上能不能用」最可靠的方式是读 lexer 的 `StringSwitch`——那就是语法清单本身（`llvm/lib/TableGen/TGLexer.cpp:630` 起）。本机版本的完整清单：

```text
eq ne le lt ge gt if cond switch isa head tail size con dag
add sub mul div not logtwo and or xor shl sra srl
cast empty subst foldl foreach filter
listconcat listflatten listsplat listremove range sort
strconcat initialized interleave instances substr find
setdagop getdagop setdagopname getdagopname
getdagarg getdagname setdagarg setdagname
exists tolower toupper repr match
```

**注意清单里没有 `mod`**。这是一个真实且常见的坑：

```console
$ echo 'def X { int A = !mod(17, 5); }' > m.td && llvm-tblgen m.td
m.td:1:11: error: unknown operator
def X { int A = !mod(17, 5); }
          ^
m.td:1:11: error: Unknown or reserved token when parsing a value
m.td:1:11: error: expected ';' after declaration
```

想做取模，本版本要用 `!sub(a, !mul(!div(a, b), b))`，或者干脆绕开。这条实验也告诉你：**「文档里有」不等于「你的版本有」**，`ProgRef.md` 在主干上是滚动的。

### 0.4.3 整数与位的字面量规矩

```console
$ echo 'def X { int A = 1_000; }' > u.td && llvm-tblgen u.td
u.td:1:17: error: Variable not defined: '1_000'
$ echo 'def X { int A = 0x1_0; }' > u2.td && llvm-tblgen u2.td
u2.td:1:20: error: expected ';' after declaration
```

**本版本不支持数字下划线分隔符**。`0b1010_1010` 也一样会报错：

```console
$ echo 'def X { bits<8> B = 0b1010_1010; }' > b.td && llvm-tblgen b.td
b.td:1:23: error: Field 'B' of type 'bits<8>' is incompatible with value
'{ 1, 0, 1, 0 }' of type bit initializer with length 4
b.td:1:29: error: expected ';' after declaration
```

注意这个错误信息的细节：lexer 把 `0b1010` 当成了完整的二进制字面量（长度 4），剩下 `_1010` 被当成下一个 token。**错误信息本身泄露了 lexer 的行为**——这正是 Ch 2 要讲的读法。

## 0.5 亲手复现一次崩溃：自包含文件

现在做一个「不要模仿」的实验，因为它揭示了 TableGen 一个真实的健壮性缺口：

```console
$ printf 'include "cyc.td"\n' > cyc.td
$ llvm-tblgen cyc.td
PLEASE submit a bug report to https://github.com/llvm/llvm-project/issues/ ...
Stack dump:
0.	Program arguments: .../llvm-tblgen cyc.td
  #0 llvm::sys::PrintStackTrace(...)
  #1 llvm::sys::SignalHandler(...)
  #2 ...
```

退出码：

```console
$ llvm-tblgen cyc.td >/dev/null 2>&1; echo $?
139
```

139 = 128 + 11 = SIGSEGV。

为什么会这样？看 `TGLexer::LexInclude()`（`llvm/lib/TableGen/TGLexer.cpp:469`）：

```cpp
CurBuffer = SrcMgr.AddIncludeFile(Filename, SMLoc::getFromPointer(CurPtr),
                                  IncludedFile);
if (!CurBuffer) {
  PrintError(getLoc(), "could not find include file '" + Filename + "'");
  return true;
}
```

`SourceMgr::AddIncludeFile` 只关心「文件能不能打开」（Ch 2 会看完整实现），**没有记录「这个文件已经在 include 栈上」**。于是 `include "cyc.td"` 打开自己、从头开始 lex、又碰到 `include`、再打开自己……递归到栈耗尽。而 `PrepIncludeStack` 在 `TGLexer::LexInclude` 里只做了 `emplace_back()`，没有任何去重检查。

这个例子是本课的方法论样本：

1. **先跑出现象**（segfault）。
2. **读最小相关代码**（`LexInclude` 20 行）。
3. **找出不变量缺失**（include 路径没有环检测）。
4. **判断这是 bug 还是设计**（TableGen 的 `.td` 通常不循环 include，所以没人加；但它确实是健壮性问题）。

`include` 环检测至今不在上游（截至本 commit），属于「可以提交的改动」之一——但 Ch 22 会讲为什么这类补丁需要的不是 3 行代码，而是错误信息设计、现有测试兼容性和一份说明「为什么以前不需要现在需要」的 RFC。

## 0.6 委托与目录约定

本课的实验目录建议：

```text
~/tg-lab/
├── build/              # 你自己的 llvm-project 构建
├── src/llvm-project/   # 源码（git clone，能 git log/blame）
└── scratch/            # 所有 .td 实验
```

`git blame` 在 TableGen 的源码阅读里价值极高：`Record.h` 里每个 `Init` 子类的来历、`TGParser.cpp` 某个分支是哪个 PR 加的，都能直接看到讨论上下文。**不要读 tarball 解压出来的源码。**

给 `.td` 加语法高亮的快速方案（官方支持）：

- Emacs：`llvm/utils/emacs/tablegen-mode.el`
- Vim：`llvm/utils/vim/syntax/tablegen.vim`
- ctags：`llvm/utils/TableGen/tdtags`（给 target `.td` 建索引）

## 0.7 本章检查清单

关掉这一章之前，确认你能做到：

- [ ] 说清楚为什么本课用 `Release + assertions` 而不是 `Debug`
- [ ] 用 `llvm-config --assertion-mode` 判断手上二进制有没有 assertions，并知道断言关掉会有什么后果
- [ ] 用 `-print-records` 证明 `!add(1,2)` 被折叠成了 `3`
- [ ] 用 `--dump-json` 找出某个 `def` 的 `!locs` 和 `!superclasses`
- [ ] 解释「`-time-phases` 显示 0.79s、`time` 显示 10.36s」的原因
- [ ] 解释 `--write-if-changed` 为什么可能让 ninja 不重编
- [ ] 说出本版本**没有** `!mod`，并给出替代写法
- [ ] 复现 `include` 自包含的 segfault，并指出 `LexInclude` 里缺失的那个检查

## 0.8 踩雷记录

**错误直觉 1：`--version` 能告诉我构建配置。**
正确认识：`--version` 只打印版本号和 `Optimized build.`。assertions、targets、编译器都要用 `llvm-config` 或 `CMakeCache.txt` 查。本机就是 `Release + NO assertions`，和「标准上游 CI 配置」不同。

**错误直觉 2：`-time-phases` 的总和就是进程总耗时。**
正确认识：它只统计显式插桩的区段。backend 构造函数、`RecordKeeper` 析构、输出写盘都可能完全在表外。看到「表里 0.79s、实际 10.4s」不是工具坏了，是你漏了一个阶段。

**错误直觉 3：文档里有的语法，我的版本就一定有。**
正确认识：`ProgRef.md` 是主干滚动文档。判断依据是 `TGLexer.cpp` 的 `StringSwitch`（bang operator）和 `TokKind` 枚举（关键字）——它们才是这个 commit 支持什么的唯一权威。

**错误直觉 4：TableGen 遇到坏输入总会给诊断。**
正确认识：`include` 环检测缺失，直接 segfault（exit 139）。读源码时遇到「假设输入合法」的地方，要意识到这是潜在 bug 面。

**错误直觉 5：`-print-records` 的输出是合法 `.td`。**
正确认识：它含 `?`、`// Foo`、`Foo:n` 这类只用于展示的写法，回灌给 `llvm-tblgen` 会失败。它是**观察工具**，不是序列化格式；要机器可读请用 `--dump-json`。

## 0.9 延伸阅读

- **`llvm/docs/TableGen/index.md`**（约 305 行）
  - 官方 Overview。读「The TableGen program」一节，注意它对 `-o` 多文件与 stdin 的说明，和本章 0.3.4、0.5 的实验对照。
- **`llvm/lib/TableGen/Main.cpp`**（241 行）
  - 整个工具的 `main` 流程：解析参数 → 读文件 → `TGParser::ParseFile()` → 选 backend → 写输出。**这是全课最短、信息密度最高的文件**，现在就可以通读一遍，Ch 1 会逐段展开。
- **`llvm/lib/TableGen/TGTimer.cpp`**（54 行）
  - `-time-phases` 的实现，解释了为什么「表外时间」存在：`TGTimer` 只在 `startTimer` 之间计时，构造函数里的工作完全不计。
- **`llvm/docs/CommandGuide/tblgen.md`**
  - `*-tblgen` 系列命令的正式文档，包含 `llvm-tblgen`、`mlir-tblgen`、`clang-tblgen` 的关系。
- **`llvm/utils/TableGen/README.md`**
  - 官方给的 hello-world 例子与 `-print-records` 输出。短，但正好是本章 0.3.1 的最小复现。

## 下一章

Ch 1 把 `llvm-tblgen` 拆成三段：**词法/语法前端** → **`RecordKeeper` 数据模型** → **backend 发射**。你会看到 `Main.cpp` 的 100 行如何决定后面 22 章的全部结构。

[Ch 1 TableGen 全景：前端、RecordKeeper、backend 三段式](./01-tablegen-architecture.md)
