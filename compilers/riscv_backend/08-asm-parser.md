# Ch 08 汇编器：从一行文本到一条 MCInst

> 目标：把 `llvm-mc` 处理 RISC-V 汇编的过程拆成“解析 → 匹配 → 展开 → 编码”四步。读完本章，你能解释为什么输入 `jal ra, 2048` 会回显成 `jal 2048`，为什么 `xori a0, a1, -1` 会回显成 `not a0, a1`，为什么 `li a0, 2048` 会变成两条编码，以及一条 `addi` 的立即数为什么在 4096 处失败。

> 环境：Windows；LLVM 23.1.4（Optimized build，host x86_64-pc-windows-msvc）；PowerShell。LLVM 工具在 `.scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin`，本课程统一入口是 `compilers/riscv_backend/code/tools/run.ps1`。所有 `llvm-mc` 命令都显式带 `-triple=riscv64`。

> 约定：标“实测”的片段来自本机 23.1.4 重跑；标“源码依据”的结论来自 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV` 的 23.1.4 源码；标 `guided` 的部分需要重编译 llc/llvm-mc 才能验证。

## 为什么

汇编器最容易被误解成“把助记符替换成字节”。如果真是这样，输入什么文本就应该回显什么文本，`-show-encoding` 的输出应当和输入逐字对应。实际并非如此：本机把 `jal ra, 2048` 回显成 `jal 2048`，把 `jalr ra, 0(a0)` 回显成 `jalr a0`，把 `xori a0, a1, -1` 回显成 `not a0, a1`，还把 `c.addi a0, 1` 回显成 `addi a0, a0, 1`。这些差异不是显示层的小毛病，而是“解析出的 MCInst 被编码器压缩、被打印器按别名重新打印”的必然结果。

理解这条链路有四个直接收益。第一，看到报错时能判断问题发生在词法、操作数约束、特征检查还是编码阶段。第二，看到 `li` 展开成多条指令时不会误以为汇编器“多此一举”，那是 64 位立即数必须走的指令序列。第三，看到 `call foo` 占 8 字节且带 `R_RISCV_CALL_PLT` 时，能把它和链接期 relax 联系起来。第四，读 TableGen 生成的匹配器时，知道 `Match_Success`、`Match_MnemonicFail`、`Match_NearMisses` 分别对应哪类用户可见错误。

本章只讲汇编器这一层。指令编码的位域细节放到第 06 章，反汇编的逆向过程放到第 09 章，MC 层的对象与流写到第 10 章。这里的主线是：**文本不是事实，MCInst 才是事实；回显是 MCInst 经打印器得到的规范化文本。**

## 主线

### 8.1 入口：parseInstruction 先改助记符，再收操作数

`RISCVAsmParser::parseInstruction` 的第一件事不是创建操作数，而是调用 `applyMnemonicAliases(Name, AvailableFeatures, 0)`。源码里的注释说明了原因：目标助记符可能需要自定义的操作数解析，而 TableGen 生成的匹配器要到 `MatchInstructionImpl` 才应用别名，那时已经太晚。于是 `move` 会在解析前被改成 `mv`，`scall` 被改成 `ecall`，`sbreak` 被改成 `ebreak`。这些是 `MnemonicAlias`，只改名字，不涉及操作数形态。

完成别名替换后，`parseInstruction` 把助记符本身作为第 0 个操作数压进 `Operands`，然后循环调用 `parseOperand`，用逗号分隔，最后要求行尾。也就是说，后续匹配器看到的第一个“操作数”是助记符 token，真正机器操作数从下标 1 开始。理解这一点，读生成的匹配表时就不会把 `MCK_GPRNoX0` 的下标数错。

`RISCVAsmParser::parseOperand` 的职责是按当前助记符选择操作数语法。RISC-V 的操作数形态很多：普通寄存器、括号内的基址寄存器、带 `%lo` / `%pcrel_hi` 的符号表达式、CSR、浮点舍入模式、向量寄存器组、压缩指令的受限寄存器类。解析器不是先解析成通用表达式再让匹配器猜，而是在解析阶段就按助记符分流。这也是为什么 `jalr ra, 0(a0)` 能正确构造出“rd + rs1 + imm”的三操作数形态，而 `jalr a0` 会走另一条别名路径。

### 8.2 寄存器：x 编号、ABI 名与受限寄存器类

寄存器解析入口是 `RISCVAsmParser::tryParseRegister`，内部调用 `matchRegisterNameHelper`。这个 helper 先调用生成的 `MatchRegisterName(Name)`，失败后再调用 `MatchRegisterAltName(Name)`；随后对 RVE 做过滤：如果目标是 RVE，`x16` 到 `x31` 会被拒绝，返回无效寄存器。

这解释了两件事。第一，`x10` 与 `a0` 是同一个寄存器的两个名字，汇编器都接受；打印器默认打印 ABI 名，所以 `-show-encoding` 回显 `a0`。第二，浮点寄存器有 16/32/64/128 位多个“同名”定义（`F0_H`、`F0_F`、`F0_D`、`F0_Q`），`matchRegisterNameHelper` 里用 `static_assert` 保证默认匹配到 64 位变体 `F0_D`，再用 ABI 别名补齐。浮点寄存器名字在源码里不是一条简单映射，而是“同一硬件编号的多个宽度视图”。

受限寄存器类由 TableGen 的 `RegisterClass` 和操作数类描述，例如 `GPRNoX0`、`GPRNoX0X2`、`GPRJALR`、`SP`、`GPRC`。汇编器本身不检查“这个寄存器是否在允许集合里”，它把寄存器当成一个 `MCRegister`，由生成的匹配器在 `MatchClassKind` 层判断。匹配失败时，`ReportNearMisses` 会把 TableGen 里声明的 `DiagnosticString` 打印出来。实测 `c.addi nope, 1` 得到的 note 是 `register must be a GPR excluding zero (x0)`，这条字符串就来自压缩指令操作数类的诊断描述。

### 8.3 立即数：范围与对齐是编码约束，不是“数值大小”

RISC-V 没有“一个通用立即数操作数”。`.td` 里给不同指令格式声明了不同的操作数类：I 型是 `simm12`，S 型是 `simm12` 的存储形态，B 型是 `simm13_lsb0`，J 型是 `simm21_lsb0`，U 型是 `uimm20`。解析器把这些文本转成 `MCOperand::Imm`，匹配器再用生成的操作数判定函数检查范围与低位对齐。

实测 `addi a0, a1, 4096`：

```text
<stdin>:1:14: error: operand must be a symbol with %lo/%pcrel_lo/%tprel_lo specifier or an integer in the range [-2048, 2047]
```

实测 `beq a0, a1, 5000`：

```text
<stdin>:1:13: error: immediate must be a multiple of 2 bytes in the range [-4096, 4094]
```

这两条错误信息把“范围”和“对齐”分开说了。`addi` 的 12 位有符号立即数范围是 `[-2048, 2047]`，没有对齐要求；`beq` 的 13 位立即数最低位恒为 0，所以范围写成 `[-4096, 4094]` 且必须是 2 的倍数。注意 `4096` 不是“太大到无法表示”，它刚好越过 12 位有符号上限一位；`-2048` 是合法的，`2048` 不是。

同一条规则也解释了 `lui a0, 0x12345` 的回显为什么是十进制 `74565`。`uimm20` 的合法范围是 20 位无符号数，`0x12345` 合法；打印器默认用十进制打印立即数，除非启用了十六进制选项。数值没变，只是表示法变了。

### 8.4 MatchInstructionImpl：一张按助记符排序的表

`RISCVAsmParser::matchAndEmitInstruction` 创建 `MCInst Inst` 和 `SmallVector<NearMissInfo, 4> NearMisses`，然后调用 `MatchInstructionImpl(Operands, Inst, &NearMisses, MatchingInlineAsm)`。这个函数不是手写的，而是 `llvm-tblgen -gen-asm-matcher` 生成的。

本机重跑生成命令：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
& "$bin/llvm-tblgen.exe" -I $inc -I $td -gen-asm-matcher "$td/RISCV.td" -o "$env:TEMP/RISCVGenAsmMatcher.inc"
```

