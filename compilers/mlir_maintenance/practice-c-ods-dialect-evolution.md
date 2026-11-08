# 练习 C — 用 ODS 演化一个小方言

> **目标**：在 `/tmp` 里从零写一个最小方言 `mini`，用 `mlir-tblgen` 生成 decls/defs；然后做一次真实演化——改 `assemblyFormat`、加一个 op、加 verifier——用 diff 证明“每一行生成物变化都能对应到 `.td` 的哪一处改动”。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（只读），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM/MLIR `24.0.0git`。所有生成都在 `/tmp/mlir-practice-c`，不碰源码树。

> **前置**：Ch 14 的生成管线、Ch 15 的 `assemblyFormat`。本练习不要求你会写 pass 或编译完整方言；只要求会读生成物。

## C.1 任务与产出

你要交付的不是“一个能跑的方言”，而是**三份可复现的证据**：

1. `v1/Mini.td` + 生成的 `v1/decls.h.inc`、`v1/defs.cpp.inc`；
2. `v2/Mini.td`（改了格式、加了 op、加了 verifier）+ 对应生成物；
3. 一份 `diff`，能逐段解释每个 hunk 的来源。

验收标准：

- `wc -l` 能复现 355/459（v1）与 542/720（v2）；
- `mlir-tblgen` 两次生成 sha256 相同；
- 能指出 `verify()` 声明、`SubOp` 类、`lhsRawType` 解析变量分别在哪个文件、哪一行；
- 能复现至少两类 `mlir-tblgen` 格式错误，并读懂错误消息。

> 本练习**不编译**生成的 C++。原因：把 `mini` 接进一个可链接的 dialect 需要 CMake target、dialect 注册、`MiniDialect.cpp` 手写实现和 `mlir-opt` 重编；这超出“ODS 演化”的范围。生成物的正确性用 `mlir-tblgen` 的成功/失败和 diff 验证；链接/运行阶段明确标为未实测。

## C.2 先建立直觉：v1 到 v2 改了三处

```text
  v1/Mini.td                         v2/Mini.td
  ┌──────────────────────┐          ┌──────────────────────────────┐
  │ Mini_Dialect         │          │ Mini_Dialect（不变）          │
  │ Mini_Op              │          │ Mini_Op（不变）               │
  │ Mini_AddOp           │          │ Mini_AddOp                    │
  │   assemblyFormat:    │          │   assemblyFormat:             │
  │   $lhs,$rhs :        │  ──────▶ │   $lhs,$rhs : type($lhs),     │
  │   type($result)      │          │              type($result)    │
  │   (无 verifier)       │          │   hasVerifier = 1             │
  │ Mini_ZeroOp          │          │ Mini_ZeroOp（不变）            │
  │                      │          │ Mini_SubOp（新增）             │
  └──────────────────────┘          └──────────────────────────────┘
          │                                      │
          ▼                                      ▼
  355 行 decls + 459 行 defs          542 行 decls + 720 行 defs
```

三处改动分别验证三种生成路径：

| 改动 | 触发的生成器逻辑 | 你应该在 diff 里看到 |
|---|---|---|
| `assemblyFormat` 加 `type($lhs)` | `OpFormatGen::genParser` / `genPrinter` | 多一个 `lhsRawType`、多一次 `parseComma`、printer 多打印一段类型 |
| 加 `Mini_SubOp` | `OpEmitter` 为新 op 生成整套类 | 新增 `class SubOp`、`SubOp::parse`、`SubOp::build` |
| 加 `hasVerifier = 1` | `OpEmitter::genCustomVerifier` | decls 多 `verify()`；defs 的 `verifyInvariants` 改成 `... && verify()` |

## C.3 Step 0：工作目录

```bash
mkdir -p /tmp/mlir-practice-c/v1 /tmp/mlir-practice-c/v2
cd /tmp/mlir-practice-c
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
S=/home/ubuntu/llvm/trees/ct-instr-trunk
```

先确认工具可用：

```bash
$T/bin/mlir-tblgen --version
```

实测：

```text
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
```

## C.4 Step 1：写 v1 的 `Mini.td`

<details>
<summary>参考：完整的 v1 `Mini.td`</summary>

