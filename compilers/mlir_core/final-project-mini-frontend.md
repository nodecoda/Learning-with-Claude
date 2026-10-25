# Final Project：写一个 mini frontend，从 `.mfl` 到汇编

> **目标**：把 `arith`、`scf`、`memref`、`func` 和 conversion pipeline 串成一个能编译、能运行、能报错的 C++ 小程序：`.mfl` → MLIR → LLVM dialect → LLVM IR → COFF → 可执行文件。

本章所有终端输出均标 `实测`，来自 `D:\Learning-with-Claude\.scratch\final-mfl`；未验证的平台会明确标 `guided`。

## 需求规格

### 语言语法

语言叫 `.mfl`（mini frontend language），只有 `int` 一种值类型，函数和外部函数都返回 `int`。语法 15 行：

```text
program   := decl+
decl      := "extern" "fn" name "(" params ")" "->" "int" ";"
           | "fn" name "(" params ")" "->" "int" block
params    := [name ":" "int" ("," name ":" "int")*]
block     := "{" stmt* "}"
stmt      := "let" name "=" expr ";" | name "=" expr ";"
           | "if" expr block ("else" block)? | "while" expr block
           | "return" expr ";" | expr ";"
expr      := cmp
cmp       := add (("=="|"!="|"<"|"<="|">"|">=") add)?
add       := mul (("+"|"-") mul)*
mul       := unary (("*"|"/") unary)*
unary     := "-" unary | primary
primary   := INT | name | name "(" args ")" | "(" expr ")"
args      := [expr ("," expr)*]
```

`//` 到行尾是注释；标识符为 `[A-Za-z_][A-Za-z0-9_]*`。没有字符串、数组、布尔字面量或 `else if` 语法糖，需要时写嵌套块。

### 语义

- 所有值是 `i64`；`/` 向零截断，除零与 `INT64_MIN / -1` 是未定义行为。
- `let` 在当前块声明变量；同一块重复声明报错，外层同名变量可被内层遮蔽。
- 赋值只能写已声明变量；参数也是变量，可以赋值。
- `if`/`while` 条件必须是比较表达式（返回 `i1`），不能写 `if x`。
- 函数必须先定义后调用；`extern` 声明供 C ABI 调用，`mfl_main()` 必须存在且无参数。
- `return` 只允许在函数体顶层；本章不支持在 `if`/`while` 中提前返回。
- `print_int(x)` 打印一行并返回 `x`，因此也可作为表达式语句使用。

## 架构与分层

```text
.mfl source
   │
   ▼
Lexer ──▶ vector<Token>（带 line/col）
   │
   ▼
Compiler（递归下降 + 直接生成 MLIR）
   ├─ funcs: name → func.func
   ├─ vars: 作用域栈，name → memref<i64> 槽位
   └─ OpBuilder: 当前插入点
   │
   ▼
func.func + arith + memref + scf
   │  canonicalize → scf→cf → cf→llvm → arith→llvm
   │  → memref→llvm → func→llvm → reconcile casts
   ▼
LLVM dialect ──mlir-translate──▶ LLVM IR ──llc -O2──▶ COFF ──cl──▶ .exe
```

这是一遍式前端：Parser 读到 `let` 就生成 `memref.alloca`，读到 `+` 就生成 `arith.addi`，不先构造 AST。代码短、数据流直接；代价是错误恢复与跨函数分析做不了。

### 四个关键决策

- **`scf.if/scf.while` 而非直接 `cf`**：`scf` 保留结构化语义，前端不手搓基本块、block argument 和 `cf.br`；`convert-scf-to-cf` 再生成 CFG。实测 `scf.while` 被降成 `^bb1`–`^bb6`。
- **`memref.alloca` 而非纯 SSA + block argument**：赋值就是 `store`，循环变量更新不必穿过 `scf.while` 的 `iter_args`。后面可跑 `mem2reg` 提升；代价是控制流内的槽位会留在栈上。
- **`i64` 而非 `index`**：语言 `int` 要匹配 C 的 `int64_t` ABI；`index` 只作为 MLIR 内部下标/大小出现。这样源级 IR 类型稳定。
- **`func.func` 默认 ABI 而非显式 `llvm.func`**：`convert-func-to-llvm` 负责调用约定与外部声明，手写 `llvm.func` 等于跳过这层计算。

## 分阶段实现

### M1：Lexer——字符变成带位置的 token

