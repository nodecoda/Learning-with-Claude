# Ch 02 — RISC-V 作为参照

> **目标**：用 RV64IMA/lp64 建立寄存器、ABI、指令格式与后端文件地图，能在一份 `llc`/`llvm-mc` 输出里指出每个字段来自哪一层。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。下文记 `B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`，`TREE=/home/ubuntu/llvm/trees/dsl-tablegen`。

## 为什么需要这个？

RISC-V 后端有几十个 `.td`、上百个 `.cpp`，直接从头读会把“ISA 事实”“LLVM 接口”“ABI 约定”三件事混在一起。做 bring-up 时我们真正需要的不是“读完整个后端”，而是三张小地图：

1. **ISA 词汇表**：`RV64IMA` 到底开了什么，`x0`–`x31` 各是什么角色，`lp64` 和 `lp64d` 差在哪，栈为什么必须 16 字节对齐，六种指令格式的 immediate 怎么摆。
2. **文件地图**：一个 target 的 `TargetMachine`、`Subtarget`、`InstrInfo`、`RegisterInfo`、`FrameLowering`、`ISelLowering`、`AsmPrinter`、`MCTargetDesc`、`AsmParser`、`Disassembler` 各在哪，谁依赖谁。
3. **取舍地图**：哪些东西 MiniRV 直接不做（RVV、压缩指令、TLS、浮点 ABI），为什么不做也能通过 MC-only 验收。

没有这三张图，常见后果是：把 `-mtriple=riscv64` 当成“打开了 RISC-V 全部扩展”；把 `a0` 和 `x10` 当成两个寄存器；看到 `addi sp, sp, -16` 以为只是“保存 16 字节”；或者把 `S` 格式的 immediate 当成连续 12 位去写编码器。

这一章只建立参照系，不重复 [`riscv_backend`](../riscv_backend/README.md) 的 ISA/ABI 细节。需要更深的指令语义、压缩指令、向量、TLS 时回查那门课。

## 先建立直觉

把“RISC-V 参照”想成三层投影：ISA 事实先落到 LLVM 的 TableGen 记录，再落到 MC/CodeGen 的 C++ 接口；MiniRV 只抄其中一条窄路径。

```
        ISA / psABI 事实                         LLVM 里的落点                         MiniRV 的取舍
 ┌──────────────────────────┐        ┌──────────────────────────────┐      ┌──────────────────────────┐
 │ RV64I 基础整数指令        │        │ RISCVInstrInfo*.td           │      │ auto-td 的 rv_i 语料      │
 │ RV64M 乘除                │  ───►  │ RISCVInstrInfoM.td           │ ───► │ 后续 commit 才加入 mul    │
 │ x0..x31 + ABI 角色        │        │ RISCVRegisterInfo.td         │      │ X0..X31，无 ABI 别名      │
 │ R/I/S/B/U/J 六种格式      │        │ RISCVInstrFormats.td         │      │ MiniRVInstrFormats.td     │
 │ lp64 / lp64d，栈 16 对齐  │        │ CC_RISCV、CSR_*、MCAsmInfo    │      │ 只做整数，栈仍 16 对齐    │
 └──────────────────────────┘        └──────────────────────────────┘      └──────────────────────────┘
```

关键直觉：**ISA 的一条事实，在 LLVM 里通常有“编码定义”和“语义接口”两个落点**。例如“乘法存在”既是 `RISCVInstrInfoM.td` 里的 `MUL` 记录（MC 层能编码/解码），也是 `RISCVTargetLowering::setOperationAction(ISD::MUL, ...)` 里的合法性决定（CodeGen 层敢不敢选它）。MiniRV 在 MC-only 阶段只有前者，后者要等 CodeGen 里程碑。

## 主线一：bring-up 需要的最小 RISC-V 词汇

### RV32 / RV64 与 RV64IMA

