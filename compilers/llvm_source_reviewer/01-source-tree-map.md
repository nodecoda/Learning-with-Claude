# Ch 1 — 源码树地图：从 3184 个 `.cpp` 找到该看的那一个

> **目标**：把 `llvm-project` 从「一个巨大的目录」变成一张可按问题检索的地图。学完这章，你能在几分钟内判断一个问题该去 `IR`、`Analysis`、`Transforms`、`CodeGen` 还是 `MC`，能区分头文件里的契约与 `lib` 里的实现，并会用 `rg` 把类、pipeline 注册点和 IR flag 解析点钉到 `path:line`。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）。树规模与 `rg` 输出都在这个 checkout 上实测；工具行为来自 `/home/ubuntu/llvm/builds/pr-lane/bin` 的 `clang`/`opt`/`llc` 24.0.0git。构建自带的版本串是 `a5eaf194d8b6`，与本章源码 checkout 不是同一个提交；源码定位以 `7c7a9072f736` 为准，命令行为以该构建为准。这个差异不影响「目录怎么读」，但提醒你不要把构建哈希当成源码哈希。

## 先把 1.7G 变成坐标

先量一次，不要凭感觉记规模：

```bash
$ cd /home/ubuntu/llvm/llvm-project
$ du -sh llvm
1.7G	llvm
$ find llvm/lib -name '*.cpp' | wc -l
3184
$ find llvm/include/llvm -name '*.h' | wc -l
2213
```

这三个数字说明两件事。第一，`llvm/lib` 的 3184 个 `.cpp` 里包含大量 target backend、测试辅助代码和平台适配，不能按「一个 pass 一个文件」去数。第二，`llvm/include/llvm` 的 2213 个头文件不是「公共 API 全集」，其中还有模板、生成代码的接口和内部实现细节。数字是导航用的，不是分类依据。

真正有用的问题是：

1. 我要找的是**数据结构的定义**，还是**某个 pass 什么时候被插进 pipeline**？
2. 我要看的是**契约**（谁能调用、保证什么），还是**一次具体执行**（谁改了 IR）？
3. 这个问题在源码里应该由**测试**锁住，还是只由**文档**描述？

这三个问题决定了你打开 `include/`、`lib/`、`test/` 还是 `docs/`。

## 直觉：include 是合同，lib 是执行

LLVM 的目录布局有一条主线：**公开声明和实现分家**。

```text
llvm/include/llvm/IR/Dominators.h
  class DominatorTree : public DominatorTreeBase<BasicBlock, false>
        │
        │  声明「我能做什么」：dominates、invalidate、recalculate
        ▼
llvm/lib/IR/Dominators.cpp
  DominatorTree::invalidate(...)
  DominatorTreeAnalysis::run(...)
        │
        │  实现「我怎么做」
        ▼
llvm/include/llvm/Support/GenericDomTree.h
  template <typename NodeT, bool IsPostDom> class DominatorTreeBase
```

这个镜像关系在大部分组件成立：

| 你看到的路径 | 通常放什么 | 例子 |
| --- | --- | --- |
| `llvm/include/llvm/IR/` | IR 类型的公开声明、inline、模板接口 | `Dominators.h`、`Instructions.h` |
| `llvm/lib/IR/` | IR 类型的非模板实现、analysis 包装 | `Dominators.cpp`、`Instructions.cpp` |
| `llvm/include/llvm/Analysis/` | analysis 的接口与结果类型 | `LoopInfo.h`、`MemorySSA.h` |
| `llvm/lib/Analysis/` | analysis 的计算逻辑 | `LoopInfo.cpp`、`MemorySSA.cpp` |
| `llvm/include/llvm/CodeGen/` | 机器 IR、寄存器、调度接口 | `MachineInstr.h`、`TargetPassConfig.h` |
| `llvm/lib/CodeGen/` | 目标无关代码生成实现 | `TargetPassConfig.cpp`、`CodeGenTargetMachineImpl.cpp` |

有三个常见例外，必须记住：

