# 自己造一个 mini AI 编译器：从计算图到 C / LLVM

> 给会写 Python、懂一点编译器、想把「模型怎么变成可执行代码」亲手做出来的工程师。

这门课不带你背 TVM 的 schedule API，也不要求先读完成千上万行生产编译器。我们从一张很小的计算图出发，自己定义 IR、写解释器、做 Pass、规划内存、生成 C，再生成 LLVM IR，最后用真实模型做数值对齐和性能测量。每一部分都落到同一套可运行代码上。

配套实现位于 [`code/`](./code/)，课程里的关键片段都来自它。

全课 **25 章 + 4 个练习 + 1 个 Final Project**，每章围绕同一个可运行的 mini 编译器推进，不做互不相干的 API 演示。

> 证据约定：`实测` = 本机真实输出；`源码依据` = 对照具体版本源码；`guided` = 需要本机没有的工具链，课程不伪造输出。

## 为什么学这个？

- **AI 编译器的核心是选择**：融合哪些算子、什么时候复用 buffer、循环怎么排、用 C 还是 LLVM。只读文章，遇到性能或正确性问题仍然定位不了。
- **自己写一遍，才会真正理解 IR**：SSA、shape inference、pass 固定点、liveness、arena、ABI，这些词只有在你亲手写过一个 500 行左右的编译器后才会变成直觉。
- **两条后端路线各有价值**：C 后端让你看清循环和内存；LLVM 后端让你看清 IR、JIT 和目标机器。两边都做，才能比较取舍。
- **和已有课程互补**：[`ai_compiler`](../ai_compiler/README.md) 讲全貌与生产栈对照；[`mlir_core`](../mlir_core/README.md) 讲 MLIR 基础设施；本课补上「自己动手实现」这一段。

## 先修知识

- **Python**：能读类、dataclass、类型标注、上下文管理器。
- **PyTorch 基础**：知道 `nn.Module`、tensor、`eval()`、`torch.no_grad()`。
- **C 语言基础**：能读指针、数组、`for` 循环、函数签名。
- **编译原理概念**：知道 IR、pass、SSA、lowering 大概是什么；不要求写过编译器。
- **命令行与 PowerShell**：能建 venv、设置环境变量、调用 `cl`。

不要求：CUDA、TVM、MLIR、LLVM 源码经验。

## 课程地图

### Part 0 — 从模型到可执行 IR（Ch 0–5）

- [Ch 0 环境与最小闭环](./00-environment-setup.md)：venv、MSVC、生成 DLL、`ctypes` 调用
- [Ch 1 我们要造什么](./01-what-we-are-building.md)：架构、支持的算子子集、五个里程碑
- [Ch 2 图 IR 设计](./02-graph-ir.md)：`Value` / `Op` / `Graph`、SSA、拓扑序、IR dump
- [Ch 3 前端：FX → 自有 IR](./03-frontend-fx.md)：`symbolic_trace`、算子映射、`nn.Linear` 降级
- [Ch 4 张量语义](./04-tensor-semantics.md)：shape/dtype/stride、broadcast、view vs copy
- [Ch 5 参考解释器](./05-reference-interpreter.md)：用 NumPy 执行 IR，建立语义金标准
- [练习 A：图 IR 与解释器](./practice-a-ir-and-interpreter.md)

### Part 1 — 校验与图优化（Ch 6–10）

- [Ch 6 Verifier 与 shape/dtype 推导](./06-verifier-and-shape-inference.md)：SSA 检查、类型检查、错误定位
- [Ch 7 Pass 基础设施](./07-pass-manager.md)：PassManager、rewrite、固定点、调试输出
- [Ch 8 常量折叠、DCE、CSE](./08-constant-folding-dce-cse.md)：三件套的实现与边界
- [Ch 9 代数化简](./09-algebraic-simplification.md)：`x*1`、`x+0`、`relu(relu(x))` 与浮点语义
- [Ch 10 算子融合](./10-operator-fusion.md)：elementwise、matmul epilogue、reduction 融合与合法性
- [练习 B：实现优化流水线](./practice-b-graph-passes.md)

