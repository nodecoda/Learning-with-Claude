# Ch 9 安全地修改 IR

> **目标**：把“改 IR”从“随手 `erase()`”变成一套有顺序、有通知、有验证的流程。学完本章，你应该能判断：什么时候可以边 walk 边改、`replaceAllUsesWith` 和 `erase` 谁先谁后、`Block`/`Region` 改动后哪些指针会失效，以及为什么 `IRRewriter` 比裸 `Operation*` 更安全。

## 为什么需要这个？

MLIR 的 pass 本质上是“读 IR、改 IR、再交给下一个 pass”。真正难的不是写匹配条件，而是**在改的过程中不破坏 IR 的结构不变量**：

- 一个 `Value` 被删除了，但它的 use-list 里还有用户；
- 一个 `Block` 没有 terminator 了；
- 一个 `Operation*` 在 `erase` 之后还被继续解引用；
- 一个 op 被原地修改了，但 analysis 和 listener 都不知道。

这些错误在 Debug build 里通常有断言，在 Optimized build 里可能变成静默的错误 IR，直到几个 pass 之后才炸。本章的规则不是“风格建议”，而是避免 use-after-free 和无效 IR 的最低要求。

## 先建立直觉：IR 是“有序链表 + 双向 use 链”

把 MLIR 的 IR 想成三层结构：

```text
Region
  └── Block（双向链表）
        └── Operation（双向链表，iplist）
              ├── operands  → 每个 operand 是一条 use 节点
              ├── results   → 每个 result 维护自己的 use-list
              └── regions   → 可以继续嵌套
```

三个关键事实：

1. **Operation 在 Block 里是有序链表**，`erase` 会把它从链表摘掉并销毁；之前拿到的 `Operation*`、迭代器都可能失效。
2. **每个 result 有一条 use-list**，`replaceAllUsesWith` 会遍历这条链，把所有 user 的 operand 指向新值。
3. **Block 和 Region 是拥有关系**，`Block::erase` 会删除块；`Region::dropAllReferences` 只断引用，不删块。

`Operation::erase` 的实现在 `mlir/lib/IR/Operation.cpp:530-535`：

```cpp
void Operation::erase() {
  if (auto *parent = getBlock())
    parent->getOperations().erase(this);
  else
    destroy();
}
```

`Block::erase` 在 `mlir/lib/IR/Block.cpp:66-69`，`Region::dropAllReferences` 在 `mlir/include/mlir/IR/Region.h:284`。记住这三个入口，后面所有“顺序”问题都围绕它们。

## 核心概念一：walk 的顺序与提前递增

`walk` 的默认顺序是 **PostOrder**：先访问子 op，再访问父 op。`WalkOrder` 定义在 `mlir/include/mlir/IR/Visitors.h:28`：

```cpp
enum class WalkOrder { PreOrder, PostOrder };
```

对 `func.func` 来说，PostOrder 会先看到 `arith.constant`、`arith.addi`、`func.return`，最后才看到 `func.func`；PreOrder 反过来。这个差异在“边 walk 边 erase”时非常关键。

`Visitors.h:100-115` 的注释说明：walk 对 region 使用普通迭代，因为 region 不能在回调里被 erase；对 block 和 op 则使用提前递增（early increment），所以 **PreOrder 下 erase 当前 op 是允许的**，前提是你之后不再使用这个迭代器。`Visitors.h:197-244` 是 `make_early_inc_range` 的实现；`Block.h:384-385` 也在用同样的模式。

但“允许”不等于“推荐”。更稳的写法是：

- 用 PostOrder，先处理子节点，再处理父节点；
- 或者两阶段：先 `walk` 收集到 `SmallVector<Operation *>`，再统一改；
- 如果必须 PreOrder 删除，删完用 `WalkResult::skip()` 跳过子树，或者保证不再触碰当前迭代器。

`Region.h:299-302` 把规则写得很直白：回调里可以 erase 当前 op/block，当且仅当 walk 是 PostOrder，或者 PreOrder 且删除后 skip。

## 核心概念二：IRRewriter 与 Listener

裸 `Operation*` 能做的事，`IRRewriter` 都能做，而且会通知 listener。`IRRewriter` 定义在 `mlir/include/mlir/IR/PatternMatch.h:780-786`：

```cpp
class IRRewriter : public RewriterBase {
public:
  explicit IRRewriter(MLIRContext *ctx, OpBuilder::Listener *listener = nullptr);
  explicit IRRewriter(const OpBuilder &builder);
  explicit IRRewriter(Operation *op, OpBuilder::Listener *listener = nullptr);
};
```

常用方法：

