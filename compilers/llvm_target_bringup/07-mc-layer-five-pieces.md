# Ch 07 — MC 层五件套

> **目标**：拆开 `MCAsmInfo`/`MCInstrInfo`/`MCRegisterInfo`/`MCSubtargetInfo`/`MCInstPrinter` 的注册与生命周期，并说清 `MCCodeEmitter`、`MCAsmBackend` 为什么也绕不过去。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。下文用 `$TREE`、`$BUILD`、`$B=$BUILD/bin` 指代。

## 为什么需要这个？

上一章我们让 TableGen 生成了 `MiniRVGenInstrInfo.inc`、`MiniRVGenAsmWriter.inc`、`MiniRVGenDisassemblerTables.inc`。但这些只是表。真正的问题是：**谁持有这些表，谁在什么时候调用它们？**

`llvm-mc` 只认识 `Target` 接口，不认识 `MiniRV`；`llc` 只认识 `TargetMachine` 和 MC 抽象；`llvm-objdump` 只认识 `MCDisassembler` 和 `MCInstPrinter`。如果每个工具都直接 `#include "MiniRVGenInstrInfo.inc"`，LLVM 就会变成一堆 if-else，新增 target 要改所有工具。MC 层的设计是：target 在 `MCTargetDesc` 里**注册工厂函数**，工具通过 `TargetRegistry` 拿到抽象对象。target 无关的代码只依赖抽象基类，target 相关的知识被关在 `MiniRV*` 子类里。

所以本章要回答三件事：

1. 五件套各自负责什么、由哪个工厂创建、谁调用。
2. `MiniRVMCTargetDesc.cpp` 里那几个 `GET_*_MC_DESC` 到底把什么代码拉进来。
3. 为什么 `InitMCRegisterInfo(X, MiniRV::X1)` 的第二个参数和 `addInitialFrameState()` 会直接影响 unwind。

## 先建立直觉

三条数据流，三种消费者：

```
.s  ──MCAsmParser──> MCInst
                      ├──MCCodeEmitter──> 字节+fixup ──MCAsmBackend──> .o
                      └──MCInstPrinter──> 汇编文本

.o  ──MCDisassembler──> MCInst ──MCInstPrinter──> 反汇编文本

.ll ──CodeGen──> MachineInstr ──AsmPrinter/MCInstLower──> MCInst
                                                   ├──MCInstPrinter──> .s
                                                   └──MCCodeEmitter+MCAsmBackend──> .o
```

`TargetRegistry` 是这三条流的“服务发现”：

```
llvm-mc / llc / llvm-objdump
        │  TheTarget->createMC*
        ▼
LLVMInitializeMiniRVTargetMC() 里注册的函数指针
        ▼
MiniRVMCAsmInfo / MCRegisterInfo / MCInstrInfo / ...
```

## 主线：五件套 + 两件

### 1. 每个对象负责什么

| 对象 | 负责的内容 | 数据来源 | 主要调用者 |
|---|---|---|---|
| `MCAsmInfo` | 汇编语法契约：注释符、指针对齐、指令对齐、FDE relocation、初始 CFA | `MiniRVMCAsmInfo.cpp` | `llvm-mc`、`llc`、`MCStreamer`/`MCContext` |
| `MCInstrInfo` | opcode → `MCInstrDesc`：操作数个数、隐式 use/def、`mayLoad`/`mayStore`、terminator 等标志 | `MiniRVGenInstrInfo.inc` | 汇编器、编码器、反汇编器、`AsmPrinter` |
| `MCRegisterInfo` | 寄存器描述、寄存器类、DWARF 编号、编码值、返回地址寄存器 | `MiniRVGenRegisterInfo*.inc` | 编码器、反汇编器、CFI、寄存器分配 |
| `MCSubtargetInfo` | CPU、feature bits、调度模型、指令延迟/吞吐 | `MiniRVGenSubtargetInfo.inc` | 汇编器、`llc`、`MCInstPrinter` |
| `MCInstPrinter` | `MCInst` → 汇编文本；寄存器名、立即数、表达式 | `MiniRVGenAsmWriter.inc` | `llvm-mc`、`llc` 汇编输出、`objdump` |
| `MCCodeEmitter` | `MCInst` → 字节；表达式操作数 → `MCFixup` | `MiniRVGenMCCodeEmitter.inc` | `llvm-mc -filetype=obj`、`llc -filetype=obj` |
| `MCAsmBackend` | fixup 应用、relocation 类型、NOP 填充、object writer | `MiniRVAsmBackend.cpp` | 同上 |

前五个是“五件套”，后两个是“要把字节写进文件”时必然出现的两件。严格说它们不是同一层：五件套描述“指令长什么样”，CodeEmitter/AsmBackend 描述“怎么变成文件”。但你在 `MCTargetDesc` 里注册时，它们是一起出现的。

### 2. `MiniRVMCTargetDesc.cpp` 逐段读

