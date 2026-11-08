# Ch 06 — 指令编码与 MC 层

> **目标**：把一条 `.td` 指令追到 2/4 字节机器码；说清 opcode 表、`InstBits`、`getImmOpValue` 的分工；理解 RVC 的压缩时机；区分 fixup 与 relocation，并读懂 `%hi/%lo/%pcrel_hi/%pcrel_lo/%got_pcrel_hi/%tprel_*` 生成的重定位。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`。本章“实测”均来自该树与构建；未跑过的明确标“未实测”。

> **前置**：Ch 04（`ALU_ri`/`ALU_rr` 与 opcode 编号）、Ch 05（`GPRC` 与 `x8`–`x15`）。

---

## 6.1 为什么维护者要读编码与 MC 层

编码层是“错了也能跑”的重灾区：

1. **位切片写错**。`Inst{14-12}` 写成 `{13-11}`，生成的仍像合法指令，只是 funct3 变了；只 CHECK 助记符的测试抓不到。
2. **fixup 写错**。`%hi` 的 `+0x800` 舍入、分支的 bit scramble、`%pcrel_lo` 配对，错了只在链接期或运行时暴露。
3. **压缩/放宽边界写错**。RVC 在 `MCInst` 层完成；越界分支在汇编期会被改写成“反向分支 + `jal`”。不知道这条链，就会把多出的字节归咎于 linker。

主线文件都不大，但都在关节上：`RISCVInstrFormats.td`（703 行）、`RISCVInstrFormatsC.td`（392 行）、`MCTargetDesc/RISCVBaseInfo.h`（1,059 行）、`RISCVMCCodeEmitter.cpp`（1,003 行）、`RISCVAsmBackend.cpp`（1,032 行）、`RISCVFixupKinds.h`（75 行）。

---

## 6.2 先建立直觉：一条指令的六次变形

```
RISCVInstrInfo.td / RISCVInstrFormats.td
        │  llvm-tblgen
        ├─ -gen-instr-info           → RISCVGenInstrInfo.inc（opcode、TSFlags、Size）
        ├─ -gen-emitter              → RISCVGenMCCodeEmitter.inc（InstBits[]、getBinaryCodeForInstr）
        └─ -gen-compress-inst-emitter → RISCVGenCompressInstEmitter.inc（compressInst）
                    │
                    ▼
             MCInst（汇编器/AsmPrinter 已尝试压缩）
                    │  encodeInstruction()
                    ▼
        getBinaryCodeForInstr() + getImmOpValue()
                    │
        ┌───────────┴────────────┐
        │ 值是立即数              │ 值是符号/重定位表达式
        ▼                        ▼
  直接 OR 进 InstBits[]      生成 MCFixup（offset + kind + expr）
                                 │
                                 ▼
                    RISCVAsmBackend::applyFixup()
                     ├─ 能解析 → adjustFixupValue() 后写回字节
                     └─ 不能解析 → ELFObjectWriter 记 .rela.text
