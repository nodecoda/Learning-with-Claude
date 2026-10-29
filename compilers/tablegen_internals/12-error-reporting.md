# Ch 12 错误报告基础设施：`PrintError`、`SMLoc`、`SourceMgr`

> **本章目标**：读通 186 行的 `Error.cpp` 与 66 行的 `Error.h`，能回答「为什么这条诊断没有位置」「为什么收不到汇总行」「为什么我的 `PrintError` 让进程段错误」；能自己写出符合上游规范的诊断，并知道什么时候必须用 `PrintFatalError`。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。主构建是 `/home/ubuntu/llvm/llvm-dev/build`（Release + `LLVM_ENABLE_ASSERTIONS=OFF`）；12.3 额外用了同 commit 的 `/home/ubuntu/llvm/build-mv2`（Release + assertions）来对照 assert 文本。

---

## 12.1 为什么 TableGen 的诊断系统值得单独一章

一个真实的场面：你在 `InstrInfoEmitter.cpp` 里加了一条检查，写

```cpp
PrintError("instruction " + Instr.getName() + " has duplicate encoding");
```

编译通过，测试跑出 `error: instruction ADDXrr has duplicate encoding`——**没有文件名、没有行号、没有 caret**。用户不知道该改哪个 `.td`。

再惨一点：你好不容易补上 `Instr.getLoc()`，构建成功；CI 却是绿的，退出码 0，坏产物已经写进了 `.inc`。

第三种：你想在一条消息里再带一个「上一次定义在哪里」，顺手写了

```cpp
PrintError("def already exists", Prev->getName());
```

进程直接 `Segmentation fault`。

这三件事都源于同一个设计：**TableGen 的诊断是一个进程级全局设施，只有一条「计数」通道，而且重载集里藏着一个会段错误的签名**。本章把这三件事逐一拆开。

## 12.2 只有 186 行的全局设施

`llvm/lib/TableGen/Error.cpp` 全文 186 行，`Error.h` 66 行。核心只有三个符号（`Error.cpp:24`）：

```cpp
SourceMgr llvm::SrcMgr;          // 进程级，全局唯一
unsigned llvm::ErrorsPrinted = 0; // 进程级计数器
```

`SrcMgr` 是 `llvm::SourceMgr` 的实例，负责「把 `SMLoc`（一个裸指针）翻译成文件名:行:列 + 源码行 + caret」。`ErrorsPrinted` 是计数器。

所有诊断最终都汇到同一个静态函数（`Error.cpp:27`）：

```cpp
static void PrintMessage(ArrayRef<SMLoc> Locs, SourceMgr::DiagKind Kind,
                         const Twine &Msg) {
  // Count the total number of errors printed.
  // This is used to exit with an error code if there were any errors.
  if (Kind == SourceMgr::DK_Error)
    ++ErrorsPrinted;

  SMLoc NullLoc;
  if (Locs.empty())
    Locs = NullLoc;
  SrcMgr.PrintMessage(Locs.consume_front(), Kind, Msg);
  for (SMLoc Loc : Locs)
    SrcMgr.PrintMessage(Loc, SourceMgr::DK_Note,
                        "instantiated from multiclass");
}
```

这 15 行的三个细节，决定了后面所有行为：

1. **只有 `DK_Error` 才 `++ErrorsPrinted`**。`DK_Warning`/`DK_Note` 不计数。
2. **`Locs` 是数组，但只有第一个是主诊断**。其余每个都会被强制打成 `note: instantiated from multiclass`——注意这句话是**硬编码的字符串**，不管第二个位置实际上是不是 multiclass。
3. **空数组会被替换成一个「无效 `SMLoc`」**，于是 `SrcMgr` 打印出 `<unknown>:0:`。

第 2 点值得单独看：`Record::getLoc()` 返回的 `ArrayRef<SMLoc>` 按 `Record.h` 的注释是

```cpp
// Location where record was instantiated, followed by the location of
// multiclass prototypes used, and finally by the locations of references to
// this record.
SmallVector<SMLoc, 4> Locs;
```

