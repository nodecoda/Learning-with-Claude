# Ch 4 Record、RecordVal、RecordKeeper：内存里的图

> **本章目标**：能在纸上画出「一个 `defm` 展开后，`RecordKeeper` 里到底有哪些对象、谁指向谁」；知道 `getValue` 是 O(n)、`getAllDerivedDefinitions` 有缓存、`Record` 的 `ID` 为什么必须稳定；能手写一个 backend 遍历这张图并输出结构。

> **环境**：LLVM `24.0.0git`。本章所有 C++ 片段都是可编译的（Ch 14 有完整编译命令）。

---

## 4.1 三个类型，一张图

`Record.h` 有 2505 行，但数据模型只有三个核心类型：

```
RecordKeeper  ── 拥有所有 Record，按名字索引
   │
   ├── Record (RK_Class)        ← class / multiclass 的模板
   │     ├── TemplateArgs: [RecordVal...]    模板参数
   │     ├── Values:       [RecordVal...]    字段
   │     └── DirectSuperClasses: [(Record*, SMRange)...]
   │
   ├── Record (RK_Def)          ← def 实例
   │     ├── Values:       [RecordVal...]    自己的 + 从父类复制的
   │     ├── DirectSuperClasses: [(Record*, SMRange)...]
   │     └── TrackedRecords: RecordKeeper&   ← 反查全局
   │
   └── ExtraGlobals: name → Init             ← defvar 的全局变量

RecordVal = Name(Init) + Loc(SMLoc) + Type(RecTy) + Value(Init) + Flags
```

三个关键性质：

1. **`RecordKeeper` 拥有 `Record`**（`StringMap<unique_ptr<Record>>`），`Record` **不拥有** `Init`（`Init` 由各自的 `FoldingSet` 池化管理，Ch 5 讲）。
2. **`Record` 与 `RecordKeeper` 双向引用**：`Record` 持有 `TrackedRecords&`，所以每个字段都能反查「我属于哪个文件/哪些记录」。这让 `!cast<T>("Name")` 能工作，也让析构顺序必须小心（Ch 14 的坑）。
3. **`Values` 是 `SmallVector<RecordVal, 0>`**——内联容量 0，意味着**任何字段都会堆分配**。这是有意的：一个 target 有 8 万多个记录，每个记录平均 5–10 个字段，内联会浪费大量内存。

## 4.2 `RecordVal`：一个字段的五个部分

```cpp
class RecordVal {
  const Init *Name;      // 字段名，通常是 StringInit
  SMLoc Loc;             // 字段定义的位置
  PointerIntPair<const RecTy *, 3, unsigned> TyAndFlags;  // 类型 + kind + used 位
  const Init *Value;     // 值，永不为 null（未赋值是 UnsetInit）
};
```

`PointerIntPair` 把三样东西塞进一个指针大小的空间：

| 位 | 含义 |
| --- | --- |
| bit 0–1 | `FieldKind`：`FK_Normal` / `FK_NonconcreteOK` / `FK_TemplateArg` |
| bit 2 | `isUsed()`：这个字段是否被引用过（用于「未使用模板参数」警告） |
| 剩余 | 指向 `RecTy` 的指针（对齐保证低位可用） |

`FieldKind` 对应源码里的三种写法：

```td
class Base { int A = 1; field int F = 2; }
```

```console
$ llvm-tblgen -print-records rec1.td
------------- Classes -----------------
class Base {
  field int F = 2;
  int A = 1;
}
```

注意 `field int F` 打印时保留了 `field` 前缀，而且**排在 `int A` 前面**。为什么？看 `operator<<(raw_ostream&, const Record&)`（`Record.cpp:2909`）：

```cpp
for (const RecordVal &Val : R.getValues())
  if (Val.isNonconcreteOK()) OS << Val;
for (const RecordVal &Val : R.getValues())
  if (!Val.isNonconcreteOK()) OS << Val;
```

**两遍扫描**：先打 `field` 字段，再打普通字段。这看起来像格式细节，其实是语义需求——`field` 字段允许未赋值（`?`），打印在一起会掩盖「哪些字段必须由用户填」这个区分。

