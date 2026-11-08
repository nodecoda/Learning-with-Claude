# Ch 07 — 汇编器、反汇编器与 InstPrinter：别名与往返

> **目标**：拆开「文本 → `MCInst` → 字节 → `MCInst` → 文本」这条双向通道；能定位一次汇编/反汇编失败落在 `RISCVAsmParser.cpp`、`RISCVDisassembler.cpp` 还是 `RISCVInstPrinter.cpp`；能解释 `nop`/`mv`/`li`/`call` 在 MC 层与 CodeGen 层的不同处理；能用 `llvm-mc` 做可复现的往返实验。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`；构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。所有「实测」均来自该构建；跑不了的标「未实测」。

> **前置**：Ch 04（TableGen 指令模型）、Ch 05（寄存器）、Ch 06（编码与 MC 层）。

---

## 7.1 为什么维护者要读这三条通道
同一份 TableGen 指令记录生成三张表：**汇编器**把 `addi a0,a0,1` 变成 `RISCV::ADDI` 加三个 `MCOperand`；**反汇编器**把 `13 05 10 00` 变回 `ADDI` 与 `a0,a0,1`，并知道它占 4 字节；**`InstPrinter`** 把 `ADDI a0,a0,0` 打印成 `mv a0,a0`。

三个真实缺陷面：

- **汇编器太宽松**。实测 `add a0, a0, 5` 被接受并汇编成 `addi`。根因是 `MatchTable0` 里同一 mnemonic 有多条 `MatchEntry`，按操作数类型挑选。
- **反汇编器受 feature 门控**。`01 00` 加 `+c` 解成 `nop`，不加 `+c` 报 `invalid instruction encoding`。长度只看低两位，表项解码仍要过 feature 检查。
- **打印器会改写文本**。`addi a0,a1,0` 与 `mv a0,a1` 是同一个 `MCInst`，字节相同，但往返后文本变了。diff 测试必须区分「文本等价」与「字节等价」。

| 文件 | 行数 | 职责 |
|---|---:|---|
| `AsmParser/RISCVAsmParser.cpp` | 4,720 | 解析、匹配、伪指令展开 |
| `Disassembler/RISCVDisassembler.cpp` | 770 | 长度分派、解码表选择 |
| `MCTargetDesc/RISCVInstPrinter.cpp` | 405 | `MCInst` → 文本、别名、反压缩 |
| `MCTargetDesc/RISCVMCCodeEmitter.cpp` | 1,003 | `MCInst` → 字节、`call`/`tail` 展开 |

---

## 7.2 先建立直觉：`MCInst` 是唯一的枢纽
```
              RISCVInstrInfo*.td
      ┌────────────┼────────────┐
 -gen-asm-matcher  -gen-disassembler  -gen-asm-writer
      ▼            ▼              ▼
 MatchTable0   DecoderTable    AliasPatterns
      │            │              │
 "addi a0,a0,1"    │              │
      │            │              │
 Lexer/Parser      │              │
      ▼            │              │
 parseInstruction  │              │
      ▼            │              │
 RISCVOperand 列表 │              │
      ▼            │              │
 MatchInstructionImpl ─► MCInst ◄─┘
      │            │  ▲
      ▼            │  │ printAliasInstr
 processInstruction│  │
      ▼            │  │
 emitToStreamer    │  │
      ▼            │  │
 RISCVMCCodeEmitter│  │
      ▼            │  │
    字节 ──────────┘  │
      │               │
      ▼               │
 RISCVDisassembler    │
 getInstruction       │
      ▼               │
 decodeInstruction ─► MCInst
                      │
                      ▼
               RISCVInstPrinter
               printInst → 文本
