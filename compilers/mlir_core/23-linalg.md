# Ch 23 Linalg：structured op

> **目标**：看懂 `linalg` 的“结构化操作”到底结构在哪。给你一个 `linalg.matmul` 或 `linalg.generic`，你能拆出三件套——indexing maps、iterator types、region——并预测 `convert-linalg-to-loops` 会生成什么；也能解释为什么同一个 pass 在 tensor 上会静默跳过。

## 为什么需要这个？

如果只有 `scf.for` + `memref.load/store`，矩阵乘法是一堆循环和地址计算。优化器必须自己反推“这是矩阵乘”“哪个维度是 reduction”“能不能 tiling、向量化”。每写一个新优化，就要重新识别一遍。

Linalg 把这些信息**显式写进 op**：循环有几维、每维是并行还是归约、每个操作数如何被索引、元素级计算长什么样。pass 不需要猜，直接读属性。

一句话：**Linalg 把“循环嵌套”提升成了可分析的数据结构**。代价是你要理解它的三件套，否则 generic 形式看起来像天书。

## 先建立直觉：一个 matmul 的三层读法

```mlir
linalg.matmul ins(%A, %B : memref<4x8xf32>, memref<8x16xf32>)
             outs(%C : memref<4x16xf32>)
```

**第一层：形状。** `A` 是 4×8，`B` 是 8×16，`C` 是 4×16。迭代空间是 4×16×8。

**第二层：迭代。** `(i, j)` 并行，`k` 归约。归约意味着同一个输出元素被多次累加，即 `C[i,j] += A[i,k] * B[k,j]` 的 `k`。

**第三层：元素体。** 给定 `A[i,k]`、`B[k,j]`、`C[i,j]` 三个标量，算出新的 `C[i,j]`。matmul 的默认体是 `C + A * B`。

这三层就是 structured op 的全部。`linalg.matmul` 只是给它们起了名字；`linalg.generic` 允许你把它们原样写出来。

## structured op 的三件套

### indexing maps：循环维到操作数维

indexing map 是一个 `affine_map`，输入是迭代空间，输出是某个操作数的下标。matmul 的三张 map：

```text
A: (i, j, k) -> (i, k)
B: (i, j, k) -> (k, j)
C: (i, j, k) -> (i, j)
```

在迭代点 `(i,j,k)` 读 `A[i,k]`、`B[k,j]`，读写 `C[i,j]`。`j` 不出现在 `A` 的 map 里，说明 `A` 沿 `j` 广播；`i` 不出现在 `B` 的 map 里，说明 `B` 沿 `i` 广播。

map 的结果个数必须等于操作数 rank，输入个数必须等于迭代维数。不满足，verifier 直接报错。

### iterator types：每一维怎么并行

| 类型 | 含义 | 典型例子 |
|---|---|---|
| `parallel` | 各次迭代互不影响，可重排、可并行 | 输出元素 `(i, j)` |
| `reduction` | 多次迭代累加到同一个位置 | 点积的 `k` |
| `window` | 卷积窗口一类带邻域的迭代 | `linalg.conv` |

matmul 的 iterator types 是 `["parallel", "parallel", "reduction"]`。这不是注释，是 pass 做合法性判断的依据：把 `reduction` 维当 `parallel` 并行化，结果就错了。

### region：元素级计算

region 是单块区域，参数是当前迭代点上的标量，结尾用 `linalg.yield` 返回输出元素的新值。matmul 的默认 region：

```mlir
^bb0(%a: f32, %b: f32, %c: f32):
  %0 = arith.mulf %a, %b : f32
  %1 = arith.addf %c, %0 : f32
  linalg.yield %1 : f32
```

`outs` 的操作数也作为 region 参数传入，所以第三个参数是旧的 `C` 值。这就是 destination-passing style（目的地传递风格）：计算不返回新 tensor，而是更新一个目的地。

