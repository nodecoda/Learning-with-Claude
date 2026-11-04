# Ch 23 — 标量与逐元素 lowering

> 逐元素 lowering 的本质不是“把 op 换个名字”，而是把“张量”这层虚拟分布拆成每个线程手里的标量寄存器。

## 目标

Ch 22 讲了 `TritonGPU → LLVM` 的框架：三次 partial conversion、共享的 TypeConverter、benefit 10 的 pattern。本章把镜头拉到框架里最常用的一类 pattern——elementwise。读完本章，你应该能回答：

- 一个 `tensor<256xf32, #blocked>` 上的 `arith.addf`，为什么在 LLIR 里会变成两条 `fadd float`，而不是一条“向量加法”？
- `sizePerThread`、`num_warps` 和 LLIR 里 `fadd` 的条数是什么关系？为什么 `num_warps=8` 时只剩一条？
- `ElementwiseOpConversionBase` 如何用 `unpackLLElements` / `packLLElements` 把 struct 拆成标量、再装回去？
- `MultipleOperandsRange` 的 `[element][operand]` 索引方式，为什么允许一个 pattern 一次消费 2 个或 4 个元素？
- `tt.fp_to_fp` 的 f16/bf16/fp8/fp4 路径分别落到什么 LLVM/PTX 指令？T4（sm_75）上哪些能跑、哪些只能 `guided`？
- bf16 的“cvt 指令”在 sm_75 上为什么看不到 `cvt.rn.bf16.f32`，而是一串 `bfe/add/setp/selp/prmt`？
- int 提升到底发生在 semantic 层还是 lowering 层？为什么 LLIR 里看到的是 `sext` 而不是“自动提升”？

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码树 | `/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 3.9.0 |
| 运行环境 | `/home/ubuntu/aicomp/triton/.venv`，Python 3.12.16 |
| `triton` | 3.9.0（venv 里的预编译 wheel，不是本地源码构建） |
| `torch` | 2.5.1+cu124 |
| GPU | Tesla T4，`sm_75`，驱动 535.216.01 |
| CUDA toolkit | 12.4，`V12.4.131`；系统 Python 3.10.12 |

本章的 LLIR/PTX 片段来自 T4 上的真实编译（**实测**；fp8/fp4 在 T4 上不可用，相关部分标 `guided`），脚本只写在 `/tmp`，没有修改 Triton 源码。源码行号用 `nl -ba` 在 `a972119e` 上核对；运行用的是同版本 wheel，因此“源码依据”与“实测”版本号一致，但 wheel 不等于本地源码构建，二者边界要分清。复现方式（**实测**）：

```python
import torch, triton, triton.language as tl

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, BLOCK: tl.constexpr):
    offs = tl.arange(0, BLOCK)
    x = tl.load(x_ptr + offs); y = tl.load(y_ptr + offs)
    tl.store(out_ptr + offs, x + y)

x = torch.randn(256, device='cuda'); y = torch.randn(256, device='cuda')
out = torch.empty_like(x)
k = add_kernel[(1,)](x, y, out, BLOCK=256, num_warps=4)
torch.cuda.synchronize()
print(k.asm['ttgir']); print(k.asm['llir']); print(k.asm['ptx'])
```

编译失败的例子（fp8）用 `TRITON_KERNEL_DUMP=1 TRITON_DUMP_DIR=/tmp/dump23` 仍能拿到 `.ttir/.ttgir/.llir/.ptx`，因为失败发生在 `ptxas`，不是前面的 lowering。这个技巧在踩雷集锦里会再用到。

## 为什么需要这个

TTGIR 里的 tensor 不是“一块连续内存”，而是“一个逻辑形状 + 一个 layout”。`tensor<256xf32, #blocked>` 只说明逻辑上有 256 个 f32；真正决定每个线程拿几个元素的是 layout：

```text
#blocked = #ttg.blocked<{sizePerThread = [2], threadsPerWarp = [32],
                         warpsPerCTA = [4], order = [0]}>
```

这个 layout 下，一个 CTA 有 `32 * 4 = 128` 个线程，每个线程拿 2 个元素，正好覆盖 256。到了 LLVM 层，没有“tensor”这种类型，也没有“layout”这种属性；每个线程只有自己的寄存器。所以 elementwise lowering 必须做三件事：**按元素展开**（把每个线程负责的 N 个元素逐个取出来）、**逐元素生成标量 op**（`fadd float`、`add i32`、`cvt`、`ex2.approx.f32` 等）、**按原 layout 装回去**（让后续 load/store、convert_layout、reduce 仍能按 struct 形状消费结果）。

这三件事听起来机械，但它们是 Triton 能“用 Python 写张量、生成接近手写 CUDA 的标量代码”的关键。如果不在这一层展开，LLVM 既不知道怎么处理 `tensor`，也无法把每个线程的寄存器分配给具体的 PTX 指令。更具体地说，elementwise lowering 同时承担四个职责：

| 职责 | 例子 | 如果做错会怎样 |
|---|---|---|
| 擦除 layout | `tensor<256xf32, #blocked>` → `!llvm.struct<(f32, f32)>` | 后续 op 拿到错误元素个数 |
| 逐元素映射 | `arith.addf` → 两条 `fadd float` | 生成“张量加法”这种不存在的 LLVM op |
| 类型转换 | `arith.extsi` → `sext i8 to i32` | 符号扩展丢失，结果错 |
| 舍入/饱和 | `tt.fp_to_fp` → `cvt.rn.*` 或软件序列 | fp8/bf16 数值与参考实现不一致 |

## 先建立直觉

先看一条最小链路。TTGIR（**实测**，T4，`num_warps=4`，`sizePerThread=[2]`）：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [2], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>
%2 = arith.addf %x_1, %y_3 : tensor<256xf32, #blocked>
```

LLIR（**实测**）：

```llvm
%23 = fadd float %14, %20
%24 = fadd float %15, %21
```

（为可读性省略行尾的 `!dbg` 位置信息；下同。）

PTX（**实测**）：

```ptx
ld.global.v2.b32 { %r1, %r2 }, [ %rd1 + 0 ];
ld.global.v2.b32 { %r3, %r4 }, [ %rd2 + 0 ];
st.global.v2.b32 [ %rd3 + 0 ], { %r5, %r6 };
```

这条链路可以画成：

```text
tensor<256xf32, #blocked>          // 逻辑 256 个元素
  │  TypeConverter: getUniqueElemsPerThread = 2
  ▼
