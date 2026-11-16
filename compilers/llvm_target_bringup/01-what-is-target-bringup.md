# Ch 01 — 什么算“接入一个 target”

> **目标**：把“接入一个 target”从一句模糊的话，拆成七层可检查的边界：Triple/DataLayout → TargetRegistry/CMake → MC → CodeGen → AsmPrinter/MCInstLower → object/link → Clang/LLD/toolchain。学完你能判断一个 target 到底完成到哪一层，以及缺一层时会在哪条命令上炸。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

“接入一个 target”至少有四种含义，四种人给的答案都不一样：

- 汇编器维护者说：`llvm-mc` 能汇编、能反汇编，就算接入了。
- 编译器工程师说：`llc` 能从 IR 生成汇编，才算接入了。
- C/C++ 用户说：`clang --target=...` 能编程序、能链接、能跑，才算接入了。
- 上游 reviewer 说：测试、诊断、ABI、补丁拆分都齐了，才算接入了。

四种说法都不错，但它们说的是**不同的层**。如果不在同一层对话，就会发生这种争论：

> “MiniRV 不是已经能跑 17 个 e2e 程序了吗？”
> “那为什么 `clang --target=minirv64` 报错？”

两个陈述同时为真。MiniRV 走到了对象/链接/运行层，但没走到 Clang 工具链层。本章的任务就是给“接入”画一张分层图，让这类问题一眼就能定位。

七层不是 LLVM 官方术语，而是本课的操作性分层。官方文档会讲 TargetMachine、MC、CodeGen，但 bring-up 还要额外看 Triple、构建系统、object writer、驱动和 runtime。把它们放在一张图里，你才知道“下一步该补哪一层”。

## 先建立直觉

### 七层边界

```
+--------------------------------------------------------------------------------+
| 7  Clang / LLD / toolchain                                                     |
|    接口：clang TargetInfo、driver ToolChain、-target、builtins、runtime、sysroot |
|    产物：C/C++ 可执行文件                                                       |
+--------------------------------------------------------------------------------+
| 6  object / link                                                               |
|    接口：MCELFObjectTargetWriter、getRelocType、ELF machine、lld emulation      |
|    产物：.o、可执行 ELF、重定位                                                 |
+--------------------------------------------------------------------------------+
| 5  AsmPrinter / MCInstLower                                                    |
|    接口：AsmPrinter::emitInstruction、MCInstLower::Lower、EmitToStreamer        |
|    产物：带 directive 的 .s 文本                                                |
+--------------------------------------------------------------------------------+
| 4  CodeGen                                                                     |
|    接口：TargetMachine、Subtarget、TargetLowering、InstrInfo、RegisterInfo、     |
|          FrameLowering、SelectionDAGISel、pass pipeline                         |
|    产物：MachineInstr / MIR                                                     |
+--------------------------------------------------------------------------------+
| 3  MC                                                                          |
|    接口：MCAsmInfo、MCInstrInfo、MCRegisterInfo、MCSubtargetInfo、              |
|          MCInstPrinter、MCCodeEmitter、MCAsmBackend、AsmParser、Disassembler     |
|    产物：MCInst、指令字节、汇编/反汇编文本                                       |
+--------------------------------------------------------------------------------+
| 2  TargetRegistry / CMake                                                      |
|    接口：RegisterTarget、LLVMInitialize<T>TargetInfo/Target/TargetMC、.def       |
|    产物：Target*，可被 lookupTarget 找到                                         |
+--------------------------------------------------------------------------------+
| 1  Triple / DataLayout                                                         |
|    接口：Triple::ArchType、isMiniRV64、computeDataLayout、TargetMachine 基类     |
|    产物：arch 判定、指针宽度、对齐、endianness、mangling                         |
+--------------------------------------------------------------------------------+
```

这张图有两个读法：

- **从下往上**：每一层都依赖下面的层。没有 Triple，注册表不知道名字；没有 MC，CodeGen 生成的 MCInst 没法变成字节；没有 object writer，链接器没有输入。
- **从上往下**：越往下越“共享”、越稳定，越往上越“面向用户”、越容易缺。LLVM 的 target-independent 框架主要在下三层；target-specific 代码主要在上四层。

