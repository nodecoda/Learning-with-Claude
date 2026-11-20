# Ch 3 — 读 LLVM 源码的方法论：类层次、`dyn_cast`、visitor、测试反推

> **目标**：把「打开一个陌生的 `.cpp`，从函数名猜到行为」换成一套可重复的动作：先画类层次，再用 `isa`/`dyn_cast` 判断动态类型，顺着 visitor 的分派找到具体实现，最后用测试和 `git log -S` 反推这段代码为什么存在。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验工具来自 `/home/ubuntu/llvm/builds/pr-lane/bin`，`assertions ON`。本章 `file:line` 以这棵源码树为准；构建产物若与源码有小幅漂移，先用 `rg` 回查符号，再跑命令确认行为。

## 为什么需要方法论？

LLVM 的源码量会让「从 main 往下读」很快失效：`llvm/lib` 下有 3184 个 `.cpp`，一个 `opt` pipeline 会跨几十个文件。真正高效的读法不是读得多，而是**每一步都在缩小候选集合**：

```
符号 → rg → 头文件（继承/classof） → 实现（visitor/宏）
     → 调用点与测试 → git log -S → review 结论
```

这套动作的价值在于：它不依赖你记得某个 pass 的名字。你要记住**当前要找的符号**，以及每一步该看什么。

## 第一步：画最小继承图

LLVM 的 IR 对象模型有一个很小的骨架。先把骨架画出来，后面所有 `dyn_cast` 才有落点：

```
Value (Value.h:75)
├── Argument (Argument.h:32)
├── BasicBlock (BasicBlock.h:61)
└── User (User.h:44)
    ├── Instruction (Instruction.h:63)
    ├── Operator (Operator.h:33)          ← 工具类，不是 Instruction 的基类
    └── Constant (Constant.h:44)
        └── GlobalValue (GlobalValue.h:49)
            ├── Function (Function.h:65)
            └── GlobalVariable (GlobalVariable.h:41)
```

这张图里最容易读错的是 `Operator`。它确实继承 `User`，也确实提供 `getOpcode()`，但 `Instruction` **不继承** `Operator`；两者是兄弟。`Operator` 的 `classof` 同时接受 `Instruction` 和 `ConstantExpr`：

```cpp
// llvm/include/llvm/IR/Operator.h:59
static bool classof(const Instruction *) { return true; }
static bool classof(const ConstantExpr *) { return true; }
static bool classof(const Value *V) {
  return isa<Instruction>(V) || isa<ConstantExpr>(V);
}
```

所以 `dyn_cast<Operator>(V)` 是在问「这个值能不能按 operator 的方式取 opcode」，而不是在问「它是不是某个具体指令类的父类实例」。读到一个 `Operator *` 时，先确认它指向的是 `Instruction` 还是 `ConstantExpr`，再决定能不能调用指令专属的 API。

### `ValueID` 与 `classof`

LLVM 不在 `Value` 上放虚函数来做运行时类型判断。每个 `Value` 构造时带一个 `SubclassID`，`getValueID()` 直接返回它：

```cpp
// llvm/include/llvm/IR/Value.h:545
unsigned getValueID() const {
  return SubclassID;
}
```

指令的 opcode 也编码在这个 ID 里：

```cpp
// llvm/include/llvm/IR/Instruction.h:348
unsigned getOpcode() const { return getValueID() - InstructionVal; }
```

`Instruction::classof` 因此可以写成一个范围判断：

```cpp
// llvm/include/llvm/IR/Instruction.h:1038
static bool classof(const Value *V) {
  return V->getValueID() >= Value::InstructionVal;
}
```

具体指令类再往下收窄。比如 `UnaryOperator`：

```cpp
// llvm/include/llvm/IR/InstrTypes.h:168
static bool classof(const Instruction *I) {
  return I->isUnaryOp();
}
static bool classof(const Value *V) {
  return isa<Instruction>(V) && classof(cast<Instruction>(V));
}
```

这是一条固定的两层写法：**先确认是 `Instruction`，再按 opcode 范围或具体 opcode 判断**。读任何 `classof` 时，先找它依赖的是 `ValueID` 还是 opcode。

