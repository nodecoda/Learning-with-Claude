# Ch 15 Trait 与 Interface 实战

> **目标**：把"给 op 加能力"这件事拆成两种完全不同的机制——Trait 是编译期混入的 C++ 基类，Interface 是运行期可查询的模型表。学完你要能判断一个需求该用哪种，并能读懂 `Pure` 这种"看起来是一个词、实际展开成四个 trait"的写法。

## 为什么需要这个？

写方言时你会反复遇到同一类需求：

- "这个 op 没有副作用，死代码可以删。"
- "这个 op 的结果类型和操作数一样，别让我手写。"
- "这个 op 的结果类型能从操作数推出来。"
- "这个 op 会读内存，别把它提到循环外。"

四个需求看起来都是"给 op 加个标记"，但它们在框架里的实现方式完全不同。前两个是 **Trait**：编译期就把一个基类混入 `Op<...>`，调用点直接是一个静态函数。后两个是 **Interface**：op 在 `AbstractOperation` 里挂一张 interface map，框架在运行期 `dyn_cast` 查表。

分不清这两者，会在两个地方卡住：一是 ODS 里该写 `[Pure]` 还是 `DeclareOpInterfaceMethods<...>` 搞错，二是读生成物时把 `MemoryEffectOpInterface::Trait` 误认为"这是一个 trait 声明"。实际上 `Pure` 展开后**就包含** `MemoryEffectOpInterface::Trait`。

## 先建立直觉：一个 op 的能力从哪来

我们的 `toy.add` 在 ODS 里只写了两个词：

```tablegen
def Toy_AddOp : Toy_Op<"add", [Pure, SameOperandsAndResultType]> {
  let arguments = (ins F32:$lhs, F32:$rhs);
  let results = (outs F32:$result);
  let assemblyFormat = "$lhs `,` $rhs attr-dict `:` type($lhs)";
}
```

生成出来的类声明（本机 `gen/toy/ToyOps.h.inc:132`，为可读性换行）：

```cpp
class AddOp : public ::mlir::Op<
    AddOp, ::mlir::OpTrait::ZeroRegions, ::mlir::OpTrait::OneResult,
    ::mlir::OpTrait::OneTypedResult<::mlir::FloatType>::Impl,
    ::mlir::OpTrait::ZeroSuccessors, ::mlir::OpTrait::NOperands<2>::Impl,
    ::mlir::OpTrait::OpInvariants,
    ::mlir::ConditionallySpeculatable::Trait,
    ::mlir::OpTrait::AlwaysSpeculatableImplTrait,
    ::mlir::MemoryEffectOpInterface::Trait,
    ::mlir::OpTrait::SameOperandsAndResultType,
    ::mlir::InferTypeOpInterface::Trait> {
```

两个词变成了 **九个 trait 参数**。这就是第一个心智模型：

```text
你在 ODS 里写的        →  生成物里的基类列表
Pure                  →  ConditionallySpeculatable + AlwaysSpeculatable + MemoryEffectOpInterface
SameOperandsAndResultType →  OpTrait::SameOperandsAndResultType + InferTypeOpInterface
Toy_Op<...>           →  ZeroRegions / OneResult / NOperands 等结构 trait
```

`Pure` 的定义在 `include/mlir/Interfaces/SideEffectInterfaces.td:147`，只有一行：

```tablegen
def Pure : TraitList<[AlwaysSpeculatable, NoMemoryEffect]>;
```

`TraitList` 是"把多个 trait 打包成一个名字"，所以它不是继承关系，而是展开。理解这一点，后面所有"为什么我的 op 明明写了 Pure 还实现了 MemoryEffectOpInterface"的疑惑都会消失。

## 核心概念一：Trait 是编译期的事

Trait 的本质是 **CRTP 基类**。`Op<AddOp, ..., OpTrait::SameOperandsAndResultType, ...>` 让 `AddOp` 继承一个模板基类，这个基类里可能有：

