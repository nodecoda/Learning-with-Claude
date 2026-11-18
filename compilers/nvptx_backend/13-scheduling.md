# Ch 13 — 调度：为什么 NVPTX 几乎不调度

> **目标**：理解 NVPTX 的调度器做到哪一步、为什么它没有 SchedModel、以及真正的指令排布发生在哪里。

## 为什么需要这个？

CPU 后端的调度器目标很明确：在单线程里隐藏延迟。GPU 不是——它靠**大量并发 warp** 隐藏延迟。这个根本差异导致 NVPTX 的调度器几乎是「摆设」：它跑，但没有硬件模型可依据。

如果你把 CPU 的调度直觉带到 GPU 后端，会做出错误的优化决策。

## 先建立直觉

```
CPU:  单线程延迟敏感 ──► 乱序/调度/寄存器重命名 ──► 填满流水线
GPU:  多 warp 吞吐敏感 ──► 每个 warp 顺序执行 ──► 用 TLP 隐藏延迟
                                    │
                                    └─ 指令级只关心：别让一个 warp 卡太久
```

GPU 的 SM 是 in-order 的：一个 warp 的指令按序发射。延迟靠「切换到别的 warp」掩盖。所以调度器的任务不是「重排本 warp 的指令以填满流水线」，而是「保证有足够多可发射的 warp」——这主要是 occupancy 的问题，不是指令顺序的问题。

## 一、NVPTX 有调度器吗？

有，但只在 pre-RA 阶段：

```cpp
// addOptimizedRegAlloc()
addPass(&RegisterCoalescerID);
if (addPass(&MachineSchedulerID))
  printAndVerify("After Machine Scheduling");
addPass(&StackSlotColoringID);
```

- 没有 `addPostRAScheduler()`。
- 没有 `SchedMachineModel`（`grep SchedMachineModel llvm/lib/Target/NVPTX/*.td` 为空）。
- 没有 `InstrItinData`。

没有 SchedModel，MachineScheduler 就拿不到「这条指令几个周期、能不能双发射、有没有 stall」。它能做的只是**依赖图上的顺序整理**：把独立指令拉开一点、把 load 提前，但没有硬件代价模型。

## 二、实测：`-O0` 与 `-O2` 的顺序差异

`-O0`（节选）：

```ptx
ld.param.b64 	%rd3, [add_param_2];
ld.param.b64 	%rd2, [add_param_1];
ld.param.b64 	%rd1, [add_param_0];
mov.u32 	%r2, %tid.x;
mov.u32 	%r3, %ctaid.x;
ld.param.b32 	%r4, [add_param_3];
mov.u32 	%r5, %ntid.x;
mul.lo.s32 	%r6, %r3, %r5;
```

`-O2`：

```ptx
ld.param.b32 	%r1, [add_param_3];
mov.u32 	%r2, %tid.x;
mov.u32 	%r3, %ctaid.x;
mov.u32 	%r4, %ntid.x;
mad.lo.s32 	%r5, %r3, %r4, %r2;
```

差异主要不是「调度」，而是：

- `-O0` 先加载所有参数再计算；`-O2` 把只需要的参数延后。
- `-O0` 保留额外的基本块与 `bra.uni`。

这说明：**你看到的顺序变化，大多来自 IR/DAG 优化，而不是机器调度器**。

## 三、真正的调度在 ptxas

SASS（T4，来自 llc 的 PTX）：

```
MOV R1, c[0x0][0x28]
S2R R2, SR_CTAID.X
S2R R3, SR_TID.X
IMAD R2, R2, c[0x0][0x0], R3
ISETP.GE.AND P0, PT, R2, c[0x0][0x178], PT
@P0 EXIT
MOV R3, 0x4
IMAD.WIDE R4, R2, R3, c[0x0][0x168]
IMAD.WIDE R6, R2, R3, c[0x0][0x160]
LDG.E.SYS R5, [R4]
LDG.E.SYS R6, [R6]
IMAD.WIDE R2, R2, R3, c[0x0][0x170]
FADD R9, R6, R5
STG.E.SYS [R2], R9
EXIT
```

`ptxas` 做的事：

