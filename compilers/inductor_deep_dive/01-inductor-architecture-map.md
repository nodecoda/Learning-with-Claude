# Ch 1 架构地图：一次 torch.compile 的六段旅程

> 目标：把一次 `torch.compile` 拆成六段可以分别观察的流水线，并建立一张“产物 → 阶段 → 职责”的对照表。读完本章，你看到 `fx_graph_readable.py`、`ir_pre_fusion.txt`、`ir_post_fusion.txt`、`output_code.py` 里的任意一行，都能说出它属于哪一段、能回答什么问题、不能回答什么问题。

> 环境：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口是 `code/tools/run.ps1`（它负责导入 MSVC 环境、设置缓存目录、设置 `PYTHONPATH`）。

## 为什么

学编译器最容易掉进的坑是“图”这个词。你在第 1 章就听到图，在第 4 章又听到图，在第 9 章还在听图，但它们是三张不同的图：Dynamo 捕获的 FX 图里节点是 Python 调用；AOTAutograd 交出的 core ATen 图里节点是 `aten` 算子；调度器手里的 IR 里节点是“一个循环嵌套 + 若干 buffer 读写”。这三张图的名字都叫 graph，能回答的问题却完全不同。

于是出现一种典型困惑：你在 `fx_graph_readable.py` 里找不到 `buf0`，就以为产物不完整；你在 `ir_pre_fusion.txt` 里找不到 `aten.relu`，就以为融合把 relu 弄丢了；你看到 `extern_kernels.addmm` 以为 Inductor 自己生成了矩阵乘，其实它把这件事交给了外部 kernel。这些都不是 bug，而是把不同层的词汇混在了一起。

本章的做法是先把六段流水线的**边界**画出来，再用 lab_a 的四份真实产物逐段对照。判断标准很朴素：每一段都有明确的输入类型和输出类型；两段之间的接口一旦说清，后面读源码时就知道该在哪个文件里找什么。

## 主线

### 1.1 六段流水线：谁产生什么

一次 `torch.compile` 的完整链路如下。方框里是**表示**，箭头上的文字是**负责的组件**：

```text
  Python 函数
      |
      | Dynamo（捕获字节码、插入 guards）
      v
  FX 图（torch.fx.Graph，节点是 Python 层调用）
      |
      | AOTAutograd（函数化、decomposition、处理参数与梯度）
      v
  core ATen 图（节点是 aten 算子）
      |
      | post-grad passes（DCE、重排、pattern 改写）
      v
  core ATen 图（还是 aten 算子，但更少、更规整）
      |
      | GraphLowering（每个 aten 算子 lowering 成 buffer + 循环体）
      v
  loop-level IR（TensorBox / ComputedBuffer / LoopBody）
      |
      | Scheduler（依赖分析、融合、内存生命周期）
      v
  调度后的 IR（SchedulerNode / FusedSchedulerNode）
      |
      | codegen（C++ kernel 与 Python wrapper）+ cl.exe
      v
  output_code.py + .pyd
```

六段的职责可以压成一张表：

| 段 | 组件 | 输入 | 输出 | 本课程的主要产物 |
| --- | --- | --- | --- | --- |
| 1 | Dynamo | Python 字节码 | FX 图 + guards | `torch._dynamo.explain` 的输出 |
| 2 | AOTAutograd | FX 图 | core ATen 图 | `fx_graph_readable.py` |
| 3 | post-grad passes | core ATen 图 | 改写后的 ATen 图 | `fx_graph_transformed.py` |
| 4 | GraphLowering | ATen 图 | loop-level IR | `ir_pre_fusion.txt` |
| 5 | Scheduler | loop-level IR | 融合后的调度 IR | `ir_post_fusion.txt` |
| 6 | codegen + 编译 | 调度 IR | C++ / wrapper / 动态库 | `output_code.py` |

