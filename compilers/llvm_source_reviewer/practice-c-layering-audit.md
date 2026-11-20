# 练习 C — 给一条新依赖画分层图

> **目标**：把「这个 include 能不能加」从口味之争变成可复核的依赖判断。你要为四个拟议改动分别给出合法/非法结论、CMake 证据、失败方式和最小修复，最后审计一个会形成三节点环的 patch，并给出拆分方案。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`。题面引用的 CMake 行号都在 7c7a 上核实过。`pr-lane` 是 `BUILD_SHARED_LIBS=ON` 的开发者构建，目录里没有 `libLLVM.so`；它的源码缓存指向另一棵 `pr-lane` 树（`a5eaf194d8b6`），所以命令输出只用于观察构建行为，组件声明以 7c7a 为准。

> **建议用时**：90–120 分钟。先独立判断，再展开 `<details>`。参考解答不是唯一写法，但证据链要能落到 `path:line`。

## 交付物

为每题写一个四段式结论：

```text
结论：合法 / 非法 / 有条件合法
证据：目标名 + CMakeLists path:line + 依赖方向
失败方式：编译期 / 链接期 / CMake 生成期 / 只是契约退化
修复：最小改动，必要时给出拆分后的依赖边
```

最后一道题额外交一张依赖图，节点是组件，边是 `LINK_COMPONENTS`，环用箭头标出来。

## 先把图摊开

本练习只涉及下面这些节点。先按第 12 章的方法，从 CMake 文件里抄出边，不要凭目录名：

```text
Support ──► Demangle
   ▲
   │
 Core ──► Support, BinaryFormat, Demangle, Option, Remarks, TargetParser
   ▲
   │
Analysis ──► Core, Support, Object, ProfileData, TargetParser
   ▲
   │
TransformUtils ──► Analysis, Core, ProfileData, Support, TargetParser
   ▲
   │
ScalarOpts ──► Analysis, Core, TransformUtils, InstCombine, ...
   ▲
   │
CodeGen ──► Analysis, Core, MC, Target, TransformUtils, Scalar, ...
```

关键文件：

- `llvm/lib/Support/CMakeLists.txt:164`，依赖块 `:361`
- `llvm/lib/IR/CMakeLists.txt:6`，依赖块 `:108`
- `llvm/lib/Analysis/CMakeLists.txt:58`，依赖块 `:197`
- `llvm/lib/Transforms/Utils/CMakeLists.txt:1`，依赖块 `:108`
- `llvm/lib/Transforms/Scalar/CMakeLists.txt:6`，依赖块 `:103`
- `llvm/lib/CodeGen/CMakeLists.txt:45`，依赖块 `:301`
- `llvm/lib/MC/CMakeLists.txt:9`，依赖块 `:88`
- `llvm/lib/Target/CMakeLists.txt:5`，依赖块 `:15`

判断时只问两件事：

1. 新增使用方 `A` 和目标提供方 `B` 之间，现有边是 `A -> B` 还是 `B -> A`？
2. 新增一条 `A -> B` 后，是否出现 `A -> B -> ... -> A`？

记住第 12 章的证据实验：include 能编译，不代表依赖合法。`llvm/CMakeLists.txt:1368` 把 `llvm/include` 加给所有目标，预处理器不看 `LINK_COMPONENTS`。

---

## 题目 1：Support 里 include `llvm/IR/Value.h`

### 任务规格

拟议改动：在 `llvm/lib/Support/ABIBreak.cpp` 里新增

```cpp
#include "llvm/IR/Value.h"
```

并添加一个 helper：

```cpp
namespace llvm {
void describeValue(const Value &V, raw_ostream &OS) {
  OS << V.getName();
}
} // namespace llvm
```

`LLVMSupport` 的 `LINK_COMPONENTS` 保持只有 `Demangle`。判断这个改动合法/非法，并说明如果为了让链接通过而把 `Core` 加进 `LLVMSupport`，会发生什么。

### 验收

- 能指出 `LLVMSupport` 的组件声明位置和当前依赖。
- 能指出 `LLVMCore` 对 `Support` 的依赖，说明方向为什么反了。
- 能说明「编译通过、链接失败」的具体符号或错误类型。
- 修复不能是「把 Core 加到 Support」，要给出一个保持层次的替代方案。

### 提示

1. 先看 `llvm/lib/Support/CMakeLists.txt:361` 的 `LINK_COMPONENTS` 列表。
2. 再看 `llvm/lib/IR/CMakeLists.txt:108` 是否已经有 `Support`。
3. 想一下 `Support` 是给所有组件用的底座。底座反向依赖 `Core`，谁会先被破坏？
4. `Value::getName()` 的符号在哪个库？用 `llvm/lib/IR/CMakeLists.txt` 的源文件列表确认 `Value.cpp` 属于 `LLVMCore`。
5. 如果要格式化值，接收 `StringRef`、`raw_ostream` 或回调，能不能把 IR 类型留在调用方？

<details>
<summary>参考解答</summary>

**结论：非法。** `Support` 不能依赖 `Core`，也不能依赖提供 `Value` 的任何上层库。

**证据：**

- `LLVMSupport` 的依赖只有 `Demangle`：`llvm/lib/Support/CMakeLists.txt:361-363`。
- `LLVMCore` 明确依赖 `Support`：`llvm/lib/IR/CMakeLists.txt:108-114` 的列表包含 `Support`。
- 因此现有方向是 `Core -> Support`。新增 `Support -> Core` 会形成两节点环 `Support -> Core -> Support`。
- `Value.cpp` 是 `LLVMCore` 的源文件，列在 `llvm/lib/IR/CMakeLists.txt:85`；`Value::getName()` 的声明带 `LLVM_ABI`（`llvm/include/llvm/IR/Value.h:279`），符号由 Core 提供。

**失败方式：**

include 本身会成功，因为 `llvm/CMakeLists.txt:1368` 把 `llvm/include` 加给了所有目标。真正的失败在链接：只链接 `LLVMSupport` 的消费者会遇到类似

```text
undefined reference to `llvm::Value::getName() const'
```

