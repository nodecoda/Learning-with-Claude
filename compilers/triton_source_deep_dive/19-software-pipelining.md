# Ch 19 — 软件流水线

> 流水线不是"多开几个 buffer"，而是把一条循环的依赖图重新排到时间轴上。

## 目标

读完本章，你应该能回答：

- `num_stages` 从 kernel 参数到 TTGIR，经过了哪几个 pass，各自产出什么中间表示？
- 一个 load→dot 循环在 `num_stages=2/3/4` 时，TTGIR 分别长什么样？buffer 数和 prologue 拷贝数是多少？
- `ttg.async_copy_global_to_local`、`ttg.async_commit_group`、`ttg.async_wait` 是在哪一步、由哪个函数插入的？`num` 是谁算的？
- 为什么 `num_stages` 太大会 spill 或超 shared memory？失败在哪一层暴露？
- TMA（mbarrier）和 cp.async（async group）两条路径在 IR 上有什么区别？
- 为什么本机 T4 上 `num_stages` 基本是空操作？

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92`，3.9.0 |
| GPU | Tesla T4，sm_75 |
| 本地 `triton` / `torch` / `triton-opt` | 均不可用（`ModuleNotFoundError` / 无构建产物） |
| `num_stages` 默认值 | 3（`third_party/nvidia/backend/compiler.py:123`） |

**证据等级**：IR 片段来自 `test/TritonGPU/loop-pipeline*.mlir` 的 CHECK 期望（`源码依据`）；T4 上无法生成的形态（cp.async、TMA、wgmma）标 `guided`；pass 行为全部用 `nl -ba` 核对行号。

**T4 的关键事实**：`make_ttgir` 里流水线 pass 只在两个分支出现——`capability // 10 in [8, 9]`（`compiler.py:332-341`）和 `>= 10`（`compiler.py:342-352`）。sm75 落到 `else`，只跑 `triton_licm`（`compiler.py:358-359`）。也就是说 **T4 上 `num_stages` 不会产生任何 async copy**，本章所有 TTGIR 例子都是 `guided`（用 sm80/90 的测试期望值来说明机制）。

## 为什么需要这个

一次 `ld.global` 打到 HBM 的延迟是几百 cycle，而一次 `mma.sync` 只要十几 cycle。如果循环写成：

```text
for k:
  a = load A[k]      # 300 cycle
  b = load B[k]      # 300 cycle
  acc = dot(a, b, acc)  # 15 cycle
```

那么每个迭代有 95% 的时间在等内存。软件流水线要做的事，是把"第 i 次迭代的 load"和"第 i-1 次迭代的 dot"放进同一个循环体，让内存和计算重叠：

```text
prologue:  load(0), load(1)
steady:    for i: wait(load i 完成); dot(i); issue load(i+2)
epilogue:  dot(last)
```

这需要三样东西同时到位：

1. **异步拷贝**：`cp.async`（sm80+）或 TMA（sm90+），否则 load 本身就会阻塞。
2. **多缓冲**：一份 buffer 在被 dot 读的同时，下一份在被写。
3. **调度**：知道每条 op 属于哪个 stage，才能正确生成 prologue/epilogue 和索引回绕。

Triton 把这三件事拆成三个 pass：`assign-latencies` 决定"每条 op 差几个 stage"，`schedule-loops` 决定"每条 op 落在哪个 stage/cluster"，`pipeline` 负责真正改写循环（先把 load 换成 async copy，再展开）。

## 先建立直觉

先看一个具体的时间轴。假设 `num_stages=3`，一个 load→dot 循环，`stageDiff=2`（后面会推）：

```text
时间 →    t0        t1        t2        t3        t4
buf0:    load(0)   dot(0)    load(2)   dot(2)    ...
buf1:              load(1)   dot(1)    load(3)   ...
```

每个时刻都有一份 buffer 在被写、一份在被读、一份在飞行中。这就是"3 级流水 / 2 块 buffer"。

三个必须分清的概念：

