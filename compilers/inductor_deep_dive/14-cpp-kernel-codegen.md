# Ch 14 C++ Kernel 生成：从 loop body 到可编译的函数

> **目标**：把 `LoopBody` 变成 `output_code.py` 里那段以 `extern "C" __declspec(dllexport)` 开头的 C++。读完后你能解释 `CppKernel`、`CppVecKernel`、`CppKernelProxy`、`CppScheduling`、`KernelGroup` 各自负责什么，能从 `x0`、`x1`、`#pragma omp`、`auto tmpN` 反推出生成路径，并知道 `cpp_fused_div_relu_sum_0` 这个名字是在哪一步产生的。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe` 14.43）。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

本章使用 [`code/labs/lab_a_pipeline.py`](./code/labs/lab_a_pipeline.py) 与 [`code/labs/lab_c_lowering.py`](./code/labs/lab_c_lowering.py)。前一章是 [Ch 13 LoopBody 与 ops_handler](./13-loop-body-and-ops-handler.md)，下一章是 [Ch 15 向量化](./15-vectorization.md)。

## 为什么

第 13 章结束时，我们有的是一张“循环里做什么”的图：`ops.load`、`ops.relu`、`ops.reduction`、`ops.store_reduction`。它不是 C++，因为还缺很多工程决定：

- 这段 body 要不要和邻居一起装进同一个函数？
- 外层循环变量叫什么，步长是 1 还是 8？
- 归约累加器放在循环外还是数组里？
- 尾部不足一个向量宽度的元素由谁处理？
- 多个 buffer 的实参如何排成函数签名？
- 生成的函数叫 `kernel` 还是 `cpp_fused_div_relu_sum_0`？
- 哪些语句必须等内存规划结束后才能定稿？

`CppKernel` 及其子类就是回答这些问题的层。它与 `LoopBody` 的分工很像“虚拟机指令”与“编译器后端”：body 负责语义，kernel 负责布局、循环、CSE、buffer 和文本。

这一层的代码量不小，但结构清楚。抓住一条主线就不会迷路：

```text
SchedulerNode[] 
  -> CppScheduling.codegen_node
  -> CppKernelProxy.codegen_nodes
  -> CppKernelProxy.codegen_functions
       -> CppKernel (标量)
       -> LoopNest.tile
       -> CppVecKernel (主向量循环)
       -> CppKernel/CppVecKernel (尾部)
  -> KernelGroup.finalize_kernel
  -> KernelGroup.codegen_group
  -> CppScheduling.define_kernel
  -> wrapper.call_kernel
```

本章沿着这条线走一遍，再用两份真实产物做对照。

## 主线

### 1. CppKernel：三块缓冲区与一次函数体拼装

`CppKernel` 继承 `Kernel`，后者提供 `loads`、`compute`、`stores` 三个 `IndentedBuffer` 与 `CSE`。`CppKernel.gen_body` 的顺序是固定的：

```python
def gen_body(self, code: BracesBuffer | None = None):
    code = BracesBuffer()
    with contextlib.ExitStack() as stack:
        if hasattr(self, "codegen_inner_loops"):
            code.splice(self.preloads)
            self.codegen_inner_loops(code)
            stack.enter_context(code.indent())
        code.splice(self.loads)
        code.splice(self.compute)
        code.splice(self.stores)
```

为什么不边生成边写进一个 buffer？因为地址计算、CSE 和 buffer 复用需要把“读”“算”“写”分开：

- `loads` 里放所有输入读取，便于把索引表达式集中到循环体开头。
- `compute` 里放纯计算与常量，CSE 只在这一层做值复用。
- `stores` 里放写回，且写入的是 `DeferredLine`，等到 buffer 名与复用关系确定后再展开。

`CppKernel.load` 是标量路径的核心：

```python
def load(self, name: str, index: sympy.Expr):
    var = self.args.input(name)
    index = self.rename_indexing(index)
    line = f"{var}[{cexpr_index(index)}]"
    csevar = self.cse.generate(self.loads, line, dtype=V.graph.get_dtype(name))
    csevar.update_on_args("load", (self, name, index), {})
    return csevar
