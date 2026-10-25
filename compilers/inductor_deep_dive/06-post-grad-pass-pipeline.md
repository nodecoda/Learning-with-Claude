# Ch 6 Post-grad Pass 管线：进入 IR 之前的最后一站

> **目标**：读完本章，你能按源码顺序复述 `fx_passes/post_grad.py::post_grad_passes` 做了哪些事；能说清哪些阶段默认执行、哪些被 config 关掉；能解释 `GraphTransformObserver` 为什么决定了自定义 pass 拿到的是 `Graph` 而不是 `GraphModule`；能指出三个"顺序敏感"的真实例子；并知道用哪两个产物观察 pass 前后的图。

> **环境**：Windows 10 x86_64；Python 3.11.12（`.scratch/aic-env`）；`torch 2.14.1+cpu`。本章命令在仓库根目录执行：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
> ```

## 为什么

AOTAutograd 交出来的是一张"规范化、函数化"的 aten 图。它已经去掉了 autograd 的痕迹，也不再有 Python 控制流，但它仍然是一张**算子图**：每个节点都是一个完整的 aten 调用，没有循环、没有 buffer、没有布局。

在把它交给 `GraphLowering` 之前，Inductor 要再做一轮图级优化。这一轮和前面的 decomposition 不同：decomposition 是把"大算子"拆成"小算子"，保持算子语义；post-grad pass 则是在 aten 层面**改写结构**——合并 `mm`+`add`、删掉空操作、把 batch norm 折进卷积、把一段子图换成更便宜的实现。

问题在于，这些改写谁先谁后并不是随意的。一个 pass 插入的节点，可能正好是另一个 pass 想匹配的模式；一个 pass 依赖的 `meta["val"]`，可能还没被刷新。想知道"为什么我的 pass 看到的图和预期不一样"，就得知道这条管线的真实顺序，以及每个 pass 被包在哪层壳里。

## 主线

### 6.1 入口：forward 和 backward 各跑一次

调用点在 `compile_fx.py`：

```python
def _recursive_post_grad_passes(gm: GraphModule, is_inference: bool = False) -> None:
    if not config.use_post_grad_passes:
        return
    for subgraph in _get_subgraph_names(gm):
        _recursive_post_grad_passes(subgraph, is_inference)
    post_grad_passes(gm, is_inference)
```

三点：

- `config.use_post_grad_passes` 本机为 `True`，这是总开关；
- 先递归处理子图（例如 `cond` 分支里的子图），再处理外层图；
- `post_grad_passes` 对前向图调用一次，对反向图再调用一次。所以一个自定义 pass 在一次训练迭代里可能被调用两次——打印次数对不上时先想到这一点。

`post_grad_passes(gm, is_inference)` 的 docstring 写明：这里的 IR 已经规范化并函数化。这也是整章的前提——你拿到的图没有原地操作、没有未定义的副作用顺序。

### 6.2 真实顺序

下面按 `post_grad_passes` 的源码顺序列出，并标注本机（`torch 2.14.1+cpu`）的默认行为。"门控"一列指代码里那个 `if`：

| # | 阶段 | 门控 / 默认值 | 本机是否执行 |
| --- | --- | --- | --- |
| 1 | `gm.graph.eliminate_dead_code()` | `config.dce`，本机 **False** | 否 |
| 2 | `reorder_for_locality` | `config.reorder_for_locality=True` 且（推理或 `reorder_for_locality_in_training`） | 是（推理） |
| 3 | 创建 `FakeTensorUpdater(gm)` | 无条件 | 是（只建对象） |
| 4 | `post_grad_custom_pre_pass`（可多个，按列表顺序） | `config.post_grad_custom_pre_pass`，默认 `None` | 否（未设置时） |
| 5 | mkldnn 分支：`grouped_gemm_pass` / `concat_linear_woq_int4` | `torch._C._has_mkldnn` 且 `cpp.enable_grouped_gemm_template` / `cpp.enable_concat_linear`，本机均 False | 否 |
| 6 | `remove_profiler_ops` | 无条件 | 是 |
| 7 | `respecialize_current_device` | 无条件 | 是 |
| 8 | `group_batch_fusion_passes(pre_grad=False)` | `config.pattern_matcher=True` | 是 |
| 9 | `remove_noop_ops` | 同上 | 是 |
| 10 | `remove_assert_ops` | 同上 | 是 |
| 11 | `pass_patterns[0]` → `[1]` → `[2]` | 同上 | 是（各一次） |
| 12 | `partitioned_scatter_optimization_pass` | `config.partitioned_scatter_enabled`，默认 False | 否 |
| 13 | `config.post_grad_fusion_options` 里的 pattern pass | 默认 `{}` | 否 |
| 14 | `B2B_GEMM_PASS` | `config.b2b_gemm_pass`，默认 False | 否 |
| 15 | `micro_pipeline_tp_pass` | `config._micro_pipeline_tp`，默认 False | 否 |
| 16 | `fuse_ddp_communication` | `config._fuse_ddp_communication`，默认 False | 否 |
| 17 | `post_grad_custom_post_pass`（可多个） | 默认 `None` | 否（未设置时） |
| 18 | `_chain_random_ops_for_ordering` | `config.fallback_random`，默认 False | 否 |
| 19 | `stable_topological_sort` | 无条件 | 是 |
| 20 | `move_constructors_to_gpu` | 无条件 | 是 |
| 21 | `fake_tensor_updater.incremental_update()` | 无条件 | 是 |
| 22 | `custom_backend_passes` | 按设备注册，通常为空 | 否 |
| 23 | 分布式相关（bucketing、SPMD 校验等） | 各自 config，默认关闭 | 否 |

这张表最反直觉的一行是第 1 行：**代码里确实有 DCE，但本机 `config.dce` 的默认值是 `False`**（`config.py` 里注释写着 `# dead code elimination`，值是 `False`）。所以默认配置下，这一步不执行。看到别处说"post-grad 先做 DCE"时，要意识到那描述的是代码结构，不是本机的实际行为。

