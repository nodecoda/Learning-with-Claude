# Ch 3 类型与属性系统

> **目标**：把 MLIR 的类型系统读到能“看一眼就知道这个值能不能喂给这个 op”的程度；同时理解属性为什么不是操作数，以及 `memref` 的 layout 到底在编码什么。

## 为什么需要这个？

类型错误是 MLIR 里最常见、也最容易误判的一类错误。你写的 op 明明“逻辑上”合法，却报：

```text
error: use of value '%i' expects different type than prior uses: 'i32' vs 'index'
```

或者：

```text
error: use of value '%m' expects different type than prior uses: 'tensor<4xf32>' vs 'memref<4xf32>'
```

这两条错误的共同点是：**类型不是“宽度”那么简单**。`index` 不是 `i64`，`tensor` 不是 `memref`，`vector` 也不是“更小的 tensor”。本章把这几个边界讲清楚。

## 先建立直觉：类型是形状，属性是常量

- **类型（Type）**描述一个 SSA value 的“形状 + 语义”：多少位、是不是浮点、有没有维度、在内存还是寄存器、是不是目标相关。
- **属性（Attribute）**描述 op 的编译期参数：常量值、谓词、布局、符号名、注解。

两者都**不可变**，都由 `MLIRContext` 唯一化（uniquing）。这意味着：

```cpp
auto t1 = builder.getI32Type();
auto t2 = builder.getI32Type();
assert(t1 == t2);          // 同一个 storage，指针相等
```

你在 IR 里看到的每个 `i32`，在内存里都是同一个对象。

## 核心概念一：builtin 类型族

| 家族 | 例子 | 语义 |
|---|---|---|
| 整数 | `i1`、`i8`、`i32`、`i64`、`i128` | 无符号性（signless）位向量 |
| 浮点 | `f16`、`bf16`、`f32`、`f64`、`f80`、`f128` | IEEE 浮点 |
| 索引 | `index` | 目标相关整数，用于循环/下标 |
| 向量 | `vector<4xf32>`、`vector<[4]xf32>` | 固定/可扩展 SIMD 值 |
| 张量 | `tensor<4x4xf32>`、`tensor<?x4xf32>` | 值语义的多维数组 |
| 内存 | `memref<4x4xf32>`、`memref<?x?xf32, strided<[?, 1], offset: ?>>` | buffer 语义的多维数组 |
| 复合 | `complex<f32>`、`tuple<i32, f32>` | 复数、元组 |
| 函数 | `(i32, i32) -> i32` | 函数签名 |
| 不透明 | `!llvm.ptr`、`!toy.pointer<f32>` | dialect 自定义类型 |

源码：`BuiltinTypes.h` 里 `TensorType`（55）、`MemRefType`、`VectorType` 等；`Types.h:74` 是所有类型的基类 `class Type`。

### `i32` 没有符号性

MLIR 的整数类型是 **signless** 的。`i32` 既不是 `si32` 也不是 `ui32`；符号由具体 op 决定（例如 `arith.divsi` vs `arith.divui`）。这是和很多 IR 不一样的地方：

```mlir
%0 = arith.divsi %a, %b : i32   // 有符号除法
%1 = arith.divui %a, %b : i32   // 无符号除法
```

同一个 `i32`，两条 op 给出两种解释。类型本身不携带符号。

## 核心概念二：tensor、memref、vector 的分工

这三个最容易混。一句话区分：

- **`tensor` 是值**：不可变、没有地址、没有 layout，适合表达“计算结果”；
- **`memref` 是缓冲**：有地址、有 layout、有 memory space，适合表达“内存里的数组”；
- **`vector` 是寄存器内的 SIMD 值**：一维/多维固定形状，直接对应目标向量寄存器。

看一个对照：

```mlir
func.func @shapes(%t: tensor<4x4xf32>, %m: memref<4x4xf32>, %v: vector<4xf32>) {
  return
}
```

- `%t` 不能取地址；你只能 `tensor.extract` 出元素，或整个 `tensor` 传来传去。
- `%m` 可以 `memref.load/store`，也可以取 `memref.subview`。
- `%v` 通常直接进 `vector.addf` 之类的 op，最终落到 SIMD 指令。

这就是为什么 `linalg` 主要吃 `tensor`/`memref`，而 `vector` 是 lowering 的目标之一：**抽象层次不同，不是大小不同**。

## 核心概念三：`memref` 的 layout 在编码什么

`memref<4x4xf32>` 是“identity layout”的简写：行主序、紧密排列、offset 为 0。

完整的 layout 可以写成：

```mlir
memref<4x4xf32, strided<[4, 1], offset: 0>>
```

