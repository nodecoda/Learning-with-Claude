# Ch 22 — TritonGPU → LLVM 的转换框架

> 这一章不讲某条指令怎么降，而讲“降”这件事本身是怎么被组织的。

## 目标

读完本章，你应该能回答：

- `ConvertTritonGPUToLLVM` 为什么要把一个 pass 拆成“lower functions → lower ops → lower CF”三段？
- ConversionTarget 里哪些 dialect 是 legal、哪些是 illegal？为什么 `cf` 和 `nvgpu` 是 legal？
- TypeConverter 如何把 `tensor<256xf32, #blocked>` 变成一个 `!llvm.struct<(f32, f32)>`？TMEM memdesc 为什么只变成一个指针？
- `UnrealizedConversionCastOp` 什么时候出现？谁负责把它消掉？
- pattern benefit 的 1 / 10 / 20 分别是什么含义，为什么 Triton 自己的 pattern 要设成 10？
- partial conversion 和 full conversion 的差别，为什么这里必须用 partial？

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | 3.9.0 |
| GPU | Tesla T4（`sm_75`） |
| Python | 仓库内 `.venv`，3.12.16，`triton 3.9.0` |
| pass 入口 | `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp` |

本章的 LLIR/PTX 片段来自 T4 上的真实编译（**实测**），TTGIR 也来自同一份 dump。凡是从源码推导、没有实际运行的部分会标 `源码依据` 或 `guided`。

**源码依据**：pass 注册在 `third_party/nvidia/include/TritonNVIDIAGPUToLLVM/Passes.td:22-45`，名字是 `convert-triton-gpu-to-llvm`，两个 option 是 `compute-capability` 和 `ptx-version`，默认都是 80。

## 为什么需要这个

TTGIR 和 LLVM dialect 之间不是“换几个 op 名字”的关系，而是四层同时变化：

| 维度 | TTGIR | LLVM dialect |
|---|---|---|
| 类型 | `tensor<256xf32, #blocked>` | `!llvm.struct<(f32, f32)>` |
| 内存 | `!ttg.memdesc<...>` | 指针 + offset 结构 |
| 控制流 | `scf.for` / `cf.br` / `ttg.warp_specialize` | `llvm.br` / `llvm.call` |
| 语义 | layout、warp、CTA、barrier | 线程、寄存器、地址空间 |

如果用一个 full conversion 一次性做完，会遇到三个问题：

1. **有些 op 必须晚点降**。`ttg.warp_specialize` 的 region 里还有 Triton op，必须等它们先降完，才能把整个结构改写成 warp id 分支。所以它在 target 里被显式标成 legal（`TritonGPUToLLVM.cpp:67-70`），留给后面的 `add_warp_specialize_to_llvm`。
2. **有些 dialect 不该由这个 pass 管**。proton 之类的扩展 dialect 在 target 里“不分类”，partial conversion 会原样保留，等它自己的 lowering pass 处理（`59-60` 的注释）。
3. **类型边界需要临时 cast**。一个 pattern 可能要把“已经降成 struct 的值”再喂给一个还没降的 op，这时只能插入 `UnrealizedConversionCastOp`，等两边都降完再消。

所以这个 pass 的本质是：**用一个共享的 ConversionTarget + 一个共享的 TypeConverter，分阶段把不同类别的 IR 推到 LLVM，允许中间态存在。**

## 先建立直觉

先看 `runOnOperation` 的五步（`TritonGPUToLLVM.cpp:164-199`）：

```text
1. 建 LowerToLLVMOptions，index bitwidth 固定 32
2. 建 TritonGPUToLLVMTypeConverter
3. lowerFunctions        // tt.func -> llvm.func（只处理函数签名）
4. createSharedMemoryGlobal  // 插入 global_smem
5. lowerTritonGPUOps     // 所有 Triton op -> LLVM/NVVM op
6. lowerControlFlow      // cf -> llvm
7. finalizeModule        // 折 CTAId、修 loop annotation、隔离 warp group
8. 对 InsertValueOp 做一轮 canonicalize
```

注意第 3 步和第 5 步是**两个独立的 partial conversion**，不是一个大 conversion。这一点是理解整个框架的钥匙：

