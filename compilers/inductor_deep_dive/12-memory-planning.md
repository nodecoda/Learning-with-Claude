# Ch 12 内存规划：谁和谁能共用一块 buffer

> **目标**：读懂本机 2.14.1 的内存规划实现：生命周期怎么算、`end < start` 为什么是复用的硬条件、`end == start` 为什么不行、`buf2 = buf1; del buf1  # reuse` 到底来自哪条路径。读完你能区分“生命周期复用池”和“kernel 内原地复用”，并知道本机的 `memory.py` 里其实没有 `MemoryPlanning` 这个类。

> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
```

模型仍是 lab_a：`relu(x @ w + b).sum(1) / 64`，`buf0` 是 `64×64`，`buf1`/`buf2` 是 `64`。

## 为什么

融合减少了 kernel 数，但 buffer 仍然要占内存。一个模型能不能在给定设备上跑，往往取决于峰值内存，而不是 kernel 数。Inductor 的内存规划要同时做两件事：调整节点顺序以降低峰值，以及在生命周期不重叠的 buffer 之间复用存储。

两类错误后果完全不同：

- 该复用却没复用：峰值变高，性能变差，结果仍然正确。属于浪费。
- 不该复用却复用了：一个 kernel 写输出时覆盖了另一个 kernel 还要读的输入。属于错误，而且通常不会崩溃。

第二类才是本章的重点。判断复用是否安全的唯一依据是生命周期：旧 buffer 的最后一个读者必须严格早于新 buffer 的第一个写者。写成公式就是 `end < start`；写成 `end <= start` 就会把“同一个节点内既读又写”的情况放进来，覆盖会改答案。

lab_a 的 `output_code.py` 里有一行很醒目的证据：

```text
buf2 = buf1; del buf1  # reuse
```

这行说明 `buf2` 复用了 `buf1` 的存储。但它并不来自“生命周期复用池”，而是来自 kernel 内的原地复用：`op2` 读 `buf1` 的 index 和写 `buf2` 的 index 完全相同，调度器判定可以写回原 buffer。两条路径生成同一句 `# reuse` 语法，所以只看这一行无法区分。本章会把两条路径拆开，并给出各自的判定条件。

还有一个版本事实要先说清楚：写作 brief 里提到的 `MemoryPlanning` 类，在本机 2.14.1 的 `torch/_inductor/memory.py` 里并不存在。这个版本把分析拆成了 `compute_memory_timeline`、`estimate_peak_memory`、`reorder_for_peak_memory` 等函数，把复用决策放到了 `codegen/wrapper.py` 的 `MemoryPlanningState` / `AllocateLine` / `FreeIfNotReusedLine` / `ReuseLine`。按类名搜索找不到，不代表功能缺失；按函数名读才找得到。

## 主线

### 12.1 先划清边界：memory.py 里有什么、没有什么

本机 `memory.py` 的顶层定义（按 `class` / `def` 扫描）包括：

| 名字 | 作用 |
| --- | --- |
| `PeakMemoryResult` | 重排结果：顺序、峰值、方法名 |
| `MemoryPlanningInfoForBuffer` | 每个 buffer 的 `size_alloc` / `size_free` / 后继节点 |
| `MemoryPlanningInfoForNode` | 每个节点的 index、size、前驱 buffer、前驱/后继节点 |
| `FreeableInputBuffer` | 可在程序中途释放的图输入 |
| `BufferInfo` | 生命周期：`start_step` / `end_step` |
| `SNodeMemory` | 区域内单个 step 的 alloc/free 量 |
| `get_freeable_input_buf` | 收集可释放输入及其用户 |
| `compute_size_for_scheduler_buffer` | 每个 buffer 的分配/释放字节数 |
| `assign_memory_planning_info_for_scheduler_buffers` | 把尺寸和后继写回 buffer |
| `assign_memory_planning_info_for_scheduler_nodes` | 把前驱/后继写回节点 |
| `compute_memory_timeline` | 把生命周期映射到 step |
| `estimate_peak_memory` / `estimate_region_peak_memory` | 峰值估计 |
| `reorder_for_peak_memory` | 试几种拓扑序，取峰值最低的 |

