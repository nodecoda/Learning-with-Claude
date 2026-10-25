# Ch 4 方言地图：谁负责什么

> **目标**：建立“抽象层 → dialect”的对照表。看到任何一个 op，你能立刻说出三件事：它操作什么类型、处在哪一层、下一步由谁消费；看到一条 pipeline，你能预判哪些 op 会变、哪些原样留下。

## 为什么需要这个？

本机 `mlir-opt` 注册了 47 个方言。这个数字本身没有意义，真正的问题是：**给你一个 op，你知不知道它在哪一层？**

初学者最常见的两种失败，都来自误判层次：

- 把 `arith.addi` 和 `llvm.add` 当成“同一个加法的两种写法”，于是觉得 conversion 只是改名游戏；
- 把 `linalg.matmul` 当成“一条矩阵乘法指令”，于是在 tensor 上跑 `convert-linalg-to-loops`，发现 IR 一动不动，以为工具坏了。

第二种在本机是可复现的（练习 A 会让你亲手做一遍）。原因不是 bug：那个 pass 只处理 buffer 语义的 linalg op，tensor 版本它不认识，于是**静默跳过**。

这一章要建立的判断力很具体：

1. 看操作数类型：是 `i32`、`tensor<...>`、`memref<...>`，还是 `!llvm.ptr`？
2. 看 region：它内部还装了哪些 op？
3. 看消费者：哪个 pass 声称能把它变没？

方言边界不是“语法分类”，而是三份契约：**类型系统、内存模型、可降级目标**。这句话是本章的主线。

## 先建立直觉：IR 是一条流水线，dialect 是工位

把编译想成工件在流水线上走：

```text
源语义
  │  tensor<...> + linalg           值语义：有形状，没有地址
  ▼
内存与循环
  │  memref<...> + scf / affine    有地址、有布局、有循环结构
  ▼
标量与跳转
  │  arith + cf                    只剩标量运算和基本块
  ▼
机器
  │  llvm                          iN / !llvm.ptr / struct
  ▼
LLVM IR（mlir-translate）
```

三个要点：

- **工件可以同时带多个工位的标签**。合法中间状态就是混合的：`func.func` 里可以有 `linalg`，`llvm.func` 里可以暂时残留 `arith`。
- **每个工位只承诺自己那一段**。`arith` 不负责循环，`scf` 不负责内存布局，`linalg` 不负责地址计算。
- **流水线不是单向的**。你可以先 bufferize 再向量化，也可以在某些路径上反过来做；顺序由契约决定，不由“层号”决定。

一个立刻可用的读法：**先看操作数类型，再看它所在的 region，最后问“谁会把它降掉”**。这三步能解释大部分“这段 IR 为什么长这样”。

## 抽象层地图：20 个方言的岗位表

| 层 | 方言 | 你会在里面看到 | 谁消费它 |
|---|---|---|---|
| 容器 | `builtin` | `module`、`unrealized_conversion_cast`、内建类型 | 不降级；一直在 |
| 函数 | `func` | `func.func`、`func.call`、`func.return` | `convert-func-to-llvm`、`inline` |
| 标量整数/浮点 | `arith` | `addi`/`addf`/`cmpi`/`constant`/`index_cast` | `convert-arith-to-llvm`、`canonicalize` |
| 数学函数 | `math` | `sqrt`/`exp`/`fma`/`rsqrt` | `convert-math-to-llvm`/`-libm` |
| 复数 | `complex` | `complex.create`/`add`/`abs` | `convert-complex-to-llvm`/`-libm` |
| 索引 | `index` | `index.add`/`cmp`/`casts`（只吃 `index`） | `convert-index-to-llvm` |
| 结构化控制流 | `scf` | `for`/`if`/`while`/`yield` | `convert-scf-to-cf` |
| 显式 CFG | `cf` | `cf.br`/`cond_br`/`switch` | `convert-cf-to-llvm` |
| 多面体 | `affine` | `affine.for`/`load`/`store`/`apply` | `lower-affine` |
| 内存 | `memref` | `alloc`/`load`/`store`/`subview`/`cast` | `finalize-memref-to-llvm` |
| 值张量 | `tensor` | `empty`/`extract_slice`/`insert_slice`/`pad` | `one-shot-bufferize` |
| SIMD | `vector` | `transfer_read`/`transfer_write`/`fma` | `convert-vector-to-llvm`/`-scf`/`-gpu` |
| 结构化计算 | `linalg` | `matmul`/`add`/`generic`（带 indexing maps） | `convert-linalg-to-loops`/`-affine-loops`、向量化 |
| 形状 | `shape` | `shape_of`/`with_shape` | `convert-shape-to-std` |
| 稀疏 | `sparse_tensor` | 带 `encoding` 的 tensor + `sparse_tensor.*` | `sparse-tensor-codegen` |
| 缓冲化桥 | `bufferization` | `to_tensor`/`to_buffer`/`alloc_tensor` | `convert-bufferization-to-memref` |
| 并行/设备 | `gpu` | `gpu.launch`/`gpu.func`/`thread_id` | `convert-gpu-to-nvvm`/`-rocdl`/`-spirv` |
| 机器 | `llvm` | `llvm.func`/`add`/`load`/`getelementptr` | `mlir-translate --mlir-to-llvmir` |
| 变换驱动 | `transform` | `transform.named_sequence`/`apply_patterns` | transform interpreter（不降级） |
| 未定义行为 | `ub` | `ub.poison`/`ub.unreachable` | `convert-ub-to-llvm` |

