# 练习 D — 自定义 op 的完整 lowering：从 TTIR 到 LLVM/PTX

> 目标：新增一个 TTIR op（饱和有符号加法 `tt.sadd_sat`），为它写 verifier、TypeConverter 兼容的 lowering pattern、lit 测试，并让它出现在最终 PTX 里。做完这个练习，你应该能独立回答“一个新 op 要改哪几层、为什么 elementwise lowering 长这样、怎么证明 lowering 没算错”。

> **环境**：Triton 3.9.0（commit `a972119e92a27b445cd160017ea461e56148590c`），源码 `/home/ubuntu/aicomp/triton`。需要能构建 `triton-opt`。T4（sm_75）只能验证 FMA 路径（Triton 3.9 对 sm_75 回退 FMA，不生成 MMAv2）；本练习的 lowering 与架构无关，PTX 部分在 T4 上可实测，如果构建没跑通则标 `guided`。

## 为什么做这个练习

读 lowering 代码时，你看到的是“一堆 pattern 注册”。真正写的时候，你要同时处理：

- **op 定义**：ODS 的 operands/results/traits/assemblyFormat，以及 verifier；
- **类型转换**：TTIR 的 `tensor<...>` 在 LLVM dialect 里不是一个 vector，而是“每线程若干个标量/vector 的结构体”；
- **逐元素语义**：一个 tensor op 在 lowering 时要展开成 per-thread 的多个标量 op；
- **pattern 注册**：`populate*` 函数、benefit、conversion target；
- **证据**：lit 怎么匹配 LLVM IR，PTX 怎么确认指令真的生成了。

这个练习选 **saturating signed add**，因为：

1. 语义清楚：溢出时钳到 `INT_MIN`/`INT_MAX`；
2. 不需要新硬件指令：用 `add` + `icmp` + `select` 就能实现；
3. 能覆盖 scalar 和 tensor 两条路径（`ElementwiseOpConversionBase` 统一处理）；
4. 能验证：`INT_MAX + 1` 必须得到 `INT_MAX`，而不是回绕。

## 任务规格

### 要新增的 op

```mlir
%r = tt.sadd_sat %a, %b : tensor<128xi32, #blocked>
```

语义：逐元素 `r[i] = saturate_signed(a[i] + b[i])`，支持 `i8/i16/i32/i64`，scalar 和 tensor 都可以。

### 验收标准

| 项 | 标准 |
|---|---|
| 解析/打印 | `triton-opt` 能 round-trip `tt.sadd_sat` |
| verifier | `f32`/`i1` 输入必须报错 |
| lowering | `--convert-triton-gpu-to-llvm` 后出现 `llvm.add`、`llvm.icmp slt`、`llvm.select` |
| 正确性 | `INT_MAX + 1 -> INT_MAX`，`INT_MIN + (-1) -> INT_MIN`，普通加法不变 |
| lit | 新增 `test/Conversion/sadd-sat-to-llvm.mlir`，FileCheck 通过 |
| PTX（加分） | 端到端 kernel 的 PTX 里能看到 `add.s32` + `setp`/`selp`（或等价） |

### 限制

- 不要用 `llvm.inline_asm` 绕过类型系统；这个练习要你走标准 lowering。
- 不要修改 `ElementwiseOpToLLVM.cpp` 的大表来“顺便”加 op；请写独立 pattern，理解 `ElementwiseOpConversionBase` 的契约。
- 不要假设 `tensor` 在 LLVM 里是一个 `vector`；先读 `TypeConverter.cpp` 和 `unpackLLElements`。

## 预备知识：TTIR tensor 在 LLVM 里长什么样

Triton 的 `tensor<128xi32, #blocked>` 不是 `[128 x i32]`。在 GPU 上，每个线程只持有其中一部分元素，所以 TypeConverter 把它转成“每线程元素数”的 struct：

```text
tensor<128xi32, #blocked>  (4 warps, sizePerThread=1)
  -> !llvm.struct<(i32, i32, i32, i32)>   // 每个线程 4 个元素（示例，具体取决于 layout）
```

**源码依据**：

- `lib/Conversion/TritonGPUToLLVM/TypeConverter.cpp:17-108`（`TritonGPUTypeConverter`）
- `include/triton/Conversion/TritonGPUToLLVM/Utility.h:749-770`（`unpackLLElements` / `packLLElements`）

