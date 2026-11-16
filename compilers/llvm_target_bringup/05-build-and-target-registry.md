# Ch 05 — 构建系统与 TargetRegistry

> **目标**：把“target 是否存在”拆成 configure / link / run 三层的可验证事实，能独立排查 `llc --version` 看不到 target、链接缺符号、`llvm-config` 报组件不存在这三类问题。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`（`X86;AArch64;RISCV;RVTrim;MiniRV`，clang，assertions on）。本章命令约定 `T=/home/ubuntu/llvm/trees/dsl-tablegen`、`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`、`BUILD=/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

一个新 target 在 LLVM 里有三种“不存在”，而且报错完全不同：

1. **configure 层不存在**：没进 `LLVM_TARGETS_TO_BUILD`，CMake 根本不会 `add_subdirectory` 你的目录。`llc --version` 里没有你，源代码却全在。
2. **生成物层不存在**：编译进了，但 `Targets.def` / `AsmParsers.def` 里没有你。`InitializeAll*` 不会调用你的初始化函数，registry 是空的。
3. **链接层不存在**：`.def` 里有你，但 `LLVMInitializeMiniRVTarget` 之类的符号没人定义。构建在链接期失败，或者运行时 registry 只填了一半。

这三层分别由 CMake、`configure_file`、`TargetSelect.h` 的宏展开负责。搞清这条链，你就能在十分钟内定位“为什么 `llc` 不认识我的 target”，而不是盲目改代码。MiniRV 的 33d9ec8 甚至专门为 MC-only target 改了 `LLVM-Config.cmake`——因为构建系统原本假设“target 一定有 CodeGen 库”。

## 先建立直觉

```
【configure 时】
llvm/CMakeLists.txt
  ├─ LLVM_ALL_TARGETS        决定“你是不是 core tier”
  └─ LLVM_TARGETS_TO_BUILD   决定“这次构建要不要你”
            │
            ▼
llvm/lib/Target/CMakeLists.txt
  foreach(t ${LLVM_TARGETS_TO_BUILD})
    add_subdirectory(${t})  ──► lib/Target/MiniRV/CMakeLists.txt
                                   ├─ add_llvm_component_group(MiniRV)
                                   ├─ llvm_tablegen(...)  （生成 .inc 的规则）
                                   ├─ add_llvm_target(MiniRVCodeGen ...)
                                   └─ add_subdirectory(MCTargetDesc/TargetInfo/AsmParser/Disassembler)
            │
            ▼  收集 LLVM_ENUM_TARGETS / LLVM_ENUM_ASM_PARSERS / ...
  configure_file → BUILD/include/llvm/Config/Targets.def
                                 AsmPrinters.def
                                 AsmParsers.def
                                 Disassemblers.def
                                 TargetMCAs.def

【build 时】
  MiniRV.td ──llvm-tblgen──► MiniRVGen*.inc ──┐
  MiniRV*.cpp ────────────────────────────────┴─► libLLVMMiniRV*.so

【运行 llc 时】
  TargetSelect.h 展开 Targets.def
     └─ InitializeAllTargets()
          └─ LLVMInitializeMiniRVTargetInfo / Target / TargetMC
                └─ RegisterTarget / RegisterTargetMachine / RegisterMC*
                      └─ TargetRegistry（FirstTarget 单链表）
                            └─ lookupTarget(triple) → MiniRV
```

一句话：**CMake 决定“编不编”，`.def` 决定“调不调”，注册函数决定“填什么”。**

## 主线：从 `LLVM_ALL_TARGETS` 到 `TargetRegistry`

### 5.1 第一层：把 target 放进构建

`llvm/CMakeLists.txt:565` 维护 core tier 名单，MiniRV 在 `RVTrim` 后面：

```cmake
set(LLVM_ALL_TARGETS
  ...
  RISCV
  RVTrim
  MiniRV
  Sparc
  ...
)
```

`llvm/CMakeLists.txt:601` 的缓存变量决定本次构建：

```cmake
set(LLVM_TARGETS_TO_BUILD "all"
    CACHE STRING "Semicolon-separated list of targets to build, or \"all\".")
```

`"all"` 在 `llvm/CMakeLists.txt:724` 展开成 `LLVM_ALL_TARGETS`；随后和 `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` 合并（`:739`）。真正决定“要不要你的目录”的是 `llvm/lib/Target/CMakeLists.txt:36`：

```cmake
foreach(t ${LLVM_TARGETS_TO_BUILD})
  message(STATUS "Targeting ${t}")
  add_subdirectory(${t})
endforeach()
```

有两个硬约束：

