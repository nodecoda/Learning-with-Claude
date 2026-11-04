# Ch 20 — Warp specialization

> 软件流水线让同一条指令流里的 load 和 dot 重叠；warp specialization 让它们根本不在同一条指令流里。

## 目标

读完本章，你应该能回答：

- `ttg.warp_specialize` 的 region、operands、`num_warps` 分别表达什么？default region 和 partition region 的区别是什么？
- 一个 matmul 循环是怎么被切成"load partition + MMA partition"的？切分靠哪些启发式和约束？
- partition 里的 warp 数是怎么定的？为什么初始值和最终值可能差 4 倍？
- warp specialization 和 software pipelining 是替代关系还是叠加关系？
- 为什么 Hopper/Blackwell 更需要它，而 T4 根本走不到这条路径？
- partition 之间传数据用 `aref` 和用 shared memory 有什么区别？

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92`，3.9.0 |
| GPU | Tesla T4，sm_75 |
| 本地 `triton` / `triton-opt` | 不可用 |
| `num_stages` 默认值 | 3（`third_party/nvidia/backend/compiler.py:123`） |

**证据等级**：本章全部 IR 形态和 partition 划分都是 `guided`——T4 上既没有 `warp_specialize` pass，也没有 TMA/TMEM/wgmma。源码行号用 `nl -ba` 核对；IR 片段来自 `test/TritonGPU/automatic-warp-specialization.mlir` 的 CHECK 和 `TritonGPUOps.td` 的文档示例。

**为什么 T4 到不了**：`make_ttgir` 只在两个分支启用 warp specialization：

```python
if capability // 10 in [8, 9]:
    nvidia.passes.hopper.add_hopper_warpspec(pm, opt.num_stages, dump_enabled)   # compiler.py:338
elif capability // 10 >= 10:
    ...
    passes.ttgpuir.add_warp_specialize(pm, opt.num_stages)                        # compiler.py:351
```

sm75 落在 `else`（`compiler.py:358-359`）。`tl.range` 的文档也写死了这一点（`python/triton/language/core.py:3692-3694`）："warp specialization is only supported on Blackwell GPUs and only works on simple matmul loops."

## 为什么需要这个

软件流水线（Ch 19）解决的是"同一组 warp 内，load 的延迟怎么被 dot 填满"。但它有两个天花板：

1. **指令流仍然串行**：一个 warp group 里，load 的地址计算、mask 判断、`cp.async` 发射、`local_load`、`mma` 都在抢同一个 issue slot。当 MMA 是异步的（wgmma/tcgen05）时，发射 MMA 的 warp 还要负责等 barrier、算地址，很容易成为瓶颈。
2. **寄存器预算共享**：所有 warp 平分同一个寄存器池。producer 需要少量寄存器做地址计算，consumer 需要大量寄存器做累加器；混在一起时，ptxas 只能折中。

warp specialization 把循环体按数据流切成几个 **partition**，每个 partition 跑在独立的 warp group 上，有：

- 独立的程序计数器（各自的 region）；
- 独立的寄存器预算（`requestedRegisters`，见 `OptimizePartitionWarps.cpp:271`）；
- 显式的跨 partition 通信（`aref` / shared memory / mbarrier）。

于是 producer 可以一直跑在"发射 TMA → 等 barrier → 再发射"的循环里，consumer 只做 `tcgen05.mma` + 等结果，互不干扰。这就是 Hopper/Blackwell 上 attention、GEMM 能持续打满张量核心的原因。

## 先建立直觉

先看一张分区图。一个标准 matmul 循环：

```text
原始循环体：
  %a = tt.descriptor_load %a_desc[...]      # TMA，慢
  %b = tt.descriptor_load %b_desc[...]
  %acc = tt.dot %a, %b, %acc                 # MMA，快但需要等数据

切分后（编号只是示意，实际顺序由调度决定）：
  partition0 (mma, stage 1)   partition1 (load, stage 0)
  ┌──────────────────┐       ┌──────────────────┐
  │ wait aref(a)     │       │ wait barrier     │
  │ tcgen05.mma      │ ◄──── │ issue TMA a      │
  │ yield acc        │       │ issue TMA b      │
  └──────────────────┘       └──────────────────┘