文件开头先选生成物的“段”：

```cpp
// MiniRVMCTargetDesc.cpp:23-31
#define GET_INSTRINFO_MC_DESC
#define ENABLE_INSTR_PREDICATE_VERIFIER
#include "MiniRVGenInstrInfo.inc"

#define GET_SUBTARGETINFO_MC_DESC
#include "MiniRVGenSubtargetInfo.inc"

#define GET_REGINFO_MC_DESC
#include "MiniRVGenRegisterInfo.inc"
```

`MiniRVGenInstrInfo.inc` 是一个多段文件：`GET_INSTRINFO_ENUM` 只给枚举，`GET_INSTRINFO_MC_DESC` 给 `MCInstrDesc` 表和 `InitMiniRVMCInstrInfo()`。同一个 `.inc` 被不同 `.cpp` 用不同宏包含，各自只拿到自己需要的段。这是 LLVM 生成物最常见的组织方式。

- `GET_INSTRINFO_MC_DESC`：拉进 `MiniRVInstrTable`、`MiniRVDescs`、操作数信息、`InitMiniRVMCInstrInfo(MCInstrInfo *II)`。
- `GET_SUBTARGETINFO_MC_DESC`：拉进 `MiniRVGenMCSubtargetInfo`、feature/CPU 表、调度表、`createMiniRVMCSubtargetInfoImpl()`。
- `GET_REGINFO_MC_DESC`：拉进 `MiniRVRegDesc`、寄存器类存储、`InitMiniRVMCRegisterInfo(MCRegisterInfo *RI, unsigned RA, ...)`。
- `ENABLE_INSTR_PREDICATE_VERIFIER`：让 `MCInstrInfo` 带上 `verifyInstructionPredicates()`，在断言构建里检查操作数谓词。

然后是一组工厂：

```cpp
// MiniRVMCTargetDesc.cpp:33-43
static MCInstrInfo *createMiniRVMCInstrInfo() {
  MCInstrInfo *X = new MCInstrInfo();
  InitMiniRVMCInstrInfo(X);
  return X;
}

static MCRegisterInfo *createMiniRVMCRegisterInfo(const Triple &TT) {
  MCRegisterInfo *X = new MCRegisterInfo();
  InitMiniRVMCRegisterInfo(X, MiniRV::X1);
  return X;
}
```

`createMiniRVMCInstrInfo()` 只是 `new` 一个空壳，再让生成的 `Init...` 填表。`createMiniRVMCRegisterInfo()` 的 `MiniRV::X1` 是**返回地址寄存器**，不是栈指针、不是 PC。下一节专门讲。

```cpp
// MiniRVMCTargetDesc.cpp:45-55
static MCAsmInfo *createMiniRVMCAsmInfo(const MCRegisterInfo &MRI,
                                        const Triple &TT,
                                        const MCTargetOptions &Options) {
  MCAsmInfo *MAI = new MiniRVMCAsmInfo(TT, Options);
  unsigned SP = MRI.getDwarfRegNum(MiniRV::X2, /*IsEH=*/true);
  MAI->addInitialFrameState(MCCFIInstruction::cfiDefCfa(nullptr, SP, 0));
  return MAI;
}
```

注意参数顺序：`createMCAsmInfo` 接收已经创建好的 `MRI`，所以这里能查 `X2` 的 DWARF 编号。也注意 `getDwarfRegNum(X2, true)` 的第二个参数是 `IsEH`：CFI 用的是 EH 编号空间，不是 debug 编号空间。

```cpp
// MiniRVMCTargetDesc.cpp:57-71
static MCSubtargetInfo *createMiniRVMCSubtargetInfo(const Triple &TT,
                                                    StringRef CPU,
                                                    StringRef FS) {
  return createMiniRVMCSubtargetInfoImpl(TT, CPU, /*TuneCPU=*/CPU, FS);
}

static MCInstPrinter *createMiniRVMCInstPrinter(const Triple &T,
                                                unsigned SyntaxVariant,
                                                const MCAsmInfo &MAI,
                                                const MCInstrInfo &MII,
                                                const MCRegisterInfo &MRI) {
  if (SyntaxVariant == 0)
    return new MiniRVInstPrinter(MAI, MII, MRI);
  return nullptr;
}
```

`createMiniRVMCSubtargetInfoImpl()` 在 `MiniRVGenSubtargetInfo.inc` 里，内部 `new MiniRVGenMCSubtargetInfo(...)`，把 CPU 表、feature 表、调度表交给基类。`SyntaxVariant == 0` 表示只有一种汇编方言；如果 target 要 AT&T/Intel 两种语法，这里会按 variant 返回不同 printer。

```cpp
// MiniRVMCTargetDesc.cpp:73-75
static MCTargetStreamer *createNullTargetStreamer(MCStreamer &S) {
  return new MCTargetStreamer(S);
}
```

