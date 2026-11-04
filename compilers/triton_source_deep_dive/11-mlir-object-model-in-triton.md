# Ch 11 — MLIR 对象模型在 Triton 中的落点

> 读 MLIR pass 的难点不是算法，而是分清“谁拥有谁、什么时候能改、改完谁失效”。

## 目标

读完本章，你应该能：

- 用 Triton 的真实代码说清楚 `MLIRContext`、`ModuleOp`、`Operation`、`Region`、`Block`、`Value`、`Attribute`、`Type` 的关系；
- 看懂 `RemoveLayoutConversions.cpp` 里 `walk`、`clone`、`replaceAllUsesWith`、`erase` 的写法；
- 解释为什么不能在 `walk` 里随手 `erase`，以及“先收集、再重写”的两阶段模式；
- 用 `IRMapping` 克隆 loop body，并正确映射 block argument 和 loop result；
- 用 `OpBuilder::setInsertionPoint` 在正确位置创建 `arith.constant`；
- 区分 `dyn_cast`/`isa`/`cast`，知道什么时候会返回 null、什么时候会 assert；
- 说清楚 `TritonGPUToLLVMTypeConverter` 如何把 tile 级 tensor 变成每线程的 struct。

本章的主线是 `lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp` 和 `lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp`：前者是“手写 walk/rewrite”，后者是“类型转换 + dialect conversion”。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 `3.9.0`。
- 机器：Tesla T4（`sm_75`），驱动 535.216.01，CUDA 12.4，系统 Python 3.10.12。
- 本章以源码阅读为主；C++ 片段都来自 3.9.0 源码，标 `源码依据`。没有编译/运行自定义 MLIR pass，所以代码示例标 `guided`。
- 本机有 3.6.0 的 `triton-opt`，但本章不依赖它；MLIR 对象模型在 3.6/3.9 之间没有本质变化，行号以 3.9.0 为准。

**命名约定**：`RemoveLayoutConversions.cpp` 指 `lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp`，`TypeConverter.cpp` 指 `lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp`，`Ops.cpp` 指 `lib/Dialect/Triton/IR/Ops.cpp`。

## 为什么需要这个

Triton 的 pass 大致分两类：

1. **手写遍历 + 手写重写**：例如 `RemoveLayoutConversions`，先做 layout 分析，再按 dominance 顺序重写 IR。
2. **Pattern/dialect conversion**：例如 `CanonicalizeMaskedLoadPattern`、`TritonGPUToLLVM`，把“匹配 + 替换”交给框架。

两类都绕不开同一组问题：

- `Operation` 被 `erase` 后，指向它的 `Value`、`Block*`、迭代器还能不能用？
- `replaceAllUsesWith` 之后，旧 op 还在不在？要不要手动 `erase`？
- 克隆一个带 region 的 op，block argument 怎么映射？
- `setInsertionPoint` 设到 op 前面还是后面？插到错误 region 会怎样？
- `dyn_cast` 失败返回什么？`cast` 失败会怎样？

这些问题不解决，pass 会在“看起来只是偶尔崩”的地方崩。MLIR 的对象模型就是为了让这些规则可推理。

## 先建立直觉

把一段 MLIR 想成一棵树 + 一张 use-def 图：

```text
ModuleOp
  └─ Region
       └─ Block
            ├─ BlockArgument (%arg0, %arg1, ...)
            └─ Operation (%0 = tt.load ..., tt.store ..., tt.return)
                 ├─ Operands: Value...
                 ├─ Results:  Value...
                 └─ Regions (scf.for / tt.reduce / tt.func ...)
```

- **树**：`ModuleOp -> Region -> Block -> Operation -> Region -> ...`。`Operation` 拥有它的 `Region`，`Region` 拥有 `Block`，`Block` 拥有 `Operation`。
- **图**：每个 `Value` 要么是 `BlockArgument`，要么是某个 `Operation` 的 `OpResult`；每个 `Value` 有一串 uses。`replaceAllUsesWith` 改的是这张图，不是树。
- **SSA**：一个 `Value` 只定义一次；使用它的 op 必须能被它的定义支配（dominance）。