- 静态断言（检查 trait 之间是否矛盾）
- 成员函数（`getOperand()` 之类的快捷方式）
- `verifyTrait` 钩子，在 op 验证时被调用
- 对 interface 的静态实现（`SameOperandsAndResultType` 就是这样给出 `inferReturnTypes` 的默认实现）

因为发生在编译期，Trait 有三个特征：

1. **零运行时开销**。`op.hasTrait<X>()` 在生成代码里是常量折叠。
2. **不能给"别人的 op"加**。你不能给 `arith.addi` 追加一个 trait，除非改上游。
3. **组合在类声明里固定**。同一类 op 的所有实例共享同一套 trait，不能按实例切换。

`SameOperandsAndResultType` 在 `include/mlir/IR/OpDefinition.h:1112`，它同时提供了：

- 验证：所有 operand 和 result 类型必须相同；
- 推断：`inferReturnTypes` 直接返回第一个 operand 的类型；
- 一个 `InferTypeOpInterface::Trait` 的注入。

所以 `toy.add` 不需要你写一行 `inferReturnTypes`，但它在框架眼里**确实实现了 `InferTypeOpInterface`**。这是最容易被忽略的"trait 带来 interface"的例子。

## 核心概念二：Interface 是运行期的事

Interface 解决的是 Trait 解决不了的问题：**框架要在不知道具体 op 类型的情况下调用它**。

`canonicalize` 的作者不可能 include 你的 `toy` 头文件。他写的代码是：

```cpp
if (auto effectInterface = dyn_cast<MemoryEffectOpInterface>(op)) { ... }
```

这一句要能在任何 op 上工作。实现方式是：每个 op 在 context 里有一个 `AbstractOperation`，里面挂着一张 interface map（`TypeID -> 模型函数指针`）。`dyn_cast` 查表，命中就返回一个轻量 wrapper，wrapper 的成员函数转调你实现的静态方法。

由此得到 Interface 的三个特征：

1. **有运行期查表成本**，虽然很低（一次 hash / 线性小表查找）。
2. **可以外挂**。用 `attachInterface` + external model 给已有 op 补接口，不用改它的源码。
3. **接口是"契约"，实现可以是任意 op**。框架只依赖 `InferTypeOpInterface` 这个名字和它的签名，不依赖 `toy`。

`InferTypeOpInterface` 的定义在 `include/mlir/Interfaces/InferTypeOpInterface.td:28`，核心方法只有一个：

```cpp
static LogicalResult inferReturnTypes(MLIRContext *context,
                                      std::optional<Location> location,
                                      ValueRange operands, DictionaryAttr attributes,
                                      PropertyRef properties, RegionRange regions,
                                      SmallVectorImpl<Type> &inferredReturnTypes);
```

`PropertyRef` 这个参数是 MLIR 23 引入的（properties 迁移），写实现时签名必须一模一样，否则编译报"没有匹配的声明"。

## 核心概念三：为什么接口能避免"框架认识所有 op"

这个问题值得单独说，因为它是 MLIR 可扩展性的根。

假设没有 interface，`canonicalize` 想删死代码，它必须知道每个 op 有没有副作用。三种可能的做法：

- 在 pass 里写 `if (isa<arith::AddIOp>(op) || isa<arith::MulIOp>(op) || ...)`：每加一个方言就要改一遍 pass。
- 让每个 op 提供一个 `bool hasSideEffects()` 虚函数：所有 op 都继承同一个基类，MLIR 的 op 是值语义的，做不到。
- 让 op 通过一个**独立的、可查询的表**声明能力。

MLIR 选第三种。`SideEffectInterfaces.td` 里的 `MemoryEffects<[...]>` 会生成 `getEffects`，同时把 `MemoryEffectOpInterface::Trait` 挂进类声明。框架侧只写：

```cpp
if (auto memInterface = dyn_cast<MemoryEffectOpInterface>(op)) {
  memInterface.getEffects(effects);
  ...
}
```

于是：

- 新方言不需要改 `canonicalize`；
- `canonicalize` 不需要 include 新方言；
- 没有实现接口的 op 被保守地当作"有未知副作用"，不会被误删。

