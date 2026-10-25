# Ch 24 Affine：循环与依赖

> **目标**：理解 affine 方言为什么值得单独存在。你能读懂 `affine.for/if`、affine map/set，能解释循环不变量与依赖分析在做什么，能按正确顺序把一段 affine 程序逐级降到 `cf`，并说清为什么顺序反了会留下 `scf.for`。

## 为什么需要这个？

`scf.for` 已经能表达循环。为什么还要 `affine.for`？

因为 `scf.for` 的边界可以是任意 SSA 值，循环体里可以做任意运算。对编译器来说，这是自由，也是负担：**它没法证明两个下标是否相等、两次迭代是否访问同一块内存、循环能不能交换顺序**。

Affine 用一组限制换取可判定性：

- 循环边界和内存下标必须是**仿射表达式**：常量、循环变量、符号的线性组合；
- 不允许任意函数调用、dim×dim 乘法、任意除法；
- 每个值要么是 dimension（循环维），要么是 symbol（循环外不变量）。

限制之后，依赖分析可以变成整数线性规划：两次访问是否可能重叠、距离是多少、哪个方向。这就是 polyhedral（多面体）模型在 MLIR 里的落点。

一句话：**`scf` 给你表达力，`affine` 给编译器证明力**。需要自动并行化、融合、tiling 时，后者更值钱。

## 先建立直觉：affine 只允许“看得懂”的下标

看两段访问：

```mlir
%a = affine.load %A[%i + 1] : memref<8xf32>
%b = affine.load %A[%i * %j] : memref<8xf32>
```

第一行是 affine 的：`i + 1` 是循环变量的线性表达式。第二行不是：`i * j` 是两个维度的乘积，超出仿射允许的范围（affine 只允许符号乘以常量，不允许 dim×dim）。

带符号的写法是 `affine_map<(d0)[s0] -> (d0 + s0)>`：`d0` 是 dimension，`s0` 是 symbol。区别不在类型（都是 `index`），而在语义角色：dimension 随循环变化，symbol 在循环内不变。依赖分析靠这个区分建立约束。

## affine.for 与 affine.if

### affine.for：半开区间 + 仿射边界

语法（源码 `mlir/include/mlir/Dialect/Affine/IR/AffineOps.td:143-152`）：

```text
affine.for %i = lower-bound to upper-bound step step-value { ... }
```

三条规则：

1. 区间是**半开**的：包含下界，不包含上界；
2. `step` 必须是正的常量整数，省略时为 1；
3. 边界可以是仿射 map 的应用，多个结果时用 `max`/`min` 取极值。

例子 `affine.for %i = 0 to 8` 迭代 `i = 0..7`。循环体只有一个块，块参数就是归纳变量，类型是 `index`；custom 打印时 `affine.yield` 省略。`affine.for` 也支持 `iter_args`，把循环携带值写进语法，和 `scf.for` 类似；区别在边界与下标的可分析性。

### affine.if：整数集定义的区域

`affine.if` 的条件不是 `i1`，而是一个 **integer set**（整数集）：一组仿射不等式的合取。

源码 `AffineOps.td:357-370` 给的语法：

```text
affine.if integer-set-attr(dim-and-symbol-use-list) { ... } else { ... }
```

例子：

```mlir
#set = affine_set<(d0) : (d0 - 2 >= 0, 6 - d0 >= 0)>
affine.if #set(%i) { ... }
```

这个集合表示 `2 <= i <= 6`。第二个约束打印时会规范化为 `-d0 + 6 >= 0`。规范化不是改写语义，只是把系数整理成统一形式。

`affine.if` 的两个 region 都必须单块、无参数，并以 `affine.yield` 结尾；不返回值时 yield 可省略。

## affine map 与 affine set

`AffineMap`（源码 `mlir/include/mlir/IR/AffineMap.h:46`）由三部分组成：`numDims`（dimension 个数）、`numSymbols`（symbol 个数）、`results`（一组仿射表达式）。

```mlir
#map = affine_map<(d0, d1)[s0] -> (d0 + d1, d0 + s0)>
```