第 2 行也值得细看。`reorder_for_locality` 不是简单的拓扑排序，它的做法是**从后往前遍历**，把每个节点的生产者尽量挪到这个节点前面（`node.prepend(other_node)`），从而提高同一段循环/同一块 cache 的局部性。三条保护值得记住：

- 只重排第一个 `copy_` 之前的节点（`copy_` 是函数化图的收尾，重排会破坏原地写语义）；
- 跳过消费随机数状态的节点（`nondeterministic_seeded`），因为 RNG 顺序敏感；
- 分布式场景下跳过 `wait_tensor`，避免把 collective 的等待顺序打乱导致挂起。

自定义 pass 排在第 4 行，位置很关键：**它在 `remove_noop_ops`、`remove_assert_ops` 和三个 `pass_patterns` 之前**。也就是说，你拿到的是"刚重排过、但还没做常规化简"的图。

### 6.3 每个 pass 外面都套了一层 Observer

源码里几乎每个阶段都长这样：

```python
GraphTransformObserver(gm, "remove_noop_ops").apply_graph_pass(remove_noop_ops)
```

`GraphTransformObserver` 在 `torch/fx/passes/graph_transform_observer.py`，构造时接 `gm`，`apply_graph_pass` 的实现是：

```python
def apply_graph_pass(self, pass_fn):
    with self:
        if self._check_disable_pass():
            return None
        with dynamo_timed(f"pass.{self.subsystem}.{self.passname}"):
            return pass_fn(self.gm.graph)
```

这解释了本章最重要的一条接口事实：**Observer 手里已经有 `gm`，所以传给 pass 的只能是 `gm.graph`**。回调签名因此是 `Callable[[Graph], ...]`，不是 `GraphModule`。在自定义 pass 里写 `gm.graph` 会得到 `AttributeError: 'Graph' object has no attribute 'graph'`。

Observer 还做三件事：

1. **禁用开关**：`passname.upper()` 若出现在 `config.disabled_passes`（可用环境变量 `TORCHINDUCTOR_DISABLED_PASSES` 设置）里，这个 pass 直接跳过；配置了 subsystem 时还会问 `CompilerBisector`。想二分定位"哪个 pass 改坏了我"，这是入口。
2. **计时**：`dynamo_timed(f"pass.{subsystem}.{passname}")`，在 profiler / 编译事件里能看到每个 pass 的耗时。
3. **来源追踪**：当 `config.trace.log_url_for_graph_xform` 不为 `None`，或 provenance tracking 打开时，Observer 会注册创建/删除/替换钩子，给每个新节点写 `meta["from_node"]`，并把 pass 前后的 dot 图落盘。注意它的 `active` 判定只看这两项，所以默认配置下这层追踪是关闭的，几乎零开销。

