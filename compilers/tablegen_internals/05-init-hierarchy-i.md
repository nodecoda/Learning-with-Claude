# Ch 5 Init 类层次 I：位串、整数、字符串、列表

> **本章目标**：理解「字段的值是一棵树而不是一个值」这个设计；说出 `Init` 的 24 个 `InitKind` 分成哪几组；解释池化（`FoldingSet`）为什么让指针比较等价于值比较；能读懂 `-print-detailed-records` 的内存统计。

> **环境**：LLVM `24.0.0git`。

---

## 5.1 为什么字段里存的不是值

先看一个反直觉的现象：

```td
class Pair<int a, int b> { int Sum = !add(a, b); }
def P : Pair<3, 4>;
```

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

**同一个 `Sum`，在 class 里是表达式，在 def 里是 `7`。** 如果 `RecordVal::Value` 是 `int64_t`，这不可能——class 的 `Sum` 没法用一个整数表示。

设计答案：`RecordVal::Value` 是 `const Init *`，`Init` 是一棵**可以表达「未求值」的表达式树**。于是：

```
class Pair 的 Sum = BinOpInit(BINOP_ADD, VarInit(Pair:a), VarInit(Pair:b))
def   P    的 Sum = IntInit(7)
```

这带来三个后果，本章和后面几章都在处理它们：

1. **类型检查必须在树上做**（Ch 9）。
2. **求值时机必须显式管理**（Ch 7）。
3. **每个「值」都要分配对象，所以必须池化**（本章 5.4）。

## 5.2 `InitKind`：24 个节点的家族树

`Init::InitKind`（`Record.h:305`）按**前序遍历**排列，注释说得很清楚：「laid out by a preorder traversal of the inheritance hierarchy, and does not contain an entry for abstract classes」。也就是说，**这个枚举的相邻关系就是 `classof()` 的实现**：

```cpp
enum InitKind : uint8_t {
  IK_First,
  IK_FirstTypedInit,
  IK_BitInit,          // bit
  IK_BitsInit,         // bits<n>
  IK_DagInit,          // dag
  IK_DefInit,          // 指向一个 def
  IK_FieldInit,        // 字段引用 X.Y
  IK_IntInit,          // int
  IK_ListInit,         // list<T>
  IK_FirstOpInit,      // ← 抽象基类 OpInit 的边界
  IK_BinOpInit,        // !add / # 之类
  IK_TernOpInit,       // !if
  IK_UnOpInit,         // !not / !size
  IK_LastOpInit,
  IK_CondOpInit,       // !cond
  IK_FoldOpInit,       // !foldl
  IK_IsAOpInit,        // !isa
  IK_ExistsOpInit,     // !exists
  IK_InstancesOpInit,  // !instances
  IK_AnonymousNameInit,
  IK_StringInit,       // string / code
  IK_VarInit,          // 变量引用
  IK_VarBitInit,       // 变量的某一位
  IK_VarDefInit,       // 变量形式的 def 引用
  IK_LastTypedInit,
  IK_UnsetInit,        // ?
  IK_ArgumentInit,     // 模板参数位置（未绑定）
};
```

按职责分成五组：

```
                      Init (abstract)
                            │
        ┌───────────────────┴────────────────────┐
   TypedInit (abstract)                    UnsetInit  ArgumentInit
        │
   ┌────┴─────┬────────┬────────┬─────────┬──────────┐
 BitInit  BitsInit DagInit DefInit IntInit ListInit StringInit AnonymousNameInit
                       FieldInit
        │
   ┌────┴──────────────────────────────────────────┐
 OpInit (abstract)                          VarInit VarBitInit VarDefInit
   │
 BinOpInit TernOpInit UnOpInit CondOpInit FoldOpInit IsAOpInit ExistsOpInit InstancesOpInit
```

关键点：

- **`TypedInit` 有 `getType()`**，其余的（`UnsetInit`、`ArgumentInit`）没有具体类型。这是「类型检查在树上做」的基础。
- **`OpInit` 是抽象基类**，`IK_FirstOpInit`/`IK_LastOpInit` 就是它的 `classof` 区间。
- **`StringInit` 同时表示 `string` 和 `code`**（由 `SF_Code` 标志区分）。所以 Ch 2 的 `[{...}]` 最终落到 `StringInit`——但注意它们进的是**两个不同的池**（见 5.4），所以对象身份不同。
- **`VarBitInit` / `VarDefInit` 是 `VarInit` 的「后缀」形态**：`$x{3}` 与 `$x` 是不同节点。

