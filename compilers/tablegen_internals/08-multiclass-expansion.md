# Ch 8 multiclass 展开算法：模板参数替换与 `defm` 前缀

> **本章目标**：能手工推演任意 `defm` 展开后会产生哪些记录、名字是什么；理解 `SubstStack` 的「替换表」语义；知道 `NAME` 隐式参数和嵌套 `defm` 前缀拼接的规则；能解释为什么 `defm D : M<5>, Base;` 会报 `Already subclass of 'Base'`。

> **环境**：LLVM `24.0.0git`。

---

## 8.1 先把展开结果看全

```td
class C<int n> { int N = n; }
multiclass M<int k> {
  def _a : C<k>;
  def _b : C<!add(k, 1)>;
}
defm D : M<5>;
defm E : M<100>;
```

```console
$ llvm-tblgen -print-records mc1.td | tail -14
def D_a {	// C
  int N = 5;
}
def D_b {	// C
  int N = 6;
}
def E_a {	// C
  int N = 100;
}
def E_b {	// C
  int N = 101;
}
```

三个可以直接观察到的规则：

1. **记录名 = `defm` 名字 + `_` + multiclass 内部 def 名**。`def _a` 在 `defm D` 下变成 `D_a`。
2. **模板参数被代入**：`k` → `5` / `100`。
3. **表达式被求值**：`!add(k, 1)` → `6` / `101`。

本章剩下的内容就是解释这三条是怎么实现的。

## 8.2 `SubstStack`：一个字符串替换表

`resolve` 的核心是一个 `MapResolver`：

```cpp
// TGParser.cpp:525
bool TGParser::resolve(const std::vector<RecordsEntry> &Source,
                       SubstStack &Substs, bool Final,
                       std::vector<RecordsEntry> *Dest, SMLoc *Loc) {
  for (auto &E : Source) {
    ...
    } else {
      auto Rec = std::make_unique<Record>(*E.Rec);   // ① 复制模板
      if (Loc) Rec->appendLoc(*Loc);

      MapResolver R(Rec.get());
      for (const auto &S : Substs)
        R.set(S.first, S.second);                    // ② 装替换表
      Rec->resolveReferences(R);                     // ③ 一遍替换

      if (Dest) Dest->push_back(std::move(Rec));
      else      Error = addDefOne(std::move(Rec));   // ④ 加进 RecordKeeper
    }
    ...
  }
}
```

**关键点：`Multiclass::Entries` 里存的是「模板记录」**（`def _a` 被解析时的 `RK_Def`），展开时**复制一份**、套上替换表、再 `resolveReferences`。原模板永远不变，所以可以展开多次。

`SubstStack` 就是 `vector<pair<const Init*, const Init*>>`（名字 → 值），装进 `MapResolver` 后，`VarInit("k")->resolveReferences(R)` 会查到 `IntInit(5)`。

这个设计的后果：

- **展开是「复制 + 替换」，不是「求值」**。所以 `def _a : C<k>` 里的 `k` 在模板里是 `VarInit`，展开后变 `IntInit(5)`。
- **同一个 multiclass 展开 N 次会产生 N 份记录**，模板本身不是记录。

## 8.3 `NAME`：隐式参数

```td
class C<string n> { string N = n; }
multiclass M<int k> { def _a : C<NAME>; }
defm D : M<5>;
```

```console
$ llvm-tblgen -print-records mc6.td | tail -3
def D_a {	// C
  string N = "D";
}
```

`NAME` 没有被声明，但在 multiclass 里可用。实现在 `resolveArgumentsOfMultiClass`：

```cpp
bool TGParser::resolveArgumentsOfMultiClass(
    SubstStack &Substs, MultiClass *MC, ...) {
  // Add an implicit argument NAME.
  Substs.emplace_back(QualifiedNameOfImplicitName(MC), DefmName);
  return resolveArguments(...);
}
```

`NAME` 的值是 `defm` 的**名字**（`D`），不是 `D_a`。注意 `QualifiedNameOfImplicitName(MC)` 会对名字做限定（`M:NAME`），所以用户写的 `NAME` 能被解析到。

`NAME` 的常见用途是「把 defm 的名字嵌进字符串」：

```td
multiclass M<int k> {
  def _a { string FullName = "prefix_" # NAME; }
}
```

