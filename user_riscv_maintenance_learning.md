---
name: riscv-maintenance-course-scope
description: LLVM RISC-V 上游维护与贡献课程（2026）— 32 章 + 5 练习 + PR-ready final，以 ct-instr-trunk 的 analysis/riscv-maintenance 分支为基线，实验在 X86;AArch64;RISCV 构建上真跑。
metadata:
  type: project
---

`compilers/riscv_maintenance/` 是一门以「维护与上游贡献」而不是「后端全景」组织主线的 LLVM RISC-V 教材。

**Why:** 使用者已经有一门 `compilers/riscv_backend`（LLVM 23.1.4、预编译工具、静态后端全景）。这次的目标是**参与 RISC-V 代码维护与贡献**，因此必须用当前 trunk、真实可运行的 RISCV 构建、真实 `[RISCV]` 提交与 review 流程来教学，而不是重复 API 讲解。

**How to apply:**

- 基线：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/riscv-maintenance`（从 `upstream/main` = `0fc5afa04c23` 开出，2026-11-08），LLVM `24.0.0git`。
- 构建：`builds/ct-instr-trunk` 已由 `toolset/configure.sh` 重配为 `X86;AArch64;RISCV`（原先为 `X86;AArch64;LoongArch`）。恢复 LoongArch 只需重跑 configure。
- 文件结构：`README.md` + Ch 00–31（32 章）+ 练习 A–E + Final，共 39 篇。
- Part 0（00–03）：树审计、后端全景、ISA/扩展/Subtarget、流水线与 MIR。
- Part 1（04–08 + 练习 A）：TableGen 指令建模、寄存器/CC、编码/MC、汇编器/反汇编器、常量物化。
- Part 2（09–12 + 练习 B）：CC lowering、栈帧、代码模型/TLS/GP relaxation、LLD 重定位与 relaxation。
- Part 3（13–17 + 练习 C）：合法化、SelectionDAG pattern、自定义 lowering/intrinsic、DAG combine、RVV。
- Part 4（18–23 + 练习 D）：伪指令展开、机器优化、原子/屏障、调度/mca、AsmPrinter/ELF flags、GlobalISel。
- Part 5（24–26 + 练习 E）：Clang driver、TargetInfo/builtins、compiler-rt/libc/qemu。
- Part 6（27–31 + Final）：lit/update 脚本、调试 CodeGen、贡献流程、维护者视角、贡献地图、PR-ready final。
- 每章固定：`为什么` / `主线` / `源码落点` / `可跑实验` / `常见坑` / `自我检查` / `延伸阅读`。
- 关键事实锚点：`llvm/lib/Target/RISCV` 210 文件 / 157,290 行；`CodeGen/RISCV` 2,742 测试、`MC/RISCV` 622、`clang/test/CodeGen/RISCV` 2,824；过去 12 个月 1,545 条提交、6 个月 820 条；热点 `RISCVISelLowering.cpp`（498 次）、`RISCVInstrInfoP.td`（184 次）。
- 流程事实：本快照 docs 为 `.md`；RISC-V 无 CODEOWNERS，reviewer 看 `llvm/Maintainers.md:357-366`；`[RISCV]` 是惯例不是强制政策（`DeveloperPolicy.md:345-349`）。
- 刻意不重写：`riscv_backend` 已覆盖的静态后端全景只做快速对照，重心放在“改得动、测得准、合得进”。
- 教学纪律：所有“实测”必须来自本机 `builds/ct-instr-trunk`；跑不了的标“未实测”；不复制其它 worktree 的结论。

状态：2026-11-08 全部完成。39 篇全部落盘（32 章 + 5 个练习 + Final + README），正文 21,284 行（不含 README）。Ch 00 的分支/构建基线、Ch 04/05/12/15 的实测夹具均已二次核对；收尾时 `upstream/main` 已到 `e44fff5decc7`（本分支落后 13 个提交），全课行号固定钉在 `0fc5afa04c23`，跟读时先不要 merge/rebase；12 章载荷实验独立重跑结果 11 MATCH / 0 MISMATCH / 1 已修复复现。实验环境新增 qemu-user-static 6.2（scalar 可端到端跑；RVV 1.0 因 qemu 仅支持 draft V 0.7 会 SIGILL(132)，所有向量运行期结论标「未实测」）。下一步可执行入口：README 的「建议的读法」+ Ch 31 的贡献地图。
