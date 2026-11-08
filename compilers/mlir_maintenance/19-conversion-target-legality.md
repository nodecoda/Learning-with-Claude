# Ch 19 — ConversionTarget 与 legality

> **目标**：能读懂一个 conversion pass 的“合法性契约”：哪些 op/type 必须被重写、哪些允许保留、嵌套 op 怎么递归判断，以及 `applyPartialConversion` 与 `applyFullConversion` 的行为差异。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance`；`mlir-opt` 带 `-test-legalize-*` 测试 pass；assertions ON。

## 19.1 为什么 legality 是 conversion 的第一现场

MLIR 的 dialect conversion 不是“把 pattern 跑一遍”。它的核心是一个**合法性证明**：

1. 目标（`ConversionTarget`）声明：哪些 op 合法、哪些非法、哪些动态判断；
2. 模式（`ConversionPattern`）把非法 op 重写成合法 op；
3. 驱动器反复应用，直到没有非法 op，或证明失败。

维护者遇到的 conversion bug，大多不是 pattern 写错，而是**合法性契约写错**：

- 忘了把 `func.return` 标成 legal，导致 partial conversion 一直失败；
- 把嵌套 region 的 op 当成“外层合法就合法”，结果 region 内部残留非法 op；
- `markUnknownOpDynamicallyLegal` 的回调太宽松，非法 op 被放过；
- full conversion 失败时，错误信息只指向根 op，真正的原因在某个 region 深处。

所以这一章先讲“什么是合法”，下一章才讲“怎么转换类型”。

### conversion 不是 canonicalization

两个 driver 经常被混为一谈：

| 维度 | canonicalization | dialect conversion |
|---|---|---|
| 目标 | 让 IR 更规范/更简单 | 让 IR 满足目标 dialect 的合法性 |
| 失败语义 | 不收敛只是不优化 | 不合法就是 pass 失败 |
| 规则 | `RewritePattern` + fold | `ConversionPattern` + `ConversionTarget` |
| 结果 | 可以部分改写 | 要么全部合法，要么失败（full） |
| 调试 | `-debug-only=greedy-rewriter` | `-debug-only=dialect-conversion` |

维护时先判断“这是 canonicalization 问题还是 conversion 问题”：前者通常改 pattern/fold，后者先改 target/type converter。把两者混在一起，补丁会越修越大。

## 19.2 先建立直觉：三方契约

```
                  ┌─────────────────────────────┐
                  │  ConversionTarget           │
                  │  legal / illegal / dynamic  │
                  └──────────────┬──────────────┘
                                 │  isLegal(op)?
                                 ▼
   ┌──────────────────┐   ┌──────────────────┐
   │ ConversionPattern│──▶│  ConversionDriver │
   │ rewrite illegal  │   │  apply patterns   │
   └──────────────────┘   └────────┬─────────┘
                                   │ 直到 fixpoint
                                   ▼
                     ┌──────────────────────────┐
                     │ 成功：所有 op 合法         │
                     │ 失败：failed to legalize  │
                     └──────────────────────────┘
```

三方契约里，`ConversionTarget` 是“法律”，pattern 是“执法”，driver 是“审判”。三者任何一方不一致，都会表现为 `failed to legalize operation`。

## 19.3 `ConversionTarget` 的三种状态

`mlir/include/mlir/Transforms/DialectConversion.h:1079` 定义 `ConversionTarget`，`:1083` 定义 `LegalizationAction`：

```bash
grep -n 'class ConversionTarget\|enum class LegalizationAction' \
  mlir/include/mlir/Transforms/DialectConversion.h
