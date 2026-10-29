# 练习 B：用 C++ 复刻 `-print-records`（逐字节一致版）

> **目标**：`-print-records` 是全书出现最多次的调试工具，也是 TableGen 唯一一个「把整个 `RecordKeeper` 摊平成文本」的入口。这个练习要求你**从零写一个替代品**，在四个真实 backend（AArch64 / X86 / RISCV / ARM）上与官方输出的**每一个字节**都相同。写完之后你会知道：这个 30 行的 `operator<<` 里藏了四个不显眼但都能咬人的决策。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。本章所有输出都在本机实跑：主构建 `/home/ubuntu/llvm/llvm-dev/build`（Release，assertions **OFF**），源码树 `/home/ubuntu/llvm/llvm-project`（实验全程未改动）。链接用的静态库来自 `/tmp/tgbuild`（只编 `llvm-tblgen` 的最小构建，见「练习 C」Step 0）。

---

## 为什么选这个练习

改 TableGen 源码的人会遇到三种「读码障碍」：**看不懂 `Init` 层次**（Ch 5–7 讲过）、**不知道 backend 该从哪个 API 拿数据**（Ch 14 讲过）、以及**无法确认自己的理解是否正确**。

第三种最阴险。你读完 Ch 4 会以为「`def` 就是一堆字段」，但 `-print-records` 的输出会告诉你：字段分两类、模板参数根本不在 `getValues()` 里、父类链是**后序且允许重复**的、匿名记录会被命名成 `anonymous_17` 而排序时按**字节序**插在 `anonymous_1` 和 `anonymous_2` 之间。

复刻这个工具的价值在于：**输出是逐字节比对的**。任何一处理解偏差——少一个缩进、字段顺序反了、把 `code` 打成 `string`——都会立刻暴露成 diff。这是比「读代码觉得自己懂了」硬得多的验收标准。

而且它顺手给你三样东西：

1. 一个能挂在任何 `.td` 上的**结构探测器**（比 `grep` 强，比 `-print-detailed-records` 轻）；
2. 一份关于 `Record`/`RecordVal`/`Init` 三者的**精确接口清单**；
3. 一次「上游代码其实有 bug」的现场教学（最后一步）。

## 任务规格

写一个独立程序 `myprint`，接受 `-I <dir>` 与一个 `.td` 文件，输出与 `llvm-tblgen -print-records` **完全相同**的字节流。

验收标准（四条，全部要过）：

| # | 条件 |
|---|---|
| 1 | `myprint a.td` 与 `llvm-tblgen -print-records a.td` 的 `cmp` 返回 0 |
| 2 | 四个真实 backend 的 AArch64.td / X86.td / RISCV.td / ARM.td 全部 `cmp` 返回 0 |
| 3 | 五次运行 md5 不变（输出必须是确定性的） |
| 4 | 单文件不超过 120 行 C++（不含空行与注释） |

第 4 条不是洁癖：**120 行装得下**，是因为真正的打印逻辑只有 30 行。如果你的版本超过 120 行，说明你在重新发明 `RecordVal::print` 已经在做的事。

---

## Step 0（15 分钟）：最小构建与链接

不需要 cmake，直接拿现成的静态库链接即可。本机实测**只需要三个库**：

```bash
g++ -std=c++17 -O2 \
  -I/home/ubuntu/llvm/llvm-project/llvm/include \
  -I/tmp/tgbuild/include \
  minimal.cpp -o minimal \
  /tmp/tgbuild/lib/libLLVMTableGen.a \
  /tmp/tgbuild/lib/libLLVMSupport.a \
  /tmp/tgbuild/lib/libLLVMDemangle.a
```

三个库分别负责：`Record`/`Init`/parser（TableGen）、`MemoryBuffer`/`raw_ostream`/`SourceMgr`（Support）、名字反修饰（Demangle，`LLVMSupport` 里有引用）。

`/tmp/tgbuild/include` 是必需的：`llvm/Config/llvm-config.h` 这类**生成**头文件只存在于构建目录，不在源码树里。少了它你会看到：

```console
fatal error: llvm/Config/llvm-config.h: No such file or directory
```

