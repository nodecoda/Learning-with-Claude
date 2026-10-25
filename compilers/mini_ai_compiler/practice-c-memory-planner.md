# 练习 C：实现内存规划器

> **目标**：从零实现一个内存规划器，输入一张已经通过校验的 `Graph`，输出每个临时值的 64 字节对齐 offset、arena 总字节数、峰值活值字节数；实现几何校验和覆盖率检查；用固定 seed 的随机图压力测试证明规划器不会让活值别名，也不会让数值出错。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"`、`$env:PYTHONIOENCODING = "utf-8"`，使用 `.scratch\aic-env\Scripts\python.exe` 运行。建议把练习文件放在 `.scratch\practice-c\planner.py`，不要改课程代码。

## 背景

第 12 章算出了每个值的活跃区间，第 13 章读了 `plan_memory` 的实现，第 14 章讨论了 arena 与别名安全，第 15 章建立了三层验证。现在把这些部分合成一个可以独立运行的规划器。

你实现的不是代码生成器，也不是 liveness 分析器。liveness 可以直接复用课程 IR 的 `producer`、`users` 和算子顺序，但规划、校验、输出格式和压力测试要自己完成。目标是让规划结果与课程实现一致，并能解释每一块内存为什么在那里。

练习不要求实现 in-place。相反，要求你显式禁止同一个 op 内的输入输出复用，并用测试证明这条规则没有被破坏。把 in-place 留到延伸挑战，避免在基础实现里引入算子级证明。

## 任务规格

### 必须实现的接口

在 `planner.py` 中实现：

```python
def align_up(n: int, alignment: int) -> int: ...
def plan_memory(graph: Graph, *, alignment: int = 64) -> Plan: ...
```

`Plan` 至少提供：

- `arena_bytes: int`
- `peak_live_bytes: int`
- `alignment: int`
- `slots: dict[Value, Slot]`
- `offset_of(value) -> int`
- `size_of(value) -> int`
- `validate() -> None`
- `summary() -> str`

`Slot` 至少保存 `value`、`start`、`end`、`size`、`offset`，并提供 `end_byte` 和 `overlaps(other)`。

### 分配规则

1. 输入和常量不进入 arena。
2. 每个有 producer 的临时值恰好一个 slot。
3. 区间是算子下标，闭区间。值的 start 是 producer 的下标，end 是最后一次被读取的下标。
4. 输出值的 end 是 `len(graph.ops)`。
5. 块大小是 `max(alignment, align_up(numel * 4, alignment))`。
6. offset 必须是 `alignment` 的倍数。
7. 只有 `old.end < new.start` 时，旧块才能回到空闲列表。等于不允许。
8. 空闲块按 `(size, offset)` 排序，选第一块足够大的，也就是 best-fit；块有剩余时从头部切出，剩余部分放回。
9. 没有合适空闲块时，从 `align_up(arena_bytes, alignment)` 处追加。
10. `arena_bytes` 最后向上取整到 `alignment`，且至少为 `alignment`。
11. `peak_live_bytes` 在每次分配后统计当前 active 块的 `size` 之和，取最大值。
12. `validate()` 检查对齐、越界、活值字节重叠；建议同时检查覆盖。

### 错误处理

- `alignment <= 0` 时，`align_up` 抛 `ValueError`。
- 图没有输出时，`plan_memory` 让 `Graph.validate()` 抛 `IRError`。
- 对齐、越界、活值别名由 `Plan.validate()` 抛 `IRError`。
- 漏分配由覆盖率检查抛出明确异常，不要留到 `offset_of` 的 `KeyError`。

### 交付物

1. `planner.py`：规划器、fixture、`main()`。
2. 运行输出：四个 fixture 的 summary。
3. 压力测试：300 张随机图，固定 seed 2026，输出通过数量和最大 arena。
4. 一段简短说明：你的 `arena_bytes` 与 `peak_live_bytes` 是否相等，若不相等，举出原因。