Lexer 跳过空白/注释、识别关键字与标识符、把运算符映射为 `Kind`。每个 token 带 `line/col`，diagnostics 直接复用。

```cpp
case '-':
  if (take('>')) out.push_back({Kind::Arrow, "->", 0, l, k});
  else out.push_back({Kind::Minus, "-", 0, l, k});
  break;
```

**验证**：`let x = 1 ! 2;` 应报 `expected '=' after '!'`；最终错误用例见后文。

### M2：Parser——递归下降，优先级写在函数层级

`parseCompare → parseAdd → parseMul → parseUnary → parsePrimary` 五层对应语法表；`parseStmt` 按首 token 分派。

```cpp
Value parseMul() {
  Value lhs = parseUnary();
  while (at(Kind::Star) || at(Kind::Slash)) {
    Token tok = peek(); ++pos; Value rhs = parseUnary();
    if (tok.kind == Kind::Star) lhs = arith::MulIOp::create(builder, loc(tok), lhs, rhs);
    else lhs = arith::DivSIOp::create(builder, loc(tok), lhs, rhs);
  }
  return lhs;
}
```

**验证**：`mflc tests\basic.mfl` 退出码 0，stdout 是 `module { ... }`，stderr 为空。

### M3：变量与表达式——`alloca` / `store` / `load`

每个 `let` 生成 0 维 `memref<i64>` 并立即 `store`；变量引用生成 `load`，赋值生成 `store`。作用域是 `vector<map<string, Value>>`，从后往前查找。

```cpp
Value s = memref::AllocaOp::create(builder, loc(start), MemRefType::get({}, i64));
memref::StoreOp::create(builder, loc(start), value, s);
vars.back()[name.text] = s;
```

**验证**：`basic.mfl` 开头出现连续的 `memref.alloca`、`memref.store`、`arith.addi`；0 维索引写作 `%slot[]`。

### M4：控制流——`scf.if` 两 region，`scf.while` 两段式

`scf.if` 创建时若没有 else region，后面访问 `getElseRegion().front()` 会得到空 region，所以统一传 `withElseRegion=true`；空 else 交给 `canonicalize`。`scf.while` 的 `before` 算条件，`after` 跑循环体。

```cpp
scf::WhileOp::create(builder, loc(start), TypeRange{}, ValueRange{},
  [&](OpBuilder &b, Location l, ValueRange) {
    Value cond = parseExpr();
    if (!cond.getType().isInteger(1)) fail(start.line, start.col, "while condition must be a comparison");
    scf::ConditionOp::create(b, l, cond, ValueRange{});
  },
  [&](OpBuilder &b, Location l, ValueRange) {
    vars.emplace_back(); parseBlock(); vars.pop_back();
    scf::YieldOp::create(b, l);
  });
```

**验证**：只跑 `convert-scf-to-cf` 后，IR 出现 `cf.cond_br`/`cf.br`，但仍保留 `func.func`、`arith`、`memref`。

### M5：函数与外部调用——先登记，再生成 body

签名先放进 `funcs`，再创建 `func.func`、`addEntryBlock()`，参数写入各自槽位。调用用 `func::CallOp`，结果类型由被调函数决定。

```cpp
std::vector<Type> args(params.size(), i64);
auto fn = func::FuncOp::create(builder, loc(name), name.text,
                               FunctionType::get(&ctx, args, {i64}));
if (external) { fn.setPrivate(); expect(Kind::Semi, "';'"); funcs[name.text] = fn; return; }
funcs[name.text] = fn;
Block *entry = fn.addEntryBlock();
```

**验证**：`print_int` 生成 `func.func private @print_int(i64) -> i64`，最后成为 LLVM 声明；`add(sum, 100)` 最终成为 `llvm.call @add`。

### M6：验证链——每一步都要能单独停住

不要串成黑盒；先按下一节设好 `$mlir`，每次只跑一段，检查方言是否绝迹：

```powershell
& "$mlir\mlir-opt.exe" .\basic.mlir --pass-pipeline='builtin.module(canonicalize,func.func(convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm,reconcile-unrealized-casts)'
```

**验证**：最终 `func.func`、`scf.`、`arith.`、`memref.` 全消失，只剩 `llvm.`。若 `mlir-translate` 报 `Dialect 'arith' not found`，是漏跑 conversion，不是要加 `--allow-unregistered-dialect`。

## 完整代码清单

