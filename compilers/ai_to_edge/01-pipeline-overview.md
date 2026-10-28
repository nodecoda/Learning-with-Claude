# Ch 1 — 全景：一个模型到一条指令的七次变身

> **目标**：在看任何细节之前，先建立整条链路的骨架。学完这章，你能对任意一个 AI 编译器的中间产物说出「这是第几阶段、这一阶段在解决什么问题」。

## 为什么需要这个？

编译器最让人迷失的地方是：**同一个模型有七种以上的表示形式**，而且每种都能跑、都能测、都长得不像原模型。

新手常见困境：

```
「我 torch.compile 了，然后呢？」
「这个 .mlir 文件是干什么的？」
「.ll 和 .s 和 .o 有什么关系？」
```

没有全景，你会把每一层当成孤立的新知识去背；有全景，你每看到一个中间产物就知道它在链条的哪一格。

## 先建立直觉

把 AI 编译想成**翻译一本小说的七道工序**：

```
原文（Python 模型）
  │  ① 抄写成通用语言（FX Graph）
  ▼
通用稿（ATen 算子图）
  │  ② 编辑合并同类情节（图优化：融合、折叠）
  ▼
精简稿（融合后的图）
  │  ③ 决定每一章怎么排版（调度：tiling、并行）
  ▼
排版稿（循环结构）
  │  ④ 交给排版工（代码生成）
  ▼
排版文件（llvm dialect / C++）
  │  ⑤ 排字工排成铅字（LLVM 优化 + 指令选择）
  ▼
铅字版（机器指令）
  │  ⑥ 装订成册（目标文件 + 链接）
  ▼
书（可执行文件 / 固件）
  │  ⑦ 读者真的读懂（在设备上跑出正确数字）
  ▼
读者验证（数值对齐）
```

关键洞察：**每一道工序都是「丢信息 + 做决定」**。抄写时丢了 Python 的控制流细节，排版时丢了「这段是 matmul 还是 conv」的语义——到了 LLVM IR，编译器再也看不出这是矩阵乘法，只知道「一堆乘加循环」。这个「语义降级」正是 Ch 23「算子库还是代码生成」要讨论的核心取舍。

## 用真实命令走一遍

下面每一段都有本机实测输出。先跑一遍，再读解释。

### ① Python 模型 → 计算图

```python
import torch

class M(torch.nn.Module):
    def forward(self, x, w):
        return torch.relu(x @ w + 1.0)
```

用 `torch.export` 抓成图（Ch 3 细讲三种表示）：

```python
ep = torch.export.export(M(), (torch.randn(4, 8), torch.randn(8, 4)))
print(ep.graph)
```

实测输出（torch 2.9.1+cpu，本机真实结果）：

```
graph():
    %x : [num_users=1] = placeholder[target=x]
    %w : [num_users=1] = placeholder[target=w]
    %matmul : [num_users=1] = call_function[target=torch.ops.aten.matmul.default](args = (%x, %w), kwargs = {})
    %add : [num_users=1] = call_function[target=torch.ops.aten.add.Tensor](args = (%matmul, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.ops.aten.relu.default](args = (%add,), kwargs = {})
    return (relu,)
```

注意三件事：

1. **`x @ w + 1.0` 被拆成 `matmul` + `add` 两个节点**，`relu` 是第三个。
2. 图里只有 `aten` 算子，**没有 Python 的 `+`、`@` 语法**。这就是「抄写成通用语言」。
3. 三个节点意味着**三次潜在的内存往返**——这正是图优化要解决的（下一节）。

> **实测纠正一个常见误解**：很多人以为 `x @ w + 1.0` 会被 PyTorch 自动识别成 `addmm`。本机实测**不会**。只有你显式调用 `torch.addmm(b, x, w)` 才得到单个 `addmm` 节点：
>
> ```
> %addmm : [num_users=1] = call_function[target=torch.ops.aten.addmm.default](args = (%b, %x, %w), kwargs = {})
> %relu  : [num_users=1] = call_function[target=torch.ops.aten.relu.default](args = (%addmm,), kwargs = {})
> ```
>
> 这类「以为编译器会帮我做」的假设，是本课用实测推翻最多的一类错误。

### ② 图优化