> Ch 3 的 `ParseDeclaration` 里有 `if (Str == "NAME") return TokError("'NAME' is a reserved variable name")`——**字段不能叫 NAME**，但表达式里可以引用它。这个不对称是因为 `NAME` 只在 multiclass 作用域里有值，做成字段名会掩盖它的特殊身份。

## 8.4 嵌套 `defm`：前缀是拼起来的

```td
class C<int n> { int N = n; }
multiclass Inner<int k> { def _i : C<k>; }
multiclass Outer<int k> { defm _o : Inner<k>; }
defm X : Outer<7>;
```

```console
$ llvm-tblgen -print-records mc2.td | tail -3
def X_o_i {	// C
  int N = 7;
}
```

名字是 `X_o_i`：外层 `defm X` + 中层 `defm _o` + 内层 `def _i`。拼接逻辑在 `ParseDefm`：

```cpp
// 匿名 defm 时
if (isa<UnsetInit>(DefmName)) {
  DefmName = Records.getNewAnonymousName();
  if (CurMultiClass)
    DefmName = BinOpInit::getStrConcat(
        VarInit::get(QualifiedNameOfImplicitName(CurMultiClass),
                     StringRecTy::get(Records)),
        DefmName);
}
```

而 `resolve` 里记录名的拼接靠 `Record` 的 name 本身是 `Init*`——`def _i` 的 `Name` 是 `StringInit("_i")`，`defm _o` 展开时把 `_o` 前缀截掉、换成 `X_o`。

### 匿名 `defm`

```td
multiclass M<int k> { def _a : C { let N = k; } }
defm : M<5>;
```

```console
$ llvm-tblgen -print-records mc4.td | tail -3
def anonymous_0_a {	// C
  int N = 5;
}
```

匿名 `defm` 得到 `anonymous_N` 前缀（Ch 6 的 `AnonymousNameInit`）。

## 8.5 `defm` 后面还能接普通 class

这是 TableGen 一个容易被忽略的语法：

```td
class Base { int B = 1; }
multiclass M<int k> { def _a : Base { let B = k; } }
defm D : M<5>, Base;
```

```console
$ llvm-tblgen mc5.td
mc5.td:3:16: error: Already subclass of 'Base'!

defm D : M<5>, Base;
               ^
```

为什么报错？因为 `def _a : Base` 已经让展开后的记录继承 `Base`，再 `, Base` 是重复继承。换成不冲突的：

```td
class Base { int B = 1; }
class Extra { int E = 2; }
multiclass M<int k> { def _a : Base { let B = k; } }
defm D : M<5>, Extra;
def D_a : Base, Extra;   // 手工写等价形式
```

*本段未实测，但 `AddParentClass` 的 `assert(!isSubClassOf(R) && "Already subclassing record!")` 决定了重复继承必然失败；上面的错误就是它的用户可见版本（`addDirectSuperClass` 的 assert 被 `TGParser::AddParentClass` 转成了 `Error`）。*

实现上，`ParseDefm` 的最后一段专门处理这种「multiclass + 普通 class」混合：

```cpp
if (InheritFromClass) {
  ClassReference ParentClass = ParseClassReference(nullptr, false);
  while (true) {
    if (!ParentClass.Rec) return true;
    for (auto &E : NewEntries) {
      if (AddParentClass(E, ParentClass)) return true;
    }
    if (!consume(tgtok::comma)) break;
    ParentClass = ParseClassReference(nullptr, false);
  }
}
```

**普通 class 只能出现在继承列表末尾**（`InheritFromClass` 一旦为 true 就 `break` 出 multiclass 循环）。这是语法限制，不是实现缺陷——因为 multiclass 展开后要统一加父类，如果中间再插一个 multiclass 就没法确定顺序。

## 8.6 展开的时机：`Final` 怎么传

```cpp
// ParseDefm 里
if (resolve(MC->Entries, Substs, !CurMultiClass && Loops.empty(),
            &NewEntries, &ParentClassLoc))
```

`Final = !CurMultiClass && Loops.empty()` 的意思是：

- **在顶层 `defm`**（不在 multiclass、不在 foreach）：立即 Final 展开。
- **在嵌套 multiclass 里**：非 Final 展开，把「还没绑定完的记录」放进 `NewEntries`，等外层再展开。

这解释了为什么嵌套 `defm` 的错误信息会带多层「instantiated from multiclass」（Ch 4 的 `dupm.td` 实验）：每一层 `resolve` 都 `appendLoc` 一次。

