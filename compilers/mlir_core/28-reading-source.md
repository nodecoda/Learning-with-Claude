# Ch 28 读 MLIR 源码：从命令行走到底层实现

> **目标**：把“读 MLIR 源码”从漫游变成可复现的追踪。学完本章，你能拿到任意一个 `mlir-opt` flag、任意一个 op 名、任意一条 lowering，在几分钟内定位到它的定义、生成物和实现文件，并判断哪一段是手写的、哪一段是 TableGen 生成的。

## 为什么需要这个？

前面 27 章讲的是“怎么用”。真实工作里更常见的是“它为什么这样”。你写的 pattern 不匹配、你的 op 过不了 verifier、某条 lowering 在某个 op 上悄悄不动了——这些问题的答案不在文档里，在源码里。

MLIR 的源码有三个特点，决定了不能“从第一行读到最后一行”：

- **7493 个文件**（本机 llvmorg-23.1.4 快照实测）。按目录顺序读，读到 Ch 30 也读不完。
- **大量代码是生成的**。你在 `.cpp` 里找不到 `arith.addi` 的 verifier，因为它在 `ArithOps.cpp.inc` 里；你在 `lib/Transforms` 里也找不到 `registerTransformsPasses()` 的函数体，因为它在生成的 `Passes.h.inc` 里。
- **同名概念分散在三层**：`.td` 声明意图，`.inc` 提供机械实现，`.cpp` 只写“机器写不出来的部分”。

所以本章不列目录，而是训练三条追踪路线。三条都会在本机真跑，输出原样贴出。

## 先建立直觉：源码树是三层同心圆

```text
        test/          行为规格：这个 pass 应该产出什么 IR
          |
      include/         契约：.td 声明 op/pass，.h 暴露 API
          |
         lib/          实现：真正改 IR 的 C++
          |
      tools/           装配：mlir-opt 把注册表和 main 接起来
```

三条经验，先记住，后面反复用：

1. **从 `tools/` 进，不从 `lib/` 进**。`mlir-opt` 的 `main` 只有二十几行，但它决定了“注册了什么”。
2. **看到 `createXXXPass()` 就去找 `.td`**。这个函数几乎总是生成的，函数体是 `make_unique<DerivedT>()`。
3. **看到 `getODSOperands(0)` 就数 `.td` 里的 operand 顺序**。`.inc` 里的下标不是猜的，是 ODS 声明顺序。

## 目录地图：每个目录什么时候去

| 目录 | 文件数 | 典型文件 | 什么时候来这找 |
|---|---|---|---|
| `mlir/include/mlir/IR` | 89 | `Operation.h`、`OpDefinition.h`、`BuiltinTypes.h` | op/region/value 的 C++ 表示；写 op 时查 `Op<>` 提供了什么 |
| `mlir/include/mlir/Pass` | 7 | `Pass.h`、`PassManager.h`、`PassRegistry.h` | pass 生命周期、pipeline 解析、注册表 |
| `mlir/include/mlir/Transforms` | 24 | `Passes.td`、`DialectConversion.h`、`GreedyPatternRewriteDriver.h` | 内置 pass 的声明与 conversion 框架契约 |
| `mlir/include/mlir/Conversion` | 3 | `Passes.td`、`Passes.h` | **上游大部分 lowering pass 的 flag 名集中在这一个 `.td` 里** |
| `mlir/include/mlir/Dialect` | 771 | `Arith/IR/ArithOps.td`、`SCF/IR/SCFOps.td` | op 定义、assemblyFormat、trait 列表 |
| `mlir/include/mlir/Interfaces` | 57 | `ControlFlowInterfaces.td`、`DataLayoutInterfaces.h` | op 之间的协议（不是某个 dialect 私有的） |
| `mlir/include/mlir/Rewrite` | 3 | `PatternApplicator.h`、`FrozenRewritePatternSet.h` | pattern 怎么被排序、怎么被尝试 |
| `mlir/lib/IR` | 48 | `Operation.cpp`、`Verifier.cpp` | 框架本身怎么实现 |
| `mlir/lib/Pass` | 9 | `Pass.cpp`、`PassRegistry.cpp` | pass 运行机制 |
| `mlir/lib/Transforms` | 35 | `Canonicalizer.cpp`、`CSE.cpp`、`InlinerPass.cpp` | 跟 dialect 无关的通用 pass |
| `mlir/lib/Conversion` | 235 | `SCFToControlFlow/SCFToControlFlow.cpp` | 每条具体 lowering 的实现 |
| `mlir/tools/mlir-opt` | 2 | `mlir-opt.cpp` | 命令行 flag 的来源 |
| `mlir/test` | 2937 个 `.mlir` | `Conversion/SCFToControlFlow/convert-to-cfg.mlir` | 这个 pass 到底保证什么 |

