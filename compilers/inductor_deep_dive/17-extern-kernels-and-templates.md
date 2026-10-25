# Ch 17 extern kernel 与模板：为什么 `x @ w + b` 不生成循环

> **目标**：理解 Inductor 什么时候放弃“自己生成循环”，把算子交给 `extern_kernels` 或 C++ 模板。读完后你能从 `output_code.py` 认出 `extern_kernels.addmm(...)`，从调度 IR 认出 `ExternKernelSchedulerNode`，说清 `use_cpp_gemm_template` 的条件，并判断一个模型里哪些部分值得自研 kernel、哪些应该交给库。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe` 14.43）。默认 `max_autotune=False`、`max_autotune_gemm=False`、`cpp.weight_prepack=True`。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

本章使用 [`code/labs/lab_a_pipeline.py`](./code/labs/lab_a_pipeline.py)。前一章是 [Ch 16 并行化](./16-parallelization.md)，下一章是 [练习 C 自定义 lowering](./practice-c-custom-lowering.md)。

## 为什么

到此为止，我们已经能把一个逐元素或归约算子变成相当像样的 C++：向量化、OpenMP、尾部处理都有。那为什么 `x @ w + b` 不这么做？

答案不是“Inductor 不会写矩阵乘法”，而是“自己写通常不划算”。一个高性能 GEMM 需要：

- 按 L1/L2/L3 分层做 blocking，块大小依赖具体 CPU；
- 打包（pack）权重，让内层循环连续读取；
- 多线程划分与任务窃取，避免负载不均；
- 处理边界块、转置、非连续 stride、bias epilogue；
- 在 AVX2/AVX-512/NEON 上选择不同的 micro-kernel。

这些工作 ATen、oneDNN、MKL 已经做了很多年。Inductor 的默认策略是：逐元素与归约自己生成，矩阵乘这类“库更强”的算子走 extern；只有在开启 autotune、权重可预打包等条件满足时，才用 C++ 模板参与竞争。

这带来一个重要的结构后果：**extern kernel 是不透明的**。调度器看得见它的输入输出，却看不见它的循环体，因此不能把它和邻居融合进同一个 loop nest。`relu(x @ w + b)` 里的 `addmm` 会先物化一个 64×64 的 `buf0`，`relu` 再从这个 buffer 读。这个边界是理解性能与内存占用的关键。

本章沿着 `x @ w + b` 走一遍：从 lowering 的选择，到调度 IR 的 `ExternKernelSchedulerNode`，再到 wrapper 里的 `extern_kernels.addmm(...)`，最后看模板路径的条件与代价。

## 主线

### 1. 默认路径：`tuned_addmm` 只给一个 extern 选择

`torch/_inductor/kernel/mm.py` 里注册了 `aten.addmm` 的 lowering：

```python
@register_lowering(aten.addmm, type_promotion_kind=None)
def tuned_addmm(inp, mat1, mat2, *, alpha=1, beta=1, layout=None):
    ...
    m, n, k, layout, mat1, mat2, inp_expanded = mm_args(mat1, mat2, inp, layout=layout)
    inp = realize_inputs(inp)
    static_shape, is_nonzero = _is_static_problem(layout)
    name = "addmm"
    ...
    kernel_inputs = MMKernelInputs([inp_expanded, mat1, mat2], scalars=dict(alpha=alpha, beta=beta))
    kernel_inputs_aten = MMKernelInputs([inp, mat1, mat2], scalars=dict(alpha=alpha, beta=beta))
    ...
    if (not is_nonzero) or (not (inductor_config.max_autotune or inductor_config.max_autotune_gemm)):
        choices.extend(V.choices.get_template_configs(kernel_inputs_aten, [aten_addmm], name))
        node, _ = autotune_select_algorithm(name, choices, kernel_inputs.nodes(), layout)
        return node
```

`aten_addmm` 是在文件上方注册的 `ExternKernelChoice`：

```python
aten_addmm = ExternKernelChoice(
    torch.addmm, "at::addmm_out", op_overload=aten.addmm.out
)
```

默认配置下 `max_autotune=False`、`max_autotune_gemm=False`，条件成立，`choices` 里只有 `aten_addmm` 一个。`autotune_select_algorithm` 没有可比较的候选，直接返回这个 extern 选择。于是 lowering 的结果不是 `ComputedBuffer`，而是一个 `ExternKernelOut`。

`ExternKernelChoice` 的 `call_name` 决定了生成代码里的名字：

```python
def call_name(self):
    return f"extern_kernels.{self.name}"
