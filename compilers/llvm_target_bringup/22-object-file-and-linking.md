# Ch 22 — 对象文件与链接

> **目标**：把 MiniRV 的机器码变成 ELF 目标文件、再链接成可执行文件，解释 `getRelocType`、重定位复用、`ld.lld --no-relax` 与分支松弛各自负责哪一段。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`；链接器 `/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld`；模拟器 `/usr/bin/qemu-riscv64-static`。

## 为什么需要这个？

`llc -o -` 输出的是汇编文本。文本不能执行：符号之间是名字引用，跳转目标是标签，全局变量的地址根本不存在。要让程序跑起来，必须有人把"名字"变成"地址"，把"标签"变成"偏移"。

这个转换分两段：

1. **汇编器/编译器后端**：把指令编码成字节，把无法就地解析的引用写成**重定位**（relocation），产出 `ET_REL` 目标文件。
2. **链接器**：决定每个节最终放在哪个虚拟地址，然后按重定位类型把值写进指令或数据，产出 `ET_EXEC`。

所以对象文件不是一个"中间产物"，它是编译器和链接器之间的**ABI**。这个 ABI 有三条硬约定：ELF 头里的 machine 号、节的名字和属性、重定位类型的编号与语义。MiniRV 在这三条上做了一个非常务实的决定：**全部复用 RISC-V**。

复用带来的收益是"立刻能跑"：`ld.lld`、`readelf`、`qemu`、`perf` 都不需要认识 MiniRV，它们只认识 `EM_RISCV`。代价是 MiniRV 的指令编码和重定位语义必须**逐位**和 RISC-V 一致，否则链接器会安静地写出错误结果。

这一章回答四个问题：

1. 一个 `.o` 里到底有什么，谁写的？
2. fixup 和 relocation 的分界线在哪里？
3. 复用 `EM_RISCV` 为什么可行，挡住了哪些工作量？
4. 分支太远时，编译器、汇编器、链接器谁负责修？

## 先建立直觉

先看整条链路，从上往下是每一层新增的信息：

```
 g.ll
  |  llc -mtriple=minirv64 -O2 -filetype=obj
  v
 g.o   (ET_REL，地址全是 0，符号之间靠 relocation 连接)
 +------------------------------------------------------------+
 | .text            0x14  5 条指令：auipc/addi/ld/addi/jalr    |
 | .rela.text       0x30  2 项：PCREL_HI20 + PCREL_LO12_I      |
 | .data            0x08  1 个 i64：41                         |
 | .eh_frame        0x28  CIE + FDE                            |
 | .rela.eh_frame   0x18  1 项：R_RISCV_32_PCREL               |
 | .symtab          0x90  6 项：main、x、.L0、文件符号…        |
 +------------------------------------------------------------+
  |  ld.lld -m elf64lriscv --no-relax start.o g.o -o g.elf
  v
 g.elf  (ET_EXEC，地址已定，relocation 全部消失)
 +------------------------------------------------------------+
 | .text  @ 0x11184   auipc gp,0x1 / addi gp,gp,0x18 / ld …    |
 | .data  @ 0x121a8   41                                       |
 | .eh_frame @ 0x10158                                         |
 | 没有 .rela.*                                                |
 +------------------------------------------------------------+
  |  qemu-riscv64-static ./g.elf
  v
 _start -> main -> exit code 42
```

再看链接器到底改了什么。MiniRV 取全局变量地址只有一种序列：`auipc` + `addi`，两条指令各带一个重定位，必须**成对**解析：

```
 汇编（未解析）                        链接后（main @ 0x11190, x @ 0x121a8）
 ----------------------------------    -------------------------------------
 .Lpcrel_hi0:                          .Lpcrel_hi0:
   auipc x3, x        <- HI20            auipc x3, 0x1        # 0x11190+0x1000
   addi  x3, x3, .Lpcrel_hi0 <- LO12_I   addi  x3, x3, 0x18   # +0x18 = 0x121a8
   ld    x3, 0(x3)                       ld    x3, 0(x3)
```

`HI20` 重定位指向符号 `x`，`LO12_I` 重定位指向**标签** `.Lpcrel_hi0`。链接器先算 `x` 的地址，再通过标签找到配对的 `auipc`，两边用同一个差值。搞错任何一边，取到的就是错误地址 —— 而且不会有任何报错。

## 主线：从 fixup 到可执行文件

### 22.1 一个 `.o` 里有什么

```console
$ readelf -SW g.o | sed -n '1,13p'
There are 9 section headers, starting at offset 0x1b8:

