# 第 12 章 生命周期分析：谁在什么时候还活着

> **目标**：读懂 `plan_memory` 的生命周期（liveness）建模：`last_use` 怎么算、区间为什么写成闭区间 `[start, end]`、输出值为什么必须 `end = len(ops)`、复用条件为什么是严格小于。用两个可复现的错误实验（同 op 复用、输出未钉住）证明：生命周期算错不会报错，只会给出错误数值。

> **环境**：Windows 10/11 x86_64，Python 3.11.12，numpy 2.4.6，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

两个错误实验要编译 DLL，需要 MSVC；`code/mini_ai/codegen_c.py` 会自动查找 `vcvars64.bat`。

## 为什么需要这个？

内存规划（memory planning）要回答一个问题：两个临时张量能不能共用同一块 buffer。答案完全取决于“它们的生命周期有没有重叠”。判断错了，有两类后果：

1. **该复用却没复用**：arena 变大，性能变差，但结果正确。属于浪费。
2. **不该复用却复用了**：一个 op 在写输出时把另一个 op 还要读的输入覆盖掉。属于错误，而且**不会崩溃**。

第 2 类才是这一章的重点。本章会实测两次：

- 把复用条件从 `end < start` 放宽成 `end <= start`，两个 matmul 链的结果最大误差 `28.9524`；
- 去掉“输出值 `end = len(ops)`”这条规则，两个输出返回完全相同的数值，而 `MemoryPlan.validate()` 依然通过。

两次都不抛异常、不越界、不崩。这就是为什么生命周期必须单独成章：它是 第 13 章 （规划器）、第 14 章 （in-place 与别名安全）、第 15 章 （内存正确性验证）共同的地基。

## 先建立直觉：每个值有一条时间线，op 是时间刻度

```text
op index:      0          1          2          3          4
               │          │          │          │          │
a = relu(x)    [==========]                                        a: [0, 2]
b = relu(y)               [==========]                             b: [1, 2]
c = add(a, b)                        [=================]           c: [2, 4]
d = relu(z)                                     [==========]       d: [3, 4]
out = add(c,d)                                             [=====] out: [4, 5]

可复用判定：旧的 end < 新的 start
  a[0,2] 与 d[3,4]：2 < 3  →  可以共用 buffer
  b[1,2] 与 out[4,5]：2 < 4 →  可以共用 buffer
  a[0,2] 与 b[1,2]：2 < 1 不成立 → 不能共用
```

闭区间 `[start, end]` 的含义是：**在 `start` 这一刻产生，在 `end` 这一刻仍然被读**。所以 `a[0, 2]` 表示它在 op 2（`add(a, b)`）里还被读，op 2 结束之后才死。

注意最后的 `out`：它是图输出，`end` 被强行设成 `len(ops) = 5`。这不是“它被 op 5 读”，而是“它必须活到所有 op 结束”，因为输出是在所有计算完成后才被拷贝出去的。

## 核心概念一：`last_use` 是“最后一次被读”的 op 下标

`plan_memory` 的第一段计算所有值的最后使用点：

```python
    graph.validate()
    graph.recompute_users()
    op_index = {id(op): i for i, op in enumerate(graph.ops)}
    last_use: dict[Value, int] = {}

    for i, op in enumerate(graph.ops):
        for v in op.inputs:
            last_use[v] = max(last_use.get(v, i), i)
```

`max` 是必要的：一个值可能被多个 op 读，最后一次才是它真正死掉的位置。累加器（例如循环里读多次）在图级 IR 里表现为多个用户，取最大即可。

第二段补上“产生位置”和“输出钉住”：

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

三件事：

1. `input` 与 `constant` 被跳过。它们不在 arena 里（输入指针由调用方给，常量内嵌在 C 代码里），所以没有“生命周期”可言。
2. `start` 是生产者 op 的下标。一个值从它被算出来的那一刻开始占用存储。
3. 没有用户的值 `end = start`（`last_use.get(v, start)` 的默认值），生命周期退化成一个点。这种值通常是死代码，DCE 应该先把它清掉。