```

`self.name` 默认取 `kernel.__name__`，这里就是 `addmm`。所以 wrapper 里出现 `extern_kernels.addmm(...)`，而不是 `torch.addmm` 或 `at::addmm_out`。`extern_kernels` 本身是 `select_algorithm.py` 里的 `KernelNamespace`，负责把注册过的 extern kernel 暴露成一个稳定的模块命名空间。

默认路径的完整选择链是：

```text
aten.addmm
  -> tuned_addmm
  -> ExternKernelChoice(torch.addmm, "at::addmm_out")
  -> ExternKernelOut
  -> ExternKernelSchedulerNode
  -> wrapper: extern_kernels.addmm(...)
```

### 2. 调度 IR：`ExternKernelSchedulerNode` 没有 loop body

lab_a 的调度 IR（`ir_pre_fusion.txt` / `ir_post_fusion.txt`）第一段是：

```text
op0: ExternKernelSchedulerNode(ExternKernelOut)
op0.writes = [StarDep(name='buf0', mode=None)]
op0.unmet_dependencies = []
op0.met_dependencies =
    [   StarDep(name='arg0_1', mode=None),
        StarDep(name='arg1_1', mode=None),
        StarDep(name='arg2_1', mode=None)]
op0.min_input_distance = 0
op0.max_input_distance = 0
op0.outputs = [
    buf0: ExternKernelOut
    buf0.layout = FixedLayout('cpu', torch.float32, size=[64, 64], stride=[64, 1])
    buf0.users = [NodeUser(node=SchedulerNode(name='op1'), can_inplace=False, is_weak=False)]
]
op0.node.kernel = extern_kernels.addmm
```

把它和 `op1` 的普通 `SchedulerNode` 对比：

| 字段 | `ExternKernelSchedulerNode` | `SchedulerNode` |
|---|---|---|
| 内部节点 | `ExternKernelOut` | `ComputedBuffer` |
| 依赖 | `StarDep`（整块 buffer） | `MemoryDep`（带索引） |
| loop body | 没有 | `class op1_loop_body` |
| 融合能力 | 不参与 loop 级融合 | 可与邻居垂直/水平融合 |
| 生成物 | 一次库调用 | 一个 C++ kernel |

`StarDep` 表示“整个 buffer 都被读写”，没有逐元素索引；`MemoryDep('buf0', c0, {c0: 64})` 才表示按 `c0` 索引访问。没有 loop body，`Scheduler` 就没有可合并的循环，`can_fuse_vertical` 也就不会把 `op1` 并进 `op0`。

`buf0.users` 指向 `op1`，`can_inplace=False`；`op1` 的输出 `buf1` 的 user 是 `op2`，`can_inplace=True`。这说明 extern 的输出是一个真实物化的 buffer，后续逐元素节点从它读，而不是把 `relu` 塞进 `addmm` 的 epilogue。

### 3. wrapper：extern 调用如何出现

lab_a 的 `output_code.py` 里，wrapper 的 `call` 是：

```python
def call(self, args):
    arg0_1, arg1_1, arg2_1 = args
    args.clear()
    assert_size_stride_grouped((arg2_1, arg0_1, arg1_1), ((64, ), (64, 64), (64, 64)), ((1, ), (64, 1), (64, 1)), 'input')
    buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
    # Topologically Sorted Source Nodes: [add], Original ATen: [aten.add]
    extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
    del arg0_1
    del arg1_1
    del arg2_1
    buf1 = empty_strided_cpu((64, ), (1, ), torch.float32)
    buf2 = buf1; del buf1  # reuse
    cpp_fused_div_relu_sum_0(buf2, buf0)
    return (buf2, )
```

几个细节：

- `extern_kernels.addmm` 的参数顺序是 `(bias, x, w, alpha, beta, out)`，不是 `(x, w, bias)`。`mm_args` 在 lowering 阶段已经把 `inp` 展开并放到首位。
- `out=buf0` 表示输出写进预分配的 buffer，避免库内部再分配。`buf0` 的 layout 是 `size=[64, 64], stride=[64, 1]`，来自 `FixedLayout`。
- `alpha=1, beta=1` 是 `x @ w + b` 的默认系数；`beta=0` 时 bias 会被忽略，`alpha=0` 时矩阵乘结果被忽略，源码在 CUDA 路径上有专门分支。
- `buf2 = buf1; del buf1  # reuse` 是内存规划的结果：归约输出 `buf1` 可以直接复用给最终输出。extern 的 `buf0` 在 `cpp_fused...` 调用后仍然活着，直到 kernel 读完。

