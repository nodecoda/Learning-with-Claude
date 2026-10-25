# 第 13 章 内存规划：从活跃区间到 arena 偏移

> **目标**：把第 12 章的 liveness interval（活跃区间）变成每个中间值的字节偏移；理解按 start 排序的线性扫描、best-fit（最佳适配）空闲块选择、64 字节对齐、arena（连续内存池）和峰值内存；能用 `plan_memory` 的真实输出解释一次 MLP（多层感知机）的 320 字节规划结果。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"` 后运行示例。本章所有输出来自 `plan_memory` 的实际运行。

## 为什么需要内存规划

解释器可以直接为每个算子创建新的 NumPy 数组，交给 Python 的分配器和垃圾回收。编译器不能这样做。生成 C 代码时，每个临时张量都需要一个确定的地址，而且这个地址要在编译期写进源码。地址不确定，代码就没法生成；地址确定得不好，arena 会浪费几倍内存。

一个 4x16 的 float32 张量有 64 个元素，占 256 字节。它在前向传播中只活两个算子，之后就可以被覆盖。MLP 的两个中间值同时活过一个 op 边界，按通用规则不能共用字节，所以真实规划器给出 320 字节。这个结果不是猜的，下面会逐行读。至于能否通过算子级 in-place 把 64 字节输出塞进 256 字节输入，是第 14 章讨论的问题；对 matmul 来说，答案不是简单的“可以”。

内存规划要同时回答四个问题：

1. 哪些值需要放进 arena，哪些值在 arena 外面。
2. 每个值从第几个字节开始，占多少字节。
3. 两个值的生命周期不重叠时，能否共享同一段字节。
4. 所有值同时活着时，最多占用多少字节。

第 12 章已经算出 interval。本章只做地址分配，不改变 IR，不改变数值语义。规划器是纯分析：输入一张图，输出一张偏移表。

## 先建立直觉

先看一张只有 5 个临时值的图：

```text
op index        0        1        2        3        4
relu_0          [=================]
relu_1                   [===========]
add_2                             [===========]
relu_3                                     [=====]
add_4                                               [=====]

offset 0     | relu_0 ........................ | relu_3 ..... |
offset 64    |          relu_1 ....................... | add_4 ..... |
offset 128   |                   add_2 ......................... |
             ^ 每个格子 64 字节，区间是算子下标，不是字节
```

`relu_0` 在 op 0 产生，最后一次被 op 2 读取，所以它的区间是 `[0, 2]`。`relu_3` 在 op 3 产生，最后一次被 op 4 读取，区间是 `[3, 4]`。两个区间没有交集，因此 `relu_3` 可以复用 `relu_0` 的 offset 0。`relu_1` 的区间是 `[1, 2]`，它不能和 `relu_0` 共用，因为 op 2 同时读取两者。`add_2` 在 op 2 产生，活到 op 4，因此它需要第三块 64 字节。

这就是线性扫描分配器的核心动作：按 start 从小到大处理值，在每个值开始前，把 end 严格小于当前 start 的块放回空闲列表，再从空闲列表里挑一块足够大的空间。

## 核心内容

### 1. 规划器的输入与输出

`plan_memory(graph, alignment=64)` 的输入是一张已经通过 `Graph.validate()` 的图。输出是一个 `MemoryPlan` 对象，里面有：

- `allocations`：`Value` 到 `Allocation` 的映射。
- `arena_bytes`：整个 arena 的字节数。
- `peak_live_bytes`：所有值同时活着时的最大字节数。
- `offset_of(value)`、`size_of(value)`：查询接口。
- `validate()`：检查对齐、越界和活值别名。
- `summary()`：打印人类可读的表格。

`Allocation` 保存 `start`、`end`、`size`、`offset`。`start` 和 `end` 都是算子下标，闭区间。`size` 已经按 64 字节向上取整。`end_byte` 是 `offset + size`，用于判断字节区间是否重叠。

```python
@dataclass
class Allocation:
    value: Value
    start: int
    end: int
    size: int
    offset: int = -1

    @property
    def end_byte(self) -> int:
        return self.offset + self.size

    def overlaps(self, other: "Allocation") -> bool:
        return self.offset < other.end_byte and other.offset < self.end_byte
```

这段来自 `code/mini_ai/memory.py`。注意 `overlaps` 判断的是半开字节区间：`[offset, offset+size)`。两个块首尾相接不算重叠，这正是复用的基础。

