# Ch 27 工程化：out-of-tree、CMake 与测试

> **目标**：把一个“能跑的 `.mlir` 示例”变成“别人能配置、能构建、能测试、能升级”的工程。学完本章，你能为 MLIR 23.1.4 搭一个 out-of-tree 项目，知道 `MLIR_DIR` 该指向哪里，能用 lit + FileCheck 组织回归测试，并能判断链接慢、二进制大、版本不匹配分别该从哪里下手。

## 为什么需要这个？

课程里的命令大多长这样：

```powershell
& "$mlir\mlir-opt.exe" .\x.mlir --canonicalize
```

学习时没问题，交付一个 pass、方言或独立工具时却会撞上另外几类错误：

```text
CMake 找不到 MLIRConfig.cmake
LNK1104: 无法打开文件 shell32.lib / gdi32.lib
LNK2019: 无法解析的外部符号 ...（一长串 mangled name）
lit: 找不到 FileCheck
error: 'foo.op' op 不存在（头文件和库不是同一版本）
```

它们分别属于**配置、Windows 工具链、链接、测试、ABI**五个边界。下面按这个顺序拆，每个边界都给可观察的证据。

## 先建立直觉：三层工程边界

```text
源码层：.td / .cpp / .h
   │  configure：find_package(MLIR) 找到 MLIRConfig.cmake
   ▼
构建层：MLIR imported targets + 你的库/工具
   │  link：静态库、Windows SDK、ntdll、zlib
   ▼
验证层：mlir-opt 跑 IR、lit 跑测试、CI 跑退出码
```

三层会互相伪装：配置失败看起来像“编译不过”，链接失败看起来像“API 用错”，测试失败看起来像“pass 逻辑错”。先把每层的输入输出固定下来，再谈优化。

## 核心概念一：工程骨架与 Windows 工具链

### 目录布局与最小 CMake

建议从这样的布局开始：

```text
ch27-demo/
├─ CMakeLists.txt
├─ lit.cfg.py
├─ include/Demo.h
├─ lib/Demo.cpp
├─ tools/ch27-demo.cpp
└─ test/
   ├─ fold.mlir
   ├─ split.mlir
   └─ diag.mlir
```

真实方言会在 `include/` 放 `.td` 和生成头，在 `lib/` 放 dialect/op/pass 实现；本章先用普通库把 CMake 链路跑通，TableGen 细节见 Ch 11–16。

本机实测可用的最小 `CMakeLists.txt`：

```cmake
cmake_minimum_required(VERSION 3.20)
project(ch27demo LANGUAGES CXX)

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

add_mlir_dialect_library(Ch27DemoLib
  lib/Demo.cpp
  LINK_LIBS PUBLIC
  MLIRIR
  MLIRSupport)

add_mlir_tool(ch27-demo tools/ch27-demo.cpp)
target_link_libraries(ch27-demo PRIVATE Ch27DemoLib)
```

逐行解释：

- `find_package(MLIR REQUIRED CONFIG)` 只认 config mode，也就是 `MLIRConfig.cmake`；它不会去猜源码树。
- `${MLIR_CMAKE_DIR}` 由 `MLIRConfig.cmake` 提供，`${LLVM_CMAKE_DIR}` 由它触发的 `find_package(LLVM)` 提供；加入 `CMAKE_MODULE_PATH` 后，`include(AddMLIR)` 才能找到 `AddMLIR.cmake`。
- `add_mlir_dialect_library` 是 `add_mlir_library(... DEPENDS mlir-headers)` 的薄包装，并登记到 `MLIR_DIALECT_LIBS`（`AddMLIR.cmake:678-681`）。
- `add_mlir_tool` 只是 `llvm_add_tool(MLIR ${ARGV})`（`AddMLIR.cmake:447-449`）；工具规则仍由 LLVM 的 CMake 体系决定。
- `CMAKE_CXX_STANDARD 17` 不能省；LLVM 23 的头文件大量使用 C++17，默认标准会在 `std::optional`、inline variable 上直接报错。

真实方言还要加 `add_mlir_dialect(...)`、`mlir_tablegen(...)`，并把生成物放进 `DEPENDS`。上游 `examples/standalone/lib/Standalone/CMakeLists.txt:1-18` 是完整样例。

### Windows 四个坑的增量清单

