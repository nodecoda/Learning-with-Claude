# Ch 10 MC 层与目标文件：一条指令怎样变成 ELF

> 目标：把“机器指令”到“目标文件”的过程拆成 MCInst 构造、编码、fixup、streamer、ELF 写出五段。读完后你能解释 `.text` 的 flags 从哪来、`.riscv.attributes` 为什么有时出现有时消失、`EntrySize: 24` 指的是什么记录、`.option` 为什么能改变后续指令的编码。

> 环境：Windows；LLVM 23.1.4；PowerShell。工具目录 `.scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin`；源码目录 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。实验统一通过 `code/tools/run.ps1` 调用。

> 约定：标“实测”的片段来自本机重跑；标“源码依据”的结论来自 23.1.4 源码；需要修改源码并重新编译 llc 才能观察的结论标 `guided`。

## 为什么

第 08、09 章把文本与字节互转，但两者都停在 `MCInst` 附近。真正落盘的是 ELF 目标文件，而 ELF 里的每个字节都由 MC 层决定：`.text` 的 `SHF_EXECINSTR` 不是链接器顺手加的，`SHT_RELA` 的 24 字节记录也不是编译器随手写的，`.riscv.attributes` 里那串 `rv64i2p1_m2p0...` 更是后端按特性位重新生成的。

本机有一次很能说明问题的对比。用 `llc -filetype=obj` 生成的对象有 11 个 section header（含 index 0 的 null 段），包括 `.riscv.attributes`（85 字节）与 `.note.GNU-stack`（0 字节）；用 `llvm-mc -filetype=obj` 汇编一个只有 `.text` 的小文件，对象里只剩 `.strtab / .text / .symtab` 三个段。同一个 MC 层，同一个编码器，产物却不同。差别不在编码，而在“谁负责声明元数据”：`llc` 走 `RISCVAsmPrinter`，会调用 target streamer 写属性；`llvm-mc` 只在源文件出现 `.attribute` 时才写。

理解了这条链路，很多现象就有了统一解释：

- 反汇编看到的字节和 `-show-encoding` 看到的字节一致，因为两者都经过同一个 `MCCodeEmitter`。
- `.option arch, +m` 能影响后面指令能否汇编，因为它改的是汇编解析器持有的 `MCSubtargetInfo` 特性位，而不是文本。
- 链接器能对 `lp64` 与 `lp64d` 报 “different floating-point ABI”，依据是 ELF header 的 `e_flags`，不是 `.riscv.attributes` 的字符串。

## 主线

### 10.1 MC 层的对象模型：MCInst、MCInstrInfo、MCSubtargetInfo

`MCInst` 是机器指令的目标无关表示：一个 opcode，加一串 `MCOperand`。操作数可以是寄存器、立即数、表达式、浮点立即数，也可以是“没有操作数”。它不携带类型信息，也不携带虚拟寄存器概念；从 `MachineInstr` 降到 `MCInst` 之后，寄存器都已经是物理寄存器。

`MCInstrInfo` 由 `llvm-tblgen -gen-instr-info` 生成。第 05 章验证过生成物里的真实行：

```cpp
ADD                                        = 13725, // RISCVInstrInfo.td:818
```

也就是说，`MCInst` 里的 opcode 只是一个整数，含义由同一份生成表解释。编码器、打印器、反汇编器都必须用同一份表，否则 opcode 会对不上。`-show-inst` 显示的 `ADDI #13727` 就是这张表的编号。

`MCSubtargetInfo` 持有 CPU、特性位、调度模型。`createRISCVMCSubtargetInfo` 在 CPU 为空或为 `generic` 时选 `generic-rv64` / `generic-rv32`，随后调用 `RISCV::updateCZceFeatureImplications`，按规则补上隐含特性，例如 `C + D` 推出 `Zcd`、`Zca` 在满足条件时推出 `C`。这解释了第 09 章的现象：`+c` 不只打开压缩编码，还会连带改变可用指令集合。

`MCAsmInfo` 描述汇编语法与数据布局。`RISCVMCAsmInfo.cpp` 的构造函数里可以直接读到：

```cpp
CodePointerSize = CalleeSaveStackSlotSize = TT.isArch64Bit() ? 8 : 4;
CommentString = "#";
AlignmentIsInBytes = false;
SupportsDebugInformation = true;
ExceptionsType = ExceptionHandling::DwarfCFI;
Data16bitsDirective = "\t.half\t";
Data32bitsDirective = "\t.word\t";
```