MiniRV 没有 target-specific 的 streamer，所以注册一个空实现。RISC-V 在这里注册的是 `RISCVTargetStreamer` / `RISCVELFStreamer`，后面会对比。

最后是注册入口：

```cpp
// MiniRVMCTargetDesc.cpp:77-89
extern "C" LLVM_ABI LLVM_EXTERNAL_VISIBILITY void
LLVMInitializeMiniRVTargetMC() {
  Target &T = getTheMiniRVTarget();

  TargetRegistry::RegisterMCAsmInfo(T, createMiniRVMCAsmInfo);
  TargetRegistry::RegisterMCInstrInfo(T, createMiniRVMCInstrInfo);
  TargetRegistry::RegisterMCRegInfo(T, createMiniRVMCRegisterInfo);
  TargetRegistry::RegisterMCSubtargetInfo(T, createMiniRVMCSubtargetInfo);
  TargetRegistry::RegisterMCInstPrinter(T, createMiniRVMCInstPrinter);
  TargetRegistry::RegisterMCCodeEmitter(T, createMiniRVMCCodeEmitter);
  TargetRegistry::RegisterMCAsmBackend(T, createMiniRVAsmBackend);
  TargetRegistry::RegisterNullTargetStreamer(T, createNullTargetStreamer);
}
```

这八行就是“MC 层对 LLVM 的全部承诺”。少注册一个，某个工具就会在运行时说“target does not support X”。比如没注册 `RegisterMCCodeEmitter`，`llvm-mc -filetype=obj` 会报无法创建 code emitter；没注册 `RegisterMCInstPrinter`，`llvm-mc` 的默认 echo 和 `llc` 的汇编输出都会失败。

### 3. 五件套之外：CodeEmitter 与 AsmBackend

`MiniRVMCCodeEmitter` 在 `MiniRVMCCodeEmitter.cpp`：

```cpp
void MiniRVMCCodeEmitter::encodeInstruction(const MCInst &MI,
                                            SmallVectorImpl<char> &CB,
                                            SmallVectorImpl<MCFixup> &Fixups,
                                            const MCSubtargetInfo &STI) const {
  uint64_t Bits = getBinaryCodeForInstr(MI, Fixups, STI);
  size_t N = CB.size();
  CB.resize(N + 4);
  support::endian::write32le(&CB[N], Bits);
}
```

`getBinaryCodeForInstr()` 来自 `MiniRVGenMCCodeEmitter.inc`，里面是一张 `InstBits[]` 和按 opcode 的 switch。`getMachineOpValue()` 处理寄存器/立即数/表达式：寄存器走 `MCRegisterInfo::getEncodingValue()`，表达式走 fixup。

`MiniRVAsmBackend` 负责“字节写下去之后”的事：

- `getFixupKindInfo()`：告诉通用代码每个 target fixup 占 32 位。
- `applyFixup()`：把 branch/jal/pcrel_hi/pcrel_lo 的值按位散进指令字；数据 fixup 直接按小端写。
- `evaluateFixup()`：对 `pcrel_lo12_i` 找到对应的 `pcrel_hi20`，算出相对 `auipc` 的偏移。
- `writeNopData()`：写 `addi x0, x0, 0`（`0x13 0x00 0x00 0x00`）。
- `createObjectTargetWriter()`：返回 `MiniRVELFObjectWriter`。

`MiniRVELFObjectWriter` 的构造函数里有一个关键选择：

```cpp
MCELFObjectTargetWriter(/*Is64Bit=*/true, OSABI, ELF::EM_RISCV,
                        /*HasRelocationAddend=*/true)
```

MiniRV 没有自己的 `EM_*`，它复用 `EM_RISCV`，relocation 也用 `R_RISCV_BRANCH`、`R_RISCV_JAL`、`R_RISCV_PCREL_HI20` 等。这让你能直接用 `ld.lld` 和 `qemu-riscv64-static` 跑 MiniRV 程序，代价是工具链会把它当成 RISC-V。第 22 章会展开这个取舍。

### 4. 谁在调用这些对象

`llvm-mc` 的调用顺序在 `llvm/tools/llvm-mc/llvm-mc.cpp`：

```cpp
// llvm-mc.cpp:483,487,520,646,652,686,689
std::unique_ptr<MCRegisterInfo> MRI(TheTarget->createMCRegInfo(TheTriple));
std::unique_ptr<MCAsmInfo> MAI(
    TheTarget->createMCAsmInfo(*MRI, TheTriple, MCOptions));
std::unique_ptr<MCSubtargetInfo> STI(
    TheTarget->createMCSubtargetInfo(TheTriple, MCPU, FeaturesStr));
std::unique_ptr<MCInstrInfo> MCII(TheTarget->createMCInstrInfo());
IP.reset(TheTarget->createMCInstPrinter(Triple(TripleName), AsmVariant, ...));
CE.reset(TheTarget->createMCCodeEmitter(*MCII, Ctx));
MAB.reset(TheTarget->createMCAsmBackend(*STI, *MRI, MCOptions));
```

