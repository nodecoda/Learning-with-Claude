# Ch 1 — Triton 在 AI 编译器栈中的位置

> Triton 不是“更快的 CUDA”，也不是图编译器；它是图编译器和机器码之间的一层 tile-level DSL + 编译器。

## 目标

读完本章，你应该能：

- 画出 PyTorch/Inductor → Triton → MLIR/LLVM/NVPTX 的完整栈；
- 说清楚 Triton 负责什么、不负责什么；
- 解释为什么 Inductor 生成 Triton source，而不是直接生成 PTX；
- 区分 graph-level、tile-level、IR-level、machine-level 四层各自的优化空间；
- 判断一个算子应该用 Triton、CUDA/CUTLASS、TVM/TensorIR 还是 XLA。

## 环境/版本

- 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 `3.9.0`（`python/triton/__init__.py:2`）。
- 后端：`third_party/nvidia/backend/compiler.py`；本机 GPU 是 Tesla T4，`sm_75`，CUDA 12.4（**实测**）。
- 本机没有安装 `torch`，所以本章关于 Inductor 的部分是 **外部源码/文档依据**，不冒充本机实测；Triton 内部路径都给 **源码依据**。
- Inductor 的参考路径是 PyTorch 仓库的 `torch/_inductor/codegen/triton.py`；本机没有 PyTorch 源码，因此不写该文件的行号。

## 为什么需要这个

AI 编译器栈里最常见的误判是“层级错位”：

- 模型层面的融合机会，应该在 Inductor/Dynamo 层解决，不是写一个更大的 Triton kernel；
- tile 层面的 layout、shared memory、MMA 选择，才是 Triton/TTGIR 的主场；
- PTX/SASS 层面的寄存器、指令调度，通常不该由 Python 层直接控制；
- 反过来，如果你在 Inductor 层抱怨“为什么生成了这么多 kernel”，那可能根本不是 Triton 的问题。

知道每一层“能改什么”，才能判断一个性能问题应该在哪一层修。

## 先建立直觉

```text
┌─────────────────────────────────────────────────────────────┐
│ 模型层：PyTorch eager / torch.compile                        │
│   - FX graph / Dynamo guard                                  │
│   - 算子融合、内存规划、调度                                  │
└──────────────────────────────┬──────────────────────────────┘
                               │ 生成 Triton Python source
┌──────────────────────────────▼──────────────────────────────┐
│ Tile 层：Triton DSL                                          │
│   - tl.program_id / tl.arange / tl.load / tl.store / tl.dot  │
│   - block tensor，不显式写 thread/register                    │
│   - layout 由编译器推断                                       │
└──────────────────────────────┬──────────────────────────────┘
                               │ AST -> TTIR
┌──────────────────────────────▼──────────────────────────────┐
│ IR 层：TTIR -> TTGIR -> LLIR                                 │
│   - TTIR：tile 级 op                                         │
│   - TTGIR：layout、warp、shared memory、MMA                  │
│   - LLIR：LLVM dialect，addrspace、intrinsic                 │
└──────────────────────────────┬──────────────────────────────┘
                               │ LLVM NVPTX
┌──────────────────────────────▼──────────────────────────────┐
│ 机器层：PTX -> cubin/SASS                                    │
│   - ptxas 做寄存器分配、指令选择、调度                        │
│   - CUDA driver 加载/launch                                  │
└─────────────────────────────────────────────────────────────┘
```

一句话：**Triton 把“每个线程做什么”换成“每个 program 处理哪块 tile”，把 thread/register/layout 的细节下沉给编译器。**

## 核心概念

### 四层各自负责什么

| 层 | 典型输入 | 典型输出 | 谁负责 | 主要优化 |
|---|---|---|---|---|
| Graph | FX graph / HLO | 融合后的图、调度 | Inductor / XLA / TVM Relax | 算子融合、内存复用、layout 传播 |
| Tile | Triton Python | TTIR | Triton frontend | tile 形状、mask、循环结构 |
| IR | TTIR/TTGIR/LLIR | 更低级 IR | Triton passes / MLIR | layout、coalescing、pipelining、MMA |
| Machine | LLVM IR / PTX | cubin/SASS | LLVM NVPTX / ptxas | 寄存器、指令选择、调度 |

**源码依据**：Triton 的 tile 层入口在 `python/triton/language/core.py`（`tensor` 在 `885-926`，`program_id` 在 `1845-1861`，`load` 在 `2424-2466`）；IR 层的 stage 在 `third_party/nvidia/backend/compiler.py:660-673`；机器层由 `make_ptx`/`make_cubin` 完成（`546-574`、`576-653`）。

### Triton 的编程契约

Triton kernel 里你写的是：

