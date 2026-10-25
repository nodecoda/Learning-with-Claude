# 练习 C：实现一个 standalone dialect

> **目标**：从零写一个 out-of-tree 方言 `toy`，包含一个自定义类型、一个自定义属性、五个 op、类型级与 op 级 verifier、一条 canonicalization pattern、一个接口实现，并且能用自己编译的 `toy-opt` 跑通解析、打印、canonicalize、bytecode 往返。这一份练习把 Ch 11–16 的所有零件拼成一个能编译的整体。

## 为什么需要这个

读章节和写方言之间的差距，不在概念，在**接线**。你会遇到这些问题：

- `.td` 里的定义对不对？只有 tblgen 能告诉你。
- 生成物里的函数签名到底是什么？只有编译能告诉你。
- 类型注册了吗？只有解析一段 IR 才能验证。
- 你的 parser 和 printer 一致吗？只有 bytecode 往返能验证。

这四条都无法靠读文档确认，所以验收标准是**每条命令都有真实输出**，不是「代码看起来对」。

本练习的所有结果都在本机跑过：VS2022 14.43 + MLIR 23.1.4（conda-forge win-64）。无法在本机复现的部分（CMake 路径）明确标 `guided`，并给出可用的替代路径。

## 任务规格

实现方言 `toy`，命名空间 `toy`，必须包含：

| 组件 | 名字 | 要求 |
| --- | --- | --- |
| 类型 | `!toy.pointer<T>` | ODS 声明式格式；pointee 必须是浮点；`genVerifyDecl` |
| 类型 | `!toy.array<N x T>` | 手写 `parse` / `print`；N 必须为正 |
| 属性 | `#toy.const<V : T>` | 参数是整数 + 类型；声明式格式 |
| op | `toy.alloc` | `() -> !toy.pointer<T>`；verifier 检查结果是指针且指向浮点 |
| op | `toy.load` | `(!toy.pointer<T>) -> T`；`MemRead`；verifier 检查结果类型 = pointee |
| op | `toy.store` | `(T, !toy.pointer<T>)`；`MemWrite`；verifier 检查 value 类型 = pointee |
| op | `toy.add` | `(f32, f32) -> f32`；`Pure` + `SameOperandsAndResultType` |
| op | `toy.cast` | `(T) -> T'`；`InferTypeOpInterface`；canonicalize 折叠双重 cast |
| 依赖 | `arith` | 通过 `dependentDialects` 声明，不在 driver 里注册 |

## 目录结构

```text
toy-standalone/
  CMakeLists.txt
  include/toy/
    CMakeLists.txt          # 8 条 mlir_tablegen
    ToyOps.td               # dialect + type + attr + op，全在一个文件里
    ToyDialect.h
    ToyTypes.h
    ToyAttrs.h
    ToyOps.h
  lib/
    ToyDialect.cpp          # initialize()
    ToyTypes.cpp            # registerTypes + verify + 手写 parse/print
    ToyAttrs.cpp            # registerAttributes
    ToyOps.cpp              # verifier + inferReturnTypes + canonicalization
  tools/
    toy-opt.cpp             # MlirOptMain + 一个自定义 pass
    toy-unique.cpp          # 验证 uniquing / getChecked 的小程序
  test/
    asm.mlir canon.mlir infer.mlir infer_bad.mlir mixed.mlir
    bad_array.mlir bad_ptr.mlir bad_store.mlir bad_name.mlir
```

为什么把 type/attr/op 放一个 `.td`？因为 `mlir_tablegen` 的 `LLVM_TARGET_DEFINITIONS` 只接受一个入口文件，而 TableGen 会顺着 `include` 展开。小项目一个文件最省事；真实项目按 `ToyDialect.td` / `ToyTypes.td` / `ToyAttrs.td` / `ToyOps.td` 拆，拆完记得每个生成命令指向**包含对应 def 的那个入口**，否则会生成空文件且不报错。

## 第 1 步：写 `.td`

