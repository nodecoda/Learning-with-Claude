# Ch 11 `assert`、`dump`、`!instances` 与运行期求值

> **本章目标**：把 `assert`/`dump` 当作「`.td` 的单元测试」来用；理解 `!instances` 的延迟求值条件（为什么定义顺序会改变结果）；知道这三个设施各自在哪个阶段执行，以及它们的错误长什么样。

> **环境**：LLVM `24.0.0git`。

---

## 11.1 为什么 `.td` 需要断言

AArch64 的 `.td` 有 5 万多行，全是手写或半自动生成的表。一个编码字段写错，最终产物是「汇编器接受但生成错误机器码」——比编译错误危险得多。

`assert` 让 `.td` 能自检。上游 `llvm/test/TableGen/assert.td` 就是 240 行的断言测试集，覆盖了「顶层 / 记录内 / 类内 / multiclass 内 / 匿名实例」五种位置。本章把这五种拆开。

## 11.2 `assert`：三处位置，三种时机

### 位置一：顶层（解析时立即检查）

```td
defvar Name = "Grace";
assert !ge(!size(Name), 20), "name is too short: " # Name;
```

```console
$ llvm-tblgen as1.td
as1.td:2:10: error: assertion failed: name is too short: Grace
assert !ge(!size(Name), 20), "name is too short: " # Name;
         ^
```

注意错误位置是 `assert` 的条件表达式处（列 10），不是 `assert` 关键字。实现（`Error.cpp:169`）：

```cpp
bool llvm::CheckAssert(SMLoc Loc, const Init *Condition, const Init *Message) {
  auto *CondValue = dyn_cast_or_null<IntInit>(Condition->convertInitializerTo(
      IntRecTy::get(Condition->getRecordKeeper())));
  if (!CondValue) {
    PrintError(Loc, "assert condition must of type bit, bits, or int.");
    return true;
  }
  if (!CondValue->getValue()) {
    auto *MessageInit = dyn_cast<StringInit>(Message);
    StringRef AssertMsg = MessageInit ? MessageInit->getValue()
                                      : "(assert message is not a string)";
    PrintError(Loc, "assertion failed: " + AssertMsg);
    return true;
  }
  return false;
}
```

两个细节：

- **条件会被 cast 成 `int`**，所以 `bit`、`bits<n>`、`int` 都行，`string` 不行。
- **消息必须是 `string`**，否则打印 `(assert message is not a string)`——不会静默通过。

`Loc` 是 `assert` 语句里条件表达式的起点，所以箭头指向条件而不是关键字。

### 位置二：记录内（`addDefOne` 时检查）

```td
def A {
  int X = 1;
  assert !eq(X, 1), "X should be 1";
}
def B {
  int X = 2;
  assert !eq(X, 1), "X should be 1: " # X;
}
```

```console
$ llvm-tblgen as2.td
as2.td:7:10: error: assertion failed: X should be 1: 2
  assert !eq(X, 1), "X should be 1: " # X;
         ^
as2.td:5:5: error: assertion failed in this record
def B {
    ^
/home/ubuntu/llvm/llvm-dev/build/bin/llvm-tblgen: 2 errors.
```

**两条错误**：一条指出具体断言，一条指出「这个记录有失败断言」。后者来自 `Record::checkRecordAssertions`。

### 位置三：class 内（每次实例化检查）

```td
class C<int n> {
  assert !ge(n, 0), "n must be non-negative: " # n;
  int N = n;
}
def A : C<5>;
def B : C<-1>;
```

```console
$ llvm-tblgen as1.td
as1.td:2:10: error: assertion failed: n must be non-negative: -1
  assert !ge(n, 0), "n must be non-negative: " # n;
         ^
as1.td:6:5: error: assertion failed in this record
def B : C<-1>;
    ^
```

注意：**`A : C<5>` 通过，`B : C<-1>` 失败**，但错误位置是 class 里的 `assert` 行（`as1.td:2:10`），不是 `B` 的定义。这是对的——断言的定义在那里。

### 位置四 & 五：multiclass 内与匿名实例

`assert.td` 里有：

