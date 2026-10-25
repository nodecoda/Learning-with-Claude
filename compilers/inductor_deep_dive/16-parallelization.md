# Ch 16 并行化：OpenMP pragma、并行深度与归约

> **目标**：搞清 `#pragma omp parallel num_threads(4)`、`#pragma omp for`、`#pragma omp single` 分别由谁生成，为什么 `torch.set_num_threads(1)` 后仍然向量化却没有 pragma，以及并行归约为什么要用 `tmp_acc_arr[tid]` 和固定顺序的合并循环。读完后你能从生成代码反推 `parallel_depth`，并判断一个归约的结果为什么不能与 eager 逐位相等。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe` 14.43）。本机默认 4 线程。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

本章使用 [`code/labs/lab_a_pipeline.py`](./code/labs/lab_a_pipeline.py) 与 [`code/labs/lab_d_perf.py`](./code/labs/lab_d_perf.py)。前一章是 [Ch 15 向量化](./15-vectorization.md)，下一章是 [Ch 17 extern kernel 与模板](./17-extern-kernels-and-templates.md)。

## 为什么

向量化让一条指令处理 8 个 float，并行化让 4 个核心同时跑。两者经常被混为一谈，但在 Inductor 里它们是两条独立的决策链：

- 向量化由 `TilingSelect` + `CppVecKernel` 决定，作用在最内层循环。
- 并行化由 `LoopNest.max_parallel_depth` + `decide_parallel_depth` + `WorkSharing` 决定，作用在某一个外层循环上。

这条分离在生成代码里非常直观。`threads=1` 时没有 `#pragma omp parallel`，但循环仍然是 `x0 += 8`、仍然调用 `loadu`；`vec_isa_ok=False` 时循环变成 `x0 += 1`，但 `#pragma omp parallel num_threads(4)` 还在。两条轴互不替代。

并行化真正难的地方不是 pragma 文本，而是三个正确性问题：

1. **并行到哪一层？** 归约循环不能随便并行，否则每个线程都在写同一个累加器。
2. **归约结果怎么合并？** 每个线程的局部结果要按固定顺序合并，否则同一份输入两次运行结果不同。
3. **哪些写操作必须原子？** 散射写、`atomic_add`、多个 buffer 之间的依赖，都会影响能不能并行。

本章先把线程数与 pragma 的来源讲清楚，再看 `parallel_depth` 的启发式，最后读归约的真实生成代码。

## 主线

### 1. 线程数从哪来：`parallel_num_threads` 与 `WorkSharing`

`WorkSharing` 是 `KernelGroup` 里的一个对象，负责在 `loops_code` 上开合并行区域：

```python
class WorkSharing:
    def __init__(self, code):
        self.code = code
        self.in_parallel = False
        self.num_threads = None
        self.stack = contextlib.ExitStack()

    def parallel(self, threads):
        if self.in_parallel and threads != self.num_threads:
            self.close()
        if not self.in_parallel:
            self.num_threads = threads
            self.in_parallel = True
            use_dynamic = False
            if config.cpp.threads >= 1:
                use_dynamic = False
            elif threads == os.cpu_count():
                use_dynamic = True
            else:
                use_dynamic = False
            if use_dynamic or config.cpp.dynamic_threads:
                self.code.writeline("#pragma omp parallel")
            else:
                self.code.writeline(f"#pragma omp parallel num_threads({threads})")
            self.stack.enter_context(self.code.indent())
            self.code.writeline("int tid = omp_get_thread_num();")
```

几个细节：

