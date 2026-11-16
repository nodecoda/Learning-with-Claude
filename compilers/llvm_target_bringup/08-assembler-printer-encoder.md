# Ch 08 — 汇编器、打印器与编码器

> **目标**：能沿着 MiniRV 的 MC 流水线解释一段 `.s` 如何变成 ELF、机器码如何变回文本，并说清 AsmParser、InstPrinter、MCCodeEmitter、AsmBackend、ELFObjectWriter、Disassembler 各自负责哪一段。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。下文令 `B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`、`BUILD=/home/ubuntu/llvm/builds/dsl-tablegen`、`TREE=/home/ubuntu/llvm/trees/dsl-tablegen`。

## 为什么需要这个？

CodeGen 最后只能交给 MC 层：指令选择、寄存器分配、帧布局完成后，`MachineInstr` 被降成 `MCInst`，再由 MC 层变成汇编文本或目标文件字节。没有这一层，后端只能算“会选指令”，不能算“能产出可链接的机器码”。

MC 层最容易被低估的是**一致性**。汇编器认识的助记符、打印器输出的寄存器名、编码器写出的位、反汇编器解码的位，必须来自同一套指令记录。手写四份表迟早会漂移：编码器多写一位，反汇编器还按旧位宽解码，测试却只覆盖一个方向。

LLVM 用 TableGen 解决：同一批记录生成 `MiniRVGenAsmMatcher.inc`、`MiniRVGenAsmWriter.inc`、`MiniRVGenMCCodeEmitter.inc`、`MiniRVGenDisassemblerTables.inc`。MiniRV 只手写操作数谓词、fixup 和 ELF writer。验收方式也直接：`llvm-mc` 正向汇编，`llvm-objdump` / `llvm-mc -disassemble` 反向解码，两边对得上才算通。

先分清两个 IR：`MachineInstr` 可能含虚拟寄存器、机器基本块、调度信息；`MCInst` 只有物理寄存器或立即数/表达式组成的扁平操作数数组。Ch 08 只讨论后者。

## 先建立直觉

```
正向（文本 → 字节）                         反向（字节 → 文本）
 .s                                           .o
  │ AsmLexer                                  │ ELF reader
  ▼                                           ▼
 token 序列                                   .text bytes
  │ MiniRVAsmParser                           │ MiniRVDisassembler
  ▼                                           ▼
 MCInst { opcode, operands[] }                MCInst
  │ MiniRVMCCodeEmitter                        │ MiniRVInstPrinter
  ▼                                           ▼
 bytes + MCFixup                              .s
  │ MCAssembler / MiniRVAsmBackend::applyFixup
  ▼
 MiniRVELFObjectWriter → .o
```

`MCInst` 就是“操作码 + 操作数数组”。标签未解析时，操作数是 `Expr`；解析后才是 `Imm`：

```console
$ printf 'start:\n beq x1, x2, start\n' >/tmp/inst.s
$ $B/llvm-mc -triple=minirv64 -show-inst /tmp/inst.s
start:
	beq	x1, x2, start                   # <MCInst #351 BEQ
                                        #  <MCOperand Reg:X1>
                                        #  <MCOperand Reg:X2>
                                        #  <MCOperand Expr:start>>
```

`BEQ` 的操作数顺序是 `rs1, rs2, imm`，由 `.td` 的 `OutOperandList` / `InOperandList` 决定；编码器按同一顺序取 `MI.getOperand(n)`。`Imm` 与 `Expr` 的分岔，就是“直接编码”与“生成 fixup”的分岔。

## 主线：AsmParser 把文本变成 MCInst

`AsmParser/MiniRVAsmParser.cpp` 刻意很小：助记符表、操作数类、匹配表全来自 `MiniRVGenAsmMatcher.inc`，手写的只有“怎么切操作数”和“诊断怎么说”。

`parseInstruction` 压入助记符，解析第一个操作数，再循环吃掉逗号后的操作数。`ParseOperand` 的分支决定能接受哪些写法：`Identifier` 先按寄存器解析，不是寄存器就当表达式（通常是标签）；`LParen` 处理 `(x2)` 零偏移；`Integer`/`Plus`/`Minus` 解析表达式，后面跟 `(` 就转内存操作数。`ParseMemOperand` 把 `8(x2)` 拆成 `Imm(8)`、token `"("`、`Reg(X2)`、token `")"` 四个操作数。生成的 matcher 把括号当字面 token，所以 `MiniRVOperand::isMem()` 始终返回 `false`；内存语义不是靠 `isMem()` 表达的。

操作数谓词由 `MiniRVInstrInfo.td` 的 `AsmOperandClass` 绑定到 `MiniRVOperand`：

| 谓词 | 接受的值 | 典型操作数 |
|---|---|---|
| `isSImm12` | 绝对值，`[-2048, 2047]` | `addi`、`ld`、`sd` 的立即数 |
| `isUImm20` | 绝对值，`[0, 1048575]` | `lui`、`auipc` |
| `isUImm6` / `isUImm5` | 绝对值，`[0, 63]` / `[0, 31]` | RV64 / W 形式移位 |
| `isBranchTarget` | 绝对值要求 `isShiftedInt<12, 1>`；非绝对值一律放行 | B 型分支 |
| `isJalTarget` | 绝对值要求 `isShiftedInt<20, 1>`；非绝对值一律放行 | `jal` |

```cpp
bool isBranchTarget() const {
  if (Kind != k_Imm)
    return false;
  int64_t Val;
  if (Imm->evaluateAsAbsolute(Val))
    return isShiftedInt<12, 1>(Val);
  return true;
}
```

