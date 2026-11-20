# Ch 26 — Target / MC / AsmPrinter：后端 review 的判断面

> **目标**：读到一个改 TableGen pattern、改 `X86MCCodeEmitter`、改 prologue/CFI、或改 `getRelocType` 的 PR 时，能说出它落在「target 描述 / MC / AsmPrinter」哪一层、这一层错了会在哪个阶段暴露、该用 `llvm-mc`、`llvm-objdump`、`llvm-readobj` 里哪一个去证伪；并且能解释为什么 `-verify-machineinstrs` 通过不等于这条路径是对的。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（LLVM 24.0.0git，assertions ON，注册了 X86/AArch64/RISCV）。构建目录缓存的 worktree 是 `a5eaf194d8b6`，与源码 hub 不是同一 commit；pass 名、flag 名与输出形态可信，行号以 7c7a 为准。本章命令都在 `pr-lane` 上真跑过；唯一的例外是链接一步，pr-lane 的 `ld.lld` 在本机起不来，改用系统 `/usr/bin/ld.lld`，26.4 有说明。

## 为什么需要这个？

Ch 25 结束时，函数已经是一堆物理寄存器的 `MachineInstr`：`MOV64rm`、`PUSH64r`、`CFI_INSTRUCTION`。CPU 一条都不认识这些词。从 `MachineFunction` 到 `.o` 之间还有三层，各有自己的失败方式：

1. **target 描述**：`.td` 的 `Pattern`、`Predicates`、编码位域，加上 `TargetInstrInfo`/`TargetRegisterInfo` 这些 C++ 表。错了的表现是「isel 选了不该选的指令」或「汇编器不认这条指令」。
2. **MC**：`MCInst`、`MCCodeEmitter`、`MCStreamer`、`MCExpr`、relocation。错了的表现是「字节不对」或「链接时才炸」。
3. **AsmPrinter**：把 `MachineInstr` 翻成 `MCInst` 与 directive（`.cfi_*`、`.globl`、`.section`）。错了的表现是「汇编文本对、unwind 信息错」。

同一个「load 全局变量」在 `.s` 里是 `g@GOTPCREL(%rip)`，在 `.o` 里是 `R_X86_64_REX_GOTPCRELX`，链接后可能已是 `leaq g(%rip)`。只看一层，就会对另外两层下错结论。

前置：Ch 23 的 pipeline 地图、Ch 24 的 `MachineInstr`/MIR、Ch 25 的 RA 之后状态。

## 26.0 先建立直觉：一条 `movq` 的四次转手

```text
  MachineInstr
    X86::MOV64rm $rip, 1, $noreg, target-flags(x86-gotpcrel) @g, $noreg
        │  AsmPrinter::emitInstruction（target 覆写）
        ▼
  MCInst    MOV64rm { $rip, 1, $noreg, <MCExpr: g@GOTPCREL>, $noreg }
        │  MCCodeEmitter::encodeInstruction
        ▼
  bytes + MCFixup
    48 8b 05 [00 00 00 00]   fixup: reloc_riprel_4byte_movq_load
        │  MCObjectStreamer → ELFObjectWriter
        ▼
  .o   48 8b 05 00 00 00 00   R_X86_64_REX_GOTPCRELX  g
        │  链接器（可 relax）
        ▼
  可执行文件   48 8d 05 b8 10 00 00   leaq 0x10b8(%rip), %rax
```

三次「信息形态改变」：

- `MachineInstr` → `MCInst`：丢掉 def/use、`killed`、`implicit` 这些机器层语义，只留「这条指令要哪些操作数」。
- `MCInst` → 字节：操作数塞进编码位域，塞不进去的部分变成 `MCFixup`（`llvm/include/llvm/MC/MCFixup.h:65`）。
- 字节 → relocation：还没解析的符号写成 relocation 条目，类型由 target 的 object writer 决定，链接器可以按规则改写字节。

本章每一节对应这张图里的一条箭头。

## 26.1 Target 层的六个类：谁决定什么

| 类 | 头文件 | 回答的问题 | review 先看哪里 |
| --- | --- | --- | --- |
| `TargetMachine` | `llvm/include/llvm/Target/TargetMachine.h:85` | 怎么建 subtarget、配 pipeline、出文件 | `addPassesToEmitFile`（`:474`）、`addAsmPrinter`（`:551`） |
| `TargetSubtargetInfo` | `llvm/include/llvm/CodeGen/TargetSubtargetInfo.h:72` | 这个**函数**用哪套 feature/CPU，指向哪组表 | `getInstrInfo`（`:111`）、`getRegisterInfo`（`:142`） |
| `TargetLowering` | `llvm/include/llvm/CodeGen/TargetLowering.h:4244` | IR 类型怎么合法化、调用怎么降 | `LowerFormalArguments`（`:5035`）、`LowerCall`（`:5330`） |
| `TargetRegisterInfo` | `llvm/include/llvm/CodeGen/TargetRegisterInfo.h:74` | 寄存器类、别名、callee-saved、call-preserved | `getCalleeSavedRegs`（`:332`）、`getCallPreservedMask`（`:362`） |
| `TargetInstrInfo` | `llvm/include/llvm/CodeGen/TargetInstrInfo.h:118` | 指令语义：copy、spill、branch、分析 | `copyPhysReg`（`:1117`）、`expandPostRAPseudo`（`:1266`） |
| `TargetFrameLowering` | `llvm/include/llvm/CodeGen/TargetFrameLowering.h:49` | 栈帧布局、prologue/epilogue、frame index 换算 | `emitPrologue`（`:211`）、`getFrameIndexReference`（`:338`） |