如果为了让这个 helper 链接通过，把 `Core` 加入 `LLVMSupport`，在 `pr-lane` 这种 `BUILD_SHARED_LIBS=ON` 的构建里，CMake 生成阶段会报 strongly connected component (cycle)。最小三节点复现的实测错误在 Ch 12 已给出。静态构建可能暂时容忍静态库之间的环，但链接顺序会变成隐式契约，换构建模型就坏。

**修复：**

把「格式化一个 IR 值」的逻辑留在 `IR` 或更高的消费者里。`Support` 只提供与 IR 无关的接口，例如：

```cpp
void printEscapedName(StringRef Name, raw_ostream &OS);
```

如果确实需要多态行为，让调用方传回调：

```cpp
using NamePrinter = function_ref<void(raw_ostream &)>;
void describeValue(NamePrinter Print, raw_ostream &OS);
```

这样 `Support` 不需要知道 `Value`，`Core` 仍然只向下依赖。跨组件复用格式化逻辑时，把它放到 `llvm/lib/IR/AsmWriter.cpp` 或一个明确属于 IR 的公开头，而不是塞回 `Support`。

**如果题目只问「加 Core 到 Support 行不行」：** 不行。它修的是链接错误，不是分层错误；在共享构建下还会把链接错误升级成 CMake 环。

**需在你的构建验证：** 具体的链接错误文本会随优化级别、是否启用 LTO、链接器类型变化；结论「符号由 Core 提供」不依赖这些条件。

</details>

---

## 题目 2：Analysis 里 include `llvm/Transforms/Utils/...`

### 任务规格

拟议改动：在 `llvm/lib/Analysis/AssumptionCache.cpp` 里新增

```cpp
#include "llvm/Transforms/Utils/AssumeBundleBuilder.h"
```

并调用其中的 `buildAssumeFromInst` 来构造 assume bundle。判断这个改动合法/非法，并说明为什么「把 TransformUtils 加进 Analysis 的 `LINK_COMPONENTS`」不是可接受的修法。

### 验收

- 能指出 `LLVMAnalysis` 和 `LLVMTransformUtils` 各自的 `LINK_COMPONENTS`。
- 能画出 `Analysis`、`TransformUtils` 之间现有的边。
- 能说明为什么目录顺序（`Transforms` 在 `Analysis` 前面被 `add_subdirectory`）不能当作依赖依据。
- 给出一个拆分方案，让 Analysis 侧的逻辑不依赖 TransformUtils。

