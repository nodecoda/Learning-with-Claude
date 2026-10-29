# Ch 3 语法层：TGParser 的语句分派与表达式解析

> **本章目标**：把 `TGParser.cpp` 的 4846 行压缩成「四类函数」的心智模型；能说出 12 种顶层语句各自的 parse 函数与副作用；理解 `ParseValue → ParseSimpleValue → ParseOperation` 的递归结构，以及为什么 `let` 是唯一会改写既有记录字段的语句。

> **环境**：LLVM `24.0.0git`，Release（无 assertions）。

---

## 3.1 4846 行其实是四类函数

第一次打开 `TGParser.cpp` 会被 56 个成员函数淹没。但按职责归类，只有四类：

```
① 语句解析（12 个顶层 + body 内 4 个）
   ParseObject → ParseDef / ParseDefm / ParseClass / ParseMultiClass /
                 ParseDefset / ParseDeftype / ParseDefvar / ParseForeach /
                 ParseIf / ParseTopLevelLet / ParseAssert / ParseDump
   内部：ParseBody → ParseBodyItem → ParseDeclaration / ParseLet

② 表达式解析（递归下降）
   ParseValue
     ├─ ParseSimpleValue        （字面量、标识符、DAG、list、class 实例）
     └─ suffix 循环              （位域 {hi...lo} 与列表下标 [i]）
          └─ ParseOperation     （所有 !xxx）
               ├─ ParseOperationSubstr / ParseOperationFind
               ├─ ParseOperationListComprehension
               └─ ParseOperationCond / ParseOperationSwitch

③ 记录构造与求值（语义动作）
   AddValue / SetValue / AddParentClass / addEntry / addDefOne /
   resolve / resolveArguments / ApplyLetStack / CheckTemplateArgValues

④ 工具
   consume / TokError / Error / ParseObjectName / ParseClassID /
   ParseTemplateArgValueList / ParseRangePiece
```

**读法建议**：先读 ①（结构），再读 ③（语义），最后才读 ②（细节）。②最啰嗦，但它只是把 token 变成 `Init` 节点，Ch 5 会讲那些节点是什么。

## 3.2 顶层分派：一个 `switch` 十二个 `case`

```cpp
bool TGParser::ParseObject() {          // TGParser.cpp:4682
  switch (Lex.getCode()) {
  default:
    return TokError("Expected assert, class, def, defm, defset, dump, "
                    "foreach, if, or let");
  case tgtok::Assert:     return ParseAssert();
  case tgtok::Def:        return ParseDef();
  case tgtok::Defm:       return ParseDefm();
  case tgtok::Deftype:    return ParseDeftype();
  case tgtok::Defvar:     return ParseDefvar();
  case tgtok::Dump:       return ParseDump();
  case tgtok::Foreach:    return ParseForeach();
  case tgtok::If:         return ParseIf();
  case tgtok::Let:        return ParseTopLevelLet();
  case tgtok::Defset:
    if (CurMultiClass) return TokError("defset is not allowed inside multiclass");
    return ParseDefset();
  case tgtok::Class:
    if (CurMultiClass) return TokError("class is not allowed inside multiclass");
    if (!Loops.empty()) return TokError("class is not allowed inside foreach loop");
    return ParseClass();
  case tgtok::MultiClass:
    if (!Loops.empty()) return TokError("multiclass is not allowed inside foreach loop");
    return ParseMultiClass();
  }
}
```

三个 `if` 揭示了 TableGen 的一条上下文规则：**`class`/`multiclass`/`defset` 不能嵌套在 multiclass 或 foreach 里**。原因是它们的语义是「定义一个可复用的模板」，而模板内部再定义模板会让展开算法失去终止性保证。实测：

```console
$ printf 'multiclass M {\n  class C {}\n}\n' > ctx.td && llvm-tblgen ctx.td
ctx.td:2:3: error: expected 'assert', 'def', 'defm', 'defvar', 'dump', 'foreach', 'if', or 'let' in multiclass body
  class C {}
  ^
```