!llvm.struct<(f32, f32)>           // 每个线程 2 个 f32
  │  unpackLLElements: extractvalue 两次
  ▼
(f32, f32)                         // 两个独立 SSA 值
  │  ElementwiseOpConversion: 每个元素一次 fadd
  ▼
(f32, f32)                         // 两个结果
  │  packLLElements: insertvalue 两次
  ▼
!llvm.struct<(f32, f32)>           // 装回原形状
```

注意 PTX 里的 `ld.global.v2.b32`：**逐元素 lowering 不等于逐元素访存**。LLIR 里是两条标量 `fadd`，但 load/store lowering 之后，两个相邻的 f32 被合并成一条 64-bit 向量 load。元素级展开和访存级向量化是两层，不要混为一谈。再换一个 `num_warps`，元素个数立刻变化（**实测**）：

| `num_warps` | 线程数 | `sizePerThread` | LLIR `fadd float` 条数 | PTX load |
|---:|---:|---:|---:|---|
| 8 | 256 | 1 | 1 | `ld.global.b32` |
| 4 | 128 | 2 | 2 | `ld.global.v2.b32` |
| 2 | 64 | 4 | 4 | `ld.global.v4.b32` |

这张表是本章最重要的一张表：**LLIR 里的 op 条数由 layout 的 `sizePerThread` 决定，不由逻辑 tensor 大小决定**。看到 256 个元素就以为应该有 256 条 `fadd`，是初学者最常见的误解。

## 核心概念

### 1. tensor 在 LLVM 层是什么

`TritonGPUToLLVMTypeConverter` 对 `RankedTensorType` 的转换非常直接（`lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp:44-46`），`convertTritonTensorType` 只做两件事（`TypeConverter.cpp:61-68`）：

```cpp
Type eltType = convertType(type.getElementType());
unsigned numElementsPerThread = getUniqueElemsPerThread(type);
SmallVector<Type, 4> types(numElementsPerThread, eltType);
return LLVM::LLVMStructType::getLiteral(ctx, types);
```

所以 `tensor<256xf32, #blocked sizePerThread=[2]>` → `!llvm.struct<(f32, f32)>`；`sizePerThread=[4]` → 四个 f32 字段；`xf16` → `f16`；`xbf16` → `bf16`；`xf8E5M2` → `i8`（`include/triton/Conversion/TritonGPUToLLVM/TypeConverter.h:31-36` 把 fp8 全部映射成 i8）；`xfp4e2m1x2` → `i8`（`TypeConverter.cpp:57-58`）。这里有一个容易忽略的点：**fp8 和 fp4 在 LLVM 层不是浮点类型**。它们是整数容器，真正的浮点语义由后续的 `cvt`/inline PTX 解释。这就是为什么 fp8 的 lowering 大量使用位运算和 inline asm，而不是普通的 `fadd`。

### 2. `ElementwiseOpConversionBase` 的骨架

所有 elementwise pattern 的共同父类在 `include/triton/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVMBase.h:43-151`。核心是 `matchAndRewrite`（`:110-147`）：

```cpp
for (auto operand : adaptor.getOperands()) {
  auto subOperands = unpackUniqueTensorElements(loc, operand, rewriter);
  allOperands.resize(subOperands.size());
  for (auto v : llvm::enumerate(subOperands))
    allOperands[v.index()].push_back(v.value());
}
SmallVector<Value> resultVals;
for (auto it = allOperands.begin(), end = allOperands.end(); it != end;) {
  auto curr = static_cast<const ConcreteT *>(this)->createDestOps(
      op, adaptor, rewriter, elemTy, MultipleOperandsRange(it, end), loc);
  if (curr.size() == 0) return failure();
  for (auto v : curr) { if (!static_cast<bool>(v)) return failure();
                        resultVals.push_back(v); }
  it += curr.size();
}
resultVals = maybeDeduplicate(op, resultVals);
Value view = packUniqueTensorElements(loc, this->getTypeConverter(),
                                      resultVals, rewriter, resultTy);
rewriter.replaceOp(op, view);
```

这段代码有四个关键设计：**操作数先按元素分组**（`allOperands[i]` 是“第 i 个元素的所有操作数”）；**`createDestOps` 可以一次消费多个元素**（返回值 `curr.size()` 是这次消费了几个元素，普通 pattern 返回 1，fp8 可能返回 2 或 4）；**`MultipleOperandsRange` 是一个滑动窗口**（不复制数据，只把 `[it, end)` 包成可索引的 range）；**`maybeDeduplicate` 在打包前做常量性去重**（如果 axis analysis 发现某些元素恒定，可以复用同一个结果）。

### 3. `MultipleOperandsRange` 的结构

定义很短（`ElementwiseOpToLLVMBase.h:20-33`）：

```cpp
class MultipleOperandsRange
    : public iterator_range<SmallVector<SmallVector<Value>>::iterator> {
  using ContainerT = SmallVector<SmallVector<Value>>;
public:
  ContainerT::reference operator[](ContainerT::size_type idx) {
    return begin()[idx];
  }
  ContainerT::size_type size() const { return end() - begin(); }
};
```

它的元素类型是 `SmallVector<Value>`，索引方式是 `operands[element_index][operand_index]`。例如一个二元 op，当前窗口有 2 个元素：`operands[0] = {lhs_0, rhs_0}`，`operands[1] = {lhs_1, rhs_1}`。`CmpIOpConversion` 只用当前元素的第 0、1 个操作数（`lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:71-79`）：

```cpp
return {LLVM::ICmpOp::create(rewriter, loc, elemTy,
                             ArithCmpIPredicateToLLVM(op.getPredicate()),
                             operands[0][0], operands[0][1])};
```

`FpToFpOpConversion` 则可能一次看 2 个元素（`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/ElementwiseOpToLLVM.cpp:470-472`）：

```cpp
for (unsigned i = 0; i < std::min(numElements, operands.size()); i++)
  inVals.push_back(operands[i][0]);
```

这就是为什么 `MultipleOperandsRange` 不是简单的 `ValueRange`：它要支持“一个 pattern 一次处理多个元素”的打包转换。

### 4. `unpackLLElements` / `packLLElements`