三条补充规则：

- `builtin` 不是“基础运算”，而是**容器与内建类型**：`module`、`unrealized_conversion_cast`、`tensor`/`memref` 的类型定义都在这里。
- `index` 是从 `arith` 分出来的专用方言：只吃 `index`，专门做索引算术，避免 `arith` 的泛化。
- `llvm` 是通向 LLVM IR 的出口：`mlir-translate --mlir-to-llvmir` 只认它。

## 同一个加法，为什么有三种写法？

三个真实片段：

```mlir
%0 = arith.addi %a, %b : i32
```

```mlir
%0 = linalg.add ins(%A, %B : tensor<4x4xf32>, tensor<4x4xf32>)
                    outs(%init : tensor<4x4xf32>) -> tensor<4x4xf32>
```

```mlir
%0 = llvm.add %a, %b : i32
```

| 维度 | `arith.addi` | `linalg.add` | `llvm.add` |
|---|---|---|---|
| 操作数类型 | `iN` 或 `index` | shaped（tensor / memref），形状与元素类型一致 | LLVM 整数 `iN` |
| 形状 | 无 | 有，且由 indexing maps 描述 | 无（向量另算） |
| 内存/副作用 | 无（`Pure`） | 读输入、写输出（buffer 语义时） | 无（机器指令） |
| 语义粒度 | 一个标量值 | 一整块形状上的逐元素运算 | 一条机器加法 |
| 谁产生它 | 前端标量表达式、循环内的地址计算 | 前端/高层算子、`convert-elementwise-to-linalg` | conversion pass |
| 谁消费它 | `convert-arith-to-llvm`、`canonicalize` | bufferization + `convert-linalg-to-loops` 等 | `mlir-translate` |
| 降级后 | `llvm.add` | `scf.for` + `memref.load/store` + `arith.addf` | LLVM IR `add` |

逐条说清楚：

- **`arith.addi` 是标量契约**。源码依据：`ArithOps.td:58-62` 把操作数和结果都约束为 `Arith_SignlessIntegerOrIndexLike`。它不关心形状、不关心内存，且带 `Pure`，所以能被折叠。谁消费它？`convert-arith-to-llvm`。
- **`linalg.add` 是结构化契约**。它要求操作数是 shaped 值，三者形状、元素类型一致；`LinalgNamedStructuredOps.yaml:502-507` 的原文是 “The shapes and element types must be identical”。它自己带一个 region，里面才是逐元素运算。
- **`llvm.add` 是机器契约**。源码依据：`LLVMOps.td:51-57` 把操作数约束为 `AnySignlessInteger`，`LLVM_AddOp` 在 `LLVMOps.td:152`。它和 LLVM IR 的 `add` 一一对应。

所以三种加法不是冗余，而是**三个不同问题的答案**：算一个标量、算一整块形状、发一条机器指令。同一个“加法”还能有更多形态：`index.add`（索引专用）、`vector.fma`（SIMD 融合乘加）。层数从来不止三。

## 47 个方言，分成六类

```powershell
& "$mlir\mlir-opt.exe" --show-dialects
```

实测输出（一行）：

