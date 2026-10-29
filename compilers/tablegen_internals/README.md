# TableGen 源码深潜：从 `.td` 语言到能提交上游的 TableGen 改动

> 给已经会用 LLVM backend、能读 C++ 大型项目，但 TableGen 只停在「照着抄 pattern」的工程师。

这门课的主线不是「怎么写 `.td`」，而是**TableGen 这个语言和工具本身是怎么实现的**：lexer 怎么切 token、parser 怎么建 Record、`Init` 树怎么 fold、multiclass 怎么展开、backend 怎么遍历 RecordKeeper。目标是你学完能读通 `llvm/lib/TableGen/` 全部约 1.9 万行，并在 `llvm/utils/TableGen/` 里加一个能让上游接受的 backend 或语法扩展。

> **证据约定**：本课所有 `.td` 实验都在本机 LLVM `24.0.0git`（`-DLLVM_ENABLE_ASSERTIONS=ON` 的 Release 构建）上实跑，输出为真实回显。涉及改动 LLVM 源码重新构建的章节标为 **guided**，给出确切文件/行号与预期输出，但不伪造构建日志。

---

## 为什么学这个？

- **「会写 pattern」和「会改 TableGen」差一个数量级**：前者是 `multiclass` + `let` 的排列组合；后者要判断「这个需求该加语法、加 backend、还是改 `Record` 的解析时机」，还要承担 ABI/兼容性/compile-time 的后果。`llvm/lib/TableGen/` 只有 14 个 `.cpp`、约 1.1 万行，加上约 2500 行的 `Record.h`，是 LLVM 里少数**一个人能完整读完**的核心子系统。
- **它是一切 target 定义的地基**：AArch64 的 instruction 定义约 12847 行、格式类 13849 行、SVE 又 11725 行；这些 `.td` 的每一个语义都由 `lib/TableGen` + `utils/TableGen` 决定。读不懂实现，就只能靠试错。
- **上游贡献的真实入口**：TableGen 的 PR 门槛在 LLVM 里相对可控——改动局部、测试用 `llvm/test/TableGen/*.td` 的 `-print-records`/`--dump-json` 就能覆盖，不需要跑完整 codegen。reviewer 关心的是「语言语义是否一致」「错误信息是否可诊断」「生成代码是否稳定」。
- **诊断能力**：`Couldn't find class 'B'`、`def already exists`、`Expected comma before next argument` 这些错误来自 lexer/parser 的哪一行？能不能自己加一条更清楚的错误？这直接决定你 debug `.td` 的效率。
- **与现有课程互补**：[`globalisel_instruction_select`](../globalisel_instruction_select/README.md) 讲 matcher 怎么被消费，[`riscv_backend`](../riscv_backend/README.md) 讲一个后端怎么搭起来；本课反过来，讲**产出这些 `.inc` 的工具本身**。

## 先修知识

- **C++17**（程度：能读模板、`std::unique_ptr`、CRTP、`ArrayRef`/`StringRef`；能改一个几千行的 C++ 子项目）
- **LLVM 基本功**（程度：知道 `Record`/`def`/`class`/`multiclass` 怎么写，见过 `llvm-tblgen -print-records`；读过 [`compiler_backend`](../compiler_backend/README.md) 任意一部分）
- **构建 LLVM**（程度：能自己 configure + ninja 出一个 `llvm-tblgen`；本课全程只要这一个二进制）
- **会看 lit 测试**（程度：知道 `RUN:` / `CHECK:` / `FileCheck` 语义）
- 没有也没关系：`Record.h` 的 `Init` 类层次、multiclass 展开算法、backend 注册机制——这些正是本课内容

## 课程地图

### Part 0 — 地基：把工具链握在手里（Ch 0–3）

- [Ch 0 环境、版本与最小可复现实验](./00-environment-setup.md)
- [Ch 1 TableGen 全景：前端、RecordKeeper、backend 三段式](./01-tablegen-architecture.md)
- [Ch 2 词法层：TGLexer 如何切出 token](./02-lexer.md)
- [Ch 3 语法层：TGParser 的语句分派与表达式解析](./03-parser.md)
- [练习 A：给 TableGen 加一个诊断更好的错误](./practice-a-better-diagnostic.md)

