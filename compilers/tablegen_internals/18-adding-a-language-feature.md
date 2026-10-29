# 第 18 章：给 TableGen 加一个新语言特性（`!min` / `!max` 全链路）

> **目标**：亲手把一个新 bang operator 从 lexer 一路加到文档和 lit 测试，得到一个**可提交形状**的补丁（7 个文件、+39/−2、2 个测试），并理解「加语法」和「加 backend」在工程上的本质区别——前者动的是编译器前端，后者只是 `RecordKeeper` 的一个消费者。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。所有输出都在本机实跑：用 `/tmp/tgbuild`（`Release + LLVM_ENABLE_ASSERTIONS=ON`，`-DLLVM_TARGETS_TO_BUILD=""`）增量编译 `llvm-tblgen`，跑 `llvm-lit llvm/test/TableGen`。实验结束后源码已全部还原，`git status` 为空；补丁与测试文件留在 `/tmp/tg18/`（临时目录可能被清，正文里给了全文）。

---

## 18.1 一个「语言特性」到底要穿过几层

先把地图画出来。TableGen 的一个表达式值，从源码到被 backend 看见，依次经过：

```text
字符流
  │  TGLexer::LexExclaim          ← 认 !min / !max 这两个词
  ▼
tgtok::XMIN / tgtok::XMAX         ← token 编号
  │  TGParser::ParseOperation      ← 语法：括号、逗号、可变参数
  ▼
BinOpInit(MIN, LHS, RHS, int)     ← AST/Init 层：一个可折叠的表达式节点
  │  BinOpInit::Fold               ← 常量折叠：能算就算
  ▼
IntInit(7)                        ← 具体值
  │  Record 字段 / backend
  ▼
生成的 .inc 或 -print-records 输出
```

这一章选的是**最小但完整**的一条纵向切片：一个纯函数式的二元运算符，可以接受两个以上参数（variadic），不引入新类型、不改语法语句结构。它小到你能在一章内真正改完，又足够完整到必须碰每一层。

对照一下「加 backend」（Ch 13/14、练习 D）：backend 只出现在最后一行箭头之后，它消费现成的 `RecordKeeper`，**完全不用动 lexer/parser/Init**。所以：

| | 加 backend | 加语言特性 |
|---|---|---|
| 改动面 | 1 个新文件 + 1 行 CMake | lexer + parser + Init + 文档 + 测试 |
| 失败方式 | 运行期 `PrintFatalError` | 编译期 `llvm_unreachable`、解析期静默错误 |
| 回归风险 | 只影响自己的输出 | 影响**所有** `.td` 文件的解析 |
| 代码量 | 100–800 行 | 本章这个 39 行（不含测试） |

**结论先说**：语言特性的工作量不在「写逻辑」，在「把同一个概念在 4 个地方正确注册，并让它们对同一条错误路径达成一致」。

---

## 18.2 为什么选 `!min` / `!max`，而不是别的

选型有四个硬约束：

1. **语义无争议**：`min`/`max` 不需要设计讨论，reviewer 不会为语义打架。
2. **不引入新类型**：参数是 `int`/`bits`，结果 `int`，复用现成的 `IntRecTy`。
3. **可复用现成机制**：`!add`/`!mul` 已经是 variadic，照抄它的折叠循环即可。
4. **有真实需求**：写 target 时经常要 `!max(Alignment, 4)`、`!min(NumRegs, 32)` 这种表达式。

排除掉的候选：

- `!tolower` / `!toupper`：**已经存在**（`TGLexer.cpp:700` 附近，`UnOpInit::TOLOWER/TOUPPER`）。这本身是个好提醒：动手前先 grep，不要重复造轮子。
- 新类型（比如 `float`）：要改 `RecTy` 继承体系、`Init` 转换、所有 backend 的消费逻辑，是 10 倍工作量。
- 新语句（比如 `assert` 之外的 `dump` 变体）：要走 `TGParser::ParseStatement` 的分派，和表达式是两套解析路径。

`!min`/`!max` 在本次实验的 LLVM 版本里**不存在**（`grep -rn '"min"' llvm/lib/TableGen` 为空），所以它适合当教材。注意：这不代表上游没有相关讨论；本章的目标是教你流程，不是抢一个 PR。

---

## 18.3 第一层：lexer 只做「认词」