注意 `ParseObject` 里那个 `if (CurMultiClass) return TokError("class is not allowed inside multiclass")` 在**顶层**才触发；一旦进入 multiclass 的 body，body parser 用的是另一套 item 列表（只有 `assert`/`def`/`defm`/`defvar`/`dump`/`foreach`/`if`/`let`），所以报的是「期望的 item 列表」。**同一类错误在两层有两条不同的信息**——读诊断时要先判断自己在哪一层。

注意 `default` 的错误信息**列出全部合法项**，包括用户刚写错的那一类。这是很好的诊断实践（Ch 12 会把它和 `PrintFatalError` 对比）。

### 每条语句的副作用

| 语句 | 产生的 `RecordKeeper` 变化 | 立即求值？ |
| --- | --- | --- |
| `class` | 新增 `RK_Class` 记录 | 否（模板参数未绑定） |
| `def` | 新增 `RK_Def` 记录 + 复制父类字段 | 是（字段被 fold） |
| `multiclass` | 新增 `MultiClass`（内含 `RK_MultiClass` 占位记录） | 否 |
| `defm` | 展开 multiclass，产生多个 `RK_Def` | 是 |
| `defset` | 新增一个「定义集合」对象（不是 Record） | — |
| `deftype` | 新增一个类型别名，登记在 `RecordKeeper` | — |
| `defvar` | 新增一个全局变量（`ExtraGlobals`） | 是 |
| `foreach` | 新增 `ForeachLoop`，延迟展开 | 否 |
| `if` | 语法糖，等价于 0/1 次的 `foreach` | 否 |
| `let` | 修改已有字段 | 是 |
| `assert` | 立即检查，失败即错 | 是 |
| `dump` | 立即打印到 stderr | 是 |

「立即求值」这一列是本章最重要的表：**它是 Ch 7（fold 时机）和 Ch 8（multiclass 展开）的入口**。

## 3.3 `def`：最简单也最能说明一切

`ParseDef` 的骨架（实际在 3964 行）：

```
def 名字 [template-args] [':' 父类 [',' 父类]*] [body]
```

关键在 `addDefOne`（580 行）——它做三件事：

1. **按父类顺序复制字段**：从 `DirectSuperClasses` 链上把每个 `RecordVal` 复制进自己，同时用当前记录替换变量引用。
2. **检查重复定义**：同名 `def` 直接报错。
3. **fold 所有字段**：把 `VarInit` 解析成具体值，把 `BinOpInit` 折叠。

实测一个最容易误解的语义：

```td
class Base<int n> { int N = n; }
class Mid<int n> : Base<n> { }
def C : Mid<7>;
```

```console
$ llvm-tblgen -print-records cr.td | tail -8
class Mid<int Mid:n = ?> {	// Base
  int N = Mid:n;
}
------------- Defs -----------------
def C {	// Base Mid
  int N = 7;
}
```

注意 `// Base Mid`：`DirectSuperClasses` 同时包含 `Base` 和 `Mid`，**不是只有 `Mid`**。因为 `AddParentClass` 会把父类的父类也展开进来（递归地），并保持顺序（父类在上）。这解释了一个常见困惑：为什么 `-print-records` 里 `def` 的父类列表比源码里写的多。

再看默认值与空参数列表：

```td
class Base<int n = 5> { int N = n; }
def A : Base<>;     // 显式空列表，用默认值
def B : Base;       // 完全省略
```

两者都得到 `N = 5`（实测一致）。但这个「省略」是有代价的：

```console
$ printf 'class B<int n, int m> { int N = n; }\ndef A : B<1>;\n' > ta.td && llvm-tblgen ta.td
ta.td:1:20: warning: unused template argument: B:m
class B<int n, int m> { int N = n; }
                   ^
ta.td:2:9: error: value not specified for template argument 'B:m'
```

**先 warning（未使用），再 error（未指定）**，两条信息都指向不同位置。这就是 `CheckTemplateArgValues` 与 `ParseTemplateArgValueList` 的分工：前者检查值与类型，后者检查个数与命名。

命名参数是支持的（实测）：

```td
class B<int n, int m = 2> { int N = n; int M = m; }
def A : B<n=1>;        // N=1, M=2
def B2 : B<1, m=9>;    // N=1, M=9
```