三种语义完全不同的位置挤在一个数组里，而 `PrintMessage` 只认得「第一个是本体 + 其余是 multiclass」。所以**你没法用 `PrintError(Locs, ...)` 打印两个 `def` 的位置**——第二个会被贴上错误的标签。

实测（自写 backend，读 `defm` 展开后的 `D_x`）：

```console
$ ./multi -mode=rec dm.td
dm.td:3:7: error: via Record
  def _x : Base;
      ^
dm.td:5:10: note: instantiated from multiclass
defm D : M;
         ^
generated
./multi: 1 errors.
exit=1
```

如果硬塞三个位置（`SmallVector<SMLoc>` 手动拼），第三个也会变成同一句 note：

```console
$ ./multi -mode=three dm.td
dm.td:3:7: error: three locs, only first is primary
  def _x : Base;
      ^
dm.td:5:10: note: instantiated from multiclass
defm D : M;
         ^
dm.td:3:7: note: instantiated from multiclass
  def _x : Base;
      ^
./multi: 1 errors.
```

**第三行把「返回原处」也标成了 multiclass**。这是 `PrintMessage` 的实现限制，不是你的 bug——但也说明它是**不可扩展的接口**。

## 12.3 六个 `PrintError` 重载，和一个陷阱

`Error.h:40` 起声明了六个 `PrintError`：

```cpp
LLVM_ABI void PrintError(const Twine &Msg);
LLVM_ABI void PrintError(function_ref<void(raw_ostream &OS)> PrintMsg);
LLVM_ABI void PrintError(ArrayRef<SMLoc> ErrorLoc, const Twine &Msg);
LLVM_ABI void PrintError(const char *Loc, const Twine &Msg);
LLVM_ABI void PrintError(const Record *Rec, const Twine &Msg);
LLVM_ABI void PrintError(const RecordVal *RecVal, const Twine &Msg);
```

一对一实现（`Error.cpp:104`–`128`）：

| 重载 | 是否走 `PrintMessage` | 是否 `++ErrorsPrinted` | 输出形态 |
| --- | --- | --- | --- |
| `PrintError(Twine)` | ❌ 直接 `WithColor::error()` | ❌ | `error: msg`（无位置） |
| `PrintError(function_ref)` | ❌ | ❌ | 同上，回调给你 `raw_ostream` |
| `PrintError(ArrayRef<SMLoc>, Twine)` | ✅ | ✅ | `file:line:col: error: msg` |
| `PrintError(const char *Loc, Twine)` | ✅ | ✅ | 同上 |
| `PrintError(const Record *, Twine)` | ✅ | ✅ | 同上，位置取 `Rec->getLoc()` |
| `PrintError(const RecordVal *, Twine)` | ✅ | ✅ | 同上，位置取 `RecVal->getLoc()` |

**第一个陷阱：`PrintError(Twine)` 不计入 `ErrorsPrinted`。**

这意味着 `llvm-tblgen` 的退出码是 0，且产物文件会被**正常写出**。实测（`silent` backend 故意在 emitting 时调 `PrintError(Twine)` 后继续输出）：

```cpp
static bool emit(raw_ostream &OS, const RecordKeeper &RK) {
  PrintError("backend error without location");
  OS << "int table[3] = {1,2,3};\n";
  return 0;
}
```

```console
$ ./silent -mode=twine c.td -o s1/o.inc
error: backend error without location
exit=0
$ cat s1/o.inc
int table[3] = {1,2,3};
```

**退出码 0，产物完整写出。** 换成带位置的重载，同一份 backend 立刻变成：

```console
$ ./silent -mode=loc c.td -o s2/o.inc
c.td:2:5: error: backend error with location
def A : C;
    ^
./silent: 1 errors.
exit=1
$ ls s2/o.inc
ls: cannot access 's2/o.inc': No such file or directory
```

（注意 `-o -` 时例外：`ToolOutputFile` 对 `"-"` 直接用 `outs()`，析构不删任何东西，所以**即使 `ErrorsPrinted > 0`，stdout 上仍能看到内容**。实测 `./silent -mode=loc c.td -o -` 会打出 `int table[3] = {1,2,3};` 且 `exit=1`。写 CI 脚本时别用 `-o -` 来判断 backend 是否成功。）