Section Headers:
  [Nr] Name              Type            Address          Off    Size   ES Flg Lk Inf Al
  [ 0]                   NULL            0000000000000000 000000 000000 00      0   0  0
  [ 1] .strtab           STRTAB          0000000000000000 000160 000052 00      0   0  1
  [ 2] .text             PROGBITS        0000000000000000 000040 000014 00  AX  0   0  4
  [ 3] .rela.text        RELA            0000000000000000 000118 000030 18   I  8   2  8
  [ 4] .data             PROGBITS        0000000000000000 000058 000008 00  WA  0   0  8
  [ 5] .note.GNU-stack   PROGBITS        0000000000000000 000060 000000 00      0   0  1
  [ 6] .eh_frame         PROGBITS        0000000000000000 000060 000028 00   A  0   0  8
  [ 7] .rela.eh_frame    RELA            0000000000000000 000148 000018 18   I  8   6  8
  [ 8] .symtab           SYMTAB          0000000000000000 000088 000090 18      1   4  8
```

四个数字值得算一遍：

- `.text` 是 `0x14` = 20 字节，正好 5 条 32 位指令。
- `.rela.text` 是 `0x30` = 48 字节。ELF64 的 `Elf64_Rela` 是 24 字节，所以**恰好 2 项重定位**。
- `.symtab` 是 `0x90` = 144 字节，`Elf64_Sym` 也是 24 字节，所以**恰好 6 个符号**。
- `Lk`（link）字段指向字符串表或符号表：`.rela.text` 的 `Lk=8` 指向 `.symtab`，`Inf=2` 表示它作用于第 2 个节（`.text`）。重定位节和"它修饰的节"就是这样关联的。

ELF 头里的 machine 号是复用的核心：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -h g.o | sed -n '2,24p'
File: g.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
ElfHeader {
  Ident {
    Magic: (7F 45 4C 46)
    Class: 64-bit (0x2)
    DataEncoding: LittleEndian (0x1)
    FileVersion: 1
    OS/ABI: SystemV (0x0)
    ABIVersion: 0
    Unused: (00 00 00 00 00 00 00)
  }
  Type: Relocatable (0x1)
  Machine: EM_RISCV (0xF3)
  Version: 1
  Entry: 0x0
  ProgramHeaderOffset: 0x0
  SectionHeaderOffset: 0x1B8
  Flags [ (0x0)
  ]
```

- `Class: 64-bit`、`DataEncoding: LittleEndian` —— 来自 `MiniRVELFObjectWriter` 构造函数的 `Is64Bit=true` 和 `MCAsmBackend(llvm::endianness::little)`。
- `Type: Relocatable` —— `ET_REL`，还不是可执行文件。
- `Machine: EM_RISCV (0xF3)` —— 这一行决定了后面所有工具的路径。
- `Flags: 0x0` —— 没有 `EF_RISCV_RVC`（不支持压缩指令）、没有 RVE 位、float ABI 位是 0（soft-float，正好对应 MiniRV 的 libcall 实现）。MiniRV 是纯 RV64IM，这个 0 是诚实的。

### 22.2 三个角色：CodeEmitter、AsmBackend、ObjectWriter

一条带符号引用的指令要经过三个对象，职责切得很干净：

| 角色 | 类 | 职责 | MiniRV 实现 |
|---|---|---|---|
| 编码 | `MCCodeEmitter` | 指令 + 操作数 → 字节，同时登记 fixup | `MiniRVMCCodeEmitter` |
| 修正 | `MCAsmBackend` | fixup 的位布局；能就地解析就写入；否则留给重定位 | `MiniRVAsmBackend` |
| 定型 | `MCELFObjectTargetWriter` | fixup kind → ELF 重定位类型；符号还是节+偏移 | `MiniRVELFObjectWriter` |

`MiniRVAsmBackend::getFixupKindInfo` 只描述"fixup 落在哪条指令里"，不描述位域：

```cpp
  static const MCFixupKindInfo Infos[] = {
      // The bit positions inside the instruction are not contiguous, so
      // applyFixup() does the placement itself; these entries only describe
      // the instruction the fixup lives in.
      {"fixup_minirv_branch", 0, 32, 0},
      {"fixup_minirv_jal", 0, 32, 0},
      {"fixup_minirv_pcrel_hi20", 0, 32, 0},
      {"fixup_minirv_pcrel_lo12_i", 0, 32, 0},
  };
```

这是 RISC-V 系分支指令的共性：B 型和 J 型的立即数被打散在指令的多个不连续位段里，通用位域框架表达不了，所以 `applyFixup` 自己拼。