### 初始化链

`llc` 启动时，名字要经过这样一条链才变成 `Target*`：

```
llc main
  |
  +-- InitializeAllTargets()
  |     +-- InitializeAllTargetInfos()            // 先跑 Info
  |     |     #include "llvm/Config/Targets.def"
  |     |       LLVM_TARGET(MiniRV)
  |     |         -> LLVMInitializeMiniRVTargetInfo()   // RegisterTarget<Triple::minirv64>
  |     +-- #include "llvm/Config/Targets.def"    // 再跑 Target
  |           LLVM_TARGET(MiniRV)
  |             -> LLVMInitializeMiniRVTarget()         // RegisterTargetMachine<MiniRVTargetMachine>
  |
  +-- InitializeAllTargetMCs()
  |     -> LLVMInitializeMiniRVTargetMC()         // 8 个 RegisterMC* 调用
  |
  +-- InitializeAllAsmPrinters()
  |     -> LLVMInitializeMiniRVAsmPrinter()       // RegisterAsmPrinter<MiniRVAsmPrinter>
  |
  +-- InitializeAllAsmParsers()
  |     -> LLVMInitializeMiniRVAsmParser()        // RegisterMCAsmParser<MiniRVAsmParser>
  |
  +-- TargetRegistry::lookupTarget("minirv64") -> Target*
        |
        +-- createTargetMachine(...) -> MiniRVTargetMachine
```

注意 `Targets.def` 不是手写的，它由 CMake 根据 `LLVM_TARGETS_TO_BUILD` 生成。本机构建的四个 `.def` 文件里分别有 `LLVM_TARGET(MiniRV)`、`LLVM_ASM_PRINTER(MiniRV)`、`LLVM_ASM_PARSER(MiniRV)`、`LLVM_DISASSEMBLER(MiniRV)`。

`llc` 和 `llvm-mc` 调用的是不同的初始化子集：

- `llc`：`InitializeAllTargets`、`InitializeAllTargetMCs`、`InitializeAllAsmPrinters`、`InitializeAllAsmParsers`。
- `llvm-mc`：`InitializeAllTargetInfos`、`InitializeAllTargetMCs`、`InitializeAllAsmParsers`、`InitializeAllDisassemblers`。

这就解释了一个常见现象：同一个 target，`llc` 能用但 `llvm-mc -disassemble` 不能用，或者反过来。不是 target “坏了”，是某个 `.def` 条目或某个 `LLVMInitialize*` 符号没进初始化链。

## 主线：逐层看接口与缺失症状

### 第 1 层：Triple / DataLayout

**接口**：`Triple::ArchType` 里的枚举值、`Triple::isMiniRV64()`、`Triple::computeDataLayout()`，以及 `TargetMachine` 基类构造函数。

MiniRV 在 `llvm/include/llvm/TargetParser/Triple.h` 里加了：

```cpp
minirv64,    // MiniRV: demo MC-only RV64I (little endian): minirv64
...
bool isMiniRV() const { return getArch() == Triple::minirv64; }
bool isMiniRV64() const { return getArch() == Triple::minirv64; }
```

DataLayout 不是 MiniRV 自己在 TargetMachine 里写的。`CodeGenTargetMachineImpl` 的构造函数把 `TT.computeDataLayout(...)` 传给 `TargetMachine`，而 `llvm/lib/TargetParser/TargetDataLayout.cpp` 的 `Triple::computeDataLayout` 里有：

```cpp
case Triple::minirv64:
  return computeRISCVDataLayout(*this, ABIName);
```

所以 MiniRV 直接继承 RISC-V 的 64 位布局：`p:64:64`、`i64:64`、`i128:128`、`n32:64`、`S128`。

**缺了会怎样**：

- `llc -mtriple=minirv64` 无法把字符串解析成 arch，注册表也找不到对应 `Target`。
- DataLayout 错会让指针宽度、整数对齐、结构体布局全错。这类错误往往不报错，而是生成“看起来能跑但结果不对”的代码。
- Clang 连 triple 都不认，直接 `unknown target triple`。

**证据**：`llc -mtriple=minirv64 -O2 -stop-after=finalize-isel` 的 IR dump 里能看到：