- `threads` 来自 `parallel_num_threads()`：`config.cpp.threads` 小于 1 时取 `torch.get_num_threads()`。本机默认是 4。
- `use_dynamic` 只在“线程数等于 `os.cpu_count()` 且用户没有显式设 `cpp.threads`”时为真，此时生成 `#pragma omp parallel`，让 OpenMP 运行时决定线程数。
- 本机默认 `config.cpp.threads=-1`，`threads=4`，而 `os.cpu_count()` 也是 4，按上面的逻辑会走 `use_dynamic=True`；但真实生成代码是 `#pragma omp parallel num_threads(4)`。原因是本机 `os.cpu_count()` 返回 8，而 `torch.get_num_threads()` 返回 4；`threads == os.cpu_count()` 为假，于是走固定 `num_threads(4)`。这也说明 pragma 文本是环境相关的，不要把它当跨机器常量。
- 同一个 `WorkSharing` 会被 `KernelGroup` 中多个 kernel 共享。如果下一个 kernel 的线程数不同，`parallel()` 会先 `close()` 再重开；线程数相同则复用同一个并行区域，避免嵌套 `omp parallel`。

`KernelGroup.__init__` 里 `self.ws = WorkSharing(self.loops_code)`，并把 `ws` 放进 `ExitStack`。这意味着一个 kernel group 的所有循环代码都在同一个 `#pragma omp parallel` 下，除非显式关闭。`call_kernel` 只负责生成 wrapper 调用，不参与线程决策。

### 2. `parallel_depth`：并行到哪一层

`LoopNest.max_parallel_depth` 先找“开头连续同类型”的循环：

```python
start_depth = 0
max_depth = 0
is_reduction = self.loops[0].is_reduction
num_steps = sympy.Integer(1)
for loop in self.loops:
    if loop.is_reduction != is_reduction:
        break
    num_steps = num_steps * FloorDiv(loop.size, loop.steps)
    max_depth += 1
```

如果第一个循环是迭代维，那么所有连续的迭代维都算可并行候选；一旦遇到归约维就停。反过来，如果第一个循环就是归约维，那么连续的归约维算候选。这个规则避免了“并行到一半突然变成归约”的非法嵌套。

接下来有一个“起点提升”启发式：如果第一段可并行的循环步数远小于后面某一层，就把并行起点移到那一层。

```python
if (max_depth < len(self.loops)
        and isinstance(num_steps, sympy.Integer)
        and isinstance(self.loops[max_depth].size, sympy.Integer)
        and num_steps * 300 < FloorDiv(self.loops[max_depth].size, self.loops[max_depth].steps)
        and not (... 归约在向量层之下 ...)):
    start_depth = max_depth
    max_depth = 0
    ...
```

用直观的话说：如果外层只有 2 步，内层有 100 万步，把 2 步分给 4 个线程没有意义，不如从内层开始并行。`300` 是源码里的经验系数，不是可配置项。这个启发式解释了为什么有些 kernel 的 `#pragma omp for` 出现在第二层而不是第一层。

`max_parallel_depth` 返回 `ParallelDepth(parallel_depth, start_depth)`。`decide_parallel_depth` 再根据实际工作量和线程数决定深度：

```python
ranges = self.call_ranges[start_depth : start_depth + parallel_depth]
seq = self.size_hint()
par = 1
depth = 0
for expr in ranges:
    hint = sizevars.optimization_hint(expr, fallback=8192)
    if par >= 2 * threads or par == threads:
        break
    if seq // threads < config.cpp.min_chunk_size:
        break
    depth += 1
    par *= hint
    seq /= hint
```

三个停止条件：

- `par >= 2 * threads` 或 `par == threads`：已经有足够多的并行任务，继续加深度只会增加调度开销。
- `seq // threads < config.cpp.min_chunk_size`：总工作量分给每个线程后不足 `min_chunk_size=512`，并行不划算。
- `dynamic_threads` 且深度为 0 时，强制至少 1 层并行，让 OpenMP 运行时自己决定串行还是并行。

`mark_parallel` 把选中的层标记为并行，并把中间层标记为 `collapsed`：

```python
loop = self.loops[par_depth.start_depth]
loop.parallel = par_depth.parallel_depth
if loop.is_reduction:
    metrics.parallel_reduction_count += 1
for i in range(par_depth.start_depth + 1, par_depth.parallel_depth):
    self.loops[i].collapsed = True
```