因此，一个 elementwise op 的 lowering 必须：

1. 把每个 operand 的 LLVM struct 拆成 per-thread 标量列表；
2. 对每一对元素生成标量 LLVM op；
3. 把结果列表打包回 struct。

`ElementwiseOpConversionBase` 已经帮你做了 1 和 3，你只需要实现 `createDestOps` 里的第 2 步。

**源码依据**：

- `include/triton/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVMBase.h:43-60`（基类与 `createDestOps` 契约）
- `lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:151-168`（`MulhiUIOpConversion` 的最小实现）

## 分段实现

### Step 1 — 定义 op 与 verifier

在 `include/triton/Dialect/Triton/IR/TritonOps.td` 里，找一个整数 op（比如 `TT_MulhiUIOp`，约 220 行）旁边加：

```tablegen
def TT_SaddSatOp : TT_Op<"sadd_sat", [Elementwise,
                                      SameOperandsAndResultType,
                                      Pure]> {
  let summary = "Saturating signed integer addition";

  let description = [{
    Adds two signed integers and saturates the result to the representable
    range of the element type instead of wrapping around.
  }];

  let arguments = (ins TT_IntLike:$x, TT_IntLike:$y);
  let results = (outs TT_IntLike:$result);
  let assemblyFormat = "$x `,` $y attr-dict `:` type($x)";
  let hasVerifier = 1;
}
```

在 `lib/Dialect/Triton/IR/Ops.cpp` 里实现 verifier：

```cpp
LogicalResult SaddSatOp::verify() {
  Type ty = getX().getType();
  if (auto tensorTy = dyn_cast<RankedTensorType>(ty))
    ty = tensorTy.getElementType();
  if (!ty.isInteger(8) && !ty.isInteger(16) && !ty.isInteger(32) &&
      !ty.isInteger(64))
    return emitError() << "tt.sadd_sat expects i8/i16/i32/i64, got " << ty;
  return success();
}
```

三个设计点：

- `Elementwise` trait 让后续 pass/analysis 知道它是逐元素、无副作用的；
- `SameOperandsAndResultType` 保证 x/y/result 类型一致，省掉一个 verifier 分支；
- `TT_IntLike` 已经包含 scalar 和 tensor 的整数类型；如果你只想要 tensor，可以换成 `TT_Tensor`，但没必要。

**验证**：重新构建后，用 `triton-opt` 解析一段最小 IR：

```mlir
module {
  tt.func @f(%a: i32, %b: i32) -> i32 {
    %0 = tt.sadd_sat %a, %b : i32
    tt.return %0 : i32
  }
}
```

```bash
triton-opt test/Conversion/sadd-sat-roundtrip.mlir
```

如果报 “custom op 'tt.sadd_sat' is unknown”，说明 TableGen 没重新生成或 dialect 没注册。

### Step 2 — 写 lowering pattern

新建 `lib/Conversion/TritonGPUToLLVM/SaddSatOpToLLVM.cpp`：

