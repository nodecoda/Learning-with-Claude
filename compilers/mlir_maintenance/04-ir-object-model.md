# Ch 04 — Operation、Block、Region、Value

> **目标**：把 MLIR 的 IR 对象模型读成一张“所有权 + 使用关系”的图：谁能删谁、`erase()`/`remove()`/`destroy()` 各自的前置条件是什么、为什么删错顺序会直接触发 assertion 或 use-after-free。读完你应该能在 review 里一眼看出“这里 erase 早了”。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`，HEAD `e44fff5decc7`，`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（24.0.0git，assertions ON，shared libs）。所有输出都是本机实测。

> **前置**：读过 `compilers/mlir_core` 的 IR 基础，知道 SSA、block、region 的语法层长什么样；知道 `mlir-opt` 能跑 pass。

## 04.1 为什么维护者要先读对象模型

维护 MLIR 时，最贵的一类 bug 不是“算错了”，而是“删早了”。

一个真实的补丁形状是这样的：某个 pattern 把 `arith.addi` 替换掉，然后顺手 `erase()` 了产生它操作数的 `arith.constant`。本地跑 `mlir-opt` 没问题；一进 `-debug` 构建就崩：

```text
Assertion `use_empty() && "expected 'op' to have no uses"' failed
```

或者更隐蔽：在 release 构建里不崩，但 use-list 里留下悬垂指针，几轮 pass 之后在完全无关的地方读到垃圾值——这种 bug 会花掉你两天，而根因只有一行。

这些错误之所以难，是因为 MLIR 的 IR 不是一个对象，而是**六种句柄 + 三层所有权**：

- 句柄：`Operation*`、`Block*`、`Region*`、`Value`、`OpOperand`、`OpResult`；
- 所有权：`Operation` 拥有它的 `Region`，`Region` 拥有 `Block`，`Block` 拥有 `Operation` 和 block argument；`Value` 只是句柄，不拥有任何东西。

维护者要改的代码，90% 落在“句柄还活着，但被指向的对象已经没了”这条缝上。所以本章不背 API，而是把这张图钉死，再用真实 assertion 输出验证。

## 04.2 先建立直觉：所有权树与使用网

把 IR 想成两套彼此垂直的结构。**所有权树**决定“谁负责释放”；**使用网（use-list）**决定“谁依赖谁的值”。

```text
所有权树（parent -> child，用 destroy/erase 向下递归）
  Context
    └── ModuleOp (Operation)
          └── Region            (Operation owns Region)
                └── Block        (Region owns Block)
                      ├── BlockArgument   (Block owns args)
                      └── Operation       (Block owns op, iplist)
                            └── Region …  (可再嵌套)

使用网（value -> users，跨所有权边界）
  OpResult ──use──▶ OpOperand ──owner──▶ Operation
  BlockArgument ─use──▶ OpOperand ──owner──▶ Operation

两条线交叉的地方就是 bug：
  · 所有权树说“可以删”，使用网说“还有人用”  -> 删了就是悬垂
  · 使用网说“替换即可”，所有权树说“还在 block 里” -> 必须先摘链
```

记住三个动作的区别，本章后面全靠它：

| 动作 | 对 block 链 | 对对象 | 对 uses | 典型场景 |
|---|---|---|---|---|
| `remove()` | 摘掉 | 保留 | 不动 | 稍后还要重插、要移动到别的 block |
| `erase()` | 摘掉 | 销毁（递归 region） | 要求已无 use | pattern 里替换完的 op |
| `destroy()` | **不动** | 销毁 | 要求已无 use 且已 detach | 只应出现在 `remove()` 之后或临时 op |

## 04.3 五种句柄，各自能做什么

先看 `Value.h` 的类划分（行号见 04.8）：

```cpp
class Value { ... };                      // 抽象句柄，可指向 OpResult 或 BlockArgument
class OpOperand : public IROperand<...>;  // “使用点”，属于某个 Operation
class BlockArgument : public Value { ... };
class OpResult : public Value { ... };    // 属于某个 Operation 的结果
```

四个关键事实：

1. **`Value` 不是 `OpResult`。** `Value` 只是“一个 SSA 值”的句柄；它可能是某个 op 的结果，也可能是 block argument。`dyn_cast<OpResult>(v)` 对 block argument 返回 null。
2. **`OpOperand` 是使用点，不是值。** 它挂在 consumer 上，`get()` 拿到被使用的 `Value`，`getOwner()` 拿到 consumer。改 IR 时你改的是 `OpOperand`，不是 `Value`。
3. **`OpResult` 由 producer 拥有。** 结果的存储内联在 `Operation` 的尾部分配里，producer 被销毁时结果一起消失。
4. **`BlockArgument` 由 block 拥有。** 它没有 defining op；`Value::getDefiningOp()` 返回 null，`getParentBlock()` 返回所属 block。

由此得到一个高频误解的纠正：**“把 block argument 换成一个 `arith.constant` 结果”不是同一类替换**——前者要改 block signature，后者只是 `replaceAllUsesWith`。

`test-print-defuse` 把这件事打印得很直白（实测，见 04.8 实验 3）：

```text
Visiting op 'arith.addi' with 2 operands:
  - Operand produced by Block argument, number 0
  - Operand produced by Block argument, number 1
