# Ch 20 XLA / StableHLO：函数式图与 HLO 管线

> **目标**：理解 XLA 的 HLO 为什么是“带形状的纯函数式程序”，StableHLO 在其中扮演什么角色，HLO 的 pass 管线（代数化简、fusion、layout assignment）各自解决什么问题，以及它与 FX、TVM 的根本差异。本机未安装 JAX/XLA，所有执行标 `guided`；StableHLO 语法逐条对照上游测试文件核对（openxla/stablehlo main，2026-10-24 快照）。

## 为什么需要这个？

前面三章的主线是“一张图怎么变成 kernel”。XLA 走的是另一条路：**先把整张图变成一个封闭的函数式程序，再做全局优化**。

这不是风格差异，而是能力差异：

- FX/Inductor 以“算子”为单位，融合是局部的、贪心的；
- TVM 以“循环”为单位，调度是显式搜索的；
- XLA 以“整个 computation”为单位，fusion 和 layout 是**全局决策**。

XLA 的输入不是 Python 对象，也不是 buffer，而是 **HLO（High-Level Optimizer IR）**：纯 SSA、每个值都有 shape、没有可变内存。StableHLO 则是 HLO 的“稳定子集 + 版本化规范”，让 JAX、TensorFlow、PyTorch/XLA 等前端能导出同一种 IR。

理解这一章，你才能回答：

- 为什么 XLA 能跨算子做全局 layout 决策？
- 为什么 TPU 上的 fusion 和 GPU 上的 fusion 策略不同？
- 为什么 PyTorch 生态没有直接用 HLO，而是先用 FX/ATen？

## 先建立直觉：HLO 是“带形状的纯函数”

一段 HLO 程序看起来像一个函数：

```text
f32[2,8] main(f32[2,4] lhs, f32[4,8] rhs) {
  dot = f32[2,8] dot(lhs, rhs)
  ...
}
```

三个特征：

1. **每个值都有静态 shape**（或带界的动态维度）。没有“运行时才知道形状”这回事；动态 shape 也表示成有界符号。
2. **没有 buffer，没有指针，没有 in-place**。所有操作都返回值；内存分配是后端的事。
3. **没有副作用**（除了 RNG、collective 等少数有状态 op）。这让编译器可以自由重排、复制、删除。

把它和 FX 对比：

| | FX Graph | HLO |
|---|---|---|
| 值 | Python 对象 / Tensor | SSA value + shape |
| 可变性 | 节点属性可改，图可重写 | 值不可变，重写产生新值 |
| 形状 | FakeTensor 携带，可能带 SymInt | 每个指令显式 shape |
| 内存 | 隐式，由 runtime 管 | 完全由后端分配 |
| 控制流 | Python 分支会断图 | `while` / `if` 是一等指令 |

“纯函数式”不是为了优雅，而是为了**让全局优化有合法的前提**。如果值可以被原地改写，fusion 和 layout 变换就必须做别名分析；HLO 直接把这个问题消灭了。

## 核心概念一：HLO 指令集的分层

HLO 的 op 不多，但覆盖了深度学习的全部基本盘。按用途分：

| 类别 | 代表指令 | 说明 |
|---|---|---|
| Elementwise | `add`, `multiply`, `maximum`, `exponential` | 逐元素，shape 不变（除 broadcast） |
| Broadcast/Reshape | `broadcast`, `reshape`, `transpose` | 只改 shape/layout，通常可被融合掉 |
| Reduction | `reduce`, `reduce_window` | sum/max/mean 的通用形式 |
| Dot | `dot`, `dot_general` | 矩阵乘、batch matmul、任意收缩维 |
| Convolution | `convolution` | 通用卷积，含 padding/stride/dilation |
| Gather/Scatter | `gather`, `scatter` | 索引类操作 |
| RNG | `rng`, `rng_bit_generator` | 有状态，需要特殊处理 |
| Collective | `all_reduce`, `all_gather` | 分布式 |
| Control flow | `while`, `conditional`, `case` | 结构化控制流 |

