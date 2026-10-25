"""Liveness analysis and a linear-scan memory planner."""

from __future__ import annotations

from dataclasses import dataclass

from .ir import Graph, IRError, Value


def align_up(n: int, alignment: int) -> int:
    if alignment <= 0:
        raise ValueError("alignment must be positive")
    return (n + alignment - 1) // alignment * alignment


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


class MemoryPlan:
    def __init__(self, graph: Graph, alignment: int) -> None:
        self.graph = graph
        self.alignment = alignment
        self.allocations: dict[Value, Allocation] = {}
        self.arena_bytes = 0
        self.peak_live_bytes = 0

    def add(self, alloc: Allocation) -> None:
        self.allocations[alloc.value] = alloc

    def offset_of(self, value: Value) -> int:
        return self.allocations[value].offset

    def size_of(self, value: Value) -> int:
        return self.allocations[value].size

    def validate(self) -> None:
        items = list(self.allocations.values())
        for i, a in enumerate(items):
            if a.offset % self.alignment != 0:
                raise IRError(f"{a.value.name}: offset {a.offset} is not aligned to {self.alignment}")
            if a.offset < 0 or a.end_byte > self.arena_bytes:
                raise IRError(f"{a.value.name}: allocation outside arena")
            for b in items[i + 1 :]:
                live_overlap = a.start <= b.end and b.start <= a.end
                if live_overlap and a.overlaps(b):
                    raise IRError(
                        f"live ranges overlap but buffers alias: {a.value.name} and {b.value.name}"
                    )

    def summary(self) -> str:
        lines = [
            f"arena_bytes={self.arena_bytes} alignment={self.alignment} peak_live_bytes={self.peak_live_bytes}",
            "value                 interval     size   offset",
        ]
        for alloc in sorted(self.allocations.values(), key=lambda x: x.offset):
            lines.append(
                f"{alloc.value.name:20s} [{alloc.start:3d},{alloc.end:3d}] "
                f"{alloc.size:6d} {alloc.offset:7d}"
            )
        return "\n".join(lines)


def plan_memory(graph: Graph, *, alignment: int = 64) -> MemoryPlan:
    """Assign non-overlapping arena offsets using live intervals.

    Inputs and constants are external (or embedded), so they are not part of the
    arena.  A value may reuse a dead value's buffer only when its live interval
    starts strictly after the old interval ends.  Equal indices mean the new op
    still reads the old value in the same kernel, so reuse would be wrong.
    """
    graph.validate()
    graph.recompute_users()
    op_index = {id(op): i for i, op in enumerate(graph.ops)}
    last_use: dict[Value, int] = {}

    for i, op in enumerate(graph.ops):
        for v in op.inputs:
            last_use[v] = max(last_use.get(v, i), i)

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

    candidates: list[tuple[int, int, Value]] = []
    for v in graph.values:
        if v.kind in {"input", "constant"} or v.producer is None:
            continue
        start = op_index[id(v.producer)]
        end = last_use.get(v, start)
        candidates.append((start, end, v))
    candidates.sort(key=lambda x: (x[0], x[1], x[2].name))

    plan = MemoryPlan(graph, alignment)
    free_blocks: list[tuple[int, int]] = []
    active: list[Allocation] = []
    arena_bytes = 0

    def free_block(offset: int, size: int) -> None:
        free_blocks.append((offset, size))

    for start, end, value in candidates:
        # Free only buffers whose last use is strictly before this op.
        still_active: list[Allocation] = []
        for alloc in active:
            if alloc.end < start:
                free_block(alloc.offset, alloc.size)
            else:
                still_active.append(alloc)
        active = still_active

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
        alloc = Allocation(value=value, start=start, end=end, size=need, offset=offset)
        plan.add(alloc)
        active.append(alloc)
        plan.peak_live_bytes = max(plan.peak_live_bytes, sum(a.size for a in active))

    plan.arena_bytes = max(alignment, align_up(arena_bytes, alignment))
    plan.validate()
    return plan