实测产物为 1,097,394 字节、13,623 行（brief 记的是 1,071.7 KB，按 KiB 换算一致）。生成文件里能看到匹配器的骨架：先取 `StringRef Mnemonic = Operands[0].getToken()`，再 `applyMnemonicAliases`，然后用 `std::equal_range(Start, End, Mnemonic, LessOpcode())` 在 `MatchTable0` 里找同名区间。对区间内每个 `MatchEntry`，先检查 `FeatureBitsets[it->RequiredFeaturesIdx]`，再按 `Classes[FormalIdx]` 逐个匹配操作数。

返回类型只有三种路径：

- `Match_Success`：构造好 `MCInst`，交给 `validateInstruction` 和 `processInstruction`。
- `Match_MnemonicFail`：助记符根本不在表里，调用 `RISCVMnemonicSpellCheck` 给出拼写建议，报 `unrecognized instruction mnemonic`。
- `Match_NearMisses`：助记符在表里，但操作数或特征不满足，走 `ReportNearMisses`。

`ReportNearMisses` 的行为也能在源码里读到：没有 near-miss 时只报 `invalid instruction`；只有一个时把它当成唯一错误；多于一个时先报 `invalid instruction, any one of the following would fix this:`，再逐条 `Note`。实测 `add a0, a1, nope` 得到的就是这条“多候选”错误，并附上 `operand must be a symbol ... or an integer in the range [-2048, 2047]` 的提示。原因是第三个操作数既可能是寄存器，也可能是符号表达式；解析器把它当成了符号，匹配器于是给出“改哪个地方就能修好”的候选说明。

