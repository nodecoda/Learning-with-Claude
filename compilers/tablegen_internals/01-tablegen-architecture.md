# Ch 1 全景：前端、RecordKeeper、backend 三段式

> **本章目标**：能在脑中画出 `llvm-tblgen` 从命令行到输出文本的完整数据流；知道每个阶段由哪个文件负责、失败时错误从哪来；理解「TableGen 是一门有求值语义的声明式语言」而不是模板引擎。

> **环境**：LLVM `24.0.0git`，Release 构建。

---

## 1.1 它不是模板引擎

大多数人对 TableGen 的第一印象来自这种代码：

```td
class Inst<bits<8> opc, string asm> {
  bits<8> Opcode = opc;
  string AsmString = asm;
}
def ADD : Inst<0x10, "add $rd, $rs">;
```

看起来像 C++ 模板 + 宏。但如果它是模板展开，那 `!foreach`、`!if`、`assert`、`dump`、`defset` 这些就不该存在——**它们是运行时求值语义**，而且求值顺序有严格规定（Ch 7、Ch 8 会证明）。

更准确的类比是：**TableGen = 一个以记录（record）为唯一数据类型的、带继承和模板参数的、静态求值的语言**。它编译到一个叫 `RecordKeeper` 的内存图，再交给「后端」把图渲染成任意文本。

```
                    ┌─────────────────────────────────────────────┐
  .td 文本 ────────▶│ TGLexer   → TGParser  → RecordKeeper        │
  (可 include)      │  词法        语法+语义      内存中的记录图   │
                    └─────────────────────────────────────────────┘
                                                    │
                                    ┌───────────────┴───────────┬──────────────┐
                                    ▼                           ▼              ▼
                            -print-records              --dump-json      --gen-instr-info
                            (调试观察)                  (程序处理)        (真实产物)
```

这 30 行是整门课的地图。下面逐段拆。

## 1.2 第一段：`main` 决定一切（`Main.cpp` 241 行）

`llvm-tblgen` 的 `main` 在 `llvm/utils/TableGen/llvm-tblgen.cpp`，只有一行：

```cpp
int main(int argc, char **argv) { return tblgen_main(argc, argv); }
```

真正的逻辑在 `llvm/utils/TableGen/Basic/TableGen.cpp` 的 `tblgen_main`：解析命令行、注册默认 backend、调用 `TableGenMain`。而 `TableGenMain` 在 `llvm/lib/TableGen/Main.cpp:150`，是**整个工具的主流程**：

```cpp
int llvm::TableGenMain(const char *argv0, MultiFileTableGenMainFn MainFn) {
  RecordKeeper Records;                    // ① 空的数据模型
  TGTimer &Timer = Records.getTimer();

  if (TimePhases) Timer.startPhaseTiming();

  Timer.startTimer("Parse, build records"); // ② 计时开始
  ErrorOr<std::unique_ptr<MemoryBuffer>> FileOrErr =
      MemoryBuffer::getFileOrSTDIN(InputFilename, /*IsText=*/true);
  ...
  SrcMgr.AddNewSourceBuffer(std::move(*FileOrErr), SMLoc());
  SrcMgr.setIncludeDirs(IncludeDirs);
  SrcMgr.setVirtualFileSystem(vfs::getRealFileSystem());

  TGParser Parser(SrcMgr, MacroNames, Records, NoWarnOnUnusedTemplateArgs);
  if (Parser.ParseFile()) return 1;        // ③ 前端全部在这里
  Timer.stopTimer();

  if (ErrorsPrinted > 0) return reportError(...);

  Timer.startBackendTimer("Backend overall");
  ...
  if (TableGen::Emitter::ApplyCallback(Records, OutFiles, FilenamePrefix))
    status = MainFn ? MainFn(OutFiles, Records) : 1;  // ④ 选中的 backend
  Timer.stopBackendTimer();
  ...
  Timer.startTimer("Write output");        // ⑤ 写文件（含 --write-if-changed）
  if (int Ret = WriteOutput(argv0, OutputFilename, OutFiles.MainFile))
    return Ret;
  ...
}
```