同一份数据之后又被排序成候选列表：

```python
    candidates: list[tuple[int, int, Value]] = []
    for v in graph.values:
        if v.kind in {"input", "constant"} or v.producer is None:
            continue
        start = op_index[id(v.producer)]
        end = last_use.get(v, start)
        candidates.append((start, end, v))
    candidates.sort(key=lambda x: (x[0], x[1], x[2].name))
```

按 `start` 升序，就是线性扫描分配器（linear-scan allocator）的输入顺序。`end` 与 `name` 是并列的稳定排序键：`start` 相同的值按“谁先死谁先排”，同 `start` 同 `end` 时按名字，保证同一张图每次跑出的 offset 完全一致（可复现性）。

## 核心概念二：输出值必须钉到 `len(ops)`

`if v in graph.outputs: end = len(graph.ops)` 这一行只有一句，但删掉它就会得到错误答案。下面这段脚本复制了 `plan_memory` 的分配逻辑，只去掉这一行：

```python
import numpy as np
from pathlib import Path
from mini_ai.ir import Graph
from mini_ai.memory import Allocation, MemoryPlan, align_up, plan_memory
from mini_ai.interpreter import evaluate_graph, max_abs_error
from mini_ai.codegen_c import generate_c, compile_c
from mini_ai.runtime import run_c_graph


def plan_without_output_pin(graph, alignment=64):
    """plan_memory 的副本，唯一区别是没有把输出 end 钉到 len(ops)。"""
    graph.validate()
    graph.recompute_users()
    op_index = {id(op): i for i, op in enumerate(graph.ops)}
    last_use = {}
    for i, op in enumerate(graph.ops):
        for v in op.inputs:
            last_use[v] = max(last_use.get(v, i), i)
    candidates = []
    for v in graph.values:
        if v.kind in {"input", "constant"} or v.producer is None:
            continue
        start = op_index[id(v.producer)]
        end = last_use.get(v, start)          # <-- 没有输出钉住
        candidates.append((start, end, v))
    candidates.sort(key=lambda x: (x[0], x[1], x[2].name))

    plan = MemoryPlan(graph, alignment)
    free_blocks, active, arena = [], [], 0
    for start, end, value in candidates:
        still = []
        for alloc in active:
            if alloc.end < start:
                free_blocks.append((alloc.offset, alloc.size))
            else:
                still.append(alloc)
        active = still
        need = max(alignment, align_up(value.type.numel * 4, alignment))
        free_blocks.sort(key=lambda x: (x[1], x[0]))
        chosen = None
        for i, (off, size) in enumerate(free_blocks):
            if size >= need:
                chosen = i
                break
        if chosen is None:
            offset = align_up(arena, alignment)
            arena = offset + need
        else:
            offset, size = free_blocks.pop(chosen)
            if size > need:
                free_blocks.append((offset + need, size - need))
        alloc = Allocation(value=value, start=start, end=end, size=need, offset=offset)
        plan.add(alloc)
        active.append(alloc)
        plan.peak_live_bytes = max(plan.peak_live_bytes, sum(a.size for a in active))
    plan.arena_bytes = max(alignment, align_up(arena, alignment))
    return plan


g = Graph()
x = g.add_input("x", (2, 3))
a = g.add_op("relu", [x], name="a")
w = g.add_const("w", (np.arange(9, dtype=np.float32).reshape(3, 3) + 1) / 9)
b = g.add_op("matmul", [x, w], name="b")
g.add_output(a, "act")
g.add_output(b, "proj")
feed = {"x": np.array([[-1, 2, 3], [4, -5, 6]], np.float32)}

good = plan_memory(g)
bad = plan_without_output_pin(g)
print("=== correct plan ===")
print(good.summary())
print("=== plan without output pinning ===")
print(bad.summary())
bad.validate()
print("bad plan validate: passed")

d = Path(".scratch/mini-ai-tests")
r_good = compile_c(generate_c(g, good), d / "outpin_ok", name="outpin_ok")
r_bad = compile_c(generate_c(g, bad), d / "outpin_bad", name="outpin_bad")
o_good = run_c_graph(g, feed, r_good.dll_path)
o_bad = run_c_graph(g, feed, r_bad.dll_path)
ref = evaluate_graph(g, feed)
print("good:", [o.tolist() for o in o_good], "err:", [max_abs_error(o, r) for o, r in zip(o_good, ref)])
print("bad :", [o.tolist() for o in o_bad], "err:", [max_abs_error(o, r) for o, r in zip(o_bad, ref)])
```