顺序很重要：先有 `MRI`，才能创建 `MCAsmInfo`（因为 `addInitialFrameState` 要查 DWARF 号）；先有 `MCII`，才能创建 `MCCodeEmitter`。`llvm-mc` 用 `MAB` + `CE` + `MCStreamer` 把 `.s` 变成 `.o`；用 `IP` 把解析后的 `MCInst` echo 回文本。

`llc` 走的是 `CodeGenTargetMachineImpl::createMCStreamer()`：

- 汇编文件：`createMCInstPrinter()` → `createAsmStreamer()`。
- 目标文件：`createMCCodeEmitter()` → `createMCAsmBackend()` → `createMCObjectStreamer()`。

`llvm-objdump` 走的是 `llvm/tools/llvm-objdump/llvm-objdump.cpp`：

```cpp
// llvm-objdump.cpp:1145,1155,1159,1171,1183
RegisterInfo(TheTarget->createMCRegInfo(TheTriple))
SubtargetInfo(TheTarget->createMCSubtargetInfo(TheTriple, MCPU, ...))
InstrInfo.reset(TheTarget->createMCInstrInfo());
DisAsm.reset(TheTarget->createMCDisassembler(*SubtargetInfo, *Context));
InstPrinter.reset(TheTarget->createMCInstPrinter(...));
```

objdump 不需要 `MCAsmInfo`，也不需要 `MCCodeEmitter`/`MCAsmBackend`；它要的是 `MCDisassembler`（读字节）和 `MCInstPrinter`（写文本）。

### 5. `InitMCRegisterInfo(X, MiniRV::X1)`：第二个参数是什么

生成的函数签名是：

```cpp
static inline void InitMiniRVMCRegisterInfo(MCRegisterInfo *RI, unsigned RA,
                                            unsigned DwarfFlavour = 0,
                                            unsigned EHFlavour = 0,
                                            unsigned PC = 0) {
  RI->InitMCRegisterInfo(..., RA, PC, ...);
}
```

`RA` 一路传到 `MCRegisterInfo::InitMCRegisterInfo()` 的 `unsigned RA` 参数，存进 `RAReg`：

```cpp
void InitMCRegisterInfo(const MCRegisterDesc *D, unsigned NR, unsigned RA,
                        unsigned PC, ...) {
  ...
  RAReg = RA;
  PCReg = PC;
  ...
}
```

`getRARegister()` 返回它。CFI 生成 CIE 时这样用：

```cpp
// llvm/lib/MC/MCDwarf.cpp
unsigned RAReg = Frame.RAReg;
if (RAReg == static_cast<unsigned>(INT_MAX))
  RAReg = MRI->getDwarfRegNum(MRI->getRARegister(), IsEH);
```

也就是说，`.eh_frame` 的 CIE 里 `return_address_register` 字段就是 `MRI->getDwarfRegNum(X1, true)` 的结果。MiniRV 的 `MiniRVReg` 写了 `DwarfNumbers = [num]`，所以 `X1` 的 DWARF 号是 1。实测对象的 CIE 正是：

```console
$ $B/llvm-readobj --unwind /tmp/cfi.o | sed -n '1,16p'

File: cfi.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
.eh_frame section at offset 0xb8 address 0x0:
  [0x0] CIE length=16
    version: 1
    augmentation: zR
    code_alignment_factor: 1
    data_alignment_factor: -8
    return_address_register: 1

    Program:
      DW_CFA_def_cfa: reg2 +0
```

为什么重要：unwinder 在每层栈帧里要知道“返回地址保存在哪个寄存器/哪个 DWARF 列”。如果这里传 `X0`，CIE 会写 `return_address_register: 0`，unwinder 会去读永远为 0 的 `x0` 当返回地址，backtrace 直接断掉；如果传 `X2`，它会把栈指针当返回地址，结果更糟。这个参数和 `.cfi_offset x1, -8` 必须配套：`x1` 的 DWARF 号、CIE 的 RA 字段、FDE 里的 offset 都指向同一个列。

### 6. `addInitialFrameState`：CFA 的起点

`createMiniRVMCAsmInfo()` 里：

```cpp
unsigned SP = MRI.getDwarfRegNum(MiniRV::X2, /*IsEH=*/true);
MAI->addInitialFrameState(MCCFIInstruction::cfiDefCfa(nullptr, SP, 0));
```

`getDwarfRegNum(X2, true)` 返回 2，所以初始规则是“CFA = `x2` + 0”，即函数入口时的栈指针。这个规则进入 `MCAsmInfo::InitialFrameState`。`MCStreamer::emitCFIStartProc()` 会遍历它，把 `CurrentCfaRegister` 设成 `x2`；`MCDwarf` 在生成 CIE 时把初始指令写进去（只对 non-simple frame）：

