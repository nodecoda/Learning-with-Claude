# 第 14 章 arena 与别名安全

> **目标**：理解 arena 的所有权边界，分清“生命周期不重叠的复用”和“同一个算子内的原地写回（in-place）”；能用生成的 C 指针解释别名；能复现一次 `end <= start` 导致的错误复用；能写出 matmul in-place 的破坏性例子；能用 canary（金丝雀哨兵字）检测越界写。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，MSVC 14.43。命令在仓库根目录执行。先设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本章的规划输出、错误信息和 canary 结果均来自实际运行。

## 为什么需要别名安全

第 13 章把每个临时值放进 arena 的不同偏移。偏移一旦分配错，两个同时活着的值就会指向同一段字节，这种状态叫 alias（别名）。别名不一定错：两个生命周期不重叠的值共用一块内存，是内存规划的目的。别名也可能是灾难：一个算子正在读取的输入被它自己的输出覆盖，后续元素读到的是已经被改写的数据。

这类错误很难靠肉眼发现。C 代码仍然能编译，运行时不一定崩溃，结果可能只差几个数。对于 `relu` 这类逐元素算子，原地写回常常碰巧正确；换成 matmul，错误会随矩阵规模放大。编译器需要一条明确的别名规则，规划器和代码生成器都遵守它。

本章讨论四件事：

1. arena 的边界：谁拥有内存，谁负责释放。
2. 安全复用：生命周期不重叠时共享字节。
3. 不安全复用：同一个算子内输入输出重叠，为什么 matmul 会错。
4. 检测手段：规划期校验、canary、调试分配器。

## 先建立直觉

一次 `mini_forward` 调用的内存布局：

```text
调用者栈 / 堆                     生成的 DLL
+------------------+             +------------------------------+
| x0: 输入指针     | ----------> | 输入数据，只读               |
| out0: 输出指针   | <---------- | 输出拷贝循环                 |
+------------------+             |                              |
                                 | arena (64 字节对齐)          |
                                 | offset 0   : relu_fused      |
                                 | offset 256 : linear_1_fused  |
                                 +------------------------------+
```

arena 内部的别名关系：

```text
安全复用：区间不相交
relu_0   [0,2]  offset 0   |########|
relu_3   [3,4]  offset 0            |########|   旧值已死，可以覆盖

不安全复用：同一 op 内读写
relu_0   [0,1]  offset 0   |########|
add_1    [1,2]  offset 0   |########|            op 1 还要读 relu_0
```

第一张图里，`relu_0` 的 end 是 2，`relu_3` 的 start 是 3，时间上完全分离。第二张图里，`add_1` 在 op 1 读取 `relu_0`，同时又要写自己的输出；同一时刻同一段字节既是输入又是输出，规划器必须禁止。

## 核心内容

### 1. arena 是什么

arena 是一整块连续内存。生成器只调用一次分配函数，之后所有临时值通过偏移访问：

```c
const size_t arena_bytes = 320;
float* arena = (float*)_aligned_malloc(arena_bytes, 64);
...
float* v_12_relu_fused = (float*)((char*)arena + 0);
float* v_13_linear_1_fused = (float*)((char*)arena + 256);
```

这段来自 `code/mini_ai/codegen_c.py` 生成的真实 C 文件。`arena` 的类型是 `float*`，偏移按字节计算，所以指针运算先把 `arena` 转成 `char*`，加上字节偏移，再转回 `float*`。C 的指针算术以元素为单位，不先转 `char*` 会把 256 当成 256 个 float，偏移变成 1024 字节。

arena 的起点由 `_aligned_malloc(arena_bytes, 64)` 保证 64 字节对齐。每个块的 offset 都是 64 的倍数，因此每个块的起点也对齐。这个不变量由规划器的 `need` 和 `align_up` 共同维持。

arena 的生命周期只有一次函数调用：

```c
if (!arena) return 1;
...
_aligned_free(arena);
return 0;
```

分配失败返回 1，调用者通过 `ctypes` 看到非零返回码并抛错。成功时返回 0。arena 在返回前释放，调用者拿不到 arena 内部的指针；输出必须先复制到 `out0`、`out1`。

### 2. 四类值，四种所有权

生成的 C 里有四类值，所有权不同：

| 值类型 | C 中的形式 | 谁拥有 | 是否占 arena |
| --- | --- | --- | --- |
| 输入 | `const float* v_0_x0 = x0;` | 调用者 | 否 |
| 常量 | `static const float C_3[128] = {...};` | 模块的只读数据段 | 否 |
| 临时值 | `float* v_12_relu_fused = (float*)((char*)arena + 0);` | 当前调用的 arena | 是 |
| 输出 | `out0` 指向调用者缓冲区 | 调用者 | 否，最后从 arena 拷贝 |

