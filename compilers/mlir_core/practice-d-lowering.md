# 练习 D：把自定义方言降到 LLVM

> **目标**：独立跑通一条完整链路：`tensor + linalg` → `memref` → `llvm` dialect → LLVM IR → AArch64 汇编。你要能解释每个阶段的关键 IR、每一条命令的退出码，以及 target triple、data layout、ABI 和 pass 顺序各自在哪一步生效。自定义方言的 C++ 部分标 `guided`，其余全部 `实测`。

## 为什么需要这个？

Ch 20 到 Ch 22 是分章讲的：bufferization、conversion、translate/llc。真实项目里它们是**一条流水线**，而且错误通常不在单章内部，而在接缝上：

- bufferize 出来的布局和后面的 ABI 不匹配；
- pass 嵌套位置不对，某个 conversion 静默跳过；
- `.ll` 没有 triple，`llc` 按宿主目标生成；
- 自定义 op 没实现 bufferization 接口，分析器只能保守拷贝；
- 文件带 BOM，`mlir-opt` 第一行就报 `unexpected character`。

这些问题的共同点是：单看某一章都“懂”，串起来就定位不到。本练习用一段 4×4 的 `axpy` 把链路固定下来，每一步都有可比的输出和行数。你之后遇到更大的程序，排查方法是一样的：**先把阶段切开，再逐段确认方言、类型、ABI 和目标。**

练习分两段：

1. **A 段（实测）**：输入是等价的 `linalg.generic`，从 bufferize 一路跑到 AArch64 汇编。
2. **B 段（guided）**：把 `toy.axpy` 这个自定义 op 降到 A 段的 `linalg.generic`。需要编译 out-of-tree pass，给出 Ch 0 的 VS2022/CMake 配方。

## 任务规格

输入固定为 `axpy.mlir`：

```mlir
#map = affine_map<(d0, d1) -> (d0, d1)>
module attributes {
  llvm.target_triple = "aarch64-unknown-linux-gnu",
  llvm.data_layout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
} {
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
}
```

产物与验收标准：

| 产物 | 验收点 |
|---|---|
| `axpy.memref.mlir` | 签名是 `memref<4x4xf32>`，函数体只剩 `linalg`/`arith` |
| `axpy.loops.mlir` | 出现 `cf.br`、`cf.cond_br`、`memref.load/store` |
| `axpy.llvm.mlir` | 只有 `llvm`/`builtin`，没有 `memref`、`linalg`、`scf`、`arith` |
| `axpy.ll` | 有 `target triple`、`target datalayout`、`define ptr @axpy` |
| `axpy.s` | 有 `ldr s0`、`fmul`、`fadd`、`str s0`、`ret` |

目标：AArch64 Linux，`aarch64-unknown-linux-gnu`。所有命令都在 `.scratch\practice-d` 下执行，下文用相对路径。

## 测试用例

### T1：完整链路（正常路径）

按本练习的 S0–S7 跑完，期望：

- `mlir-opt` 退出码 `0`；
- `axpy.llvm.mlir` 共 91 行；
- `axpy.ll` 共 80 行；
- `axpy.s` 共 45 行；
- 汇编里出现 `ldr s0, [x0, x11]`、`fmul s0, s0, s1`、`fadd s0, s0, s1`、`str s0, [x2, x11]`。

### T2：循环携带 tensor（边界情况）

`loop.mlir`：

```mlir
func.func @loop_alloc(%v: tensor<4xf32>) -> tensor<4xf32> {
  ...
  %r = scf.for %i = %c0 to %c4 step %c1 iter_args(%acc = %e) -> (tensor<4xf32>) {
    %t = tensor.empty() : tensor<4xf32>
    %0 = linalg.generic ... ins(%acc) outs(%t) { ... } -> tensor<4xf32>
    scf.yield %0 : tensor<4xf32>
  }
  return %r : tensor<4xf32>
}
```

不开 `allow-return-allocs-from-loops` 时，期望失败；开了之后，期望每轮一个 `memref.alloc`。

### T3：bare pointer 与布局

同一段 `axpy`，比较两种函数边界布局：

- `function-boundary-type-conversion=identity-layout-map`：签名是 `memref<4x4xf32>`，可以用 `use-bare-ptr-memref-call-conv`；
- 默认 `infer-layout-map`：签名是 `memref<4x4xf32, strided<[?, ?], offset: ?>>`，bare pointer 管线会失败。