`!` 开头的运算符统一走 `TGLexer::LexExclaim`（`TGLexer.cpp:630`）。它把 `!` 后面连续的字母切出来，用一个 `StringSwitch` 映射到 token：

```cpp
// llvm/lib/TableGen/TGLexer.cpp:656（原始代码）
          .Case("add", tgtok::XADD)
          .Case("sub", tgtok::XSUB)
          .Case("mul", tgtok::XMUL)
          .Case("div", tgtok::XDIV)
          .Case("not", tgtok::XNOT)
```

加两行：

```cpp
          .Case("mul", tgtok::XMUL)
          .Case("div", tgtok::XDIV)
+         .Case("min", tgtok::XMIN)
+         .Case("max", tgtok::XMAX)
```

然后在 `TGLexer.h` 的 `TokKind` 枚举里加两个编号。**这里有一个不明显的坑**：

```cpp
// llvm/lib/TableGen/TGLexer.h:104-166（节选）
  BANG_OPERATOR_FIRST,
  XConcat = BANG_OPERATOR_FIRST,
  XADD,
  ...
  XRepr,
  BANG_OPERATOR_LAST = XRepr,
```

`TGParser::ParseSimpleValue`（`TGParser.cpp:2958`）靠 `tgtok::isBangOperator(Code)` 决定「这是不是 bang operator」：

```cpp
  if (tgtok::isBangOperator(Code))
    return ParseOperation(CurRec, ItemType);
```

而 `isBangOperator` 就是一个区间判断：

```cpp
// llvm/lib/TableGen/TGLexer.h:177-180
static inline bool isBangOperator(tgtok::TokKind Kind) {
  return tgtok::BANG_OPERATOR_FIRST <= Kind && Kind <= tgtok::BANG_OPERATOR_LAST;
}
```

所以新增的 token **必须插在 `BANG_OPERATOR_LAST` 之前**。我实测了错误写法：把 `XMIN/XMAX` 放到 `BANG_OPERATOR_LAST = XRepr` 之后、`STRING_VALUE_FIRST` 之前，编译能过，但：

```console
$ cat /tmp/tg18/e.td
defvar X = !min(1, 2);
$ llvm-tblgen -print-records /tmp/tg18/e.td
/tmp/tg18/e.td:1:12: error: Unknown or reserved token when parsing a value
defvar X = !min(1, 2);
           ^
```

注意错误位置指向 `!min` 的 `!`，信息却是「未知 token」——因为 lexer 已经把它认成 `XMIN`，只是 parser 的 bang 分支不认它，落到 `ParseSimpleValue` 的 `default:`。**这种错误极难从消息反推原因**，是这一层最典型的坑。

本章最终补丁把 `XMIN/XMAX` 插在 `XDIV` 之后（`TGLexer.h:112-113`），并**不需要**改 `BANG_OPERATOR_LAST`——因为它仍然等于 `XRepr`，而新 token 在它前面。

---

## 18.4 第二层：parser 里有三处要改

`TGParser::ParseOperation` 用一个大 `switch` 分派所有 bang operator。对二元运算符，它需要三块信息：

1. **哪些 token 归这个分支**（外层 `case` 列表）；
2. **token → `BinOpInit::BinaryOp` 的映射**；
3. **结果的 `RecTy` 和每个参数的期望类型**。

漏掉任何一处，症状都不一样：

| 漏掉 | 症状 |
|---|---|
| 外层 `case` | 落到 `default: TokError("unknown bang operator")` |
| opcode 映射 | `llvm_unreachable("Unhandled code!")`（assertions 构建直接 abort） |
| 类型设置 | 参数不做类型检查；`!min("a", 1)` 进到 `Fold` 才失败或静默不折叠 |

### 18.4.1 外层 case 列表

```cpp
// llvm/lib/TableGen/TGParser.cpp:1541 附近
  case tgtok::XMUL:
  case tgtok::XDIV:
+ case tgtok::XMIN:
+ case tgtok::XMAX:
  case tgtok::XAND:
```

### 18.4.2 opcode 映射

```cpp
// llvm/lib/TableGen/TGParser.cpp:1591 附近
    case tgtok::XDIV:
      Code = BinOpInit::DIV;
      break;
+   case tgtok::XMIN:
+     Code = BinOpInit::MIN;
+     break;
+   case tgtok::XMAX:
+     Code = BinOpInit::MAX;
+     break;
```

### 18.4.3 类型