[Ch 0](./00-environment-setup.md) 已给过完整命令，这里只列“何时会再遇到”和“回看哪段”。不要整段复制：

| 症状 | 原因 | 处理 | 回看 |
|---|---|---|---|
| `LNK1104: 无法打开文件 shell32.lib` / `gdi32.lib` | `vcvars64` 默认的 SDK 10.0.26100 缺这些 lib | 把 10.0.17763 的 `um\x64`、`ucrt\x64` 加进 `/LIBPATH` | Ch 0「推荐的工程方式」 |
| `__imp_RtlGetLastNtStatus` 未解析 | `LLVMSupport.lib` 需要 `ntdll.lib`，`vcvars64` 不自动带 | 链接列表加 `ntdll.lib` | Ch 0 手写 `cl` 示例 |
| `find_package(ZLIB)` 失败 / `ZLIB::ZLIB` 不存在 | conda 的 `Library` 没进 `CMAKE_PREFIX_PATH`，或只装了 `libzlib` | 设 `CMAKE_PREFIX_PATH=...\Library`，确保 `zlib.lib`/`zlib.h` 存在 | Ch 0 构建说明 |
| 一堆 `std::optional` / inline variable 报错 | 没设 C++17 | `set(CMAKE_CXX_STANDARD 17)` | 本章 CMake 骨架 |

本章实测再补两条同属工具链边界的：

- **用 VS 自带的 CMake + Ninja，并设 `TEMP`/`TMP`**。本机用 PATH 里的 CMake 4.0 + venv 里的 Ninja 时，编译器探测报 `C1069: 无法读取编译器命令行`；换回 VS2022 自带的 CMake 3.30 + Ninja，并把 `TEMP`/`TMP` 指到短路径后配置成功。
- **`FileCheck.exe` 不在 `Library\bin`**。它在 `Library\libexec\llvm\FileCheck.exe`；`mlir-opt.exe` 在 `bin`。lit 配置要分别指向这两个目录。

## 核心概念二：测试策略，lit + FileCheck

MLIR 测试不是“跑一遍看输出”，而是把期望的 IR 形状写进测试文件：

```text
test.mlir
  RUN: mlir-opt %s --canonicalize | FileCheck %s
  CHECK: 期望的 IR 片段
        │
        ▼
lit 发现测试 → 执行 RUN 行 → FileCheck 比对 → 退出码
```

上游 `mlir/test/lit.cfg.py` 的关键结构（真实行号）：

- `config.test_format = lit.formats.ShTest(...)`（33 行）：用 lit 的 shell 测试格式。
- `config.suffixes = [".td", ".mlir", ...]`（36–48 行）：哪些文件会被当成测试。
- `tool_dirs = [config.mlir_tools_dir, config.llvm_tools_dir, config.lit_tools_dir]`（185 行）：去哪里找 `mlir-opt`、`FileCheck`、`not`。
- `llvm_config.add_tool_substitutions(tools, tool_dirs)`（329 行）：把工具名替换成真实路径。
- `FILECHECK_OPTS = "-enable-var-scope --allow-unused-prefixes=false"`（335 行）：变量作用域按 LABEL 隔离，未使用的 CHECK 前缀直接报错。

最小 `lit.cfg.py` 只需钉死工具路径和测试根目录：

```python
import os
import lit.formats

config.name = "ch27"
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = [".mlir"]
config.test_source_root = os.path.join(os.path.dirname(__file__), "test")
config.test_exec_root = os.path.join(config.test_source_root, "_build")

mlir_bin = r"D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
filecheck = r"D:\Learning-with-Claude\.scratch\mlir-env\Library\libexec\llvm\FileCheck.exe"
config.substitutions.append(("mlir-opt", '"%s"' % os.path.join(mlir_bin, "mlir-opt.exe")))
config.substitutions.append(("FileCheck", '"%s"' % filecheck))
config.environment["FILECHECK_OPTS"] = "-enable-var-scope --allow-unused-prefixes=false"
```

注意 `execute_external=False`。lit 23.1.4 已废弃 `True`，传 `True` 会报：

```text
ValueError: execute_external=True is deprected as of LLVM-23 ...
Please move to using the internal shell (execute_external=False).
```

三个测试覆盖三类断言。`fold.mlir` 验证正常路径：

