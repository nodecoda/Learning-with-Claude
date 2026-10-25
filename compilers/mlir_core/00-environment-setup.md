# Ch 0 环境与最小闭环

> **目标**：装好 MLIR 23.1.4，跑通“解析 → 变换 → 降级 → LLVM IR”这条最小闭环，并建立后面 31 章要反复使用的观察习惯：**每改一步，就把 IR 打出来看**。

## 为什么需要这个？

学 MLIR 最常见的失败方式，是把它当成“另一套 LLVM IR 语法”来读：记住 `arith.addi`、`scf.for` 的写法，却不知道谁在什么时候把什么变成了什么。结果一遇到真实 pipeline，就只能猜。

这一章不介绍抽象概念，先让工具跑起来。你会看到四件事：

1. `mlir-opt` 如何解析并重新打印一段 IR；
2. 一个 pass（`canonicalize`）如何改变 IR；
3. 一串 conversion pass 如何把 `func`/`arith` 变成 `llvm` dialect；
4. `mlir-translate` 如何把 LLVM dialect 变成真正的 LLVM IR。

这四步就是本课的骨架。后面的章节，只是把这四步拆得更细、问得更深。

## 先建立直觉：`mlir-opt` 是 IR 的 REPL

把 `mlir-opt` 想成一个“可以插拔变换的 IR 解释器”：

```text
.mlir 文本 ──parse──▶ 内存中的 Operation 树 ──pass pipeline──▶ 新的 Operation 树 ──print──▶ .mlir 文本
```

它和 `opt` 的关系是“同构但不同层”：`opt` 处理 LLVM IR，`mlir-opt` 处理 MLIR；两者都支持 `--pass-pipeline`、`--mlir-print-ir-after-all` 这类观察手段。区别在于，MLIR 的 IR 可以同时容纳多个抽象层次（dialect），所以“这段 IR 属于哪一层”永远是你第一个要回答的问题。

一个关键设计后果：**MLIR 没有全局固定的 opcode 集合**。`arith.addi` 能存在，是因为 `arith` dialect 被注册并加载了；`mlir-opt` 启动时注册了一大批方言，所以你才能直接写它们。到 Ch 16 你会看到，自定义 dialect 必须自己解决“谁负责注册、谁负责加载”。

## 安装与版本钉定

本课使用 **MLIR 23.1.4**。官方 LLVM 的 Windows 预编译包**不包含** `mlir-opt`（这一点和 Linux 发行版打包习惯不同），所以本仓库用 conda-forge 的 `mlir` 包，版本与 LLVM 23.1.4 完全一致。

可复现的安装命令（已经在本机执行过）：

```powershell
$mm = ".scratch\micromamba\micromamba.exe"
$env:MAMBA_ROOT_PREFIX = (Resolve-Path ".scratch\micromamba").Path
& $mm create -y -p (Join-Path (Get-Location) ".scratch\mlir-env") -c conda-forge "mlir=23.1.4"
```

安装后工具在：

```text
.scratch\mlir-env\Library\bin\
```

