# Ch 4 — `Value` / `User` / `Use`：def-use 链表与操作数所有权

> **目标**：把 IR 从「一行行文本」看成一张有向图：`Value` 是节点，`Use` 是边，`User` 是边的所有者。学完这章，你能说清一次 `replaceAllUsesWith` 到底改了哪几个指针、为什么删除一个仍被使用的值会在断言构建里立刻爆炸，以及遍历 users 时为什么不能随手 erase。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验工具来自 `/home/ubuntu/llvm/builds/pr-lane/bin`，`assertions ON`。本章的 C++ 小工具用 `llvm-config --cxxflags --ldflags --libs core irreader support` 编译，运行时需要 `LD_LIBRARY_PATH=$($B/llvm-config --libdir)`。

## 先建立直觉

LLVM IR 的合法性很大一部分建立在 SSA 的 def-use 关系上。文本里你看到的是：

```llvm
%y = add i32 %x, 1
%z = mul i32 %y, 2
ret i32 %z
```

内存里看到的是三组互相指向的对象：

```
%x ◄── Use ──┐
              │ operand 0
%y ◄── Use ──┼─ %z = mul
   ▲          │
   └── Use ──┘
%z ◄── Use ── ret i32 %z
```

每条「谁用了谁」的记录是一个 `Use`。`Use` 同时挂在两个链表上：

- **def 侧**：`Value::UseList` 把所有使用这个值的 `Use` 串起来。这就是 `uses()`/`users()` 能遍历的东西。
- **user 侧**：`User` 的 operand 数组按顺序存放自己的 `Use`。`getOperand(i)` 就是读第 i 个 `Use` 里的 `Value*`。

## 三个类与所有权

三个类的声明位置先钉住：

```cpp
// llvm/include/llvm/IR/Value.h:75
class Value {
...
protected:
  Use *UseList = nullptr;   // Value.h:121

// llvm/include/llvm/IR/User.h:44
class User : public Value {

// llvm/include/llvm/IR/Use.h:35
class Use {
...
  Value *Val = nullptr;     // Use.h:82
  Use *Next = nullptr;      // Use.h:83
  Use **Prev = nullptr;     // Use.h:84
  User *Parent = nullptr;   // Use.h:85
```

所有权关系可以一句话概括：**`User` 拥有 `Use` 的存储，`Value` 不拥有任何 `Use`，`Use` 指向两者**。

- `Value` 只知道「有哪些 `Use` 指向我」，不知道它们存在哪。
- `User` 在构造时分配 operand 存储，里面是一排 `Use`；`Use::Parent` 指回它，`Use::Val` 指向被使用的值。

### `Use` 数组嵌在 `User` 前面

固定 operand 数量的 `User`（例如二元运算）把 `Use` 数组和对象放在同一块分配里，位置在 `User` 对象**之前**：

```
低地址                                          高地址
[ Use[0] ][ Use[1] ] ... [ User 对象 ]
                              ▲ this
        Operands = reinterpret_cast<Use *>(this) - NumUserOperands
```

源码就是这么定位的：

```cpp
// llvm/include/llvm/IR/User.h:132
Use *Operands = reinterpret_cast<Use *>(this) - NumUserOperands;
for (unsigned I = 0; I < NumUserOperands; ++I)
  new (&Operands[I]) Use(this);
```

这种布局叫 intrusive operand storage：operand 是随对象一起分配的一排 `Use`，不是 `std::vector<Value*>`。好处是少一次间接寻址和堆分配；代价是对象大小与 operand 数量绑定。

### 固定 operand 与可变 operand

不是所有 `User` 都用同一种布局。`OperandTraits` 决定分配策略：

```cpp
// llvm/include/llvm/IR/InstrTypes.h:430
struct OperandTraits<BinaryOperator> :
  public FixedNumOperandTraits<BinaryOperator, 2> {
```

二元运算固定两个 operand。可变的类用另外两种：