没有 `MemoryPlanning`。复用池和代码行生成在 `codegen/wrapper.py`：

| 名字 | 作用 |
| --- | --- |
| `buffer_reuse_key` | 复用池的 key：device、dtype、符号尺寸、对齐、stream、mempool |
| `MemoryPlanningState` | 复用池 + 已分配总量 |
| `AllocateLine.plan` | 分配时尝试从池里取 |
| `AllocateLine.should_reuse_buffer` | 判断取到的空闲块能不能用 |
| `FreeIfNotReusedLine.plan` | 释放时把块放回池 |
| `ReuseLine` | 生成 `new = old; del old  # reuse` |
| `EfficientPeakEstimate` | 分段树，支持 `peak_between` 查询 |

这个拆分有它的道理：`memory.py` 只做“尺寸、生命周期、峰值”的分析，不依赖 codegen 的顺序；`wrapper.py` 在真正逐行生成代码时，才有“当前分配到第几行、池里有哪些空闲块”的信息。分析是全局的，复用决策是流式的。

### 12.2 生命周期：start、end 与 -1

`compute_memory_timeline` 把节点列表变成 `BufferInfo` 列表。每个 `BufferInfo` 有四个关键字段：

```python
@dataclasses.dataclass
class BufferInfo:
    buffer: SchedulerBuffer | FreeableInputBuffer
    size_alloc: int
    size_free: int
    start_step: int
    end_step: int
```

计算规则分三类：

| 对象 | `start_step` | `end_step` |
| --- | --- | --- |
| 可释放的图输入 | `0` | 最后一个消费者的 step；若它是图输出，则 `-1` |
| 普通调度 buffer | 产出它的节点 step | 后继节点的最大 step；若没有后继，则 `step` |
| 图输出 buffer | 产出它的节点 step | `-1` |

`-1` 是一个哨兵：它表示“永远不释放”。`peak_memory_from_buf_info_list` 里的处理很直接：

```python
delta[bi.start_step] += bi.size_alloc
if bi.end_step != -1:
    delta[bi.end_step + 1] -= bi.size_free
```

图输出不扣减，所以它会一直算在峰值里。`SchedulerBuffer.can_free()` 也有一致的规则：只要 `users` 里有 `OutputNode`，就返回 `False`。两个地方说的是同一件事：输出必须活到所有计算结束。

普通 buffer 的 `end_step` 有两种特殊情况：

- 有后继：取所有后继 step 的最大值。这是“最后一个读者”。
- 没有后继：`end_step = step`，也就是 `start == end`。源码注释给了原因：fusion 可能把所有消费者都合进了产出节点，buffer 只剩下定义它的那个 op。

`end == start` 的 buffer 不能复用。它在产出的那一步就已经死了，但“死”发生在这一步的写之后；同一步里没有更早的空闲窗口可以给另一个 buffer。复用的硬条件是 `end < start`：旧 buffer 的最后一个读者严格早于新 buffer 的第一个写者。

```text
step:        0          1          2
buf_a:     [alloc ---- last read]                 end = 1
buf_b:                          [alloc ...]       start = 2
                                    1 < 2  -> 可以复用

buf_c:                 [alloc ---- last read]     start = 1, end = 1
buf_d:                 [alloc ...]                 start = 1
                                    end == start -> 不能复用
```

这张图是理解全章的锚点。后面所有“为什么这条 reuse 合法/不合法”的问题，都可以回到 `end < start`。

### 12.3 尺寸从哪来，对齐按什么单位

`compute_size_for_scheduler_buffer` 处理四种情况：

1. `NoneLayout`：`(0, 0)`。没有真实存储。
2. `MultiOutputLayout`：父 buffer 在创建时分配所有子 buffer 的总和，子 buffer 创建时分配 0、释放时释放自己的大小。源码注释里的例子是：`buf0` 创建时分配 30 字节，删除时释放 0；`buf1`/`buf2` 创建时 0，删除时分别释放 10/20。
3. mutation 的“改名 buffer”：`(0, 0)`。原地改写共享同一块存储，新名字只是跟踪状态，不产生新的分配。
4. 普通 buffer：`numel * dtype_size`，其中 `numel` 用 `optimization_hint` 取一个具体值。

