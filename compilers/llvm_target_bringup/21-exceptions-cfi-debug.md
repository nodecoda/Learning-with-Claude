# Ch 21 — 异常、CFI 与调试信息

> **目标**：解释一个"没有 unwinder"的目标如何把 `invoke` 降成 `call`，如何从 `addInitialFrameState` 生成 CIE/FDE，并让 `llvm-readobj --unwind` 与 `llvm-dwarfdump` 读懂 MiniRV 的目标文件。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。

## 为什么需要这个？

到这里，MiniRV 已经能生成正确的机器码：帧、调用、参数都对。但一个目标文件除了代码，还有两份"给人看"的元数据：

1. **unwind 表**（`.eh_frame` / `.debug_frame`）：描述"函数执行到某个 PC 时，寄存器都保存在哪里"。调试器 backtrace、`perf` 栈回溯、`__builtin_return_address` 的调用者链、C++ 异常展开，全靠它。
2. **调试信息**（`.debug_info` / `.debug_line` / `.debug_loclists`）：描述"这条指令对应源码哪一行、变量现在在哪个寄存器"。

这两份东西和"能不能跑"无关，所以最容易被 bring-up 跳过。后果是：程序跑得好好的，但 `gdb` 里 `bt` 只有一层；`-g` 编译出来 `list` 不出源码；有人拿 `invoke` 试了一下，编译器直接崩。

异常模型更隐蔽。`invoke` 是一个契约，不是一条指令：调用可能异常，异常要走 landing pad。MiniRV 既没有 unwinder 也没有 personality routine，它必须**明确声明**这一点，否则通用 pipeline 按 ELF 默认值替它生成 DWARF EH 代码，然后撞上 `SelectionDAGBuilder::visitResume` 里的 `llvm_unreachable`。

这一章回答四个问题：invoke 怎么变成 call；CFI 从 C++ 到字节流经过哪几层；`.debug_frame` 和 `.eh_frame` 差在哪；一个目标为了有调试信息最少要提供什么。

## 先建立直觉

关键不是"目标支持哪种异常"，而是"有多少种异常准备 pass 被排进 pipeline"：

```
 IR: invoke / landingpad / resume
      |  CodeGenPassBuilder::addPassesToHandleExceptions
      |            |                         |
      |   supports DwarfCFI/SjLj?     两种都不支持 (MiniRV)
      |            |                         |
      |   DwarfEHPrepare/SjLjEHPrepare   LowerInvokePass (invoke -> call)
      v   (LSDA/personality)                 v
    SelectionDAG lowering            UnreachableBlockElim (landing pad 删掉)
```

再看一份真实的栈帧在 unwinder 眼里长什么样：

```
   CFA = 进入函数时的 x2
     |  saved x1 (ra)  <- CFA-8    .cfi_offset x1, -8
     |  saved x8 (s0)  <- CFA-16   .cfi_offset x8, -16
     v  局部对象
   当前 x2 = CFA-16
```

unwinder 不知道你的 prologue 长什么样。它只知道：在某个 PC 区间内，"CFA 是 x2+16，ra 在 CFA-8，s0 在 CFA-16"。这些规则就是 CFI。

## 主线：从 invoke 到 CIE/FDE

### 21.1 第一步：让目标承认自己没有异常

`TargetMachine` 的构造函数会按 triple 预设一组"能力位"。ELF 目标默认打开 DwarfCFI —— 对 MiniRV 也一样，因为 `Triple::getDefaultExceptionHandling()` 里 `isMiniRV()` 明确返回 `ExceptionHandling::DwarfCFI`：

```
// llvm/lib/TargetParser/Triple.cpp
if (isAArch64() || isX86() || ... || isMiniRV() || ...)
  return ExceptionHandling::DwarfCFI;
```

所以 MiniRV 必须在自己的构造函数里显式关掉：

```cpp
// llvm/lib/Target/MiniRV/MiniRVTargetMachine.cpp
MiniRVTargetMachine::MiniRVTargetMachine(...)
    : CodeGenTargetMachineImpl(...),
      TLOF(std::make_unique<TargetLoweringObjectFileELF>()),
      Subtarget(TT, CPU, FS, *this) {
  // There is no unwinder, no personality routine and no exception register.
  // The ELF default would enable DwarfCFI; disabling it makes the codegen
  // pipeline run LowerInvoke, which turns an invoke into a call and leaves the
  // landing pad unreachable.
  setSupportsExceptionModel(ExceptionHandling::DwarfCFI, false);
  setSupportsExceptionModel(ExceptionHandling::SjLj, false);
  initAsmInfo();
}
```

commit `9eeed8d`（`[MiniRV] Lower invokes for an unwindless target`）加的就是这两行 `setSupportsExceptionModel`。commit message 把因果链写得很清楚：

> The ELF default advertises DwarfCFI and SjLj exception models, so the codegen pipeline ran DwarfEHPrepare. A resume then had no `_Unwind_Resume` libcall and the pipeline fell through to SelectionDAGBuilder's `llvm_unreachable`.

