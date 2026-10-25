# Ch 8 Dialect Interface 与 external model

> **目标**：弄清 MLIR 的接口（interface）为什么存在、怎么实现、什么时候该用 external model。学完本章，你看到 `MemoryEffectOpInterface`、`BufferizableOpInterface` 这类名字时，应该能立刻回答三个问题：它绑定在谁身上、实现写在哪里、没有实现时框架会怎么退化。

## 为什么需要这个？

MLIR 的方言是开放的。`arith`、`linalg`、`func`、你明天自己写的方言，可以同时出现在一个 module 里。但框架代码不能为每个方言写一个 `switch`：

- `canonicalize` 要判断一个 op 能不能安全删除；
- `one-shot-bufferize` 要知道一个 tensor op 怎么改写成 memref op；
- `inline` 要知道一个 op 是不是函数、怎么拿函数体；
- `symbol-dce` 要知道一个 op 是不是符号、有没有被引用。

如果这些判断都写成“op 名字等于 `arith.addi` 就……”，每加一个方言就要改一次框架，框架和方言会互相污染。MLIR 的答案是**接口**：方言声明“我的 op 具备某种能力”，框架只面向能力编程。

问题是，C++ 的继承是封闭的。`arith::AddIOp` 的类定义在 `arith` 方言里，你不可能跑到 `linalg` 方言里给它加一个基类；内置类型 `IntegerType` 更不可能为了你的框架改继承关系。**external model** 就是为这个场景准备的：把接口实现写在对象定义之外，再注册到某个 `MLIRContext` 上。

## 先建立直觉：接口是“能力查询”，不是“类型继承”

先区分三个容易混的东西：

| 机制 | 绑定层级 | 典型查询 | 能否在定义外添加 |
|---|---|---|---|
| Trait | op 的静态属性 | `op->hasTrait<OpTrait::IsTerminator>()` | 不能，属于 op 定义 |
| OpInterface | 单个 op | `isa<MemoryEffectOpInterface>(op)` | 能，external model |
| TypeInterface | 单个类型 | `isa<ShapedType>(type)` 背后的接口机制 | 能，external model |
| AttrInterface | 单个属性 | `isa<ElementsAttr>(attr)` 背后的接口机制 | 能，external model |
| DialectInterface | 整个 dialect | `dialect->getRegisteredInterface<...>()` | 能，dialect extension |

Trait 是编译期就能回答的“这个类有没有这个性质”；接口是运行期按对象查表得到的“这个对象有没有实现这个能力”。接口查询长这样：

```cpp
Operation *op = ...;
if (auto effects = dyn_cast<MemoryEffectOpInterface>(op)) {
  if (effects.hasNoEffect())
    /* 可以安全删除 */;
}
```

`dyn_cast` 在接口不存在时返回空接口；`cast` 会断言。**不要对不确定的 op 用 `cast`**，这是本章后面“踩雷集锦”的第一条。

## 核心概念一：OpInterface、TypeInterface、AttrInterface

三个基类分别在：

- `OpInterface`：`mlir/include/mlir/IR/OpDefinition.h:2124-2168`
- `TypeInterface`：`mlir/include/mlir/IR/Types.h:262-286`
- `AttributeInterface`：`mlir/include/mlir/IR/Attributes.h:252-276`

它们都建立在同一套 concept-based polymorphism 上。`mlir/include/mlir/Support/InterfaceSupport.h:71-137` 里的 `detail::Interface` 定义了公共骨架：一个 `Concept` 虚接口、一个 `Model<ConcreteT>` 实现、一个 `FallbackModel`、一个 `ExternalModel`。三者的区别只是“被包装的值”分别是 `Operation *`、`Type`、`Attribute`。

在 ODS 里声明一个 op 接口，通常长这样：

```tablegen
def MyOpInterface : OpInterface<"MyOpInterface"> {
  let cppNamespace = "::my";
  let methods = [
    InterfaceMethod<"返回某个值", "::mlir::Value", "getPayload", (ins)>
  ];
}
```

然后让 op 声明它：

