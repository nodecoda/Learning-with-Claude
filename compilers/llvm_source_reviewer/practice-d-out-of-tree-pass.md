# 练习 D — 写一个 out-of-tree pass 并故意漏报 `PreservedAnalyses`

> **目标**：亲手制造一次「stale analysis」：让一个 pass 改了 CFG，却声称所有分析都还有效。然后用 `-debug-pass-manager` 和 `print<domtree>` 看到错误结果，最后用正确方式修复。这个练习把 Ch 13/14/16 从「读源码」变成「你亲手写出的 bug」。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project` @ `7c7a9072f736`，构建 `/home/ubuntu/llvm/builds/pr-lane`（`BUILD_SHARED_LIBS=ON`，assertions ON）。本练习的插件在本机构建上真实编译、加载、运行；输出均为实测。

## 为什么做这个？

`PreservedAnalyses` 是 LLVM New PM 里最容易被「顺手写错」的 API：

- 返回 `PreservedAnalyses::all()` 很省事，但如果改了 IR，后续 pass 会读到过期的 DominatorTree/LoopInfo。
- 返回 `preserveSet<CFGAnalyses>()` 看起来「我只改了 CFG，所以 CFG 分析应该保留」，但语义正好相反：`CFGAnalyses` 是一组**依赖 CFG 的分析**，你改了 CFG，它们才更应该失效。
- 正确做法要么返回 `none()`（全部重算），要么在修改 CFG 时用 `DomTreeUpdater` 更新，并只保留你确实维护了的分析。

review 时，这类 bug 的 diff 往往只差一行 `return PreservedAnalyses::all();`。你必须能一眼看出「这个 pass 改了 CFG，却返回 all()」。这个练习让你自己踩一次，之后看别人的 diff 会有肌肉记忆。

## 准备

```bash
mkdir -p ~/llvm-lab/pa-plugin && cd ~/llvm-lab/pa-plugin
B=/home/ubuntu/llvm/builds/pr-lane/bin
SRC=/home/ubuntu/llvm/llvm-project
```

确认构建是共享库模式（否则插件链接方式不同）：

```bash
$B/llvm-config --shared-mode
```

实测输出：

```text
shared
```

## D1：最小插件（什么都不改，只打印）

`lie.cpp`：

```cpp
#include "llvm/IR/PassManager.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

namespace {
struct LiePass : OptionalPassInfoMixin<LiePass> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM) {
    errs() << "[LiePass] " << F.getName() << "\n";
    return PreservedAnalyses::all();
  }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "LiePass", "0.1",
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name == "lie-pass") {
                    FPM.addPass(LiePass());
                    return true;
                  }
                  return false;
                });
          }};
}
```

编译（一行，不使用 CMake）：

```bash
g++ -std=c++17 -fPIC -shared lie.cpp -o LiePass.so \
  $($B/llvm-config --cxxflags) \
  $($B/llvm-config --ldflags) \
  $($B/llvm-config --libs core analysis support passes) \
  -Wl,-rpath,$($B/llvm-config --libdir)
```

跑：

```bash
$B/opt -load-pass-plugin=./LiePass.so -passes='function(lie-pass)' \
  -disable-output /tmp/llvm-src-course/min.ll
```

实测输出：

```text
[LiePass] f
```

> **LLVM 24 API 变化**：`PassInfoMixin` 现在位于 `llvm::detail`，out-of-tree pass 应继承 `llvm::OptionalPassInfoMixin` 或 `llvm::RequiredPassInfoMixin`（`llvm/include/llvm/IR/PassManager.h:103/114`）。如果你照旧写 `PassInfoMixin<MyPass>`，会得到 `expected template-name before '<'`。另外插件头文件是 `llvm/Plugins/PassPlugin.h`，不是旧路径 `llvm/Passes/PassPlugin.h`。

## D2：改 CFG，但谎报「所有分析都保留」

`split.cpp`（关键部分）：

```cpp
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Dominators.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Plugins/PassPlugin.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

