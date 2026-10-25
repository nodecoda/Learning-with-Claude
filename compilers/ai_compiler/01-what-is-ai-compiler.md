# Ch 1 AI 编译器是什么：框架、图编译器、kernel 编译器与 runtime

> **目标**：把「AI 编译器」拆成四层可观察的分工——框架、图编译器、kernel 编译器、runtime——并用同一张 `x @ W + 1 → relu` 的图，看清每一层各自看到什么、做了什么选择、丢掉了什么信息。

## 为什么需要这个？

「AI 编译器」这个词被用得很松。PyTorch 是框架，TVM 是编译器，TorchInductor 是编译器后端，ONNX Runtime 是推理运行时，MLIR 是编译器基础设施——它们都自称或被称为「AI 编译器」的一部分，但职责完全不同。如果不先分层，读文档时就会把「图优化」和「循环优化」混成一件事，遇到性能问题也不知道该看哪一层。

更常见的误解是把它当成「传统编译器的 AI 版」：以为输入是 Python，输出是机器码，中间做几趟优化就完了。真实链路里，**同一张图会被至少四种角色分别理解**：

- 框架关心「怎么把用户的 Python 代码跑起来」，以及 autograd、device、dtype。
- 图编译器关心「这张 DAG 能不能少算、少读内存、换布局」。
- kernel 编译器关心「这个算子展开成什么循环、怎么切 tile、怎么向量化」。
- runtime 关心「编译产物怎么加载、内存谁分配、调用约定是什么」。

这一章不教你装环境（Ch 0 已经做过），也不教你写 pass（Ch 5 开始）。它只解决一个前置问题：**看到一个优化或一个性能数字时，先判断它属于哪一层**。

## 先建立直觉：同一张图，四层各看到什么

先定义贯穿全课的最小模型：

```python
import torch

class Block(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(8, 8))

    def forward(self, x):
        y = x @ self.w
        y = y + 1.0
        return torch.relu(y)
```

对同一个 `Block`，四层看到的东西可以并排放在一张表里：

| 层 | 看到的东西 | 关心的核心问题 | 典型产物 |
|---|---|---|---|
| 框架（PyTorch） | `forward` 的 Python 代码、`Parameter`、autograd 图 | 语义是否正确、梯度怎么传、device/dtype 是否匹配 | eager 执行、FX Graph、ATen Graph |
| 图编译器（FX / Relax / XLA HLO） | 节点与边组成的 DAG、shape、dtype、常量 | 哪些算子能合并、哪些中间结果能删、布局怎么传播 | 优化后的图 IR |
| kernel 编译器（TVM schedule / Triton） | 循环、buffer、tile、线程/向量宽度 | 怎么切循环、怎么复用 cache、怎么生成指令 | TIR、LLVM IR、CUDA/C 代码 |
| runtime（GraphExecutor / ORT EP） | 编译好的函数、内存池、执行提供者 | 谁分配内存、按什么顺序调用、错误怎么报 | 可执行 module + 调用封装 |

注意最后一列：**每一层都有自己的「产物」**。AI 编译器不是一个单体程序，而是一条流水线；有时这条流水线会被跳过几段（比如 PyTorch eager 根本没有图编译器），有时会被合并（比如 TorchInductor 自己从 FX 一路做到 Triton 代码）。

这张表有两个容易误读的地方。第一，四层不是按代码行数切分的，而是按**决策类型**切分的：同一个 `torch.compile` 调用里，Dynamo 做抓图、Inductor 做融合和调度、runtime 做加载，三层可能在几百毫秒内全部发生。第二，「框架层」不等于「不编译」：PyTorch 的 dispatcher、autograd、ATen kernel 选择本身就是一套运行时系统，只是它不做跨算子的全局优化。

## 核心概念一：四层分工

### 框架层：语义的源头

PyTorch 的 `nn.Module` 是命令式代码。`forward` 里可以有 `if`、循环、`print`、甚至改全局变量。框架层的职责是**把这段代码按 Python 语义执行**，同时维护 tensor 的元数据：shape、dtype、device、`requires_grad`、stride。

框架层自己也有 IR：ATen 的算子 schema、dispatcher 的 dispatch key、autograd 的梯度图。只是这些 IR 主要服务于「正确执行」，而不是「跨算子优化」。所以框架层更像一个**解释器 + 算子库 + 自动微分引擎**的组合。