Inductor 会把 `matmul` 的后续 elementwise（这里是 `add` + `relu`）融合进一个 kernel，`matmul` 本身走调优过的算库。Ch 6 会完整跑一遍，这里先看结果。

```bash
cd code/03_inductor && TORCHINDUCTOR_CACHE_DIR=./cache \
  ~/.venvs/learn/bin/python demo.py
```

实测：

```
torch: 2.9.1+cpu
max abs diff: 0.0
shape: (128, 64)
```

`max abs diff: 0.0` 说明编译后的结果和 eager 完全一致。生成的 C++ kernel：

```cpp
extern "C" void kernel(float* in_out_ptr0) {
    for (int64_t x0 = 0; x0 < 8192; x0 += 8) {
        auto tmp0 = at::vec::Vectorized<float>::loadu(in_out_ptr0 + x0, 8);
        auto tmp1 = static_cast<float>(1.0);
        auto tmp2 = at::vec::Vectorized<float>(tmp1);
        auto tmp3 = tmp0 + tmp2;
        auto tmp4 = at::vec::clamp_min(tmp3, decltype(tmp3)(0));
        tmp4.store(in_out_ptr0 + x0);
    }
}
```

读出四件事：

- `8192` = 128×64 个元素，**整个 tensor 一个 kernel 处理完**
- `x0 += 8` + `Vectorized<float>` = **向量化宽度 8**（AVX2 上就是 8 个 float）
- `tmp3 = tmp0 + tmp2` 然后 `clamp_min(..., 0)` = **add 和 relu 在同一个循环里**
- 没有中间 tensor、没有 malloc

这一层的关键词是**融合（fusion）**：把多个算子合并成一次内存遍历。

### ③④ 调度与代码生成

同一个 matmul，在 GPU 上要切成 tile 放进 shared memory，在 CPU 上要选向量化宽度和循环顺序。这些决定发生在**调度**阶段（Ch 8）。

调度之后，循环结构交给 MLIR 或直接生成 C++。Ch 9 讲三条出口的选择。

### ⑤ MLIR → LLVM IR

这是本课的技术主干。先看一个最小例子。

`code/02_mlir_lowering/matmul.mlir`：

```mlir
module {
  func @matmul(%A: memref<4x8xf32>, %B: memref<8x4xf32>, %C: memref<4x4xf32>) {
    linalg.matmul ins(%A, %B : memref<4x8xf32>, memref<8x4xf32>)
                  outs(%C : memref<4x4xf32>)
    return
  }
}
```

`linalg.matmul` 是一个**高层的、有语义的**算子：编译器知道它是矩阵乘法。

降到循环（`--convert-linalg-to-loops`）之后：

```mlir
scf.for %arg3 = %c0 to %c4 step %c1 {
  scf.for %arg4 = %c0 to %c4 step %c1 {
    scf.for %arg5 = %c0 to %c8 step %c1 {
      %0 = memref.load %arg0[%arg3, %arg5]
      %1 = memref.load %arg1[%arg5, %arg4]
      %2 = memref.load %arg2[%arg3, %arg4]
      %3 = arith.mulf %0, %1
      %4 = arith.addf %2, %3
      memref.store %4, %arg2[%arg3, %arg4]
    }
  }
}
```

**语义在这里消失**：现在只是一组嵌套循环 + load/mul/add/store。没人知道这是 matmul。

再降到 LLVM IR：

```llvm
define void @matmul(float* %0, float* %1, i64 %2, i64 %3, i64 %4, i64 %5, i64 %6,
                    float* %7, float* %8, i64 %9, ... ) {
```

注意签名：**`memref<4x8xf32>` 变成了 7 个标量参数**。这是整个课程最重要的一个「坑」，Ch 21 专门讲。

### ⑥⑦ 到设备上跑

```bash
cd code/02_mlir_lowering && ./build.sh
```

实测：

```
== 1) linalg -> loops -> LLVM dialect ==
== 2) LLVM dialect -> LLVM IR ==
== 3) LLVM IR -> AArch64 object ==
== 4) link (static) ==
== 5) run under qemu ==
11.0 18.0 10.0 11.0
12.0 20.0 19.0 12.0
13.0 17.0 18.0 13.0
14.0 14.0 17.0 14.0
```

