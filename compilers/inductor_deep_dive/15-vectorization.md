# Ch 15 向量化：`Vectorized<float>`、tiling 与尾部

> **目标**：理解 TorchInductor 的 CPU 向量化不是“编译器自动帮你做”，而是代码生成阶段显式选择的 tiling + `at::vec::Vectorized<T>`。读完后你能解释 `x0 += 8` 从哪来、`simdlen` 为什么填 512 会退回标量、尾部循环由谁处理，以及为什么归约 kernel 的 `step` 可能是 1 却仍然向量化。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe` 14.43）。本机 CPU 只支持 AVX2（`valid_vec_isa_list()` 返回 `['VecAVX2']`），没有 AVX-512。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

本章使用 [`code/labs/lab_d_perf.py`](./code/labs/lab_d_perf.py) 与 [`code/labs/lab_c_lowering.py`](./code/labs/lab_c_lowering.py)。前一章是 [Ch 14 C++ Kernel 生成](./14-cpp-kernel-codegen.md)，下一章是 [Ch 16 并行化](./16-parallelization.md)。

## 为什么

同样一份 `relu(sin(x) * 2 + y)`，在 `output_code.py` 里可能长成两种完全不同的循环：

```cpp
// 向量
for(int64_t x0=0; x0<4194304; x0+=8) {
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + x0, 8);
    ...
    tmp7.store(out_ptr0 + x0);
}

// 标量
for(int64_t x0=0; x0<4194304; x0+=1) {
    auto tmp0 = in_ptr0[x0];
    ...
    out_ptr0[x0] = tmp6;
}
```

这不是 `cl.exe` 自己“看出来”的，而是 Inductor 在代码生成阶段做的两个决定：先选一个 ISA，再把最内层循环按 ISA 的 lane 数切开。理解这两个决定，才能回答下面这些问题：

- 为什么本机只有 AVX2，`Vectorized<float>` 一次处理 8 个 float？
- 为什么 `cpp.simdlen=512` 不会生成 AVX-512，而是直接退回标量？
- 为什么 4M 元素没有尾部循环，而 65535 元素会有？
- 为什么归约 kernel 的外层 `x0 += 1`，内层 `x1 += 8`？
- 为什么 `threads=1` 仍然向量化，只是没有 OpenMP pragma？
- 为什么非连续索引会让 Inductor 放弃向量化，或者生成一个带 `#pragma unroll` 的标量填充循环？

向量化是性能课程里最容易“看代码猜性能”的地方。本章先把结构讲清楚；时间对比留到第 21 章，因为这台机器上 30%~50% 的测量漂移是常态。

## 主线

### 1. 先选 ISA：`VecISA`、dry compile 与 `simdlen`

`torch/_inductor/cpu_vec_isa.py` 里每个 ISA 是一个 `VecISA` 子类，携带位宽、编译宏、编译参数和每种 dtype 的 lane 数。本机相关的类是：

```python
class VecAVX2(VecISA):
    _bit_width = 256
    _macro = ["CPU_CAPABILITY_AVX2"]
    _arch_flags = "-mavx2 -mfma -mf16c" if not _IS_WINDOWS else "/arch:AVX2"
    _dtype_nelements = {torch.float: 8, torch.bfloat16: 16, torch.float16: 16}
```

三个数字要记住：

| dtype | 位宽 | 硬件 lane 数 | 来源 |
|---|---:|---:|---|
| `float32` | 256 | 8 | `256 / 32` |
| `bfloat16` | 256 | 16 | `256 / 16` |
| `float16` | 256 | 16 | `256 / 16` |
| `int64` | 256 | 4 | `256 / 64` |
| `uint8` | 256 | 32 | `256 / 8`，实际 tiling 可能按 4 字节对齐取 8 |

硬件 lane 数与代码生成 tiling 不是同一个数。`TilingSelect.select_tiling` 默认用 `torch.float` 的 `nelements=8` 作为 `tiling_factor`，再用 `CppVecKernel._get_num_vectors(dtype)` 决定类型：`float32` 得到 1 个 `Vectorized<float>`；`int64` 得到 `ceil(8 * 8 * 8 / 256) = 2`，生成 `VectorizedN<int64_t,2>`，一次迭代仍是 8 个元素（两个寄存器，每个 4 个 int64）。这就是为什么本机实测 `int64` 的步长是 8，而不是 4。

