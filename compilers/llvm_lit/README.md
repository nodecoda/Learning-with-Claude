# LLVM lit 深潜：从写一条 `RUN:` 行到改造 lit

> 给完全没接触过 lit、但会写代码、想真正掌握 LLVM 测试基础设施的工程师。

这门课不讲「LLVM 怎么用」，而是讲**LLVM 的测试是怎么被发现的、被执行的、被判定成败的**。主线从一条最小 `RUN:` 行开始，一路下潜到 `llvm/utils/lit` 的 Python 源码：配置如何级联、shell 如何被解析、测试如何并行、结果如何变成 `PASS/FAIL/XFAIL/XPASS`，最后你能为一个新项目搭出可发布的 lit suite，并给上游 lit 提交带测试的改动。

> **证据约定**：本课所有命令输出都在本机真实运行，环境为 LLVM `24.0.0git` / lit `24.0.0dev`（`/home/ubuntu/llvm/llvm-project`，commit `3fa53fc0e1b2`，2026-10-30），Ubuntu 22.04 x86_64，Python 3.10.12。需要改 LLVM 源码重新构建的章节标为 **guided**，给出文件/行号与预期行为，但不伪造构建日志。版本差异会逐条标注。

---

## 为什么学这个？

- **LLVM 的回归测试就是 lit**：本机 `llvm/test` 下有 **71,921 个文件**，其中 **47,531 个 `.ll`**、**5,615 个 `.mir`**（2026-11-08 实测 `find` 计数）。你每天跑的 `check-llvm` 背后就是 lit 在发现、调度、执行这七万多个测试。不懂 lit，测试失败时你只能反复重跑；懂 lit，你知道「哪条命令真的跑了、退出码从哪来、为什么它是 XFAIL 而不是 FAIL」。
- **lit 不是断言库**：这是最大的认知误区。lit 负责**发现、配置、调度、执行、报告**；真正判断输出对不对的是 FileCheck、`diff`、`grep` 或测试程序自己的退出码。把 lit 当 FileCheck 学，会一直学不到点上。
- **配置才是门槛**：会写 `RUN:` 的人很多，能说清 `lit.cfg`、`lit.site.cfg`、`lit.local.cfg` 三者关系，`available_features` 怎么传播，`config.substitutions` 与 `ToolSubst` 谁先匹配的人很少。而这正是「能维护一个 suite」与「会抄测试」的分水岭。
- **lit 源码小到可以读完**：`llvm/utils/lit/lit` 约 **10,071 行** Python，核心 `TestRunner.py` 2,315 行、`cl_arguments.py` 664 行、`run.py` 362 行。它是 LLVM 里少数**一个人能完整读完、还能安全改**的基础设施。
- **上游门槛可控**：lit 的改动通常只碰 Python + 自测，review 关注「CLI 兼容性、错误信息、跨平台、测试覆盖」，不需要跑完整 codegen。`tools:llvm-lit` 标签下长期有适合新贡献者的 issue。
- **可复用**：你给自己的编译器、DSL、工具链搭测试框架时，lit 的「suite 配置 + shell 格式 + 并行调度 + xUnit 输出」是一套经过七万个测试验证过的设计。

## 学完你站在哪里

- 能写出一条**失败得有意义**的测试：知道它为什么跑、跑了什么、失败时该看哪一行。
- 能独立为一个新项目搭 lit suite：`lit.cfg` + `lit.site.cfg` + features + tool substitutions + CMake/CI 集成。
- 能诊断 `FAIL / XFAIL / XPASS / UNSUPPORTED / UNRESOLVED / TIMEOUT / FLAKYPASS`，而不是看到红字就重跑。
- 能读通 `TestRunner.py` 的解析与执行两条路径，说清 lit 内置 shell 与外部 shell 的差异。
- 能给 `llvm/utils/lit` 加一个 CLI 选项、directive 或 test format，并配上 lit 自测。
- 能给上游提交一个符合 LLVM 风格的 lit patch。

## 先修知识

- **Python 3.10 基础**：能读 class、函数、`argparse`、`multiprocessing`；不需要会写大型 Python 项目。Part 4 会逐段读源码。
- **命令行基础**：重定向 `>`/`>>`、管道 `|`、`&&`、退出码 `$?`、环境变量。不熟的话，Ch 7–8 会补。
- **LLVM IR 入门**：看得懂 `define i32 @f(i32 %x)` 与 `opt -S -passes=instcombine` 的输出即可；不要求会写 pass。
- **完全没接触过 lit 也没关系**：Ch 0 从「跑第一个测试」开始，Ch 1 才讲为什么。

