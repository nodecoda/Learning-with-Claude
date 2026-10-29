# Ch 13 Backend 注册机制：`TableGen::Emitter::Opt` 与静态初始化

> **本章目标**：搞清 `-gen-instr-info` 这个命令行选项是**谁在什么时候**注册到 `cl::opt` 里的；看明白 `Opt`/`OptClass`/`MultiFileOptClass` 三层封装各自负责什么；理解为什么两个 `Opt` 用同名会 `Aborted`、为什么 `TableGenMain(argv[0], nullptr)` 编不过、为什么多文件 backend 的 `-o -` 行为和单文件不同。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`），`llvm-tblgen` 为 Release（无 assertions）。本章所有 C++ 实验都用 Ch 0 的手动链接命令编译，可独立运行。

---

## 13.1 「注册」到底注册了什么

`llvm-tblgen --help` 里那一大串 `-gen-*`，一个也不是在 `main` 里手写的。`llvm/utils/TableGen/Basic/TableGen.cpp` 的 `tblgen_main` 只有这么点内容：

```cpp
int tblgen_main(int argc, char **argv) {
  InitLLVM X(argc, argv);
  cl::ParseCommandLineOptions(argc, argv);

  MultiFileTableGenMainFn MainFn = nullptr;
  return TableGenMain(argv[0], MainFn);
}
```

它只注册了 6 个「测试用」backend（`print-records`、`print-detailed-records`、`null-backend`、`dump-json`、`print-enums`、`print-sets`），其余 40 多个全在各自的 `.cpp` 末尾用**全局对象的构造函数**注册：

```cpp
// llvm/utils/TableGen/InstrInfoEmitter.cpp:1485
static TableGen::Emitter::OptClass<InstrInfoEmitter>
    X("gen-instr-info", "Generate instruction descriptions");
```

`X` 是一个 **namespace 作用域的静态对象**。程序启动时（`main` 之前）它的构造函数跑一次，把 `"gen-instr-info"` 这个名字和回调塞进 `cl::opt<FnT>` 的 parser。这就是全部机制——**没有注册表、没有宏表、没有显式 init 调用**。

为什么能这样：C++ 保证同一翻译单元内（以及同 TU 内跨 TU 的静态初始化顺序问题被 `ManagedStatic` 绕开，见 13.4）。而 `add_tablegen(llvm-tblgen ...)` 把所有 backend 的 `.cpp` 编进同一个可执行文件，所以链接器会把它们全保留下来（`static` 变量在目标文件里，只要目标文件被链接就存在）。

实测（自写 backend，`reg`）：

```cpp
namespace {
struct Loud {
  Loud() { errs() << "[Loud ctor] ran\n"; }
};
Loud GlobalLoud;
TableGen::Emitter::Opt Reg("my-test-backend", emit, "test description");
}
```

```console
$ ./reg --my-test-backend c.td
[Loud ctor] ran          ← 早于 main
[main] reached
my-test-backend ran, defs=1
exit=0
```

```console
$ ./reg --help-list-hidden | grep my-test
      --my-test-backend                  - test description
```

注册是**静态的、启动期的、无条件的**。这也意味着：**你不能在运行时「按需」注册 backend**，除非改 `TableGenBackend.cpp` 的机制。

## 13.2 三层封装

`TableGenBackend.h` 里的 `FnT` 是一个「二选一」的函数指针容器：

```cpp
struct FnT {
  using SingleFileGeneratorType = void(const RecordKeeper &Records, raw_ostream &OS);
  using MultiFileGeneratorType = TableGenOutputFiles(StringRef FilenamePrefix,
                                                     const RecordKeeper &Records);

  SingleFileGeneratorType *SingleFileGenerator = nullptr;
  MultiFileGeneratorType *MultiFileGenerator = nullptr;