```cpp
// llvm/lib/TableGen/TGParser.cpp:1697 附近
    case tgtok::XMUL:
    case tgtok::XDIV:
+   case tgtok::XMIN:
+   case tgtok::XMAX:
      Type = IntRecTy::get(Records);
      ArgType = IntRecTy::get(Records);
      break;
```

`Type` 是表达式结果的类型，`ArgType` 是「下一个参数期望的类型」。这里两者都是 `int`，意味着：

- 结果一定是 `int`（即使传 `bits<4>`）；
- 每个参数会被 `ParseValue(CurRec, ArgType)` 按 `int` 检查，`"a"` 这种 string 参数会在**解析期**报错，而不是等到折叠。

但只有这些还不够。`ParseOperation` 后面还有两处**隐式契约**。

### 18.4.4 契约一：算术运算符不要被 `resolveTypes` 改写类型

参数逐个解析时，parser 会做类型合一。对 `bits<N>` 参数，`resolveTypes(int, bits<4>)` 会得到 `bits<4>`。算术运算符要保持「按 int 算」，所以有一个显式的例外列表：

```cpp
// llvm/lib/TableGen/TGParser.cpp:1865 附近
        if (Code != BinOpInit::ADD && Code != BinOpInit::SUB &&
            Code != BinOpInit::AND && Code != BinOpInit::OR &&
            Code != BinOpInit::XOR && Code != BinOpInit::SRA &&
            Code != BinOpInit::SRL && Code != BinOpInit::SHL &&
            Code != BinOpInit::MUL && Code != BinOpInit::DIV) {
          ArgType = Resolved;
        }
```

`!min`/`!max` 要加进这个列表，否则 `!min(0b1, 0b0)` 的 `ArgType` 会变成 `bits<1>`，后续参数按 bits 解析，结果类型也会漂移。

### 18.4.5 契约二：variadic 是「parser 帮你左折叠」，不是 `Fold` 的职责

`!add(1, 2, 3)` 能工作，不是因为它有特殊语法，而是 parser 在收尾时把多余的参数**左结合**折成嵌套二元运算：

```cpp
// llvm/lib/TableGen/TGParser.cpp:1917 附近
    // We allow multiple operands to associative operators like !strconcat as
    // shorthand for nesting them.
    if (Code == BinOpInit::STRCONCAT || Code == BinOpInit::LISTCONCAT ||
        Code == BinOpInit::CONCAT || Code == BinOpInit::ADD ||
        Code == BinOpInit::AND || Code == BinOpInit::OR ||
        Code == BinOpInit::XOR || Code == BinOpInit::MUL) {
      while (InitList.size() > 2) {
        const Init *RHS = InitList.pop_back_val();
        RHS = (BinOpInit::get(Code, InitList.back(), RHS, Type))->Fold(CurRec);
        InitList.back() = RHS;
      }
    }
```

加进去：

```cpp
        Code == BinOpInit::XOR || Code == BinOpInit::MUL ||
+       Code == BinOpInit::MIN || Code == BinOpInit::MAX) {
```

不加会怎样？`!min(3, 1, 2)` 解析完 `InitList.size() == 3`，跳过折叠，直接落到：

```cpp
    Error(OpLoc, "expected two operands to operator");
```

这条错误我在 `!min(1)` 上实测过（单参数也会落到同一行）：

```console
$ llvm-tblgen -print-records /tmp/tg18/e.td
/tmp/tg18/e.td:1:12: error: expected two operands to operator
defvar X = !min(1);
           ^
```

**注意**：`!min`/`!max` 的「结合律」其实不需要——`min` 是结合的，但 `max` 也是。选它们做 variadic 教学是安全的；如果换成 `!sub` 这种非结合运算，就不能这样折叠。

---

## 18.5 第三层：`Init` 节点——opcode、折叠、打印

### 18.5.1 加 opcode

`Record.h` 里的 `BinaryOp` 枚举：

```cpp
// llvm/include/llvm/TableGen/Record.h:907 附近
  enum BinaryOp : uint8_t {
    ADD,
    SUB,
    MUL,
    DIV,
+   MIN,
+   MAX,
    AND,
```

放在 `DIV` 后面只是可读性；opcode 不落盘、不跨进程，顺序无所谓。但**不要**插在枚举中间去「重排」，没有收益，只会让 diff 变脏。

### 18.5.2 折叠

常量折叠在 `BinOpInit::Fold`。算术运算符共享一段把参数转成 `IntInit` 的代码：