```

`self.args.input(name)` 返回实参名，例如 `in_ptr0`。`rename_indexing` 负责把符号改写成 C++ 可用的索引形式，`cexpr_index` 再生成字符串。`cse.generate` 把 `in_ptr0[x0]` 变成一个 `CppCSEVariable`，后续 `ops.relu` 拿到的是变量名而不是重复的数组访问。

`CppKernel.store` 处理三种情况：普通写、`atomic_add`、以及 `atomic_add` 在单线程下退化为 `+=`：

```python
if mode is None:
    line = f"{var}[{cexpr_index(index)}] = {value};"
elif mode == "atomic_add":
    if not self._use_parallel_atomic_add():
        line = f"{var}[{cexpr_index(index)}] += {value};"
    else:
        line = f"atomic_add(&{var}[{cexpr_index(index)}], {value});"
self.stores.writeline(DeferredLine(name, line))
```

`DeferredLine(name, line)` 里的 `name` 是 buffer 名。内存规划可能把 `buf2` 复用成 `buf1`，真正写文本时要把名字替换成最终实参；如果提前拼死字符串，复用信息就丢了。

归约不走 `stores`，而走 `reduction_prefix` 与 `reduction_suffix`。前缀负责声明累加器：

```cpp
float tmp_acc0 = 0;
at::vec::Vectorized<float> tmp_acc0_vec = at::vec::Vectorized<float>(0);
```

后缀负责把向量累加器归并成标量并写回：

```cpp
tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<float, 1>(...);
in_out_ptr0[static_cast<int64_t>(x0)] = static_cast<float>(tmp_acc0);
```

`CppKernel` 还保存 `parallel_reduction_prefix`、`local_reduction_init`、`local_reduction_stores`，这些是第 16 章并行归约的素材。

### 2. CppKernelProxy：决定“一个节点组要生成几个 kernel”

`CppKernelProxy` 是 `CppScheduling` 与具体 kernel 之间的协调者。它的 `__init__` 很轻：

```python
def __init__(self, kernel_group):
    super().__init__(kernel_group.args, kernel_group.ws.num_threads)
    self.kernel_group = kernel_group
    self.loop_nest = None
    self.call_ranges = None
    self.picked_vec_isa = cpu_vec_isa.pick_vec_isa()
    self.kernels: list[CppKernel] = []
```

真正的决策在 `codegen_functions`。它先取“归约维度最多”的那组 sizes 作为统一循环空间：

```python
group, reduction_group = max(var_sizes_list, key=lambda sizes: len(sizes[1]))
self.set_ranges(group, reduction_group)
```

然后先无条件生成一个标量 kernel：

```python
scalar_kernel = codegen_kernel(self.kernel_cls)
self.loop_nest = LoopNest.build(scalar_kernel)
```

这个标量 kernel 有两个作用。第一，它确定 `call_ranges`、`itervars`、`reduction_depth`，后续所有 kernel 共享同一套循环空间。第二，它是尾部处理的后备：如果某个循环尾部不能被向量化，就把这段范围交给它。

接下来如果 ISA 不可用或没有迭代变量，就直接返回标量 kernel：

```python
if not self.picked_vec_isa or not self.itervars:
    self.kernels = [scalar_kernel]
    self.aggregate_reduction_buffers(False, None)
    self.loop_nest.set_kernel(self)
    return
```

否则进入 `TilingSelect.select_tiling`，得到 `tiling_factors` 与 `tiling_indices`，再按一维或二维 tiling 生成主向量 kernel 与尾部 kernel：

```python
if len(tiling_indices) == 1:
    metrics.generated_cpp_vec_kernel_count += 1
    loop = self.loop_nest.tile(tiling_indices[0], factor=tiling_factors[0])
    vec_kernel = codegen_kernel(self.vec_kernel_cls, tiling_factors[0], tiling_indices[0])
    tail_size = loop.size - loop.tiled_size
    vec_kernel.active_ranges = {loop.var: (0, loop.tiled_size)}
    tail_kernel = scalar_kernel
    tail_kernel.active_ranges = {loop.var: (loop.tiled_size, loop.size)}
    self.kernels = [vec_kernel, tail_kernel]
