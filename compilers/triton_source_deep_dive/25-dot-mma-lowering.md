# Ch 25 — dot/MMA lowering

> `tl.dot` 不是一条指令，而是一次“形状、类型、layout、硬件代际”共同参与的指令选择。

## 目标

读完本章，你应该能回答：

- `tt.dot` 在什么条件下保持 blocked layout 走 FMA，什么时候变成 MMAv2、MMAv3(WGMMA)、MMAv5(tcgen05)？
- 为什么 Tesla T4（sm_75）在 Triton 3.9.0 的默认 pipeline 里**不会**走 MMAv2，而是退回 FMA？
- `m16n8k8`、`m16n8k16`、`m64nNk16` 这些指令形状和 Triton 的 `instrShape = [16, 8]` 是什么关系？
- `dot_op` layout 的 `kWidth` 是什么？为什么 A/B 需要它，accumulator 不需要？
- K 维如何被切成多个 MMA？accumulator 在寄存器里如何分布？
- Hopper 的 WGMMA 为什么要 shared memory descriptor，Blackwell 的 tcgen05 为什么要 TMEM？**guided**

本章的主线是 `tt.dot` 从 TTGIR 到 LLVM/PTX 的选择链。重点不是记住某个 PTX 指令，而是理解“为什么这个形状、这个类型、这个架构会选这条路径”。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 3.9.0。
- GPU：Tesla T4，`sm_75`，驱动 535.216.01，CUDA 12.4。
- 仓库源码未在本机构建；本章的 Triton IR/PTX/SASS **实测**来自 `/tmp` 独立 venv 的 Triton **3.8.0** wheel。
- `cuda:80` 的产物是**编译期实测**：用 3.8.0 生成 PTX/cubin，ptxas 接受，但没有 sm80 硬件可执行。
- 标 **源码依据** 的结论来自 3.9.0 仓库，行号用 `nl -ba` 核对。
- Hopper/Blackwell、TMA、tcgen05、AMD MFMA 均标 **guided**：T4 不支持，本章只做源码级解释。

**版本导航（源码依据）**：3.9.0 中 `tt.dot` 的 NVIDIA 分发入口是
`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM.cpp`（221 行）；
基类目录下只剩 `DotOpToLLVM/FMA.cpp`（57 行）和 `DotOpToLLVM/FMADotUtility.cpp`（170 行）。
MMAv2、WGMMA、MMAv5 分别在
`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/{MMAv2.cpp,WGMMA.cpp,MMAv5.cpp}`。

## 为什么需要这个

`tl.dot(a, b, acc)` 在 Python 层只有一行，但它背后有四种完全不同的执行模型：

| 路径 | 执行单元 | 操作数位置 | 累加器位置 | 典型架构 |
|---|---|---|---|---|
| FMA | 每个线程的标量/向量 ALU | 寄存器 | 寄存器 | 所有架构 |
| MMAv2 | 一个 warp | 寄存器 | 寄存器 | Turing/Ampere |
| MMAv3/WGMMA | 一个 warpgroup（128 线程） | shared memory descriptor + 可选寄存器 A | 寄存器 | Hopper |
| MMAv5/tcgen05 | 一个 CTA（单 warp 发射） | shared memory / TMEM | TMEM | Blackwell |

选错路径不会报错，只会慢，或者在你的架构上直接 fallback。更麻烦的是，**同一个 `tl.dot` 在不同架构上会生成完全不同的 layout**：T4 上是 `#blocked`，A100 上是 `#nvidia_mma`，H100 上是 `#nvidia_mma versionMajor=3`。如果不理解选择链，你会在 TTGIR 里看到 `#blocked` 就以为“编译器没优化”，其实这是 3.9.0 对 sm<80 的明确策略。

## 先建立直觉

先看一个 32×32×32 的 fp16 dot：

```python
@triton.jit
def dotk(a_ptr, b_ptr, c_ptr, M: tl.constexpr, N: tl.constexpr, K: tl.constexpr):
    rm = tl.arange(0, M)
    rn = tl.arange(0, N)
    rk = tl.arange(0, K)
    a = tl.load(a_ptr + rm[:, None] * K + rk[None, :])
    b = tl.load(b_ptr + rk[:, None] * N + rn[None, :])
    c = tl.dot(a, b)
    tl.store(c_ptr + rm[:, None] * N + rn[None, :], c)
```

在 T4（`cuda:75`）上，TTGIR 的结果编码是：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [1, 32],
                          warpsPerCTA = [4, 1], order = [1, 0]}>