## 5.3 四个最常用的节点

### `IntInit`：64 位、有符号、池化

```cpp
class IntInit final : public TypedInit {
  int64_t Value;
  static IntInit *get(RecordKeeper &RK, int64_t V);
  const Init *getBit(unsigned Bit) const override {
    return BitInit::get(getRecordKeeper(), (Value & (1ULL << Bit)) != 0);
  }
};
```

`getBit` 是「int 可以当位串用」的底层支持——`bits<4> B = 3` 能工作，最后会走到这里（Ch 9、10）。

### `BitInit`：单个位

```cpp
class BitInit final : public TypedInit {
  bool Value;
  const Init *getBit(unsigned Bit) const override {
    assert(Bit < 1 && "Bit index out of range!");
    return this;
  }
  bool isConcrete() const override { return true; }
};
```

注意 `isConcrete()` 恒为 true，但 `BitsInit` 不一定：

```cpp
bool isComplete() const override;   // 所有位都已经确定
bool allInComplete() const;         // 所有位都未确定
bool isConcrete() const override;   // 所有位都是 BitInit
```

**`complete` 和 `concrete` 是两个不同的概念**：

- `isComplete()`：没有 `UnsetInit` 子节点（值可以算出来）。
- `isConcrete()`：每个子节点都是 `BitInit`（已经是最简形式）。

`bits<4> B = ?` 是 incomplete 但 concrete（它是 `UnsetInit`，不是 `BitsInit`）。`bits<4> B = {1, ?, 0, 0}` 是既非 complete 也非 concrete。这个区别在 Ch 10 的位域操作里很重要。

### `BitsInit`：变长、trailing objects

```cpp
class BitsInit final : public TypedInit,
                       public FoldingSetNode,
                       private TrailingObjects<BitsInit, const Init *> {
  unsigned NumBits;
  ArrayRef<const Init *> getBits() const { return getTrailingObjects(NumBits); }
};
```

`TrailingObjects<BitsInit, const Init *>` 是 LLVM 的「柔性数组成员」惯用法：`BitsInit` 对象和它的 `Init*` 数组**在同一块分配里**，省一次指针跳转和一次 malloc。这是 `BitsInit` 比 `ListInit` 更常见的优化（一个 target 的 `bits<32>` 字段可能有几万个）。

**注意 LSB-first**：`ParseSimpleValue` 里构造 `BitsInit` 是

```cpp
for (unsigned i = 0; i != BinaryVal.second; ++i)
  Bits[i] = BitInit::get(Records, BinaryVal.first & (1LL << i));
```

`Bits[0]` 是**最低位**。但 `-print-records` 打印时是 MSB-first。这个不一致是 Ch 10 的踩雷点。

### `ListInit`：同质列表

```cpp
class ListInit final : public TypedInit, public FoldingSetNode {
  using ElementInit = const Init *;
  std::vector<ElementInit> Elements;
};
```

`ListInit` 用 `std::vector`，不是 trailing objects——因为列表长度变化大且常常共享。它的 `getType()` 是 `ListRecTy`，元素类型由 `ListRecTy::getElementType()` 给出。

`ListInit` 有一个容易误解的 API：

```cpp
/// Get the element at the specified index.
const Init *getElement(unsigned I) const { return Elements[I]; }
```

**这是「线性索引」，不是哈希查找**。`list` 在 TableGen 里没有高效随机访问的需求，但 `!foreach` 会遍历它（Ch 11）。

### `StringInit`：string 与 code 合一

```cpp
class StringInit final : public TypedInit, public FoldingSetNode {
public:
  enum StringFormat { SF_String, SF_Code };
private:
  StringFormat Format;
  std::string Value;
};
```

`SF_Code` 就是 Ch 2 的 `[{...}]`。**所以 `code` 字段的值和 `string` 字段的值在内存里是同一种节点**——这也是为什么 `-print-records` 能把 `code C = [{...}]` 原样打出来（它只是按 `Format` 决定要不要加 `[{ }]`）。

## 5.4 池化：为什么指针比较等价于值比较

`Init` 的每个子类都有 `static XxxInit *get(RecordKeeper&, ...)`，不是 `new`。实现靠 `FoldingSet`：

```cpp
// Record.cpp:560
IntInit *IntInit::get(RecordKeeper &RK, int64_t V) {
  IntInit *&I = RK.getImpl().TheIntInitPool[V];
  if (!I)
    I = new (RK.getImpl().Allocator) IntInit(RK, V);
  return I;
}
```

