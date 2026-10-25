# Ch 17 Dialect Conversion 框架

> **目标**：把「降级」从一句口号变成一套可检查的机制。学完本章，你能说清 ConversionTarget 的四种 legality 状态、`applyPartialConversion` 与 `applyFullConversion` 的失败语义差异、ConversionPattern 与普通 RewritePattern 的边界，以及为什么 `ConversionPatternRewriter` 不允许你像普通 `PatternRewriter` 那样随手 `eraseOp`。

## 为什么需要这个？

Ch 7 的 `canonicalize` 是「看到能改的就改，改不动就跳过」。它没有全局目标，也不保证最后 IR 里剩下什么。降级不一样：我们要把 `arith` / `func` / `scf` 换成 `llvm`，并且必须保证**转换结束后，没有任何一个该消失的 op 还活着**。

这两件事的难度不在「写一条替换规则」，而在三个全局问题：

1. **哪些 op 必须消失？** 是 `arith` 整个 dialect，还是只有某几个 op？`func.func` 在中间态允许存在吗？
2. **类型变了怎么办？** `index` 要变 `i64`，`memref` 要变 descriptor，函数签名要重写；改不彻底时谁来补桥？
3. **失败了怎么办？** 是回滚这一条 pattern、换一条再试，还是让整个 pass 失败？

`canonicalize` 一个都不回答。Dialect Conversion 框架回答全部，代价是你必须显式声明 target 和 legality。本章就把这套声明拆开。

## 先建立直觉：target、patterns、rewriter 三件套

一次 conversion 只有三个角色：

```text
ConversionTarget   ── 回答「什么算合法」──┐
ConversionPattern  ── 回答「怎么改」  ──┼──▶ ConversionPatternRewriter ──▶ 新 IR
TypeConverter      ── 回答「类型怎么变」┘
```

- **ConversionTarget** 是验收标准，不是转换规则。它只描述「结束时哪些 op 可以留下」。
- **ConversionPattern** 是转换规则。它和普通 RewritePattern 长得很像，但拿到的 operand 已经被类型转换过，替换结果也要过 legality 检查。
- **ConversionPatternRewriter** 是受约束的 rewriter。它记录你的每一步修改，支持回滚，并在最后统一补 materialization。

一个重要推论：**target 是每个 pass 自己建的，不是全局配置。** 同一个 `builtin.module`，`convert-arith-to-llvm` 和 `convert-linalg-to-std` 各自 new 一个 target，legality 完全不同。读源码时先找 `ConversionTarget target(...)`，再找 `addLegal* / addIllegal*`。

## 核心概念一：ConversionTarget 与四态 legality

源码把 legality 定义成三值枚举，加上「没注册」一共四态：

```cpp
// DialectConversion.h:1082-1092
enum class LegalizationAction {
  Legal,    // 目标支持这个 op
  Dynamic,  // 要看实例才能决定
  Illegal,  // 目标明确不支持
};
```

| 状态 | 怎么设置 | 含义 |
|---|---|---|
| Legal | `addLegalOp` / `addLegalDialect` | 这个 op（或整个 dialect）可以原样留下 |
| Dynamic | `addDynamicallyLegalOp` / `addDynamicallyLegalDialect` | 调用回调，按实例判断 |
| Illegal | `addIllegalOp` / `addIllegalDialect` | 必须被转换掉 |
| Unknown | 什么都没注册 | 框架没意见，怎么处理取决于 partial/full |

`ConversionTarget::isLegal` 只对「确定合法」返回 `LegalOpDetails`，其余一律 `nullopt`：

```cpp
// DialectConversion.cpp:3985-3997（简化）
auto isOpLegal = [&] {
  if (info->action == LegalizationAction::Dynamic) {
    std::optional<bool> result = info->legalityFn(op);
    if (result) return *result;
  }
  return info->action == LegalizationAction::Legal;
};
if (!isOpLegal()) return std::nullopt;
```

注意 `Dynamic` 回调返回的是 `std::optional<bool>`：返回 `nullopt` 表示「我不知道」，框架会退回按「没注册」处理。这个细节在 Ch 19 讲动态 legality 时会用到。

还有两个容易忽略的点：