- 第 3 步只把 `tt.func` 变成 `llvm.func`，函数体里的 op 一个都不动；
- 第 5 步才把函数体里的 op 逐个降掉；
- 第 6 步最后处理 CF。

为什么不能反过来？因为 op 的 lowering 需要知道“当前函数的 LLVM 签名是什么”“shared memory 基地址从哪来”“device function 多了哪几个参数”。这些信息在第 3 步之后才稳定。

## 核心概念

### 1. ConversionTarget 的三层继承

代码里有两个 target 类：

```cpp
class NvidiaLLVMConversionTarget : public ConversionTarget {
  addLegalDialect<LLVM::LLVMDialect>();
  addLegalDialect<NVVM::NVVMDialect>();
  addLegalOp<ModuleOp, UnrealizedConversionCastOp>();
};

class TritonLLVMConversionTarget : public NvidiaLLVMConversionTarget {
  addLegalDialect<cf::ControlFlowDialect>();          // CF 留到第 6 步
  addLegalDialect<triton::nvgpu::NVGPUDialect>();     // LLVM 级 IR，后面再降
  addIllegalDialect<TritonDialect, TritonGPUDialect,
                    TritonNvidiaGPUDialect, TritonInstrumentDialect,
                    gpu::GPUDialect>();
  addLegalOp<WarpIdOp, WarpSpecializeOp, WarpYieldOp,
             WarpSpecializePartitionsOp, WarpReturnOp>();
  addLegalOp<ThreadScopeOp, ThreadYieldOp>();
};
```

（`TritonGPUToLLVM.cpp:38-74`）

三层各自的理由：

- **基础层**（`38-48`）：LLVM 和 NVVM 是最终目标，当然 legal；`ModuleOp` 是容器；`UnrealizedConversionCastOp` 必须 legal，否则 partial conversion 自己插的 cast 会立刻变成 illegal，陷入死循环。注释写得很明确：这个基类被多个 scoped partial conversion 共享，所以只列“每个阶段都合法”的 IR。
- **CF 层**（`55`）：`cf` 在 op lowering 期间必须合法，因为 `scf_to_cf` 已经把循环变成了 `cf.br`/`cf.cond_br`，而 `ModuleAxisInfoAnalysis` 还要在带 CF 的 IR 上跑。等 axis info 用完，第 6 步再把 `cf` 标 illegal。
- **保留层**（`67-72`）：`warp_specialize` 和 `thread_scope` 是“结构性 op”，它们的 region 里还有没降的 Triton op。现在降会把 region 拆坏，所以先 legal，等专门的 pass。

### 2. TypeConverter：tensor 变 struct，memdesc 变指针

`lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp:20-59` 注册了六类转换：

| 源类型 | 目标类型 | 行号 |
|---|---|---|
| `tt.ptr<T, Global>` | `!llvm.ptr<1>` | `24-30` |
| `tt.ptr<T, Descriptor>` | `!llvm.ptr`（generic） | `31-33` |
| `tt.tensordesc` | `!llvm.ptr` | `37-39` |
| `RankedTensorType` | `!llvm.struct<...>` | `44-46` |
| `MemDescType` | 指针或 `struct<ptr..., i32...>` | `47-49` |
| `AsyncTokenType` | `i32` | `50-52` |
| fp8 / `PackedFloat4E2M1` | `i8` | `54-58` |

tensor 的规则只有一行：

```cpp
Type eltType = convertType(type.getElementType());
unsigned numElementsPerThread = getUniqueElemsPerThread(type);
SmallVector<Type, 4> types(numElementsPerThread, eltType);
return LLVM::LLVMStructType::getLiteral(ctx, types);
```

（`TypeConverter.cpp:61-68`）

也就是说，**tensor 在 LLVM 层不是向量，而是一个“每线程元素个数”长度的字面 struct**。`tensor<256xf32, #blocked>` 在 `sizePerThread=[2]` 的布局下变成 `!llvm.struct<(f32, f32)>`；在 `sizePerThread=[4,4]` 的 2D 布局下变成 16 个 f32 的 struct。