`AlignmentIsInBytes = false` 是后面 10.7 节对齐实验的根源：`.align n` 表示 2 的 n 次方字节，不是 n 字节。`createRISCVMCAsmInfo` 还追加了一条初始 CFI 状态：`MCCFIInstruction::cfiDefCfa(nullptr, SP, 0)`，SP 的 DWARF 编号来自 `MRI.getDwarfRegNum(RISCV::X2, true)`。这解释了为什么 `.s` 文件里即使没有手写 `.cfi_def_cfa`，`.cfi_def_cfa_offset` 也能工作。

### 10.2 一次 emitInstruction 的调用顺序

源码依据：`MCStreamer` 定义 `emitInstruction(const MCInst &, const MCSubtargetInfo &)`；`MCObjectStreamer` 重写它，并负责把结果变成 fragment；`MCCodeEmitter::encodeInstruction` 的签名是：

```cpp
virtual void encodeInstruction(const MCInst &MI, SmallVectorImpl<char> &CB,
                               SmallVectorImpl<MCFixup> &Fixups,
                               const MCSubtargetInfo &STI) const = 0;
```

把它按时间顺序展开：

1. 汇编器或 AsmPrinter 调 `Streamer.emitInstruction(MI, STI)`。
2. `MCObjectStreamer::emitInstruction` 调 `MCCodeEmitter::encodeInstruction`，拿到指令字节 `CB` 和待处理 `Fixups`。
3. 如果当前在“紧凑 fragment”里，`emitInstToFragment` 把字节和 fixup 追加到 fragment；否则新建 fragment。
4. 汇编结束时，`MCAssembler` 遍历 fragment，调 `MCAsmBackend::applyFixup` 写入能静态解析的立即数，调 `MCELFObjectTargetWriter::getRelocType` 决定哪些 fixup 升级成重定位。
5. `ELFObjectWriter` 把 section 内容、符号表、重定位表写进文件。

`MCAsmBackend` 的接口决定了“汇编期能解析多少”：

```cpp
virtual void applyFixup(const MCFragment &, const MCFixup &, const MCValue &,
                        uint8_t *Data, uint64_t Value, bool IsResolved) = 0;
virtual bool mayNeedRelaxation(unsigned Opcode, ArrayRef<MCOperand> Operands,
                               const MCSubtargetInfo &STI) const;
virtual bool fixupNeedsRelaxationAdvanced(const MCFragment &, ...) const;
virtual void relaxInstruction(MCInst &Inst, const MCSubtargetInfo &) const;
```

RISC-V 的 `RISCVAsmBackend.cpp` 里能查到这些符号的真实定义：`getFixupKind`、`getFixupKindInfo`、`fixupNeedsRelaxationAdvanced`（第 118 行附近）、`relaxInstruction`（第 248 行附近）、`relaxAlign`、`relaxDwarfLineAddr`、`relaxDwarfCFA`、`relaxLEB128`、`mayNeedRelaxation`、`writeNopData`、`evaluateFixup`、`applyFixup`（第 948 行附近）。这组函数是 MC 后端的 fixup/relax 接口：`call foo` 先编码成 `auipc + jalr`，fixup 带 `LinkerRelaxable` 标记；本机 `llvm-mc -filetype=obj` 生成的对象仍保留这两条指令，真正收缩成 `jal` 发生在链接期。

一个容易混淆的点：fixup 是汇编期的“待填坑”，relocation 是对象文件里的“交给链接器的坑”。`applyFixup` 能在汇编期算出值的 fixup 不会变成 relocation；算不出的才由 `RISCVELFObjectWriter::getRelocType` 映射成 `R_RISCV_*`。`evaluateFixup` 的返回值就是在回答“这个 fixup 现在能不能定值”。

### 10.3 MCStreamer 家族与 MCTargetDesc 的逐文件职责

`MCStreamer` 是抽象基类，常见实现有三类：

- `MCAsmStreamer`：输出文本汇编，服务于 `llc -filetype=asm` 与 `llvm-mc` 的默认输出。
- `MCObjectStreamer`：输出目标文件，`RISCVELFStreamer` 派生自它，负责 `.riscv.attributes` 与映射符号。
- `MCNullStreamer`：只统计、不输出，服务于仅关心副作用或错误诊断的路径。

在此之上还有 `MCTargetStreamer`，它是“目标相关指令/伪指令”的挂钩点。RISC-V 有三层实现：`RISCVTargetStreamer`（空实现基类）、`RISCVTargetAsmStreamer`（把 `.option` 原样打印）、`RISCVTargetELFStreamer`（把 `.option` 反映到 `ArchString` 与映射符号）。