注意 `def B2` 的名字和 `class B` 撞了但没报错——**class 与 def 是两个命名空间**（`RecordKeeper::Classes` vs `Defs`）。

## 3.4 `body`：四种 item 与 `let` 的特殊地位

```cpp
bool TGParser::ParseBodyItem(Record *CurRec) {   // 3797
  if (Lex.getCode() == tgtok::Assert) return ParseAssert(CurRec);
  if (Lex.getCode() == tgtok::Defvar) return ParseDefvar(CurRec);
  if (Lex.getCode() == tgtok::Dump)   return ParseDump(CurRec);

  if (Lex.getCode() != tgtok::Let) {
    if (ParseDeclaration(CurRec, /*ParsingTemplateArgs=*/false)) return true;
    if (!consume(tgtok::semi)) return TokError("expected ';' after declaration");
    return false;
  }

  // let [append|prepend] ID OptionalBitList '=' Value ';'
  Lex.Lex();
  ...
}
```

body 里只有四种东西：`assert`、`defvar`、`dump`、以及「声明/`let`」。前三种是**语句**，后一种是**字段操作**。

### `let` 是全语言唯一能改写已有值的语句

看 `SetValue`（236 行）里两个细节：

**细节一：`let X = X` 被显式禁止**

```cpp
if (BitList.empty())
  if (const auto *VI = dyn_cast<VarInit>(V))
    if (VI->getNameInit() == ValName)
      return Error(Loc, "Recursion / self-assignment forbidden");
```

```console
$ printf 'def D { int X = 1; let X = X; }\n' > sa.td && llvm-tblgen sa.td
sa.td:1:24: error: Recursion / self-assignment forbidden
```

注释说明理由：「会在解析机制里造成无限循环」。这是**防御性诊断**的典型例子：不是因为语言不允许，而是因为实现会挂。

**细节二：`let append` 是按类型的二元拼接**

```cpp
BinOpInit::BinaryOp ConcatOp;
if (isa<ListRecTy>(FieldType))        ConcatOp = BinOpInit::LISTCONCAT;
else if (isa<StringRecTy>(FieldType)) ConcatOp = BinOpInit::STRCONCAT;
else if (isa<DagRecTy>(FieldType))    ConcatOp = BinOpInit::CONCAT;
else
  return Error(Loc, Twine("Cannot ") + (IsAppendMode ? "append to" : "prepend to") +
                        " field '" + ... + "' of type '" + ... +
                        "' (expected list, string, code, or dag)");
```

实测三种类型：

```console
$ cat l2.td
class C { list<int> L = [1]; }
def D : C { let append L = [2, 3]; }
def E : C { let prepend L = [0]; }
$ llvm-tblgen -print-records l2.td | tail -8
def D {	// C
  list<int> L = [1, 2, 3];
}
def E {	// C
  list<int> L = [0, 1];
}
```

```console
$ cat aps.td
class C { string S = "a"; }
def D : C { let append S = "b"; }
$ llvm-tblgen -print-records aps.td | tail -3
def D {	// C
  string S = "ab";
}
```

不支持的类型：

```console
$ printf 'class C { int X = 1; }\ndef D : C { let append X = 2; }\n' > ap.td && llvm-tblgen ap.td
ap.td:2:24: error: Cannot append to field 'X' of type 'int' (expected list, string, code, or dag)
```

注意错误信息里说「code」也可以，但 `code` 的类型是 `StringRecTy`（Ch 5 会证明），所以走的是 `STRCONCAT` 分支。

### `let` 的位域形式

```td
class C { bits<8> B = 0xFF; }
def D : C { let B{3...0} = 0b0000; }
```

```console
$ llvm-tblgen -print-records l3.td | tail -3
def D {	// C
  bits<8> B = { 1, 1, 1, 1, 0, 0, 0, 0 };
}
```

实现上，`BitList` 传给 `SetValue` 后走「只改部分位」的分支（Ch 10 会讲 `setBits` 的细节）。注意 `BitList` 在 `ParseBodyItem` 里被 `std::reverse` 过，因为源码写 `{3...0}` 是从低到高，而内部表示是 MSB-first。

## 3.5 表达式：`ParseValue` 的两层结构

