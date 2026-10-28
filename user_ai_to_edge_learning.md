---
name: ai_to_edge-course-scope
description: 从 PyTorch 大模型到边缘设备后端全链路：AI 编译器（图/调度/代码生成）→ MLIR → LLVM → AArch64/Cortex-M/RISC-V，30 章 + 4 练习 + 端到端 final，简体中文。
metadata:
  type: project
---

`compilers/ai_to_edge/` 是一门「全链路打通」的课程：用户要的不是「AI 编译器概览」或「LLVM 后端细节」中的任一个，而是**把一个模型一路编译到边缘设备并跑出正确数字**的完整过程，且要有例子。

**Why:** 用户明确要求「完整的、把 AI 编译器编译大模型到把编译器使用 LLVM 编译到边缘设备后端的全过程，并举例子，使用简体中文」。仓库已有 `ai_compiler`（图级偏重）、`mini_ai_compiler`（自造 mini 编译器）、`inductor_deep_dive`（Inductor 单栈）、`riscv_backend`/`mlir_core`（LLVM/MLIR 后端偏重），但**没有一门课把两端接起来**并真实落到设备上。本课填这个缺口，定位是「接缝课」，不重复既有课的深度。

**How to apply:**
- 文件结构：README + Ch 0–29 + 练习 A–D + Final Project，共 36 个 Markdown（含 README）。
- Part 0（Ch 0–2）：可复现工具链、七次变身全景、边缘设备约束级联。
- Part 1（Ch 3–5）：nn.Module / FX Graph / ExportedProgram 三种表示、Dynamo 抓图与 guard、动态 shape。
- Part 2（Ch 6–11）：Inductor 全链路、图级优化、调度、三条代码生成出口、MLIR 把算子变循环、量化位置。
- Part 3（Ch 12–17）：MLIR→LLVM IR、pass 管线、Triple/DataLayout、合法化与指令选择、寄存器与栈帧、汇编/目标文件/重定位。
- Part 4（Ch 18–24）：AArch64、Cortex-M 裸机、RISC-V、ABI（memref 变七参数）、内存规划、算子库 vs 代码生成、最小运行时。
- Part 5（Ch 25–29）：数值对齐、设备上测性能、按现象反推错层、生产栈对照（ExecuTorch/TVM/IREE/ORT/TFLite）、决策表。
- Final：同一模型编译到 AArch64 与 Cortex-M 两条路径并跑通对齐。
- 实测基线（本机 Ubuntu 22.04 x86_64，全部真跑过）：LLVM 14.0.0（全 target）、MLIR 14（**`func` 不是 `func.func`**）、torch 2.9.1+cpu、aarch64-linux-gnu-gcc 11.4、arm-none-eabi-gcc 10.3、riscv64-linux-gnu-gcc 11.4、qemu 6.2。
- 关键实测事实（写章节时不要退回推测）：`x @ w + 1.0` 在 `torch.export` 是 matmul+add+relu **三个**节点，只有显式 `torch.addmm` 才是一个节点；Cortex-M3 上 `float` 乘加在裸链接时**缺 `__aeabi_fmul`/`__aeabi_fadd` 符号**（不是变慢）；`a*b+1` 在 Cortex-M3 生成 `muls`+`adds` 而非 `mla`；`linalg.matmul` 降级后 `memref` 变成 **7 个标量参数**；MLIR 生成的 LLVM IR 即使源 IR 无堆分配也会带 `declare i8* @malloc`。
- 刻意涵盖：真实中间产物（FX graph、Inductor C++ kernel、MLIR 各降级阶段、AArch64/RV64/Thumb-2 汇编、ELF 结构）、qemu 实跑数值、真实踩坑（UART 寄存器映射错、ABI 错、wheel 命名）。
- 刻意不涵盖：GPU 后端（无 GPU）、完整生产级 runtime、训练侧编译、CUDA/Triton kernel 优化、LLVM 源码级后端开发（属 `riscv_backend`/`globalisel_*`）。
- 教学信条：先跑通再理解；每段输出必须实测，跑不了就标 `guided`；用「实测纠正」小节推翻常见误解。

## 完成状态

✅ 全部完成 (2026-10-28)：30 章（Ch 0–29）+ 4 个练习（A–D）+ 1 个端到端 Final，共 36 个 Markdown、14,666 行。全部章节为简体中文（繁体字扫描为 0），代码围栏与 `<details>` 全部配对。

可复现实验目录 `compilers/ai_to_edge/code/`，共 17 个子目录，冒烟测试全部通过：
- `01_hello_llvm`、`02_mlir_lowering`（MLIR→AArch64→qemu 数值正确）、`04_aarch64`、`05_cortexm`（qemu 打印 `cortex-m3 alive` / `42`）、`06_riscv`、`11_trace`、`12_newbackend`、`14_startup`（`data ok` / `bss ok`）、`08_quant`（7 个实验）、`10_perf`（5 个基准）、`13_arena`、`03_inductor`、`04_capture`、`05_inductor_edge`、`06_elemwise`、`07_export`。

本轮新增的可复现实测（都已写进对应章节，数字可复跑）：
- Ch 11：`int8×int8→int32` 全链路（`int32 accum == float ref : yes`、`int16` 累加 `K=256` 溢出为 256）；per-tensor 0.00879 vs per-channel 0.00577 的相对误差；**torch 2.9.1 上 `quantize_dynamic` 对 `nn.Linear` 静默不生效**（weight 仍 float32、无 `_packed_params`、forward 逐位相同，四个 backend 一致）。
- Ch 19：向量表入口 `0x6d = 0x6c|1`（Thumb 位）；`.data` 的 VMA `0x20000000` / LMA `0x181`；去掉 `.data` 拷贝后输出 `data BAD`；**`-nostdlib` 后缺 `memcpy`/`memset`，且自写 `memset` 在不加 `-fno-builtin` 时会 `bl` 到自己**（实测反汇编 + 静默卡死）。
- Ch 22：arena vs malloc 实测（纯分配 31 倍、带 memset 只剩 1.42 倍、`SMALL=64` 时反转回 6 倍）；并抓到「不加 `sink` 时编译器删掉整个分配循环」（0.154 ms vs 72 ms）。
- Ch 26：`ind2.py` / `first_vs_steady.py`（首次 1.9–3.3 s vs 稳态 0.08–0.31 ms）；`bench_full.py` 的 min/median/max；**`perf` 在 `perf_event_paranoid=4` 下连 `task-clock` 都 exit 255**；`alloc_cost.py` 分配开销对比：`relu(x*2+1)` 112.51 ms / 三段预分配 17.11 ms / 原地 2.65 ms（同一浮点运算量，41 倍）。
- Ch 27：12 条诊断全部本机复现（`optnone` 挡 opt：alloca 3→3 vs 3→0；MLIR 残留 6 个 cast 导致 `missing LLVMTranslationDialectInterface`）。

已知边界（已在章节内标注）：Cortex-M/AArch64 的**绝对时序与周期数未实测**（QEMU 不提供可信时序，`DWT_CYCCNT` 在 QEMU 返回 0）；ExecuTorch / TVM / IREE / ONNX Runtime / TFLite **未安装**，Ch 28 相关描述一律标「源码/文档依据」；GPU（Triton）路径未运行。
