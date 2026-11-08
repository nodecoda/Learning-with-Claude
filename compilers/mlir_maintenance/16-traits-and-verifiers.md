# Ch 16 — Traits、Interfaces 与 Verifier：不变量守门人

> **目标**：搞清 MLIR 的验证流水线：结构 trait、ODS 生成的 `verifyInvariantsImpl()`、普通 trait、自定义 `verify()`、region verifier 各在什么顺序执行；能根据一条诊断判断该改 trait、interface 还是 verifier。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（只读），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM/MLIR `24.0.0git`，assertions ON。

> **前置**：Ch 07 的 verifier 概念、Ch 14 的 ODS 生成物；知道 `Operation::emitOpError` 的用法。

## 16.1 为什么维护者要区分 trait、interface 与 verifier

MLIR 把“一个 op 必须满足什么”拆成三个层次：

1. **结构 trait**：`ZeroRegions`、`OneResult`、`NOperands<2>`。它们由 ODS 根据 `arguments`/`results`/`regions` 自动加上，验证的是“形状”。
2. **语义 trait / interface**：`SameOperandsAndResultType`、`Pure`、`InferTypeOpInterface`。它们表达“这个 op 有什么性质/能力”，可能带 `verifyTrait`，也可能只提供方法。
3. **自定义 verifier**：`let hasVerifier = 1;` 后手写的 `LogicalResult verify()`。它验证“这个 op 自己的业务约束”。

三者的执行顺序是**规范的一部分**，不是实现细节。顺序错了会出现两类 bug：

- 诊断指向错误的位置。例如 operand 数量不对时，先报“类型不匹配”，让你以为是类型问题。
- verifier 访问了还没验证的 IR。例如 `verify()` 里读 region 内的 op，但 region 还没验证，遇到非法 op 会崩。

维护者最常做的判断是：**这条约束应该写成 ODS 约束、trait，还是自定义 verifier？** 本章给出可操作的判据。

## 16.2 先建立直觉：验证流水线

```text
  Operation
     │
     ├─ 1. StructuralOpTrait（ZeroRegions / OneResult / NOperands<N>）
     │      只依赖 operand/result/region 数量，可独立跑
     ▼
  verifyInvariantsImpl()        ← ODS 生成
     │   a. properties 约束
     │   b. attribute 约束
     │   c. operand 类型/数量
     │   d. result 类型/数量
     │   e. PredTrait（如 `IsInteger` 之类的 predicate）
     │   f. region 数量/类型
     │   g. successor 数量/类型
     ▼
  其它 trait 的 verifyTrait()   ← SameOperandsAndResultType、Pure 等
     │
     ▼
  自定义 verify()                ← hasVerifier = 1
     │
     ├─（若 op 有 region）
     │   2. region 内所有 op 先验证
     ▼
  region trait 的 verifyRegionTrait()
     │
     ▼
  自定义 verifyRegions()         ← hasRegionVerifier = 1
```

两个阶段的分界是“是否需要访问 region 内部”：

- **第一阶段**（`verifyInvariants`）不进入 region，只看 op 自身的 operands/results/attrs/properties。
- **第二阶段**（`verifyRegionInvariants`）在 region 内所有 op 验证完之后跑，只有 `verifyRegionTrait` 和 `hasRegionVerifier` 参与。

`mlir/docs/DefiningDialects/Operations.md:703` 把这条顺序写成规范；`OpDefinition.h:2130` 的实现是“先 `verifyTraits`，再 `verify()`”。

## 16.3 Trait 是什么：CRTP 的静态混入

所有 op trait 都从 `OpTrait::TraitBase` 派生（`OpDefinition.h:381`）：

```cpp
template <typename ConcreteType, template <typename> class TraitType>
class TraitBase {
public:
  /// Return the derived trait class.
  using TraitTypeT = TraitType<ConcreteType>;
  ...
};
```

具体 trait 用 CRTP 拿到 `ConcreteType`：