```tablegen
#ifndef MINI_TD
#define MINI_TD

include "mlir/IR/OpBase.td"

def Mini_Dialect : Dialect {
  let name = "mini";
  let cppNamespace = "::mini";
  let summary = "A minimal dialect for the ODS practice.";
}

class Mini_Op<string mnemonic, list<Trait> traits = []>
    : Op<Mini_Dialect, mnemonic, traits>;

def Mini_AddOp : Mini_Op<"add"> {
  let arguments = (ins I32:$lhs, I32:$rhs);
  let results = (outs I32:$result);
  let assemblyFormat = "$lhs `,` $rhs attr-dict `:` type($result)";
}

def Mini_ZeroOp : Mini_Op<"zero"> {
  let results = (outs I32:$result);
  let assemblyFormat = "attr-dict `:` type($result)";
}

#endif // MINI_TD
```

</details>

四个设计点：

- `include "mlir/IR/OpBase.td"` 提供 `Dialect`、`Op`、`I32`；没有它，`I32` 和 `Op` 都不认识。
- `Mini_Dialect` 的 `name = "mini"` 决定文本 namespace；`cppNamespace = "::mini"` 决定生成类的命名空间。
- `Mini_Op` 是基类，把 dialect 和默认 trait 列表固定下来。
- `assemblyFormat` 只要求 `type($result)`，因为两个 operand 都是 `I32`，parser 能用 ODS 约束推回类型。

## C.5 Step 2：生成 v1 并记录基线

```bash
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include v1/Mini.td -o v1/decls.h.inc
$T/bin/mlir-tblgen -gen-op-defs  -I $S/mlir/include v1/Mini.td -o v1/defs.cpp.inc
wc -l v1/decls.h.inc v1/defs.cpp.inc
```

实测：

```text
  355 v1/decls.h.inc
  459 v1/defs.cpp.inc
  814 total
```

再生成 dialect 声明，确认 `Mini_Dialect` 也被 ODS 看见：

```bash
$T/bin/mlir-tblgen -gen-dialect-decls -I $S/mlir/include v1/Mini.td -o v1/dialect.h.inc
wc -l v1/dialect.h.inc
sed -n '1,25p' v1/dialect.h.inc
```

实测：

```text
28 v1/dialect.h.inc
/*===- TableGen'erated file -------------------------------------*- C++ -*-===*\
|*                                                                            *|
|* Dialect Declarations                                                       *|
|*                                                                            *|
|* Automatically generated file, do not edit!                                 *|
|* From: Mini.td                                                              *|
|*                                                                            *|
\*===----------------------------------------------------------------------===*/

#include "mlir/IR/Dialect.h"

namespace mini {

/// A minimal dialect for the ODS practice.
class MiniDialect : public ::mlir::Dialect {
  explicit MiniDialect(::mlir::MLIRContext *context);

  void initialize();
  friend class ::mlir::MLIRContext;
public:
  ~MiniDialect() override;
  static constexpr ::llvm::StringLiteral getDialectNamespace() {
    return ::llvm::StringLiteral("mini");
  }
};
```

注意 `From: Mini.td`：生成物头部记录了输入文件名。这也是 C.9 要讲的“改名污染 diff”的来源。

## C.6 Step 3：演化到 v2

v2 做三件事：

1. `Mini_AddOp` 的 `assemblyFormat` 从 `type($result)` 改成 `type($lhs) `,` type($result)`；
2. 加 `Mini_SubOp`；
3. 给 `Mini_AddOp` 加 `hasVerifier = 1`，并加 `[Pure]` trait（需要 include `SideEffectInterfaces.td`）。

<details>
<summary>参考：完整的 v2 `Mini.td`</summary>

```tablegen
#ifndef MINI_TD
#define MINI_TD

include "mlir/IR/OpBase.td"
include "mlir/Interfaces/SideEffectInterfaces.td"

def Mini_Dialect : Dialect {
  let name = "mini";
  let cppNamespace = "::mini";
  let summary = "A minimal dialect for the ODS practice.";
}

class Mini_Op<string mnemonic, list<Trait> traits = []>
    : Op<Mini_Dialect, mnemonic, traits>;

def Mini_AddOp : Mini_Op<"add", [Pure]> {
  let arguments = (ins I32:$lhs, I32:$rhs);
  let results = (outs I32:$result);
  let assemblyFormat = "$lhs `,` $rhs attr-dict `:` type($lhs) `,` type($result)";
  let hasVerifier = 1;
}

def Mini_ZeroOp : Mini_Op<"zero"> {
  let results = (outs I32:$result);
  let assemblyFormat = "attr-dict `:` type($result)";
}

def Mini_SubOp : Mini_Op<"sub", [Pure]> {
  let arguments = (ins I32:$lhs, I32:$rhs);
  let results = (outs I32:$result);
  let assemblyFormat = "$lhs `,` $rhs attr-dict `:` type($result)";
}

#endif // MINI_TD
```

