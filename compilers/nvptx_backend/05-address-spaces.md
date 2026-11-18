# Ch 5 — 指针与地址空间：generic / global / shared / local

> **目标**：理解 PTX 的地址空间模型，能解释 `cvta` 系列指令、为什么 `-O0` 和 `-O2` 的 load 不一样，以及 shared memory 在 PTX 层怎么表示。

## 为什么需要这个？

CPU 后端里，指针就是一个整数。GPU 不是：同一块「地址」可能落在 global 显存、block 内 shared memory、每线程 local 栈、或常量内存，访问路径和延迟差一个数量级。PTX 把这件事上升成类型系统——指针带空间，指令带空间限定符。

自研 GPU 如果照抄 CPU 的平坦地址模型，性能会直接塌掉；如果照抄 PTX 的空间模型，就要实现 `cvta` 和 generic 判定。这一章是两种路线的分界。

## 先建立直觉

```
        generic 指针（一个 64 位值）
                 │
     ┌───────────┼────────────┬──────────────┐
     ▼           ▼            ▼              ▼
  global      shared       local         constant
  (显存)     (block 共享)  (每线程栈)     (只读缓存)
   长延迟      低延迟        本地           广播
```

generic 指针像「全国地址」，具体空间像「某栋楼」。要知道它落在哪栋楼，得查一次（`cvta` 做的是反方向：把具体空间地址转成 generic）。

## 一、LLVM 里的地址空间编号

`llvm/include/llvm/Support/NVPTXAddrSpace.h` 实测值：

| 编号 | 空间 | 说明 |
|---|---|---|
| 0 | generic | 默认 `ptr` |
| 1 | global | 显存 |
| 3 | shared | block 内共享 |
| 4 | const | 常量内存 |
| 5 | local | 每线程栈 |
| 6 | tensor | Tensor Memory（新架构） |
| 7 | shared_cluster | cluster 共享（Hopper+） |
| 101 | entry_param | PTX 8.4 的 entry 参数空间 |

注意 **2 被跳过**：当前 LLVM 的 NVPTX 映射里没有定义编号 2，源码直接从 1 跳到 3。

## 二、shared memory 在 IR 与 PTX 里的样子

```cuda
__shared__ float s[32];
s[t] = in[t];
__syncthreads();
out[t] = s[31 - t];
```

clang 的 device IR：

```llvm
@_ZZ4tileE1s = internal unnamed_addr addrspace(3) global [32 x float] undef, align 4
...
%arrayidx2 = getelementptr inbounds nuw [4 x i8],
    ptr addrspacecast (ptr addrspace(3) @_ZZ4tileE1s to ptr), i64 %idxprom
```

`llc` 的 PTX（节选）：

```ptx
	// demoted variable
	.shared .align 4 .b8 _ZZ4tileE1s[128];
...
	mov.b64 	%rd7, _ZZ4tileE1s;
	add.s64 	%rd8, %rd7, %rd5;
	st.shared.b32 	[%rd8], %r2;
	bar.sync 	0;
	sub.s64 	%rd9, %rd7, %rd5;
	ld.shared.b32 	%r3, [%rd9+124];
```

三件事值得注意：

1. **demoted variable**：模块级 `addrspace(3)` 全局被「降级」成函数内 `.shared` 声明（Ch 15 讲 `emitDemotedVars`）。
2. `st.shared` / `ld.shared` 直接带空间限定符，不需要 `cvta`。
3. `s[31-t]` 被 strength-reduce 成 `[%rd9+124]`——`sub` + 偏移，而不是再算一次下标。

## 三、generic 与特化：`-O0` vs `-O2`

Ch 0 的 diff 里最值得单独看的一处：

```ptx
// -O0
ld.b32 	%r7, [%rd6];
st.b32 	[%rd8], %r9;
```

```ptx
// -O2
cvta.to.global.u64 	%rd6, %rd5;
ld.global.b32 	%r6, [%rd1];
st.global.b32 	[%rd3], %r8;
```

`-O0` 用 generic load：运行时按地址高位判断落在哪个空间。`-O2` 通过 `cvta.to.global` 把指针特化，后续可以直接用 `.global` 形式。**特化的前提是能证明指针确实指向 global**——kernel 指针参数默认是 generic，所以这个证明来自 `addrspace` 信息或已知的调用约定。

## 四、`cvta` 家族

