# Ch 18 TypeConverter 与 1:N 降级

> **目标**：搞清降级里最容易出错的一层——类型映射。学完本章，你能区分 1:1 与 1:N 转换、看懂 `SignatureConversion` 如何重写函数入口、说清 materialization 为什么不是可选项，并能从 `convert-func-to-llvm` 的真实输出反推 `index` 与 `memref` 的映射规则。

## 为什么需要这个？

Ch 17 的 pattern 只回答「这个 op 换成什么 op」。真正麻烦的是：**换完之后，类型对不上怎么办？**

三个典型场景：

1. `index` 在源 IR 里是一个类型，到了 LLVM 层必须变成某个整数（默认 `i64`）。
2. `memref<4xf32>` 在源 IR 里是一个值，到了 LLVM 层要拆成「分配指针、对齐指针、offset、size、stride」五个值。
3. 函数签名在源 IR 里写 `(%m: memref<4xf32>)`，到了 LLVM 层不能还是一个 `memref`，必须重写成五个参数。

第 2、3 条就是 **1:N 降级**：一个类型变成多个类型，一个参数变成多个参数。它和「一个 op 换成多个 op」不是一回事——后者 pattern 自己就能做，前者必须由 TypeConverter 统一裁决，否则每个 pattern 都会用不同的拆法。

本章把这些规则拆开，并用本机的 `convert-func-to-llvm` / `convert-arith-to-llvm` 验证。

## 先建立直觉：类型映射是关系，不是函数

普通函数是 `A -> B`；TypeConverter 的映射是 `A -> {B1, B2, ...}`，而且可以是空集：

```text
memref<4xf32>  ──▶  { ptr, ptr, i64, i64, i64 }
index          ──▶  { i64 }
tensor<4xf32>  ──▶  { }            // 没有注册转换时
```

由此产生两个方向的需求：

```text
源类型的值  ──target materialization──▶  目标类型的值
目标类型的值 ──source materialization──▶  源类型的值
```

- **target materialization**：某个还没转换的 pattern（或 op）需要源类型的值时，把已经拆好的目标值「拼回去」。
- **source materialization**：某个还没转换的 op 还在用源类型时，把目标值「还原」成源类型，先让 IR 合法。

这两个方向都不是「免费 bitcast」。它们是**由你注册的回调生成的真实 op**，最差情况下就是一条 `builtin.unrealized_conversion_cast`。Ch 19 会专门讲这条 cast 的命运；本章先讲它为什么必须存在。

## 核心概念一：1:1 与 1:N 都只是 `addConversion`

`TypeConverter` 的注册接口只有两类回调形态（`DialectConversion.h:145-176`）：

```cpp
// 1:1
std::optional<Type>(T);
// 1:N
std::optional<LogicalResult>(T, SmallVectorImpl<Type> &);
```

`T` 可以是 `Type` 的子类，也可以是 `Value`。用 `Value` 的重载是 **context-aware**：它能看到具体 IR，因此不会被缓存；用 `Type` 的重载会被缓存（`DialectConversion.h:162-171`）。

几个容易忽略的语义：

- **返回 `nullopt` 表示「我不处理」，不是失败**。框架会继续尝试下一条注册的转换。
- **返回空的类型列表表示「这个值被删除」**。后续使用必须在转换过程中被消掉（`DialectConversion.h:154-160`）。
- **转换函数按注册顺序的逆序尝试**。后注册的优先（`DialectConversion.h:170-171`）。
- **1:1 只是 1:N 的特例**。`convertType(Type)` 这个单值重载只能表达 1:1；要 1:N 必须用 `convertType(Type, SmallVectorImpl<Type>&)`（`DialectConversion.h:258-279`）。

LLVM 的转换器就是一堆这样的注册。`index` 的规则只有一行本质：

