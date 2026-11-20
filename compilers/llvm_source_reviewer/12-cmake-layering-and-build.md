# Ch 12 — 库分层、CMake 与 include 规则

> **目标**：看到一个改 `CMakeLists.txt` 或新增 `#include "llvm/..."` 的 PR 时，能判断它是否越层、会不会造出依赖环、该由哪个组件库提供符号，以及这个改动在静态构建、共享构建和 `libLLVM` 构建下分别会怎样失败。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`。本章用 7c7a 的源码核实宏、目标和 `LINK_COMPONENTS`；命令输出来自 `pr-lane` 构建。需要说明的是，该构建目录的缓存指向 `/home/ubuntu/llvm/trees/pr-lane`（`a5eaf194d8b6`），与 7c7a 不是同一个 commit。构建产物形态可信，组件清单和行号以 7c7a 为准。

## 为什么需要这个？

LLVM 的库分层不是编译器的强制约束。打开 `llvm/CMakeLists.txt:1368`，会看到所有目标共享同一组 include 路径：

```cmake
include_directories( ${LLVM_INCLUDE_DIR} ${LLVM_MAIN_INCLUDE_DIR})
```

这意味着 `llvm/lib/Support/ABIBreak.cpp` 里写 `#include "llvm/Analysis/ScalarEvolution.h"`，预处理器通常能找到文件并编译通过。真正的边界在链接阶段、在 `llvm-config --libs` 的展开结果里、在共享库的循环依赖检查里，以及 reviewer 的判断里。

一个 PR 可以同时做到「CI 绿」和「分层错」：静态链接器按命令行顺序解析，某些隐式依赖会碰巧通过；头文件里的 inline 函数更不会被链接器检查。LLVM 的 `CodingStandards.md:847` 把规则写得很清楚：

> A directory of header files (for example, `include/llvm/Foo`) defines a library (`Foo`). One library (both its headers and implementation) should only use things from the libraries listed in its dependencies.

本章要回答三个问题：

1. 这个目录属于哪个组件库，组件库在 CMake 里声明了哪些依赖？
2. 新增的 include 或符号引用是否顺着依赖方向？
3. 一旦加错，构建会以什么形式坏掉，最小修复是什么？

## 先建立直觉

先看依赖方向，不先看目录顺序：

```text
             X86CodeGen / AArch64CodeGen / RISCVCodeGen
                          │
                          ▼
 Passes ─────────────► CodeGen ────────► Target
   │                     │  │              │
   │                     │  └────► MC ─────┼──► Support
   │                     ▼                 │
   └────────────► Transforms (Scalar/IPO/Vectorize)
                          │
                          ▼
                   TransformUtils
                          │
                          ▼
                      Analysis
                          │
                          ▼
                      Core (IR)
                          │
                          ▼
                      Support
                          │
                          ▼
                      Demangle

 ADT：只有头文件，没有独立 CMake target；被上面的库直接 include
```

这张图有两个容易看反的地方：

- 箭头表示「依赖谁」，不是「谁在目录里排在前面」。`Analysis` 在 `llvm/lib/CMakeLists.txt:32` 才被 `add_subdirectory`，但它依赖的 `Core` 在 `:13` 就已经加入；目录顺序不是依赖顺序。
- `Target` 是抽象接口，`X86CodeGen` 这类后端在它上面。`CodeGen -> Target` 与 `X86CodeGen -> CodeGen` 同时存在，不构成环，因为 `LLVMTarget` 不反向依赖任何具体后端。

`ADT` 是特例。`llvm/include/llvm/ADT` 下没有 `CMakeLists.txt`，`llvm/lib/CMakeLists.txt` 也没有 `add_subdirectory(ADT)`。它没有自己的库，头文件被每个消费者直接编译进自己的目标。`ADT` 不能依赖 `Support` 的实现细节，否则会变成每个包含 `ADT` 的目标都隐式拖上一个库。

## 组件库模型

### `add_llvm_component_library`

LLVM 的库几乎都通过 `add_llvm_component_library` 声明。定义在 `llvm/cmake/modules/AddLLVM.cmake:1049`：