| 概念 | 含义 | 由谁决定 |
|---|---|---|
| `num_stages` | 流水级数（同时有几代迭代在飞行） | 用户/kernel 参数 |
| `stage` | 某条 op 属于第几级 | `schedule-loops` |
| `stageDiff` | 生产者和消费者的 stage 差，等于 buffer 数 | `getDefUseStageDiff` |
| `num`（`async_wait`） | 允许还有几组拷贝未完成 | `updateWaits` 按 commit 链算 |

最容易混的是 `num_stages` 和 buffer 数。对一条直接喂给 dot 的 load，latency 是 `num_stages - 1`（`AssignLatencies.cpp:127`），所以：

```text
num_stages = 1  → 不流水（直接 bail）
num_stages = 2  → stageDiff = 1 → 1 块 buffer
num_stages = 3  → stageDiff = 2 → 2 块 buffer（双缓冲，默认值）
num_stages = 4  → stageDiff = 3 → 3 块 buffer
```

**源码依据**：`AssignLatencies.cpp:120-131`（`loadLatency = (numStages - 1) / (maxIndirectionLevel + 1)`），`LowerLoops.cpp:548-551`（`createAlloc(..., asyncLoad.stageDiff)`），`PipeliningUtility.cpp:416-433`（buffer 形状在最前面插一维 `distance`）。

## 核心概念

### CoarseSchedule：stage + cluster

调度结果序列化在 loop 上，每个 op 带两个属性：

```mlir
%v = tt.load %p {loop.stage = 0 : i32, loop.cluster = 2 : i32} : ...
```

**源码依据**：属性名在 `include/triton/Dialect/TritonGPU/Transforms/PipeliningUtility.h:16-22`（`tt.num_stages`、`loop.stage`、`loop.cluster`、`tt.scheduled_max_stage`）。`CoarseSchedule` 的定义在 `Schedule.h:43` 起。

- **stage**：时间维度，0 是最早。
- **cluster**：同一 stage 内的顺序，数字大的排后面。

`test/TritonGPU/matmul-loop-pipeline.mlir:18-26` 就是一个已经调度好的例子：`tt.splat`/`tt.addptr`/`tt.load` 是 `stage = 0, cluster = 2`，`tt.store` 是 `stage = 1, cluster = 1`。

### latency 与 indirection level

`AssignLoadLatencies` 不只处理直接喂 dot 的 load，也处理 load 链（load → load → dot）：

- indirection level 0：直接给 dot 用的 load；
- level 1：给 level 0 的 load 提供地址的 load；
- 依此类推。

`loadOpsToIndirectionLevel` 用 DFS 收集（`AssignLatencies.cpp:342-400`），如果同一个 load 被不同距离使用，直接排除（`:358-369`，注释 "we don't know which one to pick"）。

### async group 与 mbarrier

两条异步路径的同步原语完全不同：

| 路径 | 拷贝 op | 完成信号 | 适用 |
|---|---|---|---|
| cp.async | `ttg.async_copy_global_to_local` | `ttg.async_commit_group` + `ttg.async_wait{num}` | sm80+ |
| TMA | `ttng.async_tma_copy_global_to_local` | `ttng.barrier_expect` + `ttng.wait_barrier` | sm90+ |

**源码依据**：`AsyncWaitOp::isSupported` 返回 `computeCapability >= 80`（`include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:152-156`）；`AsyncCopyGlobalToLocalOp` 的语义在 `:178-203`；TMA 的 barrier 逻辑在 `LowerLoops.cpp:334-431`。

## 底层机制

### 1. 三个 pass 的分工

`SoftwarePipeliner.cpp:171-229` 的 `runOnOperation` 就是主流程：

```cpp
lowerLoops(moduleOp);      // ① 把 load 换成 async copy，建 buffer，插 wait
expandLoops(moduleOp);     // ② 用调度展开 prologue/steady/epilogue
removePipeliningAttributes(moduleOp);
pipelineWgmma(moduleOp, numStages);   // ③ Hopper: asyncLaunchDots
updateWaits(getOperation());          // ④ 重算每个 async_wait 的 num
...                                    // ⑤ TMA store 流水
```

**源码依据**：`SoftwarePipeliner.cpp:179`、`187`、`196`、`198`、`201`、`226`。