%c = tt.dot %a, %b, %cst : tensor<32x32xf32, #ttg.dot_op<{opIdx = 0, parent = #blocked}>> *
                            tensor<32x32xf32, #ttg.dot_op<{opIdx = 1, parent = #blocked}>> ->
                            tensor<32x32xf32, #blocked>
```

PTX 里没有 `mma.sync`，只有 256 条 `fma.rn.f32`；SASS 里是 256 条 `FFMA`。**实测（Triton 3.8.0，sm_75）**。

在 `cuda:80` 上，同一份 kernel 的 TTGIR 变成：

```mlir
#mma = #ttg.nvidia_mma<{versionMajor = 2, versionMinor = 0,
                        warpsPerCTA = [2, 2], instrShape = [16, 8]}>
%c = tt.dot %a, %b, %cst : tensor<32x32xf16, #ttg.dot_op<{opIdx = 0, parent = #mma, kWidth = 2}>> *
                            tensor<32x32xf16, #ttg.dot_op<{opIdx = 1, parent = #mma, kWidth = 2}>> ->
                            tensor<32x32xf32, #mma>
```

PTX 里是 4 条：

```ptx
mma.sync.aligned.m16n8k16.row.col.f32.f16.f16.f32
  { %r25, %r26, %r27, %r28 }, { %r17, %r18, %r19, %r20 },
  { %r21, %r22 }, { %r25, %r26, %r27, %r28 };
```

SASS 是 4 条 `HMMA.16816.F32`。**实测（Triton 3.8.0 编译期，cuda:80，未在 sm80 执行）**。

这两个结果说明：`tl.dot` 的形态不是“由 Python 代码决定”，而是“由 `AccelerateMatmul` 的版本选择和 `DotOpConversion` 的分发决定”。

## 核心概念

### 1. `tt.dot` 的契约

**源码依据**：`include/triton/Dialect/Triton/IR/TritonOps.td` 中 `TT_DotOp` 的定义（搜索 `def TT_DotOp`）。核心约束：

- A 的形状是 `[..., M, K]`，B 是 `[..., K, N]`，D 是 `[..., M, N]`；
- A/B 的元素类型可以不同，但必须能落到同一种 MMA 类型；
- accumulator `C` 的类型决定输出类型；
- `inputPrecision` 影响 fp32 输入是否用 TF32。

`tt.dot` 的结果编码在 `AccelerateMatmul` 之前通常是 `#blocked`，之后可能变成 `#nvidia_mma`、`#nvidia_mma versionMajor=3`，或者被 `TCGen5MMAOp` 取代。

### 2. 三种 layout

| layout | 作用 | 关键参数 |
|---|---|---|
| `#blocked` | 普通分布式布局，FMA 路径使用 | `sizePerThread`、`threadsPerWarp`、`warpsPerCTA`、`order` |
| `#nvidia_mma` | tensor core 输出的分布式布局 | `versionMajor`、`versionMinor`、`warpsPerCTA`、`instrShape` |
| `#ttg.dot_op` | A/B 操作数的 MMA 专用布局 | `opIdx`、`parent`、`kWidth` |

**源码依据**：

- `include/triton/Dialect/TritonGPU/IR/TritonGPUAttrDefs.td:1287-1306`：`NvidiaMmaEncodingAttr`，version 2 的隐式 warp tile 是 `[16, 8]`。
- 同文件 `:1413-1433`：`DotOperandEncodingAttr`，A 的 `opIdx=0`，B 的 `opIdx=1`，`kWidth` 是每个线程沿 K 连续存储的元素数。
- `lib/Dialect/TritonGPU/IR/Dialect.cpp:2884-2890`：`isTuring()` 是 `versionMajor==2 && versionMinor==1`；`isAmpere()` 是 `versionMajor==2`；`isHopper()` 是 `versionMajor==3`。

注意：`isTuring()` 和 `isAmpere()` 都返回 true 对于 version 2 的 Turing 布局，但 `versionMinor` 不同。Turing 的 `instrShape` 仍然是 `[16, 8]`，真正的指令是 `m16n8k8`；Ampere 是 `m16n8k16`。**源码依据**：`MMAv2.cpp:340-349` 的 Turing map 和 `:618-619` 的 K 计算。

### 3. `instrShape` 与实际 PTX 形状

`AccelerateMatmul` 里：

```cpp
auto instrShape = mmaVersionToInstrShape(versionMajor, retShapePerCTA,
                                         oldAType.getElementType(), numWarps);
```

**源码依据**：`lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp:471-478`。

`mmaVersionToInstrShape` 对 version 2 返回：

```cpp
ret[rank - 1] = 8;
ret[rank - 2] = eltType.isF64() ? 8 : 16;
```