## 期望输出

用下面的 fixture 运行 `python .scratch\practice-c\planner.py`。四个 fixture 依次是：复用图、四连 relu、双分支、融合后的 MLP。

```text
reuse
arena_bytes=192 alignment=64 peak_live_bytes=192
value                 interval     size   offset
relu_0               [  0,  2]     64       0
relu_3               [  3,  4]     64       0
relu_1               [  1,  2]     64      64
add_4                [  4,  5]     64      64
add_2                [  2,  4]     64     128
chain
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
relu_0               [  0,  1]     64       0
relu_2               [  2,  3]     64       0
relu_1               [  1,  2]     64      64
relu_3               [  3,  4]     64      64
branches
arena_bytes=192 alignment=64 peak_live_bytes=192
value                 interval     size   offset
relu_0               [  0,  2]     64       0
relu_1               [  1,  2]     64      64
add_2                [  2,  3]     64     128
mlp
arena_bytes=320 alignment=64 peak_live_bytes=320
value                 interval     size   offset
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

这些数字来自课程实现的实际运行，你的实现应该逐行一致。`summary()` 按 offset 排序；offset 相同时按 start 排序。行宽和列顺序要一致，测试脚本会做字符串比较。

压力测试的期望输出形态：

```text
random graphs passed: 300/300, max arena_bytes: 384
```

最大 arena 可能因为随机图生成器的细节不同而变化。通过数量必须是 300/300；如果最大 arena 不同，先确认生成器的算子集合和 shape 范围与本练习一致。

## 卡点提示

### 提示 1：区间是闭区间

`start` 和 `end` 都包含在生命周期内。一个值在 op 0 产生、在 op 2 最后被读取，区间是 `[0, 2]`，不是 `[0, 3)`。释放条件是 `end < start`，不是 `end <= start`。把这一点写在代码注释里，后面调试会反复用到。

### 提示 2：输出要活到拷贝之后

输出值的 end 是 `len(graph.ops)`，比最后一个算子下标大 1。这样输出块在最后一个算子执行完后仍然占着，不会被更晚的值复用。生成 C 的拷贝循环在所有算子之后执行，拷贝完才释放 arena。

### 提示 3：空闲块要排序后再选

每次有新值开始时，先把 `active` 中 `end < start` 的块放进 `free`。然后按 `(size, offset)` 排序，找第一块 `size >= need` 的。不要按插入顺序取第一块；那是 first-fit，结果会和期望输出不同。

### 提示 4：切块时保留对齐

从空闲块切出 `need` 字节后，剩余块的起点是 `offset + need`。因为两者都是 alignment 的倍数，剩余块仍然对齐。不要从尾部切，否则剩余块的起点可能不再是原 offset，虽然仍然对齐，但排序和输出会变化。

### 提示 5：死值也会被分配

规划器不判断死代码。一个没有用户、也不是输出的值仍然有 producer，仍然会被分配。期望输出里的 `relu_1` 在某些随机图中是死值，但它仍然占一块。这是课程实现的行为。若要消除死值，先跑 `dce`，不要在规划器里偷偷跳过。

### 提示 6：summary 的排序

按 `(offset, start)` 排序，不要按 name 或 start 单独排序。offset 0 上可能有两个值，它们的 start 不同；按 start 排能让输出稳定。列宽用 `:20s`、`:3d`、`:6d`、`:7d`，与期望输出对齐。

## 实现建议

### 第 1 步：建立 Slot 与 Plan

先写数据结构和 `align_up`。`Slot.end_byte` 返回 `offset + size`，`overlaps` 用半开区间判断。`Plan.summary()` 先不急着写，等分配逻辑跑通后再补。

验收：`align_up(65, 64) == 128`，`align_up(64, 64) == 64`，`align_up(1, 64) == 64`。

### 第 2 步：算 liveness

遍历 `graph.ops`，对每个 `op.inputs` 更新 `last_use`。然后遍历 `graph.values`，跳过输入、常量和没有 producer 的值，计算 start 和 end；输出值的 end 设为 `len(graph.ops)`。最后收集候选并按 `(start, end, name)` 排序。

验收：对四连 relu，区间是 `[0,1]`、`[1,2]`、`[2,3]`、`[3,4]`。

### 第 3 步：实现 best-fit 分配

维护 `free: list[tuple[int, int]]`、`active: list[Slot]`、`arena: int`。每个候选值先释放 `end < start` 的 active 块，再按大小排序选块，切块或追加。分配后更新 `peak_live_bytes`。

验收：复用图的 `relu_3` 应该拿到 offset 0，`add_4` 应该拿到 offset 64。

### 第 4 步：写 validate 与覆盖率

`validate()` 检查每个 slot 的对齐、边界和活值重叠。覆盖率检查遍历所有有 producer 的值，确认都在 `slots` 里；再确认输入不在 `slots` 里。把覆盖率检查放进 `validate()` 或单独的 `check_coverage()`，但要在 `main()` 里调用。

验收：手工把某个 offset 改成 1，`validate()` 必须抛 `IRError`；手工删除一个 slot，覆盖率检查必须报错。

### 第 5 步：写随机图压力测试

用固定 seed 生成 300 张图，图只包含 `relu`、`add`、`mul`，输入 shape 在 1 到 6 个元素之间。每张图先跑参考解释器，再跑 `plan_memory`，检查几何，再在 arena 上覆盖执行，最后用 `assert_allclose` 对比。

验收：输出 `random graphs passed: 300/300`。如果失败，打印 case 编号、seed、IR dump 和 plan summary。

## 参考解答

<details>
<summary>点击展开：完整的 planner.py</summary>

```python
from __future__ import annotations