注意 ① 在 ② 之前：先把 IR 改写成"带 async copy 的循环"，再做循环展开。这样展开器只需要处理一种形态。

### 2. AssignLatencies：给 op 打 latency

入口 `assignLatencies`（`AssignLatencies.cpp:312-334`）：

```cpp
moduleOp->walk([&](scf::ForOp forOp) {
  if (preCondition(forOp) && getNumStagesOrDefault(forOp, defaultNumStages) > 1)
    loops.push_back(forOp);
});
...
AssignLoadLatencies(forOp, numStages, opLatency).run();
AssignMMALatencies(forOp, opLatency).run();
serializeLatencies(moduleOp, opLatency);
```

`preCondition`（`:76-85`）排除两类循环：`loopHasDistGreaterThanOne`（依赖距离 > 1）和 `isOuterLoop`（外层循环）。`getNumStagesOrDefault`（`PipeliningUtility.cpp:584-592`）优先读 loop 上的 `tt.num_stages`，否则用 kernel 的 `num_stages`。

`isPipeliningBeneficial`（`:147-203`）里有一条很实际的过滤：

```cpp
if (localAllocEnc) {
  auto vecBytes = getCopyVecBytes(registerTy, localAllocEnc);
  if (filterSmall && vecBytes < 4) return false;   // cp.async 至少 4 字节
}
```

小于 4 字节连续的数据没法用 cp.async，硬流水只会变成寄存器搬运。

### 3. ScheduleLoops：把 latency 变成 stage

`scheduleKeyOps`（`ScheduleLoops.cpp:153-249`）的核心是一个"到循环末尾的最长距离"计算：

```cpp
int d = lat + (maxDist < 0 ? 0 : maxDist);
...
opToStage[op] = maxDistance - dist;
```

也就是说：**latency 大的 op 被推到更早的 stage**，它的消费者留在后面。对一个 `load(latency=2) → dot(latency=0)`：

```text
dist(load) = 2 + dist(dot) = 2 + 0 = 2
dist(dot)  = 0
maxDistance = 2
stage(load) = 2 - 2 = 0
stage(dot)  = 2 - 0 = 2
```

然后 `scheduleLoop`（`:353-388`）按四步补全：

1. `schedulePrologueAndEpilogue`：把 `scf.if` 推到循环边界（`:307-351`）；
2. `scheduleDependencies`：保证生产者不晚于消费者；
3. `scheduleDistanceOneDependencies`：距离 1 的依赖放到下一 stage 的前一个 cluster（`:53-99`）；
4. `scheduleRemainingToLastStage`：剩下的都丢到最后一级（`:101-141`）。

**例子 1（源码依据）**：`test/TritonGPU/loop-pipeline.mlir:19-47`，`-tritongpu-pipeline=num-stages=3` 的期望输出：

```mlir
%ABUF = ttg.local_alloc : !ttg.memdesc<2x128x32xf16, #shared, #smem, mutable>
%BBUF = ttg.local_alloc : !ttg.memdesc<2x32x128xf16, #shared1, #smem, mutable>

// prologue：两次拷贝，索引 0 和 1
%a0 = ttg.memdesc_index %ABUF[%c0]
ttg.async_copy_global_to_local %aptr, %a0 mask %m0
%b0 = ttg.memdesc_index %BBUF[%c0]
ttg.async_copy_global_to_local %bptr, %b0 mask %m0 other %o
%a1 = ttg.memdesc_index %ABUF[%c1]
ttg.async_copy_global_to_local %aptr2, %a1 mask %m1
%b1 = ttg.memdesc_index %BBUF[%c1]
ttg.async_copy_global_to_local %bptr2, %b1 mask %m1

scf.for ... iter_args(..., %ins = %c1, %ext = %c-1) {
  %ext2 = arith.addi %ext, %c1
  %wrap = arith.cmpi sge, %ext2, %c2
  %ext3 = arith.select %wrap, %c0, %ext2
  ttg.async_wait {num = 2 : i32}
  %av = ttg.memdesc_index %ABUF[%ext3]
  %a  = ttg.local_load %av
  %bv = ttg.memdesc_index %BBUF[%ext3]
  %b  = ttg.local_load %bv
  tt.dot %a, %b, %acc
  // 然后 issue 下一次拷贝
}
```