为什么用 struct 而不是 `<2 x float>`？因为 Triton 的“每线程元素”不保证在寄存器里连续，也不保证能向量化；struct 的每个字段是独立的 SSA 值，lowering 可以逐个处理、逐个优化。真正的向量化留到 `packLLVector` / `unpackLLVector`（`Utility.h:775-778`）和 PTX 层的 `ld.global.v2.b32`。

memdesc 的规则分两种（`TypeConverter.cpp:70-104`）：

```cpp
if (isa<TensorMemoryEncodingAttr, TensorMemoryScalesEncodingAttr>(type.getEncoding()))
  return ptrType;                       // TMEM：就是一个指针
// 否则：base ptr(s) + 每维一个 i32 offset
```

TMEM 只返回指针，是因为 TMEM 的“offset”已经编码进 32-bit 地址的 row/col 字段，不需要额外的 offset 数组（见 Ch 21）。普通 SMEM memdesc 则返回 `struct<ptr, i32, i32, ...>`，因为它的 subview 需要运行期 offset。

### 3. Pattern 集合与 benefit

`populateConversionPatterns`（`211-276`）一次性注册几十组 pattern，顺序大致是：

```text
convert_layout → tmem subview → TMA → dot → elementwise → clampf
→ load/store → reduce → scan → gather → barrier → cluster
→ histogram → print → control flow → SPMD → arith/math/gpu/ub
→ view → assert → memory → tensor memory → make_range
→ tcgen5 mma → fp4 → instrumentation → fpsan → gsan
```

`benefit` 在函数入口就定成 10：

```cpp
const int benefit = patternBenefitPrioritizeOverLLVMConversions;
```

（`214`；常量定义在 `include/triton/Conversion/TritonGPUToLLVM/PatternTritonGPUOpToLLVM.h:18-22`）

三个常量的含义：

| 常量 | 值 | 用途 |
|---|---|---|
| `patternBenefitDefault` | 1 | 普通 pattern，和 MLIR 自带 pattern 同级 |
| `patternBenefitPrioritizeOverLLVMConversions` | 10 | 让 Triton 的 tensor-aware pattern 先于 `arith`/`math` 的标量 pattern 匹配 |
| `patternBenefitClampOptimizedPattern` | 20 | 优化变体，优先于通用变体 |

为什么需要 10？因为 `populateConversionPatterns` 末尾也注册了 MLIR 的 `arith::populateArithToLLVMConversionPatterns`（`253-254`），那些 pattern 的 benefit 是 1。一个 `arith.addf` 同时匹配“Triton 的 tensor 版”和“MLIR 的标量版”时，benefit 高的先试。没有这层优先级，`arith.addf` 会因为操作数类型是 tensor 而匹配失败，最后报 “failed to legalize”。

### 4. partial conversion 与 full conversion

| 维度 | full conversion | partial conversion |
|---|---|---|
| 要求 | 所有 op 必须 legal | 只有 target 标 illegal 的必须被改写 |
| 未注册的 dialect | 报错 | 原样保留 |
| 适合 | 单一 dialect 的完整降级 | 多 dialect、分阶段、允许扩展 |
| 本 pass | 不用 | 三段都用 |

`applyPartialConversion` 的语义是：**遍历 IR，对每个 illegal op 找 pattern；找不到就失败；legal op 不动。** 这正好允许 `warp_specialize`、`nvgpu`、proton 这些“现在不该动”的 IR 活下来。

## 底层机制

### 1. 为什么先 lower functions

`lowerFunctions` 只有 9 行（`201-209`）：

```cpp
NvidiaLLVMConversionTarget target(*mod.getContext());
target.addIllegalOp<triton::FuncOp>();
RewritePatternSet patterns(mod.getContext());
populateFuncOpConversionPattern(typeConverter, patterns, targetInfo,
                                patternBenefitDefault);
return applyPartialConversion(mod, target, std::move(patterns));
```

它用的 target 是**基类**，只把 `triton::FuncOp` 标 illegal。也就是说这一步只改函数签名，函数体里的 `tt.load`、`arith.addf` 都还是原样。

这一步必须在 op lowering 之前，原因有三：

