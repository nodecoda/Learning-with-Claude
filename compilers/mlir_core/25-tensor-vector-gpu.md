# Ch 25 Tensor、Vector 与 GPU

> **目标**：把三种“形状抽象”分清：`tensor` 是值，`vector` 是寄存器形状，`gpu` 是设备分层。你能读写 tensor 的 empty/extract/insert/pad，用 vector 的 transfer_read/write/broadcast/contract 描述 SIMD 计算，解释 `gpu.module/launch/func` 的关系，并跑通 vector 的打印与 lowering；GPU 的执行部分标 guided。

## 为什么需要这个？

同一块数据，在 MLIR 里会以三种完全不同的身份出现：

- **tensor**：SSA 值，有形状、有元素类型，**没有地址**。可以切片、插入、填充，但不能 `load`。
- **vector**：寄存器里的形状，通常是 1-D/2-D 小数组。它不是内存，`transfer_read/write` 才是它与内存的边界。
- **gpu**：设备代码的容器与执行模型。`gpu.module` 装 kernel，`gpu.launch` 描述网格，`gpu.func` 是设备函数。

分不清这三者，会写出层级错误的 pipeline：对 tensor 直接跑 `convert-linalg-to-loops`，静默跳过（Ch 23 已实测）；忘了先降 vector 就期待 `transfer_read` 变成 `llvm.load`；把 `gpu-kernel-outlining` 塞进 `func.func(...)`，报 “restricted to 'builtin.module'”。

本章的目标是建立**边界感**：哪个 op 属于值世界，哪个属于寄存器世界，哪个属于设备世界；跨界时谁负责转换。

## 先建立直觉：三种“形状观”

| 抽象 | 类型例子 | 有地址吗 | 谁负责跨界 |
|---|---|---|---|
| tensor | `tensor<4x8xf32>` | 没有 | `one-shot-bufferize` |
| memref | `memref<4x8xf32>` | 有 | `finalize-memref-to-llvm` |
| vector | `vector<4x8xf32>` | 没有 | `vector.transfer_read/write` |
| gpu | `gpu.module` / `gpu.func` | 设备侧有 | `gpu-kernel-outlining`、`convert-gpu-to-*` |

常用心智图：`tensor → bufferize → memref → transfer_read → vector → lowering → llvm`；GPU 是独立分支：`gpu.launch → outline → gpu.module/gpu.func → nvvm/rocdl/spirv`。

## tensor：值的世界

| op | 作用 | 关键点 |
|---|---|---|
| `tensor.empty` | 创建指定形状的 tensor | **未初始化**，不是零填充 |
| `tensor.extract_slice` | 从 tensor 切一块 | 结果仍是 tensor，无地址 |
| `tensor.insert_slice` | 把一块插回 tensor | 形状必须匹配 |
| `tensor.pad` | 在边界补值 | 补的值由 region 决定 |

元素级还有 `tensor.extract` / `tensor.insert`，用于取/放单个元素；slice 版操作子张量，元素版操作标量。

```mlir
func.func @tensor_ops(%t: tensor<4x8xf32>) -> tensor<6x10xf32> {
  %e = tensor.empty() : tensor<4x8xf32>
  %s = tensor.extract_slice %t[1, 2] [2, 4] [1, 1] : tensor<4x8xf32> to tensor<2x4xf32>
  %i = tensor.insert_slice %s into %e[1, 2] [2, 4] [1, 1] : tensor<2x4xf32> into tensor<4x8xf32>
  %p = tensor.pad %i low[1, 1] high[1, 1] {
  ^bb0(%arg0: index, %arg1: index):
    %c = arith.constant 0.0 : f32
    tensor.yield %c : f32
  } : tensor<4x8xf32> to tensor<6x10xf32>
  return %p : tensor<6x10xf32>
}
```

