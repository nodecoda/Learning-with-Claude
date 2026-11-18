# Ch 11 — 寄存器类与「没有寄存器分配」

> **目标**：理解 NVPTX 为什么可以完全不跑寄存器分配器，以及 `.reg` 数量是怎么算出来的、`ptxas` 又如何接手。

## 为什么需要这个？

寄存器分配是后端最核心的算法之一：live interval、图着色、spill、coalescing。你在 CPU 后端花了很多年学的东西，在 NVPTX 里**整个不存在**。这不是偷懒，而是 PTX 的虚拟寄存器模型让「分配」失去了意义。

但「没有分配」不等于「没有约束」：真正的约束在 `ptxas` 里，而 occupancy 的约束在更上层。本章把这三种东西分清楚。

## 先建立直觉

```
LLVM 机器层                     PTX 文本                ptxas
─────────────                  ─────────              ──────
虚拟寄存器 vreg1..vreg8   ──►  %r1..%r8          ──►  物理 R0..R11
（无上限）                     （声明 %r<9>）          （REG:12）
     │                              │                      │
  不做分配                      只做改名                真正的分配
```

LLVM 只负责「给每个虚拟寄存器一个 PTX 名字」；名字背后的物理资源由 `ptxas` 决定。

## 一、寄存器类：五个占位符

`NVPTXRegisterInfo.td` 原文：

```tablegen
// We use virtual registers, but define a few physical registers here to keep
// SDAG and the MachineInstr layers happy.
foreach i = 0...4 in {
  def P#i  : NVPTXReg<"%p"#i>;  // Predicate
  def RS#i : NVPTXReg<"%rs"#i>; // 16-bit
  def R#i  : NVPTXReg<"%r"#i>;  // 32-bit
  def RL#i : NVPTXReg<"%rd"#i>; // 64-bit
  def RQ#i : NVPTXReg<"%rq"#i>; // 128-bit
}
```

每个类 5 个物理寄存器，用途不是「硬件只有 5 个」，而是让 SelectionDAG / MachineInstr 的 API 有东西可指。真正的寄存器数量在 PTX 里是无限的。

对应的 RegisterClass：

| 类 | 类型 | PTX 前缀 |
|---|---|---|
| B1 | i1 | `%p` |
| B16 | i16, f16, bf16 | `%rs` |
| B32 | i32, f32, v2i16, v2f16, v4i8 | `%r` |
| B64 | i64, f64, v2i32, v2f32 | `%rd` |
| B128 | i128（仅内联汇编/128 位访存） | `%rq` |

## 二、没有分配器：源码证据

```cpp
FunctionPass *NVPTXPassConfig::createTargetRegisterAllocator(bool) {
  return nullptr; // No reg alloc
}
```

`addOptimizedRegAlloc()` 实际只加这些：

```cpp
addPass(&ProcessImplicitDefsID);
addPass(&MachineLoopInfoID);
addPass(&PHIEliminationID);
addPass(&TwoAddressInstructionPassID);
addPass(&RegisterCoalescerID);
if (addPass(&MachineSchedulerID)) printAndVerify("After Machine Scheduling");
addPass(&StackSlotColoringID);
// FIXME: Needs physical registers
// addPass(&MachineLICMID);
```

没有 greedy、没有 basic、没有 PBQP。`-regalloc=greedy` 对 NVPTX 没有任何作用。

**为什么可以这样**：PTX 的虚拟寄存器数量不设上限，live range 再长也不会溢出。所以「分配」这个问题在 PTX 层不存在。

## 三、`.reg` 数量怎么算

`NVPTXAsmPrinter::setAndEmitFunctionVirtualRegisters`（`NVPTXAsmPrinter.cpp`）：

```cpp
for (unsigned I : llvm::seq(MRI->getNumVirtRegs())) {
  Register VR = Register::index2VirtReg(I);
  if (MRI->use_empty(VR) && MRI->def_empty(VR)) continue;
  auto &RCRegMap = VRegMapping[MRI->getRegClass(VR)];
  RCRegMap[VR] = RCRegMap.size() + 1;
}
...
TS->emitRegDirective(..., It->second.size() + 1);
```

三步：

1. 遍历所有虚拟寄存器，按寄存器类分组。
2. 每组内按出现顺序编号 1、2、3…（0 保留）。
3. 打印 `.reg .bXX %prefix<N+1>`。

所以 `%r<9>` = 8 个 32 位虚拟寄存器 + 保留的 0 号。**这个数字和物理寄存器压力无关**。

## 四、那 coalescing 在做什么

`RegisterCoalescer` 在没有分配器的情况下仍然有意义：它合并 `COPY` 连接的虚拟寄存器，减少 PTX 里的 `mov`。这是「文本层」的优化，不是「资源层」的分配。

