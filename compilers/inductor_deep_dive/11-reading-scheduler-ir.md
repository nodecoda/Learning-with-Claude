# Ch 11 读调度 IR：从 ir_pre_fusion 反推 kernel

> **目标**：把 `ir_pre_fusion.txt` 的每个段落读懂，并能在打开 `output_code.py` 之前，从 `var_ranges`、`index`、`group.iteration` 和 `ops.*` 反推出循环嵌套、归约位置和向量化机会。读完你能解释 `index0 = 64*p0 + p1` 为什么对应 `in_ptr0 + x1 + 64*x0`。

> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
```

模型仍是第 1 章的 lab_a：`relu(x @ w + b).sum(1) / 64`，输入 `64×64`。

## 为什么

生成代码离源码太远。源码里一行 `h.sum(dim=1) / h.shape[1]`，生成代码里变成一个 `x0` 外层循环、一个 `x1` 内层向量循环、一个 `tmp_acc0_vec` 累加器、一次 `vec_reduce_all`，再加一个 `omp single` 里的除法。中间发生了什么？答案在调度 IR 里。

`ir_pre_fusion.txt` 是最后一个“每个操作还独立”的观察点。融合之后，`op1` 和 `op2` 被装进 `op1_op2` 这个容器；再往后，loop body 被 codegen 展开成 C++，原始边界就消失了。所以读 IR 的最佳时机是 pre-fusion 文件：它保留了每个节点的写入、依赖、迭代域和逐元素表达式。

读 IR 不是为了“看懂格式”，而是为了回答具体问题：

- 这个 kernel 有几个循环？看 `var_ranges`。
- 内层循环是不是连续访问？看 `index0` 的系数。
- 输出是标量还是向量？看 `index1` 和 `group.iteration` 的第二个元组。
- 为什么生成代码里有 `omp single`？看 reduction 和 epilogue 的分界。
- 为什么向量化是 8 个 float？IR 只给 stride 和 dtype，宽度由后端的 ISA 决定（第 15 章）。

一个可以直接练习的判断：只看 `op1` 的 loop body，你应该能说出“外层 64 行、内层 64 列、每行求和、输出 64 个数”。如果你说不出来，说明 `var_ranges` 和 `index` 还没读进去，后面看 C++ 只能靠猜。

## 主线

### 11.1 文件从哪来：trace 与两个 log 函数

lab_a 里有这一行：

```python
cfg.trace.enabled = True
```

它让 Inductor 在 cache 下写一个 `<hash>.debug/` 目录，里面有：

| 文件 | 内容 |
| --- | --- |
| `fx_graph_readable.py` | AOTAutograd 之后的 core ATen 图（第 2 章） |
| `ir_pre_fusion.txt` | 融合前的调度 IR（本章主角） |
| `ir_post_fusion.txt` | 融合后的调度 IR |
| `output_code.py` | 最终 wrapper + kernel 源码 |

写入由 `torch/_inductor/debug.py` 的两个函数负责：

```python
def log_ir_pre_fusion(nodes: SchedulerNodeList) -> None:
    if ir_pre_fusion_log.isEnabledFor(logging.INFO):
        ir_pre_fusion_log.info("BEFORE FUSION\n%s", DebugFormatter._write_ir(nodes))
    V.debug.ir_pre_fusion(nodes)

def log_ir_post_fusion(nodes: SchedulerNodeList) -> None:
    if ir_post_fusion_log.isEnabledFor(logging.INFO):
        ir_post_fusion_log.info("AFTER FUSION\n%s", DebugFormatter._write_ir(nodes))
    V.debug.ir_post_fusion(nodes)