- **模板实现留在头文件**。`DominatorTreeBase` 的类模板在 `llvm/include/llvm/Support/GenericDomTree.h:263`；树的构建与更新算法在 `llvm/include/llvm/Support/GenericDomTreeConstruction.h`（`DomTreeBuilder::InsertEdge` 在 `:1573`，`DeleteEdge` 在 `:1581`），由 `llvm/lib/IR/Dominators.cpp:30` 包含。你搜实现时找不到同名 `.cpp`，不代表实现不存在。
- **实现不一定在同名子目录**。`DominatorTree` 的类声明在 `llvm/include/llvm/IR/Dominators.h:122`，实现却在 `llvm/lib/IR/Dominators.cpp:71`，而不是 `llvm/lib/Analysis/`。目录名描述的是「主要归属」，不是「唯一归属」。
- **生成代码在构建目录**。TableGen 产出的 `.inc` 不在源码树的 `include/` 下，而在构建目录；源码里看到 `#include "X86GenInstrInfo.inc"` 时，不要用 `rg --files llvm/include` 去找它。

## 问题 → 目录：一张可执行的映射表

| 你要回答的问题 | 先看哪里 | 关键入口 |
| --- | --- | --- |
| IR 的对象模型是什么？`Value`/`User`/`Use` 怎么连？ | `llvm/include/llvm/IR/`、`llvm/lib/IR/` | `Value.h`、`User.h`、`Use.h` |
| 一个 pass 在 pipeline 的哪个位置？ | `llvm/lib/Passes/` | `PassBuilderPipelines.cpp`、`PassRegistry.def` |
| 某个 analysis 怎么算、何时失效？ | `llvm/lib/Analysis/`、对应的 `llvm/lib/IR/` | `Dominators.cpp`、`LoopInfo.cpp` |
| 中端变换的算法在哪里？ | `llvm/lib/Transforms/` | `InstCombine/`、`Scalar/`、`IPO/`、`Vectorize/` |
| IR 怎么变成 Machine IR、怎么分配寄存器？ | `llvm/lib/CodeGen/`、`llvm/lib/Target/<ARCH>/` | `TargetPassConfig.cpp`、`X86CodeGenPassBuilder.cpp` |
| 机器指令怎么编码成字节、怎么处理重定位？ | `llvm/lib/MC/`、`llvm/include/llvm/MC/` | `MCStreamer.cpp`、`MCAssembler.cpp` |
| `SmallVector`、`StringRef`、`DenseMap` 在哪？ | `llvm/include/llvm/ADT/`、`llvm/include/llvm/Support/` | `SmallVector.h`、`StringRef.h` |
| `.td` 怎么变成 `.inc`？ | `llvm/utils/TableGen/`、`llvm/lib/TableGen/` | `llvm-tblgen.cpp`、各 `*Emitter.cpp` |
| 这个行为有没有回归测试？ | `llvm/test/` | 与组件同名的子目录、`lit.cfg.py` |
| 官方语义和 review 规则在哪？ | `llvm/docs/` | `LangRef.md`、`CodeReview.md` |
| C/C++ 怎么变成 cc1 参数？ | `clang/lib/Driver/`、`clang/lib/Frontend/` | `Driver.cpp`、`CompilerInvocation.cpp` |
| 目标文件怎么链接成可执行文件？ | `lld/ELF/` | `Driver.cpp`、`Symbols.cpp`、`Writer.cpp` |

这张表的用法是「先选层，再选文件」。例如「为什么这个 `add` 的 `nsw` 没了」属于中端变换，不要先去 `CodeGen`；「为什么这条机器指令编码错了」才进入 `MC` 和 target backend。

## `tools/` 与 `utils/` 不是同一种目录

两个目录都叫「工具」，但服务对象不同：

| 目录 | 主要产物 | 是否面向最终用户 | 例子 |
| --- | --- | --- | --- |
| `llvm/tools/` | 构建出的可执行程序 | 是 | `opt`、`llc`、`llvm-as`、`llvm-dis` |
| `llvm/utils/` | 构建期生成器、维护脚本、测试基础设施 | 多数不是 | `TableGen/`、`lit/`、`UpdateTestChecks/` |

`llvm/tools/opt/CMakeLists.txt:43` 用 `add_llvm_tool(opt ...)` 产出 `opt`；`llvm/utils/TableGen/CMakeLists.txt:31` 用 `add_tablegen(llvm-tblgen ...)` 产出 TableGen 后端工具。`llvm-tblgen` 也是可执行程序，但它属于构建期工具，源码放在 `utils/`，因为它服务于「生成 LLVM 自己的代码」，不是给用户做 IR 优化的入口。

这个区分在 review 里很实用：改 `llvm/tools/` 往往影响命令行行为和用户可见输出；改 `llvm/utils/` 往往影响构建、测试生成或维护脚本，验证方式也不同。