两个输入维、一个符号、两个结果。map 可以组合，`affine.apply` 就是“把 map 应用到一组 SSA 值”。典型用途：`affine.load/store` 的下标、`affine.for` 的边界、Linalg 的 indexing maps（Ch 23）、`memref.subview` 的布局计算。

`IntegerSet`（源码 `mlir/include/mlir/IR/IntegerSet.h:44`）是仿射约束的合取：

```mlir
#set = affine_set<(d0, d1)[s0] : (d0 - 10 >= 0, s0 - d0 - 9 >= 0, d1 - 10 >= 0)>
```

每个约束形如 `expr >= 0` 或 `expr == 0`。`affine.if` 用它判断当前迭代点是否落在区域内；依赖分析用它描述迭代域。

读 IR 时不要被打印形式骗了：输入 `(d0 - 2 >= 0, 6 - d0 >= 0)` 会打印成 `(d0 - 2 >= 0, -d0 + 6 >= 0)`，比较的是解集，不是字面。

## 循环不变量

循环不变量（loop-invariant）指“在循环内不随归纳变量变化的值”。把它移到循环外，叫 LICM。

```mlir
func.func @licm(%A: memref<8xf32>, %c: f32) {
  affine.for %i = 0 to 8 {
    %v = arith.addf %c, %c : f32
    affine.store %v, %A[%i] : memref<8xf32>
  }
  return
}
```

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\licm.mlir --pass-pipeline='builtin.module(func.func(affine-loop-invariant-code-motion))'
```

实测：

```mlir
module {
  func.func @licm(%arg0: memref<8xf32>, %arg1: f32) {
    %0 = arith.addf %arg1, %arg1 : f32
    affine.for %arg2 = 0 to 8 {
      affine.store %0, %arg0[%arg2] : memref<8xf32>
    }
    return
  }
}
```

`arith.addf` 被提到循环外。判断依据在 `mlir/lib/Dialect/Affine/Transforms/AffineLoopInvariantCodeMotion.cpp:53-65`：`isOpLoopInvariant` 检查操作数是否都在循环外定义、op 是否无副作用、嵌套 `affine.if` 的条件是否也不变。

有副作用的 op 不能随便外提：`affine.store` 每次写不同地址，不能提；`affine.load` 如果对应 memref 在循环里也被写，也不能提（源码注释 `:93-94`）。

> 源码依据：`mlir/lib/Dialect/Affine/Transforms/AffineLoopInvariantCodeMotion.cpp:53-65`（`isOpLoopInvariant`）、`:93-94`（memref 在循环里被写则不 hoist）；`mlir/lib/Dialect/Affine/Transforms/AffineParallelize.cpp:60-84`（`runOnOperation` 调 `isLoopParallel`）。

## 底层机制：依赖分析在算什么

依赖是“两次内存访问指向同一位置”的关系。三类经典依赖：

| 类型 | 原名 | 含义 |
|---|---|---|
| 流依赖 | RAW | 先写后读 |
| 反依赖 | WAR | 先读后写 |
| 输出依赖 | WAW | 先写后写 |

同一次迭代内的依赖叫 loop-independent；不同迭代之间的叫 loop-carried。后者才是并行化的主要障碍。

依赖分析要回答三个问题：有没有依赖（存在性）、距离是多少（distance vector）、方向是什么（direction vector）。

例子：

```mlir
affine.for %i = 1 to 8 {
  %prev = affine.load %A[%i - 1] : memref<8xf32>
  affine.store %prev, %A[%i] : memref<8xf32>
}
```

写 `A[i]`、读 `A[i-1]`。第 `i` 次迭代的写会被第 `i+1` 次迭代读到，所以存在距离为 1 的流依赖。这个循环不能直接并行：并行执行时，读可能发生在写之前。

MLIR 的入口在 `mlir/include/mlir/Dialect/Affine/Analysis/AffineAnalysis.h:172-177`：

```cpp
DependenceResult checkMemrefAccessDependence(
    const MemRefAccess &srcAccess, const MemRefAccess &dstAccess,
    unsigned loopDepth, ...);
