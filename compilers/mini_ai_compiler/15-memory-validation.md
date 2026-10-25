# 第 15 章 内存正确性验证

> **目标**：建立内存规划的三层验证：plan 的几何不变量、arena 覆盖执行、与参考解释器的数值对比；能写一个发现越界、别名和释放后使用（use-after-free）的 debug allocator（调试分配器）；能用随机图压力测试发现规划器的错误；能解释为什么 `validate()` 通过不等于内存正确。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，MSVC 14.43。命令在仓库根目录执行。先设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本章的 300 张随机图、强制别名和 debug allocator 输出均来自实际运行。

## 为什么需要内存正确性验证

第 13 章的规划器在分配结束后调用 `plan.validate()`。它检查对齐、越界和活值别名。这个检查必要，但不充分。它验证的是规划器自己声明的区间和偏移，不验证：

1. 每个临时值是否真的被分配了。
2. liveness 的区间是否算对。
3. 生成的代码是否按预期的顺序读写。
4. arena 复用后，数值是否仍然与参考实现一致。

一个规划器可以满足所有几何约束，却在数值上出错。比如把某个值的 end 提前，让后面的值复用它的块；只要声明的区间不重叠，`validate()` 会放行。真正的问题只有在执行时才会暴露。本章用三层验证补齐这个缺口。

## 先建立直觉

```text
             IR + liveness
                  |
                  v
          +----------------+
          | plan_memory    |
          +----------------+
                  |
                  v
      +------------------------+
      | 第 1 层：几何不变量     |  对齐、边界、活值不重叠、全覆盖
      +------------------------+
                  |
                  v
      +------------------------+
      | 第 2 层：arena 覆盖执行 |  按 offset 读写，模拟生成代码
      +------------------------+
                  |
                  v
      +------------------------+
      | 第 3 层：数值对比       |  与 evaluate_graph / C DLL 比较
      +------------------------+
                  |
                  v
              pass / fail
```

三层各有分工。第一层最快，能在毫秒级发现明显的分配错误；第二层能发现 liveness 与执行顺序的问题；第三层是最终判据，只有数值一致才算通过。

## 核心内容

### 1. 第一层：plan 的几何不变量

一份合法的 plan 至少满足以下条件：

- 每个 offset 是 `alignment` 的倍数。
- 每个块满足 `offset >= 0` 且 `offset + size <= arena_bytes`。
- `arena_bytes` 是 `alignment` 的倍数。
- 任意两个活值（区间相交）的字节区间不重叠。
- 每个有 producer 且不是输入/常量的值都有 allocation。
- 输入和常量不占 arena。
- 输出值的 end 是 `len(graph.ops)`。

`MemoryPlan.validate()` 覆盖 offset 对齐、边界和活值别名；`arena_bytes` 的对齐由 `plan_memory` 的收尾步骤保证。

```python
def validate(self) -> None:
    items = list(self.allocations.values())
    for i, a in enumerate(items):
        if a.offset % self.alignment != 0:
            raise IRError(f"{a.value.name}: offset {a.offset} is not aligned to {self.alignment}")
        if a.offset < 0 or a.end_byte > self.arena_bytes:
            raise IRError(f"{a.value.name}: allocation outside arena")
        for b in items[i + 1:]:
            live_overlap = a.start <= b.end and b.start <= a.end
            if live_overlap and a.overlaps(b):
                raise IRError(
                    f"live ranges overlap but buffers alias: {a.value.name} and {b.value.name}"
                )
```

实测把一个 offset 改成 8：

```text
IRError relu_0: offset 8 is not aligned to 64
```

改成越界偏移会得到 `allocation outside arena`。这些错误在规划阶段就能定位到值和偏移，修复成本低。

但 `validate()` 不检查覆盖。规划器如果漏掉一个值，`allocations` 里没有它，循环不会遍历到，也不会报错。代码生成时访问 `plan.offset_of(value)` 会抛 `KeyError`，错误信息不如显式检查清楚。补一个覆盖检查：

```python
def check_coverage(graph, plan):
    for v in graph.values:
        if v.kind in {"input", "constant"} or v.producer is None:
            continue
        if v not in plan.allocations:
            raise AssertionError(f"missing allocation for {v.name}")
    for v in graph.inputs:
        if v in plan.allocations:
            raise AssertionError(f"input {v.name} should not live in the arena")
```

这段检查把“漏分配”和“输入误分配”变成明确错误。它应该和 `validate()` 一起跑。

### 2. 第二层：在 arena 上覆盖执行

有了偏移，可以在一块真实的字节缓冲区上模拟生成的代码。思路是：

