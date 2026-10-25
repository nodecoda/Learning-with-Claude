# TorchInductor 深潜：从 Dynamo 图到 CPU kernel

> 给已经会用 `torch.compile`、想读懂它到底生成了什么、并且愿意动手改一次 pass / lowering / 调度决策的工程师。全课以本机 torch 2.14.1+cpu、Windows x86_64、AVX2 为基线，所有关键数字都有可复现命令。

这门课不教你“如何调用 `torch.compile`”，而是带你沿着一次真实编译走完六段：Dynamo 抓图、AOTAutograd 规范化、post-grad pass、GraphLowering、Scheduler、C++ codegen。每一段都落到 `torch/_inductor` 的源码函数和 `code/.cache` 里的真实产物上，最后用一个可验证的编译期改写收尾。

全课 **24 章 + 4 个练习 + 1 个 Final Project + 本 README，共 30 篇**。配套代码在 [`code/`](./code/)，统一入口是 `code/tools/run.ps1`。

> 证据约定：`实测` = 本机当前版本的真实输出；`源码依据` = 对照 torch 2.14.1+cpu 的函数与调用关系；`guided` = 需要 GPU、源码构建或额外工具链，课程只给步骤，不伪造输出。

## 为什么

`torch.compile` 最容易制造一种错觉：代码跑通了、形状对了、甚至更快了，你就以为自己理解了它。实际可能发生的是：

- 图在某个 `.item()` 处断成两半，你只编译了一半；
- 自定义算子没有 lowering，悄悄留在 wrapper 里走 eager；
- 矩阵乘走 `extern_kernels.addmm`，你却在生成代码里找“为什么没有 matmul kernel”；
- 改了 config 但没包住第一次调用，测到的仍是默认行为；
- baseline 和 optimized 共用缓存，benchmark 数字来自旧 kernel；
- 浮点归约顺序变了，1.95e-3 的误差被误判成 bug，或者被过大的容差掩盖成“通过”。

这些问题无法靠背 API 解决，只能靠“读产物 + 做对照 + 写最小改造”建立直觉。本课的选择是：不自己造一个玩具编译器，而是直接读生产级 TorchInductor，在安全的扩展点上改它，再用结构、数值和时间三类证据验证。

## 主线

### 1. 课程定位：读真实的编译器，而不是复刻一个

| 维度 | 本课的选择 |
|---|---|
| 对象 | PyTorch 2.x 的 TorchInductor（CPU 后端为主） |
| 方法 | 读源码 + 跑实验 + 改 pass/lowering + 差分测试 |
| 产物 | FX 图、调度 IR、C++ kernel、`.pyd`、cache、报告 |
| 深度 | 从 `torch.compile` 入口到 `output_code.py` 的具体行 |
| 边界 | CPU/AVX2 实测；GPU、Triton、AOTInductor 只给路线 |

学完你应该能：

- 拿到任意一个 `torch.compile` 产物，判断它融合了什么、哪里 fallback、哪里 extern；
- 写一个 post-grad pass，把等价模式改写成更少指令的形式，并证明它没算错；
- 注册一个自定义 lowering，让原本留在 wrapper 里的算子进入融合 kernel；
- 用子进程隔离和多轮测量，给出带条件的性能结论；
- 用 config、monkeypatch、`site-packages` 三种姿势改造 Inductor，并知道何时停手、转向上游。

### 2. 先修知识

| 领域 | 需要到什么程度 | 不要求 |
|---|---|---|
| Python | 类、装饰器、上下文管理器、`subprocess`、`pathlib` | 元类、异步 |
| PyTorch | tensor、stride、broadcast、`nn.Module`、`torch.compile` 基本用法 | 分布式、量化 |
| 编译原理 | IR、pass、lowering、融合、SSA 的基本概念 | 写过完整编译器 |
| C++ | 能读循环、指针、`#pragma omp`、模板的基本语法 | 手写高性能 kernel |
| 系统 | PowerShell、环境变量、MSVC `cl.exe` | Windows 内核/驱动 |
| 测量 | 均值/中位数、warmup、噪声的基本概念 | 统计推断 |

