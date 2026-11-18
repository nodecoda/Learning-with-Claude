# Ch 17 — 扩展与边界：加一个 intrinsic，以及自研 GPU 还差什么

> **目标**：知道给 NVPTX 加东西要走哪几个文件、怎么用 lit 锁行为，并能列出自研 GPU 后端相对 NVPTX 必须补的能力。

## 为什么需要这个？

读完后端不等于能改后端。真实工作里，你要么加一条新指令（新硬件特性），要么修一个 lowering（兼容性 bug）。这条路径在 NVPTX 上是固定的，走一遍就有肌肉记忆。

更重要的是边界：NVPTX 只覆盖「翻译到 PTX」。自研 GPU 需要的东西，有一半它根本不提供。这一章把缺口列成清单。

## 先建立直觉

```
IR intrinsic (llvm.nvvm.*)
      │  ① IntrinsicsNVVM.td 声明
      ▼
SelectionDAG 节点 / intrinsic ID
      │  ② NVPTXIntrinsics.td 映射到指令
      ▼
MachineInstr
      │  ③ AsmPrinter / InstPrinter 打印
      ▼
PTX 文本
      │  ④ lit 测试锁行为
      ▼
回归保护
```

## 一、四层结构

以 `llvm.nvvm.read.ptx.sreg.tid.x` 为例：

1. **Intrinsic 声明**：`llvm/include/llvm/IR/IntrinsicsNVVM.td` 里定义 `int_nvvm_read_ptx_sreg_tid_x`（名字、返回类型）。
2. **指令与模式**：`NVPTXIntrinsics.td` 里：

```tablegen
defm INT_PTX_SREG_TID : PTX_READ_SREG_R32V4<"tid">;
```

`PTX_READ_SREG_R32V4` 一次生成 `.x/.y/.z/.w` 四个变体，每个都带 `Pat` 把 intrinsic 映射到 `mov.u32 %r, %tid.x`。

3. **打印**：`NVPTXInstPrinter` 负责把指令打印成 `mov.u32` + 特殊寄存器名。
4. **测试**：`llvm/test/CodeGen/NVPTX/*.ll` 用 `RUN: llc ... | FileCheck` 锁住输出。

## 二、加一个 intrinsic 的步骤（guided）

> 本节是操作指南，本课**没有**实际重编 LLVM（一次完整构建代价太高）；命令与文件路径都来自本机源码，行为以你实际构建为准。

### Step 1：声明 intrinsic

在 `llvm/include/llvm/IR/IntrinsicsNVVM.td` 加一个定义，例如：

```tablegen
def int_nvvm_my_op : ClangBuiltin<"__nvvm_my_op">,
                     Intrinsic<[llvm_i32_ty], [llvm_i32_ty], [IntrNoMem, IntrSpeculatable]>;
```

### Step 2：映射到 PTX 指令

在 `NVPTXIntrinsics.td`：

```tablegen
def INT_MY_OP : BasicNVPTXInst<(outs Int32Regs:$d), (ins Int32Regs:$a),
                               "my.op.u32 $d, $a;",
                               [(set i32:$d, (int_nvvm_my_op i32:$a))]>;
```

如果 PTX 指令有特殊语法（多操作数、可选修饰符），需要写自定义 `Pat` 或 `Select` 代码。

### Step 3：必要时加 lowering

如果 intrinsic 不能一对一映射（例如需要多条指令、或依赖目标特性），在 `NVPTXISelLowering.cpp` 的 `LowerOperation` / `PerformDAGCombine` 里处理。

### Step 4：构建并测试

```bash
ninja -C /home/ubuntu/llvm/builds/ct-instr-trunk llc
```

### Step 5：写 lit 测试

参考现有测试的格式（`llvm/test/CodeGen/NVPTX/add-sub-128bit.ll`）：

```llvm
; RUN: llc < %s -mtriple=nvptx64 -mcpu=sm_20 | FileCheck %s --check-prefixes=COMMON
; COMMON-LABEL: test_add
```

运行：

```bash
cd /home/ubuntu/llvm/builds/ct-instr-trunk
./bin/llvm-lit -q test/CodeGen/NVPTX/add-sub-128bit.ll
# Total Discovered Tests: 1（通过，rc=0）
```

注意要跑**构建目录**下的测试路径（`test/CodeGen/NVPTX/...`），不是源码树路径；lit 的配置在构建目录里。

## 三、加一条新指令（而非 intrinsic）

如果新硬件指令没有对应 intrinsic，只是 ISel 的候选：

