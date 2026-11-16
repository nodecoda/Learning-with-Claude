# Ch 26 — Clang 前端接入

> **目标**：解释“Clang 认识一个 target”到底意味着哪几层契约；用 RISC-V 的真实源码和实测输出定位 `TargetInfo`、driver、ABI、builtins、runtime 的接入点；给出一份可执行的 MiniRV 接入清单（guided）。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。前端实测使用 `/home/ubuntu/llvm/builds/pr-lane/bin/clang`，后端实测使用 `$B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`。原因见下面的“实测”一节。

## 为什么需要这个？

Ch 25 证明了：只要你会写 LLVM IR，`llc -mtriple=minirv64` 就能生成 MiniRV 机器码。那为什么还需要 Clang？

因为 `llc` 只回答一个问题：**这段 IR 怎么变成机器码**。它不回答：

- `long` 在 MiniRV 上是 32 位还是 64 位？
- `struct { int a,b,c; }` 按值传参时，是拆成两个寄存器，还是走栈？
- `__riscv_mul` 这类预定义宏要不要定义？
- `-march=rv64im` 应该翻译成哪些 `-target-feature`？
- 链接时用哪个 `crt0`、哪个 `libgcc`/compiler-rt、哪个链接器？
- `va_list` 是什么类型，`va_arg` 怎么推进？

这些都不是后端的问题，是**前端 + ABI + driver + runtime** 的问题。Clang 不认识 MiniRV 时，哪怕 LLVM 后端已经完整，`clang --target=minirv64 -c t.c` 仍然会直接失败。

本章要建立的判断是：

> “Clang 认识一个 target”不是“LLVM 里有这个 target”，而是五层契约同时成立：`TargetInfo`、driver `ToolChain`、`CodeGen` ABI、builtins、runtime。

MiniRV 目前只完成了后端这一层，所以本章的接入步骤是 **guided**：我们给出文件、符号、测试和验收命令，但不在本机构建 clang。

## 先建立直觉

把 `clang --target=minirv64 t.c` 想成一条流水线。每一层都可能成为“unknown target”或“链接失败”的发生点：

```
┌─────────────────────────────────────────────────────────────┐
│ Driver（clang/lib/Driver）                                   │
│  triple ──► ToolChain（BareMetal / Linux / …）                │
│  -march/-mabi ──► cc1 flags:                                │
│      -target-cpu  -target-feature  -target-abi              │
│  链接 job ──► ld.lld + crt0 + compiler-rt + libc            │
└───────────────────────────┬─────────────────────────────────┘
                            │ 启动 cc1
┌───────────────────────────▼─────────────────────────────────┐
│ Frontend（clang/lib/Basic + Sema + CodeGen）                  │
│  TargetInfo：指针宽度、对齐、预定义宏、builtins、va_list      │
│  Sema/AST：C 语义                                            │
│  ABIInfo：结构体分类、参数/返回值、varargs                    │
│      │                                                       │
│      ▼                                                       │
│  LLVM IR（.ll）                                               │
└───────────────────────────┬─────────────────────────────────┘
                            │ llc / LLVM backend
┌───────────────────────────▼─────────────────────────────────┐
│ Backend（llvm/lib/Target/MiniRV）                             │
│  ISel ──► RA ──► Frame ──► AsmPrinter ──► .o                 │
└───────────────────────────┬─────────────────────────────────┘
                            │ ld.lld
                            ▼
                         ELF + runtime
```

每一层的“认不认识”有各自的注册表：

| 问题 | 谁回答 | 失败表现 |
|---|---|---|
| `llc` 能不能编译 `minirv64`？ | LLVM `TargetRegistry` + `RegisterTarget<Triple::minirv64>` | `llc: error: unable to get target for 'minirv64'` |
| `clang` 能不能解析 `minirv64` 的 C 语义？ | `clang::TargetInfo` + `AllocateTarget` | `error: unknown target triple 'minirv64'` |
| `clang` 会不会加正确的 `-march`/`-mabi`？ | driver `ToolChain` + `Arch/*.cpp` | 编译能过，但 ABI/特性不对 |
| C 结构体/varargs 的 ABI 对不对？ | `CodeGen/Targets/*.cpp` 的 `ABIInfo` | 能编译，运行结果错 |
| 链接能不能过？ | `BareMetal`/`Gnu` + linker + runtime | `undefined symbol: __muldi3` |

## 主线：五层契约分别是什么

### 第一层：`TargetInfo`——语言层面的 target 模型

`clang::TargetInfo` 回答“在这个 target 上，C 代码意味着什么”。RISC-V 的实现在 `clang/lib/Basic/Targets/RISCV.h`（`RISCVTargetInfo`、`RISCV64TargetInfo`）和 `RISCV.cpp`（`getTargetDefines`、`getTargetBuiltins`、`initFeatureMap`、`handleTargetFeatures`）。

`RISCV64TargetInfo` 的构造函数写死 64 位模型：

```cpp
LongWidth = LongAlign = PointerWidth = PointerAlign = 64;
IntMaxType = Int64Type = SignedLong;
resetDataLayout();
```

