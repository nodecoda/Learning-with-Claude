# Ch 13 解剖 TableGen 生成物

> **目标**：把 `.h.inc` 与 `.cpp.inc` 从"生成的黑盒"变成"能逐段读懂的代码"。学完本章，你看到任何一个 ODS op，都能指出 `getODSOperands` / `getODSResults` / adaptor / properties / builder / parser 各自的生成位置与调用者，并说清 variadic op 的 `operandSegmentSizes` 为什么必须存在。

## 为什么需要这个？

Ch 11 和 Ch 12 里，`.inc` 只是"跑出来的东西"：能编译、能报错，但里面每一段为什么长那样，我们没深究。这会带来三个具体问题：

- **报错定位慢**。编译错误指向 `toy.cpp.inc(101)`，你得知道 101 行属于哪一段，才能判断是自己的 `.td` 写错了还是漏了头文件。
- **调试时看不懂调用栈**。栈里出现 `getODSOperands(0)`、`Adaptor::verify`、`getProperties()`，不知道它们从哪来、能不能改。
- **性能判断失据**。以为 `getLhs()` 是一次字典查找，于是"优化"成缓存指针；实际上它是一次 `getODSOperands(0).begin()` 解引用。

更实际的原因是：**TableGen 生成物是 MLIR 方言的 ABI**。你写的 pass、pattern、conversion 全都编译在它上面。升级 MLIR 时生成物形状会变（例如 properties 的引入），读懂它才知道升级要改哪里。

本章用两个文件做解剖标本：`toy.h.inc`（202 行）与 `toy.cpp.inc`（313 行），都来自 Ch 11 的 `toy.td`；variadic 部分用 `variadic.td` 生成的 `variadic.h.inc`（226 行）与 `variadic.cpp.inc`（361 行）。

## 先建立直觉：一个 `.td`，两种视角

同一份声明跑两次，产出的不是"头文件 + 实现"，而是两种使用视角：

```text
toy.td ─┬─ -gen-op-decls ─▶ toy.h.inc    "我是一个强类型 C++ 类"
        └─ -gen-op-defs  ─▶ toy.cpp.inc  "我怎么把值搬进 Operation"
```

`.h.inc` 回答"怎么用"：类名、访问器、builder 签名、trait 列表。`.cpp.inc` 回答"怎么实现"：下标映射、类型检查、parse/print、属性序列化。

两份文件都用 **include guard 变量**（`GET_OP_CLASSES`）控制内容，而不是传统 `#ifndef`；`#undef` 写在文件内部，所以同一个 `.inc` 可被 include 多次、每次取不同片段。`.cpp.inc` 顶部的 `GET_OP_LIST` 是另一段，只输出类名列表：

```cpp
#ifdef GET_OP_LIST
#undef GET_OP_LIST

::toy::AddOp
#endif // GET_OP_LIST
```

dialect 注册用的 `addOperations<::toy::AddOp>` 就来自这里——op 名不需要手写。

标准姿势是头文件与 `.cpp` 各写一次 `#define GET_OP_CLASSES` 再 include 对应文件。两次互不干扰，因为 `#undef` 在文件内部完成。忘了 `#define` 就什么都拿不到，且**不会报错**——这是"空实现"困惑的根源。

## `.h.inc` 逐段解剖

### 第 1 段：序幕与前向声明（1-16 行）

```cpp
// ... 文件头注释（8 行）略 ...
namespace toy {

/// element-wise addition of two tensors
/// Adds two tensors of the same shape element-wise.
class AddOp;

} // namespace toy
```

前向声明放在 guard 外面，所以**哪怕不定义 `GET_OP_CLASSES`，也能拿到 `class AddOp;`**。这让"只需要指针/引用"的头文件不必拖入完整定义。

### 第 2 段：`detail::AddOpGenericAdaptorBase`（27-56 行）

所有 adaptor 的公共基类，保存"一组操作数 + 属性 + region"的最小状态（构造参数节选）：

