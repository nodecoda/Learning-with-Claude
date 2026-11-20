# Ch 6 — `Module` / `Function` / `BasicBlock` / `Instruction`：容器、所有权、迭代器

> **目标**：把 LLVM IR 的四层容器关系、销毁顺序和迭代器失效规则钉死。学完这章，你能在 review 里一眼看出「这个循环删元素会不会踩空」「这个 `BasicBlock *` 在 `splice` 之后还能不能用」「这个 `eraseFromParent` 之前是不是还有 use」，并知道每个结论对应哪个源码位置。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，`RelWithDebInfo` + assertions ON。本章的遍历、拆分和崩溃实验都在这个构建上真实运行。

## 为什么需要这个？

LLVM review 里最常见的 use-after-free 不在复杂的算法，而在容器操作：

- 遍历 `BasicBlock` 时 `eraseFromParent()`，下一轮 `++It` 读到已经释放的节点。
- 缓存了一个 `Instruction *`，中间调用了 `splitBasicBlock`，再拿旧指针插指令。
- 以为 `Function::splice` 会重新分配 block，于是重新查找所有 block 指针；实际节点地址不变，但 parent 和编号变了。
- 删除一条还有 use 的指令，assertions 构建直接报 `Uses remain when a value is destroyed!`。

这些问题的共同点是：**容器的所有权语义和迭代器语义没有分开看**。本章先把「谁拥有谁」画清楚，再把「什么操作会让什么失效」列成表。

## 先建立直觉

LLVM 的 IR 容器是**侵入式链表**（intrusive list）：链表的 `next` / `prev` 指针嵌在元素对象里，而不是另建一个节点。这带来两个直接后果：

1. 元素对象地址稳定。`splice`、`moveBefore`、插入、删除其他元素，都不会搬动这个对象。
2. 删除元素就是销毁对象。指向它的指针、引用、迭代器立刻失效；没有「节点还在、值没了」的中间状态。

```text
Module
├─ GlobalList  : SymbolTableList<GlobalVariable>
├─ FunctionList: SymbolTableList<Function>
│    └─ Function
│         └─ BasicBlocks : SymbolTableList<BasicBlock>
│              └─ BasicBlock
│                   └─ InstList : SymbolTableList<Instruction, ...>
│                        └─ Instruction
├─ AliasList   : SymbolTableList<GlobalAlias>
└─ IFuncList   : SymbolTableList<GlobalIFunc>
```

- **所有权**：父对象的析构会清空列表，列表的 `clear()` / `erase()` 会 `delete` 元素。
- **父子关系与符号表**：元素插入/移除时，`SymbolTableListTraits` 更新 `Parent` 指针和名字符号表。

`SymbolTableList<T>` 的定义在 `llvm/include/llvm/IR/SymbolTableListTraits.h:125-129`，它继承自 `iplist_impl<simple_ilist<T, ...>, SymbolTableListTraits<T, ...>>`。`iplist_impl` 在 `llvm/include/llvm/ADT/ilist.h:110`，`ilist` 只是 `iplist` 的别名（`ilist.h:344`）。`iplist_impl::erase` 会 `deleteNode`（`ilist.h:203-207`），所以「从列表 erase」等于「销毁对象」。

## 容器与所有权

| 拥有者 | 列表类型 | 元素 | 源码 |
| --- | --- | --- | --- |
| `Module` | `GlobalListType` | `GlobalVariable` | `Module.h:73`，字段 `Module.h:218` |
| `Module` | `FunctionListType` | `Function` | `Module.h:75`，字段 `Module.h:219` |
| `Module` | `AliasListType` | `GlobalAlias` | `Module.h:77`，字段 `Module.h:220` |
| `Module` | `IFuncListType` | `GlobalIFunc` | `Module.h:79`，字段 `Module.h:221` |
| `Function` | `BasicBlockListType` | `BasicBlock` | `Function.h:67`，字段 `Function.h:80` |
| `BasicBlock` | `InstListType` | `Instruction` | `BasicBlock.h:64-65`，字段 `BasicBlock.h:76` |
| `Function` | `Argument` 数组 | `Argument` | `Function.h:88`，分配 `Function.cpp:534-550` |