### 提示

1. `llvm/lib/Analysis/CMakeLists.txt:197` 的列表里有没有 `TransformUtils`？
2. `llvm/lib/Transforms/Utils/CMakeLists.txt:108` 的列表里有没有 `Analysis`？
3. 现有边是 `TransformUtils -> Analysis`。再加一条反向边，环的长度是多少？
4. 在 `llvm/lib/Analysis` 里搜索 `#include "llvm/Transforms/`，看看当前有没有先例。
5. `AssumeBundleBuilder` 里哪部分只依赖 IR/Analysis 数据结构？哪部分依赖 transform 层的策略？能不能把前者下移？

<details>
<summary>参考解答</summary>

**结论：非法。** `Analysis` 是 `TransformUtils` 的下层，不能反向 include。

**证据：**

- `LLVMAnalysis` 的依赖列表在 `llvm/lib/Analysis/CMakeLists.txt:197-206`，包含 `Core`、`Support`、`Object`、`ProfileData`、`TargetParser`，没有 `TransformUtils`。
- `LLVMTransformUtils` 的依赖列表在 `llvm/lib/Transforms/Utils/CMakeLists.txt:108-114`，第一项就是 `Analysis`。
- 现有边：`TransformUtils -> Analysis`。新增 `Analysis -> TransformUtils` 后形成两节点环：

```text
Analysis -> TransformUtils -> Analysis
```

- `llvm/lib/CMakeLists.txt:30` 先 `add_subdirectory(Transforms)`，`:32` 才 `add_subdirectory(Analysis)`。这只是目录加入顺序，CMake 目标依赖由 `LINK_COMPONENTS` 决定，不由 `add_subdirectory` 的先后决定。
- 在 7c7a 的 `llvm/lib/Analysis` 和 `llvm/include/llvm/Analysis` 中搜索 `#include "llvm/Transforms/` 没有命中。这个空白不是巧合，是分层约束的结果。

**失败方式：**

include 能通过预处理。链接时，`LLVMAnalysis.so` 需要 `LLVMTransformUtils` 的符号；而 `LLVMTransformUtils.so` 已经需要 `LLVMAnalysis`。`pr-lane` 的共享构建会在 CMake 生成阶段报环（同类错误已在 Ch 12 实测）。静态构建可能让环潜伏到最终链接，具体表现依链接顺序而定。

**修复：**

不要加 `TransformUtils`。先看被调用的 `buildAssumeFromInst` 需要什么：

1. 如果它只做「从已知事实拼 `llvm.assume` 参数」这种 IR 级操作，把这段实现下移到 `llvm/lib/IR/Assumptions.cpp` 或 `Analysis` 自己的 helper。`Analysis` 已经依赖 `Core`，这条边合法。
2. 如果它需要 transform 层的策略（例如遍历、改写、保留分析），把调用点留在 `TransformUtils` 或 `ScalarOpts`，让 `Analysis` 只提供查询接口。
3. 如果两边都需要同一段纯数据逻辑，抽一个不依赖 `TransformUtils` 的小头文件，放在 `Analysis` 或 `IR` 的公开/私有头里；不要为了让 `Analysis` 调它而把整个 `TransformUtils` 拉进来。

一个可接受的边集是：

```text
Analysis -> Core -> Support
TransformUtils -> Analysis
ScalarOpts -> TransformUtils
```

`Analysis` 仍然不知道 `TransformUtils` 的存在。

**需在你的构建验证：** 具体函数是否真的能下移，要看 `AssumeBundleBuilder` 的实现依赖；本题的分层结论不依赖这个细节。

</details>

---

## 题目 3：IR 里使用 Analysis 的 `AssumptionCache`

### 任务规格

拟议改动：在 `llvm/lib/IR/Function.cpp` 里保存一个 `AssumptionCache *`，并在 `Function::getAssumptionCache()` 中返回它。为了让 `Function.cpp` 认识这个类型，新增

```cpp
#include "llvm/Analysis/AssumptionCache.h"
```

并把 `Analysis` 加入 `LLVMCore` 的 `LINK_COMPONENTS`。判断这个改动合法/非法，并区分「只 forward declare」与「实际使用 analysis 结果」两种情况。

