# Ch 14 写一个生产级 backend：从骨架到可合并代码

> **本章目标**：把 Ch 13 的「怎么挂上去」变成「挂上去之后写什么」。读完你能独立写出一个符合上游标准的 backend：正确的遍历方式、可预测的输出顺序、恰当的错误处理、`emitSourceFileHeader` 的正确用法，以及三类最容易在 review 被打回的确定性陷阱。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。本章所有 C++ 实验都用 Ch 0 的手动链接命令编译并在本机实跑。

---

## 14.1 上游 backend 的三个尺寸档

先看 `llvm/utils/TableGen/` 的真实分布（本 commit 实测 `wc -l`）：

| 档位 | 行数 | 例子 |
| --- | --- | --- |
| 小 | 88–230 | `X86MnemonicTables.cpp`(88)、`CTagsEmitter.cpp`(93)、`OptionRSTEmitter.cpp`(100)、`SystemZInsnDirectiveEmitter.cpp`(148)、`InstrDocsEmitter.cpp`(229) |
| 中 | 300–900 | `PseudoLoweringEmitter`、`RegisterBankEmitter`、`SDNodeInfoEmitter`、`SearchableTableEmitter` |
| 大 | 1700–4500 | `DAGISelMatcherEmitter`(1693)、`RegisterInfoEmitter`(2253)、`GlobalISelEmitter`(2621)、`AsmMatcherEmitter`(4496) |

**大档位的体量不来自「遍历记录」，而来自「算法」**——`AsmMatcherEmitter` 的 4496 行里，绝大部分是在构造匹配表、处理冲突、优化状态机。真正的「遍历 + 输出」通常只有几百行。

这决定了本章的结构：先把「小档位」写到位（14.2–14.7），再讲什么时候需要升到中/大档位（14.8）。

## 14.2 骨架与真实代码的差距

官方骨架 `TableGenBackendSkeleton.cpp`（64 行）给了两种注册方式（Ch 13 的 Option A/B），但**没有回答真正的问题**：

- 遍历哪些记录？
- 输出顺序怎么定？
- 字段可能不存在，怎么处理？
- 输出重定向到 stdout 时 header 里的文件名怎么办？

`InstrDocsEmitter.cpp`（229 行）是最好的「小档位范本」：它足够短，但每一个上流习惯都到位了。下面逐段拆。

### 文件头与 include

```cpp
//===- InstrDocsEmitter.cpp - Opcode Documentation Generator --------------===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
...
//===----------------------------------------------------------------------===//
//
// InstrDocsEmitter generates restructured text documentation for the opcodes
// that can be used by MachineInstr. For each opcode, the documentation lists:
// * Opcode name
// * Assembly string
// * Flags (e.g. mayLoad, isBranch, ...)
// * Operands, including type and name
// * Operand constraints
// * Implicit register uses & defs
// * Predicates
//
//===----------------------------------------------------------------------===//
```

那个注释块不是装饰。**上游 reviewer 第一眼看的就是它**——它必须回答「这个 backend 生成什么、给谁用、列出哪几类信息」。写不出这三句，说明你还没想清楚要写什么。

`include` 分两类：

```cpp
#include "Common/CodeGenDAGPatterns.h"
#include "Common/CodeGenInstruction.h"
#include "Common/CodeGenTarget.h"
#include "llvm/TableGen/Record.h"
#include "llvm/TableGen/TableGenBackend.h"
#include <string>
#include <vector>
```

- `Common/*`：`llvm/utils/TableGen/Common/` 里的公共分析层（`CodeGenTarget`、`CodeGenInstruction`、`CodeGenRegisters`）。**如果你的 backend 需要「指令/寄存器」这种语义概念，几乎一定该用这层，而不是自己从 `Record` 里刨字段。**
- `llvm/TableGen/*`：`lib/TableGen` 的公共 API。

`#include <string>` / `<vector>` 常被省略（靠其他头传递），但上游越来越倾向显式写出——`InstrDocsEmitter` 就写了。

### 「工具函数 + 一个主函数」的形状

```cpp
static void writeTitle(StringRef Str, raw_ostream &OS, char Kind = '-');
static void writeHeader(StringRef Str, raw_ostream &OS, char Kind = '-');
static std::string escapeForRST(StringRef Str);

static void EmitInstrDocs(const RecordKeeper &RK, raw_ostream &OS) { ... }

static TableGen::Emitter::Opt X("gen-instr-docs", EmitInstrDocs,
                                "Generate instruction documentation");
```

**小档位不用类**。200 行以内的 backend 用「一组 static 辅助函数 + 一个 `Emit*` 函数」就够了；写成类反而增加噪音。什么时候该用类？看 14.8。

三个辅助函数都值得注意：

