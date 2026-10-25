# Ch 21 MLIR 在 AI 编译器里的位置：tensor / linalg / memref

> **目标**：把同一段 matmul 分别放在 `tensor`、`linalg`、`memref` 三个抽象层上看，理解 MLIR 提供的是“IR 基础设施”而不是“现成的 AI 编译器”，并用本机 `mlir-opt` 实测 named→generic、bufferize、降循环三条路径。

## 为什么需要这个？

到这里你已经见过三种“图”：FX 图（算子级）、TIR（循环级）、HLO（纯函数式）。它们各自解决了一部分问题，但有一个共同点：**IR 和基础设施是绑死的**。

- 想给 FX 加一个新方言？得改 PyTorch。
- 想给 TVM 加一种新 buffer 语义？得改 TIR。
- 想让 HLO 支持一种新 op？得改 XLA。

MLIR 的定位完全不同：它不试图成为“AI 编译器”，而是提供一套**可组合的 IR 基础设施**——方言（dialect）、pass manager、rewrite pattern、dialect conversion、bufferization、interface。AI 编译器（IREE、Torch-MLIR、Triton 的部分前端）在这套基础设施上组装自己的管线。

所以本章的问题不是“MLIR 怎么编译模型”，而是：

- 一个 matmul 在 MLIR 里会经历哪几层抽象？
- `linalg` 的 named/generic 是什么关系？
- bufferization 到底改了什么？
- MLIR 和 TVM 的分工边界在哪？

本机 MLIR 版本（实测）：

```text
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
```

`mlir-opt` 路径：`D:\Learning-with-Claude\.scratch\mlir-env\Library\bin\mlir-opt.exe`。

## 先建立直觉：同一个 matmul，三层写法

### 第一层：tensor（值语义）

```mlir
func.func @matmul(%A: tensor<2x3xf32>, %B: tensor<3x4xf32>, %C: tensor<2x4xf32>) -> tensor<2x4xf32> {
  %0 = linalg.matmul ins(%A, %B : tensor<2x3xf32>, tensor<3x4xf32>)
                     outs(%C : tensor<2x4xf32>) -> tensor<2x4xf32>
  return %0 : tensor<2x4xf32>
}
```

`tensor` 是**不可变值**：没有地址、没有别名、没有 in-place。你只能“产生一个新 tensor”。这和 HLO 的值语义很像。

### 第二层：linalg（结构化计算）

`linalg.matmul` 仍然在 tensor 上，但它携带了**结构化信息**：

- `indexing_maps`：每个操作数如何映射到迭代空间；
- `iterator_types`：哪些维是 `parallel`，哪些是 `reduction`；
- `outs`：目的地（destination-passing style）。

这些信息让编译器知道“这是一个矩阵乘”，而不是“一堆没结构的 load/mul/add”。

### 第三层：memref（可寻址缓冲）

```mlir
%alloc = memref.alloc() : memref<2x4xf32>
linalg.matmul ins(%A, %B : memref<2x3xf32>, memref<3x4xf32>)
             outs(%alloc : memref<2x4xf32>)
```

`memref` 是**缓冲区**：有地址、有 layout、有内存空间，可以 `load/store`、可以别名。到了这一层，才能降到 `scf.for` + `memref.load/store`，再降到 LLVM。

三层的关系可以这样记：

```text
tensor       “算什么”，值语义，无地址
  │ bufferization
  ▼
memref       “放在哪”，缓冲区语义，有地址
  │ linalg → loops
  ▼
scf + memref.load/store
  │ 后续 lower
  ▼
LLVM dialect → LLVM IR
```

而 `linalg` 是横跨前两层的**算子表示**：既可以在 tensor 上，也可以在 memref 上。

## 核心概念一：tensor / linalg / memref 的职责边界

