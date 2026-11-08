# LLVM MLIR 上游维护与贡献：从 trunk 源码到可合并 PR

> 基线：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`，从 `upstream/main` 的 `e44fff5decc7` 开出（2026-11-08），LLVM/MLIR `24.0.0git`。所有“实测”都来自这棵树 + `builds/ct-instr-trunk`（`X86;AArch64;RISCV`，`clang;lld;mlir`，assertions ON，shared libs）。

这门课不是再写一遍 MLIR API 导览。它要回答维护者每天真正要回答的四个问题：

1. **这段代码改了什么、会影响谁？** 从一个 ODS 定义，追到生成的 `Op` 类、parser/printer、verifier、canonicalization、conversion legality 和下游 dialect。
2. **怎么证明它坏了、又怎么证明你修好了？** 用 `mlir-opt`、`mlir-translate`、`mlir-runner`、`mlir-reduce` 和 lit 产出可复现证据，而不是“看代码觉得对”。
3. **reviewer 会问什么？** 以真实 `[mlir]` 提交为样本，拆出“核心不变量 + 小补丁 + 覆盖边界条件的测试”的补丁形状。
4. **怎么把补丁送进上游？** issue 复现 → 根因 → 最小修复 → lit 回归 → PR 描述 → review 往返 → squash merge。

学完你应该站在这个位置：**能独立读懂 MLIR 的任意一层，能对一个 `[mlir]` issue 做出可验证的修复，并知道 `mlir/Maintainers.md` 和 `.github/CODEOWNERS` 会把 review 分给谁。**

---

## 为什么是 MLIR（维护视角）

- **规模大，而且仍在快速演化。** 本树 `mlir/` 有 8,038 个 tracked 文件；`mlir/lib` + `mlir/include` 约 92 万行 C++/TableGen；`mlir/test` 有 4,129 个测试文件、3,299 个 `.mlir`。过去 12 个月 4,060 条提交改到 `mlir/`，最近 6 个月仍有 1,973 条。
- **热点集中在“接缝”而不是核心 IR。** 一年 churn 前几名是 `OpenMPToLLVMIRTranslation.cpp`（161 次）、`NVVMOps.td`（105）、`NVVMDialect.cpp`（90）、`OpenMPDialect.cpp`（78）、`VectorOps.cpp`（66）。这些正是维护者最常被叫去 review 的地方。
- **核心不变量少，但破一次就是崩溃或 silent miscompile。** IR 修改的 use-list/erase 顺序、conversion 的 legality 与 type materialization、ODS 生成物的 ABI，是三类高频根因。
- **测试就是契约。** MLIR 的 lit 测试同时覆盖 parse/print 往返、verifier 负控、pass 输出、bytecode 兼容和端到端执行；不会写测试，补丁基本进不去。
- **所有权公开。** `mlir/Maintainers.md` 按 Core / Egress / Tensor Compiler 分区，`.github/CODEOWNERS` 对 ODS、TilingInterface、DataFlow 等再细分；找对 reviewer 比写得快更重要。

---

## 这门课与已有课程的关系

| 已有课程 | 本课怎么用它 | 本课新增什么 |
|---|---|---|
| [`compilers/mlir_core`](../mlir_core/README.md) | 复用 IR 对象模型、ODS、Pass、Conversion、Linalg 的概念划分 | **trunk 24.0.0git**、真实 MLIR 构建、维护/测试/review 视角、真实 `[mlir]` 提交案例 |
| [`compilers/tablegen_internals`](../tablegen_internals/README.md) | 复用 Record/Init、`-gen-*` 生成物知识 | 在 `OpDefinitionsGen` / `mlir-tblgen` 上做真实改动与 diff |
| [`compilers/compiler_backend`](../compiler_backend/README.md) 等 LLVM 课 | 复用 LLVM IR、后端、链接知识 | MLIR → LLVM dialect → LLVM IR 的接缝与 ABI |
| [`compilers/riscv_maintenance`](../riscv_maintenance/README.md) | 复用“树审计 → 贡献地图 → PR”的组织方式 | MLIR 特有的 ODS、interfaces、conversion、bufferization |

先修要求：

- C++17：能读模板、继承、CRTP、`switch` 分派；不要求写过 MLIR pass。
- LLVM IR：知道 module/function/basic block、SSA、`llc`。
- MLIR 基础：读过 `mlir_core` 的 Part 0–2，或等价地用 `mlir-opt` 跑过 pass。
- 命令行：能跑 `ninja`、`llvm-lit`、`git log`/`git diff`。

---

## 课程地图：7 个 Part，32 章 + 5 个练习 + 1 个 Final

### Part 0 — 这棵树与这张地图（Ch 00–03）

- [Ch 00 树审计与分析分支](./00-tree-audit-mlir-branch.md)：worktree、MLIR 构建、lit 基线、本课证据约定
- [Ch 01 MLIR 在 LLVM 中的位置](./01-mlir-in-llvm-map.md)：目录分层、库边界、工具链、与 LLVM IR 的分工
- [Ch 02 构建系统与测试矩阵](./02-build-and-test-matrix.md)：CMake 选项、`check-mlir`、lit 后缀、断言/昂贵检查
- [Ch 03 维护者、CODEOWNERS 与 churn](./03-maintainers-and-churn.md)：所有权、review 路由、issue 标签、贡献地图

### Part 1 — 核心 IR 不变量（Ch 04–08 + 练习 A）

- [Ch 04 Operation/Block/Region/Value](./04-ir-object-model.md)：对象模型、生命周期、SSA 作用域、内存所有权
- [Ch 05 Type、Attribute 与 storage](./05-types-attributes-storage.md)：uniquing、参数化、`StorageUserBase`、销毁顺序
- [Ch 06 Properties、属性与 bytecode](./06-properties-and-bytecode.md)：inherent/discardable、properties、bytecode 兼容性
- [Ch 07 Verifier 与 diagnostics](./07-verifier-and-diagnostics.md)：verifier 顺序、负控测试、错误恢复、`emitError`
- [Ch 08 安全地修改 IR](./08-modifying-ir-safely.md)：walk、listener、rewriter、use-list、erase、线程边界
- [练习 A：写一个 pass 与 pattern](./practice-a-ir-pass-pattern.md)：从 `mlir-opt` 跑通到 lit 回归

### Part 2 — Pass、Pattern、Analysis（Ch 09–13 + 练习 B）

- [Ch 09 PassManager 与 analysis](./09-pass-manager-and-analysis.md)：pass 类型、pipeline 文本、analysis 失效与保留
- [Ch 10 RewritePattern 与 canonicalize](./10-rewrite-patterns-and-canonicalize.md)：greedy driver、fold、收敛、`PatternRewriter`
- [Ch 11 DataFlow 与分析框架](./11-dataflow-and-analyses.md)：`DataFlowSolver`、dominance、alias、value bounds
- [Ch 12 Dialect interfaces 与 external models](./12-dialect-interfaces.md)：op/type/attr interface、模型注册、接口演化
- [Ch 13 调试、复现与最小化](./13-debugging-and-reduction.md)：`-mlir-print-ir-*`、`mlir-reduce`、崩溃复现器
- [练习 B：analysis + interface](./practice-b-analysis-interface.md)：写一个 analysis 与 interface 实现

### Part 3 — ODS、TableGen 与方言维护（Ch 14–18 + 练习 C）

- [Ch 14 ODS 与生成物解剖](./14-ods-and-generated-code.md)：`OpDefinitionsGen`、`getODSOperands`、builders、生成物 diff
- [Ch 15 assemblyFormat、parser 与 printer](./15-assembly-format-parser-printer.md)：声明式格式、custom parser/printer、往返测试
- [Ch 16 Traits、interfaces 与 verifier](./16-traits-and-verifiers.md)：trait 语义、接口默认实现、ODS 约束
- [Ch 17 自定义 Type 与 Attribute](./17-custom-types-attributes.md)：storage、参数、parse/print、兼容性
- [Ch 18 方言注册与版本演化](./18-dialect-registration-and-versioning.md)：dependent dialects、extensions、bytecode versioning
- [练习 C：改一个 ODS 方言并保持兼容](./practice-c-ods-dialect-evolution.md)：加 op、改格式、补负控

### Part 4 — Conversion 与 Bufferization（Ch 19–23 + 练习 D）

- [Ch 19 ConversionTarget 与 legality](./19-conversion-target-legality.md)：full/partial、recursive legality、动态 legality
- [Ch 20 TypeConverter 与 materialization](./20-type-converter.md)：1:1、1:N、source/target materialization
- [Ch 21 `unrealized_conversion_cast` 与调试](./21-dynamic-legality-and-casts.md)：类型不匹配、legalization 顺序、常见死循环
- [Ch 22 Bufferization](./22-bufferization.md)：tensor→memref、one-shot、ownership、deallocation
- [Ch 23 降到 LLVM dialect 及更远](./23-lowering-to-llvm-and-beyond.md)：LLVM/SPIR-V/GPU 接缝、ABI、数据布局
- [练习 D：conversion + bufferization](./practice-d-conversion-bufferization.md)：把自定义方言降到 LLVM

### Part 5 — 真实子系统维护（Ch 24–28 + 练习 E）

- [Ch 24 Linalg、Tensor、Vector](./24-linalg-tensor-vector.md)：structured op、iterator、tiling、vector 化边界
- [Ch 25 Affine、SCF、Arith、Math](./25-affine-scf-arith.md)：循环 IR、依赖分析、fold 与快速数学
- [Ch 26 LLVMIR/OpenMP/OpenACC 翻译层](./26-openmp-openacc-llvmir.md)：churn 热点、metadata、ABI、端到端
- [Ch 27 GPU 方言](./27-gpu-dialects.md)：GPU/NVVM/ROCDL/AMDGPU/XeGPU 的分层与测试
- [Ch 28 Transform dialect、PDLL 与测试方言](./28-transform-dialect-pdll.md)：用 IR 写变换、PDLL、`test` dialect
- [练习 E：端到端与 bug hunt](./practice-e-end-to-end-and-bug-hunt.md)：从 IR 到运行，再定位一个真实缺陷

### Part 6 — 测试、贡献与首个补丁（Ch 29–31 + Final）

- [Ch 29 测试写法与 update 脚本](./29-testing-and-update-scripts.md)：`split-input-file`、CHECK 生成、负控、bytecode 测试
- [Ch 30 真实 `[mlir]` 提交解剖与 PR 流程](./30-upstream-pr-review.md)：commit 形状、review 关注点、release note
- [Ch 31 高价值贡献地图](./31-contribution-map.md)：churn 热点、TODO/FIXME、unmaintained dialect、测试缺口
- [Final Project](./final-project-mlir-fix.md)：独立完成一个可提交上游的 MLIR 修复或小特性

---

## 实验环境

```bash
source /home/ubuntu/llvm/toolset/env.sh
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git status -sb                    # analysis/mlir-maintenance
T="$LLVM_HOME/builds/ct-instr-trunk"
$T/bin/mlir-opt --version         # LLVM 24.0.0git, assertions ON
$T/bin/mlir-opt --show-dialects | tr ',' '\n' | wc -l   # 50
$T/bin/mlir-opt --convert-to-llvm /tmp/smoke.mlir
```

关键事实（2026-11-08 实测）：

- 分支 `analysis/mlir-maintenance` 从 `upstream/main`（`e44fff5decc7`）开出；原 `analysis/riscv-maintenance` 分支仍在（`0fc5afa04c23`），其基线不受本课影响。
- `builds/ct-instr-trunk` 原为 `clang;lld`，本课用 `LLVM_ENABLE_PROJECTS="clang;lld;mlir" toolset/configure.sh ct-instr-trunk` 重配，`ninja mlir-opt mlir-translate mlir-runner mlir-tblgen` 实测 **26 分 20 秒 / 5,384 条边**；补建测试依赖 `check-mlir-build-only` 增量 **1 分 39 秒 / 404 条边**。
- 测试命令：

```bash
$T/bin/llvm-lit -sv -j16 mlir/test
$T/bin/llvm-lit -sv mlir/test/Dialect/Arith/
$T/bin/llvm-lit -sv mlir/test/Conversion/ArithToLLVM/
```

### 证据约定

- **实测**：在本机构建上真实跑出的输出；命令与输出一起给。
- **源码依据**：对照本树的具体文件、函数或行号。
- **未实测**：需要重编、需要 GPU/CUDA、需要真机或特定上游 commit 才能验证的内容；明确标注，不伪造日志。

> 本课只读取本任务自己的 worktree `trees/ct-instr-trunk` 与 `builds/ct-instr-trunk`，不引用其它 `trees/<task>`。

---

## 建议的读法

- **只想能修 bug**：Part 0 → Ch 13 → Ch 29–31 → 从 Ch 31 挑一个热点，回读对应 Part。
- **想改 ODS/方言**：Part 0 → Part 1 → Part 3 → 练习 C。
- **想改 lowering**：Part 0 → Part 2 → Part 4 → 练习 D。
- **想做完整 feature**：全课 + Final；Part 5 至少读 Ch 24–26。

每章固定结构：`为什么` → `主线` → `源码落点` → `可跑实验` → `踩雷集锦` → `重点整理` → `自我检核` → `延伸阅读`。练习带可折叠参考解答；Final 要求交出一个 PR-ready 的 patch。

---

## 当前进度

**状态：骨架已建立，Ch 00–31 + 练习 A–E + Final 的目录已定；正文待填。**

| 阶段 | 内容 | 状态 |
|---|---|---|
| 环境 | 分支、MLIR 构建、冒烟 | ✅ 完成 |
| 基线 | 全量 `mlir/test` lit | ✅ 4,947 发现 / 4,303 通过 / 643 unsupported / 1 XFAIL / 0 failed（43.16s） |
| 骨架 | README + 38 个章节文件 | ✅ 完成 |
| 正文 | 32 章 + 5 练习 + Final | ⏳ 待写 |

### 环境与基线（2026-11-08）

- worktree：`/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/mlir-maintenance`，HEAD `e44fff5decc7`。
- 构建：`builds/ct-instr-trunk`，`X86;AArch64;RISCV`，`clang;lld;mlir`，assertions ON，shared libs。
- 方言：`mlir-opt --show-dialects` 实测 **50** 个。
- lit：`mlir/test` 发现 **4,947** 个测试，`4,303` 通过、`643` unsupported、`1` expectedly failed、**0 failed**（43.16s，`-j16`）。首轮 105 个失败均为缺附属工具（exit 127）；补建 `check-mlir-build-only` 后剩 9 个，全部由 `MLIR_ENABLE_PDL_IN_PATTERNMATCH=0` 缓存导致，打开后归零。
- 额外缓存修正：`cmake -DMLIR_ENABLE_PDL_IN_PATTERNMATCH=ON`（只改这一项，其余仍由 `toolset/configure.sh` 生成），再 `ninja mlir-opt` 实测增量 **14 分 48 秒 / 5,188 条边**。

### 质量约定

- 每章至少 5 处真实实验（文中标「实测」或「真实输出」）：可复制命令 + 真实输出；跑不了的标「未实测」+ 原因。
- 所有源码行号用 `grep -n` 对本树核对；所有相对链接必须存在。
- 不把 `mlir_core` 已讲过的 API 再抄一遍；每章至少回答一个“维护者为什么会改到这里”。