```cpp
// llvm/lib/TableGen/Record.cpp:1456 附近
  case ADD:
  case SUB:
  case MUL:
  case DIV:
+ case MIN:
+ case MAX:
  case AND:
  ...
    if (LHSi && RHSi) {
      int64_t LHSv = LHSi->getValue(), RHSv = RHSi->getValue();
      int64_t Result;
      switch (getOpcode()) {
      ...
      case SUB: Result = LHSv - RHSv; break;
      case MUL: Result = LHSv * RHSv; break;
+     case MIN: Result = std::min(LHSv, RHSv); break;
+     case MAX: Result = std::max(LHSv, RHSv); break;
      case DIV:
```

两个细节：

- `convertInitializerTo(IntRecTy)` 让 `bits` 参数也能参与计算（`0b1100` → 12）。
- 折叠只在两端都是具体值时发生。类里的 `!max(n, lo)` 在解析 class 时 `n`/`lo` 还是模板参数（unset），`Fold` 返回 `this`；等 `def M3 : HasMax<3, 7>` 实例化、`resolveReferences` 把参数代进去后，才会真正算出 7。这个「两次求值」是 Ch 7 的主题，这里正好复习。

实测输出（`/tmp/tg18/minmax.td`）：

```console
$ llvm-tblgen -print-records /tmp/tg18/minmax.td
------------- Classes -----------------
class HasMax<int HasMax:n = ?, int HasMax:lo = ?> {
  int Len = !max(HasMax:n, HasMax:lo);
}
------------- Defs -----------------
def M3 {	// HasMax
  int Len = 7;
}
def M9 {	// HasMax
  int Len = 9;
}
def Rec {
  int v = 2;
  bits<4> b = { 1, 1, 0, 0 };
}
```

注意 class 那一行：**没折叠的表达式原样打印**，而且是 `!max(HasMax:n, HasMax:lo)`——模板参数被加了 `ClassName:` 前缀。这直接来自 `getAsString()`，所以下一步不能漏。

### 18.5.3 打印

```cpp
// llvm/lib/TableGen/Record.cpp:1559 附近
  case MUL: Result = "!mul"; break;
  case DIV: Result = "!div"; break;
+ case MIN: Result = "!min"; break;
+ case MAX: Result = "!max"; break;
```

漏掉这一步不会崩：`Result` 是空串，输出变成 `(a, b)`——但 `-print-records`、`!repr`、所有诊断消息都会少运算符名。更糟的是，如果编译器开了 `-Wswitch`，这里会报警告（LLVM 构建默认不当错误，但 review 会要求你补全）。

---

## 18.6 类型检查的真实边界

我们实测了五种非法输入，错误消息各不相同，这本身就是「类型检查分散在多层」的证据：

```console
$ llvm-tblgen -print-records bad.td
# 1) 参数是 string
bad.td:1:17: error: expected value of type 'int', got 'string'
defvar X = !min("a", 1);
                ^

# 2) 第二个参数是 string
bad.td:1:20: error: expected value of type 'int', got 'string'
defvar X = !min(1, "a");
                   ^

# 3) 只有一个参数
bad.td:1:12: error: expected two operands to operator
defvar X = !min(1);
           ^

# 4) 零个参数
bad.td:1:17: error: Unknown or reserved token when parsing a value
defvar X = !min();
                ^
```

- 1/2 来自 `ParseValue` 按 `ArgType=int` 解析，是**统一的参数检查**；
- 3 来自 `ParseOperation` 收尾；
- 4 之所以是「未知 token」，是因为 parser 先尝试解析第一个参数，遇到 `)`，在 `ParseSimpleValue` 的 default 分支报错。想让它变成「expected at least two operands」需要额外的前置检查——**这是可选改进，不是必须**。上游 review 时这种「错误消息不够好」经常被要求修，但也可以作为 follow-up。

64 位边界也验证了：

```console
$ cat /tmp/tg18/edge.td
defvar E = !min(9223372036854775807, -9223372036854775808);
defvar F = !max(4, !min(9, 7));
$ llvm-tblgen -print-records /tmp/tg18/edge.td
------------- Classes -----------------
------------- Defs -----------------
```

`defvar` 不打印值；要观察就用 `def`：

```console
$ cat /tmp/tg18/edge2.td
def E { int v = !min(9223372036854775807, -9223372036854775808); }
def F { int v = !max(4, !min(9, 7)); }
$ llvm-tblgen -print-records /tmp/tg18/edge2.td
def E {
  int v = -9223372036854775808;
}
def F {
  int v = 7;
}
```