`valid_vec_isa_list()` 在 x86 上不是“看 CPU 支持什么”就结束，它还会对每个候选 ISA 执行 `bool(isa)`。`VecISA.__bool__` 会做一次 dry compile：把一段使用 `at::vec::Vectorized<float>::exp()` 的代码编译成动态库并尝试加载。原因是 PyTorch 二进制与 Inductor 使用的编译器可能不是同一个，Sleef 符号不一定齐全。dry compile 的结果会按“编译器版本 + ISA flags + torch 版本”缓存，所以第一次启动慢，后续快。

本机的实际结果是只有一个候选通过：

```text
valid_vec_isa_list() -> ['VecAVX2']
```

`pick_vec_isa()` 的选择逻辑分两步：

```python
if config.cpp.simdlen is None:
    return get_isa_from_cpu_capability(os.getenv("ATEN_CPU_CAPABILITY"), _valid_vec_isa_list, invalid_vec_isa)

for isa in _valid_vec_isa_list:
    if config.cpp.simdlen == isa.bit_width():
        return isa

return invalid_vec_isa
```

这段代码解释了一个高频误解：`cpp.simdlen` 的单位是**位宽**，不是元素个数，也不是 lane 数。`simdlen=256` 能匹配 `VecAVX2.bit_width()=256`；`simdlen=512` 在本机没有 `VecAVX512` 可匹配，返回 `invalid_vec_isa`，于是 `CppKernelProxy` 只生成标量 kernel；`simdlen=128` 同理，因为本机没有 128 位的候选。不要把 `simdlen` 填成 8 或 `Vectorized<float>` 的 lane 数。

`cpp.vec_isa_ok=False` 是另一条路：它让 `VecISA.__bool__` 直接返回 `False`，`valid_vec_isa_list()` 为空，最终也退回标量。这是 lab_d 用来做对照的开关，不是性能选项。

### 2. 再选 tiling：不是每个循环都值得向量化

有了 ISA，下一步是决定“切哪一维、切多大”。`TilingSelect.select_tiling` 做四件事：

1. 检查所有 dtype 是否在 `VECTORIZABLE_DTYPES` 里。`float32`、`float64`、`bfloat16`、`float16`、`bool`、`uint8`、`int8`、`int32`、`int64`、`float8` 都在列表里；不在列表就返回空 tiling。
2. 取 `tiling_factor = pick_vec_isa().nelements(dtype)`。`float32` 是 8，`bfloat16` 是 16。
3. 调 `_select_tiling_indices` 找连续维。
4. 用 `KernelOpStats` 统计非连续索引与 mask 操作，决定是否放弃向量化。

`_select_tiling_indices` 的核心是 `stride_at_vec_range(index, var, tiling_factor)`：

```python
stride = stride_at_vec_range(index, var, tiling_factor)
if stride == 0:
    continue          # 广播维，不参与 tiling
elif stride == 1:
    contig_vars.add(int(var.name[1:]))
elif all(symbol_is_type(s, SymT.SIZE) for s in stride.free_symbols):
    non_contig_stride_const.add(...)
else:
    non_contig_stride_other.add(...)
```

只有 stride 1 的维才是“天然连续”的候选。`stride=0` 是广播维，跳过；其他常量 stride 或符号 stride 记为潜在的非连续访问。选择规则大致是：

- 没有连续维：选最后一维（`num_itervars - 1`）。
- 有“只连续”的维：取编号最大的那个。
- 有两个连续维且最后一个同时是常量 stride：两个都选，进入 2D tiling。
- 其余情况：取出现次数最多的连续维。

选出 `tiling_indices` 后，`select_tiling` 还会做两个保守判断：

```python
if non_contig_indexing_op_num >= 35 or (op_num > 0 and non_contig_indexing_op_num / op_num >= stats.OVERHEAD_RATIO_THRESHOLD):
    return [], [], stats

if (not reduction_group and group and len(tiling_indices) == 1
        and not has_free_symbols([group[tiling_indices[0]]])
        and group[tiling_indices[0]] < tiling_factor / 4
        and op_num < 10):
    return [], [], stats
```

第一段说：非连续 load/store/index 太多时，向量化的收益抵不过 gather/scatter 的开销，直接不向量化。第二段说：内层范围小于 `tiling_factor / 4` 且算子很少时，`#pragma GCC ivdep` 往往比显式向量更好，也放弃 tiling。这两条是“结构启发式”，不是正确性要求；它们解释了为什么有些小张量代码看起来“没向量化”。