```text
Available Dialects: acc,affine,amdgpu,arith,arm_neon,arm_sme,arm_sve,async,bufferization,builtin,cf,complex,dlti,emitc,func,gpu,index,irdl,linalg,llvm,math,memref,ml_program,mpi,nvgpu,nvvm,omp,pdl,pdl_interp,ptr,quant,rocdl,scf,shape,shard,smt,sparse_tensor,spirv,tensor,tosa,transform,ub,vector,wasmssa,x86,xegpu,xevm
```

47 个名字，硬背没有意义。按“它解决什么问题”分成六个抽屉（10 + 6 + 4 + 13 + 4 + 10 = 47）：

| 类 | 数量 | 成员 | 一句话职责 |
|---|---|---|---|
| 核心 IR 与标量 | 10 | `builtin` `func` `arith` `math` `complex` `index` `ub` `scf` `cf` `llvm` | 任何程序都要用的骨架：容器、函数、标量、控制流、机器类型 |
| 数据与内存 | 6 | `tensor` `memref` `vector` `sparse_tensor` `bufferization` `shape` | 值 / 地址 / 寄存器三种数据世界，外加形状与稀疏编码 |
| 计算抽象 | 4 | `linalg` `affine` `tosa` `quant` | 比标量高一层的“一块计算”：结构化、多面体、算子集、量化 |
| 并行与设备 | 13 | `gpu` `nvgpu` `nvvm` `amdgpu` `rocdl` `omp` `acc` `mpi` `x86` `arm_neon` `arm_sme` `arm_sve` `xegpu` | 线程/块/warp、厂商 intrinsic、并行运行时 |
| 目标导出 | 4 | `spirv` `emitc` `wasmssa` `xevm` | 不是“再降一层 MLIR”，而是出门到 SPIR-V / C++ / Wasm / Intel VM |
| 元编程与运行时 | 10 | `async` `dlti` `irdl` `ml_program` `pdl` `pdl_interp` `ptr` `shard` `smt` `transform` | 描述 IR 的 IR 与运行时设施：布局、模式、调度、分片、异步、指针、约束 |

怎么记：**前四类是你写 IR 时会用到的，后两类是你读 pipeline 或接后端时会遇到的**。正文反复使用的是上面那张 20 行的表；其余方言在 Ch 25–26 与后端章节按需出现。

## 底层机制：方言边界是抽象契约

### 契约一：类型系统

三段真实报错（去掉路径与 PowerShell 包装）：

```text
error: 'arith.addi' op operand #0 must be signless-non-zero-bitwidth-integer-like, but got 'tensor<4xf32>'
error: 'linalg.add' op operand #2 must be variadic of shaped of any non-token type values, but got 'i32'
error: custom op 'llvm.func' failed to construct function type: expected LLVM type for function arguments
```

三个 op 都在说同一句话：**我的类型系统里没有你**。这不是 verifier 太严，而是边界本身——`arith` 的值世界是标量，`linalg` 是 shaped，`llvm` 只认 LLVM 类型（`index` 不在其中）。跨过去只有两条路：换 op，或者换类型（conversion / bufferization）。

### 契约二：内存模型

- `tensor` 是**值语义**：不可变、没有地址；`tensor.insert_slice` 返回一个新值，原值不变。
- `memref` 是**地址语义**：有 allocation、stride、offset、memory space；`memref.load/store` 有真实读写效果。
- `vector` 是**寄存器语义**：固定长度的 SIMD 值，通常映射到硬件向量寄存器。
- `llvm` 是**机器内存语义**：`!llvm.ptr` + `getelementptr`，别名规则与 LLVM 一致。

同一个 `linalg.add` 可以同时吃 tensor 和 memref，但**切换点只有一个**：bufferization。它不是“换个类型”，而是“把值世界换成地址世界”。下面示例二会看到同一个 op 在两个世界里各长什么样。

### 契约三：可降级目标

每个方言只承诺到某几个下游，没有“万能出口”：

- `scf` 的官方出口是 `cf`（`convert-scf-to-cf`，`SCFToControlFlow.cpp:104` 起是 `ForLowering`）；
- `affine` 的出口是 `arith + scf + memref`（`lower-affine`，`AffineToStandard.cpp:158` 建 `scf::ForOp`）；
- `tensor` 的出口是 `memref`（`one-shot-bufferize`）；
- `memref` 的出口是 `llvm`（`finalize-memref-to-llvm`）；
- `linalg` 的出口有好几个（loops / affine loops / vector / gpu），这是“结构化”的代价与价值。