| 层 | 类型 | 语义 | 典型 op | 谁负责 |
|---|---|---|---|---|
| Tensor | `tensor<...>` | 不可变值，SSA | `tensor.extract`, `tensor.insert` | 前端、图优化 |
| Linalg | `linalg.matmul` 等 | 结构化循环 + 索引映射 | named/generic | 融合、tiling、向量化 |
| Memref | `memref<...>` | 可寻址缓冲，带 layout | `memref.load/store/alloc` | bufferization、内存规划 |
| SCF/Affine | `scf.for`, `affine.for` | 显式循环 | 循环变换 | 降级、调度 |
| LLVM | `llvm.load` 等 | 机器级 | codegen | 后端 |

边界很重要：

- **不要在 tensor 上做 `memref.load`**：类型系统不允许。
- **不要在 memref 上期待值语义**：一旦 bufferize，别名和生命周期就出现了。
- **linalg 不是“高层语法糖”**：它的 indexing map 和 iterator type 是后续 tiling/fusion 的依据。

## 核心概念二：linalg named vs generic

`linalg.matmul` 是 **named op**：名字固定、语义固定、验证规则固定。它的等价 generic 形式是：

```mlir
#map = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
%0 = linalg.generic {
    indexing_maps = [#map, #map1, #map2],
    iterator_types = ["parallel", "parallel", "reduction"]
  } ins(%A, %B : tensor<2x3xf32>, tensor<3x4xf32>)
    outs(%C : tensor<2x4xf32>) {
  ^bb0(%in: f32, %in_0: f32, %out: f32):
    %1 = arith.mulf %in, %in_0 : f32
    %2 = arith.addf %out, %1 : f32
    linalg.yield %2 : f32
  } -> tensor<2x4xf32>
```

读法：

- 迭代空间是 `(d0, d1, d2)`，对应 `(m, n, k)`；
- `#map  = (d0,d1,d2) -> (d0,d2)`：A 用 `(m,k)`；
- `#map1 = (d0,d1,d2) -> (d2,d1)`：B 用 `(k,n)`；
- `#map2 = (d0,d1,d2) -> (d0,d1)`：C 用 `(m,n)`；
- `iterator_types` 的前两个是 `parallel`，最后一个是 `reduction`；
- region 里是标量乘加。

named 和 generic 的取舍：

| | named | generic |
|---|---|---|
| 可读性 | 高 | 低 |
| 优化匹配 | 容易（按名字） | 需要匹配 map/region |
| 表达范围 | 固定集合 | 任意结构化计算 |
| 典型用途 | 前端产出、库调用 | 融合后的中间形态、自定义算子 |

`--linalg-generalize-named-ops` 做的是 named → generic 的机械转换；反向的 `--linalg-specialize-generic-ops` 需要模式匹配，不保证成功。注意 23.1.4 里前者已标记 deprecated（源码依据：`.scratch\mlir-env\Library\include\mlir\Dialect\Linalg\Passes.td:80`，建议改用 `linalg-morph-ops`）。

## 核心概念三：bufferization 改了什么

bufferization 的核心问题：**把“产生新值”的 tensor 程序，变成“往缓冲区写”的 memref 程序**。

难点不在语法，而在决策：

- 这个 tensor 能不能就地写进 `outs` 的 buffer？
- 如果不能，就要 `alloc` 一个新 buffer，并可能 `copy`；
- 函数边界上的 tensor 参数/返回值怎么变成 memref？
- 多个 op 共享一个 tensor 时，谁拥有它？

MLIR 的 **one-shot bufferize** 先做一次全局分析（别名、逃逸、读写冲突），再统一改写。它比“逐个 op 贪心 bufferize”更不容易产生多余 copy。

## 示例一：bufferize 的两种模式（实测）

本机实测两种模式：

### 模式 A：不 bufferize 函数边界

```powershell
mlir-opt matmul.mlir --one-shot-bufferize
```

真实输出（节选）：

