# Ch 19 动态 legality 与 unrealized_conversion_cast

> **目标**：把两类「看起来能过、实际过不去」的降级问题一次说清：一是动态 legality 什么时候该返回 `false`、什么时候该返回 `nullopt`；二是 `builtin.unrealized_conversion_cast` 从哪来、`reconcile-unrealized-casts` 能消掉什么、消不掉什么。学完本章，你能看着一串 cast 反推是哪两个 pass 的类型映射不一致。

## 为什么需要这个？

Ch 18 讲了类型映射。现在来看映射不一致时会发生什么。

最容易踩的坑是一个「看起来成功了」的 pipeline：`mlir-opt` 退出码 0，IR 也打印出来了，只是里面多了一串 `builtin.unrealized_conversion_cast`。很多人把它当成「无害的转换提示」，继续往下跑；直到 `mlir-translate` 报错，才发现整条链路从中间就断了。

Ch 1 留过一个伏笔：

> 只给 `convert-func-to-llvm` 加 `index-bitwidth=32` 会留下 `builtin.unrealized_conversion_cast`，因为其它 pass 仍按 64 位处理。

本章要回答三个问题：

1. 这些 cast 是哪一行代码插的？
2. `reconcile-unrealized-casts` 到底按什么规则消 cast？
3. 为什么它救不了「位宽不一致」这类问题？

顺带把「动态 legality」讲透，因为 cast 的产生和 legality 判断是同一套机制的两面。

## 先建立直觉：cast 是欠条，不是转换

`builtin.unrealized_conversion_cast` 的语义只有一句：**「我知道这里的类型需要变，但我还没决定怎么变。」**

它和 `llvm.bitcast`、`arith.extsi` 有本质区别：

| 维度 | `unrealized_conversion_cast` | 真实转换 op |
|---|---|---|
| 语义 | 占位，无定义 | 明确的位级/数值转换 |
| verifier | 只要求至少一个结果 | 检查类型合法性 |
| `mlir-translate` | 直接拒绝 | 正常翻译 |
| 用途 | 转换过程中的临时桥 | 最终 IR |

verifier 有多松？源码只有三行有效逻辑：

```cpp
// BuiltinDialect.cpp:225-231
LogicalResult UnrealizedConversionCastOp::verify() {
  if (getNumResults() == 0)
    return emitOpError() << "expected at least one result for cast operation";
  return success();
}
```

也就是说，`i32 -> index`、`i64 -> i32` 这种在 LLVM 层毫无意义的 cast，**MLIR verifier 一律放行**。它只保证「有个结果」，不保证「这个转换说得通」。所以「IR 能过 verify」不能作为降级正确的证据。

## 核心概念一：动态 legality 的三种返回值

Ch 17 说过 target 有四态。动态 legality 就是把判断推迟到运行时：

```cpp
// DialectConversion.h:1138-1160（简化）
void addDynamicallyLegalOp(OperationName op, const DynamicLegalityCallbackFn &callback);
template <typename OpT>
void addDynamicallyLegalOp(const DynamicLegalityCallbackFn &callback);
```

有的资料写作 `setDynamicallyLegalOp`；23.1.4 的公开 API 是 `addDynamicallyLegalOp`（底层等价于 `setOpAction(op, Dynamic)` + `setLegalityCallback`）。

回调类型是 `std::function<std::optional<bool>(Operation *)>`（`DialectConversion.h:1103-1106`）。三种返回值对应三个世界：

| 返回 | `isLegal` | `isIllegal` | partial 下的结果 |
|---|---|---|---|
| `true` | 合法 | false | 留下 |
| `false` | 不合法 | **true** | **pass 失败**（若 pattern 也降不了） |
| `nullopt` | 未知 | false | 放过（但 full 下仍要求 legalize） |

源码把这件事写得很清楚：

```cpp
// DialectConversion.cpp:4018-4023
if (info->action == LegalizationAction::Dynamic) {
  std::optional<bool> result = info->legalityFn(op);
  if (!result)
    return false;          // 不知道 -> 不算 illegal
  return !(*result);       // 明确 false -> illegal
}
```

这是最容易误解的一处：

