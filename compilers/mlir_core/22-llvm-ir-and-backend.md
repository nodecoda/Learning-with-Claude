# Ch 22 产出 LLVM IR 与后端接缝

> **目标**：把 Ch 21 的 `llvm` dialect 真正 translate 成 LLVM IR，再交给 `llc` 生成 AArch64 汇编；看懂 target triple、data layout、调用约定和 `llvm.func` 属性如何影响输出，并知道 MLIR 到此为止、LLVM 后端从哪开始。

## 为什么需要这个？

Ch 21 结束时，IR 还是 `llvm.func`、`llvm.load`、`llvm.getelementptr`——这些是 **MLIR 的 LLVM dialect**，不是 LLVM IR。两者长得像，但不是同一种格式：

```text
MLIR llvm dialect  ──mlir-translate --mlir-to-llvmir──▶  LLVM IR (.ll)
                                                              │ llc
                                                              ▼
                                                        目标汇编 (.s)
```

这条边界很重要，因为：

- `mlir-opt` 能跑 pass，但不能生成机器码；
- `mlir-translate` 只负责“格式转换”，不做指令选择、寄存器分配；
- `llc` 属于 LLVM 后端，负责从 LLVM IR 到目标汇编；
- target triple / data layout 在 MLIR 里只是 module 属性，到了 LLVM 才真正生效。

很多“MLIR 降到 LLVM 后跑不通”的问题，根源不在 conversion，而在这一层的 ABI 或 target 配置。本章把这条接缝拆开看。

## 先建立直觉：三次“换语言”，三次信息丢失

从源程序到汇编，至少跨三次表示：

```text
tensor/linalg      值语义，形状是类型的一部分
  │ bufferization
memref/linalg      有 buffer、有布局，但仍是 MLIR
  │ conversion
llvm dialect       有指针、descriptor、基本块，仍是 MLIR
  │ mlir-translate
LLVM IR            有 target triple、data layout、调用约定
  │ llc
目标汇编            有寄存器、指令、节区
```

每次跨越都会丢失一些信息，也会引入一些约定：

- bufferization 丢掉“值不可变”，引入别名；
- conversion 丢掉 memref 类型，引入 descriptor/裸指针 ABI；
- translate 丢掉 MLIR 的 region/block argument 表达，引入 phi；
- llc 丢掉 SSA 的无限寄存器假设，引入物理寄存器和栈槽。

本章关心后两次。判断一段 IR 能不能继续往下走，标准不是“看起来像 LLVM”，而是：**类型是否都是 LLVM 类型、有没有残留方言、target 信息是否完整。**

## 核心概念一：`mlir-translate` 只做格式转换