两个反直觉的点，先记下来：

- **没有 `lib/Pass/PassManager.cpp`**（实测 `Test-Path` 为 `False`）。`PassManager` 的实现就在 `lib/Pass/Pass.cpp` 里，`PassManagerOptions.cpp` 只管命令行选项。
- **没有 `lib/Transforms/Passes.cpp`**。`registerTransformsPasses()` 是 generated header 里的 `inline` 函数，不在任何 `.cpp` 里。

## 核心概念一：`.td` / `.h.inc` / `.cpp` 三段式

拿 `arith.addi` 走一遍。第一站是声明（`源码依据`，`ArithOps.td:271-272`）：

```tablegen
def Arith_AddIOp : Arith_IntBinaryOpWithOverflowFlags<"addi",
    [Commutative, DeclareOpInterfaceMethods<InferIntDivisibilityOpInterface>]> {
```

基类在 `ArithOps.td:193`，给了 `assemblyFormat`（`:203-204`），所以 `addi` 的语法不是手写的。

第二站是生成物。`Select-String` 实测命中（`ArithOps.h.inc`）：

```text
1706:  static constexpr ::llvm::StringLiteral getOperationName() {
1707:    return ::llvm::StringLiteral("arith.addi");
1714:  ::mlir::Operation::operand_range getODSOperands(unsigned index) {
```

`ArithOps.h.inc:1720-1726` 的 `getLhs()` 就是 `getODSOperands(0)` 加一个 cast。命名规律由此确定：**`.td` 里叫 `lhs`，C++ 就是 `getLhs()`，底层是 `getODSOperands(0)`**；`rhs` 是下标 1。

第三站是 verifier。`ArithOps.cpp.inc:1357-1387` 的 `AddIOp::verifyInvariantsImpl()` 对两个 operand 各跑一次类型约束，实测报错含 `'arith.addi' op operand #0 must be signless-non-zero-bitwidth-integer-like, but got 'f32'`。

最后是手写语义（`ArithOps.cpp:419-443`，节选）：

```cpp
OpFoldResult arith::AddIOp::fold(FoldAdaptor adaptor) {
  if (matchPattern(adaptor.getRhs(), m_Zero()))   // addi(x, 0) -> x
    return getLhs();
  ...
}
void arith::AddIOp::getCanonicalizationPatterns(RewritePatternSet &patterns,
                                                MLIRContext *context) {
  patterns.add<AddIAddConstant, AddISubConstantRHS, ...>(context);
}
```

**判断法则**：`.inc` 是“每个 op 都长这样”的机械代码；`.cpp` 是“这个 op 特有”的语义。`fold`、`getCanonicalizationPatterns`、`verify` 的定制重载才是人要读的。

`.inc` 的落点有规律：`Arith.h:49` 收 `ArithOps.h.inc`（声明），`ArithOps.cpp:3093` 收 `ArithOps.cpp.inc`（定义，**在文件最末尾**），`ArithDialect.cpp:45` 再收一次用于注册。所以你在 3093 行之前找不到 `AddIOp::build`。

## 核心概念二：注册表是一条链，不是一张表

`--canonicalize` 能用，是因为有人在启动时把这个名字和一个工厂函数塞进了全局注册表。链路如下（全部 `源码依据`）：

```text
tools/mlir-opt/mlir-opt.cpp:325   int main(...)
tools/mlir-opt/mlir-opt.cpp:326     registerAllPasses();
lib/RegisterAllPasses.cpp:60        void mlir::registerAllPasses() {
lib/RegisterAllPasses.cpp:62          registerTransformsPasses();
lib/RegisterAllPasses.cpp:65          registerConversionPasses();
Passes.h.inc:2497                     inline void registerTransformsPasses() {
Passes.h.inc:2500                       registerCanonicalizerPass();
Passes.h.inc:2077                       inline void registerCanonicalizerPass() {
Passes.h.inc:2078                         ::mlir::registerPass([]{ return createCanonicalizerPass(); });
Passes.h.inc:298                        createCanonicalizerPass() -> impl::createCanonicalizerPass()
Passes.h.inc:288                          friend ... createCanonicalizerPass() { return make_unique<DerivedT>(); }
lib/Transforms/Canonicalizer.cpp:30       struct Canonicalizer : impl::CanonicalizerPassBase<Canonicalizer>
```