对于低精度 dtype，还有一条降级规则：如果调用范围小于 lane 数，`tiling_factor` 减半，用更窄的 `VectorizedN` 或标量尾段，避免大量 mask。

### 3. CppVecKernel：三种 load 路径

切好 tiling 后，`CppKernelProxy.codegen_functions` 生成 `CppVecKernel`，把 `tiling_factor`、`tiling_idx` 传进去。`CppVecKernel.load` 根据索引在 tiling 维上的 stride 分三条路：

```python
stride = self._try_get_const_stride(index, tiling_var)
if stride == 0:
    return super().load(name, index)      # 标量 load，按需广播
elif stride == 1:
    line = self._get_vec_load_line(var, index, dtype, self._load_mask)
    csevar = self.cse.generate(self.loads, line, dtype=dtype)
else:
    csevar = self._load_or_store_non_contiguous(var, index, dtype)
```

**stride 0：标量加载，延迟广播。** 典型场景是 `(64, 1)` 广播到 `(64, 64)`，或者标量参数参与逐元素运算。代码只读一个标量，后续 `CppVecKernel.broadcast` 在需要向量时生成 `Vectorized<float>(tmp)`。这样不会为了广播重复读 8 次内存。

**stride 1：连续向量加载。** `_get_vec_load_line` 生成：

```cpp
at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8))
```

`loadu` 是 unaligned load，不要求 32 字节对齐；它比 `load` 安全，代价在部分平台上略高。Inductor 选择 `loadu` 是因为不能保证每个 buffer 的起始地址都对齐，尤其是在切片或原地复用之后。

**其他 stride：非连续加载。** `_load_or_store_non_contiguous` 生成一个立即执行的 lambda：

```cpp
[&]
{
    __at_align__ std::array<float, 8> tmpbuf;
    #pragma unroll 8
    for (long i0_inner = 0; i0_inner < 8; i0_inner++)
    {
        tmpbuf[i0_inner] = in_ptr0[...];
    }
    return at::vec::Vectorized<float>::loadu(tmpbuf.data(), 8);
}()
```

它用标量循环把 8 个元素填进对齐数组，再整体 load 成向量。索引用 `scale_index_with_offset` 按 `i0_inner` 展开，间接索引变量会先被 `vec_to_array` 转成数组。这条路能保证结果正确，但每个元素一次标量访问，性能通常不如连续 load；`TilingSelect` 的非连续阈值就是在避免这种情况大面积出现。

`_get_vec_type` 决定类型名：

```python
num_vectors = ceil(tiling_factor * dtype.itemsize * 8 / bit_width)
if num_vectors == 1:
    return f"at::vec::Vectorized<{DTYPE_TO_CPP[dtype]}>"
return f"at::vec::VectorizedN<{DTYPE_TO_CPP[dtype]},{num_vectors}>"
```

`float32` 的 `8*4*8/256=1`，得到 `Vectorized<float>`；`uint8` 的 `8*1*8/256=0.25`，向上取整为 1，但只用了 1/4 的 lane，源码注释里专门说明“uint8 只用 1/4 向量宽度”。`bfloat16` 的 `8*2*8/256=0.5`，也是部分利用。`VectorizedN` 在需要多个向量寄存器拼成一个逻辑向量时出现。

### 4. CppVecOverrides：标量与向量的自动提升

`CppVecOverrides` 的 `__new__` 给几乎所有 op 套了一层 wrapper。wrapper 检查实参里有没有标量、有没有向量：

- 如果全是标量，回退到 `CppOverrides`，生成普通 C++ 表达式。
- 如果既有标量又有向量，先把 Python `int`/`sympy.Expr` 转成 `ops.constant` 或 `ops.index_expr`，再把标量广播成向量。
- 如果全是向量，直接调用对应的向量实现。

这解释了 lab_c 生成代码里的两行“看似多余”的构造：

```cpp
auto tmp1 = static_cast<float>(2.0);
auto tmp2 = at::vec::Vectorized<float>(tmp1);
auto tmp3 = tmp0 * tmp2;
```

`2.0` 是标量，`tmp0` 是向量；wrapper 把标量提升为 `Vectorized<float>`，再做向量乘法。标量路径不会这样写，它会生成 `float(tmp1 * tmp2)` 或直接 `tmp0 * 2.0`。

