# 02 ISA 建模：XLEN、扩展、寄存器堆与六种格式

> 目标：把 RISC-V 的 ISA 约束拆成四层可以分别验证的模型：XLEN 决定字宽与合法类型；扩展决定可用指令与隐含依赖；寄存器堆决定寄存器类与 ABI 名称；指令格式决定位域与编码。读完本章，你能从一条 `.attribute 5` 字符串和一条 32 位编码，反推出后端在 Legalizer、ABI、编码三个方向上的行为。

> 环境：LLVM 23.1.4 预编译版；同版本源码在 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。命令在仓库根目录 `D:\Learning-with-Claude` 下执行。

## 为什么

后端的很多“选指不对”并不是选指算法的问题，而是 ISA 模型没有对齐。典型场景：

- 以为 `riscv64` 的 `i64` 乘法一定是一条 `mul`，结果没开 M，看到 `call __muldi3`。
- 以为 `+m` 只影响乘法，结果 `.attribute 5` 里多出 `zmmul1p0`，不知道这是 M 的隐含依赖。
- 以为 `x10` 和 `a0` 是两个寄存器，结果它们在寄存器类里是同一个寄存器的两个名字。
- 以为 `jal` 的立即数是连续的 21 位，自己按位拼接，得到错误的偏移。
- 以为 `riscv64be` 只是把汇编助记符换个方向，结果对象文件的数据字节序完全相反。

这些问题的共同点是：把 ISA 的“逻辑描述”和“编码描述”混在了一起。RISC-V 的 ISA 手册用逻辑值描述立即数，编码时却把立即数打散到不连续的位段；ABI 用 `a0`/`s0` 这样的名字描述寄存器，机器码里只有 5 位编号；扩展用 `+m` 这样的开关描述，ELF 属性里却要展开成带版本号的完整字符串。

本章按四层建模：XLEN、扩展、寄存器堆、指令格式。每一层都给真实命令或同版本源码锚点。最后一节把这些约束连到 Legalizer、ABI 和编码三条下游通路。

## 主线

### 2.1 RV32/RV64 与 XLEN

`riscv32` 和 `riscv64` 的第一差别是 XLEN：通用寄存器的位宽。XLEN 不是“指针大小”这么简单，它同时决定：

- 合法整数类型的宽度。`XLenVT` 在 `RISCVTargetLowering` 里被注册为 `GPR` 的寄存器类类型。
- 指针宽度与数据布局。64 位 triple 的 datalayout 里 `p:64:64`，32 位是 `p:32:32`。
- ABI 名称。RV32 用 `ilp32` 系列，RV64 用 `lp64` 系列。
- 部分指令是否存在。`ADDW`/`ADDIW`/`SLLW` 等 W 指令只在 RV64 上存在，RV32 没有对应的“窄于 XLEN”的算术。
- 乘除法的展开策略。RV32 上的 `i64` 乘法不是一条指令，而是多条 32 位乘加。

最后一条可以用实测确认。输入：

```llvm
target triple = "riscv32-unknown-elf"
define i32 @m32(i32 %a, i32 %b) {
  %m = mul i32 %a, %b
  %r = add i32 %m, 7
  ret i32 %r
}
define i64 @m64(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  ret i64 %m
}
```

命令：

```powershell
llc -mtriple=riscv32 -mattr=+m -O2 rv32.ll -o -
```

真实输出（节选，省略了 CFI、`.size` 与 `.section` 等指令）：

```asm
	.attribute	4, 16
	.attribute	5, "rv32i2p1_m2p0_zmmul1p0"
	.file	"rv32.ll"
	.text
	.globl	m32
m32:
	mul	a0, a0, a1
	addi	a0, a0, 7
	ret

	.globl	m64
m64:
	mul	a3, a0, a3
	mulhu	a4, a0, a2
	mul	a1, a1, a2
	mul	a0, a0, a2
	add	a3, a4, a3
	add	a1, a3, a1
	ret
```

`m32` 是一条 `mul`；`m64` 被拆成四条乘法和两条加法。这不是后端“不会优化”，而是 XLEN=32 的硬约束：一条 32×32→64 的乘法只能给出低 32 位或高 32 位，完整的 64 位乘积需要组合 `mul` 与 `mulhu`。

