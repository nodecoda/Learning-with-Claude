# Ch 4 GraphLowering 与 IR：FX 图如何变成 buffer 与循环

> **目标**：读完本章，你能解释 `GraphLowering` 在一次编译里做了什么；能区分 `TensorBox`、`StorageBox` 与真正的 IR 节点；能说清 `FixedLayout` 与 `FlexibleLayout` 的差别；能逐段读懂 `ir_pre_fusion.txt` 里的 `ExternKernelSchedulerNode`、`MemoryDep`、`StarDep`、`group.iteration` 与 loop body；并能指出现有 IR 表示里刻意缺失的东西。

> **环境**：Windows 10 x86_64；Python 3.11.12（`.scratch/aic-env`）；`torch 2.14.1+cpu`；MSVC 14.43（`cl.exe`）。本章命令在仓库根目录执行：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
> ```

## 为什么

到第 3 章为止，我们手里的图还只有 aten 算子：`addmm`、`relu`、`sum`、`div`。这张图能回答"算什么"，不能回答"怎么算得快"。

融合要做的决定全都落在后者：`relu` 和 `sum` 能不能共用一个循环？中间结果 `relu` 要不要真的写进内存？`addmm` 的输出是不是必须物化成一块 buffer？如果两个算子看的是同一块内存，能不能只读一次？这些问题用 aten 图根本表达不出来——aten 算子的语义是"给我一个张量，还你一个张量"，没有位置、没有步长、没有读写粒度。

所以 Inductor 在调度之前先把 aten 图翻译成一种**带内存位置的循环级表示**：

- 每个中间值有一个名字（`buf0`、`buf1`……）和一份布局（shape、stride、offset、device、dtype）；
- 每个计算被写成"对一段迭代空间做一次循环，循环体里读哪些 buffer、写哪些 buffer"；
- 依赖关系从"谁用了谁"变成"谁读/写了哪块内存的哪个范围"。

这套表示由 `GraphLowering` 建立，产物落进 `ir.py` 定义的一组类里。本章就是把这层表示拆开看：它为什么长这样、哪些部分是可变的、调度器接下来还能改什么、哪些信息它故意不管。

## 主线

### 4.1 GraphLowering 是一个解释器，不是一次遍历

`GraphLowering` 继承 `torch.fx.Interpreter`：`run()` 逐个节点执行 FX 图，但每个节点的"执行"不是算数，而是**调用对应的 lowering 函数，把结果换成 IR 节点**。它的类注释写得很直白：遍历 FX 节点、物化输入/输出/常量、把每个调用分派给 Inductor lowering、累积得到的 IR 节点。

分派逻辑在 `GraphLowering.call_function` 里，顺序固定：

1. `operator.getitem` 且参数是容器 → 直接交给 FX 解释器；
2. 目标带 `_inductor_lowering_function` 标记（pattern matcher 直接产出 IR 的函数）→ 直接调用；
3. 目标不在 `lowerings` 字典里 → 走 fallback 路径（`config.implicit_fallbacks` 默认为 `True`，于是 `make_fallback` 现场注册一个 fallback handler）；
4. 否则依次尝试 `user_lowerings`、`lowerings`，都没有就再兜底成 fallback。

注意第 3 步：**"没注册 lowering"不是报错，而是静默降级**。这正是实验 C 里 `mylab.affine_naive` 的表现，第 5 章会展开。

每个进入 IR 的 buffer 都要登记名字，命名规则在两个方法里：

```python
# torch/_inductor/graph.py
def register_operation(self, op: ir.Operation) -> str:
    name = self.qualify_name(f"op{len(self.operations)}")
    ...
def register_buffer(self, buffer: ir.Buffer, *, set_name: bool = False) -> str:
    name = self.qualify_name(f"buf{len(self.buffers)}")