```mlir
func.func @matmul(%arg0: tensor<2x3xf32>, ...) -> tensor<2x4xf32> {
  %0 = bufferization.to_buffer %arg1 : tensor<3x4xf32> to memref<3x4xf32, strided<[?, ?], offset: ?>>
  %1 = bufferization.to_buffer %arg0 : tensor<2x3xf32> to memref<2x3xf32, strided<[?, ?], offset: ?>>
  %2 = bufferization.to_buffer %arg2 : tensor<2x4xf32> to memref<2x4xf32, strided<[?, ?], offset: ?>>
  %alloc = memref.alloc() {alignment = 64 : i64} : memref<2x4xf32>
  memref.copy %2, %alloc : memref<2x4xf32, strided<[?, ?], offset: ?>> to memref<2x4xf32>
  linalg.matmul ins(%1, %0 : ...) outs(%alloc : memref<2x4xf32>)
  %3 = bufferization.to_tensor %alloc : memref<2x4xf32> to tensor<2x4xf32>
  return %3 : tensor<2x4xf32>
}
```

注意三件事：

1. 函数签名还是 tensor，所以边界处出现 `to_buffer` / `to_tensor`。
2. 因为 `%arg2` 是函数参数（可能被别名），不能直接就地写，于是 `alloc` + `copy`。
3. 这就是“安全但保守”的 bufferization：正确性优先，性能次之。

### 模式 B：bufferize 函数边界

```powershell
mlir-opt matmul.mlir --one-shot-bufferize="bufferize-function-boundaries"
```

真实输出：

```mlir
func.func @matmul(%arg0: memref<2x3xf32, strided<[?, ?], offset: ?>>,
                  %arg1: memref<3x4xf32, strided<[?, ?], offset: ?>>,
                  %arg2: memref<2x4xf32, strided<[?, ?], offset: ?>>)
    -> memref<2x4xf32, strided<[?, ?], offset: ?>> {
  linalg.matmul ins(%arg0, %arg1 : memref<2x3xf32, strided<[?, ?], offset: ?>>,
                             memref<3x4xf32, strided<[?, ?], offset: ?>>)
                outs(%arg2 : memref<2x4xf32, strided<[?, ?], offset: ?>>)
  return %arg2 : memref<2x4xf32, strided<[?, ?], offset: ?>>
}
```

现在：

- 没有 `alloc`，没有 `copy`，`outs` 直接写 `%arg2`；
- 函数签名变成 memref；
- `strided<[?, ?], offset: ?>` 表示“步长和偏移在编译期未知”，因为调用方可以传任意 strided memref。这是函数边界 bufferize 的常见结果，不是 bug。

如果想进一步特化成静态 stride，需要额外的 `memref.cast` / 类型精化 pass，或者一开始就用更具体的 memref 类型。

## 底层机制：从 linalg 到循环

有了 memref，`--convert-linalg-to-loops` 才能工作。真实命令：

```powershell
mlir-opt matmul.mlir --one-shot-bufferize="bufferize-function-boundaries" --convert-linalg-to-loops
```

真实输出（节选）：

```mlir
%c0 = arith.constant 0 : index
%c2 = arith.constant 2 : index
%c1 = arith.constant 1 : index
%c4 = arith.constant 4 : index
%c3 = arith.constant 3 : index
scf.for %arg3 = %c0 to %c2 step %c1 {
  scf.for %arg4 = %c0 to %c4 step %c1 {
    scf.for %arg5 = %c0 to %c3 step %c1 {
      %0 = memref.load %arg0[%arg3, %arg5] : memref<2x3xf32, ...>
      %1 = memref.load %arg1[%arg5, %arg4] : memref<3x4xf32, ...>
      %2 = memref.load %arg2[%arg3, %arg4] : memref<2x4xf32, ...>
      %3 = arith.mulf %0, %1 : f32
      %4 = arith.addf %2, %3 : f32
      memref.store %4, %arg2[%arg3, %arg4] : memref<2x4xf32, ...>
    }
  }
}
```

