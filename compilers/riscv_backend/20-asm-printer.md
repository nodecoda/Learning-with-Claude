# 20 AsmPrinter：机器指令如何变成你读到的汇编

> 目标：把 `.s` 文件里的每一类行都归到它的生产者。读完本章，你能说出 `emitInstruction` 的四个出口、`%hi`/`%lo` 与 `%pcrel_hi`/`%pcrel_lo` 由谁选择、`.attribute 4/5` 从哪来、`.option push/arch/pop` 与 `.option norelax` 分别解决什么问题，以及 `-asm-verbose=false` 到底关掉了什么。

> 环境：LLVM 23.1.4 预编译。命令通过 `compilers/riscv_backend/code/tools/run.ps1` 执行。本章所有汇编都是 `llc -mtriple=riscv64` 的真实输出，反汇编来自 `llvm-objdump`，重定位来自 `llvm-readobj`。

## 为什么

机器指令到汇编文本这一步看起来只是"打印"，实际是整个后端里信息损失最严重的地方。MIR 里有虚拟寄存器、有 `MachineOperand` 的 `TargetFlags`、有 CFI 指令、有函数级 feature；到了 MC 层，只剩 `MCInst` 加操作数，再往下只剩字节。你在 `.s` 里看到的每一个 `%hi`、`.option`、`.cfi_`、`.attribute`，都是某个具体函数在这一步做的决定。

理解这一步的直接收益有三个：

1. **调试选指与 RA 的结论**。最终汇编里的 `mv a0, a0`、`tail foo`、`ret` 都可能是别名或伪指令的打印形式，不是新指令。
2. **读懂重定位**。`%hi/%lo` 与 `%pcrel_hi/%pcrel_lo` 的选择发生在 `RISCVMCInstLower`，代码模型一换，修饰符就换。
3. **写稳定的测试**。`update_llc_test_checks.py` 生成的 CHECK 行依赖打印机输出的稳定性；知道哪些行会被 `-asm-verbose=false` 关掉，才知道测试该固定什么。

这一章也会给出一条明确的边界：AsmPrinter 不决定指令选择，也不决定调度。它只决定"同样的 MIR 打印成什么文本"。看到汇编差异时，先确认 MIR 是否相同，再怀疑打印机。

## 主线

### 20.1 emitInstruction 的四个出口

`RISCVAsmPrinter::emitInstruction` 的结构可以当成一张路由表。按代码顺序：

1. `RISCV_MC::verifyInstructionPredicates(MI->getOpcode(), STI->getFeatureBits())`：先检查当前函数的 feature 是否允许这条指令。feature 不匹配时这里就会报错，而不是等到编码阶段。
2. `emitNTLHint(MI)`：处理非临时标签（NTL）提示，属于分支对齐相关的附加输出。
3. `lowerPseudoInstExpansion(MI, OutInst)`：TableGen 生成的伪指令展开。`PseudoRET` 的 `PseudoInstExpansion<(JALR X0, X1, 0)>` 就是在这里生效的。命中后直接 `EmitToStreamer` 并返回。
4. `switch (MI->getOpcode())` 处理特殊指令：`HWASAN_CHECK_MEMACCESS_SHORTGRANULES`、`KCFI_CHECK`、`STACKMAP`、`PATCHPOINT`、`STATEPOINT`、`PATCHABLE_FUNCTION_*`，以及 `PseudoCALLLpadAlign` / `PseudoCALLIndirectLpadAlign`。
5. 最后才走通用路径：`lowerToMCInst(MI, OutInst)` → `EmitToStreamer(*OutStreamer, OutInst)`。

这个顺序解释了两个常见现象。第一，`PseudoRET` 在汇编里打印成 `ret`，因为它先被第 3 步换成 `JALR X0, X1, 0`，再由 InstPrinter 按别名打印。第二，`PseudoTAIL` 打印成 `tail`，因为它没有 `PseudoInstExpansion`，而是带着 `AsmString` 直接进 MC 层；真正的 `auipc` + `jalr` 在编码时才出现（见第 19 章 19.5）。

`emitInstruction` 之外还有三个钩子决定文件骨架：

