# Ch 12 约束、验证与自定义 parser/printer

> **目标**：让 op 在"类型不对""属性越界""语义不自洽"三种情况下给出可读的报错。学完本章，你能区分 parser、ODS 生成 verifier、手写 verifier 三层检查各拦什么，能写出 `ConfinedAttr` 范围约束与 `hasVerifier` 的 C++ 实现，也能在 `assemblyFormat` 与手写 `parse`/`print` 之间做出选择。

## 为什么需要这个？

Ch 11 的 `toy.add` 只声明了"两个 f64 tensor 进、一个 f64 tensor 出"。它没有回答几个关键问题：

- 两个操作数的 shape 不同，算不算合法？
- `toy.scale` 的缩放因子能不能是 0 或负数？
- 结果类型和输入类型不一致，是构造时就拒绝，还是等 lowering 时炸？

这些问题 ODS 的**类型约束**只能回答一部分。`TensorOf<[F64]>` 保证"是 f64 tensor"，但保证不了"两个 tensor shape 相同"；`I64Attr` 保证"是 64 位整数属性"，但保证不了"取值在 1 到 1024 之间"。

更麻烦的是错误信息的质量。一个没有 verifier 的 op，非法 IR 会一路活到 lowering，最后在某个 `cast` 里崩掉，栈回溯指向与你无关的代码。**验证的价值不只是"拦住错误"，而是"在离错误最近的地方，用 IR 的语言说清楚哪里错了"**。

## 先建立直觉：三层检查，三种报错口吻

一段非法 IR 从文本到内存，会依次经过三道关：

```text
文本 ──parser──▶ Operation ──ODS 生成 verifier──▶ 类型/属性/必需字段检查 ──自定义 verify()──▶ 语义检查
```

三层的报错口吻完全不同，学会从消息反推层次，调试能快一倍：

| 层 | 典型消息 | 谁生成 | 能检查什么 |
|---|---|---|---|
| parser | `use of value '%b' expects different type than prior uses` | 手写/生成的 parse | 语法、SSA 类型一致性、字面量范围 |
| ODS verifier | `operand #0 must be signless-non-zero-bitwidth-integer-like` | `mlir-tblgen` | 每个 operand/result/attr 是否满足声明的约束 |
| 自定义 verifier | `mask index #2 out of range` | 你写的 `verify()` | 跨操作数关系、shape 一致性、语义不变量 |

关键认知：**ODS 的约束是"逐值"的**。它检查"第 0 个操作数是整数"，不检查"第 0 个和第 1 个 shape 相同"。后者必须写 `hasVerifier = 1` 并实现 `verify()`。

这也解释了一个常见困惑：为什么 `arith.addi` 传入两个不同类型的值，报的是 parser 错误而不是 ODS 错误？因为 parser 在构造 SSA 引用时就发现同一个值被当成两种类型用，比 verifier 更早。**先报的那个错，往往不是你以为的那一层**。

## TypeConstraint：描述"什么样的类型"

Ch 11 用的 `TensorOf<[F64]>` 就是一个 TypeConstraint。它的定义在 `mlir/IR/CommonTypeConstraints.td`，而所有类型约束的基类是 `TypeConstraint`（`mlir/IR/Constraints.td:166`）。两个字段值得记住：

- `predicate` 是一段 C++ 表达式（`Pred`），生成 verifier 时直接内联；
- `cppType` 决定生成的访问器返回什么类型。默认 `::mlir::Type`——所以自定义约束写出来的 `getInput()` 是 `TypedValue<Type>`，不是 `TypedValue<TensorType>`。

自定义约束只需一行：

```tablegen
def Toy_FloatTensor : Type<TensorOf<[AnyFloat]>.predicate,
                           "tensor of floating-point values">;
```

`Type<...>` 是 `TypeConstraint` 的语法糖（`CommonTypeConstraints.td:103`），第二个参数是**给用户看的描述**，会原样出现在报错里。实测生成物（`scale.cpp.inc:26-33`）：

```cpp
static ::llvm::LogicalResult __mlir_ods_local_type_constraint_scale1(
    ::mlir::Operation *op, ::mlir::Type type, ::llvm::StringRef valueKind,
    unsigned valueIndex) {
  if (!(((::llvm::isa<::mlir::TensorType>(type))) && ([](::mlir::Type elementType) { return (::llvm::isa<::mlir::FloatType>(elementType)); }(::llvm::cast<::mlir::ShapedType>(type).getElementType())))) {
    return op->emitOpError(valueKind) << " #" << valueIndex
        << " must be tensor of floating-point values, but got " << type;
  }
  return ::mlir::success();
}
```