**保守默认值**是这套设计的关键。`mlir::hasUnknownEffects(op)` 的定义就是"既没有 `MemoryEffectOpInterface`，也没有 `HasRecursiveMemoryEffects`"。框架遇到这种 op 一律不动。

## 底层机制：死代码判定到底看什么

我们实测一下 `--canonicalize` 对四个函数做了什么。输入（`test/canon.mlir`）：

```mlir
func.func @dead_pure(%x: f32, %y: f32) {
  %dead = toy.add %x, %y : f32
  return
}

func.func @dead_load(%p: !toy.pointer<f32>) {
  %dead = toy.load %p : !toy.pointer<f32> -> f32
  return
}

func.func @dead_alloc_store() {
  %p = toy.alloc : !toy.pointer<f32>
  %c = arith.constant 1.000000e+00 : f32
  toy.store %c, %p : f32, !toy.pointer<f32>
  return
}

func.func @double_cast(%x: f32) -> f32 {
  %a = toy.cast %x : f32 to f64
  %b = toy.cast %a : f64 to f32
  return %b : f32
}
```

```powershell
& "...\toy-opt.exe" .\test\canon.mlir --canonicalize
```

实测输出：

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

四个结果，四种机制：

| 函数 | 结果 | 原因 |
| --- | --- | --- |
| `dead_pure` | `toy.add` 被删 | `Pure` → 无内存效果，且结果未被使用 |
| `dead_load` | `toy.load` 被删 | 只有 `MemRead` 效果，结果未被使用 |
| `dead_alloc_store` | 三个 op 全保留 | `toy.store` 有 `MemWrite` |
| `double_cast` | 嵌套 `toy.cast` 被折叠 | 自己写的 canonicalization pattern |

第二行最容易让人意外：`toy.load` 明明"读了内存"，怎么还能删？答案是 `wouldOpBeTriviallyDeadImpl` 的规则（`lib/Interfaces/SideEffectInterfaces.cpp:43` 起）：

```cpp
// Check to see if this op either has no effects, or only allocates/reads
// memory.
...
if (!llvm::all_of(effects, [&allocResults](const MemoryEffectInstance &it) {
      if (allocResults.contains(it.getValue()))
        return true;
      // Otherwise, the effect must be a read.
      return isa<MemoryEffects::Read>(it.getEffect());
    })) {
  return false;
}
```

规则是：**只有读/分配效果、且结果没人用的 op，视为可删**。因为删掉它不会改变任何可观察状态。写效果（`MemWrite`）不在此列，所以 `toy.store` 保住了，连带把它的操作数 `toy.alloc` 也保住了——`alloc` 自己的结果还被 store 用着。

这就是为什么给 op 声明**准确的** memory effects 很重要：多声明一个 `MemWrite`，你的 op 就永远不会被 DCE，性能下降；少声明一个，`canonicalize` 可能把有副作用的 op 删掉，程序语义直接错。

`toy.store` 的 effects 是 ODS 生成的（本机 `ToyOps.cpp.inc:1378`）：

```cpp
void StoreOp::getEffects(SmallVectorImpl<SideEffects::EffectInstance<MemoryEffects::Effect>> &effects) {
  effects.emplace_back(MemoryEffects::Write::get(), 0, false, SideEffects::DefaultResource::get());
}
```

四个参数依次是：效果种类、作用在哪个 operand（`0` = 第一个操作数，这里故意不绑定具体值）、是否只在 stage 阶段有效、资源。**不绑定具体 operand** 意味着"写的是默认资源"，框架无法据此做更细的别名分析，但对教学方言够用。真实项目里应该绑到 `$ptr` 上，别名分析才能区分"写 A"和"写 B"。

## 底层机制：验证顺序决定了报错的样子

理解了 trait 和 interface，还要知道它们**什么时候**被调用。一个 op 在 `verify()` 里依次经过：