框架层不是一个「编译器」，但它提供了编译器的入口：

- `torch.fx.symbolic_trace`：把一部分 Python 代码录成 FX Graph。
- `torch.export.export`：把模型导出成带签名的 ATen Graph。
- `torch.compile`：在 eager 之上加一层图捕获与后端编译。

关键点：**抓图不是免费的**。抓图过程会丢掉 Python 控制流、副作用、部分别名信息。Ch 2 会逐项对照这些损失。

### 图编译器层：在算子边界上做选择

图编译器拿到的是 DAG。它看到的不是 `forward`，而是「`matmul` 的输出喂给 `add`，`add` 的输出喂给 `relu`」。这一层做的典型事情：

- **常量折叠**：`1.0 + 2.0` 在编译期算掉。
- **死代码消除**：没人用的节点删掉。
- **算子融合**：把 `add` 和 `relu` 合成一个 kernel 边界。
- **布局传播**：决定中间张量用 NCHW 还是 NHWC。
- **量化**：把 fp32 的算子换成 int8 的算子组合。

图编译器的代表 IR：PyTorch FX、TVM Relax、XLA HLO、ONNX Graph。它们的共同点是**以算子为节点**；区别在于算子集、类型系统、是否带控制流、是否可序列化。

### kernel 编译器层：在循环和内存上做选择

图编译器说「这里要一个 matmul」，但没说 matmul 怎么实现。kernel 编译器（TVM schedule、Triton、Inductor 的 codegen）看到的是一段循环：

```text
for i in range(M):
  for j in range(N):
    for k in range(K):
      C[i, j] += A[i, k] * B[k, j]
```

然后它决定：`k` 要不要切块、`j` 要不要向量化、`i` 要不要并行、`A` 和 `B` 要不要先搬到 shared memory。**同一个 matmul，不同 schedule 的性能可以差几倍到几十倍**，这是 Ch 11–13 的主题。

### runtime 层：把产物跑起来

runtime 拿到的是编译好的 module。它关心：

- 输入输出 buffer 怎么分配、要不要复用。
- 函数入口的调用约定（参数顺序、类型检查、返回值）。
- 多设备时怎么把数据搬到对应 device。
- 出错时怎么把错误码翻译成人话。

TVM 的 GraphExecutor/Relax VM、ONNX Runtime 的 Execution Provider、PyTorch 的 dispatcher，都是 runtime 层的不同形态。

> 一句话总结：**框架定义语义，图编译器改变计算，kernel 编译器改变循环，runtime 改变调用方式。**

## 核心概念二：和传统编译器是什么关系

把 AI 编译器和 LLVM 这类传统编译器对照，会发现「阶段名」很像，但「约束」完全不同：

| 维度 | 传统编译器（如 LLVM） | AI 编译器 |
|---|---|---|
| 输入 | C/C++/Rust 等语言 | 框架的图或导出的模型 |
| 核心 IR | LLVM IR（SSA、标量/向量） | 图 IR + 张量 IR（TIR/HLO） |
| 优化目标 | 指令数、寄存器、分支 | 内存流量、并行度、kernel 数量 |
| 正确性 | 精确语义（整数、浮点严格） | 允许容差（`allclose`） |
| 性能模型 | 静态代价模型为主 | 静态模型 + 实测 autotuning |
| 控制流 | 任意循环、分支、异常 | 多数图是纯数据流；控制流是特例 |

两个关键差异：

1. **粒度不同**。LLVM 的优化单位是指令/基本块；AI 编译器的优化单位常常是整个 tensor 算子。把 `add` 和 `relu` 融合，相当于把两个「巨型指令」合成一个循环 nest。
2. **性能来源不同**。传统编译器主要靠减少指令和寄存器压力；AI 编译器主要靠**减少内存流量**和**提高并行度**。一个 elementwise 算子做 1 次浮点运算却要读写 8 字节，瓶颈几乎总在内存，不在 ALU。

这也是为什么 AI 编译器需要「调度」这个传统编译器里不常见的概念：图只说明算什么，循环结构才决定内存怎么走。

## 核心概念三：和 MLIR 是什么关系

MLIR 是**编译器基础设施**，不是 AI 编译器。它提供可扩展的 dialect、SSA 基础设施、pass 管理、pattern rewrite。AI 编译器可以用 MLIR 搭，也可以不用：