### 契约四：验证器与接口

`verify()` 管局部合法性；interface 管跨 dialect 的“通行证”：`MemoryEffectOpInterface`（能不能重排）、`BufferizableOpInterface`（能不能 bufferize）、`LinalgStructuredInterface`（能不能当结构化计算处理）。Ch 8 会打开这一层。

## 示例一：arith + func —— 标量层

`scalar.mlir`：

```mlir
func.func @scalar_math(%a: i32, %b: i32) -> i32 {
  %0 = arith.addi %a, %b : i32
  %1 = arith.muli %0, %b : i32
  %2 = arith.index_cast %1 : i32 to index
  %3 = arith.index_cast %2 : index to i32
  return %3 : i32
}
```

```powershell
& "$mlir\mlir-opt.exe" .\scalar.mlir
```

实测输出：

```mlir
module {
  func.func @scalar_math(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %1 = arith.muli %0, %arg1 : i32
    %2 = arith.index_cast %1 : i32 to index
    %3 = arith.index_cast %2 : index to i32
    return %3 : i32
  }
}
```

加 `--canonicalize`：

```powershell
& "$mlir\mlir-opt.exe" .\scalar.mlir --canonicalize
```

实测输出：

```mlir
module {
  func.func @scalar_math(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %1 = arith.muli %0, %arg1 : i32
    return %1 : i32
  }
}
```

两条 `index_cast` 一来一回，被折叠成恒等。注意：**这次折叠发生在 arith/func 层，和 conversion 无关**。

再跑 conversion：

```powershell
& "$mlir\mlir-opt.exe" .\scalar.mlir --pass-pipeline='builtin.module(convert-func-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)'
```

实测输出：

```mlir
module {
  llvm.func @scalar_math(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    %1 = llvm.mul %0, %arg1 : i32
    %2 = llvm.sext %1 : i32 to i64
    llvm.return %1 : i32
  }
}
```

三个观察：

1. **函数壳换了**：`func.func` → `llvm.func`；签名里没有 `index`，因为这个函数本来就没有 `index` 参数。
2. **运算换了**：`arith.addi` → `llvm.add`，`arith.muli` → `llvm.mul`。
3. **`index` 变成 `i64`**：`%2 = llvm.sext` 就是 `i32 → index` 的落点；`index → i32` 那一步被折叠掉了，所以 `llvm.return` 直接用 `%1`，`%2` 成了死代码。

这三点分别对应：函数层、标量层的降级，以及“`index` 是目标相关类型”这条契约。

## 示例二：tensor + linalg —— 值语义层

`linalg_add.mlir`：

```mlir
func.func @add_tensors(%A: tensor<4x4xf32>, %B: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %init = tensor.empty() : tensor<4x4xf32>
  %0 = linalg.add ins(%A, %B : tensor<4x4xf32>, tensor<4x4xf32>)
                    outs(%init : tensor<4x4xf32>) -> tensor<4x4xf32>
  return %0 : tensor<4x4xf32>
}
```

先看它的真实结构：

```powershell
& "$mlir\mlir-opt.exe" .\linalg_add.mlir --mlir-print-op-generic
```

实测输出（保留关键部分）：

```mlir
"builtin.module"() ({
  "func.func"() <{function_type = (tensor<4x4xf32>, tensor<4x4xf32>) -> tensor<4x4xf32>, sym_name = "add_tensors"}> ({
  ^bb0(%arg0: tensor<4x4xf32>, %arg1: tensor<4x4xf32>):
    %0 = "tensor.empty"() : () -> tensor<4x4xf32>
    %1 = "linalg.add"(%arg0, %arg1, %0) <{operandSegmentSizes = array<i32: 2, 1>}> ({
    ^bb0(%arg2: f32, %arg3: f32, %arg4: f32):
      %2 = "arith.addf"(%arg2, %arg3) <{fastmath = #arith.fastmath<none>}> : (f32, f32) -> f32
      "linalg.yield"(%2) : (f32) -> ()
    }) : (tensor<4x4xf32>, tensor<4x4xf32>, tensor<4x4xf32>) -> tensor<4x4xf32>
    "func.return"(%1) : (tensor<4x4xf32>) -> ()
  }) : () -> ()
}) : () -> ()
```

`linalg.add` 不是一个原子指令，它内部有一个 region：`arith.addf` + `linalg.yield`。**“张量层”的加法，元素级实现仍然是 arith**。