```cpp
//===- SaddSatOpToLLVM.cpp - lower tt.sadd_sat to LLVM --------------------===//
#include "mlir/Conversion/LLVMCommon/Pattern.h"
#include "triton/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVMBase.h"
#include "triton/Conversion/TritonGPUToLLVM/Utility.h"
#include "triton/Dialect/Triton/IR/Dialect.h"

namespace {

using namespace mlir;
using namespace mlir::triton;

struct SaddSatOpConversion
    : public ElementwiseOpConversionBase<SaddSatOp, SaddSatOpConversion> {
  using Base = ElementwiseOpConversionBase<SaddSatOp, SaddSatOpConversion>;
  using Base::Base;
  using Adaptor = typename Base::OpAdaptor;

  SmallVector<Value> createDestOps(SaddSatOp op, Adaptor adaptor,
                                   ConversionPatternRewriter &rewriter,
                                   Type elemTy, MultipleOperandsRange operands,
                                   Location loc) const {
    auto b = TritonLLVMOpBuilder(loc, rewriter);
    unsigned bits = elemTy.getIntOrFloatBitWidth();
    int64_t minVal = APInt::getSignedMinValue(bits).getSExtValue();
    int64_t maxVal = APInt::getSignedMaxValue(bits).getSExtValue();

    SmallVector<Value> results;
    results.reserve(operands.size());
    for (auto &ops : operands) {
      Value lhs = ops[0];
      Value rhs = ops[1];

      Value sum = b.add(lhs, rhs);
      Value lhsNeg = b.icmp_slt(lhs, b.int_val(bits, 0));
      Value rhsNeg = b.icmp_slt(rhs, b.int_val(bits, 0));
      Value sumNeg = b.icmp_slt(sum, b.int_val(bits, 0));

      // 同号相加才可能溢出；溢出时结果符号与操作数相反。
      Value sameSign = b.icmp_eq(lhsNeg, rhsNeg);
      Value signOk = b.icmp_eq(lhsNeg, sumNeg);
      Value overflow = b.and_(sameSign, b.xor_(signOk, b.true_val()));

      Value saturated =
          b.select(lhsNeg, b.int_val(bits, minVal), b.int_val(bits, maxVal));
      results.push_back(b.select(overflow, saturated, sum));
    }
    return results;
  }
};

} // namespace

void mlir::triton::populateSaddSatOpToLLVMPattern(
    LLVMTypeConverter &typeConverter, ModuleAxisInfoAnalysis &axisInfoAnalysis,
    RewritePatternSet &patterns, PatternBenefit benefit) {
  patterns.add<SaddSatOpConversion>(typeConverter, axisInfoAnalysis, benefit);
}
```

逐行解释：

- `operands` 是 `MultipleOperandsRange`：外层是“每个元素”，内层是“这个元素的两个 operand 值”。所以 `ops[0]`/`ops[1]` 是标量 `i32`/`i64`。
- `b.int_val(bits, 0)` 生成与元素同宽的 0；不要用 `b.i32_val(0)`，否则 `i8`/`i64` 会类型不匹配。
- 溢出条件：`sameSign && !signOk`。用 `xor_(signOk, true_val())` 取反，避免依赖 `icmp_ne` 的具体 lowering。
- 饱和值：`lhsNeg` 为真说明是负溢出，钳到 `INT_MIN`；否则钳到 `INT_MAX`。
- `b.select(overflow, saturated, sum)` 的顺序是 `(cond, trueValue, falseValue)`。

**源码依据**：

- `include/triton/Conversion/TritonGPUToLLVM/Utility.h:249-311`（`icmp_*`、`select`、`int_val`）
- `lib/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVM.cpp:151-168`（`createDestOps` 的返回约定）
- `include/triton/Conversion/TritonGPUToLLVM/ElementwiseOpToLLVMBase.h:43-60`（基类构造参数）

### Step 3 — 注册 pattern 与构建

**(a) 声明 populate 函数**。在 `include/triton/Conversion/TritonGPUToLLVM/PatternTritonGPUOpToLLVM.h` 里加：

```cpp
void populateSaddSatOpToLLVMPattern(LLVMTypeConverter &typeConverter,
                                    ModuleAxisInfoAnalysis &axisInfoAnalysis,
                                    RewritePatternSet &patterns,
                                    PatternBenefit benefit);
```

**(b) 调用它**。在 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp` 的 `populateConversionPatterns` 里，紧挨着 `populateElementwiseOpToLLVMPatterns` 加：

```cpp
  mlir::triton::populateSaddSatOpToLLVMPattern(
      typeConverter, axisInfoAnalysis, patterns, benefit);
```

**(c) 加进 CMake**。在 `lib/Conversion/TritonGPUToLLVM/CMakeLists.txt` 的源文件列表里加：

```cmake
  SaddSatOpToLLVM.cpp
```

**(d) 重新构建**：

```bash
cmake --build python/build/cmake.linux-x86_64-cpython-312 --target triton-opt -j"$(nproc)"
```

> **guided**：构建目录名按你的环境调整；本机当前构建尚未完成，这条命令的产物没有实测。

**为什么不用 `ElementwiseOpToLLVM.cpp` 的大表？** 那张表主要服务 `arith`/`math` op，按 `ElementwiseOpConversionBase` 的模板批量注册；新 op 写独立文件更容易读、也更容易在上游 review 时解释。代价是你要自己声明 `populate*` 并在 NVIDIA/AMD 两个后端各调用一次（本练习只做 NVIDIA）。

### Step 4 — 写 lit 测试

新建 `test/Conversion/sadd-sat-to-llvm.mlir`：

```mlir
// RUN: triton-opt %s -split-input-file --convert-triton-gpu-to-llvm | FileCheck %s