不要求 CUDA、TVM、MLIR 或 LLVM 源码经验。需要 GPU 的内容会明确标成 `guided`，不会用 CPU 结果冒充。

### 3. 课程地图：7 个 Part，30 篇

#### Part 0 — 环境、全景与三张图（Ch 00–03 + 练习 A）

- [Ch 00 环境与基线](./00-environment-and-baseline.md)：`run.ps1`、`env_report`、首次编译、cache、AVX2
- [Ch 01 架构地图](./01-inductor-architecture-map.md)：一次编译的六段，`lab_a` 四份产物逐段对照
- [Ch 02 三张图与边界](./02-three-graphs-and-boundaries.md)：FX/Dynamo 图、core ATen 图、loop-level IR、guards、graph break
- [Ch 03 为什么 loop-level IR](./03-why-loop-level-ir.md)：与 TVM te/tile、MLIR Linalg、Triton block 的取舍
- [练习 A：管线追踪](./practice-a-pipeline-trace.md)：用 `lab_a` 产物写 6 段对照注释与自测题

#### Part 1 — GraphLowering 与 IR（Ch 04–06）

- [Ch 04 GraphLowering 与 IR](./04-graph-lowering-and-ir.md)：`TensorBox`/`StorageBox`/`ComputedBuffer`、layout、buffer 生命周期
- [Ch 05 lowering 规则](./05-lowering-rules.md)：`register_lowering`、type promotion、broadcast、`fallback_handler`
- [Ch 06 post-grad pass 管线](./06-post-grad-pass-pipeline.md)：DCE、reorder、custom pre/post、pattern、`GraphTransformObserver`

#### Part 2 — 改图：pass 与 pattern（Ch 07–08 + 练习 B）

- [Ch 07 写自定义 pass](./07-write-a-custom-pass.md)：`lab_b` 全流程、`Graph` 不是 `GraphModule`、DCE、lint
- [Ch 08 pattern matcher 与融合](./08-pattern-matcher-and-fusions.md)：`pass_patterns`、`register_replacement`、`post_grad_fusion_options`
- [练习 B：自定义 pass](./practice-b-custom-pass.md)：再实现一个等价改写，要求差分测试 + 结构断言

#### Part 3 — 调度器与融合（Ch 09–11）

- [Ch 09 调度器数据结构](./09-scheduler-data-structures.md)：`SchedulerNode`、`FusedSchedulerNode`、`ExternKernelSchedulerNode`、`MemoryDep`
- [Ch 10 融合决策](./10-fusion-decisions.md)：`can_fuse_vertical/horizontal`、reduction 融合限制、`TORCH_LOGS=fusion`
- [Ch 11 读调度 IR](./11-reading-scheduler-ir.md)：`ir_pre_fusion.txt` / `ir_post_fusion.txt`、`var_ranges`、index、loop body

#### Part 4 — 内存与 C++ codegen（Ch 12–16）

- [Ch 12 内存规划](./12-memory-planning.md)：`MemoryPlanningInfoForBuffer` / `MemoryPlanningState`、buffer 复用、`reuse`/`del`、peak live、对齐
- [Ch 13 LoopBody 与 ops_handler](./13-loop-body-and-ops-handler.md)：IR → loop nest、`DefaultHandler`、`CppOverrides`
- [Ch 14 C++ kernel 生成](./14-cpp-kernel-codegen.md)：`CppKernel`、`CppKernelProxy`、`CppScheduling`、kernel 命名与广播
- [Ch 15 向量化](./15-vectorization.md)：`CppVecKernel`、`Vectorized<T>`、tail loop、`simdlen` 是位宽
- [Ch 16 并行化](./16-parallelization.md)：OpenMP pragma、threads、collapse、reduction 原子与确定性

#### Part 5 — extern kernel、产物与缓存（Ch 17–20 + 练习 C）

