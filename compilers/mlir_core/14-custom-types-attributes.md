# Ch 14 自定义 Type 与 Attribute

> **目标**：让你能自己定义 `!toy.pointer<f32>` 这种带参数的类型，并说清楚三件事：它为什么在 `MLIRContext` 里是唯一对象、`get()` 和 `getChecked()` 的差别在哪、自定义的解析/打印是怎么接进 `DialectAsmParser` / `DialectAsmPrinter` 的。学完你应该能只读一个 `TypeDef` 的 `.td` 就预测出生成物的接口。

## 为什么需要这个？

内置类型只覆盖"机器语言"的公共部分：`i32`、`f32`、`memref<...>`、`tensor<...>`。一旦你的方言有自己的语义单元——Toy 语言里的"指向 f32 的指针"、一个 fixed-size array、一个带有 shape 的 handle——你只有两条路：

- **骗进去**：用 `i32` 或 `!llvm.ptr` 顶替，把真实语义塞进 op 的 attribute。
- **定义出来**：写一个 dialect type，让 parser、printer、verifier、bytecode、alias 全部由框架统一处理。

第一条路会在两个地方付出代价。第一是**验证**：`i32` 顶替指针时，没有任何地方能阻止你把 `i32` 当指针传给 `toy.store`，你只能在每个 op 的 verifier 里手工检查 attribute。第二是**打印**：IR dump 里全是 `i32`，读 IR 的人必须先查 attribute 才知道这是指针。

定义类型的成本是一次性的，收益贯穿整个 pipeline：CSE、canonicalize、bytecode、`mlir-opt` 的每一个 pass 都会正确地把它当成一个不透明但可比对的类型。

这门课后面的练习 C 会完整实现下面这些片段。本章先把机制拆开。

## 先建立直觉：一个 Type 其实是四层东西

看到 `!toy.pointer<f32>`，大多数人的直觉是"一个字符串"。在 MLIR 里它对应四层完全不同的对象：

```text
源码文本          !toy.pointer<f32>
    ↓ 解析
mlir::Type       一个 8 字节的句柄（指向 impl 的指针）
    ↓ 解引用
PointerType      类型安全的 C++ wrapper（模板类，零运行时开销）
    ↓ getImpl()
PointerTypeStorage  真正存数据的对象，由 StorageUniquer 唯一化
```

关键点：**`mlir::Type` 本身没有"值语义"**。两个 `PointerType` 相等，比较的是 impl 指针，不是内容。内容相等的两个请求必须拿到同一个 impl 指针——这件事由 storage uniquing 保证，也就把"类型相等"降级成了"指针相等"，这是 MLIR 里几乎所有类型比较都快的原因。

## 核心概念一：TypeDef 生成什么

我们的 `toy` 方言里，指针类型的定义只有五行：

```tablegen
def Toy_PointerType : Toy_Type<"Pointer", "pointer"> {
  let summary = "pointer to a value of a toy element type";
  let parameters = (ins "::mlir::Type":$pointee);
  let assemblyFormat = "`<` $pointee `>`";
  let genVerifyDecl = 1;
}
```

`mlir-tblgen -gen-typedef-decls` 为它生成的公开接口（本机生成物 `gen/toy/ToyOpsTypes.h.inc`）：

```text
28: class PointerType : public ::mlir::Type::TypeBase<PointerType, ::mlir::Type, detail::PointerTypeStorage> {
34:   static PointerType get(::mlir::MLIRContext *context, ::mlir::Type pointee);
35:   static PointerType getChecked(::llvm::function_ref<::mlir::InFlightDiagnostic()> emitError, ::mlir::MLIRContext *context, ::mlir::Type pointee);
```

三件事值得记住：

- **参数直接变成函数参数**。`$pointee` 出现在 `get` / `getChecked` 的签名里，调用点没有 `ArrayRef<Attribute>` 这种通用容器。
- **`verify` 的声明来自 `genVerifyDecl`**。不写这一行，就没有 `verify` 挂钩，坏参数只能靠调用方自觉。
- **`getChecked` 的第一个参数是 `emitError`**，不是我一开始以为的 `Location`。`StorageUserBase` 里还有一个 `getChecked(const Location &loc, ...)` 的重载，它内部再转成 `emitError` 形式（`include/mlir/IR/StorageUniquerSupport.h:189`）。