```cmake
function(add_llvm_component_library name)
  cmake_parse_arguments(ARG
    ""
    "COMPONENT_NAME;ADD_TO_COMPONENT"
    ""
    ${ARGN})
  add_llvm_library(${name} COMPONENT_LIB ${ARG_UNPARSED_ARGUMENTS})
  string(REGEX REPLACE "^LLVM" "" component_name ${name})
  set_property(TARGET ${name} PROPERTY LLVM_COMPONENT_NAME ${component_name})
  ...
endfunction()
```

它做三件事：

1. 把 `LLVMSupport` 这样的目标名去掉 `LLVM` 前缀，登记为组件名 `Support`。
2. 调用 `add_llvm_library(... COMPONENT_LIB ...)`，让这个目标成为可以进入 `libLLVM` 的组件库。
3. 如果传了 `LINK_COMPONENTS`，把它保存为目标的 `LLVM_LINK_COMPONENTS` 属性；`AddLLVM.cmake:1036` 是这条属性的落点。

`LINK_COMPONENTS` 有两个入口，名字相同但作用域不同：

- 作为 `add_llvm_component_library(... LINK_COMPONENTS ...)` 的参数，进入目标的 `LLVM_LINK_COMPONENTS` 属性。
- 作为变量 `LLVM_LINK_COMPONENTS`，在 `add_llvm_library` 里被追加（`AddLLVM.cmake:651`），供同一目录下的工具或库使用。

看真实的 `Support` 声明（`llvm/lib/Support/CMakeLists.txt:164`，依赖块在 `:361`）：

```cmake
add_llvm_component_library(LLVMSupport
  ...
  LINK_COMPONENTS
  Demangle
  )
```

`Support` 只依赖 `Demangle`。这不是「Support 很小所以随便写」，而是层次底座的硬约束：如果 `Support` 依赖 `Core`，而 `Core` 又依赖 `Support`，组件图就出现环。`Core` 的声明在 `llvm/lib/IR/CMakeLists.txt:6`，依赖块在 `:108`：

```cmake
  LINK_COMPONENTS
  BinaryFormat
  Demangle
  Option
  Remarks
  Support
  TargetParser
```

`Analysis` 在 `llvm/lib/Analysis/CMakeLists.txt:197` 依赖 `Core` 和 `Support`；`TransformUtils` 在 `llvm/lib/Transforms/Utils/CMakeLists.txt:108` 又依赖 `Analysis`。这条链是：

```text
TransformUtils -> Analysis -> Core -> Support -> Demangle
```

方向稳定，构建才能按依赖顺序展开。

### `llvm_map_components_to_libnames`

树外项目不直接读目标的 `LLVM_LINK_COMPONENTS` 属性，而是调用 `llvm_map_components_to_libnames`。定义在 `llvm/cmake/modules/LLVM-Config.cmake:212`，核心工作是把用户写的组件名转成真实库名，再按依赖做拓扑展开（`:292` 的 `expand_topologically`）。