1. 在 `NVPTXInstrInfo.td` 定义指令（操作数、打印字符串、`Pattern`）。
2. 若需要新 DAG 节点，在 `NVPTXISelLowering.h` 定义 `NVPTXISD::*`，在 `PerformDAGCombine` 里生成。
3. 在 `NVPTXISelDAGToDAG.cpp` 加自定义选择逻辑（复杂寻址、特殊约束）。
4. 加 lit 测试，覆盖「能选」与「不能选」两种情况。

## 四、能力边界清单

| 能力 | NVPTX | 下家 | 自研 GPU 要不要补 |
|---|---|---|---|
| 物理寄存器分配 | 无 | ptxas | **要**（occupancy 目标） |
| spill 决策 | 无 | ptxas | **要** |
| 指令调度/时序 | 无 SchedModel | ptxas | **要**（in-order + scoreboard） |
| 指令编码/ELF | 无 | ptxas | **要** |
| divergence/reconvergence 分析 | 无 | 无 | **要** |
| shared memory 分配/swizzle | 无 | 上层（Triton/CUTLASS） | **要** |
| 软件流水/异步拷贝 | 无 | 上层 | **要** |
| MMA fragment layout | 无 | 上层 | **要** |
| occupancy 建模 | 无 | ptxas 部分 | **要** |
| PTX 语义翻译 | **有** | — | 复用/参照 |

一句话：**NVPTX 是「前半段」的完整答案，后半段要么交给 ptxas，要么自己写。**

## 五、自研 GPU 的三条现实路线

1. **吃 PTX**：写 PTX 前端 + 自己的后端。兼容性最好，但 PTX 语义面很大（Ch 2–16 全是你的输入规格）。
2. **接 NVVM IR**：在 LLVM 里加 target，复用优化器。起步快，但要跟 NVVM intrinsic 与 ABI 同步。
3. **混合**：低版本走 PTX 兼容层，新特性走自家 IR。工程上最常见。

无论哪条，Ch 11–16 的「空白清单」都是你的工作项。

## 踩雷集锦

1. **只改 TableGen 不改 Intrinsic 声明**：模式匹配不到，指令永远不被选中。
2. **忘记 `IntrNoMem`/`IntrSpeculatable`**：优化器会过于保守，或者错误地提升/删除调用。
3. **lit 跑源码树路径**：找不到测试；要用构建目录的 `test/...`。
4. **只测「能选」不测「不能选」**：错误路径没有回归保护，后面改 pattern 会静默破坏。
5. **以为加了 intrinsic 就有性能**：还要有 SASS 层验证与调度配合。

## 进阶：为自研 GPU 设计「可验证的扩展流程」

建议的最小闭环：

1. 一条新指令 → 一个 lit 测试（PTX 文本）。
2. 一个 intrinsic → 一个端到端 kernel（T4/模拟器数值对拍）。
3. 一个 pass → 一个 `-print-after` 快照测试。
4. 每次 ptxas/驱动升级 → 全量资源与 SASS 回归。

这正是把「编译器开发」变成「可维护工程」的关键。

## 动手练习

1. 在源码里找到 `INT_PTX_SREG_TID` 的完整定义链（IntrinsicsNVVM.td → NVPTXIntrinsics.td → InstPrinter）。
2. 跑一次 `llvm-lit`，记录测试数量与结果。
3. 选一条现有 NVPTX 指令，写出它的 `Pat` 对应的 IR 形状。
4. 从边界清单里挑一项（例如 shared memory 分配），写出你会怎么在 LLVM 里实现。

## 本章重点整理

- 加 intrinsic 要走四层：声明、映射、打印、测试。
- lit 测试是后端行为的唯一可靠回归。
- NVPTX 的能力边界集中在机器层与 GPU 专属优化。
- 自研 GPU 后端的核心工作是补上「ptxas + 上层框架」的那一半。

## 自我检核

- [ ] 能说出加一个 intrinsic 的四个步骤
- [ ] 知道 lit 该在哪个目录跑
- [ ] 能背出至少五项 NVPTX 不提供的能力
- [ ] 能比较三条 CUDA 兼容路线的取舍

## 延伸阅读

- **[LLVM TableGen Backends](https://llvm.org/docs/TableGen/BackEnds.html)** — LLVM 官方
  - 读指令定义与 pattern 两节；扩展新指令必读。
- **[LLVM lit](https://llvm.org/docs/CommandGuide/lit.html)** — LLVM 官方
  - 读 `RUN` 行与 `FileCheck` 用法；本章测试格式的来源。
- **[ZLUDA](https://github.com/vosen/ZLUDA)** — Andrzej Janik
  - 看真实项目如何把 PTX/cubin 接到另一套硬件；对照边界清单。

在进入 Final 之前，先用练习 D 走一遍完整的扩展流程：加一个 intrinsic，并让它被 `llc` 选中。

→ [练习 D：给 NVPTX 加一个 intrinsic](./practice-d-add-intrinsic.md)