验证：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" --version
```

实测输出：

```text
LLVM (http://llvm.org/):
  LLVM version 23.1.4
  Optimized build.
```

> `Optimized build` 意味着没有 assertions、没有 `-debug-only`、也没有 test dialect。遇到“为什么这个断言没触发”时，先想起这句话。

工具清单（本机实测存在）：

| 工具 | 用途 | 本课使用频率 |
|---|---|---|
| `mlir-opt` | 解析、跑 pass、打印 IR | 每章 |
| `mlir-translate` | MLIR ↔ LLVM IR 等格式转换 | Ch 21–22 |
| `mlir-tblgen` | 从 `.td` 生成 op/type 的 C++ 代码 | Ch 11–16 |
| `mlir-query` | 在 IR 上做结构化查询 | 选读 |
| `mlir-reduce` | 最小化触发问题的 IR | Ch 10 提及 |
| `mlir-lsp-server` | 编辑器支持 | 选读 |
| `mlir-runner` | 执行 LLVM dialect（JIT） | Ch 22 选读 |

## 示例一：最小模块与“身份变换”

新建 `hello.mlir`：

```mlir
func.func @add(%a: i32, %b: i32) -> i32 {
  %0 = arith.addi %a, %b : i32
  return %0 : i32
}
```

直接跑：

```powershell
& "$mlir\mlir-opt.exe" hello.mlir
```

实测输出：

```mlir
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    return %0 : i32
  }
}
```

注意三件事：

1. **顶层被包进 `module`**：`.mlir` 文件的顶层必须是 module，`builtin.module` 是隐式的。
2. **参数名被改名**：`%a`/`%b` 变成 `%arg0`/`%arg1`。SSA 名字不是语义的一部分，parser 只保证编号合法。
3. **`%0` 保留**：MLIR 打印时尽量保留可读名，但不保证跨 pass 稳定。不要写依赖具体 `%n` 的测试。

这一步没有任何 pass 运行，本质上只验证了“parse + verify + print”。

## 示例二：一个 pass 如何改变 IR

`canonicalize` 是 MLIR 里最常用的 pass：常量折叠、死代码消除、简单的代数化简，都在这里发生。

```mlir
func.func @fold() -> i32 {
  %0 = arith.constant 20 : i32
  %1 = arith.constant 22 : i32
  %2 = arith.addi %0, %1 : i32
  return %2 : i32
}
```

```powershell
& "$mlir\mlir-opt.exe" fold.mlir --canonicalize
```

实测输出：

```mlir
module {
  func.func @fold() -> i32 {
    %c42_i32 = arith.constant 42 : i32
    return %c42_i32 : i32
  }
}
```

这里有三个值得记住的细节：

- **折叠发生在 `arith.addi` 的 fold hook 里**，不是 canonicalizer 硬编码了加法。Ch 7 会打开这个机制。
- **打印名变成了 `%c42_i32`**：这是 MLIR 对常量 value 的命名约定，说明 value 的“身份”变了。
- **`return` 仍然保留**：canonicalize 不会做控制流重组，那是别的 pass 的职责。

## 示例三：降级到 LLVM dialect，再变成 LLVM IR

先看 MLIR 内部的降级。下面这条 pipeline 做三件事：

```powershell
& "$mlir\mlir-opt.exe" hello.mlir `
  --pass-pipeline='builtin.module(convert-func-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)'
```

实测输出：

```mlir
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
}
```

再交给 `mlir-translate`：

```powershell
& "$mlir\mlir-opt.exe" hello.mlir `
  --pass-pipeline='builtin.module(convert-func-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)' `
  | & "$mlir\mlir-translate.exe" --mlir-to-llvmir
```

实测输出：

```llvm
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

define i32 @add(i32 %0, i32 %1) {
  %3 = add i32 %0, %1
  ret i32 %3
}

!llvm.module.flags = !{!0}

!0 = !{i32 2, !"Debug Info Version", i32 3}
```

这条链路是本课 Part 3 的全部内容，只是这里用了最简单的两个 conversion。注意：

- `llvm.func` / `llvm.add` / `llvm.return` 仍然是 **MLIR**，只是用了 `llvm` dialect 表达 LLVM 语义。
- `mlir-translate` 才是真正跨出 MLIR 的一步；它输出的是 LLVM IR，可以交给 `llc`。
- 最后的 `define i32 @add...` 里，参数名和 SSA 编号由 LLVM IR 打印器决定，和 MLIR 的 `%0` 无关。

## 示例四：看 pass 之间的 IR

这是本课最重要的观察手段。加 `--mlir-print-ir-after-all`：

```powershell
& "$mlir\mlir-opt.exe" hello.mlir `
  --pass-pipeline='builtin.module(canonicalize,convert-func-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)' `
  --mlir-print-ir-after-all
```

实测输出（节选，注释为后加）：

```text
// -----// IR Dump After CanonicalizerPass: canonicalize{...} //----- //
module {
  func.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    return %0 : i32
  }
}
// -----// IR Dump After ConvertFuncToLLVMPass: convert-func-to-llvm{...} //----- //
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32   // 注意：arith 还在
    llvm.return %0 : i32
  }
}
// -----// IR Dump After ArithToLLVMConversionPass: convert-arith-to-llvm{...} //----- //
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
}
```