这段代码有三个设计点：

1. **一个约束一个函数**，名字带序号（`scale1`），同一 op 内复用；
2. 参数是 `(Operation*, Type, valueKind, valueIndex)`，所以报错能精确到 `operand #0` 还是 `result #0`；
3. 失败时直接 `emitOpError`，不返回错误码给上层拼字符串——消息在源头就完整。

注意那个 lambda：`TensorOf<[AnyFloat]>.predicate` 展开成"先 isa TensorType，再取 elementType 判断"，lambda 是为了避免对非 ShapedType 调 `getElementType()`。如果你自己用 `CPred` 写约束，这类短路必须自己写对，否则会 UB。

## AttrConstraint：范围、枚举与非空

属性约束的基类在 `Constraints.td:175`。最常用的是 `ConfinedAttr`（`CommonAttrConstraints.td:766`），它把一个基础属性类型和一串约束"与"起来：

```tablegen
ConfinedAttr<I64Attr, [IntMinValue<1>, IntMaxValue<1024>]>:$factor
```

`IntMinValue` / `IntMaxValue`（`CommonAttrConstraints.td:796`、`:800`）本身就是 `AttrConstraint`，predicate 是 `::llvm::cast<::mlir::IntegerAttr>($_self).getInt() >= 1` 这类表达式。

`ConfinedAttr` 做的事情可以用源码里的三行概括（`CommonAttrConstraints.td:766-770`）：把 `attr.predicate` 与所有约束的 predicate 用 `And` 拼起来，summary 用空格连接。所以生成物里会出现一条很长的条件：

```cpp
  if (tblgen_factor && !((((::llvm::isa<::mlir::IntegerAttr>(tblgen_factor))) && ((::llvm::cast<::mlir::IntegerAttr>(tblgen_factor).getType().isSignlessInteger(64)))) && ((::llvm::cast<::mlir::IntegerAttr>(tblgen_factor).getInt() >= 1)) && ((::llvm::cast<::mlir::IntegerAttr>(tblgen_factor).getInt() <= 1024))))
    return emitError(loc, "'toyv.scale' op attribute 'factor' failed to satisfy constraint: 64-bit signless integer attribute whose minimum value is 1 whose maximum value is 1024");
```

（实测，`scale.cpp.inc:258-259`。）

三个细节：

- summary 拼接顺序就是约束列表顺序，报错文案与 `.td` 写法一一对应；
- `isSignlessInteger(64)` 来自 `I64Attr` 本身，不是范围约束；
- 这段在 **adaptor 的 verify** 里，也在 `verifyInvariantsImpl` 里各出现一次——前者给"只拿到一组操作数和属性"的场景用（比如 conversion pattern 的 adaptor）。

范围的边界是**闭区间**。`IntMinValue<1>` 允许 1；要排除 0 就写 1，不要写 0。这是最容易差一的地方。

## `hasVerifier = 1`：声明与实现的契约

ODS 只能检查"逐值"约束，跨操作数的关系必须自己写。声明只有一行（`OpBase.td:418`）：

```tablegen
let hasVerifier = 1;
```

生成器看到它，会在类里加一个成员声明，并在 `verifyInvariants()` 里调用（实测，`scale.h.inc:399` 与 `scale.cpp.inc:507-511`）：

```cpp
  ::llvm::LogicalResult verify();
```

```cpp
::llvm::LogicalResult ScaleOp::verifyInvariants() {
  if(::mlir::succeeded(verifyInvariantsImpl()) && ::mlir::succeeded(verify()))
    return ::mlir::success();
  return ::mlir::failure();
}
```

调用顺序是**先 ODS 后自定义**。这个顺序不能反：自定义 `verify()` 里通常会 `cast` 成具体类型、直接读属性，前提是 ODS 已经保证字段存在且类型正确。如果 ODS 检查失败，`verify()` 根本不会被调用——这也是为什么你可以在 `verify()` 里放心地 `getFactor()`。

实现放在 `.cpp` 里，签名固定：