```cpp
static void writeTitle(StringRef Str, raw_ostream &OS, char Kind = '-') {
  OS << std::string(Str.size(), Kind) << "\n"
     << Str << "\n"
     << std::string(Str.size(), Kind) << "\n";
}
```

**用 `Str.size()` 决定下划线长度**，不是写死的 `----`。这样标题改字就不用改分隔线。这种「从数据推导格式」的习惯在上游到处都是。

```cpp
static std::string escapeForRST(StringRef Str) {
  std::string Result;
  Result.reserve(Str.size() + 4);
  for (char C : Str) {
    switch (C) {
    case '\n': Result += "\\n"; break;
    case '\t': Result += "\\t"; break;
    case '_':  Result += "\\_"; break;   // Underscore at the end of a line has a special meaning in rst.
    default:   Result += C;
    }
  }
  return Result;
}
```

**输出格式的转义永远是 backend 的责任**。`.td` 里的 `AsmString` 可能含 `\n`、`\t`、`_`；直接拼进 RST 会让文档渲染错乱。每个输出「非 C++」格式的 backend 都有一段这样的函数（`OptionRSTEmitter`、`InstrDocsEmitter`、`CTagsEmitter` 各有一份）。

**`reserve(Str.size() + 4)`**：预分配，避免多次 realloc。小优化，但上游会写。

## 14.3 遍历：三种 API，三种顺序

这是本章最实用的一节。

`InstrDocsEmitter` 用的是：

```cpp
const CodeGenDAGPatterns CDP(RK);
const CodeGenTarget &Target = CDP.getTargetInfo();
...
for (const CodeGenInstruction *II : Target.getInstructions()) {
```

**没有直接用 `RecordKeeper`**。这是中档以上 backend 的常态：先构造一个分析对象（`CodeGenTarget`），它内部完成「收集所有 Instruction def、解析 operand 列表、计算 flags」，然后你遍历它的结果。

但既然本章要讲 backend 基础，必须先把 `RecordKeeper` 的三个遍历 API 讲清楚，因为它们的**顺序保证各不相同**——这是确定性问题（14.6）的根源。

### 三个 API

```cpp
// Record.h
ArrayRef<const Record *> getClasses() const;
ArrayRef<const Record *> getDefs() const;
ArrayRef<const Record *> getAllDerivedDefinitions(StringRef ClassName) const;
std::vector<const Record *> getAllDerivedDefinitions(ArrayRef<StringRef> ClassNames) const;
ArrayRef<const Record *> getAllDerivedDefinitionsIfDefined(StringRef ClassName) const;
```

实现（`Record.cpp:3183` 与 `3243`）：

```cpp
// getDefs / getClasses
static ArrayRef<const Record *>
getSortedRecords(const StringMap<std::unique_ptr<Record>> &Map,
                 std::vector<const Record *> &Sorted) {
  if (Sorted.size() != Map.size()) {
    Sorted.clear();
    Sorted.reserve(Map.size());
    for (const auto &Entry : Map)
      Sorted.push_back(Entry.second.get());
    llvm::sort(Sorted, [](const Record *LHS, const Record *RHS) {
      return LHS->getName() < RHS->getName();     // ← 普通 StringRef 比较
    });
  }
  return Sorted;
}

// getAllDerivedDefinitions(ArrayRef<StringRef>)
  ...
  llvm::sort(Derived, LessRecord());              // ← compare_numeric
  Derived.erase(llvm::unique(Derived), Derived.end());
```

而 `LessRecord`（`Record.h:2184`）：

```cpp
struct LessRecord {
  bool operator()(const Record *Rec1, const Record *Rec2) const {
    return Rec1->getName().compare_numeric(Rec2->getName()) < 0;
  }
};
```

**两套排序规则不一样**：`getDefs()` 用字节序，`getAllDerivedDefinitions()` 用数字感知序。实测（`num.td` 有 `I1 I2 I9 I10`）：

```console
$ ./ord4 num.td
getDefs():            I1 I10 I2 I9
getAllDerivedDefs():  I1 I2 I9 I10
```

**同一个 `.td`，两个 API 给出不同顺序。** 如果你的 backend 混用两者（比如先用 `getDefs()` 按名字建索引，再用 `getAllDerivedDefinitions()` 遍历输出），输出顺序会和索引顺序不一致。

### 第三个：`getAllDerivedDefinitions` 的缓存

```cpp
ArrayRef<const Record *>
RecordKeeper::getAllDerivedDefinitions(StringRef ClassName) const {
  // We cache the record vectors for single classes. Many backends request
  // the same vectors multiple times.
  auto [Iter, Inserted] = Cache.try_emplace(ClassName.str());
  if (Inserted)
    Iter->second = getAllDerivedDefinitions(ArrayRef(ClassName));
  return Iter->second;
}
```