几个关键点：

- `Module` 不是用 `unique_ptr<Function>` 持有函数，而是用 `SymbolTableList<Function>`。`Module::~Module` 先 `dropAllReferences()`，再对每个列表 `clear()`（`Module.cpp:118-126`）。`clear()` 会删除节点，节点的析构再带走下一层。
- `Function::~Function` 先 `dropAllReferences()`，再 `clearArguments()`（`Function.cpp:521-531`）。基本块列表作为成员，在 `Function` 析构后由 `BasicBlockListType` 的析构 `clear()` 删除；`BasicBlock::~BasicBlock` 再 `InstList.clear()`（`BasicBlock.cpp:172-194`）。
- `BasicBlock` 自己也是 `Value`，同时继承 `ilist_node_with_parent<BasicBlock, Function>`（`BasicBlock.h:61-62`）。这解释了两件事：block 可以作为 `blockaddress` 的值；block 节点里带着 `Parent` 和链表指针。
- `Instruction` 继承 `User` 和 `ilist_node_with_parent<Instruction, BasicBlock, ...>`（`Instruction.h:63-66`）。容器拥有指令对象，但指令的操作数指向的 `Value` 不归它所有；删除指令不会删除操作数。
- `Argument` 不走链表。`Function` 用 `std::allocator<Argument>` 分配一块连续数组（`Function.cpp:534-550`），`arg_begin()` 返回 `Argument *` 指针算术（`Function.h:862-877`）。所以 `Argument *` 在 function 生命周期内稳定，但 `stealArgumentListFrom` 或 function 销毁后失效。

`Module::dropAllReferences` 的存在是为了处理循环引用：先把所有函数、全局变量、别名、ifunc 的引用放掉，再让列表析构真正删除对象（`Module.cpp:595-615`）。读销毁路径时，不要只看 `~Module` 的两行，`dropAllReferences` 才是让「删除整个模块」安全的前置条件。

## 迭代器：三种粒度

| 迭代器 / range | 遍历什么 | 关键源码 |
| --- | --- | --- |
| `Module::iterator` | 函数 | `Module.h:93`，`begin/end` `Module.h:821-829` |
| `Function::iterator` | 基本块 | `Function.h:70`，`begin/end` `Function.h:847-850` |
| `BasicBlock::iterator` | 指令 | `BasicBlock.h:170`，`begin/end` `BasicBlock.h:440-454` |
| `instructions(F)` | 函数内所有指令，跨 block | `InstIterator.h:131`、`InstIterator.h:145` |
| `inst_begin(F)` / `inst_end(F)` | 手动控制同一种遍历（`InstIterator`） | `InstIterator.h:129-130`、`InstIterator.h:32-39` |

`InstIterator` 内部保存 basic block 列表指针、当前 block 迭代器、当前指令迭代器三样状态（`InstIterator.h:37-39`），所以它比双层循环多一层失效点：删除当前 block 会让 block 迭代器失效，删除当前指令会让指令迭代器失效。`instructions(F)` 不提供额外的失效保护。

### `getFirstNonPHI()` 的现状

很多旧代码会写 `Instruction *First = BB.getFirstNonPHI();`，当前树已经没有这个 API。`rg getFirstNonPHI` 只能找到 `getFirstNonPHIIt()`（`BasicBlock.h:292`），它在 2026-08-04 的 commit `62c5ede9fd14` 中把旧的 `getFirstNonPHI()` 删除。新代码用迭代器版本：`BasicBlock::iterator It = BB.getFirstNonPHIIt(); if (It != BB.end()) It->print(outs());`。

为什么返回迭代器而不是 `Instruction *`：注释明确说「不要把迭代器拆成 `Instruction *` 再插入」，因为迭代器里带着 debug-info 的位置信息（`BasicBlock.h:290-291`）。在 PHI 之后插入指令时，位置选错会把 `dbg.value` / `DbgRecord` 放到 PHI 前面，形成不合法的顺序。

相关的边界 API：

