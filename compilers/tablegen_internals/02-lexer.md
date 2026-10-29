# Ch 2 词法层：TGLexer 如何切出 token

> **本章目标**：读完 `TGLexer`（1100 行 + 484 行头文件）后，能解释每个 token 的产生条件；知道 lexer 里藏着一个预处理器、一个 include 栈、和一个 code fragment 的特殊扫描；能手写一条新 token 并跑通。

> **环境**：LLVM `24.0.0git`，Release（无 assertions）。

---

## 2.1 先看 token 的全集

在读代码前先建立地图。`llvm/lib/TableGen/TGLexer.h:30` 的 `tgtok::TokKind` 就是 TableGen 词法层能产出的**全部**符号。它按语义分了七组：

```cpp
enum TokKind {
  // 1. 标记
  Eof, Error,

  // 2. 无信息标点
  minus, plus, l_square, r_square, l_brace, r_brace, l_paren, r_paren,
  less, greater, colon, semi, comma, dot, equal, question, paste, dotdotdot,

  // 3. 字面量
  TrueVal, FalseVal, IntVal, BinaryIntVal,

  // 4. 预处理器内部 token（永远不会由 Lex() 返回）
  Ifdef, Ifndef, Else, Endif, Define,

  // 5. 关键字
  Bit, Bits, Code, Dag, ElseKW, Field, In, Include, Int, List, String, Then,

  // 6. 对象起始（Parser 用来分派）
  Assert, Class, Def, Defm, Defset, Deftype, Defvar, Dump, Foreach, If, Let, MultiClass,

  // 7. bang 操作符（!add, !if, ...）
  XConcat, XADD, XSUB, XMUL, XDIV, XNOT, XLOG2, XAND, XOR, XXOR, XSRA, XSRL, XSHL, ...

  // 8. 带字符串值的 token
  Id, StrVal, VarName, CodeFragment,
};
```

七个分组各自有配套的判别函数（同文件末尾）：

```cpp
static inline bool isBangOperator(tgtok::TokKind Kind) {
  return tgtok::BANG_OPERATOR_FIRST <= Kind && Kind <= BANG_OPERATOR_LAST;
}
static inline bool isObjectStart(tgtok::TokKind Kind)  { ... }
static inline bool isStringValue(tgtok::TokKind Kind)  { ... }
```

**这三个函数是整个 parser 的分派依据**，记住它们比记住 100 个 token 名字有用。

> 注意第 7 组的命名：`XOR` 是 `!or`、`XXOR` 是 `!xor`。这个 `X` 前缀代表 "eXclaim"（`!`），`tgtok::Xor` 会与 C++ 的 `or` 关键字冲突，所以加了前缀 `X`。第一次读很容易看错。

## 2.2 主循环：不是 DFA，是一个大 `switch`

`TGLexer::LexToken()`（`TGLexer.cpp:150` 起）用一个 `while` 包住一个 `switch (CurChar)`，每个分支要么 `return` 一个 token，要么 `break` 继续吞掉当前字符。骨架：

```cpp
tgtok::TokKind TGLexer::LexToken(bool FileOrLineStart) {
  TokStart = CurPtr;
  while (true) {
    TokStart = CurPtr;
    CurChar = getNextChar();
    switch (CurChar) {
    case 0:                          // EOF
      if (CurPtr-1 != CurBuf.end()) break;   // 内嵌 '\0'，当普通字符
      return tgtok::Eof;
    case ';': return tgtok::semi;
    case ':': return tgtok::colon;
    ...
    case '#':
      if (FileOrLineStart) {
        tgtok::TokKind Kind = prepIsDirective();
        if (Kind != tgtok::Error) return lexPreprocessor(Kind);
      }
      return tgtok::paste;
    case '.':
      if (peekNextChar(0) == '.') {
        ++CurPtr;
        if (peekNextChar(0) == '.') { ++CurPtr; return tgtok::dotdotdot; }
        return ReturnError(TokStart, "invalid '..' punctuation");
      }
      return tgtok::dot;
    case ' ': case '\t': break;      // 跳过
    case '\n': FileOrLineStart = true; break;
    case '/':
      if (*CurPtr == '/') SkipBCPLComment();
      else if (*CurPtr == '*') { if (SkipCComment()) return tgtok::Error; }
      else return ReturnError(TokStart, "unexpected character");
      break;
    case '-': case '+': case '0' ... case '9': { ... 见 2.4 ... }
    case '"':  return LexString();
    case '$':  return LexVarName();
    case '[':  return LexBracket();
    case '!':  return LexExclaim();
    }
  }
}
```