### 验收

- 能指出 `LLVMCore` 和 `LLVMAnalysis` 的依赖方向。
- 能说明 `AssumptionCache` 定义在哪个头、属于哪个组件。
- 能指出 `IR` 里已有的 `Assumptions.cpp` 与 `Analysis/AssumptionCache` 的职责差别。
- 给出至少两种修复：把消费者上移，或在 IR 侧定义不依赖 Analysis 的接口。

### 提示

1. `llvm/lib/IR/CMakeLists.txt:108` 的列表里有没有 `Analysis`？
2. `llvm/lib/Analysis/CMakeLists.txt:197` 的列表里有没有 `Core`？
3. `AssumptionCache` 的类定义在 `llvm/include/llvm/Analysis/AssumptionCache.h:45`，构造函数在 `:117`，它属于哪个组件？
4. `llvm/lib/IR/CMakeLists.txt:9` 的 `Assumptions.cpp` 已经在处理 `llvm.assume`。它做的是 IR 语义，还是缓存分析？
5. 如果只是成员指针，forward declaration 能不能避免 include？这会不会仍然把分析结果的生命周期绑进 `Function`？

<details>
<summary>参考解答</summary>

**结论：非法（实际使用 analysis 结果时）。** 只做 forward declaration 可能编译通过，但把 `Function` 的公开契约绑到 Analysis 仍然是分层退化。

**证据：**

- `LLVMCore` 的依赖列表在 `llvm/lib/IR/CMakeLists.txt:108-114`，没有 `Analysis`。
- `LLVMAnalysis` 的依赖列表在 `llvm/lib/Analysis/CMakeLists.txt:197-206`，包含 `Core`。
- 现有边：`Analysis -> Core`。新增 `Core -> Analysis` 形成两节点环。
- `AssumptionCache` 定义在 `llvm/include/llvm/Analysis/AssumptionCache.h:45`；构造函数 `AssumptionCache(Function &F, TargetTransformInfo *TTI = nullptr)` 在 `:117`，带 `LLVM_ABI`。它属于 `LLVMAnalysis` 提供的符号。
- `llvm/lib/IR/Assumptions.cpp` 在 `llvm/lib/IR/CMakeLists.txt:9` 被编进 `LLVMCore`。它处理 `llvm.assume` 的 IR 级语义和 bundle 表示，不提供函数级的惰性 analysis cache。

**失败方式：**

- **实际调用构造函数或方法**：编译需要完整定义，链接需要 `LLVMAnalysis` 的符号。只加 include 不加 `Analysis`，会出现 `undefined reference to llvm::AssumptionCache::...`。
- **加 `Analysis` 到 `LLVMCore`**：共享构建在 CMake 生成阶段形成 `Core -> Analysis -> Core` 环。
- **只 forward declare 指针**：编译可能通过，但 `Function` 的布局/生命周期开始依赖一个它无权拥有的分析对象；这会把「谁负责失效」的问题带进 IR。即使没有链接错误，也应视为设计问题，而不是合法依赖。

**修复：**

1. **上移消费者**：把需要 `AssumptionCache` 的逻辑放到 `Analysis` pass 或 `TransformUtils` 中。`Function` 只保留 IR 自身的数据。
2. **IR 侧定义窄接口**：如果 IR 层只负责「遍历 assume 并回调」，在 `llvm/IR` 里定义一个不依赖 `Analysis` 的 visitor 或 `function_ref` 接口，由 `Analysis` 侧实现缓存。这样调用方仍然在 `Analysis`，`Core` 不反向依赖。
3. **不要**把 `AssumptionCache` 的成员塞进 `Function` 来「顺便缓存」。分析结果的创建、失效和生命周期属于 `AnalysisManager`，第 13、16 章会展开。

一个合法的边集：

```text
Core -> Support
Analysis -> Core
Transforms -> Analysis
```

`Core` 不出现指向 `Analysis` 的边。

**需在你的构建验证：** 如果只改 forward declaration，具体是否触发 `-Wunused` 或 ODR 问题取决于代码形态；分层结论按「是否实际使用 Analysis 符号」区分。

</details>

---

## 题目 4：CodeGen 里使用 Analysis 的 `ScalarEvolution`