重点看 `dot_general`：它不是“矩阵乘”，而是**带维度编号的收缩**。`contracting_dims = [1] x [0]` 表示“左边第 1 维与右边第 0 维相乘并求和”；`batching_dims` 表示批次维。矩阵乘、batch matmul、注意力里的 `QK^T` 都能用它表达。

这种“一个 op 覆盖多种形态”的设计，是 HLO 能做全局优化的基础：编译器不需要为每种 matmul 变体写一套规则。

## 核心概念二：StableHLO 是 HLO 的“稳定接口”

XLA 的 HLO 一直在演进，op 语义和属性会变。如果每个前端都直接生成 XLA HLO，升级一次编译器就可能全线崩。

StableHLO 解决这个问题的办法是：

1. **固定一组 op 和语义**，写成规范（spec）；
2. **版本化**（opset version），前端声明自己用的版本；
3. **可序列化**，能作为跨框架的交换格式；
4. **提供 legalization**：StableHLO → XLA HLO，由 XLA 负责后续优化。

所以真实链路是：

```text
JAX / TF / PyTorch/XLA
   │ 前端 lowering
   ▼
StableHLO（稳定、可序列化、带版本）
   │ stablehlo → xla legalization
   ▼
XLA HLO（可含 XLA 私有 op）
   │ HLO pass pipeline
   ▼
LLVM IR → CPU / GPU 机器码
```

一个容易混淆的点：**StableHLO 不是“另一个 XLA”**。它只负责“稳定地表达计算”，不做 fusion、不做 layout、不做 codegen。真正的优化在 XLA 的 HLO 管线里。

## 核心概念三：shape 推断与动态维度

HLO 要求每个指令的 shape 可推导。`add` 的两个输入 shape 必须一致（或可 broadcast），`dot_general` 的收缩维必须匹配，`reshape` 的元素数必须守恒。

StableHLO 规范把这类要求写成约束（constraints），例如 `dot_general` 要求：

- 左右操作数的 batch 维数量一致；
- contracting 维的尺寸相等；
- 结果的 shape 由 batch 维 + 非收缩维推出。

动态 shape 的处理方式是 **bounded dynamism**：维度可以写成 `?`，但通常带一个上界。编译器据此分配最大 buffer，并在运行时做 mask 或分支。这和 PyTorch 的 SymInt 是两种哲学：

- SymInt：符号表达式，编译期可做代数化简，但可能触发重编译；
- bounded dynamic：编译期知道上界，运行时按实际上界裁剪，代价是可能浪费内存/算力。

本机没有 XLA，无法演示动态 shape 的实际行为；这里只讲模型，执行标 `guided`。

## 底层机制：HLO pass 管线

XLA 的优化不是“一个 pass 解决一切”，而是一条管线。概念顺序大致如下（不同版本、不同后端的开关不同）：

```text
HLO（带 shape）
  │ 1. verifier：检查 shape/约束
  ▼
  │ 2. algebraic simplifier：代数恒等式、常量折叠、强度削减
  ▼
  │ 3. canonicalization / CSE / DCE：去重、删死代码
  ▼
  │ 4. layout assignment：给每个值选物理 layout
  ▼
  │ 5. fusion：producer-consumer / multi-output
  ▼
  │ 6. copy insertion：在 layout 冲突处插 copy
  ▼
  │ 7. rematerialization / scheduling：控制内存峰值与指令顺序
  ▼
后端 lowering：LLVM IR（CPU）或 LLVM IR → PTX（GPU）
```

管线里有三个容易被低估的环节：

- **verifier**：在优化前就拒绝非法 HLO。shape 不匹配、layout 维度数不对、collective 的 replica group 不一致，都会在这里报错。它保证后续 pass 不必防御性检查，是整条管线的“类型系统”。
- **copy insertion**：layout 冲突时插入 `copy`。`copy` 在 HLO 里是真实指令，不是零成本；layout assignment 的一个重要目标就是少插 copy。
- **rematerialization**：内存峰值过高时，宁可重算也不保存中间值。这是 HLO 层的内存规划，和 Ch 10 的 buffer 复用是同一个问题的两种解法。