意思是：第 0 维步长 4，第 1 维步长 1，起始偏移 0。因为这就是 identity，打印器会把它规范化回 `memref<4x4xf32>`。实测：

```text
func.func @types(... %arg2: memref<4x4xf32, strided<[4, 1]>> ...)
```

注意 `offset: 0` 被省略了，但 `strided<[4, 1]>` 仍然打印出来——因为它不是“默认省略”，而是规范化后的等价形式。

### affine map layout

更一般的 layout 用 `affine_map` 表达：

```mlir
func.func @am(%m: memref<4x4xf32, affine_map<(d0, d1) -> (d0 * 4 + d1)>>) {
  return
}
```

实测输出会把 map 提成别名：

```mlir
#map = affine_map<(d0, d1) -> (d0 * 4 + d1)>
module {
  func.func @am(%arg0: memref<4x4xf32, #map>) {
    return
  }
}
```

这说明了三件事：

1. `affine_map` 是**一等属性**，不是字符串；
2. 打印器会给重复出现的属性起别名（`#map`），方便阅读；
3. `memref` 的 layout 可以是任意 affine map，不限于 strided。

### 动态维度与动态 offset

```mlir
memref<?x?xf32, strided<[?, 1], offset: ?>>
```

实测原样保留。`?` 表示运行期才知道：维度大小、步长、offset 都可以动态。这就是 `memref` 能表达“非连续视图”的原因。

源码：`BuiltinAttributes.td:1026` 的 `StridedLayoutAttr`。

## 核心概念四：`index` 不是 `i64`

`index` 是**目标相关**的整数类型，专门用于循环变量、下标、形状计算。它的宽度由 target 决定，默认按 64 位降级。

证据在 Ch 1：同一段 `sum`，`index` 在 `llvm` dialect 里变成 `i64`；给 conversion pass 加 `index-bitwidth=32`，才变成 `i32`。

为什么需要 `index`？因为它让高层 IR 不必提前决定“指针宽度”。同一份 `scf.for` 可以降到 32 位或 64 位目标，而不需要改 IR。

代价是：**`index` 和 `i32` 不能混用**。实测：

```mlir
func.func @bad(%i: index) -> i32 {
  %c = arith.constant 1 : i32
  %0 = arith.addi %i, %c : i32
  return %0 : i32
}
```

```text
error: use of value '%i' expects different type than prior uses: 'i32' vs 'index'
```

要混用必须显式转换：`arith.index_cast`。

## 核心概念五：属性的种类

| 属性 | 例子 | 用途 |
|---|---|---|
| `IntegerAttr` | `42 : i32` | 整数常量 |
| `FloatAttr` | `1.0 : f32` | 浮点常量 |
| `StringAttr` | `"bar"` | 字符串 |
| `ArrayAttr` | `[1, 2, 3]` | 异构数组 |
| `DenseElementsAttr` | `dense<[1.0, 2.0]>` | 稠密张量常量 |
| `AffineMapAttr` | `affine_map<(d0) -> (d0 + 1)>` | 仿射映射 |
| `StridedLayoutAttr` | `strided<[4, 1], offset: 0>` | memref 布局 |
| `TypeAttr` | `i32` 作为属性 | 类型参数 |
| `UnitAttr` | `unit` | 布尔标记 |
| `DictionaryAttr` | `{foo = "bar"}` | 属性字典 |

属性是**类型化**的：每个属性知道自己是什么。`42` 不是“数字字符串”，而是 `IntegerAttr`，带类型 `i32`。

### dense 属性的打印细节

```mlir
%0 = arith.constant dense<[1.0, 2.0]> : tensor<2xf32>
```

实测打印：

```mlir
%cst = arith.constant dense<[1.000000e+00, 2.000000e+00]> : tensor<2xf32>
```

浮点被格式化成了科学计数法。这不是语义变化，只是打印器的选择。写测试时不要依赖这个格式；要锁值，用 `dense<...>` 的语义比较，或者用 FileCheck 的宽松匹配。

## 底层机制：uniquing 与 storage

类型和属性都不是“每次 new 一个”，而是**按内容唯一化**：

```text
MLIRContext
├── TypeUniquer        (MLIRContext.h:231)
│   └── 按 (kind, params) 查表
└── AttributeUniquer   (MLIRContext.h:235)
    └── 按 (kind, params) 查表
```

源码：`StorageUniquer.h:82` 定义 `class StorageUniquer`。

这带来三个后果：

1. **比较类型/属性可以用指针相等**，不需要结构比较。
2. **它们与 context 同生命周期**，不能跨 context 传递。
3. **`get` 与 `getChecked` 不同**：`get` 假设参数合法，`getChecked` 会验证并返回 `FailureOr` 或 `nullptr`。