1. **op lowering 要读函数签名**。`FuncOpConversion` 会给 kernel 加 `nvvm.kernel`、`reqntid`、`cluster_dims`，给 device function 加 `noinline`；后面的 call/return pattern 要按这个签名生成 `llvm.call`。
2. **shared memory 基地址要能解析**。`createSharedMemoryGlobal` 在函数降完之后、op 降之前插入 `global_smem`（`178-180` 的注释：“must exist before call and return conversion so those patterns can resolve each function's shared-memory base address”）。
3. **device function 的参数个数会变**。`amendFuncOp` 会给每个非 kernel 函数追加 shared 指针、global scratch、profile scratch 三个参数（`Utility.cpp:2318-2374`）。如果先降 op，函数体里的 `llvm.load` 已经按旧参数生成了，再改签名就要大面积重写。

### 2. `tt.func` → `llvm.func` 到底改了什么

`amendFuncOp`（`Utility.cpp:2318-2374`）做三件事：

```cpp
if (!isKernel) amendedInputTy.push_back(sharedPtrTy);
amendedInputTy.push_back(globalPtrTy);
amendedInputTy.push_back(profilePtrTy);
```

- **非 kernel**：追加 `shared` 指针（addrspace 3）；
- **所有函数**：追加 global scratch 指针（addrspace 1）和 profile scratch 指针（addrspace 1）；
- kernel 不加 shared 指针，因为 kernel 的 shared memory 是一个 module 级 symbol `global_smem`，不是参数。

`createSharedMemoryGlobal`（`TritonGPUToLLVM.cpp:105-115`）生成的就是这个 symbol：

```mlir
llvm.mlir.global external @global_smem() {addr_space = 3 : i32, alignment = 16 : i32}
  : !llvm.array<0 x i8>
```

0 长度数组 + external linkage 表示“动态 shared memory”，16 字节对齐是因为 `4xi32` 是最宽的访问。

**实测**（T4，一个 3 参数的 elementwise kernel）：

```llvm
define ptx_kernel void @ew(ptr addrspace(1) %0, ptr addrspace(1) %1, i32 %2,
                           ptr addrspace(1) nofree readnone captures(none) %3,
                           ptr addrspace(1) nofree readnone captures(none) %4)
    local_unnamed_addr #0 !dbg !4
```

5 个参数 = 3 个用户参数 + global scratch + profile scratch；没有 shared 参数，因为它是 kernel。`#0` 里的 `"nvvm.reqntid"="128"` 来自 `attachKernelAttributes`（`FuncOpToLLVM.cpp:102-103`，`32 * numWarps`）。

`attachKernelAttributes` 还负责（`68-104`）：

- `nvvm.kernel = 1`：告诉 NVPTX 这是 entry；
- `num_ctas > 1` 时设 `nvvm.cluster_dims`；
- module 上有 `ttg.maxnreg` 时设 `nvvm.maxnreg`；
- `reqntid = 32 * numWarps`。

device function 则设 `noinline` + internal linkage（`128-137`），避免 LLVM 把它内联掉后破坏 warp specialization 的边界。

### 3. tensor 的 struct 在 lowering 里怎么用

`ElementwiseOpConversionBase`（Ch 23 详述）的标准动作是：

```cpp
auto subOperands = unpackUniqueTensorElements(loc, operand, rewriter);
...
Value view = packUniqueTensorElements(loc, typeConverter, resultVals,
                                      rewriter, resultTy);
```

（`include/triton/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVMBase.h:118-144`）

`unpackLLElements` 的实现非常朴素（`Utility.cpp:1080-1096`）：

```cpp
if (llvmStruct.getType().isIntOrIndexOrFloat() || isa<LLVM::LLVMPointerType>(...))
  return {llvmStruct};                        // 标量直接返回单元素
ArrayRef<Type> types = cast<LLVM::LLVMStructType>(llvmStruct.getType()).getBody();
for (unsigned i = 0; i < types.size(); ++i)
  results[i] = b.extract_val(type, llvmStruct, i);
```

`packLLElements` 对称：先 `LLVM::UndefOp`，再逐字段 `insert_val`，并且严格检查每个值的类型和 struct 字段类型一致（`1112-1144`）。类型不匹配直接 `report_fatal_error`，不会静默 bitcast。