`setABI` 只接受 `lp64`、`lp64f`、`lp64d`（`lp64e` 走特殊分支）。它决定 `long`/指针宽度、`va_list` 推进方式、结构体走整数还是浮点寄存器，以及 `__riscv_float_abi_soft` 之类的宏。

`getTargetDefines` 是预定义宏的入口。实测（完整命令见“实测”）：

```c
#define __riscv_cmodel_medlow 1
#define __riscv_div 1
#define __riscv_float_abi_soft 1
#define __riscv_mul 1
#define __riscv_muldiv 1
#define __riscv_xlen 64
```

这些宏不是装饰。代码用 `#ifdef __riscv_muldiv` 决定要不要手写软乘除，用 `__riscv_xlen` 决定 `long` 的假设。没有它们，代码即使能编译，也会走错分支。

`getTargetBuiltins` 返回 `Builtin::InfosShard`。LLVM 24 的数据库是 `clang/include/clang/Basic/BuiltinsRISCV.td`，**不是**老版本的 `BuiltinsRISCV.def`；`.inc` 由 `clang_tablegen` 在 `clang/include/clang/Basic/CMakeLists.txt` 生成。

### 第二层：driver `ToolChain` 与 `Arch`——从命令行到 cc1 flags

driver 把 `--target`、`-march`、`-mabi`、`-mcmodel`、`-mrelax` 变成 cc1 能理解的 `-target-*`，再决定链接用什么。RISC-V 的入口：

- `clang/lib/Driver/ToolChains/Arch/RISCV.cpp`：`getRISCVArch`、`getRISCVABI`、`getRISCVTargetFeatures`。
- `clang/lib/Driver/ToolChains/Clang.cpp`：`Clang::AddRISCVTargetArgs` 推 `-target-abi`。
- `clang/lib/Driver/Driver.cpp`：`Driver::getToolChain` 对 unknown OS 的 riscv64 选择 `toolchains::BareMetal`（Linux 走 Linux/Gnu）。
- `clang/lib/Driver/ToolChains/Gnu.cpp` / `BareMetal.cpp`：multilib、库路径、链接器。

`getRISCVABI` 的优先级：显式 `-mabi=` → 根据 `-march` 推导 → triple 默认值。

这里有一个对 MiniRV 很关键的细节：`getRISCVABI` 开头有 `assert(Triple.isRISCV())`，而 `Triple::isRISCV()` 只包含 `riscv32/riscv64` 及其 BE 变体，**不包含** `minirv64`。直接复用 riscv 的 driver 函数会在 assertions 构建里断言失败。MiniRV 要么有自己的 `Arch/MiniRV.cpp`，要么先把 `isRISCV()` 的语义扩展清楚。

### 第三层：`CodeGen` 的 `ABIInfo`——C 的调用约定

`TargetInfo` 管类型宽度和宏；`ABIInfo` 管“这个类型怎么传”。RISC-V 的实现在：

- `clang/lib/CodeGen/Targets/RISCV.cpp`：`RISCVABIInfo`、`RISCVTargetCodeGenInfo`、`createRISCVTargetCodeGenInfo`。
- `clang/lib/CodeGen/CodeGenModule.cpp`：`createTargetCodeGenInfo` 的 `case riscv64`。
- `clang/lib/CodeGen/TargetInfo.cpp`：`ABIArgInfo`、`TargetCodeGenInfo` 的基类工具，**不**包含 `RISCVABIInfo`。

很多旧资料说“`RISCVABIInfo` 在 `clang/lib/CodeGen/TargetInfo.cpp`”。当前树已经搬到 `Targets/RISCV.cpp`；grep 不到不是代码没了，是文件拆了。

`RISCVABIInfo` 的关键方法：`computeInfo`（分类参数/返回值）、`classifyArgumentType`/`classifyReturnType`（整数寄存器、浮点寄存器、byval、coerce）、`EmitVAArg`（`va_arg` 如何推进）、`extendType`（`signext`/`zeroext`）。

实测一个 12 字节结构体：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -march=rv64im -mabi=lp64 \
  -O0 -S -emit-llvm abi2.c -o - | grep -E '^target datalayout|^define'