- **不在 `LLVM_ALL_TARGETS` 也不在 experimental 名单**：`llvm/CMakeLists.txt:1167` 直接 `FATAL_ERROR`（“not a core tier target… must be passed via LLVM_EXPERIMENTAL_TARGETS_TO_BUILD”）。这是第一次加 target 最常见的错误。
- **在 `LLVM_ALL_TARGETS` 但不在 `LLVM_TARGETS_TO_BUILD`**：不报错，静默不构建；一个在名单只有三个 target 时编译的 `llvm-config` 会一直说三个。

### 5.2 第二层：MiniRV 的 CMakeLists 逐行

`llvm/lib/Target/MiniRV/CMakeLists.txt` 的结构是任何 target 的模板：

```cmake
add_llvm_component_group(MiniRV)                          # :1  伪组件，无源码
include(AutoTD)
llvm_autotd_generate(MiniRV ...)                          # :11 语料 → AutoTdInstrInfo.inc
set(LLVM_TARGET_DEPENDS ${MiniRV_AUTOTD_INSTRINFO})
set(LLVM_TARGET_DEFINITIONS MiniRV.td)
llvm_tablegen(MiniRVGenAsmMatcher.inc -gen-asm-matcher)   # :20
...                                                       # :21-30 共 11 条
add_public_tablegen_target(MiniRVCommonTableGen)          # :32
add_llvm_target(MiniRVCodeGen ...                         # :34
  LINK_COMPONENTS ... MiniRVDesc MiniRVInfo ...
  ADD_TO_COMPONENT MiniRV)
add_subdirectory(MCTargetDesc)   # :64
add_subdirectory(TargetInfo)     # :65
add_subdirectory(AsmParser)      # :66
add_subdirectory(Disassembler)   # :67
```

三个宏的分工必须分清：

| 宏 | 定义位置 | 产生什么 | 作用 |
|---|---|---|---|
| `add_llvm_component_group(MiniRV)` | `llvm/cmake/modules/AddLLVM.cmake:985` | 一个无源码的 custom target `MiniRV` | ① 给 `ADD_TO_COMPONENT MiniRV` 一个属性宿主；② 让 MC-only 展开分支的 `TARGET ${c}` 成立 |
| `add_llvm_component_library(LLVMMiniRVDesc ...)` | `AddLLVM.cmake:1007` | `LLVMMiniRVDesc` 库 | 注册一个真实组件；`ADD_TO_COMPONENT MiniRV` 把它加进 group 的 `LLVM_LINK_COMPONENTS` |
| `add_llvm_target(MiniRVCodeGen ...)` | `AddLLVM.cmake:1747` | `LLVMMiniRVCodeGen` 库 | 本质是 `add_llvm_component_library(LLVM${target_name} ...)`，只是多设了 include 路径和 `CURRENT_LLVM_TARGET` |

`llvm_tablegen` 每个参数生成一个 `.inc`。`-gen-register-info` 还会额外生成四个文件（`Enums` / `Header` / `MCDesc` / `TargetDesc`），这是 `llvm/cmake/modules/TableGen.cmake:163` 的 `llvm_tablegen()` 内部用 `EXTRA_OUTPUTS` 做的。构建目录里的真实文件列表：

```console
$ ls $BUILD/lib/Target/MiniRV/*.inc | xargs -n1 basename
MiniRVGenAsmMatcher.inc
MiniRVGenAsmWriter.inc
MiniRVGenCallingConv.inc
MiniRVGenDAGISel.inc
MiniRVGenDisassemblerTables.inc
MiniRVGenInstrInfo.inc
MiniRVGenMCCodeEmitter.inc
MiniRVGenMCPseudoLowering.inc
MiniRVGenRegisterInfo.inc
MiniRVGenRegisterInfoEnums.inc
MiniRVGenRegisterInfoHeader.inc
MiniRVGenRegisterInfoMCDesc.inc
MiniRVGenRegisterInfoTargetDesc.inc
MiniRVGenSDNodeInfo.inc
MiniRVGenSubtargetInfo.inc
```

`MiniRVGenRegisterInfo.inc` 的四个兄弟文件不是手写的，也不在 `llvm_tablegen(...)` 列表里；它们是 `-gen-register-info` 的副产物。

生成规则可以复现：`ninja -C $BUILD -t commands lib/Target/MiniRV/MiniRVGenInstrInfo.inc | tail -1` 会打出 `llvm-tblgen -gen-instr-info -I... MiniRV.td --write-if-changed -o MiniRVGenInstrInfo.inc`（后面还有 depfile 处理）。

### 5.3 第三层：`.def` 文件是 configure 生成的，不是你写的

`llvm/CMakeLists.txt:1160` 开始，对每个 `LLVM_TARGETS_TO_BUILD` 做这几件事：

