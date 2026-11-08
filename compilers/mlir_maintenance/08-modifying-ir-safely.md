# Ch 08 — 安全地修改 IR

> **目标**：把“改 IR”拆成三种受控操作（walk、rewriter、直接改 Value），知道每种操作在什么情况下会失效：walk 中能删当前 op 但不能删未访问的 op；`replaceOp` 会先断 use 再 erase；listener 没收到通知时 analysis 会静默过期；greedy driver 的收敛与迭代上限。读完你能审一个 pattern 的“修改安全边界”。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `e44fff5decc7`，`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。本章有两个用本树 shared lib 链接的小程序（`walk_erase.cpp`、`oldapi.cpp`），编译命令在实验里给出。

> **前置**：Ch 04（use-list 与 erase 顺序）、Ch 07（verifier）；用过 `RewritePattern` 或 `OpBuilder` 更好。

## 08.1 为什么“改 IR”比“读 IR”危险一个量级

读 IR 时，最坏结果是读错；改 IR 时，最坏结果是**把结构改坏但 verifier 发现不了**。

三类高频事故：

1. **walk 中删除未访问的节点。** `walk` 用的是 `make_early_inc_range`（`Visitors.h:238` 附近），它能容忍你删“当前”节点；但删掉 `op->getNextNode()` 会让迭代器指向已释放内存。
2. **替换后继续用旧 `Value`。** `rewriter.replaceOp(op, newValue)` 之后，`op` 已经被 erase；再访问 `op->getResult(0)` 就是 UAF。
3. **绕过 rewriter 直接 `op->erase()`。** IR 变了，但 listener 没收到 `notifyOperationErased`；依赖 listener 的 analysis（如 dataflow）仍然持有旧节点，几轮迭代后给出错误结论。

所以本章的主线是：**所有修改都走 rewriter；walk 只做“读 + 受控删当前节点”；任何跨迭代的状态都要显式失效。**

## 08.2 先建立直觉：三个圈层

```text
┌─────────────────────────────────────────────────────────────┐
│ 3. Greedy driver（worklist + 收敛判定）                       │
│    applyPatternsGreedily / RegionPatternRewriteDriver        │
│    ┌───────────────────────────────────────────────────┐    │
│    │ 2. RewriterBase（唯一允许改 IR 的入口）             │    │
│    │    replaceOp / eraseOp / modifyOpInPlace            │    │
│    │    listener: notifyOperation{Inserted,Erased,Replaced}│   │
│    │    ┌───────────────────────────────────────────┐    │    │
│    │    │ 1. IR（Operation/Block/Value/use-list）    │    │    │
│    │    │    walk: PreOrder / PostOrder              │    │    │
│    │    └───────────────────────────────────────────┘    │    │
│    └───────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

三条规则对应三个圈层：

- **第 1 层**：walk 的迭代器在遍历前就 increment（early increment），所以删当前安全、删未来不安全。
- **第 2 层**：rewriter 负责“先断 use、再 erase、再通知 listener”，顺序不能换。
- **第 3 层**：driver 负责“改完是否还要再来一轮”，用 worklist 与 max-iterations 控制。

## 08.3 walk：顺序、跳过与中断

`Visitors.h` 把 walk 的所有权下放给 IR 类（`Operation::walk`、`Block::walk`），底层是自由函数 `walk(Operation*, callback, WalkOrder)`（`:224`）。回调返回 `WalkResult`：

```cpp
enum ResultEnum { Interrupt, Advance, Skip };   // WalkResult.h:32
static WalkResult interrupt();   // 立刻停止整棵树
static WalkResult advance();     // 继续
static WalkResult skip();        // 跳过当前节点的子树，继续兄弟
```

PreOrder 与 PostOrder 的差别只在“回调在子节点前还是后”（`:226`、`:244`）：

```cpp
if (order == WalkOrder::PreOrder) {
  WalkResult result = callback(op);
  if (result.wasSkipped())     return WalkResult::advance();
  if (result.wasInterrupted()) return WalkResult::interrupt();
}
... 递归子 region/block/op ...
if (order == WalkOrder::PostOrder)
  return callback(op);
return WalkResult::advance();
```

两个维护要点：

- **`skip()` 在 PostOrder 下没有“跳过子树”的意义**（子树已经走完），只影响是否继续兄弟。
- **`LogicalResult` 和 `InFlightDiagnostic` 可以隐式转成 `WalkResult`**（`WalkResult.h:37-41`）：`return op.emitError(...)` 会自动中断 walk。这是一个很方便的“发现错误就停”写法，但也容易误伤：你会以为只是发个诊断，实际整个 walk 停了。