还有一个容易忽略的细节：`FakeTensorUpdater` 在第 3 行创建，但 `incremental_update()` 直到第 21 行才调用。中间所有 pass 插入的新节点，其 `meta["val"]` 在管线结束前都不会被重算。如果你在自定义 pass 里依赖"改写后节点的 shape 元数据"，就会拿到旧值或 `None`。`FakeTensorUpdater` 的类注释说得很清楚：它用节点哈希检测变化，只在 `incremental_update` 时对变化节点及其用户重算。

### 6.4 三个顺序敏感的例子

**例一：`x * 1` 在进入自定义 pass 之前就没了。** 实测中，把 `x * 1` 写进模型，在 `post_grad_custom_pre_pass` 里打印节点，看不到 `aten.mul` 那一节点。原因是更早的阶段（Dynamo/AOTAutograd 的化简）已经把它折叠掉了。这带来一个写作/教学上的直接后果：教材里的改写例子不能用会被提前折叠掉的式子，要用真正存在于图里的结构，比如 `relu(x) + relu(-x)`（第 7 章）。

**例二：`mm` + `add` 合成 `addmm`。** 这是 post-grad 段里真实发生的结构改写。`lab_a_pipeline.py` 的 debug 目录里，`fx_graph_readable.py`（pass 之前）是：

```python
mm: "f32[64, 64]" = torch.ops.aten.mm.default(arg0_1, arg1_1)
add: "f32[64, 64]" = torch.ops.aten.add.Tensor(mm, arg2_1)
relu: "f32[64, 64]" = torch.ops.aten.relu.default(add)
```

同一个目录的 `fx_graph_transformed.py`（`_recursive_post_grad_passes` 之后立即写出）是：

```python
addmm_default: "f32[64, 64]" = torch.ops.aten.addmm.default(arg2_1, arg0_1, arg1_1)
relu: "f32[64, 64]" = torch.ops.aten.relu.default(addmm_default)
```

这就是"pass 改写结构"的直接证据，也解释了第 4 章里 `op0.node.kernel = extern_kernels.addmm` 的来历：IR 里之所以用 extern 的 `addmm`，是因为图在这一步已经被合并。

**例三：`stable_topological_sort` 在第 19 行。** 你在自定义 pass 里插入的节点，位置会被重排到满足依赖关系的最早位置。所以"我明明插在 `relu` 之前"这种预期不成立；要控制语义，靠的是数据依赖（新节点读什么、谁读新节点），不是插入位置。

### 6.5 开关：哪些能关，关了会怎样

常用开关与观察方式：

| 开关 | 作用 | 关掉的后果 |
| --- | --- | --- |
| `config.pattern_matcher=False` | 跳过第 8–14 行整块 | 常见融合与改写全部消失，kernel 数上升 |
| `config.reorder_for_locality=False` | 跳过第 2 行 | 图顺序回到 AOTAutograd 的原始拓扑序 |
| `config.dce=True` | 打开第 1 行 | 先删一遍死代码，可能改变后续 pass 看到的结构 |
| `config.disabled_passes="..."` | 按 pass 名跳过 | 只影响被点名的阶段，适合二分 |
| `config.post_grad_custom_pre_pass` | 第 4 行 | 你的 pass 排在常规化简之前 |
| `config.post_grad_custom_post_pass` | 第 17 行 | 排在 pattern 与拓扑排序之间 |

比较推荐的调试姿势不是"一个个关"，而是先看产物：`fx_graph_readable.py` 与 `fx_graph_transformed.py` 的差异就是这一段管线的净效果；`TORCH_LOGS=post_grad_graphs` 会在每次关键改写后打印图（`post_grad_graphs` 为 INFO，`+post_grad_graphs` 为 DEBUG）。

### 6.6 这条管线不做什么