这个用例的目的不是“跑通”，而是让你看到 **ABI 选择必须和布局绑定**。

### T4：错序 pipeline

把 `convert-func-to-llvm` 放到 `convert-scf-to-cf` 前面，期望：

- IR 里残留 `scf.for`；
- `mlir-translate` 报 `Dialect 'scf' not found for custom op 'scf.for'`；
- 退出码非零。

### T5：去掉 triple

把 `axpy.ll` 的 `target datalayout` 和 `target triple` 两行删掉，再跑 `llc`。期望退出码仍是 `0`，但生成的是宿主目标 `x86_64-pc-windows-msvc` 的汇编，出现 `%rcx`/`%rdx`/`%r8` 和 `.def`/`.endef`。

## 分段步骤

### S0：确认工具链

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
& $llc --version
```

实测输出（前四行）：

```text
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
  Default target: x86_64-pc-windows-msvc
```

`Optimized build` 意味着没有 assertions，也没有 test-only pass；`-debug-only`、`test-analysis-only` 这类顶层开关不可用。`--one-shot-bufferize="test-analysis-only"` 是 pass 自己的选项，可以用的部分我们会在 T2 里看到。

### S1：写输入

把上面的 `axpy.mlir` 保存为 UTF-8 无 BOM。PowerShell 里安全的写法：

```powershell
$text = Get-Content -Raw -Encoding UTF8 .\axpy.mlir
[IO.File]::WriteAllText((Resolve-Path .\axpy.mlir), $text, (New-Object System.Text.UTF8Encoding($false)))
```

检查首字节：

```powershell
[IO.File]::ReadAllBytes((Resolve-Path .\axpy.mlir))[0..2]
```

期望输出 `35 32 67`（`# c` 的 ASCII），不是 `239 187 191`。

### S2：bufferize

```powershell
& "$mlir\mlir-opt.exe" .\axpy.mlir `
  --one-shot-bufferize="bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map allow-return-allocs-from-loops" `
  -o .\axpy.memref.mlir
```

实测输出共 13 行，关键是签名：

```mlir
func.func @axpy(%arg0: memref<4x4xf32>, %arg1: memref<4x4xf32>, %arg2: memref<4x4xf32>) -> memref<4x4xf32> {
  linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel", "parallel"]} ins(%arg0, %arg1 : memref<4x4xf32>, memref<4x4xf32>) outs(%arg2 : memref<4x4xf32>) {
  ^bb0(%in: f32, %in_0: f32, %out: f32):
    %0 = arith.mulf %in, %in_0 : f32
    %1 = arith.addf %0, %out : f32
    linalg.yield %1 : f32
  }
  return %arg2 : memref<4x4xf32>
}
```

`identity-layout-map` 把边界统一成静态连续布局；这是后面用裸指针 ABI 的前提。没有任何 `memref.alloc`，因为 `outs(%arg2)` 可以原地写。

### S3：降 linalg 和 scf

```powershell
$p = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map allow-return-allocs-from-loops},func.func(convert-linalg-to-loops,convert-scf-to-cf))'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$p -o .\axpy.loops.mlir
```

实测输出共 31 行，循环已经完全展开成 `cf`：

```mlir
%c0 = arith.constant 0 : index
%c4 = arith.constant 4 : index
%c1 = arith.constant 1 : index
cf.br ^bb1(%c0 : index)
^bb1(%0: index):  // 2 preds: ^bb0, ^bb5
  %1 = arith.cmpi slt, %0, %c4 : index
  cf.cond_br %1, ^bb2, ^bb6
^bb2:  // pred: ^bb1
  cf.br ^bb3(%c0 : index)
^bb3(%2: index):  // 2 preds: ^bb2, ^bb4
  %3 = arith.cmpi slt, %2, %c4 : index
  cf.cond_br %3, ^bb4, ^bb5
^bb4:  // pred: ^bb3
  %4 = memref.load %arg0[%0, %2] : memref<4x4xf32>
  %5 = memref.load %arg1[%0, %2] : memref<4x4xf32>
  %6 = memref.load %arg2[%0, %2] : memref<4x4xf32>
  %7 = arith.mulf %4, %5 : f32
  %8 = arith.addf %7, %6 : f32
  memref.store %8, %arg2[%0, %2] : memref<4x4xf32>
  %9 = arith.addi %2, %c1 : index
  cf.br ^bb3(%9 : index)
```