## `test/` 的组织：lit 是按目录发现测试

`llvm/test/` 不是「一堆随手放的 `.ll`」。它由 lit 驱动，目录结构对应组件边界：

```text
llvm/test/
├── Analysis/                 # analysis 的单元式 IR 测试
├── Transforms/
│   ├── InstCombine/          # 每个 .ll 通常锁一个 combine 行为
│   ├── Scalar/               # 其他标量变换
│   └── Vectorize/            # 向量化
├── CodeGen/
│   ├── X86/                  # 目标相关机器码测试
│   └── Generic/              # 目标无关 CodeGen
├── MC/                       # 汇编器、编码、重定位
└── lit.cfg.py                # 测试发现与运行配置
```

`llvm/test/lit.cfg.py:33` 定义了测试后缀：

```python
config.suffixes = [".ll", ".c", ".test", ".txt", ".s", ".mir", ".yaml", ".spv"]
```

`llvm/test/lit.cfg.py:38` 排除了 `Inputs` 等辅助目录：

```python
config.excludes = ["Inputs", "CMakeLists.txt", "README.txt", "LICENSE.txt"]
```

`llvm/test/lit.cfg.py:93` 把测试根目录设为 `llvm/test/` 自身。也就是说，lit 从根往下扫，遇到后缀匹配的文件就当测试；`Inputs/` 里的文件是测试输入，不会被单独执行。

一条真实测试的 `RUN` 行长这样（`llvm/test/Transforms/InstCombine/add.ll:2`）：

```llvm
; RUN: opt < %s -passes=instcombine -S | FileCheck %s
```

这行同时告诉你三件事：用 `opt`、跑 `instcombine`、用 `FileCheck` 对照输出。它是「这个行为怎么被验证」的入口，不是「这个 pass 的完整规范」。

**什么时候该看 `test/`**：

- 你想知道某个变换的**最小可复现输入**：测试通常比真实程序小得多。
- 你要改一个 pass，先看现有测试锁住了哪些行为，避免把预期输出改掉。
- 你在 review 一个 PR，想知道新增测试是否真的覆盖了改动；这时要读 `RUN` 行和 `CHECK` 行，而不是只看文件名。
- 你怀疑文档和实现不一致，测试可以作为「当前被接受的实现行为」的证据，但它仍不是语义规范。

反过来，**不要**把 `test/` 当完整规范。`CHECK` 只断言它写出来的那几行；一个 pass 可能有很多未测试的边界，测试通过不等于所有输入都正确。

## `docs/` 的价值：先拿词汇，再回源码验证

`llvm/docs/` 里有四类文档，读法不同：

| 文档 | 作用 | 本章相关位置 |
| --- | --- | --- |
| `llvm/docs/LangRef.md:5` | IR 的官方语义：类型、指令、flag、poison/undef | 查 `nsw`、`inbounds` 等语义 |
| `llvm/docs/ProgrammersManual.md:1` | LLVM 的 C++ API 导览：`isa`/`cast`/`dyn_cast`、ADT、错误处理 | 读头文件前的 API 背景 |
| `llvm/docs/CodeReview.md:3` | review 制度与实践：什么该 review、LGTM 的含义 | 把源码证据变成 review 判断 |
| `llvm/docs/WritingAnLLVMNewPMPass.md:1` | 新 pass 的最小骨架与 `opt` 注册方式 | 理解 pass 注册的另一面 |

文档的价值是**建立词汇和边界**，不是替代源码。LangRef 会告诉你 `nsw` 的语义，但「LLVM 在哪个函数里把 `nsw` 设到指令上」只有源码能回答。遇到文档与源码冲突，先确认版本，再以对应版本的源码和测试为准；如果冲突仍然存在，把它当成一个值得报告的问题，而不是硬选一个。

## 三个定位动作

下面三个动作是本章的核心技能。每个都从「问题」出发，而不是从「我记得某个目录」出发。

### 动作一：找 `DominatorTree` 的定义与实现

问题：`DominatorTree` 到底在哪定义？它的失效逻辑在哪？

