# Ch 9 类型系统：`RecTy` 与类型推断、隐式转换

> **本章目标**：完整说出七种 `RecTy` 之间的可转换关系（含实测矩阵）；理解 `typeIsA` 与 `typeIsConvertibleTo` 是两个不同问题；知道 `getCastTo` 的三段式实现为什么顺序不能换；能自己加一个新类型并判断要改哪些函数。

> **环境**：LLVM `24.0.0git`。

---

## 9.1 只有七种类型

```cpp
class alignas(8) RecTy {
  enum RecTyKind {
    BitRecTyKind,      // bit
    BitsRecTyKind,     // bits<n>
    IntRecTyKind,      // int
    StringRecTyKind,   // string（含 code）
    ListRecTyKind,     // list<T>
    DagRecTyKind,      // dag
    RecordRecTyKind    // 某个 class 的类型
  };
};
```

和 `Init` 的 24 个节点相比，类型只有 7 个——因为**类型不区分「字面量」和「表达式」**。`!add(1,2)` 和 `3` 都是 `int`。

### `RecordRecTy` 是唯一带 payload 的

```cpp
class RecordRecTy final : public RecTy, public FoldingSetNode,
                          private TrailingObjects<RecordRecTy, const Record *> {
  unsigned NumClasses;
  ArrayRef<const Record *> getClasses() const { return getTrailingObjects(NumClasses); }
};
```

它的「类型值」是一组 class 记录（trailing objects）。注释说：

```
/// The list of superclasses is non-redundant, i.e. only contains classes that
/// are not the superclass of some other listed class.
```

举例：如果 `class B : A`，那么 `B` 的类型是 `RecordRecTy([B])`，**不是** `[A, B]`——因为 `A` 已经是 `B` 的父类，冗余。这个归一化让 `this == RHS` 的快速路径有意义（同一组 class 池化成同一个 `RecordRecTy` 对象）。

## 9.2 `typeIsA` vs `typeIsConvertibleTo`

这是本章最重要的区分。两个函数的注释：

```cpp
/// Return true if all values of 'this' type can be converted to the specified type.
virtual bool typeIsConvertibleTo(const RecTy *RHS) const;

/// Return true if 'this' type is equal to or a subtype of RHS. For example,
/// a bit set is not an int, but they are convertible.
virtual bool typeIsA(const RecTy *RHS) const;
```

- **`typeIsA`**：子类型关系（严格）。`bits<4>` is-a `int`？**否**。
- **`typeIsConvertibleTo`**：能否隐式转换。`bits<4>` 能转 `int`？**是**。

基类默认实现：

```cpp
bool RecTy::typeIsA(const RecTy *RHS) const { return this == RHS; }
bool RecTy::typeIsConvertibleTo(const RecTy *RHS) const { return this == RHS; }
```

只有 `ListRecTy` 和 `RecordRecTy` 重写了 `typeIsA`：

```cpp
bool ListRecTy::typeIsA(const RecTy *RHS) const {
  if (const auto *RHSl = dyn_cast<ListRecTy>(RHS))
    return getElementType()->typeIsA(RHSl->getElementType());
  return false;
}

bool RecordRecTy::typeIsA(const RecTy *RHS) const {
  return typeIsConvertibleTo(RHS);      // 对记录类型，"is-a" 就是 "convertible"
}
```

## 9.3 完整转换矩阵（源码 + 实测）

### 源码定义

```cpp
bool BitRecTy::typeIsConvertibleTo(const RecTy *RHS) const {
  if (RecTy::typeIsConvertibleTo(RHS) || RHS->getRecTyKind() == IntRecTyKind)
    return true;
  if (const auto *BitsTy = dyn_cast<BitsRecTy>(RHS))
    return BitsTy->getNumBits() == 1;
  return false;
}

bool BitsRecTy::typeIsConvertibleTo(const RecTy *RHS) const {
  if (RecTy::typeIsConvertibleTo(RHS)) // same type
    return cast<BitsRecTy>(RHS)->Size == Size;
  RecTyKind kind = RHS->getRecTyKind();
  return (kind == BitRecTyKind && Size == 1) || (kind == IntRecTyKind);
}

bool IntRecTy::typeIsConvertibleTo(const RecTy *RHS) const {
  RecTyKind kind = RHS->getRecTyKind();
  return kind == BitRecTyKind || kind == BitsRecTyKind || kind == IntRecTyKind;
}

bool StringRecTy::typeIsConvertibleTo(const RecTy *RHS) const {
  return RHS->getRecTyKind() == StringRecTyKind;    // code 与 string 互转
}

bool ListRecTy::typeIsConvertibleTo(const RecTy *RHS) const {
  if (const auto *ListTy = dyn_cast<ListRecTy>(RHS))
    return ElementTy->typeIsConvertibleTo(ListTy->getElementType());
  return false;
}

bool RecordRecTy::typeIsConvertibleTo(const RecTy *RHS) const {
  if (this == RHS) return true;
  const auto *RTy = dyn_cast<RecordRecTy>(RHS);
  if (!RTy) return false;
  return llvm::all_of(RTy->getClasses(), [this](const Record *TargetClass) {
    return isSubClassOf(TargetClass);
  });
}
```