`extern_kernels` 的导入来自 `output_code.py` 顶部：

```python
from torch._inductor.select_algorithm import extern_kernels
```

`common.wrapper_extern_calls` 用正则 `extern_kernels\.(\w+)\(` 扫这份源码，lab_a 的结果是 `['addmm']`。这也是写结构断言最稳的方式：不依赖 kernel 名，只依赖“有没有 extern 调用、调用的是谁”。

### 4. 为什么不能把 `relu` 融进 `addmm`

从调度角度看，融合需要两个节点都有可合并的 loop body 或至少可重写的索引。`ExternKernelSchedulerNode` 没有 body，只有一次黑盒调用；调度器无法把 `relu` 的 `ops.relu` 插到 GEMM 的内层循环里。

从库的角度看，`at::addmm_out` 是一个完整的 BLAS 调用，内部可能已经做了 blocking、packing、多线程；在它外面套一层逐元素循环需要：

- 修改库的 epilogue（oneDNN 支持 post-op，但不是所有后端都支持）；
- 或者把结果写回内存，再读一遍做 `relu`（这就是当前行为）。

当前默认路径选择后者：`addmm` 写 `buf0`，`relu`/`sum` 的融合 kernel 读 `buf0`。代价是 `buf0` 的一次写和一次读；收益是 GEMM 仍然走高度优化的库。

这也解释了为什么 `buf0` 不能被 `buf1` 复用：`op1` 的 `can_inplace=False`，`buf0` 在 `op1` 读完之后才死亡；内存规划不会把输出放到 `buf0` 上，除非依赖关系允许。

### 5. 模板路径：`CppTemplateBuffer` 与 `CppGemmTemplate`

当开启 autotune 且条件满足时，`tuned_addmm` 会把 `CppGemmTemplate` 加入候选：

```python
if use_cpp_gemm_template(layout, mat1, mat2):
    CppGemmTemplate.add_choices(
        choices, layout, kernel_inputs.nodes(),
        alpha=alpha, beta=beta, has_bias=True,
    )
```

`use_cpp_gemm_template` 的条件（`utils.py`）不是“CPU 就行”，而是一串与：

```python
if not _use_template_for_cpu(layout) or not _use_autotune_backend("CPP"):
    return False
if not config.cpp.weight_prepack:
    return False
...
return (
    layout.dtype in layout_dtypes
    and micro_gemm is not None
    and is_last_dim_stride1(mat1)
    and isinstance(mat2, ir.StorageBox)
    and (mat2.is_module_buffer() or not require_constant_mat2)
)
```

逐条拆开：

| 条件 | 含义 | 默认值 |
|---|---|---|
| `_use_template_for_cpu(layout)` | `max_autotune` 或 `max_autotune_gemm` 为真，且设备是 CPU | `False` |
| `_use_autotune_backend("CPP")` | `max_autotune_gemm_backends` 包含 `CPP` | 默认后端列表包含 CPP |
| `config.cpp.weight_prepack` | 允许权重预打包 | `True` |
| `layout.dtype in layout_dtypes` | float32/bf16/fp16/uint8/int8 | 覆盖常见 dtype |
| `micro_gemm is not None` | 能为该 shape 构造 micro-kernel | 取决于 m/n/k |
| `is_last_dim_stride1(mat1)` | 输入最后一维连续 | 常见布局满足 |
| `mat2` 是 `StorageBox` 且是模块 buffer | 权重可预打包、生命周期稳定 | 推理权重满足 |

默认 `max_autotune=False`，所以第一项就为假，`CppGemmTemplate` 不会进入候选，`extern_kernels.addmm` 是唯一选择。这也解释了为什么在默认配置下翻遍 `output_code.py` 都找不到 GEMM 模板代码。

当模板被选中时，lowering 返回的是 `CppTemplateBuffer`，调度器把它包成普通 `SchedulerNode`，`CppScheduling.is_cpp_template` 通过类型判断：

```python
def is_cpp_template(self, node):
    return isinstance(node, SchedulerNode) and isinstance(node.node, ir.CppTemplateBuffer)
```

`codegen_template` 走另一条路径：