- **RV32 / RV64** 是 XLEN=32 或 64。XLEN 决定指针宽度、通用寄存器宽度、`long` 宽度和 `DataLayout` 里的 `p:64:64` 还是 `p:32:32`。
- **I** 是基础整数指令集：算术、逻辑、移位、load/store、分支、`jal`/`jalr`、`lui`/`auipc`。任何 RISC-V 目标都至少有 I。
- **M** 是整数乘除（`mul`、`div`、`rem` 及其 `w` 变体），**A** 是原子指令，**F/D** 是单/双精度浮点，**C** 是压缩指令，**V** 是向量。
- `RV64IMA` 读作“RV64 + I + M + A”，不含 F/D/C/V。MiniRV 当前语料是 **RV64IM**：有 I 和 M，没有 A/F/D/C/V。第一个 commit 更窄，只有 14 条 RV64I 指令（见 Ch 03）。

这里最容易犯的错，是把 `-mtriple=riscv64` 当成“RV64GC”。LLVM 的 triple 只选架构家族，扩展由 `-mattr` 控制；generic `riscv64` 默认只开 I。后文的 `mul` 实测会把这件事钉死。

### x0–x31 与 ABI 寄存器角色

RISC-V 有 32 个通用整数寄存器，架构名 `x0`–`x31`。psABI 又给它们起了别名，二者是**同一个物理寄存器**：

| 架构名 | ABI 名 | 角色 | bring-up 时需要知道的事 |
|---|---|---|---|
| `x0` | `zero` | 恒为零 | 写入被丢弃；`addi x0, x0, 0` 是空操作；很多伪指令靠它 |
| `x1` | `ra` | 返回地址 | 调用者用 `jalr x1, ...` 写入；被调用者要保存 |
| `x2` | `sp` | 栈指针 | 始终 16 字节对齐；prologue/epilogue 改它 |
| `x3` | `gp` | 全局指针 | 小型数据模型用；MiniRV 没有保留它 |
| `x4` | `tp` | 线程指针 | TLS 用；bring-up 通常先拒绝 TLS |
| `x5`–`x7` | `t0`–`t2` | 调用者保存临时寄存器 | 调用后值不保证 |
| `x8` | `s0`/`fp` | 被调用者保存 / 帧指针 | 有可变长栈或需要 frame address 时保留 |
| `x9` | `s1` | 被调用者保存 | 调用前后必须一致 |
| `x10`–`x17` | `a0`–`a7` | 参数/返回值 | 前 8 个整数参数；`a0`/`a1` 也是返回值 |
| `x18`–`x27` | `s2`–`s11` | 被调用者保存 | 调用前后必须一致 |
| `x28`–`x31` | `t3`–`t6` | 调用者保存临时寄存器 | 调用后值不保证 |

在两套名字之间切换时要小心：**汇编器可能只认其中一套**。RISC-V 后端默认打印 ABI 名（`a0`），MiniRV 的 printer 只打印架构名（`x10`），MiniRV 的 parser 也只接受 `x10`，不接受 `a0`。这不是“风格差异”，而是 `MCRegisterInfo` 的寄存器名表和 `InstPrinter::printRegName` 的实现差异。Ch 08 会展开。

对 bring-up 来说，先记住四个角色就够了：`x0` 零、`x1` 返回地址、`x2` 栈、`x10`–`x17` 参数/返回。其余可以等 ABI 章节再补。

### lp64 / lp64d 与 16 字节栈对齐

RISC-V psABI 的 64 位整数 ABI 叫 `lp64`：`long` 和指针都是 64 位，浮点参数**走整数寄存器**（软浮点风格）。`lp64d` 则在有 D 扩展时把 `double` 放进 `fa0`–`fa7`，`lp64f` 对应 F 扩展。

LLVM 不会在 triple 里写 ABI，而是由 target 根据 feature 和 module 的 `target-abi` 选择。实测能把两者的差别打出来：

```console
$ cat > /tmp/bringup-ev/fp.ll <<'EOF'
define double @fadd(double %a, double %b) {
  %r = fadd double %a, %b
  ret double %r
}
EOF
$ $B/llc -mtriple=riscv64 -O2 /tmp/bringup-ev/fp.ll -o - | sed -n '1,20p'
	.attribute	4, 16
	.attribute	5, "rv64i2p1"
	.file	"fp.ll"
	.text
	.globl	fadd                            # -- Begin function fadd
	.p2align	2
	.type	fadd,@function
fadd:                                   # @fadd
	.cfi_startproc
# %bb.0:
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	call	__adddf3
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
```

