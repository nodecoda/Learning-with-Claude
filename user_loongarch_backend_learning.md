---
name: loongarch-backend-course-scope
description: LLVM LoongArch backend and toolchain course (2026) — 31 chapters + 5 practices + PR-ready final, based on ct-instr-trunk and issue #239812, with a LoongArch+lld build.
metadata:
  type: project
---

`compilers/loongarch_backend/` 是一门以「LLVM 里的龙芯（LoongArch）全栈」为主线的教材：从 ISA/Feature/ABI，到 TableGen/MC、SelectionDAG、机器 pass、Clang driver/builtins、compiler-rt，最后到 LLD 重定位/relaxation 与上游 PR 流程。目标不是「会用 clang --target=loongarch64」，而是**能读懂任意一个 LoongArch pass，并能对一个 `[LoongArch]`/`[lld][LoongArch]` issue 做出可验证的修复**。

**Why:** 使用者明确说「我想学 LLVM 里面龙芯相关的代码，我想参与对龙芯代码的维护贡献，深入分析 `/home/ubuntu/llvm/trees/ct-instr-trunk`」。这棵树当前分支是 `fix/issue-239812-relax-marker-order`，只含一条 LLD LoongArch TLS LE relaxation 修复，正好可以作为「issue → 复现 → 根因 → 修复 → lit → PR」的完整案例。课程必须建立在真实树与真实构建上，不写泛泛的 LoongArch 简介。

**How to apply:**

- 基线：`/home/ubuntu/llvm/trees/ct-instr-trunk`，LLVM `24.0.0git`，HEAD `01df7c365d7c`（2026-11-07），分支 `fix/issue-239812-relax-marker-order`，相对 `upstream/main` 1 ahead / 19 behind。
- 构建：`builds/ct-instr-trunk` 原为 `X86;AArch64` + `clang`（没有 LoongArch、没有 lld，无法跑本分支测试）。本课用
  `LLVM_TARGETS_TO_BUILD='X86;AArch64;LoongArch' LLVM_ENABLE_PROJECTS='clang;lld' ./toolset/configure.sh ct-instr-trunk`
  重配，并补建 `llc llvm-mc llvm-objdump lld llvm-readobj yaml2obj FileCheck count not split-file llvm-as llvm-dis opt llvm-mca llvm-config clang`。
- 文件结构：`README.md` + Ch 00–30（31 章）+ 练习 A–E + Final，共 38 篇。Part 0（00–03）树审计/全景/ISA/流水线；Part 1（04–08 + A）TableGen/MC；Part 2（09–12 + B）ABI/栈帧/TLS/链接；Part 3（13–17 + C）SelectionDAG/向量；Part 4（18–23 + D）机器 pass；Part 5（24–26 + E）Clang/运行时；Part 6（27–30 + Final）测试/调试/贡献/维护。
- 当前进度（2026-11-08）：**全课完成**，38 篇 10,093 行。Ch 00–30 正文 + 练习 A–E + Final 全部带实测；无残留骨架。
- 新增实测（2026-11-08，可重跑）：
  - driver：`-###` 显示 LA64 默认 `+lsx +relax +64bit +f +d +ual` / `lp64d`；`-march=loongarch64` 不带 `+lsx`；`-march=generic-la64` 被前端拒绝（`valid target CPU values: loongarch32, loongarch64, la464, la664`），但 `llc -mcpu=generic-la64` 可用；`-march=native` 在 x86 主机解析成 `znver2`。
  - LA32 默认 `ilp32d` 但 feature 无 `+d` → 后端回退 feature-implied ABI，`__addsf3` 软浮点 + 两条 warning。
  - `-mabi=lp64f`：`-fsyntax-only` 0 条、`-S` 2 条、`-c` 3 条 warning（`LoongArchBaseInfo.cpp:43/100`）。
  - builtins：三个 `.def` 58/959/1007 行；`__builtin_lsx_vadd_w` → `llvm.loongarch.lsx.vadd.w`，`v4i32` 在 ABI 上是 `i128`；`-mno-lsx` → `needs target feature lsx`；`dbar(32768)`/`csrrd_d(16384)` 立即数范围错误。
  - compiler-rt：本构建 `lib/clang/24` 只有 `include`，无 `libclang_rt`；`loongarch_SOURCES = loongarch/fp_mode.c + GENERIC_SOURCES + GENERIC_TF_SOURCES`（26 个 tf 文件）；`fp_mode.c` 实测反汇编 `movfcsr2gr`/`bstrpick.d`/`ldx.bu`。
  - lit 基线：CodeGen LoongArch 978、MC 335、lld loongarch 48、clang CodeGen/LoongArch 48、clang Driver loongarch 28。
  - 端到端：`clang --sysroot=/opt/loongarch64-cross/target -B<gcc dir> -L<gcc dir> -fuse-ld=lld` → qemu `exit=0`；TLS LE `.o` 3 对 `R_LARCH_RELAX`，最终 ELF 收缩为 `addi.d $a0,$tp,0; ret`；LSX `-march=la464` 向量输出 `11 22 33 44`。
  - MIR：`-run-pass=loongarch-late-branch-opt` 折 `BEQ $r4,$r4`→`PseudoBR`，不折 `BEQ $r4,$r0`；`dead-defs` 只改写 load/store（`dead $r0 = LD_D`），跳过 `ADD_W`；`merge-base-offset` 在 `-relocation-model=static` + `dso_local` 下把 `ADDI_D` 折进 `LD_D`。
- 关键实测（可重跑）：
  - `llc --version` 有 `loongarch32/loongarch64`；`llvm-config --targets-built` = `X86 AArch64 LoongArch`。
  - `llvm-lit -s -j 24 lld/test/ELF --filter loongarch` → 48 passed。
  - 新测试 `lld/test/ELF/loongarch-tls-le-relax-order.s` + 相关 TLS 测试 → 3 passed。
  - cross `as` 造 canonical/hint-first 对象 + 本树 `ld.lld` + `qemu-loongarch64` → 两者都生成 `addi.d $a0,$tp,0`，exit 0。
  - `llc` 生成 `localexec` TLS → `R_LARCH_TLS_LE_HI20_R/ADD_R/LO12_R` 各跟一条 `R_LARCH_RELAX`（canonical 顺序）。
  - `llc -mattr=+lasx` 的 `v8i32` load → `xvld`；去掉 `+lasx` → 两条 `vld`。
- 教学纪律：每章「目标 → 源码落点 → 实测命令/输出 → 反直觉点 → 证据表 → 练习 → 下一步」；区分 `实测` / `源码依据` / `未实测`；不伪造构建日志；不读其它 `trees/<task>` 或 `builds/<task>`（遵守 `/home/ubuntu/admin/AGENTS.md` §1.1）。
- 刻意不涵盖：LoongArch 内核/固件、GCC 后端、私有加速器；GlobalISel 只作为「为什么还没迁」的对照，不写完整迁移方案。
- 课程定位：面向「想参与上游维护」的工程师，因此 Part 6 的贡献流程和维护者视角与 Part 1–5 同等重要；Final 要求 PR-ready patch（代码 + 测试 + release note 如适用 + reviewer 可接受的说明）。