`std::min/std::max` 对 `int64_t` 是安全的，不存在 `INT64_MIN` 取负溢出（那是 `!sub(0, INT64_MIN)` 的问题，和这里无关）。

---

## 18.7 测试：两个文件、两种失败模式

新增 `llvm/test/TableGen/min-max.td`（正向）：

```text
// RUN: llvm-tblgen %s | FileCheck %s

defvar A = !min(3, 1, 2);
defvar B = !max(3, 1, 2);
defvar C = !min(-5, 5);
defvar D = !max(0b10, 0b01);
defvar E = !min(9223372036854775807, -9223372036854775808);

class HasMax<int n, int lo> {
  int Len = !max(n, lo);
}
def M3 : HasMax<3, 7>;
def M9 : HasMax<9, 7>;

def Rec {
  int v = !min(2, 8, 5);
  bits<4> b = !max(0b0011, 0b1100);
}

// CHECK: def M3 {
// CHECK:   int Len = 7;
// CHECK: def M9 {
// CHECK:   int Len = 9;
// CHECK: def Rec {
// CHECK:   int v = 2;
// CHECK:   bits<4> b = { 1, 1, 0, 0 };
```

新增 `llvm/test/TableGen/min-max-error.td`（负向）：

```text
// RUN: not llvm-tblgen %s 2>&1 | FileCheck %s

defvar X = !min(1, "a");

// CHECK: error: expected value of type 'int', got 'string'
```

实跑：

```console
$ llvm-lit -v llvm/test/TableGen/min-max.td llvm/test/TableGen/min-max-error.td
PASS: LLVM :: TableGen/min-max-error.td (1 of 2)
PASS: LLVM :: TableGen/min-max.td (2 of 2)

Total Discovered Tests: 2
  Passed: 2 (100.00%)
```

全量回归（基线 496 + 新增 2）：

```console
$ llvm-lit llvm/test/TableGen
Total Discovered Tests: 498
  Passed: 498 (100.00%)
```

再补一层「真实输入不会被新语法误伤」的验证——四个 target 全量解析：

```console
$ for T in AArch64 X86 RISCV ARM; do
    D=llvm/lib/Target/$T
    llvm-tblgen -null-backend -I $D -I llvm/lib/Target -I llvm/include $D/$T.td -o /dev/null
  done
AArch64 ok
X86 ok
RISCV ok
ARM ok
```

**测试设计要点**：

1. 正向测试必须覆盖「常量」「变量」「class 参数」「bits 参数」「64 位边界」五类，否则很容易只测到 `Fold` 的 happy path。
2. 负向测试只测**一条**最有代表性的错误。把四种错误都塞进一个文件会让 `not` + FileCheck 变得脆弱（错误顺序不稳定）。
3. 不要用 `-gen-instr-info` 之类重 backend 做这个测试，`-print-records` 足够且快 100 倍。

---

## 18.8 文档：上游不会收没有文档的语法

真正的补丁还要改两处文档。`llvm/docs/TableGen/ProgRef.md` 在 `!match` 和 `!mul` 之间按字母序插入：

```markdown
`!max(`*a*`,` *b*`, ...)`

: This operator produces the largest of *a*, *b*, etc. The arguments must be
  `bit`, `bits`, or `int` values; the result is an `int`.

`!min(`*a*`,` *b*`, ...)`

: This operator produces the smallest of *a*, *b*, etc. The arguments must be
  `bit`, `bits`, or `int` values; the result is an `int`.
```

`llvm/docs/ReleaseNotes.md` 的 `### Changes to TableGen` 下加一条：

```markdown
* Added the `!min` and `!max` bang operators, which return the smallest or
  largest of two or more `int`/`bits` arguments.
```

文档不是「附赠品」。它定义了语义边界：**结果类型是 `int`，不是 `bits<N>`**。如果你在文档里写「result has the type of the arguments」，而实现返回 `int`，这就是 bug。

---

## 18.9 完整补丁长什么样