from dataclasses import dataclass
import random

import numpy as np
import torch

from mini_ai import from_torch, standard_pipeline
from mini_ai.ir import Graph, IRError, broadcast_shape
from mini_ai.interpreter import apply_op, evaluate_graph


def align_up(n: int, alignment: int) -> int:
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return (n + alignment - 1) // alignment * alignment


@dataclass
class Slot:
    value: object
    start: int
    end: int
    size: int
    offset: int

    @property
    def end_byte(self) -> int:
        return self.offset + self.size

    def overlaps(self, other: "Slot") -> bool:
        return self.offset < other.end_byte and other.offset < self.end_byte


class Plan:
    def __init__(self, graph: Graph, alignment: int) -> None:
        self.graph = graph
        self.alignment = alignment
        self.slots: dict[object, Slot] = {}
        self.arena_bytes = 0
        self.peak_live_bytes = 0

    def offset_of(self, value) -> int:
        return self.slots[value].offset

    def size_of(self, value) -> int:
        return self.slots[value].size

    def validate(self) -> None:
        items = list(self.slots.values())
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
        for v in self.graph.values:
            if v.kind in {"input", "constant"} or v.producer is None:
                continue
            if v not in self.slots:
                raise IRError(f"missing allocation for {v.name}")
        for v in self.graph.inputs:
            if v in self.slots:
                raise IRError(f"input {v.name} must not live in the arena")

    def summary(self) -> str:
        lines = [
            f"arena_bytes={self.arena_bytes} alignment={self.alignment} "
            f"peak_live_bytes={self.peak_live_bytes}",
            "value                 interval     size   offset",
        ]
        for slot in sorted(self.slots.values(), key=lambda s: (s.offset, s.start)):
            lines.append(
                f"{slot.value.name:20s} [{slot.start:3d},{slot.end:3d}] "
                f"{slot.size:6d} {slot.offset:7d}"
            )
        return "\n".join(lines)