```cpp
// LLVMCommon/TypeConverter.cpp:278-288（简化）
Type LLVMTypeConverter::getIndexType() const {
  return IntegerType::get(&getContext(), getIndexTypeBitwidth());
}
Type LLVMTypeConverter::convertIndexType(IndexType type) const {
  return getIndexType();
}
```

`getIndexTypeBitwidth()` 来自 `LowerToLLVMOptions`（`LLVMCommon/TypeConverter.h:148`），默认是目标机器字长。这就是 `index` 默认变 `i64` 的来源，也是 `index-bitwidth` 选项能改它的原因。

`memref` 的规则是 1:N：

```cpp
// LLVMCommon/TypeConverter.cpp:534-544
/// Converts MemRefType to LLVMType. A MemRefType is converted to a struct that
/// packs the descriptor fields as defined by `getMemRefDescriptorFields`.
Type LLVMTypeConverter::convertMemRefType(MemRefType type) const {
  SmallVector<Type, 5> types =
      getMemRefDescriptorFields(type, /*unpackAggregates=*/false);
  ...
  return LLVM::LLVMStructType::getLiteral(&getContext(), types);
}
```

注意这里的 `unpackAggregates=false`：作为**普通值**，memref 变成一个 struct；作为**函数参数**，下面会看到它被拆成多个标量。

## 核心概念二：SignatureConversion 与 `convertSignatureArgs`

函数签名不是「一个类型」，而是一组输入 + 一组输出。`TypeConverter` 用一个专门的对象描述重写结果：

```cpp
// DialectConversion.h:63-115（简化）
class SignatureConversion {
  struct InputMapping {
    size_t inputNo, size;
    SmallVector<Value, 1> replacementValues;
  };
  void addInputs(unsigned origInputNo, ArrayRef<Type> types);
  void addInputs(ArrayRef<Type> types);
  void remapInput(unsigned origInputNo, ArrayRef<Value> replacements);
};
```

三个方法对应三种需求：

| 方法 | 用途 | 典型场景 |
|---|---|---|
| `addInputs(orig, types)` | 把第 `orig` 个参数映射成 N 个新类型 | `memref` 参数拆成 5 个 |
| `addInputs(types)` | 追加与原参数无关的新参数 | C wrapper 额外加返回值指针 |
| `remapInput(orig, values)` | 丢掉原参数，直接用已有值替代 | 入口块参数已经由调用方准备好 |

`convertSignatureArgs` 是批量版本，按顺序填 `SignatureConversion`：

```cpp
// DialectConversion.h:328-340
LogicalResult convertSignatureArg(unsigned inputNo, Type type,
                                  SignatureConversion &result) const;
LogicalResult convertSignatureArgs(TypeRange types, SignatureConversion &result,
                                   unsigned origInputOffset = 0) const;
```

真正把结果应用到 IR 的是 rewriter：

```cpp
// DialectConversion.h:846-865
Block *applySignatureConversion(Block *block,
                                TypeConverter::SignatureConversion &conversion,
                                const TypeConverter *converter = nullptr);
```

它会把 block 换成一个新 block，把原 block 的 op 搬过去，并把旧参数的使用替换成新参数（必要时插 materialization）。注意最后那个 `converter` 参数：注释明确说，如果不给、或者转换器没有合适的 materialization 规则，**收尾时可能无法 legalize**（`DialectConversion.h:859-861`）。这就是下一节的主题。

## 核心概念三：materialization 为什么绕不过去

看 `TypeConverter` 对 materialization 的定义（`DialectConversion.h:178-227`；23.1.4 没有单独的 `addMaterialization`，只有 `addSourceMaterialization` 与 `addTargetMaterialization`）：

- 回调形态是 `Value(OpBuilder&, T, ValueRange, Location)`；
- 返回 `nullptr` 表示「我处理不了，换下一个」；
- target materialization 可以多收一个 `originalType` 参数；
- 1:N 的 target materialization 可以返回 `SmallVector<Value>`。

为什么非要有它？因为 1:N 映射**不可逆**。