拆包在 `lib/Conversion/TritonGPUToLLVM/Utility.cpp:1080-1096`：标量（int/index/float/pointer）直接返回 `{llvmStruct}`；否则按 struct body 逐字段 `extract_val`。装包在 `Utility.cpp:1112-1144`：如果 `convertType` 不是 struct，断言只有一个结果并直接返回；否则先 `UndefOp`，再逐字段 `insert_val`。这两个函数把“标量”和“tensor”统一成了同一条路径：标量 `unpack` 返回单元素、`pack` 直接返回该值；tensor `unpack` 返回 N 个 `extractvalue`、`pack` 返回 N 个 `insertvalue`。

注意 `packLLElements` 的类型检查是**致命错误**，不是静默 bitcast（`Utility.cpp:1133-1139`）：

```cpp
if (value.getType() != elementTypes[i]) {
  emitError(loc) << "invalid element type in packLLElements. Expected "
                 << elementTypes[i] << " but got " << value.getType();
  llvm::report_fatal_error("element type mismatch when packing elements for LLVM struct");
}
```

一个 pattern 如果返回 `i32` 而 struct 字段期望 `f32`，编译会直接 abort。这是写自定义 elementwise pattern 时最容易踩的坑之一。

### 5. `TritonLLVMOpBuilder`

`include/triton/Conversion/TritonGPUToLLVM/Utility.h:98-320` 定义了一个轻量包装器，把常用的 LLVM op 变成短方法：`b.fadd/fmul/add`（浮点/整数算术）、`b.zext/sext`（扩展）、`b.fpext/fptrunc`（浮点加宽/收窄）、`b.bitcast`（同宽重解释）、`b.insert_val/extract_val`（struct 字段）、`b.insert_element/extract_element`（vector lane）、`b.int_val/f32_val/bf16_val`（常量）、`b.icmp_*`（整数比较）。它的价值不是“少写几个字符”，而是把 location 和 builder 绑定在一起，避免每个 pattern 都重复写 `rewriter, loc`。读 lowering 代码时，看到 `b.` 就知道这是 LLVM dialect 的 op，而不是 Triton op。

## 底层机制

### 1. `arith.addf` 的完整逐元素路径

以本章开头的 add kernel 为例，完整路径是 `TTIR arith.addf tensor<256xf32>` → `TTGIR arith.addf tensor<256xf32, #blocked>` → `LLIR 两条 fadd float` → `PTX ld.global.v2.b32 / st.global.v2.b32`。对应的 pattern 注册在 NVIDIA 后端（`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/ElementwiseOpToLLVM.cpp:863-865`）：

```cpp
POPULATE_OP(arith::SubFOp, LLVM::FSubOp);
POPULATE_OP(arith::AddFOp, LLVM::FAddOp);
POPULATE_OP(arith::MulFOp, LLVM::FMulOp);
```

`POPULATE_OP` 展开成 `ElementwiseOpConversion<arith::AddFOp, LLVM::FAddOp>`。这个模板的 `createDestOps` 在 `ElementwiseOpToLLVMBase.h:164-174`，只处理 `operands[0]`，也就是当前窗口的第一个元素，返回 1，基类把 `it` 前进 1，继续下一个元素。于是 2 个元素/线程 → 调用 2 次 → 2 条 `fadd float`；1 个元素/线程 → 1 条。**实测**的 `num_warps=2` 进一步验证了这个关系：4 个元素/线程，LLIR 里正好 4 条 `fadd float`，PTX 用 `ld.global.v4.b32`。

这里还要注意 benefit。`populateElementwiseOpToLLVMPatterns` 注册这些 pattern 时用的是 `patternBenefitPrioritizeOverLLVMConversions = 10`（`include/triton/Conversion/TritonGPUToLLVM/PatternTritonGPUOpToLLVM.h:18-20`），而 MLIR 自带的 `arith → LLVM` pattern 是 1。这样 `arith.addf` 的 tensor 版本会先匹配；如果没有这层优先级，MLIR 的标量 pattern 会因为操作数是 tensor 而失败，最后报 “failed to legalize”。

### 2. scalar 与 tensor 的统一处理

标量路径不需要特殊分支。`unpackLLElements` 对标量直接返回 `{llvmStruct}`（`Utility.cpp:1083-1086`），`packLLElements` 在 `convertType` 不是 struct 时直接返回单值（`Utility.cpp:1114-1119`）。所以同一个 `ElementwiseOpConversion` 既能处理 `tensor<256xf32>`，也能处理 `f32`。**实测**一个纯标量 kernel：

```mlir
%x = tt.load %x_ptr : !tt.ptr<f32>
%y_1 = arith.mulf %x, %y_0 : f32
%y_2 = arith.addf %y_1, %y : f32
tt.store %o_ptr, %y_2 : !tt.ptr<f32>
```

LLIR 是 `fmul float` + `fadd float`，PTX 是 `fma.rn.f32`。两个观察：**scalar 不是 `tensor<1>`**（Triton 的 0-d tensor 被禁止，标量走 `f32` 类型，不是长度为 1 的 struct）；**LLIR 的 `fmul + fadd` 在 PTX 里被融合成 `fma.rn.f32`**，elementwise lowering 只负责生成正确的标量 op，融合是后面 LLVM/PTX 的事。不要用 PTX 的指令数去反推 lowering 的 op 数。

### 3. int 提升：semantic 层先做，lowering 层只做显式 cast

很多人以为“int8 + int32 的提升”发生在 LLVM lowering。实际上提升在 semantic 层就完成了（`python/triton/language/semantic.py:59-121`、`:180-199`）：

```python
ret_sca_ty = self.computation_type_impl(lhs_sca_ty, lhs_is_scalar,
                                        rhs_sca_ty, rhs_is_scalar, div_or_mod)
lhs = self._cast_to_computation_type(lhs, ret_sca_ty)
rhs = self._cast_to_computation_type(rhs, ret_sca_ty)
```

所以 TTIR 里已经能看到显式的 `arith.extsi`。**实测**（int8 tensor + int32 tensor）：

```mlir
%2 = arith.extsi %a_1 : tensor<256xi8> to tensor<256xi32>
%3 = arith.addi %2, %b_3 : tensor<256xi32>
```

LLIR 是 `sext i8 to i32` + `add i32`；PTX 是 `cvt.s32.s8` + `add.s32`。对应的 lowering 注册是（`lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:712-714`）：