文件都在 `D:\Learning-with-Claude\.scratch\final-mfl`；`main.cpp` 内依次是 diagnostics、`Lexer`、`Compiler`（parser + codegen）和 CLI。下面是可直接编译的版本（与工程内文件仅空行不同）。

### `CMakeLists.txt`

```cmake
cmake_minimum_required(VERSION 3.20)
project(mflc CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(MLIR REQUIRED CONFIG)
add_executable(mflc main.cpp)
target_link_libraries(mflc PRIVATE
  MLIRIR MLIRParser MLIRSupport
  MLIRFuncDialect MLIRArithDialect MLIRSCFDialect
  MLIRMemRefDialect MLIRControlFlowDialect
  ntdll.lib ZLIB::ZLIB)
target_include_directories(mflc PRIVATE ${MLIR_INCLUDE_DIRS})
```

### `main.cpp`

```cpp
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/Location.h"
#include "mlir/IR/MLIRContext.h"
#include "mlir/IR/Verifier.h"
#include "llvm/Support/raw_ostream.h"
#include <cctype>
#include <cstdint>
#include <fstream>
#include <map>
#include <sstream>
#include <string>
#include <vector>
using namespace mlir;
struct CompileError { int line, col; std::string message; };
[[noreturn]] static void fail(int line, int col, const std::string &m) {
  throw CompileError{line, col, m};
}
enum class Kind {
  End, Ident, Number, Let, If, Else, While, Fn, Extern, Return, Int,
  LParen, RParen, LBrace, RBrace, Assign, Plus, Minus, Star, Slash,
  EqEq, NotEq, Lt, Le, Gt, Ge, Arrow, Semi, Colon, Comma
};
struct Token { Kind kind; std::string text; int64_t number = 0; int line = 1, col = 1; };
class Lexer {
public:
  Lexer(std::string source) : src(std::move(source)) {}
  std::vector<Token> lex() {
    std::vector<Token> out;
    while (true) {
      skipSpace();
      if (pos >= src.size()) { out.push_back({Kind::End, "", 0, line, col}); return out; }
      char c = src[pos]; int l = line, k = col;
      if (std::isalpha((unsigned char)c) || c == '_') out.push_back(identifier());
      else if (std::isdigit((unsigned char)c)) out.push_back(number());
      else {
        advance();
        switch (c) {
        case '(': out.push_back({Kind::LParen, "(", 0, l, k}); break;
        case ')': out.push_back({Kind::RParen, ")", 0, l, k}); break;
        case '{': out.push_back({Kind::LBrace, "{", 0, l, k}); break;
        case '}': out.push_back({Kind::RBrace, "}", 0, l, k}); break;
        case '+': out.push_back({Kind::Plus, "+", 0, l, k}); break;
        case '-':
          if (take('>')) out.push_back({Kind::Arrow, "->", 0, l, k});
          else out.push_back({Kind::Minus, "-", 0, l, k});
          break;
        case '*': out.push_back({Kind::Star, "*", 0, l, k}); break;
        case '/': out.push_back({Kind::Slash, "/", 0, l, k}); break;
        case ';': out.push_back({Kind::Semi, ";", 0, l, k}); break;
        case ':': out.push_back({Kind::Colon, ":", 0, l, k}); break;
        case ',': out.push_back({Kind::Comma, ",", 0, l, k}); break;
        case '=': out.push_back({take('=') ? Kind::EqEq : Kind::Assign, "=", 0, l, k}); break;
        case '!':
          if (!take('=')) fail(l, k, "expected '=' after '!'");
          out.push_back({Kind::NotEq, "!=", 0, l, k}); break;
        case '<': out.push_back({take('=') ? Kind::Le : Kind::Lt, "<", 0, l, k}); break;
        case '>': out.push_back({take('=') ? Kind::Ge : Kind::Gt, ">", 0, l, k}); break;
        default: fail(l, k, std::string("unexpected character '") + c + "'");
        }
      }
    }
  }
private:
  std::string src; size_t pos = 0; int line = 1, col = 1;
  void advance() { if (src[pos] == '\n') { ++line; col = 1; } else ++col; ++pos; }
  bool take(char c) { if (pos < src.size() && src[pos] == c) { advance(); return true; } return false; }
  void skipSpace() {
    while (pos < src.size()) {
      if (src[pos] == '/' && pos + 1 < src.size() && src[pos + 1] == '/')
        while (pos < src.size() && src[pos] != '\n') advance();
      else if (std::isspace((unsigned char)src[pos])) advance();
      else break;
    }
  }
  Token identifier() {
    int l = line, k = col; size_t begin = pos;
    while (pos < src.size() && (std::isalnum((unsigned char)src[pos]) || src[pos] == '_')) advance();
    std::string word = src.substr(begin, pos - begin);
    static const std::map<std::string, Kind> kw = {
      {"let", Kind::Let}, {"if", Kind::If}, {"else", Kind::Else}, {"while", Kind::While},
      {"fn", Kind::Fn}, {"extern", Kind::Extern}, {"return", Kind::Return}, {"int", Kind::Int}};
    auto it = kw.find(word);
    return {it == kw.end() ? Kind::Ident : it->second, word, 0, l, k};
  }
  Token number() {
    int l = line, k = col; size_t begin = pos;
    while (pos < src.size() && std::isdigit((unsigned char)src[pos])) advance();
    std::string digits = src.substr(begin, pos - begin);
    try {
      size_t used = 0; int64_t value = std::stoll(digits, &used, 10);
      if (used != digits.size()) fail(l, k, "invalid integer literal");
      return {Kind::Number, digits, value, l, k};
    } catch (...) { fail(l, k, "integer literal out of range"); }
  }
};
class Compiler {
public:
  Compiler(MLIRContext &ctx, std::string file, std::vector<Token> toks)
      : ctx(ctx), file(std::move(file)), toks(std::move(toks)), builder(&ctx) {}
  ModuleOp compile() {
    i64 = builder.getI64Type();
    module = ModuleOp::create(builder.getUnknownLoc());
    builder.setInsertionPointToStart(module.getBody());
    while (!at(Kind::End)) parseFunction();
    auto it = funcs.find("mfl_main");
    if (it == funcs.end()) fail(1, 1, "missing function 'mfl_main'");
    if (it->second.getFunctionType().getNumInputs() != 0)
      fail(1, 1, "'mfl_main' must have no parameters");
    return module;
  }
private:
  MLIRContext &ctx; std::string file; std::vector<Token> toks; size_t pos = 0;
  OpBuilder builder; ModuleOp module; Type i64; bool inControl = false;
  std::map<std::string, func::FuncOp> funcs;
  std::vector<std::map<std::string, Value>> vars;
  const Token &peek(size_t n = 0) const { return toks[std::min(pos + n, toks.size() - 1)]; }
  bool at(Kind k) const { return peek().kind == k; }
  bool take(Kind k) { if (!at(k)) return false; ++pos; return true; }
  const Token &expect(Kind k, const std::string &what) {
    if (!at(k)) fail(peek().line, peek().col, "expected " + what + ", found '" + peek().text + "'");
    return toks[pos++];
  }
  Location loc(const Token &t) { return FileLineColLoc::get(&ctx, file, t.line, t.col); }
  Value slot(Location l) { return memref::AllocaOp::create(builder, l, MemRefType::get({}, i64)); }
  Value lookup(const std::string &name, const Token &at) {
    for (auto it = vars.rbegin(); it != vars.rend(); ++it) {
      auto found = it->find(name);
      if (found != it->end()) return found->second;
    }
    fail(at.line, at.col, "use of undefined variable '" + name + "'");
  }
  void parseFunction() {
    bool external = take(Kind::Extern);
    expect(Kind::Fn, "'fn'");
    Token name = expect(Kind::Ident, "function name");
    if (funcs.count(name.text)) fail(name.line, name.col, "duplicate function '" + name.text + "'");
    expect(Kind::LParen, "'('");
    std::vector<std::string> params;
    if (!at(Kind::RParen)) do {
      params.push_back(expect(Kind::Ident, "parameter name").text);
      expect(Kind::Colon, "':'"); expect(Kind::Int, "'int'");
    } while (take(Kind::Comma));
    expect(Kind::RParen, "')'"); expect(Kind::Arrow, "'->'"); expect(Kind::Int, "'int'");
    std::vector<Type> args(params.size(), i64);
    auto fn = func::FuncOp::create(builder, loc(name), name.text, FunctionType::get(&ctx, args, {i64}));
    if (external) { fn.setPrivate(); expect(Kind::Semi, "';'"); funcs[name.text] = fn; return; }
    funcs[name.text] = fn;
    Block *entry = fn.addEntryBlock();
    OpBuilder::InsertionGuard guard(builder);
    builder.setInsertionPointToStart(entry);
    vars.clear(); vars.emplace_back();
    for (size_t i = 0; i < params.size(); ++i) {
      Value s = slot(loc(name));
      memref::StoreOp::create(builder, loc(name), entry->getArgument(i), s);
      vars.back()[params[i]] = s;
    }
    parseBlock();
  }
  void parseBlock() {
    expect(Kind::LBrace, "'{'");
    while (!at(Kind::RBrace)) {
      if (at(Kind::End)) fail(peek().line, peek().col, "unterminated block");
      parseStmt();
    }
    expect(Kind::RBrace, "'}'");
  }
  void parseStmt() {
    Token start = peek();
    if (take(Kind::Let)) {
      Token name = expect(Kind::Ident, "variable name"); expect(Kind::Assign, "'='");
      Value value = parseExpr(); expect(Kind::Semi, "';'");
      if (vars.back().count(name.text)) fail(name.line, name.col, "duplicate variable '" + name.text + "'");
      Value s = slot(loc(start));
      memref::StoreOp::create(builder, loc(start), value, s);
      vars.back()[name.text] = s;
    } else if (take(Kind::If)) {
      Value cond = parseExpr();
      if (!cond.getType().isInteger(1)) fail(start.line, start.col, "if condition must be a comparison");
      auto op = scf::IfOp::create(builder, loc(start), cond, true);
      bool old = inControl; inControl = true;
      builder.setInsertionPointToStart(&op.getThenRegion().front());
      vars.emplace_back(); parseBlock(); vars.pop_back();
      if (take(Kind::Else)) {
        builder.setInsertionPointToStart(&op.getElseRegion().front());
        vars.emplace_back(); parseBlock(); vars.pop_back();
      }
      inControl = old; builder.setInsertionPointAfter(op);
    } else if (take(Kind::While)) {
      scf::WhileOp::create(builder, loc(start), TypeRange{}, ValueRange{},
        [&](OpBuilder &b, Location l, ValueRange) {
          Value cond = parseExpr();
          if (!cond.getType().isInteger(1)) fail(start.line, start.col, "while condition must be a comparison");
          scf::ConditionOp::create(b, l, cond, ValueRange{});
        },
        [&](OpBuilder &b, Location l, ValueRange) {
          bool old = inControl; inControl = true;
          vars.emplace_back(); parseBlock(); vars.pop_back();
          inControl = old; scf::YieldOp::create(b, l);
        });
    } else if (take(Kind::Return)) {
      if (inControl) fail(start.line, start.col, "'return' inside if/while is not supported");
      Value value = parseExpr(); expect(Kind::Semi, "';'");
      func::ReturnOp::create(builder, loc(start), value);
    } else if (at(Kind::Ident) && peek(1).kind == Kind::Assign) {
      Token name = expect(Kind::Ident, "variable name"); expect(Kind::Assign, "'='");
      Value value = parseExpr(); expect(Kind::Semi, "';'");
      memref::StoreOp::create(builder, loc(name), value, lookup(name.text, name));
    } else {
      (void)parseExpr(); expect(Kind::Semi, "';'");
    }
  }
  Value parseExpr() { return parseCompare(); }
  Value parseCompare() {
    Value lhs = parseAdd();
    Kind op = peek().kind;
    if (op != Kind::EqEq && op != Kind::NotEq && op != Kind::Lt &&
        op != Kind::Le && op != Kind::Gt && op != Kind::Ge) return lhs;
    Token tok = peek(); ++pos; Value rhs = parseAdd();
    arith::CmpIPredicate pred =
        op == Kind::EqEq ? arith::CmpIPredicate::eq :
        op == Kind::NotEq ? arith::CmpIPredicate::ne :
        op == Kind::Lt ? arith::CmpIPredicate::slt :
        op == Kind::Le ? arith::CmpIPredicate::sle :
        op == Kind::Gt ? arith::CmpIPredicate::sgt : arith::CmpIPredicate::sge;
    return arith::CmpIOp::create(builder, loc(tok), pred, lhs, rhs);
  }
  Value parseAdd() {
    Value lhs = parseMul();
    while (at(Kind::Plus) || at(Kind::Minus)) {
      Token tok = peek(); ++pos; Value rhs = parseMul();
      if (tok.kind == Kind::Plus) lhs = arith::AddIOp::create(builder, loc(tok), lhs, rhs);
      else lhs = arith::SubIOp::create(builder, loc(tok), lhs, rhs);
    }
    return lhs;
  }
  Value parseMul() {
    Value lhs = parseUnary();
    while (at(Kind::Star) || at(Kind::Slash)) {
      Token tok = peek(); ++pos; Value rhs = parseUnary();
      if (tok.kind == Kind::Star) lhs = arith::MulIOp::create(builder, loc(tok), lhs, rhs);
      else lhs = arith::DivSIOp::create(builder, loc(tok), lhs, rhs);
    }
    return lhs;
  }
  Value parseUnary() {
    if (take(Kind::Minus)) {
      Token tok = peek();
      return arith::SubIOp::create(builder, loc(tok),
          arith::ConstantIntOp::create(builder, loc(tok), i64, 0), parseUnary());
    }
    return parsePrimary();
  }
  Value parsePrimary() {
    if (at(Kind::Number)) {
      Token tok = peek(); ++pos;
      return arith::ConstantIntOp::create(builder, loc(tok), i64, tok.number);
    }
    if (at(Kind::Ident)) {
      Token tok = peek(); ++pos;
      if (take(Kind::LParen)) {
        auto found = funcs.find(tok.text);
        if (found == funcs.end()) fail(tok.line, tok.col, "call to undefined function '" + tok.text + "'");
        llvm::SmallVector<Value> args;
        if (!at(Kind::RParen)) do { args.push_back(parseExpr()); } while (take(Kind::Comma));
        expect(Kind::RParen, "')'");
        if (args.size() != found->second.getNumArguments())
          fail(tok.line, tok.col, "function '" + tok.text + "' expects " +
               std::to_string(found->second.getNumArguments()) + " argument(s), got " +
               std::to_string(args.size()));
        return func::CallOp::create(builder, loc(tok), found->second, args).getResult(0);
      }
      return memref::LoadOp::create(builder, loc(tok), lookup(tok.text, tok), ValueRange{});
    }
    if (take(Kind::LParen)) { Value value = parseExpr(); expect(Kind::RParen, "')'"); return value; }
    fail(peek().line, peek().col, "expected expression, found '" + peek().text + "'");
  }
};
int main(int argc, char **argv) {
  if (argc != 2) { llvm::errs() << "usage: mflc <input.mfl>\n"; return 2; }
  std::string file = argv[1];
  std::ifstream input(file, std::ios::binary);
  if (!input) { llvm::errs() << file << ": error: cannot open input\n"; return 1; }
  std::stringstream buffer; buffer << input.rdbuf();
  try {
    Lexer lexer(buffer.str());
    MLIRContext ctx;
    ctx.loadDialect<arith::ArithDialect, scf::SCFDialect, memref::MemRefDialect, func::FuncDialect>();
    Compiler compiler(ctx, file, lexer.lex());
    ModuleOp module = compiler.compile();
    if (failed(module.verify())) return 1;
    module.print(llvm::outs()); llvm::outs() << "\n";
    return 0;
  } catch (const CompileError &e) {
    llvm::errs() << file << ":" << e.line << ":" << e.col << ": error: " << e.message << "\n";
    return 1;
  }
}
```