```cpp
// llvm/include/llvm/IR/Instructions.h:2944
template <> struct OperandTraits<PHINode> : public HungoffOperandTraits {};

// llvm/include/llvm/IR/InstrTypes.h:2444
struct OperandTraits<CallBase> : public VariadicOperandTraits<CallBase> {};
```

- **intrusive**：operand 数组和对象同一块内存，数量在构造时固定；**hung off**：`User` 里存一个 `Use*`，数组单独分配，可以增长。

`PHINode::addIncoming` 在容量不足时调用 `growOperands()`，然后 `setNumHungOffUseOperands(getNumOperands() + 1)`（`Instructions.h:2855-2857`）。后者只允许 hung-off 的类调用：

```cpp
// llvm/include/llvm/IR/User.h:241
assert(HasHungOffUses && "Must have hung off uses to use this method");
```

这给出一个常见不变量：**普通指令的 operand 数量在构造后不变**。`BinaryOperator` 永远是 2 个；`PHINode`、`CallBase` 才能在生命周期内增删。

## def-use API 的真实语义

| API | 源码 | 返回/副作用 |
| --- | --- | --- |
| `use_empty()` | `Value.h:348` | `UseList == nullptr`，O(1) |
| `uses()` | `Value.h:382` | `iterator_range<use_iterator>`，元素是 `Use&` |
| `users()` | `Value.h:428` | `iterator_range<user_iterator>`，元素是 `User*` |
| `user_begin()` / `user_end()` | `Value.h:404`、`Value.h:412` | 同上，显式迭代器 |
| `hasOneUse()` | `Value.h:441` | 是否恰好一个 use，避免全遍历 |
| `replaceAllUsesWith(V)` | `Value.h:302` | 把所有 use 指向 `V`，完成后 `use_empty()` 为真 |
| `replaceUsesOfWith(From, To)` | `User.h:333` | 只改**这个 user** 的 operand，返回是否改动 |
| `dropAllReferences()` | `User.h:324` | 把所有 operand `Use::set(nullptr)` |

两个 `replace` 作用域不同：`replaceAllUsesWith` 站在被使用的值这一侧，改所有 user；`replaceUsesOfWith` 站在某个 user 这一侧，只改自己的 operand。

### `Use::set` 是唯一的写入口

所有 operand 改动最终都落到 `Use::set`：

```cpp
// llvm/include/llvm/IR/Value.h:876
void Use::set(Value *V) {
  removeFromList();
  Val = V;
  if (V)
    V->addUse(*this);
}
```

顺序很重要：先摘旧链，再改 `Val`，最后挂新链。`addUse` 只做插入（`Value.h:515`），`addToList`/`removeFromList` 在 `Use.h:87` 和 `Use.h:95`。`Prev` 是 `Use**`：表头是 `Value::UseList`，非表头指向前一个节点的 `Next`。删除时不需要查找前驱。

### `replaceAllUsesWith` 的完整路径

```cpp
// llvm/lib/IR/Value.cpp:513
void Value::doRAUW(Value *New, ReplaceMetadataUses ReplaceMetaUses) {
  assert(hasUseList() && "Cannot replace constant data");
  assert(New && "Value::replaceAllUsesWith(<null>) is invalid!");
  assert(!contains(New, this) &&
         "this->replaceAllUsesWith(expr(this)) is NOT valid!");
  assert(New->getType() == getType() &&
         "replaceAllUses of value with new value of different type!");
```

四个断言分别防：常量数据没有 use 链、空替换、自引用、类型不一致。随后通知 `ValueHandleBase::ValueIsRAUWd`（`Value.cpp:523`）。主循环每次取表头，处理常量特例，然后 `U.set(New)`：

```cpp
// llvm/lib/IR/Value.cpp:527
while (!materialized_use_empty()) {
  Use &U = *UseList;
  if (auto *C = dyn_cast<Constant>(U.getUser())) {
    if (!isa<GlobalValue>(C)) {
      C->handleOperandChange(this, New);
      continue;
    }
  }
  U.set(New);
}
```

