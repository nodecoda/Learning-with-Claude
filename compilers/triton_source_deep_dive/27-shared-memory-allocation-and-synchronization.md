# Ch 27 — shared memory 分配与同步

> `ttg.local_alloc` 只是“我要一块 shared memory”，真正决定它落在哪、多大、何时加 barrier 的，是 Allocation 与 Membar 两套分析。

## 目标

读完本章，你应该能回答：

- `ttg.local_alloc` 在 TTGIR 里只是一个 memdesc，它什么时候变成 `allocation.offset` 和 `ttg.shared`？
- 两个 `local_alloc` 的生命周期不重叠时，为什么可以共用同一段 shared memory？
- 为什么 offset 要对齐？swizzle 改变的是地址映射还是分配大小？
- `MembarAnalysis` 根据什么决定在 `local_load` 前插入 `ttg.barrier local`？
- 为什么多一个 `bar.sync` 可能让整个 matmul 慢一大截？
- 在 T4（sm_75）上，shared memory 大小如何限制 occupancy？

本章主线是两条：

```text
TTGIR local_alloc
  -> AllocateSharedMemory pass
  -> allocation.offset / allocation.size / ttg.shared
  -> LLVM global_smem + GEP
  -> PTX .shared
  -> ptxas 的 smem 用量

TTGIR local_load/local_store
  -> MembarAnalysis 收集 read/write slice
  -> 插入 ttg.barrier local
  -> gpu.barrier -> NVVM barrier -> SASS BAR.SYNC
```

## 环境/版本

| 项目 | 值 | 证据 |
|---|---|---|
| Triton 源码 | `/home/ubuntu/aicomp/triton` | 本地树 |
| commit | `a972119e92a27b445cd160017ea461e56148590c` | `git log -1` |
| 版本 | 3.9.0 | `python/triton/__init__.py:2` |
| GPU | Tesla T4，compute capability 7.5 | `nvidia-smi` |
| 驱动 | 535.216.01 | `nvidia-smi` |
| CUDA / ptxas | 12.4，`V12.4.131` | `ptxas --version` |
| shared memory / SM | 65536 B | **实测**，`cudaDeviceGetAttribute(81)` |
| shared memory / block（默认） | 49152 B | **实测**，`cudaDeviceGetAttribute(8)` |
| shared memory / block（opt-in） | 65536 B | **实测**，`cudaDeviceGetAttribute(97)` |

**实测**：上面的 shared memory 数字来自本机 `libcudart.so.12` 的 `cudaDeviceGetAttribute`，不是照抄规格表。T4 每个 SM 只有 64 KiB shared memory，默认每个 block 只能用 48 KiB；要用满 64 KiB 必须走 dynamic shared memory 的 opt-in 路径。

**guided**：TMA、`cp.async`、Hopper 的 cluster shared memory、Blackwell 的 TMEM 在 sm_75 上不可用。本章会读这些路径的源码，但不会假装在 T4 上跑过。

## 为什么需要这个

在 TTGIR 里你看到的是：

```mlir
%a = ttg.local_alloc %arg : (tensor<128x32xf16, #blocked>) -> !ttg.memdesc<128x32xf16, #shared, #ttg.shared_memory>
%b = ttg.local_load %a : !ttg.memdesc<128x32xf16, #shared, #ttg.shared_memory> -> tensor<128x32xf16, #blocked>
```

这里没有地址。`%a` 是一个 SSA 值，不是 `%smem + 8192`。地址是后面由 `AllocateSharedMemory` pass 算出来的，写在 op 的属性上：

```mlir
%a = ttg.local_alloc {allocation.offset = 0 : i32} : ...
```

如果你只看 TTGIR，会误以为每个 `local_alloc` 都独立占一块新内存；实际上 Allocation 会把生命周期不重叠的 buffer 叠在一起。你也会误以为 barrier 是“写 shared memory 就自动有”，实际上 Membar 只在检测到跨线程的 RAW/WAR/WAW 时才插入。

这两件事直接决定：