（说明：SSA 名做了简化；`memdesc<2x...>` 的 2 是 buffer 数，与 CHECK 一致，其字面形态见 `test/TritonGPU/loop-pipeline-hopper.mlir:24`。B 的 buffer 类型是按 A 的同一条规则推出来的——该测试只 CHECK 了 `ttg.local_alloc` 这一行本身。）

三个可直接读出来的事实：

- `memdesc<2x...>`：2 块 buffer，对应 `num_stages=3`。
- prologue 有 **2 次** 拷贝（索引 0、1），等于 `maxStage = numStages - 1`。
- steady state 里 `async_wait {num = 2}`：允许 2 组拷贝在飞行中，只保证最老的那组完成。

**例子 2（源码依据）**：`test/TritonGPU/loop-pipeline.mlir:1700-1714`，`num_stages = 4` 但循环上界只有 2：

```mlir
// CHECK: ttg.async_copy_global_to_local {{.*}} mask %[[TRUE]]
// CHECK: ttg.async_copy_global_to_local {{.*}} mask %[[TRUE]]
// CHECK: ttg.async_copy_global_to_local {{.*}} mask %[[FALSE]]
// CHECK: scf.for
```

prologue 仍然生成 3 次拷贝（`maxStage = 3`），但第三次的 mask 被常量折叠成 false。这说明**展开器不为"迭代数不足"做特殊分支，而是用谓词把死迭代关掉**——`options.peelEpilogue = false` + `predicateFn = wrapInMaskOp`（`SoftwarePipeliner.cpp:103-110`）。

### 4. LowerLoops：把 load 换成 async copy

`lowerLoads`（`LowerLoops.cpp:460-668`）逐个检查循环体顶层的 load：

```cpp
int stageDiff = getDefUseStageDiff(&op, forOp, schedule);
if (stageDiff == 0) continue;           // 不参与流水的 load
...
canUseAsyncCp = isa<tt::LoadOp>(op) && canBeConvertedToAsyncLoad(...);
int copyVecBytes = getCopyVecBytes(...);
canUseAsyncCp &= copyVecBytes >= 4;
...
if (canUseAsyncCp || canUseTMA) {
  if (loadRequiresAdditionalBuffer(&op)) stageDiff += 1;
  asyncLoad.stageDiff = stageDiff;
  ...
}
```

`getDefUseStageDiff`（`:76-132`）取"定义 stage"和"所有顶层使用者 stage"的差，并且做了两处修正：

- 如果使用者在更晚的 cluster，`_useStage++`（`:111-118`），避免条件代码被移到 epilogue 后产生竞态；
- 如果有 `WaitBarrierOp` 使用者，取最远的那个（`:121-127`）。

然后 `createAsyncCopy`（`:156-202`）做四件事：

```cpp
Value view = createSingleBufferView(builder, alloc, insertIdx);
Operation *copy = ttg::AsyncCopyGlobalToLocalOp::create(builder, src, view, mask, other, ...);
Operation *commit = ttg::AsyncCommitGroupOp::create(builder, copy->getResult(0));
// 在使用者那一级插 wait + local_load
auto wait = ttg::AsyncWaitOp::create(builder, commit->getResult(0), 0);
auto viewLoad = createSingleBufferView(builder, alloc, extractIdx);
replaceUsesWithLocalLoad(builder, loadOp->getResult(0), viewLoad, wait.getResult());
```

`other` 不是零的时候还有一条特殊路径（`:188-199`）：`cp.async` 只支持零填充，所以先 local_load 再用 `arith.select` 把 `other` 补回去。

循环末尾还会补一个全局的收尾 wait（`:654-659`）：

```cpp
if (hasAsyncLoads) {
  builder.setInsertionPointAfter(forOp);
  ttg::AsyncWaitOp::create(builder, loc, ValueRange({}), 0);   // num = 0，等全部完成
}
```