**源码依据**：`lib/Dialect/TritonGPU/Transforms/Utility.cpp:33-43`。

所以 TTGIR 里的 `instrShape = [16, 8]` 只描述 M/N，不描述 K。K 由元素位宽决定：

```cpp
std::array<int64_t, 3> shape = {aElemTy.isF64() ? 8 : 16, 8,
                                256 / aElemTy.getIntOrFloatBitWidth()};
```

**源码依据**：`MMAv2.cpp:618-619`。

| 输入类型 | M | N | K | PTX |
|---|---:|---:|---:|---|
| fp16/bf16 | 16 | 8 | 16 | `mma.sync...m16n8k16`（Ampere） |
| fp16/bf16（Turing） | 16 | 8 | 8 | `mma.sync...m16n8k8`（Turing map） |
| tf32 | 16 | 8 | 8 | `mma.sync...m16n8k8` |
| fp64 | 8 | 8 | 4 | `mma.sync...m8n8k4` |
| int8 | 16 | 8 | 32 | `mma.sync...m16n8k32` |

这张表是“指令形状”，不是“Triton tile 形状”。一个 16×16×16 的 `tl.dot` 可能需要多条指令，具体数量取决于 warp 划分和 K。

### 4. FMA 路径

**源码依据**：

- `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM.cpp:86-87`：结果编码是 `BlockedEncodingAttr` 时调用 `convertFMADot`。
- `lib/Conversion/TritonGPUToLLVM/DotOpToLLVM/FMA.cpp:50-57`：`convertFMADot` 创建 `GenericFMAVectorMultiplier`。
- 同文件 `:18-45`：浮点用 `LLVM::FMulAddOp`，整数用 `mul + add`。
- `lib/Conversion/TritonGPUToLLVM/DotOpToLLVM/FMADotUtility.cpp:78-168`：按 `repetitions` 和 `sizePerThread` 展开三重循环，对每个输出元素做 K 长度的点积。

FMA 的优点是形状自由、不要 `dot_op` layout、不要 shared memory；缺点是每个输出元素都要 K 次乘加，完全没有 tensor core。T4 上的 32×32×32 dot 生成 256 条 `fma.rn.f32`，就是这个原因。

### 5. MMAv2 路径

**源码依据**：

- `DotOpToLLVM.cpp:74-84`：`NvidiaMmaEncodingAttr` 且 `versionMajor==2` 时调用 `convertMMA`。
- `MMAv2.cpp:485-590`：`convertMMAImpl` 的主体。
- `MMAv2.cpp:593-669`：`convertMMAWithType` 选择指令、寄存器数量和 PTX 类型。
- `MMAv2.cpp:566-571`：batch/K/M/N 四层循环。

MMAv2 的核心步骤：

1. **确认 A/B 是 `dot_op`**（`MMAv2.cpp:495-497`）。
2. **算 repetition**（`:507-525`）：`repA = getRepForOperand(...)`，得到 `repM/repN/repK`。
3. **把 per-thread 元素映射成 MMA 寄存器**（`:532-541`）：`getValuesFromDotOperandLayoutStruct` 按 `kWidth` 和 layout 重排。
4. **准备 accumulator**（`:499-500`）：`loadC` 把 C 打包成每线程的 f32 寄存器。
5. **三层循环发 MMA**（`:566-571`）：`for b, k, m, n`。K 在最外层，每次 `k` 把上一次的 `fc` 作为 C 传入，形成累加链。
6. **把结果写回 `fc`**（`:557-563`）：每个 MMA 返回 4 个 f32（`numMmaRets=4`），按 `colsPerThread` 放到对应位置。
7. **打包成张量**（`:576-588`）。

`getRepForOperand` 的 tile 大小：

**源码依据**：`lib/Dialect/TritonGPU/IR/Dialect.cpp:2901-2940`。

- A：`tileSize = [16, tileBitWidthK / bitwidth]`；
- B：`tileSize = [tileBitWidthK / bitwidth, 8]`；
- `tileBitWidthK = bitwidth == 64 ? 256 : 256`（代码写的是 `bitwidth == 64 ? (1 * 256) : (4 * 64)`，两者都是 256）；
- `numRep = shape / (tileSize * warpsPerCTA)`，至少 1。

对 fp16：`tileSizeK = 256/16 = 16`，所以 A tile 是 16×16，B tile 是 16×8。对 tf32：`tileSizeK = 256/32 = 8`，A tile 是 16×8，B tile 是 8×8。

### 6. accumulator 在寄存器里的分布