> **别做的事**：不要链接 `libLLVMTableGenCommon.a`。那是 `CodeGenSchedule`/`CodeGenTarget` 等 CodeGen 专用分析的库，拉进来还要补 `-lLLVMTableGenBasic -lLLVMCodeGenTypes -lLLVMGlobalISel` 三四个依赖，而 `-print-records` 一个都用不上。这一条能帮你省 20 分钟。

**验收**：`ls -l minimal` 显示产物约 2.3 MB（本机 2,384,000 字节）。

---

## Step 1（10 分钟）：五行版本先跑通

在拆解之前，先确认「问题可解」。整个 `-print-records` backend 的实现（`llvm/utils/TableGen/Basic/TableGen.cpp:35-37`）只有三行：

```cpp
static void printRecords(const RecordKeeper &Records, raw_ostream &OS) {
  OS << Records; // No argument, dump all contents
}
```

所以复刻它的**最小版本**是：

```cpp
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TableGen/Parser.h"
#include "llvm/TableGen/Record.h"
#include <memory>
using namespace llvm;
int main(int argc, char **argv) {
  RecordKeeper Records;
  SourceMgr SrcMgr;
  std::vector<std::string> Inc;
  const char *File = nullptr;
  for (int I = 1; I < argc; ++I) {
    StringRef A(argv[I]);
    if (A == "-I" && I + 1 < argc) Inc.push_back(argv[++I]);
    else File = argv[I];
  }
  auto Buf = MemoryBuffer::getFileOrSTDIN(File, true);
  SrcMgr.AddNewSourceBuffer(std::move(*Buf), SMLoc());
  SrcMgr.setIncludeDirs(Inc);
  if (TableGenParseFile(SrcMgr, Records)) return 1;
  outs() << Records;
  outs().flush();
  return 0;
}
```

本机实测，这个 25 行（非空）的版本在 AArch64 上就已经与官方**逐字节一致**：

```console
$ D=/home/ubuntu/llvm/llvm-project/llvm/lib/Target/AArch64
$ INC="-I $D -I /home/ubuntu/llvm/llvm-project/llvm/lib/Target -I /home/ubuntu/llvm/llvm-project/llvm/include"
$ ./src/minimal $INC $D/AArch64.td | md5sum
75d48e20003200dd07e7e567f95b6f88  -
$ md5sum mine_a64.txt      # 官方 llvm-tblgen -print-records 的输出
75d48e20003200dd07e7e567f95b6f88  mine_a64.txt
```

到这里你可能想问：**那这个练习还有什么好做的？** 因为「调用 `operator<<`」和「理解 `operator<<`」是两件事，而你要达到的是后者。下面拆开它。

> `-print-records` 是**默认 backend**（`TableGen.cpp:59` 的 `ByDefault` 参数是 `true`），所以 `llvm-tblgen foo.td` 与 `llvm-tblgen -print-records foo.td` 输出相同，本机实测 `diff` 为空。

---

## Step 2（40 分钟）：拆开 `operator<<`，自己写一遍

三层结构，逐层拆：

```
RecordKeeper  operator<<   ←  两个 section 头 + 遍历
  └─ Record   operator<<   ←  名字、模板参数、父类注释、字段
       └─ RecordVal::print ←  一个字段 = 类型 + 名字 + 值
```

### 2.1 `RecordKeeper`（`Record.cpp:3154-3163`）

```cpp
raw_ostream &llvm::operator<<(raw_ostream &OS, const RecordKeeper &RK) {
  OS << "------------- Classes -----------------\n";
  for (const Record *C : RK.getClasses())
    OS << "class " << *C;

  OS << "------------- Defs -----------------\n";
  for (const Record *D : RK.getDefs())
    OS << "def " << *D;
  return OS;
}
```

两个细节：

- 段头是**硬编码字符串**，`Classes` 段 13 个前导连字符、`Defs` 段 13 个，末尾都是 16 个连字符。数错了就 diff。
- `os << "class " << *C` 里的 `*C` 会调用下面的 `operator<<(raw_ostream&, const Record&)`——**`class`/`def` 前缀不在 `Record` 里**，因为同一个 `Record` 既可能是 class 也可能是 def。