```cmake
foreach(t ${LLVM_TARGETS_TO_BUILD})
  set(td ${LLVM_MAIN_SRC_DIR}/lib/Target/${t})
  # core tier 检查（:1167），不通过就 FATAL_ERROR
  set(LLVM_ENUM_TARGETS "...LLVM_TARGET(${t})\n")                    # :1179
  file(GLOB asmp_file "${td}/*AsmPrinter.cpp")                       # :1184
  if(asmp_file) ... LLVM_ASM_PRINTER(${t}) ... endif()
  if(EXISTS ${td}/AsmParser/CMakeLists.txt) ...                       # :1189
  if(EXISTS ${td}/Disassembler/CMakeLists.txt) ...                    # :1193
  if(EXISTS ${td}/MCA/CMakeLists.txt) ...                             # :1197
  endforeach()
```

然后 `configure_file` 写到 `BUILD/include/llvm/Config/*.def`。真实内容：

```console
$ tail -8 $BUILD/include/llvm/Config/Targets.def
LLVM_TARGET(X86)
LLVM_TARGET(AArch64)
LLVM_TARGET(RISCV)
LLVM_TARGET(RVTrim)
LLVM_TARGET(MiniRV)


#undef LLVM_TARGET
```

```console
$ tail -7 $BUILD/include/llvm/Config/TargetMCAs.def

LLVM_TARGETMCA(X86)
LLVM_TARGETMCA(RISCV)
LLVM_TARGETMCA(RVTrim)


#undef LLVM_TARGETMCA
```

`TargetMCAs.def` 里没有 MiniRV，因为 MiniRV 没有 `MCA/CMakeLists.txt`。这是一个**目录存在性即开关**的设计：你新建 `MCA/` 目录并写 CMakeLists，下一轮 configure 就会要求你提供 `LLVMInitializeMiniRVTargetMCA`；不建就不要求。同理，`AsmPrinter` 用 `file(GLOB)` 判断，所以 33d9ec8 的 MC-only MiniRV 没有 `*AsmPrinter.cpp`；按 `file(GLOB)` 的规则，当时那份 `AsmPrinters.def` 里不会有它：

```console
$ git ls-tree --name-only 33d9ec8c1aed llvm/lib/Target/MiniRV/
llvm/lib/Target/MiniRV/AsmParser
llvm/lib/Target/MiniRV/CMakeLists.txt
llvm/lib/Target/MiniRV/Disassembler
llvm/lib/Target/MiniRV/MCTargetDesc
llvm/lib/Target/MiniRV/MiniRV.td
llvm/lib/Target/MiniRV/MiniRVInstrFormats.td
llvm/lib/Target/MiniRV/MiniRVInstrInfo.td
llvm/lib/Target/MiniRV/MiniRVRegisterInfo.td
llvm/lib/Target/MiniRV/TargetInfo
llvm/lib/Target/MiniRV/auto-td
```

### 5.4 第四层：初始化链到底在哪

不少资料会把初始化函数指到 `llvm/lib/Support/TargetRegistry.cpp`；这个文件在当前树里**不存在**。这是必须澄清的路径错误：

| 你以为在哪 | 实际在哪 | 内容 |
|---|---|---|
| `llvm/lib/Support/TargetRegistry.cpp` | `llvm/lib/MC/TargetRegistry.cpp` | registry 本体：`FirstTarget` 链表、`RegisterTarget`、`lookupTarget`、`printRegisteredTargetsForVersion` |
| 同上 | `llvm/include/llvm/Support/TargetSelect.h` | `InitializeAll*` 全是 **inline**，靠 `Targets.def` 展开；C API 的 `LLVMInitializeAll*` 在 `llvm/include/llvm-c/Target.h` 也是 inline |

注意命名：C++ 侧是 `InitializeAllTargets()`，C API 侧是 `LLVMInitializeAllTargets()`。为什么是 inline？`Targets.def` 在配置时才生成，写不到 `.cpp` 里；放头文件里，每个工具编译期展开成自己需要的那组调用。副作用是**只有真正调用 `LLVMInitializeMiniRV*` 的工具才让链接器保留对应 object file**——这正是显式初始化而非静态构造函数的原因。

各工具的调用子集不同，决定了“哪些符号必须存在”：

| 工具 | 调用 | 后果 |
|---|---|---|
| `llc`（`llvm/tools/llc/lib/llcdriver.cpp:376`） | `InitializeAllTargets` / `TargetMCs` / `AsmPrinters` / `AsmParsers` | 必须有 `LLVMInitializeMiniRVTarget`（`InitializeAllTargets` 内部 `InitializeAllTargetInfos` + `LLVMInitializeMiniRVTarget`） |
| `llvm-mc`（`llvm/tools/llvm-mc/llvm-mc.cpp:390`） | `Infos` / `TargetMCs` / `AsmParsers` / `Disassemblers` | **不需要** `LLVMInitializeMiniRVTarget` |
| `llvm-objdump`（`llvm/tools/llvm-objdump/llvm-objdump.cpp:4033`） | `Infos` / `TargetMCs` / `Disassemblers` | 只需要 Info + MC + Disassembler |