```

两件事同时发生：`TORCH_LOGS=ir_pre_fusion` 时日志里出现 `BEFORE FUSION` 头；`cfg.trace.enabled=True` 时文件写到磁盘。只开 `TORCH_LOGS` 不会生成 `.debug` 目录，只开 trace 也不会在控制台打印。它们是两条独立的通道。

`DebugFormatter._write_ir(nodes)` 对每个节点调用 `debug_str()`。对 `SchedulerNode` 来说，`debug_str()` 输出固定骨架：

```text
<name>: <类名>(<ir 类型>)
<name>.writes = ...
<name>.unmet_dependencies = ...
<name>.met_dependencies = ...
<name>.min_input_distance = ...
<name>.max_input_distance = ...
<name>.outputs = [ ... ]
<debug_str_extra 的内容>
```

`SchedulerNode.debug_str_extra()` 再补上 `group.device`、`group.iteration`、`sizes`、用到的 buffer 的 layout，以及 `class <name>_loop_body:`。`ExternKernelSchedulerNode` 没有 loop body，所以它的段落短很多。

### 11.2 一个节点段落的固定顺序

读任何一个 `SchedulerNode` 段落，都按这个顺序：

| 顺序 | 行 | 回答什么问题 |
| --- | --- | --- |
| 1 | `<name>: SchedulerNode(ComputedBuffer)` | 这是什么节点 |
| 2 | `.writes` | 它写哪些 buffer，index 是什么 |
| 3 | `.unmet_dependencies` | 它读哪些“不是入口”的 buffer |
| 4 | `.met_dependencies` | 它读哪些入口 buffer |
| 5 | `.min_input_distance` / `.max_input_distance` | 输入来自前面第几个节点 |
| 6 | `.outputs` | 输出 buffer 的 layout 和 users |
| 7 | `.group.iteration` / `.sizes` | 迭代域怎么分组 |
| 8 | `*_layout` | 内存布局（size/stride） |
| 9 | `class *_loop_body` | 逐元素表达式 |

后面的 `snodes[0] =`、`snodes[1] =` 只是把多个节点的段落拼接起来，顺序仍然是这一套。

### 11.3 逐段读 `op0`：extern kernel

真实文本：

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

逐行：

- 第一行说明它是 `ExternKernelSchedulerNode`，包住的是 `ExternKernelOut`。没有 `group.iteration`，也没有 `class op0_loop_body`，因为 extern 不生成循环。
- `writes = [StarDep(name='buf0')]`：整块 `buf0` 归它写，没有逐元素 index。`buf0` 的 layout 是 `64×64`、stride `[64, 1]`，行主序连续。
- `unmet_dependencies = []`：它没有“需要别的节点产出”的读。三个输入 `arg0_1`、`arg1_1`、`arg2_1` 都是图输入，所以出现在 `met_dependencies`。这里的 “met” 是“入口就可用”，不是“运行时已经读完”。
- `min_input_distance = 0`、`max_input_distance = 0`：输入来自图入口，距离为 0。
- `buf0.users` 里只有 `op1`，`can_inplace=False`：`op1` 读 `buf0` 的方式和写 `buf0` 的方式不同（一个 reduction 读整个矩阵，一个写整个矩阵），不能原地复用。
- 最后一行 `op0.node.kernel = extern_kernels.addmm` 是 `debug_str_extra()` 给的，告诉你实际调用的是 `addmm`，不是自研 kernel。

这段 IR 已经足够解释“为什么 `op0` 不参与融合”：写是 `StarDep`，没有 index；节点又是 extern。两条都指向“证据不足”。

### 11.4 逐段读 `op1`：reduction

真实文本：

```text
op1: SchedulerNode(ComputedBuffer)
op1.writes = [MemoryDep('buf1', c0, {c0: 64})]
op1.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1.met_dependencies = []
op1.min_input_distance = 1
op1.max_input_distance = 1
op1.outputs = [
    buf1: ComputedBuffer
    buf1.layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
    buf1.users = [NodeUser(node=SchedulerNode(name='op2'), can_inplace=True, is_weak=False)]
]
op1.group.device = cpu
op1.group.iteration = ((64,), (64,))
op1.sizes = ([64], [64])
buf0_layout = FixedLayout('cpu', torch.float32, size=[64, 64], stride=[64, 1])
buf1_layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
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