三个结构性事实决定 review 的提问方式：

**一、`TargetMachine` 是 per-module，`TargetSubtargetInfo` 是 per-function。** `getSubtargetImpl(const Function &F)`（`:164`）带函数参数，因为要读函数上的 `target-features`/`target-cpu`。「这条指令在 `+avx512f` 下合法」必须落到函数级：同一模块里两个函数可以有不同 subtarget。

**二、六个类是一张互相引用的表。** `TargetSubtargetInfo::getRegisterInfo` 是纯虚（`:142`），subtarget 不实现寄存器语义，只把 target 那一份 `X86GenRegisterInfo` 递出去。看到「新增寄存器类」的 diff，改动会同时落在 `.td`、`TargetRegisterInfo` 子类、以及可能的 `getCalleeSavedRegs` 三处；只改一处还能编译通过，通常说明有默认路径在兜底。

**三、`TargetLowering` 有两个层次。** 通用机器在 `TargetLoweringBase`（`:222`），target 特化在 `TargetLowering`（`:4244`）。前者管「`i128` 要不要拆」，后者管「这个 target 怎么 lower 一个 call」。

具体场景：PR 说「X86 上 `byval` 参数改走寄存器」。第一句要问的不是「快了多少」，而是「`LowerFormalArguments`（`:5035`）改了，`LowerCall`（`:5330`）改了吗」——两边必须对称，否则 caller 按寄存器传、callee 按栈读，各自都能通过自己的测试。

## 26.2 MC 层：五个类，一条装配线

| 组件 | 头文件 | 职责 | 关键接口 |
| --- | --- | --- | --- |
| `MCInst` | `llvm/include/llvm/MC/MCInst.h:188` | 无 def/use 的指令表示，操作数是 `MCOperand` 序列 | `addOperand`、`getOpcode` |
| `MCCodeEmitter` | `llvm/include/llvm/MC/MCCodeEmitter.h:22` | 把 `MCInst` 编成字节，编码不下的部分产出 `MCFixup` | `encodeInstruction` |
| `MCStreamer` | `llvm/include/llvm/MC/MCStreamer.h:222` | 接收指令/label/directive，分派到文本或目标文件 | `emitInstruction`（`:1137`）、`emitLabel`（`:500`）、`emitCFIStartProc`（`:1029`） |
| `MCAsmInfo` | `llvm/include/llvm/MC/MCAsmInfo.h:67` | 汇编语法策略：注释符、directive 拼写、对齐写法 | 子类 `X86MCAsmInfoGNUCOFF`（`X86MCAsmInfo.h:65`）、`AArch64MCAsmInfoELF`（`AArch64MCAsmInfo.h:39`） |
| `MCExpr` | `llvm/include/llvm/MC/MCExpr.h:34` | 未解析的符号表达式树 | `MCConstantExpr`（`:142`）、`MCSymbolRefExpr`（`:190`）、`MCTargetExpr`（`:464`） |

`MCStreamer` 是这层的分叉点：同一个 `MCInst` 走 `MCAsmStreamer` 得到 `.s`，走 `MCObjectStreamer`（`llvm/lib/MC/MCObjectStreamer.cpp:402`）得到字节与 relocation。**两条路的输出不是同一份信息的两种排版**：文本保留符号名和 `@PLT`、`@GOTPCREL` 修饰，目标文件把它们换成 relocation 类型加一个占位的 0。

`MCExpr` 存在的理由：汇编阶段不知道符号最终地址，`g@GOTPCREL` 必须是一个表达式节点；`MCValue`（`llvm/include/llvm/MC/MCValue.h:30`）是它在 fixup 求值时的「已知部分 + 符号部分」。

`MCAsmInfo` 常被忽略，它决定同一份 `MCInst` 在两种语法下的样子：

```bash
$ llc -x86-asm-syntax=intel -filetype=asm reloc.ll -o - | sed -n 12,16p
```

```text
	mov	rax, qword ptr [rip + g@GOTPCREL]
	mov	r10, qword ptr [rax]
	mov	r11, qword ptr [rip + ext@GOTPCREL]
	add	r10, qword ptr [r11]
	add	rdi, r10
```

AT&T 下同一段是 `movq	g@GOTPCREL(%rip), %rax`。两种写法编出同样的字节，因为编码器只看 `MCInst`，不看 `InstPrinter`。**改 `InstPrinter` 只影响文本，改 `MCCodeEmitter` 才影响字节**。

## 26.3 AsmPrinter：从 MachineFunction 到汇编

`AsmPrinter`（`llvm/include/llvm/CodeGen/AsmPrinter.h:95`）是 `MachineFunctionPass`，入口 `runOnMachineFunction`（`:478`）调用 `emitFunctionBody()`（`:480` 调用点，定义在 `llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp:2120`）。