## 第二步：`isa` / `dyn_cast` / `cast` / `dyn_cast_or_null`

这四个名字的区别不在语法，而在**失败时你要什么**：

| 操作 | 源码位置 | 失败时 | 典型场景 |
| --- | --- | --- | --- |
| `isa<T>(V)` | `Casting.h:547` | 返回 `false` | 只判断类型，不取指针 |
| `cast<T>(V)` | `Casting.h:559`、`Casting.h:571` | assertions 构建直接 abort | 已经确定类型，失败是程序 bug |
| `dyn_cast<T>(V)` | `Casting.h:643`、`Casting.h:655` | 类型不对返回 `nullptr` | `if (auto *I = dyn_cast<...>(V))` |
| `dyn_cast_if_present<T>(V)` | `Casting.h:732`、`Casting.h:744` | 空指针或类型不对返回 `nullptr` | 输入可能为空 |
| `dyn_cast_or_null<T>(V)` | `Casting.h:753` | 同上，但已标记 deprecated | 旧代码兼容，不建议新写 |

`isa` 和 `cast` 的断言在 `Casting.h` 里写得很直白：

```cpp
// llvm/include/llvm/Support/Casting.h:559
assert(isa<To>(Val) && "cast<Ty>() argument of incompatible type!");

// llvm/include/llvm/Support/Casting.h:655
assert(detail::isPresent(Val) && "dyn_cast on a non-existent value");
```

这条规则值得单独记：**`dyn_cast` 不是空安全的**。输入可能为空时用 `dyn_cast_if_present`；`dyn_cast_or_null` 只是它的兼容别名。

### 真实边界实验

写一个小工具，把四种情况跑出来。编译命令（`casts.cpp` 依次调用 `dyn_cast_if_present`、`cast` 和 `isa` 路径）：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/clang++ -std=c++17 casts.cpp \
  $($B/llvm-config --cxxflags --ldflags --libs core irreader support --system-libs) \
  -o casts
LD_LIBRARY_PATH=$($B/llvm-config --libdir) ./casts null
```

空指针走 `dyn_cast_if_present`，实测输出：

```text
dyn_cast_if_present<Instruction>(nullptr) = 0x0
```

把同一个空指针喂给 `dyn_cast`，在 assertions 构建里直接停住：

```text
casts: .../llvm/include/llvm/Support/Casting.h:656: ... Assertion
`detail::isPresent(Val) && "dyn_cast on a non-existent value"' failed.
```

`cast` 传错类型（把一个 `Argument` 当 `Instruction`）：

```text
cast<Instruction>(Argument) ...
casts: .../llvm/include/llvm/Support/Casting.h:572: ... Assertion
`isa<To>(Val) && "cast<Ty>() argument of incompatible type!"' failed.
```

`cast` 传空指针时，断言发生在更底层的 `isa_impl_cl`：

```text
casts: .../llvm/include/llvm/Support/Casting.h:109: ... Assertion
`Val && "isa<> used on a null pointer"' failed.
```

这三个 abort 不是「工具坏了」，而是源码在告诉你契约：`cast` 的调用者必须先证明类型；`dyn_cast` 的调用者必须先证明非空。review 里看到 `dyn_cast` 接一个可能为空的返回值，就是一条可以落地的 comment。

## 第三步：CRTP 与 `Instruction.def`

`InstVisitor` 不是虚函数框架，而是 CRTP（ curiously recurring template pattern，奇异递归模板）：

```cpp
// llvm/include/llvm/IR/InstVisitor.h:78
template<typename SubClass, typename RetTy=void>
class InstVisitor {
  RetTy visit(Instruction &I) {
    static_assert(std::is_base_of<InstVisitor, SubClass>::value,
                  "Must pass the derived type to this template!");
    switch (I.getOpcode()) {
    default: llvm_unreachable("Unknown instruction type encountered!");
#define HANDLE_INST(NUM, OPCODE, CLASS) \
    case Instruction::OPCODE: return \
           static_cast<SubClass*>(this)-> \
                      visit##OPCODE(static_cast<CLASS&>(I));
#include "llvm/IR/Instruction.def"
    }
  }
};
```

关键点有三个：

