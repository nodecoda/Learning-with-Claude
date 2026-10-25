# 练习 A 一次编译的六段对照

> 目标：用 lab_a 的四份真实产物，写出一份“六段对照笔记”：每段写清输入、输出、证据、以及它不能回答的问题。这份笔记是 Ch 4 之后所有源码阅读的地基——后面的章节默认你已经能独立判断“这行文本属于哪一段”。

> 环境：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口 `code/tools/run.ps1`。

## 为什么

读完 Ch 1 到 Ch 3，你可能已经能复述六段流水线的名字。但“能复述”和“能定位”是两件事：真正读产物时，你面对的是一堆没有标注的文本行，要判断它属于哪一层，只能靠符号指纹。

这份练习的目的就是把“复述”变成“定位”。做法很笨但有效：给你四份产物和六个格子，要求每一格填上三类信息——这一段产出了什么、证据是哪几行、这一段回答不了什么。第三类信息最关键，因为后面几乎所有误判都来自“问了这一层答不出的问题”。

练习不要求你改代码，也不要求你解释优化算子的数学。它只要求你把一次真实编译的产物读到位。做完之后，你应该能在不看讲义的情况下，对任意一份 Inductor 产物说出它属于哪一段。

## 主线

### A.1 交付物：一份六段对照笔记

把下面的模板抄进你的笔记文件（自选位置，不要写进课程目录），每段填三栏：

| 段 | 产物 | 证据（3–6 行原文） | 这段回答不了什么 |
| --- | --- | --- | --- |
| 1 Dynamo | `explain` 输出 | 待填 | 待填 |
| 2 AOTAutograd | `fx_graph_readable.py` | 待填 | 待填 |
| 3 post-grad passes | `fx_graph_transformed.py` | 待填 | 待填 |
| 4 GraphLowering | `ir_pre_fusion.txt` | 待填 | 待填 |
| 5 Scheduler | `ir_post_fusion.txt` | 待填 | 待填 |
| 6 codegen | `output_code.py` | 待填 | 待填 |

三条填写规则，用来防止“抄一段代码就算证据”：

1. 证据行必须能**单独**说明问题。例如 `op1.group.iteration = ((64,), (64,))` 能说明迭代空间，而 `def body(self, ops):` 单独拿出来什么都说明不了。
2. 每段的“回答不了”至少写一条，而且要写成**问题**，例如“不能回答哪些节点会融合”。
3. 不要跨段填证据：`buf0` 不能作为第 2 段的证据，`aten.relu` 不能作为第 4 段的证据。

### A.2 第 1–3 段：从 Python 到 core ATen

这三段的任务是“把语义搬进图里”。填写时用下面这些问题自查：

**第 1 段（Dynamo）：**

- 这段代码会被切成几张图？切点在哪一行？
- `graph_count`、`op_count`、`ops_per_graph` 三者是什么关系？
- guards 大概覆盖了什么（shape、stride、dtype、代码对象）？

**第 2 段（AOTAutograd）：**

- 进入 Inductor 的图里有哪些 `aten` 算子？按顺序列出来。
- 每个中间值的 shape 是什么？从 `f32[64, 64]` 这类标注里读。
- `; mm = arg2_1 = None` 这类后缀说明了什么？
- `div.Tensor(sum_1, 64)` 里的 64 是张量还是常量？

**第 3 段（post-grad passes）：**

- `fx_graph_transformed.py` 与 `fx_graph_readable.py` 的字节数分别是多少？差异来自哪里？
- 这一段改写了什么？算子数有没有变化？
- `fx_graph_transformed.py` 与 `fx_graph_readable.py` 的字节数差在哪里？

第 3 段最容易写错，因为它“看起来没变”。本机的实际差异是：`fx_graph_readable.py` 里是 `mm` 与 `add` 两个算子，`fx_graph_transformed.py` 里它们已经被合成一个 `aten.addmm.default`，算子数从 5 变成 4，文件从 955 B 变成 917 B。写这一段时要给出的证据就是这两行 addmm：

```python
addmm_default: "f32[64, 64]" = torch.ops.aten.addmm.default(arg2_1, arg0_1, arg1_1);  arg2_1 = arg0_1 = arg1_1 = None
relu: "f32[64, 64]" = torch.ops.aten.relu.default(addmm_default);  addmm_default = None
```

这也是一个很好的教训：如果只对比“有没有 aten 字样”，你会得出“没变化”的错误结论。

### A.3 第 4–5 段：从 IR 到融合

这两段的任务是“把算子换成循环”。填写时注意三个容易混淆的点：