原因在 `Main.cpp:119` 的 `WriteOutput`：

```cpp
OutFile.os() << Content;
if (ErrorsPrinted == 0)
  OutFile.keep();     // ← 只有 0 才 keep，否则 ToolOutputFile 析构时删除文件
```

`ToolOutputFile` 的析构会 `sys::fs::remove(Filename)`（除非 `keep()`）。所以**「有没有位置」直接决定「产物存不存在」**——一个非常隐蔽的耦合。

**第二个陷阱：`PrintError("a ", "b")` 会段错误。**

看这行调用：

```cpp
PrintError("hello ", "world");
```

看起来是「消息 + 后缀」，实际匹配到的是 `PrintError(const char *Loc, const Twine &Msg)`——第一个 `const char*` 被当成**源码位置指针**。`SMLoc::getFromPointer` 只存指针不校验，随后 `SourceMgr::FindBufferContainingLoc` 遍历所有 buffer 做指针区间比较，找不到就 `assert(CurBuf && "Invalid or unspecified location!")`（`SourceMgr.cpp:325`）。本机构建 `LLVM_ENABLE_ASSERTIONS=OFF`，assert 被编译掉，于是 `getBufferInfo(0)` 越界读 → **段错误**。

```console
$ ./trap c.td
Segmentation fault (core dumped)
exit=139
```

若在 assertions 构建上（本机另一份 `Release + assertions` 构建 `/home/ubuntu/llvm/build-mv2`，同 commit），**实测**得到的是同一处逻辑的另一条 assert：

```console
$ ./trap_assert c.td
trap_assert: /home/ubuntu/llvm/mv-wt/llvm/lib/Support/SourceMgr.cpp:325:
  SMDiagnostic llvm::SourceMgr::GetMessage(SMLoc, SourceMgr::DiagKind, const Twine &,
    ArrayRef<SMRange>, ArrayRef<SMFixIt>) const:
  Assertion `CurBuf && "Invalid or unspecified location!"' failed.
Aborted (core dumped)  (exit 134)
```

两条路径的差别值得记住：**assertions 构建是 `abort`（SIGABRT，exit 134），Release 构建是越界读（SIGSEGV，exit 139）**。所以你在本地 assertions 构建上看到的标准 assert 文本，在别人 Release 构建上会变成「莫名其妙的段错误」——这不代表 assert 没写好，而是 assert 根本不存在。

**规则**：`PrintError` 的任何「两个参数」调用，第一个参数必须是 `SMLoc`/`ArrayRef<SMLoc>`/`Record*`/`RecordVal*`/`const char* 指向真实缓冲区内的字符`。想拼消息请用 `Twine`/`+`/`#=`，不要写两个字符串字面量。

这种 bug 编译器不会警告——`const char*` 和 `Twine` 都能从字符串字面量构造。这是本章最值得记住的一条。

## 12.4 `SMLoc` 是什么：一个裸指针

`SMLoc`（`SMLoc.h`）的全部内容：

```cpp
class SMLoc {
  const char *Ptr = nullptr;
public:
  constexpr SMLoc() = default;
  constexpr bool isValid() const { return Ptr != nullptr; }
  constexpr const char *getPointer() const { return Ptr; }
  static SMLoc getFromPointer(const char *Ptr) { ... }
};
```

**没有 buffer id、没有 offset、没有行号**——就是一个指向 `MemoryBuffer` 内部的裸指针。所以：

- 它只在「那个 buffer 还活着」时有效。`MemoryBuffer` 由 `SrcMgr` 持有，`SrcMgr` 是全局的，所以整个进程生命周期都有效。
- 跨文件比较没有意义。`FindBufferContainingLoc` 靠**指针落在哪个 buffer 的 `[BufferStart, BufferEnd]` 区间**来判断归属（`SourceMgr.cpp:97`）。
- 传入不在任何 buffer 里的指针 → `assert` 失败（assertions 构建）或越界（Release 构建）。

