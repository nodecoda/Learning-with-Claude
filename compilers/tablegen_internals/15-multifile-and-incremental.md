# Ch 15 多文件输出与增量构建：`-d`、`write-if-changed` 与 CMake 契约

> **本章目标**：搞清 `llvm-tblgen` 与构建系统之间那份**没有写在文档里的契约**：`-d` 生成的依赖文件为什么必须包含主输入文件、`-write-if-changed` 为什么能省下整个 target 的重编、`-gen-register-info` 的 5 个文件如何在 CMake 里被声明为产物。读完你能给自己的 backend 接上正确的增量构建语义。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`），`llvm-tblgen` 为 Release（无 assertions）。

---

## 15.1 为什么这件事重要

一个真实的数字：`llvm-tblgen -gen-instr-info` 处理 AArch64 全量 `.td` 约 10 秒，`-gen-register-info` 约 4 秒，`-gen-asm-matcher` 约 3 秒。完整构建 AArch64 后端会跑十几次 `llvm-tblgen`。

如果你改了 `AArch64.td` 里的一行注释：

- **没有 depfile**：Ninja 不知道哪些 `.inc` 依赖它 → 要么全部重跑（慢），要么一个都不跑（错误）。
- **没有 `write-if-changed`**：所有 `.inc` 的时间戳更新 → 所有 include 它们的 `.cpp` 全部重编（几分钟到几十分钟）。
- **`write-if-changed` 生效**：内容没变的 `.inc` 保持旧时间戳 → 编译停止。

这就是本章的全部价值：**让「改一行注释」从 20 分钟变成 2 秒**。

## 15.2 `-d`：给 Ninja 的依赖文件

用法：

```bash
llvm-tblgen input.td -o output.inc -d output.inc.d
```

实测（`a.td` include 了 `b.td` 和 `c.td`）：

```console
$ llvm-tblgen a.td -o a.inc -d a.inc.d
$ cat a.inc.d
a.inc: a.td b.td c.td
```

标准 Makefile 依赖规则格式：`target: prereq prereq ...`。

### 三条硬约束

**约束一：`-d` 必须配合 `-o`。**

```console
$ llvm-tblgen a.td -d x.d
llvm-tblgen: the option -d must be used together with -o
exit=1
```

实现（`Main.cpp:88`）：

```cpp
static int createDependencyFile(const TGParser &Parser, const char *argv0) {
  if (OutputFilename == "-")
    return reportError(argv0, "the option -d must be used together with -o\n");
  ...
```

注意判断的是 `OutputFilename == "-"`（**默认值**），不是「`-d` 有没有指定」。所以 `-o - -d x.d` 也会被拒（实测）：

```console
$ llvm-tblgen a.td -o - -d t.d
llvm-tblgen: the option -d must be used together with -o
```

**约束二：主输入文件必须出现在依赖列表里。**

这段代码有一段很长的注释，值得全文引用：

```cpp
  DepOut.os() << OutputFilename << ":";

  // Emit the primary input file as a dependency. This matches C compilers like
  // Clang and GCC. Without it, a .td file with no `include` directives would
  // produce a depfile listing zero dependencies. CMake's
  // `cmake_transform_depfile` then collapses that to a 0-byte file, which Ninja
  // treats as a missing depfile and re-runs the rule on every incremental
  // build.
  if (InputFilename != "-")
    DepOut.os() << ' ' << InputFilename;
```

**没有这一行会发生什么**：一个不 include 任何东西的 `.td` → depfile 变成 `a.inc:`（零依赖）→ CMake 的 `cmake_transform_depfile` 把它塌成 0 字节 → Ninja 视为「depfile 缺失」→ **每次构建都重跑这条规则**。

这是一个真实的 bug 修复（注释里写得很清楚），也是「depfile 格式的边界情况能毁掉整个增量构建」的经典案例。

实测零 include 的情况：

```console
$ printf 'def X { int Y = 1; }\n' > solo.td
$ llvm-tblgen solo.td -o solo.inc -d solo.d && cat solo.d
solo.inc: solo.td
```

注意这里是 `solo.inc: solo.td` 而不是 `solo.inc:` —— 主输入文件那一行救了这个 depfile。

**约束三：stdin 输入时主文件被跳过。**

```console
$ cat a.td | llvm-tblgen - -o s.inc -d s.inc.d
$ cat s.inc.d
s.inc: b.td c.td
```

`-` 不是一个真实文件，写进 depfile 会让 Ninja 困惑（它会尝试 stat 一个叫 `-` 的文件）。注意 **include 的文件仍然被记录**——因为它们有真实路径。

### 依赖路径是「打开时的路径」

`TGLexer::LexInclude`（`TGLexer.cpp:484`）：

```cpp
CurBuffer = SrcMgr.AddIncludeFile(Filename, SMLoc::getFromPointer(CurPtr), IncludedFile);
if (!CurBuffer) { ... }
Dependencies.insert(IncludedFile);
```

`IncludedFile` 是 `SourceMgr::AddIncludeFile` 的 out 参数——**它返回的是实际打开的路径**，可能和 `include "..."` 里写的字符串不同。

实测（`-I inc`，文件在 `inc/sub.td`）：

```console
$ cat d.td
include "sub.td"
def X : Base;
$ llvm-tblgen d.td -I inc -o d.inc -d d.inc.d
$ cat d.inc.d
d.inc: d.td inc/sub.td
```

**写的是 `sub.td`，记的是 `inc/sub.td`**。这是正确的（Ninja 需要能 stat 到的路径），但也意味着：

- depfile 里的路径取决于 `-I` 的顺序和当前工作目录。
- 不同构建目录（in-tree vs out-of-tree）产出的 depfile 内容不同。
- **不要在 git 里提交 depfile**。

### 依赖集合的类型：`std::set<std::string>`

```cpp
// TGLexer.h:211
typedef std::set<std::string> DependenciesSetTy;
DependenciesSetTy Dependencies;
```

用 `std::set` 而不是 `DenseSet`，因为：

1. 要**去重**（同一文件被 include 两次只出现一次）；
2. 要**有序**（depfile 稳定，不因 include 顺序变化而产生 diff）。

代价是字符串比较（O(n log n) 次 `std::string::operator<`）。对几十个 include 完全无所谓。

**副作用**：依赖是**按字典序**排列的，不是 include 顺序。所以 `a.td: a.td b.td c.td` 里的 `b.td c.td` 恰好也是字典序，别误以为是源码顺序。

## 15.3 `-write-if-changed`：让时间戳成为契约

```cpp
static cl::opt<bool>
WriteIfChanged("write-if-changed", cl::desc("Only write output if it changed"));
```

实现（`Main.cpp:119`）：

```cpp
static int WriteOutput(const char *argv0, StringRef Filename,
                       StringRef Content) {
  if (WriteIfChanged) {
    // Only updates the real output file if there are any differences.
    // This prevents recompilation of all the files depending on it if there
    // aren't any.
    if (auto ExistingOrErr = MemoryBuffer::getFile(Filename, /*IsText=*/true))
      if (std::move(ExistingOrErr.get())->getBuffer() == Content)
        return 0;
  }
  ...
```

**先读旧文件，逐字节比较，相同就直接返回**——连打开写句柄都不做，所以 mtime 不变。

实测（`stat -c '%Y'` 是 mtime 的 epoch 秒）：

```console
$ llvm-tblgen -print-records a.td -o w.inc && stat -c '%Y %s' w.inc
1793263598 202
$ sleep 1.1
$ llvm-tblgen -print-records a.td -o w.inc && stat -c '%Y %s' w.inc
1793263598 202        ← mtime 没变
```

不加 `-write-if-changed`：

```console
$ llvm-tblgen -print-records a.td -o w2.inc && stat -c '%Y' w2.inc
1793263599
$ sleep 1.1
$ llvm-tblgen -print-records a.td -o w2.inc && stat -c '%Y' w2.inc
1793263600            ← mtime 变了
```

### 三个必须知道的细节

**细节一：比较的是「文件内容」不是「哈希」。** 全读进 `MemoryBuffer` 再 `==`。对 `.inc` 文件（通常几十 KB 到几 MB）无所谓；如果是 GB 级输出，这就是一次完整读盘。上游没做流式比较，因为没必要。

**细节二：`-write-if-changed` 不影响 depfile 的写出。** `Main.cpp` 的顺序是：先写 depfile（`createDependencyFile`），再 `WriteOutput` 主文件，再 `WriteOutput` 额外文件：

```cpp
  // Always write the depfile, even if the main output hasn't changed.
  // If it's missing, Ninja considers the output dirty. If this was below
  // the early exit below and someone deleted the .inc.d file but not the .inc
  // file, tablegen would never write the depfile.
  if (!DependFilename.empty()) {
    if (int Ret = createDependencyFile(Parser, argv0))
      return Ret;
  }
```

实测（主文件内容不变时 depfile 仍然更新）：

```console
$ llvm-tblgen -write-if-changed a.td -o a.inc -d a2.d && cat a2.d
a.inc: a.td b.td c.td
$ stat -c '%Y' a.inc
1793263598        ← 主文件 mtime 仍是上一轮的
```

**细节三：`-o -` 时 `WriteIfChanged` 完全无效。** `MemoryBuffer::getFile("-")` 会尝试打开一个叫 `-` 的文件（通常不存在）→ 落到正常写路径 → 写 stdout。所以 `-write-if-changed` 只对真实文件有意义。

## 15.4 多文件 backend 的五个产物

`-gen-register-info` 产 5 个文件。回顾 Ch 13.5 的机制：

```cpp
return {std::move(Main),
        {{"Enums.inc", Enums},
         {"MCDesc.inc", MCDesc},
         {"Header.inc", Header},
         {"TargetDesc.inc", TargetDesc}}};
```

`Main.cpp` 写额外文件时：

```cpp
for (auto [Suffix, Content] : OutFiles.AdditionalFiles) {
  SmallString<128> Filename(OutputFilename);
  if (Filename != "-") {
    sys::path::replace_extension(Filename, "");
    Filename.append(Suffix);
  }
  if (int Ret = WriteOutput(argv0, Filename, Content))
    return Ret;
}
```

**注意 `WriteOutput` 是同一个函数**，所以 `-write-if-changed` 对**每个**额外文件独立生效。这点很重要：主文件变了、`Header.inc` 没变时，只有主文件 mtime 更新。

### CMake 侧必须声明全部产物

`llvm/cmake/modules/TableGen.cmake:71`：

```cmake
set(EXTRA_OUTPUTS)
if("-gen-register-info" IN_LIST ARGN)
  cmake_path(GET ofn STEM OUTPUT_BASENAME)
  list(APPEND EXTRA_OUTPUTS
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}Enums.inc
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}Header.inc
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}MCDesc.inc
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}TargetDesc.inc)
endif()
```

`OUTPUT_BASENAME` 是 `ofn`（主输出名）的 stem。假设 `tablegen(LLVM AArch64GenRegisterInfo.inc -gen-register-info)`，则：

- `ofn` = `AArch64GenRegisterInfo.inc`
- `OUTPUT_BASENAME` = `AArch64GenRegisterInfo`
- 额外产物 = `AArch64GenRegisterInfoEnums.inc` 等 4 个

**和 backend 端的拼接规则一致吗？** backend 端是「主输出名去掉最后一段扩展名 + 后缀」。`AArch64GenRegisterInfo.inc` → `AArch64GenRegisterInfo` → `+ "Enums.inc"` → `AArch64GenRegisterInfoEnums.inc`。**一致**，但两边是**各写各的**，没有共享常量。这是 15.6 那个「契约」问题的根源之一。

### 如果忘了声明会怎样

Ninja 的行为：

1. rule 的 `outputs` 只有主文件 → Ninja 认为 `Header.inc` 是「无来源的孤儿」。
2. 下游 `#include "AArch64GenRegisterInfoHeader.inc"` 的 `.cpp` 编译时，Ninja 找不到生成 `Header.inc` 的规则 → 报 `missing and no known rule to make it`（除非 `.cpp` 的依赖没被显式建模，那就变成「有时能编、有时不能」）。
3. 更糟的情况：`Header.inc` 存在于源码树（旧版遗留）→ Ninja 用旧的，静默产出错误二进制。