```

三条推论：`MCInst` 不保存原始文本；长度只在反汇编器手里（`Size` 输出参数）；同一 mnemonic 可以有多条匹配路径（`li` 的 `InstAlias` 与 `PseudoLI`，`add` 的 `ADD` 与 `ADDI`）。

---

## 7.3 汇编器：字符串怎么变成 `MCInst`
### 7.3.1 `parseInstruction`：先应用 mnemonic alias

入口 `RISCVAsmParser.cpp:3186`。第一件事不是解析操作数，而是应用 mnemonic alias：

```cpp
// RISCVAsmParser.cpp:3186
applyMnemonicAliases(Name, AvailableFeatures, 0);
Operands.push_back(RISCVOperand::createToken(Name, NameLoc));
```

注释说明理由：某些 mnemonic 的操作数需要自定义解析器，必须先把 `move` 改成 `mv`，否则 `MatchOperandParserImpl` 拿到的还是旧名字。`MnemonicAlias<"move","mv">` 在 `RISCVInstrInfo.td:1309`，只改名字，不产生新 opcode。

### 7.3.2 操作数不是字符串，是带分类的 `RISCVOperand`
`parseOperand`（`:3159`）按四级顺序尝试：

1. `MatchOperandParserImpl`：CSR 名、`fence` 的 `iorw`、`vtype`、原子指令的 `(a0)`；
2. `parseRegister`（`:1927`）：同时接受 `a0` 与 `x10`，RVE 下拒绝 `x16`–`x31`；
3. `parseExpression`（`:2259`）：整数、符号、重定位 specifier；
4. `parseMemOpBaseReg`（`:2911`）：严格吃掉 `(` reg `)`，否则报 `expected ')'`。

> **纠偏**：任务里提到的 `parseImmediate` 在本树不存在。`grep -rq parseImmediate llvm/lib/Target/RISCV/` 返回 `NOT_FOUND`。立即数走 `parseExpression`，再由 `isSImm12LO()`、`isUImm5()` 等谓词分类（从 `:904` 起）。

原子指令的 `parseZeroOffsetMemOp`（`:2926`）允许并丢弃偏移 `0`，非零偏移报 `optional integer offset must be 0`。CSR 走 `parseCSRSystemRegister`（`:2090`）：名字查表或 12 位整数直接当编码；所以 `csrr a0,0xfff` 合法，`csrr a0,4096` 才越界。

### 7.3.3 `MatchInstructionImpl`：按 mnemonic 二分，再逐条试操作数
`matchAndEmitInstruction`（`:1851`）里的 `MatchInstructionImpl`（`:1860`）是 `RISCVGenAsmMatcher.inc` 生成的：再次应用 mnemonic alias；用 `std::equal_range` 在 `MatchTable0` 中按 mnemonic 找候选；对每条 `MatchEntry` 比较 feature 与最多 8 个 operand class，记录 near-miss，直到完整匹配。

实测 `add a0,a0,5`（省略重复的 `InvalidMatchClass` 行）：

```bash
$ echo 'add a0, a0, 5' | $T/bin/llvm-mc -triple=riscv64 -debug-only=asm-matcher 2>&1
```

```text
AsmMatcher: found 3 encodings with mnemonic 'add'
Trying to match opcode ADD
  Matching formal operand class MCK_GPR against actual operand at index 1 (<reg: a0 (77)>): match success using generic matcher
  Matching formal operand class MCK_GPR against actual operand at index 2 (<reg: a0 (77)>): match success using generic matcher
  Matching formal operand class MCK_GPR against actual operand at index 3 (<imm: 5 rv64>): operand match failed, recording near-miss with diag code 44
  ...
Opcode result: one mismatched operand, adding near-miss
Trying to match opcode ADDI
  Matching formal operand class MCK_GPR against actual operand at index 1 (<reg: a0 (77)>): match success using generic matcher
  Matching formal operand class MCK_GPR against actual operand at index 2 (<reg: a0 (77)>): match success using generic matcher
  Matching formal operand class MCK_SImm12LO against actual operand at index 3 (<imm: 5 rv64>): match success using generic matcher
  ...
Opcode result: complete match, selecting this opcode
	addi	a0, a0, 5
```

匹配成功后：`validateInstruction`（`:4225`）处理跨操作数约束；`processInstruction`（`:4431`）展开 `PseudoLI`/`PseudoLA`/`PseudoLB`，再交给 `emitToStreamer`（`:3732`）。`emitToStreamer` 发指令前调用 `RISCVRVC::compress`，所以 `+c` 压缩发生在汇编器，不在编码器。

---

## 7.4 TableGen 的三张表
`wc -c -l` 实测（均在 `$T/lib/Target/RISCV/`）：

```bash
$ wc -c -l lib/Target/RISCV/RISCVGenAsmMatcher.inc \
         lib/Target/RISCV/RISCVGenDisassemblerTables.inc \
         lib/Target/RISCV/RISCVGenAsmWriter.inc
```

```text
  14019 1110001 lib/Target/RISCV/RISCVGenAsmMatcher.inc
  21280 1507441 lib/Target/RISCV/RISCVGenDisassemblerTables.inc
  56641 1945886 lib/Target/RISCV/RISCVGenAsmWriter.inc