```console
$ $B/llc -mtriple=riscv64 -mattr=+d -O2 /tmp/bringup-ev/fp.ll -o - | sed -n '1,12p'
	.attribute	4, 16
	.attribute	5, "rv64i2p1_f2p2_d2p2_zicsr2p0"
	.file	"fp.ll"
	.text
	.globl	fadd                            # -- Begin function fadd
	.p2align	2
	.type	fadd,@function
fadd:                                   # @fadd
	.cfi_startproc
# %bb.0:
	fadd.d	fa0, fa0, fa1
	ret
```

两件事同时被验证：

- `.attribute 4, 16` 是 **stack alignment = 16**。即使指针只有 8 字节，ABI 也要求 `sp` 在任何调用边界上 16 字节对齐；所以 `addi sp, sp, -16` 而不是 `-8`。
- 没有 D 时，`double` 参数留在整数寄存器里，函数体调用 `__adddf3`；有 D 时，参数直接在 `fa0`/`fa1`，一条 `fadd.d` 就够。前者接近 `lp64`，后者是 `lp64d` 的行为。

bring-up 的第一版通常只做整数：ABI 先按 `lp64` 的整数子集走，浮点一律降级为 libcall 或直接拒绝。MiniRV 只做整数 ABI：浮点走 `__adddf3` 一类 soft-float 入口，不做 `lp64d`。

### R/I/S/B/U/J 六种指令格式

RISC-V 的 32 位指令只有六种基本格式。bring-up 不需要背下每个 bit，但必须知道 **immediate 不是永远连续的**，尤其 S/B/J：

| 格式 | 典型指令 | 字段布局（高位 → 低位） | immediate 特点 |
|---|---|---|---|
| R | `add`、`mul` | `funct7[31:25] rs2[24:20] rs1[19:15] funct3[14:12] rd[11:7] opcode[6:0]` | 无 immediate |
| I | `addi`、`jalr`、load | `imm[11:0] rs1[19:15] funct3[14:12] rd[11:7] opcode[6:0]` | 连续 12 位，符号扩展 |
| S | `sd`、`sw` | `imm[11:5] rs2[24:20] rs1[19:15] funct3[14:12] imm[4:0] opcode[6:0]` | 12 位被拆成两段 |
| B | `beq`、`bne` | `imm[12|10:5] rs2 rs1 funct3 imm[4:1|11] opcode` | 低 bit 恒 0，位序打散 |
| U | `lui`、`auipc` | `imm[31:12] rd[11:7] opcode[6:0]` | 高 20 位，低 12 位补 0 |
| J | `jal` | `imm[20|10:1|11|19:12] rd[11:7] opcode[6:0]` | 低 bit 恒 0，位序打散 |

对应到源码，RISC-V 用 `RVInstR`、`RVInstI`、`RVInstS`、`RVInstB`、`RVInstU`、`RVInstJ` 这些 TableGen 类描述位段（`RISCVInstrFormats.td`）。MiniRV 的 `MiniRVInstFormats.td` 只保留 `InstFormatR/I/S/B/U/J` 六个标签和 `MiniRVInst` 基类，真正的位段由 auto-td 从 opcode 数据库生成——这是“格式是接口，不是装饰”的典型例子。

用 `llvm-mc -show-encoding` 可以把六种格式一次打出来：

```console
$ printf 'add x10, x11, x12\naddi x10, x11, -1\nsd x12, 8(x11)\nbeq x10, x11, 8\nlui x10, 0x12345\njal x1, 16\n' \
    | $B/llvm-mc -triple=riscv64 -mattr=+m -show-encoding
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
	addi	a0, a1, -1                      # encoding: [0x13,0x85,0xf5,0xff]
	sd	a2, 8(a1)                       # encoding: [0x23,0xb4,0xc5,0x00]
	beq	a0, a1, 8                       # encoding: [0x63,0x04,0xb5,0x00]
	lui	a0, 74565                       # encoding: [0x37,0x55,0x34,0x12]
	jal	16                              # encoding: [0xef,0x00,0x00,0x01]
```