`RemoveLayoutConversions` 的算法注释把这点说得很直白：

```text
1. Find all the anchor ops.
2. Propagate layout to descendants.
3. Resolve conflicts.
4. Rewrite the IR by walking the function in dominance order.
```

**源码依据**：`lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp:44-61`。

注意第 4 步：**按 dominance 顺序重写**。这不是审美选择，而是 SSA 正确性的要求：新值必须支配所有使用点。

## 核心概念

### 1. `MLIRContext`：类型和属性的唯一化池

`MLIRContext` 拥有并唯一化 `Type`、`Attribute`。两个 `RankedTensorType` 只要参数相同，就是同一个对象，所以可以直接用 `==` 比较 `Attribute`/`Type`。

Triton 在 `compile` 里用 `ir.context()` 创建 context，再调 `ir.load_dialects(context)` 和 `backend.load_dialects(context)`（`compiler.py:239-240`、`299-302`）。`IRSource` 还会把已有 module 的 context 换掉（`compiler.py:117-119`）。这告诉你：**Type/Attribute 不能跨 context 混用**；从一个 context 拿到的类型，不能直接塞进另一个 context 的 module。

### 2. `ModuleOp` / `Operation` / `Region` / `Block`

- `ModuleOp` 是包一个 region 的具体 op；pass 里通常 `ModuleOp m = getOperation();`（`RemoveLayoutConversions.cpp:1567`、`1582`）。
- `Operation` 是通用节点：operands、results、attributes、regions、location。
- `Region` 是 `Block` 的列表；`funcOp->getRegion(0)` 拿函数体（`415`）。
- `Block` 是 `Operation` 的列表，外加 block arguments 和 terminator。

四层同时出现的真实代码在 `RemoveLayoutConversions.cpp:204-208`：`funcOp.walk([&](Block *block) { ... for (Operation &op : *block) ... })`，一边遍历 block 一边收集 block args 和 op results。`Block` 接口也常用：`getNumArguments()`、`getArgumentTypes()`、`getTerminator()`（`Ops.cpp:701-717`）。

### 3. `Value`：`OpResult` 或 `BlockArgument`

`Value` 是句柄，指向“某个 op 的结果”或“某个 block 的参数”。真实代码（`RemoveLayoutConversions.cpp:706-725`）先看 `v.getDefiningOp()`；为空就 `cast<BlockArgument>(v)`，再用 `blockArg.getOwner()->getParentOp()` 找父 op。

- `v.getDefiningOp()` 对 `BlockArgument` 返回 null；
- `cast<BlockArgument>(v)` 断言它确实是 block arg；
- `blockArg.getOwner()` 拿所属 `Block`；`getParentOp()` 拿拥有该 block 的 op（通常是 `scf.for`/`scf.if`）。

`Value` 不拥有存储；`Operation` 被 erase 后，指向它结果的 `Value` 就悬空。

### 4. `Type` 和 `Attribute`：一个是“是什么”，一个是“附加信息”

`Type` 描述值的类型（`RankedTensorType`、`IntegerType`、`LLVM::LLVMStructType`）；`Attribute` 描述附加信息（`#ttg.blocked<...>`、`DenseElementsAttr`、`IntegerAttr`）。

tensor 的 encoding 是 `Attribute`：

```cpp
auto oldType = cast<RankedTensorType>(res.getType());
auto newType = oldType.cloneWithEncoding(it->second);
```

**源码依据**：`RemoveLayoutConversions.cpp:781-783`。`cloneWithEncoding` 返回新 type，不是原地修改。

常量也是 `Attribute`：

```cpp
auto value = cast<DenseElementsAttr>(constant.getValue()).reshape(newType);
auto newOp = arith::ConstantOp::create(builder, op->getLoc(), value);
```