- `getTerminator()` 假定 block 是良构的，没有 terminator 会断言（`BasicBlock.h:237-243`）。处理可能不完整的 block，用 `getTerminatorOrNull()`（`BasicBlock.h:248-253`）。
- `getFirstInsertionPt()` 跳过 PHI 和 landingpad，返回适合插入非 PHI 指令的位置（`BasicBlock.h:322`）。
- `phis()` 只遍历开头连续的 PHI（`BasicBlock.h:509-512`）。

### 实测：遍历 module 并打印数量

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin; $B/clang++ $($B/llvm-config --cxxflags) walk.cpp -o walk \
  $($B/llvm-config --ldflags --libs core support)
LD_LIBRARY_PATH=/home/ubuntu/llvm/builds/pr-lane/lib ./walk
```

```cpp
for (Function &F : M) {
  outs() << F.getName() << " args=" << F.arg_size()
         << " blocks=" << F.size() << '\n';
  for (BasicBlock &BB : F) {
    outs() << "  " << BB.getName() << " insts=" << BB.size()
           << " first-non-phi=" << BB.getFirstNonPHIIt()->getOpcodeName()
           << " term=" << BB.getTerminator()->getOpcodeName() << '\n';
    for (Instruction &I : BB) outs() << "    " << I << '\n';
  }
  unsigned N = 0;
  for (Instruction &I : instructions(F)) (void)I, ++N;
  outs() << "  inst_iterator count=" << N << '\n';
}
```

构造一个两参数、一个 block 的函数，再在 `%y` 处拆分。实测输出（节选）：

```text
function f args=2 blocks=1
  entry insts=3 first-non-phi=add term=ret
      %x = add i32 %0, %1
      %y = mul i32 %x, 3
      ret i32 %y
  inst_iterator count=3
-- split at %y --
function f args=2 blocks=2
  entry insts=2 first-non-phi=add term=br
      %x = add i32 %0, %1
      br label %tail
  tail insts=2 first-non-phi=mul term=ret
  inst_iterator count=4
```

两个观察：

- `splitBasicBlock` 后 `entry` 多了一条 `br`，`%y` 和 `ret` 搬到 `tail`；block 顺序是 `entry` 在前、`tail` 在后。
- `instructions(F)` 的数量从 3 变成 4，因为新增了一条 `br`。它不是「原指令数」，而是「当前所有 block 的所有指令数」。

## 修改结构：每个操作的副作用

| 操作 | 做了什么 | 主要副作用 | 失效对象 |
| --- | --- | --- | --- |
| `splitBasicBlock(I)` | 在 `I` 处拆成两块，`I..end` 移到新块 | 旧块加 `br`；更新后继 PHI；新 `br` 继承 `I` 的 `DebugLoc` | 传入的迭代器、旧 block 的指令范围 |
| `splitBasicBlockBefore(I)` | 在 `I` 前插入新块，把 `begin..I` 移过去 | 前驱改指向新块；更新本块 PHI；PHI 多前驱时断言 | 传入的迭代器、前驱的 terminator 视图 |
| `BasicBlock::eraseFromParent()` | 从 function 摘除并删除 | 指令全部销毁；`blockaddress` 的 use 被替换 | block 指针、内部指令指针 |
| `BasicBlock::removeFromParent()` | 只摘除，不删除 | `Parent` 变 null；block 仍可查询/插入 | 依赖 parent 的 API |
| `moveBefore` / `moveAfter` | 在同一个或不同 function 内移动 block | 节点地址不变；parent/编号可能变 | 依赖 block 顺序的遍历 |
| `Function::splice` / `BasicBlock::splice` | 批量移动 block 或指令 | 节点地址不变；parent、符号表更新；指令顺序缓存失效 | 源/目标容器的 `end()` 之外的缓存位置 |
| `Instruction::eraseFromParent()` | 摘除并删除指令 | 返回下一条指令的迭代器；有 use 时断言 | 指令指针、所有指向它的 `Use` |
| `Instruction::removeFromParent()` | 只摘除，不删除 | 指令仍存活，可重新插入 | 依赖 parent 的 API |

### `splitBasicBlock`

实现从 `BasicBlock.cpp:491` 开始：

```cpp
BasicBlock *New = BasicBlock::Create(getContext(), BBName, getParent(),
                                     this->getNextNode());