`TheIntInitPool` 就是一个 `DenseMap<int64_t, IntInit*>`，`Allocator` 是 `BumpPtrAllocator`（一次分配大块、永不单独回收）。**同值返回同一指针。**

字符串的池化稍微复杂——它按 `StringFormat` 分两个池：

```cpp
// Record.cpp:638
const StringInit *StringInit::get(RecordKeeper &RK, StringRef V, StringFormat Fmt) {
  detail::RecordKeeperImpl &RKImpl = RK.getImpl();
  auto &InitMap = Fmt == SF_String ? RKImpl.StringInitStringPool
                                   : RKImpl.StringInitCodePool;
  auto &Entry = *InitMap.try_emplace(V, nullptr).first;
  if (!Entry.second)
    Entry.second = new (RKImpl.Allocator) StringInit(RK, Entry.getKey(), Fmt);
  return Entry.second;
}
```

注意 **`"x"` 和 `[{x}]` 是两个不同对象**，因为它们进不同的池。这修正了 5.3 的一个直觉：`string` 和 `code` 共享**类型**（`StringRecTy`），但不共享**对象身份**。

**证据**：`-print-detailed-records` 末尾的内存统计（实测）：

```td
class C { int A = 1; int B = 1; string S = "x"; string T = "x"; }
def D : C;
```

```console
$ llvm-tblgen -print-detailed-records pool.td | sed -n '/Memory Allocation Stats/,$p'
-------------------- Memory Allocation Stats --------------------
TheIntInitPool size = 1          ← 两个 `1` 只占一个节点
StringInitStringPool size = 11   ← 字段名/类名/字符串值都在同一个池
...
Total allocator memory = 4096
Number of records instantiated = 2
```

`TheIntInitPool size = 1` 直接证明：`int A = 1` 和 `int B = 1` 共享同一个 `IntInit` 对象。

这件事的三个后果：

1. **`Record::getValue(StringRef)` 可以用指针比较**（Ch 4）：`Val.Name == Name` 比较的是 `StringInit*`，因为同名一定同指针。
2. **`DenseMap<const Init*, ...>` 是安全的**：同一个值不会有两个键。
3. **不能靠地址判断「不同来源的相同值」**：`1` 和 `1` 是同一个对象，位置信息在 `RecordVal::Loc` 里而不在 `Init` 里。

> 反例：`BitsInit` 也是池化的，但 `bits<4> {1,0,0,0}` 和 `bits<8> {0,0,0,0,1,0,0,0}` 是不同对象（长度不同）。**池化的键是「结构」，不是「数值」**。

## 5.5 用 backend 观察池化（可编译）

下面这段代码直接验证「同值同指针」：

```cpp
#include "llvm/TableGen/Record.h"
#include "llvm/TableGen/Main.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/raw_ostream.h"
using namespace llvm;

static bool CheckInterning(raw_ostream &OS, const RecordKeeper &RK) {
  const Record *D = RK.getDef("D");
  if (!D) { OS << "no def D\n"; return false; }
  const Init *A = D->getValueInit("A");
  const Init *B = D->getValueInit("B");
  OS << "A=" << A << " B=" << B << " same=" << (A == B) << "\n";
  OS << "A is IntInit: " << isa<IntInit>(A) << "\n";
  return false;
}

int main(int argc, char **argv) {
  cl::ParseCommandLineOptions(argc, argv);
  return TableGenMain(argv[0], CheckInterning);
}
```

预期输出（`pool.td`）：

```
A=0x... B=0x... same=1
A is IntInit: 1
```

*本段未在本机编译运行（需要 `clang++` + `libLLVMTableGen.a`，命令见 Ch 14）；行为依据 `IntInit::get` 的池化实现与 `-print-detailed-records` 的池大小证据。*

## 5.6 池化的代价：`Init` 永不释放

`FoldingSet` 与 `BumpPtrAllocator` 的组合意味着：

- `Init` 对象在 `RecordKeeper` 生命周期内**只增不减**。
- 解析 8 万个 def 时，`Init` 的数量是「所有字段值 + 所有中间表达式」的总和。
- 这就是 AArch64 全量解析占 0.55 秒、峰值 RSS 约 295 MB 的原因之一（Ch 21 会测量）。

```console
$ /usr/bin/time -f "maxrss=%MKB" llvm-tblgen -I llvm/include -I llvm/lib/Target/AArch64 \
    -gen-instr-info -o /dev/null llvm/lib/Target/AArch64/AArch64.td
maxrss=295272KB
```

