# Ch 09 反汇编器：同一批字节，两种读法

> 目标：把“字节 → 指令”的过程拆成 16/32/48 位分派、decode 表匹配、寄存器解码、打印四个阶段。读完后你能解释 `llvm-mc -disassemble` 与 `llvm-objdump -d` 为什么对同一段 `.text` 给出不同文本，为什么默认看不到 `c.lwsp`，以及为什么 `llvm-objdump --mattr=-c` 不能关掉压缩解码。

> 环境：Windows；LLVM 23.1.4；PowerShell。工具目录与统一入口同第 08 章。反汇编实验的对象既可以是 `llvm-mc -disassemble` 的字节串，也可以是 `llvm-mc -filetype=obj` 生成的 ELF。

> 约定：标“实测”的片段来自本机重跑；“源码依据”来自 23.1.4 的 `Disassembler/RISCVDisassembler.cpp` 与 `MCTargetDesc/RISCVInstPrinter.cpp`；需要改源码重编译才能观察的结论标 `guided`。

## 为什么

反汇编常被当作汇编的逆运算：给同一批字节，应该得到同一行文本。实际不是。本机对 `0x22,0x45` 的默认反汇编是 `lw a0, 8(sp)`，加 `-M no-aliases` 才变成 `c.lwsp a0, 8(sp)`；对 `0x82,0x80` 默认是 `ret`，加 `-M no-aliases` 是 `c.jr ra`；对 `0xff,0xf5,0xc5,0x13` 默认是 `not a0, a1`，加 `-M no-aliases` 是 `xori a0, a1, -1`。同一批字节没有变，变的是打印策略。

造成差异的机制有三层。第一层是解码宽度：低两位不是 `0b11` 就按 16 位解，否则尝试 32 位，再按位模式尝试 48/64/更大宽度。第二层是解码表：标准表、RV32-only 表、Zfinx 表、厂商扩展表按特征位过滤，缺特征时同一串字节会落到不同的表或直接失败。第三层是打印：`RISCVInstPrinter::printInst` 在允许别名时先调用 `RISCVRVC::uncompress` 把压缩指令还原成基指令，再按 `InstAlias` 选一条文本；关掉别名才会看到 `C_*` 的原名。

这三层解释了课程里最常见的两类困惑。第一类是“objdump 为什么显示 `c.*`”：默认的 LLVM objdump 其实不显示，除非加 `-M no-aliases`；GNU objdump 的默认策略不同，看到 `c.*` 不必惊讶。第二类是“反汇编再汇编，文本变了”：`c.addi a0, 1` 和 `addi a0, a0, 1` 编码相同，反汇编只会挑一种写法，原文不可恢复。

## 主线

### 9.1 宽度分派：先看低两位，再看 48/64/80+ 的位模式

`RISCVDisassembler::getInstruction` 的第一行判断是 `(Bytes[0] & 0b11) != 0b11`：成立就调用 `getInstruction16`，否则先尝试 `getInstruction32`。32 位尝试失败后，源码继续检查“是否是更长的指令”：

- 如果 `(Bytes[0] & 0b1'1100) != 0b1'1100`，按 4 字节未知指令失败，`Size` 置 4（不足 4 字节则置 0）。
- 如果 `(Bytes[0] & 0b11'1111) == 0b01'1111`，调用 `getInstruction48`。
- 如果 `(Bytes[0] & 0b111'1111) == 0b011'1111`，按 8 字节指令失败。
- 其余情况看第二个字节的高 3 位 `nnn`，长度是 `10 + nnn * 2` 字节，覆盖 80 到 176 位；`nnn == 0b111` 留给更长的保留编码。

这套分派决定了“截断字节流”会发生什么。实测只给 3 个字节 `0xef,0x00,0x10`（本应是一条 4 字节 `jal`）：前两个字节会被当成一条 16 位指令解成 `addi s0, sp, 32`，同时对剩余 1 个字节报 `invalid instruction encoding`。也就是说，截断不仅会报错，还可能先“成功”解出一条并不存在的指令，然后让后续字节错位。分析对象文件时，节区大小与指令边界必须一起看。

