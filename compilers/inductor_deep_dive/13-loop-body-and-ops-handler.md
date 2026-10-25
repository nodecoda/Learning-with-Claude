# Ch 13 LoopBody 与 ops_handler：循环体如何被保存和重放

> **目标**：看清 TorchInductor 调度 IR 里那段 `class op1_loop_body` 到底是什么。读完后你能从 `ir_post_fusion.txt` 的 loop body 反推它会调用哪些 `ops.*`、索引表达式如何由循环变量代入，并判断改动应该落在 `LoopBody`、`OpOverrides` 还是 `CSEProxy` 哪一层。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe` 14.43）。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

本章使用的实验是 [`code/labs/lab_a_pipeline.py`](./code/labs/lab_a_pipeline.py)。它编译 `relu(x @ w + b).sum(dim=1) / 64`，把 `ir_post_fusion.txt` 和 `output_code.py` 一并写到 `code/.cache` 的 `*.debug` 目录。上一章是 [Ch 12 内存规划](./12-memory-planning.md)，下一章是 [Ch 14 C++ Kernel 生成](./14-cpp-kernel-codegen.md)。

## 为什么

调度器在决定“谁和谁融合”时，手里并没有 C++ 文本，也没有 Triton 源码。它拿到的是一棵由 `SchedulerNode` 组成的图，每个节点里挂着一个 `LoopBody`。`ir_post_fusion.txt` 打印出来的 `class op1_loop_body` 既不是 C++，也不是 ATen 图，而是“用 `ops.*` 协议描述的循环体”。

这层中间表示解决了一个很实际的问题：同一个循环体要在很多场合被“执行”很多次，但每次执行的目标不同。

- 调度阶段要统计它读了哪些 buffer、写了哪些 buffer，从而算出依赖边。
- 融合阶段要把它和邻居的 body 合并、重排迭代变量、改写索引。
- 边界分析要拿它的索引表达式推上界和下界。
- 代码生成阶段要把它翻译成 C++ 标量循环、向量循环或 OpenMP 区域。
- 调试阶段要把它打印成可读文本。

如果循环体直接写成字符串模板，上面五件事都得各写一套解析器；如果循环体写成 Python 闭包，又无法在多次调用之间保持索引符号的稳定。TorchInductor 的选择是：第一次调用闭包时，用 FX tracer 把 `ops.load`、`ops.mul`、`ops.store` 这些调用记录成一张 FX 图；之后所有阶段都重放这张图，只替换当前的 `ops` handler。

理解这一层之后，很多看似奇怪的现象会变得可解释：为什么 `ops.load` 不在 `OpOverrides` 里、为什么 `index0` 是一个名字而不是表达式、为什么融合要重新 `reindex`、为什么生成的 C++ 能把 `index0` 展开成 `x1 + 64*x0`。

## 主线

### 1. LoopBody 是一次“带索引参数的 FX 录制”

`LoopBody` 的构造入口在 `torch/_inductor/loop_body.py`。它的四个关键输入是：

| 参数 | 含义 | 例子（lab_a 的 op1） |
|---|---|---|
| `fn` | 尚未执行的循环体闭包 | 读 `buf0`、`relu`、`sum`、写 `buf1` |
| `var_ranges` | 所有循环变量的取值范围 | `{p0: 64, p1: 64}` |
| `iter_vars` | 并行迭代变量 | `[p0]` |
| `reduce_vars` | 归约变量 | `[p1]` |

`LoopBody.__init__` 把 `var_ranges` 的前 `len(iter_vars)` 项切给迭代空间，其余切给归约空间，然后调用 `_init_with_tracing`。真正录制 FX 图的是 `LoopBodyBlock`：

```python
tracer = LightTracer()
proxy_ops = tracer.create_proxy("placeholder", "ops", (), {})
handler = CountOps(CaptureIndexing(proxy_ops, body, tracer), body.op_counts)
if config.constant_and_index_propagation:
    handler = IndexPropagation(handler, body.var_ranges, body.indirect_var_ranges)
with V.set_ops_handler(handler):
    ops.output(fn(*args))