- **legal 可以是递归的**。`markOpRecursivelyLegal` 表示「这个 op 合法，它里面嵌套的 op 也全部合法」，框架会跳过整个子树（`DialectConversion.h:1177-1199`）。
- **`builtin.unrealized_conversion_cast` 通常被标成 legal**。LLVM 转换的公共 target 就干了这件事（`LLVMCommon/ConversionTarget.cpp:14-18`），否则转换过程中临时插进来的桥会被当成非法 op。Ch 19 会解释这个设计带来的后果。

## 核心概念二：partial 与 full 只差失败策略

很多人以为 `applyFullConversion` 会「更努力地转换」，这是错的。两个 API 走同一个 driver、同一批 patterns、同一套 legality 判断，唯一区别是**某个 op 无法 legalize 时怎么处理**：

```cpp
// DialectConversion.cpp:3320-3340（简化）
if (failed(opLegalizer.legalize(op))) {
  if (mode == OpConversionMode::Full) {
    emitFailedToLegalizeDiag(/*wasExplicitlyIllegal=*/false);
    return failure();
  }
  if (mode == OpConversionMode::Partial) {
    if (opLegalizer.isIllegal(op)) {
      emitFailedToLegalizeDiag(/*wasExplicitlyIllegal=*/true);
      return failure();
    }
    // 非显式非法的 op：放过，记进 unlegalizedOps
  }
}
```

翻成规则：

| 场景 | Partial | Full |
|---|---|---|
| op 是 Legal | 留下 | 留下 |
| op 是 Illegal，pattern 成功 | 转换 | 转换 |
| op 是 Illegal，pattern 失败 | **pass 失败**（报 explicitly marked illegal） | pass 失败 |
| op 是 Unknown，pattern 成功 | 转换 | 转换 |
| op 是 Unknown，pattern 失败 | **放过，pass 继续** | **pass 失败** |

所以：

- **partial 的承诺是「非法的一个都不留」**，不是「能转的全转」。
- **full 的承诺是「结束时个个合法」**，它不帮你多写 pattern，只是不接受残留。

这也解释了一个常见困惑：只跑 `convert-arith-to-llvm`，`math.absf` 会原样留下且退出码为 0。不是 pass 有 bug，而是 partial 模式允许它留下。

## 核心概念三：ConversionPattern 不是普通 RewritePattern

两者都继承 `RewritePattern`，但契约完全不同。头文件第一句就说清了用途：

> Base class for the conversion patterns. ... patterns of this type can only be used with the 'apply*' methods below.
> —— `DialectConversion.h:599-601`

具体差在四处：

| 维度 | RewritePattern | ConversionPattern |
|---|---|---|
| 驱动 | greedy / canonicalize | 只有 `apply*Conversion` |
| 输入 operand | 原始 IR 的 operand | 已被目标类型替换后的 operand（adaptor） |
| 结果检查 | 无 | 替换结果必须能过 legality / 类型转换 |
| 失败恢复 | 一般直接 `failure()` | 支持 rollback，可换下一条 pattern |

最容易被忽略的是第二点。看 `ConversionPattern` 的签名：

```cpp
// DialectConversion.h:611-623
virtual LogicalResult
matchAndRewrite(Operation *op, ArrayRef<Value> operands,
                ConversionPatternRewriter &rewriter) const;

virtual LogicalResult
matchAndRewrite(Operation *op, ArrayRef<ValueRange> operands,
                ConversionPatternRewriter &rewriter) const;
```

`operands` 不是 `op->getOperands()`，而是**经过 TypeConverter 映射后的值**。1:1 用第一个重载，1:N 用第二个（每个 operand 对应一个 `ValueRange`）。如果你在 pattern 里直接读 `op.getOperand(0)`，拿到的是旧类型的值；在类型改变的场景里，这就是 bug 的源头。

还有两个副作用：

- pattern 可以只改 op 的一部分再返回 `failure()` 吗？不行。driver 会记录这一轮的修改并在失败时回滚，但前提是配置允许（`allowPatternRollback`）。别依赖「失败后 IR 还是原样」以外的任何假设。

## 核心概念四：`replaceOp` / `eraseOp` 的限制