```

注意箭头方向：**数据从 load partition 流向 MMA partition，但 IR 里 MMA 可能排在 partition0**。后文的例子 2 就是这种情况——不要用编号猜角色，要看 region 里的 op。

两个关键点：

- **stage 是粗粒度的**：`Partition::getStage()` 只返回 0 或 1——MMA partition 是 1，其余是 0（`include/triton/Dialect/TritonGPU/Transforms/PartitionSchedulingUtility.h:55-59`）。这和 SWP 的 `num_stages` 细粒度 stage 不是一回事。
- **stage 决定缓冲代数**：`Partition.h:24-26` 的注释说得很直白——"The stage of a partition determines how many cycles the partition's outputs are buffered relative to its consumers."

所以 WS 的分区语义是"生产者领先消费者一代"，具体缓冲多少由后续的 pipeline 在 partition 内部再决定。

## 核心概念

### partition / graph / edge

`PartitionScheduling` 把循环体建成一张数据流图：

| 概念 | 含义 | 源码 |
|---|---|---|
| `Node` | 一个 op，或一个 SSA 值（block arg/op result） | `PartitionSchedulingUtility.h` |
| `Edge` | 生产者→消费者的数据边，带 size | 同上 |
| `Partition` | 一组 node + flags + cost | `PartitionSchedulingUtility.h:45-73` |
| `Flags` | `LOAD`/`STORE`/`MMA`/`TMEM`/`SFU`/`VIEW`/`CLC`/`MANUAL` | `PartitionSchedulingUtility.h:18-28` |

flags 不是装饰：后面的合并启发式几乎全部按 flags 判断"这两坨能不能放一起"。例如 `tmem_mma` 约束明确禁止 MMA partition 和 TMEM partition 合并（`PartitionScheduling.cpp:769-774`）。

### aref：跨 partition 的异步引用

`aref`（async reference）是 NVWS dialect 里的概念，表示"这个值由另一个 partition 在某一代产生"。相关 op 是 `nvws.aref.create`、`nvws.aref.buffer`、`nvws.aref.phi`（`third_party/nvidia/include/Dialect/NVWS/IR/NVWSOps.td:45-80`），`PartitionLoops.cpp:382-404` 处理 `aref.phi` 的克隆：

```cpp
if (auto arefPhi = dyn_cast<nvws::ArefPhiOp>(op)) {
  auto localPartitions = getValuePartitions(arefPhi.getLocal());
  auto remotePartitions = getValuePartitions(arefPhi.getRemote());
  for (size_t idx : partitionIndices) {
    auto selected = localPartitions.contains(idx) ? arefPhi.getLocal() : arefPhi.getRemote();
    builders[idx].mapping.map(arefPhi.getResult(), ...selected);
  }
}
```

`local` 和 `remote` 分别指"本代的值"和"上一代/另一 partition 的值"，每个 partition 只保留自己需要的那一支。这就是 WS 里"循环携带值"的表达方式。

### warp group 与 `num_warps`

**例子 1（源码依据）**：`ttg.warp_specialize` 的每个 partition region 有自己的 `num_warps`。下面是 op 定义里的文档示例（`TritonGPUOps.td:713-725`）：

```mlir
%0 = ttg.warp_specialize(%a, %b)
default {
  %out = some_operation(%a)
  ttg.warp_yield %out : i32
}
partition0(%arg0: i32, %arg1: i32) num_warps(8) {
  some_async_dispatch(%arg0, %arg1)
  ttg.warp_return
}
partition1(%arg0: i32, %arg1: i32) num_warps(1) {
  some_async_dispatch(%arg0, %arg1)
  ttg.warp_return
} : (i32, i32) -> i32
```

**源码依据**：`include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:690-750`，示例在 `:713-725`。op 的参数是 `partitionNumWarps`、可选的 `warpGroupStartIds`、`requestedRegisters`、`actualRegisters`（`:729-732`），region 是"一个 default + 一个 partition holder"（`:735-738`）。

注意 default region 的特殊性（`:701-706`）：它由当前正在执行的 warp group 跑，可以隐式捕获外部值；partition region 是 isolated from above，必须显式传参——因为它们的 warp 数不同，布局域也不同。

## 底层机制

### 1. 总编排：AutomaticWarpSpecialization

**源码依据**：`AutomaticWarpSpecialization::runOnOperation`（`AutomaticWarpSpecialization.cpp:96-128`）：

```cpp
if (triton::nvidia_gpu::getModuleTwoCTAs(getOperation())) return;   // 2-CTA 不同步 TMEM 复用