`emitFunctionBody` 的主体是按 opcode 分派的 `switch`：`CFI_INSTRUCTION` → `emitCFIInstruction(MI)`（`:2223` 的 case，定义在 `:1461`）；`EH_LABEL`/`GC_LABEL` → `emitLabel`；`INLINEASM` → `emitInlineAsm`；`DBG_VALUE` → 只在 verbose 时作注释；其余走 `default: emitInstruction(&MI);`（`:2321`）。

`emitInstruction` 是虚函数（`AsmPrinter.h:671`），target 覆写它，把 `MachineInstr` 翻成 `MCInst`，再交给 `EmitToStreamer`：

```cpp
// llvm/lib/CodeGen/AsmPrinter/AsmPrinter.cpp:515
void AsmPrinter::EmitToStreamer(MCStreamer &S, const MCInst &Inst) {
  S.emitInstruction(Inst, getSubtargetInfo());
}
```

完整链条：`MachineInstr` → `X86AsmPrinter::emitInstruction`（处理 `MO_` 与 target flags）→ `MCInst` → `EmitToStreamer`（`:515`）→ `MCStreamer::emitInstruction`（`MCStreamer.h:1137`）→ `MCAsmStreamer`（文本）或 `MCObjectStreamer`（字节 + fixup）。

**实测：看 AsmPrinter 拿到的是什么（节选）。**

```bash
$ llc -mtriple=x86_64-unknown-linux-gnu -print-after=x86-asm-printer reloc.ll -o /dev/null
```

```text
# *** IR Dump After X86 Assembly Printer (x86-asm-printer) ***:
# Machine code for function mix: NoPHIs, TracksLiveness, NoVRegs, TiedOpsRewritten, TracksDebugUserValues
bb.0.entry:
  frame-setup PUSH64r undef $rax, implicit-def $rsp, implicit $rsp
  frame-setup CFI_INSTRUCTION def_cfa_offset 16
  renamable $rax = MOV64rm $rip, 1, $noreg, target-flags(x86-gotpcrel) @g, $noreg :: (load (s64) from got)
  ...
  CALL64pcrel32 target-flags(x86-plt) @callee, <regmask $bh $bl $bp ...>, implicit $rsp, implicit $ssp, implicit $rdi, implicit-def $rsp, implicit-def $ssp, implicit-def $rax
```

`-print-after` 打印的是**该 pass 之前**的状态，所以上面仍是 `PUSH64r`、`CFI_INSTRUCTION` 这些伪指令；要看 asm 文本直接看 `-filetype=asm`。`target-flags(x86-gotpcrel)` 与 `target-flags(x86-plt)` 就在操作数上（`MachineOperand::getTargetFlags`，`llvm/include/llvm/CodeGen/MachineOperand.h:232`），26.8 会看到它们如何决定 relocation 类型。

**踩雷现场**：pass 名写成 `asm-printer`，`llc` 不报错也不输出。

```bash
$ llc -mtriple=x86_64-unknown-linux-gnu -print-after=asm-printer reloc.ll -o /dev/null
$ echo $?
0
```

原因在注册名：

```cpp
// llvm/lib/Target/X86/X86PassRegistry.def:36
MACHINE_FUNCTION_PASS("x86-asm-printer", X86AsmPrinterPass())
// llvm/lib/Target/X86/X86AsmPrinter.cpp:1154
INITIALIZE_PASS(X86AsmPrinter, "x86-asm-printer", "X86 Assembly Printer", false,
```

generic 的 `asm-printer` 只是 `DEBUG_TYPE`（`AsmPrinter.cpp:144`），不是 pass 名。找真实名字让 `llc` 自己报：

```bash
$ llc -debug-pass=Structure -mtriple=x86_64-unknown-linux-gnu reloc.ll -o /dev/null 2>&1 | rg -o -- '-x86-asm-printer'
-x86-asm-printer
```

## 26.4 实测一：`.ll` → `.o` → 链接后的可执行文件

输入（`/tmp/llvm-ch26/reloc.ll`，节选）：

```llvm
@g = global i64 7, align 8
@ext = external global i64
declare i64 @callee(i64)

define i64 @mix(i64 %a, i64 %b, i64 %c, i64 %d, i64 %e, i64 %f) {
entry:
  %g0 = load volatile i64, ptr @g, align 8
  %x0 = load volatile i64, ptr @ext, align 8
  ...
  %r = call i64 @callee(i64 %s6)
  ret i64 %r
}
```

`volatile` 挡住把两次 `@g` 访问折叠成一次。

**第一层：汇编文本。** `llc -filetype=asm reloc.ll -o -` 的输出，符号和修饰符都在。

```text
	pushq	%rax
	.cfi_def_cfa_offset 16
	movq	g@GOTPCREL(%rip), %rax
	movq	ext@GOTPCREL(%rip), %r11
	...
	callq	callee@PLT
	.cfi_def_cfa_offset 8
	retq
```

**第二层：relocation。** 符号名从指令里消失。