1. 分配 `plan.arena_bytes` 字节的 `bytearray` 或 NumPy 数组。
2. 输入和常量留在 arena 外。
3. 每个算子执行后，把结果写进 `arena[offset:offset+size]`。
4. 后续算子从对应的 offset 读。

下面是一个使用解释器的覆盖执行器。它把每个临时值的结果写进 arena，再以视图的形式放回环境：

```python
def execute_on_arena(graph, plan, feed):
    arena = np.zeros(plan.arena_bytes, dtype=np.uint8)
    env = {}
    for v in graph.values:
        if v.kind == "constant":
            env[v] = v.data.astype(np.float32)
    for v in graph.inputs:
        env[v] = feed[v.name].astype(np.float32)
    for op in graph.ops:
        out = apply_op(op.kind, [env[v] for v in op.inputs], op.attrs)
        off = plan.offset_of(op.output)
        view = np.frombuffer(
            arena, dtype=np.float32, count=out.size, offset=off
        )
        view[...] = out.reshape(-1)
        env[op.output] = view.reshape(out.shape)
    return [env[v].copy() for v in graph.outputs]
```

`np.frombuffer` 不复制数据，`view` 直接映射到 arena 的字节。`view[...] = out` 把结果写进去；后续算子读到的是同一块内存。这个执行器能发现“两个活值被分到同一 offset”导致的数值错误，因为后写的值会覆盖先写的值。

它有一个重要局限：`apply_op` 是 NumPy 的 out-of-place 实现。它先算出完整结果，再写进 arena。因此，即使输出和某个输入共享同一块内存，只要 NumPy 在写入前已经读完输入，结果仍然正确。真实的 C 代码是逐元素循环，读写交错，可能出错。要测 in-place 风险，需要标量执行器，下一节会给出。

### 3. 随机图压力测试

固定图的测试覆盖有限。随机图能覆盖不同的生命周期交错、不同的 shape 和不同的输出位置。一个可复现的生成器应该：

- 使用固定 seed。
- 保证至少有一个输出。
- 避免选出无法广播的 shape。
- 记录生成参数，失败时能重放。

下面是一个生成器的核心部分：

```python
def build(rng):
    g = Graph()
    vals = []
    for i in range(rng.randint(2, 4)):
        n = rng.randint(1, 6)
        vals.append(g.add_input(f"x{i}", (n,)))
    made = []
    for _ in range(rng.randint(3, 9)):
        kind = rng.choice(["relu", "add", "mul"])
        if kind == "relu" or len(vals) < 2:
            v = g.add_op("relu", [rng.choice(vals)])
        else:
            a = rng.choice(vals)
            b = rng.choice(vals)
            try:
                broadcast_shape(a.type.shape, b.type.shape)
            except IRError:
                continue
            v = g.add_op(kind, [a, b])
        vals.append(v)
        made.append(v)
    if not made:
        made.append(g.add_op("relu", [vals[0]]))
    for _ in range(rng.randint(1, 2)):
        g.add_output(rng.choice(made))
    return g
```

测试循环同时检查几何和数值：

```python
rng = random.Random(2026)
for case in range(300):
    g = build(rng)
    feed = {
        v.name: np.array(
            [rng.uniform(-2, 2) for _ in range(v.type.numel)],
            dtype=np.float32,
        ).reshape(v.type.shape)
        for v in g.inputs
    }
    ref = evaluate_graph(g, feed)
    plan = plan_memory(g)
    plan.validate()
    got = execute_on_arena(g, plan, feed)
    for x, y in zip(got, ref):
        np.testing.assert_allclose(x, y, atol=1e-6, rtol=1e-6)
```

实测结果：

```text
random graphs passed: 300/300, max arena_bytes: 384
```

300 张图全部通过，最大 arena 是 384 字节。这个数字不是性能指标，只是说明生成器覆盖了需要多块内存的图。用 seed 7 生成的一张样本 plan：
```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
relu_0               [  0,  2]     64       0
relu_1               [  1,  1]     64      64
```

这里 `relu_1` 的区间是 `[1, 1]`，说明它是一个没有用户、也不是输出的死值。`plan_memory` 仍然为它分配了 64 字节，因为规划器不判断死代码。压力测试里的图在规划前没有跑 DCE，所以会看到这种块。生产流水线应该先跑 `dce`，再用规划器；测试里保留死值，可以顺便验证规划器不会因为死值而崩溃。

### 4. 第二层的缺口：NumPy 覆盖执行测不到 in-place

考虑一张 matmul 图：