1. **正确性**：少一个 barrier，别的 warp 可能读到旧数据。
2. **性能**：多一个 barrier，所有 warp 被迫在同一个点会合，流水线重叠被打断。
3. **occupancy**：shared memory 超了，CTA 数量下降，延迟隐藏变差。

## 先建立直觉

先记住三个“不是”：

- `allocation.offset` 是**字节偏移**，不是 buffer id，也不是元素下标。
- `ttg.shared` 是**整个 CTA 的 shared memory 字节数**，不是某个 buffer 的大小。
- `ttg.barrier local` 默认是**CTA 内所有线程的会合点**，不是“刷新 cache”的指令；`ttg.barrier warp local` 才只同步一个 warp。

一颗典型 matmul 的 shared memory 生命周期可以画成这样：

```text
时间 ->
stage 0:  A0[0,8192)   B0[8192,16384)
stage 1:  A1[0,8192)   B1[8192,16384)   # 复用 stage 0 的地址
          ^ A0 已经没人读，Allocation 可以把 A1 放在同一 offset
```

barrier 则像这样：

```text
warp 0:  st.shared A0 ----+
warp 1:  st.shared A0 ----+--> BAR.SYNC --> ld.shared A0
warp 2:  st.shared B0 ----+
warp 3:  st.shared B0 ----+
```

没有 `BAR.SYNC`，warp 3 可能在 warp 0 写完之前就 `ld.shared`。

## 核心概念

### 1. memdesc 与 `local_alloc`

`ttg.local_alloc` 的结果类型是 `!ttg.memdesc<shape, encoding, memory_space, mutable>`。它表示“一块 shared memory 的视图”，不是寄存器里的 tensor。

源码依据：`include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:240-279`。`isSharedMemoryAlloc()` 检查 memory space 是不是 `SharedMemorySpaceAttr`；`getAlignmentOrDefault()` 在 `lib/Dialect/TritonGPU/IR/Ops.cpp:897-906`，优先用 op 的 `alignment`，否则用 shared encoding 的 alignment，最后回退到 16 字节。

### 2. Buffer：size、alignment、offset

`Allocation::BufferT` 有三个关键字段：`size`、`alignment`、`offset`。`setOffsetAligned` 会把新 offset 向上取整到 alignment：

```cpp
size_t setOffsetAligned(size_t newOffset) {
  return offset = llvm::alignTo(newOffset, alignment);
}
```

源码依据：`include/triton/Analysis/Allocation.h:203-214`。

`Allocation` 对外只暴露 `getOffset(bufferId)` 和 `getAllocatedSize(bufferId)`（`Allocation.h:104-112`）。buffer id 是分析内部的编号，不是地址。

### 3. ModuleAllocation：按函数、按调用图分配

`ModuleAllocation` 不是“给整个 module 一块大内存”。它按 call graph 的后序遍历，对每个 function 跑一次 `Allocation::run`，源码在 `include/triton/Analysis/Allocation.h:278-322`。

`getSharedMemorySize()` 取所有 root function 的最大值（`Allocation.h:299-306`），因为一次 launch 只会进一个入口。

### 4. 显式 buffer、scratch buffer、virtual buffer

Allocation 区分三类 buffer：

| 类型 | 来源 | 例子 |
|---|---|---|
| Explicit | `ttg.local_alloc` 的结果 | 手写 shared tile、pipeline buffer |
| Scratch | 某个 op 内部的临时 shared memory | `convert_layout`、`reduce`、`scan`、`gather`、atomic 结果广播 |
| Virtual | call 的 callee frame | `tt.call` 的 shared memory 参数 |

源码依据：`lib/Analysis/Allocation.cpp:236-284`（explicit）、`103-158`（scratch size）、`301-332`（scratch alignment 128）。

### 5. liveness 与 interference

Allocation 先算每个 buffer 的 liveness interval，再建 interference graph：两个 buffer 的 interval 有交集，就连一条边。然后做 graph coloring，同色的 buffer 可以共享 offset。