- `emitStartOfAsmFile(Module &M)`：处理 `target-abi` 与 `riscv-isa` 两个 module flag，然后调用 `emitAttributes`。
- `emitEndOfAsmFile(Module &M)`：收尾 `.riscv.attributes` 段，输出 GNU property note。
- `emitFunctionEntryLabel()`：如果函数是 vector call 约定，先输出 `.variant_cc`。

### 20.2 RISCVMCInstLower 与操作数修饰符

`RISCVMCInstLower` 是 AsmPrinter 内部的一个小类，职责是把 `MachineOperand` 转成 `MCOperand`。核心函数是 `lowerOperand`，符号类操作数统一走 `lowerSymbolOperand(MO, Sym, ...)`。它读的是 `MachineOperand` 上的 `TargetFlags`，而 `TargetFlags` 是选指阶段（`RISCVISelDAGToDAG`、`RISCVISelLowering`）写进去的。

同一个全局变量 `@g`，在三种代码模型下的修饰符不同：

```text
默认（medlow）:
	lui	a0, %hi(g)
	ld	a0, %lo(g)(a0)

medium（medany）:
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(g)
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi0)

PIC:
	auipc	a0, %got_pcrel_hi(g)
	ld	a0, %pcrel_lo(.Lpcrel_hi0)(a0)
```

三组修饰符对应三组重定位：

| 代码模型 | 指令序列 | 重定位（`llvm-readobj --relocations`） |
|---|---|---|
| 默认 / small | `lui` + `ld` | `R_RISCV_HI20 g`、`R_RISCV_LO12_I g` |
| medium | `auipc` + `addi` | `R_RISCV_PCREL_HI20 g`、`R_RISCV_PCREL_LO12_I .Lpcrel_hi0` |
| PIC | `auipc` + `ld` | `R_RISCV_GOT_HI20 g`、`R_RISCV_PCREL_LO12_I .Lpcrel_hi0` |

`%lo` 与 `%pcrel_lo` 的区别不只是拼写：`%lo` 直接引用符号 `g`，链接器把 `lui` 的高 20 位与 `addi`/`ld` 的低 12 位拼起来；`%pcrel_lo` 必须引用配对的 `.Lpcrel_hi*` 标签，链接器通过这个标签找到 `auipc` 的位置，再算 PC 相对偏移。第 19 章的 19.3 已经展示过标签是怎么产生的。

`lowerSymbolOperand` 还要处理 `MO_LO` / `MO_HI` / `MO_PCREL_LO` / `MO_PCREL_HI` / `MO_GOT_HI20` / `MO_TLS_*` 等标志。这些标志在 MIR 里长这样：

```text
%1:gpr = AUIPC target-flags(riscv-pcrel-hi) @g, pre-instr-symbol <mcsymbol .Lpcrel_hi0>
%0:gpr = ADDI %1:gpr, target-flags(riscv-pcrel-lo) <mcsymbol .Lpcrel_hi0>
```

`target-flags(riscv-pcrel-hi)` 是 MIR 层的写法，`%pcrel_hi` 是汇编层的写法，两者一一对应。调试重定位问题时，先看 MIR 的 `target-flags`，再看汇编的 `%`，最后看 `readobj` 的 `R_RISCV_*`，三者应当一致。

### 20.3 .attribute 4 与 .attribute 5

ELF 目标的汇编文件开头通常有两行：

```asm
	.attribute	4, 16
	.attribute	5, "rv64i2p1_m2p0_zmmul1p0"
```

- `4` 是 `Tag_RISCV_stack_align`，值是 16，表示栈对齐 16 字节。
- `5` 是 `Tag_RISCV_arch`，值是模块级 ISA 字符串。

这两行由 `RISCVAsmPrinter::emitAttributes` 产生：

```cpp
void RISCVAsmPrinter::emitAttributes(const MCSubtargetInfo &SubtargetInfo) {
  RISCVTargetStreamer &RTS = getTargetStreamer();
  // Use MCSubtargetInfo from TargetMachine. Individual functions may have
  // attributes that differ ...
  RTS.emitTargetAttributes(SubtargetInfo, /*EmitStackAlign*/ true);
}
```

