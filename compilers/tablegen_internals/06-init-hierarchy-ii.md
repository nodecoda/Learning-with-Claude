# Ch 6 Init 类层次 II：DAG、DefInit、VarInit 与引用解析

> **本章目标**：读懂 target 定义里最常见的三种结构——`dag`、记录引用、变量引用——在内存里各是什么节点；知道 `DefInit` 为什么可以直接比较指针；理解 `VarInit` 存在的原因是「引用可能比定义早出现」。

> **环境**：LLVM `24.0.0git`。

---

## 6.1 为什么这三种要单独一章

Ch 5 的四个节点（`IntInit`、`BitInit`、`BitsInit`、`StringInit`）都是**自包含**的：它们的值不依赖任何其他记录。而本章的三种都**指向别处**：

```
DagInit   ── 操作符 + 参数，每个参数又可以是任意 Init
DefInit   ── 指向 RecordKeeper 里的某条 Record
VarInit   ── 指向一个名字（可能还没定义）
```

「指向」带来的是生命周期和解析顺序问题，这正是 `resolveReferences` 存在的理由（本章 6.5）。

## 6.2 `DagInit`：一个双向 trailing objects 结构

`dag` 是 LLVM target 定义里最重要的类型。一个 `(add:$dst GPR:$lhs, GPR:$rhs)` 在内存里是：

```cpp
class DagInit final
    : public TypedInit,
      public FoldingSetNode,
      private TrailingObjects<DagInit, const Init *, const StringInit *> {
  const Init *Val;            // 操作符（通常是个 DefInit，指向 `def add`）
  const StringInit *ValName;  // 操作符的名字标签（`$dst` 那种）
  unsigned NumArgs;
};
```

`TrailingObjects<DagInit, const Init*, const StringInit*>` 是**两个** trailing 数组：先是 `NumArgs` 个 `const Init*`（参数值），再是 `NumArgs` 个 `const StringInit*`（参数名）。这叫「多类型 trailing objects」，靠 `numTrailingObjects(OverloadToken<T>)` 区分：

```cpp
size_t numTrailingObjects(OverloadToken<const Init *>) const { return NumArgs; }
```

访问接口：

```cpp
const Init *getOperator() const;         // (add ...) 里的 add
const StringInit *getName() const;       // (add:$dst ...) 里的 $dst
unsigned getNumArgs() const;
const Init *getArg(unsigned Num) const;
const StringInit *getArgName(unsigned Num) const;
std::optional<unsigned> getArgNo(StringRef Name) const;  // 按名字找参数
```

实测一个完整例子：

```td
def op;
class C<dag d> { dag D = d; }
def X : C<(op:$res 1:$a, 2:$b)>;
def Y : C<(op 1, ?:$missing)>;
class Nested { dag N = (op (op 1, 2), 3); }
def Z : Nested;
```

```console
$ llvm-tblgen -print-records dg.td | tail -14
def X {	// C
  dag D = (op:$res 1:$a, 2:$b);
}
def Y {	// C
  dag D = (op 1, ?:$missing);
}
def Z {	// Nested
  dag N = (op (op 1, 2), 3);
}
def op {
}
```

四个观察：

1. **操作符必须是已定义的记录**。`(op ...)` 里的 `op` 先被当成一个标识符解析，然后 `lookup` 到 `def op`，得到 `DefInit`。如果没定义：

   ```console
   $ llvm-tblgen v1.td
   v1.td:5:12: error: Variable not defined: 'op'
   ```

   （这条错误来自 `ParseSimpleValue` 的 `Id` 分支，不是 DAG 专门检查。）

2. **参数名是可选的**。`X` 的 `$res`/`$a`/`$b` 都存进了 `ArgNames`；`Z` 的 `(op (op 1, 2), 3)` 两个参数都没有名字，`getArgName(0)` 返回 `nullptr`。

3. **`?` 可以带名字**：`?:$missing` 表示「这个位置有个名字但值未定」。这在 pattern 里很常见（`(store ?:$addr, ...)`）。

4. **DAG 可以嵌套，但操作符不能是 DAG**。`(op (op 1, 2), 3)` 里内层 `(op 1, 2)` 是**参数**，不是操作符。语法上 `((a b) c)` 会失败，因为第一个位置必须是标识符。

### 用 bang operator 拆 DAG

Ch 2 提过 `!getdagop` 系列。实测：

```td
def op;
class C<dag d> { string N = !getdagopname(d); int Cnt = !size(d); string A0 = !getdagname(d, 0); }
def X : C<(op:$r 1:$a, 2:$b)>;
```

```console
$ llvm-tblgen -print-records dg3.td | tail -6
def X {	// C
  string N = "r";
  int Cnt = 2;
  string A0 = "a";
}
```

