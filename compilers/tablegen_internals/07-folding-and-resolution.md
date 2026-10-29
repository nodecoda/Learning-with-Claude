# Ch 7 求值：`fold()`、`resolveReferences()` 与惰性求值时机

> **本章目标**：能准确说出「一个表达式在什么时刻变成常量」；理解 `fold()` 与 `resolveReferences()` 是两套不同机制；用实验证明 `!if` 的「短路」是有条件的——这直接决定你写的 `.td` 会不会因为死分支里的错误而编译失败。

> **环境**：LLVM `24.0.0git`。

---

## 7.1 两套机制，不是一个

新手常以为「TableGen 会在某一刻把所有表达式算成常量」。实际上是**两条独立的路径**：

| 机制 | 触发点 | 处理什么 | 失败表现 |
| --- | --- | --- | --- |
| `Init::Fold(CurRec)` | 每次构造 `Init` 后立刻调用 | 纯算术/字符串/列表运算 | 立刻报错（如除零） |
| `resolveReferences(Resolver&)` | `addDefOne`、`Record::resolveReferences` | 名字引用 → 具体值 | 取决于 `isFinal()` |

```
ParseSimpleValue
   │
   ├─ 构造 IntInit(3)              ← 字面量，无需 fold
   ├─ 构造 BinOpInit(ADD, 1, 2)    ← 构造后立刻 .Fold() → IntInit(3)
   ├─ 构造 VarInit("a")            ← 只登记名字，不 fold
   └─ 构造 FieldInit(I, "V")       ← 只登记，不 fold
                                        │
                                        ▼
                             addDefOne → Rec->resolveReferences(...)
                                        │
                                        ▼
                              VarInit/FieldInit 变成具体值
```

**`Fold` 只处理「不需要外部信息就能算」的表达式**；只要涉及名字，就必须等 `resolveReferences`。

## 7.2 `Fold` 是解析期的即时求值

看 `ParseOperation` 里二元运算的处理（`TGParser.cpp:1925` 附近）：

```cpp
RHS = (BinOpInit::get(Code, InitList.back(), RHS, Type))->Fold(CurRec);
```

**构造后立刻 `Fold`**。这就是为什么：

```console
$ printf 'def A { int X = !div(1, 0); }\n' > dz.td && llvm-tblgen dz.td
dz.td:1:5: error: Illegal operation: division by zero
def A { int X = !div(1, 0); }
    ^
```

错误位置在 `def A`，不是 `!div`——因为 `Fold` 是递归向上的，最外层 `def` 的 `addDefOne` 收到了失败信号。

### 死分支也会被求值

这是一个反直觉但必须记住的行为：

```console
$ printf 'def A { int X = !if(0, !div(1,0), 10); }\n' > sc4.td && llvm-tblgen sc4.td
sc4.td:1:5: error: Illegal operation: division by zero
def A { int X = !if(0, !div(1,0), 10); }
    ^
```

条件明明是 `0`，`!div(1,0)` 是死分支，**但它还是报错了**。原因：`ParseOperation` 在解析 `!if` 的三个参数时，每个参数都是独立的 `ParseValue`，**内层的 `!div` 在构造后立刻 `Fold`**，此时还没轮到外层的 `!if`。

`!cond` 同理：

```console
$ printf 'def A { int X = !cond(1:10, true:!div(1,0)); }\n' > sc5.td && llvm-tblgen sc5.td
sc5.td:1:5: error: Illegal operation: division by zero
```

### 但变量条件确实能短路

如果条件不是常量，`!if` 在解析期无法决定，于是三个参数被原样保留成 `TernOpInit`：

```td
class C<int n, int d> { int X = !if(n, 10, !div(10, d)); }
def A : C<0, 5>;    // 走 else，10/5 = 2
def B : C<1, 0>;    // 走 then，不碰 !div
```

```console
$ llvm-tblgen -print-records sc9.td | tail -6
def A {	// C
  int X = 2;
}
def B {	// C
  int X = 10;
}
```

**`B` 的 `d=0` 却没有报错**——因为 `!if` 的 `Fold` 实现（`Record.cpp:1806`）是：

```cpp
case IF: {
  if (const auto *LHSi = dyn_cast_or_null<IntInit>(
          LHS->convertInitializerTo(IntRecTy::get(RK)))) {
    if (LHSi->getValue())
      return MHS;
    return RHS;      // ← 只返回一支，另一支的 Fold 不会发生
  }
  break;              // 条件未定，保持 TernOpInit
}
```

它**先看条件**，定下来后只返回一支。但注意：这只有在「解析期条件还没定」时才有意义。如果条件在解析期就是常量，内层子表达式早已各自 fold 过了。

### 三条结论（本课最重要的求值规则）