`ConversionPatternRewriter` 的注释写得很直白：

```cpp
// DialectConversion.h:947-979
/// Replace the given operation with the new values. The number of op results
/// and replacement values must match. The types may differ: the dialect
/// conversion driver will reconcile any surviving type mismatches at the end
/// of the conversion process with source materializations.
void replaceOp(Operation *op, ValueRange newValues) override;

/// PatternRewriter hook for erasing a dead operation. The uses of this
/// operation *must* be made dead by the end of the conversion process,
/// otherwise an assert will be issued.
void eraseOp(Operation *op) override;
```

拆成四条硬约束：

1. **结果数量必须相等。** `replaceOp` 第一行就是断言 `op->getNumResults() == newValues.size()`（`DialectConversion.cpp:2169-2170`）。1:N 替换要用 `replaceOpWithMultiple`。
2. **类型可以不等，但必须能收尾。** 替换值类型和原结果不同时，driver 会插 materialization；收尾时补不出来就报错。这不是免费的，Ch 18/19 会反复踩。
3. **`eraseOp` 等价于「用空值替换」。** 源码里它就是构造一个全空的 replacement 列表（`DialectConversion.cpp:2208-2209`），所以「op 还有用户」在转换结束时会变成 unresolved materialization 失败。
4. **op 不是立刻删除的。** `replaceOp` 只是把 op 从 block 里摘下来，真正 `erase` 发生在 cleanup（`DialectConversion.cpp:1306-1323`）。因此在 `replaceOp` 之后继续持有并访问这个指针，是未定义行为；再对它调 `replaceOp` 会触发「已替换」断言（`DialectConversion.cpp:1558-1561`）。

这也是为什么 `applySignatureConversion` 会断言「不能对已被替换的 op 里的 block 做签名转换」（`DialectConversion.cpp:2216-2222`）。整棵子树的修改顺序必须自洽。

## 底层机制：一次 legalize 到底发生了什么

把 driver 压缩成一个流程（对应 `DialectConversion.cpp` 的 `OperationConverter`）：

```text
1. 收集要处理的 op（默认从 root 往下走，跳过 recursively-legal 子树）
2. 对每个 op：
   a. target 说 legal？跳过
   b. 否则按 benefit/depth 顺序试 ConversionPattern
   c. 某条成功：记录 rewrite，继续下一个 op
   d. 全失败：按 partial/full 策略决定放过还是报错
3. 统一 apply 所有 rewrite（或失败时回滚）
4. reconcile 框架自己插入的 unrealized_conversion_cast
5. 把仍然活着的 materialization 落实成真实 op；落不实就失败
```

第 4、5 步是很多人忽略的收尾，源码在 `DialectConversion.cpp:3481-3512`：

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

两个细节值得记住：

- **框架插的 cast 和 pattern 插的 cast 是两回事**。注释说得很清楚：只 reconcile 框架自己插的。你自己在 pattern 里手写的 `unrealized_conversion_cast` 不在这套账里。
- **materialization 是「延迟结算」的**。转换过程中先记一笔，最后统一兑现；兑现不了就是 pass 失败，而不是悄悄留个 cast。

下一章会展开「兑现」用的 TypeConverter；本章先把 target 和失败语义跑出来看。

## 示例一：partial conversion 只动它负责的 dialect

准备 `arith.mlir`：

```mlir
module {
  func.func @add(%a: i32, %b: i32) -> i32 {
    %0 = arith.addi %a, %b : i32
    %1 = arith.muli %0, %b : i32
    return %1 : i32
  }
}
```

只跑 `convert-arith-to-llvm`：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\arith.mlir --pass-pipeline='builtin.module(convert-arith-to-llvm)'
```

实测输出（退出码 0）：

```mlir
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    %1 = llvm.mul %0, %arg1 : i32
    return %1 : i32
  }
}
```

三件事同时发生：

1. `arith.addi` / `arith.muli` 变成了 `llvm.add` / `llvm.mul`；
2. `func.func` 和 `func.return` **原样保留**；
3. 没有报错，也没有提示「还有 func 没降」。

原因在源码里：这个 pass 用的是 partial conversion，而且 target 只把 LLVM dialect 标成 legal，其余全都没注册：

```cpp
// ArithToLLVM.cpp:709-723
LLVMConversionTarget target(getContext());
...
if (failed(applyPartialConversion(getOperation(), target,
                                  std::move(patterns))))
  signalPassFailure();
