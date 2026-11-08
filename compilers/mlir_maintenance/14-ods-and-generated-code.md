# Ch 14 — ODS 与生成代码：从 `.td` 到你能读的 C++

> **目标**：把 ODS（Operation Definition Specification）看成一条可复现的编译管线：知道 `mlir-tblgen` 的每条生成线产出什么、生成物里的 `getODSOperands`/builder/verifier 从哪来、以及改一行 `.td` 会怎样改变下游编译单元。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（只读），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM/MLIR `24.0.0git`，assertions ON。

> **前置**：读过 Ch 05 的 Type/Attribute storage 与 Ch 07 的 verifier 基础；没读过也能跟上，本章只依赖“ODS 生成 C++”这一件事。

## 14.1 为什么维护者要读生成代码

维护 MLIR 方言时，你写的是 `.td`，编译器看到的却是 `.h.inc` / `.cpp.inc`。两者之间隔着一层 4,800 行的 `OpDefinitionsGen.cpp` 和 4,788 行的 `OpFormatGen.cpp`。这层生成器决定了四类维护者每天都会碰到的事：

1. **API 形状**。`AddIOp::getLhs()` 返回 `TypedValue<Type>` 还是 `Value`，由 ODS 的约束和生成器决定；下游 dialect 的 C++ 代码直接依赖这个形状。
2. **builder 重载集**。同一个 op 生成 11 个 `build` 重载（本章实测），你写 `builder.create<AddIOp>(...)` 时选中哪一个，取决于参数类型和 properties。
3. **verifier 顺序**。`verifyInvariantsImpl()` 先查 properties、attributes，再查 operands/results；trait 和自定义 `verify()` 在它之后。顺序错了，诊断信息就会指向错误的位置。
4. **bytecode/ABI**。inherent attribute 变成 property 后，生成的 `Properties` 结构体和 bytecode 编码都会变；这是“看起来只是重构”却能破坏兼容性的改动。

一个具体尺度：`ArithOps.td` 只有 2,127 行、56 个 `def Arith_*`，生成物却是：

```text
14667  ArithOps.h.inc
24234  ArithOps.cpp.inc
```

合计 38,901 行。维护者不可能通读生成物，但必须知道**去哪里找哪一段**。

## 14.2 先建立直觉：ODS 是一条三段管线

```text
  ArithOps.td
      │  TableGen frontend：include、class、def、let
      ▼
  RecordKeeper（内存里的 record 图）
      │  mlir-tblgen 选择 generator
      ├───────────────┬────────────────┬─────────────────┐
      ▼               ▼                ▼                 ▼
  -gen-op-decls   -gen-op-defs    -gen-dialect-*   -gen-typedef-*/attrdef-*
      │               │                │                 │
      ▼               ▼                ▼                 ▼
  ArithOps.h.inc  ArithOps.cpp.inc  ArithOpsDialect.*  ArithOpsTypes.*
      │               │
      └──────┬────────┘
             ▼
  ArithOps.h / ArithOps.cpp（手写的 include 壳）
             ▼
  libMLIRArithDialect.so
```

三个关键点：

- **TableGen 前端只负责“读定义”**。`include` 顺序、`class` 继承、`let` 覆盖都在这一步完成；错误也在这一步报（例如本章练习里的 `Variable not defined: 'Pure'`）。
- **每个 generator 只输出一个片段**。`-gen-op-decls` 不生成 parser，`-gen-op-defs` 不生成 dialect 注册；它们由不同的 `.inc` 承载，再由手写 `.cpp` 按固定位置 include。
- **生成物是确定性的**。同一份 `.td`、同一份 `mlir-tblgen`，两次输出逐字节相同（本章实测 sha256 相同）。这让“生成物 diff”成为可靠的 review 工具。

## 14.3 生成线清单：先知道有哪些 `.inc`

`mlir-tblgen --help` 的 “Generator to run” 段有 50 多个选项。维护方言最常用的是下面这些：