也就是说：**这两行不是在关闭"异常支持"，而是在关闭"通用的异常准备代码"**。目标是让 pipeline 走另一条分支。

### 21.2 第二步：pipeline 选 LowerInvoke

`CodeGenPassBuilder::addPassesToHandleExceptions` 的逻辑非常直白：如果没有任何一种异常准备 pass 被排进来，那就认为"这个目标不需要异常"，用 `LowerInvoke` 兜底：

```cpp
// llvm/lib/Passes/CodeGenPassBuilder.cpp
  if (TM.supportsExceptionModel(ExceptionHandling::DwarfCFI)) {
    addFunctionPass(DwarfEHPreparePass(TM), PMW);
    HasEHPrep = true;
  }

  // If the target supports no exception model, nothing else lowers invokes.
  if (!HasEHPrep) {
    addFunctionPass(LowerInvokePass(), PMW);

    // The lower invoke pass may create unreachable code. Remove it.
    addFunctionPass(UnreachableBlockElimPass(), PMW);
  }
```

`LowerInvokePass` 本身只有一句核心逻辑，注释里写着它的适用场景（"for unwindless code generators"）：

```cpp
// llvm/lib/Transforms/Utils/LowerInvoke.cpp
static bool runImpl(Function &F) {
  bool Changed = false;
  for (BasicBlock &BB : F)
    if (InvokeInst *II = dyn_cast<InvokeInst>(BB.getTerminator())) {
      changeToCall(II);
      ++NumInvokes;
      Changed = true;
    }
  return Changed;
}
```

`changeToCall` 把 `invoke` 换成 `call`，只保留正常边（`to` 标签），`unwind` 边从此没有前驱。landing pad 块变成死块，紧接着的 `UnreachableBlockElimPass` 把它删掉。

