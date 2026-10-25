---
name: mlir_core-course-scope
description: MLIR 23.1.4 course — 32 chapters + 4 practices + mini-frontend final, IR-level experiments plus out-of-tree C++ builds, MLIR → LLVM dialect → LLVM IR → llc.
metadata:
  type: project
---

`compilers/mlir_core/` 是一门以「全貌主线」组织的 MLIR 课程：从 IR 对象模型出发，经 Pass/Pattern、ODS 自定义方言、Dialect Conversion、Bufferization，一路降到 LLVM dialect 与 LLVM IR，最后用 `llc` 接到机器层。教材以 MLIR 23.1.4 为准，简体中文。

**Why:** 用户想系统理解 MLIR 的整体结构与降级链路，而不是只背 API 或只看某一个 dialect。课程按用户确认的方案选择「全貌主线」、复用本机已有的 MSVC C++ 工具链，并要求每章尽量可跑、可验证。

**How to apply:**
- 文件结构：README + Ch 0–31 + 练习 A–D + Final Project，共 38 个 Markdown 文件、约 19,100 行。
- Part 0（Ch 0–4）：环境与最小闭环、MLIR 定位、Operation/Block/Region/Value、类型与属性、47 个 dialect 的分层地图。
- Part 1（Ch 5–10）：PassManager、分析框架、RewritePattern/canonicalize、Dialect Interface、安全改 IR、调试与 lit。
- Part 2（Ch 11–16）：ODS、约束与 verifier、TableGen 生成物、自定义 Type/Attribute、Trait/Interface、注册与 bytecode。
- Part 3（Ch 17–22）：Dialect Conversion、TypeConverter、动态 legality、Bufferization、降到 LLVM dialect、`mlir-translate` + `llc`。
- Part 4（Ch 23–27）：Linalg、Affine、Tensor/Vector/GPU、Transform dialect、工程化（out-of-tree CMake、lit、构建成本）。
- Part 5（Ch 28–31）：读源码、设计取舍、性能与规模、端到端复盘。
- Final：C++ 写的 mini frontend（`.mfl`）→ MLIR → LLVM IR → `.obj` → 可执行文件，已实际构建运行。
- 关键校正（23.1.4 实测）：通用打印是 `--mlir-print-op-generic`；IR dump 走 stderr；`index` 默认降 `i64`，`index-bitwidth=32` 必须同时给 func/cf/arith 三个 conversion；`convert-cf-to-llvm` 是 module 级，而 `convert-scf-to-cf` 在 23.1.4 可嵌套进 `func.func`；本机没有 `convert-memref-to-llvm`，只有 `finalize-memref-to-llvm`；transform 只有 `--transform-interpreter`，没有 test 版解释器；canonicalize 不是 DCE；tensor 上的 `convert-linalg-to-loops` 会静默跳过。
- 刻意涵盖：真实 IR dump、真实错误诊断、out-of-tree 构建与 lit 套件、端到端产物。
- 刻意不涵盖：完整生产级前端、Linalg 全变换集、GPU 二进制生成（缺 CUDA/ROCm）、MLIR Python bindings、LLVM backend 内部（属 `globalisel_*` 课程）。
- 教学信条：先直觉后形式化；版本特定行为标注 MLIR 23.1.4；可跑输出优先于想象；无法实跑时明确标注源码依据或 guided。

✅ 全部完成（2026-10-24）：32 章 + 练习 A–D + Final Project，38 个 Markdown 文件、约 19,100 行；根 README 索引与 MEMORY 已同步。