namespace {
struct SplitPass : OptionalPassInfoMixin<SplitPass> {
  int Mode;
  explicit SplitPass(int Mode) : Mode(Mode) {}

  PreservedAnalyses run(Function &F, FunctionAnalysisManager &AM) {
    if (F.empty())
      return PreservedAnalyses::all();
    BasicBlock &BB = F.getEntryBlock();
    if (BB.size() < 2)
      return PreservedAnalyses::all();

    BB.splitBasicBlock(BB.begin(), "split");
    errs() << "[SplitPass] split " << F.getName() << ", mode=" << Mode << "\n";

    if (Mode == 1)
      return PreservedAnalyses::all();          // 错误：CFG 变了
    if (Mode == 2) {
      PreservedAnalyses PA;
      PA.preserveSet<CFGAnalyses>();            // 也错误：语义反了
      return PA;
    }
    return PreservedAnalyses::none();           // 正确：全部失效
  }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "SplitPass", "0.1",
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name == "split-lie") { FPM.addPass(SplitPass(1)); return true; }
                  if (Name == "split-cfg") { FPM.addPass(SplitPass(2)); return true; }
                  if (Name == "split-ok")  { FPM.addPass(SplitPass(3)); return true; }
                  return false;
                });
          }};
}
```

编译方式同 D1（把 `lie.cpp` 换成 `split.cpp`，输出 `SplitPass.so`）。

关键点是 `BB.splitBasicBlock(BB.begin(), "split")`：它把入口块从第一条指令处切开，新建 `%split`，入口块只剩一个 `br`。CFG 从 1 个块变成 2 个块。

## D3：三种返回值，三种结果

先 `require<domtree>` 把 DominatorTree 算出来并缓存，再跑修改 CFG 的 pass，最后 `print<domtree>`：

```bash
$B/opt -load-pass-plugin=./SplitPass.so \
  -passes='function(require<domtree>,split-lie,print<domtree>)' \
  -disable-output /tmp/llvm-src-course/min.ll
```

实测（mode=1，`all()`）：

```text
[SplitPass] split f, mode=1
DominatorTree for function: f
=============================--------------------------------
Inorder Dominator Tree: DFSNumbers invalid: 0 slow queries.
  [1] %entry {4294967295,4294967295} [0]
Roots: %entry
```

只有 `%entry`，`%split` 不见了。因为 pass 声称所有分析保留，AnalysisManager 没有让 DT 失效，`print<domtree>` 读到的是旧的 DT。

mode=2（`preserveSet<CFGAnalyses>()`）实测输出**一模一样**：

```text
[SplitPass] split f, mode=2
...
  [1] %entry {4294967295,4294967295} [0]
Roots: %entry
```

mode=3（`none()`）实测：

```text
[SplitPass] split f, mode=3
DominatorTree for function: f
=============================--------------------------------
Inorder Dominator Tree: DFSNumbers invalid: 0 slow queries.
  [1] %entry {4294967295,4294967295} [0]
    [2] %split {4294967295,4294967295} [1]
Roots: %entry
```

这次 DT 重算了，`%split` 出现。三种返回值的差异可以压成一张表：

| 返回值 | AnalysisManager 行为 | 结果 |
| --- | --- | --- |
| `all()` | 什么都不失效 | stale DT；后续 pass 看到旧 CFG |
| `preserveSet<CFGAnalyses>()` | 保留所有 CFG 相关分析 | 同样 stale；语义正好反了 |
| `none()` | 全部失效，按需重算 | 正确，但可能多算 |

## D4：用 `-debug-pass-manager` 看失效

对 mode=3：

```bash
$B/opt -load-pass-plugin=./SplitPass.so \
  -passes='function(require<domtree>,split-ok,print<domtree>)' \
  -debug-pass-manager -disable-output /tmp/llvm-src-course/min.ll