self.graph = tracer.graph
```

这段代码里有三层包装，职责各不相同：

- `CaptureIndexing` 负责在 `load`、`store`、`store_reduction` 被调用时登记内存访问，把索引表达式交给 `LoopBody.add_index_expr` 去重命名。
- `CountOps` 统计每种 op 出现多少次，后面 `TilingSelect` 判断“是否值得向量化”时会用到。
- `IndexPropagation` 在允许时做常量与索引传播，减少后续重复计算。

`V.set_ops_handler(handler)` 是整件事的开关。循环体闭包里写的 `ops.relu(x)` 并不会直接执行 torch 算子，而是分派到当前 handler；在录制阶段，handler 是 `CaptureIndexing`，它把调用写进 FX 图。

录制结束后，`LoopBody` 里保存的是一张图，而不是执行结果。`ir_post_fusion.txt` 打印的 `def body(self, ops)` 就是这张图的可读形式，`ops` 是重放时注入的 handler。

### 2. 索引表达式：`index0` 是名字，不是公式

`CaptureIndexing.load` 会做两件事：登记这次读取，并把索引表达式 intern 成一个稳定名字。

```python
def load(self, name: str, index: sympy.Expr):
    index = self._add_index(index, MemoryUsageType.LOAD, name)
    return self.inner.load(name, index)
```

`_add_index` 调用 `LoopBody.add_index_expr`：

```python
def add_index_expr(self, expr, mtype, buffer_name=None, mode=None):
    expr = self._wrap_int_to_sympy_integer(expr)
    name = self.indexing_exprs_name.get(expr)
    if not name:
        name = f"index{len(self.indexing_exprs)}"
        self.indexing_exprs_name[expr] = name
        self.indexing_exprs[name] = expr
    self.memory_usage[mtype].append(MemoryEntry(name, buffer_name, mode))
    return name
```

于是 `ir_post_fusion.txt` 里出现的是：

```text
index0 = 64*p0 + p1
index1 = p0
```

`index0` 是一个符号名，真正的表达式保存在 `indexing_exprs["index0"]`。这样做有三个好处：

1. 同一个索引被多次使用时只保存一份，打印和比较都稳定。
2. 调度依赖可以只记名字，等需要上界时再取表达式。
3. 融合时替换 `p0`、`p1` 不会破坏名字。

把名字换成具体循环变量发生在 `LoopBody.__call__`：

```python
def __call__(self, *indices, allow_same_symbol_in_index=False):
    self.indexing = self.indexing_from_args(indices, allow_same_symbol_in_index)
    result = self.root_block()
    self.indexing = None
    return result
```

`indexing_from_args` 把当前的 `indices` 与 `var_ranges` 的键做 `sympy_subs`，得到每个 `indexN` 的当前值。代码生成阶段传入的是 `x0`、`x1`，所以 `index0` 展开成 `64*x0 + x1`，最终被 C++ 索引器写成 `x1 + 64LL*x0`。

`get_read_expr` 与 `get_write_expr` 也建立在这套结构上：它们按 buffer 名在 `memory_usage` 里找到对应的 `index_name`，再返回 `indexing_exprs` 里的表达式。调度器计算依赖、内存规划计算 live interval，用的都是这些表达式。

### 3. `ops_handler`：一张算子协议，两级默认实现

`torch/_inductor/ops_handler.py` 定义的是协议，不是具体后端。核心是 `OpsHandler[T]`：

- `T` 是 handler 的值域。对 C++ 代码生成，`T` 是字符串或 `CppCSEVariable`；对边界分析，`T` 可能是区间；对计数，`T` 可以是 `None`。
- `load`、`store`、`reduction`、`store_reduction` 这些带内存语义的 op 定义在这里，但只抛 `NotImplementedError`，因为它们必须由真正知道 buffer 与 kernel 的层实现。
- 绝大多数逐元素算子由 `DefaultHandler._init_cls` 在类创建时用 `exec` 批量生成，统一转发到 `_default`。

```python
class DefaultHandler(OpsHandler[Any]):
    def _default(self, name, args, kwargs):
        raise NotImplementedError

    def __getattr__(self, name):
        def fallback(*args, **kwargs):
            return self._default(name, args, kwargs)
        return fallback
