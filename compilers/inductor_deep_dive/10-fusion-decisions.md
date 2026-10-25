# Ch 10 融合决策：谁能和谁住进同一个 kernel

> **目标**：把 `TORCH_LOGS=fusion` 的真实输出读懂，并能预测 lab_a 的融合结果：`op1 + op2` 合成一个 kernel，`op0` 永远留在外面。读完你能解释垂直融合、水平融合、reduction 融合各自的合法条件，以及 `found 0 possible fusions` 到底意味着什么。

> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
```

实验统一走 `compilers\inductor_deep_dive\code\tools\run.ps1`，模型是第 1 章的 lab_a：`relu(x @ w + b).sum(1) / 64`。

## 为什么

融合是 loop-level IR 最直接的收益来源。两个节点合进一个 kernel，中间 buffer 就不用写回内存再读出来；对 pointwise 链，融合还让向量化覆盖整条链，而不是每个 kernel 各自向量化一次。反过来，一次不合法的融合会让结果错，或者让一个本来能向量化的 kernel 退化成标量。

麻烦在于，融合合法性不是“算子类型”的属性。`relu` 后面接 `sum` 能不能合，取决于 `relu` 写出的 index 和 `sum` 读入的 index 是否逐元素对得上；`sum` 后面接 `div` 能不能合，取决于 `div` 读的是不是 `sum` 写出的那一格。调度器不靠名字猜，它拿 `MemoryDep` 的 index 和 size 做匹配。

代价也是真实的。把两个迭代域不同的节点硬塞进一个 kernel，会让循环嵌套变形：reduction 的内层循环要包住 pointwise，或者 pointwise 要拆成 epilogue。Inductor 为此区分了普通 `FusedSchedulerNode` 和 `OuterLoopFusedSchedulerNode`。lab_a 的 `op1_op2` 属于后者，这不是命名上的小差别：它决定了生成代码里 reduction 和 epilogue 的相对位置。

这一章的目标很具体：给你一段 `TORCH_LOGS=fusion` 的输出，你能逐行指出“这一轮有几个节点、哪一对被选中、为什么下一轮没有候选”。做到这一点，排查“为什么多了一个 kernel”就不再靠猜。

## 主线

### 10.1 决策流程：最多十轮，逐步收敛

融合发生在 `Scheduler.__init__` 的中段，位置在流/内存池分配之后、`merge_loops` 之前。先看 `fuse_nodes` 的骨架：

```python
for i in range(10):
    old_len = len(nodes)
    fusion_log.debug("===== attempting fusion (%d/10): %d nodes =====", i + 1, old_len)
    nodes = self.fuse_nodes_once(nodes, is_reorder_round=False)
    new_len = len(nodes)
    fusion_log.debug("completed fusion round (%d/10): fused %d nodes into %d nodes\n", i + 1, old_len, new_len)
    if new_len == old_len or new_len == 1:
        fusion_log.debug("===== fusion complete (%d iterations) =====", i + 1)
        break