这张表有一个容易漏掉的事实：**第 3 段不改变表示层**。post-grad passes 的输入和输出都是 ATen 图，只是节点变少、顺序变好。真正的表示切换发生两次：FX 图到 ATen 图（第 2 段），ATen 图到 loop-level IR（第 4 段）。把这两次切换记住，后面就不会在同一层里找不属于它的东西。

### 1.2 第 1 段 Dynamo：把 Python 变成 FX 图

Dynamo 的工作是“在不改变语义的前提下，把一段 Python 执行过程记录成图”。它记录的是 Python 层的调用序列，并额外生成一组 guards：输入张量的 shape、stride、dtype、以及代码对象本身。guards 全部命中时，后续调用直接走编译结果；任一条件不满足，就重新捕获或回退。

Dynamo 有自己的边界，叫 graph break。遇到 `print`、数据依赖的 Python 分支、不支持的第三方库调用，它会在这里切开，把一段函数拆成两张图。切开的位置不是错误，但会带来两个后果：段与段之间要回到 Python 解释器，以及每段各自编译。

本机实测（用一个带 `print` 的小函数）：

```text
type        : ExplainOutput
graph_count : 2
op_count    : 3
ops_per_graph: [[<built-in method sin ...>, <built-in function add>], [<built-in function mul>]]
break_types : ['GraphCompileReason']
n_guards    : 19
```

两处细节值得注意：`ops_per_graph` 是**列表的列表**，两张图分别含 2 个和 1 个算子；`out_guards` 有 19 条。第 2 章会把这张图与它的边界展开讲，本章记住一点就够：Dynamo 的产物是 FX 图，它还没有经过任何算子级改写。

### 1.3 第 2 段 AOTAutograd：进入 core ATen

AOTAutograd 接收 FX 图，输出一张只由 core ATen 算子构成的图。它做三件事：把带副作用的操作函数化；把 `torch.nn.Module` 的参数、buffer 变成显式输入；按需把复合算子 decompose 成更小的 aten 算子。

lab_a 的模型只有一行矩阵运算和一行归约：

```python
def model(x, w, b):
    h = torch.relu(x @ w + b)
    return h.sum(dim=1) / h.shape[1]
```

它对应的 core ATen 图（`fx_graph_readable.py` 全文，本机实测）是：

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

三件事在这段文本里就能读出来：

1. `x @ w + b` 在这里是 `mm` 加 `add` 两个算子，不是 `addmm`。也就是说 `addmm` 不是 Python 语义，而是后面某一段的**决定**。
2. 每个算子后面跟着 `; arg0_1 = arg1_1 = None`，这是生命周期标注：这个张量此后再也不用了。
3. `div.Tensor(sum_1, 64)` 的第二个参数被吸收成了常量 64，因为 `h.shape[1]` 在抓图时是静态值。

还有一点必须强调：这份文件是**AOTAutograd 之后、post-grad passes 之前**的图。Dynamo 的原始图不会出现在这个目录里，所以不要拿它去数 graph break。

再记一条线索：这里 `mm` 与 `add` 还是两个算子，而下一篇产物 `fx_graph_transformed.py` 里它们已经合成一个 `aten.addmm.default`。也就是说，**算子合并发生在第 3 段**，lowering 只是把 `addmm` 选成外部实现。

### 1.4 第 3 段 post-grad passes：表示不变，内容变少

这一段跑在 ATen 图上，顺序大致是：死代码消除、按局部性重排、自定义 pre pass、pattern 匹配改写、自定义 post pass。它的作用是把“能融合的形状”整理出来，并给用户留出插入改写的位置。

两个扩展点在本机是实测可用的：

```python
torch._inductor.config.post_grad_custom_pre_pass = my_pass
torch._inductor.config.post_grad_custom_post_pass = my_pass
```

回调收到的是 `torch.fx.Graph`，不是 `GraphModule`。这一点在第 7 章会以完整实验展开，本章只强调边界：你在这里拿到的是“节点列表”，不是“可执行模块”。

lab_a 的模型在这一段**确实**被改写了，而且改写很关键：`mm` 与 `add` 被合并成一个 `aten.addmm.default`。`fx_graph_transformed.py` 里对应的两行是：