### `runtime.c`

```c
#include <stdint.h>
#include <stdio.h>
extern int64_t mfl_main(void);
int64_t print_int(int64_t value) { printf("%lld\n", (long long)value); return value; }
int main(void) { printf("result=%lld\n", (long long)mfl_main()); return 0; }
```

## 构建与运行

本机系统 PATH 里的 CMake 4.0.0-rc5 在配置阶段报 `C1069`，改用 VS2022 自带的 3.30.5 后成功；`TEMP` 曾落在无空间的 `E:\WindowsTemp` 并触发 `LNK1108`，所以指到工程内。
```bat
@echo off
if not exist "%~dp0tmp" mkdir "%~dp0tmp"
set TEMP=%~dp0tmp
set TMP=%TEMP%
set PATH=D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja;%PATH%
call "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" >nul
cmake -G Ninja -S . -B build-vs-cmake ^
  -DMLIR_DIR="D:\Learning-with-Claude\.scratch\mlir-env\Library\lib\cmake\mlir" ^
  -DCMAKE_PREFIX_PATH="D:\Learning-with-Claude\.scratch\mlir-env\Library" ^
  -DCMAKE_BUILD_TYPE=Release ^
  "-DCMAKE_EXE_LINKER_FLAGS=/LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/um/x64 /LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/ucrt/x64"
cmake --build build-vs-cmake
```

