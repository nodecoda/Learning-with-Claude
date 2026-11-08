# Ch 24 — Linalg / Tensor / Vector 三方边界

> **目标**：能把任意一个 `linalg.*` op 拆成「indexing map + iterator type + destination」三件事，判断它为什么在 tensor 上跑不动、为什么 bufferize 之后才降级、tiling/vectorization 分别改了哪一层；并能在 `lib/Dialect/Linalg`（一年 183 次提交）里找到对应的实现。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`，assertions ON。
>
> **前置**：Ch 19（conversion/legality）、Ch 22（bufferization 概念）；本章只讲 Linalg/Vector 自身的模型，bufferization 的实现细节留给 Part 4。

---

## 24.1 为什么维护者必须先分清三个类型

`lib/Dialect/Linalg` 在最近 12 个月被改了 **183 次**，`lib/Dialect/Vector` **170 次**——这两个目录是 MLIR 里最活跃的“真实子系统”。但它们活跃的原因不是“方言大”，而是因为**它们坐在三条语义边界的交点上**：

```text
tensor<T>   ── 值语义、不可变、无地址     ← 前端/高层融合喜欢它
memref<T>   ── 地址 + layout + 可变        ← 后端/循环优化需要它
vector<T>   ── 固定形状、寄存器语义        ← 硬件向量化需要它
```

维护者最常收到的 bug 报告，几乎都能归到“把三者当成一回事”：

- “`--convert-linalg-to-loops` 对 `linalg.matmul` 什么都没做。” → 因为它跑在 **tensor** 上，而没有 buffer 就没有可写的 destination（24.5 实测）。
- “tile 之后多出来一堆 `memref.subview`，我的 pass 认不出来了。” → 因为 tiling 是 **memref 层**的变换，subview 是必然产物（24.6 实测）。
- “vectorize 之后 `linalg.matmul` 消失了，但我没看到 `llvm` 的 `fmul`。” → 因为 vectorization 只到 `vector.multi_reduction`，还要再降一层（24.7 实测）。
- “动态 shape 下结果不对，但没有 verifier 报错。” → 因为 dynamic shape 的边界检查不在 verifier 里（24.9 实测）。

本章的主线因此是三段：**先讲清三个类型各自的承诺 → 再讲 Linalg op 的三个不变量 → 最后看 tiling / vectorization 各自动了哪一层**。

## 24.2 先建立直觉：一个 op 的三层投影

```text
                      linalg.matmul ins(%A, %B) outs(%C)
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
   [1] indexing maps        [2] iterator types       [3] destination
   #map  = (d0,d1,d2)->(d0,d2)   ["parallel",            outs(%C)
   #map1 = (d0,d1,d2)->(d2,d1)    "parallel",            ↑
   #map2 = (d0,d1,d2)->(d0,d1)    "reduction"]      tensor: 只是"形状+初值"
        │                        │                  memref: 真正的可写内存
        ▼                        ▼                        ▼
   每个 operand 如何          哪些维度可以并行        结果写到哪里
   从 iteration space 取坐标   哪些必须串行累加        决定能否直接降级
        │                        │                        │
        └────────────┬───────────┴────────────┬───────────┘
                     ▼                        ▼
              tiling / fusion            vectorization
          （切 iteration space，       （把 iteration space 的
            产出 subview + 小 op）       一块映射成 vector op）
                     │                        │
                     ▼                        ▼
              scf.for + subview        vector.transfer_read
              + 原 op（缩小版）         + vector.multi_reduction