```

| 生成物 | 大小/行数 | 解决的问题 | 关键符号（行号） |
|---|---:|---|---|
| `RISCVGenAsmMatcher.inc` | 1,110,001 B / 14,019 | mnemonic + 操作数 → opcode/转换 | `MatchTable0` 7808、`MatchInstructionImpl` 11367 |
| `RISCVGenDisassemblerTables.inc` | 1,507,441 B / 21,280 | 位域 → opcode + 寄存器/立即数 | `DecoderTable16` 28、`DecoderTable32` 1169 |
| `RISCVGenAsmWriter.inc` | 1,945,886 B / 56,641 | opcode + 操作数 → 文本/别名 | `printInstruction` 53897、`printAliasInstr` 54697 |

`RISCVAsmParser.cpp` 把 matcher include 了三次：`:208` 取声明，`:316` 取 near-miss 诊断码，`:1371` 取匹配实现、寄存器匹配器与拼写检查。

### 7.4.1 定位一个 `MatchEntry`
结构体在 `RISCVGenAsmMatcher.inc:7782`：

```cpp
// RISCVGenAsmMatcher.inc:7782
struct MatchEntry {
  uint16_t Mnemonic;        // MnemonicTable 偏移
  uint32_t Opcode;
  uint16_t ConvertFn;       // RISCVOperand → MCOperand
  uint8_t RequiredFeaturesIdx;
  uint16_t Classes[8];      // 空位 InvalidMatchClass
};
```

`grep -n` 定位关键 entry：

```bash
$ grep -n '239 /\* add \*/\|255 /\* addi \*/\|13691 /\* nop \*/\|11673 /\* li \*/\|13064 /\* mv \*/' \
    lib/Target/RISCV/RISCVGenAsmMatcher.inc
```

```text
7839:  { 239 /* add */, RISCV::ADD, Convert__Reg1_0__Reg1_1__Reg1_2, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_GPR }, },
7840:  { 239 /* add */, RISCV::ADDI, Convert__Reg1_0__Reg1_1__SImm12LO1_2, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_SImm12LO }, },
7841:  { 239 /* add */, RISCV::PseudoAddTPRel, Convert__Reg1_0__Reg1_1__Reg1_2__TPRelAddSymbol1_3, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_GPR, MCK_TPRelAddSymbol }, },
7844:  { 255 /* addi */, RISCV::ADDI, Convert__Reg1_0__Reg1_1__SImm12LO1_2, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_SImm12LO }, },
9143:  { 11673 /* li */, RISCV::ADDI, Convert__Reg1_0__regX0__SImm12LO1_1, AMFBS_None, { MCK_GPR, MCK_SImm12LO }, },
9144:  { 11673 /* li */, RISCV::PseudoLI, Convert__Reg1_0__ImmXLenLI1_1, AMFBS_None, { MCK_GPR, MCK_ImmXLenLI }, },
9323:  { 13064 /* mv */, RISCV::ADDI, Convert__Reg1_0__Reg1_1__imm_95_0, AMFBS_None, { MCK_GPR, MCK_GPR }, },
9383:  { 13691 /* nop */, RISCV::ADDI, Convert__regX0__regX0__imm_95_0, AMFBS_None, {  }, },
```

- `nop`：`regX0__regX0__imm_95_0` 是「固定 x0、固定 x0、立即数 0」，`Classes` 为空，因为没有用户操作数。
- `mv`：`Reg1_0__Reg1_1__imm_95_0` 是「操作数 0 → 目的，操作数 1 → 源，再补 0」。
- `li`：两条，`ADDI`（12 位）与 `PseudoLI`（`ixlenimm_li`）。
- `add`：三条，`ADD`/`ADDI`/`PseudoAddTPRel`；`add a0,a0,5` 落到 `ADDI`。

---

## 7.5 反汇编器：字节怎么变回 `MCInst`
### 7.5.1 先看低两位

`getInstruction`（`RISCVDisassembler.cpp:720`）：

```cpp
// RISCVDisassembler.cpp:720
// It's a 16 bit instruction if bit 0 and 1 are not 0b11.
if ((Bytes[0] & 0b11) != 0b11)
  return getInstruction16(MI, Size, Bytes, Address, CS);
DecodeStatus Result = getInstruction32(MI, Size, Bytes, Address, CS);
if (Result != MCDisassembler::Fail)
  return Result;