### 8.5 特征检查：`mul` 报的是 Zmmul，不是 M

实测在只给 `-mattr=+c` 时汇编 `mul a0, a1, a2`：

```text
<stdin>:1:1: error: instruction requires the following: 'Zmmul' (Integer Multiplication)
```

这里有一个容易误判的细节：错误信息要求的是 `Zmmul`，不是 `M`。原因在 23.1.4 的指令定义里，乘法子集已经被拆成独立的 `Zmmul`，`M` 蕴含 `Zmmul`。所以加 `-mattr=+m` 能汇编 `mul`，但错误文本仍可能显示 `Zmmul`。同理，`fadd.d` 在只给 `+c` 时报：

```text
<stdin>:1:1: error: instruction requires the following: 'D' (Double-Precision Floating-Point)
```

特征检查发生在匹配表的每个 entry 上，而不是在编码阶段。匹配器先算 `AvailableFeatures` 与 `RequiredFeatures` 的交集；不满足就记录 feature near-miss。于是同一条指令可以有“操作数不对”和“特征不够”两类失败原因，最终由 `FilterNearMisses` 选出对用户最有用的那条。

### 8.6 别名：InstAlias 的 EmitPriority 决定回显

`RISCVInstrInfo.td` 里有一组关键的 `InstAlias`：

```tablegen
def : InstAlias<"nop",           (ADDI      X0,      X0,           0), 3>;
def : InstAlias<"li $rd, $imm",  (ADDI GPR:$rd,      X0, simm12_lo:$imm), 2>;
def : InstAlias<"mv $rd, $rs",   (ADDI GPR:$rd, GPR:$rs,           0)>;
def : InstAlias<"not $rd, $rs",  (XORI GPR:$rd, GPR:$rs,      -1)>;
def : InstAlias<"j $offset",   (JAL X0, simm21_lsb0_jal:$offset)>;
def : InstAlias<"jal $offset", (JAL X1, simm21_lsb0_jal:$offset)>;
def : InstAlias<"ret",                   (JALR      X0,      X1, 0), 4>;
def : InstAlias<"jalr $rd, $rs",         (JALR GPR:$rd, GPR:$rs, 0), 2>;
```