这也是为什么 `SrcMgr` 必须是全局的：`Init`/`Record` 里存的 `SMLoc` 都是裸指针，任何时刻打印诊断都需要同一个 `SrcMgr` 提供 buffer 集合。

`TGLexer::getLoc()` 就是 `SMLoc::getFromPointer(TokStart)`；`getLocRange()` 返回 `{getLoc(), SMLoc::getFromPointer(CurPtr)}`，即「token 起点 → 当前扫描位置」的半开区间。

## 12.5 `SourceMgr` 的两个能力，TableGen 只用了一个

`SourceMgr::PrintMessage` 有 8 个参数（`SourceMgr.h:240`）：

```cpp
void PrintMessage(raw_ostream &OS, SMLoc Loc, DiagKind Kind, const Twine &Msg,
                  ArrayRef<SMRange> Ranges = {}, ArrayRef<SMFixIt> FixIts = {},
                  bool ShowColors = true) const;
```

而 `Error.cpp` 的 `PrintMessage` 只用了 `(Loc, Kind, Msg)` 三个。**`Ranges` 和 `FixIts` 完全没被暴露**——所以 TableGen 的所有诊断都只能画一个单字符 caret，无法高亮 `def Foo {...}` 整段，也无法给出「did you mean」fixit。

想用就得绕过包装、直接用全局 `SrcMgr`。实测（自写 backend）：

```cpp
SMLoc L = R->getLoc().front();
SMRange Range(L, SMLoc::getFromPointer(L.getPointer() + 3));  // "def"
SrcMgr.PrintMessage(L, SourceMgr::DK_Error, "range + fixit demo",
                    {Range}, {SMFixIt(Range, "record")});
```

```console
$ ./fix ref.td
ref.td:2:5: error: range + fixit demo
def A : C;
    ^~~
    record
```

注意第二行是 `^~~`（`~` 覆盖 range 剩余部分），第三行是 fixit 建议。这正是 `clang` 风格的诊断，TableGen 内部一行都没有。

**这也是一个真实的上游改进点**：`Error.h` 里没有 `PrintError(SMRange, Twine)`，也没有带 fixit 的重载。如果你要做 Final Project 里的「更好的诊断」，把这两个补上是最小、最合理的切入点（`Practice A` 已经列了这条，本章给出实现细节）。

顺带注意一个边界：`SMRange` 的构造函数有 assert 保证 Start/End 同时有效或同时无效；此外 `PrintMessage` 内部对 range 的处理是「只保留与本行相交的部分」，跨行 range 会被裁剪。

## 12.6 计数与退出：四道闸门

`Main.cpp` 里 `ErrorsPrinted` 被读了三次（`145`、`185`、`227`），加上 `PrintMessage` 里的自增，构成完整的生命周期：

```cpp
// ① 解析后：有任何错误就不跑 backend
if (Parser.ParseFile())
  return 1;                       // 语法错误：直接 1，没有汇总行
if (ErrorsPrinted > 0)            // 语义/断言错误：打印汇总后 1
  return reportError(argv0, Twine(ErrorsPrinted) + " errors.\n");

// ② backend 返回非 0
if (status)
  return 1;                        // 没有汇总行

// ③ 写文件时：只有 0 错误才 keep
if (ErrorsPrinted == 0)
  OutFile.keep();

// ④ 全部写完后
if (ErrorsPrinted > 0)
  return reportError(argv0, Twine(ErrorsPrinted) + " errors.\n");
```

于是退出码与输出形态有四种组合，实测如下：

| 场景 | 最后一行 stderr | 退出码 | 产物文件 |
| --- | --- | --- | --- |
| 语法错误（`def A { int X = ; }`） | 无汇总行 | 1 | 无 |
| 语义错误（`int X = "s"`） | `llvm-tblgen: 1 errors.` | 1 | 无 |
| backend 调 `PrintError(Twine)` | 无汇总行 | **0** | **被写出** |
| backend 返回 1（不调任何 Print） | 无任何消息 | 1 | 无 |

第二种尤其要注意：**汇总行 `N errors.` 只在「有位置、被计数」的错误存在时出现**。经验法则——**看到 `N errors.` 说明 `ErrorsPrinted > 0`；看不到就说明要么语法错直接返回、要么有人用了不计数的那两个重载**。