```

分派与 feature 无关：低两位不是 `0b11` 就按 16 位走。`+c` 不影响「先试 16 位」，它影响 16 位表项能否解码。

### 7.5.2 解码表按 feature 分组
`DecoderList16`（`:638`）与 `DecoderList32`（`:583`）每项带一组 feature，不满足就跳过。标准 16 位表描述为 `"standard 16-bit instructions"`，包含集为空，所以总会尝试；但表内 predicate 会检查 `Zca`。没有 `+c` 时表能进、entry 解不出来，最终 `Fail`。

`DecodeGPRRegisterClass`（`:132`）把 5 位寄存器编号变成 `MCRegister`：

```cpp
// RISCVDisassembler.cpp:132
constexpr auto DecodeGPRRegisterClass =
    DecodeSimpleRegisterClass<RISCV::X0, 32, /*RVELimit=*/16>;
```

`DecodeSimpleRegisterClass`（`:103`）检查编号范围与 RVE 的 16 寄存器限制，再用 `FirstReg + RegNo`。`NoX0`/`NoX2` 由 `DecodeFilteredRegisterClass` 包谓词实现（`:152`）。

### 7.5.3 实测：解码 `c.mv`
```bash
$ printf '0x2e 0x85\n' | $T/bin/llvm-mc -disassemble -triple=riscv64 -mattr=+c \
    -debug-only=riscv-disassembler
```

```text
Trying standard 16-bit instructions table:
1: OPC_SwitchField(13, 3): 4
246: OPC_SwitchField(0, 2): 2
469: OPC_SwitchField(12, 1): 0
474: OPC_Scope(486)
476: OPC_CheckField(2, 5, 0): FieldValue = 11, ExpectedValue = 0: FAIL, continuing at 486
486: OPC_CheckPredicate(0): PASS
488: OPC_Decode: opcode 14582, using decoder 23: PASS, decoding complete
	mv	a0, a1
```

`opcode 14582` 是 `C_MV` 内部编号，`decoder 23` 是生成的寄存器/操作数解码函数。文本 `mv` 是打印阶段做的别名。

### 7.5.4 `+c` 与否
```bash
$ printf '0x01 0x00\n' | $T/bin/llvm-mc -disassemble -triple=riscv64 -mattr=+c
$ printf '0x01 0x00\n' | $T/bin/llvm-mc -disassemble -triple=riscv64
```

```text
	nop
<stdin>:1:1: warning: invalid instruction encoding
0x01 0x00
^
```

有 `+c` 时解成 `c.nop`，再经别名打印成 `nop`；没有 `+c` 时 warning + 原样输出。`llvm-mc -disassemble` 对坏字节默认不是 hard error。

---

## 7.6 `InstPrinter`：`MCInst` 怎么变回文本
### 7.6.1 `printInst` 的三步

`RISCVInstPrinter.cpp:67`：

```cpp
// RISCVInstPrinter.cpp:67
if (PrintAliases && !CLOpts.no_aliases)
  Res = RISCVRVC::uncompress(UncompressedMI, *MI, STI);
if (Res)
  NewMI = &UncompressedMI;
if (!PrintAliases || CLOpts.no_aliases ||
    !printAliasInstr(NewMI, Address, STI, O))
  printInstruction(NewMI, Address, STI, O);
```

1. **先反压缩**：别名开着时，`C_MV` 先展开成 `ADDI`，后面才能匹配 `mv` 这种基于 `ADDI` 的别名。
2. **再试别名**：`printAliasInstr` 在 `AliasPatterns` 里找模式。
3. **最后 canonical**：`printInstruction` 走 opcode switch。

所以在 `+c` 下，`-M no-aliases` 不是「只关别名」，它同时关掉反压缩：`nop` 变 `c.nop`，`ret` 变 `c.jr ra`。

```bash
$ printf '0x01 0x00\n' | $T/bin/llvm-mc -disassemble -triple=riscv64 -mattr=+c -M no-aliases
```

```text
	c.nop