逐行读：`tensor.empty()` 只声明“我要一个 4×8 的 f32 张量”，内容未定义；`extract_slice %t[1,2] [2,4] [1,1]` 从 offset `(1,2)` 取 `2×4`，步长 `(1,1)`；`insert_slice` 把它插到 `%e` 的 `(1,2)`；`tensor.pad` 上下左右各补 1，补的值由 `tensor.yield` 给出，结果是 `6×10`。

`tensor.empty` 不是 `tensor.fill`。它没有初始化语义；直接读未写入的位置，结果是未定义的。`tensor.pad` 的 region 才是“补什么值”的明确契约。

> 源码依据：`mlir/include/mlir/Dialect/Tensor/IR/TensorOps.td:264`（`empty`）、`:362`（`extract_slice`）、`:838`（`insert_slice`）、`:1266`（`pad`）；元素级在 `:327`（`extract`）、`:789`（`insert`）。

## vector：寄存器形状

Vector 是“一个值，形状固定，通常映射到 SIMD 寄存器”。它和 tensor 的区别不在形状，而在用途：tensor 描述整体计算，vector 描述一条可向量化的指令。

| op | 作用 |
|---|---|
| `vector.transfer_read` | 从 memref/tensor 读一个切片到 vector |
| `vector.transfer_write` | 把 vector 写回 memref/tensor |
| `vector.broadcast` | 把低维 vector 广播到高维 |
| `vector.contract` | 向量收缩（矩阵乘、点积的 SIMD 形式） |

```mlir
#m0 = affine_map<(d0, d1, d2) -> (d0, d2)>
#m1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#m2 = affine_map<(d0, d1, d2) -> (d0, d1)>

func.func @vector_demo(%A: memref<4x8xf32>, %B: memref<8x16xf32>, %C: memref<4x16xf32>) {
  %c0 = arith.constant 0 : index
  %f0 = arith.constant 0.0 : f32
  %a = vector.transfer_read %A[%c0, %c0], %f0 {in_bounds = [true, true]} : memref<4x8xf32>, vector<4x8xf32>
  %b = vector.transfer_read %B[%c0, %c0], %f0 {in_bounds = [true, true]} : memref<8x16xf32>, vector<8x16xf32>
  %acc = vector.transfer_read %C[%c0, %c0], %f0 {in_bounds = [true, true]} : memref<4x16xf32>, vector<4x16xf32>
  %c = vector.contract {indexing_maps = [#m0, #m1, #m2], iterator_types = ["parallel", "parallel", "reduction"], kind = #vector.kind<add>}
      %a, %b, %acc : vector<4x8xf32>, vector<8x16xf32> into vector<4x16xf32>
  %bc = vector.broadcast %a : vector<4x8xf32> to vector<2x4x8xf32>
  vector.transfer_write %c, %C[%c0, %c0] {in_bounds = [true, true]} : vector<4x16xf32>, memref<4x16xf32>
  return
}
```

两个关键点：

1. **`transfer_read` 的第二个操作数是 padding 值。** 越界时用它填；`in_bounds = [true, true]` 表示“我保证不越界”，于是 lowering 可以省略边界检查。承诺错了就是未定义行为。
2. **`transfer_write` 写 memref 时没有结果。** 写 tensor 时会有结果 tensor（值语义）。

`vector.broadcast` 的 rank 可以增加：`vector<4x8xf32>` → `vector<2x4x8xf32>`，新增的是前导维；源形状必须匹配目标的后缀维。

> 源码依据：`mlir/include/mlir/Dialect/Vector/IR/VectorOps.td:1252`（`transfer_read`）、`:1506`（`transfer_write`）、`:379`（`broadcast`）、`:48`（`contract`）。

## gpu：三层结构

| 层 | op | 跑在哪 | 作用 |
|---|---|---|---|
| 容器 | `gpu.module` | host IR 里 | 装设备代码的 symbol table |
| 设备函数 | `gpu.func` | GPU | kernel 本体，带 `kernel` 属性 |
| 启动 | `gpu.launch` | host | 描述 grid/block 的区域 |
| 启动调用 | `gpu.launch_func` | host | outlining 之后的调用形式 |

