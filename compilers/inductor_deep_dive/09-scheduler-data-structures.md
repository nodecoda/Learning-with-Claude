# Ch 9 调度器数据结构：节点、依赖与 buffer

> **目标**：把 `ir_pre_fusion.txt` 里的三类节点、`SchedulerBuffer`、`MemoryDep`/`StarDep`/`WeakDep` 和 `unmet_dependencies` 对上号。读完你能回答：调度器为什么不能直接在 FX 图上融合、`op0` 为什么永远不参与融合、`op1` 的 `MemoryDep('buf0', c0, {c0: 4096})` 到底在说什么。

> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
```

本章不引入新脚本，只读第 1 章 lab_a 已经生成的调度 IR。所有实验入口统一为 `compilers\inductor_deep_dive\code\tools\run.ps1`。

## 为什么

GraphLowering 结束时，你得到的是一串 buffer 和它们的 loop body：`buf0` 由 `addmm` 写，`buf1` 由 `relu + sum` 写，`buf2` 由 `div` 写。这些 buffer 还没有顺序，也没有“谁和谁能合进同一个 kernel”的结论。调度器要补上的正是这一层。

问题在于，这一层不能在 FX 图上直接做。FX 节点是 tensor 级的 `aten.add`、`aten.relu`、`aten.sum`；它只告诉你“这条边存在”，不告诉你“第 0 个元素从哪来、第 4095 个元素写到哪”。两个节点读写同一个 buffer 的不同位置时，FX 只看到两条边，调度器看到的是两个带 index 表达式的 `MemoryDep`。融合合法性最终取决于这些 index 表达式能不能逐元素对上。

于是就出现了第二张图：调度视图。它不改变计算，只给每个 IR 操作配一个调度节点，并给每个 buffer 配一份“谁定义、谁使用、何时能释放”的档案。第 4 章讲的 `TensorBox`/`StorageBox`/`ComputedBuffer` 是“数据长什么样”；本章讲的是“数据被谁在什么位置读写”。

一个真实症状可以说明这层结构的价值。lab_a 的调度 IR 里，`op0` 是 `addmm`（extern kernel），`op1` 是 `relu + sum` 的 reduction，`op2` 是 `div` 的 pointwise。`op0` 与 `op1` 明明相邻，却不能融合；`op1` 与 `op2` 能融合成一个 kernel。原因不在“算子类型”，而在依赖的形态：`op0` 的写是 `StarDep`，它只声明“整块 buffer 归我”，没有逐元素 index，后续节点无法证明读写位置一一对应。读完本章，你能从 `ir_pre_fusion.txt` 直接指出是哪一行阻止了融合。

还有一层容易忽略的差别：调度器里的 `op0`、`op1`、`op2` 不是 FX 里的节点名，也不是源码里的变量名。它们是调度器按自己的顺序重新编号的。真实来源要看 `origins=OrderedSet([...])`，那是 fusion 日志和 `debug_str_short()` 提供的线索。把 FX 名字和调度名字混用，是读 IR 时最常见的错位。

## 主线

### 9.1 调度器在流水线里的位置

`Scheduler.__init__` 的顺序很固定，值得先背下来，因为它决定了你在产物里看到什么：

| 顺序 | 调用 | 你会在哪里看到它 |
| --- | --- | --- |
| 1 | `comms.decide_global_ordering_of_comms` | 集合通信的顺序约束 |
| 2 | `compute_dependencies` | `users`、`read_writes`、`unmet_dependencies` |
| 3 | `topological_sort_schedule` | 节点顺序 |
| 4 | `dead_node_elimination` | 被删掉的 buffer / operation |
| 5 | `compute_ancestors` / `compute_input_distances` | `min_input_distance`、`max_input_distance` |
| 6 | `log_ir_pre_fusion` | `ir_pre_fusion.txt` |
| 7 | `create_foreach_nodes` | foreach 节点 |
| 8 | `fuse_nodes` | `ir_post_fusion.txt`、`TORCH_LOGS=fusion` |
| 9 | `merge_loops` | 仅 GPU / halide 后端相关 |
| 10 | `reorder_for_peak_memory`（默认开启） | 节点顺序可能再次变化 |
| 11 | `codegen` | `output_code.py` |

两个结论：

- `ir_pre_fusion.txt` 写在 DCE 之后、fusion 之前。它是“干净但未融合”的调度图，不是 lowering 后立刻的快照。死节点已经被删掉，所以文件里的节点数不等于 `GraphLowering` 产出的操作数。
- 在读取依赖的 pass 之前，`compute_dependencies` 必须最先跑，因为后面所有 pass 都读 `read_writes` / `unmet_dependencies`。源码里这句注释说得很直白：`Must run first to correctly set dependencies, before all other passes that rely on reading from .read_writes.reads or .unmet_dependencies`。

### 9.2 三类节点

所有调度节点都继承 `BaseSchedulerNode`。它把公共状态集中在一处：

```python
self.outputs = [
    SchedulerBuffer(
        scheduler=self.scheduler,
        node=output,
        defining_op=self,
    )
    for output in node.get_outputs()
]
self.outputs_by_name = {buf.get_name(): buf for buf in self.outputs}
```

这段来自 `BaseSchedulerNode._init_from_node`。每个 `ir.Operation` 的输出 buffer 都会被包成 `SchedulerBuffer`，并记住“定义我的节点是谁”。除此之外，公共字段还包括 `read_writes`、`unmet_dependencies`、`group`、`last_usage`、`min_input_distance` / `max_input_distance`、`mpi_node`（内存规划信息，第 12 章）。

三类具体节点：

| 类 | 包住什么 | 关键方法 | 在 lab_a 里 |
| --- | --- | --- | --- |
| `SchedulerNode` | `ir.ComputedBuffer` 或 `ir.TemplateBuffer` | `_compute_attrs`、`simplify_and_reorder`、`extract_read_writes` | `op1`、`op2` |
| `ExternKernelSchedulerNode` | `ir.ExternKernel`（如 `addmm`） | `codegen` 直接委托给 `node.codegen` | `op0` |
| `FusedSchedulerNode` | 一组 `SchedulerNode` | `fuse()`、`get_nodes()`、`snodes` | 融合后的 `op1_op2` |

`SchedulerNode` 的构造过程决定了后面所有字段：

```python
self._sizes, body = self.node.simplify_and_reorder(...)
self._body = body
device = self.node.get_device_or_error()
group_fn = self.scheduler.get_backend(device).group_fn
self.group = (device, group_fn(self._sizes))
self.set_read_writes(
    dependencies.extract_read_writes(
        self._body, *self._sizes, normalize=should_normalize
    )
)
```

`simplify_and_reorder` 产出两个东西：迭代域的尺寸 `_sizes`，以及真正的 `LoopBody`。`group_fn` 把尺寸折成后端关心的 `(outer, reduction)` 两组 range。`extract_read_writes` 遍历 loop body 里的 `ops.load` / `ops.store`，为每个 buffer 生成一个 `MemoryDep`。所以你在 IR 里看到的 `writes` / `unmet_dependencies`，本质上是从 loop body 反推出来的访问摘要。

`ExternKernelSchedulerNode` 简单得多：它不生成 loop body，`set_read_writes(node.get_read_writes())` 直接拿 extern kernel 声明的读写；`is_extern()` 返回 `True`；它不参与分组，`get_ranges()` 返回 `([], [])`（Triton epilogue 例外）。它的 `codegen` 只有一句委托：

```python
def codegen(self, wrapper: PythonWrapperCodegen) -> None:
    return self.node.codegen(wrapper)