`applyFixup` 的两种命运：

- **能就地解析**（目标是同节、本地符号）：直接把值编码进指令，文件里**不留重定位**。MiniRV 的分支和 `jal` 大多走这条路径。
- **不能解析**（外部符号、跨节、需要链接器决定地址）：值保持 0，`MiniRVELFObjectWriter::getRelocType` 决定写哪种重定位。

`getRelocType` 是整章最该背下来的一段：

```cpp
  unsigned getRelocType(const MCFixup &Fixup, const MCValue &Target,
                        bool IsPCRel) const override {
    // ADD/SUB pairs used for symbol differences already carry their ELF type.
    if (mc::isRelocation(Fixup.getKind()))
      return Fixup.getKind();
    switch (Fixup.getKind()) {
    case MiniRV::fixup_minirv_branch:
      return ELF::R_RISCV_BRANCH;
    case MiniRV::fixup_minirv_jal:
      return ELF::R_RISCV_JAL;
    case MiniRV::fixup_minirv_pcrel_hi20:
      return ELF::R_RISCV_PCREL_HI20;
    case MiniRV::fixup_minirv_pcrel_lo12_i:
      return ELF::R_RISCV_PCREL_LO12_I;
    case FK_Data_8:
      return ELF::R_RISCV_64;
    case FK_Data_4:
      if (Target.getSpecifier() == ELF::R_RISCV_32_PCREL)
        return ELF::R_RISCV_32_PCREL;
      return IsPCRel ? ELF::R_RISCV_32_PCREL : ELF::R_RISCV_32;
    default:
      llvm_unreachable("Unknown fixup kind!");
    }
  }
```

四个自定义 fixup 对应四条指令族，`FK_Data_*` 对应数据里的指针，`mc::isRelocation` 那条分支是符号差（`a - b`）专用的 ADD/SUB 对。

`MiniRVAsmBackend::evaluateFixup` 还处理了 `pcrel_lo` 的配对：它通过标签找到对应的 `pcrel_hi` fixup，如果目标在同一个节、是本地符号且已定义，就当场解析；否则留给链接器。这个逻辑和 GNU as / RISC-V 后端的做法同源。

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -O2 g.ll -o - | sed -n '1,16p'
	.file	"g.ll"
	.text
	.globl	main                            # -- Begin function main
	.type	main,@function
main:                                   # @main
	.cfi_startproc
# %bb.0:
.Lpcrel_hi0:
	auipc	x3, x
	addi	x3, x3, .Lpcrel_hi0
	ld	x3, 0(x3)
	addi	x10, x3, 1
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	main, .Lfunc_end0-main
	.cfi_endproc
```

注意 `auipc x3, x` 和 `addi x3, x3, .Lpcrel_hi0`：**高部位引用符号 `x`，低部位引用标签**。这个"低部指向高部标签"的约定是 RISC-V psABI 规定的配对方式，也是链接器能把两条指令当成一个整体处理的原因。

对应到重定位：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -r g.o | sed -n '/Relocations/,$p'
Relocations [
  Section (3) .rela.text {
    0x0 R_RISCV_PCREL_HI20 x 0x0
    0x4 R_RISCV_PCREL_LO12_I .L0  0x0
  }
  Section (7) .rela.eh_frame {
    0x1C R_RISCV_32_PCREL .L0  0x0
  }
]
```

`0x0`/`0x4` 是节内偏移（两条指令各 4 字节）。`HI20` 的符号是 `x`，`LO12_I` 的符号是 `.L0` —— 就是 `.Lpcrel_hi0` 那个标签在符号表里的名字（临时符号统一命名）。链接器通过"LO12 指向的标签所在位置"找到对应的 `auipc`，这就是配对。

`.eh_frame` 那条 `R_RISCV_32_PCREL` 是上一章的老朋友：FDE 的 PC 起始地址。它和 `LO12` 一样是 PC 相对量，但宽度是 32 位、不是指令位域。

### 22.3 复用 RISC-V 重定位：收益与代价

MiniRV 直接发射 6 种重定位，另有一对用于符号差；全部是 RISC-V psABI 里已有的编号：