这些数字是 **qemu 里跑出来的 AArch64 机器码**算的，不是 numpy 算的。用 Python 独立核对：

```python
A = [float(i % 5) for i in range(32)]
B = [float(i % 3) for i in range(32)]
for i in range(4):
    print(" ".join(f"{sum(A[i*8+k]*B[k*4+j] for k in range(8)):.1f}" for j in range(4)))
```

```
11.0 18.0 10.0 11.0
12.0 20.0 19.0 12.0
13.0 17.0 18.0 13.0
14.0 14.0 17.0 14.0
```

完全一致。这就是 Ch 25「数值正确性」的方法论雏形。

## 七次变身对照表

| 阶段 | 产物形态 | 谁产生 | 这一层能回答的问题 | 这一层**不能**回答的问题 |
|---|---|---|---|---|
| ① 图捕获 | FX Graph / ExportedProgram | Dynamo / export | 有哪些算子、什么 shape | 每个算子怎么算 |
| ② 图优化 | 优化后的图 | Inductor / 图 pass | 哪些算子能合并 | 融合后循环怎么排 |
| ③ 调度 | 带循环结构的 IR | Scheduler | tile 多大、循环顺序 | 寄存器怎么分配 |
| ④ 代码生成 | C++ / llvm dialect | Codegen | 生成什么代码 | 这些代码在 ARM 上是什么指令 |
| ⑤ 后端 | 机器指令 | LLVM | 哪条指令、用哪个寄存器 | 加载到设备上能否运行 |
| ⑥ 链接 | .o → 可执行/固件 | ld / lld | 地址、重定位、段布局 | 计算是否正确 |
| ⑦ 运行 | 数字 | 设备 / qemu | 结果对不对、快不快 | 为什么慢（要回到 ①–⑤） |

**这张表是全课的索引。**后面每章开头都会说明它在哪一格。

## 三次「信息丢失」，三种能力

理解降级链路最有效的方式，是看**每一层丢了什么**：

```
① 图级      知道「这是 relu」           ← 语义最丰富
   ↓        丢了：Python 控制流细节
② 循环级    知道「这是 8192 次遍历」     ← 知道内存访问模式
   ↓        丢了：这是 matmul 这个事实
③ 指令级    知道「这是 vaddps + vmaxps」 ← 知道硬件细节
   ↓        丢了：这堆指令在算什么
④ 二进制    知道「这是 3 个字节的编码」   ← 只剩比特
```

这解释了 AI 编译器的一个根本困境：

- **越靠上，优化空间越大**（能做的数学变换多），但越不知道硬件
- **越靠下，越贴近硬件**，但已经看不出算法结构

所以生产级编译器（TVM、IREE、Inductor）都会**在多个层级同时做优化**，而不是只在一层做。Ch 29 的决策表会系统比较它们的取舍。

## 一个反直觉的事实

很多人以为编译器的任务是「忠实地把高层翻译成低层」。实际上：

> **编译器的任务是「在保持语义的前提下，改变执行方式」。**

证据就在本章的 `add` 例子里：

```c
int add(int a, int b) { return a * b + 1; }
```

- 语义：乘法、然后加一
- 生成的 AArch64：**一条** `madd w0, w1, w0, w8`

乘法没了，加法也没了，只剩一条融合指令。语义保住了，执行方式完全变了。

这就是为什么 AI 编译器能带来 2×、10× 的加速：**它不是在翻译，是在重新安排计算**。

## 踩雷集锦

1. **以为「图」就是「模型」**：FX Graph 丢了 Python 的函数调用、异常处理、动态属性。`ep.graph` 里看到的东西比你的 `forward` 少得多。这不是 bug，是设计。
2. **以为 `.ll` 能直接跑**：LLVM IR 是文本中间表示，没有可执行格式。必须先过 `llc`（或 `clang`）变成 `.s` / `.o`，再链接。
3. **以为「融合越多越好」**：融合会增加寄存器压力。极端情况下融合后的 kernel 会 spill 到栈上，比不融合还慢。Ch 8 会量化这一点。
4. **在错误层级调试**：数值错了去改 LLVM pass，或者性能差去改图优化——都是找错层。Ch 27 给一张「现象 → 层级」对照表。
5. **以为 `max abs diff: 0.0` 是常态**：CPU + 同精度下确实能到 0；一旦涉及 `fast-math`、FP16、或跨架构（x86 FMA vs ARM FMA），差异是**正常**的。Ch 25 讲怎么定容差。

