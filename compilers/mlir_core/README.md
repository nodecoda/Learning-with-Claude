# MLIR 核心与降级：从 IR 对象模型到 LLVM

> 给已经熟悉 LLVM IR 与 backend、想把 MLIR 真正用起来的工程师。

这门课以 **MLIR 23.1.4** 为准，沿着一条完整链路走：IR 对象模型 → 方言生态 → Pass 与 Pattern → ODS/TableGen 自定义方言 → Dialect Conversion → LLVM dialect → `llc` 后端。目标不是背 API，而是能回答三个问题：这段 IR 为什么长这样、这个 pass 改了什么、这条 lowering 路径为什么这么设计。

> 证据约定：`实测` = 本机 `mlir-opt` / `mlir-translate` / `mlir-tblgen` 的真实输出；`源码依据` = 对照 llvmorg-23.1.4 源码；`guided` = 需要 assertions 构建或额外实验设置、本课不伪造输出。

## 为什么学这个？

- **多级 IR 是现实需求**：从 TensorFlow/XLA 到 IREE、Torch-MLIR、CIRCT，真正难的不是“再写一个 IR”，而是让不同抽象层共存、可组合、可逐步降级。MLIR 就是这套基础设施。
- **方言不是语法糖**：`arith`、`scf`、`memref`、`linalg` 各自承担不同抽象层次；理解边界，才能判断一条 lowering 该放在哪。
- **ODS 改变了编译器工程**：op 的定义、验证、parser/printer、builder 都由 TableGen 生成；读懂生成物，才能定位“为什么我的 op 不能被 parse”。
- **Conversion 框架是主战场**：真实 bug 多集中在 legality、type conversion 和 `unrealized_conversion_cast`，而不是 pattern 本身。
- **与已有课程互补**：[`globalisel_regbankselect`](../globalisel_regbankselect/README.md) 讲 LLVM 机器层；本课停在 LLVM dialect / LLVM IR，把 MLIR 到后端的接缝讲清楚。

## 先修知识

- **C++17**（程度：能读模板与虚函数；能改中小型 C++ 项目）
- **LLVM IR 基础**（程度：知道 `llc`、模块/函数/基本块、SSA、`llvm.dialect` 的落点）
- **编译器前端概念**（程度：知道 AST、类型检查、IR 生成的大致流程）
- **命令行与 PowerShell**（程度：能设 PATH、跑管道、读退出码）
- 不要求：写过 MLIR dialect、懂 Linalg、懂 bufferization

## 课程地图

### Part 0 — 心智模型与 IR（Ch 0–4）

- [Ch 0 环境与最小闭环](./00-environment-setup.md)：装好 `mlir-opt`，跑通第一段 MLIR 并降级到 LLVM IR
- [Ch 1 MLIR 是什么](./01-what-is-mlir.md)：多级 IR、dialect 组合，与 LLVM IR / AST 的分工
- [Ch 2 IR 对象模型](./02-ir-object-model.md)：Operation / Block / Region / Value，SSA 与作用域
- [Ch 3 类型与属性](./03-types-and-attributes.md)：builtin 类型、memref/tensor/vector、affine map
- [Ch 4 方言地图](./04-dialect-map.md)：arith/func/cf/scf/affine/memref/tensor/linalg/vector/gpu/llvm 的边界
- [练习 A：读写 MLIR 与观察 pass](./practice-a-ir-and-passes.md)

### Part 1 — Pass 与变换（Ch 5–10）

- [Ch 5 PassManager](./05-pass-manager.md)：pass 类型、嵌套、pipeline 语法、analysis 失效
- [Ch 6 分析框架](./06-analysis-framework.md)：DominanceInfo、DataFlowSolver、AliasAnalysis
- [Ch 7 RewritePattern 与 canonicalize](./07-patterns-and-canonicalize.md)：匹配、greedy driver、folding、收敛
- [Ch 8 Dialect Interface](./08-dialect-interfaces.md)：op/type/attr interface 与 external model
- [Ch 9 安全地修改 IR](./09-modifying-ir-safely.md)：walk、listener、use-list、erase、事务式 rewriter
- [Ch 10 调试与测试](./10-debugging-and-testing.md)：diagnostics、`-mlir-print-ir-after-all`、split-input-file、lit
- [练习 B：写一个 pass 与 pattern](./practice-b-pass-and-pattern.md)

### Part 2 — 定义自己的方言（Ch 11–16）