`LLVMInitializeRISCVTargetMC` 是注册总表。源码依据（`RISCVMCTargetDesc.cpp` 第 473 行起）按顺序注册：

```text
MCAsmInfo / MCObjectFileInfo / MCInstrInfo / MCRegInfo /
MCAsmBackend / MCCodeEmitter / MCInstPrinter / MCSubtargetInfo /
ELFStreamer / MachOStreamer / ObjectTargetStreamer /
MCInstrAnalysis / AsmTargetStreamer / NullTargetStreamer
```

这解释了“为什么换个文件格式只改一个注册项”：编码器、打印器、寄存器信息与对象格式解耦，Mach-O 仅换 streamer 与 writer。

`MCTargetDesc` 目录本机共 24 个文件（含 `CMakeLists.txt`）。按职责分组：

| 文件 | 职责 |
|---|---|
| `RISCVMCTargetDesc.cpp/.h` | 目标注册、工厂函数、`LLVMInitializeRISCVTargetMC` |
| `RISCVMCAsmInfo.cpp/.h` | 汇编语法、指针大小、注释符、初始 CFI |
| `RISCVMCCodeEmitter.cpp` | `encodeInstruction`，把 `MCInst` 编成字节并产生 fixup |
| `RISCVAsmBackend.cpp/.h` | fixup 求值、relax、NOP 填充、对齐 |
| `RISCVELFObjectWriter.cpp` | `getRelocType`、`needsRelocateWithSymbol` |
| `RISCVELFStreamer.cpp/.h` | 属性段、映射符号、`e_flags` |
| `RISCVTargetStreamer.cpp/.h` | `.option`、`emitTargetAttributes` |
| `RISCVInstPrinter.cpp/.h` | `MCInst` 到文本，别名与压缩反打印 |
| `RISCVMCExpr.cpp` | `%hi/%lo/%pcrel_hi` 等修饰符表达式 |
| `RISCVFixupKinds.h` | fixup 枚举 |
| `RISCVBaseInfo.cpp/.h` | ABI 枚举、修饰符名称、`RISCVRVC::uncompress` |
| `RISCVMatInt.cpp/.h` | 大立即数的 `lui/addi/slli` 物化序列 |
| `RISCVMCObjectFileInfo.cpp/.h` | 目标相关 section 选择 |
| `RISCVMachObjectWriter.cpp` | Mach-O 写出 |
| `RISCVMCInstrAnalysis`（在 `RISCVMCTargetDesc.cpp` 内） | 分支目标、返回指令识别 |

这里最值得先读的是 `RISCVMCCodeEmitter.cpp` 与 `RISCVAsmBackend.cpp`：前者决定字节，后者决定这些字节在什么时候还不能定稿。
### 10.4 ELF 输出：真实段表与符号表

准备一个同时含字符串、全局变量、外部调用的 IR：

```llvm
@.str = private unnamed_addr constant [6 x i8] c"hello\00", align 1
@g = dso_local global i32 42, align 4
declare i32 @ext(i32)
declare i32 @puts(ptr)

define dso_local i32 @f(i32 %x) {
entry:
  %y = call i32 @ext(i32 %x)
  ret i32 %y
}

define dso_local i32 @h() {
entry:
  %p = call i32 @puts(ptr @.str)
  %v = load i32, ptr @g
  ret i32 %v
}
```