```

所以 `buf0`、`op1` 这种编号只表示**登记顺序**，不是稳定标识符。改一行 lowering、换一个 decomposition，编号就会整体漂移。拿编号做断言是脆的；断言结构特征（有几个 kernel、有几条向量指令、有没有 extern 调用）才稳。

`GraphLowering.run()` 只做一件事：把整趟遍历包进 `dynamo_timed("GraphLowering.run")`。遍历结束后，IR 交给 `codegen()`（C++）或 `codegen_with_cpp_wrapper()`（AOTInductor 的 C++ wrapper）生成 wrapper 与 kernel。

### 4.2 三层盒子：TensorBox、StorageBox、IRNode

`lowering.py` 里的函数不返回"张量"，返回的是 `TensorBox`。这层包装的意义是把**视图**和**存储**分开：

| 层次 | 类 | 职责 | 典型内容 |
| --- | --- | --- | --- |
| 值 | `TensorBox` | 一次 lowering 的结果，可能是视图、常量、或待物化的计算 | `StorageBox`、`ExpandView`、`ReinterpretView`、`ShapeAsConstantBuffer` |
| 存储 | `StorageBox` | 指向一块真正的存储；负责"物化" | `InputBuffer`、`ConstantBuffer`、`ComputedBuffer` |
| 计算 | `IRNode` 子类 | 描述循环 | `Pointwise`、`Reduction`、`Scan`、`FallbackKernel`、`ExternKernel` |

关键在 `StorageBox.realize()`。lowering 阶段，`relu(x)` 得到的是 `TensorBox(StorageBox(Pointwise(...)))`——一块**还没有名字、也没有布局**的计算。只有当某个消费者需要按名字读它时，`realize()` 才把它包成 `ComputedBuffer`，分配 `bufN`，注册进 `graph.buffers`：

```python
self.data = ComputedBuffer(
    name=None,
    layout=FlexibleLayout(device=..., dtype=..., size=..., is_pinned=False),
    data=self.data,
)
self.data.name = V.graph.register_buffer(self.data)
V.graph.register_operation(self.data)
```

这个"延迟物化"就是融合的物理基础。`relu` 和 `sum` 都在同一个 `Pointwise`/`Reduction` 链上时，中间值从来没有变成一块 buffer，也就没有"写一次、读一次"的往返。反过来，一旦你在 lowering 里对中间结果调用了会强制物化的操作（比如某些 view 的 `realize`、或跨设备拷贝），融合机会就被你自己掐掉了。

### 4.3 Layout：哪些布局可以改，哪些不能

`ir.py` 里布局分两类：

- `FixedLayout`：注释就一句 "A Tensor layout we cannot change"。输入张量、extern kernel 的输出、`as_strided` 的结果都是这一类；`make_indexer()` 返回按 size/stride/offset 算下标的闭包。
- `FlexibleLayout`：允许调度器决定最终 stride（`allow_indexing = False`，在冻结前不能直接当下标用）。`StorageBox.realize()` 新建的 `ComputedBuffer` 默认拿到的就是它，布局优化（`layout_opt`）可以在不改变语义的前提下挑一个更合适的 stride。

为什么值得区分？因为**布局是后续所有优化的前提**。调度器要做向量化，需要知道相邻下标是否真的相邻；内存规划要复用 buffer，需要知道两个 buffer 是否可能重叠；转置能不能省掉，取决于消费端的布局是否允许。把"可改"和"不可改"写进类型，比在注释里约定可靠。

一个容易忽略的细节：`FlexibleLayout.contiguous_strides` 是静态方法，按行优先算连续 stride。生成代码里的 `index0 = 64*p0 + p1`（见 4.4 的 loop body）就是这份 stride 的直接结果，而不是从 aten 图里抄来的。

### 4.4 逐段读 ir_pre_fusion.txt

`lab_a_pipeline.py` 打开 `cfg.trace.enabled = True`，编译后 cache 下出现 `<hash>.debug/`，其中 `ir_pre_fusion.txt` 是调度器**建好依赖、还没融合**时的快照。实测片段（64×64 的 `relu(x @ w + b).sum(dim=1) / 64`）：

```text
op0: ExternKernelSchedulerNode(ExternKernelOut)
op0.writes = [StarDep(name='buf0', mode=None)]
op0.unmet_dependencies = []
op0.met_dependencies =
    [   StarDep(name='arg0_1', mode=None),
        StarDep(name='arg1_1', mode=None),
        StarDep(name='arg2_1', mode=None)]