- [Ch 11 ODS 入门](./11-ods-basics.md)：Op 定义、operands/results/attrs、assemblyFormat
- [Ch 12 约束与验证](./12-constraints-and-verifiers.md)：TypeConstraint、verifier、custom parser/printer
- [Ch 13 解剖 TableGen 生成物](./13-tablegen-generated-code.md)：`.h.inc` / `.cpp.inc`、getODSOperands
- [Ch 14 自定义 Type 与 Attribute](./14-custom-types-attributes.md)：storage uniquing、参数、解析/打印
- [Ch 15 Trait 与 Interface 实战](./15-traits-and-interfaces.md)：Canonicalizer、fold、memory effects
- [Ch 16 Dialect 注册与分发](./16-dialect-registration.md)：dependent dialects、registry、bytecode
- [练习 C：实现一个 standalone dialect](./practice-c-standalone-dialect.md)

### Part 3 — 转换与降级（Ch 17–22）

- [Ch 17 Dialect Conversion](./17-dialect-conversion.md)：ConversionTarget、legality、partial/full conversion
- [Ch 18 TypeConverter](./18-type-converter.md)：1:1、1:N、materialization
- [Ch 19 动态 legality](./19-dynamic-legality.md)：`unrealized_conversion_cast` 的角色
- [Ch 20 Bufferization](./20-bufferization.md)：tensor→memref、one-shot bufferize
- [Ch 21 降到 LLVM dialect](./21-lowering-to-llvm.md)：arith/scf/memref 的对应、ABI、数据布局
- [Ch 22 产出 LLVM IR](./22-llvm-ir-and-backend.md)：`mlir-translate`、`llc`、与 GlobalISel 管线的接缝
- [练习 D：把自定义方言降到 LLVM](./practice-d-lowering.md)

### Part 4 — 真实生态（Ch 23–27）

- [Ch 23 Linalg](./23-linalg.md)：structured op、iterator type、named vs generic
- [Ch 24 Affine](./24-affine.md)：`affine.for/if`、依赖分析与循环变换
- [Ch 25 Tensor、Vector 与 GPU](./25-tensor-vector-gpu.md)：抽象层次与 lowering 路线
- [Ch 26 Transform dialect](./26-transform-dialect.md)：用 IR 编写 pass pipeline
- [Ch 27 工程化](./27-engineering.md)：out-of-tree、CMake、测试策略、compile-time

### Part 5 — 收束（Ch 28–31）

- [Ch 28 读 MLIR 源码](./28-reading-source.md)：目录结构、关键文件、追一条 lowering 路径
- [Ch 29 设计取舍](./29-design-tradeoffs.md)：MLIR 与 LLVM IR / 其它 IR 的对比
- [Ch 30 性能与规模](./30-performance-and-scale.md)：MLIRContext、storage uniquing、pass 复杂度
- [Ch 31 端到端复盘](./31-end-to-end.md)：从自定义方言到 LLVM，逐段解释 IR 变形
- [Final Project：从 mini frontend 到 LLVM](./final-project-mini-frontend.md)

## 学习方式建议

1. **每章先跑再读**：把章节里的 `.mlir` 存盘，用 `mlir-opt` 跑一遍，再读解释。
2. **用 `-mlir-print-ir-after-all` 看变化**：不要只看最终输出，IR 的中间形态才是教学内容。
3. **每章至少改一处**：改类型、改 pass 顺序、改 legality，观察报错信息。
4. **Part 2 开始动手写代码**：ODS 与 pass 必须自己写一遍，读生成物才算学会。
5. **Final 不跳过**：把 mini frontend、方言、优化、降级串起来，才算走完闭环。

## 环境与版本

> **环境**：MLIR 23.1.4（conda-forge win-64），Windows 10/11 x86_64。所有 IR 级实验均在本机实测。

```powershell
# 本仓库已装好的环境
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" --version
```

实测输出：

```text
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
```

可用的工具：`mlir-opt`、`mlir-translate`、`mlir-tblgen`、`mlir-query`、`mlir-reduce`、`mlir-lsp-server`、`mlir-pdll`、`mlir-rewrite`、`mlir-runner`。

**C++ 工具链（已实测）**：本机 VS2022 Enterprise 在 `D:\VStudio\2022\Enterprise`（MSVC 14.43），可编译、链接并运行 MLIR 程序；VS2017（14.16）会被 LLVM 23 直接拒绝。构建时需额外注意两点：Windows SDK 10.0.26100 缺 `shell32.lib`，要补 10.0.17763 的 lib 路径；`LLVMSupport.lib` 还需要 `ntdll.lib`。完整命令见 Ch 0。

- **IR 级实验（Ch 0–10、17–31）**：`mlir-opt` / `mlir-translate` 全部可跑。
- **`.td` → `.inc`**：`mlir-tblgen` 可跑，生成物可逐行读（Ch 13）。
- **out-of-tree dialect/pass（Ch 11–19、练习 B/C/D）**：VS2022 + CMake 已实测跑通（`MLIR_DIR=.scratch\mlir-env\Library\lib\cmake\mlir`、`CMAKE_PREFIX_PATH=.scratch\mlir-env\Library`，并补 `zlib`、`ntdll.lib` 与 SDK 10.0.17763 的 lib 路径）。
- **`-debug-only` / assertions**：conda 包是 Optimized build，标 `guided`。