- **返回 `false` 不是「我现在处理不了，回头再说」，而是「它是非法的」**。在 partial 模式下，非法 op 降不掉就是 pass 失败。
- **返回 `nullopt` 才是「不知道」**。框架会退回 Unknown 语义：partial 放过，full 仍然要求它 legalize。

所以写动态 legality 回调时，问自己的是「这个实例**是否已经合法**」，不是「这个实例**能不能被我的 pattern 处理**」。这两个问题的答案经常相反。

`markUnknownOpDynamicallyLegal` 是另一回事：它只给「没有注册过 legality 的 op」装回调（`DialectConversion.h:1232-1237`）。`addDynamicallyLegalDialect` 则是给整个 dialect 装（`DialectConversion.h:1219-1230`）。三个 API 管的范围不同，别混用。

## 核心概念二：cast 是谁插的

框架里所有「临时桥」都走同一个函数：

```cpp
// DialectConversion.cpp:1707-1743（简化）
ValueRange ConversionPatternRewriterImpl::buildUnresolvedMaterialization(
    MaterializationKind kind, OpBuilder::InsertPoint ip, Location loc, ...) {
  ...
  OpBuilder builder(outputTypes.front().getContext());
  builder.setInsertionPoint(ip.getBlock(), ip.getPoint());
  UnrealizedConversionCastOp convertOp =
      UnrealizedConversionCastOp::create(builder, loc, outputTypes, inputs);
  if (config.attachDebugMaterializationKind) {
    StringRef kindStr = kind == MaterializationKind::Source ? "source" : "target";
    convertOp->setAttr("__kind__", builder.getStringAttr(kindStr));
  }
  ...
  unresolvedMaterializations[convertOp] = ...;
}
```

关键信息：

- **插入点是计算出来的**，通常是「最近一次映射值之后」（`computeInsertPoint`），不是随便塞在 block 末尾。
- **框架会登记这笔账**：`unresolvedMaterializations[convertOp]`。后面收尾就是按这本账逐个兑现。
- **`__kind__` 是调试用的**，只有打开 `attachDebugMaterializationKind` 才会出现；默认 IR 里看不到。

什么时候会调用它？最常见的是重映射 operand 时类型对不上：

```cpp
// DialectConversion.cpp:1541-1548
// Create a materialization for the most recently mapped values.
repl = lookupOrDefault(operand, /*desiredTypes=*/{}, /*skipPureTypeConversions=*/true);
ValueRange castValues = buildUnresolvedMaterialization(
    MaterializationKind::Target, computeInsertPoint(repl), operandLoc,
    /*valuesToMap=*/repl, /*inputs=*/repl, /*outputTypes=*/legalTypes,
    /*originalType=*/origType, currentTypeConverter);
```

把它翻译成人话：pattern 要的是 `legalTypes`，手里只有 `repl`，两者类型不一致，于是插一个 target materialization 占位。

再加上 Ch 18 提到的兜底规则——LLVM 转换器把不认识的 materialization 直接实现成 `unrealized_conversion_cast`（`LLVMCommon/TypeConverter.cpp:225-234`）——就能解释为什么「只改一个 pass 的 index-bitwidth」会冒出一串 cast：

- func 侧按 32 位把签名改成 `i32`，但它不负责函数体里的 `arith`；
- arith 侧仍按默认 64 位把 `index` 算成 `i64`；
- 两边接不上，driver 只能插 cast。

## 核心概念三：`reconcile-unrealized-casts` 的规则

这个 pass 的实现只有一个循环：

```cpp
// ReconcileUnrealizedCasts.cpp:42-47
void runOnOperation() override {
  SmallVector<UnrealizedConversionCastOp> ops;
  getOperation()->walk(
      [&](UnrealizedConversionCastOp castOp) { ops.push_back(castOp); });
  reconcileUnrealizedCasts(ops);
}
```

真正的算法在 `DialectConversion.cpp:3122-3218`，可以压缩成三步：

1. **从下往上处理每条 cast**（`while (!worklist.empty())`，LIFO）。
2. **沿「直接生产者也是 cast」的链向上找**，直到某一层的输入类型等于当前 cast 的结果类型（`3152-3167`）。
3. **替换并清理死 cast**：能替换就 `replaceAllUsesWith`；没有非 cast 用户的 cast 直接删掉（`3206-3218`）。

用伪代码写：