输入指针是 `const`，生成的代码不会写它。常量是 `static const`，放在只读段，多个调用共享。临时值由 arena 拥有，函数返回即失效。输出由调用者拥有，函数只负责把结果复制进去。

这个划分让输出拷贝成为必要步骤。假设 `relu_fused` 恰好是输出，代码不能把 `out0` 直接指向 arena，因为 arena 随后会被释放。拷贝循环在所有算子之后、释放之前执行：

```c
for (int i = 0; i < 16; ++i) out0[i] = v_13_linear_1_fused[i];
```

### 3. 安全复用与不安全复用

安全复用的条件是：两个值的生命周期不相交，并且旧值的最后一次读取严格早于新值的产生。`plan_memory` 用 `end < start` 表达这个条件。

不安全复用有两种常见形态：

1. 同一个算子内输入输出重叠。比如 `add(a, a)` 的输出复用 `a` 的缓冲区。
2. 生命周期计算错误，把一个还活着的值标记为已死，让后面的值覆盖它。

第一种由释放条件防守，第二种由 liveness 分析和校验器防守。两种都需要测试，因为它们的表现不同：第一种可能在规划阶段被抓到，第二种可能通过几何校验，只在数值上出错。

### 4. 错误复用：把 `<` 写成 `<=`

下面这张图只有两个算子：

```python
g = Graph()
x = g.add_input("x", (4,))
a = g.add_op("relu", [x])
b = g.add_op("add", [a, a])
g.add_output(b)
```

正确的规划是两块 64 字节：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
relu_0               [  0,  1]     64       0
add_1                [  1,  2]     64      64
```

把释放条件改成 `alloc.end <= start`，规划器会认为 `relu_0` 在 op 1 开始前已经死亡，于是让 `add_1` 复用 offset 0。实测输出：

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

`validate()` 的检查逻辑是：

```python
live_overlap = a.start <= b.end and b.start <= a.end
if live_overlap and a.overlaps(b):
    raise IRError(
        f"live ranges overlap but buffers alias: {a.value.name} and {b.value.name}"
    )
```

它先用闭区间判断两个值是否在某个 op 上同时活着，再用半开字节区间判断两块内存是否重叠。两个条件同时成立才报错。这个检查依赖 `start`、`end` 的正确性；如果 liveness 本身算错，校验器可能放行。

### 5. in-place 为什么在 matmul 上出错

逐元素算子可以原地写回：`relu` 的每个输出只依赖同一个下标的输入，读和写交替进行也不会读到别人的结果。matmul 不同。输出元素 `C[i,j]` 依赖整行 `A[i,:]` 和整列 `B[:,j]`。如果 `C` 与 `A` 共享内存，写 `C[i,j]` 会覆盖 `A` 中后续计算还要用的元素。

用两个 2x2 矩阵做实验：

```python
A = np.array([[2.0, 3.0], [4.0, 5.0]], dtype=np.float32)
B = np.array([[2.0, 1.0], [0.0, 1.0]], dtype=np.float32)
expected = A @ B
buf = A.copy()
for i in range(2):
    for j in range(2):
        acc = np.float32(0.0)
        for k in range(2):
            acc += buf[i, k] * B[k, j]
        buf[i, j] = acc
```

这段循环刻意按照生成 C 的顺序，把结果直接写回 `buf`。实测：

```text
expected:
 [[4. 5.]
 [8. 9.]]
in-place buffer:
 [[ 4.  7.]
 [ 8. 13.]]