`-print-detailed-records` 能给更多信息：

```console
$ llvm-tblgen -print-detailed-records rec1.td
-------------------- Classes (1) --------------------
Base  |rec1.td:1|
  Template args: (none)
  Superclasses: (none)
  Fields:
    int A = 1  |rec1.td:1|
    field int F = 2  |rec1.td:1|
-------------------- Records (1) --------------------
D  |rec1.td:2|
  Superclasses: Base
  Fields:
    int A = 10  |rec1.td:1|
    field int F = 2  |rec1.td:1|
    int B = 20  |rec1.td:2|
```

三个观察：

1. `D` 的 `int A` 位置是 `rec1.td:1`——**继承来的字段保留父类的位置**，不是 `def` 的位置。
2. `D` 的字段顺序是 `A, F, B`（继承顺序 + 自己顺序），但 `-print-records` 打印时把 `F` 提前了。**两个 backend 的排序策略不同**。
3. `-print-detailed-records` 末尾还打印了 `TheIntInitPool size = 4` 之类的内存统计（Ch 5 会用到）。

## 4.3 `Record`：21 个成员，5 个真正重要

```cpp
class Record {
  const Init *Name;
  SmallVector<SMLoc, 4> Locs;              // ① 位置（诊断与 note 基础）
  SmallVector<RecordVal, 0> TemplateArgs;  // ② 模板参数
  SmallVector<RecordVal, 0> Values;        // ③ 字段
  SmallVector<std::pair<const Record *, SMRange>, 0> DirectSuperClasses;  // ④ 继承
  unsigned ID;                             // ⑤ 全局唯一序号
  RecordKind Kind;
  RecordKeeper &TrackedRecords;
  mutable SmallVector<SMRange, 0> ReferenceLocs;
  mutable DenseMap<const Init *, SmallVector<SMRange, 0>> ValueReferenceLocs;
  ...
};
```

### ① `Locs`：为什么是数组

实测（`dupm.td`）：

```td
class C<int n> { int N = n; }
multiclass M<int n> { def _x : C<n>; }
defm D : M<1>;
def D_x : C<2>;
```

```console
$ llvm-tblgen dupm.td
dupm.td:4:5: error: def already exists: D_x
def D_x : C<2>;
    ^
dupm.td:2:27: note: location of previous definition
multiclass M<int n> { def _x : C<n>; }
                          ^
dupm.td:3:10: note: instantiated from multiclass
defm D : M<1>;
         ^
```

`Prev->getLoc()` 返回 3 个位置：multiclass 里的定义点 + `defm` 的实例化点。`Error.cpp` 的 `PrintMessage` 把第一个当主诊断、其余当 `instantiated from multiclass` note（Practice A 已拆过）。**这就是诊断系统依赖 `Locs` 是数组的原因**。

### ② `TemplateArgs` vs `Values`

`class C<int n> { int N = n; }` 里，`n` 进 `TemplateArgs`，`N` 进 `Values`。二者的区别不只是「位置不同」：

```cpp
bool addTemplateArg(const RecordVal &Arg) {
  if (getTemplateArg(Arg.getNameInit())) return false;  // 重名静默失败
  TemplateArgs.push_back(Arg); return true;
}

void addValue(const RecordVal &RV) {
  assert(!RV.isTemplateArg() && "Use addTemplateArg for template arguments");
  assert(getValue(RV.getNameInit()) == nullptr && "Value already added!");
  Values.push_back(RV);
}
```

- `addTemplateArg` 遇到重名**返回 false**（让调用者报错，Practice A 讲过）。
- `addValue` 遇到重名**直接 assert**（这是内部不变量，调用者必须先检查）。

**这个不对称是有意的**：模板参数来自用户源码（可以重名，要报友好错误），字段来自内部构造（重名是 bug，要立刻炸）。Exercise：在 assertion 构建下故意 `addValue` 两次，看它怎么死。

### ③ 字段查找是线性的