## 本课不涵盖什么

- **从零实现一个完整编译器**：Final 是 mini frontend，不是生产级前端。
- **Linalg 的完整变换集合与 scheduling**：只讲模型、边界和典型 lowering。
- **GPU/SPIR-V 后端细节**：只讲方言分层与落点。
- **MLIR Python bindings 深入**：本机 conda 包不含 Python 绑定。
- **LLVM backend 内部**：那是 `globalisel_*` 课程的内容，本课停在 LLVM IR / `llc`。

## 精选资料库

### 官方文档

- **[MLIR 文档首页](https://mlir.llvm.org/docs/)**：整个文档集的入口。先读 “MLIR: A New Approach to IR”，再按本课章节进入具体页面。
- **[Toy Tutorial](https://mlir.llvm.org/docs/Tutorials/Toy/)**：官方端到端教程（Ch 1–7）。本课不重复它，但 Ch 11–21 会引用其设计；建议在 Part 2 前通读一遍。
- **[Defining Dialects](https://mlir.llvm.org/docs/DefiningDialects/)**：ODS、类型、属性、接口的官方定义。Ch 11–16 的主要依据。
- **[Dialect Conversion](https://mlir.llvm.org/docs/DialectConversion/)**：legality、TypeConverter、conversion pattern。Ch 17–19 的核心。
- **[Bufferization](https://mlir.llvm.org/docs/Bufferization/)**：one-shot bufferize 的模型与接口。Ch 20 的依据。
- **[Linalg Dialect](https://mlir.llvm.org/docs/Dialects/Linalg/)**：structured op、iterator type、named/generic。Ch 23 的依据。
- **[Transform Dialect](https://mlir.llvm.org/docs/Dialects/Transform/)**：用 IR 描述变换。Ch 26 的依据。

### 源码

- **[mlir/ 目录（llvmorg-23.1.4）](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/mlir)**：本课所有“源码依据”的锚点。
- **[mlir/include/mlir/IR](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/mlir/include/mlir/IR)**：`Operation.h`、`OpDefinition.h`、`BuiltinTypes.h`。Ch 2–3 的核心。
- **[mlir/lib/Transforms](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/mlir/lib/Transforms)**：canonicalizer、CSE、inline 等真实 pass。Ch 7 的范例。
- **[mlir/lib/Conversion](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/mlir/lib/Conversion)**：`ArithToLLVM`、`FuncToLLVM`、`SCFToControlFlow`。Ch 17–21 的范例。
- **[mlir/lib/Dialect/Linalg](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/mlir/lib/Dialect/Linalg)**：Linalg 的实现与 lowering。Ch 23 的核心。

### 论文与演讲

- **[MLIR: A Compiler Infrastructure for the End of Moore's Law (arXiv:2002.11054)](https://arxiv.org/abs/2002.11054)**：MLIR 的设计动机与多级 IR 论证。Ch 1、29 的理论背景。
- **[MLIR: Scaling Compiler Infrastructure for Domain Specific Computation (CGO 2021)](https://doi.org/10.1109/CGO51591.2021.9370308)**：正式论文版，讲 dialect 组合与工程取舍。
- **[LLVM Developers' Meeting: MLIR 相关演讲](https://llvm.org/devmtg/)**：每年都有 MLIR 进展与实战；Ch 27–29 的补充材料。

### 博客 / 技术文章

- **[MLIR 官方博客](https://mlir.llvm.org/blog/)**：方言设计、bufferization、transform 等专题文章，作者多为上游开发者。
- **[IREE 项目](https://github.com/iree-org/iree)**：真实生产级 MLIR 使用者，适合看端到端 pipeline 的组织方式（Ch 27 延伸）。

## 课程状态

✅ **全部完成（2026-10-24）**：README + Part 0–5（Ch 0–31）+ 练习 A–D + Final Project，共 **38 个 Markdown 文件、约 19,100 行**。

- **实测覆盖**：`mlir-opt` / `mlir-translate` / `mlir-tblgen` / `llc` 的 IR、汇编与错误诊断均为本机真实输出；Ch 27 的 out-of-tree CMake 构建与 `lit` 套件、Final 的 mini frontend（C++ → MLIR → LLVM IR → `.obj` → 可执行文件）都已实际跑通。
- **源码依据**：正文里的 `文件:行号` 全部对照 `llvmorg-23.1.4` 快照逐条查过。
- **guided 边界**：`-debug-only` / assertions 专属行为、GPU 二进制生成（本机缺 CUDA/ROCm 工具链）、Linux/macOS ABI、CI 与上游 git 历史——相关章节均已就地标注，未伪造输出。