### 任务规格

拟议改动：在 `llvm/lib/CodeGen/HardwareLoops.cpp` 里新增对 `ScalarEvolution` 的使用，并 include

```cpp
#include "llvm/Analysis/ScalarEvolution.h"
```

作者在 PR 描述里写「CodeGen 是后端，Analysis 是中端，这个依赖越层了，需要拆」。判断这个说法是否正确，并给出 CMake 证据。若合法，说明是否还需要改 `llvm/lib/CodeGen/CMakeLists.txt`。

### 验收

- 能指出 `LLVMCodeGen` 的 `LINK_COMPONENTS` 是否包含 `Analysis`。
- 能举出至少一个现有的 `CodeGen` 源文件使用 `llvm/Analysis/...` 的真实例子。
- 能解释为什么这条边不构成环，而 `Core -> Analysis` 构成环。
- 能指出「合法」不等于「无条件安全」：还要看分析结果在 codegen 阶段是否可用、是否正确保留。

### 提示

1. `llvm/lib/CodeGen/CMakeLists.txt:301` 的 `LINK_COMPONENTS` 列表里找 `Analysis`。
2. 看 `llvm/lib/CodeGen/HardwareLoops.cpp:25`，以及 `MachineFunctionPass.cpp:27`、`SafeStack.cpp:30`。
3. `HardwareLoops.cpp:42` 还 include 了 `llvm/Transforms/Utils/...`。`CodeGen` 的 CMake 列表里有没有 `TransformUtils`？
4. 画边：`CodeGen -> Analysis` 和 `Analysis -> CodeGen` 哪条存在？`Analysis` 的依赖列表里有没有 `CodeGen`？
5. 分析结果在 codegen 阶段可能已经失效。分层合法只说明「可以链接」，不说明「拿到的结果一定新鲜」。

<details>
<summary>参考解答</summary>

**结论：合法。** 作者的「CodeGen 用 Analysis 一定越层」判断是错的。

**证据：**

- `LLVMCodeGen` 的 `LINK_COMPONENTS` 在 `llvm/lib/CodeGen/CMakeLists.txt:301-317`，明确包含 `Analysis`。
- 现有代码已经在用：
  - `llvm/lib/CodeGen/HardwareLoops.cpp:25`：`#include "llvm/Analysis/ScalarEvolution.h"`
  - `llvm/lib/CodeGen/MachineFunctionPass.cpp:27`：同一个头
  - `llvm/lib/CodeGen/SafeStack.cpp:30`：同一个头
- `LLVMAnalysis` 的依赖列表（`llvm/lib/Analysis/CMakeLists.txt:197-206`）没有 `CodeGen`。所以只有 `CodeGen -> Analysis`，没有反向边，不构成环。
- `CodeGen` 还声明了 `TransformUtils`（`llvm/lib/CodeGen/CMakeLists.txt:316`），所以 `HardwareLoops.cpp:42` 的 `llvm/Transforms/Utils/ScalarEvolutionExpander.h` 也有对应的组件依赖。

**是否需要改 CMake：**

如果只是新增一个 `CodeGen` 目录下的 `.cpp` 并 include 已声明的 `llvm/Analysis/ScalarEvolution.h`，不需要改 `llvm/lib/CodeGen/CMakeLists.txt` 的 `LINK_COMPONENTS`；`Analysis` 已经在列表里。需要改的是源文件列表（把新 `.cpp` 加进 `add_llvm_component_library(LLVMCodeGen ...)`），以及必要的 `DEPENDS`（如果用到 TableGen 生成的头）。

**但要注意：**

- **分析可用性**：`ScalarEvolution` 是函数级分析。codegen 阶段是否还能通过 `MachineFunction` 拿到对应函数的新鲜结果，要看 pass 的 analysis 注册方式；拿不到时应显式重算或改用不依赖它的接口。
- **保留与失效**：如果前面的 pass 改写了循环但没正确保留 `ScalarEvolution`，这里读到的是旧结果。分层正确不补救失效协议，第 16 章会讲 `PreservedAnalyses`。
- **不要因为「合法」就到处加**：`CodeGen` 依赖 `Analysis` 是已声明的边，但每次新增使用仍要问「这个分析在 codegen 时点是否合适」。例如 `ScalarEvolution` 对循环的假设可能已经被 machine-level 变换破坏。