`LoopLevel.lines()` 看到 `parallel` 就生成 `#pragma omp for`；`parallel > 1` 时加 `collapse(N)`。`collapsed` 的层不再单独生成 `for` 头，而是被 `collapse` 合并。最终代码里可能只看到一个 `#pragma omp for collapse(2)` 和两个 `for`，而不是两个 pragma。

### 3. 归约并行：先关并行区域，再开局部累加

归约不能像迭代循环那样直接 `#pragma omp for`，因为多个线程写同一个 `tmp_acc` 会产生数据竞争。`Kernel.codegen_loops_impl` 的处理顺序是：

```python
if par_depth.parallel_depth:
    if is_reduction_loop:
        worksharing.close()      # 先在并行区域外声明累加器
    else:
        worksharing.parallel(threads)
    loop_nest.mark_parallel(par_depth)
elif threads > 1:
    if worksharing.single():
        stack.enter_context(code.indent())
```

如果是归约循环，先关闭当前并行区域，让 reduction prefix（`float tmp_acc0 = 0;`）落在并行区域外；然后在 `gen_loop_with_reduction` 里再开并行：

```python
if is_reduction_loop and loop.parallel:
    worksharing.parallel(threads)
    if kernel.local_reduction_init:
        code.splice(kernel.local_reduction_init)
gen_loop_at(_loop_nest, depth)
if is_reduction_loop and loop.parallel:
    if kernel.local_reduction_stores:
        code.splice(kernel.local_reduction_stores)
    worksharing.close()
```

`CppKernel._gen_parallel_reduction_buffers` 生成局部累加器与线程数组：

```python
acc_local = f"{acc}_local"
acc_local_in_array = f"{acc}_arr[tid]"
self.local_reduction_init.writeline(f"{acc_type} {acc_local} = {reduction_init_fn(...)};")
self.parallel_reduction_prefix.splice(reduction_prefix_array(acc, acc_type, ...))
self.local_reduction_stores.writeline(f"{acc_local_in_array} = {acc_local};")
self.parallel_reduction_suffix.writelines([
    f"for (int tid = 0; tid < {num_threads}; tid++)",
    "{",
    f"    {acc} = {reduction_combine_fn(reduction_type, acc, acc_local_in_array, ...)};",
    "}",
])
```

生成的代码骨架是：

```cpp
float tmp_acc0 = 0;
float tmp_acc0_arr[4];
#pragma omp parallel num_threads(4)
{
    int tid = omp_get_thread_num();
    float tmp_acc0_local = 0;
    #pragma omp for
    for (...) {
        tmp_acc0_local += ...;
    }
    tmp_acc0_arr[tid] = tmp_acc0_local;
}
for (int tid = 0; tid < 4; tid++) {
    tmp_acc0 = tmp_acc0 + tmp_acc0_arr[tid];
}
```

这里有两个关键设计：

1. 每个线程只写自己的 `tmp_acc0_local`，最后写到 `tmp_acc0_arr[tid]`，没有竞争。
2. 合并循环按 `tid` 从 0 到 3 固定顺序执行，保证同一份输入的结果可复现。相比 `atomic_add` 的任意顺序，这种做法牺牲一点并行度，换取确定性。

`update_stores_with_parallel_reduction` 会在并行归约时把 `stores` 里的 `tmp_acc0` 替换成 `tmp_acc0_local`，避免线程直接写全局累加器。`_use_parallel_atomic_add` 则决定 `atomic_add` 模式是否真的用原子操作：

```python
def _use_parallel_atomic_add(self):
    return config.cpp.dynamic_threads or self.num_threads != 1
```

`threads=1` 时，`atomic_add` 退化成 `+=`，因为不存在竞争；多线程时才生成 `atomic_add(&buf[i], value)`。

### 4. 迭代并行 + 内层归约：lab_a 的真实形态

