---
name: nvptx_backend-course-scope
description: LLVM NVPTX backend deep dive (2026) — 20 chapters + 5 practices + end-to-end final, reading llvm/lib/Target/NVPTX against the real ct-instr-trunk build, CUDA 12.4 and a Tesla T4.
metadata:
  type: project
---

`compilers/nvptx_backend/` 是一门以「PTX 发射器」为主线的教材：把 `llvm/lib/Target/NVPTX` 从定位、PTX 契约、ABI、地址空间、IR pass、ISel、寄存器/栈帧/调度、AsmPrinter，一路读到 `ptxas` 与自研 GPU 的能力边界。

**Why:** 使用者已熟悉 LLVM 全管线（IR → MIR → MC → .s/.o，含指令选择、调度、target bring-up、pass 编写），要面向「自研 GPU + CUDA 兼容编译器」岗位补 GPU/PTX 后端知识。已有 `parallel/gpu_cuda`（CUDA 使用者视角）与 `triton_source_deep_dive`（AI 编译器上层视角），缺的是「LLVM 里 GPU target 怎么建模、哪些事它不做」这一层。

**How to apply:**

- 文件结构：`README.md` + Ch 00–19（20 章）+ 练习 A–E + Final，共 27 篇。
- Part 0（00–01）：环境与全链路、NVPTX 定位（无 regalloc / 无 SchedModel / 无 object emission）。
- Part 1（02–05 + A）：PTX 类型与状态空间、版本/目标门控、kernel ABI、地址空间与 `cvta`。
- Part 2（06–10 + B）：clang device 前端与 NVVM IR、IR 预处理 pass 群、函数 lowering、类型合法化、ISel。
- Part 3（11–14 + C）：寄存器类与「没有分配」、栈帧/local memory、调度、内存与向量化。
- Part 4（15–17 + D）：AsmPrinter/MC、ptxas 与 SASS、扩展路径与自研 GPU 缺口。
- Part 5（18–19 + E）：PTX 内存一致性模型（scope/ordering/fence，IR→PTX→SASS 对照）、SASS 128 位编码与调度控制位（`nvdisasm -hex` 差分）。
- 基线：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，LLVM `24.0.0git`，commit `881d51b5cb17`（2026-11-17）；构建 `/home/ubuntu/llvm/builds/ct-instr-trunk`（assertions，含 NVPTX）；CUDA 12.4（`/usr/local/cuda`）；Tesla T4（sm_75，驱动 535.216.01）。
- 实测基线：`llc -mtriple=nvptx64` 默认 `sm_75` + `.version 6.3`；clang device-only 默认 `+ptx84`；`llc -filetype=obj` 报 `createMCCodeEmitter failed`；`llvm-mc -triple=nvptx64` 不支持汇编；`llc → ptxas → cubin → driver` 在 T4 上 1024 元素 vadd 全部正确；`ptxas -v` 显示 `REG:12`。
- 教学纪律：每章「目标 → 为什么 → 直觉 → 主线 → 源码落点 → 实测 → 踩雷 → 进阶 → 练习 → 自检 → 延伸阅读 → 下一章」；产物必须本机真跑，未跑的（如重编 LLVM 加 intrinsic）明确标 guided。
- 刻意不涵盖：CUDA 语言本身、Triton/CUTLASS 源码、ptxas 内部算法、完整 PTX ISA（只覆盖读后端的必要子集）；SASS 只讲可实测的 128 位编码/控制位观察，不给未经验证的位表。
- 与其他课的关系：`parallel/gpu_cuda` 提供 CUDA/微架构基础；`triton_source_deep_dive` 提供「LLVM 之上的 GPU 优化」；`compilers/compiler_backend` 与 `llvm_isel_sched_riscv` 提供 CPU 后端对照。
- 状态：已完成 (2026-11-18)，27 篇，约 4.9k 行。