- [Ch 17 extern kernel 与模板](./17-extern-kernels-and-templates.md)：`addmm`/`mm`/`bmm`、oneDNN、`cpp_gemm_template` 的条件
- [Ch 18 产物与 ABI](./18-artifacts-and-abi.md)：`output_code.py`、cpp→pyd、pybind、调用开销
- [Ch 19 缓存体系](./19-cache-system.md)：`FxGraphCache`、`PyCodeCache`、`CppCodeCache`、`AsyncCompile`、cache key
- [Ch 20 动态 shape 与 guards](./20-dynamic-shapes-and-guards.md)：symbolic int、`automatic_dynamic_shapes`、recompile 计数
- [练习 C：自定义 lowering](./practice-c-custom-lowering.md)：给自定义算子加第二个 lowering，比较生成代码

#### Part 6 — 性能、正确性与改造（Ch 21–23 + 练习 D + Final）

- [Ch 21 性能工程](./21-performance-engineering.md)：结构优先、交错测量、子进程隔离、compile time vs runtime、噪声区间
- [Ch 22 正确性与 fallback](./22-correctness-and-fallbacks.md)：差分测试、容差、边界 shape、graph break、fallback 识别
- [Ch 23 改造 Inductor 与后续路线](./23-modify-inductor-and-next-steps.md)：config → monkeypatch → `site-packages`、上游贡献、AOTInductor/Triton/MLIR
- [练习 D：性能扫掠](./practice-d-performance-sweep.md)：在 `lab_d` 上加转置/切片布局、多轮测量与报告
- [Final Project：可验证的编译期改造](./final-project-inductor-deep-dive.md)：`relu-pair -> abs` 复现，再换一个等价改写交报告
- [课程 README](./README.md)：本页

### 4. 运行方式

所有命令都在仓库根目录 `D:\Learning-with-Claude` 执行，统一入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本>
```

`run.ps1` 做四件事：

1. 从 `D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat` 导入 MSVC 环境（可用 `$env:VCVARS` 覆盖）；
2. 检查 `cl.exe` 是否可见；
3. 设置 `TORCHINDUCTOR_CACHE_DIR=compilers\inductor_deep_dive\code\.cache`、`PYTHONPATH=code`、`PYTHONIOENCODING=utf-8`；
4. 用 `.scratch\aic-env\Scripts\python.exe` 运行脚本（可用 `$env:IND_PY` 覆盖）。

环境自检：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

推荐顺序：

| 阶段 | 命令（脚本路径省略前缀） | 预计 |
|---|---|---|
| 环境 | `code\tools\env_report.py` | 首次编译 5–15 秒 |
| 管线 | `code\labs\lab_a_pipeline.py` | 生成 FX/IR/C++ 产物 |
| pass | `code\labs\lab_b_pass.py` | `relu-pair -> abs` |
| lowering | `code\labs\lab_c_lowering.py` | fallback vs 融合 |
| 性能 | `code\labs\lab_d_perf.py` | 约 1 分钟 |
| Final | `code\labs\final_project.py` | 两个子进程 + 报告 |

首次编译慢是正常的；之后命中 `code/.cache` 会快很多。需要额外调试产物时加 `-DebugArtifacts`，它会把产物复制到仓库根的 `torch_compile_debug/`。

### 5. 证据约定

本课所有数字都带条件，阅读时先看条件，再看结论：

| 标记 | 含义 | 例子 |
|---|---|---|
| 实测 | 在本机当前版本真实运行得到 | `tmp 5→2`、`max err 1.95e-3`、`step=8` |
| 源码依据 | 对照 torch 2.14.1+cpu 的函数名与调用关系 | `fallback_handler`、`GraphLowering.run` |
| guided | 需要 GPU/源码构建/额外工具链，课程不伪造输出 | Triton、AOTInductor 打包、上游测试 |

性能数字的额外约定：

- 单轮结果只用于同一会话内的相对比较；不写“稳定加速”；
- 同一变体重复测量的漂移在本机可达 20%–50%；
- 变体之间必须子进程隔离 + 独立 cache，否则可能测到旧产物；
- 先看结构（kernel 数、tmp、step、omp），再看时间；
- 区间重叠时写“不可区分”，不强行排名。

### 6. 与其他课程的分工

| 课程 | 它负责什么 | 和本课的关系 |
|---|---|---|
| [`compilers/ai_compiler/README.md`](../ai_compiler/README.md) | AI 编译器的全貌、生产栈对照、更多后端与优化主题 | 先建立广度；本课把 TorchInductor 这一段放大到源码级 |
| [`compilers/mini_ai_compiler/README.md`](../mini_ai_compiler/README.md) | 从零造一个能跑通 C/LLVM 的 mini 编译器 | 先亲手实现 IR/pass/内存规划；本课读生产实现时更容易定位概念 |
| [`compilers/mlir_core/README.md`](../mlir_core/README.md) | MLIR/StableHLO/Linalg 等基础设施与 pass 组合 | 本课第 03 章对比 loop-level IR；想走 MLIR 路线接着读它 |
| 本课 | 读并改造 TorchInductor 的 CPU 编译链 | 聚焦一次真实 `torch.compile` 的六段与可验证改造 |

建议路径：

- **只想读懂产物**：Ch 00 → 01 → 02 → 04 → 09 → 11 → 21；
- **想写 pass/lowering**：上面的路径 + Ch 05 → 06 → 07 → 08 → 练习 B/C；
- **想做性能**：Ch 09 → 10 → 11 → 14 → 15 → 16 → 21 → 练习 D；
- **想改 Inductor/做贡献**：全课 + Ch 22 → 23 → Final。

## 可跑实验

最小闭环（环境自检 + 一次真实编译）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

四个核心实验：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py
```