```
target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
target triple = "minirv64"
```

这行 DataLayout 就是 `computeRISCVDataLayout` 的输出，不是 MiniRV 手写的。

### 第 2 层：TargetRegistry / CMake

**接口**：`RegisterTarget<Triple::minirv64>`、`LLVMInitializeMiniRVTargetInfo`、`LLVMInitializeMiniRVTarget`、`LLVMInitializeMiniRVTargetMC`，以及 CMake 的 `LLVM_TARGETS_TO_BUILD` / `Targets.def`。

`MiniRVTargetInfo.cpp` 里：

```cpp
Target &llvm::getTheMiniRVTarget() {
  static Target TheMiniRVTarget;
  return TheMiniRVTarget;
}

extern "C" LLVM_ABI LLVM_EXTERNAL_VISIBILITY void
LLVMInitializeMiniRVTargetInfo() {
  RegisterTarget<Triple::minirv64> X(getTheMiniRVTarget(), "minirv64",
                                     "MiniRV (auto-td demo)", "MiniRV");
}
```

`RegisterTarget` 这一层只负责“名字 → Target 对象”的映射。它不关心你有没有 CodeGen、有没有 AsmPrinter；那些是后续 `Register*` 调用的事。

**缺了会怎样**：

- `llc --version` 的 Registered Targets 里没有 `minirv64`。
- `llc -mtriple=minirv64` 报 `unable to get target for 'minirv64'` 之类的错误。
- `llvm-mc -triple=minirv64` 也找不到 target。

**证据**：`llc --version` 列出 `minirv64 - MiniRV (auto-td demo)`；本机构建的 `include/llvm/Config/Targets.def` 里有 `LLVM_TARGET(MiniRV)`。

**一个容易忽略的细节**：MC-only 阶段的 `LLVMInitializeMiniRVTarget` 是 MC 入口。`33d9ec8` 的 `MiniRVMCTargetDesc.cpp` 里明确写着：

```cpp
// This target has no TargetMachine, so LLVMInitializeMiniRVTarget (referenced
// from Targets.def) is the MC entry point rather than a CodeGen one.
```

到了 `ad74868`，`LLVMInitializeMiniRVTarget` 才移到 `MiniRVTargetMachine.cpp`，变成 `RegisterTargetMachine<MiniRVTargetMachine>`。同一个符号，前后含义完全不同。读 commit 时如果只看名字，会以为“注册早就有了，CodeGen 也早就有”，其实不是。

### 第 3 层：MC

**接口**：`MCAsmInfo`、`MCInstrInfo`、`MCRegisterInfo`、`MCSubtargetInfo`、`MCInstPrinter`、`MCCodeEmitter`、`MCAsmBackend`，外加 `MCTargetAsmParser` 和 `MCDisassembler`。

MiniRV 在 `MiniRVMCTargetDesc.cpp` 的 `LLVMInitializeMiniRVTargetMC()` 里做了 8 个注册：

```cpp
TargetRegistry::RegisterMCAsmInfo(T, createMiniRVMCAsmInfo);
TargetRegistry::RegisterMCInstrInfo(T, createMiniRVMCInstrInfo);
TargetRegistry::RegisterMCRegInfo(T, createMiniRVMCRegisterInfo);
TargetRegistry::RegisterMCSubtargetInfo(T, createMiniRVMCSubtargetInfo);
TargetRegistry::RegisterMCInstPrinter(T, createMiniRVMCInstPrinter);
TargetRegistry::RegisterMCCodeEmitter(T, createMiniRVMCCodeEmitter);
TargetRegistry::RegisterMCAsmBackend(T, createMiniRVAsmBackend);
TargetRegistry::RegisterNullTargetStreamer(T, createNullTargetStreamer);
```

MC 层是 target-independent 框架 + target-specific hook。`MCInst` 是“指令的抽象表示”，它既能由汇编器产生，也能由 CodeGen 的 AsmPrinter 产生。正因为 MC 不依赖 IR，一个 target 可以**只**有 MC，没有 CodeGen。

**缺了会怎样**：