```bash
$ rg --sort path -n 'class DominatorTree :|template <typename NodeT, bool IsPostDom> class DominatorTreeBase|bool DominatorTree::invalidate|DominatorTree DominatorTreeAnalysis::run' \
    llvm/include/llvm/IR/Dominators.h \
    llvm/include/llvm/Support/GenericDomTree.h \
    llvm/lib/IR/Dominators.cpp
llvm/include/llvm/IR/Dominators.h:122:class DominatorTree : public DominatorTreeBase<BasicBlock, false> {
llvm/include/llvm/Support/GenericDomTree.h:263:template <typename NodeT, bool IsPostDom> class DominatorTreeBase {
llvm/lib/IR/Dominators.cpp:71:bool DominatorTree::invalidate(Function &F, const PreservedAnalyses &PA,
llvm/lib/IR/Dominators.cpp:336:DominatorTree DominatorTreeAnalysis::run(Function &F,
```

读法：

- `IR/Dominators.h:122` 是具体类，继承 `DominatorTreeBase<BasicBlock, false>`；`false` 表示前向支配树，不是 post-dominator。
- `Support/GenericDomTree.h:263` 是泛型类模板；构建/更新算法在 `Support/GenericDomTreeConstruction.h`，由 `lib/IR/Dominators.cpp:30` 包含。复杂的树逻辑不在 `Dominators.cpp` 的函数体里。
- `lib/IR/Dominators.cpp:71` 的 `invalidate` 决定 `PreservedAnalyses` 是否保留这棵分析树；这是 Ch 16 的核心。
- `lib/IR/Dominators.cpp:336` 的 `DominatorTreeAnalysis::run` 是 New PM 取分析结果的入口。

注意：`DominatorTree` 的实现在 `lib/IR`，不在 `lib/Analysis`。如果你按目录名去 `lib/Analysis` 找，会找到很多使用它的 pass，却找不到定义。这是目录直觉第一次失效的地方。

### 动作二：找 `InstCombinePass` 的 pipeline 注册

问题：`-passes=instcombine` 和 `-O2` 里的 `InstCombinePass` 是同一套东西吗？

先看 pipeline 构造：

```bash
$ rg -n 'FPM.addPass\(InstCombinePass' llvm/lib/Passes/PassBuilderPipelines.cpp | head -5
388:  FPM.addPass(InstCombinePass());
461:  FPM.addPass(InstCombinePass());
485:  FPM.addPass(InstCombinePass());
498:  FPM.addPass(InstCombinePass());
550:  FPM.addPass(InstCombinePass());
```

这些行分布在不同的 pipeline 构造函数里。`388` 位于 `buildO1FunctionSimplificationPipeline`（函数从 `:370` 开始），`550` 位于 O2 使用的 `buildFunctionSimplificationPipeline`（函数从 `:505` 开始）。同一个 `InstCombinePass` 会在一个优化级别里被插入多次，因为每次前面的 pass 都可能暴露新的 combine 机会。

再看文本名字的注册：

```bash
$ rg -n '"instcombine"' llvm/lib/Passes/PassRegistry.def
630:    "instcombine", "InstCombinePass",
```

`PassRegistry.def:630` 把命令行字符串 `instcombine` 映射到 `InstCombinePass`；`PassBuilderPipelines.cpp` 决定它在默认 pipeline 里的位置；真正的执行体在 `llvm/lib/Transforms/InstCombine/InstructionCombining.cpp:6328` 的 `InstCombinePass::run`。

所以「pipeline 注册」有两层含义：

1. **文本注册**：`-passes=instcombine` 能解析成某个 pass。
2. **调度注册**：`-O2` 的默认 pipeline 在哪些位置插入这个 pass。

Review 时两层都要看。只改 `PassRegistry.def` 不会改变默认 `-O2`；只改 `PassBuilderPipelines.cpp` 不会让 `-passes=instcombine` 消失。

### 动作三：找某个 IR flag 的解析处

问题：`add nsw` 里的 `nsw` 是在哪里被读进 `Instruction` 的？

```bash
$ rg -n 'kw_nsw' llvm/lib/AsmParser/LLParser.cpp
4780:      if (EatIfPresent(lltok::kw_nsw)) {
7806:    bool NSW = EatIfPresent(lltok::kw_nsw);
7898:    bool NSW = EatIfPresent(lltok::kw_nsw);
```

三个命中对应三个语法位置：

- `4780`：常量表达式（`constant expr`）里的 `add`/`sub`/`xor` 等。
- `7806`：普通二进制指令（`add`、`sub`、`mul`、`shl`）的 flag 解析。
- `7898`：`trunc` 指令的 flag 解析。

普通指令那条路径在 `7806` 读到 `NSW` 后，于 `7813` 调用：