def plan_memory(graph: Graph, *, alignment: int = 64) -> Plan:
    graph.validate()
    graph.recompute_users()
    op_index = {id(op): i for i, op in enumerate(graph.ops)}
    last_use: dict[object, int] = {}

    for i, op in enumerate(graph.ops):
        for v in op.inputs:
            last_use[v] = max(last_use.get(v, i), i)

    for v in graph.outputs:
        last_use[v] = len(graph.ops)

    candidates: list[tuple[int, int, object]] = []
    for v in graph.values:
        if v.kind in {"input", "constant"} or v.producer is None:
            continue
        start = op_index[id(v.producer)]
        candidates.append((start, last_use.get(v, start), v))
    candidates.sort(key=lambda item: (item[0], item[1], item[2].name))

    plan = Plan(graph, alignment)
    free: list[tuple[int, int]] = []
    active: list[Slot] = []
    arena = 0

    for start, end, value in candidates:
        still_active: list[Slot] = []
        for slot in active:
            if slot.end < start:
                free.append((slot.offset, slot.size))
            else:
                still_active.append(slot)
        active = still_active

        need = max(alignment, align_up(value.type.numel * 4, alignment))
        free.sort(key=lambda item: (item[1], item[0]))
        chosen = None
        for i, (_, size) in enumerate(free):
            if size >= need:
                chosen = i
                break

        if chosen is None:
            offset = align_up(arena, alignment)
            arena = offset + need
        else:
            offset, size = free.pop(chosen)
            if size > need:
                free.append((offset + need, size - need))

        slot = Slot(value, start, end, need, offset)
        plan.slots[value] = slot
        active.append(slot)
        plan.peak_live_bytes = max(
            plan.peak_live_bytes, sum(item.size for item in active)
        )

    plan.arena_bytes = max(alignment, align_up(arena, alignment))
    plan.validate()
    return plan


def build_reuse() -> Graph:
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
    return g


def build_chain() -> Graph:
    g = Graph()
    x = g.add_input("x", (8,))
    v = x
    for _ in range(4):
        v = g.add_op("relu", [v])
    g.add_output(v)
    return g


def build_branches() -> Graph:
    g = Graph()
    a = g.add_input("a", (4,))
    b = g.add_input("b", (4,))
    r1 = g.add_op("relu", [a])
    r2 = g.add_op("relu", [b])
    out = g.add_op("add", [r1, r2])
    g.add_output(out)
    return g