一个 `m16n8k16` f32 MMA 的 D 是 16×8，32 个线程各持有 4 个 f32（`numMmaRets = 32/8 = 4`）。**源码依据**：`MMAv2.cpp:543-545`。

`loadC` 对 f32 直接返回每线程 4 个值；对 f16 会把 2 个 f16 打包进 32 位（`MMAv2.cpp:37-54`）。`callMma` 里：

```cpp
fc[(numRegisters.m * m * colsPerThread +
    numMmaRets * numCPackedElem * numRegisters.n * n) / numCPackedElem +
   i + batchOffset * b] = tb.extract_val(elemTy, mmaOut, i);
```

**源码依据**：`MMAv2.cpp:557-563`。

这不是“一个线程持有整个 16×8 tile”，而是“每个线程持有该 tile 的 4 个分散元素”。理解这一点，才能理解为什么 `convert_layout` 从 `#mma` 回到 `#blocked` 需要 shared memory 或 shuffle。

### 7. WGMMA（Hopper，guided）

**源码依据**：

- `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/WGMMA.cpp:185-373`：`convertDot`。
- `:226-247`：`DotOpMmaSmemLoader::build` 为 A/B 构造 shared memory descriptor，失败就报 “failed to find valid wgmma layout”。
- `:209-221`：`M = 4 * instrMNK[0]`，`N = instrMNK[1]`，`K = instrMNK[2]`，`numRepM/N/K` 按 CTA shape 计算。
- `:261`：`wgmma.fence`。
- `:338-340`：创建 `WGMMAOp`。
- `:360-363`：`wgmma.commit_group` / `wgmma.wait_group`（通过 `WgmmaGroupSyncAlignedOp` 和 `emitWait`）。

WGMMA 和 MMAv2 最大的区别不是指令名，而是**操作数来源**：MMAv2 从寄存器取 A/B，WGMMA 从 shared memory descriptor 取 B（A 可以在寄存器或 shared）。descriptor 里编码了 base address、leading dimension、stride、swizzle。**guided**：T4 没有 WGMMA，无法执行验证。

### 8. MMAv5/tcgen05（Blackwell，guided）

**源码依据**：

- `MMAv5.cpp:453-485`：`DotConversion` 描述 `mmaSizeM/N/K`、`repShape`、`transA/transB`。
- `:487-560`：`convertDotImpl`，包括 TMEM loader、`elect` 单线程发射、2CTA 谓词、M/N/K 计算。
- `:941-958`：`TCGen5MMAOpConversion`。
- `:987-1015`：`TCGen5CommitOpConversion`。

MMAv5 的累加器在 TMEM（Tensor Memory），不是寄存器；MMA 只由一个 warp 发射，其他线程等待 barrier。**guided**：sm100+ 才能验证。

## 底层机制

### 1. 版本选择：`getMMAVersionSafe`

**源码依据**：`AccelerateMatmul.cpp:45-87`。

```cpp
if (computeCapability < 75) {
  versionsSupported = {1};
} else if (computeCapability < 90) {
  versionsSupported = {2};
} else if (computeCapability < 100) {
  versionsSupported = {3, 2};
} else if (computeCapability < 120) {
  versionsSupported = {5, 2};   // sm120 例外，可能只 {2}
} else if (computeCapability < 130) {
  versionsSupported = {2};
}
```

然后按顺序调用 `supportMMA(op, version)`。注意这只是“候选列表”，真正创建编码的是 `BlockedToMMA`，而它在 sm<80 时直接返回 failure。

### 2. 为什么 T4 不走 MMAv2

**源码依据**：`AccelerateMatmul.cpp:514-521`。

```cpp
if (computeCapability < 70)
  return failure();
if (computeCapability < 80) {
  dotOp.emitRemark()
      << "Dot op using MMA for compute capability " << computeCapability
      << " has been deprecated. It falls back to the FMA path.";
  return failure();
}
```

这段代码是 3.9.0 的关键事实：**Turing 的 MMAv2 路径仍然存在于 `MMAv2.cpp`，但默认 pipeline 不会为 sm75 创建 `#nvidia_mma` 编码**。因此 `DotOpConversion` 看到的是 `#blocked`，走 `convertFMADot`。

所以“T4 只能用 MMAv2”这句话要分两层说：

- 硬件层：T4 是 Turing，支持 `mma.sync.m16n8k8`；
- 当前编译路径：3.9.0 的 `BlockedToMMA` 对 sm<80 明确 fallback 到 FMA。