注意最后两行：**工厂函数是生成的，被 `make_unique` 出来的类是手写的**。这就是为什么你搜 `createCanonicalizerPass` 的定义只能搜到 `.inc`。

`--convert-scf-to-cf` 走同一条链，只是换成分支：

```text
include/mlir/Conversion/Passes.td:1171   def SCFToControlFlowPass : Pass<"convert-scf-to-cf">
include/mlir/Conversion/Passes.h.inc:9477  inline void registerConversionPasses()
include/mlir/Conversion/Passes.h.inc:9183  inline void registerSCFToControlFlowPass()
lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp:37  struct SCFToControlFlowPass
```

如果名字写错，报错来自 pipeline 解析器，不是 pass 本身：

```powershell
& "$mlir\mlir-opt.exe" .\demo.mlir --pass-pipeline='builtin.module(canonicalise)'
```

`实测`：

```text
<unknown>:0: error: MLIR Textual PassPipeline Parser:1:1: error: 'canonicalise' does not refer to a registered pass or pass pipeline
canonicalise
^
```

这条错误信息本身就是入口：它说明查找发生在注册表里，而注册表的填充只有上面那条链。

## 核心概念三：测试是源码的说明书

`lib` 里的代码回答“怎么实现”，`test` 里的代码回答“保证什么”。同一个 pass，两个文件对着读效率最高。

`mlir/test/Conversion/SCFToControlFlow/convert-to-cfg.mlir:1`：

```text
// RUN: mlir-opt -allow-unregistered-dialect -convert-scf-to-cf -split-input-file %s | FileCheck %s
```

第 4-14 行就是 `scf.for` 的标准期望形状（`cf.br` → `cf.cond_br` → 回边）。你在 `ForLowering::matchAndRewrite` 里读到的每个 `rewriter.` 调用，都能在这里找到对应的 `CHECK-NEXT`。

## 底层机制：一次 `scf.for` 降级到底改了哪些结构

`ForLowering::matchAndRewrite`（`SCFToControlFlow.cpp:329-399`）只有 70 行，却用到了改 IR 的全部基本动作：

```cpp
// 1. 当前 block 一分为二：前半是 init，后半是循环之后的代码
auto *initBlock = rewriter.getInsertionBlock();
auto initPosition = rewriter.getInsertionPoint();
auto *endBlock = rewriter.splitBlock(initBlock, initPosition);

// 2. region 首 block 当 condition；在第一条指令前切开，得到循环体首 block
auto *conditionBlock = &forOp.getRegion().front();
auto *firstBodyBlock = rewriter.splitBlock(conditionBlock, conditionBlock->begin());
auto *lastBodyBlock = &forOp.getRegion().back();
rewriter.inlineRegionBefore(forOp.getRegion(), endBlock);

// 3. 循环体末尾补 iv += step，再跳回 condition
auto stepped = arith::AddIOp::create(rewriter, loc, iv, step).getResult();
loopCarried.push_back(stepped);
loopCarried.append(terminator->operand_begin(), terminator->operand_end());
cf::BranchOp::create(rewriter, loc, conditionBlock, loopCarried);

// 4. init 末尾跳到 condition，实参是 lowerBound + initArgs
destOperands.push_back(lowerBound);
llvm::append_range(destOperands, forOp.getInitArgs());
cf::BranchOp::create(rewriter, loc, conditionBlock, destOperands);

// 5. condition 里生成 cmp + cond_br，最后用 block 参数替换 for 的结果
auto comparison = arith::CmpIOp::create(rewriter, loc, predicate, iv, upperBound);
cf::CondBranchOp::create(rewriter, loc, comparison, firstBodyBlock, ..., endBlock, ...);
rewriter.replaceOp(forOp, conditionBlock->getArguments().drop_front());
```

三个只有读过这段才成立的结论：