class MLP(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.fc1 = torch.nn.Linear(8, 16)
        self.fc2 = torch.nn.Linear(16, 4)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(torch.relu(self.fc1(x)))


def build_mlp() -> Graph:
    torch.manual_seed(7)
    model = MLP().eval()
    x = torch.randn(4, 8)
    g = from_torch(model, (x,))
    standard_pipeline().run(g)
    return g


def build_random(rng):
    g = Graph()
    vals = []
    for i in range(rng.randint(2, 4)):
        vals.append(g.add_input(f"x{i}", (rng.randint(1, 6),)))
    made = []
    for _ in range(rng.randint(3, 9)):
        kind = rng.choice(["relu", "add", "mul"])
        if kind == "relu":
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
        view = np.frombuffer(arena, dtype=np.uint8, count=out.size * 4, offset=off)
        view.view(np.float32)[...] = out.reshape(-1)
        env[op.output] = view.view(np.float32).reshape(out.shape)
    return [env[v].copy() for v in graph.outputs]


def stress_test(cases=300, seed=2026):
    rng = random.Random(seed)
    max_arena = 0
    for case in range(cases):
        graph = build_random(rng)
        feed = {
            v.name: np.array(
                [rng.uniform(-2, 2) for _ in range(v.type.numel)],
                dtype=np.float32,
            ).reshape(v.type.shape)
            for v in graph.inputs
        }
        ref = evaluate_graph(graph, feed)
        plan = plan_memory(graph)
        got = execute_on_arena(graph, plan, feed)
        for x, y in zip(got, ref):
            np.testing.assert_allclose(x, y, atol=1e-6, rtol=1e-6)
        max_arena = max(max_arena, plan.arena_bytes)
    return cases, max_arena


def main() -> None:
    for name, graph in [
        ("reuse", build_reuse()),
        ("chain", build_chain()),
        ("branches", build_branches()),
        ("mlp", build_mlp()),
    ]:
        print(name)
        print(plan_memory(graph).summary())
    passed, max_arena = stress_test()
    print(f"random graphs passed: {passed}/300, max arena_bytes: {max_arena}")


if __name__ == "__main__":
    main()
```

</details>

## 测试用例表

| 编号 | 输入 | 期望 arena | 关键断言 | 目的 |
| --- | --- | --- | --- | --- |
| C1 | 复用图（3 输入，5 算子） | 192 | `relu_3.offset == relu_0.offset`，`add_4.offset == relu_1.offset` | 验证生命周期复用 |
| C2 | 四连 relu（1 输入，4 算子） | 128 | offset 序列是 0、64、0、64 | 验证交替复用 |
| C3 | 双分支（2 输入，3 算子） | 192 | 三个块互不重叠 | 验证同时活跃 |
| C4 | 融合后的 MLP | 320 | `relu_fused=0`，`linear_1_fused=256` | 验证真实模型 |
| C5 | 两个 1 元素张量 | 128 | 两块各 64 字节 | 验证标量对齐 |
| C6 | `a=relu(x); b=add(a,a)` | 128 | `a.offset != b.offset` | 验证同 op 不复用 |
| C7 | 无输出图 | 不适用 | `IRError: graph has no outputs` | 验证前置校验 |
| C8 | 随机图 300 张，seed 2026 | 最大 384 | 300/300 数值一致 | 验证鲁棒性 |

C6 的期望输出：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
relu_0               [  0,  1]     64       0
add_1                [  1,  2]     64      64
```

C5 的期望输出：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
relu_0               [  0,  1]     64       0
add_1                [  1,  2]     64      64
```

注意 C5 和 C6 的表格形态相同，但图的 shape 不同。断言要检查 size 和 offset，不要只看 arena 总数。

## 延伸挑战

1. **空闲块合并**：释放时按 offset 插入 `free`，检查前后邻块是否相邻，能合并就合并。用复用图比较合并前后的 arena 是否变化。
2. **first-fit 对照**：把排序键改成 `(offset, size)`，在 300 张随机图上比较 best-fit 和 first-fit 的 arena 分布。报告最大值、中位数和差值。
3. **size class**：把空闲块按 64、128、256、512 分桶，分配时先查当前桶，再查更大桶。实现后测量分配耗时。
4. **逐元素 in-place**：只为 `relu`、`exp`、`add`、`mul` 开启 in-place，条件是输出 shape 与某个输入相同。写出每个算子的安全性理由，并用 300 张随机图验证数值一致。
5. **动态 shape**：假设输入的第 0 维在运行时变化，设计一个按最大 shape 预留 arena 的方案，说明浪费的字节数和替代方案。
6. **多输出**：构造一张有两个输出的图，确认两个输出的 end 都是 `len(ops)`，且它们的块不重叠。

## 自我检核

- [ ] 我的 `align_up` 对 0、1、64、65 都返回正确结果。
- [ ] 我的规划器跳过了输入和常量，没有给它们分配 arena。
- [ ] 我的释放条件是 `end < start`，并且有测试覆盖同 op 的情况。
- [ ] 我的输出值 end 是 `len(graph.ops)`。
- [ ] 我的空闲块按 `(size, offset)` 排序，并且切块后保留剩余块。
- [ ] 我的 `validate()` 检查对齐、越界、活值别名和覆盖。
- [ ] 我的四个 fixture 输出与期望逐行一致。
- [ ] 我的随机图压力测试是 300/300，且 seed 固定。
- [ ] 我能解释 `arena_bytes` 与 `peak_live_bytes` 为什么不总是相等。

---

下一章：[第 16 章 从 IR 到 C](./16-lower-to-c.md)