```mlir
func.func @host(%A: memref<16xf32>) {
  %c1 = arith.constant 1 : index
  %c16 = arith.constant 16 : index
  %f0 = arith.constant 0.0 : f32
  gpu.launch blocks(%bx, %by, %bz) in (%gx = %c1, %gy = %c1, %gz = %c1)
             threads(%tx, %ty, %tz) in (%txn = %c16, %tyn = %c1, %tzn = %c1) {
    %tid = gpu.thread_id x
    memref.store %f0, %A[%tid] : memref<16xf32>
    gpu.terminator
  }
  return
}
```

`blocks(...)` 声明网格有 1×1×1 个 block，`threads(...)` 声明每个 block 有 16×1×1 个线程；`gpu.thread_id x` 取当前线程编号，`gpu.terminator` 结束 launch 区域。

`gpu.launch` 是 **host 侧的结构化区域**，不是 kernel 本身。`gpu-kernel-outlining` 把它拆成 host 侧 `gpu.launch_func` 和设备侧 `gpu.module @name { gpu.func @name(...) kernel ... }`。

> 源码依据：`GPUOps.td:802`（`gpu.launch`）、`:1542`（`gpu.module`）、`:348`（`gpu.func`）、`:605`（`gpu.launch_func`）、`:214`（`thread_id`）、`:169`（`block_id`）。

## 底层机制：lowering 路径

### vector → scf → llvm

`vector.transfer_read/write` 不是 LLVM 指令。它们要先降到循环或 `vector.load/store`：

- `--convert-vector-to-scf` 把高维 transfer 拆成按行循环 + 低维 transfer；`--convert-vector-to-llvm` 把 vector op 降到 `llvm.load/store`、`llvm.extractelement/insertelement`、`llvm.intr.vector.reduce.*`。

`vector.contract` 的 lowering 由 `--vector-contract-lowering` 控制：

| 模式 | 行为 |
|---|---|
| `dot`（默认） | 逐步降到更细的 contract 和 dot-product |
| `outerproduct` | 降到 `vector.outerproduct` |
| `parallelarith` | reduction 维全部展开成元素级运算 |
| `llvmintr` | 直接降到 `llvm.intr.matrix.multiply` |

源码入口：`mlir/lib/Dialect/Vector/Transforms/LowerVectorContract.cpp:239`（OuterProduct）、`:287`（Dot）、`:316`（渐进式 lowering）。

### gpu → nvvm/rocdl/spirv

设备侧：NVIDIA 用 `gpu.module(convert-gpu-to-nvvm)`，AMD 用 `gpu.module(convert-gpu-to-rocdl)`，SPIR-V 用 `convert-gpu-to-spirv`。host 侧 `gpu-to-llvm` 把 `gpu.launch_func` 的操作数降成 LLVM 类型；真正生成 fatbin/cubin 需要 `gpu-module-to-binary`，它调用 `ptxas`/ROCm 工具链。