### Part 2 — 内存规划（Ch 11–15）

- [Ch 11 布局与 stride](./11-layout-and-strides.md)：contiguous、broadcast stride、布局转换
- [Ch 12 生命周期分析](./12-liveness-analysis.md)：last-use、liveness interval、输出值
- [Ch 13 内存规划器](./13-memory-planner.md)：offset、对齐、复用、峰值内存
- [Ch 14 Arena、in-place 与别名安全](./14-arena-and-alias-safety.md)：什么时候能复用，什么时候绝对不能
- [Ch 15 内存正确性验证](./15-memory-validation.md)：debug allocator、canary、复用冲突测试
- [练习 C：实现内存规划器](./practice-c-memory-planner.md)

### Part 3 — C 后端（Ch 16–20）

- [Ch 16 Lowering 到标量 C](./16-lower-to-c.md)：IR → C 函数、参数/返回值、临时变量
- [Ch 17 循环代码生成](./17-loop-codegen.md)：elementwise、broadcast、reduction 的索引生成
- [Ch 18 Matmul 与 tiling](./18-matmul-tiling.md)：朴素三重循环 → tile、loop order、cache
- [Ch 19 向量化与编译选项](./19-vectorization-and-flags.md)：对齐、`/O2`、MSVC 自动向量化
- [Ch 20 Runtime ABI 与 DLL](./20-runtime-abi-and-dll.md)：导出函数、`ctypes`、错误码、benchmark
- [练习 D：C 后端](./practice-d-c-backend.md)

### Part 4 — LLVM 与工程化（Ch 21–24）

- [Ch 21 LLVM IR 基础](./21-llvm-ir-basics.md)：类型、SSA、`phi`、`getelementptr`
- [Ch 22 llvmlite 后端](./22-llvm-backend.md)：自有 IR → LLVM IR → JIT，与 C 后端对照
- [Ch 23 自动调优 mini 版](./23-autotuning.md)：tile 参数搜索、成本模型、测量噪声
- [Ch 24 调试与差分测试](./24-debugging-and-differential-testing.md)：IR dump、miscompile 二分、随机图测试
- [Final Project：mini AI compiler](./final-project-mini-ai-compiler.md)

## 学习方式建议

1. **读完一章就动手**：把该章的代码片段放进 `code/` 里跑一遍，不要只看。
2. **故意改坏**：把 shape 改成不合法、把 buffer 复用条件放宽、把 bias 索引写错，观察 verifier 和差分测试怎么报错。
3. **每完成一个 Part 做一次对照**：解释器 vs C vs LLVM，三者结果必须一致；不一致时先怀疑自己的 IR 或索引。
4. **性能数字要带条件**：本课所有 benchmark 都注明 CPU、shape、编译选项和重复次数；小于 1.5 倍的差异不作为结论。

## 环境与版本

本机实测环境：

```text
Windows 10/11 x86_64
Python 3.11.12
torch 2.14.1+cpu
numpy 2.4.6
llvmlite 0.50.0
MSVC 14.43（cl）
```

复用已有环境：

```powershell
$py = ".scratch\aic-env\Scripts\python.exe"
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& $py -c "import torch, numpy, llvmlite; print(torch.__version__, numpy.__version__, llvmlite.__version__)"
```

从零建立环境：

```powershell
python -m venv .scratch\mini-ai-env
$py = ".scratch\mini-ai-env\Scripts\python.exe"
& $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
& $py -m pip install numpy llvmlite
```

C 编译需要 MSVC 环境。`code/mini_ai/codegen_c.py` 会自动查找本机 `vcvars64.bat`；也可以手动设置：

```powershell
$env:VCVARS64 = "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
```