| 方法 | 行号 | 作用 |
|---|---|---|
| `replaceOp(op, ValueRange)` | 518 | RAUW 所有结果，然后 erase op |
| `replaceOp(op, Operation*)` | 523 | 用新 op 的结果替换 |
| `replaceOpWithNewOp<OpTy>` | 529 | 构造新 op 并替换 |
| `eraseOp(op)` | 543 | 删除无 uses 的 op |
| `eraseBlock(block)` | 546 | 删除 block（带通知） |
| `inlineBlockBefore` | 566 | 把 block 内联到目标位置 |
| `mergeBlocks` | 591 | 合并两个 block |
| `splitBlock` | 595 | 在指定位置切块 |
| `replaceAllUsesWith` | 652 | 只做 RAUW，不删 op |

Listener 的接口在 `PatternMatch.h:368-425`：`notifyOperationInserted`、`notifyOperationErased`、`notifyOperationReplaced`、`notifyOperationModified`、`notifyBlockInserted`、`notifyBlockErased`。你可以继承 `RewriterBase::Listener`，只覆写关心的事件。

为什么要在意 listener？因为 analysis、transform dialect 的 handle、调试器都靠这些通知维持状态。你绕过 rewriter 直接 `op->erase()`，它们不会知道。

## 核心概念三：use-list、RAUW 与 erase 顺序

`Operation::replaceAllUsesWith` 的实现在 `mlir/include/mlir/IR/Operation.h:296-299`，它转调 `ValueRange::replaceAllUsesWith`；底层是 `UseDefLists.h:211-216`：

```cpp
void replaceAllUsesWith(ValueT &&newValue) {
  assert((!newValue || this != OperandType::getUseList(newValue)) &&
         "cannot RAUW a value with itself");
  while (!use_empty())
    use_begin()->set(newValue);
}
```

顺序只有一条铁律：**先让结果没有 uses，再 erase 定义它的 op。**

正确写法：

```cpp
// 方案 A：用 rewriter，一步完成 RAUW + erase
rewriter.replaceOp(op, replacementValues);

// 方案 B：手动，先 RAUW 再 erase
op->replaceAllUsesWith(replacementValues);
op->erase();
```

错误写法：

```cpp
op->erase();  // 如果还有 uses，Debug build 会报 "operation destroyed but still has uses"
```

`Operation.cpp:179-186` 的析构检查会遍历 `getUsers()`，把每个 user 作为 note 打出来：

```text
error: operation destroyed but still has uses
note: - use: ...
```

在 Optimized build 里，这个检查被 `#ifndef NDEBUG` 包住；没有断言时，你得到的是悬空的 use-list，而不是一个漂亮的错误。

`UseDefLists.h:197-199` 还有一条更底层的断言：

```cpp
~IRObjectWithUseList() {
  assert(use_empty() && "Cannot destroy a value that still has uses!");
}
```

所以“先 RAUW 再 erase”不是风格问题，是对象析构的前提。

## 核心概念四：block/region 修改的失效规则

Block 和 Region 的修改比 op 更容易出事，因为它们涉及**作用域**和**支配关系**。

### Block

- `Block::erase`（`Block.h:92`）会删除块本身；块参数和块内 op 都会被销毁。
- 删除块之前，块参数的 uses 必须已经处理掉。`Block::dropAllDefinedValueUses`（`Block.cpp:94-100`）会遍历参数和 op，断掉它们定义值的 uses；它适合“整块要删”的场景。
- 删除块之后，指向该块的 `Block*`、迭代器、以及块内 op 的指针都失效。

### Region

- `Region::dropAllReferences`（`Region.h:284`）只断引用，不删块；适合“我要把 body 换掉”的场景。
- `Region::takeBody`（`Region.h:265-269`）先 `dropAllReferences`，再 `blocks.clear()`，最后 splice 另一个 region 的块。它不会帮你处理外部 uses，调用前要自己想清楚。
- 直接 `region->getBlocks().clear()` 是危险操作：它绕过 rewriter 的通知，也不会检查 uses。

### 支配关系

插入一个新 op 到 use 之后，SSA 支配关系就被破坏；把 op 从 def 之前移到 use 之后，同样。MLIR 的 verifier 会检查支配关系，但 **rewriter 不会自动帮你维护 DominanceInfo**。如果你在 pass 里缓存了 `DominanceInfo`，改完 IR 要重新计算或让 pass manager 失效它（Ch 6）。

一个实用规则：**在同一个 block 内，新 op 插在 use 之前；跨 block 移动，先确认支配关系，再移动。**

## 底层机制：erase 之后指针为什么不能再用

