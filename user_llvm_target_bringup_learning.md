---
name: llvm-target-bringup-course-scope
description: LLVM target bring-up course (2026) — 29 chapters + 4 practices + end-to-end final, using the real MiniRV target and RISC-V as the reference, LLVM 24.0.0git on /home/ubuntu/llvm.
metadata:
  type: project
---

`compilers/llvm_target_bringup/` 是一门以「从零把一个 target 接进 LLVM」为主线的教材：Triple/DataLayout → TargetRegistry/CMake → MC-only target → TableGen/编码/汇编/反汇编 → TargetMachine/Subtarget/Lowering → 寄存器/ABI/栈帧/合法化/选指 → 调用/参数/链接/异常/调试 → 测试/Clang/上游化。RISC-V 是参照实现，MiniRV 是真实实验对象。

**Why:** 使用者明确说「我想学 LLVM 里面把一个 target 接入，或者说是 bring-up，以 RISC-V 为例」。已有 `riscv_backend` 教「读完整 RISC-V 后端」、`loongarch_backend` 教「维护成熟后端」、`tablegen_internals` 教「TableGen 内部」，但缺少一门按**接入顺序与验收里程碑**组织的课。本机 `trees/dsl-tablegen` 里有一个真实的 MiniRV target：42 个 bring-up commit、53 个 lit 测试、17 个 qemu 端到端程序，正好可以作为「每层到底改哪些文件、怎么验收」的活样本。

**How to apply:**

- 文件结构：`README.md` + Ch 00–28（29 章）+ 练习 A–D + Final，共 34 篇。目标每章 350–550 行，练习 450–650 行，Final 600–800 行。
- Part 0（00–03）：课程地图/环境、target 七层边界、RISC-V 参照、MC-only 里程碑。
- Part 1（04–08 + A）：Triple/DataLayout、构建系统/TargetRegistry、最小 TableGen、MC 五件套、AsmParser/Printer/Encoder。
- Part 2（09–15 + B）：TargetMachine/Subtarget/Lowering、寄存器/调用约定、FrameLowering、Legalization、SelectionDAG、内存/全局地址、控制流/尾调用。
- Part 3（16–22 + C）：调用序列、参数/返回、栈帧进阶、常量/位运算、软浮点/libcall、异常/CFI/调试、对象文件/链接。
- Part 4（23–28 + D + Final）：诊断/拒绝、测试体系、端到端差分、Clang 前端、上游评审、42 commit 复盘。
- 基线：源码 `/home/ubuntu/llvm/trees/dsl-tablegen`，LLVM `24.0.0git`，commit `6e7e7abeb2b7`（2026-11-14），构建 `/home/ubuntu/llvm/builds/dsl-tablegen`（`X86;AArch64;RISCV;RVTrim;MiniRV` + clang + assertions）。`ld.lld` 用 `/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld`；`qemu-riscv64-static` 用 `/usr/bin/qemu-riscv64-static`。
- 关键实测基线：`llvm-lit llvm/test/CodeGen/MiniRV llvm/test/MC/MiniRV` = 53 passed；`dev/e2e/run.sh` = 17/17 exit code 与 riscv64 参照一致；`llc -mtriple=minirv64` 对 `add3` 生成 `add x3,x10,x11; mul x10,x3,x12; jalr x0,0(x1)`；`llvm-readobj` 显示 `Format: elf64-littleriscv`、`Machine: EM_RISCV`。
- 教学纪律：每章固定「目标 → 为什么 → 直觉 → 主线 → 源码落点 → 实测 → 踩雷 → 进进阶 → 自我检核 → 延伸阅读 → 下一章」；所有输出必须本机真跑，跑不了标 `guided`/`未实测`；优先写「文件 + 符号名」，行号只在 `grep -n` 现场核对后写。
- 刻意不涵盖：生产级完整后端、TableGen 内部实现、Clang 完整实现、GPU/加速器、JIT、完整 LLD/compiler-rt 源码。
- 与其他课的关系：`riscv_backend` 提供 RISC-V 细节；`loongarch_backend` 提供成熟后端组织与上游流程；`tablegen_internals` 提供生成物内部；`llvm_lit` 提供测试语法；`llvm_isel_sched_riscv` 提供选指/调度交互。
- 状态：已完成 (2026-11-16)。35 篇（README + 29 章 + 4 练习 + Final），约 19.4k 行。终检：MiniRV lit 53/53、`dev/e2e/run.sh` 17/17、练习 A/B 的 FENCE/EBREAK 实验逐项复现（含 `Cannot select` 失败对照），回滚后 `git status` 仅 `?? ana-docs/`。