- `llvm-mc` 无法解析 `.s`，或者能解析但编码器给不出字节。
- `llvm-objdump -d` 无法反汇编，或者打印的寄存器/立即数不对。
- `-filetype=obj` 没有 `MCAsmBackend` / object writer，写不出 ELF。

**证据**：

```console
$ printf 'add x10, x11, x12\n' > m.s
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-mc -triple=minirv64 -show-encoding m.s
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
```

`[0x33,0x85,0xc5,0x00]` 是 RV64 `add x10,x11,x12` 的小端编码。注意这条路径完全没有经过 `llc`：`llvm-mc` 直接把文本变成 MCInst，再变成字节。

### 第 4 层：CodeGen

**接口**：`TargetMachine`、`TargetSubtargetInfo`、`TargetLowering`、`TargetInstrInfo`、`TargetRegisterInfo`、`TargetFrameLowering`、`SelectionDAGISel`，以及 pass pipeline（MiniRV 用 `MiniRVCodeGenPassBuilder.cpp`）。

CodeGen 的入口是 `MiniRVTargetMachine.cpp` 里的：

```cpp
extern "C" LLVM_ABI LLVM_EXTERNAL_VISIBILITY void LLVMInitializeMiniRVTarget() {
  RegisterTargetMachine<MiniRVTargetMachine> X(getTheMiniRVTarget());
}
```

`MiniRVTargetMachine` 负责：

- 选择 relocation model / code model；
- 创建 `MiniRVSubtarget`；
- 提供 `TargetLowering`、`InstrInfo`、`RegisterInfo`、`FrameLowering`；
- 配置 `TargetLoweringObjectFile`（MiniRV 用 `TargetLoweringObjectFileELF`）；
- 声明不支持异常（`setSupportsExceptionModel(DwarfCFI, false)` 等）。

**缺了会怎样**：

- `llc` 能查到 target，但 `createTargetMachine` 失败，报 “target does not support code generation” 之类的错误。
- 或者 TargetMachine 有了，但 `TargetLowering` 没设置 operation action，遇到某类 IR 直接 `Cannot select`。
- 或者 ISel 能选指，但没有 register allocator 需要的 `RegisterInfo`，MIR 阶段崩。

**证据**：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 add3.ll -o -
	.file	"add3.ll"
	.text
	.globl	add3                            # -- Begin function add3
	.type	add3,@function
add3:                                   # @add3
	.cfi_startproc
# %bb.0:                                # %entry
	add	x3, x10, x11
	mul	x10, x3, x12
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	add3, .Lfunc_end0-add3
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

这条输出同时跨了第 4 层和第 5 层：`add/mul/jalr` 是 CodeGen 选出来的机器指令，`.globl/.cfi_startproc/.section` 是 AsmPrinter 打印的。

如果想只看第 4 层的产物，用：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel add3.ll -o -
```

输出会停在 `finalize-isel` 之后，是 MachineInstr/MIR，而不是最终汇编。这是区分“选指完成”和“汇编打印完成”的分界线。

### 第 5 层：AsmPrinter / MCInstLower

**接口**：`AsmPrinter::emitInstruction`、`MiniRVMCInstLower::Lower`、`EmitToStreamer`、`TargetLoweringObjectFile`。

`MiniRVAsmPrinter::emitInstruction` 的核心是：

```cpp
MiniRVMCInstLower MCInstLowering(OutContext, *this);
MCInstLowering.Lower(MI, TmpInst);
EmitToStreamer(*OutStreamer, TmpInst);
```

- `MCInstLower` 把 `MachineInstr` 的操作数（虚拟/物理寄存器、符号、frame index、全局地址）翻译成 `MCInst` 的操作数。
- `EmitToStreamer` 把 `MCInst` 交给 `MCStreamer`，后者调用 `MCInstPrinter` 打印文本，或调用 `MCCodeEmitter` 编成字节。

**缺了会怎样**：

- CodeGen 已经选好指令，但 `llc` 没有 AsmPrinter，可能在 `-filetype=asm` 时报 “no asm printer” 之类的错误。
- 或者 AsmPrinter 有了，但 `MCInstLower` 不处理某种操作数，打印出 `<mcsymbol>` 或直接崩。
- 或者 directive/CFI 不对，汇编能看但 `.cfi_startproc` 缺失，调试和 unwind 全乱。

**证据**：上面 `add3` 的 `.cfi_startproc`、`.type`、`.size`、`.section .note.GNU-stack` 都来自 AsmPrinter / MCAsmInfo / TLOF 的组合；`x10`、`x11`、`x12` 的打印来自 `MiniRVInstPrinter`。

这一层最容易背锅：ISel 选对了指令，但 AsmPrinter 打印错寄存器，看起来像“选指错了”。看到汇编不对，先确认是 MIR 错还是打印错。

### 第 6 层：object / link

**接口**：`MCELFObjectTargetWriter`、`getRelocType`、ELF machine type、`ld.lld` 的 emulation。

MiniRV 的 `MiniRVELFObjectWriter` 构造时传的是：

```cpp
MCELFObjectTargetWriter(/*Is64Bit=*/true, OSABI, ELF::EM_RISCV,
                        /*HasRelocationAddend=*/true)