**源码依据**：`RemoveLayoutConversions.cpp:817-822`。

### 5. `OpBuilder`：插入点是 `(Block, iterator)`

`OpBuilder` 不拥有 IR，只是“在哪里创建 op”的游标。真实代码里三种插入点都有：

```cpp
builder.setInsertionPoint(op);                      // 插在 op 前面
builder.setInsertionPointAfter(v.getDefiningOp());  // 插在定义 op 后面
rewriter.setInsertionPointAfterValue(value);        // 插在 value 的定义之后
```

**源码依据**：`RemoveLayoutConversions.cpp:803`、`1523`、`478-480`。

插入点属于某个 `Block`。设到 A block 却拿 B block 的 operands 创建 op，verifier 会在 dominance 检查时报错；builder 自己不阻止。

### 6. `IRMapping`：克隆时的“旧值 -> 新值”表

`IRMapping` 不是 IR 的一部分，是 side table：

```cpp
IRMapping mapping;
mapping.map(oldValue, newValue);
Operation *newOp = builder.clone(*op, mapping);
Value v = mapping.lookupOrDefault(oldValue);
```

**源码依据**：`RemoveLayoutConversions.cpp:698-700`、`828`、`842-843`。

`builder.clone(*op, mapping)` 会克隆 op，并按 mapping 替换 operands；没有 mapping 的 operand 保持原样。带 region 的 op 也会被克隆，但 block argument 的映射要自己处理。

### 7. `PatternRewriter`：框架托管的“匹配 + 替换”

canonicalization pattern 长这样：

```cpp
struct CanonicalizeMaskedLoadPattern : public OpRewritePattern<LoadOp> {
  LogicalResult matchAndRewrite(LoadOp loadOp, PatternRewriter &rewriter) const override {
    ...
    rewriter.replaceOpWithNewOp<LoadOp>(loadOp, ...);
    ...
    rewriter.replaceOp(loadOp, loadOp.getOther());
    return success();
  }
};
```

**源码依据**：`Ops.cpp:140-161`。`PatternRewriter` 记录替换/删除，驱动 `applyPatternsGreedily` 维护 worklist；你不需要也不应该自己删旧 op。

## 底层机制

### 1. `walk` 只读，重写用两阶段

`initAnchorLayout` 用 `walk` 只做收集：

```cpp
funcOp.walk([&](Operation *op) {
  if (isLayoutAnchor(op)) {
    for (auto result : op->getResults())
      addAnchor(result);
  }
});
```

**源码依据**：`RemoveLayoutConversions.cpp:253-259`。它没有在 walk 里 erase 任何东西。

真正重写在 `rewriteRegion`（`RemoveLayoutConversions.cpp:415-472`）：它用 `std::deque<Region *>` 做显式 region 队列，遍历 `currentRegion->getOps()`，重写 op 后把子 region 入队；不是用 `walk` 的递归迭代器。这样重写 op 时不依赖 `walk` 内部的 next 指针。

`backwardRematerialization` 更典型：先收集所有 `ConvertLayoutOp`，再逐个处理（`926-939`）。

### 2. 为什么不能在 `walk` 里随手 `erase`

`walk` 遍历时持有“下一个节点”的信息。在回调里 `erase` 当前 op，轻则跳过节点，重则迭代器失效。更麻烦的是，erase 一个 op 会让它的 results 悬空，而其他 op 的 operands 还指着这些 `Value`。

正确做法：

- 只读/收集：`walk`；
- 要改：先收集到 `SmallVector`，收集完再改；
- 或用 `PatternRewriter`/dialect conversion，让框架管 worklist；
- 必须边走边改时，用 `WalkResult` 的 `skip`/`interrupt`，并确保不 erase 当前节点。

`RemoveLayoutConversions` 的注释也强调“one-shot rewrite based on the analysis”（`44-45`）：先分析完，再重写。

### 3. 替换和删除的顺序

