# Ch 20 Bufferization：tensor 到 memref

> **目标**：把一段 `tensor` + `linalg` 程序真正 bufferize 成 `memref`，看懂 one-shot bufferize 的决策过程，并能解释为什么它不是“把类型名换一下”。学完本章，你应该能判断一次 bufferize 为什么插入了 `memref.alloc`、为什么没有插入、以及函数边界上的 tensor 参数去了哪里。

## 为什么需要这个？

`tensor` 和 `memref` 看起来只差一个类型名，实际差的是两套语义：

```mlir
// 值语义：%0 是一个新值，和 %a 没有别名关系
%0 = linalg.generic ins(%a : tensor<4xf32>) outs(%init : tensor<4xf32>) { ... } -> tensor<4xf32>
```

```mlir
// 缓冲语义：这次写操作直接改了 %buf，%buf 的所有别名都能看见
linalg.generic ins(%a : memref<4xf32>) outs(%buf : memref<4xf32>) { ... }
```

在 tensor 世界里，每个 op 产生一个新值，SSA 的 use-def 链就是数据流；在 memref 世界里，值是指针加布局，多个 SSA 值可以指向同一块内存。**bufferization 的核心任务不是改类型，而是给每个 tensor 值选一块 buffer，并证明“复用这块 buffer 不会改变程序结果”。**

这个证明一旦做错，程序会静默算错。于是 one-shot bufferize 先做一次全局的 in-place 分析，再统一改 IR；`linalg`、`scf`、`tensor` 等方言通过 `BufferizableOpInterface` 向分析器报告“我这个 operand 会读/会写/会别名”。

## 先建立直觉：从数据流图到别名图

先看一段 tensor 程序：

```mlir
#map = affine_map<(d0, d1) -> (d0, d1)>
func.func @axpy(%a: tensor<4x4xf32>, %b: tensor<4x4xf32>, %c: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %0 = linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel", "parallel"]}
      ins(%a, %b : tensor<4x4xf32>, tensor<4x4xf32>)
      outs(%c : tensor<4x4xf32>) {
  ^bb0(%in1: f32, %in2: f32, %out: f32):
    %m = arith.mulf %in1, %in2 : f32
    %s = arith.addf %m, %out : f32
    linalg.yield %s : f32
  } -> tensor<4x4xf32>
  return %0 : tensor<4x4xf32>
}
```

值语义下，`%0` 是“`%a * %b + %c` 的结果”，它与 `%c` 是两个独立的值。bufferize 之后，一种合法实现是：

因为每个元素只被读一次、写一次，复用安全。于是输出里没有 `memref.alloc`，`return` 直接返回第三个参数。这就是 **in-place bufferization**。

反过来，如果某个 tensor 在被写之后还要被读，复用就会破坏旧值。分析器要么给新结果分配一块 buffer（out-of-place），要么在写之前拷贝旧 buffer。选哪一种，取决于冲突的形状和代价模型。

把 bufferization 想成三步：

```text
tensor IR
  │ 1. 分析：每个 tensor 值能否与其 producer/consumer 共用 buffer？
  ▼
带 in-place 决策的 tensor IR
  │ 2. 拷贝插入：不能共用时，插入 alloc/copy
  ▼
memref IR
  │ 3. 重写：每个可 bufferize 的 op 换成 memref 版本
  ▼
memref + linalg/scf/arith
```

one-shot 的名字就来自这里：**一次分析，覆盖整个函数（开启函数边界后是整个模块）**，而不是像旧的 bufferization pass 那样边走边猜。

## 核心概念一：tensor 的“值”与 memref 的“地址”

`tensor<4x4xf32>` 只描述形状和元素类型，不描述内存在哪、怎么排布；`memref<4x4xf32>` 描述一块可寻址的 buffer，`memref<4x4xf32, strided<[?, ?], offset: ?>>` 还描述了运行时才知道的 stride 和 offset。

这带来两个直接后果：

1. **tensor 值没有“地址”可复用**。两个 tensor 值即使内容相同，也不能假设它们共享存储；bufferization 必须显式建立这种共享。
2. **memref 的布局是类型的一部分**。bufferize 函数边界时，参数类型可能从 `tensor<4x4xf32>` 变成 `memref<4x4xf32, strided<[?, ?], offset: ?>>`，因为 callee 不能假设调用者一定传连续内存。