```

`LLVMConversionTarget` 的构造函数只做两件事：LLVM dialect legal、`unrealized_conversion_cast` legal（`LLVMCommon/ConversionTarget.cpp:14-18`）。`func` 是 Unknown，partial 模式下失败也放过。

## 示例二：partial 的成功不代表降级完成

把 `arith` 换成一个 partial pass 不认识的 dialect：

```mlir
module {
  func.func @abs(%a: f32) -> f32 {
    %0 = math.absf %a : f32
    return %0 : f32
  }
}
```

同一个 pass：

```powershell
& "$mlir\mlir-opt.exe" .\math.mlir --pass-pipeline='builtin.module(convert-arith-to-llvm)'
```

实测输出（退出码 0）：

```mlir
module {
  func.func @abs(%arg0: f32) -> f32 {
    %0 = math.absf %arg0 : f32
    return %0 : f32
  }
}
```

IR 一个字都没改，pass 依然成功。这不是「没问题」，而是 **partial 只对显式非法的 op 负责**。`math` 在 target 里是 Unknown，pattern 也不匹配，于是被放过（`DialectConversion.cpp:3329-3340`）。

同样的道理适用于 `convert-scf-to-cf`：它显式把 `scf` 的几个 op 标成 illegal，同时把未知 op 标成动态合法：

源码依据：SCFToControlFlow.cpp:734-743 里，addIllegalOp<scf::...> 列了七个必须消失的 op，紧接着 markUnknownOpDynamicallyLegal([](Operation *) { return true; }) 把其余全部放行。

拿 `math.mlir` 跑 `func.func(convert-scf-to-cf)`，输出同样原样不变。**只要 target 没把某个 op 标成 illegal，partial 就没有义务动它。**

## 示例三：full conversion 会因「目标不合法」而失败

`convert-linalg-to-std` 是少数使用 full conversion 的内置 pass：

```cpp
// LinalgToStandard.cpp:141-151
ConversionTarget target(getContext());
target.addLegalDialect<affine::AffineDialect, arith::ArithDialect,
                       func::FuncDialect, memref::MemRefDialect,
                       scf::SCFDialect>();
target.addLegalOp<ModuleOp>();
...
if (failed(applyFullConversion(module, target, std::move(patterns))))
  signalPassFailure();