源码依据：`lib/Analysis/Allocation.cpp:538-568`。注释里写得很清楚：

```text
// Sort buffers by size in descending order to reduce the fragmentation
// on big buffers caused by smaller buffers.
```

`calculateStarts` 先按“最低可用 offset”放，`allocate` 再用 first-fit coloring 修正冲突（`Allocation.cpp:598-716`、`780-837`）。

### 6. 对齐与 swizzle 不是一回事

- **alignment** 决定 buffer 的起始 offset 必须是 16、128 等倍数。
- **swizzle** 决定 buffer 内部元素到 bank 的映射，用来避免 bank conflict，并满足 MMA/TMA 的布局要求。

swizzle 不改变 `getAllocationElems` 算出来的元素数，除非 encoding 带 padding。`getExplicitValueSize` 先取 `getAllocationElems`，有 `getPaddedEncoding` 时再取 `getPaddedSize`，然后乘 bitwidth 除以 8；源码在 `lib/Analysis/Allocation.cpp:274-283`。

所以一个 `128x32xf16` 的 swizzled tile 仍然是 `128*32*2 = 8192` 字节；swizzle 只改变 `vec/perPhase/maxPhase` 对应的地址异或规则。

### 7. barrier 的三种依赖

Membar 要防的是跨线程的：

| 依赖 | 含义 | 例子 |
|---|---|---|
| RAW | 写后读 | warp A `local_store`，warp B `local_load` |
| WAR | 读后写 | warp A `local_load` 还没读完，warp B 要覆盖同一块 |
| WAW | 写后写 | 两个 warp 先后写同一块，后写不能早于先写完成 |

同一个线程内的访问天然有序，不需要 barrier；Membar 的 `canSkip` 会利用 `haveSameThreadSyncIssuer` 跳过同 issuer 的 pair（`lib/Analysis/Membar.cpp:360-370`）。

## 底层机制

### 1. Pass 顺序：先分配，再插 barrier

NVIDIA 后端的 `make_llir` 里，顺序是：

```python
nvidia.passes.ttgpuir.add_allocate_shared_memory_nv(pm, capability, ptx_version)
nvidia.passes.ttnvgpuir.add_allocate_tensor_memory(pm)
...
nvidia.passes.ttgpuir.add_membar(pm, capability, ptx_version)
```

源码依据：`third_party/nvidia/backend/compiler.py:442-447`。

这个顺序不是随意的：Membar 需要知道每个 buffer 的 `allocation.offset` 和 interval，才能判断两个访问是否真的落在同一段 shared memory。没有 Allocation，Membar 只能保守地给所有 shared 访问加 barrier。

### 2. `AllocateSharedMemory` 只做两件事

核心 pass 非常短：

```cpp
void runOnOperation() override {
  ModuleOp mod = getOperation();
  ModuleAllocation allocation(mod);
  mlir::triton::gpu::attachAllocationSizeAndOffsetAttr(mod, allocation);
}
```

源码依据：`lib/Conversion/TritonGPUToLLVM/AllocateSharedMemory.cpp:20-25`。

`attachAllocationSizeAndOffsetAttr` 遍历每个 function 的每个 op：

- 如果 op 有 scratch buffer id，写 `allocation.offset` 和 `allocation.size`；
- 如果 op 只有一个 result 且该 result 有 explicit buffer id，写 `allocation.offset`；
- 如果是 partitioned tensor，写一个 offset 数组；
- 最后给 module 写 `ttg.shared`。

源码依据：`lib/Conversion/TritonGPUToLLVM/AllocateSharedMemoryUtility.cpp:11-54`。

### 3. 一个具体的 offset/size 例子

先看源码树里已有的 FileCheck 期望，避免凭空编造。`test/Conversion/allocate_shared_memory.mlir:43-52` 里 `128xi32` 是 512 字节，callee 的 scratch 从 512 开始，所以总大小至少 1024 字节。再看一个更大的真实 fixture：