**设计取舍**：如果 `Init` 可以释放，`class` 模板被实例化后其未折叠表达式就该回收。但 TableGen 允许 `!cast<T>("Name")` 在任意时刻反查记录，也允许 backend 持有 `Init*`（`CodeGenInstruction` 就存了一堆），生命周期管理会变得极复杂。**「不释放」换「简单」是这个系统的核心权衡**。

## 5.7 本章检查清单

- [ ] 说出 `InitKind` 的五组分类，以及 `IK_FirstOpInit`/`IK_LastOpInit` 的用途
- [ ] 解释为什么 `class` 里的 `!add(a,b)` 在 `def` 里变成 `7`，以及这对 `RecordVal::Value` 的类型意味着什么
- [ ] 说出 `isComplete()` 与 `isConcrete()` 的区别，各举一个例子
- [ ] 解释 `-print-detailed-records` 里 `TheIntInitPool size = 1` 说明了什么
- [ ] 说出 `code` 和 `string` 在内存里的关系
- [ ] 解释池化的键是「结构」而不是「数值」，并举反例
- [ ] 说出 `Init` 不释放带来的一个好处和一个代价

## 5.8 踩雷记录

**错误直觉 1：`bits<4>` 里的第 0 位是最高位。**
正确认识：`ParseSimpleValue` 用 `1LL << i` 构造 `Bits[i]`，所以 `Bits[0]` 是最低位。但 `-print-records` 打印 MSB-first。**存储与显示的顺序相反**，Ch 10 会用实验证明。

**错误直觉 2：`isConcrete()` 和 `isComplete()` 是一回事。**
正确认识：`?` 是 concrete（它是最简形式）但 incomplete（值未定）。`{1, ?, 0, 0}` 两者都不是。

**错误直觉 3：`StringInit` 只表示字符串字面量。**
正确认识：`code` 字段也是 `StringInit`，只是 `Format == SF_Code`。但两者进**不同的池**（`StringInitStringPool` vs `StringInitCodePool`），所以 `StringInit::get(RK, "x")` 和 `StringInit::get(RK, "x", SF_Code)` 是两个不同指针。写 backend 时别以为 `isa<StringInit>` 就一定是字符串语义。

**错误直觉 4：池化意味着「相同的值一定是同一个对象」。**
正确认识：键是**结构**。`bits<4>{1,0,0,0}` 与 `bits<8>{0,0,0,0,1,0,0,0}` 数值都是 8，但是不同对象（长度不同）。

**错误直觉 5：`Init` 会在解析结束时释放。**
正确认识：`RecordKeeper` 析构才统一释放，且 `BumpPtrAllocator` 一次释放整块。8 万记录时内存峰值就在解析结束时，而不是「用完就回收」。

**错误直觉 6：`ListInit::getElement(i)` 是 O(1) 且安全。**
正确认识：它是 `Elements[i]`，没有边界检查（`std::vector::operator[]`）。查 `!size` 后再访问是调用者的责任。

## 5.9 延伸阅读

- **`llvm/include/llvm/TableGen/Record.h`** 的 `Init`（289–435）与 `TypedInit`（435–470）
  - `InitKind` 枚举 + `Init` 的虚函数表。建议打印出来贴在旁边，Part 1 全程要用。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `BitInit`（575）/ `BitsInit`（608）/ `IntInit`（654）/ `StringInit`（715）/ `ListInit`（768）
  - 本章 5.3 的四个类。重点看 `BitsInit` 的 `TrailingObjects` 用法和 `StringInit::SF_Code`。
- **`llvm/lib/TableGen/Record.cpp`** 的 `*Init::get` 系列
  - 池化的真实实现。搜 `IntInit::get` 看 `FoldingSet` 的插入逻辑。
- **`llvm/lib/TableGen/DetailedRecordsBackend.cpp`**（189 行）
  - `-print-detailed-records` 的实现，包括内存统计怎么收集。
- **LLVM 的 `FoldingSet` 文档**（`llvm/include/llvm/ADT/FoldingSet.h`）
  - 池化的底层数据结构。知道它「按 hash 找同结构节点」就够了，不必读完整实现。

## 下一章

Ch 6 讲剩下的一半：`DagInit`（target 定义里最重要的结构）、`DefInit`（记录之间的引用）、`VarInit`（为什么 `$x` 在 fold 前是独立节点）。

[Ch 6 Init 类层次 II：DAG、DefInit、VarInit 与引用解析](./06-init-hierarchy-ii.md)