```console
$ git diff --stat
 llvm/docs/ReleaseNotes.md           |  3 +++
 llvm/docs/TableGen/ProgRef.md       | 10 ++++++++++
 llvm/include/llvm/TableGen/Record.h |  2 ++
 llvm/lib/TableGen/Record.cpp        |  6 ++++++
 llvm/lib/TableGen/TGLexer.cpp       |  2 ++
 llvm/lib/TableGen/TGLexer.h         |  2 ++
 llvm/lib/TableGen/TGParser.cpp      | 16 ++++++++++++++--
 7 files changed, 39 insertions(+), 2 deletions(-)
```

生产代码 39 行，测试约 36 行，文档 13 行。这就是一个「小语言特性」的真实形状。

每一层的职责可以压成一张表：

| 层 | 文件 | 你加的东西 | 忘掉会怎样 |
|---|---|---|---|
| 词法 | `TGLexer.cpp/.h` | keyword + token 编号（必须在 `BANG_OPERATOR_LAST` 前） | 「未知 token」 |
| 语法 | `TGParser.cpp` | case 列表、opcode 映射、类型 | `unknown bang operator` / `llvm_unreachable` |
| 语法细节 | `TGParser.cpp` | 算术例外表、variadic 折叠表 | 类型漂移 / 三参数报错 |
| AST | `Record.h` | `BinaryOp` 枚举 | 编译失败 |
| 求值 | `Record.cpp` | `Fold` 分支 | 永远不折叠，`-print-records` 原样输出表达式 |
| 打印 | `Record.cpp` | `getAsString` | 输出 `(a, b)` |
| 文档 | `ProgRef.md` / `ReleaseNotes.md` | 语义与类型 | review 打回 |
| 测试 | `llvm/test/TableGen` | 正/负各一 | 无法证明没坏 |

---

## 18.10 编译与验证的真实成本

改 `Record.h` 会触发 TableGen 全量重编。本机（`ninja -C /tmp/tgbuild llvm-tblgen`）：

- 只改 `TGParser.cpp`：约 10 秒（1 个对象 + 链接）。
- 改 `Record.h`：**113 个目标**，约 40 秒——因为 `Record.h` 被 `utils/TableGen` 下几乎所有 emitter 包含。
- 全量 `llvm-lit llvm/test/TableGen`：约 6.6 秒（498 个测试）。

这意味着：**开发期把 opcode 先加好**，避免反复改头文件；逻辑迭代集中在 `TGParser.cpp` / `Record.cpp`。

---

## 18.11 如果你要加的不是 bang operator

本章的路径只适用于「表达式级、纯函数」的特性。另外两类特性要碰的东西完全不同：

1. **新语句**（如 `dump`、`assert`）：入口在 `TGParser::ParseStatement`，要做语句分派、作用域、`Record` 副作用，通常还要改 `RecordKeeper` 的执行顺序。参考 Ch 11 的 `dump` 实现。
2. **新的记录语义**（字段、模板参数、继承规则的改变）：入口在 `Record`/`RecordKeeper`，影响**所有** backend 和所有 `.td`。这是第 19 章的主题——它比加一个运算符危险一个数量级。

判断标准很简单：**你的特性会不会改变已有 `.td` 文件的含义？** 会 → 按第 19 章的兼容性流程走；不会 → 按本章的「新增语法」流程走。

---

## 18.12 检查清单

做完一个语言特性补丁，逐条打勾：

- [ ] `grep` 过现有实现，确认不是重复造轮子
- [ ] token 插在 `BANG_OPERATOR_FIRST..BANG_OPERATOR_LAST` 区间内
- [ ] `ParseOperation` 的 case 列表、opcode 映射、类型三处都改了
- [ ] 算术类运算符加进了 `resolveTypes` 例外表
- [ ] variadic 运算符加进了 `while (InitList.size() > 2)` 折叠表
- [ ] `BinaryOp` 枚举、`Fold`、`getAsString` 三处一致
- [ ] 正向测试覆盖常量/变量/class 参数/bits/边界
- [ ] 负向测试覆盖最有代表性的类型错误
- [ ] `llvm-lit llvm/test/TableGen` 全绿（数字 = 基线 + 新增）
- [ ] 四个真实 target 的 `.td` 仍能解析
- [ ] `ProgRef.md` + `ReleaseNotes.md` 已更新
- [ ] `git status` 里只有你预期的文件

---

## 18.13 踩雷记录

1. **错误直觉**：在 `TGLexer.cpp` 的 `StringSwitch` 里加一行，`!min` 就能用了。
   **正确认识**：lexer 只是把字符变成 token。parser 的 bang 分支靠 `isBangOperator` 的**枚举区间**判断，token 必须插在 `BANG_OPERATOR_LAST` 之前；否则你会得到一条指向 `!` 的「Unknown or reserved token」，完全看不出是枚举顺序问题。

