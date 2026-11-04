# Ch 15 — 内存层次与异步原语

> 共享内存不是“更快的 global”，它是一块需要你自己证明可见性的协作缓冲区。

## 目标

读完本章，你应该能回答：

- `ttg.local_alloc` 返回的 `memdesc` 到底描述了什么？它和 `tt.load` 得到的 tensor 有什么区别？
- 一个 matmul 的 TTGIR 里，`async_copy_global_to_local`、`local_load`、`tt.dot` 之间为什么必须夹着 `async_wait` 或 `wait_barrier`？
- TMA descriptor 的 `shape`、`strides`、`block shape` 分别对应 global 张量的什么信息？为什么 block shape 的最后一维会被 swizzle 字节数“改写”？
- mbarrier 的 phase 为什么在循环里要 `xor 1`？`arrive`、`expect_tx`、`wait` 三者的计数关系是什么？
- 为什么 shared memory 需要 swizzle？不加 swizzle 会慢在哪？
- T4（sm_75）上哪些原语不可用？编译器在哪一步把 descriptor 退回成普通指针？

本章的主线是“数据放在哪、谁负责搬、搬完谁负责证明可见”。Triton 把这些答案分别编码进 `memdesc`、async token、mbarrier 和 fence 里。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5，15360 MiB |
| 驱动 | 535.216.01 |
| CUDA | 12.4，`V12.4.131` |
| 系统 Python | 3.10.12 |

**源码依据**：

- `include/triton/Dialect/TritonGPU/IR/TritonGPUTypes.td:23-41`：`MemDescType` 的参数是 `shape`、`elementType`、`encoding`、`memorySpace`、`mutableMemory`、`allocShape`。
- `include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:178-237`：`ttg.async_copy_global_to_local` 的 operand、token 和 `contiguity` 属性。
- `include/triton/Dialect/TritonNvidiaGPU/IR/TritonNvidiaGPUOps.td:287-430`：`init_barrier`、`barrier_expect`、`wait_barrier`、`arrive_barrier`。
- `include/triton/Dialect/TritonNvidiaGPU/IR/TritonNvidiaGPUOps.td:523-750`：TMA load/store/reduce/gather/scatter 和 store wait。
- `lib/Dialect/TritonNvidiaGPU/Transforms/TMALowering.cpp:27-66`：一次 TMA load 的完整 lowering 配方。
- `lib/Dialect/TritonNvidiaGPU/Transforms/TMAUtilities.cpp:139-225`：descriptor 的 `box_dim`、`global_dim`、`global_stride` 如何生成。

**guided**：T4 是 `sm_75`。`cp.async` 需要 sm_80+（`TritonGPUOps.td:223-231` 的 `getEligibleLoadByteWidth` 只在 `computeCapability >= 80` 返回 `{4,8,16}`），TMA 需要 sm_90+（`third_party/nvidia/backend/compiler.py:368-369` 只在 `capability // 10 >= 9` 时加 `add_tma_lowering`）。本章的 TTGIR/TMA 片段是按源码和测试文件推导的预期形态，不是在本机 T4 上跑出来的。TMEM/TCGen5 需要 sm_100+，同样标 `guided`。

## 为什么需要这个

如果只看 `tt.load` / `tt.store`，你会以为 Triton 的每个值都在寄存器里。真实情况是：

- `tt.load` 把 global 数据拉进**分布式 tensor**（每个线程持有自己那份寄存器）；
- `ttg.local_alloc` 在 shared memory 里开一块缓冲区，返回一个**描述符**；
- `ttg.async_copy_global_to_local` 把 global 直接搬到 shared，不经过寄存器；
- `ttg.local_load` 再把 shared 的某个 view 读成分布式 tensor，供 `tt.dot` 使用；
- TMA 把“搬一块 tile”变成一条由单个 warp 发出的 bulk copy，完成信号通过 mbarrier 的 transaction count 传递。

这条链上任何一步的“完成”都不是自动的。`async_copy` 返回 token，但 token 只表示“这批 copy 可以被等待”，不表示数据已经对别的 warp 可见；`async_wait` 只等 copy，不做 CTA 同步；`wait_barrier` 等的是 mbarrier 的 phase，不是普通的 `__syncthreads()`。把这些混在一起，就会写出“本机偶尔对、换架构就错”的 kernel。

## 先建立直觉

先看一张时间线。一个双缓冲的 matmul 循环大致是：

```text
iteration i:
  producer:  async_copy(global -> smem[buf 0])  ---+
                                                   |
  consumer:  wait(async group)                      |
             local_load(smem[buf 0]) -> regs        |
             tt.dot(regs, ...)                       |
             async_copy(global -> smem[buf 1])  -----+--> 下一轮
```

