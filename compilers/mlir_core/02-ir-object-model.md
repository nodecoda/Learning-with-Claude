# Ch 2 IR 对象模型：Operation、Block、Region、Value

> **目标**：把「Operation 是唯一单元」这句话拆到能画图、能数、能在源码里指认的程度。学完本章，你应当能从任何一段 generic form 直接读出：有几个 region、几个 block、哪些是 block argument、哪些是 op result、属性分在哪一组。

## 为什么需要这个？

读 MLIR 时最常见的三个误判，全都来自对象模型没建立起来：

1. **把 region 当函数体。** 于是看不懂 `scf.if` 的两个 region，也看不懂 `linalg.add` 为什么自带一个 region。
2. **把 block argument 当变量声明。** 于是把函数参数 `%arg0` 和 `arith.constant` 的结果 `%c0` 当成同一类东西。
3. **把属性当操作数。** 于是不理解为什么 `arith.constant 42` 的 `42` 不占一个 SSA 值。

这三个误判会让后面的 pass、pattern、conversion 全部读得似是而非。本章一次解决。

## 先建立直觉：三层容器 + 一张 def-use 图

MLIR 的 IR 在结构上是**树**，在数据流上是**图**：

```text
Module (Operation)
└── Region
    └── Block
        ├── BlockArgument(%arg0: i32)
        ├── Operation: arith.constant
        │   └── OpResult(%c0: i32)
        └── Operation: func.return
            └── operand: 指向 %c0
```

- **树**：Operation 包含 Region，Region 包含 Block，Block 包含 Operation。递归下去。
- **图**：每个 operand 是指向某个 Value 的 use；每个 Value 记录自己的 use-list。

文本里的缩进和 `^bb` 标签只是这棵树的打印形式。真正决定语义的是对象之间的引用关系。

## 核心概念一：Operation

### 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| 名字 | `OperationName` | `dialect.op`，运行期按字符串查表 |
| 操作数 | `OpOperand` 列表 | 每个 operand 是一个 use |
| 结果 | `OpResult` 列表 | 每个 result 是一个 Value |
| 属性 | `DictionaryAttr`（discardable） | 可附加的键值对 |
| Properties | 固定布局的 C++ 对象 | op 的固有字段，可转成 Attribute |
| Region | `Region` 列表 | 嵌套 IR |
| Successor | `BlockOperand` 列表 | CFG 后继 |
| Location | `Location` | 源码位置/调试信息 |

### generic 语法

通用形式把所有结构显式写出来：

```text
"dialect.op"(%operand0, %operand1) <{inherent attrs / properties}>
              ({region0}, {region1})
              {discardable attrs}
              : (operand types) -> (result types)
```

用 Ch 0 的 `arith.addi` 验证：

```mlir
%0 = "arith.addi"(%arg0, %arg1) <{overflowFlags = #arith.overflow<none>}> {foo = "bar"} : (i32, i32) -> i32
```

这里同时出现了两组属性：

- `<{overflowFlags = ...}>`：**inherent**，op 定义里声明的字段，属于 op 的语义；
- `{foo = "bar"}`：**discardable**，任意附加的注解，框架可以忽略。

源码依据：`Operation.h:468` 的 `getInherentAttr`、`Operation.h:526` 的 `getDiscardableAttrDictionary`。23.x 把固有字段迁到 Properties 后，generic 打印仍把 inherent 组放在 `<{...}>` 里。

> 注意：不是所有 inherent 信息都在 `<{...}>`。`func.func` 的 `sym_name`、`function_type` 是 Properties，打印在 `<{...}>`；`arith.constant` 的 `value` 是 ODS 声明的 attribute argument，也打印在 `<{...}>`。判断依据是“是否属于 op 定义”，不是“存储位置”。

## 核心概念二：Block 与 BlockArgument

Block 是**有序的 Operation 列表 + 参数列表 + 终止符**。

