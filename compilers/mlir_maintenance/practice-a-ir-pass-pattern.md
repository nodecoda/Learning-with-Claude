# 练习 A — 写一个 pass 与 pattern

> **目标**：把 Ch 04–08 的不变量拼成一个**真的能跑**的 out-of-tree 项目：一个 `OperationPass<func::FuncOp>` + 两个 `RewritePattern`，用 CMake 编译成 `.so`，用 `mlir-opt -load-pass-plugin` 加载，用 `FileCheck` 做回归。验收标准不是“代码看起来对”，而是四条命令都能在你机器上复现。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `e44fff5decc7`），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，`S=/home/ubuntu/llvm/trees/ct-instr-trunk`。项目建在 `/tmp/mlir-practice-a`，不写回源码树。

> **前置**：Ch 04–08；知道 `OpRewritePattern` 的 `matchAndRewrite` 签名；能读 CMake。

## A.1 任务与验收

写一个 pass，把两类恒等运算折掉：

```text
arith.addi %x, 0   ->  %x
arith.muli %x, 1   ->  %x
```

要求：

1. pass 注册名 `practice-a-fold-identity`，可被 `--pass-pipeline` 调用。
2. 非恒等常量（如 `addi %x, 7`）必须保留。
3. 至少一个 `FileCheck` 测试，含正向与负向断言。
4. 插件能通过 `-load-pass-plugin` 加载；不加载时调用该 pass 必须报“未注册”。
5. 用 `clang++ -fsyntax-only` 对 build 的 MLIR 头做一次编译检查。

验收命令（后面每一条都有真实输出）：

```bash
clang++ -fsyntax-only ...                     # 语法检查
cmake -S . -B build2 -DMLIR_DIR=...           # 配置
cmake --build build2 -j4                      # 编译 .so
mlir-opt test.mlir -load-pass-plugin=... --pass-pipeline=... | FileCheck test.mlir
```

## A.2 先建立直觉：插件工具链

```text
MyPass.cpp ──clang++/CMake──▶ libPracticeAPlugin.so
                                   │
                                   │ 导出 mlirGetPassPluginInfo()
                                   ▼
mlir-opt -load-pass-plugin=libPracticeAPlugin.so
   │  调用 plugin info 回调 -> PassRegistration<PracticeAPass>()
   │  解析 --pass-pipeline='builtin.module(func.func(practice-a-fold-identity))'
   ▼
IR ──▶ PracticeAPass::runOnOperation()
          └─ RewritePatternSet{FoldIdentity<AddIOp>, FoldIdentity<MulIOp>}
             └─ applyPatternsGreedily(...)
   ▼
stdout ──▶ FileCheck
```

三个边界：

- **编译期**：头文件路径、`-fno-rtti`、生成物 `.inc` 目录。
- **加载期**：必须导出 `mlirGetPassPluginInfo`，且 ABI 与 `mlir-opt` 一致。
- **运行期**：pass 的锚点 op 必须与 pipeline 匹配，pattern 必须通过 rewriter 改 IR。

## A.3 目录与文件

```text
/tmp/mlir-practice-a/
├── CMakeLists.txt
├── MyPass.cpp
├── practice_a.mlir
└── build2/                 # cmake 生成
    └── libPracticeAPlugin.so
```

```bash
mkdir -p /tmp/mlir-practice-a && cd /tmp/mlir-practice-a
```

## A.4 Pass.cpp 全文

下面这份代码是本章实测通过的版本（原样可编译）。先看整体，再逐块解释。