lab_a 的三个 buffer：

| buffer | 形状 | dtype | 字节数 |
| --- | --- | --- | --- |
| `buf0` | `64×64` | float32 | `64*64*4 = 16384` |
| `buf1` | `64` | float32 | `64*4 = 256` |
| `buf2` | `64` | float32 | `64*4 = 256` |

这是算术，不是实测打印。`buf0` 和 `buf2` 在 kernel 调用时同时活着，所以这两个至少占 `16384 + 256 = 16640` 字节；输入 `arg0_1`/`arg1_1`/`arg2_1` 在 `addmm` 之后被 `del`，峰值出现在哪一步要看完整时间线，不能只用这三个数下结论。

对齐由 `torch/_inductor/utils.py` 定义：

```python
ALIGN_BYTES = 64

def _align(nbytes: int) -> int:
    """Round up to the nearest multiple of ALIGN_BYTES"""
    return (nbytes + ALIGN_BYTES - 1) & -ALIGN_BYTES
```

`output_code.py` 顶部的 `from torch._inductor.codegen.memory_planning import _align as align` 就是它。单位是字节，不是元素。按公式：

- `_align(256) = (256 + 63) & -64 = 256`
- `_align(257) = (257 + 63) & -64 = 320`

所以“64 个 float”正好是 256 字节，已经对齐；“65 个 float”是 260 字节，会被抬到 320 字节。把对齐按元素数算，会低估空洞。

### 12.4 生命周期复用（A）：复用池 + 峰值保护

这条路径的流程是：

```text
FreeIfNotReusedLine.plan
    -> 如果没被复用，按 buffer_reuse_key 放进池
AllocateLine.plan
    -> 如果同 key 的池里有空闲块，pop 出来
    -> should_reuse_buffer(free_line, size) 通过，就生成 ReuseLine
    -> 否则把块放回池，自己正常分配
```

`buffer_reuse_key` 不只看大小：

```python
def buffer_reuse_key(node: BufferLike) -> ReuseKey:
    storage_size = V.graph.get_allocation_storage_size(node)
    alignment = node.get_name() not in V.graph.unaligned_buffers
    stream = V.graph.scheduler.get_buf_stream(node.get_name())
    mempool = V.graph.scheduler.get_buf_mempool(node.get_name())
    return (
        node.get_device_or_error(),
        node.get_dtype(),
        sympy_str(V.graph.sizevars.simplify(storage_size)),
        alignment,
        stream,
        mempool,
    )
```

六个维度：device、dtype、符号化后的存储大小、是否对齐、stream、mempool。源码注释特别说明 size 用符号串而不是 `size_hint`，否则 `s0` 和 `s1` 可能因为碰巧共享同一个 hint 而被错误复用。

`should_reuse_buffer` 是最后一道门：

```python
def should_reuse_buffer(self, free_line, size):
    if self.comm_buffer:
        return True
    if free_line.scheduler_node_index + 1 == self.scheduler_node_index:
        return True
    overall_peak_memory = self.wrapper.estimate_peak.overall_peak_memory
    peak_memory_in_range = self.wrapper.estimate_peak.peak_between(free_line, self)
    new_peak_memory = size + peak_memory_in_range
    return new_peak_memory <= overall_peak_memory
```

三种情况：

1. 通信 buffer：单独池，直接允许。
2. 相邻节点：`free_idx + 1 == alloc_idx`，等价于 `end < start` 的最小间隔，直接允许。
3. 有间隔：只有当“这块新分配 + 间隔内的峰值”不超过全局峰值时才允许。也就是说，复用不能把峰值抬高；否则宁可不复用。

所以 `end < start` 是必要条件，不是充分条件。你还需要：同 key、池里有块、峰值不升。三件事缺一不可。反过来，`end == start` 连池都进不去：同一个调度节点的分配行先于释放行，分配发生时池里还没有这个 free line。

