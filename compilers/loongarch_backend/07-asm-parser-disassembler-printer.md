# Ch 07 — 汇编器、反汇编器与 InstPrinter：文本 ↔ 字节的往返

> 本章目标：搞清楚一条 `move $a0, $a1` 从**文本**走到**字节**、再走回**文本**的每一站；知道 `move`/`nop`/`ret` 其实都不是独立指令；知道 `$r4` 和 `$a0` 为什么都合法；并能用 `llvm-mc` 自己做一次可复现的往返实验。

---

## 7.1 四个角色，别混在一起

| 组件 | 文件 | 方向 | 职责 |
|---|---|---|---|
| `AsmParser` | `AsmParser/LoongArchAsmParser.cpp` | 文本 → `MCInst` | 词法、操作数解析、`%modifier`、伪指令展开、fixup |
| `AsmMatcher`（生成） | `LoongArchGenAsmMatcher.inc` | 操作数向量 → opcode | 由 `.td` 的 `AsmString` 生成匹配表 |
| `InstPrinter`（生成 + 手写） | `MCTargetDesc/LoongArchInstPrinter.cpp` + `GenAsmWriter.inc` | `MCInst` → 文本 | 打印寄存器、立即数、别名 |
| `Disassembler` | `Disassembler/LoongArchDisassembler.cpp` + `GenDisassemblerTables.inc` | 字节 → `MCInst` | 查表解码，再交给 `InstPrinter` |

注意：**汇编和反汇编不是互逆的两份代码**，但共享同一个 `MCInst` 表示和同一张 `InstPrinter`。这就是为什么“汇编进去、反汇编出来”能对上——它们用的是同一套 opcode 和同一个打印器。

---

## 7.2 汇编路径：一行 `.s` 的完整旅程

```text
"move $a0, $a1"
   │  Lexer 切成 Identifier / Dollar / Identifier / Comma ...
   ▼
parseInstruction()            读助记符 "move"
   ▼
parseOperandList()            逐个尝试 parseRegister / parseImmediate
   ▼
MatchInstructionImpl()        ← TableGen 生成的 AsmMatcher
   │  匹配到 `InstAlias<"move $dst, $src", (OR $dst, $src, R0)>`
   ▼
MCInst: OR { $a0, $a1, $zero }
   ▼
processInstruction()          伪指令在这里展开（la/li/call...）
   ▼
MCCodeEmitter                 → 4 字节 0x001500a4
```

对应源码：

- `LoongArchAsmParser::parseInstruction()`（`LoongArchAsmParser.cpp:867`）
- `parseRegister()`（`:727`）、`parseImmediate()`（`:747`）、`parseOperandWithModifier()`（`:776`）
- `matchAndEmitInstruction()`（`:1779`）
- `processInstruction()`（`:1611`）

`matchAndEmitInstruction()` 的错误分类值得记住，因为写测试时常看到这些字符串：

| 匹配结果 | 报错 | 典型原因 |
|---|---|---|
| `Match_MissingFeature` | `instruction requires the following: ...` | 指令存在但 feature 没开（如 `-mattr=-lasx`） |
| `Match_MnemonicFail` | `unrecognized instruction mnemonic` | 拼错，后面还会给拼写建议 |
| `Match_InvalidOperand` | `invalid operand for instruction` | 寄存器类/立即数范围不对 |
| `Match_TooFewOperands` | `too few operands for instruction` | 少写了操作数 |

---

## 7.3 寄存器名：`$` 前缀与“别名”

`LoongArch.td` 里三行关键设置：

```tablegen
let ShouldEmitMatchRegisterAltName = 1;
let AllowDuplicateRegisterNames = 1;
...
string RegisterPrefix = "$";
```

它们的效果：

- `RegisterPrefix = "$"`：汇编语法里寄存器必须带 `$`（`$a0`，不是 `a0`）。
- `ShouldEmitMatchRegisterAltName = 1`：生成匹配表时**同时**接受 ABI 名和数字名。
- `AllowDuplicateRegisterNames = 1`：`$r4` 和 `$a0` 指向同一个物理寄存器，允许两个名字共存。