注释里那句"individual functions may have attributes that differ"是关键。`.attribute 5` 用的是 **TargetMachine 的** `MCSubtargetInfo`，也就是 `-mattr` / `-mcpu` 决定的模块级特性；函数级 `target-features` 不会改这一行。函数级差异走 `.option arch`（下一节）。

一个真实的坑：你用 `-mattr=+m` 编一个带 `target-features="+m,+v"` 的函数，`.attribute 5` 仍然只有 `rv64i2p1_m2p0_zmmul1p0`，`v` 不出现在这里。若下游工具只读 `.attribute 5` 判断"这个文件用了向量"，它会判断错误。

### 20.4 .option push / .option arch / .option pop

函数级 feature 差异由 `emitTargetFeaturePush` / `emitTargetFeaturePop` 处理。逻辑是：把函数的 `MCSubtargetInfo` 和模块的 `MCSTI` 逐特性比对，只把差异项写进 `.option arch`，并用 `.option push` / `.option pop` 包住。

本机实测（模块用 `-mattr=+m`，第二个函数带 `target-features="+m,+v"`）：

```asm
	.option	push
	.option	arch, +d, +f, +v, +zicsr, +zve32f, +zve32x, +zve64d, +zve64f, +zve64x, +zvl128b, +zvl32b, +zvl64b
	.globl	withv
withv:
	...
	.option	pop
```

虽然函数体里只有一次 `lui` + `ld`，`.option arch` 仍然列出了 `+v` 隐含的整串扩展。原因是 `+v` 在 ISA 解析时会带出 `zve32x`、`zve64f`、`zvl128b` 等依赖，`getAllProcessorFeatures()` 比对的是展开后的 feature 集合，不是用户写的字符串。

`.option push` / `pop` 的意义是作用域：`.option arch` 修改的是汇编器的 ISA 状态，不 push 就会污染后面的函数。`RISCVTargetStreamer::emitDirectiveOptionArch` 负责输出，`RISCVAsmPrinter.cpp` 里 `emitLpadAlignedCall` 还展示了另一组用法：

```cpp
if (HasZca && HasRelax) {
  RTS.emitDirectiveOptionPush();
  RTS.emitDirectiveOptionExact();
}
...
RTS.emitDirectiveOptionPop();
```

`.option exact` 是"不要放松这条指令"的提示，用在需要 LPAD 对齐的调用序列上。

### 20.5 .option norelax：一条真实的实验

`llvm-mc` 源码里，`.option norelax` 由 `RISCVAsmParser::parseDirectiveOption` 识别，最终调到 `RISCVTargetAsmStreamer::emitDirectiveOptionNoRelax`，输出 `\t.option\tnorelax`。它的作用是让汇编器在 `.option norelax` 区间内**不给可放松的重定位打 `R_RISCV_RELAX` 标记**。

实测输入（`-mattr=+relax`）：

```asm
.text
.globl f
f:
  call foo
  .option push
  .option norelax
  call bar
  .option pop
  ret
```

`llvm-mc -triple=riscv64 -mattr=+relax -filetype=obj` 之后，`llvm-readobj --relocations` 给出：

```text
Section (3) .rela.text {
  0x0 R_RISCV_CALL_PLT foo 0x0
  0x0 R_RISCV_RELAX - 0x0
  0x8 R_RISCV_CALL_PLT bar 0x0
}
```

`call foo` 有两条重定位，`call bar` 只有一条。链接器只放松带 `R_RISCV_RELAX` 的那条：`foo` 的 `auipc`+`jalr`（8 字节）会被压成 `jal`（4 字节），`bar` 保持 8 字节。这就是 `.option norelax` 的实际效果——不是"禁止链接器放松整个文件"，而是"在这一段不打放松标记"。

什么时候需要它？当你在写依赖精确字节布局的代码（启动代码、打补丁的桩、对齐敏感的 trampoline），放松会改变后续指令的地址。此时用 `.option norelax` 锁住这一段的长度。

### 20.6 .cfi_* 与 -asm-verbose=false