这解释了一个常见误判：`llvm-config --libs core analysis` 不会只输出两个 `-l`。在 `pr-lane` 构建上实测：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/llvm-config --libs core analysis
```

```text
-lLLVMAnalysis -lLLVMProfileData -lLLVMSymbolize -lLLVMDebugInfoBTF -lLLVMDebugInfoPDB -lLLVMDebugInfoMSF -lLLVMDebugInfoCodeView -lLLVMDebugInfoGSYM -lLLVMDebugInfoDWARF -lLLVMObject -lLLVMTextAPI -lLLVMMCParser -lLLVMIRReader -lLLVMAsmParser -lLLVMMC -lLLVMDebugInfoDWARFLowLevel -lLLVMBitReader -lLLVMFrontendHLSL -lLLVMCore -lLLVMRemarks -lLLVMBitstreamReader -lLLVMOption -lLLVMBinaryFormat -lLLVMTargetParser -lLLVMSupport -lLLVMDemangle
```

顺序不是字母序，是链接顺序：依赖者在前，被依赖者在后。再跑一个只依赖 `Support` 的查询：

```bash
$B/llvm-config --libs support
```

```text
-lLLVMSupport -lLLVMDemangle
```

如果 `Support` 的 `LINK_COMPONENTS` 里多了 `Core`，这条查询会把 `Core` 和它背后的 `Analysis` 一并拖进来。用户只是想用 `raw_ostream`，链接命令行却膨胀成半个 LLVM。这是分层错误最直接的可见后果。

构建里到底有哪些组件、哪些目标，直接问工具：

```bash
$B/llvm-config --components | head
$B/llvm-config --targets-built
$B/llvm-config --shared-mode
```

`--components` 的输出是一行，下面按原样保留；在 `pr-lane` 上它从 `aarch64` 排到 `xray`，共 133 个名字：

```text
aarch64 aarch64asmparser aarch64codegen aarch64desc aarch64disassembler aarch64info aarch64utils abi aggressiveinstcombine all all-targets amdgpu amdgpuasmparser amdgpucodegen amdgpudesc amdgpudisassembler amdgpuinfo amdgputargetmca amdgpuutils analysis asmparser asmprinter binaryformat bitreader bitstreamreader bitwriter cas cfguard cgdata codegen codegentypes core coroutines coverage debuginfobtf debuginfocodeview debuginfodwarf debuginfodwarflowlevel debuginfogsym debuginfologicalview debuginfomsf debuginfopdb demangle dlltooldriver dtlto dwarflinker dwarflinkerclassic dwarflinkerparallel dwp engine executionengine extensions filecheck frontendatomic frontenddirective frontenddriver frontendhlsl frontendoffloading frontendopenacc frontendopenmp fuzzercli fuzzmutate globalisel hipstdpar instcombine instrumentation interfacestub interpreter ipo irprinter irreader jitlink libdriver lineeditor linker lto mc mca mcdisassembler mcjit mcparser mirparser native nativecodegen nvptx nvptxcodegen nvptxdesc nvptxinfo objcarcopts objcopy object objectyaml option orcdebugging orcjit orcshared orctargetprocess passes plugins profiledata remarks riscv riscvasmparser riscvcodegen riscvdesc riscvdisassembler riscvinfo riscvtargetmca runtimedyld sandboxir scalaropts selectiondag support supportlsp symbolize tablegen target targetparser telemetry textapi textapibinaryreader transformutils vectorize windowsdriver windowsmanifest x86 x86asmparser x86codegen x86desc x86disassembler x86info x86targetmca xray
```

```text
X86 AArch64 RISCV NVPTX AMDGPU
shared
```

最后一行来自 `--shared-mode`：这个构建按组件共享库链接，不是 `libLLVM` 单库模式。`--components` 是小写组件名，`llvm_map_components_to_libnames` 接收时大小写不敏感，输出会还原成 `LLVMCore` 这类目标名。注意 `--targets-built` 与 Ch 0 记录的三目标不同；这是构建配置变化，不是源码变化。判断一个组件是否存在，以你手上的构建输出为准；判断它依赖谁，回到 7c7a 的 CMake 文件。

组件组（component group）还会用 `ADD_TO_COMPONENT` 合并子库。`X86Desc` 在 `llvm/lib/Target/X86/MCTargetDesc/CMakeLists.txt:30` 把 `X86Desc` 加到 `X86` 组；`AddLLVM.cmake:1063` 会把子组件名追加到组的 `LLVM_LINK_COMPONENTS`。所以 `llvm-config --libs x86` 能展开成 `X86Info`、`X86Desc`、`X86CodeGen` 等一组库。组不是新的编译单元，只是组件名的聚合。

## 依赖方向：用真实 CMakeLists 证明

把关键组件排成表。每一行都能在源码里找到 `LINK_COMPONENTS`：

| 组件 | CMake 目标 | 声明的依赖 | 证据 |
| --- | --- | --- | --- |
| Support | `LLVMSupport` | Demangle | `llvm/lib/Support/CMakeLists.txt:361` |
| Core / IR | `LLVMCore` | BinaryFormat, Demangle, Option, Remarks, Support, TargetParser | `llvm/lib/IR/CMakeLists.txt:108` |
| Analysis | `LLVMAnalysis` | Core, Support, Object, ProfileData, TargetParser 等 | `llvm/lib/Analysis/CMakeLists.txt:197` |
| TransformUtils | `LLVMTransformUtils` | Analysis, Core, ProfileData, Support, TargetParser | `llvm/lib/Transforms/Utils/CMakeLists.txt:108` |
| CodeGen | `LLVMCodeGen` | Analysis, Core, MC, Target, TransformUtils 等 | `llvm/lib/CodeGen/CMakeLists.txt:301` |
| MC | `LLVMMC` | BinaryFormat, DebugInfoDWARFLowLevel, Option, Support, TargetParser | `llvm/lib/MC/CMakeLists.txt:88` |
| Target | `LLVMTarget` | Analysis, Core, MC, Support, TargetParser | `llvm/lib/Target/CMakeLists.txt:15` |
| Scalar | `LLVMScalarOpts` | Analysis, Core, TransformUtils 等 | `llvm/lib/Transforms/Scalar/CMakeLists.txt:103` |
| Passes | `LLVMPasses` | Analysis, CodeGen, Target, TransformUtils, Vectorize 等 | `llvm/lib/Passes/CMakeLists.txt:24` |

这张表里最值得盯的是 `CodeGen`：它依赖 `Analysis`。所以 `llvm/lib/CodeGen/HardwareLoops.cpp:25` 出现

```cpp
#include "llvm/Analysis/ScalarEvolution.h"
```

是合法的，不是越层。`HardwareLoops.cpp` 还在 `:42` 包含 `llvm/Transforms/Utils/ScalarEvolutionExpander.h`，因为 `CodeGen` 也声明了 `TransformUtils`。判断依据永远是目标声明，不是「名字看起来像上层还是下层」。

反过来，`IR` 使用 `Analysis` 就非法。`LLVMCore` 的依赖列表里没有 `Analysis`，而 `LLVMAnalysis` 明确依赖 `Core`。在 `llvm/lib/IR` 里引入 `AssumptionCache` 会形成：

```text
Core -> Analysis -> Core
```

`Analysis/AssumptionCache.h` 是公开头文件，`AssumptionCache` 类在 `llvm/include/llvm/Analysis/AssumptionCache.h:45`，构造函数带 `LLVM_ABI`（`:117`）。它属于 `LLVMAnalysis` 的符号，不属于 `LLVMCore`。`IR` 里已有的 `Assumptions.cpp`（`llvm/lib/IR/CMakeLists.txt:9`）处理的是 `llvm.assume` 这一层的 IR 语义，不等于可以把整套 analysis 缓存搬下来。

`Analysis` 使用 `llvm/Transforms/Utils/...` 同样非法。`LLVMAnalysis` 的依赖列表没有 `TransformUtils`，而 `LLVMTransformUtils` 依赖 `Analysis`。这是反向边，加入后就是两节点环。源码现状也能佐证：在 `llvm/lib/Analysis` 和 `llvm/include/llvm/Analysis` 里搜索 `#include "llvm/Transforms/`，没有命中。不是没人想复用，而是这条边被分层挡住了。