```

对同一个 `math.mlir` 跑它：

```powershell
& "$mlir\mlir-opt.exe" .\math.mlir --pass-pipeline='builtin.module(convert-linalg-to-std)'
```

实测 stderr（退出码 1，PowerShell 会把 stderr 包成 `NativeCommandError`，下面只保留程序本身的诊断）：

```text
.\math.mlir:3:10: error: failed to legalize operation 'math.absf': %0 = "math.absf"(%arg0) <{fastmath = #arith.fastmath<none>}> : (f32) -> f32
    %0 = math.absf %a : f32
         ^
.\math.mlir:3:10: note: see current operation: %0 = "math.absf"(%arg0) <{fastmath = #arith.fastmath<none>}> : (f32) -> f32
```

这就是**目标不合法导致的失败**：target 只列了 affine/arith/func/memref/scf + module，`math` 是 Unknown；full 模式不接受「legalize 失败」，于是整个 pass 失败。

对照实验：把同一个 pass 跑在只含 `func` + `arith` 的 `arith.mlir` 上，退出码 0，IR 原样输出。因为这两个 dialect 本来就在 legal 名单里——**full 不要求「发生转换」，只要求「结束时没有非法 op」**。

顺带回答一个常见疑问：为什么报错里没有 `explicitly marked illegal`？因为 `math` 是 Unknown。显式非法的消息是 `failed to legalize operation '<op>' that was explicitly marked illegal`（源码 `DialectConversion.cpp:3311-3317`）。

在 partial 模式下，legalize 失败只有这一种会让 pass 失败；收尾的 materialization 失败另算（Ch 19）。在 full 模式下，任何 legalize 失败都会失败。

## 对比与取舍

canonicalize 与 conversion 的分工在 Ch 7 已经立住：前者让 IR 更规范，后者让 IR 落到另一层。本章只讨论后者。

### partial vs full

| 维度 | `applyPartialConversion` | `applyFullConversion` |
|---|---|---|
| 典型用法 | 增量降级，一个 pass 管一个 dialect | 最终收口，确认没有漏网之鱼 |
| Unknown op | 放过 | 必须 legalize |
| 内置例子 | arith/scf/cf/vector→llvm | linalg→std、gpu→spirv |
| 风险 | 以为降完了，其实还有一堆 | 上游没降干净就整段失败 |

我们的建议：

- **写降级 pass 时默认 partial**。你只对自己声明的 dialect 负责，别替别的 pass 做决定。
- **在 pipeline 末尾放一个 full conversion 做验收**。它不需要写新 pattern，只要 target 足够严格，就能把「漏降」变成显式错误。
- **自己的 pass 如果要求严格，显式 `markUnknownOpDynamicallyLegal([](Operation*) { return false; })`**，别依赖 full 的默认语义。

## 踩雷集锦

1. **把 partial 当成「尽力转换」。** partial 只保证「显式非法的 op 被处理」，Unknown 失败会被静默放过。示例二就是活证据。
2. **看到退出码 0 就以为降级完成。** 判断标准是 target 的合法性，不是退出码。跑完一定要 `grep` 一下目标 dialect 是否还在。
3. **在 ConversionPattern 里读原始 operand。** `op.getOperand(0)` 是旧类型；要用 `matchAndRewrite` 传进来的 adaptor。
4. **`replaceOp` 后继续用旧 op 指针。** op 只是被摘下来，cleanup 才真正 erase；之后再访问或替换它都违反 API 契约。
5. **结果数不匹配。** `replaceOp` 要求一一对应；1:N 用 `replaceOpWithMultiple`。Optimized build 下断言不触发，后果可能是一段难以定位的崩溃。
6. **`eraseOp` 一个还有用户的 op。** 转换结束时用户还在，就会变成 unresolved materialization 失败；这不是「删除失败」，而是「没人接盘」。
7. **以为 target 是全局的。** 每个 pass 自己建 target。调 pass 顺序时，legality 也跟着换，别拿上一个 pass 的结论套下一个。

## 进阶：再往深一层

- **`applyAnalysisConversion`**（`DialectConversion.h:1546-1549`）只分析不改 IR，用来回答「这些 op 能不能降」。写大 pipeline 前可以先探测，避免跑到一半才失败。
- **递归 legal**：`markOpRecursivelyLegal` 能让 `llvm.func` 内部的整棵子树跳过检查（`DialectConversion.h:1177-1199`）。用得好是性能优化，用不好会漏掉内部非法 op。
- **观察每次 legalize**：`--mlir-print-ir-after-all` 能看到 pass 结束后的 IR，但看不到 pattern 级别的尝试；pattern 级调试要自己加 `notifyMatchFailure` 或自建 assertions 版本（本机是 Optimized build）。

## 动手练习

统一约定：在 `.scratch\mlir-17-19\` 下新建文件，`$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"`。每题至少跑三个用例，记录退出码和关键 IR。

### 练习 1：找出 partial 的边界

**任务**：从 `arith.mlir` 出发，逐步加入 `math.absf`、`scf.if`、`cf.br`，每次只跑 `convert-arith-to-llvm`，画出「哪些 op 被转换、哪些被留下」。

**测试用例**：(a) 纯 arith；(b) arith + math；(c) arith + scf.if + cf.br。

**步骤**：1) 复制文件并加 op；2) 跑 pass 并保存输出；3) 用 `Select-String` 统计 `llvm.`、`arith.`、`math.`、`scf.` 的出现次数；4) 对照 target 源码解释差异。

<details><summary>参考解答</summary>

只有 arith 会被转换；math/scf/cf 全部保留，退出码均为 0。因为 `ArithToLLVM` 的 target 只有 LLVM legal，其余都是 Unknown，partial 允许它们 legalize 失败。