```cpp
const Init *TGParser::ParseValue(Record *CurRec, const RecTy *ItemType, IDParseMode Mode) {
  SMLoc LHSLoc = Lex.getLoc();
  const Init *Result = ParseSimpleValue(CurRec, ItemType, Mode);
  if (!Result) return nullptr;

  // Parse the suffixes now if present.
  while (true) {
    switch (Lex.getCode()) {
    default: return Result;
    case tgtok::l_brace: {          // 位域 {hi...lo}
      ...
      Result = Result->convertInitializerBitRange(Ranges);
      if (!Result) { Error(CurlyLoc, "Invalid bit range for value"); return nullptr; }
      ...
    }
    case tgtok::l_square: {         // 下标 [i] 或切片 [a...b]
      ...
      if (isa<ListRecTy>(RHS->getType()))
        Result = BinOpInit::get(BinOpInit::LISTSLICE, LHS, RHS, LHSTy)->Fold(CurRec);
      else
        Result = BinOpInit::get(BinOpInit::LISTELEM, LHS, RHS, LHSTy->getElementType())->Fold(CurRec);
      break;
    }
    }
  }
}
```

两层结构：

```
ParseValue = ParseSimpleValue + 后缀循环
              ↑                    ↑
        一个原子值              {bits} 或 [list]
```

**后缀可以串联**（`B{7...4}{3...0}` 这种），但实测会失败：

```console
$ printf 'class T { bits<16> B = 0xBEEF; bits<4> Q = B{3...0}{7...4}; }\ndef D : T;\n' > s4.td
$ llvm-tblgen s4.td
s4.td:1:52: error: Invalid bit range for value
```

因为第一次 `B{3...0}` 得到 `bits<4>`，再对它做 `{7...4}` 越界。**语法允许串联，语义不允许越界**——这是两个不同层次的检查。

`ParseSimpleValue`（2952 行）是一个 40 个 `case` 的 `switch`，开头就分流：

```cpp
if (tgtok::isBangOperator(Code))
  return ParseOperation(CurRec, ItemType);

switch (Code) {
case tgtok::TrueVal:  R = IntInit::get(Records, 1); Lex.Lex(); break;
case tgtok::FalseVal: R = IntInit::get(Records, 0); Lex.Lex(); break;
case tgtok::IntVal:   R = IntInit::get(Records, Lex.getCurIntVal()); Lex.Lex(); break;
case tgtok::BinaryIntVal: {
  auto BinaryVal = Lex.getCurBinaryIntVal();          // pair<value, bitwidth>
  SmallVector<Init *, 16> Bits(BinaryVal.second);
  for (unsigned i = 0; i != BinaryVal.second; ++i)
    Bits[i] = BitInit::get(Records, BinaryVal.first & (1LL << i));
  R = BitsInit::get(Records, Bits);                   // ← 变成 BitsInit
  Lex.Lex();
  break;
}
case tgtok::StrVal: {
  std::string Val = Lex.getCurStrVal();
  Lex.Lex();
  while (Lex.getCode() == tgtok::StrVal) { Val += Lex.getCurStrVal(); Lex.Lex(); }
  R = StringInit::get(Records, Val);
  break;
}
case tgtok::CodeFragment:
  R = StringInit::get(Records, Lex.getCurStrVal(), StringInit::SF_Code);
  Lex.Lex();
  break;
```

四个必须记住的映射：

| 源码 | 变成什么 `Init` |
| --- | --- |
| `true` / `false` | `IntInit(1)` / `IntInit(0)`，**不是** `BitInit` |
| `42` | `IntInit(42)` |
| `0b1010` | `BitsInit([0,1,0,1])`（LSB-first 构造，见代码 `1LL << i`） |
| `"a" "b"`（相邻字符串） | 一个 `StringInit("ab")` |
| `[{ ... }]` | `StringInit(..., SF_Code)` |

**`true` 是 int 不是 bit** 这一点很重要：`bit B = true;` 能过，是因为后续有隐式转换（Ch 9）。

**相邻字符串自动拼接**是个隐藏语法：

```console
$ printf 'def D { string S = "abc" "def"; }\n' > adj.td && llvm-tblgen -print-records adj.td
def D {
  string S = "abcdef";
}
```