```cpp
if (!Frame.IsSimple) {
  const std::vector<MCCFIInstruction> &Instructions =
      MAI.getInitialFrameState();
  emitCFIInstructions(Instructions, nullptr);
}
```

实测 CIE 里的 `DW_CFA_def_cfa: reg2 +0` 就是这一行。没有它，unwinder 不知道 CFA 的基准；prologue 里的 `.cfi_def_cfa_offset 16` 会变成“相对一个未知基址 +16”，栈回溯和异常展开都会错。

这里有一个容易误判的现象：`llc` 输出 leaf 函数时，汇编里**看不到** `.cfi_def_cfa x2, 0`。比如：

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/add3.ll -o - | sed -n '1,12p'
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
```

因为 `add3` 没有栈帧、没有 callee-saved 寄存器，frame 是 simple 的，`MCDwarf` 不把初始指令写进 FDE/CIE 的可见汇编。换成带 `uwtable` 且需要保存寄存器的函数，CIE 里就会出现那条 `DW_CFA_def_cfa`。所以“汇编里没有”不等于“对象里没有”。

### 7. 和 RISC-V 的差异

RISC-V 的注册块更长：

```cpp
// RISCV/MCTargetDesc/RISCVMCTargetDesc.cpp
LLVMInitializeRISCVTargetMC() {
  static opt::RegisterLibraryOptions<RISCVMCOptions> O;
  for (Target *T : {&getTheRISCV32Target(), &getTheRISCV64Target(),
                    &getTheRISCV32beTarget(), &getTheRISCV64beTarget()}) {
    TargetRegistry::RegisterMCAsmInfo(*T, createRISCVMCAsmInfo);
    TargetRegistry::RegisterMCObjectFileInfo(*T, createRISCVMCObjectFileInfo);
    TargetRegistry::RegisterMCInstrInfo(*T, createRISCVMCInstrInfo);
    TargetRegistry::RegisterMCRegInfo(*T, createRISCVMCRegisterInfo);
    TargetRegistry::RegisterMCAsmBackend(*T, createRISCVAsmBackend);
    TargetRegistry::RegisterMCCodeEmitter(*T, createRISCVMCCodeEmitter);
    TargetRegistry::RegisterMCInstPrinter(*T, createRISCVMCInstPrinter);
    TargetRegistry::RegisterMCSubtargetInfo(*T, createRISCVMCSubtargetInfo);
    TargetRegistry::RegisterELFStreamer(*T, createRISCVELFStreamer);
    TargetRegistry::RegisterMachOStreamer(*T, createMachOStreamer);
    TargetRegistry::RegisterObjectTargetStreamer(
        *T, createRISCVObjectTargetStreamer);
    TargetRegistry::RegisterMCInstrAnalysis(*T, createRISCVInstrAnalysis);
    TargetRegistry::RegisterAsmTargetStreamer(*T, createRISCVAsmTargetStreamer);
    TargetRegistry::RegisterNullTargetStreamer(*T,
                                               createRISCVNullTargetStreamer);
  }
}
```

多出来的每一项都不是装饰：

- `RegisterMCObjectFileInfo`：`.eh_frame`、`.riscv.attributes` 等 section 的布局；MiniRV 直接用通用 ELF 布局。
- `RegisterELFStreamer` / `RegisterMachOStreamer`：target-specific 的 streamer，处理 `.option`、relaxation、Mach-O 的特殊指令。MiniRV 只注册 `NullTargetStreamer`。
- `RegisterObjectTargetStreamer`：对象级的 target 指令/属性。
- `RegisterMCInstrAnalysis`：给 objdump/反汇编器提供分支目标、基本块边界分析；MiniRV 没注册，所以它只能做线性反汇编。
- `RegisterAsmTargetStreamer`：汇编文本层面的 `.attribute`、`.option arch`、`.variant_cc`。
- `RISCVMCOptions`：library options，控制 RISC-V 特有的 MC 行为。
- 四个 triple：`riscv32`、`riscv64`、`riscv32be`、`riscv64be`。MiniRV 只有一个 `minirv64`。
- `RISCVMCAsmInfoDarwin`：Mach-O 的语法差异；MiniRV 只支持 ELF。

判断规则：**先只注册五件套 + emitter/backend，能跑 `llvm-mc`、`llc`、`objdump` 就够了**。当你要支持 target-specific 伪指令、section、属性、分支分析、Mach-O 时，再逐个加 streamer/analysis。MiniRV 的选择是“ELF-only、无 target directive、无分支分析”，所以注册表最短。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| MC 注册 | `MCTargetDesc/MiniRVMCTargetDesc.cpp` | `LLVMInitializeMiniRVTargetMC`、`createMiniRVMC*` | 八个 `Register*` 的顺序与参数 |
| 生成物接线 | 同上 | `GET_INSTRINFO_MC_DESC`、`GET_SUBTARGETINFO_MC_DESC`、`GET_REGINFO_MC_DESC` | 多段 `.inc` 如何被选中 |
| 汇编属性 | `MCTargetDesc/MiniRVMCAsmInfo.cpp` | `CodePointerSize`、`CommentString`、`DwarfFDERelSymbolSpec` | `addInitialFrameState` 的落点 |
| 打印器 | `MCTargetDesc/MiniRVInstPrinter.cpp` | `printInst`、`printOperand`、`include GenAsmWriter` | 生成函数如何拼进 C++ 类 |
| 编码器 | `MCTargetDesc/MiniRVMCCodeEmitter.cpp` | `encodeInstruction`、`getMachineOpValue`、`getBranchTargetOpValue` | 字节 + fixup 的产生 |
| 汇编后端 | `MCTargetDesc/MiniRVAsmBackend.cpp` | `applyFixup`、`evaluateFixup`、`writeNopData` | fixup 如何散进位域 |
| ELF writer | `MCTargetDesc/MiniRVELFObjectWriter.cpp` | `MCELFObjectTargetWriter(..., EM_RISCV, ...)` | relocation 映射 |
| 反汇编 | `Disassembler/MiniRVDisassembler.cpp` | `getInstruction`、`DecodeGPRRegisterClass` | `DecoderTable32` 的消费者 |
| 解析器 | `AsmParser/MiniRVAsmParser.cpp` | `MatchInstructionImpl`、`isSImm12` | matcher 如何被调用 |
| RISC-V 参照 | `RISCV/MCTargetDesc/RISCVMCTargetDesc.cpp` | 上面的长注册块 | 多出来的 streamer/analysis |

## 实测：三条命令看 MC 层

本节输入：`/tmp/mc06.s` 是 `add/mul/ld/beq/jal/jalr` 六条指令；`/tmp/add3.ll` 是 `(a+b)*c`；`/tmp/cfi.o` 由 `llc -mtriple=minirv64 -O2 -filetype=obj $TREE/llvm/test/CodeGen/MiniRV/cfi.ll` 生成。

### 1. `llvm-mc -show-encoding`：MCInst + fixup

```console
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/mc06.s
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
	mul	x10, x10, x13                   # encoding: [0x33,0x05,0xd5,0x02]
	ld	x5, 8(x6)                       # encoding: [0x83,0x32,0x83,0x00]
	beq	x1, x2, target                  # encoding: [0x63'A',0x80'A',0x20'A',A]
                                        #   fixup A - offset: 0, value: target, kind: fixup_minirv_branch
	jal	x0, target                      # encoding: [0x6f'A',A,A,A]
                                        #   fixup A - offset: 0, value: target, kind: fixup_minirv_jal