`rewriteSlice` 有一段关键注释：

```cpp
// replaceAllUsesWith calls delayed until after initial rewrite.
// This is required for slice.contains(value) to work mid rewrite.
SmallVector<std::tuple<Value, Value>> replacements;
...
for (auto &kv : replacements)
  builder.replaceAllUsesWith(std::get<0>(kv), std::get<1>(kv));
convertOp->erase();
for (Operation *op : deadOps)
  op->erase();
```

**源码依据**：`RemoveLayoutConversions.cpp:730-732`、`846-852`。

顺序是：克隆/创建新 op，记录 `old -> new`；所有替换记录完，再统一 `replaceAllUsesWith`；最后 erase 旧 op。延迟的原因是中途还要用 `slice.contains(value)` 判断 value 是否在待重写集合里；提前换掉 uses 会让判断失真。

这也说明：`replaceAllUsesWith` **不会**删除旧 op，只把 uses 改指向新值；旧 op 变成 dead code，需要显式 `erase`（或让 DCE 处理）。

### 4. 克隆 loop：`IRMapping` 要映射 result 和 block arg

`scf.for` 不能简单 `clone` 后改类型，因为签名（iter_args/results）变了。真实代码用 `replaceForOpWithNewSignature` 重建 loop，再手动映射：

```cpp
scf::ForOp newForOp = replaceForOpWithNewSignature(builder, forOp, newOperands, replacements);
Block &loopBody = *newForOp.getBody();
for (auto m : argMapping) {
  mapping.map(forOp.getResult(m.first), newForOp.getResult(m.second));
  int numIndVars = newForOp.getNumInductionVars();
  mapping.map(loopBody.getArgument(m.first + numIndVars),
              loopBody.getArgument(m.second + numIndVars));
}
```

**源码依据**：`RemoveLayoutConversions.cpp:737-771`。

两个映射缺一不可：`forOp.getResult(i) -> newForOp.getResult(j)` 让循环外的 uses 指向新结果；`loopBody.getArgument(oldIdx) -> loopBody.getArgument(newIdx)` 让新 body 里的旧 block arg 指向新 block arg。`numIndVars` 是 induction variable 数量，iter_args 从它之后开始；偏移写错就会把两者映射错。

### 5. `TypeConverter`：tile 级 tensor 怎么变成每线程 struct

`TritonGPUToLLVMTypeConverter` 在构造函数里用 `addConversion` 注册 `PointerType`、`TensorDescType`、`RankedTensorType`、`MemDescType` 等转换（`TypeConverter.cpp:14-59`）。关键是 `convertTritonTensorType`：

```cpp
Type eltType = convertType(type.getElementType());
unsigned numElementsPerThread = getUniqueElemsPerThread(type);
SmallVector<Type, 4> types(numElementsPerThread, eltType);
return LLVM::LLVMStructType::getLiteral(ctx, types);
```

**源码依据**：`TypeConverter.cpp:61-68`。

也就是说：TTGIR 的 `tensor<1024xf32, #blocked>` 先由 layout 算出“每线程多少个元素”，再变成字段数为 `numElementsPerThread` 的 `LLVMStructType`。一个 tile 级 `Value` 在 LLVM dialect 里变成“每线程一个小 struct”。

`MemDescType` 则变成“base pointer(s) + 每维 offset”的 struct，或某些 tensor memory encoding 下直接是一个 pointer（`TypeConverter.cpp:70-104`）。

### 6. `applyPatternsGreedily` 和 `applyPartialConversion`

canonicalization 用 greedy driver：

```cpp
RewritePatternSet cleanUpPatterns(context);
ConvertLayoutOp::getCanonicalizationPatterns(cleanUpPatterns, context);
if (applyPatternsGreedily(m, std::move(cleanUpPatterns)).failed())
  signalPassFailure();
```

**源码依据**：`RemoveLayoutConversions.cpp:1565-1572`。