**第一，buffer 与值的区别。** ATen 图里 `relu` 是一个值；IR 里没有 `relu` 这个 buffer，relu 只是 `buf1` 计算表达式的一部分。你要找的证据是：

```text
op1.outputs = [
    buf1: ComputedBuffer
    buf1.layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
]
```

**第二，`var_ranges` 与 shape 的区别。** `var_ranges = {p0: 64, p1: 64}` 描述循环范围；`size=[64]` 描述 buffer 形状。本例两者数值一致，但角色不同。

**第三，融合前与融合后的对照。** 第 4 段的产物是节点集合（3 个），第 5 段的产物是融合后的节点集合（2 个）。要写出的证据是这一行：

```text
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
```

填这一段时，建议额外记一个数：`op1_op2.unmet_dependencies` 里 `MemoryDep('buf0', c0, {c0: 4096})` 的 4096。它是 `buf0` 被压平后的大小（64×64）。这个数字能同时验证两件事：依赖是对整个 buffer 的，不是对某一行；以及压平发生在 IR 层。

### A.4 第 6 段：从调度 IR 到 C++ 与 wrapper

最后一段的产物有两个部分，性质完全不同，要分开记：

**C++ kernel 部分**，证据是循环与向量化：

```cpp
#pragma omp parallel num_threads(4)
...
for(int64_t x1=...; x1<64LL; x1+=8LL)
...
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + (x1 + 64LL*x0), 8);
```

**Python wrapper 部分**，证据是 buffer 分配与调用顺序：

```python
buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
buf2 = buf1; del buf1  # reuse
cpp_fused_div_relu_sum_0(buf2, buf0)
```

填写时回答四个问题：

- C++ 函数名是什么？Python 绑定名是什么？为什么两者不同？
- `num_threads(4)` 的 4 从哪来？
- `x1+=8` 的 8 由什么决定？
- `buf2 = buf1; del buf1  # reuse` 与第 5 段的哪个决策对应？

本机四个答案分别是：`kernel` 与 `cpp_fused_div_relu_sum_0`；4 来自 `torch.get_num_threads()`；8 来自 AVX2 的 256 位除以 float32 的 32 位；对应 `op1_op2` 融合后 `buf1` 只在 kernel 内存在。

### A.5 自测题

先自己做，再展开答案。答案里给出的数字都来自本机实测，你的机器可能不同，但**判断方法**应当一致。

**题 1**：`fx_graph_readable.py` 里有几个 `torch.ops.aten` 调用？分别是什么？

<details>
<summary>答案</summary>

5 个：`aten.mm.default`、`aten.add.Tensor`、`aten.relu.default`、`aten.sum.dim_IntList`、`aten.div.Tensor`。注意 `x @ w + b` 在这里是 `mm` 加 `add`，不是 `addmm`；`addmm` 出现在后面的 extern kernel 调用里。
</details>

**题 2**：为什么 `ir_pre_fusion.txt` 里有 `buf0`、`buf1`、`buf2`，而 ATen 图里一个都没有？

<details>
<summary>答案</summary>

buffer 是 GraphLowering 引入的概念。ATen 图描述“值之间的运算”，IR 描述“按 buffer 与下标计算”。中间值一旦要落内存，就会得到 `bufN` 这样的名字；能原地复用的会被合并（本例 `buf1` 与 `buf2`）。
</details>

**题 3**：`op1` 的循环体里 `index0 = 64*p0 + p1`、`index1 = p0`，这两个下标分别对应什么？

<details>
<summary>答案</summary>

`index0` 是 `buf0`（64×64）的线性下标，行主序下等于 `行 * 64 + 列`；`index1` 是 `buf1`（64 个元素）的下标，等于行号。归约沿列方向进行，所以每行聚合成一个元素。
</details>

**题 4**：融合日志里 `fused 3 nodes into 2 nodes` 的 3 与 2 分别指什么？为什么不是 3 到 1？

<details>
<summary>答案</summary>

3 是融合前的节点数：`op0`（extern addmm）、`op1`（归约）、`op2`（点态除法）。2 是融合后的节点数：`op0` 与 `op1_op2`。不能到 1，因为 `op0` 是 extern kernel，它的实现不在 Inductor 的循环层里，无法与循环节点合并。
</details>

**题 5**：为什么生成的 C++ 里除法那一段用 `#pragma omp single`，而不是 `#pragma omp for`？

<details>
<summary>答案</summary>

那一段只有 64 个元素。分给 4 个线程的调度与同步开销高于并行收益，所以由单线程执行；它仍然被向量化（步长 8），因为向量化不依赖线程。判断依据是迭代空间大小，不是“代码看起来短”。
</details>

