# Ch 6 — CUDA → NVVM IR：clang 的 device 前端

> **目标**：能自己把 `.cu` 编译成 device 侧 NVVM IR，读懂 `ptx_kernel`、`nvvm.*` intrinsic、地址空间与 libdevice 的痕迹。

## 为什么需要这个？

自研 GPU 做 CUDA 兼容，最上游的选择是「复用谁的前端」。nvcc 的前端是 `cudafe++` + `cicc`（闭源），clang 的前端是开源的，产物是以 LLVM IR 为底、加了 NVVM 约定的方言。理解这份 IR，你才能判断：

- 哪些语义是 CUDA 语言层的（必须自己实现）；
- 哪些已经变成 LLVM 能处理的 IR（可以复用优化器）；
- 哪些是 NVIDIA 私有的内建函数（必须提供等价实现）。

## 先建立直觉

```
 .cu ──clang──► NVVM IR ──llc(NVPTX)──► PTX
        │            │
        │            ├─ ptx_kernel 调用约定
        │            ├─ llvm.nvvm.* intrinsic
        │            ├─ addrspace(3) shared
        │            └─ libdevice 内联后的 __nv_* 代码
        └─ 主机侧代码单独走 host triple
```

关键分界：**NVVM IR 就是 LLVM IR**，只是约定了一组 intrinsic、地址空间和调用约定。这也是为什么「接 NVVM IR」比自己写前端便宜得多。

## 一、只编 device 侧

```bash
BIN=/home/ubuntu/llvm/builds/ct-instr-trunk/bin
$BIN/clang -x cuda --cuda-path=/usr/local/cuda --cuda-gpu-arch=sm_75 \
  --cuda-device-only -O3 -S -emit-llvm -o vadd-dev.ll vadd.cu
```

`vadd.cu`：

```cuda
extern "C" __global__ void vadd(const float *a, const float *b, float *c, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n) c[i] = a[i] + b[i];
}
```

真实产物（节选）：

```llvm
target datalayout = "e-p6:32:32-i64:64-i128:128-i256:256-v16:16-v32:32-n16:32:64"
target triple = "nvptx64-nvidia-cuda"

define dso_local ptx_kernel void @vadd(ptr nofree noundef readonly captures(none) %a,
                                       ptr nofree noundef readonly captures(none) %b,
                                       ptr nofree noundef writeonly captures(none) %c,
                                       i32 noundef %n) local_unnamed_addr #0 {
entry:
  %0 = tail call noundef i32 @llvm.nvvm.read.ptx.sreg.ctaid.x()
  %1 = tail call noundef i32 @llvm.nvvm.read.ptx.sreg.ntid.x()
  ...
}
declare noundef range(i32 0, 2147483647) i32 @llvm.nvvm.read.ptx.sreg.ctaid.x() #1
declare noundef range(i32 1, 1025) i32 @llvm.nvvm.read.ptx.sreg.ntid.x() #1
declare noundef range(i32 0, 1024) i32 @llvm.nvvm.read.ptx.sreg.tid.x() #1
```

四个观察点：

1. **`ptx_kernel` 是调用约定**，不是属性。它决定最终是 `.entry` 还是 `.func`。
2. **`llvm.nvvm.read.ptx.sreg.*`** 是特殊寄存器 intrinsic；`range` 元数据告诉优化器 `tid.x ∈ [0,1024)`。
3. **`target datalayout` 里的 `p6:32:32`**：`NVPTXAddrSpace.h` 把 addrspace 6 定义为 tensor memory；这行说明它的指针是 32 位。这是新架构留下的痕迹。
4. **`captures(none)` / `readonly` / `writeonly`**：CUDA 指针参数默认没有 `noalias`，但 clang 能推出只读/只写；这些属性直接影响后端能否向量化。

## 二、shared memory 与 barrier

```cuda
__shared__ float s[32];
s[t] = in[t];
__syncthreads();
```

IR：

```llvm
@_ZZ4tileE1s = internal unnamed_addr addrspace(3) global [32 x float] undef, align 4
...
%arrayidx2 = getelementptr inbounds nuw [4 x i8],
    ptr addrspacecast (ptr addrspace(3) @_ZZ4tileE1s to ptr), i64 %idxprom
```

`__syncthreads()` 变成 `call void @llvm.nvvm.barrier0()`。注意 IR 里出现了 `addrspacecast`：shared 指针被转成 generic 才能做统一的下标计算，后端再把它 `cvta` 回去（Ch 5）。

## 三、libdevice：数学函数从哪来

CUDA 的 `sinf` / `expf` 不是硬件指令，而是 libdevice 里的实现：

```bash
ls /usr/local/cuda/nvvm/libdevice/
# libdevice.10.bc
```

clang 在 device 编译时会把 `libdevice.10.bc` 链进来，然后内联。实测 `sinf(x)+expf(x)` 在 `-O2` 后的 IR 里没有 `__nv_sinf` 调用了，只剩下底层 intrinsic：