实测两种写法：

```console
$ llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding
add.d $r4, $r5, $r6
add.d $a0, $a1, $a2
```

```text
	add.d	$a0, $a1, $a2                   # encoding: [0xa4,0x98,0x10,0x00]
	add.d	$a0, $a1, $a2                   # encoding: [0xa4,0x98,0x10,0x00]
```

输入两种名字，输出都被规范化成 ABI 名。打印侧在 `LoongArchInstPrinter::getRegisterName()`：

```cpp
const char *LoongArchInstPrinter::getRegisterName(MCRegister Reg) {
  // Default print reg alias name
  return getRegisterName(Reg, NumericReg ? LoongArch::NoRegAltName
                                         : LoongArch::RegAliasName);
}
```

`-Mreg-names=numeric`（即 `NumericReg=true`）时打印 `$r4`，默认打印 `$a0`。

**FPR 的坑**：FPR 有 `F0`–`F31` 和 `F0_64`–`F31_64` 两套枚举，但汇编器只接受 32 位那套名字。源码里有两处断言：

```cpp
// match always matches the 32-bit variant, and not the 64-bit one.
assert(!(RegNo >= LoongArch::F0_64 && RegNo <= LoongArch::F31_64));
static_assert(LoongArch::F0 < LoongArch::F0_64,
              "FPR matching must be updated");
```

也就是说 `$f0_64` 不是合法汇编写法；64 位寄存器类只在内部（寄存器类约束）使用。

---

## 7.4 操作数修饰符：`%le_hi20_r` 是怎么进 MC 的

LoongArch 的汇编里能看到这种写法：

```asm
lu12i.w $a0, %le_hi20_r(sym)
add.d   $a0, $a0, $tp
addi.d  $a0, $a0, %le_lo12_r(sym)
```

解析入口是 `parseImmediate()` 遇到 `%` 时转给 `parseOperandWithModifier()`：

```cpp
if (getLexer().getKind() != AsmToken::Percent)
  return Error(getLoc(), "expected '%' for operand modifier");
getParser().Lex(); // Eat '%'
...
auto VK = LoongArch::parseSpecifier(Identifier);
if (VK == LoongArchMCExpr::VK_None)
  return Error(getLoc(), "invalid relocation specifier");
```

要点：

- 修饰符不是字符串拼接，而是 `LoongArchMCExpr::VariantKind` 枚举（`VK_LE_HI20_R` 之类）。
- 生成 `MCExpr` 后，`MCCodeEmitter` 根据 `VK` 选 fixup 种类，最终变成 `R_LARCH_*` 重定位。
- 这些 `VK` 与 linker 的 relaxation 判定直接相关（Ch 11/12）。#239812 的 `R_LARCH_RELAX` 就是在这条链上产生的。

---

## 7.5 别名：你写的很多指令都不存在

`LoongArchInstrInfo.td:2644` 起是一串 `InstAlias`：

```tablegen
def : InstAlias<"nop", (ANDI R0, R0, 0)>;
def : InstAlias<"move $dst, $src", (OR GPR:$dst, GPR:$src, R0)>;
def : InstAlias<"ret", (JIRL R0, R1, 0)>;
def : InstAlias<"jr $rj", (JIRL R0, GPR:$rj, 0)>;
def : InstAlias<"bgt $rj, $rd, $imm16", ...>;
def : InstAlias<"bltz $rd, $imm16", ...>;
```

实测：

```console
$ llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding
move $a0, $a1
nop
ret
```

```text
	move	$a0, $a1                        # encoding: [0xa4,0x00,0x15,0x00]
	nop                                     # encoding: [0x00,0x00,0x40,0x03]
	ret                                     # encoding: [0x20,0x00,0x00,0x4c]
```

拆开看：

| 写法 | 真实指令 | 32 位编码 | 拆解 |
|---|---|---|---|
| `move $a0,$a1` | `or $a0,$a1,$zero` | `0x001500a4` | `rd=4, rj=5, rk=0` |
| `nop` | `andi $zero,$zero,0` | `0x03400000` | `rd=0, rj=0, imm=0` |
| `ret` | `jirl $zero,$ra,0` | `0x4c000020` | `rd=0, rj=1, imm=0` |