```mlir
// RUN: mlir-opt %s --canonicalize | FileCheck %s
// CHECK-LABEL: func.func @fold
// CHECK:         %[[C42:.*]] = arith.constant 42 : i32
// CHECK-NOT:     arith.addi
// CHECK:         return %[[C42]] : i32
func.func @fold() -> i32 {
  %0 = arith.constant 20 : i32
  %1 = arith.constant 22 : i32
  %2 = arith.addi %0, %1 : i32
  return %2 : i32
}
```

`split.mlir` 用 `// -----` 分隔两个模块，配合 `--split-input-file`（下面是结构节选，payload 已省略）：

```mlir
// RUN: mlir-opt %s --split-input-file --canonicalize | FileCheck %s
// CHECK-LABEL: func.func @add
// CHECK:         arith.constant 3 : i32
// ...
// -----
// CHECK-LABEL: func.func @mul
// CHECK:         arith.constant 8 : i32
```

`diag.mlir` 用 `--verify-diagnostics` 断言错误和 note：

```mlir
// RUN: mlir-opt %s --verify-diagnostics
func.func @bad() -> i32 {
  // expected-note @below {{prior use here}}
  %c0 = arith.constant 0 : i64
  %c1 = arith.constant 1 : i32
  // expected-error @below {{use of value '%c0' expects different type than prior uses: 'i32' vs 'i64'}}
  %0 = arith.addi %c0, %c1 : i32
  return %0 : i32
}
```

`--split-input-file` 把多个独立模块放进一个文件；`--verify-diagnostics` 让 expected 指令成为断言，而不是靠退出码。这两个是 MLIR 测试的基本武器。

## 核心概念三：构建成本与版本边界

### compile-time、链接与体积

本机真实数字：

| 对象 | 大小/观察 |
|---|---|
| `Library\lib` 下所有 `.lib` | 约 1.6 GB |
| `Library\lib` 下 `MLIR*.lib` | 约 890.7 MB |
| `mlir-opt.exe` | 约 149.3 MB |
| Ch 9 的 `modify-demo.exe`（链接 MLIRIR/MLIRPass） | 约 3.2 MB |

为什么链接慢、产物大？

- **头文件重**：`Operation.h`、`OpDefinition.h`、各 dialect 生成头包含大量模板和 inline 代码，每个翻译单元都要重新解析。
- **op 数量多**：`mlir-opt` 注册几乎所有方言与 pass，静态链接会拉进大量目标文件；只链接用到的库时，体积会小很多（3.2 MB 对 149.3 MB）。
- **调试信息与静态归档**：Release + `/Zi` 仍生成 PDB；Windows `.lib` 对顺序敏感，缺一个间接依赖会在链接末期爆一堆未解析符号。

三个常见开关的取舍：

- **`LLVM_LINK_LLVM_DYLIB`**：这是**构建 LLVM 本身**的选项；MLIR 在 `mlir/CMakeLists.txt:178-179` 把它映射成 `MLIR_LINK_MLIR_DYLIB`，再由 `mlir_target_link_libraries` 决定链接 `MLIR` 还是逐个静态库（`AddMLIR.cmake:748-758`）。消费预编译静态包时，在自己的项目里设它没有魔法效果。
- **`BUILD_SHARED_LIBS`**：Linux 上常用，但上游 LLVM 在 Windows 直接拒绝：`llvm/CMakeLists.txt:990-991` 是 `FATAL_ERROR "BUILD_SHARED_LIBS options is not supported on Windows."`。别把 Linux 配方原样搬过来。
- **`MLIR_ENABLE_BINDINGS_PYTHON=OFF`**：关掉 Python 绑定，省掉 Python 开发头、nanobind 和额外目标。本机 conda 包本身是 `MLIR_ENABLE_BINDINGS_PYTHON "0"`（`MLIRConfig.cmake:22`），所以没有 Python API 可用。

实用策略：开发期用 Ninja + 只构建目标；CI 用干净构建验证依赖完整；发布期再考虑 LTO/裁剪方言。

### 版本与 ABI，必须严格对齐

MLIR 的 C++ API **没有稳定 ABI**。op 类、TypeID、storage、生成的 parser/printer 都跟具体版本绑定；头文件和库只要不是同一次构建，就可能“编译通过、链接失败”或“链接通过、运行时崩溃”。

`MLIRConfig.cmake:10-11` 写得很直接：