```cpp
POPULATE_UNARY_OP(arith::TruncIOp, LLVM::TruncOp)
POPULATE_UNARY_OP(arith::ExtSIOp, LLVM::SExtOp)
POPULATE_UNARY_OP(arith::ExtUIOp, LLVM::ZExtOp)
```

以及 NVIDIA 后端的浮点 cast（`third_party/nvidia/.../ElementwiseOpToLLVM.cpp:872-874`）：`arith::ExtFOp → LLVM::FPExtOp`、`arith::TruncFOp → LLVM::FPTruncOp`、`arith::FPToSIOp → LLVM::FPToSIOp`。结论：**int 提升是 TTIR 里的显式 cast，lowering 只是把 `extsi` 映射成 `sext`、把 `trunci` 映射成 `trunc`。** 不要在 lowering 里找“自动提升”的逻辑，它不在那里。

### 4. `math` op：不是所有 elementwise 都是“换名字”

`math.exp` 没有简单映射成 `LLVM::ExpOp`，而是有一个专门的近似 pattern（`third_party/nvidia/.../ElementwiseOpToLLVM.cpp:557-576`）：

```cpp
if (getIntOrFloatOrPtrBitWidth(elemTy) != 32)
  return {};                                  // 非 fp32 回退到 libdevice
const double log2e = 1.4426950408889634;
Value prod = b.fmul(f32_ty, operands[0][0], b.f32_val(log2e));
return {NVVM::Ex2Op::create(rewriter, loc, f32_ty, prod, /*ftz=*/false)};
```

**实测**（T4，`tl.exp`，256 个 f32）：LLIR 是 `fmul float %13, f0x3FB8AA3B` + `llvm.nvvm.ex2.approx.f32`；PTX 是 `mul.f32` + `ex2.approx.f32`；`max rel err = 2.0866586680767796e-07`。这不是“精确 exp”，而是 `exp(x) = 2^(x * log2(e))` 的近似；精度足够大多数训练场景，但不是 libdevice 的 `__nv_expf`。如果输入不是 f32，`ExpOpConversionApprox` 返回空，基类会 `return failure()`，然后由后面注册的 `ElementwiseOpConversion<math::ExpOp, math::ExpOp>` 回退到 MLIR 的 math-to-LLVM 路径，最终调用 libdevice。这个“先尝试专用 pattern，失败再回退”的链式设计，是 elementwise 变体的常见模式。

### 5. `tt.fp_to_fp`：f16 / bf16 / fp8 / fp4 的分叉

`tt.fp_to_fp` 是类型转换的主战场。它和 `arith.extf/truncf` 的区别是：`tt.fp_to_fp` 可以携带 `rounding` 属性，并且专门处理 fp8/fp4 这些 LLVM 不直接支持的类型。

**f16 / bf16 / f32 之间**：`FpToFpOpConversion::createDestOps` 的第一段（`third_party/nvidia/.../ElementwiseOpToLLVM.cpp:435-452`）分三个分支：同类型直接返回；加宽用 `fpext`；收窄时如果源/目的同宽，先 `fpext` 到 f32，再 `fptrunc.round`。为什么同宽也要先 `fpext`？源码注释说得很清楚（`:446-447`）：LLVM cast 要求源/目的位宽不同；先把 16 位扩到 32 位，再 trunc 到 16 位，NVPTX 才有机会选择一条直接的 `f16 ↔ bf16` 转换，或者在 f32 中间值上做舍入。`convertFpTrunc` 用的是 `llvm.fptrunc.round` intrinsic（`:306-319`），`rounding` 映射成 `round.tonearest` 或 `round.towardzero`（`:296-303`）。这告诉 LLVM“我要哪种舍入”，而不是让后端猜。

**bf16 在 sm_75 上没有原生 cvt**：这是本章最值得记住的一个实测。一个 `f32 → bf16` 的 cast，TTIR 是 `arith.truncf`（**实测**；前端把 f32→bf16 直接生成了 `arith.truncf`，没有留下 `tt.fp_to_fp`，所以你在 TTGIR/LLIR 里找不到 `tt.fp_to_fp` 是正常的）。LLIR 是 `fptrunc <2 x float> %15 to <2 x bfloat>`。PTX（**实测**，sm_75，节选）是：

```ptx
bfe.u32 %r7, %r2, 16, 1;
add.s32 %r9, %r8, 32767;
setp.nan.f32 %p1, %r2, %r2;
selp.b32 %r11, %r10, %r9, %p1;
prmt.b32 %r3, %r16, %r11, 0x7632U;
```

这段序列在做 round-to-nearest-even 的 bf16 收窄：取保留位、加 `0x7fff`、处理 NaN、最后用 `prmt` 把两个 f32 的高 16 位拼成一个 32-bit 寄存器。**sm_75 上没有 `cvt.rn.bf16.f32`**，所以 NVPTX 后端把它展开成整数指令。`guided`：在 sm_80+ 上，NVPTX 通常会选择 `cvt.rn.bf16.f32`（或 packed 形式 `cvt.rn.bf16x2.f32`）。本章没有 sm_80 机器，不能把这条当实测；它是由 PTX ISA 和源码里的 `hasPackedBf16` 分支推出的预期（`third_party/nvidia/.../ElementwiseOpToLLVM.cpp:351-353`）：

```cpp
// Blackwell and newer can convert packed BF16/FP8 values directly.
// Require PTX 9.2 to support both directions.
bool hasPackedBf16 = computeCapability >= 100 && ptxVersion >= 92;
```

注意这个分支是 `cc >= 100 && ptx >= 92`，比“sm_80 有标量 cvt”更严格；它说的是 **packed** bf16 转换，不是所有 bf16 cvt。

**fp8：类型是 i8，转换靠 PTX**。fp8 在 LLVM 层是 `i8`（`TypeConverter.h:31-36`），所以 `tensor<256xf8E5M2>` 在 LLVM 层是 `!llvm.struct<(i8, i8)>`。真正的浮点转换由 `FpToFpOpConversion::getConversionFunc` 选择（`third_party/nvidia/.../ElementwiseOpToLLVM.cpp:340-411`）：