逐元素算子的向量实现大多只改函数名：

| op | 标量 | 向量 |
|---|---|---|
| `relu` | `std::max(x, decltype(x)(0))` | `at::vec::clamp_min(x, decltype(x)(0))` |
| `sin` | `std::sin(x)` | `x.sin()` |
| `exp` | `std::exp(x)` | `x.exp()` |
| `where` | 三元表达式 | `at::vec::VecMask` + blend |
| `masked` | `mask ? body : other` | 递归处理 masked body，再按 mask 选择 |

`masked` 是特例。它不能简单地把 mask 广播成向量，因为 masked body 里可能有越界 load；`CppVecOverrides` 会保留 lambda 结构，让 body 在 mask 条件下执行，`_load_mask` 则用于向量 load 的 masked 版本。

### 5. 尾部：主循环之外的 0 到 7 个元素

`LoopNest.tile` 把循环改成 `x0 += 8`，同时记录：

```text
tiled_size = FloorDiv(size, 8) * 8
```

主向量 kernel 的 `active_ranges` 是 `(0, tiled_size)`，尾部 kernel 是 `(tiled_size, size)`。尾部怎么生成，取决于两个开关：

```python
if config.cpp.enable_loop_tail_vec and _tail_vec_worthwhile(tail_size, tiling_factor):
    tail_kernel = codegen_kernel(self.vec_kernel_cls, tiling_factors[0], tiling_indices[0], tail_size)
else:
    tail_kernel = scalar_kernel
    scalar_kernel.inner_itervars = [loop.var]
```

`enable_loop_tail_vec` 默认是 `True`，但不是有尾部就一定走 masked 向量。`_tail_vec_worthwhile` 会看 mask 与非连续索引的比例：

```python
if op_num > 0 and (mask_op_count + non_contig_indexing_op_counter) / op_num > OVERHEAD_RATIO_THRESHOLD:
    hint_tail_size = sizevars.optimization_hint(tail_size)
    return 2 * hint_tail_size > tiling_factor
return True
```

如果尾部很小、mask 开销又高，就退回标量尾部；否则生成带 `num_elems=tail_size` 的 masked 向量。`_tail_vec_worthwhile` 的注释写得很清楚：tail vectorization 有不可忽略的 mask/cast/blend 开销。

尾部向量 load 的文本是：

```cpp
at::vec::Vectorized<float>::loadu(ptr, static_cast<int64_t>(tail))
```

第二个参数告诉 `Vectorized` 只读 `tail` 个元素，剩余 lane 用默认值填充；store 也带长度：

```cpp
value.store(ptr, static_cast<int64_t>(tail));
```

对于 `float32` 且没有尾部的主循环，store 可以省略长度，生成 `value.store(ptr)`；这是 `_get_store_line` 里的一个小优化。

### 6. 指标与“看起来没向量化”的三种情况

`metrics.generated_cpp_vec_kernel_count` 在 `CppKernelProxy.codegen_functions` 里递增：一维 tiling 加 1，二维 tiling 加 2。它统计的是“生成了几个向量 kernel”，不是“执行了多少条向量指令”。`metrics.generated_kernel_count` 统计的是 `CppScheduling` 层的 kernel 组数，通常一个融合组算 1 个，但它会减去 `CppKernelProxy` 内部生成的计数，避免重复。

判断一份代码有没有向量化，优先看结构：

```python
body = common.extract_kernel(src)
vectorized = "Vectorized<float>" in body
step = re.search(r"x0\+=static_cast<int64_t>\((\d+)LL\)", body)
```

`kernel_stats` 的 `vectorized` 就是这么来的。三种常见误判：

1. **归约 kernel 的 `step=1`。** 外层 `x0` 是迭代维，没有被 tile；内层 `x1` 才是向量维。看 `x1+=8` 或 `loadu`。
2. **尾部标量 kernel 的代码也在同一个函数里。** `extract_kernel` 截到的是整个函数，里面既有 `x0+=8` 的主循环，也可能有尾部标量循环。只看第一处 `x0+=` 会漏掉尾部。
3. **小张量被启发式拒绝。** `TilingSelect` 在范围小于 `tiling_factor/4` 且算子很少时返回空 tiling，代码里完全没有 `Vectorized`。这不是 bug，是策略。