几个立刻能看出来的设计选择：

1. **`TokStart` 在每个循环开头被重置**。因为注释和空白会让一次 `LexToken` 消费多个「逻辑片段」（比如 `/* c */ def`），此时 token 的位置必须是 `def` 而不是注释起点。
2. **`FileOrLineStart` 是 `#` 的歧义消除器**。`#` 在 TableGen 里有两个身份：paste 运算符（`foo#bar`）和预处理指令引导符（行首 `#ifdef`）。判据是「这个 `#` 是否在文件/行首」。
3. **`\r` 是 `llvm_unreachable`**：`getNextChar()` 已经把 `\r` 规范成 `\n`。所以 lexer 里永远不会看到 `\r`。
4. **`case '.'` 手工处理三连点**：因为 `0...2` 的 `...` 是区间运算符，不能靠「每个字符一个 token」拼出来。

## 2.3 字符到 token：容易被忽略的细节

### 2.3.1 注释

```cpp
void TGLexer::SkipBCPLComment() {
  ++CurPtr;                                     // 跳过第二个 /
  auto EOLPos = CurBuf.find_first_of("\r\n", CurPtr - CurBuf.data());
  CurPtr = (EOLPos == StringRef::npos) ? CurBuf.end() : CurBuf.data() + EOLPos;
}
```

`//` 注释简单：找行尾。但 `/* */` 有个特别之处——**支持嵌套**：

```cpp
bool TGLexer::SkipCComment() {
  ++CurPtr;                    // 跳过 *
  unsigned CommentDepth = 1;
  while (true) {
    int CurChar = getNextChar();
    switch (CurChar) {
    case EOF: PrintError(TokStart, "unterminated comment"); return true;
    case '*':
      if (*CurPtr == '/') { --CommentDepth; ++CurPtr;
                            if (CommentDepth == 0) return false; }
      break;
    case '/':
      if (*CurPtr == '*') { ++CommentDepth; ++CurPtr; }
      break;
    }
  }
}
```

注释里还写了「与 C 不同，我们允许嵌套」。实测确认：

```td
/* outer /* inner */ still comment */
def A { int X = 1; }
```

```console
$ llvm-tblgen -print-records n1.td
------------- Classes -----------------
------------- Defs -----------------
def A {
  int X = 1;
}
```

如果这是 C 风格，`still comment */` 会被当成代码，直接语法错误。**这是 TableGen 与 C 的一个有意差异**，读 AArch64 那些上万行的 `.td` 时会遇到。

### 2.3.2 字符串：只有五种转义

```cpp
switch (*CurPtr) {
case '\\': case '\'': case '"':  CurStrVal += *CurPtr++; break;
case 't':  CurStrVal += '\t'; ++CurPtr; break;
case 'n':  CurStrVal += '\n'; ++CurPtr; break;
case '\r': return ReturnError(CurPtr, "escaped newlines not supported in tblgen");
case '\0': if (CurPtr == CurBuf.end())
             return ReturnError(StrStart, "end of file in string literal");
           [[fallthrough]];
default:   return ReturnError(CurPtr, "invalid escape in string literal");
}
```

**没有 `\x`、`\0`、`\r`、`\u`**。实测：

```console
$ printf 'def A { string S = "a\\qb"; }\n' > n4.td && llvm-tblgen n4.td
n4.td:1:23: error: invalid escape in string literal
def A { string S = "a\qb"; }
                      ^
```

注意错误箭头指在 `q` 上——`ReturnError(CurPtr, ...)` 用的是**当前字符**的位置，不是字符串起点。这种细节决定了诊断的可用性，Ch 12 会系统讨论。

还有一个容易踩的：

```console
$ printf 'def A { string S = "abc;\n' > n5.td && llvm-tblgen n5.td
n5.td:1:21: error: end of line in string literal
def A { string S = "abc;
                    ^
```

**字符串不能跨行**（`\r`/`\n` 都直接报错）。想写多行字符串只能用 `!strconcat` 拼，或者在 `code` 里写。