```

只需盯住三个问题：**位怎么摆**（`.td` 的 `Inst{...}`）、**地址不确定怎么办**（`MCFixup`，能算就算，算不了变 relocation）、**何时变 2 字节**（`emitInstruction` 前 `compressInst()` 已把 `MCInst` 换成 `C_*`）。

---

## 6.3 位切片：格式类就是“字段落位表”

`RISCVInstrFormats.td:13-21` 的注释是读法：指令按 MSB 在左描述，但**立即数不是逻辑值，而是排好序的编码位序列**——“JAL's immediate is logically a 21-bit value ..., we describe it as an imm20 to match how it is encoded.” R 型基类在 `:373`：

```tablegen
class RVInstRBase<bits<3> funct3, RISCVOpcode opcode, dag outs, dag ins,
                  string opcodestr, string argstr>
    : RVInst<..., InstFormatR> {
  bits<5> rs2; bits<5> rs1; bits<5> rd;
  let Inst{24-20} = rs2;
  let Inst{19-15} = rs1;
  let Inst{14-12} = funct3;
  let Inst{11-7}  = rd;
  let Inst{6-0}   = opcode.Value;
}
```

六种主线格式（行号已核对）：

| 格式 | 类（行号） | 字段切片 |
|---|---|---|
| R | `RVInstRBase` 373 / `RVInstR` 387 | `funct7[31:25]`、`rs2[24:20]`、`rs1[19:15]`、`funct3[14:12]`、`rd[11:7]` |
| I | `RVInstI` 467 | `imm12[31:20]`、`rs1`、`funct3`、`rd[11:7]` |
| S | `RVInstS` 500 | `imm[11:5]`→31:25、`rs2`、`rs1`、`imm[4:0]`→11:7 |
| B | `RVInstB` 515 | `imm[12]`→31、`imm[10:5]`→30:25、`imm[4:1]`→11:8、`imm[11]`→7 |
| U | `RVInstU` 548 | `imm20[31:12]`、`rd[11:7]` |
| J | `RVInstJ` 559 | `imm[20]`→31、`imm[10:1]`→30:21、`imm[11]`→20、`imm[19:12]`→19:12 |

B/J 只是把“去掉 bit0 的位序列”打散塞进空隙；S/B 拆 `imm` 是为了让 `rs1`/`rs2` 保持固定位置——这是 ISA 的硬约束。

opcode 数值在 `RISCVOpcode`（`:121`）：`OP=0x33`（`:149`）、`OP_IMM=0x13`（`:141`）、`LUI=0x37`（`:150`）、`AUIPC=0x17`（`:142`）、`JAL=0x6f`（`:163`）。TableGen 把它生成为 `RISCVGenSearchableTables.inc:12652` 的 `RISCVOpcodesList[]`。

注意：`RISCVBaseInfo.h` **没有** `OPCODE_*` 宏。它提供 `RISCVInsnOpcode::RISCVOpcode` 查表接口（`:705`）和编码器真正使用的 `RISCVII::InstFormat*` 常量（`:158-186`，必须与 `.td` 的 `InstFormat` 一致）。`getImmOpValue` 用 `RISCVII::getFormat(Desc.TSFlags)`（`:294`）决定 `lo12_i` 还是 `lo12_s`。

---

## 6.4 手算五条，再用 `llvm-mc` 对账

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cat > /tmp/base.s <<'EOF'
add     a0, a1, a2
addi    a1, a2, -1
lui     a3, 0x12345
auipc   a4, 0x12345
jal     ra, 8
EOF
$T/bin/llvm-mc -triple=riscv64 -mattr=+m -show-encoding /tmp/base.s
```

实测：
```
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
	addi	a1, a2, -1                      # encoding: [0x93,0x05,0xf6,0xff]
	lui	a3, 74565                       # encoding: [0xb7,0x56,0x34,0x12]
	auipc	a4, 74565                       # encoding: [0x17,0x57,0x34,0x12]
	jal	8                               # encoding: [0xef,0x00,0x80,0x00]
```

| 指令 | 格式 | 字段 | 32 位值 |
|---|---|---|---|
| `add a0,a1,a2` | R | `f7=0,rs2=12,rs1=11,f3=0,rd=10,op=0x33` | `0x00c58533` |
| `addi a1,a2,-1` | I | `imm12=0xfff,rs1=12,f3=0,rd=11,op=0x13` | `0xfff60593` |
| `lui a3,0x12345` | U | `imm20=0x12345,rd=13,op=0x37` | `0x123456b7` |
| `auipc a4,0x12345` | U | `imm20=0x12345,rd=14,op=0x17` | `0x12345717` |
| `jal ra,8` | J | `imm=8,rd=1,op=0x6f` | `0x008000ef` |

`jal` 单独走一遍：`getImmOpValueAsrN<1>` 先右移一位（去掉恒 0 的 bit0）得 `op=4`；生成的编码器把它拆进 `31/30-21/20/19-12` 四处（`(op&0x80000)<<12`、`(op&0x3ff)<<21`、`(op&0x400)<<10`、`(op&0x7f800)<<1`），本例只有 `<<21` 项非零得 `0x00800000`，加上 `rd=1` 的 `0x80` 与 `0x6f`，正好 `0x008000ef`。`-show-encoding` 的 `[0xef,0x00,0x80,0x00]` 是同一值的 little-endian 字节。

分支同理：`beq a0,a1,8` 的 `imm12` 也是“去掉 bit0 的 12 位字段”，编码器同样走 `getImmOpValueAsrN<1>`；固定立即数与符号 fixup 是两条路径，但必须落到同一组位。

---

## 6.5 编码器：`InstBits[]` + 操作数 OR

TableGen 把每条指令的固定位算成 64 位常量（`RISCVGenMCCodeEmitter.inc`）：

```cpp
UINT64_C(51),	// ADD      (:17)
UINT64_C(19),	// ADDI     (:19)
UINT64_C(23),	// AUIPC    (:420)
UINT64_C(111),	// JAL      (:1185)
UINT64_C(55),	// LUI      (:1208)
```