- 一个 program 处理一块数据；
- `tl.arange(0, BLOCK)` 产生一个 block tensor；
- `tl.load(ptr + offs)` 对整个 block 做 load；
- `tl.dot(a, b)` 对整个 tile 做矩阵乘；
- 你看不到 thread id、warp id、shared memory 地址、register 分配。

这些都由编译器在 TTGIR 层决定。**源码依据**：`python/triton/language/semantic.py:26-53` 是 `TritonSemantic` 的编程模型入口；`semantic.py:621-635` 的 `arange` 要求 range 是 2 的幂，并创建 `tt.make_range`。

### 后端抽象

Triton 不是只会生成 NVIDIA 代码。`BaseBackend.add_stages` 的契约是：

> 按插入顺序执行 stage，每个 stage 返回 `str`，最后一个 stage 返回 `bytes`；stage 之间通过 `metadata` 通信。

**源码依据**：`python/triton/backends/compiler.py:56-70`。NVIDIA 后端实现这条契约：`third_party/nvidia/backend/compiler.py:660-673`。AMD 后端在 `third_party/amd`，Proton 在 `third_party/proton`；它们共享同一套 `compile` 驱动（`python/triton/compiler/compiler.py:290-295`）。

### Gluon 是什么

Triton 3.9 还带了一个实验性的 **Gluon**：它允许你显式写 layout，而不是完全依赖推断。入口在 `python/triton/experimental/gluon/`，编译器分支在 `python/triton/compiler/code_generator.py:300-308`（`GluonSemantic`）和 `third_party/nvidia/backend/compiler.py:665-667`（`glir` stage）。

你可以把 Gluon 理解成“Triton 的显式 layout 变体”：普通 Triton 是“我写 tile，你推 layout”；Gluon 是“我连 layout 一起写”。本课主线仍然是普通 Triton；Gluon 在 Ch 29 展开。

## 底层机制

### 从 kernel 到 stage

`JITFunction.run` 的路径是：

```text
kernel[grid](args)
  -> JITFunction.run
  -> binder 产出 bound args + specialization
  -> compute_cache_key
  -> 内存 cache miss
  -> _do_compile
  -> ASTSource
  -> compile
  -> backend.add_stages
  -> ttir -> ttgir -> llir -> ptx -> cubin
  -> CompiledKernel
  -> launch
```

**源码依据**：`python/triton/runtime/jit.py:744-800`、`910-945`；`python/triton/compiler/compiler.py:226-364`。

`compile` 里有一个很重要的设计：`first_stage = list(stages.keys()).index(src.ext)`。如果 `src` 是 `ASTSource`，`src.ext == "ttir"`，从 TTIR 开始；如果 `src` 是 `IRSource` 且文件是 `.ttgir`，就从 TTGIR 开始，跳过 TTIR。**源码依据**：`compiler.py:290-295`、`python/triton/compiler/compiler.py:52-85`、`87-131`。

### Inductor 在哪里接入

PyTorch 2 的 `torch.compile` 大致是：

```text
Python model
  -> TorchDynamo 捕获 FX graph
  -> Inductor 做融合/调度
  -> Inductor 的 Triton codegen 生成 @triton.jit Python source
  -> Triton 编译这段 source
  -> CUDA kernel
```

Inductor 生成的是**普通 Triton Python 源码**，不是某种私有 IR。它用 `@triton.jit`、`tl.program_id`、`tl.load`、`tl.store`、`tl.dot` 这些公开 API；Triton 把它当用户 kernel 一样编译。参考路径是 PyTorch 仓库的 `torch/_inductor/codegen/triton.py`（**外部源码**，本机未安装 PyTorch，未核对行号）。

这也解释了一个常见问题：**Inductor 生成的 kernel 很丑，不等于 Triton 很丑。** 先看 Inductor 生成的 Python source（例如 `TORCH_LOGS=output_code`），再判断是图调度问题还是 Triton lowering 问题。

### Triton 和 MLIR/LLVM 的边界

- TTIR/TTGIR 是 Triton 自己的 MLIR dialect：`lib/Dialect/Triton`、`lib/Dialect/TritonGPU`、`include/triton/Dialect/Triton/IR/TritonOps.td`。
- TTIR → TTGIR 的 layout 分配在 `lib/Conversion/TritonToTritonGPU`。
- TTGIR → LLVM dialect 在 `lib/Conversion/TritonGPUToLLVM`；NVIDIA 专用部分在 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM`。
- LLVM dialect → LLVM IR → NVPTX 由 `make_llir`/`make_ptx` 完成（`third_party/nvidia/backend/compiler.py:422-532`、`546-574`）。
- PTX → cubin 由 `ptxas` 完成（`576-653`）。

所以 Triton 的“编译器”其实分成两半：**前半段是 Triton 自己的 dialect/pass，后半段是 MLIR/LLVM 生态。**

## 进一步用法/重要变体

### 例 1：融合 elementwise（guided）

Inductor 经常生成这种 kernel：把多个 pointwise 运算融合进一个 launch。

```python
import triton
import triton.language as tl