| fixup / 场景 | ELF 类型 | 编码位置 | 范围 | 谁应用 |
|---|---|---|---|---|
| `fixup_minirv_branch` | `R_RISCV_BRANCH` | B 型立即数（12\|10:5\|4:1\|11） | ±4 KiB | 同节就地 / 链接器 |
| `fixup_minirv_jal` | `R_RISCV_JAL` | J 型立即数（20\|10:1\|11\|19:12） | ±1 MiB | 同节就地 / 链接器 |
| `fixup_minirv_pcrel_hi20` | `R_RISCV_PCREL_HI20` | `auipc` 的 imm[31:12] | ±2 GiB | 链接器 |
| `fixup_minirv_pcrel_lo12_i` | `R_RISCV_PCREL_LO12_I` | I 型 imm[11:0] | 配对高部 | 链接器 |
| `FK_Data_8` | `R_RISCV_64` | 8 字节数据 | 64 位绝对 | 链接器 |
| `FK_Data_4` | `R_RISCV_32` / `R_RISCV_32_PCREL` | 4 字节数据 | 32 位 | 链接器 |
| 符号差 `a-b` | `R_RISCV_ADD32` + `R_RISCV_SUB32` | 4 字节数据 | 两个重定位 | 链接器 |

和 RISC-V 参照后端对比，差异不在"用哪些类型"，而在"什么时候用"：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -r g_rv.o \
    | sed -n '/rela.text/,/}/p'
  Section (3) .rela.text {
    0x0 R_RISCV_HI20 x 0x0
    0x4 R_RISCV_LO12_I x 0x0
  }
```

同一份 IR 编译给 `riscv64`，取全局变量走的是 `lui` + `addi` + 绝对重定位 `HI20/LO12_I`；MiniRV 只有 `auipc` + `addi` 这一种序列，所以永远发 PC 相对对。这不是谁更先进，而是"MiniRV 的 GlobalAddress lowering 只实现了 PC 相对形式"。

**为什么复用可行**：MiniRV 的指令编码、ABI、寄存器编号都是 RV64IM 的子集，重定位的位布局和 RISC-V 完全一致。链接器只看 `e_machine` 和重定位类型，不看编译器是谁。

**它挡住了哪些工作量**：

1. 新 machine 号（比如 `EM_MINIRV`）—— 需要新写一个 lld target、给 binutils/llvm-readobj 加支持、给 qemu 加 target、给内核 binfmt 加识别。任何一环缺失，"能编译"就变成"跑不起来"。
2. 新重定位编号 —— 需要写 psABI 文档、改链接器、改所有反汇编器；而且这些编号一旦发布就不能回收。
3. 自己写链接器 —— 那是最不划算的重复劳动。

**代价**：语义必须逐位一致。链接器对 `R_RISCV_BRANCH` 的假设是"13 位有符号、位 12 在 bit31、位 10:5 在 bit30:25……"。如果 MiniRV 哪天改了分支立即数的布局却仍然发 `R_RISCV_BRANCH`，链接器会写出完全错误的跳转，而且**没有任何诊断**。

`needsRelocateWithSymbol` 也值得单说：

```cpp
  bool needsRelocateWithSymbol(const MCValue &, unsigned) const override {
    // Keep the original symbol: the low part of a PC-relative pair has to name
    // the auipc label, not the section it lives in.
    return true;
  }
```

如果返回 false，链接器可能把"符号"折叠成"节 + 偏移"，`LO12` 就找不到 `auipc` 那个标签，配对直接失败。RISC-V 后端的实现更保守（注释里写着 "very conservative, update once RISC-V psABI requirements are clarified"），MiniRV 直接照抄这个结论。

### 22.4 链接与运行

`ld.lld` 用和 RISC-V 一样的 emulation：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/ld.lld -m elf64lriscv --no-relax \
    start.o g.o -o g.elf && /usr/bin/qemu-riscv64-static ./g.elf; echo "exit=$?"
exit=42
```

`42` = `41 + 1`：`main` 从 `.data` 里读到 41，加 1 返回。这个数字证明了三件事都对了：全局地址的 `auipc/addi` 对算对了、`.data` 被放到可读地址、`exit` 系统调用拿到了返回值。

链接后的代码里已经没有符号，只有算好的立即数：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-objdump -d --no-show-raw-insn g.elf \
    | sed -n '/<main>:/,/ret/p'
0000000000011190 <main>:
   11190:      	auipc	gp, 0x1
   11194:      	addi	gp, gp, 0x18
   11198:      	ld	gp, 0x0(gp)
   1119c:      	addi	a0, gp, 0x1
   111a0:      	ret
```

（objdump 用 RISC-V 的反汇编器，所以寄存器显示成 `gp`/`a0`，而不是 MiniRV 的 `x3`/`x10` —— 二进制层面它们是同一个编码。）

`0x1` 和 `0x18` 就是 `0x121a8 - 0x11190 = 0x1018` 拆成的高 20 位和低 12 位。可执行文件里也没有任何重定位残留：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -r g.elf | sed -n '/Relocations/,$p'
Relocations [
]
```