**隐式 cast 的实测**：

```console
$ printf 'def D { bits<4> B = 3; }\n' > bc.td && llvm-tblgen -print-records bc.td
def D {
  bits<4> B = { 0, 0, 1, 1 };
}
```

注意位数是 **MSB-first** 打印，`3` 的二进制 `0011` 在 `bits<4>` 里显示成 `{0,0,1,1}`。

## 3.6 `ParseOperation`：每个 bang operator 的三种形态

`ParseOperation`（1220 行）先按「元数」分三组：

```cpp
case tgtok::XNOT: case tgtok::XToLower: case tgtok::XToUpper:
case tgtok::XListFlatten: case tgtok::XLOG2: case tgtok::XHead: case tgtok::XTail:
case tgtok::XSize: case tgtok::XEmpty: case tgtok::XCast: case tgtok::XRepr:
case tgtok::XGetDagOp: case tgtok::XGetDagOpName: case tgtok::XInitialized: {
  // Value ::= !unop '(' Value ')'
  UnOpInit::UnaryOp Code;
  const RecTy *Type = nullptr;
  switch (Lex.getCode()) {
  case tgtok::XCast:
    Lex.Lex(); Code = UnOpInit::CAST;
    Type = ParseOperatorType();          // !cast<type>(...)
    ...
  case tgtok::XListFlatten:
    Lex.Lex(); Code = UnOpInit::LISTFLATTEN;
    Type = IntRecTy::get(Records); // Bogus type used here.
    ...
```

注意那行注释 **`// Bogus type used here.`**——`!listflatten` 的结果类型取决于输入（`list<list<T>>` → `list<T>`），但 `UnOpInit` 的构造函数需要一个 `RecTy`，这里塞了个假的 `int` 类型，实际类型在 fold 时才确定。**这是「类型系统在表达式层是不完备的」的直接证据**，Ch 9 会展开。

三组分别是：

| 组 | 形态 | 例子 |
| --- | --- | --- |
| 一元 | `!op(x)` | `!not`, `!size`, `!cast<T>`, `!repr` |
| 二元 | `!op(a, b)` | `!add`, `!eq`, `!subst`, `!find` |
| 特殊 | `!if`, `!cond`, `!switch`, `!foreach`, `!filter`, `!foldl` | 有自己的语法 |

`!if` 是**三元但不是函数式**：

```td
int X = !if(cond, then, else);
```

而 `!cond` 是 `cond1 : val1, cond2 : val2, ...` 的冒号语法，`!switch` 是 `value, case1: v1, ..., default: d`。这三个在 `ParseOperationCond`/`ParseOperationSwitch` 里单独实现，因为它们需要**短路求值**（Ch 7 会测量）。

## 3.7 `ParseClassReference`：模板参数的三道关卡

```cpp
ClassReference TGParser::ParseClassReference(Record *CurRec, bool isDefm) {
  ClassReference Result;
  Result.RefRange.Start = Lex.getLoc();

  Result.Rec = isDefm ? (MultiClass *MC = ParseMultiClassID()) ? &MC->Rec : nullptr
                      : ParseClassID();
  if (!Result.Rec) return Result;

  if (!consume(tgtok::less)) { Result.RefRange.End = Lex.getLoc(); return Result; }

  SmallVector<SMLoc> ArgLocs;
  if (ParseTemplateArgValueList(Result.TemplateArgs, ArgLocs, CurRec, Result.Rec)) {
    Result.Rec = nullptr;                       // 关卡一：语法
    return Result;
  }
  if (CheckTemplateArgValues(Result.TemplateArgs, ArgLocs, Result.Rec)) {
    Result.Rec = nullptr;                       // 关卡二/三：个数 + 类型
    return Result;
  }
  Result.RefRange.End = Lex.getLoc();
  return Result;
}
```

三道关卡分别对应：

1. **`ParseTemplateArgValueList`**：解析 `a, b, c` 或 `name=value`，检查**个数**与**命名是否合法**。
2. **`CheckTemplateArgValues`**：检查**类型**，必要时插入 cast。
3. **`Record::addTemplateArg`**（在 `ParseDeclaration` 里）：检查**重复声明**。