target:
	jalr	x0, 0(x1)                       # encoding: [0x67,0x80,0x00,0x00]
```

`0x33,0x85,0xc5,0x00` 是 `add x10,x11,x12` 的小端字节。`beq`/`jal` 的 `'A'` 是 assembler fixup：目标地址在流式输出时还不知道，`MCCodeEmitter` 只写 0，`MCAsmBackend` 在布局后回填。`fixup_minirv_branch` 和 `fixup_minirv_jal` 就是 `MiniRVMCTargetDesc.h` 里的 target fixup 枚举。

### 2. `llvm-mc -show-inst`：MCInst printer 的输入

```console
$ $B/llvm-mc -triple=minirv64 -show-inst /tmp/mc06.s | sed -n '1,8p'
	add	x10, x11, x12                   # <MCInst #342 ADD
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Reg:X11>
                                        #  <MCOperand Reg:X12>>
	mul	x10, x10, x13                   # <MCInst #371 MUL
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Reg:X13>>
```

这才是 `MCInstPrinter` 真正吃的东西：opcode 编号 + 操作数（寄存器/立即数/表达式）。`#342` 是 `MiniRV::ADD` 的枚举值，不是机器码里的 opcode；它会随 target 的指令数量变化，别在脚本里硬编码。

### 3. `llvm-mc -filetype=obj` + `llvm-objdump -d`：编码与解码闭环

```console
$ $B/llvm-mc -triple=minirv64 -filetype=obj /tmp/mc06.s -o /tmp/mc06.o
$ $B/llvm-objdump -d --triple=minirv64 /tmp/mc06.o | sed -n '1,16p'

mc06.o:	file format elf64-littleriscv

Disassembly of section .text:

0000000000000000 <.text>:
       0: 33 85 c5 00  	add	x10, x11, x12
       4: 33 05 d5 02  	mul	x10, x10, x13
       8: 83 32 83 00  	ld	x5, 8(x6)
       c: 63 84 20 00  	beq	x1, x2, 8
      10: 6f 00 40 00  	jal	x0, 4

0000000000000014 <target>:
      14: 67 80 00 00  	jalr	x0, 0(x1)
```