```tablegen
#ifndef TOY_OPS
#define TOY_OPS

include "mlir/IR/OpBase.td"
include "mlir/IR/AttrTypeBase.td"
include "mlir/Interfaces/InferTypeOpInterface.td"
include "mlir/Interfaces/SideEffectInterfaces.td"

def Toy_Dialect : Dialect {
  let name = "toy";
  let cppNamespace = "::toy";
  let summary = "teaching dialect used by the MLIR course, practice C";
  let useDefaultTypePrinterParser = 1;
  let useDefaultAttributePrinterParser = 1;
  let dependentDialects = ["::mlir::arith::ArithDialect"];
  let extraClassDeclaration = [{
    void registerTypes();
    void registerAttributes();
  }];
}

class Toy_Op<string mnemonic, list<Trait> traits = []>
    : Op<Toy_Dialect, mnemonic, traits>;

class Toy_Type<string name, string typeMnemonic, list<Trait> traits = []>
    : TypeDef<Toy_Dialect, name, traits> {
  let mnemonic = typeMnemonic;
}

class Toy_Attr<string name, string attrMnemonic, list<Trait> traits = []>
    : AttrDef<Toy_Dialect, name, traits> {
  let mnemonic = attrMnemonic;
}

def Toy_PointerType : Toy_Type<"Pointer", "pointer"> {
  let parameters = (ins "::mlir::Type":$pointee);
  let assemblyFormat = "`<` $pointee `>`";
  let genVerifyDecl = 1;
}

def Toy_ArrayType : Toy_Type<"Array", "array"> {
  let parameters = (ins "int64_t":$size, "::mlir::Type":$elementType);
  let hasCustomAssemblyFormat = 1;
  let genVerifyDecl = 1;
}

def Toy_ConstAttr : Toy_Attr<"Const", "const"> {
  let parameters = (ins "int64_t":$value, "::mlir::Type":$type);
  let assemblyFormat = "`<` $value `:` $type `>`";
}

def Toy_AllocOp : Toy_Op<"alloc"> {
  let results = (outs AnyType:$result);
  let assemblyFormat = "attr-dict `:` type($result)";
  let hasVerifier = 1;
}

def Toy_LoadOp : Toy_Op<"load", [MemoryEffects<[MemRead]>]> {
  let arguments = (ins AnyType:$ptr);
  let results = (outs AnyType:$value);
  let assemblyFormat = "$ptr attr-dict `:` type($ptr) `->` type($value)";
  let hasVerifier = 1;
}

def Toy_StoreOp : Toy_Op<"store", [MemoryEffects<[MemWrite]>]> {
  let arguments = (ins AnyType:$value, AnyType:$ptr);
  let assemblyFormat = "$value `,` $ptr attr-dict `:` type($value) `,` type($ptr)";
  let hasVerifier = 1;
}

def Toy_AddOp : Toy_Op<"add", [Pure, SameOperandsAndResultType]> {
  let arguments = (ins F32:$lhs, F32:$rhs);
  let results = (outs F32:$result);
  let assemblyFormat = "$lhs `,` $rhs attr-dict `:` type($lhs)";
}

def Toy_CastOp : Toy_Op<"cast",
    [Pure, DeclareOpInterfaceMethods<InferTypeOpInterface>]> {
  let arguments = (ins AnyType:$input);
  let results = (outs AnyType:$result);
  let assemblyFormat = "$input attr-dict `:` type($input) `to` type($result)";
  let hasCanonicalizer = 1;
}

#endif // TOY_OPS
```

三个刻意的选择，理由在 Ch 14 都讲过：

- 指针/数组的 op 用 `AnyType` 而不是 `Toy_PointerType`，否则 ODS 会 strip 掉类型前缀，打印成 `<f32>`。
- `Toy_Type` 的参数叫 `typeMnemonic` 而不是 `mnemonic`，避开自赋值错误。
- `Toy_CastOp` 用 `DeclareOpInterfaceMethods`，只生成声明，实现自己写。

## 第 2 步：头文件

```cpp
// include/toy/ToyDialect.h
#include "mlir/IR/Dialect.h"
#include "toy/ToyOpsDialect.h.inc"

// include/toy/ToyTypes.h
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/Types.h"
#define GET_TYPEDEF_CLASSES
#include "toy/ToyOpsTypes.h.inc"

// include/toy/ToyAttrs.h
#include "mlir/IR/Attributes.h"
#include "mlir/IR/BuiltinAttributes.h"
#define GET_ATTRDEF_CLASSES
#include "toy/ToyOpsAttrs.h.inc"

// include/toy/ToyOps.h
#include "toy/ToyAttrs.h"
#include "toy/ToyDialect.h"
#include "toy/ToyTypes.h"
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/OpDefinition.h"
#include "mlir/Interfaces/InferTypeOpInterface.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#define GET_OP_CLASSES
#include "toy/ToyOps.h.inc"
```
顺序有讲究：`GET_*_CLASSES` 之前必须 include 对应接口的头文件，否则生成物里的 `::mlir::InferTypeOpInterface` 是未声明标识符。