max_abs_error: 4.0
```

`C[0,0]` 计算为 4，写回 `buf[0,0]`。接着计算 `C[0,1]`，它需要 `A[0,0]`，但读到的已经是 4，而不是原来的 2，于是得到 7 而不是 5。误差继续传播到第二行，最终 max_abs_error 为 4.0。

这个例子说明：不能把“区间不重叠”当成 in-place 的唯一条件。即使输出和输入在同一个 op 上，逐元素算子可能安全，matmul 不安全。通用规划器选择一刀切：同一个 op 内不复用。代码生成器也不假设输入输出可以重叠。

### 6. 生成代码里的别名关系

对融合后的 MLP，生成器输出两个临时指针：

```c
float* v_12_relu_fused = (float*)((char*)arena + 0);
float* v_13_linear_1_fused = (float*)((char*)arena + 256);
```

第一个块 256 字节，第二个块 64 字节，偏移相差 256。`matmul_bias` 读取 `v_12_relu_fused`，写 `v_13_linear_1_fused`。两个指针没有重叠，循环里可以先读后写，不存在覆盖。

如果规划器把第二个指针写成 `+ 0`，生成的 C 仍然能编译，循环仍然会跑，但 `matmul_bias` 会在累加过程中覆盖输入。编译器不会报错，因为 C 语言允许指针别名；优化器甚至可能假设它们不别名，生成更难调试的代码。别名安全必须由编译器前端保证，不能指望 C 编译器发现。

### 7. canary：用哨兵字检测越界

canary 是在块末尾放一个已知值，写操作完成后检查它是否被改写。它不阻止越界，但能让越界从“结果偶尔不对”变成“明确报错”。

下面是一个独立的 Python 调试分配器，块大小 64 字节，块间留出哨兵字：

```python
import struct

class DebugArena:
    def __init__(self, nbytes):
        self.mem = bytearray([0xCD]) * nbytes
        self.regions = {}

    def alloc(self, name, offset, nbytes):
        self.regions[name] = (offset, nbytes)
        struct.pack_into("<I", self.mem, offset + nbytes, 0xDEADBEEF)

    def write_f32(self, offset, values):
        for i, v in enumerate(values):
            struct.pack_into("<f", self.mem, offset + 4 * i, v)

    def check(self, name):
        off, size = self.regions[name]
        return struct.unpack_from("<I", self.mem, off + size)[0] == 0xDEADBEEF
```

分配两块 64 字节，第二块放在 offset 128，中间留出哨兵和填充：

```python
arena = DebugArena(256)
arena.alloc("relu_0", 0, 64)
arena.alloc("add_1", 128, 64)
print("before:", arena.check("relu_0"), arena.check("add_1"))
arena.write_f32(0, [float(i) for i in range(17)])
print("after 17 floats into 16-float block:", arena.check("relu_0"))
print("neighbor untouched:", arena.check("add_1"))
```

实测：

```text
before: True True
after 17 floats into 16-float block: False
neighbor untouched: True
```

16 个 float 占 64 字节，第 17 个 float 写到 offset 64，正好覆盖哨兵字。`relu_0` 的 canary 变成 False，`add_1` 仍然 True。这说明越界发生在第一块末尾，没有波及第二块。

在 C 里，canary 的实现方式相同：在每个块的有效数据后写一个 `uint32_t` 哨兵，算子执行后检查。不要在 `-O2` 下把检查写成未定义行为；哨兵要通过 `volatile` 指针或单独的校验函数读取，避免优化器把它删掉。更严格的做法是用 guard page（保护页）或 AddressSanitizer，越界会直接触发段错误。

### 8. 别名检测的另一种写法

规划期的别名检测可以独立成一个函数，遍历所有 allocation 对：

```python
def assert_no_live_alias(plan):
    items = list(plan.allocations.values())
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.start <= b.end and b.start <= a.end and a.overlaps(b):
                raise AssertionError(
                    f"{a.value.name} and {b.value.name} alias while both are live"
                )