```mlir
^bb1(%0: index, %1: i32):  // 2 preds: ^bb0, ^bb2
  %2 = arith.cmpi slt, %0, %arg0 : index
  cf.cond_br %2, ^bb2, ^bb3
```

- `^bb1` 是标签；
- `(%0: index, %1: i32)` 是 **block argument**，由前驱通过分支传入；
- 最后一条 `cf.cond_br` 是 **terminator**（终止符）。

源码依据：

- `Block.h:32`：`class alignas(8) Block : public IRObjectWithUseList<BlockOperand>, ...`
- `Block.h:111`：`BlockArgListType getArguments()`
- `Block.h:248`：`Operation *getTerminator()`

### 函数参数就是 entry block 的参数

```mlir
func.func @add(%a: i32, %b: i32) -> i32
```

在对象模型里，`%a`/`%b` 是函数体这个 region 的 **entry block** 的两个 `BlockArgument`。`func.func` 只是把这些 argument 的类型记录在 `function_type` 属性里。这也解释了为什么打印时会从 `%a` 变成 `%arg0`：名字不是语义。

### BlockArgument vs OpResult

两者都是 `Value` 的子类，但来源不同：

```cpp
// 源码结构（简化）
class Value { ... };
class BlockArgument : public Value { ... };  // Value.h:306
class OpResult     : public Value { ... };  // Value.h:454
```

- `BlockArgument` 由 block 的参数列表产生，def 点是 block 入口；
- `OpResult` 由某条 op 产生，def 点是那条 op。

在文本里两者都写成 `%n`，所以只能靠上下文区分：出现在 `^bb(...)` 里的是前者，出现在 `%n = op` 左边的是后者。

## 核心概念三：Region

Region 是**Block 的列表**。一个 region 可以只有一个 block（structured op 常见），也可以有多个 block（CFG 常见）。

源码：`Region.h:26` 定义 `class Region`，`Region.h:45` 的 `getBlocks()` 返回 `BlockListType`。

### 例：`scf.if` 有两个 region

```mlir
func.func @pick(%c: i1, %a: i32, %b: i32) -> i32 {
  %r = scf.if %c -> (i32) {
    scf.yield %a : i32
  } else {
    scf.yield %b : i32
  }
  return %r : i32
}
```

generic form（实测）：

```mlir
"builtin.module"() ({
  "func.func"() <{function_type = (i1, i32, i32) -> i32, sym_name = "pick"}> ({
  ^bb0(%arg0: i1, %arg1: i32, %arg2: i32):
    %0 = "scf.if"(%arg0) ({
      "scf.yield"(%arg1) : (i32) -> ()
    }, {
      "scf.yield"(%arg2) : (i32) -> ()
    }) : (i1) -> i32
    "func.return"(%0) : (i32) -> ()
  }) : () -> ()
}) : () -> ()
```

数一下：

| 层级 | 数量 | 说明 |
|---|---|---|
| Operation | 6 | module、func.func、scf.if、2×scf.yield、func.return |
| Region | 3 | func 体、then、else |
| Block | 3 | 每个 region 各一个 |
| BlockArgument | 3 | `%arg0`、`%arg1`、`%arg2` |
| OpResult | 1 | `scf.if` 的 `%0` |

`scf.if` 的两个 region 是**并列**的，不是嵌套的。文本里的 `{ ... } else { ... }` 只是 custom 语法对两个 region 的排版。

### 例：`linalg.add` 的 region

Ch 1 已经看过：`linalg.add` 自带一个 region，里面是 `arith.addf` 和 `linalg.yield`。这说明 region 不属于“控制流结构”，它只是“op 内部的 IR”。任何 op 都可以带 region，包括算术 op。

## 核心概念四：Value、use 与作用域

### def-use

每个 `Value` 有一张 use-list，记录所有引用它的 `OpOperand`。这带来两个直接后果：

- 改一个 value 的类型/定义，可以 O(1) 找到所有使用者；
- 删除一个还有 use 的 op 是非法的，必须先替换或清除 use。