注意**单参数版返回 `ArrayRef`（缓存），多参数版返回 `std::vector`（不缓存）**。如果你要长期持有结果，`ArrayRef` 指向 `RecordKeeper` 内部，生命周期跟 `RecordKeeper` 绑定——`TableGenMain` 里 `Records` 是栈对象，backend 跑完就没了，所以正常用法没问题；但如果你在 backend 里把 `ArrayRef` 存进某个跨阶段的结构，就要小心。

### 三个 API 的选择规则

| 你要做的事 | 用什么 | 顺序 |
| --- | --- | --- |
| 遍历某类 def 的实例 | `getAllDerivedDefinitions("Instr")` | 数字感知序 |
| 遍历所有 def（不分类型） | `getDefs()` | 字节序 |
| 遍历所有 class（调试/反射） | `getClasses()` | 字节序 |
| 「这个类可能不存在」 | `getAllDerivedDefinitionsIfDefined` | 同上，缺类返回空 |
| 按 ID / 定义顺序 | 自己 `llvm::sort` + `LessRecordByID` | `Record::getID()` |

**别用 `getDefs()` 来遍历「某类记录」**。它包含 class 吗？不包含（class 在 `Classes` 里），但它包含**所有** def，你得自己过滤 `isSubClassOf`，而且顺序是字节序。`getAllDerivedDefinitions` 快得多——它靠 `DefsByDirectSuperClass` 反向索引（见 14.6）。

## 14.4 字段访问：四种风格，四种失败方式

`Record` 的字段读取 API 有十几组，最容易踩坑的是「找不到字段时怎么办」。

`InstrDocsEmitter` 用的是：

```cpp
StringRef Constraints = Inst->getValueAsString("Constraints");
if (!Constraints.empty()) {
  OS << "Constraints: ``" << Constraints << "``\n\n";
}
```

```cpp
std::vector<const Record *> Predicates =
    II->TheDef->getValueAsListOfDefs("Predicates");
if (!Predicates.empty()) { ... }
```

四种 API 的行为差异（`Record.h`）：

| API | 字段不存在时 | 字段是 `?` 时 | 字段类型错时 |
| --- | --- | --- | --- |
| `getValue(StringRef)` | 返回 `nullptr` | 返回 `RecordVal*` | 返回 `RecordVal*` |
| `getValueAsString` | **`PrintFatalError`**（`Record 'X' does not have a field named 'Y'!`） | 返回 `""` | `PrintFatalError` |
| `getValueAsListOfDefs` | **`PrintFatalError`** | `PrintFatalError`（不是 list） | `PrintFatalError` |
| `getValueAsBits` / `getValueAsInt` 等 | `PrintFatalError` | 见源码 | `PrintFatalError` |

`getValueAs*` 系列**全部是 fatal**。这解释了 Ch 12.7 那个统计：`utils/TableGen` 里 `PrintFatalError` 是 `PrintError` 的 4 倍——**大部分 fatal 是 `Record::getValueAs*` 内部产生的**，不是 backend 手写的。

设计意图很清楚：**backend 假设的字段是数据契约的一部分**。`.td` 里没有 `Constraints` 字段，说明用户用错了 backend 或忘了 include 定义该字段的基类——这不是「跳过这一项」能解决的，继续跑只会产出半截代码。

**但可选字段必须用 `getValue()` 判断**：

```cpp
// 错误：OptionalField 是可选的，getValueAsString 会 fatal
StringRef S = Rec->getValueAsString("OptionalField");

// 正确
if (const RecordVal *RV = Rec->getValue("OptionalField"))
  if (const auto *SI = dyn_cast<StringInit>(RV->getValue()))
    OS << SI->getValue();
```

上游的惯例是：**字段是 `field` 关键字声明的（`FK_NonconcreteOK`）或文档里写「optional」的，一律用 `getValue` + `dyn_cast`**。

## 14.5 输出格式：`emitSourceFileHeader` 与「从哪里来」

`emitSourceFileHeader(Desc, OS, RK)` 生成那个标准的 LLVM 文件头。实测：

```console
$ ./hdr c.td
/*===- TableGen'erated file -------------------------------------*- C++ -*-===*\
|*                                                                            *|
|* Instruction Information                                                    *|
|*                                                                            *|
|* Automatically generated file, do not edit!                                 *|
|* From: c.td                                                                 *|
|*                                                                            *|
\*===----------------------------------------------------------------------===*/

struct Foo {};
```

`From: c.td` 来自 `RecordKeeper::getInputFilename()`，而它是 `Main.cpp` 里 `Records.saveInputFilename(InputFilename)` 存下来的。**stdin 输入时显示 `From: -`**（实测）。

三个实现细节：

### 行长固定 80