```cpp
template <typename ConcreteType>
class SameOperandsAndResultType
    : public TraitBase<ConcreteType, SameOperandsAndResultType> {
public:
  static LogicalResult verifyTrait(Operation *op) {
    return impl::verifySameOperandsAndResultType(op);
  }
};
```

（`OpDefinition.h:1114`。）

三个维护要点：

1. **trait 是静态的**。它编译进 op 类，不能运行时增删；`Op<..., TraitA, TraitB>` 的模板参数就是全部 trait。
2. **`verifyTrait` 是静态函数**，不依赖实例状态；它拿 `Operation *`，自己做 cast。
3. **trait 可以只提供方法，不做验证**。例如 `Pure` 主要表达“无副作用”，它的验证由 `MemoryEffectOpInterface` 等机制承担。

### 16.3.1 结构 trait 由 ODS 自动加

`AddIOp` 的生成类里有：

```cpp
::mlir::OpTrait::ZeroRegions,
::mlir::OpTrait::OneResult,
::mlir::OpTrait::OneTypedResult<::mlir::Type>::Impl,
::mlir::OpTrait::ZeroSuccessors,
::mlir::OpTrait::NOperands<2>::Impl,
```

这些不是你在 `.td` 里手写的，而是 ODS 从 `arguments = (ins I32:$lhs, I32:$rhs)` 和 `results = (outs I32:$result)` 推出来的。维护含义：**改 operand 数量会自动改结构 trait，也会自动改 verifier 的第一层检查**；你不需要（也不应该）再写一个“检查有两个 operand”的自定义 verifier。

## 16.4 Interface 是什么：可扩展的能力契约

Trait 与 interface 都能“给 op 加能力”，但机制不同：

| 维度 | Trait | Interface |
|---|---|---|
| 绑定时机 | 编译期，写进 `Op<>` 模板参数 | 编译期声明 + 运行期查表 |
| 典型声明 | `[SameOperandsAndResultType]` | `DeclareOpInterfaceMethods<InferTypeOpInterface>` |
| 能否外部附加 | 不能 | 能（external model / dialect extension） |
| 是否有 vtable | 通常无 | 有 interface map |
| 适合表达 | “这个 op 天生是什么” | “这个 op 能做什么，别人可以扩展” |
| 验证钩子 | `verifyTrait` / `verifyRegionTrait` | `verifyWithRegions` 等 |

判据：

- 如果性质由 op 的定义唯一决定，且不需要第三方扩展，用 **trait**。
- 如果能力需要跨方言复用、允许外部 model 附加，用 **interface**。
- 如果只是“这个 op 自己的业务规则”，用 **自定义 verifier**，不要硬塞进 trait。

一个常见误用是把“必须有两个 operand 且类型相同”写成自定义 verifier。正确做法是 ODS 约束 + `SameOperandsAndResultType`；自定义 verifier 只处理 ODS 表达不了的语义（例如“symbol 必须存在”“region 必须以某个 terminator 结尾”）。

## 16.5 ODS 生成的 verifier 到底查什么

`OpDefinitionsGen.cpp:3806` 的 `genVerifier()` 按固定顺序拼接：

```text
genPropertyVerifier
genAttributeVerifier
genOperandResultVerifier(operands)
genOperandResultVerifier(results)
PredTrait 循环
genRegionVerifier
genSuccessorVerifier
return success()
```

然后 `genVerifier()` 生成两个方法：

```cpp
// verifyInvariantsImpl()：上面那串检查
// verifyInvariants()：
//   hasVerifier ? (verifyInvariantsImpl() && verify()) : verifyInvariantsImpl()
```

这解释了两个现象：

