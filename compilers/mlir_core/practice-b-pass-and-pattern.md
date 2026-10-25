# 练习 B：写一个 pass 与 pattern

> **目标**：从零搭一个 out-of-tree 工程，写一个 `func.func` 级 pass，用 `RewritePattern` 把 `arith.addi x, 0`（以及 `0 + x`）化简成 `x`，再用 `--canonicalize` 清掉死常量。你会亲手跑通 CMake 配置、编译、链接、注册 pass、MLIR 测试，并用一个对照实验证明“确实是 pattern 在改 IR，不是 folding 顺手做的”。

## 任务规格

写一个独立的 `addzero-opt` 工具，满足：

1. 可执行文件名 `addzero-opt`，基于 `MlirOptMain`，支持 `--split-input-file`、`--canonicalize` 等标准选项；
2. 注册一个 pass `--add-zero`，作用域是 `func.func`；
3. pass 内用 `OpRewritePattern<arith::AddIOp>` 匹配 `addi(x, 0)` 和 `addi(0, x)`，用 `rewriter.replaceOp` 替换成非零操作数；
4. 关闭 greedy driver 的 folding，确保改写来自我们自己的 pattern；
5. 用至少 3 个测试用例覆盖：右零、左零、非零；再加一个 `index` 类型用例；
6. 构建产物能在本机 VS2022 + MLIR 23.1.4 上跑通。

验收命令：

```powershell
.\build2\addzero-opt.exe .\test\add-zero.mlir --split-input-file --add-zero
.\build2\addzero-opt.exe .\test\add-zero.mlir --split-input-file --add-zero --canonicalize
```

## 为什么需要这个？

Ch 5–10 讲的都是“读别人的 pass”。真正写一个 out-of-tree pass，你才会遇到这些现实问题：

- CMake 怎么找到 MLIR 的 imported targets；
- pass 怎么注册，为什么 `--add-zero` 会“unknown pass”；
- `RewritePattern` 怎么拿到 operands，`replaceOp` 和 `eraseOp` 的差别；
- greedy driver 默认会 folding，为什么你的 pattern 看起来“没被调用”；
- Windows 上 MSVC、SDK、Ninja、TEMP 路径的组合坑。

这个练习把这些一次性串起来。它很小，但结构和大项目一样：库 + 工具 + 测试。

## 先建立直觉：一个 out-of-tree pass 的四件套

```text
CMakeLists.txt        → 找到 MLIR，声明库和可执行文件
include/AddZero/*.h   → 对外暴露 createAddZeroPass()
lib/AddZero.cpp       → Pass + RewritePattern 的实现
tools/addzero-opt.cpp → 注册 dialect/pass，调用 MlirOptMain
test/*.mlir           → MLIR 用例
```

四个部分各司其职：

- **库**（`AddZeroPass`）只依赖 MLIR 的 IR/Pass/Transforms，不依赖 `mlir-opt` 的 main；
- **工具**（`addzero-opt`）负责注册方言和 pass，把命令行交给 `MlirOptMain`；
- **测试**用 `--split-input-file` 放在一个文件里，方便一次跑完；
- **CMake** 用 `add_mlir_library` + `add_executable`，链接 `MLIROptLib`。

## 环境与工程结构