```

`FusedSchedulerNode` 是“假节点”。它不重新计算 loop body，只把若干子节点收进 `snodes`，并把未满足依赖取并集：

```python
# 简化示意：先取并集，再减掉融合内部已经产出的依赖
self.unmet_dependencies = (
    OrderedSet.union(*[x.unmet_dependencies for x in snodes])
    - OrderedSet(...)
)
```

源码里的原话是：`This is a "fake" scheduler node that represents a group of scheduler nodes that are meant to be fused together.` 这句话解释了为什么 `ir_post_fusion.txt` 里 `op1_op2` 下面还能看到 `snodes[0] = op1`、`snodes[1] = op2` 两份完整的 loop body。融合并没有把两份 body 改写成一份，它只是打包；真正的循环合并发生在 codegen（第 13、14 章）。

还有几个旁支节点：`NopKernelSchedulerNode`（占位）、`OutputNode`（图输出的虚拟用户）、`GroupedSchedulerNode`（分组后再展开）、`ExternKernelSchedulerNode` 的 Triton epilogue 变体。它们不影响本章主线，但在 `WhyNoFuse` 的理由里会出现。

### 9.3 SchedulerBuffer：buffer 的调度侧档案

`SchedulerBuffer` 不是 tensor，也不是 storage。它是“这个 buffer 在调度期的全部社交关系”：

| 字段 | 含义 |
| --- | --- |
| `scheduler` | 所属调度器 |
| `node` | 对应的 `ir.Buffer`（`ComputedBuffer` / `ExternKernelOut` 等） |
| `defining_op` | 谁产出它 |
| `users` | `NodeUser` 列表：谁读它、能否原地复用、是不是弱依赖 |
| `mpi_buffer` | 内存规划用的尺寸/后继信息（第 12 章） |

`NodeUser` 只有三个字段，但每个都影响决策：

```text
NodeUser(node=SchedulerNode(name='op2'), can_inplace=True, is_weak=False)
```

- `node`：消费者。
- `can_inplace`：这个读是否和写“同 index 同 size”，因此允许把输出写回输入 buffer。`op1` 的 `buf1` 对 `op2` 是 `True`，这正是 `output_code.py` 里 `buf2 = buf1; del buf1  # reuse` 的来源之一（第 12 章会区分两条 reuse 路径）。
- `is_weak`：这个用户只贡献排序约束，不是真正的读；内存生命周期还要看对应的 `WeakDep.is_fake`（第 12 章）。

