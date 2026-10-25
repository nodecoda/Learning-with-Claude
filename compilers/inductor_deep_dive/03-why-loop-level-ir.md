# Ch 3 为什么是 loop-level IR

> 目标：理解 Inductor 为什么把中间表示选在“循环层”而不是“张量层”“分块层”或“block 层”，以及这个选择给后面每一章留下了什么。读完本章，你能解释 `var_ranges`、`index0`、`ops.reduction` 为什么长这样，也能说出哪些优化在这套 IR 里做不了、被交给了谁。

> 环境：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口 `code/tools/run.ps1`。

## 为什么

同一个矩阵加法，四种编译器会写成四种 IR：

- TVM 的 te 写张量表达式，再由 schedule 原语决定怎么分块、怎么向量化；
- MLIR 的 Linalg 写在 tensor/memref 上，用 tiling 与 fusion 变换；
- Triton 写 block 级程序，`tl.load` 一次搬一个 block，线程映射交给编译器；
- Inductor 写循环层 IR：一个循环嵌套、一组 buffer 读写、一组下标表达式。

这四种选择没有绝对优劣，但它们决定了“你能在 IR 上表达什么优化”。Inductor 选了循环层，于是它的强项是**融合**，弱项是**分块级优化**；它把后者交给了外部 kernel 与模板，把 GPU 的 block 层交给了 Triton 那一侧的后端。

理解这一点很实用。第 9 到 16 章会反复出现“调度器在循环空间上做决策”“codegen 决定向量宽度与线程数”这类描述；如果你心里默认 Inductor 像 TVM 那样有 tile 层，就会一直找不到对应的概念，误以为漏读了章节。

## 主线

### 3.1 四个层次：调度单位不同

用“调度单位”来区分这四类 IR，比记名字清楚：

| 编译器 | IR 的调度单位 | 典型原语 | 分块由谁决定 |
| --- | --- | --- | --- |
| TVM（te + schedule） | 张量表达式上的循环变换 | `split`、`reorder`、`tile`、`vectorize`、`unroll`、`bind` | 用户写在 schedule 里 |
| MLIR（Linalg） | 结构化算子 + 变换 | tiling、fusion、vectorization、bufferization | 变换管线（transform/tiling 参数） |
| Triton（block 程序） | 一个 block 的 program | `tl.load`、`tl.store`、`tl.dot`、program id | 用户写 block 大小，编译器映射到线程 |
| Inductor（loop-level IR） | 一个循环嵌套 + buffer | `ops.load`、`ops.store`、`ops.reduction`、`var_ranges` | codegen 阶段按 ISA 与线程数决定，IR 层不表达 |

注意最后一行：Inductor 的 IR 里**没有 tile 尺寸这个字段**。`ir_pre_fusion.txt` 里能看到的只有循环变量的范围（`var_ranges`）、下标表达式（`index0`）、以及 buffer 布局（`FixedLayout`）。分块、向量宽度、线程数都出现在后面的 codegen 阶段。

这不是“还没实现”，而是设计取舍的结果。Inductor 的目标场景是“用户写普通 PyTorch 代码，框架负责把它变快”，所以它优先保证覆盖率高：任何能降低成逐元素、归约、广播的算子都能进来；同时把复杂度集中在一处——调度器决定融合，codegen 决定落地细节。

### 3.2 融合优先：证据在 IR 与日志里

Inductor 的核心优化是融合。这一点在产物里有三处独立证据。

**证据一：未融合 IR 的节点划分。** `ir_pre_fusion.txt` 有三个节点，其中 `op1` 是归约、`op2` 是点态：

```text
op1: SchedulerNode(ComputedBuffer)
op1.writes = [MemoryDep('buf1', c0, {c0: 64})]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1.group.iteration = ((64,), (64,))
```

`group.iteration` 里两个元组是“外层迭代空间”和“内层迭代空间”。融合的可行性判断就落在这些空间上：两个节点的迭代空间能不能对齐。

**证据二：融合日志的节点分类。**

```text
[__fusion] fuse_nodes_once, candidates:
[__fusion]   ExternKernelSchedulerNode(name='op0')
[__fusion]   SchedulerNode(name='op1'), Reduction(['[64]', 'sum', ...])
[__fusion]   SchedulerNode(name='op2'), Pointwise(['[64]', ...])
[__fusion] found 1 possible fusions
[__fusion] fusing op1 with op2
[__fusion] completed fusion round (1/10): fused 3 nodes into 2 nodes
```

日志把每个候选节点的“类型标签”打出来了：`Reduction`、`Pointwise`。融合决策就是在这些标签与迭代空间上做的。`op0` 是 extern kernel，它不参与这种融合。