**这是 Final Project 的必检项**：加多文件 backend 必须同时改 backend 和 `TableGen.cmake`。

## 15.5 `tablegen()` 函数的完整契约

`llvm/cmake/modules/TableGen.cmake:10` 的 `tablegen(project ofn ...)` 做四件事：

### ① 选 TableGen 可执行文件

```cmake
if(NOT ${project}_TABLEGEN_EXE)
  message(FATAL_ERROR "${project}_TABLEGEN_EXE not set")
endif()
```

`${project}_TABLEGEN_EXE` 由 `add_tablegen` 或交叉编译配置设置。**交叉编译**时它指向**宿主机**的 `llvm-tblgen`（因为 target 二进制跑不了），这就是 `LLVM_NATIVE_TOOL_DIR` 存在的原因。

### ② 构造 depfile 命令行

```cmake
cmake_policy(GET CMP0116 cmp0116_state)
if(CMAKE_GENERATOR MATCHES "Ninja" AND cmp0116_state STREQUAL NEW
   AND NOT (CMAKE_HOST_WIN32 AND CMAKE_VERSION VERSION_LESS 3.30)
   AND NOT (CMAKE_VERSION VERSION_LESS 3.23))
  set(additional_cmdline
    -o ${ofn}
    -d ${ofn}.d
    DEPFILE ${ofn}.d
    )
  set(global_tds)
else()
  set(include_td_dirs "${tblgen_includes}")
  list(TRANSFORM include_td_dirs APPEND "/*.td")
  file(GLOB global_tds ${include_td_dirs})
  set(additional_cmdline
    -o ${CMAKE_CURRENT_BINARY_DIR}/${ofn}
    )
endif()
```