```cpp
//===- MyPass.cpp - practice A out-of-tree pass -----------------*- C++ -*-===//
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Tools/Plugins/PassPlugin.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
#include "llvm/Config/llvm-config.h"
#include "llvm/Support/Compiler.h"

using namespace mlir;

namespace {
static bool isZero(arith::ConstantOp c) {
  auto ia = dyn_cast<IntegerAttr>(c.getValue());
  return ia && ia.getValue().isZero();
}
static bool isOne(arith::ConstantOp c) {
  auto ia = dyn_cast<IntegerAttr>(c.getValue());
  return ia && ia.getValue().isOne();
}

/// Rewrite `addi %x, 0` -> `%x` and `muli %x, 1` -> `%x`.
template <typename OpT, bool (*Pred)(arith::ConstantOp)>
struct FoldIdentity : public OpRewritePattern<OpT> {
  using OpRewritePattern<OpT>::OpRewritePattern;
  LogicalResult matchAndRewrite(OpT op,
                                PatternRewriter &rewriter) const override {
    auto cst = op.getRhs().template getDefiningOp<arith::ConstantOp>();
    if (!cst || !Pred(cst))
      return failure();
    rewriter.replaceOp(op, op.getLhs());
    return success();
  }
};

struct PracticeAPass
    : public PassWrapper<PracticeAPass, OperationPass<func::FuncOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PracticeAPass)

  StringRef getArgument() const final { return "practice-a-fold-identity"; }
  StringRef getDescription() const final {
    return "Fold addi x,0 and muli x,1 (practice A)";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect>();
  }
  void runOnOperation() override {
    RewritePatternSet patterns(&getContext());
    patterns.add<FoldIdentity<arith::AddIOp, isZero>>(&getContext());
    patterns.add<FoldIdentity<arith::MulIOp, isOne>>(&getContext());
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns),
                                     GreedyRewriteConfig().enableFolding())))
      signalPassFailure();
  }
};
} // namespace

extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo mlirGetPassPluginInfo() {
  return {MLIR_PLUGIN_API_VERSION, "PracticeAPlugin", LLVM_VERSION_STRING,
          []() { PassRegistration<PracticeAPass>(); }};
}
```

### A.4.1 pattern 部分

`FoldIdentity` 是模板：`OpT` 决定匹配 `AddIOp` 还是 `MulIOp`，`Pred` 决定“右边的常量是不是 identity”。

```cpp
auto cst = op.getRhs().template getDefiningOp<arith::ConstantOp>();
if (!cst || !Pred(cst))
  return failure();
rewriter.replaceOp(op, op.getLhs());
```

三个细节都对应前面的章节：

- `getDefiningOp<arith::ConstantOp>()` 返回 null 时直接 `failure()`——不要把“不是常量”当成错误。
- `template` 关键字是必需的：`op` 的类型依赖模板参数 `OpT`。
- `replaceOp` 而不是 `erase`：它先断 use 再删（Ch 08）。

### A.4.2 pass 部分

```cpp
struct PracticeAPass
    : public PassWrapper<PracticeAPass, OperationPass<func::FuncOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(PracticeAPass)
  StringRef getArgument() const final { return "practice-a-fold-identity"; }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect>();
  }
  ...
};
```

- `OperationPass<func::FuncOp>` 决定锚点：pipeline 必须写 `func.func(...)`。
- `getArgument()` 是命令行名字，必须全局唯一。
- `getDependentDialects` 声明本 pass 会创建 `arith` 的 op；少了它，某些驱动下会报“dialect 未加载”。
- `MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID` 用于匿名 namespace 内的 pass（`TypeID.h:81` 的推荐用法）。

### A.4.3 插件入口

```cpp
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo mlirGetPassPluginInfo() {
  return {MLIR_PLUGIN_API_VERSION, "PracticeAPlugin", LLVM_VERSION_STRING,
          []() { PassRegistration<PracticeAPass>(); }};
}
```

`-load-pass-plugin` 找的就是这个符号。返回值里版本号必须与 `mlir-opt` 匹配，否则加载会失败。

<details>
<summary>参考解答：如果你只想先跑通，完整文件就是 A.4 这一段</summary>

把 A.4 的代码原样保存为 `MyPass.cpp` 即可。不要改 `applyPatternsGreedily` 的拼写——本 trunk 已删除 `applyPatternsAndFoldGreedily`（见 A.10 排错 3）。

最小验证顺序：

```bash
clang++ -std=c++17 -fno-rtti -fsyntax-only MyPass.cpp \
  -I$S/mlir/include -I$T/tools/mlir/include -I$S/llvm/include -I$T/include
```

退出码 0 且无输出，就说明头文件与 API 都对。

</details>

## A.5 CMakeLists.txt 全文

```cmake
cmake_minimum_required(VERSION 3.20)
# MLIR's LLVMConfig runs C-language compiler checks; enable C as well as CXX.
project(mlir-practice-a LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(MLIR REQUIRED CONFIG)
list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(AddLLVM)
include(AddMLIR)
include(HandleLLVMOptions)

add_mlir_library(PracticeAPlugin SHARED MyPass.cpp
  LINK_LIBS PUBLIC
    MLIRArithDialect
    MLIRFuncDialect
    MLIRIR
    MLIRPass
    MLIRSupport
    MLIRTransforms
)

# This is an in-tree build of MLIR, so the source headers are not installed;
# point the compiler at the source tree as well as the generated .inc tree.
target_include_directories(PracticeAPlugin PRIVATE
  /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include
  /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/include
  /home/ubuntu/llvm/builds/ct-instr-trunk/tools/mlir/include
  /home/ubuntu/llvm/builds/ct-instr-trunk/include)
```

