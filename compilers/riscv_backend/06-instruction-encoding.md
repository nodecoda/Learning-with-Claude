# Ch 06 指令编码：从 4 字节到字段

> 目标：把 `addi a0, a1, 42` 这样的文本，一路拆到 opcode、rd、funct3、rs1、imm 的位；再看 RVC 为什么让一条 `lw` 变成 2 字节；最后分清 MC 编码器与 AsmPrinter 各自负责什么。读完本章，你能拿任意一条 RISC-V 指令的字节，按格式表拆出字段，并能解释 `-show-encoding` 里的压缩和别名现象。

> 环境：LLVM 23.1.4 预编译版，Windows x86_64。编码实验全部用 `llvm-mc` 和 `llvm-objdump`，不依赖重新构建 llc。本章的 32 位字节来自 brief 1.2 并与本机重跑一致；RVC 边界和报错文本来自本机 2026-10-25 的重跑。

## 为什么

指令编码是编译器和硬件之间的最后一层契约。前面所有的 DAG、MIR、寄存器分配、调度，最后都要落到一串字节。字节错了，反汇编可能仍然“看起来像”一条指令，但字段位置已经偏了；这类错误不会在 `.td` 解析阶段暴露，也不会在 `llc` 的文本汇编里暴露，只有编码或上板才暴露。

RISC-V 的编码有几个容易踩的点。

第一，立即数不是连续存放的。I 型是连续的 12 位，S 型拆成两段，B 型和 J 型拆得更散。你不能把汇编里的立即数直接按小端写进 4 字节；要先按格式把每一位放到指定位置。

第二，RVC 会改变指令长度。`lw a0, 8(sp)` 在 `+c` 下只输出 2 字节，但它的具体形式是 `c.lwsp`，不是 `c.lw`。`c.lw` 要求基址在 `x8` 到 `x15`，`sp` 不在这个范围。把两者混为一谈，字段就会对不上。

第三，别名化会让回显的 mnemonic 和实际 opcode 不一致。`xori a0, a1, -1` 回显成 `not a0, a1`；`bne a0, zero, -8` 回显成 `bnez`；`jal ra, 2048` 回显成 `jal 2048`。这些是汇编打印器的别名，不是编码器把 opcode 改了。

第四，越界立即数的报错是分层的。`addi` 的 12 位有符号范围是 `[-2048, 2047]`；分支是 13 位、2 字节对齐；`jal` 是 21 位、2 字节对齐。不同指令的报错文本不同，读懂报错能快速定位是格式问题还是范围问题。

本章先把六种 32 位格式的字段表列出来，再用真实字节逐条拆；然后讲 RVC 的 16 位格式和压缩条件；最后讲 MC 编码器与 AsmPrinter 的分工。

## 主线

### 6.1 六种 32 位格式：字段在哪里

RISC-V 的 32 位指令按字段布局分成 R/I/S/B/U/J 六种。所有格式都有 7 位 opcode 在最低位 `[6:0]`，但其余字段位置不同。

| 格式 | 31..25 | 24..20 | 19..15 | 14..12 | 11..7 | 6..0 |
| --- | --- | --- | --- | --- | --- | --- |
| R | funct7 | rs2 | rs1 | funct3 | rd | opcode |
| I | imm[11:5] | imm[4:0] | rs1 | funct3 | rd | opcode |
| S | imm[11:5] | rs2 | rs1 | funct3 | imm[4:0] | opcode |
| B | imm[12] + imm[10:5] | rs2 | rs1 | funct3 | imm[4:1] + imm[11] | opcode |
| U | imm[31:12] | | | | rd | opcode |
| J | imm[20] + imm[10:1] + imm[11] + imm[19:12] | | | | rd | opcode |

更精确的位段：

- **R 型**：`funct7[31:25]`、`rs2[24:20]`、`rs1[19:15]`、`funct3[14:12]`、`rd[11:7]`、`opcode[6:0]`。
- **I 型**：`imm[11:0][31:20]`、`rs1[19:15]`、`funct3[14:12]`、`rd[11:7]`、`opcode[6:0]`。
- **S 型**：`imm[11:5][31:25]`、`rs2[24:20]`、`rs1[19:15]`、`funct3[14:12]`、`imm[4:0][11:7]`、`opcode[6:0]`。
- **B 型**：`imm[12][31]`、`imm[10:5][30:25]`、`rs2[24:20]`、`rs1[19:15]`、`funct3[14:12]`、`imm[4:1][11:8]`、`imm[11][7]`、`opcode[6:0]`。
- **U 型**：`imm[31:12][31:12]`、`rd[11:7]`、`opcode[6:0]`。
- **J 型**：`imm[20][31]`、`imm[10:1][30:21]`、`imm[11][20]`、`imm[19:12][19:12]`、`rd[11:7]`、`opcode[6:0]`。