1. `static_cast<SubClass*>(this)` 在编译期把基类指针转回派生类，不需要 vtable。
2. `switch` 的 `case` 不是手写的，而是由 `Instruction.def` 展开。
3. `static_cast<CLASS&>(I)` 之所以安全，是因为 opcode 已经唯一决定了具体类。

`Instruction.def` 把「编号、枚举名、C++ 类」放在同一行：

```cpp
// llvm/include/llvm/IR/Instruction.def:148
HANDLE_BINARY_INST(14, Add  , BinaryOperator)
```

`Instruction.h` 用宏把它展开成枚举：

```cpp
// llvm/include/llvm/IR/Instruction.h:1059
enum BinaryOps {
#define  FIRST_BINARY_INST(N)             BinaryOpsBegin = N,
#define HANDLE_BINARY_INST(N, OPC, CLASS) OPC = N,
#define   LAST_BINARY_INST(N)             BinaryOpsEnd = N+1
#include "llvm/IR/Instruction.def"
};
```

`InstVisitor.h` 用同一份文件展开成 switch。为什么用宏，而不是手写枚举加手写 switch？

- **单一事实来源**：新增一条指令只改 `Instruction.def`，枚举、visitor 分派、可能的其他表一起更新。忘记改一处会编译失败，而不是运行期静默错分派。
- **零虚调用开销**：visitor 的 `visitAdd` 是普通成员函数，分派是一次 switch 加一次 static_cast。
- **代价**：宏展开后的代码不好 grep，IDE 跳转经常跳到 `.def`；读错误信息时要能在脑内展开 `HANDLE_INST`。

LLVM 不在 `Value` 上放虚析构，也是同一个取舍。源码注释写得很清楚：

```cpp
// llvm/include/llvm/IR/Value.h:219
/// Value's destructor should be virtual by design, but that would require
/// that Value and all of its subclasses have a vtable that effectively
/// duplicates the information in the value ID. As a size optimization, the
/// destructor has been protected, and the caller should manually call
/// deleteValue.
```

IR 节点数量巨大，每个节点多一个 vptr 是实打实的内存和缓存代价。LLVM 用 `ValueID` 加 `classof` 换掉了 RTTI，用 CRTP 换掉了 visitor 的虚分派。

## 第四步：visitor 如何分派到 `visitAdd`

`InstCombinerImpl` 同时继承 `InstCombiner` 和 `InstVisitor`：

```cpp
// llvm/lib/Transforms/InstCombine/InstCombineInternal.h:69
class LLVM_LIBRARY_VISIBILITY InstCombinerImpl final
    : public InstCombiner,
      public InstVisitor<InstCombinerImpl, Instruction *> {
```

主循环里每个指令走一次 `visit`：

```cpp
// llvm/lib/Transforms/InstCombine/InstructionCombining.cpp:5933
if (Instruction *Result = visit(*I)) {
```

`visit(Instruction &)` 按 opcode 分派。对 `add`，opcode 是 `Instruction::Add`（`Instruction.def:148` 的编号 14），于是调用：

```cpp
// llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp:1590
Instruction *InstCombinerImpl::visitAdd(BinaryOperator &I) {
  if (Value *V = simplifyAddInst(I.getOperand(0), I.getOperand(1),
                                 I.hasNoSignedWrap(), I.hasNoUnsignedWrap(),
                                 SQ.getWithInstruction(&I)))
    return replaceInstUsesWith(I, V);
```

这段代码解释了为什么读 pass 要先找 `visitXxx`：**opcode 就是分派键**。你不需要在 `InstructionCombining.cpp` 里找 `if (opcode == Add)`；直接 `rg 'visitAdd'` 就能落到具体变换。

`replaceInstUsesWith` 里同时能看到 debug 输出和 RAUW：

```cpp
// llvm/include/llvm/Transforms/InstCombine/InstCombiner.h:433
LLVM_DEBUG(dbgs() << "IC: Replacing " << I << "\n"
                  << "    with " << *V << '\n');
...
I.replaceAllUsesWith(V);
```

后面 Ch 4 会把 `replaceAllUsesWith` 拆开讲；这里先记住：**visitor 找到变换，RAUW 落实变换**。

## 第五步：命名约定