`reportError` 的格式是 `${argv0}: ${Msg}`（`Main.cpp:81`），所以汇总行长这样：

```console
$ llvm-tblgen two.td
two.td:1:17: error: Field 'X' of type 'int' is incompatible with value '"s"' of type 'string'
def A { int X = "s"; }
                ^
two.td:2:5: error: Initializer of 'Y' in 'B' could not be fully resolved: !cast<bit>(3)
def B { bit Y = 3; }
    ^
llvm-tblgen: 2 errors.
```

`argv0` 是 `main` 收到的 `argv[0]` 原文，所以从不同相对路径调用会出现不同的前缀（`./dump: 1 errors.`、`/abs/path/llvm-tblgen: 2 errors.`）。上游测试全用 `FileCheck` 匹配这条消息时必须写 `{{.*}}` 或省略，不能写死 `llvm-tblgen`。

### 全局计数器带来的第二个后果：多次 parse 会累积

`TableGenParseFile`（`Parser.cpp`）是给 MLIR 的 `tblgen-lsp-server` 这类工具用的入口，它**重置 `SrcMgr` 但不重置 `ErrorsPrinted`**：

```cpp
SrcMgr = SourceMgr();
SrcMgr.takeSourceBuffersFrom(InputSrcMgr);
...
InputSrcMgr.takeSourceBuffersFrom(SrcMgr);
SrcMgr = SourceMgr();
```

实测连续 parse 三个文件（`twice` 工具）：

```console
$ ./twice c.td two.td c.td
parse c.td -> ok, ErrorsPrinted=0, defs=1
two.td:1:17: error: ...
two.td:2:5: error: ...
parse two.td -> ok, ErrorsPrinted=2, defs=2
parse c.td -> ok, ErrorsPrinted=2, defs=1
final ErrorsPrinted=2
```

注意两点：

- **`TableGenParseFile` 返回 `ok`，即使 `ErrorsPrinted` 已经是 2**。返回值只反映「语法是否走下去」，不反映「有没有错误」。用它写工具时，判断成功必须查 `ErrorsPrinted`。
- **计数器只增不减**。第二次 parse 一个完全干净的文件，`ErrorsPrinted` 仍是 2。任何长驻进程（LSP、JIT）都必须自己在每次 parse 前后快照/恢复这个全局量。

这也解释了 `Parser.h` 里那句注释：

```cpp
/// NOTE: TableGen currently relies on global state within a given parser
///       invocation, so this function is not thread-safe.
```

## 12.7 `PrintError` 与 `PrintFatalError` 的分界线

上游的分布很能说明问题（实测 `grep` 计数，本 commit）：

| | `lib/TableGen`（11k 行） | `utils/TableGen`（38k 行） |
| --- | --- | --- |
| `PrintError(` | 37 | 113 |
| `PrintFatalError(` | **63** | **469** |
| `PrintWarning(` | 5 | 18 |
| `PrintNote(` | 14 | 27 |

**backend 里 fatal 是 error 的 4 倍**。这不是风格问题，而是语义分界：

- **`PrintError`**：用户能自己修的输入问题——`.td` 写错类型、字段名拼错、断言失败。报完继续，尽量一次报全。
- **`PrintFatalError`**：**程序不变量被破坏**——`DagInit` 里出现不是 dag 的项（`Record.cpp:713` 的 `expected record type for the element with index`）、backend 假设的前提不成立（缺字段、编码宽度不符）。这类情况继续跑只会产生更离谱的输出或直接崩，所以立刻 `std::exit(1)`。

`fatal_exit()` 的实现（`Error.cpp:44`）：

```cpp
[[noreturn]] inline static void fatal_exit() {
  sys::RunInterruptHandlers();   // 跑清理 handler（删临时文件等）
  std::exit(1);
}
```

两个关键点：