先看前四行：

- `writes = [MemoryDep('buf1', c0, {c0: 64})]`：写一个 64 元素的 buffer。
- `unmet = [MemoryDep('buf0', c0, {c0: 4096})]`：读 4096 个元素，也就是整个 64×64 矩阵。`c0` 是同一个符号名，但范围不同；不要因为名字相同就以为两个 dep 在说同一个大小。
- `met = []`：没有入口 buffer 参与。
- `min/max_input_distance = 1`：输入来自前一个节点 `op0`。

再看域：

- `group.iteration = ((64,), (64,))`：第一个元组是 outer range，第二个是 reduction range。
- `sizes = ([64], [64])`：原始迭代尺寸，外层 64，内层 64。
- `buf1` 的 layout 是 `size=[64]`、`stride=[1]`，连续的一维数组；它的唯一用户 `op2` 带 `can_inplace=True`。

最后看 loop body，这是全章最重要的一段：

```text
var_ranges = {p0: 64, p1: 64}
index0 = 64*p0 + p1
index1 = p0
```

- `var_ranges` 有两个变量，所以是两个循环：`p0` 从 0 到 63，`p1` 从 0 到 63。
- `index0 = 64*p0 + p1` 是行主序的扁平偏移。`p0` 是行，`p1` 是列，乘 64 跳到下一行。`buf0` 的 stride 是 `[64, 1]`，与这个表达式一致。
- `index1 = p0` 是输出偏移。输出只依赖行号，说明每行产出一个值。

body 里的调用顺序就是执行顺序：

1. `self.get_index('index0')` 把符号 index 解析成当前循环上下文里的表达式。
2. `ops.load('buf0', get_index)` 读一个元素。
3. `ops.relu(load)` 做逐元素 relu。
4. `ops.reduction(torch.float32, torch.float32, 'sum', relu)` 声明这是一个 sum 归约，输入/输出 dtype 都是 float32。
5. `self.get_index('index1')` 解析输出 index。
6. `ops.store_reduction('buf1', get_index, reduction)` 写归约结果。
7. `return None`：`store_reduction` 是副作用，没有返回值，所以 body 返回 `None`。

把这段翻译成伪代码：

```text
for p0 in range(64):          # 外层：行
    acc = 0
    for p1 in range(64):      # 内层：列
        acc += relu(buf0[64*p0 + p1])
    buf1[p0] = acc
```

C++ 生成代码里的 `x0`、`x1` 就是这里的 `p0`、`p1`；`tmp_acc0_vec` 是 `acc` 的向量化版本。`ops.reduction` 不是立即做归约，而是记录一个归约节点，标记“这个值要参与归约”；真正的累加由 codegen 决定。

### 11.5 逐段读 `op2`：pointwise

真实文本：

```text
op2: SchedulerNode(ComputedBuffer)
op2.writes = [MemoryDep('buf2', c0, {c0: 64})]
op2.unmet_dependencies = [MemoryDep('buf1', c0, {c0: 64})]
op2.met_dependencies = []
op2.min_input_distance = 2
op2.max_input_distance = 2
op2.outputs = [
    buf2: ComputedBuffer
    buf2.layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
    buf2.users = [NodeUser(node=OUTPUT, can_inplace=False, is_weak=False)]
]
op2.group.device = cpu
op2.group.iteration = ((64,), ())
op2.sizes = ([64], [])
buf1_layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
buf2_layout = FixedLayout('cpu', torch.float32, size=[64], stride=[1])
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

和 `op1` 对比，差异都在关键位置：

- `group.iteration = ((64,), ())`：第二个元组是空，说明没有 reduction range。这是一个纯 pointwise。
- `var_ranges = {p0: 64}`：只有一个循环。
- `index0 = p0`：输入和输出都是 64 元素的一维数组，逐元素对应。
- `buf1` 是输入，`buf2` 是输出；`buf2.users = [NodeUser(node=OUTPUT, ...)]` 说明它是图输出。
- body 返回 `store`，不是 `None`。`ops.store` 返回的是 store 节点本身，作为 body 的最后表达式；`store_reduction` 没有返回值。这是读 IR 时区分普通写和归约写的一个小线索。

伪代码：

```text
for p0 in range(64):
    buf2[p0] = buf1[p0] / 64.0