```cpp
if (computeCapability < 89 &&
    (isa<Float8E4M3FNType>(srcTy) || isa<Float8E4M3FNType>(dstTy))) {
  llvm::report_fatal_error("Conversion from/to f8e4m3nv is only supported "
                           "on compute capability >= 89\n");
}
if (computeCapability >= 89) {
  if (isa<Float8E4M3FNType, Float8E5M2Type>(dstTy) &&
      (srcTy.isF16() || srcTy.isBF16() || srcTy.isF32()) &&
      roundingMode == RoundingMode::RTNE) {
    bool useFp32 = srcTy.isF32() || (srcTy.isBF16() && !hasPackedBf16);
    return {makeNativeFp8Converter(srcTy, dstTy, useFp32), 2};
  }
}
```

两条路：**sm_89+ 原生路径**用 `NVVM::ConvertF32x2ToF8x2Op` / `ConvertF16x2ToF8x2Op` / `ConvertBF16x2ToF8x2Op`，一次处理 2 个元素，并带 `SATFINITE` 饱和模式（`:260-294`）；**sm_89 以下软件路径**用一张 `DenseMap` 查表，落到 `Fp_to_Fp8E5M2_RTNE`、`Fp16_to_Fp8E5M2_RTZ`、`Fp8E5M2_to_Fp16` 等 inline PTX 序列（`:372-410`）。**guided**：T4 是 sm_75，本章不把任何 fp8 数值结果当实测。实际尝试的结果见踩雷集锦：e4m3 在前端就被拒绝，e5m2 能生成 TTGIR/LLIR，但 `ptxas` 在 sm_75 上失败。

**fp4：一个 i8 装两个 nibble**。类型定义在 `include/triton/Dialect/Triton/IR/TritonTypes.td:18-25`：低 4 位是第一个 fp4，高 4 位是第二个。`Fp4ToFpOp` 的语义在 `include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:623-655`。TypeConverter 把 `PackedFloat4E2M1Type` 映射成 `i8`（`TypeConverter.cpp:57-58`）。基类的 `matchAndRewrite` 很短（`lib/Conversion/TritonGPUToLLVM/Fp4ToFpOpToLLVMBase.cpp:11-37`）：

```cpp
auto xVals = unpackUniqueTensorElements(loc, adaptor.getSrc(), rewriter);
SmallVector<Value> results;
results.reserve(xVals.size() * 2);
assert(xVals.size() % 4 == 0);
for (int i = 0; i < xVals.size(); i += 4) {
  Value packedVec = b.undef(vec_ty(i8_ty, 4));
  for (int j = 0; j < 4; ++j)
    packedVec = b.insert_element(packedVec, xVals[i + j], b.i32_val(j));
  auto upcast = upcastPackedFp4(op, rewriter, packedVec, elemType);
  results.append(upcast.begin(), upcast.end());
}
```

关键点：每 4 个 i8 组成一个 `<4 x i8>`，也就是 8 个 fp4；`upcastPackedFp4` 必须返回 8 个结果（`Fp4ToFpOpToLLVMBase.h:20-25`）；所以 `xVals.size()` 必须是 4 的倍数，否则 assert 失败。NVIDIA 的实现（`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/Fp4ToFpOpToLLVM.cpp:94-130`）分两条：sm_100+ / PTX 8.6+ 且目标是 f16 时用 `NVVM::ConvertF4x2ToF16x2Op`；否则用 `FP4ToFP16Ptx` 或 `FP4ToBP16Ptx` 的 inline PTX；bf16 在 sm_100+ / PTX 9.2+ 还可以用 `cvt.rn.bf16x2.e2m1x2`（`:37-45`、`:117-119`）。**guided**：T4 不支持 fp4 原生转换，本章不跑 fp4 数值实验；上面的结论全部来自源码。

### 6. 舍入与饱和规则

| 转换 | 舍入 | 饱和/特殊值 | 证据 |
|---|---|---|---|
| int 收窄 `trunci` | 截断（wrap） | 无饱和 | LLVM `trunc`，源码依据 |
| int 扩展 `extsi/extui` | 无 | 符号/零扩展 | 实测 `sext i8 to i32` |
| f32 → f16 | 默认 RTNE | IEEE 溢出到 inf | `fptrunc` / `fptrunc.round`，源码依据 |
| f32 → bf16 | RTNE | NaN 保留；sm_75 软件序列 | 实测 PTX `setp.nan/selp` |
| f32 → fp8 e5m2 | RTNE 或 RTZ | `SATFINITE`（原生路径） | 源码依据，T4 不可用 |
| f16 → fp8 | 只支持 RTNE | 源码直接 `report_fatal_error` | `:426-432`，源码依据 |
| fp8 → f16 | 无 rounding 参数 | 直接位扩展 | 源码依据 |
| fp4 → f16/bf16 | 原生 `cvt.rn` 或软件表 | e2m1 无 inf/nan 编码 | 源码依据，T4 不可用 |

两个容易忽略的细节：**f16 → fp8 只支持 RTNE**，源码在 `:426-432` 明确拒绝其他舍入模式；如果你传 `RTZ`，会在 lowering 时 fatal error，而不是悄悄换成 RTNE。**fp8 原生路径带 `SATFINITE`**，`makeNativeFp8Converter` 在 `:275-277` 和 `:284-286` 使用 `NVVM::SaturationMode::SATFINITE`；超出 fp8 有限范围的值会被饱和到最大有限值，而不是 inf（具体行为以 NVVM 语义为准）。

## 进一步用法/重要变体

**平凡映射：`ElementwiseOpConversion`**（`ElementwiseOpToLLVMBase.h:153-175`）。大多数 arith/math op 用这个模板就够了：它把 `SourceOp` 的 operand 直接喂给 `DestOp`，只做属性拆分。适合 `arith.addi/subi/muli → LLVM::AddOp/SubOp/MulOp`、`arith.andi/ori/xori → LLVM::AndOp/OrOp/XOrOp`、`math.floor/ceil/sqrt/rsqrt → 同名 math:: op`、`triton.bitcast → LLVM::BitcastOp`。不适合需要改变元素个数、需要多指令序列、需要 target-specific 指令的转换。

**内建函数：`ElementwiseToIntrinsicOpConversion`**（`ElementwiseOpToLLVMBase.h:177-204`）。如果目标是一条 LLVM intrinsic，可以用这个模板：它把 `intrinsic` 名字存成成员，`createDestOps` 只负责发 call。NVIDIA 后端没有大量使用它（AMD 后端用它处理 `ApproxDivFOp`），但它是理解“pattern 可以参数化”的好例子。