```text
for cast in reverse(casts):
  cur = cast
  while cur is cast:
    if types(cur.inputs) == types(cast.results):
      cast.replaceAllUsesWith(cur.inputs)
      break
    cur = defining_cast(cur.inputs)   # 只认「直接定义者是 cast」
```

这条规则有两个直接后果：

- **它只做「类型回到原点」的消链**。`i32 -> index -> i32` 能消；`i32 -> index -> i64` 消不了，因为链上没有一层的输入类型是 `i32`。
- **链中间夹了真实 op 就断**。`getInputCast` 要求 cast 的输入直接由另一条 cast 定义（`3132-3143`）；中间隔一条 `arith.addi`，链就断了。

所以 `reconcile` 的本质是**消除冗余的往返转换**，不是「补做类型转换」。它没有 TypeConverter，也没有 ABI 知识，无法把 `i32` 变成 `i64`。

## 底层机制：conversion 收尾的两次结算

一次 conversion 结束时，driver 会做两轮清理（`DialectConversion.cpp:3481-3512`）：

```cpp
// After a successful conversion, apply rewrites.
rewriterImpl.applyRewrites();

// Reconcile all UnrealizedConversionCastOps that were inserted by the
// dialect conversion frameworks. (Not the ones that were inserted by patterns.)
reconcileUnrealizedCasts(materializations, &remainingCastOps);
...
if (rewriter.getConfig().buildMaterializations) {
  for (UnrealizedConversionCastOp castOp : remainingCastOps)
    if (failed(legalizeUnresolvedMaterialization(irRewriter, castOp, ...)))
      return failure();
}
```

两轮各管一件事：

| 轮次 | 做什么 | 失败会怎样 |
|---|---|---|
| reconcile | 把能折叠的 cast 链折掉 | 不失败，只是留下 |
| legalize materialization | 用 TypeConverter 回调生成真实 op | **pass 失败** |

第二轮的错误长这样（`DialectConversion.cpp:3388-3393`）：

```text
failed to legalize unresolved materialization from (<types>) to (<types>) that remained live after conversion
```

注意「remained live」：如果 cast 没有用户，第一轮就删了；只有还被真实 op 用着，才会走到第二轮。**cast 之所以危险，不是因为它存在，而是因为它被真实 op 消费**——这时它必须变成一个语义正确的转换，否则整个 pass 失败。

## 示例一：复现 Ch 1 的混合位宽

沿用 Ch 18 的 `inc.mlir`：

```mlir
module {
  func.func @inc(%i: index) -> index {
    %one = arith.constant 1 : index
    %0 = arith.addi %i, %one : index
    return %0 : index
  }
}
```

只给 func 加 `index-bitwidth=32`，arith 保持默认：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\inc.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm),convert-func-to-llvm{index-bitwidth=32})'
```

实测输出（退出码 0）：

```mlir
module {
  llvm.func @inc(%arg0: i32) -> i32 {
    %0 = builtin.unrealized_conversion_cast %arg0 : i32 to index
    %1 = builtin.unrealized_conversion_cast %0 : index to i64
    %2 = llvm.mlir.constant(1 : index) : i64
    %3 = llvm.add %1, %2 : i64
    %4 = builtin.unrealized_conversion_cast %3 : i64 to index
    %5 = builtin.unrealized_conversion_cast %4 : index to i32
    llvm.return %5 : i32
  }
}
```

把四条 cast 逐个对上账：

| cast | 方向 | 谁需要它 |
|---|---|---|
| `%0` | `i32 -> index` | func 已把签名改成 `i32`，但 arith 仍按 `index` 消费 |
| `%1` | `index -> i64` | arith 按默认 64 位转换，输入要 `i64` |
| `%4` | `i64 -> index` | 旧的 `index -> i32` 返回桥仍要 `index` |
| `%5` | `index -> i32` | func 的返回类型是 `i32` |

注意这不是「一条 cast 太多」，而是**两套位宽假设在同一个值上打架**：入口 `i32 -> index -> i64`，出口 `i64 -> index -> i32`。中间那个 `index` 是两套假设的交汇点。

## 示例二：reconcile 不消除，translate 直接拒绝

在同一个 pipeline 末尾加 `reconcile-unrealized-casts`：

```powershell
& "$mlir\mlir-opt.exe" .\inc.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm),convert-func-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)'
```

实测输出——**与示例一逐字相同**，四条 cast 一条没少（退出码 0）。

为什么？`%1` 需要上游输入是 `i64`，实际是 `i32`；`%5` 需要上游输入是 `i32`，实际是 `i64`；四条 cast 都有真实用户，第一轮也删不掉。

把这份 IR 交给 translator：

```powershell
& "$mlir\mlir-opt.exe" .\inc.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm),convert-func-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)' `
  -o .\caseA.mlir