lab_a 的 `sum(dim=1)` 是“外层 64 行，内层 64 列”的归约。`TilingSelect` 把内层 `x1` 向量化，`decide_parallel_depth` 选择外层 `x0` 并行。真实生成代码（节选）：

```cpp
#pragma omp parallel num_threads(4)
{
    int tid = omp_get_thread_num();
    {
        #pragma omp for
        for(int64_t x0=...; x0<static_cast<int64_t>(64LL); x0+=static_cast<int64_t>(1LL))
        {
            float tmp_acc0 = 0;
            at::vec::Vectorized<float> tmp_acc0_vec = at::vec::Vectorized<float>(0);
            for(int64_t x1=...; x1<static_cast<int64_t>(64LL); x1+=static_cast<int64_t>(8LL))
            {
                auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x1 + 64LL*x0), static_cast<int64_t>(8));
                auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
                tmp_acc0_vec = tmp_acc0_vec + tmp1;
            }
            tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<float, 1>(...);
            in_out_ptr0[static_cast<int64_t>(x0)] = static_cast<float>(tmp_acc0);
        }
    }
    #pragma omp single
    {
        for(int64_t x0=...; x0<static_cast<int64_t>(64LL); x0+=static_cast<int64_t>(8LL))
        {
            auto tmp0 = at::vec::Vectorized<float>::loadu(out_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
            auto tmp1 = static_cast<float>(64.0);
            auto tmp2 = at::vec::Vectorized<float>(tmp1);
            auto tmp3 = tmp0 / tmp2;
            tmp3.store(in_out_ptr0 + static_cast<int64_t>(x0));
        }
    }
}
```

这份代码里没有 `tmp_acc0_arr[tid]`。原因是归约维 `x1` 完全在单个线程内完成：每个线程拿一行，自己用向量累加器 `tmp_acc0_vec` 加完 64 个元素，`vec_reduce_all` 得到该行的标量，再写到 `in_out_ptr0[x0]`。线程之间没有共享累加器，所以不需要线程数组。

`#pragma omp single` 里的除法是融合进来的后缀节点 `op2`。它必须在所有行的归约完成之后执行，所以放在 `single` 区域；`single` 由 `WorkSharing.single()` 生成，不是第二个 kernel。

这个例子说明：**并行归约的形态取决于归约维是否跨线程**。外层并行 + 内层归约时，累加器是线程私有的；归约维本身被并行时，才需要 `tmp_acc_arr[tid]` 与合并循环。

### 5. 前缀/后缀与 `single` 的放置

`Kernel.codegen_loops_impl` 用 `gen_loop_with_reduction` 递归生成循环。它在进入归约层之前插入 prefix，在离开归约层之后插入 suffix：

```python
if loop.is_reduction and not in_reduction:
    reduction_prefix = get_reduction_prefix_suffix(kernel, loop.parallel, is_suffix=False)
    code.splice(reduction_prefix)
gen_loop_at(_loop_nest, depth)
if loop.is_reduction and not in_reduction:
    code.splice(get_reduction_prefix_suffix(kernel, loop.parallel, is_suffix=True))
```

`get_reduction_prefix_suffix` 根据 `parallel` 选择 `parallel_reduction_prefix` 或 `non_parallel_reduction_prefix`。并行归约时，prefix 还要加上 `tmp_acc0_arr` 的声明；非并行时只声明 `tmp_acc0`。

当 `par_depth.parallel_depth == 0` 但 `threads > 1` 时，代码会尝试 `worksharing.single()`：

```python
elif threads > 1:
    if worksharing.single():
        stack.enter_context(code.indent())
```

这处理的是“外层没有可并行循环，但当前已经在一个并行区域里”的情况：用 `#pragma omp single` 让一个线程执行剩余代码，其余线程等待。它不会新开并行区域。

`OuterLoopFusedKernel` 是另一个分支：它管理多个内层 loop nest，`decide_parallel_depth` 取所有内层 kernel 的并行深度的最大值，再与外部允许的最大深度取较小值：