同理，`StackSlotColoring` 合并栈槽，`PHIElimination` 消除 phi——它们都在为「生成干净的 PTX」服务。

## 五、真正的分配在 ptxas

```bash
ptxas -arch=sm_75 add.ptx -o add.cubin
cuobjdump -res-usage add.cubin
# REG:12
```

`ptxas` 才做：

- live interval 计算与物理寄存器分配；
- spill 决策（溢出到 local memory）；
- occupancy 相关的寄存器预算（受 `.maxntid` / `.minnctapersm` 影响）；
- 指令调度与 SASS 编码。

这也解释了一个常见现象：**同一份 PTX 用不同版本的 ptxas，REG 数会变**。Triton 甚至把 `ptxas` 版本拼进 cache key，就是因为它会改变分配结果。

## 六、对自研 GPU 的启示

如果你做的是「吃 PTX → 自家 ISA」的编译器，你不能假设「PTX 已经分配好了」：PTX 里全是虚拟寄存器，分配是你的工作。你需要：

1. **live interval 分析**（可用 LLVM 的 LiveIntervals）。
2. **occupancy 感知的目标函数**：不是「spill 最少」，而是「在寄存器预算内让驻留 warp 最多」。
3. **spill 到 local 的策略**：GPU 的 local 是显存，代价远高于 CPU 栈。
4. **与 `.maxntid` / `.minnctapersm` 的联动**。

这四件事，NVPTX 一件都没做——因为它的下家是 ptxas。

## 踩雷集锦

1. **以为 `%r<9>` 是寄存器压力**：它只是虚拟寄存器计数。
2. **用 `-regalloc=greedy` 调 NVPTX**：没有 allocator，开关无效。
3. **以为 coalescing 是分配**：它只消除 `mov`，不决定物理资源。
4. **在 PTX 里找 spill**：没有；spill 发生在 `ptxas`。
5. **拿 PTX 的 `.reg` 数量做 occupancy 估算**：必须看 cubin 的 `REG`。

## 进阶：如果自己写 GPU 分配器

一个实用的起点是「线性扫描 + 寄存器上限 + spill 代价模型」：

- 以 `.maxntid` / `.minnctapersm` 推出每线程寄存器预算。
- 超出预算时，优先 spill 生命周期长、使用次数少的值。
- spill 选择要避开「每次循环都访问」的地址（否则 local 流量爆炸）。

这套逻辑和 CPU 的线性扫描同源，但目标函数完全不同：**CPU 最小化执行时间，GPU 最大化并行度**。

## 动手练习

1. 写一个虚拟寄存器很多的 kernel（例如 20 个独立 float 加法），看 `.reg` 数量怎么涨。
2. 用 `-regalloc=basic` / `-regalloc=greedy` 编译同一个文件，确认产物完全一样。
3. 把同一份 PTX 用 `ptxas -O0` 和 `-O3` 各编一次，比较 `REG` 与 SASS 长度。
4. 给 kernel 加 `__launch_bounds__`，看 `REG` 是否变化（`ptxas` 会据此调整）。

## 本章重点整理

- NVPTX 不做寄存器分配；PTX 的虚拟寄存器无上限。
- `.reg` 数量由虚拟寄存器计数决定，与物理压力无关。
- 真正的分配、spill、occupancy 预算都在 `ptxas`。
- 自研 GPU 后端必须自己补 occupancy 感知的分配器。

## 自我检核

- [ ] 能解释为什么 PTX 层不需要寄存器分配
- [ ] 能算出 `.reg .b32 %r<9>` 对应几个虚拟寄存器
- [ ] 知道 `-regalloc` 对 NVPTX 无效的原因
- [ ] 能说出自研 GPU 分配器与 CPU 分配器的目标差异

## 延伸阅读

- **[LLVM Register Allocation](https://llvm.org/docs/CodeGenerator.html#register-allocation)** — LLVM 官方
  - 读「Register Allocation」一节，对照 NVPTX 的 `createTargetRegisterAllocator` 返回空。
- **[PTX ISA: Registers](https://docs.nvidia.com/cuda/parallel-thread-execution/#registers)** — NVIDIA
  - 确认虚拟寄存器无数量上限。
- **[Triton: ptxas 版本与 cache](https://github.com/triton-lang/triton)** — Triton
  - 搜 `get_ptxas_version`，看真实产品如何处理 ptxas 行为变化。

寄存器不分配，那栈帧怎么办？答案是：只有「取地址的局部变量」才需要 local memory。

→ [Ch 12 栈帧、alloca 与 local memory](./12-frames-and-local-memory.md)