`InstAlias` 的第三个参数是 `Emit`，默认值为 1（见预编译头 `llvm/Target/Target.td` 的 `class InstAlias<string Asm, dag Result, int Emit = 1, ...>`，字段名 `EmitPriority`）。打印一条 MCInst 时，打印器会枚举所有目标 opcode 相同的别名，选优先级最高的那条来打印；指令自身的 `AsmString` 优先级最低。

这解释了三组实测回显：

- 输入 `jal ra, 2048`：解析成 `JAL X1, 2048`。别名 `jal $offset` 的目标也是 `JAL X1`，优先级 1；指令自身的 `"jal", "$rd, $imm20"` 优先级 0。打印器选别名，输出 `jal 2048`。
- 输入 `jalr ra, 0(a0)`：匹配到 `"jalr $rd, $rs"`（优先级 2），目标 `JALR X1, X10, 0`。优先级 4 的 `ret` 目标要求 `JALR X0, X1, 0`，不匹配；优先级 3 的 `jalr $rs` 目标要求 `JALR X1, GPR, 0`，这里 `rs` 是 `a0`，也不匹配。于是输出 `jalr a0`。
- 输入 `xori a0, a1, -1`：`XORI` 的别名 `not` 优先级 1，指令 `xori` 自身优先级 0，输出 `not a0, a1`。

`mv` 与 `li` 同理。`mv a0, a1` 先被压缩成 `C_MV`，打印时 `uncompress` 把它变回 `ADD a0, x0, a1`，再匹配 `mv` 别名，输出 `mv a0, a1`。`addi x0, x0, 1` 更极端：`ADDI X0, X0, 1` 同时匹配 `nop`（要求立即数 0，不成立）、`li`（目标 `ADDI GPR, X0, imm`，成立，优先级 2）和 `mv`（要求立即数 0，不成立），所以回显成 `li zero, 1`。写 `x0` 的指令在语义上会被丢弃，但汇编器仍然接受并给出编码。

### 8.7 伪指令：`li` 是解析期的多指令展开，`call` 是编码期的双指令展开

`processInstruction` 是“匹配成功后”的分派器。它按 `Inst.getOpcode()` 走不同分支：

- `PseudoLI`：取立即数，调用 `emitLoadImm(Reg, Imm, Out)`。这个 helper 用 `RISCVMatInt::generateInstSeq` 生成一条或多条真实指令。实测 `li a0, 2048` 生成 `li a0, 1` 与 `slli a0, a0, 11` 两条；`li a0, -1` 只生成一条 `ADDI a0, x0, -1`，再按 `li` 别名打印。
- `PseudoLA` / `PseudoLLA` / `PseudoLGA`：走 `emitLoadAddress` / `emitLoadLocalAddress` / `emitLoadGlobalAddress`，最终由 `emitAuipcInstPair` 生成 `auipc` + 第二条指令，并挂 `%pcrel_hi` / `%pcrel_lo` 表达式。
- `PseudoCALL` / `PseudoTAIL`：不在 AsmParser 里展开。它们是带 `Size = 8` 的伪指令，由 `RISCVMCCodeEmitter::encodeInstruction` 展开成 `auipc` + `jalr`，并发出 `fixup_riscv_call_plt`。实测 `call foo` 的编码是 `[0x97'A',A,A,A,0xe7'A',0x80'A',A,A]`，带 `fixup A - offset: 0, value: foo, kind: fixup_riscv_call_plt`；`-filetype=obj` 后符号表里出现 `R_RISCV_CALL_PLT`。
- `PseudoRET`：在 `.td` 里用 `PseudoInstExpansion<(JALR X0, X1, 0)>` 描述，最终打印成 `ret`。