常量特例：`ConstantExpr` 等是 uniqued 的，直接改 operand 会破坏常量池唯一性，所以走 `handleOperandChange`；`GlobalValue` 虽然也是 `Constant`，但它是可变全局对象，走普通 `U.set`。

### `dropAllReferences` 与析构顺序

```cpp
// llvm/include/llvm/IR/User.h:324
void dropAllReferences() {
  for (Use &U : operands())
    U.set(nullptr);
}
```

它把所有 operand 置空，让被引用值的 use 数归零，但**不删除任何对象**。边界写在注释里：

```cpp
// llvm/include/llvm/IR/User.h:318
/// Note that no operations are valid on an object that has "dropped
/// all references", except operator delete.
```

所以典型用法是「先集体断开引用，再集体删除」，例如 `Function::dropAllReferences` 和 `Module` 析构路径。之后还去读 operand、打印指令、跑 verifier，都会撞上空指针。

## 关键不变量

### 1. 删除前必须 `use_empty()`

`Value` 的析构在断言构建里会检查这件事：

```cpp
// llvm/lib/IR/Value.cpp:72
Value::~Value() {
...
  if (!materialized_use_empty()) {
    dbgs() << "While deleting: " << *VTy << " %" << getName() << "\n";
    for (auto *U : users())
      dbgs() << "Use still stuck around after Def is destroyed:" << *U << "\n";

    llvm_unreachable("Uses remain when a value is destroyed!");
  }
```

这里用 `materialized_use_empty()` 而不是 `use_empty()`：后者可能向下转型到 `GlobalValue`，而派生类此刻已经析构，属于 UB。

更早的防线在删除指令的封装上：

```cpp
// llvm/lib/Transforms/InstCombine/InstCombineInternal.h:508
LLVM_DEBUG(dbgs() << "IC: ERASE " << I << '\n');
assert(I.use_empty() && "Cannot erase instruction that is used!");
```

`eraseInstFromFunction` 先断言，再 salvage 调试信息，再 `eraseFromParent()`。这个顺序保证了删除时不会留下悬空 use。

### 2. RAUW 的类型必须匹配

`doRAUW` 的第四个断言是类型检查（`Value.cpp:518`）。需要改类型时，先插 `bitcast`/`trunc`/`zext` 等转换指令，再 RAUW 成转换结果；不要指望 RAUW 帮你转换。

### 3. operand 数量通常固定

固定 operand 的类在构造时决定 `NumUserOperands`，之后不能改。只有 hung-off 的类能调 `setNumHungOffUseOperands`（`User.h:239`）。`PHINode` 和 `CallBase` 是常见例外；`BinaryOperator`、`CmpInst`、`CastInst` 等都不是。

### 4. `ConstantData` 没有 use 链

`hasUseList()` 的实现是一行（`Value.h:346`）：`return !isa<ConstantData>(this);`。`ConstantInt`、`ConstantFP` 这类 `ConstantData` 不记录 uses。对它们调 `use_empty()` 会得到 `true`，即使有指令在用这个常量；`uses()`/`users()` 会命中 `assert(hasUseList())`。写工具时先判断 `hasUseList()`。

## 危险场景

### 遍历 users 时 RAUW/erase

`use_iterator` 保存当前 `Use*`，自增时读 `U->getNext()`（`Value.h:147`）。循环体里摘掉当前 use，`getNext()` 可能已指向被释放或重排的节点。安全写法有三种：

1. **先收集再处理**：把 `User*` 收进 `SmallVector`，离开遍历后再改。
2. **`make_early_inc_range`**：在改动当前元素前先自增迭代器（`STLExtras.h:649`）。
3. **`WeakTrackingVH`**：句柄在 RAUW 时跟随新值，在删除时置空（`ValueHandle.h:224`）。