```td
multiclass MC1<string id, int seq> {
  assert !le(!size(id), 5), "MC1 id string is too long";
  def _mc1 { string ID = id; int Seq = seq; }
}
defm Rec40 : MC1<"ILISP", 999>;      // 通过
defm Rec41 : MC1<"ILISPX", 999>;     // 失败
```

因为 multiclass 的 assert 在**展开时**（`resolve` 的 `Assertion` 分支）检查，所以报错位置是 `assert` 行 + `defm` 行两层。

## 11.3 `dump`：不中断的 stderr 输出

`dump` 和 `assert` 的区别：**它永不失败**，只打印。

```td
foreach i = 0...2 in {
  dump "iteration " # i;
  def E#i { int V = i; }
}
```

```console
$ llvm-tblgen du2.td
du2.td:2:3: note: iteration 0
  dump "iteration " # i;
  ^
du2.td:2:3: note: iteration 1
  dump "iteration " # i;
  ^
du2.td:2:3: note: iteration 2
  dump "iteration " # i;
  ^
```

三个要点：

1. **走 `PrintNote`**，所以格式是 `file:line:col: note: ...` 加源码行。
2. **在 `foreach` 里每次迭代都打**——所以它能看到「展开过程」。
3. **不增加 `ErrorsPrinted`**，退出码仍是 0。

第一句注释来自 `dumpMessage`：

```cpp
void llvm::dumpMessage(SMLoc Loc, const Init *Message) {
  if (auto *MessageInit = dyn_cast<StringInit>(Message))
    PrintNote(Loc, MessageInit->getValue());
  else
    PrintError(Loc, "dump value is not of type string");
}
```

注意：**非 string 的 dump 消息是 error**，会 `++ErrorsPrinted`。所以 `dump 42;` 会让编译失败。

### `dump` 的最佳用途：调试模板展开

```td
multiclass M<int k> {
  dump "expanding M with k=" # k;
  def _a { int N = k; }
}
defm D : M<5>;
defm E : M<7>;
```

输出（预期，结构同上面的 foreach 例子）：

```
x.td:2:8: note: expanding M with k=5
x.td:2:8: note: expanding M with k=7
```

*本段未单独实测，但机制与 `du2.td` 相同：`resolve` 对 `E.Dump` 调 `dumpMessage`。*

## 11.4 `!instances`：按类型 + 正则收集记录

```td
class C<int v> { int V = v; }
def A1 : C<1>;
def A2 : C<2>;
def B1 : C<3>;
defvar OnlyA = !instances<C>("A.*");
def Holder { list<C> X = OnlyA; }
```

```console
$ llvm-tblgen -print-records in4.td | tail -4
def Holder {
  list<C> X = [A1, A2];
}
```

`A.*` 只匹配 `A1`、`A2`，`B1` 被排除。**返回的是 `list<C>`，元素是 `DefInit`**。

### 实现：延迟到 `IsFinal`

```cpp
const Init *InstancesOpInit::Fold(const Record *CurRec, bool IsFinal) const {
  if (CurRec && !IsFinal)
    return this;                      // ← 记录内、非 final：保持自己

  const auto *RegexInit = dyn_cast<StringInit>(Regex);
  if (!RegexInit) return this;

  StringRef RegexStr = RegexInit->getValue();
  llvm::Regex Matcher(RegexStr);
  if (!Matcher.isValid())
    PrintFatalError(Twine("invalid regex '") + RegexStr + Twine("'"));

  const RecordKeeper &RK = Type->getRecordKeeper();
  SmallVector<Init *, 8> Selected;
  for (auto &Def : RK.getAllDerivedDefinitionsIfDefined(Type->getAsString()))
    if (Matcher.match(Def->getName()))
      Selected.push_back(Def->getDefInit());

  return ListInit::get(Selected, Type);
}
```

**`if (CurRec && !IsFinal) return this;`** 是关键：只要还在「记录内」且不是 final resolve，就保持未求值状态。

### 这导致一个反直觉行为

```td
class C<int v> { int V = v; }
defvar L = !instances<C>(".*");
def A : C<1>;
def Holder { list<C> All = L; }
```

```console
$ llvm-tblgen -print-records in3.td | tail -8
def A {	// C
  int V = 1;
}
def Holder {
  list<C> All = [];
}
```