`.attribute 5` 也随 triple 改变：`rv32i2p1_m2p0_zmmul1p0`。栈对齐仍然是 16 字节（`.attribute 4, 16`），这说明 XLEN 改变字宽，但不改变 psABI 的栈对齐要求。

### 2.2 base + 扩展与 Feature 定义

RISC-V 的 ISA 字符串是“基础 ISA + 扩展”的拼接。基础 ISA 是 `rv32i` 或 `rv64i`（也可以选 E 等变体），扩展用单字母（M/A/F/D/C/V）或 `Z` 前缀的多字母名字（Zicsr、Zmmul、Zca、Zcd 等）。每个扩展带主版本和小版本，例如 `m2p0`、`a2p1`、`f2p2`。

扩展定义在 `RISCVFeatures.td`。真实节选：

```tablegen
def FeatureStdExtZmmul
    : RISCVExtension<1, 0, "Integer Multiplication">,
      RISCVExtensionBitmask<1, 12>;

def FeatureStdExtM
    : RISCVExtension<2, 0, "Integer Multiplication and Division",
                     [FeatureStdExtZmmul]>,
      RISCVExtensionBitmask<0, 12>;
def HasStdExtM : Predicate<"Subtarget->hasStdExtM()">,
                 AssemblerPredicate<(all_of FeatureStdExtM),
                 "'M' (Integer Multiplication and Division)">;

def FeatureStdExtA
    : RISCVExtension<2, 1, "Atomic Instructions",
                     [FeatureStdExtZaamo, FeatureStdExtZalrsc]>,
      RISCVExtensionBitmask<0, 0>;

def FeatureStdExtF
    : RISCVExtension<2, 2, "Single-Precision Floating-Point",
                     [FeatureStdExtZicsr]>,
      RISCVExtensionBitmask<0, 5>;

def FeatureStdExtD
    : RISCVExtension<2, 2, "Double-Precision Floating-Point",
                     [FeatureStdExtF]>,
      RISCVExtensionBitmask<0, 3>;

def FeatureStdExtC
    : RISCVExtension<2, 0, "Compressed Instructions", [FeatureStdExtZca]>,
      RISCVExtensionBitmask<0, 2>;

def FeatureStdExtV
    : RISCVExtension<1, 0,
                     "Vector Extension for Application Processors",
                     [FeatureStdExtZvl128b, FeatureStdExtZve64d]>,
      RISCVExtensionBitmask<0, 21>;
```

这段定义里有四种信息：

1. **版本**：`RISCVExtension<major, minor, description, deps>`。M 是 2.0，A 是 2.1，F/D 是 2.2，C 是 2.0，V 是 1.0。
2. **依赖**：M 依赖 Zmmul；A 依赖 Zaamo 与 Zalrsc；F 依赖 Zicsr；D 依赖 F；C 依赖 Zca；V 依赖 Zvl128b 与 Zve64d。
3. **位掩码**：`RISCVExtensionBitmask<word, bit>` 把扩展映射到特性位集合里的某一位。`ParseSubtargetFeatures` 解析字符串后写这些位。
4. **谓词**：`Predicate<"Subtarget->hasStdExtM()">` 给 C++ 用；`AssemblerPredicate<...>` 给汇编器和 TableGen 生成的匹配器用。

依赖关系会直接反映到 `.attribute 5`。实测：

```text
-mattr=+m
  .attribute 5, "rv64i2p1_m2p0_zmmul1p0"

-mattr=+m,+a,+f,+d
  .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0"

-mattr=+m,+a,+f,+d,+c
  .attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0_zmmul1p0_zaamo1p0_zalrsc1p0_zca1p0_zcd1p0"
```

`+m` 带出 `zmmul1p0`，`+a` 带出 `zaamo1p0`/`zalrsc1p0`，`+f` 带出 `zicsr2p0`，`+c` 与 `+d` 组合带出 `zca1p0`/`zcd1p0`。这些不是命令行的重复输入，而是依赖闭包。

`-mattr` 的语义可以压成三条：

- `+x` 打开扩展 `x`，并递归打开它的依赖。
- `-x` 关闭扩展 `x`；关闭后，依赖它的指令不能再选。
- 未知名字只产生警告并忽略，不改变退出码。

第三条已经在前一章实测过：`-mattr=+notafeature` 输出 `'+notafeature' is not a recognized feature for this target (ignoring feature)`，退出码仍是 0。所以验证特性是否生效，要看指令，不要看退出码。