### 2.2 `Record`（`Record.cpp:2909-2940`）

```cpp
raw_ostream &llvm::operator<<(raw_ostream &OS, const Record &R) {
  OS << R.getNameInitAsString();

  ArrayRef<RecordVal> TArgs = R.getTemplateArgs();
  if (!TArgs.empty()) {
    OS << "<";
    ListSeparator LS;
    for (const RecordVal &TA : TArgs) {
      OS << LS;
      TA.print(OS, false);
    }
    OS << ">";
  }

  OS << " {";
  std::vector<const Record *> SCs = R.getSuperClasses();
  if (!SCs.empty()) {
    OS << "\t//";
    for (const Record *SC : SCs)
      OS << " " << SC->getNameInitAsString();
  }
  OS << "\n";

  for (const RecordVal &Val : R.getValues())
    if (Val.isNonconcreteOK())
      OS << Val;
  for (const RecordVal &Val : R.getValues())
    if (!Val.isNonconcreteOK())
      OS << Val;

  return OS << "}\n";
}
```

四个决策，每个都能单独写一节：

**(a) 模板参数打印在 `<...>` 里，而且名字是「限定名」。**

```console
class Ins<bits<8> Ins:e = { ?, ?, ?, ?, ?, ?, ?, ? }> {
```

`Ins:e` 不是笔误。`TGParser.cpp:121-131` 的 `QualifyName` 把类里的模板参数名重写成 `类名:参数名`（multiclass 用 `::`，class 用 `:`），这样继承链上两个类的同名参数不会撞车。你在 `.td` 里写的是 `class Ins<bits<8> e>`，打印出来是 `Ins:e`。**这一条让 `-print-records` 的输出无法原样喂回 `llvm-tblgen`**（Step 6 会实测）。

**(b) 父类注释用 `\t//` 前缀，列表是后序（post-order）、允许重复。**

```console
class D {	// A B A C
```

`D : B, C`，`B : A`，`C : A`，`getSuperClasses()` 递归先吐父类再吐自己，于是 A 出现两次。AArch64 上带这个注释的行共 **89,336** 行：87,314 个 def（占 99.96%）+ 2,022 个 class（占 90.0%），说明几乎每个记录都有父类。

**(c) 字段分两遍打印：`field` 关键字字段先，普通字段后。**

```console
$ cat l.td
class C {
  int A = 1;
  field int B = 2;
  int C2 = 3;
  field bits<2> D = { 1, 0 };
}
def X : C;
$ llvm-tblgen -print-records l.td
class C {
  field int B = 2;
  field bits<2> D = { 1, 0 };
  int A = 1;
  int C2 = 3;
}
```

**声明的顺序被重排了**。`RecordVal::isNonconcreteOK()` 判断的是 `FieldKind == FK_NonconcreteOK`——只有 `field` 关键字声明的字段才是 `true`。分两遍的理由是：`field` 字段允许保持非具体（`?`），打印时先集中列出，读者一眼能看出「这个类的哪些槽还没填」。AArch64 上这样被提前的字段有 **29,031 个**。

**(d) 空记录也不特殊化**，直接输出 `A {\n}`。名字为空（`def ""`）时输出 `def  {`——`def` 后面两个空格，因为 `operator<<` 打印空名字时什么都不写。

### 2.3 `RecordVal`（`Record.cpp:2798-2806` + `Record.h:1670-1673`）

字段的打印由两段代码拼成，这是本练习**最容易踩的坑**：

```cpp
// Record.h:1670
inline raw_ostream &operator<<(raw_ostream &OS, const RecordVal &RV) {
  RV.print(OS << "  ");      // ← 两个空格缩进在这里
  return OS;
}
// Record.cpp:2798
void RecordVal::print(raw_ostream &OS, bool PrintSem) const {
  if (isNonconcreteOK()) OS << "field ";
  OS << getPrintType() << " " << getNameInitAsString() << " = " << *getValue();
  if (PrintSem) OS << ";\n";
}
```