```python
ctb = cast(ir.CppTemplateBuffer, template_node.node)
epilogue_ir_nodes = [n.node for n in epilogue_nodes]
if not all(isinstance(n, ir.ComputedBuffer) for n in epilogue_ir_nodes):
    raise AssertionError("Epilogue nodes must all be instances of ir.ComputedBuffer")
kernel, render = ctb.make_kernel_render(ctb, flag_template_buffer_has_other_users=..., epilogue_nodes=...)
with kernel:
    template_node.mark_run()
    for node in epilogue_nodes:
        node.mark_run()
    src_code = render()
kernel_name = self.define_kernel(src_code, [template_node, *epilogue_nodes], kernel.args)
```

模板路径允许把 `epilogue_nodes` 融进同一个模板，但有两个限制：

1. `template_node.group` 的 `rnumel` 必须为 `()`，也就是模板节点本身不做归约。
2. epilogue 节点必须全部是 `ir.ComputedBuffer`；`MultiOutput` 之类的节点会被过滤掉。
3. 如果模板 buffer 还有其他用户，`flag_template_buffer_has_other_users` 会告诉模板不要做原地 epilogue，避免破坏后续读取。

所以“模板能融合 epilogue”不是无条件的；它只处理模板输出之后的逐元素链，而且要求这些节点是普通的 `ComputedBuffer`。

### 6. extern、模板、自研 kernel 的分工

把三条路放在一起：

| 路径 | 触发条件 | 优点 | 代价 |
|---|---|---|---|
| `extern_kernels` | 默认，或 autotune 关闭 | 库高度优化、覆盖广、实现稳定 | 不透明、不能 loop 融合、输出物化 |
| `CppTemplateBuffer` | autotune 开启、CPU、权重可打包、静态 n/k | 可融合 epilogue、可 autotune | 编译时间长、条件多、维护复杂 |
| 自研 C++ kernel | 逐元素、归约、简单融合 | 可完全控制、无库调用开销 | 需要自己处理向量化/并行/边界 |

判断标准不是“自研更酷”，而是“这项工作是否已经有人做得更好”。矩阵乘、卷积、FFT 这类算子的库实现通常远超手写循环；逐元素链、归约、简单 broadcast 则是 Inductor 的强项，因为融合能省掉中间 buffer。

一个实用的经验法则：

- 如果算子的核心是“大块矩阵运算 + 标准 epilogue”，优先看 extern 或模板。
- 如果算子的核心是“逐元素 + 归约 + 少量 broadcast”，交给默认代码生成。
- 如果算子的语义特殊、库没有对应实现，才考虑自定义 lowering + 生成 kernel。
- 如果确实要自研 GEMM，先确认打包、线程、边界、dtype 支持是否都能达到库的水平；否则很容易“代码更短、性能更差”。

### 7. 读产物：三处证据

判断一个模型里有没有 extern，最直接的三处证据：

**第一处：调度 IR。** 搜 `ExternKernelSchedulerNode` 与 `op0.node.kernel`：

```text
op0: ExternKernelSchedulerNode(ExternKernelOut)
op0.node.kernel = extern_kernels.addmm
```

**第二处：wrapper 调用。** 搜 `extern_kernels.`：

```text
extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
```

**第三处：`common.wrapper_extern_calls(src)`。** 它返回排序后的 extern 名列表，lab_a 是 `['addmm']`。写测试时用它比硬编码整行调用更稳。

如果三条证据都没有，但模型里明显有矩阵乘，检查是不是被 decomposition 成了 `mm` + `add`，或者被 `use_native_matmul` 走了别的路径；也可能是在 CUDA 上走了 Triton 模板，与本章的 CPU 路径不同。

## 可跑实验

### 实验 1：确认 `addmm` 走 extern

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

真实输出（节选）：

```text
========================================================================
3) 调度前 IR：SchedulerNode 与 loop body（节选）
========================================================================
op0: ExternKernelSchedulerNode(ExternKernelOut)
op0.writes = [StarDep(name='buf0', mode=None)]
...
op0.node.kernel = extern_kernels.addmm

========================================================================
5) wrapper 侧的调用（节选）
========================================================================
def call(self, args):
        ...
        buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
        # Topologically Sorted Source Nodes: [add], Original ATen: [aten.add]
        extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
        ...
        cpp_fused_div_relu_sum_0(buf2, buf0)

========================================================================
6) 结构断言
========================================================================
  extern kernels: ['addmm']
[PASS] matmul 走 extern kernel  (['addmm'])
```