```

`active_ranges` 是这里最关键的字段。主向量 kernel 只在 `[0, tiled_size)` 生效，尾部只在 `[tiled_size, size)` 生效。最终 `gen_body` 用 `C10_LIKELY`/`C10_UNLIKELY` 把两个 kernel 的 body 包进条件分支：

```python
def gen_body(self, code):
    if_prefix = "C10_LIKELY"
    for kernel in self.kernels:
        if kernel.codegen_conditions(code, if_prefix):
            if_prefix = "C10_UNLIKELY"
            code.indent()
            code.splice(kernel.gen_body())
```

`codegen_conditions` 把 `active_ranges` 变成：

```cpp
if(C10_LIKELY(x0 >= 0 && x0 < 4194304))
```

当尺寸能被向量宽度整除时，尾部的 `start == end`，`gen` 返回 `False`，条件分支不生成；这就是 4M 元素（能被 8 整除）的代码里只有一个 `C10_LIKELY` 分支的原因。

### 3. LoopNest 与 LoopLevel：循环文本的唯一出口

所有 `for(...)` 都来自 `LoopLevel.lines()`。它根据 `parallel`、`simd_vec`、`simd_omp`、`steps`、`offset` 拼出 pragma 与循环头：

```python
if self.parallel:
    line1 = "#pragma omp for"
    if self.parallel > 1:
        line1 += f" collapse({self.parallel})"
elif self.simd_vec:
    line1 = ""
elif not self.is_reduction and cpp_builder.is_gcc():
    line1 = "#pragma GCC ivdep"
offset_str = f"{INDEX_TYPE} {self.var}={cexpr_index(self.offset)}"
size_str = f"{self.var}<{cexpr_index(self.size)}"
steps_str = f"{self.var}+={cexpr_index(self.steps)}"
line2 = f"for({offset_str}; {size_str}; {steps_str})"
```

`LoopNest.tile(depth, factor)` 做的是：

```text
for (x0 = 0; x0 < size; x0++)
  -> for (x0 = 0; x0 < size; x0 += factor)
```

同时把 `tiled_size` 设为 `FloorDiv(size, factor) * factor`，供 `active_ranges` 切主循环与尾部。这就是 `x0 += 8` 的来源：`factor` 来自 `VecISA.nelements(float) = 8`。

`LoopNest.build(kernel)` 把 `kernel.itervars` 与 `kernel.ranges` 一一对应成 `LoopLevel`，并按照 `reduction_depth` 标记哪些 level 是归约。`max_parallel_depth()` 只统计开头连续同类型的循环；遇到第一个归约/非归约切换就停。这个规则直接决定了 OpenMP 能并行到哪一层。

`LoopLevel.lines()` 还处理 `no_redundant_loops`：如果偏移等于大小，直接返回 `None`，不生成空循环。对于 `size=0` 的边界情况，这个判断避免了生成无意义的循环头。

### 4. CppScheduling：节点组、模板与 kernel 的落地

`CppScheduling` 是调度器与代码生成器的接口。`codegen_node` 的骨架很短：

```python
def codegen_node(self, node):
    kernel_group = self.kernel_group
    if isinstance(node, OuterLoopFusedSchedulerNode):
        self.codegen_outer_loop_node(node)
    else:
        nodes = node.get_nodes()
        nodes = self.try_loop_split(nodes)
        cpp_kernel_proxy = self.kernel_proxy_cls(kernel_group)
        cpp_kernel_proxy.codegen_nodes(nodes)
        kernel_group.finalize_kernel(cpp_kernel_proxy, nodes)
    if self._get_scheduled_num_args() > CppScheduling.MAX_FUSED_KERNEL_ARGS_NUM:
        self._set_flush_status(True)