LLVM 的 API 命名很稳定，读到名字就能猜出副作用边界：

| 前缀/后缀 | 含义 | 例子 |
| --- | --- | --- |
| `get*` | 读状态，通常不改变 IR | `User::getOperand`（`User.h:208`）、`Instruction::getOpcode`（`Instruction.h:348`） |
| `set*` | 写状态，可能改变 def-use | `User::setOperand`（`User.h:212`） |
| `is*` | 布尔判断，通常是只读 | `Instruction::isTerminator`（`Instruction.h:351`）、`isBinaryOp`（`Instruction.h:353`） |
| `has*` | 是否存在某个属性/子对象 | `Value::hasName`（`Value.h:263`） |
| `*_begin` / `*_end` | 迭代器区间 | `Value::use_begin`（`Value.h:366`）、`User::op_begin`（`User.h:259`） |
| `classof` | 供 `isa`/`cast` 使用的类型判定 | `UnaryOperator::classof`（`InstrTypes.h:168`） |

`classof` 不是给你直接调的。`isa_impl` 会把它接进统一的 cast 框架：

```cpp
// llvm/include/llvm/Support/Casting.h:63
template <typename To, typename From, typename Enabler = void> struct isa_impl {
  static inline bool doit(const From &Val) { return To::classof(&Val); }
};
```

所以看到一个新类没有 `classof`，先看它是不是通过基类的 `classof` 加 opcode 范围判断；看到 `classof(const Value *V)` 里出现 `isa<Instruction>(V)`，就知道它在处理「`Value` 可能是常量、参数、基本块」的一般情况。

## 第六步：从测试反推实现

测试是 LLVM 里最可靠的行为文档之一，因为它必须能跑。拿 `InstCombine` 的一个真实测试：

```llvm
; llvm/test/Transforms/InstCombine/add.ll:2
; RUN: opt < %s -passes=instcombine -S | FileCheck %s

; llvm/test/Transforms/InstCombine/add.ll:8
; fold (X + C) + (Y & ~C) -> X + (Y | C)
define i32 @add_masked_overwrite_basic(i32 %x, i32 %y) {
; CHECK-LABEL: @add_masked_overwrite_basic(
; CHECK-NEXT:    [[OR:%.*]] = or i32 [[Y:%.*]], 1
; CHECK-NEXT:    [[ADD:%.*]] = add i32 [[X:%.*]], [[OR]]
; CHECK-NEXT:    ret i32 [[ADD]]
;
  %a = add i32 %x, 1
  %b = and i32 %y, -2
  %r = add i32 %a, %b
  ret i32 %r
}
```

先确认测试真的会过：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt < llvm/test/Transforms/InstCombine/add.ll -passes=instcombine -S   | $B/FileCheck llvm/test/Transforms/InstCombine/add.ll
```

实测输出是 `FileCheck PASS`。只看这个函数，`opt` 的输出是：

```llvm
define i32 @add_masked_overwrite_basic(i32 %x, i32 %y) {
  %1 = or i32 %y, 1
  %r = add i32 %x, %1
  ret i32 %r
}
```

测试注释里的 `(X + C) + (Y & ~C)` 就是源码注释。用 `rg` 搜它，落点是：

```text
llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp:1354:
  // (A + C) + (B & ~C) == A + (B | C)
```

再搜调用链：

```text
llvm/lib/Transforms/InstCombine/InstCombineInternal.h:656:
  Instruction *foldAddLikeCommutative(...)
llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp:1327:
  Instruction *InstCombinerImpl::foldAddLikeCommutative(...)
llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp:1628:
  if (Instruction *R = foldAddLikeCommutative(LHS, RHS, ...
```

链条闭合：测试的 `add`/`and` 形状 → `visitAdd`（1590）→ `foldAddLikeCommutative`（1628）→ 注释与匹配器（1354）。

再补一条历史证据：

```bash
git log -S'(A + C) + (B & ~C)' --oneline -- \
  llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp
```

实测：

```text
8a69fb096cb3 [InstCombine] Fold (X + C) + (Y & ~C) to X + (Y | C) (#191334)
```

`git show --stat --oneline 8a69fb096cb3` 显示实现和测试在同一个提交里（`InstCombineAddSub.cpp` 17 行，`add.ll` 366 行）。这条证据能告诉你这个变换当初带了哪些边界测试。

## 固定动作：`rg` → 头文件 → 实现 → 调用点 → 测试 → `git log -S`

把上面的流程固化成六步，每步只问一个问题：

| 步骤 | 命令 | 回答什么 |
| --- | --- | --- |
| 1. `rg` 找符号 | `rg -n 'visitAdd' llvm/lib/Transforms/InstCombine` | 定义在哪个文件 |
| 2. 头文件 | `rg -n 'class InstCombinerImpl|InstVisitor' llvm/lib/Transforms/InstCombine/InstCombineInternal.h` | 继承关系与返回类型 |
| 3. 实现 | `sed -n '1590,1600p' llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp` | 分派后先做什么、后做什么 |
| 4. 调用点 | `rg -n 'visit\(\*I\)|replaceAllUsesWith' llvm/lib/Transforms/InstCombine/InstructionCombining.cpp` | 谁在什么条件下进来 |
| 5. 测试 | `rg -n 'add_masked_overwrite_basic' llvm/test/Transforms/InstCombine/add.ll` | 行为被哪条 CHECK 锁定 |
| 6. 历史 | `git log -S'foldAddLikeCommutative' --oneline -- llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp` | 为什么引入、修过什么 |

实测输出（第 4、6 步）：

```text
5933:    if (Instruction *Result = visit(*I)) {
5947:        I->replaceAllUsesWith(Result);
a6edcea211a3 [InstCombine] Simplify `(add/sub (sub/add) (sub/add))` irrelivant of use-count
```

六步走完，你应该能回答：它什么时候被调用、对什么形状生效、当初为什么加进来。答不出任何一个，说明还差一步。

## 踩雷集锦

1. **把 `dyn_cast` 当空安全用**。当前树上 `dyn_cast` 对空指针断言（`Casting.h:655`）。错误直觉是「`dyn_cast` 失败返回 null，所以空指针也返回 null」；正确做法是输入可能为空时用 `dyn_cast_if_present`，或者先 `if (!V) return;`。
2. **以为 `InstVisitor` 是虚函数**。它在基类里用 `static_cast<SubClass*>` 分派（`InstVisitor.h:124`）。如果你通过 `InstVisitor<Base>*` 调用，得到的是 `Base` 的 `visitAdd`，不是派生类的。CRTP 的代价就是类型必须在编译期已知。
3. **硬编码 opcode 数字**。`Instruction.def:148` 的 `14` 是当前编号，不是稳定 ABI。代码里应该写 `Instruction::Add`，不要写 `14`。
4. **只读 `CHECK` 不读 `RUN`**。同一个函数在不同 pipeline 下可能有不同结果；`RUN` 行决定这条测试证明了哪个 pass、哪个 flag。删掉 `RUN` 只留 `CHECK`，测试就什么也不证明。
5. **把 `classof` 当成通用 RTTI**。`classof` 依赖 `ValueID`/opcode 范围；对不继承 `Value` 的类型、或者被 `deleteValue()` 之后的悬空指针，它没有意义。

## 动手练习

1. 用 `rg` 找到 `Instruction::isTerminator` 的实现，再用 `Instruction.def` 推出哪些 opcode 落在 `TermOpsBegin..TermOpsEnd` 里。用 `opt -passes=verify` 构造一个合法 terminator，确认你的推断。
2. 把 `casts.cpp` 的 `castbad` 分支改成先 `isa<Instruction>` 再 `cast`，跑一遍，解释为什么不再 abort。
3. 在 `llvm/test/Transforms/InstCombine/` 里挑一个带 `CHECK-NEXT` 的测试，按六步动作找到实现，并写出「测试形状 → visit 函数 → fold 函数」的链条。
4. 用 `git log -S'visitAdd'` 找到 `InstCombineAddSub.cpp` 拆分的提交，读它的 commit message，说明拆分前后的文件边界。
5. 给一个你熟悉的 pass 画最小继承图，标出哪些类有 `classof`、哪些靠 opcode 范围判断。

## 本章重点整理

- 先画类层次，再看 cast。`Operator` 是 `User` 的兄弟分支，不是 `Instruction` 的基类。
- `isa` 只判断；`cast` 断言类型；`dyn_cast` 类型失败返回 null、但空指针会断言；空输入用 `dyn_cast_if_present`。
- `InstVisitor` 用 CRTP 加 `Instruction.def` 生成 switch，避免虚调用；opcode 是分派键。
- `Instruction.def` 是编号、枚举名、C++ 类的单一事实来源；宏的代价是可读性和可搜索性。
- 命名约定能提示副作用：`get*`/`is*`/`has*` 通常只读，`set*` 可能改 def-use，`*_begin` 给区间。
- 测试反推的顺序是 `RUN` → `CHECK` → 实现的注释/匹配器 → 调用点 → `git log -S`。

## 自我检核

- [ ] 不看笔记，能画出 `Value`/`User`/`Instruction`/`Operator`/`Constant` 的最小继承图，并指出 `Operator` 的特殊位置
- [ ] 能说出 `isa`、`cast`、`dyn_cast`、`dyn_cast_if_present` 在失败和空指针上的四种行为差异
- [ ] 能解释为什么当前树的 `dyn_cast` 不是空安全的，以及该用哪个替代
- [ ] 能用自己的话解释 CRTP 在这里替代了什么，以及 `static_cast<CLASS&>(I)` 为什么安全
- [ ] 能说出 `Instruction.def` 同时被哪些地方 include，以及新增一条指令要改哪里
- [ ] 能从 `visitAdd` 追到 `foldAddLikeCommutative`，并说明 `replaceInstUsesWith` 在链条里的位置
- [ ] 能解释 `classof` 与 `ValueID`/opcode 的关系，以及为什么它不是通用 RTTI
- [ ] 能独立完成六步动作，并给出每一步的真实命令
- [ ] 能举出一个「只读 CHECK 不读 RUN」会导致误判的例子
- [ ] 能说出 `git log -S` 与 `git log -p` 在定位「某段代码何时引入」上的差别

## 延伸阅读

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`The LLVM Type System`、`Casting`、`Iterating over Instructions` 三节
  - **学什么**：官方版本的类层次与 cast 规则；本章的 `isa`/`dyn_cast` 表格可以和它对照
  - **前提**：读过本章的继承图；否则文档里的类名没有落点
- **[LLVM Language Reference](https://llvm.org/docs/LangRef.html)**
  - **读哪里**：`Instruction Reference` 里 `add`、`and`、`or` 的语义与 flags
  - **学什么**：测试里的 `add`/`and` 形状为什么能这样折叠；`nsw`/`nuw` 改变什么
  - **前提**：知道 SSA 和基本块；否则 `poison`/`undef` 段落会读得很吃力
- **[LLVM: Writing an LLVM New PM Pass](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)**
  - **读哪里**：pass 骨架、`run` 方法、指令遍历示例
  - **学什么**：`InstVisitor` 在真实 pass 里怎么用；本章的 visitor 分派在工程中的位置
  - **前提**：读过 Ch 13 的 New PM 结构，或者至少知道 `FunctionPass` 的 `run`
- **[LLVM source: `llvm/include/llvm/IR/Instruction.def`](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/Instruction.def)**
  - **读哪里**：文件头的宏定义约定，以及 `HANDLE_BINARY_INST` 段
  - **学什么**：宏如何同时服务枚举、visitor、其他生成表；这是本章 CRTP 部分的原始材料
  - **前提**：能读 C 预处理器；否则先把 `#define`/`#include` 的展开顺序弄清楚
- **[LLVM: A Compilation Framework for Lifelong Program Analysis & Transformation](https://llvm.org/pubs/2004-01-30-LLVM-CGO.pdf)**
  - **读哪里**：IR 设计与 SSA 表示部分
  - **学什么**：为什么 LLVM 的 IR 选择这种类型系统与 def-use 表示；本章「为什么不用虚函数」的设计背景
  - **前提**：读过编译原理的 SSA 章节；否则先看 Ch 4 的 def-use 链

→ [Ch 4 — `Value` / `User` / `Use`：def-use 链表与操作数所有权](./04-value-user-use.md)