```cmake
set(LLVM_VERSION 23.1.4)
find_package(LLVM ${LLVM_VERSION} EXACT REQUIRED CONFIG)
```

也就是说，`MLIR_DIR` 指向的包会要求**精确匹配**的 LLVM。把 `LLVM_DIR` 指到另一个版本，configure 阶段就会失败。

还有一个误导人的细节：`MLIRConfigVersion.cmake:1` 把 `PACKAGE_VERSION` 设成空字符串，兼容性靠第 5–12 行的 `23.1` + patch 判断。于是 `find_package(MLIR 99.0 REQUIRED CONFIG)` 会报：

```text
Could not find a configuration file for package "MLIR" that is compatible
with requested version "99.0".

The following configuration files were considered but not accepted:

  .../MLIRConfig.cmake, version: unknown
```

“version: unknown” 不代表包坏了，而是这个 config 文件没有填 `PACKAGE_VERSION`。真正的版本约束看 `MLIRConfig.cmake` 里的 `LLVM_VERSION` 和安装目录。

版本不匹配分两级：

- **configure 级（可复现）**：`MLIR_DIR` 指错，报 “Could not find a package configuration file provided by MLIR”；或 `find_package(MLIR 99.0)` 报不兼容。
- **链接级（本章标 guided）**：头文件来自 A 版本、`.lib` 来自 B 版本时，通常是 `LNK2019` / `undefined reference`，符号名是一长串 mangled name；也可能运行时因对象布局不同而崩溃。本机只装了 23.1.4，无法安全复现第二套 ABI。

工程上的做法只有一条：**把 MLIR 版本当成工具链的一部分钉死**。`MLIR_DIR`、`LLVM_DIR`、编译器版本、CI 镜像一起版本化；升级时整体升级。

## 底层机制：CMake 如何找到 MLIR

```text
MLIR_DIR/MLIRConfig.cmake
  → 读取 MLIR_CMAKE_DIR、LLVM_CMAKE_DIR、MLIR_INCLUDE_DIRS
  → find_package(LLVM 23.1.4 EXACT CONFIG)
  → 导入 MLIRTargets.cmake 里的 imported targets
  → include(AddMLIR) 提供 add_mlir_dialect_library / add_mlir_tool
```

`add_mlir_dialect_library` 做两件事：登记 target 到 `MLIR_DIALECT_LIBS`，再转调 `add_mlir_library(... DEPENDS mlir-headers)`（`AddMLIR.cmake:678-681`）。`add_mlir_tool` 只是 `llvm_add_tool(MLIR ...)`（447–449 行）。所以“库建了但工具没建”时，先看目标是否在默认 ALL 里，再显式 `--target`。

## 示例一：真实 out-of-tree 构建

源码很小：

```text
include/Demo.h:  std::string demoName();
lib/Demo.cpp:    std::string ch27::demoName() { return "ch27-demo"; }
tools/ch27-demo.cpp:
  #include "Demo.h"
  #include <cstdio>
  int main() { std::printf("%s\n", ch27::demoName().c_str()); return 0; }
```

配置（本机实测；下面的 `cmake` 指 VS2022 自带的 CMake 3.30，不是 PATH 里的 4.0）：

```powershell
cmake -G Ninja -B build `
  -DMLIR_DIR="D:\Learning-with-Claude\.scratch\mlir-env\Library\lib\cmake\mlir" `
  -DCMAKE_PREFIX_PATH="D:\Learning-with-Claude\.scratch\mlir-env\Library" `
  -DCMAKE_BUILD_TYPE=Release `
  "-DCMAKE_EXE_LINKER_FLAGS=/LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/um/x64 /LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/ucrt/x64"
```

实测输出（节选）：

```text
-- The CXX compiler identification is MSVC 19.43.34809.0
-- Found ZLIB: D:/Learning-with-Claude/.scratch/mlir-env/Library/lib/z.lib (found version "1.3.2")
-- Configuring done (3.0s)
-- Generating done (0.1s)
```

构建并运行：

```powershell
cmake --build build --target ch27-demo
.\build\ch27-demo.exe
```

实测输出：

```text
[1/2] Building CXX object CMakeFiles\ch27-demo.dir\tools\ch27-demo.cpp.obj
[2/2] Linking CXX executable ch27-demo.exe
ch27-demo
```