& "$mlir\mlir-translate.exe" --mlir-to-llvmir .\caseA.mlir
```

实测 stderr（退出码 1）：

```text
.\caseA.mlir:3:10: error: LLVM Translation failed for operation: builtin.unrealized_conversion_cast
    %0 = builtin.unrealized_conversion_cast %arg0 : i32 to index
         ^
.\caseA.mlir:3:10: note: see current operation: %0 = "builtin.unrealized_conversion_cast"(%arg0) : (i32) -> index
```

这就是「reconcile 不是万能补丁」的最直接证据：**它不会失败，也不会修好**。pipeline 退出码 0，问题被推迟到 translate。

## 示例三：正确修法是把映射对齐

把两个 pass 都设成 32：

```powershell
& "$mlir\mlir-opt.exe" .\inc.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm{index-bitwidth=32}),convert-func-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)'
```

实测输出（退出码 0）：

```mlir
module {
  llvm.func @inc(%arg0: i32) -> i32 {
    %0 = llvm.mlir.constant(1 : index) : i32
    %1 = llvm.add %arg0, %0 : i32
    llvm.return %1 : i32
  }
}
```

四条 cast 全部消失，因为它们本来就是为了弥合两套假设而存在的；假设一致，桥就不需要了。继续 translate：

```powershell
& "$mlir\mlir-opt.exe" .\inc.mlir `
  --pass-pipeline='builtin.module(func.func(convert-arith-to-llvm{index-bitwidth=32}),convert-func-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)' `
  -o .\caseC.mlir
& "$mlir\mlir-translate.exe" --mlir-to-llvmir .\caseC.mlir
```

实测输出（省略末尾的 module flags）：

```llvm
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