源码：`Value.h:96` 的 `class Value` 继承自带 use-list 的基类；`Operation.h` 的 operand 是 `OpOperand`。

### SSA 作用域不是词法作用域

MLIR 的 SSA 规则是**支配（dominance）**，不是“同一个 `{}` 内可见”。两个典型错误：

**错误一：跨 region 引用**

```mlir
func.func @s(%c: i1) -> i32 {
  %r = scf.if %c -> (i32) {
    %a = arith.constant 1 : i32
    scf.yield %a : i32
  } else {
    scf.yield %a : i32
  }
  return %r : i32
}
```

实测报错：

```text
error: use of undeclared SSA value name
    scf.yield %a : i32
              ^
```

`%a` 定义在 then region，else region 里根本看不到这个名字。parser 的作用域是**按 region 隔离**的。

**错误二：同 region 内先用后定义**

```mlir
func.func @bad() -> i32 {
  cf.br ^bb1
^bb1:
  %0 = arith.addi %1, %1 : i32
  return %0 : i32
^bb2:
  %1 = arith.constant 1 : i32
  cf.br ^bb1
}
```

实测报错：

```text
error: operand #0 does not dominate this use
note: operand defined here (op in the same region)
```

`%1` 确实在同一个 region 里，但它定义在 `^bb2`，不支配 `^bb1` 中的使用。**同 region 不等于可见**，还要看 CFG 支配关系。

### 支配规则速记

- 同一个 block 内：定义必须在 use 之前；
- 跨 block：def 所在 block 必须支配 use 所在 block；
- 跨 region：只能引用父 region 中支配该 op 的值，或通过 region 的 `yield`/block argument 显式传出。

## 底层机制：这些对象在内存里怎么放

Operation 的注释（`Operation.h:60-80`）说得很清楚：

- operand 数组**初始与 Operation 一起 tail allocate**，需要时再移到堆上；
- Region 也存在 tail-allocated 数组里；
- Properties 是一个**固定大小的 C++ 对象**，由 Operation 拥有，可转成 Attribute；
- 每个 Block 是 `ilist_node`，挂在 Region 的 block list 上。

这解释了三个现象：

1. **创建 op 必须通过 `Operation::create` / `OpBuilder`**，不能 `new` 一个具体 op 类。op 类只是对 `Operation*` 的轻量包装。
2. **region 的增删会改变 op 的布局**，所以不能在持有迭代器时随意改 region。
3. **属性与 Properties 是两套存储**，但都能通过 `getAttr` 风格的接口读到。

## 示例一：从文本反推对象树

以 Ch 1 的 `sum` 降级前的 `scf` 版本为例：

```mlir
func.func @sum(%n: index) -> i32 {
  %c0 = arith.constant 0 : i32
  ...
  %r = scf.for %i = %zero to %n step %one iter_args(%acc = %c0) -> (i32) {
    %next = arith.addi %acc, %c1 : i32
    scf.yield %next : i32
  }
  return %r : i32
}
```

对应的树：

```text
module
└── func.func @sum
    └── region (1 block)
        ├── %c0..%one (4 个 OpResult)
        ├── scf.for
        │   └── region (1 block)
        │       ├── BlockArgument %i, %acc
        │       ├── arith.addi -> %next
        │       └── scf.yield %next
        └── func.return %r
```

注意 `scf.for` 的 region 有两个 block argument：`%i`（induction variable）和 `%acc`（iter_arg）。它们不是 op result，而是 block argument；循环的“下一轮值”由 `scf.yield` 传回。

## 示例二：属性分组

```mlir
func.func @attrs(%a: i32, %b: i32) -> i32 {
  %0 = arith.addi %a, %b {foo = "bar"} : i32
  return %0 : i32
}
```

custom 形式下，`{foo = "bar"}` 写在类型前面。generic 形式（实测）：