```

这个函数和 `MemoryPlan.validate()` 的检查重复，但可以单独用于调试。它的复杂度是 O(n^2)。对几十个临时值的图足够；对上千个值，需要按 offset 排序后扫描，或者用区间树。

注意它检查的是声明的 interval，不是实际执行顺序。如果 interval 本身错了，函数不会发现。第 15 章会用执行层测试补上这个缺口。

## 底层机制

### 指针算术与对齐

`(float*)((char*)arena + off)` 做两次转换：

1. `arena` 是 `float*`，转成 `char*` 后，`+ off` 按字节移动。
2. 结果转回 `float*`，后续 `ptr[i]` 按 float 下标访问。

如果 `off` 不是 4 的倍数，转回 `float*` 会产生未对齐指针，在某些架构上直接崩溃。规划器保证 `off` 是 64 的倍数，比 4 更严格。`_aligned_malloc` 保证基址是 64 的倍数，两者相加仍然对齐。

### 为什么不在每个值结束时释放

arena 没有 per-value free。规划器在编译期算好偏移，运行时只做一次分配和一次释放。每个值结束时“释放”只是逻辑上的：它的字节可以被后续值复用，但不会归还给系统分配器。这让运行时没有分配器元数据，也没有碎片整理，代价是 arena 必须按峰值预留。

### 输出拷贝与别名

输出拷贝发生在所有算子之后：

```c
for (int i = 0; i < 16; ++i) out0[i] = v_13_linear_1_fused[i];
```

如果调用者传入的 `out0` 恰好指向某个输入缓冲区，拷贝仍然在最后执行，不会影响前面的计算。这是输出拷贝的一个额外好处：调用者可以用同一个缓冲区做输入和输出，只要 shape 兼容。生成的函数不检查这种重叠，调用者需要自己保证 `out0` 有足够的空间。

### arena 释放与返回值

函数在释放 arena 之后返回 0。调用者拿到的 `out0` 是独立缓冲区，不依赖 arena。任何试图返回 arena 内部指针的改动都会产生悬空指针：函数返回后内存已释放，调用者读到的是未定义内容。正确做法永远是拷贝。

## 进一步用法与变体

### 逐元素 in-place

对 `relu`、`exp`、`add`、`mul` 这类逐元素算子，如果输出 shape 与某个输入完全一致，可以让输出复用该输入的块。条件是：

- 算子的每个输出元素只依赖相同下标的输入元素。
- 没有广播导致多个输出元素读同一个输入元素后再写。
- 没有其他值在同一 op 内还需要这个输入。

满足条件时，生成 C 可以把 `dst` 和 `src` 指向同一块内存。对 `relu`，`dst[i] = fmaxf(src[i], 0)` 安全。对 `add(x, y)`，如果输出复用 `x`，`dst[i] = src[i] + y[i]` 也安全，因为 `y` 与 `dst` 不重叠。

### 内存捐赠

有些运行时允许调用者把输出缓冲区“捐赠”给编译器作为临时空间。这要求输出缓冲区足够大，且调用者同意在函数执行期间内容被覆盖。捐赠能减少一次 arena 分配，但把生命周期和所有权责任推给调用者。mini 编译器没有实现这个接口，生成函数的签名只有输入和输出指针。

### 跨调用复用 arena

把 arena 缓存到模块级变量可以省掉每次 `malloc`，但会引入线程安全问题。多线程同时调用 `mini_forward` 会共享同一块 arena，产生数据竞争。若要跨调用复用，需要线程局部存储或显式的 workspace 参数。第 22 章的 LLVM JIT 使用 workspace 参数，思路类似。

### guard page 与 ASan

canary 只能检测块末尾的越界写，检测不到越界读，也可能被精心构造的写绕过。guard page 在块边界放一个不可访问页，越界访问直接触发异常。AddressSanitizer 在编译期插桩，能检测堆、栈和全局变量的越界。它们比 canary 更强，但需要额外的运行时或编译选项。

## 对比与取舍

| 策略 | 内存占用 | 安全性 | 实现复杂度 | 适用场景 |
| --- | --- | --- | --- | --- |
| 每个值独立分配 | 最大 | 高，互不干扰 | 低 | 解释器、调试 |
| 生命周期不重叠复用 | 中 | 高，规则统一 | 中 | 本章规划器 |
| 逐元素 in-place | 较小 | 需要算子级证明 | 高 | 推理内核 |
| 任意 in-place | 最小 | 低，容易覆盖输入 | 很高 | 不推荐 |
| 跨调用 arena 复用 | 小 | 需要线程安全 | 中 | 单线程服务 |

本章的实现选择第二行。它没有追求最小内存，而是用一条简单规则换取可验证的安全性。逐元素 in-place 是下一步优化，必须配合算子白名单和测试。

## 踩雷集锦

### 1. 认为区间不相交就允许任意 in-place

区间不相交只说明两个值不在同一个 op 上同时活跃。它不说明一个算子的输入输出可以重叠。matmul 的反例已经说明：输出和输入在同一个 op 上，区间必然重叠，通用规则直接禁止；如果手工放宽，必须逐个算子证明。

### 2. 把 `_aligned_malloc` 的结果交给 `free`

MSVC 的 `_aligned_malloc` 必须配 `_aligned_free`。混用会导致堆损坏或崩溃。生成代码用 `#if defined(_MSC_VER)` 分开处理，非 MSVC 平台用 `aligned_alloc` 配 `free`。移植时不要只改一半。

### 3. 在 `-O2` 下让 canary 检查被优化掉

如果哨兵只通过普通指针写和读，编译器可能认为它没有副作用并删除检查。把哨兵声明为 `volatile`，或者把校验放进单独的、编译器无法内联推断的函数。更稳妥的是使用 ASan 或 guard page。

### 4. 用 `validate()` 代替执行测试

