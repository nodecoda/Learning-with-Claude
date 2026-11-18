# Ch 7 — IR 预处理 pass 群：真正的 GPU 语义修正

> **目标**：知道 NVPTX 在 ISel 之前跑了哪些 IR pass、每个 pass 修的是哪条 GPU 语义，并能用产物验证至少三个。

## 为什么需要这个？

clang 产出的 NVVM IR 仍然是「通用 LLVM IR」：byval 参数是普通指针、取地址的局部变量是 alloca、不可达块还在、数学表达式还没折成 FMA。PTX 的表达能力比 IR 窄，所以在选指令之前必须把 IR 改写成 PTX 能承接的形状。

这一串 pass 是 NVPTX 最有「GPU 味」的部分：很多规则在 CPU 后端根本不存在。

## 先建立直觉

```
NVVM IR
  │
  ├─ NVVMReflect              __CUDA_ARCH__ 条件编译常量折叠
  ├─ CanonicalizeDynamicShared  extern __shared__ 归一
  ├─ AssignValidGlobalNames     符号名合法化
  ├─ GenericToNVVM              通用 IR → NVVM 约定
  ├─ ExpandVariadics            可变参数降级
  ├─ MarkKernelPtrsGlobal       kernel 指针推断为 global（插 addrspacecast）
  ├─ PromoteParamAlign          参数对齐提升
  ├─ LowerArgs                  byval 参数：只读直用 / grid constant / 本地拷贝
  ├─ 地址空间推断 + 直线标量优化
  ├─ AtomicExpand               atomic 语义展开
  ├─ CtorDtorLowering           全局构造/析构 → kernel
  │
  ├─ LSR / EarlyCSE / GVN / SROA / LoadStoreVectorizer
  ├─ TagInvariantLoads          invariant.load → ld.global.nc
  ├─ IRPeephole                 FMUL+FADD → FMA
  └─ LowerUnreachable           绕过 ptxas 已知 bug
  │
  ▼
SelectionDAG
```

## 一、参数与 ABI 相关

### `NVPTXMarkKernelPtrsGlobal`

源码注释说得很清楚：**CUDA kernel 从 byval 参数里读出的指针，一定在 global 空间**。这个 pass 插入 `addrspacecast` 对，让后续地址空间推断能把 global 传播下去。它还处理「指针先被 load 成整数、再 inttoptr」的模式。

为什么需要它：PTX 的 generic 访问比 global 访问贵，而 kernel 参数是 generic 指针（Ch 4）。有了这个证明，`-O2` 才能生成 `cvta.to.global` + `ld.global`。

### `NVPTXLowerArgs`

byval 参数在 PTX 里是 `.param` 空间，只读、只能通过 `ld.param` 访问。这个 pass 有三种策略（源码注释原文归纳）：

| 策略 | 条件 | 结果 |
|---|---|---|
| 直接只读 | 所有 use 都能转成 param 空间 | 不拷贝，直接 `ld.param` |
| grid constant | 参数是 grid constant 且支持 `cvta.param` | 转成 generic 指针直接用 |
| 本地拷贝 | 参数被写、或 use 追踪不全 | 复制到 local，再当普通指针用 |

这正是 Ch 4 里 `ld.param.b32 [bv_param_0+4]` 能出现的原因：走了「直接只读」策略。

### `NVPTXPromoteParamAlign`

把参数对齐提升到 ABI 要求（例如指针至少 8 字节）。影响 `.param .ptr .align N` 的数值，也影响后端能否用更宽的 load。

## 二、地址与内存相关

### `NVPTXAddressFolder`

SelectionDAG 只能在单个基本块内把符号地址折进内存操作数；跨块时地址会被具体化成 `mov.b64 %rd1, kernel_param_0`，后续访问变成寄存器相对。这个 pass 把符号折回去：

```
; 之前
mov.b64       %rd1, kernel_param_0;
ld.param.b64  %rd2, [%rd1];
ld.param.b64  %rd3, [%rd1+8];

; 之后
ld.param.b64  %rd2, [kernel_param_0];
ld.param.b64  %rd3, [kernel_param_0+8];
```

它**刻意不碰 shared memory**：shared 符号的 `mov` 另有用途（Ch 5 的 `mov.b64 %rd6, sh`）。

### `NVPTXTagInvariantLoads`

给只读、不可变的 load 打 `!invariant.load`，后端据此生成 `ld.global.nc`（non-coherent，走只读缓存）。实测：

```cuda
__global__ void rd(const float * __restrict__ in, float *out) {
  out[threadIdx.x] = in[threadIdx.x] * 2.0f;
}
```

```ptx
ld.global.nc.b32 	%r2, [%rd6];
```

`__restrict__` + `const` 是这条优化的常见触发条件。

### `NVPTXLowerAlloca`（仅 `-O0`）

`-O0` 下必须显式把 alloca 降到 local 空间，保证栈布局正确；`-O2` 由 SROA/寄存器化处理掉大部分 alloca。

## 三、语义与正确性相关

### `NVVMReflect`

把 `llvm.nvvm.reflect`（来自 `__CUDA_ARCH__` 条件）按目标 SM 版本折成常量。典型用法：

```cuda
#if __CUDA_ARCH__ >= 800
  // Ampere 路径
#else
  // 旧路径
#endif
```

