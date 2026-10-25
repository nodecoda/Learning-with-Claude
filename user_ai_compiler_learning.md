---
name: ai_compiler-course-scope
description: AI compiler course (2026) — 31 chapters + 4 practices + mini C-codegen compiler final, PyTorch FX/torch.export + Apache TVM 0.27 CPU as main stack, Inductor/ORT/XLA/MLIR as comparisons.
metadata:
  type: project
---

`compilers/ai_compiler/` 是一门以「全貌主线」组织的 AI 编译器课程：从 PyTorch 图出发，经图级优化、调度、代码生成到 runtime，主力实验栈是 **PyTorch FX / torch.export + Apache TVM 0.27（CPU）**，并用 TorchInductor、ONNX Runtime、XLA/StableHLO、MLIR/IREE 做对照。教材简体中文，版本以本机实测为准。

**Why:** 用户想系统学习 AI 编译器，选择「全貌主线」而不是单栈深入或从零造轮子。课程按确认的方案：CPU 可跑、每章尽量实测、GPU 相关内容标 guided。

**How to apply:**
- 文件结构：README + Ch 0–30 + 练习 A–D + Final Project，共 37 个 Markdown 文件、约 17,400 行。
- Part 0（Ch 0–4）：环境与最小闭环、四层分工、图 IR 对照、张量语义、算子与融合边界。
- Part 1（Ch 5–10）：FX 变换、常量折叠/DCE/CSE、算子融合、布局、量化、内存规划。
- Part 2（Ch 11–17）：循环优化、TVM TIR/schedule、自动调优、codegen、向量化、Roofline、runtime。
- Part 3（Ch 18–22）：TorchInductor、Triton、XLA/StableHLO、MLIR/IREE、ONNX Runtime/TensorRT。
- Part 4（Ch 23–27）：动态 shape、并行、稀疏/低精度、正确性、性能工程。
- Part 5（Ch 28–30）：读 TVM 源码、设计取舍、transformer block 端到端复盘。
- Final：mini AI compiler（FX 图 → 自研 IR → 折叠/融合 → C 代码 → cl 编译 DLL → 数值对齐）。
- 环境：`.scratch\aic-env`（Python 3.11.12 / torch 2.14.1+cpu / tvm 0.27.0.post1 / onnx 1.23.2 / onnxruntime 1.31.0）；C 编译器用 VS2022 的 `cl`。
- 关键校正（实测）：TVM 0.27 无 `te.create_schedule`、`tvm.nd.array`、`sch.get_block`、`tvm.tir`、`get_source()`；正确写法是 `te.create_prim_func` + `tvm.s_tir.Schedule`、`sch.get_sblock`、`tvm.runtime.tensor/empty`、`inspect_source()`。ONNX 导出需 `PYTHONIOENCODING=utf-8` 且 opset ≥ 18。
- 刻意涵盖：真实 IR/图/生成代码、真实性能数字（注明机器与样本）、失败路径与报错原文。
- 刻意不涵盖：CUDA kernel 手写与 GPU 调优、训练框架内部、芯片后端、生产级 serving。
- 教学信条：先直觉后形式化；版本特定行为标注 0.27/2.14；可跑输出优先；无法实跑时明确标注 guided，不伪造。

✅ 全部完成（2026-10-24）：31 章 + 练习 A–D + Final，37 个 Markdown 文件、约 17,400 行；根 README 索引与 MEMORY 已同步。