pm.addPass(createTritonGPUNormalizeWSWhileLoops());
addPassWithPartitionVerifier(createTritonGPUPartitionScheduling());
addPassWithPartitionVerifier(createNVWSHoistTmemStore());
addPassWithPartitionVerifier(createNVWSInsertAref());
addPassWithPartitionVerifier(createNVWSInsertTmemAref());
addPassWithPartitionVerifier(createSCCPPass());
addPassWithPartitionVerifier(createCSEPass());
addPassWithPartitionVerifier(createNVWSLowerAref({numStages}));
pm.addPass(createTritonGPUPartitionLoops());
pm.addPass(createNVWSLowerWarpGroup());
pm.addPass(createTritonGPUScheduleLoops());
...
multiBufferTMADescriptors(getOperation(), numStages);
```

四个阶段：

1. **PartitionScheduling**：建图、定 partition、打 `ttg.partition` 属性；
2. **aref 相关**：把跨 partition 的值变成 aref，再 lower 成 buffer；
3. **PartitionLoops**：把循环克隆进每个 partition，生成 `nvws.warp_group`；
4. **LowerWarpGroup**：把 `nvws.warp_group` 变成 `ttg.warp_specialize`，再跑一次 `schedule-loops` 给每个 partition 内部定调度。

注意最后一步：WS 之后还要 `scheduleLoops`（`:120`），因为每个 partition 里的循环需要自己的 stage 信息——**WS 不是 SWP 的替代品，它先把代码分开，再让每份代码各自流水**。在 `compiler.py:351-352` 也能看到这个顺序：`warp_specialize` 紧接着 `pipeline`。

### 2. PartitionScheduling：怎么切

建图之后，先做三件事：

```cpp
// PartitionScheduling.cpp:292-316
void propagateDataValues(const SmallVector<OutputPort> &values) {
  // 从"必须当数据传"的端口出发，沿输出边传播 data 标记
}

// :318-325
void initialPartitionAssignment(Graph *graph) {
  graph->walk([&](Node *node) {
    if (node->isData() && !node->hasPartition()) {
      auto partition = graph->addPartition();
      node->setPartition(partition);
    }
  });
}
```

- `propagateDataValues`：某些值必须跨 partition 传（不能重算），把它们标记出来并传播；
- `initialPartitionAssignment`：每个"数据节点"先各自成一个 partition，后面再合并；
- 手写 `ttg.partition` 属性的 op 会被 `deserializeManualPartitions` 识别（`:350-374`），标成 `MANUAL`，并且约束 `manual` 禁止两个 manual partition 合并（`:760-766`）。

属性名来自 `include/triton/Dialect/TritonGPU/IR/Partitioning.h:13-14`（`ttg.partition`、`ttg.partition.outputs`）和 `WarpSpecialization/PartitionAttrs.h:18`（`ttg.partition.stages`）。

然后是一张**启发式表**（`PartitionScheduling.cpp:473-747`）和一张**约束表**（`:749-783`）。启发式决定"想合并"，约束决定"不能合并"。挑几条关键的：

| 规则 | 类型 | 作用 |
|---|---|---|
| `load_local_alloc` | 启发式 | load 和它的 local_alloc 放同一 partition，避免数据绕一圈 |
| `view_sequence` | 启发式 | 连续的 view op 合并 |
| `loop_block_arg` | 启发式 | 循环携带值跟它的生产者放一起（非 token） |
| `none_consumer` | 启发式 | 普通计算并入消费者，但不并进 LOAD/MMA |
| `connected` | 启发式 | 边大于 16384 元素时优先合并，减少 aref 流量 |
| `load_epilog` | 启发式 | 不被 dot 消费的 load 组并回主 partition |
| `tmem_mma` | 约束 | MMA 和 TMEM partition 不合并 |
| `tmem_alloc` | 约束 | TMEM alloc 不并进 MMA partition |
| `clc` | 约束 | CLC 独占 worker partition |

**例子 2（源码依据）**：`test/TritonGPU/automatic-warp-specialization.mlir:33-52`，一个 Blackwell matmul：

```mlir
// CHECK-LABEL: ttg.warp_specialize
// CHECK-LABEL: default
// CHECK-LABEL: partition0
// OPT-SAME: num_warps(1)
// PIPELINE-COUNT-1: tc_gen5_mma
// CHECK-LABEL: partition1
// OPT-SAME: num_warps(2)
// PIPELINE-COUNT-2: async_tma_copy_global_to_local
// CHECK-NOT: partition2
```

按 CHECK 的位置读出来的事实：

- 除了 default，只有 **2 个 partition region**（partition0、partition1），没有 partition2；
- **partition0 是 MMA**：`PIPELINE-COUNT-1: tc_gen5_mma` 落在 `partition0` 标签之后，最终 `num_warps(1)`；
- **partition1 是 TMA + descriptor 管理**：`PIPELINE-COUNT-2: async_tma_copy_global_to_local` 落在它后面，最终 `num_warps(2)`；
- 也就是说，**MMA partition 反而比 TMA partition 小**。TMA 侧有 `minWarps = 2` 的硬下界（`OptimizePartitionWarps.cpp:204-205`）；MMA 侧没有这个下界，只要寄存器估计允许就能一路减半到 1（`:237-252`）——tcgen05 是异步发射的，持有它的 warp 不需要同时搬数据。

这和后文 Hopper 路径"每个 partition 固定 4 warp"形成鲜明对比。

### 3. PartitionLoops：克隆进 `nvws.warp_group`

**源码依据**（本节代码均来自 `PartitionLoops.cpp`）。切分只是属性标记，真正把代码搬进 region 的是 `PartitionLoops`。入口处有一条重要判断（`PartitionLoops.cpp:528-530`）：

```cpp
// There is nothing to do if the loop has 1 or fewer partitions.
if (llvm::size(partitions.getPartitions()) <= 1)
  return success();