这解释了 33d9ec8 为什么要在 MC-only target 里额外定义一个 `LLVMInitializeMiniRVTarget`：

```cpp
// 33d9ec8 的 MCTargetDesc/MiniRVMCTargetDesc.cpp 末尾
extern "C" LLVM_ABI LLVM_EXTERNAL_VISIBILITY void
LLVMInitializeMiniRVTarget() {
  LLVMInitializeMiniRVTargetInfo();
  LLVMInitializeMiniRVTargetMC();
}
```

`llvm-mc` 永远不会调用它，但 `llc` 链接时会引用它。到了 ad74868，这个函数被移到 `MiniRVTargetMachine.cpp`，内容换成真正的 `RegisterTargetMachine<MiniRVTargetMachine>`。

### 5.5 三个初始化函数各填什么

| 函数 | 实现位置（当前树） | 调用 | 填进 registry 的槽 |
|---|---|---|---|
| `LLVMInitializeMiniRVTargetInfo` | `TargetInfo/MiniRVTargetInfo.cpp` | `RegisterTarget<Triple::minirv64>(T, "minirv64", "MiniRV (auto-td demo)", "MiniRV")` | target 身份：`Name`、`ShortDesc`、`BackendName`、`ArchMatchFn` |
| `LLVMInitializeMiniRVTargetMC` | `MCTargetDesc/MiniRVMCTargetDesc.cpp` | `RegisterMCAsmInfo` / `MCInstrInfo` / `MCRegInfo` / `MCSubtargetInfo` / `MCInstPrinter` / `MCCodeEmitter` / `MCAsmBackend` / `NullTargetStreamer` | `Target` 里对应的 8 个构造函数指针 |
| `LLVMInitializeMiniRVTarget` | `MiniRVTargetMachine.cpp` | `RegisterTargetMachine<MiniRVTargetMachine>` | `TargetMachineCtorFn` |
| `LLVMInitializeMiniRVAsmPrinter` | `MiniRVAsmPrinter.cpp` | `RegisterAsmPrinter<MiniRVAsmPrinter>` | `AsmPrinterCtorFn` |

`AsmParser`/`Disassembler` 同理，分别填 `MCAsmParserCtorFn` 和 `MCDisassemblerCtorFn`。`RegisterTarget` 的实现很短，但有两个细节值得记：

```cpp
// llvm/lib/MC/TargetRegistry.cpp:189
void TargetRegistry::RegisterTarget(Target &T, const char *Name, ...) {
  if (T.Name)
    return;                       // 幂等：重复初始化不会重复插入
  T.Next = FirstTarget;           // 头插法
  FirstTarget = &T;
  ...
}
```

- 头插法意味着 `TargetRegistry::targets()` 的顺序是**注册顺序的逆序**；`llc --version` 会排序后再打印（`printRegisteredTargetsForVersion`）。
- `ArchMatchFn` 来自 `RegisterTarget<Triple::minirv64>` 模板，比较 `TT.getArch() == Triple::minirv64`。所以 Triple 的枚举值和这里的模板参数必须是同一个东西——Ch 04 的枚举不是装饰。

`llc --version` 的输出就是这两个字段的直接投影：

```console
$ $B/llc --version
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
  Default target: x86_64-unknown-linux-gnu
  Host CPU: znver2

  Registered Targets:
    aarch64    - AArch64 (little endian)
    aarch64_32 - AArch64 (little endian ILP32)
    aarch64_be - AArch64 (big endian)
    arm64      - ARM64 (little endian)
    arm64_32   - ARM64 (little endian ILP32)
    minirv64   - MiniRV (auto-td demo)
    riscv32    - 32-bit RISC-V
    riscv32be  - 32-bit big endian RISC-V
    riscv64    - 64-bit RISC-V
    riscv64be  - 64-bit big endian RISC-V
    rvtrim64   - RVTrim: trimmed RV64IMA
    x86        - 32-bit X86: Pentium-Pro and above
    x86-64     - 64-bit X86: EM64T and AMD64
```

`minirv64 - MiniRV (auto-td demo)` 这一行逐字对应 `RegisterTarget` 的第二、三个参数。

### 5.6 MC-only 例外：`llvm_expand_pseudo_components`

`llvm_map_components_to_libnames(LLVM_LIBS MiniRV)` 会把伪组件 `MiniRV` 展开成真实库名。展开函数在 `llvm/cmake/modules/LLVM-Config.cmake:132`。正常 CodeGen target 的路径是：