```

三个细节：

- 十轮是上限，不是目标。lab_a 两轮就收敛：第一轮 3 → 2，第二轮 2 → 2。
- 终止条件是“这一轮没有减少节点”或“只剩一个节点”。不是“没有候选”才算结束；有时候候选存在，但都被更高优先级的融合挡掉了。
- 循环结束后还有一次可选的 reorder round（当 `loop_ordering_after_fusion` 或 `loop_index_inversion_in_fusion` 开启时）。它调用 `fuse_nodes_once(..., is_reorder_round=True)`，所以你在 `fusion complete` 之后还会看到一组 `candidates:` 和 `found 0 possible fusions`。那不是“又失败了一次”，而是换一种循环顺序再试一次。

`fuse_nodes_once` 做四步：

1. `prune_redundant_deps(nodes)`：把已经被更早节点满足的依赖去掉，避免同一对节点反复出现在候选里。
2. 打印候选：`fuse_nodes_once, candidates:`，每个节点用 `debug_str_short()`，也就是 `SchedulerNode(name='op1'), Reduction([...])` 这种短格式。
3. `get_possible_fusions(nodes, is_reorder_round)`：按“共同使用的 buffer”分组，组内两两检查；每对只检查紧随其后的 `config.max_fusion_buffer_group_pairwise_attempts`（默认 64）个节点。`aggressive_fusion` 打开时，还会按相同的 `group` 再分组检查一遍。
4. `get_possible_fusions_with_highest_priority` 过滤优先级，`score_fusion_key` 排序，打印 `found N possible fusions`，然后按分数从高到低融合。

为什么必须迭代？因为融合改变了图。`op1` 和 `op2` 合成 `op1_op2` 之后，`op1_op2` 的 `unmet_dependencies` 是两者的并集，`writes` 也是并集；原来“`op2` 读 `buf1`”这条边消失了，新的候选关系只能下一轮再算。一次扫完所有对是不成立的，因为第一对融合会改变后面所有对的合法性。

### 10.2 垂直融合：从 producer 写到 consumer 读

`_can_fuse` 先判断两个节点有没有祖先关系：

```python
if node1.get_operation_names() & node2.ancestors:
    # node2 depends on node1 outputs -> vertical
else:
    # common reads -> horizontal