一个观察：直接 `cmake --build build` 时，本机只构建了 `Ch27DemoLib.lib`，没有产出工具；加 `--target ch27-demo` 才构建可执行文件。不同 LLVM CMake 版本的 ALL 规则可能不同，但“库和工具分别构建、分别验证”是稳妥做法。

## 示例二：真实 lit 套件

把三个测试文件放进 `test/`，用前面的 `lit.cfg.py`，执行：

```powershell
lit -v .
```

实测输出：

```text
-- Testing: 3 tests, 3 workers --
PASS: ch27 :: fold.mlir (1 of 3)
PASS: ch27 :: diag.mlir (2 of 3)
PASS: ch27 :: split.mlir (3 of 3)

Testing Time: 0.43s

Total Discovered Tests: 3
  Passed: 3 (100.00%)
```

如果 `diag.mlir` 少写一个 `expected-note`，实测会失败并给出：

```text
error: unexpected note: prior use here
```

这说明 `--verify-diagnostics` 不是“宽松匹配”：所有 note/error 都要有对应的 expected 指令。CI 里就跑：

```powershell
cmake --build build --target ch27-demo
lit -v .
```

这两条命令本机跑过；GitHub Actions 的 YAML 未在本仓库验证，标 `guided`。CI 的关键不是 YAML 长什么样，而是配置、构建、测试三步都从干净目录开始，且 `MLIR_DIR` 固定到同一版本。

没有 lit 的环境，最小替代是 PowerShell 断言：

```powershell
$out = & "$mlir\mlir-opt.exe" .\test\fold.mlir --canonicalize 2>&1 | Out-String
if ($out -notmatch 'arith.constant 42 : i32') { throw 'expected 42' }
'PASS: found 42'
```

实测输出：

```text
PASS: found 42
```

它不如 FileCheck 精确，但足以在 CI 里挡住“pass 没跑/输出完全变了”的低级错误。

## 示例三：配置错误长什么样

### `MLIR_DIR` 指错

```text
CMake Error at CMakeLists.txt:7 (find_package):
  Could not find a package configuration file provided by "MLIR" with any of
  the following names:

    MLIRConfig.cmake
    mlir-config.cmake

  Add the installation prefix of "MLIR" to CMAKE_PREFIX_PATH or set
  "MLIR_DIR" to a directory containing one of the above files.
```

先确认 `MLIR_DIR` 指向的是**安装目录下的 `lib/cmake/mlir`**，不是源码树的 `mlir/`。

## 对比与取舍

| 选择 | 优点 | 代价 | 建议 |
|---|---|---|---|
| 预编译包（本机 conda） | 配置快，版本固定 | 不能改 MLIR 本身，ABI 受包限制 | 学习和业务工具首选 |
| 源码构建 MLIR | 可开 assertions、可改上游 | 构建时间长，磁盘占用大 | 需要调试 MLIR 内部时 |
| 静态链接 | 部署简单，无 DLL 依赖 | 链接慢，体积大 | Windows 默认 |
| 共享库 | 链接快，多工具共享 | Windows 上游不支持 `BUILD_SHARED_LIBS`，部署复杂 | Linux/服务端优先 |
| lit + FileCheck | 精确、可组合、上游同款 | 需要配工具路径 | 有回归价值的 pass 必上 |
| PowerShell 断言 | 零依赖，快 | 只能做粗粒度检查 | 临时验证/无 lit 环境 |

## 工程检查清单（10 条）

1. `MLIR_DIR` 指向 `...\Library\lib\cmake\mlir`，不是源码树。
2. `LLVM_DIR`/`CMAKE_PREFIX_PATH` 与 `MLIR_DIR` 来自同一套安装。
3. `CMAKE_CXX_STANDARD 17` 已设置。
4. Windows 链接器带 10.0.17763 的 `um\x64`、`ucrt\x64`。
5. 链接列表包含 `ntdll.lib`。
6. conda 环境里有 `zlib.lib`/`zlib.h`，`CMAKE_PREFIX_PATH` 指到 `Library`。
7. `mlir-opt.exe` 用绝对路径；`FileCheck.exe` 用 `libexec\llvm` 下的路径。
8. 每个 pass 至少有 1 个正向测试 + 1 个诊断/边界测试。
9. 测试里用 `CHECK-LABEL` 限定函数，避免跨函数误匹配。
10. CI 从干净 build 目录配置、构建、跑 lit，并固定 MLIR 版本。

