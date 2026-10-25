# Ch 21 降到 LLVM dialect

> **目标**：把 Ch 20 得到的 memref 程序完整降成 `llvm` dialect，能自己排出正确的 conversion 顺序，读懂函数签名从 `func.func` 到 `llvm.func` 的变化，并能解释 ABI、data layout、`index` 位宽和 `unrealized_conversion_cast` 各自的角色。

## 为什么需要这个？

Ch 20 结束时，IR 里还有 `func.func`、`linalg.generic`、`scf.for`、`arith.addf`、`memref.load`。这些都不是 LLVM 能接受的形式。`mlir-translate --mlir-to-llvmir` 只接受有 LLVM translation interface 的 op（实际上就是 `llvm` dialect），所以中间必须有一串 conversion。

这串 conversion 不是“一个总 pass”，而是按 dialect 拆开的：

```text
func.func + linalg + scf + arith + memref
  │ convert-linalg-to-loops      linalg → scf/arith/memref
  │ convert-scf-to-cf            scf → cf + block argument
  │ convert-cf-to-llvm           cf → llvm.br/cond_br
  │ convert-arith-to-llvm        arith → llvm.add/fadd/icmp...
  │ finalize-memref-to-llvm      memref → llvm.ptr + descriptor
  │ convert-func-to-llvm         func.func → llvm.func + ABI
  │ reconcile-unrealized-casts   折掉临时 cast
  ▼
llvm dialect
```

每一步只承诺自己那段。顺序错了，轻则留下 `unrealized_conversion_cast`，重则某个 pass 根本看不到目标 op，IR 被静默跳过。本章的核心判断力就是：**看到一段混合 IR，知道下一个该跑哪个 conversion，以及为什么。**

## 先建立直觉：conversion 是“分 dialect 的替换”，不是“整体重写”

先记住三条规则：

1. **每个 conversion pass 只合法化自己负责的 dialect。** `convert-arith-to-llvm` 不会碰 `memref`，`finalize-memref-to-llvm` 不会碰 `func`。
2. **中间状态允许混合。** `llvm.func` 的函数体里可以暂时残留 `arith.addi`，只要后面的 pass 会处理它。
3. **类型转换可能产生临时 cast。** `index` → `i64`、memref → descriptor 都不是同一种类型，conversion 框架需要 `unrealized_conversion_cast` 做占位，最后由 `reconcile-unrealized-casts` 折掉。

所以一条正确的 pipeline 通常长这样：

```text
结构化 op → 控制流 → 标量 → 内存 → 函数/ABI → 清理 cast
```

这个顺序不是唯一，但有一个硬约束：**消费者必须在生产者之后。** `finalize-memref-to-llvm` 要把 memref 变成 LLVM 类型，`convert-func-to-llvm` 要把函数签名里的 memref 一起变；如果函数先降了、memref 后降，函数体里就会出现 descriptor 和 memref 并存的局面，只能靠 cast 过渡。

## 核心概念一：每个 pass 负责什么

| pass | 输入 | 输出 | 备注 |
|---|---|---|---|
| `convert-linalg-to-loops` | buffer 语义的 `linalg` | `scf.for` + `memref.load/store` | tensor 版本会被跳过 |
| `convert-scf-to-cf` | `scf.for/if/while` | `cf.br/cond_br` + block argument | 只处理结构化控制流 |
| `convert-cf-to-llvm` | `cf` | `llvm.br/cond_br` | module 级 pass |
| `convert-arith-to-llvm` | `arith` | `llvm` 标量运算 | 处理 `index` 位宽 |
| `finalize-memref-to-llvm` | `memref` | `llvm.ptr` + descriptor | 23.1.4 没有 `convert-memref-to-llvm` |
| `convert-func-to-llvm` | `func.func/call/return` | `llvm.func/call/return` | 重写 ABI，处理 C wrapper |
| `reconcile-unrealized-casts` | `builtin.unrealized_conversion_cast` | 尽量折掉 | 必须最后跑 |

两个容易踩的命名问题：