这就是 Ch 11 里手写过的三重循环，只不过由 pass 自动生成。注意：

- 循环顺序是 `m → n → k`，k 在最内层；
- 没有 tiling、没有向量化、没有并行；
- `memref.load/store` 逐个元素访问，性能不会好。

**这才是 MLIR 的定位**：它提供“从结构化 op 到循环”的可靠降级，但**不替你做 tile/vectorize/parallel**。那些优化要么由上层 transform（`transform.structured.tile_using_for`、vectorize）完成，要么由 IREE 这样的端到端编译器组装。

## 示例二：pass 顺序错了会怎样（实测）

先降循环、再 bufferize：

```powershell
mlir-opt matmul.mlir --convert-linalg-to-loops --one-shot-bufferize="bufferize-function-boundaries"
```

真实输出（节选）：

```mlir
func.func @matmul(%arg0: memref<2x3xf32, ...>, ...) -> memref<2x4xf32, ...> {
  linalg.matmul ins(%arg0, %arg1 : memref<...>, memref<...>) outs(%arg2 : memref<...>)
  return %arg2 : memref<...>
}
```

`linalg.matmul` 还在！原因是：

- 第一步 `convert-linalg-to-loops` 作用在 **tensor** 上，而它的前置条件是 buffer 语义（源码依据：`.scratch\mlir-env\Library\include\mlir\Dialect\Linalg\Passes.td:118-127`，原文写明 “the operands used by the `linalg` ops have buffer semantics”）。
- 条件不满足时它**不报错，也不转换**，直接跳过。
- 第二步 bufferize 把类型改成了 memref，但循环转换已经跑完了。

这类“pass 静默不生效”是 MLIR 最常见的调试坑。判断方法：看输出里目标 op 是否还在；不要只看退出码。

## 对比与取舍：MLIR vs TVM

| 维度 | MLIR | TVM |
|---|---|---|
| 定位 | IR 基础设施 + 方言生态 | 端到端 AI 编译器 |
| 输入 | 任意方言（StableHLO/TOSA/Torch-MLIR…） | Relax/Relay/TE |
| 调度 | 无统一调度语言；靠 tiling/transform dialect | `s_tir.Schedule` + `meta_schedule` |
| 自动调优 | 不提供 | 提供 |
| codegen | LLVM/SPIR-V 等 dialect 降级 | LLVM/CUDA/… 内置 |
| runtime | 不提供（IREE 提供） | 提供（VM/GraphExecutor） |
| 学习曲线 | 高（要理解方言组合） | 中（API 集中） |
| 适合 | 做编译器、做方言、做端到端框架 | 做模型优化与部署 |

一句话分工：

- **MLIR 提供“IR 的乐高积木”**：类型系统、pass 管理、模式重写、转换框架、bufferization。
- **TVM 提供“装好的编译器”**：Relax/TIR、调度、自动调优、codegen、runtime。
- **IREE 是“用 MLIR 积木装出来的 TVM 类产品”**：StableHLO/TOSA/Torch-MLIR → linalg → bufferize → LLVM/SPIR-V，再配自己的 runtime。

IREE 的典型路径：

```text
PyTorch / JAX / TF
   │ Torch-MLIR / StableHLO / TOSA
   ▼
linalg on tensors
   │ tiling / fusion / vectorization
   ▼
memref + scf
   │ bufferization / lowering
   ▼
LLVM dialect / SPIR-V
   ▼
IREE runtime（CPU / Vulkan / CUDA）
```

和本章手敲的三条命令相比，IREE 只是把中间的 pass 组合得更多、更自动，底层机制完全一样。

## 踩雷集锦