```

返回值有三种：`HasDependence`、`NoDependence`、`Failure`。`Failure` 表示“分析不了”，此时优化必须保守。更高层封装是 `isLoopParallel` / `isLoopMemoryParallel`（同文件 `:53-62`）；`--affine-parallelize` 先调它，再决定要不要把 `affine.for` 变成 `affine.parallel`（源码 `AffineParallelize.cpp:60-84`）。

## 示例一：逐级降级

准备 `affine.mlir`：

```mlir
#set = affine_set<(d0) : (d0 - 2 >= 0, 6 - d0 >= 0)>
#map = affine_map<(d0) -> (d0 + 1)>

func.func @affine_demo(%A: memref<8xf32>, %B: memref<8xf32>) {
  affine.for %i = 0 to 8 {
    %a = affine.load %A[%i] : memref<8xf32>
    %b = affine.load %B[%i] : memref<8xf32>
    %s = arith.addf %a, %b : f32
    affine.if #set(%i) {
      %j = affine.apply #map(%i)
      affine.store %s, %B[%j] : memref<8xf32>
    }
  }
  return
}
```

解析后的 custom 形式：

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir
```

实测（节选）：

```mlir
#map = affine_map<(d0) -> (d0 + 1)>
#set = affine_set<(d0) : (d0 - 2 >= 0, -d0 + 6 >= 0)>
affine.for %arg2 = 0 to 8 {
  ...
  affine.if #set(%arg2) {
    %3 = affine.apply #map(%arg2)
    affine.store %2, %arg1[%3] : memref<8xf32>
  }
}
```

第一级：`--lower-affine`。

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir --pass-pipeline='builtin.module(func.func(lower-affine))'
```

实测（节选）：

```mlir
module {
  func.func @affine_demo(%arg0: memref<8xf32>, %arg1: memref<8xf32>) {
    %c0 = arith.constant 0 : index
    %c8 = arith.constant 8 : index
    %c1 = arith.constant 1 : index
    scf.for %arg2 = %c0 to %c8 step %c1 {
      %0 = memref.load %arg0[%arg2] : memref<8xf32>
      %1 = memref.load %arg1[%arg2] : memref<8xf32>
      %2 = arith.addf %0, %1 : f32
      ...
      %4 = arith.cmpi sge, %3, %c0_0 : index
      ...
      %7 = arith.cmpi sge, %6, %c0_0 : index
      %8 = arith.andi %4, %7 : i1
      scf.if %8 {
        ...
        memref.store %2, %arg1[%9] : memref<8xf32>
      }
    }
    return
  }
}
```

逐项对照（节选省略了部分常量与中间值）：`affine.for` → `scf.for`；`affine.load/store` → `memref.load/store`；`affine.apply` → `arith.addi`；`affine.if` → `scf.if`，整数集展开成两条 `arith.cmpi sge` + `arith.andi`；`arith` 仍然在场，因为这一步只降 affine。

第二级：接 `convert-scf-to-cf`。

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir `
  --pass-pipeline='builtin.module(func.func(lower-affine,convert-scf-to-cf))'
```

实测（节选关键结构）：

```mlir
cf.br ^bb1(%c0 : index)
^bb1(%0: index):  // 2 preds: ^bb0, ^bb4
  %1 = arith.cmpi slt, %0, %c8 : index
  cf.cond_br %1, ^bb2, ^bb5
^bb2:  // pred: ^bb1
  cf.cond_br %10, ^bb3, ^bb4
^bb4:  // 2 preds: ^bb2, ^bb3
  %12 = arith.addi %0, %c1 : index
  cf.br ^bb1(%12 : index)
^bb5:  // pred: ^bb1
  return
```

到这里，affine 与 scf 都没了，只剩 `arith`、`memref`、`cf`、`func`。再往 `llvm` 降就是 Ch 21。

## 示例二：顺序反了会怎样

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir `
  --pass-pipeline='builtin.module(func.func(convert-scf-to-cf,lower-affine))'