`llvm-objdump` 走的是 `MCDisassembler::getInstruction()` → `decodeInstruction(DecoderTable32, ...)` → `MCInst` → `MCInstPrinter::printInst()`。它和编码器共享同一批指令记录，但不是同一个 `.inc`：编码用 `MiniRVGenMCCodeEmitter.inc`，解码用 `MiniRVGenDisassemblerTables.inc`。两个方向的表由同一个 `.td` 生成，所以不会出现“编码器认识、反汇编器不认识”的分叉。

### 4. `llc`：从 IR 到 MCInst 再到汇编

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/add3.ll -o - | sed -n '1,12p'
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
```

`llc` 的汇编输出路径是 `AsmPrinter` → `MiniRVMCInstLower` → `MCInst` → `MiniRVInstPrinter`。`jalr x0, 0(x1)` 是返回：`PseudoRET` 被 `MiniRVGenMCPseudoLowering.inc` 展开成 `JALR X0, X1, 0`，再由打印器输出。注意这里没有 `call`/`ret` 助记符，因为 MiniRV 的汇编语法就是真实 RISC-V 形式。

### 5. `llvm-readobj`：对象格式与 CIE

```console
$ $B/llvm-readobj -h /tmp/mc06.o | grep -E 'Format:|Arch:|Machine:|Class:'
Format: elf64-littleriscv
Arch: riscv64
    Class: 64-bit (0x2)
  Machine: EM_RISCV (0xF3)
```

MiniRV 复用了 `EM_RISCV`，所以 `llvm-readobj` 把它显示成 `riscv64`。这也是为什么 `llvm-objdump` 要显式带 `--triple=minirv64`：不带时它按 ELF header 选 `riscv64` 后端，会把 `x10` 打成 `a0`，`mul` 可能显示 `<unknown>`（generic RISC-V 默认不开 M），`jalr x0, 0(x1)` 显示成 `ret`。指令字节相同，但打印语义不同。

CFI 的证据在 `--unwind`（见上一节的 CIE 输出）：`return_address_register: 1` 对应 `InitMCRegisterInfo(X, MiniRV::X1)`，`DW_CFA_def_cfa: reg2 +0` 对应 `addInitialFrameState(cfiDefCfa(nullptr, 2, 0))`。这两个数字不是巧合，是两处代码的直接输出。

## 反直觉与踩雷

1. **错误直觉**：“MC 层五件套”就是 MC 层的全部。→ **正确认识**：五件套描述指令和寄存器，但要把 `MCInst` 变成文件，还需要 `MCCodeEmitter`（字节 + fixup）和 `MCAsmBackend`（fixup 应用 + relocation + object writer）。MiniRV 的注册表有八项，不是五项。

2. **错误直觉**：`InitMCRegisterInfo(X, MiniRV::X1)` 的第二个参数是栈指针或 PC。→ **正确认识**：它是返回地址寄存器（RA）。`MCRegisterInfo::RAReg` 会被 `MCDwarf` 用来填 CIE 的 `return_address_register`。传错会让 unwinder 读错列，backtrace/异常展开断裂。PC 是第五个参数（`PC = 0`），MiniRV 没有独立的 PC 寄存器。

3. **错误直觉**：`addInitialFrameState` 会在每个函数的汇编里打印一条 `.cfi_def_cfa`。→ **正确认识**：它进入 `MCAsmInfo::InitialFrameState`，只有在 non-simple frame 生成 CIE 时才被写进 `.eh_frame`。`-O2` 的 leaf 函数没有栈帧，汇编里看不到这条指令，但对象 CIE 里仍然有 `DW_CFA_def_cfa: reg2 +0`（只要函数需要 unwind 信息）。

4. **错误直觉**：`llvm-objdump` 用编码器的逆运算，所以复用 `MiniRVGenMCCodeEmitter.inc`。→ **正确认识**：反汇编器用 `MiniRVGenDisassemblerTables.inc` 的 `DecoderTable32`，编码器用 `MiniRVGenMCCodeEmitter.inc` 的 `InstBits[]`。两者由同一批指令记录生成，但生成器和运行时消费者都不同。

5. **错误直觉**：MiniRV 是一个全新的 ELF machine。→ **正确认识**：它写的是 `EM_RISCV`，relocation 也是 `R_RISCV_*`。这换来的是 `ld.lld`/`qemu-riscv64-static` 可直接用；代价是 `llvm-readobj`/`file` 会把它识别成 RISC-V，且不能和真正的 RISC-V 目标文件区分。

6. **错误直觉**：`MCInst` 的 opcode 编号是稳定的 ABI。→ **正确认识**：`#342` 是 TableGen 生成的枚举，增删指令就会漂移。测试里应该匹配助记符/编码，不要匹配 `MCInst #N`。

## 进阶：再往深一层