| generator | 典型输出 | 内容 |
|---|---|---|
| `-gen-op-decls` | `XOps.h.inc` | op 类、Adaptor、builder 声明、`getODSOperands` |
| `-gen-op-defs` | `XOps.cpp.inc` | builder 定义、parse/print、verifier 实现 |
| `-gen-dialect-decls` | `XOpsDialect.h.inc` | `class XDialect` |
| `-gen-dialect-defs` | `XOpsDialect.cpp.inc` | `initialize()`、`getCanonicalizationPatterns` 等 |
| `-gen-typedef-decls/-defs` | `XOpsTypes.h.inc/.cpp.inc` | 自定义 Type |
| `-gen-attrdef-decls/-defs` | `XOpsAttributes.h.inc/.cpp.inc` | 自定义 Attribute |
| `-gen-rewriters` | `XOps.cpp.inc` | `Pattern` 的 C++ 代码（`PatternBase.td`） |
| `-gen-bytecode` | `XOpsBytecode.cpp.inc` | dialect bytecode 读写 |

真正决定“生成哪一段”的注册点在：

```text
mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp:4847  genOpDecls("gen-op-decls", ...)
mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp:4852  genOpDefs("gen-op-defs", ...)
```

你可以把 `mlir-tblgen` 当成一个“按 generator 分派的纯函数”：输入 `.td` + include 路径，输出一个文本片段。它不读源码树之外的状态，也不写构建目录以外的文件。

## 14.4 生成一份 decls：从 2,127 行到 14,667 行

先跑一次最小生成（命令和输出都是本机实测）：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
S=/home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include \
  $S/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td -o /tmp/arith-decls.h.inc