3.9.0 的 API 叫 `applyPatternsGreedily`，不是老教程里的 `applyPatternsAndFoldGreedily`；本仓库里搜不到后者。概念相同：反复应用 pattern 直到不动点。

dialect conversion 用 `applyPartialConversion`（`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp:201-209`）：`lowerFunctions` 建 `NvidiaLLVMConversionTarget`，`target.addIllegalOp<triton::FuncOp>()`，填 `populateFuncOpConversionPattern`，最后 `return applyPartialConversion(mod, target, std::move(patterns));`。

`applyPartialConversion` 只要求 illegal ops 被转换，合法 ops 可保留。`TritonToTritonGPU` 也用它，并设置 `allowPatternRollback = false`（`TritonToTritonGPUPass.cpp:723-727`）。

### 7. Region 隔离和 dominance

`tt.func` 有 `IsolatedFromAbove`（`TritonOps.td:1232-1236`），函数体不能隐式捕获外部 SSA value；`tt.return` 必须匹配函数签名（`Ops.cpp:1534-1552`）。

函数内部，`scf.for` 的 region 有自己的 block arguments：induction variable 和 iter_args。不能从 loop 外直接使用 loop 内定义的值，只能通过 `scf.yield` 和 loop results 传出来。`IRMapping` 的 result/block-arg 映射就是在维护这条边界。

`getExistingConversion` 还显式检查 dominance：

```cpp
if (domInfo.properlyDominates(remat, user)) { ... }
```

**源码依据**：`RemoveLayoutConversions.cpp:863-892`。

替换值必须支配所有使用点，否则 IR 不合法。`replaceAllUsesWith` 只改指针，不检查 dominance；verifier 会在 pass 结束后报错。

## 进一步用法/重要变体

### 1. 遍历 module：只读收集

```cpp
ModuleOp module = getOperation();
SmallVector<Operation *> worklist;
module.walk([&](Operation *op) {
  if (isa<tt::LoadOp, tt::StoreOp>(op))
    worklist.push_back(op);
});
for (Operation *op : worklist) { ... }   // 收集完之后再改
```

**guided**。真实代码见 `RemoveLayoutConversions.cpp:1534-1553` 的 `module.walk([&](FuncOp funcOp) { ... })`。

### 2. 克隆 op 并替换 operand

```cpp
IRMapping mapping;
mapping.map(oldValue, newValue);
Operation *newOp = builder.clone(*op, mapping);
for (auto [oldRes, newRes] : llvm::zip(op->getResults(), newOp->getResults()))
  oldRes.replaceAllUsesWith(newRes);
op->erase();
```

**guided**。真实代码用 `builder.clone(*op, mapping)`（`RemoveLayoutConversions.cpp:828`），再按 layout 改结果类型（`829-837`）。`clone` 出的新 op 还没有插入点；builder 的插入点决定它插在哪。

### 3. 替换 value 并删除旧 op

```cpp
value.replaceAllUsesWith(newValue);
oldOp->erase();
```

**guided**。真实代码把替换延迟到批量结束后（`RemoveLayoutConversions.cpp:730-731`、`846-852`）。如果旧 op 还有别的结果被使用，不能直接 erase；要么先替换所有结果，要么只 erase 确定 dead 的 op。

### 4. 在指定位置创建 `arith.constant`

```cpp
auto tensorType = cast<RankedTensorType>(constant.getType());
auto newType = tensorType.cloneWithEncoding(layout[constant.getResult()]);
auto value = cast<DenseElementsAttr>(constant.getValue()).reshape(newType);
auto newOp = arith::ConstantOp::create(builder, constant.getLoc(), value);
```

**源码依据**：`RemoveLayoutConversions.cpp:817-822`。这里 builder 的插入点已经在 `constant` 前面（`setInsertionPoint(op)`，`803`）；`arith::ConstantOp::create` 用 builder 的插入点，不需要额外传 block。

### 5. `dyn_cast` / `isa` / `cast`