MLIR 的 `Operation` 通常分配在 block 的 `iplist` 节点里；`erase` 会调用 `iplist::erase`，析构并释放节点。你手里的 `Operation*` 指向的就是那块内存。之后：

- 读 `op->getName()` 是 use-after-free；
- 读 `op->getResult(0)` 是 use-after-free；
- 把它和另一个指针比较通常“看起来没事”，但这是未定义行为。

Debug build 有时能靠 poisoning 抓到，Optimized build 不会。**不要用“我跑了一遍没崩”来证明安全。**

同样的问题也适用于：

- `Block*`：`Block::erase` 后失效；
- `Region*`：region 被 `takeBody`/`clear` 后，指向旧 block 的指针失效；
- `OpOperand*`：op 被 erase 后，operand 自然失效；
- `Value`：定义它的 op 被 erase 后，`Value` 失效（即使 use-list 已经空了）。

## 示例一：walk 顺序（实测）

我们写了一个最小工具 `modify-demo`，输入：

```mlir
func.func @f(%x: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %dead = arith.addi %x, %x : i32
  %0 = arith.addi %x, %c0 : i32
  %1 = arith.muli %0, %0 : i32
  return %1 : i32
}
```

`--walk-order` 的核心代码：

```cpp
getOperation()->walk<WalkOrder::PreOrder>([](Operation *op) {
  llvm::outs() << "  " << op->getName() << "\n";
});
getOperation()->walk<WalkOrder::PostOrder>([](Operation *op) {
  llvm::outs() << "  " << op->getName() << "\n";
});
```

实测输出：

```text
pre-order:
  func.func
  arith.constant
  arith.addi
  arith.addi
  arith.muli
  func.return
post-order:
  arith.constant
  arith.addi
  arith.addi
  arith.muli
  func.return
  func.func
```

结论：PreOrder 先父后子，PostOrder 先子后父。删除整棵子树时，PostOrder 更自然；只删当前 op、不碰子树时，PreOrder + skip 也可以。

## 示例二：IRRewriter + Listener（实测）

`--rewrite-demo` 做两件事：

1. 先 `walk` 收集所有 `arith.addi`，**不在 walk 里改**；
2. 再用 `IRRewriter::replaceOp` 把 `addi(x, 0)` 替换成 `x`，listener 统计 replaced/erased。

核心代码：

```cpp
CountingListener listener;
IRRewriter rewriter(&getContext(), &listener);

SmallVector<Operation *> worklist;
getOperation()->walk([&](Operation *op) {
  if (isa<arith::AddIOp>(op))
    worklist.push_back(op);
});

for (Operation *op : worklist) {
  auto add = cast<arith::AddIOp>(op);
  if (matchPattern(add.getRhs(), m_Zero()))
    rewriter.replaceOp(op, add.getLhs());
}
```

实测输出：

```text
replaced=1 erased=1
module {
  func.func @f(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %0 = arith.addi %arg0, %arg0 : i32
    %1 = arith.muli %arg0, %arg0 : i32
    return %1 : i32
  }
}
```

注意 `replaced=1 erased=1`：`replaceOp` 先 RAUW，再 erase，所以 listener 同时收到 replaced 和 erased。死掉的 `%dead = addi %x, %x` 没有 uses，但我们的模式只匹配 `x + 0`，所以它留着；后面的 `--erase-dead` 才会清掉它。

## 示例三：正确的 collect-then-erase（实测）

`--erase-dead` 的正确版本：

```cpp
SmallVector<Operation *> dead;
getOperation()->walk([&](Operation *op) {
  if (op->use_empty() && isMemoryEffectFree(op) && op->getNumRegions() == 0 &&
      !op->hasTrait<OpTrait::IsTerminator>())
    dead.push_back(op);
});
for (Operation *op : llvm::reverse(dead))
  op->erase();
```

实测输出：

```mlir
module {
  func.func @f(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %0 = arith.addi %arg0, %c0_i32 : i32
    %1 = arith.muli %0, %0 : i32
    return %1 : i32
  }
}
```

`%dead` 被删了；`%c0_i32` 还在，因为收集时它还被 `addi` 使用，删掉 `addi` 之后它才变成死代码。这说明 DCE 往往要**跑到不动点**：一轮收集只能看到“当前已经死”的 op，下一轮才会看到“因为上一轮删除而变死”的 op。`--canonicalize` 的 greedy driver 就是反复跑到不动点。

## 示例四：常见错误模式（实测失败）

把条件放松成“只要 `use_empty()` 且 `isMemoryEffectFree` 就删”：

```cpp
if (op->use_empty() && isMemoryEffectFree(op))
  dead.push_back(op);
```