`51=0x33`、`19=0x13`、`23=0x17`、`111=0x6f`、`55=0x37`，正好是 opcode。`getBinaryCodeForInstr` 是生成的（`RISCVMCCodeEmitter.cpp` 末尾 include），核心是：

```cpp
uint64_t Value = InstBits[TableIndex];          // :3216
switch (opcode) {
  case RISCV::ADD:
  case RISCV::ZIP8HP:
  ...
  case RISCV::ZIP8P: {
    op = getMachineOpValue(MI, MI.getOperand(2), Fixups, STI);
    Value |= (op & 0x1f) << 20;                 // rs2
    op = getMachineOpValue(MI, MI.getOperand(1), Fixups, STI);
    Value |= (op & 0x1f) << 15;                 // rs1
    op = getMachineOpValue(MI, MI.getOperand(0), Fixups, STI);
    Value |= (op & 0x1f) << 7;                  // rd
    break;
  }
```

`ADD` 与 `ZIP8P` 共享这段 R 型 body。寄存器值来自 `getMachineOpValue`（`:687`）：`isReg()` 调 `getEncodingValue()`，`isImm()` 直接返回。

分叉点在 `getImmOpValue`（`:775`）：立即数直接返回；表达式按 `MIFrm` 选 `FixupKind` 并 `addFixup`（`:139`）；若 `EnableRelax && RelaxCandidate`，再 `setLinkerRelaxable()`，让 `applyFixup` 补一条 `R_RISCV_RELAX`。`encodeInstruction`（`:564-683`）只按 `Desc.getSize()` 写 2/4/6/8 字节——**编码器不负责压缩**。

---

## 6.6 RVC：同一条指令的两种宽度

压缩发生在 `MCInst` 层（`RISCVAsmParser.cpp:3732`）：

```cpp
if (!STI.hasFeature(RISCV::FeatureExactAssembly))
  Res = RISCVRVC::compress(CInst, Inst, STI);
S.emitInstruction((Res ? CInst : Inst), STI);
```

`RISCVAsmPrinter::EmitToStreamer`（`:276`）同构。实测 `+c`/`-c`：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
printf 'addi a0,a0,1\nadd a0,a1,a2\nlw a0,4(a1)\nsd a0,8(a1)\njal ra,8\nj 8\n' > /tmp/rvc.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+c  -show-encoding /tmp/rvc.s
$T/bin/llvm-mc -triple=riscv64 -mattr=-c -show-encoding /tmp/rvc.s
```

`+c` 实测：

```
	addi	a0, a0, 1        # [0x05,0x05]
	add	a0, a1, a2       # [0x33,0x85,0xc5,0x00]
	lw	a0, 4(a1)        # [0xc8,0x41]
	sd	a0, 8(a1)        # [0x88,0xe5]
	jal	8                # [0xef,0x00,0x80,0x00]
	j	8                # [0x21,0xa0]