```

### 11.6 `ir_post_fusion.txt`：容器，不是合并后的 body

融合后，`op1` 和 `op2` 的段落被包进一个新节点：

```text
op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)
op1_op2.writes = [MemoryDep('buf1', c0, {c0: 64}), MemoryDep('buf2', c0, {c0: 64})]
op1_op2.unmet_dependencies = [MemoryDep('buf0', c0, {c0: 4096})]
op1_op2.met_dependencies = []
op1_op2.min_input_distance = 1
op1_op2.max_input_distance = 2
op1_op2.outputs = [
    buf1: ComputedBuffer
    ...
    buf2: ComputedBuffer
    ...
]
op1_op2.snodes[0] =
op1: SchedulerNode(ComputedBuffer)
...
op1_op2.snodes[1] =
op2: SchedulerNode(ComputedBuffer)
...
```

要读出四件事：

1. `writes` 是并集：`buf1` 和 `buf2` 都算这个融合节点的输出。
2. `unmet_dependencies` 收缩成只剩 `buf0`：`op2` 对 `buf1` 的读在融合内部解决了。
3. `min_input_distance = 1`、`max_input_distance = 2`：融合节点跨了两个原始节点。
4. `snodes[0]`、`snodes[1]` 下面是两份**完整未合并**的 loop body。

第 4 点最容易误判。你可能会在 post-fusion 文件里找“一个包含 relu、sum、div 的 body”，找不到，然后怀疑融合没发生。实际上 `FusedSchedulerNode` 的职责只是打包和取并集；循环合并、reduction 累加器、epilogue 的位置，都在 codegen 阶段由 `CppKernelProxy` / `LoopBody` 决定（第 13、14 章）。post-fusion 文件证明“它们被安排在一起”，不证明“它们已经被改写成一段代码”。

### 11.7 反推 kernel：一张对照表

把 `op1`、`op2` 的 IR 和最终 C++ 对起来：

| IR 里的东西 | C++ 里的对应物 | 说明 |
| --- | --- | --- |
| `var_ranges = {p0: 64, p1: 64}` | `for x0 < 64` + `for x1 < 64` | 两个循环，名字由 codegen 重命名 |
| `index0 = 64*p0 + p1` | `in_ptr0 + x1 + 64*x0` | 行主序扁平偏移 |
| `index1 = p0` | `in_out_ptr0[x0]` | 每行一个输出 |
| `ops.load('buf0', ...)` | `Vectorized<float>::loadu(...)` | 连续访问被向量化 |
| `ops.relu` | `at::vec::clamp_min(tmp0, 0)` | relu 的向量实现 |
| `ops.reduction(..., 'sum', ...)` | `tmp_acc0_vec` + `vec_reduce_all` | 归约累加器 |
| `ops.store_reduction` | `in_out_ptr0[x0] = ...` | 归约结果写回 |
| `op2` 的 `truediv` | `omp single` 段里的 `tmp0 / tmp2` | epilogue |
| `group.iteration = ((64,), (64,))` | 外层并行、内层归约 | reduction range 决定内层 |
| `stride=[64, 1]` | `loadu(..., 8)` 连续 8 个 float | AVX2 的向量宽度（第 15 章） |

对应的 C++ 片段（已删去无关行，`...` 表示省略）：

```text
#pragma omp parallel num_threads(4)
{
    #pragma omp for
    for(int64_t x0=0; x0<64; x0+=1)
    {
        float tmp_acc0 = 0;
        at::vec::Vectorized<float> tmp_acc0_vec = at::vec::Vectorized<float>(0);
        for(int64_t x1=0; x1<64; x1+=8)
        {
            auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + x1 + 64*x0, 8);
            auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
            tmp_acc0_vec = tmp_acc0_vec + tmp1;
        }
        tmp_acc0 = tmp_acc0 + at::vec::vec_reduce_all<float, 1>(
            [](at::vec::Vectorized<float>& x, at::vec::Vectorized<float>& y) { return x + y; },
            tmp_acc0_vec);
        in_out_ptr0[x0] = static_cast<float>(tmp_acc0);
    }
    #pragma omp single
    {
        for(int64_t x0=0; x0<64; x0+=8)
        {
            auto tmp0 = at::vec::Vectorized<float>::loadu(out_ptr0 + x0, 8);
            auto tmp1 = static_cast<float>(64.0);
            auto tmp2 = at::vec::Vectorized<float>(tmp1);
            auto tmp3 = tmp0 / tmp2;
            tmp3.store(in_out_ptr0 + x0);
        }
    }
}
```

注意 `in_out_ptr0` 同时承担 `buf1` 和 `buf2` 的角色：`buf1` 是 reduction 的输出，`buf2` 是除法的输出，`can_inplace=True` 让它们共用一块存储。这也是第 12 章要讲的 reuse 的一个入口。

### 11.8 读 IR 的固定流程

每次拿到一份新的 `ir_pre_fusion.txt`，按这五步走：

1. **看 header**：`SchedulerNode` 还是 `ExternKernelSchedulerNode`。前者有 loop body，后者没有。
2. **看 writes/unmet**：建立“谁读谁”的边。名字相同就画一条边，范围不同只说明访问的元素数不同。
3. **看 group.iteration/sizes**：第一个元组是外层域，第二个是 reduction 域；空元组表示没有归约。
4. **看 loop body**：`var_ranges` 决定循环层数，`index` 决定访问模式，`ops.*` 决定计算。
5. **看 layout**：stride 决定连续性和向量化可能；`size` 决定 buffer 的实际形状。
6. **最后看 output_code**：验证你的预测，而不是让 C++ 反过来教你 IR。

这套流程的价值在于“可预测”。看完 `op1` 的 IR，你应该先写下“外层 64、内层 64、每行 sum、输出 64”，再去 C++ 里找 `for x0`、`for x1`、`vec_reduce_all`。如果预测和代码不一致，差异本身就是线索：可能是循环顺序被重排、reduction 被拆分，或者 fusion 改变了域。

## 可跑实验

生成产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

定位最新的 debug 目录，并完整打印 pre-fusion IR：

```powershell
$dbg = Get-ChildItem compilers\inductor_deep_dive\code\.cache -Recurse -Directory -Filter *.debug |
    Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content -LiteralPath (Join-Path $dbg.FullName 'ir_pre_fusion.txt') -Encoding utf8