**实测**：`tensor<256xf32, #blocked>` 在 `sizePerThread=[2]` 下，LLIR 里看到的是两个独立的 `fmul` / `fadd`，而不是一个 struct 变量——因为 `InsertValueOp` / `ExtractValueOp` 在 canonicalize 阶段被折掉了。想看到 struct 本身，要在 `-canonicalize` 之前 dump，或者找一个 struct 无法被完全拆开的 op（比如 reduce 的 scratch 交换）。

### 4. `UnrealizedConversionCastOp` 何时出现

它是 partial conversion 的“类型补丁”。当 pattern 产生的新值类型和 op 期望的类型不一致，而 op 还没降时，框架会插一个 cast，让 IR 暂时合法。

一个真实例子在 reduce 的 lowering 里（`lib/Conversion/TritonGPUToLLVM/ReduceOpToLLVM.cpp:508-527`）：

```cpp
Value packed = packUniqueTensorElements(loc, getTypeConverter(), inVals[i], rewriter, srcTy);
auto srcTensor = UnrealizedConversionCastOp::create(rewriter, loc, srcTy, packed).getResult(0);
auto cvt = triton::gpu::ConvertLayoutOp::create(rewriter, loc, dstTy, srcTensor);
...
Type packedDstTy = getTypeConverter()->convertType(dstTy);
auto packedDst = UnrealizedConversionCastOp::create(rewriter, loc, packedDstTy, cvt.getResult()).getResult(0);
outVals[i] = unpackUniqueTensorElements(loc, packedDst, rewriter);
```

这里 `packUniqueTensorElements` 返回的是 `!llvm.struct`，但 `ConvertLayoutOp` 还没降，它的 operand 必须是 `tensor`。于是：

```text
struct --unrealized_cast--> tensor --convert_layout--> tensor --unrealized_cast--> struct
```

两个 cast 都是“假的”，等 `ConvertLayoutOp` 被 pattern 降掉之后，它们就变成 struct→struct 的恒等转换，可以被 canonicalize 消掉。

target 里 `UnrealizedConversionCastOp` 是 legal 的（`TritonGPUToLLVM.cpp:46`），所以它们能活到这一步。真正强制清理是在 warp specialization 的嵌套 pipeline 里：

```cpp
OpPassManager pm;
pm.addPass(createReconcileUnrealizedCastsPass());
```

（`ConvertWarpSpecializeToLLVM.cpp:287-290`）

注意：**这个主 pass 自己不跑 reconcile**。它依赖后续的 canonicalize / warp-specialize 流程把 cast 消掉。如果你在这个 pass 之后立刻 dump，看到残留的 `unrealized_conversion_cast` 是正常的，不一定是 bug。

### 5. 一个 op 的 legalize 全过程

以 `arith.addf` 为例（**实测**，T4）：

TTGIR：

```mlir
%2 = arith.mulf %x_6, %cst : tensor<256xf32, #blocked>
%3 = arith.addf %2, %cst_0 : tensor<256xf32, #blocked>
```

`arith.addf` 的 operand 是 tensor，类型是 `!llvm.struct<(f32, f32)>`。MLIR 自带的 `ArithToLLVM` pattern 只处理标量，匹配失败；Triton 在 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/ElementwiseOpToLLVM.cpp:863-865` 注册了：

```cpp
POPULATE_OP(arith::SubFOp, LLVM::FSubOp);
POPULATE_OP(arith::AddFOp, LLVM::FAddOp);
POPULATE_OP(arith::MulFOp, LLVM::FMulOp);
```

这个 pattern 的 benefit 是 10，先匹配成功。它把两个 struct 各拆成 2 个 f32，生成 2 条 `llvm.fadd`，再 pack 回 struct。

LLIR：

```llvm
%21 = fmul float %18, 2.000000e+00
%22 = fmul float %19, 2.000000e+00
%23 = fadd float %21, 1.000000e+00
%24 = fadd float %22, 1.000000e+00
```

PTX：

```text
fma.rn.f32 %r3, %r1, 0f40000000, 0f3F800000;
fma.rn.f32 %r4, %r2, 0f40000000, 0f3F800000;
```

三步各有归属：TTGIR 的 tensor op 由 Triton pattern 拆成标量 LLVM op；LLIR 的 `fmul`+`fadd` 由 LLVM 的 instruction selection 合成 `fma`（默认 fp fusion 开启）。**Triton 的 elementwise lowering 没有生成 fma，fma 是 LLVM 后端做的**。

### 6. lowerControlFlow 与 finalizeModule

`lowerControlFlow`（`290-301`）先 `inlineThreadScopes`，把 `ttg.thread_scope` 的 region 直接内联成 CFG（`78-103`），然后把 `cf` 标 illegal，用 MLIR 的 `populateControlFlowToLLVMConversionPatterns` 降成 `llvm.br`/`llvm.cond_br`。

`finalizeModule`（`303-316`）做三件收尾：

```cpp
if (numCTAs == 1)
  // 把所有 ttg.cluster_cta_id 折成常量 0