`isShiftedInt<12, 1>` 等价于“字节偏移必须是偶数，且落在 `[-4096, 4094]`”。所以 `beq x1, x2, 3` 被拒绝（奇数没有隐式零位），`beq x1, x2, 4096` 也被拒绝。`isSImm12`/`isUImm20` 只接受 `evaluateAsAbsolute`，普通立即数不接受标签；`isBranchTarget`/`isJalTarget` 对非绝对值表达式返回 `true`，这是标签能进入 `MCInst` 的原因。标签的真实距离要到 fixup 阶段才知道。

`matchAndEmitInstruction` 调 `MatchInstructionImpl(Operands, Inst, ErrorInfo, MatchingInlineAsm)`，成功时 `Inst.setLoc(Loc); Out.emitInstruction(Inst, *STI);`。失败时按 `MatchResult` 和 `ErrorInfo` 定位操作数。生成的 `validateOperandClass` 用 `DiagnosticPredicate` 区分完全匹配、接近匹配和不匹配，所以 `Match_InvalidSImm12`、`Match_InvalidUImm20`、`Match_InvalidBranchTarget` 能分别映射成人话。

`tryParseRegister` 只把标识符转小写再查 `MatchRegisterName`：

```cpp
Reg = MatchRegisterName(getLexer().getTok().getIdentifier().lower());
if (Reg == MiniRV::NoRegister)
  return ParseStatus::NoMatch;
```

因此 `X10` 能解析成 `x10`；但 `a0` 不在生成表里，会走到“当表达式”的分支，最后报 `invalid operand for instruction`，而不是 `invalid register name`。原因见后面的别名讨论。

## 主线：InstPrinter 把 MCInst 打印成文本

`MCTargetDesc/MiniRVInstPrinter.cpp` 的核心只有三个函数：

```cpp
void MiniRVInstPrinter::printInst(...) {
  printInstruction(MI, Address, O);
  printAnnotation(O, Annot);
}
void MiniRVInstPrinter::printOperand(const MCInst *MI, unsigned OpNo, raw_ostream &O) {
  const MCOperand &Op = MI->getOperand(OpNo);
  if (Op.isReg())      O << getRegisterName(Op.getReg());
  else if (Op.isImm()) O << Op.getImm();
  else                 MAI.printExpr(O, *Op.getExpr());
}
void MiniRVInstPrinter::printBranchOperand(...) { printOperand(MI, OpNo, O); }
```

`printInstruction` 是 TableGen 生成的，按位域分派：普通算术走 `printOperand`，`BEQ` 的第三操作数走 `printBranchOperand`，`LD`/`SD` 走 `printOperand + "(" + printOperand + ")"`。`printBranchOperand` 直接 `printOperand` 有两个后果：它不把 `Address` 加到立即数上，反汇编看到的是相对偏移；它也不告诉符号化器“这是分支目标”，所以 objdump 保留 `8`、`-36`，不替换成 `<forward>`。RISC-V 的 `RISCVInstPrinter::printBranchOperand` 会处理 `SymbolizeOperands` 和 `PrintBranchImmAsAddress`。

`printRegName` 只调用 `getRegisterName(Reg)`，也就是规范名。MiniRV 只定义 `x0` 到 `x31`，没有 ABI 别名，所以永远打印 `x10`。

## 主线：MCCodeEmitter 与生成的编码表

`MCTargetDesc/MiniRVMCCodeEmitter.cpp` 的 `encodeInstruction` 只做三件事：取 32 位编码、扩容 4 字节、小端写回：

```cpp
uint64_t Bits = getBinaryCodeForInstr(MI, Fixups, STI);
size_t N = CB.size();
CB.resize(N + 4);
support::endian::write32le(&CB[N], Bits);
```

`getBinaryCodeForInstr` 由 `MiniRVGenMCCodeEmitter.inc` 生成：先查 `InstBits[]` 拿固定位，再按操作码调用操作数 getter，把字段 OR 进去。例如 `BEQ`：

```cpp
op = getBranchTargetOpValue(MI, 2, Fixups, STI);
Value |= (op & 0x800) << 20;
Value |= (op & 0x3f0) << 21;
Value |= (op & 0xf) << 8;
Value |= (op & 0x400) >> 3;
// rs2 / rs1 同理
```

`getMachineOpValue` 分派寄存器、立即数和表达式：

```cpp
if (MO.isReg()) return Ctx.getRegisterInfo()->getEncodingValue(MO.getReg());
if (MO.isImm()) return static_cast<unsigned>(MO.getImm());
assert(MO.isExpr() && "Unexpected operand");
MCFixupKind Kind;
if (MI.getOpcode() == MiniRV::AUIPC)     Kind = MiniRV::fixup_minirv_pcrel_hi20;
else if (MI.getOpcode() == MiniRV::ADDI) Kind = MiniRV::fixup_minirv_pcrel_lo12_i;
else llvm_unreachable("MiniRV has no symbol operands for this instruction");
Fixups.push_back(MCFixup::create(0, MO.getExpr(), Kind, /*PCRel=*/true));
return 0;
```

寄存器用 `getEncodingValue` 取 5 位编号；表达式只有 `AUIPC`/`ADDI` 能接。给 `LUI` 或 `LD` 传符号表达式会直接 `llvm_unreachable`。`getBranchTargetOpValue` 对立即数返回 `(imm >> 1) & 0xfff`，对表达式压入 `fixup_minirv_branch` 并返回 0；`getJalTargetOpValue` 同理，掩码 `0xfffff`、fixup `fixup_minirv_jal`。`MCFixup::create(0, Expr, Kind, PCRel=true)` 的偏移为 0，因为每条指令固定 4 字节；`PCRel=true` 让 `MCAssembler` 减去 fixup 位置，得到相对位移。

## 主线：AsmBackend、fixup 与 applyFixup