### 2.3 寄存器堆与 ABI 名

RISC-V 有三套寄存器堆：通用寄存器 `x0`-`x31`、浮点寄存器 `f0`-`f31`、向量寄存器 `v0`-`v31`。机器码里只有 5 位编号；ABI 名是汇编层的别名。`RISCVRegisterInfo.td` 用 `RegAltNameIndices = [ABIRegAltName]` 和 `RISCVReg<..., ["a0"]>` 这样的写法建立别名。

通用寄存器表：

| 编号 | ABI 名 | 角色 |
| --- | --- | --- |
| `x0` | `zero` | 常量 0；写入被丢弃 |
| `x1` | `ra` | 返回地址 |
| `x2` | `sp` | 栈指针 |
| `x3` | `gp` | 全局指针 |
| `x4` | `tp` | 线程指针 |
| `x5` | `t0` | 临时寄存器 |
| `x6` | `t1` | 临时寄存器 |
| `x7` | `t2` | 临时寄存器 |
| `x8` | `s0` / `fp` | callee-saved；可作帧指针 |
| `x9` | `s1` | callee-saved |
| `x10` | `a0` | 参数 / 返回值 |
| `x11` | `a1` | 参数 / 返回值 |
| `x12` | `a2` | 参数 |
| `x13` | `a3` | 参数 |
| `x14` | `a4` | 参数 |
| `x15` | `a5` | 参数 |
| `x16` | `a6` | 参数 |
| `x17` | `a7` | 参数 |
| `x18` | `s2` | callee-saved |
| `x19` | `s3` | callee-saved |
| `x20` | `s4` | callee-saved |
| `x21` | `s5` | callee-saved |
| `x22` | `s6` | callee-saved |
| `x23` | `s7` | callee-saved |
| `x24` | `s8` | callee-saved |
| `x25` | `s9` | callee-saved |
| `x26` | `s10` | callee-saved |
| `x27` | `s11` | callee-saved |
| `x28` | `t3` | 临时寄存器 |
| `x29` | `t4` | 临时寄存器 |
| `x30` | `t5` | 临时寄存器 |
| `x31` | `t6` | 临时寄存器 |

浮点寄存器表：

| 编号范围 | ABI 名 | 角色 |
| --- | --- | --- |
| `f0`-`f7` | `ft0`-`ft7` | 临时寄存器 |
| `f8`-`f9` | `fs0`-`fs1` | callee-saved |
| `f10`-`f11` | `fa0`-`fa1` | 参数 / 返回值 |
| `f12`-`f17` | `fa2`-`fa7` | 参数 |
| `f18`-`f27` | `fs2`-`fs11` | callee-saved |
| `f28`-`f31` | `ft8`-`ft11` | 临时寄存器 |

向量寄存器没有单独的 ABI 别名，汇编层直接用 `v0`-`v31`。按 23.1.4 的 `RISCVCallingConv.td`，`CSR_V` 包含 `V1`-`V7`、`V24`-`V31` 以及若干成对/四倍/八倍寄存器组，因此这些寄存器按 callee-saved 处理；`V0` 与 `V8`-`V23` 不在 `CSR_V` 中，按 caller-saved 处理。`V0` 还承担 mask 角色，`V8`-`V23` 是向量参数的传递窗口。

寄存器类决定分配器的选择空间。常见的有：

- `GPR`：完整通用寄存器类。
- `GPRNoX0`：去掉 `x0`，因为 `x0` 不能作为普通目的寄存器。
- `GPRC`：可编码进压缩指令的寄存器子集，通常是 `x8`-`x15`。
- `FPR64`：`f64` 值的浮点寄存器类。
- `VR` / `VRM2` / `VRM4` / `VRM8`：不同 LMUL 的向量寄存器类。

`RISCVRegisterInfo.td` 里的 `CostPerUse` 会影响贪心分配器：不在 `GPRC` 里的寄存器成本更高，因为用了它们就失去 16 位压缩的机会。这条成本模型会在第 11 章展开。

### 2.4 六种格式与真实位域

RISC-V 的 32 位指令有六种基本格式：R/I/S/B/U/J。`RISCVInstrFormats.td` 用 TableGen 类描述它们，位段与 ISA 手册一致。核心映射如下：