```cpp
const size_t MAX_LINE_LEN = 80U;

static void printLine(raw_ostream &OS, const Twine &Prefix, char Fill,
                      StringRef Suffix) {
  size_t Pos = (size_t)OS.tell();
  assert((Prefix.str().size() + Suffix.size() <= MAX_LINE_LEN) &&
         "header line exceeds max limit");
  OS << Prefix;
  for (size_t i = (size_t)OS.tell() - Pos, e = MAX_LINE_LEN - Suffix.size();
         i < e; ++i)
    OS << Fill;
  OS << Suffix << '\n';
}
```

`Prefix + Suffix` 超过 80 会触发 assert（assertions 构建）。**Release 构建下 `Fill` 的循环会不执行**（`e < i`），于是那一行没有填充——不会崩，只是难看。

### 长描述自动折行

`Desc` 超过 `80 - 4 = 76` 字符会按 76 折行。实测 200 个 `X`：

```console
$ ./longb -desc=long c.td
/*===- TableGen'erated file -------------------------------------*- C++ -*-===*\
|*                                                                            *|
|* XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX *|
|* XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX *|
|* XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX                       *|
|*                                                                            *|
|* Automatically generated file, do not edit!                                 *|
```

76 + 76 + 48。**所以 `Desc` 可以写长，不用自己折行。**

### 没有 `RK` 时的降级

签名有默认参数：

```cpp
LLVM_ABI void emitSourceFileHeader(StringRef Desc, raw_ostream &OS,
                                   const RecordKeeper &Record = RecordKeeper());
```

不传 `RK` 时 `getInputFilename()` 为空，`From:` 那一行整行**不打印**（`if (!Record.getInputFilename().empty())`）。所以调试时想省事可以只传两个参数。

## 14.6 确定性：三类陷阱

生成的 `.inc` 会被 CMake/Ninja 缓存、被 git 跟踪、被下游编译。**同样的输入必须产出逐字节相同的输出**，否则：

- 每次构建都重编（`.inc` 时间戳变了）
- diff 里全是噪音，review 无法进行
- reproducible build 失败

三个真实陷阱，按发现难度排序。

### 陷阱一：`DenseMap`/`StringMap` 的迭代顺序

`getDefs()` 内部迭代 `StringMap`，但**立刻排序**了，所以外部看到的是有序的。麻烦的是**你自己**建的 map：

```cpp
// 危险
DenseMap<StringRef, unsigned> NameToID;
for (const Record *R : Records.getAllDerivedDefinitions("Instr"))
  NameToID[R->getName()] = NextID++;
for (const auto &[Name, ID] : NameToID)      // ← 顺序取决于 hash
  OS << "case " << ID << ": ...\n";
```

`DenseMap` 的迭代顺序由 hash 和插入历史决定，**跨平台可能不同**（`std::hash<std::string>` 的实现各异），甚至同一平台不同 build 可能不同。

上游的解法有两种：

```cpp
// 方案 A：MapVector（保持插入顺序）
MapVector<SmallVector<int>, unsigned> OperandMap;
for (const auto &[OpList, _] : OperandMap)      // ← 插入顺序
  ...

// 方案 B：先收集到 vector，再显式排序
std::vector<std::pair<StringRef, unsigned>> Ordered(NameToID.begin(), NameToID.end());
llvm::sort(Ordered, [](auto &A, auto &B) { return A.second < B.second; });
```

`InstrInfoEmitter.cpp:298` 与 `327` 用的是方案 A——`MapVector` 的定义就是「`DenseMap` 的查找 + `vector` 的顺序」。

### 陷阱二：按指针排序

```cpp
std::vector<const Record *> Recs = ...;
llvm::sort(Recs);              // ← 默认比较的是指针值！
```

`std::less<const Record*>` 比地址。地址由分配顺序决定，**不同运行可能不同**（ASLR 通常不影响堆分配顺序，但 slab 复用会让它变）。

上游提供了三个 comparator（`Record.h:2184`）：

```cpp
struct LessRecord            { /* compare_numeric on getName()   */ };
struct LessRecordByID        { /* getID() < getID()               */ };
struct LessRecordFieldName   { /* getValueAsString("Name") 比较   */ };
```

`LessRecordByID` 是这里最值得注意的：

```cpp
/// Sorting predicate to sort record pointers by their
/// unique ID. If you just need a deterministic order, use this, since it
/// just compares two `unsigned`; the other sorting predicates require
/// string processing.
struct LessRecordByID {
  bool operator()(const Record *LHS, const Record *RHS) const {
    return LHS->getID() < RHS->getID();
  }
};
```

**ID 是「创建顺序」**，而这个顺序完全由 `.td` 的文本顺序 + 展开顺序决定，所以是确定的。缺点：ID 顺序和名字无关，用户看 `-print-records`（按名字）和你的输出（按 ID）会觉得对不上。