DebugLoc Loc = I->getDebugLoc();
New->splice(New->end(), this, I, end());
UncondBrInst *BI = UncondBrInst::Create(New, this);
BI->setDebugLoc(Loc);
New->replaceSuccessorsPhiUsesWith(this, New);
```

按顺序读：

1. 新 block 插在旧 block 的下一个节点之前，也就是紧跟在旧 block 后面。
2. 在移动之前保存 `I->getDebugLoc()`，因为 `splice` 之后 `I` 可能不再属于旧 block。
3. `New->splice(New->end(), this, I, end())` 把 `[I, end)` 的指令移走，包括旧 terminator。
4. 旧 block 末尾插入 `br New`，并把拆分点的 `DebugLoc` 挂到这条 `br` 上。
5. `replaceSuccessorsPhiUsesWith(this, New)` 修改**后继 block** 的 PHI：原来从 `this` 来的 incoming value，现在要从 `New` 来。

注意它**没有**修改旧 block 自己的 PHI；旧 block 的 PHI 留在原地，因为它们仍然在 block 开头，控制流进入旧 block 的路径没有变。

头文件注释补充了两条限制（`BasicBlock.h:591-607`）：

- block 必须有 terminator，`I` 不能是 `end()`。
- `splitBasicBlock` 不保留任何 analysis；要在循环里拆块，用 `SplitBlock` 工具，让 `LoopInfo` / `DominatorTree` 一起更新。

### `splitBasicBlockBefore`

实现从 `BasicBlock.cpp:521` 开始，方向相反：

- 新 block 插在旧 block 之前。
- 把 `[begin, I)` 移到新 block，旧 block 保留 `[I, end)`。
- 遍历旧 block 的所有前驱，把 terminator 的后继从旧 block 改成新 block。
- 对旧 block 的 PHI 调 `replacePhiUsesWith(Pred, New)`，把 incoming block 从 `Pred` 改成 `New`。
- 最后在新 block 末尾加 `br` 指向旧 block。

它有一条额外断言：如果 `I` 是 PHI，旧 block 必须只有一个前驱（`BasicBlock.cpp:527-528`）。原因是多个前驱的 PHI 不能简单地把 incoming 边全部改到一个新 block；拆分点落在 PHI 中间会破坏 PHI 的语义。

### `eraseFromParent` 与 `removeFromParent`

`BasicBlock::eraseFromParent` 只有两行（`BasicBlock.cpp:209-211`）：

```cpp
iplist<BasicBlock>::iterator BasicBlock::eraseFromParent() {
  return getParent()->getBasicBlockList().erase(getIterator());
}
```

它返回下一个 block 的迭代器。删除动作会进入 `BasicBlock::~BasicBlock`（`BasicBlock.cpp:172-194`）：

- 如果 block 的地址被取过（`hasAddressTaken()`），找到对应的 `BlockAddress`，把它的所有 use 替换成 `inttoptr 1`，再销毁这个常量。这样不会留下悬空的 `blockaddress`。
- `dropAllReferences()` 放掉指令之间的引用。
- 删除所有 `DbgMarker`，再 `InstList.clear()`，销毁全部指令。

`removeFromParent` 只做 `remove`（`BasicBlock.cpp:207`），block 对象还在，`Parent` 变成 null。之后可以用 `insertInto` 插到另一个 function。不要在 `removeFromParent` 后调用 `getModule()` / `getParent()` 相关 API，除非你先重新插入。

### `moveBefore` / `moveAfter` 与 `splice`

`BasicBlock::moveBefore` 调 `getParent()->splice(...)`，`moveAfter` 调 `++MovePos->getIterator()` 后再 splice（`BasicBlock.cpp:212-219`）。block 节点地址不变，变的是链表位置和 `Parent`。

`Function::splice`（`Function.cpp:455-465`）把 block 从一个 function 移到另一个 function；`BasicBlock::splice`（`BasicBlock.cpp:929-951`）把指令从一个 block 移到另一个 block。真正更新 parent 和符号表的是 `SymbolTableListTraits::transferNodesFromList`（`SymbolTableListTraitsImpl.h:88-120`）：

- 跨 parent 时，从旧 `ValueSymbolTable` 移除名字，设置新 `Parent`，再插入新符号表。
- 同一 parent 内移动时，只更新 parent 字段。
- 无论哪种，都会 `invalidateParentIListOrdering`（`SymbolTableListTraitsImpl.h:90-93`），让指令顺序缓存失效。

`splice` 还会搬运 debug-info：`BasicBlock::splice` 先 `spliceDebugInfo`，再移动指令，最后 `flushTerminatorDbgRecords`（`BasicBlock.cpp:945-950`）。如果一条指令带着 `DbgRecord`，它会跟着走；空区间也有专门的 `spliceDebugInfoEmptyBlock` 处理。

一个容易忽略的边界：`moveBefore` 不检查你是不是在移动 entry block。把 entry block 移到别处，`Function::getEntryBlock()` 仍然返回 `front()`（`Function.h:803-804`），但控制流入口已经不是原来的 block。CFG 变换里不要对 entry block 做无保护的 `moveBefore`。

### 指令级操作

- `Instruction::removeFromParent()`（`Instruction.cpp:95-99`）只从 `InstList` 移除，并处理 `DbgMarker`；指令对象还在。
- `Instruction::eraseFromParent()`（`Instruction.cpp:108-111`）移除并删除，返回下一条指令的迭代器。
- `Instruction::moveBefore` / `moveAfter`（`Instruction.cpp:181-200`）移动单条指令，地址不变；`moveBeforePreserving` 会连同相邻 debug-info 一起移动。
- 删除前必须先处理 use。`Value` 析构时有 `Uses remain when a value is destroyed!` 断言（`Value.cpp:94`）；assertions 构建会 abort，release 构建会留下悬空 `Use`。

## 符号表：`getOrInsertFunction` 与 `Argument`

`Module::getOrInsertFunction` 的声明在 `Module.h:469-505`，实现从 `Module.cpp:212` 开始。它的契约是：

- 名字不存在：创建 `Function`，链接类型默认是 external，插入 module 的符号表。
- 名字已存在：返回已有的 `Function`，**不会**因为这次传入的 `FunctionType` 不同而改签名。
- 返回值是 `FunctionCallee`，里面同时带「这次传入的 `FunctionType *`」和「实际的 `Value *`」。

第三条是 review 里容易漏的坑。实测：

```cpp
FunctionCallee A = M.getOrInsertFunction("f", FunctionType::get(I32, {I32}, false));
FunctionCallee B = M.getOrInsertFunction("f", FunctionType::get(I64, {I64}, false));
```

输出：

```text
A param bits=32
B param bits=64
stored param bits=32
same Function*=1
```

`B.getFunctionType()` 仍然是 i64 版本，但 `M.getFunction("f")` 存的是 i32 版本，两个 `FunctionCallee` 指向同一个 `Function`。如果后续用 `B` 去创建 `call`，调用点类型和实际函数签名不一致；Verifier 不一定会拦住（LLVM 允许 call 的签名与 callee 定义不同），但调用约定和优化假设容易错。写代码时要么先 `getFunction` 检查，要么保证同名函数的类型一致；不要用 `getOrInsertFunction` 做「类型覆盖」。

`Module::getFunction` 是纯查找，找不到返回 null（`Module.h:509`）。`Function::arg_size()` 返回参数个数，不触发 lazy 构建（`Function.h:895`）；`arg_begin()` / `getArg(i)` 会调 `CheckLazyArguments()`，第一次访问时才分配 `Argument` 数组（`Function.h:862-883`）。

`Argument` 的所有权在 `Function`：

- 类定义在 `Argument.h:32`，`getArgNo()` 在 `Argument.h:50`。
- `Function` 的 `Arguments` 指针和 `NumArgs` 在 `Function.h:88-89`。
- `BuildLazyArguments` 用 `std::allocator<Argument>().allocate(NumArgs)` 一次性分配，逐个 placement-new（`Function.cpp:534-550`）。
- `clearArguments` 逐个析构再 `deallocate`（`Function.cpp:561-566`）。

所以 `Argument *` 不是链表节点，不能单独 `eraseFromParent`；参数个数由 `FunctionType` 决定，改签名要重建 `Function` 或走 `stealArgumentListFrom` 这类受控路径。

## 容器操作何时让指针/迭代器失效

先给结论表，再解释：

| 操作 | 指向其他元素的指针 | 指向被操作元素的指针/迭代器 | parent / 符号表 | analysis |
| --- | --- | --- | --- | --- |
| `insert` | 不变 | 新元素有效 | 设置为新 owner | 结构变了，相关 analysis 失效 |
| `remove` | 不变 | 对象仍存活，但 parent 为 null | 从旧符号表移除 | CFG/结构可能变 |
| `erase` | 不变 | **失效**，对象已 delete | 从旧符号表移除 | CFG/结构可能变 |
| `splice` / `moveBefore` | 不变 | 仍有效，节点地址不变 | 跨 owner 时更新 | 顺序/CFG 可能变 |
| `clear` / 析构 | 全部失效 | 全部失效 | 全部清空 | 全部失效 |
| `replaceAllUsesWith` | 不变 | 被替换的 value 仍存活 | 不变 | 使用关系变 |

几条实用规则：

1. **不要缓存 `end()` 之后又删除容器本身**。`end()` 是链表哨兵，插入/删除其他元素不会让它失效；容器析构后它当然失效。
2. **erase 当前元素后，不要用旧迭代器 `++`**。用 `eraseFromParent()` 的返回值，或 `make_early_inc_range`。
3. **erase 之前先 RAUW**。有 use 的指令不能直接删；先 `replaceAllUsesWith`，再 `eraseFromParent`。
4. **splice/move 之后不要重新查 block 指针**。节点地址没变，重新查只会增加代码；要更新的是 parent、编号、analysis。
5. **CFG 改变后，指针稳定不等于 analysis 稳定**。`DominatorTree`、`LoopInfo` 等需要显式更新或失效，这是 Ch 16 的主题。

### 失败例子：遍历时 erase

```cpp
for (auto It = BB->begin(), E = BB->end(); It != E; ++It) {
  if (It->getOpcode() == Instruction::Add) {
    It->eraseFromParent();
    errs() << "erased one, continuing\n";
  }
}
```

在一个有两个独立 `add` 的 block 上运行。实测输出：

```text
erased one, continuing
Segmentation fault (core dumped)
```

原因不是 `E` 失效，而是 `It`：`eraseFromParent()` 删除了 `It` 指向的 `Instruction`，下一轮 `++It` 去读已释放节点的 `next` 指针。这个例子在当前构建上稳定 segfault；换分配器、换优化级别，也可能表现成「跳过下一条指令」而不是崩溃，所以不能靠「没崩」判断安全。

安全的两种写法：

```cpp
// 用 eraseFromParent 的返回值
for (auto It = BB->begin(); It != BB->end();) {
  if (It->getOpcode() == Instruction::Add)
    It = It->eraseFromParent();
  else
    ++It;
}