  FnT(SingleFileGeneratorType *Gen) : SingleFileGenerator(Gen) {}
  FnT(MultiFileGeneratorType *Gen) : MultiFileGenerator(Gen) {}
};
```

注意**两个成员指针**而不是 `variant`/`union` + tag：它靠「哪个非空」区分。因为两者默认都是 `nullptr`，所以「两个都空」也是一种合法状态（未选中任何 backend 时）。

三层封装：

| 层 | 签名 | 用途 | 上游使用数 |
| --- | --- | --- | --- |
| `Opt` | `(Name, FnT, Desc, ByDefault=false)` | 最底层，接受任意 `FnT` | 11 |
| `OptClass<Emitter>` | `(Name, Desc)` | 包装 `EmitterC(RK).run(OS)` | 34 |
| `MultiFileOptClass<Emitter>` | `(Name, Desc)` | 包装 `EmitterC(RK).run(FilenamePrefix)` | 1（`RegisterInfoEmitter`） |

（数字是本 commit 实测 `grep` 结果。）

`OptClass` 的实现值得逐行看：

```cpp
template <class EmitterC> class OptClass : Opt {
  static TableGenOutputFiles run(StringRef /*FilenamePrefix*/,
                                 const RecordKeeper &RK) {
    std::string S;
    raw_string_ostream OS(S);
    EmitterC(RK).run(OS);
    return {std::move(S), {}};
  }
public:
  OptClass(StringRef Name, StringRef Desc) : Opt(Name, run, Desc) {}
};
```

三个细节：

1. **`static run` 是私有的、且不是成员**：它只负责适配签名。`EmitterC(RK).run(OS)` 是上游所有 backend 类的统一形状（`InstrInfoEmitter`、`AsmWriterEmitter`……）。
2. **输出先进 `std::string`**，不是直接写 `OS`。这是 `TableGenOutputFiles` 结构的要求——`Main.cpp` 需要「先拿到全部内容，判断有没有错误，再决定是否写盘」（Ch 12.3 的 `OutFile.keep()`）。
3. **`EmitterC` 必须是「构造时接 `RecordKeeper`、`run(raw_ostream&)`」的形状**。所以 `OptClass<void>` 不合法（`EmitterC(RK)` 是 void 表达式），实测编译错误是 `member reference base type 'void' is not a structure or union`。

实测两种写法等价（`reg2`）：

```cpp
struct MyEmitter { const RecordKeeper &RK; ...; void run(raw_ostream &OS); };
TableGen::Emitter::OptClass<MyEmitter> A("gen-a", "class-based");

static void emitB(const RecordKeeper &RK, raw_ostream &OS) { OS << "Opt(fn) ran\n"; }
TableGen::Emitter::Opt B("gen-b", emitB, "function-based");
```

```console
$ ./reg2 --gen-a c.td
OptClass ran, 1
$ ./reg2 --gen-b c.td
Opt(fn) ran
$ ./reg2 --help-list-hidden | grep -E "gen-a|gen-b"
      --gen-a                            - class-based
      --gen-b                            - function-based
```

**注意 `emitB` 的参数顺序**：`Opt` 的 `FnT` 要求 `(const RecordKeeper &, raw_ostream &)`，而 `TableGenMainFn` 是 `(raw_ostream &, const RecordKeeper &)`——**顺序相反**。写反了编译错误长这样（实测）：

```console
Reg3.cpp:8:31: error: no matching constructor for initialization of 'TableGen::Emitter::Opt'
    8 | static TableGen::Emitter::Opt O1("gen-dup", emitA, "first");
      |                               ^  ~~~~~~~~~~~~~~~~~~~~~~~~~
note: candidate constructor not viable: no known conversion from
      'bool (raw_ostream &, const RecordKeeper &)' to 'FnT' for 2nd argument