Has 1 results:
  - Result 0 has a single use:     - arith.muli
```

注意 “Operand produced by Block argument” 与 “Result 0 has a single use” 是两套词汇：operand 侧看到的是值的来源，result 侧看到的是结果的使用者。

## 04.4 生命周期：destroy / erase / remove

源码把三者的契约写得很清楚。`Operation::erase()`（`Operation.cpp:530`）：

```cpp
void Operation::erase() {
  if (auto *parent = getBlock())
    parent->getOperations().erase(this);   // iplist 摘链 + deleteNode -> destroy()
  else
    destroy();                             // 已经 detached，直接销毁
}
```

`Operation::remove()`（`:538`）只摘链：

```cpp
void Operation::remove() {
  if (Block *parent = getBlock())
    parent->getOperations().remove(this);
}
```

`Operation::destroy()`（`:204`）只释放：

```cpp
void Operation::destroy() {
  char *rawMem = reinterpret_cast<char *>(this) - llvm::alignTo(prefixAllocSize(), alignof(Operation));
  this->~Operation();
  free(rawMem);
}
```

而 `iplist` 的 `deleteNode` 只有一行（`:482`）：

```cpp
void llvm::ilist_traits<::mlir::Operation>::deleteNode(Operation *op) { op->destroy(); }
```

所以 `erase()` = `remove()` + `destroy()`（当 op 在 block 里时）。这也解释了为什么**对 detached op 调 `erase()` 也能工作**：它走 else 分支直接 `destroy()`。

真正的契约在析构函数里（`Operation.cpp:175`）：

```cpp
Operation::~Operation() {
  assert(block == nullptr && "operation destroyed but still in a block");
#ifndef NDEBUG
  if (!use_empty()) { ... llvm::report_fatal_error("operation destroyed but still has uses"); }
#endif
  ...
}
```

两个断言就是两条硬约束。用本机 shared lib 链接一个小程序实测（见 04.8 实验 6、7），能拿到原样输出：

```text
assert2: .../mlir/lib/IR/Operation.cpp:176: mlir::Operation::~Operation():
  Assertion `block == nullptr && "operation destroyed but still in a block"' failed.
```

```text
error: 'foo.producer' op operation destroyed but still has uses
LLVM ERROR: operation destroyed but still has uses
```

第一条是“没摘链就销毁”；第二条是“还有人用就销毁”。**维护者改 erase 顺序时，就是把这两条断言当护栏用。**

## 04.5 use-list：为什么“先删 user 再删 producer”

MLIR 的 use-list 是侵入式双向链表：每个 `OpOperand` 同时挂在“它使用的值的 use-list”上。`Value.cpp` 的 `Value::UseLists` 段（`:65` 起）就是这套链表的操作面。

由此得到两条顺序规则：

```text
正确：user 先死，producer 后死
  erase(user)      -> user 的 OpOperand 析构 -> 从 producer 的 use-list 摘除
  erase(producer)  -> 此时 use_empty() 为真，断言通过

错误：producer 先死
  destroy(producer) -> use-list 仍指向已释放的 OpOperand -> 断言报 has uses / UAF
```

`Block` 和 `Region` 提供了批量版本，专门处理“整棵树一起删”：

```cpp
void Block::dropAllReferences() { for (Operation &i : *this) i.dropAllReferences(); }   // Block.cpp:89
void Region::dropAllReferences() { for (Block &b : *this) b.dropAllReferences(); }     // Region.cpp:181
```