```cpp
auto Rec = std::make_unique<Record>(*E.Rec);
if (Loc) Rec->appendLoc(*Loc);       // ← 每层加一个位置
```

## 8.7 展开后的 `assert` / `dump` 也会被执行

`resolve` 对 `Assertion` 和 `Dump` 有单独分支：

```cpp
} else if (E.Assertion) {
  MapResolver R;
  for (const auto &S : Substs) R.set(S.first, S.second);
  const Init *Condition = E.Assertion->Condition->resolveReferences(R);
  const Init *Message = E.Assertion->Message->resolveReferences(R);
  if (Dest) Dest->push_back(std::make_unique<Record::AssertionInfo>(...));
  else      CheckAssert(E.Assertion->Loc, Condition, Message);
}
```

所以 multiclass 里的 `assert` **在展开时**检查，且只检查一次（每次 `defm` 一次）。上游测试 `llvm/test/TableGen/assert.td` 里就有这个场景：

```td
multiclass MC1<string id, int seq> {
  assert !le(!size(id), 5), "MC1 id string is too long";
  def _mc1 { string ID = id; int Seq = seq; }
}
defm Rec40 : MC1<"ILISP", 999>;      // 通过
defm Rec41 : MC1<"ILISPX", 999>;     // id 太长 → 失败
```

注意 `Dest` 非空时（嵌套 multiclass），assert **不立刻检查**，只是把节点转移过去——和记录一样延迟。

## 8.8 本章检查清单

- [ ] 手工推导嵌套两层 `defm` 产生的记录名
- [ ] 解释 `NAME` 的值是什么，为什么字段不能叫 `NAME`
- [ ] 说出 `defm` 展开的三步：复制、装替换表、resolve
- [ ] 解释为什么同一 multiclass 可以展开多次而不互相污染
- [ ] 说出 `Final` 参数在 `ParseDefm` 里的表达式，以及它的含义
- [ ] 解释 `defm D : M<5>, Base;` 为什么会报 `Already subclass of` 错误

## 8.9 踩雷记录

**错误直觉 1：`defm` 会「修改」multiclass 里的 def。**
正确认识：它**复制**一份模板记录，套替换表后 resolve。multiclass 本体永远是模板。

**错误直觉 2：`NAME` 是 `D_a`（展开后的记录名）。**
正确认识：是 `D`（`defm` 的名字）。展开后的记录名是 `D_a`，由「前缀 + 内部名」拼成，`NAME` 不参与。

**错误直觉 3：`defm` 后面只能接 multiclass。**
正确认识：可以接普通 class（`defm D : M<5>, Base;`），但只能放在末尾，且不能和 multiclass 内部已继承的类重复。

**错误直觉 4：嵌套 `defm` 的名字是 `X` + `_o` + `_i` 三段拼接的字符串。**
正确认识：机制上是「每层截掉自己的前缀、换上新前缀」。`defm X : Outer<7>` 里的 `defm _o : Inner<k>` 先被外层展开成 `X_o`，再展开内层 `_i` 成 `X_o_i`。结果一样，但过程不是简单字符串拼接。

**错误直觉 5：multiclass 里的 `assert` 会在 `defm` 时就检查。**
正确认识：只在 `Final` 展开时检查（`Dest == nullptr` 时）。嵌套 multiclass 里的 `assert` 会推迟。

## 8.10 延伸阅读

- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseDefm`（4565）
  - 本章 8.4–8.6 的来源。约 100 行，建议完整读。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `resolve`（525）
  - 展开算法的核心 55 行。注意 `Record` 的复制、`MapResolver` 的构建、`Dest` 的分支。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `resolveArgumentsOfMultiClass`（684）
  - `NAME` 隐式参数的注入点。
- **`llvm/lib/TableGen/TGParser.h`** 的 `MultiClass` 与 `RecordsEntry`（55–110）
  - 模板存储的数据结构。对照本章 8.2。
- **`llvm/test/TableGen/multiclass.td`** 与 `multiclass-forward.td`
  - 官方对展开语义的回归测试。后者专门测「multiclass 引用后面的 multiclass」的边界。

## 下一章

Part 1 结束。练习 B 会让你用 C++ 复刻 `-print-records` 的输出——这会强迫你把 `Record`、`Init`、`Fold`、`resolve` 全部串起来。之后 Part 2 进入类型系统与语义检查。

[练习 B：用 C++ 复刻 `-print-records` 的输出](./practice-b-reimplement-print-records.md)