### Part 1 — 数据模型：Record 与 Init（Ch 4–8）

- [Ch 4 Record、RecordVal、RecordKeeper：内存里的图](./04-record-model.md)
- [Ch 5 Init 类层次 I：位串、整数、字符串、列表](./05-init-hierarchy-i.md)
- [Ch 6 Init 类层次 II：DAG、DefInit、VarInit 与引用解析](./06-init-hierarchy-ii.md)
- [Ch 7 求值：`fold()`、`resolveReferences()` 与惰性求值时机](./07-folding-and-resolution.md)
- [Ch 8 multiclass 展开算法：模板参数替换与 `defm` 前缀](./08-multiclass-expansion.md)
- [练习 B：用 C++ 复刻 `-print-records` 的输出](./practice-b-reimplement-print-records.md)

### Part 2 — 语义检查：类型、断言与错误（Ch 9–12）

- [Ch 9 类型系统：`RecTy` 与类型推断、隐式转换](./09-type-system.md)
- [Ch 10 位域、`bits<>` 与字段偏移的语义](./10-bits-and-fields.md)
- [Ch 11 `assert`、`dump`、`!instances` 与运行期求值](./11-assert-dump-instances.md)
- [Ch 12 错误报告基础设施：`PrintError`、`SMLoc`、`SourceMgr`](./12-error-reporting.md)
- [练习 C：给一个语义错误加上精确的 source range](./practice-c-source-ranges.md)

### Part 3 — Backend 工程（Ch 13–17）

- [Ch 13 Backend 注册机制与 `TableGen::Emitter::Opt`](./13-backend-registration.md)
- [Ch 14 写一个生产级 backend：遍历、排序、稳定输出](./14-writing-a-backend.md)
- [Ch 15 多文件输出、`--write-if-changed` 与增量构建](./15-multifile-and-incremental.md)
- [Ch 16 生成代码的可读性：`IfDefEmitter`、`NamespaceEmitter`、缩进工具](./16-emitted-code-shape.md)
- [Ch 17 大型 backend 走读：`InstrInfoEmitter` 与它的 10 个 timer 阶段](./17-instrinfo-walkthrough.md)
- [练习 D：写一个 `gen-isa-stats` backend](./practice-d-isa-stats-backend.md)

### Part 4 — 真实上游改动（Ch 18–22）

- [Ch 18 加一条语言特性：从语法到 `Init` 的全链路](./18-adding-a-language-feature.md)
- [Ch 19 给 `Record` 加字段 / 改默认值：兼容性代价清单](./19-changing-record-semantics.md)
- [Ch 20 测试策略：`llvm/test/TableGen` 的 496 个测试怎么用](./20-testing-strategy.md)
- [Ch 21 性能：内存、`instregex` 重复匹配与变体爆炸](./21-performance.md)
- [Ch 22 上游流程：从一个可提交 PR 到 review](./22-upstream-process.md)
- [Final Project：给 `llvm-tblgen` 加一个可合并的 backend](./final-project-contributable-backend.md)

## 学习方式建议

1. **只读源码是不够的**：每章都有一两个「把它弄坏」的实验。改一行 `TGParser.cpp`、加一条 `PrintError`，重编 `llvm-tblgen`（增量构建通常 1–3 分钟），比读十遍有效。
2. **先看数据结构，再看算法**：`Record.h` 约 2500 行，但真正的类只有 20 来个。先建立 `Record` → `RecordVal` → `Init` 的图，再去看 `TGParser.cpp` 的 4800 行，否则会被 `ParseXxx` 淹没。
3. **每章跑一次 `-print-records` 与 `--dump-json`**：这两个 backend 是你观察内部状态的显微镜，也是上游测试最常用的断言手段。
4. **对每个语法结构问「它变成哪几个 `Init` 节点」**：这是本课的核心思维训练。
5. **Final 不要跳过**：写一个真能被 review 的 backend，比读完全部源码更能证明你会了。