`!size(d)` 返回**参数个数**（不含操作符）；`!getdagname(d, 0)` 返回第 0 个参数的名字。这些操作符让 backend 之外的 `.td` 也能分析 DAG 结构——AArch64 的 `AArch64InstrInfo.td` 里大量使用。

## 6.3 `DefInit`：指针就是身份

```cpp
class DefInit final : public TypedInit {
  const Record *Def;
public:
  const Record *getDef() const { return Def; }
  const Init *getBit(unsigned Bit) const override {
    llvm_unreachable("Illegal bit reference off def");
  }
};
```

三个要点：

1. **`DefInit` 不是池化的**（没有 `FoldingSetNode`）。但它指向的 `Record` 在 `RecordKeeper` 里唯一，所以「同一个 def 的两个引用」是两个 `DefInit` 对象但同一个 `getDef()`。**判断两个引用是否指向同一个 def，要比较 `getDef()`，不是比较 `DefInit*`。**
2. **`getBit()` 是 `llvm_unreachable`**：你不能对 `def` 做位操作。这个设计把错误从「运行时返回垃圾」变成「立刻崩」，是 TableGen 里常见的防御。
3. **`DefInit` 由 `Record` 持有**：`Record::getDefInit()` 返回缓存的 `DefInit`，`mutable CorrespondingDefInit` 字段。所以「一个 Record 只有一个 DefInit」实际上不成立——见 Ch 4 的 `getSuperClasses` 讨论，多父类场景下会有多个实例（`VarDefInit::instantiate` 也产生）。**依赖 `DefInit` 指针相等是危险的**。

### 字段引用：`FieldInit`

```td
class Inner { int V = 5; string Name = "inner"; }
def I : Inner;
class Outer { Inner Ref = I; int FromRef = I.V; string Nm = I.Name; string N2 = Ref.Name; }
def O : Outer;
```

```console
$ llvm-tblgen -print-records fi2.td | tail -9
def O {	// Outer
  Inner Ref = I;
  int FromRef = 5;
  string Nm = "inner";
  string N2 = "inner";
}
```

`I.V` 被折叠成 `5`，`Ref.Name` 也被折叠成 `"inner"`。但**折叠前**它们是 `FieldInit(Rec=DefInit(I), FieldName="V")`。`FieldInit` 的构造里有一句：

```cpp
FieldInit(const Init *R, const StringInit *FN)
    : TypedInit(IK_FieldInit, R->getFieldType(FN)), Rec(R), FieldName(FN) {
#ifndef NDEBUG
  if (!getType()) {
    llvm::errs() << "In Record = " << Rec->getAsString()
                 << ", got FieldName = " << *FieldName
                 << " with non-record type!\n";
    llvm_unreachable("FieldInit with non-record type!");
  }
#endif
}
```

**类型在构造时就查**（`getFieldType`），失败时在 debug 构建里 `llvm_unreachable`。但实测发现 release 下会报更友好的错：

```console
$ llvm-tblgen fi.td
fi.td:3:63: error: Cannot access field 'Name' of value 'I'
```

因为 `Inner` 没有 `Name` 字段，parser 在 `ParseSimpleValue` 的 `XGetDagOp`/`FieldInit` 构造路径里先检查了。**「assert 兜底 + parser 友好错误」的双层模式**在本课反复出现。

## 6.4 `VarInit`：为什么需要「未解析的引用」

```cpp
class VarInit final : public TypedInit {
  const Init *VarName;
  static const VarInit *get(StringRef VN, const RecTy *T);
  const Init *resolveReferences(Resolver &R) const override;
};
```

`VarInit` 的注释说得很清楚：

```
/// This method is used by classes that refer to other
/// variables which may not be defined at the time they expression is formed.
/// If a value is set for the variable later, this method will be called on
/// users of the value to allow the value to propagate out.
```

**先澄清一个常见误解**：这句话说的是「模板参数在 class 定义时没有值」，**不是**「TableGen 支持前向引用」。实测表明 TableGen 要求**先定义后引用**：

```console
$ cat fw2.td
def A {
  int X = Y;     // Y 在后面声明
  int Y = 5;
}
$ llvm-tblgen fw2.td
fw2.td:2:11: error: Variable not defined: 'Y'
```

```console
$ cat fw3.td
def A {
  int Y = 5;
  int X = Y;     // 先定义后引用，可以
}
$ llvm-tblgen -print-records fw3.td | tail -3
def A {
  int Y = 5;
  int X = 5;
}
```

记录级也一样：

```console
$ cat fw5.td
class Ref { int V = 1; }
def X { Ref R = Y; }
def Y : Ref;
$ llvm-tblgen fw5.td
fw5.td:2:17: error: Variable not defined: 'Y'
```

类的引用同样不行：