```

这个顺序差异是有历史的：老的 backend API 是 `EmitFoo(RK, OS)`，`TableGenMainFn` 是后来为「一个 backend 一个文件」的简单场景加的，参数顺序跟了 `main(OS, Records)` 的感觉。**新写 backend 一律用 `Opt`/`OptClass` 的形状（`RK` 在前）。**

## 13.3 只能有一个 backend 被应用

`ApplyCallback` 只读**一个**值：

```cpp
bool llvm::TableGen::Emitter::ApplyCallback(const RecordKeeper &Records,
                                           TableGenOutputFiles &OutFiles,
                                           StringRef FilenamePrefix) {
  FnT Fn = CallbackFunction->getValue();
  if (Fn.SingleFileGenerator) { ...; return false; }
  if (Fn.MultiFileGenerator)  { ...; return false; }
  return true;
}
```

`CallbackFunction` 是一个 `cl::opt<FnT>`。`cl::opt<T>::getValue()` 返回**当前值**，而 `cl::opt` 的语义是「最后一次赋值生效」。所以：

- 传两个 `--gen-a --gen-b` → **后者赢**（实测 `second`）。
- 传 `--gen-a --gen-a` → `first`（同一个值，无所谓）。
- 传 `--gen-b --gen-a` → `a`（**实测确认是 `first`，即最后一个**）。

实测（`twob`）：

```console
$ ./twob --gen-first --gen-second c.td
second
$ ./twob --gen-second --gen-first c.td
first
```

**没有「链式执行」**。想一次生成多个文件，只能选 `MultiFileOptClass`（13.5）。

顺带一个 `cl::opt` 的坑：**同名注册会直接 abort**（实测）：

```cpp
static TableGen::Emitter::Opt O1("gen-dup", emitA, "first");
static TableGen::Emitter::Opt O2("gen-dup", emitB, "second");
```

```console
$ ./reg3 --gen-dup c.td
: CommandLine Error: Option 'gen-dup' registered more than once!
LLVM ERROR: inconsistency in registered CommandLine options
Aborted (core dumped)  (exit 134)
```

而且这发生在 `main` 里 `cl::ParseCommandLineOptions` 那一刻，不是链接时。所以**两个 backend 重名，只要有人跑这个二进制就会炸**。上游 46 处注册名互不重复，靠的是人（没有编译期检查）。这算一个真实的架构弱点，写新 backend 时别起名撞车。

## 13.4 `ManagedStatic` 在这里解决什么问题

`TableGenBackend.cpp` 的注册表是三行：

```cpp
namespace {
struct OptCreatorT {
  static void *call() { return new cl::opt<FnT>(cl::desc("Action to perform:")); }
};
} // namespace

static ManagedStatic<cl::opt<FnT>, OptCreatorT> CallbackFunction;

Opt::Opt(StringRef Name, FnT CB, StringRef Desc, bool ByDefault) {
  if (ByDefault)
    CallbackFunction->setInitialValue(CB);
  CallbackFunction->getParser().addLiteralOption(Name, CB, Desc);
}
```

`ManagedStatic<T, Creator>` 是「懒初始化 + 可销毁」的全局对象。它解决的是 **static initialization order fiasco**：

- 46 个 `Opt` 分布在 30 多个 `.cpp` 里，每个都是 namespace-scope 静态对象。
- 它们都要访问 `CallbackFunction`。
- C++ 不保证跨 TU 的初始化顺序。

如果 `CallbackFunction` 是裸的 `cl::opt<FnT>`，某个 `Opt` 的构造函数可能在它构造之前跑，`getParser()` 就操作了未初始化的对象。

`ManagedStatic` 的做法是：`operator->` 第一次被解引用时（通过 `Creator::call()`）才 `new`。所以**谁先访问谁触发构造**，顺序问题消失。

代价是：

- 需要 `OptCreatorT` 这个 helper 类（因为 `cl::opt<FnT>` 没有默认构造，`ManagedStatic` 需要知道怎么造）。
- 需要给 `FnT` 写一个 `cl::OptionValue<FnT>` 特化（`TableGenBackend.cpp:36`），否则 `cl::opt<FnT>` 编不过——`OptionValue` 默认实现要求 `T` 可默认构造、可拷贝、可比较，而 `FnT` 的「值」其实是两个函数指针。注释写得很清楚：

  ```cpp
  // CommandLine options of class type are not directly supported with some
  // specific exceptions like std::string ... the `FnT` function_ref object is
  // also safe to copy.
  ```

`ManagedStatic` 还有个副作用：**销毁时机在 `llvm_shutdown()`**。所以如果你的 backend 依赖「进程退出时 registry 还在」，只要不调 `llvm_shutdown` 就没问题（`llvm-tblgen` 不调）。

## 13.5 `MultiFileOptClass`：一个 backend 写多个文件

`RegisterInfoEmitter` 需要产 5 个文件（主文件 + `Enums.inc`/`Header.inc`/`MCDesc.inc`/`TargetDesc.inc`）。它的 `run` 返回：

```cpp
TableGenOutputFiles RegisterInfoEmitter::run(StringRef FilenamePrefix) {
  ...
  return {std::move(Main),
          {{"Enums.inc", Enums},
           {"MCDesc.inc", MCDesc},
           {"Header.inc", Header},
           {"TargetDesc.inc", TargetDesc}}};
}
```

`Main.cpp` 写这些额外文件的方式（`Main.cpp:210`）：

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

关键：**`replace_extension("")` 先去掉 `.inc`，再 `append(Suffix)`**。所以 `-o AArch64GenRegisterInfo.inc` + 后缀 `Enums.inc` 得到 `AArch64GenRegisterInfoEnums.inc`。

实测（自写 `MultiFileOptClass`，`mf`）：

```console
$ ./mf --gen-mf c.td -o AArch64GenRegisterInfo.inc
$ ls
AArch64GenRegisterInfo.extra.h
AArch64GenRegisterInfo.impl.inc
AArch64GenRegisterInfo.inc
$ cat AArch64GenRegisterInfo.inc
main body for AArch64GenRegisterInfo
```

**三个可观察行为**：

1. **`FilenamePrefix` 是 `sys::path::stem(OutputFilename)`**（`Main.cpp:194`），即去掉目录和扩展名。所以 backend 能拿 `AArch64GenRegisterInfo` 去拼 include guard、命名空间。实测 `main body for AArch64GenRegisterInfo` 就是这个值。
2. **`AdditionalFiles` 是 `std::map<StringRef, std::string>`**——**按 key 排序**输出，不是插入顺序。
3. **`-o -` 时后缀被忽略，全部内容串联到 stdout**（实测）：

   ```console
   $ ./mf --gen-mf c.td -o -
   main body for -
   extra header
   impl body
   ```

   注意 `FilenamePrefix` 变成了 `"-"`（stem 的 `-`），而额外文件没有文件名可拼，直接按 map 顺序追加。源码里那个 `TODO: Format using the split-file convention when writing to stdout?` 就是承认这里还没设计好。

**CMake 侧必须同步**：`llvm/cmake/modules/TableGen.cmake:71` 硬编码了 `-gen-register-info` 的四个额外输出：

```cmake
if("-gen-register-info" IN_LIST ARGN)
  cmake_path(GET ofn STEM OUTPUT_BASENAME)
  list(APPEND EXTRA_OUTPUTS
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}Enums.inc
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}Header.inc
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}MCDesc.inc
       ${CMAKE_CURRENT_BINARY_DIR}/${OUTPUT_BASENAME}TargetDesc.inc)