1. **类型约束错误先于 trait 错误**。`test.loc_dst` 的 result 是 `i64` 而约束是 `I32` 时，报的是“result #0 must be 32-bit signless integer”，而不是 `SameOperandsAndResultType` 的“same type”。因为 operand/result 约束在 trait 之前。
2. **自定义 `verify()` 能看到已通过 ODS 约束的 IR**。你不需要在 `verify()` 里重复检查 operand 数量或类型；它们已经被 `verifyInvariantsImpl()` 保证。

`genCustomVerifier()`（3848 行）只负责声明 `verify()` / `verifyRegions()`，不生成实现；实现由方言作者提供。

## 16.6 实测：四类验证错误

### 16.6.1 结构 trait：operand 数量

```mlir
func.func @f(%a: i32) -> i32 {
  %0 = "arith.addi"(%a) : (i32) -> i32
  return %0 : i32
}
```

实测：

```text
/tmp/verify4.mlir:2:8: error: 'arith.addi' op expected 2 operands, but found 1
  %0 = "arith.addi"(%a) : (i32) -> i32
       ^
/tmp/verify4.mlir:2:8: note: see current operation: %0 = "arith.addi"(%arg0) <{overflowFlags = #arith.overflow<none>}> : (i32) -> i32
```

这是 `NOperands<2>` 的结构检查，发生在 ODS 约束之前。

### 16.6.2 ODS 约束：result 类型

```mlir
func.func @f(%a: i32) -> i64 {
  %0 = "test.loc_dst"(%a) : (i32) -> i64
  return %0 : i64
}
```

实测：

```text
/tmp/trait1.mlir:2:8: error: 'test.loc_dst' op result #0 must be 32-bit signless integer, but got 'i64'
  %0 = "test.loc_dst"(%a) : (i32) -> i64
       ^
```

`test.loc_dst` 同时有 `SameOperandsAndResultType`，但先报的是 ODS 的 result 约束——这就是 16.5 的顺序。

### 16.6.3 语义 trait：`SameOperandsAndResultType`

换成 operand/result 都是 `AnyType` 的 `test.same_operand_result_type`：

```mlir
func.func @f(%a: tensor<2xi32>) -> tensor<2xf32> {
  %0 = "test.same_operand_result_type"(%a) : (tensor<2xi32>) -> tensor<2xf32>
  return %0 : tensor<2xf32>
}
```

实测：

```text
/tmp/trait5.mlir:2:8: error: 'test.same_operand_result_type' op requires the same type for all operands and results
  %0 = "test.same_operand_result_type"(%a) : (tensor<2xi32>) -> tensor<2xf32>
       ^
```

实现在 `Operation.cpp:1114` 的 `verifySameOperandsAndResultType`：先比 element type，再用 `verifyCompatibleShape` 比 shape；encoding 也要一致。

### 16.6.4 结构 trait：region 数量

```mlir
"test.format_region_a_op"() ({
  "test.op"() : () -> ()
}, {
  "test.op"() : () -> ()
}) : () -> ()
```

实测：

```text
/tmp/trait2.mlir:2:3: error: 'test.format_region_a_op' op requires one region
  "test.format_region_a_op"() ({
  ^
```

`format_region_a_op` 的 ODS 写的是 `regions = (region AnyRegion:$region)`，生成 `OneRegion`；给两个 region 直接触发结构检查。

### 16.6.5 自定义 verifier

```mlir
func.func @f() {
  %0 = "test.result_type_with_trait"() : () -> i32
  return
}
```

实测：

```text
/tmp/verify5.mlir:2:8: error: result type should have trait 'TestTypeTrait'
  %0 = "test.result_type_with_trait"() : () -> i32
       ^
/tmp/verify5.mlir:2:8: note: see current operation: %0 = "test.result_type_with_trait"() : () -> i32
```

实现只有三行（`TestOpDefs.cpp:720`）：

```cpp
LogicalResult ResultTypeWithTraitOp::verify() {
  if ((*this)->getResultTypes()[0].hasTrait<TypeTrait::TestTypeTrait>())
    return success();
  return emitError("result type should have trait 'TestTypeTrait'");
}
```