```text
t = matmul(A, B)   # op 0, 区间 [0, 2]
u = relu(t)        # op 1, 区间 [1, 1]
w = matmul(t, B)   # op 2, 区间 [2, 3]
output = w
```

正确的 plan 给 `t` 和 `w` 不同的 offset。现在人为破坏 liveness：把 `t` 的 end 改成 0，再把 `w` 的 offset 改成 `t` 的 offset。声明的区间变成：

```text
t  [0, 0]  offset 0
u  [1, 1]  offset 64
w  [2, 3]  offset 0
```

按声明区间看，`t` 和 `w` 不重叠，`validate()` 通过。实际执行时，`w` 在 op 2 读取 `t`，同时写 `t` 的缓冲区。用 NumPy 的 out-of-place 执行器，`t` 的完整结果在写之前已经算好，可能仍然正确。下面用标量执行器按 C 的循环顺序读写：

```python
def run_scalar(graph, plan, feed):
    arena = np.zeros(plan.arena_bytes // 4, dtype=np.float32)
    env = {}
    for v in graph.inputs:
        env[v] = np.asarray(feed[v.name], np.float32).reshape(-1)
    for v in graph.values:
        if v.kind == "constant":
            env[v] = np.asarray(v.data, np.float32).reshape(-1)
    for op in graph.ops:
        dst = arena[
            plan.offset_of(op.output) // 4:
            plan.offset_of(op.output) // 4 + op.output.type.numel
        ]
        ins = [env[v] for v in op.inputs]
        if op.kind == "relu":
            for i in range(dst.size):
                dst[i] = max(ins[0][i], np.float32(0.0))
        elif op.kind == "add":
            for i in range(dst.size):
                dst[i] = ins[0][i] + ins[1][i]
        elif op.kind == "matmul":
            m, k = op.inputs[0].type.shape
            n = op.inputs[1].type.shape[1]
            for i in range(m):
                for j in range(n):
                    acc = np.float32(0.0)
                    for kk in range(k):
                        acc += ins[0][i * k + kk] * ins[1][kk * n + j]
                    dst[i * n + j] = acc
        env[op.output] = dst
    return [env[v].copy().reshape(v.type.shape) for v in graph.outputs]
```

用 `A = [[1,2],[3,4]]`、`B = [[0,1],[1,0]]`，参考结果是 `[[1,2],[3,4]]`。强制别名后的实测输出：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
t                    [  0,  0]     64       0
w                    [  2,  3]     64       0
u                    [  1,  1]     64      64
ref:
 [[1. 2.]
 [3. 4.]]
aliased scalar run:
 [[1. 1.]
 [3. 3.]]
max_abs_error: 1.0
```

`validate()` 没有报错，标量执行器发现 max_abs_error 为 1.0。这个实验说明两件事：

1. 几何校验只信任声明的区间，liveness 错误可以绕过它。
2. 用 NumPy 的 out-of-place 语义做覆盖执行，测不到 in-place 风险；必须模拟生成的循环顺序，或者直接测编译后的 C。

### 5. 第三层：与 C DLL 做差分测试

最终判据是生成代码的输出。`runtime.run_c_graph` 加载 DLL，按 `graph.inputs` 的顺序传输入，按 `graph.outputs` 的顺序接收输出。测试模式是：

```python
ref = evaluate_graph(g, feed)[0]
result = compile_graph_c(g, build_dir)
out = run_c_graph(g, feed, result.dll_path)[0]
np.testing.assert_allclose(out, ref, atol=1e-6, rtol=1e-6)
```

demo 中的真实误差是 `4.470348358154297e-08`，远小于 `1e-6`。这个误差来自浮点累加顺序：NumPy 的 matmul 和 C 的 triple loop 不是同一条指令序列，结果不会逐位相同。容差要按算子链长度和数值范围设置，不能固定用 `1e-12`。

差分测试能抓到规划器、代码生成器和运行时三层的问题。代价是慢：每次都要写 C 文件、调用 `cl`、加载 DLL。随机图压力测试通常先用解释器层跑几百张，筛出可疑图，再对少量图编译 C 验证。

### 6. debug allocator：把错误变成异常

生产 arena 只做一次 `_aligned_malloc`，错误往往表现为数值异常或崩溃。debug allocator 在开发期替代它，主动检查：

- 写入是否超过块大小。
- 分配是否与已存在的块重叠。
- 释放后是否仍然读写。
- 块末尾的 canary 是否被覆盖。

下面是一个可运行的 Python 版本：

```python
import struct