`func.return` 没有结果、没有内存效果，于是也被收进 `dead`。删掉它之后，block 没有 terminator，verifier 立刻报错：

```text
.scratch\ch09-demo\test\demo.mlir:5:8: error: block with no terminator, has %2 = "arith.muli"(%1, %1) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
  %1 = arith.muli %0, %0 : i32
       ^
.scratch\ch09-demo\test\demo.mlir:5:8: note: see current operation: %2 = "arith.muli"(%1, %1) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
```

这个例子的教训不是“记住排除 `func.return`”，而是：**`use_empty` + `isMemoryEffectFree` 不是删除的充分条件。** 还要考虑 terminator、带 region 的 op、符号定义、以及 op 自己的验证规则。

另一个常见错误模式是“在 PreOrder walk 里直接 erase，然后继续用当前迭代器”。这种写法在 Debug build 可能触发断言，在 Optimized build 可能静默损坏链表。本章标 `guided`：本机是 Optimized build，不适合故意制造 UB。正确做法是 PostOrder、两阶段收集，或者 PreOrder 删除后 `WalkResult::skip()`。

## 对比与取舍

| 方式 | 适用场景 | 有 listener | 有回滚 | 风险 |
|---|---|---|---|---|
| 裸 `Operation*` + `erase` | 非常局部、确定无 uses | 否 | 否 | 高 |
| `IRRewriter` | pass 内的常规修改 | 是 | 否 | 中 |
| `PatternRewriter` | RewritePattern 内 | 是 | 部分驱动支持 | 低 |
| 两阶段收集 + rewriter | 批量删除/替换 | 是 | 否 | 低 |
| `applyPatternsGreedily` | 规则化重写 | 是（可配） | 驱动管理 | 低 |

选择建议：

- 在 pass 里改 IR，优先用 `IRRewriter`；
- 在 pattern 里，只能用 `PatternRewriter`；
- 批量删除，先收集再删；
- 需要通知 analysis/transform handle，必须走 rewriter；
- 只有在“确定没有 uses、没有 listener 依赖、生命周期极短”时，才考虑裸 `erase`。

## 踩雷集锦

1. **在 walk 回调里 `op->erase()`，然后继续用 `op`。** PreOrder 下 erase 当前 op 是允许的，但之后不能再解引用；更稳的是 PostOrder 或两阶段。

2. **先 `erase` 再 RAUW。** 结果还有 uses，Debug build 报 “operation destroyed but still has uses”；Optimized build 留下悬空 use。

3. **RAUW 的类型不匹配。** `replaceAllUsesWith` 要求新值的类型和旧值兼容；类型不对会触发断言或生成无效 IR。替换前用 `Value` 的类型检查。

4. **删除 block 前不处理块参数 uses。** `Block::erase` 不会替你 RAUW；块参数还有外部 uses 时，删块就是悬空引用。

5. **直接 `region->getBlocks().clear()`。** 绕过 listener，也不检查 uses；用 `Region::dropAllReferences` / `takeBody` 或 rewriter 的 block API。

6. **在 pass 里缓存 `DominanceInfo`，改完 IR 继续用。** 插入/移动 op 会改变支配关系；重新计算或让 analysis 失效。

7. **用“跑一遍没崩”证明 erase 安全。** Optimized build 没有断言，use-after-free 可能看起来正常。用 verifier、ASan（如果可用）和 Code Review 兜底。

8. **忘记 `func.return` 是 terminator。** DCE 条件只看 `use_empty` + `isMemoryEffectFree` 会把 terminator 删掉，直接产生无效 block。

## 进阶：再往深一层

- **Listener 链**：`RewriterBase::ForwardingListener`（`PatternMatch.h:431-480`）可以把通知转发给另一个 listener，适合同时接调试器和 transform handle。
- **`notifyOperationModified`**：原地修改属性/operand 时，用 `rewriter.modifyOpInPlace` 或 `startOpModification`/`finalizeOpModification`，否则 analysis 不知道。
- **`PatternRewriter` 的回滚**：部分 driver 支持在 pattern 失败时回滚；`IRRewriter` 没有这个能力。写 pattern 时不要绕过 `PatternRewriter`。
- **Block 级重写**：`eraseBlock`、`inlineBlockBefore`、`mergeBlocks`、`splitBlock` 都在 `PatternMatch.h:546-595`，优先用它们而不是手工 `iplist` 操作。
- **DominanceInfo 失效**：Ch 6 讲过 analysis 失效；修改 IR 后，pass manager 会在下一个 pass 前重新计算，但同一个 pass 内缓存的 analysis 要自己处理。