以 `memref` 为例：拆成 5 个值之后，仅凭「分配指针 + 对齐指针 + offset + size + stride」这五个 SSA 值，框架没法凭空知道你原来想还原成 `memref<4xf32>` 还是 `memref<2x2xf32>`——rank 不同，descriptor 的字段数都不同。所以 LLVM 转换器注册了专门的 pack/unpack 回调：

```cpp
// LLVMCommon/TypeConverter.cpp:223-248（简化）
// Add generic source and target materializations to handle cases where
// non-LLVM types persist after an LLVM conversion.
addSourceMaterialization([&](OpBuilder &builder, Type resultType, ValueRange inputs, Location loc) {
  return UnrealizedConversionCastOp::create(builder, loc, resultType, inputs).getResult(0);
});
addTargetMaterialization([&](OpBuilder &builder, Type resultType, ValueRange inputs, Location loc) {
  return UnrealizedConversionCastOp::create(builder, loc, resultType, inputs).getResult(0);
});
// Source materializations convert from the new block argument types
// (multiple SSA values that make up a memref descriptor) back to the
// original block argument type.
addSourceMaterialization([&](OpBuilder &builder, MemRefType resultType, ValueRange inputs, Location loc) {
  return rankedMemRefMaterialization(builder, resultType, inputs, loc, *this);
});
```

这段代码解释了两件事：

1. **memref 的 materialization 是「打包 descriptor」**，不是空操作；`packRankedMemRefDesc` 会用 `MemRefDescriptor::pack` 生成一串 `llvm.insertvalue`。
2. **兜底的 source/target materialization 就是 `unrealized_conversion_cast`**。当类型没有被专门处理时，框架只能先插一个「欠条」，等收尾再算账。

最后那句「等收尾再算账」不是比喻。转换结束时如果还有活着的 materialization 无法落实，driver 直接报错：

```text
failed to legalize unresolved materialization from (<types>) to (<types>) that remained live after conversion
```

源码在 `DialectConversion.cpp:3388-3393`。所以 materialization 的完整契约是：**要么你注册了能生成真实 op 的回调，要么类型映射本身自洽到不需要它**；只靠兜底 cast 是过不了收尾的。

## 底层机制：`convert-func-to-llvm` 如何重写签名

把 `FuncOpConversion` 的主干抽出来（`FuncToLLVM.cpp:447-497`）：

```cpp
TypeConverter::SignatureConversion result(funcOp.getNumArguments());
FailureOr<LLVM::LLVMFunctionType> llvmType = convertFuncSignature(
    funcOp, converter, useBarePtrCallConv, result, byValRefNonPtrAttrs);
...
auto newFuncOp = createLLVMFuncOp(funcOp, rewriter, *llvmType, *loweredAttrs, symbolTables);
rewriter.inlineRegionBefore(funcOp.getFunctionBody(), newFuncOp.getBody(), newFuncOp.end());
if (!newFuncOp.getBody().empty())
  rewriter.applySignatureConversion(&newFuncOp.getBody().front(), result, &converter);
```

顺序很关键：

1. 先算 `SignatureConversion`（每个旧参数对应哪些新类型）；
2. 用新签名创建 `llvm.func`；
3. 把旧函数体搬进来；
4. **只对 entry block 做签名转换**，其余 block 交给 `ControlFlowToLLVM`（注释在 `FuncToLLVM.cpp:492-493`）。

第 1 步内部按参数逐个展开（`LLVMCommon/TypeConverter.cpp:366-388`）：

```cpp
auto funcArgConverter = useBarePtrCallConv ? barePtrFuncArgTypeConverter
                                           : structFuncArgTypeConverter;
for (auto [idx, type] : llvm::enumerate(funcTy.getInputs())) {
  SmallVector<Type, 8> converted;
  if (failed(funcArgConverter(*this, type, converted)))
    return {};
  ...
  result.addInputs(idx, converted);   // 一个旧参数 -> N 个新类型
}
```