**打印侧也会用别名**。`printInst()` 先试 `printAliasInstr`：

```cpp
if (!PrintAliases || NoAliases || !printAliasInstr(MI, Address, STI, O))
  printInstruction(MI, Address, STI, O);
```

所以即使你写的是 `or $a0,$a1,$zero`，反汇编/`-show-encoding` 的输出仍是 `move $a0, $a1`。这也是 Ch 06 里“写 MC 测试要对齐打印结果”的原因：`CHECK` 行写 `or` 会失败。

---

## 7.6 反汇编：字节 → opcode → 打印

`LoongArchDisassembler.cpp` 的主入口：

```cpp
Result = decodeInstruction(DecoderTable32, MI, Insn, Address, this, STI);
```

流程：

1. 取 4 字节，按小端拼成 `uint32_t Insn`；
2. 用生成的 `DecoderTable32` 按位域匹配；
3. 每个操作数调用 `Decode*RegisterClass` 把 5 位寄存器号映射成 `MCRegister`；
4. 返回 `MCInst`，由 `InstPrinter` 打印。

`DecodeGPRRegisterClass` / `DecodeFPR32RegisterClass` / `DecodeFPR64RegisterClass` / `DecodeCFRRegisterClass` / `DecodeLSX128RegisterClass` / `DecodeLASX256RegisterClass` 都在文件前 140 行内，结构几乎一样：范围检查 + 查表。

反汇编对**重定位**是盲的：它只看编码里的位，所以 `lu12i.w $a0, 0` 反汇编出来就是立即数 0，不会告诉你“这里有个 `%le_hi20_r`”。要看重定位必须 `llvm-readobj -r`。

---

## 7.7 往返实验（可复现）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cd /tmp

cat > rt.s <<'EOF'
move $a0, $a1
li.w $a0, 1
nop
bnez $a0, .L1
.L1:
  ret
EOF

# 1) 文本 -> 编码
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding rt.s

# 2) 文本 -> 目标文件 -> 反汇编
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -filetype=obj rt.s -o rt.o
$T/bin/llvm-objdump -d rt.o
```

实测 `-show-encoding`：

```text
	move	$a0, $a1                        # encoding: [0xa4,0x00,0x15,0x00]
	ori	$a0, $zero, 1                   # encoding: [0x04,0x04,0x80,0x03]
	nop                                     # encoding: [0x00,0x00,0x40,0x03]
	bnez	$a0, .L1                        # encoding: [0x80'A',A,A,0b010001AA]
                                        #   fixup A - offset: 0, value: .L1, kind: fixup_loongarch_b21
.L1:
	ret                                     # encoding: [0x20,0x00,0x00,0x4c]
```

实测 `llvm-objdump -d`：

```text
0000000000000000 <.text>:
       0: a4 00 15 00  	move	$a0, $a1
       4: 04 04 80 03  	ori	$a0, $zero, 0x1
       8: 00 00 40 03  	nop
       c: 80 04 00 44  	bnez	$a0, 0x4 <.text+0x10>
      10: 20 00 00 4c  	ret
```

两个观察：

1. `li.w $a0, 1` 在 MC 层就被展开成 `ori $a0, $zero, 1`——**没有 `li.w` 的机器码**。展开发生在 `processInstruction()` 的 `PseudoLI_W` 分支 → `emitLoadImm()`（Ch 08 展开）。
2. `bnez` 的编码里带 `fixup_loongarch_b21`，因为目标地址在汇编时未知；`A` 表示由 fixup 填的位。反汇编看到的是**已经填好的** `0x44000480`（相对 `.text+0x10`，偏移 +4）。

再手工反汇编那 3 条无分支指令，验证字节序：

```console
$ printf '0x00 0x00 0x40 0x03\n0xa4 0x00 0x15 0x00\n0x20 0x00 0x00 0x4c\n' \
    | llvm-mc -triple=loongarch64-unknown-linux-gnu -disassemble
```

```text
	nop
	move	$a0, $a1
	ret