**例子 3（源码依据）**：`test/TritonGPU/loop-pipeline-cuda.mlir:37-43` 的 CHECK 里能看到这个结构：

```mlir
// CHECK: scf.for
// CHECK:   ttg.async_wait {{.*}} {num = 1 : i32}
// CHECK:   tt.dot
// CHECK:   tt.dot
// CHECK:   ttg.async_copy_global_to_local
// CHECK:   scf.yield
// CHECK: ttg.async_wait {num = 0 : i32}
```

循环体内 `num=1`，循环外 `num=0`。前者是"允许 1 组在飞"，后者是"全部排干"。

### 5. TMA 路径：mbarrier 而不是 async group

TMA 拷贝不用 `commit/wait`，而是用一个 mbarrier 数组 + phase 翻转：

```cpp
// LowerLoops.cpp:406-420
Value barrierAlloc = triton::createBarrierAlloc(forOp, numBuffers, /*arriveCount=*/1, usePairBarrier);
ttng::BarrierExpectOp::create(builder, barrier, sizeInBytes, pred);
...
Operation *waitOp = ttng::WaitBarrierOp::create(builder, barrierViewWait, loadGroup.phase);
```

`createTMAAsyncCopy`（`:204-232`）不生成 `local_load` 的 wait token，而是把 `local_load` 插在 `waitOp` 之后。phase 是循环携带变量，每次索引回绕时翻转（`:615-619`）：

```cpp
Value nextPhase = arith::XOrIOp::create(builder, loc, phase, one);
phase = arith::SelectOp::create(builder, loc, cndExt, nextPhase, phase);
```

这就是 mbarrier 的"两相重叠"：两个 phase 可以同时在飞，用布尔值区分。

**guided**：T4 既没有 `cp.async` 也没有 TMA。sm80 走 `AsyncCopyGlobalToLocalOp`，sm90+ 的 descriptor load 走 TMA；`add_tma_lowering` 也只在 `capability // 10 >= 9` 时加入（`compiler.py:368-369`）。

### 6. PipelineExpander：真正展开循环

展开器在 `PipelineExpander.cpp`，三个关键函数：

**`initializeLoopInfo`（`:130-236`）** 做合法性检查：

- 每条 op 必须有 stage（`:184-191`，否则 "not assigned a pipeline stage"）；
- 只能调度顶层 op，不能是嵌套 region 里的（`:198-216`）；
- 循环携带依赖距离必须 ≤ 1（`:218-233`）；
- 静态 trip count 小于 `maxStage` 时允许动态循环（`:161-172`）。

**`createKernelLoop`（`:416-478`）** 建新循环，把跨 stage 的值变成 loop iter arg，并按 `maxStage - defStage` 分配版本号（`:430-452`）。如果 peel epilogue，上界要减掉 `maxStage * step`（`:455-467`）。

**`createKernel`（`:480` 起）** 按 `opOrder` 克隆 op，并给每个使用归纳变量的 op 加偏移（`:519-533`）：

```cpp
// offset = (maxStage - stages[op]) * step
Value offset = arith::MulIOp::create(rewriter, loc, step, const(maxStage - stages[op]));
Value iv = arith::AddIOp::create(rewriter, loc, newForOp.getInductionVar(), offset);
```

这就是为什么同一个循环体里，stage 0 的 load 用的是 `i+2` 的地址，stage 2 的 dot 用的是 `i` 的地址。

**`emitEpilogue`（`:658` 起）** 生成 `maxStage - 1` 段收尾。在 Triton 的默认配置里 `peelEpilogue=false`，收尾靠谓词完成，所以 epilogue 的形态是"带 mask 的普通迭代"。

### 7. updateWaits：`num` 不是常量

`LowerLoops` 插入的 wait 初值都是 0（`createAsyncCopy` 里 `AsyncWaitOp::create(..., 0)`）。真正的 `num` 由 `updateWaits` 事后算（`WGMMAPipeline.cpp:135-148`）：

```cpp
module.walk([&](ttg::AsyncWaitOp waitOp) {
  int minNumCommits = minNumInterleavedCommitOps(waitOp);
  waitOp.setNum(minNumCommits);
  waitOps.insert(waitOp);
});
tt::combineRedundantWaitOps(waitOps, ...);
```