环境沿用 Ch 0：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$cmake = "D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
$ninja = "D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe"
```

工程结构：

```text
practice-b/
├── CMakeLists.txt
├── include/AddZero/AddZero.h
├── lib/AddZero.cpp
├── tools/addzero-opt.cpp
└── test/add-zero.mlir
```

> 不要在工程根目录放无关的 `.cpp`。LLVM 的 `llvm_check_source_file_list` 会检查“目录里的源文件是否都属于某个 target”，多一个 `hello.cpp` 就会配置失败。这个坑本章实测踩过。

## 第一步：CMakeLists

完整文件：

```cmake
cmake_minimum_required(VERSION 3.20)
project(addzero LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(MLIR REQUIRED CONFIG)
list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(TableGen)
include(AddLLVM)
include(AddMLIR)

include_directories(${MLIR_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

add_mlir_library(AddZeroPass
  lib/AddZero.cpp

  ADDITIONAL_HEADER_DIRS
  ${CMAKE_CURRENT_SOURCE_DIR}/include/AddZero

  LINK_LIBS PUBLIC
  MLIRArithDialect
  MLIRFuncDialect
  MLIRIR
  MLIRPass
  MLIRSupport
  MLIRTransforms
  )

add_executable(addzero-opt tools/addzero-opt.cpp)
target_link_libraries(addzero-opt PRIVATE AddZeroPass MLIROptLib)
```

逐段解释：

- `find_package(MLIR REQUIRED CONFIG)` 走 `MLIRConfig.cmake`；本机路径是 `.scratch\mlir-env\Library\lib\cmake\mlir`。
- `LLVM_CMAKE_DIR` 必须加进 module path，否则 `add_mlir_library` 里会报 `Unknown CMake command "llvm_add_library"`。
- `include(AddLLVM)` 提供 `llvm_add_library`，`include(AddMLIR)` 提供 `add_mlir_library`。
- `MLIRTransforms` 是 `applyPatternsGreedily` 的来源；`MLIRArithDialect` / `MLIRFuncDialect` 提供 op 定义。
- `MLIROptLib` 提供 `MlirOptMain`，省得自己写 parser 和 pass pipeline。

## 第二步：完整 C++

### 头文件 `include/AddZero/AddZero.h`

```cpp
#ifndef ADDZERO_ADDZERO_H
#define ADDZERO_ADDZERO_H

#include <memory>

namespace mlir {
class Pass;

std::unique_ptr<Pass> createAddZeroPass();
} // namespace mlir

#endif // ADDZERO_ADDZERO_H
```

### Pass 与 Pattern `lib/AddZero.cpp`

```cpp
#include "AddZero/AddZero.h"

#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"

namespace mlir {
namespace {

/// Rewrite `addi(x, 0) -> x` and `addi(0, x) -> x`.
struct AddZeroPattern : public OpRewritePattern<arith::AddIOp> {
  using OpRewritePattern::OpRewritePattern;

  LogicalResult matchAndRewrite(arith::AddIOp op,
                                PatternRewriter &rewriter) const override {
    if (matchPattern(op.getRhs(), m_Zero())) {
      rewriter.replaceOp(op, op.getLhs());
      return success();
    }
    if (matchPattern(op.getLhs(), m_Zero())) {
      rewriter.replaceOp(op, op.getRhs());
      return success();
    }
    return failure();
  }
};

class AddZeroPass
    : public PassWrapper<AddZeroPass, OperationPass<func::FuncOp>> {
public:
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(AddZeroPass)

  StringRef getArgument() const final { return "add-zero"; }
  StringRef getDescription() const final {
    return "Rewrite arith.addi x, 0 to x with a custom pattern";
  }

  void runOnOperation() override {
    RewritePatternSet patterns(&getContext());
    patterns.add<AddZeroPattern>(&getContext());

    GreedyRewriteConfig config;
    // Turn folding off so that the rewrite can only come from our pattern.
    config.enableFolding(false);
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns),
                                     config)))
      signalPassFailure();
  }
};

} // namespace

std::unique_ptr<Pass> createAddZeroPass() {
  return std::make_unique<AddZeroPass>();
}

} // namespace mlir
```

几个关键点：

- `OpRewritePattern<arith::AddIOp>` 的 `matchAndRewrite` 收到的是强类型 op，不需要自己 `dyn_cast`。
- `m_Zero()` 匹配整数/浮点/`index` 的零常量；它比手写 `IntegerAttr` 判断更稳。
- `rewriter.replaceOp(op, op.getLhs())` 内部做 RAUW + erase；不要自己先 `erase`。
- `GreedyRewriteConfig::enableFolding(false)` 是关键。MLIR 的 `arith.addi` **本身就有 `x + 0 -> x` 的 fold**（`ArithOps.cpp:419-423`），如果不关 folding，greedy driver 可能在你的 pattern 之前就把它折掉，你会误以为 pattern 没被调用。
- MLIR 23.1.4 的函数名是 `applyPatternsGreedily`，不是老版本的 `applyPatternsAndFoldGreedily`。本章实测踩过这个编译错误。

### 工具 `tools/addzero-opt.cpp`

```cpp
#include "AddZero/AddZero.h"