```

`-c` 下 `addi/lw/sd/j` 变 4 字节。`add a0,a1,a2` 始终 4 字节（`c.add` 要求 `rd==rs1`）；`jal ra,8` 也不能压（RV64 无 `c.jal`，`j 8` 才能压成 `c.j`）。

`.option norvc` 清 `FeatureStdExtC` 与 `FeatureStdExtZca`（`:3467`），`.option rvc` 设回 `FeatureStdExtC`（`:3456`）。实测（复制命令见 6.11 实验 2）：

```
addi a0,a0,1        # [0x05,0x05]
.option norvc
addi a0,a0,1        # [0x13,0x05,0x15,0x00]
.option rvc
addi a0,a0,2        # [0x09,0x05]
```

### `RISCVCompressInstEmitter.inc` 的角色

由 `-gen-compress-inst-emitter` 从 `CompressPat` 生成。同一 `.inc` 被三个宏各取一段：`GEN_COMPRESS_INSTR`→`compressInst`（AsmParser/AsmPrinter）、`GEN_UNCOMPRESS_INSTR`→`uncompressInst`（InstPrinter/放宽）、`GEN_CHECK_COMPRESS_INSTR`→`getCompressedSize`（`getInstSizeInBytes`，`:2090`）。第三段决定机器层估算的分支距离，必须与 MC 层一致。

### 为什么压缩要求寄存器在 `x8`–`x15`

**不是所有 RVC 都限制**，限制来自字段宽度：

| 格式 | 字段 | 可编码 | 例子 |
|---|---|---|---|
| CR/CI（`:29`/`:45`） | 5 位 | 全部 32 个 | `c.add`、`c.mv`、`c.addi` |
| CL/CS/CA（`:87`/`:102`/`:114`） | 3 位 | `x8`–`x15` | `c.lw`、`c.sw`、`c.sub` |
| CB/CIW（`:127`/`:73`） | 3 位 | `x8`–`x15` | `c.beqz`、`c.addi4spn` |

3 位字段只能表示 `0`–`7`，硬件解释为 `x8+field`。所以这些格式的 `CompressPat` 显式写 `GPRC`（`RISCVInstrInfoC.td:700`），而 `c.addi` 的 CI 是 5 位 `rd`，用 `GPRNoX0`（`:728`）。实测（`printf 'lw a0,4(a1)\nlw a0,4(t0)\nlw s0,4(s1)\naddi t0,t0,1\n' > /tmp/regs.s; $T/bin/llvm-mc -triple=riscv64 -mattr=+c -show-encoding /tmp/regs.s`）：

```
lw   a0, 4(a1)   # [0xc8,0x41]
lw   a0, 4(t0)   # [0x03,0xa5,0x42,0x00]
lw   s0, 4(s1)   # [0xc0,0x40]
addi t0, t0, 1   # [0x85,0x02]
```

`t0=x5` 不在 `GPRC`，`lw a0,4(t0)` 保持 4 字节；`addi t0,t0,1` 仍是 2 字节。**硬约束只对 CL/CS/CA/CB/CIW 成立。**

RVC fixup 范围：`rvc_jump [-2048,2046]`、`rvc_branch [-256,254]`、`rvc_imm [-32,31]`（`RISCVAsmBackend.cpp:121-145`）。

---

## 6.7 fixup 与 `MCExpr`：地址还不确定时怎么办

**fixup** 是汇编器内部的“洞”（`offset`+`kind`+`MCExpr`）；**relocation** 是 `.rela.text` 里的 `R_RISCV_*`。编码器先按值为 0 编码，fixup 能算就算，算不了变 relocation（`MCFixup.h:62-68`）。

### 6.7.1 前提纠正：`RISCVMCExpr.h` 在本树已不存在

`ls .../MCTargetDesc/RISCVMCExpr.h` 会报 `No such file or directory`。历史重构：`f11dd116e0aa`（Replace RISCVMCExpr with MCSpecifierExpr）、`e3025c95090f`（Rename VK_ to S_），均在 2025-06-15。现在是 `MCSpecifierExpr`（`MCExpr.h:494`）+ `RISCV::Specifier`（`RISCVMCAsmInfo.h:34-55`）：`S_None/S_LO/S_PCREL_LO/S_PCREL_HI/S_TPREL_LO/S_CALL_PLT/S_GOT_HI/S_QC_*`。其余 specifier 直接复用 ELF relocation 编号：

```cpp
.Case("lo", RISCV::S_LO)               .Case("hi", ELF::R_RISCV_HI20)
.Case("pcrel_lo", RISCV::S_PCREL_LO)   .Case("pcrel_hi", RISCV::S_PCREL_HI)
.Case("got_pcrel_hi", RISCV::S_GOT_HI) .Case("tprel_lo", RISCV::S_TPREL_LO)
.Case("tprel_hi", ELF::R_RISCV_TPREL_HI20)
.Case("tprel_add", ELF::R_RISCV_TPREL_ADD)
```

`git show e3025c95090f^:...RISCVMCExpr.h` 显示旧 `VK_*` 只有 `VK_LO`、`VK_PCREL_LO`、`VK_TPREL_LO`、`VK_QC_ABS20`；`%hi/%pcrel_hi/%got_pcrel_hi` 一直是 ELF 编号。语义对应：`VK_LO→S_LO`、`VK_PCREL_LO→S_PCREL_LO`、`VK_TPREL_LO→S_TPREL_LO`，`%hi→R_RISCV_HI20`、`%pcrel_hi→S_PCREL_HI`、`%got_pcrel_hi→S_GOT_HI`。

### 6.7.2 选 fixup kind

`%lo` 在 I/S 型位置不同（`RISCVMCCodeEmitter.cpp:828`）：

```cpp
case RISCV::S_LO:
  if (MIFrm == RISCVII::InstFormatI)      FixupKind = RISCV::fixup_riscv_lo12_i;
  else if (MIFrm == RISCVII::InstFormatS) FixupKind = RISCV::fixup_riscv_lo12_s;