### 08.3.1 为什么删“当前”是安全的

`Visitors.h:238` 的注释写得很直接：

```cpp
// Early increment here in the case where the operation is erased.
for (auto &nestedOp :
     llvm::make_early_inc_range(Iterator::makeIterable(block))) {
```

`make_early_inc_range` 在解引用前先把迭代器推进到下一个节点。所以 `op->erase()` 之后，循环变量已经指向后继，不会读到已释放节点。实测（实验 5）：

```text
visit foo.a
erased current
visit foo.keep
visit foo.b
visited=3, remaining=2
```

但注意：这只保证“删当前”。删 `op->getNextNode()`、删父 op、或删正在被外层栈引用的节点，仍然危险。

## 08.4 RewriterBase：唯一允许改 IR 的入口

`PatternMatch.h:368` 的 `RewriterBase` 提供三类修改：

| 方法 | 语义 | 前置条件 |
|---|---|---|
| `replaceOp(op, newValues)` | 用新值替换 op 的所有结果，然后 erase op | 结果个数匹配 |
| `eraseOp(op)` | 删除无用户的 op | `assert(op->use_empty())` |
| `modifyOpInPlace(op, fn)` | 原地改 op（属性/operand） | 通知 listener |

`replaceOp` 的实现（`PatternMatch.cpp:127`）严格按顺序：

```cpp
void RewriterBase::replaceOp(Operation *op, ValueRange newValues) {
  assert(op->getNumResults() == newValues.size() && "incorrect # of replacement values");
  replaceAllOpUsesWith(op, newValues);   // 先断 use，并通知 listener
  eraseOp(op);                           // 再 erase
}
```

`eraseOp`（`:155`）的第一个断言就是维护者的护栏：

```cpp
assert(op->use_empty() && "expected 'op' to have no uses");
```

带 listener 时，`eraseOp` 还会做后序删除（注释原文）：

```text
Nested ops must be erased one-by-one, so that listeners have a consistent
view of the IR every time a notification is triggered. Users must be
erased before definitions. I.e., post-order, reverse dominance.
```

这解释了为什么不能图省事 `op->erase()`：**裸 erase 会一次销毁整棵子树，listener 只看到“一个 op 消失”，中间所有节点都没有通知。**

`modifyOpInPlace`（`PatternMatch.h:651`）是“改属性但不换 op”的正规入口：

```cpp
void modifyOpInPlace(Operation *root, CallableT &&callable) {
  startOpModification(root);
  callable();
  finalizeOpModification(root);
}
```

直接 `op->setAttr(...)` 在只是本地修改时也能工作，但任何依赖 listener 的 analysis 都不会知道。

## 08.5 use-list 的维护 API

`Value` 侧有三个常用替换：

```cpp
void replaceAllUsesWith(Value newValue);                       // Value.cpp
void replaceAllUsesExcept(Value newValue, Operation *exceptedUser);
bool replaceUsesWithIf(Value newValue, function_ref<bool(OpOperand&)>);
```

`RewriterBase` 侧还有两个“结构性”删除，专门处理“多结果 op 只删一部分”：

```cpp
void eraseOperands(Operation *op, const BitVector &eraseIndices);        // PatternMatch.cpp:247
Operation *eraseOpResults(Operation *op, const BitVector &eraseIndices); // :278
```

维护者最常犯的错是**手写循环改 operand**：

```cpp
for (OpOperand &use : value.getUses())   // 危险：改 use-list 会破坏遍历
  use.set(newValue);
```

正确写法是 `value.replaceAllUsesWith(newValue)`，它内部用安全的方式遍历并摘链。如果确实要条件替换，用 `replaceUsesWithIf`。

## 08.6 greedy driver：收敛、迭代上限与 API 迁移

`GreedyPatternRewriteDriver.h:234` 的入口是：

```cpp
LogicalResult applyPatternsGreedily(Operation *op,
                                    const FrozenRewritePatternSet &patterns,
                                    GreedyRewriteConfig config = ...,
                                    bool *changed = nullptr);
```

本轮 trunk 的一个真实维护点：**旧的 `applyPatternsAndFoldGreedily` 已经被删除。** 编译旧代码会得到（实验 7）：