### 2.3.3 `[{ ... }]`：code fragment 是单独的扫描器

```cpp
tgtok::TokKind TGLexer::LexBracket() {
  if (CurPtr[0] != '{') return tgtok::l_square;   // 普通 '['
  ++CurPtr;
  const char *CodeStart = CurPtr;
  while (true) {
    int Char = getNextChar();
    if (Char == EOF) break;
    if (Char != '}') continue;
    Char = getNextChar();
    if (Char == EOF) break;
    if (Char == ']') {
      CurStrVal.assign(CodeStart, CurPtr - 2);
      return tgtok::CodeFragment;
    }
  }
  return ReturnError(CodeStart - 2, "unterminated code block");
}
```

它的规则极其简单：**找第一个 `}]`**。不配对花括号、不识别字符串、不识别注释。所以：

```td
def A { code C = [{ if (x) { return 1; } }]; }
```

能过，因为 `}]` 在 `return 1; } }]` 中的最后两个字符。但如果 code 里有字符串 `"}]"`，会提前截断：

```td
def A { code C = [{ printf("}]"); }]; }
```

这是**已知限制**，不是 bug——`code` 字段的内容对 TableGen 完全不透明，它只负责原样存进 `StringInit`。上游的态度是「别在 code 里写 `}]`」。Ch 5 会看到 `CodeInit` 其实就是 `StringInit` 的别名。

### 2.3.4 `$` 只有一个用途

`case '$': return LexVarName();`——这看起来像是「变量」语法，但实测：

```console
$ printf 'defvar $x = 5;\n' > v2.td && llvm-tblgen v2.td
v2.td:1:8: error: expected identifier
```

```console
$ printf 'foreach $i = 0...2 in {\n  def E$i { int V = $i; }\n}\n' > n6.td && llvm-tblgen n6.td
n6.td:1:9: error: Expected identifier in foreach declaration
```

真正用途只有一处：**DAG 参数名**：

```td
def op;
class C<dag d> { dag D = d; }
def X : C<(op:$o 1:$first, 2:$second)>;
```

```console
$ llvm-tblgen -print-records v6.td
def X {	// C
  dag D = (op:$o 1:$first, 2:$second);
}
```

`$first` 不是变量引用，是 **DAG 参数的名字标签**（对应 `DagInit` 的 `ArgNames`）。这在 LLVM 的 target 定义里大量使用，例如 `(add:$dst GPR:$lhs, GPR:$rhs)`——`$dst` 就是给结果起的名字，供 pattern 引用。

> 所以 `$` 是「DAG 参数名引导符」，不是「变量名引导符」。这个误解很常见，因为 `$` 在 shell/Perl/PHP 里都是变量。

## 2.4 数字：三种进制，一个歧义

`case '-': case '+': case '0'...'9'` 那段有 40 行，逻辑是：

1. 如果当前是数字，先向前看：跳过连续数字，如果下一个是 `x`/`b` 且再下一个是合法数字，就转 `LexNumber()`。
2. 如果 `NextChar` 是合法标识符起始字符，转 `LexIdentifier()`（**允许标识符以数字开头**，比如 `8i` 这种 paste 产物）。
3. 否则 `LexNumber()`。

第 2 条是为了支持这种写法：

```td
def foo#8i;        // paste 之后变成 foo8i
```

`LexNumber()`（543 行）：

```cpp
if (CurPtr[-1] == '0') {
  NumStart = CurPtr + 1;
  if (CurPtr[0] == 'x') { Base = 16; do ++CurPtr; while (isHexDigit(CurPtr[0])); }
  else if (CurPtr[0] == 'b') { Base = 2; do ++CurPtr; while (CurPtr[0]=='0'||CurPtr[0]=='1'); }
}
...
if (Base == 0) {
  if (!isDigit(CurPtr[0])) {           // 符号后没数字
    if (CurPtr[-1] == '-') return tgtok::minus;
    else if (CurPtr[-1] == '+') return tgtok::plus;
  }
  Base = 10; NumStart = TokStart;
  IsMinus = CurPtr[-1] == '-';
  while (isDigit(CurPtr[0])) ++CurPtr;
}
if (CurPtr == NumStart) return ReturnError(TokStart, "invalid number");
errno = 0;
if (IsMinus) CurIntVal = strtoll(NumStart, nullptr, Base);
else         CurIntVal = strtoull(NumStart, nullptr, Base);
if (errno == EINVAL) return ReturnError(TokStart, "invalid number");
if (errno == ERANGE) return ReturnError(TokStart, "number out of range");
return Base == 2 ? tgtok::BinaryIntVal : tgtok::IntVal;
```