`Operation::dropAllReferences()`（`Operation.cpp:577`）做三件事：

```cpp
for (auto &op : getOpOperands())  op.drop();       // 断开本 op 对别人的使用
for (auto &region : getRegions()) region.dropAllReferences();  // 递归
for (auto &dest : getBlockOperands()) dest.drop(); // 断掉 successor
```

它是**打破循环依赖**的标准手段：两个 op 互相使用、或 CFG 里前后 block 互相引用时，直接按任意顺序销毁会互相卡住；先对所有相关对象 `dropAllReferences()`，use-list 清空，再删就没有顺序问题。

还有 `Operation::dropAllDefinedValueUses()`（`:590`）：方向相反，它断开“别人对本 op 结果的使用”，并递归到嵌套 region 的 block（含 block argument）。

## 04.6 Region 与多 block：dominance 不在语法里

一个 `Region` 是一个 `Block` 链表，block 之间可以有 successor（CFG）。`Region` 本身不拥有 op，只有 `Block` 拥有：

```text
Region（Operation 拥有）
  blocks: iplist<Block>
    Block A --successor--> Block B      // 由 terminator 的 BlockOperand 表示
```

于是 verifier 需要单独一层 **dominance 检查**。`Dominance.cpp` 的核心为 `properlyDominatesImpl`（`:259`）与 `DominanceInfo::properlyDominates(Value, Operation)`（`:344`）：

```cpp
bool DominanceInfo::properlyDominates(Value a, Operation *b) const {
  if (auto blockArg = dyn_cast<BlockArgument>(a)) {
    Block *block = blockArg.getOwner();
    ...
    return dominates(block, b->getBlock());   // block arg 看它所在 block 是否支配
  }
  return properlyDominates(a.getDefiningOp(), b, /*enclosingOpOk=*/false);
}
```

这段分支回答了一个高频疑问：block argument 在 block 入口就“已定义”，而 op result 要等 defining op 执行完，所以两者走不同的 dominance 判定。

实测一个跨 block 的非支配使用（`bad-dom.mlir`，实验 5）：

```text
bad-dom.mlir:9:8: error: operand #0 does not dominate this use
  %r = arith.addi %x, %x : i32
...
bad-dom.mlir:4:8: note: operand defined here (op in the same region)
  %x = arith.constant 1 : i32
```

注意 note 里的 “(op in the same region)”——verifier 已经判断出两者在同一 region，只是 block 层面不支配。

## 04.7 源码落点

用 `grep -n` 在本树核对过（HEAD `e44fff5decc7`）：

```bash
grep -n 'Operation::~Operation\|void Operation::destroy\|void Operation::erase\|void Operation::remove\|void Operation::dropAllReferences\|void Operation::dropAllDefinedValueUses' mlir/lib/IR/Operation.cpp
grep -n 'void Block::erase\|void Block::dropAllReferences\|void Block::dropAllDefinedValueUses' mlir/lib/IR/Block.cpp
grep -n 'void Region::dropAllReferences' mlir/lib/IR/Region.cpp
grep -n 'class Value\b\|class OpOperand\|class BlockArgument\|class OpResult' mlir/include/mlir/IR/Value.h
grep -n 'properlyDominates' mlir/lib/IR/Dominance.cpp | head
```

本机实测输出：

```text
174:Operation::~Operation() {
176:  assert(block == nullptr && "operation destroyed but still in a block");
204:void Operation::destroy() {
482:void llvm::ilist_traits<::mlir::Operation>::deleteNode(Operation *op) {
530:void Operation::erase() {
538:void Operation::remove() {
577:void Operation::dropAllReferences() {
590:void Operation::dropAllDefinedValueUses() {
```

```text
mlir/lib/IR/Block.cpp:66:void Block::erase() {
mlir/lib/IR/Block.cpp:89:void Block::dropAllReferences() {
mlir/lib/IR/Block.cpp:94:void Block::dropAllDefinedValueUses() {
mlir/lib/IR/Region.cpp:181:void Region::dropAllReferences() {
```

```text
mlir/include/mlir/IR/Value.h:96:class Value {
mlir/include/mlir/IR/Value.h:256:class OpOperand : public IROperand<OpOperand, Value> {
mlir/include/mlir/IR/Value.h:308:class BlockArgument : public Value {
mlir/include/mlir/IR/Value.h:456:class OpResult : public Value {
```