op0.outputs = [
    buf0: ExternKernelOut
    buf0.layout = FixedLayout('cpu', torch.float32, size=[64, 64], stride=[64, 1])
    buf0.users = [NodeUser(node=SchedulerNode(name='op1'), can_inplace=False, is_weak=False)]
]
op0.node.kernel = extern_kernels.addmm
```

三件事值得记住：

- `StarDep` 表示"整块依赖、无法精确到元素"。extern kernel 对调度器是不透明的黑盒：它读三个输入、写 `buf0`，具体读哪些元素不知道，所以只给星号依赖。
- `met_dependencies` 与 `unmet_dependencies` 是同一套依赖的两种视角：前者是"我已经满足的输入"，后者是"我要等的前驱"。`op0` 没有 unmet，因为它只依赖图输入。
- `buf0.layout` 是 `FixedLayout`。extern kernel 的产物布局由外部实现决定，Inductor 不会去改它，这也是 `addmm` 之后常常要多一次拷贝/重排的原因之一。

接着看 reduction：

```text
op1: SchedulerNode(ComputedBuffer)
op1.writes = [MemoryDep('buf1', c0, {c0: 64})]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1.group.iteration = ((64,), (64,))
op1.sizes = ([64], [64])
class op1_loop_body:
    var_ranges = {p0: 64, p1: 64}
    index0 = 64*p0 + p1
    index1 = p0
    def body(self, ops):
        load = ops.load('buf0', self.get_index('index0'))
        relu = ops.relu(load)
        reduction = ops.reduction(torch.float32, torch.float32, 'sum', relu)
        store_reduction = ops.store_reduction('buf1', self.get_index('index1'), reduction)
```

这是全章最该慢读的一段：

- `MemoryDep('buf0', c0, {c0: 4096})`：读 `buf0`，索引表达式是单变量 `c0`，取值覆盖 4096 个元素——正好是 64×64。依赖的粒度是**元素范围**，不是"读了整个张量"这种笼统说法。调度器后面判断能否融合，靠的就是两个 `MemoryDep` 的范围是否有交集。
- `group.iteration = ((64,), (64,))`：`group` 的结构是 `(device, (numel, rnumel))`。第一组是输出空间的范围（64 个元素，对应 `p0`），第二组是归约空间的范围（64 个元素，对应 `p1`）；`sizes` 也按同样顺序给出。两者不是"外层/内层"的嵌套关系，混起来会看不懂融合限制。
- `var_ranges` 与 `index0 = 64*p0 + p1`：`p0` 是输出下标，`p1` 是归约下标；`buf0` 是行优先的 64×64，所以线性下标是 `64*p0 + p1`；`buf1` 是 64 个元素，下标就是 `p0`。
- `body` 是 define-by-run 的：`ops.load` / `ops.relu` / `ops.reduction` / `ops.store_reduction` 在 IR 里是"记录"，要等到 codegen 才变成真正的 C++ 语句。`ops` 是 handler，第 13 章会讲它怎么把同一段 body 映射成标量或向量代码。

最后一段是归约之后的逐元素除法：

```text
op2: SchedulerNode(ComputedBuffer)
op2.writes = [MemoryDep('buf2', c0, {c0: 64})]
op2.unmet_dependencies = [MemoryDep('buf1', c0, {c0: 64})]
op2.group.iteration = ((64,), ())
    ...
    constant = ops.constant(64.0, torch.float32)
    truediv = ops.truediv(load, constant)
    store = ops.store('buf2', ..., truediv, None)
```

`group.iteration` 的第二个元素是空元组，表示"没有归约维"。这一点在融合决策里很关键：两个纯 pointwise 节点可以水平/垂直融合，归约节点和内层循环的融合要受更多限制（第 10 章）。

把三段连起来看，语义链路是：

```text
arg0_1 @ arg1_1 + arg2_1  --addmm(extern)-->  buf0 (FixedLayout 64x64)
buf0 --relu+sum-->  buf1 (ComputedBuffer 64)   # 一个 loop nest
buf1 --/64-->        buf2 (ComputedBuffer 64)  # 另一个 loop nest
```

一轮 `torch.compile` 到这里，已经能看出"哪里可能省一次内存往返"：`buf1` 只被 `op2` 读一次，且两者迭代空间都是 64，把 `op2` 融进 `op1` 就能省掉 `buf1` 的写与读。这正是 `ir_post_fusion.txt` 里 `op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)` 的来源。

### 4.5 buffer 的生命周期：从 IR 名字到 wrapper 里的 del

IR 里的 `bufN` 是逻辑名字，到了 `output_code.py` 才变成真实的分配与释放。实测 wrapper 片段：

```python
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

三点对应关系：

- `buf0` 是 extern kernel 的输出，布局 `(64, 64)/(64, 1)` 与 IR 里的 `FixedLayout` 一致；
- `buf1`/`buf2` 在 IR 里是两个 buffer，在 wrapper 里合并成一次分配（`buf2 = buf1; del buf1  # reuse`）。这是 codegen 期间由内存规划（第 12 章）做出的复用决策，**IR 阶段看不到**；
- `del arg0_1` 这类语句是"这块输入之后不再被读"的显式提示，方便 Python 侧尽早释放引用。