```cpp
if (isa<DescriptorOpInterface>(op))            // 只判断，不拿句柄
  return true;
if (auto tensorType = dyn_cast<RankedTensorType>(v.getType())) { ... } // 失败返回 null
auto ifOp = dyn_cast<scf::IfOp>(op);            // 失败返回 null
auto parentOp = cast<LoopLikeOpInterface>(blockArg.getOwner()->getParentOp());
```

**源码依据**：`RemoveLayoutConversions.cpp:225-237`、`241`、`719`、`774`、`817`。

`isa<T>` 返回 bool；`dyn_cast<T>` 失败返回空句柄；`cast<T>` 失败 assert/UB。不要把 `dyn_cast` 的结果直接解引用而不检查；也不要在可能失败的场景用 `cast`。

### 6. `setInsertionPoint` 的三个变体

```cpp
builder.setInsertionPoint(op);               // 插在 op 之前
builder.setInsertionPointAfter(op);          // 插在 op 之后
builder.setInsertionPointAfterValue(value);  // 插在 value 的定义之后
```

**源码依据**：`RemoveLayoutConversions.cpp:803`、`1523`、`478-480`。

`setInsertionPointAfterValue` 对 block argument 会插在 block 开头；对 op result 会插在 defining op 之后。插入点必须在同一 region 内，否则 verifier 会报 dominance 错误。

### 7. `IRMapping` 做 loop 克隆

```cpp
IRMapping mapping;
mapping.map(forOp.getResult(0), newForOp.getResult(0));
mapping.map(loopBody.getArgument(1), newLoopBody.getArgument(1));
Operation *cloned = builder.clone(*bodyOp, mapping);
```

**guided**。真实代码在 `RemoveLayoutConversions.cpp:737-771` 用 `replaceForOpWithNewSignature` 重建 loop，再映射 result/block arg。不要直接 `clone` 一个 `scf.for` 然后改类型；loop 的 region 签名和 yield operands 都要一起改。

## 对比与取舍

| 选择 | 优点 | 代价 | 适用 |
|---|---|---|---|
| `walk` 只读收集 | 简单、安全 | 需要额外 worklist | 分析、找 anchor |
| 先收集再重写 | 不会迭代器失效 | 多一份内存 | 大部分手写 pass |
| `PatternRewriter` | 框架管 worklist/替换 | 受 pattern 匹配限制 | canonicalization、局部重写 |
| `applyPartialConversion` | 类型转换 + legality 一起管 | 配置复杂 | TTIR->TTGIR、TTGIR->LLVM |
| `IRMapping` + `clone` | 保留原 op 结构 | 要手动映射 region/block arg | rematerialization、loop 克隆 |
| `replaceAllUsesWith` + `erase` | 直接、可控 | 顺序错会悬空 | 手写重写的最后一步 |

`IRRewriter` 是普通 builder + 替换/删除辅助，不参与 pattern driver；`rewriteSlice` 用它（`RemoveLayoutConversions.cpp:735`）。`PatternRewriter` 是 driver 传进来的，必须通过它替换/删除，driver 才知道 IR 变了（`Ops.cpp:144-161`）。

## 踩雷集锦