class DebugArena:
    def __init__(self, nbytes):
        self.mem = bytearray([0xCD]) * nbytes
        self.blocks = {}
        self.canaries = {}
        self.freed = set()

    def alloc(self, name, offset, nbytes):
        for other, (o, n, _) in self.blocks.items():
            if offset < o + n and o < offset + nbytes:
                raise RuntimeError(f"alias: {name} overlaps {other}")
        self.blocks[name] = (offset, nbytes, False)
        self.canaries[name] = offset + nbytes
        struct.pack_into("<I", self.mem, offset + nbytes, 0xDEADBEEF)

    def write(self, name, values):
        if name in self.freed:
            raise RuntimeError(f"use-after-free: {name}")
        off, size, _ = self.blocks[name]
        if len(values) * 4 > size:
            raise RuntimeError(
                f"overflow: {name} needs {len(values) * 4} bytes, has {size}"
            )
        for i, v in enumerate(values):
            struct.pack_into("<f", self.mem, off + 4 * i, v)

    def free(self, name):
        off, size, _ = self.blocks[name]
        for i in range(off, off + size):
            self.mem[i] = 0xDD
        self.freed.add(name)

    def check(self, name):
        return struct.unpack_from("<I", self.mem, self.canaries[name])[0] == 0xDEADBEEF
```

实际运行三类错误：

```python
def expect(label, fn):
    try:
        fn()
    except RuntimeError as e:
        print(label + ":", e)