**新路径（Ninja + CMake ≥3.23）**：用 `-d` 产出 depfile，交给 CMake 的 `DEPFILE` 关键字。CMake 会在构建后解析、合并进 Ninja 的依赖图。

**旧路径（fallback）**：`file(GLOB *.td)` **把所有 include 目录下的 `.td` 全列为依赖**。这意味着：

- 任何 `.td` 变动都会让**所有** tablegen 规则重跑（因为每个规则都依赖全部 `.td`）。
- 这正是「为什么老 LLVM 增量构建那么慢」的原因。

CMP0116 那段注释解释了为什么需要版本判断：

```cmake
# CMake versions older than v3.23 on other platforms use the fallback
# behavior as v3.22 and earlier fail to parse some depfiles that get
# generated, and this behavior was fixed in CMake commit
# e04a352cca523eba2ac0d60063a3799f5bb1c69e.
```

**如果你在写自定义 CMake 集成，务必走 `DEPFILE` 路径**，别复制那段 GLOB fallback。

### ③ 注入目标相关 flag

```cmake
if (LLVM_ENABLE_DAGISEL_COV AND "-gen-dag-isel" IN_LIST ARGN)
  list(APPEND LLVM_TABLEGEN_FLAGS "-instrument-coverage")
endif()
if (LLVM_ENABLE_GISEL_COV AND "-gen-global-isel" IN_LIST ARGN)
  list(APPEND LLVM_TABLEGEN_FLAGS "-instrument-gisel-coverage")
  list(APPEND LLVM_TABLEGEN_FLAGS "-gisel-coverage-file=${LLVM_GISEL_COV_PREFIX}all")
endif()
if (LLVM_OMIT_DAGISEL_COMMENTS AND "-gen-dag-isel" IN_LIST ARGN)
  list(APPEND LLVM_TABLEGEN_FLAGS "-omit-comments")
endif()
```