### 2. 先算 last_use，再收集候选值

`plan_memory` 首先调用 `graph.validate()` 和 `graph.recompute_users()`。前者保证 SSA、拓扑序和输出存在；后者重建每个 `Value` 的 `users` 列表。然后它建立算子下标表：

```python
op_index = {id(op): i for i, op in enumerate(graph.ops)}
last_use: dict[Value, int] = {}

for i, op in enumerate(graph.ops):
    for v in op.inputs:
        last_use[v] = max(last_use.get(v, i), i)
```

这段循环把每个值最后一次被读取的算子下标记下来。一个值可能被多个算子使用，取最大值。没有用户的值不会被这条循环更新，后面用 `start` 作为默认 end。

接着处理输出：

```python
for v in graph.values:
    if v.kind in {"input", "constant"}:
        continue
    if v.producer is None:
        continue
    start = op_index[id(v.producer)]
    end = last_use.get(v, start)
    if v in graph.outputs:
        end = len(graph.ops)
    last_use[v] = end
```

输出值的 end 被强制设为 `len(graph.ops)`。原因在生成 C 时很清楚：所有算子执行完之后，还有一段输出拷贝循环，把 arena 里的结果复制到调用者提供的 `out0`、`out1`。输出值必须活过这段拷贝，不能在中途被覆盖。`len(graph.ops)` 是一个哨兵下标，表示“最后一个算子之后”。

输入和常量被跳过。输入由调用者通过 `const float* x0` 传入，常量以 `static const float C_x[]` 的形式嵌在 C 文件里。它们不占 arena 的字节。这个选择让输入可以零拷贝进入生成的函数，也让权重不参与每次调用的内存分配。

候选值收集完之后按 `(start, end, name)` 排序：

```python
candidates.sort(key=lambda x: (x[0], x[1], x[2].name))
```

按 start 排序是线性扫描的前提。end 和 name 只是让同一 start 下的顺序稳定，方便复现实验。

### 3. 64 字节对齐与实际大小

每个值需要的字节数不是 `numel * 4`，而是对齐之后的大小：

```python
need = max(alignment, align_up(value.type.numel * 4, alignment))
```

`align_up` 的实现是：

```python
def align_up(n: int, alignment: int) -> int:
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return (n + alignment - 1) // alignment * alignment
```

`align_up(65, 64)` 返回 128，`align_up(64, 64)` 返回 64。实测：

```text
align_up(65,64) = 128 align_up(64,64) = 64
```

`max(alignment, ...)` 处理小张量。一个标量只有 4 字节，但它的块仍然占 64 字节。这不是浪费疏忽，而是有意的：所有块起始地址都必须是 64 的倍数，块大小也必须是 64 的倍数，后续块才能继续保持对齐。一个 4 字节的块如果只占 4 字节，下一个块的 offset 就会变成 68，不再是 64 的倍数。

实测一个包含两个 1 元素张量的图：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
relu_0               [  0,  1]     64       0
add_1                [  1,  2]     64      64
```

`relu_0` 的类型是 `tensor<1>`，`numel * 4 = 4`，向上取整后是 64。`add_1` 也是 64。两个块首尾相接，arena 是 128 字节。`add_1` 在 op 1 读取 `relu_0`，所以 `relu_0` 的 end 是 1，`add_1` 的 start 是 1，释放条件 `end < start` 不成立，两块不能复用。这是下一节要展开的边界。

### 4. 释放条件为什么必须是严格小于

规划器维护三个结构：

- `free_blocks`：可用的空闲块，元素是 `(offset, size)`。
- `active`：当前还活着的分配。
- `arena_bytes`：已经分配到的最高字节位置。

每处理一个新值，先把 `active` 中 `end < start` 的块移到 `free_blocks`：

```python
still_active: list[Allocation] = []
for alloc in active:
    if alloc.end < start:
        free_block(alloc.offset, alloc.size)
    else:
        still_active.append(alloc)