```

`DefaultHandler` 的子类通常只实现 `_default`，就自动获得全部算子名。`NoopHandler` 把所有 op 变成 `None`，用于只关心形状或依赖的分析；`KernelFormatterHandler` 把调用打印成 `ops.xxx(...)` 文本，调试 IR 时使用。

具体的字符串映射在 `torch/_inductor/codegen/common.py` 的 `OpOverrides` 与 `torch/_inductor/codegen/cpp.py` 的 `CppOverrides`、`CppVecOverrides`。这里有一个容易踩的版本差异：本章写作时安装的 2.14.1+cpu 里，`OpOverrides` 不在 `ops_handler.py`，而在 `codegen/common.py`；`ops_handler.py` 负责协议与默认分派。读源码时如果按旧文章去 `ops_handler.py` 找 `OpOverrides`，会找不到。

三层的分工可以这样记：

| 层 | 负责 | 典型实现 |
|---|---|---|
| 协议 | 定义有哪些 op、参数与返回约定 | `OpsHandler[T]` |
| 默认分派 | 把未实现的 op 转发到 `_default` | `DefaultHandler` |
| 后端映射 | 把 op 变成具体文本或对象 | `OpOverrides`、`CppOverrides`、`CppVecOverrides` |
| 代码生成接线 | 计算 dtype/bounds、做 CSE、写 buffer | `CSEProxy`、`CppKernel` |

`CppOverrides` 的映射通常只有一行：

```python
@staticmethod
def add(a, b):
    return arith_promoted("+", a, b)

@staticmethod
def relu(x):
    return f"std::max({x}, decltype({x})(0))"

@staticmethod
def index_expr(expr, dtype):
    idx_str = cexpr(V.kernel.rename_indexing(expr))
    var = V.kernel.cse.generate(V.kernel.compute, idx_str)
    return ops.to_dtype(var, dtype)
```

注意 `index_expr` 会把表达式写进 `V.kernel.compute`，而不是 `loads`。这解释了后面一个常见坑：索引表达式如果被提前 CSE 到 `compute`，再插到 `loads` 就会形成前向引用；源码里因此在 `check_bounds` 路径上专门尝试 `self.cse.try_get(idx_str)`，命中时回退到 `compute`。

`ops.load` 不在 `OpOverrides` 里。它必须由 `CppKernel.load` 或 `CppVecKernel.load` 实现，因为只有 kernel 知道 buffer 的实参名、索引器的线性化方式和向量化步长。`CSEProxy` 负责在调用后端 handler 前后补齐 bounds、dtype、shape，并把结果写进 CSE：

```python
value = getattr(self.parent_handler, name)(*args, **kwargs)
csevar = V.kernel.cse.generate(V.kernel.compute, value, bounds=bounds, dtype=output_dtype, shape=output_shape)
csevar.update_on_args(name, args, kwargs)
```

所以改一个算子的文本映射，应该改 `CppOverrides`；改 load/store 的地址计算，应该改 `CppKernel`/`CppVecKernel`；改“结果如何复用”，才轮到 `CSEProxy`。

### 4. 同一份 body，四种执行者

`LoopBodyBlock` 录制完之后，重放入口是：

```python
def __call__(self):
    graph = self.graph
    submodules = self.body.submodules
    return InterpreterShim(graph, submodules).run(V.get_ops_handler())