```text
oldapi.cpp:6:9: error: use of undeclared identifier 'applyPatternsAndFoldGreedily'; did you mean 'applyPatternsGreedily'?
```

删除发生在 `72f5050ae765`（2026-02-23，`[mlir][NFC] Remove unused deprecated API wrappers (#182715)`），它移除了 `applyPatternsAndFoldGreedily`、`applyOpPatternsAndFold`、`NamedAttrList(std::nullopt_t)`、`OpPrintingFlags(std::nullopt_t)`。对 out-of-tree 项目，这是**编译期破坏**；对 in-tree，作者的理由是“monorepo 里零调用者”。

迁移只需要改名，因为 `GreedyRewriteConfig` 的 `fold` 字段本来就是 `true`（`GreedyPatternRewriteDriver.h:173`）。显式写出来只是让意图可见：

```cpp
applyPatternsGreedily(op, std::move(patterns),
                      GreedyRewriteConfig().enableFolding());
```

driver 的收敛逻辑在 `GreedyPatternRewriteDriver.cpp`：`simplify`（`:1228`）反复处理 worklist，`notifyOperationReplaced`（`:1146`）把新 op 重新入队。三个配置最常改：

| 配置 | 默认 | 作用 |
|---|---|---|
| `setMaxIterations` | 10 | 防止 pattern 振荡导致死循环 |
| `enableFolding` | 开（`fold = true`） | 每轮顺手做 constant fold |
| `setStrictness` | `AnyOp` | 限制 driver 只看特定 op |

维护者看到 “pattern application failed to converge” 时，先找振荡对（两个 pattern 互相反向改写），而不是简单调大 `maxIterations`。

## 08.7 线程边界

MLIR 的 pass pipeline 可以并行跑不同 op（`OpPassManager` 的 `runOnOperation` 并行）。边界规则：

- **同一个 `MLIRContext` 的 type/attribute uniquing** 需要 `setThreadSafeLocking(true)`（Ch 05）。
- **同一棵 IR 子树不能被两个线程同时改。** 并行单元是 `IsolatedFromAbove` 的 op；跨单元共享的 `Value` 需要通过 clone/mapping 传递。
- **listener 通常不是线程安全的。** 每个并行 driver 要有自己的 listener 或加锁。

一个实用判据：**你的 pattern 如果读写 `getOperation()->getParentOp()` 之外的状态，就已经越过并行边界。**

## 08.8 源码落点

```bash
grep -n 'enum class WalkOrder\|WalkResult walk(Operation \*op' mlir/include/mlir/IR/Visitors.h
grep -n 'class RewriterBase\|virtual void replaceOp\|virtual void eraseOp\|void eraseOperands\|eraseOpResults\|modifyOpInPlace\|notifyOperationErased' mlir/include/mlir/IR/PatternMatch.h | head
grep -n 'void RewriterBase::replaceOp\|void RewriterBase::eraseOp\|void RewriterBase::eraseOperands\|RewriterBase::eraseOpResults' mlir/lib/IR/PatternMatch.cpp
grep -n 'LogicalResult RegionPatternRewriteDriver::simplify\|notifyOperationReplaced' mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp | head
grep -n 'applyPatternsGreedily' mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h | head -3
```

本机实测：

```text
mlir/include/mlir/IR/Visitors.h:28:enum class WalkOrder { PreOrder, PostOrder };
mlir/include/mlir/IR/Visitors.h:224:WalkResult walk(Operation *op, function_ref<WalkResult(Operation *)> callback,
mlir/include/mlir/IR/PatternMatch.h:368:class RewriterBase : public OpBuilder {
mlir/include/mlir/IR/PatternMatch.h:403:    virtual void notifyOperationErased(Operation *op) {}
mlir/include/mlir/IR/PatternMatch.h:518:  virtual void replaceOp(Operation *op, ValueRange newValues);
mlir/include/mlir/IR/PatternMatch.h:543:  virtual void eraseOp(Operation *op);
mlir/include/mlir/IR/PatternMatch.h:553:  void eraseOperands(Operation *op, const BitVector &eraseIndices);
mlir/include/mlir/IR/PatternMatch.h:559:  Operation *eraseOpResults(Operation *op, const BitVector &eraseIndices);
mlir/include/mlir/IR/PatternMatch.h:651:  void modifyOpInPlace(Operation *root, CallableT &&callable) {
```