中间那一步非常关键：`convert-func-to-llvm` 之后，函数签名已经是 `llvm.func`，但函数体里还留着 `arith.addi`。**conversion 是分 dialect 逐步完成的，不是一次性整体替换**。Part 3 会反复回到这个观察。

> PowerShell 注意：这些 dump 走的是 **stderr**，所以 `| Out-File` 抓不到。要合并：`2>&1 | Out-File dump.txt`。

## 两类必须认识的错误

### 错误一：类型不匹配

```mlir
func.func @bad(%a: i32) -> i64 {
  return %a : i64
}
```

实测输出：

```text
bad.mlir:2:10: error: use of value '%a' expects different type than prior uses: 'i64' vs 'i32'
  return %a : i64
         ^
bad.mlir:1:16: note: prior use here
func.func @bad(%a: i32) -> i64 {
```

MLIR 的诊断会同时给 **use 点**和 **prior use 点**。读错误时先看 note，它通常直接指出“这个值最早是按什么类型定义的”。Ch 10 会系统讲 diagnostics。

### 错误二：`mlir-translate` 不认识你的方言

只跑 `convert-func-to-llvm`、不跑 `convert-arith-to-llvm`，然后直接 translate：

```powershell
& "$mlir\mlir-opt.exe" hello.mlir --pass-pipeline='builtin.module(convert-func-to-llvm)' `
  | & "$mlir\mlir-translate.exe" --mlir-to-llvmir
```

实测输出：

```text
<stdin>:3:10: error: Dialect `arith' not found for custom op 'arith.addi'
    %0 = arith.addi %arg0, %arg1 : i32
         ^
<stdin>:3:10: note: Available dialects: acc, arm_neon, arm_sme, arm_sve, builtin, dlti, func, gpu, llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm
```

这条错误很容易误判成“arith dialect 没注册”，其实是 **`mlir-translate` 的进程里只注册了它需要的少数方言**。`mlir-opt` 注册得多，不代表下游工具也注册得多。Ch 16 会把“谁注册、谁加载”讲清楚。

## 底层机制：一次 `mlir-opt` 调用发生了什么

把上面的现象串起来，一次调用大致是：

```text
1. 解析命令行，构建 PassPipeline（字符串 → PassManager）
2. 创建 MLIRContext，注册并加载 dialect
3. Parser 读 .mlir：构造 Operation 树，解析类型/属性/region
4. 运行 verifier（每个 op 的 verify() + 结构约束）
5. 按 pipeline 顺序运行 pass；每个 pass 前后默认 verify
6. 打印最终 IR（或按 --mlir-print-ir-after-all 打印中间态）
```

有三个点值得现在记住：

- **verify 不是可选的调试功能**，它是 pass 之间的契约检查。`--verify-each` 默认开启；关掉它通常只会让 bug 更晚暴露。
- **PassPipeline 是字符串解析出来的**，所以嵌套语法 `builtin.module(...)` 有意义：它表示“在 module 这一层运行这些 pass”。
- **MLIRContext 拥有类型/属性的 uniquing storage**，所以 `i32` 在内存里只有一份。Ch 30 会讲这对性能和生命周期的影响。

## 对比：`mlir-opt` vs `opt` vs `llc`

| 维度 | `mlir-opt` | `opt` | `llc` |
|---|---|---|---|
| 输入 | MLIR（多方言） | LLVM IR | LLVM IR |
| 抽象层 | 可同时含高层与低层 | 固定为 LLVM IR | 固定为 LLVM IR |
| 典型变换 | dialect conversion、canonicalize、bufferize | 优化 pipeline | 指令选择、寄存器分配 |
| 本课位置 | 主工具 | Ch 22 对照 | Ch 22 收尾 |

一句话：**`mlir-opt` 负责“把高层 IR 逐步降到 LLVM 能接受的样子”，`llc` 负责“把 LLVM IR 变成机器码”**。两者的接缝就是 Ch 22 的主题。

## C++ 工具链：实测可用的组合

本课的 Part 2–3 需要编译 out-of-tree dialect/pass。这台机器上其实有 **两套** MSVC，结果完全不同：