```mlir
// test/TritonNvidiaGPU/test_tensor_memory_allocation.mlir:368-370
%0 = ttg.local_alloc {allocation.offset = 196880 : i32} : () -> !ttg.memdesc<2x1xi64, #shared, #smem, mutable>
%2 = ttg.local_alloc {allocation.offset = 0 : i32} : () -> !ttg.memdesc<2x128x128xbf16, #shared1, #smem, mutable>
```

`2x128x128xbf16` 是 `2*128*128*2 = 65536` 字节；barrier 的 `2x1xi64` 是 16 字节，却被放在 196880。这说明 offset 不是按声明顺序紧凑排列的，而是由 liveness、alignment、partition 和 graph coloring 共同决定的。**源码依据**：该测试文件的 CHECK 行；具体分配逻辑在 `Allocation.cpp:598-837`。

一个**计算示例**（不是某次 dump）：非流水线 matmul 的 A tile `128x32xf16` 和 B tile `32x128xf16` 都是 8192 字节，生命周期重叠，所以 A 在 0、B 在 8192，总 shared 至少 16384 字节。若 `num_stages=3`，三份 A/B buffer 可能把总量推到 `3*(8192+8192)=49152` 字节，刚好接近 T4 默认的 48 KiB 上限。

### 4. 放置算法：先大后小，再做图着色

`computeOffsets` 先按 size 降序排序：

```cpp
llvm::stable_sort(buffers, [&](BufferT *A, BufferT *B) { return A->size > B->size; });
calculateStarts(buffers);
```

源码依据：`lib/Analysis/Allocation.cpp:538-552`。

`calculateStarts` 维护一个 `(offset, liveness range)` 的 multimap，每次取最低 offset 的 slot，挑一个 liveness 不冲突、且 partition 不冲突的 buffer，`setOffsetAligned` 后把剩余空间重新插回 map（`Allocation.cpp:598-716`）。

`allocate` 再做 first-fit coloring：对每个 buffer，取所有 interfering buffer 的 `offset + size` 最大值；partition neighbor 则跳到下一个 partition 边界；非 0 色才 `setOffsetAligned`，最后用 `offset + size` 更新总大小。源码在 `lib/Analysis/Allocation.cpp:814-837`。

注意 `setOffsetAligned` 会在最后一步再次对齐；所以你在 TTGIR 里看到的 offset 不一定是 `y->offset + y->size` 的精确值，而是对齐后的值。

### 5. offset 如何变成 LLVM 地址

`LocalAllocOpConversion` 拿到 `allocation.offset`，构造 `SharedMemoryObject`：

```cpp
SmallVector<Value> smemBases = LLVM::getSharedMemoryBases(
    loc, rewriter, targetInfo, op.getOperation());
...
auto smemObj = SharedMemoryObject(smemBases, llvmElemTy,
                                  memDescTy.getRank(), loc, rewriter);
```

源码依据：`lib/Conversion/TritonGPUToLLVM/MemoryOpToLLVM.cpp:127-157`。

`getSharedMemoryBase` 做的是：

```cpp
size_t offset = cast<IntegerAttr>(offsetAttr).getValue().getZExtValue();
Value offVal = b.i32_val(offset);
Value base = b.gep(ptrTy, i8_ty, LLVM::getStackPointer(rewriter, func), offVal);
```

源码依据：`lib/Conversion/TritonGPUToLLVM/Utility.cpp:1781-1801`。

`getStackPointer` 对 kernel 返回 `global_smem` 的地址；对 device function 返回额外的 shared memory 参数：

```cpp
if (!isKernel(funcOp)) {
  return funcOp.getArgument(funcOp.getNumArguments() + kSharedMemoryOffset);
}
auto globalBase = dyn_cast<LLVM::GlobalOp>(mod.lookupSymbol("global_smem"));
```

源码依据：`lib/Conversion/TritonGPUToLLVM/Utility.cpp:1680-1689`。

`global_smem` 本身在 NVIDIA lowering 里创建：

```cpp
LLVM::Linkage::External, "global_smem", /*value=*/Attribute(),
```