`MCTargetDesc/MiniRVMCTargetDesc.h` 的 `enum Fixups` 从 `FirstTargetFixupKind` 开始定义四种目标 fixup：`fixup_minirv_branch`、`fixup_minirv_jal`、`fixup_minirv_pcrel_hi20`、`fixup_minirv_pcrel_lo12_i`。

| fixup | 谁生成 | 编码位置 | ELF relocation |
|---|---|---|---|
| `fixup_minirv_branch` | `getBranchTargetOpValue` 遇标签 | B 型 `imm[12\|10:5\|4:1\|11]` | `R_RISCV_BRANCH` |
| `fixup_minirv_jal` | `getJalTargetOpValue` 遇标签 | J 型 `imm[20\|10:1\|11\|19:12]` | `R_RISCV_JAL` |
| `fixup_minirv_pcrel_hi20` | `getMachineOpValue` 遇 `AUIPC` 表达式 | U 型 `imm[31:12]` | `R_RISCV_PCREL_HI20` |
| `fixup_minirv_pcrel_lo12_i` | `getMachineOpValue` 遇 `ADDI` 表达式 | I 型 `imm[31:20]` | `R_RISCV_PCREL_LO12_I` |
| `FK_Data_8` / `FK_Data_4` | 数据指令、`.quad`/`.word` | 小端整数 | `R_RISCV_64` / `R_RISCV_32` / `R_RISCV_32_PCREL` |
| 符号差 `FK_Data_*` | `.quad end - start` | 常量部分 | `R_RISCV_ADD*` + `R_RISCV_SUB*` |

`getFixupKindInfo` 对四个目标 fixup 都写 `{0, 32, 0}`。它只告诉通用汇编器“覆盖一整条 32 位指令”，真正位摆放由 `applyFixup` 做。RISC-V 会写 `{"fixup_riscv_lo12_i", 20, 12, 0}` 这种连续位段；MiniRV 不能照抄，因为 B/J 型立即数不连续。通用 `applyFixup` 只会把值左移 `TargetOffset` 后按字节 OR 进去，处理不了 `imm[12]` 在 bit31、`imm[11]` 在 bit7 的布局。

`applyFixup` 有三条路径。**符号差**：`Target.getSubSym()` 非空时，一个 `A - B` 不能由单条 relocation 表示，必须发 ADD/SUB 对：

```cpp
case FK_Data_8:
  AddType = ELF::R_RISCV_ADD64;
  SubType = ELF::R_RISCV_SUB64;
  break;
Asm->getWriter().recordRelocation(F, ..., AddTarget, AddValue);
Asm->getWriter().recordRelocation(F, ..., SubTarget, SubValue);
Value = AddValue - SubValue;
IsResolved = true;
```

`llvm/test/MC/MiniRV/symbol-difference.s` 的 `.quad end - start` 就会产生 `R_RISCV_ADD64 end` 和 `R_RISCV_SUB64 start`。**数据 fixup**：未解析就 `maybeAddReloc`，已解析就按 `TargetSize` 写 64/32/16/8 位小端整数。**指令 fixup**：未解析时 `maybeAddReloc` 后返回，指令保留 0 立即数；已解析时读回 32 位字，清位段再写回：

```cpp
uint32_t Imm = static_cast<uint32_t>(Value >> 1) & 0xfff;
Insn &= ~0xfe000f80u;              // 清 bit31-25、bit11-7
Insn |= (Imm & 0x800) << 20;  Insn |= (Imm & 0x3f0) << 21;
Insn |= (Imm & 0x00f) << 8;   Insn |= (Imm & 0x400) >> 3;
```

JAL 用 `~0xfffff000u` 清 bit31-12，再按 J 型布局写回。

`fixup_minirv_pcrel_lo12_i` 不能独立求值：`addi rd, rd, .Lpcrel_hi0` 的标签只是指向前面那条 `auipc`。`evaluateFixup` 用 `getPCRelHiFixup` 在标签所在 fragment（必要时看下一个）找偏移相同的 `fixup_minirv_pcrel_hi20`；如果高 20 位指向同一 section 的局部符号，就计算相对 `auipc` 的位移并返回 `true`，否则返回 `false` 交给 linker。找不到高 fixup 时诊断 `could not find corresponding pcrel_hi`。这条路径主要由 CodeGen 的 `PseudoLA` 展开触发。

**relax？** `MiniRVAsmBackend` 没有 `relaxInstruction`，也没有 `shouldRelax`，所以 `llvm-mc` 不会把超范围分支展开成“反向分支 + 远跳”，只会截断。`writeNopData` 只写固定的 `addi x0, x0, 0`（`13 00 00 00`）。这不是理论问题：

```console
$ cat >/tmp/far.s <<'EOF'
  beq x1, x2, far
  .space 5000
far:
  addi x1, x1, 1
EOF
$ $B/llvm-mc -triple=minirv64 -filetype=obj /tmp/far.s -o /tmp/far.o; echo exit=$?
exit=0
$ $B/llvm-objdump -d --triple=minirv64 /tmp/far.o | sed '/^$/d' | head -8
/tmp/far.o:	file format elf64-littleriscv
Disassembly of section .text:
0000000000000000 <.text>:
       0: 63 86 20 b8  	beq	x1, x2, -3188
		...
000000000000138c <far>:
    138c: 93 80 10 00  	addi	x1, x1, 1
```

`far` 在 `0x138c`，分支在 `0x0`，真实偏移 `+5004`，编码后却成了 `-3188`；`applyFixup` 只做了 `& 0xfff`，没有范围诊断。对比 RISC-V：`RISCVAsmBackend::relaxInstruction` 会把同一输入展开成 `bne ra, sp, 0x8 <.text+0x8>` + `j 0x1390 <far>`。所以 MiniRV 的 `.ll` 输入靠 CodeGen 的 `BranchRelaxation` pass（`MiniRVCodeGenPassBuilder::addPreEmitPass`），手写 `.s` 则完全没有保险；写 MiniRV 汇编必须自己保证 B 型在 ±4 KiB、J 型在 ±1 MiB 内，或者手写 `jalr` 间接跳转。e2e 链接用 `--no-relax`，也是因为没有链接器 relaxation 语义。