```

把这幅图记住，本章后面每个实验都能对上其中一条边。

三个类型的关键差异（维护时必须先问“这个 op 现在在哪一层”）：

| | `tensor` | `memref` | `vector` |
|---|---|---|---|
| 语义 | SSA 值，不可变 | 内存引用，可变 | SSA 值，固定形状 |
| 有地址吗 | 没有（`tensor.empty` 只是形状） | 有（base ptr + offset + strides） | 没有（但可 legalize 成寄存器/栈） |
| Linalg 能直接降级吗 | 不能，必须先 bufferize | 能（`--convert-linalg-to-loops`） | 不适用 |
| 典型生产者 | 前端、`tensor.pad`、`tensor.extract_slice` | `memref.alloc`、bufferization | vectorization、`vector.transfer_*` |
| 典型消费者 | bufferization | `--convert-to-llvm` | `--convert-vector-to-scf` / `-to-llvm` |

`mlir/docs/Dialects/Linalg/_index.md:45-57` 明确写了这个双栖设计：

```text
These ops can have ***either tensor or buffer*** as both input and output
operands. Output tensors operands serve the purpose of providing a unifying
abstraction and give a shape to the results.
```

## 24.3 Linalg op 的三个不变量

### 24.3.1 indexing map：把 iteration space 投影到 operand

`linalg.matmul` 的 iteration space 是 `(d0, d1, d2)` = `(M, N, K)`。三个 operand 各自一张 map：

```text
A: (d0,d1,d2) -> (d0,d2)    # 取 M,K
B: (d0,d1,d2) -> (d2,d1)    # 取 K,N
C: (d0,d1,d2) -> (d0,d1)    # 取 M,N
```

第一次看 `linalg.matmul` 的通用形式，用 `--linalg-generalize-named-ops` 把它摊开：

```bash
cd /tmp/mlir_p5
cat > matmul.mlir <<'EOF'
func.func @matmul(%A: tensor<2x3xf32>, %B: tensor<3x4xf32>) -> tensor<2x4xf32> {
  %C = tensor.empty() : tensor<2x4xf32>
  %0 = linalg.matmul ins(%A, %B : tensor<2x3xf32>, tensor<3x4xf32>)
                    outs(%C : tensor<2x4xf32>) -> tensor<2x4xf32>
  return %0 : tensor<2x4xf32>
}
EOF
$T/bin/mlir-opt matmul.mlir --linalg-generalize-named-ops
```

真实输出（节选）：

```text
#map = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
module {
  func.func @matmul(%arg0: tensor<2x3xf32>, %arg1: tensor<3x4xf32>) -> tensor<2x4xf32> {
    %0 = tensor.empty() : tensor<2x4xf32>
    %1 = linalg.generic {indexing_maps = [#map, #map1, #map2], iterator_types = ["parallel", "parallel", "reduction"]} ins(%arg0, %arg1 : tensor<2x3xf32>, tensor<3x4xf32>) outs(%0 : tensor<2x4xf32>) {
    ^bb0(%in: f32, %in_0: f32, %out: f32):
      %2 = arith.mulf %in, %in_0 : f32
      %3 = arith.addf %out, %2 : f32
      linalg.yield %3 : f32
    } -> tensor<2x4xf32>
    return %1 : tensor<2x4xf32>
  }
}
```

这就是 `linalg.matmul` 的**全部信息**：三张 map、三个 iterator type、一个 region（标量计算）。`linalg.matmul` 只是这份 generic 的“别名 + 额外接口方法 + verifier 特化”。

维护者视角：当你看到“某个 named op 的行为和 generic 不一致”时，第一步永远是 `--linalg-generalize-named-ops` 摊开对比。如果 generic 形式也对，问题在 named op 的额外方法（`LinalgInterfaces.td` 里的 `isRowMajorMatmul` 之类）；如果 generic 也错，问题在 region 或 map。

### 24.3.2 iterator type：并行/归约的语义承诺

`iterator_types` 只有三种值（`parallel` / `reduction` / `window`），但它决定两件大事：

1. **能否并行化**：`--convert-linalg-to-parallel-loops` 只把 parallel 维度变成 `scf.parallel`，reduction 维度留成 `scf.for`。
2. **数据竞争是否算 bug**：reduction 维度上的 `memref.load`/`store` 是“合法的串行累加”，parallel 维度上的同址读写就是 race。

源码里的检查在 `include/mlir/Dialect/Linalg/IR/LinalgInterfaces.td:227` 起的 `LinalgStructuredInterface`，它继承 `DestinationStyleOpInterface` 与 `IndexingMapOpInterface`——也就是说，“有 destination”和“有可分析的 index map”是**接口层面的承诺**，不是命名约定。

### 24.3.3 destination passing style（DPS）：outs 不是输出参数，是“初值 + 形状”

这是从 tensor 世界进 memref 世界最容易搞错的一点：

- tensor 上的 `outs(%C)`：`%C` 提供**形状**和**初值**（reduction 的起点）。`tensor.empty` 表示“形状对，值随便”。
- memref 上的 `outs(%C)`：`%C` 是**真正被写的内存**。`linalg.matmul` 会 `load`/`store` 它。

`include/mlir/Interfaces/DestinationStyleOpInterface.td:14` 定义的接口只有四个核心方法（`getDpsInitsMutable`、`getDpsInits`、`getNumDpsInits`、`getDpsInitOperand`），它们回答“哪些 operand 是 init”，而不回答“init 是 tensor 还是 memref”——这正是双栖的关键。

## 24.4 第一个分水岭：tensor 上的 Linalg 不能直接降级

拿 24.3.1 的 tensor 版 `matmul.mlir` 直接降级：

```bash
$T/bin/mlir-opt matmul.mlir --convert-linalg-to-loops
```

真实输出（注意：**完全没变**）：

```text
module {
  func.func @matmul(...) -> tensor<2x4xf32> {
    %0 = tensor.empty() : tensor<2x4xf32>
    %1 = linalg.matmul ins(%arg0, %arg1 : ...) outs(%0 : ...) -> tensor<2x4xf32>
    return %1 : tensor<2x4xf32>
  }
}
```

为什么不报错也不降级？因为 `--convert-linalg-to-loops` 的 pattern 只匹配 **buffer 语义**的 Linalg op（`Loops.cpp:297` 的 `lowerLinalgToLoopsImpl` 生成的是 `memref.load/store`）。tensor 版没有可写的内存，pattern 直接不匹配，pass 静默成功。

这就是“静默 no-op”类 bug 的典型来源：**pass 成功 ≠ 做了事**。维护者判断方法：

```bash
$T/bin/mlir-opt matmul.mlir --convert-linalg-to-loops | grep -c 'scf.for'
0
```

先 bufferize 再降级，就正常了：

```bash
$T/bin/mlir-opt matmul.mlir -one-shot-bufferize='bufferize-function-boundaries' --convert-linalg-to-loops
```

真实输出（节选）：

```text
module {
  func.func @matmul(%arg0: memref<2x3xf32, strided<[?, ?], offset: ?>>, %arg1: memref<3x4xf32, strided<[?, ?], offset: ?>>) -> memref<2x4xf32> {
    %alloc = memref.alloc() alignment = 64 : memref<2x4xf32>
    scf.for %arg2 = %c0 to %c2 step %c1 {
      scf.for %arg3 = %c0 to %c4 step %c1 {
        scf.for %arg4 = %c0 to %c3 step %c1 {
          %0 = memref.load %arg0[%arg2, %arg4] : memref<2x3xf32, strided<[?, ?], offset: ?>>
          %1 = memref.load %arg1[%arg4, %arg3] : memref<3x4xf32, strided<[?, ?], offset: ?>>
          %2 = memref.load %alloc[%arg2, %arg3] : memref<2x4xf32>
          ...
          memref.store %4, %alloc[%arg2, %arg3] : memref<2x4xf32>
```

两个细节值得记住：

1. bufferize 之后函数签名变成了 `memref<2x3xf32, strided<[?, ?], offset: ?>>`——不再是紧凑 layout，因为 one-shot bufferize 允许函数边界返回带 offset 的 view。
2. `%alloc` 带 `alignment = 64`，这是 bufferization 给 `tensor.empty` 分配时加的默认对齐。

## 24.5 第二层：三种 loop 形态，只差一个模板参数

同一个 memref 版 `linalg.matmul`，三个 pass 给出三种循环：

```bash
cat > matmul_mem.mlir <<'EOF'
func.func @matmul(%A: memref<2x3xf32>, %B: memref<3x4xf32>, %C: memref<2x4xf32>) {
  linalg.matmul ins(%A, %B : memref<2x3xf32>, memref<3x4xf32>)
               outs(%C : memref<2x4xf32>)
  return
}
EOF
$T/bin/mlir-opt matmul_mem.mlir --convert-linalg-to-loops
$T/bin/mlir-opt matmul_mem.mlir --convert-linalg-to-affine-loops
$T/bin/mlir-opt matmul_mem.mlir --convert-linalg-to-parallel-loops
```

`--convert-linalg-to-loops` 真实输出：

```text
module {
  func.func @matmul(%arg0: memref<2x3xf32>, %arg1: memref<3x4xf32>, %arg2: memref<2x4xf32>) {
    %c0 = arith.constant 0 : index
    %c2 = arith.constant 2 : index
    %c1 = arith.constant 1 : index
    %c4 = arith.constant 4 : index
    %c3 = arith.constant 3 : index
    scf.for %arg3 = %c0 to %c2 step %c1 {
      scf.for %arg4 = %c0 to %c4 step %c1 {
        scf.for %arg5 = %c0 to %c3 step %c1 {
          %0 = memref.load %arg0[%arg3, %arg5] : memref<2x3xf32>
          %1 = memref.load %arg1[%arg5, %arg4] : memref<3x4xf32>
          %2 = memref.load %arg2[%arg3, %arg4] : memref<2x4xf32>
          %3 = arith.mulf %0, %1 : f32
          %4 = arith.addf %2, %3 : f32
          memref.store %4, %arg2[%arg3, %arg4] : memref<2x4xf32>
        }
      }
    }
    return
  }
}
```

`--convert-linalg-to-affine-loops` 换成 `affine.for` + `affine.load/store`，边界直接是常量，不再需要 `arith.constant`：

```text
    affine.for %arg3 = 0 to 2 {
      affine.for %arg4 = 0 to 4 {
        affine.for %arg5 = 0 to 3 {
          %0 = affine.load %arg0[%arg3, %arg5] : memref<2x3xf32>
          ...
          affine.store %4, %arg2[%arg3, %arg4] : memref<2x4xf32>
```

`--convert-linalg-to-parallel-loops` 只把两个 parallel 维度提升成 `scf.parallel`，reduction 维度仍是 `scf.for`：

```text
    scf.parallel (%arg3, %arg4) = (%c0, %c0) to (%c2, %c4) step (%c1, %c1) {
      scf.for %arg5 = %c0 to %c3 step %c1 { ... }
      scf.reduce 
    }
```

注意 `scf.reduce` 是空的——因为没有任何跨 iteration 的归约值需要合并（累加发生在 memref 里）。如果这个 op 用 `iter_args` 做累加，`scf.reduce` 就会带上组合子。

源码上，这三个 pass 共用一个实现（`Loops.cpp:297`）：

```cpp
static void lowerLinalgToLoopsImpl(Operation *enclosingOp) {
```

然后 `Loops.cpp:317/328/337` 分别用 `affine::AffineForOp`、`scf::ForOp`、`scf::ParallelOp` 实例化。**维护者要改循环生成，只改一处；改“哪种循环”只是换模板参数。**

## 24.6 tiling：pass 已经不在，改成 transform op

老教程里的 `-linalg-tile` 在当前 trunk 里**不存在**了。这不是笔误，是刻意的架构迁移：tiling 需要“切哪个 op、切几刀、切完怎么用”这些策略，放在 pass option 里表达不了，于是搬到 Transform dialect（Ch 28）。

先确认：

```bash
$T/bin/mlir-opt --help 2>&1 | grep -E '^      --[a-z-]*tile'
```

真实输出（只有 affine tiling 和 test pass，没有 `linalg-tile`）：

```text
      --affine-loop-tile                                     -   Tile affine loop nests
      --test-linalg-transform-patterns                       -   Test Linalg transformation patterns by applying them greedily.
```

（`--help` 里 `test-tile-pattern` 这个**选项名**和帮助文本对不上，是真实的 CLI 不一致；实测 `--test-linalg-transform-patterns='test-tile-pattern tile-sizes=1,2'` 会报 `no such option test-tile-pattern`。）

正确的 tiling 写法是 transform schedule：

```bash
cat > tile.mlir <<'EOF'
func.func @matmul(%A: memref<8x8xf32>, %B: memref<8x8xf32>, %C: memref<8x8xf32>) {
  linalg.matmul ins(%A, %B : memref<8x8xf32>, memref<8x8xf32>)
               outs(%C : memref<8x8xf32>)
  return
}

module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%arg0: !transform.any_op {transform.readonly}) {
    %mm = transform.structured.match ops{["linalg.matmul"]} in %arg0 : (!transform.any_op) -> !transform.any_op
    %tiled, %loops:3 = transform.structured.tile_using_for %mm tile_sizes [2, 2, 2]
      : (!transform.any_op) -> (!transform.any_op, !transform.any_op, !transform.any_op, !transform.any_op)
    transform.yield
  }
}
EOF
$T/bin/mlir-opt tile.mlir --transform-interpreter
```

真实输出（节选）：

```text
    scf.for %arg3 = %c0 to %c8 step %c2 {
      scf.for %arg4 = %c0_0 to %c8_2 step %c2_4 {
        scf.for %arg5 = %c0_1 to %c8_3 step %c2_5 {
          %subview = memref.subview %arg0[%arg3, %arg5] [2, 2] [1, 1] : memref<8x8xf32> to memref<2x2xf32, strided<[8, 1], offset: ?>>
          %subview_7 = memref.subview %arg2[%arg3, %arg4] [2, 2] [1, 1] : ...
          linalg.matmul ins(%subview, %subview_6 : ...) outs(%subview_7 : ...)
```

tiling 的产物有三样，缺一不可：

1. **外层 `scf.for`**：iteration space 被切成 4×4×4 个 trip。
2. **`memref.subview`**：每刀切出一块“非紧凑”的 view（`strided<[8, 1], offset: ?>` 里 stride 8 来自原 buffer 的行宽）。
3. **缩小后的原 op**：`linalg.matmul` 从 8×8 变成 2×2，但 indexing map 完全没变。

**这就是为什么“tile 之后我的 pattern 认不出 op 了”**：op 还在，但它 operands 的类型从 `memref<8x8xf32>` 变成了 `memref<2x2xf32, strided<[8,1], offset:?>>`。写 pattern 时要么用 `memref` 的 cast，要么用 interface 而不是具体类型。

源码落点：`lib/Dialect/Linalg/Transforms/Tiling.cpp:433` 的 `tileLinalgOpImpl` 是统一实现，`:794` 的静态版本负责把 `OpFoldResult` 归一化，`:816` 的 `tileLinalgOp` 再按 loop 类型分派（`:820` scf.for，`:822` scf.parallel）。

## 24.7 vectorization：从 iteration space 到 vector op

vectorization 不是“把 load 换成 vector.load”，而是**把一整块 iteration space 折叠成 vector 的维度**。用同一个 schedule 但换 op：

```bash
cat > vecsched.mlir <<'EOF'
func.func @matmul(%A: memref<2x3xf32>, %B: memref<3x4xf32>, %C: memref<2x4xf32>) {
  linalg.matmul ins(%A, %B : memref<2x3xf32>, memref<3x4xf32>)
               outs(%C : memref<2x4xf32>)
  return
}
module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%arg0: !transform.any_op {transform.readonly}) {
    %mm = transform.structured.match ops{["linalg.matmul"]} in %arg0 : (!transform.any_op) -> !transform.any_op
    transform.structured.vectorize %mm vector_sizes [2, 4, 3] : !transform.any_op
    transform.yield
  }
}
EOF
$T/bin/mlir-opt vecsched.mlir --transform-interpreter
```

真实输出（节选）：

```text
#map = affine_map<(d0, d1) -> (d0, 0, d1)>
#map1 = affine_map<(d0, d1) -> (0, d1, d0)>
module {
  func.func @matmul(%arg0: memref<2x3xf32>, %arg1: memref<3x4xf32>, %arg2: memref<2x4xf32>) {
    %0 = ub.poison : f32
    %1 = vector.transfer_read %arg0[%c0, %c0], %0 {permutation_map = #map} : memref<2x3xf32>, vector<2x4x3xf32>
    %3 = vector.transfer_read %arg1[%c0, %c0], %2 {permutation_map = #map1} : memref<3x4xf32>, vector<2x4x3xf32>
    %5 = vector.transfer_read %arg2[%c0, %c0], %4 : memref<2x4xf32>, vector<2x4xf32>
    %6 = arith.mulf %1, %3 : vector<2x4x3xf32>
    %7 = vector.multi_reduction <add>, %6, %5 [2] : vector<2x4x3xf32> to vector<2x4xf32>
    vector.transfer_write %7, %arg2[%c0_0, %c0_0] : vector<2x4xf32>, memref<2x4xf32>
```

三个关键点：

1. **`permutation_map`**：`vector<2x4x3>` 的维度顺序是 `(M, N, K)`，而 `%A` 是 `(M, K)`。map `(d0,d1) -> (d0, 0, d1)` 把 K 放在第 3 维、N 维广播成常量 0——这就是“vector 形状与 memref 形状不对齐”时的标准解法。
2. **`ub.poison` 作为 padding**：`transfer_read` 的第二个参数是越界时填的值。这里用 poison 而不是 0，因为 reduction 里 poison 参与乘加后整条链变成 poison，能暴露“越界读了垃圾”的 bug，而不是静默算错。
3. **`vector.multi_reduction <add>, ..., [2]`**：reduction 维度（K，第 2 维）在这里被显式折叠，而不是留在循环里。

也就是说，vectorization 把 24.2 图里的“tiling”那条边换成了“整个 iteration space 一次算完”。代价是：**只对静态形状（或已知上界）可行**，动态 shape 需要 mask 或 scf 循环回退。

实现落点在 `lib/Dialect/Linalg/Transforms/Vectorization.cpp:1482`（`vectorizeAsLinalgGeneric`，模板化地把任意 structured op 变成 generic 再向量化）与 `:2551`（`vectorizeOpPrecondition`，判断“能不能向量化”——形状、类型、region 是否可映射）。`:2664` 附近有一条重要的注释：

```text
            // 'vectorizeAsLinalgGeneric'. This is highly problematic: if we
```

维护者在改 vectorization 时最常碰的就是这里：precondition 放太松 → 生成非法 vector op；放太紧 → 该向量化的没向量化，性能回归。

## 24.8 vector 也要分步降：2D transfer 先变循环

`--convert-vector-to-scf` 不是“把 vector 全变成循环”，它只做**渐进降级的第一步**：高维/非常规 transfer 变成低维 transfer + 循环。实测一个 2D transfer：

```bash
cat > vec2.mlir <<'EOF'
func.func @copy2d(%A: memref<8x16xf32>, %B: memref<8x16xf32>) {
  %c0 = arith.constant 0 : index
  %cst = arith.constant 0.0 : f32
  %v = vector.transfer_read %A[%c0, %c0], %cst {in_bounds = [true, true]}
        : memref<8x16xf32>, vector<2x4xf32>
  vector.transfer_write %v, %B[%c0, %c0] {in_bounds = [true, true]}
        : vector<2x4xf32>, memref<8x16xf32>
  return
}
EOF
$T/bin/mlir-opt vec2.mlir --convert-vector-to-scf
```

真实输出：

```text
module {
  func.func @copy2d(%arg0: memref<8x16xf32>, %arg1: memref<8x16xf32>) {
    ...
    %cst = arith.constant 0.000000e+00 : f32
    %c0 = arith.constant 0 : index
    %alloca = memref.alloca() : memref<vector<2x4xf32>>
    %alloca_0 = memref.alloca() : memref<vector<2x4xf32>>
    %0 = vector.type_cast %alloca : memref<vector<2x4xf32>> to memref<2xvector<4xf32>>
    scf.for %arg2 = %c0 to %c2 step %c1 {
      %3 = vector.transfer_read %arg0[%arg2, %c0], %cst {in_bounds = [true]} : memref<8x16xf32>, vector<4xf32>
      memref.store %3, %0[%arg2] : memref<2xvector<4xf32>>
    }
    ...
    scf.for %arg2 = %c0 to %c2 step %c1 {
      %3 = memref.load %2[%arg2] : memref<2xvector<4xf32>>
      vector.transfer_write %3, %arg1[%arg2, %c0] {in_bounds = [true]} : vector<4xf32>, memref<8x16xf32>
    }
```

注意三件事，都是维护时会被问到的：

1. **`memref.alloca` + `vector.type_cast`**：把 `vector<2x4xf32>` 看成 `memref<2xvector<4xf32>>`，才能对第 0 维做循环。这是“vector 没有可寻址的 lane”这一语义的直接后果。
2. **`in_bounds = [true, true]` 变成 `[true]`**：降维之后只剩下内层那一维需要说明。
3. **1D 满宽 transfer 原地不动**：如果你试的是 `vector<4xf32>` 读 `memref<4xf32>`，这个 pass 什么都不做（`test/Conversion/VectorToSCF/vector-to-scf.mlir:2-5` 的注释写明 0-d/1-d 满宽留给后续 pass）。

`--convert-vector-to-llvm` 才是最后一步，它会把 transfer 变成 `llvm.load/store` + GEP：

```bash
$T/bin/mlir-opt vec.mlir --convert-vector-to-llvm --convert-to-llvm
```

真实输出里有大量 `builtin.unrealized_conversion_cast`（memref 描述符在 LLVM 结构体与 memref 类型之间来回转），以及：

```text
    %21 = llvm.load %20 <alignment = 4> : !llvm.ptr -> vector<4xf32>
    llvm.store %21, %23 <alignment = 4> : vector<4xf32>, !llvm.ptr
```

**踩雷**：`--convert-vector-to-llvm` 不负责把 `scf` 降完，也不负责 `memref` 描述符；它只注册 vector 的 pattern。看到 `unrealized_conversion_cast` 残留，说明还有 dialect 没降（见 Ch 21 的 reconcile 讨论）。

## 24.9 动态 shape：verifier 不管运行时一致性

```bash
cat > dyn.mlir <<'EOF'
func.func @matmul_dyn(%A: memref<?x?xf32>, %B: memref<?x?xf32>, %C: memref<?x?xf32>) {
  linalg.matmul ins(%A, %B : memref<?x?xf32>, memref<?x?xf32>)
               outs(%C : memref<?x?xf32>)
  return
}
EOF
$T/bin/mlir-opt dyn.mlir --convert-linalg-to-loops
```

真实输出（节选）：

```text
    %dim = memref.dim %arg0, %c0 : memref<?x?xf32>
    %dim_0 = memref.dim %arg0, %c1 : memref<?x?xf32>
    %dim_1 = memref.dim %arg1, %c1 : memref<?x?xf32>
    scf.for %arg3 = %c0 to %dim step %c1 {
      scf.for %arg4 = %c0 to %dim_1 step %c1 {
        scf.for %arg5 = %c0 to %dim_0 step %c1 {
```

只有三个 `memref.dim`：M 和 K 取自 `%A`，N 取自 `%B`。**`%C` 的维度从来没有被检查**。如果调用方传了一个 `memref<2x3xf32>` 当 C，循环会写出界——这是 UB，verifier 不会拦（它只看静态类型，`?` 对 `?` 是合法的）。

维护者视角：dynamic shape 的正确性靠 **runtime verifier**（`RuntimeVerifiableOpInterface`）或调用方的 shape 约束，不靠 op verifier。Linalg 的 `RuntimeVerifiableOpInterfaceImpl.cpp` 就是为此存在的。

## 24.10 源码落点

| 你想改什么 | 去哪里 |
|---|---|
| `linalg.matmul` 的 map/region | `mlir/include/mlir/Dialect/Linalg/IR/LinalgStructuredOps.td:96`（`#matmul_accesses`）、`:143`（`indexing_maps`） |
| named op 的额外接口方法 | `mlir/include/mlir/Dialect/Linalg/IR/LinalgInterfaces.td:227`（`LinalgStructuredInterface`）、`:66-132`（`isRowMajorMatmul` 等） |
| 解析/打印/verifier | `mlir/lib/Dialect/Linalg/IR/LinalgOps.cpp:4243`（parse `indexing_maps`）、`:4281`（print） |
| 降成循环 | `mlir/lib/Dialect/Linalg/Transforms/Loops.cpp:297`（`lowerLinalgToLoopsImpl`）、`:317/328/337` |
| tiling | `mlir/lib/Dialect/Linalg/Transforms/Tiling.cpp:433`、`:816` |
| vectorization | `mlir/lib/Dialect/Linalg/Transforms/Vectorization.cpp:1482`、`:2551` |
| DPS 接口 / vector 渐进降级 | `mlir/include/mlir/Interfaces/DestinationStyleOpInterface.td:14`；`mlir/lib/Conversion/VectorToSCF/VectorToSCF.cpp` |

规模参考（本章写作时实测）：

```bash
find mlir/lib/Dialect/Linalg -name '*.cpp' | wc -l                              # 56
find mlir/lib/Dialect/Linalg -name '*.cpp' -o -name '*.h' | xargs cat | wc -l   # 45298
grep -rn 'TODO\|FIXME' mlir/lib/Dialect/Linalg | wc -l                          # 162
find mlir/test/Dialect/Linalg -name '*.mlir' | wc -l                            # 179
```

## 24.11 可跑实验清单（均在 `/tmp/mlir_p5` 下）

- `matmul.mlir --linalg-generalize-named-ops`（named → generic）、`--convert-linalg-to-loops | grep -c scf.for`（0 = 静默 no-op）、`-one-shot-bufferize=... --convert-linalg-to-loops`（三重循环）；`matmul_mem.mlir --convert-linalg-to-parallel-loops`（parallel → `scf.parallel`）。
- `tile.mlir --transform-interpreter`（subview + 缩小 op）、`vecsched.mlir --transform-interpreter`（`multi_reduction`）、`vec2.mlir --convert-vector-to-scf`（2D → `scf.for` + `vector.type_cast`）、`dyn.mlir --convert-linalg-to-loops`（3 个 `memref.dim`，C 不被检查）。

## 24.12 踩雷集锦

1. **错误直觉：`--convert-linalg-to-loops` 会处理所有 Linalg op。** → 正确认识：它只匹配 buffer 语义；tensor 版静默跳过，pass 仍返回成功。先用 `grep -c scf.for` 验证“真的降了”。
2. **错误直觉：`linalg.matmul` 是“特殊 op”，和 generic 不同。** → 正确认识：它等价于三张固定 map 的 `linalg.generic`；差异只在额外接口方法和 verifier 特化。
3. **错误直觉：`outs` 是“输出参数”，可以随便传一个同形状的 tensor。** → 正确认识：tensor 版 `outs` 是**初值**（reduction 起点），传 `tensor.empty` 意味着“初值无所谓”，改变它不是语义中立。
4. **错误直觉：tiling 只是加循环，op 不变。** → 正确认识：operand 类型变成 `strided<[...], offset: ?>` 的 subview；依赖精确类型的 pattern 会失配。
5. **错误直觉：`-linalg-tile` 还在，只是改了名字。** → 正确认识：已迁移到 `transform.structured.tile_using_for`；`--help` 里没有对应 pass。
6. **错误直觉：vectorize 之后就该看到 `llvm.fmul`。** → 正确认识：vectorization 只到 `vector.multi_reduction`，还要 `--convert-vector-to-scf`/`-to-llvm` 两三层。
7. **错误直觉：`--convert-vector-to-scf` 会把所有 transfer 变成循环。** → 正确认识：0-d/1-d 满宽 transfer 原地不动，留给后续 pass；它只拆“高维或不连续”的那些。
8. **错误直觉：dynamic shape 下 verifier 会保证 C 的维度匹配。** → 正确认识：`?` 对 `?` 静态合法；运行时一致性要靠 `RuntimeVerifiableOpInterface` 或调用方契约。

## 24.13 本章重点整理

- 三个类型三条边界：tensor（值、不可变）、memref（地址、可变）、vector（固定形状、寄存器语义）。Linalg 的“双栖”就建立在 tensor/memref 之上。
- 一个 Linalg op = indexing maps（怎么取坐标）+ iterator types（能否并行）+ DPS（写哪里/初值是什么）+ region（标量计算）。
- `--convert-linalg-to-loops`、`-to-affine-loops`、`-to-parallel-loops` 共用 `lowerLinalgToLoopsImpl`，区别只是 loop 类型模板参数。
- tiling 的产物是“外层循环 + subview + 缩小 op”，subview 的 strided 类型是后续 pattern 失配的高发点。
- vectorization 把 iteration space 折成 vector 维度，用 `permutation_map` 对齐形状、用 `ub.poison` 做越界填充、用 `multi_reduction` 折叠 reduction 维。
- vector 的降级是渐进的：高分维 → scf 循环 + `vector.type_cast`，最后才 `llvm.load/store`。
- 静默 no-op 是这一层最常见的 bug 形态：pass 成功不代表匹配到了任何 op。

## 24.14 自我检核

- [ ] 能不看源码写出 `linalg.matmul` 的三张 indexing map 和三个 iterator type
- [ ] 能解释为什么 tensor 版 `--convert-linalg-to-loops` 什么都不做，且不报错
- [ ] 能说出 `outs(%C)` 在 tensor 与 memref 上语义的两点差异
- [ ] 能预测 tiling 后 operand 的类型（含 strided/offset）
- [ ] 能解释 `vector.transfer_read` 的 `permutation_map` 与 `ub.poison` 各自解决什么问题
- [ ] 能说出 `--convert-vector-to-scf` 对 1D 满宽 transfer 为什么不动
- [ ] 能解释 dynamic shape 下为什么 verifier 不检查 C 的维度
- [ ] 能在 `lib/Dialect/Linalg` 里定位“降循环 / tiling / vectorize”三处实现

## 24.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/Linalg/_index.md`**
  - 读哪里：`45-57`（tensor/buffer 双栖与 init tensor 的两种用途）、`284`（region 的标量参数）
  - 能学到什么：官方对“output tensor 只是形状/初值”的措辞；和本章关联：24.3.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/Linalg/IR/LinalgInterfaces.td`**
  - 读哪里：`227` 起（`LinalgStructuredInterface` 的继承列表与自定义方法）
  - 能学到什么：为什么“可分析”是接口承诺而非命名约定；和本章关联：24.3.2
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Linalg/Transforms/Loops.cpp`**
  - 读哪里：`297-340`（统一实现 + 三种实例化）
  - 能学到什么：三种循环形态如何共用一份逻辑；和本章关联：24.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Linalg/Transforms/Vectorization.cpp`**
  - 读哪里：`1482`（`vectorizeAsLinalgGeneric`）、`2551`（precondition）
  - 能学到什么：向量化的前置条件与“为什么有些 op 不向量化”；和本章关联：24.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Conversion/VectorToSCF/vector-to-scf.mlir`**
  - 读哪里：`1-30`（RUN 行与 0-d/1-d 不降级的注释）
  - 能学到什么：渐进降级的分工边界；和本章关联：24.8

Linalg 把“算什么”表达清楚了，但它默认循环是 `scf.for`。下一章回到更底层的控制流：Affine 为什么能做出 SCF 做不到的分析，以及 `arith` 里那些还没关掉的 TODO 意味着什么。

→ 下一章：[Ch 25 Affine / SCF / Arith](./25-affine-scf-arith.md)