这一步结束后，`linalg` 和 `scf` 都没了。`index` 还在，`memref` 还在；它们分别由 arith 和 memref 的 conversion 处理。

### S4：降到 llvm dialect

```powershell
$p = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map allow-return-allocs-from-loops},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm{use-bare-ptr-memref-call-conv},reconcile-unrealized-casts)'
& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$p -o .\axpy.llvm.mlir
```

实测输出共 91 行，函数签名：

```mlir
llvm.func @axpy(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: !llvm.ptr) -> !llvm.ptr {
```

`use-bare-ptr-memref-call-conv` 让三个 memref 参数都变成裸指针。函数体里仍然要重建 descriptor，因为循环里的地址计算需要 sizes/strides：

```mlir
%0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
%1 = llvm.insertvalue %arg1, %0[0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
%2 = llvm.insertvalue %arg1, %1[1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
%3 = llvm.mlir.constant(0 : index) : i64
%4 = llvm.insertvalue %3, %2[2] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
```

循环核心：

```mlir
^bb4:  // pred: ^bb3
  %46 = llvm.extractvalue %25[1] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
  %47 = llvm.mlir.constant(4 : index) : i64
  %48 = llvm.mul %42, %47 overflow<nsw, nuw> : i64
  %49 = llvm.add %48, %44 overflow<nsw, nuw> : i64
  %50 = llvm.getelementptr inbounds|nuw %46[%49] : (!llvm.ptr, i64) -> !llvm.ptr, f32
  %51 = llvm.load %50 : !llvm.ptr -> f32
```

结尾：

```mlir
^bb6:  // pred: ^bb1
  %73 = llvm.extractvalue %38[0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
  llvm.return %73 : !llvm.ptr
```

检查没有残留方言：

```powershell
Select-String -Path .\axpy.llvm.mlir -Pattern 'memref|linalg|scf|arith|cf\.'
```

实测没有匹配，计数为 `0`。这就是 `mlir-translate` 能接受的输入。

### S5：translate 成 LLVM IR

```powershell
& "$mlir\mlir-translate.exe" --mlir-to-llvmir .\axpy.llvm.mlir -o .\axpy.ll
```

实测 `axpy.ll` 共 80 行，开头：

```llvm
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
target triple = "aarch64-unknown-linux-gnu"

define ptr @axpy(ptr %0, ptr %1, ptr %2) {
```

descriptor 的 `insertvalue` 原样保留：

```llvm
%4 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } poison, ptr %1, 0
%5 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %4, ptr %1, 1
%6 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %5, i64 0, 2
%7 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %6, i64 4, 3, 0
```

block argument 变成了 `phi`：

```llvm
25:                                               ; preds = %55, %3
  %26 = phi i64 [ %56, %55 ], [ 0, %3 ]
  %27 = icmp slt i64 %26, 4
  br i1 %27, label %28, label %57
```

结尾返回裸指针：

```llvm
57:                                               ; preds = %25
  %58 = extractvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %24, 0
  ret ptr %58
}
```

### S6：生成 AArch64 汇编

```powershell
& $llc -O2 .\axpy.ll -o .\axpy.s
```

实测退出码 `0`，`axpy.s` 共 45 行。完整输出：

```asm
	.file	"LLVMDialectModule"
	.text
	.globl	axpy                            // -- Begin function axpy
	.p2align	2
	.type	axpy,@function
axpy:                                   // @axpy
	.cfi_startproc
// %bb.0:
	mov	x8, xzr
	mov	x9, xzr
	b	.LBB0_2
.LBB0_1:                                //   in Loop: Header=BB0_2 Depth=1
	add	x9, x9, #1
	add	x8, x8, #16
.LBB0_2:                                // =>This Loop Header: Depth=1
                                        //     Child Loop BB0_4 Depth 2
	cmp	x9, #3
	b.gt	.LBB0_5
// %bb.3:                               // %.preheader
                                        //   in Loop: Header=BB0_2 Depth=1
	mov	x10, xzr
	mov	x11, x8
	cmp	x10, #3
	b.gt	.LBB0_1
.LBB0_4:                                //   Parent Loop BB0_2 Depth=1
                                        // =>  This Inner Loop Header: Depth=2
	ldr	s0, [x0, x11]
	ldr	s1, [x1, x11]
	add	x10, x10, #1
	fmul	s0, s0, s1
	ldr	s1, [x2, x11]
	fadd	s0, s0, s1
	str	s0, [x2, x11]
	add	x11, x11, #4
	cmp	x10, #3
	b.le	.LBB0_4
	b	.LBB0_1
.LBB0_5:
	mov	x0, x2
	ret
.Lfunc_end0:
	.size	axpy, .Lfunc_end0-axpy
	.cfi_endproc
                                        // -- End function
	.section	".note.GNU-stack","",@progbits
```