```tablegen
def MyOp : Op<My_Dialect, "my_op", [DeclareOpInterfaceMethods<MyOpInterface>]> {
  ...
}
```

`DeclareOpInterfaceMethods` 会生成方法声明，要求你在 C++ 里实现；如果接口方法有默认实现，也可以只在 ODS 里写 `methodBody`。

**builtin 方言里就有现成的例子。** `ModuleOp` 的定义在 `mlir/include/mlir/IR/BuiltinOps.td:33-35`，trait 列表里同时有 `SymbolTable`、`Symbol`、`OpAsmOpInterface`；同一个文件 `77-81` 行还声明了 `DataLayoutOpInterface` 的两个方法：

```tablegen
DataLayoutSpecInterface getDataLayoutSpec();
TargetSystemSpecInterface getTargetSystemSpec();
```

这说明一个 op 可以同时实现多个接口，接口之间互不干扰。

类型和属性也一样。`mlir/include/mlir/IR/BuiltinAttributeInterfaces.td` 定义了 `TypedAttrInterface`、`BlobAttrInterface`、`ElementsAttrInterface`；`mlir/include/mlir/IR/BuiltinTypeInterfaces.td` 定义了 `VectorElementTypeInterface`、`DenseElementTypeInterface`。其中 `ElementsAttrInterface : AttrInterface<"ElementsAttr", [TypedAttrInterface]>` 还演示了**接口继承**：一个接口可以把另一个接口作为父接口。

## 核心概念二：external model

external model 的完整说明在 `mlir/docs/Interfaces.md:299-390`。核心代码只有三行：

```cpp
struct MyModel : MyInterface::ExternalModel<MyModel, IntegerType> {
  // 实现接口方法
};

// 在某个 MLIRContext 上挂载
IntegerType::attachInterface<MyModel>(context);
```

`ExternalModel` 的第一个模板参数是“实现类自己”，第二个是“被实现的类型/op/属性”。它继承自 `FallbackModel`，所以默认实现可以复用 ODS 生成的代码；需要覆盖时直接写方法即可。

注意 `attachInterface` 是**按 context** 生效的，不是全局注册。文档里写得很清楚：其他 context 看不到这个实现，除非你也给它挂上。真实方言通常把挂载写进 dialect extension：

```cpp
void mlir::arith::registerBufferizableOpInterfaceExternalModels(
    DialectRegistry &registry) {
  registry.addExtension(+[](MLIRContext *ctx, ArithDialect *dialect) {
    ConstantOp::attachInterface<ConstantOpInterface>(*ctx);
    IndexCastOp::attachInterface<IndexCastOpInterface>(*ctx);
    SelectOp::attachInterface<SelectOpInterface>(*ctx);
  });
}
```

这段来自 `mlir/lib/Dialect/Arith/Transforms/BufferizableOpInterfaceImpl.cpp:210-217`，头文件声明在 `mlir/include/mlir/Dialect/Arith/Transforms/BufferizableOpInterfaceImpl.h:17`。它解释了一个后面会反复用到的事实：**一个方言注册了 external model，不等于它的所有 op 都实现了这个接口**。`arith` 这里只给 `ConstantOp`、`IndexCastOp`、`SelectOp` 挂了 bufferization 模型，`AddIOp`、`AddFOp` 没挂。

如果你忘了注册，MLIR 有一个“承诺接口”机制：

```cpp
void MyDialect::initialize() {
  declarePromisedInterface<SomeInterface, SomeOp>();
}
```

之后如果在没注册 external model 的情况下使用这个接口，会得到类似这样的运行期错误（文档 `Interfaces.md:402-410` 的原话）：

```text
LLVM ERROR: checking for an interface (`SomeInterface`) that was promised by dialect 'mydialect' but never implemented. This is generally an indication that the dialect extension implementing the interface was never registered.
```

这类错误比“静默地 `dyn_cast` 得到空”好定位得多。写跨方言扩展时，值得主动用 `declarePromisedInterface`。

## 核心概念三：MemoryEffectOpInterface