注意它用的是 `emitError` 而不是 `emitOpError`：前者只打印消息，后者会自动带 `'op' op` 前缀。两者都能工作，但诊断风格不同。

## 16.7 `--verify-diagnostics`：把错误变成测试

`mlir-opt --verify-diagnostics` 会收集 diagnostic，并与文件里的 `expected-error` / `expected-note` 匹配。匹配失败会报 `unexpected error` / `expected error ... was not produced`。

一个通过的负控：

```mlir
func.func @f(%a: tensor<2xi32>) -> tensor<2xf32> {
  // expected-error @+1 {{requires the same type for all operands and results}}
  %0 = "test.same_operand_result_type"(%a) : (tensor<2xi32>) -> tensor<2xf32>
  return %0 : tensor<2xf32>
}
```

实测：

```text
exit=0
```

（`--verify-diagnostics` 成功时不打印任何东西。）

一个失败的反例——漏写 `expected-note`：

```mlir
func.func @f(%a: i32, %b: f32) -> i32 {
  // expected-error @+1 {{use of value '%b' expects different type than prior uses: 'i32' vs 'f32'}}
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
```

实测：

```text
/tmp/verify2.mlir:1:23: error: unexpected note: prior use here
func.func @f(%a: i32, %b: f32) -> i32 {
                      ^
```

parser 的“prior use here”是一条 note，必须显式写 `// expected-note @+1 {{prior use here}}` 才会被接受。**负控测试不是只写 expected-error**；带 note 的诊断要成对声明。

## 16.8 源码落点

| 内容 | 路径 | 关键行/符号（本树 `grep -n` 核对） |
|---|---|---|
| `TraitBase` | `mlir/include/mlir/IR/OpDefinition.h` | 381 |
| `OpInvariants` 注释 | 同上 | 435（“run after core traits and before user defined traits”） |
| `SameOperandsAndResultType` | 同上 | 1114 |
| `verifyTraits` 折叠实现 | 同上 | 1685 |
| `verifyInvariants`（traits + verify） | 同上 | 2130 |
| 结构 trait 类 | 同上 | 482 `NOperands`；527 `ZeroRegions`；620 `ZeroResults`；798 `ZeroSuccessors` |
| `verifySameOperandsAndResultType` | `mlir/lib/IR/Operation.cpp` | 1114 |
| 顶层 verifier 调用 | `mlir/lib/IR/Verifier.cpp` | 315 `registeredInfo->verifyInvariants(&op)` |
| ODS verifier 生成 | `mlir/tools/mlir-tblgen/OpDefinitionsGen.cpp` | 3806 `genVerifier`；3848 `genCustomVerifier` |
| 验证顺序规范 | `mlir/docs/DefiningDialects/Operations.md` | 684 自定义 verifier；703 Verification Ordering |
| trait 编写指南 | `mlir/docs/Traits/_index.md` | 1–80 定义 trait；`verifyTrait`/`verifyRegionTrait` |
| 测试 op | `mlir/test/lib/Dialect/Test/TestOps.td` | 236 `SameOperandsResultType`；1367 `ResultTypeWithTraitOp` |
| 自定义 verifier 实现 | `mlir/test/lib/Dialect/Test/TestOpDefs.cpp` | 720 `ResultTypeWithTraitOp::verify` |

## 16.9 可跑实验

> 全部真跑；`$T`/`$S` 见 14.4。

### 实验 1：结构 trait 先报错（实测）

```bash
cat > /tmp/verify4.mlir <<'EOF'
func.func @f(%a: i32) -> i32 {
  %0 = "arith.addi"(%a) : (i32) -> i32
  return %0 : i32
}
EOF
$T/bin/mlir-opt /tmp/verify4.mlir
```

实测：

```text
/tmp/verify4.mlir:2:8: error: 'arith.addi' op expected 2 operands, but found 1
  %0 = "arith.addi"(%a) : (i32) -> i32
       ^
```