```

这里有三个值得注意的决定：

1. `try_loop_split` 会在融合后的循环过大时拆分节点，避免生成一个巨大的循环体。
2. `OuterLoopFusedSchedulerNode` 走另一条路径，由 `OuterLoopFusedKernel` 管理多个内层 loop nest。
3. `MAX_FUSED_KERNEL_ARGS_NUM = 500` 是 ctypes 参数上限的保护线；超过就强制 flush，把当前 kernel 截断，避免调用约定失败。

`finalize_kernel` 把 kernel 的循环代码写进 `KernelGroup.loops_code`：

```python
def finalize_kernel(self, new_kernel, nodes):
    self.scheduled_nodes += nodes
    new_kernel.codegen_loops(self.loops_code, self.ws)
```

`flush` 时，`KernelGroup.codegen_group()` 把所有循环代码、参数、prologue 拼成完整函数，再交给 `define_kernel`：

```python
def flush(self):
    src_code = self.kernel_group.codegen_group()
    if src_code:
        kernel_name = self.define_kernel(src_code, self.kernel_group.scheduled_nodes)
        self.codegen_comment(...)
        self.kernel_group.call_kernel(V.graph.wrapper_code, kernel_name)
    self.reset_kernel_group()
```

`define_kernel` 做三件事：去重、命名、包装成 async 编译单元。

```python
if src_code in wrapper.src_to_kernel:
    kernel_name = wrapper.src_to_kernel[src_code]
else:
    fused_name = get_fused_kernel_name(nodes, config.cpp.descriptive_names) if config.cpp.descriptive_names else ""
    kernel_name = "_".join(["cpp", fused_name, wrapper.next_kernel_suffix()])
    wrapper.src_to_kernel[src_code] = kernel_name
    kernel_decl_name = kernel_name if V.graph.cpp_wrapper else "kernel"
    src_code = src_code.replace(str(Placeholder.KERNEL_NAME), kernel_decl_name)
    src_code = src_code.replace(str(Placeholder.DESCRIPTIVE_NAME), kernel_name)
    compile_wrapper.writeline(f"async_compile.cpp_pybinding({arg_types!r}, r'''")
    compile_wrapper.splice(src_code, strip=True)
    wrapper.define_kernel(kernel_name, compile_wrapper.getvalue(), gpu=False, cpp_definition=kernel_definition)
```

这段代码解释了一个常见困惑：在 `output_code.py` 里搜索 `cpp_fused_div_relu_sum_0`，你会看到它是 Python 变量名；但搜索生成的 `.cpp` 文件，函数名是 `kernel`。原因是 `V.graph.cpp_wrapper` 为 `False` 时，源码里的 `Placeholder.KERNEL_NAME` 被替换成 `kernel`，真正的描述性名字留给 Python 绑定与 wrapper 调用。`src_to_kernel` 则保证同一份源码只编译一次，重复的 kernel 直接复用名字。

### 5. KernelGroup：参数、别名与函数外壳

`KernelGroup` 收集一次 flush 内的所有 kernel 共享的东西：

```python
class KernelGroup:
    def __init__(self):
        self.args = KernelArgs()
        self.loops_code = BracesBuffer()
        self.ws = WorkSharing(self.loops_code)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(self.ws)
        self.scheduled_nodes = []