B 型和 J 型的立即数最低位恒为 0，所以编码里不存 `imm[0]`。解码时把拼出来的值当作有符号数，再左移或直接使用。分支范围 `[-4096, 4094]`，`jal` 范围 `[-1048576, 1048574]`，都是 2 字节对齐。

这些位段在 `.td` 里就是 `Inst{...}` 赋值。`RISCVInstrFormats.td` 第 387 行的 `RVInstR`：

```tablegen
class RVInstR<bits<7> funct7, bits<3> funct3, RISCVOpcode opcode, ...>
    : RVInstRBase<funct3, opcode, outs, ins, opcodestr, argstr> {
  let Inst{31-25} = funct7;
}
```

`RVInstRBase` 负责 `rs2`、`rs1`、`funct3`、`rd`、`opcode`；`RVInstR` 只补 `funct7`。`RVInstI` 在第 467 行补 `imm12`，`RVInstS` 在第 500 行拆 `imm`，`RVInstB` 在第 515 行拆分支偏移，`RVInstU` 在第 548 行放 20 位高位立即数，`RVInstJ` 在第 559 行拆 `jal` 偏移。生成器把这些位段变成 `getBinaryCodeForInstr` 里的移位和掩码。

### 6.2 真实 4 字节：逐条拆开

以下是 brief 1.2 的真实输出，命令是：

```powershell
llvm-mc -triple=riscv64 -mattr=+m,+a,+f,+d,+c -show-encoding
```

| 指令 | 字节（内存顺序） | 32 位字（小端） | 关键字段 |
| --- | --- | --- | --- |
| `addi a0, a1, 42` | `[0x13,0x85,0xa5,0x02]` | `0x02A58513` | opcode `0x13`，rd 10，funct3 0，rs1 11，imm 42 |
| `add a0, a1, a2` | `[0x33,0x85,0xc5,0x00]` | `0x00C58533` | opcode `0x33`，rd 10，funct3 0，rs1 11，rs2 12，funct7 0 |
| `sub a0, a1, a2` | `[0x33,0x85,0xc5,0x40]` | `0x40C58533` | 同 `add`，funct7 `0x20` |
| `ld a0, -8(s0)` | `[0x03,0x35,0x84,0xff]` | `0xFF843503` | opcode `0x03`，rd 10，funct3 3，rs1 8，imm -8 |
| `sd ra, 2040(sp)` | `[0x23,0x3c,0x11,0x7e]` | `0x7E113C23` | opcode `0x23`，funct3 3，rs1 2，rs2 1，imm 2040 |
| `beq a0, a1, 16` | `[0x63,0x08,0xb5,0x00]` | `0x00B50863` | opcode `0x63`，funct3 0，rs1 10，rs2 11，imm 16 |
| `jal ra, 2048` | `[0xef,0x00,0x10,0x00]` | `0x001000EF` | opcode `0x6F`，rd 1，imm 2048 |
| `lui a0, 0x12345` | `[0x37,0x55,0x34,0x12]` | `0x12345537` | opcode `0x37`，rd 10，imm[31:12] `0x12345` |
| `auipc a0, 0x1` | `[0x17,0x15,0x00,0x00]` | `0x00001517` | opcode `0x17`，rd 10，imm[31:12] 1 |
| `fadd.d fa0, fa1, fa2` | `[0x53,0xf5,0xc5,0x02]` | `0x02C5F553` | opcode `0x53`，rd 10，funct3 7，rs1 11，rs2 12，funct7 1 |

`sd ra, 8(sp)` 在 brief 里是 `[0x06,0xe4]`，被压缩成 `c.sdsp`。为了展示 S 型，这里用 `sd ra, 2040(sp)`，它超出 RVC 的栈偏移范围，保持 4 字节。`sw a0, 2044(sp)` 得到 `[0x23,0x2e,0xa1,0x7e]`，字是 `0x7EA12E23`，也是 S 型。

先看 `add` 和 `sub` 的区别。两个字：