## 课程地图

### Part 0 — 地基：先跑起来，再问为什么（Ch 0–3）

- [Ch 0 环境、版本与第一个 lit suite](./00-environment-setup.md) — 用源码里的 lit 跑一个最小 suite，读懂退出码与摘要。
- [Ch 1 为什么是 lit：LLVM 测试哲学与工具边界](./01-why-lit.md) — unit test / regression test / TestSuite 的分工，lit 与 FileCheck 各管什么。
- [Ch 2 lit 全景架构：一次调用经过哪些对象](./02-lit-architecture.md) — `main → discovery → TestingConfig → TestRunner → run/worker → display/reports` 全链路。
- [Ch 3 测试发现与配置级联](./03-discovery-and-config-files.md) — `lit.cfg` / `lit.site.cfg` / `lit.local.cfg`、`suffixes`、`excludes`、`--config-prefix`。

### Part 1 — 写测试：ShTest 语言（Ch 4–9）

- [Ch 4 第一条 `RUN:` 行](./04-first-run-line.md) — `%s`、`%t`、退出码、命令展开；第一次看 `-v` 的完整输出。
- [Ch 5 Directives：`RUN`、`REQUIRES`、`UNSUPPORTED`、`XFAIL`、`ALLOW_RETRIES`](./05-directives.md) — 解析规则、行继续、`END.`、注释不是前提。
- [Ch 6 替换系统全景](./06-substitutions.md) — `%s/%S/%t/%p`、`%{s:stem}`、`%if`、`%(line)`、`%{readfile:}`、`DEFINE/REDEFINE`。
- [Ch 7 lit 内置 shell：词法、管道、重定向](./07-internal-shell.md) — `ShLexer`、`ShParser`、`Pipeline`、`Seq`；lit 的 shell 不是 bash。
- [Ch 8 内置命令与执行环境](./08-shell-builtins.md) — `cd`、`export`、`mkdir`、`rm`、`not`、`ulimit`、变量展开的真实边界。
- [Ch 9 FileCheck 与 lit 的边界](./09-filecheck-boundary.md) — `RUN` 是胶水，`CHECK` 不是 lit 语法；失败时如何分清谁错了。
- [练习 A：写一组能诊断的 LLVM IR 测试](./practice-a-diagnosable-ir-tests.md) — 从正常路径、边界到故意失败；要求每个测试失败时都能定位。

### Part 2 — 配置：把一个 suite 搭起来（Ch 10–14）

- [Ch 10 `lit_config` 与 `config`：两个对象的一生](./10-config-objects.md) — 谁创建、谁复制、`parent`/`root`、`finish()` 做了什么。
- [Ch 11 从零写 `lit.cfg`](./11-lit-cfg-from-scratch.md) — `name`、`test_format`、`suffixes`、source/exec root、`excludes`、`substitutions`。
- [Ch 12 `lit.site.cfg` 与 CMake：构建树怎么接进来](./12-site-cfg-and-cmake.md) — `configure_lit_site_cfg`、`add_lit_testsuite`、`check-llvm`、`LLVM_EXTERNAL_LIT`。
- [Ch 13 Features 与条件测试](./13-features.md) — `available_features`、`REQUIRES` 布尔表达式、`target=`/`asserts`/平台 feature。
- [Ch 14 工具替换：`lit.llvm`、`ToolSubst`、`FindTool`](./14-tool-substitutions.md) — 为什么 `opt` 能被替换、`%opt` 为什么会被二次替换、找不到工具怎么办。
- [练习 B：给一个玩具编译器搭 lit suite](./practice-b-toy-compiler-suite.md) — 自带 `lit.cfg`、site cfg、features、工具替换与 CI 入口。

### Part 3 — 运行：并行、筛选、报告、诊断（Ch 15–18）

- [Ch 15 并行执行模型](./15-parallel-execution.md) — `run.py`、`worker.py`、`-j`、`parallelism_group`、资源竞争与 `%t` 隔离。
- [Ch 16 选择、排序、分片与重跑](./16-selection-and-rerun.md) — `--filter`、`--filter-out`、`--order`、`--num-shards`、`--filter-failed`、`--max-retries-per-test`、`--max-failures`。
- [Ch 17 输出与报告](./17-reports-and-output.md) — `-v/-a/-q/-s`、`-o`、xUnit、JSON、ResultDB、`.lit_test_times.txt`。
- [Ch 18 失败诊断学](./18-failure-forensics.md) — `RUN: at line N`、`%t` 残留、超时、flaky、内置/外部 shell 差异。
- [练习 C：从一份失败报告定位根因](./practice-c-failure-forensics.md) — 给 suite、日志与退出码，要求写出「实际执行了什么」和最小修复。