active = still_active
```

条件是 `alloc.end < start`，不是 `<=`。如果旧值的 end 等于新值的 start，说明新值的算子在同一个下标上还要读取旧值。比如：

```text
a = relu(x)      # op 0, 区间 [0, 1]
b = add(a, a)    # op 1, 区间 [1, 1]
```

`a.end = 1`，`b.start = 1`。如果允许 `<=`，`b` 会复用 `a` 的块。生成的 C 会写 `b[i] = a[i] + a[i]`。对逐元素加法，同一个下标先读后写，结果可能碰巧正确；但对 matmul、reduction 或带广播的算子，写 `b` 会破坏 `a` 中尚未读取的元素。规划器不区分算子种类，统一采用保守规则：同一个 op 内读写不复用。

实测把释放条件改成 `alloc.end <= start`，规划器给 `relu_0` 和 `add_1` 分配了同一个 offset：

```text
arena_bytes=64 alignment=64 peak_live_bytes=64
value                 interval     size   offset
relu_0               [  0,  1]     64       0
add_1                [  1,  2]     64       0
```

随后 `plan.validate()` 报错：

```text
IRError live ranges overlap but buffers alias: relu_0 and add_1
```

这个错误来自 `MemoryPlan.validate()` 的活值重叠检查。它说明几何校验能在规划阶段拦截这类错误。第 15 章会用随机图和执行层校验继续加固。

### 5. best-fit：从空闲块里挑最合适的

收集完空闲块后，规划器按 `(size, offset)` 排序，然后找第一块大小足够的：

```python
free_blocks.sort(key=lambda x: (x[1], x[0]))
chosen = None
for i, (off, size) in enumerate(free_blocks):
    if size >= need:
        chosen = i
        break
```

排序键是 `(size, offset)`，所以第一个满足 `size >= need` 的块就是所有可用块中最小的一块。这就是 best-fit（最佳适配）。如果改用 `(offset, size)` 排序并取第一块，就是 first-fit（首次适配）。best-fit 倾向于保留大块给后面的大张量，代价是空闲列表更碎、每次分配都要排序。

如果找到空闲块：

```python
offset, size = free_blocks.pop(chosen)
if size > need:
    free_blocks.append((offset + need, size - need))
```

块比需求大时，从头部切出 `need` 字节，剩余部分放回空闲列表。切出来的剩余块起点是 `offset + need`。因为 `offset` 和 `need` 都是 64 的倍数，剩余块的起点仍然对齐。

如果没有空闲块：

```python
offset = align_up(arena_bytes, alignment)
arena_bytes = offset + need
```

这就是 bump allocation（指针碰撞分配）：arena 只看过的最高位置向后推。`arena_bytes` 始终是 64 的倍数，所以 `align_up` 在这里通常不改变值；保留它是为了防御未来引入非对齐大小。

### 6. 真实规划结果：MLP 的 320 字节

端到端 demo 中的 MLP 是 `Linear(8,16) -> ReLU -> Linear(16,4)`，输入 `4x8`。经过融合后，IR 只剩两个算子：

```text
00 %relu_fused = matmul_bias_relu(x0, linear_weight_t, fc1_bias) : tensor<4, 16>
01 %linear_1_fused = matmul_bias(relu_fused, linear_1_weight_t, fc2_bias) : tensor<4, 4>
```

实际运行 `plan_memory`，输出是：

```text
arena_bytes=320 alignment=64 peak_live_bytes=320
value                 interval     size   offset
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

逐行读：

- `relu_fused` 是 `4x16`，`numel = 64`，`64 * 4 = 256` 字节，向上取整仍是 256。它从 offset 0 开始。
- `linear_1_fused` 是 `4x4`，`numel = 16`，`16 * 4 = 64` 字节。它从 offset 256 开始。
- 两个区间在 op 1 相接。`relu_fused.end = 1`，`linear_1_fused.start = 1`，严格小于不成立，所以不能复用。
- arena 总大小是 `256 + 64 = 320`。峰值也是 320，因为 op 1 执行时两个值都活着。

这里的 320 不是“最少可能”的下界。如果允许 `matmul_bias` 在读取 `relu_fused` 的同时把结果写进 `relu_fused` 的尾部，理论上可以省掉 64 字节。但通用规划器不做这种算子相关的 in-place 判断。它用 64 字节换取一条对所有算子都成立的别名安全规则。第 14 章会讨论什么时候可以打破这条规则，以及打破之后需要哪些检查。

### 7. 峰值与 arena 不是同一个数

`peak_live_bytes` 在每次加入新分配后更新：

```python
plan.peak_live_bytes = max(
    plan.peak_live_bytes,
    sum(a.size for a in active),
)
```

它统计的是“当前活着的块大小之和”，不是“最大 offset + size”。在没有碎片的理想情况下，两者相等。出现碎片时，arena 可能比峰值大。举例：先分配 64，再分配 128，释放 64，再请求 128；best-fit 找不到合适的 64 块，只能在 256 处开新块，arena 变成 384，但任一时刻活着的字节数最多 256。