| 格式 | 位段 |
| --- | --- |
| R | `funct7[31:25]`、`rs2[24:20]`、`rs1[19:15]`、`funct3[14:12]`、`rd[11:7]`、`opcode[6:0]` |
| I | `imm[11:0][31:20]`、`rs1[19:15]`、`funct3[14:12]`、`rd[11:7]`、`opcode[6:0]` |
| S | `imm[11:5][31:25]`、`rs2[24:20]`、`rs1[19:15]`、`funct3[14:12]`、`imm[4:0][11:7]`、`opcode[6:0]` |
| B | `imm[12]` 在 bit31；`imm[10:5]` 在 bits30-25；`rs2`、`rs1`、`funct3`；`imm[4:1]` 在 bits11-8；`imm[11]` 在 bit7；`opcode` |
| U | `imm[31:12]` 在 bits31-12；`rd[11:7]`；`opcode[6:0]` |
| J | `imm[20]` 在 bit31；`imm[10:1]` 在 bits30-21；`imm[11]` 在 bit20；`imm[19:12]` 在 bits19-12；`rd`；`opcode` |

TableGen 里的真实类名是 `RVInstR`、`RVInstI`、`RVInstS`、`RVInstB`、`RVInstU`、`RVInstJ`。`RVInstCommon` 把格式值写进 `TSFlags{4-0}`，对应的 `InstFormat` 编号是 R=1、I=3、S=4、B=5、U=6、J=7。MC 层的 `RISCVMCCodeEmitter` 会读这个字段，决定 fixup 的形式。

`RISCVInstrFormats.td` 顶部有一段重要注释：ISA 手册用逻辑值描述立即数，但格式类里的 `imm` 参数表示“要插入指令的位序列”。例如 JAL 的逻辑立即数是 21 位、最低位恒为 0；格式类却写成 `imm20`，因为编码时只保留打散后的 20 位。这个差异是手写解码时最常见的错误来源。

用真实编码走一遍。`llvm-mc` 命令（不启用 `+c`，强制 4 字节形式）：

```powershell
$s = Join-Path $env:TEMP "enc.s"
@(
  "addi a0, a1, 42"
  "add a0, a1, a2"
  "sub a0, a1, a2"
  "sw a0, 12(sp)"
  "ld a0, -8(s0)"
  "beq a0, a1, 16"
  "lui a0, 0x12345"
  "jal ra, 2048"
) -join "`n" | Set-Content -LiteralPath $s -Encoding ASCII
llvm-mc -triple=riscv64 -mattr=+m,+a,+f,+d -show-encoding $s
```

真实输出：

```text
	addi	a0, a1, 42                      # encoding: [0x13,0x85,0xa5,0x02]
	add	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x00]
	sub	a0, a1, a2                      # encoding: [0x33,0x85,0xc5,0x40]
	sw	a0, 12(sp)                      # encoding: [0x23,0x26,0xa1,0x00]
	ld	a0, -8(s0)                      # encoding: [0x03,0x35,0x84,0xff]
	beq	a0, a1, 16                      # encoding: [0x63,0x08,0xb5,0x00]
	lui	a0, 74565                       # encoding: [0x37,0x55,0x34,0x12]
	jal	2048                            # encoding: [0xef,0x00,0x10,0x00]