### Part 4 — 内部：读源码，改源码（Ch 19–23）

- [Ch 19 `TestRunner` I：从注释到脚本](./19-testrunner-parsing.md) — `parseIntegratedTestScriptCommands`、keyword parser、`DEFINE/REDEFINE`。
- [Ch 20 `TestRunner` II：脚本如何被执行](./20-testrunner-execution.md) — `executeShTest`、替换应用顺序、内置 shell 与外部 shell 两条路径。
- [Ch 21 Test/Result 模型：状态码的语义](./21-result-model.md) — `PASS/FAIL/XFAIL/XPASS/UNSUPPORTED/UNRESOLVED/TIMEOUT/FLAKYPASS/FIXED`。
- [Ch 22 Discovery 与 TestingConfig 源码深潜](./22-config-internals.md) — 配置缓存、local cfg 复制、feature 传播、嵌套 suite。
- [Ch 23 GoogleTest format：另一种测试发现](./23-googletest-format.md) — `--gtest_list_tests`、sharding、XML/JSON、与 ShTest 的差异。
- [练习 D：给 lit 加一个自定义 format](./practice-d-custom-format.md) — 实现 `TestFormat`、注册到 config、补 lit 自测。

### Part 5 — 工程化与上游（Ch 24–25 + Final）

- [Ch 24 自建 suite 的工程化](./24-production-suite.md) — 目录布局、pip 安装 lit、版本 pin、CI、跨平台、缓存与增量。
- [Ch 25 给上游 lit 提交改动](./25-upstream-lit.md) — issue 选择、CLI 兼容、错误信息、自测、release notes、review 关注点。
- [Final Project：从零做一个可发布的 lit 测试框架](./final-project-lit-framework.md) — 整合 70% 以上核心概念：suite、format、directive、报告、CI 与上游补丁。

## 章节规模与练习位置

- 普通章 300–450 行；原理深挖章 400–550 行；练习 400–600 行；Final 500–700 行。
- 每个 Part 结束有练习：A（Part 1）、B（Part 2）、C（Part 3）、D（Part 4）；Final 覆盖全课。
- 每章固定收尾：`踩雷集锦`、`重点整理`、`自我检核`、`延伸阅读`、`→ 下一章`。

## 环境与版本

> **环境**：LLVM `24.0.0git`，lit `24.0.0dev`，Ubuntu 22.04 x86_64，Python 3.10.12，GCC 11.4。

三种拿到 lit 的方式，本课都会用到：

```bash
# 1) 直接跑源码里的入口（不需要安装）
python3 /home/ubuntu/llvm/llvm-project/llvm/utils/lit/lit.py --version
# lit 24.0.0dev

# 2) 用构建目录里的 llvm-lit（LLVM 自家测试的入口）
/home/ubuntu/llvm/builds/pr-lane/bin/llvm-lit --version
# lit 24.0.0dev

# 3) 安装到你自己的 Python 环境（自建 suite / CI 用）
python3 -m pip install -e /home/ubuntu/llvm/llvm-project/llvm/utils/lit
lit --version
```

**版本钉子（很重要）**：

- `%T` 在 LLVM 24 已被移除（commit `7ff6973f1ae9`，2025-09-26），当前 lit 会把 `%T` **原样保留**，不会报错也不会展开。`llvm/docs/TestingGuide.md` 仍保留 `%T` 的说明，这是**文档滞后**，不要照抄。需要临时目录时用 `%t` 或 `rm -rf %t && mkdir %t`。
- 当前替换系统新增了 `%{s:basename}`、`%{s:stem}`、`%{t:stem}`、`%basename_t`、`%{readfile:...}`、`%if ... %{...%}`、`%(line)` 等；旧教程里的 `%b` 在本机源码中**不存在**。
- `ShTest(execute_external=True)` 自 LLVM 23 起已废弃，LLVM 24 默认使用**内置 shell**；外部 shell 只在显式 `force_execute_external=True` 或环境覆盖时使用。

## 本课不涵盖什么