`can_free()` 的规则也很关键：

```python
for use in self.users:
    if isinstance(use.node, OutputNode):
        return False
return True
```

只要这个 buffer 被图输出用过，就永远不释放。这与第 12 章“图输出的 `end_step = -1`”是同一件事的两个视角。

`allocate()` 则负责把调度决策翻译成 wrapper 代码。它先看 `should_allocate()`，再看是否有 alias/mutation，最后才决定是普通分配、donated buffer，还是原地复用。读源码时注意：这里已经是 codegen 阶段，发生在 fusion 与内存重排之后。

### 9.4 Dep 家族：MemoryDep、StarDep、WeakDep

依赖不是“边”，而是带语义的对象。三类的区别决定了融合能力：

| 类型 | 有 index 吗 | 能表达逐元素对应吗 | 典型来源 |
| --- | --- | --- | --- |
| `MemoryDep` | 有 | 能 | 生成的 pointwise / reduction kernel |
| `StarDep` | 没有 | 不能 | extern kernel、mutation、fallback |
| `WeakDep` | 没有 | 不适用 | 排序用；`is_fake=True` 的版本不参与生命周期 |

`MemoryDep` 是一个 frozen dataclass：

```python
@dataclasses.dataclass(frozen=True)
class MemoryDep(Dep):
    name: str
    index: sympy.Expr
    var_names: tuple[sympy.Symbol, ...]
    size: tuple[sympy.Expr, ...]
    mode: str | None = None
```

它的 `repr` 就是你在 IR 里看到的形式：

```text
MemoryDep('buf0', c0, {c0: 4096})
```

三个部分分别是：buffer 名、index 表达式、变量范围。`ranges` 属性把 `var_names` 和 `size` 合起来：

```python
@property
def ranges(self) -> dict[sympy.Symbol, sympy.Expr]:
    return dict(zip(self.var_names, self.size))
```

注意 `index` 是“元素偏移表达式”，不是 buffer 的维度下标。`{c0: 4096}` 表示这个表达式里的 `c0` 取值范围是 4096。对 `buf0`（64×64）来说，`4096` 正好是全部元素；对 `buf1`（64）来说，范围是 64。同一个符号 `c0` 在不同 dep 里可以有不同范围，这也是为什么 `MemoryDep` 必须把 `size` 一起带上。

`StarDep` 只声明“整块 buffer”：

```python
class StarDep(Dep):
    name: str
    mode: str | None = None

    @property
    def index(self) -> sympy.Expr:
        raise NotImplementedError("StarDep does not have an index")
```

它没有 index，所以无法证明“你写的第 i 个元素正好是我读的第 i 个元素”。fusion 的 `fusable_read_and_write` 对 `StarDep` 只接受另一种 `StarDep`（且 `mode` 相同）。`op0` 的写就是 `StarDep(name='buf0', mode=None)`，它读的三个输入也都是 `StarDep`。这解释了一个常见疑问：为什么 `addmm` 后面紧跟 `relu` 却不融合——不是 relu 不能进 kernel，而是 extern 的写没有逐元素 index，调度器没有依据把它和 consumer 的读对上。