InstCombine 自己就用了第三种（`InstructionCombining.cpp:3958`：`SmallVector<WeakTrackingVH, 64> Users(...)`）。注释说明了原因：先收集裸指针，只在确实要删除分配点时转成 `WeakTrackingVH`，避免为每个 user 付注册/注销开销。

### `dropAllReferences` 之后继续用对象

`dropAllReferences` 只保证「可以 delete」，不保证「还能读」。之后调用 `verifyFunction`，verifier 会在 `Verifier.cpp:568` 看到 null operand 并报 `Operand is null`。对象已经处于「只允许析构」的状态。

### `ValueHandle` 的三种强度

| 句柄 | 源码 | RAUW 时 | 删除时 |
| --- | --- | --- | --- |
| `WeakVH` | `ValueHandle.h:172` | 置空 | 置空 |
| `WeakTrackingVH` | `ValueHandle.h:224` | 跟随新值 | 置空 |
| `AssertingVH` | `ValueHandle.h:280` | 不跟随，访问时断言 | 断言 |

选择：只关心「还活着吗」用 `WeakVH`；要跨 RAUW 继续指向同一逻辑值用 `WeakTrackingVH`；确信生命周期覆盖句柄时用 `AssertingVH` 抓悬空。

## 实验一：打印 users 与 `use_empty()`

写一个最小工具：解析 `.ll`，对每条指令打印 `use_empty()`、user 数量和每个 user。核心是 `for (Instruction &I : instructions(F))` 加 `for (User *U : I.users())`。

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/clang++ -std=c++17 defuse.cpp \
  $($B/llvm-config --cxxflags --ldflags --libs core irreader support --system-libs) \
  -o defuse
export LD_LIBRARY_PATH=$($B/llvm-config --libdir)
./defuse f.ll
```

输入是 `%y = add i32 %x, 1`、`%z = mul i32 %y, 2`、`ret i32 %z`。实测输出：

```text
inst:   %y = add i32 %x, 1 | use_empty=0 users=1
  user:   %z = mul i32 %y, 2
inst:   %z = mul i32 %y, 2 | use_empty=0 users=1
  user:   ret i32 %z
inst:   ret i32 %z | use_empty=1 users=0
```

`ret` 没有用户，所以 `use_empty=1`；`%y` 和 `%z` 各有一个用户。

## 实验二：合法 RAUW 与类型不匹配

把 `%y` 替换成同类型的参数 `%x`：

```text
after-rauw:   %y = add i32 %x, 1 | use_empty=1 users=0
--- module ---
define i32 @f(i32 %x) {
  %y = add i32 %x, 1
  %z = mul i32 %x, 2
  ret i32 %z
}
```

`%y` 仍然存在（工具没有删它），但已经 `use_empty=1`；`%z` 的 operand 变成 `%x`。RAUW **只改 use，不删定义**。把 `%y` 替换成 `i64 7`，断言立即触发：

```text
defuse: .../llvm/lib/IR/Value.cpp:518: void llvm::Value::doRAUW(...):
Assertion `New->getType() == getType() &&
"replaceAllUses of value with new value of different type!"' failed.
```

类型检查不是文档约定，是运行期断言。

## 实验三：从 `IC: Replacing` 到 IR 差异

用 `add i32 %x, 0` 观察 InstCombine 的 RAUW：

```bash
$B/opt -passes=instcombine -debug-only=instcombine -S addzero.ll
```

关键输出：

```text
IC: Visiting:   %y = add i32 %x, 0
IC: Replacing   %y = add i32 %x, 0
    with i32 %x
IC: ERASE   %y = add i32 %x, 0
IC: Visiting:   ret i32 %x
```