`实测`关键配置/构建行（`LibXml2` 找不到不是致命错误）：

```text
-- Found ZLIB: .../Library/lib/z.lib (found version "1.3.2")
-- Found zstd: .../Library/lib/zstd.lib
-- Could NOT find LibXml2 (missing: LIBXML2_LIBRARY LIBXML2_INCLUDE_DIR)
[2/2] Linking CXX executable mflc.exe
```

四个构建要点：`CMAKE_PREFIX_PATH` 必须指向 conda 的 `Library`，本机找到的是 `z.lib`，所以链接 `ZLIB::ZLIB` 而非硬写 `zlib.lib`；补 SDK 10.0.17763 的 `um\x64` 与 `ucrt\x64` 以绕开 10.0.26100 缺 `shell32.lib`；`LLVMSupport.lib` 需要 `ntdll.lib`；源码含非 ASCII 时加 `/utf-8`。

生成、降级、翻译、汇编、链接：

```powershell
$proj = "D:\Learning-with-Claude\.scratch\final-mfl"
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$llc  = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
$pipeline = "builtin.module(canonicalize,func.func(convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm,reconcile-unrealized-casts)"
cmd /d /c "cd /d $proj && build-vs-cmake\mflc.exe tests\basic.mfl > basic.mlir"
cmd /d /c "cd /d $proj && `"$mlir\mlir-opt.exe`" basic.mlir --pass-pipeline=`"$pipeline`" > basic.llvm.mlir && `"$mlir\mlir-translate.exe`" --mlir-to-llvmir basic.llvm.mlir > basic.ll"
& $llc -O2 "$proj\basic.ll" -filetype=obj -o "$proj\basic.obj"
New-Item -ItemType Directory -Force -Path "$proj\tmp" | Out-Null
cmd /d /c "set TEMP=$proj\tmp&& set TMP=$proj\tmp&& call ""D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"" >nul && cd /d $proj && cl /nologo /O2 /MD runtime.c basic.obj /Fe:basic.exe"
```