a = DebugArena(256)
a.alloc("relu_0", 0, 64)
a.alloc("add_1", 128, 64)
print("canaries:", a.check("relu_0"), a.check("add_1"))
expect("overflow", lambda: a.write("relu_0", [1.0] * 17))
expect("alias", lambda: a.alloc("bad", 32, 64))
a.free("relu_0")
expect("uaf", lambda: a.write("relu_0", [1.0]))
print("canary after legal writes:", a.check("add_1"))
```

输出：

```text
canaries: True True
overflow: overflow: relu_0 needs 68 bytes, has 64
alias: alias: bad overlaps relu_0
uaf: use-after-free: relu_0
canary after legal writes: True
```

三条错误分别对应块太小、块重叠和释放后写入；输出里的 `overflow`、`alias`、`uaf` 标签直接指出类别。

### 7. 失效注入：验证测试本身有效

一个好的测试不仅要通过，还要在注入错误时失败。至少做三种失效注入：

1. 把某个 offset 加 8，期望 `validate()` 报对齐错误。
2. 把释放条件改成 `end <= start`，期望 `validate()` 报活值别名。
3. 把某个值的 end 提前，期望几何校验通过但数值对比失败。

第三种最重要。它证明测试能抓到“合法几何 + 错误 liveness”的组合。本章的 matmul 强制别名实验就是第三种：`validate()` 放行，标量执行器发现误差 1.0。

### 8. 测试矩阵

| 测试 | 检查对象 | 能发现 | 不能发现 | 运行成本 |
| --- | --- | --- | --- | --- |
| `plan.validate()` | 对齐、边界、声明区间别名 | 明显的分配错误 | 漏分配、liveness 错误 | 极低 |
| 覆盖率检查 | 每个临时值都有块 | 漏分配、输入误分配 | 数值错误 | 极低 |
| NumPy 覆盖执行 | 规划后的读写位置 | 活值覆盖、偏移错误 | in-place 风险 | 低 |
| 标量执行器 | C 顺序的读写 | in-place、别名、liveness 错误 | 编译器优化问题 | 中 |
| debug allocator | 越界、重叠、UAF | 内存访问错误 | 逻辑错误 | 中 |
| 随机图压力测试 | 大量生命周期组合 | 规划器边界情况 | 特定 shape 的遗漏 | 中 |
| C DLL 差分测试 | 最终生成代码 | 代码生成、ABI、数值问题 | 极端输入的性能 | 高 |

## 底层机制

### 为什么几何校验是 O(n^2)

`validate()` 对每对 allocation 检查一次，复杂度是 O(n^2)。对 100 个临时值是 4950 次比较，对 1000 个是 499500 次。mini 编译器的图很小，这个成本可以接受。生产级规划器会按 offset 排序后扫描，或者用区间树，把检查降到 O(n log n)。

### 为什么浮点对比要用容差

浮点加法和乘法不满足结合律。NumPy 的 matmul 可能使用分块、FMA 或不同的累加顺序；C 的 triple loop 按固定顺序累加。两者在数学上等价，在二进制上不等价。`np.testing.assert_allclose` 的 `atol` 和 `rtol` 要覆盖这种差异。demo 的 MLP 误差约 `4.47e-08`，设置 `1e-6` 有足够余量。

## 进一步用法与变体

### 属性测试与 fuzzing

随机图生成器是属性测试的简化版。更系统的做法是用 Hypothesis 生成图结构，声明属性：

- 规划后的 arena 字节数不少于峰值。
- 任意两个活值的字节区间不重叠。
- 覆盖执行的输出与解释器一致。
- DCE 后的图与 DCE 前的图输出一致。

Hypothesis 会自动缩小失败样例，把一张大图缩减到最小的反例。实现成本比手写生成器高，定位效率也更高。

## 对比与取舍

| 方案 | 检测能力 | 性能开销 | 实现成本 | 适用阶段 |
| --- | --- | --- | --- | --- |
| plan.validate | 几何错误 | 无 | 低 | 每次规划 |
| canary | 块末尾越界写 | 低 | 低 | 调试构建 |
| debug allocator | 越界、重叠、UAF | 高 | 中 | 单元测试 |
| 随机图 + 解释器 | 规划与语义错误 | 中 | 中 | CI |
| 标量执行器 | in-place、读写顺序 | 中 | 中 | 别名相关改动 |
| C DLL 差分 | 最终代码 | 高 | 高 | 发布前 |

## 踩雷集锦

### 1. 只跑 `validate()` 就认为内存正确

`validate()` 检查的是声明，不是事实。liveness 算错、漏分配、代码生成写错偏移，它都可能放行。至少要加覆盖执行和数值对比。

### 2. 用 NumPy 覆盖执行测试 in-place

`apply_op` 先算完整结果再写入，掩盖了逐元素读写交错的问题。要测 in-place，必须用标量执行器模拟 C 的循环顺序，或者直接编译 C 运行。

### 3. 随机图不固定 seed

失败无法复现，调试只能靠猜。固定 seed，并记录 case 编号和生成参数。失败时把图 dump 出来，作为回归测试。

## 进阶

1. 给随机图生成器加 shape 广播、`sum` 和 `matmul`，覆盖更多算子。每加一个算子，先确认解释器支持，再加入生成器。
2. 实现一个缩小器：当某张图失败时，逐步删除算子或输入，保留仍然失败的最小图。

## 动手练习

1. 运行本章的 300 张随机图测试，把 seed 改成 2027，记录通过数量和最大 arena。
2. 在 `check_coverage` 中增加“每个 allocation 的 value 属于 graph”的检查，构造一个外部 Value 验证它会报错。
3. 复现 matmul 强制别名实验，确认 `validate()` 通过而标量执行器报错。把 `t.end` 改回正确值，观察误差消失。
4. 给 `DebugArena` 增加 `read(name)`，在释放后读取时抛 `use-after-free`。写一个测试覆盖它。

## 本章重点

- `validate()` 只检查几何不变量，不检查覆盖、liveness 正确性和数值语义。
- 覆盖率检查要确认每个临时值都有 allocation，输入和常量不在 arena。
- arena 覆盖执行能发现活值覆盖和偏移错误，但 NumPy 的 out-of-place 语义测不到 in-place。
- 标量执行器按 C 的循环顺序读写，能发现强制别名导致的数值错误。
- 300 张随机图全部通过，最大 arena 384 字节；随机图要固定 seed 并可复现。
- debug allocator 把越界、重叠和 use-after-free 变成明确异常。
- 最终判据是与解释器或 C DLL 的数值对比，容差要合理。

## 自我检核

- [ ] 我能列出 plan 至少五条几何不变量。
- [ ] 我能写出覆盖检查，发现漏分配和输入误分配。
- [ ] 我能解释为什么 NumPy 覆盖执行测不到 in-place。
- [ ] 我能用标量执行器复现 max_abs_error 为 1.0 的强制别名错误。

## 延伸阅读

- **《Engineering a Compiler》第 13 章**：读寄存器分配的冲突检查。学“同时活跃不能共享”的判定；本章的 `validate()` 是它在字节层面的实现。
- **AddressSanitizer 官方文档**：读 heap-buffer-overflow、use-after-free 和 redzone 的实现。学工业级工具如何在不改代码的情况下检测越界；对照本章的 debug allocator。
- **Hypothesis 文档的 stateful testing 章节**：读如何生成有状态的测试序列。学自动缩小失败样例；本章的随机图生成器可以升级成 Hypothesis 策略。
- **TVM `tests/python/unittest/test_tir_transform_storage_rewrite.py`**：读存储重写的测试用例。学生产编译器如何构造复用、in-place 和别名场景；对照本章的随机图。

---

下一章：[练习 C：实现内存规划器](./practice-c-memory-planner.md)