**实测（Triton 3.8.0，sm_75）**：32×32×32 fp16 dot 生成 256 条 `fma.rn.f32`，0 条 `mma.sync`；SASS 是 256 条 `FFMA`，0 条 `HMMA`。这与源码的 fallback 一致。

### 3. `supportMMA` 的约束

**源码依据**：`lib/Analysis/Utility.cpp:1015-1093`、`:1101-1115`。

- fp32 + fp32 且 `inputPrecision != TF32` → 不支持 MMA（`:1021-1023`）。
- MMAv5：`numWarps` 必须是 4 或 8；rank 必须 2；K ≥ 256/bitwidth；M%64==0、N%8==0；不支持 fp64（`:1024-1057`）。
- MMAv3：`numWarps % 4 == 0`；M%64==0、N%8==0；rank != 3；A 类型必须是 fp8/int8/f16/bf16/f32；fp8 累加 fp32 且 `maxNumImpreciseAcc < 32` 时不支持（`:1059-1086`）。
- 通用：fp8 可以提升到 fp16，所以 `supportMMA(Value)` 对 fp8 返回 true（`:1109-1115`）。

这些约束解释了为什么“看起来能 dot”的 kernel 最后走了 FMA：K 太小、M/N 不满足 64/8 对齐、`numWarps` 不是 4 的倍数、或者 fp32 没用 TF32。

### 4. FMA lowering 的循环

**源码依据**：`FMADotUtility.cpp:92-168`。

```cpp
for (unsigned bRep = 0; bRep < repetitions[0]; ++bRep)
  for (unsigned mRep = 0; mRep < repetitions[1]; ++mRep)
    for (unsigned nRep = 0; nRep < repetitions[2]; ++nRep)
      for (unsigned b = 0; b < sizePerThread[0]; ++b)
        for (unsigned m = 0; m < sizePerThread[1]; ++m)
          for (unsigned n = 0; n < sizePerThread[2]; ++n) {
            ...
            for (unsigned k = 0; k < K; ++k)
              aOpVector.push_back(has.at({bRep, mRep, b, m, k}));
              bOpVector.push_back(hbs.at({bRep, nRep, b, n, k}));
            acc[linearAccumIdx] = multiplier.multiplyVectors(aOpVector, bOpVector, acc[linearAccumIdx]);
          }
```

`multiplyVectors` 对浮点生成 `LLVM::FMulAddOp`（`FMA.cpp:34-37`），对整数生成 `mul + add`（`:38-42`）。LLVM 后端再把 `llvm.fmuladd.f32` 落到 `fma.rn.f32`。

### 5. MMAv2 的指令选择

**源码依据**：`MMAv2.cpp:593-669`。

- `getMmaTypeDot` 决定 `TensorCoreType`（fp16/bf16/tf32/fp8/int8/fp64）。
- `ptxTypes` 由 `getMmaPtxType` 给出（`:351-367`）。
- Turing 用 `mmaInstrPtxTuring.at(mmaType)`（`:606-607`），Ampere 用 `NVVM::MmaOp`（`:656-665`）。
- `shape = {16, 8, 256/bitwidth}`（`:618-619`）。
- `NumRegisters{2, 1, 2}`（非 fp64，`:620-622`）。
- `loadC` 准备 C，`getMmaOperands` 准备 A/B/C 的寄存器列表，`NVVM::MmaOp` 生成 `llvm.nvvm.mma` intrinsic，最终落到 `mma.sync`。

Turing 的 `mmaInstrPtxTuring`：

```cpp
{FP32_FP16_FP16_FP32, "mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32"},
{INT32_INT8_INT8_INT32, "mma.sync.aligned.m8n8k16.row.col.satfinite.s32.s8.s8.s32"},
{FP16_FP16_FP16_FP16, "mma.sync.aligned.m16n8k8.row.col.f16.f16.f16.f16"}
```

**源码依据**：`MMAv2.cpp:340-349`。

**实测（ptxas，sm_75）**：手写一条 `mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32` 可以通过 ptxas，SASS 为 `HMMA.1688.F32`。这证明 Turing 指令存在，但**不证明** 3.9.0 的默认 pipeline 会生成它。

### 6. WGMMA 的 descriptor 与异步

**源码依据**：`WGMMA.cpp:226-247`。

```cpp
auto loader = DotOpMmaSmemLoader::build(loc, rewriter, cast<MemDescType>(aTensorTy),
                                        baseA, {M, K}, 0, 3, false, dTensorTy);
...
auto bLoader = DotOpMmaSmemLoader::build(loc, rewriter, bTensorTy, baseB,
                                         {K, N}, 1, 3, false, dTensorTy);
```