```

对照 11.3 到 11.5，确认三件事：

- `op0` 没有 `class op0_loop_body`。
- `op1` 的 `var_ranges` 有两个变量，`op2` 只有一个。
- `op1` 的 body 返回 `None`，`op2` 的 body 返回 `store`。

只抽取关键行做快速核对：

```powershell
Select-String -LiteralPath (Join-Path $dbg.FullName 'ir_pre_fusion.txt') `
    -Pattern 'group.iteration|var_ranges|^    index\d|ops\.load|ops\.reduction|ops\.store_reduction|ops\.truediv|ops\.store\(|return ' |
    ForEach-Object { $_.Line }
```

预期能看到：

```text
op1.group.iteration = ((64,), (64,))
    var_ranges = {p0: 64, p1: 64}
    index0 = 64*p0 + p1
    index1 = p0
        load = ops.load('buf0', get_index)
        reduction = ops.reduction(torch.float32, torch.float32, 'sum', relu)
        store_reduction = ops.store_reduction('buf1', get_index_1, reduction)
        return None
op2.group.iteration = ((64,), ())
    var_ranges = {p0: 64}
    index0 = p0
        load = ops.load('buf1', get_index)
        truediv = ops.truediv(load, constant)
        store = ops.store('buf2', get_index_1, truediv, None)
        return store
```