### S7：验收

```powershell
Select-String -Path .\axpy.s -Pattern 'ldr\s+s0|fmul|fadd|str\s+s0|ret'
```

实测输出：

```text
27:ldr	s0, [x0, x11]
30:fmul	s0, s0, s1
32:fadd	s0, s0, s1
33:str	s0, [x2, x11]
40:ret
```

注意 `x0`、`x1`、`x2`：AArch64 的前三个整数参数寄存器。`mov x0, x2` 把 `c` 的指针放进返回值寄存器，因为函数返回的是 `%arg2`。

## 参考解答

<details>
<summary>点击展开：一条命令链与关键 IR</summary>

把 S2–S6 合成一条脚本：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
$pipeline = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map allow-return-allocs-from-loops},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm{use-bare-ptr-memref-call-conv},reconcile-unrealized-casts)'

& "$mlir\mlir-opt.exe" .\axpy.mlir --pass-pipeline=$pipeline -o .\axpy.llvm.mlir
& "$mlir\mlir-translate.exe" --mlir-to-llvmir .\axpy.llvm.mlir -o .\axpy.ll
& $llc -O2 .\axpy.ll -o .\axpy.s
```

逐行解释 pipeline：

| 片段 | 作用 | 不能省的理由 |
|---|---|---|
| `one-shot-bufferize{...}` | tensor → memref | 后面的 conversion 只处理 buffer 语义 |
| `function-boundary-type-conversion=identity-layout-map` | 边界统一静态布局 | 裸指针 ABI 只接受 identity layout |
| `convert-linalg-to-loops` | linalg → scf + memref | LLVM 不认 structured op |
| `convert-scf-to-cf` | scf → cf | 下一步只处理 cf |
| `convert-cf-to-llvm` | cf → llvm.br/cond_br | 否则 translate 报 `cf` 未注册 |
| `convert-arith-to-llvm` | arith → llvm 运算 | 顺带把 `index` 定成 `i64` |
| `finalize-memref-to-llvm` | memref → ptr/descriptor | 23.1.4 没有 `convert-memref-to-llvm` |
| `convert-func-to-llvm{use-bare-ptr...}` | 重写函数签名 | 决定 ABI 是裸指针还是 descriptor |
| `reconcile-unrealized-casts` | 折掉临时 cast | 最后必须清干净，translate 不认 |

关键数字：

- `axpy.memref.mlir`：13 行；
- `axpy.loops.mlir`：31 行；
- `axpy.llvm.mlir`：91 行；
- `axpy.ll`：80 行；
- `axpy.s`：45 行。

`axpy.llvm.mlir` 里为什么有 descriptor，明明参数是裸指针？因为 `use-bare-ptr-memref-call-conv` 只改**调用约定**：参数和返回值用裸指针，函数体内部的 `memref.load/store` 仍然需要 layout 信息，所以要从裸指针重建 descriptor。这个区别在 Ch 21 的 bare pointer 示例里已经出现过。

`axpy.ll` 里的 `insertvalue ... poison` 不是多余代码。它逐字段填入 descriptor：allocated pointer、aligned pointer、offset、两个 size、两个 stride。源码依据：`mlir/include/mlir/Conversion/LLVMCommon/MemRefBuilder.h:104-108`。

</details>

## 自定义方言部分（guided）

A 段用 `linalg.generic` 代替了自定义 op。真实项目里，你通常先把自己的 op 降到 `linalg`，再复用 A 段。下面给出最小设计，**没有在本机编译**，所以标 `guided`。

假设方言叫 `toy`，op 是：

```mlir
%0 = toy.axpy %a, %b, %c : tensor<4x4xf32>, tensor<4x4xf32>, tensor<4x4xf32> -> tensor<4x4xf32>
```

它和 A 段的 `linalg.generic` 语义相同。最小 TableGen 定义（复用练习 C 的 `Toy_Dialect` / `Toy_Op`）：

```tablegen
def Toy_AxpyOp : Toy_Op<"axpy", [Pure]> {
  let arguments = (ins AnyRankedTensor:$a, AnyRankedTensor:$b,
                       AnyRankedTensor:$c);
  let results = (outs AnyRankedTensor:$result);
  let assemblyFormat = "$a `,` $b `,` $c attr-dict `:` type($a) `,` type($b) `,` type($c) `->` type($result)";
}
```

conversion pattern 的骨架：

```cpp
struct AxpyOpLowering : OpConversionPattern<toy::AxpyOp> {
  using OpConversionPattern::OpConversionPattern;