```

本机实测：

```text
1079:class ConversionTarget {
1083:  enum class LegalizationAction {
```

三种状态：

| 状态 | API | 含义 |
|---|---|---|
| Legal | `addLegalOp<OpT>()` / `addLegalDialect<DialectT>()` | 允许保留 |
| Illegal | `addIllegalOp<OpT>()` / `addIllegalDialect<DialectT>()` | 必须被重写 |
| Dynamic | `addDynamicallyLegalOp<OpT>(fn)` | 运行时按 fn 判断 |

默认状态取决于 target 的构造方式：`ConversionTarget(MLIRContext &ctx)` 默认所有 op 是“unknown”，由 `markUnknownOpDynamicallyLegal` 或显式规则决定。

维护含义：

- **不要默认“没写就是合法”。** 新加一个 op 时，如果 target 没有覆盖它，行为可能是 unknown → 失败，也可能是 unknown → 被动态回调放过，取决于 driver。
- **不要用 `addLegalDialect` 一把梭。** 整个 dialect 合法会掩盖内部 op 的非法状态；除非你确实要保留整个 dialect。

### dialect 级与 op 级规则

`ConversionTarget` 同时支持 dialect 级和 op 级规则：

```cpp
// dialect 级：整个 dialect 的 op 默认 legal/illegal
target.addLegalDialect<func::FuncDialect>();
target.addIllegalDialect<arith::ArithDialect>();

// op 级：只覆盖某一个 op
target.addLegalOp<func::ReturnOp>();
target.addIllegalOp<arith::AddIOp>();
```

优先级不是“后写的覆盖先写的”这么简单：driver 会按 op 的 `OperationName` 查表，op 级规则优先于 dialect 级。维护时建议：

- 先用 dialect 级建立大边界；
- 再用 op 级处理例外；
- 最后用动态 legality 处理“同 op 不同类型”的情况。

这样 review 时能一眼看出哪些是“整体保留”、哪些是“例外”。

## 19.4 递归 legality：外层合法不代表 region 内部合法

`ConversionTarget::isLegal` 的实现在 `mlir/lib/Transforms/Utils/DialectConversion.cpp:3999`。它会检查：

- op 本身是否 legal；
- 如果 op 有 region，region 内的 op 是否也 legal（递归）；
- 某些 op 可以通过 `RecursiveLegality` 配置跳过递归。

这就是为什么“我把 `func.func` 标成 legal 了，为什么还失败？”——`func.func` 合法，但它 region 里的 `arith.addi` 可能非法。

实验：`test-legalizer-full.mlir` 的失败信息：

```bash
mlir-opt -allow-unregistered-dialect \
  -test-legalize-patterns="test-legalize-mode=full" \
  mlir/test/Transforms/test-legalizer-full.mlir 2>&1 \
  | grep -E 'error: failed to legalize' | head
```

本机实测：

```text
mlir/test/Transforms/test-legalizer-full.mlir:70:5: error: failed to legalize operation 'foo.unknown_op': "foo.unknown_op"() : () -> ()
mlir/test/Transforms/test-legalizer-full.mlir:84:3: error: failed to legalize operation 'test.post_order_legalization': "test.post_order_legalization"() ({...}) : () -> ()
```

第二行 `({...})` 表示失败发生在一个带 region 的 op 内部；只看根 op 会误判。

### 递归检查的边界

`isLegal` 的递归不是无条件的。它大致按以下顺序判断：

1. op 自身是否在 `legalOps` / `illegalOps` / `dynamicLegalOps` 中；
2. 如果 op 属于某个 dialect，查 dialect 级规则；
3. 如果 op 有 region，递归检查 region 内的 op；
4. 如果 op 实现了 `RecursiveLegality` 相关接口，按接口决定是否跳过递归。

维护含义：如果你确实要“外层合法、内部暂时不检查”，需要显式使用递归合法性接口，而不是指望 `addLegalOp` 自动跳过 region。

## 19.5 动态 legality：什么时候用回调

`addDynamicallyLegalOp` 适合“同一个 op 在不同类型/属性下合法性不同”的场景：

```cpp
target.addDynamicallyLegalOp<func::FuncOp>([](func::FuncOp op) {
  return llvm::all_of(op.getArgumentTypes(), isLegalType);
});
```

典型用途：

- 函数签名里的类型是否已转换；
- `memref` 的 layout 是否满足目标；
- 某个 op 的 operand 是否已全部变成 LLVM 类型。

一个最小形态：

```cpp
target.addDynamicallyLegalOp<func::FuncOp>([](func::FuncOp op) {
  return llvm::all_of(op.getArgumentTypes(), [](Type t) {
    return t.isInteger(32) || t.isF32();   // 只允许已经转换过的类型
  });
});
```

维护含义：

- 回调必须**纯**、快、无副作用；driver 会多次调用。
- 回调返回 `true` 只表示“当前合法”，不表示“永远合法”；重写后要重新检查。
- 回调太宽松 → 非法 IR 被放过；太严格 → 无限失败。

## 19.6 partial vs full：失败语义不同

| API | 行为 | 什么时候用 |
|---|---|---|
| `applyPartialConversion` | 允许目标中 unknown 的 op 保留；只要求已声明 illegal 的被重写 | 渐进式 lowering、混合 dialect |
| `applyFullConversion` | 所有 op 必须 legal；任何 unknown/illegal 都失败 | 最终出口、严格验证 |

实验：partial conversion 可以留下 `unrealized_conversion_cast` 和未转换 op；full conversion 会直接报错。Ch 21 会专门讲 cast。

调用形态：

```cpp
if (failed(applyPartialConversion(op, target, std::move(patterns))))
  return failure();

if (failed(applyFullConversion(op, target, std::move(patterns))))
  return failure();
```

维护含义：

- 如果你在写一个“把 X 降到 Y”的 pass，通常用 partial，因为上游可能还有别的 dialect；
- 如果你在写“最终出口”，用 full，确保没有残留；
- 测试要覆盖两种模式，否则 partial 的宽松会掩盖 full 的失败。

## 19.7 调试 conversion：看 driver 的决策

assertions 构建支持：

```bash
mlir-opt --convert-to-llvm --debug-only=dialect-conversion /tmp/map.mlir 2>&1 | head
```

本机实测：

```text
[dialect-conversion:1]
[dialect-conversion:1] //===-------------------------------------------===//
[dialect-conversion:1] Legalizing operation : 'builtin.module' (0x...)
[dialect-conversion:1]   * Fold {
[dialect-conversion:1]   } -> FAILURE : unable to fold
[dialect-conversion:1] } -> FAILURE : no matched legalization pattern
[dialect-conversion:1]
[dialect-conversion:1] Legalizing operation : 'func.func' (0x...)
[dialect-conversion:1]   * Pattern : 'func.func -> ()' {
[dialect-conversion:1]     ** Insert  : 'llvm.func' (0x...) (was detached)
```

读法：

- `Legalizing operation` 是 driver 当前处理的 op；
- `Fold` 失败不代表错，只是没有 fold；
- `Pattern : 'func.func -> ()'` 是匹配到的 conversion pattern；
- `Insert` 是重写产生的新 op；
- 最后 `FAILURE : no matched legalization pattern` 才是需要关注的。

### 常见失败信息对照

| 日志 | 含义 | 下一步 |
|---|---|---|
| `failed to legalize operation 'X'` | X 没有匹配 pattern，且 target 认为它非法 | 查 X 的 pattern 是否注册、target 是否误标 |
| `no matched legalization pattern` | driver 试过 fold/pattern 都不匹配 | 看 pattern 的 root op 和 benefit |
| `failed to legalize operation 'X' ({...})` | 失败在 X 的 region 内部 | 用 debug 日志定位内层 op |
| `unrealized_conversion_cast` 残留 | 类型没被 materialize 或 reconcile | 查 TypeConverter 与 finalize |
| `pattern failed to converge` | pattern 反复重写同一个 op | 查 pattern 是否产生同类 op |

## 19.8 从 pattern 到 target：一个最小骨架

真实 conversion pass 的形状通常是这样：

```cpp
struct AddIOpLowering : OpConversionPattern<arith::AddIOp> {
  using OpConversionPattern::OpConversionPattern;

  LogicalResult matchAndRewrite(arith::AddIOp op, OpAdaptor adaptor,
                                ConversionPatternRewriter &rewriter) const override {
    Type ty = typeConverter->convertType(op.getType());
    if (!ty) return rewriter.notifyMatchFailure(op, "type not convertible");
    rewriter.replaceOpWithNewOp<LLVM::AddOp>(op, ty, adaptor.getLhs(),
                                             adaptor.getRhs());
    return success();
  }
};

void runLowering(Operation *op) {
  ConversionTarget target(*op->getContext());
  target.addLegalDialect<LLVM::LLVMDialect>();
  target.addIllegalDialect<arith::ArithDialect>();
  target.addDynamicallyLegalOp<func::FuncOp>([](func::FuncOp f) {
    return llvm::all_of(f.getArgumentTypes(),
                        [](Type t) { return !t.isInteger(64); });
  });

  RewritePatternSet patterns(op->getContext());
  patterns.add<AddIOpLowering>(op->getContext());
  if (failed(applyPartialConversion(op, target, std::move(patterns))))
    signalPassFailure();
}
```

三个顺序不能反：

1. 先建 `ConversionTarget`，把 legal/illegal/dynamic 写清；
2. 再建 `RewritePatternSet`，把 pattern 加进去；
3. 最后 `applyPartialConversion`/`applyFullConversion`。

如果你先写 pattern 再补 target，很容易出现“pattern 匹配了但 target 说结果仍非法”的循环失败。

## 19.9 可跑实验

### 实验 1：看 `ConversionTarget` 的 API

```bash
grep -n 'class ConversionTarget\|enum class LegalizationAction\|addLegalOp\|addIllegalOp\|addDynamicallyLegalOp\|markUnknownOpDynamicallyLegal' \
  mlir/include/mlir/Transforms/DialectConversion.h | head -12
```

本机实测：

```text
1079:class ConversionTarget {
1083:  enum class LegalizationAction {
1126:  void addLegalOp(OperationName op) {
1141:  void addDynamicallyLegalOp(OperationName op,
1236:  void markUnknownOpDynamicallyLegal(const DynamicLegalityCallbackFn &fn) {
```

### 实验 2：partial conversion 成功

```bash
mlir-opt --convert-to-llvm /tmp/map.mlir
```

本机实测：

```text
module {
  llvm.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
}
```

### 实验 3：full conversion 的失败信息

```bash
mlir-opt -allow-unregistered-dialect \
  -test-legalize-patterns="test-legalize-mode=full" \
  mlir/test/Transforms/test-legalizer-full.mlir 2>&1 \
  | grep -E 'error: failed to legalize' | head -3
```

本机实测：

```text
mlir/test/Transforms/test-legalizer-full.mlir:70:5: error: failed to legalize operation 'foo.unknown_op': "foo.unknown_op"() : () -> ()
mlir/test/Transforms/test-legalizer-full.mlir:84:3: error: failed to legalize operation 'test.post_order_legalization': "test.post_order_legalization"() ({...}) : () -> ()
```

### 实验 4：legalizer 的 listener 通知

```bash
mlir-opt -allow-unregistered-dialect -split-input-file \
  -test-legalize-patterns="allow-pattern-rollback=1" -verify-diagnostics \
  mlir/test/Transforms/test-legalizer.mlir | head -10
```

本机实测：

```text
notifyOperationInserted: test.legal_op_a, was unlinked
notifyOperationReplaced: test.illegal_op_a
notifyOperationModified: func.return
notifyOperationErased: test.illegal_op_a
module {
  func.func @verifyDirectPattern() -> i32 {
    %0 = "test.legal_op_a"() <{status = "Success"}> : () -> i32
    return %0 : i32
  }
}
```

顺序是 `Inserted → Replaced → Modified → Erased`。conversion driver 在 conversion 结束时批量发通知，不是每条 pattern 后立刻发。

### 实验 5：debug 日志

```bash
mlir-opt --convert-to-llvm --debug-only=dialect-conversion /tmp/map.mlir 2>&1 | head -12
```

本机实测：

```text
[dialect-conversion:1] Legalizing operation : 'builtin.module'
[dialect-conversion:1]   * Fold {
[dialect-conversion:1]   } -> FAILURE : unable to fold
[dialect-conversion:1] } -> FAILURE : no matched legalization pattern
...
[dialect-conversion:1]   * Pattern : 'func.func -> ()' {
[dialect-conversion:1]     ** Insert  : 'llvm.func' (0x...) (was detached)
```

### 实验 6：type conversion 的独立测试

```bash
mlir-opt -allow-unregistered-dialect \
  -test-legalize-type-conversion -split-input-file \
  mlir/test/Transforms/test-legalize-type-conversion.mlir > /dev/null
echo "exit=$?"
```

本机实测：

```text
exit=0
```

## 19.9 对比与取舍

| 方案 | 优点 | 缺点 | 适用 |
|---|---|---|---|
| `addLegalDialect` | 规则少、维护成本低 | 容易放过内部非法 op | 整个 dialect 确定保留 |
| `addLegalOp` | 精确 | op 多时规则长 | 少量例外 |
| `addDynamicallyLegalOp` | 能表达类型/属性条件 | 回调写错会放过或卡死 | 同 op 不同类型 |
| partial conversion | 可与其它 dialect 共存 | 允许残留，测试易假绿 | 渐进 lowering |
| full conversion | 保证无残留 | 任何 unknown 都失败 | 最终出口 |

选择顺序：先决定 partial/full，再决定 legal 的粒度，最后才写 pattern。反过来做，通常会在 review 时被要求重写 target。

| 文件 | 行号 | 作用 |
|---|---|---|
| `mlir/include/mlir/Transforms/DialectConversion.h` | 1079 | `ConversionTarget` |
| 同上 | 1083 | `LegalizationAction` |
| 同上 | 1236 | `markUnknownOpDynamicallyLegal` |
| `mlir/lib/Transforms/Utils/DialectConversion.cpp` | 3999 | `ConversionTarget::isLegal` |
| 同上 | 4243 / 4260 | `applyPartialConversion` / `applyFullConversion` |
| `mlir/docs/DialectConversion.md` | 16–150 | Modes / Conversion Target / recursive legality |

核对：

```bash
grep -n 'ConversionTarget::isLegal' mlir/lib/Transforms/Utils/DialectConversion.cpp
grep -n 'applyPartialConversion\|applyFullConversion' mlir/lib/Transforms/Utils/DialectConversion.cpp | head
```

## 19.11 踩雷集锦

1. **把“外层 op legal”当“整个 region legal”。** `isLegal` 会递归；region 内的 op 仍要单独合法。
2. **以为不写规则就是 legal。** unknown 状态可能导致失败或被动态回调放过，取决于 target/driver。
3. **`addLegalDialect` 一把梭。** 会掩盖 dialect 内部真正非法的 op，测试变绿但 lowering 不完整。
4. **动态 legality 回调有副作用。** driver 会多次调用；回调必须纯。
5. **partial 测试通过就认为 full 也会过。** partial 允许残留，full 不允许。
6. **只看根 op 的失败信息。** `({...})` 表示失败在 region 内部；用 `--debug-only=dialect-conversion` 追。
7. **把 `unrealized_conversion_cast` 当合法结果。** 它只是临时桥；最终出口前必须 reconcile 或消除。
8. **在 conversion pattern 里手动 erase 旧 op。** driver 负责 erase；手动 erase 会破坏 listener/rollback。

## 19.12 本章重点整理

- Conversion 的核心是合法性证明：`ConversionTarget` 定规则，pattern 重写，driver 迭代到 fixpoint。
- `ConversionTarget` 三种状态：legal / illegal / dynamic；默认 unknown 需要显式处理。
- `isLegal` 递归检查 region 内部；外层合法不等于整体合法。
- `applyPartialConversion` 允许 unknown 保留，`applyFullConversion` 要求全部合法。
- `--debug-only=dialect-conversion` 是定位“为什么没匹配/为什么失败”的第一工具。
- listener 通知在 conversion 结束时批量发出，顺序是 Inserted → Replaced → Modified → Erased。

## 19.13 自我检核

- [ ] 能解释 `ConversionTarget` 的三种状态，以及 unknown 的默认行为
- [ ] 能写出一个动态 legality 回调，并说明它会被多次调用
- [ ] 能解释为什么 `func.func` legal 但 region 内的 `arith.addi` 仍可能失败
- [ ] 能区分 partial 与 full 的失败语义，并各举一个使用场景
- [ ] 能用 `--debug-only=dialect-conversion` 读出“没匹配到 pattern”的原因
- [ ] 能说出 listener 通知的顺序，以及为什么不能手动 erase 旧 op

## 19.14 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DialectConversion.md`**
  - 读哪里：16–235 行（Modes、Conversion Target、Rewrite Pattern）
  - 能学到什么：官方对 legality/partial/full 的定义
  - 和本章关联：19.3–19.6 的原文
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Transforms/DialectConversion.h`**
  - 读哪里：1079–1260 行
  - 能学到什么：`ConversionTarget` 的完整 API
  - 和本章关联：19.3 的三种状态
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Transforms/Utils/DialectConversion.cpp`**
  - 读哪里：3999–4100、4243–4290
  - 能学到什么：`isLegal` 的递归与 partial/full 的实现
  - 和本章关联：19.4、19.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Transforms/test-legalizer.mlir`**
  - 读哪里：整份
  - 能学到什么：各种 legality 场景的测试写法
  - 和本章关联：19.9 的实验 4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Transforms/test-legalizer-full.mlir`**
  - 读哪里：整份
  - 能学到什么：full conversion 的失败负控
  - 和本章关联：19.9 的实验 3

合法性写对了，下一步才是“类型怎么变”。下一章进入 `TypeConverter`：1:1、1:N、materialization，以及为什么类型转换比 pattern 更容易出隐蔽 bug。

→ 下一章：[Ch 20 TypeConverter 与 materialization](./20-type-converter.md)