```

`%pcrel_lo`/`%tprel_lo` 同构（`:841`/`:859`）；`%hi`→`fixup_riscv_hi20`（`:836`）；`%got_pcrel_hi` 直接是 relocation（`:854`）。所以 `%hi` 的位由汇编器写回，`%got_pcrel_hi` 的 immediate 保持 0 等 linker：

```
lui   a0, %hi(foo)             # [0x37,0bAAAA0101,A,A] kind: fixup_riscv_hi20
auipc a3, %got_pcrel_hi(foo)   # [0x97,0x06,0x00,0x00] relocation type: 20
```

### 6.7.3 `getFixupKindInfo` 与 `adjustFixupValue`

`RISCVAsmBackend.cpp:58` 的表必须与 `RISCVFixupKinds.h` 同序：`hi20 (12,20)`、`lo12_i (20,12)`、`lo12_s (0,32)`、`pcrel_hi20 (12,20)`、`jal (12,20)`、`branch (0,32)`、`rvc_jump (2,11)`、`rvc_branch (0,16)`、`rvc_imm (0,16)`、`call (0,64)`。`offset` 是写回左移位数，`bits` 是占用位数。

`adjustFixupValue`（`:500`）的关键三段（为排版省略 `Ctx.reportError` 的 `MCLoc` 实参）：

```cpp
return ((Value + 0x800) >> 12) & 0xfffff;          // %hi 舍入
if (!isInt<13>(Value)) reportError("fixup value out of range");
if (Value & 1)          reportError("fixup value must be 2-byte aligned");
return ((Value>>12&1)<<31)|((Value>>5&0x3f)<<25)|((Value>>1&0xf)<<8)|((Value>>11&1)<<7);
```

实测（`printf '.equ foo,0x12345\nlui a0,%%hi(foo)\naddi a0,a0,%%lo(foo)\n.equ bar,0x12fff\nlui a1,%%hi(bar)\naddi a1,a1,%%lo(bar)\n' > /tmp/abs.s`，再 `llvm-mc -filetype=obj` + `llvm-objdump -d`）：`0x12345`→`lui 0x12`+`addi 0x345`；`0x12fff`→`lui 0x13`+`addi -1`（`+0x800` 后右移 12 位）。`applyFixup`（`:936`）把结果左移 `TargetOffset` 后逐字节 OR 进 `Data`。

### 6.7.4 `FK_Data_4` 与分支 fixup

`FK_Data_4 < FirstTargetFixupKind`，`getFixupKindInfo` 直接返回内置的 `{"FK_Data_4",0,32,0}`（`MCAsmBackend.cpp:94`）：`.word foo` 是“原值→小端写 4 字节”，分支要范围/对齐检查 + bit scramble。relocation 也不同：

```
0x0 R_RISCV_32 foo 0x0        # .word foo
0x4 R_RISCV_BRANCH foo 0x0    # beq a0,a1,foo（.option exact）
```

`foo - .` 会拆成 `R_RISCV_ADD32`+`R_RISCV_SUB32`（`addReloc`，`:847`）。

### 6.7.5 `%pcrel_lo` 配对

`%pcrel_lo(label)` 的 label 必须指向同一条 `auipc` 的 `%pcrel_hi`。`getPCRelHiFixup`（`:684`）按 fragment/offset 找同 offset 的 `fixup_riscv_pcrel_hi20`（或 raw GOT/TLS HI），找不到报 `could not find corresponding %pcrel_hi`。所以 `la` 展开后 label 贴在 `auipc` 上；手写时放到 `addi` 后面会立刻失败。

---

## 6.8 实测：`call`/`la`/`%pcrel_lo` 生成什么 relocation

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cat > /tmp/reloc.s <<'EOF'
.text
.globl foo
call foo
la a0, foo
lui a1, %hi(foo)
addi a1, a1, %lo(foo)
EOF
$T/bin/llvm-mc -triple=riscv64 -mattr=+m,+a,+c -filetype=obj /tmp/reloc.s -o /tmp/reloc.o
$T/bin/llvm-readobj -r /tmp/reloc.o
```

实测：
```
0x0  R_RISCV_CALL_PLT foo 0x0
0x8  R_RISCV_PCREL_HI20 foo 0x0
0xC  R_RISCV_PCREL_LO12_I .Lpcrel_hi0 0x0
0x10 R_RISCV_HI20 foo 0x0
0x14 R_RISCV_LO12_I foo 0x0
```