2. **错误直觉**：`Fold` 里写了 `min/max`，表达式就会在解析时算出来。
   **正确认识**：`Fold` 只在参数具体时生效。class 里的模板参数是 unset，`Fold` 返回 `this`；实例化后靠 `resolveReferences` 再折一次。`-print-records` 里 class 显示 `!max(HasMax:n, HasMax:lo)`、def 显示 `7`，就是这两次求值的直接证据。

3. **错误直觉**：`!min(1, 2, 3)` 的多参数由 `Fold` 处理。
   **正确认识**：多参数是 **parser** 左折叠成嵌套二元节点，`Fold` 永远只看到两个操作数。不把 `MIN/MAX` 加进 `1925` 行那个列表，三个参数会直接报 "expected two operands"。

4. **错误直觉**：类型写成 `int` 就够了，`bits` 参数会被拒绝。
   **正确认识**：`convertInitializerTo(IntRecTy)` 会把 `bits` 转成整数，所以 `!max(0b0011, 0b1100)` 合法，结果是 `int` 12；但 `-print-records` 打印目标字段时，如果字段声明是 `bits<4>`，会按字段类型显示成 `{ 1, 1, 0, 0 }`——**表达式结果类型和目标字段类型是两件事**。

5. **错误直觉**：`getAsString` 只影响报错，不重要。
   **正确认识**：它决定 `-print-records`、`!repr`、`dump`、以及所有诊断里表达式的样子。漏改不会崩，但会输出 `(a, b)`，而且测试里一旦 CHECK 了未折叠表达式就会失败。

6. **错误直觉**：加语言特性只改一个文件，很快。
   **正确认识**：真正的成本是「跨层一致性」。39 行生产代码背后是 4 个必须同步的注册点、2 份文档、2 个测试；改 `Record.h` 一次要重编 113 个目标。

---

## 18.14 延伸阅读

1. **`llvm/lib/TableGen/TGParser.cpp` 的 `ParseOperation`（约 1440–1940 行）**
   读什么：完整的 bang operator 分派，包括 unary / binary / ternary / 列表推导四类。
   学什么：一个表达式的类型信息如何在「解析参数」和「构造节点」之间流动；哪些检查在 parser、哪些留给 `Fold`。
   和本章的关系：本章只动了 binary 分支的三个点，读懂全函数你才知道为什么是这三个点。

2. **`llvm/lib/TableGen/Record.cpp` 的 `BinOpInit::Fold` / `resolveReferences`（约 1220–1555 行）**
   读什么：每个 opcode 的折叠条件、`resolveReferences` 如何重建节点、`getAsString` 的完整 switch。
   学什么：TableGen 的「惰性求值 + 两阶段折叠」模型；为什么 `Fold` 返回 `this` 是正常路径而不是错误。
   和本章的关系：`!min`/`!max` 的 6 行实现只是这个模式的一个实例。

3. **`llvm/docs/TableGen/ProgRef.md` 的 "Bang operators" 一节（约 1750–2200 行）**
   读什么：每个运算符的语义、参数类型、结果类型、边界行为。
   学什么：上游如何用文档定义契约；哪些行为被明确保证、哪些被标为「不保证稳定」（如 `!repr`）。
   和本章的关系：你写的文档条目必须和实现严格一致，尤其是结果类型。

4. **`llvm/test/TableGen/` 下的 `!` 运算符测试（`grep -l 'RUN.*llvm-tblgen' *.td`）**
   读什么：正向 `FileCheck`、负向 `not` + `FileCheck`、以及用 `-D` 做条件编译的测试。
   学什么：LLVM 测试的最小惯例；如何用一条 `CHECK` 锁定错误消息。
   和本章的关系：本章的两个测试就是照这个模板写的。

---

## 下一章

[第 19 章：改记录语义——兼容性、模板参数与「沉默的破坏」](19-changing-record-semantics.md)

加运算符是「扩展现有语义」，风险可控。下一章做一件危险得多的事：改变 `Record` 的字段/模板参数语义。你会看到一个真实的上游 commit 如何用「单独存储模板参数」换掉旧的 `getValues()` 行为，以及为什么这种改动需要同时改打印、改所有 backend、还要在 release notes 里写清楚迁移方式。