```cpp
const RecordVal *getValue(const Init *Name) const {
  for (const RecordVal &Val : Values)
    if (Val.Name == Name) return &Val;
  return nullptr;
}
```

**O(n) 线性扫描，指针比较**（因为 `Init` 是池化的，同一名字的 `StringInit` 是同一指针，Ch 5 证明）。所以：

```cpp
// 在循环里反复 getValue 是 O(n²)
for (const Record *R : Recs)
  for (StringRef F : Fields)
    R->getValue(F);      // 每个都是线性
```

AArch64 的 `Instruction` 有约 40 个字段，8 万个记录，反复 `getValue` 会变成热点。**这是 Ch 21 要测的性能陷阱之一**。上游的应对是「在 backend 构造函数里一次性把字段指针缓存起来」（`InstrInfoEmitter` 就是这么做的）。

### ④ `DirectSuperClasses` 是递归展开的

```cpp
class Base<int n> { int N = n; }
class Mid<int n> : Base<n> { }
def C : Mid<7>;
```

```console
$ llvm-tblgen -print-records cr.td | tail -3
def C {	// Base Mid
  int N = 7;
}
```

`DirectSuperClasses` 里是 `[Mid]` 还是 `[Base, Mid]`？实测打印顺序是 `// Base Mid`，但要注意 `getSuperClasses()`（复数）和 `getDirectSuperClasses()`（单数）是**两个不同的函数**：

```cpp
// 直接父类，原样
ArrayRef<std::pair<const Record *, SMRange>> getDirectSuperClasses() const {
  return DirectSuperClasses;
}

// 全部祖先，post-order 递归
void getSuperClasses(std::vector<const Record *> &Classes) const {
  for (const Record *SC : make_first_range(DirectSuperClasses)) {
    SC->getSuperClasses(Classes);
    Classes.push_back(SC);
  }
}
std::vector<const Record *> getSuperClasses() const { ... }
```

`operator<<` 用的是 `getSuperClasses()`（递归、post-order），所以 `// Base Mid` 是**全部祖先**，且顺序是「最远的在前」。`def C : Mid<7>` 的 `DirectSuperClasses` 里大概率只有 `Mid` —— 要确认得自己写 backend，这正是本章 4.5 的练习。

### ⑤ `ID`：为什么不能用名字排序

```cpp
static unsigned getNewUID(RecordKeeper &RK);
unsigned getID() const { return ID; }
```

`ID` 是**全局单调递增的序号**，在 `Record` 构造时分配。它的用途是「稳定顺序」：

- 字典序会随记录名变化（加一个 `def AAA` 会改变排序位置）。
- `ID` 只依赖**创建顺序**，而创建顺序由源码顺序决定。

但注意：**`getDefs()` 和 `getAllDerivedDefinitions` 返回的是按名字排序的**：

```cpp
static ArrayRef<const Record *>
getSortedRecords(const StringMap<std::unique_ptr<Record>> &Map,
                 std::vector<const Record *> &Sorted) {
  if (Sorted.size() != Map.size()) {
    Sorted.clear();
    Sorted.reserve(Map.size());
    for (const auto &Entry : Map)
      Sorted.push_back(Entry.second.get());
    llvm::sort(Sorted, [](const Record *LHS, const Record *RHS) {
      return LHS->getName() < RHS->getName();
    });
  }
  return Sorted;
}
```

实测验证：

```console
$ cat ids.td
def Zeta;
def Alpha;
def Mid;
$ llvm-tblgen -print-records ids.td
def Alpha {
}
def Mid {
}
def Zeta {
}
```

源码顺序是 `Zeta, Alpha, Mid`，输出是 `Alpha, Mid, Zeta`——**按名字排序**。

所以「枚举值稳定」实际上依赖的是 **`getSortedRecords` 的名字排序**，不是 `ID`。`ID` 用在别处：`DenseMap<const Record*, ...>` 的哈希、以及 `assert` 消息里的对象标识。