```

也就是"切了等于没切"的循环直接跳过，不生成 `warp_specialize`。

然后建 `nvws.warp_group`（`:556-558`）：

```cpp
SmallVector<int32_t> numWarps(numPartitions, lookupNumWarps(loop));
auto wgOp = nvws::WarpGroupOp::create(topBuilder, resultTypes, numWarps, numPartitions);
```

**这里是最容易误解的一点**：`numWarps` 的长度是 `numPartitions`（**包含 default partition 0**），每个元素初始都是 `lookupNumWarps(loop)`。所以 `num_warps=4`、总共 2 个 partition（default + 1 个 worker）时，初始就是 4+4=8 个 warp；例子 2 那种 default + 2 个 worker 的结构，初始是 4+4+4=12，最终被压到 4+1+2=7。这也是为什么后面必须有 `OptimizePartitionWarps`。

克隆逻辑按 op 类型分派（`:414-430`）：

```cpp
if (auto forOp = dyn_cast<scf::ForOp>(op))          cloneForOp(forOp, builders, partitions);
else if (auto whileOp = dyn_cast<scf::WhileOp>(op)) cloneWhileOp(...);
else if (auto ifOp = dyn_cast<scf::IfOp>(op))       cloneIfOp(...);
else if (auto reduceOp = dyn_cast<triton::ReduceOp>(op)) cloneReduceOp(...);
else if (auto scanOp = dyn_cast<triton::ScanOp>(op))     cloneScanOp(...);
else if (auto conditionOp = dyn_cast<scf::ConditionOp>(op)) ...
```

每个 partition 一个 `WarpGroupBuilder`，只克隆"属于自己 partition 的 op"（`cloneOp` 的 `partitionIndices` 参数，`:375-412`）。循环结构本身（`for`/`while`/`if`/`reduce`）要特殊处理，因为它们有 region 和 iter_args，不能简单 clone。

### 4. LowerWarpGroup：变成 `ttg.warp_specialize`

**源码依据**：`third_party/nvidia/lib/Dialect/NVWS/Transforms/LowerWarpGroup.cpp:158-185`：

```cpp
auto wsOp = WarpSpecializeOp::create(rewriter, loc, warpGroupOp.getResultTypes(), numWarps);
auto &defaultBlock = wsOp.getDefaultRegion().emplaceBlock();
...
auto &block = wsOp.getPartitionOpHolder().emplaceBlock();
auto wspOp = WarpSpecializePartitionsOp::create(rewriter, loc, inputs, partitions.size());
for (auto [in, out, mapping] : zip(partitions, regions, mappings))
  populateRegion(rewriter, in, &out, inputs, mapping);