```

`codegen_group` 生成的外壳包含：

```cpp
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C" __declspec(dllexport) void kernel(const float* in_ptr0, const float* in_ptr1, float* out_ptr0)
{
    std::atomic<int> inductor_cpu_integer_div_error{0};
    inductor_cpu_integer_div_error_flag = &inductor_cpu_integer_div_error;
    auto out_ptr0 = in_out_ptr0;
    #pragma omp parallel num_threads(4)
    { ... }
    inductor_cpu_integer_div_error_flag = nullptr;
    inductor_cpu_throw_if_integer_div_error(inductor_cpu_integer_div_error);
}
```

其中 `auto out_ptr0 = in_out_ptr0;` 来自 `self.args.aliases()`：当一块 buffer 被判定为原地更新时，wrapper 传入的是 `in_out_ptr0`，kernel 内部仍用 `out_ptr0` 这个名字，别名语句把两者接上。整数除零检查是固定 prologue，每个 kernel 都带，不是业务逻辑。

`KernelGroup.call_kernel` 最终生成 wrapper 侧调用：

```python
_, call_args, arg_types = self.args.cpp_argdefs()
wrapper.generate_kernel_call(kernel_name, call_args, triton=False, arg_types=arg_types)
```

在 `output_code.py` 里看到的就是：

```python
cpp_fused_div_relu_sum_0(buf2, buf0)
```

参数顺序由 `KernelArgs` 维护，`in_out` 参数排在前面，输入其次，输出最后；这也是为什么归约 kernel 的签名是 `(float* in_out_ptr0, const float* in_ptr0)`。

### 6. 案例 A：lab_a 的归约 kernel 逐行读

lab_a 的生成产物（节选，真实输出）：

```cpp
extern "C" __declspec(dllexport) void  kernel(float* in_out_ptr0,
                       const float* in_ptr0)
{
    std::atomic<int> inductor_cpu_integer_div_error{0};
    inductor_cpu_integer_div_error_flag = &inductor_cpu_integer_div_error;
    auto out_ptr0 = in_out_ptr0;
    #pragma omp parallel num_threads(4)
    {
        int tid = omp_get_thread_num();
        {
            #pragma omp for
            for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(64LL); x0+=static_cast<int64_t>(1LL))
            {
                {
                    float tmp_acc0 = 0;
                    at::vec::Vectorized<float> tmp_acc0_vec = at::vec::Vectorized<float>(0);
                    for(int64_t x1=static_cast<int64_t>(0LL); x1<static_cast<int64_t>(64LL); x1+=static_cast<int64_t>(8LL))
                    {
                        {
                            if(C10_LIKELY(x1 >= static_cast<int64_t>(0) && x1 < static_cast<int64_t>(64LL)))
                            {
                                auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x1 + 64LL*x0), static_cast<int64_t>(8));
                                auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
                                tmp_acc0_vec = tmp_acc0_vec + tmp1;
                            }
                        }
                    }
                    tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<float, 1>([](at::vec::Vectorized<float>& x, at::vec::Vectorized<float>& y) { return x + y; }, tmp_acc0_vec);
                    in_out_ptr0[static_cast<int64_t>(x0)] = static_cast<float>(tmp_acc0);
                }
            }
        }
        #pragma omp single
        {
            {
                for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(64LL); x0+=static_cast<int64_t>(8LL))
                {
                    {
                        if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(64LL)))
                        {
                            auto tmp0 = at::vec::Vectorized<float>::loadu(out_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                            auto tmp1 = static_cast<float>(64.0);
                            auto tmp2 = at::vec::Vectorized<float>(tmp1);
                            auto tmp3 = tmp0 / tmp2;
                            tmp3.store(in_out_ptr0 + static_cast<int64_t>(x0));
                        }
                    }
                }
            }
        }
    }
    inductor_cpu_integer_div_error_flag = nullptr;
    inductor_cpu_throw_if_integer_div_error(inductor_cpu_integer_div_error);
}
```

逐段对应到 codegen：

| 代码 | 来源 |
|---|---|
| `#pragma omp parallel num_threads(4)` | `WorkSharing.parallel(threads)`，`threads=4` |
| `int tid = omp_get_thread_num();` | `WorkSharing.parallel` 固定写入 |
| `#pragma omp for` | `LoopLevel.parallel` |
| `x0 += 1` | 外层迭代循环，未被 tile |
| `x1 += 8` | `LoopNest.tile` 后的归约循环，`factor=8` |
| `tmp_acc0` / `tmp_acc0_vec` | `CppKernel` 的 reduction prefix |
| `C10_LIKELY(x1 >= 0 && x1 < 64)` | `active_ranges` 生成的条件 |
| `#pragma omp single` | 归约后的逐元素后缀，由 `codegen_loops_impl` 放置 |
| `tmp3.store(in_out_ptr0 + x0)` | 后缀里的 `CppVecKernel.store` |

注意 `#pragma omp single` 里的循环仍然走向量化（`x0 += 8`），因为它属于同一个 `CppKernelProxy` 的后缀 kernel，而不是第二个 kernel。用 `extern "C"` 的数量统计 kernel 数时，这里只算一个。