跑完先看产物，而不是先看时间：

```powershell
Get-ChildItem -Recurse compilers\inductor_deep_dive\code\.cache -Filter output_code.py
Get-Content -Encoding UTF8 compilers\inductor_deep_dive\code\.cache\final_report.md
```

如果 `cl.exe` 找不到，先确认 `run.ps1` 里的 vcvars 路径；如果编译结果和 eager 不一致，先清 `code\.cache` 再重跑，排除旧 kernel。完整排错见 Ch 00 与 Ch 22。

## 源码落点

| 文件 | 函数 / 类 | 对应章节 |
|---|---|---|
| `torch/_inductor/compile_fx.py` | `compile_fx`、`compile_fx_inner`、`fx_codegen_and_compile` | 01、18 |
| `torch/_inductor/graph.py` | `GraphLowering`、`GraphLowering.run`、`graph_codegen` | 04、05 |
| `torch/_inductor/lowering.py` | `register_lowering`、`fallback_handler`、`make_pointwise` | 05、17 |
| `torch/_inductor/fx_passes/post_grad.py` | `post_grad_passes` | 06、07 |
| `torch/_inductor/scheduler.py` | `Scheduler`、`compute_dependencies`、`fuse_nodes`、`SchedulerNode` | 09、10 |
| `torch/_inductor/ir.py` | `TensorBox`、`StorageBox`、`ComputedBuffer`、`FixedLayout`、`FlexibleLayout` | 04、11、12 |
| `torch/_inductor/loop_body.py` | `LoopBody` | 13 |
| `torch/_inductor/ops_handler.py` | `OpOverrides`、`DefaultHandler` | 13 |
| `torch/_inductor/codegen/cpp.py` | `CppKernel`、`CppVecKernel`、`CppKernelProxy`、`CppScheduling` | 14、15、16 |
| `torch/_inductor/codegen/wrapper.py` | `WrapperCodeGen`、`PythonWrapperCodegen` | 18 |
| `torch/_inductor/memory.py` | `MemoryPlanningInfoForBuffer` / `MemoryPlanningState` | 12 |
| `torch/_inductor/codecache.py` | `PyCodeCache`、`CppCodeCache` | 18、19 |
| `torch/_inductor/cpu_vec_isa.py` | `pick_vec_isa`、`valid_vec_isa_list`、`VecAVX2` | 15 |
| `torch/_inductor/metrics.py` | `generated_kernel_count`、`generated_cpp_vec_kernel_count` | 21、22 |