布局不是装饰。它决定了后续 `memref.load/store` 的地址计算，也决定了 ABI：Ch 21 会看到，一个 memref 参数在 LLVM 层会变成一个 descriptor 结构或一个裸指针，二者完全不兼容。

## 核心概念二：one-shot bufferize 的分析对象

one-shot 分析的不是 op，而是 **tensor SSA 值的别名关系**。每个 tensor 值在分析里有一组 alias set；两个值如果在同一个 alias set 里，说明它们可能指向同一块 buffer。

分析要回答的问题是：

- 这个 operand 在 bufferize 后会被读吗？
- 这个 operand 在 bufferize 后会被写吗？
- 这个 op 的 result 与哪个 operand 别名？
- 如果 result 与某个 operand 别名，写 result 会不会破坏一个之后还要读的旧值？

前三个问题由 `BufferizableOpInterface` 回答，最后一个由冲突检测回答。接口的约定很明确：**所有 `bufferizesTo*` 和 `getAliasing*` 方法都必须保守实现**；静态不确定时，按最坏情况返回。

源码依据（路径相对 `.scratch\llvm-23.1.4-src`）：`mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td:15-23` 写明“must be implemented conservatively”，`:30-38` 是 `bufferizesToAllocation`，`:42-61` 是 `bufferizesToMemoryRead`，`:64-90` 是 `bufferizesToMemoryWrite`，`:296-365` 是 `getAliasingOpOperands`，`:395-410` 是 `bufferize` 的替换契约。

`linalg.generic` 的实现很能说明问题（`mlir/lib/Dialect/Linalg/Transforms/BufferizableOpInterfaceImpl.cpp:99-160`）：

- `bufferizesToMemoryRead` 调用 `payloadUsesValueFromOperand`：region 里真的用了这个 operand 才算读；
- `bufferizesToMemoryWrite` 调用 `DestinationStyleOpInterface::isDpsInit`：只有 `outs`/init operand 才算写；
- `bufferize` 直接复用 `bufferizeDestinationStyleOpInterface`，把 tensor 换成 memref 后重建 op。

注意这里的“读”和“写”是 **bufferize 后的行为**，不是 tensor op 的字面语义。`linalg.generic` 的 `outs` 在 tensor 世界里是“初始值”，在 buffer 世界里是“可写目标”；同一个 operand 在两边的角色不同，接口就是这层翻译。

## 核心概念三：in-place 与 out-of-place

一次 bufferize 对某个 tensor 值的决策只有两类：

| 决策 | 结果 | 什么时候发生 |
|---|---|---|
| in-place | 复用某个 operand 的 buffer | 读写不冲突，且布局兼容 |
| out-of-place | 新分配一块 buffer（可能伴随 copy） | 存在 RaW 冲突、布局不兼容，或 op 明确要求新分配 |

“in-place”不等于“没有拷贝”。一个 op 可以在 in-place 写自己的输出，但为了保住旧值，bufferization 可能在写之前先 `memref.copy`。反过来，“out-of-place”也不一定拷贝：如果新 buffer 的内容完全由 op 写满，就不需要初始化。

最典型的冲突是 **read-after-write（RaW）**：一个值先被读，随后同一块 buffer 被写，而那个读还要用旧值。分析器会沿着 SSA 链检查读和写的顺序，只有在能证明读发生在写之前（或写发生在定义之前）时才允许复用。

源码依据：`mlir/lib/Dialect/Bufferization/Transforms/OneShotAnalysis.cpp:286-358` 用注释列出了各种 `DEF/READ/WRITE` 顺序，`:435-449` 的 `annotateConflict` 会生成 `C_x[READ: n]`、`C_x[CONFL-WRITE: n]` 这类调试属性——也就是本章示例里看到的那些标记。

## 核心概念四：函数边界是另一场谈判

默认的 `one-shot-bufferize` 只改函数体，不动 `func.func` 的签名。遇到 tensor 参数/返回值时，它在边界插入：

- `bufferization.to_buffer`：tensor → memref；
- `bufferization.to_tensor`：memref → tensor。

这样函数签名仍然是 tensor，调用者不受影响，但函数体里多了一次“借来的 buffer”视图，返回值还可能触发 `memref.alloc` + `memref.copy`。

要让函数签名一起变成 memref，加 `bufferize-function-boundaries`。它是实验特性，官方描述里明确列了限制：不支持递归/环形调用图，不支持多 block 或多 return 的函数（`mlir/include/mlir/Dialect/Bufferization/Transforms/Passes.td:446-459`）。