```python
addmm_default: "f32[64, 64]" = torch.ops.aten.addmm.default(arg2_1, arg0_1, arg1_1);  arg2_1 = arg0_1 = arg1_1 = None
relu: "f32[64, 64]" = torch.ops.aten.relu.default(addmm_default);  addmm_default = None
```

两处变化值得记下来：

1. 算子数从 5 变成 4：`mm` 与 `add` 合成一个 `addmm`。两份文件的字节数也从 955 B 降到 917 B。
2. 参数顺序变了：`addmm` 把 bias 放在第一位（`arg2_1` 在前），这是 `aten.addmm(bias, mat1, mat2, beta, alpha)` 的参数约定。

这一段的价值正在于此：它不改变表示层（输入输出都是 ATen 图），却改变了算子集合。读后面几段时如果发现“凭空多了一个 addmm”，答案在这里。

### 1.5 第 4 段 GraphLowering：ATen 图变成 buffer 与循环体

GraphLowering 遍历 ATen 图的每个节点，调用对应的 lowering 规则，产出 loop-level IR。这里的节点类型换了一套词汇：

- `TensorBox` / `StorageBox`：值的包装，前者带 layout 推导，后者指向具体存储。
- `ComputedBuffer`：一个“由循环计算出来”的 buffer。
- `FixedLayout` / `FlexibleLayout`：布局。前者布局固定，后者允许后续调整。

lab_a 的 `ir_pre_fusion.txt` 里有三个节点。第一个是 extern kernel：

```text
op0: ExternKernelSchedulerNode(ExternKernelOut)
op0.writes = [StarDep(name='buf0', mode=None)]
op0.outputs = [
    buf0: ExternKernelOut
    buf0.layout = FixedLayout('cpu', torch.float32, size=[64, 64], stride=[64, 1])
]
op0.node.kernel = extern_kernels.addmm
```

读法：这个节点写出 `buf0`（一个 64×64、行主序的 float32 buffer），实现方式是 `extern_kernels.addmm`。第 1.3 节里还是 `mm` + `add` 两个 aten 算子，到这里变成了一个 extern 调用。这次合并不在 lowering 里完成：第 3 段已经把两者合成 `aten.addmm.default`，lowering 只是把这个算子选成外部实现（`extern_kernels.addmm`）。

第二个节点是归约，带完整的循环体：

```text
op1.group.iteration = ((64,), (64,))
op1.sizes = ([64], [64])
class op1_loop_body:
    var_ranges = {p0: 64, p1: 64}
    index0 = 64*p0 + p1
    index1 = p0
    def body(self, ops):
        load = ops.load('buf0', get_index)
        relu = ops.relu(load)
        reduction = ops.reduction(torch.float32, torch.float32, 'sum', relu)
        store_reduction = ops.store_reduction('buf1', get_index_1, reduction)
```

两个 `var_ranges` 变量 `p0`、`p1` 分别对应外层 64 与内层 64；`index0 = 64*p0 + p1` 是 `buf0` 的线性下标；`index1 = p0` 是 `buf1` 的下标。这里有一个关键设计：**循环变量空间与 buffer 下标空间是分开描述的**。`p0/p1` 是循环变量，`index0/index1` 是下标表达式，两者可以不一致。第 4 章会把这条设计讲透。

第三个节点是除法：

```text
class op2_loop_body:
    var_ranges = {p0: 64}
    index0 = p0
    def body(self, ops):
        load = ops.load('buf1', get_index)
        constant = ops.constant(64.0, torch.float32)
        truediv = ops.truediv(load, constant)
        store = ops.store('buf2', get_index_1, truediv, None)
```

一个 64 元素的点态循环，读 `buf1`、除以常量、写 `buf2`。到这里，三张图的关系已经很清楚：`fx_graph_readable.py` 描述“算什么”，`ir_pre_fusion.txt` 描述“怎么按 buffer 算”。

### 1.6 第 5 段 Scheduler：融合只在这里发生

