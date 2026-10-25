# Ch 2 三张图与两道边界

> 目标：分清一次编译里同时存在的三张图：Dynamo 捕获的 FX 图、AOTAutograd 交出的 core ATen 图、调度器手里的 loop-level IR；并认识它们之间的两道边界。读完本章，你能在看到任意一段产物时先判断“这是哪张图”，再决定哪些问题问得有意义。

> 环境：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口 `code/tools/run.ps1`。

## 为什么

“图”在 Inductor 这条链路上至少有三层含义，而三层用的是三套词汇：

- FX 图里，节点是 Python 层的调用，比如 `torch.sin`、`operator.add`。
- core ATen 图里，节点是 `aten` 算子，比如 `torch.ops.aten.mm.default`、`torch.ops.aten.relu.default`。
- loop-level IR 里，节点是“一个循环嵌套 + 若干 buffer 读写”，出现的是 `ops.load`、`ops.reduction`、`var_ranges`。

如果你不知道自己在看哪一层，就会问出层与层不匹配的问题：在 FX 图里找 `buf0`，在 IR 里找 `aten.relu`，在 C++ 里找 guard。三类问题都得不到答案，而你会误以为是产物缺了东西。

本章用两道边界把这些层分开。第一道在 Python 与图之间：Dynamo 只能捕获它看得懂的连续片段，看不懂的地方就切开，于是有了 graph break 和多张图。第二道在算子与循环之间：lowering 把每个算子变成 buffer 与循环体，从此“算子名”消失，只剩“下标表达式”。认清这两道边界，第 4 章往下读源码时才知道该在哪个文件里找哪类信息。

## 主线

### 2.1 第一张图：Dynamo 捕获的 FX 图

Dynamo 在字节码层面跟踪执行，把张量运算记录成 FX 图。它的节点是 Python 调用，不是 aten 算子；它的元数据是 guards，不是 buffer。

判断一段文本是不是 Dynamo 图，有个简单办法：看节点里写的是不是 Python 名字。下面这段 FX 图（`torch.fx.Graph` 的 `print` 输出形态）里，算子写成 `torch.sin`、`operator.add`：

```python
def forward(self, x):
    sin = torch.sin(x)
    add = sin + 1.0
    return add * 2.0
```

`torch._dynamo.explain` 是把这张图连同“为什么切开”一起打印出来的工具。本机对一个带副作用的函数实测：

```python
import torch
import torch._dynamo as dyn

def f(x):
    y = torch.sin(x) + 1.0
    print('side effect')   # graph break
    return y * 2.0

exp = dyn.explain(f)(torch.randn(8))
```

输出（本机实跑）：

```text
type        : ExplainOutput
graph_count : 2
op_count    : 3
ops_per_graph: [[<built-in method sin of type object at 0x...>, <built-in function add>], [<built-in function mul>]]
break_types : ['GraphCompileReason']
n_guards    : 19
```

四个字段的读法：

- `graph_count = 2`：函数被切成两张图，`print` 是切点。
- `op_count = 3`：两张图加起来的算子数（`sin`、`add`、`mul`）。
- `ops_per_graph`：列表的列表，第一张图 2 个算子，第二张图 1 个。它不是扁平列表，取用时不要当下标访问。
- `out_guards`：19 条 guards。guards 是“这张图在什么条件下有效”的断言集合。

`ExplainOutput` 是一个对象，字段用属性访问。它不是元组，`exp[0]` 一类写法不成立。

### 2.2 边界一：图与 Python 解释器之间

graph break 是 Dynamo 的边界。它在三种情况下出现：遇到它不支持的调用（例如某些第三方库、`print`）；遇到依赖张量数值的 Python 控制流；遇到显式要求回退的标记。

这条边界带来的后果很实际：

1. **一张函数变成多张图，每张图单独编译。** 本机上面那个例子就是 2 张。
2. **张量要在边界上传递。** 前一张图的输出被搬到 Python 侧，再作为后一张图的输入，形状与 dtype 必须能被 guards 描述。
3. **guards 数量随捕获内容增长。** 19 条 guards 意味着每次调用都要检查 19 个条件；条件不满足就重新捕获。

这里要区分两件常被混为一谈的事：graph break 是“图在哪里断开”，guards 是“这张图在什么条件下有效”。前者解释图的数量，后者解释重编译。第 20 章会把 guards 与动态 shape 的相互作用讲透。