把“展开发生在哪一层”分清很重要。`li` 展开后，`-show-encoding` 会打印两条独立编码；`call` 展开后仍是一条汇编文本、8 字节编码，但对象文件里有两个重定位（`R_RISCV_CALL_PLT` 和 `R_RISCV_RELAX`，后者在 brief 的 relax 实验里出现）。同样是“伪指令”，一个在解析期变成多条 MCInst，一个在编码期变成两段机器码。

### 8.8 失败与边界：未知寄存器、缺扩展、符号重定位

未知寄存器的报错取决于上下文。实测三种：

```text
addi nope, a1, 1     -> <stdin>:1:6: error: invalid operand for instruction
jalr ra, 0(nope)     -> <stdin>:1:12: error: expected register
c.addi nope, 1       -> error: invalid instruction, any one of the following would fix this:
                        note: register must be a GPR excluding zero (x0)
```

第一种是“第一个操作数必须是 GPR，`nope` 被解析成符号”；第二种是括号内语法明确要求寄存器，解析器直接拒绝；第三种是压缩指令的受限寄存器类不匹配，走 near-miss。三者的差异来自解析路径，不是错误处理不一致。

符号重定位实验：把下面内容写成 `riscv-ch08.asm`，用 `llvm-mc -filetype=obj` 汇编，再读符号与重定位。

```asm
	.text
	.globl	foo
foo:
	ret
	.globl	use
use:
	call	foo
	tail	foo
	la	a0, g
	ret
	.data
	.globl	g
	.p2align 2
g:
	.word	42
```

实测 `llvm-readobj --symbols --relocations`：

```text
Relocations [
  Section (3) .rela.text {
    0x4 R_RISCV_CALL_PLT foo 0x0
    0xC R_RISCV_CALL_PLT foo 0x0
    0x14 R_RISCV_PCREL_HI20 g 0x0
    0x18 R_RISCV_PCREL_LO12_I .Lpcrel_hi0 0x0
  }
]
```

符号表里还能看到 `$xrv64i2p1`（`.text` 的映射符号）和 `$d`（`.data` 的映射符号），以及局部符号 `.Lpcrel_hi0`。`call` 与 `tail` 都产生 `R_RISCV_CALL_PLT`；`la` 产生一对 `R_RISCV_PCREL_HI20` / `R_RISCV_PCREL_LO12_I`。这些重定位由汇编器在流写对象时落盘，链接器再决定是否 relax。

### 8.9 用 `-show-inst` 看 MCInst，别只看文本

`llvm-mc` 的 `-show-inst` 会把匹配后的 MCInst 打印出来。实测：

```powershell
@'
addi a0, a1, 42
jal  ra, 2048
lw   a0, 8(sp)
'@ | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -show-inst -show-encoding
```

```text
	addi	a0, a1, 42                      # encoding: [0x13,0x85,0xa5,0x02]
                                        # <MCInst #13727 ADDI
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Reg:X11>
                                        #  <MCOperand Imm:42>>
	jal	2048                            # encoding: [0xef,0x00,0x10,0x00]
                                        # <MCInst #14891 JAL
                                        #  <MCOperand Reg:X1>
                                        #  <MCOperand Imm:2048>>
	lw	a0, 8(sp)                       # encoding: [0x22,0x45]
                                        # <MCInst #14523 C_LWSP
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Reg:X2>
                                        #  <MCOperand Imm:8>>
```

三行输出把三件事钉死了。第一，`addi` 的 opcode 编号是 13727，这是 TableGen 分配的枚举值，不是文本。第二，`jal ra, 2048` 的 MCInst 是 `JAL`，操作数是 `X1` 与 `2048`；打印器只是按别名把 `X1` 省掉了。第三，`lw a0, 8(sp)` 的 MCInst 是 `C_LWSP`，不是 `LW`；汇编器确实做了压缩。打印时 `uncompress` 再把它变回 `LW`，所以文本看起来像一条完整宽度的 `lw`。