target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
define dso_local signext i32 @f([2 x i64] %s.coerce) #0 {
```

同样 C 在 x86-64 上是 `define dso_local i32 @f(i64 %s.coerce0, i32 %s.coerce1)`。RISC-V 用两个 64 位槽，x86-64 拆成 `{i64,i32}`。MiniRV 若声称遵循 RISC-V lp64 子集，就必须让 IR coerce 形状与后端 calling convention 一致；否则前端和后端各说各话，链接能过，运行结果错。

### 第四层：builtins

builtins 需要三样东西：数据库（`BuiltinsRISCV.td`）、`getTargetBuiltins` 挂载、Sema/CodeGen 把它变成 intrinsic/libcall。MiniRV 的第一版可以返回空列表，只支持标准 C；用户写 `__builtin_minirv_*` 会得到“未知 builtin”，这是可接受的 bring-up 状态。

### 第五层：runtime

Clang 生成的代码不保证自包含。MiniRV 后端会发这些 libcall（Ch 20）：`__muldi3`/`__divdi3`（无 M 时）、`__divti3`（128 位）、`__addsf3` 等软浮点、`__atomic_*`（libatomic）、`sin`/`cos`（libm）。

所以“Clang 能生成 `.o`”不等于“能链接”。你还需要 `crt0`/`start.o`、compiler-rt（或 libgcc）、可选的 libc/libm、`-m elf64lriscv` 链接，以及 `--no-relax`（MiniRV 没有完整的链接期 relaxation 模型）。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| TargetInfo 基类 | `clang/include/clang/Basic/TargetInfo.h` | `TargetInfo`、`getTargetDefines`、`getTargetBuiltins` | 一个 target 必须提供什么 |
| TargetInfo 注册 | `clang/lib/Basic/Targets.cpp` | `AllocateTarget` | `switch (Triple.getArch())` 的入口 |
| RISC-V TargetInfo | `clang/lib/Basic/Targets/RISCV.h/.cpp` | `RISCVTargetInfo`、`RISCV64TargetInfo` | 64 位模型、`setABI`、宏 |
| Driver Arch | `clang/lib/Driver/ToolChains/Arch/RISCV.cpp` | `getRISCVArch`、`getRISCVABI`、`getRISCVTargetFeatures` | `-march`/`-mabi` 优先级 |
| Driver cc1 flags | `clang/lib/Driver/ToolChains/Clang.cpp` | `Clang::AddRISCVTargetArgs` | `-target-abi`、`-target-feature` |
| Driver ToolChain | `clang/lib/Driver/Driver.cpp` | `Driver::getToolChain` | triple → `BareMetal` |
| 链接/库 | `clang/lib/Driver/ToolChains/BareMetal.cpp`、`Gnu.cpp` | `isRISCVBareMetal`、`findRISCVMultilibs` | crt0、库路径、multilib |
| ABI 注册 | `clang/lib/CodeGen/CodeGenModule.cpp` | `createTargetCodeGenInfo` | `case riscv64` → `createRISCVTargetCodeGenInfo` |
| ABI 实现 | `clang/lib/CodeGen/Targets/RISCV.cpp` | `RISCVABIInfo`、`computeInfo`、`EmitVAArg` | 结构体/varargs/返回值 |
| ABI 基类 | `clang/lib/CodeGen/TargetInfo.cpp` | `ABIArgInfo`、`TargetCodeGenInfo` | 工具，不是 RISC-V 实现 |
| Builtins | `clang/include/clang/Basic/BuiltinsRISCV.td` | `RISCVBuiltin`、`TargetBuiltin` | TableGen 数据库 |
| Builtins 生成 | `clang/include/clang/Basic/CMakeLists.txt` | `clang_tablegen(BuiltinsRISCV.inc ...)` | `.td` → `.inc` |
| 测试 | `clang/test/CodeGen/RISCV`、`clang/test/Driver/riscv-*` | `RUN:` / `CHECK:` | ABI、宏、flag、诊断 |

## 实测：riscv64 在 Clang 眼里是什么样

先看 driver 实际传给 cc1 的关键 flag：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -### -c t.c 2>&1 \
  | grep -o '"-\(triple\|target-cpu\|target-abi\)" "[^"]*"\|"-target-feature" "+m"'
"-triple" "riscv64"
"-target-cpu" "generic-rv64"
"-target-feature" "+m"
"-target-abi" "lp64"
```

这四行说明 driver 固定了 triple、选了默认 CPU `generic-rv64`、推导出 ABI `lp64`，并打开 M 扩展。

再看生成的汇编：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -S t.c -o - \
  | grep -E '\.attribute|^[[:space:]]*(addw|addi[[:space:]]+sp|jalr|ret)'
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_a2p1_c2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0"
	addi	sp, sp, -32
	addw	a0, a0, a1
	addi	sp, sp, 32
	ret
```

`.attribute 5` 是 ISA 字符串：`rv64i2p1_m2p0_a2p1_c2p0_...`。它由 driver 的默认特性决定，不是 `llc` 的默认。`addw` 是 32 位加法在 RV64 上的正确形式。

预定义宏：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -march=rv64im -mabi=lp64 \
  -dM -E -x c /dev/null 2>/dev/null | grep -E '^#define __riscv_(cmodel_medlow|xlen|mul|div|muldiv|float_abi_soft) '
#define __riscv_cmodel_medlow 1
#define __riscv_div 1
#define __riscv_float_abi_soft 1
#define __riscv_mul 1
#define __riscv_muldiv 1
#define __riscv_xlen 64
```

这些宏来自 `RISCVTargetInfo::getTargetDefines`。注意 `-march=rv64im` 仍然定义了 `__riscv_div`/`__riscv_muldiv`，因为 M 包含除法；`__riscv_float_abi_soft` 说明默认 ABI 不带浮点寄存器。

## 实测：minirv64 在 Clang 眼里是什么样

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -c t.c -o t_mini.o
error: unknown target triple 'minirv64'
$ echo $?
1
```

这是前端 `TargetInfo::CreateTargetInfo` 的错误。源码路径是：

```
AllocateTarget(Triple, Opts)
  switch (Triple.getArch()) {
  default:
    return nullptr;          // ← minirv64 落到这里
  case llvm::Triple::riscv64:
    ...
  }