`build` 的参数里有 opIdx、transpose 允许、以及 `dTensorTy`。它返回的 loader 带 `getDescriptor().transposed`，决定 `layoutA/layoutB` 是 row 还是 col（`:256-259`）。

WGMMA 是异步的：`wgmma.fence` 保证操作数可见，`wgmma.commit_group` 提交一组，`wgmma.wait_group` 等待。`convertDot` 在 `:261` 创建 fence，在 `:360` 创建 group sync，在 `:362-363` 按 `sync` 参数决定是否 wait。

### 7. MMAv5 的单线程发射

**源码依据**：`MMAv5.cpp:497-520`。

```cpp
Value warpId = WarpIdOp::create(rewriter, loc);
Value isWarp0 = tb.icmp_eq(warpId, tb.i32_val(0));
...
Value elect = LLVM::NVIDIA::createElectPredicate(loc, rewriter);
```

只有 warp 0 的一个 elected lane 发射 tcgen05 MMA；其他线程不执行 MMA，但要在 barrier 上等待。`twoCTAs` 时还要加 `createLeadCTAPredicate`（`:504-507`）。这是 Blackwell 的编程模型，和 MMAv2 的“每个 warp 独立发 MMA”完全不同。

## 进一步用法/重要变体

### 变体 1：`input_precision` / TF32

**源码依据**：`lib/Analysis/Utility.cpp:1021-1023`、`:1088-1091`。

- fp32 输入 + `input_precision != "tf32"` → `supportMMA` 直接 false，走 FMA。
- fp32 输入 + `input_precision == "tf32"` → 允许 MMAv2+，但 T4 没有 TF32 tensor core，仍会 fallback。
- fp16/bf16 输入不受这条限制。

**实测（Triton 3.8.0，sm_75）**：fp16 输入的 TTGIR 里仍然写着 `inputPrecision = tf32`，但 FMA 路径实际用 fp32 乘加。不要看到 `tf32` 就以为 T4 在执行 TF32。

### 变体 2：`max_num_imprecise_acc`

**源码依据**：`WGMMA.cpp:332-351`。

WGMMA 的 fp8 累加如果超过 `maxNumImpreciseAcc`，会分配一个 partial accumulator，定期用 `faddAccumulate` 合并到高精度累加器。**guided**：Hopper/Blackwell 才有意义。

### 变体 3：batched dot

`tt.dot` 支持 rank-3 的 batch 维。MMAv2 的 `repBatch` 在 `MMAv2.cpp:525`，循环在 `:566`。MMAv3 明确拒绝 rank 3（`Utility.cpp:1072-1073`），会 fallback 到 MMAv2。**源码依据**：`supportMMA` 的 v3 分支。

### 变体 4：`tl.dot_scaled`

**源码依据**：`DotOpToLLVM.cpp:35-53` 的 `ScaledDotOpConversion`；`MMAv2.cpp:258-288` 的 `getMmaTypeDotScaled`。

`tl.dot_scaled` 用于 FP8/FP4 的块缩放。它要求结果 layout 是 permutation-matrix linear layout（`DotOpToLLVM.cpp:47-51`）。T4 没有 FP8 tensor core，**guided**。

### 变体 5：`DISABLE_MMA_V3` / `DISABLE_MMA_V5`

**源码依据**：`lib/Analysis/Utility.cpp:1025`、`:1060`。

设置 `DISABLE_MMA_V3=1` 或 `DISABLE_MMA_V5=1` 可以让 `supportMMA` 返回 false，用于对比实验。**guided**：只有对应硬件才有意义。

### 变体 6：AMD MFMA（guided）

AMD 的 dot lowering 在 `third_party/amd/lib/TritonAMDGPUToLLVM/DotOpToLLVM.cpp`，使用 MFMA/WMMA，不是 NVIDIA 的 `mma.sync`。**源码依据**：该文件存在且被 AMD backend 注册。不要把 `instrShape=[16,8]` 套到 AMD。

## 对比与取舍

| 路径 | 形状灵活性 | 吞吐 | 操作数 | 累加器 | 适用 |
|---|---|---|---|---|---|
| FMA | 最高 | 最低 | 寄存器 | 寄存器 | 小 K、非对齐、无 tensor core |
| MMAv2 | 中（M16/N8） | 高 | 寄存器 | 寄存器 | Turing/Ampere，规则形状 |
| MMAv3 WGMMA | 较低（M64/N8+） | 很高 | shared descriptor | 寄存器 | Hopper，大 tile、异步 |
| MMAv5 tcgen05 | 较低（M64/N8+） | 最高 | shared/TMEM | TMEM | Blackwell，大 tile、2CTA |