```text
add : 0000 0000 1100 0101 1000 0101 0011 0011
sub : 0100 0000 1100 0101 1000 0101 0011 0011
```

只有 bit 30 不同。`funct7` 从 `0000000` 变成 `0100000`，也就是 `0x20`。`rs2`、`rs1`、`funct3`、`rd`、`opcode` 完全一样。这就是 R 型的好处：同一组操作数，只靠 `funct7` 和 `funct3` 区分 `add`/`sub`/`mul`/`div` 等。

`addi` 的字 `0x02A58513`：

```text
0000 0010 1010 0101 1000 0101 0001 0011
```

拆开：

- `[6:0]` = `0010011` = `0x13`，`OP-IMM`。
- `[11:7]` = `01010` = 10，`a0`。
- `[14:12]` = `000`，`funct3`。
- `[19:15]` = `01011` = 11，`a1`。
- `[31:20]` = `000000101010` = 42。

`ld a0, -8(s0)` 的字 `0xFF843503`：

```text
1111 1111 1000 0100 0011 0101 0000 0011
```

- opcode `[6:0]` = `0000011` = `0x03`，`LOAD`。
- rd `[11:7]` = `01010` = 10，`a0`。
- funct3 `[14:12]` = `011` = 3，64 位 `ld`。
- rs1 `[19:15]` = `01000` = 8，`s0`。
- imm `[31:20]` = `111111111000` = `-8`（12 位有符号）。

`sd ra, 2040(sp)` 的字 `0x7E113C23` 是 S 型：

```text
0111 1110 0001 0001 0011 1100 0010 0011
```

- opcode `[6:0]` = `0100011` = `0x23`，`STORE`。
- `imm[4:0]` = `[11:7]` = `11000` = 24。
- funct3 `[14:12]` = `011` = 3，64 位 `sd`。
- rs1 `[19:15]` = `00010` = 2，`sp`。
- rs2 `[24:20]` = `00001` = 1，`ra`。
- `imm[11:5]` = `[31:25]` = `0111111` = 63。
- 拼起来：`63 << 5 | 24` = `2016 + 24` = `2040`。

`beq a0, a1, 16` 的字 `0x00B50863` 是 B 型：

```text
0000 0000 1011 0101 0000 1000 0110 0011
```

- opcode `[6:0]` = `1100011` = `0x63`，`BRANCH`。
- `imm[11]` = bit 7 = 0。
- `imm[4:1]` = `[11:8]` = `1000` = 8，所以 `imm[4:1]` 是 8，即 bit4=1。
- funct3 `[14:12]` = `000`，`beq`。
- rs1 `[19:15]` = `01010` = 10，`a0`。
- rs2 `[24:20]` = `01011` = 11，`a1`。
- `imm[10:5]` = `[30:25]` = `000000`。
- `imm[12]` = bit 31 = 0。
- 拼起来是 16。

`jal ra, 2048` 的字 `0x001000EF` 是 J 型：

```text
0000 0000 0001 0000 0000 0000 1110 1111
```

- opcode `[6:0]` = `1101111` = `0x6F`，`JAL`。
- rd `[11:7]` = `00001` = 1，`ra`。
- `imm[19:12]` = `[19:12]` = `00000000`。
- `imm[11]` = bit 20 = 1。
- `imm[10:1]` = `[30:21]` = `0000000000`。
- `imm[20]` = bit 31 = 0。
- 拼起来是 `1 << 11` = 2048。

`lui a0, 0x12345` 的字 `0x12345537` 是 U 型：高 20 位是 `0x12345`，rd 是 10，opcode 是 `0x37`。`auipc` 同布局，opcode 换成 `0x17`。

`fadd.d fa0, fa1, fa2` 的字 `0x02C5F553` 是 R 型，但 `funct3` 是 7，表示动态舍入模式（`rm=111`），不是固定的 `funct3` 编码。浮点指令的 `funct3` 经常是舍入模式字段，读的时候不要按整数 R 型的习惯理解。

### 6.3 RVC：16 位格式与压缩条件

RVC 指令固定 16 位，低 2 位是 quadrant：

- `00`：C0，包含 `c.addi4spn`、`c.lw`、`c.ld`、`c.sw`、`c.sd` 等。
- `01`：C1，包含 `c.addi`、`c.jal`、`c.li`、`c.lui`、`c.srli`、`c.srai`、`c.andi`、`c.sub`、`c.xor`、`c.or`、`c.and`、`c.j`、`c.beqz`、`c.bnez`。
- `10`：C2，包含 `c.slli`、`c.lwsp`、`c.ldsp`、`c.jr`、`c.mv`、`c.ebreak`、`c.jalr`、`c.add`、`c.swsp`、`c.sdsp`。