```cpp
class AddOpGenericAdaptorBase {
public:
  using Properties = ::mlir::EmptyProperties;
protected:
  ::mlir::DictionaryAttr odsAttrs;
  ::std::optional<::mlir::OperationName> odsOpName;
  Properties properties;
  ::mlir::RegionRange odsRegions;
public:
  AddOpGenericAdaptorBase(::mlir::DictionaryAttr attrs, const Properties &properties,
                          ::mlir::RegionRange regions = {}) : /* ... */ {
    if (odsAttrs)
      odsOpName.emplace("toy.add", odsAttrs.getContext());
  }
  std::pair<unsigned, unsigned> getODSOperandIndexAndLength(unsigned index, unsigned odsOperandsSize) {
    return {index, 1};
  }
  const Properties &getProperties() { return properties; }
  ::mlir::DictionaryAttr getAttributes() { return odsAttrs; }
};
```

三个关键点：

1. **`odsAttrs` 可以是 null**，所以构造函数先 `if (odsAttrs)` 才 `emplace` op 名——adaptor 允许轻量构造。
2. **`getODSOperandIndexAndLength` 返回 `{index, 1}`**：固定 arity 时第 i 个操作数就是第 i 个，长度恒为 1。
3. **`Properties` 是 `EmptyProperties`**：没有属性，但类型仍然存在，不是被优化掉。

### 第 3 段：`AddOpGenericAdaptor<RangeT>`（59-101 行）

模板化的 adaptor，`RangeT` 可以是 `ValueRange`、`OperandRange`，甚至 `ArrayRef<Attribute>`（fold 用）：

```cpp
template <typename RangeT>
class AddOpGenericAdaptor : public detail::AddOpGenericAdaptorBase {
  using ValueT = ::llvm::detail::ValueOfRange<RangeT>;
  using Base = detail::AddOpGenericAdaptorBase;
  static constexpr int odsIndex_lhs = 0;
  static constexpr int odsIndex_rhs = 1;
public:
  RangeT getODSOperands(unsigned index) {
    auto valueRange = getODSOperandIndexAndLength(index);
    return {std::next(odsOperands.begin(), valueRange.first),
             std::next(odsOperands.begin(), valueRange.first + valueRange.second)};
  }

  ValueT getLhs() {
    return (*getODSOperands(0).begin());
  }
```

`ValueOfRange<RangeT>` 是"这个 range 里元素的类型"：`ValueRange` → `Value`，`ArrayRef<Attribute>` → `Attribute`。这让同一个 adaptor 既能包 SSA 值，也能包 fold 时的常量属性——**fold 的 `FoldAdaptor` 就是 `GenericAdaptor<ArrayRef<Attribute>>`**（`.h.inc:116`）。

`getLhs()` 的实现只有一句：取 `getODSOperands(0)` 的第一个元素再解引用。没有字典查找、没有字符串比较。这是"生成物性能好"的根源。

### 第 4 段：`AddOpAdaptor`（102-108 行）

`AddOpAdaptor` = `GenericAdaptor<ValueRange>` + 一个 `verify(::mlir::Location)`。它给 conversion pattern 用：那里只有 `(operands, attrs)`，没有 `Operation`，但仍要检查合法性（Ch 12 展示过实现）。

### 第 5 段：`AddOp` 本体（109-195 行）

类头就是 trait 列表，全部由 `.td` 推导：

```cpp
class AddOp : public ::mlir::Op<AddOp, ::mlir::OpTrait::ZeroRegions, ::mlir::OpTrait::OneResult, ::mlir::OpTrait::OneTypedResult<::mlir::TensorType>::Impl, ::mlir::OpTrait::ZeroSuccessors, ::mlir::OpTrait::NOperands<2>::Impl, ::mlir::OpTrait::OpInvariants, ::mlir::ConditionallySpeculatable::Trait, ::mlir::OpTrait::AlwaysSpeculatableImplTrait, ::mlir::MemoryEffectOpInterface::Trait> {
public:
  using Op::Op;
  using Op::print;
  using Adaptor = AddOpAdaptor;
  template <typename RangeT>
  using GenericAdaptor = AddOpGenericAdaptor<RangeT>;
  using FoldAdaptor = GenericAdaptor<::llvm::ArrayRef<::mlir::Attribute>>;
  static constexpr int odsIndex_lhs = 0;
  static constexpr int odsIndex_rhs = 1;
  using Properties = FoldAdaptor::Properties;
```

`using Op::print;` 这行容易忽略：它把基类的 `print(Operation*, OpAsmPrinter&, StringRef)` 和成员 `print(OpAsmPrinter&)` 放进同一重载集，缺了它调用会歧义。

接着是名字、下标映射与访问器：

