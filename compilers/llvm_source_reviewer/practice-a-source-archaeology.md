# 练习 A — 源码考古：从一个 flag 追到实现

> **目标**：练「给定一个用户可见行为，沿源码找到实现、注册点、测试」的完整路径。这不是问答练习，是 Ch 1–3 的动作固化：每一步都要有 `path:line` 和一条可复现命令。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project` @ `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`。练习中的行号以这棵树为准，升级后会漂；验收以符号和输出为准。

## 规则

1. 先自己做，再看 `<details>` 里的参考解答。
2. 每个任务都要写出**文件路径 + 行号 + 命令 + 真实输出**；只写结论不算完成。
3. 允许用 `rg`、`git log -S`、`llvm-config`、`--help-hidden`；不允许直接搜网页。
4. 行号对不上时，用符号重新定位，并在答案里说明「行号漂了，实际在 X」。
5. 每个任务的验收标准是：另一个人按你的答案能复现同样的输出。

## 任务 A1：`instcombine<max-iterations=N>` 的默认值与解析链

**规格**：`opt -passes='instcombine<max-iterations=N>'` 可以控制 InstCombine 的最大迭代次数。请回答：

1. 默认值是多少？定义在哪个文件/行？
2. 这个参数在 pipeline 文本里由哪个函数解析？解析失败时会发生什么？
3. `-print-pipeline-passes` 打印出来的文本长什么样？`max-iterations=4` 和默认值有什么区别？

**交付物**：

```text
默认值: <数值> @ <path:line>
解析函数: <函数名> @ <path:line>
打印位置: <函数名> @ <path:line>
命令与输出: ...
```

**提示**：

- 先 `rg -n "max-iterations" llvm/lib llvm/include`。
- 默认值在 `InstCombineOptions` 结构体里，不在命令行解析处。
- 解析器在 `PassBuilder` 的 pipeline 解析路径上，和 `verify-fixpoint` 相邻。
- 打印函数是 `InstCombinePass::printPipeline`。

**验收**：两条 `opt --print-pipeline-passes` 输出都能贴出来，并解释 `verify-fixpoint` 为什么一个有 `no-`、一个没有。

<details>
<summary>参考解答</summary>

默认值：

```text
1 @ llvm/include/llvm/Transforms/InstCombine/InstCombine.h:29
static constexpr unsigned InstCombineDefaultMaxIterations = 1;
```

`InstCombineOptions` 在 `InstCombine.h:31`，`MaxIterations = InstCombineDefaultMaxIterations` 在 `:34`。

解析函数：`PassBuilder::parseInstCombineOptions`（或同文件内的 `parsePassPipeline` 分支）在 `llvm/lib/Passes/PassBuilder.cpp:1282`：

```cpp
} else if (Enable && ParamName.consume_front("max-iterations=")) {
  ...
  Result.setMaxIterations((unsigned)MaxIterations.getZExtValue());
```

非法值的错误文本在 `:1286`。`verify-fixpoint` 的分支在 `:1280`。

打印位置：`InstCombinePass::printPipeline`，`llvm/lib/Transforms/InstCombine/InstructionCombining.cpp:6321`：

```cpp
OS << "max-iterations=" << Options.MaxIterations << ";";
```

实测命令：

```bash
$B/opt --print-pipeline-passes -passes='instcombine<max-iterations=4>' -disable-output empty.ll
```

实测输出：

```text
function(instcombine<max-iterations=4;verify-fixpoint>),verify
```

默认值（`default<O2>` 文本里）是 `instcombine<max-iterations=1;no-verify-fixpoint>`。`no-` 来自 `InstCombineOptions::VerifyFixpoint` 的默认值；显式写 `instcombine<max-iterations=4>` 时默认开启 fixpoint 校验，所以打印为 `verify-fixpoint`。这个差异本身就是一个值得注意的点：**同一个 pass 在不同 pipeline 构造路径下，默认选项可能不同**。

</details>

## 任务 A2：`-verify-each` 从命令行到 verifier

**规格**：回答 `opt -verify-each` 是如何变成「每个 pass 后都跑 Verifier」的：

1. 命令行选项定义在哪个文件/行？
2. 它如何影响 pass manager 的构造？找到把 `VerifyEach` 映射到 `VerifierKind::EachPass` 的那一行。
3. `StandardInstrumentations` 里如何处理 `VerifyEach`？
4. 给一条命令，证明每个 pass 后确实跑了 verifier。

**交付物**：四个 `path:line` + 一条命令输出。

**提示**：

- 选项定义不在 `opt.cpp`，在 `tools/opt/optdriver.cpp`。
- 搜 `VerifierKind::EachPass`。
- `StandardInstrumentations` 的构造函数接收 `VerifyEach`。
- `-debug-pass-manager` 会把 `Verifying function ...` 和 `VerifierPass` 打出来。

**验收**：命令输出里同时出现 `Verifying function` 和 `VerifierPass`。

<details>
<summary>参考解答</summary>

定义：`llvm/tools/opt/optdriver.cpp:152`：

```cpp
static cl::opt<bool> VerifyEach("verify-each",
                                cl::desc("Verify after each transform"));
```

映射：同文件 `:822`：

```cpp
VerifierKind VK = VerifierKind::InputOutput;
if (NoVerify)
  VK = VerifierKind::None;
else if (VerifyEach)
  VK = VerifierKind::EachPass;
```

`StandardInstrumentations` 的构造函数在 `llvm/lib/Passes/StandardInstrumentations.cpp:2422` 接收 `bool VerifyEach`，并在 `:2510` 附近注册 verifier instrumentation。

实测命令：

```bash
$B/opt -passes='function(instcombine)' -verify-each -debug-pass-manager -disable-output min.ll
```

实测输出（节选）：

```text
Running pass: InstCombinePass on f (4 instructions)
Running analysis: DominatorTreeAnalysis on f
...
Verifying function f
Running pass: VerifierPass on [module]
Running analysis: VerifierAnalysis on [module]
```

`Verifying function f` 是函数级验证，`VerifierPass` 是模块级验证。两者都出现，说明 `-verify-each` 不是「最后验一次」，而是每个 pass 之后都验。

</details>

## 任务 A3：`musttail` 的 Verifier 检查

**规格**：构造一段 `musttail` 违规的 IR，回答：

1. 你构造的 IR 违反了哪条规则？
2. Verifier 的报错原文是什么？
3. 报错对应 `Verifier.cpp` 的哪个检查？给出 `path:line`。
4. 为什么这个检查必须放在 Verifier，而不能只放在 parser？

**交付物**：IR、命令、报错原文、`path:line`、一段解释。

**提示**：

- `musttail` 后面必须紧跟 `ret`，且返回值必须被返回。
- 报错文本是源码里的字符串字面量，直接 `rg`。
- 相关检查在 `Verifier::visitCallBase` 附近。

**验收**：报错原文与 `rg` 到的字符串完全一致。

<details>
<summary>参考解答</summary>

IR：

```llvm
declare i32 @g(i32)
define i32 @f(i32 %x) {
  %r = musttail call i32 @g(i32 %x)
  %s = add i32 %r, 1
  ret i32 %s
}
```

命令：

```bash
$B/opt -passes=verify -disable-output musttail.ll
```

实测报错：

```text
musttail call must precede a ret
  %r = musttail call i32 @g(i32 %x)
.../opt: musttail.ll: error: input module is broken!
```

源码：`llvm/lib/IR/Verifier.cpp:4554`：

```cpp
Check(Ret, "musttail call must precede a ret", &CI);
```

另有 `:4557` 的 `musttail call result must be returned`，覆盖「下一条是 ret，但返回的不是 musttail 的返回值」。

为什么不能只放在 parser：`musttail` 的合法性依赖「下一条指令是不是 ret」「返回值是不是这个 call 的结果」，这是跨指令的结构条件，parser 逐条解析时没有足够上下文（而且 bitcode 路径不经过文本 parser）。Verifier 在完整 IR 上检查，才能同时覆盖 `.ll` 和 `.bc` 两条入口。

</details>

## 任务 A4：`DominatorTreeAnalysis` 如何被 `print<domtree>` 找到

**规格**：回答：

1. `opt -passes='print<domtree>'` 里的 `domtree` 这个名字注册在哪里？给出 `path:line`。
2. `PassBuilder::registerFunctionAnalyses` 在哪个文件/行？它如何把 `DominatorTreeAnalysis` 注册进 `FunctionAnalysisManager`？
3. 实际跑一次 `print<domtree>`，贴出输出。

**交付物**：两个 `path:line` + 命令输出 + 一段「名字 → 类型 → 注册」的链路说明。

**提示**：

- 名字在 `llvm/lib/Passes/PassRegistry.def`，搜 `FUNCTION_ANALYSIS("domtree"`。
- 注册函数在 `llvm/lib/Passes/PassBuilder.cpp:738`。
- 宏 `FUNCTION_ANALYSIS` 会展开成 `FAM.registerPass([&] { return CREATE_PASS; })`。
- `print<domtree>` 的打印器在 `llvm/include/llvm/IR/Dominators.h`。

**验收**：输出里有 `DominatorTree for function: f`。

<details>
<summary>参考解答</summary>

注册名：`llvm/lib/Passes/PassRegistry.def:368`：

```cpp
FUNCTION_ANALYSIS("domtree", DominatorTreeAnalysis())
```

注册函数：`llvm/lib/Passes/PassBuilder.cpp:738`：

```cpp
void PassBuilder::registerFunctionAnalyses(FunctionAnalysisManager &FAM) {
```

宏在 `:749` 附近展开：

```cpp
#define FUNCTION_ANALYSIS(NAME, CREATE_PASS)                                   \
  FAM.registerPass([&] { return CREATE_PASS; });
```

`DominatorTreeAnalysis` 类型定义在 `llvm/include/llvm/IR/Dominators.h:241`。

实测命令：

```bash
$B/opt -passes='print<domtree>' -disable-output min.ll
```

实测输出：

```text
DominatorTree for function: f
=============================--------------------------------
Inorder Dominator Tree: DFSNumbers invalid: 0 slow queries.
  [1] %entry {4294967295,4294967295} [0]
Roots: %entry
```

链路：pipeline 文本里的 `domtree` → `PassRegistry.def` 的 `FUNCTION_ANALYSIS` → `PassBuilder::registerFunctionAnalyses` 的宏展开 → `FAM.registerPass` → `DominatorTreeAnalysis::run` 计算并缓存 → `DominatorTreePrinterPass` 打印。任何一环改名/漏注册，`print<domtree>` 都会报「unknown analysis」。

</details>

## 任务 A5：`add X, 0` 到底是谁折叠的

**规格**：`opt -passes=instcombine` 会把：

```llvm
%a = add i32 %x, 0
```

折叠成 `%x`。请回答：

1. 这条折叠发生在哪个函数？给出 `path:line`。
2. 它是 `visitAdd` 的哪个分支调用的？
3. 对应的测试在哪个文件、哪个函数？给出 `path:line`。
4. 如果操作数是 `poison` 而不是 0，行为是什么？为什么？

**交付物**：三个 `path:line` + 命令输出 + 一段 poison 解释。

**提示**：

- `visitAdd` 在 `llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp:1590`。
- 它第一步调用 `simplifyAddInst`。
- 在 `InstructionSimplify.cpp` 里搜 `X + 0 -> X`。
- 测试在 `llvm/test/Transforms/InstCombine/add.ll`，搜 `add i32 %x, 0`。

**验收**：能指出「`visitAdd` → `simplifyAddInst` → `m_Zero` 分支」这条链，而不是只说「InstCombine 做的」。

<details>
<summary>参考解答</summary>

折叠在 `llvm/lib/Analysis/InstructionSimplify.cpp:594` 的 `simplifyAddInst`：

```cpp
static Value *simplifyAddInst(Value *Op0, Value *Op1, bool IsNSW, bool IsNUW,
                              const SimplifyQuery &Q, unsigned MaxRecurse) {
  if (Constant *C = foldOrCommuteConstant(Instruction::Add, Op0, Op1, Q))
    return C;

  // X + poison -> poison
  if (isa<PoisonValue>(Op1))
    return Op1;

  // X + undef -> undef
  if (Q.isUndefValue(Op1))
    return Op1;

  // X + 0 -> X
  if (match(Op1, m_Zero()))
    return Op0;
```

调用链：`InstCombinerImpl::visitAdd`（`InstCombineAddSub.cpp:1590`）第一步：

```cpp
if (Value *V = simplifyAddInst(I.getOperand(0), I.getOperand(1),
                               I.hasNoSignedWrap(), I.hasNoUnsignedWrap(),
                               SQ.getWithInstruction(&I)))
  return replaceInstUsesWith(I, V);
```

测试：`llvm/test/Transforms/InstCombine/add.ll:255` 的 `@add_masked_overwrite_const_0`，其中 `%a = add i32 %x, 0` 被折叠掉，CHECK 里只剩 `add i32 [[A]], [[B]]`。

实测命令：

```bash
$B/opt -passes=instcombine -S zero.ll
```

输出：

```llvm
define i32 @z(i32 %x) {
  ret i32 %x
}
```

poison 分支：`X + poison -> poison` 在 `X + 0 -> X` **之前**，所以如果第二个操作数是 poison，结果是 poison，不会走到「加 0 等于自己」。这体现了一个通用规则：**poison 的传播优先于代数恒等式**。如果顺序反过来，`poison + 0` 就会被错误地折叠成 poison 之外的某个值（虽然 poison 可以 refinement 成任意值，但保持传播更精确、也更容易推理）。

</details>

## 加分挑战

1. 用 `git log -S 'InstCombineDefaultMaxIterations' --oneline -- llvm/include/llvm/Transforms/InstCombine/InstCombine.h` 找出这个默认值是何时引入/改变的，读 commit message。
2. 给 A5 加一个 `add nsw i32 %x, 0` 的变体，确认 `nsw` 是否影响折叠，并解释为什么。
3. 找一个你常用的 `opt` flag，用同样的五步（pipeline 文本 → 注册 → 解析 → 实现 → 测试）写一份考古报告。

## 自我检核

- [ ] 每个答案都有 `path:line`，不是只写文件名
- [ ] 每条命令都能在干净 shell 里复现
- [ ] 能解释「名字 → 类型 → 注册 → 执行」的完整链路
- [ ] 能区分「parser 能查」和「Verifier 才能查」的规则
- [ ] 知道行号漂移时怎么用符号重新定位
- [ ] 能说出至少一个「默认值因 pipeline 构造路径不同而不同」的例子

## 延伸阅读

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`PassManager`、`AnalysisManager`、`dyn_cast` 相关章节
  - **和本练习的关联**：A2/A4 的注册与查询机制
- **[LLVM Command Guide: opt](https://llvm.org/docs/CommandGuide/opt.html)**
  - **读哪里**：`-passes`、`-print-pipeline-passes`、`-verify-each`、`-debug-pass-manager`
  - **为什么值得读**：本练习所有命令的官方说明
- **[LLVM FileCheck](https://llvm.org/docs/CommandGuide/FileCheck.html)**
  - **读哪里**：`CHECK-LABEL` 与匹配区间
  - **和本练习的关联**：A5 的测试阅读需要理解 CHECK 区间
- **[llvm-project source](https://github.com/llvm/llvm-project)**
  - **读哪里**：`llvm/lib/Passes/PassRegistry.def`、`llvm/lib/Passes/PassBuilder.cpp`
  - **为什么值得读**：所有 pipeline 名字的单一事实来源

→ [Ch 4 — `Value` / `User` / `Use`：def-use 链表与操作数所有权](./04-value-user-use.md)
