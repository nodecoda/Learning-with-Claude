# Ch 31 端到端复盘：从 `func + scf + memref` 到 `llc`

> **目标**：把前 30 章串成一条能真跑的链路。给定一段带 `func.func`、`scf.for`、`memref`、`arith` 的 IR，你能逐段降级、逐段解释 IR 变了什么、为什么这一段必须在这个位置，最后用 `mlir-translate` 出 LLVM IR、用 `llc -O2` 出汇编；也能在顺序排错时读懂真实报错。这是 Part 5 的收口章。

## 为什么需要这个？

前面每一章都在解决一个局部问题：`scf` 怎么变成 CFG、`memref` 怎么变成 descriptor、`index` 怎么变成 `i64`、`unrealized_conversion_cast` 怎么消掉。但真实工作里没人按章节顺序问你，他们问的是：

> 「这段 IR 为什么降不下去？」

要回答这个问题，脑子里得有一条完整的链，并且知道每一段的**输入契约**和**输出契约**。本章就是把这条链走一遍——不是画图，是真跑，每一段都贴 `mlir-opt` 的 dump。

这也是 Part 5 的终点：Ch 28 教你怎么读源码，Ch 29 讲设计取舍，Ch 30 讲性能，本章把这些放回同一条 pipeline 里。之后的 Final Project 会在这条链的**前端**再接一段 mini frontend。

## 先建立直觉：六段，每段只干一件事

```text
scf + memref + arith + func
  │ 1. convert-scf-to-cf        结构化控制流 → 基本块 + 跳转
  │ 2. convert-cf-to-llvm       cf.br/cond_br → llvm.br/cond_br
  │ 3. convert-func-to-llvm     func.func → llvm.func，展开 ABI
  │ 4. convert-arith-to-llvm    arith → llvm 标量运算，index → i64
  │ 5. finalize-memref-to-llvm  memref.load/store → extractvalue + gep + load/store
  │ 6. reconcile-unrealized-casts  折掉临时 cast
  ▼
llvm dialect → mlir-translate → LLVM IR → llc → 汇编
```

记住三句话，后面每一段都能对上：

- **越结构化的越先降**：`scf.for` 必须先变成 CFG，后面才有「基本块」这个概念可用。
- **类型转换会欠债**：`index → i64`、`memref → descriptor` 都不是同构替换，中间会产生 `builtin.unrealized_conversion_cast`，这是借条，不是 bug。
- **ABI 是最后一道闸**：函数签名一旦展开成多个标量参数，函数体里的 memref 就得用 descriptor 重建，所以 `func` 的降级要和 `memref` 的收尾配合。

## 核心概念一：每个 pass 只买自己那一段票

`convert-scf-to-cf` 只认 `scf.for/if/while/...`，其它 op 一律放行。源码里它把目标定成「这些 op 非法，其余动态合法」：

```cpp
// mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp:730-743（源码依据）
void SCFToControlFlowPass::runOnOperation() {
  RewritePatternSet patterns(&getContext());
  populateSCFToControlFlowConversionPatterns(patterns);
  ConversionTarget target(getContext());
  target.addIllegalOp<scf::ForallOp, scf::ForOp, scf::IfOp, scf::IndexSwitchOp,
                      scf::ParallelOp, scf::WhileOp, scf::ExecuteRegionOp>();
  target.markUnknownOpDynamicallyLegal([](Operation *) { return true; });
  ...
  if (failed(applyPartialConversion(getOperation(), target, std::move(patterns), config)))
    signalPassFailure();
}
```

这段代码解释了两个常见疑问：

- **为什么它「看起来什么都没干」**：IR 里没有 `scf` 时，所有 op 都动态合法，pass 直接通过。
- **为什么它放在 module 级也能跑**：`Pass<"convert-scf-to-cf">` 没有限定 op，`getOperation()` 拿到 module 就降 module 里的所有 `scf`；在 23.1.4 里嵌进 `func.func(...)` 也能跑（本机实测两种都成功）。本章统一放 module 级，因为一条 pipeline 里其它 conversion 都是 module 级，少一层嵌套少一次心智负担。

`convert-arith-to-llvm` 同理，只认 `arith`，并且负责 `index` 的位宽。`CmpIOpLowering` 就是一个普通 conversion pattern：

