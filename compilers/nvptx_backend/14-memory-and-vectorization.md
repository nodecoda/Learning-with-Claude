# Ch 14 — 内存操作与向量化：float4 为什么变成两个 b64

> **目标**：能解释 PTX 的向量访存形式、什么时候会退化成多条标量访问、以及 atomic 的 ordering/scope 怎么映射。

## 为什么需要这个？

GPU 的性能瓶颈大多在内存：coalescing、bank conflict、缓存命中。编译器能控制的是**访问宽度**与**访问模式**。NVPTX 的向量化规则很直接，但经常被 IR 的形状骗到——你以为它会生成 `v4`，实际却是两条 `b64`。

## 先建立直觉

```
warp 的 32 个线程各读 4 字节：
  连续地址 ──► 一次 128B 事务（coalesced）
  跨步地址 ──► 32 次事务（uncoalesced，慢 32 倍）

每线程读 16 字节：
  ld.global.v4.b32  ──► 一条指令，四个 32 位寄存器
  ld.global.b64 ×2   ──► 两条指令，两次事务
```

宽度越大，指令越少、事务越整；但前提是 IR 里真的有「向量形状」。

## 一、PTX 的向量访存

手写 `<4 x float>` 的 load/store：

```llvm
%v = load <4 x float>, ptr addrspace(1) %in, align 16
store <4 x float> %v, ptr addrspace(1) %out, align 16
```

```ptx
	ld.global.v4.b32 	{%r1, %r2, %r3, %r4}, [%rd1];
	st.global.v4.b32 	[%rd2], {%r1, %r2, %r3, %r4};
```

`<2 x float>` 则生成 `ld.global.v2.b32`。这是 NVPTX 的「正常」向量化路径。

## 二、float4 struct 为什么变成两条 b64

```cuda
struct alignas(16) F4 { float x, y, z, w; };
__global__ void vcopy(const F4 *in, F4 *out) {
  out[threadIdx.x] = in[threadIdx.x];
}
```

clang 的 device IR 把它变成 **memcpy**，不是向量 load：

```llvm
tail call void @llvm.memcpy.p0.p0.i64(
    ptr noundef nonnull align 16 dereferenceable(16) %arrayidx2,
    ptr noundef nonnull align 16 dereferenceable(16) %arrayidx,
    i64 16, i1 false), !tbaa.struct !12
```

NVPTX 对 16 字节 memcpy 的 lowering 是两条 64 位拷贝：

```ptx
	ld.global.b64 	%rd8, [%rd6+8];
	st.global.b64 	[%rd7+8], %rd8;
	ld.global.b64 	%rd9, [%rd6];
	st.global.b64 	[%rd7], %rd9;
```

**结论**：这不是「NVPTX 不支持 v4」，而是**IR 里根本没有 `<4 x float>`**。C++ 的 struct 是聚合类型，clang 用 memcpy 表达复制；NVPTX 把 memcpy 展开成两条 b64。

想让后端生成 `v4`，得让 IR 里出现真正的向量 load/store。这也是 `LoadStoreVectorizer`（Ch 7）存在的原因：它尝试把相邻标量访问合并成向量形状。

## 三、对齐决定能不能向量化

PTX 的向量访问要求操作数按向量宽度对齐：

| 类型 | 要求对齐 |
|---|---|
| `v2.b32` | 8 字节 |
| `v4.b32` | 16 字节 |
| `v2.b64` | 16 字节 |

如果 IR 只声明 `align 4`，后端不会生成 `v4`（否则是未定义行为）。所以：

```llvm
load <4 x float>, ptr %p, align 4    ; 不会变成 v4
load <4 x float>, ptr %p, align 16   ; 会
```

## 四、atomic 与内存序

```cuda
atomicAdd(p, 1);
```

IR：

```llvm
%0 = atomicrmw add ptr %p, i32 1 syncscope("device") monotonic, align 4
```

PTX：

```ptx
atom.relaxed.gpu.global.add.u32 	%r1, [%rd2], 1;
```

三段映射：

| IR | PTX | 说明 |
|---|---|---|
| `monotonic` | `.relaxed` | 最弱序，只保证原子性 |
| `acquire` / `release` | `.acquire` / `.release` | 获取/释放语义 |
| `seq_cst` | `.acq_rel` 或带 fence | 最强序 |
| `syncscope("device")` | `.gpu` | 设备内可见 |
| `syncscope("system")` | `.sys` | 跨设备/主机可见 |

