# Ch 4 — 内核 ABI 与调用约定

> **目标**：能读懂 `.entry` / `.func` 的参数列表、返回槽、调用序列与 launch bounds，并知道每一项背后的 ABI 规则。

## 为什么需要这个？

CUDA 的 kernel 调用不是普通函数调用：主机把参数打包成一块缓冲区，驱动在启动时把它放进 constant bank，SM 从 `.param` 空间读取。这个约定如果建模错了，kernel 会「参数全乱」而不是崩溃——最难查的一类 bug。

LLVM 把这套约定集中在两处：`NVPTXISelLowering::LowerFormalArguments` / `LowerCall`（Ch 8）和 `NVPTXAsmPrinter` 的参数打印（本章）。本章先看产物，下一章再看 lowering。

## 先建立直觉

```
host:  cuLaunchKernel(f, grid, block, ..., args)
           │
           ▼
   驱动把 args 按 ABI 打包 ──► constant bank 0
                                   │
kernel:  .entry k(.param .u64 k_param_0, .param .b32 k_param_1)
             │
             ▼
         ld.param.b64 %rd1, [k_param_0]
```

`.param` 不是普通内存：它是**调用者分配、被调用者只读**的参数空间。想写它，必须先 `mov` 到 local（Ch 7 的 `ForwardParams` 就是干这个优化）。

## 一、`.entry` 与 `.func`

```ptx
.visible .entry add(
	.param .u64 .ptr .align 1 add_param_0,
	...
)
```

```ptx
.visible .func (.param .b32 func_retval0) scale(
	.param .b32 scale_param_0,
	.param .b32 scale_param_1
)
```

差异三条：

1. `.entry` 是 kernel，由驱动启动；`.func` 是 device 函数，只能被 device 代码调用。
2. `.func` 的返回值是一个**显式参数槽** `func_retval0`，用 `st.param` 写入；`.entry` 只能返回 void。
3. 两者的参数提升规则不同（下节）。

LLVM 侧用调用约定区分：`ptx_kernel` vs `ptx_device`。`isKernelFunction()`（`NVVMProperties.h`）就是查 `CallingConv::PTX_Kernel`。

## 二、参数类型提升规则

源码里两个函数说清了一切（`NVPTXUtilities.h`）：

```cpp
// device 函数/返回值：至少 32 位
inline unsigned promoteScalarArgumentSize(unsigned Size) {
  return PowerOf2Ceil(std::max(Size, 32U));
}
// kernel 参数：至少 8 位
inline unsigned promoteScalarKernelArgumentSize(unsigned Size) {
  return PowerOf2Ceil(std::max(Size, 8U));
}
```

于是：

| IR 类型 | kernel `.param` | device `.param` |
|---|---|---|
| i1 / i8 | `.b8` | `.b32` |
| i16 | `.b16` | `.b32` |
| i32 / float | `.b32` | `.b32` |
| i64 / double / ptr | `.b64`（指针带 `.ptr`） | `.b64` |
| half / bf16 | `.b8[2]`（按数组传） | `.b8[2]` |
| 聚合 / 向量 / ≥128 位 | `.b8[N]`（按数组传） | 同左 |

实测 `dev8`（device 函数收 i8/i16）：

```ptx
.visible .func (.param .b32 func_retval0) dev8(
	.param .b32 dev8_param_0,
	.param .b32 dev8_param_1
)
...
	ld.param.b8 	%rs1, [dev8_param_0];
	ld.param.b8 	%rs2, [dev8_param_1];
```

声明按 ABI 提升到 `.b32`，读取仍可写 `ld.param.b8`（PTX 允许窄读）。这类「声明宽度 ≠ 读取宽度」是读 PTX 时最容易误判的地方。

## 三、对齐：`.ptr .align 1` 从哪来

默认的 kernel 指针参数长这样：

```ptx
.param .u64 .ptr .align 1 add_param_0,
```

`align 1` 不是 bug。LLVM 24 用 opaque pointer，IR 里没有 pointee 对齐信息，参数也没有 `align` 属性，于是 `valueOrOne()` 给出 1。给参数加 `align` 属性就变：

```llvm
define ptx_kernel void @al(ptr align 8 %out, ptr align 16 %in)
```

```ptx
.param .u64 .ptr .align 8 al_param_0,
.param .u64 .ptr .align 16 al_param_1
```

这就是 clang 生成的代码里常见 `align 1` 的原因：它没给 kernel 指针参数加对齐属性。**要验证真实对齐假设，得看源头 IR，而不是凭 PTX 猜。**

## 四、聚合参数：byval 与直接读

```llvm
%S = type { float, float }
define ptx_kernel void @bv(ptr byval(%S) align 8 %s, ptr %out)
```

```ptx
.visible .entry bv(
	.param .align 8 .b8 bv_param_0[8],
	.param .u64 .ptr .align 1 bv_param_1
)
...
	ld.param.b32 	%r1, [bv_param_0+4];
```

注意两点：