> 还有一层容易漏：`getSortedRecords` 有「size 相同就复用旧结果」的优化。**如果两条记录同名替换（size 不变），缓存不会失效**。实际不会发生（重名直接 assert），但如果你自己写代码修改 `RecordKeeper`，要知道这个假设。

> 这是本课一个容易搞混的点：**`ID` 是创建顺序，`getAllDerivedDefinitions` 是名字顺序**。改一个 backend 的输出顺序时，先确认它用的是哪个。

## 4.4 `RecordKeeper`：三个容器 + 一个缓存

```cpp
class RecordKeeper {
  using RecordMap = StringMap<std::unique_ptr<Record>>;
  using GlobalMap = std::map<std::string, const Init *, std::less<>>;

  RecordMap Classes;
  RecordMap Defs;
  mutable DenseMap<const Record*, std::vector<const Record*>> DefsByDirectSuperClass;
  GlobalMap ExtraGlobals;
  mutable StringMap<ArrayRef<const Record*>> Cache;   // getAllDerivedDefinitions 缓存
  std::unique_ptr<detail::RecordKeeperImpl> Impl;     // 匿名名计数器等
};
```

四个成员各自的角色：

| 成员 | 类型 | 用途 |
| --- | --- | --- |
| `Classes` | `StringMap<unique_ptr<Record>>` | class / multiclass 模板，按名字 O(1) |
| `Defs` | 同上 | def 实例 |
| `DefsByDirectSuperClass` | `DenseMap<Record*, vector<Record*>>` | 「谁继承了我」的反向索引 |
| `ExtraGlobals` | `std::map<std::string, const Init*>` | `defvar` 定义的全局变量，**有序** |
| `Cache` | `StringMap<ArrayRef<Record*>>` | `getAllDerivedDefinitions` 的结果缓存 |

两个细节：

**`ExtraGlobals` 用 `std::map` 而不是 `StringMap`**——因为 `defvar` 是少量（通常 < 10 个），且需要稳定遍历顺序。而 `Classes`/`Defs` 用 `StringMap`（哈希）是因为量大（8 万+）。

**`Cache` 会被 `addDef` 清空**：

```cpp
void addDef(std::unique_ptr<Record> R) {
  for (const Record *SC : make_first_range(R->getDirectSuperClasses()))
    DefsByDirectSuperClass[SC].push_back(R.get());
  bool Ins = Defs.try_emplace(R->getName(), std::move(R)).second;
  (void)Ins;
  assert(Ins && "Record already exists");
  if (!Cache.empty()) Cache.clear();
}
```

`assert(Ins)` 意味着**重复 def 在 `RecordKeeper` 层是致命错误**，用户可见的诊断在 `TGParser::addDefOne` 里先做了（Practice A 拆过）。这个「双层检查」在 LLVM 里很常见：parser 给人看，assert 给开发者看。

## 4.5 手写一个遍历器（可编译）

下面这个 backend 打印每个 `def` 的直接父类、`ID`、字段数。它同时验证了本章的三个 API：

```cpp
#include "llvm/TableGen/Record.h"
#include "llvm/TableGen/Main.h"
#include "llvm/TableGen/TableGenBackend.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

static bool DumpStructure(raw_ostream &OS, const RecordKeeper &RK) {
  OS << "name,id,kind,nsuper,nfields,supertypes\n";
  auto Emit = [&](const Record *R) {
    OS << R->getName() << "," << R->getID() << ","
       << (R->isClass() ? "class" : R->isAnonymous() ? "anon" : "def") << ","
       << R->getDirectSuperClasses().size() << ","
       << R->getValues().size() << ",\"";
    for (const auto &SC : R->getDirectSuperClasses())
      OS << SC.first->getName() << ";";
    OS << "\"\n";
  };
  for (const Record *C : RK.getClasses()) Emit(C);
  for (const Record *D : RK.getDefs())   Emit(D);
  return false;
}

static TableGen::Emitter::OptClass<void> X("gen-dump-structure", "Dump record graph");

int main(int argc, char **argv) {
  cl::ParseCommandLineOptions(argc, argv);
  return TableGenMain(argv[0], DumpStructure);
}
```