这个区别在调优时有用。`peak_live_bytes` 告诉你理论下界附近的位置，`arena_bytes` 告诉你实际要分配多少。两者差距大，说明空闲块碎片化，或者规划策略不适合当前图的形状分布。

### 8. 正常路径：复用确实发生

下面这张图有三个独立输入和一条交错的生命周期：

```python
g = Graph()
x = g.add_input("x", (4,))
y = g.add_input("y", (4,))
z = g.add_input("z", (4,))
a = g.add_op("relu", [x])
b = g.add_op("relu", [y])
c = g.add_op("add", [a, b])
d = g.add_op("relu", [z])
out = g.add_op("add", [c, d])
g.add_output(out)
plan = plan_memory(g)
print(plan.summary())
```

实际输出：

```text
arena_bytes=192 alignment=64 peak_live_bytes=192
value                 interval     size   offset
relu_0               [  0,  2]     64       0
relu_3               [  3,  4]     64       0
relu_1               [  1,  2]     64      64
add_4                [  4,  5]     64      64
add_2                [  2,  4]     64     128
```

`relu_3` 的区间 `[3, 4]` 与 `relu_0` 的 `[0, 2]` 不相交，且 `2 < 3`，所以它复用了 offset 0。`add_4` 的区间 `[4, 5]` 与 `relu_1` 的 `[1, 2]` 不相交，复用了 offset 64。`add_2` 活到 op 4，不能和 `relu_3` 或 `add_4` 共用，只能占 offset 128。三块 64 字节，arena 192 字节。

这张图也说明 `summary()` 的排序是按 offset，不是按 start。读表时先看 interval，再看 offset，不要假设行序等于执行顺序。

### 9. 失败路径：对齐被破坏

`validate()` 会检查每个 offset 是否是 `alignment` 的倍数：

```python
if a.offset % self.alignment != 0:
    raise IRError(f"{a.value.name}: offset {a.offset} is not aligned to {self.alignment}")
```

手工把一个 offset 改成 1，实测报错：

```text
IRError relu_0: offset 1 is not aligned to 64
```

同一段校验还会检查 `offset < 0`、`end_byte > arena_bytes`，以及活值之间的字节重叠。它不检查值语义，也不检查规划器是否漏掉了某个值。后者需要第 15 章的覆盖检查和随机压力测试。

另一个失败路径来自图本身。没有输出的图在 `plan_memory` 的第一步就被拒绝：

```text
IRError graph has no outputs
```

这说明内存规划的前置条件不是“有算子”，而是“有可观察的输出”。没有输出，所有临时值都不可观察，DCE 可以把整张图删空，规划没有意义。

## 底层机制

### 区间是算子下标，不是字节

`start` 和 `end` 指向 `graph.ops` 的下标。一个值在 op `i` 产生，在 op `j` 最后一次被读取，区间就是 `[i, j]`。同一 op 内的读写视为同时发生，因为生成的 C 在一个循环体内完成读和写，规划器不能假设循环内的执行顺序。

### 空闲块不合并

实现只把释放的块放回 `free_blocks`，不检查相邻块是否可以合并。考虑两个相邻的 64 字节块先后释放，空闲列表里会出现 `(0,64)` 和 `(64,64)`，而不是 `(0,128)`。如果下一个值需要 128 字节，best-fit 找不到合适块，只能在 arena 尾部新开 128 字节。这会让 arena 偏大。

不合并的实现更短，行为也更容易复现。生产级规划器通常会维护按 offset 排序的空闲块，并在释放时与前后邻块合并。练习会让你加这个功能，并观察 arena 的变化。

### arena 的生命周期

生成的 C 函数在入口分配 arena，在出口释放：

```c
float* arena = (float*)_aligned_malloc(arena_bytes, 64);
...
_aligned_free(arena);
return 0;
```

arena 是每次调用独立的。它不跨调用缓存，也不做线程局部复用。这样做的好处是重入和线程安全；代价是每次前向传播都有一次 `malloc`。MLP 的 320 字节分配在 benchmark 中会被 `malloc` 的开销放大，这也是第 20 章讨论 ABI 和 benchmark 时要考虑的因素。

### 对齐为什么是 64

64 字节是 x86-64 的 cache line 大小，也是 AVX-512 寄存器的宽度。`_aligned_malloc(arena_bytes, 64)` 保证 arena 起点按 64 字节对齐；每个块的 offset 又是 64 的倍数，因此每个块起点都对齐。编译器在生成向量化指令时，可以直接使用对齐加载，不必退化成 `movups`。