1. **走 `std::exit`，不走 `abort`**——所以 `ToolOutputFile` 的析构会执行，临时产物被删掉。实测 part 写在内存里的内容完全丢弃：

   ```console
   $ ./fat dm.td -o -          # backend 先 OS << "partial..." 再 PrintFatalError
   dm.td:3:7: error: fatal with record loc
     def _x : Base;
         ^
   dm.td:5:10: note: instantiated from multiclass
   defm D : M;
            ^
   exit=1
   ```

   `stdout` 上一个字节都没有——因为输出先进 `std::string`，`PrintFatalError` 在 `WriteOutput` 之前就退出了。

2. **`fatal_exit` 不经过 `ErrorsPrinted`**，所以 fatal 之后不会有 `N errors.` 汇总行，也不会有「继续收集」的机会。第一个 fatal 就是最后一个。

判断标准可以简化成一句：**「如果用户把 `.td` 改对了，这个错误会不会消失？」会 → `PrintError`；不会（因为你自己的代码假设错了）→ `PrintFatalError`。**

### `PrintWarning` 的位置参数陷阱

`PrintWarning` 只有三个重载，且**没有 `Record*` 版本**：

```cpp
LLVM_ABI void PrintWarning(const Twine &Msg);
LLVM_ABI void PrintWarning(ArrayRef<SMLoc> WarningLoc, const Twine &Msg);
LLVM_ABI void PrintWarning(const char *Loc, const Twine &Msg);
```

传 `const Record*` 会找不到匹配（`Record*` 不能隐式转 `const char*`），必须写 `PrintWarning(Rec->getLoc(), ...)`。上游实战里只有 5 处用它，其中一条是 `Record::checkUnusedTemplateArgs`（`Record.cpp:3136`）：

```cpp
for (const RecordVal &Arg : getTemplateArgs()) {
  if (!Arg.isUsed())
    PrintWarning(Arg.getLoc(),
                 "unused template argument: " + Twine(Arg.getName()));
}
```

```console
$ llvm-tblgen warn2.td
warn2.td:1:23: warning: unused template argument: C:unused
class C<int used, int unused> { int A = used; }
                      ^
```

**退出码是 0**，产物照常写出（警告不计数）。这个警告可以用 `-no-warn-on-unused-template-args` 关掉，实测关掉后完全静默。

## 12.8 位置是怎么变成 `file:line:col` + caret 的

`SrcMgr.PrintMessage(Loc, Kind, Msg)` 内部（`SourceMgr.cpp:391`）：

```
PrintMessage(Loc, Kind, Msg)
  → GetMessage(Loc, Kind, Msg)          // 定位 → SMDiagnostic
  → PrintMessage(OS, Diagnostic)
       if (Diagnostic.getLoc().isValid()) {
         unsigned CurBuf = FindBufferContainingLoc(...);   // 指针区间查找
         PrintIncludeStack(...);                            // 打印 include 链
       }
       Diagnostic.print(nullptr, OS, ShowColors);
```

`GetMessage`（`SourceMgr.cpp:313`）做的事：

1. `FindBufferContainingLoc(Loc)` 找到 buffer，`assert(CurBuf)`（就是 12.3 段错误的来源）；
2. 从 `Loc.getPointer()` 向前扫到 `\n`/`\r` 取行首、向后扫取行尾 → `LineStr`；
3. `getLineAndColumn(Loc, CurBuf)` 算 1-based 行号、1-based 列号；
4. 构造 `SMDiagnostic`，`ColumnNo` 存的是 `col - 1`（0-based，打印时 +1）。

`Diagnostic.print` 里对 caret 的处理有一个值得注意的限制（`SourceMgr.cpp:575`）：

```cpp
if (any_of(LineContents, isNonASCII)) {
  printSourceLine(OS, LineContents);
  return;                       // ← 非 ASCII 行只打印源码，不画 caret
}
```

**源码行里出现任何非 ASCII 字符（中文注释、全角标点），caret 和 range 全部消失**，只剩源码行。实测：

```console
$ printf 'def A { int X = "s"; } // 中文注释\n' > cn.td && llvm-tblgen cn.td
cn.td:1:17: error: Field 'X' of type 'int' is incompatible with value '"s"' of type 'string'
def A { int X = "s"; } // 中文注释
$ 
```