```cpp
// mlir/lib/Conversion/ArithToLLVM/ArithToLLVM.cpp:282-288（源码依据）
struct CmpIOpLowering : public ConvertOpToLLVMPattern<arith::CmpIOp> {
  using ConvertOpToLLVMPattern::ConvertOpToLLVMPattern;
  LogicalResult
  matchAndRewrite(arith::CmpIOp op, OpAdaptor adaptor,
                  ConversionPatternRewriter &rewriter) const override;
};
```

`mulf/addf` 也没有专门的结构体，走的是模板化的 lowering（`:90-91` 的 `AddFOpLowering`、`:156-157` 的 `MulFOpLowering` 是 `ConstrainedVectorConvertToLLVMPattern` 的别名，`:98-99` 的 `AddIOpLowering` 是 `VectorConvertToLLVMPattern`）。所以这一段的行为很可预测：算术 op 一对一映射，`index` 统一变 `i64`。

## 核心概念二：`unrealized_conversion_cast` 是借条

转换框架有一条铁律：**pattern 只能拿到「已经转换过」的操作数**。但 `cf.br` 要 `i64`，原来的 block argument 是 `index`；`llvm.func` 的 body 要 memref 时，参数已经被拆成 descriptor。类型对不上，又不能丢数据，于是插入一个 `builtin.unrealized_conversion_cast` 占位：

```mlir
%0 = builtin.unrealized_conversion_cast %c0 : index to i64
```

它的语义是「我知道这两个值对应，但转换还没完成」。它**不是** no-op 优化提示，而是 conversion 框架的未完成标记：

- 中间阶段出现它是正常的（第 2、3、4 段都会产生）；
- 最后必须由 `reconcile-unrealized-casts` 折掉；
- 折不掉就说明两侧的类型真的不一致，这时候跑 `mlir-translate` 会报错，而不是静默生成错误代码。

Ch 19 专门讲过它的 legality 角色；本章只需要记住：**看到它，问「谁欠谁」**。

## 核心概念三：ABI 在 `func` 边界上炸开

`func.func @axpy(%alpha: f32, %x: memref<8xf32>, %y: memref<8xf32>)` 看起来很干净。但 `memref<8xf32>` 在 LLVM 里没有对应类型，默认 ABI 把它拆成 5 个标量：

```text
allocated_ptr, aligned_ptr, offset, size[0], stride[0]
```

于是 `llvm.func` 的签名变成 1 + 5 + 5 = **11 个参数**：

```mlir
llvm.func @axpy(%arg0: f32, %arg1: !llvm.ptr, %arg2: !llvm.ptr, %arg3: i64,
                %arg4: i64, %arg5: i64, %arg6: !llvm.ptr, %arg7: !llvm.ptr,
                %arg8: i64, %arg9: i64, %arg10: i64)
```

函数体开头会用 `llvm.mlir.poison` + `llvm.insertvalue` 把参数重建成 descriptor；之后 `memref.load/store` 才能被 `finalize-memref-to-llvm` 降到 `extractvalue + getelementptr + load/store`。Ch 21 详细讲了 ABI 的两种选择（descriptor vs bare pointer），本章只看默认路径的形态。

## 底层机制：为什么 `reconcile` 必须最后跑

`reconcile-unrealized-casts` 做的是「cast 对消」：`index → i64 → index` 折成原值，`memref → descriptor → memref` 折成原值。它必须看到**所有** conversion 的结果，否则前面还会继续产生新 cast。

反过来，前面五个 pass 的顺序有几条硬约束、几条软约束：

| 约束 | 类型 | 证据 |
|---|---|---|
| `scf` 必须在 `cf` 之前 | 硬 | `convert-cf-to-llvm` 不认识 `scf.for`，跳过后 `cf.br` 也不会出现 |
| `reconcile` 必须最后 | 硬 | 它是收尾 pass，先跑就会被后续 pass 重新引入 cast |
| `func` 在 `cf` 前或后 | 软 | 本机实测两种顺序都能跑通，且都能 translate |
| `arith` 在 `func` 前或后 | 软 | 本机实测都能跑通；`index` 位宽由 arith 决定，放后面对齐更自然 |
| `finalize-memref` 在 `func` 之后 | 软偏好 | 先 finalize 会留下 `memref → descriptor` 的临时 cast，`func` 降级后再补 |