```cpp
::llvm::LogicalResult ScaleOp::verify() {
  if (getInput().getType() != getOutput().getType())
    return emitOpError("input and output types must match, got ")
           << getInput().getType() << " and " << getOutput().getType();
  return ::mlir::success();
}
```

两个约定：

- 返回 `LogicalResult`，失败前必须 `emitOpError`（或 `emitError`），否则用户只看到 "failed to verify" 没有原因；
- 用 `emitOpError` 而不是 `emitError`，前者会自动带上 op 名和位置。

这条 `verify()` 本章实测编译通过（见示例三）。但注意：**它只在这个 op 被真正注册进 dialect 并跑 `mlir-opt` 时才会执行**——本章没有做注册，所以这里只验证了编译，不伪造运行输出。

## 自定义 parser/printer：两种写法，一条分界线

`assemblyFormat` 能覆盖大多数 op，但有些语法它表达不了：

- 需要根据前面的内容决定后面怎么解析（例如"如果有 `step` 关键字就多读一个操作数"）；
- 需要兼容历史语法，同时打印成新语法；
- 需要在解析时做语义决策（例如把一组操作数折叠成一个 variadic 段）。

这时用 `hasCustomAssemblyFormat = 1`，然后自己实现两个函数。ODS 侧只写一行：

```tablegen
def Toyv_NormOp : Toyv_Op<"norm", [Pure]> {
  let summary = "L2 norm with a handwritten assembly format";
  let arguments = (ins Toy_FloatTensor:$input);
  let results = (outs Toy_FloatTensor:$output);
  let hasCustomAssemblyFormat = 1;
}
```

生成物里只有声明，没有定义（实测，`scale.h.inc:179-180`）：

```cpp
  static ::mlir::ParseResult parse(::mlir::OpAsmParser &parser, ::mlir::OperationState &result);
  void print(::mlir::OpAsmPrinter &p);
```

注意参数名是 `p` 而不是 `_odsPrinter`——这是"没有生成实现"的信号。验证一下：

```powershell
Select-String .scratch\tblgen-work\scale.cpp.inc -Pattern 'NormOp::(parse|print)'
```

实测：无输出。`.cpp.inc` 里连一行都没有，两段实现都得你写。

手写实现（本章实测编译通过）：

```cpp
::mlir::ParseResult NormOp::parse(::mlir::OpAsmParser &parser,
                                  ::mlir::OperationState &result) {
  ::mlir::OpAsmParser::UnresolvedOperand input;
  ::mlir::Type inputType, outputType;
  if (parser.parseOperand(input) || parser.parseColonType(inputType) ||
      parser.parseArrow() || parser.parseType(outputType))
    return ::mlir::failure();
  if (parser.resolveOperand(input, inputType, result.operands))
    return ::mlir::failure();
  result.addTypes(outputType);
  return ::mlir::success();
}

void NormOp::print(::mlir::OpAsmPrinter &p) {
  p << ' ' << getInput() << " : " << getInput().getType() << " -> "
    << getOutput().getType();
}
```

手写 parser 的核心是两件事：

1. **先收集 `UnresolvedOperand` 和 `Type`**，不要急着建值；
2. **最后统一 `resolveOperand`**，把文本引用变成 SSA 值。这一步会做类型检查，也是自定义 parser 最常见的失败点。

`parseColonType` 是"解析 `:` 再解析一个类型"的合体；类似的还有 `parseComma`、`parseArrow`、`parseOptionalAttrDict`。用这些组合函数比手写 `parseColon()` + `parseType()` 少一半代码。

## 底层机制：verifier 是"拼"出来的

`hasVerifier` 的处理在 `OpDefinitionsGen.cpp:3899`：如果为真，就往 `verifyInvariants()` 里插一句对 `verify()` 的调用；否则只返回 `verifyInvariantsImpl()`（`:3866` 处生成 `verifyInvariantsImpl`）。

`verifyInvariantsImpl` 的内容完全由约束决定，顺序固定：

必需属性存在性检查（`requires attribute 'factor'`）→ 属性约束（`__mlir_ods_local_attr_constraint_*`）→ 每个 operand 的类型约束 → 每个 result 的类型约束。

这个顺序解释了报错优先级：**属性问题先于类型问题**。如果你同时写错了属性名和操作数类型，只会看到属性那条。