## 环境与版本

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`），Ubuntu x86_64，Release + assertions。只构建 `llvm-tblgen` 与 `llvm-lit` 相关工具。

```bash
cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AArch64;X86" \
  -DLLVM_ENABLE_PROJECTS="" \
  -DLLVM_INCLUDE_TESTS=ON
ninja -C build llvm-tblgen FileCheck count not split-file
```

版本差异会逐条标注。TableGen 的语言和实现都在持续演进（例如 `!repr` 这类较新的 bang operator），旧版 LLVM 上某些章节的语法会直接报错——Ch 0 有「怎么确认你的版本支持什么」的做法。

## 本课不涵盖什么

- **LLVM backend 的完整教学**：不教你写 `TargetMachine`、寄存器分配、调度；这些看 [`compiler_backend`](../compiler_backend/README.md)。
- **每个 backend 的生成物语义**：`-gen-dag-isel`、`-gen-global-isel` 产出的 matcher table 格式属于 GlobalISel/SelectionDAG 的课，本课只讲它们**怎么被生成**、以及为什么 `InstrInfoEmitter` 会成为最大热点。
- **Clang 的 `-gen-clang-*` backend 细节**：结构同源，只在需要对照时提。
- **TableGen 的 LSP / jupyter kernel**：Ch 0 会指出它们存在，但不深入。

## 精选资料库

### 官方文档

- **[TableGen Overview](https://llvm.org/docs/TableGen/index.html)**
  - 三段式架构（parse → records → backend）的官方表述。Ch 1 会把它落到具体文件和函数。先读这一页，再读本课 Ch 1。
- **[TableGen Programmer's Reference](https://llvm.org/docs/TableGen/ProgRef.html)**
  - 约 2400 行的语言参考，是本课的「规格书」。本课不重复它的内容，而是讲**这份规格在代码里怎么实现**。读某章时对照相应小节。
- **[TableGen Backend Developer's Guide](https://llvm.org/docs/TableGen/BackGuide.html)**
  - `RecordKeeper`/`Record`/`Init` 的官方 API 说明 + backend 骨架。Ch 13–14 的主要依据。
- **[TableGen BackEnds](https://llvm.org/docs/TableGen/BackEnds.html)**
  - 每个 `-gen-*` backend 产出的内容说明。查「这个 `.inc` 是谁生成的」时的第一站。

### 源码（本课的真正教材）

- **`llvm/lib/TableGen/`**（14 个 `.cpp`，约 1.1 万行）
  - 语言实现本体的全部：`TGLexer`、`TGParser`、`Record`、`SetTheory`、`Main`、`Error`、`JSONBackend`。
- **`llvm/include/llvm/TableGen/Record.h`**（约 2500 行）
  - 数据模型的唯一定义。`Init` 的 30 个左右子类全在这里，是本课 Part 1 的地图。
- **`llvm/utils/TableGen/`**（85 个 `.cpp`，约 6.5 万行，含 `Common/` 与各 target 专属 emitter）
  - backend 的真实写法样本。Ch 17 精读 `InstrInfoEmitter`，Ch 14 从零写一个。

### 测试与工具

- **`llvm/test/TableGen/`**（501 个 `.td` 文件，lit 实际运行 496 个测试）
  - 语言语义的可执行规格。你加的每条语法都该在这里有对应测试。
- **`llvm/utils/TableGen/jupyter/`**
  - 官方 TableGen Jupyter kernel + 教程 notebook。想交互式探索 Record 时的捷径，本课不深入。

### 读完之后

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)**（把本课的 backend 知识接到真实 codegen）
- **[GlobalISel 系列课程](../globalisel_instruction_select/README.md)**（matcher table 的消费端）
- **LLVM Discourse 的 `TableGen` 标签**（上游讨论与 RFC 的实际战场）