**缩进不由 `print()` 负责，由 `operator<<` 负责。** 所以 `Record::operator<<` 里写 `OS << Val` 时有缩进，而模板参数那里写 `TA.print(OS, false)` 时**没有**缩进——所以模板参数列表长得像 `<bits<8> Ins:e = ...>` 而不是 `<  bits<8> Ins:e = ...>`。

我在第一次实现时就踩了：把 `OS << "  "` 写进了自己的 `printVal()`，然后对 `field` 字段也得到了 `  field bits<32> ...`——看起来对。但同一个函数被模板参数复用时，输出变成 `field   bits<32>`（`field ` + 两个空格 + 类型），AArch64 第 92 行立刻 diff。

`getPrintType()` 也有一个非对称行为（`Record.cpp:2751-2764`）：类型是 `string` 但值来自 `[{...}]` 时，打印成 `code`：

```console
$ cat v.td
def A { string C = [{ x }]; }
$ llvm-tblgen -print-records v.td | tail -3
def A {
  code C = [{ x }];
}
```

也就是说**打印出的类型名由值决定，不是由声明决定**。你从输出里读不到「它当初声明成 string」。

---

## Step 3（30 分钟）：把三层拼起来

参考实现（本机实测逐字节一致；去掉注释与空行是 58 行，落在规格的 120 行上限之内）：

```cpp
#include "llvm/ADT/StringExtras.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TableGen/Parser.h"
#include "llvm/TableGen/Record.h"
#include <memory>
using namespace llvm;

static void printRecord(raw_ostream &OS, StringRef Kw, const Record &R) {
  OS << Kw << " " << R.getNameInitAsString();
  ArrayRef<RecordVal> TArgs = R.getTemplateArgs();
  if (!TArgs.empty()) {
    OS << "<";
    ListSeparator LS;
    for (const RecordVal &TA : TArgs) {
      OS << LS;
      TA.print(OS, false);          // 注意：无缩进、无分号
    }
    OS << ">";
  }
  OS << " {";
  std::vector<const Record *> SCs = R.getSuperClasses();
  if (!SCs.empty()) {
    OS << "\t//";
    for (const Record *SC : SCs)
      OS << " " << SC->getNameInitAsString();
  }
  OS << "\n";
  for (const RecordVal &Val : R.getValues())
    if (Val.isNonconcreteOK()) OS << Val;    // operator<< 加两空格
  for (const RecordVal &Val : R.getValues())
    if (!Val.isNonconcreteOK()) OS << Val;
  OS << "}\n";
}

int main(int argc, char **argv) {
  std::vector<std::string> Inc;
  const char *File = nullptr;
  for (int I = 1; I < argc; ++I) {
    StringRef A(argv[I]);
    if (A == "-I" && I + 1 < argc) Inc.push_back(argv[++I]);
    else if (!File) File = argv[I];
  }
  RecordKeeper Records;
  SourceMgr SrcMgr;
  auto Buf = MemoryBuffer::getFileOrSTDIN(File, /*IsText=*/true);
  if (!Buf) { errs() << "cannot open " << File << "\n"; return 1; }
  SrcMgr.AddNewSourceBuffer(std::move(*Buf), SMLoc());
  SrcMgr.setIncludeDirs(Inc);
  Records.saveInputFilename(File);
  if (TableGenParseFile(SrcMgr, Records)) return 1;

  raw_ostream &OS = outs();
  OS << "------------- Classes -----------------\n";
  for (const Record *C : Records.getClasses()) printRecord(OS, "class", *C);
  OS << "------------- Defs -----------------\n";
  for (const Record *D : Records.getDefs()) printRecord(OS, "def", *D);
  OS.flush();
  return 0;
}
```

三个「看起来该有但没有」的东西，解释一下：

- **没有 `OS << "  "`**：缩进在 `RecordVal` 的 `operator<<` 里，你手写就等于加两次。
- **没有去重父类**：`getSuperClasses()` 返回的是 `std::vector`，重复项要保留（见 2.2b）。
- **没有手动排序**：`getClasses()` / `getDefs()` 内部已经排好（见 Step 4）。

---

## Step 4（20 分钟）：排序规则——不是你想的那样

`Record.cpp:3183-3204`：