常见的 C 格式：

| 格式 | 用途 | 字段 |
| --- | --- | --- |
| CI | 带立即数和 rd | funct3、imm、rd、op |
| CR | 两个寄存器 | funct4、rd/rs1、rs2、op |
| CSS | 栈存储 | funct3、imm、rs2、op |
| CIW | 宽立即数 | funct3、imm、rd'、op |
| CL | 寄存器基址加载 | funct3、imm、rs1'、imm、rd'、op |
| CS | 寄存器基址存储 | funct3、imm、rs1'、imm、rs2'、op |
| CB | 分支 | funct3、imm、rs1'、imm、op |
| CJ | 跳转 | funct3、imm、op |

`lw a0, 8(sp)` 的真实输出是 `[0x22,0x45]`，也就是半字 `0x4522`。它的低 2 位是 `10`，属于 C2；funct3 是 `010`；rd 是 `a0`；立即数是 8。它是 `c.lwsp`，不是 `c.lw`。`c.lwsp` 的基址固定是 `sp`，所以不需要 rs1 字段；`c.lw` 需要 rs1' 在 `x8` 到 `x15`。

对比：

```text
lw a0, 8(sp)   -> [0x22,0x45]  = 0x4522  c.lwsp
lw a0, 8(s0)   -> [0x08,0x44]  = 0x4408  c.lw
c.lw a0, 8(a1) -> [0x88,0x45]  = 0x4588  c.lw
```

`c.lw` 的 CL 格式要求：

- `rd'` 和 `rs1'` 都在 `x8` 到 `x15`（压缩寄存器组）。
- 偏移是 7 位无符号、4 字节对齐，范围 0 到 124。
- `c.lwsp` 的 `rd` 不能是 `x0`，偏移是 8 位无符号、4 字节对齐，范围 0 到 252。
- 64 位的 `c.ldsp` 偏移是 9 位、8 字节对齐，范围 0 到 504。

本机边界实验：

```text
lw a0, 252(sp)   # [0x7e,0x55]          c.lwsp
lw a0, 256(sp)   # [0x03,0x25,0x01,0x10] 32 位 lw
lw a0, 124(s0)   # [0x68,0x5c]          c.lw
lw a0, 128(s0)   # [0x03,0x25,0x04,0x08] 32 位 lw
addi a0, a0, 31  # [0x7d,0x05]          c.addi
addi a0, a0, 32  # [0x13,0x05,0x05,0x02] 32 位 addi
```

`c.addi` 的立即数范围是 `[-32, 31]`。0 也能汇编，本机 `c.addi a0, 0` 得到 `[0x01,0x05]`，但那是 HINT 编码，不应在正常代码里依赖。显式写 `c.addi a0, 32` 会报：

```text
error: immediate must be an integer in the range [-32, 31]
c.addi a0, 32
           ^
```

自动压缩发生在汇编器的 `emitToStreamer`：

```cpp
void RISCVAsmParser::emitToStreamer(MCStreamer &S, const MCInst &Inst) {
  MCInst CInst;
  bool Res = false;
  const MCSubtargetInfo &STI = getSTI();
  if (!STI.hasFeature(RISCV::FeatureExactAssembly))
    Res = RISCVRVC::compress(CInst, Inst, STI);
  if (Res)
    ++RISCVNumInstrsCompressed;
  S.emitInstruction((Res ? CInst : Inst), STI);
}
```

`RISCVBaseInfo.cpp` 第 175 行的 `RISCVRVC::compress` 做实际匹配。关闭压缩有两种方式：

```powershell
llvm-mc -triple=riscv64 -mattr=-c -show-encoding
llvm-mc -triple=riscv64 -mattr=+c -show-encoding   # 文件里加 .option norvc
```

本机 `-mattr=-c` 的结果：

```text
lw   a0, 8(sp)   # [0x03,0x25,0x81,0x00]
sw   a0, 12(sp)  # [0x23,0x26,0xa1,0x00]
sd   ra, 8(sp)   # [0x23,0x34,0x11,0x00]
addi a0, a0, 1   # [0x13,0x05,0x15,0x00]
```