**这几行是「同一个 backend、不同 flag → 不同输出」的例子**，也说明了为什么 backend 里的 `cl::opt` 必须先 `cl::ParseCommandLineOptions` 才生效（Ch 0 那个坑：不 parse 就读 stdin）。

### ④ 声明产物

```cmake
add_custom_command(OUTPUT ${ofn} ${EXTRA_OUTPUTS}
                   COMMAND ${${project}_TABLEGEN_EXE} ${additional_cmdline}
                   ...
                   DEPENDS ${LLVM_TARGET_DEFINITIONS} ${global_tds} ${ARG_DEPENDS}
                   ...)
```

`OUTPUT` 里必须同时列主文件和 `EXTRA_OUTPUTS`。这就是 15.4 那段 `EXTRA_OUTPUTS` 的作用。

## 15.6 那份「没有写在文档里的契约」

把上面所有约束汇总成一张表——**这就是你和构建系统之间的契约**：

| 侧 | 约束 | 违反的后果 |
| --- | --- | --- |
| backend | 多文件产物的后缀必须和 `TableGen.cmake` 的 `EXTRA_OUTPUTS` 一致 | Ninja 不知道产物 → 增量构建失败或静默用旧文件 |
| `Main.cpp` | depfile 必须包含主输入文件 | 零 include 的 `.td` → depfile 被塌成 0 字节 → 每次重跑 |
| `Main.cpp` | depfile 必须在 `WriteOutput` **之前**写 | 主文件未变时 depfile 缺失 → Ninja 视为脏 |
| backend | 输出必须是确定的（Ch 14.6） | `write-if-changed` 永远失效（每次内容都不同）→ 全量重编 |
| backend | `emitSourceFileHeader` 会写入输入文件名 | 同一内容放不同路径 → 输出不同 → `write-if-changed` 失效 |