if (!Target)
  Diags.Report(diag::err_target_unknown_triple) << Triple.str();
```

`AllocateTarget` 在 `clang/lib/Basic/Targets.cpp`。它没有 `case llvm::Triple::minirv64`，所以返回 `nullptr`，然后 `TargetInfo::CreateTargetInfo` 报错。**这与 LLVM 后端有没有 MiniRV 完全无关。**

### 一个非常容易误判的现象：`-###` 成功

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -### -c t.c > /tmp/mini_dry.txt 2>&1
$ echo $?
0
$ grep -o '"-triple" "minirv64"' /tmp/mini_dry.txt
"-triple" "minirv64"
```

`-###` 只打印将要执行的 job，**不启动 cc1**，因此不会构造 `TargetInfo`。它甚至不会加 `-target-abi`，因为 driver 的 arch 分支也不知道 MiniRV。用 `-###` 的退出码判断“target 支持”是错的；必须实际 `-c`。

### `-print-targets` 也不是 Clang 支持矩阵

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang -print-targets 2>&1 \
  | grep -E 'Registered|aarch64 |riscv64 |x86'
  Registered Targets:
    aarch64     - AArch64 (little endian)
    riscv64     - 64-bit RISC-V
    x86         - 32-bit X86: Pentium-Pro and above
    x86-64      - 64-bit X86: EM64T and AMD64
```

这个列表来自 LLVM 的 `TargetRegistry`，而且只包含当前这个 clang 构建编进去的后端。`pr-lane` 构建没有 MiniRV，所以列表里没有 `minirv64`；`dsl-tablegen` 的 `llc --version` 有：

```console
$ $B/llc --version | grep -E 'minirv64|riscv64'
    minirv64   - MiniRV (auto-td demo)
    riscv64    - 64-bit RISC-V
    riscv64be  - 64-bit big endian RISC-V
```

即使某个 clang 构建把 MiniRV 后端编进去了，`-print-targets` 会列出 `minirv64`，`clang --target=minirv64 -c` 仍然会失败，直到 `AllocateTarget` 有了对应 case。这两张注册表不是同一张。

### 后端确实能独立工作

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/t.ll -o - | grep -E '^[[:space:]]*(addw|jalr)'
	addw	x10, x10, x11
	jalr	x0, 0(x1)
```

这份 IR 是：

```llvm
define i32 @f(i32 %a, i32 %b) {
  %r = add i32 %a, %b
  ret i32 %r
}
```

`llc` 不需要 Clang，也不需要 `TargetInfo`。它只需要 LLVM IR + MiniRV 后端。

### 关于 `dsl-tablegen` 的 clang

当前树里的 clang 二进制启动失败：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/clang --version
/home/ubuntu/llvm/builds/dsl-tablegen/bin/clang: symbol lookup error: /home/ubuntu/llvm/builds/dsl-tablegen/bin/../lib/../lib/libLLVMLTO.so.24.0git: undefined symbol: _ZN4llvm17SampleProfileFileB5cxx11E
```

这是共享库和二进制不同步的构建问题，不是 MiniRV 的问题。因此前端行为用 `pr-lane` 的 clang 验证；后端行为用 `dsl-tablegen` 的 `llc` 验证。写课程时要把“哪个二进制证明了哪件事”分清楚，不要把构建环境的锅算到 target 上。

## 主线：为什么 llc 接受 minirv64，clang 不接受

把两个注册路径并排看：

```
LLVM 后端注册（llc / opt / llvm-mc 使用）
  MiniRVTargetInfo.cpp:
    LLVMInitializeMiniRVTargetInfo()
      RegisterTarget<Triple::minirv64>(..., "minirv64", "MiniRV ...")
  → TargetRegistry 里有 minirv64
  → llc --version 能看到
  → llc -mtriple=minirv64 能建 TargetMachine

Clang 前端注册（clang -c 使用）
  clang/lib/Basic/Targets.cpp:
    AllocateTarget(Triple):
      switch (Triple.getArch()) {
        case llvm::Triple::riscv64: return RISCV64TargetInfo...
        default: return nullptr;
      }
  → 没有 minirv64 分支
  → TargetInfo::CreateTargetInfo 报 unknown target triple