bufferize：

```powershell
& "$mlir\mlir-opt.exe" .\linalg_add.mlir --one-shot-bufferize="bufferize-function-boundaries"
```

实测输出：

```mlir
module {
  func.func @add_tensors(%arg0: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg1: memref<4x4xf32, strided<[?, ?], offset: ?>>) -> memref<4x4xf32> {
    %alloc = memref.alloc() {alignment = 64 : i64} : memref<4x4xf32>
    linalg.add ins(%arg0, %arg1 : memref<4x4xf32, strided<[?, ?], offset: ?>>, memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%alloc : memref<4x4xf32>)
    %cast = memref.cast %alloc : memref<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
    return %alloc : memref<4x4xf32>
  }
}
```

同一个 `linalg.add`，类型从 tensor 换成了 memref，函数签名也跟着换。**op 没变，契约变了**。

再降到循环：

```powershell
& "$mlir\mlir-opt.exe" .\linalg_add.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops))'
```

实测输出：

```mlir
module {
  func.func @add_tensors(%arg0: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg1: memref<4x4xf32, strided<[?, ?], offset: ?>>) -> memref<4x4xf32> {
    %c1 = arith.constant 1 : index
    %c4 = arith.constant 4 : index
    %c0 = arith.constant 0 : index
    %alloc = memref.alloc() {alignment = 64 : i64} : memref<4x4xf32>
    scf.for %arg2 = %c0 to %c4 step %c1 {
      scf.for %arg3 = %c0 to %c4 step %c1 {
        %0 = memref.load %arg0[%arg2, %arg3] : memref<4x4xf32, strided<[?, ?], offset: ?>>
        %1 = memref.load %arg1[%arg2, %arg3] : memref<4x4xf32, strided<[?, ?], offset: ?>>
        %2 = arith.addf %0, %1 : f32
        memref.store %2, %alloc[%arg2, %arg3] : memref<4x4xf32>
      }
    }
    return %alloc : memref<4x4xf32>
  }
}
```

到这里，`linalg` 消失了，换成了 `scf + memref + arith`。这就是“消费”：每个层只负责把自己变没。

## 示例三：memref + scf + affine —— 内存与控制流

`axpy_affine.mlir`：

```mlir
func.func @axpy_affine(%alpha: f32, %x: memref<?xf32>, %y: memref<?xf32>, %n: index) {
  affine.for %i = 0 to %n {
    %xi = affine.load %x[%i] : memref<?xf32>
    %yi = affine.load %y[%i] : memref<?xf32>
    %t = arith.mulf %alpha, %xi : f32
    %s = arith.addf %t, %yi : f32
    affine.store %s, %y[%i] : memref<?xf32>
  }
  return
}
```

```powershell
& "$mlir\mlir-opt.exe" .\axpy_affine.mlir --lower-affine
```

实测输出：

```mlir
module {
  func.func @axpy_affine(%arg0: f32, %arg1: memref<?xf32>, %arg2: memref<?xf32>, %arg3: index) {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    scf.for %arg4 = %c0 to %arg3 step %c1 {
      %0 = memref.load %arg1[%arg4] : memref<?xf32>
      %1 = memref.load %arg2[%arg4] : memref<?xf32>
      %2 = arith.mulf %arg0, %0 : f32
      %3 = arith.addf %2, %1 : f32
      memref.store %3, %arg2[%arg4] : memref<?xf32>
    }
    return
  }
}
```

`affine` 的三个 op 同时变成了别的方言：`affine.for` → `scf.for`，`affine.load/store` → `memref.load/store`。**这就是 affine 的“出口契约”**：它承诺降到 arith + scf + memref，而不是直接到 LLVM。

把两步串起来，一次降到显式 CFG。注意顺序：`lower-affine` 先产生 `scf.for`，`convert-scf-to-cf` 才能把它降掉。

```powershell
& "$mlir\mlir-opt.exe" .\axpy_affine.mlir --pass-pipeline='builtin.module(func.func(lower-affine,convert-scf-to-cf))'
```

实测输出：