- **看 `MCStreamer` 的分层**。`MCAsmStreamer` 负责文本，`MCObjectStreamer` 负责对象；两者都通过 `MCCodeEmitter`/`MCAsmBackend` 落地。MiniRV 注册的 `NullTargetStreamer` 让通用 streamer 不处理任何 target 指令。
- **看 fixup 的生命周期**。`MCCodeEmitter::encodeInstruction()` 在流式输出时不知道分支目标，只写 0 并压入 `MCFixup`；`MCAssembler` 布局完 section 后调用 `MCAsmBackend::applyFixup()` 回填。`evaluateFixup()` 是“能否在汇编期解析”的钩子，MiniRV 用它处理 `pcrel_lo12_i` 对 `pcrel_hi20` 的依赖。
- **看 `.eh_frame` 的两级结构**。CIE 存“所有函数共享的初始规则”（RA、data alignment、初始 CFA），FDE 存“这个函数从入口到结束的增量规则”。MiniRV 的 `addInitialFrameState` 只影响 CIE，`MiniRVFrameLowering::emitPrologue` 里的 `.cfi_def_cfa_offset` 影响 FDE。

## 本章重点整理

- 五件套：`MCAsmInfo`（语法契约）、`MCInstrInfo`（opcode 描述）、`MCRegisterInfo`（寄存器/RA/DWARF）、`MCSubtargetInfo`（CPU/feature/调度）、`MCInstPrinter`（文本）。
- 另外两件：`MCCodeEmitter`（MCInst→字节+fixup）、`MCAsmBackend`（fixup/relocation/object writer）。
- `MiniRVMCTargetDesc.cpp` 的 `GET_*_MC_DESC` 只是选择生成物的段；真正创建对象的是 `createMiniRVMC*` 工厂，注册的是 `LLVMInitializeMiniRVTargetMC()`。
- `InitMCRegisterInfo(X, MiniRV::X1)` 的 `X1` 是返回地址寄存器，决定 CIE 的 `return_address_register`。
- `addInitialFrameState(cfiDefCfa(nullptr, 2, 0))` 给 CFA 一个入口基准；non-simple frame 的 CIE 会写 `DW_CFA_def_cfa: reg2 +0`。

## 自我检核

- [ ] 不看源码，说出五个 MC 对象各自的数据来源和至少两个调用者。
- [ ] 解释 `GET_INSTRINFO_MC_DESC`、`GET_SUBTARGETINFO_MC_DESC`、`GET_REGINFO_MC_DESC` 分别把哪些符号拉进 `MiniRVMCTargetDesc.cpp`。
- [ ] 解释 `InitMCRegisterInfo(X, MiniRV::X1)` 的 `X1` 如何一路变成 CIE 的 `return_address_register: 1`。
- [ ] 解释 `addInitialFrameState` 为什么在 leaf 函数的汇编里看不到，但对象里可能有。

## 延伸阅读

- **[The MC Layer](https://llvm.org/docs/CodeGenerator.html#the-mc-layer)** — LLVM 官方 CodeGenerator 文档
  - **读哪里**：`MCStreamer`、`MCInst`、`MCCodeEmitter`、`MCAsmBackend` 的概述。
  - **能学到什么**：五件套在整体架构中的位置，以及为什么 MC 层要独立于 CodeGen。
  - **前提**：本章的 `MiniRVMCTargetDesc.cpp` 已经能读懂。

- **[llvm-mc Command Guide](https://llvm.org/docs/CommandGuide/llvm-mc.html)** — LLVM 官方
  - **读哪里**：`-show-encoding`、`-show-inst`、`-filetype=obj`、`-disassemble` 的语义。
  - **能学到什么**：如何用 `llvm-mc` 单独验证编码器/反汇编器，而不经过 `llc`。
  - **前提**：知道 `.s` 输入和 `MCInst` 的关系。

- **[Exception Handling in LLVM](https://llvm.org/docs/ExceptionHandling.html)** — LLVM 官方
  - **读哪里**：`.eh_frame`、CIE/FDE、CFA、`DW_CFA_*` 指令、return address register。
  - **能学到什么**：为什么 `addInitialFrameState` 和 `MiniRV::X1` 会影响 unwinding，以及 `.cfi_*` 指令如何编码。
  - **前提**：读过本章的 `llvm-readobj --unwind` 输出。

- **[`elf_linking` 课程](../elf_linking/README.md)** — 本仓库
  - **读哪里**：ELF relocation、`R_RISCV_*`、`EM_RISCV`、链接器如何应用 fixup。
  - **能学到什么**：MiniRV 复用 RISC-V ELF 的代价与收益；`MiniRVELFObjectWriter` 的 relocation 映射为什么成立。
  - **前提**：本章的 `MiniRVAsmBackend::applyFixup` 和 `MiniRVELFObjectWriter::getRelocType` 已经读过。

→ [Ch 08 汇编器、打印器与编码器](./08-assembler-printer-encoder.md)