1. **结构 trait 的检查**：region 数量、result 数量、operand 数量、terminator 位置。这些来自 `Op<...>` 里的固定 trait，失败信息像 `expected 1 region`。
2. **各 trait 的 `verifyTrait`**：`SameOperandsAndResultType` 在这一步比对类型；`InferTypeOpInterface` 的 verify trait 也在这里跑。
3. **ODS 约束**：`F32:$lhs` 这类类型约束，失败信息带 constraint 名字。
4. **op 自己的 `verify()`**：你手写的那段，`hasVerifier = 1` 才有。

这个顺序解释了两件事：

- **为什么接口的报错比手写 verifier 早**。`InferTypeOpInterface` 在第 2 步，你的 `StoreOp::verify()` 在第 4 步。想让自定义检查先跑是不可能的。
- **为什么有些错误信息"不像我写的"**。第 1、3 步的文本由 tablegen 生成，风格统一但不可定制。

顺带一个实践建议：**能在 ODS 约束里表达的检查，不要挪到 C++ verifier**。约束在第 3 步，错误信息带 source location 和约束名；C++ verifier 只能 `emitOpError`，位置是整个 op。诊断质量差一截。

## 示例一：接口派发是怎么被调用的

我写了一个 30 行的 pass `-toy-infer-types`，它对每个 op 只做一件事：`dyn_cast<InferTypeOpInterface>`，然后调 `inferReturnTypes`。

```cpp
module.walk([&](Operation *op) {
  auto infer = dyn_cast<InferTypeOpInterface>(op);
  if (!infer)
    return;
  SmallVector<Type> inferred;
  if (failed(infer.inferReturnTypes(
          op->getContext(), op->getLoc(), op->getOperands(),
          op->getAttrDictionary(), op->getPropertiesStorage(),
          op->getRegions(), inferred)))
    return;
  op->emitRemark(...);
});
```

注意这个 pass 里**没有任何 `toy` 的类型判断**。它不知道 `toy.cast` 存在，只知道"某个 op 实现了推断接口"。

实测：

```powershell
& "...\toy-opt.exe" .\test\infer.mlir --toy-infer-types
```

```text
remark: interface dispatch: toy.cast infers result type(s) f64
  %a = toy.cast %x : f32 to f64
       ^
remark: interface dispatch: toy.cast infers result type(s) tensor<2x3xf32>
  %b = toy.cast %t : tensor<2x3xf64> to tensor<2x3xf32>
       ^
```

同时 `toy.add` 也会被这个 pass 命中——因为它通过 `SameOperandsAndResultType` 拿到了 `InferTypeOpInterface::Trait`。这是"trait 带来 interface"最直观的一次观察：你在 `.td` 里只写了 `SameOperandsAndResultType`，框架却把它当 `InferTypeOpInterface` 用。

## 示例二：接口自带验证

`InferTypeOpInterface` 不只是一个"可以被调用"的方法，它还挂了一个 `verifyTrait`：每次 op 验证时，框架会拿推断结果和你声明的结果类型比对。`mlir::detail::verifyInferredResultTypes` 在 `lib/Interfaces/InferTypeOpInterface.cpp:246`。

于是这个输入：

```mlir
func.func @bad(%x: f32) {
  %a = toy.cast %x : f32 to i32
  return
}
```

在解析阶段就被拒了：

```text
error: 'toy.cast' op inferred type(s) 'f64' are incompatible with return type(s) of operation 'i32'
error: 'toy.cast' op failed to infer returned types
note: see current operation: %0 = "toy.cast"(%arg0) : (f32) -> i32
```

这解释了一个常见困惑："我明明想写个 pass 去修正类型，为什么 pass 根本没跑？"因为 **IR 在进 pass 之前必须先通过验证**，而 `InferTypeOpInterface` 把"声明类型必须等于推断类型"变成了验证的一部分。想用推断结果，只能在 builder 里用（构造时不写结果类型），不能靠 pass 事后修。

`toy.cast` 的实现里有一个刻意的选择：推断失败时用 `emitOptionalError(location, ...)` 而不是 `emitError`，因为 `location` 可能是 `std::nullopt`（builder 路径下没有位置）。写成 `emitError(*location)` 会在没有位置时崩掉。