```

也就是说，MiniRV 的对象文件在 ELF 层面就是 `EM_RISCV`。`getRelocType` 把 MiniRV 的 fixup 映射到 `R_RISCV_BRANCH`、`R_RISCV_JAL`、`R_RISCV_PCREL_HI20` 等既有重定位类型。

**缺了会怎样**：

- `llc -filetype=obj` 报 “no object writer”。
- 或者 object 写出来了，但 machine type 是 `EM_NONE`，链接器拒绝。
- 或者重定位类型错，链接时 `relocation R_RISCV_xxx against ... can not be used`，或者链接成功但运行跳飞。

**证据**：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -h add3.o
File: add3.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
ElfHeader {
  ...
  Type: Relocatable (0x1)
  Machine: EM_RISCV (0xF3)
  ...
}
```

（省略 `Ident` 等字段。）

`Format: elf64-littleriscv` 和 `Machine: EM_RISCV` 说明链接器会把它当 RISC-V 对象处理。e2e 里 `ld.lld -m elf64lriscv` 能直接链接，原因就在这里。

代价也要说清楚：**复用 `EM_RISCV` 让 MiniRV 省掉了 LLD 支持，但也让它无法在 ELF 层面与 RISC-V 区分**。链接器不会知道这是 MiniRV，relaxation、ABI 检查、平台识别都按 RISC-V 来。MiniRV 的 e2e 因此加了 `--no-relax`，把链接行为限制在它验证过的范围内。

### 第 7 层：Clang / LLD / toolchain

**接口**：Clang 的 `TargetInfo`、driver 的 `ToolChain`、`-target` 解析、builtins、runtime（compiler-rt、libc、libm）、sysroot、LLD 的 emulation。