三个关键点：

1. **二进制字面量单独返回 `BinaryIntVal`**，因为它要保留「长度」信息：`0b1010` 是 `bits<4>`，`0b00001010` 是 `bits<8>`，数值都是 10。十进制/十六进制没有这个语义，所以都是 `IntVal`。
2. **`-` 和 `+` 后面没有数字时，退回成 `minus`/`plus` 标点**。这解释了为什么 `a - b` 能工作（减号）而 `-5` 是数字。
3. **`strtoll`/`strtoull` 直接调用，错误靠 `errno`**。实测：

```console
$ printf 'def A { int X = 99999999999999999999; }\n' > big.td && llvm-tblgen big.td
big.td:1:17: error: number out of range
```

`0b102` 这种非法二进制：

```console
$ printf 'def A { int X = 0b102; }\n' > b2.td && llvm-tblgen b2.td
b2.td:1:21: error: expected ';' after declaration
```

注意它是**语法错误**，不是「非法二进制字面量」——因为 lexer 在 `0b10` 处停下（`2` 不是 0/1），`0b10` 成为一个 token，`2` 成为下一个，parser 期望 `;` 却看到 `2`。**错误信息的形状泄露了 lexer 的切分方式**，这是读 TableGen 诊断的基本功。

## 2.5 标识符与关键字：`StringSwitch` 是语法清单

```cpp
tgtok::TokKind TGLexer::LexIdentifier() {
  const char *IdentStart = TokStart;
  while (isValidIDChar(*CurPtr, /*First=*/false)) ++CurPtr;
  StringRef Str(IdentStart, CurPtr - IdentStart);

  tgtok::TokKind Kind = StringSwitch<tgtok::TokKind>(Str)
      .Case("int", tgtok::Int)
      .Case("bit", tgtok::Bit)
      ... 26 个关键字 ...
      .Default(tgtok::Id);

  switch (Kind) {
  case tgtok::Include:
    if (LexInclude()) return tgtok::Error;
    return Lex();                 // include 是「透明」的，递归取下一个 token
  case tgtok::Id:
    CurStrVal.assign(Str.begin(), Str.end());
    break;
  }
  return Kind;
}
```

两个设计点：

1. **关键字是保留字，不是上下文相关**。你不能把字段叫 `list`：

   ```console
   $ printf 'def A { int list = 1; }\n' > kw.td && llvm-tblgen kw.td
   kw.td:1:13: error: Expected identifier in declaration
   ```

2. **`include` 在 lexer 内部展开**。注意 `return Lex()`——不是返回一个 `Include` token 让 parser 处理，而是**当场把被包含文件的内容接到当前流上**。这就是为什么 `include` 在 `.td` 里可以出现在任意位置（甚至在表达式中间），而 parser 完全不知道 include 存在。

### include 的实现细节

```cpp
bool TGLexer::LexInclude() {
  tgtok::TokKind Tok = LexToken();
  if (Tok != tgtok::StrVal) { PrintError(getLoc(), "expected filename after include"); return true; }
  std::string Filename = CurStrVal;
  std::string IncludedFile;

  CurBuffer = SrcMgr.AddIncludeFile(Filename, SMLoc::getFromPointer(CurPtr), IncludedFile);
  if (!CurBuffer) { PrintError(getLoc(), "could not find include file '" + Filename + "'"); return true; }
  Dependencies.insert(IncludedFile);
  CurBuf = SrcMgr.getMemoryBuffer(CurBuffer)->getBuffer();
  CurPtr = CurBuf.begin();
  PrepIncludeStack.emplace_back();
  return false;
}
```

配合 `LexToken` 里的 EOF 处理（113 行附近）：