```

垂直融合的语义是“把 consumer 塞进 producer 的 kernel”。合法性由 `can_fuse_vertical` 检查：

- 遍历 `node2.unmet_dependencies`，先按 `mutation_renames` 改名，避免被原地改写的历史名字干扰。
- `WeakDep` 可以走 `fusable_weak_dep` 单独放行；它只影响排序。
- 对 `node1.read_writes.writes` 里的每个写，尝试用 `fusable_read_and_write(rd, cd)` 消掉一个读。
- 消不掉的读如果还指向 `node1` 的 buffer，理由就是 `memory deps did not match`；如果指向的 buffer 由 `node1` 和 `node2` 之间的某个节点产出，理由就是 `intermediate nodes between node1 & node2`。如果剩余的读由 `node1` 之前就已完成的节点产出，是允许的；只有夹在两者之间的中间节点才会阻止融合。

`fusable_read_and_write` 的匹配规则可以浓缩成一张表：

| 检查 | 作用 |
| --- | --- |
| `read.name == write.name` | 必须是同一个 buffer |
| 不含 `SymT.TMP` 自由符号 | 临时符号无法在融合后复用 |
| `mode_requires_synchronization(write.mode)` | 任何非空 store mode（如 `atomic_add`）都要求全局同步，拒绝融合 |
| `_same_index_with_prefix_size(read, write)` | index 与 size 必须完全一致 |
| 可选 normalize | 只在 `loop_ordering_after_fusion` 开启时合并循环变量再比一次 |
| `StarDep` 分支 | 只接受另一个同 `mode` 的 `StarDep` |

回到 lab_a：

```text
op1.writes = [MemoryDep('buf1', c0, {c0: 64})]
op2.unmet_dependencies = [MemoryDep('buf1', c0, {c0: 64})]
```

名字相同、index 都是 `c0`、size 都是 64，所以这一对能过。gen 代码里 `op1` 的 `store_reduction('buf1', index1, ...)` 用的是 `index1 = p0`，`op2` 的 `load('buf1', index0)` 用的是 `index0 = p0`；在 IR 层面它们被归一成同一个 `c0`，这就是“逐元素对上”的具体含义。

`op0` 与 `op1` 则对不上：

```text
op0.writes = [StarDep(name='buf0', mode=None)]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
```

`StarDep` 没有 index，`fusable_read_and_write` 的 `StarDep` 分支只在两边都是 `StarDep` 且 `mode` 相同时才返回 `True`。这里一边是 `StarDep`、一边是 `MemoryDep`，匹配失败。就算把这一条绕过去，`_can_fuse` 还有一条更早的硬规则：`node1` 是 extern 但不是 Triton kernel 时直接拒绝，理由是 `node1 is extern but not a triton kernel`。lab_a 的 `addmm` 正是这种情况。

### 10.3 水平融合：同 group 的兄弟节点

水平融合处理的是“两个节点互不依赖，但读同一批 buffer”的情况。它在 `_can_fuse` 的 `else` 分支里，由后端的 `can_fuse_horizontal` 决定。

CPU 后端的实现有三道门：

1. 任一边是 template → 拒绝。
2. `len(node1.get_nodes()) + len(node2.get_nodes()) > config.cpp.max_horizontal_fusion_size` → 拒绝。默认上限是 16（`TORCHINDUCTOR_CPP_MAX_HORIZONTAL_FUSION_SIZE` 可改）。
3. 任一边是 `OuterLoopFusedSchedulerNode` 或 `ExternKernelSchedulerNode` → 拒绝。

真正的域匹配在 `_why_fuse_nodes`：

| 条件 | 含义 |
| --- | --- |
| `vars1 == vars2 and reduce1 == reduce2` | 迭代域完全一致 |
| `reduce1 == () and vars1 == vars2 + reduce2` | 左边是 pointwise，右边是它的 reduction 前缀 |
| `_can_fuse_nodes_with_compatible_ranges` | 两边都是 pointwise、元素总数相同、至少一边是一维，可以重算 sizes/body |

第三条不是万能的：`_can_fuse_nodes_with_compatible_ranges` 里明确写了，`FusedSchedulerNode` 不能作为被重算的那一边（`We can not recompute sizes and body for nodes other than SchedulerNode`）。所以“元素数相同”只是必要条件，不是充分条件。

lab_a 里没有水平融合：`op1` 和 `op2` 是依赖关系，不是兄弟；`op0` 是 extern，直接被排除。你看到的唯一融合是垂直方向的 `op1 + op2`。

### 10.4 reduction 的边界

reduction 是最容易误判的部分。三条边界要分开看：

**第一，reduction → pointwise 可以融合，但往往不是普通垂直融合。** `op1` 的 group 是 `((64,), (64,))`，`op2` 是 `((64,), ())`。迭代域不同，普通 `_can_fuse_horizontal_impl` 匹配不上；CPU 后端走的是 `can_fuse_vertical_outer_loop`：

```python
return (
    not node1.is_template()
    and not node2.is_template()
    and node1.get_operation_names() & node2.ancestors
    and not (
        self._can_fuse_horizontal_impl(node1, node2)
        and not node1.is_reduction()
    )
    and self._get_outer_loop_fusion_depth(node1, node2) >= 1
)
```

结果就是 `OuterLoopFusedSchedulerNode`：reduction 留在内层，pointwise 变成每个外层下标执行一次的 epilogue。生成代码里的 `#pragma omp single` 段就是那个 epilogue（第 14 章会展开）。

**第二，reduction → reduction 受严格求和顺序限制。** 两个 reduction 如果都带 `has_strict_sum()`，`_can_fuse` 直接拒绝，理由是 `reduction fusion does not preserve strict sum ordering`。不同归约顺序的 reduction 还有 `MixOrderReduction` / `FusedMixOrderReductions` 这一套专门处理，不是简单的“能”或“不能”。

**第三，带同步语义的写不能垂直融合。** `mode_requires_synchronization` 对任何非 `None` 的 mode 返回 `True`。源码注释解释了原因：`index_add_`、`scatter_add_` 这类操作要求所有线程先完成写，再允许读；把它们和 consumer 合进一个 kernel，读可能看到未完成的写。

水平方向的 reduction 还多一条：`_why_fuse_nodes` 的兼容范围分支要求 `reduce1 == () and reduce2 == ()`，也就是两边都必须是 pointwise。两个 reduction 的 reduce range 不同时，水平融合没有合法域。