它沿所有路径数"这个 wait 之前最多有几个 commit 没被 wait 掉"，取最小值。所以 `num=2` 不是"2 个 buffer"直接推出来的，而是从 commit 链分析出来的——两者在简单循环里数值相同，在分支/嵌套里会分叉。

## 进一步用法/重要变体

### 逐循环覆盖 `num_stages`

`tl.range(..., num_stages=k)` 会在 loop 上写 `tt.num_stages`（`core.py:3697-3709`），`getNumStagesOrDefault` 优先读它。文档里有一句很关键的区分（`core.py:3672-3675`）：

> The kernel argument only pipelines loads that feed into `dot` operations, while this attribute tries to pipeline most (though not all) loads in this loop.

也就是说：kernel 级 `num_stages` 走 `loadOpsToIndirectionLevel` 的 dot 反向切片；`tl.range` 的 `num_stages` 会让 `pipelineWithoutDot = true`（`AssignLatencies.cpp:113`），连不喂 dot 的 load 也尝试流水。

### `disallow_acc_multi_buffer`

`tl.range(..., disallow_acc_multi_buffer=True)` 会阻止 accumulator 多缓冲（`PipeliningUtility.h:17-18` 的 `tt.disallow_acc_multi_buffer`）。在 Hopper/Blackwell 上，累加器多缓冲能让 MMA 与自己重叠，但会多占一份 TMEM/寄存器；attention 这类累加器很大又频繁读的 kernel 常常关掉它。

### `flatten`

`tl.range(..., flatten=True)` 把嵌套循环压平再流水，避免"内层循环太短，prologue 占比过高"的 stage stall（`core.py:3681-3683`）。

### 和 warp specialization 的关系

在 sm100 的 pipeline 里，顺序是 `assign_latencies → schedule_loops → warp_specialize → pipeline`（`compiler.py:349-352`）。WS 先把循环切成 partition，**然后每个 partition 内部再各自流水**。所以 `num_stages` 在 WS 里同时影响 partition 的缓冲深度和 `OptimizePartitionWarps` 的寄存器估计。

## 对比与取舍

`num_stages` 的取舍（单条 load→dot 链，`guided`，因为本机不跑流水）：

| num_stages | latency | buffer 数 | prologue 拷贝 | shared 增量 | 收益 | 风险 |
|---|---|---|---|---|---|---|
| 1 | — | 0 | 0 | 0 | 无流水 | 内存延迟完全暴露 |
| 2 | 1 | 1 | 1 | 1×tile | 有限，拷贝与 dot 仍可能串行 | 收益常被 prologue 抵消 |
| 3 | 2 | 2 | 2 | 2×tile | 双缓冲，默认值，通常最佳 | shared 翻倍 |
| 4 | 3 | 3 | 3 | 3×tile | 更长延迟隐藏（TMA/大 tile） | 易超 shared、occupancy 下降 |
| 8 | 7 | 7 | 7 | 7×tile | 极端延迟隐藏 | 几乎必然 spill/超限 |

两条硬约束：

- **shared memory**：`metadata["shared"]` 来自 `ttg.shared`（`compiler.py:531`），加载时和 `max_shared_mem(device)` 比较，超了抛 `OutOfResources`，错误信息就是 "Reducing block sizes or `num_stages` may help."（`python/triton/runtime/errors.py:22`，检查点在 `python/triton/compiler/compiler.py:470-474`）。
- **寄存器**：流水会把多代值同时保活，ptxas 分配不下就 spill；`maxnreg` 只能限制上限，不能消除 live range。

## 踩雷集锦

1. **"T4 上调大 `num_stages` 会更快。"**
   sm75 根本不进流水线分支（`compiler.py:358-359`），`num_stages` 对生成代码没有影响。想在 T4 上验证流水线，只能读测试或换机器。

2. **"`num_stages=3` 就是 3 块 buffer。"**
   对直接喂 dot 的 load，buffer 数 = `stageDiff` = `numStages - 1` = 2。3 是"级数"，不是"缓冲数"。`memdesc<2x...>` 才是证据（`loop-pipeline-hopper.mlir:24`）。