- **XLA / StableHLO**：HLO 是 XLA 的高层 IR，StableHLO 是它的可移植子集；再往下可以进 MLIR 的 linalg/memref。
- **IREE**：完全基于 MLIR，从 StableHLO/linalg 一路 lower 到 HAL。
- **TVM**：有自己的 Relax 和 TIR，不依赖 MLIR。
- **TorchInductor**：FX → Inductor IR → Triton/C++，也没有走 MLIR。

所以「AI 编译器 = MLIR」是错的；更准确的说法是：**MLIR 提供了一套搭编译器的积木，StableHLO 是其中一块常用的 AI 方言**。Ch 21 会展开 linalg/tensor/memref 的分工，并回指 [`mlir_core`](../mlir_core/README.md)。

## 底层机制：一次 `x @ W + 1 → relu` 的旅程

把这条链路拆成四站，每站都问三个问题：输入是什么、做了什么选择、输出是什么。

### 第 1 站：框架层

输入是 Python 调用 `m(x)`。PyTorch 的 dispatcher 根据 dtype/device 选择 `matmul`、`add`、`relu` 的具体实现，autograd 记录反向图（如果 `requires_grad=True`）。这一站没有「编译」，只有「解释执行 + 算子派发」。

### 第 2 站：图编译器

如果用 `symbolic_trace`，得到 5 个计算节点：`placeholder`、`get_attr`、3 个 `call_function`，再加一个 `output`。图编译器在这一层可以做：

- 把 `add` 的常量 `1.0` 折进 `relu` 的阈值？不行，`relu(x+1)` 不等于 `relu(x)+1`，代数上不能随便换。
- 把 `add` 和 `relu` 融合成一个 kernel？可以，因为它们是逐元素的，迭代空间相同。
- 把 `matmul` 的输出直接留在寄存器里给 `add` 用？可以，如果融合边界允许。

### 第 3 站：kernel 编译器

对 `matmul`，它看到 `i/j/k` 三层循环；对 `relu`，它看到 `i/j` 两层循环。它可以决定 `k` 切多大、`j` 是否向量化、要不要把 `C` 的 tile 留在 cache。**图编译器决定「融合不融合」，kernel 编译器决定「融合后的循环怎么排」。**

### 第 4 站：runtime

编译产物是一个函数：输入 `A`、`B`、`C`、`D` 四个 buffer，输出写到 `D`。runtime 负责分配 `C`/`D` 的内存、把 numpy 数组搬进 TVM tensor、按顺序调用函数、把结果搬回 numpy。它不知道 `relu` 是什么，只知道「第四个参数是输出」。

## 示例一：框架层看到的是 Python

先看 FX 录下来的「源码」：

```python
m = Block().eval()
x = torch.randn(4, 8)
g = torch.fx.symbolic_trace(m)
print(g.code)
```

实测输出（torch 2.14.1+cpu）：

```text


def forward(self, x):
    w = self.w
    matmul = x @ w;  x = w = None
    add = matmul + 1.0;  matmul = None
    relu = torch.relu(add);  add = None
    return relu
```

这段代码不是原始的 `forward`：`self.w` 被提升成一个局部变量 `w`，中间变量被显式置 `None` 以提示生命周期。它仍然是 Python，但已经**去掉了 `self` 的方法调用结构**，只保留数据流。框架层在这里的产物是「可读的 Python 子集」，不是机器码。

## 示例二：图编译器看到的是 DAG

同一张图打印成 IR：

```python
print(g.graph)
```

实测输出：

```text
graph():
    %x : [num_users=1] = placeholder[target=x]
    %w : [num_users=1] = get_attr[target=w]
    %matmul : [num_users=1] = call_function[target=operator.matmul](args = (%x, %w), kwargs = {})
    %add : [num_users=1] = call_function[target=operator.add](args = (%matmul, 1.0), kwargs = {})
    %relu : [num_users=1] = call_function[target=torch.relu](args = (%add,), kwargs = {})
    return relu
```

图编译器看到的是：

- `placeholder`：图的外部输入。
- `get_attr`：从 module 上取参数/buffer。
- `call_function`：一个可调用对象 + 参数列表。
- `output`：返回值。
- 每个节点上的 `num_users`：这条边被几个消费者使用。

`num_users` 不是装饰：它是 DCE、融合、内存规划的输入。一个中间结果如果有 2 个用户，融合时就要考虑「算一次还是算两次」。