## 公开头文件、私有头文件与 include 路径

### 两个目录，两种契约

- `llvm/include/llvm/<Component>/...` 是公开头文件。树外项目会包含它，安装规则会安装它，符号需要按 ABI 规则导出。例子：`llvm/include/llvm/Analysis/AssumptionCache.h`。
- `llvm/lib/<Component>/...` 下的头文件是私有的，只给同一组件的 `.cpp` 使用。例子：`llvm/lib/IR/LLVMContextImpl.h`、`llvm/lib/IR/AttributeImpl.h`。它们不该被其他组件 include，也不该承诺 ABI 稳定。

`CodingStandards.md:893` 的标题就是 “Keep Internal Headers Private”，`:900` 给出做法：把内部通信接口放在源文件同目录的私有头里，本地 include。把 helper 塞进公开头，等于把实现细节变成外部契约。

### 为什么 `Support` 里能看到 `llvm/Analysis/...`

因为 include 路径是全局的，见本章开头的 `llvm/CMakeLists.txt:1368`。`Support` 的编译命令行里包含 `-I<build>/include -I<llvm>/include`，`llvm/Analysis/AssumptionCache.h` 就在第二个目录下。预处理器不会读 `LINK_COMPONENTS`。

所以「这个 include 能不能写」有两层答案：

- 语法层：能，编译器找得到。
- 分层层：不能，`Support` 不该知道 `Analysis` 的存在；即使只用到头文件里的声明，也会把上层类型带进下层 API。

`CodingStandards.md:858` 还提醒：链接器不会检查 inline 函数造成的头文件环。一个只在头文件里调用的上层 inline 函数，可以让依赖环在静态构建里长期潜伏；换成共享构建或另一种链接顺序才爆出来。