```cpp
if (NSW) cast<BinaryOperator>(Inst)->setHasNoSignedWrap(true);
```

token 本身的定义在 `llvm/include/llvm/AsmParser/LLToken.h:112` 的 `kw_nsw`。这条链是「词法 token → 语法分支 → 指令属性」的完整例子。以后看到任何 IR flag，先搜 token 名，再搜 `setHas...`，通常就能定位解析和落点。

## 先看头文件，还是先看调用点？

没有一条规则适用于所有问题。按问题类型选入口：

| 你的问题 | 第一站 | 原因 |
| --- | --- | --- |
| 这个类提供什么接口？谁拥有谁？ | 头文件 | 声明、注释、类型关系都在头文件 |
| 这个函数在什么条件下被调用？ | 调用点 / pipeline | 头文件不告诉你调度顺序 |
| 这个值为什么被改错？ | 实现 `.cpp` + 调用点 | 需要看具体分支和前置条件 |
| 这个 pass 在 `-O2` 里跑几次？ | `PassBuilderPipelines.cpp` | 调度信息不在 pass 本体 |
| 命令行字符串怎么变成 pass？ | `PassRegistry.def` | 文本注册与调度是两套表 |
| 这个行为有没有被锁住？ | `test/` | 测试给出最小输入和断言 |
| 这个 API 的语义权威在哪？ | `docs/` + 头文件注释 | 文档给语义，头文件给契约 |

三条判断规则：

1. **先确定问题属于「契约」还是「执行」**。契约问题先看头文件；执行问题先看调用点，再下钻实现。
2. **看到声明不要停**。头文件里的 `virtual`、`override`、`= 0` 会告诉你真正的实现可能在 target 子类；只读基类会漏掉实际行为。
3. **看到第一个 `rg` 命中不要停**。同名函数有重载，同一个 pass 有多个插入点，同一个 flag 有多个语法上下文。用 `rg -n` 看全部命中，再用上下文 `sed -n` 确认。

## 一个边界例子：同名符号不等于同一个东西

假设你要找「`DominatorTree` 的实现」，先按目录直觉去 `lib/Analysis`：

```bash
$ rg -n 'class DominatorTree :' llvm/lib/Analysis
$ echo $?
1
```

没有命中。再搜宽松一点，却会看到 `PostDominatorTree`：

```bash
$ rg -n 'DominatorTreeAnalysis' llvm/lib/Analysis | head -3
llvm/lib/Analysis/PostDominators.cpp:47:  auto PAC = PA.getChecker<PostDominatorTreeAnalysis>();
llvm/lib/Analysis/PostDominators.cpp:94:AnalysisKey PostDominatorTreeAnalysis::Key;
llvm/lib/Analysis/PostDominators.cpp:96:PostDominatorTree PostDominatorTreeAnalysis::run(Function &F,
```

这些是 post-dominator 的实现与使用者，不是 `DominatorTree` 的定义。`DominatorTree` 是 Analysis 的使用者最常请求的结果类型之一，`llvm/lib/Analysis`、`llvm/lib/Transforms` 里到处都是 `AM.getResult<DominatorTreeAnalysis>(F)`。这些命中说明「谁在用」，不说明「谁定义」。

正确的做法是加限定词：

```bash
$ rg -n 'class DominatorTree|DominatorTree::|DominatorTreeAnalysis::run' \
    llvm/include/llvm/IR llvm/lib/IR
```

这也是本章反复强调的模式：**目录提供候选集，符号和上下文负责收敛**。不要让目录名替你做最终判断。

## 踩雷集锦

1. **以为所有 analysis 都在 `lib/Analysis`**。`DominatorTree` 的定义和核心实现在 `lib/IR/Dominators.cpp`；`lib/Analysis` 里有大量使用者，却不是它的家。错误直觉是「目录名等于归属」，正确认识是「先看声明在哪个库，再看实现落在哪个库」。
2. **把 `include/llvm` 当成完整实现**。头文件里的 `class` 只给接口；模板实现可能藏在 `Support/GenericDomTree.h` 或 `GenericDomTreeConstruction.h`，非模板实现可能在 `lib/IR`，生成代码在构建目录。只读头文件会得到「能做什么」，得不到「怎么做」。
3. **把 `test/` 当成规范**。`CHECK` 只锁住写出来的行；一个 pass 的未测试边界不会因为测试通过就自动正确。测试是证据，不是公理。
4. **把 `tools/` 和 `utils/` 混为一谈**。`opt` 是用户工具，`llvm-tblgen` 是构建期生成器；两者都用 `add_*` CMake 宏，但验证方式和影响面不同。
5. **拿第一个 `rg` 命中当答案**。`InstCombinePass` 在 `PassBuilderPipelines.cpp` 里有二十多个插入点；`nsw` 在 `LLParser.cpp` 里有常量表达式、二进制指令、`trunc` 三个上下文。第一个命中通常只是最近的一个，不是最相关的一个。

