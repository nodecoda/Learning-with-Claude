# Ch 12 — Dialect interfaces 与 external models

> **目标**：把 interface 读成**运行时的 vtable 查表**：知道 `Concept`/`Model`/`FallbackModel`/`ExternalModel` 四层怎么分派、`attachInterface` 做了什么、为什么“在我这边测没问题、合进去就 abort”，以及给已有 op 补接口的正确姿势。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，assertions ON（promised-interface 的 fatal error 只在 debug 生效的部分可观测）。
>
> **前置**：Ch 04（Operation/RegisteredOperationName）、Ch 09（pass 与 dependent dialect）。

---

## 12.1 为什么维护者要读 interface

MLIR 的核心算法（canonicalize、CSE、bufferization、tiling、inlining、conversion）都**不能** `dyn_cast` 具体 op——它们要跨方言工作。于是所有“泛化行为”都压在 interface 上：

```text
算法（不知道 arith/linalg/你的方言）
     │
     ├── 想知道“这个 op 有没有副作用”  → MemoryEffectOpInterface
     ├── 想知道“这个 op 的循环边界”    → LoopLikeOpInterface
     ├── 想知道“这个值上界是多少”      → ValueBoundsOpInterface
     └── 想知道“这个 op 的 DLTI 键值”  → DLTIQueryOpInterface
```

维护者在这里踩的坑有明确的三类：

1. **接口没注册，算法静默跳过**。`dyn_cast<SomeInterface>(op)` 返回空，算法走“保守路径”，不报错。你以为是分析精度问题，其实是注册漏了。
2. **注册了但生命周期错**。`ExternalModel` 只在**注册它的 context** 里有效；`attachInterface` 在 `Dialect::initialize` 里调用，早于任何 pass。放到别处就是 use-after-free 或“只在某些测试里生效”。
3. **接口被 promised 但没实现**。`declarePromisedInterface` 会在有人查询时 `report_fatal_error`（`Dialect.h:228-240`）——注意是 **fatal**，不是 diagnostic，进程直接死。

本章主线的判据只有一句：**接口是“op 类型 → concept 指针”的查表，查不到就退化，退化要能被算法察觉。**

## 12.2 先建立直觉：一次接口查询的四层

```text
   auto iface = dyn_cast<LoopLikeOpInterface>(op);
        │
        ▼
  Interface<...>::classof(op)
        │  ConcreteType::getInterfaceFor(op)
        ▼
  RegisteredOperationName::getInterface<T>()            [OperationSupport.h:396]
        │
        ▼
  InterfaceMap::lookup(TypeID)                          [InterfaceSupport.h:202]
        │
        ├── 命中 Model<ConcreteOp>       ← op 直接实现（trait/ODS）
        ├── 命中 ExternalModel<Model, T> ← 事后给已有 op/type 挂的模型
        ├── 命中 FallbackModel<T>        ← 方言级兜底（per-op 默认）
        └── 未命中 → nullptr → dyn_cast 返回空 → 算法走保守路径
```

四个概念对应四段源码：

| 概念 | 生成/定义位置 | 作用 |
|---|---|---|
| `Concept` | tblgen 生成（`-gen-op-interface-decls`） | 函数指针表，等价于 vtable |
| `Model<ConcreteOp>` | tblgen 生成 | 把 `Concept` 的槽指向 `ConcreteOp` 的静态方法 |
| `FallbackModel` | tblgen 生成（`InterfaceSupport.h:79`） | 方言提供的默认实现 |
| `ExternalModel<Model, T>` | `InterfaceSupport.h:83` | 把 `T` 的模型挂到**不属于 T 的** op 上 |

`ExternalModel` 的定义只有一行别名，真正的“外来”语义来自 `attachInterface` 的调用点：

```cpp
// InterfaceSupport.h:83
  template <typename T, typename U>
  using ExternalModel = typename Traits::template ExternalModel<T, U>;
```