endif()
```

**如果你加一个多文件 backend，必须同时改这里**，否则 Ninja 不知道那些文件是产物，增量构建会漏。这是 Final Project 的必做项，也是 PR review 最容易被挑的地方。

## 13.6 `ByDefault` 与「没选 backend」

`Opt` 的第四个参数：

```cpp
{"print-records", printRecords, "Print all records to stdout (default)", true},
```

`ByDefault=true` 时调 `CallbackFunction->setInitialValue(CB)`，于是**不传任何 `-gen-*` 也有 backend 可跑**。实测（`bydef`）：

```console
$ ./bydef c.td
default backend
$ ./bydef --gen-explicit c.td
explicit backend
```

`--help-list-hidden` 里**看不出**哪个是 default（实测两个条目长得一样）。这是 `cl::opt` 的限制，不是 TableGen 的。

**如果你忘了给任何 backend 设 `ByDefault`，且用户没传 flag**，`ApplyCallback` 会返回 `true`，然后 `TableGenMain` 走 `MainFn`：

```cpp
if (TableGen::Emitter::ApplyCallback(Records, OutFiles, FilenamePrefix))
  status = MainFn ? MainFn(OutFiles, Records) : 1;
```

`MainFn` 是 `nullptr` 时 `status = 1`，**静默退出码 1，没有任何消息**。实测：

```console
$ ./reg c.td          # reg 只注册了 --my-test-backend，没设 ByDefault
[Loud ctor] ran
[main] reached
exit=1
```

一个字节的提示都没有。这是「写 backend 时忘了设 ByDefault」最坑的表现形式。

## 13.7 `TableGenMain(argv[0], nullptr)` 编不过

想写一个「只有 backend、没有 fallback」的工具，直觉写法是：

```cpp
return TableGenMain(argv[0], nullptr);
```

实测编译错误：

```console
error: call to 'TableGenMain' is ambiguous
note: candidate function
  int TableGenMain(const char *argv0, TableGenMainFn MainFn = nullptr);
note: candidate function
  int TableGenMain(const char *argv0, MultiFileTableGenMainFn MainFn = nullptr);