```

为什么 Clang 不直接复用 LLVM 的 `TargetRegistry`？因为前端需要的信息不在后端里：

- `long` 的宽度、`wchar_t` 的类型、`long double` 的格式；
- 预定义宏；
- `va_list` 的类型和推进规则；
- 内建函数；
- 约束寄存器、inline asm 的寄存器名；
- 原子操作的最大宽度；
- 哪些类型可以按值传。

这些是“语言/ABI 模型”，不是“指令选择”。所以 Clang 有一套独立的 `TargetInfo` 类层次，`AllocateTarget` 是它的工厂。`Triple::minirv64` 这个枚举存在，只说明 LLVM 的 triple 解析认识它；不说明 Clang 的工厂能造出对象。

同理，driver 还有第三张表：`Driver::getToolChain`。它决定用哪个 `ToolChain` 子类（`BareMetal`、`Linux`、`Generic_GCC`……），进而决定链接器和库路径。三张表要分别补，缺一张就会在对应阶段失败。

## 主线：给 MiniRV 接 Clang 的 guided 清单

以下步骤是 **guided**：给出文件、符号、测试和验收命令，本章**没有**重新构建 clang。

### 第 0 步：先定契约

- 数据模型 LP64；ISA RV64IM；ABI 是 RISC-V lp64 的整数子集（8 个参数寄存器、2 个返回值寄存器）。
- 软浮点，`long double` 走 quad helper；原子降到 libatomic；TLS 不支持。
- 运行时用 `start.o` + compiler-rt；链接用 `ld.lld -m elf64lriscv --no-relax`。

### 第 1 步：`TargetInfo` 子类

新增 `clang/lib/Basic/Targets/MiniRV.h` 和 `MiniRV.cpp`：

```cpp
class MiniRVTargetInfo : public TargetInfo {
  std::string ABI;
public:
  MiniRVTargetInfo(const llvm::Triple &T, const TargetOptions &O)
      : TargetInfo(T) {
    LongWidth = LongAlign = PointerWidth = PointerAlign = 64;
    IntMaxType = Int64Type = SignedLong;
    LongDoubleWidth = LongDoubleAlign = 128;
    LongDoubleFormat = &llvm::APFloat::IEEEquad();
    resetDataLayout("e-m:e-p:64:64-i64:64-i128:128-n32:64-S128");
  }
  bool setABI(const std::string &N) override {
    if (N != "lp64") return false;
    ABI = N; return true;
  }
  void getTargetDefines(const LangOptions &, MacroBuilder &) const override;
  llvm::SmallVector<Builtin::InfosShard> getTargetBuiltins() const override;
  BuiltinVaListKind getBuiltinVaListKind() const override {
    return TargetInfo::VoidPtrBuiltinVaList;
  }
};
```

`getTargetDefines` 至少定义 `__minirv`、`__minirv_xlen 64`、`__minirv_mul`、`__minirv_div`、`__minirv_float_abi_soft`。不要定义 `__riscv`：那会让大量代码走 RISC-V 分支，而那些分支可能假设 A/C/F/D 存在。第一版 `getTargetBuiltins` 可以返回 `{}`。

### 第 2 步：注册 `TargetInfo`

在 `clang/lib/Basic/Targets.cpp` 的 `AllocateTarget` 加：

```cpp
case llvm::Triple::minirv64:
  return std::make_unique<MiniRVTargetInfo>(Triple, Opts);
```

把 `Targets/MiniRV.cpp` 加进 `clang/lib/Basic/CMakeLists.txt`。验收：

```console
$ clang --target=minirv64 -c t.c -o t.o
$ clang --target=minirv64 -dM -E -x c /dev/null | grep __minirv
```

### 第 3 步：`ABIInfo`

新增 `clang/lib/CodeGen/Targets/MiniRV.cpp`：

```cpp
class MiniRVABIInfo : public DefaultABIInfo {
public:
  void computeInfo(CGFunctionInfo &FI) const override;
  ABIArgInfo classifyArgumentType(QualType Ty) const;
  ABIArgInfo classifyReturnType(QualType RetTy) const;
  RValue EmitVAArg(CodeGenFunction &, Address, QualType,
                   AggValueSlot) const override;
};
```

策略：前 8 个整数参数走 `x10`–`x17`，其余走栈；返回值前 2 个走 `x10`/`x11`；≤ 2×XLEN 的聚合体按整数字寄存器 coerce；大聚合体用 `byval`；varargs 实现 RISC-V 风格保存区；标量按 ABI 加 `signext`/`zeroext`。

在 `clang/lib/CodeGen/CodeGenModule.cpp` 的 `createTargetCodeGenInfo` 加 `case minirv64: return createMiniRVTargetCodeGenInfo(CGM);`，在 `TargetInfo.h` 声明，并把 `Targets/MiniRV.cpp` 加进 `clang/lib/CodeGen/CMakeLists.txt`。

验收：`-emit-llvm -S -O0` 看 coerce 形状，再和 `llc -mtriple=minirv64` 的 calling convention 对照。

### 第 4 步：driver 的 `Arch/MiniRV.cpp`

新增 `clang/lib/Driver/ToolChains/Arch/MiniRV.h/.cpp`，提供：

- `getMiniRVArch`：只接受 `rv64im`/`rv64i`，默认 `rv64im`；拒绝 A/C/F/D/V。
- `getMiniRVABI`：只接受 `lp64`。
- `getMiniRVTargetFeatures`：总是 `+m`，`-relax`。

不要直接调用 `riscv::getRISCVABI`，因为它的 `assert(Triple.isRISCV())` 对 `minirv64` 不成立。

### 第 5 步：`Clang::AddMiniRVTargetArgs`

在 `clang/lib/Driver/ToolChains/Clang.cpp` 加：

```cpp
void Clang::AddMiniRVTargetArgs(const ArgList &Args,
                                ArgStringList &CmdArgs) const {
  CmdArgs.push_back("-target-abi");  CmdArgs.push_back("lp64");
  CmdArgs.push_back("-target-cpu");  CmdArgs.push_back("generic-minirv64");
  CmdArgs.push_back("-target-feature"); CmdArgs.push_back("+m");
  CmdArgs.push_back("-target-feature"); CmdArgs.push_back("-relax");
}
```

并在 arch switch 里加 `case llvm::Triple::minirv64: AddMiniRVTargetArgs(Args, CmdArgs); break;`。验收：

```console
$ clang --target=minirv64 -### -c t.c 2>&1 | grep -E 'target-abi|target-feature|target-cpu'
```

### 第 6 步：`ToolChain` 与链接

在 `clang/lib/Driver/Driver.cpp` 的 `getToolChain` 加：

```cpp
case llvm::Triple::minirv64:
  TC = std::make_unique<toolchains::BareMetal>(*this, Target, Args);
  break;