调度器读入未融合的节点，做依赖分析，把可以放在同一个循环里的节点合并。lab_a 的 `ir_post_fusion.txt` 里，节点数从 3 变成 2：

```text
op0: ExternKernelSchedulerNode(ExternKernelOut)
...
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
op1_op2.writes = [MemoryDep('buf1', c0, {c0: 64}), MemoryDep('buf2', c0, {c0: 64})]
op1_op2.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1_op2.min_input_distance = 1
op1_op2.max_input_distance = 2
```

`op1_op2` 这个名字是融合的产物：`op1`（sum）与 `op2`（div）被合并成一个节点。它有两个写者（`buf1`、`buf2`），但只有一组未满足的依赖（读 `buf0`）。融合的判据不是“算子看起来像”，而是下标关系能不能对上：两个节点的循环空间都是 64 长的外层循环，内层一个做归约、一个做逐元素，因此可以套在一起。

这一步不改变数学，但决定了两件影响性能的事：

1. 中间结果 `buf1` 能不能不出内存。融合之后，`buf1` 只在同一个 kernel 内存在，wrapper 里可以把它和 `buf2` 合并（实际生成里就是 `buf2 = buf1; del buf1  # reuse`）。
2. 循环要写几层、向量化作用在哪一层。融合后的 kernel 里，归约循环的向量化与除法循环的向量化是分开生成的。

### 1.7 第 6 段 codegen 与编译：C++、动态库、wrapper

codegen 把调度 IR 翻译成 C++，再交给 `cl.exe` 编译成 `.pyd`，最后生成 Python wrapper 负责 buffer 分配与调用顺序。lab_a 的 `output_code.py` 里，kernel 名是 `cpp_fused_div_relu_sum_0`，结构如下（节选）：

```cpp
extern "C" __declspec(dllexport) void  kernel(float* in_out_ptr0, const float* in_ptr0)
{
    auto out_ptr0 = in_out_ptr0;
    #pragma omp parallel num_threads(4)
    {
        int tid = omp_get_thread_num();
        {
            #pragma omp for
            for(int64_t x0=...; x0<64LL; x0+=1LL)
            {
                float tmp_acc0 = 0;
                at::vec::Vectorized<float> tmp_acc0_vec = at::vec::Vectorized<float>(0);
                for(int64_t x1=...; x1<64LL; x1+=8LL)
                {
                    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + (x1 + 64LL*x0), 8);
                    auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
                    tmp_acc0_vec = tmp_acc0_vec + tmp1;
                }
                tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<...>(...);
                in_out_ptr0[x0] = static_cast<float>(tmp_acc0);
            }
        }
        #pragma omp single
        { /* 64 个元素除以 64.0，向量化步长 8 */ }
    }
}
```

这段代码把三件事同时暴露出来了：

- **向量化**：内层 `x1 += 8`，`at::vec::Vectorized<float>`，`loadu` 一次读 8 个 float。8 来自 AVX2 的 256 位除以 32 位元素。
- **并行化**：外层 `#pragma omp for` 分给 4 个线程，线程数来自 `torch.get_num_threads()`。
- **融合的代价与收益**：两个循环共享一次 kernel 启动；除法循环用 `#pragma omp single` 由单线程执行，因为它只有 64 个元素，分给 4 个线程反而多出同步开销。

wrapper 侧则完全是另一种语言：

```python
def call(self, args):
    arg0_1, arg1_1, arg2_1 = args
    args.clear()
    assert_size_stride_grouped((arg2_1, arg0_1, arg1_1), ((64,), (64, 64), (64, 64)), ((1,), (64, 1), (64, 1)), 'input')
    buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
    extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
    buf1 = empty_strided_cpu((64,), (1,), torch.float32)
    buf2 = buf1; del buf1  # reuse
    cpp_fused_div_relu_sum_0(buf2, buf0)
    return (buf2, )
```

读 wrapper 时注意三行：`assert_size_stride_grouped` 是 guards 的运行时形态；`extern_kernels.addmm` 是第 4 段选中的外部 kernel；`buf2 = buf1; del buf1  # reuse` 是内存规划的结果，直接来自第 5 段的融合。