`--no-relax` 在这里的作用需要说清楚。它让 `ctx.arg.relax = false`（`lld/ELF/Driver.cpp` 里 `args.hasFlag(OPT_relax, OPT_no_relax, true)`），于是 `R_RISCV_RELAX` 不再被登记成 `R_RELAX_HINT`（`lld/ELF/Arch/RISCV.cpp`）；`relaxOnce` 仍会被调用，但找不到任何带 hint 的项。

但 MiniRV 的对象里**根本没有** `R_RISCV_RELAX` —— 上一节的 3 条重定位就是全部。所以这个参数对 MiniRV 自己的代码没有影响。实测去掉它一样能链接、一样退出 42：

```console
$ /home/ubuntu/llvm/builds/pr-lane/bin/ld.lld -m elf64lriscv start.o g.o -o g_relax.elf \
    && echo "link without --no-relax OK"
link without --no-relax OK
```

e2e 脚本统一传 `--no-relax`，是为了让 MiniRV 和 RISC-V 参照两条链路在"同一种链接行为"下比较，而不是因为 MiniRV 需要它。

**端到端 17/17**：脚本把每个 `.ll` 分别编译成 MiniRV 和 RISC-V 目标文件，链接同一个 `start.s`，在 qemu 下比较退出码（在 tree 根目录执行）：

```console
$ LLVM_BUILD_DIR=/home/ubuntu/llvm/builds/dsl-tablegen \
    LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld \
    QEMU=/usr/bin/qemu-riscv64-static \
    llvm/lib/Target/MiniRV/dev/e2e/run.sh | tail -17
OK   bf10             exit=108
OK   bf2              exit=21
OK   bigframe         exit=42
OK   bigframe2        exit=42
OK   edge             exit=237
OK   jt               exit=129
OK   misc             exit=114
OK   narrowstack      exit=223
OK   selftest         exit=66
OK   t1               exit=20
OK   t150             exit=150
OK   tc               exit=105
OK   twoarr           exit=129
OK   va               exit=184
OK   vla              exit=17
OK   vla2             exit=39
OK   vla3             exit=45
```

差分对比的意义：如果 MiniRV 的调用约定、栈布局、全局地址序列有任何一处和 RISC-V 不一致，某个程序的退出码就会不同。17 个程序覆盖了递归、VLA、可变参数、跳表、大帧、窄整数、移位与比较 —— 这是 bring-up 阶段性价比最高的一张网（软浮点和 128 位运算不在这套 e2e 里，它们靠 lit 测试覆盖）。

### 22.5 分支松弛：编译器侧和链接器侧是两件事

三个地方的"距离限制"完全不同：

| 指令 | 编码 | 可达范围 | 谁检查 |
|---|---|---|---|
| `beq/bne/blt/bge/bltu/bgeu` | 13 位有符号（含符号位） | ±4 KiB | CodeGen 的 `isBranchOffsetInRange`；链接器 `checkInt(..., 13, ...)` |
| `jal` | 21 位有符号 | ±1 MiB | 同上，`checkInt(..., 21, ...)` |
| `auipc` + `addi` | 高 20 + 低 12 | ±2 GiB | `checkInt(..., 20, ...)` |

commit `f597d37`（`[MiniRV] Relax out-of-range branches`）之前，MiniRV 没有跑 `BranchRelaxationPass`，越界的分支会被直接编码成回绕的立即数，然后**静默跳到别处**。commit message 写得很直白：

> The six comparison records reach +-4 KiB and jal reaches +-1 MiB, but the backend never ran BranchRelaxation, so a farther target was encoded with a wrapped displacement and silently jumped elsewhere.

修复分两部分。第一，把 pass 排进 pipeline：

```cpp
void MiniRVCodeGenPassBuilder::addPreEmitPass(PassManagerWrapper &PMW) {
  // A conditional branch reaches 4 KiB and jal 1 MiB.  Split a block whose
  // target is farther away instead of letting the immediate wrap around.
  addMachineFunctionPass(BranchRelaxationPass(), PMW);
}
```

第二，实现 pass 需要的四个钩子（`MiniRVInstrInfo`）：