`.option norvc` 得到同样的 4 字节。这说明压缩不是 `.td` 的 `Inst` 自动变短，而是汇编器在发出 MCInst 前做的替换；`llc` 的代码生成路径还会经过 `riscv-make-compressible` 等 pass，但最终仍要落到这些 16 位记录。

### 6.4 别名化：回显的 mnemonic 不是 opcode

`llvm-mc -show-encoding` 的输出里，输入和回显经常不同：

```text
xori a0, a1, -1   -> not a0, a1       [0x13,0xc5,0xf5,0xff]
bne a0, zero, -8  -> bnez a0, -8      [0x65,0xfd]
jal ra, 2048      -> jal 2048         [0xef,0x00,0x10,0x00]
jalr ra, 0(a0)    -> jalr a0          [0x02,0x95]
addi a0, a0, 1    -> addi a0, a0, 1   [0x05,0x05]
li a0, -1         -> li a0, -1        [0x7d,0x55]
lw a0, 8(a1)      -> lw a0, 8(a1)     [0x88,0x45]
```

`not` 是 `xori rd, rs, -1` 的 `InstAlias`；`bnez` 是 `bne rs, x0, offset` 的别名；`jal offset` 是 `jal ra, offset` 的别名；`jalr rs` 是 `jalr ra, 0(rs)` 的别名。源码在 `RISCVInstrInfo.td` 第 1059 到 1124 行：

```tablegen
def : InstAlias<"not $rd, $rs",  (XORI GPR:$rd, GPR:$rs,      -1)>;
def : InstAlias<"bnez $rs, $offset", ...>;
def : InstAlias<"jal $offset", (JAL X1, simm21_lsb0_jal:$offset)>;
def : InstAlias<"jalr $rs", (JALR X1, GPR:$rs, 0), 3>;
```

别名只是打印/解析层的语法糖。`not` 的字节仍然是 `xori` 的 opcode `0x13`、funct3 `100`、imm `-1`；`bnez` 的字节仍然是 `bne` 的 opcode `0x63`、rs2 `x0`；`jal 2048` 的 rd 仍然是 `ra`。读 `-show-encoding` 时，先看字节，再看回显；不要用回显的 mnemonic 反推 opcode。

RVC 的别名还会把压缩指令打印成普通指令：

```text
addi a0, a0, 1  -> 回显 addi，但字节 [0x05,0x05] 是 c.addi
li a0, -1       -> 回显 li，但字节 [0x7d,0x55] 是 c.li
lw a0, 8(a1)    -> 回显 lw，但字节 [0x88,0x45] 是 c.lw
```

这就是“文本看着是 4 字节指令，字节只有 2 字节”的原因。判断是否压缩，看字节数，不看 mnemonic。

### 6.5 MC 编码与 AsmPrinter 的分工

同一条指令有两条路径：

1. **文本路径**：`llc -filetype=asm` 或 `llvm-mc` 的汇编输出。`RISCVAsmPrinter::emitInstruction`（第 380 行）把 `MachineInstr` 降到 `MCInst`（`lowerToMCInst`，第 1291 行），再由 `MCStreamer` 打印 `AsmString`/别名。这里不一定做编码。
2. **对象路径**：`llc -filetype=obj` 或 `llvm-mc -filetype=obj`。`RISCVMCCodeEmitter::encodeInstruction`（第 559 行）调用生成的 `getBinaryCodeForInstr`，把 `MCInst` 变成字节，并把符号引用登记成 fixup。

`getBinaryCodeForInstr` 的固定位来自 `InstBits[]`，可变字段来自 `getMachineOpValue`/`getImmOpValue`。`ADD` 的固定位是 `UINT64_C(51)`（`0x33`），`ADDI` 是 `UINT64_C(19)`（`0x13`）。`getImmOpValue` 在 `RISCVMCCodeEmitter.cpp` 第 769 行，它负责把立即数或符号表达式转成编码值；如果遇到需要重定位的符号，会创建 fixup，而不是硬编码一个数。

`llvm-mc -show-encoding` 走的是 MC 层，所以能看到字节；`llc` 的文本汇编走的是 AsmPrinter，所以只看到 mnemonic 和操作数。`llc -filetype=obj` 才会走编码器。这个分工也解释了为什么 `llc` 文本里 `lw a0, 8(sp)` 可能显示成 `lw`，但目标文件里是 2 字节的 `c.lwsp`。

本机用对象文件验证：