```bash
$ llc -filetype=obj reloc.ll -o reloc.o && llvm-readobj -r reloc.o
```

```text
Relocations [
  Section (3) .rela.text {
    0x4 R_X86_64_REX_GOTPCRELX g 0xFFFFFFFFFFFFFFFC
    0xE R_X86_64_REX_GOTPCRELX ext 0xFFFFFFFFFFFFFFFC
    0x2E R_X86_64_PLT32 callee 0xFFFFFFFFFFFFFFFC
  }
  Section (7) .rela.eh_frame {
    0x20 R_X86_64_PC32 .text 0x0
    0x38 R_X86_64_PC32 .text 0x40
  }
]
```

`.rela.eh_frame` 的两条 `PC32` 是 CFI 的 FDE 指向函数体产生的。改 prologue 会同时改 `.text` 与 `.eh_frame` 的 relocation，这是容易漏的连带影响。

**第三层：`objdump` 不加 `-r` 时（节选）。**

```text
0000000000000000 <mix>:
       1:      	movq	(%rip), %rax            # 0x8 <mix+0x8>
       b:      	movq	(%rip), %r11            # 0x12 <mix+0x12>
      2a:      	movq	%rdi, (%rax)
      2d:      	callq	0x32 <mix+0x32>
```

这是本章的失败例子：把 `movq (%rip), %rax` 读成「从地址 0x8 加载」，把 `callq 0x32` 读成「调用下一条指令」。两个都错，那 4 个字节是占位符。加 `-r` 才是真实语义：

```text
       1: 48 8b 05 00 00 00 00         	movq	(%rip), %rax            # 0x8 <mix+0x8>
		0000000000000004:  R_X86_64_REX_GOTPCRELX	g-0x4
...
      2d: e8 00 00 00 00               	callq	0x32 <mix+0x32>
		000000000000002e:  R_X86_64_PLT32	callee-0x4
```

**第四层：链接之后。** `REX_GOTPCRELX` 的 `X` 后缀表示可 relax：GOT 间接访问被改写成 PC-relative 地址，长度不变（7 字节），操作码 `48 8b 05` 变 `48 8d 05`。

```bash
$ /usr/bin/ld.lld reloc.o main.o -o exe
$ llvm-objdump -d exe | sed -n '/<mix>:/,+5p'
```

```text
00000000002011e0 <mix>:
  2011e0: 50                           	pushq	%rax
  2011e1: 48 8d 05 b8 10 00 00         	leaq	0x10b8(%rip), %rax      # 0x2022a0 <g>
  2011e8: 4c 8b 10                     	movq	(%rax), %r10
  2011eb: 4c 8d 1d b6 10 00 00         	leaq	0x10b6(%rip), %r11      # 0x2022a8 <ext>
  2011f2: 4d 03 13                     	addq	(%r11), %r10
```

链接用的是系统 `/usr/bin/ld.lld`（Ubuntu LLD 14.0.0）：pr-lane 的 `ld.lld` 在本机报 `undefined symbol: _ZN4llvm18StringTableBuilder3addENS_19CachedHashStringRefEh`，`liblldELF.so` 与可执行文件不匹配。relax 规则来自 x86-64 psABI，与链接器版本无关。

| 层 | 看到的东西 | 能证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| `.s` | `g@GOTPCREL(%rip)`、`callee@PLT` | 符号修饰、CFI directive | 最终字节、是否 relax |
| `.o` + `-dr` | 字节 + `R_X86_64_*` | 编码、fixup→relocation 映射 | 链接后的寻址方式 |
| 可执行文件 | `leaq 0x10b8(%rip)` | 最终代码形态 | 源码原本写的是 GOT 访问 |

## 26.5 实测二：`llvm-mc -show-encoding` 与 fixup kind

`llc` 一次跑完整个后端，定位不到「编码」这一步。`llvm-mc` 只做汇编器：读 `.s`，出编码。

```bash
$ llvm-mc -triple=x86_64-unknown-linux-gnu -show-encoding enc.s
```

```text
	movq	g@GOTPCREL(%rip), %rax          # encoding: [0x48,0x8b,0x05,A,A,A,A]
                                        #   fixup A - offset: 3, value: g@GOTPCREL, kind: reloc_riprel_4byte_movq_load
	callq	callee@PLT                      # encoding: [0xe8,A,A,A,A]
                                        #   fixup A - offset: 1, value: callee@PLT, kind: FK_PCRel_4
	imulq	%rsi, %rdi                      # encoding: [0x48,0x0f,0xaf,0xfe]
	pushq	%rax                            # encoding: [0x50]
```

两个 `A` 的位置就是要被 relocation 填的 4 字节。fixup kind 是 MC 层的名字（`reloc_riprel_4byte_movq_load` 定义在 `llvm/lib/Target/X86/MCTargetDesc/X86FixupKinds.h:18`），到 ELF 层换成另一个名字：

```bash
$ llvm-mc -triple=x86_64-unknown-linux-gnu -filetype=obj enc.s -o enc.o && llvm-readobj -r enc.o
```

```text
  Section (3) .rela.text {
    0x3 R_X86_64_REX_GOTPCRELX g 0xFFFFFFFFFFFFFFFC
    0x8 R_X86_64_PLT32 callee 0xFFFFFFFFFFFFFFFC
  }
```