### `LLVM_ABI` 解决的是导出，不是分层

`LLVM_ABI` 定义在 `llvm/include/llvm/Support/Compiler.h:156`。注释说明它标记「在共享库构建中显式导出的符号」。实际展开受 `LLVM_ENABLE_LLVM_EXPORT_ANNOTATIONS` 和 `LLVM_BUILD_STATIC` 控制（`:187`）；开关在 `llvm/CMakeLists.txt:1123` 默认 OFF，`:1124` 在 dylib 构建或插件开启时打开。注意 `llvm/CMakeLists.txt:1201` 才把 `BUILD_SHARED_LIBS` 赋给 `LLVM_BUILD_SHARED_LIBS`，读 `:1124` 的条件要按 CMake 的求值顺序看，不能把这个名字当成即时别名。

一个公开头里的函数是否带 `LLVM_ABI`，是它在 `BUILD_SHARED_LIBS=ON` 或 `libLLVM` 构建下能否被外部目标解析的关键因素。它不决定这个函数属于哪个组件，也不阻止你把 `#include` 写反。review 时要分开看：

- 分层：这个符号该不该由这个组件提供？
- ABI：这个公开符号有没有正确导出？
- 私有头：这个声明是不是应该留在 `lib/` 里？

`LLVM_ABI_FOR_TEST` 是另一个信号（`Compiler.h:174`）：它用于「库内部头文件里的符号，只为单测而导出」。看到它，说明这个头不是公开接口，别把它当稳定 API 依赖。

## 构建选项的取舍

LLVM 的构建选项经常被混为一谈。它们控制的是不同层次：

| 选项 | 控制什么 | 关键约束 | 证据 |
| --- | --- | --- | --- |
| `LLVM_ENABLE_PROJECTS` | 是否加入 clang、lldb、mlir 等子项目 | 只影响项目目录，不改变 LLVM 组件库依赖 | `llvm/CMakeLists.txt:141`，`llvm/docs/CMake.md:234` |
| `LLVM_TARGETS_TO_BUILD` | 构建哪些后端 | `all` 会展开成全部目标；减少目标能显著缩小构建 | `llvm/CMakeLists.txt:592`，`llvm/docs/CMake.md:259` |
| `BUILD_SHARED_LIBS` | 每个组件库是 `.so` 还是 `.a` | Windows MSVC 不支持；与 `LLVM_LINK_LLVM_DYLIB` 互斥 | `llvm/CMakeLists.txt:598`、`:964`、`:1001` |
| `LLVM_BUILD_LLVM_DYLIB` | 是否额外生成单个 `libLLVM` | 文档说明不能与 `BUILD_SHARED_LIBS` 同开；可用 `LLVM_DYLIB_COMPONENTS` 裁剪 | `llvm/CMakeLists.txt:989`，`llvm/docs/CMake.md:402` |
| `LLVM_LINK_LLVM_DYLIB` | 工具是否链接到 `libLLVM` | 打开它会自动打开 `LLVM_BUILD_LLVM_DYLIB`；不能与 `BUILD_SHARED_LIBS` 同开 | `llvm/CMakeLists.txt:982`、`:1001` |

`pr-lane` 构建的实际选择可以直接从缓存读：

```bash
grep -E '^(BUILD_SHARED_LIBS|LLVM_BUILD_LLVM_DYLIB|LLVM_LINK_LLVM_DYLIB|LLVM_ENABLE_PROJECTS|LLVM_TARGETS_TO_BUILD):' \
  /home/ubuntu/llvm/builds/pr-lane/CMakeCache.txt
```

```text
BUILD_SHARED_LIBS:BOOL=ON
LLVM_BUILD_LLVM_DYLIB:BOOL=OFF
LLVM_ENABLE_PROJECTS:STRING=clang
LLVM_LINK_LLVM_DYLIB:BOOL=OFF
LLVM_TARGETS_TO_BUILD:STRING=X86;AArch64;RISCV;NVPTX;AMDGPU
```

这说明它是「每个组件一个 `.so`」的开发者构建，不是 `libLLVM` 构建。产物也对得上：

```bash
cd /home/ubuntu/llvm/builds/pr-lane/lib
ls -lhL libLLVMSupport.so libLLVMCore.so libLLVMAnalysis.so \
       libLLVMTransformUtils.so libLLVMCodeGen.so
ls libLLVM.so
```