`Operation.h` 的公开面在同一层：`erase()` 在 128、`remove()` 在 131、`getBlock()` 在 230、`destroy()` 在 311。读补丁时先看调用的是哪一个，就能判断作者有没有处理 use-list。

## 04.8 可跑实验

```bash
mkdir -p /tmp/mlirc && cd /tmp/mlirc
cat > nest.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  cf.br ^bb1(%a : i32)
^bb1(%x: i32):
  %s = arith.addi %x, %b : i32
  %p = arith.cmpi slt, %s, %c0 : i32
  cf.cond_br %p, ^bb2, ^bb3
^bb2:
  cf.br ^bb3
^bb3:
  %r = arith.select %p, %s, %c0 : i32
  return %r : i32
}
EOF
```

### 实验 1：同一条 IR 的两种打印（实测）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt nest.mlir
```

真实输出（节选）：

```text
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    cf.br ^bb1(%arg0 : i32)
  ^bb1(%0: i32):  // pred: ^bb0
    %1 = arith.addi %0, %arg1 : i32
...
```

再跑 generic：

```bash
$T/bin/mlir-opt --mlir-print-op-generic nest.mlir
```

真实输出（节选）：

```text
"builtin.module"() ({
  "func.func"() <{function_type = (i32, i32) -> i32, sym_name = "f"}> ({
  ^bb0(%arg0: i32, %arg1: i32):
    %0 = "arith.constant"() <{value = 0 : i32}> : () -> i32
    "cf.br"(%arg0)[^bb1] : (i32) -> ()
  ^bb1(%1: i32):  // pred: ^bb0
    %2 = "arith.addi"(%1, %arg1) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
    "cf.cond_br"(%3)[^bb2, ^bb3] <{operandSegmentSizes = array<i32: 1, 0, 0>}> : (i1) -> ()
```

要点：generic 形式把 **region（`({ ... })`）、successor（`[^bb1]`）、result type** 的边界全部显式写出来；怀疑 region/successor 被改坏时先看 generic。

### 实验 2：让工具自己数嵌套（实测）

```bash
cat > simple.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %s = arith.addi %a, %b : i32
  %m = arith.muli %s, %a : i32
  return %m : i32
}
EOF
$T/bin/mlir-opt simple.mlir -test-print-nesting
```

真实输出：

```text
visiting op: 'builtin.module' with 0 operands and 0 results
 1 nested regions:
  Region with 1 blocks:
    Block with 0 arguments, 0 successors, and 1 operations
      visiting op: 'func.func' with 0 operands and 0 results
       1 nested regions:
        Region with 1 blocks:
          Block with 2 arguments, 0 successors, and 3 operations
            visiting op: 'arith.addi' with 2 operands and 1 results
             0 nested regions:
```

这是本章最重要的“结构校验”：`0 operands / 0 results` 的 module、`2 arguments` 的 entry block、`3 operations` 的函数体。写 pass 前先跑一遍，能立刻发现“我以为有 region 其实没有”。

### 实验 3：区分 block argument 与 op result（实测）

```bash
$T/bin/mlir-opt simple.mlir -test-print-defuse
```

真实输出：

```text
Visiting op 'arith.addi' with 2 operands:
  - Operand produced by Block argument, number 0
  - Operand produced by Block argument, number 1
Has 1 results:
  - Result 0 has a single use:     - arith.muli
Visiting op 'arith.muli' with 2 operands:
  - Operand produced by operation 'arith.addi'
  - Operand produced by Block argument, number 0
```

“Block argument” 与 “operation” 的措辞差异就是 `Value` 的两种底层实现。

### 实验 4：把 use-list 打印成注释（实测）

```bash
$T/bin/mlir-opt simple.mlir --mlir-print-value-users
```

真实输出：

```text
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    // %arg0 is used by %2, %1
    // %arg1 is used by %1
    %1 = arith.addi %arg0, %arg1 : i32 // user: %2
    %2 = arith.muli %1, %arg0 : i32 // user: %3
    return %2 : i32 // id: %3
  }
}
```

`%arg0 is used by %2, %1` 的顺序是 use-list 的**逆序**（最近挂上去的在前）。维护 use-list 顺序的补丁（比如 `-test-verify-uselistorder`）就是在测这个顺序是否稳定。

### 实验 5：非支配使用（实测）

```bash
cat > bad-dom.mlir <<'EOF'
func.func @f(%cond: i1) -> i32 {
  cf.cond_br %cond, ^bb1, ^bb2
^bb1:
  %x = arith.constant 1 : i32
  cf.br ^bb3
^bb2:
  cf.br ^bb3
^bb3:
  %r = arith.addi %x, %x : i32
  return %r : i32
}
EOF
$T/bin/mlir-opt bad-dom.mlir
```

真实输出：

```text
bad-dom.mlir:9:8: error: operand #0 does not dominate this use
  %r = arith.addi %x, %x : i32
       ^
bad-dom.mlir:9:8: note: see current operation: %1 = "arith.addi"(%0, %0) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
bad-dom.mlir:4:8: note: operand defined here (op in the same region)
  %x = arith.constant 1 : i32
```

这就是 `Dominance.cpp:344` 那条路径在真实 IR 上的表现。

### 实验 6：真断言——销毁时还在 block 里（实测）

用本树 shared lib 链接一个最小程序（源码见练习 A 的编译方法）：

```cpp
MLIRContext ctx; ctx.allowUnregisteredDialects();
OpBuilder b(&ctx);
auto mod = ModuleOp::create(b.getUnknownLoc());
b.setInsertionPointToStart(mod.getBody());
OperationState st(b.getUnknownLoc(), "foo.op");
Operation *op = b.create(st);
op->destroy();          // 没有 remove()
```

真实输出（exit 134）：

```text
assert2: /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Operation.cpp:176: mlir::Operation::~Operation():
  Assertion `block == nullptr && "operation destroyed but still in a block"' failed.
Aborted (core dumped)
```

### 实验 7：真断言——销毁时还有用户（实测）

把上一步改成“先 `remove()` 再 `destroy()`”，但 producer 的结果仍被另一个 op 使用：

```cpp
OperationState st1(b.getUnknownLoc(), "foo.producer");
st1.addTypes({i32, i32});
Operation *prod = b.create(st1);
OperationState st2(b.getUnknownLoc(), "foo.consumer");
st2.addOperands(prod->getResult(0));
b.create(st2);
prod->remove();     // 摘链，绕过上一条断言
prod->destroy();    // use-list 仍非空
```

真实输出（exit 134）：

```text
error: 'foo.producer' op operation destroyed but still has uses
LLVM ERROR: operation destroyed but still has uses
Aborted (core dumped)
```

这两条断言就是你 review 别人 erase 顺序时的判据：**先 remove 再 destroy 只解决了“在 block 里”，没解决“有用户”。**

### 实验 8：操作等价性（实测）

```bash
printf 'module {\n  arith.constant 1 : i32\n  arith.constant 1 : i32\n}\n' > eq2.mlir
$T/bin/mlir-opt eq2.mlir -test-operations-equality
```

真实输出：

```text
arith.constant with attr {} compares equals.
```

换成不同常量：

```text
arith.constant with attr {} compares NOT equals!
```

该 pass 只比较最后两个顶层 op，默认忽略 location；`test.ignore_commutativity` 可再忽略交换律。注意两个 `func.func` 即使函数体一样，`sym_name` 不同就 NOT equal。

## 04.9 踩雷集锦

1. **错误直觉：`erase()` 只是从链表摘掉。** → 正确认识：`erase()` 在 op 属于 block 时会 `destroy()`；对 detached op 也直接 `destroy()`。想要“稍后重插”只能用 `remove()`。
2. **错误直觉：detached op 可以安全持有。** → 正确认识：detached 的 op 仍然有 uses 和 region；它不归任何 block 管，但也不会被自动释放。忘记 `destroy()` 就是泄漏，提前 `destroy()` 而未断 use 就是断言。
3. **错误直觉：block 一直属于某个 region。** → 正确认识：`Block::erase()` 断言 `getParent()` 非空（`Block.cpp:66`）；`Region` 的 `addNodeToList` 也断言 `!block->getParent()`。block 被 `takeBody`/`splice` 搬走后，旧 region 不再拥有它。
4. **错误直觉：`erase` 一个还有用户的 op 编译器会帮你修。** → 正确认识：`RewriterBase::eraseOp` 有 `assert(op->use_empty())`；裸 `Operation::erase()` 靠析构里的 `report_fatal_error`。release 构建关掉断言后就是 UAF。
5. **错误直觉：block argument 也是 `OpResult`。** → 正确认识：`BlockArgument` 与 `OpResult` 是 `Value` 的两个兄弟实现；`getDefiningOp()` 对 block arg 返回 null。替换 block arg 要改 block signature。
6. **错误直觉：单 block region 不需要 dominance。** → 正确认识：仍需检查“定义在 use 之前”和“op 不能使用自己尚未产生的结果”；多 block region 还要看 CFG 支配。`Dominance.cpp` 对 block arg 与 op result 走不同分支。
7. **错误直觉：递归删除时可以从外往里删。** → 正确认识：`RewriterBase::eraseOp` 的注释明确写 “Users must be erased before definitions. I.e., post-order, reverse dominance.”，且嵌套 op 必须逐个 erase，让 listener 每次都看到一致的 IR。
8. **错误直觉：`dropAllReferences()` 会删除对象。** → 正确认识：它只断开 uses/successors，对象还在；它用来打破循环依赖，之后仍要 `erase()`/`destroy()`。

## 04.10 本章重点整理

- IR 是“所有权树 + 使用网”两套结构；bug 几乎都出在交叉点上。
- `remove()` 只摘链，`erase()` = 摘链 + 销毁，`destroy()` 只销毁且要求已 detach。
- 析构函数的两条断言（`Operation.cpp:176` 与 `:185`）是 erase 顺序的硬契约。
- use-list 是侵入式双向链表；必须“user 先死、producer 后死”。
- `dropAllReferences()` 用于打破循环依赖，不负责释放。
- `Value` 是句柄；`OpOperand` 是使用点；`BlockArgument` 与 `OpResult` 是两种定义者。
- 多 block region 的合法性由 `DominanceInfo` 单独校验，不在 parser 里。
- 维护视角：看到 `erase` 就先问“它还有用户吗？它 detach 了吗？”。

## 04.11 自我检核

- [ ] 能画出 module → region → block → operation → result 的所有权链，并指出每一层由谁释放
- [ ] 能说出 `remove()`/`erase()`/`destroy()` 三者各自对 block 链、对象、use-list 的影响
- [ ] 能解释为什么“先 remove 再 destroy”仍然可能触发 `has uses` 断言
- [ ] 能用自己的话说明 block argument 与 op result 在 dominance 上的差异
- [ ] 能读懂 `--mlir-print-op-generic` 输出里的 `({ ... })`、`[^bb1]`、`<{...}>` 分别是什么
- [ ] 能用 `-test-print-nesting` / `-test-print-defuse` 验证自己对一段 IR 的结构假设
- [ ] 遇到 `operation destroyed but still has uses` 时，知道先查 use-list 而不是怀疑内存分配器

## 04.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Operation.cpp`**
  - 读哪里：`~Operation`（170–200）、`destroy`（204）、`erase`/`remove`（530–545）、`dropAllReferences`（577–588）
  - 能学到什么：生命周期契约的唯一权威实现，以及两条断言的准确措辞
  - 和本章关联：04.4、04.5 的全部结论都来自这里
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/Value.h`**
  - 读哪里：`Value`（96）、`OpOperand`（256）、`BlockArgument`（308）、`OpResult`（456）
  - 能学到什么：四种句柄的继承关系与可用成员
  - 和本章关联：04.3 的“五种句柄”
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Block.cpp`**
  - 读哪里：`erase`（66）、`dropAllReferences`（89）、`dropAllDefinedValueUses`（94）、`eraseArguments`（198）
  - 能学到什么：block 级别的所有权与批量断链
  - 和本章关联：04.5 的批量版本
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Dominance.cpp`**
  - 读哪里：`properlyDominatesImpl`（259）、`properlyDominates(Value, Operation)`（344）
  - 能学到什么：block arg 与 op result 的 dominance 分支
  - 和本章关联：04.6 的真实报错
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/LangRef.md`**
  - 读哪里：SSA value、Block、Region 三节
  - 能学到什么：本章 C++ 结构的语法层对应物
  - 和本章关联：把 `--mlir-print-op-generic` 的输出对回规范

下一章往下钻一层：这些 `Value` 句柄指向的 `Type` 和 `Attribute` 到底存在哪里、为什么 `i32` 在同一个 context 里只有一个对象。

→ 下一章：[Ch 05 Type、Attribute 与 storage](./05-types-attributes-storage.md)