所以读 IR 时要记住：IR 决定"有哪些 buffer、谁读谁写"，内存规划决定"哪两块能共用一块内存"，codegen 决定"最终长什么样"。三者不是一回事。

### 4.6 IR 里刻意没有的东西

这层表示不是"更底层的 FX 图"，它砍掉了很多语义：

- **没有 dtype 提升规则**。`add(Tensor, float)` 该提升成什么 dtype，在 lowering 阶段就由 `type_promotion_kind` 解决掉了（第 5 章）。
- **没有广播**。broadcast 在 lowering 里被展开成 `ExpandView`，IR 里看到的是显式下标映射。
- **没有 autograd**。图在 AOTAutograd 之后已经是前向/反向分开的纯函数图。
- **没有控制流的动态结构**。`if`/`for` 在 Dynamo 阶段要么被展开、要么留下 graph break。

每砍掉一层语义，调度器就少一类需要推理的情况；代价是这些语义必须在更早的阶段做对。理解这条分工，才能判断一个问题该在哪一层修。

## 可跑实验

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

本机实测输出（节选）：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)

========================================================================
1) 编译产物位置
========================================================================
...\code\.cache\tmp_fusion_log\iu\ciuthffwhfhayqfvotrqims4knkmv4djwqtmdr6j553vjzor6b4d.debug
  fx_graph_readable.py       955 B
  ir_pre_fusion.txt         2842 B
  ir_post_fusion.txt        3591 B
  output_code.py            6226 B
```

`ir_post_fusion.txt` 比 `ir_pre_fusion.txt` 大，是因为融合后的节点会展开成 `op1_op2.snodes[0]`、`op1_op2.snodes[1]` 两段，把被融合的成员的依赖、布局、loop body 全部重印一遍。

再看结构统计：

```text
  name         kernel
  vectorized   True
  step         1
  threads      4
  tmp_ops      6
  uses_sin     False
  uses_abs     False
  cpp_lines    108