```cmake
if(LLVM${c}CodeGen IN_LIST LLVM_AVAILABLE_LIBS)     # MiniRV → LLVMMiniRVCodeGen
  list(APPEND expanded_components "${c}CodeGen")
else()
  if(LLVM${c} IN_LIST LLVM_AVAILABLE_LIBS)          # 一个叫 LLVMMiniRV 的库
    ...
  else()
    message(FATAL_ERROR "Target ${c} is not in the set of libraries.")
  endif()
endif()
foreach(subcomponent IN ITEMS AsmPrinter AsmParser Desc Disassembler Info Utils)
  ...                                                # 把存在的子库也加进来
endforeach()
```

MC-only target 既没有 `LLVMMiniRVCodeGen`，也没有 `LLVMMiniRV`，于是 configure 直接 `FATAL_ERROR`。33d9ec8 加了一个空分支：

```cmake
elseif(TARGET ${c} AND LLVM${c}Desc IN_LIST LLVM_AVAILABLE_LIBS)
  # An MC-only target (one with no CodeGen library and no ${c} library)
  # still has a component group and the MC sub-components below.
```

这个分支**故意不 append 任何东西**：`${c}` 是 `add_llvm_component_group` 创建的 custom target（不是库），真正要加的 `Desc`/`Info`/`AsmParser`/`Disassembler` 由下面的 `foreach(subcomponent ...)` 完成。

为什么正常 CodeGen target 不需要？因为 `add_llvm_target(MiniRVCodeGen ...)` 一定生成 `LLVMMiniRVCodeGen`，第一个 `if` 永远命中。33d9ec8 的例外是“历史上第一个只有 MC 库的 target”逼出来的；ad74868 给 MiniRV 加了 `LLVMMiniRVCodeGen` 后，这个分支变成死代码，同一次提交又把它删掉了：

```console
$ git show ad7486897b30 -- llvm/cmake/modules/LLVM-Config.cmake | sed -n '44,54p'
-      elseif(TARGET ${c} AND LLVM${c}Desc IN_LIST LLVM_AVAILABLE_LIBS)
-        # An MC-only target (one with no CodeGen library and no ${c} library)
-        # still has a component group and the MC sub-components below.
       else()
-        message(FATAL_ERROR "Target ${c} is not in the set of libraries.")
+        if(LLVM${c} IN_LIST LLVM_AVAILABLE_LIBS)
+          list(APPEND expanded_components "${c}")
+        else()
+          message(FATAL_ERROR "Target ${c} is not in the set of libraries.")
+        endif()
       endif()
```

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 总名单 | `llvm/CMakeLists.txt` | `LLVM_ALL_TARGETS`（:565）、`LLVM_TARGETS_TO_BUILD`（:601） | core tier vs 本次构建 |
| 目录分发 | `llvm/lib/Target/CMakeLists.txt` | `foreach(t ...)` / `add_subdirectory`（:36） | 只有名单里的 target 才有目录 |
| 组件宏 | `llvm/cmake/modules/AddLLVM.cmake` | `add_llvm_component_group`（:985）、`add_llvm_component_library`（:1007）、`add_llvm_target`（:1747） | group 是 custom target，不是库 |
| target 构建 | `llvm/lib/Target/MiniRV/CMakeLists.txt` | `llvm_tablegen`、`add_public_tablegen_target`、`add_llvm_target`、`ADD_TO_COMPONENT` | 生成的库名与组件归属 |
| 子库 | `MCTargetDesc/`、`TargetInfo/`、`AsmParser/`、`Disassembler/` 的 `CMakeLists.txt` | `add_llvm_component_library(LLVMMiniRV*)` | 每个库对应哪一层 |
| `.def` 生成 | `llvm/CMakeLists.txt` | `LLVM_ENUM_*`（:1154）、`file(GLOB ...AsmPrinter.cpp)`（:1184）、`EXISTS .../AsmParser/CMakeLists.txt`（:1189） | 目录/文件存在性即开关 |
| 初始化声明 | `llvm/include/llvm/Support/TargetSelect.h` | `InitializeAllTargetInfos/Targets/TargetMCs/AsmPrinters/AsmParsers/Disassemblers` | inline 展开 Targets.def |
| registry 本体 | `llvm/lib/MC/TargetRegistry.cpp` | `RegisterTarget`（:189）、`lookupTarget`（:124/:161）、`printRegisteredTargetsForVersion`（:218） | 头插链表与 ArchMatchFn |
| 组件展开 | `llvm/cmake/modules/LLVM-Config.cmake` | `llvm_expand_pseudo_components`（:132） | 伪组件 → 真实库名 |

## 实测：五条命令验证三层

### 1. `llc --version`：run-time 层

见 5.5 的完整输出。判断标准只有一条：**Registered Targets 里有没有你的名字**。有名字说明 configure、`.def`、链接、注册四件事都成了。