```powershell
llvm-mc -triple=riscv64 -mattr=+c -filetype=obj enc.s -o enc.o
llvm-objdump -d enc.o
```

真实输出：

```text
0: 02a58513      addi a0, a1, 0x2a
4: 00c58533      add  a0, a1, a2
8: 7e113c23      sd   ra, 0x7f8(sp)
c: 00b50863      beq  a0, a1, 0x1c <.text+0x1c>
10: 001000ef     jal  0x810 <.text+0x810>
```

`7e113c23` 就是 `sd ra, 2040(sp)` 的 4 字节；`0x7f8` 是 2040 的十六进制。反汇编显示的立即数是解码后的值，不是编码字段的原始位段。

### 6.6 越界立即数：真实报错

`addi` 的 12 位有符号立即数范围是 `[-2048, 2047]`。写 2048：

```text
error: operand must be a symbol with %lo/%pcrel_lo/%tprel_lo specifier or an integer in the range [-2048, 2047]
addi a0, a1, 2048
             ^
```

`sd a0, 2048(sp)` 同样报 12 位范围，因为 S 型的 `imm` 也是 12 位有符号：

```text
error: operand must be a symbol with %lo/%pcrel_lo/%tprel_lo specifier or an integer in the range [-2048, 2047]
sd a0, 2048(sp)
       ^
```

分支的立即数是 13 位、2 字节对齐，范围 `[-4096, 4094]`。写 4096：

```text
error: immediate must be a multiple of 2 bytes in the range [-4096, 4094]
beq a0, a1, 4096
            ^
```

`jal` 是 21 位、2 字节对齐，范围 `[-1048576, 1048574]`。写 1048576：

```text
error: immediate must be a multiple of 2 bytes in the range [-1048576, 1048574]
jal ra, 1048576
        ^
```

边界成功值：

```text
addi a0, a1, 2047   # [0x13,0x85,0xf5,0x7f]
sd   ra, 2040(sp)   # [0x23,0x3c,0x11,0x7e]
beq  a0, a1, 4094   # [0xe3,0x0f,0xb5,0x7e]
jal  ra, 1048574    # [0xef,0xf0,0xff,0x7f]
```

这些报错发生在汇编解析/立即数检查阶段，不是编码器溢出。看到 `must be a multiple of 2 bytes` 时，先查对齐；看到 `range [-2048, 2047]` 时，先查格式的立即数位宽。

## 可跑实验

### 实验 1：同一组指令在 +c 和 -c 下的字节

目标：观察自动压缩和别名化。

```powershell
$asm = @(
  'lw a0, 8(sp)'
  'sw a0, 12(sp)'
  'sd ra, 8(sp)'
  'addi a0, a0, 1'
  'xori a0, a1, -1'
  'bne a0, zero, -8'
  'jal ra, 2048'
  'jalr ra, 0(a0)'
)
[IO.File]::WriteAllText("$env:TEMP\enc-c.s", ($asm -join "`n")+"`n", (New-Object Text.UTF8Encoding($false)))
llvm-mc -triple=riscv64 -mattr=+c -show-encoding "$env:TEMP\enc-c.s"
llvm-mc -triple=riscv64 -mattr=-c -show-encoding "$env:TEMP\enc-c.s"
```

预期：`+c` 下前四条是 2 字节，`xori` 回显 `not`，`bne` 回显 `bnez`；`-c` 下全部是 4 字节，别名仍然存在。

### 实验 2：越界立即数

目标：确认不同格式的立即数范围和报错文本。

```powershell
$bad = @('addi a0, a1, 2048','sd a0, 2048(sp)','beq a0, a1, 4096','jal ra, 1048576')
[IO.File]::WriteAllText("$env:TEMP\enc-bad.s", ($bad -join "`n")+"`n", (New-Object Text.UTF8Encoding($false)))
llvm-mc -triple=riscv64 -mattr=+c -show-encoding "$env:TEMP\enc-bad.s"
```

预期：四条各自报错，进程返回 1；报错文本分别指出 12 位范围、12 位范围、13 位 2 字节对齐、21 位 2 字节对齐。

### 实验 3：按格式拆字段

目标：不靠反汇编，直接从 32 位字拆出字段。