  LogicalResult matchAndRewrite(toy::AxpyOp op, OpAdaptor adaptor,
                                ConversionPatternRewriter &rewriter) const override {
    Location loc = op.getLoc();
    auto resultType = cast<RankedTensorType>(op.getType());
    Value empty = rewriter.create<tensor::EmptyOp>(
        loc, resultType.getShape(), resultType.getElementType());
    AffineMap id = AffineMap::getMultiDimIdentityMap(2, rewriter.getContext());
    auto generic = rewriter.create<linalg::GenericOp>(
        loc, resultType, ValueRange{adaptor.getA(), adaptor.getB()},
        ValueRange{empty}, ArrayRef<AffineMap>{id, id, id},
        ArrayRef<utils::IteratorType>{utils::IteratorType::parallel,
                                      utils::IteratorType::parallel},
        /*doc=*/"", /*libraryCall=*/"",
        [&](OpBuilder &b, Location loc, ValueRange args) {
          Value mul = b.create<arith::MulFOp>(loc, args[0], args[1]);
          Value add = b.create<arith::AddFOp>(loc, mul, args[2]);
          b.create<linalg::YieldOp>(loc, add);
        });
    rewriter.replaceOp(op, generic.getResults());
    return success();
  }
};
```

pass 的合法性配置：

```cpp
ConversionTarget target(getContext());
target.addIllegalDialect<toy::ToyDialect>();
target.addLegalDialect<linalg::LinalgDialect, tensor::TensorDialect,
                       arith::ArithDialect>();
RewritePatternSet patterns(&getContext());
patterns.add<AxpyOpLowering>(&getContext());
if (failed(applyPartialConversion(getOperation(), target, std::move(patterns))))
  signalPassFailure();
```

这段代码的关键不是 API 细节，而是边界：**toy 必须被标记为 illegal，linalg/tensor/arith 必须 legal**。否则 conversion 会静默成功但什么也不改，后面的 pipeline 就会在 `linalg` 之前看到 `toy.axpy`。

构建配方（Ch 0 的 CMake 路线，`guided`；本机 VS2022 为 `D:\VStudio\2022\Enterprise`，MSVC 14.43）：

```bat
call "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" >nul
cmake -G Ninja -B build ^
  -DMLIR_DIR="D:\Learning-with-Claude\.scratch\mlir-env\Library\lib\cmake\mlir" ^
  -DCMAKE_PREFIX_PATH="D:\Learning-with-Claude\.scratch\mlir-env\Library" ^
  -DCMAKE_BUILD_TYPE=Release ^
  "-DCMAKE_EXE_LINKER_FLAGS=/LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/um/x64 /LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/ucrt/x64"