`call foo` 展开成 `auipc ra`+`jalr ra`，relocation 只挂 `auipc`；`la` 是 `PCREL_HI20`+`PCREL_LO12_I`，且 `%pcrel_lo` 引用 `.Lpcrel_hi0`；`%hi/%lo` 是绝对地址对。默认 `llvm-mc` **没有** `+relax`，所以这里没有 `R_RISCV_RELAX`。

加 `+relax` 后每条可放宽 relocation 后多一条 `R_RISCV_RELAX`（同 offset）：

```
0x0 R_RISCV_CALL_PLT foo 0x0
0x0 R_RISCV_RELAX - 0x0
0x8 R_RISCV_PCREL_HI20 foo 0x0
0x8 R_RISCV_RELAX - 0x0
0xC R_RISCV_PCREL_LO12_I .Lpcrel_hi0 0x0
0xC R_RISCV_RELAX - 0x0
...
```

逻辑在 `getImmOpValue` 末尾的 `setLinkerRelaxable()`，落成 relocation 在 `applyFixup`→`addReloc`（`:847`）。

GOT/TLS 写法（`.Ltmp2` 必须在 `auipc` 前）；接在上面的 `reloc.s` 之后，offset 从 0x18 起：

```asm
.Ltmp2:
auipc a3, %got_pcrel_hi(foo)
ld    a3, %pcrel_lo(.Ltmp2)(a3)
lui   a4, %tprel_hi(foo)
addi  a4, a4, %tprel_lo(foo)
add   a4, a4, tp, %tprel_add(foo)
```

实测：

```
0x18 R_RISCV_GOT_HI20 foo 0x0
0x1C R_RISCV_PCREL_LO12_I .Ltmp2 0x0
0x20 R_RISCV_TPREL_HI20 foo 0x0
0x24 R_RISCV_TPREL_LO12_I foo 0x0
0x28 R_RISCV_TPREL_ADD foo 0x0
```

`%tprel_add` 写法是 `add rd, rs2, tp, %tprel_add(sym)`，第 3 个操作数必须是 `tp`/`x4`；实测编码 `[0x33,0x07,0x47,0x00]`，fixup 为 `relocation type: 32`。

---

## 6.9 越界、对齐与真实报错

常量立即数在解析期就被拒绝（`llvm-mc -triple=riscv64 -mattr=+m,+a,+c -show-encoding` 实测）：

| 输入 | 报错 |
|---|---|
| `addi a0,a0,2048` | `operand must be a symbol with %lo/%pcrel_lo/%tprel_lo specifier or an integer in the range [-2048, 2047]` |
| `lui a0,0x100000` | `operand must be a symbol with %hi/%tprel_hi specifier or an integer in the range [0, 1048575]` |
| `beq a0,a1,4096` | `immediate must be a multiple of 2 bytes in the range [-4096, 4094]` |
| `jal ra,1048576` | `immediate must be a multiple of 2 bytes in the range [-1048576, 1048574]` |
| `c.beqz a0,300` | `immediate must be a multiple of 2 bytes in the range [-256, 254]` |
| `c.addi a0,32` | `immediate must be an integer in the range [-32, 31]` |

模板在 `RISCVAsmParser::getCustomOperandDiag`（`:1503`），`Match_Invalid*`→`Range` 的映射从 `:1515` 起。

符号 fixup 则**默认放宽，不报错**：

```bash
printf '.text\nbne a0,a1,1f\n.skip 4096\n1:\n' > /tmp/long.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+c -filetype=obj /tmp/long.s -o /tmp/long.o
$T/bin/llvm-objdump -d /tmp/long.o
```

实测：

```
0: 00b50463   beq a0, a1, 0x8
4: 0040106f   j   0x1008
```

`fixupNeedsRelaxationAdvanced`（`:106`）返回 true，`getRelaxedOpcode` 换成 `PseudoLongBNE`，`expandLongCondBr`（`RISCVMCCodeEmitter.cpp:382`）展开成“反向分支 + `jal`”。要报错得用 `.option exact`：

```
/tmp/lbe.s:3:11: error: fixup value out of range
bne a0,a1,1f
          ^
```

`c.beqz` 同理报 `fixup value out of range`。对齐错误则来自 fixup 期：

```
error: fixup value must be 2-byte aligned
```

区分：`beq a0,a1,3` 是解析期 “multiple of 2 bytes”；`beq a0,a1,target`（target 在奇数偏移）是 fixup 期 “must be 2-byte aligned”。

---