```

实测（节选）：

```text
Running pass: RequireAnalysisPass<llvm::DominatorTreeAnalysis, llvm::Function> on f (4 instructions)
Running analysis: DominatorTreeAnalysis on f
Running pass: {anonymous}::SplitPass on f (4 instructions)
[SplitPass] split f, mode=3
Invalidating analysis: DominatorTreeAnalysis on f
Running pass: DominatorTreePrinterPass on f (5 instructions)
Running analysis: DominatorTreeAnalysis on f
```

把 mode 换成 1，`Invalidating analysis` 这一行会消失。这就是 review 时最想看到的证据：**改了 CFG 的 pass，后面应该出现 invalidation，或者出现 `DomTreeUpdater` 的更新记录。**

## D5：正确修复的两种方式

### 方式 A：返回 `none()`（最简单）

适合「改完 CFG 就不管了」的 pass。代价是后续所有分析重算。对于小函数、低频 pass，这通常是最安全的选择。

### 方式 B：用 `DomTreeUpdater` 更新，再保留

如果你确实想保留 DT，必须在每次增删边时提交更新：

```cpp
DomTreeUpdater DTU(AM.getResult<DominatorTreeAnalysis>(F),
                   DomTreeUpdater::UpdateStrategy::Eager);
DTU.applyUpdates({{DominatorTree::Delete, &BB, &Split}});
// ... 修改 CFG ...
DTU.applyUpdates({{DominatorTree::Insert, &BB, &Split}});
```

然后返回：

```cpp
PreservedAnalyses PA;
PA.preserve<DominatorTreeAnalysis>();
return PA;
```

注意：`splitBasicBlock` 自己不会帮你更新 DT；它只改 IR。你必须手动提交 edge updates，或者在修改后调用 `DTU.getDomTree().recalculate(F)`（代价更高）。无论哪种，`preserve<DominatorTreeAnalysis>()` 的前提是「你确实更新了它」。

> 本练习不要求你把方式 B 写完整；重点是能解释「为什么 `preserveSet<CFGAnalyses>()` 是错的」以及「`none()` 为什么安全」。

## D4.5：`-verify-each` 抓不到 stale analysis

很多人以为「verifier 过了就没问题」。对 mode=1 加 `-verify-each`：

```bash
$B/opt -load-pass-plugin=./SplitPass.so \
  -passes='function(require<domtree>,split-lie,print<domtree>)' \
  -verify-each -disable-output /tmp/llvm-src-course/min.ll
```

实测输出与不加 `-verify-each` 完全一样，退出码 `0`，DT 仍然是 stale 的：

```text
[SplitPass] split f, mode=1
DominatorTree for function: f
...
  [1] %entry {4294967295,4294967295} [0]