**选择规则**：输出给人看 → `LessRecord`（名字序）；输出给机器查表 → `LessRecordByID`（快且稳定）。**千万别用裸 `llvm::sort(Recs)`。**

### 陷阱三：`getAllDerivedDefinitions` vs `getDefs` 的顺序不一致

14.3 已实测。如果你的 backend 先用 `getDefs()` 建索引、再用 `getAllDerivedDefinitions()` 输出，两处顺序不同——这在「输出 opcode 编号」这类场景里会造成真的 bug（编号和表顺序错位）。

**规则**：一个 backend 内部**只用一个遍历 API 作为主序**，其余用 `MapVector` 或显式排序对齐。

### 验证手段

```bash
# 同一输入跑两次，输出必须逐字节相同
llvm-tblgen -gen-my-thing x.td -o a.inc
llvm-tblgen -gen-my-thing x.td -o b.inc
cmp a.inc b.inc && echo "deterministic"

# 不同 .td 书写顺序，输出应当相同（若不依赖定义顺序）
llvm-tblgen -gen-my-thing x.td -o c.inc
llvm-tblgen -gen-my-thing x-shuffled.td -o d.inc
diff c.inc d.inc
```

第二条是上游 reviewer 常问的：「如果你 shuffle 输入的 def 顺序，输出变不变？」**答案应该是「不变」**，除非你的格式本质上依赖定义顺序（比如 opcode 编号——那就该显式说明并加测试）。

## 14.7 一个完整的最小生产级 backend

把上面所有规则套进去，写一个「给每个 `Ins` 记录输出 C++ 常量表」的 backend（约 80 行，本机实测）。两个文件：backend 本体 + 一个 `main`（`llvm-tblgen` 自己的 `main` 你已经有了，这里是为了独立验证）。

```cpp
//===- ConstTableEmitter.cpp - Emit a C++ table of instruction encodings ---===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// ConstTableEmitter generates a lookup table mapping instruction names to
// their encoding width and value, for the `Ins` class defined by the input.
// Output is sorted by name so that it is stable across input reorderings.
//
//===----------------------------------------------------------------------===//

#include "llvm/ADT/STLExtras.h"
#include "llvm/TableGen/Error.h"
#include "llvm/TableGen/Record.h"
#include "llvm/TableGen/TableGenBackend.h"

using namespace llvm;

namespace {

struct Entry {
  StringRef Name;
  unsigned Width;
  int64_t Value;
};

std::vector<Entry> collect(const RecordKeeper &RK) {
  std::vector<Entry> Entries;
  for (const Record *R : RK.getAllDerivedDefinitions("Ins")) {
    const RecordVal *EncRV = R->getValue("Enc");
    if (!EncRV) {
      PrintError(R->getLoc(), "instruction is missing the 'Enc' field");
      continue;
    }
    const auto *Enc = dyn_cast<IntInit>(EncRV->getValue());
    if (!Enc) {
      PrintError(EncRV->getLoc(), "field 'Enc' is not fully resolved");
      continue;
    }

    unsigned Width = 32;
    if (const RecordVal *WRV = R->getValue("EncWidth")) {
      if (const auto *W = dyn_cast<IntInit>(WRV->getValue()))
        Width = (unsigned)W->getValue();
      else
        PrintWarning(WRV->getLoc(), "ignoring non-integer 'EncWidth'");
    }
    Entries.push_back({R->getName(), Width, Enc->getValue()});
  }
  llvm::sort(Entries, [](const Entry &A, const Entry &B) {
    return A.Name < B.Name;
  });
  return Entries;
}

void emit(raw_ostream &OS, const RecordKeeper &RK) {
  std::vector<Entry> Entries = collect(RK);
  if (Entries.empty())
    PrintWarning("no 'Ins' records found; emitting an empty table");

  emitSourceFileHeader("Instruction encoding table", OS, RK);

  OS << "struct InsEncoding {\n"
        "  const char *Name;\n"
        "  unsigned Width;\n"
        "  int64_t Value;\n"
        "};\n\n";
  OS << "static constexpr InsEncoding InsEncodings[] = {\n";
  for (const Entry &E : Entries)
    OS << "  {\"" << E.Name << "\", " << E.Width << ", " << E.Value << "},\n";
  OS << "};\n";
}
} // anonymous namespace

static void EmitConstTable(const RecordKeeper &RK, raw_ostream &OS) {
  emit(OS, RK);
}

static TableGen::Emitter::Opt X("gen-const-table", EmitConstTable,
                                "Generate a C++ instruction encoding table");
```