`WeakDep` 主要表示排序依赖：

```python
@dataclasses.dataclass(frozen=True)
class WeakDep(Dep):
    name: str
    mutating_buf: str
    is_fake: bool = False
```

`is_fake=True` 的 fake 版本不参与内存生命周期；`is_fake=False` 的仍会进入 `succ_nodes`，可能推迟释放。`memory.py` 在收集 buffer 后继时明确过滤的是前者。把二者混为一谈，会让你误判一个 buffer 为什么“看起来没人用却还不释放”。

### 9.5 依赖是怎么建起来的

`compute_dependencies` 做四件事：

1. 建立 `name_to_users`：每个 buffer 名对应一个去重后的用户列表。别名（alias）会把两个名字指向同一个列表，这样 `buf0` 和它的 view 不会各自维护一份用户。
2. 处理 mutation：被原地改写的 buffer 加一个 `StarDep` 或 `WeakDep`，强制“写者排在所有读者之后”。
3. 给图输出挂 `OutputNode`：输出 buffer 永远有用户，因此不会被 DCE 删掉。
4. 把用户列表写回每个 `SchedulerBuffer.users`。

这里有一个容易读错的字段。`set_read_writes` 先把 `unmet_dependencies` 设成全部 reads，然后 `prune_deps()`：

```python
self.unmet_dependencies = OrderedSet(
    dep
    for dep in self.unmet_dependencies
    if dep.name not in self.scheduler.available_buffer_names
)
```

`available_buffer_names` 是图输入、常量、torchbind 常量的集合。所以 `unmet_dependencies` 的准确含义是：**这些读不是入口就有的，必须由别的调度节点产出**。它不是“运行时还没满足”，也不会在执行过程中逐个减少。`met_dependencies` 则是补集：

```text
met_dependencies = read_writes.reads - unmet_dependencies
```

这正好解释了 lab_a 的输出：

- `op0.unmet_dependencies = []`，因为 `addmm` 只读三个图输入，全部在 `available_buffer_names` 里；它们出现在 `met_dependencies`。
- `op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]`，因为 `buf0` 是 `op0` 的产物，不在入口集合里。
- `op2.unmet_dependencies = [MemoryDep('buf1', c0, {c0: 64})]`，同理。

`min_input_distance` / `max_input_distance` 是另一组有用的数字。它们表示“这个节点的输入最早/最晚由前面第几个节点产出”。lab_a 里 `op1` 是 1，`op2` 是 2；`op0` 是 0，因为它的输入都是图输入。`FusedSchedulerNode` 会把子节点的这两个值取 min/max；读 IR 时它们也是判断“这个节点跨了多远”的线索。

### 9.6 把字段和真实输出对上

把 lab_a 的 `ir_pre_fusion.txt` 浓缩成一张表：

| 节点 | 类型 | writes | unmet_dependencies | outputs | group.iteration |
| --- | --- | --- | --- | --- | --- |
| `op0` | `ExternKernelSchedulerNode(ExternKernelOut)` | `StarDep('buf0')` | `[]` | `buf0` 64×64 | 未打印（extern） |
| `op1` | `SchedulerNode(ComputedBuffer)` | `MemoryDep('buf1', c0, {c0: 64})` | `MemoryDep('buf0', c0, {c0: 4096})` | `buf1` 64 | `((64,), (64,))` |
| `op2` | `SchedulerNode(ComputedBuffer)` | `MemoryDep('buf2', c0, {c0: 64})` | `MemoryDep('buf1', c0, {c0: 64})` | `buf2` 64 | `((64,), ())` |

这张表能直接回答三个问题：

- 谁读谁？看 `unmet_dependencies` 的名字。
- 读多少？看 `{c0: ...}` 的范围。`4096` 是 64×64 的全部元素，`64` 是向量长度。
- 能不能融合？看 dep 类型和 index。`op1` 与 `op2` 都是 `MemoryDep`，`op2` 读 `buf1` 的 index 是 `c0`，`op1` 写 `buf1` 的 index 也是 `c0`（在 loop body 里是 `index1 = p0`），所以能对上。`op0` 的写是 `StarDep`，对不上任何 `MemoryDep`。

下一章会把“能不能融合”展开成完整决策树；本章先把结构记牢：节点是壳，`SchedulerBuffer` 是档案，`Dep` 是证据。