### 2.3 第二张图：core ATen 图

AOTAutograd 把 FX 图接过来，输出一张只含 core ATen 算子的图。两次“翻译”发生在这一步：把 Python 层调用换成 `aten` 算子；把带副作用的操作函数化。

lab_a 的模型在 `fx_graph_readable.py` 里长这样（本机实测）：

```python
class <lambda>(torch.nn.Module):
    def forward(self, arg0_1: "f32[64, 64]", arg1_1: "f32[64, 64]", arg2_1: "f32[64]"):
        mm: "f32[64, 64]" = torch.ops.aten.mm.default(arg0_1, arg1_1);  arg0_1 = arg1_1 = None
        add: "f32[64, 64]" = torch.ops.aten.add.Tensor(mm, arg2_1);  mm = arg2_1 = None
        relu: "f32[64, 64]" = torch.ops.aten.relu.default(add);  add = None
        sum_1: "f32[64]" = torch.ops.aten.sum.dim_IntList(relu, [1]);  relu = None
        div: "f32[64]" = torch.ops.aten.div.Tensor(sum_1, 64);  sum_1 = None
        return (div,)
```

这段文本能回答四类问题：有哪些算子（5 个）；它们的顺序；每个中间值的 shape（`f32[64, 64]`、`f32[64]`）；哪些值在此之后不再使用（`; x = None`）。

它不能回答的是：哪些算子会合进同一个 kernel；中间 buffer 有多大；循环怎么写。这三类问题属于下一张图。

还有一个容易踩的细节：这份文件是 post-grad passes **之前**的图。同一目录下的 `fx_graph_transformed.py` 是 passes **之后**的图，里面 `mm` 与 `add` 已经合成 `aten.addmm.default`，算子数从 5 变成 4（字节数 955 → 917）。第 3 段不改变表示层，所以两张图能用同一套符号读，只是算子集合不同。把两份文件当成“同一层的两个时刻”，而不是“两个不同的层”。

一个容易误读的点：`div.Tensor(sum_1, 64)` 里的 64 是标量常量，不是张量。到了 IR 层，它变成 `ops.constant(64.0, torch.float32)`，再变成 C++ 里的 `auto tmp1 = static_cast<float>(64.0);`。同一条信息在三张图里的三种写法，第 3 章会反复用到。

### 2.4 边界二：从算子到循环

第二条边界在 `GraphLowering` 里。它遍历 ATen 图，为每个算子产出 loop-level IR。从这里开始，产物里不再出现 `aten.relu`，取而代之的是 buffer 与循环体。

三道变化值得逐个看：

**第一，值变成了 buffer。** ATen 图里的 `relu` 是一个值；IR 里它不再独立存在，而是被吸收进 `buf1` 的计算里：

```text
op1.outputs = [
    buf1: ComputedBuffer
    buf1.layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
]
```

`buf1` 是 64 个 float32，行主序。它没有“relu 过”的标记：relu 只是产生这个 buffer 的表达式的一部分。

**第二，循环变量与下标分开了。** 归约节点的循环体是：

```text
class op1_loop_body:
    var_ranges = {p0: 64, p1: 64}
    index0 = 64*p0 + p1
    index1 = p0
```

`p0`、`p1` 是循环变量，范围都是 64；`index0`、`index1` 是下标表达式。注意 `var_ranges` 不是 shape：shape 是 `[64, 64]`（`buf0` 的布局），`var_ranges` 是“这个循环要迭代多少”。两者对这例子恰好都是 64，但含义不同。第 4 章会给出形状不一致的例子。

**第三，算子变成了 `ops` 调用。** 循环体里出现的是 `ops.load`、`ops.relu`、`ops.reduction`、`ops.store_reduction`。这一层是 Inductor 自己的 IR，不是 FX，也不是 ATen。`ops` 的方法名来自 `ops_handler` 的协议，第 13 章会展开。

### 2.5 第三张图：调度 IR

调度器读入 IR，做依赖分析，再把能放进同一循环的节点合并。它的产物还是“节点列表”，但节点类型换成了 `SchedulerNode`、`ExternKernelSchedulerNode`、`FusedSchedulerNode`。

`ir_pre_fusion.txt` 里三个节点，`ir_post_fusion.txt` 里两个：

```text
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
op1_op2.writes = [MemoryDep('buf1', c0, {c0: 64}), MemoryDep('buf2', c0, {c0: 64})]
op1_op2.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
```