- **不做 lowering**。第 11 行的 pattern 可以调用 `register_lowering_pattern` 直接产出 IR 函数，但常规 pattern 只是在 aten 层换节点。
- **不做布局决策**。布局要等 `GraphLowering` 建 buffer 时才有对象可谈。
- **不做内存规划**。buffer 复用发生在 codegen 之后的 `memory.py`。
- **不保证图不变**。`stable_topological_sort` 每次都会重排；你的 pass 必须是"对图做变换"，不能假设输入图的节点顺序稳定。

### 6.7 把顺序读成一张图

把 6.2 的表压缩成一张流程图，方便贴在显示器边上：

```text
gm (forward / backward)
  |
  |  [gate] config.dce
  +--> eliminate_dead_code()          # 默认关闭
  |
  +--> reorder_for_locality           # 生产者尽量靠近消费者
  |
  +--> FakeTensorUpdater(gm)          # 只建对象，不重算
  |
  +--> post_grad_custom_pre_pass      # <-- 你的 pass 在这里
  |
  +--> remove_profiler_ops
  +--> respecialize_current_device
  |
  |  [gate] config.pattern_matcher
  +--> group_batch_fusion_passes
  +--> remove_noop_ops
  +--> remove_assert_ops
  +--> pass_patterns[0] -> [1] -> [2]
  +--> post_grad_fusion_options (opt-in)
  |
  +--> post_grad_custom_post_pass     # <-- 想看到 pattern 改写结果就用这里
  |
  +--> stable_topological_sort
  +--> move_constructors_to_gpu
  +--> fake_tensor_updater.incremental_update()
  |
  v
GraphLowering(gm)                     # 第 4 章从这里开始
```

这张图有两个"用户入口"：pre 在常规化简之前，post 在 pattern 之后。选择哪一个，等价于选择"我要看到原始结构"还是"我要看到优化后的结构"。

第三个入口不在图里，但同样重要：`config._pre_fusion_custom_pass` / `_post_fusion_custom_pass` 是**调度期**的钩子（第 9–10 章），那时对象已经是 `BaseSchedulerNode` 列表，不再是 FX 图。三条入口分属三个层次，混用会得到类型错误，而不是"改写没生效"。
## 可跑实验

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

脚本会打印 debug 目录。用该目录里的两个文件做对照（本机实测路径形如 `...\.cache\tmp_fusion_log\iu\<hash>.debug\`）：

```text
  fx_graph_readable.py       955 B
  ir_pre_fusion.txt         2842 B
  ir_post_fusion.txt        3591 B
  output_code.py            6226 B