</details>

**常见卡点**：把「输出里还有 arith」当成 pass 失败——其实要看的是 target 声明了哪些 illegal。

### 练习 2：让 full conversion 失败，再解释为什么修不好

**任务**：对 `math.mlir` 跑 `convert-linalg-to-std`，记录完整报错；然后在它前面加 `convert-math-to-llvm`，再跑一次。

**测试用例**：(a) 原样；(b) 先 math→llvm；(c) 先 math→llvm 再把 pass 换成 `convert-to-llvm`。

**步骤**：1) 保存 stderr；2) 对比两次失败信息；3) 读 `LinalgToStandard.cpp:141-151` 的 legal 名单，指出 LLVM 在不在里面；4) 解释「先降 math」为什么救不了 full。

<details><summary>参考解答</summary>

(a) 报 `failed to legalize operation 'math.absf'`；(b) 仍然失败，因为 `llvm` 不在 linalg target 的 legal 名单里，`llvm.abs` 变成新的 Unknown；(c) `convert-to-llvm` 是 partial + 接口驱动，能过。结论：full 的 legal 名单必须覆盖「转换后会出现的一切」，只补前半段没用。

</details>

**常见卡点**：以为 full 失败是 pattern 不够；实际是 target 没把结果 dialect 标 legal。

### 练习 3：拼一条能跑通的最小降级链

**任务**：把 `func.func` + `arith` 的模块降到 `llvm.func` + `llvm.add`，要求输出里没有 `unrealized_conversion_cast`。

**测试用例**：(a) 只跑 `convert-func-to-llvm`；(b) 只跑 `convert-arith-to-llvm`；(c) 先 `func.func(convert-arith-to-llvm)` 再 `convert-func-to-llvm`。

**步骤**：1) 三次分别保存 IR；2) 统计 cast 数量；3) 解释 (a) 为什么会出现 cast；4) 用 `--mlir-print-ir-after-all` 复核顺序。

<details><summary>参考解答</summary>

(a) 函数签名先变 `i64`，函数体还是 `arith`/`index`，driver 插 cast；(b) arith 变 llvm，但 `func.func` 还在；(c) 无 cast。关键是把 arith 的转换放在 func 签名重写之前，二者对 `index` 的映射一致。

</details>

**常见卡点**：pass 顺序写成 `convert-func-to-llvm` 在前；或 arith 用了非默认 `index-bitwidth`（Ch 19）。

### 练习 4：从源码读出一个 pass 的 legality

**任务**：选 `convert-cf-to-llvm`，在 `mlir\lib\Conversion\ControlFlowToLLVM\ControlFlowToLLVM.cpp` 里找出 target 配置，写出「哪些 dialect legal、哪些 op illegal、unknown 怎么处理」。

**测试用例**：(a) 纯 cf；(b) cf + func；(c) cf + 一个未知 dialect op（加 `--allow-unregistered-dialect`）。

**步骤**：1) `Select-String` 搜 `ConversionTarget`、`addLegal`、`addIllegal`、`applyPartial`；2) 做一张 legality 表；3) 用三个用例验证预测。

<details><summary>参考解答</summary>

该 pass 用 `LLVMConversionTarget`，因此 LLVM legal、unrealized cast legal；`cf` 由 pattern 覆盖，未知 op 在 partial 下可留下。用例 (c) 中未知 op 应原样保留。

</details>

**常见卡点**：只读 pass 的 `--help` 描述，不读 target 构造；描述里不会写 legality。

### 练习 5：为自定义方言设计 target（guided）

**任务**：假设有 `foo.add %a, %b : i32` 要降到 `arith.addi`，写出 `ConversionTarget` 与 pattern 的骨架（C++ 伪代码，不要求编译）。

**测试用例**：(a) 只有 foo.add；(b) foo.add + arith；(c) foo.add 的操作数类型是 `f32`（应拒绝）。

**步骤**：1) 决定 foo dialect 是 illegal 还是动态 legal；2) 写 `OpConversionPattern<foo::AddOp>`；3) 用 `notifyMatchFailure` 处理 f32；4) 说明为什么用 partial。

<details><summary>参考解答</summary>