</details>

生成 v2：

```bash
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include v2/Mini.td -o v2/decls.h.inc
$T/bin/mlir-tblgen -gen-op-defs  -I $S/mlir/include v2/Mini.td -o v2/defs.cpp.inc
wc -l v2/decls.h.inc v2/defs.cpp.inc
```

实测：

```text
  542 v2/decls.h.inc
  720 v2/defs.cpp.inc
 1262 total
```

增量是 187 行 decls、261 行 defs。新增一个 op 的“固定成本”大致就是两百多行：Adaptor、GenericAdaptor、builders、parse/print、properties 辅助函数全都重新生成一份。

## C.7 Step 4：逐段读 diff

### C.7.1 decls：trait、verifier、新 op

```bash
diff -u v1/decls.h.inc v2/decls.h.inc | sed -n '1,60p'
```

实测（节选；长行用 `...` 省略了中间内容）：

```text
@@ -14,6 +14,11 @@
 } // namespace mini
 namespace mini {
 
+class SubOp;
+
+} // namespace mini
+namespace mini {
+
 class ZeroOp;
@@ -111,7 +116,7 @@
-class AddOp : public ::mlir::Op<AddOp, ..., ::mlir::OpTrait::OpInvariants> {
+class AddOp : public ::mlir::Op<AddOp, ..., ::mlir::OpTrait::OpInvariants, ::mlir::ConditionallySpeculatable::Trait, ::mlir::OpTrait::AlwaysSpeculatableImplTrait, ::mlir::MemoryEffectOpInterface::Trait> {
@@ -193,8 +198,10 @@
   ::llvm::LogicalResult verifyInvariantsImpl();
   ::llvm::LogicalResult verifyInvariants();
+  ::llvm::LogicalResult verify();
   static ::mlir::ParseResult parse(::mlir::OpAsmParser &parser, ::mlir::OperationState &result);
   LLVM_ATTRIBUTE_MINSIZE void print(::mlir::OpAsmPrinter &_odsPrinter);
+  void getEffects(::llvm::SmallVectorImpl<::mlir::SideEffects::EffectInstance<::mlir::MemoryEffects::Effect>> &effects);
```

三个 hunk 的读法：

- **`class SubOp;` 前置声明**：ODS 按 op 名字排序生成前置声明，新 op 插在 `AddOp` 和 `ZeroOp` 之间。
- **`AddOp` 的模板参数变长**：`[Pure]` 展开成 `ConditionallySpeculatable` + `AlwaysSpeculatableImplTrait` + `MemoryEffectOpInterface`。这是“trait 列表直接进 `Op<>` 模板参数”的直接证据。
- **`verify()` 声明 + `getEffects`**：`hasVerifier = 1` 生成 `verify()`；`Pure` 生成 `getEffects`（空实现，表示无副作用）。两者都是声明，实现在 defs 或手写 `.cpp`。

<details>
<summary>参考：`SubOp` 类的完整模板参数</summary>

```bash
grep -n "class SubOp :" v2/decls.h.inc
```

实测：

```text
300:class SubOp : public ::mlir::Op<SubOp, ::mlir::OpTrait::ZeroRegions, ::mlir::OpTrait::OneResult, ::mlir::OpTrait::OneTypedResult<::mlir::IntegerType>::Impl, ::mlir::OpTrait::ZeroSuccessors, ::mlir::OpTrait::NOperands<2>::Impl, ::mlir::OpTrait::OpInvariants, ::mlir::ConditionallySpeculatable::Trait, ::mlir::OpTrait::AlwaysSpeculatableImplTrait, ::mlir::MemoryEffectOpInterface::Trait> {
```

`SubOp` 和 `AddOp` 的模板参数只差 op 名字；因为两者都是 `[Pure]` + 两个 `I32` operand + 一个 `I32` result。这正是“用基类和 trait 复用”的效果。

</details>

### C.7.2 defs：verifier 调用链

```bash
diff -u v1/defs.cpp.inc v2/defs.cpp.inc | sed -n '1,40p'
```

实测（节选）：