源码依据：`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TritonGPUToLLVM.cpp:113`。

所以 LLIR 里会看到：

```llvm
llvm.mlir.global external @global_smem() {addr_space = 3 : i32, alignment = 16 : i64} : !llvm.array<0 x i8>
...
%base = llvm.mlir.addressof @global_smem : !llvm.ptr<3>
%p = llvm.getelementptr %base[%offset] : (!llvm.ptr<3>, i32) -> !llvm.ptr<3>
```

**源码依据**：`test/Conversion/tritongpu_to_llvm.mlir:1031`、`1606-1613` 的 FileCheck 行；SSA 名字会因版本和 CSE 变化。

### 6. barrier 插入：从 slice 冲突到 `ttg.barrier local`

Membar 对每个 op 调 `getMemoryAccesses`，只保留 memory space 是 shared 的访问，然后按 `AllocationSlice` 把 op 记进 `syncWriteSlices` 或 `syncReadSlices`。源码在 `lib/Analysis/Membar.cpp:649-663`。

scratch buffer 被视为“先写后读”的内部序列：

```cpp
curBlockInfo.syncWriteSlices[*scratchSlice].insert(op);
syncIfNeeded(op, curBlockInfo, membarInfo, builder, cluster);
...
curBlockInfo.syncReadSlices[*scratchSlice].insert(op);
```

源码依据：`lib/Analysis/Membar.cpp:669-709`。

`syncIfNeeded` 判断 pending 状态和当前 effects 是否有交集；有就插 barrier：

```cpp
if (!requiresThreadSync(pending, effects) &&
    !pending.isIntersected(effects, canSkip, &allocation, sliceFilter))
  return nullptr;
builder->setInsertionPoint(op);
Operation *barrier = insertBarrier(op, builder, cluster);
membarInfo->sync();
```

源码依据：`lib/Analysis/Membar.cpp:354-381`。

`insertBarrier` 生成的是：

```cpp
return triton::gpu::BarrierOp::create(*builder, op->getLoc(),
                                      triton::gpu::AddrSpace::Local);
```

源码依据：`lib/Analysis/Membar.cpp:383-390`。

### 7. 测试里的 barrier 位置

`test/Analysis/test-membar.mlir` 是最直接的证据。RAW：

```mlir
// test/Analysis/test-membar.mlir:51-54
%2 = ttg.local_alloc %1 : ... 
// CHECK: ttg.barrier local
// CHECK-NEXT: ttg.local_load
%3 = ttg.local_load %2 : ...
```

WAR 在 `test-membar.mlir:64-71`：`local_alloc` 后先 barrier 再 `local_load`；`local_load` 之后又有一个 barrier，然后才是下一次 `local_alloc`，防止新写覆盖还没读完的旧数据。

`war_single_block_local_store` 还展示了 `local_load` 之后、`local_store` 之前的 barrier（`test-membar.mlir:81-88`）。

这些 CHECK 行是测试期望，不是我在 T4 上跑出来的输出；但它们精确描述了 Membar 的行为，**源码依据**成立。

### 8. barrier 如何落到 SASS

`ttg.barrier local` 先由 `BarrierOpConversion` 处理：warp barrier 走 `targetInfo.warpSync`，CTA barrier 替换成 `gpu.barrier`；源码在 `lib/Conversion/TritonGPUToLLVM/MemoryOpToLLVM.cpp:342-352`。

之后 `gpu.barrier` 在 NVVM lowering 里变成 `nvvm.barrier`，最终 PTX 是 `bar.sync 0`，SASS 是 `BAR.SYNC 0x0`。**实测**：我用一段最小 PTX 跑 `ptxas -arch=sm_75`，再用 `cuobjdump -sass` 看到：

```text
/*0050*/  BAR.SYNC 0x0 ;
/*0070*/  STS [RZ], R0 ;
/*0080*/  BAR.SYNC 0x0 ;
/*0090*/  LDS.U R2, [RZ] ;
```