**`All` 是空列表！** 因为 `defvar L` 在 `def A` 之前求值，那时 `C` 的唯一实例还没有。

而把 `def A` 挪到 `defvar` 之前（`in2.td`），结果就正常：

```console
$ llvm-tblgen -print-records in2.td | tail -4
def Holder {
  list<C> All = [A, B];
  int Count = 2;
}
```

**规则：`!instances` 只看到「它被求值那一刻已经存在的 def」**。这是 Ch 6「先定义后引用」在 `!instances` 上的体现，也是它最危险的陷阱——**不会报错，只是悄悄给出不完整的结果**。

### `getAllDerivedDefinitionsIfDefined` 的宽容

注意用的是 `...IfDefined` 而不是 `getAllDerivedDefinitions`。区别：

```cpp
ArrayRef<const Record *>
RecordKeeper::getAllDerivedDefinitionsIfDefined(StringRef ClassName) const {
  // 类不存在时返回空，而不是报错
}
```

所以 `!instances<NotAClass>(".*")` 得到空列表，不报错。这在「可选基类」的场景下有用，但也意味着**拼错类名不会报错**。

## 11.5 `!exists`：只问「在不在」

```td
class C { int V = 1; }
def A : C;
def F1 { bit HasA = !exists<C>("A"); }
def F2 { bit HasZ = !exists<C>("Z"); }
def F3 { bit HasClass = !exists<C>("C"); }
```

```console
$ llvm-tblgen -print-records ex1.td | tail -9
def F1 {
  bit HasA = 1;
}
def F2 {
  bit HasZ = 0;
}
def F3 {
  bit HasClass = 0;
}
```

`F3` 是 0：**`!exists<C>("C")` 查的是 `def` 不是 `class`**。即使 `C` 这个 class 存在，也返回 0，因为它查的是 `getDef("C")`。

实现（`Record.cpp:2128`）：

```cpp
const Init *ExistsOpInit::Fold(const Record *CurRec, bool IsFinal) const {
  if (const auto *Name = dyn_cast<StringInit>(Expr)) {
    const Record *D = CheckType->getRecordKeeper().getDef(Name->getValue());
    if (D) {
      return IntInit::get(getRecordKeeper(),
                          D->getDefInit()->getType()->typeIsA(CheckType));
    }
    if (CurRec) {
      // Self-references are allowed, but their resolution is delayed until
      // the final resolve to ensure that we get the correct type for them.
      ...
    }
  }
  ...
}
```

**类型检查**：`D->getDefInit()->getType()->typeIsA(CheckType)`。所以 `!exists<C>("SomeOtherDef")` 返回 0，不是 1。

## 11.6 三者的定位对比

| 设施 | 失败时 | 时机 | 典型用途 |
| --- | --- | --- | --- |
| `assert` | 报错、`++ErrorsPrinted` | 顶层立即 / 记录在 `addDefOne` / multiclass 在展开 | 检查编码约束、版本一致性 |
| `dump` | 报错仅当消息非 string | 同上 | 调试展开、观察参数 |
| `!instances` | 正则非法时 `PrintFatalError` | 延迟到 `IsFinal` 或无 `CurRec` | 收集同类记录、做全局校验 |
| `!exists` | 不报错 | 名字是常量时立即 | 可选基类、条件包含 |

## 11.7 用 `assert` 做「跨记录校验」：能做到什么程度

先破除一个误解：**`!instances` 可以在 `assert` 里用**。它是一个普通表达式，只是求值时机被延迟。下面这段真的能跑（`gl9.td`）：

```td
class Ins { int Enc; string Name; }
def I1 : Ins { let Enc = 0; let Name = "nop"; }
def I2 : Ins { let Enc = 0; let Name = "ret"; }
def I3 : Ins { let Enc = 1; let Name = "add"; }

defvar All = !instances<Ins>(".*");   // 必须在所有 def 之后

def EncodingCheck {
  bit UniqueI1I2 = !ne(All[0].Enc, All[1].Enc);
  bit UniqueI1I3 = !ne(All[0].Enc, All[2].Enc);
  assert UniqueI1I2,
         !strconcat("duplicate encoding with ", All[1].Name);
}
```