### 2. `llvm-config --targets-built`：会撒谎的编译期字符串

```console
$ $B/llvm-config --targets-built
X86 AArch64 RISCV
```

这个输出和 `CMakeCache.txt` 矛盾：

```console
$ grep LLVM_TARGETS_TO_BUILD $BUILD/CMakeCache.txt
LLVM_TARGETS_TO_BUILD:STRING=X86;AArch64;RISCV;RVTrim;MiniRV
```

原因不是 CMake，是**二进制没重链**。`llvm-config` 的这行输出来自编译期宏 `LLVM_TARGETS_BUILT`（`llvm/tools/llvm-config/llvm-config.cpp:602`），宏值写在 `BuildVariables.inc`：

```console
$ grep LLVM_TARGETS_BUILT $BUILD/tools/llvm-config/BuildVariables.inc
#define LLVM_TARGETS_BUILT "X86 AArch64 RISCV RVTrim MiniRV"

$ stat -c '%y %n' $BUILD/bin/llvm-config $BUILD/bin/llc $BUILD/tools/llvm-config/BuildVariables.inc
2026-11-09 07:43:22.168488148 +0800 /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-config
2026-11-14 03:33:45.860839406 +0800 /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc
2026-11-13 17:59:20.923799057 +0800 /home/ubuntu/llvm/builds/dsl-tablegen/tools/llvm-config/BuildVariables.inc
```

`BuildVariables.inc` 已经包含五个 target，但 `bin/llvm-config` 还是 11-09 的旧二进制——它编译时宏还是三个。重新构建 `llvm-config` 后会打印 `X86 AArch64 RISCV RVTrim MiniRV`（**guided**：我没有在这个共享构建目录里重链）。教训：**`llvm-config --targets-built` 不是真相源，`Targets.def` 和 `llc --version` 才是。**

### 3. `ninja -t query bin/llc`：link-time 层

```console
$ ninja -C $BUILD -t query bin/llc | sed -n '1,19p'
bin/llc:
  input: CXX_EXECUTABLE_LINKER__llc_RelWithDebInfo
    tools/llc/CMakeFiles/llc.dir/llc.cpp.o
    | lib/libLLVMLlcDriver.so.24.0git
    | lib/libLLVMX86AsmParser.so.24.0git
    | lib/libLLVMAArch64AsmParser.so.24.0git
    | lib/libLLVMRISCVAsmParser.so.24.0git
    | lib/libLLVMRVTrimAsmParser.so.24.0git
    | lib/libLLVMMiniRVAsmParser.so.24.0git
    | lib/libLLVMX86CodeGen.so.24.0git
    | lib/libLLVMAArch64CodeGen.so.24.0git
    | lib/libLLVMRISCVCodeGen.so.24.0git
    | lib/libLLVMRVTrimCodeGen.so.24.0git
    | lib/libLLVMMiniRVCodeGen.so.24.0git
    | lib/libLLVMX86Desc.so.24.0git
    | lib/libLLVMAArch64Desc.so.24.0git
    | lib/libLLVMRISCVDesc.so.24.0git
    | lib/libLLVMRVTrimDesc.so.24.0git
    | lib/libLLVMMiniRVDesc.so.24.0git
```

MiniRV 的四个库（CodeGen / AsmParser / Desc / Info，`Info` 在后面几行）全部在 `llc` 的链接输入里。少一个，`InitializeAll*` 展开出来的符号就会 undefined。

### 4. `ninja -n llc`：重建范围

```console
$ ninja -C $BUILD -n llc
[0/2] Re-checking globbed directories...
[1/2] Re-running CMake...
```

`-n` 是 dry-run，不执行。它说明当前树相对上次 configure 有变化，ninja 会先重跑 CMake。**加/删 target 正是这种变化**：CMake 重跑 → `Targets.def` 等五个 `.def` 重新生成 → 所有 `#include` 它们的翻译单元重编 → `llc`/`llvm-mc`/`clang` 重链。这不是“全量重建”，但也不是“只编你的 target”。

### 5. `.def` 内容：configure 层的成品

见 5.3 的 `Targets.def` 与 `TargetMCAs.def`。再补一个 AsmParser 的证据：

```console
$ tail -8 $BUILD/include/llvm/Config/AsmParsers.def
LLVM_ASM_PARSER(X86)
LLVM_ASM_PARSER(AArch64)
LLVM_ASM_PARSER(RISCV)
LLVM_ASM_PARSER(RVTrim)
LLVM_ASM_PARSER(MiniRV)


#undef LLVM_ASM_PARSER
```

MiniRV 在 `AsmParsers.def` 里，因为 `AsmParser/CMakeLists.txt` 存在；它不在 `TargetMCAs.def` 里，因为 `MCA/CMakeLists.txt` 不存在。**目录结构就是功能开关**。