这也是为什么写 dialect 类型时，构造函数通常是 `get(context, params...)`，而解析用户输入时要用 `getChecked`（Ch 14）。

## 示例一：类型打印会规范化

输入：

```mlir
func.func @types(%i: index, %t: tensor<4x4xf32>, %m: memref<4x4xf32, strided<[4, 1], offset: 0>>, %v: vector<4xf32>, %p: !llvm.ptr) {
  return
}
```

实测输出：

```mlir
module {
  func.func @types(%arg0: index, %arg1: tensor<4x4xf32>, %arg2: memref<4x4xf32, strided<[4, 1]>>, %arg3: vector<4xf32>, %arg4: !llvm.ptr) {
    return
  }
}
```

两处变化：

- `strided<[4, 1], offset: 0>` → `strided<[4, 1]>`（省略默认 offset）；
- `%i/%t/%m/%v/%p` → `%arg0...%arg4`（SSA 名不稳定）。

**不要拿“文本完全一致”当验证标准**，除非你明确控制了打印选项。

## 示例二：tensor 与 memref 不能混

```mlir
func.func @bad2(%t: tensor<4xf32>, %m: memref<4xf32>) {
  %0 = arith.addf %t, %m : tensor<4xf32>
  return
}
```

实测：

```text
error: use of value '%m' expects different type than prior uses: 'tensor<4xf32>' vs 'memref<4xf32>'
```

`arith.addf` 要求两个操作数同类型。`tensor` 与 `memref` 在类型系统里是两回事，即使形状相同。这也解释了为什么 `linalg` 的 bufferization 是一个独立阶段：**从值语义切到 buffer 语义，必须显式转换**（Ch 20）。

## 示例三：动态维度与可扩展向量

```mlir
func.func @more(%d: memref<?x?xf32, strided<[?, 1], offset: ?>>, %s: vector<[4]xf32>, %c: complex<f32>, %t: tuple<i32, f32>) {
  return
}
```

实测输出原样保留：

```mlir
%arg0: memref<?x?xf32, strided<[?, 1], offset: ?>>
%arg1: vector<[4]xf32>
%arg2: complex<f32>
%arg3: tuple<i32, f32>
```

`vector<[4]xf32>` 是**可扩展向量**（scalable vector），`[4]` 表示 `4 × vscale` 个元素。它和 `vector<4xf32>` 是不同类型，不能互换。SVE/RVV 相关 lowering 会用到它。

## 示例四：属性的类型性

`42` 作为属性时不是裸数字：

```mlir
%c = arith.constant 42 : i32
```

generic form 里是：

```mlir
%0 = "arith.constant"() <{value = 42 : i32}> : () -> i32
```

`value` 是 `IntegerAttr`，类型是 `i32`。如果你把它换成 `42 : i64` 而结果类型还是 `i32`，verifier 会报错。属性不是“注释”，它参与验证。

## 对比：MLIR 类型 vs LLVM 类型

| 维度 | LLVM IR | MLIR |
|---|---|---|
| 整数 | `i32`（同样 signless） | `i32`（signless） |
| 指针 | `ptr` / `i64` | `!llvm.ptr`、`memref`、dialect 类型 |
| 数组 | `[4 x float]` | `tensor<4xf32>`、`memref<4xf32>` |
| 向量 | `<4 x float>` | `vector<4xf32>`、`vector<[4]xf32>` |
| 索引 | 无（用 i32/i64） | `index`（目标相关） |
| 结构体 | `{i32, float}` | `tuple<i32, f32>` 或 dialect 类型 |
| 布局 | 由 datalayout 决定 | `memref` 的 layout 属性显式编码 |
| 扩展 | 固定 | dialect 可加类型/属性 |

关键差异：**MLIR 把布局与语义写进类型**，LLVM IR 更多依赖 target 的 datalayout 与约定。

## 踩雷集锦

1. **以为 `index` 就是 `i64`。** 它们是不同类型，不能直接混用；`index` 的宽度由 target 决定。

2. **以为 `tensor` 和 `memref` 只差“有没有地址”。** 它们属于不同抽象层：值语义 vs buffer 语义，lowering 路径也不同。

3. **以为 `vector<4xf32>` 和 `vector<[4]xf32>` 可以互换。** 后者是可扩展向量，元素数是 4×vscale。

4. **用文本相等做测试。** 打印器会规范化 layout、重命名 SSA、格式化浮点。测试要锁语义，不是锁字符串。

5. **把属性当操作数。** `arith.constant 42` 的 42 不占 SSA 值；它是 `IntegerAttr`。

6. **跨 context 传类型/属性。** 它们由 `MLIRContext` 唯一化并拥有，跨 context 使用是未定义行为。

7. **用 `get` 解析不可信输入。** 用户输入应该走 `getChecked`；`get` 假设参数已经合法，失败时可能直接崩溃。