命令：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -O2 -filetype=obj ch10.ll -o ch10.o
llvm-readobj --sections ch10.o
```

实测段表的关键字段（省略 Flags 的多行展开）：

| Index | Name | Type | Size | Align | EntrySize | 关键 Flags |
|---|---|---|---|---|---|---|
| 1 | `.strtab` | `SHT_STRTAB` | 200 | 1 | 0 | — |
| 2 | `.text` | `SHT_PROGBITS` | 72 | 4 | 0 | `ALLOC | EXECINSTR` |
| 3 | `.rela.text` | `SHT_RELA` | 144 | 8 | 24 | `INFO_LINK` |
| 4 | `.rodata.str1.1` | `SHT_PROGBITS` | 6 | 1 | 1 | `ALLOC | MERGE | STRINGS` |
| 5 | `.data` | `SHT_PROGBITS` | 4 | 4 | 0 | `ALLOC | WRITE` |
| 6 | `.note.GNU-stack` | `SHT_PROGBITS` | 0 | 1 | 0 | — |
| 7 | `.riscv.attributes` | `SHT_RISCV_ATTRIBUTES (0x70000003)` | 85 | 1 | 0 | — |
| 8 | `.eh_frame` | `SHT_PROGBITS` | 80 | 8 | 0 | `ALLOC` |
| 9 | `.rela.eh_frame` | `SHT_RELA` | 48 | 8 | 24 | `INFO_LINK` |
| 10 | `.symtab` | `SHT_SYMTAB` | 360 | 8 | 24 | — |

这张表里有四处值得逐个解释。

第一，`.text` 的 `AddressAlignment` 是 4，不是 8。函数对齐由 `.p2align 2` 控制，默认 4 字节。`Size: 72` 是两条函数的机器码总长：`f` 28 字节，`h` 44 字节。

第二，`.rela.text` 的 `Info: 2` 指回 `.text` 的 section index，`Link: 10` 指回 `.symtab`。`EntrySize: 24` 是 `Elf64_Rela` 的大小：`r_offset` 8 字节 + `r_info` 8 字节 + `r_addend` 8 字节。看到 144 字节就知道有 6 条重定位；这与 `f` 调用 `ext`、`h` 取 `@g` 与 `@.str` 的地址相吻合。

第三，`.rodata.str1.1` 带 `SHF_MERGE | SHF_STRINGS`，`EntrySize: 1` 表示按 1 字节粒度合并相同字符串。名字里的 `.str1.1` 就是“1 字节对齐、1 字节实体”的缩写。这是常量池与字符串去重的交界点，第 22 章会继续展开。

第四，`.note.GNU-stack` 大小为 0。它不装数据，只作为标记段存在；按 GNU 工具链的约定，它用于声明对象的栈权限。

符号表同样能读出大量信息：

```text
Name: f        Value: 0x0   Size: 28  Binding: Global  Type: Function  Section: .text
Name: ext      Value: 0x0   Size: 0   Binding: Global  Type: None      Section: Undefined
Name: h        Value: 0x1C  Size: 44  Binding: Global  Type: Function  Section: .text
Name: puts     Value: 0x0   Size: 0   Binding: Global  Type: None      Section: Undefined
Name: g        Value: 0x0   Size: 4   Binding: Global  Type: Object    Section: .data
Name: .L.str   Value: 0x0   Size: 6   Binding: Local   Type: Object    Section: .rodata.str1.1
Name: $xrv64i2p1_m2p0_a2p1_f2p2_d2p2_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0  Section: .text
Name: $d       Value: 0x0   Size: 0   Binding: Local   Type: None      Section: .data
```

`$x...` 是映射符号（mapping symbol），告诉反汇编器“从这里开始是某套 ISA 的指令”。`$d` 表示“从这里开始是数据”。第 09 章讲反汇编按宽度分派，但真实 objdump 还要先靠这些符号切分代码与数据，否则会把常量池当指令解。

### 10.5 `.riscv.attributes`：谁写、写什么、为什么有时没有

`llc` 生成的 `.riscv.attributes` 用 `llvm-readelf -A` 读出来是：

```text
BuildAttributes {
  FormatVersion: 0x41
  Section 1 {
    SectionLength: 84
    Vendor: riscv
    Tag: Tag_File (0x1)
    Size: 74
    FileAttributes {
      Attribute {
        Tag: 4
        Value: 16
        TagName: stack_align
        Description: Stack alignment is 16-bytes
      }
      Attribute {
        Tag: 5
        TagName: arch
        Value: rv64i2p1_m2p0_a2p1_f2p2_d2p2_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0
      }
    }
  }
}
```

两个 tag：`stack_align = 16` 与 `arch = ...`。`arch` 字符串不是把命令行 `-mattr` 原样抄下来，而是由 `RISCVFeatures::parseFeatureBits` 解析后 `toString()` 规范化得到。所以输入 `+m` 会展开成 `m2p0`，还会补上 `zmmul`；输入 `+a` 会补上 `zaamo` 与 `zalrsc`。这是“属性字符串不能拿来做字符串相等比较”的直接原因。

`emitTargetAttributes` 的源码依据（`RISCVTargetStreamer.cpp`）：`EmitStackAlign` 为真时按 ABI 选 16/8/4（`ILP32E` 为 4，`LP64E` 为 8，其余 16）；随后写 `RISCVAttrs::ARCH`；`RiscvAbiAttr` 开启且支持 A 时再写 `ATOMIC_ABI`（A6S/A6C/A7）。

`llvm-mc` 的行为不同。实测最小输入：

```asm
	.text
	.globl f