#blocked = #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>

module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32} {
  // CHECK-LABEL: @sadd_sat_tensor
  // CHECK: llvm.add
  // CHECK: llvm.icmp slt
  // CHECK: llvm.select
  tt.func @sadd_sat_tensor(%a: tensor<128xi32, #blocked>,
                           %b: tensor<128xi32, #blocked>)
      -> tensor<128xi32, #blocked> {
    %0 = tt.sadd_sat %a, %b : tensor<128xi32, #blocked>
    tt.return %0 : tensor<128xi32, #blocked>
  }
}

// -----

module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32} {
  // CHECK-LABEL: @sadd_sat_scalar
  // CHECK: llvm.add
  // CHECK: llvm.select
  tt.func @sadd_sat_scalar(%a: i64, %b: i64) -> i64 {
    %0 = tt.sadd_sat %a, %b : i64
    tt.return %0 : i64
  }
}
```

运行：

```bash
lit -v test/Conversion/sadd-sat-to-llvm.mlir
```

**注意**：`--convert-triton-gpu-to-llvm` 的完整 pipeline 可能还需要 `--allocate-shared-memory-nv` 等前置 pass；如果直接跑报 “failed to legalize”，先用 `triton-opt --help | grep convert-triton-gpu` 看该版本的实际 flag，再参考 `test/Conversion/tritongpu_to_llvm.mlir` 的 RUN 行。

### Step 5 — 验证语义（三种方式，按可得性选）

**(a) 手算 + FileCheck**：在 lit 里加注释，列出边界值：

```text
INT32_MAX + 1      -> INT32_MAX
INT32_MIN + (-1)   -> INT32_MIN
INT32_MAX + (-1)   -> INT32_MAX - 1
0 + 0              -> 0
```

**(b) 端到端 kernel（加分）**：如果愿意补前端，在 `python/triton/language/standard.py` 加：

```python
@core.extern
def saturating_add(x, y, _builder=None):
    ...
```

并在 `semantic.py` 里发射 `tt.sadd_sat`。然后用 pytest 对比 `torch.clamp(x + y, min, max)`（注意 torch 的整数加法会回绕，必须用 `int64` 计算再 clamp）。

**(c) 直接构造 TTIR**：用 `triton.compile(IRSource(ttir_text), target=...)` 编译一段手写 TTIR，再 launch。这条路径能绕过前端，但需要你熟悉 `IRSource` 和 `CompiledKernel` 的调用方式，属于延伸挑战。

## 测试用例

| 用例 | 输入 | 期望 |
|---|---|---|
| 正溢出 | `INT32_MAX, 1` | `INT32_MAX` |
| 负溢出 | `INT32_MIN, -1` | `INT32_MIN` |
| 普通 | `7, 3` | `10` |
| 异号 | `INT32_MAX, -1` | `INT32_MAX - 1` |
| 零 | `0, 0` | `0` |
| i8 边界 | `127, 1` | `127` |
| i64 边界 | `INT64_MAX, 1` | `INT64_MAX` |

非法输入：

| 输入 | 期望 |
|---|---|
| `f32` | verifier 报错 |
| `i1` | verifier 报错 |
| `x: i32, y: i64` | ODS 类型约束报错 |

## 参考解答

<details>
<summary>完整文件（点击展开）</summary>

### `TritonOps.td`（追加）

```tablegen
def TT_SaddSatOp : TT_Op<"sadd_sat", [Elementwise,
                                      SameOperandsAndResultType,
                                      Pure]> {
  let summary = "Saturating signed integer addition";

  let arguments = (ins TT_IntLike:$x, TT_IntLike:$y);
  let results = (outs TT_IntLike:$result);
  let assemblyFormat = "$x `,` $y attr-dict `:` type($x)";
  let hasVerifier = 1;
}
```

### `lib/Dialect/Triton/IR/Ops.cpp`（追加）

```cpp
LogicalResult SaddSatOp::verify() {
  Type ty = getX().getType();
  if (auto tensorTy = dyn_cast<RankedTensorType>(ty))
    ty = tensorTy.getElementType();
  if (!ty.isInteger(8) && !ty.isInteger(16) && !ty.isInteger(32) &&
      !ty.isInteger(64))
    return emitError() << "tt.sadd_sat expects i8/i16/i32/i64, got " << ty;
  return success();
}
```

### `lib/Conversion/TritonGPUToLLVM/SaddSatOpToLLVM.cpp`

见 Step 2 的完整代码。

### `PatternTritonGPUOpToLLVM.h`（追加声明）

```cpp
void populateSaddSatOpToLLVMPattern(LLVMTypeConverter &typeConverter,
                                    ModuleAxisInfoAnalysis &axisInfoAnalysis,
                                    RewritePatternSet &patterns,
                                    PatternBenefit benefit);