- **`iter_args` 不是语法糖，它就是 block argument**。第 4 步把 init 值当 `cf.br` 实参送进 condition，第 3 步从 `scf.yield` 的 operand 取下一轮的值。
- **结果靠 block argument 传递**。`replaceOp` 用的是 `conditionBlock->getArguments()`，因为它们在 `endBlock` 处支配原 `forOp` 的所有使用者；`drop_front()` 去掉 iv。
- **顺序是反的**：先建 body 的回边，再建 init 的边，最后填 condition。SSA 支配要求先有块、再有边、最后有终结符。

再看收口（`SCFToControlFlow.cpp:730-743`）：

```cpp
void SCFToControlFlowPass::runOnOperation() {
  RewritePatternSet patterns(&getContext());
  populateSCFToControlFlowConversionPatterns(patterns);
  ConversionTarget target(getContext());
  target.addIllegalOp<scf::ForallOp, scf::ForOp, scf::IfOp, scf::IndexSwitchOp,
                      scf::ParallelOp, scf::WhileOp, scf::ExecuteRegionOp>();
  target.markUnknownOpDynamicallyLegal([](Operation *) { return true; });
  if (failed(applyPartialConversion(getOperation(), target, std::move(patterns), config)))
    signalPassFailure();
}
```

它叫 conversion，用的是 `applyPartialConversion`，却只把 7 个 op 标成 illegal。所以 `convert-scf-to-cf` 永远不会因为“还有 arith”失败——Ch 17 的 partial 语义在这里是活的。另外，`populateSCFToControlFlowConversionPatterns`（`:722-728`）给 `DoWhileLowering` 设了 `benefit=2`：多个 pattern 竞争同一 op 时，benefit 高的先试。

## 示例一：从 `--canonicalize` 追到 `Canonicalizer.cpp`

```powershell
& "$mlir\mlir-opt.exe" --help 2>&1 | Select-String -Pattern '^\s+--canonicalize' -Context 0,2
```

`实测`（节选）：

```text
--canonicalize   -   Canonicalize operations
  --cse-between-iterations  - Run full CSE between each pattern-application iteration. ...
  --disable-patterns=<string>  - Labels of patterns that should be filtered out during application
```

子选项来自 `Passes.td:19-40` 的 `Option<...>`，生成到 `Passes.h.inc:270-285`。**`canonicalize` 的这些子选项，都能在 `.td` 找到同名 `Option`**。

用一个能体现 fold 的输入：

```mlir
func.func @f(%x: i32, %y: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %a = arith.addi %x, %c0 : i32
  %s = arith.subi %y, %y : i32
  %r = arith.addi %a, %s : i32
  return %r : i32
}
```

`实测` `--canonicalize` 的输出：

```mlir
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    return %arg0 : i32
  }
}
```

三步折叠都能对上源码：`subi(y, y)` 折成 0，`addi(x, 0)` 由 `ArithOps.cpp:419-422` 折成 `x`，最后一个 `addi` 折成 `x` 本身。没有一条 pattern 是专为这段代码写的。

谁调用了这些 fold？`Canonicalizer.cpp:74-80`：

```cpp
for (auto *dialect : context->getLoadedDialects())
  if (isAllowed(dialect))
    dialect->getCanonicalizationPatterns(owningPatterns);
for (RegisteredOperationName op : context->getRegisteredOperations())
  if (isAllowed(&op.getDialect()))
    op.getCanonicalizationPatterns(owningPatterns, context);
```

**读法**：`getCanonicalizationPatterns` 分 dialect 级和 op 级两层。查“某个 op 为什么被 canonicalize 了”去 op 的 `.cpp`；查“某个 dialect 注册了什么”去 dialect 的 `.cpp`。

## 示例二：从 `arith.addi` 追到定义（五分钟流程）

**第 1 分钟：确认 operand 顺序。** generic 形式直接暴露 ODS 下标：

```powershell
& "$mlir\mlir-opt.exe" .\demo.mlir --mlir-print-op-generic
```

`实测`（节选）：

```mlir
%7 = "arith.addi"(%arg3, %6) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
```

两个 operand、一个结果、一个 `overflowFlags` 属性；顺序就是 `getODSOperands(0/1)`。

**第 2 分钟：找 `.td`。**

```powershell
$src = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-src\mlir"
Get-ChildItem -Path "$src\include\mlir\Dialect" -Recurse -Filter *.td |
  Select-String -Pattern 'def \w+_AddIOp'
```