f:
	addi a0, a1, 1
	ret
```

`llvm-mc -triple=riscv64 -filetype=obj` 产出的段只有 `.strtab`（36 字节）、`.text`（8 字节，Align 4）、`.symtab`（72 字节，EntrySize 24）。没有 `.riscv.attributes`，也没有 `.note.GNU-stack`。原因很直接：没有 `.attribute` 指令，`RISCVTargetELFStreamer` 的属性表是空的，`finishAttributeSection` 不会创建 section；`.note.GNU-stack` 通常由编译器驱动或 AsmPrinter 发出，`llvm-mc` 不负责。

手动写 `.attribute 5, "rv64i2p1_m2p0"` 后，`.riscv.attributes` 出现，大小 40 字节，`llvm-readelf -A` 输出：

```text
BuildAttributes {
  FormatVersion: 0x41
  Section 1 {
    SectionLength: 39
    Vendor: riscv
    Tag: Tag_File (0x1)
    Size: 29
    FileAttributes {
      Attribute {
        Tag: 5
        TagName: arch
        Value: rv64i2p1_m2p0_zmmul1p0
      }
    }
  }
}
```

注意两点。第一，没有 `stack_align`：那是 `emitTargetAttributes` 的 `EmitStackAlign` 分支写的，`llvm-mc` 的 `.attribute 5` 只写 arch。第二，`zmmul` 仍然被补上了，因为 M 蕴含 Zmmul 的规则在解析时生效。

`llvm-readobj` 没有 `--attributes`。实测：

```text
llvm-readobj: error: unknown argument '--attributes'
```

等价入口是 `llvm-readobj --arch-specific` 或 `llvm-readelf -A`，两者输出同一份 BuildAttributes。

### 10.6 `.option` 与 target streamer：汇编期改变特性位

`.option` 不是注释，也不是给链接器的提示。它在汇编期修改 `MCSubtargetInfo` 的特性位，并（对 ELF 输出）更新 `ArchString` 与映射符号。实测输入：

```asm
	.text
opt1:
	.option arch, +m
	mul a0, a0, a1
	.option push
	.option arch, +c
	lw a0, 8(sp)
	.option pop
	lw a0, 8(sp)
	.option norvc
	lw a0, 8(sp)
	.option rvc
	lw a0, 8(sp)
	ret
```

不加任何 `-mattr` 时，`llvm-mc -triple=riscv64 -show-encoding` 的真实输出：

```text
	.option	arch, +m
	mul	a0, a0, a1                      # encoding: [0x33,0x05,0xb5,0x02]
	.option	push
	.option	arch, +c
	lw	a0, 8(sp)                       # encoding: [0x22,0x45]
	.option	pop
	lw	a0, 8(sp)                       # encoding: [0x03,0x25,0x81,0x00]
	.option	norvc
	lw	a0, 8(sp)                       # encoding: [0x03,0x25,0x81,0x00]
	.option	rvc
	lw	a0, 8(sp)                       # encoding: [0x22,0x45]
	ret                                     # encoding: [0x82,0x80]
```

三个观察：

- `.option arch, +m` 让 `mul` 在完全没有 `-mattr=+m` 的情况下可汇编。这说明 `.option` 改的是解析器使用的 `STI`，不是文本替换。
- `.option push` / `pop` 保存与恢复了“进入 push 前的特性集”。这里 pop 之后 `lw` 回到 4 字节，因为进入 push 前没有 C。
- `.option norvc` 与 `.option rvc` 是更窄的开关，只控制压缩，不改变其他扩展。

同一文件加上 `-mattr=+c` 后，`pop` 之后的 `lw` 仍然是 `[0x22,0x45]`：因为基线的特性集已经含 C，pop 恢复的正是这个基线。这说明 `push/pop` 不是“清空”，而是“回到栈顶保存的状态”。

源码依据：`RISCVTargetAsmStreamer` 的各个 `emitDirectiveOption*` 只是把指令原样打印；`RISCVTargetELFStreamer` 则维护 `ArchString` 与 `ArchStringStack`，`emitDirectiveOptionPush` 入栈、`pop` 出栈并调用 `setArchString`。`RISCVELFStreamer::emitInstructionsMappingSymbol` 会在 ISA 变化时发出新的 `$x<ISA>` 映射符号，并用 `LastEmittedArch` 去重。这解释了为什么 `.option arch` 之后符号表里可能出现多个 `$x...`。

`norelax` 与 `relax` 的编码相同，差别在汇编器是否为该调用点标记 linker-relaxable。实测 `call foo` 在两种模式下都是：

```text
	call	foo                             # encoding: [0x97'A',A,A,A,0xe7'A',0x80'A',A,A]
                                        #   fixup A - offset: 0, value: foo, kind: fixup_riscv_call_plt