源码锚点以函数名为主，不写死行号：行号会随版本漂移，函数名和调用关系更稳定。定位某个符号时用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\find_source.py torch._inductor.scheduler Scheduler.fuse_nodes
```

## 常见坑

1. **用系统 Python 而不是课程 venv。** torch 版本、MSVC 环境和依赖都可能不同；统一用 `run.ps1`，它默认指向 `.scratch\aic-env`。
2. **`cl.exe` 不在 PATH。** 直接运行脚本会找不到编译器；`run.ps1` 会导入 vcvars，手动跑脚本时先执行 `vcvars64.bat`。
3. **缓存污染。** 改了 config/pass 却看到旧结构，先清 `code\.cache` 或用 `fresh_inductor_cache()`；跨变体用独立子进程。
4. **把单轮时间当结论。** 本机漂移 20%–50%；至少多轮、看区间，先看结构再看时间。
5. **config patch 没包住第一次调用。** `torch.compile` 是惰性的，回调不会执行。
6. **期待 matmul 被融合成自研 kernel。** `x @ w + b` 走 `extern_kernels.addmm`；这不是 fallback，是设计选择。
7. **在本机 AVX2 上设置 `simdlen=512`。** 512 是位宽，本机没有 AVX512，会退回标量；这是预期行为。
8. **直接改 `site-packages` 不备份。** 升级即丢失、同事不可复现；按 Ch 23 的备份/回滚流程做。
9. **把 `torch.ops.*` 和 `extern_kernels.*` 混为一谈。** 前者常是未 lowering 的 fallback，后者可能是刻意的外部库调用；判读方式不同。
10. **在报告里只写“更快”。** 没有 shape、dtype、stride、线程、轮次、区间和误差，结论不可复现。

## 自我检查

- [ ] 我能说出一次 `torch.compile` 的六段，以及每段对应的源码入口。
- [ ] 我能从 `output_code.py` 判断 kernel 数、向量化、omp 和 extern 调用。
- [ ] 我能写一个 post-grad pass，并证明它真的改变了图结构。
- [ ] 我能注册 lowering，让自定义算子从 fallback 进入融合 kernel。
- [ ] 我能解释 `ir_pre_fusion` / `ir_post_fusion` 里的 `var_ranges`、index 和 loop body。
- [ ] 我能用子进程隔离和多轮测量给出带条件的性能结论。
- [ ] 我能区分逐元素精确改写和需要归约容差的改写。
- [ ] 我能用 config、monkeypatch、`site-packages` 三种姿势改造 Inductor，并知道何时停手。
- [ ] 我知道本课哪些内容是 `实测`，哪些是 `源码依据`，哪些只是 `guided`。
- [ ] 我能把一次改造写成包含正确性、结构、时间、限制和复现命令的报告。

## 延伸阅读

- **PyTorch 官方文档，`torch.compile`**：读编译流程、后端、动态 shape 和调试入口；这是全课的地图。
- **PyTorch 官方文档，TorchInductor**：读 CPU/GPU 后端、config 和性能调优；配合第 01、18、19 章。
- **PyTorch 官方文档，Torch FX**：读 `Graph`、`Node`、`GraphModule`、`interpreter`；第 07、08 章的 API 基础。
- **PyTorch 源码，`torch/_inductor`**：按本页“源码落点”表逐个函数读；不要从第一行读到尾，带着问题跳读。
- **PyTorch 开发者文档，`CONTRIBUTING.md` 与 `test/inductor/`**：读测试组织、lint、PR 要求；第 23 章的上游流程以它为准。
- **`compilers/mini_ai_compiler/README.md`**：先自己造一个小编译器，再回读 Inductor；IR、pass、内存规划的概念会更容易落地。
- **`compilers/ai_compiler/README.md` 与 `compilers/mlir_core/README.md`**：补全 AI 编译器全貌与 MLIR 基础设施，理解 Inductor 在更大的技术版图中的位置。
- **PyTorch 论文与 TorchInductor 设计资料**：读 fusion-first、loop-level IR、模板后端的设计取舍；对照第 03 章。

---

[上一章：Final Project](./final-project-inductor-deep-dive.md)