## 踩雷集锦

1. **把 `MLIR_DIR` 指到源码树**。源码树没有 `MLIRConfig.cmake`；它要的是安装产物。
2. **以为 `find_package(MLIR 23 REQUIRED)` 能可靠卡版本**。`PACKAGE_VERSION` 是空的，报错会写 unknown；真正约束在 `MLIRConfig.cmake` 的 `LLVM_VERSION`。
3. **在 PATH 里找 `FileCheck`**。本机 `mlir-opt` 和 `FileCheck` 不在同一目录；lit 配置要分别写路径。
4. **用 `lit.formats.ShTest(execute_external=True)`**。lit 23.1.4 会直接抛 `ValueError`；用 `False`。
5. **只跑 `cmake --build build` 就以为工具建好了**。本机默认 ALL 只建了库，工具要 `--target ch27-demo`。
6. **在 Windows 上开 `BUILD_SHARED_LIBS`**。上游 LLVM 顶层 CMake 直接 fatal；别照搬 Linux 配方。
7. **头文件和库混版本**。编译能过、链接报 mangled symbol、运行时崩溃都可能；把 MLIR/LLVM/编译器作为一套工具链升级。

## 进阶：再往深一层

- **`check-*` target**：用 `add_lit_testsuite` 把测试挂到目标上，CI 只需 `cmake --build build --target check-ch27`；本机未配该 target，标 `guided`。
- **链接器**：Windows 上可试 `lld-link` 替代 `link.exe`；是否能缩短链接时间要以你的工程实测为准，前提是 SDK/lib 路径已经正确。
- **编译缓存**：可试 `sccache`/`ccache`；是否有效取决于缓存命中率，CI 上配合缓存 key（MLIR 版本 + 编译器版本）使用。
- **裁剪方言**：只链接用到的 `MLIR*Dialect`，不要无脑 `MLIRRegisterAllDialects`；这是缩小工具体积最直接的手段。

## 动手练习

### 练习 1：把最小工程跑起来

**任务**：按本章骨架创建 `ch27-demo`，配置、构建 `ch27-demo`，运行并输出 `ch27-demo`。
**测试用例**：A) `MLIR_DIR` 正确，configure 成功；B) 指向不存在的目录，期望 `MLIRConfig.cmake` 找不到；C) 去掉 `CMAKE_CXX_STANDARD 17`，记录编译错误第一行。
**步骤**：1) 先只建库，确认 `Ch27DemoLib.lib`；2) 再 `--target ch27-demo`；3) 运行 exe 检查输出与退出码。

<details>
<summary>参考解答</summary>
关键是 `find_package(MLIR REQUIRED CONFIG)` + `${MLIR_CMAKE_DIR}`/`${LLVM_CMAKE_DIR}` 进 `CMAKE_MODULE_PATH`，再加 `include(AddMLIR)`。`add_mlir_dialect_library` 建库，`add_mlir_tool` 建工具；工具要显式 `--target`。
</details>

**常见卡点**：只设 `MLIR_DIR` 不设 `CMAKE_PREFIX_PATH`，会在 `find_package(ZLIB)` 阶段失败。

### 练习 2：给工程加一个 lit 测试

**任务**：为本章的最小工程加一个 `fold.mlir`，用 `CHECK-LABEL` + `CHECK` 断言输出。
**测试用例**：A) 正常输入，期望常量折叠；B) 输入不匹配，期望输出不变；C) 非法输入，期望 `expected-error`。
**步骤**：1) 手跑 `mlir-opt`，把真实输出贴进测试文件；2) 用 `CHECK-LABEL` 限定函数；3) 跑 `lit -v .`，确认三个测试通过。

<details>
<summary>参考解答</summary>
正向测试用 `--canonicalize | FileCheck %s`；诊断测试用 `--verify-diagnostics`，每个 note/error 都要有 `expected-*`。不要凭记忆写 IR，先跑真实输出再写 CHECK。
</details>

**常见卡点**：`CHECK` 写得太松，pass 没跑也能过；至少加 `CHECK-NOT` 或 `CHECK-LABEL` 限定范围。

### 练习 3：做一个无 lit 的 CI 断言