```text
@@ -11,6 +11,7 @@
 ::mini::AddOp,
+::mini::SubOp,
 ::mini::ZeroOp
@@ -200,7 +201,9 @@
 ::llvm::LogicalResult AddOp::verifyInvariants() {
-  return verifyInvariantsImpl();
+  if(::mlir::succeeded(verifyInvariantsImpl()) && ::mlir::succeeded(verify()))
+    return ::mlir::success();
+  return ::mlir::failure();
 }
```

这是本练习最重要的一段。`hasVerifier = 1` 之前，`verifyInvariants()` 只转发 `verifyInvariantsImpl()`；之后它变成：

```text
verifyInvariantsImpl()  &&  verify()
```

也就是：ODS 约束先过，再跑你手写的 `verify()`。这直接对应 Ch 16 的验证顺序。

<details>
<summary>参考：defs diff 中 parser/printer 的变化</summary>

```bash
diff -u v1/defs.cpp.inc v2/defs.cpp.inc | sed -n '35,90p'
```

下面是从 diff 中摘出的 `+` 行（略去上下文，便于阅读；不是命令原样输出）：

```text
+  ::mlir::Type lhsRawType{};
+  ::llvm::ArrayRef<::mlir::Type> lhsTypes(&lhsRawType, 1);
...
+    lhsRawType = type;
+  }
+  if (parser.parseComma())
+    return ::mlir::failure();
...
-  if (parser.resolveOperands(lhsOperands, odsBuildableType0, lhsOperandsLoc, result.operands))
+  if (parser.resolveOperands(lhsOperands, lhsTypes, lhsOperandsLoc, result.operands))
...
+    auto type = getLhs().getType();
+    if (auto validType = ::llvm::dyn_cast<::mlir::IntegerType>(type))
+      _odsPrinter.printStrippedAttrOrType(validType);
+   else
+     _odsPrinter << type;
+  }
+  _odsPrinter << ",";
+  _odsPrinter << ' ';
```

读法：

- `lhsRawType` 是新增的“解析暂存”；因为格式里多了一个 `type($lhs)`。
- `parseComma()` 是格式里的 `,` 生成的。
- `resolveOperands(lhsOperands, odsBuildableType0, ...)` 变成 `lhsTypes`：以前 operand 类型由 ODS 约束硬编码（`i32`），现在由文本里的类型决定，parser 要检查它是否满足 `I32` 约束。
- printer 端多了一段 `getLhs().getType()` 的打印；格式里写了 `type($lhs)`，printer 就必须输出。

</details>

### C.7.3 新 op 的 defs 规模

```bash
grep -n "SubOp::parse" v2/defs.cpp.inc | head -3
```

实测：

```text
464:::mlir::ParseResult SubOp::parse(::mlir::OpAsmParser &parser, ::mlir::OperationState &result) {
```

从 464 行开始，到文件末尾 720 行，几乎全是 `SubOp` 的 parse/print/build/properties 辅助函数。这解释了 C.6 的 261 行增量。

## C.8 Step 5：故意制造格式错误

格式错误是 `mlir-tblgen` 阶段最能教人的部分。下面四个都是本机实测。

### C.8.1 忘了 include `SideEffectInterfaces.td`

<details>
<summary>参考：错误 1 的完整诊断</summary>

```bash
# 从 v2/Mini.td 删掉 include "mlir/Interfaces/SideEffectInterfaces.td"
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include bad-pure.td -o /tmp/x.h.inc
```

实测：

```text
bad-pure.td:15:34: error: Variable not defined: 'Pure'
def Mini_AddOp : Mini_Op<"add", [Pure]> {
                                 ^
```

`Pure` 不是 `OpBase.td` 里的东西，而是 `SideEffectInterfaces.td` 定义的 trait。TableGen 的错误是“变量未定义”，不是“trait 不存在”；看到 `Variable not defined` 先查 include。

</details>

### C.8.2 格式里引用了不存在的变量

```bash
# 把 type($lhs) 改成 type($missing)
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include bad-name.td -o /tmp/x.h.inc
```

实测：

```text
error: expected variable to refer to an argument, region, result, or successor
$lhs `,` $rhs attr-dict `:` type($missing)
                                 ^
bad-name.td:17:5: note: in custom assembly format for this operation
def Mini_AddOp : Mini_Op<"add", [Pure]> {
    ^
```

注意错误分两层：第一行是格式错误，第二段 note 指回 `.td` 的 op 定义。维护时先看 note 的 `def`，再看格式串的插入点。

### C.8.3 同一个变量绑定两次