## 主线：ELFObjectWriter 与 EM_RISCV 复用

`MiniRVELFObjectWriter` 的关键决策只有一行：

```cpp
MCELFObjectTargetWriter(/*Is64Bit=*/true, OSABI, ELF::EM_RISCV,
                        /*HasRelocationAddend=*/true)
```

`Is64Bit=true` 只支持 64 位；`OSABI=ELFOSABI_NONE`；`EM_RISCV` 让现成的 linker、objdump、qemu 路径直接可用；`HasRelocationAddend=true` 生成 RELA。`getRelocType` 把四种目标 fixup 映射到 RISC-V 编号：

```cpp
case MiniRV::fixup_minirv_branch:       return ELF::R_RISCV_BRANCH;
case MiniRV::fixup_minirv_jal:          return ELF::R_RISCV_JAL;
case MiniRV::fixup_minirv_pcrel_hi20:   return ELF::R_RISCV_PCREL_HI20;
case MiniRV::fixup_minirv_pcrel_lo12_i: return ELF::R_RISCV_PCREL_LO12_I;
```

数据 fixup 映射到 `R_RISCV_64`、`R_RISCV_32`、`R_RISCV_32_PCREL`；符号差的 ADD/SUB relocation 已在 `applyFixup` 里显式创建，直接透传。`needsRelocateWithSymbol` 返回 `true`，因为 pcrel_lo 必须保留 `.Lpcrel_hi0`，不能替换成 section 符号，否则 linker 找不到对应的 auipc。`MiniRVMCAsmInfo` 继承 `MCAsmInfoELF`，设置 `CodePointerSize=8`、`CommentString="#"`、`AlignmentIsInBytes=false`、`SupportsDebugInformation=true`，并把 `DwarfFDERelSymbolSpec` 设为 `R_RISCV_32_PCREL`。

收益是 lld、objdump、qemu 直接可用；代价是工具按 RISC-V 规则解释对象。对象头（`/tmp/ch08.o`，第 3 节生成）里 `e_flags=0`，没有 RVC、没有浮点 ABI 标记，也没有独立的 `EM_MINIRV`：

```console
$ $B/llvm-readobj -h /tmp/ch08.o | grep -E 'Format:|OS/ABI:|Type:|Machine:|Flags'
Format: elf64-littleriscv
    OS/ABI: SystemV (0x0)
  Type: Relocatable (0x1)
  Machine: EM_RISCV (0xF3)
  Flags [ (0x0)
```

任何只根据 `EM_RISCV` 判断“这是标准 RISC-V”的工具，都会把 MiniRV 当成缺少扩展标记的 RISC-V 对象。调试够用，做产品时必须明确这条兼容性边界。

## 主线：Disassembler 把字节变回 MCInst

`Disassembler/MiniRVDisassembler.cpp` 的入口是 `getInstruction`：

```cpp
if (Bytes.size() < 4) {
  Size = 0;
  return MCDisassembler::Fail;
}
uint64_t Insn = support::endian::read32le(Bytes.data());
DecodeStatus Result =
    decodeInstruction(DecoderTable32, MI, Insn, Address, this, STI);
Size = 4;
return Result;
```

MiniRV 没有 16 位压缩指令，所以 `Size` 恒为 4。`DecoderTable32` 由 TableGen 生成，按 `Inst[6:0]`、`Inst[14:12]`、`Inst[31:25]` 逐层 switch，再调用操作数 decoder。手写 decoder 只有几个：`DecodeGPRRegisterClass` 查 `GPRDecoderTable` 并检查越界；`DecodeSImm12` 符号扩展 12 位；`DecodeSImm13Lsb0` 做 `SignExtend64<12>(Imm) << 1`；`DecodeSImm21Lsb0` 做 `SignExtend64<20>(Imm) << 1`；`DecodeUImm6`/`DecodeUImm5`/`DecodeUImm20` 直接加立即数。

decoder 只还原相对偏移，不加上 `Address`。是否显示绝对地址、是否符号化，是 InstPrinter 和 objdump 的事。MiniRV 的 `printBranchOperand` 两者都没做，所以反汇编看到 `8`、`-36`，不是 `0x28 <forward>`。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 操作数类 | `$TREE/llvm/lib/Target/MiniRV/MiniRVInstrInfo.td` | `SImm12AsmOperand`、`BranchTargetAsmOperand`、`simm13_lsb0`、`simm21_lsb0` | 谓词、编码器/解码器方法、打印方法如何绑定 |
| AsmParser | `$TREE/llvm/lib/Target/MiniRV/AsmParser/MiniRVAsmParser.cpp` | `matchAndEmitInstruction`、`ParseOperand`、`isSImm12`、`isBranchTarget` | 文本如何变成 `MCInst`，诊断如何选操作数 |
| InstPrinter | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVInstPrinter.cpp` | `printInst`、`printOperand`、`printBranchOperand`、`printRegName` | 规范名、分支偏移如何打印 |
| MCCodeEmitter | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVMCCodeEmitter.cpp` | `encodeInstruction`、`getMachineOpValue`、`getBranchTargetOpValue`、`getJalTargetOpValue` | 位如何拼、fixup 何时产生 |
| 生成编码表 | `$BUILD/lib/Target/MiniRV/MiniRVGenMCCodeEmitter.inc` | `getBinaryCodeForInstr` | `InstBits[]` 与字段拼装 |
| fixup 定义 | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVMCTargetDesc.h` | `enum Fixups` | 四种目标 fixup |
| AsmBackend | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVAsmBackend.cpp` | `getFixupKindInfo`、`applyFixup`、`evaluateFixup`、`writeNopData` | 位打散、符号差、pcrel_lo |
| ELF writer | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVELFObjectWriter.cpp` | `getRelocType`、`needsRelocateWithSymbol` | `EM_RISCV` 与 relocation |
| AsmInfo | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVMCAsmInfo.cpp` | `MiniRVMCAsmInfo`、`DwarfFDERelSymbolSpec` | 注释符、指针大小、FDE |
| Disassembler | `$TREE/llvm/lib/Target/MiniRV/Disassembler/MiniRVDisassembler.cpp` | `getInstruction`、`DecodeSImm12`、`DecodeSImm13Lsb0`、`DecodeSImm21Lsb0` | 定长 4 字节、decoder 分派 |
| 注册 | `$TREE/llvm/lib/Target/MiniRV/MCTargetDesc/MiniRVMCTargetDesc.cpp` | `LLVMInitializeMiniRVTargetMC`、`RegisterMCInstPrinter`、`RegisterMCCodeEmitter`、`RegisterMCAsmBackend` | MC 五件套如何挂到 TargetRegistry |