三个最关键的 pass：

### 1. Algebraic simplifier

做的是“不改变语义的改写”：

- `x + 0 → x`、`x * 1 → x`；
- `x * 0 → 0`（要注意 NaN/Inf 语义，XLA 默认遵循 fast-math 风格）；
- `transpose(transpose(x)) → x`；
- `dot(a, b) + bias → dot(a, b) with epilogue`（如果后端支持）。

这一步和 Ch 6 在 FX 上做的常量折叠/代数化简是同一类问题，只是 HLO 的 shape 信息更完整，规则可以更激进。

### 2. Fusion

XLA 的 fusion 主要有两种：

- **producer-consumer fusion（kLoop）**：把 producer 融进 consumer 的循环，避免中间结果落内存。elementwise 链是典型受益者。
- **multi-output fusion（kInput/kOutput）**：一个 consumer 有多个 producer，或一个 producer 被多个 consumer 使用，合成一个 computation，减少 launch 和内存流量。

fusion 不是越多越好。反例：

- 融合后循环体太大，寄存器溢出；
- 融合把并行度高的 op 塞进串行循环；
- 融合导致 layout 冲突，反而插入更多 copy。

所以 XLA 的 fusion 有成本模型（fusion cost model），不是纯贪心。具体阈值随版本/后端变化，本机无法实测，`guided`。

### 3. Layout assignment

这是 HLO 最有特色的一步。HLO 的 **logical shape** 和 **physical layout** 是分开的：

```text
f32[2,8]{1,0}   // 逻辑 shape 2x8，维度 1 是最内层（row-major）
f32[2,8]{0,1}   // 逻辑 shape 不变，维度 0 是最内层（column-major）
```

`{1,0}` 表示“维度 1 的步长是 1，维度 0 的步长是 8”；`{0,1}` 相反。逻辑上都是 2×8，物理上访问顺序不同。

layout assignment 要解决的问题是：**给每个值选一个 layout，使得全图的 copy 最少、关键 op 最快**。约束来自：

- 后端偏好：GPU 卷积通常偏好 NHWC；CPU 偏好 row-major；
- op 要求：`dot` 的 contracting 维在某些后端要求特定 layout；
- fusion：融合后的循环访存模式；
- 边界：函数参数/返回值的 layout 由调用方约定。

这是一个全局约束问题，不是逐个 op 的局部决策。也正因为如此，XLA 需要在 fusion 之后再做 copy insertion：如果两个相邻 op 的 layout 不兼容，就插入 `copy` 做转换。

## 示例一：一段 StableHLO（guided）

下面这段程序计算 `relu(lhs @ rhs)`。语法逐条对照上游测试文件（本机已下载到 `.scratch\ai-compiler\ch20\`）：

- `dot_general` 的 prettified 形式来自 `stablehlo/tests/interpret/dot_general.mlir:8`；
- `add` 来自 `stablehlo/tests/interpret/add.mlir:6`；
- `maximum` 来自 `stablehlo/tests/interpret/maximum.mlir:6`；
- 规范条目见 `stablehlo/docs/spec.md:2803`（`dot_general`）。

```mlir
func.func @matmul_relu(%lhs: tensor<2x4xf32>, %rhs: tensor<4x8xf32>) -> tensor<2x8xf32> {
  %zero = stablehlo.constant dense<0.000000e+00> : tensor<2x8xf32>
  %0 = stablehlo.dot_general %lhs, %rhs,
    contracting_dims = [1] x [0],
    precision = [DEFAULT, DEFAULT]
    : (tensor<2x4xf32>, tensor<4x8xf32>) -> tensor<2x8xf32>
  %1 = stablehlo.add %0, %zero : tensor<2x8xf32>
  %2 = stablehlo.maximum %1, %zero : tensor<2x8xf32>
  return %2 : tensor<2x8xf32>
}
```

逐行读：

- `%zero` 是一个 splat 常量：一个标量值广播成 2×8。HLO 的常量是值，不是 buffer。
- `dot_general` 的 `contracting_dims = [1] x [0]` 表示 lhs 的第 1 维（4）与 rhs 的第 0 维（4）收缩。
- `add` 加上 0 是恒等操作，algebraic simplifier 会把它删掉。
- `maximum(x, 0)` 就是 ReLU。
- 返回值是 SSA value，没有“输出参数”。

**这段代码没有在本机执行**：本机没有 `stablehlo-opt`，也没有 XLA/JAX。语法是核对过的，运行结果是 `guided`。

## 示例二：同一段计算的 HLO 文本形态（guided）

经过 legalization 后，XLA 内部看到的是 HLO（文本形式大致如下）：

```text
HloModule matmul_relu, entry_computation_layout={(f32[2,4]{1,0}, f32[4,8]{1,0})->f32[2,8]{1,0}}