真实输出：

```text
=== correct plan ===
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
act                  [  0,  2]     64       0
proj                 [  1,  2]     64      64
=== plan without output pinning ===
arena_bytes=64 alignment=64 peak_live_bytes=64
value                 interval     size   offset
act                  [  0,  0]     64       0
proj                 [  1,  1]     64       0
bad plan validate: passed
good: [[[0.0, 2.0, 3.0], [4.0, 0.0, 6.0]], [[3.1111111640930176, 3.555555820465088, 4.0], [2.8888890743255615, 3.444444417953491, 4.0]]] err: [0.0, 2.384185791015625e-07]
bad : [[[3.1111111640930176, 3.555555820465088, 4.0], [2.8888890743255615, 3.444444417953491, 4.0]], [[3.1111111640930176, 3.555555820465088, 4.0], [2.8888890743255615, 3.444444417953491, 4.0]]] err: [3.444444417953491, 2.384185791015625e-07]
```

三个结果要一起看（末几位数字取决于 MSVC 的规约顺序与版本；offset、区间与“两个输出完全相同”这几条结构事实不受影响）：

1. 没有钉住时，`act` 的区间变成 `[0, 0]`，`proj` 是 `[1, 1]`，两者 offset 都是 0 —— 分配器认为 `act` 在 op 0 就死了，op 1 可以覆盖它。
2. `validate()` **通过**。因为按它记录的区间，两者确实不重叠（`0 <= 1` 与 `1 <= 0` 不会同时成立）。校验器检查的是“区间与 offset 是否自洽”，它无法判断区间本身对不对。
3. 两个输出返回完全相同的值，误差 `3.4444`。原因是 C 后端在最后统一拷贝输出，此时 `act` 的 buffer 已经被 `proj` 覆盖。

这条实验的意义：**错误的生命周期信息会通过所有自洽性检查**。校验器只能保证“你没有违反你自己声明的规则”，不能保证“你的规则是对的”。后者要靠测试用例与差分测试。

## 核心概念三：同一个 op 内不能复用

复用条件是严格小于：

```python
        # Free only buffers whose last use is strictly before this op.
        still_active: list[Allocation] = []
        for alloc in active:
            if alloc.end < start:
                free_block(alloc.offset, alloc.size)
            else:
                still_active.append(alloc)
        active = still_active
```

为什么不能用 `<=`？因为 `end == start` 意味着“旧值在这条 op 里还被读，新值也在这条 op 里被写”。一个 kernel 内部的读写是交错的：`matmul` 在内层循环里反复读输入，同时把累加结果写出去；`sum` 在循环里读 `x`、累加到 `out`。覆盖会直接改掉还没读完的数据。

最短的例子是 `relu(relu(x))`：