## 实测：一轮完整往返

### 1. 多指令 `-show-encoding`

```console
$ cat >/tmp/ch08.s <<'EOF'
start:
  add   x10, x11, x12
  addi  x10, x10, -1
  lui   x5, 0x12345
  auipc x6, 0x1
  ld    x7, 8(x8)
  sd    x9, -16(x10)
  slli  x11, x12, 63
  mul   x13, x14, x15
  beq   x1, x2, forward
  bne   x3, x4, start
forward:
  jal   x1, start
  jalr  x0, 0(x1)
EOF
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/ch08.s
start:
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
	addi	x10, x10, -1                    # encoding: [0x13,0x05,0xf5,0xff]
	lui	x5, 74565                       # encoding: [0xb7,0x52,0x34,0x12]
	auipc	x6, 1                           # encoding: [0x17,0x13,0x00,0x00]
	ld	x7, 8(x8)                       # encoding: [0x83,0x33,0x84,0x00]
	sd	x9, -16(x10)                    # encoding: [0x23,0x38,0x95,0xfe]
	slli	x11, x12, 63                    # encoding: [0x93,0x15,0xf6,0x03]
	mul	x13, x14, x15                   # encoding: [0xb3,0x06,0xf7,0x02]
	beq	x1, x2, forward                 # encoding: [0x63'A',0x80'A',0x20'A',A]
                                        #   fixup A - offset: 0, value: forward, kind: fixup_minirv_branch
	bne	x3, x4, start                   # encoding: [0x63'A',0x90'A',0x41'A',A]
                                        #   fixup A - offset: 0, value: start, kind: fixup_minirv_branch
forward:
	jal	x1, start                       # encoding: [0xef'A',A,A,A]
                                        #   fixup A - offset: 0, value: start, kind: fixup_minirv_jal
	jalr	x0, 0(x1)                       # encoding: [0x67,0x80,0x00,0x00]
```

`lui x5, 0x12345` 打印成 `74565`，`auipc x6, 0x1` 打印成 `1`：`printOperand` 对立即数统一用十进制，文本往返不保证字面形式不变。带标签的分支显示 `A` 占位符和 `fixup ... kind`，不是最终字节；`-show-encoding` 是流式视图，布局还没定。

### 2. 同一批指令交给 RISC-V

用一段更短的相同输入对比文本层：

```console
$ cat >/tmp/ch08cmp.s <<'EOF'
add x10, x11, x12
ld x7, 8(x8)
beq x1, x2, 8
jalr x0, 0(x1)
EOF
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/ch08cmp.s
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
	ld	x7, 8(x8)                       # encoding: [0x83,0x33,0x84,0x00]
	beq	x1, x2, 8                       # encoding: [0x63,0x84,0x20,0x00]
	jalr	x0, 0(x1)                       # encoding: [0x67,0x80,0x00,0x00]
$ $B/llvm-mc -triple=riscv64 -show-encoding /tmp/ch08cmp.s
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
	ld	t2, 8(s0)                       # encoding: [0x83,0x33,0x84,0x00]
	beq	ra, sp, 8                       # encoding: [0x63,0x84,0x20,0x00]
	ret                                     # encoding: [0x67,0x80,0x00,0x00]
```

编码字节逐条一致，差异全在文本层：MiniRV 打印 `x10`/`x8`/`x1`，RISC-V 打印 `a0`/`s0`/`ra`；`jalr x0, 0(x1)` 被 RISC-V 别名成 `ret`。全量例子里的 `mul` 需要 `-mattr=+m`：MiniRV 是 RV64IM，通用 `riscv64` 默认不启用 M。

### 3. 生成对象并用 objdump 反汇编

```console
$ $B/llvm-mc -triple=minirv64 -filetype=obj /tmp/ch08.s -o /tmp/ch08.o
$ $B/llvm-objdump -d --triple=minirv64 /tmp/ch08.o

/tmp/ch08.o:	file format elf64-littleriscv

Disassembly of section .text:

0000000000000000 <start>:
       0: 33 85 c5 00  	add	x10, x11, x12
       4: 13 05 f5 ff  	addi	x10, x10, -1
       8: b7 52 34 12  	lui	x5, 74565
       c: 17 13 00 00  	auipc	x6, 1
      10: 83 33 84 00  	ld	x7, 8(x8)
      14: 23 38 95 fe  	sd	x9, -16(x10)
      18: 93 15 f6 03  	slli	x11, x12, 63
      1c: b3 06 f7 02  	mul	x13, x14, x15
      20: 63 84 20 00  	beq	x1, x2, 8
      24: e3 9e 41 fc  	bne	x3, x4, -36

0000000000000028 <forward>:
      28: ef f0 9f fd  	jal	x1, -40
      2c: 67 80 00 00  	jalr	x0, 0(x1)
```

