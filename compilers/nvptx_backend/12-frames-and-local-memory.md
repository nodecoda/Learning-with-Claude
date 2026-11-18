# Ch 12 — 栈帧、alloca 与 local memory

> **目标**：知道什么时候会产生 `.local`、`%SP`/`%SPL` 从哪来、动态 alloca 需要什么条件，以及为什么「没有寄存器分配」意味着「没有 spill」。

## 为什么需要这个？

GPU 的 local memory 在物理上是显存（每线程一段，带缓存），延迟比寄存器高两个数量级。编译器每多一个 local 访问，性能就掉一截。NVPTX 的栈帧逻辑很薄，但正因为薄，你更容易看清「什么情况下局部变量会被赶到 local」。

## 先建立直觉

```
IR alloca
   │
   ├─ SROA / 寄存器化 ──► 消失（大多数情况）
   │
   └─ 取地址 / 动态大小 / volatile
          │
          ▼
      .local __local_depot0[N]
          │
          ▼
      %SPL = 基址；访问 = [%SPL + 偏移]
```

关键判断：**alloca 是否「逃逸」或「被取地址」**。能完全拆成标量的，不会进 local。

## 一、静态 alloca：`.local` + `%SPL`

```llvm
%buf = alloca [64 x float], align 4
store volatile float 1.5, ptr %p
%v = load volatile float, ptr %p
```

```ptx
	.local .align 4 .b8 	__local_depot0[256];
	.reg .b64 	%SP;
	.reg .b64 	%SPL;
...
	mov.b64 	%SPL, __local_depot0;
	add.u64 	%rd4, %SPL, 0;
	add.s64 	%rd5, %rd4, %rd3;
	cvta.local.u64 	%rd6, %rd5;
	st.volatile.b32 	[%rd6], 1069547520;
	ld.volatile.b32 	%r2, [%rd6];
```

四个部件：

1. `.local .align 4 .b8 __local_depot0[256]`：每线程 256 字节的栈槽。
2. `%SP`：frame pointer（generic）。
3. `%SPL`：local 空间的 frame pointer。
4. `cvta.local`：把 generic 栈地址转成 local 空间地址。

为什么需要两个栈指针？因为 PTX 的 frame 可能是 generic 的（`%SP`），也可能是 local 的（`%SPL`）；具体用哪个取决于访问方式。

## 二、动态 alloca：需要 PTX 7.3

```llvm
%buf = alloca float, i64 %n, align 4
```

默认 sm_75 / PTX 6.3 直接报错（Ch 3 的原文）：

```
Support for dynamic alloca introduced in PTX ISA version >= 7.3 and requires target sm_52.
```

加 `-mattr=+ptx73` 后：

```ptx
	.version 7.3
...
	stacksave.u64 	%rd4;
	shl.b64 	%rd5, %rd1, 2;
	add.s64 	%rd6, %rd5, 7;
	and.b64 	%rd7, %rd6, -8;
	alloca.u64 	%rd8, %rd7, 8;
	st.local.b32 	[%rd8], 1073741824;
	stackrestore.u64 	%rd4;
```

`alloca.u64` 是真正的动态栈分配；`stacksave`/`stackrestore` 保存和恢复栈顶。注意对齐被手动算成 8（`add 7` + `and -8`）。

## 三、`NVPTXPrologEpilogPass`

这个 pass 做两件事：

1. 计算每个 frame object 的偏移。
2. 把 `FrameIndex` 操作数替换成 `VRFrame` 寄存器相对访问。

源码注释（`NVPTXTargetMachine.cpp`）：

```
NVPTXPrologEpilogPass calculates frame object offset and replace frame
index with VRFrame register. NVPTXPeephole need to be run after that and
will replace VRFrame with VRFrameLocal when possible.
```

`NVPTXPeephole` 再把 `VRFrame` 换成 `VRFrameLocal`——也就是 Ch 5 看到的 `%SPL` 路径。这是一次「generic 栈地址 → local 栈地址」的特化。

## 四、没有 spill

因为不做寄存器分配（Ch 11），LLVM 层不会产生 spill 代码。你看到的 local 访问只有三种来源：

1. 取地址的局部变量（alloca）。
2. 动态 alloca。
3. 内联汇编或特殊 intrinsic 显式要求。

**真正的 spill 在 `ptxas`**：当物理寄存器不够时，它会把值溢出到 local。这部分你在 PTX 里看不到。

## 五、local memory 的性能含义

- 物理位置：显存；每线程独立，访问模式通常是「每线程不同地址」，coalescing 效果差。
- 有 L1/L2 缓存，但命中率取决于访问模式。
- 大小受每线程栈限制（由驱动/架构决定）。

所以：**能不进 local 就不进**。`__shared__` 替代大数组、把数组拆成标量、避免取地址，都是常见手段。

## 踩雷集锦

1. **以为所有 alloca 都会进 local**：SROA 会消掉大部分；只有逃逸的才留下。
2. **在 PTX 里找 spill**：没有；spill 是 `ptxas` 的行为。
3. **动态 alloca 忘了 `+ptx73`**：直接编译失败。
4. **以为 `.local` 是共享的**：它是每线程私有的。
5. **忽略 `cvta.local`**：generic 地址直接喂给 `st.local` 是错的，必须先转换。

## 进阶：栈帧与 occupancy

`.local` 的大小会影响每线程资源占用，但不像寄存器那样直接限制 occupancy。真正需要注意的是**访问频率**：循环里的 local 访问会变成显存流量，成为新的瓶颈。

## 动手练习

1. 写一个「取地址但只读」的局部数组，确认 `.local` 出现；再用 SROA 能消掉的形式重写，确认消失。
2. 用 `-mattr=+ptx73` 编动态 alloca，标出 `stacksave`/`alloca`/`stackrestore`。
3. 把 `.local` 数组改成 `__shared__`，比较 PTX 与 SASS。
4. 用 `ptxas -v` 看 `STACK`/`LOCAL` 字段的变化。

## 本章重点整理

- `.local` 只为「逃逸的 alloca / 动态 alloca」产生。
- `%SP` 是 generic 栈指针，`%SPL` 是 local 栈指针；`cvta.local` 负责转换。
- 动态 alloca 需要 PTX 7.3 + sm_52。
- LLVM 层没有 spill，spill 在 `ptxas`。

## 自我检核

- [ ] 能说出 alloca 进 local 的三个条件
- [ ] 能解释 `%SP` 与 `%SPL` 的分工
- [ ] 知道动态 alloca 的版本要求
- [ ] 能说明为什么 PTX 里看不到 spill

## 延伸阅读

- **[PTX ISA: Stack and Local Memory](https://docs.nvidia.com/cuda/parallel-thread-execution/#state-spaces-local)** — NVIDIA
  - 读 `.local` 与 `alloca` 两节；动态分配的语义在这里。
- **[LLVM Stack Frame Layout](https://llvm.org/docs/CodeGenerator.html#stack-frame-layout)** — LLVM 官方
  - 对照 NVPTX 的 `PrologEpilog` 与 `Peephole` 两个 pass。
- **[CUDA C++ Programming Guide: Local Memory](https://docs.nvidia.com/cuda/cuda-c-programming-guide/#device-memory-accesses)** — NVIDIA
  - 读 local memory 的缓存行为，理解为什么它比寄存器慢。

栈帧之后是调度：NVPTX 有一个 pre-RA 调度器，但它几乎不改变顺序。为什么？

→ [Ch 13 调度：为什么 NVPTX 几乎不调度](./13-scheduling.md)