```

再扩展 `BareMetal.cpp`：`isMiniRVBareMetal`、链接器 `ld.lld`、`-m elf64lriscv --no-relax`、找 `start.o`/`crt0.o`、库路径指向 MiniRV 的 sysroot。第一版可以只支持 `-nostdlib` + 显式 `start.o`。

### 第 7 步：builtins

新增 `BuiltinsMiniRV.td`（旧版本对应 `BuiltinsMiniRV.def`），在 `clang/include/clang/Basic/CMakeLists.txt` 加 `clang_tablegen(BuiltinsMiniRV.inc -gen-clang-builtins SOURCE BuiltinsMiniRV.td TARGET ClangBuiltinsMiniRV)`，在 `getTargetBuiltins` 返回对应 `InfosShard`。第一版可以完全跳过。

### 第 8 步：runtime

准备 `start.o`、compiler-rt builtins（`__muldi3`、`__divdi3`、`__divti3`、软浮点）、可选的 libm/libc、链接脚本。没有 runtime，Clang 能生成 `.o`，但链接会失败。

### 第 9 步：测试与构建

| 测试 | 文件 | 检查什么 |
|---|---|---|
| 宏 | `clang/test/Preprocessor/minirv-macros.c` | `__minirv_xlen` 等 |
| driver | `clang/test/Driver/minirv-*.c` | `-target-abi lp64`、`+m`、`-relax`、链接器 |
| ABI | `clang/test/CodeGen/MiniRV/abi.c` | `signext`、`[2 x i64]`、byval |
| varargs | `clang/test/CodeGen/MiniRV/varargs.c` | `va_start`/`va_arg` |
| 诊断 | `clang/test/Driver/minirv-invalid-*.c` | 拒绝 `rv64gc`、`lp64d` |

构建是 guided：

```console
$ ninja -C /home/ubuntu/llvm/builds/minirv-clang clang
$ llvm-lit clang/test/CodeGen/MiniRV clang/test/Driver/minirv-*.c
```

## 文件清单

| 层 | 要新增/修改的文件 | 关键符号 | RISC-V 参照 |
|---|---|---|---|
| TargetInfo | `clang/lib/Basic/Targets/MiniRV.h/.cpp` | `MiniRVTargetInfo` | `Targets/RISCV.h/.cpp` |
| 注册 | `clang/lib/Basic/Targets.cpp` | `AllocateTarget` | `case riscv64` |
| ABI | `clang/lib/CodeGen/Targets/MiniRV.cpp` | `MiniRVABIInfo` | `Targets/RISCV.cpp` |
| ABI 注册 | `clang/lib/CodeGen/CodeGenModule.cpp`、`TargetInfo.h` | `createMiniRVTargetCodeGenInfo` | `createRISCVTargetCodeGenInfo` |
| Driver arch | `clang/lib/Driver/ToolChains/Arch/MiniRV.h/.cpp` | `getMiniRVArch`、`getMiniRVABI` | `Arch/RISCV.cpp` |
| cc1 flags | `clang/lib/Driver/ToolChains/Clang.cpp` | `AddMiniRVTargetArgs` | `AddRISCVTargetArgs` |
| ToolChain | `clang/lib/Driver/Driver.cpp` | `getToolChain` case | `BareMetal` |
| 链接 | `clang/lib/Driver/ToolChains/BareMetal.cpp` | `isMiniRVBareMetal`、`--no-relax` | `isRISCVBareMetal` |
| Builtins | `clang/include/clang/Basic/BuiltinsMiniRV.td`、`CMakeLists.txt`、`TargetBuiltins.h` | `TargetBuiltin` | `BuiltinsRISCV.td` |
| 测试 | `clang/test/CodeGen/MiniRV`、`clang/test/Driver`、`clang/test/Preprocessor` | `RUN:`/`CHECK:` | `clang/test/CodeGen/RISCV` |
| Runtime | `compiler-rt/lib/builtins`（clang 之外） | `__muldi3`、`__divti3`、软浮点 | 同左 |

## 为什么后端可以先于 Clang 有用

很多人以为“没有 Clang 就没有工具链”。不对。LLVM 的核心接口是 **LLVM IR**，不是 C。只要后端完成了 IR → 机器码，它就已经对以下用户有用：

| 用户 | 需要 Clang 吗？ | 用什么 |
|---|---|---|
| 手写 IR / 编译器研究 | 不需要 | `llc`、`opt` |
| 汇编/反汇编 | 不需要 | `llvm-mc`、`llvm-objdump` |
| 语言前端（Rust/Swift/Flang/MLIR/DSL） | 不需要 | 直接发射 LLVM IR |
| JIT / ExecutionEngine | 不需要 | ORC/MCJIT 把 IR 编成机器码（在 MiniRV 主机上执行） |
| 固件/裸机 | 不需要 | `llc` + `llvm-mc` + linker script + `start.o` |
| C/C++ 用户 | 需要 | `clang` |
| 差分测试（Ch 25） | 不需要 | `llc` + `ld.lld` + qemu |

这就是为什么 Ch 25 的 e2e 能在“Clang 完全不认识 MiniRV”的情况下跑通。后端是一个独立的编译器组件；Clang 只是它最大的 IR 生产者之一。

没有 Clang 你会失去什么：

- C/C++ 源码的直接编译；
- 预定义宏、类型布局、ABI 决策；
- builtins；
- driver 的 `-march`/`-mabi`/sysroot/linker 集成；
- 运行时库的自动选择。

但只要你愿意手写符合 ABI 的 IR，后端就已经能交付价值。这也是 bring-up 的合理顺序：先把后端和 ABI 跑通，再接前端。

## 反直觉与踩雷

1. **错误直觉**：`llc --version` 里有 `minirv64`，所以 `clang --target=minirv64` 也应该能用。 → **正确认识**：LLVM `TargetRegistry` 和 Clang `AllocateTarget` 是两张独立的表。后端注册只让 `llc`/`opt`/`llvm-mc` 认识它；Clang 还需要自己的 `TargetInfo` 子类和 `AllocateTarget` case。
2. **错误直觉**：`clang --target=minirv64 -###` 退出 0，说明 target 被接受了。 → **正确认识**：`-###` 只打印 job，不启动 cc1，不构造 `TargetInfo`；实际 `-c` 仍然报 `unknown target triple`。验收必须跑真正的编译。
3. **错误直觉**：在 `Triple.h` 里加了 `minirv64` 枚举，Clang 就认识了。 → **正确认识**：枚举只影响 triple 解析。Clang 的语言模型、ABI、driver、链接器各自还有一张表；枚举是必要不充分条件。
4. **错误直觉**：`RISCVABIInfo` 在 `clang/lib/CodeGen/TargetInfo.cpp`，找不到就是没有。 → **正确认识**：当前树里它在 `clang/lib/CodeGen/Targets/RISCV.cpp`；`TargetInfo.cpp` 是 `ABIArgInfo`/`TargetCodeGenInfo` 的基类工具。文件拆分后，旧路径的 grep 会误导你。
5. **错误直觉**：builtins 数据库是 `BuiltinsRISCV.def`。 → **正确认识**：LLVM 24 用 `BuiltinsRISCV.td` + `clang_tablegen` 生成 `BuiltinsRISCV.inc`。`.def` 是旧版本/旧文档的路径。
6. **错误直觉**：直接复用 `riscv::getRISCVABI` 最省事。 → **正确认识**：它开头 `assert(Triple.isRISCV())`，而 `isRISCV()` 不包含 `minirv64`。assertions 构建会直接断言失败；release 构建则可能给出错误的默认 ABI。
7. **错误直觉**：driver 的 `riscv64` 默认带 `+m`，所以 MiniRV 也可以照抄。 → **正确认识**：`llc` 的 `riscv64` 默认是 `rv64i2p1`（Ch 25 已实测）；clang driver 默认带 `+m`。MiniRV 要显式规定 `+m`、`-relax`，不要依赖任何一方的默认。
8. **错误直觉**：有了 `TargetInfo` 就能编译 C 并正确运行。 → **正确认识**：没有 `ABIInfo`，结构体/varargs 的 IR 形状可能和后端 calling convention 不一致；能编译、能链接，但运行结果错。ABI 是独立的一层。