| 安装 | 版本 | 结果 |
|---|---|---|
| `C:\Program Files (x86)\Microsoft Visual Studio\2017\BuildTools` | MSVC 14.16 | ❌ 头文件直接拒绝 |
| `D:\VStudio\2022\Enterprise` | MSVC 14.43 | ✅ 编译、链接、运行都通过 |

VS2017 的失败信息：

```text
llvm/Support/Compiler.h(99): fatal error C1189: #error:  LLVM requires at least VS 2019.
```

VS2022 Enterprise 不在默认路径，所以要用完整路径调 `vcvars64.bat`。下面这个最小程序已经在本机实测跑通：

```cpp
// test.cpp
#include "mlir/IR/MLIRContext.h"
#include "mlir/IR/BuiltinDialect.h"
#include "llvm/Support/raw_ostream.h"

int main() {
  mlir::MLIRContext ctx;
  ctx.loadDialect<mlir::BuiltinDialect>();
  llvm::outs() << "mlir-cpp-ok dialects=" << ctx.getLoadedDialects().size() << "\n";
  return 0;
}
```

构建脚本（`build.bat`，路径按本机写死）：

```bat
@echo off
call "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "D:\Learning-with-Claude\.scratch\mlir-build-test"
cl /nologo /utf-8 /std:c++17 /EHsc /MD ^
   /I"D:\Learning-with-Claude\.scratch\mlir-env\Library\include" ^
   test.cpp ^
   /link ^
   /LIBPATH:"D:\Learning-with-Claude\.scratch\mlir-env\Library\lib" ^
   /LIBPATH:"D:\VStudio\2022\Enterprise\VC\Tools\MSVC\14.43.34808\lib\x64" ^
   /LIBPATH:"C:\Program Files (x86)\Windows Kits\10\lib\10.0.17763.0\um\x64" ^
   /LIBPATH:"C:\Program Files (x86)\Windows Kits\10\lib\10.0.17763.0\ucrt\x64" ^
   MLIRIR.lib MLIRSupport.lib LLVMSupport.lib LLVMDemangle.lib ntdll.lib ^
   /OUT:test.exe
```

实测输出：

```text
mlir-cpp-ok dialects=1
```

三个必须记住的坑（都是本机实测踩出来的）：

1. **Windows SDK 10.0.26100 的 `um\x64` 缺 `shell32.lib`**。`vcvars64` 默认指向它，链接会报 `LNK1104: 无法打开文件 shell32.lib`。解决办法是把 10.0.17763 的 lib 目录也加进 `/LIBPATH`（本机实测可行）。
2. **`LLVMSupport.lib` 需要 `ntdll.lib`**，否则 `__imp_RtlGetLastNtStatus` 未解析。`vcvars64` 不会自动带上它。
3. **源码和头文件里有非 ASCII 字符，代码页 936 会刷 C4819 警告**。加 `/utf-8` 即可，不要靠改系统区域设置。

### 推荐的工程方式：CMake

真实项目不会手写 `cl` 命令行。`mlir-env` 自带 `MLIRConfig.cmake`，out-of-tree 项目用标准 CMake 即可。本机实测可用的最小 `CMakeLists.txt`：

```cmake
cmake_minimum_required(VERSION 3.20)
project(mlir_smoke CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(MLIR REQUIRED CONFIG)
add_executable(mlir_smoke test.cpp)
target_link_libraries(mlir_smoke PRIVATE MLIRIR MLIRSupport ntdll.lib)
target_include_directories(mlir_smoke PRIVATE ${MLIR_INCLUDE_DIRS})
```

配置与构建（实测通过）：

```bat
@echo off
call "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d "D:\Learning-with-Claude\.scratch\mlir-cmake-test"
cmake -G Ninja -B build ^
  -DMLIR_DIR="D:\Learning-with-Claude\.scratch\mlir-env\Library\lib\cmake\mlir" ^
  -DCMAKE_PREFIX_PATH="D:\Learning-with-Claude\.scratch\mlir-env\Library" ^
  -DCMAKE_BUILD_TYPE=Release ^
  "-DCMAKE_EXE_LINKER_FLAGS=/LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/um/x64 /LIBPATH:C:/PROGRA~2/WI3CF2~1/10/lib/10.0.17763.0/ucrt/x64"
cmake --build build
```