fixUpLoopAnnotation(mod);
makeAllWarpGroupsIsolatedFromAbove(mod);
```

第一件很重要：单 CTA 时 `cluster_cta_id` 恒为 0，折掉能省掉一次特殊寄存器读取，也能让后续优化看到常量。第二件修 loop 的 annotation，第三件保证 warp group 的 region 是 isolated-from-above，方便后面的 warp specialization pass 独立处理。

## 进一步用法/重要变体

### 1. 新增一个 lowering pattern 的最小步骤

1. 在 `include/triton/Conversion/TritonGPUToLLVM/PatternTritonGPUOpToLLVM.h` 声明 `populateXxxToLLVMPatterns`；
2. 在 `.cpp` 里写 `ConvertOpToLLVMPattern<YourOp>`，实现 `matchAndRewrite`；
3. 在 `populateConversionPatterns`（`211-276`）里加一行；
4. 如果新 op 有自己的类型，确认 `TypeConverter` 能处理，否则先加 conversion；
5. 如果新 op 必须在某个阶段之后降，把它加到 `TritonLLVMConversionTarget` 的 legal 列表，或单独开一个 partial conversion。

第 5 步是最容易漏的。`warp_specialize` 就是靠 legal 列表活到后面的；如果你把新 op 标 illegal 却没有 pattern，`applyPartialConversion` 会直接失败并打印 “failed to legalize operation”。

### 2. 设备函数与调用约定

`tt.func` 里 `isKernel == false` 的函数会拿到 shared 指针 + global scratch + profile scratch 三个额外参数（`Utility.cpp:2342-2346`），并设 `noinline`（`FuncOpToLLVM.cpp:132-134`）。调用点由 `CallOpConversion` 按同一个签名生成。这意味着：

- 设备函数里可以用 shared memory，因为指针是显式传进来的；
- 但设备函数不能有 TMEM（`NVGPUToLLVMPass.cpp:643-644` 直接 assert “TODO: add support for function calls using tmem”）；
- 设备函数不会被内联，所以它的 warp specialization 边界是稳定的。

### 3. 延后 lowering 的三类 IR

| IR | 为什么延后 | 谁处理 |
|---|---|---|
| `ttg.warp_specialize` 家族 | region 里还有 Triton op | `add_warp_specialize_to_llvm`（`compiler.py:462`） |
| `ttg.thread_scope` | 需要保留 scope 归属到内存降完 | `inlineThreadScopes`（本 pass 第 6 步） |
| `triton_nvgpu` | 已经是 LLVM 级 IR，只是不在 LLVM dialect 里 | `add_nvgpu_to_llvm`（`compiler.py:463`） |

### 4. AMD 后端的对照

AMD 的 `third_party/amd/lib/TritonAMDGPUToLLVM/` 复用同一套 `TritonGPUToLLVMTypeConverter` 和大部分 pattern 接口，但 target 和 pattern 集合不同。看两边的 `TritonGPUToLLVM.cpp` 可以最快理解“哪些是共享框架、哪些是 NVIDIA 专有”。

## 对比与取舍

### struct-of-elements vs vector

| 方案 | 优点 | 缺点 | Triton 的选择 |
|---|---|---|---|
| `!llvm.struct<(f32, f32)>` | 每个元素独立 SSA，易优化、易处理非连续布局 | 不能直接映射到向量指令 | ✅ 默认 |
| `<2 x float>` | 可直接生成向量指令 | 要求元素在寄存器里连续，layout 稍复杂就非法 | 只在 pack/unpack 和 PTX 层用 |

### 先 function 还是先 op

| 顺序 | 结果 |
|---|---|
| 先 function 后 op（当前） | 签名稳定、shared global 已就位、call/return 可解析 |
| 先 op 后 function | op 按旧签名生成，改签名要重写函数体；shared base 无处解析 |

### partial vs full

| 场景 | 选择 |
|---|---|
| 单一 dialect 完整降级 | full |
| 多 dialect、扩展可保留、分阶段 | partial（本 pass） |
| 需要临时 cast | partial + legal `UnrealizedConversionCastOp` |

## 踩雷集锦

1. **以为这个 pass 会一次把 IR 降到“纯 LLVM”。**
   不会。它保留 `cf`（第 6 步才降）、`nvgpu`、`warp_specialize`、`thread_scope`，也允许 `unrealized_conversion_cast` 残留。看 dump 时看到这些不要以为 pass 失败了。

2. **把 `UnrealizedConversionCastOp` 当成错误。**
   它是 partial conversion 的正常中间态（`ReduceOpToLLVM.cpp:508-527`）。真正的错误是“cast 的两端类型永远对不上”，那会在 `reconcile` 或后续 verifier 报错。判断方法：看 cast 的 src/dst 是否在后续 pattern 里被消掉。

3. **给新 pattern 用默认 benefit。**
   Triton 自己的 pattern 统一用 10（`TritonGPUToLLVM.cpp:214`），MLIR 的 arith/math pattern 是 1。新 pattern 如果用默认 1，可能输给标量版 pattern，表现为“明明注册了却没匹配”。

4. **在 `populateConversionPatterns` 里漏掉 `TypeConverter`。**
   新 op 的 operand 如果是自定义类型，`convertType` 返回 null，pattern 会在 `getTypeConverter()->convertType(...)` 处崩，或者在 partial conversion 里报 “failed to legalize”。先加 conversion，再加 pattern。

5. **以为 `global_smem` 是函数参数。**
   kernel 的 shared memory 是 module 级 symbol（`TritonGPUToLLVM.cpp:105-115`），不是参数；只有非 kernel 函数才把 shared 指针作为参数（`Utility.cpp:2342-2344`）。**实测**的 LLIR 里 kernel 只有 5 个参数，其中没有 shared。

6. **在 kernel 上期待 `noinline`。**
   `noinline` 只加给 device function（`FuncOpToLLVM.cpp:128-137`）；kernel 是 external + `nvvm.kernel`，不会被内联。

## 进阶

- 读 `lib/Conversion/TritonGPUToLLVM/ControlFlowOpToLLVM.cpp`，看 `scf.if` / `scf.for` 在 `scf_to_cf` 之后还剩什么，以及 `cf` 的 pattern 如何和 `llvm.br` 的 block argument 对齐。
- 读 `lib/Conversion/TritonGPUToLLVM/ConvertLayoutOpToLLVM.cpp` 的 pattern 选择：它按 src/dst layout 分派到 shared memory 路径或 register shuffle 路径，是 benefit 20 的典型用户。
- 读 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/ConvertWarpSpecializeToLLVM.cpp:270-300`，看它如何重建 type converter、跑 reconcile、再重写 warp id。
- 读 `lib/Conversion/TritonGPUToLLVM/AllocateSharedMemory.cpp`，理解 shared memory 的 offset 是在哪个 pass 算的，以及它和 `global_smem` 的关系。