```

实测输出（节选）：

```mlir
scf.for %arg2 = %c0 to %c8 step %c1 {
  %0 = memref.load %arg0[%arg2] : memref<8xf32>
  %1 = memref.load %arg1[%arg2] : memref<8xf32>
  ...
  scf.if %8 {
    ...
  }
}
```

`scf.for` 和 `scf.if` 都留下来了。原因：**`convert-scf-to-cf` 先跑时，IR 里还没有 `scf` op**（只有 affine），它什么也没做；随后 `lower-affine` 生成了新的 `scf.for/if`，但负责降它们的 pass 已经跑完。

这不是 bug，是 pipeline 的基本规则：**pass 只处理它运行时看到的 IR**。正确顺序永远是把生产者放在消费者前面：

```text
affine → lower-affine → scf → convert-scf-to-cf → cf → convert-cf-to-llvm → llvm
```

## 示例三：依赖判定让 parallelize 停手

```mlir
func.func @carried(%A: memref<8xf32>) {
  affine.for %i = 1 to 8 {
    %prev = affine.load %A[%i - 1] : memref<8xf32>
    %v = arith.addf %prev, %prev : f32
    affine.store %v, %A[%i] : memref<8xf32>
  }
  return
}

func.func @independent(%A: memref<8xf32>, %B: memref<8xf32>) {
  affine.for %i = 0 to 8 {
    %v = affine.load %A[%i] : memref<8xf32>
    affine.store %v, %B[%i] : memref<8xf32>
  }
  return
}
```

```powershell
& "$mlir\mlir-opt.exe" .\dependence.mlir --pass-pipeline='builtin.module(func.func(affine-parallelize))'
```

实测：

```mlir
module {
  func.func @carried(%arg0: memref<8xf32>) {
    affine.for %arg1 = 1 to 8 {
      %0 = affine.load %arg0[%arg1 - 1] : memref<8xf32>
      %1 = arith.addf %0, %0 : f32
      affine.store %1, %arg0[%arg1] : memref<8xf32>
    }
    return
  }
  func.func @independent(%arg0: memref<8xf32>, %arg1: memref<8xf32>) {
    affine.parallel (%arg2) = (0) to (8) {
      %0 = affine.load %arg0[%arg2] : memref<8xf32>
      affine.store %0, %arg1[%arg2] : memref<8xf32>
    }
    return
  }
}
```

`carried` 保持 `affine.for`，`independent` 变成 `affine.parallel`。这不是启发式猜测：`isLoopParallel` 做了依赖检查，前者的写和下一次迭代的读重叠，后者两个 memref 不重叠。

## 对比与取舍

| 维度 | `affine` | `scf` | `linalg` |
|---|---|---|---|
| 边界/下标 | 必须是仿射表达式 | 任意 SSA 值 | 由 maps 描述 |
| 可分析性 | 强，支持依赖/多面体分析 | 弱，需其他分析 | 中，结构显式 |
| 优化 | 并行化、融合、tiling、LICM | 通用循环优化 | tiling、vectorization、fusion |
| 降级目标 | `arith + scf + memref` | `cf` | 循环或 vector |
| 适合 | 规则数值内核 | 通用控制流 | 张量计算 |

选择建议：边界来自运行时任意值、体内有复杂控制流，选 `scf`；规则嵌套循环、想自动并行/融合/分块，选 `affine`；带形状的张量计算、想保留 maps 语义，选 `linalg`。三者可以混用，但**不要指望 `lower-affine` 之后再跑 affine 优化**；降级是单向的。

## 踩雷集锦

1. **以为 `affine.load` 能吃任意下标。** 在 `affine.for` 里把 `arith.muli` 的结果当下标，实测报 “operand cannot be used as a dimension id”；affine 只认 affine 作用域里的 dim/symbol（归纳变量、`affine.apply`、循环不变量）。
2. **以为 `affine.if` 的条件是 `i1`。** 它要 integer set + 绑定值；写 `affine.if %cond` 直接解析失败。
3. **先 `convert-scf-to-cf` 再 `lower-affine`。** 实测留下 `scf.for/if`。顺序就是契约。
4. **把 `affine.parallel` 当成线程。** 它只是并行语义标记；没有后续映射，仍然按顺序执行。
5. **依赖分析返回 `Failure` 时当成“无依赖”。** 必须保守；`Failure` 是“证明不了”，不是“没问题”。

## 进阶：再往深一层

- **多面体模型**：`FlatAffineValueConstraints` 把迭代域和访问关系编码成整数约束系统；依赖检查本质上是问“这个系统有没有整数解”（`mlir/lib/Dialect/Affine/Analysis/AffineStructures.cpp`）。
- **依赖分量**：`DependenceComponent` 给出每个循环维的距离上下界（`AffineAnalysis.h:143-150`），融合/交换顺序时用它判断合法性。
- **affine scope**：`affine.for` 建立新的 affine scope；跨 scope 的值只能作为 symbol，且必须是循环不变量。
- **和 Linalg 的关系**：Linalg 的 indexing maps 就是 affine map；`convert-linalg-to-affine-loops` 生成的正是 `affine.for`。

## 动手练习

### 练习 1：复现逐级降级

**任务规格**：保存 `affine.mlir`，依次跑原样打印、`lower-affine`、`lower-affine,convert-scf-to-cf`。

**测试用例**：第二步出现 `scf.for` + `scf.if` + `memref.load/store`；第三步没有 `affine.` 和 `scf.`，并出现 `cf.br` / `cf.cond_br`。

**分步骤**：

1. 保存文件；
2. 跑三条命令；
3. 用 `Select-String` 检查每步残留的方言；
4. 确认 `affine.apply` 变成了 `arith.addi`。

<details>
<summary>参考解答</summary>

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir
& "$mlir\mlir-opt.exe" .\affine.mlir --pass-pipeline='builtin.module(func.func(lower-affine))'
& "$mlir\mlir-opt.exe" .\affine.mlir --pass-pipeline='builtin.module(func.func(lower-affine,convert-scf-to-cf))'
```