1. **子表达式先于父表达式求值**（因为 `ParseValue` 自底向上构造）。
2. **常量条件不会保护死分支**（死分支在更早的时刻已经 fold 过）。
3. **变量条件可以保护死分支**（`TernOpInit` 保留到 resolve 期，`Fold` 只碰选中的一支）。

第 3 条还有个更早的阻碍：**未定义的标识符在解析期就报错**。

```console
$ cat dead.td
class C<int n> { int X = !if(n, 10, NotDefined); }
def A : C<1>;
$ llvm-tblgen dead.td
dead.td:1:37: error: Variable not defined: 'NotDefined'
class C<int n> { int X = !if(n, 10, NotDefined); }
                                    ^
```

`ParseSimpleValue` 的 `Id` 分支在找不到名字时**立刻报错**（Ch 3 的 `Variable not defined`），根本轮不到 `!if` 短路。所以「变量条件保护死分支」只适用于**语法上合法、语义上会失败**的表达式（如除零），不适用于**名字根本不存在**的情况。

## 7.3 `resolveReferences`：从名字到值

`Record::resolveReferences`（`Record.h:1963`）的契约：

```
/// If there are any field references that refer to fields that have been
/// filled in, we can propagate the values now.
void resolveReferences();
```

它构造一个 `RecordResolver`，然后对**每个字段**调用 `Init::resolveReferences`：

```cpp
void Record::resolveReferences() {
  RecordResolver R(*this);
  R.setFinal(true);
  R.resolveName(..., Name);
  for (RecordVal &Val : Values)
    Val.setValue(Val.getValue()->resolveReferences(R));
  ...
}
```

`RecordResolver::resolve` 的核心是「按名字在当前记录的 `Values` 里查」：

```cpp
const Init *RecordResolver::resolve(const Init *Name) const {
  // 先看 ShadowResolver 覆盖
  ...
  if (const RecordVal *RV = CurRec->getValue(Name))
    return RV->getValue();
  if (const RecordVal *RV = CurRec->getTemplateArg(Name))
    return RV->getValue();
  return nullptr;
}
```

### `isFinal()` 的两层含义

`Resolver::setFinal(true)` 之后：

1. **子节点必须全部解析成功**，否则报错。
2. **某些节点会「改形」**：`AnonymousNameInit` → `StringInit`（Ch 6）、`ArgumentInit` → 具体值。

非 final 时，未解析的 `VarInit` 原样返回，等待下一轮。这就是 Ch 6 说的「模板参数」场景：

```cpp
// ParseDefm 里（TGParser.cpp:4612）
if (resolve(MC->Entries, Substs, !CurMultiClass && Loops.empty(), ...))
```

只有最外层 `defm` 才 `Final = true`。

## 7.4 一个完整的时机实验

用 `dump` 把求值时机变成可观测的（Ch 11 会系统讲 `dump`，这里先用）：

```td
class C<int n, int d> {
  dump "class C body, n=" # n;
  int X = !if(n, 10, !div(10, d));
}
def A : C<0, 5>;
```

```console
$ llvm-tblgen -print-records tm.td
tm.td:2:3: note: class C body, n=0
  dump "class C body, n=" # n;
  ^
```

（`dump` 输出到 stderr，格式是 `note:`，且会附带源码行——这和 `PrintNote(Loc, ...)` 的行为一致，Ch 12 会拆。）

注意它打印的是 `n=0` 而不是 `n=C:n`——因为 `dump` 的参数在 `def` 实例化时已经 resolve 过。**`dump` 的位置决定了你看到的是「模板形态」还是「实例形态」**：

- 写在 `class` body：每次实例化都打印一次，值已绑定。
- 写在 `def` body：只打印一次。
- 写在顶层：只在解析时打印一次。

## 7.5 `Fold` 的完整清单

`Init` 的每个子类都有 `Fold`，但语义不同：

| 节点 | `Fold` 做什么 |
| --- | --- |
| `IntInit` / `BitInit` / `BitsInit` / `StringInit` | 返回自己（已是最简） |
| `ListInit` | 对每个元素 `Fold`，若变了就重建 |
| `DagInit` | 解析操作符与每个参数，`ArgNames` 跟随 |
| `VarInit` | 查 `RecordKeeper` 的 `ExtraGlobals`，查不到就返回自己 |
| `FieldInit` | 先 `Fold` 记录，再查字段 |
| `BinOpInit` | 按 opcode 分派：算术、字符串、列表、DAG |
| `TernOpInit` | `!if` 短路；`!foreach`/`!filter`/`!sort` 走 helper |
| `UnOpInit` | `!not`/`!size`/`!cast` 等 |
| `CondOpInit` | 依次比条件，命中即返回（**但子节点已各自 fold**） |

**`CondOpInit::Fold` 的注释值得看**：

```cpp
// This is a hack to make !cond work. ...
// !cond short-circuits at the first true condition, resolving to that
// condition's corresponding value. Subsequent conditions and values are left
// unresolved.
```