`NVPTX.h` 里的 `Ordering` / `Scope` 枚举就是这张表的实现。GPU 的内存模型是弱序的，写代码时**默认不要假设顺序**。

## 五、只读缓存：`ld.global.nc`

```cuda
__global__ void rd(const float * __restrict__ in, float *out) {
  out[threadIdx.x] = in[threadIdx.x] * 2.0f;
}
```

```ptx
ld.global.nc.b32 	%r2, [%rd6];
```

`.nc` 表示 non-coherent：数据在 kernel 执行期间不会被写，可以走只读缓存（texture path）。触发条件是「只读 + 不可变」的证明，通常来自 `const __restrict__` 与 `!invariant.load`。

## 六、shared memory 与 bank conflict

NVPTX 不做 shared memory 的分配与 swizzle（Ch 1）。它只负责发射 `st.shared` / `ld.shared`；bank conflict 由**访问模式**决定，通常要在源码/Triton/CUTLASS 层解决（例如 padding、swizzle）。

编译器能做的是：把相邻访问合并成更宽的 shared 访问。但 32 bank 的冲突规则是硬件行为，不是后端能改写的。

## 踩雷集锦

1. **以为 float4 一定生成 `v4.b32`**：看 IR 是不是 memcpy/聚合复制。
2. **声明 `align 4` 却期待 16 字节向量访问**：对齐不足，后端不会生成。
3. **以为 `.nc` 是自动的**：需要只读/不变的证明；写过的指针不会走 `.nc`。
4. **把 `atomicAdd` 当顺序一致的锁**：默认是 relaxed + device scope。
5. **在 PTX 层解决 bank conflict**：它由访问模式决定，后端只发射指令。

## 进阶：什么时候该手动向量化

- 数据本来就连续、对齐已知（例如 `float4*` 且 16 字节对齐）。
- 每个线程处理多个元素（thread coarsening），让一条向量指令覆盖更多数据。
- 避免 uncoalesced 的跨步访问；必要时重排数据布局。

Triton 的 block-level 模型就是在 IR 层显式表达这些形状，让后端能稳定生成向量访问。

## 动手练习

1. 用 `<4 x float>` 与 `struct F4` 各写一个拷贝 kernel，对比 PTX。
2. 把 `align 16` 改成 `align 4`，确认 `v4` 消失。
3. 写一个 `atomicAdd`，改 `syncscope` 与 ordering，观察 PTX 后缀变化。
4. 用 `__restrict__ const` 触发 `ld.global.nc`，再去掉 `const` 看它消失。

## 本章重点整理

- PTX 支持 `v2`/`v4` 向量访存，但要求对齐与真正的向量 IR 形状。
- C++ struct 复制常被 lower 成 memcpy，再展开成多条 b64。
- atomic 的 ordering/scope 直接映射到 PTX 后缀。
- `.nc` 需要只读证明；shared 的 bank conflict 不在后端解决。

## 自我检核

- [ ] 能解释 float4 为什么可能变成两条 b64
- [ ] 能说出 `v4.b32` 的对齐要求
- [ ] 能把 `monotonic` / `syncscope("device")` 映射到 PTX
- [ ] 知道 `.nc` 的触发条件与正确性前提

## 延伸阅读

- **[PTX ISA: Memory Instructions](https://docs.nvidia.com/cuda/parallel-thread-execution/#memory-instructions)** — NVIDIA
  - 读 `ld` / `st` 的向量形式与对齐要求；本章的 `v4` 语法在这里。
- **[PTX ISA: Memory Consistency Model](https://docs.nvidia.com/cuda/parallel-thread-execution/#memory-consistency-model)** — NVIDIA
  - 读 ordering 与 scope 两节；atomic 后缀的语义来源。
- **[CUDA C++ Best Practices: Memory Optimizations](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/#memory-optimizations)** — NVIDIA
  - 读 coalescing 与 shared memory 两节，理解访问模式为什么比指令宽度更重要。

下面是练习 C：把目标矩阵、ptxas 资源与 SASS 放在一起看。

→ [练习 C：版本矩阵、ptxas 资源与 SASS 对照](./practice-c-target-matrix-and-ptxas.md)