```mlir
%0 = "arith.addi"(%arg0, %arg1) <{overflowFlags = #arith.overflow<none>}> {foo = "bar"} : (i32, i32) -> i32
```

如果你把 `{foo = "bar"}` 写到类型后面：

```mlir
%c = arith.constant 42 : i32 {foo = "bar"}
```

实测报错：

```text
error: expected operation name in quotes
```

parser 认为你已经结束了这条 op，接下来应该是一条新 op（generic 形式必须以引号开头）。**属性字典的位置是语法的一部分**，不是随便放的。

## 示例三：block argument 的显式传递

`scf` 层用 `iter_args` 隐藏了“循环携带变量”；`cf` 层必须显式传：

```mlir
cf.br ^bb1(%c0, %c0_i32 : index, i32)
...
^bb1(%0: index, %1: i32):
```

`^bb1` 的两个参数 `%0`、`%1` 分别接收前驱传来的 `index` 和 `i32`。这就是 block argument 的语义：**它是函数参数在任意 block 上的推广**。

LLVM IR 没有这个机制，只能用 phi 在块首合成。Ch 1 已经看过对照输出。

## 对比：MLIR / LLVM IR / AST 的对象模型

| 概念 | AST | LLVM IR | MLIR |
|---|---|---|---|
| 容器 | 节点子节点 | 指令链表 | Operation 树 + Region |
| 参数 | 函数参数 | 函数参数 | BlockArgument |
| 定义 | 变量声明 | 指令结果 | OpResult |
| 合并 | 无 | phi | BlockArgument |
| 类型 | 语言类型 | 第一类类型 | 第一类类型 + dialect 类型 |
| 附加信息 | 注解 | metadata | 属性 + Properties |
| 遍历 | visitor | iterator | walk / region 递归 |

一句话：MLIR 把“控制流容器”和“数据流定义”都统一成可嵌套的对象，所以它能同时表达高层 structured op 和低层 CFG。

## 踩雷集锦

1. **把 Region 当 Block。** Region 是 block 的列表；Block 是 op 的列表。`scf.if` 有两个 region，不是两个 block。

2. **把 BlockArgument 当 OpResult。** `%arg0` 和 `%c0` 在文本里长得一样，但前者由 block 入口定义，后者由 op 定义。改 `%arg0` 的类型要改 block 参数列表，不是改某条 op。

3. **以为同一个 region 内就能随便引用。** 还要满足支配关系；`does not dominate this use` 是真实会遇到的错误。

4. **以为跨 region 能直接引用。** parser 直接报 `use of undeclared SSA value name`；要传值必须用 `yield` / block argument / op result。

5. **把属性写在类型后面。** 语法位置错了会得到 `expected operation name in quotes`，而不是“属性不合法”。

6. **把 properties 与 discardable attributes 混为一谈。** generic form 里 `<{...}>` 与 `{...}` 是两组；前者属于 op 定义，后者是可丢弃注解。

7. **以为 op 类是 IR 节点。** `arith::AddIOp` 只是 `Operation*` 的包装；真正被分配的是 `Operation`。所以不能 `new AddIOp`，也不能靠 `sizeof` 推布局。

## 进阶：再往深一层

- **Properties 的动机**：固定布局让 verifier 和 bytecode 更快，也避免属性字典的字符串查找。Ch 13 会看 ODS 如何生成 properties 结构。
- **Use-list 的复杂度**：`replaceAllUsesWith` 是 O(uses)，不是 O(IR)；大 IR 上频繁替换会变慢。Ch 9 会讲批量修改的策略。
- **Region 的 single-block 特化**：很多 op 保证只有一个 block（`SingleBlock` trait），这让 canonicalize 可以安全地假设没有 CFG。
- **Bytecode**：Properties 的固定布局是 `.mlirbc` 能快速反序列化的原因之一。Ch 16 会实测。

## 动手练习