注意 `sd a2, 8(a1)` 的 `8` 被拆进 `[0x23,0xb4,...]` 的高位和低位；`beq` 的偏移 8 变成 `0x04`，因为最低位恒 0、编码时右移一位；`jal 16` 的编码也不是简单的 `0x10`。如果编码器把 S/B/J 当成连续 immediate，`llvm-mc` 能编过，反汇编就会错位——这是 bring-up 里最常见的“看起来能跑”的 bug。

## 主线二：RISC-V 后端的按层文件地图

下面这张表是本章最该背下来的东西。左边是层，中间是 RISC-V 的参照实现，右边是 MiniRV 在对应阶段的最小实现（没有的就写“MC-only 阶段缺席”）。

| 层 | RISC-V 文件与关键符号 | 作用 | MiniRV 对应 |
|---|---|---|---|
| TargetMachine | `RISCVTargetMachine.cpp/h`，`RISCVTargetMachine::getSubtargetImpl` | 创建 target、选 subtarget、设 TLOF/异常模型/ABI | `MiniRVTargetMachine.cpp/h` |
| Subtarget | `RISCVSubtarget.cpp/h`，`RISCVSubtarget` | feature/CPU 解析，TTI、InstrInfo、FrameLowering 的宿主 | `MiniRVSubtarget.cpp/h` |
| InstrInfo | `RISCVInstrInfo.cpp/h`，`RISCVInstrInfo::copyPhysReg` | 寄存器复制、spill/reload、分支分析、immediate 范围 | `MiniRVInstrInfo.cpp/h` |
| RegisterInfo | `RISCVRegisterInfo.cpp/h`，`getCalleeSavedRegs`、`getReservedRegs` | 寄存器类、保留寄存器、帧索引消除 | `MiniRVRegisterInfo.cpp/h` |
| FrameLowering | `RISCVFrameLowering.cpp/h`，`emitPrologue`、`getFrameIndexReference` | prologue/epilogue、栈对齐、大栈调整 | `MiniRVFrameLowering.cpp/h` |
| ISelLowering | `RISCVISelLowering.cpp/h`，`RISCVTargetLowering::LowerOperation`、`setOperationAction` | 合法化、调用约定、全局地址、libcall 决策 | `MiniRVISelLowering.cpp/h` |
| ISelDAGToDAG | `RISCVISelDAGToDAG.cpp`，`RISCVDAGToDAGISel::Select` | 复杂模式、寻址模式、手动选指 | `MiniRVISelDAGToDAG.cpp` |
| AsmPrinter | `RISCVAsmPrinter.cpp`，`RISCVAsmPrinter::emitInstruction` | MachineInstr → MCInst → 文本/对象 | `MiniRVAsmPrinter.cpp` |
| MCTargetDesc | `MCTargetDesc/RISCVMCTargetDesc.cpp`，`LLVMInitializeRISCVTargetMC` | 注册 MCAsmInfo/InstrInfo/RegInfo/SubtargetInfo/Printer/Emitter/Backend | `MiniRVMCTargetDesc.cpp` |
| AsmParser | `AsmParser/RISCVAsmParser.cpp`，`parseInstruction`、`MatchInstructionImpl` | 文本 → MCInst，操作数/立即数诊断 | `MiniRVAsmParser.cpp` |
| Disassembler | `Disassembler/RISCVDisassembler.cpp`，`getInstruction`、`decodeInstruction` | 字节 → MCInst | `MiniRVDisassembler.cpp` |
| CallingConv | `RISCVCallingConv.td`（`CSR_ILP32_LP64` 等）+ `RISCVISelLowering.cpp` 里的 `CC_RISCV` 自定义代码 | 参数/返回值位置、callee-saved 集合 | `MiniRVCallingConv.td`（后续 commit） |

读这张表的方式不是“从上到下读文件”，而是**顺着一次编译走**：