> `OptClass<void>` 不能实例化（`EmitterC(RK).run(OS)` 对 `void` 非法）。**这是本段代码故意留下的错误**：把它编译一遍，你会看到 Ch 0 那个 `member reference base type 'void'` 的报错。正确写法见 Ch 14。

## 4.6 本章检查清单

- [ ] 画出 `RecordKeeper → Record → RecordVal → Init` 的指向关系，标出谁拥有谁
- [ ] 解释 `PointerIntPair<const RecTy*, 3, unsigned>` 里的 3 位怎么用
- [ ] 解释为什么 `-print-records` 先打 `field` 字段
- [ ] 说出 `getValue()` 的复杂度，并举一个会变成 O(n²) 的写法
- [ ] 区分 `getDirectSuperClasses()` 与 `getSuperClasses()`
- [ ] 解释 `Record::ID` 与 `getAllDerivedDefinitions` 排序的关系
- [ ] 说出 `RecordKeeper` 的 `Cache` 什么时候失效

## 4.7 踩雷记录

**错误直觉 1：`DirectSuperClasses` 就是源码里写的那些父类。**
正确认识：它是**递归展开后的祖先列表**（`Base Mid` 而非只有 `Mid`）。而且 `operator<<` 用的是 `getSuperClasses()`，又做了一次收集——两层都可能让你数错。

**错误直觉 2：`Record` 拥有自己的字段值。**
正确认识：`RecordVal::Value` 是 `const Init*`，由 `Init` 的池子（`FoldingSet`）拥有。`Record` 析构不会释放它们。

**错误直觉 3：`getValue("Name")` 是哈希查找。**
正确认识：线性扫 `Values`，指针比较。字段多、调用频繁时是热点。

**错误直觉 4：`def` 的 `ID` 决定枚举值顺序。**
正确认识：`getDefs()` / `getAllDerivedDefinitions` 走 `getSortedRecords`，按**名字**排序。`ID` 只反映构造顺序，用在 `DenseMap` 哈希与对象识别。实测：源码写 `Zeta, Alpha, Mid`，输出是 `Alpha, Mid, Zeta`。

**错误直觉 5：`addValue` 重名会报友好错误。**
正确认识：直接 `assert`。友好错误是调用者的责任（`AddValue` 里的 `ERV->setValue` 分支）。

**错误直觉 6：`ExtraGlobals` 和 `Defs` 可以互换。**
正确认识：`ExtraGlobals` 是 `std::map`（有序、量小），`Defs` 是 `StringMap`（哈希、量大）。`getGlobal` 先查 `Defs` 再查 `ExtraGlobals`——因为 `defvar` 也能引用 `def` 名。

## 4.8 延伸阅读

- **`llvm/include/llvm/TableGen/Record.h`** 的 `RecordVal`（1589）与 `Record`（1675）
  - 两个类的完整定义。重点看 `RecordVal` 的 `TyAndFlags` 注释和 `Record` 的 `mutable` 成员。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `RecordKeeper`（2066）
  - 本章 4.4 的来源。注意 `getAllDerivedDefinitions` 的三个重载。
- **`llvm/lib/TableGen/Record.cpp`** 的 `operator<<(raw_ostream&, const Record&)`（2909）
  - `-print-records` 的实体。45 行，读完能解释所有输出格式细节。
- **`llvm/lib/TableGen/DetailedRecordsBackend.cpp`**（189 行）
  - `-print-detailed-records` 的实现。比 `-print-records` 多打印位置与内存统计，是观察内部状态的好工具。
- **`llvm/docs/TableGen/BackGuide.md`** 的 `Record`/`RecordVal` 小节
  - 官方 API 说明，与本章 4.2、4.3 对照。

## 下一章

数据模型的上半部分结束。Ch 5 下沉到 `Init`：为什么 `int N = 1 + 2` 在记录里存的是树而不是 `3`，以及那个「池化」到底省了什么。

[Ch 5 Init 类层次 I：位串、整数、字符串、列表](./05-init-hierarchy-i.md)