```

`InterpreterShim` 是一个极薄的 `torch.fx.Interpreter` 包装，它按图顺序执行节点，每个节点调用当前 `V.ops` 上同名的方法。因此同一张图可以被不同 handler 执行：

| 执行者 | 目的 | 结果 |
|---|---|---|
| `CaptureIndexing` | 录制循环体 | FX 图与 `memory_usage` |
| `KernelFormatterHandler` | 打印 IR | `ops.load('buf0', index0)` 文本 |
| `CppOverrides` | 生成标量 C++ | `in_ptr0[...]`、`std::max(...)` |
| `CppVecOverrides` | 生成向量 C++ | `Vectorized<float>::loadu(...)` |

循环嵌套本身不在 `LoopBody` 里，而在 `CppKernel` 的 `ranges`、`itervars`、`reduction_depth`。`CppKernel.set_ranges` 把 `call_ranges` 拆成迭代部分和归约部分，并用 `SymT.XBLOCK` 前缀生成 `x0`、`x1`：

```python
self.call_ranges = tuple(lengths) + tuple(reduction_lengths)
self.ranges = [self.rename_indexing(x) for x in self.call_ranges]
self.itervars = [sympy_index_symbol_with_prefix(SymT.XBLOCK, n) for n in range(len(self.ranges))]
self.reduction_depth = len(lengths)
```

`LoopNest.build` 再把这些变量变成 `LoopLevel`，每个 level 保存 `var`、`size`、`steps`、`tiled_size`、`is_reduction`。你在 `output_code.py` 里看到的 `for(int64_t x0=...; x0<...; x0+=8)`，就是 `LoopLevel.lines()` 根据这些字段拼出来的。

`CppKernelProxy.codegen_nodes` 把每个 `SchedulerNode` 包成 `fn(node, *index_vars)`，调用 `node.codegen(index_vars)`；`SchedulerNode.codegen` 最终执行 `self._body(*index_vars)`。于是 `LoopBody.__call__` 拿到 `x0`、`x1`，`indexing_from_args` 代入，`CppVecKernel.load` 写出向量 load。整条链路在这里闭合。

### 5. 变换：融合、重排、重索引

`LoopBody` 不只有 `__call__`。融合与调度需要在不重新 trace 的情况下改写循环体，源码提供了几组变换：

| 方法 | 作用 | 典型场景 |
|---|---|---|
| `merge_loops` | 合并迭代与归约循环 | 归约节点的迭代维度和归约维度重新划分 |
| `reorder_iter_loops` | 交换迭代变量顺序 | 让相邻节点按同一维度遍历 |
| `reindex_iter_loops` | 用新的因子分解重写迭代空间 | `[1024, 8192]` 变成 `[65536, 128]` |
| `expand_dimension_for_pointwise_node` | 扩大某一维并用 `Mod` 防越界 | 广播逐元素节点融入更大循环 |
| `with_indexing_exprs` | 直接替换索引表达式 | 融合时改写读地址 |

这些方法大多走 `_init_with_copy` 快路径：复制已有的 `indexing_exprs`、`subblocks`、`root_block`，再用新的 `var_ranges` 重算索引，而不是重新执行闭包。源码注释说得很直接：`_init_with_tracing()` 慢，复制快。

代价是必须保证符号一致。`indexing_from_args` 默认拒绝“索引里出现 `var_ranges` 之外的符号”，除非显式传 `allow_same_symbol_in_index=True`。融合时如果两个 body 的 `p0` 都还在索引里，就必须走允许同符号的路径，否则会触发断言。理解这一点，才能解释为什么某些融合在 `ir_post_fusion.txt` 里表现为 `OuterLoopFusedSchedulerNode`，而不是简单地把两段文本拼在一起。

### 6. 从 loop body 反推生成代码

lab_a 的 `ir_post_fusion.txt` 里，归约节点与后缀逐元素节点被外层循环融合：

```text
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
...
class op1_loop_body:
    var_ranges = {p0: 64, p1: 64}
    index0 = 64*p0 + p1
    index1 = p0
    def body(self, ops):
        get_index = self.get_index('index0')
        load = ops.load('buf0', get_index)
        relu = ops.relu(load)
        reduction = ops.reduction(torch.float32, torch.float32, 'sum', relu)
        get_index_1 = self.get_index('index1')
        store_reduction = ops.store_reduction('buf1', get_index_1, reduction)
        return None