```cpp
static ArrayRef<const Record *>
getSortedRecords(const StringMap<std::unique_ptr<Record>> &Map,
                 std::vector<const Record *> &Sorted) {
  if (Sorted.size() != Map.size()) {          // 只有数量变了才重排
    Sorted.clear();
    Sorted.reserve(Map.size());
    for (const auto &Entry : Map)
      Sorted.push_back(Entry.second.get());
    llvm::sort(Sorted, [](const Record *LHS, const Record *RHS) {
      return LHS->getName() < RHS->getName();  // StringRef 的字节序比较
    });
  }
  return Sorted;
}
```

三件事：

1. **按名字排序，不是按定义顺序**。你在 `.td` 里从下往上写 `def`，输出永远是字典序。
2. **`StringRef::operator<` 是字节序**，所以大写字母全部排在前面。AArch64 实测：`Z_s`、`ZeroMinusAPlusB` 之后才是 `aarch64mfp8`（第 18,043 个 def 换段）。
3. **匿名记录的名字是 `anonymous_<十进制>`，也参与字符串排序**，于是出现 `anonymous_0, anonymous_1, anonymous_10, anonymous_100`——**不是数字序**。

实测数据（AArch64）：87,351 个 def 的名单与 `sorted(names)` 完全相等；2,247 个 class 同样。其中 **49,744 个是匿名记录**，占 def 段字节数的 **24.1%**。

> 这一条对 debug 有直接价值：你在 `-print-records` 输出里 `grep anonymous_17` 找不到「第 17 个匿名记录」——它只是名字恰好是 `anonymous_17` 的那个，可能是第 18 个被创建的。

---

## Step 5（20 分钟）：逐字节验收

四个 target 全部 `cmp` 返回 0。本机实测表：

| target | classes | defs | 匿名 def | 输出行数 | 输出字节 |
|---|---:|---:|---:|---:|---:|
| AArch64 | 2,247 | 87,351 | 49,744 | 1,628,881 | 63,139,910 |
| X86 | 1,557 | 87,794 | 39,114 | 3,307,328 | 112,237,089 |
| RISCV | 2,138 | 260,265 | 214,784 | 4,348,777 | 151,246,877 |
| ARM | 1,965 | 48,709 | 22,555 | 942,176 | 36,928,153 |

md5（官方与自写版本相同）：

```console
75d48e20003200dd07e7e567f95b6f88  AArch64
8ca6122dda85165c565a22a8c430c839  X86
740e4d592f2091467228eafa8665c948  RISCV
82248f876ab26988fbe6f7c59d4dd3cd  ARM
```

**为什么 RISCV 的 def 数是 AArch64 的三倍？** 因为 RISCV 的 `.td` 用 `foreach` 把指令按「向量长度 × 类型 × mask 变体」展开，2,138 个 class 里 1,641 个带模板参数；AArch64 是 2,247 个 class 里 1,766 个带参数，但每个参数只展开少数几个值。这不是「AArch64 更简单」，是「RISCV 展开了更多格子」。

`-I` 参数也有讲究。AArch64 需要**两个** include 目录：

```console
$ llvm-tblgen -print-records -I .../AArch64 AArch64.td
error: could not find include file 'llvm/Target/Target.td'   # 缺 llvm/include
$ llvm-tblgen -print-records -I .../llvm/include AArch64.td
error: could not find include file 'AArch64Features.td'      # 缺目标目录
```

`#include "llvm/Target/Target.td"` 是**相对 include 根**的路径，`#include "AArch64Features.td"` 是**同目录**文件。真实构建里 `TableGen.cmake:26-29` 把 `CMAKE_CURRENT_SOURCE_DIR`（目标目录）和 `INCLUDE_DIRECTORIES`（含 `llvm/include`）一起塞给 `-I`，所以你手跑时要手动补。

---

## Step 6（40 分钟）：输出格式的真相表

把「你从 `.td` 里读到的」和「输出里打印的」并排，是这个练习最有价值的部分。全部本机实测：