`实测` 命中 `ArithOps.td:271`。别依赖 `<Dialect>_<Op>` 命名规律：`linalg` 的 `def MatmulOp : LinalgStructuredBase_Op<...>` 就没有前缀，搜 mnemonic 更稳。

**第 3 分钟：读生成物。** `ArithOps.h.inc:1676` 一行给出完整 trait 列表：`OneResult`、`NOperands<2>`、`SameOperandsAndResultType`、`Elementwise`、`Scalarizable`/`Vectorizable`/`Tensorizable`。**读 trait 比读文档快**：它决定这个 op 能出现在哪些 context、能被哪些通用 pass 处理。

**第 4-5 分钟：** verifier（`ArithOps.cpp.inc:1357`）与手写语义（`ArithOps.cpp:419`）已在核心概念一走过。这里补一个反例：`%r = arith.addi %a, %b : i32` 里 `%b` 是 `f32`，实测报 `use of value '%b' expects different type than prior uses: 'i32' vs 'f32'`。这条不是 ODS verifier 发的，是 **parser 的 SSA 类型一致性检查**。同一个 `addi`，两类错来自两个阶段，先分清阶段再翻代码。

## 示例三：从 `scf.for` 追到 lowering 的实际输出

```powershell
& "$mlir\mlir-opt.exe" .\demo.mlir `
  --pass-pipeline='builtin.module(func.func(convert-scf-to-cf))' `
  --mlir-print-ir-after-all 2>&1 | Select-Object -First 24
```

`实测`（只留 `@g` 这一段）：

```mlir
// -----// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //----- //
func.func @g(%arg0: index) -> i32 {
  %c0_i32 = arith.constant 0 : i32
  %c1_i32 = arith.constant 1 : i32
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  cf.br ^bb1(%c0, %c0_i32 : index, i32)
^bb1(%0: index, %1: i32):  // 2 preds: ^bb0, ^bb2
  %2 = arith.cmpi slt, %0, %arg0 : index
  cf.cond_br %2, ^bb2, ^bb3
^bb2:  // pred: ^bb1
  %3 = arith.addi %1, %c1_i32 : i32
  %4 = arith.addi %0, %c1 : index
  cf.br ^bb1(%4, %3 : index, i32)
^bb3:  // pred: ^bb1
  return %1 : i32
}
```

| 源码 | 输出 | 说明 |
|---|---|---|
| `:338` `splitBlock` | `^bb0` 与 `^bb1` 分离 | 循环之后的代码落到 `^bb3` |
| `:348` `inlineRegionBefore` | `^bb1`/`^bb2` 进入函数 | region 被拉平成 CFG |
| `:357` `arith::AddIOp::create` | `%4 = arith.addi %0, %c1` | iv 自增 |
| `:382` `cf::BranchOp` | `cf.br ^bb1(...)` | 入口传 lowerBound + initArgs |
| `:389` `arith::CmpIOp` | `%2 = arith.cmpi slt` | `unsignedCmp` 时是 `ult` |
| `:392` `cf::CondBranchOp` | `cf.cond_br %2, ^bb2, ^bb3` | 真出口是 `^bb3`（endBlock） |
| `:397` `replaceOp` | `return %1` | `%1` 是 `drop_front()` 后的首参 |

这张表就是“读源码”的产出：不必记住 70 行 C++，只要能从一个 block 反推回哪几行建了它。

## 读不懂时的降级路径

源码读不懂，通常不是智商问题，是入口选错了。固定按下面顺序降级：

1. **先看 `test`**：`RUN` 行给你复现命令，`CHECK` 行给你“什么算对”。看不懂实现时，先看懂期望输出，往往就懂了一半。
2. **再看 `.td`**：op 的 operand/result/attribute/trait 全在这。很多“为什么它不接受这个类型”的疑问，在 `.td` 的 `TypeConstraint` 一行就结束了。
3. **再看 `.inc`**：只查具体某个成员函数。用 `Select-String -Pattern 'AddIOp::'` 精确定位，不要通读 1.1 MB 的 `.cpp.inc`。
4. **最后看 `.cpp`**：只看 `matchAndRewrite` / `runOnOperation` / `fold` 这三个入口。其余是 helper，按名字跳转。
5. **还看不懂就改**：复制一段最小 IR，逐行删，看行为在哪一行变化。这是最快的“实验式阅读”。