**失败方式（如果作者按错误判断去拆）：**

把 `Analysis` 从 `CodeGen` 的依赖里删掉，会导致现有 `HardwareLoops.cpp`、`MachineFunctionPass.cpp`、`SafeStack.cpp` 全部链接失败。这不是修复，是破坏已声明的契约。

**需在你的构建验证：** 具体 pass 在 codegen pipeline 中能否取得 `ScalarEvolutionAnalysis`，取决于 pass 类型和 analysis manager 的注册；本题只判定 CMake 分层。

</details>

---

## 题目 5：审计一个三节点 CMake diff

### 任务规格

有人提出下面的 patch，声称「只是把两个 helper 放到更合适的位置」。判断它会不会形成依赖环；如果会，指出环上的节点和边，并给出一个不形成环的拆分方案。

```diff
diff --git a/llvm/lib/Analysis/CMakeLists.txt b/llvm/lib/Analysis/CMakeLists.txt
@@
   LINK_COMPONENTS
   BinaryFormat
   Core
+  TransformUtils
   FrontendHLSL
   Object
   Option
   ProfileData
   Support
   TargetParser
   )
diff --git a/llvm/lib/Transforms/Utils/CMakeLists.txt b/llvm/lib/Transforms/Utils/CMakeLists.txt
@@
   LINK_COMPONENTS
   Analysis
   Core
+  ScalarOpts
   ProfileData
   Support
   TargetParser
   )
```

补充约束：

- `pr-lane` 是 `BUILD_SHARED_LIBS=ON`。
- `LLVMScalarOpts` 的依赖列表在 `llvm/lib/Transforms/Scalar/CMakeLists.txt:103-111`，包含 `Analysis` 和 `TransformUtils`。
- 不要假设 `add_subdirectory` 的顺序能打破环。
- 不要新增一个「万能 Utils」组件来掩盖方向。

### 验收

- 能画出 patch 后的有向图，节点至少包含 `Analysis`、`TransformUtils`、`ScalarOpts`。
- 能指出环的长度和每条边的来源（哪个 CMakeLists 的哪一行）。
- 能说明在共享构建与静态构建下，失败时机为什么不同。
- 能给出拆分方案，并说明拆分后每条边为什么合法。
- 能指出至少一个「看起来能修、实际只是把环藏起来」的方案。

### 提示

1. 先不要看 patch 的 `+` 行，先把三条现有边写出来：`TransformUtils -> Analysis`、`ScalarOpts -> Analysis`、`ScalarOpts -> TransformUtils`。
2. patch 加了两条边：`Analysis -> TransformUtils` 和 `TransformUtils -> ScalarOpts`。把它们接到现有图上。
3. 从 `Analysis` 出发走一圈，看能不能回到 `Analysis`。
4. `llvm/cmake/modules/LLVM-Config.cmake:295` 的 `expand_topologically` 用 `visited_libs` 防递归死循环，但它不消除 CMake 目标图里的环。不要把这当成「有环也能配置」。
5. 拆分的关键不是「再加一个组件」，而是把被两边共用的代码下移到不依赖任何一方的地方，或把调用方向反转成回调。

<details>
<summary>参考解答</summary>

**结论：会形成环。** patch 后有三条边首尾相接：

```text
Analysis -> TransformUtils     （patch 新增）
TransformUtils -> ScalarOpts   （patch 新增）
ScalarOpts -> Analysis         （现有，llvm/lib/Transforms/Scalar/CMakeLists.txt:103-111）
```

环是：

```text
Analysis -> TransformUtils -> ScalarOpts -> Analysis
```

**证据：**

- `TransformUtils -> Analysis` 现有：`llvm/lib/Transforms/Utils/CMakeLists.txt:108-114` 的第一项是 `Analysis`。
- `ScalarOpts -> Analysis` 现有：`llvm/lib/Transforms/Scalar/CMakeLists.txt:103-111` 包含 `Analysis`。
- `ScalarOpts -> TransformUtils` 现有：同一段 `:111` 包含 `TransformUtils`。
- patch 新增 `Analysis -> TransformUtils`，把 `Analysis` 和 `TransformUtils` 直接接成两节点环；再新增 `TransformUtils -> ScalarOpts`，把 `ScalarOpts` 也卷进三节点环。即使只加第一条，环已经成立。