### 实验 2：ODS 约束先于 trait（实测）

```bash
cat > /tmp/trait1.mlir <<'EOF'
func.func @f(%a: i32) -> i64 {
  %0 = "test.loc_dst"(%a) : (i32) -> i64
  return %0 : i64
}
EOF
$T/bin/mlir-opt /tmp/trait1.mlir
```

实测：

```text
/tmp/trait1.mlir:2:8: error: 'test.loc_dst' op result #0 must be 32-bit signless integer, but got 'i64'
```

### 实验 3：语义 trait 的错误（实测）

```bash
cat > /tmp/trait5.mlir <<'EOF'
func.func @f(%a: tensor<2xi32>) -> tensor<2xf32> {
  %0 = "test.same_operand_result_type"(%a) : (tensor<2xi32>) -> tensor<2xf32>
  return %0 : tensor<2xf32>
}
EOF
$T/bin/mlir-opt /tmp/trait5.mlir
```

实测：

```text
/tmp/trait5.mlir:2:8: error: 'test.same_operand_result_type' op requires the same type for all operands and results
```

### 实验 4：region 数量（实测）

```bash
cat > /tmp/trait2.mlir <<'EOF'
func.func @f() {
  "test.format_region_a_op"() ({
    "test.op"() : () -> ()
  }, {
    "test.op"() : () -> ()
  }) : () -> ()
  return
}
EOF
$T/bin/mlir-opt /tmp/trait2.mlir
```

实测：

```text
/tmp/trait2.mlir:2:3: error: 'test.format_region_a_op' op requires one region
```

### 实验 5：自定义 verifier（实测）

```bash
cat > /tmp/verify5.mlir <<'EOF'
func.func @f() {
  %0 = "test.result_type_with_trait"() : () -> i32
  return
}
EOF
$T/bin/mlir-opt /tmp/verify5.mlir
```

实测：

```text
/tmp/verify5.mlir:2:8: error: result type should have trait 'TestTypeTrait'
```

### 实验 6：`--verify-diagnostics` 通过与漏 note（实测）

```bash
cat > /tmp/trait5v.mlir <<'EOF'
func.func @f(%a: tensor<2xi32>) -> tensor<2xf32> {
  // expected-error @+1 {{requires the same type for all operands and results}}
  %0 = "test.same_operand_result_type"(%a) : (tensor<2xi32>) -> tensor<2xf32>
  return %0 : tensor<2xf32>
}
EOF
cat > /tmp/verify2.mlir <<'EOF'
func.func @f(%a: i32, %b: f32) -> i32 {
  // expected-error @+1 {{use of value '%b' expects different type than prior uses: 'i32' vs 'f32'}}
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
EOF
$T/bin/mlir-opt --verify-diagnostics /tmp/trait5v.mlir; echo exit=$?
$T/bin/mlir-opt --verify-diagnostics /tmp/verify2.mlir; echo exit=$?
```

实测：

```text
exit=0
/tmp/verify2.mlir:1:23: error: unexpected note: prior use here
func.func @f(%a: i32, %b: f32) -> i32 {
                      ^
exit=1
```

## 16.10 踩雷集锦

1. **把 trait 当 interface 用。** → trait 不能外部附加，也不能运行时查表；需要扩展能力时用 interface + external model。
2. **以为 verifier 顺序可以随便调。** → 结构 trait → ODS 约束 → 普通 trait → 自定义 `verify()` 是规范；调换会让诊断指向错误层。
3. **在 `verify()` 里重复检查 operand 数量。** → ODS 已经做了；重复检查只会让诊断重复。
4. **在 `verify()` 里读 region 内的 op。** → 应该用 `hasRegionVerifier` / `verifyRegions()`；否则可能读到未验证的 IR。
5. **用 `emitError` 和 `emitOpError` 混着写。** → 前者无 op 前缀，后者有；测试里的 `expected-error` 要匹配完整消息。
6. **负控测试只写 `expected-error`。** → 带 note 的诊断会被报 `unexpected note`；要成对写 `expected-note`。
7. **把业务规则塞进 trait。** → trait 是全局、可复用的性质；方言私有规则应放自定义 verifier。
8. **改了 ODS 约束却只跑正向测试。** → 约束是负控测试的契约；正向用例通过不代表非法输入被拒绝。