## 可跑实验

先生成产物（命中缓存时不会重新编译）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

找到最新的 `.debug` 目录，并只打印调度节点的骨架：

```powershell
$dbg = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
    Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -LiteralPath (Join-Path $dbg.FullName 'ir_pre_fusion.txt') `
    -Pattern '^(op\d+:|op\d+\.|    buf\d+:|class )' |
    ForEach-Object { $_.Line }
```

预期能看到（摘录；实际输出还会包含 `min_input_distance`、`max_input_distance`、`outputs` 里的 layout 等行）：

```text
op0: ExternKernelSchedulerNode(ExternKernelOut)
op0.writes = [StarDep(name='buf0', mode=None)]
op0.unmet_dependencies = []
op0.met_dependencies =
op0.outputs = [
    buf0: ExternKernelOut
op1: SchedulerNode(ComputedBuffer)
op1.writes = [MemoryDep('buf1', c0, {c0: 64})]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1.outputs = [
    buf1: ComputedBuffer
op1.group.device = cpu
op1.group.iteration = ((64,), (64,))
op2: SchedulerNode(ComputedBuffer)
op2.writes = [MemoryDep('buf2', c0, {c0: 64})]
op2.unmet_dependencies = [MemoryDep('buf1', c0, {c0: 64})]
op2.group.iteration = ((64,), ())
```

再做一次只针对依赖的查询：

```powershell
Select-String -LiteralPath (Join-Path $dbg.FullName 'ir_pre_fusion.txt') `
    -Pattern 'unmet_dependencies|met_dependencies|\.users =' |
    ForEach-Object { $_.Line }
```

预期能看到 `op0.met_dependencies` 里的 `arg0_1`、`arg1_1`、`arg2_1`，以及 `buf1.users = [NodeUser(node=SchedulerNode(name='op2'), can_inplace=True, is_weak=False)]`。把这两组输出与 9.4、9.5 的定义对照，确认你能解释：

- 为什么 `op0.unmet_dependencies` 是空的，而 `op1` 不是。
- 为什么 `buf1` 的 `can_inplace` 是 `True`，而 `buf2` 的 `is_weak` 是 `False`。
- 为什么 `op1.group.iteration` 的第二个元组是 `(64,)`，`op2` 是 `()`。

## 源码落点

- `torch/_inductor/scheduler.py`
  - `Scheduler.__init__`：看阶段顺序，尤其是 `compute_dependencies` → `log_ir_pre_fusion` → `fuse_nodes` 的相对位置。
  - `compute_dependencies`：看 `name_to_users` 如何合并别名、如何给 mutation 加 `StarDep`、如何给图输出挂 `OutputNode`。
  - `dead_node_elimination`：看“没有用户”的节点和 buffer 如何被删；注意 `removed_buffers` 会从后续 `read_writes` 里减掉。
  - `BaseSchedulerNode._init_from_node` 与 `set_read_writes`：看 `outputs` 和 `unmet_dependencies` 的初始化。
  - `SchedulerNode._compute_attrs`：看 `_sizes`、`_body`、`group` 和 `extract_read_writes` 的关系。
  - `FusedSchedulerNode.fuse` 与 `snodes`：看“打包”而不是“重写 loop body”。
  - `ExternKernelSchedulerNode`：看 `is_extern()`、`group` 和 `codegen` 的委托。
  - `SchedulerBuffer`：看 `users`、`can_free`、`allocate`、`debug_str`。
- `torch/_inductor/dependencies.py`
  - `MemoryDep`：字段、`repr`、`ranges`、`num_vars`。
  - `StarDep`：为什么 `index` 会抛 `NotImplementedError`。
  - `WeakDep`：`is_fake` 的两种用法。
  - `ReadWrites` 与 `extract_read_writes`：一次 loop body 如何变成 reads/writes。
- `torch/_inductor/ir.py`
  - `ComputedBuffer.get_read_writes`、`ExternKernel`：两类节点分别怎么声明依赖。
- `torch/_inductor/debug.py`
  - `log_ir_pre_fusion` / `log_ir_post_fusion`：文件是怎么写出来的，以及 `TORCH_LOGS=ir_pre_fusion` 时的 `BEFORE FUSION` 头。

## 常见坑

1. **把 `unmet_dependencies` 当成“运行时还没满足的读”**。它其实是“不属于图输入/常量、必须由别的节点产出的读”。`op0` 的 `unmet` 为空，不代表它没有输入，只代表输入都在入口集合里；这些输入在 `met_dependencies` 里。

2. **把 `StarDep` 当 `MemoryDep` 用**。`StarDep` 没有 index，`fusable_read_and_write` 不接受 `StarDep` 对 `MemoryDep` 的匹配。extern kernel 的边界因此很难融合，这不是 bug，是证据不足。

3. **融合后按原名找节点**。`op1` 和 `op2` 融合后名字变成 `op1_op2`，`ir_post_fusion.txt` 里用 `snodes[0]`、`snodes[1]` 展开。按 `op1` 搜索会只命中子节点段落，容易误以为没有融合。

4. **把 FX 节点名当调度节点名**。调度器自己编号 `op0`、`op1`、`op2`；FX 里的 `add`、`relu`、`sum`、`div` 只在 `origins=OrderedSet([...])` 里出现。两者不是一一对应。

5. **忽略 `WeakDep` 的 `is_fake`**。fake weak dep 只参与排序，不参与生命周期；把它算进“还有用户”，会错误地认为 buffer 不能释放。`memory.py` 明确把 `WeakDep(is_fake=True)` 从 `succ_nodes` 里排除。

6. **把 `debug_str_short()` 当成完整 IR**。fusion 日志里的 `SchedulerNode(name='op1'), Reduction([...])` 是短格式，只有类型和 origins；`writes`、`unmet`、`loop body` 只在 `debug_str()` / `ir_pre_fusion.txt` 里。看融合日志找不到 index 时，不要怀疑 IR 没写，是你看错了格式。

## 自我检查

- [ ] 我能说出 `SchedulerNode`、`ExternKernelSchedulerNode`、`FusedSchedulerNode` 各自包住什么，以及 `is_extern()` 在哪里返回 `True`。
- [ ] 我能解释 `MemoryDep('buf0', c0, {c0: 4096})` 的三个部分，并说明 `4096` 与 `64×64` 的关系。
- [ ] 我能解释 `unmet_dependencies` 与 `met_dependencies` 的分工，并说明为什么 `op0` 的 `unmet` 为空。
- [ ] 我能解释 `FusedSchedulerNode` 的 `unmet_dependencies` 为什么是子节点并集，以及它为什么不重写 loop body。
- [ ] 我能按顺序说出 `Scheduler.__init__` 里 `compute_dependencies`、`log_ir_pre_fusion`、`fuse_nodes` 的先后关系。
- [ ] 我能从 `ir_pre_fusion.txt` 里指出是哪一行（`StarDep`）让 `op0` 无法与 `op1` 融合。
- [ ] 我能区分 `NodeUser.can_inplace` 与 `NodeUser.is_weak` 各自影响什么。

## 延伸阅读

- **`torch/_inductor/scheduler.py`**：按 `SchedulerBuffer` → `BaseSchedulerNode` → `SchedulerNode` → `FusedSchedulerNode` → `Scheduler` 的顺序读。与本章的关联：所有字段和阶段顺序的原始定义；先读类定义，再读 `__init__`，最后读 `fuse_nodes`。
- **`torch/_inductor/dependencies.py`**：读 `MemoryDep`、`StarDep`、`WeakDep`、`ReadWrites` 与 `extract_read_writes`。与本章的关联：依赖对象的全部语义，尤其是 `index`/`size`/`mode` 三个字段如何影响融合。
- **PyTorch 2 论文（ASPLOS 2024）**：读 TorchInductor 一节里关于 loop-level IR 与 fusion 的段落。与本章的关联：解释为什么 Inductor 选择“先融合、后 tile”，以及为什么调度层需要独立于 FX。
- **`compilers/inductor_deep_dive/04-graph-lowering-and-ir.md`**：读 `TensorBox`/`StorageBox`/`ComputedBuffer`/layout 的部分。与本章的关联：本章的 `SchedulerBuffer.node` 指向的就是那些 IR 对象，先有数据模型，才有调度视图。
- **`compilers/mini_ai_compiler/12-liveness-analysis.md`**：读生命周期区间那一节。与本章的关联：`last_usage` 与 `SchedulerBuffer.can_free` 的直觉来源，第 12 章会把它接到 Inductor 的真实实现上。

---

[上一章：Practice B：再写一个自定义 Pass](./practice-b-custom-pass.md) | [下一章：融合决策](./10-fusion-decisions.md)