- **FileCheck 语言全貌**：`CHECK-DAG`/`CHECK-NEXT`/`CHECK-SAME`/变量/`--implicit-check-not` 只讲与 lit 的交界和诊断，不写成 FileCheck 专课。
- **LLVM IR 与 pass 编写**：需要时用最小 IR 举例，不教你写 InstCombine。
- **LLVM Test Suite（LNT / 性能基准）**：那是另一套跑分基础设施，Ch 1 会说明边界。
- **Clang 测试套件的所有细节**：结构同源，只在 CMake 与 site cfg 处对照。
- **lit 的 GUI / IDE 集成**：不涉及。

## 学习信条

1. **先问「lit 实际执行了什么」**，再问「为什么失败」。`-v` 和 `-a` 是你的第一工具，不是最后手段。
2. **文档与源码冲突时，信源码**。本课会多次展示这种冲突（`%T` 就是第一个）。
3. **测试要能失败**。一个永远不会红的测试不是测试，是注释。每写一个 `RUN`，都想一遍「它怎么才会失败」。
4. **配置从最小开始**。不要一上来抄 LLVM 的 900 行 `lit.cfg`；先让 1 个测试跑起来，再逐项加 feature、substitution、site cfg。
5. **读源码要带着问题**：`%t` 是在哪一行拼出来的？`XPASS` 为什么算失败？`RUN:` 为什么出现在字符串里也会被解析？每个问题都能落到具体文件。

## 精选资料库

### 官方文档

- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)**
  - lit 的 CLI、测试发现、状态码、substitutions、内部架构的官方说明。Ch 2、Ch 3、Ch 6 的主要对照物。注意它和 `TestingGuide` 都有滞后，遇到 `%T` 之类以源码为准。
- **[LLVM Testing Infrastructure Guide](https://llvm.org/docs/TestingGuide.html)**
  - LLVM 回归测试的组织方式、`RUN`/`CHECK` 写法、`REQUIRES`/`XFAIL`、平台限制、`split-file`、`update_test_checks.py`。Ch 4–9 的官方背景。
- **[LLVM Test Suite Guide](https://llvm.org/docs/TestSuiteGuide.html)**
  - `test-suite`（性能/基准）与 lit 的关系。Ch 1 会用它划边界，本课不深入。
- **[lit README](https://github.com/llvm/llvm-project/blob/main/llvm/utils/lit/README.rst)**
  - 如何跑 lit 自测、如何给 lit 贡献。Ch 25 的入口。

### 源码（本课真正的教材）

- **`llvm/utils/lit/lit/TestRunner.py`**（2,315 行）
  - directive 解析、替换、内置 shell 执行、结果判定。Ch 19–20 逐段读。
- **`llvm/utils/lit/lit/Test.py`**（466 行）
  - `Test`、`Result`、`ResultCode`、`XFAIL` 语义。Ch 21 的核心。
- **`llvm/utils/lit/lit/discovery.py`**（286 行）+ **`TestingConfig.py`**（303 行）
  - suite 发现、配置级联、local cfg 复制。Ch 22。
- **`llvm/utils/lit/lit/run.py`**（362 行）+ **`worker.py`**（112 行）
  - 并行调度、超时、失败上限。Ch 15。
- **`llvm/utils/lit/lit/llvm/config.py`**（916 行）+ **`llvm/subst.py`**（152 行）
  - LLVM 专属的工具替换、feature 检测、`ToolSubst`/`FindTool`。Ch 13–14。
- **`llvm/utils/lit/tests/`**（647 个文件）
  - lit 自己的可执行规格。Ch 19–25 的测试写法范例。

### LLVM 真实测试

- **`llvm/test/lit.cfg.py`** 与 **`llvm/test/lit.site.cfg.py.in`**
  - 一个成熟 suite 的真实配置：suffixes、features、PATH、site 参数注入。Ch 11–12 的对照。
- **`llvm/test/Transforms/InstCombine/`**
  - 大量 `RUN` + `FileCheck` 的真实样本，适合练习「读一条 RUN 行」。
- **`llvm/utils/lit/tests/lit.cfg`**
  - lit 自测的 suite 配置，展示了 `lit.llvm`、`--path`、`not`/`FileCheck` 依赖。

### 读完之后

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)**（把测试放回编译流程里理解）
- **[`compilers/tablegen_internals`](../tablegen_internals/README.md)**（另一门「读源码 + 改上游」的 LLVM 课，方法论可复用）
- **[`compilers/riscv_maintenance`](../riscv_maintenance/README.md)**（真实后端维护中的 lit 使用与失败诊断）

---

> 下一步：先把 lit 跑起来。Ch 0 不需要你构建 LLVM，只需要一个 Python 和一份最小的 suite。

→ [Ch 0 环境、版本与第一个 lit suite](./00-environment-setup.md)