**任务**：在没有 lit 的环境里，用 PowerShell 跑 `mlir-opt` 并断言输出包含关键 IR；故意改坏期望，确认脚本以非零退出。
**测试用例**：A) 期望 `arith.constant 42 : i32`，应通过；B) 期望 43，应失败；C) 输入文件不存在，应失败并打印清晰错误。
**步骤**：1) 用 `Out-String` 捕获 stdout+stderr；2) 用 `-notmatch` 断言，失败时 `throw`；3) 在 CI 里检查退出码。

<details>
<summary>参考解答</summary>
```powershell
$out = & "$mlir\mlir-opt.exe" .\test\fold.mlir --canonicalize 2>&1 | Out-String
if ($out -notmatch 'arith.constant 42 : i32') { throw 'expected 42' }
```
这是粗粒度断言，适合冒烟；有 lit 时仍应优先 FileCheck。
</details>

**常见卡点**：忘了 `2>&1`，诊断在 stderr，PowerShell 变量里只剩 stdout。

## 本章重点整理

- out-of-tree 的第一道门槛是 `find_package(MLIR REQUIRED CONFIG)`，`MLIR_DIR` 指向安装目录的 `lib/cmake/mlir`。
- `add_mlir_dialect_library` 是 `add_mlir_library` 的方言包装；`add_mlir_tool` 是 `llvm_add_tool` 的薄包装。
- Windows 的四个老坑仍然有效：SDK 10.0.17763、`ntdll.lib`、conda `zlib`、C++17。
- lit + FileCheck 是 MLIR 测试的标准形态；`split-input-file` 和 `verify-diagnostics` 是两个基本武器。
- 本机 lit/FileCheck 可用，但 `FileCheck` 在 `libexec\llvm`，不在 `bin`。
- 链接慢/体积大来自模板头、op 数量和静态归档；`LLVM_LINK_LLVM_DYLIB` 不是下游项目的万能开关。
- 版本必须严格对齐；`MLIRConfig.cmake` 要求精确匹配 LLVM 23.1.4。

## 自我检核

- [ ] 我能说出 `MLIR_DIR` 应该指向哪个目录
- [ ] 我能解释 `add_mlir_dialect_library` 与 `add_mlir_library` 的关系
- [ ] 我知道 Windows 上为什么要补 10.0.17763 的 lib 路径
- [ ] 我知道 `ntdll.lib` 和 `zlib` 分别解决什么问题
- [ ] 我能写出一个带 `CHECK-LABEL` 的 lit 测试
- [ ] 我知道 `--split-input-file` 与 `--verify-diagnostics` 的用途
- [ ] 我能解释为什么 `LLVM_LINK_LLVM_DYLIB` 不能直接加速我的 out-of-tree 项目
- [ ] 我知道版本不匹配时 configure 与 link 两级各长什么样

## 延伸阅读

- **`mlir/cmake/modules/AddMLIR.cmake`**
  - **读哪里**：`add_mlir_library`（328 行起）、`add_mlir_tool`（447–449 行）、`add_mlir_dialect_library`（678–681 行）、`mlir_target_link_libraries`（748–758 行）。
  - **学什么**：库、工具、dylib 重映射的真实实现。
  - **和本章的关联**：本章 CMake 骨架的每一行都能在这里找到落点。
- **`mlir/test/lit.cfg.py`**
  - **读哪里**：33 行的 `ShTest`、36–48 行的 suffixes、185 行的 `tool_dirs`、303–329 行的工具替换、335 行的 `FILECHECK_OPTS`。
  - **学什么**：上游如何组织测试发现、工具路径和 FileCheck 选项。
  - **和本章的关联**：我们的最小 `lit.cfg.py` 是它的简化版。
- **`mlir/examples/standalone/`**
  - **读哪里**：顶层 `CMakeLists.txt:8-28` 的 `find_package`/module path，`lib/Standalone/CMakeLists.txt:1-18` 的 `add_mlir_dialect_library`，`standalone-opt/CMakeLists.txt:1-13` 的工具链接。
  - **学什么**：上游推荐的 out-of-tree 目录组织与 CMake 写法。
  - **和本章的关联**：本章的最小工程是它的裁剪版；真实方言应参考它加 TableGen。

→ [Ch 28 读 MLIR 源码](./28-reading-source.md)