五个编号就是五个契约：

| # | 做什么 | 关键对象 | 失败表现 |
| --- | --- | --- | --- |
| ① | 建空 `RecordKeeper` | `Record` 的所有权容器 | — |
| ② | 读文件 + 建 `SourceMgr` | `MemoryBuffer`、`SrcMgr` | `Could not open input file` |
| ③ | 解析并填充记录 | `TGParser`、`TGLexer` | 语法/类型/assert 错误 |
| ④ | 调用 backend | `TableGen::Emitter::ApplyCallback` | backend 内部 `PrintFatalError` |
| ⑤ | 写输出 | `WriteOutput` | `error opening ...` |

> **这里有个设计决策值得注意**：`RecordKeeper` 是 `main` 里的**栈对象**，`TGParser` 持有它的引用，backend 也只拿到 `const RecordKeeper &`。整条链没有全局状态——所以同一个进程理论上可以解析多个文件（`MultiFileTableGenMainFn` 就是为此存在的，MLIR 的 `mlir-tblgen` 会用到）。

## 1.3 第二段：前端（`TGLexer` + `TGParser`，约 5900 行）

`TGParser::ParseFile()`（`TGParser.cpp:4732`）只有 20 行：

```cpp
bool TGParser::ParseFile() {
  Lex.Lex();                              // 读第一个 token
  TGVarScope *GlobalScope = PushScope();
  if (ParseObjectList())                  // 循环解析顶层对象
    return true;
  PopScope(GlobalScope);

  if (Lex.getCode() == tgtok::Eof) return false;

  if (Lex.getCode() == tgtok::Error) return true;

  return TokError("Unexpected token at top level");
}
```

`ParseObjectList` 里那个 `while (tgtok::isObjectStart(Lex.getCode()))` 就是**全部语法结构的分派入口**。`isObjectStart` 检查的区间是 `tgtok::Assert` 到 `tgtok::MultiClass`——换句话说，TableGen 只有 12 种顶层语句：

```
assert  class  def  defm  defset  deftype  defvar  dump
foreach  if  let  multiclass
```

这个清单在 `TGLexer.h` 的 `TokKind` 枚举里（`OBJECT_START_FIRST = Assert`，`OBJECT_START_LAST = MultiClass`）。**记住这 12 个，Ch 3 的全部内容就是它们各自的 parse 函数。**

前端内部还有两个隐藏层次，Ch 3 会展开：

```
Lex.Lex() ──▶ token 流
                 │
                 ├─ ParseObject()  ── 按 12 种语句分派
                 │      └─ ParseValue() / ParseSimpleValue()  ── 表达式递归下降
                 │             └─ ParseClassReference()  ── 模板参数
                 │
                 └─ addEntry(RecordsEntry)  ── 把结果放进 RecordKeeper
                        └─ resolve()  ── foreach/multiclass 的延迟展开
```

注意 `RecordsEntry` 这个类型（`TGParser.h:57`）：它能装 `Record`、`ForeachLoop`、`AssertionInfo`、`DumpInfo` 四种东西，**恰好一种**（`addEntry` 开头有 `assert((!!E.Rec + !!E.Loop + !!E.Assertion + !!E.Dump) == 1)`）。这解释了一个经常让人困惑的现象：

```td
foreach i = 0...2 in {
  def E#i;
}
```

`foreach` 的主体在被解析时**不会立刻产生记录**，而是被存成 `ForeachLoop` 放进去，等到 `addEntry` 发现 `E.Loop` 才调 `resolve()` 展开。这是 TableGen 里第一个「延迟求值」例子，Ch 8 会完整讲。

## 1.4 第三段：`RecordKeeper` 与记录图

`RecordKeeper`（`Record.h:2066`）不复杂，本质是三个容器：

```cpp
class RecordKeeper {
  using RecordMap = StringMap<std::unique_ptr<Record>>;
  RecordMap Classes;   // class / multiclass 的模板
  RecordMap Defs;      // def 实例
  GlobalMap ExtraGlobals;  // defvar 定义的全局变量
  mutable DenseMap<StringRef, ArrayRef<const Record *>> Cache;  // getAllDerivedDefinitions 缓存
};
```