```

`'A'` 是 `llvm-mc` 对“待重定位字节”的占位写法，不是具体的 0。两种模式生成的对象里都有 `R_RISCV_CALL_PLT foo`；只有 `.option relax` 的那条额外带一条 `R_RISCV_RELAX`。实测对象的重定位表：

```text
0x0 R_RISCV_CALL_PLT foo 0x0
0x8 R_RISCV_CALL_PLT foo 0x0
0x8 R_RISCV_RELAX    -   0x0
```

真正的收缩发生在链接器：带 `R_RISCV_RELAX` 的调用点才能把 8 字节 `auipc + jalr` 压成 4 字节的 `jal`。不要把 `norelax` 理解成“生成不同的指令”，它改变的是重定位的可放松性。

### 10.7 record layout 与对齐：`.align` 是 2 的幂

RISC-V 的 `MCAsmInfo` 设了 `AlignmentIsInBytes = false`，所以 `.align n` 是“对齐到 2^n 字节”。实测：

```asm
	.data
	.byte 1
	.align 3
	.byte 2
	.globl g1
g1:
	.word 7
	.text
	.globl f
f:
	addi a0, a0, 1
	.size f, .-f
```

`llvm-mc -triple=riscv64 -filetype=obj` 后：

```text
.text   Offset 0x40  Size 4   Align 4
.data   Offset 0x48  Size 13  Align 8
.symtab Offset 0x58  Size 120 Align 8  EntrySize 24
```

逐个字节推一遍：`.byte 1` 在 `.data` 的偏移 0；`.align 3` 补齐到偏移 8；`.byte 2` 在偏移 8；`g1` 在偏移 9（符号表里 `Value: 0x9`）；`.word 7` 占偏移 9 到 12，所以 `Size: 13`。下一个段 `.symtab` 要求 8 字节对齐，0x48 + 13 = 0x55，向上取整到 0x58，中间 3 个字节是填充。文件偏移不是简单累加，必须按每个段的 `AddressAlignment` 取整。

`.bss` 的实测更能说明“单位不是字节”：

```asm
	.bss
	.align 4
	.globl b1
b1:
	.zero 8
```

结果 `.bss` 的 `Size: 8`、`AddressAlignment: 16`。`.align 4` 得到 16 字节对齐，因为 2^4 = 16。若按“4 字节”理解，就会错误地以为这里仅需 4 字节对齐。

`EntrySize` 与记录布局的关系也可以从真实值反推：

| 段 | EntrySize | 对应结构 | 字段 |
|---|---|---|---|
| `.symtab` | 24 | `Elf64_Sym` | `st_name` 4 + `st_info` 1 + `st_other` 1 + `st_shndx` 2 + `st_value` 8 + `st_size` 8 |
| `.rela.text` | 24 | `Elf64_Rela` | `r_offset` 8 + `r_info` 8 + `r_addend` 8 |
| `.rodata.str1.1` | 1 | 字符串实体 | 每 1 字节一个可合并实体 |

这三个 24 不是巧合，而是 64 位 ELF 的固定布局。看懂 `EntrySize` 就能从段大小算出条目数：144 / 24 = 6 条 `.rela.text`，360 / 24 = 15 个符号表项（含 null 项与 `$x/$d` 映射符号）。

## 可跑实验

实验脚本：`code/labs/10-mc-layer.ps1`。它有 A–D 四组主实验与一个 E 失败/边界用例，并把所有输出前缀成 `OUT:`，避免 PowerShell 把 stderr 当成终止错误。

### 实验 A：从 IR 到对象，读段表与符号表

```powershell
.\code\tools\run.ps1 labs\10-mc-layer.ps1 -Case A
```

内部命令：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -O2 -filetype=obj ch10.ll -o ch10.o
llvm-readobj --sections ch10.o
llvm-readobj --symbols ch10.o
llvm-readelf -A ch10.o
```