`validate()` 只检查声明的 interval 和字节范围。如果 liveness 算错，它可能放行。必须用参考解释器或标量执行器对比输出。第 15 章的随机图测试就是为此设计的。

### 5. 返回 arena 内部指针

输出必须拷贝到调用者缓冲区。返回 `arena + offset` 会在函数退出后变成悬空指针。即使调用者立刻使用，内存也可能被下一次 `malloc` 复用。拷贝循环不是可选优化，是生命周期要求。

## 进阶

1. 为 `relu` 实现一个受控的 in-place 规划：当输出 shape 与输入相同且算子在白名单中时，让输出复用输入块。写测试证明结果与不复用版本一致。
2. 把 canary 加进生成的 C：每个块末尾写 `0xDEADBEEF`，所有算子执行后检查。用一个故意越界的算子验证它能报错。
3. 用 guard page 替换 canary：在 arena 前后各留一页 `PROT_NONE`，观察越界写的信号。
4. 写一个别名审计器：输入一张图和一份 plan，输出所有“区间重叠且字节重叠”的对，按字节数排序。
5. 比较“每次调用分配 arena”和“跨调用缓存 arena”的 benchmark。用多线程并发调用，观察数据竞争或锁开销。

## 动手练习

1. 复现 `relu_0` 与 `add_1` 的错误复用：把释放条件改成 `<=`，打印 plan，再调用 `validate()`。记录报错文本。
2. 把本章的 2x2 matmul 例子改成 3x3，手算原地写回后的错误位置。不要运行，先推导，再运行验证。
3. 给 `DebugArena` 增加 `free(name)`：释放后把整块填成 `0xDD`，再次写入后检查哨兵。思考为什么“释放后填充”能帮助发现 use-after-free。
4. 在生成的 C 里找到两个临时指针，确认它们的偏移不重叠。把第二个偏移改成第一个，重新编译运行，记录数值误差。
5. 设计一个逐元素 in-place 的白名单，至少包含 `relu`、`exp`、`add`、`mul`，并写出每个算子的安全性理由。

## 本章重点

- arena 是一次调用内的连续内存，所有临时值通过 64 字节对齐的偏移访问。
- 输入和常量不占 arena；输出在释放前拷贝到调用者缓冲区。
- 安全复用的条件是 `end < start`；同一个 op 内读写不能复用。
- matmul 的 in-place 会覆盖后续计算还要读取的输入，实测 max_abs_error 为 4.0。
- `validate()` 能抓声明区间内的活值别名，但抓不到错误的 liveness。
- canary 在块末尾放哨兵字，能检测越界写；它不是越界读的完整解决方案。
- 逐元素 in-place 需要算子级证明，不能由通用规划器自动决定。

## 自我检核

- [ ] 我能说出 arena、输入、常量、输出四类值各自的所有者。
- [ ] 我能解释 `(float*)((char*)arena + off)` 为什么要先转 `char*`。
- [ ] 我能复现 `end <= start` 导致的别名错误，并读出 `validate()` 的报错。
- [ ] 我能解释 matmul 原地写回为什么会让 `C[0,1]` 读到被覆盖的 `A[0,0]`。
- [ ] 我能写出一个最小 canary 检查，并说明它的局限。
- [ ] 我知道输出为什么必须拷贝，不能返回 arena 指针。
- [ ] 我能区分“生命周期复用”和“算子内 in-place”。

## 延伸阅读

- **《Engineering a Compiler》第 13 章**：读寄存器分配中的冲突图。学“同时活跃的变量不能共用寄存器”与“同时活跃的值不能共用字节”的对应关系；本章的别名检查是它的内存版本。
- **TVM `src/tir/transforms/storage_rewrite.cc`**：读 in-place 检测和 storage scope 处理。学生产编译器如何用算子级规则放宽复用；对照本章的白名单思路。
- **AddressSanitizer 官方文档**：读 heap-buffer-overflow 和 use-after-free 的检测原理。学编译期插桩如何替代 canary；本章的 `DebugArena` 是它的玩具版本。
- **Microsoft `_aligned_malloc` 文档**：读对齐参数和释放要求。确认 `_aligned_free` 的配对规则；这是生成 C 在 Windows 上不崩溃的前提。
- **LLVM `lib/CodeGen/StackSlotColoring.cpp`**：读栈槽复用的冲突判断。学它如何在分配后做颜色合并；与本章的 plan 校验对照。

---

下一章：[第 15 章 内存正确性验证](./15-memory-validation.md)