依赖写成了 `MemoryDep('buf0', c0, {c0: 4096})`：读 `buf0`，下标表达式是 `c0`，范围 4096（因为 `buf0` 是 64×64，被压平成一维）。这就是第三张图的词汇：不带算子名，只带 buffer 名、下标表达式与范围。

融合日志（`TORCH_LOGS` 的 `fusion` 通道）把决策过程写得很直白，本机在空缓存下实跑一次得到（节选，省略了 `stack_traces` 的大段内容）：

```text
[__fusion] ===== attempting fusion (1/10): 3 nodes =====
[__fusion] fuse_nodes_once, candidates:
[__fusion]   ExternKernelSchedulerNode(name='op0')
[__fusion]   SchedulerNode(name='op1'), Reduction(['[64]', 'sum', ...])
[__fusion]   SchedulerNode(name='op2'), Pointwise(['[64]', ...])
[__fusion] found 1 possible fusions
[__fusion] fusing op1 with op2
[__fusion] completed fusion round (1/10): fused 3 nodes into 2 nodes
[__fusion] ===== attempting fusion (2/10): 2 nodes =====
[__fusion] found 0 possible fusions
```

这段日志的价值在于它把“节点分类”说穿了：`op1` 是 `Reduction`，`op2` 是 `Pointwise`，`op0` 是 `ExternKernelSchedulerNode`。融合只发生在后两者之间，因为它们的循环空间都是 `[64]`。第 10 章会把 `can_fuse_vertical` / `can_fuse_horizontal` 的判据拆开。

### 2.6 三道判据：先确认自己在看哪张图

把三张图的“指纹”列成表，遇到陌生产物先对一遍：

| 你看到的符号 | 说明在哪一层 | 该层能回答的问题 |
| --- | --- | --- |
| `torch.sin`、`operator.add` | Dynamo FX 图 | 图在哪断开、guards 覆盖了什么 |
| `torch.ops.aten.*`、`f32[64, 64]` | core ATen 图 | 算子序列、shape、生命周期 |
| `TensorBox`、`ComputedBuffer`、`FixedLayout` | loop-level IR | buffer 多大、布局如何 |
| `ops.load`、`ops.reduction`、`var_ranges` | loop-level IR 的循环体 | 循环怎么迭代、下标怎么算 |
| `SchedulerNode`、`MemoryDep`、`OuterLoopFusedSchedulerNode` | 调度 IR | 谁和谁融合、依赖多重 |
| `Vectorized<float>`、`#pragma omp`、`loadu` | 生成代码 | 向量宽度、线程数 |

一个实用的自查动作：拿到一份不认识的产物，先在第 2 列的符号表里找命中项，再看它属于哪一层。找不到命中项，说明它多半是外部 kernel 的产物或者某个 pass 的调试输出，那要按那一层的规则读。

## 可跑实验

第一步，跑主线实验，拿到三张图的落盘形态：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

本机输出（节选）：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)
1) 编译产物位置
  fx_graph_readable.py       955 B
  ir_pre_fusion.txt         2842 B
  ir_post_fusion.txt        3591 B
  output_code.py            6226 B