**自定义 `createDestOps`：`MulhiUIOpConversion`**（`lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:151-170`）。它把两个 N 位无符号数零扩展到 2N 位，相乘，右移 N 位取高半部分，再截回 N 位。但 NVIDIA 后端还注册了一个 benefit 更高的版本（`third_party/nvidia/.../ElementwiseOpToLLVM.cpp:853-856`）：`ElementwiseOpConversion<MulhiUIOp, LLVM::UMulHOp>`，benefit +1。所以在 NVIDIA 上 `LLVM::UMulHOp` 会先匹配；`MulhiUIOpConversion` 是通用回退。这个例子说明：**同一个 op 可以有多个 pattern，benefit 决定谁先试**；读 lowering 时不要只看一个文件，要看后端的注册顺序。

**打包变体：`ElementwiseInlineAsmOpConversion` 与 `MapElementwiseOpConversion`**。前者处理 `tt.elementwise_inline_asm`，会把小于 32 位的操作数打包进 32-bit 寄存器（`ElementwiseOpToLLVM.cpp:214-246`）；后者按 `pack` 属性把元素分组，每组调用一次 scalar op（`:615-693`）。这两个变体的共同点是：它们都利用 `MultipleOperandsRange` 的“一次消费多个元素”能力，而不是在 pattern 外面手动拼 struct。

**常量性去重：`maybeDeduplicate`**（`ElementwiseOpToLLVMBase.h:54-109`）。它用 axis analysis 的 `constancy` 信息，把“结果在多个元素上相同”的情况去重。只对无副作用、恰好一个结果、结果是 ranked tensor、axis analysis 能给出 constancy、且 constancy 不全为 1 的 op 生效。核心是用 linear layout 的伪逆把“寄存器索引”映射到“逻辑元素索引”，再把常量维度的基向量清零（`:84-100`），最后按映射复用结果（`:102-107`）。这不是“性能优化小技巧”，而是基类里唯一会改变结果值数量的地方。如果你写自定义 pattern，返回的 `resultVals` 数量必须和 `packLLElements` 期望的 struct 字段数一致；`maybeDeduplicate` 只会在打包前调整，不会改变 struct 类型。

## 对比与取舍

**逐元素标量 vs 打包向量**。同样是 2 个元素/线程：f32 的 LLIR 是两条 `fadd float`；bf16 的 LLIR 是一条 `fadd <2 x bfloat>`（均**实测**）。这个差异很容易让人误以为“bf16 的 elementwise pattern 会主动打包”。实际情况更微妙：`ElementwiseOpConversion` 仍然按元素调用，但 bf16 的 16 位值在 LLVM 层可以被合并成向量；具体由哪一步合并，本章没有单独插桩验证，按 `guided` 看待。可靠的结论是：**不要用 LLIR 的 op 条数反推元素个数**。要看 `sizePerThread`、struct 类型和 `unpack` 的次数。

**原生指令 vs 软件序列**：

| 场景 | sm_75（T4） | sm_80+ | sm_89+ | sm_100+ |
|---|---|---|---|---|
| bf16 cvt | 软件 `bfe/add/setp/selp/prmt`（实测） | `cvt.rn.bf16.f32`（guided） | 同左 | packed `cvt.rn.bf16x2`（guided） |
| fp8 e4m3 | 前端拒绝（实测） | 仍不支持 e4m3 | 原生 `cvt`（guided） | 原生 + packed（guided） |
| fp8 e5m2 | `ptxas` 失败（实测） | 软件/原生混合（guided） | 原生（guided） | 原生 + packed（guided） |
| fp4 | 不支持（guided） | 不支持 | 不支持 | 原生 `cvt.rn.bf16x2.e2m1x2`（guided） |

取舍很清楚：**原生指令**快、代码短，但受 compute capability 限制；**软件序列**可移植到老架构，但指令多、可能仍然引用新架构指令（fp8 e5m2 在 sm_75 上的失败就是例子）；**inline PTX** 能精确控制舍入和饱和，但把后端绑死在 PTX 上，AMD 需要另一套实现（`third_party/amd/lib/TritonAMDGPUToLLVM/ConvertFpCastOpToLLVM.cpp`）。

**通用 pattern vs 专用 pattern**：通用 `ElementwiseOpConversion` 一行注册、代码量小、风险低，适合一一映射；专用 `createDestOps` 几十行，适合多指令、打包、target-specific，但类型/数量不匹配会 fatal。经验法则：**能用通用模板就用通用模板；一旦需要改变元素个数、改变类型、或者依赖 compute capability，就写专用 pattern。** 专用 pattern 的 `createDestOps` 必须严格遵守“返回值的数量和类型与 struct 字段一致”，否则 `packLLElements` 会直接 abort。

## 踩雷集锦

**雷 1：以为 fp8 在 T4 上“只是慢一点”**。**guided（T4 不支持；下面是实际尝试的失败输出）**：`x.to(tl.float8e4nv)` 在 T4 上直接在前端失败：

```text
ValueError("type fp8e4nv not supported in this architecture.
The supported fp8 dtypes are ('fp8e4b15', 'fp8e5')")
```

这不是 lowering 的问题，而是前端/后端的能力检查。e4m3 需要 sm_89+；T4 只“认识” `fp8e4b15` 和 `fp8e5` 这两个名字，不代表它们能跑出正确结果。

**雷 2：以为 fp8 e5m2 的“软件路径”在 sm_75 上能用**。**guided（T4 不支持；下面是实际尝试的失败输出）**：`x.to(tl.float8e5)` 能生成 TTIR/TTGIR/LLIR，但 `ptxas` 报：

```text
error: Feature 'cvt.f16x2.f32' requires .target sm_80 or higher
error: Feature 'min.f16 or min.f16x2' requires .target sm_80 or higher
ptxas fatal: Ptx assembly aborted due to errors
```

原因：`Fp_to_Fp8E5M2_RTNE` 的 inline PTX 里用了 `cvt.rz.f16x2.f32` 和 `min.f16x2`，这些在 sm_75 上不存在。“软件路径”指的是“不用原生 fp8 指令”，不等于“sm_75 安全”。看到 `computeCapability < 89` 分支就以为能在 T4 跑，是本章最容易踩的坑。