3. **"循环里有 `ttg.barrier` 也能流水。"**
   `isSafeToPipeline` 明确排除含 `BarrierOp`、`GridDependencyWaitOp`、`AssertOp`、`PrintOp` 的循环（`ScheduleLoops.cpp:36-49`）。测试 `loop-pipeline.mlir:1741-1753` 就是反例：`num_stages=2` 也不生成 async copy。

4. **"小于 4 字节的 load 也会被转成 cp.async。"**
   `copyVecBytes < 4` 直接 `canUseAsyncCp = false`（`LowerLoops.cpp:496-499`）。此时如果 `stageDiff > 1`，会打印 "Pipelining load that cannot use vectorized copy... severe performance degradation"（`:527-530`）——它会在寄存器里流水，通常更慢。

5. **"`async_wait` 的 `num` 是 `num_stages`。"**
   `num` 由 `updateWaits` 沿 commit 链算（`WGMMAPipeline.cpp:135-148`）。在分支、嵌套 if、多组 load 的情况下，它和 `num_stages` 没有简单对应关系。

6. **"peel epilogue 一定发生。"**
   默认 `peelEpilogue = false`（`SoftwarePipeliner.cpp:105`），收尾靠谓词；只有 MMAv5 且最后一级有 wait 的循环才会走 `peelLoopEpilogue`（`SoftwarePipeliner.cpp:117-123`）。看到 epilogue 里还有 mask，不要以为是 bug。

7. **"`tl.range(num_stages=...)` 和 kernel 的 `num_stages` 等价。"**
   前者会尝试流水不喂 dot 的 load（`pipelineWithoutDot`），后者只沿 dot 的反向切片找 load（`AssignLatencies.cpp:113`、`392-399`）。语义不同，效果也可能差很多。

## 进阶

- **读 `loop-pipeline-expand.mlir`**：它直接测 `pipelineForLoop`，可以看到 `tt.scheduled_max_stage` 和 `loop.stage` 的最小输入形态，比读完整 pipeline 容易。
- **手写调度做实验**：给一个循环手动加 `loop.stage`/`loop.cluster`，只跑 `-tritongpu-pipeline`，观察展开器如何按你的调度生成 prologue。这是理解 `maxStage` 和索引回绕最快的方式。
- **对比 AMD 路径**：`test/TritonGPU/loop-pipeline-hip.mlir` 用 `-tritonamdgpu-schedule-loops`/`-tritonamdgpu-pipeline`，同样是 stage/cluster 模型，但同步原语不同（Ch 30）。
- **看 `combineRedundantWaitOps`**（`PipeliningUtility.cpp:460` 起）：多个 wait 合并成一个的条件，是理解 `num` 语义的延伸。

## 动手练习

> 需要构建过的 `triton-opt`；本机没有，标 `guided`。

1. **数 buffer**：对 `loop-pipeline.mlir` 的 `@matmul_loop`，分别用 `-tritongpu-pipeline=num-stages=2/3/4` 跑，记录 `memdesc<...>` 的第一维和 prologue 拷贝次数，验证 `numStages-1`。
2. **制造不流水**：在循环体里插一条 `ttg.barrier local`，确认 `num_stages=3` 也不产生 `async_copy`，并指出是哪个函数拒绝的。
3. **看 wait 的 num**：用 `-tritongpu-pipeline` 后接 `-canonicalize`，找出循环内和循环外的 `async_wait`，解释为什么一个是 2、一个是 0。
4. **触发 shared 超限**：在 T4 上把 tile 调到 `256x256xf32`、`num_stages=8`，记录 `OutOfResources` 的 required/limit 数值（`guided`，需要 GPU）。
5. **对比 TMA**：读 `loop-pipeline-hopper.mlir`，找出 `barrier_expect`、`wait_barrier`、`async_tma_copy_global_to_local` 三者的配对关系，画出 phase 翻转的时序。

## 本章重点