1. **用 PowerShell 的 `Set-Content -Encoding UTF8` 写 `.mlir`**：会带 BOM，`mlir-opt` 报 `error: unexpected character`（本机实测，错误指向 `1:1` 的 `﻿module`）。用 `[IO.File]::WriteAllText(..., UTF8Encoding($false))` 或编辑器保存为无 BOM。
2. **在 tensor 上直接 `--convert-linalg-to-loops`**：不报错、不转换，`linalg.matmul` 原样留下。先 bufferize。
3. **pass 顺序颠倒**：`--convert-linalg-to-loops` 写在 `--one-shot-bufferize` 前面，结果同上。
4. **以为 bufferize 一定就地**：不 bufferize 函数边界时，会出现 `alloc` + `memref.copy`；这是保守但正确的选择。
5. **把 `strided<[?, ?], offset: ?>` 当错误**：它是函数边界上的动态 stride，合法；要静态化需要额外类型精化。
6. **把 named→generic 当优化**：它只是换表示，不改变性能；真正的优化是 tiling/fusion/vectorize。
7. **在 23.1.4 里继续用 `--linalg-generalize-named-ops`**：能用但已 deprecated，新代码用 `--linalg-morph-ops`。
8. **以为 MLIR 会自动帮你调度**：不会；它只提供积木。

## 进阶：再往深一层

- **Transform dialect**：用 `transform.structured.tile_using_for` 描述 tiling，把“怎么切”变成 IR 而不是 C++ pass 参数。
- **Transform dialect 的 vectorize**：把内层循环变成 `vector` dialect，再降到 SIMD；对应 Ch 15。
- **one-shot bufferize 的分析**：读 `mlir/lib/Dialect/Bufferization/Transforms/OneShotAnalysis.cpp`，理解 alias/escape 分析怎么决定 in-place。
- **IREE 的 flow/hal/stream 方言**：从 linalg 到 runtime 的调度与内存规划，是 MLIR 生态里最完整的生产案例。
- **回指本仓库的 MLIR 课程**：bufferization 的细节见 [`../mlir_core/20-bufferization.md`](../mlir_core/20-bufferization.md)，linalg 的 iterator/map 见 [`../mlir_core/23-linalg.md`](../mlir_core/23-linalg.md)。

## 动手练习

### 练习 1：跑通三条命令