这也说明为什么“反汇编再汇编”不保证文本逐字相同：MCInst 只保留 opcode 和操作数，不保留用户原来写的是 `x10` 还是 `a0`，是 `jal ra, 2048` 还是 `jal 2048`。

### 8.10 把 brief 的编码表逐行解释一遍

brief 1.2 的 17 行输出可以按“输入 → 回显 → 字节 → 原因”拆开：

| 输入 | 回显 | 字节 | 原因 |
| --- | --- | --- | --- |
| `addi a0, a1, 42` | `addi a0, a1, 42` | `13 85 a5 02` | 无别名，I 型 |
| `add a0, a1, a2` | `add a0, a1, a2` | `33 85 c5 00` | R 型，不能被压缩 |
| `lw a0, 8(sp)` | `lw a0, 8(sp)` | `22 45` | 压缩成 `C_LWSP`，打印时反压缩 |
| `sw a0, 12(sp)` | `sw a0, 12(sp)` | `2a c6` | 压缩成 `C_SWSP` |
| `ld a0, -8(s0)` | `ld a0, -8(s0)` | `03 35 84 ff` | 偏移超出 `C_LDSP` 范围，保持 32 位 |
| `sd ra, 8(sp)` | `sd ra, 8(sp)` | `06 e4` | 压缩成 `C_SDSP` |
| `beq a0, a1, 16` | `beq a0, a1, 16` | `63 08 b5 00` | B 型，寄存器不是 x0 |
| `bne a0, zero, -8` | `bnez a0, -8` | `65 fd` | `bnez` 别名；压缩成 `C_BNEZ` |
| `jal ra, 2048` | `jal 2048` | `ef 00 10 00` | `jal $offset` 别名省略 `ra` |
| `lui a0, 0x12345` | `lui a0, 74565` | `37 55 34 12` | 十进制打印，值不变 |
| `auipc a0, 0x1` | `auipc a0, 1` | `17 15 00 00` | 十进制打印 |
| `jalr ra, 0(a0)` | `jalr a0` | `02 95` | `jalr $rd, $rs` 别名；压缩成 `C_JALR` |
| `xori a0, a1, -1` | `not a0, a1` | `13 c5 f5 ff` | `not` 别名，优先级 1 |
| `fadd.d fa0, fa1, fa2` | `fadd.d fa0, fa1, fa2` | `53 f5 c5 02` | 需要 D 扩展 |
| `c.addi a0, 1` | `addi a0, a0, 1` | `05 05` | 反压缩成 `ADDI` |
| `c.li a0, -1` | `li a0, -1` | `7d 55` | 反压缩后按 `li` 别名打印 |
| `c.lw a0, 8(a1)` | `lw a0, 8(a1)` | `88 45` | 反压缩成 `LW` |

这张表的用法：执行 `-show-inst` 得到 opcode，执行 `-show-encoding` 得到字节，再看 `.td` 的别名和 `CompressPat`。三者对齐后，就不会把“打印差异”误判成“编码差异”。
## 可跑实验

### 实验 A：别名与压缩的真实回显

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
@'
jal  ra, 2048
jalr ra, 0(a0)
xori a0, a1, -1
mv   a0, a1
li   a0, -1
nop
j    16
c.addi a0, 1
c.lw a0, 8(a1)
'@ | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+m,+a,+f,+d,+c" -show-encoding
```

本机输出（节选，逐字）：

```text
	jal	2048                            # encoding: [0xef,0x00,0x10,0x00]
	jalr	a0                              # encoding: [0x02,0x95]
	not	a0, a1                          # encoding: [0x13,0xc5,0xf5,0xff]
	mv	a0, a1                          # encoding: [0x2e,0x85]
	li	a0, -1                          # encoding: [0x7d,0x55]
	nop                                     # encoding: [0x01,0x00]
	j	16                              # encoding: [0x01,0xa8]
	addi	a0, a0, 1                       # encoding: [0x05,0x05]
	lw	a0, 8(a1)                       # encoding: [0x88,0x45]