我们实测一次，停在 `lower-invoke` 之后：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 -stop-after=lower-invoke inv.ll -o - | sed -n '11,20p'
  define void @invoke_test() personality ptr @__gxx_personality_v0 {
  entry:
    call void @may_throw()
    br label %ok

  ok:                                               ; preds = %entry
    ret void

  lpad:                                             ; No predecessors!
    %lp = landingpad { ptr, i32 }
```

两个细节值得盯住：

- `invoke` 已经变成 `call`，后面的 `br label %ok` 是 `changeToCall` 补出来的正常边。
- `lpad` 标着 `No predecessors!` —— 它已经死了，下一个 pass 才删；`personality` 属性还挂在函数上，但已经没有人引用它。

最终生成的汇编里，landing pad、personality、resume 全部消失：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 inv.ll -o - | sed -n '4,22p'
	.type	invoke_test,@function
invoke_test:                            # @invoke_test
	.cfi_startproc
# %bb.0:                                # %entry
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x1, 8(x2)
	.cfi_offset x1, -8
	jal	x1, may_throw
# %bb.1:                                # %ok
	ld	x1, 8(x2)
	.cfi_restore x1
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
.Lfunc_end0:
	.size	invoke_test, .Lfunc_end0-invoke_test
	.cfi_endproc
                                        # -- End function
```

**语义后果必须说清楚**：如果 `may_throw` 真的抛异常，MiniRV 上的控制流不会进入任何清理路径，而是沿着调用链一路返回或直接崩溃。所以"支持 `invoke`"这句话对 MiniRV 是不成立的：它只是**能编译**，不是**能执行**。拒绝执行语义的正确方式是在文档和诊断里写明白，而不是让编译器在 SelectionDAG 里断言失败。

### 21.3 CFI 的三层：MCCFIInstruction → `.cfi_*` → DW_CFA

同一份 CFI 在 LLVM 里有三种形态，搞清楚映射关系，读任何后端的帧代码都不再迷路：

```
 CFIInstBuilder::buildDefCFAOffset(16)        <- 机器层，目标代码只碰这里
        |  MCCFIInstruction::OpDefCfaOffset(16)
        v  MCAsmStreamer 打印 ".cfi_def_cfa_offset 16"
        |  MCDwarf 编码成 ULEB128 + opcode
        v
 DW_CFA_def_cfa_offset 16  (.eh_frame 字节流，unwinder 读)
```

`MiniRVFrameLowering` 只碰第一层，它调 `CFIInstBuilder`；`llc -o -` 看到的是第二层；`llvm-readobj --unwind` 解的是第三层。三层是同一份信息的不同编码，不是三份信息。

常用指令的对照表（`MCCFIInstruction` 的枚举名来自 `llvm/include/llvm/MC/MCDwarf.h`）：

| 汇编指示 | `MCCFIInstruction` | DWARF opcode | 作用 |
|---|---|---|---|
| `.cfi_startproc` / `.cfi_endproc` | 帧的开始/结束（不产生字节） | — | 界定一个 FDE 的 PC 范围 |
| `.cfi_def_cfa reg, off` | `OpDefCfa` | `DW_CFA_def_cfa` | CFA = reg + off，完全重定义 |
| `.cfi_def_cfa_offset off` | `OpDefCfaOffset` | `DW_CFA_def_cfa_offset` | 只改偏移，基址寄存器不变 |
| `.cfi_def_cfa_register reg` | `OpDefCfaRegister` | `DW_CFA_def_cfa_register` | 只改基址寄存器（切到帧指针） |
| `.cfi_offset reg, off` | `OpOffset` | `DW_CFA_offset` | reg 的旧值保存在 CFA + off |
| `.cfi_rel_offset reg, off` | `OpRelOffset` | `DW_CFA_offset` | off 相对**当前** CFA |
| `.cfi_adjust_cfa_offset d` | `OpAdjustCfaOffset` | `DW_CFA_def_cfa_offset` | 在现有偏移上做增量 |
| `.cfi_restore reg` | `OpRestore` | `DW_CFA_restore` | 恢复初始规则（等于"没保存"） |
| `.cfi_remember_state` / `.cfi_restore_state` | `OpRememberState` / `OpRestoreState` | `DW_CFA_remember_state` / `DW_CFA_restore_state` | CFI 状态栈 |
| `.cfi_personality` / `.cfi_lsda` | `OpPersonality` / `OpLsda` | augmentation `zPLR` | EH 元数据，MiniRV 不使用 |

MiniRV 的 prologue 只用了其中四个：`def_cfa_offset`、`offset`、`def_cfa_register`、`restore`。这正是"最小可用集合"。

### 21.4 CIE 与 FDE：一个函数一份，一个模块一份

`.eh_frame` 由两类记录组成：

- **CIE**（Common Information Entry）：整个模块共享的"初始规则"。版本、增强字符串、code/data 对齐因子、返回地址寄存器号、初始 CFA 规则都在这里。
- **FDE**（Frame Description Entry）：每个函数一份。它指回一个 CIE，给出自己的 PC 区间，然后从这个 CIE 的初始状态出发，用 `advance_loc`/`offset`/`restore` 描述函数体内的变化。

```
.eh_frame                                      .debug_frame 的差别
+--------------------------------+ 0x00
| CIE                            |   CIE id: 0        vs 0xffffffff
|  length = 0x10                 |   version: 1       vs 4
|  CIE id = 0                    |   PC 编码: pcrel4  vs absptr8
|  version = 1, aug = "zR"       |   重定位: 32_PCREL vs R_RISCV_64
|  code_align = 1, data_align=-8 |
|  return_reg = 1   (x1 / ra)    |
|  aug len=1, R=0x1b (pcrel4)    |
|  DW_CFA_def_cfa reg2 +0        |
+--------------------------------+ 0x14
| FDE                            |
|  length = 0x20                 |
|  CIE pointer = 0x18 (回指 0)   |
|  initial_location = 0  (4B)    |
|  address_range = 0x28          |
|  advance_loc 4                 |
|  def_cfa_offset 16             |
|  advance_loc 8                 |
|  offset x1, -8 / x8, -16       |
|  ... epilogue 反向 restore ... |
|  def_cfa_offset 0, padding     |
+--------------------------------+ 0x38
```

两个"初始规则"来自两处不同的代码，这是最容易漏的一步：

**(1) CFA 的初始值**。`MiniRVMCTargetDesc.cpp` 在创建 `MCAsmInfo` 时写入：

```cpp
static MCAsmInfo *createMiniRVMCAsmInfo(const MCRegisterInfo &MRI, ...) {
  MCAsmInfo *MAI = new MiniRVMCAsmInfo(TT, Options);
  // The unwinder starts from the stack pointer as it was on entry, before the
  // prologue has adjusted it.
  unsigned SP = MRI.getDwarfRegNum(MiniRV::X2, /*IsEH=*/true);
  MAI->addInitialFrameState(MCCFIInstruction::cfiDefCfa(nullptr, SP, 0));
  return MAI;
}
```

注意它先用 `getDwarfRegNum(X2, IsEH=true)` 把 LLVM 的寄存器编号换算成 **DWARF 寄存器号**。MiniRV 的 X2 是 2，所以 CIE 里是 `DW_CFA_def_cfa: reg2 +0`。如果寄存器表里没写 `DwarfNumbers`，这里会拿到 ~0U，CIE 就是错的。

**(2) 返回地址寄存器**。它不在 CFI 指令里，而在 CIE 的固定头部。来源是注册 MC 寄存器信息时的第二个参数：

```cpp
static MCRegisterInfo *createMiniRVMCRegisterInfo(const Triple &TT) {
  MCRegisterInfo *X = new MCRegisterInfo();
  InitMiniRVMCRegisterInfo(X, MiniRV::X1);   // 第二个参数 = 返回地址寄存器
  return X;
}
```

TableGen 生成的签名是 `InitMiniRVMCRegisterInfo(MCRegisterInfo *RI, unsigned RA, ...)`，`RA` 直接写进 CIE 的 `return_address_register`。MiniRV 传 X1，所以是 1。

**函数体内的变化**来自 `MiniRVFrameLowering`。prologue 里每一段 SP 调整后面都跟一条 CFI（`emitPrologue`）：

```cpp
  if (NeedsCFI)                                     // SP 动完，先改 CFA 偏移
    CFIInstBuilder(MBB, AfterSP, MachineInstr::FrameSetup)
        .buildDefCFAOffset(FirstAdjust);
  if (NeedsCFI) {                                   // 再登记 callee-saved 的位置
    CFIInstBuilder CFIBuilder(MBB, InsertPt, MachineInstr::FrameSetup);
    for (const CalleeSavedInfo &CS : MFI.getCalleeSavedInfo())
      CFIBuilder.buildOffset(CS.getReg(), MFI.getObjectOffset(CS.getFrameIdx()));
  }
  if (NeedsCFI)                                     // 有帧指针，CFA 换成 X8
    CFIInstBuilder(MBB, AfterSP, MachineInstr::FrameSetup)
        .buildDefCFARegister(MiniRV::X8);
```

顺序上有个坑：**spill 槽的偏移只有 PEI 布局完帧之后才确定**，所以 unwind 描述不能在发 store 的时候写，而要在 prologue 末尾统一补。`getObjectOffset` 能直接当 CFA 相对偏移用，是因为 PEI 布局时已经按"CFA = 入口 SP"算好了。

`NeedsCFI` 的来源是 `MachineFunction::needsFrameMoves()`：

```cpp
// MachineFunction::needsFrameMoves()
return getTarget().Options.ForceDwarfFrameSection ||
       F.needsUnwindTableEntry() ||
       !F.getParent()->debug_compile_units().empty();
// Function::needsUnwindTableEntry()
return hasUWTable() || !doesNotThrow() || hasPersonalityFn();
```

也就是说，**只要函数不是 `nounwind`，它就有 unwind 表**。这个条件比大多数人想的宽得多。

### 21.5 实测：读回 CIE 与 FDE

拿一个带 `uwtable` 的普通函数编译成目标文件：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 cfi.ll -o - | sed -n '5,22p'
caller:                                 # @caller
	.cfi_startproc
# %bb.0:
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x1, 8(x2)
	sd	x8, 0(x2)
	.cfi_offset x1, -8
	.cfi_offset x8, -16
	addiw	x8, x10, 1
	jal	x1, callee
	addi	x10, x8, 0
	ld	x8, 0(x2)
	.cfi_restore x8
	ld	x1, 8(x2)
	.cfi_restore x1
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
```

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj --unwind cfi.o | sed -n '1,33p'

File: cfi.o
Format: elf64-littleriscv
Arch: riscv64
AddressSize: 64bit
LoadName: <Not found>
.eh_frame section at offset 0x68 address 0x0:
  [0x0] CIE length=16
    version: 1
    augmentation: zR
    code_alignment_factor: 1
    data_alignment_factor: -8
    return_address_register: 1

    Program:
      DW_CFA_def_cfa: reg2 +0

  [0x14] FDE length=32 cie=[0x0]
    initial_location: 0x0
    address_range: 0x28 (end : 0x28)

    Program:
      DW_CFA_advance_loc: 4 to 0x4
      DW_CFA_def_cfa_offset: +16
      DW_CFA_advance_loc: 8 to 0xc
      DW_CFA_offset: reg1 -8
      DW_CFA_offset: reg8 -16
      DW_CFA_advance_loc: 16 to 0x1c
      DW_CFA_restore: reg8
      DW_CFA_advance_loc: 4 to 0x20
      DW_CFA_restore: reg1
      DW_CFA_advance_loc: 4 to 0x24
      DW_CFA_def_cfa_offset: +0
```

逐条对上：

- `return_address_register: 1` 来自 `InitMiniRVMCRegisterInfo(X, MiniRV::X1)`。
- `DW_CFA_def_cfa: reg2 +0` 来自 `addInitialFrameState`：CFA 一开始就是入口的 x2。
- `DW_CFA_advance_loc: 4` 对应 `addi x2, x2, -16`；计数单位是 `code_alignment_factor`，MiniRV 是 1（每条指令 4 字节）。
- `DW_CFA_offset: reg1 -8` 里的 `-8` 是 readobj 乘过 `data_alignment_factor` 之后的值：字节里存 `1`，因为 `1 * (-8) = -8`。**这是读 `.eh_frame` 十六进制时最常见的困惑**。
- `address_range: 0x28` 就是函数长度 40 字节。

### 21.6 `.debug_frame` 与 `.eh_frame`：同一份规则，两种容器

| | `.eh_frame` | `.debug_frame` |
|---|---|---|
| 消费者 / CIE id | unwinder、profiler / `0` | 调试器（`gdb`/`lldb`）/ `0xffffffff` |
| PC 起始地址编码 | `DW_EH_PE_pcrel｜sdata4`，4 字节 | `DW_EH_PE_absptr`，8 字节绝对值 |
| 重定位 / 生成时机 | `R_RISCV_32_PCREL`（不能是 ADD/SUB 符号差）/ 默认 | `R_RISCV_64` / `-force-dwarf-frame-section` |

两者由同一套 `MCDwarfFrameEmitter` 生成，靠一个 `IsEH` 布尔值切换格式（`llvm/lib/MC/MCDwarf.cpp`）。MiniRV 没有为 `.debug_frame` 写任何专门代码 —— 通用路径够了，但需要两处配合：

1. `MiniRVMCAsmInfo` 打开 `SupportsDebugInformation = true`，并指定 FDE 的符号修饰，避免 PC 相对字段被当成"符号差"：

```cpp
  // The FDE's initial location is a 32-bit PC-relative field.  Naming the
  // relocation directly keeps it from being confused with a symbol
  // difference, which would be emitted as an ADD/SUB pair.
  DwarfFDERelSymbolSpec = ELF::R_RISCV_32_PCREL;
```

2. `MiniRVELFObjectWriter::getRelocType` 认得这个 specifier（下一章细讲）。

实测两种容器的重定位差异：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 \
    -force-dwarf-frame-section -filetype=obj cfi.ll -o cfi_df.o
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-readobj --relocations cfi_df.o \
    | sed -n '/rela.eh_frame/,$p'
  Section (6) .rela.eh_frame {
    0x1C R_RISCV_32_PCREL .L0  0x0
  }
  Section (8) .rela.debug_frame {
    0x1C R_RISCV_32 .L0  0x0
    0x20 R_RISCV_64 .L0  0x0
  }
]
```

`0x20` 的 `R_RISCV_64` 就是 `.debug_frame` 的 PC 起始地址（8 字节绝对地址）。`0x1C` 的 `R_RISCV_32` 是 FDE 头部里的 CIE 指针：`.eh_frame` 里它是"往回数的距离"（`0x18`，纯算术，不需要重定位），`.debug_frame` 里它是"CIE 在节内的偏移"，所以要一个节内符号引用。

`llvm-dwarfdump --debug-frame` 读到的是 `Version: 4` 和 `Return address column: 1`，规则和 `.eh_frame` 一模一样 —— 因为两者本就出自同一个发射器，只是容器格式不同。

commit `8bffd72`（`[MiniRV] Emit real unwind tables and finish the profiling hooks`）之前，这些全是空的。可以直接验证：

```console
$ git -C /home/ubuntu/llvm/trees/dsl-tablegen show 8bffd72^:llvm/lib/Target/MiniRV/MiniRVFrameLowering.cpp | grep -c CFI
0
```

帧代码里一条 CFI 都没有，`.eh_frame` 里只有一个空 CIE 和一个空 FDE —— 链接得起来、跑得动，但调试器无法 unwind。这个 commit 一次补齐了四件事：prologue/epilogue 的规则、初始 CFA、FDE 的 `R_RISCV_32_PCREL`、以及 profile 描述符需要的 ADD/SUB 符号差重定位。

### 21.7 调试信息：目标需要提供什么

`.debug_info` 的绝大部分由通用代码（`DwarfDebug`/`DwarfCompileUnit`）生成，目标要做的其实很少：

1. **寄存器编号**：`MiniRVRegisterInfo.td` 里每个寄存器带 `DwarfNumbers`：

```tablegen
class MiniRVReg<bits<5> num, string n> : Register<n> {
  let Namespace = "MiniRV";
  let HWEncoding{4-0} = num;
  let DwarfNumbers = [num];      // <- X2 -> 2, X10 -> 10
}
```

这一行让 `X2 -> 2`、`X10 -> 10`，也让 CFI 和 `.debug_loclists` 里的寄存器号自动正确。

2. **frame base**：`TargetFrameLowering::getDwarfFrameBase` 的默认实现返回 `getFrameRegister(MF)`，MiniRV 没有覆盖它：

```cpp
// llvm/lib/CodeGen/TargetFrameLoweringImpl.cpp
TargetFrameLowering::DwarfFrameBase
TargetFrameLowering::getDwarfFrameBase(const MachineFunction &MF) const {
  const TargetRegisterInfo *RI = MF.getSubtarget().getRegisterInfo();
  return DwarfFrameBase{DwarfFrameBase::Register, {RI->getFrameRegister(MF).id()}};
}
```

`getFrameRegister` 无帧指针时返回 X2、有帧指针时返回 X8，所以 `DW_AT_frame_base` 会是 `DW_OP_reg2` 或 `DW_OP_reg8`。

3. **行号表**：只需要 `MCAsmInfo::SupportsDebugInformation = true` 和正确的 `CodePointerSize`。`.loc` 由 `AsmPrinter` 自动发。

实测一份带调试信息的最小 IR（`/tmp/dbg.ll`，函数 `f` 在第 2、3 行各有一条指令）：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llc -mtriple=minirv64 /tmp/dbg.ll -o - | sed -n '1,13p'
	.file	"dbg.ll"
	.text
	.globl	f                               # -- Begin function f
	.type	f,@function
f:                                      # @f
.Lfunc_begin0:
	.file	0 "/tmp" "t.c"
	.cfi_startproc
# %bb.0:                                # %entry
	.loc	0 2 3 prologue_end              # t.c:2:3
	addi	x10, x10, 1
	.loc	0 3 3                           # t.c:3:3
	jalr	x0, 0(x1)
```