@triton.jit
def fused_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask, other=0.0)
    y = tl.load(y_ptr + offs, mask=mask, other=0.0)
    z = x * 2.0 + y
    z = tl.maximum(z, 0.0)
    tl.store(out_ptr + offs, z, mask=mask)
```

**guided**：这是合法写法，但本机没有 `triton`/`torch`，未执行。它体现了 tile 层的价值：一个 program 处理 `BLOCK` 个元素，融合在 kernel 内部完成，不需要中间 tensor。

### 例 2：tile 级 matmul（guided）

```python
@triton.jit
def matmul_kernel(a_ptr, b_ptr, c_ptr, M, N, K,
                  BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr, BLOCK_K: tl.constexpr):
    pid_m = tl.program_id(0)
    pid_n = tl.program_id(1)
    offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    offs_k = tl.arange(0, BLOCK_K)
    acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=tl.float32)
    for k in range(0, K, BLOCK_K):
        a = tl.load(a_ptr + offs_m[:, None] * K + (k + offs_k)[None, :])
        b = tl.load(b_ptr + (k + offs_k)[:, None] * N + offs_n[None, :])
        acc += tl.dot(a, b)
    tl.store(c_ptr + offs_m[:, None] * N + offs_n[None, :], acc)
```

**guided**：省略了 mask、边界处理和 `input_precision`，只用来展示 tile 抽象。`tl.dot` 在 `core.py:2253` 起，最终由 `TritonSemantic.dot` 创建 `tt.dot`（`semantic.py:1557` 起；TTIR op 定义在 `include/triton/Dialect/Triton/IR/TritonOps.td:762`）。

### 例 3：边界——T4 上没有 Hopper 特性（源码依据）

在 `sm_75` 上：

- `num_ctas > 1` 被拒绝：`third_party/nvidia/backend/compiler.py:244-247` 要求 SM90+；
- `clc=True` 被拒绝：`241-242` 要求 SM100+；
- TMA lowering 只在 `capability // 10 >= 9` 时加入：`368-369`；
- Blackwell 的 warp specialization/TMEM 分支在 `342-357`。

所以你在 T4 上读 Hopper/Blackwell 代码时，要分清“代码存在”和“本机能跑”。**guided**：想跑这些特性，需要 H100/B200 或远端环境。

## 对比与取舍

| 方案 | 抽象层 | 谁决定 layout/调度 | 适合 | 不适合 |
|---|---|---|---|---|
| CUDA C++ | thread/block | 你 | 极端手调、特殊指令、库内核 | 快速迭代、自动融合 |
| CUTLASS | tile/MMA | 模板 + 你选的 policy | 高性能 GEMM/卷积 | 非规则算子、快速实验 |
| Triton | tile | 编译器（可调 knob） | elementwise/reduction/attention/自定义 kernel | 需要精确控制 register/shared 的极端场景 |
| TVM/TensorIR | tile + schedule | 你写 schedule | 多后端、搜索式调优 | 想直接复用 PyTorch 生态 |
| XLA | HLO graph | 编译器 | TPU/大图、静态形状 | 手写 kernel、细粒度控制 |

Triton 的甜点是：**比 CUDA 高一层，能自动处理 layout/coalescing/pipelining；又比图编译器低一层，能让你控制 tile 和循环。** 代价是：当编译器推断不符合预期时，你只能通过 tile 形状、`num_warps`、`num_stages`、`tl.range` 属性等间接影响，不能像 CUDA 那样直接写 shared memory 地址。

## 踩雷集锦

1. **把 Triton 当图编译器。**
   Triton 不会跨 kernel 融合；融合是 Inductor/TVM 的职责。一个 Triton kernel 只做你写的那一块 tile。

2. **以为 `tl.dot` 一定生成一条 MMA。**
   `tl.dot` 先变成 `tt.dot`，到 TTGIR 才决定用 MMA 还是 FMA；取决于 shape、dtype、layout、capability。**源码依据**：`TritonOps.td:762`、`third_party/nvidia/backend/compiler.py:326-329`。

3. **在 T4 上照搬 Hopper 教程。**
   TMA、warp specialization、`num_ctas>1`、`clc` 都有 capability gate；先看 `third_party/nvidia/backend/compiler.py:241-247`、`332-370`。

4. **把 TTIR 当成最终 IR。**
   TTIR 还没有 layout；TTGIR 才有 `#blocked`/`#shared`/`convert_layout`。性能问题经常在 TTGIR 才看得见。