关键设计点：

1. **`StringMap<unique_ptr<Record>>`**：名字唯一、按名字 O(1) 查找、记录永不移除。`def` 重名就是错（Ch 0 已验证：`def already exists: A`）。
2. **`Cache` 是 `mutable`**：`getAllDerivedDefinitions("Instruction")` 是 backend 最常调的函数，结果被缓存；`addDef` 会 `Cache.clear()`。
3. **`Record` 自己持有 `RecordKeeper &`**（`TrackedRecords` 成员），所以每条记录都能反查全局——`!cast<SomeClass>("Name")` 这类按名字查找靠它。

一个 `Record` 内部长这样（`Record.h:1675` 起，节选）：

```cpp
class Record {
  const Init *Name;                              // 名字是个 Init（通常是 StringInit）
  SmallVector<SMLoc, 4> Locs;                    // 实例化位置 + 引用位置
  SmallVector<RecordVal, 0> TemplateArgs;        // class 的模板参数
  SmallVector<RecordVal, 0> Values;              // 字段（含继承来的）
  SmallVector<AssertionInfo, 0> Assertions;
  SmallVector<DumpInfo, 0> Dumps;
  SmallVector<std::pair<const Record *, SMRange>, 0> DirectSuperClasses;
  RecordKeeper &TrackedRecords;
  unsigned ID;                                   // 全局唯一，用于稳定排序
  RecordKind Kind;                               // RK_Def / RK_AnonymousDef / RK_Class / RK_MultiClass
};
```

注意三件事：

- **字段的值类型是 `Init *`，不是 `std::string`**。这是本课最重要的设计决定：`int N = 1 + 2` 在记录里存的是 `BinOpInit(!add, IntInit(1), IntInit(2))`，直到某个时刻才 fold 成 `IntInit(3)`。Ch 5–7 的全部内容是这条。
- **`DirectSuperClasses` 是 `pair<Record*, SMRange>`**：范围用于诊断「这个父类是在哪写的」。顺带一提，这也是你在 Ch 0 写 backend 时踩到的 API 坑（`for (const Record *SC : ...)` 编译失败，因为它不是裸指针）。
- **`ID` 是全局唯一递增的**：所有依赖「稳定顺序」的生成物（枚举值、表索引）都靠它，而不是名字字典序。Ch 14 会讲什么时候必须用它。

## 1.5 第四段：backend 是一个函数

`TableGenBackend.h` 开头的注释说得很直白：

```cpp
// A TableGen backend is a function that looks like
//
//    EmitFoo(RecordKeeper &RK, raw_ostream &OS /*, anything else you need */ )
```

注册方式是 `TableGen::Emitter::Opt`（`TableGenBackend.h:64`）：

```cpp
struct Opt {
  LLVM_ABI Opt(StringRef Name, FnT CB, StringRef Desc, bool ByDefault = false);
};

template <class EmitterC> class OptClass : Opt {
  static TableGenOutputFiles run(StringRef, const RecordKeeper &RK) {
    std::string S;
    raw_string_ostream OS(S);
    EmitterC(RK).run(OS);
    return {std::move(S), {}};
  }
public:
  OptClass(StringRef Name, StringRef Desc) : Opt(Name, run, Desc) {}
};
```

真正让 `--help` 出现一行的，是文件底部的静态对象：

```cpp
// llvm/utils/TableGen/InstrInfoEmitter.cpp:1485
static TableGen::Emitter::OptClass<InstrInfoEmitter>
    X("gen-instr-info", "Generate instruction descriptions");
```

**构造函数有副作用**：它调用 `Opt::Opt`，往 `cl::opt<FnT>` 里注册一个字面量选项。所以「注册一个 backend」= 「在某个 `.cpp` 里定义一个全局 `OptClass` 对象」。Ch 13 会讲这个机制的完整实现，包括为什么它必须用 `ManagedStatic`。

## 1.6 三段式为什么这样切