取舍点：

- **形状优先**：FMA 最自由，但 K 一大就慢。
- **寄存器压力**：MMAv2 的 A/B/C 都在寄存器，tile 大了会 spill；WGMMA 把 B 放 shared，缓解寄存器压力。
- **异步**：WGMMA/tcgen05 是异步的，能和 load/其他计算重叠；MMAv2 是同步 warp 指令。
- **架构锁定**：WGMMA 只能 Hopper，tcgen05 只能 Blackwell；写可移植 kernel 时要准备 fallback。

## 踩雷集锦

### 雷 1：以为 T4 会走 MMAv2

T4 硬件有 Turing tensor core，但 3.9.0 的 `BlockedToMMA` 对 `computeCapability < 80` 明确 fallback 到 FMA。**源码依据**：`AccelerateMatmul.cpp:516-521`。**实测**：3.8.0 在 sm75 上生成 256 条 `FFMA`，0 条 `HMMA`。

### 雷 2：以为 `16x16x16` 是一条 MMA

`instrShape=[16,8]` 只描述 M/N；K 由类型决定。16×16×16 的 tile 可能需要多条 `m16n8k8`/`m16n8k16`，数量取决于 warp 划分。**实测**：16×16×16、1 warp、sm80 生成 2 条 `mma.sync.m16n8k16`；4 warps 只生成 1 条，因为每个 warp 的输出 tile 更小。不要用 tile 形状反推指令数量。

### 雷 3：以为 K 可以任意小

MMAv3/v5 要求 `K >= 256 / bitwidth`（`Utility.cpp:1047-1048`、`:1066-1067`）。K=8 的 fp16 dot 不满足 `K>=16`，会 fallback。小 K 的 dot 用 FMA 可能更合适。

### 雷 4：以为 accumulator 是一整块

accumulator 是每个线程 4 个 f32（m16n8），不是整块 16×8。**源码依据**：`MMAv2.cpp:543-545`。从 `#mma` 转回 `#blocked` 需要 layout conversion，通常走 shared memory。

### 雷 5：以为 WGMMA 一定更快

WGMMA 需要 M%64==0、N%8==0、`numWarps%4==0`，否则 fallback 到 MMAv2。小 tile 或 batch dot 可能根本不满足条件。**源码依据**：`Utility.cpp:1059-1086`。

### 雷 6：以为 fp32 dot 在 T4 能用 TF32

T4 没有 TF32 tensor core；`supportMMA` 虽然允许 `inputPrecision=TF32`，但 `BlockedToMMA` 的 sm<80 gate 先把它挡掉了。**源码依据**：`AccelerateMatmul.cpp:516-521`。

### 雷 7：以为 `num_warps` 随便设

MMAv5 只支持 4 或 8 warps（`Utility.cpp:1042-1045`）；MMAv3 要求 4 的倍数（`:1074`）。设 2 warps 可能直接 fallback。

## 进阶

1. **读 `getValuesFromDotOperandLayoutStruct`**：`MMAv2.cpp:78-180`，理解 `kWidth`、`kTiles`、`outer` 如何把 per-thread 元素排成 MMA 操作数。
2. **读 `loadC` 和 `getMmaOperands`**：`MMAv2.cpp:26-58`、`:200-340`，理解 accumulator 的打包和寄存器约束。
3. **读 WGMMA 的 `DotOpMmaSmemLoader`**：`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/Utility.cpp`（搜索 `DotOpMmaSmemLoader`），理解 descriptor 的 leading dimension/stride/swizzle。
4. **读 tcgen05 的 TMEM loader**：`MMAv5.cpp:497-560`，理解 TMEM 的分配和 2CTA 模式。**guided**。
5. **对比 PTX ISA 的 `mma`/`wgmma`/`tcgen05.mma`**：看指令级约束，和 Triton 的 `supportMMA` 对照。

## 动手练习

### 练习 1：观察 T4 的 FMA fallback

编译一个 32×32×32 fp16 dot，目标 `cuda:75`，dump TTGIR/PTX/SASS。确认：

- TTGIR 的结果编码是 `#blocked`；
- PTX 没有 `mma.sync`；
- SASS 是 `FFMA`。

然后读 `AccelerateMatmul.cpp:516-521`，解释为什么。

### 练习 2：观察 sm80 的 MMAv2

同一份 kernel，目标改成 `cuda:80`，确认：

- TTGIR 出现 `#nvidia_mma<{versionMajor = 2, versionMinor = 0, instrShape = [16, 8]}>`；
- A/B 变成 `#ttg.dot_op<{opIdx = 0/1, parent = #mma, kWidth = 2}>`；
- PTX 出现 `mma.sync.aligned.m16n8k16...`；
- SASS 出现 `HMMA.16816.F32`。