```console
$ cat fw4.td
class A { B b = ?; }
class B { int V = 1; }
$ llvm-tblgen fw4.td
fw4.td:1:11: error: Couldn't find class 'B'
fw4.td:1:13: error: unknown class name
```

**所以 `VarInit` 的「未解析」只出现在三种有明确边界的场景**：

### 场景一：模板参数

```td
class Pair<int a, int b> { int Sum = !add(a, b); }
```

`Sum` 的初值是 `BinOpInit(ADD, VarInit("a"), VarInit("b"))`——**`a` 和 `b` 在 class 定义时没有值**。它们要等到 `def P : Pair<3,4>` 时，由 `resolveArguments` 把 `VarInit("a")` 替换成 `IntInit(3)`。

### 场景二：字段互相引用

```td
class C {
  int A = 1;
  int B = A;        // 这里 A 是 VarInit 还是 IntInit？
}
```

答案是 `VarInit("A")`。因为**声明顺序**：`B` 的初值在解析时，`A` 已经存进 `Values` 了，但 `ParseSimpleValue` 的 `Id` 分支不会立刻 fold——它返回 `VarInit`，等整个 body 解析完再由 `resolveReferences` 统一替换。

### 场景三：foreach / multiclass 的延迟展开

这是 `isFinal()` 真正起作用的地方。`foreach` 与 `defm` 的主体在解析时被存成 `ForeachLoop`/`MultiClass::Entries`，**等外层参数就位后才展开**：

```cpp
// TGParser.cpp:432（addEntry 里遇到 loop）
return resolve(*E.Loop, Stack, CurMultiClass == nullptr, ...);
//                                    ^^^^^^^^^^^^^^^^^^^^^^^^^
//                                    只有不在 multiclass 里才 Final
```

```cpp
// TGParser.cpp:4612（ParseDefm 里）
if (resolve(MC->Entries, Substs, !CurMultiClass && Loops.empty(), ...))
```

所以「嵌套在 multiclass 里的 foreach」会经历多次非 Final 的 resolve，直到最外层 `defm` 才 Final。**这是 `isFinal()` 的唯一真实来源**——不是「前向引用」，而是「模板没实例化完不准报错」。

`defvar` 也在这一类：它在 `ParseDefvar` 时就求值并放进 `ExtraGlobals`，后续引用是普通的名字查找，不涉及延迟。

### `VarBitInit`：`$x{3}` 是独立节点

```cpp
class VarBitInit final : public TypedInit {
  const TypedInit *TI;
  unsigned Bit;
  VarBitInit(const TypedInit *T, unsigned B)
      : TypedInit(IK_VarBitInit, BitRecTy::get(...)), TI(T), Bit(B) {
    assert(T->getType() &&
           (isa<IntRecTy>(T->getType()) ||
            (isa<BitsRecTy>(T->getType()) &&
             cast<BitsRecTy>(T->getType())->getNumBits() > B)) &&
           "Illegal VarBitInit expression!");
  }
};
```

注意 assert 的条件：**int 可以任意取位，bits<n> 必须 `n > Bit`**。这解释了为什么 `bits<4> B = 0xAB; let B{7} = 0;` 会失败——它在 `VarBitInit` 构造时就炸了（如果走这条路）。实际走的是 `ParseValue` 的 `l_brace` 后缀，报的是 `Invalid bit range for value`（Ch 3 见过）。

## 6.5 `resolveReferences`：TableGen 的「链接器」

所有指涉外部名字的节点都重写了这个方法：

```cpp
virtual const Init *resolveReferences(Resolver &R) const;
```

`Resolver` 基类（`Record.h:2294`）只有两个状态：`CurRec`（当前记录）与 `IsFinal`。五个派生类：

| Resolver | 用途 |
| --- | --- |
| `MapResolver` | 名字 → `Init*` 的显式映射（模板参数替换用它） |
| `RecordResolver` | 从 `Record` 的字段查值（`record.checkRecordAssertions()` 用它） |
| `ShadowResolver` | 覆盖某个名字的映射，包在别的 resolver 外 |
| `TrackUnresolvedResolver` | 记录「哪些名字没解析」（用于诊断） |
| `HasReferenceResolver` | 只回答「有没有引用某名字」（`Record::isValueUsed` 用它） |

`IsFinal` 由调用方通过 `setFinal()` 设置，派生类不自己决定。

**最关键的概念是 `isFinal()`**：

- `final == false`：尽可能替换，但保留未解析的 `VarInit` 原样返回（可以下次再试）。
- `final == true`：**必须**全部解析，仍有未解析的 `VarInit` 就报错。

这就是为什么某些错误只在「最后一遍」才出现：

```console
$ llvm-tblgen v1.td
v1.td:5:12: error: Variable not defined: 'op'
```