- 把 `mul.wide` 变成 `IMAD.WIDE`（更强的融合）。
- 把两个 `LDG` 尽量靠近，制造并行内存请求。
- 把第三个 `IMAD.WIDE` 插在 load 之后，填补等待。
- 给每条指令配控制位（stall、yield）——这些不会显示在反汇编里，但决定实际发射时机。

## 四、GPU 调度的四个真实维度

| 维度 | 谁负责 | NVPTX 有吗 |
|---|---|---|
| warp 级延迟隐藏 | occupancy / 调度器 | 无（靠硬件与 launch config） |
| 指令级重排 | ptxas | 无（只有 pre-RA） |
| stall/scoreboard 控制位 | ptxas | 无 |
| 双发射 / 端口分配 | ptxas + 硬件 | 无 |

所以「NVPTX 不调度」不是缺陷，而是分工：PTX 层不承诺时序，`ptxas` 才承诺。

## 五、为什么没有 SchedModel 也合理

PTX 的目标是跨架构可移植。sm_75 和 sm_100 的延迟、端口、双发射规则完全不同。如果在 PTX 层就按某个架构调度，反而会绑死可移植性。**把时序留到 ptxas，是虚拟 ISA 的必然选择。**

## 踩雷集锦

1. **用 `-misched` 参数调 NVPTX**：没有 SchedModel，调了也几乎没有效果。
2. **拿 PTX 的指令顺序判断性能**：真正的顺序在 SASS。
3. **以为 `-O2` 的调度更好**：`-O2` 的差异主要来自 IR/DAG 优化。
4. **忽略 occupancy**：PTX 层的顺序再漂亮，occupancy 不够也白搭。
5. **把 CPU 的「填满流水线」直觉套过来**：GPU 靠 TLP，不靠单线程 ILP。

## 进阶：如果自研 GPU 要自己调度

你需要一张真正的 SchedModel：每条指令的延迟、吞吐、端口、是否可双发射、scoreboard 依赖。然后才谈得上：

- 软件流水（把循环里的 load 提前若干迭代）；
- warp specialization（生产者/消费者 warp）；
- 异步拷贝与 barrier 的排布。

这些在 Triton / CUTLASS 里都有成熟实现，值得对照。

## 动手练习

1. 用 `-O0`/`-O2` 各编一次，标出「顺序变化」与「指令变化」，分类归因。
2. 用 `cuobjdump -sass` 看同一份 PTX 的 SASS 顺序，找出 `ptxas` 插入的 `IMAD.WIDE` 位置。
3. 在 `NVPTXInstrInfo.td` 里 grep `Sched`，确认没有调度模型。
4. 把 kernel 的 `__launch_bounds__` 调小，看 SASS 的寄存器用量与顺序是否变化。

## 本章重点整理

- NVPTX 只有 pre-RA 调度，没有 SchedModel，没有 post-RA 调度。
- 顺序差异主要来自 IR/DAG 优化，不是机器调度器。
- 真正的调度、stall 控制位、双发射都在 `ptxas`。
- GPU 的性能关键是 occupancy，不是单线程指令顺序。

## 自我检核

- [ ] 能解释为什么 NVPTX 不需要 SchedModel
- [ ] 能说出 pre-RA 调度与 ptxas 调度的分工
- [ ] 知道该去哪里看真实的指令顺序
- [ ] 能说明 TLP 与 ILP 在 GPU 上的优先级

## 延伸阅读

- **[LLVM Machine Scheduler](https://llvm.org/docs/CodeGenerator.html#machine-instruction-scheduler)** — LLVM 官方
  - 读「Pre-RA scheduling」与「SchedModel」两节，对照 NVPTX 的缺失。
- **[NVIDIA: CUDA C++ Best Practices](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/)** — NVIDIA
  - 读「Instruction Level Parallelism」与「Occupancy」两章。
- **[CUTLASS](https://github.com/NVIDIA/cutlass)** — NVIDIA
  - 看它如何手工安排 pipeline 与 warp specialization，补上编译器不做的事。

最后一个机器层话题：内存访问与向量化——为什么 `float4` 会变成两个 64 位 load？

→ [Ch 14 内存操作与向量化：float4 为什么变成两个 b64](./14-memory-and-vectorization.md)