`forward` 在 `0x28`，所以 `beq` 偏移 `+8`；`bne` 回 `start` 是 `-36`；`jal` 回 `start` 是 `-40`。objdump 仍只打印数字，不打印 `<start>`，原因就是 `printBranchOperand` 不参与符号化。

### 4. 用十六进制文本直接喂给 `-disassemble`

```console
$ cat /tmp/ch08.hex
0x33 0x85 0xc5 0x00 0x13 0x05 0xf5 0xff 0xb7 0x52 0x34 0x12 0x17 0x13 0x00 0x00
0x83 0x33 0x84 0x00 0x23 0x38 0x95 0xfe 0x93 0x15 0xf6 0x03 0xb3 0x06 0xf7 0x02
0x63 0x84 0x20 0x00 0xe3 0x9e 0x41 0xfc 0xef 0xf0 0x9f 0xfd 0x67 0x80 0x00 0x00
$ $B/llvm-mc -triple=minirv64 -disassemble /tmp/ch08.hex
	add	x10, x11, x12
	addi	x10, x10, -1
	lui	x5, 74565
	auipc	x6, 1
	ld	x7, 8(x8)
	sd	x9, -16(x10)
	slli	x11, x12, 63
	mul	x13, x14, x15
	beq	x1, x2, 8
	bne	x3, x4, -36
	jal	x1, -40
	jalr	x0, 0(x1)
```

这一轮没有 ELF 解析参与，标签信息完全丢失，分支只剩相对偏移；这是 MC 反汇编的固有限制，不是 MiniRV 的 bug。

### 5. 外部符号：fixup 变成 relocation

```console
$ printf 'beq x1, x2, ext\njal x1, ext\n' >/tmp/ch08ext.s
$ $B/llvm-mc -triple=minirv64 -filetype=obj /tmp/ch08ext.s -o /tmp/ch08ext.o
$ $B/llvm-readobj --relocations /tmp/ch08ext.o | sed -n '/Relocations \[/,/^]/p'
Relocations [
  Section (3) .rela.text {
    0x0 R_RISCV_BRANCH ext 0x0
    0x4 R_RISCV_JAL ext 0x0
  }
]
```

`applyFixup` 在未解析时调 `maybeAddReloc` 后返回，编码器写出的立即数保持 0；linker 在最终布局里回填。`R_RISCV_BRANCH`/`R_RISCV_JAL` 的编号和语义都来自 RISC-V，这就是复用 `EM_RISCV` 的直接收益。

### 6. CodeGen 的 pcrel 对

```console
$ cat >/tmp/glob.ll <<'EOF'
@g = global i64 7
define i64 @loadg() {
  %v = load i64, ptr @g
  ret i64 %v
}
EOF
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj /tmp/glob.ll -o /tmp/glob.o
$ $B/llvm-readobj --relocations /tmp/glob.o | grep -E 'R_RISCV_(PCREL|32_PCREL)'
    0x0 R_RISCV_PCREL_HI20 g 0x0
    0x4 R_RISCV_PCREL_LO12_I .L0  0x0
    0x1C R_RISCV_32_PCREL .L0  0x0
```

`R_RISCV_PCREL_LO12_I` 关联的是 `.L0` 这个 auipc 标签，不是 `g`；这正是 `evaluateFixup` 里 `getPCRelHiFixup` 维护的配对关系，也是 `needsRelocateWithSymbol=true` 要保留符号的原因。

## 寄存器名：为什么 MiniRV 打印 `x10` 而 RISC-V 打印 `a0`

根因在 `MiniRVRegisterInfo.td`：

```tablegen
class MiniRVReg<bits<5> num, string n> : Register<n> {
  let Namespace = "MiniRV"; let HWEncoding{4-0} = num; let DwarfNumbers = [num];
}
foreach i = 0-31 in { def X#i : MiniRVReg<i, "x"#i>; }
```

`Register<n>` 的 `AsmName` 是 `xN`，没有 `AltNames`；`MiniRVInstPrinter::printRegName` 又只调用规范名 `getRegisterName(Reg)`。所以打印器没有“ABI 别名”这个概念。RISC-V 定义了 `ABIRegAltName` 和每个寄存器的 `AltNames`（`X10` 是 `["a0"]`），`RISCVInstPrinter::printRegName` 默认调用 `getRegisterName(Reg, RISCV::ABIRegAltName)`；汇编器仍接受 `x10` 和 `a0`，但输出统一成 ABI 名。

```console
$ printf 'add x10, x11, x12\nadd a0, a1, a2\n' >/tmp/alias2.s
$ $B/llvm-mc -triple=minirv64 -show-encoding /tmp/alias2.s
/tmp/alias2.s:2:5: error: invalid operand for instruction
add a0, a1, a2
    ^
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
$ $B/llvm-mc -triple=riscv64 -show-encoding /tmp/alias2.s
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
```

MiniRV 对 `a0` 报 `invalid operand for instruction`，因为 `a0` 被当成表达式，而不是“寄存器拼写错误”。

### 想加别名，要改三处

1. **寄存器定义**：增加 `RegAltNameIndex`，给 `MiniRVReg` 加 `AltNames` 参数：