在本机创建 `matmul.mlir`（注意无 BOM），依次运行：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin\mlir-opt.exe"
& $mlir matmul.mlir --convert-linalg-to-loops
& $mlir matmul.mlir --one-shot-bufferize="bufferize-function-boundaries"
& $mlir matmul.mlir --one-shot-bufferize="bufferize-function-boundaries" --convert-linalg-to-loops
```

回答：哪一条命令的输出里还有 `linalg.matmul`？为什么？

<details>
<summary>参考思路</summary>

第一条还有（tensor 上不转换）；第二条还有（只 bufferize，不降循环）；第三条没有（buffer 语义满足前置条件，降成 `scf.for`）。三条命令的差别就是本章的主线。
</details>

### 练习 2：改矩阵形状

把 `2x3 @ 3x4` 改成 `4x5 @ 5x6`，重新跑第三条命令。回答：循环边界变成多少？`memref.load` 的索引顺序变了吗？

<details>
<summary>参考思路</summary>

外层 `0..4`，中层 `0..6`，内层 `0..5`。索引仍是 `A[m,k]`、`B[k,n]`、`C[m,n]`，只是边界数字变了。这个练习是为了确认你能把 IR 和数学公式对上。
</details>

### 练习 3：named → generic 再降循环

先跑 `--linalg-generalize-named-ops`，再 bufferize、再降循环。回答：generic 形式的输出和 named 形式降出来的循环有区别吗？

<details>
<summary>参考思路</summary>

语义相同，循环结构相同；区别只在中间表示。named op 在降级时会被展开成同样的 map/region。这个练习说明“named 只是更方便匹配的表示”，不是性能优化。
</details>

### 练习 4：制造 BOM 错误

用 PowerShell 的 `Set-Content -Encoding UTF8` 写一个 `.mlir` 文件，运行 `mlir-opt`，保存完整报错；再改用无 BOM 写入，确认通过。

<details>
<summary>参考思路</summary>

报错形如：

```text
matmul.mlir:1:1: error: unexpected character
﻿module {
```

BOM 是文件头的 `EF BB BF`，MLIR parser 不认。用 `[IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`。
</details>

### 练习 5：把 matmul 降到 LLVM（可选，本机实测通过）

在第三条命令后接 `--convert-scf-to-cf --convert-arith-to-llvm --convert-cf-to-llvm --convert-func-to-llvm --finalize-memref-to-llvm --reconcile-unrealized-casts`，尝试得到 LLVM dialect。回答：还缺哪些 pass？为什么 `memref` 的动态 stride 会增加降级难度？

<details>
<summary>参考思路</summary>

上面这条已经把常用的 func/arith/cf/memref 转换都串起来了，本机 `mlir-opt` 23.1.4 实测可以输出 `llvm.func` + `llvm.br`/`llvm.cond_br`；如果起始 IR 里还有 affine 或 vector，需要先转掉它们。动态 stride 需要把 stride/offset 作为运行时值传入，并生成描述符（descriptor）；静态 memref 则可以直接算地址常量。完整链路见 `../mlir_core/21-lowering-to-llvm.md`。
</details>

## 本章重点整理

- MLIR 是 IR 基础设施，不是端到端 AI 编译器；它提供方言、pass、rewrite、conversion、bufferization。
- `tensor` 是值语义，`memref` 是缓冲语义，`linalg` 是横跨两者的结构化算子表示。
- `linalg.matmul` 的 indexing maps 是 `(m,k)`、`(k,n)`、`(m,n)`，iterator types 是 `parallel, parallel, reduction`。
- named → generic 是机械转换，不改变性能。
- bufferization 把 tensor 程序变成 memref 程序；是否就地写取决于 one-shot 分析。
- `convert-linalg-to-loops` 的前置条件是 buffer 语义；在 tensor 上会静默不转换。
- MLIR 与 TVM 的分工：MLIR 提供 IR 积木，TVM 提供调度/codegen/runtime；IREE 是用 MLIR 积木装出来的端到端编译器。

## 自我检核

- [ ] 我能解释 tensor / linalg / memref 三层的职责差异
- [ ] 我能写出 `linalg.matmul` 的三张 indexing map
- [ ] 我知道 named op 和 generic op 的关系
- [ ] 我能解释 bufferization 在做什么决策
- [ ] 我知道为什么 `convert-linalg-to-loops` 在 tensor 上不生效
- [ ] 我能读懂 `scf.for` + `memref.load/store` 的降级结果
- [ ] 我能说出 MLIR 不提供哪些东西（调度、调优、runtime）
- [ ] 我能把本章和 `../mlir_core/` 的 bufferization、linalg 章节对应起来

## 延伸阅读

- **MLIR Linalg 文档**（https://mlir.llvm.org/docs/Dialects/Linalg/）：读 named/generic、iterator types、indexing maps。与本章关联：核心概念二。
- **MLIR Bufferization 文档**（https://mlir.llvm.org/docs/Bufferization/）：读 one-shot bufferize 的分析与接口。与本章关联：核心概念三。
- **本仓库 `../mlir_core/20-bufferization.md`**：bufferization 的完整机制与 C++ 接口。与本章关联：把本章的 IR 现象落到实现。
- **本仓库 `../mlir_core/23-linalg.md`**：linalg 方言的完整变换集合。与本章关联：tiling/fusion/vectorization。
- **IREE 文档**（https://iree.dev/）：读 “Importing models” 与 “Compilation” 两节。与本章关联：生产级 MLIR 管线。

---

→ [Ch 22 ONNX Runtime 与 TensorRT：图优化与 Execution Provider](./22-onnxruntime-tensorrt.md)