```cpp
case tgtok::Eof:
  // ... 如果 PrepIncludeStack 非空，说明当前在 include 里，弹回父文件
  SMLoc ParentIncludeLoc = SrcMgr.getParentIncludeLoc(CurBuffer);
  if (ParentIncludeLoc != SMLoc()) {
    if (!prepExitInclude(false)) ...
  }
```

**include 是一个显式的栈**，不是递归调用。所以 include 深度不受 C++ 栈限制，但——Ch 0 那个 segfault 就来自这里：`SrcMgr.AddIncludeFile` 不检查环，`include` 自己会不停入栈直到内存耗尽（实测是 `snprintf` 处崩溃）。

搜索顺序（`SourceMgr::OpenIncludeFile`）：

1. 直接按写出的路径打开（相对当前工作目录）。
2. 依次尝试 `-I` 给的每个目录。
3. 先匹配先赢。实测：

```console
$ mkdir -p inc1 inc2 && echo 'def FromInc1;' > inc1/x.td && echo 'def FromInc2;' > inc2/x.td
$ printf 'include "x.td"\n' > incmain.td
$ llvm-tblgen -Iinc1 -Iinc2 -print-records incmain.td | tail -3
def FromInc1 {
}
```

## 2.6 预处理器：`#ifdef` 不是 C 预处理器

`PreprocessorDirs` 表（`TGLexer.cpp:49`）只有四个指令：`#ifdef`、`#ifndef`、`#else`、`#endif`，加上 `#define`。

判据在 `prepIsDirective()`：`#` 必须在**行首**，且后面紧跟的单词精确匹配，且后面有空白/换行/EOF。所以：

```td
def A { int X = 1; } # commented
```

```console
$ llvm-tblgen pc.td
pc.td:1:22: error: Unexpected token at top level
def A { int X = 1; } # commented
                     ^
```

这里的 `#` 不是行首，所以 lexer 返回的是 paste 运算符；parser 在 `def A` 这个对象结束后遇到 `#`，它不属于任何一种对象起始 token，于是报 `Unexpected token at top level`。**这就是 `#` 双关的实际后果**——如果你想把 `#` 当注释用，它不会给你「注释」的提示，而是「顶层多余 token」。

`#define` 只支持「无值宏」：

```console
$ printf '#define FOO 7\n' > e_pre.td && llvm-tblgen e_pre.td
e_pre.td:1:13: error: only comments are supported after #define NAME
#define FOO 7
            ^
```

要传值宏，只能用命令行 `-D`：

```console
$ printf '#ifdef EXTRA\ndef Y { int V = 1; }\n#endif\ndef Z { int V = 2; }\n' > e_pre2.td
$ llvm-tblgen -D EXTRA -print-records e_pre2.td
def Y { int V = 1; }
def Z { int V = 2; }
```

**这个设计的原因**：TableGen 的宏只用于「这个 target 要不要包含某段 .td」，不需要值替换。真要值参数化，该用 `class` 模板参数。

> `-D` 的实现在 `llvm/lib/TableGen/Main.cpp:57` 的 `MacroNames`，一路传到 `TGParser` 构造函数。上游用它来处理「某些 `.td` 只在特定子目标下编译」这类条件包含。

## 2.7 手写一条新 token（实验）

现在做一个最小的「改源码」实验：给 TableGen 加一个 `!sq`（平方）操作符。这里只做词法层，看它怎么在 parser 处失败——这个失败信息本身就是学习点。

在 `TGLexer.h` 的 `BANG_OPERATOR_FIRST` 之后加：

```cpp
  XConcat = BANG_OPERATOR_FIRST,
  XSQ,            // 新增
  XADD,
```

在 `TGLexer.cpp` 的 `StringSwitch` 里加：

```cpp
      .Case("sq", tgtok::XSQ)
```

重新编译：

```bash
ninja -C build llvm-tblgen
```

（本机增量编译约 1–3 分钟，实测取决于机器。**本章未在本机重跑这一步**，因为 `lib/TableGen` 的修改需要重新链接整个 `llvm-tblgen`；下面描述的是预期行为，标为 guided。）

然后：

```console
$ echo 'def A { int X = !sq(3); }' > sq.td
$ llvm-tblgen sq.td
sq.td:1:17: error: Unknown or reserved token when parsing a value
def A { int X = !sq(3); }
                  ^
```