### 1.8 同一段语义，五种表示：对照表

把同一件事的五种写法放在一起，这张表值得抄下来：

| 表示 | 出现在 | 这段语义写成什么 | 能回答 | 不能回答 |
| --- | --- | --- | --- | --- |
| Python | `model` | `h = torch.relu(x @ w + b)` | 用户想算什么 | 机器怎么算 |
| core ATen 图 | `fx_graph_readable.py` | `mm` → `add` → `relu` → `sum` → `div` | 有哪些算子、顺序如何 | 融合点、buffer 布局 |
| loop-level IR | `ir_pre_fusion.txt` | `ops.load` / `ops.relu` / `ops.reduction` / `ops.truediv` | 每个 buffer 怎么算出来 | 哪些节点会合在一起 |
| 调度后 IR | `ir_post_fusion.txt` | `op1_op2: OuterLoopFusedSchedulerNode` | 一个 kernel 里放了几段循环 | 循环体长什么样、向量化多宽 |
| C++ | `output_code.py` | 两层 `for` + `Vectorized<float>` + omp pragma | 真正执行的指令结构 | 为什么这么选（要看前面几层） |

一个 5 行的函数，五种表示，字节数从 955 涨到 6226。多出来的部分全是“怎么算”的信息：buffer、下标、循环边界、向量宽度、线程数、内存复用。

## 可跑实验

运行主线实验，产出四份文件并逐段打印：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

本机输出（节选，保留原始中文）：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)

1) 编译产物位置
D:\Learning-with-Claude\compilers\inductor_deep_dive\code\.cache\iu\ciuthff...debug
  fx_graph_readable.py       955 B
  ir_pre_fusion.txt         2842 B
  ir_post_fusion.txt        3591 B
  output_code.py            6226 B

4) 生成的 C++ kernel 结构
  name         kernel
  vectorized   True
  step         1
  threads      4
  tmp_ops      6
  uses_abs     False
  cpp_lines    108

6) 结构断言
  extern kernels: ['addmm']
[PASS] 存在生成的 C++ kernel  (kernel)
[PASS] matmul 走 extern kernel  (['addmm'])
[PASS] 调度 IR 里有 SchedulerNode
[PASS] kernel 被向量化  (step=1)
```

输出里有三个数字容易被误读，提前说清：

- `name  kernel` 是 C++ 函数名。Python 侧的绑定名是 `cpp_fused_div_relu_sum_0`，两者不一样。前者由 codegen 生成，后者由 Python wrapper 里的变量名决定。
- `step 1` 是外层循环的步长（`x0 += 1`），不是向量化宽度。向量化宽度在代码里的内层循环，是 `x1 += 8`。
- `tmp_ops 6` 数的是 `auto tmpN =` 的个数。它随实现变化，不要拿它当跨版本的稳定指标。

拿到产物后，按下面的顺序做一次“阶段归属”练习：

1. 打开 `fx_graph_readable.py`，数 `torch.ops.aten` 出现次数（本机是 5 次）。
2. 打开 `ir_pre_fusion.txt`，数 `SchedulerNode(` 出现次数（本机是 2 次，另有 1 个 `ExternKernelSchedulerNode`）。
3. 打开 `ir_post_fusion.txt`，确认 `OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)` 出现了 1 次。
4. 打开 `output_code.py`，确认 `#pragma omp parallel num_threads(4)` 与 `Vectorized<float>` 同时存在。

## 源码落点