### 10.5 分数、优先级与 benchmark

合法性是布尔值，分数只决定“先合哪一对”。`score_fusion_memory` 估算融合能省下多少内存操作；`score_fusion_key` 用它排序；`get_possible_fusions_with_highest_priority` 再按后端优先级过滤一轮。

分数还影响“要不要为了融合改变循环顺序”。当 `shared_data_score` 低于 `config.score_fusion_memory_threshold` 时，源码会尝试：

- `shared_data_after_reordering_loop`：交换循环顺序后再算共享数据；
- `shared_data_after_inverting_indexing`：反转 index 后再算；
- `_try_reindex_pointwise_for_reduction`：把 pointwise 的 index 重写成 reduction 的域，再试一次垂直融合。

这些尝试都有开关（`loop_ordering_after_fusion`、`loop_index_inversion_in_fusion`、`loop_reindexing_after_fusion`），默认值随版本变化。它们的存在说明一件事：融合决策不是一次静态匹配，必要时会先改循环形状再匹配。

`config.benchmark_kernel` 打开时，`benchmark_fused_nodes` 会实际测融合前后，`PendingFusion` 那套异步编译逻辑就是为它准备的。本章的实验不打开它：输出会更长，而且和“合法性”这一层混在一起，不利于初学。

### 10.6 `TORCH_LOGS=fusion` 逐行读

先给命令。通道名是 `fusion`；加 `+` 是 DEBUG，不加是 INFO。命中缓存时调度器不会重跑，所以要强制重编译：