```text
-rwxrwxr-x 1 ubuntu ubuntu 170M Nov 20 20:45 libLLVMAnalysis.so
-rwxrwxr-x 1 ubuntu ubuntu 370M Nov 20 20:47 libLLVMCodeGen.so
-rwxrwxr-x 1 ubuntu ubuntu 119M Nov 20 20:43 libLLVMCore.so
-rwxrwxr-x 1 ubuntu ubuntu  46M Nov 20 20:39 libLLVMSupport.so
-rwxrwxr-x 1 ubuntu ubuntu 128M Nov 20 20:45 libLLVMTransformUtils.so
ls: cannot access 'libLLVM.so': No such file or directory
```

`LLVM_BUILD_LLVM_DYLIB=OFF` 时没有 `libLLVM.so`，不要让 review 结论依赖它的存在。反过来，发行版常见的 `libLLVM-XX.so` 是 dylib 模型，符号可见性、链接命令和这里不同。

`LLVM_TARGETS_TO_BUILD` 也要以构建为准。`pr-lane` 的缓存是五个目标，而本课 Ch 0 记录的实验构建写的是三个目标。这不是 `llvm-config` 说谎，而是构建配置已经变化。看到这类不一致，先读 `CMakeCache.txt`，再决定用哪份输出写结论。

## 失败例子：越层 include 怎样变成链接错误

下面这个实验在 `/tmp` 建一个树外项目，只链接 `support`，但源码包含 `llvm/IR/LLVMContext.h` 并构造 `LLVMContext`。`CMakeLists.txt` 的关键部分：

```cmake
find_package(LLVM REQUIRED CONFIG
  PATHS /home/ubuntu/llvm/builds/pr-lane/lib/cmake/llvm NO_DEFAULT_PATH)
include_directories(${LLVM_INCLUDE_DIRS})
add_definitions(${LLVM_DEFINITIONS})
add_executable(layer_include main.cpp)
llvm_map_components_to_libnames(llvm_libs support)
target_link_libraries(layer_include PRIVATE ${llvm_libs})
```

`main.cpp`：

```cpp
#include "llvm/IR/LLVMContext.h"
#include "llvm/Support/raw_ostream.h"
int main() {
  llvm::LLVMContext C;
  llvm::errs() << "context alive\n";
}
```

配置成功，编译也成功；链接失败。实测输出：

```text
/usr/bin/ld: CMakeFiles/layer_include.dir/main.cpp.o: in function `main':
main.cpp:(.text+0x24): undefined reference to `llvm::LLVMContext::LLVMContext()'
main.cpp:(.text+0x4a): undefined reference to `llvm::LLVMContext::~LLVMContext()'
main.cpp:(.text+0x73): undefined reference to `llvm::LLVMContext::~LLVMContext()'
collect2: error: ld returned 1 exit status
```

把组件列表改成 `support core`，重新配置和链接，程序输出：

```text
context alive
```

这个实验说明两件事：

1. include 成功不代表依赖声明正确。头文件全局可见，符号仍由组件库提供。
2. 修法不是「把缺的库全加到 `LINK_COMPONENTS`」。如果这个 include 出现在 `Support` 的实现里，正确修法是移除对 IR 的依赖，而不是让 `Support -> Core`。

为什么不能直接加？把 `Support -> Core` 放进共享构建，会形成 `Support -> Core -> Support`。用同样的三节点结构做最小复现（共享库，CMake 配置阶段）：

```text
CMake Error: The inter-target dependency graph contains the following strongly connected component (cycle):
  "LLVMSupport" of type SHARED_LIBRARY
    depends on "LLVMCore" (weak)
    depends on "LLVMAnalysis" (weak)
  "LLVMCore" of type SHARED_LIBRARY
    depends on "LLVMSupport" (weak)
    depends on "LLVMAnalysis" (weak)
  "LLVMAnalysis" of type SHARED_LIBRARY
    depends on "LLVMCore" (weak)
    depends on "LLVMSupport" (weak)
At least one of these targets is not a STATIC_LIBRARY.  Cyclic dependencies are allowed only among static libraries.
```