## 第 3 步：C++ 骨架

### Dialect

```cpp
#include "toy/ToyDialect.h"
#include "toy/ToyAttrs.h"
#include "toy/ToyOps.h"
#include "toy/ToyTypes.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/IR/DialectImplementation.h"

using namespace mlir;
using namespace toy;

#include "toy/ToyOpsDialect.cpp.inc"

void ToyDialect::initialize() {
  addOperations<
#define GET_OP_LIST
#include "toy/ToyOps.cpp.inc"
      >();
  registerTypes();
  registerAttributes();
}
```

`#include "mlir/Dialect/Arith/IR/Arith.h"` 是必须的：`dependentDialects` 生成的构造函数会调 `loadDialect<arith::ArithDialect>()`，没有这个头文件就是 `"arith": 不是 "mlir" 的成员`。

### 类型

```cpp
#define GET_TYPEDEF_CLASSES
#include "toy/ToyOpsTypes.cpp.inc"

void ToyDialect::registerTypes() {
  addTypes<
#define GET_TYPEDEF_LIST
#include "toy/ToyOpsTypes.cpp.inc"
      >();
}

LogicalResult PointerType::verify(function_ref<InFlightDiagnostic()> emitError,
                                  Type pointee) {
  if (!isa<FloatType>(pointee))
    return emitError() << "toy.pointer needs a float pointee, got " << pointee;
  return success();
}

Type ArrayType::parse(AsmParser &parser) {
  int64_t size = 0;
  Type elementType;
  if (parser.parseLess() || parser.parseInteger(size) ||
      parser.parseKeyword("x") || parser.parseType(elementType) ||
      parser.parseGreater())
    return Type();

  SMLoc loc = parser.getCurrentLocation();
  return getChecked([&]() -> InFlightDiagnostic { return parser.emitError(loc); },
                    parser.getContext(), size, elementType);
}

void ArrayType::print(AsmPrinter &printer) const {
  printer << "<" << getSize() << " x " << getElementType() << ">";
}

LogicalResult ArrayType::verify(function_ref<InFlightDiagnostic()> emitError,
                                int64_t size, Type elementType) {
  if (size <= 0)
    return emitError() << "toy.array size must be positive, got " << size;
  if (!elementType)
    return emitError() << "toy.array needs an element type";
  return success();
}
```

注意：**不要**在这里手写 `ToyDialect::parseType` / `printType`。`useDefaultTypePrinterParser = 1` 时它们由 `ToyOpsTypes.cpp.inc` 生成，重写就是 C2084。

### Op 的 verifier 与 canonicalize

```cpp
LogicalResult AllocOp::verify() {
  auto pointer = dyn_cast<PointerType>(getResult().getType());
  if (!pointer)
    return emitOpError("result must be a !toy.pointer, got ")
           << getResult().getType();
  if (!isa<FloatType>(pointer.getPointee()))
    return emitOpError("must allocate a pointer to a float, got ")
           << pointer.getPointee();
  return success();
}

LogicalResult StoreOp::verify() {
  auto pointer = dyn_cast<PointerType>(getPtr().getType());
  if (!pointer)
    return emitOpError("operand must be a !toy.pointer, got ")
           << getPtr().getType();
  if (pointer.getPointee() != getValue().getType())
    return emitOpError("stored value type ") << getValue().getType()
                                             << " does not match pointee type "
                                             << pointer.getPointee();
  return success();
}
```

```cpp
namespace {
struct FoldDoubleCast : public OpRewritePattern<CastOp> {
  using OpRewritePattern<CastOp>::OpRewritePattern;
  LogicalResult matchAndRewrite(CastOp op, PatternRewriter &rewriter) const override {
    auto inner = op.getInput().getDefiningOp<CastOp>();
    if (!inner)
      return failure();
    if (inner.getInput().getType() != op.getResult().getType())
      return failure();
    rewriter.replaceOp(op, inner.getInput());
    return success();
  }
};
} // namespace

void CastOp::getCanonicalizationPatterns(RewritePatternSet &results,
                                         MLIRContext *context) {
  results.add<FoldDoubleCast>(context);
}
```