`structFuncArgTypeConverter` 对 memref 的处理是「展开、不打包」：

```cpp
// LLVMCommon/TypeConverter.cpp:802-812
if (auto memref = dyn_cast<MemRefType>(type)) {
  // In signatures, Memref descriptors are expanded into lists of
  // non-aggregate values.
  auto converted = converter.getMemRefDescriptorFields(memref, /*unpackAggregates=*/true);
  ...
  result.append(converted.begin(), converted.end());
}
```

返回值则走另一条规则（`LLVMCommon/TypeConverter.cpp:394-397`）：

```cpp
// 0 个结果 -> void；1 个结果 -> 原样转换；多个结果 -> 打包成 struct
Type resultType =
    funcTy.getNumResults() == 0
        ? LLVM::LLVMVoidType::get(&getContext())
        : packFunctionResults(funcTy.getResults(), useBarePtrCallConv);
```

于是「多返回值」在 LLVM 层变成「一个 struct 返回值」，`ReturnOpLowering` 负责用 `llvm.insertvalue` 打包（`FuncToLLVM.cpp:743-750`）。

## 示例一：`index` → `i64` 与签名重写

准备 `inc.mlir`：

```mlir
module {
  func.func @inc(%i: index) -> index {
    %one = arith.constant 1 : index
    %0 = arith.addi %i, %one : index
    return %0 : index
  }
}
```