```

`llvm-mc` 打印的是小端字节序列。把字节按从低到高拼回 32 位字，再按上表拆字段：

| 指令 | 32 位字 | 字段解码 |
| --- | --- | --- |
| `addi a0,a1,42` | `0x02A58513` | opcode=`0x13`，rd=`x10`，funct3=`0`，rs1=`x11`，imm=`42` |
| `add a0,a1,a2` | `0x00C58533` | opcode=`0x33`，funct7=`0x00`，rs2=`x12`，rs1=`x11`，funct3=`0`，rd=`x10` |
| `sub a0,a1,a2` | `0x40C58533` | opcode=`0x33`，funct7=`0x20`，其余同 `add` |
| `sw a0,12(sp)` | `0x00A12623` | opcode=`0x23`，imm[11:5]=`0`，rs2=`x10`，rs1=`x2`，funct3=`2`，imm[4:0]=`12` |
| `ld a0,-8(s0)` | `0xFF843503` | opcode=`0x03`，imm=`0xFF8`（有符号 -8），rs1=`x8`，funct3=`3`，rd=`x10` |
| `beq a0,a1,16` | `0x00B50863` | opcode=`0x63`，imm[12]=`0`，imm[10:5]=`0`，rs2=`x11`，rs1=`x10`，funct3=`0`，imm[4:1]=`8`，imm[11]=`0` |
| `lui a0,0x12345` | `0x12345537` | opcode=`0x37`，imm[31:12]=`0x12345`，rd=`x10` |
| `jal ra,2048` | `0x001000EF` | opcode=`0x6F`，imm[20]=`0`，imm[10:1]=`0`，imm[11]=`1`，imm[19:12]=`0`，rd=`x1` |

几个容易读错的点：

- `sw` 的 `rd` 字段不存在。S 格式把 bit11-7 用作了 `imm[4:0]`，所以按 R 格式去读会把立即数当成目的寄存器。
- `beq` 的立即数不是连续的。`imm[4:1]=8` 表示逻辑值 16，因为最低位恒为 0，编码时省略。
- `jal ra,2048` 的 `imm[11]=1` 对应逻辑值 2048。`llvm-mc` 回显成 `jal 2048`，省略了默认的 `ra`。
- `lui a0,74565` 是十进制回显；`74565 = 0x12345`。不要把回显的十进制当成不同编码。

### 2.5 大小端 triple

RISC-V 有四个注册 triple：`riscv32`、`riscv64`、`riscv32be`、`riscv64be`。后两个是大端。实测一个全局常量：

```llvm
target datalayout = "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128"
target triple = "riscv64-unknown-elf"
@g = global i32 16909060
define i32 @ld() {
  %v = load i32, ptr @g, align 4
  ret i32 %v
}
```

模块里的 datalayout 写的是小端 `e-m:e:...`。实测把这个模块分别交给 `riscv64` 和 `riscv64be`，对象文件的数据编码随 triple 改变；这说明 ELF 的对象格式由 triple 决定，不要求 IR 里的 datalayout 先改成 `E`。如果要在 IR 层也显式表达大端，可以把 datalayout 的 `e` 改成 `E`，再确认常量与内存布局的语义一致。命令：

```powershell
llc -mtriple=riscv64   -mattr=+m -O2 -filetype=obj endian.ll -o le.o
llc -mtriple=riscv64be -mattr=+m -O2 -filetype=obj endian.ll -o be.o
llvm-readobj --file-headers --hex-dump=.data le.o
llvm-readobj --file-headers --hex-dump=.data be.o
```

真实结果：

```text
le.o:
  Format: elf64-littleriscv
  DataEncoding: LittleEndian (0x1)
  Hex dump of section '.data':
  0x00000000 04030201

be.o:
  Format: elf64-bigriscv
  DataEncoding: BigEndian (0x2)
  Hex dump of section '.data':
  0x00000000 01020304