`MemoryEffectOpInterface`（ODS 侧的 def 名是 `MemoryEffectsOpInterface`）定义在 `mlir/include/mlir/Interfaces/SideEffectInterfaces.td:26-34`，基类 `EffectOpInterfaceBase` 在 `SideEffectInterfaceBase.td:48-150`。它只有一个必须实现的方法：

```tablegen
void getEffects(SmallVectorImpl<SideEffects::EffectInstance<MemoryEffects::Effect>> &effects);
```

四个基本效果是 `Allocate`、`Free`、`Read`、`Write`，都带一个 `Resource`。ODS 里常用的 trait 是：

```tablegen
def NoMemoryEffect : MemoryEffects<[]>;
def RecursiveMemoryEffects : NativeOpTrait<"HasRecursiveMemoryEffects">;
def Pure : TraitList<[AlwaysSpeculatable, NoMemoryEffect]>;
```

`Pure` 的定义在 `SideEffectInterfaces.td:147-149`。注意它等于“总是可推测 + 无内存效果”，比“无内存效果”多了一个条件。一个 op 可以 `NoMemoryEffect` 但不可推测（例如可能触发 UB 的运算），这种 op 不能随便 hoist。

接口还提供了一组便利方法（`SideEffectInterfaceBase.td:73-146`）：`hasEffect<Read>()`、`hasNoEffect()`、`onlyHasEffect<Write>()`、`getEffectsOnValue(v)`、`getEffectsOnResource(r)`。框架里的 `isMemoryEffectFree(op)` 就是走这套查询。

`RecursiveMemoryEffects` 表示“这个 op 自己的效果由 region 里的 op 递归决定”。`scf.if`、`linalg.generic` 这类带 region 的 op 常用它；没有这个 trait 时，框架会把整个 op 当成一个黑盒。

## 核心概念四：BufferizableOpInterface

`BufferizableOpInterface` 定义在 `mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td:14-27`。它比 memory effect 复杂得多，因为 bufferization 要回答“这个 tensor 操作读不读、写不写、和谁 alias、能不能原地做、怎么做”。

必须理解的几个方法（行号来自同一个 `.td`）：

| 方法 | 行号 | 回答的问题 |
|---|---|---|
| `bufferizesToMemoryRead` | 54 | 这个 tensor operand 是否读内存 |
| `bufferizesToMemoryWrite` | 82 | 这个 tensor operand 是否写内存 |
| `getAliasingOpOperands` | 356 | 这个 result 可能和哪些 operand alias |
| `bufferize` | 433 | 真正把 tensor op 改写成 memref op |
| `isWritable` | 460 | 这个 value 能不能原地写 |
| `hasTensorSemantics` | 597 | 这个 op 当前是不是 tensor 语义 |

接口文档特别强调**保守性**：所有 `bufferizesTo*` 和 `getAliasing*` 方法都必须按最坏情况回答；宁可多报 alias，也不能漏报。原因是一旦漏报，one-shot analysis 会错误地允许原地写，生成错误的 IR。

Linalg 的 op 是怎么声明这些能力的？看 `mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td:28-36`：

```tablegen
class LinalgStructuredBase_Op<string mnemonic, list<Trait> props>
  : Op<Linalg_Dialect, mnemonic, !listconcat([
       SingleBlockImplicitTerminator<"YieldOp">,
       DeclareOpInterfaceMethods<MemoryEffectsOpInterface>,
       DeclareOpInterfaceMethods<ConditionallySpeculatable>,
       RecursiveMemoryEffects,
       DestinationStyleOpInterface,
       LinalgStructuredInterface,
       ReifyRankedShapedTypeOpInterface], props)> {
```

这一行列表把本章的几个概念串起来了：`MemoryEffectOpInterface` 是显式声明的；`RecursiveMemoryEffects` 说明 region 里的效果要递归算；`DestinationStyleOpInterface` 说明 ins/outs 的 destination 语义；`LinalgStructuredInterface` 是 Linalg 自己的结构化接口（定义在 `LinalgInterfaces.td:227`）；`ReifyRankedShapedTypeOpInterface` 负责把 result shape 具体化。