## 进阶：再往深一层

- **看 ABI 的 IR 形状**：`clang -cc1 -triple riscv64 -emit-llvm -S -O0 abi.c`，对照 `define` 里的 `[2 x i64]`、`byval`、`signext`、`zeroext`。这是检查 `ABIInfo` 最快的方法。
- **看记录布局**：`clang -Xclang -fdump-record-layouts -c abi.c`，确认 `TargetInfo` 的宽度/对齐和 ABI 的假设一致。
- **绕过 driver 直接测 cc1**：`clang -cc1 -triple minirv64 -target-abi lp64 -emit-obj t.c`。这能在 driver 还没接好时，单独验证 `TargetInfo` + `ABIInfo` + 后端。
- **对照 `llc` 的 calling convention**：`llc -mtriple=minirv64 -O2 -stop-after=finalize-isel` 看参数寄存器；再和 Clang 生成的 IR 对照。两边对不上，就是 `ABIInfo` 或 `CallingConv.td` 的问题。
- **诊断优先**：MiniRV 应该拒绝 `-march=rv64gc`、`-mabi=lp64d`、TLS、scalable vector。早报错比生成错误代码好；Ch 23 的拒绝路径可以复用。
- **CI 顺序**：先 `check-clang` 的 CodeGen/Driver 子集，再跑 `dev/e2e`。前者快、定位准；后者慢、覆盖真。