**雷 3：在 sm_75 上找 `cvt.rn.bf16.f32`**。**实测**：`f32 → bf16` 的 PTX 是 `bfe/add/setp/selp/prmt`，没有 `cvt.rn.bf16.f32`。如果你按“bf16 应该有 cvt”去 grep PTX，会以为编译器没走 bf16 路径。实际上 NVPTX 在 sm_75 上用整数序列模拟了 RTNE 收窄；`cvt.rn.bf16.f32` 是 sm_80+ 的指令（`guided`）。

**雷 4：用逻辑 tensor 大小估算 LLIR 的 op 条数**。**实测**：同样是 `BLOCK=256`，`num_warps=8` → 1 条 `fadd float`，`num_warps=4` → 2 条，`num_warps=2` → 4 条。如果你写了一个 pattern，假设“每个 tensor 只有一条标量 op”，在 `num_warps=2` 时会少处理 3 个元素，结果直接错。正确做法是用 `getUniqueElemsPerThread(resultTy)` 或 `unpackUniqueTensorElements` 的实际返回长度，不要硬编码。

**雷 5：`packLLElements` 类型不匹配**。**源码依据**（`Utility.cpp:1133-1139`）：它不会帮你 bitcast；一个自定义 pattern 如果 `elemTy` 是 `f32`，但 `createDestOps` 返回了 `i32`（比如从 PTX inline asm 拿到的位模式），必须在返回前 `b.bitcast` 成 `f32`。`ExternElementwiseOpConversion` 处理 TF32 时就是这么做的（`ElementwiseOpToLLVM.cpp:189-202`）。

**雷 6：把 `getUniqueElemsPerThread` 和 `getTotalElemsPerThread` 混用**。`ElementwiseOpConversionBase` 用的是 `unpackUniqueTensorElements`，它按“唯一元素”计数；`getTotalElemsPerThread` 会把广播/重复的寄存器也算进去。对于有 broadcast 的 tensor，这两个数可能不同。用错会导致以为有 N 个元素、实际 struct 只有 M 个字段，`packLLElements` 报 size mismatch；或者更糟：数量碰巧对上，但语义错位。写 pattern 时，元素个数应该从 `unpackUniqueTensorElements` 的返回值拿，而不是从逻辑 shape 算。

**雷 7：在 LLIR 里找 `tt.fp_to_fp`**。**实测**：`f32 → bf16` 的 TTIR 是 `arith.truncf`，不是 `tt.fp_to_fp`。`tt.fp_to_fp` 主要出现在 fp8、fp4、以及显式指定 rounding 的转换里。如果你在 LLIR 里 grep `fp_to_fp` 找不到，不代表转换没发生；它可能已经被 canonicalize 成 `arith.truncf`，再由 `arith::TruncFOp → LLVM::FPTruncOp` 降掉。排查顺序应该是 TTIR → TTGIR → LLIR，而不是直接看 LLIR。

## 进阶

**用 axis analysis 消除冗余计算**。`maybeDeduplicate` 是 elementwise lowering 里最“编译器”的部分。它把 constancy 信息转成 linear layout 的零基向量，再用伪逆算出“哪些寄存器索引映射到同一个逻辑元素”。如果你要写一个高性能的自定义 elementwise op，先问：这个 op 的结果是否在某个维度上恒定？如果是，`maybeDeduplicate` 可能已经帮你省掉重复计算，不需要自己再写一遍。

**Blackwell 的 packed 转换**。`hasPackedBf16 = cc >= 100 && ptx >= 92` 和 fp4 的 `cvt.rn.bf16x2.e2m1x2` 说明：新架构的 elementwise lowering 越来越倾向于“一次处理两个 16 位值”。这带来的影响是：`createDestOps` 的 `numElements` 可能是 2 或 4，而不是 1；inline PTX 的 operand 约束要用 `"r"` 或 `"h"`，并处理 `undef` 填充；结果打包时要注意 `vec_ty` 和 `struct_ty` 的嵌套。本章没有 Blackwell 机器，这部分只能按源码 `guided`。

**性能视角：elementwise 不是瓶颈，访存才是**。`fadd`、`mul.f32`、`ex2.approx.f32` 的吞吐通常远高于全局访存。elementwise lowering 的优化目标不是“把两条 fadd 合成一条”，而是：不要生成多余的 `extractvalue/insertvalue`（canonicalize 会折掉大部分）；不要让类型转换退化成 libdevice 调用（比如非 f32 的 `exp`）；不要因为 pattern 返回类型错误导致 fatal error；尽量让 load/store 能向量化（这属于 Ch 24，不属于本章）。如果你在 profile 里看到 elementwise 占了大头，先检查是不是 `exp/log/sin` 走了 libdevice，而不是盯着 `fadd` 的条数。

**加一个新 elementwise op 的 checklist**：在 TTIR/TTGIR 里确定 op 的 operand/result 类型；选择基类（一一映射用 `ElementwiseOpConversion`，多指令用自定义 `createDestOps`）；用 `TritonLLVMOpBuilder` 生成 LLVM op；确保返回值的数量和类型与 `packLLElements` 的 struct 字段一致；用 benefit 10 注册，避免被 MLIR 的标量 pattern 抢先；加 lit test，至少覆盖 scalar、1 elem/thread、多 elem/thread 三种情况。

## 动手练习

1. **数 fadd**：把本章的 add kernel 分别用 `num_warps=2/4/8` 编译，dump LLIR，数 `fadd float` 的条数，和 `sizePerThread` 对照。再解释为什么 PTX 的 load 宽度也跟着变。
2. **换类型**：把 add kernel 的输入换成 `int8` 和 `int32`，在 TTIR 里找 `arith.extsi`，在 LLIR 里找 `sext`，在 PTX 里找 `cvt.s32.s8`。如果你把 `int8` 换成 `uint8`，应该看到 `zext` 和 `cvt.u32.u8`；动手验证。
3. **标量路径**：写一个只处理一个元素的 kernel（`tl.load(ptr)`，不是 `tl.load(ptr + tl.arange(0, 1))`），确认 TTIR 里是 `f32` 而不是 `tensor<1xf32>`，并解释 `unpackLLElements` 为什么返回单元素。
4. **bf16 观察**：在 T4 上跑 `f32 → bf16` cast，dump PTX，找出 `bfe/add/setp/selp/prmt` 序列。如果你有 sm_80+ 机器，对比是否出现 `cvt.rn.bf16.f32`（`guided`）。
5. **fp8 失败复现**：在 T4 上跑 `x.to(tl.float8e5)`，用 `TRITON_KERNEL_DUMP=1` 拿到 TTGIR/LLIR，确认 `tt.fp_to_fp` 还在，然后读 `ptxas` 报错，解释为什么“软件路径”仍然需要 sm_80 指令。
6. **读 `maybeDeduplicate`**：构造一个结果在某个维度恒定的 elementwise kernel（比如 `tl.broadcast_to` 后的加法），在 LLIR 里观察结果值是否被复用。**guided**，需要你手动对比 dump。