`inner.getInput().getType() != op.getResult().getType()` 这个判断不能省：`toy.cast` 在语义上是"翻转浮点宽度"，`f32 -> f64 -> f32` 可以折叠，但 `f32 -> f64 -> ?` 不行。少了它，`replaceOp` 会产生类型不匹配的 IR，canonicalize 之后验证失败。

### 推断接口

```cpp
static Type flippedFloat(MLIRContext *context, Type type) {
  if (type.isF32()) return Float64Type::get(context);
  if (type.isF64()) return Float32Type::get(context);
  return Type();
}

LogicalResult CastOp::inferReturnTypes(MLIRContext *context,
                                       std::optional<Location> location,
                                       ValueRange operands, DictionaryAttr attributes,
                                       PropertyRef properties, RegionRange regions,
                                       SmallVectorImpl<Type> &inferred) {
  Type input = operands.front().getType();
  if (Type scalar = flippedFloat(context, input)) {
    inferred.assign({scalar});
    return success();
  }
  if (auto tensor = dyn_cast<RankedTensorType>(input)) {
    Type element = flippedFloat(context, tensor.getElementType());
    if (!element)
      return emitOptionalError(location, "toy.cast cannot rewrite element type ",
                               tensor.getElementType());
    inferred.assign({RankedTensorType::get(tensor.getShape(), element)});
    return success();
  }
  return emitOptionalError(location, "toy.cast does not support ", input);
}
```

签名里的 `PropertyRef` 必须写，少一个参数就是"没有匹配的声明"。

## 第 4 步：driver

```cpp
int main(int argc, char **argv) {
  registerAllPasses();
  PassRegistration<InferResultTypesPass>();

  DialectRegistry registry;
  registry.insert<toy::ToyDialect, func::FuncDialect>();

  return asMainReturnCode(
      MlirOptMain(argc, argv, "toy optimizer driver\n", registry));
}
```

只注册 toy 和 func，arith 靠 `dependentDialects` 进来——这是 Ch 16 的 A/B 实验。

## 第 5 步：测试用例

**用例 1：语法往返**

```mlir
func.func @memory() {
  %p = toy.alloc : !toy.pointer<f32>
  %v = toy.load %p : !toy.pointer<f32> -> f32
  toy.store %v, %p : f32, !toy.pointer<f32>
  return
}
```

期望：打印出完全一样的三行（SAA 名字可能变成 `%0`/`%1`）。

**用例 2：纯 op 被 DCE、写内存的保留**

```mlir
func.func @dead_pure(%x: f32, %y: f32) {
  %dead = toy.add %x, %y : f32
  return
}
func.func @dead_alloc_store() {
  %p = toy.alloc : !toy.pointer<f32>
  %c = arith.constant 1.000000e+00 : f32
  toy.store %c, %p : f32, !toy.pointer<f32>
  return
}
```

期望：`toy.add` 消失；alloc/store 保住。

**用例 3：双重 cast 折叠**

```mlir
func.func @double_cast(%x: f32) -> f32 {
  %a = toy.cast %x : f32 to f64
  %b = toy.cast %a : f64 to f32
  return %b : f32
}
```

期望：函数体只剩 `return %arg0 : f32`。

**用例 4：接口派发**

```mlir
func.func @casts(%x: f32, %t: tensor<2x3xf64>) {
  %a = toy.cast %x : f32 to f64
  %b = toy.cast %t : tensor<2x3xf64> to tensor<2x3xf32>
  return
}
```

期望：两条 remark，分别是 `f64` 和 `tensor<2x3xf32>`。

**用例 5：坏类型**

```mlir
func.func @bad(%x: f32) {
  %a = toy.cast %x : f32 to i32
  return
}
```

期望：`inferred type(s) 'f64' are incompatible with return type(s) of operation 'i32'`。

## 参考解答

<details>
<summary>点击展开：完整实现与实测输出</summary>

### 实测 3：canonicalize