```console
$ llvm-tblgen gl9.td
gl9.td:10:10: error: assertion failed: duplicate encoding with ret
  assert UniqueI1I2,
         ^
gl9.td:6:5: error: assertion failed in this record
def EncodingCheck {
    ^
llvm-tblgen: 2 errors.
```

把 `I2` 的 `Enc` 改成 2 后一切正常。所以「`.td` 只能做局部校验」这个说法**是错的**——真正的限制是：

1. **不能循环**。`.` 后缀和 `[...]` 下标都只能手写，`!foreach` 在 `assert` 的条件里不能用（`assert` 的条件必须是能 cast 成 `int` 的表达式，`!foreach` 返回 list）。
2. **只能两两手写**。要校验 N 个 instruction 的编码唯一性，得写 `N*(N-1)/2` 条 `assert`。所以实践上没人这么干。
3. **顺序敏感**。`defvar All` 必须在所有目标 def 之后（11.4 的坑）。

真正需要「遍历所有记录」的校验，上游放在 backend 里（Ch 17 的 `InstrInfoEmitter` 就有 `PrintFatalError("multiple overrides of ...")`）。但**小规模的、固定条数的跨记录断言**在 `.td` 里是可行且确实被使用的。

### 一个更隐蔽的坑：`#` 的右侧是「名字上下文」

上面 `!strconcat("duplicate encoding with ", All[1].Name)` 能用，但换成 `#` 就炸：

```td
def EncodingCheck {
  assert !ne(All[0].Enc, All[1].Enc),
         "duplicate encoding: " # All[1].Name;   // ← 报错
}
```

```console
$ llvm-tblgen gl4.td
gl4.td:7:35: error: Type 'string' is invalid, list expected
         "duplicate encoding: " # All[1].Name;
                                  ^
```

**为什么**：`ParseValue` 处理 `#` 时按左侧类型分两条路（`TGParser.cpp:3345` 起）：

```cpp
case tgtok::paste:
  SMLoc PasteLoc = Lex.getLoc();
  const auto *LHS = dyn_cast<TypedInit>(Result);
  ...
  // (A) 左侧是 list → 列表拼接
  if (isa<ListRecTy>(LHS->getType())) {
    Lex.Lex();
    ...
    const Init *RHSResult = ParseValue(CurRec, ItemType, ParseValueMode);
    Result = BinOpInit::getListConcat(LHS, RHSResult);
    break;
  }
  // (B) 其余一律 cast 成 string → 字符串拼接
  ...
  Lex.Lex();  // eat '#'
  const Init *RHSResult = ParseValue(CurRec, nullptr, ParseNameMode);  // ← 关键
```

走 (B) 时右侧用的是 **`ParseNameMode`**。而 `ParseIDValue` 在 `ParseNameMode` 下会在查完局部作用域后**立刻返回名字本身、不再查全局变量表**（`TGParser.cpp:1189`）：

```cpp
const Init *TGParser::ParseIDValue(Record *CurRec, const StringInit *Name,
                                   SMRange NameLoc, IDParseMode Mode) {
  if (const Init *I = CurScope->getVar(Records, CurMultiClass, Name, NameLoc,
                                       TrackReferenceLocs))
    return I;
  if (Mode == ParseNameMode)
    return Name;                                   // ← 这里
  if (const Init *I = Records.getGlobal(Name->getValue())) {
    ...
```

而 `defvar` 定义在**顶层**时，值根本不在任何 `TGVarScope` 里——`ParseDefvar` 把它塞进 `RecordKeeper`（`TGParser.cpp:4079`）：

```cpp
if (!CurScope->isOutermost())
  CurScope->addVar(DeclName->getValue(), Value);
else
  Records.addExtraGlobal(DeclName->getValue(), Value);   // ← 顶层走这条
```

于是 `All` 在 `ParseNameMode` 下退化成字符串 `"All"`，紧接着的 `[0]` 后缀对一个 `string` 做下标，就得到上面那条 `Type 'string' is invalid, list expected`。

**这个区分可以被实验直接证实**（`gn3.td` vs `gn4.td`）——把 `defvar` 从顶层挪进 `def` 里，同样的 `#` 写法就通了：