## 示例一：tensor op 的真实打印

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\tensor.mlir
```

实测（节选）：

```mlir
func.func @tensor_ops(%arg0: tensor<4x8xf32>) -> tensor<6x10xf32> {
  %0 = tensor.empty() : tensor<4x8xf32>
  %extracted_slice = tensor.extract_slice %arg0[1, 2] [2, 4] [1, 1] : tensor<4x8xf32> to tensor<2x4xf32>
  %inserted_slice = tensor.insert_slice %extracted_slice into %0[1, 2] [2, 4] [1, 1] : tensor<2x4xf32> into tensor<4x8xf32>
  %padded = tensor.pad %inserted_slice low[1, 1] high[1, 1] {
  ^bb0(%arg1: index, %arg2: index):
    %cst = arith.constant 0.000000e+00 : f32
    tensor.yield %cst : f32
  } : tensor<4x8xf32> to tensor<6x10xf32>
  return %padded : tensor<6x10xf32>
}
```

所有结果都是 SSA 值，没有 memref；offset/size/stride 都是显式属性；`tensor.pad` 的 region 参数是输出坐标（这里未使用），`tensor.yield` 给出填充值；形状从 `4x8` 变成 `6x10`。

## 示例二：vector 的打印与 lowering

```powershell
& "$mlir\mlir-opt.exe" .\vector.mlir
```

实测（关键部分）：

```mlir
#map = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
module {
  func.func @vector_demo(%arg0: memref<4x8xf32>, %arg1: memref<8x16xf32>, %arg2: memref<4x16xf32>) {
    %c0 = arith.constant 0 : index
    %cst = arith.constant 0.000000e+00 : f32
    %0 = vector.transfer_read %arg0[%c0, %c0], %cst {in_bounds = [true, true]} : memref<4x8xf32>, vector<4x8xf32>
    %1 = vector.transfer_read %arg1[%c0, %c0], %cst {in_bounds = [true, true]} : memref<8x16xf32>, vector<8x16xf32>
    %2 = vector.transfer_read %arg2[%c0, %c0], %cst {in_bounds = [true, true]} : memref<4x16xf32>, vector<4x16xf32>
    %3 = vector.contract {indexing_maps = [#map, #map1, #map2], iterator_types = ["parallel", "parallel", "reduction"], kind = #vector.kind<add>} %0, %1, %2 : vector<4x8xf32>, vector<8x16xf32> into vector<4x16xf32>
    %4 = vector.broadcast %0 : vector<4x8xf32> to vector<2x4x8xf32>
    vector.transfer_write %3, %arg2[%c0, %c0] {in_bounds = [true, true]} : vector<4x16xf32>, memref<4x16xf32>
    return
  }
}
```

`--convert-vector-to-scf` 会把 transfer 拆成按行循环：

```mlir
scf.for %arg3 = %c0 to %c4 step %c1 {
  %8 = vector.transfer_read %arg0[%arg3, %c0], %cst {in_bounds = [true]} : memref<4x8xf32>, vector<8xf32>
  memref.store %8, %0[%arg3] : memref<4xvector<8xf32>>
}
```

这是实测节选。注意 `convert-vector-to-scf` **不降 contract**，只拆 transfer；contract 留给后续 contract lowering。下面用一个可完整贴出的 vadd 例子。

```mlir
func.func @vadd(%A: memref<4xf32>, %B: memref<4xf32>, %C: memref<4xf32>) {
  %c0 = arith.constant 0 : index
  %f0 = arith.constant 0.0 : f32
  %a = vector.transfer_read %A[%c0], %f0 {in_bounds = [true]} : memref<4xf32>, vector<4xf32>
  %b = vector.transfer_read %B[%c0], %f0 {in_bounds = [true]} : memref<4xf32>, vector<4xf32>
  %s = arith.addf %a, %b : vector<4xf32>
  vector.transfer_write %s, %C[%c0] {in_bounds = [true]} : vector<4xf32>, memref<4xf32>
  return
}
```

跑 `--convert-vector-to-llvm`：

```powershell
& "$mlir\mlir-opt.exe" .\vadd.mlir --pass-pipeline='builtin.module(func.func(convert-vector-to-llvm))'
```

实测：

```mlir
%0 = builtin.unrealized_conversion_cast %arg2 : memref<4xf32> to !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
%1 = builtin.unrealized_conversion_cast %arg1 : memref<4xf32> to !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
%2 = builtin.unrealized_conversion_cast %arg0 : memref<4xf32> to !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
%c0 = arith.constant 0 : index
%3 = builtin.unrealized_conversion_cast %c0 : index to i64
%4 = llvm.extractvalue %2[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
%5 = llvm.getelementptr %4[%3] : (!llvm.ptr, i64) -> !llvm.ptr, f32
%6 = llvm.load %5 {alignment = 4 : i64} : !llvm.ptr -> vector<4xf32>
%7 = llvm.extractvalue %1[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
%8 = llvm.getelementptr %7[%3] : (!llvm.ptr, i64) -> !llvm.ptr, f32
%9 = llvm.load %8 {alignment = 4 : i64} : !llvm.ptr -> vector<4xf32>
%10 = arith.addf %6, %9 : vector<4xf32>
%11 = llvm.extractvalue %0[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
%12 = llvm.getelementptr %11[%3] : (!llvm.ptr, i64) -> !llvm.ptr, f32
llvm.store %10, %12 {alignment = 4 : i64} : vector<4xf32>, !llvm.ptr
return
```

关键观察：`transfer_read` 变成 `llvm.load ... -> vector<4xf32>`，`transfer_write` 变成 `llvm.store`，`vector<4xf32>` 直接作为 LLVM 向量类型保留。`memref` 还没降完，所以有 `unrealized_conversion_cast`；这需要后续 `finalize-memref-to-llvm`。

contract 的 lowering：对 `vector<1x1xf32>` 跑 `--pass-pipeline='builtin.module(func.func(convert-vector-to-llvm))'`，实测核心三行：

```mlir
func.func @dot1(%a: vector<1x1xf32>, %b: vector<1x1xf32>, %c: vector<1x1xf32>) -> vector<1x1xf32> {
  %r = vector.contract {indexing_maps = [#m0, #m1, #m2], iterator_types = ["parallel", "parallel", "reduction"], kind = #vector.kind<add>}
      %a, %b, %c : vector<1x1xf32>, vector<1x1xf32> into vector<1x1xf32>
  return %r : vector<1x1xf32>
}
```

```mlir
%5 = arith.mulf %3, %4 : vector<1xf32>
%7 = "llvm.intr.vector.reduce.fadd"(%6, %5) <{fastmathFlags = #llvm.fastmath<none>}> : (f32, vector<1xf32>) -> f32
%13 = arith.addf %12, %arg2 : vector<1x1xf32>
```

先做元素乘，再用 `vector.reduce.fadd` 沿 reduction 维求和，最后把结果加到累加器 `%arg2`。这就是 `kind = add` + `reduction` 维的具体含义。

## 示例三：gpu 分层与 outlining

打印 `gpu.mlir`（节选）：

```mlir
func.func @host(%arg0: memref<16xf32>) {
  %c1 = arith.constant 1 : index
  %c16 = arith.constant 16 : index
  %cst = arith.constant 0.000000e+00 : f32
  gpu.launch blocks(%arg1, %arg2, %arg3) in (%arg7 = %c1, %arg8 = %c1, %arg9 = %c1) threads(%arg4, %arg5, %arg6) in (%arg10 = %c16, %arg11 = %c1, %arg12 = %c1) {
    %thread_id_x = gpu.thread_id x
    memref.store %cst, %arg0[%thread_id_x] : memref<16xf32>
    gpu.terminator
  }
  return
}
```

跑 outlining：

```powershell
& "$mlir\mlir-opt.exe" .\gpu.mlir --pass-pipeline='builtin.module(gpu-kernel-outlining)'
```

实测（节选）：

```mlir
module attributes {gpu.container_module} {
  func.func @host(%arg0: memref<16xf32>) {
    ...
    gpu.launch_func  @host_kernel::@host_kernel blocks in (%c1, %c1, %c1) threads in (%c16, %c1, %c1)  args(%cst : f32, %arg0 : memref<16xf32>)
  }
  gpu.module @host_kernel {
    gpu.func @host_kernel(%arg0: f32, %arg1: memref<16xf32>) kernel attributes {known_block_size = array<i32: 16, 1, 1>, known_grid_size = array<i32: 1, 1, 1>} {
      %thread_id_x = gpu.thread_id x
      ...
      memref.store %arg0, %arg1[%thread_id_x_0] : memref<16xf32>
      gpu.return
    }
  }
}
```

三个变化：`module` 多了 `gpu.container_module` 属性；`gpu.launch` 变成 host 侧 `gpu.launch_func`；kernel 被搬进 `gpu.module` 的 `gpu.func ... kernel`，并自动注入 `block_id/thread_id/grid_dim/block_dim`。

继续降到 NVVM：

```powershell
& "$mlir\mlir-opt.exe" .\gpu.mlir `
  --pass-pipeline='builtin.module(gpu-kernel-outlining,gpu.module(convert-gpu-to-nvvm),gpu-to-llvm)'
```

实测（节选）：

```mlir
module attributes {gpu.container_module} {
  llvm.func @host(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64) {
    ...
    gpu.launch_func  @host_kernel::@host_kernel blocks in (%6, %6, %6) threads in (%7, %6, %6) : i64 args(%8 : f32, %9 : !llvm.ptr, %10 : !llvm.ptr, %11 : i64, %12 : i64, %13 : i64)
    llvm.return
  }
  gpu.module @host_kernel {
    llvm.func @host_kernel(%arg0: f32, %arg1: !llvm.ptr, %arg2: !llvm.ptr, %arg3: i64, %arg4: i64, %arg5: i64) attributes {gpu.kernel, gpu.known_block_size = array<i32: 16, 1, 1>, gpu.known_grid_size = array<i32: 1, 1, 1>, nvvm.kernel, nvvm.maxntid = array<i32: 16, 1, 1>} {
      %0 = nvvm.read.ptx.sreg.tid.x range <i32, 0, 16> : i32
      ...
      llvm.store %arg0, %2 : f32, !llvm.ptr
      llvm.return
    }
  }
}
```

设备侧出现 `nvvm.read.ptx.sreg.tid.x`，host 侧仍是 `gpu.launch_func`，但参数已经变成 LLVM 类型。

> **guided**：本章**不执行** GPU 代码。生成 fatbin/cubin 需要 `gpu-module-to-binary` 加 CUDA/ROCm 工具链（`ptxas`、`hipcc` 等），在 GPU 上运行还需要驱动和 `mlir-runner` 的 CUDA 支持。本机只有 `mlir-opt`，所以“执行”只给路径，不给实测输出。

层级放错会直接报错：

```powershell
& "$mlir\mlir-opt.exe" .\gpu.mlir --pass-pipeline='builtin.module(func.func(gpu-kernel-outlining))'
```

```text
error: Can't add pass 'GpuKernelOutliningPass' restricted to 'builtin.module'
on a PassManager intended to run on 'func.func', did you intend to nest?
```

同理，`convert-gpu-to-nvvm` 只能放在 `gpu.module(...)` 里。pass 的嵌套是作用域契约，不是语法糖。

## 对比与取舍

**tensor vs memref，vector vs linalg**：tensor 是值，可复制、可返回、无别名问题；memref 是地址，有布局、有别名、可原地写，bufferization 是桥（Ch 20）。linalg 描述“算什么”，不承诺怎么映射；vector 描述“用多宽的寄存器算”，已经做了映射决定；先 tiling 再 vectorization 是常见顺序，直接全向量化可能生成超出硬件支持的巨型 vector。

**gpu.module vs gpu.launch**：`gpu.module` 是设备代码容器，最终被 `convert-gpu-to-nvvm/rocdl/spirv` 消费；`gpu.launch` 是 host 侧启动描述，最终 outline 成 `launch_func`。两者可以在同一个 `builtin.module` 里共存，`gpu.container_module` 属性标记这种混合状态。

## 踩雷集锦

1. **把 `tensor.empty` 当零初始化。** 它没有初始化语义；需要零就显式 `tensor.fill` 或 `linalg.fill`。
2. **`transfer_read` 的 `in_bounds` 乱写。** 承诺不越界但实际越界，是未定义行为；lowering 不会替你检查。
3. **以为 `convert-vector-to-scf` 会降 contract。** 实测 contract 原样保留；它只拆 transfer。
4. **把 `gpu-kernel-outlining` 放进 `func.func`。** 实测直接报层级错误。
5. **把 `gpu-to-llvm` 当成二进制生成器。** 它只降 IR；二进制要靠 `gpu-module-to-binary` 和外部工具链。

## 进阶：再往深一层

- **GPU 目标相关 lowering**：`GPUToNVVM`、`GPUToROCDL`、`GPUToSPIRV` 的差异主要在 intrinsics、内存空间和同步原语；`gpu.async`/`async` 用于重叠拷贝与计算。

## 动手练习

### 练习 1：改 pad 的边界

**任务规格**：把 `tensor.mlir` 的 `low[1, 1] high[1, 1]` 改成 `low[0, 2] high[3, 0]`，预测并验证结果形状。

**测试用例**：`tensor.pad` 属性变为 `low[0, 2] high[3, 0]`；输出类型为 `tensor<7x10xf32>`；verifier 通过。

**分步骤**：

1. 改 `tensor.pad` 属性；
2. 同步改函数返回类型和 `return` 类型；
3. 跑 `mlir-opt`，确认 verifier 通过。

<details>
<summary>参考解答</summary>

行：`4 + 0 + 3 = 7`；列：`8 + 2 + 0 = 10`，所以是 `tensor<7x10xf32>`。只改 pad 不改函数签名，会报 “specified type 'tensor<6x10xf32>' does not match the inferred type 'tensor<7x10xf32>'”。

</details>

**常见卡点**：忘记同步改函数返回类型。

**自我检核**：`tensor.pad` 的 region 参数是什么坐标系？

### 练习 2：把 vadd 从 4 lane 改成 8 lane

**任务规格**：把所有 `vector<4xf32>` 改成 `vector<8xf32>`，memref 改成 `memref<8xf32>`，重跑 `convert-vector-to-llvm`。

**测试用例**：输出中出现 `llvm.load ... -> vector<8xf32>` 与 `llvm.store ... : vector<8xf32>`；两个输入 memref 都是 `memref<8xf32>`；没有标量 `addf`。

**分步骤**：

1. 改类型；
2. 保持 `in_bounds = [true]`；
3. 跑 pass，对比 `llvm.load` 的向量宽度。

<details>
<summary>参考解答</summary>

所有 `4xf32` 都要改：两个输入 memref、输出 memref、两个 transfer_read、transfer_write、`vector<4xf32>` 的 addf。少改一处就类型不匹配。

</details>

**常见卡点**：只改 vector 类型，忘了 memref 形状；transfer_read 的 memref 与 vector 必须兼容。

**自我检核**：8 lane 一定比 4 lane 快吗？取决于什么？

### 练习 3：跑通 GPU 的 IR 分层（不执行）

**任务规格**：对 `gpu.mlir` 跑 outlining 和 `gpu.module(convert-gpu-to-nvvm)` + `gpu-to-llvm`，确认 host/device 分离。

**测试用例**：输出中有 `gpu.module @host_kernel`、`nvvm.read.ptx.sreg.tid.x`，host 侧有 `gpu.launch_func`。

**分步骤**：

1. 跑 outlining；
2. 跑完整 pipeline，用 `Select-String 'gpu.module|nvvm\.|gpu.launch_func'` 检查；
3. 记录哪些部分没有实测（二进制生成、执行）。

<details>
<summary>参考解答</summary>

```powershell
& "$mlir\mlir-opt.exe" .\gpu.mlir --pass-pipeline='builtin.module(gpu-kernel-outlining)'
& "$mlir\mlir-opt.exe" .\gpu.mlir `
  --pass-pipeline='builtin.module(gpu-kernel-outlining,gpu.module(convert-gpu-to-nvvm),gpu-to-llvm)'
```

第二条命令可跑，输出 NVVM IR；`gpu-module-to-binary` 需要 CUDA 工具链，执行需要 GPU 驱动，均标 guided。

</details>

**常见卡点**：把 `convert-gpu-to-nvvm` 放在 `builtin.module(...)` 而不是 `gpu.module(...)`，会报 “restricted to 'gpu.module'”。

**自我检核**：`gpu.launch` 和 `gpu.launch_func` 分别在哪一侧？

### 练习 4：元素级 extract / insert

**任务规格**：用 `tensor.extract` 读 `tensor<4xf32>` 下标 1 的元素，加标量后用 `tensor.insert` 写回。

**测试用例**：打印含 `tensor.extract %arg0[%c1]`、`tensor.insert ... into %arg0[%c1]`，返回 `tensor<4xf32>`。

**分步骤**：写 extract → 接 `arith.addf` → 用 insert 写回并 return。

<details>
<summary>参考解答</summary>
extract 产标量，insert 产新 tensor；两者无地址语义，原样打印。
</details>
**常见卡点**：把 insert 当原地写。**自我检核**：`extract_slice` 与 `extract` 的结果类型差在哪？

## 本章重点整理

- tensor 是值：`empty/extract_slice/insert_slice/pad` 都不涉及地址。
- vector 是寄存器形状：`transfer_read/write` 是内存边界，`broadcast/contract` 是计算。
- `vector.contract` 与 `linalg.generic` 结构相似，但 contract 已经做了向量化决定。
- `convert-vector-to-scf` 只拆 transfer；contract 由 contract lowering 处理。
- `convert-vector-to-llvm` 把 transfer 变成 `llvm.load/store`；本章的 `1x1` contract 降成 `mulf + reduce.fadd + addf`。
- `gpu.module` 装设备代码，`gpu.func` 是 kernel，`gpu.launch` 是 host 侧启动描述。
- `gpu-kernel-outlining` 是 module pass；`convert-gpu-to-nvvm` 是 gpu.module pass。
- GPU 二进制生成与执行需要外部工具链，本章标 guided。

## 自我检核

- [ ] 我能说出 tensor 与 memref 的三个区别
- [ ] 我能解释 `tensor.empty` 为什么不是零初始化
- [ ] 我能写出一个 `tensor.pad` 并算出结果形状
- [ ] 我能解释 `transfer_read` 的 padding 值与 `in_bounds` 各自的作用
- [ ] 我能说出 contract 的 maps、iterator types、kind 分别是什么
- [ ] 我能复现 vadd 的 `llvm.load/store` lowering
- [ ] 我能说清 `gpu.launch` 与 `gpu.func` 的分工
- [ ] 我知道哪些 GPU 步骤在本机不可执行

## 延伸阅读

- **[Tensor Dialect 文档](https://mlir.llvm.org/docs/Dialects/Tensor/)**
  - **读哪里**：`empty`、`extract_slice`、`insert_slice`、`pad` 的 op 定义。
  - **学什么**：值语义 tensor 的完整操作集。
  - **和本章的关联**：示例一的语法依据。
- **[Vector Dialect 文档](https://mlir.llvm.org/docs/Dialects/Vector/)**
  - **读哪里**：transfer、contract、broadcast 三节。
  - **学什么**：vector 的维度语义与 mask/in_bounds。
  - **和本章的关联**：示例二的输入与 lowering 目标。
- **[Vector 源码：VectorOps.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Dialect/Vector/IR/VectorOps.td)**
  - **读哪里**：`Vector_TransferReadOp`、`Vector_ContractionOp`。
  - **学什么**：属性、约束、verifier。
  - **和本章的关联**：解释为什么某些 vector 形状不被接受。
- **[GPU Dialect 文档](https://mlir.llvm.org/docs/Dialects/GPU/)**
  - **读哪里**：`gpu.module`、`gpu.func`、`gpu.launch`、memory space 映射。
  - **学什么**：设备代码的组织方式。
  - **和本章的关联**：示例三的分层依据。
- **[VectorToLLVM 源码](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/VectorToLLVM/ConvertVectorToLLVM.cpp)**
  - **读哪里**：`populateVectorToLLVMConversionPatterns` 与 transfer/contract 的 pattern。
  - **学什么**：每个 vector op 最终变成哪些 LLVM op。
  - **和本章的关联**：示例二的逐行对照。

---

形状、寄存器和设备三层都见过了。下一章把“变换本身”变成 IR：用 transform dialect 写调度，而不是把 pass 顺序硬编码在脚本里。

→ [Ch 26 Transform dialect](./26-transform-dialect.md)