### 矩阵

| from \ to | `bit` | `bits<n>` | `int` | `string` | `list<T>` | `dag` | `Record` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `bit` | ✓ | 仅 n=1 | ✓ | ✗ | ✗ | ✗ | ✗ |
| `bits<n>` | 仅 n=1 | 仅同 n | ✓ | ✗ | ✗ | ✗ | ✗ |
| `int` | ✓ | ✓（值需放得下） | ✓ | ✗ | ✗ | ✗ | ✗ |
| `string` | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| `list<T>` | ✗ | ✗ | ✗ | ✗ | T 可转 | ✗ | ✗ |
| `dag` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | ✗ |
| `Record` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | 子类 → 父类 |

### 实测验证

**`int` → `bits<4>`**（值 3，放得下）：

```console
$ printf 'def D { bits<4> B = 3; }\n' > bc.td && llvm-tblgen -print-records bc.td
def D {
  bits<4> B = { 0, 0, 1, 1 };
}
```

**`int` → `bit`，值 2（不合法）**：

```console
$ printf 'def X { bit B = 2; }\n' > ty2.td && llvm-tblgen ty2.td
ty2.td:1:5: error: Initializer of 'B' in 'X' could not be fully resolved: !cast<bit>(2)
def X { bit B = 2; }
    ^
```

注意错误信息里的 `!cast<bit>(2)`：**类型检查通过了**（`int` convertible to `bit`），但**转换失败**（`convertInitializerTo` 返回 null），最后报的是「无法完全解析」。这是一个「两阶段失败」的例子：可转换 ≠ 可转换成功。

源码对应 `IntInit::convertInitializerTo`：

```cpp
if (isa<BitRecTy>(Ty)) {
  int64_t Val = getValue();
  if (Val != 0 && Val != 1) return nullptr;  // Only accept 0 or 1 for a bit!
  return BitInit::get(getRecordKeeper(), Val != 0);
}
```

**`bits<8>` → `bits<4>`（不同长度）**：

```console
$ printf 'def X { bits<8> A = 0xFF; bits<4> B = A; }\n' > ty3.td && llvm-tblgen ty3.td
ty3.td:1:39: error: Field 'B' of type 'bits<4>' is incompatible with value 'A' of type 'bits<8>'
```

**`list<int>` ← `int`**：

```console
$ printf 'def X { list<int> L = 1; }\n' > ty4.td && llvm-tblgen ty4.td
ty4.td:1:23: error: Field 'L' of type 'list<int>' is incompatible with value '1' of type 'int'
```

**`bit` → `int`（反向可行）**：

```console
$ printf 'def X { int I = 1; bit B = 1; int J = B; }\n' > ty5.td && llvm-tblgen -print-records ty5.td | tail -5
def X {
  int I = 1;
  bit B = 1;
  int J = 1;
}
```

**记录的上转（子类 → 父类）可以**：

```td
class A { int V = 1; }
class B : A { int W = 2; }
def Da : A;
def Db : B;
class HolderA { A X = ?; }
def H1 : HolderA { let X = Db; }
```

```console
$ llvm-tblgen -print-records ty10.td | tail -6
def H1 {	// HolderA
  A X = Db;
}
```

**下转（父类 → 子类）不行**：

```console
$ llvm-tblgen ty10b.td
ty10b.td:8:24: error: Field 'Y' of type 'B' is incompatible with value 'Da' of type 'A'
def H2 : HolderB { let Y = Da; }
                       ^
```

**`list<A>` ← `list<B>` 也不行**（列表不协变）：