## 6.10 源码落点

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrFormats.td`**（703 行）
  - 读哪里：`13-21` 立即数说明；`30-56` `InstFormat`；`121-168` `RISCVOpcode`/`OPC_*`；`373-575` 六个基类。
  - 为什么读：位切片的唯一事实来源。

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrFormatsC.td`**（392 行）
  - 读哪里：`22-150` 的 `RVInst16*`，重点 `29`（CR 5 位）、`45`（CI 5 位）、`87`（CL 3 位）。
  - 为什么读：解释哪些 RVC 限制 `x8`–`x15`。

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVBaseInfo.h`**（1,059 行）
  - 读哪里：`156-186` `InstFormat*`；`294` `getFormat()`；`705-713` `RISCVInsnOpcode`。
  - 为什么读：编码器靠 `TSFlags` 选 fixup。

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp`**（1,003 行）
  - 读哪里：`139` `addFixup`；`564-683` `encodeInstruction`；`687` `getMachineOpValue`；`748` `getImmOpValueAsrN`；`775-934` `getImmOpValue`。
  - 为什么读：fixup 的诞生地。

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVAsmBackend.cpp`**（1,032 行）
  - 读哪里：`58-104` `getFixupKindInfo`；`106-145` 放宽判定；`500-657` `adjustFixupValue`；`684-781` pcrel 配对；`936-965` `applyFixup`。
  - 为什么读：位重排、范围检查、写回字节。

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp`**（4,720 行）
  - 读哪里：`1503-1722` 立即数诊断；`3456-3475` `.option rvc/norvc`；`3732-3745` `emitToStreamer`。
  - 为什么读：解析期报错与压缩入口。

- **生成物**：`/home/ubuntu/llvm/builds/ct-instr-trunk/lib/Target/RISCV/RISCVGenMCCodeEmitter.inc`（15,937 行，`9`/`3216`）、`/home/ubuntu/llvm/builds/ct-instr-trunk/lib/Target/RISCV/RISCVGenCompressInstEmitter.inc`（7,418 行，`297`/`5110`）。

---

## 6.11 可跑实验

下面各块沿用实验 1 的 `T`；单独复制时先设 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

### 实验 1：手算对账
```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
printf 'add a0,a1,a2\naddi a1,a2,-1\nlui a3,0x12345\nauipc a4,0x12345\njal ra,8\n' > /tmp/enc.s
$T/bin/llvm-mc -triple=riscv64 -show-encoding /tmp/enc.s
python3 -c 'w=0x00c58533;print(hex(w&0x7f),(w>>7)&31,(w>>12)&7,(w>>15)&31,(w>>20)&31)'
```

### 实验 2：RVC 开关
```bash
printf 'addi a0,a0,1\n.option norvc\naddi a0,a0,1\n.option rvc\naddi a0,a0,2\n' > /tmp/opt.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+c  -show-encoding /tmp/opt.s
$T/bin/llvm-mc -triple=riscv64 -mattr=-c -show-encoding /tmp/opt.s
```
第二组最后一条仍是 2 字节，证明 `.option rvc` 直接改 STI feature。

### 实验 3：relocation 全表
```bash
cat > /tmp/rel.s <<'EOF'
.text
.globl foo
call foo
la a0, foo
.Lh:
auipc a2, %pcrel_hi(foo)
addi a2, a2, %pcrel_lo(.Lh)
EOF
$T/bin/llvm-mc -triple=riscv64 -mattr=+m,+a,+c,+relax -filetype=obj /tmp/rel.s -o /tmp/rel.o
$T/bin/llvm-readobj -r /tmp/rel.o
```
应看到 `CALL_PLT`、`PCREL_HI20/LO12_I` 及成对 `R_RISCV_RELAX`。

### 实验 4：越界分支
```bash
printf '.text\nbne a0,a1,1f\n.skip 4096\n1:\n' > /tmp/lb.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+c -filetype=obj /tmp/lb.s -o /tmp/lb.o && $T/bin/llvm-objdump -d /tmp/lb.o
sed '1i .option exact' /tmp/lb.s > /tmp/lbe.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+c -filetype=obj /tmp/lbe.s -o /dev/null
```
第一组 `beq`+`j`，第二组 `fixup value out of range`。

---

## 6.12 踩雷集锦