源码依据：`Graph` 类定义在 `torch/fx/graph.py:1397`；`placeholder`、`get_attr`、`call_function`、`output` 的构造函数分别在 `torch/fx/graph.py:1766`、`:1799`、`:1959`、`:2424`；节点类型字段 `op` 的定义和注释在 `torch/fx/node.py:291-292`（torch 2.14.1）。TVM 侧对应的是 Relax 的 `Function`（`tvm/relax/expr.py:357`）和 `Call`（`tvm/ir/expr.py:485`）。

## 示例三：kernel 编译器看到的是循环

把同一个 matmul 写成 TVM 的 TE 表达式，再加一个 relu，然后做一次 `split + reorder`：

```python
import tvm
from tvm import te

M, N, K = 4, 8, 8
A = te.placeholder((M, K), name="A")
B = te.placeholder((K, N), name="B")
k = te.reduce_axis((0, K), name="k")
C = te.compute((M, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")
D = te.compute((M, N), lambda i, j: te.max(C[i, j], te.const(0, "float32")), name="D")

func = te.create_prim_func([A, B, C, D])
sch = tvm.s_tir.Schedule(func)
blk = sch.get_sblock("C")
i, j, kk = sch.get_loops(blk)
ko, ki = sch.split(kk, factors=[2, None])
sch.reorder(i, j, ko, ki)
print(sch.mod.script())
```

实测输出（节选，tvm 0.27.0.post1）：

```text
@I.ir_module
class Module:
    @T.prim_func(s_tir=True)
    def main(A: T.Buffer((4, 8), "float32"), B: T.Buffer((8, 8), "float32"), C: T.Buffer((4, 8), "float32"), D: T.Buffer((4, 8), "float32")):
        T.func_attr({"tirx.noalias": True})
        # with T.sblock("root"):
        for i, j, k_0, k_1 in T.grid(4, 8, 2, 4):
            with T.sblock("C"):
                v_i, v_j = T.axis.remap("SS", [i, j])
                v_k = T.axis.reduce(8, k_0 * 4 + k_1)
                T.reads(A[v_i, v_k], B[v_k, v_j])
                T.writes(C[v_i, v_j])
                with T.init():
                    C[v_i, v_j] = T.float32(0.0)
                C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
        for i, j in T.grid(4, 8):
            with T.sblock("D"):
                v_i, v_j = T.axis.remap("SS", [i, j])
                T.reads(C[v_i, v_j])
                T.writes(D[v_i, v_j])
                D[v_i, v_j] = T.max(C[v_i, v_j], T.float32(0.0))
```

这张 TIR 里能看到两层循环（`C` 的 4 层嵌套、`D` 的 2 层），以及 `k_0/k_1` 的切分。**图里只有一个 `matmul` 节点，TIR 里已经是 4 层循环 nest**——这就是 kernel 编译器新增的信息。

源码依据：`te.create_prim_func` 定义在 `tvm/te/operation.py:538`，`te.compute` 在 `:60`（tvm 0.27.0.post1）。

## 示例四：runtime 看到的是调用约定

把上面的 schedule build 出来并执行：

```python
import numpy as np

f = tvm.build(sch.mod, target="llvm")
np.random.seed(0)
a = np.random.randn(M, K).astype("float32")
b = np.random.randn(K, N).astype("float32")

ta = tvm.runtime.tensor(a, tvm.cpu())
tb = tvm.runtime.tensor(b, tvm.cpu())
tc = tvm.runtime.empty((M, N), "float32", tvm.cpu())
td = tvm.runtime.empty((M, N), "float32", tvm.cpu())

f(ta, tb, tc, td)
print("entry", f.entry_name, "maxerr", float(np.abs(td.numpy() - np.maximum(a @ b, 0)).max()))
```

实测输出：

```text
entry main maxerr 4.76837158203125e-07
```

runtime 看到的是 `main(A, B, C, D)` 四个 tensor 参数。它不关心 `C` 是中间结果、`D` 是最终结果，也不关心 `relu` 的语义；它只负责把 buffer 按顺序传进去。`maxerr` 不为 0 是因为 BLAS 和 TVM 的累加顺序不同，`4.8e-07` 对 float32 是正常范围。

## 对比与取舍

四层不是「必须全用」，而是「按需组合」。常见组合：