- `getInstSizeInBytes` —— 基类返回 `~0U`，不实现的话每个块的大小都是 `0xffffffff`，pass 根本无法判断距离。
- `getBranchDestBlock` —— 分支/跳转的最后一个操作数是目标 MBB。
- `isBranchOffsetInRange` —— `isInt<13>` / `isInt<21>`。
- `insertIndirectBranch` —— 无条件跳转太远时，把目标物化到保留寄存器 `x5`（`PseudoLA` + `PseudoBRIND`），不污染任何活值，也不需要 restore block。

实测两个方向：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 br.ll -o - | sed -n '7,12p'
# %bb.0:                                # %entry
	bne	x10, x0, .LBB0_1
	jal	x0, .LBB0_2
.LBB0_1:                                # %near
	#APP
	.zero	4200
```

条件分支太远时，pass 把条件**反过来**（`beq` → `bne`），让 fallthrough 变成"跳到远块"的 `jal`。`jal` 能到 1 MiB，所以 4200 字节轻松覆盖。

无条件跳转超过 1 MiB 时只能走寄存器：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 br.ll -o - | sed -n '25,30p'
	bne	x10, x0, .LBB1_1
# %bb.3:                                # %entry
.Lpcrel_hi0:
	auipc	x5, .LBB1_2
	addi	x5, x5, .Lpcrel_hi0
	jalr	x0, 0(x5)
```

`x5` 是保留寄存器（`MiniRVRegisterInfo` 里不参与分配），所以 `insertIndirectBranch` 不需要保存/恢复，`RestoreBB` 保持为空。如果连 32 位偏移都放不下，它直接 `report_fatal_error("branch offsets outside the signed 32-bit range are not supported")`。

**汇编器这一侧不做任何松弛**。`MiniRVAsmBackend` 没有覆盖 `mayNeedRelaxation`、`relaxInstruction`、`fixupNeedsRelaxation`，所以 `.s` 里手写的越界分支不会被修：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-mc -triple=minirv64 -filetype=obj far.s -o far.o \
    && echo "assembled OK" && /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-objdump -d far.o | sed -n '6,7p'
assembled OK
0000000000000000 <f>:
       0: 80050263     	beqz	a0, 0xfffffffffffff004 <f+0xfffffffffffff004>
```

`far.s` 里分支后面隔了 4096 字节的 `.zero`，目标在 4100 字节外，超出 ±4096。汇编器没有报错，而是把立即数截成 13 位后回绕成 `-0xffc`。更糟的是这个对象里**没有任何重定位**：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj -r far.o | sed -n '/Relocations/,$p'
Relocations [
]
```

因为目标是同节、本地、已定义，`applyFixup` 当场就把值写死了。链接器根本看不到这条分支，也就没有机会检查范围。

这是 MiniRV 的 AsmBackend 与 RISC-V 最大的差距：`RISCVAsmBackend` 实现了 `mayNeedRelaxation` / `relaxInstruction`，它的松弛方向是**展开** —— 够不着的短形式换成能到更远的长形式（`C.BEQZ` → `BEQ`，`BEQ` → `PseudoLongBEQ`），所以汇编器产出的对象里分支永远是合法的。MiniRV 没有这一层，把责任全部推给 CodeGen 的 `BranchRelaxation`，并且接受"手写汇编越界 = 未定义"。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 目标文件写出 | `MCTargetDesc/MiniRVELFObjectWriter.cpp` | `MiniRVELFObjectWriter`、`getRelocType` | 4 个 fixup → RISC-V 重定位；`needsRelocateWithSymbol` |
| 基类 | `llvm/include/llvm/MC/MCELFObjectTargetWriter.h` | 构造函数 `(Is64Bit, OSABI, EMachine, HasRelocAddend)` | `EM_RISCV` + RELA |
| fixup 布局 | `MCTargetDesc/MiniRVAsmBackend.cpp` | `getFixupKindInfo`、`applyFixup` | 位段自己拼；同节就地解析 |
| 配对解析 | `MCTargetDesc/MiniRVAsmBackend.cpp` | `evaluateFixup`、`getPCRelHiFixup` | `pcrel_lo` 找 `pcrel_hi` |
| 符号差 | 同上 | `R_RISCV_ADD*/SUB*` | profile 描述符 |
| MC 注册 | `MCTargetDesc/MiniRVMCAsmInfo.cpp` | `DwarfFDERelSymbolSpec` | FDE 的 PC 字段类型 |
| 分支松弛 | `MiniRVCodeGenPassBuilder.cpp` | `addPreEmitPass` | `BranchRelaxationPass` |
| 松弛钩子 | `MiniRVInstrInfo.cpp` | `getInstSizeInBytes`、`isBranchOffsetInRange`、`insertIndirectBranch` | `~0U` 的坑、`x5` 保留寄存器 |
| ASM 输出 | `MiniRVAsmPrinter.cpp` | `emitSymbolAddress` | `.Lpcrel_hi0` 标签由谁发 |
| 链接器 | `lld/ELF/Arch/RISCV.cpp` | `relocate`、`checkInt`、`relaxOnce` | 每种重定位的位布局与范围检查 |
| 链接器开关 | `lld/ELF/Driver.cpp` | `OPT_relax` / `OPT_no_relax` | `ctx.arg.relax` 的唯一来源 |
| RISC-V 参照 | `RISCVELFObjectWriter.cpp`、`RISCVAsmBackend.cpp` | `getRelocType`、`mayNeedRelaxation`、`relaxInstruction` | 完整版长什么样 |