实测输出（关键行）：

```text
-- Found zstd: .../Library/lib/zstd.lib
-- Build files have been written to: .../build
[1/2] Building CXX object CMakeFiles\mlir_smoke.dir\test.cpp.obj
[2/2] Linking CXX executable mlir_smoke.exe
mlir-cpp-ok dialects=1
```

三个额外细节：

- **`CMAKE_PREFIX_PATH` 必须指向 conda 的 `Library`**，否则 `find_package(ZLIB)` 失败，`LLVMExports.cmake` 会报 `ZLIB::ZLIB` target 不存在。本机还需要 `micromamba install zlib` 才有 `zlib.lib` 和 `zlib.h`（`libzlib` 只带 DLL）。
- **`CMAKE_CXX_STANDARD 17` 不能省**，否则 LLVM 头文件里的 `std::optional`、inline variable 全部报错。
- **`LibXml2` 找不到不是致命错误**；本机实测配置继续通过，构建成功。

结论：

- **IR 级实验**：`mlir-opt` / `mlir-translate`，全部可跑。
- **`.td` → `.inc`**：`mlir-tblgen` 可跑，生成物可以逐行读（Ch 13）。
- **编译并链接 out-of-tree dialect/pass**：用 VS2022 + 上面的 `/LIBPATH` 组合，可跑；后续章节统一用 CMake（`MLIR_DIR=.scratch\mlir-env\Library\lib\cmake\mlir`）。
- **`-debug-only` / assertions**：conda 包是 Optimized build，仍不可用；这部分标 `guided`。

> 如果你的机器路径不同，只改 `vcvars64.bat`、`mlir-env` 和 SDK 版本号这三处；不要把 VS2017 的 `cl` 混进来。
## 踩雷集锦

1. **以为 `mlir-opt -O2` 存在。** MLIR 没有 `-O0/-O2` 这种统一优化级别；优化由具体 pipeline 决定。`mlir-opt --help` 里没有 `-O` 选项。要什么变换，就显式写什么 pass。

2. **把 `--mlir-print-ir-after-all` 写成 `-print-after-all`。** 后者是 LLVM `opt` 的拼写；MLIR 的前缀是 `mlir-`。写错不会报错，只会静默不打印——然后你以为 pass 没运行。

3. **用 `| Out-File` 抓 dump，结果文件是空的。** MLIR 的 IR dump 走 stderr。必须 `2>&1`。

4. **看到 `Dialect 'arith' not found` 就以为要加 `--allow-unregistered-dialect`。** 那个 flag 是给“故意写未知方言”的场景用的；这里的根因是 `mlir-translate` 进程没有注册 `arith`。正确做法是把 arith 降级掉，而不是放行未知方言。

5. **以为 `%0`/`%1` 的名字有语义。** MLIR 的 SSA 名只是打印用的。测试里锁 `%0` 会在换一个 pass 顺序后随机失败。要锁的是 op、类型和操作数关系。

6. **在 Optimized build 上找 `-debug-only=...`。** 本机包是 Optimized build，没有 debug 日志。需要时用 `--mlir-print-ir-after-all` 和 `--mlir-print-op-generic` 代替；真正的 `-debug-only` 要自建 assertions 版本。

## 进阶：再往深一层

- **看 generic 形式**：加 `--mlir-print-op-generic`，你会看到 `"arith.addi"(%arg0, %arg1) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32`。这是 op 的真实结构（含属性字典），custom 语法只是糖。Ch 2 会用它解释 Operation 的通用模型。
- **看 op 的属性**：`--mlir-print-debuginfo`、`--mlir-print-local-scope` 会改变打印内容；理解“打印器可配置”有助于读源码里的测试。
- **试 `mlir-opt --help` 里的 `--split-input-file`**：一个文件里用 `// -----` 分隔多个测试，是上游测试的标准写法（Ch 10）。
- **试 `mlir-query`**：对 IR 做结构查询，比 grep 文本可靠。

## 动手练习