```tablegen
def ABIRegAltName : RegAltNameIndex;
class MiniRVReg<bits<5> num, string n, list<string> alt = []> : Register<n> {
  let Namespace = "MiniRV"; let HWEncoding{4-0} = num;
  let DwarfNumbers = [num]; let AltNames = alt;
}
let RegAltNameIndices = [ABIRegAltName] in {
  def X0 : MiniRVReg<0, "x0", ["zero"]>;
  def X1 : MiniRVReg<1, "x1", ["ra"]>;
  def X10 : MiniRVReg<10, "x10", ["a0"]>;
}
```

2. **打印器**：把 `printRegName`/`printOperand` 改成走 `getRegisterName(Reg, MiniRV::ABIRegAltName)`。只加 `AltNames` 不改打印机，结果是“能输入 `a0`，但输出仍然 `x10`”。

3. **测试**：`llvm/test/MC/MiniRV/basic.s` 加 `add a0, a1, a2`，确认接受且输出统一成 `a0`；再跑 `llvm-lit llvm/test/MC/MiniRV`。只想放宽输入、保留 `x10` 输出，只做第 1 步即可。

## 分支立即数为什么要打散

B/J 型把立即数拆开，是为了给 `rs1`、`rs2`、`rd`、`funct3`、`opcode` 留固定位置。源码里连续的字节偏移，编码后分散在多个不连续位段。

B 型布局：

```
bit 31  | 30..25    | 24..20 | 19..15 | 14..12 | 11..8    | 7       | 6..0
imm[12] | imm[10:5] | rs2    | rs1    | funct3 | imm[4:1] | imm[11] | opcode
```

编码器先做 `imm >> 1`，把字节偏移变成“半字”单位，再取低 12 位。`0x20` 处的 `beq x1, x2, 0x28`，偏移 `+8`：

```
8 >> 1 = 4 = 0b0000_0000_0100
imm[12]=0, imm[10:5]=000000, imm[4:1]=0100, imm[11]=0
```

拼出的字是 `0x00208463`，小端字节 `63 84 20 00`。解码时 `DecodeSImm13Lsb0` 把 12 位字段符号扩展，再左移 1：`SignExtend64<12>(4) << 1 = 8`。

负偏移同样成立。`0x24` 处的 `bne x3, x4, 0x00`，偏移 `-36`：

```
-36 的 64 位补码右移 1 位，截低 12 位 = 0xFEE = 0b1111_1110_1110
imm[12]=1, imm[10:5]=111110, imm[4:1]=1110, imm[11]=1
```

拼出的字是 `0xFC419EE3`，字节 `e3 9e 41 fc`。解码：`0xFEE` 作为 12 位有符号数是 `-18`，`-18 << 1 = -36`。

J 型布局：

```
bit 31  | 30..21    | 20      | 19..12     | 11..7 | 6..0
imm[20] | imm[10:1] | imm[11] | imm[19:12] | rd    | opcode
```

`0x28` 处的 `jal x1, 0x00`，偏移 `-40`：

```
-40 >> 1 = -20 = 0xFFFEC（20 位补码）
imm[20]=1, imm[10:1]=0x3EC, imm[11]=1, imm[19:12]=0xFF
```

拼出的字是 `0xFD9FF0EF`，字节 `ef f0 9f fd`。解码：`0xFFFEC` 作为 20 位有符号数是 `-20`，`-20 << 1 = -40`。

这就是为什么 `applyFixup` 不能复用通用连续位段：它必须在已写好的 32 位字上，分别清 bit31-25/11-7（B）或 bit31-12（J），再把 12/20 位值按上表塞回去。`getFixupKindInfo` 的 `0/32` 只是把“整条指令”交给它。

## 反直觉与踩雷

1. **错误直觉**：`-show-encoding` 的 `encoding:` 就是最终对象字节。  
   **正确认识**：带标签的分支显示 `A` 占位符；最终字节在 `-filetype=obj` + `applyFixup` 之后才确定。绝对立即数分支才会直接给出完整字节。

2. **错误直觉**：MiniRV 不打印 `a0` 是打印器漏了别名。  
   **正确认识**：`MiniRVRegisterInfo.td` 没有 `AltNames`，`getRegisterName(Reg)` 只能返回规范名。只加 `AltNames` 而不改 `printRegName`/`printOperand`，结果是“能输入别名，但输出仍是 `x10`”。

3. **错误直觉**：分支第三个操作数是“指令条数”。  
   **正确认识**：它是字节偏移，且必须是偶数；`isShiftedInt<12, 1>` 检查右移 1 位后的有符号 12 位范围。`beq ..., 3` 和 `beq ..., 4096` 都会被拒绝。

4. **错误直觉**：汇编器会像 CodeGen 一样自动处理超范围分支。  
   **正确认识**：MiniRV 的 `MiniRVAsmBackend` 没有 `relaxInstruction`；`isBranchTarget` 对标签一律放行，`applyFixup` 只做 `& 0xfff`，5 KB 外的 `beq` 会静默变成 `-3188`。`.ll` 输入靠 `BranchRelaxation` pass，手写 `.s` 没有这层保险。

5. **错误直觉**：AsmPrinter 能打印的文本，`llvm-mc` 一定能重新汇编。  
   **正确认识**：当前 MiniRV 的文本汇编器只接受绝对立即数；`PseudoLA` 打印出的 `auipc x3, g` / `addi x3, x3, .Lpcrel_hi0` 会被 `isUImm20`/`isSImm12` 拒绝。pcrel 对目前只在 `MCInst`/CodeGen 路径成立，文本回灌会报错。

## 进阶：再往深一层

`MachineInstr` 里的虚拟寄存器、`MachineBasicBlock`、调度信息在 MC 层都不存在。MC 层只认物理寄存器和 `MCExpr`，所以它既能服务编译器，也能服务独立汇编器；`MCAssembler::evaluateFixup` 里的 `Value -= getFragmentOffset(F) + Fixup.getOffset()` 就是 PC-relative 的具体含义。