三个“为什么”：

| 写法 | 原因 |
|---|---|
| `LANGUAGES C CXX` | `LLVMConfig.cmake` 会跑 C 编译器检查；只开 CXX 会 configure 失败 |
| `add_mlir_library(... SHARED ...)` | 插件必须是动态库；`MODULE` 也可，但 `SHARED` 最省事 |
| 额外 `target_include_directories` | 本机是**构建树**而非安装树，源码头与 `.inc` 不在默认 include path |

## A.6 编译：三条路径

### A.6.1 语法检查（最快）

```bash
S=/home/ubuntu/llvm/trees/ct-instr-trunk
T=/home/ubuntu/llvm/builds/ct-instr-trunk
clang++ -std=c++17 -fno-rtti -fsyntax-only MyPass.cpp \
  -I$S/mlir/include -I$T/tools/mlir/include -I$S/llvm/include -I$T/include
echo "SYNTAX_EXIT=$?"
```

真实输出：

```text
SYNTAX_EXIT=0
```

无输出就是成功。`-fsyntax-only` 不链接，所以能验证 API 签名与头文件，但验证不了符号可见性。

### A.6.2 直接编 .so（不写 CMake）

```bash
clang++ -std=c++17 -fno-rtti -fPIC -shared MyPass.cpp \
  -I$S/mlir/include -I$T/tools/mlir/include -I$S/llvm/include -I$T/include \
  -L$T/lib -lMLIRIR -lMLIRPass -lMLIRTransforms -lMLIRFuncDialect \
  -lMLIRArithDialect -lMLIRSupport -lLLVMSupport \
  -Wl,-rpath,$T/lib -o MyPass.so
ls -l MyPass.so
```

真实输出：

```text
-rwxrwxr-x 1 ubuntu ubuntu 399728 Nov  8 19:16 MyPass.so
```

`-fno-rtti` 不能省：MLIR 用 `-fno-rtti` 构建，插件带 RTTI 会引用不存在的 `typeinfo`（见 A.10 排错 1）。

### A.6.3 CMake 配置与构建

```bash
cmake -S . -B build2 -DMLIR_DIR=$T/lib/cmake/mlir \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++
```

真实输出（节选）：

```text
-- The C compiler identification is Clang 14.0.0
-- Could NOT find LibEdit (missing: LibEdit_INCLUDE_DIRS LibEdit_LIBRARIES)
-- Configuring done (3.0s)
-- Generating done (0.0s)
-- Build files have been written to: /tmp/mlir-practice-a/build2
```

```bash
cmake --build build2 -j4
```

真实输出：

```text
[ 50%] Building CXX object CMakeFiles/PracticeAPlugin.dir/MyPass.cpp.o
[100%] Linking CXX shared library libPracticeAPlugin.so
[100%] Built target PracticeAPlugin
```

`Could NOT find LibEdit` 是警告，不影响插件。

## A.7 加载与运行

先准备输入：

```bash
cat > test.mlir <<'EOF'
func.func @f(%x: i32) -> i32 {
  %z = arith.constant 0 : i32
  %o = arith.constant 1 : i32
  %a = arith.addi %x, %z : i32
  %m = arith.muli %a, %o : i32
  return %m : i32
}
EOF
```

运行：

```bash
$T/bin/mlir-opt test.mlir -load-pass-plugin=build2/libPracticeAPlugin.so \
  --pass-pipeline='builtin.module(func.func(practice-a-fold-identity))'
```

真实输出：

```text
module {
  func.func @f(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}
```

两层折叠都发生：`addi %x, 0 -> %x`，再 `muli %x, 1 -> %x`。

### A.7.1 确认 pass 真的注册了

```bash
$T/bin/mlir-opt test.mlir -load-pass-plugin=build2/libPracticeAPlugin.so --help | grep -A1 'practice-a'
```

真实输出：

```text
      --practice-a-fold-identity                             -   Fold addi x,0 and muli x,1 (practice A)
```

### A.7.2 不加载插件时必须失败

```bash
$T/bin/mlir-opt test.mlir --pass-pipeline='builtin.module(func.func(practice-a-fold-identity))'
```

真实输出：

```text
<unknown>:0: error: MLIR Textual PassPipeline Parser:1:11: error: 'practice-a-fold-identity' does not refer to a registered pass or pass pipeline
func.func(practice-a-fold-identity)
          ^
```