再看 post-fusion 的节点头：

```powershell
Select-String -LiteralPath (Join-Path $dbg.FullName 'ir_post_fusion.txt') `
    -Pattern '^(op\d+(_op\d+)?:|op\d+(_op\d+)?\.(writes|unmet)|op\d+_op\d+\.snodes)' |
    ForEach-Object { $_.Line }
```

预期能看到 `op1_op2: OuterLoopFusedSchedulerNode(SchedulerNode,SchedulerNode)`、两行 `snodes`，以及合并后的 `writes` / `unmet`。如果你只看到 `op1`、`op2` 而没有 `op1_op2`，说明你打开的是 pre-fusion 文件，或者融合被配置关掉了。

最后一个练习：在看 `output_code.py` 之前，先在纸上写出你预测的循环结构，然后运行：

```powershell
Select-String -LiteralPath (Join-Path $dbg.FullName 'output_code.py') `
    -Pattern 'for\(int64_t x0|for\(int64_t x1|vec_reduce_all|omp single|loadu' |
    ForEach-Object { $_.Line.Trim() }
```

把实际结果和你写的预测对齐。差异通常来自三处：循环顺序被重排、reduction 被拆成内外两段、epilogue 被挪到 `omp single`。

## 源码落点

- `torch/_inductor/scheduler.py`
  - `BaseSchedulerNode.debug_str`：骨架的拼接顺序；`writes`、`unmet`、`met`、距离、outputs。
  - `SchedulerNode.debug_str_extra`：`group.device`、`group.iteration`、`sizes`、layout、`class <name>_loop_body`。
  - `FusedSchedulerNode.debug_str`：`snodes` 的展开方式。
  - `SchedulerNode._compute_attrs`：`_sizes`、`_body`、`group` 怎么来的。
  - `ranges_from_index_vars` / `merge_loops`：`var_ranges` 与循环合并的关系。
- `torch/_inductor/loop_body.py`
  - `LoopBody.debug_str`：`var_ranges`、`indexing_exprs`、`def body(self, ops)` 的生成。
  - `get_index`：符号 index 如何解析成当前循环上下文里的表达式。
  - `load` / `store` / `store_reduction` / `reduction`：四个最常出现在 IR 里的 ops。
- `torch/_inductor/debug.py`
  - `log_ir_pre_fusion` / `log_ir_post_fusion`：`BEFORE FUSION` / `AFTER FUSION` 头和文件写入。
  - `DebugFormatter._write_ir`：节点列表如何拼成文本。
- `torch/_inductor/dependencies.py`
  - `extract_read_writes`、`index_vars_squeeze`：loop body 怎么变成 `MemoryDep`。
- `torch/_inductor/ir.py`
  - `ComputedBuffer.simplify_and_reorder`：`_sizes` 和 `LoopBody` 的来源。
  - `FixedLayout`：`size`/`stride` 的语义。
- `torch/_inductor/ops_handler.py`
  - `load`、`store`、`store_reduction`、`reduction` 的 docstring：四个 ops 的契约。

## 常见坑

1. **把 `index0` 当成 buffer 的维度下标**。它是扁平的元素偏移表达式；`index0 = 64*p0 + p1` 里的 64 是行宽，不是“第 64 维”。真正的维度信息在 `layout.size` 和 `layout.stride` 里。