## 本章重点整理

- 树规模是导航背景：`llvm/` 1.7G、`llvm/lib` 3184 个 `.cpp`、`llvm/include/llvm` 2213 个 `.h`；数字不构成分类。
- `include/llvm` 放契约和模板接口，`lib` 放实现；同名子目录是默认映射，不是硬约束。
- 问题先分层：IR 对象模型、pipeline、analysis、中端变换、CodeGen、MC、Support/ADT、TableGen、测试、文档、driver、lld。
- `tools/` 是用户可执行程序，`utils/` 是构建期生成器与维护脚本；`llvm-tblgen` 属于后者。
- `test/` 由 lit 按目录和后缀发现；`RUN` 行给出复现命令，`CHECK` 行只锁住被断言的行为。
- 定位一个符号时，用「声明 → 实现 → 调用点 → 测试」四步，不要停在第一个 `rg` 命中。

## 自我检核

- [ ] 不看笔记，能说出 `llvm/`、`llvm/lib`、`llvm/include/llvm` 的三个实测规模数字，并解释它们为什么不能直接当分类依据
- [ ] 能画出 `include/llvm/IR/Dominators.h` 与 `lib/IR/Dominators.cpp` 的镜像关系，并说出一个不遵守该镜像的例外
- [ ] 给定「这个 pass 在 O2 里跑几次」，能说出先去哪个文件、再看哪个文件
- [ ] 给定「这个 IR flag 在哪里被解析」，能写出从 token 到 `setHas...` 的搜索路径
- [ ] 能解释 `tools/` 与 `utils/` 的差别，并各举一个真实目录
- [ ] 能说出 `llvm/test/lit.cfg.py` 中 `suffixes`、`excludes`、`test_source_root` 各自控制什么
- [ ] 能解释为什么 `test/` 通过不等于行为正确，以及在 review 中应该怎样读 `RUN`/`CHECK`
- [ ] 能说出「先看头文件」和「先看调用点」各自的适用问题，并各举一个例子
- [ ] 遇到同名符号的多个 `rg` 命中时，知道用哪些限定词收敛

## 延伸阅读

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`Introduction` 与 `Important and useful LLVM APIs` 中的 `isa<>`、`cast<>`、`dyn_cast<>` 小节
  - **能学到什么**：LLVM 的类型判别和类层次阅读方式，补上本章「头文件里的契约」这一层
  - **前提知识**：能读 C++ 模板和继承；不要求写过 LLVM pass
- **[lit Command Guide](https://llvm.org/docs/CommandGuide/lit.html)**
  - **读哪里**：`TEST DISCOVERY`、`LOCAL CONFIGURATION FILES`、`SUBSTITUTIONS`
  - **能学到什么**：`llvm/test` 如何被扫描、`lit.cfg.py` 如何影响测试发现，以及 `RUN` 行里的替换规则
  - **前提知识**：跑过至少一条 `llvm-lit` 测试；知道 `FileCheck` 的基本用法
- **[LLVM Code-Review Policy and Practices](https://llvm.org/docs/CodeReview.html)**
  - **读哪里**：`What Code Should Be Reviewed?`、`LGTM - How a Patch Is Accepted`、`Splitting Requests and Conditional Acceptance`
  - **能学到什么**：为什么源码定位和测试证据要一起进入 review，以及「有条件 LGTM」在 LLVM 里的含义
  - **前提知识**：读过至少一个 LLVM GitHub PR 的 diff
- **[Writing an LLVM New PM Pass](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)**
  - **读哪里**：`Running a pass with opt` 与 `Testing a pass`
  - **能学到什么**：从另一个方向理解 `PassRegistry.def` 和 `PassBuilderPipelines.cpp` 的分工
  - **前提知识**：知道 `opt -passes=` 的基本用法；读过本章的「动作二」

→ [Ch 2 — 一次编译的完整旅程：driver → cc1 → opt → llc → MC → lld](./02-compilation-journey.md)