## 动手练习

1. **数参数**：写一个带 `@triton.jit` device function 的 kernel，dump LLIR，数 kernel 和 device function 各有多少参数，对照 `amendFuncOp` 解释每一个。
2. **看 struct**：把 elementwise kernel 的 `sizePerThread` 调大（例如用 2D tile），在 LLIR 里找 `insertvalue` / `extractvalue`，确认 struct 的长度等于 `getUniqueElemsPerThread`。
3. **制造 cast**：写一个 reduce kernel，在 `convert-triton-gpu-to-llvm` 之后、canonicalize 之前 dump，找出 `unrealized_conversion_cast`，说明它连接的是哪两个 op。
4. **改 benefit**：把 `arith::AddFOp` 的 pattern benefit 从 10 改成 1，重新编译，观察是否出现 “failed to legalize” 或回退到标量路径。
5. **对照 target**：列出 `TritonLLVMConversionTarget` 里所有 legal op，逐个说明“为什么现在不降”。**guided**，需要读源码而不是运行。

## 本章重点

- 这个 pass 是三次 partial conversion：functions → ops → CF，不是一个 full conversion。
- ConversionTarget 只把 LLVM/NVVM/Module/cast 设为永远 legal；`cf`、`nvgpu`、`warp_specialize`、`thread_scope` 按阶段延后。
- TypeConverter 把 tensor 变成“每线程元素个数”的字面 struct；TMEM memdesc 只变成一个指针。
- 先 lower functions 是为了让签名、shared global、额外参数先稳定。
- `UnrealizedConversionCastOp` 是 partial conversion 的类型补丁，target 允许它存活，由后续 reconcile 清理。
- benefit 10 让 Triton 的 tensor-aware pattern 先于 MLIR 的标量 pattern 匹配。
- kernel 的 shared memory 是 `global_smem` symbol，不是函数参数。