`实测`的中间状态。`mflc` 输出（节选）：

```mlir
func.func private @print_int(i64) -> i64
func.func @mfl_main() -> i64 {
  scf.while : () -> () {
    %5 = memref.load %alloca_0[] : memref<i64>
    %6 = arith.cmpi sle, %5, %c10_i64 : i64
    scf.condition(%6)
  } do {
    scf.yield
  }
```

只跑 `func.func(convert-scf-to-cf)` 后仍保留 `cf.cond_br`/`cf.br` 与 `func.func`、`arith`、`memref`；可以在这一步停住检查。

完整 pipeline 后（节选），只剩 LLVM dialect：

```mlir
llvm.func @print_int(i64) -> i64 attributes {sym_visibility = "private"}
%23 = llvm.icmp "sle" %22, %2 : i64
llvm.cond_br %23, ^bb2, ^bb6
%33 = llvm.call @print_int(%32) : (i64) -> i64
```

`mlir-translate` 的 LLVM IR（节选）；`llvm-objdump -d --no-show-raw-insn basic.obj` 也已验证反汇编成功：

```llvm
define i64 @mfl_main() {
  %14 = icmp sle i64 %13, 10
  %26 = call i64 @print_int(i64 %25)
  %42 = call i64 @add(i64 %41, i64 100)
  ret i64 %52
}
```