`VarInit("op")` 在前几遍 resolve 时被原样返回（因为 `op` 还没定义），到最后一遍 `isFinal() == true` 才报错。这个「多遍扫描」的循环在 `TGParser::resolve`（462 行）里，Ch 7 会完整拆开。

### `AnonymousNameInit` 的特殊 resolve

```cpp
const Init *AnonymousNameInit::resolveReferences(Resolver &R) const {
  auto *Old = this;
  auto *New = R.resolve(Old);
  New = New ? New : Old;
  if (R.isFinal())
    if (const auto *Anonymous = dyn_cast<AnonymousNameInit>(New))
      return Anonymous->getNameInit();     // 转成真正的 StringInit
  return New;
}
```

匿名 def 的名字在 resolve 前是 `AnonymousNameInit`，`isFinal()` 时才转成 `"anonymous_N"` 字符串。这解释了为什么 `-print-records` 里看不到 `anonymous_` 名字（它在 resolve 后已经变成普通 `StringInit`）。

## 6.6 本章检查清单

- [ ] 画出 `DagInit` 的内存布局（两个 trailing 数组 + 三个字段）
- [ ] 说出 `getArgName(0)` 在没有名字时返回什么
- [ ] 解释为什么 `DefInit` 指针相等不可靠，应该比较什么
- [ ] 说出 `VarInit` 存在的三个场景
- [ ] 解释 `VarBitInit` 的 assert 条件，以及 int 与 bits 的差异
- [ ] 说出 `isFinal()` 的两个含义，以及它只与什么场景相关（不是前向引用）
- [ ] 解释 `AnonymousNameInit` 为什么在 `isFinal()` 时才改名

## 6.7 踩雷记录

**错误直觉 1：`dag` 的第一个元素可以是任意表达式。**
正确认识：操作符必须解析成一个记录（`DefInit`）。`((a b) c)` 这种会报 `Variable not defined`，因为第一个位置被当成标识符解析。

**错误直觉 2：`DefInit*` 相等意味着同一个 def。**
正确认识：`DefInit` 不是池化的，同一个 `Record` 可能有多个 `DefInit` 实例（`Record::getDefInit()` 缓存一个，`VarDefInit::instantiate` 又产生一个）。**比较 `getDef()`**。

**错误直觉 3：TableGen 支持前向引用。**
正确认识：**不支持**。字段、记录、类的引用都必须「先定义后引用」。`VarInit` 的「未解析」只出现在模板参数、multiclass/foreach 的延迟展开这些有明确边界的场景。实测：`int X = Y; int Y = 5;` 直接报 `Variable not defined: 'Y'`。

**错误直觉 4：`!size(dag)` 算上操作符。**
正确认识：只算参数。`(op 1, 2)` 的 `!size` 是 2。

**错误直觉 5：`VarInit` 只是「还没算的表达式」的语法糖。**
正确认识：它是**一等节点**，有自己的 `getType()`、能被 `isa<>` 匹配、能出现在任何 `Init*` 的位置。backend 可能拿到未解析的 `VarInit`（如果 `isFinal()` 没跑完），必须处理。

**错误直觉 6：`AnonymousNameInit` 就是个字符串前缀。**
正确认识：它是独立类型，`getAsString()` 返回 `"anonymous_N"`，但 `getNameInit()` 在 final resolve 前返回自己。`getKind()` 也不同于 `StringInit`。

## 6.8 延伸阅读

- **`llvm/include/llvm/TableGen/Record.h`** 的 `DagInit`（1467）
  - 完整体现在 `TrailingObjects` 的双数组用法。重点看 `getArgNo` 的实现（线性查找）。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `DefInit`（1336）/ `VarDefInit`（1367）/ `FieldInit`（1424）
  - 三个「指向记录」的节点。`VarDefInit` 是匿名 class 实例化，Ch 8 会用到。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `VarInit`（1262）/ `VarBitInit`（1299）
  - 前向引用的核心。注意 `VarInit::get` 有两个重载（`StringRef` 与 `Init*`）。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `Resolver` 家族（2294–2505）
  - 五个 Resolver 的完整定义。`Resolvers` 的 virtual 设计是理解 `resolveReferences` 的钥匙。
- **`llvm/lib/TableGen/Record.cpp`** 的 `DagInit::resolveReferences`
  - DAG 的 resolve 是「先解析操作符，再逐个解析参数」，看它怎么处理 `ArgNames` 的同步。

## 下一章

Ch 7 进入求值：`fold()` 与 `resolveReferences()` 分别在什么时候被调用，为什么有些表达式「永远不 fold」，以及怎么用 `dump` + `assert` 把求值时机变成可观测的。

[Ch 7 求值：fold()、resolveReferences() 与惰性求值时机](./07-folding-and-resolution.md)