先把 arith 在 `func.func` 内降掉，再降函数本身：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\inc.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm),convert-func-to-llvm,reconcile-unrealized-casts)'
```

实测输出（退出码 0）：

```mlir
module {
  llvm.func @inc(%arg0: i64) -> i64 {
    %0 = llvm.mlir.constant(1 : index) : i64
    %1 = llvm.add %arg0, %0 : i64
    llvm.return %1 : i64
  }
}
```

逐项对照：

- `%i: index` → `%arg0: i64`，返回值同理；这是 `convertFunctionSignature` 按 `index -> i64` 算出来的。
- `arith.constant 1 : index` → `llvm.mlir.constant(1 : index) : i64`。**常量括号里的 `: index` 是源类型语义**，冒号后的 `: i64` 才是 LLVM 类型。别把两者混为一谈。
- 没有 `unrealized_conversion_cast`：因为 arith 的转换发生在 func 之前，二者对 `index` 的映射一致。

如果只跑 `convert-func-to-llvm`，函数签名会先变 `i64`，函数体里的 `arith.addi` 仍要 `index`，driver 只能插 cast 桥接。这正是 Ch 19 的复现材料。

## 示例二：`memref` 参数的 1:N 展开

准备 `mem.mlir`：

```mlir
module {
  func.func @store(%m: memref<4xf32>, %v: f32) {
    %c0 = arith.constant 0 : index
    memref.store %v, %m[%c0] : memref<4xf32>
    return
  }
}
```

跑完整降级：

```powershell
& "$mlir\mlir-opt.exe" .\mem.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm),convert-func-to-llvm,finalize-memref-to-llvm,reconcile-unrealized-casts)'
```

实测输出（退出码 0）：

```mlir
module {
  llvm.func @store(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64, %arg5: f32) {
    %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %1 = llvm.insertvalue %arg0, %0[0] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %2 = llvm.insertvalue %arg1, %1[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %3 = llvm.insertvalue %arg2, %2[2] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %4 = llvm.insertvalue %arg3, %3[3, 0] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %5 = llvm.insertvalue %arg4, %4[4, 0] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %6 = llvm.mlir.constant(0 : index) : i64
    %7 = llvm.extractvalue %5[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %8 = llvm.getelementptr inbounds|nuw %7[%6] : (!llvm.ptr, i64) -> !llvm.ptr, f32
    llvm.store %arg5, %8 : f32, !llvm.ptr
    llvm.return
  }
}
```

这里是全章最值得盯住的一段：

- **函数签名里没有 struct**。一个 `memref<4xf32>` 参数变成了 5 个标量参数：`%arg0`（分配指针）、`%arg1`（对齐指针）、`%arg2`（offset）、`%arg3`（size）、`%arg4`（stride）。这就是 `structFuncArgTypeConverter` 的 `unpackAggregates=true`。
- **函数体里又把它们拼回 struct**。入口的 `%0..%5` 就是 source materialization 的产物：`poison` 起手，五次 `insertvalue` 还原成 `!llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>`。
- **字段数是 `3 + 2 * rank`**。rank 1 是 5 个；把例子改成 `memref<2x2xf32>` 会变成 7 个参数，struct 里的 array 变成 `array<2 x i64>`。

所以「memref 参数」在 LLVM ABI 里不是「传一个 struct」，而是「传展开后的字段」。这一点在写外部函数声明或 C wrapper 时会直接决定 ABI。

## 示例三：多返回值打包成 struct

```mlir
module {
  func.func @pair(%a: i32, %b: i32) -> (i32, i32) {
    return %b, %a : i32, i32
  }
}
```

```powershell
& "$mlir\mlir-opt.exe" .\pair.mlir --pass-pipeline='builtin.module(convert-func-to-llvm)'
```

实测输出（退出码 0）：

```mlir
module {
  llvm.func @pair(%arg0: i32, %arg1: i32) -> !llvm.struct<(i32, i32)> {
    %0 = llvm.mlir.poison : !llvm.struct<(i32, i32)>
    %1 = llvm.insertvalue %arg1, %0[0] : !llvm.struct<(i32, i32)>
    %2 = llvm.insertvalue %arg0, %1[1] : !llvm.struct<(i32, i32)>
    llvm.return %2 : !llvm.struct<(i32, i32)>
  }
}
```

这是「N 个返回值 → 1 个 struct」的反方向打包，规则在 `packFunctionResults`（`LLVMCommon/TypeConverter.cpp:394-397`）。注意 `poison` 起手再逐个 `insertvalue`：没有第一个字段的合法初始值时，这是构造聚合值的标准写法。

## 对比与取舍

### descriptor 展开 vs bare pointer

`convert-func-to-llvm` 有一个选项 `use-bare-ptr-memref-call-conv`（`Passes.td` 中定义，默认 false）。打开后，memref 参数不再展开成 5 个字段，而是一个裸指针：

```powershell
& "$mlir\mlir-opt.exe" .\mem.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm),convert-func-to-llvm{use-bare-ptr-memref-call-conv=1},finalize-memref-to-llvm,reconcile-unrealized-casts)'
```

实测输出的函数头（节选，body 省略）：

```mlir
llvm.func @store(%arg0: !llvm.ptr, %arg1: f32) {
```

对比两种 call conv：

| 维度 | descriptor 展开（默认） | bare pointer |
|---|---|---|
| 参数个数 | `3 + 2 * rank` 个标量 | 1 个 `!llvm.ptr` |
| 动态 shape | 支持 | 只有静态 shape 才能安全还原 |
| 函数体 | 入口 `insertvalue` 拼 descriptor | 用 `MemRefDescriptor::fromStaticShape` 补全 |
| 适用 | 通用 ABI、跨语言边界 | 内部 kernel、已知静态形状 |

## 踩雷集锦

1. **把 1:N 理解成「一个值变多个 op」。** 1:N 是一个**类型**映射到多个**SSA 值**；多个 op 是 pattern 的自由，不需要 TypeConverter。
2. **只用 `convertType(t)` 单值重载。** 它只能返回一个类型，遇到 memref 这类 1:N 映射会直接失败；要用 `SmallVectorImpl<Type>` 重载。
3. **只改签名，不注册 materialization。** `applySignatureConversion` 的注释已经警告过：没有合适的 materialization，收尾会失败（`DialectConversion.h:859-861`）。
4. **在 pattern 里读原始 operand。** 拿到的 adaptor 才是转换后的值；旧值是源类型，混用会生成类型不匹配的 IR。
5. **以为 memref 参数在 LLVM 签名里是 struct。** 默认是展开的标量列表；只有函数体里才拼回 struct。

## 进阶：再往深一层

- **context-aware 转换**：用 `Value` 作参数的 `addConversion` 能看到具体 IR（比如按 use 决定布局），但不会被缓存；热路径上要自己权衡（`DialectConversion.h:162-168`）。
- **`convertRegionTypes`**：CFG 里每个 block 的参数都要转换，不只是 entry block；`ControlFlowToLLVM` 就是这么做的（`ControlFlowToLLVM.cpp:117`）。

## 动手练习

约定：文件放在 `.scratch\mlir-17-19\`，`$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"`。每题至少三个用例。

### 练习 1：预测 1:N 的参数个数

**任务**：把 `mem.mlir` 的 memref 依次改成 `memref<4xf32>`、`memref<2x2xf32>`、`memref<?xf32>`，先按 `3 + 2 * rank` 预测签名参数个数，再跑验证。

**测试用例**：(a) rank 1 静态；(b) rank 2 静态；(c) rank 1 动态 shape。

**步骤**：1) 改类型；2) 预测；3) 跑 `convert-func-to-llvm + finalize-memref-to-llvm`；4) 数 `llvm.func` 的参数。