```cpp
// main.cpp —— 只是为了能独立跑，真实上游直接放进 llvm-tblgen
#include "llvm/Support/CommandLine.h"
#include "llvm/TableGen/Main.h"
using namespace llvm;
int main(int argc, char **argv) {
  cl::ParseCommandLineOptions(argc, argv);
  return TableGenMain(argv[0], MultiFileTableGenMainFn(nullptr));
}
```

注意 `main.cpp` 那一行不是随便写的：直接传 `nullptr` 会因为两个重载的默认参数而二义（Ch 13.7），必须先构造具体类型的值。

输入（`ct.td`）：

```td
class Ins { int Enc; int EncWidth = 32; }
def NOP  : Ins { let Enc = 0xD503201F; let EncWidth = 32; }
def RET  : Ins { let Enc = 0xD65F03C0; }
def ADD  : Ins { let Enc = 0x8B000000; }
```

```console
$ ./ct -gen-const-table ct.td
/*===- TableGen'erated file -------------------------------------*- C++ -*-===*\
|*                                                                            *|
|* Instruction encoding table                                                 *|
|*                                                                            *|
|* Automatically generated file, do not edit!                                 *|
|* From: ct.td                                                                *|
|*                                                                            *|
\*===----------------------------------------------------------------------===*/

struct InsEncoding {
  const char *Name;
  unsigned Width;
  int64_t Value;
};

static constexpr InsEncoding InsEncodings[] = {
  {"ADD", 32, 2332033024},
  {"NOP", 32, 3573751839},
  {"RET", 32, 3596551104},
};
```

### 三个设计决定的理由

1. **`collect()` 与 `emit()` 分离**。所有错误先收集完，再输出。这避免了「输出到一半 fatal」的半截产物，也让「一个错就停」变成「一次报全」。
2. **用 `getValue()` 而不是 `getValueAsInt()`**。`Enc` 是必填字段，但用户可能写成 `int Enc;`（未赋值 = `?`）。`getValueAsInt` 对 `?` 会 `PrintFatalError`（14.4），而我们想要「报一条可定位的错误、跳过这条记录、继续处理下一条」。
3. **`getLoc()` 而不是裸 `PrintError(Twine)`**。Ch 12.3 的坑：不带位置的错误不计数，产物会被写出。

### 实测的错误路径

**情况一：字段存在但没赋值**（`int Enc;` 在 class 里声明、def 里忘了 `let Enc = ...`）：

```td
class Ins {
  int Enc;
  int EncWidth = 32;
}
def A : Ins { }
def B : Ins { let Enc = 5; }
```

```console
$ ./ct -gen-const-table bad4.td
bad4.td:2:7: error: field 'Enc' is not fully resolved
  int Enc;
      ^
./ct: 1 errors.
<!-- 但 stdout 仍然输出了完整表格，含 B 不含 A -->
static constexpr InsEncoding InsEncodings[] = {
  {"B", 32, 5},
};
```

**两个可观察行为**：

- 错误位置指向**字段名**（`2:7`），不是 `def A` 那一行。因为用的是 `EncRV->getLoc()`（Ch 12.9 规则二）。这对用户更有用——他们该去改 class 的默认值或给 def 补 `let`。
- **stdout 上仍然有完整表格**（因为 `-o` 未指定，默认 `-`，`ToolOutputFile` 对 `"-"` 不删任何东西，Ch 12.3 例外）。但**退出码是 1**。写 CI 时不能只看 stdout 有没有内容。

指定 `-o` 时行为不同：

```console
$ ./ct -gen-const-table bad3.td -o bad3.inc
bad3.td:1:17: error: field 'Enc' is not fully resolved
...
./ct: 1 errors.
exit=1
$ ls bad3.inc
ls: cannot access 'bad3.inc': No such file or directory
```

**产物被 `ToolOutputFile` 的析构删掉了**（`ErrorsPrinted > 0` → 没调 `keep()`）。

**情况二：所有 `Ins` 记录都缺 `Enc`** → 还会额外触发一条「空表」警告：

```console
$ ./ct -gen-const-table bad1.td
... error ...
warning: no 'Ins' records found; emitting an empty table
./ct: 1 errors.
```

这条 warning 有价值：它让「输入写错了」和「输入里本来就没有这类记录」在日志上可区分。

**情况三：`EncWidth` 类型错**——**根本到不了你的 backend**。`.td` 在解析期就被类型检查拦下：

```console
$ ./ct -gen-const-table bad2.td
bad2.td:2:41: error: Field 'EncWidth' of type 'int' is incompatible with value '"wide"' of type 'string'
def A : Ins { let Enc = 0x8B000000; let EncWidth = "wide"; }
                                        ^
exit=1
```