```

只关别名同时去掉压缩（`-M no-aliases -mattr=-c`）时，`nop` 才会变成 `addi zero, zero, 0`。

### 7.6.2 别名模式表与权重
`printAliasInstr` 在 `RISCVGenAsmWriter.inc:54697`，先用 `OpToPatterns` 按 opcode 找模式区间（`ADDI` 起始索引 4、共 4 条）。`ADDI` 的 4 条模式在 `:54834`，`nop`/`mv` 分别对应索引 4 与 7；`AsmStrings` 在 `:56278`，`/* 31 */ "nop\0"`、`/* 60 */ "mv $\x01, $\x02\0"`。`$` 后是操作数编号，由 `printOperand`（`:87`）替换。

`InstAlias` 的第四个参数是 weight；`0` 表示「汇编器接受，但打印器永不选它」。`bgt` 等靠这个保持 canonical 输出（`RISCVInstrInfo.td:1175` 的注释）。`ret` 的 weight 是 4（`:1217`），`jr $rs` 是 3，所以同时能匹配时输出 `ret`。

`-M numeric` 只换寄存器名，不换 mnemonic：同一条 `13 85 05 00` 会打印成 `mv x10, x11`。

---

## 7.7 别名与伪指令：三种机制，十条指令
| 机制 | TableGen 写法 | 新 opcode | 生效点 |
|---|---|---|---|
| `MnemonicAlias` | `MnemonicAlias<"move","mv">` | 否 | `parseInstruction` 开头 |
| `InstAlias` | `InstAlias<"mv $rd,$rs",(ADDI ...)>` | 否 | 汇编匹配 / 打印匹配 |
| `Pseudo` | `def PseudoCALL : Pseudo<...>` | 是 | `processInstruction` 或编码器/打印器 |

`InstAlias` 只是同一个 `MCInst` 的另一种写法；`Pseudo` 有自己的 opcode，必须有人展开。

| 文本 | TableGen 位置 | MC 层（`llvm-mc`） | CodeGen 层（`llc`） |
|---|---|---|---|
| `nop` | `RISCVInstrInfo.td:1158` | `InstAlias` → `ADDI x0,x0,0` | 无专用 opcode；需要时发 `ADDI`，打印变 `nop` |
| `mv` | `:1160` | `InstAlias` → `ADDI rd,rs,0` | 复制通常由 `ADDI rd,rs,0` 承担，`.s` 打印成 `mv` |
| `li` | `:1139` / `:1159` | 12 位走 `InstAlias`；更宽走 `PseudoLI` | `PseudoLI` 注明「只在 AsmParser 用」；CodeGen 用 `RISCVMatInt` 发 `lui/addi` |
| `not` | `:1162` | `InstAlias` → `XORI rd,rs,-1` | 选 `XORI`，打印成 `not` |
| `neg` | `:1163` | `InstAlias` → `SUB rd,x0,rs` | 选 `SUB`，打印成 `neg` |
| `seqz` | `:1170` | `InstAlias` → `SLTIU rd,rs,1` | 选 `SLTIU`，打印成 `seqz` |
| `j` | `:1206` | `InstAlias` → `JAL x0,offset` | MCInst 是 `JAL x0`；`.s` 是否出现 `j` 看打印器 |
| `ret` | `:1217` | `InstAlias` → `JALR x0,x1,0` | `PseudoRET`（`:1949`）→ `JALR` |
| `call` | `:1899` | `PseudoCALL`，编码器展开 `auipc+jalr` | `.s` 保留 `call`，`.o` 展开 |
| `tail` | `:1963` | `PseudoTAIL`，同样在编码器展开 | `.s` 保留 `tail`，`.o` 展开 |

### 7.7.1 `li`：同名两条路
`li a0,5` 命中 `InstAlias`（`:1159`），直接变 `ADDI`；`li a0,0x12345678` 超出 12 位，命中 `PseudoLI`，`processInstruction` 调 `emitLoadImm`（`:3743`），后者用 `RISCVMatInt::generateMCInstSeq` 生成序列。实测：

```bash
$ echo 'li a0, 5' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
$ echo 'li a0, 2048' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
$ echo 'li a0, 0x12345678' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
```

```text
	li	a0, 5                           # encoding: [0x13,0x05,0x50,0x00]
	li	a0, 1                           # encoding: [0x13,0x05,0x10,0x00]
	slli	a0, a0, 11                      # encoding: [0x13,0x15,0xb5,0x00]
	lui	a0, 74565                       # encoding: [0x37,0x55,0x34,0x12]
	addi	a0, a0, 1656                    # encoding: [0x13,0x05,0x85,0x67]
```

一行源码可能对应多行输出。`PseudoLI` 的注释（`:1135`）明确说它「currently only used in the AsmParser」。

### 7.7.2 `call` / `tail`：`.s` 保留，`.o` 展开
`PseudoCALL`/`PseudoTAIL` 的 `AsmString` 就是 `"call"`/`"tail"`，所以 `.s` 原样保留。编码阶段由 `expandFunctionCall`（`:169`）展开成 `AUIPC+JALR`，并挂 `R_RISCV_CALL_PLT`。实测：

```bash
$ $T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c /tmp/ch07_li.ll -o - | sed -n '/callfoo:/,/End function/p'
$ $T/bin/llvm-objdump -dr --mattr=+m,+a,+c /tmp/ch07_li.o | sed -n '/callfoo/,/^$/p'
```

```text
	call	foo
	...
	ret