栈帧信息由 `RISCVFrameLowering` 在 prologue/epilogue 里插入 CFI 伪指令，AsmPrinter 再把它们打印成 `.cfi_*`。一个带调用的小函数：

```asm
caller:
	.cfi_startproc
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	...
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
	.cfi_endproc
```

四条 CFI 各管一件事：`cfi_startproc`/`endproc` 划定函数范围；`cfi_def_cfa_offset` 记录 CFA 相对 sp 的偏移；`cfi_offset` 记录 `ra` 存在 `CFA-8`；`cfi_restore` 表示该寄存器恢复。它们不占指令字节，但决定 `eh_frame` 的内容。

`-asm-verbose=false` 的真实差异（同一份 `vec2.ll`）：

```text
默认:
	.globl	addv4                           # -- Begin function addv4
addv4:                                  # @addv4
	.cfi_startproc
# %bb.0:                                # %entry
	vsetivli	zero, 4, e32, m1, ta, ma
	...
	                                        # -- End function
	.section	".note.GNU-stack","",@progbits

-asm-verbose=false:
	.globl	addv4
addv4:
	.cfi_startproc
	vsetivli	zero, 4, e32, m1, ta, ma
	...
	.section	".note.GNU-stack","",@progbits
```

被关掉的是注释：`# -- Begin function`、`# @addv4`、`# %bb.0:`、`# 8-byte Folded Spill`。**没有被关掉**的是 `.cfi_*`、`.attribute`、`.option`、`.type`、`.size`。所以：

- `-asm-verbose=false` 不能用来"生成不带 CFI 的汇编"，它只去掉注释行（本机实测：`.cfi_*` 与 `.attribute` 都保留）。
- 它可以让测试的 CHECK 行更稳定，因为注释里含函数名、基本块号、spill 大小，改一行 IR 就会全变。
- 它也不会去掉 `.note.GNU-stack`，这个段标记与可执行栈有关，属于安全属性。

### 20.7 汇编稳定性与 update_llc_test_checks

后端测试里大量使用 `update_llc_test_checks.py`：它跑一遍 `llc`，把输出里的汇编行前面加上 `; CHECK:` 或 `# CHECK:`。这个脚本的稳定性完全依赖 AsmPrinter 的输出确定性。三类东西会让 CHECK 行抖动：

1. 注释里的函数名、基本块编号、spill 大小（`-asm-verbose=false` 可去掉大部分）。
2. 寄存器分配结果（与具体 RA 实现绑定，跨版本会变）。
3. `.option arch` 里的 feature 列表（ISA 解析一改，顺序和内容都可能变）。

实践中的写法是：测试里固定 `-asm-verbose=false`，只对关键指令加 CHECK，不要对 `.attribute`/`.option` 做逐字匹配，除非你测的就是属性输出。RISC-V 后端自己的 MC 测试（`llvm/test/MC/RISCV/`）更多用 `llvm-mc` 直接测汇编与编码，绕开了 AsmPrinter 的注释噪声。

`guided`：本机没有 `llvm-lit` 与 `FileCheck`，也没有 `update_llc_test_checks.py`（预编译包只带 bin，不带 utils）。要真正跑一遍脚本，需要完整源码树与 Python 环境：

```powershell
python llvm\utils\update_llc_test_checks.py --llc-binary llc.exe path\to\test.ll
```

预期现象：脚本会把 `; NOTE: Assertions have been autogenerated` 写在文件头，并在每条汇编指令前插入 `; CHECK:`。这一步不需要重编 llc，但需要完整源码树，因此本章标 `guided`。

## 可跑实验

### 实验 1：属性、.option arch、CFI 一次看全

```powershell
cd D:\Learning-with-Claude
.\compilers\riscv_backend\code\tools\run.ps1 labs\20-asmprinter.ps1
```

脚本在 `%TEMP%\rvlab20` 写 `mod.ll`，其中 `plain` 用模块级 `-mattr=+m`，`withv` 带 `target-features="+m,+v"`，`caller` 有一次调用。预期输出按顺序包含：