```python
from mini_ai.ir import Graph
from mini_ai.memory import plan_memory

g = Graph()
x = g.add_input("x", (8,))
a = g.add_op("relu", [x], name="a")
b = g.add_op("relu", [a], name="b")
g.add_output(b)
plan = plan_memory(g)
print(plan.summary())
print("a offset:", plan.offset_of(a), "b offset:", plan.offset_of(b))
```

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
a                    [  0,  1]     64       0
b                    [  1,  2]     64      64
a offset: 0 b offset: 64
```

`a.end = 1`，`b.start = 1`，不满足 `1 < 1`，所以两块 buffer 分开。arena 因此是 128 字节，`peak_live_bytes` 也是 128 —— 在 op 1 这一刻，`a` 正在被读、`b` 正在被写，两者同时存活。

放宽成 `<=` 的后果，用 matmul 链看得最清楚：

```python
import numpy as np
from pathlib import Path
from mini_ai.ir import Graph
from mini_ai.memory import plan_memory
from mini_ai.interpreter import evaluate_graph, max_abs_error
from mini_ai.codegen_c import generate_c, compile_c
from mini_ai.runtime import run_c_graph

g = Graph()
x = g.add_input("x", (2, 3))
w1 = g.add_const("w1", (np.arange(9, dtype=np.float32).reshape(3, 3) + 1) / 9)
w2 = g.add_const("w2", (np.arange(9, dtype=np.float32).reshape(3, 3) + 2) / 7)
c = g.add_op("matmul", [x, w1], name="c")
d = g.add_op("matmul", [c, w2], name="d")
g.add_output(d)
feed = {"x": np.array([[1, 2, 3], [4, 5, 6]], np.float32)}
ref = evaluate_graph(g, feed)[0]

plan = plan_memory(g)
print(plan.summary())
good = compile_c(generate_c(g, plan), Path(".scratch/mini-ai-tests") / "alias_ok", name="alias_ok")
print("correct out:", run_c_graph(g, feed, good.dll_path)[0].tolist())

# 手工制造一次错误复用：d 直接用 c 的 buffer
plan.allocations[d].offset = plan.allocations[c].offset
plan.allocations[d].size = plan.allocations[c].size
try:
    plan.validate()
except Exception as exc:
    print("validate:", type(exc).__name__, exc)
bad = compile_c(generate_c(g, plan), Path(".scratch/mini-ai-tests") / "alias_bad", name="alias_bad")
out = run_c_graph(g, feed, bad.dll_path)[0]
print("aliased out:", out.tolist())
print("max err vs interpreter:", max_abs_error(out, ref))
```

真实输出：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
c                    [  0,  1]     64       0
d                    [  1,  2]     64      64
correct out: [[9.142858505249023, 10.85714340209961, 12.571430206298828], [20.71428680419922, 24.571430206298828, 28.428573608398438]]
validate: IRError live ranges overlap but buffers alias: c and d
aliased out: [[9.142858505249023, 13.346939086914062, 25.23809814453125], [20.71428680419922, 30.30612564086914, 57.380958557128906]]
max err vs interpreter: 28.9523868560791
```

`validate()` 这次抓到了：`c[0,1]` 与 `d[1,2]` 重叠，且 offset 相同，报 `live ranges overlap but buffers alias`。但注意 `generate_c` 并不调用 `plan.validate()`，所以我照样生成了错误代码并跑出结果 —— 误差 `28.95`。

这个例子说明校验器与代码生成之间没有强制绑定。生产实现里应该在 `CCodegen.__init__` 里调用 `plan.validate()`，本仓库把它留给调用方。

## 核心概念四：一次完整的规划

拿 demo 的 MLP（`Linear(8,16) -> ReLU -> Linear(16,4)`，输入 `(4, 8)`）跑完 `standard_pipeline` 后：