`ReuseLine.codegen` 最终调用 `make_buffer_reuse`，输出：

```text
buf2 = buf1; del buf1  # reuse
```

`config.allow_buffer_reuse` 默认是 `True`；关掉它，`FreeIfNotReusedLine.plan` 就不会把块放进池。

### 12.5 原地复用（B）：同一个 kernel 内的写回

这条路径不依赖复用池的 `[start, end]` 区间；它的安全性来自“逐元素读写相同”，判定发生在 codegen 之前：

```text
SchedulerNode.run
    -> decide_inplace_update()
    -> mark_run()
    -> codegen()
```

`decide_inplace_update` 的条件很多，按重要性列：

| 条件 | 含义 |
| --- | --- |
| 节点是 `SchedulerNode` | extern 不走这条 |
| `config.inplace_buffers` 为真 | 总开关 |
| 后端有 `INPLACE_BUFFERS` 特性 | CPU 后端有 |
| 输入 buffer 存在且 `can_reuse` | 不是所有 buffer 都能被复用 |
| 只剩一个剩余使用，且就是当前节点 | 没有别的读者 |
| `can_inplace(read)` | 读 index 和写 index、size 完全一致 |
| `can_match_buffer_size` | 大小完全相等，或在 95%–100% 之间 |
| 无跨 stream / 跨 mempool 风险 | 并发的另一条流可能还在读 |

`SchedulerNode.can_inplace` 的核心是：

```python
if len(self.read_writes.writes) == 1 and isinstance(read_dep, dependencies.MemoryDep):
    write_dep = next(iter(self.read_writes.writes))
    return read_dep.index == write_dep.index and read_dep.size == write_dep.size
```

读和写的 index、size 必须完全一样。回到 lab_a：

```text
op1: store_reduction('buf1', index1, reduction)   # index1 = p0
op2: load('buf1', index0)                        # index0 = p0
op2: store('buf2', index0, truediv, None)        # index0 = p0
```

`op2` 读 `buf1` 和写 `buf2` 用的是同一个 `index0`，size 都是 64；`buf1` 的 users 里只有 `op2`，且 `can_inplace=True`。所以 `V.kernel.inplace_update_buffers['buf2'] = 'buf1'`，`SchedulerBuffer.allocate()` 走 `codegen_inplace_reuse`：

```python
def codegen_inplace_reuse(self, input_buffer, output_buffer):
    ...
    self.codegen_allocation(input_buffer)
    self.freed.add(input_buffer.get_name())
    self.allocated.add(output_buffer.get_name())
    self.reuses[output_buffer.get_name()] = input_buffer.get_name()
    self.writeline(ReuseLine(self, input_buffer, output_buffer))
```

先给输入 buffer 生成分配行，再生成 `ReuseLine`。这就是 `output_code.py` 里那两行的来源：

```text
buf1 = empty_strided_cpu((64, ), (1, ), torch.float32)
buf2 = buf1; del buf1  # reuse
```

这条 reuse 的安全性来自“逐元素读写相同”，不是来自“生命周期不重叠”。两条路径的产物长得一样，但证据不同：A 看 `end < start` + key + 峰值；B 看 `can_inplace` + 唯一剩余使用 + 大小匹配。

### 12.6 峰值估计与重排

`prepare_planning_info` 是分析与重排的共同入口：

```python
name_to_freeable_input_buf = get_freeable_input_buf(nodes, graph_inputs)
assign_memory_planning_info_for_scheduler_buffers(nodes, name_to_buf)
assign_memory_planning_info_for_scheduler_nodes(...)
estimated_peak_memory, _ = estimate_peak_memory(
    nodes, name_to_freeable_input_buf, graph_outputs
)
```

`reorder_for_peak_memory` 在此之上试几种拓扑序：

- `topological_sort_lpmf`：按“生命周期最长优先”之类的启发式；
- `topological_sort_bfs`：广度优先；
- `topological_sort_dfs`：深度优先。