- `.attribute 4, 16` 与 `.attribute 5, "rv64i2p1_m2p0_zmmul1p0"`。
- `plain` 的 `lui a0, %hi(g)` / `ld a0, %lo(g)(a0)`。
- `withv` 前的 `.option push`、`.option arch, +d, +f, +v, ...`、函数后的 `.option pop`。
- `caller` 里的 `.cfi_def_cfa_offset 16`、`.cfi_offset ra, -8`、`.cfi_restore ra`。

### 实验 2：-asm-verbose=false 到底关了什么

同一脚本对 `mod.ll` 再跑一次 `-asm-verbose=false`。对比两次输出，差异只在注释行；`.cfi_*`、`.attribute`、`.option`、`.size` 全部保留。这个实验用来纠正"关掉 verbose 就没有 CFI"的误解。

### 实验 3（失败/边界）：.attribute 5 不含函数级扩展

在实验 1 的输出里搜索 `.attribute 5`，只会看到 `rv64i2p1_m2p0_zmmul1p0`，即使 `withv` 的函数体声明了 `+v`。这是设计使然：`emitAttributes` 用的是 TargetMachine 的 `MCSubtargetInfo`。如果你的工具链靠 `.attribute 5` 判断"文件是否含向量指令"，在这个例子上会得到错误结论；正确判据是函数体里有没有向量指令，或读 `.option arch` 的区间。

### 实验 4：.option norelax 抑制 RELAX 标记

```powershell
$bin = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin"
& "$bin\llvm-mc.exe" -triple=riscv64 -mattr=+relax -filetype=obj relax.s -o relax.o
& "$bin\llvm-readobj.exe" --relocations relax.o
```

预期只看到 `call foo` 那组有 `R_RISCV_RELAX`，`call bar` 没有。若把 `.option push/.option norelax/.option pop` 三行删掉，两条 call 都会带 `R_RISCV_RELAX`。这个对比实验比读文档更能说明"标记"而不是"禁令"的语义。

### 实验 5：链接后确认放松发生

```powershell
& "$bin\llvm-mc.exe" -triple=riscv64 -mattr=+relax -filetype=obj link.s -o link.o
& "$bin\ld.lld.exe" -o link.elf link.o -e _start
& "$bin\llvm-objdump.exe" -d link.elf
```

本机输出：

```text
0000000000011120 <_start>:
   11120: 008000ef     	jal	0x11128 <foo>
   11124: 00008067     	ret
```

`call foo`（8 字节）被放松成 `jal`（4 字节），所以下一条 `ret` 落在 `0x11124`。若没有 `+relax`，`jal` 会保持 `auipc` + `jalr` 两条。

## 源码落点

路径相对 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。

| 位置 | 符号 | 读它回答什么问题 |
|---|---|---|
| `RISCVAsmPrinter.cpp` | `RISCVAsmPrinter::emitInstruction`、`lowerPseudoInstExpansion`、`lowerToMCInst`、`EmitToStreamer` | 一条机器指令走哪条出口，伪指令在哪一步换掉 |
| `RISCVAsmPrinter.cpp` | `lowerOperand`、`lowerSymbolOperand`、`PrintAsmOperand`、`PrintAsmMemoryOperand` | 操作数与 `%hi`/`%lo`/`%pcrel_*` 的打印 |
| `RISCVAsmPrinter.cpp` | `emitStartOfAsmFile`、`emitEndOfAsmFile`、`emitAttributes`、`emitTargetFeaturePush`、`emitTargetFeaturePop` | `.attribute`、`.option push/arch/pop` 的来源 |
| `RISCVAsmPrinter.cpp` | `emitLpadAlignedCall` | `.option push`/`.option exact`/`.option pop` 的真实用例 |
| `MCTargetDesc/RISCVTargetStreamer.cpp` | `RISCVTargetAsmStreamer::emitDirectiveOptionNoRelax`、`emitDirectiveOptionArch`、`emitDirectiveOptionPush/Pop` | 这些指令最终打印成什么文本 |
| `MCTargetDesc/RISCVMCAsmInfo.cpp` | 汇编语法、指令对齐、注释字符 | 为什么 `.attribute` 用 tab 分隔、`#` 是注释 |
| `MCTargetDesc/RISCVELFStreamer.cpp` | `.riscv.attributes` 段的构造 | `.attribute 4/5` 如何写进 ELF |
| `RISCVFrameLowering.cpp` | `emitPrologue`、`emitEpilogue`、`determineCalleeSaves` | `.cfi_*` 的插入点与 CFA 偏移 |