```cpp
  static constexpr ::llvm::StringLiteral getOperationName() {
    return ::llvm::StringLiteral("toy.add");
  }

  std::pair<unsigned, unsigned> getODSOperandIndexAndLength(unsigned index) {
    return {index, 1};
  }

  ::mlir::Operation::operand_range getODSOperands(unsigned index) {
    auto valueRange = getODSOperandIndexAndLength(index);
    return {std::next(getOperation()->operand_begin(), valueRange.first),
             std::next(getOperation()->operand_begin(), valueRange.first + valueRange.second)};
  }

  ::mlir::TypedValue<::mlir::TensorType> getLhs() {
    return ::llvm::cast<::mlir::TypedValue<::mlir::TensorType>>(*getODSOperands(0).begin());
  }
```

结果侧完全对称，只是把 `operand_begin` 换成 `result_begin`：`getODSResultIndexAndLength` → `getODSResults` → `getResult`。

注意 op 版的 `getLhs()` 多了一次 `cast`，adaptor 版没有：op 的操作数运行时可能是任意 `Value`，必须 `cast` 到声明类型；adaptor 的 `ValueT` 已是静态类型。

`getODSResults` 与 `getODSOperands` 是**所有** ODS 访问器的两个根。`getLhs`、`getResult`、`getInputs` 都只是薄包装。记住这条，生成物里 90% 的访问器都能一眼看懂。之后是 properties 钩子（170-176）、12 个 builder（177-188）、verify（189-190）、parse/print（191-192）、`getEffects`（193）。

## `.cpp.inc` 逐段解剖

### 第 1 段：`GET_OP_LIST`（10-14 行）与本地约束（25-33 行）

`GET_OP_LIST` 前面说过，是给 dialect 注册的类名列表。紧接着是**文件级静态函数**：

```cpp
static ::llvm::LogicalResult __mlir_ods_local_type_constraint_toy1(
    ::mlir::Operation *op, ::mlir::Type type, ::llvm::StringRef valueKind,
    unsigned valueIndex) {
  if (!(((::llvm::isa<::mlir::TensorType>(type))) && ([](::mlir::Type elementType) { return (elementType.isF64()); }(::llvm::cast<::mlir::ShapedType>(type).getElementType())))) {
    return op->emitOpError(valueKind) << " #" << valueIndex
        << " must be tensor of 64-bit float values, but got " << type;
  }
  return ::mlir::success();
}
```

命名规则是 `__mlir_ods_local_type_constraint_<td 文件名><序号>`。`toy1` 里的 `toy` 来自 `toy.td`，`1` 是文件内序号——所以同一个文件里改一个约束，编号可能整体位移。**不要在任何地方引用这个函数名**，它不是 API。

它是 `static` 的：每个 `.cpp.inc` 一份，不会跨翻译单元冲突。

### 第 2 段：adaptor 实现与 verify（43-51 行）

```cpp
AddOpGenericAdaptorBase::AddOpGenericAdaptorBase(AddOp op) : odsAttrs(op->getRawDictionaryAttrs()), odsOpName(op->getName()), properties(op.getProperties()), odsRegions(op->getRegions()) {}

AddOpAdaptor::AddOpAdaptor(AddOp op) : AddOpGenericAdaptor(op->getOperands(), op) {}
```

`op->getRawDictionaryAttrs()` 而不是 `getAttrs()`：raw 版本返回**未解析的原始字典**，避开属性名排序与延迟解析——adaptor 构造是热路径。

`AddOpAdaptor::verify` 直接 `success()`，因为这个 op 没有任何约束；有属性时（Ch 12 的 `ScaleOpAdaptor`）这里会变成一长串检查。

### 第 3 段：builder（90-166 行）

最简单的那个已经在 Ch 11 看过。这里补充一条容易忽略的：结果数量断言。

```cpp
  assert(resultTypes.size() == 1u && "mismatched number of results");
```

`assert` 只在 Debug 构建生效；本机 conda 包是 Optimized build，不会触发。**传错结果数量在 Release 下静默通过**，直到 verifier 报"结果数量不对"。这是"构造便宜、验证独立"的另一面。

### 第 4 段：`verifyInvariantsImpl`（168-194 行）