`CheckTemplateArgValues` 里有个值得注意的行为——它会**自动插入 cast**：

```cpp
if (const auto *ArgValue = dyn_cast<TypedInit>(Value->getValue())) {
  auto *CastValue = ArgValue->getCastTo(ArgType);
  if (CastValue) { Value = Value->cloneWithValue(CastValue); }
  else { /* 报错 */ }
}
```

所以 `bits<4> B = 3;` 能过，因为 `IntInit(3)` 被转成 `BitsInit`。这就是 Ch 9 的「隐式转换」在 parser 里的落点。

## 3.8 本章检查清单

- [ ] 不看源码，写出 12 种顶层语句的名字
- [ ] 说出 `class`/`multiclass`/`defset` 各自「不能出现在哪里」，并说出原因
- [ ] 解释 `def C : Mid<7>` 为什么 `DirectSuperClasses` 是 `[Base, Mid]`
- [ ] 说出 `let` 支持的三类拼接操作及对应的 `BinOpInit::BinaryOp`
- [ ] 解释 `let X = X` 为什么被禁止（不是语义问题，是实现问题）
- [ ] 说出 `true` 变成什么 `Init`，`0b1010` 变成什么 `Init`
- [ ] 解释 `!listflatten` 的 `// Bogus type used here.` 说明了什么
- [ ] 说出 `ParseClassReference` 的三道关卡各检查什么

## 3.9 踩雷记录

**错误直觉 1：`def` 只继承直接父类。**
正确认识：`DirectSuperClasses` 是**递归展开后的列表**，包含所有祖先，顺序是「近的在前」。`-print-records` 的 `// Base Mid` 就是这个列表。

**错误直觉 2：`class B` 和 `def B` 会冲突。**
正确认识：class 与 def 是两个命名空间。`def B2 : B<1, m=9>;` 里 `B2` 是 def 名、`B` 是 class 名，互不干扰。

**错误直觉 3：`B{3...0}{7...4}` 会做两次切片。**
正确认识：语法上允许串联，但第二次切片作用在 `bits<4>` 上，`{7...4}` 越界直接报错。**语法合法 ≠ 语义合法**。

**错误直觉 4：`let append` 对任意类型都行。**
正确认识：只支持 list/string/code/dag（后两者都是 string 类型）。int 会报 `Cannot append to field 'X' of type 'int'`。

**错误直觉 5：`true` 是 `BitInit(1)`。**
正确认识：`IntInit(1)`。能赋给 `bit` 字段是靠后续的隐式 cast，不是 lexer/parser 的功劳。

**错误直觉 6：`!listflatten` 的返回类型是 `int`。**
正确认识：那行 `Type = IntRecTy::get(Records); // Bogus type used here.` 是占位符，真实类型在 fold 时算。别把注释当规范。

## 3.10 延伸阅读

- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseObject`（4682）与 `ParseFile`（4732）
  - 结构入口，20 行读完。对照本章 3.2 的表。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseSimpleValue`（2952）
  - 40 个 `case` 的 `switch`，是「源码文本 → `Init` 节点」的完整对照表。建议逐 `case` 读完，对照本章 3.5 的表。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `SetValue`（236）
  - `let` 的全部语义：append/prepend、位域、自引用检测、类型转换。这是 TableGen 里最「像编译器」的一段代码。
- **`llvm/lib/TableGen/TGParser.h`**（347 行）
  - 先看 `RecordsEntry`、`ForeachLoop`、`MultiClass` 三个结构体，再看 `TGParser` 的成员。比读 `.cpp` 高效得多。
- **`llvm/docs/TableGen/ProgRef.md`** 的 "Statements" 章节（约 600 行）
  - 官方语法参考。读某一段不确定时，对照 `ParseXxx` 的实现——文档和代码有时会不一致，代码为准。

## 下一章

Part 0 到此结束。练习 A 会让你给 Ch 2 的那个 `!sq` 补上 parser 与 `Init` 支持，走完「词法 → 语法 → 求值」的最小链路。之后 Part 1 进入数据模型：`Record` 里到底存了什么。

[练习 A：给 TableGen 加一个带精确诊断的语法错误](./practice-a-better-diagnostic.md)