这段 PTX 是手写的最小样例，不是 Triton 生成；它只用来证明 `bar.sync` 到 SASS 的映射。

### 9. shared memory 大小与 occupancy

T4 的约束（**实测**）：

- 每 SM 65536 B shared memory；
- 每 block 默认 49152 B；
- 每 block opt-in 上限 65536 B；
- 每 SM 65536 个寄存器。

一个 CTA 能否驻留，取决于 shared memory 和寄存器两边：

```text
max_ctas_by_smem = floor(65536 / shared_per_cta)
max_ctas_by_regs = floor(65536 / (threads_per_cta * regs_per_thread))
resident_ctas = min(max_ctas_by_smem, max_ctas_by_regs, 32)
```

举例（**计算**，不是实测 occupancy）：

| shared/CTA | regs/thread | threads/CTA | smem 上限 | regs 上限 | 实际驻留 |
|---:|---:|---:|---:|---:|---:|
| 16 KiB | 32 | 128 | 4 | 16 | 4 |
| 32 KiB | 32 | 128 | 2 | 16 | 2 |
| 48 KiB | 32 | 128 | 1 | 16 | 1 |
| 64 KiB | 32 | 128 | 1 | 16 | 1 |

这就是为什么 pipeline 的 `num_stages` 不是越大越好：每多一级 buffer，shared memory 线性增长，驻留 CTA 数可能从 4 掉到 2，延迟隐藏变差。

## 进一步用法/重要变体

### 1. 显式 alignment

`ttg.local_alloc` 可以带 `alignment` 属性：

```mlir
%a = ttg.local_alloc {alignment = 128 : i32} : () -> !ttg.memdesc<128x32xf16, #shared, #ttg.shared_memory, mutable>
```

源码依据：`TritonGPUOps.td:252-264`。`getAlignmentOrDefault` 优先用这个属性（`Ops.cpp:897-906`）。

### 2. scratch buffer 的 128 字节对齐

`getScratchValueSize` 里：

```cpp
constexpr size_t scratchAlignment = 128;
...
maybeAddScratchBuffer<BufferT::BufferKind::Scratch>(op, bytes, scratchAlignment);
```

源码依据：`lib/Analysis/Allocation.cpp:300-332`。

`convert_layout`、`reduce`、`scan`、`gather` 的临时 buffer 都走这条路径。它们没有 `local_alloc` 的显式 alignment，统一按 128 字节对齐。

### 3. partitioned shared memory（guided）

`PartitionedSharedEncodingAttr` 会为每个 partition 建一个 buffer，并把它们标记为 neighbor，强制放在不同的物理 partition：

```cpp
allocation->addPartitionBuffers(alloc, numPartitions, partitionBufferSize, alignment);
```

源码依据：`lib/Analysis/Allocation.cpp:247-271`、`859-880`。

这条路径主要用于 Hopper/Blackwell 的多 CTA 或 warp-specialized 场景；T4 上不要照搬。

### 4. TMA / async copy（guided）

`AsyncCopyGlobalToLocal`、`TMA` 会引入 mbarrier 和 async proxy，Membar 需要区分 `ttng::WaitBarrierOp`、`FenceAsyncSharedOp` 等：

```cpp
if (isa<ttng::WaitBarrierOp>(op))
  return {ThreadSyncKind::MBarrierWait};
if (isa<ttng::FenceAsyncSharedOp>(op))
  return {ThreadSyncKind::Completion};
```

源码依据：`lib/Analysis/Membar.cpp:165-208`。

这些在 sm_75 上不可用；读源码时把它们当成“另一套同步协议”，不要和 `bar.sync` 混为一谈。

## 对比与取舍

