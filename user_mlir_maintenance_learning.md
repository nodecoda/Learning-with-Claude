---
name: mlir-maintenance-course-scope
description: LLVM MLIR 上游维护与贡献课程（2026）— 32 章 + 5 练习 + PR-ready final，以 ct-instr-trunk 的 analysis/mlir-maintenance 分支与 clang;lld;mlir 构建为基线，实验在 X86;AArch64;RISCV 构建上真跑。
metadata:
  type: project
---

`compilers/mlir_maintenance/` 是一门以「维护与上游贡献」而不是「API 导览」组织主线的 MLIR 教材。

**Why:** 使用者已经有一门 `compilers/mlir_core`（MLIR 23.1.4、31 章、预编译工具、概念/降级全景）。这次的目标是**参与 MLIR 代码维护与贡献**，因此必须用当前 trunk、真实可运行的 MLIR 构建、真实 `[mlir]` 提交与 review 流程来教学，而不是重复 API 讲解。

**How to apply:**

- 基线：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`（从 `upstream/main` = `e44fff5decc7` 开出，2026-11-08），LLVM/MLIR `24.0.0git`。
- 构建：`builds/ct-instr-trunk` 由 `LLVM_ENABLE_PROJECTS="clang;lld;mlir" toolset/configure.sh ct-instr-trunk` 重配；`RelWithDebInfo` + assertions ON + `BUILD_SHARED_LIBS=ON`；XRISCV/AArch64/X86 targets。
- 额外缓存修正：`MLIR_ENABLE_PDL_IN_PATTERNMATCH=ON`（否则 Transform/PDL 的测试 pass 不注册，lit 会出现 9 个假失败）。
- 文件结构：`README.md` + Ch 00–31（32 章）+ 练习 A–E + Final，共 39 篇。
- Part 0（00–03）：树审计/构建/lit 基线、MLIR 在 LLVM 中的位置、测试矩阵、维护者与 churn。
- Part 1（04–08 + 练习 A）：IR 对象模型、type/attr/storage、properties/bytecode、verifier、IR 修改安全。
- Part 2（09–13 + 练习 B）：PassManager/analysis、RewritePattern/canonicalize、DataFlow、interfaces、调试/minimize。
- Part 3（14–18 + 练习 C）：ODS/生成物、assemblyFormat、traits/verifier、自定义 type/attr、注册与 versioning。
- Part 4（19–23 + 练习 D）：ConversionTarget/legality、TypeConverter、unrealized cast、bufferization、LLVM/SPIR-V/GPU。
- Part 5（24–28 + 练习 E）：Linalg/Tensor/Vector、Affine/SCF/Arith、OpenMP/OpenACC/LLVMIR、GPU、Transform/PDLL。
- Part 6（29–31 + Final）：测试/update 脚本、真实 `[mlir]` 提交与 PR、贡献地图、PR-ready final。
- 每章固定：`为什么` / `主线` / `源码落点` / `可跑实验` / `踩雷集锦` / `重点整理` / `自我检核` / `延伸阅读`。
- 关键事实锚点：`mlir/` 8,038 tracked 文件；`mlir/lib`+`include` 约 92 万行；`mlir/test` 4,129 文件 / 3,299 个 `.mlir`；50 个已注册方言；102 个 `*Ops.td`；12 个月 4,060 条提交、6 个月 1,973 条；热点 `OpenMPToLLVMIRTranslation.cpp`（161）、`NVVMOps.td`（105）。
- 所有权：`mlir/Maintainers.md` 分 Core / Egress / Tensor Compiler；`.github/CODEOWNERS` 对 ODS、TilingInterface、DataFlow 等细分。
- 刻意不重写：`mlir_core` 已覆盖的 API 只做快速对照；重心放在“改得动、测得准、合得进”。
- 教学纪律：所有“实测”必须来自本机 `builds/ct-instr-trunk`；跑不了的标“未实测”；不复制其它 worktree 的结论。

状态：2026-11-08 全课完成。分支 `analysis/mlir-maintenance` + MLIR 构建就绪（5,384 边 / 26 分 20 秒）；39 篇全部成文（README + 32 章 + 5 练习 + Final），正文 **20,199 行**，每章 ≥5 处本机实测；全量 lit 基线 **4,947 发现 / 4,303 通过 / 643 unsupported / 1 XFAIL / 0 failed（43.4s）**；未实测项（后端二进制、PTX/HSACO、`mlir-reduce`）均在正文标注原因。