```python
return ParallelDepth(
    parallel_depth=min(max_parallel_depth.parallel_depth, max(kernels_parallel_depth)),
    start_depth=max_parallel_depth.start_depth,
)
```

这样外层融合后的循环不会因为某个内层 kernel 想并行得更深而破坏其他内层的正确性。

### 6. `threads=1`：没有 pragma，但仍然向量化

`torch.set_num_threads(1)` 后，`parallel_num_threads()` 返回 1。`decide_parallel_depth` 里 `seq // threads < min_chunk_size` 可能仍然为假，`par >= 2 * threads` 也可能不满足，但 `codegen_loops_impl` 在 `threads=1` 时不会走 `worksharing.parallel(threads)` 的 `elif threads > 1` 分支，因此没有 `#pragma omp parallel`。

真实生成代码（`perf\threads1`，节选）：

```cpp
{
    for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(4194304LL); x0+=static_cast<int64_t>(8LL))
    {
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
        auto tmp1 = tmp0.sin();
        ...
        tmp7.store(out_ptr0 + static_cast<int64_t>(x0));
    }
}
```

注意 `x0 += 8` 与 `loadu` 仍在，只是外层少了 `omp parallel`。这说明并行化与向量化是正交的：线程数影响“几个核”，tiling 影响“每个核一次处理几个元素”。

### 7. 确定性与误差：为什么不能要求逐位相等

并行归约会改变浮点加法的顺序。即使合并循环按 `tid` 固定顺序执行，每个线程内部的累加顺序也与 eager 的单线程顺序不同；向量化又会把 8 个 lane 的累加再横向合并。结果是一个合法的 fp32 近似，不是 eager 的逐位复制。

本机的两个真实数字：

- lab_a（64×64，`sum` 后除以 64）：`max_abs_err=4.768e-07`。
- final_project（512×512，`sum(|z|)`）：`max_abs_err=1.95e-03`。

误差随归约长度增长，这是 fp32 的正常现象。正确性测试应该用相对/绝对容差，而不是 `torch.equal`。如果业务要求确定性，可以固定线程数、固定归约顺序，或者使用更高精度累加；Inductor 的 `need_use_acc_helper` 对长 `sum` 会启用辅助累加来改善精度，代价是额外的指令。

`atomic_add` 模式更要注意：原子加的顺序由运行时决定，同一份输入多次运行可能得到不同低位。它适合散射写，不适合要求确定性的归约。

## 可跑实验

### 实验 1：`threads=1` 的结构变化

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

结构表（真实结构，时间列随机器变化）：

```text
variant      vectorized  step  omp threads  compile
vec          yes         8     4            <本次编译耗时>
scalar       no          1     4            <本次编译耗时>
threads1     yes         8     none         <本次编译耗时>
vec_again    yes         8     4            <本次编译耗时>
```

对应断言（真实输出）：

```text
[PASS] vec 走 8 元素向量  (step=8)
[PASS] scalar 退化为 1 元素步长  (step=1)
[PASS] threads1 不再生成 omp pragma  (None)
```

`threads1` 行同时满足 `vectorized=yes` 与 `omp threads=none`，这是本章最重要的一条证据：向量化没有被线程数关掉。

### 实验 2：读归约 kernel 的 `omp for` 与 `omp single`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

然后：

```powershell
$main = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Filter *.main.cpp |
        Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -Path $main.FullName -Pattern '#pragma omp|for\(int64_t|vec_reduce_all|omp_get_thread_num' | Select-Object -First 12
```

预期（真实产物）：

```text
#pragma omp parallel num_threads(4)
int tid = omp_get_thread_num();
#pragma omp for
for(int64_t x0=...; x0<...64LL...; x0+=...1LL)
for(int64_t x1=...; x1<...64LL...; x1+=...8LL)
at::vec::vec_reduce_all<float, 1>(...)
#pragma omp single
for(int64_t x0=...; x0<...64LL...; x0+=...8LL)
```