## 自我检核

- 你能说出 `runOnOperation` 里五个阶段的顺序，并解释每一步为什么不能提前吗？
- 你能写出 `tensor<128x256xf16, #blocked>`（`sizePerThread=[4,4]`）在 LLVM 层的 struct 类型吗？
- 你能解释 `UnrealizedConversionCastOp` 在 reduce lowering 里为什么必须存在吗？
- 你能说出 benefit 1/10/20 的差别，并给出一个必须用 10 的例子吗？
- 你能指出 kernel 和 device function 在参数、linkage、`noinline` 上的三处不同吗？

## 延伸阅读

1. **`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp:38-316`**
   - 读哪里：两个 target、`runOnOperation`、`lowerFunctions`、`populateConversionPatterns`、`lowerControlFlow`、`finalizeModule`。
   - 学什么：整个框架的控制流和每个阶段的职责边界。
   - 与本章关系：本章的主线；建议按函数顺序读一遍，再回头看 target 的 legal 列表。

2. **`lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp:14-109`**
   - 读哪里：构造函数里的六类 `addConversion`，以及 `convertTritonTensorType` / `convertMemDescType`。
   - 学什么：tensor、memdesc、pointer、fp8 在 LLVM 层的具体类型。
   - 与本章关系：理解 struct 长度和 TMEM 指针的唯一权威来源。

3. **`lib/Conversion/TritonGPUToLLVM/FuncOpToLLVM.cpp:12-157` 与 `Utility.cpp:2318-2374`**
   - 读哪里：`amendFuncOp`、`attachKernelAttributes`、`FuncOpConversion::matchAndRewrite`。
   - 学什么：`tt.func` → `llvm.func` 的参数、属性、linkage 变化。
   - 与本章关系：解释**实测** LLIR 里那 5 个参数的来源。

4. **`lib/Conversion/TritonGPUToLLVM/ReduceOpToLLVM.cpp:495-530`**
   - 读哪里：`packUniqueTensorElements` + `UnrealizedConversionCastOp` + `ConvertLayoutOp` 的组合。
   - 学什么：cast 为什么出现、连接了哪两种类型。
   - 与本章关系：把“partial conversion 的类型补丁”从概念变成具体 IR。

5. **MLIR 官方文档 “Dialect Conversion”**
   - 读哪里：ConversionTarget、TypeConverter、`applyPartialConversion`、unrealized cast 四节。
   - 学什么：框架本身的语义，而不是 Triton 的用法。
   - 与本章关系：Triton 只是这套基础设施的一个用户；先懂框架，再读 Triton 的 target 会快很多。

## 下一章

下一章进入这个框架里最常用的一类 pattern：elementwise。看 `arith.addf` 的 tensor 如何被拆成 per-thread `llvm.fadd`，`tt.fp_to_fp` 的 fp8/fp4 路径怎么选指令，以及 bf16 的 `cvt` 为什么在 sm_75 上会失败。

→ [Ch 23 elementwise 与类型 lowering](./23-elementwise-and-type-lowering.md)