## 12.3 三类接口：op / type-attr / dialect

| 类别 | 例子 | 查询对象 | 典型维护动作 |
|---|---|---|---|
| Op interface | `LoopLikeOpInterface`、`MemoryEffectOpInterface`、`TilingInterface` | `Operation *` | 给新 op 加 `let extraClassDeclaration` 或 ODS 声明 |
| Type/Attr interface | `DataLayoutTypeInterface`、`DLTIQueryInterface`（attr） | `Type` / `Attribute` | 补 `ExternalModel`，因为 type 往往不能改 |
| Dialect interface | `DialectInlinerInterface`、`BufferizableOpInterface` 的 dialect 侧 | `Dialect *` | `addInterface<...>()` 到自己的 dialect |

第三类和前两类容易混：`DialectInterface` 是**每个方言一份**，用 `DialectInterfaceCollection` 批量收集：

```cpp
// Dialect.cpp:121-133
DialectInterfaceCollectionBase::DialectInterfaceCollectionBase(
    MLIRContext *ctx, TypeID interfaceKind, StringRef interfaceName) {
  for (auto *dialect : ctx->getLoadedDialects()) {
    ...
    if (auto *interface = dialect->getRegisteredInterface(interfaceKind)) {
```

维护含义：**collection 是在构造时快照的**。你在 pass 运行中才加载一个新方言，已有 collection 看不到它。这是“bufferization 在某个测试里少处理一个方言”的经典原因。

## 12.4 四种提供实现的方式

### 方式 1：op 直接实现（最常见）

ODS 里 `let interface = [LoopLikeOpInterface]`，或 C++ 里继承 `Op<..., LoopLikeOpInterface::Trait>`。tblgen 会生成 `Model<YourOp>` 并塞进 `InterfaceMap`。

### 方式 2：`FallbackModel`（方言级默认实现）

当多数 op 行为一致、少数要特化时，用方言 fallback：接口方法在找不到 op 级 Model 时退到方言级实现（`Interfaces.md:414` 的 “Dialect Fallback for OpInterface”）。纯虚方法必须给默认实现，否则编译失败。

### 方式 3：`ExternalModel`（给别人的 op/type 挂接口）

经典例子在本树：

```cpp
// mlir/test/lib/Dialect/Test/TestTypes.cpp:411-423
struct PtrElementModel
    : public LLVM::PointerElementTypeInterface::ExternalModel<PtrElementModel,
                                                              SimpleAType> {};
...
void TestDialect::registerTypes() {
  ...
  SimpleAType::attachInterface<PtrElementModel>(*getContext());
```

注意三点：

- `SimpleAType` **没有**改类定义，接口是事后挂的。
- 挂在 `Dialect::initialize`（`registerTypes` 由它调用），所以 context 一加载 TestDialect 就有。
- 模型类型必须是 `ExternalModel<Model, ConcreteT>`，编译期会检查 `Model` 是否真的实现了 `Concept` 的所有槽。

### 方式 4：`DialectInterface`（方言级钩子）

`addInterface<MyDialectInterface>()`（`Dialect.h:200-204`）。适合“整个方言一个策略”的场景，如 inliner、bufferization 的 dialect 策略。

## 12.5 注册路径与生命周期

`Dialect::addInterface`（`Dialect.cpp:101-113`）：

```cpp
void Dialect::addInterface(std::unique_ptr<DialectInterface> interface) {
  // Handle the case where the models resolve a promised interface.
  handleAdditionOfUndefinedPromisedInterface(getTypeID(), interface->getID());

  auto it = registeredInterfaces.try_emplace(interface->getID(),
                                             std::move(interface));
  if (!it.second)
    LDBG() << "repeated interface registration for dialect " << getNamespace();
}
```

两个维护点：

1. **重复注册不报错**，只打一条 debug 日志。所以“我加了两次”在 release 下完全静默，第二次的实例被丢弃。
2. **`handleAdditionOfUndefinedPromisedInterface`** 在注册时解除 promise。promise 是“我说我会实现，先别 abort”的欠条。