换成 TMA 后，producer 不是每个线程发 `cp.async`，而是一个 warp 发一条：

```text
thread 0/warp 0:
  mbarrier.arrive.expect_tx(bar, bytes)
  cp.async.bulk.tensor.2d.shared::cta.global.mbarrier::complete_tx::bytes
  ...
consumer:
  mbarrier.try_wait.parity(bar, phase)
  local_load / tcgen05.mma
```

`expect_tx` 先告诉 barrier “我期望这么多字节”；TMA 每完成一块，就递减 transaction count；当 pending arrivals 和 transaction count 都归零，barrier 完成当前 phase。`wait` 用 phase 的奇偶性判断“我等的是哪一轮”，所以循环里 phase 必须翻转。

## 核心概念

### 1. global / shared / register 三层

| 层次 | 谁可见 | 典型用途 | Triton 里的表示 |
|---|---|---|---|
| global | 整个 grid | 输入输出、大 tensor | `tt.ptr`、`tt.tensor` |
| shared | 一个 CTA（cluster 可跨 CTA） | tile 复用、MMA operand、TMA 目标 | `!ttg.memdesc<..., #shared, #smem>` |
| register | 一个线程 | 元素计算、MMA accumulator | 分布式 `tensor<..., #blocked/#mma>` |
| TMEM | 一个 CTA（Blackwell） | MMAv5 accumulator | `!ttg.memdesc<..., #tmem>` |

shared 的容量是每 SM 几十到两百多 KB（依架构），所以它必须被复用；register 的容量更小，但延迟最低。Triton 的 layout 系统本质上是在决定“哪个元素放在哪个线程的哪个寄存器”，而 shared 的 encoding 决定“同一个 tile 在 shared 里怎么排”。

### 2. `memdesc` 不是 tensor

`MemDescType` 的六个参数（`TritonGPUTypes.td:34-41`）说明它描述的是“一块内存的视图”，而不是“一组值”：

- `shape`：逻辑形状；
- `elementType`：元素类型；
- `encoding`：shared/TMEM 的布局（swizzle、bank 排列等）；
- `memorySpace`：`#smem`、`#tmem` 等；
- `mutableMemory`：是否允许写；
- `allocShape`：底层分配的形状，允许 view 比 allocation 小。

所以 `local_alloc` 的结果可以传给 `local_load`、`local_store`、`memdesc_subview`、`memdesc_trans`，这些 op 都在操作“同一块内存的不同视图”。这也解释了为什么 `local_dealloc` 是可选的：编译器把 memdesc 的死亡点放在“后支配所有 uses 的第一个点”（`TritonGPUOps.td:290-297`）。

### 3. `local_alloc` / `local_load` / `local_store`

- `ttg.local_alloc`：在 shared 里分配，返回 memdesc。可带 `src` 初始化；不带 `src` 时结果必须是 mutable（`TritonGPUOps.td:240-280`）。
- `ttg.local_load`：memdesc → 分布式 tensor；可选 `token` 表示依赖哪个 async 组（`TritonGPUOps.td:445-466`）。
- `ttg.local_store`：分布式 tensor → memdesc（`TritonGPUOps.td:468-484`）。

一个最小的“写 shared 再读回”片段：

```mlir
// guided：形状和 encoding 仅作示意
%smem = ttg.local_alloc : () -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
ttg.local_store %vals, %smem : tensor<128x64xf16, #blocked> -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%out = ttg.local_load %smem : !ttg.memdesc<128x64xf16, #shared, #smem, mutable> -> tensor<128x64xf16, #blocked>
```

这段代码在语义上等价于“每个线程把自己那份写进 shared，再读回来”，但它没有插入任何 barrier。如果 `%vals` 的生产者和 `%out` 的消费者不是同一组线程，你必须自己保证可见性。Triton 的 pass 会在需要时插入 `ttg.local_barrier` / `ttng.wait_barrier`，但手写 TTGIR 时不要假设它一定存在。

### 4. async copy

`ttg.async_copy_global_to_local` 的语义是“把 global 指针 tensor 指向的数据异步复制到 memdesc”（`TritonGPUOps.td:194-209`）。关键点：

- 它返回 `!ttg.async.token`；
- 必须用 `ttg.async_commit_group` 把当前这批 copy 封成一个组，才能被 `ttg.async_wait` 等待；
- `ttg.async_wait {num = 0}` 等所有组完成，`num = N` 允许最多 N 个组未完成（`TritonGPUOps.td:135-157`）；
- 它**不做 CTA 同步**。op 的文档明确说：需要同步时另加 `ttg.local_barrier`（`TritonGPUOps.td:142-143`）。