```
IR
 │  TargetMachine::getSubtargetImpl
 ▼
Subtarget ──► InstrInfo / RegisterInfo / FrameLowering / ISelLowering
 │
 ▼
ISelDAGToDAG::Select ──► MachineInstr
 │
 ▼
AsmPrinter::emitInstruction ──► MCInst
 │
 ├──► MCCodeEmitter ──► bytes ──► ELFObjectWriter ──► .o
 └──► InstPrinter   ──► .s
```

MC 层的 AsmParser/Disassembler 不在 `llc` 的这条主线上，它们是 `llvm-mc` 的入口。Ch 03 会看到：MiniRV 的第一个 commit 只把下面这条横线接好，上面整条 `IR → MachineInstr` 都还不存在。

## 实测：同一个 IR，三种后端配置

先固定输入：

```console
$ cat > /tmp/bringup-ev/mul.ll <<'EOF'
define i64 @muladd(i64 %a, i64 %b, i64 %c) {
entry:
  %m = mul i64 %a, %b
  %r = add i64 %m, %c
  ret i64 %r
}
EOF
```

### 1. generic `riscv64`：没有 M，乘法变 `__muldi3`

```console
$ $B/llc -mtriple=riscv64 -O2 /tmp/bringup-ev/mul.ll -o - | sed -n '1,31p'
	.attribute	4, 16
	.attribute	5, "rv64i2p1"
	.file	"mul.ll"
	.text
	.globl	muladd                          # -- Begin function muladd
	.p2align	2
	.type	muladd,@function
muladd:                                 # @muladd
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	sd	s0, 0(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	.cfi_offset s0, -16
	mv	s0, a2
	call	__muldi3
	add	a0, a0, s0
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	ld	s0, 0(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	.cfi_restore s0
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
.Lfunc_end0:
	.size	muladd, .Lfunc_end0-muladd
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

`.attribute 5` 是 `rv64i2p1`，没有 `m2p0`。合法化阶段发现 `ISD::MUL` 不合法，把它降级成 `__muldi3` 调用；因为要跨调用保存 `a2`，还多用了 `s0` 和 16 字节栈。这是“triple 不等于 feature”的直接证据。

### 2. `riscv64 -mattr=+m`：乘法变成一条 `mul`

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/bringup-ev/mul.ll -o - | sed -n '1,18p'
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
	.file	"mul.ll"
	.text
	.globl	muladd                          # -- Begin function muladd
	.p2align	2
	.type	muladd,@function
muladd:                                 # @muladd
	.cfi_startproc
# %bb.0:                                # %entry
	mul	a0, a0, a1
	add	a0, a0, a2
	ret
.Lfunc_end0:
	.size	muladd, .Lfunc_end0-muladd
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

### 3. `minirv64`：乘法是一条 `mul`，寄存器用架构名

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/bringup-ev/mul.ll -o - | sed -n '1,15p'
	.file	"mul.ll"
	.text
	.globl	muladd                          # -- Begin function muladd
	.type	muladd,@function
muladd:                                 # @muladd
	.cfi_startproc
# %bb.0:                                # %entry
	mul	x3, x10, x11
	add	x10, x3, x12
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	muladd, .Lfunc_end0-muladd
	.cfi_endproc
                                        # -- End function
	.section	".note.GNU-stack","",@progbits
```

三个输出放在一起，能读出四件事：

1. **`minirv64` 的 `mul` 不是“LLVM 自动给的”**。当前 MiniRV 的 auto-td 语料包含 `rv-m/mul.yaml`，所以 `MUL` 从第一天起就是合法指令；而 generic `riscv64` 只有 I，必须 `-mattr=+m` 才合法。
2. **`minirv64` 没有 `+m` 这个开关**。它的 feature 集合是空的（`Proc<"generic", []>`），指令可用性不由 feature 控制，而由语料里有没有这条记录控制。这是一种“静态子集”设计，适合 bring-up，不适合生产后端。
3. **寄存器命名不同**。RISC-V printer 打印 `a0`/`a1`/`a2`，MiniRV 打印 `x10`/`x11`/`x12`；`mul x3, ...` 还说明 MiniRV 没保留 `gp`（`x3`），它只借了 RISC-V 的编码，没有完整 ABI。
4. **返回序列不同**。RISC-V 有 `ret` 伪指令，MiniRV 只认 `jalr x0, 0(x1)`。Ch 03 的 `llvm-mc` 测试会再次证明：MiniRV 的汇编器不认 `ret`。