对齐不是语义要求，是性能要求。把 alignment 改成 16，程序仍然正确，但 AVX-512 的 64 字节加载可能跨 cache line，自动向量化的收益会下降。第 19 章会用编译选项和汇编观察这一点。

## 进一步用法与变体

### first-fit 与 best-fit

把排序键从 `(size, offset)` 换成 `(offset, size)`，就是 first-fit。first-fit 分配更快，但可能把大块切碎；best-fit 保留大块，但每次分配都要排序，且容易产生大量很小的剩余块。对于算子数量在几十到几百的 mini 编译器，两者差异不大；对于上千个临时值的图，分配策略会影响 arena 大小。

### 按 size class 管理

所有块都是 64 的倍数，可以把空闲块按大小分桶：64、128、256、512……分配时先查对应桶，再查更大的桶。这样不用每次排序，代价是实现更复杂，且需要处理跨桶的剩余块。

### 静态偏移与动态形状

当前 IR 是静态 shape，`numel` 在编译期已知，所以 offset 可以直接写进 C。动态 shape 需要运行时计算 arena 大小，或者为最大 shape 预留空间。动态形状还会让 liveness 随 shape 变化，规划器需要在运行时或按 shape 特化后重新运行。

### 多 arena

一个大 arena 适合单线程前向。多线程推理可以把 arena 按线程切分，或者为每个线程分配独立 arena。多 arena 的峰值是各 arena 峰值之和，不能简单套用单 arena 的 `peak_live_bytes`。

### 与 in-place 规划的关系

当前规划器把“值”与“缓冲区”视为一一对应，除了生命周期不重叠的复用。in-place 规划允许一个算子的输出直接写进某个输入缓冲区，即使两个值的区间在同一个 op 上重叠。这需要算子级的安全证明，不能只靠区间。第 14 章会给出 matmul 的反例和 canary 检测方法。

## 对比与取舍

| 方案 | 分配速度 | arena 大小 | 实现复杂度 | 适用场景 |
| --- | --- | --- | --- | --- |
| 每个值独立 malloc | 慢，调用次数多 | 大，含分配器开销 | 低 | 解释器、原型 |
| bump 分配，不复用 | 快 | 等于所有临时值之和 | 低 | 临时值很少的图 |
| 线性扫描 + first-fit | 中 | 中 | 中 | 通用 mini 编译器 |
| 线性扫描 + best-fit | 中 | 较小 | 中 | 本章实现 |
| 图着色式分配 | 慢 | 小 | 高 | 寄存器分配、复杂约束 |
| 算子级 in-place | 快 | 最小 | 很高 | 手工调优的推理内核 |

本章选择 best-fit 加严格释放条件。它在实现复杂度和 arena 大小之间取得了可解释的平衡，也给后续章节留下了明确的改进空间。

## 踩雷集锦

### 1. 把释放条件写成 `end <= start`

这会让同一个 op 内读取旧值的算子复用旧缓冲区。逐元素算子可能碰巧正确，matmul 和 reduction 会出错。实测中 `validate()` 能抓到 `relu_0` 与 `add_1` 的别名，但不要依赖校验器兜底；条件本身就要写对。

### 2. 规划前不跑 DCE

`plan_memory` 会为所有有 producer 的值分配空间，包括没有被任何输出依赖的死算子。融合和化简之后如果残留死代码，它们仍然会占 arena。正确顺序是先跑 `dce`，再规划。demo 的 `standard_pipeline()` 已经包含 `dce`，所以它的 320 字节是清理后的结果。

### 3. 用 `numel * 4` 当块大小

不向上取整会让后续块的 offset 不再对齐。一个 3 元素张量占 12 字节，下一块从 12 开始，`_aligned_malloc` 的整体对齐被破坏。`need = max(alignment, align_up(numel * 4, alignment))` 两个部分都不能省。

### 4. 把 peak 当成 arena 大小

`peak_live_bytes` 是活值字节数之和，`arena_bytes` 是实际高水位。碎片会让 arena 大于峰值。用峰值去分配 arena 会越界；用 arena 去解释峰值会低估理论下界。

### 5. 忘记输出值的 end 要延长

如果输出值的 end 等于最后一个使用它的算子下标，后续算子可能复用它的块。输出拷贝在所有算子之后执行，此时块已经被覆盖，调用者拿到错误结果。`end = len(graph.ops)` 是必须的。