```cpp
::llvm::LogicalResult AddOp::verifyInvariantsImpl() {
  {
    unsigned index = 0; (void)index;
    auto valueGroup0 = getODSOperands(0);
    for (auto v : valueGroup0) {
      if (::mlir::failed(__mlir_ods_local_type_constraint_toy1(*this, v.getType(), "operand", index++)))
        return ::mlir::failure();
    }
    // ... rhs 相同 ...
  }
  {
    unsigned index = 0; (void)index;
    auto valueGroup0 = getODSResults(0);
    for (auto v : valueGroup0) {
      if (::mlir::failed(__mlir_ods_local_type_constraint_toy1(*this, v.getType(), "result", index++)))
        return ::mlir::failure();
    }
  }
  return ::mlir::success();
}
```

两个操作数用的是同一个约束，生成器复用了 `toy1`；但循环仍分开写，因为 `index` 要连续编号（operand #0、#1）。每个值都走一次 `getODSOperands(i)`，即使固定 arity 也**不做特化**。

### 第 5 段：parse 与 print（200-302 行）

parser 可浓缩成四步（节选，保留结构与失败路径）：

```cpp
::mlir::ParseResult AddOp::parse(::mlir::OpAsmParser &parser, ::mlir::OperationState &result) {
  ::mlir::OpAsmParser::UnresolvedOperand lhsRawOperand{};
  ::llvm::ArrayRef<::mlir::OpAsmParser::UnresolvedOperand> lhsOperands(&lhsRawOperand, 1);
  ::mlir::Type lhsRawType{};
  ::llvm::ArrayRef<::mlir::Type> lhsTypes(&lhsRawType, 1);
  // ... rhs、result 同理 ...

  if (parser.parseOperand(lhsRawOperand)) return ::mlir::failure();
  if (parser.parseComma()) return ::mlir::failure();
  // ... 读 rhs、attr-dict、类型 ...
  result.addTypes(resultTypes);
  if (parser.resolveOperands(lhsOperands, lhsTypes, lhsOperandsLoc, result.operands))
    return ::mlir::failure();
  return ::mlir::success();
}
```

四种状态对应四个变量：`UnresolvedOperand`（文本引用）、`Type`（解析出的类型）、`OperationState`（正在构造的 op）、`result.operands`（已解析的 SSA 值）。

`print` 是**逐 token 翻译**，连空格都显式打：

```cpp
void AddOp::print(::mlir::OpAsmPrinter &_odsPrinter) {
  _odsPrinter << ' ' << getLhs() << ",";
  // ... rhs ...
  ::llvm::SmallVector<::llvm::StringRef, 2> elidedAttrs;
  _odsPrinter.printOptionalAttrDict((*this)->getAttrs(), elidedAttrs);
}
```

`elidedAttrs` 是"已在语法里打印过、不要重复出现在 attr-dict 里"的属性名。对 `AddOp` 它是空的；有属性时生成器会把 `$factor` 这类名字塞进去。**忘了这个机制，打印结果里属性会出现两次**。

### 第 6 段：结尾（304-312 行）

```cpp
void AddOp::getEffects(::llvm::SmallVectorImpl<::llvm::SideEffects::EffectInstance<::mlir::MemoryEffects::Effect>> &effects) {
}
...
} // namespace toy
MLIR_DEFINE_EXPLICIT_TYPE_ID(::toy::AddOp)
```

空 `getEffects` 来自 `Pure`；`MLIR_DEFINE_EXPLICIT_TYPE_ID` 与头文件里的 `MLIR_DECLARE_EXPLICIT_TYPE_ID` 配对，给 `TypeID` 提供唯一定义。`.h.inc:198` 是 declare，`.cpp.inc:309` 是 define——**两个必须同时存在**，只 include 头文件不 include 实现会链接失败。

## `properties`：`EmptyProperties` 之外

`toy.add` 没有属性，所以 `using Properties = ::mlir::EmptyProperties;`。一旦有属性，这里会变成结构体：每个属性一个字段，另有 `getX()` / `setX()` 与 `operator==`（Ch 12 的 `scale.h.inc:200-220`）。

两个后果：访问从 `DictionaryAttr` 查找变成字段读取（`getProperties().factor`）；带 properties 的 op 自动获得 `BytecodeOpInterface`，需要 `readProperties` / `writeProperties`（`scale.h.inc:367-368`）。

`OpDefinition.h:2016` 的 `getProperties()` 是底座：从 `Operation` 的 properties 存储 `as<Properties*>()` 取指针，`EmptyProperties` 时返回静态空对象。

## `operandSegmentSizes` 与 variadic