#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
#include "mlir/Transforms/Passes.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registry.insert<mlir::arith::ArithDialect, mlir::func::FuncDialect>();

  mlir::registerPass(
      []() -> std::unique_ptr<mlir::Pass> { return mlir::createAddZeroPass(); });
  mlir::registerCanonicalizerPass();

  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "addzero-opt: out-of-tree pass demo\n", registry));
}
```

- `registry.insert` 注册你需要的方言。只注册 `arith` 和 `func` 就够；测试里没有 `cf`。
- `registerPass` 把 `createAddZeroPass` 挂到 `--add-zero`。
- `registerCanonicalizerPass` 让 `--canonicalize` 可用；不注册的话，命令行会报 unknown pass。
- `MlirOptMain` 负责 parse、pass pipeline、print、`--split-input-file` 等所有标准行为。

## 第三步：构建（实测）

在 `cmd` 里先 `vcvars64.bat`，再跑 CMake。实测可用的 `build.bat`：

```bat
@echo off
set TEMP=D:\Learning-with-Claude\.scratch\tmp
set TMP=D:\Learning-with-Claude\.scratch\tmp
call "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "D:\Learning-with-Claude\.scratch\practice-b"
"D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" -G Ninja -B build2 ^
  -DMLIR_DIR="D:\Learning-with-Claude\.scratch\mlir-env\Library\lib\cmake\mlir" ^
  -DCMAKE_PREFIX_PATH="D:\Learning-with-Claude\.scratch\mlir-env\Library" ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_MAKE_PROGRAM="D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja\ninja.exe" ^
  "-DCMAKE_EXE_LINKER_FLAGS=/LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/um/x64 /LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/ucrt/x64"
if errorlevel 1 exit /b 1
"D:\VStudio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe" --build build2
```

实测配置输出（关键行）：

```text
-- Found ZLIB: D:/Learning-with-Claude/.scratch/mlir-env/Library/lib/z.lib (found version "1.3.2")
-- Found zstd: D:/Learning-with-Claude/.scratch/mlir-env/Library/lib/zstd.lib
-- Could NOT find LibXml2 (missing: LIBXML2_LIBRARY LIBXML2_INCLUDE_DIR)
-- Configuring done
-- Generating done
-- Build files have been written to: D:/Learning-with-Claude/.scratch/practice-b/build2
[1/4] Building CXX object CMakeFiles/obj.AddZeroPass.dir/lib/AddZero.cpp.obj
[2/4] Linking CXX static library AddZeroPass.lib
[3/4] Building CXX object CMakeFiles/addzero-opt.dir/tools/addzero-opt.cpp.obj
[4/4] Linking CXX executable addzero-opt.exe
```

`LibXml2` 找不到不是致命错误；本机实测配置继续通过。`C4819` 警告来自 MLIR 头文件的非 ASCII 字符，也不影响构建。

> **TEMP 的坑（实测）**：本机默认 `TEMP=E:\WindowsTemp`，`cl` 会报 `C1069: 无法读取编译器命令行`，链接会报 `LNK1108: 无法在 0x0 处写入文件`。把 `TEMP`/`TMP` 指到一个普通目录（例如工程下的 `.scratch\tmp`）即可。这个坑和 MLIR 无关，但在 Windows 上很常见。

## 第四步：MLIR 测试（实测）

`test/add-zero.mlir`：

```mlir
func.func @right_zero(%x: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %0 = arith.addi %x, %c0 : i32
  return %0 : i32
}

// -----

func.func @left_zero(%x: i32) -> i32 {
  %c0 = arith.constant 0 : i32
  %0 = arith.addi %c0, %x : i32
  return %0 : i32
}

// -----

func.func @index_zero(%i: index) -> index {
  %c0 = arith.constant 0 : index
  %0 = arith.addi %i, %c0 : index
  return %0 : index
}

// -----