映射代码在 object writer 里：

```cpp
// llvm/lib/Target/X86/MCTargetDesc/X86ELFObjectWriter.cpp:228
    case X86::reloc_riprel_4byte_movq_load:
      return ELF::R_X86_64_REX_GOTPCRELX;
```

结论：**`fixup kind` 与 `relocation type` 不是同一个枚举，也不是一一对应**。`getRelocType64`（`:107`）里一个 `case` 映射到什么，取决于 specifier、是否 PC-relative、以及 32/64 位。新增寻址方式时，`MCCodeEmitter` 产出的 fixup kind 与 object writer 里的映射必须同时加；只加一边的表现是「汇编通过、链接报 unknown relocation」。

**relocation 甚至不是一条指令一个。** 同一份 IR 换 AArch64：

```bash
$ llc -mtriple=aarch64-unknown-linux-gnu -filetype=obj reloc.ll -o reloc-a64.o && llvm-readobj -r reloc-a64.o | head -10
```

```text
  Section (3) .rela.text {
    0x4 R_AARCH64_ADR_GOT_PAGE g 0x0
    0x8 R_AARCH64_ADR_GOT_PAGE ext 0x0
    0xC R_AARCH64_LD64_GOT_LO12_NC g 0x0
    0x10 R_AARCH64_LD64_GOT_LO12_NC ext 0x0
    0x34 R_AARCH64_CALL26 callee 0x0
  }
```

一次 GOT 访问在 AArch64 上是 `adrp` + `ldr` 两条指令、两条 relocation，必须配对：改了一条不改另一条，页内偏移就是错的。对应文本是 `adrp x8, :got:g` 与 `ldr x8, [x8, :got_lo12:g]`。RISC-V 同理，`R_RISCV_HI20`/`R_RISCV_LO12_I` 成对，libcall 以 `R_RISCV_CALL_PLT __muldi3` 出现：默认 `riscv64` triple 没开 M 扩展，`i64` 乘法降成 libcall，加 `-mattr=+m` 后这 3 条 relocation 全部消失。这里只取「libcall 也是 relocation」，legalizer 的细节看 `llvm_isel_sched_riscv` 课。

## 26.6 TableGen：pattern 的 predicate 与编码域

`Instruction` 的编码信息来自 `InstructionEncoding`（`llvm/include/llvm/Target/Target.td:545`），三个高频字段：`Predicates`（`:554`，isel 匹配的前提）、`DecoderNamespace`（`:551`）、`EncodingByHwMode`（`:598`，同一助记符在不同 `HwMode` 下用不同编码，`Inst` 与 `Size` 被忽略，改用 `Ts` 里的表）。

`Requires<[...]>`（`:854`）是给 `Predicates` 赋值的语法糖：

```tablegen
// llvm/lib/Target/X86/X86InstrSSE.td:1439
                    Requires<[HasAVX]>, Sched<[WriteCvtSS2SD]>;
```

三条 review 规则：

1. **predicate 是必要条件，不是充分条件。** `Requires<[HasAVX]>` 只说「有 AVX 才能选我」。放宽 predicate 时要问「哪些原本被排除的组合会进到这个 pattern，那些组合的编码域是否也支持」。
2. **isel 侧和汇编器侧是两套判断。** isel 用 `Predicates` 过滤候选；汇编器要在 `AsmParser` 里判断同一条指令能否汇编，判据可能不同（例如只在 64 位模式下可汇编）。目标文件编码正确、`llvm-mc` 却拒收同一条指令，是这类不一致的典型表现。
3. **predicate 依赖 subtarget 就写成依赖 subtarget 的形式。** `PredicateUsesSubtarget`（`:958`）与 `PredicateMethod`（`:955`）决定生成的判定函数签名带不带 `const MCSubtargetInfo &STI`。写成不带 STI、却在函数体里读 feature，函数级 `target-features` 就不生效。

「编码域」在 MC 层的落点是 `MCOI::OperandType`（`llvm/include/llvm/MC/MCInstrDesc.h:59`）与 `MCOperandInfo`（`:88`）：`OPERAND_REGISTER`、`OPERAND_IMMEDIATE`、`OPERAND_MEMORY`、`OPERAND_PCREL` 声明「这个位置允许什么类别的操作数」，编码器按它决定走哪条 encode 路径。把一个操作数从 `OPERAND_IMMEDIATE` 改成 `OPERAND_PCREL`，字节、fixup kind、relocation 三处都会变。

一条新指令的测试面：isel 用 `llc -mattr=...` 比对 `.s`/MIR 锁 pattern 与 predicate；汇编用 `llvm-mc -show-encoding` 或 `.s` round-trip 锁 AsmParser 的 operand 匹配；反汇编用 `-disassemble` 或 `llvm-objdump -d` 锁 decoder 与 `DecoderNamespace`；编码用 `-show-encoding` 的字节锁 `MCCodeEmitter` 的位域。PR 只补 isel 测试时，可以要求补汇编/反汇编测试——三条走的是不同代码路径。