| 你在 `.td` 里写 | 输出里是 | 为什么 |
|---|---|---|
| `class Ins<bits<8> e>` | `class Ins<bits<8> Ins:e = { ?, ... }>` | `QualifyName`（`TGParser.cpp:121`）+ 默认值也打 |
| `int A = 1;` 先于 `field int B = 2;` | `field int B = 2;` 先 | 两遍循环（`Record.cpp:2932-2937`） |
| `string C = [{ x }];` | `code C = [{ x }];` | `getPrintType()` 看值不看声明 |
| `def ADD : Ins<0x10>` | `def ADD {	// Ins` | 父类只留注释，继承关系丢失 |
| 定义顺序 | 字典序 | `getSortedRecords` |
| `def "" { int X = 1; }` | `def  {` | 空名字 + `"def "` 前缀 |
| 匿名 `Ins<0x31>` | `def anonymous_0 { ... }` | `AnonymousNameInit::getAsString` |
| `string A = "he said \"hi\""` | `string A = "he said "hi""` | **不转义**，见下文 |

最后一条是**真 bug**（本机实测）：

```console
$ cat esc.td
def X {
  string A = "he said \"hi\"";
  string B = "tab\there";
  string C = "back\\slash";
}
$ llvm-tblgen -print-records esc.td | cat -A
def X {$
  string A = "he said "hi"";$
  string B = "tab^Ihere";$
  string C = "back\slash";$
}$
```

`StringInit::getAsString()`（`Record.h:752-757`）就是 `"\"" + Value.str() + "\""`——**没有任何转义**。所以带引号或反斜杠的字符串打印出来后不再是合法 TableGen：

```console
$ printf 'def X {\n  string A = "he said "hi"";\n}\n' > esc2.td
$ llvm-tblgen -print-records esc2.td
esc2.td:2:24: error: expected ';' after declaration
```

这不是「文件里本来就不该有引号」——AArch64 的 `AsmString` 里就有 `"`（`"\t"`、`"\n"` 这类转义序列在 TableGen 字符串里合法）。**这是 `-print-records` 不可 round-trip 的原因之一**。

顺便把另外两个原因也实测掉：

```console
# 原因 2：段头不是注释，也不是合法语法
$ llvm-tblgen -print-records roundtrip.td
roundtrip.td:1:1: error: Unexpected token at top level
------------- Classes -----------------

# 原因 3：class 模板参数打印成 `Ins:e = 值`，而语法只接受 `bits<8> e`
$ sed 's/^-------------.*/\/\/ &/' mine.txt > rt6.td
$ llvm-tblgen -print-records rt6.td
rt6.td:2:22: error: expected '>' at end of template argument list
class Ins<bits<8> Ins:e = { ?, ?, ?, ?, ?, ?, ?, ? }> {
```

还有第四个更隐蔽的：**def 段单独回灌会失败**，因为父类信息只剩注释，`Ins Inner = anonymous_0;` 里的 `Ins` 类型在 def 段没声明。

```console
$ awk '/^------------- Defs/{f=1;next} f' mine.txt > rt_defs.td
$ llvm-tblgen -print-records rt_defs.td
rt_defs.td:10:3: error: Couldn't find class 'Ins'
```

**结论**：`-print-records` 是给人看的**快照**，不是序列化格式。需要机器可读的等价物请用 `-dump-json`（`JSONBackend.cpp`），需要保留 source range 用 `-print-detailed-records`（`DetailedRecordsBackend.cpp`，189 行，比 `-print-records` 的 30 行多出来的是位置、defm 序列和内存分配统计）。

---

## Step 7（30 分钟）：这个工具的性能画像

`-print-records` 本身很便宜，但**便宜在哪里**值得量化，因为下一步优化（或 debug 大文件卡住）会用到。

本机 AArch64 实测，总墙钟 1.61–1.69 s。分段拆解（`timing.cpp` 探针，跑三次取范围）：

| 阶段 | 耗时 | 说明 |
|---|---:|---|
| `TableGenParseFile` | 1.00–1.14 s | 解析 + 建记录，占 60–70% |
| 遍历所有字段 | 0.12–0.14 s | `getValues()` 计数 |
| 格式化 + 写流 | 0.58–0.63 s | 5900 万字符 |

再把「格式化」拆开（`timing3.cpp`）：