cmake --build build
```

三个本机约束：

- `CMAKE_PREFIX_PATH` 必须指向 conda 的 `Library`，否则 `find_package(ZLIB)` 失败；
- 链接要补 `ntdll.lib` 和 SDK `10.0.17763` 的 `um\x64`/`ucrt\x64`，否则 `LLVMSupport` 或 `shell32.lib` 报错；
- 源码含非 ASCII 时加 `/utf-8`，避免代码页 936 的 C4819。

官方包是 Optimized build：没有断言，也没有 test dialect。你可以编译 out-of-tree pass，但不能依赖 `-debug-only` 或 `mlir-opt` 的 test pass 做验证。

编出 `toy-opt.exe` 后，假设 pass 注册为 `--convert-toy-to-linalg`，命令是：

```powershell
& .\build\toy-opt.exe .\toy-axpy.mlir --convert-toy-to-linalg | & "$mlir\mlir-opt.exe" --pass-pipeline=$pipeline
```

这一步会把 `toy.axpy` 换成 A 段的 `linalg.generic`，后面完全复用 S2–S6。若暂时不想编译，直接把 `toy.axpy` 手写成 `linalg.generic`，A 段就是它的等价验证。

## 五条卡点

**1. 文件带 BOM，第一行就失败**

```text
.\bom.mlir:1:1: error: unexpected character
```

`mlir-opt` 不跳过 UTF-8 BOM。用 `[IO.File]::WriteAllText(..., UTF8Encoding($false))`，不要用会写 BOM 的默认 `Out-File` 旧行为。

**2. 裸指针 ABI 配了动态布局**

去掉 `function-boundary-type-conversion=identity-layout-map`，保留 `use-bare-ptr-memref-call-conv`：

```text
error: type of return operand 0 ('!llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>')
doesn't match function result type ('memref<4x4xf32, strided<[?, ?], offset: ?>>')
```

裸指针 ABI 只对静态 identity 布局成立。先统一布局，再谈 ABI。

**3. 把 module 级 pass 塞进 `func.func(...)`**

```text
error: Can't add pass 'ConvertControlFlowToLLVMPass' restricted to 'builtin.module'
on a PassManager intended to run on 'func.func', did you intend to nest?
```

`convert-cf-to-llvm`、`convert-arith-to-llvm`、`finalize-memref-to-llvm`、`convert-func-to-llvm` 都是 module 级。只有 `convert-linalg-to-loops`、`convert-scf-to-cf` 放在 `func.func(...)` 里。

**4. `.ll` 没有 triple，`llc` 静默用宿主目标**

删掉 `target triple` 后，`llc` 仍然退出 `0`，但生成的是 x86_64 Windows 汇编：

```text
	.def	axpy;
	.globl	axpy                            # -- Begin function axpy
	movq	%r8, %rax
```

有 triple 时才是 `x0`/`x1`/`x2` 和 `.note.GNU-stack`。`llc` 不会替你检查“这是不是你想要的目标”。

**5. 残留方言，translate 才报错**

错序 pipeline 留下 `scf.for` 后：

```text
<stdin>:33:5: error: Dialect `scf' not found for custom op 'scf.for'
```

`mlir-opt` 能打印这段 IR，也会退出 `0`；错误要到 `mlir-translate` 才出现。判断标准不是“mlir-opt 没报错”，而是“IR 里还有没有非 `llvm` 方言”。

## 自我检核

- [ ] 我能不看笔记写出 S2–S6 的完整 pipeline
- [ ] 我能解释 `identity-layout-map` 为什么是裸指针 ABI 的前提
- [ ] 我能指出 `axpy.llvm.mlir` 里 descriptor 的五个字段和 `insertvalue` 下标
- [ ] 我能解释 `axpy.s` 里 `mov x0, x2` 为什么对应 `return %arg2`
- [ ] 我能复现 T2 的 `Yield operand #0` 并用 `allow-return-allocs-from-loops` 修好
- [ ] 我能复现 T5，并说明 `llc` 为什么不报错
- [ ] 我能说出 `mlir-translate` 拒绝哪些残留方言
- [ ] 我能把 `toy.axpy` 的 conversion 边界说清楚：谁 illegal、谁 legal

## 延伸阅读

- **`mlir/docs/Bufferization.md`**
  - **读哪里**：One-Shot Bufferize 与 Function Boundary 两节。
  - **学什么**：`bufferize-function-boundaries` 和布局选项的语义。
  - **和本练习的关联**：S2 的签名变化就是这两节的直接结果。

- **`mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp:456-509`**
  - **读哪里**：`convertFuncSignature`、`useBarePtrCallConv`、`wrapWithCInterface`。
  - **学什么**：函数签名如何从 memref 变成裸指针或 descriptor。
  - **和本练习的关联**：S4 的 `!llvm.ptr` 签名和 T3 的失败都来自这里。

- **`mlir/lib/Target/LLVMIR/ModuleTranslation.cpp:2511-2535`**
  - **读哪里**：data layout 与 target triple 的读取。
  - **学什么**：module 属性如何进入 `llvm::Module`。
  - **和本练习的关联**：S5 的 `target datalayout`/`target triple` 不是 translate 猜的。

- **`mlir/examples/standalone/`**
  - **读哪里**：`CMakeLists.txt`、`include/Standalone/StandaloneOps.td`、`standalone-opt`。
  - **学什么**：out-of-tree 方言和 pass 的标准目录组织。
  - **和本练习的关联**：B 段的 `toy-opt` 可以照这个骨架搭。

→ [Ch 23 Linalg：structured op](./23-linalg.md)