wc -l /tmp/arith-decls.h.inc
```

实测输出：

```text
14667 /tmp/arith-decls.h.inc
```

再定位 `AddIOp`：

```bash
grep -n "class AddIOp" /tmp/arith-decls.h.inc | head
grep -n "class AddIOp :" /tmp/arith-decls.h.inc | cut -c1-120
```

实测：

```text
69:class AddIOp;
1629:class AddIOpGenericAdaptorBase {
1681:class AddIOpGenericAdaptor : public detail::AddIOpGenericAdaptorBase {
1723:class AddIOpAdaptor : public AddIOpGenericAdaptor<::mlir::ValueRange> {
1730:class AddIOp : public ::mlir::Op<AddIOp, ::mlir::OpTrait::ZeroRegions, ::mlir::OpTrait::OneResult, ::mlir::OpTrait:
```

第 1730 行是真正的 op 类。它的模板参数列表就是 ODS 的“trait 清单”：

```cpp
class AddIOp : public ::mlir::Op<AddIOp,
    ::mlir::OpTrait::ZeroRegions,
    ::mlir::OpTrait::OneResult,
    ::mlir::OpTrait::OneTypedResult<::mlir::Type>::Impl,
    ::mlir::OpTrait::ZeroSuccessors,
    ::mlir::OpTrait::NOperands<2>::Impl,
    ::mlir::OpTrait::OpInvariants,
    ::mlir::BytecodeOpInterface::Trait,
    ::mlir::OpTrait::IsCommutative,
    ...
    ::mlir::InferTypeOpInterface::Trait> {
```

读法：`ZeroRegions`/`OneResult`/`NOperands<2>` 是结构 trait，`OpInvariants` 是 ODS 生成 verifier 的挂点，后面的 `BytecodeOpInterface`、`IsCommutative`、`InferTypeOpInterface` 来自 `ArithOps.td` 的 trait 列表与接口声明。**维护者改 `.td` 的 trait，就是在改这一行模板参数**。

生成物和构建目录里的副本一致，可以直接对照：

```bash
sha256sum /tmp/arith-decls.h.inc \
  $T/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc
```

实测：

```text
e87bb8c14e224f45ec214ec406e3868387ee6ef30532a2a454438104c00ab0c6  /tmp/arith-decls.h.inc
e87bb8c14e224f45ec214ec406e3868387ee6ef30532a2a454438104c00ab0c6  /home/ubuntu/llvm/builds/ct-instr-trunk/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc
```

这条等式很重要：你在 `/tmp` 里复现的生成物，和构建时真正喂给 C++ 编译器的文件**完全是同一份**。任何“生成器行为”问题都可以这样离线复现，不需要重编整个 MLIR。

## 14.5 `getODSOperands`：单值、可变参数与 segment

ODS 生成的核心访问器是 `getODSOperands(unsigned index)`。它不是“按名字取字段”，而是“按 ODS operand 序号取一段 `ValueRange`”。

### 14.5.1 固定 arity：slice 一个元素

`AddIOp` 的生成物在 `ArithOps.h.inc:1769`：

```cpp
::mlir::Operation::operand_range getODSOperands(unsigned index) {
  auto [start, length] = getODSOperandIndexAndLength(index);
  return getOperation()->getOperands().slice(start, length);
}

::mlir::TypedValue<::mlir::Type> getLhs() {
  return ::llvm::cast<::mlir::TypedValue<::mlir::Type>>(
      *getODSOperands(0).begin());
}
```

`getODSOperandIndexAndLength` 对固定 arity 的 op 直接返回 `{index, 1}`（`ArithOps.h.inc:1765`）。所以 `getLhs()` 就是“取第 0 段、解引用第一个值、cast 成 `TypedValue<Type>`”。

### 14.5.2 变长 operand：`Variadic`

变长 operand 的 `length` 不是 1，而是由 segment 信息决定。ODS 有两种模式：

- **单变长段**：如果只有一个 `Variadic`，长度可以由 `getNumOperands()` 推出，不需要额外属性。
- **多个变长/可选段**：必须加 `AttrSizedOperandSegments`，把每段长度存进 `operandSegmentSizes`。

`test.attr_sized_operands` 是一个真实例子（`TestOps.td:1249`）。它的生成物在 `TestOps.h.inc:4603`，Properties 里直接有：

```cpp
struct Properties {
  using operandSegmentSizesTy = std::array<int32_t, 4>;
  operandSegmentSizesTy operandSegmentSizes;
  ...
};
```

对应的 `getODSOperandIndexAndLength` 在 `TestOps.cpp.inc:3265`：

```cpp
std::pair<unsigned, unsigned>
AttrSizedOperandOp::getODSOperandIndexAndLength(unsigned index) {
  ::llvm::ArrayRef<int32_t> sizeAttr = getProperties().operandSegmentSizes;

  unsigned start = 0;
  for (unsigned i = 0; i < index; ++i)
    start += sizeAttr[i];
  return {start, sizeAttr[index]};
}
```

这就是“operand segment”的全部机制：**前 i 段长度求和得到起点，第 i 段长度给出切片长度**。维护者如果不小心把 `operandSegmentSizes` 的顺序和 ODS 的 operand 顺序写反，生成的 `getA()`/`getB()` 会静默指向错误的 `ValueRange`——verifier 通常抓不到，因为每个 `Value` 单独看类型是对的。

### 14.5.3 表格：三种 operand 形状

| ODS 写法 | 生成的长度来源 | 典型错误 |
|---|---|---|
| `I32:$lhs` | `{index, 1}` | 把 `getLhs()` 当 `ValueRange` 用 |
| `Variadic<AnyType>:$args` | 单段，`getNumOperands()` 推 | 与其它变长段混用却忘加 segment trait |
| `Variadic + Optional + Variadic` | `operandSegmentSizes` property | segment 顺序与 ODS 不一致 |

## 14.6 builder：一个 op 为什么有 11 个 `build`

```bash
grep -c "void AddIOp::build" \
  $T/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.cpp.inc
```

实测：

```text
11
```

这 11 个重载不是重复劳动，而是覆盖四种维度：

1. **result type 显式 vs 推断**：`build(..., Type result, Value lhs, Value rhs, ...)` 与 `build(..., Value lhs, Value rhs, ...)`。
2. **`Type` vs `TypeRange`**：单结果用 `Type`，多结果/变长用 `TypeRange`。
3. **属性 vs properties**：`IntegerOverflowFlagsAttr`（attribute 形式）与 `IntegerOverflowFlags`（enum/property 形式）。
4. **`ValueRange` + `ArrayRef<NamedAttribute>` 的通用兜底**：给 parser/反序列化用。

最小重载长这样（`ArithOps.cpp.inc:1077` 起，实测片段）：

```cpp
void AddIOp::build(::mlir::OpBuilder &odsBuilder, ::mlir::OperationState &odsState,
                   ::mlir::Type result, ::mlir::Value lhs, ::mlir::Value rhs,
                   ::mlir::arith::IntegerOverflowFlagsAttr overflowFlags) {
  auto &odsProperties = odsState.getOrAddProperties<Properties>();
  odsState.addOperands(lhs);
  odsState.addOperands(rhs);
  if (overflowFlags) {
    odsProperties.overflowFlags = overflowFlags;
  }
  odsState.addTypes(result);
}
```

维护含义：

- 你新增一个 operand/attribute，重载集会重新洗牌；下游 `create<AddIOp>(...)` 可能从“选中推断类型重载”变成“选中显式类型重载”，编译错误看起来像是 API 坏了。
- 重载数量随“是否有 inferReturnTypes、是否有 properties、是否 variadic”组合增长；不要试图手写这些 builder。
- 如果只想验证某个调用会选中哪个重载，最可靠的办法是看生成物里的签名，而不是猜 C++ 重载决议。

## 14.7 properties 与 attributes：生成物的分界线

`AddIOp` 的生成类里有：

```cpp
using Properties = FoldAdaptor::Properties;
```

`overFlowFlags` 在 ODS 里是 `IntegerOverflowFlagsAttr`，但生成物把它放进 `odsProperties.overflowFlags`。这说明：**ODS 可以把一个“看起来像 attribute”的东西生成为 property**。两者的区别不在语法，而在生成物和 bytecode：

| 维度 | inherent attribute | discardable attribute | property |
|---|---|---|---|
| 存放位置 | `Operation` 的 inherent attrs | `Operation` 的 discardable attrs | `Operation` 的 properties 结构体 |
| 生成物 | `getXAttr()` / `getX()` | `getXAttr()` | `getProperties().x` |
| 打印 | 默认进 `attr-dict` | 默认进 `attr-dict` | 需要 `prop-dict` 或自定义格式 |
| bytecode | 作为 attribute 编码 | 作为 attribute 编码 | 从 bytecode v5 起原生编码 |

维护者最容易踩的坑是：**把 attribute 改成 property 后，`assemblyFormat` 里的 `attr-dict` 不再包含它**。生成的 parser 仍然能工作，但打印出来的 IR 少了字段，roundtrip 测试会失败。修法是同时改格式（`prop-dict` 或显式 `$x`），而不是在 verifier 里补。

## 14.8 生成物可复现性：可以 diff，但不要手改

两次生成逐字节相同：

```bash
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include \
  $S/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td -o /tmp/arith-decls2.h.inc
sha256sum /tmp/arith-decls.h.inc /tmp/arith-decls2.h.inc
```

实测：

```text
e87bb8c14e224f45ec214ec406e3868387ee6ef30532a2a454438104c00ab0c6  /tmp/arith-decls.h.inc
e87bb8c14e224f45ec214ec406e3868387ee6ef30532a2a454438104c00ab0c6  /tmp/arith-decls2.h.inc
```

但“可复现”不等于“可以手改”。生成物顶部写着 `Automatically generated file, do not edit!`；构建系统会在下次 `ninja` 时覆盖你。更隐蔽的是：**生成物里的内部符号名会随输入文件名变化**。练习 C 里把 `Mini.td` 复制成 `Mini-copy.td` 后，类型约束函数从 `__mlir_ods_local_type_constraint_Mini1` 变成 `__mlir_ods_local_type_constraint_Mini2Dcopy1`——一行逻辑没改，diff 却出现大量噪声。维护时保持 `.td` 文件名稳定，能让生成物 diff 只反映真实语义变化。

## 14.9 源码落点

| 内容 | 路径 | 关键行/符号（本树 `grep -n` 核对） |
|---|---|---|
| decls/defs 注册 | `mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp` | 4847 `genOpDecls`；4852 `genOpDefs` |
| op 代码生成主类 | 同上 | 535 `class OpEmitter` |
| 命名 operand/result getter | 同上 | 2197 `genNamedOperandGetters`；2275 `genNamedResultGetters` |
| builder 生成 | 同上 | 2976 `genBuilder` |
| verifier 生成 | 同上 | 3806 `genVerifier`；3848 `genCustomVerifier` |
| 声明式格式生成 | `mlir/tools/mlir-tblgen/OpFormatGen.cpp` | 1849 `genParser`；3402 `genPrinter` |
| TableGen 侧 op 视图 | `mlir/include/mlir/TableGen/Operator.h` | 85 `class Operator`（共 447 行） |
| TableGen 侧 dialect 视图 | `mlir/include/mlir/TableGen/Dialect.h` | 30 `class Dialect` |
| op 基类与 trait 挂点 | `mlir/include/mlir/IR/OpDefinition.h` | 381 `TraitBase`；435 `OpInvariants` |
| `assemblyFormat` 字段 | `mlir/include/mlir/IR/OpBase.td` | 400/402/409 |
| Arith op 定义 | `mlir/include/mlir/Dialect/Arith/IR/ArithOps.td` | 52 `Arith_BinaryOp`；271 `Arith_AddIOp` |
| 生成物（本构建） | `$T/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc` | 1730 `class AddIOp`；1769 `getODSOperands` |
| 生成物（本构建） | `$T/.../ArithOps.cpp.inc` | 1077 起 11 个 `AddIOp::build` |

## 14.10 可跑实验

> 全部在本机 `builds/ct-instr-trunk` 真跑；输出原样粘贴。`$T` 与 `$S` 见 14.4。

### 实验 1：生成 decls 并定位 op 类（实测）

```bash
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include \
  $S/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td -o /tmp/arith-decls.h.inc
wc -l /tmp/arith-decls.h.inc
grep -n "class AddIOp :" /tmp/arith-decls.h.inc | cut -c1-120
```

实测：

```text
14667 /tmp/arith-decls.h.inc
1730:class AddIOp : public ::mlir::Op<AddIOp, ::mlir::OpTrait::ZeroRegions, ::mlir::OpTrait::OneResult, ::mlir::OpTrait:
```

### 实验 2：生成 defs 并数 builder 重载（实测）

```bash
$T/bin/mlir-tblgen -gen-op-defs -I $S/mlir/include \
  $S/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td -o /tmp/arith-defs.cpp.inc
wc -l /tmp/arith-defs.cpp.inc
grep -c "void AddIOp::build" /tmp/arith-defs.cpp.inc
grep -n "AddIOp::build" /tmp/arith-defs.cpp.inc | head -3
```

实测：

```text
24234 /tmp/arith-defs.cpp.inc
11
1077:void AddIOp::build(::mlir::OpBuilder &odsBuilder, ::mlir::OperationState &odsState, ::mlir::Type result, ::mlir::Value lhs, ::mlir::Value rhs, ::mlir::arith::IntegerOverflowFlagsAttr overflowFlags) {
1099:void AddIOp::build(::mlir::OpBuilder &odsBuilder, ::mlir::OperationState &odsState, ::mlir::Value lhs, ::mlir::Value rhs, ::mlir::arith::IntegerOverflowFlagsAttr overflowFlags) {
1131:void AddIOp::build(::mlir::OpBuilder &odsBuilder, ::mlir::OperationState &odsState, ::mlir::TypeRange resultTypes, ::mlir::Value lhs, ::mlir::Value rhs, ::mlir::arith::IntegerOverflowFlagsAttr overflowFlags) {
```

观察：第 1077/1099 只差 `Type result`；1131 把 `Type` 换成 `TypeRange`。这就是“显式类型 vs 推断类型”的两组重载。

### 实验 3：验证生成物与构建副本一致（实测）

```bash
sha256sum /tmp/arith-decls.h.inc \
  $T/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc
```

实测：

```text
e87bb8c14e224f45ec214ec406e3868387ee6ef30532a2a454438104c00ab0c6  /tmp/arith-decls.h.inc
e87bb8c14e224f45ec214ec406e3868387ee6ef30532a2a454438104c00ab0c6  /home/ubuntu/llvm/builds/ct-instr-trunk/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc
```

### 实验 4：观察 operand segment 的生成代码（实测）

```bash
H=$T/tools/mlir/test/lib/Dialect/Test/TestOps.h.inc
C=$T/tools/mlir/test/lib/Dialect/Test/TestOps.cpp.inc
grep -n "class AttrSizedOperandOp :" $H
grep -n "AttrSizedOperandOp::getODSOperandIndexAndLength" $C
```

实测：

```text
4603:class AttrSizedOperandOp : public ::mlir::Op<AttrSizedOperandOp, ...
3265:std::pair<unsigned, unsigned> AttrSizedOperandOp::getODSOperandIndexAndLength(unsigned index) {
```

再打开 `TestOps.h.inc:4499-4502`，能看到 `std::array<int32_t, 4>` 的 `operandSegmentSizes`。

### 实验 5：生成 dialect 声明（实测）

```bash
$T/bin/mlir-tblgen -gen-dialect-decls -I $S/mlir/include \
  $S/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td -o /tmp/arith-dialect.h.inc
cat /tmp/arith-dialect.h.inc
```

实测（节选）：

```text
class ArithDialect : public ::mlir::Dialect {
  explicit ArithDialect(::mlir::MLIRContext *context);
  void initialize();
  friend class ::mlir::MLIRContext;
public:
  ~ArithDialect() override;
  static constexpr ::llvm::StringLiteral getDialectNamespace() {
    return ::llvm::StringLiteral("arith");
  }
};
```

这解释了一个常见疑问：为什么 `ArithOps.td` 能生成 `ArithDialect`？因为 `ArithBase.td` 里的 `def Arith_Dialect : Dialect` 也在同一份 record 图里。

### 实验 6：改名会污染生成物 diff（实测）

```bash
cat > /tmp/MiniName.td <<'EOF'
#ifndef MINI_TD
#define MINI_TD
include "mlir/IR/OpBase.td"
def Mini_Dialect : Dialect { let name = "mini"; let cppNamespace = "::mini"; }
class Mini_Op<string mnemonic, list<Trait> traits = []> : Op<Mini_Dialect, mnemonic, traits>;
def Mini_AddOp : Mini_Op<"add"> {
  let arguments = (ins I32:$lhs, I32:$rhs);
  let results = (outs I32:$result);
  let assemblyFormat = "$lhs `,` $rhs attr-dict `:` type($result)";
}
#endif
EOF
cp /tmp/MiniName.td /tmp/MiniName-copy.td
$T/bin/mlir-tblgen -gen-op-defs -I $S/mlir/include /tmp/MiniName.td -o /tmp/name1.cpp.inc
$T/bin/mlir-tblgen -gen-op-defs -I $S/mlir/include /tmp/MiniName-copy.td -o /tmp/name2.cpp.inc
grep -n "__mlir_ods_local_type_constraint" /tmp/name1.cpp.inc /tmp/name2.cpp.inc | grep static
```

实测：

```text
/tmp/name1.cpp.inc:30:static ::llvm::LogicalResult __mlir_ods_local_type_constraint_MiniName1(
/tmp/name2.cpp.inc:30:static ::llvm::LogicalResult __mlir_ods_local_type_constraint_MiniName2Dcopy1(
```

两份 `.td` 内容完全相同，只有文件名不同；生成物的内部分派函数却从 `MiniName1` 变成 `MiniName2Dcopy1`，但语义没变。维护时不要为了让 diff “看起来干净”而手改生成物，正确做法是保持输入文件名稳定。

## 14.11 踩雷集锦

1. **以为生成物是“源码”，可以直接 patch。** → 生成物会在下次 `ninja` 被覆盖；要改行为就改 `.td` 或生成器。
2. **把 `getODSOperands(i)` 当成“第 i 个 operand”。** → 它是“第 i 段 `ValueRange`”；对 variadic op，段内可能有多个值。
3. **多段 variadic 忘加 `AttrSizedOperandSegments`。** → parser 能过，但 `getX()` 会切错段；加 trait 后还要保证 segment 顺序与 ODS 一致。
4. **看到 11 个 `build` 就以为有重复实现。** → 它们覆盖显式/推断类型、`Type`/`TypeRange`、attr/property、parser 兜底四类调用；删任何一个都会破坏下游。
5. **把 attribute 改成 property 却不改 `assemblyFormat`。** → 打印物少字段，roundtrip 失败；`attr-dict` 不包含 property。
6. **用 `grep` 的行号当跨版本契约。** → 行号随 commit 漂移；引用时同时给函数名或 `getOperationName()`。
7. **在 `/tmp` 生成时漏 `-I $S/mlir/include`。** → 报 `Could not find include file`；`include "mlir/IR/OpBase.td"` 需要 include 根。
8. **改名 `.td` 后惊讶于 diff 变大。** → 生成器的内部符号名含文件名；保持文件名稳定。

## 14.12 本章重点整理

- ODS 是“`.td` → record 图 → 按 generator 输出 `.inc`”的三段管线；每段都可离线复现。
- `-gen-op-decls`/`-gen-op-defs` 是两条独立生成线；decls 里是类与访问器，defs 里是 builder/parser/verifier 实现。
- `getODSOperands(i)` 返回第 i 段 `ValueRange`；固定 arity 的段长是 1，variadic 多段依赖 `operandSegmentSizes`。
- 一个 op 的 builder 重载数由“类型推断 × Type/TypeRange × attr/property × 通用兜底”决定；`AddIOp` 在本树是 11 个。
- properties 与 attributes 的分界会同时影响打印、bytecode 和下游 C++；改属性存储方式必须同步改 assembly format。
- 生成物逐字节可复现（sha256 相同），因此可以安全地 diff；但内部符号名受输入文件名影响。
- 维护者的工作不是读 38,901 行生成物，而是知道“这段行为由哪个 generator、哪个函数生成”。

## 14.13 自我检核

- [ ] 能说出 `-gen-op-decls` 与 `-gen-op-defs` 各输出什么，以及它们分别被哪个手写文件 include
- [ ] 能解释 `getODSOperands(1)` 与 `getRhs()` 的关系，以及 variadic 段长从哪来
- [ ] 能列出 `AddIOp` 的 builder 重载至少三种不同签名，并说明各自适用场景
- [ ] 能指出 `operandSegmentSizes` 存在哪里、为什么 verifier 抓不到段顺序错误
- [ ] 能复现“生成物与构建副本 sha256 相同”，并解释这条等式的用途
- [ ] 能说出把 attribute 改成 property 后，`assemblyFormat` 需要改哪里
- [ ] 能解释为什么改名 `.td` 会让生成物 diff 出现大量内部符号改名
- [ ] 能定位 `OpDefinitionsGen.cpp` 里 `genVerifier` 与 `genCustomVerifier` 的分工

## 14.14 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp`**（4,856 行）
  - 读哪里：535–720 的 `OpEmitter` 声明；2197–2360 的 named getter；2976–3100 的 builder；3806–3900 的 verifier。
  - 能学到什么：每个生成片段对应的 C++ 拼接逻辑，以及 trait/属性如何映射到模板参数。
  - 和本章关联：14.4–14.7 的所有生成物都能在这里找到“为什么长这样”。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/tools/mlir-tblgen/OpFormatGen.cpp`**（4,788 行）
  - 读哪里：1849–1900 的 `genParser`；3402–3480 的 `genPrinter`；1019 附近的 optional group 生成。
  - 能学到什么：声明式格式如何变成手写风格的 parser/printer 代码。
  - 和本章关联：Ch 15 会把这条生成线展开。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/TableGen/Operator.h`**（447 行）
  - 读哪里：85–200 的 `Operator` 接口：`getOperands()`、`getResults()`、`getTraits()`。
  - 能学到什么：生成器看到的“op 视图”与最终 C++ 类之间的中间层。
  - 和本章关联：解释为什么 `.td` 的 class 继承会影响生成的 trait 列表。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td`**（2,127 行）
  - 读哪里：42–70 的 `Arith_Op`/`Arith_BinaryOp`；271 的 `Arith_AddIOp`。
  - 能学到什么：一个成熟方言如何用基类复用 operand/result/assemblyFormat。
  - 和本章关联：14.4 的 56 个 `def` 都从这几行基类派生。
- **`/home/ubuntu/llvm/builds/ct-instr-trunk/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc`**
  - 读哪里：1730–1810 的 `class AddIOp`；1769 的 `getODSOperands`。
  - 能学到什么：真实构建产物，而不是文档示例。
  - 和本章关联：14.5/14.6 的所有片段都取自这里。

下一章把镜头从“生成什么”转到“文本长什么样”：自定义 assembly format 如何解析、打印，以及为什么 parser 和 printer 必须成对维护。

→ 下一章：[Ch 15 Assembly Format、Parser 与 Printer](./15-assembly-format-parser-printer.md)