```text
arena_bytes=320 alignment=64 peak_live_bytes=320
value                 interval     size   offset
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

两处细节：

- `relu_fused` 的 256 字节 = `4 * 16 * 4`，第一个 matmul 的输出 `(4, 16)` 占满。
- `linear_1_fused` 是输出，`end = len(ops) = 2`，所以它的 buffer 在第 1 条 op 之后不会被任何东西复用；arena 恰好等于 `256 + 64 = 320`，与峰值相同。

融合把 5 条 op 压成 2 条，也把 5 个中间值压成 2 个。生命周期分析跑在融合之后，所以它看到的是“融合后的图”。如果反过来先规划内存再融合，会得到一份包含大量已死 buffer 的计划 —— 数值仍然正确，arena 却翻倍。

## 底层机制：best-fit、对齐与两个“峰值”

分配循环是“先释放、再找洞、找不到就长”：

```python
        need = max(alignment, align_up(value.type.numel * 4, alignment))
        free_blocks.sort(key=lambda x: (x[1], x[0]))
        chosen = None
        for i, (off, size) in enumerate(free_blocks):
            if size >= need:
                chosen = i
                break
        if chosen is None:
            offset = align_up(arena_bytes, alignment)
            arena_bytes = offset + need
        else:
            offset, size = free_blocks.pop(chosen)
            if size > need:
                free_blocks.append((offset + need, size - need))
```

- `need` 至少是 `alignment`（默认 64），即使张量只有 4 字节。这是为了让每个 buffer 的起点都对齐，方便后续向量化。
- `free_blocks` 按 `(size, offset)` 排序，取第一个足够大的块 —— 这是 best-fit（按大小找最贴合的洞），不是 first-fit。注释没有强调这一点，读代码时要按实现走；第 13 章会对比两种策略的差别。
- 找到的洞如果比需求大，剩余部分会以新块的形式放回。

`peak_live_bytes` 的更新方式是：

```python
        plan.peak_live_bytes = max(plan.peak_live_bytes, sum(a.size for a in active))
```

它统计的是**当前活跃分配的 size 之和**，不是 offset 的跨度。于是会出现 `arena_bytes > peak_live_bytes` 的情况。下图有两条入口：`x (16,) -> a = relu(x) -> s = sum(a)` 与 `y (32,) -> out = relu(y)`，`s` 与 `out` 都是输出：

```text
arena_bytes=256 alignment=64 peak_live_bytes=192
value                 interval     size   offset
a                    [  0,  1]     64       0
s                    [  1,  3]     64      64
out                  [  2,  3]    128     128
```

`a` 死后留下一个 64 字节的洞，但 `out` 需要 128 字节，装不进去，只能在 128 处新开一块。峰值活跃是 `64 + 128 = 192`，arena 却是 256。**arena 是“地址空间跨度”，峰值是“同时活着的字节数”**，两个数字不能互相替代。第 13 章会用这两个数字讨论碎片率。

`validate()` 用区间与 offset 一起判断冲突：

```python
            for b in items[i + 1 :]:
                live_overlap = a.start <= b.end and b.start <= a.end
                if live_overlap and a.overlaps(b):
                    raise IRError(
                        f"live ranges overlap but buffers alias: {a.value.name} and {b.value.name}"
                    )