### `NVPTXCtorDtorLowering`

CUDA 没有 C++ 运行期的全局构造器机制，`__device__` 全局对象的构造/析构被改写成特殊 kernel，由驱动在模块加载时调度。

### `NVPTXLowerUnreachable`

源码里写得很直白：这是为了绕过 ptxas 的一个已知 bug，只在目标带 `PTXASUnreachableBug` 时启用。**后端里出现「为某个 ptxas 版本打补丁」是常态**，这也是维护 GPU 后端的隐性成本。

### `NVPTXProxyRegErasure`

调用序列为了保活 `callseq_end` 会插入伪寄存器；这个 pass 在寄存器分配前把它们删掉，避免污染 PTX。

## 四、优化相关

| Pass | 作用 | 验证方式 |
|---|---|---|
| `EarlyCSE` / `GVN` | 清理 LSR 产生的重复表达式 | 看 IR 里重复的 `add/shl` 是否合并 |
| `LoadStoreVectorizer` | 合并相邻 load/store | 看是否出现更宽的访问 |
| `SROA`（`AggregateToVector`） | 聚合拆成标量/向量 | 看 `float4` 是否被拆成 4 个 float |
| `NVPTXIRPeephole` | FMUL+FADD → FMA | 见下 |

FMA 折叠实测：

```cuda
o[i] = a[i] * b[i] + c[i];
```

```ptx
fma.rn.f32 	%r5, %r2, %r3, %r4;
```

注意它需要 `contract` fast-math 标志；clang 默认给 CUDA 打开，手写 IR 则要自己加 `contract`。

## 五、为什么这些 pass 不能省

一个思想实验：把 `LowerArgs` 拿掉，byval 参数会以普通 generic 指针出现在 IR 里，但 PTX 的 `.param` 空间根本不支持「普通指针写」。后端要么生成非法 PTX，要么在 ISel 里临时补救。**这些 pass 的存在，是为了让 ISel 只需要处理「已经符合 PTX 形状」的 IR。**

## 踩雷集锦

1. **以为 byval 一定拷贝**：只读且 use 可追踪时，`ForwardParams`/`LowerArgs` 会直接 `ld.param`。
2. **以为 `!invariant.load` 只是元数据**：它直接决定 `ld.global.nc`，进而影响走不走只读缓存。
3. **手写 IR 时忘了 `contract` 却期待 FMA**：没有 fast-math 标志，`fmul+fadd` 不会被折。
4. **在 `-O0` 下找 `ld.global.nc`**：大量优化 pass 只在非 `-O0` 启用。
5. **以为这些 pass 是「优化」**：`LowerArgs`、`LowerAlloca`、`CtorDtorLowering` 是正确性必需。

## 进阶：读 pass 的注册顺序

顺序本身就是设计文档。例如 `MarkKernelPtrsGlobal` 必须在地址空间推断之前，`LowerArgs` 必须在 `MarkKernelPtrsGlobal` 之后（它依赖参数已被标记为 global）。读 `NVPTXTargetMachine.cpp` 的 `addIRPasses()` 时，不要只看「加了什么」，要看「为什么必须在这个位置」。

## 动手练习

1. 写一个 byval 参数被写入的 kernel，确认 PTX 出现 local 拷贝（对比只读版本）。
2. 用 `__restrict__` + `const` 触发 `ld.global.nc`，再去掉 `const` 看它消失。
3. 手写 `fmul`+`fadd`，分别加/不加 `contract`，验证 FMA 折叠。
4. 在 `-O0` 与 `-O2` 下各编一次，统计 `alloca` 与 `ld.param` 的数量差异。

## 本章重点整理

- ISel 之前有一整套 GPU 语义修正，很多是正确性必需，不是优化。
- byval 参数有三条 lowering 策略，决定 PTX 里是 `ld.param` 还是 local 拷贝。
- `!invariant.load` → `ld.global.nc`，`contract` → FMA，都是「IR 元数据决定 PTX」。
- 后端会为 ptxas 的 bug 打局部补丁（`LowerUnreachable`）。

## 自我检核

- [ ] 能说出 byval 参数的三条 lowering 策略
- [ ] 能解释 `MarkKernelPtrsGlobal` 为什么能带来 `cvta.to.global`
- [ ] 知道 `ld.global.nc` 的触发条件
- [ ] 能举出至少两个「正确性必需」而非「优化」的 pass

## 延伸阅读

- **[LLVM NVPTX Usage](https://llvm.org/docs/NVPTXUsage.html)** — LLVM 官方
  - 读「Address Spaces」与「Intrinsics」两节，理解这些 pass 依赖的约定。
- **[PTX ISA: State Spaces](https://docs.nvidia.com/cuda/parallel-thread-execution/#state-spaces)** — NVIDIA
  - 看 `.param` 的只读限制；`LowerArgs` 的三条策略都由此而来。
- **[LLVM Alias Analysis](https://llvm.org/docs/AliasAnalysis.html)** — LLVM 官方
  - 读 `!invariant.load` 与 `noalias` 的语义，理解 `ld.global.nc` 的正确性前提。

IR 修好之后，下一个问题是：函数边界怎么落到 PTX 的 `.entry` / `.func` 上。

→ [Ch 8 函数 lowering：参数、返回与调用](./08-function-lowering.md)