这一层不是“再写一个 TargetMachine”，而是让 **C/C++ 前端和链接器**认识你的平台。RISC-V 的完整链路（`add3.c` 是 `long add3(long a, long b, long c) { return (a+b)*c; }`）：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -### -c add3.c
clang version 24.0.0git (https://github.com/nodecoda/llvm-project.git 1bfdad8e5c16f98c7a6aa531eb061fd2550ba169)
Target: riscv64
Thread model: posix
InstalledDir: /home/ubuntu/llvm/builds/pr-lane/bin
```

再实际编译：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -march=rv64im -mabi=lp64 -c add3.c -o add3.riscv.o
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -h add3.riscv.o | grep -E 'Format|Arch|Machine'
Format: elf64-littleriscv
Arch: riscv64
  Machine: EM_RISCV (0xF3)
```

MiniRV 在同一个位置失败：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -c add3.c -o add3.clang.o
error: unknown target triple 'minirv64'
```

**缺了会怎样**：

- `clang --target=<triple>` 直接拒绝，C/C++ 用户完全用不了。
- 或者 driver 接受了 triple，但找不到 assembler/linker/sysroot，链接阶段失败。
- 或者 ABI 不匹配（`lp64` vs `lp64d`、参数寄存器、栈对齐），编译能过但运行错。

**证据的边界**：本树的 `builds/dsl-tablegen/bin/clang` 有 LTO 符号问题（`undefined symbol ...SampleProfileFile...`），所以第 7 层的 driver 证据用 `builds/pr-lane/bin/clang`（同为 24.0.0git，commit `1bfdad8`）。这不影响 `llc`/`llvm-mc`/`llvm-readobj` 的证据，它们都来自 `dsl-tablegen`。

另外要注意：RISC-V 的 `clang -c` 能过，只说明**前端 + CodeGen + object** 这一段完整；本机这次只验证了 `-c`，完整链接 C 程序仍需要 runtime 和 libc/sysroot。M5 的“工具链”不只是“认识 triple”，还包括 runtime、头文件、库和 ABI 约定。

## MC-only、CodeGen、完整工具链：三种完成度

| 完成度 | 实现的层 | 用户能做什么 | 验收命令 | 本课例子 |
|---|---|---|---|---|
| MC-only | 1–3 + object writer | 手写汇编、反汇编、读 object | `llvm-mc`、`llvm-objdump`、`llvm-readobj` | MiniRV `33d9ec8` |
| CodeGen | 1–5 + object writer | 从 IR 生成汇编/对象 | `llc -mtriple=...` | MiniRV `ad74868` 之后 |
| Object/link/run | 1–6 | 链接并在 qemu 跑 | `ld.lld` + qemu + e2e | MiniRV `e186e2a` 之后 |
| 完整工具链 | 1–7 | 用 C/C++ 写程序 | `clang --target=...` | RISC-V |

三种完成度的关键区别不是“代码量”，而是**入口不同**：

- MC-only 的入口是 `.s` 文件。它能产出 `.o`，但那个 `.o` 只能来自手写汇编，不能来自 IR。
- CodeGen 的入口是 `.ll`。它能产出 `.s`/`.o`，但用户还得自己调用 `llc`，没有 C 前端。
- 完整工具链的入口是 `.c`/`.cpp`。用户只看到 `clang`，背后才走 driver → cc1 → CodeGen → MC → object → linker。

MiniRV 的例子说明“target 可以不完整但很有用”：它没有 Clang，但已经能验证 RV64 子集的编码、选指、ABI、栈帧、ELF 和链接。对固件/bootloader 场景，MC-only 就足够；对编译器实验，CodeGen 就足够；只有面向 C/C++ 用户时才需要第 7 层。

## 为什么 bring-up 要分阶段？

因为每一层都是**独立的契约**，测试工具、失败模式、依赖顺序和返工成本都不同。MC 层用 `llvm-mc`/`llvm-objdump`，CodeGen 用 `llc`/`llvm-lit`，工具链用 `clang`/`ld.lld`/qemu；没有一套测试能同时证明七层。

更实际的是风险：MC-only 的改动局部、可逆；一到 Clang/toolchain，就涉及 ABI、runtime、sysroot、平台约定，返工成本高一个数量级。先做下层是物理约束，不是教学偏好。

“不完整也有用”不是借口，而是工程策略：**每一层都可以独立交付、独立验收、独立被用户使用**。但必须诚实标注完成度。把 MC-only 说成“支持这个架构”，和把 e2e 全绿说成“Clang 能用”，都是过度承诺。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 1 Triple | `llvm/include/llvm/TargetParser/Triple.h` | `Triple::minirv64`、`isMiniRV64` | arch 枚举与判定 |
| 1 DataLayout | `llvm/lib/TargetParser/TargetDataLayout.cpp` | `Triple::computeDataLayout`、`computeRISCVDataLayout` | `minirv64` 复用 RISC-V 布局 |
| 2 注册 | `llvm/lib/Target/MiniRV/TargetInfo/MiniRVTargetInfo.cpp` | `RegisterTarget<Triple::minirv64>` | 名字 → Target |
| 2 初始化 | `llvm/tools/llc/lib/llcdriver.cpp` | `InitializeAllTargets()` 等 | llc 的初始化子集 |
| 2 初始化 | `llvm/tools/llvm-mc/llvm-mc.cpp` | `InitializeAllTargetInfos()` 等 | llvm-mc 的初始化子集 |
| 3 MC | `llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVMCTargetDesc.cpp` | `LLVMInitializeMiniRVTargetMC` | 8 个 `RegisterMC*` |
| 3 汇编 | `llvm/lib/Target/MiniRV/AsmParser/MiniRVAsmParser.cpp` | `LLVMInitializeMiniRVAsmParser` | 文本 → MCInst |
| 4 CodeGen | `llvm/lib/Target/MiniRV/MiniRVTargetMachine.cpp` | `LLVMInitializeMiniRVTarget` | CodeGen 入口 |
| 5 打印 | `llvm/lib/Target/MiniRV/MiniRVAsmPrinter.cpp` | `MiniRVAsmPrinter::emitInstruction` | MI → MCInst → streamer |
| 5 降级 | `llvm/lib/Target/MiniRV/MiniRVMCInstLower.cpp` | `MiniRVMCInstLower::Lower` | 操作数翻译 |
| 6 对象 | `llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVELFObjectWriter.cpp` | `MiniRVELFObjectWriter` | `ELF::EM_RISCV`、relocation |
| 7 Clang | `clang/lib/Driver/ToolChains/`、`clang/lib/Basic/Targets/` | RISC-V 参照 | driver/TargetInfo 的接入点 |

## 实测：用命令切开七层

### 第 3 层：`llvm-mc` 只走 MC

```console
$ printf 'add x10, x11, x12\n' > m.s
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-mc -triple=minirv64 -show-encoding m.s
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
```

这条命令不经过 IR、不经过 TargetMachine、不经过寄存器分配。它证明的是第 1–3 层：triple 能解析、MC 注册存在、AsmParser 能匹配、CodeEmitter 能编码。

### 第 4–5 层：`llc` 从 IR 到汇编

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 add3.ll -o -
	.file	"add3.ll"
	.text
	.globl	add3                            # -- Begin function add3
	.type	add3,@function
add3:                                   # @add3
	.cfi_startproc
# %bb.0:                                # %entry
	add	x3, x10, x11
	mul	x10, x3, x12
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	add3, .Lfunc_end0-add3
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

看到这段输出，说明第 4 层（TargetMachine/Lowering/ISel/regalloc）和第 5 层（AsmPrinter/MCInstLower）都通了。但**不能**说明第 6 层：`-o -` 输出的是文本，不是 ELF。

### 第 6 层：`llvm-readobj` 看 ELF

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 -filetype=obj add3.ll -o add3.o
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -h add3.o
File: add3.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
ElfHeader {
  ...
  Type: Relocatable (0x1)
  Machine: EM_RISCV (0xF3)
  ...
}
```

（为节省篇幅，`readobj` 这一段省略了 `Ident` 和 header 的其余字段。）

`Format`/`Machine` 是第 6 层的证据。它证明 object writer 存在、ELF machine 正确。再加上 `ld.lld -m elf64lriscv` 能链接、qemu 能运行，第 6 层才算完整。

### 第 7 层：`clang` 认不认这个 triple

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=riscv64 -### -c add3.c
clang version 24.0.0git (https://github.com/nodecoda/llvm-project.git 1bfdad8e5c16f98c7a6aa531eb061fd2550ba169)
Target: riscv64
Thread model: posix
InstalledDir: /home/ubuntu/llvm/builds/pr-lane/bin

$ /home/ubuntu/llvm/builds/pr-lane/bin/clang --target=minirv64 -c add3.c -o add3.clang.o
error: unknown target triple 'minirv64'
```

对比非常直接：RISC-V 有 Clang TargetInfo 和 driver toolchain，MiniRV 没有。`llc` 能用不代表 `clang` 能用，这就是第 4 层和第 7 层的边界。

## 反直觉与踩雷

1. **错误直觉**：`clang --target=minirv64 -###` 打印了 `Target: minirv64`，说明 Clang 支持它。 → **正确认识**：`-###` 只回显 driver 计划，不校验 target。实际 `clang --target=minirv64 -c` 会报 `unknown target triple 'minirv64'`。

2. **错误直觉**：`llc --version` 里有 `minirv64`，CodeGen 就一定完整。 → **正确认识**：注册只证明第 2 层。MC-only commit 明确写了 “This target has no TargetMachine”，那时 `llc` 不能从 IR 生成代码。

3. **错误直觉**：MiniRV 的 ELF 是 `EM_RISCV`，所以它就是 RISC-V。 → **正确认识**：它只是复用了 RISC-V 的 machine type 和重定位类型。链接器会按 RISC-V 规则处理它，relaxation、平台识别、ABI 检查都不区分 MiniRV。这是便利，也是边界。

## 进阶：再往深一层

- **读 `TargetRegistry` 的 `RegisterTarget` 和 `lookupTarget`**。你会看到注册表只是一个 `Target` 链表，`lookupTarget` 按 triple 的 arch 匹配；`createTargetMachine` 是虚函数，具体实现由 TargetMachine 子类提供。
- **用 `-stop-after` 切层**。`-stop-after=finalize-isel` 看 ISel 产物；`-stop-after=prologepilog` 看 frame lowering 后的 MIR；不加 `-stop-after` 看最终汇编。每次只往前推一层，比盯着最终汇编猜要快。

## 本章重点整理

- “接入”有七层：Triple/DataLayout、TargetRegistry/CMake、MC、CodeGen、AsmPrinter/MCInstLower、object/link、Clang/LLD/toolchain。
- 每层都有独立接口、独立产物、独立验收命令；`llvm-mc`、`llc`、`llvm-readobj`、`clang` 分别守不同的门。
- MC-only 覆盖 1–3 层和 object writer，CodeGen 覆盖 1–5 层和 object writer，object/link/run 到第 6 层，完整工具链才到第 7 层。
- MiniRV 走到第 6 层，RISC-V 走到第 7 层；这个差距不是“代码多少”，而是“入口不同”。
- 分阶段不是为了好看，而是因为每层的失败模式、测试工具、返工成本都不同。
- 复用 `EM_RISCV` 让 MiniRV 省掉 LLD 工作，但也让它无法在 ELF 层面与 RISC-V 区分。

## 自我检核

- [ ] 不看笔记，按顺序说出七层，并给每层一个接口名和一个验收命令。
- [ ] 解释 `llc --version` 有 `minirv64`、但 `clang --target=minirv64 -c` 失败，分别断在哪一层。
- [ ] 解释 `llvm-mc` 能出 `.o` 为什么不等于 CodeGen 完整。
- [ ] 说出 `LLVMInitializeMiniRVTarget` 在 `33d9ec8` 和 `ad74868` 之后分别是什么含义。
- [ ] 解释 `EM_RISCV` 复用带来的两个好处和两个边界。
- [ ] 解释为什么 `clang -###` 不能证明 target 被支持。

## 延伸阅读

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)** — LLVM 官方
  - **读哪里**：“The LLVM Target-Independent Code Generator” 和 “Target Machine” 两节。
  - **能学到什么**：第 4 层 TargetMachine/Subtarget/ISel 的官方架构，以及 target-independent 框架如何调用 target hook。
  - **前提**：能读基本 LLVM IR，知道 `llc` 的输入输出。

- **[LLVM MC Layer](https://llvm.org/docs/CommandGuide/llvm-mc.html)** — LLVM 官方
  - **读哪里**：`-show-encoding`、`-disassemble`、`-filetype=obj` 的语义。
  - **能学到什么**：第 3 层为什么可以脱离 CodeGen 独立存在，以及 `llvm-mc` 的输入输出边界。
  - **前提**：能在命令行跑 `llvm-mc`。

- **[LLVM TargetRegistry 头文件](https://llvm.org/doxygen/TargetRegistry_8h_source.html)** — LLVM doxygen
  - **读哪里**：`RegisterTarget`、`RegisterTargetMachine`、`lookupTarget`、`createTargetMachine`。
  - **能学到什么**：第 2 层的注册表数据结构和查找流程，以及为什么初始化顺序重要。
  - **前提**：能读简单 C++ 模板和函数指针。

- **[RISC-V 后端课程](../riscv_backend/README.md)** — 本仓库
  - **读哪里**：MC、ISel、ABI、frame lowering 章节。
  - **能学到什么**：本课只给“第 7 层参照点”的地方，RISC-V 有完整实现和 ISA 细节。
  - **前提**：先读完本章，知道自己要找哪一层。

→ [Ch 02 RISC-V 作为参照](./02-riscv-as-reference.md)