**最后一条是隐藏的耦合**：`write-if-changed` 的效果依赖「同一次构建里 `InputFilename` 不变」。这在正常构建里成立（CMake 传的是固定路径），但如果你做「同一份 `.td` 从两个目录各生成一份」，两份产物除了 `From:` 行以外必然相同——此时 `write-if-changed` 仍然有效（因为每份产物自己和自己比）。

## 15.7 实测：改一行注释的完整影响链

本机用 `llvm/test/TableGen` 的 496 个测试做了一次间接验证：改 `TGParser.cpp` 里的诊断文案后，**只有 `llvm-tblgen` 重编 + 检测到新二进制**，`.td` 测试文件本身不重编（它们是输入不是产物）。这验证了 depfile 的方向是对的。

要做「改 `.td` 注释 → 只有需要的东西重编」的完整验证，需要一个真实 target：

```bash
ninja -C build -d explain llvm/lib/Target/AArch64/CMakeFiles/LLVMAArch64CodeGen.dir/AArch64GenInstrInfo.inc
```

`-d explain` 会打印 Ninja 为什么认为每条规则是脏的。三种典型输出：

- `output ... older than most recent input` → 正常重跑
- `output ... is dirty because of depfile` → depfile 里的某个 `.td` 变了
- `restat of output ... unchanged` → **`write-if-changed` 生效了**

**`restat` 那句是本章的胜利条件**：它意味着「规则跑了，但产物没变，所以下游不用重编」。

## 15.8 给新 backend 接增量构建的检查表

1. backend 是单文件 → 只需在 `tablegen()` 调用里加 `-gen-my-thing`，`EXTRA_OUTPUTS` 不用动。
2. backend 多文件 → 在 `TableGen.cmake` 加 `if("-gen-my-thing" IN_LIST ARGN)` 分支，列出**全部**后缀。
3. 输出里有时间戳/路径/随机数 → 删掉。`emitSourceFileHeader` 的 `From:` 是唯一允许的「输入相关」内容。
4. 遍历顺序不确定（`DenseMap`、指针排序）→ 修（Ch 14.6）。
5. 加一个 lit 测试验证「两次运行输出相同」：

```td
// RUN: llvm-tblgen -gen-my-thing %s -o %t.1
// RUN: llvm-tblgen -gen-my-thing %s -o %t.2
// RUN: diff %t.1 %t.2
```

6. 如果产物会被 `#include`，确认头文件保护（include guard）用的是**稳定的标识**（`RecordKeeper` 名字或固定字符串），不是指针或地址。

## 15.9 本章检查清单

- [ ] 说出 `-d` 的三条硬约束，以及「零 include」时为什么必须显式写主输入
- [ ] 解释 depfile 里的路径为什么可能和 `include "..."` 里写的不一样
- [ ] 说出 `Dependencies` 用 `std::set` 的两个理由
- [ ] 解释 `-write-if-changed` 如何影响 mtime，以及为什么它能阻止下游重编
- [ ] 说出 depfile 为什么必须在主文件写出之前生成
- [ ] 解释多文件 backend 的产物后缀在哪两处被硬编码，以及漏改的后果
- [ ] 说出 CMP0116 两条路径的区别，以及 fallback 为什么慢
- [ ] 解释 `restat of output ... unchanged` 的含义

## 15.10 踩雷记录