用这个顺序，`SCFToControlFlow.cpp` 的 744 行里你真正需要读的只有 70 行（`ForLowering`）+ 14 行（`runOnOperation`）。

## 阅读技巧：在 7493 个文件里定位

**技巧一：先搜 mnemonic，不要搜 C++ 类名。** `arith.addi` 的 C++ 类是 `AddIOp`，但 `def` 名可能是 `Arith_AddIOp`。搜字符串字面量 `"addi"` 或 `getOperationName` 更可靠。

```powershell
rg -n 'def SCFToControlFlowPass' "$src\include\mlir\Conversion\Passes.td"
rg -n 'struct ForLowering' "$src\lib\Conversion\SCFToControlFlow\SCFToControlFlow.cpp"
```

`实测` 分别命中 `1171` 和 `104`。本机 `rg` 版本是 15.2.0；没有 `rg` 时用 `Get-ChildItem -Recurse -Filter *.td | Select-String -Pattern ...`；注意 PowerShell 的 `-Path` 不展开 `**`。

**技巧二：用 `#include "....inc"` 反查生成物。** `.inc` 的落点只有两种：头文件里（声明）和 `.cpp` 末尾（定义）。看到 `GEN_PASS_DEF_XXX` 就知道这个 `.inc` 会展开出一个 `impl::XXXBase` 基类。

**技巧三：`getODSOperands(n)` 的 `n` 就是 `.td` 声明顺序。** 变长 operand（`Variadic`）会有 `getODSOperandIndexAndLength`，用 `operandSegmentSizes` 分段。`linalg.generic` 的 ins/outs 就是这么分的。

**技巧四：`git log` / `git blame` 先确认仓库形态。** 本机快照只有一个 commit：

```powershell
git -C $src rev-list --count HEAD
```

`实测` 输出 `1`；`git blame -L 329,331` 的三行全部指向 `^7a941dfb`（2026-10-19，release 脚本提交）。**这个快照的历史考古没有信息量**。要看真实演进，克隆上游仓库后对同一个文件跑 `git log -L 329,399:mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp`。这一步本机无法给出有意义输出，标 `guided`。

**技巧五：按“最近改了什么”缩小范围。** 上游仓库里 `git log --oneline -20 -- <dir>` 能告诉你哪个 lowering 最近在动。正在动的代码，通常也是 bug 最多、文档最旧的地方。
## 对比与取舍：三种读法的成本

| 读法 | 上手成本 | 能回答的问题 | 什么时候别用 |
|---|---|---|---|
| 从 `test` 倒推 | 最低 | “这个 pass 保证什么行为” | 想问“为什么这样实现”时会卡住 |
| 从 `.td` 顺推 | 中 | “这个 op/pass 声明了什么” | `.td` 里没有算法 |
| 从 `.cpp` 正读 | 最高 | “具体怎么改 IR、边界在哪” | 700 行以上的文件，必先有 `test` 或 `.td` 做锚 |

我们的建议：**永远从 `test` 的 `RUN` 行开始**，因为它同时给你命令和期望输出。只有当 `CHECK` 行解释不了某个现象时，才下潜到 `.cpp`。

另一个取舍是“读生成物还是读生成器”。`.inc` 有 1.1 MB，`mlir-tblgen` 的输入 `.td` 只有 79 KB。**理解语义读 `.td`，确认生成行为读 `.inc`，两者不要混着读**。

## 踩雷集锦

1. **在 `lib/` 里找 pass 的注册代码。** `registerTransformsPasses()` 在 `Passes.h.inc`，不在任何 `.cpp`。搜不到不是你没搜对，是它真的不在那。
2. **以为 `.cpp` 里有 `AddIOp::verify`。** 生成的 `verifyInvariantsImpl` 在 `.cpp.inc`，而且被 include 在 `ArithOps.cpp:3093`——文件最末尾。
3. **把 parser 错误当成 verifier 错误。** “expects different type than prior uses” 来自 parser；“must be signless-...” 来自 ODS verifier。两者的修复方向完全不同。
4. **通读 `.cpp.inc`。** 它是模板产物，逐行读只会消耗耐心。用 `Select-String -Pattern 'AddIOp::'` 精确跳转。
5. **用类名搜 `.td`。** 不是所有 op 都叫 `Linalg_MatmulOp`；搜 mnemonic 字符串更稳。
6. **相信本机快照的 `git blame`。** 单 commit 快照里所有行都指向同一个提交（实测 `rev-list --count` 为 1），历史信息为零。
7. **在 `--pass-pipeline` 里用 `--` 前缀。** pipeline 里写 pass 名不带 `--`；带 `--` 的是顶层 flag，混用会得到 “does not refer to a registered pass”。