// 或使用 early-inc range
for (Instruction &I : make_early_inc_range(*BB))
  if (I.getOpcode() == Instruction::Add)
    I.eraseFromParent();
```

第二种写法实测输出 `before adds=2`、`survived, insts=1 adds=0`，两个 `add` 都被删掉。

### 第二个失败例子：删除还有 use 的指令

```llvm
%x = add i32 %a, 1
%y = add i32 %x, 2
```

直接 `%x->eraseFromParent()`，assertions 构建输出：

```text
While deleting: i32 %x
Use still stuck around after Def is destroyed:  %y = add i32 %x, 2
Uses remain when a value is destroyed!
UNREACHABLE executed at .../llvm/lib/IR/Value.cpp:94!
```

正确顺序是 `%x->replaceAllUsesWith(NewValue)`，确认没有 use 之后再删除。这个断言是 review 里判断「作者有没有处理 use」的直接证据。

## 实测：block 顺序与 PHI

写一段带循环和 PHI 的 IR（节选）：

```llvm
entry:
  %c = icmp sgt i32 %n, 0
  br i1 %c, label %loop, label %exit
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %acc = phi i32 [ 0, %entry ], [ %acc.next, %loop ]
  %i.next = add i32 %i, 1
  %done = icmp eq i32 %i.next, %n
  br i1 %done, label %exit, label %loop