## 26.7 ABI、调用约定、栈帧与 CFI

调用约定在 IR 层是 `CallingConv::ID`（`llvm/include/llvm/IR/CallingConv.h:151` 的 `X86_64_SysV = 78`），在 CodeGen 层是 `CCState`（`llvm/include/llvm/CodeGen/CallingConvLower.h:171`），中间是 `LowerFormalArguments`（`:5035`）与 `LowerCall`（`:5330`）。

三个集合容易混：caller-saved 来自 `getCallPreservedMask`（`TargetRegisterInfo.h:362`）的反面，不能跨调用存活；callee-saved 来自 `getCalleeSavedRegs`（`:332`），用到就必须在 prologue 保存；live-in 是函数入口实际活着的寄存器，是机器 verifier 与 RA 的输入。前两个必须是同一集合的两种视角：给 target 新增物理寄存器、只更新其中一个，RA 就会在调用点把一个实际上是 callee-saved 的寄存器当自由寄存器用；这类 bug 在单函数测试里看不出来，要跨函数才暴露。

栈帧由 `TargetFrameLowering` 负责。`PrologEpilogInserter` 在 `llvm/lib/CodeGen/PrologEpilogInserter.cpp:1180` 调用 `TFI.emitPrologue(MF, *SaveBlock)`，声明在 `TargetFrameLowering.h:211`。frame index 到实际地址的换算在 `getFrameIndexReference`（`:338`），它决定 `-128(%rsp,%rcx,8)` 这种寻址的基址是 SP 还是 FP。

**CFI 是 prologue 的影子。** 每条改变栈指针的指令都要有对应的 `.cfi_def_cfa_offset`：

```text
withframe:                              # @withframe
	.cfi_startproc
	subq	$392, %rsp                      # imm = 0x188
	.cfi_def_cfa_offset 400
```

`subq $392` 之后是 `400`（原来的 8 加 392）；`mix` 里 `pushq %rax` 之后是 `16`。这些 directive 是 `.eh_frame` 的内容来源：`AsmPrinter::emitCFIInstruction`（`AsmPrinter.cpp:1461`）把它们写成 `MCCFIInstruction`（`llvm/include/llvm/MC/MCDwarf.h:510`），再由 streamer 落成 FDE。改 prologue 的 PR（换 push/pop 保存 callee-saved、引入 frame pointer）必须同时检查：`emitPrologue`/`emitEpilogue` 的 CFI 更新与指令顺序是否一致；`getFrameIndexReference` 的基址假设是否还成立；`.eh_frame` 的 relocation 是否仍指向正确的函数体（26.4 那两条 `R_X86_64_PC32`）。漏第一条的表现是「程序能跑，backtrace 时栈帧错位」；`-verify-cfiinstrs` 能查 CFI 指令自身合法性，查不到「CFI 与真实栈操作不一致」。

## 26.8 relocation 与 `MO_` 操作数类型

`MachineOperand` 的类型枚举从 `llvm/include/llvm/CodeGen/MachineOperand.h:52` 起：`MO_Register`（`%rdi`）、`MO_Immediate`（`$392`）、`MO_FrameIndex`（`-128(%rsp,%rcx,8)`）、`MO_GlobalAddress`（`g@GOTPCREL(%rip)`）、`MO_ConstantPoolIndex`（`.LCPI0_0(%rip)`）、`MO_BlockAddress`、`MO_RegisterMask`（不直接出现，转成 `implicit` 列表）。

`MO_GlobalAddress` + `target-flags(x86-gotpcrel)` 才是 `g@GOTPCREL` 的完整信息。**类型和 target flags 是两个维度**：类型说「这是一个全局符号」，flags 说「用 GOT 的 PC-relative 方式取它」。看到「新增 target flag」的 diff，要追三处：

1. `X86BaseInfo.h`/`X86FixupKinds.h` 里的枚举；
2. `X86AsmPrinter`/`X86MCInstLower`（`llvm/lib/Target/X86/X86MCInstLower.cpp`）里 flag → `MCExpr` 修饰符的转换（决定 `.s` 长什么样）；
3. `X86ELFObjectWriter::getRelocType64` 里 flag → relocation 类型的映射（决定 `.o` 长什么样）。

只改前两处，汇编文本正确但 relocation 是默认类型，链接到错误地址；只改第三处，`.o` 正确但 `.s` 不可重汇编。

`MO_RegisterMask` 值得一提：它在 `MachineInstr` 里指向 target 的 call-preserved mask，打印和转 `MCInst` 时都不出现，只在 `CALL` 的 `implicit` 列表里显形（26.3 dump 里那串 `<regmask $bh $bl ...>`）。PR 动了 calling convention，`regmask` 内容会变而 `.s` 完全看不出来——这类改动只能靠 MIR 测试锁。

## 26.9 `-verify-machineinstrs` 之外还能查什么

`-verify-machineinstrs` 检查机器 IR 的不变量（tied operand、live-in、栈槽大小、implicit def），跑在 pass 之间。它通过只说明「机器 IR 自洽」，不说明编码正确、relocation 正确、CFI 正确。