（只有两行，没有 `^`。）列号也按**字节**算，所以中文注释在错误位置**之后**时列号仍然正确，在之前时会偏。

### include 链

`PrintIncludeStack` 递归打印所有上层 include（`SourceMgr.cpp:288`），实测三层：

```console
$ llvm-tblgen -I lvl2 lvl2/a.td
Included from lvl2/a.td:1:
Included from lvl2/b.td:1:
lvl2/c.td:1:19: error: Field 'X' of type 'int' is incompatible with value '"s"' of type 'string'
def Bad { int X = "s"; }
                  ^
llvm-tblgen: 1 errors.
```

顺序是**最外层在前**（`a.td` 是 main file，最先打印）。stdin 输入时文件名是 `<stdin>`（`SMDiagnostic::print` 特别处理 `Filename == "-"`）。

### 颜色

`SMDiagnostic::print` 用 `ColorMode::Auto`，即「输出是终端才上色」。实测用 `script` 伪造 tty：

```console
^[[1mtwo.td:1:17: ^[[0m^[[0;1;31merror: ^[[0m^[[1mField 'X' ...
^[[0;1;32m                ^^M          ← caret 是绿色
```

caret 用绿色、`error:` 用红色加粗。重定向到文件时无颜色（本课所有 console 输出都是这种）。

## 12.9 改源码时的三条实用规则

**规则一：backend 里的每条错误都要带位置。**

```cpp
// ✗ 错了：不计数，产物照样写出，CI 绿
PrintError("bad encoding for " + Instr.getName());

// ✓ 对了
PrintError(Instr.getLoc(), "bad encoding for " + Instr.getName());
```

如果这个 `Record` 是 `defm` 展开出来的，`getLoc()` 带多个元素，你会自动得到 `instantiated from multiclass` 链——这正是想要的。

**规则二：`RecordVal` 有位置，用它。**

`PrintError(const RecordVal *RecVal, ...)` 是本章最被低估的重载。字段级诊断（「这个字段的值不对」）应该指向**字段那一行**，而不是整个 `def`：`RecVal->getLoc()` 是字段名 token 的位置。`Record::checkUnusedTemplateArgs` 就是这么做的。

**规则三：需要「两个位置」时，别用 `ArrayRef<SMLoc>`。**

第一种位置 = 主诊断，其余 = `instantiated from multiclass`。要表达「A 和 B 冲突」，正确做法是：

```cpp
PrintError(RecA->getLoc(), "duplicate encoding");
PrintNote(RecB->getLoc(), "conflicts with this definition");
```

一次 error + 一次 note，读起来清楚，也符合 clang 的习惯。`TGParser::addDefOne` 就是这么写的（`TGParser.cpp:584`）。

## 12.10 本章检查清单

- [ ] 说出 `Error.cpp` 里三个全局符号，以及谁在写 `ErrorsPrinted`
- [ ] 解释 `PrintError(Locs, ...)` 里第二个及之后的位置一定会变成什么
- [ ] 说出哪两个 `PrintError` 重载**不**增加 `ErrorsPrinted`，后果是什么
- [ ] 解释 `PrintError("a ", "b")` 为什么段错误，以及 assertions 开关如何改变它的表现
- [ ] 说出 `SMLoc` 的全部成员，以及为什么 `SrcMgr` 必须是全局的
- [ ] 解释 `llvm-tblgen: N errors.` 什么时候出现、什么时候不出现
- [ ] 说出 `PrintFatalError` 走 `std::exit` 导致的两个可观察后果
- [ ] 解释为什么源码行含中文时 caret 会消失
- [ ] 说出 `PrintWarning` 为什么没有 `Record*` 重载

## 12.11 踩雷记录

**错误直觉 1：`PrintError` 报了错，退出码就会是 1。**
正确认识：`PrintError(Twine)` 和 `PrintError(function_ref)` 不经过 `PrintMessage`，不加 `ErrorsPrinted`。退出码 0、产物被 `keep()` 写出。实测 `silent -mode=twine` 就是这种情形。