```

因为两个重载的第二个参数都有默认值 `= nullptr`，`nullptr` 本身两个都能转。正确写法是显式指定：

```cpp
return TableGenMain(argv[0], MultiFileTableGenMainFn(nullptr));
```

或者干脆用单文件版本（`TableGenMainFn`）——`clang` 会选第一个吗？**不会，仍然二义**。上游 `TableGen.cpp` 避开了这个问题，因为它写的是：

```cpp
MultiFileTableGenMainFn MainFn = nullptr;
return TableGenMain(argv[0], MainFn);
```

**先声明成具体类型的变量**，二义就消失了。这是上游代码里一个不显眼但必要的小技巧——如果你重构那段代码时把它「简化」成直接传 `nullptr`，就会编不过。

## 13.8 从「加一个 backend」到「构建系统」

完整链路（Final Project 会走一遍）：

1. 写 `llvm/utils/TableGen/MyEmitter.cpp`，末尾 `static TableGen::Emitter::OptClass<MyEmitter> X("gen-my-thing", "...");`
2. 在 `llvm/utils/TableGen/CMakeLists.txt` 的 `add_tablegen(llvm-tblgen LLVM ...)` 源文件列表里加 `MyEmitter.cpp`（**按字母序**，上游风格）。
3. 如果产出多个文件 → 在 `llvm/cmake/modules/TableGen.cmake` 的 `EXTRA_OUTPUTS` 里加分支。
4. 写 `llvm/test/TableGen/my-thing.td`，`RUN: llvm-tblgen -gen-my-thing %s | FileCheck %s`。
5. 若某个 target 要用它 → 在 `llvm/lib/Target/X/CMakeLists.txt` 里加 `tablegen(LLVM XGenMyThing.inc -gen-my-thing)`。

第 2 步最容易漏，漏了的表现是「编译通过、`--help` 里没有、`-gen-my-thing` 报 unknown argument」。

顺带注意 `add_tablegen` 的两个变体（`TableGen.cmake:192`）：

- `add_tablegen(llvm-min-tblgen LLVM_HEADERS ...)`：**只含 `LLVMTableGenBasic` 的 object library**，用于在构建 LLVM 自身头文件时打破「需要 tblgen 才能编 tblgen」的循环依赖。
- `add_tablegen(llvm-tblgen LLVM ...)`：全部 backend + `LLVMTableGenCommon`。

`CMakeLists.txt` 顶部那段注释就是在解释这件事：

```cmake
# llvm-min-tablegen only contains a subset of backends necessary to
# build llvm/include. It must not depend on TableGenCommon, as
# TableGenCommon depends on this already to generate things such as
# ValueType definitions.
```

**如果你新加的 backend 被 `LLVMTableGenBasic` 里的某个生成步骤依赖，就必须放进 `Basic/` 而不是顶层。** 这是 bootstrap 顺序问题，不是目录偏好。

## 13.9 本章检查清单

- [ ] 说出 `-gen-instr-info` 是在什么时机、由什么代码注册的
- [ ] 解释 `FnT` 为什么用两个函数指针而不是 `variant`
- [ ] 说出 `Opt`/`OptClass`/`MultiFileOptClass` 各自的使用数与适用场景
- [ ] 解释为什么 `ManagedStatic` 在这里是必需的，以及它带来的两个额外代码量
- [ ] 说出传两个 `-gen-*` 时哪个生效，以及同名注册的后果
- [ ] 解释多文件 backend 的后缀拼接规则，以及 `-o -` 时的特殊行为
- [ ] 说出加一个多文件 backend 时必须同步修改的 CMake 位置
- [ ] 解释 `TableGenMain(argv[0], nullptr)` 为什么编不过

## 13.10 踩雷记录

**错误直觉 1：`-gen-*` 选项在某个中心注册表里列出。**
正确认识：没有注册表。每个 backend 的 `.cpp` 末尾一个 namespace-scope `Opt`/`OptClass` 对象，构造时把自己塞进 `cl::opt<FnT>` 的 parser。链接哪些 `.o`，就有哪些 backend。

**错误直觉 2：可以同时跑多个 backend。**
正确认识：`ApplyCallback` 只读 `cl::opt<FnT>` 的当前值，**最后一个 flag 赢**。实测 `--gen-a --gen-b` → `b`，`--gen-b --gen-a` → `a`。

**错误直觉 3：两个 backend 重名只会互相覆盖。**
正确认识：`cl::ParseCommandLineOptions` 阶段直接 `Aborted`，退出码 134，且这条检查发生在**运行时**——所以两个重名 backend 会让整个二进制不可用，不只是那个 flag 不可用。

**错误直觉 4：`Opt` 的回调和 `TableGenMainFn` 签名一样。**
正确认识：`FnT` 是 `void(const RecordKeeper &, raw_ostream &)`，`TableGenMainFn` 是 `bool(raw_ostream &, const RecordKeeper &)`——**参数顺序相反、返回类型相反**。写反了是「no known conversion」而不是「参数不匹配」，容易看懵。

**错误直觉 5：忘了设 `ByDefault` 只会让默认输出为空。**
正确认识：`ApplyCallback` 返回 `true` → 走 `MainFn` → `nullptr` 时 `status = 1` → **静默退出码 1，零输出**。没有任何错误消息。

**错误直觉 6：`TableGenMain(argv[0], nullptr)` 是「不要 fallback」的写法。**
正确认识：两个重载的默认参数都是 `nullptr`，直接传 `nullptr` 二义。必须先声明 `MultiFileTableGenMainFn MainFn = nullptr;` 再传变量。

**错误直觉 7：多文件 backend 的额外文件名就是 `Suffix`。**
正确认识：`sys::path::replace_extension(Filename, "")` 去掉**最后一段**扩展名，再 `append(Suffix)`。实测两种：

```console
$ ./mf --gen-mf c.td -o AArch64GenRegisterInfo.inc
AArch64GenRegisterInfo.inc          ← 主文件
AArch64GenRegisterInfo.extra.h
AArch64GenRegisterInfo.impl.inc