## 反直觉与踩雷

1. **错误直觉**：把名字加进 `LLVM_TARGETS_TO_BUILD` 就够了。
   → **正确认识**：还得在 `LLVM_ALL_TARGETS`（core tier）或 `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD` 里。否则 `llvm/CMakeLists.txt:1167` 直接 `FATAL_ERROR`。前者是“默认构建就包含”，后者是“显式点名才构建”。

2. **错误直觉**：`add_llvm_target(MiniRVCodeGen ...)` 会自动创建一个叫 `MiniRV` 的组件。
   → **正确认识**：它只创建 `LLVMMiniRVCodeGen`。伪组件 `MiniRV` 必须由 `add_llvm_component_group(MiniRV)` 显式创建；否则 `ADD_TO_COMPONENT MiniRV` 会因 TARGET 不存在而失败，MC-only target 的伪组件展开也会在 configure 期报 “Target MiniRV is not in the set of libraries.”。

3. **错误直觉**：`LLVMInitializeAllTargetInfos` 在 `llvm/lib/Support/TargetRegistry.cpp`。
   → **正确认识**：这个文件在当前树里不存在。`InitializeAll*` 是 `llvm/include/llvm/Support/TargetSelect.h` 里的 inline 函数；registry 本体在 `llvm/lib/MC/TargetRegistry.cpp`。改错文件会“看起来改了但没生效”。

4. **错误直觉**：MC-only target 不需要 `LLVMInitializeMiniRVTarget`。
   → **正确认识**：`llvm-mc` 确实不调用它，但 `llc` 的 `InitializeAllTargets()` 会引用它。33d9ec8 因此把它定义成 `TargetInfo + TargetMC` 的 shim。**“llvm-mc 能跑”不能证明“llc 能链接”**。

5. **错误直觉**：`llvm-config --targets-built` 是构建配置的权威来源。
   → **正确认识**：它是编译进二进制的字符串，不重链就会撒谎。本机实测：宏值五个、输出三个。交叉验证用 `Targets.def` / `llc --version`。

6. **错误直觉**：新增 `MCA/` 或 `Disassembler/` 目录没有副作用。
   → **正确认识**：`EXISTS .../CMakeLists.txt` 会让下一轮 configure 把 target 写进对应的 `.def`，进而要求你定义 `LLVMInitialize<T>TargetMCA` / `LLVMInitialize<T>Disassembler`。目录是接口承诺，不是收纳盒。

## 进阶：再往深一层

### 加 target 到已有构建：命令与影响面

```console
# guided：在已有 build 目录上追加（会触发 CMake 重跑）
$ cmake -S $T/llvm -B $BUILD -DLLVM_TARGETS_TO_BUILD="X86;AArch64;RISCV;RVTrim;MiniRV"
$ ninja -C $BUILD llc llvm-mc
```

影响面按顺序：

1. CMake 重跑，五个相关 `.def` 重新生成。
2. 所有 `#include "llvm/Config/Targets.def"` 的翻译单元重编（`TargetSelect.h` 的每个使用者，例如 `llc`、`llvm-mc`、`clang`、`lli`）。
3. 你的 target 库编译（`.td` → `.inc`，`.cpp` → `.so`）。
4. 链接所有调用 `InitializeAll*` 的工具。

`LLVM_LINK_LLVM_DYLIB=ON` 时第 4 步是重链 `libLLVM.so`，然后所有工具重链；`BUILD_SHARED_LIBS=ON` 时则是一串 target 共享库重链。两者都会明显变慢。想减少迭代成本，可以只构建 `llc`/`llvm-mc`，而不是默认 `all`。另外，`BUILD_SHARED_LIBS=ON` 时 `LLVM_EXTERNAL_VISIBILITY` 不是装饰：隐藏可见性会藏掉未标记的 `LLVMInitialize*`，照抄即可。

### `llvm_map_components_to_libnames` 与下游

下游项目 `find_package(LLVM)` 后常用 `llvm_map_components_to_libnames(LLVM_LIBS support core irreader MiniRV)`。这里的 `MiniRV` 就是 5.6 的伪组件，能否展开取决于 group 和 `ADD_TO_COMPONENT` 是否都写了。**这是“LLVM 里能跑、下游 link 不到”的常见根因。**

## 本章重点整理