这里能看到三件事：外层迭代并行、内层归约向量化、后缀在 `single` 区域。它同时也说明 `#pragma omp for` 后面跟的不一定是向量循环；本例里 `omp for` 绑定的是 `x0 += 1`。

### 实验 3：观察线程数对结构的影响

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

在脚本里把 `torch.set_num_threads(1)` 放到 `torch.compile` 之前，再运行一次。预期变化：

- `#pragma omp parallel` 与 `#pragma omp for` 消失。
- `x1 += 8`、`loadu`、`vec_reduce_all` 保留。
- `kernel_stats` 的 `threads` 变成 `None`。

不要在同一进程里“先编译 4 线程，再改 1 线程”做对比；已生成的 kernel 不会因为 `set_num_threads` 重写。用子进程或新的 cache 目录。

### 实验 4：归约误差的量级

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py
```

脚本会在 `code/.cache/final_report.md` 里写 baseline 与 optimized 的 `max abs err`。真实结果里优化版的误差是 `1.95e-03` 量级（fp32 归约顺序差异），不是代码错误。把线程数改成 1 再跑一次，误差量级可能变化，但通常仍不为 0；这正好说明误差来自归约顺序，而不是并行本身。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/utils.py` | `parallel_num_threads` | `config.cpp.threads` 与 `torch.get_num_threads()` 的优先级 |
| `torch/_inductor/codegen/cpp.py` | `WorkSharing` | `parallel`、`single`、`close`、`use_dynamic` 判断 |
| `torch/_inductor/codegen/cpp.py` | `KernelGroup` | `ws` 的创建、`finalize_kernel` 如何共享并行区域 |
| `torch/_inductor/codegen/cpp.py` | `LoopNest.max_parallel_depth` | 连续同类型循环、`start_depth` 提升、`300` 系数 |
| `torch/_inductor/codegen/cpp.py` | `CppKernelProxy.decide_parallel_depth` | `min_chunk_size`、`2 * threads`、`dynamic_threads` |
| `torch/_inductor/codegen/cpp.py` | `LoopNest.mark_parallel` | `parallel`、`collapsed`、`parallel_reduction_count` |
| `torch/_inductor/codegen/common.py` | `Kernel.codegen_loops_impl` | 并行区域开合、reduction prefix/suffix、`omp single` |
| `torch/_inductor/codegen/cpp.py` | `CppKernel._gen_parallel_reduction_buffers` | `tmp_acc_arr[tid]`、局部累加器、合并循环 |
| `torch/_inductor/codegen/cpp.py` | `CppKernel.update_stores_with_parallel_reduction` | `tmp_acc` 到 `tmp_acc_local` 的替换 |
| `torch/_inductor/codegen/cpp.py` | `CppKernel._use_parallel_atomic_add` / `store` | `atomic_add` 与单线程 `+=` |
| `torch/_inductor/codegen/cpp.py` | `OuterLoopFusedKernel.decide_parallel_depth` | 多个内层 loop nest 的深度取最小 |

阅读顺序建议：先读 `WorkSharing`，再看 `decide_parallel_depth`，最后读 `codegen_loops_impl` 的 `gen_loop_with_reduction`。这样 pragma 文本与归约骨架能一一对应。

## 常见坑

1. **在 `torch.compile` 之后改 `torch.set_num_threads`。** 线程数在 kernel 生成时就被写进 `num_threads(...)` 或决定是否生成 pragma；生成后改线程不会重写已有 kernel。实验必须在第一次调用前设置。

2. **把 `num_threads(4)` 当固定契约。** 它来自 `torch.get_num_threads()` 与 `config.cpp.threads`，不同机器、不同 `OMP_NUM_THREADS`、不同容器配额都会变。测试断言应该检查“有/没有 omp”，不要硬编码 4。