variadic 操作数没有固定数量，"第 i 个操作数是谁"需要额外信息。`variadic.td` 定义两个 variadic 组：

```tablegen
def Toy_ConcatOp : Op<Toy_Dialect2, "concat", [Pure]> {
  let summary = "concatenate two variadic operand groups";
  let arguments = (ins Variadic<TensorOf<[F64]>>:$inputs,
                       Variadic<TensorOf<[F64]>>:$others);
  let results = (outs TensorOf<[F64]>:$result);
}
```

直接跑 tblgen（实测）：

```text
variadic.td:9:5: error: op has multiple variadic operands but no specification over their sizes
```

多个 variadic 组时生成器无法确定边界。补 trait 后通过：

```tablegen
def Toy_ConcatOp : Op<Toy_Dialect2, "concat", [Pure, AttrSizedOperandSegments]>
```

生成物 `variadic.h.inc` 226 行、`variadic.cpp.inc` 361 行。核心变化是 `Properties` 多了一个定长数组（实测，`variadic.h.inc:30-33`）：

```cpp
  struct Properties {
    using operandSegmentSizesTy = std::array<int32_t, 2>;
    operandSegmentSizesTy operandSegmentSizes;
```

下标映射也不再是 `{index, 1}`，而是查表累加（实测，`variadic.cpp.inc:72-79`）：

```cpp
std::pair<unsigned, unsigned> ConcatOp::getODSOperandIndexAndLength(unsigned index) {
  ::llvm::ArrayRef<int32_t> sizeAttr = getProperties().operandSegmentSizes;
  unsigned start = 0;
  for (unsigned i = 0; i < index; ++i)
    start += sizeAttr[i];
  return {start, sizeAttr[index]};
}
```

builder 负责写入尺寸（实测，`variadic.cpp.inc:214`）：

```cpp
::llvm::copy(::llvm::ArrayRef<int32_t>({static_cast<int32_t>(inputs.size()), static_cast<int32_t>(others.size())}), odsState.getOrAddProperties<Properties>().operandSegmentSizes.begin());
```

**尺寸与操作数数量是否一致**由 trait 的 verifier 保证（源码依据：`OpDefinition.h:1381-1384` 的 `verifyTrait` → `verifyOperandSizeAttr`）。手改 `operandSegmentSizes` 后必须重跑验证，否则 `getInputs()` 会读越界。

`MutableOperandRange` 还带 `OperandSegment(0u, {...})`（`variadic.cpp.inc:81-91`），保证 `getInputsMutable().assign(...)` 时同步更新尺寸——这是"改一段、另一段下标自动平移"的机制。

## 底层机制：parse/print 怎么挂到 op 上

生成的 `parse` / `print` 不是虚函数，而是静态/成员函数，靠模板注入 `OperationName`：

- `Op::getPrintAssemblyFn()` 检测 `ConcreteType::print` 是否存在，存在就返回调用 `cast<ConcreteType>(op).print(p)` 的 lambda（`OpDefinition.h:2001-2009`）；
- `RegisteredOperationName::Model<ConcreteOp>::getParseAssemblyFn()` 直接返回 `ConcreteOp::parse`（`OperationSupport.h:571-573`），打印走 `printAssembly`（`:578-581`）；
- 注册入口是 `RegisteredOperationName::insert<T>(Dialect&)`（`:705-707`），`T` 就是 `GET_OP_LIST` 里的类名。

所以"注册"= 在 dialect 的 `initialize()` 里 `addOperations<::toy::AddOp>()`。没注册时 parser 只认 generic form，这就是 Ch 11 那条 "unregistered dialect" 错误的来源。

## 示例一：对同一 `.td` 生成两遍

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$w = ".scratch\tblgen-work"

& "$mlir\mlir-tblgen.exe" -gen-op-decls "$w\toy.td" `
  -I "$mlir\..\include" -o "$w\toy.h.inc"
& "$mlir\mlir-tblgen.exe" -gen-op-defs "$w\toy.td" `
  -I "$mlir\..\include" -o "$w\toy.cpp.inc"