| 子项 | 耗时 |
|---|---:|
| `getSuperClasses()` 单独 | 100–133 ms（243,279 条边） |
| `Init::getAsString()` 单独 | 128–171 ms（1667 万字符） |
| `getPrintType()` + `getNameInitAsString()` | 69–83 ms（2537 万字符） |
| 字符串 → `raw_null_ostream` | 146–173 ms |

结论：**打印开销均匀分布在三件事上**（父类链、值的字符串化、类型的字符串化），没有任何一项占掉一半。这也解释了为什么「优化打印」没什么油水——真正的成本在 parse。

两个次要但真实的发现：

**(a) 内存。** 直写 `outs()` 时 RSS 260.9 MB；先攒成 `std::string` 再 `fwrite` 时 **344 MB**（+83 MB，正好是 63 MB 输出的 1.3 倍，`std::string` 的倍增扩容）。官方 `llvm-tblgen -print-records` 是 255.6 MB——比直写的我们还低 5 MB，因为它的 `OutFiles` 机制（`Main.cpp:236`）用移动语义把 `std::string` 交给 `ToolOutputFile`，没有第二次拷贝。

**(b) 空跑基线。** `-null-backend` 在 AArch64 上是 0.93–0.96 s / 171 MB。也就是说 `-print-records` 相对「什么都不做」只多花 0.7 s、多占 85 MB。**如果有人在 TableGen 上抱怨慢，先问清楚他跑的是哪个 backend**——Ch 17 里 `-gen-instr-info` 是 10.19 s，比 `-print-records` 慢一个数量级。

**(c) 长行。** AArch64 输出里最长的一行 **15,829 字节**（第 44,056 行，`class WMMA_REGS` 的一个未折叠 `!if` 表达式）；RISCV 是 18,562 字节。这让 `-print-records` 的输出**不适合用 `diff`/`sort` 之类的行工具做二次加工**——单行几十 KB 的文本会把很多编辑器和工具打趴下。`fmt` 一下再读是常规操作。

---

## 卡点提示

**卡点 1：`cannot find include file`。** 你把 `-I` 写成了「能 include 到 `llvm/Target/Target.td`」就够了，其实还要能 include 到与 `.td` **同目录**的文件。要么两个都加，要么用 `-I` 把 `llvm/lib/Target/<X>` 和 `llvm/include` 都放进去（见 Step 5）。

**卡点 2：`field` 字段多了三个空格。** 你把缩进写进了 `printVal()`，又把它复用到模板参数。正确做法：模板参数用 `TA.print(OS, false)`，字段用它自己的 `operator<<`。

**卡点 3：字段顺序对但 diff 还是报第 92 行。** 你只写了一层循环。`field` 关键字字段要先打完再打普通字段（`isNonconcreteOK()`）。AArch64 有 29,031 个这样的字段，只差一行代码，diff 会是 58,106 行。

**卡点 4：class 和 def 共用了一套打印，`class` 前缀丢了。** 前缀在 `RecordKeeper::operator<<` 里，不在 `Record` 里。

**卡点 5：想 round-trip 但一直失败。** 别修了，这是设计如此（Step 6 列了四个独立原因）。要做序列化请用 `-dump-json`。

---

## 延伸阅读

- **`llvm/lib/TableGen/Record.cpp` 的 2909–2940 行（`operator<<(raw_ostream&, const Record&)`）**
  - 本章 Step 2 的原文。只有 32 行，但四个决策（模板参数位置、父类注释、两遍字段循环、结尾 `}\n`）全在里面。读的时候数一下每处 `OS <<` 产生几个字符——`cmp` 就是这么过的。
- **`llvm/include/llvm/TableGen/Record.h` 的 1589–1674 行（`RecordVal`）**
  - `FieldKind` 三个取值（`FK_Normal`/`FK_NonconcreteOK`/`FK_TemplateArg`）和 `PointerIntPair<const RecTy *, 3, unsigned>` 的位打包。`operator<<` 在 1670 行，就三行，但它是「缩进归属」这个坑的全部来源。