预期看到 10.4 节的 11 个段（含 index 0 的 null 段）、`SectionLength: 84`、`stack_align = 16`，以及 `f/h/ext/puts/g` 五个符号。重点核对 `.rela.text` 的 `EntrySize: 24` 与 `Info: 2`。

### 实验 B：最小 `llvm-mc` 对象与 `.attribute`

```powershell
.\code\tools\run.ps1 labs\10-mc-layer.ps1 -Case B
```

两个源文件只在有没有 `.attribute 5` 上不同。预期：第一个对象只有 `.strtab/.text/.symtab`；第二个对象多出 `.riscv.attributes`（40 字节），`Value` 为 `rv64i2p1_m2p0_zmmul1p0`。如果第二个对象没有 `stack_align`，这是正常的，不是实验失败。

### 实验 C：`.option` 开关与压缩

```powershell
.\code\tools\run.ps1 labs\10-mc-layer.ps1 -Case C
```

脚本先不加 `-mattr` 跑一遍，再加 `-mattr=+c` 跑一遍。对照两遍的 `lw` 编码：第一遍 pop 后是 `[0x03,0x25,0x81,0x00]`，第二遍 pop 后是 `[0x22,0x45]`。这说明 `pop` 恢复的是基线，不是“关闭所有扩展”。

### 实验 D：对齐、填充与 `.align` 的幂语义

```powershell
.\code\tools\run.ps1 labs\10-mc-layer.ps1 -Case D
```

脚本生成 `.data` 的 13 字节与 `.bss` 的 16 字节对齐，并用 `llvm-readobj --sections` 打出 `Offset / Size / AddressAlignment`。核对 `.symtab` 偏移 0x58 与 `.data` 结束 0x55 之间的 3 字节填充。

### 失败/边界例子：`.option arch, -m` 不会移除 Zmmul

```asm
	.text
	.option arch, +m
	.option arch, -m
	div a0, a0, a1
	mul a0, a0, a1
```

实测：

```text
error: instruction requires the following: 'M' (Integer Multiplication and Division)
	mul	a0, a0, a1                      # encoding: [0x33,0x05,0xb5,0x02]
```

`div` 失败，`mul` 仍然成功。原因是 `-m` 只移除了 M，而 M 蕴含的 Zmmul 是独立特性位，仍然打开。同理，`.option arch, rv64i` 会被规范化打印成 `rv64i2p1`，随后 `mul` 报：

```text
error: instruction requires the following: 'Zmmul' (Integer Multiplication)
```

另一个边界：`llvm-readobj --attributes` 不存在，必须改用 `--arch-specific` 或 `llvm-readelf -A`。这条不是“版本差异”，本机 23.1.4 就是如此。

## 源码落点

按阅读顺序：

1. `MCTargetDesc/RISCVMCTargetDesc.cpp`
   - `LLVMInitializeRISCVTargetMC`：注册表，确认每个组件由哪个工厂创建。
   - `createRISCVMCSubtargetInfo`：`generic-rv64` 回退与 `updateCZceFeatureImplications`。
   - `createRISCVMCAsmInfo`：初始 CFI 状态 `cfiDefCfa(nullptr, SP, 0)`。
2. `MCTargetDesc/RISCVMCAsmInfo.cpp`
   - `RISCVMCAsmInfo::RISCVMCAsmInfo`：`CodePointerSize`、`AlignmentIsInBytes`、`CommentString`。
   - `getExprForFDESymbol`：`R_RISCV_32_PCREL` 的来历。
3. `MCTargetDesc/RISCVMCCodeEmitter.cpp`
   - `encodeInstruction`：字节与 fixup 的产生点。
4. `MCTargetDesc/RISCVAsmBackend.cpp`
   - `applyFixup`、`evaluateFixup`、`mayNeedRelaxation`、`relaxInstruction`、`relaxAlign`。
5. `MCTargetDesc/RISCVELFStreamer.cpp`
   - `RISCVTargetELFStreamer::finish`：`EF_RISCV_FLOAT_ABI_*`、`EF_RISCV_RVC`、`EF_RISCV_TSO`。
   - `finishAttributeSection`：`SHT_RISCV_ATTRIBUTES` 的创建。
   - `emitInstructionsMappingSymbol`：`$x<ISA>` 的生成与去重。
6. `MCTargetDesc/RISCVTargetStreamer.cpp`
   - `emitTargetAttributes`：`stack_align`、`arch`、`atomic_abi`。
   - `RISCVTargetAsmStreamer::emitDirectiveOption*`：`.option` 的文本形式。