| 手段 | 命令 | 能抓到 |
| --- | --- | --- |
| pass 级 dump | `-print-after=x86-asm-printer` | AsmPrinter 的输入、target flags |
| pipeline 名 | `-debug-pass=Structure` | 真实 pass 名与顺序 |
| 机器 IR 单测 | `-run-pass=machineverifier`（MIR 输入） | 手改 MIR 引入的不变量违规 |
| 汇编器单测 | `llvm-mc -show-encoding` / `-disassemble` | 编码器、fixup kind、语法 |
| 目标文件 | `llvm-readobj -r`、`llvm-objdump -dr` | relocation 类型、编码字节 |
| CFI | `-verify-cfiinstrs` | CFI 指令序列的合法性 |
| 链接后 | `llvm-objdump -d` 可执行文件 | relaxation、最终寻址方式 |

**实测：手改 MIR 触发 machine verifier。** 先取 MIR，再插入一条 tied operand 不一致的指令：

```bash
$ llc -mtriple=x86_64-unknown-linux-gnu -stop-before=x86-asm-printer -simplify-mir reloc.ll -o mix.mir
$ llc -run-pass=machineverifier mix_kill.mir -o /dev/null
```

```text
*** Bad machine code: Tied physical registers must match. ***
- function:    mix
- basic block: %bb.0 entry (0x5578d76de8e0)
- instruction: $rax = ADD64rr killed $r11(tied-def 0), $rax, implicit-def dead $eflags
- operand 0:   $rax
```

assertions 构建下 verifier 直接 abort（退出码 134），不会继续到 asm printer。它只检查「操作数之间是否自洽」，不检查「这条指令该不该在这里」。`-run-pass` 的 pass 名是 `machineverifier`（`llvm/lib/CodeGen/MachineVerifier.cpp:417` 注册）；写成 `machine-verifier` 会得到 `error: run-pass machine-verifier is not registered.`。

另外，`llc --print-passes` 不存在（那是 `opt` 的 flag）：

```text
llc: Unknown command line argument '--print-passes'.  Try: '/home/ubuntu/llvm/builds/pr-lane/bin/llc --help'
llc: Did you mean '--time-passes'?
```

要在 `llc` 里查 pass 名，用 `-debug-pass=Structure` 或 `-help-hidden`。

## 踩雷集锦

**踩雷 1：`-print-after=asm-printer` 静默无输出。** 直觉是「类名 kebab-case 就是 pass 名」；实际名字来自 `INITIALIZE_PASS` 的第二个参数（`X86AsmPrinter.cpp:1154` 的 `"x86-asm-printer"`）与新 PM 注册表（`X86PassRegistry.def:36`）。名字不匹配时 `-print-after` 不报错、不输出，退出码仍是 0。

**踩雷 2：读 `.o` 的 `objdump -d` 不加 `-r`。** `callq 0x32 <mix+0x32>` 看着像在调用下一条指令，`movq (%rip), %rax # 0x8` 看着像从地址 8 加载；那 4 个字节是占位符，真实语义在 relocation 里。

**踩雷 3：以为 `.s` 里的 `@GOTPCREL` 就是最终形态。** `R_X86_64_REX_GOTPCRELX` 可被链接器 relax 成 `leaq`，实测字节从 `48 8b 05` 变成 `48 8d 05`。讨论「这条指令有几次内存访问」时必须说明在哪一层。

**踩雷 4：改 prologue 不同步 CFI。** CFI 不是给调试器看的注释，它决定 `.eh_frame`，影响 unwind、`backtrace()`、采样 profiler。`-verify-cfiinstrs` 只查 CFI 指令自身合法。

**踩雷 5：把 `fixup kind` 当成 `relocation type`。** `kind: reloc_riprel_4byte_movq_load` 是 MC 层枚举（`X86FixupKinds.h:18`），ELF 层由 `getRelocType64`（`X86ELFObjectWriter.cpp:107`，映射在 `:228`）换成 `R_X86_64_REX_GOTPCRELX`；两者数量与粒度都不同。

## 动手练习

1. 把 `@g` 改成 `dso_local`，重走 26.4 的四层，说明哪一层变了、为什么 `R_X86_64_REX_GOTPCRELX` 变成 `R_X86_64_PC32`。
2. 加 `-mattr=+avx512f`，用 `-debug-pass=Structure` 找到 isel 后的指令，对照 `X86InstrAVX512.td` 的 predicate 说明它为什么被选中。
3. 用 `-stop-before=prolog-epilog` 取 MIR，把 `stackSize` 改大 8 字节，跑 `-run-pass=machineverifier` 记录报错类别，再看 `-filetype=asm` 的 `.cfi_def_cfa_offset` 是否跟着变。
4. 构造一个走 `MO_ExternalSymbol` 的 libcall，用 `llvm-readobj -r` 找出 relocation，说明为什么 `.s` 里看不到它的 GOT 修饰。

## 本章重点整理