回到那个问题：**为什么不是「解析直接生成 C++」**？

因为 TableGen 被四种完全不同的消费者使用：

| 消费者 | 需要的派生信息 |
| --- | --- |
| `-gen-instr-info` | 指令枚举、操作数类型、`MCInstrDesc` |
| `-gen-dag-isel` | pattern → matcher table（要额外做 DAG pattern 分析） |
| `-gen-register-info` | 寄存器类闭包、压力集合（要算传递闭包） |
| `-gen-asm-matcher` | 汇编字符串 → 匹配 DFA |

如果解析阶段就绑定输出，每个消费者都得重新解析一遍——AArch64 全量 `.td` 解析要 0.55 秒，而一次 `ninja` 要跑十几个 backend 调用。中间那层 `RecordKeeper` 让**解析一次、渲染多次**成为可能（这也是为什么 `-time-phases` 里 "Parse, build records" 是最大单项：Ch 17）。

> 顺带回答一个常见疑问：**为什么不干脆把 `.td` 编译成 `.bc`？** 因为 `RecordKeeper` 里全是互指的指针（`Init` 之间的引用、`Record` 与 `RecordKeeper` 的双向关系），序列化它需要一套引用图编码，而 `.td` 解析只要 0.55 秒，收益不划算。上游试过类似方向（`-gen-searchable-tables` 是另一个问题），但没有做成缓存格式。

## 1.7 一次完整的数据流追踪

用一个最小例子把三段串起来：

```td
class Pair<int a, int b> { int Sum = !add(a, b); }
def P : Pair<3, 4>;
```

阶段一（lexer）：`class`、`Pair`、`<`、`int`、`a` … 逐 token。

阶段二（parser）：`ParseClass()` 建一个 `RK_Class` 的 `Record`，`TemplateArgs` 里放两个 `RecordVal`（类型 `IntRecTy`）；`Def` 语句建 `RK_Def` 的 `Record`，`DirectSuperClasses` 指向 `Pair`，然后**立即**对继承来的字段做替换求值——`Sum` 变成 `BinOpInit(!add, VarInit(a→3), VarInit(b→4))` 再 fold 成 `IntInit(7)`。

阶段三（backend）：`-print-records` 遍历 `RecordKeeper`，先打印 `Classes`（此时 `Sum` 仍是**未折叠的表达式** `!add(Pair:a, Pair:b)`，因为 class 本体没有具体参数），再打印 `Defs`（`Sum` 已是 `7`）。

真实输出（本机实测）：

```console
$ llvm-tblgen -print-records p.td
------------- Classes -----------------
class Pair<int Pair:a = ?, int Pair:b = ?> {
  int Sum = !add(Pair:a, Pair:b);
}
------------- Defs -----------------
def P {	// Pair
  int Sum = 7;
}
```

注意这个输出本身就是一条重要证据：**同一个表达式在 class 里保持未折叠，在 def 里已折叠**。这不是打印器的美化，是 `Record` 里真的存了两种东西。Ch 7 会把「什么时候 fold」讲透。

## 1.8 三段式的边界在哪

读源码时最容易迷路的地方，是把「解析期的错」和「backend 的错」混在一起：

| 症状 | 属于哪段 | 通常在哪看 |
| --- | --- | --- |
| `Couldn't find class 'B'` | 解析期（`ParseClassReference`） | `TGParser.cpp` |
| `Field 'I' of type 'int' is incompatible with value '"str"'` | 解析期（类型检查） | `TGParser.cpp` 的 `CheckTemplateArgValues` / `Record::setValue` |
| `assertion failed: ...` | 解析期（`addEntry`） | `TGParser.cpp:443` |
| `The class 'Instruction' is not defined` | backend 期 | 你调用的 `RK.getAllDerivedDefinitions` |
| `multiple overrides of 'X' defined` | backend 期 | 具体 emitter 的 `PrintFatalError` |
| 生成物里出现了不该有的记录 | backend 期 | 该 emitter 的过滤条件 |

这张表是全书的路由图。Ch 12 会把「错误报告基础设施」完整拆开。