| 指令 | 方向 | 典型场景 |
|---|---|---|
| `cvta.to.global` | 具体 → generic | 把 global 符号地址变成可传的 generic 指针 |
| `cvta.global` | generic → 具体 | 反向，用于已经确定空间时 |
| `cvta.to.shared` / `cvta.shared` | shared ↔ generic | 共享内存指针传递 |
| `cvta.local` | generic → local | 栈上对象的地址 |

`alloc2` 实测（Ch 12 会完整拆）：

```ptx
	mov.b64 	%SPL, __local_depot0;
	add.s64 	%rd5, %rd4, %rd3;
	cvta.local.u64 	%rd6, %rd5;
	st.volatile.b32 	[%rd6], 1069547520;
```

为什么这里要 `cvta.local`？因为 `%SPL` 是 generic 形式的栈指针，`st.local` 需要 local 空间地址。LLVM 用 `LowerADDRSPACECAST` 生成这些转换（`NVPTXISelLowering.cpp`）。

## 五、地址宽度与算术成本

64 位地址在 PTX 里是常态（`nvptx64`）。代价是：

- 所有地址算术是 `add.s64` / `mul.wide.s32`，比 32 位贵。
- 编译器会尽量把「基址 + 常量偏移」折叠进内存操作数（`[%rd9+124]`），避免额外的 `add`。
- 索引计算常用 `mul.wide.s32`（32×32→64），而不是先 `cvt` 再 `shl`（`-O2` 的成果，Ch 10）。

## 踩雷集锦

1. **以为 `ptr` 就一定是 global**：默认是 addrspace(0) generic；kernel 参数尤其如此。
2. **以为 `cvta` 是空操作**：它可能产生真实指令；但在已知空间时会被折叠。
3. **把 `.shared` 当全局变量**：它是每 block 一份，大小在启动时决定；声明在函数内还是模块级影响可见性。
4. **在 shared 指针上做 generic 访问**：能跑，但多一次空间判定；shared 的延迟优势会被吃掉一部分。
5. **忽略 addrspacecast 的合法性**：不是任意空间之间都能转；LLVM 只允许有定义的组合。

## 进阶：cluster 与 tensor memory

Hopper 引入 thread block cluster，shared memory 可以跨 block 访问（addrspace 7）；Blackwell 引入 tensor memory（addrspace 6）。它们的 PTX 表示和同步原语都在演化——这正是「PTX 是活契约」的体现。自研 GPU 若要兼容新特性，得同步跟进这些空间编号和指令。

## 动手练习

1. 把 `smem.cu` 的 `__shared__` 改成动态 shared（`extern __shared__`），看 PTX 里 `.shared` 声明变成什么。
2. 手写一个 `addrspace(1)` 全局变量并在 kernel 里读写，观察是否还出现 `cvta`。
3. 对同一个函数分别用 `-O0`/`-O2` 编译，统计 `ld.global` 与 `ld.b32` 的出现次数。
4. 把 kernel 指针参数改成 `ptr addrspace(1)`，看 PTX 是否还保留 `cvta.to.global`。

## 本章重点整理

- GPU 指针 = 空间 + 偏移；generic 是默认空间，需要运行时判定。
- shared memory 在 IR 是 `addrspace(3)`，在 PTX 是 `.shared`，常被 demote 到函数内。
- `cvta` 家族负责具体空间与 generic 之间转换；能证明空间时会被优化掉。
- 地址算术以 64 位为主，偏移折叠是性能关键。

## 自我检核

- [ ] 能背出 0/1/3/4/5 五个地址空间
- [ ] 能解释 `-O0` 的 `ld.b32` 与 `-O2` 的 `ld.global.b32` 差在哪
- [ ] 能读懂 `cvta.to.global` / `cvta.local` 的方向
- [ ] 知道 shared 变量为什么会「demoted」

## 延伸阅读

- **[PTX ISA: State Spaces](https://docs.nvidia.com/cuda/parallel-thread-execution/#state-spaces)** — NVIDIA
  - 读「Address Space Identifiers」与「cvta」两节；本章的编号表和指令语义都在这里。
- **[LLVM NVPTX Usage](https://llvm.org/docs/NVPTXUsage.html)** — LLVM 官方
  - 看「Address Spaces」小节，确认 LLVM 侧的编号约定。
- **[CUDA C++ Programming Guide: Memory Hierarchy](https://docs.nvidia.com/cuda/cuda-c-programming-guide/#memory-hierarchy)** — NVIDIA
  - 读 shared memory 与 constant memory 两节，把空间和硬件路径对上。

下面是练习 A：用你手写的 IR 把这一章的规则全部逼出来。

→ [练习 A：用手写 IR 逼出 ABI 与地址空间](./practice-a-handwritten-ir.md)