三个常被问到的选项：

| 选项 | 作用 | 默认 |
|---|---|---|
| `bufferize-function-boundaries` | 连 `func.func`/`return`/`call` 一起 bufferize | false |
| `function-boundary-type-conversion` | 函数签名用 `infer-layout-map`、`identity-layout-map` 还是 `fully-dynamic-layout-map` | `infer-layout-map` |
| `allow-return-allocs-from-loops` | 允许循环把新分配的 buffer 作为 iter_arg 结果带出 | false |

> 注意：不少旧文章写 `allow-return-allocs`。本机 23.1.4 的 `mlir-opt --help` 里 **没有** 这个选项，只有 `allow-return-allocs-from-loops`。源码注释 `OneShotAnalysis.cpp:39` 也还留着旧名字，但 CLI 和 `Passes.td:498-500` 用的是新名字。不要照抄旧博客。

`Passes.td:511-513` 定义 `bufferize-function-boundaries`，`:517-519` 定义 `copy-before-write`，`:528-532` 定义 `function-boundary-type-conversion`。这些名字都可以用 `mlir-opt --help` 直接核对。
## 底层机制：分析器到底在看什么

把 one-shot 的决策压缩成伪代码，大概是：

```text
for each tensor value v:
  aliasSet[v] = {v}
  inplace[v] = unknown

repeat until fixed point:
  for each bufferizable op:
    for each result r with aliasing operand o:
      if canReuse(o, r): merge(aliasSet[o], aliasSet[r])

for each read r and write w in the same alias set:
  if r may happen after w:  // RaW
    mark conflict; 不批准这次 in-place

insert copies / allocs for values whose in-place request was rejected
rewrite every bufferizable op to its memref form
```

三个细节决定了实际行为：

1. **alias set 是 may-alias，不是 must-alias**。合并得多，分析更保守，可能多分配；合并得少，可能漏掉冲突。接口要求保守，所以宁可多合并。
2. **顺序判断依赖 dominance 和 region 结构**。同一个 block 里按程序顺序，跨 region 要看 `RegionBranchOpInterface` 的语义；循环和 `scf.if` 是重点。
3. **布局不兼容会直接否决复用**。即使没有读写冲突，`memref<4x4xf32>` 和 `memref<4x4xf32, strided<[?, ?], offset: ?>>` 也不能无条件共用，需要 `memref.cast` 或新分配。

这就是为什么 bufferization 不是类型替换：它要同时维护 **值语义正确性**、**布局兼容性** 和 **性能**（少分配、少拷贝）。三者冲突时，默认优先级是正确性，其次是布局，最后才是性能。

## 示例一：一段 tensor + linalg 程序 bufferize 前后

输入文件 `axpy.mlir`（本章所有命令都在仓库根目录执行）：

```mlir
#map = affine_map<(d0, d1) -> (d0, d1)>
func.func @axpy(%a: tensor<4x4xf32>, %b: tensor<4x4xf32>, %c: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %0 = linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel", "parallel"]}
      ins(%a, %b : tensor<4x4xf32>, tensor<4x4xf32>)
      outs(%c : tensor<4x4xf32>) {
  ^bb0(%in1: f32, %in2: f32, %out: f32):
    %m = arith.mulf %in1, %in2 : f32
    %s = arith.addf %m, %out : f32
    linalg.yield %s : f32
  } -> tensor<4x4xf32>
  return %0 : tensor<4x4xf32>
}
```