### 7. `simdlen`、`vec_isa_ok`、`threads` 三组对照

把本机的实测结构整理成表：

| 配置 | ISA | 主循环步长 | omp pragma | 说明 |
|---|---|---:|---|---|
| 默认 | `VecAVX2` | `x0 += 8` | `num_threads(4)` | 本机只有 AVX2 |
| `cpp.simdlen=256` | `VecAVX2` | `x0 += 8` | `num_threads(4)` | 位宽精确匹配 |
| `cpp.simdlen=512` | 无 | `x0 += 1` | `num_threads(4)` | 没有 AVX-512，退回标量 |
| `cpp.simdlen=128` | 无 | `x0 += 1` | `num_threads(4)` | 没有 128 位候选，退回标量 |
| `cpp.vec_isa_ok=False` | 无 | `x0 += 1` | `num_threads(4)` | 强制关闭向量化 |
| `torch.set_num_threads(1)` | `VecAVX2` | `x0 += 8` | 无 | 向量化与线程数独立 |

最后一行很重要：向量化和并行化是两条正交的轴。`threads=1` 只是让 `WorkSharing` 不生成 `#pragma omp parallel`，`CppVecKernel` 仍然按 8 lane 生成。反过来，`vec_isa_ok=False` 只是让 tiling 为空，OpenMP 仍然按 4 线程生成。

`cpp.simdlen=512` 的“退回标量”不是 `pick_vec_isa` 主动降级到 AVX2，而是精确匹配失败后返回 `invalid_vec_isa`。这一点在读 `pick_vec_isa` 时容易看漏：它不是“找最接近的位宽”，而是“找完全相等的位宽，找不到就无效”。

## 可跑实验

### 实验 1：三种变体的结构差异

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

lab_d 用三个子进程分别编译 `vec`、`scalar`、`threads1`，每个子进程独立 cache，避免同进程缓存复用。脚本会打印结构表与时间表。结构部分应当与本机实测一致：

```text
variant      vectorized  step  omp threads  compile
vec          yes         8     4            <本次编译耗时>
scalar       no          1     4            <本次编译耗时>
threads1     yes         8     none         <本次编译耗时>
vec_again    yes         8     4            <本次编译耗时>
```

断言部分（真实输出）：

```text
[PASS] vec 走 8 元素向量  (step=8)
[PASS] scalar 退化为 1 元素步长  (step=1)
[PASS] threads1 不再生成 omp pragma  (None)
[PASS] 标量明显慢于向量  (...)
[PASS] 单线程明显慢于四线程  (...)
[PASS] vec 重复测量未出现量级异常（< 2x）  (...)
```

时间数字不写死，因为这台机器的测量漂移很大；结构断言是稳定的。第 21 章会专门讨论怎么测量。注意 lab_d 的 `vec_again` 是同一变体的重复测量，用来估计噪声，不是第四个配置。

### 实验 2：直接看向量与标量的代码

```powershell
$main = Get-ChildItem compilers\inductor_deep_dive\code\.cache\perf -Recurse -Filter *.main.cpp |
        Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -Path $main.FullName -Pattern 'x0\+=|Vectorized<float>|loadu|store\(' | Select-Object -First 15
```

`perf\vec` 下的真实产物（节选）：

```cpp
for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(4194304LL); x0+=static_cast<int64_t>(8LL))
{
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
    auto tmp5 = at::vec::Vectorized<float>::loadu(in_ptr1 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
    auto tmp1 = tmp0.sin();
    auto tmp2 = static_cast<float>(2.0);
    auto tmp3 = at::vec::Vectorized<float>(tmp2);
    auto tmp4 = tmp1 * tmp3;
    auto tmp6 = tmp4 + tmp5;
    auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0));
    tmp7.store(out_ptr0 + static_cast<int64_t>(x0));
}
```

`perf\scalar` 下（节选）：

```cpp
for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(4194304LL); x0+=static_cast<int64_t>(1LL))
{
    auto tmp0 = in_ptr0[static_cast<int64_t>(x0)];
    auto tmp4 = in_ptr1[static_cast<int64_t>(x0)];
    auto tmp1 = std::sin(tmp0);
    auto tmp2 = static_cast<float>(2.0);
    auto tmp3 = float(tmp1 * tmp2);
    auto tmp5 = float(tmp3 + tmp4);
    auto tmp6 = std::max(tmp5, decltype(tmp5)(0));
    out_ptr0[static_cast<int64_t>(x0)] = tmp6;
}
```