所以本章的六段顺序不是唯一解，而是**读起来最顺、临时 cast 最少**的一条。真实项目里你会按 dialect 组合调整，但硬约束不能破。

## 示例一：起点 IR（实测）

存成 `e2e.mlir`：

```mlir
func.func @axpy(%alpha: f32, %x: memref<8xf32>, %y: memref<8xf32>) {
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  %c8 = arith.constant 8 : index
  scf.for %i = %c0 to %c8 step %c1 {
    %xi = memref.load %x[%i] : memref<8xf32>
    %t = arith.mulf %xi, %alpha : f32
    %yi = memref.load %y[%i] : memref<8xf32>
    %r = arith.addf %t, %yi : f32
    memref.store %r, %y[%i] : memref<8xf32>
  }
  return
}
```

这段 `y = alpha * x + y` 同时覆盖了四种抽象：结构化控制流、缓冲语义、标量算术、函数边界。用静态形状 `memref<8xf32>` 是为了让 descriptor 的 size/stride 是常量，汇编更好读；换成动态形状，降级路径一样，只是多几个 `llvm.mlir.constant`。

## 示例二：六段降级，逐段看 dump（实测）

一次跑完的 pipeline：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$pipe = 'builtin.module(convert-scf-to-cf,convert-cf-to-llvm,convert-func-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,reconcile-unrealized-casts)'
& "$mlir\mlir-opt.exe" .\e2e.mlir "--pass-pipeline=$pipe" --mlir-print-ir-after-all
```

实测 dump 的六个头（stderr）：

```text
// -----// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //----- //
// -----// IR Dump After ConvertControlFlowToLLVMPass: convert-cf-to-llvm{index-bitwidth=0} //----- //
// -----// IR Dump After ConvertFuncToLLVMPass: convert-func-to-llvm{index-bitwidth=0 use-bare-ptr-memref-call-conv=false} //----- //
// -----// IR Dump After ArithToLLVMConversionPass: convert-arith-to-llvm{index-bitwidth=0} //----- //
// -----// IR Dump After FinalizeMemRefToLLVMConversionPass: finalize-memref-to-llvm{index-bitwidth=0 use-aligned-alloc=false use-generic-functions=false} //----- //
// -----// IR Dump After ReconcileUnrealizedCastsPass: reconcile-unrealized-casts //----- //
```

下面逐段看。为了让示例可复现，每段都是「在上一段输出上再跑一个 pass」。

### 第 1 段：`convert-scf-to-cf`

```mlir
module {
  func.func @axpy(%arg0: f32, %arg1: memref<8xf32>, %arg2: memref<8xf32>) {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    %c8 = arith.constant 8 : index
    cf.br ^bb1(%c0 : index)
  ^bb1(%0: index):  // 2 preds: ^bb0, ^bb2
    %1 = arith.cmpi slt, %0, %c8 : index
    cf.cond_br %1, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %2 = memref.load %arg1[%0] : memref<8xf32>
    %3 = arith.mulf %2, %arg0 : f32
    %4 = memref.load %arg2[%0] : memref<8xf32>
    %5 = arith.addf %3, %4 : f32
    memref.store %5, %arg2[%0] : memref<8xf32>
    %6 = arith.addi %0, %c1 : index
    cf.br ^bb1(%6 : index)
  ^bb3:  // pred: ^bb1
    return
  }
}
```

变化：`scf.for` 没了，变成三段 CFG；循环变量 `%i` 变成 `^bb1` 的 block argument；`scf.yield` 没了，迭代变量靠 `cf.br` 的参数传递。`arith`、`memref` 一个没动——这段只负责控制流。

### 第 2 段：`convert-cf-to-llvm`

```mlir
  func.func @axpy(%arg0: f32, %arg1: memref<8xf32>, %arg2: memref<8xf32>) {
    %c0 = arith.constant 0 : index
    %0 = builtin.unrealized_conversion_cast %c0 : index to i64
    %c1 = arith.constant 1 : index
    %c8 = arith.constant 8 : index
    llvm.br ^bb1(%0 : i64)
  ^bb1(%1: i64):  // 2 preds: ^bb0, ^bb2
    %2 = builtin.unrealized_conversion_cast %1 : i64 to index
    %3 = arith.cmpi slt, %2, %c8 : index
    llvm.cond_br %3, ^bb2, ^bb3
```

变化：跳转 op 换成 `llvm.br`/`llvm.cond_br`；**block argument 的类型从 `index` 变成 `i64`**，于是两端各欠一张借条。注意 `func.func` 还在——它不归这段管。

### 第 3 段：`convert-func-to-llvm`

```mlir
  llvm.func @axpy(%arg0: f32, %arg1: !llvm.ptr, %arg2: !llvm.ptr, %arg3: i64, %arg4: i64, %arg5: i64, %arg6: !llvm.ptr, %arg7: !llvm.ptr, %arg8: i64, %arg9: i64, %arg10: i64) {
    %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %1 = llvm.insertvalue %arg6, %0[0] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    ...
    %6 = builtin.unrealized_conversion_cast %5 : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)> to memref<8xf32>
```

变化：签名炸成 11 个参数；函数体开头用 `insertvalue` 重建两个 descriptor，再 cast 回 `memref<8xf32>`，好让还没降级的 `memref.load/store` 继续工作。这一步把 cast 从 3 个推到 5 个；第 4、5 段会到 7 个。

### 第 4 段：`convert-arith-to-llvm`

```mlir
    %14 = llvm.mlir.constant(0 : index) : i64
    %15 = builtin.unrealized_conversion_cast %14 : i64 to index
    %16 = builtin.unrealized_conversion_cast %15 : index to i64
    %17 = llvm.mlir.constant(1 : index) : i64
    %18 = llvm.mlir.constant(8 : index) : i64
    llvm.br ^bb1(%16 : i64)
  ^bb1(%19: i64):  // 2 preds: ^bb0, ^bb2
    %20 = builtin.unrealized_conversion_cast %19 : i64 to index
    %21 = llvm.icmp "slt" %19, %18 : i64
```

变化：`arith.constant` → `llvm.mlir.constant`（`index` 常量直接落到 `i64`）；`cmpi slt` → `llvm.icmp "slt"`。`%15/%16` 是一对自反的 cast，`reconcile` 会把它折掉。函数体里还留着 `memref.load/store`，等第 5 段。

### 第 5 段：`finalize-memref-to-llvm`

```mlir
    %22 = llvm.extractvalue %12[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %23 = llvm.getelementptr inbounds|nuw %22[%19] : (!llvm.ptr, i64) -> !llvm.ptr, f32
    %24 = llvm.load %23 : !llvm.ptr -> f32
    %25 = llvm.fmul %24, %arg0 : f32
    %26 = llvm.extractvalue %5[1] : !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
    %27 = llvm.getelementptr inbounds|nuw %26[%19] : (!llvm.ptr, i64) -> !llvm.ptr, f32
    %28 = llvm.load %27 : !llvm.ptr -> f32
    %29 = llvm.fadd %25, %28 : f32
    ...
    llvm.store %29, %31 : f32, !llvm.ptr
```

变化：`memref.load` 拆成「取 aligned ptr → GEP → load」；`memref.store` 同理。到这里，所有 `memref` op 都没了，只剩类型层面的 cast。

### 第 6 段：`reconcile-unrealized-casts`

```mlir
  llvm.func @axpy(%arg0: f32, %arg1: !llvm.ptr, %arg2: !llvm.ptr, %arg3: i64, ...) {
    ...
    %12 = llvm.mlir.constant(0 : index) : i64
    %13 = llvm.mlir.constant(1 : index) : i64
    %14 = llvm.mlir.constant(8 : index) : i64
    llvm.br ^bb1(%12 : i64)
  ^bb1(%15: i64):  // 2 preds: ^bb0, ^bb2
    %16 = llvm.icmp "slt" %15, %14 : i64
```

变化：所有 `builtin.unrealized_conversion_cast` 消失；descriptor 的重建还在（load/store 要用），但不再经 memref 中转。这就是 `mlir-translate` 能接受的形态。

## 示例三：LLVM IR 与 `llc -O2`（实测）
```powershell
  # 第 6 段的输出存成 s6.mlir
& "$mlir\mlir-opt.exe" .\e2e.mlir "--pass-pipeline=$pipe" -o .\s6.mlir
& "$mlir\mlir-translate.exe" .\s6.mlir --mlir-to-llvmir -o .\e2e.ll
& "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe" -O2 .\e2e.ll -o .\e2e.s
```

LLVM IR 的函数头与循环（节选）：

```llvm
define void @axpy(float %0, ptr %1, ptr %2, i64 %3, i64 %4, i64 %5, ptr %6, ptr %7, i64 %8, i64 %9, i64 %10) {
  %12 = insertvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } poison, ptr %6, 0
  ...
  br label %22

22:                                               ; preds = %25, %11
  %23 = phi i64 [ %36, %25 ], [ 0, %11 ]
  %24 = icmp slt i64 %23, 8
  br i1 %24, label %25, label %37

25:                                               ; preds = %22
  %26 = extractvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } %21, 1
  %27 = getelementptr inbounds nuw float, ptr %26, i64 %23
  %28 = load float, ptr %27, align 4
  %29 = fmul float %28, %0
```

`llc -O2` 之后的汇编（节选，x86-64）：

```asm
axpy:                                   # @axpy
	movq	64(%rsp), %rax
	xorl	%ecx, %ecx
	cmpq	$7, %rcx
	jg	.LBB0_3
.LBB0_2:                                # =>This Inner Loop Header: Depth=1
	movss	(%r8,%rcx,4), %xmm1
	mulss	%xmm0, %xmm1
	addss	(%rax,%rcx,4), %xmm1
	movss	%xmm1, (%rax,%rcx,4)
	incq	%rcx
	cmpq	$7, %rcx
	jle	.LBB0_2
.LBB0_3:
	retq
```

三点观察：

- **phi 就是 block argument 的落点**：`%23 = phi i64 [ %36, %25 ], [ 0, %11 ]` 对应 MLIR 里 `^bb1(%15: i64)` 的循环变量。
- **descriptor 按值传的后果肉眼可见**：`movq 64(%rsp), %rax` 是从栈上取参数——11 个参数早把寄存器用完了。
- **`llc` 默认不向量化**：循环被转成了 `movss/mulss/addss`；即使加 `-mattr=+avx2`，本机也只变成 VEX 编码的标量指令（实测），没有变成 `vmulps`。MLIR 的降级不负责这件事，向量化要在 MLIR 层用 `vector`/`linalg` 做，或靠 `llc` 的自动向量化在别的 IR 形态下生效。

## 示例四：顺序错了会怎样（真实报错）

**错误一：把 module 级 pass 塞进 `func.func`。**

```powershell
& "$mlir\mlir-opt.exe" .\e2e.mlir --pass-pipeline='builtin.module(func.func(convert-func-to-llvm))'
```

实测：

```text
<unknown>:0: error: Can't add pass 'ConvertFuncToLLVMPass' restricted to 'builtin.module' on a PassManager intended to run on 'func.func', did you intend to nest?
failed to add `convert-func-to-llvm` with options ``
failed to add `func.func` with options `` to inner pipeline
```

注意它是在**构建 pipeline 时**就失败，IR 还没开始跑。报错里的 `did you intend to nest?` 是 MLIR 在提醒你：这个 pass 想跑在 module 上，你是不是少了一层。

**错误二：照抄别人的 `convert-memref-to-llvm`。**

实测（节选）：

```text
mlir-opt.exe: Unknown command line argument '--convert-memref-to-llvm'.
mlir-opt.exe: Did you mean '--convert-spirv-to-llvm'?
```

23.1.4 只有 `finalize-memref-to-llvm`。**先 `--help` 再写 pipeline**，别凭记忆。

**错误三：跳过 `convert-cf-to-llvm` 直接 translate。**

实测（节选）：

```text
error: Dialect `cf' not found for custom op 'cf.br'
note: Available dialects: ... builtin, dlti, func, gpu, llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm
```

`mlir-opt` 会成功（它允许混合 IR），但 `mlir-translate --mlir-to-llvmir` 的 registry 只带 LLVM 相关 dialect，看到 `cf.br` 直接拒绝。**「mlir-opt 没报错」不等于「能出 LLVM IR」**，这是本课最值钱的一条经验。

## 复盘表：每个 MLIR 概念落到哪里

| MLIR 概念 | 降级后的落点 | 本机证据 |
|---|---|---|
| `scf.for` 的归纳变量（`iter_args` 同理） | CFG 基本块 + block argument → LLVM `phi` | 第 1 段；LLVM IR 的 `%23 = phi` |
| region / block | LLVM basic block + `br`/`cond_br` | 第 1、2 段 |
| `memref<8xf32>` | `!llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>` | 第 3 段签名 |
| `memref.load/store` | `extractvalue` + `getelementptr` + `load/store` | 第 5 段 |
| `index` | `i64`（`llvm.mlir.constant(0 : index) : i64`） | 第 4 段 |
| `arith.mulf/addf` | `llvm.fmul/fadd` → LLVM `fmul/fadd` | 第 4 段、LLVM IR |
| `arith.cmpi slt` | `llvm.icmp "slt"` → `icmp slt` | 第 2、4 段 |
| `func.func` | `llvm.func` + ABI 展开（11 参数） | 第 3 段 |
| `unrealized_conversion_cast` | 被 `reconcile` 折掉，不出现在 LLVM IR | 第 6 段 |
| `return` | `llvm.return` → `ret void` | 第 3 段起 |

## 与 `globalisel_regbankselect` 的接缝

MLIR 的职责到 LLVM IR 为止。`llc` 之后才进入 **ISel / RegBankSelect / 寄存器分配**：本章的 `-O2` 走的是 SelectionDAG，想走 GlobalISel 要显式加 `-global-isel`，那是 [`globalisel_regbankselect`](../globalisel_regbankselect/README.md) 课程的地盘。一句话：**MLIR 决定「算什么」，LLVM 后端决定「用哪些寄存器和指令算」**，中间的分界线就是这份 `.ll`。

## 全课回顾：37 个文件 → 6 个 Part → 一条主线

37 个章节文件（不含 `README.md`）的分工：

| Part | 文件 | 数量 | 在这条链路里的位置 |
|---|---|---|---|
| Part 0 | Ch 0–4 + 练习 A | 6 | 心智模型、IR 对象模型、方言地图：看懂起点 IR |
| Part 1 | Ch 5–10 + 练习 B | 7 | pass、pattern、analysis、调试：本章六个 pass 的执行机制 |
| Part 2 | Ch 11–16 + 练习 C | 7 | ODS、自定义方言：起点 IR 可以由你定义 |
| Part 3 | Ch 17–22 + 练习 D | 7 | conversion、type converter、bufferize、LLVM 降级：本章主体 |
| Part 4 | Ch 23–27 | 5 | linalg/affine/vector/transform/工程化：往链路前端扩展 |
| Part 5 | Ch 28–31 + Final | 5 | 读源码、取舍、性能、复盘：把链路讲清楚 |
| 合计 |  | **37** |  |

一条主线：

```text
源语言 / 前端
  → 自定义方言（Part 2）
  → 结构化 op：linalg / tensor（Part 4）
  → bufferize：tensor → memref（Ch 20）
  → 结构化控制流 scf（Ch 1、17–19）
  → CFG：cf（本章第 1 段）
  → LLVM dialect：llvm.func/br/gep（本章第 2–5 段）
  → LLVM IR（mlir-translate）
  → llc：ISel / RegBankSelect / 寄存器分配（另一门课）
```

每一章都能在这条线上找到位置；找不到位置的章节，要么是工具链（Ch 0、10、27），要么是判断力（Ch 29、30）。

## 对比与取舍

- **一步到位 vs 分段降级**：MLIR 没有「一键到 LLVM」的官方 pass，因为每一步的合法性、类型转换、ABI 选择都需要人决策。分段是特性，不是缺陷。
- **descriptor ABI vs bare pointer**：descriptor 通用、支持动态形状；bare pointer 签名小、调用快，但要求静态形状和 identity layout。本章用默认 descriptor，是为了让「类型怎么变」看得见。
- **在 MLIR 层向量化 vs 交给 LLVM**：本章的循环在 `llc -O2` 下仍是标量。想在 MLIR 层拿到 `vector` 好处，得在 bufferize 前后用 `linalg`/`vector` 的 pass；这属于 Ch 23–25 的路线。
- **静态形状 vs 动态形状**：静态形状让常量折叠和 GEP 更干净，但真实程序多半是动态的；动态形状只是多几条 `llvm.mlir.constant` 和 `extractvalue`，降级路径不变。

## 踩雷集锦

1. **只看 `mlir-opt` 成功就以为能 translate。** 跳过 `cf-to-llvm` 的例子说明：混合 IR 合法，但 translate 不认。
2. **把 module 级 pass 嵌进 `func.func(...)`。** 构建 pipeline 时就报 `did you intend to nest?`。
3. **凭记忆写 `convert-memref-to-llvm`。** 23.1.4 只有 `finalize-memref-to-llvm`。
4. **在 `reconcile` 之后再跑 conversion。** 新产生的 cast 没人收尾，translate 会失败或留下非法 IR。
5. **以为 `index` 是 `i64` 的别名。** 它是目标相关类型；本章默认降到 `i64`，但 `--index-bitwidth=32` 会变。
6. **忽略函数签名的膨胀。** 两个 memref 参数变成 10 个标量参数，调用方/被调方都得按同一份 ABI 改；手工写 `llvm.func` 很容易对不上。
7. **开着多线程看 dump 顺序。** 32 个函数时顺序每次都不同；要确定性加 `--mlir-disable-threading`（Ch 30 有实测）。

## 进阶：再往深一层

- **把 `use-bare-ptr-memref-call-conv=true` 打开**，对比签名和函数体：descriptor 的 `insertvalue/extractvalue` 会大幅减少，但动态形状不再支持。
- **把 `memref<8xf32>` 改成 `memref<?xf32>`**，观察 `memref.dim` 怎么变成 `extractvalue`，以及调用方要多传几个参数。
- **在 `reconcile` 之前插一个 `--canonicalize`**，看它能不能提前折掉一部分 cast；答案通常是不能，因为 cast 两侧类型真的不同。
- **把 `-O2` 换成 `-O0`**，对比汇编：`-O0` 会保留更多 descriptor 重建的冗余指令，正好用来理解 ABI 的成本。

## 动手练习

**任务规格**：独立走通一条与本章不同形状的链路。

1. 把起点 IR 改成 `memref<?xf32>` + `memref.dim`，重跑六段，记录签名和 `memref.dim` 的落点。
2. 把 `f32` 换成 `i32`，把 `mulf/addf` 换成 `muli/addi`，重跑并解释 `arith` 映射的变化。
3. 故意交换第 2、3 段（先 `func` 后 `cf`），比较最终 IR 的 cast 数量，再解释为什么仍能跑通。
4. 用 `--mlir-print-ir-after-all` 抓六段 dump，找出「cast 数量最多」的那一刻，并说明原因。
5. 把 `llc -O2` 换成 `llc -O0`，指出汇编里多出来的 descriptor 重建指令。

**测试用例**：

- 静态 `memref<8xf32>`：应与本章输出一致，`llc` 产出标量 SSE 循环。
- 动态 `memref<?xf32>`：应多出 size/stride 的 `extractvalue`，但能 translate。
- 交换 `cf`/`func` 顺序：应仍能 translate，cast 数量不同。

**分段步骤**：先只跑第 1 段确认 CFG；再逐段加一个 pass 并保存中间文件；最后一次性跑完对比。不要一上来就跑整条 pipeline，否则报错时不知道停在哪。

<details>
<summary>参考解答与常见卡点</summary>

- 动态形状的关键是 `memref.dim` 会被 `finalize-memref-to-llvm` 降到 `extractvalue descriptor[3]`（size 字段），不是常量折叠。
- 交换 `cf`/`func` 仍能跑通，因为 conversion 框架允许混合 IR；差异在 cast 数量和重建次数，不在正确性。
- cast 峰值在第 4、5 段（各 7 个）：函数签名已展开，函数体还没完全降到 LLVM，两边靠 cast 连接。
- `llc -O0` 会保留 `insertvalue/extractvalue` 的影子；对照 `-O2` 能直观看到 ABI 的成本被优化掉了多少。
- 如果 translate 报 `Dialect 'xxx' not found`，先检查是否漏了对应的 `convert-*-to-llvm`，而不是怀疑 LLVM IR 语法。

</details>

**常见卡点**：`--pass-pipeline` 里的 module 级 pass 不要加 `func.func(...)`；`-o` 输出文件不要和输入同名；`mlir-translate` 的 `-o -` 打到 stdout 时会和诊断混在一起。

## 本章重点整理

- 端到端降级是六段：`scf→cf`、`cf→llvm`、`func→llvm`、`arith→llvm`、`finalize-memref→llvm`、`reconcile`。
- 硬约束只有两条：`scf` 在 `cf` 之前；`reconcile` 最后。其余顺序可调，代价是临时 cast 数量。
- `unrealized_conversion_cast` 是未完成标记，不是优化提示；它必须被 `reconcile` 收掉。
- `memref` 默认 ABI 是 5 字段 descriptor，函数签名会膨胀；bare pointer 是另一种取舍。
- `index` 不是 `i64` 的别名，但默认降到 `i64`；`arith` 负责这次转换。
- `mlir-opt` 跑通 ≠ 能 translate：translate 的 dialect registry 更窄。
- 这门课停在 LLVM IR；ISel/RegBankSelect 属于 `globalisel_regbankselect`。

## 自我检核

- [ ] 我能不看笔记写出六段 pipeline，并说出每段的输入/输出契约
- [ ] 我能解释为什么 `scf` 必须先降成 `cf`
- [ ] 我能指出本链路里 cast 最多的时刻，并说明原因
- [ ] 我能从 LLVM IR 里指出 `phi`、`getelementptr`、`insertvalue` 分别对应 MLIR 的什么
- [ ] 我能复现 `did you intend to nest?` 报错，并说出它发生在 pipeline 构建阶段
- [ ] 我知道 23.1.4 没有 `convert-memref-to-llvm`，只有 `finalize-memref-to-llvm`
- [ ] 我能解释 `mlir-opt` 成功但 `mlir-translate` 失败的原因
- [ ] 我能把 37 个文件映射到 6 个 Part，并指出本章在主线上的位置

## 延伸阅读
- **[SCFToControlFlow.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp)**
  - **读哪里**：`SCFToControlFlowPass::runOnOperation`（730-743）和上面的 `ForLowering`/`IfLowering`。
  - **学什么**：结构化控制流如何被拆成单入口单出口的子图。
  - **和本章的关联**：第 1 段的每一行输出都来自这里。

- **[ArithToLLVM.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Conversion/ArithToLLVM/ArithToLLVM.cpp)**
  - **读哪里**：`populateArithToLLVMConversionPatterns`（764 起）和 `CmpIOpLowering`（282-288）。
  - **学什么**：算术 op 的一对一映射，以及 `index` 位宽在哪里决定。
  - **和本章的关联**：第 4 段的输出就是这些 pattern 的结果。

- **[MLIR Dialect Conversion 文档](https://mlir.llvm.org/docs/DialectConversion/)**
  - **读哪里**：legality、partial vs full conversion、`unrealized_conversion_cast` 一节。
  - **学什么**：为什么中间态允许混合 IR，为什么需要 reconcile。
  - **和本章的关联**：第 2–6 段的行为都由这套规则决定。

- **[Ch 21 降到 LLVM dialect](./21-lowering-to-llvm.md)**
  - **读哪里**：ABI、descriptor、bare pointer 三节。
  - **学什么**：本章第 3、5 段的完整背景。
  - **和本章的关联**：本章是它的可跑复盘版。

- **[Ch 30 性能与规模](./30-performance-and-scale.md)**
  - **读哪里**：多线程 dump 顺序、bytecode、测量协议。
  - **学什么**：怎么给这条链路做性能测量。
  - **和本章的关联**：本章的 dump 如果没有 `--mlir-disable-threading`，顺序就不可复现。

链路走完，下一步把它往前延伸：自己写一个 mini frontend，产出 MLIR，再复用这条 pipeline。

→ [Final Project：从 mini frontend 到 LLVM](./final-project-mini-frontend.md)