还有一层容易忽略：`verifyInvariants()` 不是你手动调用的，而是 `Operation::verify()` 经 `OperationName` 的虚函数表转调的。op 注册进 dialect 后，`RegisteredOperationName::Model::verifyInvariants` 会调用 `ConcreteOp::getVerifyInvariantsFn()`（`OperationSupport.h:582-583`），这就是 Ch 13 要拆的注册机制。

## 示例一：一个 `.td` 用满四种机制

`.scratch\tblgen-work\scale.td`：

```tablegen
// include 与 Dialect 声明同 Ch 11，这里只列本章新增的部分
def Toy_FloatTensor : Type<TensorOf<[AnyFloat]>.predicate,
                           "tensor of floating-point values">;

def Toyv_ScaleOp : Toyv_Op<"scale", [Pure]> {
  let summary = "scale a tensor by a bounded integer factor";
  let arguments = (ins Toy_FloatTensor:$input,
                       ConfinedAttr<I64Attr, [IntMinValue<1>, IntMaxValue<1024>]>:$factor);
  let results = (outs Toy_FloatTensor:$output);
  let assemblyFormat = "$input `,` $factor attr-dict `:` type($input) `->` type($output)";
  let hasVerifier = 1;
}

def Toyv_NormOp : Toyv_Op<"norm", [Pure]> {
  let summary = "L2 norm with a handwritten assembly format";
  let arguments = (ins Toy_FloatTensor:$input);
  let results = (outs Toy_FloatTensor:$output);
  let hasCustomAssemblyFormat = 1;
}
```

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$w = ".scratch\tblgen-work"

& "$mlir\mlir-tblgen.exe" -gen-op-decls "$w\scale.td" `
  -I ".scratch\llvm-23.1.4-src\mlir\include" -o "$w\scale.h.inc"
& "$mlir\mlir-tblgen.exe" -gen-op-defs "$w\scale.td" `
  -I ".scratch\llvm-23.1.4-src\mlir\include" -o "$w\scale.cpp.inc"