命令：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-translate.exe" --mlir-to-llvmir .\axpy.llvm.mlir
```

它做的事情可以概括为：

1. 遍历 module 里的 `llvm.func`、`llvm.global` 等 op；
2. 把 MLIR 类型转成 `llvm::Type`；
3. 把 block argument 转成 LLVM 的 phi；
4. 把 module 属性里的 triple/data layout 写进 `llvm::Module`；
5. 打印成 `.ll` 文本。

源码依据：`mlir/lib/Target/LLVMIR/ConvertToLLVMIR.cpp:25-35` 注册了 `mlir-to-llvmir`，内部调用 `translateModuleToLLVMIR`，最后 `llvmModule->print(output, nullptr)`。

三个限制：

- **只认有 LLVM translation interface 的 op。** 实际上就是 `llvm` dialect；`func.func` 虽然在注册表里，也会报 `missing LLVMTranslationDialectInterface`。残留 `arith`、`scf`、`memref`、`unrealized_conversion_cast` 都会报错。
- **不做优化。** 它不会替你做常量折叠、DCE、内联；这些要在 MLIR 侧或 LLVM 侧做。
- **不验证目标合法性。** 它可以把一个 AArch64 的 data layout 配到一个 x86 的 triple 上，照样输出文本；错误要到 `llc` 才暴露。

## 核心概念二：target triple 与 data layout 是 module 属性

在 MLIR 里，这两者都是 module 上的 discardable attribute：

```mlir
module attributes {
  llvm.target_triple = "aarch64-unknown-linux-gnu",
  llvm.data_layout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
} {
  ...
}
```

属性名来自 `mlir/include/mlir/Dialect/LLVMIR/LLVMDialect.td:31-32`（源码路径相对 `.scratch\llvm-23.1.4-src`）（`llvm.data_layout`）和 `:84-85`（`llvm.target_triple`）。

translate 时，`ModuleTranslation` 会：

- 读 `llvm.data_layout`，调用 `llvmModule->setDataLayout(...)`；
- 读 `llvm.target_triple`，调用 `llvmModule->setTargetTriple(...)`。

源码依据：`mlir/lib/Target/LLVMIR/ModuleTranslation.cpp:2511-2535`。如果属性不存在，LLVM module 的 triple 为空，`llc` 会退回宿主默认目标。

这意味着：

- **同一份 `.ll`，triple 不同，生成的汇编就不同。** 调用约定、寄存器、指令选择全变。
- **data layout 必须和 triple 匹配。** 它决定指针宽度、对齐、结构体布局；不匹配时可能生成错误的 GEP 或栈布局。
- **MLIR 侧不会替你推导 data layout。** 你可以用 `set-llvm-module-datalayout` 显式附加，也可以让 `llc` 用目标默认值；但两者最好一致。

## 核心概念三：`llvm.func` 的属性如何映射到 LLVM IR

`llvm.func` 支持一组和 LLVM `Function` 对应的属性。常用的几个：

| MLIR 写法 | LLVM IR | 作用 |
|---|---|---|
| `llvm.func internal @f` | `define internal ... @f` | 内部链接，不导出 |
| `llvm.func fastcc @f` | `define fastcc ... @f` | 调用约定 |
| `attributes {dso_local}` | `define dso_local ... @f` | 局部可见性优化 |
| `attributes {llvm.emit_c_interface}` | 额外生成 `_mlir_ciface_f` | C ABI 包装 |
| `llvm.linkage<private>`（generic 形式） | `private` | 私有符号 |

实测：`llvm.func internal fastcc @helper` + `llvm.call fastcc @helper`：

```mlir
llvm.func internal fastcc @helper(%a: i32, %b: i32) -> i32 {
  %0 = llvm.add %a, %b : i32
  llvm.return %0 : i32
}
llvm.func @caller(%a: i32) -> i32 {
  %c1 = llvm.mlir.constant(1 : i32) : i32
  %0 = llvm.call fastcc @helper(%a, %c1) : (i32, i32) -> i32
  llvm.return %0 : i32
}
```

translate 后：

```llvm
define internal fastcc i32 @helper(i32 %0, i32 %1) {
  %3 = add i32 %0, %1
  ret i32 %3
}