- **`llvm/lib/TableGen/Record.cpp` 的 3181–3204 行（`getSortedRecords`）**
  - 排序 + 缓存失效条件（`Sorted.size() != Map.size()`）。注意它缓存的 `SortedClasses`/`SortedDefs` 是 `RecordKeeper` 的成员——所以一个 `RecordKeeper` 只能有一套顺序，这也是「按定义顺序输出」做不到的根因。
- **`llvm/lib/TableGen/DetailedRecordsBackend.cpp`（全文 189 行）**
  - `-print-detailed-records` 的完整实现。和 `-print-records` 对比着读：多了 `SrcMgr.getFormattedLocationNoOffset`、defm 序列（`reverse(LocList)`）、间接父类的括号标记（`hasDirectSuperClass`）、以及 `dumpAllocationStats`。这是「同一份数据、两种呈现」的范本，也是你做加分挑战 1 的直接参考。
- **`llvm/utils/TableGen/Basic/TableGen.cpp` 的 35–70 行**
  - `printRecords` 三行实现 + `TableGen::Emitter::Opt X[]` 的注册表。注意 `print-records` 的 `ByDefault = true`，以及 `print-sets` 借 `SetTheory` 做的事——后者是 Ch 08 的实时演示。
- **`llvm/test/TableGen/Dag.td` 的 70–90 行**
  - 用 `FileCheck` 对 `-print-records` 做断言的官方测试。注意 `CHECK-NEXT:  dag Dag1 = (somedef1 1:$name1);`——dag 参数名打印成 `值:$名字`，和你手写的 `.td` 语法（`值:$名字` 也是这个顺序）一致，但 `DagInit::getAsString` 里是 `Arg->getAsString() + ":$" + Name`，顺序反了就会在这里被抓住。

## 加分挑战

**挑战 1：给 `myprint` 加一个 `--sort-by-loc`。** 按 `Record::getLoc().front()` 的 `SMLoc` 偏移排序，输出定义顺序而非字典序。你需要自己实现排序（`getDefs()` 已排序，但你拿不到未排序的 `Map`——试试用 `getAllDerivedDefinitions(ArrayRef<StringRef>{})` 或者遍历 `getClasses()` 再查 `getDef()`）。这个练习会逼你发现 `RecordKeeper` 的接口**故意不暴露**插入顺序。

**挑战 2：实现一个 `--stats` 模式。** 统计字段类型直方图、`?` 值数量、最长记录。本机数字可以对照：AArch64 每个 def 平均 16.6 个字段（1,448,485 / 87,351），未设置字段 65,677 个。

**挑战 3（真·上游可提交）：让 `StringInit::getAsString()` 正确转义。** 这个改动的风险在于**所有** backend 都用它（`ImmLeaf` 的 `ImmediateCode`、`AsmString`、调度模型的 `instregex`……），改错会让生成代码里的字符串常量变形。可行路径：只给 `-print-records` 加一个 `printEscaped` 变体，而不是改 `getAsString`。这也是 Ch 22 要讲的「一个看似三行的 PR 为什么需要 RFC」的实例。

---

## 完成后你应该能回答

1. 不看笔记，`-print-records` 的 `operator<<` 为什么要在 `Record` 里打印模板参数、而不是在 `RecordKeeper` 里？
2. 为什么 `-print-records` 的输出不能直接喂回 `llvm-tblgen`？说出至少三个独立原因。
3. `class D : B, C`（B、C 都继承 A）的父类注释为什么是 `// A B A C` 而不是 `// A B C`？
4. `-print-records` 在 AArch64 上 1.6 s，其中多少是 parse、多少是打印？如果要把打印提速一倍，最多能省多少？
5. `field` 关键字字段为什么被提到最前？如果反过来（普通字段先），哪个真实文件会立刻暴露这个差异？

---

## 下一章

你已经能复刻一个 backend 的输出格式。练习 D 更进一步：不是复刻，而是**写一个全新的 backend**——`-gen-isa-stats`，统计指令数、操作数分布与 flag 分布，并把它注册进 `llvm-tblgen`。

[练习 D：写一个 `-gen-isa-stats` backend（并注册进 llvm-tblgen）](./practice-d-isa-stats-backend.md)