```console
$ llvm-tblgen ty11.td
ty11.td:6:26: error: Element type mismatch for list: element type 'A' not convertible to 'B
def L2 { list<B> Y = [Da]; }
                         ^
```

> **注意这条错误信息缺了一个右引号**：`'B` 而不是 `'B'`。源码在 `TGParser.cpp:3175`：
>
> ```cpp
> TokError(Twine("Element type mismatch for list: element type '") +
>          EltTy->getAsString() + "' not convertible to '" +
>          GivenListTy->getElementType()->getAsString());
> ```
>
> 最后少了 `+ "'"`。这是一个**真实存在、至今未修的上游小 bug**——正好是 Practice A 那类改动的绝佳素材：3 个字符、加个测试、能进 PR。

## 9.4 `getCastTo`：三段式，顺序不能换

```cpp
const Init *TypedInit::getCastTo(const RecTy *Ty) const {
  // Handle the common case quickly
  if (getType()->typeIsA(Ty))
    return this;

  if (const Init *Converted = convertInitializerTo(Ty)) {
    assert(!isa<TypedInit>(Converted) ||
           cast<TypedInit>(Converted)->getType()->typeIsA(Ty));
    return Converted;
  }

  if (!getType()->typeIsConvertibleTo(Ty))
    return nullptr;

  return UnOpInit::get(UnOpInit::CAST, this, Ty)->Fold(nullptr);
}
```

三段：

1. **已经是子类型** → 原样返回（零成本快路径）。
2. **能直接转换** → 调 `convertInitializerTo`（如 `IntInit(3)` → `BitsInit`）。
3. **可转换但需要 `!cast<T>`** → 构造 `UnOpInit(CAST)` 再 `Fold`。这一支处理「运行期才知道能否成功」的情况，比如 `int` → `bit` 的值检查。

**顺序不能换的原因**：如果先做 2，那 `bits<4>` → `bits<4>` 会走 `convertInitializerTo` 而不是快路径，多一次函数调用；如果先做 3，`int` → `bits<4>` 会生成不必要的 `!cast` 节点。**快路径 / 直接转换 / 延迟转换** 是三档成本，必须按成本升序。

## 9.5 `resolveTypes`：列表字面量的类型推断

```cpp
/// Find a common type that T1 and T2 convert to. Return 0 if no such type exists.
const RecTy *resolveTypes(const RecTy *T1, const RecTy *T2);
```

它用在「列表字面量没有显式元素类型」的地方：

```td
list<int> L = [1, 2, 3];      // 有显式类型
defvar M = [1, 2, 3];          // 推断为 list<int>
```

`resolveTypes` 的实现是「找最近公共祖先」：

- 两个相同 → 那个类型。
- 一个是 `bit`、一个是 `int` → `int`（因为 `bit` convertible to `int`）。
- 两个 `RecordRecTy` → 找公共父类。
- 否则 `nullptr`（报错）。

实测列表字面量里出现不可统一类型的元素：

```console
$ printf 'def X { list<int> L = [1, "a"]; }\n' > ty13.td && llvm-tblgen ty13.td
ty13.td:1:31: error: Incompatible types in list elements
def X { list<int> L = [1, "a"]; }
                              ^
```

这条信息来自另一个产生点（`resolveTypes` 返回 `nullptr` 后由 `TGParser.cpp:3143` 附近报出），**和 9.3 那条 `Element type mismatch` 是两个不同的诊断**，描述的问题也不同：

- `Element type mismatch for list: ...`：元素类型与**显式声明的列表类型**不兼容（`list<B> = [Da]`）。
- `Incompatible types in list elements`：字面量内部元素**互相**不能统一（`[1, "a"]`）。

这就是为什么读 TableGen 诊断要精确到「哪一行源码产生」——同一类问题可能有两个完全不同的措辞。

## 9.6 加一个新类型要动哪些地方

假设你要加 `float` 类型（TableGen 至今没有浮点）。清单：