```

第二步，把三份产物里的“指纹”逐个数出来，填进下表。括号里是本机实测值，你可以用来对照：

| 文件 | 要找的符号 | 本机计数 |
| --- | --- | --- |
| `fx_graph_readable.py` | `torch.ops.aten` | 5 |
| `ir_pre_fusion.txt` | `SchedulerNode(` | 2（另有 1 个 `ExternKernelSchedulerNode`） |
| `ir_pre_fusion.txt` | `var_ranges` | 2（`op1` 与 `op2` 各一处） |
| `ir_post_fusion.txt` | `OuterLoopFusedSchedulerNode` | 1 |
| `output_code.py` | `Vectorized<float>` 与 `#pragma omp` | 都有 |

第三步，看 Dynamo 侧。把 2.1 节那段 `explain` 代码存成临时脚本，用同一个入口跑（把脚本路径换成你自己的）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

把路径换成你的脚本后，期望看到 `graph_count : 2` 与 `break_types : ['GraphCompileReason']`。如果你删掉 `print('side effect')` 再跑一次，`graph_count` 会变成 1。这一步是本课程里最便宜的“亲手制造边界”实验。

第四步，看融合日志。`+` 表示 DEBUG，`fusion` 表示 INFO：

```powershell
$env:TORCH_LOGS="fusion"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

期望看到 `attempting fusion (1/10): 3 nodes`、`found 1 possible fusions`、`fusing op1 with op2`、`completed fusion round (1/10): fused 3 nodes into 2 nodes`。如果什么都没打印，先确认这次是不是命中了缓存：缓存命中时调度器不跑，日志自然也不会出现。

## 源码落点

- `torch/_dynamo/`（`eval_frame.py` 里的 `explain`）：读 `explain` 如何包一层并返回 `ExplainOutput`；看 `ExplainOutput` 的字段定义，确认为什么不能用下标访问。
- `torch/_inductor/compile_fx.py`：读 `compile_fx`，看 FX 图进入 Inductor 的位置；这是第一道边界之后、第二道边界之前的入口。
- `torch/_inductor/graph.py`：读 `GraphLowering.run`，看 ATen 算子是逐个被 lowering 的；第二道边界就在这个循环里。
- `torch/_inductor/lowering.py`：读 `register_lowering` 与 `fallback_handler`，理解“算子没有 lowering 时会发生什么”；第 5 章会展开。
- `torch/_inductor/scheduler.py`：读 `SchedulerNode` 与 `fuse_nodes`，把 2.5 节的日志逐行对到代码上。
- `torch/_inductor/loop_body.py`：读 `LoopBody`，看 `var_ranges` 与 `index` 是怎么被保存和序列化的。

## 常见坑

1. **把 `fx_graph_readable.py` 当 Dynamo 图。** 它是 AOTAutograd 之后的 core ATen 图，里面不会出现 graph break 的位置信息。
2. **在 IR 里找 `aten` 算子名。** lowering 之后算子名只剩在 `origins` / 调度日志里；IR 层写的是 `ops.relu` 这种。
3. **把 `var_ranges` 当 shape。** `var_ranges` 描述循环迭代范围，shape 在 `buf.layout` 里。两者恰好相同只是本例的巧合。
4. **`ExplainOutput` 用下标访问。** 它是对象，字段是 `graph_count`、`op_count`、`ops_per_graph`、`out_guards`、`break_reasons`。
5. **把 `ops_per_graph` 当扁平列表。** 它是列表的列表，每个元素对应一张图。
6. **日志变量带尾随空格。** 在 cmd 里写 `set TORCH_LOGS=fusion && ...` 会把空格带进变量，本版本会直接报 `Invalid log settings`；PowerShell 用 ` = "fusion"` 更稳。
7. **缓存命中时找不到融合日志。** 调度器没有跑，就没有日志；换一个空缓存目录再看。

## 自我检查

- [ ] 我能说出三张图各自的节点类型，并举出一个只属于该层的符号。
- [ ] 我能解释 graph break 与 guards 是两件不同的事。
- [ ] 我能从 `fx_graph_readable.py` 数出算子个数并说出每个 intermediate 的 shape。
- [ ] 我能解释 `var_ranges` 与 `buf.layout` 的区别。
- [ ] 我能读懂 `MemoryDep('buf0', c0, {c0: 4096})` 里三个参数各自的意思。
- [ ] 我能在不看讲义的情况下写出一次 `explain` 调用，并说出 `ExplainOutput` 的五个字段。
- [ ] 我知道融合日志在什么条件下不会出现。

## 延伸阅读

- **`torch/_dynamo/exc.py` 与 `torch/_dynamo/convert_frame.py`**：读 graph break 的判定与记录位置，理解“为什么这里切开”；对应 2.2 节。
- **PyTorch 官方 “torch.compiler 内部机制” 文档**：读 Dynamo 与 AOTAutograd 的分段说明，确认第二张图为什么叫 core ATen。
- **`torch/_inductor/lowering.py`**：读 `fallback_handler`，看没有 lowering 的算子如何被保留为 extern 调用；对应第 5 章。
- **`torch/_inductor/scheduler.py`**：读 `fuse_nodes` 与 `fuse_nodes_once` 的日志语句，把 2.5 节的日志对上源码行。
- **`torch/_inductor/ir.py`**：读 `FixedLayout` 与 `FlexibleLayout` 的差别，理解“布局是第二道边界之后才出现的信息”。

[上一章：架构地图：一次 torch.compile 的六段旅程](./01-inductor-architecture-map.md) | [下一章：为什么是 loop-level IR](./03-why-loop-level-ir.md)