```

### `TritonGPUToLLVM.cpp`（NVIDIA，追加调用）

```cpp
  mlir::triton::populateSaddSatOpToLLVMPattern(
      typeConverter, axisInfoAnalysis, patterns, benefit);
```

### `lib/Conversion/TritonGPUToLLVM/CMakeLists.txt`（追加）

```cmake
  SaddSatOpToLLVM.cpp
```

### `test/Conversion/sadd-sat-to-llvm.mlir`

见 Step 4。

</details>

## 常见卡点

1. **`unknown op 'tt.sadd_sat'`**：ODS 改了但没重新生成；或者 `TT_IntLike` 的约束不匹配（比如传了 `tensor<128xf32>`）。
2. **`failed to legalize operation 'tt.sadd_sat'`**：pattern 没注册到 `populateConversionPatterns`，或者 pattern 的 `matchAndRewrite` 返回了 `failure()`。先在 `populateConversionPatterns` 里下断点/打日志确认 pattern 被加进去。
3. **`operands` 为空**：`ElementwiseOpConversionBase` 只对 elementwise op 生效；确认 op 有 `Elementwise` trait，且 result 是 tensor/scalar 而不是 pointer。
4. **`int_val` 类型不匹配**：`b.int_val(bits, 0)` 的 `bits` 必须是元素位宽；`b.i32_val(0)` 只适合 i32。
5. **PTX 里没有 `selp`**：`select` 可能被 LLVM 优化成 `min/max` 或分支；先看 LLVM IR 的 `llvm.select` 是否在，再看 PTX。不要在 LLVM 优化前就断言“lowering 错了”。
6. **i64 在 T4 上性能很差**：这是硬件/PTX 的问题，不是 lowering 错；`add.s64` + `setp` 在 sm_75 上本来就慢。

## 延伸挑战

1. **加 `tt.uadd_sat`**：无符号饱和加法，溢出条件是 `sum < lhs`（无符号比较）。练习 `icmp_ult`。
2. **用 `llvm.intr.sadd.sat`**：如果你的 LLVM 版本提供 `LLVM::SAddSatOp`，把 lowering 换成一条 intrinsic，比较生成的 PTX 和指令数。
3. **加 AMD 后端**：在 `third_party/amd/lib/TritonAMDGPUToLLVM/` 里调用同一个 `populate*`，确认 RDNA/CDNA 的 `v_add_sat` 是否可用（`guided`）。
4. **写一个 canonicalizer**：`sadd_sat(x, 0) -> x`，以及 `sadd_sat(sadd_sat(x, y), z)` 的结合律优化（注意饱和不满足结合律，不能随便合并）。
5. **补前端**：`tl.saturating_add` + semantic + pytest，走完 Ch 5/Ch 6 的整条链。

## 检核清单

- [ ] `tt.sadd_sat` 能被 `triton-opt` 解析和打印
- [ ] `f32`/`i1` 输入 verifier 报错
- [ ] `--convert-triton-gpu-to-llvm` 后出现 `llvm.add` + `llvm.icmp` + `llvm.select`
- [ ] scalar 和 tensor 两条路径都覆盖
- [ ] lit 通过
- [ ] 能解释 `unpackLLElements`/`packLLElements` 在 lowering 中的位置
- [ ] 能解释为什么溢出条件是 `sameSign && !signOk`
- [ ] 能说出 pattern 注册的三个落点（头文件声明、NVIDIA 调用、CMake）

## 下一章

回到 [Ch 23](./23-elementwise-and-type-lowering.md) 看 elementwise lowering 的全貌；然后去 [Ch 25](./25-dot-mma-lowering.md) 看 dot 这种非 elementwise op 如何 lowering。