2. **把 `group.iteration` 的第二个元组当成第二个输出**。它是 reduction range；`((64,), (64,))` 表示外层 64、归约 64，`((64,), ())` 表示纯 pointwise。输出在 `outputs` 里，不在 group 里。

3. **在 post-fusion 文件里找合并后的 loop body**。`FusedSchedulerNode` 只打包，不重写 body；`snodes[0]`、`snodes[1]` 下仍是两份独立 body。合并发生在 codegen。

4. **用 `c0` 的范围判断 buffer 大小**。`MemoryDep('buf0', c0, {c0: 4096})` 里的 4096 是元素个数，`c0` 只是一个符号；同一个 `c0` 在 `buf1` 的 dep 里范围是 64。看 buffer 形状要看 layout。

5. **把 `return None` 当成 body 写坏了**。`store_reduction` 是副作用，没有返回值；返回 `None` 是正常的。`ops.store` 才会把 store 节点作为表达式返回。

6. **把 `p0`/`p1` 当成 C++ 里的变量名**。它们只是 IR 的符号；codegen 会重命名为 `x0`/`x1`，顺序也可能因为循环重排而变化。

7. **只开 `TORCH_LOGS=ir_pre_fusion` 却找不到文件**。文件由 `cfg.trace.enabled=True` 写；日志由 `TORCH_LOGS` 打印。两条通道要分别开。

8. **把 `sizes` 和 `group.iteration` 混为一谈**。`sizes` 是原始迭代尺寸（`([64], [64])`），`group.iteration` 是后端分组后的 outer/reduction 域。循环重排或合并后，两者可能不再一一对应。

## 自我检查

- [ ] 我能只读 `op1` 的 loop body，写出“外层 64、内层 64、每行 sum、输出 64”的伪代码。
- [ ] 我能解释 `index0 = 64*p0 + p1` 与 `layout.stride = [64, 1]` 的一致性。
- [ ] 我能说出 `group.iteration` 两个元组分别代表什么，以及空元组的含义。
- [ ] 我能解释 `op1` 的 `unmet` 为什么是 4096 个元素，而 `writes` 只有 64 个。
- [ ] 我能说出 post-fusion 文件里 `snodes` 的作用，以及为什么看不到合并后的 body。
- [ ] 我能把 `var_ranges`、`index`、`ops.load`、`ops.reduction` 映射到 C++ 的循环、偏移、`loadu` 和 `vec_reduce_all`。
- [ ] 我能区分 `cfg.trace.enabled` 与 `TORCH_LOGS` 两条通道。

## 延伸阅读

- **`torch/_inductor/scheduler.py` 的 `debug_str` / `debug_str_extra`**：读它们如何把节点状态拼成文本。与本章的关联：所有段落顺序的原始定义；想加自己的字段时也知道该改哪里。
- **`torch/_inductor/loop_body.py` 的 `debug_str` 与四个 ops**：读 `var_ranges`、`indexing_exprs`、`load`、`store_reduction`。与本章的关联：`class *_loop_body` 段落的生成逻辑，以及 `get_index` 的解析方式。
- **`torch/_inductor/debug.py`**：读 `log_ir_pre_fusion`、`log_ir_post_fusion`、`DebugFormatter._write_ir`。与本章的关联：文件与日志两条通道的入口。
- **`compilers/inductor_deep_dive/04-graph-lowering-and-ir.md`**：读 `ComputedBuffer`、`FixedLayout`、`read/write` 的部分。与本章的关联：IR 里的 layout 和 buffer 是那些对象的打印形式。
- **`compilers/inductor_deep_dive/13-loop-body-and-ops-handler.md`**：读 `LoopBody` 到 loop nest 的展开。与本章的关联：本章只读 IR，下一章讲这些 ops 如何变成真正的循环和处理链。

---

[上一章：融合决策](./10-fusion-decisions.md) | [下一章：内存规划](./12-memory-planning.md)