```

`fx_graph_readable.py` 是 post-grad **之前**的图，`fx_graph_transformed.py`（脚本没打印，但同目录存在）是**之后**的图。用编辑器 diff 一下，能看到 `mm`+`add` 被合成 `addmm`，节点数从 5 个 aten 调用降到 4 个。

想直接看日志，可以在同一命令行前加环境变量：

```powershell
$env:TORCH_LOGS = "post_grad_graphs"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py
Remove-Item Env:\TORCH_LOGS
```

`lab_b_pass.py` 自带一个自定义 pre pass，所以这次运行里管线第 4 行会真的执行，日志中能看到 "AFTER POST GRAD" 的图。脚本自身也会打印"pass 看到的图"的节点序列。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `torch/_inductor/fx_passes/post_grad.py` | `post_grad_passes` | 全章的主函数；按 6.2 的表逐行对照 |
| `torch/_inductor/fx_passes/post_grad.py` | `reorder_for_locality` | `prepend` 的实现、`copy_` 与 RNG 两个保护 |
| `torch/_inductor/fx_passes/post_grad.py` | `pass_patterns` | 三个 `PatternMatcherPass` 的顺序容器 |
| `torch/_inductor/compile_fx.py` | `_recursive_post_grad_passes` | 子图递归、`use_post_grad_passes` 总开关 |
| `torch/_inductor/compile_fx.py` | `V.debug.fx_graph` / `V.debug.fx_graph_transformed` 调用点 | 两个产物分别在什么时刻写出 |
| `torch/_inductor/fx_utils.py` | `FakeTensorUpdater` | 哈希检测、`incremental_update` 的重算范围 |
| `torch/fx/passes/graph_transform_observer.py` | `GraphTransformObserver.apply_graph_pass` | 为什么传 `Graph`；`disabled_passes`；provenance 钩子 |
| `torch/_inductor/debug.py` | `fx_graph` / `fx_graph_transformed` | 文件名与落盘条件（`trace.enabled`） |
| `torch/_inductor/config.py` | `dce`、`pattern_matcher`、`reorder_for_locality*`、`post_grad_*` | 默认值，别凭记忆 |

## 常见坑

1. **照着文章背顺序，不照源码。** 不同版本顺序会变，本机 `config.dce` 甚至是 `False`。改动前先在 `post_grad.py` 里搜 `GraphTransformObserver`，把所有 `apply_graph_pass` 按出现顺序列出来，那才是你这台机器上的顺序。
2. **以为自定义 pass 一定第一个跑。** `post_grad_custom_pre_pass` 排在 `reorder_for_locality` 之后、`remove_noop_ops` 之前。图已经被重排过，但还没做常规化简；看到的 `relu` 可能还没被 pattern 改写成别的形式。
3. **在 pass 里依赖 `meta["val"]`。** `FakeTensorUpdater.incremental_update()` 在管线末尾才调用；你在第 4 行插入的新节点没有可靠的 fake tensor 元数据。
4. **以为插入位置会被保留。** 第 19 行会做 `stable_topological_sort`。插入位置只影响一件事：插入时是否满足"节点在使用者之前"的合法性。
5. **把日志变量写脏。** `TORCH_LOGS=+post_grad_graphs` 本身有效（DEBUG 级）；会炸的是 cmd 里 `set TORCH_LOGS=fusion && ...` 留下的尾随空格。
6. **关掉 `pattern_matcher` 来"简化观察"。** 关掉之后图的结构和你线上跑的不是同一张，看到的 IR 已经失去参考价值。定位问题优先用 `disabled_passes` 精确关一个 pass。

## 自我检查

- [ ] 我能按顺序说出 `post_grad_passes` 里前 11 个阶段，并指出自定义 pre pass 的位置。
- [ ] 我知道本机 `config.dce` 默认为 `False`，因此第 1 行的 DCE 默认不执行。
- [ ] 我能解释 `reorder_for_locality` 为什么从后往前遍历，以及它跳过哪两类节点。
- [ ] 我能说明 `GraphTransformObserver.apply_graph_pass` 为什么把 `gm.graph` 传给 pass，而不是 `gm`。
- [ ] 我能举出 `x * 1` 被提前折叠的例子，并说明它对教学例子的影响。
- [ ] 我能用 `fx_graph_readable.py` 与 `fx_graph_transformed.py` 的 diff 证明 post-grad 段改写了图。
- [ ] 我知道 `FakeTensorUpdater` 在管线开头创建、结尾才更新，因此新节点没有可信的 shape 元数据。
- [ ] 我能说出 `config.disabled_passes` 与 `config.pattern_matcher=False` 在调试上的区别。

## 延伸阅读

- **`torch/_inductor/fx_passes/post_grad.py::post_grad_passes`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/fx_passes/post_grad.py>）：本章的主线源码。建议边读边把每个 `GraphTransformObserver(...).apply_graph_pass(...)` 抄成一张表。
- **`torch/fx/passes/graph_transform_observer.py`**（<https://github.com/pytorch/pytorch/blob/main/torch/fx/passes/graph_transform_observer.py>）：读 `apply_graph_pass`、`_check_disable_pass`、`get_node_creation_hook` 三处，理解 pass 外壳与 provenance。
- **`torch/_inductor/fx_utils.py::FakeTensorUpdater`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/fx_utils.py>）：读类注释与 `incremental_update`，理解"为什么不在每个 pass 后重算元数据"。
- **`torch/_inductor/config.py` 中 `dce`、`pattern_matcher`、`reorder_for_locality` 一带**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/config.py>）：确认默认值，避免把别的版本的默认行为套到本机。

---

上一章：[Lowering 规则](./05-lowering-rules.md) ｜ 下一章：亲手写一个改变生成代码的自定义 pass。

[上一章：Lowering 规则](./05-lowering-rules.md) | [下一章：写一个自定义 Pass](./07-write-a-custom-pass.md)