```

观察点：`jal ra` 的 `ra` 被别名省略；`jalr ra, 0(a0)` 的 `ra` 与 `0` 都被省略；`xori -1` 变成 `not`；`c.addi` 与 `c.lw` 被反压缩成基指令后打印。输入文本和输出文本不是一一对应的。

### 实验 B：`li` 的多指令展开

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
@'
li a0, 2048
li a0, -1
li a0, 0x123456789abcdef
'@ | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -show-encoding
```

前两行实测为：

```text
	li	a0, 1                           # encoding: [0x05,0x45]
	slli	a0, a0, 11                      # encoding: [0x2e,0x05]
	li	a0, -1                          # encoding: [0x7d,0x55]
```

`2048` 等于 `1` 左移 11 位，所以 `emitLoadImm` 选择“先装 1，再左移 11 位”。`-1` 落在 12 位有符号范围内，一条 `addi` 就够。第三行的 64 位常量会展开成更多条，输出行数随立即数位模式变化；具体条数由 `RISCVMatInt::generateInstSeq` 决定，`llvm-mc` 只是把生成的每条 MCInst 依次打印。

### 实验 C：三类失败

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
foreach ($c in @('add a0, a1, nope','addi a0, a1, 4096','beq a0, a1, 5000','mul a0, a1, a2')) {
  $c | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -show-encoding
  "exit=$LASTEXITCODE"
}
```

本机关键行：

```text
add a0, a1, nope  -> error: invalid instruction, any one of the following would fix this:
addi a0, a1, 4096 -> error: operand must be ... integer in the range [-2048, 2047]
beq a0, a1, 5000  -> error: immediate must be a multiple of 2 bytes in the range [-4096, 4094]
mul a0, a1, a2    -> error: instruction requires the following: 'Zmmul' (Integer Multiplication)
```

四个用例的 `exit=1`。第四例在加 `-mattr=+m` 后成功，编码为 `[0x33,0x85,0xc5,0x02]`。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `AsmParser/RISCVAsmParser.cpp` | `parseInstruction` | 助记符别名在解析前应用，第 0 个操作数是助记符 |
| `AsmParser/RISCVAsmParser.cpp` | `matchAndEmitInstruction` | `Match_Success` / `Match_MnemonicFail` / `Match_NearMisses` 三条分支 |
| `AsmParser/RISCVAsmParser.cpp` | `matchRegisterNameHelper` | `MatchRegisterName` 后接 `MatchRegisterAltName`；RVE 过滤 x16–x31 |
| `AsmParser/RISCVAsmParser.cpp` | `ReportNearMisses` | 多候选错误文本的生成 |
| `AsmParser/RISCVAsmParser.cpp` | `processInstruction` | `PseudoLI` / `PseudoLA` / `PseudoLLA` / `PseudoLGA` 的展开分派 |
| `AsmParser/RISCVAsmParser.cpp` | `emitLoadImm` / `emitAuipcInstPair` | 立即数序列与 PC 相对地址对 |
| `RISCVInstrInfo.td` | `InstAlias<...>` | `nop` / `li` / `mv` / `not` / `j` / `jal` / `ret` 的优先级 |
| `RISCVInstrInfo.td` | `PseudoLI` / `PseudoCALL` / `PseudoTAIL` | 伪指令的大小与 AsmString |
| `MCTargetDesc/RISCVMCCodeEmitter.cpp` | `encodeInstruction` | `PseudoCALL` / `PseudoTAIL` 在编码期展开为 `auipc` + `jalr` |
| 生成的 `RISCVGenAsmMatcher.inc` | `MatchInstructionImpl` / `MatchTable0` | 按助记符排序的匹配表与特征位检查 |

`RISCVGenAsmMatcher.inc` 不在源码树里，需要自己用 `llvm-tblgen -gen-asm-matcher` 生成。生成文件的头部写着 `Assembly Matcher Source Fragment` 和 `From: RISCV.td`，可以据此确认它和当前 `.td` 对应。

## 常见坑

1. **把 `-show-encoding` 当成输入回显。** 它打印的是 MCInst 经 InstPrinter 的结果。`jalr ra, 0(a0)` 回显 `jalr a0`，并不代表汇编器丢了 `ra` 或偏移；编码里两者都在。
2. **以为 `li` 永远是一条指令。** `li a0, 2048` 是两条，`li a0, -1` 是一条，64 位常量更多。写性能敏感的汇编时，用 `-show-encoding` 数行数，不要凭印象。
3. **把 `mul` 的错误当成“没加 M”。** 23.1.4 要求的是 `Zmmul`，加 `+m` 能满足；只加 `+zmmul` 也能满足 `mul`，但 `div` 仍需要 `+m`。
4. **把立即数错误理解成“值太大”。** `addi` 的 `4096` 越界一位；`beq` 的 `5000` 既是范围外又不是 2 的倍数，错误信息会优先说对齐与范围。
5. **忘了 RVE 会拒绝 x16–x31。** 同一份汇编在 RVE 配置下结果不同；`matchRegisterNameHelper` 的 RVE 分支会把这些寄存器变成无效。
6. **在汇编层找 `.riscv.attributes`。** `llvm-mc -filetype=obj` 默认不生成这个段，只有写了 `.attribute` 或显式段指令才会出现；`llc -filetype=obj` 会自动生成。详见第 10 章。
7. **把 `call` 当成 4 字节 `jal`。** 汇编层 `call` 固定 8 字节，带 `R_RISCV_CALL_PLT`；链接器在范围内才把它 relax 成 `jal`。用 `-show-encoding` 看到的 8 字节是正确现象。

## 自我检查

- [ ] 能说出 `parseInstruction` 在解析操作数之前做了什么，以及为什么不能等到 `MatchInstructionImpl` 再应用助记符别名。
- [ ] 能解释 `jal ra, 2048` 为什么回显成 `jal 2048`，并指出是哪条 `InstAlias` 的优先级生效。
- [ ] 能解释 `xori a0, a1, -1` 为什么回显成 `not`，以及 `-M no-aliases` 下会变成什么。
- [ ] 能说明 `li a0, 2048` 的两条编码分别是什么，以及为什么不是一条。
- [ ] 能复述 `Match_Success`、`Match_MnemonicFail`、`Match_NearMisses` 三种返回对应的用户可见行为。
- [ ] 能解释 `addi a0, a1, 4096` 与 `beq a0, a1, 5000` 的错误信息为什么不同。
- [ ] 能用 `llvm-mc -filetype=obj` 加 `llvm-readobj` 复现 `R_RISCV_CALL_PLT` 与 `R_RISCV_PCREL_HI20/LO12_I`。

## 延伸阅读

- LLVM 官方文档：*LLVM MC Layer* 与 *TableGen* 的 `InstAlias` / `MnemonicAlias` 说明，重点看 `EmitPriority` 的语义。
- RISC-V psABI 与 ISA 手册第 20 章“Assembler Pseudo Instructions”，对照 `li`、`la`、`call`、`tail` 的定义。
- 源码：`AsmParser/RISCVAsmParser.cpp` 全文，尤其是 `parseOperand`、`processInstruction`、`parseDirectiveOption`。
- 源码：`RISCVInstrInfo.td` 的别名区块，以及 `RISCVInstrInfoC.td` 的压缩指令定义。
- 测试：`llvm/test/MC/RISCV/`（brief 记录该目录存在），搜索 `show-encoding` 与 `invalid` 用例，和本章实测对照。
- 下一章：[反汇编器与 llvm-mc](./09-disassembler.md)，看同一批字节如何被逆向解码与别名打印。

[上一章：练习 B · 自定义指令](./practice-b-custom-instruction.md) | [下一章：反汇编器与 llvm-mc](./09-disassembler.md)