```

注意：`-disassemble` 的 token 用 `strtoll` 解析，**必须带 `0x`**。写成 `00 00 40 03` 会按十进制解释成 `0,0,40,3`，拼出 `0x03280000`，反汇编成 `lu52i.d $zero, $zero, -1536`——这是一个真实踩过的坑：

```console
$ echo '00 00 40 03' | llvm-mc -triple=loongarch64-unknown-linux-gnu -disassemble
	lu52i.d	$zero, $zero, -1536
$ echo '0x00 0x00 0x40 0x3' | llvm-mc -triple=loongarch64-unknown-linux-gnu -disassemble
	nop
```

---

## 7.8 反直觉点

1. **`move`/`nop`/`ret`/`jr`/`bgt` 都不是真指令**，是 `InstAlias`。查 `LoongArchInstrInfo.td` 的 `InstAlias` 比查 ISA 手册更快。
2. **`li.w`/`li.d`/`la.*`/`call` 是 MC 层伪指令**，在 `processInstruction()` 里展开，不是选指阶段。所以 `llvm-mc` 单独也能处理 `li.w`，但 `llc` 生成的是已经展开好的序列。
3. **打印优先用别名**。写 lit 测试时期望输出是 `move`，不是 `or`。
4. **`$r4` 和 `$a0` 完全等价**，但 `-Mreg-names=numeric` 才打印数字名。
5. **反汇编不知道重定位**。同一个 4 字节，链接前链接后含义可能完全不同（relaxation 会改写指令）。
6. **`-disassemble` 的输入是十进制，除非写 `0x`**。这是最容易浪费十分钟的坑。
7. **`bnez` 的 `fixup_loongarch_b21` 是 21 位、2 字节对齐的分支**，范围 ±1MB；`b` 是 26 位（`b26`），`bl` 也是。超出范围时汇编器报 `fixup` 越界，而不是自动换成 `pcaddu18i+jirl`——那是 `llc` 的 `PseudoCALL` 展开负责的事。

---

## 7.9 本章证据

| 结论 | 命令/源码 |
|---|---|
| 解析入口与错误分类 | `AsmParser/LoongArchAsmParser.cpp:867,1779,1611` |
| `$` 前缀/别名开关 | `LoongArch.td:232-239` |
| 寄存器别名打印 | `MCTargetDesc/LoongArchInstPrinter.cpp:162` |
| FPR 32/64 断言 | `AsmParser/LoongArchAsmParser.cpp:670-676` |
| `%modifier` 解析 | `LoongArchAsmParser.cpp:776` |
| `nop`/`move`/`ret` 别名 | `LoongArchInstrInfo.td:2644-2649` |
| 别名优先打印 | `LoongArchInstPrinter.cpp:99-104` |
| 解码入口 | `Disassembler/LoongArchDisassembler.cpp:215` |
| 编码/反汇编往返 | 本章 7.7 的 `llvm-mc`/`llvm-objdump` 实测 |
| `-disassemble` 十进制坑 | 本章 7.7 末尾实测 |

---

## 7.10 练习

1. 用 `llvm-mc -show-encoding` 验证 `jr $ra`、`bgt $a0, $a1, .`、`bltz $a0, .` 分别展开成哪条真指令。
2. 找出 `InstAlias` 里所有“条件分支别名”（`bgt`/`ble`/`bgez`…），列出它们对应的真实 opcode。
3. 给 `llvm-mc` 传 `-Mreg-names=numeric`，观察 `add.d $a0,$a1,$a2` 的打印变化。
4. 故意写 `$f0_64` 当操作数，记录报错；再对照 7.3 的两处断言解释为什么。
5. 写一个 4 条指令的 `.s`，只保留 `-show-encoding` 的字节，手工拼成 32 位整数，再用 `-disassemble` 还原，验证你理解了小端。

---

## 7.11 下一步

下一章看常量物化：`li.w 1` / `li.d 0x123456789` 这种“一条伪指令”在 MC 层和选指层分别怎么变成 1–5 条真指令，以及 `LoongArchMatInt::generateInstSeq()` 的贪心策略。