```

把它和生成的 C++ 对照：

```cpp
for(int64_t x1=static_cast<int64_t>(0LL); x1<static_cast<int64_t>(64LL); x1+=static_cast<int64_t>(8LL))
{
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x1 + 64LL*x0), static_cast<int64_t>(8));
    auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
    tmp_acc0_vec = tmp_acc0_vec + tmp1;
}
tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<float, 1>(..., tmp_acc0_vec);
in_out_ptr0[static_cast<int64_t>(x0)] = static_cast<float>(tmp_acc0);
```

对应关系如下：

| loop body | C++ |
|---|---|
| `ops.load('buf0', index0)` | `loadu(in_ptr0 + (x1 + 64*x0), 8)` |
| `ops.relu(load)` | `clamp_min(tmp0, decltype(tmp0)(0))` |
| `ops.reduction(..., 'sum', relu)` | `tmp_acc0_vec = tmp_acc0_vec + tmp1` 与 `vec_reduce_all` |
| `ops.store_reduction('buf1', index1, ...)` | `in_out_ptr0[x0] = tmp_acc0` |
| `index0 = 64*p0 + p1` | `x1 + 64LL*x0`（`p0=x0, p1=x1`） |

后缀节点 `op2` 不参与归约，它被放进 `#pragma omp single` 的区域：

```text
class op2_loop_body:
    var_ranges = {p0: 64}
    index0 = p0
    def body(self, ops):
        get_index = self.get_index('index0')
        load = ops.load('buf1', get_index)
        constant = ops.constant(64.0, torch.float32)
        truediv = ops.truediv(load, constant)
        get_index_1 = self.get_index('index0')
        store = ops.store('buf2', get_index_1, truediv, None)
        return store
```

`ops.store` 与 `ops.store_reduction` 的区别在这里体现得很清楚：前者写普通 buffer，后者写归约结果，代码生成时分别进入 `stores` 与 `reduction_suffix`。

## 可跑实验

运行 lab_a：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

一次真实的输出（节选，来自本机 2.14.1+cpu）如下：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)

========================================================================
3) 调度前 IR：SchedulerNode 与 loop body（节选）
========================================================================
op1.group.iteration = ((64,), (64,))
op1.group.device = cpu
...
class op1_loop_body:

========================================================================
4) 生成的 C++ kernel 结构
========================================================================
  name         kernel
  vectorized   True
  step         1
  threads      4
  tmp_ops      6
  cpp_lines    108
```

这里 `step=1` 不是“没有向量化”。`kernel_stats` 抓的是第一个 `x0+=`，而归约 kernel 的外层 `x0` 每次只前进 1；真正向量化的是内层 `x1`，步长 8。判断向量化要看 `vectorized` 字段或直接搜索 `loadu`，不要只看 `step`。

再看 `ir_post_fusion.txt` 的原始文本：

```powershell
$d = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
     Sort-Object LastWriteTime | Select-Object -Last 1
Select-String -Path (Join-Path $d.FullName 'ir_post_fusion.txt') -Pattern 'class op|index0|index1|ops\.' -Context 0,1
```

预期能看到：

```text
class op1_loop_body:
    var_ranges = {p0: 64, p1: 64}
    index0 = 64*p0 + p1
    index1 = p0
...
class op2_loop_body:
    var_ranges = {p0: 64}
    index0 = p0
```

然后检查 wrapper 的调用名：

```powershell
Select-String -Path (Join-Path $d.FullName 'output_code.py') -Pattern 'cpp_fused|cpp_fused_div_relu_sum_0'
```

预期看到：

```text
cpp_fused_div_relu_sum_0(buf2, buf0)
```

这一步也验证了 loop body 与 kernel 的边界：body 描述“循环里做什么”，`CppScheduling` 决定“这段 body 被装进哪个 kernel、kernel 叫什么名字”。

第二个实验是 lab_c，用来观察“自定义算子被翻译成 IR 后，body 里出现的是普通 `ops.mul`、`ops.add`”：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py
```

真实输出（节选）：

```text
[compile] fallback: 1 module(s)
[compile] lowering: 1 module(s)

1) 未注册 lowering：调用留在 wrapper 里，无法融合
  buf0 = torch.ops.mylab.affine_naive.default(arg0_1, 2.0, 1.0)

2) 注册 lowering：算子展开成 IR，与 relu 同 kernel
  for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(65536LL); x0+=static_cast<int64_t>(8LL))
  auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
  auto tmp3 = tmp0 * tmp2;
  auto tmp6 = tmp3 + tmp5;
  auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0));

[PASS] lowering 后只有一个 kernel  (1)
[PASS] affine 的乘法/加法进入 kernel  (tmp_ops=8)
```