`IC: Replacing` 来自 `InstCombiner.h:434-435`，紧接着的 `I.replaceAllUsesWith(V)` 在 `InstCombiner.h:440`。`visitAdd` 先用 `simplifyAddInst` 得到 `%x`，再走 `replaceInstUsesWith`；旧指令已死，主循环随后 `eraseInstFromFunction` 打出 `IC: ERASE`。

同一变化的文本视图：`llvm-as addzero.ll -o addzero.bc` 后 `llvm-dis addzero.bc -o -` 得到原始 IR：

```llvm
define i32 @f(i32 %x) {
  %y = add i32 %x, 0
  ret i32 %y
}
```

`opt -passes=instcombine addzero.bc -S -o -` 得到：

```llvm
define i32 @f(i32 %x) {
  ret i32 %x
}
```

`%y` 消失，`ret` 的 operand 从 `%y` 变成 `%x`。文本少了一行，内存里少了一个 `Instruction`、两个 `Use`，以及 `%x` use 链上的一次摘除和一次插入。

## 实验四：「删了还在用」的三种失败

**文本 IR：解析器先拒绝。**

```llvm
define i32 @f() {
  ret i32 %1
}
```

```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: undef.ll:2:11:
error: use of undefined value '%1'
```

这不是 verifier 的报错，是 `LLParser` 在解析阶段就拒绝（`LLParser.cpp:4050`）。文本 IR 无法构造「删掉定义但保留 use」的模块。

**C++ API：verifier 真正报错。** 先 `dropAllReferences()`，再 `verifyModule`：

```text
Operand is null
  %y = add
Operand is null
  %z = mul
Operand is null
  ret void
verifyModule broken=1
```

`Operand is null` 来自 `Verifier.cpp:568` 的 `Check(I.getOperand(i) != nullptr, ...)`。这是「operand 已断开、指令还在」的真实 verifier 反应。

**直接 erase 仍被使用的指令。**

```text
While deleting: i32 %y
Use still stuck around after Def is destroyed:  %z = mul i32 %y, 2
Uses remain when a value is destroyed!
UNREACHABLE executed at .../llvm/lib/IR/Value.cpp:94!
```

这条来自 `Value.cpp:72` 的析构检查（`llvm_unreachable` 在 `Value.cpp:94`）。它比 verifier 更早：对象还没进入下一个检查点，析构就先抓到悬空 use。

## 踩雷集锦

1. **以为 `replaceAllUsesWith` 会删掉旧值**。它只改 use；旧指令还在原位置，是否删除由调用者决定。忘了删会留下死指令，跑 DCE 才会消失。
2. **遍历 `users()` 时 erase 当前 user**。`use_iterator` 自增依赖当前 `Use::Next`；当前节点被摘掉后，迭代器可能失效。用 `make_early_inc_range` 或先收集 `WeakTrackingVH`。
3. **对 `ConstantData` 调 `users()`**。`ConstantInt`/`ConstantFP` 没有 use 链（`Value.h:346`），会命中 `assert(hasUseList())`；`use_empty()` 也不能用来判断它们是否被使用。
4. **`dropAllReferences` 之后继续读对象**。它只保证可以 delete，不保证还能 print/verify（`User.h:318` 的注释）。
5. **对 `Constant` 调 `replaceUsesOfWith`**。会触发 `User.cpp:29` 的断言 `Cannot call User::replaceUsesOfWith on a constant!`；常量是 uniqued 的，必须走 `handleOperandChange`。

## 动手练习

1. 把实验一的工具改成同时打印每个 `Use` 的 `getOperandNo()` 和 `getUser()`，验证 `%z` 的 operand 0 对应 `%y`。
2. 在 `%y` 上先 `replaceAllUsesWith(%x)`，再 `eraseFromParent()`，用 `verifyModule` 确认模块仍然合法。把顺序反过来，记录断言输出。
3. 写一个遍历 `V.users()` 并在循环里 `U->dropAllReferences()` 的实验，观察是否崩溃；如果不崩，解释为什么这次运气好，并给出安全改写。
4. 用 `opt -passes=instcombine -debug-only=instcombine` 找一条不是 `add` 的 RAUW，指出它走的是 `replaceInstUsesWith` 还是主循环的 `I->replaceAllUsesWith(Result)`。