一个典型的双缓冲片段（`guided`，形状简化自 `test/TritonGPU/loop-pipeline-cuda.mlir`）：

```mlir
%a_smem = ttg.local_alloc : () -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%tok0 = ttg.async_copy_global_to_local %a_ptrs, %a_smem
    : tensor<128x64x!tt.ptr<f16>, #blocked> -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%grp = ttg.async_commit_group tokens %tok0
%wait = ttg.async_wait %grp {num = 0 : i32}
%a = ttg.local_load %a_smem token %wait
    : !ttg.memdesc<128x64xf16, #shared, #smem, mutable> -> tensor<128x64xf16, #dot_op<...>>
```

### 5. TMA

TMA（Tensor Memory Accelerator）是 Hopper 起的硬件搬运单元。Triton 侧对应 `ttng.async_tma_copy_global_to_local`（`TritonNvidiaGPUOps.td:523-593`）：

```mlir
// guided：T4 不支持，仅示意语法
ttng.async_tma_copy_global_to_local %desc[%off_m, %off_k] %smem, %bar, %true
    : !tt.tensordesc<128x64xf16, #shared>, !ttg.memdesc<1xi64, #barrier, #smem, mutable>
      -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
```

它和 `async_copy` 的区别不是“更快一点”，而是执行模型不同：

- `async_copy` 是每个线程发 `cp.async`，按 4/8/16 字节搬运；
- TMA 是单个线程/warp 发一条 `cp.async.bulk.tensor.Nd...`，按 descriptor 的 box 搬一整块；
- TMA 的完成信号走 mbarrier 的 `complete_tx::bytes`，不是 async group token；
- TMA 需要 host/device 侧先创建 descriptor（`tt.make_tensor_descriptor` → `ttng.tensormap_create`）。

### 6. mbarrier 与 fence

`ttng.init_barrier` 在 shared 里初始化一个 mbarrier，`count` 是期望的 arrive 次数（`TritonNvidiaGPUOps.td:287-306`）。`ttng.barrier_expect` 追加“期望搬运的字节数”（`TritonNvidiaGPUOps.td:322-363`）。`ttng.arrive_barrier` 执行 arrive（`TritonNvidiaGPUOps.td:412-430`）。`ttng.wait_barrier` 阻塞到当前 phase 完成（`TritonNvidiaGPUOps.td:364-411`）。

fence 解决的是“代理之间的可见性”：

- `ttng.fence_async_shared`：generic proxy 写 shared 后，async proxy（TMA store）读 shared 前需要它；lowering 成 `NVVM::FenceProxy(async_shared, shared_cta/shared_cluster)`（`BarrierOpToLLVM.cpp:118-133`）。
- `ttng.fence_mbarrier_init_release_cluster`：cluster 内 mbarrier 初始化后的 release fence（`TritonNvidiaGPUOps.td:95-108`）。
- `ttng.tensormap_fenceproxy_acquire`：device 侧创建 tensormap 后，让 TMA 代理看到它；lowering 成 `fence.proxy.tensormap::generic.acquire.gpu`（`TMAToLLVM.cpp:93-102`）。

## 底层机制

### 1. 一个 matmul 的 TTGIR 骨架

下面是把 `test/TritonGPU/loop-pipeline-cuda.mlir` 的循环简化后的形态。它不是逐字输出，但 op 顺序和类型关系与源码一致（`guided`）：

```mlir
// guided：sm_80+ 的 pipelined matmul 骨架
%a_smem = ttg.local_alloc : () -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%b_smem = ttg.local_alloc : () -> !ttg.memdesc<64x64xf16, #shared1, #smem, mutable>

%tok_a = ttg.async_copy_global_to_local %a_ptrs, %a_smem
    : tensor<128x64x!tt.ptr<f16>, #blocked> -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%tok_b = ttg.async_copy_global_to_local %b_ptrs, %b_smem
    : tensor<64x64x!tt.ptr<f16>, #blocked1> -> !ttg.memdesc<64x64xf16, #shared1, #smem, mutable>
%grp = ttg.async_commit_group tokens %tok_a, %tok_b
%wait = ttg.async_wait %grp {num = 0 : i32}

%a = ttg.local_load %a_smem token %wait
    : !ttg.memdesc<128x64xf16, #shared, #smem, mutable> -> tensor<128x64xf16, #dot_op<...>>
%b = ttg.local_load %b_smem token %wait
    : !ttg.memdesc<64x64xf16, #shared1, #smem, mutable> -> tensor<64x64xf16, #dot_op<...>>
%acc = tt.dot %a, %b, %zero : tensor<128x64xf16, #dot_op<...>> * tensor<64x64xf16, #dot_op<...>> -> tensor<128x64xf32, #mma>
```