1. **在 `walk` 回调里 `erase` 当前 op。** 迭代器会失效，可能跳过节点或崩。先收集到 `SmallVector`，再统一改；真实代码见 `RemoveLayoutConversions.cpp:926-939`。
2. **`replaceAllUsesWith` 之后忘了 `erase` 旧 op。** 替换只改 uses，不删 op；旧 op 变成 dead code。真实代码在批量替换后显式 `convertOp->erase()` 和 `deadOps` erase（`846-852`）。
3. **在替换前就 erase 旧 op。** 旧 results 悬空，其他 op 的 operands 还指着它们。顺序必须是“记录替换 -> 统一 replace -> 最后 erase”。
4. **`dyn_cast` 结果不检查就解引用。** `dyn_cast` 失败返回 null；`cast` 才会 assert。真实代码在 `dyn_cast<scf::IfOp>` 后先 `if (ifOp)` 再用（`774`）。
5. **`setInsertionPoint` 设到了错误 region。** builder 不检查 dominance；插到 loop 外却用 loop 内的 value，verifier 会在 pass 结束后报错。
6. **克隆 loop 只映射 result，不映射 block arg。** loop body 里的旧 block arg 还指向旧 loop；新 body 的 iter_arg 对不上。`RemoveLayoutConversions.cpp:755-759` 两个都映射。
7. **把 `applyPatternsAndFoldGreedily` 当成 3.9.0 的 API。** 3.9.0 用 `applyPatternsGreedily`（`RemoveLayoutConversions.cpp:1570`）；老名字在仓库里搜不到，编译不过。
8. **在 `PatternRewriter` 里手动 `op->erase()`。** 绕过 driver 的记录，worklist 可能还持有已删除的 op。用 `rewriter.eraseOp(op)`。

## 进阶

- 读 `LayoutPropagation::rewriteRegion`（`RemoveLayoutConversions.cpp:415-472`），看它如何用 region 队列而不是 `walk` 来避免迭代器问题。
- 读 `LayoutRematerialization::rewriteSlice`（`694-853`），把“收集 slice -> topologicalSort -> clone/create -> 延迟替换 -> erase”画成时序图。
- 读 `getExistingConversion` 的 dominance 检查（`863-892`），理解为什么“有现成的 remat 值”不一定能直接用。
- 读 `TritonGPUToLLVMTypeConverter`（`TypeConverter.cpp:14-104`），对照 `getUniqueElemsPerThread`，算一个 `tensor<128x64xf32, #blocked>` 在 4 warps 下会变成多少个 struct 字段。
- 读 `Dialect.cpp` 的 inliner（`56-103`），看 `handleTerminator` 如何用 `OpBuilder` 创建 `cf.br` 再 `erase` 旧 `tt.return`。

## 动手练习

1. **画对象图**：给下面这段 IR 画出 Module/Region/Block/Operation/Value 的树和 use-def 图：`%0 = tt.load %p : tensor<128x!tt.ptr<f32>>`、`%1 = arith.addf %0, %0 : tensor<128xf32>`、`tt.store %p, %1 : tensor<128x!tt.ptr<f32>>`。**guided**。
2. **找两阶段**：在 `RemoveLayoutConversions.cpp` 里找出三处“先收集、后修改”的代码，说明如果不这样写会出什么问题。**guided**。
3. **写一个 pattern**：模仿 `CanonicalizeMaskedLoadPattern`（`Ops.cpp:140-161`），写一个把 `tt.load` 的 `mask=1` 去掉的 pattern，并说明为什么必须用 `rewriter.replaceOpWithNewOp`。**guided**。
4. **IRMapping 练习**：克隆一个 `scf.for` body 里的 `arith.addf`，把它的两个 operands 映射到新值；写出 `mapping.map` 的两行。**guided**。
5. **TypeConverter 推算**：给 `tensor<128x64xf32, #blocked>` 和 `num_warps=4`，推算 `getUniqueElemsPerThread` 的结果，以及 LLVM struct 的字段数。**guided**。
6. **dominance 实验**：故意把一个 `replaceAllUsesWith` 的新值定义在 use 之后，运行 `triton-opt` 的 verifier，记录 dominance 报错。**guided**。

## 本章重点

