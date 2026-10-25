---
name: inductor_deep_dive-course-scope
description: Single-stack TorchInductor deep dive (2026) — 24 chapters + 4 practices + final. Reads compile_fx / scheduler / C++ codegen source, writes custom post-grad passes and lowerings, benchmarks vectorization and thread scaling on CPU.
metadata:
  type: project
---

`compilers/inductor_deep_dive/` 是一门「单栈深入」课程：不自己造编译器，而是钻进 PyTorch 自带的 TorchInductor，沿 `torch.compile` 的真实链路读源码、改 pass、写 lowering，并用生成物与 benchmark 验证。

**Why:** 用户在 A（全貌主线）、B（自己造 mini 编译器）之后选择方案 C：只选 TorchInductor 一个栈，要求更高、实验更重，范围约 24 章。

**How to apply:**
- 结构：README + Ch 0–23（24 章）+ 练习 A–D + Final，共 30 篇 Markdown；配套 `code/` 实验脚本。
- Part 0（Ch 0–3）：环境与基线、架构地图、三张图与边界、为什么是 loop-level IR。
- Part 1（Ch 4–8）：GraphLowering/IR、lowering 规则、post-grad pass 管线、自定义 pass、pattern matcher。
- Part 2（Ch 9–12）：调度器数据结构、融合决策、读调度 IR、内存规划。
- Part 3（Ch 13–17）：LoopBody/ops_handler、C++ kernel 生成、向量化、并行化、extern kernel 与模板。
- Part 4（Ch 18–20）：产物与 ABI、缓存体系、动态 shape 与 guards。
- Part 5（Ch 21–23）：性能工程、正确性与 fallback、改造 Inductor 的三种姿势与后续路线。
- 环境：Windows + Python 3.11.12 + torch 2.14.1+cpu + MSVC 14.43；统一入口 `code/tools/run.ps1`（导入 vcvars、固定 cache、venv）。
- 实测：lab_a 四件套产物（fx_graph_readable / ir_pre_fusion / ir_post_fusion / output_code）；lab_b 向量指令 5→2（relu-pair→abs，误差 0）；lab_c fallback vs lowering（1 kernel、tmp_ops=8）；lab_d vec≈7ms、scalar≈28ms、threads1≈14ms（4M float32，子进程隔离）；final kernel 数 1、tmp 5→2、误差 1.95e-3。
- 边界：只讲 CPU 后端（C++/OpenMP）；Triton/CUDA/AOTInductor 只作路线介绍；本机仅 AVX2，`cpp.simdlen=512` 会退回标量。
- 关联：`compilers/ai_compiler/` 提供全貌与生产栈对照；`compilers/mini_ai_compiler/` 提供自己动手实现；本课只钻 TorchInductor 一个栈。