```bash
# 把 $rhs 改成 $lhs
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include bad-dup.td -o /tmp/x.h.inc
```

实测：

```text
error: operand 'lhs' is already bound
$lhs `,` $lhs attr-dict `:` type($result)
         ^
bad-dup.td:17:5: note: in custom assembly format for this operation
def Mini_AddOp : Mini_Op<"add", [Pure]> {
    ^
```

格式里每个 operand/result/region 只能绑定一次；重复绑定会让 parser 不知道把第二个值放哪。

### C.8.4 optional group 的 anchor 不是变长元素

```bash
# 把格式改成 "(`(` $lhs^ `)`)? `,` $rhs ..."
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include bad-optional.td -o /tmp/x.h.inc
```

实测：

```text
error: only variable length operands can be used within an optional group
(`(` $lhs^ `)`)? `,` $rhs attr-dict `:` type($result)
^
bad-optional.td:17:5: note: in custom assembly format for this operation
def Mini_AddOp : Mini_Op<"add", [Pure]> {
    ^
```

`$lhs` 是固定 arity operand，不能做 optional group 的 anchor；要么改成 `Optional<I32>`/`Variadic`，要么去掉 optional group。

## C.9 Step 6：可复现性与文件名

### C.9.1 两次生成逐字节相同

```bash
$T/bin/mlir-tblgen -gen-op-decls -I $S/mlir/include v2/Mini.td -o /tmp/v2-check.h.inc
sha256sum v2/decls.h.inc /tmp/v2-check.h.inc
```

实测：

```text
84295c74cd66ad72e1724a1fd401f4721d91edbb4cc1268990feaafdfb38fae8  v2/decls.h.inc
84295c74cd66ad72e1724a1fd401f4721d91edbb4cc1268990feaafdfb38fae8  /tmp/v2-check.h.inc
```

这条等式让你可以安全地“生成到 /tmp 再 diff”，不需要改构建目录。

### C.9.2 改名会污染内部符号

如果只改输入文件名（内容不变），生成物的内部类型约束函数名会变：

```bash
cp v1/Mini.td v1/Mini-copy.td
$T/bin/mlir-tblgen -gen-op-defs -I $S/mlir/include v1/Mini-copy.td -o /tmp/mini-copy-defs.cpp.inc
grep -n "__mlir_ods_local_type_constraint" v1/defs.cpp.inc /tmp/mini-copy-defs.cpp.inc | head -6
```

实测（节选）：

```text
v1/defs.cpp.inc:31:static ::llvm::LogicalResult __mlir_ods_local_type_constraint_Mini1(
v1/defs.cpp.inc:180:      if (::mlir::failed(__mlir_ods_local_type_constraint_Mini1(*this, v.getType(), "operand", index++)))
/tmp/mini-copy-defs.cpp.inc:31:static ::llvm::LogicalResult __mlir_ods_local_type_constraint_Mini2Dcopy1(
```

语义没变，符号名变了。维护时保持 `.td` 文件名稳定，diff 才只反映真实改动。

## C.10 自我检核与验收清单

- [ ] `v1/decls.h.inc` 355 行、`v1/defs.cpp.inc` 459 行
- [ ] `v2/decls.h.inc` 542 行、`v2/defs.cpp.inc` 720 行
- [ ] `v2/decls.h.inc` 里有 `verify()` 声明（201 行）
- [ ] `v2/decls.h.inc` 里有 `class SubOp`（300 行）
- [ ] `v2/defs.cpp.inc` 的 `AddOp::verifyInvariants` 调用 `verify()`（203 行起）
- [ ] `v2/defs.cpp.inc` 里有 `lhsRawType` 与 `resolveOperands(..., lhsTypes, ...)`
- [ ] 能复现四类格式错误中的至少两类
- [ ] 知道“改名 `.td` 会改内部符号名”

<details>
<summary>参考：一键验收脚本</summary>

```bash
cd /tmp/mlir-practice-c
wc -l v1/decls.h.inc v1/defs.cpp.inc v2/decls.h.inc v2/defs.cpp.inc
grep -n "verify();" v2/decls.h.inc
grep -n "class SubOp :" v2/decls.h.inc
grep -n "AddOp::verifyInvariants" v2/defs.cpp.inc
grep -n "lhsRawType" v2/defs.cpp.inc | head -3
sha256sum v2/decls.h.inc
```

实测输出（节选）：