```

实测：`toy.h.inc` 202 行 / 9815 字节，`toy.cpp.inc` 313 行 / 13233 字节，都来自同一份 29 行（含末尾空行）的 `.td`。`-I` 指向安装目录的 `Library\include` 或源码树，输出哈希相同（Ch 11 已测）。

## 示例二：三份生成物都能编译

| 编译单元 | 来源 `.td` | 结果 |
|---|---|---|
| `ops.cpp` | `toy.td` | `EXIT=0` |
| `scale_impl.cpp` | `scale.td`（含手写 verify/parse/print） | `EXIT=0` |
| `variadic_impl.cpp` | `variadic.td`（含 `operandSegmentSizes`） | `EXIT=0` |

三条都是本机 `cl /c` 实测。`variadic_impl.cpp` 能过，说明 `std::array<int32_t,2>`、`getOrAddProperties`、`MutableOperandRange::OperandSegment` 这些被生成物引用的 API 在 23.1.4 里齐全——升级 MLIR 时先编译一次生成物，就能发现这类断裂。

## 示例三：从一行报错定位到生成段

Ch 11 第一次编译 `ops.cpp` 时报：

```text
toy.cpp.inc(101): error C2440: “return”: 无法从“int”转换为“toy::AddOp”
```

101 行属于 `create`（`.cpp.inc:96-102`）。它不是"返回值写错了"，而是 `builder.create(__state__)` 因为 `OpBuilder` 未定义而推导失败，级联到 `return`。补 `mlir/IR/Builders.h` 后整段消失。

## 对比与取舍

| 选择 | 好处 | 代价 |
|---|---|---|
| `.h.inc` + `.cpp.inc` 分离 | 声明轻、实现重，编译依赖少 | 两份 include 都要记得 |
| 全部塞进头文件 | 少一个文件 | 每个 TU 重复生成，编译时间爆炸 |
| 不读生成物，只用访问器 | 上手快 | 报错定位慢、升级被动 |

## 踩雷集锦

1. **忘了 `#define GET_OP_CLASSES`**：不报错，但类定义整段消失，随后报"未定义类型"。
2. **只 include 头文件不 include 实现**：链接期报 `MLIR_DEFINE_EXPLICIT_TYPE_ID` 相关符号未定义。
3. **手改 `.inc`**：下次生成即丢失；所有修改必须回到 `.td`。
4. **多 variadic 不加 `AttrSizedOperandSegments`**：tblgen 直接报错并打 stack dump。
5. **以为 `getODSOperands` 是虚函数**：它是内联成员，override 不了。

## 进阶：再往深一层

- 读 `OpDefinitionsGen.cpp` 的 `OpEmitter::genOpClass`，对照本章的段落顺序——生成物里各段的排列与代码里的调用顺序基本一致。
- 打开 `scale.cpp.inc`，找 `getPropertiesAsAttr` 与 `setPropertiesFromAttr`，理解 properties 如何退化回 `DictionaryAttr`（bytecode 与 generic form 都走这条路）。
- 看 `variadic.h.inc:209-215` 的 `getAttributeNameForIndex` 返回空 `StringAttr`：这个 op 没有 ODS 属性（`operandSegmentSizes` 存在 properties 里），生成器在 `attributes.empty()` 时就走空实现（`OpDefinitionsGen.cpp:1253-1254`）。

## 动手练习

> 在 `.scratch\tblgen-work` 下做。每题记录"命令 + 退出码 + 关键行号"。

### 练习 1：给 `toy.add` 加一个结果

**任务**：把 `results` 改成两个，重新生成两遍。

**测试用例**：
1. `.h.inc` 出现 `NResults<2>::Impl`，`OneResult` 消失。
2. `.h.inc` 出现 `OpAsmOpInterface::Trait`。
3. `.cpp.inc` 的 builder 出现 `assert(resultTypes.size() == 2u ...)`。

<details>
<summary>参考解答</summary>

同步把 `assemblyFormat` 改成 ``$lhs `,` $rhs attr-dict `:` type($lhs) `,` type($rhs) `->` type($sum) `,` type($diff)``（否则 tblgen 报 `expected variable to refer to an argument...`），然后跑两条命令。三条都是本机实测。

</details>

**常见卡点**：只改 `results` 不改格式串。

### 练习 2：定位 `getODSOperands` 的生成位置

**任务**：在 `OpDefinitionsGen.cpp` 里找到生成 `getODSOperands` 的代码行。

**测试用例**：
1. 找到 `addInlineMethod(..., "getODSOperands", ...)` 附近（`:2219`）。
2. 说明它属于 adaptor 段还是 op 段。
3. 对照 `toy.h.inc:81-85` 与 `132-136`，指出两者差异（`std::next(odsOperands...)` vs `std::next(getOperation()->operand_begin()...)`）。