## 进阶：再往深一层

- **看 pass 的 `getDependentDialects`**：它决定了这个 pass 会偷偷加载哪些 dialect。`Canonicalizer.cpp:45-50` 就是一个例子（为 `filter-dialects` 预加载）。调试“为什么我的 dialect 没被加载”时，这是第一现场。
- **看 `ConversionTarget` 的完整配置**：`SCFToControlFlow.cpp:735-738` 只有 4 行，但它定义了“什么叫降级完成”。任何 lowering 的行为异常，先读这 4 行。
- **看 pattern 的 benefit**：`populateSCFToControlFlowConversionPatterns` 给 `DoWhileLowering` 设了 2。多个 pattern 竞争同一 op 时，benefit 决定谁先试。
- **看 `test` 的 `split-input-file`**：一个文件里多个 `// -----` 分段，每段独立验证。读测试时按段读，不要当一个大用例。

## 动手练习

统一约定：在 `.scratch\mlir-28\` 下新建文件，`$mlir` 指向本机 bin 目录；每题至少跑三个用例。

### 练习 1：把 `--cse` 追到实现

**任务**：写出 `--cse` 的完整注册链，并给出实现文件里 `runOnOperation` 的行号。
**用例**：(a) 纯 arith 的重复加法；(b) 带 `func.call` 的函数；(c) 空 module。
**步骤**：1) `--help` 确认 flag；2) `Select-String registerCSEPass` 找生成物；3) 打开 `lib/Transforms/CSE.cpp`；4) 跑三个用例，比较 CSE 前后 op 数。

<details><summary>参考解答</summary>

链路是 `mlir-opt.cpp:326` → `RegisterAllPasses.cpp:62` → `Passes.h.inc` 的 `registerCSEPass` → `CSE.cpp` 的 `CSEPass::runOnOperation`。用例 (b) 跨函数不会合并——CSE 以 region 为单位；(c) 退出码 0，无变化。

</details>

**卡点**：以为 CSE 跨函数；它按 `IsolatedFromAbove` 的 region 分片。

### 练习 2：从 generic dump 反推 ODS

**任务**：给一段 generic 形式的 `scf.for`，写出 operand 顺序并用 `getODSOperands` 验证。
**用例**：(a) 无 iter_args；(b) 一个 iter_arg；(c) 两个 iter_arg。
**步骤**：1) 各打印一次；2) 数 operand；3) 读 `SCFOps.td:154` 附近的 `ForOp` 声明；4) 对照 `ArithOps.h.inc:1710-1712` 的下标映射。

<details><summary>参考解答</summary>

顺序是 lowerBound、upperBound、step，再跟全部 `initArgs`：(a) 3 个，(b) 4 个，(c) 5 个。最后一个 operand 与 region 的 block argument 一一对应。

</details>

**卡点**：把 `scf.yield` 的 operand 当成 `for` 的 operand——它们属于 region 内的 terminator。

### 练习 3：给一条 lowering 画控制流图

**任务**：读 `IfLowering::matchAndRewrite`（`SCFToControlFlow.cpp:401-454`），画出 block 结构并实跑验证。
**用例**：(a) 无 else、无返回值；(b) 有 else、无返回值；(c) 有返回值。
**步骤**：1) 写三个 `scf.if`；2) 跑 `convert-scf-to-cf`；3) 数 block 与 `cf.br`；4) 对照 `splitBlock`、`createBlock`、`inlineRegionBefore` 解释来源。

<details><summary>参考解答</summary>

(a) then block + continuation；(b) 多一个 else block；(c) 多一个带 block argument 的 continue block，`replaceOp` 用它的参数替换结果。源码注释 `:111-129` 是这张图的文字版。

</details>

**卡点**：漏掉“有返回值时为什么必须新建 block”——不能改已有 block 的参数表。

### 练习 4：只读测试预测行为

**任务**：只读 `convert-to-cfg.mlir` 的 `RUN`/`CHECK`，预测“输入同时有 `scf.for` 和 `linalg.matmul`”的结果，再实跑。
**用例**：(a) 只有 scf；(b) scf + arith；(c) scf + linalg.matmul。
**步骤**：1) 写预测；2) 跑三个用例；3) `Select-String 'linalg\.'` 检查输出；4) 回到 `SCFToControlFlow.cpp:736-738` 解释。

<details><summary>参考解答</summary>

三个用例退出码都是 0，`linalg.matmul` 原样保留。target 只把 7 个 scf op 标为 illegal，并用 `markUnknownOpDynamicallyLegal` 放过其余一切。

</details>

**卡点**：把“pass 成功”当成“scf 一定消失”；必须显式检查输出里是否还有 `scf.`。

### 练习区自我检核

- [ ] 我能不看笔记写出 `--canonicalize` 的注册链
- [ ] 我知道 `ArithOps.cpp.inc` 被 include 在哪个文件哪一行
- [ ] 我能区分 parser 错误与 ODS verifier 错误

## 本章重点整理

- 源码树分三层：`test` 是规格，`include` 是契约，`lib` 是实现，`tools` 是装配。
- `.td` 写意图，`.h.inc`/`.cpp.inc` 写机械代码，`.cpp` 只写 fold / verifier / pattern 这类语义。
- 注册是一条链：`main` → `registerAllPasses` → 各组 `register*Passes` → `registerPass(lambda)` → `createXXXPass()`。
- `getODSOperands(n)` 的下标就是 `.td` 声明顺序；变长 operand 靠 `operandSegmentSizes` 分段。
- `convert-scf-to-cf` 用 partial conversion，只对 7 个 op 非法，其余 dynamic legal。
- 读不懂就降级：`test` → `.td` → `.inc` → `.cpp`，每步只回答一个问题。

## 自我检核

- [ ] 我能说出 `mlir/include/mlir/Conversion/Passes.td` 为什么只有 3 个文件却很重要
- [ ] 我能解释“没有 `lib/Pass/PassManager.cpp`”该怎么向同事说明
- [ ] 我能从 `--help` 的一个子选项倒推回 `.td` 的 `Option`
- [ ] 我能用 generic dump 判断一个 op 有几个 operand、几个结果
- [ ] 我知道 `ArithOps.cpp.inc` 在 `ArithOps.cpp` 末尾被 include，以及为什么
- [ ] 我能解释 `ForLowering` 为什么先建 body 的边、再建 init 的边
- [ ] 我能说出本机快照做 `git blame` 为什么没有信息量
- [ ] 我能用一句话说明 partial conversion 在 `convert-scf-to-cf` 里的实际效果

## 延伸阅读

- **[mlir-opt.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/tools/mlir-opt/mlir-opt.cpp)**
  - **读哪里**：`main`（325-349）。
  - **学什么**：一个 MLIR 工具最小的装配流程。
  - **关联**：本章所有追踪路线的起点。

- **[SCFToControlFlow.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp)**
  - **读哪里**：`ForLowering`（104-109）、`matchAndRewrite`（329-399）、`runOnOperation`（730-743）。
  - **学什么**：结构化控制流怎么拆成 CFG。
  - **关联**：底层机制与示例三的唯一依据。

- **[ArithOps.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Arith/IR/ArithOps.td)**
  - **读哪里**：`Arith_IntBinaryOp`（58）、`...WithOverflowFlags`（193-206）、`Arith_AddIOp`（271）。
  - **学什么**：ODS 的继承链与 `assemblyFormat` 怎么层层复用。
  - **关联**：示例二的第 2 分钟。

- **[MLIR 文档：Pass Management](https://mlir.llvm.org/docs/PassManagement/)**
  - **读哪里**：Pass Registration、Pass Pipeline。
  - **学什么**：官方对注册与 pipeline 语法的定义。
  - **关联**：核心概念二的文档版。

- **[MLIR 文档：Defining Dialects](https://mlir.llvm.org/docs/DefiningDialects/)**
  - **读哪里**：Operation Definition、Generated Builders。
  - **学什么**：`.td` 的每个字段最终生成什么。
  - **关联**：帮你预测 `.inc` 里该有什么。

---

下一章把“读源码”升级成“做判断”：既然 MLIR 能承载这么多抽象，为什么 LLVM IR 不能？dialect 边界该划在哪？什么情况下根本不该用 MLIR？

→ [Ch 29 设计取舍](./29-design-tradeoffs.md)