```

实测：两条命令退出码均为 0；`scale.h.inc` 425 行，`scale.cpp.inc` 605 行。

`verifyInvariantsImpl` 开头就是三段式（实测，`scale.cpp.inc:480-485`）：

```cpp
::llvm::LogicalResult ScaleOp::verifyInvariantsImpl() {
  auto tblgen_factor = getProperties().factor; (void)tblgen_factor;
  if (!tblgen_factor) return emitOpError("requires attribute 'factor'");

  if (::mlir::failed(__mlir_ods_local_attr_constraint_scale1(*this, tblgen_factor, "factor")))
    return ::mlir::failure();
```

先查"属性在不在"，再查"值合不合法"，最后才是类型。注意 `getProperties().factor`：ODS 属性默认走 **properties**，不是 `DictionaryAttr`，所以是零成本字段读取。这也让基类多出 `BytecodeOpInterface::Trait`。

## 示例二：在 `mlir-opt` 上制造同类错误

我们的 dialect 没注册，但内置 op 用的是同一套生成机制。下面每段都是"输入 + 实测输出"的转录（`mlir-opt` 23.1.4）。

### 情况 1：ODS 类型约束

```text
// bad_type.mlir
func.func @f(%a: f64, %b: f64) -> f64 {
  %0 = "arith.addi"(%a, %b) : (f64, f64) -> f64
  return %0 : f64
}

// $ mlir-opt bad_type.mlir
bad_type.mlir:2:8: error: 'arith.addi' op operand #0 must be signless-non-zero-bitwidth-integer-like, but got 'f64'
```

"must be ... but got ..." 是 `__mlir_ods_local_type_constraint_*` 的标准句式，与 `Toy_FloatTensor` 生成的 "must be tensor of floating-point values, but got" 同构。**用 generic form 才能绕过 parser 直接触发 verifier**。

### 情况 2：ODS 属性范围约束

```text
// bad_group.mlir
func.func @f() {
  nvvm.cp.async.bulk.wait_group -1
  return
}

// $ mlir-opt bad_group.mlir
bad_group.mlir:2:3: error: 'nvvm.cp.async.bulk.wait_group' op attribute 'group' failed to satisfy constraint: 32-bit signless integer attribute whose minimum value is 0
```

该 op 的 `.td` 是 `ConfinedAttr<I32Attr, [IntMinValue<0>]>`（源码依据：`NVVMOps.td:4251`）。文案 "whose minimum value is 0" 来自 `IntMinValue` 的 summary（`CommonAttrConstraints.td:796-798`），和 `factor` 同构。

### 情况 3：必需属性缺失与字面量越界

```text
// $ mlir-opt missing_attr.mlir
missing_attr.mlir:2:8: error: 'arith.constant' op requires attribute 'value'

// $ mlir-opt bad_const.mlir
bad_const.mlir:3:25: error: integer constant out of range for attribute
```

前者是 verifier（generic form 允许漏属性，验证阶段才拒绝）；后者是 **parser**——`300 : i8` 在构造 `IntegerAttr` 时就失败，没进入 verifier。

### 情况 4：手写 verifier 的语义检查

```text
// bad_shuffle3.mlir
func.func @f(%a: vector<4xf32>, %b: vector<4xf32>) -> vector<2xf32> {
  %0 = vector.shuffle %a, %b [0, 9] : vector<4xf32>, vector<4xf32>
  return %0 : vector<2xf32>
}

// $ mlir-opt bad_shuffle3.mlir
bad_shuffle3.mlir:3:10: error: 'vector.shuffle' op mask index #2 out of range
```

不是 ODS 生成的：源码是 `emitOpError("mask index #") << (idx + 1) << " out of range"`（`VectorOps.cpp:3335`）。`#2` 从 1 开始，因为源码写了 `idx + 1`。**跨元素检查 ODS 表达不了**。

| 你看到的消息 | 层次 | 该改哪里 |
|---|---|---|
| `expects different type than prior uses` | parser（SSA） | IR 文本 |
| `integer constant out of range for attribute` | parser（字面量） | IR 文本 |
| `operand #0 must be ...` | ODS 类型约束 | `arguments`/`results` |
| `attribute 'x' failed to satisfy constraint` | ODS 属性约束 | `ConfinedAttr` |
| `requires attribute 'x'` | ODS 必需字段 | IR 或 builder |
| `mask index #2 out of range` | 自定义 verifier | `verify()` 实现 |

## 示例三：手写部分真的能编译

把两份 `.inc`、dialect 类和手写的 `verify()` / `parse` / `print` 放进一个编译单元（`scale_impl.cpp`）。头文件部分（只列关键项，完整列表见 `scale_impl.cpp`）：

```cpp
#include "mlir/Bytecode/BytecodeOpInterface.h"   // properties / bytecode 接口
#include "mlir/IR/Builders.h"
#include "mlir/IR/OpImplementation.h"            // OpAsmParser / OpAsmPrinter
#include "mlir/IR/OpDefinition.h"

namespace toyv { /* ToyvDialect，与 Ch 11 的 ToyDialect 同构，略 */ }

#define GET_OP_CLASSES
#include "scale.h.inc"
#define GET_OP_CLASSES
#include "scale.cpp.inc"
```

用与 Ch 11 相同的 `cl /c` 脚本编译，实测：

```text
scale_impl.cpp
EXIT=0
```

第一次失败在 `scale.h.inc(300): error C2039: "BytecodeOpInterface": 不是 "mlir" 的成员`——`ScaleOp` 有 properties，生成物引用了 bytecode 接口，而 `OpDefinition.h` 不包含它。补 `mlir/Bytecode/BytecodeOpInterface.h` 后通过。这是"生成物不自包含"的第二个实例，第一个是 Ch 11 的 `Builders.h`。

> `guided`：编译通过不等于运行通过。要让这些 verifier 在 `mlir-opt` 里触发，还得把 dialect 注册进工具（Ch 16）。本章所有运行时报错都来自内置 op，我方 op 的证据止步于 tblgen 生成与 `cl /c`。

## 对比与取舍

| 需求 | 首选 | 备选 | 代价 |
|---|---|---|---|
| "必须是某种类型" | `TensorOf` / `Type` 约束 | 手写 `verify()` | 约束无法表达"跨值关系" |
| "属性在某个范围" | `ConfinedAttr` | 手写 `verify()` | 范围约束只支持单值比较 |
| "两个操作数 shape 相同" | 手写 `verify()` | — | 报错位置要自己维护 |
| "语法是线性的" | `assemblyFormat` | 手写 parse/print | DSL 表达力有限 |
| "语法依赖上下文" | 手写 parse/print | — | 两倍代码量，且要自己做类型解析 |

一条实用的判断标准：**能用约束表达的，绝不下沉到 `verify()`**。约束在 `.td` 里、和类型定义放一起、自动生成、自动有文档；`verify()` 在 `.cpp` 里，容易被后续改动绕过。

## 踩雷集锦

1. **`hasVerifier = 1` 但没实现 `verify()`**。tblgen 不报错，链接期报未定义符号。症状是"生成成功、编译失败"，错误信息在 `.cpp.inc` 里，很容易误判。
2. **`verify()` 里忘了 `emitOpError`**。返回 `failure()` 但没消息，用户只看到 `'toyv.scale' op failed to verify`。验证失败必须带原因。
3. **在 `verify()` 里假设 ODS 已经检查过**。顺序确实是先 ODS 后自定义，但**只有 `verifyInvariantsImpl` 成功才会调 `verify()`**——如果你在 `verify()` 里手动 `cast` 一个本可能为空的属性，仍然要先判空。
4. **`ConfinedAttr` 用在可选属性上写成 `OptionalAttr<ConfinedAttr<...>>` 的顺序颠倒**。正确写法是 `OptionalAttr<ConfinedAttr<I64Attr,[...]>>`；反过来 `ConfinedAttr<OptionalAttr<...>>` 不成立（`ConfinedAttr` 的第一个参数必须是 `Attr`，`OptionalAttr` 返回的也是 `Attr`，但语义会丢）。
5. **以为 `assemblyFormat` 里能写条件分支**。不能。格式串没有 if；需要分支就转 `hasCustomAssemblyFormat`。
6. **自定义 parser 忘了 `resolveOperand`**。解析成功但 `result.operands` 是空的，后续 verify 报"操作数数量不对"，位置却指向 op 起始处，极难定位。

## 进阶：再往深一层

- 试给 `Toy_FloatTensor` 加 `cppTypeParam = "::mlir::TensorType"`，观察生成的 `getInput()` 从 `TypedValue<Type>` 变成 `TypedValue<TensorType>`，以及 verifier 里 `isa` 检查是否还在。这是"用类型信息换代码可读性"的典型权衡。

## 动手练习

> 在 `.scratch\tblgen-work` 下做。每题给出"命令 + 退出码 + 关键输出"。

### 练习 1：把范围收紧到 1..8

**任务**：把 `IntMaxValue<1024>` 改成 `IntMaxValue<8>`，重新生成两份 `.inc`。

**测试用例**：
1. `scale.cpp.inc` 里出现 `<= 8`，不再出现 `<= 1024`。
2. 报错文案变成 "whose maximum value is 8"。

<details>
<summary>参考解答</summary>

```powershell
(Get-Content $w\scale.td -Raw).Replace('IntMaxValue<1024>','IntMaxValue<8>') |
  Set-Content $w\scale8.td -Encoding ascii
& "$mlir\mlir-tblgen.exe" -gen-op-defs "$w\scale8.td" -I ".scratch\llvm-23.1.4-src\mlir\include" -o "$w\scale8.cpp.inc"
Select-String $w\scale8.cpp.inc -Pattern 'maximum value is 8'
```

</details>

**常见卡点**：只改 decls 不改 defs；报错文案在 defs 里。

### 练习 2：加一条跨值检查

**任务**：给 `Toyv_ScaleOp::verify()` 加一条：`factor` 必须是 2 的幂。

**测试用例**：
1. 编译通过（`cl /c` 退出码 0）。
2. 用 `(f & (f - 1)) != 0` 判断。
3. 失败消息包含 "power of two"。

<details>
<summary>参考解答</summary>

```cpp
  uint64_t f = getFactor();
  if (f == 0 || (f & (f - 1)) != 0)
    return emitOpError("factor must be a power of two, got ") << f;
```

`ConfinedAttr` 已保证 `f >= 1`，但 `verify()` 不该依赖超出自己检查范围的前提。

</details>

**常见卡点**：用 `getFactorAttr().getInt()` 拿到有符号 `int64_t`，与 `uint64_t` 比较会触发编译警告。

### 练习 3：给 `NormOp` 补一个 attr-dict

**任务**：在 `NormOp::parse` / `print` 里支持可选属性字典。

**测试用例**：
1. parse 里调用 `parseOptionalAttrDict(result.attributes)`。
2. print 里调用 `printOptionalAttrDict`。
3. 编译通过。

<details>
<summary>参考解答</summary>

parse 侧在读完类型后加 `if (parser.parseOptionalAttrDict(result.attributes)) return ::mlir::failure();`；print 侧在末尾加 `p.printOptionalAttrDict(getOperation()->getAttrs());`。

</details>

**常见卡点**：先解析 attr-dict 再解析类型。属性字典总在语法末尾。

### 练习 4：判断报错层次

**任务**：对下面三段 IR 跑 `mlir-opt`，先预测层次（parser / ODS / 自定义 verifier），再实测对照。

```mlir
func.func @a(%x: i32, %y: f32) -> i32 { %0 = arith.addi %x, %y : i32
  return %0 : i32 }
func.func @b(%x: f64) -> f64 { %0 = "arith.addi"(%x, %x) : (f64, f64) -> f64
  return %0 : f64 }
func.func @c() -> i8 { %0 = arith.constant 300 : i8
  return %0 : i8 }
```

**测试用例**：
1. a 报 parser 层（SSA 类型一致性）。
2. b 报 ODS 层（operand #0 约束）。
3. c 报 parser 层（字面量越界）。

<details>
<summary>参考解答</summary>

a 与 c 都在解析阶段失败，b 在 verifier 阶段失败。a 的失败来自同一个 SSA 值被两种类型使用；c 来自 `IntegerAttr` 构造时越界；b 的 IR 语法完全合法，只是约束不满足。

</details>

**常见卡点**：以为 b 会先报"操作数类型不一致"。b 的两个操作数都是 f64，类型一致，只是不满足"整数"约束。

## 本章重点整理

- 三层检查：parser（语法/SSA/字面量）→ ODS verifier（逐值约束）→ 自定义 `verify()`（跨值语义）。
- `TypeConstraint` 的 `cppType` 决定访问器返回类型；默认 `::mlir::Type`。
- `ConfinedAttr<T, [IntMinValue<a>, IntMaxValue<b>]>` 是闭区间，报错文案由约束 summary 拼接而成。
- `hasVerifier = 1` 生成 `verify()` 声明，并在 `verifyInvariantsImpl()` 成功后才调用它。
- `hasCustomAssemblyFormat = 1` 只生成声明；`.cpp.inc` 里没有 parse/print 实现。
- 手写 parser 的模式是"收集 UnresolvedOperand → 解析类型 → resolveOperand"。

## 自我检核

- [ ] 能从一条 `mlir-opt` 报错反推它属于 parser、ODS 还是自定义 verifier
- [ ] 能写出一个带 `ConfinedAttr` 范围的 op 并解释生成的检查代码
- [ ] 知道 `hasVerifier = 1` 时 `verify()` 与 `verifyInvariantsImpl()` 的调用顺序
- [ ] 能说清 `assemblyFormat` 与 `hasCustomAssemblyFormat` 的互斥关系
- [ ] 记得 `Type<...>` 的第二个参数会进报错文案
- [ ] 能复现"漏 include `BytecodeOpInterface.h`"的编译错误

## 延伸阅读

- **`mlir/include/mlir/IR/Constraints.td`**：读 `TypeConstraint`（`:166`）、`AttrConstraint`（`:175`）、`PropConstraint`（`:180`）三个基类。学"约束的抽象层次"；本章所有自定义约束都从这里派生。
- **`mlir/include/mlir/IR/CommonAttrConstraints.td`**：读 `ConfinedAttr`（`:766`）与 `IntMinValue`/`IntMaxValue`（`:796`/`:800`）。学"范围约束如何拼 predicate"；与本章生成的 `&&` 长条件对照。
- **`mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp`**：搜 `hasVerifier`（`:3899`）与 `verifyInvariantsImpl`（`:3866`）。学"验证代码的生成顺序"；解释示例一里"属性先于类型"的报错优先级。
- **`mlir/lib/Dialect/Vector/IR/VectorOps.cpp`**：看 `ShuffleOp::verify` 附近的 mask 检查（`:3335`）。学"真实 op 怎么写跨值验证"；与本章的 `factor` 检查对照。

→ [Ch 13 解剖 TableGen 生成物](./13-tablegen-generated-code.md)