- target 的“存在”是三层事实：`LLVM_TARGETS_TO_BUILD`（编不编）、`Targets.def`（调不调）、`LLVMInitialize*`（填什么）。
- `LLVM_ALL_TARGETS` 是 core tier 白名单；不在里面就必须走 `LLVM_EXPERIMENTAL_TARGETS_TO_BUILD`，否则 configure `FATAL_ERROR`。
- `add_llvm_component_group` 创建的是 custom target（伪组件），`add_llvm_target` 创建的是 `LLVM<T>CodeGen` 库，两者不能互相替代。
- `.def` 文件由 `configure_file` 生成；`AsmPrinter` 靠 `file(GLOB *AsmPrinter.cpp)`，`AsmParser`/`Disassembler`/`MCA` 靠目录是否存在。
- 初始化函数是 `TargetSelect.h` 里的 inline 展开，registry 本体在 `llvm/lib/MC/TargetRegistry.cpp`，不在 `lib/Support`。
- `llc` 调 `InitializeAllTargets`，`llvm-mc` 不调；这解释了 MC-only target 为什么需要一个 `LLVMInitialize<T>Target` shim。

## 自我检核

- [ ] 不看源码，画出从 `LLVM_ALL_TARGETS` 到 `RegisterTarget` 的完整链，标出哪一步是 configure、哪一步是 link、哪一步是 run。
- [ ] 解释 `add_llvm_component_group(MiniRV)` 与 `add_llvm_target(MiniRVCodeGen ...)` 各自产生什么；如果只写后者会发生什么。
- [ ] `AsmPrinters.def` 与 `AsmParsers.def` 的生成条件有什么不同？为什么 33d9ec8 的 MiniRV 不在前者里？
- [ ] `llc` 和 `llvm-mc` 分别调用哪几个 `InitializeAll*`？由此解释 MC-only shim 的必要性。
- [ ] 复述 `llvm_expand_pseudo_components` 的 MC-only 分支为什么是空体，以及它在哪个提交被删除。
- [ ] 你在本机看到 `llvm-config --targets-built` 与 `CMakeCache.txt` 不一致，如何用两个文件的时间戳证明原因？

## 延伸阅读

- **[LLVM CMake 文档](https://llvm.org/docs/CMake.html)** — LLVM 官方
  - **读哪里**：`LLVM_TARGETS_TO_BUILD`、`LLVM_EXPERIMENTAL_TARGETS_TO_BUILD`、`LLVM_ENABLE_PROJECTS` 三节。
  - **能学到什么**：官方对“core tier / experimental / 默认 all”的定义，以及为什么新后端通常先走 experimental。
  - **前提**：读过本章 5.1；不需要 CMake 专家级知识。

- **[llvm/cmake/modules/AddLLVM.cmake](https://github.com/llvm/llvm-project/blob/main/llvm/cmake/modules/AddLLVM.cmake)** — LLVM 上游源码
  - **读哪里**：`add_llvm_component_group`、`add_llvm_component_library`、`add_llvm_target` 三个函数，以及 `ADD_TO_COMPONENT` 的属性写入。
  - **能学到什么**：组件不是“目录”，是 CMake target + `LLVM_LINK_COMPONENTS` 属性；这是 `llvm-config` 与下游链接行为的根。
  - **前提**：能读 CMake `function`/`set_property`；对照本章 5.2 的 MiniRV CMakeLists 一起看。

- **[llvm/cmake/modules/LLVM-Config.cmake](https://github.com/llvm/llvm-project/blob/main/llvm/cmake/modules/LLVM-Config.cmake)** — LLVM 上游源码
  - **读哪里**：`llvm_expand_pseudo_components` 整个函数，尤其是 `foreach(subcomponent IN ITEMS AsmPrinter AsmParser Desc Disassembler Info Utils)`。
  - **能学到什么**：伪组件展开的真实顺序；为什么“有 CodeGen 库”与“只有 MC 库”要走不同分支。
  - **前提**：读过本章 5.6；最好手边有 33d9ec8 的历史版本对照。

- **[llvm/include/llvm/MC/TargetRegistry.h](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/MC/TargetRegistry.h)** — LLVM 上游源码
  - **读哪里**：`class Target` 的构造函数指针字段、`RegisterTarget`/`RegisterTargetMachine`/`RegisterMC*` 模板。
  - **能学到什么**：registry 不是“名字到对象的 map”，而是“一个 Target 对象 + 一组可空构造函数指针”；`lookupTarget` 只挑 identity，具体能力在用到时才查。
  - **前提**：读过 Ch 04 的 Triple 与 Ch 03 的 MC-only 结构。

- **[Writing an LLVM Backend: Register a Target](https://llvm.org/docs/WritingAnLLVMBackend.html)** — LLVM 官方（偏旧）
  - **读哪里**：`Register a Target` 与 `Target Machine` 两节。
  - **能学到什么**：官方最小注册骨架；对照本章能看出它省略了 `.def` 生成、组件组、MC-only 例外这些工程细节。
  - **前提**：已读本章 5.4/5.5。

→ [Ch 06 最小 TableGen](./06-tablegen-minimum.md)