```llvm
declare i32 @llvm.nvvm.f2i.rn(float)
declare float @llvm.nvvm.fma.f32(float, float, float, i32 immarg range(i32 0, 4))
declare float @llvm.nvvm.fabs.f32(float)
declare float @llvm.nvvm.fmul.f32(float, float, i32 immarg range(i32 0, 4))
```

这对自研 GPU 是个硬约束：**你要么实现这些 `llvm.nvvm.*` intrinsic 的等价语义，要么替换整个 libdevice**。数值兼容（ULP、FTZ、特殊值）是 CUDA 兼容测试里最容易翻车的地方。

## 四、NVVM IR 与普通 LLVM IR 的差异

| 维度 | 普通 LLVM IR | NVVM IR |
|---|---|---|
| 调用约定 | C/fastcc/... | 多出 `ptx_kernel` / `ptx_device` |
| intrinsic | 通用 | 大量 `llvm.nvvm.*`（特殊寄存器、屏障、shfl、mma） |
| 地址空间 | 通常平坦 | 0/1/3/4/5/6/7 有明确语义 |
| 异常 | EH / landingpad | 不支持（`invoke` 要 lower 掉） |
| 数学库 | libm | libdevice（`.bc`，可内联） |
| 注解 | 通用 | `nvvm.annotations`、`!nvvmir.version` 等元数据 |

严格说，NVVM IR 是「LLVM IR 的一个受约束子集 + 约定」。这也是为什么它能直接喂给 `llc`。

## 五、nvcc 那条路

nvcc 不是编译器，是调度器：

```
nvcc → cudafe++ → cicc → ptxas → fatbinary → host linker
        (C++ 前端)  (NVVM→PTX)
```

`cicc` 消费的也是 NVVM IR。所以「clang 的前端」和「nvcc 的前端」在 IR 层是同一个生态位，只是实现不同。做兼容层时，接 NVVM IR 等于同时支持两条来路。

## 踩雷集锦

1. **用 `-emit-llvm` 编完整 `.cu`**：会触发 fatbinary 打包失败（Ch 0）；加 `--cuda-device-only`。
2. **以为 IR 里的 `sinf` 还是调用**：`-O2` 会把 libdevice 内联到看不出原样，只剩 `llvm.nvvm.*`。
3. **以为 `ptx_kernel` 只是标记**：它同时决定 ABI（参数提升、返回槽、`.entry`）。
4. **忽略 `captures(none)`/`readonly`**：这些属性丢了，后端会少掉向量化和 `ld.global.nc` 的机会。
5. **拿 `-O0` 的 IR 判断代码质量**：`-O0` 会保留大量 alloca 与临时变量，和最终 PTX 差很远。

## 进阶：自己写一个最小的 NVVM IR

不需要 clang，直接手写 `ptx_kernel` + `llvm.nvvm.read.ptx.sreg.tid.x` 就能跑通（Ch 0 的 `add.ll` 就是）。这对做后端验证特别有用：**你可以精确控制 IR，观察每条 lowering 规则**，而不被 clang 的优化干扰。

## 动手练习

1. 用 `--cuda-device-only -O0` 和 `-O2` 各编一次 `vadd.cu`，diff IR，找出三处被优化掉的结构。
2. 写一个用 `__shared__` 和 `__syncthreads()` 的 kernel，标出 IR 里的 `addrspace(3)` 与 `barrier0`。
3. 把 `sinf` 换成 `__sinf`（fast math intrinsic），对比 IR 里剩下的东西。
4. 给指针参数加 `__restrict__`，确认 PTX 出现 `ld.global.nc`。

## 本章重点整理

- clang 的 device 产物是 NVVM IR：LLVM IR + `ptx_*` 调用约定 + `llvm.nvvm.*` + 地址空间。
- libdevice 在 `-O2` 基本被内联，留下的是 NVVM intrinsic。
- nvcc 的 `cicc` 也消费 NVVM IR，所以这是兼容层的共同接入点。
- 指针属性（`readonly`/`captures`）会一路影响 PTX 的向量化与 `.nc`。

## 自我检核

- [ ] 能不看文档写出一个最小 `ptx_kernel`
- [ ] 能解释 `--cuda-device-only` 解决什么问题
- [ ] 知道 libdevice 的位置与它在 IR 里的痕迹
- [ ] 能说出 NVVM IR 与普通 LLVM IR 的三处差异

## 延伸阅读

- **[LLVM NVPTX Usage](https://llvm.org/docs/NVPTXUsage.html)** — LLVM 官方
  - 读「NVVM IR」相关段落，确认哪些约束是官方承诺。
- **[Clang CUDA 支持](https://clang.llvm.org/docs/CUDA.html)** — Clang 官方
  - 读「Compilation Model」与「Device-side compilation」两节，把本章命令对上。
- **[libdevice 文档](https://docs.nvidia.com/cuda/libdevice-users-guide/)** — NVIDIA
  - 查 `__nv_*` 函数的精度与特殊值行为；数值兼容测试的依据。

clang 产出的 IR 还很「LLVM」，真正把它变成 GPU 语义的是 NVPTX 自己的一串 IR pass。

→ [Ch 7 IR 预处理 pass 群：真正的 GPU 语义修正](./07-ir-preprocessing-passes.md)