```powershell
function Bits([uint32]$w,[int]$hi,[int]$lo) {
  $len = $hi - $lo + 1
  return (($w -shr $lo) -band ((1 -shl $len) - 1))
}
function Sx([long]$v,[int]$n) {
  $m = 1 -shl ($n - 1)
  if ($v -band $m) { return $v - (1 -shl $n) }
  return $v
}
$w = [Convert]::ToUInt32('02A58513',16)
"opcode=0x{0:X2} rd={1} funct3={2} rs1={3} imm={4}" -f (Bits $w 6 0),(Bits $w 11 7),(Bits $w 14 12),(Bits $w 19 15),(Sx (Bits $w 31 20) 12)
$w = [Convert]::ToUInt32('7E113C23',16)
$imm = ((Bits $w 31 25) -shl 5) -bor (Bits $w 11 7)
"opcode=0x{0:X2} funct3={1} rs1={2} rs2={3} imm={4}" -f (Bits $w 6 0),(Bits $w 14 12),(Bits $w 19 15),(Bits $w 24 20),(Sx $imm 12)
```

真实输出：

```text
opcode=0x13 rd=10 funct3=0 rs1=11 imm=42
opcode=0x23 funct3=3 rs1=2 rs2=1 imm=2040
```

### 实验 4：反汇编验证

目标：确认字节和反汇编一致。

```powershell
llvm-mc -triple=riscv64 -mattr=+c -filetype=obj "$env:TEMP\enc-c.s" -o "$env:TEMP\enc-c.o"
llvm-objdump -d "$env:TEMP\enc-c.o"
```

预期：`addi`、`add`、`sd`、`beq`、`jal` 的 8 位十六进制字与 6.2 表一致；`c.lwsp` 等压缩指令显示为普通 mnemonic，但地址步进是 2。

### 实验 5：RVC 边界

目标：看压缩何时停止。

```powershell
$range = @('lw a0, 252(sp)','lw a0, 256(sp)','lw a0, 124(s0)','lw a0, 128(s0)','addi a0, a0, 31','addi a0, a0, 32')
[IO.File]::WriteAllText("$env:TEMP\enc-range.s", ($range -join "`n")+"`n", (New-Object Text.UTF8Encoding($false)))
llvm-mc -triple=riscv64 -mattr=+c -show-encoding "$env:TEMP\enc-range.s"
```

预期：`252(sp)`、`124(s0)`、`31` 是 2 字节；`256(sp)`、`128(s0)`、`32` 退回 4 字节。`c.addi a0, 32` 显式写会报 `[-32, 31]`。

## 源码落点

| 位置 | 符号 | 说明 |
| --- | --- | --- |
| `RISCVInstrFormats.td:387` | `RVInstR` | R 型 `funct7` 位段 |
| `RISCVInstrFormats.td:467` | `RVInstI` | I 型 `imm12` 位段 |
| `RISCVInstrFormats.td:500` | `RVInstS` | S 型立即数拆分 |
| `RISCVInstrFormats.td:515` | `RVInstB` | B 型分支偏移拆分 |
| `RISCVInstrFormats.td:548` | `RVInstU` | U 型高 20 位 |
| `RISCVInstrFormats.td:559` | `RVInstJ` | J 型跳转偏移拆分 |
| `RISCVInstrInfo.td:1059-1124` | `InstAlias` | `li`/`mv`/`not`/`bnez`/`jal`/`jalr` 别名 |
| `RISCVInstrInfoC.td:219` | `class CLoad_ri` | `c.lw`/`c.ld` 的 CL 格式 |
| `RISCVInstrInfoC.td:281` | `def C_LW` | `c.lw`，`GPRC` + `uimm7_lsb00` |
| `RISCVInstrInfoC.td:443` | `def C_LWSP` | `c.lwsp`，`GPRNoX0` + `uimm8_lsb00` |
| `RISCVInstrInfoC.td:490` | `def C_SWSP` | `c.swsp` |
| `RISCVInstrInfoC.td:502` | `def C_SDSP` | `c.sdsp`，`uimm9_lsb000` |
| `AsmParser/RISCVAsmParser.cpp:3711` | `emitToStreamer` | 自动压缩入口 |
| `MCTargetDesc/RISCVBaseInfo.cpp:175` | `RISCVRVC::compress` | 压缩匹配实现 |
| `MCTargetDesc/RISCVMCCodeEmitter.cpp:559` | `encodeInstruction` | MCInst 到字节 |
| `MCTargetDesc/RISCVMCCodeEmitter.cpp:769` | `getImmOpValue` | 立即数/符号编码与 fixup |
| `RISCVAsmPrinter.cpp:380` | `emitInstruction` | MachineInstr 到 MCInst 的打印路径 |
| `RISCVAsmPrinter.cpp:1291` | `lowerToMCInst` | 操作数 lowering |
| `RISCVGenMCCodeEmitter.inc:17` | `UINT64_C(51), // ADD` | `ADD` 固定位 `0x33` |
| `RISCVGenMCCodeEmitter.inc:7065` | `case RISCV::ADD` | 可变字段拼接 |
| `RISCVGenInstrInfo.inc:13740` | `ADD = 13725` | 枚举号与源码注释 |