这也解释了为什么 `linalg.add` 能被 bufferize，而 `arith.addi` 的 tensor 形式不能：前者在 ODS 里声明了完整的能力，后者在 arith 的 external model 列表里根本没有 `AddIOp`。

## 底层机制：concept-based polymorphism 到底怎么查

把接口想成“每个 IR 对象带一张能力表”。表由 `InterfaceMap` 管理（`InterfaceSupport.h:157-253`），键是接口的 `TypeID`，值是指向 `Concept` 的指针。`Model<ConcreteT>` 是这张表里的一个静态实现；`ExternalModel` 只是把“实现类”和“被实现类”拆开，让实现可以写在别处。

`OpInterface::getInterfaceFor`（`OpDefinition.h:2137-2165`）的查找顺序是：

1. 如果 op 是已注册 op，先从 `RegisteredOperationName` 的 interface map 查；
2. 查不到，再让 dialect 提供 fallback；
3. 如果 op 未注册，直接走 dialect 的 fallback。

Type 和 Attribute 走 `AbstractType::getInterface` / `AbstractAttribute::getInterface`（`Types.h:272-281`、`Attributes.h:262-270`），external model 挂在抽象类型/抽象属性上。

这个设计的代价很低：一次指针查表，不需要每个 op 实例带 vtable，也不需要改对象布局。代价是**注册是显式的**：你没挂 external model，查表就是空。

## 示例一：用 canonicalize 观察 MemoryEffectOpInterface（实测）

输入 `effects.mlir`：

```mlir
func.func @effects(%m: memref<4xi32>, %x: i32) {
  %c0 = arith.constant 0 : i32
  %dead = arith.addi %x, %c0 : i32
  %idx = arith.constant 0 : index
  memref.store %x, %m[%idx] : memref<4xi32>
  return
}
```

命令：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\effects.mlir --canonicalize
```

实测输出：

```mlir
module {
  func.func @effects(%arg0: memref<4xi32>, %arg1: i32) {
    %c0 = arith.constant 0 : index
    memref.store %arg1, %arg0[%c0] : memref<4xi32>
    return
  }
}
```

`%dead` 和它的 `%c0` 被删了，`memref.store` 留着。原因不是 canonicalize 认识 `arith.addi` 这个名字，而是它通过 `MemoryEffectOpInterface` 查到：`arith.addi` 是 `Pure`（无内存效果，可推测），结果没人用，可以删；`memref.store` 有 `Write` 效果，不能删。

这就是接口的价值：**框架不需要知道 op 的名字，只需要知道能力。**

## 示例二：用 one-shot-bufferize 观察 BufferizableOpInterface（实测）

先看一个能成功的 `linalg.add`：

```mlir
func.func @tensor_add(%a: tensor<4xf32>, %b: tensor<4xf32>) -> tensor<4xf32> {
  %init = tensor.empty() : tensor<4xf32>
  %0 = linalg.add ins(%a, %b : tensor<4xf32>, tensor<4xf32>)
                  outs(%init : tensor<4xf32>) -> tensor<4xf32>
  return %0 : tensor<4xf32>
}
```

命令：

```powershell
& "$mlir\mlir-opt.exe" .\linalg-add.mlir --one-shot-bufferize=bufferize-function-boundaries
```

实测输出：

```mlir
module {
  func.func @tensor_add(%arg0: memref<4xf32, strided<[?], offset: ?>>, %arg1: memref<4xf32, strided<[?], offset: ?>>) -> memref<4xf32> {
    %alloc = memref.alloc() {alignment = 64 : i64} : memref<4xf32>
    linalg.add ins(%arg0, %arg1 : memref<4xf32, strided<[?], offset: ?>>, memref<4xf32, strided<[?], offset: ?>>) outs(%alloc : memref<4xf32>)
    %cast = memref.cast %alloc : memref<4xf32> to memref<4xf32, strided<[?], offset: ?>>
    return %alloc : memref<4xf32>
  }
}
```

同一个 pass 作用在 `arith.addi` 的 tensor 形式上：

```mlir
func.func @tensor_addi(%a: tensor<4xi32>, %b: tensor<4xi32>) -> tensor<4xi32> {
  %0 = arith.addi %a, %b : tensor<4xi32>
  return %0 : tensor<4xi32>
}
```

实测失败：

```text
error: op was not bufferized
  %0 = arith.addi %a, %b : tensor<4xi32>
       ^