<details><summary>参考解答</summary>

(a) 5 个；(b) 7 个（本机实测 `%arg0..%arg6`）；(c) 仍是 5 个，但 size/stride 由运行时值填充。动态 shape 不改变字段数，只改变字段来源。

</details>

**常见卡点**：把 `array<1 x i64>` 当成一个参数——它在签名里被展开成 1 个标量。

### 练习 2：只降函数，观察 materialization

**任务**：对 `inc.mlir` 只跑 `convert-func-to-llvm`，找出哪些 op 是框架插入的 materialization，并说明方向。

**测试用例**：(a) 只降 func；(b) 只降 arith；(c) 先 arith 后 func。

**步骤**：1) 三个用例各存 IR；2) 统计 `unrealized_conversion_cast`；3) 对每条 cast 标注 source/target；4) 解释 (c) 为什么干净。

<details><summary>参考解答</summary>

(a) 两条：入口 `i64 -> index` 是 source materialization（还原给未转换的 arith 用），返回前 `index -> i64` 是 target materialization（满足新签名）。(b) 没有 cast，但 `func.func` 还在。(c) 类型映射一致，无需桥。

</details>

**常见卡点**：把两条 cast 的方向记反；记住 source 是「回源类型」，target 是「去目标类型」。

### 练习 3：bare pointer 的 ABI 差异

**任务**：对 `mem.mlir` 分别用默认 call conv 和 `use-bare-ptr-memref-call-conv=1`，写出两种签名的 C 等价声明。

**测试用例**：(a) 默认；(b) bare ptr；(c) rank 2 + bare ptr（应报错或行为不同）。

**步骤**：1) 跑两条 pipeline；2) 抄函数头；3) 写 C 声明；4) 解释为什么 rank 2 用 bare ptr 会丢 stride 信息。

<details><summary>参考解答</summary>

(a) `void store(void*, void*, int64_t, int64_t, int64_t, float)`；(b) `void store(void*, float)`；(c) bare ptr 假设静态连续布局，rank 2 仍能算地址，但非连续 view 会错——所以它只适合「保证连续」的内部 ABI。

</details>

**常见卡点**：忘了 bare ptr 仍会在函数体里补 descriptor，签名和 body 是两套表示。

### 练习 4：读源码找 materialization

**任务**：在 `LLVMCommon/TypeConverter.cpp` 里找出所有 `addSourceMaterialization` / `addTargetMaterialization`，按「通用 / memref / bare ptr」分类。

**测试用例**：(a) memref 参数；(b) 非 memref 的未转换类型；(c) bare ptr 参数。