### 7. 案例 B：lab_c 的纯逐元素 kernel

lab_c 注册 lowering 后，`y = relu(x * 2 + 1)` 被压进一个 256×256 的向量循环。真实输出（节选）：

```cpp
for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(65536LL); x0+=static_cast<int64_t>(8LL))
{
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
    auto tmp1 = static_cast<float>(2.0);
    auto tmp2 = at::vec::Vectorized<float>(tmp1);
    auto tmp3 = tmp0 * tmp2;
    auto tmp4 = static_cast<float>(1.0);
    auto tmp5 = at::vec::Vectorized<float>(tmp4);
    auto tmp6 = tmp3 + tmp5;
    auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0));
    tmp7.store(out_ptr0 + static_cast<int64_t>(x0));
}
```

这份代码里没有归约、没有 `reduction_prefix`，`kernels` 只有一个 `CppVecKernel`（如果 65536 不能被 8 整除，还会有一个尾部 `CppKernel`）。`tmp_ops=8` 正好对应 `load`、两个常量、两次向量构造、乘法、加法、`clamp_min` 这 8 条 `auto tmpN =`。

`static_cast<float>(2.0)` 与 `at::vec::Vectorized<float>(tmp1)` 是 `CppVecOverrides` 把标量常量提升为向量的证据。标量路径会直接写 `float(tmp1 * tmp2)`，不会构造 `Vectorized`；两条路径的差异在第 15 章展开。

### 8. 命名：`cpp_fused_div_relu_sum_0` 从哪来

名字由三部分拼成：

```text
cpp + "_" + fused_name + "_" + suffix
```

- `cpp` 是后端前缀。
- `fused_name` 来自 `get_fused_kernel_name(nodes, config.cpp.descriptive_names)`。默认 `descriptive_names="original_aten"`，按节点里原始 ATen 名去重后排序，lab_a 的 `relu`、`sum`、`div` 得到 `fused_div_relu_sum`。
- `suffix` 来自 `wrapper.next_kernel_suffix()`，同一个图里从 0 递增。

所以最终是 `cpp_fused_div_relu_sum_0`。它不是 C++ 函数名，而是 wrapper 里的 Python 变量名与调用名；C++ 函数名在非 `cpp_wrapper` 模式下被替换成 `kernel`。如果你把 `descriptive_names` 改成别的值，名字会变；写测试时不要把它当稳定契约，应该断言“存在一个 cpp kernel”或“调用名以 cpp_ 开头”。

## 可跑实验

### 实验 1：读 lab_a 的 kernel 与命名

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

真实输出（节选）：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)
...
  vectorized   True
  step         1
  threads      4
  tmp_ops      6
  cpp_lines    108
...
  extern kernels: ['addmm']
[PASS] 存在生成的 C++ kernel  (kernel)
[PASS] matmul 走 extern kernel  (['addmm'])
[PASS] kernel 被向量化  (step=1)
```

`name` 显示 `kernel` 是因为 `kernel_stats` 从 `extern "C"` 后面抓函数名；`step=1` 是外层归约循环；`tmp_ops=6` 是后缀逐元素部分的临时变量数。要看命名，直接看 wrapper：

```powershell
$d = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
     Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -Path (Join-Path $d.FullName 'output_code.py') -Pattern 'cpp_fused|cpp_fused_div_relu_sum_0'
```

预期：

```text
cpp_fused_div_relu_sum_0(buf2, buf0)
```

### 实验 2：读 lab_c 的纯逐元素 kernel

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py
```

真实输出（节选）：

```text
2) 注册 lowering：算子展开成 IR，与 relu 同 kernel
  for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(65536LL); x0+=static_cast<int64_t>(8LL))
  auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
  auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0));

[PASS] lowering 后只有一个 kernel  (1)
[PASS] affine 的乘法/加法进入 kernel  (tmp_ops=8)
```

### 实验 3：直接翻 `.main.cpp`