| 选择 | 优点 | 缺点 | 什么时候用 |
|---|---|---|---|
| `local_alloc` 显式 buffer | 生命周期清楚，可复用 | 需要自己管 layout/barrier | 手写 pipeline、Gluon |
| `convert_layout` scratch | 编译器自动分配 | 有隐式 barrier，难精确控制 | 普通 Triton layout 转换 |
| `swizzled_shared` | 避免 bank conflict，满足 MMA | 地址映射不直观 | matmul tile |
| `padded_shared` | 用 padding 破 bank conflict | 多占空间 | 非 MMA 的通用 tile |
| `ttg.barrier local` | 语义清楚，CTA 级 | 会合所有 warp | 跨 warp 共享数据 |
| mbarrier（Hopper+） | 可异步、可分阶段 | 协议复杂 | TMA/warp specialization |

## 踩雷集锦

1. **以为 offset 按声明顺序递增。**
   Allocation 会按 size 降序、liveness、alignment、partition 重新放置；`test_tensor_memory_allocation.mlir:368-370` 里 16 字节的 barrier 在 196880，而 64 KiB 的 tile 在 0。不要用源码顺序推断地址。

2. **`local_alloc` 之后直接 `local_load`，以为同线程写就同线程读。**
   `local_alloc` 的初始化可能由不同 warp 完成；跨 warp 读必须等 barrier。`test-membar.mlir:51-54` 的 RAW 例子就是 `local_alloc` -> barrier -> `local_load`。

3. **在循环里无脑加 barrier。**
   每个 `bar.sync` 都让所有 warp 会合；在 pipeline 内层循环里，多一个 barrier 会把 load/compute/store 的重叠打断。先看 Membar 是否已经插了，再用 `ttgir` 确认，不要手写“保险 barrier”。

4. **把 alignment 当成 swizzle。**
   `alignment=128` 只保证起始地址 128 字节对齐；bank conflict 由 `vec/perPhase/maxPhase/order` 决定。两者解决的是不同问题。

5. **在 T4 上按 64 KiB 默认值算 occupancy。**
   T4 默认每 block 只有 48 KiB；要 64 KiB 必须走 dynamic shared memory opt-in。`driver.py:282-286` 也提到 Ampere+ 按 128 字节单位分配并扣除 reserved shared memory。

6. **把 `ttg.shared` 当成动态 shared memory 的 launch 参数。**
   `ttg.shared` 是编译期算出的 CTA shared 用量，写进 metadata；launch 时 driver 用它设置 dynamic shared memory。它不是某个 `local_alloc` 的 size。

## 进阶

- 读 `Allocation.cpp:598-716` 的 `calculateStarts`，自己画一个 3 个 buffer 的 liveness 区间，手算 offset 和总大小。
- 读 `Membar.cpp:335-352` 的 `requiresThreadSync`，理解“线程级完成”与“地址级冲突”为什么要分开判断。
- 读 `Membar.cpp:415-467` 的 `getLocalBarrierStages`，看显式 barrier、atomic、warp specialization 的 before/after stages 差异。
- 读 `Allocation.cpp:571-595` 的 partition eligibility，理解为什么 partitioned buffer 不能只按 liveness 叠放。
- 在 T4 上用不同 `num_stages` 编译同一个 matmul，比较 `ttg.shared` 和 `ptxas -v` 的 smem 数字；不要运行 Hopper 特性。

## 动手练习

1. **手算 offset**：三个 buffer，大小分别是 8192、4096、8192 字节，liveness 分别是 `[0,3)`、`[1,4)`、`[3,6)`，alignment 都是 16。画出 interference graph，给出一种合法 offset 和总大小。
2. **读 TTGIR**：用 `TRITON_KERNEL_DUMP=1` 导出一颗 matmul 的 TTGIR，找到所有 `ttg.local_alloc` 的 `allocation.offset`，解释为什么某个 offset 不是前一个 buffer 的结尾。
3. **找 barrier**：在同一份 TTGIR 里数 `ttg.barrier local` 的数量，对照 `test-membar.mlir` 的 RAW/WAR 规则，说明每一个 barrier 防的是哪种依赖。
4. **改 alignment**：给一个 `local_alloc` 加 `alignment = 128`，重新 dump，观察 offset 是否变化；如果没变，解释为什么。
5. **算 occupancy**：假设 `ttg.shared=32768`、`num_warps=4`、每线程 40 个寄存器，用 T4 的 65536 B / 65536 regs 算驻留 CTA 数。