检查残留：

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir --pass-pipeline='builtin.module(func.func(lower-affine))' |
  Select-String 'affine\.|scf\.|memref\.'
```

</details>

**常见卡点**：把 `lower-affine` 放进 `builtin.module(...)`。它是 op-agnostic pass，放 module 层也能跑，但教材统一用 `func.func`，便于和后续 pass 对齐。

**自我检核**：`affine.if` 的两条约束分别变成了哪两个 `arith.cmpi`？

### 练习 2：制造顺序错误

**任务规格**：交换两条 pass，观察最终 IR。

**测试用例**：输出中仍有 `scf.for` 和 `scf.if`；没有 `cf.br`；`affine.for` 已经消失。

**分步骤**：

1. 跑 `convert-scf-to-cf,lower-affine`；
2. 用 `Select-String 'scf\.'` 确认残留；
3. 解释为什么第一次 `convert-scf-to-cf` 什么也没做。

<details>
<summary>参考解答</summary>

```powershell
& "$mlir\mlir-opt.exe" .\affine.mlir `
  --pass-pipeline='builtin.module(func.func(convert-scf-to-cf,lower-affine))' |
  Select-String 'scf\.|cf\.'
```

输出只有 `scf.for` / `scf.if`，没有 `cf.`。第一次 `convert-scf-to-cf` 时 IR 里只有 `affine`，没有可匹配的 `scf`。

</details>

**常见卡点**：以为 pass 会“递归处理新生成的 IR”。不会；一个 pass 只在自己那一轮扫描里处理当时存在的 op。

**自我检核**：如果再加一条 `convert-scf-to-cf`，结果会怎样？

### 练习 3：让 parallelize 停手

**任务规格**：用 `carried` 和 `independent` 两个函数验证依赖分析的选择性。

**测试用例**：`carried` 保持 `affine.for`；`independent` 变成 `affine.parallel`；两个函数签名不变。

**分步骤**：

1. 保存 `dependence.mlir`；
2. 跑 `--affine-parallelize`；
3. 把 `carried` 里的 `%i - 1` 改成 `%i`，再跑一次；
4. 观察它是否也变成 `affine.parallel`。