```

`threads 4` 来自 OpenMP；`tmp_ops 6` 是归约 kernel 的临时变量条数（load、clamp_min 与归约相关的几条）。这些数字是第 14–16 章的入口，本章只用来确认"IR 里的 loop body 确实变成了一条条语句"。

三步自查：

1. 打开 `ir_pre_fusion.txt`，用 4.4 的读法给每个 `opN` 标出"writes / unmet / outputs.layout / 迭代空间"；
2. 打开 `ir_post_fusion.txt`，找出哪些 `opN` 被合成了 `opX_opY`，以及它们的 `unmet_dependencies` 合并后是什么；
3. 打开 `output_code.py`，把每个 `bufN` 的出现位置圈出来，对照 4.5 的 `del` 与 `reuse`。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `torch/_inductor/graph.py` | `GraphLowering`（类注释与 `__init__`） | 它持有哪些图级状态：`sizevars`、`graph_inputs`、`buffers`、`operations`、`device_types` |
| `torch/_inductor/graph.py` | `GraphLowering.call_function` | 分派优先级；`implicit_fallbacks` 分支；`user_lowerings` 优先于 `lowerings` |
| `torch/_inductor/graph.py` | `register_buffer` / `register_operation` | `bufN`/`opN` 的命名来源，以及"编号不稳定"的原因 |
| `torch/_inductor/graph.py` | `run` / `codegen` / `codegen_with_cpp_wrapper` | 遍历入口与两种 codegen 出口 |
| `torch/_inductor/ir.py` | `TensorBox`、`StorageBox.realize` | 延迟物化的实现；`ComputedBuffer` + `FlexibleLayout` 的创建点 |
| `torch/_inductor/ir.py` | `FixedLayout`、`FlexibleLayout` | `make_indexer`、`contiguous_strides`、`allow_indexing` |
| `torch/_inductor/ir.py` | `Pointwise`、`Reduction`、`ComputedBuffer`、`ExternKernel`/`ExternKernelOut` | 四类 IR 节点各自携带什么 |
| `torch/_inductor/scheduler.py` | `Scheduler.__init__` | `compute_dependencies` → `topological_sort_schedule` → `dead_node_elimination` → `log_ir_pre_fusion` |
| `torch/_inductor/debug.py` | `log_ir_pre_fusion` / `log_ir_post_fusion` | `TORCH_LOGS` 通道与 `*.debug` 文件是同一条输出路径的两端 |

读这些文件时的建议：先读类注释和 `__init__`，再读被本章点名的函数体。不要从第一行线性读到尾，`ir.py` 上万行，没有主线会被淹掉。

## 常见坑

1. **以为 lowering 返回的是 `torch.Tensor`。** 返回值是 `TensorBox`，`print` 出来是一串 `TensorBox(StorageBox(ComputedBuffer(...)))`。要形状看 `get_size()`，要 dtype 看 `get_dtype()`，要真实数值只能去跑生成的代码。
2. **在 lowering 里过早 `realize()`。** 一旦中间值变成 `ComputedBuffer`，它就有了名字和存储，融合只能在"读这块 buffer"的层面发生，写回内存的那一次往返省不掉。
3. **把 `ir_pre_fusion.txt` 当成最终 kernel 结构。** 它只是调度器建好依赖、还没融合的快照；融合、内存规划、codegen 都还没发生。`ir_post_fusion.txt` 也不是最终代码，`output_code.py` 才是。
4. **把 `MemoryDep('buf0', c0, {c0: 4096})` 里的 4096 当成形状。** 它表示"索引 `c0` 的取值范围是 4096"，对 64×64 恰好等于元素总数；对带广播或切片的读，范围与形状不是一回事。
5. **用 `buf0`、`op1` 写断言。** 编号随 lowering 实现、pass 顺序、图结构漂移。要断言就断言"kernel 数""是否向量化""有没有 extern 调用"这类结构特征。
6. **以为 `del arg0_1` 是编译器在管内存。** 它只是让 Python 侧尽早丢掉引用；真正的 buffer 复用发生在内存规划，读 IR 时看不到。

## 自我检查

- [ ] 我能说出 `GraphLowering` 与普通 FX `Interpreter` 的区别：每个节点执行的是 lowering，不是算子本身。
- [ ] 我能按顺序背出 `call_function` 的四条分派路径，并解释"没注册 lowering 不报错"的后果。
- [ ] 我能画出 `TensorBox → StorageBox → Pointwise → (realize) → ComputedBuffer` 的转换链，并说明延迟物化为什么能带来融合。
- [ ] 我能解释 `FixedLayout` 与 `FlexibleLayout` 各自允许调度器做什么，以及 `addmm` 的输出为什么是前者。
- [ ] 我能读懂 `MemoryDep` 与 `StarDep` 的区别，并说明 extern kernel 为什么只能给星号依赖。
- [ ] 我能在 `ir_pre_fusion.txt` 里指出输出空间与归约空间（`group.iteration` 的两个分量）。
- [ ] 我能解释 `buf1`/`buf2` 在 IR 里是两个 buffer、在 wrapper 里却共用一块内存。
- [ ] 我知道 IR 层刻意不做 dtype 提升、广播与 autograd，并知道它们分别在哪一层解决。

## 延伸阅读

- **`torch/_inductor/graph.py` 的 `GraphLowering` 类注释与 `call_function`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/graph.py>）：先读类注释建立全局，再带着"四条第分派路径"读 `call_function`。对应本章 4.1。
- **`torch/_inductor/ir.py` 的 `TensorBox`、`StorageBox.realize`、`Layout` 家族**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/ir.py>）：重点看 `realize()` 如何创建 `ComputedBuffer` 并登记名字，以及 `FlexibleLayout` 为什么禁止直接索引。对应 4.2、4.3。
- **TorchInductor 设计说明：define-by-run IR 与 symbolic shapes**（<https://dev-discuss.pytorch.org/t/torchinductor-a-pytorch-native-compiler-with-define-by-run-ir-and-symbolic-shapes/747>）：读"为什么选择 loop-level IR"那一节，对照本章 4.6 的取舍清单。
- **`torch.fx.Interpreter` 文档**（<https://pytorch.org/docs/stable/fx.html>）：读 `run`、`call_function`、`run_node` 的语义，理解 `GraphLowering` 继承后重写了哪些钩子。

---

上一章：[practice-a-pipeline-trace.md](./practice-a-pipeline-trace.md) ｜ 下一章：lowering 规则，把 aten 算子翻译成 IR，并解释"没注册 lowering"会发生什么。

[上一章：Practice A 管线追踪](./practice-a-pipeline-trace.md) | [下一章：Lowering 规则](./05-lowering-rules.md)