```text
  355 v1/decls.h.inc
  459 v1/defs.cpp.inc
  542 v2/decls.h.inc
  720 v2/defs.cpp.inc
201:  ::llvm::LogicalResult verify();
300:class SubOp : public ::mlir::Op<SubOp, ...
203:::llvm::LogicalResult AddOp::verifyInvariants() {
216:  ::mlir::Type lhsRawType{};
84295c74cd66ad72e1724a1fd401f4721d91edbb4cc1268990feaafdfb38fae8  v2/decls.h.inc
```

</details>

## C.11 源码落点

| 内容 | 路径 | 关键行/符号（本树 `grep -n` 核对） |
|---|---|---|
| op 类/访问器/builder/verifier 生成 | `mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp` | 535、2197、2976、3806 |
| `verify()` 与 `verifyInvariantsImpl()` 的组合 | 同上 | 3839 |
| 格式 parser/printer 生成 | `mlir/tools/mlir-tblgen/OpFormatGen.cpp` | 1849、3402 |
| `Pure` 的展开 | `mlir/include/mlir/Interfaces/SideEffectInterfaces.td` | 147 |
| 格式测试 op | `mlir/test/lib/Dialect/Test/TestOpsSyntax.td` | 189、203、231 |
| 本练习输入/产物 | `/tmp/mlir-practice-c/v1`、`/tmp/mlir-practice-c/v2` | `Mini.td`、`decls.h.inc`、`defs.cpp.inc` |

## C.12 踩雷集锦

1. **只生成 decls 不生成 defs。** → decls 里的 `verify()`/`parse()` 只有声明；不生成 defs 就看不到实现侧的调用链。
2. **忘了 `-I $S/mlir/include`。** → `include "mlir/IR/OpBase.td"` 找不到；错误是 “Could not find include file”，不是 `.td` 语法错。
3. **把 `hasVerifier = 1` 当成“verifier 已实现”。** → 它只生成声明和调用；你仍然要手写 `LogicalResult Mini_AddOp::verify()`，否则链接失败。
4. **在 `assemblyFormat` 里加 `type($lhs)` 却不改 parser 期望的输入。** → 生成器会生成新 parser；旧测试输入少一个类型，直接 parse 失败。
5. **用 `Optional<I32>` 做 optional group 的 anchor 却没加 `^`。** → 生成器报 “only variable length operands can be used within an optional group” 或组永远不进。
6. **改输入文件名来做版本对比。** → 内部符号名跟着变，diff 里全是噪声；用同名的 `v1/Mini.td`、`v2/Mini.td`。
7. **把生成物提交进源码树。** → `.inc` 会在构建时被覆盖；`.td` 才是源。
8. **在本练习里期待“能跑 mlir-opt”。** → 没有 dialect 注册和 C++ 实现；本练习只验证生成阶段。

## C.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp`**（4,856 行）
  - 读哪里：2197–2360 的 named getter；2976–3100 的 builder；3806–3900 的 verifier。
  - 能学到什么：本练习 diff 的每一行由哪个函数拼接。
  - 和本章关联：C.7 的 `verify()`/`SubOp` 生成逻辑都在这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/tools/mlir-tblgen/OpFormatGen.cpp`**（4,788 行）
  - 读哪里：1849–1900 的 `genParser`；3402–3480 的 `genPrinter`。
  - 能学到什么：`type($lhs)` 如何变成 `lhsRawType` 与 printer 分支。
  - 和本章关联：C.7.2 的 parser/printer diff 来自这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DefiningDialects/Operations.md`**
  - 读哪里：450–735 的 builder/verifier/assembly format。
  - 能学到什么：`hasVerifier`、`assemblyFormat`、optional group 的规范语义。
  - 和本章关联：C.6 的三处改动都对应这一节。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Test/TestOpsSyntax.td`**（868 行）
  - 读哪里：189–320 的格式测试 op。
  - 能学到什么：官方如何为每个格式特性写最小定义。
  - 和本章关联：C.8 的四个错误在真实测试里都有正例。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/mlir-tblgen/op-format.mlir`**
  - 读哪里：每个输入 op 与对应 `CHECK`。
  - 能学到什么：声明式格式的期望输出，可作为你后续扩展示例。
  - 和本章关联：把 `mini` 接进真实 dialect 后，测试写法可照此模仿。

练习到此结束。下一章进入 Part 4：从 ODS 世界转到 Conversion，先看 target legality 与 `DialectConversion` 的失败模式。

→ 下一章：[Ch 19 Conversion 与 target legality](./19-conversion-target-legality.md)