5. **抱怨 Inductor 生成的 Triton 代码差。**
   先看生成的 Python source 和 graph 融合策略；很多“Triton 慢”其实是 tile 形状或融合决策差。

## 进阶

- 读 `python/triton/compiler/compiler.py:226-364`，把 `compile` 当成“stage 调度器”而不是“一个大函数”。
- 读 `third_party/nvidia/backend/compiler.py:307-393` 的 `make_ttgir`，数一数 layout 相关 pass 的顺序；这是 Triton 性能的主战场。
- 如果你有 PyTorch 环境，用 `TORCH_LOGS=output_code` 看 Inductor 生成的 Triton source，再和手写 kernel 对比。
- 读 `docs/programming-guide/chapter-1/introduction.rst` 和 `chapter-2/related-work.rst`，看设计者如何定位 Triton 与 Halide/polyhedral 的关系。

## 动手练习

1. **画栈图**：不看本章，画出 PyTorch → Inductor → Triton → PTX → cubin 的路径，并标出每一层的一个优化。**guided**。
2. **写融合 kernel**：把 `z = max(2*x + y, 0)` 写成一个 Triton kernel，和 PyTorch 三段式对比 launch 次数。**guided**。
3. **写 matmul**：用 `tl.dot` 写一个 `BLOCK_M=BLOCK_N=BLOCK_K=32` 的 matmul，在 T4 上和 `torch.matmul` 对比。**guided**。
4. **触发边界**：在 T4 上传 `num_ctas=2`，记录报错；再读 `third_party/nvidia/backend/compiler.py:244-247` 解释。**guided**。
5. **看生成代码**：如果有 PyTorch，跑 `torch.compile` 并用 `TORCH_LOGS=output_code` 找生成的 Triton kernel；如果没有，读 `python/tutorials/` 里的例子代替。**guided**。

## 本章重点

- Triton 是 tile-level DSL + 编译器，不是图编译器，也不是 CUDA 的替代品。
- 图层的融合归 Inductor/XLA/TVM；tile 层的 layout/循环归 Triton；机器码归 LLVM/ptxas。
- Inductor 生成普通 Triton Python source，Triton 把它当用户 kernel 编译。
- TTIR 没有 layout，TTGIR 才有；性能问题通常在 TTGIR 层定位。
- T4 是 `sm_75`，Hopper/Blackwell 特性有 gate，读源码时要区分“存在”和“可跑”。

## 自我检核

1. Inductor 为什么不直接生成 PTX，而要先生成 Triton source？
2. TTIR 和 TTGIR 的分工是什么？为什么 layout 不在 TTIR 里？
3. `BaseBackend.add_stages` 的契约是什么？为什么 stage 之间用 `metadata` 通信？
4. 在 T4 上，哪些 NVIDIA 后端特性会被 capability gate 拒绝？
5. 一个“Triton kernel 很慢”的问题，你会先看哪三层？各自的证据是什么？

## 延伸阅读

- **Triton 官方 Programming Guide**
  - **读哪里**：`docs/programming-guide/chapter-1/introduction.rst`、`chapter-2/related-work.rst`。
  - **学什么**：Triton 的 tile 模型、与 CUDA/Halide/polyhedral 的定位差异。
  - **前提**：知道 grid/block/warp 和基本线性代数。
- **PyTorch Inductor 文档**
  - **读哪里**：`torch.compile`、Inductor、Triton codegen 相关章节。
  - **学什么**：图捕获、融合、调度、Triton 代码生成。
  - **前提**：装好 PyTorch 2.x，能跑 `torch.compile`。
- **PyTorch 源码 `torch/_inductor/codegen/triton.py`**
  - **读哪里**：Triton codegen 的 kernel 生成部分。
  - **学什么**：Inductor 如何把 FX 节点变成 `tl.load`/`tl.store`/`tl.dot`。
  - **前提**：有 PyTorch 源码 checkout；本机未安装，未核对行号。
- **MLIR 论文**
  - **读哪里**：第 2–3 节（多级 IR、dialect）。
  - **学什么**：TTIR/TTGIR/LLIR 为什么能共存于一套基础设施。
  - **前提**：读过 [`mlir_core`](../mlir_core/README.md) 的前几章。
- **`python/tutorials/01-vector-add.py` 与 `03-matrix-multiplication.py`**
  - **读哪里**：vector-add 的 `29-54`；matmul 的 kernel 和 autotune 部分。
  - **学什么**：最小 tile 程序、grid、mask、`tl.dot`、autotune。
  - **前提**：装好 `torch`/`triton`，有 NVIDIA GPU。

## 下一章

→ [Ch 2 Python AST 与 Triton 的语言边界](./02-python-ast-and-triton-language-boundary.md)