- `num_stages` 是级数；对直接 load→dot，buffer 数 = `stageDiff` = `numStages - 1`。
- 三个 pass 的顺序：`assign-latencies`（打 latency）→ `schedule-loops`（定 stage/cluster）→ `pipeline`（lower + expand）。
- `lowerLoops` 先把 load 换成 `async_copy + commit + wait + local_load`，`expandLoops` 再展开成 prologue/steady/epilogue。
- cp.async 用 async group + `num`，TMA 用 mbarrier + phase；两条路径的 IR 形态不同。
- 默认不 peel epilogue，用谓词关掉死迭代；prologue 拷贝数恒为 `maxStage`。
- 代价是 shared memory 和寄存器：超限在 `CompiledKernel._init_handles` 抛 `OutOfResources`，spill 在 ptxas 发生。
- T4（sm75）不跑流水线 pass，`num_stages` 在本机无效。

## 自我检核

1. `num_stages=4`、单条 load→dot，`stageDiff` 和 buffer 数分别是多少？prologue 有几次拷贝？
2. `ttg.async_wait {num = 2}` 的 `num` 是谁算的？它和 buffer 数的关系为什么不是恒等式？
3. 为什么 `lowerLoops` 必须在 `expandLoops` 之前跑？
4. 一个循环里有 `tt.print`，`num_stages=3` 会发生什么？在哪一行源码被拒绝？
5. TMA 路径里 phase 为什么需要翻转？不翻转会怎样？

## 延伸阅读

1. **`lib/Dialect/TritonGPU/Transforms/Pipeliner/SoftwarePipeliner.cpp:171-229`**
   - 读哪里：`runOnOperation` 的五步。
   - 学什么：lower → expand → remove attrs → wgmma → updateWaits 的编排，以及每步之后 IR 的形态。
   - 与本章关系：这是整章的骨架，其他文件都是它的子过程。

2. **`lib/Dialect/TritonGPU/Transforms/Pipeliner/AssignLatencies.cpp:106-334`**
   - 读哪里：`AssignLoadLatencies::run` 的 latency 公式、`isPipeliningBeneficial` 的过滤、`assignLatencies` 的循环筛选。
   - 学什么：哪些 load 值得流水、latency 怎么算、indirection level 怎么影响分配。
   - 与本章关系：决定"能不能流水"和"差几级"，是 buffer 数的源头。

3. **`lib/Dialect/TritonGPU/Transforms/Pipeliner/LowerLoops.cpp:76-232`、`460-668`**
   - 读哪里：`getDefUseStageDiff`、`createAsyncCopy`、`createTMAAsyncCopy`、`lowerLoads`。
   - 学什么：async copy/commit/wait 的插入位置、`other` 的 select 补偿、buffer 索引回绕、循环外收尾 wait。
   - 与本章关系：这是"TTGIR 里为什么会出现 `ttg.async_copy_global_to_local`"的直接答案。

4. **`lib/Dialect/TritonGPU/Transforms/PipelineExpander.cpp:130-236`、`416-560`、`658` 起**
   - 读哪里：合法性检查、`createKernelLoop` 的版本化、`createKernel` 的 IV 偏移、`emitEpilogue`。
   - 学什么：prologue/steady/epilogue 的生成规则，跨 stage 值如何变成 iter arg。
   - 与本章关系：解释 prologue 拷贝数和 `(maxStage - stage) * step` 偏移。

5. **`test/TritonGPU/loop-pipeline.mlir`、`loop-pipeline-hopper.mlir`、`loop-pipeline-expand.mlir`**
   - 读哪里：`RUN:` 行和 CHECK 块。
   - 学什么：不同 `num_stages` 的真实 IR 差异；cp.async 与 TMA 的形态对比。
   - 与本章关系：本机没有 GPU 时，这是唯一能"看到"流水线输出的地方。

## 下一章

流水线让一个 warp group 内的 load 和 dot 重叠，但所有 warp 仍然共享同一个指令流和寄存器预算。下一章把 producer 和 consumer 拆到不同 warp group：看 `ttg.warp_specialize` 的 region/operands 怎么表达这种并发，以及 partition 和 warp 数是怎么定的。

→ [Ch 20 Warp specialization](./20-warp-specialization.md)