define i32 @inc(i32 %0) {
  %2 = add i32 %0, 1
  ret i32 %2
}
```

这才是「降级完成」的样子：没有占位 cast，LLVM IR 可以继续交给 `llc`。

## 示例四：reconcile 真正能消的形态

造一条往返链：

```mlir
module {
  func.func @f(%a: i32) -> i32 {
    %0 = builtin.unrealized_conversion_cast %a : i32 to index
    %1 = builtin.unrealized_conversion_cast %0 : index to i32
    return %1 : i32
  }
}
```

```powershell
& "$mlir\mlir-opt.exe" .\roundtrip.mlir --pass-pipeline='builtin.module(reconcile-unrealized-casts)'
```

实测输出（省略末尾的 module flags）：

```mlir
module {
  func.func @f(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}
```

两条 cast 都消失了：`%1` 的结果是 `i32`，上游 `%0` 的输入也是 `i32`，直接替换成 `%a`，`%0` 变成死代码后被删。

再长一点也能折：`i32 -> index -> i64 -> i32` 的三角链，实测 `reconcile-unrealized-casts` 之后同样只剩 `return %arg0 : i32`。算法会沿最后那条链一路向上找到输入类型为 `i32` 的层，再把整条链替换掉。对比示例二：同样是「一串 cast」，一个能全消，一个一条都消不掉。区别不在 cast 的数量，而在**链的末端有没有回到起点类型**。

## 对比与取舍

### 修映射 vs 事后 reconcile

| 做法 | 成本 | 正确性 | 适用 |
|---|---|---|---|
| 对齐所有 pass 的 `index-bitwidth` / DataLayout | 改 pipeline | 根治 | 首选 |
| 写带语义的 materialization | 写 C++ 回调 | 正确 | 自定义类型、特殊 ABI |

判断标准很简单：**cast 两端的类型描述的是同一个值吗？**

- 是，只是中间多绕了一圈 → reconcile 合适。
- 否，比如 `i32` 和 `i64` 是两种 ABI 约定 → reconcile 无能为力，必须对齐映射。

### 动态 legality 的取舍

经验规则：能用静态 `addLegalOp` / `addIllegalOp` 就别用动态；只有「同名 op、部分实例合法」时才用，且回调只做类型/属性检查，别遍历 IR。

## 踩雷集锦

1. **把 `unrealized_conversion_cast` 当 `llvm.bitcast`。** 它没有语义，translate 会直接拒绝；`llvm.bitcast` 才能进 LLVM IR。
2. **只给一个 pass 加 `index-bitwidth`。** 示例一的标准翻车姿势；凡是影响类型映射的选项，所有相关 pass 必须一致。
3. **以为 reconcile 会「补做转换」。** 它只折叠类型回到原点的链，不做任何数值/布局转换。
4. **动态 legality 回调返回 `false` 当「稍后再试」。** `false` 就是 illegal；partial 模式下 pattern 降不掉就失败。想表达「不知道」要返回 `nullopt`。
5. **cast 链中间夹了真实 op，还指望 reconcile。** 链在 `arith.addi` 处就断了；只认直接生产者是 cast 的情况（`DialectConversion.cpp:3132-3143`）。

## 进阶：再往深一层

- **看 materialization 的来源**：打开 `attachDebugMaterializationKind` 后，框架插入的 cast 会带 `__kind__ = "source" | "target"`（`DialectConversion.cpp:1723-1727`），排查方向时非常有用。
- **先分析再转换**：`applyAnalysisConversion`（`DialectConversion.h:1546-1549`）只标记哪些 op 能 legalize，不改 IR；适合在大 pipeline 前做可行性检查。

## 动手练习

约定：文件放在 `.scratch\mlir-17-19\`，`$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"`。每题至少三个用例，记录退出码、cast 数量和 translate 结果。

### 练习 1：制造一个 reconcile 救不了的用例

**任务**：构造一个 pipeline，使 `reconcile-unrealized-casts` 跑完后 cast 数量不变，并且 `mlir-translate` 失败。

**测试用例**：(a) 混合 bitwidth；(b) 全 32；(c) 全默认 64。

**步骤**：1) 跑 pipeline；2) 用 `Select-String 'unrealized_conversion_cast'` 数 cast；3) 尝试 translate；4) 解释为什么 (a) 不变、(b)(c) 为 0。

<details><summary>参考解答</summary>

(a) 4 条不变，translate 报 `LLVM Translation failed for operation: builtin.unrealized_conversion_cast`；(b)(c) 0 条，translate 成功。原因是链上没有回到起点类型的层，reconcile 无事可做。

</details>

**常见卡点**：用管道直接 `| mlir-translate`，被 PowerShell 的编码/BOM 干扰，误判成 IR 问题；用 `-o` 落盘再 translate。

### 练习 2：验证 reconcile 的折叠边界

**任务**：分别构造 `i32 -> index -> i32`、`i32 -> index -> i64 -> i32`、`i32 -> index -> (真实 op) -> i32` 三种链，跑 reconcile，记录哪些能消。

**测试用例**：(a) 两跳往返；(b) 三跳三角；(c) 中间夹 `arith.addi`。

**步骤**：1) 手写 IR；2) 跑 `builtin.module(reconcile-unrealized-casts)`；3) 对比输出；4) 用 `3132-3143` 与 `3152-3167` 解释。

<details><summary>参考解答</summary>

(a)(b) 全部消掉，只剩 `return %arg0`；(c) 一条都消不掉，因为 `getInputCast` 要求输入的直接定义者是 cast，中间的真实 op 断链。

</details>

**常见卡点**：把「有 cast」等同于「能 reconcile」；关键看链的形状，不看数量。

### 练习 3：写一个动态 legality 回调（guided）

**任务**：为自定义 `foo.op` 设计 legality：只有当所有 operand 和 result 都是 `i32` 时才合法。写出回调，并说明什么情况返回 `nullopt`。

**测试用例**：(a) 全 i32；(b) 混入 f32；(c) 类型是 `index`（你的 converter 能处理）。

**步骤**：1) 写 `target.addDynamicallyLegalOp<foo::Op>(...)`；2) 用 `llvm::all_of` 检查类型；3) 对无法判断的情况返回 `nullopt`；4) 解释 partial 下 (b)(c) 的差异。

<details><summary>参考解答</summary>