## 示例三：同一份 `.td`，两种声明风格

`MemoryEffectOpInterface` 和 `InferTypeOpInterface` 在 ODS 里的写法不一样，这个差别值得记住：

```tablegen
// 效果：直接给一个 trait 列表，框架生成 getEffects
def Toy_LoadOp : Toy_Op<"load", [MemoryEffects<[MemRead]>]> { ... }

// 推断：用 DeclareOpInterfaceMethods，只生成声明，实现你自己写
def Toy_CastOp : Toy_Op<"cast", [Pure, DeclareOpInterfaceMethods<InferTypeOpInterface>]> { ... }
```

- `MemoryEffects<[MemRead]>` 是**配置式**的：你描述效果，tblgen 生成 `getEffects` 的函数体。
- `DeclareOpInterfaceMethods<X>` 是**委托式**的：tblgen 只生成 `static ... inferReturnTypes(...)` 的声明，函数体你写。

用错了会怎样？把 `DeclareOpInterfaceMethods<InferTypeOpInterface>` 写成光秃秃的 `InferTypeOpInterface`，tblgen 会认为你要用接口的默认实现，而它没有默认实现，链接期报 `unresolved external symbol`。反之，在只需要声明效果的地方手写 `getEffects`，你会和生成物打架（重复定义）。

## 对比与取舍

| 维度 | Trait | Interface |
| --- | --- | --- |
| 生效时机 | 编译期（CRTP 基类） | 运行期（interface map 查表） |
| 能否给别人的 op 加 | 不能 | 能（external model） |
| 能否按实例切换 | 不能 | 不能（按 op 种类） |
| 框架如何发现 | 不需要发现，代码里直接用 | `dyn_cast<XOpInterface>(op)` |
| 典型用途 | 结构约束、静态验证、零开销访问器 | 跨方言能力、pass 与方言解耦 |
| 代价 | 类声明变长、模板实例化变多 | 一次查表、接口实现要写全签名 |

三条经验：

- **"这个 op 是什么"用 Trait**。单结果、零区域、terminator、父 op 限制。
- **"这个 op 能做什么"用 Interface**。能推断、能折叠、能读内存、能被内联。
- **两者会互相转换**。`Pure` 是 Trait，但它带来的 `MemoryEffectOpInterface` 是 Interface。设计时看的是"谁能用"，不是"我写了哪个词"。

一个反直觉的取舍：不要为了"让框架认识我的 op"而发明新接口。接口的价值在于**有第二个使用者**。如果只有你的 pass 用，直接 `isa<MyOp>` 更简单、更快、更好读。

## 踩雷集锦

1. **以为 `Pure` 只是一个 trait**。它展开成三个，其中包括 `MemoryEffectOpInterface`。看生成物时别被吓到。
2. **给会写内存的 op 加了 `Pure`**。`canonicalize` 会把未使用的 store 删掉，程序静默错误。`Pure` 的语义是"无副作用且可推测执行"，不是"我觉得它很纯"。
3. **`DeclareOpInterfaceMethods<InferTypeOpInterface>` 但忘了实现**。链接期才报错，而且错误信息是一长串 mangled name，很难认。
4. **`inferReturnTypes` 签名少了 `PropertyRef`**。MLIR 23 的签名带 properties，少一个参数就是"没有匹配的声明"，编译错误指向生成的头文件，不在你的 `.cpp`。
5. **以为 `MemRead` 能阻止 DCE**。只有读效果的 op 在结果未使用时会被删（`SideEffectInterfaces.cpp:72`）。
6. **在 `getEffects` 里不绑定 operand**。效果落到 `DefaultResource`，别名分析退化成"全局写"，性能损失但不报错。
7. **用 pass 去"修正"推断出的类型**。`InferTypeOpInterface` 的 verify trait 在 pass 之前就跑完了，坏的声明类型根本进不了 pipeline。

## 进阶：再往深一层