为什么 lexer 已经认识 `!sq`，parser 还是报错？因为 `ParseValue()` 里的 `switch (Lex.getCode())` 没有 `case tgtok::XSQ`，走到 `default` 分支。**这就是「词法层和语法层必须同步改」的最小证明**——Ch 18 会把这条链路走完（lexer → parser → `Init` 节点 → fold → backend 打印）。

## 2.8 本章检查清单

- [ ] 说出 `tgtok::TokKind` 的七组分类，以及每组的判别函数
- [ ] 解释 `TokStart` 为什么在 while 循环里重置
- [ ] 解释 `#` 的两个身份及区分条件
- [ ] 说出 TableGen 支持的 5 种字符串转义，以及 `\r` 的行为
- [ ] 解释 `0b1010` 与 `10` 在 token 类型上的差异，以及为什么
- [ ] 解释 `[{ ... }]` 的扫描规则和它的已知限制
- [ ] 说出 `$` 的唯一用途，并举一个真实例子
- [ ] 解释 `include` 为什么在 lexer 而不是 parser 里处理
- [ ] 复现 Ch 0 的 include 自包含崩溃，并指出缺的是哪个检查

## 2.9 踩雷记录

**错误直觉 1：`!xor` 的 token 是 `XXOR`，那 `!or` 应该是 `XOR`。**
正确认识：对，但 `XOR` 这个枚举名在 C++ 里会和 `or` 关键字冲突吗？不会——`XOR` 不是关键字。真正容易错的是把 `tgtok::XOR`（`!or`）读成「异或」。`!xor` 是 `tgtok::XXOR`。

**错误直觉 2：注释里写 `*/` 会提前结束。**
正确认识：TableGen 的 `/* */` 支持嵌套。`/* a /* b */ c */` 整段都是注释。这在 C 里是错的，在 TableGen 里是对的。

**错误直觉 3：字符串可以用 `\x41` 表示字符。**
正确认识：只有 `\\`、`\'`、`\"`、`\t`、`\n` 五种。其他一律 `invalid escape in string literal`。

**错误直觉 4：`code` 块里可以随便写 C++，包括 `}]`。**
正确认识：`[{ ... }]` 的扫描就是找第一个 `}]`。字符串里的 `}]` 会提前截断。

**错误直觉 5：`$foo` 是变量引用。**
正确认识：`$` 只用于 DAG 参数名。`foreach $i`、`defvar $x` 都会报错。变量直接用名字（`foreach i = ...`）。

**错误直觉 6：`#define FOO 7` 能定义带值宏。**
正确认识：只支持 `#define NAME`（无值），且注释说得很清楚。值参数化用 `class`，条件包含用 `-D` + `#ifdef`。

**错误直觉 7：`0b102` 会报「非法二进制」。**
正确认识：报的是 `expected ';' after declaration`。因为 lexer 在 `0b10` 停下，`2` 成了下一个 token。读错误信息要看它「在哪断的」。

## 2.10 延伸阅读

- **`llvm/lib/TableGen/TGLexer.h`**（484 行）
  - 先读 `TokKind` 枚举（30–190 行），再读三个 `is*` 判别函数。这是全课的符号表。
- **`llvm/lib/TableGen/TGLexer.cpp`** 的 `LexToken`（150 行起）
  - 主 `switch`。建议边读边用 2.7 的方法加断点/加 token 验证。
- **`llvm/lib/TableGen/TGLexer.cpp`** 的 `prepIsDirective` / `lexPreprocessor`（727 行起）
  - 完整预处理器实现。注意它是**行级**的，不是 token 级的，这也是 `#` 必须行首的原因。
- **`llvm/lib/Support/SourceMgr.cpp`** 的 `OpenIncludeFile`（69 行起）
  - include 搜索顺序的权威实现，也是 Ch 0 崩溃的现场。
- **`llvm/test/TableGen/CStyleComment.td`**
  - 官方对嵌套注释的回归测试。三行 `.td`，配一条 `CHECK`。

## 下一章

Ch 3 进入 parser：12 种顶层语句怎么分派、表达式怎么递归下降、`ParseClassReference` 怎么处理模板参数。你会发现 `TGParser.cpp` 的 4846 行其实只有四类函数。

[Ch 3 语法层：TGParser 的语句分派与表达式解析](./03-parser.md)