note: see current operation: %2 = "arith.addi"(%1, %0) <{overflowFlags = #arith.overflow<none>}> : (tensor<4xi32>, tensor<4xi32>) -> tensor<4xi32>
```

这不是 `arith` 方言“不支持 bufferization”，而是 **`arith.addi` 这个 op 没有挂 BufferizableOpInterface 的 external model**。同样的方言里，`arith.constant`、`arith.index_cast`、`arith.select` 是挂了的（前面 `BufferizableOpInterfaceImpl.cpp:213-215` 的三行）。tensor 级的逐元素算术在 MLIR 里通常由 `linalg` 承担，而不是 `arith`。

`--allow-unknown-ops` 可以让 pass 不报这个错，但它只是把问题推后：未知 op 会被保守地当成读写，真正的 bufferization 仍然没有发生。**这是诊断开关，不是修复手段。**

## 示例三：`mlir-opt --help` 里能看到什么（实测）

`--one-shot-bufferize` 的帮助直接暴露了它对接口的依赖：

```text
--one-shot-bufferize                                               -   One-Shot Bufferize
    --allow-return-allocs-from-loops                                 - Allows returning/yielding new allocations from a loop.
    --allow-unknown-ops                                              - Allows unknown (not bufferizable) ops in the input IR.
```

`allow-unknown-ops` 的措辞是 “not bufferizable ops”，而不是 “unknown dialect ops”。这两个概念不一样：一个 op 可以属于已注册的 dialect，但仍然没有实现 `BufferizableOpInterface`。`arith.addi` 就是后者。

## 示例四：给 builtin 类型挂 external model（guided）

这个例子需要自己编译一个最小 C++ 程序，本机具备条件（Ch 0 的 VS2022 + CMake 配方），但本章不把编译输出伪装成实测。代码本身是完整的，验证命令也给出；标 `guided` 是因为它依赖你本地的构建目录。

假设我们定义一个 `TypeInterface`，让类型报告自己的位宽：

```cpp
// BitWidthInterface.td（概念示意，实际项目里用 mlir-tblgen 生成）
def BitWidthInterface : TypeInterface<"BitWidthInterface"> {
  let cppNamespace = "::demo";
  let methods = [
    InterfaceMethod<"返回位宽", "unsigned", "getBitWidth", (ins)>
  ];
}
```

C++ 侧给 `IntegerType` 挂一个 external model：

```cpp
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/MLIRContext.h"
#include "llvm/Support/raw_ostream.h"

struct IntegerBitWidthModel
    : demo::BitWidthInterface::ExternalModel<IntegerBitWidthModel,
                                             mlir::IntegerType> {
  unsigned getBitWidth(mlir::Type type) const {
    return mlir::cast<mlir::IntegerType>(type).getWidth();
  }
};

int main() {
  mlir::MLIRContext ctx;
  mlir::IntegerType::attachInterface<IntegerBitWidthModel>(ctx);
  auto i32 = mlir::IntegerType::get(&ctx, 32);
  auto iface = mlir::dyn_cast<demo::BitWidthInterface>(i32);
  llvm::outs() << "bitwidth=" << iface.getBitWidth(i32) << "\n";
  return 0;
}
```

预期输出：

```text
bitwidth=32
```

验证思路：

```powershell
 # 用 Ch 0 的 CMake 配方建一个最小工程，链接 MLIRIR MLIRSupport
.\build\bitwidth-demo.exe
```

如果你把 `attachInterface` 那一行注释掉，`dyn_cast` 会返回空，随后调用 `getBitWidth` 会崩溃或断言。这个对照实验比读十遍文档更能说明“external model 必须注册”。

## 从 ODS 到 C++：接口方法是怎么长出来的

ODS 里的 `InterfaceMethod` 不是直接生成虚函数，而是生成三层东西：

| 生成物 | 作用 |
|---|---|
| `Concept` | 纯虚接口，框架通过它调用 |
| `Model<ConcreteOp>` | 把 `Concept` 的调用转发到 `ConcreteOp` 的方法 |
| `FallbackModel` / `ExternalModel` | 允许默认实现和外部实现 |

所以你在 C++ 里写的 `getBitWidth(Type)`、`getEffects(...)`，最终是通过 `Model` 的转发被调用的。接口对象本身很轻：它只保存“被包装的值 + `Concept*`”。这也是为什么 `dyn_cast<MemoryEffectOpInterface>(op)` 可以按值返回，不会有堆分配。

一个容易忽略的细节：**接口方法的 `this` 不一定是你以为的那个对象**。`ExternalModel` 的默认实现会把被包装的 `Type` / `Operation *` 作为参数传进来；如果你在方法体里用 `cast<ConcreteType>(...)`，要确认参数确实是那个具体类型。文档 `Interfaces.md:319-351` 专门解释了 `FallbackModel` 和 `ExternalModel` 的参数传递差异。

## 对比与取舍


| 方案 | 适合什么 | 代价 |
|---|---|---|
| Trait | 静态性质，编译期就能确定 | 不能跨定义添加，不能带运行期状态 |
| OpInterface | 框架对单个 op 的能力查询 | 需要 op 定义或 external model |
| TypeInterface / AttrInterface | 类型/属性的能力查询 | 需要类型/属性定义或 external model |
| DialectInterface | 整个 dialect 的钩子（注册、解析、打印） | 粒度粗，不能按 op 区分 |
| external model | 给别人的 op/type/attr 补能力 | 必须显式注册到 context，容易漏 |

选择顺序可以简单记成：

1. 这个性质是 op 定义的一部分、永远不变？用 trait。
2. 这个能力会被多个框架查询、需要按 op 分派？用 interface。
3. 你拥有接口，但不拥有对象（例如给 builtin 类型补能力）？用 external model。
4. 你要挂钩整个方言的生命周期？用 dialect interface（Ch 16）。

## 踩雷集锦

1. **以为“方言实现了接口”等于“方言里所有 op 都实现了”。** `arith` 注册了 bufferization external model，但只给三个 op 挂了；`arith.addi` 的 tensor 形式照样失败。

2. **忘记注册 external model。** 症状是 `dyn_cast` 得到空，或者 `declarePromisedInterface` 触发运行期错误。注册是按 context 的，测试里新建 context 就要重新挂。

3. **对可能为空的接口用 `cast`。** `cast<MemoryEffectOpInterface>(op)` 在 op 没实现时直接断言；先用 `dyn_cast` 判断。

4. **把 `hasNoEffect()` 当成“可以随便删”。** 它只说明没有内存效果，不说明没有 UB、没有符号引用、没有 region 里的隐藏操作。真正删除前还要看 use、trait 和 op 语义。

5. **把 `Pure` 当成 `NoMemoryEffect` 的同义词。** `Pure = AlwaysSpeculatable + NoMemoryEffect`（`SideEffectInterfaces.td:147-149`）；一个 `NoMemoryEffect` 但不可推测的 op 不能随意 hoist。

6. **实现 BufferizableOpInterface 时“乐观”返回 false。** `bufferizesToMemoryWrite`、`getAliasingOpOperands` 必须保守；漏报 alias 会生成错误的原地写。

7. **用 `--allow-unknown-ops` 掩盖 bufferization 失败。** 它不会让 op 变得可 bufferize，只会让 pass 继续跑，问题留到后面。

## 进阶：再往深一层

- **读生成物**：ODS 会为每个接口生成 `*Interface.h.inc` / `*Interface.cpp.inc`，里面有 `Concept`、`Model`、`FallbackModel` 的实际代码。Ch 13 会逐段读。
- **接口继承**：`ElementsAttrInterface` 继承 `TypedAttrInterface`；声明接口时可以传父接口列表。给对象添加子接口时，父接口也会隐式添加，所以 `dyn_cast<TypedAttrInterface>` 同样成功；反过来，只有父接口实现不代表子接口可用（`Interfaces.md:754-792`）。
- **Dialect fallback**：`OpInterface::getInterfaceFor` 在 op 没注册或没实现时会问 dialect；这给“开放方言”（例如允许未知 op 的方言）留了口子。细节在 `Interfaces.md:414-450`。
- **`declarePromisedInterface`**：跨方言扩展的“欠条”。它把“必须有人实现”变成运行期可检查的约束，适合大型项目。
- **给 builtin 类型挂接口**：`IntegerType::attachInterface<MyModel>(context)` 是合法用法；文档建议只在你“拥有接口”时这么做，避免两个项目给同一个类型挂出冲突实现。

## 调试接口问题的顺序

接口相关的 bug 往往表现为“框架什么也没做”或者“运行期空指针”，而不是编译错误。可以按这个顺序查：

1. **确认 op 是否已注册。** 未注册 op 的接口查询会走 dialect fallback；如果你的方言没有 fallback，结果就是空。
2. **确认接口是否在该 op 的 trait/interface 列表里。** 打开 `.td`，搜 `DeclareOpInterfaceMethods<YourInterface>` 或 `YourInterface`。
3. **确认 external model 是否注册。** 搜 `registerYourInterfaceExternalModels`，看它有没有在 `DialectRegistry` 的 extension 里被调用。
4. **确认 context 是同一个。** `attachInterface` 只影响传入的那个 `MLIRContext`；测试里 `MLIRContext ctx;` 新建的 context 需要重新挂。
5. **确认查询方式。** `dyn_cast` 失败是正常的“没实现”；`cast` 失败才是“我以为它实现了”。先把 `cast` 改成 `dyn_cast`，打印 `op->getName()`，通常就能定位。

如果开了 assertions，`declarePromisedInterface` 会在第 3 步给你一个明确的 “promised but never implemented” 错误；Optimized build 没有断言，所以第 3 步要自己查。

## 接口设计的取舍：什么时候不该用接口

接口不是越多越好。下面这些情况，用普通函数或 trait 反而更清楚：

- **只有一个方言、一个 pass 用。** 直接写一个 `getFoo(MyOp)` 工具函数，比定义一个只有你能实现的接口便宜。
- **方法语义依赖大量上下文。** 接口方法签名里塞进五六个参数、还要传 analysis state，往往说明它不该是“能力查询”，而该是某个框架的专用钩子。
- **返回 `bool` 但语义容易误解。** `bufferizesToMemoryRead` 这种名字已经算清楚；如果你的接口方法叫 `isSimple()`，框架作者迟早会理解错。把问题拆成可验证的具体查询。
- **需要版本兼容。** 接口是 ABI 的一部分；给已发布接口加纯虚方法会破坏下游实现。ODS 的 `InterfaceMethod` 可以给默认实现，但默认值必须对所有实现都安全。
- **性能极热。** 每次查询都是一次 `TypeID` 查表；如果在最内层循环里对每个 operand 查接口，先测量，再决定是否缓存。

反过来，下面这些信号说明你**应该**用接口：

- 同一个问题会被多个 pass 问（“这个 op 有没有副作用”“这个 op 能不能 bufferize”）；
- 实现者可能不在你的方言里（external model 的典型场景）；
- 你希望下游在不改你代码的情况下扩展行为。

## 动手练习


1. **读源码**：打开 `mlir/include/mlir/Interfaces/SideEffectInterfaces.td`，找出 `Pure`、`NoMemoryEffect`、`RecursiveMemoryEffects` 的定义，解释三者的区别。

2. **查接口**：在 `LinalgStructuredOps.td` 里找出 `linalg.generic` 继承的接口列表，逐个说明它为什么需要 `DestinationStyleOpInterface` 和 `RecursiveMemoryEffects`。

3. **做对照**：把 `arith.addi` 的 tensor 例子改成 `linalg.add`，确认 `one-shot-bufferize` 成功；再改回 `arith.addi`，确认失败信息。

4. **观察 effects**：把 `effects.mlir` 里的 `memref.store` 换成 `memref.load`（结果未使用），跑 `--canonicalize`，解释为什么 load 也不一定被删。

5. **找 external model**：在源码里搜索 `registerBufferizableOpInterfaceExternalModels`，找出至少三个方言的注册函数，列出它们各自挂了哪些 op。

6. **设计接口**：假设你要写一个 `CostModelOpInterface`，让 op 报告自己的估算代价。写出 ODS 声明和一个 external model 的骨架（不需要编译）。

7. **做一次“忘注册”实验**：把本章示例四的 `attachInterface` 注释掉，重新编译运行，记录 `dyn_cast` 的返回值和程序行为，解释为什么它没有在编译期报错。

## 本章重点整理


- 接口是运行期的能力查询，trait 是编译期的静态属性；两者解决不同问题。
- `OpInterface`、`TypeInterface`、`AttributeInterface` 共用 `detail::Interface` 的 concept/model 机制。
- external model 让你在不改对象定义的前提下补接口；它按 `MLIRContext` 注册，不是全局。
- `MemoryEffectOpInterface` 的 `getEffects` 决定 DCE、hoist、sink 等框架行为；`Pure` 比 `NoMemoryEffect` 多一个“可推测”。
- `BufferizableOpInterface` 是 one-shot bufferize 的合同；实现必须保守，且是**按 op**而不是按方言。
- `arith.addi` 的 tensor 形式不能 bufferize，`linalg.add` 可以；这是接口列表差异，不是方言优劣。
- `--allow-unknown-ops` 是诊断开关，不是修复。

## 自我检核

- [ ] 我能说出 trait 和 interface 的三个区别
- [ ] 我能解释 `dyn_cast<MemoryEffectOpInterface>(op)` 为空意味着什么
- [ ] 我知道 external model 为什么要按 context 注册
- [ ] 我能说出 `Pure` 和 `NoMemoryEffect` 的差异
- [ ] 我能解释 `linalg.add` 为什么能 bufferize，而 `arith.addi` 的 tensor 形式不能
- [ ] 我知道 `--allow-unknown-ops` 不会修复 bufferization
- [ ] 我能指出 `OpInterface::getInterfaceFor` 的三步查找顺序
- [ ] 我能说出 `declarePromisedInterface` 解决什么问题

## 延伸阅读

- **[MLIR Interfaces 文档](https://mlir.llvm.org/docs/Interfaces/)**
  - **读哪里**：`External Models for Attribute, Operation and Type Interfaces` 与 `Dialect Fallback for OpInterface` 两节。
  - **学什么**：external model 的注册规则、fallback、promised interface。
  - **和本章的关联**：本章的“按 context 注册”和“忘了注册”都来自这里。

- **[mlir/include/mlir/Support/InterfaceSupport.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Support/InterfaceSupport.h)**
  - **读哪里**：`detail::Interface`、`InterfaceMap`、`insertModel`。
  - **学什么**：concept-based polymorphism 的真实实现。
  - **和本章的关联**：解释“查表”到底查的是什么。

- **[SideEffectInterfaces.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Interfaces/SideEffectInterfaces.td)**
  - **读哪里**：`MemoryEffectsOpInterface`、`Pure`、`RecursiveMemoryEffects`。
  - **学什么**：效果模型和 trait 的组合方式。
  - **和本章的关联**：示例一的 DCE 行为。

- **[BufferizableOpInterface.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td)**
  - **读哪里**：接口描述和 `bufferize`、`getAliasingOpOperands` 的方法注释。
  - **学什么**：bufferization 的合同与保守性要求。
  - **和本章的关联**：示例二的成功/失败对照。

- **[LinalgStructuredOps.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td)**
  - **读哪里**：`LinalgStructuredBase_Op` 的 trait 列表。
  - **学什么**：一个真实方言如何组合多个接口。
  - **和本章的关联**：把“接口列表”与“实际能力”对上。

---

下一章换一个角度：接口告诉你“能做什么”，但真正改 IR 时，顺序、use-list、listener 才是 bug 的来源。

→ [Ch 9 安全地修改 IR](./09-modifying-ir-safely.md)