> 源码依据：`mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td:28-49` 定义所有 structured op 的基类，包含 `SingleBlockImplicitTerminator<"YieldOp">`、`DestinationStyleOpInterface`、`LinalgStructuredInterface`；`LinalgOps.td:30` 是 `linalg.yield`。

## named op 与 generic op

`linalg.matmul`、`linalg.add` 是 **named op**：名字固定、region 由 op 定义生成、custom 语法简短。`linalg.generic` 是 **generic op**：三件套全部写成属性，region 由你提供。

两者语义可以完全等价，区别只在“信息存在哪”：named op 把 maps、iterator types、region 的构造逻辑写进 C++/TableGen；generic op 把 maps、iterator types 写成属性，把 region 写进 IR。

用 `--mlir-print-op-generic` 看 `linalg.matmul`：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\matmul.mlir --mlir-print-op-generic
```

实测输出（关键部分）：

```mlir
#map = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
"linalg.matmul"(%arg0, %arg1, %arg2) <{indexing_maps = [#map, #map1, #map2], operandSegmentSizes = array<i32: 2, 1>}> ({
^bb0(%arg3: f32, %arg4: f32, %arg5: f32):
  %0 = "arith.mulf"(%arg3, %arg4) <{fastmath = #arith.fastmath<none>}> : (f32, f32) -> f32
  %1 = "arith.addf"(%arg5, %0) <{fastmath = #arith.fastmath<none>}> : (f32, f32) -> f32
  "linalg.yield"(%1) : (f32) -> ()
}) : (memref<4x8xf32>, memref<8x16xf32>, memref<4x16xf32>) -> ()
```

两个细节：

1. generic 打印里有 `indexing_maps`，但**没有** `iterator_types`。matmul 的迭代类型由 `MatmulOp::getIteratorTypesArray()` 直接返回（`mlir/lib/Dialect/Linalg/IR/LinalgOps.cpp:3969-3973`）。
2. `indexing_maps` 是 matmul 的可选属性，因为 matmul 允许转置/广播变体（`LinalgStructuredOps.td:724-732`），默认值由 `getDefaultIndexingMaps()` 生成（`LinalgOps.cpp:3945-3953`）。

`linalg.add` 的 generic 打印更短：

```mlir
"linalg.add"(%arg0, %arg1, %arg2) <{operandSegmentSizes = array<i32: 2, 1>}> ({
^bb0(%arg3: f32, %arg4: f32, %arg5: f32):
  %0 = "arith.addf"(%arg3, %arg4) <{fastmath = #arith.fastmath<none>}> : (f32, f32) -> f32
  "linalg.yield"(%0) : (f32) -> ()
})
```

它连 `indexing_maps` 都没存。因为 `linalg.add` 是逐元素加法，maps 和 iterator types 可以按操作数 rank 推导；YAML 定义用的是 rank 多态的 `affine_map<() -> ()>` 和空的 `iterator_types`（`LinalgNamedStructuredOps.yaml:501-549`）。

这正是 `linalg.generic` 的意义：**需要非默认 maps 或 iterator types 时，generic 是唯一能完整表达它们的载体**。

## 底层机制：从 structured op 到循环

`--convert-linalg-to-loops` 的实现：

1. 用 `RewritePattern` 匹配任意 `LinalgOp`；
2. 检查 `hasPureBufferSemantics()`；
3. 按 iterator types 生成 `scf.for` 嵌套；
4. 把 region 内联进最内层循环；
5. 用 indexing maps 生成 `memref.load/store`。

关键在第 2 步。源码 `mlir/lib/Dialect/Linalg/Transforms/Loops.cpp:263-274`：

```cpp
auto linalgOp = dyn_cast<LinalgOp>(op);
if (!isa<LinalgOp>(op) || !linalgOp.hasPureBufferSemantics()) {
  return rewriter.notifyMatchFailure(
      op, "expected linalg op with buffer semantics");
}
```

`hasPureBufferSemantics()` 为假的 op **不会被报错**，只是匹配失败；greedy driver 继续找下一个 pattern，最后原样打印。这就是“静默跳过”的全部机制。

> `Loops.cpp:131-132` 和 `:220` 还有 `assert(linalgOp.hasPureBufferSemantics())`。本机是 Optimized build，assert 关闭；真正拦住 tensor 的是上面那行显式检查。

tensor 版本要先 bufferize 成 memref。`outs` 变成 memref 后，`hasPureBufferSemantics()` 为真，pass 才会动手。

## 示例一：named 与 generic 的来回

准备 `matmul.mlir`：

```mlir
func.func @matmul(%A: memref<4x8xf32>, %B: memref<8x16xf32>, %C: memref<4x16xf32>) {
  linalg.matmul ins(%A, %B : memref<4x8xf32>, memref<8x16xf32>)
               outs(%C : memref<4x16xf32>)
  return
}
```

custom 打印：

```powershell
& "$mlir\mlir-opt.exe" .\matmul.mlir
```

```mlir
module {
  func.func @matmul(%arg0: memref<4x8xf32>, %arg1: memref<8x16xf32>, %arg2: memref<4x16xf32>) {
    linalg.matmul ins(%arg0, %arg1 : memref<4x8xf32>, memref<8x16xf32>) outs(%arg2 : memref<4x16xf32>)
    return
  }
}
```

展开成 generic：

```powershell
& "$mlir\mlir-opt.exe" .\matmul.mlir --pass-pipeline='builtin.module(func.func(linalg-generalize-named-ops))'
```

实测：

```mlir
#map = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
module {
  func.func @matmul(%arg0: memref<4x8xf32>, %arg1: memref<8x16xf32>, %arg2: memref<4x16xf32>) {
    linalg.generic {indexing_maps = [#map, #map1, #map2], iterator_types = ["parallel", "parallel", "reduction"]} ins(%arg0, %arg1 : memref<4x8xf32>, memref<8x16xf32>) outs(%arg2 : memref<4x16xf32>) {
    ^bb0(%in: f32, %in_0: f32, %out: f32):
      %0 = arith.mulf %in, %in_0 : f32
      %1 = arith.addf %out, %0 : f32
      linalg.yield %1 : f32
    }
    return
  }
}
```

反过来，写一个 generic，`linalg-specialize-generic-ops` 能把它认回 named op。实测 `add_generic` 被还原成 `linalg.add`。这说明 named 与 generic 不是两种计算，而是**同一份语义的两种存储方式**。

## 示例二：`--convert-linalg-to-loops` 的前后对照

memref 版本：

```powershell
& "$mlir\mlir-opt.exe" .\matmul.mlir --pass-pipeline='builtin.module(func.func(convert-linalg-to-loops))'
```

实测：

```mlir
module {
  func.func @matmul(%arg0: memref<4x8xf32>, %arg1: memref<8x16xf32>, %arg2: memref<4x16xf32>) {
    %c0 = arith.constant 0 : index
    %c4 = arith.constant 4 : index
    %c1 = arith.constant 1 : index
    %c16 = arith.constant 16 : index
    %c8 = arith.constant 8 : index
    scf.for %arg3 = %c0 to %c4 step %c1 {
      scf.for %arg4 = %c0 to %c16 step %c1 {
        scf.for %arg5 = %c0 to %c8 step %c1 {
          %0 = memref.load %arg0[%arg3, %arg5] : memref<4x8xf32>
          %1 = memref.load %arg1[%arg5, %arg4] : memref<8x16xf32>
          %2 = memref.load %arg2[%arg3, %arg4] : memref<4x16xf32>
          %3 = arith.mulf %0, %1 : f32
          %4 = arith.addf %2, %3 : f32
          memref.store %4, %arg2[%arg3, %arg4] : memref<4x16xf32>
        }
      }
    }
    return
  }
}
```

三个 `scf.for` 对应三个 iterator；循环顺序 `(i, j, k)` 与 map 输入顺序一致；下标来自三张 map；最内层是 region 内联后的 `mulf + addf + store`。

换成 tensor：

```mlir
func.func @matmul_tensor(%A: tensor<4x8xf32>, %B: tensor<8x16xf32>, %C: tensor<4x16xf32>) -> tensor<4x16xf32> {
  %0 = linalg.matmul ins(%A, %B : tensor<4x8xf32>, tensor<8x16xf32>)
                     outs(%C : tensor<4x16xf32>) -> tensor<4x16xf32>
  return %0 : tensor<4x16xf32>
}
```

```powershell
& "$mlir\mlir-opt.exe" .\matmul_tensor.mlir --pass-pipeline='builtin.module(func.func(convert-linalg-to-loops))'
```

实测输出与输入逐字相同，退出码 0；加 `--mlir-print-ir-after-change` 也没有任何 `IR Dump After` 段。原因是 pattern 没有匹配成功，IR 没有 change，自然没有 after-change dump。

bufferize 之后再跑就正常了：

```powershell
& "$mlir\mlir-opt.exe" .\matmul_tensor.mlir `
  --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries=true},func.func(convert-linalg-to-loops))'
```

实测会得到三层 `scf.for`，`outs` 变成带 strided layout 的 memref。因果链是：**tensor → bufferize → buffer 语义 → loops**。

## 示例三：tiling 是什么

Tiling（分块）不是魔法，而是“把大迭代空间切成小块，外层按块迭代，内层在小块上做原计算”。

手写一个沿 `i` 方向按 2 分块的 matmul：

```mlir
func.func @matmul_tiled(%A: memref<4x8xf32>, %B: memref<8x16xf32>, %C: memref<4x16xf32>) {
  %c0 = arith.constant 0 : index
  %c4 = arith.constant 4 : index
  %c2 = arith.constant 2 : index
  scf.for %i = %c0 to %c4 step %c2 {
    %A_tile = memref.subview %A[%i, 0] [2, 8] [1, 1]
      : memref<4x8xf32> to memref<2x8xf32, strided<[8, 1], offset: ?>>
    %C_tile = memref.subview %C[%i, 0] [2, 16] [1, 1]
      : memref<4x16xf32> to memref<2x16xf32, strided<[16, 1], offset: ?>>
    linalg.matmul ins(%A_tile, %B : memref<2x8xf32, strided<[8, 1], offset: ?>>, memref<8x16xf32>)
                 outs(%C_tile : memref<2x16xf32, strided<[16, 1], offset: ?>>)
  }
  return
}
```

跑 `convert-linalg-to-loops`，实测内层循环变成 `2 × 16 × 8`：

```mlir
scf.for %arg3 = %c0 to %c4 step %c2 {
  %subview = memref.subview %arg0[%arg3, 0] [2, 8] [1, 1] : memref<4x8xf32> to memref<2x8xf32, strided<[8, 1], offset: ?>>
  %subview_0 = memref.subview %arg2[%arg3, 0] [2, 16] [1, 1] : memref<4x16xf32> to memref<2x16xf32, strided<[16, 1], offset: ?>>
  scf.for %arg4 = %c0 to %c2 step %c1 {
    scf.for %arg5 = %c0 to %c16 step %c1 {
      scf.for %arg6 = %c0 to %c8 step %c1 {
        %0 = memref.load %subview[%arg4, %arg6] : memref<2x8xf32, strided<[8, 1], offset: ?>>
        %1 = memref.load %arg1[%arg6, %arg5] : memref<8x16xf32>
        %2 = memref.load %subview_0[%arg4, %arg5] : memref<2x16xf32, strided<[16, 1], offset: ?>>
        %3 = arith.mulf %0, %1 : f32
        %4 = arith.addf %2, %3 : f32
        memref.store %4, %subview_0[%arg4, %arg5] : memref<2x16xf32, strided<[16, 1], offset: ?>>
      }
    }
  }
}
```

分块没有改变计算，只改变迭代顺序和访问粒度。`memref.subview` 让内层 linalg op 看到 `2x8` / `2x16`，于是生成更小的循环。真实 tiling 由 `TilingInterface` 驱动，入口在 `mlir/lib/Dialect/Linalg/Transforms/Tiling.cpp:816`（`tileLinalgOp`），核心是 `:794` 的 `tileLinalgOpImpl`；归约维分块走 `:588` 的 `tileReductionUsingForall`。

## 对比与取舍

| 维度 | named op | generic op |
|---|---|---|
| 可读性 | 高，custom 语法短 | 低，属性多 |
| 表达范围 | 固定的常见计算 | 任意 maps + iterator types |
| 优化识别 | 可按名字匹配 | 按结构匹配 |
| 适合 | 稳定的库级计算 | 融合、实验、非标准布局 |

Linalg vs Affine/SCF：需要“形状 + 迭代语义”让高层 pass 分析，选 Linalg；需要手写循环、精细控制边界和步长，选 `scf`/`affine`；已经 buffer 化、只差地址计算，直接用 `memref` + `scf`。Linalg 的价值在于**保留结构**；一旦降成循环，很多优化只能靠模式识别重新发现。

## 踩雷集锦

1. **在 tensor 上跑 `convert-linalg-to-loops`，以为工具没生效。** 实测退出码 0、IR 不变。先 bufferize，或改用支持 tensor 的 tiling/vectorization 路径。
2. **把 `iterator_types` 的 `reduction` 写成 `parallel`。** 并行化/重排会算错。归约维必须诚实标注。
3. **indexing map 的结果个数与操作数 rank 不一致。** 直接报错。
4. **以为 `linalg.matmul` 的 generic 打印一定带 `iterator_types`。** 实测不带；要看完整 generic，跑 `linalg-generalize-named-ops`。
5. **以为 `linalg.add` 的 generic 形式必须写 maps。** 实测只有 `operandSegmentSizes`。
6. **tiling 时只切输出、不切输入。** 所有相关操作数都要用一致的 subview，否则 verifier 拒绝。

## 进阶：再往深一层

- **`TilingInterface`**：tiling 通过接口查询迭代域、生成 tile，不是硬编码在 pass 里（Ch 8）。
- **destination-passing style**：`outs` 既是输入也是输出，这是 bufferization 能原地复用 buffer 的前提（Ch 20）。
- **fusion**：elementwise 生产者可以折进消费者的 region，入口在 `mlir/lib/Dialect/Linalg/Transforms/ElementwiseOpFusion.cpp`。
- **vectorization**：把 tile 内的 linalg op 变成 `vector.transfer_read/contract/write`，这是 Ch 25 的桥。
- **transform dialect**：用 IR 描述 tile/vectorize 调度，而不是写死 pass pipeline（Ch 26）。

## 动手练习

### 练习 1：generic 与 named 来回

**任务规格**：对 `matmul.mlir` 先跑 `linalg-generalize-named-ops`，再对结果跑 `linalg-specialize-generic-ops`。

**测试用例**：第一次输出出现 `linalg.generic`，第二次回到 `linalg.matmul`，函数签名不变。

**分步骤**：

1. 跑 generalize，把输出存成 `matmul_generic.mlir`；
2. 对落盘文件跑 specialize；
3. 对比输出与原始 custom 打印。

<details>
<summary>参考解答</summary>

```powershell
$gen = & "$mlir\mlir-opt.exe" .\matmul.mlir `
  --pass-pipeline='builtin.module(func.func(linalg-generalize-named-ops))'
[IO.File]::WriteAllText("$PWD\matmul_generic.mlir", ($gen -join "`n"), (New-Object System.Text.UTF8Encoding($false)))
& "$mlir\mlir-opt.exe" .\matmul_generic.mlir `
  --pass-pipeline='builtin.module(func.func(linalg-specialize-generic-ops))'
```

没回到 named 时，检查 `iterator_types` 是否为 `["parallel","parallel","reduction"]`，以及 maps 是否恰好是 matmul 的默认三张。

</details>

**常见卡点**：PowerShell 5.1 的 `>` 默认写 UTF-16，`mlir-opt` 读不了；上面的 `WriteAllText` 明确写 UTF-8 无 BOM。

**自我检核**：generic 形式里哪三样东西是 named 形式隐藏的？

### 练习 2：让 tensor 版 matmul 真正降成循环

**任务规格**：先证明单 pass 在 tensor 上静默跳过，再 bufferize 后降成三层循环。

**测试用例**：第一次输出仍有 `linalg.matmul` 且退出码 0；第二次有三个 `scf.for`，没有 `linalg.matmul`。

**分步骤**：

1. 跑单 pass，记录 IR 与退出码；
2. 加 `--mlir-print-ir-after-change`，确认没有 dump；
3. 换 `one-shot-bufferize{bufferize-function-boundaries=true}` + `convert-linalg-to-loops`；
4. 观察函数签名从 tensor 变成 memref。

<details>
<summary>参考解答</summary>

```powershell
& "$mlir\mlir-opt.exe" .\matmul_tensor.mlir `
  --pass-pipeline='builtin.module(func.func(convert-linalg-to-loops))'
& "$mlir\mlir-opt.exe" .\matmul_tensor.mlir `
  --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries=true},func.func(convert-linalg-to-loops))'
```

第二次会得到 `memref<4x8xf32, strided<[?, ?], offset: ?>>` 等类型，以及三层 `scf.for`。

</details>

**常见卡点**：`one-shot-bufferize` 是 module pass，不能塞进 `func.func(...)`；放错层级会报 “Can't add pass ... restricted to 'builtin.module' on a PassManager intended to run on 'func.func'”。

**自我检核**：`Loops.cpp` 里哪一行导致 tensor 被跳过？

### 练习 3：把 tile 从 2 改成 1

**任务规格**：把 `matmul_tiled.mlir` 的 tile size 改成 1，重跑 `convert-linalg-to-loops`。

**测试用例**：外层循环变成 `0 to 4 step 1`；内层变成 `1 × 16 × 8`；两个 subview 的 size 改为 `[1, 8]` / `[1, 16]`。

**分步骤**：

1. 改 `%c2 = arith.constant 1 : index`；
2. 同步改两个 subview 的 size 为 `[1, 8]`、`[1, 16]`，结果类型改成 `memref<1x8xf32, ...>` / `memref<1x16xf32, ...>`；
3. 重跑并对比。

<details>
<summary>参考解答</summary>

tile size 必须同时改三处：常量、subview size、subview 结果类型。只改常量会报类型不匹配。改对后最内层循环上界是 1，实际退化成一次迭代。

</details>

**常见卡点**：忘记改 subview 的结果类型。

**自我检核**：tile size 变小一定更快吗？为什么？

### 练习 4：用 map 表达转置 matmul

**任务规格**：写一个 generic op 计算 `C[i, j] = sum_k A[k, i] * B[k, j]`，其中 `A: memref<8x4xf32>`、`B: memref<8x16xf32>`、`C: memref<4x16xf32>`。

**测试用例**：verifier 通过；打印出 `affine_map<(d0, d1, d2) -> (d2, d0)>`；降级后 A 的下标是 `[%arg5, %arg3]`（k, i）。

**分步骤**：

1. 以 `(i, j, k) = (d0, d1, d2)` 写出三张 map；
2. 跑 `mlir-opt`，确认不报错；
3. 跑 `convert-linalg-to-loops`，检查 `memref.load` 的下标顺序。

<details>
<summary>参考解答</summary>

```mlir
#m0 = affine_map<(d0, d1, d2) -> (d2, d0)>
#m1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#m2 = affine_map<(d0, d1, d2) -> (d0, d1)>
linalg.generic {indexing_maps = [#m0, #m1, #m2], iterator_types = ["parallel", "parallel", "reduction"]}
    ins(%A, %B : memref<8x4xf32>, memref<8x16xf32>) outs(%C : memref<4x16xf32>) {
^bb0(%a: f32, %b: f32, %c: f32):
  %0 = arith.mulf %a, %b : f32
  %1 = arith.addf %c, %0 : f32
  linalg.yield %1 : f32
}
```

降级后 A 的第一维是 k（`%arg5`）、第二维是 i（`%arg3`）：`memref.load %arg0[%arg5, %arg3]`。

</details>

**常见卡点**：把 A 的 map 写成 `(d0, d2)`。`8x4` 对不上迭代空间，verifier 会报 map 结果与操作数 rank 不匹配。

**自我检核**：为什么 `j` 不出现在 A 的 map 里？

## 本章重点整理

- structured op = indexing maps + iterator types + region + destination。
- named op 与 generic op 语义等价，差别在信息存储位置。
- `linalg.matmul` 的 generic 打印保留 `indexing_maps`，但不打印 `iterator_types`；`linalg.add` 两者都可能不打印。
- `convert-linalg-to-loops` 只处理纯 buffer 语义；tensor 上匹配失败，静默跳过。
- 静默跳过的证据是“IR 不变 + 退出码 0 + 没有 after-change dump”。
- bufferize 是 tensor 路径进入 loop lowering 的前置条件。
- tiling 的本质是“外层块循环 + 内层原 op”，不改变计算语义。

## 自我检核

- [ ] 我能不看教材，写出 matmul 的三张 indexing map
- [ ] 我能解释 `reduction` 维为什么不能当 `parallel` 并行
- [ ] 我能说出 named 与 generic 各自适合什么场景
- [ ] 我能预测 `convert-linalg-to-loops` 生成的循环顺序
- [ ] 我能解释 tensor 版为什么静默跳过，并给出两种修复路径
- [ ] 我能手写一个 tile size 为 2 的 matmul 分块
- [ ] 我知道 tiling 的入口在 `Tiling.cpp` 的哪个函数

## 延伸阅读

- **[Linalg Dialect 文档](https://mlir.llvm.org/docs/Dialects/Linalg/)**
  - **读哪里**：Structured ops、indexing maps、iterator types 三节。
  - **学什么**：官方对三件套的定义与约束。
  - **和本章的关联**：示例一、二的语法依据。

- **[Linalg 源码：LinalgStructuredOps.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td)**
  - **读哪里**：`LinalgStructuredBase_Op`、`GenericOp`、`MatmulOp`。
  - **学什么**：三件套在 ODS 里如何声明。
  - **和本章的关联**：解释 generic 打印为什么长那样。

- **[Linalg 源码：Loops.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Dialect/Linalg/Transforms/Loops.cpp)**
  - **读哪里**：`LinalgRewritePattern::matchAndRewrite` 与 `linalgOpToLoopsImpl`。
  - **学什么**：structured op 如何变成 `scf.for`。
  - **和本章的关联**：静默跳过的直接证据。

- **[Linalg 源码：Tiling.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Dialect/Linalg/Transforms/Tiling.cpp)**
  - **读哪里**：`tileLinalgOpImpl`、`tileLinalgOp`、`tileReductionUsingForall`。
  - **学什么**：真实 tiling 如何生成循环与 subview。
  - **和本章的关联**：示例三的工业版。

- **[Bufferization 文档](https://mlir.llvm.org/docs/Bufferization/)**
  - **读哪里**：One-Shot Bufferize 的约束与 `bufferize-function-boundaries`。
  - **学什么**：tensor 如何进入 buffer 世界。
  - **和本章的关联**：练习 2 的前置知识。

---

structured op 讲完了，但它只描述“怎么算”，不描述“循环边界能不能证明”。下一章换一个视角：用 affine 方言把循环和下标写成可被数学分析的形式。

→ [Ch 24 Affine：循环与依赖](./24-affine.md)