1. **跑通最小闭环**：把 `hello.mlir` 保存到本地，依次执行本章四条命令，确认输出与教材一致。
2. **制造一个错误**：把 `arith.addi` 的两个操作数改成不同类型（`i32` 与 `i64`），记录完整错误信息，指出 use 点与 prior use 点。
3. **只降一半**：只跑 `convert-arith-to-llvm`、不跑 `convert-func-to-llvm`，观察 `mlir-translate` 的报错，并解释为什么和示例四的错误不同。
4. **观察 pass 顺序**：交换 `convert-func-to-llvm` 与 `convert-arith-to-llvm` 的顺序，看 IR dump 有什么变化。提示：函数签名与函数体的转换是分开的。
5. **写一个 `fold.mlir` 变体**：把常量换成函数参数，确认 `canonicalize` 不再折叠，并解释原因。

## 本章重点整理

- MLIR 23.1.4 的 `mlir-opt` / `mlir-translate` / `mlir-tblgen` 已在本机可用；官方 LLVM Windows 包不含 `mlir-opt`。
- 一次调用 = parse → verify → pass pipeline → verify → print。
- `canonicalize` 的折叠来自 op 的 fold hook，不是 pass 硬编码。
- conversion 是 **按 dialect 逐步** 的：`llvm.func` 里可以暂时残留 `arith.addi`。
- `mlir-translate` 只注册少量方言；残留高层 op 会直接报 “Dialect not found”。
- IR dump 走 stderr；PowerShell 要 `2>&1`。
- 本机 C++ 链路可用：VS2022 Enterprise（MSVC 14.43）+ SDK 10.0.17763 的 lib + `ntdll.lib`；VS2017 不可用。

## 自我检核

- [ ] 我能不看 notes，写出 `mlir-opt` 跑单文件 + 单 pass 的命令
- [ ] 我能解释为什么 `canonicalize` 把两条常量 + 一条 add 变成一条常量
- [ ] 我能说出 `convert-func-to-llvm` 之后为什么还会残留 `arith.addi`
- [ ] 我能解释 `mlir-translate` 报 “Dialect `arith' not found” 的真正原因
- [ ] 我知道 IR dump 走 stderr，并知道怎么在 PowerShell 里保存
- [ ] 我能说出本机为什么不能链接自定义 dialect，以及缺什么
- [ ] 我能解释 `%arg0` 和 `%a` 只是打印差异，不是两个不同的值

## 延伸阅读

- **[MLIR 文档首页](https://mlir.llvm.org/docs/)**
  - **读哪里**：首页的 “MLIR: A New Approach to IR” 与 Getting Started。
  - **学什么**：MLIR 的设计动机与工具链全貌。
  - **和本章的关联**：本章只用了工具，这里补“为什么需要这些工具”。

- **[Toy Tutorial: Chapter 1](https://mlir.llvm.org/docs/Tutorials/Toy/Ch-1/)**
  - **读哪里**：Toy 语言与目标 IR。
  - **学什么**：一个真实前端如何映射到 MLIR dialect。
  - **和本章的关联**：Final Project 的 mini frontend 参照它，但规模更小。

- **[MLIR FAQ](https://mlir.llvm.org/getting_started/Faq/)**
  - **读哪里**：“What's up with dialects management?” 一节。
  - **学什么**：dialect 注册、加载、dependent dialects 的规则。
  - **和本章的关联**：解释示例四里 `mlir-translate` 的 “Dialect not found”。

- **[mlir/include/mlir/IR/Operation.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Operation.h)**
  - **读哪里**：`Operation` 的成员与 `getRegions()`、`getOperands()`。
  - **学什么**：IR 在内存里的真实结构。
  - **和本章的关联**：Ch 2 的对象模型从这里开始。

- **[mlir/lib/Transforms/Canonicalizer.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Transforms/Canonicalizer.cpp)**
  - **读哪里**：greedy driver 的主循环。
  - **学什么**：canonicalize 如何反复应用 pattern 直到收敛。
  - **和本章的关联**：Ch 7 会逐段读这个文件。

---

下一章先不碰 pass，回到最基本的问题：MLIR 到底是一套什么东西？为什么它要在 LLVM IR 之上再造一层 IR，而不是直接扩展 LLVM IR？

→ [Ch 1 MLIR 是什么：多级 IR 与 dialect 组合](./01-what-is-mlir.md)