数一数 32×32×32 生成了几条 MMA，并用 `repM/repN/repK` 解释。

### 练习 3：用 ptxas 验证 Turing 指令

手写一条 `mma.sync.aligned.m16n8k8.row.col.f32.f16.f16.f32`，用 `ptxas -arch=sm_75` 编译，`nvdisasm` 看 SASS。确认是 `HMMA.1688.F32`。然后说明：这条指令存在，但 3.9.0 的默认 pipeline 不会为 sm75 生成它。

### 练习 4：故意让 MMAv3 不适用

在 H100 上（**guided**）写一个 `M=32` 的 dot，观察 `supportMMA` 因 `M%64 != 0` 拒绝 MMAv3，fallback 到 MMAv2。对比 `M=64` 的 TTGIR。

## 本章重点

- `tt.dot` 的路径由 `AccelerateMatmul` 的版本选择和 `DotOpConversion` 的分发共同决定。
- T4 硬件有 Turing MMAv2，但 3.9.0 对 sm<80 明确 fallback 到 FMA。
- `instrShape=[16,8]` 只描述 M/N；K 由元素位宽决定（fp16→16，tf32→8，fp64→4，int8→32）。
- MMAv2 的 A/B 是 `dot_op` layout，accumulator 是每线程 4 个 f32（m16n8）。
- K 在外层循环，每次 MMA 把上一次的 C 传入，形成累加链。
- WGMMA 用 shared descriptor + 异步 group；tcgen05 用 TMEM + 单 warp 发射。
- 形状、类型、warp 数、对齐任一不满足，都会 fallback；看 TTGIR/PTX，不要看 Python 猜。

## 自我检核

1. 为什么 T4 在 3.9.0 上走 FMA 而不是 MMAv2？给出源码行号。
2. `instrShape=[16,8]` 里的 16 和 8 分别是什么？K 在哪里？
3. `dot_op` 的 `kWidth` 对 fp16 是多少？对 tf32 呢？
4. 一个 m16n8k16 f32 MMA 的 accumulator 每个线程有几个 f32？
5. MMAv2 的循环顺序是什么？为什么 K 在最外层？
6. `supportMMA` 对 fp32 输入有什么额外要求？
7. WGMMA 的 A/B 操作数在哪里？descriptor 描述了什么？
8. MMAv5 为什么只让一个 warp 发射？
9. 16×16×16 的 dot 一定是 2 条 MMA 吗？为什么？
10. `num_warps=2` 对 MMAv3/v5 分别意味着什么？

## 延伸阅读

### 1. `AccelerateMatmul.cpp` 的版本选择

读 `lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp:45-87` 和 `:502-604`。重点看 `getMMAVersionSafe` 的候选列表、`BlockedToMMA` 的 sm<80 gate、以及 MMAv3 与 MMAv2 的分支。学到“谁决定 dot 的 layout”。与本章的 T4 fallback 直接对应。

### 2. `MMAv2.cpp` 的 `convertMMAImpl`

读 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/MMAv2.cpp:485-590`。重点看 `repA/repB/repK`、`loadC`、`fc` 的索引、`for b,k,m,n` 循环。学到“K 如何切、accumulator 如何分布”。与本章的 32×32×32 例子对应。

### 3. `WGMMA.cpp` 的 descriptor

读 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/WGMMA.cpp:185-373`。重点看 `DotOpMmaSmemLoader::build`、`wgmma.fence`、`WGMMAOp`、`emitWait`。学到 Hopper 的异步 warpgroup 模型。与本章的 MMAv3 小节对应。**guided**。

### 4. `MMAv5.cpp` 的 tcgen05

读 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/MMAv5.cpp:487-560` 和 `:941-1015`。重点看 TMEM loader、`elect`、2CTA、commit。学到 Blackwell 的 TMEM 模型。与本章的 MMAv5 小节对应。**guided**。

### 5. PTX ISA 的 `mma` / `wgmma` / `tcgen05.mma`

读 NVIDIA PTX ISA 对应章节。学到指令级形状、操作数、内存序。与本章的指令形状表对应。

## 下一章

下一章从“算”转向“搬与合并”：`tt.reduce`、`tt.scan`、`tt.gather` 如何用 warp shuffle 和 shared memory 实现，`convert_layout` 又如何在这两条路径之间选择。

[Ch 26 — reduce/scan/gather 与 layout conversion](26-reduce-scan-gather-and-layout-conversion.md)