生成物里还有一处容易忽略：`PointerType` 的 **storage class 是 `detail::PointerTypeStorage`**，它在 `.cpp.inc` 里，长这样（本机 `gen/toy/ToyOpsTypes.cpp.inc` 第 128 行附近）：

```cpp
struct PointerTypeStorage : public ::mlir::TypeStorage {
  using KeyTy = ::mlir::Type;
  PointerTypeStorage(::mlir::Type pointee) : pointee(pointee) {}
  bool operator==(const KeyTy &key) const { return key == pointee; }
  static PointerTypeStorage *construct(StorageAllocator &allocator, const KeyTy &key) {
    return new (allocator.allocate<PointerTypeStorage>()) PointerTypeStorage(key);
  }
  ::mlir::Type pointee;
};
```

`KeyTy` 就是"什么算同一个类型"的定义。这里 `KeyTy = Type`，所以 `!toy.pointer<f32>` 的唯一性只由 pointee 决定。**改 `KeyTy` 就等于改相等语义**，这是本章最需要记住的一句话。

## 核心概念二：storage uniquing 到底保证了什么

官方文档的说法是：attribute 和 type 是 "immortal objects that get uniqued within an MLIRContext"（`docs/DefiningDialects/AttributesAndTypes.md:988`）。

落到本机行为上，我用一个 20 行的程序直接验证：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "D:\Learning-with-Claude\.scratch\writer-c\toy-standalone\bin\toy-unique.exe"
```

实测输出：

```text
pointer_same_params_same_object = true
pointer_diff_params_same_object = false
array_same_params_same_object = true
getChecked_pointer_index_ok = false
getChecked_array_zero_ok = false
getChecked_array_four_ok = true
```

同时在 stderr 上有两条诊断：

```text
error: toy.pointer needs a float pointee, got 'index'
error: toy.array size must be positive, got 0
```

四点结论：

1. **同参数 = 同对象**。`PointerType::get(ctx, f32)` 调两次，返回的 impl 指针相同，`==` 为真。
2. **不同参数 = 不同对象**。`f64` 版本的指针是另一个对象。
3. **uniquing 是 per-context 的**。两个 `MLIRContext` 之间不共享 storage，跨 context 比较类型没有意义，也基本不会发生。
4. **`getChecked` 失败返回空 Type**，诊断由你传进去的 `emitError` 发出，不会崩溃。

uniquing 的收益不止"比较快"。它还让 **`Type` 可以安全地当 map key、可以在不同 pass 之间传递、可以不复制**，因为一旦对象存在就永远存在（不会有人在你持有指针时销毁它）。

## 核心概念三：参数、解析与打印

ODS 给 TypeDef 两条路：

| 方式 | 写法 | 谁写解析/打印 |
| --- | --- | --- |
| 声明式 | `let assemblyFormat = "`<` $pointee `>`";` | tblgen 生成 |
| 命令式 | `let hasCustomAssemblyFormat = 1;` | 你写 `parse` / `print` |

指针类型走的是声明式，所以 `!toy.pointer<f32>` 的解析和打印都是生成的。数组类型故意走命令式，用来对比：

```tablegen
def Toy_ArrayType : Toy_Type<"Array", "array"> {
  let parameters = (ins "int64_t":$size, "::mlir::Type":$elementType);
  let hasCustomAssemblyFormat = 1;
  let genVerifyDecl = 1;
}
```

手写的打印（`lib/ToyTypes.cpp`）：

```cpp
void ArrayType::print(AsmPrinter &printer) const {
  printer << "<" << getSize() << " x " << getElementType() << ">";
}
```

手写的解析：

```cpp
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
```

两个细节是踩过坑才知道的：

- **`getCurrentLocation()` 返回 `SMLoc`，不是 `Location`**。我第一版写成 `Location loc = parser.getCurrentLocation();`，编译直接报 `无法从 llvm::SMLoc 转换为 mlir::Location`。要用 `Location` 就调 `parser.getEncodedSourceLoc(...)`。
- **解析里也要走 `getChecked`**。手写 parser 很容易在"语法对了但语义不对"的地方（`!toy.array<0 x f32>`）放行，`getChecked` 把类型自己的 verifier 接回来了。

`parse` / `print` 的签名里出现的是 `AsmParser` / `AsmPrinter`，不是名字更长的 `DialectAsmParser` / `DialectAsmPrinter`。后者是前者的子类，`Dialect::printType(Type, DialectAsmPrinter &)` 这一层才是老接口；ODS 生成的 `parse`/`print` 用的是基类引用，功能一样，只是能少写几个字符。

## 核心概念四：Attribute 与 Type 同源，但多一个 type 字段

Attribute 的实现机制和 Type 几乎一模一样：`AttrDef` 生成 wrapper、storage、`get` / `getChecked`、`KeyTy`、解析/打印挂钩。区别集中在两处语义上。

第一处是**用途**。Type 描述"值是什么形状"，Attribute 描述"这个 op 上挂着什么常量数据"。`toy.add` 上的 `{tag = #toy.const<7 : f32>}` 是一个 attribute，它不占用 SSA 值，不参与数据流，但会参与 op 的相等比较、会被 CSE 看见、会进 bytecode。

第二处是**多出来的 `type` 参数**。`Dialect::parseAttribute(DialectAsmParser &, Type type)` 和 `printAttribute` 都带一个 `Type`，因为同一个 attribute 语法在 `f32` 和 `tensor<4xf32>` 语境下可能是同一个东西的不同解释。MLIR 内置的 `DenseElementsAttr` 就是这样：`dense<[1.0, 2.0]> : tensor<2xf32>` 的 attribute 数据和它的 element type 是绑在一起解析的。

我们的 `Toy_ConstAttr` 用最朴素的写法：

```tablegen
def Toy_ConstAttr : Toy_Attr<"Const", "const"> {
  let summary = "integer constant carrying its own element type";
  let parameters = (ins "int64_t":$value, "::mlir::Type":$type);
  let assemblyFormat = "`<` $value `:` $type `>`";
}
```

打印出来是 `#toy.const<7 : f32>`——`#` 前缀由框架加，`const` 是 mnemonic，`<...>` 是 `assemblyFormat` 负责的部分，和 type 的分工完全对称。

注册也不能忘：

```cpp
void ToyDialect::registerAttributes() {
  addAttributes<
#define GET_ATTRDEF_LIST
#include "toy/ToyOpsAttrs.cpp.inc"
      >();
}
```

漏掉 `registerAttributes()` 的症状和漏 `registerTypes()` 一样：C++ 侧能 `get`，一解析就 `unknown toy attribute`。


## 底层机制：谁把 `!toy.pointer` 印出来的

这里有个非常容易搞错的层次问题。`printDialectType` 在 `lib/IR/AsmPrinter.cpp:2993`：

```cpp
void AsmPrinter::Impl::printDialectType(Type type) {
  auto &dialect = type.getDialect();
  std::string typeName;
  {
    llvm::raw_string_ostream typeNameStr(typeName);
    Impl subPrinter(typeNameStr, state);
    DialectAsmPrinter printer(subPrinter);
    dialect.printType(type, printer);
  }
  printDialectSymbol(os, "!", dialect.getNamespace(), typeName);
}
```

也就是说：**dialect 的 `printType` 只负责 `<...>` 这一段**，`!toy.` 前缀由框架拼上去。这解释了为什么我们手写的 `ArrayType::print` 里没有 `!toy.array`。

在 MLIR 23 里，`useDefaultTypePrinterParser = 1` 时，`parseType` / `printType` 的函数体 **是 tblgen 生成的**，写在 `ToyOpsTypes.cpp.inc` 里（本机生成物第 188 行起）。所以你在 `ToyDialect.cpp` 里再手写一遍会得到：

```text
error C2084: 函数“mlir::Type toy::ToyDialect::parseType(mlir::DialectAsmParser &) const”已有主体
```

这是我实际撞上的错误。正确做法是：**两个函数都别写**，只保留 `registerTypes()`；如果你非要自己派发，就把 `useDefaultTypePrinterParser` 关掉。

### StorageUniquer 这一层

上面说"同参数同对象"，具体是谁在保证？答案是 `MLIRContext` 里的 `StorageUniquer`，加上每个 wrapper 的唯一 `TypeID`。

流程是这样：`PointerType::get(ctx, f32)` → `TypeUniquer::get<PointerType>(ctx, f32)` → 用 `TypeID::get<PointerType>()` 找到这个类型的 uniquer → 把 `f32` 作为 `KeyTy` 去 hash 表里查 → 命中就返回已有的 `PointerTypeStorage*`，没命中才 `construct` 一个新的。

三个推论：

- **hash 表是按类型族分开的**。`PointerType` 和 `ArrayType` 各有自己的表，即使 key 撞了也不会互相干扰。
- **`TypeID` 的稳定性依赖注册**。dialect 没有 `addTypes<...>()` 时，抽象信息不存在，parser 找不到 mnemonic 对应的构造函数。
- **构造是线程安全的**，但 `get` 的前提是 context 已经允许并发访问（多线程 pass 里默认如此）。你的 `construct` 里不要做有副作用的初始化。

这也解释了为什么 type 的 storage 一定要"不可变"：对象一旦进 hash 表就永远在那里，任何后续修改都会破坏其他持有者的假设。要"改"一个类型，只能造一个新的。
### 从 .td 到 .inc：本章实验的真实流水线

上面所有讨论都建立在一组生成物上。本机实际执行的命令只有八条（`build-cl.bat` 里就是它们）：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library"
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-op-decls            -o gen\toy\ToyOps.h.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-op-defs             -o gen\toy\ToyOps.cpp.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-typedef-decls -typedefs-dialect=toy -o gen\toy\ToyOpsTypes.h.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-typedef-defs  -typedefs-dialect=toy -o gen\toy\ToyOpsTypes.cpp.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-attrdef-decls -attrdefs-dialect=toy -o gen\toy\ToyOpsAttrs.h.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-attrdef-defs  -attrdefs-dialect=toy -o gen\toy\ToyOpsAttrs.cpp.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-dialect-decls -dialect=toy          -o gen\toy\ToyOpsDialect.h.inc
& "$mlir\bin\mlir-tblgen.exe" -I "$mlir\include" include\toy\ToyOps.td --gen-dialect-defs  -dialect=toy          -o gen\toy\ToyOpsDialect.cpp.inc
```

四条与本章直接相关，读法如下：

| 生成物 | 里面有什么 | 什么时候去看 |
| --- | --- | --- |
| `ToyOpsTypes.h.inc` | `PointerType` / `ArrayType` 的类、`get` / `getChecked` / `verify` 声明 | 想知道"我该实现哪些函数" |
| `ToyOpsTypes.cpp.inc` | storage 类、`get` 定义、**`parseType` / `printType` 的函数体** | 报 C2084、或 print 行为不对 |
| `ToyOpsAttrs.h.inc` | `ConstAttr` 的 wrapper | 同上 |
| `ToyOpsAttrs.cpp.inc` | attribute 的 storage 与 parse/print 派发 | attribute 解析报 unknown |

注意 `-typedefs-dialect=toy` 和 `-attrdefs-dialect=toy` 这两个参数：它们决定生成物只包含该方言的 def。少写会得到"包含所有方言 def"的文件，编译时报一堆不相关符号。

另外要注意 `.td` 的 include 关系。我们的 `ToyOps.td` 里 `include` 了所有内容（dialect、types、attrs、ops），所以八条命令都只吃一个输入文件。真实项目通常拆成 `ToyDialect.td` / `ToyTypes.td` / `ToyAttrs.td` / `ToyOps.td`，此时每条命令要指向正确的输入，否则会生成空文件而不报错——这是"生成了但什么都没生成"的经典困惑。

## 示例一：同一段 IR 的两种打印

输入（`test/asm.mlir` 片段）：

```mlir
func.func @memory() {
  %p = toy.alloc : !toy.pointer<f32>
  %v = toy.load %p : !toy.pointer<f32> -> f32
  toy.store %v, %p : f32, !toy.pointer<f32>
  return
}
```

自定义打印（默认）：

```powershell
& "D:\Learning-with-Claude\.scratch\writer-c\toy-standalone\bin\toy-opt.exe" .\test\asm.mlir
```

实测输出：

```mlir
module {
  func.func @memory() {
    %0 = toy.alloc : !toy.pointer<f32>
    %1 = toy.load %0 : !toy.pointer<f32> -> f32
    toy.store %1, %0 : f32, !toy.pointer<f32>
    return
  }
  func.func @arrays(%arg0: !toy.array<4 x f32>) -> !toy.array<4 x f32> {
    return %arg0 : !toy.array<4 x f32>
  }
  func.func @attrs(%arg0: f32, %arg1: f32) -> f32 {
    %0 = toy.add %arg0, %arg1 {tag = #toy.const<7 : f32>} : f32
    return %0 : f32
  }
}
```

generic 打印：

```powershell
& "...\toy-opt.exe" .\test\asm.mlir --mlir-print-op-generic
```

实测输出（节选）：

```mlir
"builtin.module"() ({
  "func.func"() <{function_type = () -> (), sym_name = "memory"}> ({
    %1 = "toy.alloc"() : () -> !toy.pointer<f32>
    %2 = "toy.load"(%1) : (!toy.pointer<f32>) -> f32
    "toy.store"(%2, %1) : (f32, !toy.pointer<f32>) -> ()
    "func.return"() : () -> ()
  }) : () -> ()
```

**重点：`--mlir-print-op-generic` 只换 op 的语法，不换 type 的语法。** 类型始终由 dialect 的 `printType` 决定，MLIR 23 没有"类型 generic 打印"这个开关。所谓"generic 形式"指的是 `!dialect.mnemonic<...>` 这套由 `DialectAsmParser` 语法约束的写法，它和 op 的 generic 形式是两回事。

还有一处更细的坑。如果你在 op 的 `assemblyFormat` 里写 `type($result)`，而 `$result` 的约束是具体的 `Toy_PointerType`，ODS 会生成 `printStrippedAttrOrType`（本机 `ToyOps.cpp.inc:371`），把 `!toy.pointer` 前缀省掉，打印成 `toy.alloc : <f32>`。我第一版就是这样，看起来像 bug。修法是把 operand/result 的约束放宽成 `AnyType`，把类型检查交给 op 的 verifier——本章后面的例子都是这么做的。

## 示例二：解析失败的四种姿势

```powershell
& "...\toy-opt.exe" .\test\bad_array.mlir
```

```text
error: expected 'x'
func.func @bad_array(%a: !toy.array<4, f32>) {
                                     ^
```

逗号不是我们 parser 接受的语法。这条错误证明 **解析完全由你的 `parse` 决定**，没有"通用兜底语法"。

```text
error: toy.pointer needs a float pointee, got 'index'
  %p = toy.alloc : !toy.pointer<index>
```

这是 `PointerType::verify` 在 `getChecked` 路径上发出的，属于**类型级**验证。

```text
error: unknown  type `pointr` in dialect `toy`
func.func @bad_name(%a: !toy.pointr<f32>) {
```

注意 `unknown` 后面有两个空格——这是生成代码里的字面量（`"unknown  type \`"`），不是排版问题。

```text
error: 'toy.store' op stored value type 'i32' does not match pointee type 'f32'
  toy.store %c, %p : i32, !toy.pointer<f32>
```

这是 **op 级**验证，是我们自己在 `StoreOp::verify()` 里写的。类型级和 op 级的分工要在设计时就定下来：类型级只检查"这个类型本身是否成立"，op 级检查"这个 op 用这个类型是否成立"。

## 示例三：当前置约束被放松之后

把 `toy.alloc` 的结果约束从 `Toy_PointerType` 改成 `AnyType`，解析和打印都变干净了，但验证责任转移了：

```cpp
LogicalResult AllocOp::verify() {
  auto pointer = dyn_cast<PointerType>(getResult().getType());
  if (!pointer)
    return emitOpError("result must be a !toy.pointer, got ")
           << getResult().getType();
  Type pointee = pointer.getPointee();
  if (!isa<FloatType>(pointee))
    return emitOpError("must allocate a pointer to a float, got ") << pointee;
  return success();
}
```

实测效果就是前面那段 `!toy.pointer<f32>` 的正常打印，加上需要时的清晰报错。

## 示例四：自定义类型进 bytecode

类型能不能被 `--emit-bytecode` 写出去、再读回来，是"这个类型定义完整了没有"的试金石。实测：

```powershell
& "...\toy-opt.exe" .\test\asm.mlir --emit-bytecode -o .\test\asm.mlirbc
Get-Item .\test\asm.mlirbc | Select-Object Length
```

```text
Length
------
   520
```

读回来打印：

```powershell
& "...\toy-opt.exe" .\test\asm.mlirbc
```

```mlir
module {
  func.func @memory() {
    %0 = toy.alloc : !toy.pointer<f32>
    %1 = toy.load %0 : !toy.pointer<f32> -> f32
    toy.store %1, %0 : f32, !toy.pointer<f32>
    return
  }
  ...
}
```

类型和属性都完整往返了。原因是默认的 bytecode 序列化会把 dialect type 按 textual 形式写进常量区，读的时候再走同一个 `parseType`。也就是说：**你写的 parser 同时也是 bytecode 的 reader**，解析器有 bug，bytecode 往返就会暴露。Ch 16 会把这条链讲完，包括版本区间和 `BytecodeDialectInterface`。

## 边界：什么时候不该定义新类型

自定义类型的成本不在 `.td`，而在生态：每个下游 pass、每个 conversion pattern、每个 bytecode 版本策略都要理解它。所以先问三个问题：

- **它能被现有类型无损表达吗？** 例如"带 offset 的 memref"用 `memref<4xf32, strided<[1], offset: 2>>` 就够了，不需要 `!toy.buffer`。
- **它只在 op 的 attribute 里出现吗？** 如果它从不进入 SSA 值类型的位置，用 `AttrDef` 更轻，不会污染类型系统。
- **它需要参与 CSE 吗？** 需要的话必须是 uniqued 的（type 天然满足），不要在 op 里塞两个互相约束的 string attribute。

反过来，**该定义的时候别犹豫**：只要这个类型会出现在函数签名、会被 conversion 当成不透明值搬运、或者需要在 IR dump 里被一眼认出来，定义类型比"用 `i32` 加注释"便宜得多。Toy 的 `!toy.pointer<f32>` 就是典型：它在 `toy.load` / `toy.store` / `toy.alloc` 之间流动，用 `i32` 顶替会让每个 verifier 都要重复一遍"这其实是指针"。

## 对比与取舍

| 选择 | 代价 | 什么时候选 |
| --- | --- | --- |
| `assemblyFormat`（声明式） | 语法受 ODS 表达能力限制 | 90% 的类型，尤其参数是 int/Type/Attr 的组合 |
| `hasCustomAssemblyFormat` | 要写两个函数，还要自己处理错误恢复 | 语法有歧义、需要 `x` 这种分隔符、要从旧语法兼容读入 |
| `get()` | 参数非法时 assert（Release 下静默通过） | 只有你能证明参数一定合法时（例如来自已验证的 IR） |
| `getChecked()` | 多一个 `emitError` 参数 | 解析、builder、任何"用户输入"路径 |
| 参数用 `Type` | 直观，能表达嵌套 | 需要指向另一个类型时 |
| 参数用 `StringRef` | 需要自己管生命周期 | 名字类参数，且已经 `copyInto` |

`get()` 的行为需要说准。`StorageUserBase::get` 在 `include/mlir/IR/StorageUniquerSupport.h:177` 长这样：

```cpp
template <typename... Args>
static ConcreteT get(MLIRContext *ctx, Args &&...args) {
  assert(succeeded(
      ConcreteT::verifyInvariants(getDefaultDiagnosticEmitFn(ctx), args...)));
  return UniquerT::template get<ConcreteT>(ctx, std::forward<Args>(args)...);
}
```

`assert` 在 `NDEBUG` 下消失。conda 的 MLIR 库是 Optimized build，但**你的 TU 是否定义 `NDEBUG` 是你自己的事**。我第一次编译 toy-unique 时没加 `/DNDEBUG`，`get(pointer, index)` 直接断言崩了；加上之后同一行就静默创建了一个"非法"类型。这就是为什么解析路径必须用 `getChecked`。

两条经验补充：

- **验证放在类型里还是 op 里，取决于"错误属于谁"**。`toy.pointer` 要求 pointee 是浮点，这是类型自身不成立，放在 `PointerType::verify`；`toy.store` 要求 value 与 pointee 一致，这是两个东西的关系，只能放 op verifier。
- **不要为了省一次 `dyn_cast` 而收紧约束**。`AnyType` + verifier 打印出完整类型名，`Toy_PointerType` 打印出被 strip 的 `<f32>`；前者多几行 verifier，后者多一个长期困惑。

## 踩雷集锦

1. **在 `ToyDialect.cpp` 里手写 `parseType` / `printType`**。`useDefaultTypePrinterParser` 已经生成了函数体，你会得到 C2084。生成物在 `ToyOpsTypes.cpp.inc`，不在 `.h.inc`。
2. **以为 `getCurrentLocation()` 返回 `Location`**。它返回 `SMLoc`。要 `Location` 用 `getEncodedSourceLoc`。
3. **TypeDef 不写 `assemblyFormat` 也不写 `hasCustomAssemblyFormat`**。tblgen 直接报 `must specify either ... when 'mnemonic' is set`，并且在 `-gen-typedef-decls` 路径上还会顺带打出一份 crash 报告（本机 23.1.4 实测）。别指望它给你"默认格式"。
4. **`.td` 里 class 参数与字段同名**。`class Foo_Type<string name, string mnemonic> { let mnemonic = mnemonic; }` 会得到 `Recursion / self-assignment forbidden`。参数改名成 `typeMnemonic`。
5. **以为 ODS 一定会打印完整类型名**。具体约束 + `type($x)` 会被 strip 成 `<f32>`，用 `AnyType` 或显式 `printType` 才能印全。
6. **在 `get()` 里传用户输入**。Release 下不报错，坏类型会一路流到下游，最后在一个毫不相关的地方崩。
7. **忘了 `registerTypes()`**。类型能生成、能编译、能 `get`，但一解析 `!toy.pointer` 就报 unknown type，因为 dialect 没有把 TypeID 注册进去。

## 进阶：再往深一层

- **`KeyTy` 用 tuple 时怎么办**。两个以上参数，`KeyTy` 就是 `std::tuple<...>`，`operator==` 和 `construct` 都要跟着写；ODS 会自动生成，但你要能读出来。
- **type alias**。`AsmPrinter` 会给长类型自动生成别名（`!toy.array_0`），条件是类型长度超过阈值且 context 允许 alias。这也是 `printAlias` 在 `printType` 之前被调用的原因（`AsmPrinter.cpp:2784`）。
- **External model**。类型不能改代码时（比如给 `memref` 挂接口），用 `attachInterface` + external model，Ch 8 讲过 op 侧，type 侧同理。
- **bytecode 里的类型**。类型默认按 textual 形式进 bytecode；要自定义编码就实现 `BytecodeDialectInterface`。Ch 16 会看到实测的往返。

- **`KeyTy` 里放 `StringRef` 的陷阱**。`StringRefParameter` 生成的 storage 会用 `copyInto` 把字符串复制进 context 的 allocator；如果你手写 storage 直接用 `StringRef` 当 key，字符串生命周期归调用方，跨 pass 传递时会读到野内存。本章的 `ConstAttr` 用 `int64_t` + `Type` 就是刻意避开这个问题。
- **type 的打印是热路径**。`printDialectType` 每次都会新建一个 `raw_string_ostream` 和子 printer（`AsmPrinter.cpp:2997-3009`），所以 `print` 里不要做 O(n) 的字符串拼接；要拼就 `printer << a << b`。

## 动手练习

1. **加一个 `!toy.slice<16 x f32>`**，用 `assemblyFormat` 写，要求参数是 size + elementType，并让它和 `toy.array` 打印出不同语法。
2. **把 `PointerType::verify` 改成"允许任意 pointee"**，然后解释为什么 `toy.load` 的 verifier 不需要改。再改回来。
3. **写一个 `getChecked` 的失败用例**，让它在 `toy-opt` 解析阶段触发，并贴出诊断里的 source location。
4. **观察 uniquing 的边界**：把 `KeyTy` 临时改成 `std::tuple<Type, int>`（加一个永远为 0 的 tag），重新编译，说明为什么 `pointer_same_params_same_object` 仍然是 `true`。
5. **对比 alias**：把 `!toy.array<4 x f32>` 在同一个 module 里出现 10 次，加 `--mlir-print-op-generic` 和不加，看打印长度差异。

## 本章重点整理

- 一个 dialect type = `Type` 句柄 + C++ wrapper + storage + uniquing key，四层各司其职。
- `KeyTy` 决定相等语义；同参数同对象，比较退化为指针比较。
- `get()` 走 assert，`getChecked()` 走诊断并返回空对象；解析路径只能用后者。
- `useDefaultTypePrinterParser` 会**生成** `parseType` / `printType` 的函数体，不要再手写。
- `!toy.` 前缀由 `printDialectType` 拼，dialect 的 `printType` 只输出 `<...>`。
- `--mlir-print-op-generic` 只管 op，不管 type。
- 具体类型约束 + `type($x)` 会被 strip 前缀，需要完整打印时放宽约束。

## 自我检核

- [ ] 能不看文档写出一个带 `Type` 参数的 TypeDef，并说出它会生成哪几个 `get` 重载
- [ ] 能解释为什么两个 `MLIRContext` 之间的类型比较没有意义
- [ ] 知道 `getChecked` 的第一个参数类型，以及 `SMLoc` 与 `Location` 的区别
- [ ] 能指出 `!toy.pointer` 前缀是在哪一层打印的（文件 + 函数）
- [ ] 遇到 `unknown  type` 时，第一反应是查 `registerTypes()` 而不是 parser
- [ ] 能说出 `--mlir-print-op-generic` 对类型打印没有任何影响
- [ ] 能解释 `toy.alloc : <f32>` 这种输出的成因和修法

## 延伸阅读

- **`mlir/docs/DefiningDialects/AttributesAndTypes.md`**：读 "Defining a Type" 到 "Storage" 几节；学 storage 与 uniquing 的官方模型；和本章的 `KeyTy` 小节对照看，能把生成物读成文档的实例。
- **`mlir/include/mlir/IR/StorageUniquerSupport.h`**：读 150–200 行；学 `get` / `getChecked` 的两种语义；这是踩雷 1、6 的根因所在。
- **`mlir/lib/IR/AsmPrinter.cpp`**：读 2778–2800 与 2993–3012；学 type 打印的两级分派；能解释本章 `!toy.` 前缀的问题。
- **`mlir/examples/toy/Ch7`**：读 `mlir/Dialect.cpp` 的 type 打印部分；学一个真实教学方言怎么组织；和练习 C 的实现对照。
- **`mlir/docs/BytecodeFormat.md`**：读 140–200 行；学类型在 bytecode 里的落点；为 Ch 16 的往返实验做铺垫。

→ [Ch 15 Trait 与 Interface 实战](./15-traits-and-interfaces.md)