```

```text
      18: 00000097     	auipc	ra, 0x0
		0000000000000018:  R_RISCV_CALL_PLT	foo
      1c: 000080e7     	jalr	ra <callfoo+0x4>
```

`tail foo` 用 `x6`（`t1`）作临时寄存器：

```text
      26: 00000317     	auipc	t1, 0x0
		0000000000000026:  R_RISCV_CALL_PLT	foo
      2a: 00030067     	jr	t1 <tailfoo>
```

`jr t1` 又是 `JALR x0,t1,0` 的别名。一条 `tail` 在文本里 1 行，在 `.o` 里是 2 条指令 + 1 个重定位。

### 7.7.3 `not` / `neg` / `seqz`：CodeGen 只选基指令
CodeGen 里没有这三条专用 opcode：`xor -1` 选 `XORI`，`sub 0` 选 `SUB`，`icmp eq`+`zext` 选 `SLTIU`；打印器再改写成别名。实测：

```bash
$ $T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -O2 /tmp/ch07_alias.ll -o - | grep -E '^\s+(not|neg|seqz)'
$ $T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c -O2 -riscv-no-aliases /tmp/ch07_alias.ll -o - | grep -E '^\s+(xori|sub|sltiu)'
```

```text
	not	a0, a0
	neg	a0, a0
	seqz	a0, a0
```

```text
	xori	a0, a0, -1
	sub	a0, zero, a0
	sltiu	a0, a0, 1
```

这是「MC 层与 CodeGen 层不同处理」的最直接证据：CodeGen 选基指令，别名只发生在打印阶段。

---

## 7.8 源码落点
- **`RISCVAsmParser.cpp`**（4,720 行）：`:1851` `matchAndEmitInstruction`、`:1860` `MatchInstructionImpl`、`:1927` `parseRegister`、`:2090` `parseCSRSystemRegister`、`:2259` `parseExpression`、`:2911` `parseMemOpBaseReg`、`:2926` `parseZeroOffsetMemOp`、`:3159` `parseOperand`、`:3186` `parseInstruction`、`:3732` `emitToStreamer`、`:3743` `emitLoadImm`、`:4225` `validateInstruction`、`:4431` `processInstruction`、`:4499` `PseudoLI`。
- **`RISCVDisassembler.cpp`**（770 行）：`:103` `DecodeSimpleRegisterClass`、`:132` `DecodeGPRRegisterClass`、`:152` `DecodeGPRNoX0` 等、`:583` `DecoderList32`、`:638` `DecoderList16`、`:610/:660` `getInstruction32/16`、`:720` `getInstruction`。
- **`RISCVInstPrinter.cpp`**（405 行）：`:49` `applyTargetSpecificCLOption`、`:67` `printInst`、`:83` `printRegName`、`:87` `printOperand`。
- **`RISCVMCCodeEmitter.cpp`**（1,003 行）：`:169–228` `expandFunctionCall`、`:578–583` 伪指令编码入口。
- **`RISCVInstrInfo.td`**（2,553 行）：`:1135–1141` `PseudoLI`、`:1158–1173` `nop`/`li`/`mv`/`not`/`neg`/`seqz`、`:1206–1222` `j`/`jal`/`jr`/`jalr`/`ret` 与权重、`:1309` `move → mv`、`:1888–1904` `PseudoCALL`、`:1949` `PseudoRET`、`:1963–1980` `PseudoTAIL`。
- **生成物**（`$T/lib/Target/RISCV/`）：`RISCVGenAsmMatcher.inc` 的 `MatchEntry` 7782、`MatchTable0` 7808、`MatchInstructionImpl` 11367；`RISCVGenDisassemblerTables.inc` 的 `DecoderTable16` 28、`DecoderTable32` 1169；`RISCVGenAsmWriter.inc` 的 `printInstruction` 53897、`printAliasInstr` 54697、`Patterns` 54834、`AsmStrings` 56278；`RISCVGenMCPseudoLowering.inc`（13,050 B）的 `PseudoRET` 142。

---

## 7.9 可跑实验
```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
```

### 实验 1：匹配器与解码器 trace
```bash
echo 'add a0, a0, 5' | $T/bin/llvm-mc -triple=riscv64 -debug-only=asm-matcher 2>&1 | head -40
printf '0x2e 0x85\n' | $T/bin/llvm-mc -disassemble -triple=riscv64 -mattr=+c -debug-only=riscv-disassembler
```

完整输出见 7.3.3 与 7.5.3。关键观察：`found 3 encodings with mnemonic 'add'`，先试 `ADD` 再试 `ADDI`；`OPC_Decode: opcode 14582, using decoder 23` 对应 `c.mv`。

### 实验 2：往返 A（文本与字节都不变）与 B（文本变、字节不变）
```bash
echo 'nop' | $T/bin/llvm-mc -triple=riscv64 -mattr=+c -show-encoding
printf '0x01 0x00\n' | $T/bin/llvm-mc -disassemble -triple=riscv64 -mattr=+c
echo 'addi a0, a1, 0' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
printf '0x13 0x85 0x05 0x00\n' | $T/bin/llvm-mc -disassemble -triple=riscv64
echo 'mv a0, a1' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
```

```text
	nop                                     # encoding: [0x01,0x00]
	nop
	mv	a0, a1                          # encoding: [0x13,0x85,0x05,0x00]
	mv	a0, a1
	mv	a0, a1                          # encoding: [0x13,0x85,0x05,0x00]