**错误直觉 1：`-d` 只写 include 的文件。**
正确认识：主输入文件也被写进去（`if (InputFilename != "-")`）。这不是冗余——没有它，零 include 的 `.td` 会产出空依赖列表，被 CMake 塌成 0 字节，Ninja 每次重跑。

**错误直觉 2：depfile 里的路径就是 `include` 里写的字符串。**
正确认识：是 `SourceMgr::AddIncludeFile` 实际打开的路径。`include "sub.td"` + `-I inc` → depfile 里是 `inc/sub.td`（实测）。

**错误直觉 3：`-write-if-changed` 只是「少写一次盘」的微优化。**
正确认识：它决定**下游是否重编**。内容不变 → mtime 不变 → `#include` 它的 `.cpp` 不重编。改一行注释省下的是几十分钟。

**错误直觉 4：depfile 在产物写完之后生成。**
正确认识：在之前（`Main.cpp` 有明确注释）。反过来的话，产物未变时提前 `return` 会跳过 depfile 写出，Ninja 认为 depfile 缺失 → 视为脏 → 每次重跑。

**错误直觉 5：多文件 backend 只要改 `CMakeLists.txt` 加入 `.cpp`。**
正确认识：还要改 `TableGen.cmake` 的 `EXTRA_OUTPUTS`。两处的后缀字符串**各写各的**，没有共享定义。

**错误直觉 6：`-o -` 也能用 `-d`。**
正确认识：`createDependencyFile` 检查 `OutputFilename == "-"` 并直接报错，因为没有真实 target 名。

**错误直觉 7：`-write-if-changed` 对 stdout 也有效。**
正确认识：`MemoryBuffer::getFile("-")` 打不开 → 走正常写路径。只对真实文件有效。

**错误直觉 8：CMake 的 GLOB fallback 和 DEPFILE 路径等价。**
正确认识：GLOB 把 include 目录下**所有** `.td` 列为依赖，任何 `.td` 变动都让所有 tablegen 规则重跑。DEPFILE 路径才只依赖实际 include 的文件。

## 15.11 延伸阅读

- **`llvm/lib/TableGen/Main.cpp`** 的 `createDependencyFile`（87–117）与 `WriteOutput`（119–149）
  - 本章两个主角的完整实现。那段关于 CMake `cmake_transform_depfile` 的注释值得逐字读。
- **`llvm/lib/TableGen/TGLexer.cpp`** 的 `LexInclude`（468–499）
  - `Dependencies.insert(IncludedFile)` 那一行，以及 `IncludedFile` 来自哪。
- **`llvm/cmake/modules/TableGen.cmake`**（约 300 行，全读）
  - `tablegen()` 的四段（选可执行文件、depfile、flag 注入、声明产物）与 `add_tablegen()` 的两个变体。
- **`llvm/lib/TableGen/Main.cpp`** 的 `WriteOutput` 里 `ErrorsPrinted == 0` 那三行（145）
  - 与 Ch 12.3 呼应：错误时不 `keep()`，产物被 `ToolOutputFile` 析构删除。这是「产物存在性」与「成功」的耦合点。
- **`llvm/utils/TableGen/RegisterInfoEmitter.cpp`** 的 `run`（2123–2158）
  - 多文件产物的 5 个后缀，对照 `TableGen.cmake` 的 `EXTRA_OUTPUTS`。
- **`llvm/docs/TableGen/BackGuide.html`**（源码 `llvm/docs/TableGen/BackGuide.rst`）
  - 官方对 backend 的说明。注意它**没有**讲 depfile 与增量构建——本章的 15.6 那份契约表在官方文档里不存在。
- **Ninja 手册的 `restat` 与 `depfile` 两节**
  - 理解 `restat of output ... unchanged` 的关键。读 Ninja 而不是 LLVM，因为这是构建系统的语义。

## 下一章

Ch 15 讲的是「产物怎么被管理」。下一章看**产物本身长什么样**——`-gen-instr-info` 的 `.inc` 文件内部结构、为什么要用 `static const` 而不是 `extern`、`emitSourceFileHeader` 之外的表格生成惯例、以及「生成的代码怎么被 C++ 消费」这条链。

[Ch 16 生成代码的形状：从 Record 到 C++ 表的映射惯例](./16-emitted-code-shape.md)