func.func @not_zero(%x: i32, %y: i32) -> i32 {
  %0 = arith.addi %x, %y : i32
  return %0 : i32
}
```

跑 `--add-zero`：

```powershell
& .\build2\addzero-opt.exe .\test\add-zero.mlir --split-input-file --add-zero
```

实测输出：

```mlir
module {
  func.func @right_zero(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}

// -----
module {
  func.func @left_zero(%arg0: i32) -> i32 {
    return %arg0 : i32
  }
}

// -----
module {
  func.func @index_zero(%arg0: index) -> index {
    return %arg0 : index
  }
}

// -----
module {
  func.func @not_zero(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    return %0 : i32
  }
}
```

再跑 `--add-zero --canonicalize`：

```powershell
& .\build2\addzero-opt.exe .\test\add-zero.mlir --split-input-file --add-zero --canonicalize
```

实测输出与上面一致：前三个函数都只剩 `return`，第四个保留 `addi`。`--canonicalize` 在这里主要起“清死常量、跑到不动点”的作用；我们的 pattern 已经把 `addi` 替换掉了。

## 第五步：对照实验：pattern 真的在做事吗

因为 `arith.addi` 自己有 `x + 0 -> x` 的 fold，你必须证明“改写来自 pattern，而不是 folding”。做法是复制一份工程，把 `patterns.add<AddZeroPattern>` 注释掉，重新构建，再跑同一个输入。

对照实验的 `AddZero.cpp` 关键行：

```cpp
RewritePatternSet patterns(&getContext());
// CONTROL EXPERIMENT: no pattern registered.
```

实测输出（截取前两个 chunk）：

```mlir
module {
  func.func @right_zero(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %0 = arith.addi %arg0, %c0_i32 : i32
    return %0 : i32
  }
}

// -----
module {
  func.func @left_zero(%arg0: i32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %0 = arith.addi %c0_i32, %arg0 : i32
    return %0 : i32
  }
}
```

`addi` 还在，`arith.constant` 也在。这证明 `enableFolding(false)` 生效了，且正式版本的改写确实来自 `AddZeroPattern`。

## 分段步骤（如果你要自己做）

1. **搭骨架**：建目录、写空的 `CMakeLists.txt`、头文件、`main`，先让 `addzero-opt --help` 跑起来。
2. **注册空 pass**：`--add-zero` 先只打印一行或什么都不做，确认注册成功。
3. **写 pattern**：先只匹配 `x + 0`，跑通一个用例；再加 `0 + x`。
4. **关 folding**：加 `config.enableFolding(false)`，用对照实验确认 pattern 真的被调用。
5. **补测试**：加 `index`、非零、`0 + x`，用 `--split-input-file` 一次跑完。
6. **接 canonicalize**：确认 `--add-zero --canonicalize` 的组合行为，理解 pass 顺序。

<details>
<summary>参考解答（CMake、pass 与工具）</summary>

`CMakeLists.txt`：

```cmake
cmake_minimum_required(VERSION 3.20)
project(addzero LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(MLIR REQUIRED CONFIG)
list(APPEND CMAKE_MODULE_PATH "${MLIR_CMAKE_DIR}")
list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")
include(TableGen)
include(AddLLVM)
include(AddMLIR)

include_directories(${MLIR_INCLUDE_DIRS})
include_directories(${CMAKE_CURRENT_SOURCE_DIR}/include)

add_mlir_library(AddZeroPass
  lib/AddZero.cpp

  ADDITIONAL_HEADER_DIRS
  ${CMAKE_CURRENT_SOURCE_DIR}/include/AddZero

  LINK_LIBS PUBLIC
  MLIRArithDialect
  MLIRFuncDialect
  MLIRIR
  MLIRPass
  MLIRSupport
  MLIRTransforms
  )

add_executable(addzero-opt tools/addzero-opt.cpp)
target_link_libraries(addzero-opt PRIVATE AddZeroPass MLIROptLib)
```

`include/AddZero/AddZero.h`：与上文第二步相同（4 行接口声明），这里不重复。

`lib/AddZero.cpp`：

```cpp
#include "AddZero/AddZero.h"

#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/IR/Matchers.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"

namespace mlir {
namespace {

struct AddZeroPattern : public OpRewritePattern<arith::AddIOp> {
  using OpRewritePattern::OpRewritePattern;

  LogicalResult matchAndRewrite(arith::AddIOp op,
                                PatternRewriter &rewriter) const override {
    if (matchPattern(op.getRhs(), m_Zero())) {
      rewriter.replaceOp(op, op.getLhs());
      return success();
    }
    if (matchPattern(op.getLhs(), m_Zero())) {
      rewriter.replaceOp(op, op.getRhs());
      return success();
    }
    return failure();
  }
};

class AddZeroPass
    : public PassWrapper<AddZeroPass, OperationPass<func::FuncOp>> {
public:
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(AddZeroPass)

  StringRef getArgument() const final { return "add-zero"; }
  StringRef getDescription() const final {
    return "Rewrite arith.addi x, 0 to x with a custom pattern";
  }

  void runOnOperation() override {
    RewritePatternSet patterns(&getContext());
    patterns.add<AddZeroPattern>(&getContext());

    GreedyRewriteConfig config;
    config.enableFolding(false);
    if (failed(applyPatternsGreedily(getOperation(), std::move(patterns),
                                     config)))
      signalPassFailure();
  }
};

} // namespace

std::unique_ptr<Pass> createAddZeroPass() {
  return std::make_unique<AddZeroPass>();
}

} // namespace mlir
```

`tools/addzero-opt.cpp`：

```cpp
#include "AddZero/AddZero.h"

#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Pass/PassRegistry.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
#include "mlir/Transforms/Passes.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registry.insert<mlir::arith::ArithDialect, mlir::func::FuncDialect>();

  mlir::registerPass(
      []() -> std::unique_ptr<mlir::Pass> { return mlir::createAddZeroPass(); });
  mlir::registerCanonicalizerPass();

  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "addzero-opt: out-of-tree pass demo\n", registry));
}
```

`test/add-zero.mlir` 就是本章第四步的四段用例。预期结果：前三个函数只剩 `return`，第四个保留 `arith.addi`。

</details>

## 常见卡点（5 条）

1. **`Unknown CMake command "llvm_add_library"`。**
   `find_package(MLIR)` 之后只加了 `MLIR_CMAKE_DIR`，没加 `LLVM_CMAKE_DIR`，也没 `include(AddLLVM)`。补上这两行；`add_mlir_library` 依赖 LLVM 的 CMake 函数。

2. **`applyPatternsAndFoldGreedily` 找不到。**
   MLIR 23.1.4 改名为 `applyPatternsGreedily`（`GreedyPatternRewriteDriver.h:204/234`）。旧教程和旧代码里的名字不能用；看头文件，不要凭记忆。

3. **`--add-zero` 报 unknown pass，或者 dialect 没注册。**
   `registerPass` 没调用、`registry.insert` 少了 `arith`/`func`，或者 `MLIROptLib` 没链接。先跑 `addzero-opt --help | Select-String add-zero` 确认注册。

4. **pattern 看起来没被调用。**
   `arith.addi` 自己有 `x + 0 -> x` 的 fold；greedy driver 默认 folding 开着，可能先折掉。用 `config.enableFolding(false)`，再用“注释掉 pattern”的对照实验确认。

5. **Windows 构建失败：`C1069` / `LNK1108` / `shell32.lib` / `ntdll.lib`。**
   先检查 `TEMP`/`TMP` 是否指向可写目录；再按 Ch 0 补 SDK 10.0.17763 的 `um\x64`、`ucrt\x64` 和 `ntdll.lib`。CMake 的 imported targets 通常会处理 `ntdll`，手工 `cl` 链接才需要显式加。VS2017 会被 LLVM 23 直接拒绝。

## 自我检核

- [ ] 我能不看 notes，写出 `add_mlir_library` + `add_executable` 的最小 CMake
- [ ] 我能解释 `MLIRTransforms` 和 `MLIROptLib` 各自提供什么
- [ ] 我能写出一个 `OpRewritePattern<arith::AddIOp>` 并正确 `replaceOp`
- [ ] 我知道为什么要 `enableFolding(false)`
- [ ] 我能用 `--split-input-file` 组织 4 个用例
- [ ] 我能用对照实验证明 pattern 真的在改 IR
- [ ] 我能说出 5 个常见卡点及其修复
- [ ] 我知道 `--add-zero` 和 `--canonicalize` 的顺序会影响观察结果

## 延伸阅读

- **[MLIR Standalone 示例](https://github.com/llvm/llvm-project/tree/llvmorg-23.1.4/mlir/examples/standalone)**
  - **读哪里**：CMakeLists、`tools/standalone-opt`、pass 注册。
  - **学什么**：官方 out-of-tree 工程的最小结构。
  - **和本章的关联**：本练习的工程布局与它同构。

- **[GreedyPatternRewriteDriver.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Transforms/GreedyPatternRewriteDriver.h)**
  - **读哪里**：`GreedyRewriteConfig`、`enableFolding`、`applyPatternsGreedily`。
  - **学什么**：driver 的收敛与 folding 行为。
  - **和本章的关联**：卡点 2 和 4 的根源。

- **[ArithOps.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Dialect/Arith/IR/ArithOps.cpp)**
  - **读哪里**：`AddIOp::fold`（约 419 行）。
  - **学什么**：为什么 `x + 0` 会被上游先折掉。
  - **和本章的关联**：对照实验的动机。

- **[Ch 0 环境与最小闭环](./00-environment-setup.md)**
  - **读哪里**：CMake 与 VS2022 的配方。
  - **学什么**：`MLIR_DIR`、`CMAKE_PREFIX_PATH`、SDK lib 路径。
  - **和本章的关联**：构建命令的直接来源。

---

练习 B 结束，下一步是定义自己的方言：从 ODS 写第一个 Op，把 pass、pattern、测试串成一条完整链路。

→ [Ch 11 ODS 入门：定义第一个 Op](./11-ods-basics.md)