每种顺序都重新 `estimate_peak_memory`，取峰值最低的那个。源码会记录 baseline 和每种方法的峰值，并在 `config.reorder_for_peak_memory_debug` 打开时导出图给模拟器。重排前还会 `validate_graph_acyclic` 和 `validate_unique_buffer_names`，避免把一个错误的图交给后续 pass。

这个 pass 在 `Scheduler.__init__` 里位于 `fuse_nodes` 之后、`codegen` 之前。顺序很重要：fusion 会改变后继关系，也就会改变 `end_step`。在 fusion 之前算生命周期，会把已经被合进同一节点的 buffer 当成还要单独写回，峰值和复用机会都不对。

`EfficientPeakEstimate` 把“每个节点的峰值”建成一棵分段树，支持 `peak_between(free_line, alloc_line)` 查询。`should_reuse_buffer` 的第 3 条规则用的就是它。分析阶段的 `estimate_peak_memory` 和决策阶段的 `peak_between` 是同一套数据的两个视角：前者给全局峰值，后者给区间峰值。

### 12.7 把 output_code 逐行对上

lab_a 的 wrapper 关键行：

```text
buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
del arg0_1
del arg1_1
del arg2_1
buf1 = empty_strided_cpu((64, ), (1, ), torch.float32)
buf2 = buf1; del buf1  # reuse
cpp_fused_div_relu_sum_0(buf2, buf0)
return (buf2, )
```

逐行解释：

- `buf0` 是 `addmm` 的输出，`64×64` float32，16384 字节。它一直活到 fused kernel 结束（kernel 还要读它），所以没有 `del buf0`。
- 三个 `del arg*` 是输入的最后使用释放，不是复用。`addmm` 之后它们不再被读，调度器的 `free_buffers` / `last_usage` 让它们提前释放。
- `buf1` 是 `op1` 的输出，`64` float32，256 字节。它本来要作为 reduction 的结果写回，但 `op2` 的读写 index 与它完全一致，于是被原地复用。
- `buf2 = buf1; del buf1  # reuse` 是 B 路径的产物。它没有新建存储，只是把名字从 `buf1` 换成 `buf2`，并让旧名字失效。
- fused kernel 的参数是 `(buf2, buf0)`：输出用 `buf2`，输入用 `buf0`。`buf1` 在 Python 层已经不存在。
- `return (buf2, )` 是图输出；`buf2` 不会被释放。

这里看不到 A 路径的 reuse，因为 lab_a 只有三个 buffer，且 `buf1` 的生命周期和 `buf2` 重叠到同一个 kernel 里。要看 A，需要一个“旧 buffer 已经死了、新 buffer 在更晚的 step 才分配”的图；那类图的输出里会出现同样语法的 `new = old; del old  # reuse`，但来源是池，不是 `can_inplace`。判断方法不是看语法，而是回到 IR：旧 buffer 的后继是否都排在前面，新 buffer 的产出节点是否严格更晚。

## 可跑实验

生成产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

抽取分配、释放、复用的行：

```powershell
$dbg = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
    Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -LiteralPath (Join-Path $dbg.FullName 'output_code.py') `
    -Pattern '^\s*buf\d = empty_strided_cpu|# reuse|^\s*del arg\d|addmm\(|^\s*cpp_fused_div_relu_sum_0\(buf2|^\s*return \(buf2' |
    ForEach-Object { $_.Line.Trim() }
```

预期输出：

```text
buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
del arg0_1
del arg1_1
del arg2_1
buf1 = empty_strided_cpu((64, ), (1, ), torch.float32)
buf2 = buf1; del buf1  # reuse
cpp_fused_div_relu_sum_0(buf2, buf0)
return (buf2, )
```

确认三件事：

1. `buf1` 只有一行分配，紧接着就是 reuse；没有第二个 256 字节的分配。
2. `del arg*` 在 `addmm` 之后、fused kernel 之前；它们是输入的最后使用释放。
3. 没有 `del buf0`：它在 kernel 调用时还要被读。

再验证本机 `memory.py` 的结构（确认没有 `MemoryPlanning` 类）：

```powershell
Select-String -LiteralPath '.scratch\aic-env\Lib\site-packages\torch\_inductor\memory.py' `
    -Pattern '^class |^def ' |
    ForEach-Object { $_.Line }
```