## 常见坑

1. **把字节数组当成大端字。** `[0x13,0x85,0xa5,0x02]` 是内存顺序，字是 `0x02A58513`。直接用 `0x1385A502` 拆字段会全错。

2. **把 RVC 的 `c.lwsp` 叫成 `c.lw`。** `lw a0, 8(sp)` 是 `c.lwsp`（C2，基址固定 `sp`）；`c.lw` 是 CL 格式，基址必须在 `x8` 到 `x15`。两者字段布局不同。

3. **用 mnemonic 判断是否压缩。** `addi a0, a0, 1` 回显是 `addi`，字节却是 `c.addi`；`lw a0, 8(a1)` 回显是 `lw`，字节是 `c.lw`。看字节数。

4. **把别名当成不同 opcode。** `not` 是 `xori -1`，`bnez` 是 `bne x0`，`jal offset` 是 `jal ra, offset`。字节仍然是对应基础指令的编码。

5. **忘记 B/J 型的低位对齐。** 分支和 `jal` 的立即数最低位不存，范围是 2 字节对齐；写奇数会报 `multiple of 2 bytes`。

6. **把 `funct3` 都当操作码。** 浮点 R 型的 `funct3` 经常是舍入模式（`rm`），`fadd.d` 的 7 表示动态舍入，不是整数 ALU 的 funct3。

7. **拿 `llc` 文本汇编验证编码。** `llc` 默认输出文本，不显示字节；编码要用 `llvm-mc -show-encoding` 或 `llc -filetype=obj` + `llvm-objdump`。

8. **把符号立即数当常量。** `%lo(symbol)`、`%pcrel_lo(...)` 需要 fixup/重定位，编码器不会在编译期填一个固定数。看到 `getImmOpValue` 里的 `MO.isExpr()` 检查和 `FixupKind` 赋值，就知道这是重定位路径。

9. **忽略 `.option norvc`。** 即使 `-mattr=+c`，文件里的 `.option norvc` 也会关闭压缩；反过来 `.option rvc` 可以重新打开。读测试时先看文件级选项。

## 自我检查

- [ ] 我能不看表写出 R/I/S/B/U/J 六种格式的字段位置。
- [ ] 我能把 `0x02A58513` 拆成 opcode、rd、funct3、rs1、imm，并说明每段来自哪几位。
- [ ] 我能解释 `sd ra, 2040(sp)` 的 `imm[11:5]` 和 `imm[4:0]` 如何拼成 2040。
- [ ] 我能区分 `c.lwsp` 和 `c.lw`，并说出各自的基址约束和偏移范围。
- [ ] 我能解释 `not`、`bnez`、`jal offset` 为什么不是新的 opcode。
- [ ] 我知道 `llvm-mc -show-encoding`、`llc -filetype=asm`、`llc -filetype=obj` 各自走哪条路径。
- [ ] 我能说出 `addi`、分支、`jal` 的立即数范围和报错文本差异。
- [ ] 我能用 `llvm-objdump -d` 验证字节和反汇编一致。

## 延伸阅读

- RISC-V 非特权指令集手册（编码章节）：<https://riscv.org/technical/specifications/>
- RISC-V 压缩指令扩展（RVC）：<https://riscv.org/technical/specifications/>
- LLVM MC 层文档：<https://llvm.org/docs/CodeGenerator.html#the-mc-layer>
- `llvm/lib/Target/RISCV/RISCVInstrFormats.td`
- `llvm/lib/Target/RISCV/RISCVInstrFormatsC.td`
- `llvm/lib/Target/RISCV/RISCVInstrInfoC.td`
- `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp`
- `llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp`
- `llvm/lib/Target/RISCV/RISCVAsmPrinter.cpp`

[上一章：生成物解剖](./05-generated-artifacts.md) | [下一章：操作数与 Pattern](./07-operands-and-patterns.md)