```powershell
$main = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Filter *.main.cpp |
        Sort-Object LastWriteTime | Select-Object -Last 1
$main.FullName
Select-String -Path $main.FullName -Pattern 'extern "C"|omp|loadu|single|x0\+=' | Select-Object -First 20
```

预期能看到 `extern "C" __declspec(dllexport) void kernel(...)`、`#pragma omp parallel num_threads(4)`、`#pragma omp for`、`loadu`，归约 kernel 还会多一个 `#pragma omp single`。把这份 `.cpp` 与 `output_code.py` 里 `cpp_fused_... = async_compile.cpp_pybinding(...)` 后面的源码对照，两者内容是同一份，只是函数名与包装层不同。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/codegen/cpp.py` | `CppKernel` | `__init__` 的各类 buffer、`gen_body`、`load`、`store`、`gen_reduction_prefix` |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel` | `load` 的 stride 分支、`_get_vec_load_line`、`_get_store_line`、`store` 的 `atomic_add` |
| `torch/_inductor/codegen/cpp.py` | `CppKernelProxy` | `__init__`、`codegen_nodes`、`codegen_functions`、`gen_body`、`aggregate_reduction_buffers` |
| `torch/_inductor/codegen/cpp.py` | `TilingSelect` | `select_tiling`、`_select_tiling_indices` |
| `torch/_inductor/codegen/cpp.py` | `CppScheduling` | `codegen_node`、`try_loop_split`、`codegen_template`、`define_kernel`、`flush` |
| `torch/_inductor/codegen/cpp.py` | `KernelGroup` | `new_kernel`、`finalize_kernel`、`codegen_group`、`call_kernel` |
| `torch/_inductor/codegen/cpp.py` | `LoopNest` / `LoopLevel` | `build`、`max_parallel_depth`、`mark_parallel`、`tile`、`lines` |
| `torch/_inductor/codegen/common.py` | `Kernel.codegen_loops_impl` | 并行区域、归约 prefix/suffix、`omp single` 的放置 |
| `torch/_inductor/codegen/wrapper.py` | `WrapperCodeGen.define_kernel` / `generate_kernel_call` | kernel 名如何进入 Python 包装与调用 |
| `torch/_inductor/codegen/cpp.py` | `CppOverrides` / `CppVecOverrides` | 逐元素文本映射 |

阅读顺序建议：`CppScheduling.codegen_node` → `CppKernelProxy.codegen_functions` → `CppKernel.gen_body` → `KernelGroup.codegen_group` → `define_kernel`。先看“谁调用谁”，再看每个函数内部，代码会好读很多。

## 常见坑

1. **在 `.cpp` 里搜 `cpp_fused_div_relu_sum_0`。** 生成文件里的函数名是 `kernel`，描述性名字只存在于 `output_code.py` 的 Python 绑定与 wrapper 调用。搜不到不代表命名失败，搜 `output_code.py` 才能验证。

2. **把 `kernel_stats` 的 `step` 当向量化开关。** 归约 kernel 的第一个 `x0+=` 是外层循环，步长 1；真正的向量步长在内层 `x1+=8`。判断向量化看 `Vectorized<float>` 或 `loadu`，不要只看 `step`。

3. **只数主向量分支的 `tmp_ops`。** 有尾部时，`CppKernelProxy` 会生成主向量 kernel 与尾部 kernel，`gen_body` 用 `C10_LIKELY`/`C10_UNLIKELY` 分支包起来。`extract_kernel` 截到的是整个函数，但 `kernel_stats` 的 `tmp_ops` 统计的是整个函数体，尾部分支的临时变量也会计入；解读时要结合 `active_ranges`。

4. **把 `#pragma omp single` 当第二个 kernel。** 归约后的逐元素后缀在同一个 `extern "C"` 函数里，只是放在 `single` 区域串行执行。用 `extern "C"` 数量或 `metrics.generated_kernel_count` 判断 kernel 数，不要用 `for` 循环数量。

5. **把 kernel 名写进测试断言。** `descriptive_names` 的取值、节点顺序、ATen 名去重都会影响 `fused_name`；上游版本调整命名规则时，硬编码名字的测试会碎。断言“存在 cpp kernel”“调用参数正确”更稳。