`MCFixupKindInfo` 不是位布局的唯一来源：位段连续（如 `lo12_i` 的 `{20, 12}`）可用通用布局，不连续（B/J）必须写 `{0, 32}` 并自己 `applyFixup`。文本 pcrel 的补法则是让 AsmPrinter 打印 `%pcrel_hi(sym)`/`%pcrel_lo(label)`，让 AsmParser 增加对应操作数类和 `MCExpr` 解析，再接上 `AUIPC`/`ADDI` 分支；只改 `isUImm20` 不够，因为编码器需要知道这是 pcrel。

加一条新指令：在 `auto-td/instructions/` 加 YAML（或手写 `.td`），TableGen 同时生成 matcher、编码位、decoder 表和 printer fragment，再补 `-show-encoding` 与 objdump 往返测试。MiniRV 的 14 条初始指令就是这样长出来的，练习 A 会带你走一遍。

## 本章重点整理

- MC 正向：`AsmLexer → MiniRVAsmParser → MCInst → MiniRVMCCodeEmitter → bytes + fixup → MiniRVAsmBackend → MiniRVELFObjectWriter → .o`。
- MC 反向：`.text bytes → MiniRVDisassembler → MCInst → MiniRVInstPrinter → text`。
- `isSImm12`/`isUImm20`/`isUImm6`/`isUImm5` 只接受绝对常量；`isBranchTarget`/`isJalTarget` 对非绝对表达式放行，这是标签能进入 `MCInst` 的关键。
- `MatchInstructionImpl` 做助记符和操作数类匹配；`matchAndEmitInstruction` 把 `Match_Invalid*` 映射成具体诊断。
- `printOperand` 对寄存器走 `getRegisterName`，对立即数走十进制，对表达式走 `MAI.printExpr`；`printBranchOperand` 只是 `printOperand`。
- `getBinaryCodeForInstr` 是生成代码；`getMachineOpValue` 负责寄存器/立即数/表达式分派，表达式只允许 `AUIPC`/`ADDI` 的 pcrel 对。
- `getBranchTargetOpValue`/`getJalTargetOpValue` 对立即数做 `>>1` 并返回 12/20 位值；对标签生成 `fixup_minirv_branch`/`fixup_minirv_jal`。
- `getFixupKindInfo` 对目标 fixup 统一写 `0/32`；`applyFixup` 负责 B/J/pcrel 的不连续位摆放、符号差 ADD/SUB、数据 fixup。
- `MiniRVELFObjectWriter` 复用 `EM_RISCV`、生成 RELA、保留 pcrel_lo 符号；`e_flags=0` 是兼容性边界。
- MiniRV 没有 MC relaxation；手写汇编的超范围分支会静默截断，CodeGen 侧靠 `BranchRelaxation`，链接侧靠 `--no-relax`。
- 寄存器别名由 `RegAltNameIndex` + `AltNames` + `getRegisterName(Reg, AltIdx)` 三件事共同决定，缺一不可。

## 自我检核

- [ ] 画出 `source text → object` 和 `object → text` 两条流水线，标出 `MCInst`、`MCFixup`、`MCExpr` 出现的位置。
- [ ] 解释为什么 `beq x1, x2, 3` 被拒绝，而 `beq x1, x2, start` 能通过；两者的谓词路径有什么不同？
- [ ] 给定 `beq x1, x2, -36`，写出 `imm >> 1` 的 12 位值、四个 B 型字段和最终 32 位字。
- [ ] 给定 `jal x1, -40`，写出 J 型的四个字段，并解释 `DecodeSImm21Lsb0` 如何还原。
- [ ] 为什么 `getFixupKindInfo` 对 B/J 型写 `0/32`，而不是写 `{31,1}`、`{25,6}` 这种精确位段？
- [ ] MiniRV 的 `applyFixup` 如何处理 `A - B`？为什么要发两条 relocation？

## 延伸阅读

- **[LLVM MC Layer](https://llvm.org/docs/CodeGenerator.html#the-mc-layer)** — LLVM Project
  - **读哪里**：`The MC Layer` 整节，重点看 `MCInst`、`MCStreamer`、`MCAssembler`、`MCAsmBackend`、`MCObjectWriter` 的职责边界。
  - **能学到什么**：为什么 MC 层要独立于 CodeGen，fixup 和 relocation 分别在什么时候处理。
  - **前提**：读完 Ch 07，知道 `MCInstrInfo`/`MCRegisterInfo`/`MCSubtargetInfo` 是什么。

- **[TableGen BackEnds](https://llvm.org/docs/TableGen/BackEnds.html)** — LLVM Project
  - **读哪里**：AsmWriter、AsmMatcher、Disassembler、MCCodeEmitter 四个 backend 的输出说明。
  - **能学到什么**：`MiniRVGenAsmMatcher.inc` / `MiniRVGenMCCodeEmitter.inc` / `MiniRVGenAsmWriter.inc` / `MiniRVGenDisassemblerTables.inc` 里的表是怎么从 `.td` 长出来的。
  - **前提**：读过 Ch 06，能看懂 `Instruction`、`Operand`、`AsmOperandClass` 的基本写法。

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V International
  - **读哪里**：寄存器命名/ABI 别名、ELF object files、relocation 类型、`e_flags`。
  - **能学到什么**：`a0`/`x10` 的对应关系，`R_RISCV_BRANCH`/`R_RISCV_JAL`/`R_RISCV_PCREL_*` 的语义，以及复用 `EM_RISCV` 时哪些标志必须一致。
  - **前提**：知道 RISC-V 基本整数指令和调用约定；可配合 `../riscv_backend/README.md`。

→ [Ch 09 TargetMachine、Subtarget 与 TargetLowering](./09-targetmachine-subtarget-lowering.md)