```td
// gn3.td：defvar 在 def 内部（走 CurScope->addVar）
def R2 {
  defvar Loc = !instances<Ins>(".*");
  string A = "x" # Loc[0].Name;
}

// gn4.td：defvar 在顶层（走 Records.addExtraGlobal）
defvar G = !instances<Ins>(".*");
def R2 {
  string A = "x" # G[0].Name;
}
```

```console
$ llvm-tblgen -print-records gn3.td | grep 'string A'
  string A = "xnop";

$ llvm-tblgen gn4.td
gn4.td:5:20: error: Type 'string' is invalid, list expected
  string A = "x" # G[0].Name;
```

`gn3` 的 `Loc` 能被 `CurScope->getVar` 查到，所以 `#` 右边正常工作；`gn4` 的 `G` 只能靠 `Records.getGlobal` 查，而 `ParseNameMode` 在那一行之前就返回了。

顺带一个更极端的对照：`"x" # Loc`（`Loc` 是 list）会得到一条「能解析但语义荒谬」的初始化式：

```console
gn2.td:3:5: error: Initializer of 'B' in 'R2' could not be fully resolved: !strconcat("x", !cast<string>([I1]))
```

即 `#` 把整个 list 当值 cast 成 `string` 后拼接——**这是 `#` 唯一的「list 参与字符串拼接」路径**，几乎不可能是你想要的。

整理成表（全部实测）：

| 写法 | 结果 | 原因 |
| --- | --- | --- |
| `"x" # G`（`G` 是顶层 `defvar`） | `"xG"` | RHS 走 `ParseNameMode`，顶层 global 被跳过 |
| `"x" # G[0].Name`（同上） | error | `"G"` 是 string，不能下标 |
| `"x" # Loc[0].Name`（`Loc` 是 def 内 `defvar`） | `"xnop"` ✅ | 走 `CurScope->getVar`，正常查到 |
| `"x" # Loc`（`Loc` 是 list） | `!cast<string>([I1])` | 整表被 cast 成 string |
| `All[0].Name # "x"` | `"nopx"` ✅ | LHS 走 `ParseValueMode` |
| `All # []` | list concat ✅ | LHS 是 list，走 `getListConcat` 分支 |
| `"x" # 1` | `"x1"` ✅ | 字面量不需要查名字 |
| `!strconcat("x", G[0].Name)` | `"xnop"` ✅ | `!strconcat` 的参数是普通表达式 |

**可操作结论**：`#` 右侧只要出现「变量名 + `.`/`[` 后缀」，就换成 `!strconcat`。这不是代码风格问题，而是 `ParseNameMode` 与 `ParseValueMode` 的语义差异。这也解释了为什么上游 `.td` 里 `!strconcat` 出现频率远高于 `#`。

## 11.7.1 那「跨记录全局校验」到底该怎么写

三个选项，按成本排序：

1. **手写若干条 `assert`**（上面 `gl9.td`）：适合「固定 2–5 条」的不变量，比如「这个 target 的 ABI 版本号必须和三个 def 一致」。
2. **`foreach` 展开成多条 assert**：`foreach` 的循环体是语句序列，里面可以放 `assert`：
   ```td
   foreach i = 1...3 in {
     assert !ne(i, 2), "foreach i cannot be 2";
     def bar_ # i;
   }
   ```
   这是 `llvm/test/TableGen/assert.td` 里的原始测试。注意 `assert` 每条都独立求值，报错时位置是 `assert` 那一行（不是 `i` 的具体值），所以循环里的断言**要自己把 `i` 拼进消息**，否则 N 条一样的错误无法区分。
3. **写进 backend**：O(N²) 或需要排序、聚合的校验都该在这里做。好处是能一次报出所有冲突，坏处是用户必须跑 `llvm-tblgen -gen-*` 才能触发。`InstrInfoEmitter` 的编码冲突检查就是这一类。

## 11.8 本章检查清单