「left unresolved」意味着 `!cond` 的未命中分支在 `Fold` 里真的不会被访问。但**前提是它们还没在解析期被单独 fold**——这就是 7.2 的实验结论。

## 7.6 什么时候会「永远不 fold」

有三种情况，表达式会一直保持树形态：

1. **class body 里的字段**：模板参数未绑定，`Fold` 只能部分求值。

   ```console
   $ llvm-tblgen -print-records p.td | head -4
   class Pair<int Pair:a = ?, int Pair:b = ?> {
     int Sum = !add(Pair:a, Pair:b);
   }
   ```

2. **`!if` 条件未定**：整个 `TernOpInit` 保留（7.2 的 `sc7.td`）。

3. **`!foreach`/`!filter` 的输入部分未定**：这是最微妙的一种——**它会「部分求值」**。

   实测：

   ```td
   class C<int n> { list<int> L = !foreach(v, [1,2,n], !mul(v, 2)); }
   def A : C<5>;
   ```

   ```console
   $ llvm-tblgen -print-records fe.td | tail -7
   class C<int C:n = ?> {
     list<int> L = [2, 4, !mul(C:n, 2)];
   }
   ------------- Defs -----------------
   def A {	// C
     list<int> L = [2, 4, 10];
   }
   ```

   **注意 class 里的形态是 `[2, 4, !mul(C:n, 2)]`，不是完整的 `!foreach(...)`！** `Fold` 对已知元素逐个求值，只把未定的那一个留下。这比「整体保留」更细，也意味着 `!foreach` 不是「全有或全无」的。

**这个「部分求值」是 TableGen 能表达复杂模板的关键**：同一个字段在 class 里是函数，在 def 里是值。

## 7.7 本章检查清单

- [ ] 说出 `Fold` 与 `resolveReferences` 各自处理什么，触发点在哪
- [ ] 解释为什么 `!if(0, !div(1,0), 10)` 会报错
- [ ] 解释为什么 `class C<int n,int d>{int X=!if(n,10,!div(10,d));}` 配 `C<1,0>` 不报错
- [ ] 说出「子表达式先于父表达式求值」带来的三条结论
- [ ] 解释 `isFinal()` 的两个含义
- [ ] 说出三种「永远不 fold」的情况

## 7.8 踩雷记录

**错误直觉 1：`!if`/`!cond` 会短路，所以死分支里的错误不会报。**
正确认识：只在**条件本身未定**时短路。条件在解析期是常量时，死分支的子表达式早已各自 fold 过，错误照报。实测 `!if(0, !div(1,0), 10)` 直接 error。

**错误直觉 2：`Fold` 是「最后统一求值」。**
正确认识：它是**解析期即时求值**，每个 `Init` 构造后立刻调用。所以错误位置常常指向 `def` 而不是具体表达式。

**错误直觉 3：`resolveReferences` 会重算所有东西。**
正确认识：它只处理「名字引用」。已经 fold 成常量的部分不会再被访问。

**错误直觉 4：`isFinal()` 是「最后一遍」的意思。**
正确认识：它是调用方设的**语义开关**——final 时未解析的名字必须报错、匿名名必须转正。非 final 时可能保留未解析节点。

**错误直觉 5：class body 里的字段「没有值」。**
正确认识：它有值，只是那个值是**未完全折叠的表达式树**（如 `!add(Pair:a, Pair:b)`）。`-print-records` 能看到它。

## 7.9 延伸阅读

- **`llvm/lib/TableGen/Record.cpp`** 的 `TernOpInit::Fold`（1742）
  - 本章 7.2 的权威来源。重点看 `case IF:` 的「先看条件再返回一支」，以及 `case COND:` 的延迟语义注释。
- **`llvm/lib/TableGen/Record.cpp`** 的 `BinOpInit::Fold`
  - 二元运算的完整分派表。想确认某个 `!op` 在什么条件下能 fold，查这里。
- **`llvm/lib/TableGen/TGParser.cpp`** 的 `ParseOperation`（1220）
  - 每个 `->Fold(CurRec)` 的调用点。这是「解析期求值」的证据链。
- **`llvm/include/llvm/TableGen/Record.h`** 的 `Resolver`（2294）与 `RecordResolver`（2394）
  - `resolve` 的查找顺序：Shadow → 当前记录字段 → 模板参数。
- **`llvm/lib/TableGen/Record.cpp`** 的 `Record::resolveReferences`（约 3000 行）
  - 记录级 resolve 的完整流程，包括 `SkipVal` 参数（避免自引用死循环）。

## 下一章

Ch 8 讲 multiclass 展开：`defm` 如何把一份模板变成 N 个记录，`SubstStack` 怎么记录替换，以及为什么嵌套 `defm` 的 `isFinal` 会推迟。

[Ch 8 multiclass 展开算法：模板参数替换与 defm 前缀](./08-multiclass-expansion.md)