exit:
  %r = phi i32 [ 0, %entry ], [ %acc.next, %loop ]
```

用 `opt -S -passes=verify` 重新打印，block 顺序和 PHI 都保留：

```text
entry:
  %c = icmp sgt i32 %n, 0
  br i1 %c, label %loop, label %exit

loop:                                             ; preds = %loop, %entry
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %acc = phi i32 [ 0, %entry ], [ %acc.next, %loop ]
  ...
exit:                                             ; preds = %loop, %entry
  %r = phi i32 [ 0, %entry ], [ %acc.next, %loop ]
  ret i32 %r
```

`print<domtree>` 则从分析角度给出同一张图：

```bash
$B/opt -passes='print<domtree>' -disable-output cfg.ll
```

```text
DominatorTree for function: f
  [1] %entry
    [2] %loop
    [2] %exit
```

这里有一个工具边界：`llvm-dis` 只吃 bitcode，直接喂 `.ll` 会报 `Invalid bitcode signature`。要看文本 IR，用 `opt -S`；要反汇编 `.bc`，才用 `llvm-dis`。

## 踩雷集锦

1. **遍历时 `eraseFromParent()` 后继续 `++It`**：`It` 已指向被删除对象。错误直觉是「erase 只改链表，节点还在」；正确认识是「erase 就是 delete，旧迭代器立刻失效」。
2. **删除还有 use 的指令**：assertions 构建报 `Uses remain when a value is destroyed!`，release 构建留下悬空 `Use`。正确顺序是先 RAUW，再删除。
3. **继续调用 `getFirstNonPHI()`**：当前树只有 `getFirstNonPHIIt()`，旧的 `getFirstNonPHI()` 在 `62c5ede9fd14` 删除。用迭代器版本，且不要在插入前把它拆成 `Instruction *`。
4. **以为 `splice`/`moveBefore` 会重新分配 block**：节点地址不变，重新查找指针是多余的；要更新的是 parent、编号和 analysis。反过来，erase 之后继续用旧 block 指针才是真的 UAF。
5. **用 `getOrInsertFunction` 覆盖同名函数签名**：已有函数不会被改类型，返回的 `FunctionCallee` 可能和实际存着的 `Function` 类型不一致。先 `getFunction` 检查，或保证同名同类型。
6. **对不完整 block 调 `getTerminator()`**：没有 terminator 会断言；中间构造阶段用 `getTerminatorOrNull()`。
7. **把 `llvm-dis` 当 `.ll` 解析器**：它只读 bitcode。文本 IR 用 `opt -S`。

## 动手练习

1. 在一个有后继 PHI 的 block 上调用 `splitBasicBlock`，打印拆分前后后继 PHI 的 incoming block，确认 `replaceSuccessorsPhiUsesWith` 改了什么。
2. 把本章的 `erase_bad.cpp` 改成 `make_early_inc_range` 版本，确认两个 `add` 都被删除；再把删除条件改成「删除有 use 的指令」，观察断言。
3. 用 `Module::getOrInsertFunction` 先插入 `i32(i32)`，再插入 `i64(i64)`，打印两个 `FunctionCallee` 的类型和实际 `Function` 的类型，解释为什么不一致。
4. 构造一个 `blockaddress` 指向某个 block，删除该 block，观察 `BasicBlock::~BasicBlock` 如何把 use 替换成 `inttoptr 1`。

## 本章重点整理

- `Module` 拥有全局变量、函数、别名、ifunc；`Function` 拥有基本块；`BasicBlock` 拥有指令。所有权通过 `SymbolTableList` / `iplist_impl` 的 `erase` / `clear` 实现。
- 容器是侵入式链表：节点地址稳定，erase 就是销毁对象。指针稳定和迭代器有效是两件事。
- `Function::iterator`、`BasicBlock::iterator`、`instructions(F)` 是三种粒度；`InstIterator` 同时持有 block 和指令迭代器。
- `splitBasicBlock` 在后继 PHI 上改 incoming block；`splitBasicBlockBefore` 在前驱和本块 PHI 上改；两者都不保留 analysis。
- `eraseFromParent` 删除并返回下一个迭代器；`removeFromParent` 只摘除。删除有 use 的指令前必须先 RAUW。
- `getOrInsertFunction` 不覆盖已有签名；`Argument` 由 `Function` 以数组形式拥有，不是链表节点。

## 自我检核

- [ ] 不看源码，能画出 `Module → Function → BasicBlock → Instruction` 的所有权链，并说出每层用的列表类型
- [ ] 能解释 `SymbolTableList` 与 `iplist_impl` / `simple_ilist` 的关系，以及为什么节点地址稳定
- [ ] 能说出 `Module::~Module`、`Function::~Function`、`BasicBlock::~BasicBlock` 各自先做什么、再删除什么
- [ ] 能区分 `Function::iterator`、`BasicBlock::iterator`、`instructions(F)` 三种遍历，并说出 `InstIterator` 保存了哪些状态
- [ ] 能解释 `getFirstNonPHIIt()` 为什么返回迭代器，以及旧 `getFirstNonPHI()` 的现状
- [ ] 能逐条说出 `splitBasicBlock` 和 `splitBasicBlockBefore` 对 PHI、前驱/后继、DebugLoc 的影响
- [ ] 能解释 `eraseFromParent`、`removeFromParent`、`moveBefore`、`splice` 各自让什么失效、让什么保持有效
- [ ] 能写出至少两种「遍历时删除指令」的安全写法，并说明为什么旧写法会崩
- [ ] 能解释为什么删除有 use 的指令会触发 `Uses remain when a value is destroyed!`

## 延伸阅读

- **[LLVM Programmer's Manual: Iterating over Instructions](https://llvm.org/docs/ProgrammersManual.html#iterating-over-the-instructions-in-a-function)**
  - **读哪里**：`inst_iterator`、`instructions()`、`early_inc_range` 相关段落
  - **能学到什么**：官方推荐的遍历写法，以及为什么「遍历时删除」需要 early-inc 模式
  - **前提**：知道 `Function` / `BasicBlock` 的 `begin/end` 返回什么
- **[LLVM Programmer's Manual: The Value/User/Use classes](https://llvm.org/docs/ProgrammersManual.html#the-value-user-and-use-classes)**
  - **读哪里**：`Value` 的 use 链、`replaceAllUsesWith`、`dropAllReferences`
  - **能学到什么**：为什么删除有 use 的值会断言，以及 RAUW 在删除前的作用
  - **前提**：读过本课 Ch 4 的 def-use 链表
- **[LLVM ADT: ilist and simple_ilist](https://llvm.org/docs/ProgrammersManual.html#llvm-adt-ilist-h)**
  - **读哪里**：`ilist`、`iplist`、intrusive list 的设计说明
  - **能学到什么**：节点地址为什么稳定、`erase` 与 `remove` 的所有权差别
  - **前提**：能读 C++ 模板和迭代器适配
- **[LLVM Doxygen: BasicBlock](https://llvm.org/doxygen/classllvm_1_1BasicBlock.html)**
  - **读哪里**：`splitBasicBlock`、`splitBasicBlockBefore`、`getFirstNonPHIIt`、`getTerminator` 的注释
  - **能学到什么**：每个结构修改 API 的前置条件和返回值；和本章的源码行号对照阅读
  - **前提**：知道 PHI 的基本语义

→ [Ch 7 — 常量、全局值与调用契约](./07-constants-globals-attributes.md)