| 步骤 | 文件 | 内容 |
| --- | --- | --- |
| 1 | `Record.h` | `RecTyKind` 加枚举；新增 `FloatRecTy` 类 |
| 2 | `Record.h` | `InitKind` 加 `IK_FloatInit`；新增 `FloatInit` 类 |
| 3 | `TGLexer.h` | `TokKind` 加 `FloatVal`（如果支持字面量） |
| 4 | `TGLexer.cpp` | 词法识别 `3.14` |
| 5 | `TGParser.cpp` | `ParseType` 加 `case`；`ParseSimpleValue` 加字面量分支；`ParseOperation` 加算术 |
| 6 | `Record.cpp` | `FloatInit::get`、`convertInitializerTo`、`getAsString`、`Fold` |
| 7 | `Record.cpp` | 所有 `typeIsConvertibleTo` 加规则 |
| 8 | 所有 backend | 处理 `isa<FloatInit>`（或至少不崩） |
| 9 | `llvm/test/TableGen/` | 新增测试 |

**第 8 步是最容易被忽略的**：`InstrInfoEmitter`、`RegisterInfoEmitter` 等几十个 backend 都可能遇到 `FloatInit`，每个都要判断「我不认识这个类型，应该报错还是跳过」。这就是为什么 TableGen 加新类型是**大工程**——语言核心只有 7 个文件，但消费方有 44 个。

> 这也是上游对「加语法特性」保守的原因。Ch 18 会讲一个真实的加特性案例。

## 9.7 本章检查清单

- [ ] 说出 7 种 `RecTy`，以及哪个带 payload
- [ ] 区分 `typeIsA` 与 `typeIsConvertibleTo`，各举一个例子
- [ ] 默写 `int` 的可转换目标，以及 `bits<n>` 的可转换目标
- [ ] 解释 `int` → `bit` 为什么「类型检查通过但转换失败」
- [ ] 解释 `getCastTo` 三段式的顺序为什么不能换
- [ ] 说出 `list<A>` ← `list<B>` 为什么不行
- [ ] 说出加一个新类型要动几处，哪一处最容易漏

## 9.8 踩雷记录

**错误直觉 1：`bits<4>` 是 `int` 的子类型。**
正确认识：不是。它们只是**可转换**。`typeIsA` 返回 false，`typeIsConvertibleTo` 返回 true。这个区分在 `getCastTo` 的三段式里决定走哪条路。

**错误直觉 2：`int` → `bit` 只要值合法就行。**
正确认识：类型层允许（`IntRecTy::typeIsConvertibleTo` 含 `BitRecTyKind`），但值层要 `0/1`。`bit B = 2` 的报错是「could not be fully resolved」，不是「类型不兼容」——**两阶段失败**。

**错误直观 3：列表是协变的（`list<B>` 可以赋给 `list<A>`）。**
正确认识：不是。`ListRecTy::typeIsConvertibleTo` 要求元素类型**互相可转换**，而 `A` → `B`（下转）不成立。实测报 `Element type mismatch`。

**错误直觉 4：`string` 和 `code` 是不同类型的字段。**
正确认识：同一个 `StringRecTy`，`typeIsConvertibleTo` 直接返回 true。区别只在 `StringInit::Format`。

**错误直觉 5：`resolveTypes` 只在显式调用时用。**
正确认识：列表字面量、`!if` 两支的公共类型、`!cond` 的返回值类型都靠它。它是**类型推断的核心**。

**错误直觉 6：加一个类型只要改 `Record.h`。**
正确认识：至少 9 处，其中 44 个 backend 的兼容性是主要成本。

## 9.9 延伸阅读

- **`llvm/include/llvm/TableGen/Record.h`** 的 `RecTy`（64–290）
  - 七个类型类 + `resolveTypes`。本章 9.1–9.3 的全部来源。
- **`llvm/lib/TableGen/Record.cpp`** 的 `typeIsConvertibleTo` 系列（153–307）
  - 转换规则的全部实现。建议按本章 9.3 的矩阵逐行核对。
- **`llvm/lib/TableGen/Record.cpp`** 的 `TypedInit::getCastTo`（2262）
  - 三段式转换。理解它是理解「隐式 cast 发生在哪」的钥匙。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseType`（约 3300 行）
  - 类型语法到 `RecTy` 的映射。
- **`llvm/lib/TableGen/TGParser.cpp:3175` 的漏引号 bug**
  - 3 个字符的上游修复机会。动手改它 + 加测试，是本章最好的练习。

## 下一章

Ch 10 专讲 `bits<n>` 与位域：为什么存储是 LSB-first 而打印是 MSB-first，`{hi...lo}` 的索引方向，以及 `let B{3...0} = x` 的位操作细节。

[Ch 10 位域、bits<> 与字段偏移的语义](./10-bits-and-fields.md)