```powershell
& "...\bin\toy-opt.exe" .\test\canon.mlir --canonicalize
```

```mlir
module {
  func.func @dead_pure(%arg0: f32, %arg1: f32) {
    return
  }
  func.func @dead_load(%arg0: !toy.pointer<f32>) {
    return
  }
  func.func @dead_alloc_store() {
    %cst = arith.constant 1.000000e+00 : f32
    %0 = toy.alloc : !toy.pointer<f32>
    toy.store %cst, %0 : f32, !toy.pointer<f32>
    return
  }
  func.func @double_cast(%arg0: f32) -> f32 {
    return %arg0 : f32
  }
}
```

注意 `dead_load` 也被删了：`MemRead` + 结果未使用 = 可删（Ch 15 的 DCE 规则）。

### 实测 4：接口派发

`--toy-infer-types` 打出两条 remark，分别是 `f64` 与 `tensor<2x3xf32>`（完整输出见 Ch 15 示例一）。
## 构建：两条路径

### 路径 A：CMake（本机 `guided`）

标准写法，与上游 `examples/standalone` 同构：

```cmake
cmake_minimum_required(VERSION 3.20)
project(toy-standalone LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17 CACHE STRING "C++ standard")
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(MLIR REQUIRED CONFIG)
list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(TableGen)
include(AddLLVM)
include(AddMLIR)

include_directories(${LLVM_INCLUDE_DIRS} ${MLIR_INCLUDE_DIRS}
                    ${PROJECT_SOURCE_DIR}/include ${PROJECT_BINARY_DIR}/include)
add_definitions(${LLVM_DEFINITIONS})

add_subdirectory(include/toy)
add_subdirectory(lib)
add_subdirectory(tools)
```

`include/toy/CMakeLists.txt`：

```cmake
set(LLVM_TARGET_DEFINITIONS ToyOps.td)
mlir_tablegen(ToyOps.h.inc -gen-op-decls)
mlir_tablegen(ToyOps.cpp.inc -gen-op-defs)
mlir_tablegen(ToyOpsTypes.h.inc -gen-typedef-decls -typedefs-dialect=toy)
mlir_tablegen(ToyOpsTypes.cpp.inc -gen-typedef-defs -typedefs-dialect=toy)
mlir_tablegen(ToyOpsAttrs.h.inc -gen-attrdef-decls -attrdefs-dialect=toy)
mlir_tablegen(ToyOpsAttrs.cpp.inc -gen-attrdef-defs -attrdefs-dialect=toy)
mlir_tablegen(ToyOpsDialect.h.inc -gen-dialect-decls -dialect=toy)
mlir_tablegen(ToyOpsDialect.cpp.inc -gen-dialect-defs -dialect=toy)
add_public_tablegen_target(ToyOpsIncGen)
```

**本机实测失败**，原因与配置无关，与 CMake 4.0.0-rc5 的 try-compile 有关：

```text
testCXXCompiler.cxx(5): fatal error C1069: 无法读取编译器命令行
```

定位过程：把失败命令行原样拿出来单独跑，`cl /nologo /TP ... /FdCMakeFiles\cmTC_xxx.dir\ /FS -c t.cpp` 就能复现；去掉 `/Fd...\` 就正常。`/Fd` 后面的目录以反斜杠结尾，紧跟空格，`cl` 把空格的转义吃掉了，于是"读不到命令行"。这是环境问题，不是你的 CMakeLists 的问题。换一个 CMake 正式版（或让 `-D` 到不带尾反斜杠的路径）大概率就好。

### 路径 B：直接调 `cl`（本机 `实测`）

不依赖 CMake，八条 tblgen + 一次编译 + 一次链接：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library"
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-op-decls -o gen\toy\ToyOps.h.inc

cl /nologo /std:c++17 /EHsc /MD /O2 /utf-8 /DNDEBUG /D_CRT_SECURE_NO_WARNINGS `
   /I "$mlir\include" /I .\include /I .\gen /Foobj\ `
   /c lib\ToyDialect.cpp lib\ToyTypes.cpp lib\ToyAttrs.cpp lib\ToyOps.cpp
```

链接时有两个 Windows 特有的坑：