## 本章重点

- elementwise lowering 的核心是“按 layout 展开成每线程 N 个标量”，不是“把 tensor op 换成向量 op”。
- `tensor` 在 LLVM 层是 `!llvm.struct`，字段数 = `getUniqueElemsPerThread`；`unpackLLElements`/`packLLElements` 是 struct ↔ 标量的唯一通道。
- `ElementwiseOpConversionBase` 用 `[element][operand]` 的 `MultipleOperandsRange`，允许一个 pattern 一次消费多个元素；`curr.size()` 决定前进几步。
- `arith.addf` 走 `ElementwiseOpConversion<arith::AddFOp, LLVM::FAddOp>`；2 个元素/线程就是 2 条 `fadd float`，PTX 的向量 load 是后面 load/store lowering 做的。
- int 提升在 semantic 层完成，TTIR 里已经是显式 `extsi/extui/trunci`；lowering 只负责映射成 `sext/zext/trunc`。
- bf16 在 sm_75 上没有原生 cvt，PTX 是 `bfe/add/setp/selp/prmt` 软件序列；sm_80+ 才可能有 `cvt.rn.bf16.f32`（`guided`）。
- fp8 在 LLVM 层是 `i8`；e4m3 需要 sm_89+，e5m2 的软件路径在 sm_75 上仍会因 `cvt.f16x2.f32` 失败（**实测**）。
- fp4 是“一个 i8 装两个 nibble”，基类每 4 个 i8 调一次 `upcastPackedFp4`，返回 8 个 f16/bf16。
- 自定义 pattern 必须保证返回值数量和类型与 struct 字段一致；`packLLElements` 的类型检查是 fatal，不会静默 bitcast。

## 自我检核

- 你能解释为什么 `tensor<256xf32>` 在 `num_warps=8` 时只有一条 `fadd float`，而在 `num_warps=2` 时有四条吗？
- 你能画出 `unpackLLElements → createDestOps → packLLElements` 的数据流，并指出 `MultipleOperandsRange` 的索引含义吗？
- 你能说出 `arith.extsi`、`arith.truncf`、`tt.fp_to_fp` 三种 cast 分别由哪个 pattern 处理吗？
- 你能解释为什么 bf16 在 sm_75 上的 PTX 没有 `cvt`，以及 `setp.nan/selp` 在做什么吗？
- 你能说出 fp8 e4m3 和 e5m2 在 T4 上分别失败在哪一步吗？
- 你能说出 `packLLElements` 类型不匹配时会发生什么，以及正确的修复方式吗？

## 延伸阅读

1. **`include/triton/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVMBase.h:20-204`** — 读 `MultipleOperandsRange`、`maybeDeduplicate`、`matchAndRewrite`、`ElementwiseOpConversion`、`ElementwiseToIntrinsicOpConversion`。学 elementwise pattern 的通用骨架，以及“一次消费多个元素”的协议。与本章关系：本章的主线；先读 `matchAndRewrite`，再回头看 `MultipleOperandsRange` 的索引方式。
2. **`lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:64-779`** — 读 `CmpIOpConversion`、`MulhiUIOpConversion`、`ClampFOpConversion`、`MapElementwiseOpConversion`、`populateElementwiseOpToLLVMPatterns`。学通用模板不够用时专用 pattern 怎么写，以及 op 注册表的组织方式。与本章关系：把“pattern 可以参数化/打包/多指令”从概念变成具体代码。
3. **`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/ElementwiseOpToLLVM.cpp:296-498`** — 读 `convertFpTrunc`、`FpToFpOpConversion::getConversionFunc`、`createDestOps`、`makeNativeFp8Converter`。学 f16/bf16/fp8 的舍入模式、原生 vs 软件路径、packed 转换的选择逻辑。与本章关系：本章 bf16/fp8 结论的主要来源；建议和 `TypeConverter.h:31-36` 一起读。
4. **`lib/Conversion/TritonGPUToLLVM/Fp4ToFpOpToLLVMBase.cpp:11-37` 与 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/Fp4ToFpOpToLLVM.cpp:12-140`** — 读 4 个 i8 一组、`upcastPackedFp4` 返回 8 个值、软件 PTX 表、Blackwell 原生分支。学 fp4 的 packed 表示和“一个 pattern 一次处理 8 个逻辑元素”的实现。与本章关系：本章 fp4 小节的直接依据；T4 上只能 `guided`。
5. **`lib/Conversion/TritonGPUToLLVM/Utility.cpp:1080-1164`** — 读 `unpackLLElements`、`unpackUniqueTensorElements`、`packLLElements`、`packUniqueTensorElements`。学 struct ↔ 标量的拆装细节，以及类型检查为什么是 fatal。与本章关系：理解 `ElementwiseOpConversionBase` 的前提；写自定义 pattern 前必读。
6. **MLIR 官方文档 “LLVM IR Dialect” 与 “Dialect Conversion”** — 读 `llvm.mlir.undef`、`llvm.insertvalue`、`llvm.extractvalue`、`llvm.insertelement`、`llvm.extractelement` 的语义。学这些 op 在 LLVM dialect 里到底表示什么，为什么 struct 和 vector 是两种不同的东西。与本章关系：Triton 只是这些基础设施的用户；先懂 MLIR 的 struct/vector，再读 `packLLElements` 会快很多。

## 下一章

本章把 tensor 拆成了每线程的标量寄存器，但只处理了“寄存器里的计算”。下一章进入内存：`tt.load` / `tt.store` 如何根据 layout、对齐和 mask 选择向量宽度，`tt.atomic_rmw` 如何降成 `atom.global`，以及 `ld.global.v4.b32` 这种向量访存到底是在哪一步形成的。

→ [Ch 24 load/store、atomic 与内存 lowering](./24-load-store-atomic-lowering.md)