## 测试用例与期望输出

### 用例 1：`tests/basic.mfl`

```text
extern fn print_int(x: int) -> int;
fn add(a: int, b: int) -> int { return a + b; }
fn mfl_main() -> int {
  let sum = 0;
  let i = 1;
  while i <= 10 {
    if i / 2 * 2 == i {
      let ignored = print_int(i);
    } else {
      sum = sum + i;
    }
    i = i + 1;
  }
  let answer = add(sum, 100);
  print_int(answer);
  return answer;
}
```

`实测`运行结果（`basic.exe`）：

```text
2
4
6
8
10
125
result=125
```

偶数为 `2 4 6 8 10`；奇数和 `25`；加 `100` 得 `125`。最后一行是 C runtime 打印的 `mfl_main` 返回值。

### 用例 2：`tests/gcd.mfl`

```text
extern fn print_int(x: int) -> int;
fn gcd(a: int, b: int) -> int {
  while b != 0 { let r = a - (a / b) * b; a = b; b = r; }
  return a;
}
fn abs(x: int) -> int {
  if x < 0 { x = 0 - x; }
  return x;
}
fn mfl_main() -> int {
  let g = gcd(1071, 462);
  print_int(g);
  print_int(abs(-7));
  return g;
}
```

`实测`运行结果（`gcd.exe`）：