**步骤**：1) `Select-String`；2) 记录行号；3) 对每个用例指出命中哪条回调；4) 解释兜底回调为什么返回 `unrealized_conversion_cast`。

<details><summary>参考解答</summary>

通用兜底在 225-234；memref source 在 239-248；bare ptr → descriptor 的 target 在 251-266。用例 (a) 命中 memref 专用回调；(b) 落到通用兜底，生成 `unrealized_conversion_cast`；(c) 命中 bare ptr 回调。

</details>

**常见卡点**：只看函数名不看注册顺序；后注册的优先，兜底必须最后。

### 练习区自我检核

- [ ] 我能区分「1:N 类型」与「1:N op」
- [ ] 我能解释 signature conversion 为什么只改 entry block

## 本章重点整理

- TypeConverter 的映射是 `A -> {B1..Bn}`，可以是 1:1、1:N，也可以是空集（删除值）。
- 1:1 用 `optional<Type>` 回调；1:N 用 `optional<LogicalResult>(T, SmallVectorImpl<Type>&)`。
- `index` 默认映射为目标字长的整数（`i64`），由 `index-bitwidth` / DataLayout 决定。
- memref 作为普通值是 struct；作为函数参数默认展开成 `3 + 2 * rank` 个标量。
- `SignatureConversion` 记录「旧参数 → 新类型列表」，由 `convertSignatureArgs` 填充、`applySignatureConversion` 落地。
- materialization 负责两个方向的桥；1:N 不可逆，所以没有它就无法把值还原回源类型。
- LLVM 转换器的兜底 materialization 就是 `unrealized_conversion_cast`，它不是真正的转换。

## 自我检核

- [ ] 我能写出 1:1 与 1:N 两种 `addConversion` 回调的签名
- [ ] 我能解释 `convertType(Type)` 为什么表达不了 1:N
- [ ] 我能说出 `SignatureConversion::addInputs` 的两个重载各自的用途
- [ ] 我能解释 memref 参数为什么在签名里被展开
- [ ] 我能解释 `applySignatureConversion` 为什么需要 converter 参数
- [ ] 我知道 memref descriptor 的字段顺序
- [ ] 我能说出多返回值在 LLVM 签名里如何表示

## 延伸阅读

- **[MLIR 文档：Dialect Conversion — Type Conversion](https://mlir.llvm.org/docs/DialectConversion/#type-conversion)**
  - **读哪里**：Type Conversion 与 Materialization 两节。
  - **学什么**：官方对 1:N、source/target materialization 的定义。
  - **和本章的关联**：本章的核心概念一至三。

- **[mlir/include/mlir/Transforms/DialectConversion.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Transforms/DialectConversion.h)**
  - **读哪里**：`SignatureConversion`（65-115）、`addConversion`（145-176）、materialization（178-227）、`convertSignatureArgs`（328-345）。
  - **学什么**：每个回调的契约与失败语义。
  - **和本章的关联**：示例与踩雷集的依据。

- **[mlir/lib/Conversion/LLVMCommon/TypeConverter.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/LLVMCommon/TypeConverter.cpp)**
  - **读哪里**：materialization 注册（223-266）、index（278-288）、签名转换（360-401）、memref 字段（536-544、802-821）。
  - **学什么**：`index`/`memref` 的真实映射规则。
  - **和本章的关联**：示例一、二的输出就是这些函数的产物。

- **[mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp)**
  - **读哪里**：`FuncOpConversion`（447-497）、`ReturnOpLowering`（743-750）、pass 入口（839-880）。
  - **学什么**：签名重写的顺序与返回值打包。
  - **和本章的关联**：底层机制一节逐行对应。

---

下一章处理本章留下的尾巴：当类型映射不一致时，那些 `unrealized_conversion_cast` 从哪来、能不能消掉、为什么 `reconcile-unrealized-casts` 不是万能补丁。

→ [Ch 19 动态 legality 与 unrealized_conversion_cast](./19-dynamic-legality.md)