7. `MCTargetDesc/RISCVELFObjectWriter.cpp`
   - `getRelocType`、`needsRelocateWithSymbol`。
8. `MCTargetDesc/RISCVFixupKinds.h`
   - fixup 枚举与 `FirstTargetFixupKind`。

配合阅读的上游接口：`llvm/include/llvm/MC/MCStreamer.h`、`MCObjectStreamer.h`、`MCCodeEmitter.h`、`MCAsmBackend.h`、`MCELFObjectTargetWriter.h`。这些头文件不在 `lib/Target/RISCV` 下，但签名决定了后端必须实现什么。

## 常见坑

1. **把 `--attributes` 当成 `llvm-readobj` 的选项**。本机报 `unknown argument '--attributes'`。用 `--arch-specific` 或 `llvm-readelf -A`。
2. **把 `stack_align` 当成一定存在**。`llvm-mc` 的 `.attribute 5` 只写 `arch`，不写 `stack_align`；`llc` 才通过 `emitTargetAttributes` 两者都写。
3. **用字符串比较 ISA 属性**。`+m` 会被规范化成 `m2p0` 并补 `zmmul`，`+a` 会补 `zaamo/zalrsc`。比较前必须解析，不能直接 `-eq`。
4. **以为 `.option arch, -m` 关掉了所有乘法**。M 蕴含的 Zmmul 仍在，`mul` 可汇编，`div` 不可。要关乘法需同时处理 Zmmul 相关特性。
5. **以为 `.align n` 是 n 字节**。RISC-V 上它是 2^n；`.align 4` 得到 16 字节对齐。这个语义来自 `AlignmentIsInBytes = false`。
6. **把文件偏移当段大小累加**。段与段之间按 `AddressAlignment` 填充：`.data` 结束于 0x55，`.symtab` 从 0x58 开始，中间 3 字节不属于任何段。
7. **把 fixup 与 relocation 混为一谈**。能在汇编期定值的 fixup 不会进 `.rela.*`；只有跨段、跨符号、需要链接器决定的值才会升级成重定位。
8. **以为 `norelax` 会改变指令**。`call foo` 在 `relax` 与 `norelax` 下编码相同，差别是 fixup 是否带 `LinkerRelaxable`，收缩发生在链接期。
9. **漏掉 `$x` 映射符号**。反汇编器靠它区分代码与数据；自己写工具解析 `.text` 时，忽略映射符号会把常量池当指令。

## 自我检查

- [ ] 能说出 `MCInst` 与 `MachineInstr` 的三个区别（无虚拟寄存器、无类型、opcode 由生成表解释）。
- [ ] 能画出 `emitInstruction → encodeInstruction → addFixup → applyFixup → ELFObjectWriter` 的顺序。
- [ ] 能解释 `.rela.text` 的 `EntrySize: 24` 由哪三个 8 字节字段组成。
- [ ] 能解释为什么 `llc` 对象有 `.riscv.attributes` 而最小 `llvm-mc` 对象没有。
- [ ] 能解释 `.option push/pop` 恢复的是基线特性集，并用 `-mattr=+c` 的实测结果证明。
- [ ] 能用 `.align 3` 与 `.align 4` 分别算出 8 与 16 字节对齐。
- [ ] 能从 `.data` 的 `Offset 0x48 / Size 13` 推出 `.symtab` 的 `Offset 0x58` 与 3 字节填充。
- [ ] 能解释 `--attributes` 为什么报错，以及正确的替代命令。

## 延伸阅读

- LLVM 源码：`llvm/lib/Target/RISCV/MCTargetDesc/` 下本机 24 个文件，优先读 10.3 节的表。
- LLVM 源码：`llvm/include/llvm/MC/MCStreamer.h`、`MCObjectStreamer.h`、`MCCodeEmitter.h`、`MCAsmBackend.h`。
- RISC-V psABI：ELF 对象文件、`e_flags`、`.riscv.attributes` 的 tag 定义。
- ELF gABI：`Elf64_Sym`、`Elf64_Rela`、`SHT_*` 与 `SHF_*` 的位定义。
- 课程第 06 章：编码器如何把 `MCInst` 变成字节；第 21 章：重定位与链接；第 22 章：常量池与 GOT/TLS。
- 本机命令：`llvm-readobj --help`、`llvm-readelf --help`，确认本版本可用的选项，而不是凭记忆。

[上一章：反汇编器与 llvm-mc](./09-disassembler.md) | [下一章：寄存器与寄存器类](./11-registers-and-classes.md)