```

捕获值的处理在 `:98-145`，有一条硬规则：

```cpp
} else if (auto tensorTy = dyn_cast<RankedTensorType>(capture.getType())) {
  SharedEncodingTrait sharedEnc = getSharedEncoding(tensorTy);
  auto alloc = LocalAllocOp::create(rewriter, loc, memdescTy, capture);
  for (auto [i, region] : llvm::enumerate(partitions)) {
    Value value = LocalLoadOp::create(builders[i], capture.getLoc(), tensorTy, alloc);
    replaceAllUsesInRegionWith(capture, value, *region);
  }
  inputs.push_back(alloc);
}
```

**tensor 不能被 partition region 直接捕获**，必须落到 shared memory 再由各 partition `local_load`。常量和不含 tensor 的纯函数可以重算（`:120-126`），标量直接当参数传（`:141-143`）。

这解释了一个常见现象：加了 WS 之后 shared memory 用量突然变大——跨 partition 的每个 tensor 都多了一份 shared 缓冲。

### 5. OptimizePartitionWarps：warp 数怎么定

**源码依据**：这是 WS 里最"工程"的一段（`OptimizePartitionWarps.cpp:152-274`）。它分三步。

**第一步：估算每个 partition 的寄存器需求**（`:143-174`）：

```cpp
static unsigned getTensorNumI32Regs(RankedTensorType ty) {
  unsigned numElems = getTotalElemsPerThread(ty) * product(getThreadsPerWarp(ty)) * product(getWarpsPerCTA(ty));
  unsigned elSize = getIntOrFloatOrPtrBitWidth(ty.getElementType());
  return numElems * elSize / 32;
}
...
tensorRegs = std::max(tensorRegs, getTensorNumI32Regs(tensor));
...
tensorRegs *= 2;   // 假设最大的 tensor 占 warpgroup 寄存器的一半
```

**第二步：确定每个 partition 的 warp 下界**（`:195-210`）：

```cpp
if (isa<ttng::TMALoadLikeOpInterface>(op))  *minWarps = 2;
else if (isa<TMEMLoadOp, TMEMStoreOp, TMEMAllocOp>(op)) *minWarps = 4;
```

TMA 至少要 2 个 warp 才跑得动，TMEM 读写要 4 个 warp 才能覆盖所有 lane。

**第三步：迭代减半**（`:212-253`）。核心假设是 ptxas 会把 `nTotalRegs = 1<<16`（`:187`）平均分给所有 warp：

```cpp
unsigned reqRegsPerThread = tensorRegs / threadsPerWarp / (numWarps / 2);
unsigned nextTotalNumWarps = curTotalNumWarps - (numWarps / 2);
unsigned nextRegsPerThread = nTotalRegs / threadsPerWarp / nextTotalNumWarps;
if (reqRegsPerThread <= nextRegsPerThread) {
  numWarps /= 2;
  changed = true;
  break;
}
```

**例子 3（源码依据）**：源码注释给了一个具体算例（`:220-233`）。18 个 warp、`tensor<128x256xf32>` 在 8-warp partition 里需要 128 regs/thread，而 `65536/32/18 ≈ 113`，放不下；换成 `tensor<128x128xf32>` 只需 64 regs/thread，减到 4 warp 后 `65536/32/14 ≈ 146`，放得下，于是停在 4。

减完之后如果 warp 数变了，还要**重跑布局分配**（`:262-269` → `relayoutWarps`，`:120-136`）：

```cpp
pm.addPass(createTritonGPUCoalesce());
pm.addPass(createTritonGPURemoveLayoutConversions());
pm.addPass(createTritonGPUOptimizeThreadLocality());
pm.addPass(createTritonGPUAccelerateMatmul());
pm.addPass(createTritonGPURemoveLayoutConversions());
```

因为 `warpsPerCTA` 变了，blocked 和 MMA 编码都得重算。这也把 Ch 18 的 pass 串起来了：**WS 的每个 partition 都要重新做一遍 TTGIR 优化**。

最后写回两个属性（`:271-272`）：

```cpp
wsOp.setRequestedRegisters(estRegUsage);   // tensorRegs ? 88 : 24
wsOp.setPartitionNumWarps(partitionNumWarps);
```

### 6. 为什么 Hopper/Blackwell 更需要它

**guided**（T4 上无法验证以下硬件行为）。三个硬件事实：

- **wgmma / tcgen05 是异步的**：发射 MMA 只花几个 cycle，结果要等 mbarrier/TMEM。发射它的 warp 如果还要算地址、发 TMA，就会挡住下一发。
- **TMA 是单线程发起的**：一个线程就能发起整块 tile 的拷贝（`AsyncTMACopyGlobalToLocalOp`），不需要整个 warp group 陪着。把它放进 1-warp partition 最划算（例子 2 里的 `num_warps(1)`）。
- **TMEM 是独立地址空间**：Blackwell 的累加器在 TMEM，读写需要专门的 `tcgen05.ld/st`，和普通寄存器压力解耦。这让"MMA partition 用少量 warp 持有大累加器"成为可能。

T4 三者都没有：没有 TMA、没有 wgmma、没有 TMEM，`cp.async` 和 `mma.sync` 都是同步语义。把 load 和 dot 拆到不同 warp 只会增加同步开销，所以编译器根本不为 sm75 生成 WS。

## 进一步用法/重要变体

### `tl.range(warp_specialize=True)`

`core.py:3684-3694`：

```python
:param warp_specialize: Enable automatic warp specialization on the loop.
    The compiler will attempt to partition memory, MMA, and vector
    operations in the loop into separate async partitions. This will
    increase the total number of warps required by the kernel.