所以 `collect()` 里 `dyn_cast<IntInit>(WRV->getValue())` 失败的那个分支，**在实际使用中几乎不会触发**——它只能被「`?`」或「从别的记录继承了非 int」这类边界情况触发。这类防御性代码留着是对的（`.td` 可以 `include` 别人的文件），但不要以为它常被走到。

### 实测的确定性验证

把 `ct.td` 里的三个 `def` 顺序打乱（`ct2.td`：`ADD` → `RET` → `NOP`），在同名文件下跑：

```console
$ cp ct.td  s1/x.td
$ cp ct2.td s2/x.td
$ ./ct -gen-const-table s1/x.td -o a2.inc
$ ./ct -gen-const-table s2/x.td -o b2.inc
$ cmp a2.inc b2.inc && echo SAME
SAME
```

**输入顺序不影响输出**——这是 `llvm::sort` 那一行换来的。

但**不要**用不同的文件名做这个测试：

```console
$ ./ct -gen-const-table ct.td  -o a.inc
$ ./ct -gen-const-table ct2.td -o b.inc
$ diff a.inc b.inc
6c6
< |* From: ct.td                                                                *|
---
> |* From: ct2.td                                                               *|
```

**`emitSourceFileHeader` 会把输入文件名写进产物**。所以「输出可复现」的精确表述是：**同一份输入文件（同路径）→ 同一份输出**。跨路径不可比。这是 `From:` 行的必然代价，也是为什么上游不把 `.inc` 提交进仓库——构建目录里生成的 `From:` 路径会是绝对路径或相对路径，取决于 CMake 怎么调。

## 14.8 什么时候该升级到「类 + 分析层」

`InstrDocsEmitter` 是「函数式」。什么时候需要变成类？

看 `CodeGenInstruction` 这个类的字段数量（`Common/CodeGenInstruction.h:205` 起）：`TheDef`、`Namespace`、`AsmString`、`Operands`、`ImplicitDefs`、`ImplicitUses`，加上 **40 多个 bit-field**（`isPreISelOpcode`、`isBranch`、`mayLoad`……）。

这些 flag 都不是直接来自 `.td` 某个字段，而是**从多个字段 + 继承链推导出来的**。推导逻辑集中在 `CodeGenInstruction` 的构造函数里。

**判据**：

- 需要「从多个字段推导一个概念」→ 建分析类
- 需要「跨记录的状态」→ 建类（比如 `AsmMatcherEmitter` 要维护冲突集合）
- 需要「多阶段流水线」→ 建类（`GlobalISelEmitter` 是 parse → build → optimize → emit 四阶段）

反例：`InstrDocsEmitter` 只有「读字段 → 打印」，没有推导，所以函数式就够。

**上游的中间层**：

| 头文件 | 提供什么 |
| --- | --- |
| `Common/CodeGenTarget.h` | `CodeGenTarget`：所有 `Instr`/`Register`/`RegClass` 的语义视图 |
| `Common/CodeGenInstruction.h` | `CodeGenInstruction`：单条指令的解析结果 |
| `Common/CodeGenRegisters.h` | `CodeGenRegisters`：寄存器类的层次与位掩码 |
| `Common/CodeGenDAGPatterns.h` | `CodeGenDAGPatterns`：pattern 的 DAG 表示与类型推断 |

**写 backend 的第一件事是 `grep` 这层**，十有八九已经有人做过你要的推导。

## 14.9 本章检查清单

- [ ] 说出 `getDefs()`、`getAllDerivedDefinitions()` 各自的排序规则，并解释为什么不同
- [ ] 解释 `MapVector` 在 backend 里解决什么问题，以及它与 `DenseMap` 的差
- [ ] 说出 `getValueAsString` 与 `getValue` 在「字段不存在」时的行为差异
- [ ] 解释为什么 `llvm::sort(std::vector<const Record*>)` 是错的，正确写法是什么
- [ ] 说出 `emitSourceFileHeader` 的三处固定行为（80 列、76 折行、nullptr `RK`）
- [ ] 说出「什么时候该写成类」的三条判据
- [ ] 解释为什么「错误先收集再输出」比「边输出边报错」好
- [ ] 说出为什么 `emitSourceFileHeader` 让「跨文件名」的确定性测试失效
- [ ] 解释「字段没赋值」和「字段类型错」分别在哪一阶段被拦下

## 14.10 踩雷记录

**错误直觉 1：`getDefs()` 和 `getAllDerivedDefinitions()` 顺序一样。**
正确认识：前者按字节序（`LHS->getName() < RHS->getName()`），后者按 `compare_numeric`。实测 `I1 I10 I2 I9` vs `I1 I2 I9 I10`。混用会让输出与索引错位。

**错误直觉 2：`getValueAsString` 对不存在的字段返回空串。**
正确认识：直接 `PrintFatalError`（`Record 'X' does not have a field named 'Y'!`）。这正是 `utils/TableGen` 里 fatal 数量是 error 4 倍的原因。可选字段必须 `getValue()` + `dyn_cast`。