promise 的失败是 fatal：

```cpp
// Dialect.h:228-240（节选）
  void handleUseOfUndefinedPromisedInterface(TypeID interfaceRequestorID,
                                             TypeID interfaceID,
                                             StringRef interfaceName = "") {
    if (unresolvedPromisedInterfaces.count(
            {interfaceRequestorID, interfaceID})) {
      llvm::report_fatal_error(
          "checking for an interface (`" + interfaceName +
          "`) that was promised by dialect '" + getNamespace() +
```

维护含义：`declarePromisedInterface<FooOpInterface, MyOp>()` 的唯一合法用途是“跨库依赖，模型在另一个 TU 注册”。它把“返回空”变成“进程中止”，所以**只在确定有人会补上时**用。

`attachInterface` 侧（`OperationSupport.h:400-411`）：

```cpp
  /// Attach the given models as implementations of the corresponding
  /// interfaces for the concrete operation.
  template <typename... Models>
  void attachInterface() {
    // Handle the case where the models resolve a promised interface.
    (dialect_extension_detail::handleAdditionOfUndefinedPromisedInterface(
         *getDialect(), getTypeID(), Models::Interface::getInterfaceID()),
     ...);

    getImpl()->getInterfaceMap().insertModels<Models...>();
  }
```

它改的是 `RegisteredOperationName` 的 `interfaceMap`——**全局的、按 op 类型**，不是按 op 实例。所以 `attachInterface` 只能调一次（在 initialize 里），不能对单个 op 做。

## 12.6 默认实现：以 `DLTIQueryOpInterface` 为标本

本树有一个非常适合读的接口：`DLTIQueryOpInterface`（`DLTIOpInterfaces.td:14-57`）。它的两个方法都带 `defaultImplementation`：

```tablegen
    InterfaceMethod<
      /*description=*/"Returns the DLTI value associated with the key.",
      /*retTy=*/"::mlir::FailureOr<::mlir::Attribute>",
      /*methodName=*/"queryDlti",
      /*args=*/(ins "::mlir::DataLayoutEntryKey":$key),
      /*methodBody=*/"",
      /*defaultImplementation=*/[{
        return ::mlir::dlti::detail::queryDlti($_op.getOperation(), key);
      }]
    >