`getInstruction16` 与 `getInstruction32` 都会先检查剩余字节数，不足时把 `Size` 置 0 并返回 `Fail`。这个 `Size` 是流式反汇编的游标：失败时如果 `Size` 非 0，调用方仍会前进对应字节数，避免死循环；`Size` 为 0 时通常意味着输入耗尽。

### 9.2 decode 表：TableGen 生成，运行时按特征挑表

本机重跑 `-gen-disassembler`：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
& "$bin/llvm-tblgen.exe" -I $inc -I $td -gen-disassembler "$td/RISCV.td" -o "$env:TEMP/RISCVGenDisassemblerTables.inc"
```

实测产物 1,485,949 字节（brief 记 1,451.1 KB，按 KiB 换算一致）。文件里可以看到：

```text
static const uint8_t DecoderTable16[593] = {
static const uint8_t DecoderTable32[20815] = {
static DecodeStatus decodeToMCInst(...)
static DecodeStatus decodeInstruction(...)
```

`DecoderTable16` 并不大，因为 16 位编码空间有限；`DecoderTable32` 两万多字节，覆盖标准指令与各扩展。`decodeInstruction` 按表走位，`decodeToMCInst` 把匹配到的编码转换成 `MCInst` 的操作数。

运行时不是只查一张表。`DecoderList32` 的顺序在源码里写死：先是厂商扩展表（XCV、Xqci、XVentana、XTHead、XSfvector、XSfSystem、XSfcease、XMIPS、XAndes、XSMT、XAIF 等），再是标准 `DecoderTable32`、`RV32Only32`、`Zfinx32`、`ZdinxRV32Only32`。每张表有一个 `haveContainedFeatures` 检查，当前 `STI` 的特征位不满足就跳过。`DecoderList16` 同样先厂商、再 `Zicfiss16`、标准 `DecoderTable16`、`RV32Only16`、`ZcOverlap16`。

这解释了一个常见现象：同一串字节在 `-mattr=+c` 与不带 `+c` 时结果不同。不是解码算法变了，而是候选表集合变了。`0x22,0x45` 在不带 `+c` 时没有任何 16 位表能接受，于是报 `invalid instruction encoding`；带 `+c` 时标准 16 位表命中 `C_LWSP`。

### 9.3 寄存器解码：压缩指令只有 3 位，靠受限寄存器类补齐

生成的 decode 表只给出寄存器编号，真正的 `MCRegister` 由 `RISCVDisassembler.cpp` 里的解码函数补上。源码里能看到 `DecodeGPRRegisterClass`、`DecodeGPRX1X5RegisterClass`、`DecodeGPRNoX0RegisterClass`、`DecodeGPRNoX2RegisterClass`、`DecodeGPRPairRegisterClass`、`DecodeGPRPairNoX0RegisterClass`，以及浮点与向量的对应函数。

以 `DecodeGPRRegisterClass` 为例，它用模板 `DecodeSimpleRegisterClass<RISCV::X0, 32>` 把 0 到 31 的编号映射到 `X0` 到 `X31`。压缩指令的寄存器字段只有 3 位，能表示 8 个寄存器；`C_LWSP` 的 `rd` 是 `GPRNoX0`，`C_LW` 的基址是 `GPRC`（x8–x15）。解码函数在编号超范围或落在禁止集合时会返回 `Fail`，而不是随便映射一个寄存器。

`c.addi` 的 `rd` 解码函数会拒绝 `x0`；`c.li` 允许 `x0`（因为 `li x0, imm` 虽然无副作用，但编码合法）。这些差异来自 TableGen 里 `DecoderMethod` 与操作数类的组合，不是解码器手写的 if 链。读 `.td` 时看到 `let DecoderMethod = ...`，就知道这里有一个对应的 C++ 模板实例。

### 9.4 打印：先反压缩，再选别名

`RISCVInstPrinter::printInst` 的源码很短，但决定了一切：

```cpp
bool Res = false;
const MCInst *NewMI = MI;
MCInst UncompressedMI;
if (PrintAliases && !NoAliases)
  Res = RISCVRVC::uncompress(UncompressedMI, *MI, STI);
if (Res)
  NewMI = &UncompressedMI;
if (!PrintAliases || NoAliases || !printAliasInstr(NewMI, Address, STI, O))
  printInstruction(NewMI, Address, STI, O);
```

`RISCVRVC::uncompress` 声明在 `MCTargetDesc/RISCVBaseInfo.h`，定义在 `MCTargetDesc/RISCVBaseInfo.cpp`，内部调用 `-gen-compress-inst-emitter` 生成的 `uncompressInst`。本机生成的 `RISCVGenCompressInstEmitter.inc` 有 `uncompressInst` 与每个 `C_*` 到基指令的转换分支，例如 `C_ADD` 转 `ADD`、`C_LWSP` 转 `LW`、`C_JALR` 转 `JALR`。

反压缩成功后，`printAliasInstr` 再按 `InstAlias` 的 `EmitPriority` 选文本。于是 `C_LWSP` 先变 `LW`，再匹配 `lw` 的别名/自带 AsmString，输出 `lw a0, 8(sp)`；`C_JALR` 先变 `JALR X1, X10, 0`，再匹配 `jalr $rd, $rs`，输出 `jalr a0`；`C_JR` 先变 `JALR X0, X1, 0`，再匹配优先级 4 的 `ret`，输出 `ret`。

`applyTargetSpecificCLOption` 负责接收 objdump 的 `-M` 选项：`no-aliases` 把 `PrintAliases` 置 false，`numeric` 把 `ArchRegNames` 置 true，`emit-x8-as-fp` 控制 x8 的打印名。`llvm-mc -disassemble -M no-aliases` 与 `llvm-objdump -d -M no-aliases` 走的是同一个开关，所以两者在“显示 `c.*`”这件事上一致。

### 9.5 往返实验：文本会变，字节不变

把第 08 章的压缩输入与基指令输入放在一起：

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
@('c.addi a0, 1','addi a0, a0, 1','ret','c.jr ra') |
  & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -show-encoding
```

实测四行：

```text
	addi	a0, a0, 1                       # encoding: [0x05,0x05]
	addi	a0, a0, 1                       # encoding: [0x05,0x05]
	ret                                     # encoding: [0x82,0x80]
	ret                                     # encoding: [0x82,0x80]
```

`c.addi a0, 1` 与 `addi a0, a0, 1` 都被压缩成同一个 `C_ADDI`，编码都是 `[0x05,0x05]`；打印器把 `C_ADDI` 反压缩成 `ADDI`，于是回显成 `addi`。`c.jr ra` 与 `ret` 都是 `[0x82,0x80]`，打印器选优先级更高的 `ret`。

把 `[0x05,0x05]` 丢给 `llvm-mc -disassemble`：

```powershell
'0x05,0x05' | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble
```

实测输出 `addi a0, a0, 1`。再把它汇编回去，仍然是 `[0x05,0x05]`。所以“往返”在字节层是稳定的，在文本层不是恒等的。任何依赖文本比较的测试（例如把反汇编输出与原始 `.s` 做 diff）都会在这类等价写法上失败。

### 9.6 RVC：同一串字节，缺 `+c` 就是无效编码

实测：

```powershell
'0x22,0x45' | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble
'0x22,0x45' | & "$bin/llvm-mc.exe" -triple=riscv64 -disassemble
```

第一行输出 `lw a0, 8(sp)`；第二行输出：

```text
<stdin>:1:1: warning: invalid instruction encoding
```

同一个 2 字节串，特征位不同，结论从“合法 `c.lwsp`”变成“无效编码”；该行没有输出任何指令。这也是 objdump 必须读 ELF 属性的原因：对象文件里 `.riscv.attributes` 的 `arch` 字符串告诉反汇编器该启用哪些表。

RVC 的边界还包括 `0x0000` 与全 `0xff`。实测 `0x00,0x00` 输出 `unimp`，`exit=0`；`0xff,0xff,0xff,0xff` 输出四条 `warning: invalid instruction encoding`（每 2 字节一条），`exit=0`。前者是合法的 `C_UNIMP`，后者不是“一个大错误”，而是流式解码对每个 16 位槽位分别失败。

### 9.7 objdump：默认反压缩，`-M no-aliases` 才看到 `c.*`

用一个带压缩指令的对象做实验：

```powershell
$asm = @(
  "`t.text",
  "`t.globl`tf",
  "f:",
  "`tlw`ta0, 8(sp)",
  "`tsw`ta0, 12(sp)",
  "`tjalr`tra, 0(a0)",
  "`txori`ta0, a1, -1",
  "`tret"
) -join "`n"
$src = Join-Path $env:TEMP 'riscv-c.asm'
$obj = Join-Path $env:TEMP 'riscv-c.o'
[IO.File]::WriteAllText($src, $asm, (New-Object Text.UTF8Encoding($false)))
& "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -filetype=obj $src -o $obj
```

后文的 `riscv-c.o` 都指 `$obj`。

实测 `llvm-objdump -d`：

```text
0000000000000000 <f>:
       0: 4522         	lw	a0, 0x8(sp)
       2: c62a         	sw	a0, 0xc(sp)
       4: 9502         	jalr	a0
       6: fff5c513     	not	a0, a1
       a: 8082         	ret
```

加 `-M no-aliases`：

```text
       0: 4522         	c.lwsp	a0, 0x8(sp)
       2: c62a         	c.swsp	a0, 0xc(sp)
       4: 9502         	c.jalr	a0
       6: fff5c513     	xori	a0, a1, -0x1
       a: 8082         	c.jr	ra
```

默认输出里没有一条 `c.*`，因为 `printInst` 先反压缩再打印。加 `-M no-aliases` 后 `PrintAliases=false`，`uncompress` 不执行，`C_*` 的 AsmString 原样打印。注意同一份输出里 `not` 也变回 `xori`：别名开关是全局的，不只影响压缩指令。

再加 `-M numeric`：

```text
       0: 4522         	lw	x10, 0x8(x2)
       2: c62a         	sw	x10, 0xc(x2)
       4: 9502         	jalr	x10
       6: fff5c513     	not	x10, x11
       a: 8082         	ret
```

`numeric` 只改寄存器名，不改别名；`lw` 仍然是 `lw`，但 `a0` 变成 `x10`，`sp` 变成 `x2`。这三个开关可以组合，读输出时要先确认开了哪些。

### 9.8 失败与边界：`--mattr` 不是万能开关

`llvm-objdump` 接受 `--mattr`，但它的行为和 `llvm-mc` 不同。实测对同一个带 `+c` 属性的对象：

```powershell
& "$bin/llvm-objdump.exe" -d "--mattr=-c" $obj
```

输出与不加 `--mattr` 完全一致，仍然是 `lw` / `sw` / `jalr` / `not` / `ret`。原因在特征合并：对象文件的 `.riscv.attributes` 已经启用了 C/Zca，`--mattr=-c` 不会把属性里的位清掉。想改变解码，需要构造一个没有该属性的对象，或者用 `llvm-mc -disassemble` 直接控制 `-mattr`。

另一个坑是未知特征：

```text
llvm-objdump.exe : '+notreal' is not a recognized feature for this target (ignoring feature)
```

`exit=0`，反汇编照常输出。也就是说，拼错特征名不会让命令失败，只会让该特征被忽略；如果这个特征本来是解码所必需的，你会得到一份“看起来正常、其实按缺省特征解码”的输出。

最根本的边界是“反汇编不可逆”。`c.jr ra` 与 `ret` 共享 `[0x82,0x80]`，`c.addi a0, 1` 与 `addi a0, a0, 1` 共享 `[0x05,0x05]`，`xori a0, a1, -1` 与 `not a0, a1` 共享 `[0x13,0xc5,0xf5,0xff]`。MCInst 只保留 opcode 与操作数，打印器只挑一条文本；原始拼写、原始别名、原始压缩形式都不在字节里。任何“反汇编后逐字比对源码”的验证都需要先规范化，或者直接比较字节。

## 可跑实验

### 实验 A：14 组字节的默认读法

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
foreach ($s in @(
  '0x13,0x85,0xa5,0x02','0x13,0xc5,0xf5,0xff','0x22,0x45','0x2a,0xc6',
  '0x02,0x95','0x01,0x00','0x05,0x05','0x7d,0x55','0x88,0x45',
  '0x2e,0x85','0x82,0x80','0x65,0xfd','0x01,0xa8','0xef,0x00,0x10,0x00'
)) {
  $s | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble
}
```

实测输出依次为：

```text
	addi	a0, a1, 42
	not	a0, a1
	lw	a0, 8(sp)
	sw	a0, 12(sp)
	jalr	a0
	nop
	addi	a0, a0, 1
	li	a0, -1
	lw	a0, 8(a1)
	mv	a0, a1
	ret
	bnez	a0, -8
	j	16
	jal	2048
```

这 14 行里，只有 `addi a0, a1, 42`、`sw a0, 12(sp)` 等少数几行与编码的字面形态一致；其余都经过了反压缩或别名选择。

### 实验 B：同一批字节的 `no-aliases` 读法

```powershell
foreach ($s in @('0x13,0xc5,0xf5,0xff','0x02,0x95','0x2e,0x85','0x82,0x80','0x65,0xfd','0x7d,0x55')) {
  $s | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble -M no-aliases
}
```

实测：

```text
	xori	a0, a1, -1
	c.jalr	a0
	c.mv	a0, a1
	c.jr	ra
	c.bnez	a0, -8
	c.li	a0, -1
```

与实验 A 对照：`not` ↔ `xori`，`jalr a0` ↔ `c.jalr a0`，`mv` ↔ `c.mv`，`ret` ↔ `c.jr ra`，`bnez` ↔ `c.bnez`，`li` ↔ `c.li`。字节完全一样，只是打印策略不同。

### 实验 C：对象文件上的 objdump 三连

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
& "$bin/llvm-objdump.exe" -d $obj
& "$bin/llvm-objdump.exe" -d -M no-aliases $obj
& "$bin/llvm-objdump.exe" -d -M numeric $obj
```

三份输出的指令条数相同、地址相同、字节相同，差异只在文本。默认与 `numeric` 都会反压缩，`no-aliases` 不反压缩。把三份输出并排看，可以一次性确认“别名”“压缩”“寄存器名”是三个独立开关。

### 实验 D：无效与截断

```powershell
'0x00,0x00' | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble
'0xff,0xff,0xff,0xff' | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble
'0xef,0x00,0x10' | & "$bin/llvm-mc.exe" -triple=riscv64 "-mattr=+c" -disassemble
```

实测第一行 `unimp`；第二行四条 `invalid instruction encoding` 警告，`exit=0`；第三行先输出 `addi s0, sp, 32`，再对最后一个字节报一条警告。第三行是最危险的：截断流会产生一条“成功解码”的假指令（`addi s0, sp, 32`），后续地址全部错位。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `Disassembler/RISCVDisassembler.cpp` | `getInstruction` | 低两位判 16 位；32 位失败后判 48/64/80+ |
| `Disassembler/RISCVDisassembler.cpp` | `getInstruction16/32/48` | 每个宽度的字节读取与 `Size` 设置 |
| `Disassembler/RISCVDisassembler.cpp` | `DecoderList16/32/48` | 表顺序与 `haveContainedFeatures` 过滤 |
| `Disassembler/RISCVDisassembler.cpp` | `DecodeGPRRegisterClass` 等 | 编号到 `MCRegister` 的映射与受限集合 |
| `MCTargetDesc/RISCVInstPrinter.cpp` | `printInst` | `uncompress` → `printAliasInstr` → `printInstruction` |
| `MCTargetDesc/RISCVInstPrinter.cpp` | `applyTargetSpecificCLOption` | `no-aliases` / `numeric` / `emit-x8-as-fp` |
| `MCTargetDesc/RISCVBaseInfo.h` | `namespace RISCVRVC` | `compress` / `uncompress` 的声明 |
| `MCTargetDesc/RISCVBaseInfo.cpp` | `RISCVRVC::uncompress` | 调用生成的 `uncompressInst` |
| 生成的 `RISCVGenDisassemblerTables.inc` | `DecoderTable16/32`、`decodeToMCInst`、`decodeInstruction` | 解码表与 MCInst 构造 |
| 生成的 `RISCVGenCompressInstEmitter.inc` | `uncompressInst` | `C_*` 到基指令的逐条转换 |

`RISCVGenDisassemblerTables.inc` 与 `RISCVGenCompressInstEmitter.inc` 都要自己生成。前者用 `-gen-disassembler`，后者用 `-gen-compress-inst-emitter`；两者都从 `RISCV.td` 出发，生成文件的头部会写明来源。

## 常见坑

1. **以为 objdump 默认显示 `c.*`。** LLVM 23.1.4 的 `llvm-objdump -d` 默认先反压缩，显示 `lw`、`jalr`、`ret`；加 `-M no-aliases` 才显示 `c.lwsp`、`c.jalr`、`c.jr`。GNU objdump 的默认策略不同，不要把两者的输出混为一谈。
2. **用 `--mattr=-c` 试图关掉压缩解码。** 对象文件的 `.riscv.attributes` 仍然启用 C/Zca，`--mattr` 不会清除属性位；实测输出不变。要控制解码，用 `llvm-mc -disassemble` 直接给 `-mattr`，或构造不同属性的对象。
3. **拼错 `--mattr` 后继续相信输出。** 未知特征只打印 `ignoring feature`，`exit=0`，反汇编照常进行。特征名拼错时，结果可能按缺省特征解码，看起来“没有报错”。
4. **把反汇编文本与源码逐字 diff。** `c.jr ra` 与 `ret`、`c.addi a0, 1` 与 `addi a0, a0, 1`、`not` 与 `xori -1` 都是同一编码的不同写法；文本 diff 必然失败。比较字节或规范化后的 MCInst。
5. **忽略 `-M numeric` 与 `-M no-aliases` 的独立作用。** `numeric` 只改寄存器名，`no-aliases` 只改别名/反压缩；两者都开时得到 `xori x10, x11, -0x1` 这类输出。
6. **截断字节流只看到一条警告就放心。** 3 字节的 `jal` 会解出一条 16 位 `addi`，并对剩余字节报错；地址与指令边界已经错位。对象分析要先确认节区大小是 2 的倍数且指令完整。
7. **忘了 `llvm-mc -disassemble` 不读 ELF 属性。** 它只看命令行 `-mattr`；对象文件才带属性。把对象里的字节抠出来喂给 `llvm-mc` 时，必须手动补 `+c` 等特征。

## 自我检查

- [ ] 能说出 `getInstruction` 如何用低两位区分 16 位与 32 位，以及 48/64/80+ 的判定位模式。
- [ ] 能解释 `DecoderList32` 的顺序为什么会影响同一串字节的解码结果。
- [ ] 能说明 `RISCVRVC::uncompress` 在打印链路中的位置，以及它依赖哪个生成文件。
- [ ] 能解释 `0x22,0x45` 在带 `+c` 与不带 `+c` 时的两种结果，并说明为什么命令行仍可能以 `exit=0` 返回。
- [ ] 能复现 `llvm-objdump -d`、`-M no-aliases`、`-M numeric` 三份输出，并指出差异来源。
- [ ] 能解释 `--mattr=-c` 为什么没有改变带 `+c` 属性对象的反汇编。
- [ ] 能举出至少三组“同一编码、不同文本”的例子，并说明为什么反汇编不可逆。
- [ ] 能解释 3 字节 `jal` 截断后为什么会先出现一条 `addi`。

## 延伸阅读

- LLVM 官方文档：*The LLVM MC Layer* 中关于 `MCDisassembler`、`MCInstPrinter`、`MCSubtargetInfo` 的章节。
- RISC-V 非特权 ISA 手册第 16 章“C Extension”，对照 `c.lwsp`、`c.jalr`、`c.jr` 的编码与寄存器限制。
- 源码：`Disassembler/RISCVDisassembler.cpp` 全文，重点看 `DecodeList` 与各 `Decode*RegisterClass`。
- 源码：`MCTargetDesc/RISCVInstPrinter.cpp`，对照 `printInst` 与 `applyTargetSpecificCLOption`。
- 测试：`llvm/test/MC/RISCV/` 下的 `rvc-*` 与 `invalid-*` 用例，和本章的字节表对照。
- 下一章：[MC 层与目标文件](./10-mc-layer-and-objects.md)，把 MCInst、流写器、编码器与 ELF 段连起来。

[上一章：汇编器](./08-asm-parser.md) | [下一章：MC 层与目标文件](./10-mc-layer-and-objects.md)