```text
mlir/lib/IR/PatternMatch.cpp:127:void RewriterBase::replaceOp(Operation *op, ValueRange newValues) {
mlir/lib/IR/PatternMatch.cpp:155:void RewriterBase::eraseOp(Operation *op) {
mlir/lib/IR/PatternMatch.cpp:247:void RewriterBase::eraseOperands(Operation *op, const BitVector &eraseIndices) {
mlir/lib/IR/PatternMatch.cpp:278:Operation *RewriterBase::eraseOpResults(Operation *op,
mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:1146:void GreedyPatternRewriteDriver::notifyOperationReplaced(
mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp:1228:LogicalResult RegionPatternRewriteDriver::simplify(bool *changed) && {
mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h:234:applyPatternsGreedily(Operation *op, const FrozenRewritePatternSet &patterns,
```

## 08.9 可跑实验

### 实验 1：walk 的六种顺序（实测）

```bash
mkdir -p /tmp/mlirc && cd /tmp/mlirc
cat > simple.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %s = arith.addi %a, %b : i32
  %m = arith.muli %s, %a : i32
  return %m : i32
}
EOF
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt simple.mlir -test-ir-visitors
```

真实输出（节选）：

```text
Op pre-order visits
Visiting op 'builtin.module'
Visiting op 'func.func'
Visiting op 'arith.addi'
Visiting op 'arith.muli'
Visiting op 'func.return'
...
Op post-order visits
Visiting op 'arith.addi'
Visiting op 'arith.muli'
Visiting op 'func.return'
Visiting op 'func.func'
Visiting op 'builtin.module'
...
Op reverse post-order visits
Visiting op 'func.return'
Visiting op 'arith.muli'
Visiting op 'arith.addi'
Visiting op 'func.func'
Visiting op 'builtin.module'
```

这张输出是判断“pattern 里该用哪种 walk”的依据：**pre-order 适合自上而下建立状态，post-order 适合“先处理子节点再处理父节点”（如死代码删除）。**

### 实验 2：generic visitor 的 before/after region（实测）

```bash
$T/bin/mlir-opt simple.mlir -test-generic-ir-visitors
```

真实输出：

```text
step 0 op 'builtin.module' before all regions
step 1 op 'func.func' before all regions
step 2 op 'arith.addi' before all regions
step 3 op 'arith.muli' before all regions
step 4 op 'func.return' before all regions
step 5 op 'func.func' after all regions
step 6 op 'builtin.module' after all regions
```

generic visitor 把“进入 region 前”和“离开 region 后”显式暴露，适合实现“维护一个作用域栈”的 pass（比如 dominator 信息、符号表）。

### 实验 3：use-list 顺序的往返校验（实测）

```bash
$T/bin/mlir-opt simple.mlir -test-verify-uselistorder
```

真实输出：

```text
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %1 = arith.muli %0, %arg0 : i32
    return %1 : i32
  }
}
```

这个 pass 把 IR 写进 bytecode 再读回，检查 use-list 顺序是否保持（bytecode v3 起编码 use-list order）。**你自己的 pass 如果重排了 use 顺序，可能让依赖顺序的下游分析结果变化**，即使语义没变。

### 实验 4：erase unused operands/results 的真实效果（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Dialect/Linalg/erase-unused-operands-and-results.mlir \
  -split-input-file -test-linalg-transform-patterns=test-erase-unused-operands-and-results