**题 6**：`kernel` 与 `cpp_fused_div_relu_sum_0` 是同一个东西吗？

<details>
<summary>答案</summary>

是同一个 kernel。`kernel` 是 C++ 侧的导出函数名（`extern "C" __declspec(dllexport) void kernel(...)`）；`cpp_fused_div_relu_sum_0` 是 Python wrapper 里指向它的绑定名，由变量赋值建立。
</details>

**题 7**：`[PASS] 数值与 eager 一致 (max_abs_err=4.768e-07 tol=1e-05)` 说明了什么？它不能说明什么？

<details>
<summary>答案</summary>

说明这次编译产物的数值与 eager 结果在 1e-5 容差内一致。它不能说明产物是最新的（可能命中缓存）、不能说明性能有提升、也不能说明所有分支都覆盖到了（只测了一个 shape）。
</details>

**题 8**：如果 `ir_pre_fusion.txt` 里出现了 `aten.relu`，你会怎么判断？

<details>
<summary>答案</summary>

几乎可以断定看错了文件或看错了层。IR 层不会出现 `aten.*` 算子名；relu 在循环体里是 `ops.relu(...)`。若真在 ATen 图文件里看到，那是第 2 段或第 3 段的产物，不是第 4 段。
</details>

**题 9**：`min_input_distance = 1`、`max_input_distance = 2` 说明什么？

<details>
<summary>答案</summary>

说明融合后的节点在调度顺序上，它的输入从前面第 1 到第 2 个节点之间产生。这个指标用于内存规划：距离越近，buffer 越可能被原地复用。
</details>

**题 10**：为什么 wrapper 里要写 `assert_size_stride_grouped(...)`？

<details>
<summary>答案</summary>

它是 guards 在运行时的检查点：输入 shape 与 stride 必须与编译时假设一致，否则这张图不适用。它同时也告诉读者：编译产物对布局是有假设的。
</details>

**题 11**：`ops.constant(64.0, torch.float32)` 与 ATen 图里的 `div.Tensor(sum_1, 64)` 是什么关系？

<details>
<summary>答案</summary>

同一条信息的两种表示。Python 里的 `h.shape[1]` 在抓图时是常量 64；ATen 图把它写成标量参数；lowering 后它变成 `ops.constant`，再变成 C++ 里的 `static_cast<float>(64.0)`。
</details>

**题 12**：第 3 段把 `mm` + `add` 改成了什么？你怎么从产物里确认？

<details>
<summary>答案</summary>

改成 `aten.addmm.default`。确认方法是打开 `fx_graph_transformed.py`，看到 `addmm_default` 这一行，且 `mm`/`add` 消失；对照 `fx_graph_readable.py` 里的两行 `mm` 与 `add`。算子数 5 → 4，字节数 955 → 917。
</details>

### A.6 评分标准与三种典型误读

对照笔记按“证据可复核”打分，不按字数：

| 档位 | 标准 |
| --- | --- |
| 合格 | 六段都有产物、都有原文证据、都写了“回答不了什么” |
| 良好 | 证据行能独立说明问题；跨段证据没有混用；数字与本次运行一致 |
| 优秀 | 能指出至少一处“产物与直觉不符”的地方并解释（例如 `mm+add` 变 `addmm`） |

三种典型误读，提前列出以免踩坑：

1. **把 `fx_graph_readable.py` 当 Dynamo 图。** 它是 AOTAutograd 之后的 core ATen 图，里面没有 graph break 的位置信息。要数图的数量请用 `torch._dynamo.explain`。
2. **把 `ir_pre_fusion.txt` 当最终结构。** 融合发生在调度器里，最终结构在 `ir_post_fusion.txt`；两者节点数不同（3 与 2）。
3. **把 wrapper 当 kernel。** wrapper 是 Python 代码，负责分配与调用；kernel 是 C++，负责算。两者在同一个 `output_code.py` 里，但阅读方法完全不同。

## 可跑实验

第一步，跑一次，拿到产物：

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

6) 结构断言
  extern kernels: ['addmm']