## 本章重点

- `local_alloc` 是 memdesc；地址由 `AllocateSharedMemory` 写成 `allocation.offset`，总大小写成 `ttg.shared`。
- Allocation 用 liveness + interference graph + first-fit coloring 复用 shared memory；offset 受 size 排序、alignment、partition 影响。
- 默认对齐 16 字节，scratch 对齐 128 字节；swizzle 改地址映射，不改元素数（除非 padding）。
- Membar 收集 shared read/write slice，检测 RAW/WAR/WAW 后插入 `ttg.barrier local`。
- `ttg.barrier local` 最终变成 `bar.sync 0` / `BAR.SYNC`；多一个 barrier 会会合所有 warp，可能显著变慢。
- T4 每 SM 64 KiB shared memory，默认每 block 48 KiB；shared 用量直接限制 occupancy。

## 自我检核

1. `ttg.local_alloc` 的结果类型里，哪一部分表示“这是 shared memory”？
2. 两个 buffer 的 liveness 不重叠，Allocation 一定会让它们共用 offset 吗？还有什么约束？
3. 为什么 Membar 要在 `local_load` 前插 barrier，而同一个线程内的 load/store 不需要？
4. `alignment` 和 `swizzle` 分别解决什么问题？各自影响分配大小吗？
5. 在 T4 上，一个 CTA 用 32 KiB shared memory、4 个 warp、每线程 40 个寄存器，最多能驻留几个 CTA？

## 延伸阅读

1. **`lib/Analysis/Allocation.cpp:538-837`**
   - 读哪里：`computeOffsets`、`calculateStarts`、`allocate`。
   - 学什么：liveness interval、interference graph、first-fit coloring、alignment 如何共同决定 offset。
   - 与本章关系：这是 `allocation.offset` 和 `ttg.shared` 的来源。

2. **`lib/Analysis/Membar.cpp:335-467`、`614-719`**
   - 读哪里：`requiresThreadSync`、`syncIfNeeded`、`getLocalBarrierStages`、`updateMemoryEffects`。
   - 学什么：RAW/WAR/WAW 如何映射到 slice 冲突，显式 barrier/atomic/warp-specialize 的 before/after stages。
   - 与本章关系：解释“barrier 为什么插在这里”。

3. **`lib/Conversion/TritonGPUToLLVM/AllocateSharedMemoryUtility.cpp:11-54`**
   - 读哪里：`attachAllocationSizeAndOffsetAttr`。
   - 学什么：offset/size 属性如何写到 op 和 module 上，partitioned tensor 的数组形式。
   - 与本章关系：连接 Allocation 分析与 LLVM lowering。

4. **`test/Analysis/test-membar.mlir:45-90`**
   - 读哪里：`raw_single_block`、`war_single_block`、`war_single_block_local_store`。
   - 学什么：Membar 的 CHECK 期望，RAW/WAR 的 barrier 位置。
   - 与本章关系：用测试固定行为，避免只靠猜。

5. **`third_party/nvidia/backend/compiler.py:442-447`、`python/triton/backends/compiler.py:48-59`**
   - 读哪里：`add_allocate_shared_memory_nv`、`add_membar` 的顺序，`add_stages` 契约。
   - 学什么：为什么先分配再插 barrier，stage 如何串起来。
   - 与本章关系：把本章两个分析放回完整编译流水线。

## 下一章

下一章沿着 `llir -> ptx -> cubin` 继续往下：MLIR LLVM dialect 如何变成 LLVM IR，NVPTX 如何生成 PTX，`ptxas` 如何报告寄存器和 shared memory，`cuobjdump`/`nvdisasm` 又如何把 cubin 还原成 SASS。

→ [Ch 28 LLVM IR、NVPTX、PTX、cubin、SASS](./28-llvm-ir-nvptx-ptx-cubin-sass.md)