预期能看到 `PeakMemoryResult`、`MemoryPlanningInfoForBuffer`、`MemoryPlanningInfoForNode`、`FreeableInputBuffer`、`BufferInfo`、`SNodeMemory`，以及 `compute_memory_timeline`、`estimate_peak_memory`、`reorder_for_peak_memory` 等函数；不会看到 `class MemoryPlanning`。找不到时不要怀疑装错了版本，按函数名继续读。

最后验证复用池的入口：

```powershell
Select-String -LiteralPath '.scratch\aic-env\Lib\site-packages\torch\_inductor\codegen\wrapper.py' `
    -Pattern 'class MemoryPlanningState|def buffer_reuse_key|def should_reuse_buffer|class ReuseLine|def codegen_inplace_reuse' |
    ForEach-Object { $_.Line.Trim() }
```

这五条正好对应本章的两条路径：`buffer_reuse_key` / `MemoryPlanningState` / `should_reuse_buffer` 属于 A，`ReuseLine` / `codegen_inplace_reuse` 同时服务 A 和 B。

## 源码落点

- `torch/_inductor/memory.py`
  - `MemoryPlanningInfoForBuffer` / `MemoryPlanningInfoForNode`：尺寸、前驱/后继的容器。
  - `get_freeable_input_buf`：哪些图输入可以在中途释放。
  - `compute_size_for_scheduler_buffer`：普通 buffer、`NoneLayout`、`MultiOutputLayout`、mutation 改名四种尺寸规则。
  - `compute_memory_timeline`：`start_step` / `end_step` 的计算，重点看 `-1` 和“无后继时 `end_step = step`”两个分支。
  - `peak_memory_from_buf_info_list`：`delta[start] += alloc`、`delta[end+1] -= free`、输出跳过 free。
  - `estimate_peak_memory` / `estimate_region_peak_memory`：全局峰值与区间峰值。
  - `reorder_for_peak_memory`：LPMF/BFS/DFS 三种顺序与最小峰值选择。
- `torch/_inductor/codegen/wrapper.py`
  - `buffer_reuse_key`：复用池的六个维度。
  - `MemoryPlanningState`：`reuse_pool` 与分配总量。
  - `AllocateLine.plan` / `should_reuse_buffer`：从池里取块、相邻直通、峰值保护。
  - `FreeIfNotReusedLine.plan`：没被复用的块放回池。
  - `ReuseLine.codegen` / `make_buffer_reuse`：生成 `new = old; del old  # reuse`。
  - `codegen_inplace_reuse`：先分配输入 buffer，再生成 ReuseLine。
  - `EfficientPeakEstimate.peak_between`：区间峰值查询。
- `torch/_inductor/scheduler.py`
  - `SchedulerBuffer.can_free`：图输出不释放。
  - `SchedulerBuffer.allocate`：原地复用与普通分配的分叉。
  - `compute_last_usage` / `free_buffers`：`del arg*` 这类释放的来源。
  - `decide_inplace_update`：B 路径的全部条件。
  - `SchedulerNode.can_inplace`：读/写 index 与 size 的相等性。
- `torch/_inductor/utils.py`
  - `ALIGN_BYTES = 64`、`_align`、`align`：字节对齐。
- `torch/_inductor/config.py`
  - `allow_buffer_reuse`、`inplace_buffers`、`reorder_for_peak_memory`、`reorder_for_peak_memory_debug`。

## 常见坑

1. **把复用条件写成 `end <= start`**。相等意味着“同一节点内既读又写”，覆盖会改答案。安全条件是严格的 `end < start`；`should_reuse_buffer` 的相邻分支也是 `free_idx + 1 == alloc_idx`，对应 `end < start` 的最小间隔。

2. **把 `output_code` 的 `# reuse` 一律当成生命周期复用**。lab_a 的 `buf2 = buf1` 是 B 路径（`can_inplace`），不是 A 路径（复用池）。两者语法相同，判断要看 IR：读/写 index 是否相同、旧 buffer 的后继是否都更早。