$ ./mf2 --gen-mf c.td -o Foo.generated.inc     # 后缀是 "Enums.inc"
Foo.generated.inc
Foo.generatedEnums.inc              ← 只去掉 ".inc"，".generated" 留着
```

所以 `-o Foo.inc` + `"Enums.inc"` → `FooEnums.inc`；`-o Foo.generated.inc` + `"Enums.inc"` → `Foo.generatedEnums.inc`。**这是 `replace_extension` 的语义，不是 TableGen 特意设计**——如果你的输出文件名里有多个点，位置会很反直觉。

**错误直觉 8：加了多文件 backend 只要改 `.cpp` 和 `CMakeLists.txt`。**
正确认识：还要改 `llvm/cmake/modules/TableGen.cmake` 的 `EXTRA_OUTPUTS`，否则 Ninja 不知道那些是产物。

## 13.11 延伸阅读

- **`llvm/lib/TableGen/TableGenBackend.cpp`**（119 行，全读）
  - 本章 13.2–13.5 的全部实现。重点：`OptionValue<FnT>` 特化（36–52）、`ManagedStatic` + `OptCreatorT`（54–66）、`Opt` 构造函数（68–73）、`ApplyCallback`（76–92）。
- **`llvm/include/llvm/TableGen/TableGenBackend.h`**（约 90 行）
  - `FnT`、`Opt`、`OptClass`、`MultiFileOptClass` 的完整定义。`OptClass` 只有 12 行，值得背下来。
- **`llvm/lib/TableGen/Main.cpp`** 的 `TableGenMain`（150–230）
  - `FilenamePrefix` 怎么来的（194）、额外文件怎么写（210–220）、四道 `ErrorsPrinted` 闸门。
- **`llvm/utils/TableGen/RegisterInfoEmitter.cpp`** 的 `run`（2123–2158）与注册点（2252）
  - 全仓唯一的 `MultiFileOptClass` 实例。5 个文件的组织方式值得照抄。
- **`llvm/cmake/modules/TableGen.cmake`** 的 `tablegen` 函数（10–190）与 `add_tablegen`（192+）
  - 「backend 怎么进构建系统」。`EXTRA_OUTPUTS` 那段是 13.5 的必读补充。
- **`llvm/utils/TableGen/TableGenBackends.h`** 顶部的长注释
  - 上游对 backend 的定义与「应该长什么样」的说明。最后那段 FIXME 抱怨的正是 13.1 描述的静态注册机制（「Perhaps have a standalone TableGen binary and have the backends be loadable modules」）。
- **`llvm/utils/TableGen/TableGenBackendSkeleton.cpp`**（64 行）
  - 官方骨架。它把 `OptClass`（Option A）和 `Opt`（Option B）两种写法并排放，是 13.2 那张表的最佳脚注。

## 下一章

Ch 13 讲的是「怎么挂上去」。下一章讲「挂上去之后写什么」——从零实现一个生产级 backend：如何组织 `RecordKeeper` 的遍历、如何用 `emitSourceFileHeader`、如何处理可选字段、如何避免生成物的确定性陷阱（`DenseMap` 迭代顺序、指针排序）。

[Ch 14 写一个生产级 backend：从骨架到可合并代码](./14-writing-a-backend.md)