```

A 段文本与字节都不变；B 段输入 `addi`、输出 `mv`，字节始终 `13 85 05 00`。这类差异合法，diff 测试必须容忍。

### 实验 3：往返 C（字节会变的坑）
```bash
echo 'addi a0, a0, 1' | $T/bin/llvm-mc -triple=riscv64 -mattr=+c -show-encoding
printf '0x05 0x05\n' | $T/bin/llvm-mc -disassemble -triple=riscv64 -mattr=+c
echo 'li a0, 1' | $T/bin/llvm-mc -triple=riscv64 -mattr=+c -show-encoding
```

```text
	addi	a0, a0, 1                       # encoding: [0x05,0x05]
	addi	a0, a0, 1
	li	a0, 1                           # encoding: [0x05,0x45]
```

`c.addi` 与 `c.li` 的立即数字段布局不同，所以字节不同。往返必须用同一条汇编文本重放。

### 实验 4：`llvm-objdump` 的别名开关
```bash
printf 'nop\nmv a0,a1\nli a0,5\nret\n' > /tmp/ch07_rt.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+m,+a,+c -filetype=obj -o /tmp/ch07_rt.o /tmp/ch07_rt.s
$T/bin/llvm-objdump -d --mattr=+m,+a,+c /tmp/ch07_rt.o
$T/bin/llvm-objdump -d --mattr=+m,+a,+c -M no-aliases /tmp/ch07_rt.o
```

```text
       0: 0001         	nop
       2: 852e         	mv	a0, a1
       4: 4515         	li	a0, 0x5
       6: 8082         	ret
```

```text
       0: 0001         	c.nop
       2: 852e         	c.mv	a0, a1
       4: 4515         	c.li	a0, 0x5
       6: 8082         	c.jr	ra
```

### 实验 5：错误路径原文
```bash
printf 'addi a0, a0, 4096\n' | $T/bin/llvm-mc -triple=riscv64
printf 'csrr a0, fflagz\n' | $T/bin/llvm-mc -triple=riscv64
printf 'li a0, 0x100000000\n' | $T/bin/llvm-mc -triple=riscv32
```

```text
<stdin>:1:14: error: operand must be a symbol with %lo/%pcrel_lo/%tprel_lo specifier or an integer in the range [-2048, 2047]
addi a0, a0, 4096
             ^
<stdin>:1:10: error: operand must be a valid system register name or an integer in the range [0, 4095]
csrr a0, fflagz
         ^
<stdin>:1:1: error: invalid instruction, any one of the following would fix this:
li a0, 0x100000000
^
<stdin>:1:8: note: operand must be a symbol with %lo/%pcrel_lo/%tprel_lo specifier or an integer in the range [-2048, 2047]
li a0, 0x100000000
       ^
<stdin>:1:8: note: immediate must be an integer in the range [-2147483648, 4294967295]
li a0, 0x100000000
       ^