3. **把 `omp single` 当第二个 kernel。** `single` 是同一个 `extern "C"` 函数里的串行区域，用于归约后的后缀。kernel 数看 `extern "C"` 或 metrics，不要数 `for` 或 pragma。

4. **以为归约一定用 `tmp_acc_arr[tid]`。** 只有归约维被并行时才需要线程数组；外层并行 + 内层归约时，每个线程有私有累加器，最后直接写自己的输出行，lab_a 就是这种形态。

5. **用 `atomic_add` 做需要确定性的归约。** 原子加的顺序由运行时决定，低位可能每次不同。要求可复现时用线程局部累加 + 固定顺序合并，或固定线程数并接受顺序差异。

6. **忽略 `min_chunk_size`。** 总工作量分给每个线程后不足 `512` 时，`decide_parallel_depth` 不会并行；小张量没有 pragma 不是 bug。把 64 元素的小循环硬并行，调度开销通常更大。

7. **在同一进程里比较 4 线程与 1 线程。** 缓存与 kernel 生成都可能复用，结果会误导。lab_d 用子进程与独立 cache 正是为了避免这一点。

8. **要求归约结果与 eager 逐位相等。** 并行与向量化都会改变加法顺序；`4.768e-07`（lab_a）与 `1.95e-03`（final_project）都是合法误差。用容差与相对误差判断正确性。

## 自我检查

- [ ] 我能说出 `parallel_num_threads()` 的两个来源，以及本机为什么是 4。
- [ ] 我能解释 `WorkSharing.parallel` 什么时候生成 `num_threads(N)`，什么时候生成裸 `#pragma omp parallel`。
- [ ] 我能说明 `max_parallel_depth` 为什么在第一个归约/非归约切换处停止。
- [ ] 我能解释 `start_depth` 提升的 `300` 系数在解决什么问题。
- [ ] 我能说出 `decide_parallel_depth` 的三个停止条件。
- [ ] 我能区分“外层并行 + 内层归约”和“归约维并行”两种归约形态。
- [ ] 我能解释 `omp single` 出现的位置与作用，并说明它不是第二个 kernel。
- [ ] 我知道 `threads=1` 仍然向量化，且归约误差不能要求逐位相等。

## 延伸阅读

- **`torch/_inductor/codegen/cpp.py` 的 `WorkSharing`**：读哪里——`parallel`、`single`、`close` 与 `use_dynamic` 分支；学什么——pragma 文本的唯一生成点；关联——本章所有 `#pragma` 片段。
- **`torch/_inductor/codegen/cpp.py` 的 `LoopNest.max_parallel_depth` 与 `decide_parallel_depth`**：读哪里——连续同类型循环、`start_depth` 提升、`min_chunk_size`；学什么——并行深度的启发式；关联——为什么 `omp for` 有时在第二层。
- **`torch/_inductor/codegen/common.py` 的 `Kernel.codegen_loops_impl`**：读哪里——`is_reduction_loop` 的 `worksharing.close()`、`gen_loop_with_reduction`、`omp single`；学什么——归约前后缀的放置；关联——lab_a 的生成代码。
- **`torch/_inductor/codegen/cpp.py` 的 `_gen_parallel_reduction_buffers`**：读哪里——`acc_local`、`acc_arr[tid]`、合并循环；学什么——并行归约的确定性合并；关联——第 22 章的数值正确性。
- **OpenMP 5.2 规范的 `parallel`、`for`、`single`、`collapse` 子句**：读哪里——worksharing 构造与数据环境；学什么——`single` 的隐式 barrier、`for` 的迭代分配；关联——读懂生成的 pragma 语义。
- **`compilers/inductor_deep_dive/21-performance-engineering.md`**：读哪里——为什么线程数不是越多越好、交错测量；学什么——并行收益的测量方法；关联——lab_d 的时间表。

[上一章：Ch 15 向量化](./15-vectorization.md) | [下一章：Ch 17 extern kernel 与模板](./17-extern-kernels-and-templates.md)