```mlir
module {
  func.func @axpy_affine(%arg0: f32, %arg1: memref<?xf32>, %arg2: memref<?xf32>, %arg3: index) {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    cf.br ^bb1(%c0 : index)
  ^bb1(%0: index):  // 2 preds: ^bb0, ^bb2
    %1 = arith.cmpi slt, %0, %arg3 : index
    cf.cond_br %1, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %2 = memref.load %arg1[%0] : memref<?xf32>
    %3 = memref.load %arg2[%0] : memref<?xf32>
    %4 = arith.mulf %arg0, %2 : f32
    %5 = arith.addf %4, %3 : f32
    memref.store %5, %arg2[%0] : memref<?xf32>
    %6 = arith.addi %0, %c1 : index
    cf.br ^bb1(%6 : index)
  ^bb3:  // pred: ^bb1
    return
  }
}
```

循环的归纳变量 `%0` 变成了 `^bb1` 的 block argument，回边用 `%6` 更新。`memref` 和 `arith` 都没动——这一步只消费 `scf`。

## 对比与取舍

| 你想做的事 | 该在哪层做 | 为什么 | 用错层的代价 |
|---|---|---|---|
| 常量折叠、代数化简 | `arith` | fold 是 op 自己的 hook，`canonicalize` 只负责驱动 | 在 `linalg` 层做，只能折叠整个 structured op，漏掉元素级 |
| 循环分块/融合/交换 | `affine`（有 map/set）或 `scf`（没有多面体信息） | 需要仿射分析才用 `affine` | 用 `scf` 做分块要自己证下标关系，容易错 |
| 算子融合、算子特化 | `linalg` | 只有 structured op 才带 indexing maps 与 iterator types | 在 `memref` 层做，已丢失“这是一个 matmul”的信息 |
| 显式内存管理/复用 | `memref` | `tensor` 没有地址，谈不了 alloc/reuse | 在 tensor 层谈复用，只能等 bufferization 之后 |
| SIMD / 向量化 | `vector` | 只有它能表达寄存器宽度与 mask | 用 `arith` 表达，后端只能逐标量生成 |
| 设备映射 | `gpu` | 线程/块/warp 是设备概念 | 在 `scf` 里模拟线程，后端无法识别 |
| ABI、指令选择 | `llvm` | 与 LLVM IR 一一对应 | 更早的层没有 ABI 概念 |

取舍的核心不是“哪层更高级”，而是“**你需要哪份信息**”。要仿射分析就用 `affine`；要跨算子融合就用 `linalg`；要控制寄存器就用 `vector`。反过来，用错层要么信息不足（`scf` 没有 map），要么过度约束（`affine` 要求仿射下标）。

## 踩雷集锦

1. **把 dialect 当语法分类。** `linalg.add` 在 tensor 和 memref 上都能 parse；决定它行为的是类型世界，不是名字。

2. **以为 `convert-linalg-to-loops` 能处理 tensor。** 实测：tensor 版本静默不动。源码依据：`Loops.cpp:266` 要求 `hasPureBufferSemantics()`，不满足就 `notifyMatchFailure`。

3. **以为 `llvm.func` 能写 `index`。** 实测 parse 阶段就报 `expected LLVM type for function arguments`。`index` 是 MLIR 的目标相关类型，不是 LLVM 类型。

4. **以为 `arith` 能吃 tensor。** 实测 verifier 报 operand 类型错误。要逐元素算张量，用 `linalg` 或 `vector`。

5. **以为 `affine.load` 可以吃任意循环变量。** 实测：把 `scf.for` 的归纳变量喂给它，报 `operand cannot be used as a dimension id`；affine 只认仿射作用域里的 dim/symbol。

6. **以为 `--show-dialects` 里的名字就能直接写。** 注册 ≠ 加载；custom 语法还需要该 dialect 的 parser。Ch 1 的 `foo.bar` 就是这样失败的。

7. **以为 pass 顺序无所谓。** 先 `convert-scf-to-cf` 再 `lower-affine`，新生成的 `scf.for` 没人再降；实测最终 IR 里仍有 `scf.for`（练习 A 会复现）。

## 进阶：再往深一层

- **注册 vs 加载**：47 个只是“编译进 `mlir-opt` 的清单”；`MLIRContext` 里真正加载哪些，取决于 registry 与 dependent dialects（Ch 16）。
- **接口是跨方言通行证**：`BufferizableOpInterface` 让 bufferization 不认识 `linalg` 也能处理它（Ch 8、Ch 20）。
- **transform dialect 是“用 IR 写调度”**：它不降级，只驱动别的 pass 与 pattern（Ch 26）。
- **方言集合是构建产物**：47 不是 MLIR 的常量，换一个构建配置就会变；别把 `--show-dialects` 当规范。