最小复现不是 LLVM 本体，但它复刻了 `pr-lane` 的共享库条件：`BUILD_SHARED_LIBS=ON` 时，CMake 在生成阶段就拒绝环。静态构建允许静态库之间成环，环会推迟到链接器按顺序解析时才暴露；这就是「本地静态构建能过、共享构建挂掉」的来源。真实 patch 是否触发同样的错误，`需在你的构建验证`，但依赖方向已经由 CMake 文件决定。

## Reviewer 的审计顺序

拿到一个改依赖的 PR，按这个顺序看，别从 diff 的缩进开始：

1. **定位目标**：改的是哪个 `add_llvm_component_library`，目标名是什么？
2. **读现有依赖**：该目标的 `LINK_COMPONENTS` 列表里已经有什么？不要凭目录名猜。
3. **列新增 include**：每个 `#include "llvm/X/..."` 对应哪个组件库？公开头还是私有头？
4. **查方向**：依赖边是否与现有图同向？把 `A -> B` 和 `B -> A` 都写出来，找两节点或三节点环。
5. **查生成头**：新增 include 是否依赖 TableGen 产物？对应目标是否在 `DEPENDS` 里？第 11 章讲过 `.inc` 的生成时机。
6. **查 ABI**：新增公开符号是否带 `LLVM_ABI`？是否误用了 `LLVM_ABI_FOR_TEST`？
7. **查构建模型**：这个 patch 在静态、共享、`libLLVM` 三种构建下分别怎样？至少要说明一种会失败的条件。

如果 PR 只改头文件、不碰 CMake，也不要跳过第 4 步。头文件里的 inline 函数可以把依赖藏进每个消费者，链接器不查。

## 踩雷集锦

1. **「能编译就能依赖」**：全局 include 路径让越层 include 编译通过，错误在链接或共享库生成阶段才出现。看到新 include，先查组件，不要等 CI。
2. **「Analysis 是上层，所以谁都能用」**：方向反了。`Analysis` 依赖 `Core`，所以 `Core` 不能依赖 `Analysis`。`CodeGen` 能用 `Analysis`，是因为 `CodeGen` 明确声明了这条边。
3. **「CodeGen 用 ScalarEvolution 一定越层」**：`CodeGen` 的 `LINK_COMPONENTS` 含 `Analysis`（`llvm/lib/CodeGen/CMakeLists.txt:301`），真实代码也在用（`HardwareLoops.cpp:25`）。判断依据是声明，不是直觉。
4. **「`BUILD_SHARED_LIBS=ON` 就是 `libLLVM`」**：前者是每个组件一个 `.so`，后者是单个聚合库。`pr-lane` 是前者，目录里没有 `libLLVM.so`。
5. **「`llvm-config --libs core` 只输出 `-lLLVMCore`」**：它会按依赖拓扑展开，输出一长串库，顺序就是链接顺序。
6. **「加 `LLVM_ABI` 就能跨库用」**：它只控制导出可见性。符号属于哪个库、该不该被这个层使用，仍是分层问题。

## 动手练习

1. 用 `rg -n "add_llvm_component_library" llvm/lib` 列出 10 个组件，挑三个画出它们的 `LINK_COMPONENTS` 边。检查有没有你原本以为在上层的库其实在下层。
2. 在 `pr-lane` 上跑 `llvm-config --libs support` 和 `--libs analysis`，比较两者的库数量与顺序。解释为什么 `analysis` 的输出里会出现 `LLVMMC`。
3. 找一个同时包含 `llvm/Analysis/...` 和 `llvm/Transforms/Utils/...` 的 `CodeGen` 源文件，核对 `llvm/lib/CodeGen/CMakeLists.txt` 是否同时声明了 `Analysis` 和 `TransformUtils`。
4. 把一个树外工具的 `llvm_map_components_to_libnames` 从 `support` 改成 `support core`，观察链接命令和可执行文件大小的变化。不要修改 LLVM 源码树。

## 本章重点整理