跑：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\axpy.mlir --one-shot-bufferize="bufferize-function-boundaries allow-return-allocs-from-loops"
```

实测输出：

```mlir
#map = affine_map<(d0, d1) -> (d0, d1)>
module {
  func.func @axpy(%arg0: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg1: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg2: memref<4x4xf32, strided<[?, ?], offset: ?>>) -> memref<4x4xf32, strided<[?, ?], offset: ?>> {
    linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel", "parallel"]} ins(%arg0, %arg1 : memref<4x4xf32, strided<[?, ?], offset: ?>>, memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%arg2 : memref<4x4xf32, strided<[?, ?], offset: ?>>) {
    ^bb0(%in: f32, %in_0: f32, %out: f32):
      %0 = arith.mulf %in, %in_0 : f32
      %1 = arith.addf %0, %out : f32
      linalg.yield %1 : f32
    }
    return %arg2 : memref<4x4xf32, strided<[?, ?], offset: ?>>
  }
}
```

逐个看变化：

- **没有 `memref.alloc`**：`%0` 与 `%arg2` 的 buffer 合并了，`linalg.generic` 直接在 `%arg2` 上写。
- **没有 `memref.copy`**：因为每个元素只读一次、写一次，旧值不需要保留。
- **函数签名全变了**：tensor 参数变成 `memref<4x4xf32, strided<[?, ?], offset: ?>>`。这是 `infer-layout-map` 的默认行为：参数布局未知，结果布局从函数体推断。
- **`return %arg2`**：返回值就是第三个参数本身。调用者拿到的是同一块 buffer，所以“返回新 tensor”的语义在这里变成了“返回一个 alias”。

这一步已经能回答本章标题：bufferize 之后，**值的身份变了**。tensor 的 `%0` 消失了，取而代之的是对 `%arg2` 这块 buffer 的写。分析器必须证明这个替换合法，而不是把 `tensor` 字样替换成 `memref`。

## 示例二：看分析器如何标记冲突

构造一个转置写回的冲突：第一条 op 把 `%v` 写进 `%e0`，第二条 op 从 `%e1`（与 `%e0` 同源）转置读、再写回 `%e0`。

```mlir
#id = affine_map<(d0, d1) -> (d0, d1)>
#tr = affine_map<(d0, d1) -> (d1, d0)>
func.func @transpose_conflict(%v: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %e0 = tensor.empty() : tensor<4x4xf32>
  %0 = linalg.generic {indexing_maps = [#id, #id], iterator_types = ["parallel", "parallel"]}
      ins(%v : tensor<4x4xf32>) outs(%e0 : tensor<4x4xf32>) {
  ^bb0(%in: f32, %out: f32):
    linalg.yield %in : f32
  } -> tensor<4x4xf32>
  %1 = linalg.generic {indexing_maps = [#tr, #id], iterator_types = ["parallel", "parallel"]}
      ins(%0 : tensor<4x4xf32>) outs(%e0 : tensor<4x4xf32>) {
  ^bb0(%in: f32, %out: f32):
    linalg.yield %in : f32
  } -> tensor<4x4xf32>
  return %1 : tensor<4x4xf32>
}
```

先只看分析，不改 IR：

```powershell
& "$mlir\mlir-opt.exe" .\transpose.mlir --one-shot-bufferize="test-analysis-only print-conflicts"
```

实测输出（保留关键属性）：

```mlir
func.func @transpose_conflict(%arg0: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %0 = tensor.empty() : tensor<4x4xf32>
  %1 = linalg.generic {indexing_maps = [#map, #map], iterator_types = ["parallel", "parallel"]} ins(%arg0 : tensor<4x4xf32>) outs(%0 : tensor<4x4xf32>) attrs =  {"C_0[DEF: result 0]", __inplace_operands_attr__ = ["true", "false"]} {
  ...
  } -> tensor<4x4xf32>
  %2 = linalg.generic {indexing_maps = [#map1, #map], iterator_types = ["parallel", "parallel"]} ins(%1 : tensor<4x4xf32>) outs(%0 : tensor<4x4xf32>) attrs =  {"C_0[CONFL-WRITE: 1]", "C_0[READ: 0]", __inplace_operands_attr__ = ["true", "true"]} {
  ...
  } -> tensor<4x4xf32>
  return {__inplace_operands_attr__ = ["true"]} %2 : tensor<4x4xf32>
}
```

读法：

- `C_0` 是一个 alias set 的名字；`DEF: result 0` 表示这个 set 由第 0 个 result 定义。
- 第二条 op 的 `C_0[READ: 0]` 表示它读 `C_0`，`C_0[CONFL-WRITE: 1]` 表示第 1 个 operand（`outs(%0)`）写 `C_0`，两者冲突。
- `__inplace_operands_attr__` 是分析器给出的候选决策；有冲突时，最终 bufferization 会否决其中的 in-place 请求。

实际 bufferize：

```powershell
& "$mlir\mlir-opt.exe" .\transpose.mlir --one-shot-bufferize="bufferize-function-boundaries"
```

实测输出：

```mlir
func.func @transpose_conflict(%arg0: memref<4x4xf32, strided<[?, ?], offset: ?>>) -> memref<4x4xf32> {
  %alloc = memref.alloc() {alignment = 64 : i64} : memref<4x4xf32>
  %alloc_0 = memref.alloc() {alignment = 64 : i64} : memref<4x4xf32>
  linalg.generic {indexing_maps = [#map, #map], iterator_types = ["parallel", "parallel"]} ins(%arg0 : memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%alloc_0 : memref<4x4xf32>) {
  ...
  }
  linalg.generic {indexing_maps = [#map1, #map], iterator_types = ["parallel", "parallel"]} ins(%alloc_0 : memref<4x4xf32>) outs(%alloc : memref<4x4xf32>) {
  ...
  }
  %cast = memref.cast %alloc : memref<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
  return %alloc : memref<4x4xf32>
}
```

这里分析器选择了 **给第二条 op 新分配输出 buffer**，而不是在写之前拷贝 `%alloc_0`。这是 out-of-place 的一种：第二条 op 写满 `%alloc`，所以不需要初始化，也不需要 copy。`%alloc_0` 保留旧值，供第二条 op 读。

注意最后那个 `memref.cast`：`%alloc` 是静态布局 `memref<4x4xf32>`，函数返回类型是动态 strided，所以要 cast。cast 不是拷贝，只是换一个布局视图。

## 示例三：不 bufferize 函数边界，以及循环里的 alloc

### 3a. 只 bufferize 函数体

去掉 `bufferize-function-boundaries`：

```powershell
& "$mlir\mlir-opt.exe" .\axpy.mlir --one-shot-bufferize
```

实测输出（节选）：

```mlir
func.func @axpy(%arg0: tensor<4x4xf32>, %arg1: tensor<4x4xf32>, %arg2: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %0 = bufferization.to_buffer %arg1 : tensor<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
  %1 = bufferization.to_buffer %arg0 : tensor<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
  %2 = bufferization.to_buffer %arg2 : tensor<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
  %alloc = memref.alloc() {alignment = 64 : i64} : memref<4x4xf32>
  memref.copy %2, %alloc : memref<4x4xf32, strided<[?, ?], offset: ?>> to memref<4x4xf32>
  linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel", "parallel"]} ins(%1, %0 : memref<4x4xf32, strided<[?, ?], offset: ?>>, memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%alloc : memref<4x4xf32>) {
  ...
  }
  %3 = bufferization.to_tensor %alloc : memref<4x4xf32> to tensor<4x4xf32>
  return %3 : tensor<4x4xf32>
}
```

三个新东西：

- `bufferization.to_buffer` 把 tensor 参数“借”成 memref。它不拷贝，只是告诉 bufferization“这个 tensor 背后有一块 buffer”。
- `memref.copy %2, %alloc`：因为函数返回 tensor，`%alloc` 不能直接返回（tensor 语义要求新值），所以先把 `%arg2` 的 buffer 拷进新 alloc。
- `bufferization.to_tensor` 把结果 buffer 包回 tensor，函数签名保持不变。

这条路径适合“只 bufferize 库内部、边界仍用 tensor”的场景，但每次调用都多一次 copy。`bufferize-function-boundaries` 的价值就是把这次 copy 省掉，代价是 ABI 变成 memref。

### 3b. 循环里返回新 alloc

```mlir
func.func @loop_alloc(%v: tensor<4xf32>) -> tensor<4xf32> {
  ...
  %r = scf.for %i = %c0 to %c4 step %c1 iter_args(%acc = %e) -> (tensor<4xf32>) {
    %t = tensor.empty() : tensor<4xf32>
    %0 = linalg.generic ... ins(%acc : tensor<4xf32>) outs(%t : tensor<4xf32>) { ... } -> tensor<4xf32>
    scf.yield %0 : tensor<4xf32>
  }
  return %r : tensor<4xf32>
}
```

不开启选项：

```powershell
& "$mlir\mlir-opt.exe" .\loop-alloc.mlir --one-shot-bufferize="bufferize-function-boundaries"
```

实测报错：

```text
.\loop-alloc.mlir:16:5: error: Yield operand #0 is not equivalent to the corresponding iter bbArg
    scf.yield %0 : tensor<4xf32>
    ^
.\loop-alloc.mlir:16:5: note: see current operation: "scf.yield"(%7) : (tensor<4xf32>) -> ()
```

原因：循环体每次迭代都 `tensor.empty()` 一块新 buffer，再把它 yield 给下一次迭代。one-shot 默认要求“yield 的 buffer 必须和 iter_arg 是同一块”，否则循环的 buffer 生命周期无法静态确定。

加上 `allow-return-allocs-from-loops`：

```powershell
& "$mlir\mlir-opt.exe" .\loop-alloc.mlir --one-shot-bufferize="bufferize-function-boundaries allow-return-allocs-from-loops"
```

实测输出（节选）：

```mlir
func.func @loop_alloc(%arg0: memref<4xf32, strided<[?], offset: ?>>) -> memref<4xf32> {
  %alloc = memref.alloc() {alignment = 64 : i64} : memref<4xf32>
  %0 = scf.for %arg1 = %c0 to %c4 step %c1 iter_args(%arg2 = %alloc) -> (memref<4xf32>) {
    %alloc_0 = memref.alloc() {alignment = 64 : i64} : memref<4xf32>
    linalg.generic {indexing_maps = [#map, #map], iterator_types = ["parallel"]} ins(%arg2 : memref<4xf32>) outs(%alloc_0 : memref<4xf32>) {
    ...
    }
    scf.yield %alloc_0 : memref<4xf32>
  }
  %cast = memref.cast %0 : memref<4xf32> to memref<4xf32, strided<[?], offset: ?>>
  return %0 : memref<4xf32>
}
```

这次允许了“每轮新 alloc、yield 出去”。代价是每轮一次分配，且没有 dealloc；真实 pipeline 还要接 `buffer-deallocation-pipeline` 或 ownership-based deallocation 来回收。这里只关心 bufferization 的决策，不展开释放策略。
## 补充验证：`copy-before-write`——没有分析的世界

如果你想直观感受“分析”带来了什么，可以关掉分析，让每次写之前都拷贝：

```powershell
& "$mlir\mlir-opt.exe" .\axpy.mlir --one-shot-bufferize="bufferize-function-boundaries copy-before-write"
```

实测输出（节选）：

```mlir
func.func @axpy(%arg0: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg1: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg2: memref<4x4xf32, strided<[?, ?], offset: ?>>) -> memref<4x4xf32> {
  %alloc = memref.alloc() {alignment = 64 : i64} : memref<4x4xf32>
  memref.copy %arg2, %alloc : memref<4x4xf32, strided<[?, ?], offset: ?>> to memref<4x4xf32>
  linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel", "parallel"]} ins(%arg0, %arg1 : memref<4x4xf32, strided<[?, ?], offset: ?>>, memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%alloc : memref<4x4xf32>) {
  ...
  }
  %cast = memref.cast %alloc : memref<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
  return %alloc : memref<4x4xf32>
}
```

和示例一对比：

- 示例一：in-place 写 `%arg2`，**0 次分配、0 次拷贝**。
- 这里：先分配 `%alloc`，再把 `%arg2` 整块拷过去，然后写 `%alloc`，**1 次分配、1 次拷贝**。
- 语义都正确，差别只在性能。`copy-before-write` 的价值是“保证正确、跳过分析”，适合调试或分析器不支持的 op；生产管线不会默认开它。

这也解释了为什么 one-shot 的分析值得做：它把“每次写都拷贝”降成“只在冲突处拷贝/分配”。分析越精确，分配越少；分析越保守，IR 越接近 `copy-before-write` 的形态。

## 对比与取舍

### 早 bufferize vs 晚 bufferize

| 时机 | 好处 | 代价 |
|---|---|---|
| 在 tensor 层做优化，最后 bufferize | 优化器只看值语义，不用考虑别名；`linalg` 的 reshape/fusion 更容易 | 最后一次性决策，冲突多时可能多分配 |
| 先 bufferize，再做循环优化 | 直接操作 memref，接近最终代码；方便做向量化/循环变换 | 别名信息变复杂，很多 tensor 层 pattern 失效 |

主流做法是“尽量晚”：在 tensor 层完成结构化变换，进入 codegen 前才 bufferize。IREE、Torch-MLIR 都采用类似分层。晚 bufferize 的前提是 one-shot 能正确决策；如果 IR 里混入大量不实现接口的 op，分析会变保守，分配会变多。

### 函数边界：tensor ABI 还是 memref ABI

- **保留 tensor 边界**：调用者简单，库可以独立 bufferize；代价是每次调用都有 `to_buffer`/`to_tensor`，返回 tensor 时可能拷贝。
- **bufferize 函数边界**：省掉边界拷贝，签名直接暴露 memref；代价是 ABI 变了，调用者必须按 memref descriptor 传参，且实验特性对调用图有限制。

这也是 Ch 21 的起点：一旦函数签名变成 memref，下一步就是把它降成 LLVM 的 descriptor 或裸指针。

### one-shot vs 旧的 partial bufferization

旧的 `-bufferize` 系列按 op 局部改写，遇到冲突时倾向于拷贝，缺乏全局视野。one-shot 用一次全局分析换更少的分配，但要求所有相关 op 都实现 `BufferizableOpInterface`；不实现的 op 只能保守处理，或者用 `allow-unknown-ops` 在边界插 `to_buffer`/`to_tensor`。

### 分配与释放是两件事

one-shot 只负责“决定 buffer 并改写”，不负责释放。`memref.alloc` 之后谁来 `memref.dealloc`，是另一个 pass 的职责。很多初学者看到输出里全是 alloc 就以为 bufferization 有内存泄漏；其实这是管线还没接 deallocation。上游提供 `buffer-deallocation-pipeline` 和 ownership-based deallocation 两种方案，前者适合简单函数，后者适合跨函数所有权传递。

## 踩雷集锦

1. **把 bufferization 当成类型替换。** 只把 `tensor<4x4xf32>` 写成 `memref<4x4xf32>`，却保留两个 op 各自写同一块 buffer，程序会静默算错。正确做法是让 one-shot 分析别名，或手工保证读写不冲突。

2. **在 tensor IR 上直接跑 `convert-linalg-to-loops`。** 这个 pass 只处理 buffer 语义的 `linalg` op；tensor 版本会被静默跳过，IR 看起来“没反应”。先 bufferize，再降 linalg。

3. **忘了 `bufferize-function-boundaries`，却奇怪为什么有 `to_buffer` 和 `memref.copy`。** 默认只改函数体；边界上的 tensor 参数/返回值必须用 `to_buffer`/`to_tensor` 过渡，返回 tensor 还会触发拷贝。

4. **照抄旧文章的 `allow-return-allocs`。** 23.1.4 的 CLI 没有这个选项，只有 `allow-return-allocs-from-loops`。用 `mlir-opt --help` 核对，别靠记忆。

5. **以为 `memref.alloc` 一定带初始化。** `memref.alloc` 不给初值；如果 op 没有写满整个 buffer，读到的是未定义内容。需要零初始化时用 `memref.alloc` 后接 `linalg.fill`，或使用 `bufferization.alloc_tensor` 的 copy 语义。

6. **以为任意两个 memref 布局都能 `memref.cast`。** cast 只在布局兼容（可证明的 stride/offset 关系）时合法；静态 `memref<4x4xf32>` 可以 cast 成动态 strided，反过来不一定。布局不兼容时，one-shot 会选新分配而不是 cast。

7. **bufferize 完直接跑 `mlir-translate`。** 输出里还有 `linalg`、`scf`、`memref`，`mlir-translate` 只认 `llvm` dialect。bufferization 只是 Part 3 的第一步，后面还有 Ch 21 的 conversion。

## 进阶：再往深一层

- **看 alias set**：`--one-shot-bufferize="test-analysis-only dump-alias-sets"` 会给每个 tensor 值标注 alias set。对比 `print-conflicts`，一个看“谁和谁可能别名”，一个看“哪次读写冲突”。
- **用 `bufferization.alloc_tensor` 表达“我要新 buffer”**：它和 `tensor.empty` 的区别是显式声明分配意图，还可以带 `copy` operand，让 bufferization 在需要时保留旧值。
- **读 `bufferization.to_tensor`/`to_buffer` 的实现**：它们不是 no-op，而是 bufferization 的边界契约；理解它们能解释很多“为什么这里多了一次 copy”。
- **接 deallocation**：在 one-shot 之后跑 `--buffer-deallocation-pipeline`，观察 `memref.dealloc` 插在哪。注意它要求 dealloc op 之外没有别的 dealloc，且对控制流有要求。

## 动手练习

1. **复现 axpy 的前后 IR。** 保存 `axpy.mlir`，分别跑“只 bufferize 函数体”和“bufferize 函数边界”，把两份输出并排比较，指出 `memref.copy` 出现的条件。

2. **改一处 in-place 决策。** 把 `axpy` 的 `outs(%c)` 改成 `outs(%a)`，重新 bufferize，观察输出是否仍然没有 alloc，并解释为什么。

3. **制造一个必须 out-of-place 的例子。** 在 `transpose_conflict` 后面再加一条读 `%1` 的 op，预测 bufferization 会增加几个 alloc，再跑命令验证。提示：看 `C_0` 的 alias set 是否扩大。

4. **切换函数边界布局。** 用 `function-boundary-type-conversion=identity-layout-map` 重跑示例一，比较签名和函数体里的 `memref.cast` 数量。再试 `fully-dynamic-layout-map`。

5. **观察 `copy-before-write`。** 对 `axpy.mlir` 跑 `--one-shot-bufferize="bufferize-function-boundaries copy-before-write"`，解释为什么每个写之前都多了一次 `memref.copy`，以及它和 one-shot 分析的关系。

## 本章重点整理

- bufferization 的核心是**别名决策**，不是类型替换；tensor 是值语义，memref 是 buffer 语义。
- one-shot bufferize = 全局 in-place 分析 + 拷贝插入 + 按 `BufferizableOpInterface` 重写。
- `bufferizesToMemoryRead/Write`、`getAliasingOpOperands`、`bufferize` 是接口的四个关键点；实现必须保守。
- 函数边界默认不动；`bufferize-function-boundaries` 才会重写签名，且是实验特性。
- `allow-return-allocs-from-loops` 控制循环能否带出新 alloc；23.1.4 没有 `allow-return-allocs`。
- 布局不兼容会否决 in-place；`memref.cast` 只改视图，不拷贝。
- one-shot 不负责释放；deallocation 是后续 pass 的职责。

## 自我检核

- [ ] 我能用一句话说清 tensor 值语义和 memref buffer 语义的差别
- [ ] 我能解释为什么 `axpy` bufferize 后没有 `memref.alloc`
- [ ] 我能读懂 `C_0[READ: 0]` 和 `C_0[CONFL-WRITE: 1]` 的含义
- [ ] 我知道 `bufferize-function-boundaries` 打开前后，函数签名和 `to_buffer` 各有什么变化
- [ ] 我能说出 `allow-return-allocs-from-loops` 解决的是哪一类循环
- [ ] 我能解释 `memref.cast` 为什么不是拷贝
- [ ] 我知道 one-shot 之后还缺 deallocation，并知道至少一个补它的 pass

## 延伸阅读

- **[MLIR Bufferization 官方文档](https://mlir.llvm.org/docs/Bufferization/)**
  - **读哪里**：开头的 “Bufferization” 概念、One-Shot Bufferize 一节、以及 “Bufferization Options”。
  - **学什么**：one-shot 的设计目标、in-place 分析、函数边界选项的官方解释。
  - **和本章的关联**：本章的示例就是这份文档的最小可跑版本；文档补全了 ownership 和 deallocation 的上下文。

- **[BufferizableOpInterface.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td)**
  - **读哪里**：`bufferizesToAllocation`、`bufferizesToMemoryRead/Write`、`getAliasingOpOperands`、`bufferize` 四组方法。
  - **学什么**：接口契约里“必须保守”的具体含义。
  - **和本章的关联**：理解这四个方法，才能解释为什么某些 op 会多分配。

- **[OneShotAnalysis.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Dialect/Bufferization/Transforms/OneShotAnalysis.cpp)**
  - **读哪里**：文件开头 286-358 行的 `DEF/READ/WRITE` 顺序表，以及 `annotateConflict`。
  - **学什么**：RaW 冲突如何被规则排除，调试属性如何生成。
  - **和本章的关联**：示例二里的 `CONFL-WRITE` 就来自这里。

- **[Linalg BufferizableOpInterfaceImpl.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Dialect/Linalg/Transforms/BufferizableOpInterfaceImpl.cpp)**
  - **读哪里**：`LinalgOpInterface` 的 `bufferizesToMemoryRead/Write` 和 `bufferize`。
  - **学什么**：一个真实 dialect 如何把 structured op 接进 one-shot。
  - **和本章的关联**：`linalg.generic` 能 bufferize，就是因为这个 external model。

bufferization 把 tensor 程序变成了 memref 程序，但 memref、linalg、scf、arith 仍然不是 LLVM 能吃的形式。下一章把这堆方言逐个降成 `llvm` dialect，并处理 ABI、data layout 和 `index` 位宽。

→ [Ch 21 降到 LLVM dialect](./21-lowering-to-llvm.md)