## 动手练习

1. **复现 walk 顺序**：用本章的 `demo.mlir`，分别跑 PreOrder 和 PostOrder，画出访问顺序树。

2. **改一个错误条件**：把 `--erase-dead` 的 `!op->hasTrait<OpTrait::IsTerminator>()` 去掉，确认 verifier 报错，并解释为什么错误发生在 pass 之后而不是 `erase` 当时。

3. **两阶段 vs 边 walk 边删**：把 `--erase-dead` 改成在 walk 回调里直接 `op->erase()`，在 Debug build（如果你有）里观察断言；没有 Debug build 就标 guided，并写出你会怎么验证。

4. **RAUW 顺序**：写一个 pass，把 `%0 = arith.addi %x, %x` 替换成 `%x`，要求先 RAUW 再 erase；用 `--mlir-print-ir-after-all` 观察 IR。

5. **Block 删除**：构造一个带两个 block 的 `cf` 函数，删除不可达 block 前先处理块参数 uses，验证 verifier 通过。

6. **Listener 计数**：给 `--rewrite-demo` 加一个 `notifyOperationInserted` 计数，观察 `replaceOp` 是否触发 inserted（提示：`replaceOp` 不一定插入新 op）。

## 本章重点整理

- IR 是“有序链表 + use 链 + 拥有关系”的组合；erase 会销毁对象，之前拿到的指针失效。
- walk 默认 PostOrder；PreOrder 下 erase 当前 op 需要 skip，最稳的是两阶段收集。
- `IRRewriter` 的 `replaceOp` = RAUW + erase，并通知 listener；`eraseOp` 只负责删除。
- 先 RAUW 再 erase 是硬性顺序；否则 Debug build 报“still has uses”，Optimized build 留悬空引用。
- `Block::erase` 不处理 uses；块参数和块内 op 的 uses 要先清理。
- `Region::dropAllReferences` 只断引用，`takeBody` 会清空当前 body；不要直接 `clear()`。
- `use_empty` + `isMemoryEffectFree` 不是 DCE 的充分条件；terminator、region、符号都要单独考虑。
- Optimized build 没有断言，“没崩”不等于“安全”。

## 自我检核

- [ ] 我能解释 PreOrder 和 PostOrder 在删除场景下的差异
- [ ] 我知道 `replaceOp` 内部做了哪两步
- [ ] 我能说出 `Operation::erase` 之后哪些指针会失效
- [ ] 我知道 `Block::erase` 之前要处理块参数 uses
- [ ] 我能解释为什么 DCE 要跑到不动点
- [ ] 我能说出 `use_empty` + `isMemoryEffectFree` 为什么会误删 terminator
- [ ] 我知道 `IRRewriter` 和 `PatternRewriter` 的适用边界
- [ ] 我不会用“跑一遍没崩”证明 erase 安全

## 延伸阅读

- **[MLIR Walkers 文档](https://mlir.llvm.org/docs/Walkers/)**
  - **读哪里**：walk 的顺序、`WalkResult`、提前递增。
  - **学什么**：如何在遍历中安全地修改 IR。
  - **和本章的关联**：示例一的顺序与删除规则。

- **[mlir/include/mlir/IR/Visitors.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Visitors.h)**
  - **读哪里**：`WalkOrder`、`detail::walk`、`make_early_inc_range`。
  - **学什么**：walk 的真实实现与 erase 安全条件。
  - **和本章的关联**：解释“为什么 PreOrder 删除要 skip”。

- **[mlir/include/mlir/IR/PatternMatch.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/PatternMatch.h)**
  - **读哪里**：`RewriterBase`、`IRRewriter`、`Listener`。
  - **学什么**：rewriter 的通知机制和 block API。
  - **和本章的关联**：示例二的 `replaceOp` 与 listener。

- **[mlir/include/mlir/IR/UseDefLists.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/UseDefLists.h)**
  - **读哪里**：`IRObjectWithUseList`、`replaceAllUsesWith`、`use_empty`。
  - **学什么**：use-list 的数据结构与 RAUW 的成本。
  - **和本章的关联**：解释“先 RAUW 再 erase”。

- **[mlir/lib/IR/Operation.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/IR/Operation.cpp)**
  - **读哪里**：析构检查和 `Operation::erase`。
  - **学什么**：Debug build 如何报告“still has uses”。
  - **和本章的关联**：示例四的错误模式。

---

下一章把“观察”做成流程：怎么让 pass 自己报错、怎么在 pass 边界打印 IR、怎么把失败输入缩到最小。

→ [Ch 10 调试、诊断与测试](./10-debugging-and-testing.md)