- `MachineFunction` 到 `.o` 之间有三层：target 描述（`TargetInstrInfo`/`TargetRegisterInfo`/`TargetFrameLowering`）、MC（`MCInst`/`MCCodeEmitter`/`MCStreamer`/`MCExpr`）、AsmPrinter。
- `TargetMachine` 是 per-module，`TargetSubtargetInfo` 是 per-function；`getSubtargetImpl(const Function &)` 带函数参数就是为了函数级 feature。
- `MCStreamer` 是 MC 层的分叉点：`MCAsmStreamer` 出文本、`MCObjectStreamer` 出字节 + relocation，两条路的证据不能互换。
- `AsmPrinter::emitFunctionBody`（`AsmPrinter.cpp:2120`）按 opcode 分派伪指令，其余走 `emitInstruction`（`:2321`）→ `EmitToStreamer`（`:515`）→ `MCStreamer::emitInstruction`。
- pass 注册名是 `x86-asm-printer`（`X86PassRegistry.def:36`），`asm-printer` 只是 `DEBUG_TYPE`；写错名字时 `-print-after` 静默失败。
- `fixup kind`（`X86FixupKinds.h:18`）≠ `relocation type`（`X86ELFObjectWriter.cpp:228`）；一个 load 在 AArch64/RISC-V 上可能对应成对的 relocation。
- `R_X86_64_REX_GOTPCRELX` 可 relax，链接后 `movq` 变 `leaq`；讨论访存次数必须指明层次。
- CFI 是 prologue 的影子，改栈操作必须同步 `.cfi_def_cfa_offset`，漏改的表现是 unwind 错而不是 crash。
- `-verify-machineinstrs` 查机器 IR 自洽；`llvm-mc`、`llvm-readobj`、`-verify-cfiinstrs` 查更低层，覆盖的是不同失败面。

## 自我检核

- [ ] 能说出 `TargetSubtargetInfo::getRegisterInfo` 为什么是纯虚，以及它与 `TargetMachine` 的分工
- [ ] 能解释为什么 `AsmPrinter` 是 `MachineFunctionPass`，而 `MCStreamer` 不是 pass
- [ ] 给一条 `MOV64rm`，能写出它在 `MachineInstr`、`MCInst`、`.o`、链接后可执行文件四层的形态
- [ ] 能说明 `-print-after=asm-printer` 为什么没有输出，以及用什么命令找到真实名字
- [ ] 能解释 `R_X86_64_REX_GOTPCRELX` 与 `R_X86_64_PLT32` 分别对应源码里的哪种访问
- [ ] 能说出 AArch64 上一次 GOT 访问为什么需要两条 relocation，是哪两条
- [ ] 能举出一个 `-verify-machineinstrs` 通过、但编码仍然错误的场景
- [ ] 能说明改 prologue 时需要同步检查的三件事
- [ ] 能解释 `regmask` 为什么在 `.s` 里完全不可见，以及该用什么测试锁住它
- [ ] 拿到「新增 target flag」的 PR，能列出必须一起改的三处代码

## 延伸阅读

- **[LLVM 文档：The "MC" Layer](https://llvm.org/docs/CodeGenerator.html#the-mc-layer)**
  - **读哪里**：`MC` 整节，尤其是 `MCStreamer`、`MCInst`、`MCCodeEmitter` 的段落
  - **能学到什么**：官方对「汇编器/反汇编器/目标文件写入器共用一套 MC 表示」的设计说明，补上 26.2 的动机
  - **前提**：读过 Ch 24 的 `MachineInstr`；不需要先懂 ELF 格式
- **[LLVM 文档：TableGen Programmer's Reference](https://llvm.org/docs/TableGen/ProgRef.html)**
  - **读哪里**：`class`、`list`、`bit` 语法，以及 `Target.td` 里 `Instruction`、`Predicate` 的定义（`llvm/include/llvm/Target/Target.td:608`、`:854`）
  - **能学到什么**：`Predicates`、`EncodingByHwMode`、`PredicateMethod` 在 TableGen 层怎么组合；配合 Ch 11 的 `.td` → `.inc`
  - **前提**：读过 Ch 11；能读简单的 `.td`
- **[x86-64 psABI](https://gitlab.com/x86-psABIs/x86-64-ABI)**
  - **读哪里**：relocation 表里 `R_X86_64_GOTPCRELX`/`REX_GOTPCRELX` 的条目与「relaxable」说明
  - **能学到什么**：为什么链接器可以把 GOT 间接访问改写成 `lea`，改写的前提是什么（符号不可抢占等）
  - **前提**：知道 ELF relocation 的基本结构；26.4 的输出是例子
- **[LLVM 源码：`X86MCCodeEmitter.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/X86/MCTargetDesc/X86MCCodeEmitter.cpp)**
  - **读哪里**：`encodeInstruction`（约 `:1511`）与 `emitMemModRMByte` 一类辅助函数
  - **能学到什么**：编码域如何落成字节、fixup 在什么条件下产生、REX 前缀的拼接顺序
  - **前提**：读过 26.2 与 26.5；需要一点 x86 指令格式常识

把这一章压成一句话：**后端 review 的判断面不是「这段代码对不对」，而是「这个改动在哪一层留下痕迹，我用哪一层的工具去证伪」。**

→ [Ch 27 — LLVM review 制度：GitHub PR、reviewer 职责、LGTM、revert、RFC](./27-review-process-and-culture.md)