Roots: %entry
```

原因：Verifier 检查的是 **IR 结构合法性**，它不知道 `PreservedAnalyses` 说了什么，也没有责任检查 analysis 是否过期。`-verify-each` 只能抓「pass 产生了非法 IR」，抓不到「pass 产生了合法 IR 但 analysis 缓存已过期」。

这也是 review 时最容易漏的一类：CI 全绿、verifier 全过，但某个后续 pass 在 stale DT 上做决策，产生 miscompile。你必须单独检查 `PreservedAnalyses`。

## D5.5：`PreservedAnalyses` 的 API 语义

打开 `llvm/include/llvm/IR/PassManager.h`，把常用 API 和语义对上：

| API | 语义 | 什么时候用 |
| --- | --- | --- |
| `PreservedAnalyses::all()` | 声明「什么都没变」 | 真的没改 IR/CFG/metadata |
| `PreservedAnalyses::none()` | 声明「全部失效」 | 改了 IR，懒得逐项维护 |
| `PA.preserve<T>()` | 只保留分析 T | 你确实维护了 T 的结果 |
| `PA.preserveSet<CFGAnalyses>()` | 保留「所有 CFG 相关分析」 | 你**没有**改 CFG 时；改了 CFG 反而不能用 |
| `PA.isPreserved<T>()` | 查询是否保留 | 组合/转发 PA 时 |
| `PA.abandon<T>()` | 从保留集合移除 T | 组合 pass 时局部撤销 |

最容易误解的是 `preserveSet<CFGAnalyses>()`：它不是「我要保留 CFG」，而是「我声明所有依赖 CFG 的分析仍然有效」。如果你改了 CFG，这个声明就是假的。正确做法是 `none()`，或者更新 DT 后只 `preserve<DominatorTreeAnalysis>()`。

## D6：在 review 里识别这类 bug

看到一个 pass 的 diff，按下面顺序扫：

1. **它改了什么？** 搜索 `splitBasicBlock`、`eraseFromParent`、`addSuccessor`、`removePredecessor`、`renumberBlocks`、`replaceAllUsesWith`。
2. **它返回什么？** 找 `return PreservedAnalyses::...`。
3. **两者匹配吗？**
   - 改了 CFG + `all()` → blocker；
   - 改了 CFG + `preserveSet<CFGAnalyses>()` → blocker；
   - 改了 IR + `all()` → blocker（除非只改 metadata/调试信息）；
   - 没改 + `none()` → 只是性能问题，不是正确性。
4. **有没有更新？** 如果保留 DT，搜 `DomTreeUpdater`、`applyUpdates`、`recalculate`。
5. **测试能抓吗？** 大概率不能（`-verify-each` 也抓不到）；要求作者加一个 `-debug-pass-manager` 的输出或专门的 analysis 查询测试。

一条合格的 review comment：

```text
[blocker] llvm/lib/.../Foo.cpp:120
这个 pass 调用了 splitBasicBlock，但返回 PreservedAnalyses::all()。
后续 pass 会读到 stale DominatorTree；-verify-each 抓不到。
最小改法：返回 none()，或用 DomTreeUpdater 更新后只 preserve<DominatorTreeAnalysis>()。
```

## 卡点提示

1. **`PassInfoMixin` 找不到**：LLVM 24 里它在 `llvm::detail`；用 `OptionalPassInfoMixin`/`RequiredPassInfoMixin`。
2. **`PassPlugin.h` 找不到**：路径是 `llvm/Plugins/PassPlugin.h`。
3. **链接失败/加载失败**：确认 `llvm-config --shared-mode` 是 `shared`，并加 `-Wl,-rpath,$($B/llvm-config --libdir)`。
4. **`opt` 报 unknown pass**：检查 `registerPipelineParsingCallback` 的名字与 `-passes=` 一致；插件是否真的加载（`-load-pass-plugin` 路径）。
5. **`print<domtree>` 看不到新块**：先确认 `require<domtree>` 在修改前执行；再看 pass 返回值是否错误地保留了 DT。
6. **`splitBasicBlock` 后 verifier 报错**：检查 phi、`musttail`、landingpad 等对 block 结构的约束；本练习的 `min.ll` 结构足够简单，不会触发。

## 验收标准

- [ ] 能编译并加载 `LiePass.so`，看到 `[LiePass] f`
- [ ] 能复现 mode=1/2 的 stale DT（只有 `%entry`）
- [ ] 能复现 mode=3 的正确 DT（有 `%split`）
- [ ] 能在 `-debug-pass-manager` 里指出 `Invalidating analysis: DominatorTreeAnalysis`
- [ ] 能解释 `preserveSet<CFGAnalyses>()` 为什么不是「保留 CFG 分析」的安全写法
- [ ] 能说出方式 A/B 的取舍，以及方式 B 需要哪些额外更新

## 加分挑战

1. 把 `SplitPass` 改成「删除一个基本块」，观察 `LoopInfo` 是否也 stale；用 `print<loops>` 验证。
2. 给 `SplitPass` 加一个 `-verify-each` 运行，确认 verifier 不会抓 stale analysis（它不是 IR 合法性检查）。
3. 用 `DomTreeUpdater` 实现方式 B，确保 `print<domtree>` 不重算也能看到新块；用 `-debug-pass-manager` 证明没有 `Invalidating analysis`。
4. 把这个练习改成一个 review 题：给同学一段「改 CFG + 返回 all()」的 diff，要求他在 60 秒内指出问题。

## 常见编译错误与真实修复

这三个错误在本机都真实出现过，值得提前知道。

### 错误 1：`PassInfoMixin` 找不到

```text
error: expected template-name before '<' token
struct LiePass : PassInfoMixin<LiePass> {
```

原因：LLVM 24 把 `PassInfoMixin` 放进了 `llvm::detail`，out-of-tree pass 应使用顶层的 `OptionalPassInfoMixin`/`RequiredPassInfoMixin`（`llvm/include/llvm/IR/PassManager.h:103/114`）。

修复：

```cpp
struct LiePass : OptionalPassInfoMixin<LiePass> { ... };
```

### 错误 2：`PassPlugin.h` 路径

```text
fatal error: llvm/Passes/PassPlugin.h: No such file or directory
```

原因：当前树的插件头文件在 `llvm/Plugins/PassPlugin.h`。旧教程/旧代码里的 `llvm/Passes/PassPlugin.h` 已经不适用。

修复：

```cpp
#include "llvm/Plugins/PassPlugin.h"
```

### 错误 3：`PassBuilder` 是 incomplete type

```text
error: invalid use of incomplete type 'class llvm::PassBuilder'
```

原因：`PassPlugin.h` 只前置声明了 `PassBuilder`，`registerPipelineParsingCallback` 需要完整定义。

修复：

```cpp
#include "llvm/Passes/PassBuilder.h"
```

## 评分标准

| 项 | 分值 | 满分标准 |
| --- | --- | --- |
| 插件能编译/加载 | 20 | `opt` 能识别 `lie-pass` |
| stale DT 复现 | 20 | mode=1/2 输出只有 `%entry` |
| 正确行为复现 | 20 | mode=3 输出包含 `%split` |
| 观测证据 | 15 | 能指出 `Invalidating analysis` 的有/无 |
| 解释能力 | 15 | 能说明 `preserveSet<CFGAnalyses>()` 为什么错 |
| 修复方案 | 10 | 能给出 `none()` 与 `DomTreeUpdater` 两种方案及取舍 |

## 延伸阅读

- **[Writing an LLVM New PM Pass](https://llvm.org/docs/WritingAnLLVMNewPMPass.html)**
  - **读哪里**：插件注册、`PreservedAnalyses`、`-load-pass-plugin` 的完整示例
  - **和本练习的关联**：D1 的骨架来自官方文档；本练习补上了「错误返回值」的对照实验
- **[LLVM PassManager.h](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/PassManager.h)**
  - **读哪里**：`PreservedAnalyses`、`AnalysisManager`、`RequiredPassInfoMixin`/`OptionalPassInfoMixin`
  - **为什么值得读**：`all()`/`none()`/`preserveSet` 的语义以头文件注释为准
- **[LLVM DomTreeUpdater](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/Analysis/DomTreeUpdater.h)**
  - **读哪里**：`applyUpdates`、`UpdateStrategy`、`insertEdge`/`deleteEdge`
  - **和本练习的关联**：D5 方式 B 的 API
- **[LLVM Debugging Guide](https://llvm.org/docs/DebuggingLLVM.html)**
  - **读哪里**：`-debug-pass-manager`、`-print-after`、assertions 构建
  - **为什么值得读**：本练习的观测手段都在这里

→ [Ch 17 — 验证与观测：`Verifier`、`Debugify`、`-print-after`、statistics](./17-verification-and-observability.md)