## 本章重点整理

- `Value` 是节点，`User` 是 operand 的所有者，`Use` 是边；`Use` 同时存在于 def 侧链表和 user 侧数组。
- 固定 operand 的 `User` 把 `Use` 数组分配在对象前面；`PHINode`/`CallBase` 用 hung-off/variadic 存储，可以增长。
- `replaceAllUsesWith` 走 def 侧 use 链，只改 use 不删定义；`replaceUsesOfWith` 只改一个 user 的 operand。
- RAUW 要求类型一致；删除要求 `use_empty()`；`dropAllReferences` 之后对象只允许析构。
- `ConstantData` 没有 use 链，`use_empty()` 对它们不代表「没人用」。
- 遍历 users 时改动 use 链要用 early-inc、先收集或 `WeakTrackingVH`。

## 自我检核

- [ ] 能画出 `Value`、`User`、`Use` 三者的指针关系，并指出 `Use` 数组在内存里的位置
- [ ] 能解释为什么固定 operand 的指令不能在构造后改 operand 数量
- [ ] 能说出 `replaceAllUsesWith` 与 `replaceUsesOfWith` 各自站在哪一侧、改哪些对象
- [ ] 能背出 RAUW 的四个断言，并解释每一个防的是什么
- [ ] 能解释 `use_empty()` 与 `materialized_use_empty()` 为什么在析构里选择后者
- [ ] 能说出 `ConstantData` 为什么没有 use 链，以及这对写工具有什么影响
- [ ] 能给出三种安全遍历 users 并修改 IR 的写法
- [ ] 能解释 `dropAllReferences` 之后为什么不能 print/verify
- [ ] 能区分 `WeakVH`、`WeakTrackingVH`、`AssertingVH` 在 RAUW 和删除时的行为
- [ ] 能复现「erase 仍被使用的指令」的断言，并指出它发生在 `Value.cpp` 的哪一行

## 延伸阅读

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`The Value class`、`The User class`、`The Use class`、`Iterating over uses and users`
  - **学什么**：官方对 def-use 链与 `replaceAllUsesWith` 的描述；本章的内存布局部分可以和它对照
  - **前提**：知道 SSA 的基本定义；否则先读 Ch 3 的类层次
- **[LLVM source: `llvm/include/llvm/IR/Value.h`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/Value.h)**
  - **读哪里**：`use_iterator_impl`、`user_iterator_impl`、`addUse`、`replaceAllUsesWith` 声明
  - **学什么**：迭代器如何在 `Use*` 上做薄封装；`materialized_` 前缀的生命周期含义
  - **前提**：能读模板与嵌套类；本章已经拆过关键片段
- **[LLVM source: `llvm/lib/IR/Value.cpp` 的 `doRAUW`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/IR/Value.cpp)**
  - **读哪里**：`doRAUW` 的断言、常量特例、`ValueHandle` 通知、基本块后继修正
  - **学什么**：一次 RAUW 要处理哪些边角；为什么常量不能直接 `U.set`
  - **前提**：读过本章的 `Use::set` 与 `ConstantData` 两节
- **[Simple and Efficient Construction of Static Single Assignment Form](https://doi.org/10.1007/978-3-642-37051-9_6)**
  - **读哪里**：SSA 构造与 def-use 维护的基本算法（Braun et al., CC 2013）
  - **学什么**：为什么 def-use 链是 SSA 的核心数据结构；LLVM 的 intrusive 设计在算法层面的对应
  - **前提**：读过编译原理的 SSA 章节；否则先看 Ch 9 的 verifier 不变量
→ [Ch 5 — `Type` 与 `DataLayout`：类型系统、布局与 opaque pointer](./05-type-and-datalayout.md)