这里的 `[PASS]` 是 lab_a 的断言，不是本章的推测。它同时证明了：同一个图里既有 extern 调用，也有生成的 C++ kernel；两者通过 `buf0` 连接。

### 实验 2：检查 extern 输出是否被复用

```powershell
$d = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
     Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -Path (Join-Path $d.FullName 'ir_post_fusion.txt') -Pattern 'ExternKernelSchedulerNode|can_inplace|buf0' -Context 0,2
```

预期能看到 `buf0.users = [NodeUser(node=SchedulerNode(name='op1'), can_inplace=False, ...)]`。`can_inplace=False` 说明 `op1` 不能把输出写回 `buf0`；这就是 extern 输出必须独立物化的证据。

### 实验 3：数一数 wrapper 里的 extern 调用

```powershell
$d = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
     Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -Path (Join-Path $d.FullName 'output_code.py') -Pattern 'extern_kernels\.' | ForEach-Object { $_.Line.Trim() }
```

预期只有一行 `extern_kernels.addmm(...)`。如果出现多行，可能是图里有多个矩阵乘，或者 decomposition 产生了额外的 extern 算子。

### 实验 4：观察 autotune 开关对候选的影响（不要求跑完）

把 `torch._inductor.config.max_autotune` 设为 `True` 会进入 `tuned_addmm` 的另一条分支，`CppGemmTemplate` 可能被加入候选；是否能真正选中还取决于 `use_cpp_gemm_template` 的全部条件与 autotune 结果。这个实验的编译时间和测量噪声都明显高于本章其他实验，本章不给出耗时数字。你可以在自己的分支上跑，但结论要基于同一台机器、同一轮测量，并且用子进程隔离缓存。