```

同一个 `16909060`（`0x01020304`）在小端对象里是 `04 03 02 01`，在大端对象里是 `01 02 03 04`。汇编文本里的 `lw` 助记符不变，但数据布局、ELF 数据编码和常量池字节顺序都变了。这就是 triple 的连锁影响：它不是“换个名字”，而是换了一整套对象文件约定。
### 2.6 对后端的连锁影响：Legalizer、ABI、编码

ISA 模型不是文档层的装饰，它会在三个下游方向产生硬约束。

**Legalizer 方向**。`RISCVTargetLowering` 的构造函数先取 `MVT XLenVT = Subtarget.getXLenVT()`，再 `addRegisterClass(XLenVT, &RISCV::GPRRegClass)`。随后它对 `MUL`/`MULHS`/`MULHU` 设置 `Expand`，并在 `!Subtarget.hasStdExtM()` 时把 `SDIV`/`UDIV`/`SREM`/`UREM` 也设为 `Expand`。源码节选：

```cpp
setOperationAction({ISD::MUL, ISD::MULHS, ISD::MULHU}, XLenVT, Expand);
...
if (!Subtarget.hasStdExtM()) {
  setOperationAction({ISD::SDIV, ISD::UDIV, ISD::SREM, ISD::UREM},
                     XLenVT, Expand);
}
```

`Expand` 的结果是走 libcall 或目标自定义 lowering。实测 `-mattr=-m` 的 `i64` 乘法输出 `tail __muldi3`；开启 `+m` 后输出 `mul`。同一段 IR，特性位不同，Legalizer 的选择就不同。

**ABI 方向**。`RISCVSubtarget::initializeSubtargetDependencies()` 在解析完特性后调用 `RISCVABI::computeTargetABI(*this, ABIName)`。ABI 名称决定：

- 参数用哪些寄存器传递（`a0`-`a7`，浮点 `fa0`-`fa7`）。
- 哪些寄存器是 callee-saved。`CSR_ILP32_LP64` 包含 `X1`、`X8`、`X9`、`X18`-`X27`；`CSR_ILP32D_LP64D` 再加上 `F8_F`、`F9_F`、`F18_F`-`F27_F`；`CSR_ILP32_LP64_V` 再加上 `CSR_V`。
- 栈对齐与参数溢出区的布局。

`-mattr=+f,+d` 不只是让 `fadd.d` 可选，它还会把 ABI 从 `lp64` 推到 `lp64d`，从而改变浮点参数的传递方式和 callee-saved 集合。这个连锁反应在第 12、13 章会展开。

**编码方向**。每条指令的 `InstFormat` 写进 `TSFlags{4-0}`；`RISCVMCCodeEmitter` 根据它选择 fixup 形式。扩展谓词 `AssemblerPredicate` 决定这条指令在给定特性下是否可匹配。`.attribute 5` 与 `.option arch` 则把特性集合写进汇编/对象文件。于是同一个 `mul` 在不同特性下有四种状态：不可选、可选、可汇编、可反汇编；四者由不同的表和谓词控制。

把三条通路压成一张表：

| ISA 约束 | Legalizer | ABI | 编码 |
| --- | --- | --- | --- |
| XLEN=32/64 | `XLenVT`、合法类型宽度 | `ilp32`/`lp64` | `ADD` vs `ADDW` 等指令可用性 |
| M 扩展 | `MUL`/`DIV` 是否 Expand | 无直接影响 | `mul`/`div` 的谓词与编码 |
| F/D 扩展 | 浮点操作合法化 | `lp64f`/`lp64d`、浮点 callee-saved | `fadd.d` 等编码 |
| C 扩展 | 压缩候选 | 无直接影响 | 16 位编码与 `.option rvc` |
| V 扩展 | 向量类型合法化 | `CSR_V`、向量参数窗口 | `vsetvli` 与向量指令编码 |
| 大小端 triple | 常量/内存布局 | 对象文件约定 | ELF 数据编码 |

## 可跑实验

### 实验 1：开关 M，观察选指退化

目的：确认特性位如何改变 Legalizer/ISel 的结果。

命令：

```powershell
$ll = Join-Path $env:TEMP "mul.ll"
$lines = @(
  'target triple = "riscv64-unknown-elf"'
  'define i64 @f(i64 %a, i64 %b) {'
  '  %m = mul i64 %a, %b'
  '  ret i64 %m'
  '}'
)
[IO.File]::WriteAllText($ll, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))

"=== +m ==="
llc -mtriple=riscv64 "-mattr=+m" -O2 $ll -o -
"=== -m ==="
llc -mtriple=riscv64 "-mattr=-m" -O2 $ll -o -
```

真实输出（节选，省略了 CFI、`.size` 与 `.section` 等指令）：

```asm
=== +m ===
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
f:
	mul	a0, a0, a1
	ret

=== -m ===
	.attribute	5, "rv64i2p1"
f:
	tail	__muldi3