## 常见坑

1. **以为 `-asm-verbose=false` 会去掉 CFI**。它只去掉注释行；`.cfi_*`、`.attribute`、`.option` 全部保留。
2. **把 `mv a0, a0` 当冗余指令**。它是 `addi a0, a0, 0` 的别名打印；未链接目标文件里立即数常为 0，等重定位填。
3. **用 `.attribute 5` 判断函数级扩展**。它来自 TargetMachine 的模块级 feature，函数级差异在 `.option arch`。
4. **以为 `.option norelax` 禁用整个文件的放松**。它只影响作用域内的重定位标记；实测 `call bar` 没有 `R_RISCV_RELAX`，作用域外的 `call foo` 仍然有。
5. **直接写 `%pcrel_lo(g)`**。`%pcrel_lo` 必须引用配对的 `.Lpcrel_hi*` 标签，否则链接器无法配对。
6. **在 `.option arch` 区间里调用外部汇编**。`.option push/pop` 是作用域，忘了 `pop` 会把后续函数也放进错误的 ISA 状态。
7. **对注释行做 CHECK 匹配**。函数名、基本块号、spill 大小都会变；测试里加 `-asm-verbose=false`。
8. **把 `tail foo` 当成真实指令**。汇编层的 `tail` 是伪指令形式，目标文件里是 `auipc` + `jalr`。
9. **忽略 `.attribute 4` 的栈对齐**。ABI 要求 16 字节，手写汇编与 C 交互时若不一致，会在调用边界崩。

## 自我检查

- [ ] 能按顺序说出 `emitInstruction` 的四个出口，并解释 `PseudoRET` 与 `PseudoTAIL` 分别走哪一条。
- [ ] 能解释 `%hi/%lo`、`%pcrel_hi/%pcrel_lo`、`%got_pcrel_hi` 三组修饰符对应的代码模型与重定位。
- [ ] 能说出 `.attribute 4` 与 `.attribute 5` 的含义，以及为什么函数级 `+v` 不出现在 `.attribute 5`。
- [ ] 能解释 `.option push/arch/pop` 的作用域语义，并说出 `emitTargetFeaturePush` 的比对逻辑。
- [ ] 能用 `llvm-mc` + `llvm-readobj` 复现 `.option norelax` 抑制 `R_RISCV_RELAX` 的实验。
- [ ] 能用 `-asm-verbose=false` 的 diff 说清它关闭了什么、保留了什么。
- [ ] 能说明 `update_llc_test_checks.py` 生成 CHECK 行时，哪些输出会导致测试抖动。
- [ ] 能解释为什么 `mv a0, a0` 与 `tail foo` 都不是"多出来的指令"。

## 延伸阅读

- RISC-V psABI 的 relocation 章节：`HI20/LO12_I/LO12_S` 与 `PCREL_HI20/PCREL_LO12_I` 的配对规则。
- RISC-V 汇编手册的 `.option` 章节：`push/pop`、`arch`、`relax/norelax`、`exact`。
- `llvm/test/MC/RISCV/` 下的 `attribute`、`option`、`reloc` 测试：直接验证打印机与流式输出。
- 本课程第 19 章（伪指令展开）：`PseudoRET` / `PseudoTAIL` 的展开时机，正好补全本章 20.1。
- 本课程第 21 章（重定位与链接）：把本章的 `%hi/%lo` 接到链接器视角。
- `llvm/utils/update_llc_test_checks.py` 的源码：CHECK 行的生成规则与 `--asm-verbose` 相关选项。

[上一章：伪指令展开与机器 pass](./19-pseudo-expansion.md) | [下一章：练习 D：调度与链接审计](./practice-d-scheduling-and-linking.md)