ENTRY main {
  lhs = f32[2,4]{1,0} parameter(0)
  rhs = f32[4,8]{1,0} parameter(1)
  dot = f32[2,8]{1,0} dot(lhs, rhs), lhs_contracting_dims={1}, rhs_contracting_dims={0}
  zero = f32[] constant(0)
  zero_bcast = f32[2,8]{1,0} broadcast(zero), dimensions={}
  add = f32[2,8]{1,0} add(dot, zero_bcast)
  ROOT relu = f32[2,8]{1,0} maximum(add, zero_bcast)
}
```

注意 `{1,0}`：逻辑 shape 是 `[2,8]`，物理 layout 是“第 1 维连续”。这是 CPU 上最常见的 row-major。如果后端偏好别的 layout，`entry_computation_layout` 会跟着变。

这段文本同样**未执行**，只用于展示 HLO 的结构。不同的 XLA 版本在属性、括号、layout 语法上可能有细微差异，以你所用版本的 `--xla_dump_hlo_as_text` 输出为准（`guided`）。

## 示例三：fusion 与 layout 的互动（推演）

假设图是：

```text
transpose(A) → dot(transpose(A), B) → add(bias) → relu
```

如果 `A` 的输入 layout 是 `{1,0}`，而 `dot` 偏好 contracting 维连续，编译器有三种选择：

1. 在 `transpose` 后插 copy，把 layout 转成 `dot` 喜欢的；
2. 把 `transpose` 融进 `dot` 的循环，边读边转；
3. 改 `dot` 的 layout，接受较慢的访存但省掉 copy。

选哪个取决于：

- copy 的数据量 vs 重算的代价；
- 后端是否支持“带 transpose 的 dot”；
- 后续 `add/relu` 能否融进 dot 的 epilogue。

这就是“全局决策”的含义：单独的 `transpose` 看起来只是一个 op，但它的 layout 会一路影响 dot、fusion、copy。FX 图里 `permute` 只是 stride 变化，Inductor 把它折叠进索引计算；HLO 里 layout 是显式属性，必须由 layout assignment 统一决定。

## 对比与取舍

| 维度 | FX / Inductor | TVM Relax / TIR | StableHLO / XLA |
|---|---|---|---|
| 表达层 | ATen 算子图 | Relax 图 + TIR 循环 | 纯函数式 HLO |
| 可变性 | Python 对象可变 | Relax 值 + TIR buffer | 值不可变 |
| shape | FakeTensor + SymInt | 静态/符号 shape | 静态 + bounded dynamic |
| 融合 | Scheduler 贪心 + 启发式 | 调度显式控制 | 全局 fusion pass |
| layout | Tensor stride / 索引折叠 | schedule 决定 buffer 布局 | layout assignment 全局决策 |
| 控制流 | Python 分支断图 | `If`/`While` | `if`/`while` 一等指令 |
| codegen | C++ / Triton | LLVM / CUDA | XLA 后端（LLVM/GPU/TPU） |
| 生态 | PyTorch 原生 | 独立编译器 | JAX/TF/TPU 生态 |

选择建议：

- **PyTorch 模型、要低门槛加速**：Inductor。
- **要跨框架交换、要 TPU 或 XLA 生态**：StableHLO + XLA。
- **要精确控制循环和调度、要 CPU 极致优化**：TVM。
- **要做编译器基础设施、要自定义方言**：MLIR（下一章）。

## 踩雷集锦

1. **把 StableHLO 当 XLA**：StableHLO 只负责稳定表达，不做 fusion/layout/codegen。
2. **以为 HLO 有 buffer**：HLO 是纯值语义；buffer 是后端 lowering 之后才出现的。
3. **把 `{1,0}` 当逻辑 shape**：花括号里是 layout（维度顺序），逻辑 shape 在外面。
4. **以为 `dot_general` 只算矩阵乘**：它支持 batch、任意收缩维、任意非收缩维。
5. **忽略 dynamic shape 的有界性**：bounded dynamic 要按上界分配 buffer，可能浪费内存。
6. **以为 fusion 越多越好**：融合过度会寄存器溢出、降低并行度、引入 layout copy。
7. **在不同 XLA 版本间复制 HLO 文本**：属性和语法会变；以本机 dump 为准。
8. **把 StableHLO 的 opset 版本当可选**：前端必须声明版本，否则 legalization 可能不匹配。

## 进阶：再往深一层

- **PJRT**：XLA 的运行时接口，负责 device 管理、编译缓存、跨 host 通信；HLO 编译产物通过 PJRT 加载。
- **SPMD / partitioning**：`shardings` 把一张全局 HLO 切成每设备一份；all-reduce/all-gather 是编译器插入的（Ch 24）。
- **Shardy**：新一代的 sharding 表示，把 partitioning 从 HLO 属性里独立出来。
- **XLA:GPU 的 fusion 与 tiling**：HLO → Triton/LLVM 的路径与 Inductor 类似，但 fusion 决策在 HLO 层完成。
- **StableHLO 的 portable artifact**：把模型序列化成 StableHLO，脱离框架版本；这是跨栈部署的关键。

## 动手练习

### 练习 1：手算 shape

给定 `lhs: tensor<2x4xf32>`、`rhs: tensor<4x8xf32>`，`contracting_dims = [1] x [0]`，写出 `dot_general` 的结果 shape，并解释如果改成 `contracting_dims = [0] x [0]` 会发生什么。

<details>
<summary>参考思路</summary>

结果是 `tensor<2x8xf32>`：去掉收缩维 4，剩下 lhs 的 2 和 rhs 的 8。如果改成 `[0] x [0]`，要求 lhs 的第 0 维（2）等于 rhs 的第 0 维（4），约束不满足，shape 推断报错。这个检查在编译期完成，不需要运行。
</details>

### 练习 2：layout 的步长

`f32[2,8]{1,0}` 和 `f32[2,8]{0,1}` 的逻辑 shape 相同。分别写出两种 layout 下元素 `[i,j]` 的线性偏移（假设起始偏移为 0）。

<details>
<summary>参考思路</summary>

`{1,0}`：维度 1 最内层，偏移 = `i*8 + j`。`{0,1}`：维度 0 最内层，偏移 = `j*2 + i`。两者元素总数相同，但访存顺序相反。layout assignment 的目标就是让关键 op 用上合适的那个。
</details>

### 练习 3：fusion 反例

构造一个“融合后更慢”的场景，并解释原因。提示：考虑一个巨大的 elementwise 链 + 一个 reduction。

<details>
<summary>参考思路</summary>

如果把一个产生大量中间值的 elementwise 链全部融进 reduction 的循环，循环体变大，寄存器压力上升，可能溢出到栈；同时 reduction 的并行度被限制。另一种情况是 fusion 让原本可以并行的两个分支变成串行。结论：fusion 需要成本模型，不是越彻底越好。
</details>

### 练习 4：StableHLO vs FX

把本章的 `matmul_relu` 分别用 FX 和 StableHLO 表达，列出三个差异（值语义、shape、layout）。

<details>
<summary>参考思路</summary>

FX：节点是 `operator.matmul`/`torch.relu`，值是 Python 对象，shape 由 FakeTensor 携带，layout 体现在 stride；StableHLO：值是 SSA，shape 是类型的一部分，layout 是显式属性，没有 buffer。FX 可以直接改节点属性，HLO 只能生成新值。
</details>

### 练习 5：探测你的环境

运行下面的脚本，判断你是否能实际执行本章的 StableHLO。不能执行时，说明缺什么。

```python
import importlib.util
for m in ["jax", "jaxlib", "stablehlo", "torch_xla"]:
    print(m, importlib.util.find_spec(m) is not None)