## 本章重点整理

- “Clang 认识一个 target”是五层契约：`TargetInfo`、driver `ToolChain`/`Arch`、`CodeGen` `ABIInfo`、builtins、runtime。
- `llc` 用 LLVM `TargetRegistry`；Clang 用 `clang/lib/Basic/Targets.cpp:AllocateTarget`。两张表独立。
- `Triple::minirv64` 存在，只说明 triple 解析认识它；不说明 Clang 能构造 `TargetInfo`。
- RISC-V 的 `TargetInfo` 在 `Targets/RISCV.h/.cpp`；ABI 在 `CodeGen/Targets/RISCV.cpp`；builtins 是 `BuiltinsRISCV.td`。
- `clang --target=minirv64 -c` 报 `unknown target triple`；`-###` 却退出 0，因为 `-###` 不跑 cc1。
- 接 MiniRV 的顺序：契约 → `TargetInfo` → `AllocateTarget` → `ABIInfo` → `Arch`/`AddMiniRVTargetArgs` → `ToolChain`/链接 → builtins → runtime → 测试。
- 不要复用 `riscv::getRISCVABI`，除非先处理 `Triple::isRISCV()` 不包含 `minirv64` 的事实。
- 后端可以先于 Clang 有用：`llc`、`llvm-mc`、JIT、语言前端、固件、差分测试都不需要 Clang。

## 自我检核

- [ ] 不看笔记，画出 driver → frontend → IR → llc 的流程图，并标出三张注册表的位置。
- [ ] 解释为什么 `llc --version` 有 `minirv64`，而 `clang --target=minirv64 -c` 仍然失败。
- [ ] 解释 `-###` 为什么退出 0；它证明了什么、没证明什么。
- [ ] 说出 `TargetInfo` 至少五个职责，并各举一个 RISC-V 源码符号。
- [ ] 说出 `ABIInfo` 至少三个职责；解释 12 字节结构体在 RISC-V 和 x86-64 上的 IR 差异。
- [ ] 说出给 MiniRV 接 Clang 的 10 步，以及每步的验收命令。
- [ ] 解释为什么不能直接调用 `riscv::getRISCVABI`。
- [ ] 说出至少三种“没有 Clang 也能用后端”的用户，以及他们各自用什么工具。
- [ ] 列出至少三个“旧文档路径”和它们在当前树的真实位置。

## 延伸阅读

- **[Clang Driver Internals](https://clang.llvm.org/docs/DriverInternals.html)** — Clang 项目
  - **读哪里**：Driver 的阶段划分、`ToolChain`、`Job`、参数翻译。
  - **能学到什么**：`-###` 打印的到底是什么，为什么 `AddRISCVTargetArgs` 在 `Clang.cpp` 而不是 `Driver.cpp`。
  - **前提**：读过本章的 driver 一节，跑过一次 `clang -###`。
- **[Cross-compilation with Clang](https://clang.llvm.org/docs/CrossCompilation.html)** — Clang 项目
  - **读哪里**：`--target`、`--sysroot`、`-rtlib`、`--gcc-toolchain`、`-fuse-ld`。
  - **能学到什么**：一个 target 的“工具链”不只是编译器，还包括 crt0、库、链接器。
  - **前提**：知道裸机程序和 hosted 程序的区别。
- **[Clang RISC-V Support](https://github.com/llvm/llvm-project/blob/main/clang/docs/RISCVSupport.md)** — LLVM 项目
  - **读哪里**：支持的 `-march`/`-mabi` 组合、扩展、ABI 说明。
  - **能学到什么**：RISC-V 前端契约的官方描述，以及哪些行为是 stable、哪些是 experimental。
  - **前提**：了解 RISC-V psABI 的基本寄存器约定。
- **[Writing an LLVM Backend](https://llvm.org/docs/WritingAnLLVMBackend.html)** — LLVM 项目
  - **读哪里**：TargetMachine、指令选择、寄存器分配的入门流程。
  - **能学到什么**：后端边界在哪里，为什么它不负责 C 的类型系统和宏。
  - **前提**：读过 Ch 9–13。
- **[../riscv_backend/README.md](../riscv_backend/README.md)** — 本仓库课程
  - **读哪里**：RISC-V calling convention、栈帧、ABI 细节。
  - **能学到什么**：MiniRV 要遵循的 lp64 子集从哪里来。
  - **前提**：本章的 ABIInfo 一节。
- **[../llvm_lit/README.md](../llvm_lit/README.md)** — 本仓库课程
  - **读哪里**：`clang/test/CodeGen` 和 `clang/test/Driver` 的 RUN/CHECK 写法。
  - **能学到什么**：如何把“宏、flag、ABI IR”变成可回归的测试。
  - **前提**：能读 `-###` 和 `-emit-llvm` 的输出。

→ [Ch 27 上游化与评审](./27-upstreaming-and-review.md)