- **`finalize-memref-to-llvm` 不是 `convert-memref-to-llvm`。** 本机 23.1.4 的 `mlir-opt --help` 里只有前者。很多教程写 `convert-memref-to-llvm`，那是更老或别的分支的名字；照抄会得到 “unknown pass”。
- **`convert-func-to-llvm`、`convert-cf-to-llvm`、`convert-arith-to-llvm`、`finalize-memref-to-llvm` 都是 module 级 pass。** 把它们塞进 `func.func(...)` 会报：

```text
error: Can't add pass 'ConvertControlFlowToLLVMPass' restricted to 'builtin.module' on a PassManager intended to run on 'func.func', did you intend to nest?
```

只有 `convert-linalg-to-loops`、`convert-scf-to-cf` 这类可以放在 `func.func(...)` 里。本章的 pipeline 就是按这个限制排的。

## 核心概念二：memref 的 ABI 是 descriptor，不是裸指针

`memref<4x4xf32>` 在 LLVM 层没有对应类型。MLIR 的约定是把它拆成一个 descriptor 结构：

```text
{ allocated_ptr, aligned_ptr, offset, sizes[rank], strides[rank] }
```

源码依据（路径相对 `.scratch\llvm-23.1.4-src`）：`mlir/include/mlir/Conversion/LLVMCommon/MemRefBuilder.h:33-108`。`MemRefDescriptor` 继承 `StructBuilder`，注释明确列了字段顺序：allocated pointer、aligned pointer、offset、`<rank>` sizes、`<rank>` strides（`:104-108`）。

这带来两个直接后果：

1. **函数签名会膨胀。** 一个 `memref<4x4xf32, strided<[?, ?], offset: ?>>` 参数在 `llvm.func` 里可能变成 7 个标量参数（两个指针、一个 offset、两个 size、两个 stride），返回值则是一个 `!llvm.struct`。
2. **descriptor 是按值传的。** 默认 ABI 把 descriptor 拆成多个参数传递；callee 重建 descriptor 后再做 GEP/load/store。大结构按值传的代价由目标 ABI 决定。

另一种选择是 **bare pointer**：只传元素指针，不传 offset/size/stride。`convert-func-to-llvm{use-bare-ptr-memref-call-conv}` 打开它，前提是 memref 静态形状且布局可退化成 identity。源码依据：`mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp:58-63` 的 `shouldUseBarePtrCallConv`，以及 `mlir/include/mlir/Conversion/Passes.td:549-555` 的选项定义。

bare pointer 的代价是信息丢失：callee 不知道 stride 和 offset，只能假设连续。它适合和 C 交互、或者你确定所有 buffer 都是 identity layout 的场景；不适合动态 shape/非连续 slice。

## 核心概念三：`unrealized_conversion_cast` 是“欠条”，不是错误

conversion 过程中，类型系统会出现短暂的不一致：

- `index` 在 MLIR 里是目标相关类型，降到 LLVM 时必须选 `i32` 或 `i64`；
- memref 在 MLIR 里是一个类型，降到 LLVM 时变成结构体或多个标量；
- 函数签名先降、函数体后降时，入口参数的类型会对不上。

`unrealized_conversion_cast` 就是给这些不一致打的欠条：它声明“我知道这里类型不同，先欠着，后面会还”。它 **不是** 可执行的转换，`mlir-translate` 不认它；如果最后没折掉，translate 会报错或被下游拒绝。

`reconcile-unrealized-casts` 的工作是折叠“A→B→A”这种可逆链：如果一串 cast 的输入输出类型最终能对上，就用原始值替换。源码依据：`mlir/lib/Conversion/ReconcileUnrealizedCasts/ReconcileUnrealizedCasts.cpp:27-40`，注释里的例子就是 `!A → !B → !C → !A` 最后全部折掉。

注意它只能折“类型能对上”的链，不能凭空把 memref 变成 descriptor。真正的类型转换由各个 conversion pass 的 `TypeConverter` 完成；reconcile 只是清理残余。

## 核心概念四：data layout 与 `index` 位宽

`index` 不是 `i64` 的别名。它是一个目标相关的整数类型，位宽由目标决定：