这段骨架里有三个“证明”：

1. `async_commit_group` 证明“这批 copy 可以被等待”；
2. `async_wait` 证明“copy 已完成”，但它只对发起 copy 的线程/组有意义；
3. `local_load` 的 `token` 把“我读的这块 shared 已经就绪”编码进 SSA，让调度器不能把读提到 wait 之前。

如果 `%a_smem` 被多个 warp 共享，而生产者不是所有 warp，你还需要一个 CTA 级 barrier。`async_wait` 不会替你做这件事（`TritonGPUOps.td:142-143`）。

### 2. `async_copy` 的 lowering

`AsyncCopyGlobalToLocalOpConversion` 在 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/LoadStoreOpToLLVM.cpp:950` 起。它先算每个线程最多能搬多少元素：

- `maxVec = getContiguity(src)`，再被 mask 的 alignment 限制（`1018-1027`）；
- 可以用 `contiguity` 属性提高上限（`1028-1029`）；
- 上限是 128 bit（`1030-1031`）；
- 每次 `cp.async` 只能是 4、8、16 字节，小于 4 字节直接报错（`1033-1039`）。

然后它发 `cp.async`（`1090-1117`）。注意 masked copy 的语义：lowering 不 predicate copy，而是把 `src-size` 设为 0，让 shared 目标写 0（`1100-1108`）。源码注释明确写着 “Always assume other = 0 for now”。所以如果你依赖 `other != 0`，在 async copy 路径上会得到 0，而不是你给的值。

`ttg.async_commit_group` / `ttg.async_wait` 的 lowering 分别对应 `cp.async.commit_group` 和 `cp.async.wait_group N`。`num` 是“允许未完成的组数”，不是“等待第 N 组”。

### 3. TMA descriptor 的 shape / strides / block shape

`tt.make_tensor_descriptor` 的 TTIR 形态是：

```mlir
// guided：TTIR 语法示意
%desc = tt.make_tensor_descriptor %base, [%M, %K], [%stride_m, %stride_k]
    : !tt.ptr<f16>, !tt.tensordesc<128x64xf16, #shared>
```

三个概念不要混：

| 概念 | 来自哪里 | 含义 |
|---|---|---|
| global shape | `tt.make_tensor_descriptor` 的 `shape` operand | 整个 global 张量每维多大 |
| global strides | `strides` operand | 每维跨多少**元素**；descriptor 创建时会乘 `elemSize` 变成字节（`TMAUtilities.cpp:202-204`） |
| block shape | `!tt.tensordesc<128x64xf16, ...>` 的类型参数 | 一次 TMA 搬的 tile 形状 |

`createTMADesc` 的生成顺序（`TMAUtilities.cpp:155-223`）：

1. `shapePerCTA = getShapePerCTA(encoding, descShape)`；
2. `blockShape = getTMABlockShape(...)`；
3. `boxDim` 先 push 连续维，再从 `rank-2` 倒着 push 其余维（`166-168`）；
4. `globalDim = reverse(shape)`（`188`）；
5. `globalStride` 去掉最后一维，再乘 `elemSize`（`189-204`）；
6. `elementStride` 全 1（`200`）；
7. `TensormapCreateOp` 接收 `box_dim`、`global_dim`、`global_stride`、`element_stride`、`elem_type`、`swizzle_mode`、`fill_mode`（`212-223`）。

block shape 不是简单等于 `shapePerCTA`。`getTMABlockShapeTiled`（`lib/Dialect/TritonGPU/IR/Dialect.cpp:4757-4791`）会：

- 每维 clamp 到 256（`4767-4770`）；
- 如果 `swizzleBytes != 0`，把连续维设成 `8 * swizzleBytes / elementBitWidth`（`4771-4787`）；
- fp4 padded 时再乘 2 / 除 2（`4764-4765`、`4788-4790`）。

举个具体例子：`f16`、`128B` swizzle、tile `256x128`。连续维期望 `8*128/16 = 64`，所以 block shape 变成 `256x64`；一次 TMA 搬 `256x64`，K 方向需要两条 message。这不是 bug，而是 TMA 的 box 必须和 swizzle 粒度对齐。

### 4. mbarrier 的 phase / arrive / wait

一个 TMA load 的 lowering 配方在 `TMALowering.cpp:27-66`，顺序非常固定：

```mlir
// guided：TMA load 的展开形态
%buf = ttg.local_alloc : () -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%bar = ttg.local_alloc : () -> !ttg.memdesc<1xi64, #barrier, #smem, mutable>
ttng.init_barrier %bar, 1 : !ttg.memdesc<1xi64, #barrier, #smem, mutable>
ttng.barrier_expect %bar, 16384, %true : !ttg.memdesc<1xi64, #barrier, #smem, mutable>
ttng.async_tma_copy_global_to_local %desc[%m, %k] %buf, %bar, %true
    : !tt.tensordesc<128x64xf16, #shared>, !ttg.memdesc<1xi64, #barrier, #smem, mutable>
      -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