1. **数对象**：把本章的 `scf.if` 例子抄下来，用 `--mlir-print-op-generic` 打印，数出 Operation / Region / Block / BlockArgument / OpResult 各多少个。
2. **画树**：给 Ch 1 的 `linalg.add` 例子画对象树，标出每个 region 的 block 数与 terminator。
3. **制造支配错误**：写一个 `%x` 定义在后继 block、使用在前驱 block 的函数，记录完整报错。
4. **制造跨 region 错误**：把 `scf.if` 的 then 里的值拿到 else 用，记录报错，并改成通过 `scf.if` 的 result 传出。
5. **属性位置**：把 `{foo = "bar"}` 在 custom 语法里前后移动，找出唯一合法的位置，并解释为什么。
6. **generic 手写**：不看教材，用 generic 形式写出一个带两个 region 的 `scf.if`，再用 `mlir-opt` 验证。

## 本章重点整理

- IR 在结构上是 Operation → Region → Block → Operation 的树；在数据流上是 Value → use 的图。
- Operation 的字段：名字、operands、results、attributes、properties、regions、successors、location。
- Block = op 列表 + block arguments + terminator；函数参数就是 entry block 的参数。
- BlockArgument 与 OpResult 都是 Value，但定义点不同。
- SSA 可见性 = region 隔离 + 支配关系；跨 region 必须显式传值。
- generic form 里 `<{...}>` 是 inherent，`{...}` 是 discardable。
- op 类是包装，底层对象是 `Operation`；region/operand 数组 tail-allocated。

## 自我检核

- [ ] 我能画出 `scf.if` 例子的完整对象树，并数对每类对象的数量
- [ ] 我能解释 block argument 与 op result 的区别，并各举一例
- [ ] 我能说出 region 与 block 的区别，以及为什么 `scf.if` 有两个 region
- [ ] 我能解释 `does not dominate this use` 与 `use of undeclared SSA value name` 的差别
- [ ] 我知道函数参数在对象模型里是什么
- [ ] 我能读出 generic form 里两组属性分别是什么
- [ ] 我知道为什么不能 `new` 一个具体 op 类
- [ ] 我能说出 tail allocation 对“持有迭代器时修改 region”的影响

## 延伸阅读

- **[MLIR: Understanding the IR Structure](https://mlir.llvm.org/docs/Tutorials/UnderstandingTheIRStructure/)**
  - **读哪里**：Operation/Block/Region 的关系与遍历示例。
  - **学什么**：官方给出的对象模型与 `walk` 用法。
  - **和本章的关联**：本章的树形图是它的展开版。

- **[mlir/include/mlir/IR/Operation.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Operation.h)**
  - **读哪里**：第 60–100 行的类注释、`getRegions()`（699–705）、`getOperands()`（403）、`getResults()`（440）。
  - **学什么**：字段的真实类型与内存布局说明。
  - **和本章的关联**：底层机制一节的依据。

- **[mlir/include/mlir/IR/Block.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Block.h)**
  - **读哪里**：`class Block`（32）、`getArguments()`（111）、`getTerminator()`（248）。
  - **学什么**：block 的参数与终止符接口。
  - **和本章的关联**：Block 一节的依据。

- **[mlir/include/mlir/IR/Value.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Value.h)**
  - **读哪里**：`class Value`（96）、`class BlockArgument`（306）、`class OpResult`（454）。
  - **学什么**：两种 Value 的继承关系与接口差异。
  - **和本章的关联**：BlockArgument vs OpResult 一节的依据。

- **[MLIR: MLIR Language Reference](https://mlir.llvm.org/docs/LangRef/)**
  - **读哪里**：SSA value、block、region、属性的语法定义。
  - **学什么**：文本语法与对象模型的对应关系。
  - **和本章的关联**：generic form 的权威语法说明。

---

下一章进入类型与属性系统：`index` 到底怎么表示、`memref` 的 layout 如何编码、`affine_map` 为什么是一等公民、属性字典与类型如何 uniquing。

→ [Ch 3 类型与属性系统](./03-types-and-attributes.md)