<details>
<summary>参考解答</summary>

第三次运行中，读和写都在 `A[i]`，同一次迭代内是 RAW，但没有跨迭代依赖；`affine-parallelize` 会把 `carried` 也转成 `affine.parallel`。这说明它检查的是**循环携带依赖**，不是“循环里有没有 load/store”。

</details>

**常见卡点**：把 loop-independent 依赖误当成并行化障碍。同一个迭代内的读写不阻止不同迭代并行。

**自我检核**：距离为 0 和距离为 1 的依赖分别意味着什么？

### 练习 4：把 step 改成 2

**任务规格**：把 `affine.mlir` 的 `affine.for %i = 0 to 8` 改成 `step 2`，重跑 `lower-affine`。

**测试用例**：输出出现 `%c2 = arith.constant 2 : index` 和 `scf.for %arg2 = %c0 to %c8 step %c2`；归纳变量取 0、2、4、6。

**分步骤**：改 step → 跑 `lower-affine` → 数迭代次数。

<details>
<summary>参考解答</summary>
step 原样保留到 `scf.for`，`affine.if` 仍用 `%arg2` 与整数集比较。
</details>

**常见卡点**：以为 step 会被折叠进下标。**自我检核**：`step 2` 时循环体执行几次？

## 本章重点整理

- affine 用限制换可分析性：仿射边界、仿射下标、dim/symbol 区分。
- `affine.for` 是半开区间，step 为正常量；`affine.if` 的条件是 integer set。
- affine map 描述下标变换，integer set 描述迭代域；打印会规范化。
- LICM 只外提无副作用且操作数不变的值。
- 依赖分 RAW/WAR/WAW，关注 loop-carried；`checkMemrefAccessDependence` 可能返回 `Failure`。
- `affine-parallelize` 用 `isLoopParallel` 做合法性检查，有依赖就保留 `affine.for`。
- `lower-affine` 生成 `arith + scf + memref`；`convert-scf-to-cf` 必须排在它后面。

## 自我检核

- [ ] 我能写出一个合法的 affine map 和一个 integer set
- [ ] 我能解释 dim 与 symbol 的区别
- [ ] 我能解释 LICM 为什么不能外提 store
- [ ] 我能判断一个循环是否有 loop-carried 依赖
- [ ] 我能复现“顺序反了留下 scf.for”
- [ ] 我知道 `Failure` 在依赖分析里意味着什么

## 延伸阅读

- **[Affine Dialect 文档](https://mlir.llvm.org/docs/Dialects/Affine/)**
  - **读哪里**：Operations、affine maps and integer sets、restrictions 三节。
  - **学什么**：dim/symbol 与作用域的官方定义。
  - **和本章的关联**：`affine.for/if` 的语法依据。
- **[Affine 源码：AffineOps.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Affine/IR/AffineOps.td)**
  - **读哪里**：`AffineForOp`、`AffineIfOp`、`AffineLoadOp`、`AffineStoreOp`。
  - **学什么**：每个 op 的约束与 region 结构。
  - **和本章的关联**：示例一的输入语法。
- **[Affine 源码：AffineAnalysis.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Affine/Analysis/AffineAnalysis.h)**
  - **读哪里**：`isLoopParallel`、`MemRefAccess`、`checkMemrefAccessDependence`。
  - **学什么**：依赖分析的 API 与返回值语义。
  - **和本章的关联**：示例三的判定逻辑。
- **[LowerAffine 实现](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/AffineToStandard/AffineToStandard.cpp)**
  - **读哪里**：`AffineForLowering`、`AffineIfLowering`、`AffineLoadLowering`。
  - **学什么**：每种 affine op 降成什么。
  - **和本章的关联**：示例一的每一行输出都能在这里找到对应 pattern。

---

循环和依赖讲完了，下一步是把“形状”本身变成一等公民：tensor 表示值，vector 表示寄存器，gpu 表示设备分层。

→ [Ch 25 Tensor、Vector 与 GPU](./25-tensor-vector-gpu.md)