| 场景 | 用到的层 | 说明 |
|---|---|---|
| PyTorch eager 调试 | 框架 + runtime | 没有图编译，逐算子执行，最容易调试 |
| `torch.compile` | 框架 + 图编译器 + kernel 编译器 | Dynamo 抓图，Inductor 生成 Triton/C++ |
| TVM 部署 | 图（Relax）+ kernel（TIR）+ runtime（VM） | 端到端可控，但需要自己处理 frontend |
| ONNX Runtime 推理 | 图（ONNX）+ runtime（EP） | 跨框架交换，EP 决定哪些算子落到哪个后端 |
| XLA/TPU | 图（HLO）+ kernel（XLA codegen） | 图级融合和 layout assignment 很强 |

取舍的核心是**你在哪一层拥有控制权**：

- 想快速改模型：留在框架层。
- 想跨算子优化：需要图编译器。
- 想榨干某个 kernel：需要 schedule/codegen。
- 想换硬件/部署：runtime 和 EP 决定可行性。

## 踩雷集锦

1. **把 PyTorch 叫「AI 编译器」**：PyTorch 默认是 eager 框架；只有 `torch.compile`/`torch.export` 才引入编译。说「PyTorch 编译了这张图」通常不准确。
2. **以为 FX 图能保留所有 Python 语义**：`if`、`for`、`print`、原地修改都可能让 `symbolic_trace` 失败或失真。抓图是有损的。
3. **把图优化和 kernel 优化混为一谈**：融合是图级决策，tile/vectorize 是 kernel 级决策；两者失败的原因完全不同。
4. **以为 TVM 一定走 MLIR**：TVM 有自己的 Relax/TIR；MLIR 是另一条技术路线。
5. **把 `num_users=1` 当成「只有一个读者」**：`output` 也算一个 user；做 DCE 时要区分「图内消费者」和「输出」。
6. **拿一次计时当性能结论**：本机是 4 核笔记本，频率和负载波动很大；至少给中位数、样本数和波动范围。
7. **忽略 runtime 的拷贝**：`tvm.runtime.tensor` 会把 numpy 数据复制进 TVM 的紧凑 buffer；非连续数组的 stride 在这一步被抹平（Ch 3 实测）。
8. **把「编译成功」当成「部署成功」**：`tvm.build` 成功只说明 codegen 过了；`export_library`/`load_module` 还可能因为动态库、ABI、路径问题失败，这属于 runtime 层。

## 进阶：再往深一层

- 打开 `torch.fx` 的 `Node.meta`，看 `tensor_meta` 里存了哪些 shape/dtype 信息；这些信息是图编译器做布局传播的输入。
- 用 `torch.export` 导出同一模型，对比 `graph_signature`：参数从 `get_attr` 变成了 `placeholder`，这改变了「谁是图的外部输入」。
- 读 TVM Relax 的 `Function`/`Call` 定义，理解为什么 Relax 要把 shape/dtype 写进类型，而 FX 把元数据放在 `node.meta`。
- 把本章的 matmul 换成 `n=512`，用 `time_evaluator` 测不同 `split` factor；你会看到 kernel 层的选择如何直接影响数字（Ch 11 展开）。

## 动手练习

### 练习 1：加一个 bias，判断哪一层变了

给 `Block` 加 `self.b = nn.Parameter(torch.randn(8))`，`forward` 改成 `torch.relu(x @ self.w + self.b)`。重新抓 FX 图，回答：节点数变了吗？`get_attr` 变了吗？如果用 TVM 实现，TIR 里会多出什么？

<details>
<summary>参考思路</summary>

FX 图会多一个 `get_attr`（`b`），`add` 的参数从常量 `1.0` 变成 `%b`；节点总数从 5 个（不含 output）变成 6 个。TVM 侧则要在 `te.compute` 里多读一个 `Bias[j]`，TIR 的 `reads` 会多一项。**图层的「多一个输入」和 kernel 层的「多一次 load」是同一件事的两种表示。**
</details>

### 练习 2：同一张图，两种 schedule

把本章 matmul 的 `split` factor 从 2 改成 4、8，打印 TIR 并运行。回答：`k_0/k_1` 的循环边界怎么变？为什么 TIR 变了但 `maxerr` 不变？

<details>
<summary>参考思路</summary>

`T.grid(4, 8, K//factor, factor)` 的第三/第四维会变；循环顺序不变时，累加顺序可能变，但浮点误差仍在 `1e-6` 量级。**schedule 改变的是执行顺序，不是数学定义**——这是 Ch 11 的核心。
</details>