```

两个条件都要成立才算冲突：区间重叠（`a.start <= b.end and b.start <= a.end`，闭区间的标准重叠判定）**并且**字节区间重叠（`offset < other.end_byte and other.offset < self.end_byte`）。区间不重叠时，两块 buffer 重叠也合法 —— 这正是复用的定义。

## 进一步用法与变体

**变体 1：半开区间。** 把 `[start, end]` 改成 `[start, end)`，规则随之变化：区间重叠判定变成 `a.start < b.end and b.start < a.end`，复用条件变成 `a.end <= b.start`。两种写法等价，但混用会让“同一 op 内不能复用”这条规则表达得非常别扭。选一种，全局统一。

**变体 2：允许 in-place。** 把某个值的 `end` 减 1（表示“最后一个 op 不再需要它”），它就能和那条 op 的输出共用 buffer。前提是那条 op 满足别名安全条件：逐元素、同 shape、读写位置一一对应（`relu`、`add(x, c)` 可以，`matmul`、`sum` 不行）。第 14 章 展开这部分。

**变体 3：跨 op 的融合副作用。** 融合会减少值的数量，从而直接降低 arena。但它也可能让某个值“活得更久”（例如融合 op 把两个输入的读取推迟到同一时刻）。规划器不关心原因，只看图是否正确。

**变体 4：把生命周期用于其他分析。** 同一份 `last_use` 可以驱动：buffer 复用（本章）、in-place 判定、梯度检查点的重计算决策、以及第 15 章的压力测试生成器（随机图 + 随机复用，检查数值）。

## 对比与取舍

| 方案 | 复用率 | 复杂度 | 适用场景 |
|---|---|---|---|
| 不复用（每个值一块） | 0 | 最低 | 调试、对照基线 |
| 线性扫描 + best-fit（本章） | 中高 | 低 | 静态图、拓扑序固定 |
| 图着色 / 区间图着色 | 高 | 中 | 生命周期已知、追求最小 arena |
| 整数规划 | 最高 | 高 | 编译期预算充足 |

再看两个“峰值”的取舍：

| 指标 | 含义 | 用途 |
|---|---|---|
| `peak_live_bytes` | 同时活跃的字节数 | 判断算法本身需要多少并行存储 |
| `arena_bytes` | 地址空间跨度（含碎片） | 决定实际要分配多少 |
| 二者之差 | 碎片 | 换分配策略的收益上限 |

`peak_live_bytes` 是理论下界（任意分配策略都无法低于它），`arena_bytes` 是当前策略的实际值。两者差距越大，说明分配策略越值得改。

## 踩雷集锦

1. **把复用条件写成 `end <= start`**：同一 op 里“读旧值、写新值”会互相覆盖。本章实测 matmul 链误差 `28.9524`，`validate()` 能抓到，但 `generate_c` 不会替你调用它。
2. **忘了给输出钉 `end = len(ops)`**：输出会在后续 op 里被复用，最后统一拷贝时读到的是被别人覆盖后的数据。本章实测两个输出完全相同，误差 `3.4444`，而且 `validate()` 通过。
3. **把输入与常量也算进 arena**：它们的存储由调用方或 C 代码提供。算进去会让 arena 虚增，还会让 `_ptr` 的错误更隐蔽（本该用 `x0` 却指向 arena 里的副本）。
4. **以为 `peak_live_bytes == arena_bytes`**：本章实测过 `256` 对 `192`。碎片、对齐、大块装不进小洞，都会让 arena 更大。
5. **对齐只在 arena 起点做一次**：每个 offset 都必须 `align_up`，包括从空洞里切出来的剩余块。否则某个 `(N,)` buffer 的起点可能只有 4 字节对齐，向量化代码会崩或变慢。

## 进阶：再往深一层

- **生命周期与 SSA**：本 IR 是 SSA，每个值只定义一次，所以“区间”足以描述生命周期。如果允许重定义（非 SSA），就需要引入 live range splitting，区间会变成多段。
- **别名分析**：区间不重叠只是复用的必要条件。真正的 in-place 还要证明“读的位置与写的位置不冲突”，这需要形状与 stride 信息（第 11 章）。`matmul` 的 `c = a @ b` 里 `c` 与 `a` 形状相同，但读写位置交叉，不能 in-place。
- **压力测试**：随机生成图、随机打乱 op 顺序（保持拓扑序）、随机选择分配策略，然后比对解释器结果。这是第 15 章的方法，也是发现“区间算错”最有效的手段。
- **源码对照**：读 `code/mini_ai/memory.py` 全文（约 150 行），重点看 `plan_memory` 的两段循环与 `MemoryPlan.validate` 的双重判定。

## 动手练习

### 练习 1：手算三组区间

对下面的图，先手写每个值的 `[start, end]`，再跑 `plan_memory` 验证：

```text
x, y 是输入
a = relu(x)
b = relu(a)
c = add(a, b)
d = relu(c)
out = add(c, d)     # 输出
```

要求写出哪些值能复用、arena 与峰值各是多少。提示：`a` 被 `b` 和 `c` 读，`c` 被 `d` 和 `out` 读。

### 练习 2：把 `<=` 的错误复现出来

把 `if alloc.end < start:` 改成 `<=`，跑本章的 matmul 链，记录 `plan.summary()` 与 `max_abs_error`。然后回答：为什么 `validate()` 这次会抛异常，而“输出未钉住”的实验里它通过了？

### 练习 3：给输出加“最后一个消费者”

修改 `plan_memory`，让输出值的 `end` 不是 `len(ops)`，而是“最后一个读它的 op 的下标”，但同时在 `plan.summary()` 里打印一条警告。用本章的 `act/proj` 例子证明：这样改会让两个输出相同。这个练习的目的是让你亲手确认“钉住”是必需的，而不是过度保守。

### 练习 4：测量碎片

构造 5 张不同形状组合的图（大小 buffer 交替），对每张图打印 `arena_bytes` 与 `peak_live_bytes`，计算 `arena / peak`。找出比值最大的一张，解释是哪个大块装不进哪个小洞。

## 本章重点

- 生命周期用闭区间 `[start, end]` 表示：`start` 是生产者 op 下标，`end` 是最后一次被读的 op 下标。
- 输入与常量不参与 arena 分配；没有用户的值区间退化成一个点。
- 输出值必须 `end = len(ops)`，否则会在后续 op 里被复用，导致最后拷贝读到脏数据。
- 复用条件是严格 `end < start`；相等意味着“同一条 op 内既读又写”，覆盖会改答案。
- 分配器是“先释放、再 best-fit、找不到就长”，`need` 至少一个 `alignment`。
- `arena_bytes` 是地址空间跨度，`peak_live_bytes` 是同时活跃字节数，两者之差是碎片。
- `validate()` 只能检查“区间与 offset 是否自洽”，不能检查“区间本身对不对”。

## 自我检核

- [ ] 我能对任意一张图手算每个值的 `[start, end]`
- [ ] 我能解释为什么输出值要钉到 `len(ops)`，并复现不钉住时的错误输出
- [ ] 我能解释 `end < start` 与 `end <= start` 的区别，以及为什么前者是必须的
- [ ] 我知道 `input` 与 `constant` 为什么不在 arena 里
- [ ] 我能构造一个 `arena_bytes > peak_live_bytes` 的图并解释原因
- [ ] 我能说出 `validate()` 在“输出未钉住”的实验里为什么没有报错
- [ ] 我能描述线性扫描分配器的释放与 best-fit 流程

## 延伸阅读

- **`code/mini_ai/memory.py`**：通读 `plan_memory` 与 `MemoryPlan.validate`。与本章的关联：所有区间与分配规则的原始实现。
- **`code/tests/test_mini_ai.py` 的 `TestMemory`**：读 `test_reuse_dead_buffer` 与 `test_no_alias_for_live_values`。与本章的关联：复用与不复用两条最小断言，可以照着扩展。
- **Engineering a Compiler（Cooper & Torczon）第 13 章 “Register Allocation”**：读 live range 与 interference graph 的定义。与本章的关联：寄存器分配与 buffer 复用是同一个区间重叠问题，只是粒度不同。
- **TVM `tir.transform.StorageRewrite`**（https://tvm.apache.org/docs/）：读它如何用 liveness 合并 buffer。与本章的关联：工业实现里同样的“区间 + 复用”思路，以及它对 in-place 的额外检查。
- **`compilers/ai_compiler/10-memory-planning.md`**：读 liveness、in-place、峰值三节。与本章的关联：同一组概念在 PyTorch 侧的可观测现象（RSS、缓存分配器）。

---

下一章：[第 13 章 内存规划：从活跃区间到 arena 偏移](./13-memory-planner.md)