这条负向验证很重要：它证明 pass 来自插件，而不是恰好被 in-tree 注册。

## A.8 写 FileCheck 测试

`practice_a.mlir`：

```mlir
// RUN: mlir-opt %s -load-pass-plugin=%plugin --pass-pipeline='builtin.module(func.func(practice-a-fold-identity))' | FileCheck %s

// CHECK-LABEL: func.func @fold_add_zero
// CHECK-SAME: (%[[ARG:.*]]: i32) -> i32
// CHECK-NEXT: return %[[ARG]] : i32
func.func @fold_add_zero(%x: i32) -> i32 {
  %z = arith.constant 0 : i32
  %a = arith.addi %x, %z : i32
  return %a : i32
}

// CHECK-LABEL: func.func @keep_nonzero
// CHECK: arith.addi
func.func @keep_nonzero(%x: i32) -> i32 {
  %z = arith.constant 7 : i32
  %a = arith.addi %x, %z : i32
  return %a : i32
}
```

直接跑（用 `%plugin` 占位替换成真实路径）：

```bash
$T/bin/mlir-opt practice_a.mlir -load-pass-plugin=build2/libPracticeAPlugin.so \
  --pass-pipeline='builtin.module(func.func(practice-a-fold-identity))' \
  | $T/bin/FileCheck practice_a.mlir
echo "FILECHECK_EXIT=$?"
```

真实输出：

```text
FILECHECK_EXIT=0
```

三个写法要点：

1. **`CHECK-LABEL` 划分函数边界**，避免跨函数误匹配。
2. **`%[[ARG:.*]]` 捕获变量**，`CHECK-NEXT: return %[[ARG]]` 断言“原样返回同一个值”。
3. **负向断言用 `CHECK: arith.addi`**：`keep_nonzero` 必须保留 `addi`。

<details>
<summary>为什么不用 <code>CHECK-NOT: arith.addi</code> 做全局负向断言？</summary>

`CHECK-NOT` 的作用域是“上一个 CHECK 到下一个 CHECK 之间”，不是整个文件。如果直接写：

```text
CHECK-NOT: arith.addi
```

它只保证“在第一个匹配点之前没有 addi”，后面的内容不受约束。正确做法是把负向断言放在 `CHECK-LABEL` 之后、下一个 `CHECK` 之前，或像本例这样用正向断言锁定“必须还有 addi”。范围语义是 FileCheck 最常见的误用。

</details>

## A.9 与现有 test pass 做行为对照

在写自己的 pass 之前，先用 in-tree 的 `-canonicalize` 确认“这个折叠本来就是 canonicalization 的一部分”：

```bash
$T/bin/mlir-opt test.mlir -canonicalize
```

真实输出：

```text
module {
  func.func @f(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}
```

结论：你的 pass 与 canonicalizer 在这一类输入上**行为等价**。这不是坏事，反而是写测试时的基线：如果 `-canonicalize` 已经折掉，你的 pattern 就必须在“canonicalizer 不跑”的场景下仍有价值（比如在转换 pipeline 中间、或需要保留特定 op 时）。

再对照 Ch 08 的 `eraseOperands`：结构性删除（如 `linalg.generic` 的 `ins` 与 region block argument）必须同步删 operand 与 block arg，而你这份 pass 只做 SSA 值替换，不需要那套逻辑。

## A.10 排错手册

<details>
<summary>排错 1：<code>undefined symbol: _ZTIN4mlir4PassE</code></summary>

真实错误：

```text
Failed to load passes from './MyPass.so'. Request ignored.
Expected<T> must be checked before access or destruction.
Unchecked Expected<T> contained error:
Could not load library './MyPass.so': ./MyPass.so: undefined symbol: _ZTIN4mlir4PassE
```

`_ZTIN4mlir4PassE` 是 `typeinfo for mlir::Pass`。原因是编译插件时开了 RTTI，而 MLIR 与 `mlir-opt` 是 `-fno-rtti` 构建的。

修法：所有编译命令加 `-fno-rtti`。CMake 里 `HandleLLVMOptions` 通常会带进来；直接 `clang++` 时必须手写。

验证：

```bash
clang++ -std=c++17 -fno-rtti -fPIC -shared ... -o MyPass.so
$T/bin/mlir-opt test.mlir -load-pass-plugin=./MyPass.so --help | grep practice-a
```

</details>

<details>
<summary>排错 2：<code>fatal error: 'mlir/Dialect/Arith/IR/ArithOpsDialect.h.inc' file not found</code></summary>

真实错误：