`.loc 0 2 3` 是"文件 0、第 2 行、第 3 列"，`prologue_end` 标记 prologue 与函数体的分界 —— 调试器靠它决定断点应该落在哪。

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-dwarfdump --debug-info dbg.o \
    | sed -n '/DW_TAG_subprogram/,/DW_AT_type/p'
0x00000023:   DW_TAG_subprogram
                DW_AT_low_pc	(0x0000000000000000)
                DW_AT_high_pc	(0x0000000000000008)
                DW_AT_frame_base	(DW_OP_reg2 X2)
                DW_AT_name	("f")
                DW_AT_decl_file	("/tmp/t.c")
                DW_AT_decl_line	(1)
                DW_AT_type	(0x00000032 "long")
```

`DW_AT_frame_base (DW_OP_reg2 X2)` 是 `getFrameRegister` + `DwarfNumbers` 的直接产物。

**location list** 也是通用能力。给一个循环里的归纳变量写 `dbg.value`，`.debug_loclists` 就会出现：

```console
$ /home/ubuntu/llvm/builds/dsl-tablegen/bin/llvm-dwarfdump --debug-loclists loc2.o | tail -2
0x00000010: 
            DW_LLE_offset_pair     (0x000000000000000c, 0x0000000000000014): DW_OP_reg4 X4