- `add_llvm_component_library` 把目标名、组件名和 `LLVM_LINK_COMPONENTS` 属性绑定在一起；依赖声明是分层的事实来源。
- 依赖方向是 `Support -> Core -> Analysis -> TransformUtils`，`CodeGen` 在这条链之上并合法依赖 `Analysis`；`IR -> Analysis` 和 `Analysis -> TransformUtils` 是反向边。
- 公开头在 `llvm/include/llvm`，私有头在 `llvm/lib`；全局 include 路径只保证能编译，不保证分层正确。
- `LLVM_ABI` 管导出，不管归属；`LLVM_ABI_FOR_TEST` 标记的是内部测试符号。
- `BUILD_SHARED_LIBS`、`LLVM_BUILD_LLVM_DYLIB`、`LLVM_LINK_LLVM_DYLIB` 是三种不同的构建模型，失败方式不同。
- `llvm-config --libs` 的输出是拓扑展开后的链接顺序，不是组件名清单。

## 自我检核

- [ ] 不看笔记，能说出 `add_llvm_component_library` 至少做哪三件事
- [ ] 能解释 `LINK_COMPONENTS` 作为参数和作为变量的区别
- [ ] 能画出 `Support`、`Core`、`Analysis`、`TransformUtils`、`CodeGen` 的依赖方向
- [ ] 能举出一个「CodeGen 使用 Analysis 合法」的真实文件和 CMake 证据
- [ ] 能解释为什么 `Support` 里 include `llvm/IR/Value.h` 会编译通过但链接失败
- [ ] 能说明 `LLVM_ABI` 与分层判断为什么是两件事
- [ ] 能区分 `BUILD_SHARED_LIBS` 与 `LLVM_BUILD_LLVM_DYLIB` 的产物
- [ ] 能读懂 `llvm-config --libs` 的输出顺序，并解释它来自拓扑展开
- [ ] 拿到一个新增 `LINK_COMPONENTS` 的 diff，能按本章顺序找出潜在环

## 延伸阅读

- **[LLVM CMake 文档：LLVM-related variables](https://llvm.org/docs/CMake.html#llvm-related-variables)**
  - **读哪里**：`BUILD_SHARED_LIBS`、`LLVM_BUILD_LLVM_DYLIB`、`LLVM_LINK_LLVM_DYLIB`、`LLVM_TARGETS_TO_BUILD` 四节
  - **能学到什么**：每个选项的官方语义和互斥条件，尤其是「开发者共享构建」与「发行版单库构建」的区别
  - **前提**：读过本章的构建选项表；不熟悉 CMake 的话先看 `add_library` 的 `STATIC`/`SHARED`
- **[LLVM Coding Standards：Library Layering](https://llvm.org/docs/CodingStandards.html#library-layering)**
  - **读哪里**：整节，包括 Unix linker 从左到右解析、inline 函数绕过检查的段落
  - **能学到什么**：为什么「链接器能过」不等于分层正确，以及如何用「把所有 inline 函数改成 out-of-line」来检验依赖
  - **和本章的关联**：这是本章所有方向判断的规范来源
- **[LLVM Coding Standards：Keep Internal Headers Private](https://llvm.org/docs/CodingStandards.html#keep-internal-headers-private)**
  - **读哪里**：整节
  - **能学到什么**：私有头应该放在哪里、为什么不该把 helper 塞进公开头
  - **前提**：知道 `llvm/include/llvm` 与 `llvm/lib` 的安装差异
- **[LLVM `Compiler.h` 中的 `LLVM_ABI` 注释](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/Support/Compiler.h)**
  - **读哪里**：`LLVM_ABI`、`LLVM_ABI_EXPORT`、`LLVM_ABI_FOR_TEST` 三段注释
  - **能学到什么**：导出宏在 Windows/ELF 上的展开差异，以及测试专用导出的边界
  - **前提**：读过本章的公开/私有头一节；不需要先懂链接器脚本
- **[CMake 官方文档：target_link_libraries](https://cmake.org/cmake/help/latest/command/target_link_libraries.html)**
  - **读哪里**：`PUBLIC`、`PRIVATE`、`INTERFACE` 的传播语义
  - **能学到什么**：为什么一个组件的依赖会传给消费者，以及为什么环在共享库上更早失败
  - **和本章的关联**：LLVM 的 `llvm_add_library` 最终仍落到 CMake 的目标链接模型

本章的结论可以压成一句话：**include 决定你看见什么，`LINK_COMPONENTS` 决定你链接什么，reviewer 要同时看这两层。**

→ [Ch 13 — New Pass Manager：`PassBuilder`、`AnalysisManager`、pipeline 文本](./13-new-pass-manager.md)