## 反直觉与踩雷

1. **错误直觉**：`EM_RISCV` 只是一个标签，填什么都行。→ **正确认识**：它决定链接器调用哪套 `relocate`/`getRelExpr`/`relaxOnce`。填了 `EM_RISCV` 就必须接受 RISC-V 的全部语义，包括位布局和范围检查。

2. **错误直觉**：分支越界汇编器会报错。→ **正确认识**：MiniRV 的 `applyFixup` 只掩码写入，不检查范围；同节分支当场解析，连重定位都不留（`far.o` 的 `Relocations` 为空）。范围检查只有两条路：CodeGen 的 `BranchRelaxation`，或链接器处理**重定位**时的 `checkInt`。

3. **错误直觉**：`--no-relax` 是 MiniRV 必需的。→ **正确认识**：MiniRV 不发射 `R_RISCV_RELAX`，去掉 `--no-relax` 一样能链接、一样 `exit=42`。harness 传它是为了让两条链路行为一致。

4. **错误直觉**：`needsRelocateWithSymbol` 返回 true 只是保守。→ **正确认识**：`pcrel_lo` 的符号必须是 `auipc` 的标签。折叠成"节+偏移"后，链接器找不到配对的高部，PC 相对对就废了。

5. **错误直觉**：符号差 `a - b` 是一个重定位。→ **正确认识**：ELF 没有"差值"类型，必须拆成 `R_RISCV_ADD32` + `R_RISCV_SUB32` 两条，链接器分别加、减。`8bffd72` 里 profile 描述符就踩过这个坑。

6. **错误直觉**：`.rela.text` 的 `size` 是重定位的字节数。→ **正确认识**：`0x30` 是**节的大小**，除以 24（`Elf64_Rela`）才是条数。看到 `size=0x30` 就说"两条重定位"，这个换算值得练熟。

## 进阶：再往深一层

**thunk 与 PLT**。`-m elf64lriscv` 让 lld 走 RISC-V 的 `needsThunks` 分支（`createThunks`），远处调用会插入 thunk 而不是松弛。MiniRV 的 `PseudoCALL` 展开成 `auipc` + `jalr`，天然覆盖 ±2 GiB，所以它不需要 thunk；但一旦引入动态链接，`R_RISCV_CALL_PLT` 和 PLT 就会进来，那时"复用 RISC-V"的边界要重新审一遍。

**relaxation 是一个不动点迭代**。`Writer.cpp` 里的循环最多跑 30 轮，因为删除字节会改变后续地址，可能让原本"够近"的调用变得"够远"。`--no-relax` 下每一轮 `relaxOnce` 都无事可做，一轮就收敛；MiniRV 因为没有 `RELAX` 提示，即使开着也一样。

**`.option norelax` 与编译器无关**。GNU as 用它阻止汇编器发 `R_RISCV_RELAX`。MiniRV 的汇编器根本不发，所以这个伪指令对它是无操作 —— 这也解释了为什么 MiniRV 的 `.s` 可以直接被 RISC-V 工具链消费（只要指令子集相同）。

**代码尺寸也是一个验收维度**。`e186e2a` 加的 harness 除了差分退出码，还统计代码尺寸。bring-up 后期真正难的不是"跑通"，而是"跑通且不比参照后端差太多"；`getInstSizeInBytes`、`ADDI` 拆分策略、`x0` 分支形式都属于这一类优化。想看 MiniRV 为了尺寸做过哪些取舍，读 Ch 15 和 Ch 18；想看 RISC-V 的完整重定位表和 relaxation 实现，读 [`../elf_linking/README.md`](../elf_linking/README.md)。

## 本章重点整理