## 16.11 本章重点整理

- 验证分两阶段：`verifyInvariants`（op 自身）与 `verifyRegionInvariants`（region 内 op 验证后）。
- 第一阶段顺序：结构 trait → ODS `verifyInvariantsImpl()` → 普通 trait → 自定义 `verify()`。
- `verifyInvariantsImpl()` 内部顺序：properties → attributes → operands → results → pred traits → regions → successors。
- trait 是编译期 CRTP 混入；interface 是运行期能力查表，可被 external model/dialect extension 附加。
- 自定义 verifier 只应处理 ODS 表达不了的业务规则；不要重复 ODS 已做的检查。
- `--verify-diagnostics` 是负控测试的核心工具；带 note 的诊断必须同时声明 `expected-note`。
- 诊断消息是测试契约的一部分；改 verifier 消息要同步改所有引用它的测试。

## 16.12 自我检核

- [ ] 能画出两阶段验证流水线，并说出每阶段的输入
- [ ] 能解释为什么 `test.loc_dst` 先报 ODS 类型约束而不是 `SameOperandsAndResultType`
- [ ] 能说出 trait 与 interface 的至少三点区别
- [ ] 能判断一条新约束应该写成 ODS 约束、trait 还是自定义 verifier
- [ ] 能写出一个带 `expected-note` 的 `--verify-diagnostics` 用例
- [ ] 能解释 `hasVerifier` 与 `hasRegionVerifier` 的适用边界
- [ ] 能定位 `verifyInvariantsImpl()` 的生成代码并读出检查顺序
- [ ] 知道 `emitError` 与 `emitOpError` 在诊断文本上的差异

## 16.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/OpDefinition.h`**
  - 读哪里：381–450 的 `TraitBase`/`OpInvariants`；1100–1130 的 `SameOperandsAndResultType`；1680–1710 的 `verifyTraits`；2120–2150 的 `verifyInvariants`。
  - 能学到什么：trait 的 CRTP 结构、验证顺序的实现依据。
  - 和本章关联：16.3/16.5 的所有结论都能在这里核对。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Traits/_index.md`**（383 行）
  - 读哪里：1–80 的 trait 定义与 `verifyTrait`；120–200 的 parametric traits。
  - 能学到什么：官方推荐的 trait 写法，以及何时该用 out-of-line 实现。
  - 和本章关联：16.3 的代码模板来自这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DefiningDialects/Operations.md`**
  - 读哪里：684–735 的自定义 verifier 与 Verification Ordering。
  - 能学到什么：规范层面的执行顺序，以及诊断 severity 的建议。
  - 和本章关联：16.2 的流水线就是这一节的实现版。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Verifier.cpp`**（569 行）
  - 读哪里：280–360 的 `verifyOperation`；315 的 `verifyInvariants` 调用。
  - 能学到什么：顶层 verifier 如何遍历 region、如何调用 registered op 的钩子。
  - 和本章关联：解释“region 内 op 先验证”的实现。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Test/TestOps.td`**
  - 读哪里：236 的 `SameOperandsResultType`；1367 的 `ResultTypeWithTraitOp`。
  - 能学到什么：官方如何为 trait/verifier 写最小测试 op。
  - 和本章关联：16.6 的实验输入都来自这些定义。

下一章进入 Type/Attribute 的世界：storage、uniquing、自定义 parser，以及为什么 `Type` 的生命周期比你想的长。

→ 下一章：[Ch 17 自定义 Type 与 Attribute](./17-custom-types-attributes.md)