```

维护含义，三条：

1. **有默认实现的接口，op 可以“零成本”声明支持**。加 `DLTIQueryOpInterface` 到 ODS 列表即可工作，因为默认实现读 `dlti` 属性。
2. **默认实现是“约定”而不是“约束”**。测试 op 可以覆盖它，做出 `update`/`clear`/`fail`/`invalid` 四种行为（`TestDataLayoutQuery.cpp:130-170`）。
3. **失败要能表达**。`setDlti` 返回 `LogicalResult`；测试里对不可变表示调用会 `failed(...)`，然后 op 被打上 `test.set_dlti_failed`（`:162`），供 FileCheck 断言。

### 12.6.1 给已有 op 补接口的三步

维护里最常见的需求不是“新 op 实现接口”，而是“老 op 没实现，但下游算法需要”。安全顺序是：

1. **先查它到底有没有**。`dyn_cast<X>(op)` 返回空 ≠ 你的 `mlir-opt` 有问题；用 `--show-dialects` 确认方言已注册，再读该 dialect 的 `initialize`。
2. **能改 op 定义就改 op**。在 ODS 的 `let interface = [...]` 里加，重新生成；这是编译期检查最完整的路径。
3. **不能改就挂 `ExternalModel`**，并且**只在 `Dialect::initialize` 里 `attachInterface`**。挂载点必须是“该 op 所属方言已初始化”之后、任何 pass 之前。

第三步的典型错误是把 `attachInterface` 放在某个 pass 的 `runOnOperation` 里：第一次跑有效，第二次跑会重复注册（静默），而换一个入口（比如别的工具）就完全没挂上。

## 12.7 源码落点

| 位置 | 内容 | 维护相关性 |
|---|---|---|
| `mlir/include/mlir/Support/InterfaceSupport.h:79/:83` | `FallbackModel` / `ExternalModel` 别名 | 四种实现方式的类型基础 |
| `mlir/include/mlir/Support/InterfaceSupport.h:147/:202/:211` | `InterfaceMap` / `lookup` / `insertModels` | 查表与注册的真实容器 |
| `mlir/include/mlir/IR/OperationSupport.h:396` | `getInterface<T>()` | `dyn_cast` 的底层 |
| `mlir/include/mlir/IR/OperationSupport.h:405` | `attachInterface<Models...>` | 给 op 类型挂模型 |
| `mlir/lib/IR/Dialect.cpp:101` | `Dialect::addInterface` | 方言接口注册、promise 解除 |
| `mlir/lib/IR/Dialect.cpp:121-141` | `DialectInterfaceCollectionBase` | 构造时快照，`getInterfaceFor(op)` |
| `mlir/include/mlir/IR/Dialect.h:158/:167` | `getRegisteredInterface` | 按 `TypeID` 取方言接口 |
| `mlir/include/mlir/IR/Dialect.h:211/:228` | `declarePromisedInterface` / fatal | 欠条与 abort |
| `mlir/include/mlir/Dialect/DLTI/DLTIOpInterfaces.td:14-57` | `DLTIQueryOpInterface` 与默认实现 | 最佳阅读样本 |
| `mlir/test/lib/Dialect/DLTI/TestDataLayoutQuery.cpp:117-170` | 测试 pass 与四种 action | 接口行为的可断言化 |
| `mlir/test/lib/Dialect/Test/TestTypes.cpp:411-423` | `PtrElementModel` + `attachInterface` | ExternalModel 的真实用法 |

核对命令：

```bash
grep -n 'using ExternalModel\|using FallbackModel' mlir/include/mlir/Support/InterfaceSupport.h
grep -n 'attachInterface\|getInterface()' mlir/include/mlir/IR/OperationSupport.h | head
grep -n 'declarePromisedInterface\|handleUseOfUndefinedPromisedInterface' mlir/include/mlir/IR/Dialect.h
grep -n 'attachInterface' mlir/test/lib/Dialect/Test/TestTypes.cpp
```

## 12.8 可跑实验

### 实验 1：接口存在性从方言注册开始（实测）

```bash
$T/bin/mlir-opt --show-dialects | tr ',' '\n' | wc -l
```

```text
50
```

```bash
$T/bin/mlir-opt --show-dialects | tr ',' '\n' | head -12
```

```text
Available Dialects: acc
affine
amdgpu
arith
arm_neon
arm_sme
arm_sve
async
bufferization
builtin
cf
complex
```

只有已注册的方言才能贡献接口。**“某个 op 不支持某接口”的第一步永远是确认它的方言已注册**（`--show-dialects` 里有它）。

### 实验 2：`InterfacePass` 只在实现者上跑（实测）

```bash
printf 'func.func @main() {\n  return\n}\n' > /tmp/mw/ch12.mlir
$T/bin/mlir-opt /tmp/mw/ch12.mlir -pass-pipeline='builtin.module(func.func(test-interface-pass))'
```

```text
/tmp/mw/ch12.mlir:1:1: remark: Executing interface pass on operation
func.func @main() {
^
/tmp/mw/ch12.mlir:1:1: note: see current operation: 
func.func @main() {
  return
}
module {
  func.func @main() {
    return
  }
}
```

`remark` 是 pass 里 `emitRemark()` 打的（`TestPassManager.cpp` 的 `TestInterfacePass`）。换一个不实现 `FunctionOpInterface` 的 op 就不会有这条 remark——**静默跳过是默认行为**。

### 实验 3：锚点错时 interface pass 直接拒绝调度（实测）

```bash
$T/bin/mlir-opt /tmp/mw/ch12.mlir -pass-pipeline='builtin.module(test-interface-pass)'
```

```text
<unknown>:0: error: unable to schedule pass '{anonymous}::TestInterfacePass' on a PassManager intended to run on 'builtin.module'!
```

注意这是**注册期**（`finalizePassList`）就失败，不是运行到那个 op 才失败。所以“interface pass 写对了但 pipeline 报错”看的是锚点，不是接口。

### 实验 4：外部接口在真实方言上的行为（实测）

`-test-acc-pointer-like-interface` 走 OpenACC 的 `PointerLikeTypeInterface`：

```bash
cat > /tmp/mw/ch12acc.mlir <<'EOF'
func.func @test_memref_cast_static_to_dynamic() {
  %0 = memref.alloca() {test.cast, cast_dest = memref<?xf32>} : memref<4xf32>
  return
}
EOF
$T/bin/mlir-opt /tmp/mw/ch12acc.mlir \
  -pass-pipeline='builtin.module(func.func(test-acc-pointer-like-interface{test-mode=cast}))'
```

```text
Successfully generated cast for operation: %alloca = memref.alloca() {cast_dest = memref<?xf32>, test.cast} : memref<4xf32>
	Cast result type: memref<?xf32>
	Generated: %cast = memref.cast %alloca : memref<4xf32> to memref<?xf32>
module {
  func.func @test_memref_cast_static_to_dynamic() {
    %alloca = memref.alloca() {cast_dest = memref<?xf32>, test.cast} : memref<4xf32>
    %cast = memref.cast %alloca : memref<4xf32> to memref<?xf32>
    return
  }
}
```

这就是“算法不认识 memref，但通过接口拿到了 `cast` 的实现”。同样输入用 `test-mode=walk` 则什么都不打（memref 没有 `test.cast` 标记的 op）：

```bash
$T/bin/mlir-opt /tmp/mw/ch12walk.mlir \
  -pass-pipeline='builtin.module(func.func(test-acc-pointer-like-interface{test-mode=walk}))'
```

```text
module {
  func.func @walk(%arg0: memref<4xf32>) {
    %alloca = memref.alloca() : memref<4xf32>
    return
  }
}
```

**没有输出 = 没有匹配的接口调用**，不是接口不存在。

### 实验 5：op interface 的默认实现（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Dialect/DLTI/query-op-interface.mlir -test-dlti-query-op-interface 2>&1 | head -10
```

```text
module <dlti = #dlti.map<"module" = 1 : i32>> {
  "test.op_with_data_layout"() <{dlti = #dlti.map<"replaced" = 2 : i32, i32 = 32 : i32, "inserted" = 1 : i32>}> ({
  }) : () -> ()
  "test.op_with_data_layout"() ({
  }) : () -> ()
  "test.op_with_data_layout"() <{dlti = #dlti.dl_spec<"immutable" = 0 : i32>}> ({
  }) {test.set_dlti_failed} : () -> ()
  "test.op_with_data_layout"() <{dlti = #dlti.map<"kept" = 0 : i32>}> ({
  }) : () -> ()
}
```

逐个对照输入里的 `test.dlti_action`：

- `update` → `replaced` 改成 2、新增 `inserted`、新增 `i32 = 32`，`removed` 消失（`setDlti(key, Attribute())` 是删除）。
- `clear` → 只剩 `only` 被删后的空 map，`dlti` 属性整体消失。
- `fail` → `dl_spec` 是不可变表示，`setDlti` 失败，op 被打上 `test.set_dlti_failed`。
- 第四个（`kept`）没有 action，原样保留。

### 实验 6：接口代码是 tblgen 生成的（实测）

```bash
$T/bin/mlir-tblgen -gen-op-interface-decls -I mlir/include \
  mlir/include/mlir/Interfaces/LoopLikeInterface.td -o /tmp/mw/loop.h
wc -l /tmp/mw/loop.h
```

```text
988 /tmp/mw/loop.h
```

```bash
$T/bin/mlir-tblgen -gen-op-interface-defs -I mlir/include \
  mlir/include/mlir/Interfaces/CallInterfaces.td -o /tmp/mw/call-iface.cpp
wc -l /tmp/mw/call-iface.cpp
```

```text
211 /tmp/mw/call-iface.cpp
```

`decls` 里能看到 `Concept`（`:70`）和 `FallbackModel`（`:118`）；`defs` 里是 `::mlir::CallOpInterface::getCallableForCallee()` 这类转发到 `getImpl()->...` 的薄封装：

```cpp
::mlir::CallInterfaceCallable mlir::CallOpInterface::getCallableForCallee() {
      return getImpl()->getCallableForCallee(getImpl(), getOperation());
  }
```

维护含义：**接口方法的性能 = 一次间接调用**；不要在热路径上按 op 反复 `dyn_cast` 同一个接口，缓存接口对象（`LoopLikeOpInterface` 这类是值类型，很轻）。

### 实验 7：attr interface 的“能力缺口”（实测）

`-test-elements-attr-interface` 用 `ElementsAttr` 的迭代接口，对同一个属性尝试多种 C++ 元素类型：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/IR/elements-attr-interface.mlir \
  -test-elements-attr-interface -verify-diagnostics --mlir-disable-threading 2>&1 | head -4
```

```text
module {
  %cst = arith.constant #test.i64_elements<[10, 11, 12, 13, 14]> : tensor<5xi64>
  %cst_0 = arith.constant dense<[10, 11, 12, 13, 14]> : tensor<5xi64>
```

输出是“无诊断”的干净 IR，说明所有 `expected-error` 都对上了。真正的信息在输入文件的第 12 行：

```text
// expected-error@below {{Test iterating `int64_t`: unable to iterate type}}
// expected-error@below {{Test iterating `uint64_t`: 10, 11, 12, 13, 14}}
```

同一个 `#test.i64_elements`，`uint64_t` 可迭代、`int64_t` 不可迭代。这不是 bug，是接口的能力边界：`ElementsAttr` 的迭代模板只对特定类型开了特化。维护含义：**接口存在 ≠ 所有参数组合都支持**，读接口文档时要看 `FailureOr`/`bool` 返回的失败条件。

### 实验 8：未实测项

- **`declarePromisedInterface` 的 fatal error 文本**：要触发得写一个“只 promise 不实现”的方言并重编 `mlir-opt`；源码树只读，**未实测**。文本见 `Dialect.h:228-240`。
- **`ExternalModel` 在 `SimpleAType` 上的运行时效果**：本树没有暴露对应的 CLI 查询（`TestTypes.cpp:411-423` 只做注册），**未实测**，仅按源码陈述。

## 12.9 踩雷集锦

1. **错误直觉：`dyn_cast<SomeInterface>(op)` 失败会报错。** → 正确认识：返回空，算法静默走保守路径；这是“精度莫名下降”的第一嫌疑。
2. **错误直觉：`attachInterface` 可以给单个 op 实例挂模型。** → 正确认识：它改的是 `RegisteredOperationName` 的 `InterfaceMap`，按**类型**生效，且应在 `Dialect::initialize` 里调。
3. **错误直觉：`declarePromisedInterface` 只是文档。** → 正确认识：查询未实现的 promise 会 `report_fatal_error`，进程中止。
4. **错误直觉：`addInterface` 重复注册会 assert。** → 正确认识：只打一条 `repeated interface registration` 的 debug 日志（`Dialect.cpp:110-111`）。
5. **错误直觉：`DialectInterfaceCollection` 会看到运行期新加载的方言。** → 正确认识：构造时快照（`Dialect.cpp:121-133`）。
6. **错误直觉：有 `defaultImplementation` 的接口方法不用管。** → 正确认识：默认实现是一条约定；覆盖它时返回 `failure()` 必须被调用方处理，否则行为和默认实现没区别。
7. **错误直觉：interface pass 和普通 pass 一样可以挂在任意锚点。** → 正确认识：`InterfacePass<T>` 的锚点是**所有实现 T 的 op**，挂错 manager 会在 `finalizePassList` 报 `unable to schedule`。
8. **错误直觉：接口方法调用很贵，要尽量避免。** → 正确认识：它是一次函数指针间接调用（`getImpl()->...`）；真正贵的是每次重新 `dyn_cast` 和 `lookup`，缓存接口值是常规做法。

## 12.10 本章重点整理

- 接口 = `(op 类型 → Concept*)` 的查表；四次分派：`classof` → `getInterfaceFor` → `InterfaceMap::lookup` → 函数指针。
- 四种实现：op 直接实现、`FallbackModel`（方言兜底）、`ExternalModel`（事后给别人的 op/type 挂）、`DialectInterface`（方言级策略）。
- `attachInterface` 按类型生效、在 initialize 里调；`addInterface` 重复注册静默。
- `declarePromisedInterface` 是欠条，查询未实现会 fatal abort，不是 diagnostic。
- `DialectInterfaceCollection` 是构造时快照，运行期新加载方言不可见。
- `defaultImplementation` 让 op 零成本声明支持（`DLTIQueryOpInterface` 是范本）。
- 接口查不到时算法**不报错**，所以“精度问题”要先查注册。

## 12.11 自我检核

- [ ] 能画出从 `dyn_cast<OpInterface>` 到 `InterfaceMap::lookup` 的四层
- [ ] 能说出 `Model` / `FallbackModel` / `ExternalModel` 三者的区别与使用场景
- [ ] 能解释 `attachInterface` 为什么必须在 `Dialect::initialize` 里调用
- [ ] 能说出 `declarePromisedInterface` 的失败形态（fatal，不是 error）
- [ ] 能复现 `InterfacePass` 的 remark，并解释换一个 op 为什么没有输出
- [ ] 能读懂 `DLTIQueryOpInterface` 的 `defaultImplementation` 与测试 op 的覆盖行为
- [ ] 能用 `mlir-tblgen -gen-op-interface-decls/-defs` 生成接口代码并指出 `Concept`/`FallbackModel`
- [ ] 能解释 `DialectInterfaceCollection` 的快照语义会带来什么 bug

## 12.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Interfaces.md`**
  - 读哪里：`### Dialect Interfaces`（22 行）、`#### DialectInterfaceCollection`（176 行）、`#### External Models`（299 行）、`#### Dialect Fallback for OpInterface`（414 行）
  - 能学到什么：官方对四种实现方式的取舍说明；和本章关联：12.3/12.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Support/InterfaceSupport.h`**
  - 读哪里：`60-260`（`Interface` 模板、`InterfaceMap`、`insertModels`）
  - 能学到什么：查表与类型擦除的真实实现；和本章关联：12.2
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Dialect.cpp`**
  - 读哪里：`101-145`（`addInterface` 与 collection）
  - 能学到什么：方言接口的注册与快照；和本章关联：12.3/12.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/DLTI/DLTIOpInterfaces.td`**
  - 读哪里：`14-57`（接口定义与默认实现）
  - 能学到什么：带默认实现的 OpInterface 怎么写；和本章关联：12.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Test/TestTypes.cpp`**
  - 读哪里：`411-423`（`PtrElementModel` 与 `attachInterface`）
  - 能学到什么：ExternalModel 的最小可编译样例；和本章关联：12.4 方式 3

接口告诉算法“这个 op 能做什么”，debug 工具告诉你在哪里崩的。下一章把前四章的手段合起来：用 `-mlir-print-ir-*`、crash reproducer 和 `mlir-reduce` 把一个真实故障压到最小可复现。

→ 下一章：[Ch 13 调试、复现与最小化](./13-debugging-and-reduction.md)