```text
/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/Arith/IR/ArithDialect.h:12:10: fatal error: 'mlir/Dialect/Arith/IR/ArithOpsDialect.h.inc' file not found
```

`.inc` 是 TableGen 生成物，只在构建树里，不在源码树。需要同时给出：

```text
-I$S/mlir/include                     # 源码头
-I$S/llvm/include                     # LLVM 源码头
-I$T/tools/mlir/include               # MLIR 生成 .inc
-I$T/include                          # LLVM 生成头
```

CMake 版见 A.5 的 `target_include_directories`。

</details>

<details>
<summary>排错 3：<code>use of undeclared identifier 'applyPatternsAndFoldGreedily'</code></summary>

真实错误：

```text
oldapi.cpp:6:9: error: use of undeclared identifier 'applyPatternsAndFoldGreedily'; did you mean 'applyPatternsGreedily'?
```

旧 API 在本 trunk 已被删除：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git log -1 --format='%h %ad %s' --date=short 72f5050ae765
```

真实输出：

```text
72f5050ae765 2026-02-23 [mlir][NFC] Remove unused deprecated API wrappers (#182715)
```

修法：改用 `applyPatternsGreedily`；folding 默认已开（`GreedyRewriteConfig::fold = true`，`GreedyPatternRewriteDriver.h:173`）。

</details>

<details>
<summary>排错 4：<code>unable to schedule pass ... on a PassManager intended to run on 'builtin.module'</code></summary>

真实错误：

```text
<unknown>:0: error: unable to schedule pass '{anonymous}::ClonePass' on a PassManager intended to run on 'builtin.module'!
```

原因：这个 pass 只能运行在函数级 op 上（`ClonePass` 是 `InterfacePass<FunctionOpInterface>`，`TestClone.cpp:28`），但你把它直接挂在 `builtin.module`。修法：

```bash
# 错
--pass-pipeline='builtin.module(practice-a-fold-identity)'
# 对
--pass-pipeline='builtin.module(func.func(practice-a-fold-identity))'
```

推广：`OperationPass<X>` / `InterfacePass<I>` 必须出现在匹配的 op 之内（本例是 `func.func`）；只有 module 级 pass 才能直接挂顶层。

</details>

## A.11 验收清单

- [ ] `clang++ -fsyntax-only` 退出码 0、无输出
- [ ] CMake configure 成功，能生成 `libPracticeAPlugin.so`
- [ ] `-load-pass-plugin` 后 `--help` 能看到 `practice-a-fold-identity`
- [ ] 不加载插件时，pipeline 解析报“does not refer to a registered pass”
- [ ] `test.mlir` 被折叠成 `return %arg0`
- [ ] `practice_a.mlir` 的 `FileCheck` 退出码 0
- [ ] `keep_nonzero` 保留 `arith.addi`
- [ ] 能解释为什么必须 `-fno-rtti`、为什么需要 `$T/tools/mlir/include`

## A.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/examples/standalone/standalone-plugin/standalone-plugin.cpp`**
  - 读哪里：整个文件（约 40 行）
  - 能学到什么：`mlirGetPassPluginInfo` 的官方最小形状
  - 和本章关联：A.4.3 的插件入口
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Tools/Plugins/PassPlugin.h`**
  - 读哪里：`PassPluginLibraryInfo` 与 `MLIR_PLUGIN_API_VERSION`
  - 能学到什么：加载器如何校验版本与符号
  - 和本章关联：A.7 的加载失败排查
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Transforms/Utils/GreedyPatternRewriteDriver.cpp`**
  - 读哪里：`simplify`（1228）、`notifyOperationReplaced`（1146）
  - 能学到什么：pattern 被重复应用的时机与收敛判定
  - 和本章关联：为什么两层折叠能在一次 pass 里完成
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/IR/TestBytecodeRoundtrip.cpp`**
  - 读哪里：`getArgument`/`getDescription`/`runOnOperation` 的写法
  - 能学到什么：测试 pass 的注册与选项声明
  - 和本章关联：对照你自己 pass 的骨架
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/PassManagement.md`**
  - 读哪里：`Pass Infrastructure`、`Pass Pipeline Registration`
  - 能学到什么：`OperationPass<T>` 的锚点规则与 pipeline 文本语法
  - 和本章关联：A.10 排错 4

练习 A 把 Part 1 的不变量落成了一个可加载的插件。下一章进入 Part 2：PassManager 如何调度这些 pass、analysis 何时失效、以及 `--pass-pipeline` 的完整语法。

→ 下一章：[Ch 09 PassManager 与 analysis](./09-pass-manager-and-analysis.md)
