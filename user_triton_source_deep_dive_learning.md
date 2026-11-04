---
name: triton_source_deep_dive-course-scope
description: Triton 3.9.0 源码深潜：34 章 + 5 练习 + 1 全栈 final；从 Python AST 到 TTIR/TTGIR/LLVM/PTX/SASS，目标能修改并维护 Triton。
metadata:
  type: project
---

`compilers/triton_source_deep_dive/` 是一门源码级 Triton 课程：不求“会写 kernel”，而求能读、改、调试、维护 Triton 编译器本身。主线是本机 Triton 3.9.0（commit `a972119e92`）的完整编译链，T4/sm75 作为可实测基线，Hopper/Blackwell/AMD 标 guided。

**Why:** 用户已有编译器前端/IR/LLVM/MLIR 背景，想彻底理解 Triton 源码，达到 AI 编译器开发维护水平；本机在 `/home/ubuntu/aicomp/triton` 有源码，在 `/home/ubuntu/llvm` 有 LLVM。课程用 `.claude/skills/learn` 的厚书规范，简体中文。

**How to apply:**
- 文件结构：README + Ch 0–33 + 练习 A–E + Final，共 41 个 Markdown 文件，合计 19,453 行；目标每章 350–550 行（实测 297–589），练习 284–468 行，final 520 行。
- Part 0（Ch 0–3）环境/全局地图/语言边界/端到端；Part 1（Ch 4–9）前端与运行时；Part 2（Ch 10–15）TTIR/TTGIR/layout/内存；Part 3（Ch 16–21）pass/pipeline/流水线/warp specialization/Blackwell；Part 4（Ch 22–28）LLVM/PTX/SASS lowering；Part 5（Ch 29–33）Gluon/AMD/正确性/测试/维护。
- 刻意涵盖：Python AST→TTIR、`TritonSemantic`、JIT/cache/launcher、TTIR/TTGIR/TritonNvidiaGPU、LinearLayout、RemoveLayoutConversions、Coalesce、AccelerateMatmul、Pipeliner、WarpSpecialization、TMA/TMEM/tcgen05、TritonGPUToLLVM、NVPTX/ptxas、Gluon、AMD、lit/pytest、上游贡献。
- 刻意不涵盖：CUDA 手写入门、PyTorch 图捕获全貌、完整 LLVM 后端教程、生产级 serving；这些分别由 `gpu_cuda`、`inductor_deep_dive`、`compiler_backend`、`ai_to_edge` 承担。
- 证据约定：`实测` / `源码依据` / `guided`；所有源码行号以 commit `a972119e92` 为准；T4 上 `tt.dot` 回退 FMA（不生成 MMAv2），SM90/SM100/AMD 路径不伪造运行结果。
- 教学信条：先 IR dump 再读 C++；每章至少一个“弄坏它”的实验；final 按上游 PR 标准写设计、测试、性能和回滚边界。

✅ **全部完成 (2026-11-04)**：Ch 0–33 + 练习 A–E + Final 共 41 个 Markdown 文件，19,453 行。全部章节通过结构 QA（15 个规定二级标题齐全、相对链接有效、无占位）。Ch 23/26 的 IR/PTX 与指令计数在 T4 + triton 3.9.0 wheel 上复现；源码行号以 commit `a972119e92` 核对。