未注册 lowering 时，loop body 里没有 `affine` 的乘加，只有 wrapper 里的一次黑盒调用；注册后，`L.mul`/`L.add` 产生的就是标准 `ops.mul`/`ops.add`，调度器才有机会把它们与 `relu` 放进同一个 body。这是第 13 章和第 5 章的交界点：lowering 决定 body 里有什么，body 决定调度器能融合什么。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/loop_body.py` | `LoopBody` | `__init__`、`_init_with_tracing`、`_init_with_copy`、`indexing_from_args`、`__call__` |
| `torch/_inductor/loop_body.py` | `LoopBodyBlock` | `__init__` 的三层 handler 包装、`__call__` 的 `InterpreterShim` 重放 |
| `torch/_inductor/loop_body.py` | `CaptureIndexing` | `load`、`store`、`store_reduction` 如何登记 `memory_usage` |
| `torch/_inductor/loop_body.py` | `InterpreterShim` | 为什么需要一个极薄的 FX Interpreter 包装 |
| `torch/_inductor/loop_body.py` | `LoopBody.add_index_expr` / `get_read_expr` / `get_write_expr` | `indexN` 的 intern 与查询 |
| `torch/_inductor/loop_body.py` | `merge_loops` / `reorder_iter_loops` / `reindex_iter_loops` / `expand_dimension_for_pointwise_node` | 融合时如何改写循环体而不重新 trace |
| `torch/_inductor/ops_handler.py` | `OpsHandler` / `DefaultHandler` / `NoopHandler` / `KernelFormatterHandler` | 协议、默认分派、打印器 |
| `torch/_inductor/codegen/common.py` | `OpOverrides` / `CSEProxy` | 通用文本映射与 CSE 接线（2.14.1 的实际位置） |
| `torch/_inductor/codegen/common.py` | `Kernel.set_ranges` / `Kernel.codegen_loops_impl` | `x0`、`x1` 的生成与循环嵌套 |
| `torch/_inductor/codegen/cpp.py` | `CppOverrides` / `CppVecOverrides` | 逐元素 op 的标量与向量文本 |
| `torch/_inductor/codegen/cpp.py` | `CppKernel.load` / `CppKernel.store` / `CppVecKernel.load` | 地址计算与向量 load/store |
| `torch/_inductor/ir.py` | `ComputedBuffer.get_default_sizes_body` | `SchedulerNode` 如何拿到初始 loop body |

阅读顺序建议：先读 `LoopBody.__init__` 与 `LoopBodyBlock.__init__`，再看 `CaptureIndexing.load`，最后看 `CppKernel.load`。这三段连起来，`index0` 从符号名到 C++ 下标的路径就完整了。

## 常见坑

1. **把 `LoopBody` 当成 `GraphModule`。** `LoopBody` 不是 `nn.Module`，它保存的是 `root_block.graph` 一张 FX 图；`GraphModule` 只在 `debug_str` 里临时构造。写 pass 时的“回调收到 Graph，不是 GraphModule”是另一层（post-grad），两处不要混淆。

2. **以为 `ops.load` 在 `OpOverrides` 里。** `load`、`store`、`store_reduction` 在 `OpsHandler` 里只是抽象方法，具体实现在 `CppKernel`/`CppVecKernel` 与 `CSEProxy`。在 `CppOverrides` 里找 `load` 会找不到，进而误以为 Inductor 不支持自定义地址计算。

3. **把 `index0` 当表达式比较。** `ir_post_fusion.txt` 里的 `index0` 是名字；真正的 `sympy` 表达式在 `indexing_exprs`。比较两个索引是否相等要比较表达式，不能比较字符串，因为同一个表达式可能被 intern 成不同名字，也可能在不同 body 里都叫 `index0`。

4. **在录制阶段之外调用 body。** `LoopBody.__call__` 会临时设置 `self.indexing`，调用结束再清空。如果在重放过程中递归调用同一个 body，或者多线程同时重放，`self.indexing` 会被覆盖。源码的 `subblocks` 与 `bind_*_shim` 就是为 `masked`、`scan` 这类需要嵌套重放的场景准备的。

5. **改了 body 却没更新 `var_ranges`。** 融合、重排、重索引都要求 `var_ranges` 与索引表达式一致。手工往图里插 `ops.load` 时如果用了新的符号，`indexing_from_args` 会在“索引长度不匹配”或“同一符号重复”处断言。

6. **把 `ops.store` 与 `ops.store_reduction` 混用。** 前者生成 `buf[i] = value`，后者进入 reduction suffix，并在并行归约时决定是 `tmp_acc` 还是 `tmp_acc_arr[tid]`。写反了通常不会立刻报错，而是数值在多次运行间漂移。

7. **在 `loads` 里插入依赖 `compute` 的索引。** `CppKernel` 的输出顺序是 `loads`、`compute`、`stores`；索引表达式默认写 `compute`。如果把它塞进 `loads`，会形成前向引用。源码在 `check_bounds` 里用 `self.cse.try_get(idx_str)` 做了回退，这就是这个坑的现场补丁。

## 自我检查

- [ ] 我能说出 `LoopBody` 的四个构造参数，并解释 `var_ranges` 如何切给迭代与归约。
- [ ] 我能解释 `index0` 为什么是一个名字，以及 `get_read_expr('buf0')` 返回什么。
- [ ] 我能画出从 `CaptureIndexing` 到 `CppVecKernel.load` 的 handler 替换链。
- [ ] 我能说明 `ops.load` 为什么不在 `OpOverrides` 里，而在 `CppKernel`/`CSEProxy` 里。
- [ ] 我能从 `class op1_loop_body` 反推出 `x1 + 64*x0` 这个 C++ 下标。
- [ ] 我能区分 `ops.store` 与 `ops.store_reduction` 在生成代码中的落点。
- [ ] 我能解释 `merge_loops`、`reorder_iter_loops`、`reindex_iter_loops` 各解决什么融合问题。
- [ ] 我知道在 2.14.1 里 `OpOverrides` 位于 `codegen/common.py`，不在 `ops_handler.py`。

## 延伸阅读

- **`torch/_inductor/loop_body.py` 的 `LoopBody` 与 `LoopBodyBlock`**：读哪里——两个类的 `__init__`、`__call__`、`add_index_expr`；学什么——循环体如何被录制、索引如何被 intern、重放如何注入 handler；关联——本章的 `index0` 与 `class op1_loop_body`。
- **`torch/_inductor/ops_handler.py` 的 `OpsHandler` 与 `DefaultHandler`**：读哪里——`OpsHandler` 的方法签名与 `DefaultHandler._init_cls`；学什么——算子协议如何做到“新增 op 不改所有后端”；关联——`CppOverrides` 为什么能只写差异。
- **`torch/_inductor/codegen/common.py` 的 `OpOverrides` 与 `CSEProxy`**：读哪里——`OpOverrides` 的 `paren`、`constant`、`load/store` 抛错位置，`CSEProxy._default`；学什么——文本映射与 CSE/dtype/bounds 的分工；关联——自定义后端时该继承谁。
- **`torch/_inductor/codegen/cpp.py` 的 `CppKernel.set_ranges`、`CppKernel.load`、`CppVecKernel.load`**：读哪里——`set_ranges` 的 `XBLOCK` 生成、`load` 的 `cexpr_index`、`CppVecKernel.load` 的 stride 分支；学什么——`p0/p1` 如何变成 `x0/x1`，标量与向量地址如何分叉；关联——本章末尾的 C++ 片段。
- **PyTorch FX 文档的 `Interpreter` 与 `Tracer`**：读哪里——`Interpreter.run` 与 `Tracer.create_proxy` 的基本语义；学什么——为什么可以用不同 handler 重放同一张图；关联——`InterpreterShim`。
- **`compilers/inductor_deep_dive/11-reading-scheduler-ir.md`**：读哪里——调度 IR 的 `var_ranges`、`index`、`loop body` 三件套；学什么——从 IR 反推 kernel 结构；关联——本章是它的下一层。

[上一章：Ch 12 内存规划](./12-memory-planning.md) | [下一章：Ch 14 C++ Kernel 生成](./14-cpp-kernel-codegen.md)