## 1.9 本章检查清单

- [ ] 不看源码，画出 `main` → `TGParser::ParseFile` → `addEntry` → `ApplyCallback` → `WriteOutput` 的调用链
- [ ] 说出 `RecordKeeper` 的三个容器各装什么
- [ ] 解释为什么 `Record` 里的字段是 `Init *` 而不是具体值
- [ ] 说出 TableGen 的 12 种顶层语句（不看代码）
- [ ] 解释「解析一次、渲染多次」带来了什么，代价是什么
- [ ] 判断 `assertion failed` 和 `The class 'X' is not defined` 分别属于哪一段

## 1.10 踩雷记录

**错误直觉 1：TableGen 是 C++ 模板的语法糖。**
正确认识：它有求值语义（`!add` 会算出一个数）、有延迟求值（`foreach` 主体、class 本体的表达式）、有断言（`assert` 在解析期执行）。模板没有这些。

**错误直觉 2：`-print-records` 打出来的就是内部存的东西。**
正确认识：它会重新格式化、把未绑定参数写成 `?`、把 `!strconcat` 折叠成字符串字面量。想看原始 `Init` 树，得自己写 backend 遍历（Ch 5 有完整例子），或者读 `DetailedRecordsBackend.cpp`。

**错误直觉 3：backend 拿到的 `RecordKeeper` 是解析完的不可变快照。**
正确认识：它是 `const` 引用，但 `Record` 有 `mutable` 成员（`CorrespondingDefInit`、`ReferenceLocs`），`RecordKeeper::Cache` 也是 `mutable`。**「const」在这里不意味着「无副作用」**。

**错误直觉 4：`class` 里的字段和 `def` 里的字段是同一份数据。**
正确认识：`class` 是一条 `RK_Class` 记录，`def` 是另一条 `RK_Def` 记录，后者通过 `DirectSuperClasses` 链接前者，并在实例化时把父类字段**复制并替换**进自己的 `Values`。改父类不会影响已实例化的 def（Ch 8 会用这个性质讲 multiclass 展开）。

**错误直觉 5：一次 `llvm-tblgen` 调用只跑一个 backend。**
正确认识：`Opt` 支持注册多个回调，`ApplyCallback` 会遍历所有被命令行选中的项。`-gen-register-info` 就同时产出 `RegInfo.inc`、`RegInfoEnums.inc`、`RegInfoMCDesc.inc` 等多个文件（Ch 15）。

## 1.11 延伸阅读

- **`llvm/lib/TableGen/Main.cpp`**（241 行）
  - 通读。重点：`TableGenMain` 的五个阶段、`WriteOutput` 里 `--write-if-changed` 的比较逻辑（第 122–130 行）、`createDependencyFile` 为什么把主输入文件也写进 depfile。
- **`llvm/utils/TableGen/Basic/TableGen.cpp`**（约 120 行）
  - 默认 backend 的注册（`print-records` 等）与 `tblgen_main`。注意 `printRecords` 只有一行：`OS << Records;`——`operator<<` 才是真正的打印实现。
- **`llvm/lib/TableGen/Record.cpp`** 中的 `operator<<(raw_ostream&, const RecordKeeper&)`
  - `-print-records` 的实体。看它怎么决定「哪些 field 打印成表达式、哪些打印成值」。
- **`llvm/docs/TableGen/BackGuide.md`** 的 "Data Structures" 一节
  - 官方对 `RecordKeeper`/`Record`/`RecordVal`/`Init` 的说明，和本章 1.4 对照读。
- **`llvm/lib/TableGen/TableGenBackendSkeleton.cpp`**（64 行）
  - 官方给的 backend 骨架。Ch 14 会从它出发写生产级版本。

## 下一章

Ch 2 下沉到最底层：`TGLexer` 怎么把字符流切成 token。你会发现 TableGen 的 lexer 里藏着一个预处理器（`#ifdef`/`#define`）、一个 include 栈，以及 Ch 0 那个 segfault 的根源。

[Ch 2 词法层：TGLexer 如何切出 token](./02-lexer.md)