- **`shell32.lib` 找不到**：Windows SDK 10.0.26100 缺它，要补 `/LIBPATH:...\10.0.17763.0\um\x64` 和 `...\ucrt\x64`。
- **`zlib` / `zstd`**：静态链接 MLIR 后 `LLVMSupport` 会引用压缩符号，要显式加 `zlibstatic.lib` 与 `libzstd.lib`。用 `zlib.lib`（import lib）会让 exe 依赖 `zlib.dll`，运行时 `0xC0000135`（找不到 DLL）。

本机最终可用的链接方式是"把所有 `MLIR*.lib` + `LLVM*.lib` 放进一个 response file"，因为静态库之间的依赖太密，手挑会陷入"补一个又冒出三个"的循环。

## 五条卡点

**1. TableGen 报 `Recursion / self-assignment forbidden`**

```text
ToyOps.td:31:7: error: Recursion / self-assignment forbidden
  let mnemonic = mnemonic;
```

原因：class 参数和字段同名。改参数名（`typeMnemonic` / `attrMnemonic`）。

**2. TypeDef 缺格式声明**

```text
error: must specify either 'assemblyFormat' or 'hasCustomAssemblyFormat' when 'mnemonic' is set
```

而且 `-gen-typedef-decls` 会顺带打一份 crash 报告。TypeDef 没有"默认格式"，必须二选一。

**3. 手写 `parseType` 撞上生成物**

```text
error C2084: 函数“mlir::Type toy::ToyDialect::parseType(mlir::DialectAsmParser &) const”已有主体
```

`useDefaultTypePrinterParser = 1` 已经把函数体生成到 `ToyOpsTypes.cpp.inc`。删掉你写的那份。

**4. `generatedTypeParser` 找不到**

```text
error C3861: “generatedTypeParser”: 找不到标识符
```

它生成在 `ToyOpsTypes.cpp.inc` 里，是**该 TU 内的 static 函数**。你把 `parseType` 写在 `ToyDialect.cpp`、而 `#include "ToyOpsTypes.cpp.inc"` 在 `ToyTypes.cpp`，就看不到。解决办法就是用生成的版本，别手写。

**5. 类型前缀被 strip**

打印出 `toy.alloc : <f32>` 而不是 `!toy.pointer<f32>`。原因是 ODS 对具体类型约束生成了 `printStrippedAttrOrType`。把 operand/result 放宽成 `AnyType`，验证交给 op 的 verifier。

## 自我检核

- [ ] `.td` 能被 `mlir-tblgen` 跑过全部八条命令，没有 warning
- [ ] `toy-opt test/asm.mlir` 能打印出 `!toy.pointer<f32>` 与 `!toy.array<4 x f32>`
- [ ] `--mlir-print-op-generic` 下类型语法不变
- [ ] `--canonicalize` 删掉 `toy.add` 与未使用的 `toy.load`，保留 `toy.store`
- [ ] 双重 cast 被折叠成 `return %arg0`
- [ ] `--toy-infer-types` 打出两条 `tensor<2x3xf32>` / `f64` 的 remark
- [ ] 四类错误输入都有可读诊断
- [ ] bytecode 往返后 IR 完全一致
- [ ] 能在生成物里指出 `getEffects` / `inferReturnTypes` / `printType` 各在哪一行
- [ ] 能解释为什么 driver 不注册 arith 也能解析 `arith.constant`

## 延伸阅读

- **`mlir/examples/standalone/`**：整个目录；学 out-of-tree 方言的标准目录与 CMake 组织；和本练习的结构一一对应。
- **`mlir/docs/DefiningDialects/Operations.md`**：读 "Assembly Format" 与 "Verification" 两节；学 `assemblyFormat` 的全部指令；能解释本练习里每个 op 的格式串。
- **`mlir/include/mlir/IR/OpBase.td`**：查 `AnyType` / `F32` / `MemoryEffects` 的定义；学约束与效果的可用拼装方式。
- **`mlir/examples/toy/Ch7/`**：读 `mlir/Dialect.cpp` 与 `ToyCombine.cpp`；学一个更完整的教学方言怎么做 canonicalize；对照本练习的 `FoldDoubleCast`。
- **`compilers/mlir_core/13-tablegen-generated-code.md`**：重读一遍；把本练习生成物里的 `getODSOperands` / `verifyInvariants` 对上号。

→ [Ch 17 Dialect Conversion 框架](./17-dialect-conversion.md)