### 4. `llvm-mc`：同一条 `add`/`mul` 的字节对比

```console
$ printf 'add x10, x11, x12\nmul x10, x11, x12\n' | $B/llvm-mc -triple=riscv64 -mattr=+m -show-encoding
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
	mul	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x02]

$ printf 'add x10, x11, x12\nmul x10, x11, x12\n' | $B/llvm-mc -triple=minirv64 -show-encoding
	add	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x00]
	mul	x10, x11, x12                   # encoding: [0x33,0x85,0xc5,0x02]
```

字节完全相同：`add` 是 `0x00c58533`，`mul` 是 `0x02c58533`。差别只在助记符后的寄存器名和 `funct7`：

- `add` 的 `funct7 = 0000000`，所以最高字节是 `0x00`。
- `mul` 的 `funct7 = 0000001`，所以最高字节变成 `0x02`（bit 25 置位）。

这也说明 MiniRV 复用 RISC-V 编码数据库的代价：**编码必须逐位一致**，否则 `EM_RISCV` 的对象在真正的 RISC-V 工具链里就是错的。

再看没有 M 的 generic `riscv64`：

```console
$ printf 'add x10, x11, x12\nmul x10, x11, x12\n' | $B/llvm-mc -triple=riscv64 -show-encoding 2>&1
<stdin>:2:1: error: instruction requires the following: 'Zmmul' (Integer Multiplication)
mul x10, x11, x12
^
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
```

注意错误信息说的是 **`Zmmul`**，不是 `M`。RISC-V 把“只乘不除”拆成了 `Zmmul` 子扩展；`+m` 会同时带来 `m` 和 `zmmul`。这就是为什么 `.attribute 5` 里同时出现 `m2p0` 和 `zmmul1p0`。bring-up 时如果只照抄 feature 名，很容易漏掉这种“子扩展隐含关系”。

## 主线三：哪些东西 bring-up 不需要

RISC-V 后端的大部分代码量在 bring-up 第一阶段都用不上。明确“不做”比“以后再说”更重要：

| 能力 | RISC-V 里的位置 | 为什么 MC-only 不需要 | MiniRV 策略 |
|---|---|---|---|
| RVV / 向量 | `RISCVInstrInfoV*.td`、`RISCVInsertVSETVLI.cpp` | 没有向量寄存器类和 ISel，MC 层也不需要 | 不生成、不解析，后续显式拒绝 |
| 压缩指令 C | `RISCVInstrInfoC.td`、`RISCVInstrFormatsC.td` | 变长指令会改变 decoder 的 `Size` 和对齐假设 | 只做 4 字节定长 |
| 浮点 F/D | `RISCVInstrInfoF/D.td`、`lp64d` ABI | MC-only 不跑调用约定，不需要 `fa*` | 整数寄存器 + soft-float libcall |
| 原子 A | `RISCVInstrInfoA.td`、`RISCVExpandPseudoAtomics.cpp` | 需要内存模型和 libatomic 协作 | 后续降级到 `__atomic_*` |
| TLS | `RISCVISelLowering` 的 TLS 路径、`tp` 寄存器 | 需要 linker relaxation 和线程运行时 | 直接诊断拒绝 |
| 异常/展开 | `RISCVFrameLowering`、`.cfi_*`、`__unwind` | MC 层可以只发 `.cfi` 指令，不生成 EH | 不做 invoke/landingpad |
| 链接 relaxation | `RISCVAsmBackend`、`RISCVELFStreamer` | 需要 LLD 配合和 relocation 语义 | 不做，保持固定编码 |