define i32 @caller(i32 %0) {
  %2 = call fastcc i32 @helper(i32 %0, i32 1)
  ret i32 %2
}
```

注意 `call` 也要带 `fastcc`。调用约定不是“函数自己的事”，caller 和 callee 必须一致；MLIR 的 `llvm.call` 有独立的 `CConv` 属性，translate 时会 `setCallingConv`（`ModuleTranslation.cpp:2011` 处理函数，`LLVMToLLVMIRTranslation.cpp:490` 处理 call）。

`llvm.emit_c_interface` 是另一条路：它不改变函数本身的 ABI，而是 **额外生成一个 C 兼容的包装函数**。实测：

```mlir
func.func @add(%a: i32, %b: i32) -> i32 attributes {llvm.emit_c_interface} {
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
```

跑 `convert-func-to-llvm` 后：

```mlir
llvm.func @add(%arg0: i32, %arg1: i32) -> i32 attributes {llvm.emit_c_interface} {
  %0 = arith.addi %arg0, %arg1 : i32
  llvm.return %0 : i32
}
llvm.func @_mlir_ciface_add(%arg0: i32, %arg1: i32) -> i32 attributes {llvm.emit_c_interface} {
  %0 = llvm.call @add(%arg0, %arg1) : (i32, i32) -> i32
  llvm.return %0 : i32
}
```

`_mlir_ciface_` 前缀是约定；源码在 `mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp:425-430`（`wrapWithCInterface`）和 `:468-508`（何时触发）。

## 核心概念四：`llc` 的选择：SelectionDAG 还是 GlobalISel

`llc` 默认用 SelectionDAG 做指令选择。加 `-global-isel` 换成 GlobalISel：

```powershell
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"
& $llc -O2 .\axpy.ll -o axpy.s
& $llc -global-isel -O2 .\axpy.ll -o axpy-gisel.s
```

两者都输出 AArch64 汇编，但指令选择路径不同：

- SelectionDAG：先把 IR 降到 DAG，再做模式匹配；
- GlobalISel：IRTranslator → Legalizer → RegBankSelect → InstructionSelect，全程保持 SSA。

本机实测，这个 `axpy` 在 `-global-isel-abort=1` 下也能完整走 GlobalISel（退出码 0），说明它没有回退到 SelectionDAG。更复杂的 IR 可能触发 fallback；`-global-isel-abort=1` 会把 fallback 变成错误，`=2` 更严格。MLIR 侧的 register bank 选择不在本课范围，属于 `globalisel_regbankselect` 课程。

这就是“接缝”的含义：MLIR 只负责产出合法的 LLVM IR；用哪条指令选择管线、开了哪些优化，是 `llc` 的决定。你可以用同一份 `.ll` 对比两条管线，而不需要改 MLIR。

## 底层机制：translate 到底改了什么

`translateModuleToLLVMIR` 的主流程可以按源码顺序读：

1. **建 `llvm::Module`**：读 `llvm.data_layout`，没有就尝试从 `DataLayoutOpInterface` 推导，最后 `setDataLayout`（`ModuleTranslation.cpp:2511-2530`）。
2. **设 triple**：读 `llvm.target_triple`，`setTargetTriple`（`:2532-2535`）。
3. **声明所有函数**：`convertFunctionSignatures` 遍历 `LLVMFuncOp`，建 `llvm::Function`，设置 linkage 和 calling convention（`:2001-2013`）。
4. **转换函数体**：每个 block 的 block argument 变成 phi（`convertBlockImpl`，`:1051-1077`），每条 op 交给对应 dialect 的 translation。
5. **打印**：`llvmModule->print(output, nullptr)`。

三个容易忽略的点：

- **函数先全部声明，再填函数体。** 这样递归调用和互相调用都能找到符号。
- **block argument → phi 是机械转换。** MLIR 用 block argument 表达 SSA 合并，LLVM 用 phi；`CreatePHI` 在 `:1076`。
- **属性转换是白名单式的。** 不认识的 `llvm.*` 属性不会自动变成 LLVM 属性；`convertFunctionAttributes` 只处理映射表里的项。

## 示例一：把 Ch 21 的结果 translate 成 LLVM IR

输入文件 `axpy-dl.mlir`，在 Ch 21 的 bare-pointer pipeline 基础上加了 triple 和 data layout：

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

跑 Ch 21 的完整 pipeline，再 translate：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$pipeline = 'builtin.module(one-shot-bufferize{bufferize-function-boundaries function-boundary-type-conversion=identity-layout-map},func.func(convert-linalg-to-loops,convert-scf-to-cf),convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,convert-func-to-llvm{use-bare-ptr-memref-call-conv},reconcile-unrealized-casts)'
& "$mlir\mlir-opt.exe" .\axpy-dl.mlir --pass-pipeline=$pipeline `
  | & "$mlir\mlir-translate.exe" --mlir-to-llvmir `
  | Out-File -Encoding ascii .\axpy.ll
```

实测输出（`axpy.ll` 开头）：

```llvm
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
target triple = "aarch64-unknown-linux-gnu"

define ptr @axpy(ptr %0, ptr %1, ptr %2) {
  %4 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } poison, ptr %1, 0
  %5 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %4, ptr %1, 1
  %6 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %5, i64 0, 2
  %7 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %6, i64 4, 3, 0
  %8 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %7, i64 4, 4, 0
  %9 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %8, i64 4, 3, 1
  %10 = insertvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %9, i64 1, 4, 1
  ...
```

循环体（节选）：

```llvm
25:                                               ; preds = %55, %3
  %26 = phi i64 [ %56, %55 ], [ 0, %3 ]
  %27 = icmp slt i64 %26, 4
  br i1 %27, label %28, label %57

29:                                               ; preds = %32, %28
  %30 = phi i64 [ %54, %32 ], [ 0, %28 ]
  %31 = icmp slt i64 %30, 4
  br i1 %31, label %32, label %55

32:                                               ; preds = %29
  %33 = extractvalue { ptr, ptr, i64, [2 x i64], [2 x i64] } %17, 1
  %34 = mul nuw nsw i64 %26, 4
  %35 = add nuw nsw i64 %34, %30
  %36 = getelementptr inbounds nuw float, ptr %33, i64 %35
  %37 = load float, ptr %36, align 4
  %48 = fmul float %37, %42
  %49 = fadd float %48, %47
  store float %49, ptr %53, align 4
  %54 = add i64 %30, 1
  br label %29
```

这段 IR 值得逐行看：

- **`define ptr @axpy(ptr, ptr, ptr)`**：bare pointer ABI，三个参数都是裸指针，返回值也是裸指针。
- **`insertvalue ... poison`**：从裸指针重建 memref descriptor。`%17` 是 `b` 的 descriptor，`%10` 是 `a` 的，`%24` 是 `c` 的。
- **`phi`**：MLIR 的 `scf.for` iter_arg/block argument 在这里变成了 phi。
- **`target datalayout` 和 `target triple`**：来自 module 属性，不是 translate 猜的。
- **`getelementptr inbounds nuw float`**：地址计算已经变成 LLVM 的 typed GEP；`inbounds`/`nuw` 是优化信息。

## 示例二：用本机 `llc` 生成 AArch64 汇编

```powershell
& $llc -O2 .\axpy.ll -o axpy-aarch64.s
```

因为 module 里有 `target triple = "aarch64-unknown-linux-gnu"`，`llc` 会按 AArch64 生成，不需要额外 `-mtriple`。

实测输出（`axpy-aarch64.s`，完整 45 行）：

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

读这段汇编：

- **`x0`、`x1`、`x2`** 是 AArch64 的前三个整数参数寄存器，对应 `%0`、`%1`、`%2`。
- **`ldr s0, [x0, x11]`**：`s0` 是 32 位浮点寄存器，`[x0, x11]` 是基址+偏移的寻址。
- **`fmul`/`fadd`**：标量浮点乘加。没有向量化，因为 `linalg` 已经降成标量循环，`llc -O2` 不会自动把它变成 NEON。
- **`mov x0, x2; ret`**：返回值是 `%2`（也就是 `c` 的指针），和 LLVM IR 的 `ret ptr %58` 对应。
- **`.note.GNU-stack`**：Linux ELF 的栈可执行标记；Windows 目标会换成 `.def`/`.endef`。

同一份 `.ll`，强制换成 x86_64 Windows：

```powershell
& $llc -O2 -mtriple=x86_64-pc-windows-msvc .\axpy.ll -o axpy-x86.s
```

实测输出（节选）：

```asm
	.text
	.globl	axpy                            # -- Begin function axpy
	.p2align	4
axpy:                                   # @axpy
	movq	%r8, %rax
	xorl	%r8d, %r8d
	xorl	%r9d, %r9d
	jmp	.LBB0_1
```

对比两点：

- **参数寄存器变了**：Windows x64 是 `%rcx`、`%rdx`、`%r8`（第三个参数在 `%r8`，所以函数开头 `movq %r8, %rax` 保存返回值）；AArch64 是 `x0`、`x1`、`x2`。
- **汇编语法和节区变了**：x86 输出 `.def`/`.endef`/`.att_syntax`，AArch64 输出 `.type`/`.cfi_*`/`.note.GNU-stack`。

这就是 target triple 的作用：**同一份 LLVM IR，不同的 ABI、寄存器、指令和对象格式。** `llc` 不会“自动选一个能跑的”，它严格按 triple（或 `-mtriple` 覆盖）生成。

## 示例三：GlobalISel 管线的同一段汇编

```powershell
& $llc -global-isel -O2 .\axpy.ll -o axpy-gisel.s
```

实测输出（节选，和 SelectionDAG 版对比）：

```asm
// %bb.0:
	mov	x8, xzr
	mov	x9, xzr
	b	.LBB0_2
.LBB0_1:                                //   in Loop: Header=BB0_2 Depth=1
	add	x9, x9, #1
	add	x8, x8, #16
.LBB0_2:                                // =>This Loop Header: Depth=1
	cmp	x9, #4
	b.ge	.LBB0_5
...
.LBB0_4:                                //   Parent Loop BB0_2 Depth=1
                                        // =>  This Inner Loop Header: Depth=2
	ldr	s0, [x0, x11]
	ldr	s1, [x1, x11]
	add	x12, x2, x11
	fmul	s0, s0, s1
	ldr	s1, [x12]
	fadd	s0, s0, s1
	str	s0, [x12]
	cmp	x10, #4
	b.lt	.LBB0_4
```

差异很小，但能看出两条管线：

- SelectionDAG 用 `cmp #3; b.gt`，GlobalISel 用 `cmp #4; b.ge`；语义相同，规范化方式不同。
- SelectionDAG 用 `[x2, x11]` 直接寻址，GlobalISel 先 `add x12, x2, x11`，再 `[x12]`。
- 两者都保留了标量 `ldr s0`/`fmul`/`fadd`，没有自动向量化。

用 `-global-isel-abort=1` 验证没有回退：

```powershell
& $llc -global-isel -global-isel-abort=1 -O2 .\axpy.ll -o NUL
```

本机实测退出码为 `0`，说明这个函数完整走完了 GlobalISel。复杂 IR 可能触发 fallback；`-global-isel-abort=1` 会把 fallback 变成错误，是检查“我的 IR 到底有没有走 GlobalISel”的实用开关。

## 对比与取舍

### SelectionDAG vs GlobalISel

| 维度 | SelectionDAG | GlobalISel |
|---|---|---|
| 成熟度 | 更老、覆盖更全 | 新目标/新特性优先 |
| IR | 降到 DAG 再匹配 | 保持 SSA，分阶段 legalize/select |
| 调试 | DAG 视图成熟 | 阶段多，但每步 IR 可打印 |
| 适用 | 保守、求稳 | 新后端、需要统一 pipeline |

### fastcc vs 默认 C 调用约定

`fastcc` 允许后端用更多寄存器、跳过一些 ABI 约束，适合内部函数；但它不是 C ABI，跨语言调用会崩。需要和 C 交互时，用默认 C 约定 + `llvm.emit_c_interface` 包装，别为了性能把导出函数改成 `fastcc`。

### 有 triple vs 无 triple

有 `llvm.target_triple`：`llc` 按它生成，可复现。
无 triple：`llc` 用宿主默认目标（本机是 x86_64-pc-windows-msvc），同一份 `.ll` 在不同机器上产物不同。生产管线应该显式钉住 triple 和 data layout。

## 踩雷集锦

1. **把 `llvm` dialect 当成 LLVM IR。** `llvm.func`/`llvm.load` 仍然是 MLIR，必须经过 `mlir-translate`；直接把 `.mlir` 交给 `llc` 会报解析错误。

2. **忘了 `--mlir-to-llvmir`。** `mlir-translate` 需要显式指定目标格式；不写会列出可用翻译或报错。

3. **module 没有 triple，却以为 `llc` 会按 AArch64 生成。** 没有 triple 时 `llc` 用宿主默认目标；本机会生成 x86_64，不是 AArch64。

4. **data layout 和 triple 不匹配。** 指针宽度、对齐、结构体布局都可能错；`llc` 不一定会替你报错，但生成的代码可能不符合目标 ABI。

5. **`llvm.call` 的调用约定和 callee 不一致。** `llvm.func fastcc` 配 `llvm.call`（默认 C）会生成错误的调用序列；两边都要带 `fastcc`。

6. **在导出函数上用 `fastcc`。** C 调用者按平台 C ABI 传参，callee 按 fastcc 收参，寄存器/栈布局对不上，通常直接崩。

7. **以为 `-O2` 会自动向量化。** 本例的 `linalg` 已经降成标量循环，`llc` 不会替你做循环向量化；向量化要在 MLIR 侧用 `linalg`/`vector` 的 transformation。

## 进阶：再往深一层

- **看 LLVM IR 的 phi 来源**：把 `axpy.ll` 的 `phi` 和 Ch 21 的 block argument 对照，理解 MLIR 为什么用 block argument 而不是 phi。
- **用 `llc -print-after=...` 看后端阶段**：`-print-after=finalize-isel`、`-print-after=regalloc` 能把机器 IR 打出来；这是 MLIR 课程之外、LLVM 后端课程之内的内容。
- **看 `llvm.emit_c_interface` 的 wrapper**：把 `_mlir_ciface_add` 也 translate 出来，观察它如何把 C ABI 参数转成内部函数调用。

## 动手练习

1. **复现完整链路。** 从 `axpy-dl.mlir` 跑到 `axpy.ll`，再跑 `llc`，确认汇编里出现 `ldr s0`/`fmul`/`fadd`。
2. **改 triple。** 把 module 的 triple 改成 `x86_64-pc-windows-msvc`，重新 translate + llc，比较参数寄存器和节区。
3. **去掉 data layout。** 删除 `llvm.data_layout`，只留 triple，重新生成；确认 `.ll` 里没有 `target datalayout`，并观察 `llc` 是否仍能生成。
4. **试 GlobalISel。** 用 `-global-isel` 和 `-global-isel-abort=1` 各跑一次，比较退出码和汇编差异。
5. **加 `fastcc`。** 给一个内部函数加 `fastcc`，确认 LLVM IR 的 `define` 和 `call` 都带 `fastcc`；再故意只改一处，观察结果。

## 本章重点整理

- `mlir-translate --mlir-to-llvmir` 只做格式转换，不做优化、不做目标合法性检查。
- `llvm.target_triple` 和 `llvm.data_layout` 是 module 属性，translate 时写进 `llvm::Module`。
- `llvm.func` 的 linkage、cconv、`dso_local` 会映射到 LLVM `Function`；`llvm.emit_c_interface` 生成 `_mlir_ciface_*` 包装。
- `llc` 按 triple 选择目标；`-mtriple` 可覆盖；`-global-isel` 切换指令选择管线。
- 同一份 `.ll` 在 AArch64 和 x86_64 上的寄存器、指令、节区都不同。
- MLIR 的职责到 LLVM IR 为止；指令选择、寄存器分配、机器码是 LLVM 后端的事。

## 自我检核

- [ ] 我能说出 `mlir-translate` 和 `llc` 各自负责什么
- [ ] 我能解释为什么 `llvm` dialect 不是 LLVM IR
- [ ] 我知道 triple 和 data layout 存在哪里、由谁读取
- [ ] 我能读懂 `define internal fastcc` 和 `call fastcc` 的对应关系
- [ ] 我知道 `llvm.emit_c_interface` 生成什么函数
- [ ] 我能用 `-mtriple` 让同一份 `.ll` 产出两种目标汇编
- [ ] 我能解释 `-global-isel` 和 `-global-isel-abort=1` 的作用

## 延伸阅读

- **[LLVM IR Language Reference](https://llvm.org/docs/LangRef.html)**
  - **读哪里**：Functions、Calling Conventions、Data Layout 三节。
  - **学什么**：`define`/`declare`、属性、phi、target 相关语义。
  - **和本章的关联**：本章的 `.ll` 输出逐条对应这些语法。

- **[mlir-translate 源码](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Target/LLVMIR/ConvertToLLVMIR.cpp)**
  - **读哪里**：`registerToLLVMIRTranslation` 和 `translateModuleToLLVMIR` 调用。
  - **学什么**：翻译注册机制、输出路径。
  - **和本章的关联**：解释 `--mlir-to-llvmir` 到底做了什么。

- **[ModuleTranslation.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Target/LLVMIR/ModuleTranslation.cpp)**
  - **读哪里**：2511-2535 的 data layout/triple，2001-2013 的函数签名与 cconv，1051-1077 的 phi。
  - **学什么**：MLIR → LLVM IR 的完整映射。
  - **和本章的关联**：本章所有“为什么 `.ll` 长这样”的答案都在这里。

---

MLIR 的链路到这里闭合：tensor/linalg → memref → llvm dialect → LLVM IR → 汇编。下一部分是练习 D：从一段自定义输入出发，自己把整条链路跑一遍，并写出每个阶段的关键 IR。

→ [练习 D：把自定义方言降到 LLVM](./practice-d-lowering.md)