%phase = arith.constant 0 : i32
ttng.wait_barrier %bar, %phase : !ttg.memdesc<1xi64, #barrier, #smem, mutable>
ttng.inval_barrier %bar : !ttg.memdesc<1xi64, #barrier, #smem, mutable>
```

计数关系：

- `init_barrier count = 1`：期望 1 次 arrive；
- `barrier_expect size = 16384`：期望 16384 字节的 transaction；
- TMA 完成时递减 transaction count；
- `wait_barrier` 等到“arrive 计数归零 + transaction 计数归零”，当前 phase 完成；
- 下一轮复用同一个 barrier 时，phase 从 0 变 1，再变 0。

phase 的翻转在 `HoistMBarrierLifecycle.cpp:52-56` 的注释里写得很清楚：

```text
scf.for ... iter_args(%phase = 0) {
  ttng.wait_barrier %bar, %phase
  %next_phase = arith.xori %phase, 1
  ...
}
```

如果你在循环里一直传 `0`，第二次 `wait` 会等一个已经完成的旧 phase，行为要么立刻返回（读到还没搬完的数据），要么挂死。两种都很糟。

### 5. 为什么 shared memory 需要 swizzle

shared memory 有 32 个 bank，每个 bank 4 字节。一个 warp 的 32 个线程如果同时访问同一列的不同行，地址会落到同一个 bank，形成 bank conflict，硬件要串行化。

`SwizzledSharedEncodingAttr` 的文档给了最直观的例子（`TritonGPUAttrDefs.td:42-51`）：

```text
#ttg.swizzled_shared<{vec=1, perPhase=1, maxPhase=4, order=[1,0]}>
[ 0,  1,  2,  3],  // xor with 0
[ 5,  4,  7,  6],  // xor with 1
[10, 11,  8,  9],  // xor with 2
[15, 14, 13, 12]   // xor with 3
```

第 `r` 行的元素按 `c ^ r` 重排。这样同一列的访问会散到不同 bank。`vec`、`perPhase`、`maxPhase` 控制异或的粒度和周期（`TritonGPUAttrDefs.td:53-107`）。

对 MMA 来说，swizzle 不只是“优化”，而是硬件要求：`NVMMASharedEncodingAttr` 表示 MMAv3/MMAv5 的 shared operand 布局（`TritonGPUAttrDefs.td:454-466`），它的 `swizzlingByteWidth` 必须和 TMA descriptor 的 `swizzle_mode` 对上（`TMAUtilities.cpp:48-80`：128B→3，64B→2，32B→1，0→0）。对不上，TMA 搬进来的数据和 MMA 期望的排布不一致，结果会错，而不只是慢。

### 6. fence 与可见性

shared memory 的写和读可能来自不同的“代理”：

- generic proxy：普通 `st.shared` / `ld.shared`；
- async proxy：`cp.async.bulk.tensor`（TMA）。

TMA store 的 lowering 是“先 `local_alloc %src` 把分布式 tensor 写进 shared，再 `fence_async_shared`，再发 TMA store，再 `async_tma_store_wait`”（`TMALowering.cpp:104-123`）。顺序不能换：

```mlir
%alloc = ttg.local_alloc %src : (tensor<128x64xf16, #blocked>) -> !ttg.memdesc<128x64xf16, #shared, #smem>
ttng.fence_async_shared {bCluster = false}
ttng.async_tma_copy_local_to_global %desc[%m, %k], %alloc
ttng.async_tma_store_wait {pendings = 0 : i32}
```

`fence_async_shared` 保证 generic proxy 对 shared 的写在 async proxy 看来是可见的。没有它，TMA 可能读到旧的 shared 内容。`async_tma_store_wait` 则保证 TMA 已经读完 shared，之后才能复用这块 buffer；如果只关心“读完 shared”而不关心 global 写完成，可以用 `read_only`（`TritonNvidiaGPUOps.td:739-750`）。

## 进一步用法/重要变体

### `local_alloc` 的两种形态

- 带 `src`：分配并初始化，结果可以是 immutable（只读）。TMA store lowering 就用这种形态（`TMALowering.cpp:114-117`）。
- 不带 `src`：只分配，结果必须 mutable，后续用 `local_store` 或 async copy 写入。

### 多缓冲与 subview

pipelining 会把一个 `local_alloc` 变成带 `numStages` 的数组，再用 `ttg.memdesc_subview` / `ttg.memdesc_index` 取某一 stage 的 view。你会在 pipelined TTGIR 里看到：

```mlir
// guided：多缓冲 view 的形态
%bufs = ttg.local_alloc : () -> !ttg.memdesc<3x128x64xf16, #shared, #smem, mutable>
%buf0 = ttg.memdesc_index %bufs[%c0] : !ttg.memdesc<3x128x64xf16, #shared, #smem, mutable> -> !ttg.memdesc<128x64xf16, #shared, #smem, mutable>
```

`memdesc_trans` 则用于“同一个 buffer 的转置视图”，比如把 `64x16` 看成 `16x64` 供另一个 dot operand 使用（`test/TritonGPU/loop-pipeline-cuda.mlir:53-54`）。

### TMA gather / scatter / reduce

TMA 不只有 tiled copy：

- `ttng.async_tma_gather`：按行索引 gather 多行（`TritonNvidiaGPUOps.td:658-700`）；
- `ttng.async_tma_scatter`：按行索引 scatter 多行（`701-738`）；
- `ttng.async_tma_reduce`：在 global 上做原子 reduce（`630-657`）；
- `ttng.async_tma_copy_global_to_local` 的 IM2COL 变体：卷积友好的 im2col 模式（`TritonNvidiaGPUOps.td:548-565`）。

这些都需要 sm_90+，T4 上不可用。

### TMEM（Blackwell）

`ttng.tmem_alloc`、`ttng.tmem_load`、`ttng.tmem_store`、`ttng.tmem_wait` 和 `ttng.tc_gen5_mma` 是 Blackwell 的 tensor memory 路径（`TritonNvidiaGPUOps.td:751-1207`）。TMEM 是 MMAv5 accumulator 的专用存储，容量和寻址方式都和 shared 不同：

- `tmem_alloc` 按列分配，返回 memdesc；
- `tmem_load/store` 在 TMEM 和分布式 tensor 之间搬；
- `tmem_wait` 等 `tcgen05.ld/st` 完成，不等待 MMA；
- `tc_gen5_mma` 是异步 MMA，完成信号走 mbarrier。

T4 完全不支持，读源码时把它当成“另一种 shared”即可，具体 lowering 在 Ch 21 展开。

### cluster 变体

`ttng.async_shared_store` 用 `st.async.shared` 把分布式 tensor 写到另一个 CTA 的 shared，完成时递减 mbarrier 的 transaction count（`TritonNvidiaGPUOps.td:492-520`）。它要求 cluster 至少两个 CTA，和 TMA multicast 一起用于跨 CTA 的 tile 复用。

## 对比与取舍

| 方案 | 延迟隐藏方式 | 寄存器压力 | 适用架构 | 主要成本 |
|---|---|---|---|---|
| `tt.load` → register | warp 切换 + 编译器调度 | 高（数据进寄存器） | 所有 | 大 tile 会占满寄存器 |
| `ttg.async_copy` → shared | async group + `async_wait` | 低（不经过寄存器） | sm_80+ | 每线程发指令，4/8/16B 限制 |
| TMA → shared | mbarrier transaction | 最低（单线程发一条） | sm_90+ | descriptor 设置、box/swizzle 约束 |
| TMEM | tcgen05 async | 最低（专用存储） | sm_100+ | 容量/寻址受限，编程复杂 |

shared vs TMEM：shared 更通用，能放任意 tile、支持 swizzle 和多种 view；TMEM 更适合 MMAv5 的 accumulator，读写路径和 layout 都更受限。T4 上只有 shared 和 register 两条路。

swizzle vs padding：swizzle 用异或重排，不浪费空间，但地址计算多一步；padding 在行间插入空洞，简单但浪费 shared 容量。Triton 主要用 swizzle，`NVMMASharedEncodingAttr` 还要求特定的 swizzle 宽度。

## 踩雷集锦

1. **把 `ttg.async_wait` 当成 CTA barrier。**
   op 文档明确说它不提供 CTA 同步（`TritonGPUOps.td:142-143`）。如果 shared 的生产者和消费者跨 warp，必须再加 `ttg.local_barrier` 或 mbarrier wait。否则一个 warp 可能读到另一个 warp 还没写完的 shared。

2. **masked `async_copy` 的 `other` 不是任意值。**
   lowering 里 masked copy 把 `src-size` 设为 0，shared 目标写 0；源码注释说“Always assume other = 0 for now”（`LoadStoreOpToLLVM.cpp:1100-1108`）。依赖 `other=NaN` 或非零填充的 kernel 在 async 路径上会得到 0。

3. **TMA block shape 的最后一维被 swizzle 改写。**
   你以为 `!tt.tensordesc<256x128xf16>` 会一次搬 `256x128`，但 `getTMABlockShapeTiled` 会把连续维设成 `8*swizzleBytes/elemBits`（`Dialect.cpp:4771-4787`）。如果 swizzle 是 128B、f16，连续维就是 64，不是 128。descriptor 的 `box_dim` 和你的 tile 形状不一致时，TMA 会搬多条 message。

4. **mbarrier phase 不翻转。**
   循环里一直用 phase 0，第二次 wait 等的是旧 phase。轻则立刻返回读到未完成数据，重则挂死。参考 `HoistMBarrierLifecycle.cpp:52-56` 的 `arith.xori %phase, 1`。

5. **`local_alloc` 不带 `src` 却想要 immutable。**
   `local_alloc` 的文档说：没有 `src` 时结果必须是 mutable（`TritonGPUOps.td:248-250`）。immutable memdesc 只能初始化一次，后续写入是未定义行为。

6. **TMA store 前忘记 `fence_async_shared`。**
   generic proxy 写 shared 和 async proxy 读 shared 之间需要 fence（`TMALowering.cpp:117-121`）。少了它，TMA 可能读到旧数据；这类 bug 在 T4 上根本复现不了，因为 T4 不走 TMA。

7. **在 T4 上期待 TMA。**
   `compiler.py:295-296` 在 `capability // 10 < 9` 时先跑 `rewrite_tensor_descriptor_to_pointer`，`compiler.py:368-369` 才在 sm_90+ 加 TMA lowering。T4 上 `tt.descriptor_load` 会被改写成普通 `tt.load`，不会生成 TMA。写课程实验时不要把 guided 的 TMA IR 当成 T4 实测。

## 进阶

- 读 `TMAUtilities.cpp` 的 `getTMAMessageLayouts` / `getTMAGatherScatterLayouts`（`TMAUtils.cpp`），理解 multi-CTA 时一条 TMA message 如何路由到不同 CTA 的 shared。
- 读 `BarrierOpToLLVM.cpp:312-390`，对比 sm_90+ 的 `mbarrier.try_wait.parity` 和 sm_80 的 `mbarrier.test_wait.parity + nanosleep` 循环。为什么 Hopper 可以用 `try_wait`，而 Ampere 要自己 spin？
- 读 `HoistMBarrierLifecycle.cpp` 全文，看 pass 如何把 barrier 的 init/inval 提出循环，并把 phase 作为 loop-carried value。
- 读 `AllocateSharedMemory`（Ch 27）如何把 memdesc 的 liveness 变成 `allocation.offset`，以及多缓冲如何复用同一块 shared。
- 读 `test/TritonGPU/loop-pipeline-cuda.mlir` 和 `test/TritonNvidiaGPU/membar.mlir`，把 CHECK 行当成“期望的 op 顺序”来读。

## 动手练习

1. **T4 的 descriptor 回退**：写一个用 `tl.make_tensor_descriptor` 的 kernel，dump TTIR，确认 `tt.descriptor_load` 在 `make_ttir` 后被改写成 `tt.load`。对照 `compiler.py:295-296` 和 `RewriteTensorDescriptorToPointer.cpp`。**guided**（本机没有可导入的 `triton`）。
2. **手写 async copy**：构造一个 TTGIR 模块，包含 `local_alloc`、`async_copy_global_to_local`、`async_commit_group`、`async_wait`、`local_load`，用 `triton-opt -tritongpu-pipeline` 跑一遍，观察 `num` 和 token 的变化。**guided**。
3. **算 TMA box**：对 `f16`、`128B` swizzle、tile `256x128`，手算 `blockShape` 和 `boxDim`，再对照 `getTMABlockShapeTiled`（`Dialect.cpp:4757-4791`）与 `createTMADesc`（`TMAUtilities.cpp:155-168`）。
4. **phase 翻转**：写一个两缓冲循环，用 `%phase` 作为 iter_arg，每轮 `arith.xori %phase, 1`；解释如果去掉 xor 会发生什么。**guided**。
5. **数 bank conflict**：给一个 `32x32` 的 f32 tile，分别用 `vec=1, perPhase=1, maxPhase=1` 和 `maxPhase=8` 的 swizzle，画出一个 warp 访问同一列时的 bank 分布。**guided**。

## 本章重点

- `memdesc` 是 shared/TMEM 的视图描述符，不是值；`local_alloc/local_load/local_store` 围绕它工作。
- `async_copy` 用 token + commit + wait 管理完成；`async_wait` 不是 CTA barrier。
- TMA 用 descriptor + mbarrier transaction 管理完成；box shape 受 256 和 swizzle 字节数约束。
- mbarrier 的 phase 必须按轮次翻转；`arrive` 和 `expect_tx` 的计数共同决定 phase 完成。
- swizzle 既避免 bank conflict，也满足 MMA shared operand 的硬件布局要求。
- T4 没有 `cp.async`、TMA、TMEM；descriptor 会在 TTIR 阶段退回指针。

## 自我检核

1. `ttg.local_alloc` 返回的 memdesc 里，`allocShape` 和 `shape` 为什么要分开？
2. `ttg.async_wait {num = 2}` 和 `{num = 0}` 的区别是什么？它为什么不保证 CTA 内可见？
3. TMA descriptor 的 `global_stride` 在创建时为什么要乘 `elemSize`？最后一维的 stride 为什么不传？
4. 一个 `128B` swizzle、f16、`256x128` 的 tile，TMA 的 `box_dim` 是多少？为什么？
5. mbarrier 的 `init count = 1`、`expect_tx = 16384`、TMA 搬了 16384 字节，wait 之后 phase 会怎样变化？
6. 为什么 TMA store 前需要 `fence_async_shared`，而 TMA load 后需要的是 mbarrier wait 而不是这个 fence？

## 延伸阅读

1. **`include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:178-280`、`445-484`**
   - **读哪里**：`async_copy_global_to_local`、`local_alloc`、`local_load`、`local_store`。
   - **学什么**：operand/trait/token 的精确契约，尤其是 `contiguity` 和 mutable 的约束。
   - **与本章关系**：这是 shared 路径的“指令集”，先能把 op 读对，才能读懂 lowering。

2. **`include/triton/Dialect/TritonNvidiaGPU/IR/TritonNvidiaGPUOps.td:287-430`、`523-750`**
   - **读哪里**：mbarrier 四个 op、TMA load/store/reduce/gather/scatter。
   - **学什么**：每个 op 的 `isSupported`、predicate、`cachePolicy` 和 completion 语义。
   - **与本章关系**：TMA 的硬件契约都在这里；先读 summary，再读 arguments。

3. **`lib/Dialect/TritonNvidiaGPU/Transforms/TMALowering.cpp:27-208`**
   - **读哪里**：`lowerTMALoad`、`lowerTMAStore`、`TMAStoreLowering`、pass 注册。
   - **学什么**：一次 TMA load/store 的固定展开顺序：alloc → init → expect → copy → wait → inval；store 则是 alloc → fence → copy → wait。
   - **与本章关系**：把“概念上的 TMA”落成可验证的 op 序列。

4. **`lib/Dialect/TritonNvidiaGPU/Transforms/TMAUtilities.cpp:48-225`**
   - **读哪里**：`getTMASwizzleMode`、`getTMAElementType`、`createTMADesc`。
   - **学什么**：swizzle mode 的 0/1/2/3 映射，`box_dim`/`global_dim`/`global_stride` 的生成。
   - **与本章关系**：解释“为什么 descriptor 的 shape 和 block shape 不是一回事”。

5. **PTX ISA：`cp.async.bulk.tensor` 与 `mbarrier`**
   - **读哪里**：PTX ISA 的 “Tensor Memory Accelerator” 和 “Parallel Synchronization and Communication Instructions: mbarrier”。
   - **学什么**：`complete_tx::bytes`、`try_wait.parity`、`expect_tx` 的硬件语义。
   - **与本章关系**：Triton 的 op 只是 PTX 的薄封装；硬件语义要以 PTX 为准。

## 下一章

→ [Ch 16 Pass 基础设施与 NVIDIA pipeline](./16-pass-infrastructure-and-nvidia-pipeline.md)：本章的 TMA/async/mbarrier op 是由哪些 pass 在什么顺序插入的？`add_stages` 如何把它们串成 `ttir → ttgir → llir`？