## 动手练习

1. **画自己的地图**：从 47 个里挑 12 个，写成“层 / 类型 / 消费者”三列，不许看本章表格。
2. **换类型世界**：把 `scalar.mlir` 改写成 `f32` 版（`addi`→`addf`、`muli`→`mulf`），重跑三条命令，确认得到 `llvm.fadd` / `llvm.fmul`。
3. **制造静默失败**：对 `linalg_add.mlir` 只跑 `convert-linalg-to-loops`（不 bufferize），加上 `--mlir-print-ir-after-change`，确认没有任何 IR dump 打印出来。
4. **顺序实验**：用一条 pipeline 跑 `func.func(lower-affine,convert-scf-to-cf)`，再交换成 `func.func(convert-scf-to-cf,lower-affine)`，比较两次输出，解释为什么第二种顺序最后还剩下 `scf.for`。
5. **读一个真实 op**：选 `vector.transfer_read`，用 `--mlir-print-op-generic` 看它的属性，判断它属于哪一层、谁会消费它。

## 本章重点整理

- 47 个方言按职责分六类；正文深入 20 个，其余按需查表。
- 三种加法 = 三份契约：标量（`arith`）、shaped（`linalg`）、机器（`llvm`）。
- 方言边界 = 类型系统 + 内存模型 + 可降级目标 + 验证/接口。
- 每个 pass 只消费它认识的 op；不匹配就静默跳过，不报错。
- `linalg` 的元素级实现仍是 `arith`；`affine` 的出口是 `arith + scf + memref`。
- `index` 在 LLVM 侧默认是 `i64`，但它不是 `i64` 的别名。

## 自我检核

- [ ] 我能不看表说出 arith / linalg / llvm 三种加法的类型契约
- [ ] 我能解释 `linalg.add` 的 region 里为什么是 `arith.addf`
- [ ] 我能说出 bufferization 改变了什么、没改变什么
- [ ] 我能预测 `lower-affine` 之后哪三个方言还在
- [ ] 我能解释 `convert-linalg-to-loops` 对 tensor 为什么静默
- [ ] 我能把 47 个方言按六类粗略归类
- [ ] 我能说出至少两个“注册但没加载”会导致的错误

## 延伸阅读

- **[MLIR Dialects 索引](https://mlir.llvm.org/docs/Dialects/)**
  - **读哪里**：左侧列表里逐个 dialect 的 op/type 定义。
  - **学什么**：每个 op 的操作数约束与类型系统。
  - **和本章的关联**：本章表格是“索引的索引”，查具体 op 回到这里。

- **[MLIR Language Reference](https://mlir.llvm.org/docs/LangRef/)**
  - **读哪里**：Builtin Types、Modules、Regions 三节。
  - **学什么**：`module`、`index`、`tensor`/`memref` 的正式定义。
  - **和本章的关联**：解释“为什么 builtin 不降级”与“index 为什么目标相关”。

- **[Linalg Dialect 文档](https://mlir.llvm.org/docs/Dialects/Linalg/)**
  - **读哪里**：Structured ops、indexing maps、iterator types。
  - **学什么**：`linalg.add` 的 region 与 maps 如何描述一次计算。
  - **和本章的关联**：示例二的 generic dump 就是这些概念的实例。

- **[Bufferization 文档](https://mlir.llvm.org/docs/Bufferization/)**
  - **读哪里**：One-Shot Bufferize 的动机与约束。
  - **学什么**：tensor → memref 的契约，以及为什么不能随便混用。
  - **和本章的关联**：本章“值世界 → 地址世界”的完整展开。

- **[MLIR 论文（arXiv:2002.11054）](https://arxiv.org/abs/2002.11054)**
  - **读哪里**：第 2–3 节，dialect 组合与多级 IR 的设计论证。
  - **学什么**：为什么要把“层”做成数据而不是继承层次。
  - **和本章的关联**：给这张地图补上设计动机。

---

地图画完了，下一步是把它用起来：给你一段混合 IR，先预测每个 op 的层，再跑 pipeline 验证。练习 A 会复现本章的三个观察——bufferization 的切换点、`convert-linalg-to-loops` 的静默跳过、以及未知 dialect 的两种报错。

→ [练习 A：读写 MLIR 与观察 pass](./practice-a-ir-and-passes.md)