**证据三：融合后的节点名。**

```text
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
```

`OuterLoopFusedSchedulerNode` 这个类名直接说明了融合方式：外层循环融合——两个节点的外层迭代共享，内层保持各自的形态（一个归约、一个逐元素）。第 9、10 章会把融合的纵向/横向判据拆开。

### 3.3 循环层能表达什么，不能表达什么

循环层 IR 的表达力可以用三类结构来界定：

**能表达：**

- 点态运算：任意多个 `ops.load` / 算术 / `ops.store`。
- 归约：`ops.reduction(dtype, acc_dtype, 'sum', value)` 与 `ops.store_reduction`，支持沿某个循环变量归约。
- 广播：通过下标表达式实现，不需要专门的 broadcast 节点。`index0 = 64*p0 + p1` 里 `p0` 是行、`p1` 是列；把 `p1` 从下标里去掉就得到按行广播。
- 内存复用：buffer 的读写关系是显式的，调度器可以判断何时允许原地写（`can_inplace=True`）。

**不能表达（或表达得很别扭）：**

- 显式分块：IR 里没有“把 64×64 切成 8×8 块”的构造。分块要等到模板化 kernel（例如 GEMM 模板）或外部库。
- 寄存器级流水：没有 software pipelining、double buffering 这类原语。
- 线程/ warp 级协作：`#pragma omp` 是 codegen 生成的，不是 IR 里的节点。
- 需要跨迭代重排的优化：循环顺序在 IR 里基本固定，重排属于调度阶段的受限操作。

这张清单解释了后面几章的很多“为什么”：为什么向量化要去读 `codegen/cpp.py` 而不是 IR；为什么 GEMM 走 extern kernel；为什么 GPU 侧需要另一套后端。

### 3.4 代价落在 codegen：三个决策、三个数字

循环层把“怎么落地”推给了 codegen，于是三个关键数字都出现在生成代码里，而不是 IR 里：

| 数字 | 出现位置 | 决策依据 | 本机实测 |
| --- | --- | --- | --- |
| 向量宽度 8 | `x1+=8LL`、`Vectorized<float>` | ISA 位宽 ÷ 元素位宽（256 ÷ 32） | 8 |
| 线程数 4 | `#pragma omp parallel num_threads(4)` | `torch.get_num_threads()` | 4 |
| 尾部循环 | 主循环后的标量收尾 | 元素数能否被向量宽度整除 | 本例 64 可整除，无尾部 |

本机 `valid_vec_isa_list()` 返回 `['VecAVX2']`，没有 AVX512，所以向量宽度是 8 而不是 16。这是“循环层 + codegen 决策”组合的直接后果：同一份 IR，换一台有 AVX512 的机器，生成代码会不同，而 IR 一字不改。

换成 TVM 那套写法，向量宽度会出现在 schedule 的 `vectorize` 因子或 `bind` 的 lane 数里；换成 Triton，block 大小会出现在 program 的参数里。三种位置对应三种“谁负责这类决策”的答案。

### 3.5 从 IR 反推 kernel：一次真实推演

拿 `ir_pre_fusion.txt` 的三个节点，按循环结构推一遍最终 kernel 应该长什么样。这一步是第 11 章的核心技能，本章先做一次简化版。

第一个节点是 extern kernel：64×64 的 `addmm`，写出 `buf0`。它不生成循环，只生成一次外部调用。

第二个节点是归约，循环体是：

```text
var_ranges = {p0: 64, p1: 64}
index0 = 64*p0 + p1
index1 = p0
load('buf0', index0) -> relu -> reduction('sum') -> store_reduction('buf1', index1)
```

推演：外层 `p0` 走 64 行，内层 `p1` 走 64 列，每行累加出一个值写进 `buf1[p0]`。归约是沿内层的，所以内层适合向量化（连续读 `buf0`），外层适合分线程（行与行之间独立）。

第三个节点是 64 元素除法，循环体是 `load('buf1') -> truediv(64.0) -> store('buf2')`。它的迭代空间只有一维 64，元素太少，分给 4 个线程不划算。

两个节点融合后，实际生成的 C++ 与推演一致：

```cpp
#pragma omp parallel num_threads(4)
{
    #pragma omp for
    for (int64_t x0 = 0; x0 < 64; x0 += 1) {          // 归约：外层分线程
        at::vec::Vectorized<float> tmp_acc0_vec = ...; // 内层向量化
        for (int64_t x1 = 0; x1 < 64; x1 += 8) { ... }
        in_out_ptr0[x0] = ...;
    }
    #pragma omp single
    { /* 除法：单线程，向量化步长 8 */ }
}
```