3. **以为图输出也会被释放**。`end_step = -1` 是哨兵，`peak_memory_from_buf_info_list` 跳过 free；`SchedulerBuffer.can_free()` 遇到 `OutputNode` 也返回 `False`。输出必须活到所有计算结束。

4. **只按大小配 reuse**。`buffer_reuse_key` 还要求 device、dtype、符号化存储大小、是否对齐、stream、mempool 全部一致。跨 stream 复用会引入并发读写危险，所以 stream 被放进 key。

5. **以为 `end < start` 就一定能复用**。还需要池里有同 key 的块，且 `size + peak_between <= overall_peak`（相邻节点除外）。复用不能把峰值抬高。

6. **把对齐按元素数算**。`ALIGN_BYTES = 64` 是字节；`_align(257) = 320`。按“元素个数”算会把 65 个 float 当成 65 字节对齐，严重低估。

7. **在 fusion 之前算生命周期**。`reorder_for_peak_memory` 在 `fuse_nodes` 之后运行；fusion 改变后继，也改变 `end_step`。提前算会把已经合进同一 kernel 的 buffer 当成还要单独写回。

8. **按 `MemoryPlanning` 类名搜索**。本机 2.14.1 的 `memory.py` 没有这个类；按 `compute_memory_timeline`、`estimate_peak_memory`、`reorder_for_peak_memory` 和 `wrapper.py` 的 `MemoryPlanningState` 读，才能找到完整链路。

## 自我检查

- [ ] 我能画出 `start` / `end` 的时间线，并解释为什么 `end < start` 才能复用、`end == start` 不能。
- [ ] 我能说出 `end_step = -1` 和“无后继时 `end_step = step`”分别对应什么情况。
- [ ] 我能列出 `buffer_reuse_key` 的六个维度，并解释为什么 size 要用符号串而不是 hint。
- [ ] 我能解释 A 路径（复用池 + 峰值保护）和 B 路径（原地复用）各自的判定条件。
- [ ] 我能从 `buf2 = buf1; del buf1  # reuse` 判断这是 B 路径，并给出 IR 证据。
- [ ] 我能解释 `buf0` 为什么没有 `del`，而 `arg0_1` 有。
- [ ] 我能按公式算出 `_align(256)` 与 `_align(257)`，并说明单位是字节。
- [ ] 我能说出 `reorder_for_peak_memory` 为什么必须在 `fuse_nodes` 之后。

## 延伸阅读

- **`torch/_inductor/memory.py`**：从 `compute_memory_timeline` 读到 `reorder_for_peak_memory`。与本章的关联：生命周期、尺寸、峰值三件事的原始实现；重点看 `-1` 哨兵和无后继分支。
- **`torch/_inductor/codegen/wrapper.py` 的 `MemoryPlanningState` 到 `ReuseLine`**：读 `buffer_reuse_key`、`should_reuse_buffer`、`codegen_inplace_reuse`。与本章的关联：复用池与原地复用两条路径的全部判定和代码生成。
- **`torch/_inductor/scheduler.py` 的 `decide_inplace_update` 与 `SchedulerBuffer.allocate`**：读条件列表和 `inplace_update_buffers` 的分叉。与本章的关联：B 路径的调度侧证据。
- **`compilers/mini_ai_compiler/12-liveness-analysis.md`**：读 `[start, end]` 闭区间、`end < start`、输出钉住的实验。与本章的关联：同一组生命周期直觉的最小实现；先在那门课里把区间算对，再回来看 Inductor 的 `-1` 和复用池。
- **TVM `tir.transform.StorageRewrite`（https://tvm.apache.org/docs/）**：读它如何用 liveness 合并 buffer。与本章的关联：工业实现里同样的“区间 + 复用”思路，以及它对 in-place 的额外检查。

---

[上一章：读调度 IR](./11-reading-scheduler-ir.md) | [下一章：LoopBody 与 ops_handler](./13-loop-body-and-ops-handler.md)