### 练习 3：同一张图，不同 runtime

把本章的 TVM module 用 `f.export_library("block.dll")` 导出，再用 `tvm.runtime.load_module("block.dll")` 加载，调用方式有什么变化？如果导出失败，报错说明了什么？

<details>
<summary>参考思路</summary>

`export_library` 会把 host 代码、常量和依赖打包成平台动态库；`load_module` 只负责加载，不会重新编译。调用入口仍然是 `main`，但参数类型检查由 module 的 wrapper 负责。Windows 上如果缺少 MSVC 运行库或路径含非 ASCII 字符，导出/加载可能失败——这类错误属于 runtime 层，不是图或 kernel 层。
</details>

### 练习 4：给优化归类

把下面的优化分别归到「图编译器」或「kernel 编译器」，并说明理由：常量折叠、`add+relu` 融合、`k` 轴 tiling、NCHW→NHWC、向量化、死代码消除。

<details>
<summary>参考思路</summary>

图级：常量折叠、`add+relu` 融合、NCHW→NHWC、死代码消除（都作用在算子边界/整图）。kernel 级：`k` 轴 tiling、向量化（作用在循环和内存访问）。边界情况：布局转换既可能由图编译器决定「在哪插 copy」，也可能由 kernel 编译器决定「怎么读」，要看它改的是图还是循环。
</details>

## 本章重点整理

- AI 编译器是四层分工：框架、图编译器、kernel 编译器、runtime；不是所有链路都四层齐全。
- 框架层定义语义，图编译器在算子边界做融合/折叠/布局，kernel 编译器在循环上做 tile/vectorize，runtime 负责加载与调用。
- 与传统编译器相比，AI 编译器更关心内存流量、并行度和实测调优，正确性允许数值容差。
- MLIR 是基础设施，不是 AI 编译器；TVM 走 Relax/TIR，XLA 走 HLO/StableHLO，Inductor 走 FX→Triton/C++。
- 同一张 `x @ W + 1 → relu` 图：FX 看到 5 个计算节点（不含 output），TIR 看到 matmul 的 4 层循环，runtime 看到 4 个 buffer 参数。
- `num_users`、shape、dtype、stride 是跨层传递的关键元数据；抓图和 runtime 拷贝都可能丢信息。

## 自我检核

- [ ] 我能说出四层各自的一句话职责，并各举一个代表实现
- [ ] 我能解释为什么 PyTorch eager 不属于「图编译器」
- [ ] 我能从 FX 图里指出 `placeholder`/`get_attr`/`call_function`/`output`
- [ ] 我能解释 `num_users` 在 DCE 和融合里的作用
- [ ] 我能说清 AI 编译器与 LLVM 的三个关键差异
- [ ] 我能说清 MLIR 与 AI 编译器的关系，不把两者等同
- [ ] 我能把「融合」「tiling」「布局转换」正确归到图级或 kernel 级
- [ ] 我知道本机跑的是哪个 target，以及为什么 CUDA 内容要标 `guided`

## 延伸阅读

- **TVM 官方文档：Relax**（https://tvm.apache.org/docs/relax/）：读「Relax IR」与「Dataflow」两节，重点看 `R.Call`、`R.Var`、`R.dataflow` 的语义。与本章关联：图编译器层的真实 IR。
- **PyTorch FX 文档**（https://pytorch.org/docs/stable/fx.html）：读 `symbolic_trace`、`Graph`、`Node` 三节，对照本章的节点类型。与本章关联：框架层如何抓图。
- **TVM 论文 (OSDI 2018)**（https://www.usenix.org/conference/osdi18/presentation/chen）：读第 3–4 节，理解「计算与调度分离」为什么是 kernel 层的关键抽象。与本章关联：kernel 编译器层。
- **MLIR 论文**（https://arxiv.org/abs/2002.11054）：读第 2 节，理解 dialect 的分层思想。与本章关联：Ch 21 与 `mlir_core`。
- **TorchInductor 设计讨论**（https://dev-discuss.pytorch.org/）：搜索 Inductor 的架构图，看它如何把 FX 一路降到 Triton/C++。与本章关联：四层在实际生产栈里如何被合并。

---

→ [Ch 2 图 IR 对照：FX、torch.export、ONNX、Relax、StableHLO](./02-graph-ir.md)