**失败方式：**

- **共享构建**：`BUILD_SHARED_LIBS=ON` 时，CMake 在生成阶段检查目标图，报 strongly connected component (cycle)。Ch 12 的最小复现已经实测过同类错误；LLVM 本体 patch 的精确报错 `需在你的构建验证`。
- **静态构建**：CMake 允许静态库之间的环，配置和编译可能通过；环会推迟到最终链接。GNU ld 按命令行顺序解析静态库，环上的符号可能在某些顺序下解析成功、换顺序就失败。macOS、Windows 链接器和 lld 不保证按同样的方式兜底，所以「本机静态构建绿」不是证据。
- **`llvm-config` 展开**：`llvm_map_components_to_libnames` 的 `expand_topologically`（`llvm/cmake/modules/LLVM-Config.cmake:295`）用 `visited_libs` 防止无限递归，所以查询不会无限循环；但这只是遍历保护，不会让 CMake 目标图变成无环。不要把「命令能返回」误判为「依赖合法」。

**拆分方案：**

先判断两个 helper 的真实依赖：

1. **如果 Analysis 需要的 helper 只依赖 Analysis/Core 的数据结构**：把它从 `TransformUtils` 下移到 `Analysis`（或 `Core`，如果它连 Analysis 都不需要）。这样 `Analysis` 不需要新增任何边，`TransformUtils` 继续依赖 `Analysis`，方向不变。
2. **如果 TransformUtils 需要的 helper 依赖 ScalarOpts 的 pass 逻辑**：说明这段代码本来就不该放在 `TransformUtils`。把调用点上移到 `ScalarOpts` 或 `Passes`，让 `TransformUtils` 保持只依赖 `Analysis`。
3. **如果两边都需要同一段逻辑，但它依赖上层策略**：在 `Analysis` 定义窄接口（回调、visitor、`function_ref`），由 `ScalarOpts`/`TransformUtils` 注入实现。依赖边仍然向下，控制流可以向上。
4. **如果确实需要新的共享层**：把它放在 `Support` 或 `Core` 能承载的纯数据/纯接口层，并证明它不依赖 `Analysis`、`TransformUtils`、`ScalarOpts`。新增组件要有独立理由，不能叫「Utils」然后把环藏进去。

拆分后的合法边集示例：

```text
Analysis -> Core -> Support
TransformUtils -> Analysis
ScalarOpts -> TransformUtils
ScalarOpts -> Analysis
```

没有 `Analysis -> TransformUtils`，也没有 `TransformUtils -> ScalarOpts`。

**看起来能修、实际只是藏环的方案：**

- 把 `Analysis -> TransformUtils` 改成 `Analysis -> ScalarOpts`：环变成 `Analysis -> ScalarOpts -> Analysis`，更短。
- 把 `TransformUtils -> ScalarOpts` 改成 `TransformUtils -> Passes`：`Passes` 依赖 `TransformUtils` 和 `ScalarOpts`，环仍然存在，只是多一个节点。
- 新增 `LLVMUtils` 让三方都依赖它：如果 `LLVMUtils` 反向 include 任何一方，环只是换了名字；如果它真的只含纯数据，那它应该并入 `Support`/`Core`，而不是制造第四层。
- 用 `target_link_libraries(... INTERFACE ...)` 绕过 `LINK_COMPONENTS`：CMake 目标图仍然看得到边；共享构建的环检查不会因为属性名改变而消失。

**需在你的构建验证：**

- 真实 LLVM 树的 patch 没有在本题中应用；环的判定来自 7c7a 的 `LINK_COMPONENTS` 边和 patch 文本。
- 如果要在本地验证，复制一份构建目录配置，应用 patch 后跑 `cmake -S llvm -B <build> -DBUILD_SHARED_LIBS=ON`，观察生成阶段错误。不要直接改课程引用的源码树。

</details>

---

## 踩雷集锦