```

`DW_OP_reg4 X4` 里的 4 同样是 `DwarfNumbers` 给的。

commit `0405b48`（`[MiniRV] Test variable locations and .debug_frame`）是纯测试提交，补了两条之前没被覆盖的路径：

- `dbg.declare` 落在 alloca 上 → `DW_AT_location (DW_OP_fbreg +8)`，同时 alloca 地址被物化为 `addi x3, x2, 8`；
- `-force-dwarf-frame-section` → `.debug_frame` 的 PC 起始地址是 `R_RISCV_64`，CFA 规则完整。

它的价值不在代码，而在**把"通用代码恰好能工作"变成"有回归测试保证"**。这是 bring-up 后期最划算的一类提交。

### 21.8 MiniRV 缺什么

对照一个成熟目标，MiniRV 在调试/异常这条线上是"够用但薄"：

| 能力 | MiniRV 现状 | 后果 |
|---|---|---|
| 寄存器 DWARF 编号 | 有（`DwarfNumbers = [num]`） | CFI、`.debug_loclists` 都正确 |
| frame base | 有（默认 `getFrameRegister`） | `DW_AT_frame_base` 正确 |
| `.eh_frame` 规则 | 有（`8bffd72` 起） | 调试器可 unwind，profiler 可用 |
| `.debug_frame` | 有（通用路径，`0405b48` 锁测试） | `gdb` 仍可 unwind |
| location list | 有（通用），但只有 `dbg.declare`/寄存器两种形态被测过 | 变量跨 spill 的复杂情形没验证 |
| personality / LSDA / entry values | 无 | 没有 C++ 异常和 `cleanup` 语义；优化后看不到 entry value |

给读者自己目标的检查清单：

1. 寄存器表有没有 `DwarfNum`？`Init<XYZ>MCRegisterInfo(X, RA)` 的 RA 传对了吗（传成 X0，CIE 的 `return_address_register` 就是 0）？
2. prologue 每改一次 SP 都补了 `def_cfa_offset` 吗？epilogue 反向补回 0 了吗？有帧指针时切了 `def_cfa_register` 吗？
3. FDE 的 PC 字段重定位类型对吗（`.eh_frame` 相对、`.debug_frame` 绝对）？搞反了链接器会报错。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| Triple 默认值 / 能力位 | `Triple.cpp`、`TargetMachine.h` | `getDefaultExceptionHandling`、`setSupportsExceptionModel` | `isMiniRV()` 默认 DwarfCFI，能力位是 bitmask |
| 目标构造函数 | `MiniRVTargetMachine.cpp` | `MiniRVTargetMachine::MiniRVTargetMachine` | 两行关闭 DwarfCFI/SjLj |
| pipeline 分叉 | `llvm/lib/Passes/CodeGenPassBuilder.cpp` | `addPassesToHandleExceptions` | `HasEHPrep` 决定 LowerInvoke |
| invoke 降级 / 崩溃点 | `Utils/LowerInvoke.cpp`、`SelectionDAGBuilder.cpp` | `changeToCall`、`visitResume` | 只保留正常边；`llvm_unreachable` 的位置 |
| 初始 CFA | `MiniRVMCTargetDesc.cpp` | `createMiniRVMCAsmInfo` | `addInitialFrameState` + `getDwarfRegNum` |
| 返回地址寄存器 | `MiniRVMCTargetDesc.cpp` | `createMiniRVMCRegisterInfo` | `InitMiniRVMCRegisterInfo(X, X1)` |
| 帧内规则 | `MiniRVFrameLowering.cpp` | `emitPrologue` / `emitEpilogue` / `restoreCalleeSavedRegisters` | `CFIInstBuilder` 的调用点；`needsFrameMoves` 的宽条件 |
| MC 编码 | `llvm/lib/MC/MCDwarf.cpp` | `FrameEmitterImpl::EmitFDE` | `IsEH` 分支、`DW_EH_PE_absptr` |
| 调试信息 | `llvm/lib/CodeGen/AsmPrinter/DwarfCompileUnit.cpp` | `DW_AT_frame_base` | `getDwarfFrameBase` |
| 寄存器编号 | `MiniRVRegisterInfo.td` | `MiniRVReg` (`DwarfNumbers`) | 编号来源 |

## 反直觉与踩雷

1. **错误直觉**：目标不支持异常，就不会生成 unwind 表。→ **正确认识**：`needsUnwindTableEntry()` 是 `hasUWTable() || !doesNotThrow() || hasPersonalityFn()`。普通函数默认不是 `nounwind`，所以**一定**有 `.eh_frame`。MiniRV 关掉的是"异常准备 pass"，不是"CFI 发射"。想关掉 CFI，得给函数加 `nounwind` 且不加 `uwtable` 且不编译调试信息。

2. **错误直觉**：`setSupportsExceptionModel(DwarfCFI, false)` 会关掉 `.debug_frame`/`.eh_frame`。→ **正确认识**：它只影响 `addPassesToHandleExceptions` 选择哪套 pass，以及 `AsmPrinter` 对 EH 语义的判断。CFI 是否发射由 `needsFrameMoves()` 决定，和这个 bit 无关。

3. **错误直觉**：`.cfi_offset x1, -8` 里的 `-8` 就是写进文件的值。→ **正确认识**：文件里存的是 `-8 / data_alignment_factor = 1`，unwinder 读的时候再乘回去。看十六进制时 `81 01` 是"寄存器 1，偏移单位 1"，不是"偏移 1 字节"。

4. **错误直觉**：`.eh_frame` 和 `.debug_frame` 内容可以互换。→ **正确认识**：CIE id（`0` vs `0xffffffff`）、PC 编码（pcrel 4 字节 vs absptr 8 字节）、重定位类型（`R_RISCV_32_PCREL` vs `R_RISCV_64`）、padding 规则都不同。MiniRV 的 `DwarfFDERelSymbolSpec` 就是为了让 `.eh_frame` 那个 4 字节字段拿到正确的重定位类型。

5. **错误直觉**：`invoke` 降成 `call` 之后，`personality` 属性没用了，应该删掉。→ **正确认识**：`LowerInvoke` 不改函数属性；属性留在那里不影响代码生成。真正要处理的是**语义**：异常穿过 MiniRV 帧是未定义行为，这一点必须写进 target 的文档/诊断，而不是让用户以为"编译通过就能用"。

6. **错误直觉**：帧指针模式下只要切 `def_cfa_register` 就够了。→ **正确认识**：MiniRV 的 epilogue 里先把 SP 从 X8 算回来（`addi x2, x8, -32`），然后 `def_cfa_register x2`，最后 `def_cfa_offset 0`。少一步，unwinder 在 epilogue 中间就会算出错误的 CFA。

## 进阶：再往深一层

**CFI 是一个状态机，不是一组属性**。每条 CFI 指令在当前状态上做增量修改，`advance_loc` 把"当前状态"绑定到一个 PC 区间。所以 `.cfi_restore x1` 的含义是"回到 CIE 里的初始规则"，而不是"删除 x1 的保存记录"。这也解释了为什么 epilogue 里必须显式 `restore`：否则 unwinder 会一直认为 ra 在 CFA-8。

**`DW_CFA_advance_loc` 是压缩编码**。opcode 高两位是 `01`，低六位直接放 delta（最多 63 个 code unit）。超过 63 就得用 `DW_CFA_advance_loc1/2/4`。MiniRV 的函数体小，实测输出里全是单字节 `44`/`48`/`50`；函数一大就会看到扩展形式，读十六进制时别以为是别的指令。

**`.eh_frame` 的 augmentation 字符串是能力协商**。"zR" 表示"我有一个 augmentation data 长度字段，并且带 R（FDE 地址编码）"。R 的值 `0x1b` = `DW_EH_PE_pcrel | DW_EH_PE_sdata4`，这就是为什么 PC 字段是 4 字节、且需要 PC 相对重定位。如果目标改成 `DW_EH_PE_absptr`，`.eh_frame` 就会需要 8 字节绝对重定位 —— 链接后无法共享、也无法放在只读段里做 relocation，这是所有现代 ABI 都用 pcrel 的原因。

**为什么 RA 的 DWARF 编号是 1 而不是 0**：DWARF 寄存器号是 ABI 的一部分，`x0` 是零寄存器，`ra` 是 `x1`。CFI 的 `return_address_register` 必须写 `ra` 的编号，unwinder 靠它找到调用者的 PC；写 0 会把零寄存器当返回地址，backtrace 立刻断掉。

**调试信息里的 `DW_OP_fbreg` 和 `DW_OP_regN` 是两种模型**：前者"变量在内存里，相对于 frame base 偏移"，后者"变量就在这个寄存器里"。`dbg.declare` 产生前者，`dbg.value` 产生后者；寄存器分配之后，同一个变量可能在多个位置之间迁移，这就是 location list。MiniRV 两条路径都能走通，但只有前者有测试。

## 本章重点整理

- 关闭异常模型 = 关闭"异常准备 pass"，pipeline 于是跑 `LowerInvokePass` + `UnreachableBlockElimPass`，`invoke` 变 `call`，landing pad 变死块。
- MiniRV 关 DwarfCFI/SjLj 的两行在 `MiniRVTargetMachine` 构造函数里（commit `9eeed8d`）；CFI 三层是 `CFIInstBuilder` → `.cfi_*` → `DW_CFA_*`。
- CIE 提供初始规则（CFA、返回地址寄存器、对齐因子），FDE 提供 PC 区间和增量规则；初始 CFA 来自 `addInitialFrameState`，返回地址寄存器来自 `InitMiniRVMCRegisterInfo` 的第二个实参。
- `needsFrameMoves()` 的触发条件很宽：非 `nounwind` 的函数就有 unwind 表。
- `.eh_frame` 用 PC 相对 4 字节 + `R_RISCV_32_PCREL`；`.debug_frame` 用 8 字节绝对 + `R_RISCV_64`。
- 目标的调试信息只需要两样：`DwarfNumbers` 和 `getFrameRegister`；MiniRV 没有 personality/LSDA/entry value。

## 自我检核

- 不看源码，说出 `setSupportsExceptionModel` 的两个调用点、它们影响的那个布尔变量、以及它决定的两个 pass。
- `LowerInvoke` 之后 `lpad` 块为什么是 `No predecessors!`？谁删掉它？如果删之前有人 `br` 到 `lpad` 会怎样？
- CIE 里的 `return_address_register: 1` 是哪个函数、哪个参数写进去的？改成 `MiniRV::X0` 会看到什么现象？
- `.cfi_offset x1, -8` 在文件里存的是什么？`data_alignment_factor` 起了什么作用？
- 同一个函数编译两次（默认 / `-force-dwarf-frame-section`），两个 `.rela.*` 各有什么重定位？为什么不一样？
- 给函数加 `nounwind`、不加 `uwtable`、不编译调试信息，`.eh_frame` 还会有 FDE 吗？为什么？

## 延伸阅读

- **[LLVM Exception Handling](https://llvm.org/docs/ExceptionHandling.html)** — LLVM 项目
  - **读哪里**：`invoke`/`landingpad`/`resume` 的语义，以及 "CodeGen" 一节对 target exception model 的描述。
  - **能学到什么**：为什么"不支持异常"必须显式声明，以及 `LowerInvoke` 在语义上做了什么妥协。
  - **前提**：读过本章 21.1–21.2；Ch 16 已经讲过 `callsEHReturn`/`callsUnwindInit` 的灰区。

- **[DWARF 5 Standard](https://dwarfstd.org/doc/DWARF5.pdf)** — DWARF Standards Committee
  - **读哪里**：第 6.4 节 "Call Frame Information"（CIE/FDE 布局、`DW_CFA_*` 指令集）。
  - **能学到什么**：`advance_loc` 的编码、`data_alignment_factor` 的语义、状态机模型。
  - **前提**：能读二进制格式文档；配合本章的 `--unwind` 输出对照看。

- **[LLVM MC: MCDwarf.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/MC/MCDwarf.cpp)** — LLVM 项目
  - **读哪里**：`FrameEmitterImpl::EmitCIE` / `EmitFDE` / `emitCFIInstruction`。
  - **能学到什么**：`IsEH` 分支如何切换两种容器，PC 编码和重定位如何决定。
  - **前提**：读过本章 21.6；能对照 `llvm-readobj --hex-dump` 看字节。

- **[RISC-V 后端全解](../riscv_backend/README.md)** — 本仓库课程
  - **读哪里**：帧布局、`RISCVFrameLowering` 的 CFI、`hasFP` 与 realign 的组合处理。
  - **能学到什么**：成熟后端在同样的 CIE/FDE 契约下多处理了哪些情况。
  - **前提**：本章的 MiniRV 帧代码已看懂。

→ [Ch 22 对象文件与链接](./22-object-file-and-linking.md)