```cpp
target.addDynamicallyLegalOp<foo::Op>([](foo::Op op) -> std::optional<bool> {
  auto ok = [](Type t) { return t.isInteger(32); };
  return llvm::all_of(op->getOperandTypes(), ok) &&
         llvm::all_of(op->getResultTypes(), ok);
});
```

(a) `true`；(b) `false`，partial 下若 pattern 降不掉就失败；(c) 如果 converter 能把 `index` 转成 `i32`，应返回 `false`（当前实例不合法），而不是 `nullopt`——`nullopt` 只留给「确实无法判断」。

</details>

**常见卡点**：把「我的 pattern 处理不了」写成 `false`；`false` 是「非法」，会直接让 partial 失败。

### 练习 4：定位 cast 的产生 pass

**任务**：用 `--mlir-print-ir-after-all` 找出 `unrealized_conversion_cast` 首次出现在哪个 pass 之后。

**测试用例**：(a) 只 `convert-func-to-llvm`；(b) func + arith；(c) func + scf→cf + arith。

**步骤**：1) 命令末尾加 `2>&1` 保存日志；2) 搜 `unrealized_conversion_cast`；3) 对照最近的 pass 名。

<details><summary>参考解答</summary>

(a) 在 `convert-func-to-llvm` 之后首次出现 `i64 -> index`；(b)(c) 整条 pipeline 结束仍为 0 条。cast 是「签名先变、body 后变」的产物，不是某个 pass 主动生成的。

</details>

**常见卡点**：IR dump 走 stderr，不重定向就看不到。

### 练习区自我检核

- [ ] 我能说出 dynamic 回调三种返回值的后果
- [ ] 我知道 reconcile 只折「回到原点」的链

## 本章重点整理

- 动态 legality 回调返回 `true/false/nullopt`，分别对应合法、非法、未知；`false` 在 partial 下是致命的。
- `unrealized_conversion_cast` 是占位符，verifier 只检查「至少一个结果」。
- conversion 收尾先 reconcile，再把剩余 materialization 落实；落不实就报 `failed to legalize unresolved materialization`。
- `reconcile-unrealized-casts` 只折「链上某层输入类型等于当前结果类型」的 cast，且链不能被真实 op 打断。
- 位宽/布局不一致要靠对齐映射解决，reconcile 不是补丁；`mlir-translate` 会拒绝残留 cast。

## 自我检核

- [ ] 我能解释 `false` 与 `nullopt` 在 partial 模式下的不同后果
- [ ] 我能说出 `buildUnresolvedMaterialization` 做了哪三件事
- [ ] 我能画出 `i32 -> index -> i64` 为什么消不掉
- [ ] 我知道 cast 链被真实 op 打断后会发生什么
- [ ] 我知道 `__kind__` 只在调试配置下出现
- [ ] 我能用 `-o` + `mlir-translate` 复现翻译失败

## 延伸阅读

- **[MLIR 文档：Dialect Conversion](https://mlir.llvm.org/docs/DialectConversion/)**
  - **读哪里**：Legality 与 Materialization 两节。
  - **学什么**：动态 legality 与 materialization 的官方定义。
  - **和本章的关联**：核心概念一、二的规范来源。

- **[mlir/lib/Transforms/Utils/DialectConversion.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Transforms/Utils/DialectConversion.cpp)**
  - **读哪里**：`buildUnresolvedMaterialization`（1707-1743）、reconcile（3122-3218）、收尾（3481-3512）、`isIllegal`（4013-4027）。
  - **学什么**：cast 的插入与消除的真实算法。
  - **和本章的关联**：示例一至四的每一条结论都能在这几段里找到依据。

- **[mlir/lib/Conversion/ReconcileUnrealizedCasts/ReconcileUnrealizedCasts.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/ReconcileUnrealizedCasts/ReconcileUnrealizedCasts.cpp)**
  - **读哪里**：整个文件（24-47）。
  - **学什么**：pass 只是收集 cast 并调用公共算法。
  - **和本章的关联**：解释「为什么 pass 本身没有魔法」。

---

下一章进入 tensor 到 memref 的世界：bufferization 如何决定「什么时候分配、什么时候复用」，以及 one-shot bufferize 为什么需要一整套接口。

→ [Ch 20 Bufferization：tensor 到 memref](./20-bufferization.md)