1. **「include 能编译，所以依赖合法」**：`llvm/CMakeLists.txt:1368` 把所有公开头路径加给所有目标。编译通过只证明文件存在，符号归属和依赖方向要靠 `LINK_COMPONENTS` 判断。
2. **「Analysis 是上层，谁都能用」**：方向要看边。`Analysis -> Core` 存在，所以 `Core -> Analysis` 非法；`CodeGen -> Analysis` 存在，所以 CodeGen 用 `ScalarEvolution` 合法。
3. **「`add_subdirectory` 顺序就是依赖顺序」**：`llvm/lib/CMakeLists.txt` 里 `Transforms` 在 `Analysis` 前，`Analysis` 在 `MC` 前；这些顺序不定义依赖。依赖只看目标属性和链接边。
4. **「有环也能配置，只要 `llvm-config` 不崩」**：`expand_topologically` 的 visited 集合只防遍历死循环，不消除 CMake 目标环。共享构建会在生成阶段拒绝。
5. **「把依赖加到最上层组件就能修」**：把 `Core` 加到 `Support`、把 `TransformUtils` 加到 `Analysis`，都是把反向边固化。修法是下移实现或反转调用，不是扩大 `LINK_COMPONENTS`。

## 自我检核

- [ ] 能从 `add_llvm_component_library` 的声明里读出目标名、组件名和 `LINK_COMPONENTS`
- [ ] 能不看笔记写出 `Support -> Core -> Analysis -> TransformUtils` 的方向
- [ ] 能解释 `CodeGen -> Analysis` 为什么合法，而 `Core -> Analysis` 为什么非法
- [ ] 能区分「include 能编译」与「组件依赖合法」
- [ ] 能指出 `AssumptionCache` 属于哪个组件，以及 `IR/Assumptions.cpp` 与它的职责差别
- [ ] 能画出题目 5 的三节点环，并说出每条边的 CMake 来源
- [ ] 能解释共享构建与静态构建为什么在不同阶段暴露环
- [ ] 能给出至少一种「下移实现」和一种「反转调用」的拆分方案
- [ ] 能在 review comment 里写出 `path:line` 级别的证据，而不是只说「分层不对」

## 延伸阅读

- **[LLVM CMake 文档：Building LLVM with CMake](https://llvm.org/docs/CMake.html)**
  - **读哪里**：`LLVM-related variables` 与 `Embedding LLVM in your project` 两节
  - **能学到什么**：`LLVM_ENABLE_PROJECTS`、`LLVM_TARGETS_TO_BUILD`、dylib 选项的官方语义，以及树外项目如何用 `llvm_map_components_to_libnames`
  - **前提**：知道 CMake 的 `add_library` 和 `target_link_libraries` 基本用法
- **[LLVM Coding Standards：Library Layering](https://llvm.org/docs/CodingStandards.html#library-layering)**
  - **读哪里**：整节，特别是「Unix linker 从左到右、不回头」和「inline 函数绕过检查」两段
  - **能学到什么**：为什么静态链接能碰巧通过，以及如何用「所有 inline 函数改成 out-of-line」做思想实验
  - **和本练习的关联**：题目 1、2、5 的失败方式都来自这里
- **[`llvm/cmake/modules/LLVM-Config.cmake`](https://github.com/llvm/llvm-project/blob/main/llvm/cmake/modules/LLVM-Config.cmake)**
  - **读哪里**：`llvm_map_components_to_libnames`（`:212`）和 `expand_topologically`（`:295`）
  - **能学到什么**：组件名怎样变成库名、链接顺序怎样由依赖图产生、visited 集合做了什么和不做什么
  - **前提**：读过本章的 `llvm-config --libs` 实验；不要求会写 CMake 函数
- **[LLVM `llvm-config` 文档](https://llvm.org/docs/CommandGuide/llvm-config.html)**
  - **读哪里**：`--components`、`--libs`、`--targets-built`、`--link-shared`
  - **能学到什么**：怎样在不读 CMake 文件的情况下快速核对一个构建的组件和链接模型
  - **和本练习的关联**：题目 1、4 都可以用这些命令交叉验证

把这份练习做完，你应该能在看到一条新依赖时先画出边，再决定它是不是环，而不是等构建替你发现。

→ [Ch 13 — New Pass Manager：`PassBuilder`、`AnalysisManager`、pipeline 文本](./13-new-pass-manager.md)