`perf\threads1` 下（节选）：

```cpp
{
    for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(4194304LL); x0+=static_cast<int64_t>(8LL))
    {
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
        ...
        tmp7.store(out_ptr0 + static_cast<int64_t>(x0));
    }
}
```

三份代码的差别可以逐项对照：`step`、`Vectorized`、`loadu`、`omp`。不要用“代码行数”判断性能，标量代码往往更短。

### 实验 3：归约的内层向量化

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

lab_a 的归约 kernel 真实产物（节选）：

```cpp
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
```

这里 `x0 += 1` 与 `x1 += 8` 同时存在。`TilingSelect` 选择的是归约维 `x1`，因为沿 `x1` 的索引 stride 是 1；外层 `x0` 是迭代维，不在 tiling 里。归约向量化不是“把整个归约变成一个向量”，而是“每个 lane 各累加一部分，最后 `vec_reduce_all` 横向合并”。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/cpu_vec_isa.py` | `VecISA` | `bit_width`、`nelements`、`check_build`、`__bool__` |
| `torch/_inductor/cpu_vec_isa.py` | `VecAVX2` | `_bit_width=256`、`_dtype_nelements`、`_arch_flags` |
| `torch/_inductor/cpu_vec_isa.py` | `valid_vec_isa_list` / `pick_vec_isa` | x86 探测、`simdlen` 精确匹配、`invalid_vec_isa` 回退 |
| `torch/_inductor/codegen/cpp.py` | `TilingSelect.select_tiling` | dtype 检查、非连续阈值、小范围阈值、低精度降级 |
| `torch/_inductor/codegen/cpp.py` | `TilingSelect._select_tiling_indices` | `stride_at_vec_range` 与连续维选择 |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel` | `_get_num_vectors`、`_get_vec_type`、`load` 三条路径、`_load_or_store_non_contiguous` |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel._get_vec_load_line` | `loadu`、mask、`VectorizedN` |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel._get_store_line` / `store` | `store` 与带长度 `store`、`atomic_add` |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel.broadcast` | 标量到向量的延迟广播 |
| `torch/_inductor/codegen/cpp.py` | `CppVecOverrides` | `__new__` 的标量/向量混合包装、`map` 表 |
| `torch/_inductor/codegen/cpp.py` | `CppKernelProxy.codegen_functions` | `_tail_vec_worthwhile`、`enable_loop_tail_vec`、`active_ranges` |
| `torch/_inductor/codegen/cpp.py` | `LoopLevel.tile` / `LoopLevel.lines` | `tiled_size` 与 `steps` 如何变成循环头 |

阅读顺序建议：先读 `VecAVX2` 与 `pick_vec_isa`，再看 `TilingSelect.select_tiling`，最后读 `CppVecKernel.load`。这样 `tiling_factor` 的 8 与 `loadu(..., 8)` 的 8 就能对上。

## 常见坑

1. **把 `simdlen` 当 lane 数。** `cpp.simdlen` 比较的是 `VecISA.bit_width()`。本机 `simdlen=256` 才是 AVX2；填 8、128、512 都不会得到“8 个 float 的 AVX2”，其中 128 和 512 直接退回标量。

2. **以为 `simdlen=512` 会自动降级到 256。** `pick_vec_isa` 是精确匹配，找不到等值位宽就返回 `invalid_vec_isa`。这不是“选最接近”，而是“选相等，否则关闭”。

3. **用外层 `x0 += 1` 判断没有向量化。** 归约 kernel 的 `x0` 是迭代维，`x1` 才是向量维。判断向量化看 `Vectorized<float>`/`loadu`，或者看是否有任意 `+= 8` 的内层循环。

4. **以为有尾部就一定有 masked 向量。** `enable_loop_tail_vec=True` 只是允许，`_tail_vec_worthwhile` 还会看 mask 比例；尾部很小或非连续访问多时，仍然用标量尾部。

5. **忽略非连续 load 的代价。** `_load_or_store_non_contiguous` 生成的是 `#pragma unroll` 的标量填充循环，不是硬件 gather。它保证正确性，但性能可能明显低于连续 `loadu`；`TilingSelect` 的非连续阈值就是为了避免这种代码大面积出现。