```

本机实测输出：

```text
jax False
jaxlib False
stablehlo False
torch_xla False
```

<details>
<summary>参考思路</summary>

本机全部为 `False`（`jax`/`jaxlib`/`stablehlo` 未安装，`torch_xla` 也没有）。要执行需要安装 JAX（CPU 版即可）和 `jaxlib`，或用 `stablehlo-opt` 做 IR 级验证。即使装了 JAX，HLO 文本也需要 `--xla_dump_hlo_as_text` 才能看到。安装依赖不在本课范围，标 `guided`。
</details>

## 本章重点整理

- HLO 是带静态 shape 的纯函数式 SSA IR；没有 buffer、没有 in-place、没有隐式副作用。
- StableHLO 是 HLO 的稳定子集与版本化规范，负责“表达”，不负责“优化”。
- HLO 管线：verifier → algebraic simplifier → canonicalize/CSE/DCE → layout assignment → fusion → copy insertion → backend lowering。
- `dot_general` 是带维度编号的收缩，不是单纯的矩阵乘。
- layout assignment 是全局决策：逻辑 shape 不变，物理访问顺序可变；冲突处插 copy。
- fusion 有成本模型；过度融合会导致寄存器溢出和并行度下降。
- 与 FX/TVM 的分工不同：XLA 在整图层面做 fusion/layout，TVM 在循环层面做调度，Inductor 在算子层面做贪心融合。

## 自我检核

- [ ] 我能解释 HLO 为什么必须是纯函数式
- [ ] 我知道 StableHLO 与 XLA HLO 的分工
- [ ] 我能读懂 `f32[2,8]{1,0}` 里的逻辑 shape 和 layout
- [ ] 我能说出 HLO pass 管线的至少五个阶段
- [ ] 我能解释 producer-consumer fusion 与 multi-output fusion 的区别
- [ ] 我知道 bounded dynamic shape 与 SymInt 的取舍
- [ ] 我能说出 layout assignment 为什么是全局问题
- [ ] 我知道本机为什么跑不了本章示例，以及需要什么

## 延伸阅读

- **OpenXLA StableHLO 规范**（https://openxla.org/stablehlo/spec）：读 `dot_general`、`add`、`maximum` 的语义与约束。与本章关联：示例一的语法依据。
- **XLA 架构文档**（https://openxla.org/xla/architecture）：读 HLO、backend、PJRT 的分层。与本章关联：pass 管线的位置。
- **XLA 操作语义**（https://openxla.org/xla/operation_semantics）：逐个 op 的 shape 规则。与本章关联：shape 推断。
- **StableHLO 仓库测试**（https://github.com/openxla/stablehlo/tree/main/stablehlo/tests/interpret）：读 `dot_general.mlir`、`add.mlir`、`maximum.mlir`。与本章关联：真实语法样例。
- **JAX 文档: XLA 与 jit**（https://jax.readthedocs.io/en/latest/notebooks/thinking_in_jax.html）：理解 tracing 与纯函数约束。与本章关联：HLO 的前端心智模型。

---

→ [Ch 21 MLIR 在 AI 编译器里的位置](./21-mlir-in-ai-compilers.md)