[PASS] 存在生成的 C++ kernel  (kernel)
[PASS] matmul 走 extern kernel  (['addmm'])
[PASS] 调度 IR 里有 SchedulerNode
[PASS] kernel 被向量化  (step=1)
```

第二步，按 A.1 的模板填完六段。填的过程中可以用下面三条自查命令定位产物路径（把路径换成你这次的 `*.debug` 目录）：

```powershell
Get-ChildItem -Recurse -Directory -Filter "*.debug" compilers\inductor_deep_dive\code\.cache
Get-Content -LiteralPath <debug目录>\ir_pre_fusion.txt -Encoding UTF8
Get-Content -LiteralPath <debug目录>\output_code.py -Encoding UTF8
```

第三步，做一次“跨段一致性检查”，这是本练习的核心：

| 检查 | 期望 |
| --- | --- |
| ATen 图的算子数 | 5 |
| 未融合节点数 | 3（1 个 extern + 2 个 SchedulerNode） |
| 融合后节点数 | 2 |
| 生成 kernel 数 | 1（外加 1 次 extern 调用） |
| 向量化步长（内层） | 8 |
| 线程数 | 4 |

六个数字互相对得上，说明你的笔记没有把不同层的证据混在一起。任一对不上，先回到那一层重写，而不是调数字。

## 源码落点

- `compilers/inductor_deep_dive/code/labs/lab_a_pipeline.py`：读六段打印的顺序，理解它为什么按这个顺序切分；`common.snippet(pre, 34)` 只截前 34 行，说明讲义里的“节选”是有意的。
- `compilers/inductor_deep_dive/code/labs/common.py`：读 `kernel_stats` 与 `wrapper_extern_calls`，理解 `name`、`step`、`threads`、`tmp_ops` 各由哪个正则得到。
- `torch/_inductor/compile_fx.py`：读 `fx_codegen_and_compile`，把第 4–6 段对到函数调用上。
- `torch/_inductor/graph.py`：读 `GraphLowering.run`，确认 IR 生成是逐节点进行。
- `torch/_inductor/scheduler.py`：读 `Scheduler.fuse_nodes`，确认融合轮次与日志字符串。
- `torch/_inductor/codegen/cpp.py`：读 `CppKernelProxy`，确认 `#pragma omp` 与向量化循环的生成位置。

## 常见坑

1. **只跑一次就下结论。** 缓存命中时看到的产物可能是上一次的；结构断言一致不代表这次重新编译了。
2. **把 955 / 2842 / 3591 / 6226 当固定值。** 它们随版本、shape、机器变化；笔记里要写“本次运行的数值”，不是“课程的数值”。
3. **证据抄太长。** 抄一整段 `loop_body` 不能说明具体问题；3–6 行、能独立说明一句结论就够。
4. **用 `Get-Content` 不带编码参数。** 生成物可能含 cp936 内容，中文注释会变乱码；带上 `-Encoding UTF8`，或按 `common.read_text_any` 的方式兜底。
5. **把 `ExplainOutput` 当下标访问。** 它没有 `__getitem__`，要用字段名。
6. **以为 `threads=4` 是 Inductor 的优化结论。** 它来自运行时线程数设置，换个机器就是别的数。
7. **把“回答不了什么”写成“没有问题”。** 每一层都有盲区，写不出来说明还没分清层。

## 自我检查

- [ ] 六段笔记都填完了，每段至少 3 行原文证据。
- [ ] 每段都写了至少一条“这一段回答不了什么”。
- [ ] 我能不看讲义，说出 ATen 图的 5 个算子名与顺序。
- [ ] 我能解释 `mm + add` 为什么在生成代码里变成一次 `addmm` 调用。
- [ ] 我能把 `var_ranges`、`index0`、`index1` 三个符号的含义各用一句话说清。
- [ ] 我能说出融合前 3 个节点、融合后 2 个节点的具体名字。
- [ ] 我能解释 `buf2 = buf1; del buf1  # reuse` 的来源。
- [ ] 我的六个跨段数字互相对得上（5 / 3 / 2 / 1 / 8 / 4）。

## 延伸阅读

- **Ch 11 “读调度 IR”**：本练习的第 4、5 段会在那一章逐行展开，包括 `var_ranges` 与 `index` 的完整语义。
- **`torch/_inductor/codegen/cpp.py`**：读 `CppKernelProxy` 与 `CppVecKernel`，理解第 6 段的向量化循环是怎么被拼出来的。
- **`torch/_inductor/scheduler.py`**：读 `SchedulerNode` 的 `debug_str`，你会发现 `ir_pre_fusion.txt` 的格式就是这个函数输出的。
- **`torch/_dynamo/eval_frame.py`**：读 `explain` 的实现，确认 `ExplainOutput` 的字段来源；这是第 1 段唯一需要的外部工具。
- **PyTorch 官方 “torch.compile 端到端教程”**：把教程里的图与本练习的四份产物对照，确认同一套分段说法。

[上一章：为什么是 loop-level IR](./03-why-loop-level-ir.md) | [下一章：GraphLowering 与 IR：FX 图如何变成 buffer 与循环](./04-graph-lowering-and-ir.md)