6. **看到旧代码却以为新代码没生效。** 同一进程内 config 变体可能复用缓存，`output_code.py` 可能是上一次的。做结构对比时用 `fresh_inductor_cache()` 或 `--variant` 子进程隔离；lab_d 就是这么做的。

7. **忽略 `MAX_FUSED_KERNEL_ARGS_NUM`。** 融合太多节点会让实参超过 500，`CppScheduling` 会强制 flush，把一个大 kernel 拆成两个。看到 kernel 数比预期多时，先数参数，不要先怀疑融合逻辑。

8. **以为 `C10_LIKELY` 一定存在。** 当尺寸能被向量宽度整除时，尾部 `start == end`，`codegen_conditions` 不生成分支；整型或小循环也可能没有向量 kernel。条件分支是优化，不是契约。

## 自我检查

- [ ] 我能说出 `CppKernel.gen_body` 为什么按 `loads`、`compute`、`stores` 顺序拼装。
- [ ] 我能解释 `DeferredLine(name, line)` 里的 `name` 为什么不能提前替换。
- [ ] 我能画出 `CppKernelProxy.codegen_functions` 生成标量 kernel、主向量 kernel、尾部 kernel 的顺序。
- [ ] 我能说明 `active_ranges` 如何变成 `C10_LIKELY` / `C10_UNLIKELY` 分支。
- [ ] 我能从 `x0 += 8` 反推出 `tiling_factor` 与 `VecISA.nelements`。
- [ ] 我能解释 `define_kernel` 里 `src_to_kernel` 的去重作用。
- [ ] 我能说出 `cpp_fused_div_relu_sum_0` 的三段组成，以及它为什么不是 C++ 函数名。
- [ ] 我能区分 `extern "C"` 数量、`metrics.generated_kernel_count` 与 `generated_cpp_vec_kernel_count` 的含义。

## 延伸阅读

- **`torch/_inductor/codegen/cpp.py` 的 `CppKernelProxy.codegen_functions`**：读哪里——从 `scalar_kernel = codegen_kernel(...)` 到 `self.kernels = [vec_kernel, tail_kernel]` 的完整分支；学什么——一个节点组如何变成多个 kernel，尾部如何处理；关联——本章第 2 节与 lab_a/lab_c 的产物。
- **`torch/_inductor/codegen/cpp.py` 的 `CppScheduling.define_kernel`**：读哪里——`src_to_kernel`、`get_fused_kernel_name`、`Placeholder.KERNEL_NAME` 替换、`cpp_pybinding` 包装；学什么——kernel 名与 C++ 函数名的分离；关联——`cpp_fused_div_relu_sum_0`。
- **`torch/_inductor/codegen/cpp.py` 的 `KernelGroup.codegen_group`**：读哪里——header、函数签名、`args.aliases()`、整数除零 prologue；学什么——函数外壳的固定成分；关联——`.main.cpp` 的前 10 行。
- **`torch/_inductor/codegen/cpp.py` 的 `LoopLevel.lines`**：读哪里——`parallel`、`simd_vec`、`steps`、`no_redundant_loops` 四个分支；学什么——循环文本的唯一出口；关联——`x0 += 1` 与 `x1 += 8` 的差异。
- **`torch/_inductor/codegen/common.py` 的 `Kernel.codegen_loops_impl`**：读哪里——`worksharing.parallel`、`mark_parallel`、reduction prefix/suffix、`gen_loop_with_reduction`；学什么——`omp for` 与 `omp single` 的放置；关联——第 16 章。
- **`compilers/inductor_deep_dive/11-reading-scheduler-ir.md`**：读哪里——如何从调度 IR 的 `group.iteration` 和 `loop_body` 推 kernel 结构；学什么——生成代码之前的形状；关联——本章逐行对照的上游。

[上一章：Ch 13 LoopBody 与 ops_handler](./13-loop-body-and-ops-handler.md) | [下一章：Ch 15 向量化](./15-vectorization.md)