```

来源：`addi` 越界来自 `isSImm12LO()` 的谓词诊断；`csrr` 非法名来自 `parseCSRSystemRegister`；`li` 的 RV32 越界是 near-miss 聚合。

---

## 7.10 踩雷集锦
- **`add a0,a0,5` 合法**，会变成 `addi`；不要把它当负向测试输入。
- **`-M no-aliases` 同时关反压缩**，得到 `c.nop`/`c.mv`，不是 `addi x0,x0,0`。要看到 `addi zero,zero,0`，跑 `-M no-aliases -mattr=-c`。
- **`li` 不是一条指令**：`li a0,2048` 展开成两条，`-show-encoding` 输出两行；FileCheck 的 `CHECK` 数量要按展开后写。
- **`'A'` 不是字节**：`call foo` 的 `[0x97'A',A,A,A,...]` 里 `'A'` 是 fixup 占位，真正字节由汇编后端填。
- **反汇编坏字节是 warning**：`llvm-mc -disassemble` 默认继续，退出码不一定非零；lit 测试要用 `not` 或显式检查 stderr。
- **`csrr a0,0xfff` 合法**：CSR 字段 12 位，4095 可以没有名字；`4096` 才越界。
- **`ret` 没有独立编码**：`+c` 下是 `c.jr ra`；`-M no-aliases` 会打回原形。
- **`-riscv-no-aliases` 也影响 RVC**：`llc` 输出 `c.jr ra`、`c.addi sp,-16`。
- **零权重别名只认不印**：`bgt`/`ble`/`sgt` 能汇编，但打印器永远输出 canonical 形式。
- **`PseudoLI` 不在 CodeGen 出现**：MIR 或 `.s` 里找不到它；CodeGen 用 `RISCVMatInt` 直接物化常量。

---

## 7.11 本章重点整理
- 三方分工：`RISCVAsmParser.cpp`（文本 → `MCInst`）、`RISCVDisassembler.cpp`（字节 → `MCInst`）、`RISCVInstPrinter.cpp`（`MCInst` → 文本）。
- `MatchInstructionImpl` 是生成的：按 mnemonic 二分，再逐条 `MatchEntry` 比 operand class；同一 mnemonic 可有多条 entry。
- `MatchEntry` 的 `Mnemonic` 是 `MnemonicTable` 偏移，`Classes[8]` 是操作数类，`ConvertFn` 负责构造 `MCOperand`。
- 三张表各管一段：AsmMatcher 管名字+形状，Disassembler 管位域+形状，AsmWriter 管 opcode+操作数 → 文本/别名。
- 反汇编长度只看低两位；`+c` 只影响 16 位表项能否解码。
- `printInst` 顺序是「反压缩 → 别名 → canonical」；`-M no-aliases` 同时关掉前两步。
- 三种机制：`MnemonicAlias` 只改名字；`InstAlias` 不产生新 opcode；`Pseudo` 有自己的 opcode，必须有人展开。
- `li` 两条路：12 位走 `InstAlias`，更宽走 `PseudoLI`；CodeGen 不用 `PseudoLI`。
- `call`/`tail` 在 `.s` 保留，在 `.o` 展开成 `AUIPC+JALR` + `R_RISCV_CALL_PLT`。
- `not`/`neg`/`seqz` 在 CodeGen 里是 `XORI`/`SUB`/`SLTIU`，别名只发生在打印阶段。
- 往返要区分「文本等价」与「字节等价」；`addi a0,a1,0` ↔ `mv a0,a1` 是文本变、字节不变的标准例子。

---

## 7.12 自我检核
- [ ] 能画出「文本 → 匹配表 → `MCInst` → 编码 → 字节 → 解码表 → `MCInst` → 打印」的完整图
- [ ] 能说出 `parseInstruction` 为什么先调用 `applyMnemonicAliases`
- [ ] 能在 `RISCVGenAsmMatcher.inc` 里定位 `nop`/`mv`/`li` 的 `MatchEntry` 并读懂 `ConvertFn`
- [ ] 能解释 `add a0,a0,5` 为什么落到 `ADDI`
- [ ] 能说出 `getInstruction` 如何决定 16 位还是 32 位，以及 `+c` 在哪一步生效
- [ ] 能说出 `li` 的两条路径与 `call`/`tail` 的展开位置
- [ ] 能独立跑出「文本变、字节不变」和「字节变」两个往返例子

---

## 7.13 延伸阅读
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp`**：读 1851–1876、3159–3220、4431–4517。所有「汇编器为什么接受/拒绝这段文本」的问题都在这三块。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/Disassembler/RISCVDisassembler.cpp`**：读 103–160、583–688、720–770。反汇编错位、长度错、feature 门控的第一现场。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVInstPrinter.cpp`**：读 49–110。别名、`-M no-aliases`、`-M numeric` 的全部行为都在这里分叉。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfo.td`**：读 1135–1230、1888–1980。新增别名或伪指令时，照抄这里的形状最稳。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp`**：读 169–228、560–600。`call`/`tail` 的字节与重定位在这里生成。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/rv32i-aliases-valid.s`**：读文件头的四个 RUN 行与 `CHECK-INST`/`CHECK-ALIAS`/`CHECK-EXPAND`/`CHECK-ASM-NOALIAS`。这是「同一条源码在四种视图下的期望输出」的标准模板。

---

→ 下一章：[Ch 08 — 常量与地址物化](./08-constant-materialization.md)