- **`HasRecursiveMemoryEffects`**。容器类 op（比如 `scf.if`）自己不声明效果，而是声明"我的效果来自 region 里的 op"。`isMemoryEffectFree` 会递归进去看（`SideEffectInterfaces.cpp:335`）。
- **External model**。`attachInterface<MyModel>(context)` 能给已有 op 挂接口，Ch 8 有完整例子。代价是模型必须和 op 的语义严格一致，否则框架会在错误的地方调用它。
- **`ConditionallySpeculatable`**。`Pure` 里包含它，意味着"可以安全地提前执行"。如果你的 op 在除零时会 trap，就不能用 `Pure`，要用自定义的 `getSpeculatability` 返回 `NotSpeculatable`。
- **interface 的版本兼容**。接口方法签名变了是 ABI 破坏，下游要重新编译。这也是 MLIR 生态里 out-of-tree 方言要跟版本走的原因之一。

## 动手练习

1. **给 `toy.load` 去掉 `MemoryEffects`**，重跑 `--canonicalize`，解释为什么 `dead_load` 不再被删。
2. **给 `toy.store` 加 `Pure`**，观察 `dead_alloc_store` 的输出变化，并说明这是不是一个 bug。
3. **写一个新 op `toy.neg`**，要求它既能被 `-toy-infer-types` 命中，又能被 DCE 删除。最少要写哪几个 trait？
4. **把 `toy.cast` 的 `DeclareOpInterfaceMethods` 去掉**，改成手写 `InferTypeOpInterface::Trait` 的模型注册，比较两种写法的代码量。
5. **读生成物**：在 `ToyOps.h.inc` 里找到 `AddOp` 的基类列表，数一数 `Pure` 贡献了几个。
6. **验证保守默认**：写一个不声明任何效果的 op，跑 `--canonicalize`，确认它不会被删。

## 本章重点整理

- Trait = 编译期 CRTP 基类；Interface = 运行期 interface map。
- `Pure` 展开成 `AlwaysSpeculatable` + `NoMemoryEffect`，后者再带来 `MemoryEffectOpInterface`。
- `SameOperandsAndResultType` 同时给验证、推断和 `InferTypeOpInterface`。
- DCE 规则：无效果，或"只读/只分配且结果未被使用"，才可删。
- 框架与方言解耦靠 interface + 保守默认值，不靠中心化的 op 列表。
- `InferTypeOpInterface` 自带 verify trait，坏类型进不了 pass。
- 接口的签名（含 `PropertyRef`）必须逐字匹配。

## 自我检核

- [ ] 能说出 `Pure` 展开后的三个 trait，以及它们各自的作用
- [ ] 能解释为什么 `toy.load`（MemRead）会被 DCE 而 `toy.store`（MemWrite）不会
- [ ] 知道 `DeclareOpInterfaceMethods` 与直接写接口名的区别
- [ ] 能举一个"只有 Trait 能做到、Interface 做不到"的例子，以及反过来
- [ ] 能解释 `inferReturnTypes` 的 `PropertyRef` 参数从哪来
- [ ] 遇到"pass 没跑到"时，会先怀疑 verifier 而不是 pass manager
- [ ] 能说出 external model 解决的具体问题

## 延伸阅读

- **`mlir/docs/Interfaces.md`**：读开头到 "Interface Models" 一节；学接口的注册与查询模型；对应本章"运行期查表"。
- **`mlir/include/mlir/Interfaces/SideEffectInterfaces.td`**：读 100–160 行；学 `Pure` / `MemoryEffects` 的声明方式；是本章示例三的依据。
- **`mlir/lib/Interfaces/SideEffectInterfaces.cpp`**：读 43–105 行；学 DCE 的精确规则；能解释本章 `dead_load` 的结果。
- **`mlir/include/mlir/IR/OpDefinition.h`**：读 1100–1160 行；学 `SameOperandsAndResultType` 的实现；理解 trait 如何注入 interface。
- **`mlir/lib/Interfaces/InferTypeOpInterface.cpp`**：读 240–270 行；学推断验证与 fatal error 的边界；对应示例二。

→ [Ch 16 Dialect 注册、bytecode 与兼容性](./16-dialect-registration.md)