- [ ] 说出 `assert` 在三种位置的检查时机
- [ ] 解释为什么 class 里的 assert 报错位置是 class 定义处
- [ ] 说出 `dump` 走的是 `PrintNote` 还是 `PrintError`，以及什么时候会变成 error
- [ ] 解释 `!instances` 的 `if (CurRec && !IsFinal) return this;` 造成什么后果
- [ ] 说出 `!exists<C>("C")` 为什么返回 0
- [ ] 说出 `.td` 层面做跨记录校验的三个限制（不能循环、只能两两手写、顺序敏感）
- [ ] 解释 `"x" # All` 为什么得到 `"xAll"`

## 11.9 踩雷记录

**错误直觉 1：`!instances` 总是给出完整列表。**
正确认识：它只看到「求值时刻已存在的 def」。`defvar` 在 `def` 之前时得到空列表，且**不报错**。这是本章最危险的坑。

**错误直觉 2：`!exists<ClassName>("Name")` 检查类是否存在。**
正确认识：检查的是**名为 `Name` 的 def 是否存在，且类型是 `ClassName` 的子类**。`!exists<C>("C")` 返回 0。

**错误直觉 3：`dump` 只是调试输出，无所谓。**
正确认识：非 string 的 dump 消息会让 `ErrorsPrinted++`，退出码变 1。`dump 42;` 是一次编译失败。

**错误直觉 4：`assert` 的条件可以是任意类型。**
正确认识：必须能 cast 成 `int`（`bit`/`bits`/`int`）。`assert "x", "msg"` 会报 `assert condition must of type bit, bits, or int.`。

**错误直觉 5：`assert` 失败会继续解析，收集所有错误。**
正确认识：会继续（`PrintError` 不 `exit`），但 `ErrorsPrinted` 非 0 时 `TableGenMain` 直接返回 1，**不执行 backend**。所以 assert 失败时你拿不到任何生成物。

**错误直觉 6：`!instances` 的正则很强大，可以随便写。**
正确认识：用的是 `llvm::Regex`，非法正则直接 `PrintFatalError`（进程退出），不是可恢复错误。

**错误直觉 7：`"prefix" # X` 和 `!strconcat("prefix", X)` 等价。**
正确认识：`#` 的右侧走 `ParseNameMode`，**顶层** `defvar` 在那里会退化成字面名字（`"x" # All` → `"xAll"`）；def 内部的 `defvar` 反而正常。所以同一个表达式在两种作用域下行为不同——这是 `CurScope->getVar` 与 `Records.getGlobal` 两条查找路径的差别。稳妥做法一律用 `!strconcat`。

**错误直觉 8：`.td` 层面做不了跨记录校验。**
正确认识：能做，只是不能循环。`!instances` + 手写下标 + `assert` 完全可以校验固定条数的不变量（11.7 的 `gl9.td` 是完整可跑示例）。

## 11.10 延伸阅读

- **`llvm/lib/TableGen/Error.cpp`** 的 `CheckAssert`（169）与 `dumpMessage`（187）
  - 本章 11.2–11.3 的全部实现。40 行读完。
- **`llvm/lib/TableGen/Record.cpp`** 的 `InstancesOpInit::Fold`（2190）
  - `!instances` 的延迟条件。对照 11.4 的「空列表」实验。
- **`llvm/lib/TableGen/Record.cpp`** 的 `ExistsOpInit::Fold`（2128）
  - `!exists` 的类型检查与自引用延迟。
- **`llvm/test/TableGen/assert.td`**（245 行）
  - 官方断言测试集。五种位置、`CHECK-NOT` 验证「只报一次」、multiclass 展开后的断言——全在这里。
- **`llvm/test/TableGen/`** 里的 `instances*.td`
  - `!instances` 的回归测试，能看到它和其他 operator 的组合方式。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseIDValue`（1189）与 `ParseValue` 的 `paste` 分支（3345）
  - 11.7 那个 `#` 坑的全部实现。重点看 `ParseNameMode` 提前返回那一行，以及 `Records.getGlobal` 的位置。

## 下一章

Ch 12 讲错误报告基础设施：`PrintError(ArrayRef<SMLoc>)` 为什么要数组、`SrcMgr` 的全局状态怎么工作、`PrintFatalError` 和 `PrintError` 的分界线在哪。这是「改源码」时最容易踩坑的一章。

[Ch 12 错误报告基础设施：PrintError、SMLoc、SourceMgr](./12-error-reporting.md)