## 运行课程代码

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& ".scratch\aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\demo_e2e.py"
```

端到端 demo 会打印原始 IR、优化后的 IR、内存计划、C 源码路径、DLL 路径、LLVM IR 行数、两种后端的误差和 benchmark。

跑测试：

```powershell
& ".scratch\aic-env\Scripts\python.exe" -m unittest discover -s "compilers\mini_ai_compiler\code\tests" -v
```

## 本课不深挖什么

- TVM schedule、`meta_schedule`、Relax 的完整体系（见 [`ai_compiler`](../ai_compiler/README.md)）。
- MLIR / StableHLO / TorchInductor / Triton 的内部实现（见 [`mlir_core`](../mlir_core/README.md) 与 A 课）。
- CUDA / GPU kernel 手写与调优。
- 量化、动态 shape、完整 ONNX 前端。
- 生产级错误恢复、并发编译、分布式 runtime。

这些不是「不重要」，而是本课的边界：我们只造一个能跑、能测、能讲清楚原理的 mini 编译器。

## 精选资料

### 必读基础

- **《Engineering a Compiler》** — Keith Cooper、Linda Torczon
  - IR、SSA、数据流分析、指令选择的标准教材；本课 Ch 2、Ch 6、Ch 12 的概念基础。
- **[LLVM Language Reference Manual](https://llvm.org/docs/LangRef.html)**
  - Ch 21–22 的权威来源；重点读 Types、Functions、`getelementptr`、`phi`。
- **[llvmlite 文档](https://llvmlite.readthedocs.io/)**
  - Ch 22 的 API 依据；注意 0.50 的初始化行为与旧版本不同。

### 推荐论文

- **[TVM: An Automated End-to-End Optimizing Compiler for Deep Learning](https://www.usenix.org/conference/osdi18/presentation/chen)** — OSDI 2018
  - **核心贡献**：把图级优化与 kernel 级调度分开，并提出 schedule 抽象。
  - **读哪里**：Section 3–5；本课 Ch 10、Ch 18 对应它的图融合与 tiling 思想。
- **[Ansor: Generating High-Performance Tensor Programs for Deep Learning](https://www.usenix.org/conference/osdi20/presentation/zheng)** — OSDI 2020
  - **核心贡献**：用搜索和成本模型自动找 tile/并行方案。
  - **读哪里**：Section 4–5；对应 Ch 23 的 mini autotune。
- **[MLIR: A Compiler Infrastructure for the End of Moore's Law](https://arxiv.org/abs/2002.11054)** — arXiv 2020
  - **核心贡献**：多层 dialect 与可组合 pass 基础设施。
  - **读哪里**：Section 2–4；读完再回看本课的 IR 设计，会知道它刻意简化了什么。

### 官方文档 / 源码

- **[PyTorch FX](https://pytorch.org/docs/stable/fx.html)**：Ch 3 的 `symbolic_trace`、`GraphModule`、`ShapeProp`。
- **[CPython `ctypes`](https://docs.python.org/3/library/ctypes.html)**：Ch 20 的 DLL 调用与指针参数。
- **[MSVC `/O2` 文档](https://learn.microsoft.com/cpp/build/reference/o1-o2-minimize-size-maximize-speed)**：Ch 19 的编译选项。
- **[TVM 源码](https://github.com/apache/tvm)**：读完本课后再看 `src/tir` 与 `src/target/llvm`，会更容易定位。

### 读完之后

- **[MLIR Toy Tutorial](https://mlir.llvm.org/docs/Tutorials/Toy/)**：把一个真实 dialect 从定义写到 lowering。
- **[LLVM Kaleidoscope](https://llvm.org/docs/tutorial/)**：用 LLVM 官方教程补 JIT、IRBuilder 和优化 pass。
- **[TinyTensor / micrograd 类项目](https://github.com/karpathy/micrograd)**：从更小的自动微分/张量库继续往下挖。

---

先跑通 Ch 0 的最小闭环，再决定要不要一次读完。编译器不是读会的，是改会、跑会、调错的。