**错误直觉 3：`DenseMap` 迭代顺序稳定。**
正确认识：由 hash 与插入历史决定，跨平台/跨构建可能不同。要顺序就用 `MapVector` 或显式排序。`InstrInfoEmitter` 的 `OperandMap` 是 `MapVector`，不是 `DenseMap`。

**错误直觉 4：`llvm::sort(vec_of_RecordPtr)` 会按名字排。**
正确认识：按**指针地址**排。要用 `LessRecord`（名字）或 `LessRecordByID`（定义顺序）。

**错误直觉 5：`emitSourceFileHeader` 的 `Desc` 太长会崩。**
正确认识：会自动按 76 字符折行（实测 200 字符 → 76+76+48）。会崩的是 `Prefix + Suffix` 本身超 80——那是内部常量，用户碰不到。

**错误直觉 6：`emitSourceFileHeader` 总要传 `RK`。**
正确认识：第三个参数有默认值 `RecordKeeper()`；不传时 `From:` 那行整行不打印。

**错误直觉 7：输出到 stdout 和输出到文件，`From:` 一样。**
正确认识：`From:` 来自 `getInputFilename()`，stdin 输入时是 `-`（实测 `From: -`），和 `-o -` 无关。

**错误直觉 8：backend 里遇到坏数据应该立刻 `return` 停止。**
正确认识：更好的做法是「收集所有错误，一次报完」，最后靠 `ErrorsPrinted` 闸门决定是否写产物。边输出边 fatal 会让用户改一个错重启一次。

**错误直觉 9：「同样的 `.td` 内容 → 同样的输出」是确定性的定义。**
正确认识：`emitSourceFileHeader` 会写入输入文件名，所以同一个内容放在不同路径下产出的头部不同（实测 `From: ct.td` vs `From: ct2.td`）。可复现性的精确表述是「同一输入路径 + 同一内容 → 同一字节」。做确定性测试时必须用同名文件（或不同目录下同名）。

**错误直觉 10：backend 里的 `dyn_cast` 失败分支会经常被走到。**
正确认识：类型错误在 `.td` 解析期就被拦下（`Field 'X' of type 'int' is incompatible with value ...`），backend 根本看不到。`collect()` 里那些 `dyn_cast` 检查只对 `?`（未赋值）和跨文件继承这类边界有效，是防御性代码而非主路径。

## 14.11 延伸阅读

- **`llvm/utils/TableGen/InstrDocsEmitter.cpp`**（229 行，全读）
  - 最好的小档位范本。重点：文件头注释的写法、`escapeForRST`、`Target.getInstructions()` 的用法、字段读取的 `if (!X.empty())` 模式。
- **`llvm/utils/TableGen/TableGenBackendSkeleton.cpp`**（64 行）
  - 官方骨架。把它和 `InstrDocsEmitter` 对比，能看出「骨架」缺了哪些真实工程要素（错误处理、转义、排序）。
- **`llvm/lib/TableGen/Record.cpp`** 的 `getSortedRecords`（3183）与 `getAllDerivedDefinitions`（3243）
  - 本章 14.3 的两种排序。后者那段 `DefsByDirectSuperClass` 反向索引是性能关键，值得读注释。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `LessRecord`/`LessRecordByID`/`LessRecordFieldName`（2184–2205）
  - 三个 comparator 的注释直接告诉你该选哪个。
- **`llvm/lib/Support/StringRef.cpp`** 的 `compare_numeric`（57）
  - 数字感知比较的实现。注意注释里那句「This doesn't really handle prefixed zeros well」。
- **`llvm/lib/TableGen/TableGenBackend.cpp`** 的 `emitSourceFileHeader`（96–119）
  - 80 列、76 折行的全部实现。
- **`llvm/utils/TableGen/Common/CodeGenInstruction.h`**（约 400 行）
  - 「分析层」的典型形状。看那 40 多个 bit-field 就知道为什么要建类。
- **`llvm/utils/TableGen/InstrInfoEmitter.cpp`** 的 `emitGetNamedOperandIdx`（280–340）
  - `MapVector` 在真实输出里的用法，以及为什么不能用 `DenseMap`。

## 下一章

Ch 14 的产物是**一个文件**。下一章处理「一个 backend 产出多个文件」以及「怎么让增量构建正确」——`RegisterInfoEmitter` 的 5 文件模式、`-d` 依赖文件、`tablegen()` CMake 函数的 `DEPFILE` 机制，以及为什么 `write-if-changed` 能救你的构建时间。

[Ch 15 多文件输出与增量构建：`-d`、`write-if-changed` 与 CMake 契约](./15-multifile-and-incremental.md)