三个推演结论都被产物验证了：归约的外层被分线程（`#pragma omp for`）；归约的内层被向量化（`x1 += 8`）；点态段被放进 `#pragma omp single`，因为 64 个元素的并行收益低于同步成本。

值得注意 `in_out_ptr0` 这个名字：归约的结果直接写进输出指针，除法再原地读改写。这与 wrapper 里的 `buf2 = buf1; del buf1  # reuse` 对应。循环层的 IR 允许这种原地复用，是因为读写关系是显式的。

### 3.6 这套选择什么时候显得别扭

循环层 IR 不是万能的，遇到下面三类情形会明显吃亏：

**第一类：矩阵乘这类需要分块的算子。** 循环层没有 tile 构造，自己生成一个高性能 GEMM 不现实，所以 Inductor 把它交给外部 kernel（本机是 `extern_kernels.addmm`），或者在特定条件下用模板化实现。第 17 章会讲这条路径的判据。

**第二类：需要寄存器级优化的算子。** 卷积、注意力这类算子依赖分块 + 数据复用 + 流水，循环层表达不了，通常走后端库或专用实现。

**第三类：GPU 上的 block 级并行。** GPU 需要显式管理 block 与线程映射，Inductor 在 GPU 路径上把 kernel 生成交给 Triton 这类 block 级后端：IR 的循环被翻译成 block 程序，再由后端处理线程映射。也就是说，“Inductor 是循环层”这句话对 CPU 侧最准确；GPU 侧它更像“循环层 IR + block 层后端”的组合。

把这三类记下来，后面读到“为什么这里走 extern”“为什么这个 kernel 不是 Inductor 生成的”时，就不会误判成缺陷。

### 3.7 同一段归约在四种 IR 里的写法

把 lab_a 的第二行语义（`sum(relu(x @ w + b), dim=1)`）分别写成四种 IR 的骨架，差异会更直观：

| IR | 骨架写法 | 谁决定向量宽度 | 谁决定分块 |
| --- | --- | --- | --- |
| TVM te | `B = te.compute((64,), lambda i: te.sum(A[i, k], axis=k))` 再由 schedule 加 `split` / `vectorize` | schedule 里的 `vectorize(n)` | schedule 里的 `split` / `tile` |
| MLIR Linalg | `linalg.generic` 带 reduction iterator，再由 tiling pass 切分 | vectorization pass | tiling pass 的 tile 尺寸 |
| Triton | 一个 program 处理一行或一个 block，`tl.sum` 归约块内元素 | 编译器按 block 大小与硬件映射 | 用户写的 block 大小与 grid |
| Inductor IR | `var_ranges = {p0: 64, p1: 64}` 加 `ops.reduction(...)` | `codegen/cpp.py` 按 ISA 决定 | 不表达；大算子走模板或外部 kernel |

四个格子填完，“为什么 Inductor 的 IR 里只有 `var_ranges` 与 `index`”就有答案了：它选择了最少的信息，把其余决策留给下游。这份 IR 的优点是任何能降低成循环的算子都能进来，缺点是它无法承载依赖分块的优化。

还有一处细节值得注意：Inductor 的循环体里，归约是**一个算子调用**而不是嵌套的 `for`：

```text
reduction = ops.reduction(torch.float32, torch.float32, 'sum', relu)
store_reduction = ops.store_reduction('buf1', get_index_1, reduction)
```

真正生成几层循环，由 codegen 读 `var_ranges` 与 `index` 决定。这就是“IR 只描述算什么与按什么下标访问，不描述循环怎么展开”的具体形态。

## 可跑实验

运行主线实验：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

本机输出（节选）：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)
4) 生成的 C++ kernel 结构
  name         kernel
  vectorized   True
  step         1
  threads      4
  tmp_ops      6
  cpp_lines    108
  extern kernels: ['addmm']
```

然后做三个对照观察，每个都对应本章一个论点：

1. **IR 里没有向量宽度。** 打开 `ir_pre_fusion.txt`，搜 `8`，你会发现找不到 `x1 += 8` 这类信息；它只在 `output_code.py` 里。这验证了“向量宽度是 codegen 决策”。
2. **IR 里没有线程数。** 同样，`num_threads(4)` 只出现在生成代码里。IR 层只有 `var_ranges`。
3. **融合发生在调度器。** 打开 `ir_post_fusion.txt`，`OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)` 出现一次；打开 `ir_pre_fusion.txt`，同样的两个节点是分开的。这验证了“融合单位是循环节点”。

再补一个结构观察：打开 `output_code.py`，找到 `#pragma omp single` 那一段。它包住的是 64 元素除法。如果你把 `h.shape[1]` 换成一个大得多的数（例如输入改成 64×4096），这一段是否还适合单线程，就是一个可以自己推演的问题——不需要真的跑，只要能从 `var_ranges` 与迭代空间推出来。