如果只想验证“默认路径是 extern”，不需要打开 autotune；实验 1 已经给出证据。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/kernel/mm.py` | `tuned_addmm` | 默认分支为什么只放 `aten_addmm`、`is_nonzero` 与 autotune 条件 |
| `torch/_inductor/kernel/mm.py` | `aten_addmm` / `aten_bias_addmm` | `ExternKernelChoice` 的注册与 1D bias 特例 |
| `torch/_inductor/select_algorithm.py` | `ExternKernelChoice` | `call_name`、`_registry`、`extern_kernels` 命名空间 |
| `torch/_inductor/select_algorithm.py` | `KernelNamespace` / `extern_kernels` | extern 名字如何变成模块属性 |
| `torch/_inductor/ir.py` | `ExternKernelOut` / `ExternKernel` | 输出 layout、`out=` 参数、物化 buffer |
| `torch/_inductor/ir.py` | `CppTemplateBuffer` | `TemplateBuffer` 的 C++ 特化、`make_kernel_render` |
| `torch/_inductor/utils.py` | `use_cpp_gemm_template` | 全部前置条件与 dtype/stride 检查 |
| `torch/_inductor/utils.py` | `_use_template_for_cpu` / `use_aten_gemm_kernels` | autotune 开关如何决定候选 |
| `torch/_inductor/codegen/cpp.py` | `CppScheduling.is_cpp_template` / `codegen_template` | 模板节点如何生成、epilogue 的限制 |
| `torch/_inductor/codegen/cpp_gemm_template.py` | `CppGemmTemplate` | micro-kernel 选择、bias epilogue、打包 |
| `torch/_inductor/codegen/wrapper.py` | `generate_kernel_call` | extern 调用如何写进 wrapper |

阅读顺序建议：先读 `tuned_addmm` 的默认分支，再看 `ExternKernelChoice.call_name`，然后读 `use_cpp_gemm_template` 的条件，最后看 `codegen_template`。这样“为什么默认是 extern、什么条件下变模板”会一次串起来。

## 常见坑

1. **以为矩阵乘一定生成 C++ 循环。** 默认配置下 `addmm` 走 `extern_kernels.addmm`，`output_code.py` 里没有 GEMM 循环。看到 `extern_kernels` 不是“没编译”，而是“交给库了”。

2. **想把 `relu` 融进 extern。** `ExternKernelSchedulerNode` 没有 loop body，调度器无法做 loop 级融合。默认路径会物化 `buf0`；要融合 epilogue，需要模板路径或库的 post-op 支持。

3. **把 `max_autotune=True` 当“一定更快”。** 它只是把模板加入候选，是否选中取决于 shape、dtype、stride、打包条件与 autotune 结果；编译时间也会上升。没有同机测量，不要下结论。

4. **忽略 `use_cpp_gemm_template` 的 `weight_prepack` 条件。** 权重不是模块 buffer、或 `cpp.weight_prepack=False` 时，模板不会参与。只看到“CPU + float32”就断言模板可用，会误判。

5. **把 `addmm` 参数顺序记成 `(x, w, bias)`。** 生成代码是 `extern_kernels.addmm(bias, x, w, alpha, beta, out)`。`mm_args` 在 lowering 阶段已经重排；读 wrapper 时按这个顺序对。

6. **忽略模板 epilogue 的类型限制。** `codegen_template` 要求 epilogue 节点全部是 `ir.ComputedBuffer`，且模板节点 `rnumel == ()`；`MultiOutput` 或多归约节点会被过滤或断言。不是所有“模板后面接的算子”都能融。

7. **在 `cpp_wrapper` 与 Python wrapper 之间混用假设。** `bias_addmm` 只有 Python 实现，`cpp_wrapper` 路径不会加它；`extern_kernels` 的调用形式也随 wrapper 类型变化。读代码前先确认当前是哪种 wrapper。

8. **忘了 extern 输出也要占内存。** `buf0` 是实打实的 64×64 分配；它不能被 `buf1` 复用，因为 `op1` 还要读。分析峰值内存时，extern 的输出必须计入 live set。

## 自我检查

- [ ] 我能说出默认配置下 `tuned_addmm` 为什么只返回 `aten_addmm` 一个候选。
- [ ] 我能从调度 IR 认出 `ExternKernelSchedulerNode`，并解释它为什么没有 loop body。
- [ ] 我能写出 `extern_kernels.addmm` 的实参顺序与 `alpha`、`beta`、`out` 的含义。
- [ ] 我能解释 `can_inplace=False` 对 `buf0` 生命周期的影响。
- [ ] 我能列出 `use_cpp_gemm_template` 至少四个前置条件。
- [ ] 我能说明模板路径允许融合 epilogue 的两个限制。
- [ ] 我能用 `wrapper_extern_calls` 或正则从 `output_code.py` 提取 extern 调用。
- [ ] 我能判断一个算子应该走 extern、模板还是自研 kernel，并说出理由。

## 延伸阅读

- **`torch/_inductor/kernel/mm.py` 的 `tuned_addmm`**：读哪里——默认分支与 autotune 分支的 `choices` 构造；学什么——候选集如何决定最终是 extern 还是模板；关联——lab_a 的 `addmm`。
- **`torch/_inductor/select_algorithm.py` 的 `ExternKernelChoice` 与 `KernelNamespace`**：读哪里——`call_name`、`_registry`、`extern_kernels` 的属性注册；学什么——`extern_kernels.addmm` 这个名字从哪来；关联——wrapper 调用。
- **`torch/_inductor/ir.py` 的 `ExternKernelOut` 与 `CppTemplateBuffer`**：读哪里——`ExternKernelOut` 的 layout/out 参数、`CppTemplateBuffer` 的 `make_kernel_render`；学什么——extern 与模板在 IR 层的差异；关联——调度 IR 的两种节点。
- **`torch/_inductor/utils.py` 的 `use_cpp_gemm_template`**：读哪里——从 `_use_template_for_cpu` 到 `micro_gemm`、`is_last_dim_stride1` 的全部条件；学什么——模板不是默认路径；关联——实验 4。
- **`torch/_inductor/codegen/cpp.py` 的 `CppScheduling.codegen_template`**：读哪里——epilogue 过滤、`rnumel == ()` 断言、`make_kernel_render`；学什么——模板融合的边界；关联——为什么 `relu` 默认不融进 `addmm`。
- **oneDNN 文档的 GEMM post-ops 与 packed weights**：读哪里——post-op 支持范围、packing 的生命周期；学什么——库在什么条件下能替你融合 epilogue；关联——模板路径的动机。
- **`compilers/inductor_deep_dive/12-memory-planning.md`**：读哪里——live interval 与 buffer 复用；学什么——为什么 extern 的 `buf0` 不能被复用；关联——本章第 4 节。

[上一章：Ch 16 并行化](./16-parallelization.md) | [下一章：练习 C 自定义 lowering](./practice-c-custom-lowering.md)