**错误直觉 2：传了位置就能高亮整段代码。**
正确认识：TableGen 只传 `Loc`，不传 `Ranges`，所以只能画一个 `^`。要高亮得绕过包装直接调 `SrcMgr.PrintMessage(..., {Range}, {FixIt})`。

**错误直觉 3：`PrintError` 两个参数就是「消息 + 补充」。**
正确认识：第一个参数是位置。`PrintError("hello ", "world")` 会把 `"hello "` 当源码指针，Release 构建直接段错误（实测 exit 139）。

**错误直觉 4：`ArrayRef<SMLoc>` 可以放任意多个相关位置。**
正确认识：只有第一个是主诊断，其余全部硬编码为 `note: instantiated from multiclass`。想表达「两处冲突」要 error + note 两条。

**错误直觉 5：`TableGenParseFile` 返回 false 就代表没错误。**
正确认识：它只表示「语法解析走完了」。语义错误会让 `ErrorsPrinted` 增加但返回值仍是 `false`；且计数器跨调用累积，第二次干净 parse 后仍是上次的值（实测 `twice c.td two.td c.td` → 最终 2）。

**错误直觉 6：`PrintFatalError` 会留下已写出的产物。**
正确认识：`fatal_exit` 走 `std::exit(1)`，`ToolOutputFile` 析构删文件；且输出还只在内存里，一个字节都没写。

**错误直觉 7：`PrintWarning` 和 `PrintError` 参数一样。**
正确认识：`PrintWarning` **没有** `Record*`/`RecordVal*` 重载，必须自己传 `getLoc()`。

**错误直觉 8：位置总是指向正确的行列。**
正确认识：行列按字节算，且源码行含非 ASCII 字符时 caret/range 完全不画。中文注释会破坏视觉定位（但 `file:line:col` 仍在）。

## 12.12 延伸阅读

- **`llvm/lib/TableGen/Error.cpp`**（186 行，全读）
  - 本章的地基。重点：`PrintMessage` 的 15 行（27–41）、`fatal_exit`（44–48）、六个 `PrintError` 的逐一实现（104–128）。读完你应该能默写这张重载表。
- **`llvm/include/llvm/TableGen/Error.h`**（66 行）
  - 注意 `[[noreturn]]` 标在哪些函数上——那是编译器帮你抓「fatal 之后还有代码」的唯一手段。
- **`llvm/include/llvm/Support/SMLoc.h`**（60 行）
  - `SMLoc` 与 `SMRange` 的完整定义。看完就明白为什么位置信息这么脆弱。
- **`llvm/lib/Support/SourceMgr.cpp`** 的 `GetMessage`（313）、`PrintMessage`（374）、`SMDiagnostic::print`（523）
  - 「裸指针 → 行列 + caret」的全部实现。575 行的 `isNonASCII` 早退是 12.8 那条限制的出处。
- **`llvm/lib/TableGen/Main.cpp`** 的 `WriteOutput`（119）与 `TableGenMain`（150）
  - `ErrorsPrinted` 的四道闸门。145 行的 `if (ErrorsPrinted == 0) OutFile.keep()` 是「产物存不存在」的开关。
- **`llvm/lib/TableGen/Parser.cpp`**（47 行）
  - 给工具用的 `TableGenParseFile`。全局状态的重置与不重置，全在这 47 行里。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `addDefOne`（580）与 `ParseDefvar`（4079）
  - 前者示范「error + note 表达两处冲突」，后者是 11.7 那个作用域坑的源头。
- **`llvm/unittests/TableGen/CodeExpanderTest.cpp`** 的 `setDiagHandler` 用法（34–65）
  - 唯一在测试里接管 `SrcMgr` 诊断的地方。写单测需要捕获诊断时照这个抄。

## 下一章

Ch 12 一直在说「backend 里的错误」。下一章正式进入 Part 3：先把 backend **怎么被注册**讲清楚——`TableGen::Emitter::Opt` 为什么能用一个全局对象的构造函数注册命令行选项、`ManagedStatic` 在这里解决什么问题、`-gen-*` 的注册表在哪。

[Ch 13 Backend 注册机制与 TableGen::Emitter::Opt](./13-backend-registration.md)