这些不是“永远不做”，而是“不在 milestone 0 做”。每一条都对应后面某一章；现在只需要知道它们**不在 MC-only 的验收范围里**。RISC-V 的完整实现见 [`riscv_backend`](../riscv_backend/README.md)，本课不复制。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| Triple | `llvm/include/llvm/TargetParser/Triple.h`、`llvm/lib/TargetParser/Triple.cpp` | `Triple::riscv64`、`isRISCV()`、`getArchTypeName` | 一个 arch 名字要穿过多少 switch |
| 寄存器 | `llvm/lib/Target/RISCV/RISCVRegisterInfo.td` | `GPR`、`X0`–`X31`、`F0_D`–`F31_D` | 寄存器类与 ABI 别名 |
| 指令格式 | `llvm/lib/Target/RISCV/RISCVInstrFormats.td` | `RVInstR`、`RVInstI`、`RVInstS`、`RVInstB`、`RVInstU`、`RVInstJ` | 六种位段 |
| 指令定义 | `llvm/lib/Target/RISCV/RISCVInstrInfo*.td` | `ADD`、`MUL`、`ADDI`、`LD`、`BEQ`、`JAL` | 编码、pattern、feature 谓词 |
| 调用约定 | `llvm/lib/Target/RISCV/RISCVCallingConv.td`、`RISCVISelLowering.cpp` | `CSR_ILP32_LP64`、`CC_RISCV` | 参数位置、callee-saved、栈对齐 |
| MC 注册 | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCTargetDesc.cpp` | `LLVMInitializeRISCVTargetMC` | 七种 `RegisterMC*` |
| 汇编器 | `llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp` | `parseInstruction`、`MatchInstructionImpl` | 操作数类别与诊断 |
| 反汇编器 | `llvm/lib/Target/RISCV/Disassembler/RISCVDisassembler.cpp` | `getInstruction`、`decodeInstruction` | 变长指令与 decoder 表 |

## 反直觉与踩雷

1. **错误直觉**：`-mtriple=riscv64` 就是一台完整的 64 位 RISC-V。
   **正确认识**：triple 只选架构家族，默认只有 RV64I；`mul` 需要 `-mattr=+m`，否则合法化会调用 `__muldi3`。先看 `.attribute 5`，再看代码。

2. **错误直觉**：`a0` 和 `x10` 是两个不同的寄存器，汇编器应该都认。
   **正确认识**：它们是同一个物理寄存器的两个名字。RISC-V 后端默认打印 ABI 名；MiniRV 只打印/接受 `x10`。跨 target 复制汇编时，寄存器名可能先炸，而不是编码先炸。

3. **错误直觉**：栈对齐等于指针对齐，64 位就是 8 字节。
   **正确认识**：psABI 要求 `sp` 在调用边界 16 字节对齐；`.attribute 4, 16` 和 `addi sp, sp, -16` 都在说这件事。把 prologue 的调整量写成 8，会让 `sd`/`ld` 的对齐假设和 varargs 一起出问题。

4. **错误直觉**：指令格式只是 TableGen 的写法，编码器可以“按顺序塞 immediate”。
   **正确认识**：S/B/J 的 immediate 是打散的，B/J 的最低位还隐含为 0。编码器和解码器必须用同一套位段；否则 `llvm-mc` 能编、`llvm-objdump` 会解错。

5. **错误直觉**：`lp64` 和 `lp64d` 只是命令行字符串。
   **正确认识**：它决定 `double` 是进 `fa0` 还是整数寄存器、对象 `e_flags` 怎么写、链接时是否 ABI 兼容。实测里 `fadd.d fa0, fa0, fa1` 与 `call __adddf3` 是两种 ABI 行为，不只是优化差异。

## 进阶：再往深一层

- **用 `llvm-mc` 当编码 oracle**。任何“这条指令到底怎么编”的问题，先跑 `-show-encoding`，再回头读 `.td`。这比从 TableGen 反推快得多。
- **用 `.attribute` 验证 feature**。`llc` 输出顶部的 `.attribute 5` 是 target 对自身 ISA 的自述；如果它和你以为的 feature 不一致，后面的代码生成一定有问题。
- **区分“MC 能编”与“CodeGen 敢选”**。`llvm-mc -triple=riscv64 -mattr=+m` 能编 `mul`，不代表 `llc -mtriple=riscv64` 会选 `mul`；前者只看 MC 层，后者还要过合法化和 ISel。
- **把 ABI 当成接口而不是文档**。`RISCVCallingConv.td` 里的 `CSR_ILP32_LP64` 和 `RISCVISelLowering.cpp` 里的 `CC_RISCV` 是两份互补的实现：一份给寄存器分配器，一份给调用点 lowering。只改一份会出现“callee 保存了但 caller 没预期”的错位。

## 本章重点整理

- RV64IMA = RV64 + I + M + A；`riscv64` triple 默认只有 I，M 要 `-mattr=+m`。MiniRV 当前是 RV64IM，第一个 commit 只有 RV64I。
- `x0`–`x31` 是架构名，`a0`–`a7`/`ra`/`sp`/`s0` 是 ABI 名；同一寄存器，两套名字，汇编器可能只认一套。
- `lp64` 的浮点参数走整数寄存器，`lp64d` 走 `fa0`–`fa7`；栈始终 16 字节对齐。
- 六种格式里，S/B/J 的 immediate 不连续，B/J 最低位隐含 0；编码器和解码器必须共享同一套位段。
- RISC-V 后端按层分成 TargetMachine/Subtarget、InstrInfo/RegisterInfo/FrameLowering、ISelLowering/ISelDAGToDAG、AsmPrinter、MCTargetDesc、AsmParser、Disassembler、CallingConv。
- MC-only 阶段只需要“格式 + 编码 + 打印 + 解码”，不需要 RVV/C/F/D/A/TLS/EH。

## 自我检核

- [ ] 不看笔记，说出 `x1`、`x2`、`x8`、`x10`–`x17` 的 ABI 角色，以及哪些是 callee-saved。
- [ ] 解释为什么 `llc -mtriple=riscv64` 对 `mul` 生成 `__muldi3`，而 `-mattr=+m` 只生成一条 `mul`；`.attribute 5` 分别是什么。
- [ ] 给出 `sd x12, 8(x11)` 的 32 位编码，并指出 8 被拆到哪两个位段。
- [ ] 解释 `lp64` 与 `lp64d` 在 `double fadd(double, double)` 上的寄存器差异。
- [ ] 说出 `RISCVISelLowering` 和 `RISCVISelDAGToDAG` 的分工，并各举一个符号。
- [ ] 说出 MC-only 阶段可以不做、但生产后端必须做的三件事。

## 延伸阅读

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V 非 ISA 工作组
  - **读哪里**：`riscv-elf.adoc` 的 “Register Usage”“Stack Alignment”“Calling Convention” 三节。
  - **能学到什么**：`lp64`/`lp64d` 的精确定义、callee-saved 集合、16 字节栈对齐的来由。
  - **前提**：知道 32 个通用寄存器；不需要读完整份 ABI。

- **[RISC-V ISA Manual](https://riscv.org/technical/specifications/)** — RISC-V International
  - **读哪里**：Volume I 的 “RV64I Base Integer Instruction Set” 和 “RISC-V Instruction Formats”。
  - **能学到什么**：R/I/S/B/U/J 的位段、immediate 的符号扩展规则、`x0` 的硬连线语义。
  - **前提**：能读指令编码表；配合 `llvm-mc -show-encoding` 对照。

- **[LLVM MC Layer](https://llvm.org/docs/CommandGuide/llvm-mc.html)** — LLVM 官方
  - **读哪里**：`-show-encoding`、`-disassemble`、`-filetype=obj` 三个选项的说明。
  - **能学到什么**：MC 层能独立于 CodeGen 工作，以及“汇编文本/字节/对象”三种表示如何互转。
  - **前提**：跑过一次 `llvm-mc`。

- **[`riscv_backend`](../riscv_backend/README.md)** — 本仓库课程
  - **读哪里**：RISC-V 的 MC、ISel、ABI、TLS、压缩指令章节。
  - **能学到什么**：本章刻意略过的完整实现细节。
  - **前提**：先完成本章的词汇表和文件地图。

→ [Ch 03 里程碑 0：MC-only target](./03-mc-only-milestone.md)