```powershell
$env:TORCH_LOGS = "fusion"
$env:TORCHINDUCTOR_FORCE_DISABLE_CACHES = "1"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

真实输出（去掉时间戳/pid/文件行号和 `[0/0]` 前缀；`...` 表示我省略了 `stack_traces`）：

```text
[__fusion] ===== attempting fusion (1/10): 3 nodes =====
[__fusion] fuse_nodes_once, candidates:
[__fusion]   ExternKernelSchedulerNode(name='op0')
[__fusion]   SchedulerNode(name='op1'), Reduction(['[64]', 'sum', 'origins=OrderedSet([sum_1, relu])', ...])
[__fusion]   SchedulerNode(name='op2'), Pointwise(['[64]', 'origins=OrderedSet([div])', ...])
[__fusion] found 1 possible fusions
[__fusion] fusing op1 with op2
[__fusion] completed fusion round (1/10): fused 3 nodes into 2 nodes
[__fusion] ===== attempting fusion (2/10): 2 nodes =====
[__fusion] fuse_nodes_once, candidates:
[__fusion]   ExternKernelSchedulerNode(name='op0')
[__fusion]   OuterLoopFusedSchedulerNode(nodes=op1_op2), snodes: ['SchedulerNode(name=\'op1\'), Reduction([...])', 'SchedulerNode(name=\'op2\'), Pointwise([...])']
[__fusion] found 0 possible fusions
[__fusion] completed fusion round (2/10): fused 2 nodes into 2 nodes
[__fusion] ===== fusion complete (2 iterations) =====
[__fusion] fuse_nodes_once, candidates:
[__fusion]   ExternKernelSchedulerNode(name='op0')
[__fusion]   OuterLoopFusedSchedulerNode(nodes=op1_op2), snodes: [...]
[__fusion] found 0 possible fusions
```

逐行解释：

- `[0/0] [__fusion]` 是日志记录前缀，和“融合了 0 个”无关。时间戳、pid、`scheduler.py:行号` 都在前面，读的时候可以忽略。
- 第一轮的 `candidates:` 有三行，对应三个节点。短格式只给名字、类型和 origins；`origins=OrderedSet([sum_1, relu])` 说明这个 reduction 来自 `sum` 和 `relu` 两个 ATen 操作。
- `found 1 possible fusions`：只有一对通过合法性检查。`fusing op1 with op2` 指名道姓。
- `fused 3 nodes into 2 nodes`：三个变两个，正好少一个。注意措辞是 “fused 3 nodes into 2”，不是“融合了 2 个节点”。
- 第二轮只剩两个节点：`op0` 和融合后的 `op1_op2`。`OuterLoopFusedSchedulerNode(nodes=op1_op2)` 里的 `snodes` 列出两个子节点，说明它是 outer-loop 融合，不是普通 `FusedSchedulerNode`。
- 第二轮 `found 0 possible fusions`：`op0` 是 extern，`op1_op2` 是 outer-loop 融合节点，后端对这两类都不再给候选。收敛。
- `fusion complete (2 iterations)`：两轮结束。
- 最后那组没有 `attempting` 头的 `candidates:` / `found 0` 是 reorder round。它只换循环顺序再试一次，不改变节点数。

为什么 `op0` 一次都没被选中？两条独立的理由：`_can_fuse` 对 “extern 但不是 Triton kernel” 直接返回 `False`；即使绕过这一条，`StarDep` 和 `MemoryDep` 也匹配不上。你在日志里看不到 `cannot fuse op0 with op1: ...`，是因为 `WhyNoFuse` 只在具体检查点被调用时才打印；某些路径在更早的硬规则处就返回了。

### 10.7 融合结果如何落到代码

`ir_post_fusion.txt` 里，`op1` 和 `op2` 变成：

```text
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
op1_op2.writes = [MemoryDep('buf1', c0, {c0: 64}), MemoryDep('buf2', c0, {c0: 64})]
op1_op2.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1_op2.min_input_distance = 1
op1_op2.max_input_distance = 2
```

`output_code.py` 里对应一个 kernel：

```text
cpp_fused_div_relu_sum_0 = async_compile.cpp_pybinding(['float*', 'const float*'], r'''
...
    #pragma omp parallel num_threads(4)
    {
        ...
            for(int64_t x0=0; x0<64; x0+=1)
            {
                float tmp_acc0 = 0;
                at::vec::Vectorized<float> tmp_acc0_vec = ...;
                for(int64_t x1=0; x1<64; x1+=8) { ... tmp_acc0_vec = tmp_acc0_vec + tmp1; }
                tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<float, 1>(...);
                in_out_ptr0[x0] = static_cast<float>(tmp_acc0);
            }
        #pragma omp single
        {
            for(int64_t x0=0; x0<64; x0+=8) { ... tmp3.store(in_out_ptr0 + x0); }
        }
    }
''')
```

结构上就是两段：外层 `x0` 的 reduction，和内层 `x1` 的向量化累加；reduction 结束后，`omp single` 段做 `div 64` 的 epilogue。如果 `op1` 和 `op2` 没融合，这里会是两个 kernel，`buf1` 要完整写回再读入。融合把这次 roundtrip 省掉了，代价是 epilogue 必须等 reduction 完成，不能和 reduction 的内层循环并行。

## 可跑实验

第一段：抓融合日志。

```powershell
$env:TORCH_LOGS = "fusion"
$env:TORCHINDUCTOR_FORCE_DISABLE_CACHES = "1"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py 2>&1 |
    Select-String -Pattern 'attempting fusion|candidates:|SchedulerNode|possible fusions|fusing |fusion round|fusion complete' |
    ForEach-Object { $_.Line }
```

预期输出与 10.6 一致。实际每行前面还有 `V1025 ... scheduler.py:<行号>] [0/0] [__fusion]` 前缀；用 `Select-String` 过滤后仍然保留，方便你确认通道名。

第二段：确认融合落成了一个 kernel。

```powershell
$dbg = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
    Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -LiteralPath (Join-Path $dbg.FullName 'output_code.py') `
    -Pattern 'cpp_fused|#pragma omp|addmm' |
    ForEach-Object { $_.Line.Trim() }
```