- 默认 `index-bitwidth=0` 表示“按 machine word”，在本机 x86_64 上是 64 位；
- 显式 `index-bitwidth=32` 会把它降成 `i32`；
- 这个选择必须和 data layout 的 pointer size 一致，否则 GEP/descriptor 的偏移会算错。

实测对比（同一个 `func.func @idx(%i: index) -> index`）：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
Write-Output '=== default ==='
& "$mlir\mlir-opt.exe" .\idx.mlir --pass-pipeline='builtin.module(convert-func-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)'
Write-Output '=== index-bitwidth=32 ==='
& "$mlir\mlir-opt.exe" .\idx.mlir --pass-pipeline='builtin.module(convert-func-to-llvm{index-bitwidth=32},convert-arith-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)'
```

实测输出（两次的签名与加法）：

```mlir
llvm.func @idx(%arg0: i64) -> i64 {
  %0 = llvm.add %arg0, %arg0 : i64
```

```mlir
llvm.func @idx(%arg0: i32) -> i32 {
  %0 = llvm.add %arg0, %arg0 : i32
```

源码依据：`mlir/include/mlir/Conversion/Passes.td:185-187` 定义 `index-bitwidth`，默认 `0` 表示“use size of machine word”；`mlir/lib/Conversion/ArithToLLVM/ArithToLLVM.cpp:714-715` 把选项写进 `LLVMTypeConverter`。

data layout 则是另一层：它描述指针大小、对齐、`i64` 对齐、栈对齐等。MLIR 把它存在 module 的 `llvm.data_layout` 属性里，translate 时原样写进 LLVM IR 的 `target datalayout`。属性名来自 `mlir/include/mlir/Dialect/LLVMIR/LLVMDialect.td:31-32`。本机可以用 `set-llvm-module-datalayout` 附加：

```powershell
$dl = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm,reconcile-unrealized-casts,set-llvm-module-datalayout{data-layout="e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"})'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$dl
```

实测输出开头：

```mlir
module attributes {llvm.data_layout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"} {
```

这个字符串是 AArch64 Linux 的布局（由本机 `clang --target=aarch64-unknown-linux-gnu -S -emit-llvm` 生成，Ch 22 会用到）。它和 x86_64 的布局不同；**data layout 必须和目标 triple 匹配**，否则结构体布局、向量对齐、指针宽度都可能错。

## 底层机制：TypeConverter 如何重写签名

每个 conversion pass 背后都有一个 `TypeConverter`。它做两件事：

1. **类型映射**：把源类型转成一组目标类型。`i32` → `i32`，`index` → `i64`，`memref<4x4xf32>` → `!llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>`。
2. **签名重写**：把函数的参数/返回值按类型映射展开。一个 memref 参数可能展开成 7 个标量，一个 memref 返回值可能打包成一个 struct。

`convert-func-to-llvm` 的签名转换在 `mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp:456-508`：先 `convertFunctionSignature`，再创建 `llvm.func`，最后按需 `wrapWithCInterface`。如果原函数带 `llvm.emit_c_interface`，它会额外生成一个 `_mlir_ciface_*` 包装函数（Ch 22 会实测）。

`finalize-memref-to-llvm` 则负责 memref op 的 lowering。`memref.alloc` 变成 `llvm.call @malloc` + descriptor 构造，`memref.load` 变成 `llvm.extractvalue` + GEP + `llvm.load`。源码依据：`mlir/lib/Conversion/MemRefToLLVM/MemRefToLLVM.cpp:179-213` 里 `memref.alloc` 的 lowering 会创建 descriptor 并替换原 op。

一个关键点：**descriptor 是按值传递的**。callee 收到一堆标量后，第一件事是用 `llvm.insertvalue` 把它们拼回 struct。你在示例一里看到的那一串 `insertvalue` 不是冗余代码，而是 ABI 的直接后果。

## 示例一：完整 pipeline 到 `llvm` dialect

输入仍是 Ch 20 的 `axpy.mlir`：

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

完整 pipeline：

```powershell
$pipeline = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm,reconcile-unrealized-casts)'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$pipeline
```

实测输出（开头与签名）：

```mlir
module {
  llvm.func @axpy(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64, %arg5: i64, %arg6: i64, %arg7: !llvm.ptr, %arg8: !llvm.ptr, %arg9: i64, %arg10: i64, %arg11: i64, %arg12: i64, %arg13: i64, %arg14: !llvm.ptr, %arg15: !llvm.ptr, %arg16: i64, %arg17: i64, %arg18: i64, %arg19: i64, %arg20: i64) -> !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> {
    %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
    %1 = llvm.insertvalue %arg7, %0[0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %2 = llvm.insertvalue %arg8, %1[1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %3 = llvm.insertvalue %arg9, %2[2] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %4 = llvm.insertvalue %arg10, %3[3, 0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %5 = llvm.insertvalue %arg12, %4[4, 0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %6 = llvm.insertvalue %arg11, %5[3, 1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %7 = llvm.insertvalue %arg13, %6[4, 1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    ...
```

中间的计算循环（节选）：

```mlir
    %25 = llvm.mlir.constant(4 : index) : i64
    ...
    %43 = llvm.getelementptr %41[%42] : (!llvm.ptr, i64) -> !llvm.ptr, f32
    %44 = llvm.extractvalue %7[4, 0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %45 = llvm.mul %27, %44 overflow<nsw> : i64
    %46 = llvm.extractvalue %7[4, 1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %47 = llvm.mul %29, %46 overflow<nsw> : i64
    %48 = llvm.add %45, %47 overflow<nsw> : i64
    %49 = llvm.getelementptr inbounds %43[%48] : (!llvm.ptr, i64) -> !llvm.ptr, f32
    %50 = llvm.load %49 : !llvm.ptr -> f32
    ...
    %61 = llvm.fmul %40, %50 : f32
    %62 = llvm.fadd %61, %60 : f32
    ...
    llvm.store %62, %71 : f32, !llvm.ptr
```

返回：

```mlir
  ^bb6:  // pred: ^bb1
    llvm.return %23 : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
  }
}
```

读这段输出时，注意四件事：

1. **签名里没有 memref 了。** 三个 memref 参数被展开成 21 个标量，返回值是一个 descriptor struct。
2. **`%23` 是第三个参数的 descriptor。** 循环最后 `return %23`，说明 in-place 的结果直接返回 `%arg2` 对应的 buffer。
3. **地址计算是显式的。** `extractvalue` 取 stride，`mul`/`add` 算线性下标，`getelementptr inbounds` 得到元素指针。
4. **`overflow<nsw>` 出现了。** `convert-linalg-to-loops`/`memref` lowering 会为部分乘法加 `nsw`（no signed wrap），这是优化信息，不是语义要求。

整份输出实测 91 行。教材里只截了签名、循环和返回；完整文件可以用命令自己生成。

## 示例二：bare pointer ABI 与 `index` 位宽

同一个输入，改成静态 identity 布局 + bare pointer：

```powershell
$pipeline = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm{use-bare-ptr-memref-call-conv},reconcile-unrealized-casts)'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$pipeline
```

实测输出（开头）：

```mlir
module {
  llvm.func @axpy(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: !llvm.ptr) -> !llvm.ptr {
    %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
    %1 = llvm.insertvalue %arg1, %0[0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %2 = llvm.insertvalue %arg1, %1[1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %3 = llvm.mlir.constant(0 : index) : i64
    %4 = llvm.insertvalue %3, %2[2] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    %5 = llvm.mlir.constant(4 : index) : i64
    ...
```

循环末尾：

```mlir
  ^bb6:  // pred: ^bb1
    %73 = llvm.extractvalue %38[0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> 
    llvm.return %73 : !llvm.ptr
  }
}
```

对比示例一：

- 签名从 21 个参数变成 3 个 `!llvm.ptr`，返回值从 struct 变成 `!llvm.ptr`。
- 函数体里仍然要构造 descriptor：bare pointer 只改变 **调用边界**，不改变 callee 内部的寻址方式。
- 只有静态形状 + identity 布局才能这样降；如果 memref 有动态 stride，bare pointer 会丢信息，conversion 会直接拒绝（见练习 D 的 T3）。

再叠加 `index-bitwidth=32`：

```powershell
& "$mlir\mlir-opt.exe" .\idx.mlir --pass-pipeline='builtin.module(convert-func-to-llvm{index-bitwidth=32},convert-arith-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)'
```

实测输出：

```mlir
llvm.func @idx(%arg0: i32) -> i32 {
  %0 = llvm.add %arg0, %arg0 : i32
```

注意 `index-bitwidth` 要同时传给所有会碰 `index` 的 pass。只给 `convert-func-to-llvm` 不给 `convert-arith-to-llvm`，函数签名是 `i32`、函数体里的加法还是 `i64`，reconcile 会留下 cast 或者直接类型不匹配。实际项目里通常由一个 pipeline 构造器统一设置，而不是手写每个 pass 的选项。

## 示例三：错序 pipeline 与 `unrealized_conversion_cast`

把 `convert-func-to-llvm` 提到 `convert-scf-to-cf` 前面：

```powershell
$bad = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops),convert-func-to-llvm,func.func(convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,reconcile-unrealized-casts)'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$bad
```

实测输出（节选）：

```mlir
llvm.func @axpy(%arg0: !llvm.ptr, ...) -> !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> {
  %25 = builtin.unrealized_conversion_cast %24 : i64 to index
  %27 = builtin.unrealized_conversion_cast %26 : i64 to index
  %29 = builtin.unrealized_conversion_cast %28 : i64 to index
  scf.for %arg21 = %25 to %27 step %29 {
    %30 = builtin.unrealized_conversion_cast %arg21 : index to i64
    scf.for %arg22 = %25 to %27 step %29 {
      %31 = builtin.unrealized_conversion_cast %arg22 : index to i64
```

为什么 `convert-scf-to-cf` 没生效？因为它被放在 `func.func(...)` 里，而 `convert-func-to-llvm` 已经把 `func.func` 换成了 `llvm.func`。嵌套 pass 只会在 `func.func` 上跑，找不到目标，于是 `scf.for` 原样留下。`convert-cf-to-llvm` 也找不到 `cf`，因为 `scf` 还没降。

把结果交给 translate：

```powershell
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$bad | & "$mlir\mlir-translate.exe" --mlir-to-llvmir
```

实测报错：

```text
<stdin>:33:5: error: Dialect `scf' not found for custom op 'scf.for'
    scf.for %arg21 = %25 to %27 step %29 {
    ^
<stdin>:33:5: note: Available dialects: acc, arm_neon, arm_sme, arm_sve, builtin, dlti, func, gpu, llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm
```

这条错误和 Ch 0 的 “Dialect `arith' not found” 是同一类：**残留了 `mlir-translate` 不注册的方言**。区别是这次残留的原因是 pass 顺序/嵌套位置，而不是漏跑某个 conversion。

再看“正确顺序但漏掉 reconcile”：

```powershell
$norec = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm)'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$norec
```

实测输出（节选）：

```mlir
%8 = builtin.unrealized_conversion_cast %7 : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> to memref<4x4xf32, strided<[?, ?], offset: ?>>
%17 = builtin.unrealized_conversion_cast %16 : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> to memref<4x4xf32, strided<[?, ?], offset: ?>>
%26 = builtin.unrealized_conversion_cast %25 : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> to memref<4x4xf32, strided<[?, ?], offset: ?>>
%27 = builtin.unrealized_conversion_cast %26 : memref<4x4xf32, strided<[?, ?], offset: ?>> to !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
%31 = builtin.unrealized_conversion_cast %30 : i64 to index
%32 = builtin.unrealized_conversion_cast %31 : index to i64
```

这些 cast 里有几对是“descriptor ↔ memref”和“i64 ↔ index”的往返。`reconcile-unrealized-casts` 能把可逆的链折掉；漏了它，IR 仍然能 verify，但 `mlir-translate` 不认 `builtin.unrealized_conversion_cast`，最终会失败或留下非法 IR。

一个重要区别：

- **错序**会导致“该降的 op 没降”，`scf.for` 还在；
- **漏 reconcile** 会导致“降完了但欠条没还”，`unrealized_conversion_cast` 还在。

两者都表现为“translate 失败”，但修法完全不同：前者调顺序，后者补 pass。

## 对比与取舍

### descriptor ABI vs bare pointer ABI

| 维度 | descriptor（默认） | bare pointer |
|---|---|---|
| 签名 | 每个 memref 展开成多个标量/struct | 每个 memref 一个 `!llvm.ptr` |
| 信息 | 保留 offset、size、stride | 只保留元素指针 |
| 适用 | 动态 shape、非连续 slice、通用库 | 静态 identity 布局、C 互操作 |
| 代价 | 参数多、callee 要重建 descriptor | 丢失布局信息，误用会算错 |

选 bare pointer 不是“更高级”，而是“用信息换简洁”。如果你的 memref 可能来自 `memref.subview` 或动态 shape，别用它。

### `index` 32 位 vs 64 位

32 位 index 能减小 descriptor 和地址计算的开销，适合 wasm32、部分嵌入式目标；64 位是 x86_64/AArch64 的默认。关键是 **所有相关 pass 必须一致**：`convert-func-to-llvm`、`convert-arith-to-llvm`、`finalize-memref-to-llvm` 都要拿到同一个 `index-bitwidth`，否则类型边界会对不上。

### 先 finalize memref 还是先 convert func

本机实测两种顺序都能在补上 reconcile 后产出合法 IR，但推荐 **先 finalize memref，再 convert func**：

- 先 finalize：函数体里的 memref 已经变成 descriptor，`convert-func-to-llvm` 只需处理函数签名和调用，cast 更少。
- 先 convert func：签名先变成 descriptor，函数体里还是 memref，中间状态需要更多 cast 过渡，出错时更难读。

顺序不是教条，原则是“让消费者的输入类型已经稳定”。

## 踩雷集锦

1. **找 `convert-memref-to-llvm`。** 23.1.4 没有这个 pass，只有 `finalize-memref-to-llvm`。用 `--help` 核对，别照抄旧教程。

2. **把 module 级 pass 塞进 `func.func(...)`。** `convert-cf-to-llvm`、`convert-arith-to-llvm`、`finalize-memref-to-llvm`、`convert-func-to-llvm` 都会报 “restricted to 'builtin.module'”。嵌套只留给 `convert-linalg-to-loops`、`convert-scf-to-cf` 这类。

3. **先 `convert-func-to-llvm`，再 `func.func(convert-scf-to-cf)`。** `func.func` 已经变成 `llvm.func`，嵌套 pass 找不到目标，`scf.for` 原样留下，translate 报 “Dialect `scf' not found”。

4. **漏掉 `reconcile-unrealized-casts`。** IR 能 verify，但 `unrealized_conversion_cast` 不是可执行 op；translate 会拒绝。看到 descriptor ↔ memref 的往返 cast，先检查这个 pass 在不在最后。

5. **只给一个 pass 设 `index-bitwidth=32`。** 函数签名变成 `i32`，函数体还是 `i64`，中间全是 cast。统一设置，或者干脆用默认 machine word。

6. **对动态 memref 用 bare pointer。** 静态形状是前提；动态 stride/offset 会被丢掉，callee 按连续内存寻址，结果错误且不会报错。

7. **以为 data layout 只是注释。** 它决定指针宽度、对齐和结构体布局；AArch64 的 layout 拿到 x86_64 上用，可能生成错误的 GEP/栈布局。target triple 和 data layout 要成对出现。

## 进阶：再往深一层

- **看 `--mlir-print-ir-after-all` 的中间态**：在 `finalize-memref-to-llvm` 之后、`convert-func-to-llvm` 之前停下来，你会看到 `llvm.func` 与 `memref` 并存的混合 IR，这是理解 cast 的最好材料。
- **读 `MemRefDescriptor` 的字段顺序**：`MemRefBuilder.h:104-108` 明确列了 allocated/aligned/offset/sizes/strides；对照实测 IR 的 `insertvalue` 下标，能一一对应。
- **试 per-function bare pointer**：除了全局选项，函数上还可以加 `llvm.bareptr` 属性（`FuncToLLVM.cpp:56-62`），只让某个函数走裸指针 ABI。
- **看 C wrapper**：给 `func.func` 加 `llvm.emit_c_interface`，再跑 `convert-func-to-llvm`，会多出一个 `_mlir_ciface_*` 函数。Ch 22 会实测。

## 动手练习

1. **跑通完整 pipeline。** 保存 `axpy.mlir`，执行示例一的命令，确认输出里没有 `memref`、`linalg`、`scf`、`arith`，只有 `llvm` 和 `builtin`。
2. **数参数。** 在示例一的签名里数出三个 memref 各占几个参数，并和 descriptor 字段顺序对上。
3. **切 bare pointer。** 跑示例二，比较签名和函数体；解释为什么函数体里仍然有 descriptor 构造。
4. **制造错序。** 复现示例三的 `scf.for` 残留，再把 `convert-func-to-llvm` 移到最后，确认 translate 成功。
5. **改 index 位宽。** 把 `idx.mlir` 的 pipeline 改成 32 位，确认签名和加法都变成 `i32`；再只改一个 pass，观察 cast。
6. **加 data layout。** 用 `set-llvm-module-datalayout` 给模块加 AArch64 layout，确认输出开头出现 `llvm.data_layout`。

## 本章重点整理

- conversion 按 dialect 分步：linalg → scf → cf → arith → memref → func → reconcile。
- 23.1.4 的 memref 出口是 `finalize-memref-to-llvm`，不是 `convert-memref-to-llvm`。
- memref 的默认 ABI 是 descriptor（allocated/aligned/offset/sizes/strides）；bare pointer 只适用于静态 identity 布局。
- `index` 位宽由 `index-bitwidth` 控制，必须所有相关 pass 一致。
- data layout 存在 `llvm.data_layout`，必须和目标 triple 匹配。
- 错序会留下未降的 op；漏 reconcile 会留下 `unrealized_conversion_cast`。两者修法不同。

## 自我检核

- [ ] 我能不看笔记写出完整 pipeline 的顺序
- [ ] 我能解释为什么 `convert-func-to-llvm` 不能放进 `func.func(...)`
- [ ] 我能说出 descriptor 的五个字段和它们在 IR 里的 `insertvalue` 下标
- [ ] 我能判断一个 memref 能不能用 bare pointer ABI
- [ ] 我知道 `index-bitwidth` 不一致会出现什么现象
- [ ] 我能区分“错序残留 scf”和“漏 reconcile 残留 cast”
- [ ] 我能用 `set-llvm-module-datalayout` 给模块加布局并验证输出
- [ ] 我知道下一步 translate 需要什么条件

## 延伸阅读

- **[MLIR Dialect Conversion 文档](https://mlir.llvm.org/docs/DialectConversion/)**
  - **读哪里**：TypeConverter、ConversionTarget、materialization 三节。
  - **学什么**：1:N 类型转换和 `unrealized_conversion_cast` 的设计。
  - **和本章的关联**：本章的每个 conversion pass 都建立在这套框架上。

- **[FuncToLLVM.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp)**
  - **读哪里**：`shouldUseBarePtrCallConv`、`convertFunctionSignature`、`wrapWithCInterface`。
  - **学什么**：函数签名如何被重写，C wrapper 如何生成。
  - **和本章的关联**：示例一/二的签名差异就来自这里。

- **[MemRefBuilder.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Conversion/LLVMCommon/MemRefBuilder.h)**
  - **读哪里**：`MemRefDescriptor` 的字段注释和 `bufferPtr`。
  - **学什么**：descriptor 的内存布局与地址计算。
  - **和本章的关联**：读懂实测 IR 里那串 `extractvalue`/`GEP` 的钥匙。

- **[LLVM IR Language Reference: Data Layout](https://llvm.org/docs/LangRef.html#data-layout)**
  - **读哪里**：Data Layout 一节的字符串语法。
  - **学什么**：`e-m:e-...` 每个字段的含义。
  - **和本章的关联**：解释 `llvm.data_layout` 为什么要和 triple 匹配。

---

→ [Ch 22 产出 LLVM IR 与后端接缝](./22-llvm-ir-and-backend.md)