## 进阶：再往深一层

- **Dialect 类型**：`!llvm.ptr`、`!toy.pointer<f32>` 由 dialect 定义 storage 与 parser/printer。Ch 14 会写一个。
- **Type interfaces**：`ShapedType`、`MemRefElementTypeInterface` 等让泛型代码不依赖具体类型。Ch 8 会讲。
- **DenseElementsAttr 的 splat**：`dense<0.0>` 是 splat 形式，内部只存一个值。大张量常量要理解这个优化。
- **Affine map 的简化**：`affine_map<(d0) -> (d0 * 1 + 0)>` 会被简化为 `(d0) -> (d0)`；这是 `AffineMap::get` 的规范化，不是打印器行为。

## 动手练习

1. **类型打印**：把本章 `types.mlir` 的 `strided<[4, 1], offset: 0>` 改成 `offset: 8`，观察打印结果是否还省略。
2. **layout 等价**：写两个语义等价的 `memref` 类型（一个 strided、一个 affine map），确认它们**不**是同一个类型。
3. **index 转换**：把 `index` 与 `i32` 混用的例子改成用 `arith.index_cast`，跑通。
4. **dense 格式**：把 `dense<[1.0, 2.0]>` 改成 `dense<0.0>`（splat），观察打印与类型。
5. **可扩展向量**：把 `vector<[4]xf32>` 改成 `vector<4xf32>`，确认 `mlir-opt` 接受，但两者不是同一类型。
6. **属性验证**：把 `arith.constant 42 : i32` 的结果类型改成 `i64`，记录 verifier 报错。

## 本章重点整理

- 类型描述值的形状与语义；属性描述 op 的编译期参数；两者都不可变且唯一化。
- `i32` 是 signless；符号由 op 决定。
- `tensor`（值）、`memref`（buffer）、`vector`（SIMD）是三个不同抽象层。
- `memref` 的 layout 可以是 strided 或 affine map；打印会规范化。
- `index` 是目标相关类型，默认降为 `i64`。
- 属性是类型化的；`getChecked` 用于不可信输入。
- 类型/属性由 `MLIRContext` 拥有，不能跨 context。

## 自我检核

- [ ] 我能说出 `tensor`、`memref`、`vector` 各自的语义与典型 op
- [ ] 我能解释 `memref<4x4xf32>` 与 `memref<4x4xf32, strided<[4, 1]>>` 的关系
- [ ] 我知道 `index` 为什么不是 `i64`，以及它何时变成 `i64`
- [ ] 我能解释 `i32` 为什么没有符号性
- [ ] 我能说出至少 6 种属性及其用途
- [ ] 我知道 `get` 与 `getChecked` 的区别
- [ ] 我能解释为什么类型/属性可以指针比较
- [ ] 我知道打印器会做哪些规范化，因此测试不能锁文本

## 延伸阅读

- **[MLIR: Builtin Types](https://mlir.llvm.org/docs/Dialects/Builtin/#types)**
  - **读哪里**：整数、浮点、vector、tensor、memref 的定义。
  - **学什么**：每个类型的语法与语义约束。
  - **和本章的关联**：类型族表格的官方依据。

- **[MLIR: Builtin Attributes](https://mlir.llvm.org/docs/Dialects/Builtin/#attributes)**
  - **读哪里**：`AffineMapAttr`、`DenseElementsAttr`、`StridedLayoutAttr`。
  - **学什么**：属性的语法与用途。
  - **和本章的关联**：属性一节的官方依据。

- **[mlir/include/mlir/IR/BuiltinTypes.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/BuiltinTypes.h)**
  - **读哪里**：`TensorType`（55）、`MemRefType`、`VectorType` 的接口。
  - **学什么**：形状、layout、元素类型的访问方式。
  - **和本章的关联**：类型 API 的源码依据。

- **[mlir/include/mlir/IR/BuiltinAttributes.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/BuiltinAttributes.td)**
  - **读哪里**：`StridedLayoutAttr`（1026）等定义。
  - **学什么**：属性参数与验证规则。
  - **和本章的关联**：`memref` layout 的源码依据。

- **[MLIR: Affine Dialect](https://mlir.llvm.org/docs/Dialects/Affine/)**
  - **读哪里**：affine map 的语法与语义。
  - **学什么**：`d0`、`d1` 的含义与限制。
  - **和本章的关联**：Ch 24 会展开循环与依赖分析。

---

下一章把类型和 op 放回“层”的视角：47 个 dialect 各自负责什么，为什么同一个加法有三种写法，以及一条真实的混合 dialect pipeline 如何在层之间移动。

→ [Ch 4 方言地图：谁负责什么](./04-dialect-map.md)