```text
21
7
result=21
```

### 用例 3：`tests/bad.mfl`

```text
fn mfl_main() -> int {
  let x = 1
  return x;
}
```

`实测`编译（退出码 1）：

```text
tests\bad.mfl:3:3: error: expected ';', found 'return'
```

## 常见失败与排查

- **`Dialect 'arith' not found`**：`mlir-translate` 只注册少数方言；补跑 `convert-arith-to-llvm`，不要加 `--allow-unregistered-dialect`。
- **`'-pass-pipeline' option can't be used with individual pass options`**：把 `canonicalize` 放进 `builtin.module(...)`，不要同时写 `--canonicalize`。
- **`unexpected character` / UTF-16**：PowerShell 5 的 `>` 生成 UTF-16，改用 `cmd` 重定向；双引号字符串里嵌 cmd 引号要用 `""`，不要写 `\"`。
- **`LNK1108` / `LNK1181`**：前者检查 `TEMP/TMP` 空间；后者链接 `ZLIB::ZLIB`，不要硬写 `zlib.lib`。
- **`scf.if` 的 else 丢失**：创建时给 else region 预留位置；空 else 交给 `canonicalize`。

## 扩展方向

1. 加 `bool` 或真值转换：在 codegen 里插入 `arith.cmpi ne, x, 0`，再生成 `scf.if`。
2. 支持提前 `return`：把 `scf.if` 做成带结果的控制流，或在 `cf` 层合并基本块；当前版本明确拒绝。
3. 加 `for` 与 `break/continue`：`for` 映射 `scf.for`；`break` 需要让 `scf.while` 的继续条件携带退出标志。
4. 做 SSA 变量提升：跑 `mem2reg`/`canonicalize` 消除简单 `alloca`，对比栈槽版 IR。

## 验收清单

- [ ] `mflc tests\basic.mfl` 输出合法 MLIR，退出码 0
- [ ] 能指出 `memref.alloca`、`memref.load/store`、`scf.while`、`scf.if`
- [ ] 跑完整 pipeline 后，IR 中不再有 `arith.`、`scf.`、`memref.`、`func.func`
- [ ] 能翻译 LLVM IR、用 `llc -O2` 出 COFF 对象
- [ ] 能链接 `runtime.c` 并运行，看到 `basic` 与 `gcd` 的期望输出
- [ ] 能说清 `scf` vs `cf`、`alloca` vs SSA、`index` vs `i64`、`func` vs `llvm.func` 四组取舍

## 从 Final 到生产级还差什么

这是可运行的教学前端，不是生产编译器。语言面只有 `i64`，没有结构体、数组、字符串、模块、短路逻辑和提前返回；只支持先定义后调用，没有前置声明、跨文件链接、重载或泛型。也没有优化流水线：`canonicalize` 只做局部清理，没有内联、循环优化、标量提升、别名分析。控制流内的 `alloca` 可能在每轮分配，长循环需提升到入口块或跑 `mem2reg`。诊断只有位置和一句话，没有 note、源码片段、修复建议和错误恢复。Windows x64 + MSVC 2022 已实测；Linux/macOS 的 C ABI 与链接命令是 `guided`，需要重新验证 `print_int` 约定与 `llc` 目标。

真正要往前走，优先补类型系统与诊断、SSA/控制流正确性测试、以及可重复的 lit/CTest 回归；否则“能生成 IR”很容易变成“只在两个例子上能生成 IR”。

## 延伸阅读

- **[Ch 0 环境与最小闭环](./00-environment-setup.md)**：CMake 配方与 Windows 链接坑；本章构建直接建立在它之上。
- **[Ch 17 Dialect Conversion](./17-dialect-conversion.md)**：legality 与 partial/full；本章 pipeline 的每一步都是一个小 conversion。
- **[Ch 20 Bufferization](./20-bufferization.md)**：tensor→memref 的别名决策；本章直接用 memref，跳过这一层。
- **[Ch 21 降到 LLVM dialect](./21-lowering-to-llvm.md)**：`finalize-memref-to-llvm` 与 descriptor；解释本章的 `insertvalue/extractvalue`。
- **[MLIR Toy Tutorial](https://mlir.llvm.org/docs/Tutorials/Toy/)**：AST 与方言设计；对照本章的一遍式前端。

→ [返回课程首页](./README.md)