<details>
<summary>参考解答</summary>

`:2219` 在 `genOperandGetters` 路径上，同时服务 adaptor 与 op 两个类模板，靠传入的 range 表达式区分。这正是为什么两处代码形状相同、数据源不同。

</details>

**常见卡点**：只搜 `getODSOperands` 会命中多处，先按文件名缩小到 `OpDefinitionsGen.cpp`。

### 练习 3：删掉 trait，复现 tblgen 报错

**任务**：把 `variadic.td` 里的 `[Pure, AttrSizedOperandSegments]` 改回 `[Pure]`，跑一次 `-gen-op-decls`。

**测试用例**：
1. 退出码 1。
2. 报错原文是 `op has multiple variadic operands but no specification over their sizes`。
3. 错误位置指向 `def Toy_ConcatOp` 那一行。

**常见卡点**：以为这是生成器 bug 去改 TableGen 源码；它是有意为之的诊断。

<details>
<summary>参考解答</summary>

`AttrSizedOperandSegments` 会生成 `operandSegmentSizes` 属性记录两组 variadic 的边界；删掉后 tblgen 无法区分它们，于是在生成 Op 类时直接拒绝。
</details>

### 练习 4：解释一次链接失败

**任务**：故意只 include `toy.h.inc`（不 include `toy.cpp.inc`），编译并链接一个小程序，观察错误。

**测试用例**：
1. 编译通过。
2. 链接报 `MLIR_DEFINE_EXPLICIT_TYPE_ID` 或 `TypeID` 相关符号未定义。
3. 补上 `.cpp.inc` 后链接通过。

<details>
<summary>参考解答</summary>

`.h.inc` 里的 `MLIR_DECLARE_EXPLICIT_TYPE_ID(::toy::AddOp)` 只声明，定义在 `.cpp.inc` 末尾的 `MLIR_DEFINE_EXPLICIT_TYPE_ID`。缺定义时 `TypeID::get<AddOp>()` 无法取地址，链接失败。

</details>

**常见卡点**：把链接错误当成"生成器 bug"；其实是 include 姿势不完整。

## 本章重点整理

- 同一 `.td` 两遍生成：`.h.inc` 管"怎么用"，`.cpp.inc` 管"怎么实现"。
- 访问器的两个根是 `getODSOperands` 与 `getODSResults`；其余都是薄包装。
- adaptor 三层：`GenericAdaptorBase`（状态）、`GenericAdaptor<RangeT>`（模板）、`Adaptor`（`ValueRange` + verify）。
- 有属性就有 `Properties` 结构体与 `BytecodeOpInterface`；`EmptyProperties` 是零属性的特例。
- 多 variadic 必须 `AttrSizedOperandSegments`，尺寸存进 `Properties`，由 trait verifier 校验。
- `parse` / `print` 通过 `OperationName` 的模板钩子注册，入口是 `insert<T>`。

## 自我检核

- [ ] 能说出 `.h.inc` 六个段落各自负责什么
- [ ] 能指出 `getODSOperands` 在 adaptor 与 op 里的两处实现及差异
- [ ] 知道 `properties` 引入后哪些生成函数会出现
- [ ] 能画出 `operandSegmentSizes` 从 builder 到 `getODSOperands` 的路径
- [ ] 知道 `parse`/`print` 如何被 `RegisteredOperationName` 调用
- [ ] 记得 `.h.inc` 与 `.cpp.inc` 的 type id 声明/定义必须配对

## 延伸阅读

- **`mlir/include/mlir/IR/OpDefinition.h`**：读 `class Op`（`:1712`）、`getPrintAssemblyFn`（`:2001`）、`getProperties`（`:2016`）、`AttrSizedOperandSegments`（`:1376-1385`）。学"生成物依赖的基类契约"；本章每个 `using` 都能在这里找到出处。
- **`mlir/include/mlir/IR/OperationSupport.h`**：读 `RegisteredOperationName`（`:549` 起）与 `insert<T>`（`:705`）。学"op 如何被注册与查表调用"；解释本章最后一节的注册链路。
- **`mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp`**：按 `genOpClass` → `genOperandGetters` → `genOpBuilders` → `genOpVerifier` 的顺序读。学"生成物的产出顺序"；对照本章的段落编号。

→ [Ch 14 自定义 Type 与 Attribute](./14-custom-types-attributes.md)