## 进阶

1. 给空闲块加合并：释放时按 offset 插入，检查前后邻块是否相邻，能合并就合并。用同一批图比较合并前后的 `arena_bytes`。
2. 把 best-fit 换成 first-fit，记录两种策略在随机图上的 arena 分布。不要只看一张图，至少跑 100 张。
3. 为 `MemoryPlan` 增加 `assert_covers_all_temps()`：检查每个有 producer 且不是输入/常量的值都有 allocation。当前 `validate()` 不检查漏分配。
4. 实现按 size class 的空闲列表，测量分配循环的耗时。图规模小的时候，排序反而不一定是瓶颈。
5. 设计一个只允许逐元素算子 in-place 的保守规划器，并写一条规则：只有当输出 shape 与输入 shape 相同、且算子满足逐元素语义时才允许重叠。

## 动手练习

1. 对本章的 5 算子复用图，手工画出 `free_blocks` 在每一步的内容。重点看 op 3 开始前 `relu_0` 被释放、op 4 开始前 `relu_1` 被释放的时刻。
2. 把 alignment 改成 16，重新运行 MLP。解释为什么 `arena_bytes` 仍然是 320，而单元素图会从 128 变成 32。
3. 构造一张图，让 `arena_bytes > peak_live_bytes`。提示：先分配大块，再分配小块，释放大块，再请求一个比小块大的块。
4. 给 `plan_memory` 加一个 `stats` 字段，记录分配次数、复用次数、空闲块数量。用 demo 的 MLP 和本章复用图各跑一次。
5. 把释放条件改回 `<=`，用第 15 章的随机图压力测试跑 100 张图，记录 `validate()` 第一次报错的图和值名。

## 本章重点

- `plan_memory` 用算子下标表示生命周期，按 start 排序后做线性扫描。
- 释放条件是 `end < start`，同一个 op 内的读写不能复用。
- 每个块的大小是 `max(64, align_up(numel * 4, 64))`，offset 必须是 64 的倍数。
- 空闲块按 `(size, offset)` 排序后取第一块，是 best-fit。
- 输出值的 end 是 `len(graph.ops)`，因为它要活到输出拷贝之后。
- MLP 的真实规划结果是 `arena_bytes=320`、`peak_live_bytes=320`，两个值分别占 offset 0 和 256。
- `arena_bytes` 是实际高水位，`peak_live_bytes` 是活值总和，碎片会让两者分离。

## 自我检核

- [ ] 我能解释为什么 `relu_0` 和 `relu_3` 可以共用 offset 0。
- [ ] 我能解释为什么 `relu_0` 和 `add_1` 不能共用，即使它们只差一个 op。
- [ ] 我能手算一个 4x16 float32 张量对齐后的块大小。
- [ ] 我能说出 `peak_live_bytes` 与 `arena_bytes` 的区别。
- [ ] 我知道规划前要先跑 DCE，并知道原因。
- [ ] 我能读懂 `summary()` 的三列：interval、size、offset。
- [ ] 我能构造一个让 `validate()` 报对齐错误的例子。

## 延伸阅读

- **《Engineering a Compiler》第 13 章寄存器分配**：读线性扫描和区间图部分，重点看“活跃区间不能重叠”的约束如何转成分配规则。本章把寄存器换成 arena 字节，约束形状相同。
- **Poletto 与 Sarkar, “Linear Scan Register Allocation” (1999)**：读算法主体和 spill 处理。学它如何用一条扫描线维护活跃集合；本章的 `active` 列表是它的简化版，没有 spill。
- **TVM `src/tir/transforms/storage_rewrite.cc`**：读 StorageRewrite 的复用检测和 in-place 条件。学生产编译器如何在 liveness 之上加入算子级别名分析；对照本章的保守策略，理解差距在哪里。
- **LLVM `lib/CodeGen/StackSlotColoring.cpp`**：读栈槽着色的区间重叠判断。学它如何在栈槽层面做类似 best-fit 的复用；与本章的 `overlaps` 和 `free_blocks` 对照。
- **PyTorch `torch/_inductor/codegen/memory_planning.py`**：读内存规划器的 pool 和 size class 设计。学工业级实现如何处理大量小张量和碎片；本章的 best-fit 是它的最小化版本。

---

下一章：[第 14 章 arena 与别名安全](./14-arena-and-alias-safety.md)