- 聚合按 `.b8[N]` + `.align` 传，N 是 `getTypeAllocSize`。
- `ld.param.b32 [bv_param_0+4]` 直接带常量偏移读取，没有先 `mov` 到 local。这是 `NVPTXForwardParams` 的成果（Ch 7）：能证明「只读且偏移可表示」时，就不做本地拷贝。

## 五、调用序列

device 函数调用在 PTX 里是显式参数槽 + `call.uni`：

```ptx
{ // callseq 0, 0
	.param .b32 	param0;
	.param .b32 	param1;
	.param .b32 	retval0;
	st.param.b32 	[param1], %r1;
	st.param.b32 	[param0], %r3;
	call.uni (retval0), scale, (param0, param1);
	ld.param.b32 	%r4, [retval0];
} // callseq 0
```

读法：

1. 调用者先声明三个**局部** `.param`（两个入参 + 一个返回槽）。
2. 用 `st.param` 写入。
3. `call.uni` 同步调用（不是 `call`，`uni` 表示非递归/直接）。
4. 用 `ld.param` 取回返回值。

这段结构由 `LowerCall`（Ch 8）生成，`NVPTXProxyRegErasure` 负责把为保活 `callseq_end` 而插入的伪寄存器删掉（Ch 7）。

## 六、launch bounds 变成 PTX 指令

```cuda
__global__ void __launch_bounds__(128, 4) lb(float *p);
```

```ptx
.visible .entry lb(
	.param .u64 .ptr .align 1 lb_param_0
)
.maxntid 128
.minnctapersm 4
```

`.maxntid` 是编译期承诺「每 block 最多 128 线程」，`.minnctapersm` 是「每 SM 至少驻留 4 个 block」。它们不会改变 PTX 指令，但会作为**元数据**传给 `ptxas`，影响寄存器分配策略——这是 Ch 11、16 的伏笔：**occupancy 的唯一编译期入口，就是这类指令**。

## 踩雷集锦

1. **看到 `.ptr .align 1` 就以为指针只能 1 字节对齐**：它是声明缺省，不代表真实数据对齐。
2. **以为 kernel 参数可以直接写**：`.param` 只读；取地址或写入会触发 `mov` 到 local 的拷贝。
3. **把 `.func` 当成普通函数**：它的返回值是显式参数槽，`st.param`/`ld.param` 成对出现。
4. **忽略参数总大小上限**：kernel 参数空间有容量限制（随架构/工具链变化），参数过多会启动失败而不是编译失败。
5. **以为 `__launch_bounds__` 只是优化提示**：它同时是正确性承诺；线程数超过 `.maxntid` 行为未定义。

## 进阶：为什么 ABI 要「至少 8 位 / 32 位」

PTX 的寄存器银行最小是 8 位、参数槽按 2 的幂对齐。硬件的 constant bank 读取粒度更粗，所以 ABI 选择「向上取到 2 的幂」。这也解释了为什么 i1 参数要占一个 `.b8`：**PTX 没有 1 位的参数槽**。

## 动手练习

1. 写一个 kernel，参数依次为 i1、i8、i16、i32、i64、float、double、half，列出全部 `.param` 类型，并和上表对照。
2. 给指针参数加 `align 16`，确认 PTX 变化。
3. 写一个 device 函数收 `{float,float}` 聚合参数，看它变成 `.b8[8]` 还是两个 `.b32`。
4. 用 `__launch_bounds__(256, 2)` 编译，确认两条指令的数值。

## 本章重点整理

- `.entry` 是驱动启动的 kernel；`.func` 有显式返回槽。
- kernel 标量参数至少 8 位，device 函数至少 32 位；聚合/半精度按数组传。
- `align 1` 来自缺失的对齐属性，不是硬件限制。
- `__launch_bounds__` 变成 `.maxntid` / `.minnctapersm`，是 occupancy 的唯一编译期入口。

## 自我检核

- [ ] 能说出 `.entry` 与 `.func` 的三处差异
- [ ] 能背出 kernel/device 两套参数提升规则
- [ ] 能解释 `.ptr .align 1` 的由来
- [ ] 能读懂 callseq 里的三个 `.param` 分别是什么

## 延伸阅读

- **[PTX ISA: Function Declarations](https://docs.nvidia.com/cuda/parallel-thread-execution/#function-declarations)** — NVIDIA
  - 读 `.entry` / `.func` / `.param` 三节，本章的产物逐条对得上。
- **[PTX ISA: Kernel and Device Functions](https://docs.nvidia.com/cuda/parallel-thread-execution/#kernel-and-device-functions)** — NVIDIA
  - 看参数传递与 ABI 对齐要求；`.align` 的语义在这里。
- **[CUDA C++ Programming Guide: Execution Configuration](https://docs.nvidia.com/cuda/cuda-c-programming-guide/#execution-configuration)** — NVIDIA
  - 读 `__launch_bounds__` 小节，理解 `.maxntid` 的承诺性质。

ABI 讲完，接下来是 GPU 编译器最独特的部分：指针不是地址，而是「空间 + 偏移」。

→ [Ch 5 指针与地址空间：generic / global / shared / local](./05-address-spaces.md)