- `torch/_inductor/compile_fx.py`：读 `compile_fx` 与 `compile_fx_inner`，看一次调用的入口如何把 FX 图交给后面几段；再读 `fx_codegen_and_compile`，看“调度 → codegen → 编译”这几步在哪里被串起来。
- `torch/_inductor/graph.py`：读 `GraphLowering.run` 与 `graph_codegen`。前者遍历 ATen 图调 lowering，后者把 IR 交给调度与 codegen。
- `torch/_inductor/scheduler.py`：读 `Scheduler.compute_dependencies` 与 `fuse_nodes`。前者建依赖图，后者做融合决策。
- `torch/_inductor/ir.py`：读 `TensorBox`、`StorageBox`、`ComputedBuffer` 三个类，理解“值”与“存储”为什么要分开。
- `torch/_inductor/codegen/cpp.py`：读 `CppKernel` 与 `CppScheduling`，看循环体如何变成 `for` 与 pragma。
- `torch/_inductor/codegen/wrapper.py`：读 `PythonWrapperCodegen`，看 buffer 分配、`del`、`# reuse` 这些行是怎么写出来的。

## 常见坑

1. **以为 `x @ w + b` 会融合成一个自己的 kernel。** 第 3 段先把 `mm` + `add` 合成 `aten.addmm.default`，lowering 再把它选成 `extern_kernels.addmm(...)`，仍然走外部 kernel。融合的边界是“能不能用更快的现成实现”，不是“能不能合并”。
2. **在 `fx_graph_readable.py` 里找 `buf0`。** buffer 是 lowering 才引入的概念，ATen 图里没有。找不到不是产物缺失。
3. **以为 `fx_graph_readable.py` 是 Dynamo 的图。** 它是 AOTAutograd 之后的 core ATen 图。要数 graph break 请用 `torch._dynamo.explain`。
4. **把 pass 回调当 GraphModule 用。** 回调收到的是 `torch.fx.Graph`，写 `gm.graph` 会报错。第 7 章有完整对照。
5. **以为 `ir_pre_fusion` 已经是最终结构。** 它是融合**前**的节点集合；融合发生在调度器里，结果在 `ir_post_fusion.txt`。
6. **把 `kernel` 与 `cpp_fused_div_relu_sum_0` 当成两个 kernel。** 同一个 kernel 的 C++ 函数名与 Python 绑定名。
7. **用首次运行耗时评价性能。** 首次包含 5–15 秒的编译；结构断言与耗时指标要分开看。

## 自我检查

- [ ] 我能不看讲义，按顺序说出六段流水线的名字与各自的输入/输出表示。
- [ ] 我知道表示层切换发生在哪两段之间，为什么第 3 段不算切换。
- [ ] 我能解释 `mm + add` 为什么在产物里变成了 `extern_kernels.addmm`，以及这是哪一段决定的。
- [ ] 我能从 `ir_post_fusion.txt` 里指出 `op1` 与 `op2` 融合成了哪个节点，并说出融合判据。
- [ ] 我能解释 `buf2 = buf1; del buf1  # reuse` 来自哪一段的决策。
- [ ] 我知道 `step 1` 与 `x1 += 8` 分别指什么，不会把两者混为一谈。
- [ ] 我能说出 `fx_graph_readable.py` 与 Dynamo 图的关系。

## 延伸阅读

- **`torch/_inductor/compile_fx.py`**：读 `compile_fx_inner` 的注释与调用链，理解“一张图从进入到产物落盘”经过哪些函数；对应本章 1.1 的总图。
- **`torch/_inductor/graph.py`**：读 `GraphLowering.run`，对照 `ir_pre_fusion.txt` 的节点顺序，确认 lowering 是逐节点进行的。
- **`torch/_inductor/scheduler.py`**：读 `Scheduler` 类文档字符串与 `fuse_nodes`，理解融合的判据；第 9、10 章会展开。
- **`torch/_inductor/codegen/cpp.py`**：读 `CppKernelProxy`，看一个调度节点如何被拆成“向量化主循环 + 尾部循环”；第 14、15 章会展开。
- **PyTorch 官方 “torch.compile 内部机制” 演讲/文档**：读 Dynamo → AOTAutograd → Inductor 的分段说明，作为本章总图的第二来源。

[上一章：环境与基线：让 TorchInductor 在本机跑起来](./00-environment-and-baseline.md) | [下一章：三张图与两道边界](./02-three-graphs-and-boundaries.md)