预期能看到三行关键信息：`cpp_fused_div_relu_sum_0`、`#pragma omp parallel num_threads(4)` / `#pragma omp single`、`extern_kernels.addmm`。前两个来自融合后的生成 kernel，最后一个来自没被融合的 `op0`。

实验结束后把环境变量清掉，避免影响后面的缓存实验：

```powershell
Remove-Item Env:TORCH_LOGS
Remove-Item Env:TORCHINDUCTOR_FORCE_DISABLE_CACHES
```

一个实测细节：开启 `TORCH_LOGS` 后，日志走 stderr；在 PowerShell 里用 `2>&1` 合并时，即使 4 个结构断言全部 `[PASS]`，外层管道的退出码也可能是 1。判断实验是否成功，看 stdout 里的 `[PASS]` 和产物，不要只看 `$LASTEXITCODE`。

## 源码落点

- `torch/_inductor/scheduler.py`
  - `Scheduler.fuse_nodes`：十轮循环、终止条件、可选的 reorder round。
  - `Scheduler.fuse_nodes_once`：去冗余依赖、打印候选、取候选、排序、融合。
  - `Scheduler.get_possible_fusions`：按共同 buffer 分组，组内两两检查；`max_fusion_buffer_group_pairwise_attempts` 限制每对窗口。
  - `Scheduler.get_possible_fusions_with_highest_priority`：后端优先级过滤。
  - `Scheduler._can_fuse`：流/内存池约束、strict sum、template、extern、nop、grouped 的硬规则。
  - `Scheduler.can_fuse_vertical`：consumer 的 unmet deps 如何被 producer 的 writes 消掉。
  - `Scheduler.fusable_read_and_write`：index/size 匹配、TMP 符号、同步 mode、StarDep 规则。
  - `Scheduler.fusable_weak_dep`：弱依赖的放行条件。
  - `Scheduler.score_fusion_memory` / `score_fusion_key`：分数怎么算、怎么排序。
  - `WhyNoFuse`：拒绝理由的格式化；`cannot fuse op1 with op2: ...` 就是它。
  - `MixOrderReduction` / `NestedReduction` / `has_strict_sum`：reduction 的特殊路径。
- `torch/_inductor/codegen/cpp.py`
  - `CppScheduling.can_fuse_horizontal`：template、节点数上限、`_can_fuse_horizontal_impl`。
  - `CppScheduling.can_fuse_vertical`：template 分支、`_can_fuse_horizontal_impl`、outer-loop 分支。
  - `CppScheduling.can_fuse_vertical_outer_loop`：reduction + pointwise 的 outer-loop 条件。
  - `CppScheduling._why_fuse_nodes`：`SAME_VARS_REDUCE`、`COMPATIBLE_REDUCTION`、`COMPATIBLE_RANGES_NO_REDUCTION`。
  - `CppScheduling._can_fuse_nodes_with_compatible_ranges`：元素数相同但形状不同时的重算条件。
- `torch/_inductor/choices.py`
  - `can_fuse` / `can_fuse_vertical` / `can_fuse_horizontal` / `score_fusion`：`V.choices` 这一层后端策略。
- `torch/_inductor/config.py`
  - `max_fusion_buffer_group_pairwise_attempts`、`aggressive_fusion`、`cpp.max_horizontal_fusion_size`、`benchmark_kernel`、`score_fusion_memory_threshold`、`loop_ordering_after_fusion`、`loop_index_inversion_in_fusion`、`loop_reindexing_after_fusion`。

## 常见坑

1. **`TORCH_LOGS` 变量带尾随空格**。`+fusion` 与 `fusion` 都有效（DEBUG / INFO）；但在 cmd 里写 `set TORCH_LOGS=fusion && ...` 会带进空格，直接报 `Invalid log settings`。PowerShell 用 ` = "fusion"`。

2. **命中缓存后看不到任何融合日志**。缓存命中时调度器根本不跑，`fuse_nodes` 不会执行。需要 `$env:TORCHINDUCTOR_FORCE_DISABLE_CACHES = "1"`，或者换一个没编译过的图。