`target.addIllegalDialect<foo::FooDialect>(); target.addLegalDialect<arith::ArithDialect>();`，pattern 用 `OpConversionPattern<foo::AddOp>`，检查 i32 后创建 `arith::AddIOp` 并 `replaceOp`。

`f32` 用例应 `notifyMatchFailure`，让 pass 以「显式非法但无法降级」失败，而不是生成错误 IR。

</details>

**常见卡点**：把 foo 标成 dynamic legal 却忘了回调；或在 pattern 里直接 `eraseOp` 而不产生替代值。

### 练习区常见卡点

- 用退出码判断降级是否完成；正确做法是检查目标 dialect 是否绝迹。
- 在 `builtin.module(...)` 里把 module-level pass 塞进 `func.func(...)`，报「not a registered pass or pass pipeline」。
- 改了 `index-bitwidth` 却只改一个 pass，留下一串 cast（Ch 19 专门处理）。

### 练习区自我检核

- [ ] 我能不看笔记说出 partial/full 的失败策略差异
- [ ] 我能用源码里的 target 配置预测一次转换的结果
- [ ] 我能解释「退出码 0 但 IR 没变」的三种可能原因

## 本章重点整理

- `applyPartialConversion` 只对显式 illegal 的 op 负责；Unknown 失败会被放过。
- `applyFullConversion` 不接受任何未 legalize 的 op；它不会更努力，只会更严格。
- ConversionPattern 只能配 `apply*Conversion`，operand 是转换后的值，替换结果要过 legality。
- `ConversionPatternRewriter::replaceOp` 要求结果数匹配、类型可收尾；op 延迟删除，替换后不得再访问。
- `eraseOp` 等价于空替换：用户必须在此后被处理掉，否则收尾失败。

## 自我检核

- [ ] 我能画出 target / pattern / rewriter 三件套的关系图
- [ ] 我能说出 Unknown op 在 partial 与 full 下的两种命运
- [ ] 我能解释 `math.absf` 为什么能在 `convert-arith-to-llvm` 后存活
- [ ] 我能复现并解释 `failed to legalize operation 'math.absf'`
- [ ] 我能说出 ConversionPattern 与 RewritePattern 的至少三处差异
- [ ] 我知道 `replaceOp` 之后为什么不能再用旧 op 指针
- [ ] 我能解释 `eraseOp` 与 unresolved materialization 失败的关系

## 延伸阅读

- **[MLIR 文档：Dialect Conversion](https://mlir.llvm.org/docs/DialectConversion/)**
  - **读哪里**：Legality、Conversion Patterns、Type Conversion 三节。
  - **学什么**：官方对 partial/full、dynamic legality 的定义。
  - **和本章的关联**：本章的规则表就是这三节的工程化版本。

- **[mlir/include/mlir/Transforms/DialectConversion.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Transforms/DialectConversion.h)**
  - **读哪里**：`ConversionTarget`（1078-1270）、`ConversionPattern`（602-627）、`ConversionPatternRewriter`（839-990）。
  - **学什么**：每个 API 的契约与限制。
  - **和本章的关联**：核心概念一至四的来源。

- **[mlir/lib/Transforms/Utils/DialectConversion.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Transforms/Utils/DialectConversion.cpp)**
  - **读哪里**：`OperationConverter::convert`（3308-3348）、`ConversionTarget::isLegal`（3978-4011）、收尾逻辑（3481-3512）。
  - **学什么**：partial/full 的真实分支与 materialization 结算。
  - **和本章的关联**：底层机制一节逐行对应。

- **[mlir/lib/Conversion/LinalgToStandard/LinalgToStandard.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/LinalgToStandard/LinalgToStandard.cpp)**
  - **读哪里**：`runOnOperation`（141-152）。
  - **学什么**：一个最小 full conversion pass 的 target 长什么样。
  - **和本章的关联**：示例三的失败就来自这份 legal 名单。

---

下一章把本章反复提到的「类型可收尾」拆开：TypeConverter 如何把一个类型映射成多个值，函数签名怎么重写，materialization 为什么不是可选项。

→ [Ch 18 TypeConverter 与 1:N 降级](./18-type-converter.md)