```

真实输出（节选）：

```text
func.func @remove_deadargs_generic_basic(%arg0: tensor<?xf32>) -> tensor<?xf32> {
  ...
  %1 = linalg.generic {indexing_maps = [#map, #map], iterator_types = ["parallel"]} ins(%arg0 : tensor<?xf32>) outs(%0 : tensor<?xf32>) {
  ^bb0(%in: f32, %out: f32):
    %2 = arith.addf %in, %cst : f32
    linalg.yield %2 : f32
  } -> tensor<?xf32>
```

输入有两个 `ins` 和一个未使用的 `%2` temp，输出只剩一个 `ins`、没有多余 temp。这正是 `eraseOperands` 的产物：它同时改 op 的 operand 段和 region 的 block argument（region-based op 的 `ins` 与 `^bb0` 参数一一对应）。**只改 operand 不改 block arg，会被 linalg 的 verifier 抓到**——`LinalgOps.cpp:2010` 的报错正是 `mismatching number of operands and block arguments`。

### 实验 5：walk 中删当前 op（实测）

用本树 shared lib 链接（编译命令同练习 A）：

```cpp
for (const char *n : {"foo.a", "foo.keep", "foo.b"})
  b.create(OperationState(b.getUnknownLoc(), n));
mod.walk([&](Operation *op) {
  if (op == mod.getOperation()) return WalkResult::advance();
  if (op->getName().getStringRef() == "foo.a") op->erase();
  return WalkResult::advance();
});
```

真实输出：

```text
visit foo.a
erased current
visit foo.keep
visit foo.b
visited=3, remaining=2
```

三个 op 都被访问，`foo.a` 被删且没有崩溃。**这是 `make_early_inc_range` 的保证，不是巧合。**

### 实验 6：pass 锚点不匹配的报错（实测）

```bash
$T/bin/mlir-opt simple.mlir -test-clone
```

真实输出：

```text
<unknown>:0: error: unable to schedule pass '{anonymous}::ClonePass' on a PassManager intended to run on 'builtin.module'!
```

`-test-clone` 是 `InterfacePass<FunctionOpInterface>`（`TestClone.cpp:28`），只能挂在实现了 `FunctionOpInterface` 的 op 上，所以必须放进 `func.func(...)`。这条错误和 07.8 实验 5 同类：**调度错误发生在 pass 运行前，和 verifier 无关。**

```bash
$T/bin/mlir-opt simple.mlir -pass-pipeline='builtin.module(func.func(test-clone))'
```

真实输出：正常打印 IR（pass 运行成功）。

### 实验 7：旧 API 已被删除（实测）

```bash
cd /tmp/mlirc
cat > oldapi.cpp <<'EOF'
#include "mlir/IR/PatternMatch.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
using namespace mlir;
void f(Operation *op) {
  RewritePatternSet patterns(op->getContext());
  (void)applyPatternsAndFoldGreedily(op, std::move(patterns));
}
EOF
T=/home/ubuntu/llvm/builds/ct-instr-trunk; S=/home/ubuntu/llvm/trees/ct-instr-trunk
clang++ -std=c++17 -fno-rtti -fsyntax-only oldapi.cpp \
  -I$S/mlir/include -I$T/tools/mlir/include -I$S/llvm/include -I$T/include
```

真实输出：

```text
oldapi.cpp:6:9: error: use of undeclared identifier 'applyPatternsAndFoldGreedily'; did you mean 'applyPatternsGreedily'?
        applyPatternsGreedily
```

查它是什么时候没的：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git log -1 --format='%h %ad %s' --date=short 72f5050ae765
```

真实输出：

```text
72f5050ae765 2026-02-23 [mlir][NFC] Remove unused deprecated API wrappers (#182715)
```

`git rev-list --count 72f5050ae765..HEAD` 实测为 `37011`——也就是说“零调用者”的判断基于 3.7 万个提交之后的树。**out-of-tree 项目不能假设 deprecated wrapper 会一直留着。**

### 实验 8：用 IR dump 观察 driver 配置与实际修改（实测）

```bash
cat > /tmp/mlirc/omp.mlir <<'EOF'
func.func @f(%a: i32) -> i32 {
  %z = arith.constant 0 : i32
  %s = arith.addi %a, %z : i32
  return %s : i32
}
EOF
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt /tmp/mlirc/omp.mlir -canonicalize --mlir-print-ir-after-all
```

真实输出：

```text
// -----// IR Dump After CanonicalizerPass: canonicalize{cse-between-iterations=false    max-iterations=10 max-num-rewrites=-1 region-simplify=normal test-convergence=false top-down=true} //----- //
module {
  func.func @f(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}
```

这张 dump 有两个信息：**driver 的实际配置**（`max-iterations=10`、`top-down=true`）和**这一轮之后的 IR**。维护者调 pattern 时，先看 dump 再猜代码。

注意：`--mlir-print-ir-after-change` 在本树的 `-canonicalize` 上并不会打印这段 banner（实测只输出最终 IR），所以本章用 `--mlir-print-ir-after-all` 做观察。**不要把“没打印”误读成“没修改”。** 同理，绕过 rewriter 的修改可能让依赖 listener 的分析过期，但 IR dump 仍然只反映最终状态。

## 08.10 踩雷集锦

1. **错误直觉：walk 里不能 erase。** → 正确认识：删**当前** op 是被支持的（early increment）；删未访问节点才会崩。
2. **错误直觉：`erased` 之后还能读 `op->getResult(0)`。** → 正确认识：`replaceOp` 已经 erase；要用替换值就提前存 `Value` 句柄。
3. **错误直觉：`eraseOp` 会帮你检查 use。** → 正确认识：它只有 `assert(use_empty())`；release 构建下断言消失，直接 UAF。
4. **错误直觉：直接 `op->erase()` 和 `rewriter.eraseOp(op)` 等价。** → 正确认识：后者逐节点通知 listener 并按后序删除，前者跳过所有通知。
5. **错误直觉：手写 `for (OpOperand &u : v.getUses()) u.set(...)` 很直观。** → 正确认识：改 use-list 会破坏遍历；用 `replaceAllUsesWith` / `replaceUsesWithIf`。
6. **错误直觉：`applyPatternsAndFoldGreedily` 还在，只是 deprecated。** → 正确认识：本 trunk 已删除（`72f5050ae765`），必须改 `applyPatternsGreedily` + `enableFolding()`。
7. **错误直觉：pattern 不收敛就调大 `maxIterations`。** → 正确认识：先找互相反向的 pattern 对；调大只是把死循环拖长。
8. **错误直觉：并行 pass 里可以随便读全局 IR。** → 正确认识：并行单元是 `IsolatedFromAbove`；跨单元共享值要走 clone/mapping，listener 要各自隔离。

## 08.11 本章重点整理

- walk 用 early increment：删当前安全，删未来不安全；`skip`/`interrupt` 语义不同。
- `replaceOp` = 先断 use（通知 listener）+ 再 erase；`eraseOp` 断言无 use。
- 带 listener 的 erase 是逐节点、后序、reverse dominance。
- 改 use-list 必须用 `Value::replaceAllUsesWith` 系列，不要手写循环。
- greedy driver 的收敛由 worklist + `maxIterations` 控制；folding 默认开启（`fold = true`）。
- `applyPatternsAndFoldGreedily` 在本 trunk 已被删除，迁移到 `applyPatternsGreedily`。
- 并行边界是 `IsolatedFromAbove`；listener 默认不跨线程安全。
- 绕过 rewriter 的修改会让 listener/analysis 静默过期。

## 08.12 自我检核

- [ ] 能解释 `make_early_inc_range` 为什么让“删当前”安全
- [ ] 能说出 PreOrder/PostOrder 各自适合什么 pass
- [ ] 能背出 `replaceOp` 的两步顺序与 `eraseOp` 的断言
- [ ] 能解释 listener 缺失会导致什么类型的 bug
- [ ] 能说出 `eraseOperands` 为什么要同时改 block argument
- [ ] 能写出迁移到 `applyPatternsGreedily` + `enableFolding()` 的等价代码
- [ ] 能判断一段 pattern 是否越过并行边界
- [ ] 能用 `-test-ir-visitors` / `-test-verify-uselistorder` 做行为基线

## 08.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/Visitors.h`**
  - 读哪里：`WalkOrder`（28）、`walk(Operation*)`（224）、early-inc 循环（240 附近）
  - 能学到什么：walk 的迭代器安全边界与 skip/interrupt 语义
  - 和本章关联：08.3、08.9 实验 1/5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/PatternMatch.h`**
  - 读哪里：`RewriterBase`（368）、listener 通知（403）、`replaceOp`/`eraseOp`（518/543）、`modifyOpInPlace`（651）
  - 能学到什么：合法修改 IR 的完整入口
  - 和本章关联：08.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/PatternMatch.cpp`**
  - 读哪里：`replaceOp`（127）、`eraseOp`（155）、`eraseOperands`（247）、`eraseOpResults`（278）
  - 能学到什么：断 use、erase、通知的确切顺序与断言
  - 和本章关联：08.4、08.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/PatternRewriter.md`**
  - 读哪里：`Defining Patterns`、`Pattern Application`、`RewriterBase` 三节
  - 能学到什么：pattern 的 benefit、root op 与 driver 的契约
  - 和本章关联：08.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h`**
  - 读哪里：`GreedyRewriteConfig`（43–170）、`applyPatternsGreedily`（234）
  - 能学到什么：迭代上限、folding、strictness 三个旋钮
  - 和本章关联：08.6、08.9 实验 7

Ch 04–08 的不变量到这里拼齐了。下一章不再讲概念，而是把这些拼成一个能编译、能加载、能用 FileCheck 回归的 out-of-tree pass。

→ 下一章：[练习 A 写一个 pass 与 pattern](./practice-a-ir-pass-pattern.md)