6. **把 `generated_cpp_vec_kernel_count` 当向量指令数。** 它统计向量 kernel 个数，一维 tiling 加 1，二维加 2；不反映每个 kernel 里有多少条 `Vectorized` 指令。

7. **在 `torch.compile` 之后再改 `torch.set_num_threads`。** 线程数影响 kernel 生成；先生成后改线程，不会重写已有 kernel。做 `threads=1` 实验要在第一次调用前设置。

8. **忘记 dry compile 的启动成本。** `valid_vec_isa_list()` 第一次要编译探测代码；如果 `cl.exe` 不在 `PATH`，会直接抛 `InvalidCxxCompiler`。用 `run.ps1` 或在调用前导入 vcvars。

9. **用 `kernel_stats` 的 `vectorized` 判断 int64。** `common.kernel_stats` 只检查 `Vectorized<float>`；`int64` 生成 `VectorizedN<int64_t,2>` 时会被判为 `False`，尽管它确实向量化了。判断整型向量化要搜 `VectorizedN<`、`loadu` 与步长。

## 自我检查

- [ ] 我能说出本机 `VecAVX2` 的位宽，以及 `float32`、`bfloat16`、`int64` 各有多少 lane。
- [ ] 我能解释 `cpp.simdlen=512` 为什么退回标量，而不是 AVX2 降级。
- [ ] 我能从 `x0 += 8` 反推出 `tiling_factor=8` 与 `VecISA.nelements(float)=8`。
- [ ] 我能区分 `load` 的 stride 0、1、其他三条路径，并各举一个场景。
- [ ] 我能解释为什么 lab_c 里常量会生成 `static_cast<float>(2.0)` 和 `Vectorized<float>(tmp1)` 两行。
- [ ] 我能说出 `tiled_size` 的计算方式，以及尾部标量与 masked 向量各自的条件。
- [ ] 我能解释 lab_a 的 `x0 += 1` 与 `x1 += 8` 为什么可以同时存在。
- [ ] 我知道 `threads=1` 不影响向量化，`vec_isa_ok=False` 才是关闭向量化。
- [ ] 我能解释 `int64` 为什么生成 `VectorizedN<int64_t,2>`、一次前进 8 个元素，而不是 `Vectorized<int64_t>`、一次 4 个。

## 延伸阅读

- **`torch/_inductor/cpu_vec_isa.py` 的 `VecISA` 与 `VecAVX2`**：读哪里——`bit_width`、`nelements`、`check_build`、`_dtype_nelements`；学什么——位宽、lane 数与 dry compile；关联——本机为什么只有 AVX2。
- **`torch/_inductor/cpu_vec_isa.py` 的 `pick_vec_isa`**：读哪里——`simdlen is None` 分支与精确匹配循环；学什么——`simdlen` 的语义与 `invalid_vec_isa` 回退；关联——512/128 的实验结果。
- **`torch/_inductor/codegen/cpp.py` 的 `TilingSelect`**：读哪里——`select_tiling` 的四个提前返回、`_select_tiling_indices` 的 `stride_at_vec_range`；学什么——哪些循环会被拒绝向量化；关联——小张量与广播的代码差异。
- **`torch/_inductor/codegen/cpp.py` 的 `CppVecKernel.load` 与 `_load_or_store_non_contiguous`**：读哪里——三条 load 路径与 lambda 填充循环；学什么——连续、广播、非连续各自的代码形态；关联——`loadu` 与 `tmpbuf`。
- **`torch/_inductor/codegen/cpp.py` 的 `CppVecOverrides`**：读哪里——`__new__` 的 wrapper 与 `map` 表；学什么——标量如何提升为向量、哪些 op 有专门实现；关联——常量构造与 `clamp_min`。
- **PyTorch 的 `ATen/cpu/vec` 头文件**：读哪里——`Vectorized<T>::loadu`、`store`、`clamp_min`、`vec_reduce_all`；学什么——生成代码调用的底层 API；关联——本章所有 C++ 片段。
- **`compilers/inductor_deep_dive/21-performance-engineering.md`**：读哪里——交错测量、子进程隔离、噪声区间；学什么——为什么本章不写死时间数字；关联——lab_d 的时间表。

[上一章：Ch 14 C++ Kernel 生成](./14-cpp-kernel-codegen.md) | [下一章：Ch 16 并行化](./16-parallelization.md)