1. **照旧博客找 `RISCVMCExpr.h` 的 `VK_*`。** 本树 2025-06-15 已换成 `MCSpecifierExpr` + `RISCV::Specifier`，文件不存在。
2. **以为 `getBinaryCodeForInstr` 是手写的。** 它在 `RISCVGenMCCodeEmitter.inc`，手写的只有 `getImmOpValue`/`getMachineOpValue`。
3. **以为压缩在 `MCCodeEmitter` 里。** 压缩在 `emitToStreamer`/`EmitToStreamer` 完成；编码器只按 `Desc.getSize()` 写 2/4 字节。
4. **以为所有 RVC 都要求 `x8`–`x15`。** CL/CS/CA/CB/CIW 是 3 位；CR/CI 是 5 位，`c.addi t0` 合法。
5. **以为越界分支一定报错。** 默认会放宽成“反向分支 + `jal`”；只有 `.option exact` 才报 `fixup value out of range`。
6. **改 `RISCVFixupKinds.h` 顺序却不同步 `getFixupKindInfo`。** 表按枚举下标索引，错一位就会把 `%lo` 当分支；`static_assert` 只保证数量。
7. **`%pcrel_lo` 的 label 放错位置。** 必须指向 `%pcrel_hi` 所在的 `auipc`，否则 `could not find corresponding %pcrel_hi`。
8. **用 `+c` 却奇怪没有 `R_RISCV_RELAX`。** `llvm-mc` 默认不开 `FeatureRelax`，要显式 `+relax`；clang driver 默认值不同（driver 默认值本章未实测）。

---

## 6.13 本章重点整理

- `.td` 的 `Inst{...}` 是编码唯一事实来源；`imm12`/`imm20` 是编码位序列，不是逻辑值。
- 固定位被 TableGen 压成 `InstBits[]`；编码器用 `Value |= (op & mask) << shift` 补操作数。
- 立即数走 `MO.isImm()`；符号生成 `MCFixup`。分支/JAL 用 `getImmOpValueAsrN<1>` 先去掉 bit0。
- RVC 在 `MCInst` 层压缩；`compressInst`/`uncompressInst`/`getCompressedSize` 三段都来自 `RISCVGenCompressInstEmitter.inc`。
- 3 位字段（CL/CS/CA/CB/CIW）限制 `x8`–`x15`；5 位字段（CI/CR）不受限。
- `%hi` 的 `+0x800` 是低 12 位为负的补偿；`%lo` 只取低 12 位。
- fixup 是汇编器内部的洞，relocation 是留给 linker 的 `R_RISCV_*`；`%hi` 是 target fixup，`%got_pcrel_hi` 直接是 relocation。
- `%pcrel_lo(label)` 的 label 必须贴在 `auipc` 上；`la` 引用的是 `.Lpcrel_hi*`。
- 越界分支默认放宽；`.option exact` 同时关闭压缩和放宽。

---

## 6.14 自我检核

- [ ] 能默写 R/I/S/B/U/J 的字段切片，并解释 B 型为什么把 `imm[11]` 放在 bit 7
- [ ] 能手算 `add`/`addi`/`lui` 的 32 位值并用 `-show-encoding` 验证
- [ ] 能说清 `InstBits[ADD]=51` 与 `getBinaryCodeForInstr` 如何补 `rd/rs1/rs2`
- [ ] 能解释 `%hi(0x12fff)=0x13`、`%lo(0x12fff)=-1`
- [ ] 能说出 `fixup_riscv_hi20` 的 `offset/bits` 与 `applyFixup` 的写回方式
- [ ] 能解释 `%pcrel_lo` 的 label 为什么必须指向 `auipc`
- [ ] 能说出哪些 RVC 格式要求 `x8`–`x15`，各举一条实测指令
- [ ] 能解释 `.option exact` 与默认行为在越界分支上的差别

---

## 6.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/fixups-diagnostics.s`**
  - 读哪里：全文。为什么读：`out of range` / `2-byte aligned` 的最小复现。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/option-exact-long-jump-disable.s`**
  - 读哪里：`pass.s` 与 `fail.s`。为什么读：`.option exact` 关闭长分支放宽的官方测试。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/pcrel-fixups.s`**
  - 读哪里：每对 `%pcrel_hi`/`%pcrel_lo` 的 CHECK。为什么读：配对规则的正例。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/rv64c-valid.s`**
  - 读哪里：`c.addi`/`c.lw`/`c.sd` 的编码 CHECK。为什么读：RVC 回归基线。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/compressed-relocations.s`**
  - 读哪里：`R_RISCV_RVC_*` 的期望。为什么读：RVC 与 32 位 fixup 的差异。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoC.td`**
  - 读哪里：`697-745` 的 `CompressPat`。为什么读：压缩规则的源头。

---

→ 下一章：[Ch 07 — 汇编器、反汇编器与 InstPrinter](./07-asm-parser-disassembler-printer.md)