3. **用退出码判断日志实验是否成功**。日志走 stderr，PowerShell 的 `2>&1` 合并可能把退出码变成 1；本机实测 4 个 `[PASS]` 全过、脚本走到“下一步”，退出码仍是 1。看 `[PASS]` 和产物，不看退出码。

4. **把 `found 0 possible fusions` 当成“没有融合”**。它只说明这一轮没有新的合法候选。lab_a 的第二轮就是 0，但第一轮已经完成了 `op1 + op2`；`fusion complete` 后面的那次 0 是 reorder round 的结果。

5. **以为相邻节点就一定能融合**。`op0` 和 `op1` 相邻，但 `op0` 是 extern、写的是 `StarDep`；两条理由任意一条都足以拒绝。相邻只是必要条件里最弱的一条。

6. **把分数当成合法性**。`score_fusion_memory` 只决定先试哪一对；合法性由 `can_fuse` / `can_fuse_vertical` / `can_fuse_horizontal` 决定。分数为 0 不代表一定不合，分数高也不代表一定合。

7. **融合后还在找 `op1`、`op2`**。节点名变成 `op1_op2`，子节点只在 `snodes` 里。生成 kernel 的名字则来自 origins，例如 `cpp_fused_div_relu_sum_0`，和调度节点名不是一套命名。

## 自我检查

- [ ] 我能说出 `fuse_nodes` 的终止条件，以及为什么 lab_a 两轮就结束。
- [ ] 我能解释 `_can_fuse` 如何用 `ancestors` 区分垂直融合和水平融合。
- [ ] 我能逐条检查 `fusable_read_and_write` 的匹配规则，并说明 `StarDep` 为什么过不了。
- [ ] 我能解释 `op1` 与 `op2` 为什么是 `OuterLoopFusedSchedulerNode`，而不是普通 `FusedSchedulerNode`。
- [ ] 我能说出 reduction 融合的三条边界（outer-loop、strict sum、同步 mode）。
- [ ] 我能从 `found 1 possible fusions` / `fused 3 nodes into 2 nodes` 推出这一轮发生了什么。
- [ ] 我能解释 `fusion complete` 之后那组 `candidates:` 属于哪个阶段。

## 延伸阅读

- **`torch/_inductor/scheduler.py` 的 `fuse_nodes` 到 `can_fuse_vertical`**：按调用顺序读，先看循环，再看 `_can_fuse` 的分支，最后看 `fusable_read_and_write` 的匹配。与本章的关联：所有决策规则的原始实现；重点对照 `WhyNoFuse` 的几种理由。
- **`torch/_inductor/codegen/cpp.py` 的 `CppScheduling`**：读 `can_fuse_horizontal`、`can_fuse_vertical_outer_loop`、`_why_fuse_nodes`。与本章的关联：CPU 后端如何在调度器的合法性之上再加域匹配和节点数上限。
- **PyTorch 2 论文（ASPLOS 2024）**：读 TorchInductor 的 fusion 与 codegen 部分。与本章的关联：解释“fusion-first”设计的动机，以及为什么 reduction 的 epilogue 要单独处理。
- **`compilers/inductor_deep_dive/09-scheduler-data-structures.md`**：读 `MemoryDep`/`StarDep` 和 `unmet_dependencies`。与本章的关联：融合匹配的输入就是那些字段；先能读字段，才能读决策。
- **`compilers/inductor_deep_dive/11-reading-scheduler-ir.md`**：读 `ir_post_fusion.txt` 的 `snodes` 展开。与本章的关联：把“融合决策”与“融合后的 IR 形态”对上，避免把打包误当成重写 loop body。

---

[上一章：调度器数据结构](./09-scheduler-data-structures.md) | [下一章：读调度 IR](./11-reading-scheduler-ir.md)