- `MLIRContext` 唯一化 `Type`/`Attribute`；跨 context 混用会出问题。
- `ModuleOp -> Region -> Block -> Operation` 是所有权树；`Value` 的 use-def 是另一张图。
- `Value` 要么是 `OpResult`，要么是 `BlockArgument`；`replaceAllUsesWith` 只改 uses，不删 op。
- 不能在 `walk` 里随手 `erase`；先收集、再重写，或用 `PatternRewriter`/dialect conversion。
- 替换顺序：记录 `old -> new` -> 统一 `replaceAllUsesWith` -> 最后 `erase`。
- 克隆 loop 要同时映射 loop result 和 block argument；`IRMapping` 是 side table。
- `dyn_cast` 失败返回 null，`cast` 失败 assert；`isa` 只判断。
- `setInsertionPoint` 是 `(Block, iterator)`；builder 不检查 dominance。
- `TypeConverter` 把 tile 级 tensor 变成每线程的 `LLVMStructType`，这是 TTGIR 到 LLVM 的关键一跳。
- 3.9.0 用 `applyPatternsGreedily`，不是 `applyPatternsAndFoldGreedily`。

## 自我检核

1. 为什么不能在 `walk` 里 `erase` 当前 op？Triton 的真实代码是怎么绕开的？
2. `replaceAllUsesWith` 之后旧 op 还在吗？什么时候必须手动 `erase`？
3. 克隆 `scf.for` 时，为什么 result 和 block argument 都要映射？
4. `dyn_cast` 和 `cast` 的失败行为分别是什么？各举一个 Triton 源码里的例子。
5. `setInsertionPoint(op)` 和 `setInsertionPointAfter(op)` 插在哪里？插错 region 会怎样？
6. `TritonGPUToLLVMTypeConverter` 如何把 `tensor<1024xf32>` 变成 LLVM struct？
7. `applyPatternsGreedily` 和 `applyPartialConversion` 各自解决什么问题？

## 延伸阅读

1. **`lib/Dialect/TritonGPU/Transforms/RemoveLayoutConversions.cpp:44-61`、`694-853`、`1512-1553`**
   - **读哪里**：算法注释、`rewriteSlice`、`IRMapping` loop 克隆、`module.walk`。
   - **学什么**：手写 pass 如何做“分析 -> 重写”、如何延迟替换、如何映射 loop。
   - **与本章关系**：这是本章的主案例，几乎覆盖所有对象模型规则。

2. **`lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp:14-104`**
   - **读哪里**：构造函数里的 `addConversion`、`convertTritonTensorType`、`convertMemDescType`。
   - **学什么**：tile 级 tensor 如何变成每线程 struct；pointer/descriptor/memdesc 如何映射到 LLVM 类型。
   - **与本章关系**：把 Ch 10 的 TTIR 类型和 Ch 22 的 LLVM lowering 连起来。

3. **`lib/Dialect/Triton/IR/Ops.cpp:140-161`**
   - **读哪里**：`CanonicalizeMaskedLoadPattern`。
   - **学什么**：`OpRewritePattern`、`matchAndRewrite`、`replaceOpWithNewOp`、`replaceOp` 的标准写法。
   - **与本章关系**：对比手写 `IRRewriter`，理解 `PatternRewriter` 的职责。

4. **`lib/Dialect/Triton/IR/Dialect.cpp:56-103`**
   - **读哪里**：`TritonInlinerInterface::handleTerminator`、`handleResult`。
   - **学什么**：如何用 `OpBuilder` 创建 `cf.br`、如何 `erase` 旧 terminator、如何用 `replaceAllUsesWith`。
   - **与本章关系**：一个比 `RemoveLayoutConversions` 小得多的对象模型例子，适合先读。

5. **`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp:164-209`**
   - **读哪里**：`runOnOperation`、`lowerFunctions`、`applyPartialConversion`。
   - **学什么**：TypeConverter + conversion target + patterns 如何组合成一个 lowering pass。
   - **与本章关系**：把“类型转换”和“dialect conversion”放到真实 pass 里看。

## 下一章

下一章进入 TTGIR 的 layout 代数：`#ttg.blocked`、`#ttg.slice`、`#ttg.dot_op`、`#ttg.shared` 等 encoding 如何描述“tile 怎么摊到线程”，以及 `getUniqueElemsPerThread` 背后的线性布局。

→ [Ch 12 TTGIR layout 代数](./12-tritongpu-layout-algebra.md)