## 本章重点整理

- 一个模型到一条指令要过 **七次变身**，每次都在「丢语义、做决定」。
- 图级、循环级、指令级三层各有各的优化空间与盲区。
- 编译器的本质是**保持语义、改变执行方式**，不是逐字翻译。
- `linalg.matmul` → 嵌套循环 → 7 参数函数签名，中间丢了「这是矩阵乘」这个语义。

## 自我检核

- [ ] 不看表格，说出七个阶段的名字与产物形态
- [ ] 能解释为什么 `x @ w + 1.0` 在 FX Graph 里是三个节点，以及什么写法才会得到一个 `addmm`
- [ ] 能说出「循环级知道什么、丢了什么」，以及为什么这影响优化选择
- [ ] 看到一份 LLVM IR 里的 `@matmul(float*, float*, i64, i64, ...)`，知道那串 `i64` 从哪来
- [ ] 能解释「编译器不是翻译，是重新安排计算」并举一个本章以外的例子

## 延伸阅读

### 必读

- **[LLVM Language Reference Manual](https://llvm.org/docs/LangRef.html)**
  - **读哪里**：开头 `Module Structure` 与 `Type System` 两节
  - **为什么值得读**：本章说 `.ll` 不能直接跑，读完这两节你会理解 `.ll` 到底是什么抽象层
  - **前提**：能读 C 代码即可，不需要编译原理基础

- **[MLIR: A Compiler Infrastructure for the End of Moore's Law](https://arxiv.org/abs/2002.11054)** — Lattner et al., 2020
  - **核心贡献**：提出多层 IR 与 dialect 并存的架构，正是本章「七次变身」每层都能定制的理论基础
  - **读哪里**：Section 2（Motivation）与 Section 3（MLIR 设计）；Section 4 的 case study 可先跳过
  - **和本章的关联**：本章的 ④⑤ 两格，就是 MLIR 想统一的那段「代码生成到后端」

### 论文

- **[TVM: An Automated End-to-End Optimizing Compiler for Deep Learning](https://www.usenix.org/conference/osdi18/presentation/chen)** — Chen et al., OSDI 2018
  - **核心贡献**：把「图级优化」和「调度」明确分层，并用 `compute`/`schedule` 分离来表达
  - **读哪里**：Section 3.3（Graph Optimizer）到 Section 4（Scheduling）
  - **和本章的关联**：本章 ②③ 两格的经典论述，Ch 7 与 Ch 8 的论文基础

- **[Glow: Graph Lowering Compiler Techniques for Neural Networks](https://arxiv.org/abs/1805.00907)** — Rotem et al., 2018
  - **核心贡献**：完整展示从图到 IR 到指令的 lowering 流程，并讨论内存规划
  - **读哪里**：Section 3（Graph Lowering）与 Section 5（Memory Allocation）
  - **和本章的关联**：本章 ②→⑤ 一整段落地案例，Ch 22 源头

### 博客 / 工程文章

- **[Eli Bendersky — LLVM 系列](https://eli.thegreenplace.net/tag/llvm)**
  - **这系列说什么**：从 IR 到 JIT 的后端概念，讲得比官方文档更适合第一次接触的人
  - **读哪里**：先读 "Creating and using LLVM bitcode" 与 "LLVM IR: first steps"
  - **为什么值得读**：作者是资深编译器工程师，例子都能跑

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版）
  - **这书的定位**：把本章的 ⑤⑥ 两格（后端与链接）的理论讲透
  - **读哪几章**：第 8 章（指令选择）、第 9 章（寄存器分配）、第 11 章（代码生成）
  - **注意**：书里没有 AI 相关内容，是本课 Ch 13–17 的理论底座

下一章我们先看清「边缘设备」到底特殊在哪——为什么同一个模型，到了 ARM Cortex-M 上就不能照搬服务器那套做法。

→ [Ch 2 边缘设备的真实约束](./02-edge-constraints.md)