```

注意最后一句：**总 warp 数会增加**。`metadata["num_warps"]` 在 lowering 时会被 `ttg.total-num-warps` 覆盖（`compiler.py:527-530`），launch 用的是总 warp 数，不是原来的 `num_warps`。

### Hopper 的旧路径

sm90 用的是 `add_hopper_warpspec`（`compiler.py:338`），实现在 `third_party/nvidia/hopper/lib/Transforms/WarpSpecialization/`。它和 sm100 的自动分区是两套代码：Hopper 路径按 `AsyncTaskId` 切分（`WSSpecialize.cpp:403-420`），每个 partition 固定 4 warps：

```cpp
for (AsyncTaskId asyncTaskId : nTaskIds) {
  if (asyncTaskId == 0) continue;
  partitionNumWarps.push_back(4);
}
auto wsOp = ttg::WarpSpecializeOp::create(impB, dummyTypes, partitionNumWarps, nTaskIds.size() - 1);
```

对比 sm100 的 `OptimizePartitionWarps`（可以减到 1 warp），Hopper 路径更粗放。读源码时不要把两者混为一谈。

### `requestedRegisters` 与 `actualRegisters`

`requestedRegisters` 是编译器对每个 partition 的寄存器估计（88 或 24，`OptimizePartitionWarps.cpp:260`），`actualRegisters` 是 lowering 后实际分配的值。两者不一致时，说明 ptxas 的选择和估计有偏差，通常意味着该调 tile 或 `maxnreg`。

### 和 `num_stages` 的交互

WS 之后 `scheduleLoops` 会给每个 partition 重新定 stage，而 `AssignLatencies` 在 WS 开启时会调整 MMA 的 self-latency（`AssignLatencies.cpp:252-269`）：

```cpp
if (isWarpSpecialized(forOp)) {
  if (ttng::hasAccReadModifyWrite(mma, forOp)) opLatency.erase(&op);
  else opLatency[&op] += 1;
  ...
  mmaSelfLatency[mma] = 0;   // 操作数都在别的 partition 时，MMA 不用等自己
}
```

所以 `num_stages` 在 WS 内核里同时影响三件事：partition 间 buffer 的代数、partition 内 SWP 的级数、以及 MMA 的 latency 赋值。

## 对比与取舍

### WS vs SWP

| 维度 | Software pipelining | Warp specialization |
|---|---|---|
| 执行单元 | 同一组 warp，交错执行 | 不同 warp group，真正并发 |
| 同步 | `async_wait` / mbarrier | aref / mbarrier / shared |
| 寄存器 | 全组共享，容易互相挤压 | 每个 partition 独立预算 |
| 代码复杂度 | 低（自动） | 高（分区、通信、warp 数） |
| 适用 | 所有 sm80+ | sm90（Hopper 路径）/ sm100（自动路径） |
| 代价 | shared 翻倍 | 总 warp 数增加，occupancy 下降 |
| 失败模式 | spill、shared 超限 | partition 切错、通信开销超过收益 |

### 什么时候 WS 不划算

- **kernel 太小**：总 warp 数从 4 涨到 8+，occupancy 直接减半，收益抵不过同步开销。
- **没有 TMA**：用 `cp.async` 时 producer 仍需要一个 warp group 做地址计算和 mask，拆开的收益小。
- **非 matmul 循环**：`core.py:3692-3694` 明确说"only works on simple matmul loops"；分区启发式也是围绕 LOAD/MMA/TMEM 设计的。
- **2-CTA**：`AutomaticWarpSpecialization.cpp:98-99` 直接 bail，因为 2-CTA 的 TMEM 复用还没同步。

## 踩雷集锦

1. **"T4 上写 `tl.range(warp_specialize=True)` 就能加速。"**
   sm75 不进 WS 分支（`compiler.py:358-359`），属性会被忽略；文档也写死了 Blackwell only（`core.py:3692-3694`）。在 T4 上它不会报错，只是没效果——这种"静默无效"比报错更难查。

2. **"partition 数 = warp group 数。"**
   `ttg.warp_specialize` 有一个 default region（当前 warp group 执行）+ N 个 partition region。总 warp 数 = `num_warps`（default）+ Σ `partitionNumWarps`。例子 2 里 default 是 4，partition0 是 1，partition1 是 2，总共 7。

3. **"WS 会替代软件流水线。"**
   不会。`compiler.py:351-352` 的顺序是 `warp_specialize` 之后紧接 `pipeline`；`AutomaticWarpSpecialization.cpp:120` 也会再跑一次 `schedule-loops`。WS 只是把代码分到不同 warp group，每个 group 内部仍然要流水。

4. **"手改 `partitionNumWarps` 就行。"**
   verifier 会检查 `partitionNumWarps` 的数量与 partition region 数一致、`warpGroupStartIds` 的数量一致（`lib/Dialect/TritonGPU/IR/Ops.cpp:1398-1414`）。只改一个字段会直接报 "expected ... elements"。

5. **"partition 里可以直接用外面的 tensor。"**
   partition region 是 isolated from above，tensor 捕获会被 materialize 成 shared memory（`LowerWarpGroup.cpp:127-140`）。大 tensor 跨 partition 传会显著增加 shared 用量，甚至触发 `OutOfResources`。

6. **"2-CTA + WS 能一起用。"**
   `AutomaticWarpSpecialization.cpp:98-99` 对 `getModuleTwoCTAs` 直接 return；`AssignLatencies.cpp:290-293` 也把 2-CTA 的循环排除在 WS 之外。

7. **"Hopper 和 Blackwell 的 WS 是同一套实现。"**
   sm90 走 `add_hopper_warpspec`（固定 4 warp/task），sm100 走 `add_warp_specialize`（可减到 1 warp）。两者的 partition 依据、warp 数策略、pass 列表都不同。

## 进阶

- **读 `PartitionSchedulingUtility.h` 的 `Graph`/`Node`/`Edge`**：理解 `getOutDataEdges`、`getSize`、`crossesPartitions` 这些查询，才能看懂启发式表。
- **手写 `ttg.partition` 属性**：`deserializeManualPartitions`（`PartitionScheduling.cpp:350-374`）支持手工指定 partition，标成 `MANUAL` 后不会被自动合并。这是做 WS 实验的入口。
- **看 `NormalizeWSWhileLoops`**：`while` 循环的 WS 需要先规范化成统一形态（`AutomaticWarpSpecialization.cpp:107`），否则 clone 逻辑处理不了。
- **对比 `automatic-warp-specialization.mlir` 的 `OPT`/`BASE`/`PIPELINE` 三种 CHECK 前缀**：它们对应"只分区"、"分区+lower"、"再流水"三个阶段，正好是本章的四步编排。

## 动手练习

> 需要 sm100（或至少 sm90）的机器和构建好的 `triton-opt`；本机全部 `guided`。

1. **数 partition**：用 `triton-opt test/TritonGPU/automatic-warp-specialization.mlir --tritongpu-partition-scheduling`，找出每个用例的 partition 数和 flags，对照启发式表解释为什么这么切。
2. **看 warp 数变化**：同一份 IR，分别跑到 `--tritongpu-partition-loops`（初始 `num_warps`）和 `--tritongpu-optimize-partition-warps`（最终值），记录每个 partition 的 warp 数，用 `nTotalRegs` 公式验证。
3. **制造 aref**：把两个 partition 之间的 tensor 依赖改成标量依赖，观察 aref 是否消失、shared 用量是否下降。
4. **触发 2-CTA bail**：设置 `num_ctas=2` 再跑 `--tritongpu-warp-specialize`，确认没有生成 `ttg.warp_specialize`，并指出是 `AutomaticWarpSpecialization.cpp` 的哪一行返回的。
5. **读 region 结构**：从 `ttg.warp_specialize` 的 `partitionNumWarps`、`warpGroupStartIds`、`requestedRegisters` 三个属性反推每个 warp group 的起始 warp id，画出 7 个 warp 的分配图（default 4 + p0 1 + p1 2）。

## 本章重点

- `ttg.warp_specialize` = 1 个 default region + N 个 partition region，每个 region 有自己的 `num_warps` 和寄存器预算。
- partition 由数据流图 + 启发式/约束表切出；`Partition::getStage()` 只有 0/1 两档，MMA 是 stage 1。
- 初始每个 partition 拿 `lookupNumWarps(loop)` 个 warp，`OptimizePartitionWarps` 按寄存器估计迭代减半，TMA 至少 2、TMEM 至少 4。
- 跨 partition 的 tensor 走 shared memory（或 aref），标量直接传，常量/纯函数重算。
- WS 之后每个 partition 内部还要跑 SWP；`compiler.py:351-352` 的顺序是 `warp_specialize → pipeline`。
- Hopper 和 Blackwell 是两套实现：固定 4 warp/task vs 自动分区 + 动态 warp 数。
- T4 上完全不生效，且不会报错。

## 自我检核

1. `ttg.warp_specialize` 的 default region 和 partition region 在捕获语义上有什么区别？为什么？
2. 一个 `num_warps=4`、总共 2 个 partition（default + 1 个 worker）的循环，初始总 warp 数是多少？如果 worker 里只有 TMA，`OptimizePartitionWarps` 最多能把它降到多少？
3. `Partition::getStage()` 为什么只返回 0 或 1？这个 stage 和 SWP 的 `num_stages` 是什么关系？
4. 为什么跨 partition 的 tensor 必须经过 shared memory，而标量不用？
5. `AutomaticWarpSpecialization.cpp` 的四步编排里，`schedule-loops` 为什么放在最后？

## 延伸阅读

1. **`lib/Dialect/TritonGPU/Transforms/WarpSpecialization/AutomaticWarpSpecialization.cpp:96-128`**
   - 读哪里：`runOnOperation` 的 pass 列表和 two-CTA bail。
   - 学什么：WS 的四阶段编排，以及每个阶段之间为什么插一个 partition verifier。
   - 与本章关系：全章骨架；先读它，再读各个子 pass 就不会迷路。

2. **`lib/Dialect/TritonGPU/Transforms/WarpSpecialization/PartitionScheduling.cpp:292-430`、`473-783`**
   - 读哪里：data value 传播、初始分区、启发式表、约束表。
   - 学什么：一个循环怎么被切成 load/mma/store partition；哪些合并是禁止的。
   - 与本章关系：解释例子 2 里"为什么只有 2 个 partition"。

3. **`lib/Dialect/TritonGPU/Transforms/WarpSpecialization/OptimizePartitionWarps.cpp:143-274`**
   - 读哪里：寄存器估算、min warps、减半循环、`relayoutWarps`。
   - 学什么：warp 数不是拍脑袋定的，是按 `nTotalRegs` 和 tensor 大小算出来的；改 warp 数要重跑布局。
   - 与本章关系：回答"warp group 数如何决定"，并把 Ch 18 的 pass 串回来。

4. **`include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:690-750`**
   - 读哪里：`WarpSpecializeOp` 的 summary、示例、arguments/regions。
   - 学什么：region/operands/属性的精确语义；default region 可以隐式捕获，partition 不行。
   - 与本章关系：IR 层面的权威定义，写/读 TTGIR 时的对照表。

5. **`third_party/nvidia/lib/Dialect/NVWS/Transforms/LowerWarpGroup.cpp:98-185`**
   - 读哪里：捕获值处理、`WarpSpecializeOp::create`、region 填充。
   - 学什么：`nvws.warp_group` 如何变成 `ttg.warp_specialize`；tensor 为什么必须走 shared。
   - 与本章关系：解释 IR 里 shared 用量增加的来源。

## 下一章

WS 在 Blackwell 上还依赖两个本章只提到名字的东西：TMEM（累加器所在的独立地址空间）和 tcgen05（异步 MMA 指令），以及 CLC（cluster launch control）这类 Hopper/Blackwell 专属调度机制。下一章进入这些"只在最新硬件上存在"的 TTGIR 结构和 lowering。

→ [Ch 21 Blackwell：TMEM、tcgen05 与 CLC](./21-blackwell-tmem-tcgen05-clc.md)
