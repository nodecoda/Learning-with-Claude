---
name: mini_ai_compiler-course-scope
description: Hands-on mini AI compiler course (2026) — 25 chapters + 4 practices + C/LLVM backends, PyTorch FX frontend, own graph IR, passes, memory planning, C DLL and llvmlite JIT.
metadata:
  type: project
---

`compilers/mini_ai_compiler/` 是一门「动手造轮子」的 AI 编译器课程：从 PyTorch FX 抓图开始，自己定义图 IR、参考解释器、Pass、内存规划，再实现 C 后端和 LLVM 后端，最后用一个小型 MLP 做端到端数值对齐与 benchmark。教材简体中文，配套实现位于 `compilers/mini_ai_compiler/code/`。

**Why:** 用户在 A 课（全貌主线）之外明确选择方案 B：自己写一个 mini AI 编译器，范围约 22–26 章，理论覆盖比 A 窄，重点是实现。课程按确认的 25 章 + 4 练习 + Final 组织。

**How to apply:**
- 结构：README + Ch 0–24（25 章）+ 练习 A–D + Final，共 31 个课程 Markdown 文件；另有 `code/` 参考实现和测试。
- Part 0（Ch 0–5）：环境最小闭环、架构、图 IR、FX 前端、张量语义、参考解释器。
- Part 1（Ch 6–10）：verifier/shape inference、PassManager、常量折叠/DCE/CSE、代数化简、算子融合。
- Part 2（Ch 11–15）：layout/stride、liveness、内存规划、arena/别名安全、内存验证。
- Part 3（Ch 16–20）：C lowering、循环代码生成、matmul tiling、向量化/编译选项、DLL ABI/runtime。
- Part 4（Ch 21–24）：LLVM IR、llvmlite 后端/JIT、autotune、调试与差分测试。
- Final：FX → IR → Pass → 内存 → C/LLVM → runtime 的完整 mini compiler。
- 环境：Windows + Python 3.11.12 + torch 2.14.1+cpu + numpy 2.4.6 + llvmlite 0.50.0 + MSVC 14.43；复用 `.scratch\aic-env`。
- 实测：`unittest` 12/12 通过；MLP 的 C/LLVM `max_abs_error=4.470348358154297e-08`；`arena_bytes=320`；autotune 最佳记录 `(16,16,16) 0.2132 ms`。
- 边界：静态 shape、float32、CPU；C/LLVM 后端不支持 softmax（解释器支持）；不覆盖 CUDA、量化、动态 shape、完整 ONNX、TVM schedule。
- 关联：A 课 `compilers/ai_compiler/` 提供全貌与生产栈对照；本课只补「自己实现」这一段。