想看融合决策的原始日志，把通道打开（`fusion` 为 INFO，`+fusion` 为 DEBUG）：

```powershell
$env:TORCH_LOGS="fusion"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

期望看到 `fused 3 nodes into 2 nodes`，以及第二轮 `found 0 possible fusions`。如果一行都没打印，是缓存命中了：调度器没跑。

## 源码落点

- `torch/_inductor/ir.py`：读 `TensorBox`、`StorageBox`、`ComputedBuffer`，确认 IR 层的“值”最终都落在 buffer 上；这是循环层选择的直接体现。
- `torch/_inductor/loop_body.py`：读 `LoopBody` 的 `var_ranges`、`index`、`body` 三部分，理解循环层 IR 的最小完备信息。
- `torch/_inductor/scheduler.py`：读 `SchedulerNode` 与 `fuse_nodes`，看融合如何只看迭代空间与依赖，不看算子语义。
- `torch/_inductor/codegen/cpp.py`：读 `CppKernelProxy` 与 `CppScheduling`，看循环如何被拆成向量化主循环与尾部；这是“决策下推”的落点。
- `torch/_inductor/cpu_vec_isa.py`：读 `VecAVX2`，确认向量宽度来自 ISA 位宽，而不是 IR。
- `torch/_inductor/select_algorithm.py`：读 extern kernel 的选择逻辑，理解为什么 `addmm` 不自己生成。

## 常见坑

1. **在 IR 里找分块参数。** 循环层 IR 没有 tile 尺寸；分块属于模板或外部 kernel。
2. **以为 Inductor 有 autotune 的 tile 搜索。** 本机 CPU 路径上，能搜索的是外部 kernel 的选择（例如 GEMM 实现），不是 IR 层的 tile 组合。
3. **把 `var_ranges` 当 shape。** shape 在 `buf.layout`，`var_ranges` 是循环范围；归约时两者可以不一致。
4. **以为向量宽度写在 IR 里。** 它由 codegen 按 ISA 决定，本机 AVX2 对应 8 个 float32。
5. **以为融合在 pass 阶段完成。** post-grad passes 在 ATen 图上跑，融合在调度器里做；两者产物不同。
6. **把 `addmm` 当成 Inductor 生成的 kernel。** 它是 extern kernel，产物里只有调用，没有对应的 C++ 循环。
7. **看到 GPU 讨论就套用本章结论。** 循环层对 CPU 侧最准确；GPU 侧的 block 映射由后端负责。

## 自我检查

- [ ] 我能用“调度单位”一句话区分 TVM、MLIR Linalg、Triton、Inductor 四者。
- [ ] 我能说出 Inductor 循环层 IR 能表达的三种结构与表达不了的三类优化。
- [ ] 我能解释为什么向量宽度与线程数不在 IR 里。
- [ ] 我能从 `var_ranges` 与 `index` 推出归约 kernel 的循环层次。
- [ ] 我能说明 `OuterLoopFusedSchedulerNode` 里的 `OuterLoop` 指什么。
- [ ] 我能举出两个“这套选择显得别扭”的场景，并说出各自的替代路径。
- [ ] 我知道本机为什么是 8 而不是 16 个 float 的向量宽度。

## 延伸阅读

- **TVM 的 “Tensor Expressions and Schedules” 文档**：读 `split`、`tile`、`vectorize`、`bind` 几个原语，理解 tile 层把决策放在 schedule 里的做法；与本章 3.1 对照。
- **MLIR Linalg 文档**：读 “Linalg dialect” 与 tiling 相关章节，理解结构化算子与变换管线的关系。
- **Triton 官方教程**：读 “Matrix Multiplication” 一篇，看 block 大小与 program id 如何写进 kernel；对应本章 3.1 与 3.6。
- **`torch/_inductor/loop_body.py`**：读 `LoopBody` 全类，确认循环层 IR 里到底有哪些字段；这是“最小完备信息”的直接证据。
- **`torch/_inductor/scheduler.py`**：读 `fuse_nodes` 的注释与日志，理解融合判据只依赖迭代空间与依赖，而不依赖算子语义。

[上一章：三张图与两道边界](./02-three-graphs-and-boundaries.md) | [下一章：练习 A 一次编译的六段对照](./practice-a-pipeline-trace.md)