```

解读：`+m` 让 `MUL` 走目标指令；`-m` 让 Legalizer 选择 libcall，函数尾部变成 `tail __muldi3`。`tail` 而不是 `call` 是因为这是尾调用，不需要保存 `ra`。

失败排查：如果 `-m` 仍然输出 `mul`，检查是否有函数级 `target-features` 把 M 又打开了。函数属性优先于命令行。

### 实验 2：六种格式的编码与解码

目的：把 `llvm-mc -show-encoding` 的字节序列和 `RISCVInstrFormats.td` 的位段对上。

命令：

```powershell
$s = Join-Path $env:TEMP "enc.s"
$lines = @(
  "addi a0, a1, 42"
  "add a0, a1, a2"
  "sub a0, a1, a2"
  "sw a0, 12(sp)"
  "ld a0, -8(s0)"
  "beq a0, a1, 16"
  "lui a0, 0x12345"
  "jal ra, 2048"
)
[IO.File]::WriteAllText($s, ($lines -join "`n"), (New-Object Text.UTF8Encoding($false)))
llvm-mc -triple=riscv64 -mattr=+m,+a,+f,+d -show-encoding $s
```

预期输出：就是 2.4 节的八行 `encoding:`。注意 `jal ra, 2048` 的反汇编显示会省略默认的 `ra`，写成 `jal 2048`，但编码里的 `rd` 仍是 1。

解读：挑三条核对：

- `addi` 的字节 `[0x13,0x85,0xa5,0x02]` 拼成 `0x02A58513`。bit31-20 是 `0x02A`，即 42；bit19-15 是 `11`，即 `a1`；bit11-7 是 `10`，即 `a0`；opcode 是 `0x13`。
- `beq` 的 `0x00B50863` 里，bit31 是 0，bit30-25 是 0，bit24-20 是 11（`a1`），bit19-15 是 10（`a0`），bit14-12 是 0，bit11-8 是 8，bit7 是 0。把 `imm[4:1]=8` 左移一位得到 16。
- `jal` 的 `0x001000EF` 里，bit20 是 1，对应 `imm[11]=1`；bit11-7 是 1，即 `ra`；opcode 是 `0x6F`。立即数 2048 的编码不是连续的一段，而是打散在 bit31、bit30-21、bit20、bit19-12。

失败排查：如果 `sw` 变成两字节 `[0x2a,0xc6]`，说明 `+c` 被默认启用了（或者命令里带了 `+c`）。压缩形式是第 06 章的主题，做格式练习时把 `+c` 去掉。

### 实验 3（边界）：RV32 上的 i64 乘法

目的：看 XLEN 约束如何把一条 IR 乘法拆成多条机器指令。

命令：

```powershell
llc -mtriple=riscv32 "-mattr=+m" -O2 rv32.ll -o -
```

预期输出：就是 2.1 节的 `m64`：`mul`、`mulhu`、`mul`、`mul`、`add`、`add` 六条。

解读：RV32 的 GPR 只有 32 位，`i64` 乘法需要分别计算低 32 位和高 32 位，再组合。这不是优化失败，而是 XLEN 的直接后果。

失败排查：如果 `m64` 只有一条 `mul`，说明 triple 不是 `riscv32`，或者函数签名被改成了 `i32`。

### 实验 4（边界）：大端对象的数据字节序

目的：确认 `riscv64be` 不只是 triple 名字，而是对象文件约定。

命令：

```powershell
llc -mtriple=riscv64   -mattr=+m -O2 -filetype=obj endian.ll -o le.o
llc -mtriple=riscv64be -mattr=+m -O2 -filetype=obj endian.ll -o be.o
llvm-readobj --file-headers --hex-dump=.data le.o
llvm-readobj --file-headers --hex-dump=.data be.o
```

预期输出：见 2.5 节：`le.o` 是 `elf64-littleriscv`，`.data` 为 `04030201`；`be.o` 是 `elf64-bigriscv`，`.data` 为 `01020304`。

解读：同一个 `i32` 常量在两个对象里字节序相反，说明端序是 triple 带来的对象级约定，而不是汇编文本的差异。

失败排查：如果两个对象都是小端，检查 `-mtriple` 是否被后面的 `-mtriple=` 覆盖，或者 `.ll` 里是否硬编码了另一个 triple。模块内的 `target triple` 与命令行不一致时，命令行通常优先，但不要依赖这种覆盖关系，保持两者一致。

## 源码落点

- `RISCVFeatures.td`：`FeatureStdExtI/M/A/F/D/C/V`、`RISCVExtension<major, minor, description, deps>`、`RISCVExtensionBitmask<word, bit>`、`HasStdExt*` 谓词。
- `RISCVRegisterInfo.td`：`X0`-`X31`、`F0_H`-`F31_H`、`VR`/`VRM2`/`VRM4`/`VRM8`、`GPR`/`GPRNoX0`/`GPRC`/`FPR64`；`ABIRegAltName`。
- `RISCVCallingConv.td`：`CSR_ILP32_LP64`、`CSR_ILP32F_LP64F`、`CSR_ILP32D_LP64D`、`CSR_V`。
- `RISCVInstrFormats.td`：`InstFormat`、`RVInstCommon`、`RVInstR`、`RVInstI`、`RVInstS`、`RVInstB`、`RVInstU`、`RVInstJ`；`TSFlags{4-0}`。
- `RISCVInstrInfo.td`：`ADDI` 在 23.1.4 第 797 行，`ADD` 在第 818 行，`SUB` 在第 820 行。
- `RISCVISelLowering.cpp`：`XLenVT` 与 `addRegisterClass`；`setOperationAction({ISD::MUL, ISD::MULHS, ISD::MULHU}, XLenVT, Expand)`；`!Subtarget.hasStdExtM()` 分支里的除法 Expand。
- `RISCVSubtarget.cpp`：`initializeSubtargetDependencies()` 的 `ParseSubtargetFeatures`、`computeTargetABI`、`RISCVFeatures::validate`。
- `MCTargetDesc/RISCVMCCodeEmitter.cpp`：读取 `TSFlags`、选择 fixup 类型。
- `RISCVAsmPrinter.cpp`：`emitAttributes`、`emitTargetFeaturePush` / `emitTargetFeaturePop`。

## 常见坑

1. **把 `-mattr` 当成全局唯一开关**。函数级 `target-features` 可以覆盖命令行；`.attribute 5` 又是文件级。三者作用域不同，看到指令和属性不一致时先分清作用域。
2. **用 `+c` 做格式练习**。`llvm-mc` 会自动压缩 `lw`/`sw`/`ld`/`sd`，输出两字节编码。做 R/I/S/B/U/J 位域练习时去掉 `+c`。
3. **把 RISC-V 立即数当成连续位段**。B 和 J 的立即数被打散到四个不连续区间；按连续位段拼接会得到错误偏移。
4. **把 `sw` 的 bit11-7 当成 `rd`**。S 格式没有 `rd`，bit11-7 是 `imm[4:0]`；按 R 格式读会把立即数误认成寄存器。
5. **把 `x0` 当成普通寄存器**。`x0` 是常量 0，写入被丢弃；`GPRNoX0` 之类的寄存器类就是为这个约束存在的。
6. **以为 `riscv64be` 只改汇编文本**。数据布局、ELF 数据编码、常量池字节序都会变；对象文件里的 `.data` 字节序是直接证据。
7. **用退出码判断 `-mattr` 是否生效**。未知特性只警告，退出码是 0；要看汇编里是否出现预期指令。
8. **把 `.attribute 5` 当成函数级特性**。函数级差异体现在 `.option push` / `.option arch, ...` / `.option pop`，不是 `.attribute 5`。

## 自我检查

- [ ] 我能解释 RV32 上 `i64` 乘法为什么会被拆成多条 `mul`/`mulhu`/`add`。
- [ ] 我能从 `FeatureStdExtM` 的定义里指出它的版本、依赖和位掩码。
- [ ] 我能解释 `+m` 为什么会带出 `zmmul1p0`，`+a` 为什么会带出 `zaamo1p0` 和 `zalrsc1p0`。
- [ ] 我能把 `x0`-`x31` 的 ABI 名与角色对应起来，并说出 `GPRC` 的用途。
- [ ] 我能说出 `F8`-`F9`、`F18`-`F27` 在 `CSR_ILP32D_LP64D` 里的角色。
- [ ] 我能画出 R/I/S/B/U/J 六种格式的位段，并把 `addi`/`beq`/`jal` 的真实编码拆开。
- [ ] 我能解释 `riscv64be` 对数据布局和 ELF 数据编码的影响，并给出 `.data` 字节序证据。
- [ ] 我能把 ISA 约束分别映射到 Legalizer、ABI、编码三条下游通路。

## 延伸阅读

- RISC-V ISA 手册：指令格式、立即数编码、扩展版本号。
- RISC-V psABI：整数/浮点/向量寄存器约定、callee-saved 集合、栈对齐。
- LLVM 官方文档：`llvm.org/docs/TableGen/`，理解 `RISCVExtension` 这类自定义记录。
- 同版本源码：`RISCVFeatures.td`、`RISCVRegisterInfo.td`、`RISCVCallingConv.td`、`RISCVInstrFormats.td`、`RISCVISelLowering.cpp`。
- 同版本源码：`RISCVInstrInfo.td` 的 `ADDI`（第 797 行）、`ADD`（第 818 行）、`SUB`（第 820 行）。

[上一章：后端全景与对象家族](./01-backend-map.md) | [下一章：流水线与 MIR](./03-pipeline-and-mir.md)