- 对象文件是编译器和链接器的 ABI：machine 号、节属性、重定位编号三者缺一不可。
- MiniRV 复用 `EM_RISCV` + RISC-V 重定位编号，代价是"语义必须逐位一致"，收益是 lld/qemu/readelf 全部现成可用。
- 三个角色分工：`MCCodeEmitter` 编码并登记 fixup，`MCAsmBackend` 决定就地解析还是留重定位，`MCELFObjectTargetWriter` 决定重定位类型。
- `pcrel_hi20` 指向符号，`pcrel_lo12_i` 指向 `auipc` 的标签；配对靠标签，`needsRelocateWithSymbol` 必须为 true。
- 同节、本地、已定义的分支不留重定位，因此链接器没有机会检查范围；`far.o` 的 `Relocations` 为空就是证据。
- 分支松弛在 CodeGen（`BranchRelaxationPass` + 四个钩子）；汇编器侧 MiniRV 完全没有松弛，越界手写汇编会静默回绕。
- `--no-relax` 对 MiniRV 无影响（不发射 `R_RISCV_RELAX`），harness 用它只是为了两条链路一致。
- 端到端验收是 `17/17` 差分 + `qemu` 退出码，而不是"能链接"。

## 自我检核

- 不看代码，按顺序说出 fixup 从编码到写进 ELF 经过的三个类，以及每个类"决定什么"。
- `auipc x3, x` 和 `addi x3, x3, .Lpcrel_hi0` 的两条重定位各指向谁？为什么低部不能指向 `x`？
- `.rela.text` 的 `size=0x30` 意味着几条重定位？`.symtab` 的 `size=0x90` 意味着几个符号？换算过程写出来。
- 如果 MiniRV 改用新的 `EM_MINIRV`，列出至少四处需要改动的工具（不用写代码，说清改什么）。
- 为什么 `far.o` 里没有重定位？如果没有重定位，链接器还能查出越界吗？那 CodeGen 的 `BranchRelaxation` 在防什么？
- 去掉 `--no-relax` 后链接行为有变化吗？为什么？如果换成 RISC-V 参照对象呢？
- `getInstSizeInBytes` 不实现会发生什么？为什么基类返回 `~0U` 会让 pass 失效而不是报错？

## 延伸阅读

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V 社区
  - **读哪里**：重定位表（`R_RISCV_PCREL_HI20`/`LO12_I`/`BRANCH`/`JAL` 的位布局）与 `%pcrel_hi`/`%pcrel_lo` 的配对规则。
  - **能学到什么**：为什么低部要指向高部标签，以及 13/21/32 位范围限制的来源。
  - **前提**：读过本章 22.2–22.3，能看懂一条 `auipc` + `addi`。

- **[lld: ELF 链接器实现](https://lld.llvm.org/)** — LLVM 项目
  - **读哪里**：RISC-V 架构支持一节，以及 `lld/ELF/Arch/RISCV.cpp` 里的 `relocate` 与 `relaxOnce`。
  - **能学到什么**：链接器如何把重定位落到指令位域，`checkInt` 的范围诊断长什么样，relaxation 为什么要迭代。
  - **前提**：知道 ELF 的 `SHT_RELA` 和 `Elf64_Rela` 结构。

- **[LLVM MC 层文档](https://llvm.org/docs/CodeGenerator.html#the-mc-layer)** — LLVM 项目
  - **读哪里**：MC 层的对象写出、fixup 与 `MCAssembler` 的执行流程。
  - **能学到什么**：`applyFixup` / `evaluateFixup` / `getRelocType` 分别在什么时机被调用。
  - **前提**：读过本课程 Ch 7 和 Ch 8。

- **[ELF 与链接](../elf_linking/README.md)** — 本仓库课程
  - **读哪里**：节、符号表、RELA、链接脚本与 relaxation 的完整讲解。
  - **能学到什么**：把本章的 MiniRV 例子放回通用 ELF 链接模型里，理解 `--no-relax`、thunk、PLT 的位置。
  - **前提**：本章的 `.o → ld.lld → ELF` 流程已经跑过一遍。

- **[RISC-V 后端全解](../riscv_backend/README.md)** — 本仓库课程
  - **读哪里**：`RISCVELFObjectWriter`、`RISCVAsmBackend` 的 relaxation、`%specifier` 处理。
  - **能学到什么**：成熟后端的重定位矩阵有多复杂，以及 MiniRV 砍掉了哪些。
  - **前提**：本章的 6 种重定位已经能对上号。

→ [Ch 23 诊断与拒绝](./23-diagnostics-and-rejection.md)
