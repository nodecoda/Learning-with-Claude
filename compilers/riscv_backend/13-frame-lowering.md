# Ch 13 栈帧：从 caller 的 16 字节说起

> 目标：把 RISC-V 后端的栈帧拆成五件可以分别观察的事：帧大小与对齐、callee-saved 判定、prologue/epilogue 发射、frame index 消除、调试信息。读完本章，你能对着任意一段 RISC-V 汇编说出“这 16 字节是谁算的、这个 `.cfi_*` 是谁发的、这个 `s0` 到底是帧指针还是普通被调用者保存寄存器”。

> 环境：Windows 10 x86_64；LLVM 23.1.4 预编译 Optimized build；源码为同版本 sparse clone（`llvmorg-23.1.4`）。命令在仓库根目录 `D:\Learning-with-Claude` 下执行，统一入口是 `compilers/riscv_backend/code/tools/run.ps1`。本章所有汇编都来自本机 `llc` 的重新运行；需要重新构建 `llc` 才能观察的改动会标 `guided`。

## 为什么

栈帧是后端里最容易被“看起来懂了”掩盖的一层。你看到 `addi sp, sp, -16` 会以为已经了解栈帧，但这条指令背后至少有四个独立决定：ABI 要求 16 字节对齐；`ra` 需要保存，因为函数里有调用；保存 `ra` 需要一个 8 字节槽；这个槽放在帧的哪个偏移。任何一个决定变了，输出都会变。

更麻烦的是同一个寄存器名会扮演不同角色。`s0` 在 `-frame-pointer=all` 下是帧指针，在 `-O2` 的大栈帧函数里却可能只是保存实参的普通 callee-saved GPR。只看“有没有 `s0`”会得出错误结论，必须看它是否被写成 `addi s0, sp, ...`，以及 `.cfi_def_cfa` 是否切到 `s0`。

本章用一条真实调用链做主线：`caller` 调用 `mul_add`。它只有 16 字节栈帧，却完整包含 `addi`、`sd ra`、`ld ra` 和四条 `.cfi_*`。把这 16 字节逐行拆开，再扩展到 5000 字节、12000 字节、动态 alloca 和 RVV 可伸缩对象，栈帧的边界就清楚了。

## 主线

### 13.1 栈帧对象的生命周期：五个阶段

RISC-V 的栈帧不是在一个函数里一次算完的，而是分布在五个阶段。每个阶段改的东西不同，出问题时的症状也不同。

| 阶段 | 主要函数 | 输入 | 输出 | 典型症状 |
| --- | --- | --- | --- | --- |
| 帧对象收集 | `MachineFrameInfo`、指令选择 | 局部变量、spill、调用 | frame index 列表 | 多出莫名的 spill 槽 |
| callee-saved 判定 | `determineCalleeSaves` | 寄存器使用信息 | `SavedRegs` 位向量 | 少存/多存 `s0`、`fs0` |
| 帧最终化 | `processFunctionBeforeFrameFinalized` | frame index、RVV 信息 | scavenging 槽、RVV 大小 | 大栈帧缺 scratch 槽 |
| 布局计算 | `determineFrameLayout` | `MFI.getStackSize()` | 对齐后的 `StackSize` | 栈不是 16 的倍数 |
| 发射与消除 | `emitPrologue` / `emitEpilogue` / `eliminateFrameIndex` | 最终布局 | 汇编与 CFI | 偏移越界、CFI 错位 |

源码依据：`RISCVFrameLowering.cpp` 里的 `determineCalleeSaves`、`processFunctionBeforeFrameFinalized`、`determineFrameLayout`、`emitPrologue`、`emitEpilogue`；`RISCVRegisterInfo.cpp` 里的 `eliminateFrameIndex`。这些函数的分工不要混：`determineFrameLayout` 只负责把大小对齐，不负责发指令；`emitPrologue` 才把大小变成 `addi` 或 `li + add`。

`processFunctionBeforeFrameFinalized` 里有一个容易忽略的细节：它用 `isInt<11>(MFI.estimateStackSize(MF))` 判断是否需要 emergency spill slot，而不是 12 位。源码注释给的理由是 `estimateStackSize` 曾观察到低估最终栈大小，所以留出余量。这个槽和“栈帧本身多大”是两件事，但在汇编里会表现为额外的局部空间。

### 13.2 16 字节对齐：getABIStackAlignment 与 determineFrameLayout

RISC-V 的栈对齐不是一个全局常数，而是由 ABI 决定。`getABIStackAlignment` 的真实逻辑是：

| ABI | 栈对齐 |
| --- | --- |
| `ILP32E` | 4 字节 |
| `LP64E` | 8 字节 |
| 其他（`ILP32`、`LP64`、`ILP32F/D`、`LP64F/D`） | 16 字节 |

`RISCVFrameLowering` 的构造函数把这个值同时传给 `TargetFrameLowering` 的栈对齐和 transient stack alignment。到了 `determineFrameLayout`，帧大小会被 `alignTo(FrameSize, StackAlign)` 向上取整。也就是说，`caller` 需要保存一个 8 字节的 `ra`，但最终帧是 16 字节，不是 8。

实测 `caller`（`llc -mtriple=riscv64 -mattr=+m,+a,+f,+d -O2`）：

```asm
caller:                                 # @caller
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	li	a1, 3
	call	mul_add
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
```

这段输出里没有 `s0`，因为默认的 frame pointer 策略是 `non-leaf`（只有非叶子函数才可能保留帧指针），而 `caller` 虽然非叶子，却没有动态 alloca、没有可变大小对象、没有取帧地址，所以不需要专用帧指针。`ra` 仍然必须保存，因为 `call` 会覆盖它；保存 `ra` 之后，帧大小按 16 对齐，于是空出来的 `0(sp)` 不会被使用。

### 13.3 caller 逐行：addi、sd、ld、cfi 的来源

把 `caller` 的 12 行拆开，可以看到每一行都由不同的组件产生：

| 汇编 | 谁产生 | 为什么是这个值 |
| --- | --- | --- |
| `addi sp, sp, -16` | `emitPrologue` → `allocateStack` → `RISCVRegisterInfo::adjustReg` | 16 是 `determineFrameLayout` 对齐后的帧大小；`-16` 落在 12 位有符号立即数内，所以是一条 `ADDI` |
| `.cfi_def_cfa_offset 16` | `allocateStack` 里的 `CFIBuilder.buildDefCFAOffset(RealStackSize)` | 进入函数后，CFA 从“调用者的 `sp`”变成“当前 `sp + 16`” |
| `sd ra, 8(sp)` | `spillCalleeSavedRegisters` / `assignCalleeSavedSpillSlots` | `ra` 是 callee-saved；它的槽由帧布局放在 `sp + 8` |
| `.cfi_offset ra, -8` | `emitPrologue` 里遍历 `CalleeSavedInfo` 的 `CFIBuilder.buildOffset(Reg, Offset)` | 这里打印的是“相对于 CFA 的偏移”，CFA 是 `sp + 16`，`ra` 在 `sp + 8`，所以是 `-8` |
| `li a1, 3` | 指令选择里的常量选择（`selectImm` / `movImm`） | 第二个实参是常数 3，与栈帧无关 |
| `call mul_add` | 调用 lowering + `PseudoCALL` | 伪指令在后续展开成 `auipc + jalr` 或 `jal` |
| `ld ra, 8(sp)` | `restoreCalleeSavedRegisters` | 与 `sd` 对称，读回同一个槽 |
| `.cfi_restore ra` | 恢复路径的 `CFIBuilder.buildRestore` | 告诉 unwinder：`ra` 恢复到调用者保存的值 |
| `addi sp, sp, 16` | `emitEpilogue` → `deallocateStack` | 恢复调用者的 `sp` |
| `.cfi_def_cfa_offset 0` | `deallocateStack` 里的 CFI | CFA 回到“调用者的 `sp`”，偏移为 0 |
| `ret` | 返回指令选择 | `jalr zero, 0(ra)` 的别名 |

这里有两个数字容易混：`sd ra, 8(sp)` 里的 `8` 是**当前 `sp` 的偏移**；`.cfi_offset ra, -8` 里的 `-8` 是**相对 CFA 的偏移**。二者符号和基准都不同。把 `.cfi_offset` 当成“栈槽偏移”去反推帧布局，会得到错误结论。

`.cfi_startproc` 和 `.cfi_endproc` 不是栈帧 lowering 发的，而是 AsmPrinter 在函数边界发的；`needsDwarfCFI(MF)` 为真时，栈帧 lowering 才插入中间的 `.cfi_def_cfa_offset`、`.cfi_offset`、`.cfi_restore`。源码依据：`RISCVFrameLowering.cpp` 里的 `needsDwarfCFI` 与 `CFIInstBuilder` 调用点，以及 `RISCVAsmPrinter.cpp` 的函数边界处理。

### 13.4 -frame-pointer=all/none 与 callee-saved 判定

把同一个 `caller` 加上 `-frame-pointer=all`，输出立刻变成：

```asm
caller:                                 # @caller
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	sd	s0, 0(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	.cfi_offset s0, -16
	addi	s0, sp, 16
	.cfi_def_cfa s0, 0
	li	a1, 3
	call	mul_add
	.cfi_def_cfa sp, 16
	ld	ra, 8(sp)                       # 8-byte Folded Reload
	ld	s0, 0(sp)                       # 8-byte Folded Reload
	.cfi_restore ra
	.cfi_restore s0
	addi	sp, sp, 16
	.cfi_def_cfa_offset 0
	ret
```

新增的三行是关键：`sd s0, 0(sp)` 保存旧的 `s0`；`addi s0, sp, 16` 让 `s0` 指向调用者的 `sp`，也就是 CFA；`.cfi_def_cfa s0, 0` 把 CFA 的基准从 `sp` 切成 `s0`。这时 `s0` 才是帧指针。

`-frame-pointer=none` 对 `caller` 没有影响，因为它本来就不需要帧指针。但对动态 alloca 函数 `dyn`，即使显式写 `-frame-pointer=none`，输出仍然有：

```asm
dyn:                                    # @dyn
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -16
	.cfi_def_cfa_offset 16
	sd	ra, 8(sp)                       # 8-byte Folded Spill
	sd	s0, 0(sp)                       # 8-byte Folded Spill
	.cfi_offset ra, -8
	.cfi_offset s0, -16
	addi	s0, sp, 16
	.cfi_def_cfa s0, 0
	addi	a0, a0, 15
	andi	a0, a0, -16
	sub	a0, sp, a0
	mv	sp, a0
	call	use
	addi	sp, s0, -16
```

原因是 `hasFPImpl` 的判定里包含 `MFI.hasVarSizedObjects()`。动态 alloca 让 `sp` 在函数体里变化，不能再拿 `sp` 作为稳定的帧基准，所以帧指针是强制的，`-frame-pointer=none` 不能覆盖这个正确性条件。源码依据：`hasFPImpl` 的四个条件——`DisableFramePointerElim`、`hasStackRealignment`、`hasVarSizedObjects`、`isFrameAddressTaken`——再加上“最大调用帧超过 2047”时的保守保留。

callee-saved 判定则由 `determineCalleeSaves` 完成。它先调用 `TargetFrameLowering::determineCalleeSaves`，再修正 RISC-V 向量的子寄存器语义：只有当 `v24m2` 的两个子寄存器都被标记时，才保留整个 `v24m2`，而不是“任一子寄存器被标记就保留父寄存器”。之后如果 `hasFP(MF)`，无条件把 `ra` 和 `s0` 加入 `SavedRegs`。这解释了为什么 `-frame-pointer=all` 的叶子函数 `mul_add` 也会出现完整的 16 字节 prologue：它没有调用，也没有局部变量，但帧指针策略强制保留了 `s0` 和 `ra`。

### 13.5 大栈帧：拆分 SP 调整与 li+add

5000 字节局部数组的 `big` 函数，实测输出是：

```asm
big:                                    # @big
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -2032
	.cfi_def_cfa_offset 2032
	sd	ra, 2024(sp)                    # 8-byte Folded Spill
	sd	s0, 2016(sp)                    # 8-byte Folded Spill
	.cfi_offset ra, -8
	.cfi_offset s0, -16
	addi	sp, sp, -2048
	addi	sp, sp, -944
	.cfi_def_cfa_offset 5024
	mv	s0, a0
	addi	a0, sp, 8
	call	use
	add	a0, a0, s0
	addi	sp, sp, 2032
	addi	sp, sp, 960
	.cfi_def_cfa_offset 2032
	ld	ra, 2024(sp)                    # 8-byte Folded Reload
	ld	s0, 2016(sp)                    # 8-byte Folded Reload
	.cfi_restore ra
	.cfi_restore s0
	addi	sp, sp, 2032
	.cfi_def_cfa_offset 0
	ret
```

这里有两个独立现象。第一，SP 调整被拆成三次：`-2032`、`-2048`、`-944`。`getFirstSPAdjustAmount` 在“帧大小超出 12 位有符号范围且存在 callee-saved 寄存器”时返回 `2048 - StackAlign`，本机是 `2048 - 16 = 2032`；先把 `sp` 拉下 2032，spill 的偏移就落在单条 `sd` 能编码的范围内。剩下的 `5024 - 2032 = 2992` 又被 `adjustReg` 拆成 `-2048` 和 `-944`，因为 `-2048` 是 12 位有符号立即数的下界，再剩下的 `-944` 也能编码。

第二，`s0` 在这里不是帧指针。它被写成 `mv s0, a0`，用来跨 `call use` 保存实参 `a`；没有 `addi s0, sp, ...`，也没有 `.cfi_def_cfa s0, ...`。判断帧指针要看“是否用它作为 CFA 基准”，而不是看它有没有被保存。

12000 字节的 `huge` 更能暴露 `li + add` 路径：

```asm
huge:                                   # @huge
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -2032
	.cfi_def_cfa_offset 2032
	sd	ra, 2024(sp)                    # 8-byte Folded Spill
	.cfi_offset ra, -8
	lui	a0, 2
	addi	a0, a0, 1808
	sub	sp, sp, a0
	.cfi_def_cfa_offset 12032
	addi	a0, sp, 16
	call	use
	lui	a1, 2
	addi	a1, a1, 1808
	add	sp, sp, a1
	.cfi_def_cfa_offset 2032
	ld	ra, 2024(sp)                    # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 2032
	.cfi_def_cfa_offset 0
	ret
```

第二次调整量是 `12032 - 2032 = 10000`，超出 `adjustReg` 的两条 `ADDI` 范围（源码条件是 `Val > -4096 && Val <= 2 * MaxPosAdjStep`），于是走最后的 fallback：`movImm` 把 10000 物化到临时寄存器，再发 `SUB`。`movImm` 在汇编层展开成 `lui a0, 2; addi a0, a0, 1808`（`2 << 12 + 1808 = 10000`），所以你在最终汇编里看到的是 `lui + addi + sub`，而不是字面上的 `li + add`。这是伪指令展开后的形态，不是另一条算法。

`RISCVRegisterInfo::eliminateFrameIndex` 也会在消除 frame index 时调用 `adjustReg`。如果局部对象的偏移超出 12 位，它先把低 12 位折进访存指令的立即数，剩下的部分用 `adjustReg` 生成基址调整。源码依据：`eliminateFrameIndex` 里的 `Lo12`、`Val - Lo12` 以及最后的 `adjustReg` 调用。

### 13.6 frame index elimination：SP/FP 选择与 12 位立即数

`getFrameIndexReference` 决定一个 frame index 相对哪个基寄存器、偏移是多少。它的规则可以压成四层：

1. 如果这个 frame index 落在 callee-saved 区间，基寄存器用 `SP`，偏移加上 `getStackSizeWithRVVPadding(MF)` 或 `FirstSPAdjustAmount`。
2. 如果函数需要动态栈重对齐，并且不是 fixed object，基寄存器用 `BP`（如果有）或 `SP`，因为 `SP` 已经被重对齐改过。
3. 如果不需要重对齐且没有帧指针，基寄存器用 `SP`。
4. 如果有帧指针，默认用 `FP`；当 `hasReservedCallFrame(MF)` 为真时，还会比较 `SP` 相对偏移是否落在压缩访存的范围内（RV64 的 `c.ldsp` 阈值是 504，RV32 的 `c.lwsp` 是 252），能落进去就优先用 `SP`，以生成更短的指令。

`eliminateFrameIndex` 拿到 `(FrameReg, Offset)` 后，把偏移拆成“指令立即数 + 剩余调整”。对普通 load/store，它把 `SignExtend64<12>(Val)` 写进立即数操作数；如果还有剩余，就创建一个虚拟 GPR，用 `adjustReg` 把 `FrameReg + 剩余` 算进去，再把访存基址换成这个虚拟寄存器。对 `ADDI` 这种本身带立即数的指令，如果偏移超出 12 位，它会把原来的立即数清零，避免重复加。

RV32 还有一个硬边界：`eliminateFrameIndex` 里显式检查 `!Is64Bit && !isInt<32>(Offset.getFixed())`，超出 32 位有符号范围直接 `reportFatalUsageError`（“Frame offsets outside of the signed 32-bit range not supported on RV32”）。RV64 的上限则由 `getStackThreshold()` 给出，是 `INT64_MAX`。这说明“栈帧能有多大”不是统一的，先看 XLEN。

### 13.7 RVV 的 vlen 对齐与 .cfi_escape

可伸缩向量对象不能用一个固定的字节数描述。实测一个 `alloca <vscale x 4 x i32>`：

```asm
rvv:                                    # @rvv
	.cfi_startproc
# %bb.0:                                # %entry
	addi	sp, sp, -32
	.cfi_def_cfa_offset 32
	sd	ra, 24(sp)                      # 8-byte Folded Spill
	.cfi_offset ra, -8
	csrr	a0, vlenb
	slli	a0, a0, 1
	sub	sp, sp, a0
	.cfi_escape 0x0f, 0x0d, 0x72, 0x00, 0x11, 0x20, 0x22, 0x11, 0x02, 0x92, 0xa2, 0x38, 0x00, 0x1e, 0x22 # sp + 32 + 2 * vlenb
	addi	a0, sp, 16
	call	usev
	csrr	a0, vlenb
	slli	a0, a0, 1
	add	sp, sp, a0
	.cfi_def_cfa sp, 32
	ld	ra, 24(sp)                      # 8-byte Folded Reload
	.cfi_restore ra
	addi	sp, sp, 32
	.cfi_def_cfa_offset 0
	ret
```

固定部分是 32 字节：`ra` 在 `sp + 24`，对象基址在 `sp + 16`。可变部分是 `2 * vlenb`：`<vscale x 4 x i32>` 需要 2 个向量寄存器（LMUL=2），所以运行时读 `vlenb` 再乘 2 后从 `sp` 减去。`.cfi_escape` 后面的注释直接写出表达式 `sp + 32 + 2 * vlenb`，这是 `createDefCFAExpression` 生成的 DWARF 表达式；`0x0f` 是 `DW_CFA_def_cfa_expression`，`0x72` 是 `DW_OP_breg2`（`sp`），其余字节编码固定偏移、`vlenb` 寄存器和乘法/加法。

源码链路：`processFunctionBeforeFrameFinalized` 调用 `assignRVVStackObjectOffsets`，把 RVV 大小与对齐写入 `RISCVMachineFunctionInfo`；`determineFrameLayout` 在 `RVFI->getRVVStackSize()` 非零且没有帧指针/重对齐时计算 `RVVPadding`；`getStackSizeWithRVVPadding` 再把 `MFI.getStackSize() + RVVPadding` 对齐到栈对齐。`emitPrologue` 里的 `RVVStackSize` 分支用 `adjustReg` 的 scalable 路径，生成 `PseudoReadVLENB`（最终展开为 `csrr vlenb`）和 `SUB`。

注意一个边界：如果函数同时有 RVV 对象和帧指针，`determineFrameLayout` 不会走 `!hasFP(MF) || hasStackRealignment(MF)` 的 padding 分支；根据源码注释，这时 RVV 对象通过 `FP` 访问时要减去整个栈大小，布局假设不同。修改 RVV 栈布局时不能只盯 `getStackSizeWithRVVPadding` 一个函数。

### 13.8 .cfi_* 的来源

把本章出现的 CFI 指令与源码对应起来：

| CFI | 生成点 | 语义 |
| --- | --- | --- |
| `.cfi_startproc` / `.cfi_endproc` | AsmPrinter 的函数边界 | 开始/结束一个 FDE |
| `.cfi_def_cfa_offset N` | `allocateStack` 的 `buildDefCFAOffset(RealStackSize)` | CFA = `sp + N` |
| `.cfi_def_cfa s0, 0` | `emitPrologue` 的 `buildDefCFA(FPReg, VarArgsSaveSize)` | CFA = `s0 + 0` |
| `.cfi_offset ra, -8` | 遍历 `CalleeSavedInfo` 的 `buildOffset` | `ra` 保存在 `CFA - 8` |
| `.cfi_restore ra` | epilogue 的 `buildRestore` | `ra` 已恢复 |
| `.cfi_def_cfa sp, 16` | epilogue 切回 `sp` 基准 | 有帧指针时先把 CFA 切回 `sp` |
| `.cfi_escape ...` | `createDefCFAExpression` | RVV 的可伸缩 CFA 表达式 |

这些指令的插入点不是“越靠近指令越好”，而是由 `CFIInstBuilder` 的插入点和 `MachineInstr::FrameSetup` 标记决定。`emitPrologue` 先把 `MBBI` 跳到 callee-saved spill 之后，再设置 `CFIBuilder` 的插入点，目的就是让 `.cfi_offset` 出现在对应的 `sd` 之后，而不是之前。调试信息错位经常不是 CFI 数值算错，而是插入点选错。

## 可跑实验

统一入口（在仓库根目录执行）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  compilers/riscv_backend/code/tools/run.ps1 labs/13-frame-lowering.ps1
```

脚本会在 `%TEMP%\riscv-course-lab13` 生成 `basic.ll`、`frame.ll`、`rvvframe.ll`，并跑六次 `llc`。下面给出三个必须亲手核对的现象。以下命令假设 `llc` 已在 `PATH`（统一入口会把它放到前面）；如果直接在新终端运行，把 `llc` 换成 `& "$LLVM\llc.exe"`，并把 `$LLVM` 设为工具目录。

实验一：默认 `caller` 与 `-frame-pointer=all` 的差异。

```powershell
llc -mtriple=riscv64 -mattr=+m,+a,+f,+d -O2 basic.ll -o caller-default.s
llc -mtriple=riscv64 -mattr=+m,+a,+f,+d -O2 -frame-pointer=all basic.ll -o caller-fp-all.s
```

预期：默认只有 `addi sp, sp, -16`、`sd ra, 8(sp)`、四条 CFI；加 `-frame-pointer=all` 后多出 `sd s0, 0(sp)`、`addi s0, sp, 16`、`.cfi_def_cfa s0, 0`，以及 epilogue 的 `ld s0, 0(sp)` 和 `.cfi_restore s0`。

实验二：大栈帧的拆分。

```powershell
llc -mtriple=riscv64 -O2 frame.ll -o frame.s
```

在 `frame.s` 里核对：`big` 的第一次调整是 `-2032`，随后是 `-2048` 和 `-944`；`huge` 的第二次调整是 `lui a0, 2; addi a0, a0, 1808; sub sp, sp, a0`，也就是 10000 字节。把 `big` 的 `s0` 误认成帧指针是本章最常见的错误；它没有 `addi s0, sp, ...`，只是跨调用保存实参。

实验三：动态 alloca 与 `-frame-pointer=none`。

```powershell
llc -mtriple=riscv64 -O2 -frame-pointer=none frame.ll -o frame-none.s
```

预期：`dyn` 仍然有 `addi s0, sp, 16` 和 `.cfi_def_cfa s0, 0`。这不是 `-frame-pointer=none` 失效，而是 `hasVarSizedObjects()` 强制了帧指针。

附加实验：RVV 帧。

```powershell
llc -mtriple=riscv64 -mattr=+v -O2 rvvframe.ll -o rvv.s
```

预期：固定 32 字节，`csrr a0, vlenb; slli a0, a0, 1; sub sp, sp, a0`，以及 `.cfi_escape ... # sp + 32 + 2 * vlenb`。

失败/边界例子：如果只看 `addi sp, sp, -N` 的 `N` 是否等于“局部变量大小”，`big` 会给出反例。局部数组是 5000 字节，但第一次调整是 2032，之后还有两次；帧总大小是 5024，因为 5000 向上对齐到 16 是 5008，再加上保存的 `ra`、`s0` 和可能的对齐/槽位，最终是 5024。不要把第一条 `addi` 当成完整帧大小。

## 源码落点

| 文件 | 符号 | 本章对应 |
| --- | --- | --- |
| `lib/Target/RISCV/RISCVFrameLowering.cpp` | `getABIStackAlignment` | 16/8/4 字节对齐 |
| 同上 | `hasFPImpl` | 动态 alloca、取帧地址、重对齐、最大调用帧 >2047 |
| 同上 | `determineFrameLayout` | `alignTo` 与 RVV padding |
| 同上 | `processFunctionBeforeFrameFinalized` | RVV 偏移、emergency spill slot、`CalleeSavedStackSize` |
| 同上 | `emitPrologue` / `emitEpilogue` | `allocateStack`、CFI、FP、第二次 SP 调整 |
| 同上 | `allocateStack` | `adjustReg` 与 CFI 的配对 |
| 同上 | `getFirstSPAdjustAmount` | 2032（或 Zca 下的 496）拆分点 |
| 同上 | `getFrameIndexReference` | SP/FP/BP 选择与压缩偏移阈值 |
| 同上 | `determineCalleeSaves` | callee-saved 与 RVV 子寄存器修正 |
| `lib/Target/RISCV/RISCVRegisterInfo.cpp` | `eliminateFrameIndex` | 12 位立即数拆分、RV32 32 位上限 |
| 同上 | `adjustReg` | `ADDI` / 两条 `ADDI` / `movImm + ADD/SUB` / scalable `vlenb` |
| `lib/Target/RISCV/RISCVCallingConv.cpp` | `getArgGPRs` / `getArgFPRs` | ABI 参数寄存器列表（与第 12 章、练习 C 相连） |
| `lib/Target/RISCV/RISCVInstrInfo.cpp` | `storeRegToStackSlot` / `loadRegFromStackSlot` | spill/restore 的实际指令选择 |

需要重新构建 `llc` 才能验证的改动（`guided`）：修改 `getFirstSPAdjustAmount` 的拆分阈值、修改 `hasFPImpl` 的条件、或者给 `getFrameIndexReference` 增加新的基寄存器选择分支。验证方式：构建后重跑本章三个实验，比较 `big`/`dyn`/`rvv` 的 prologue；不要只跑单元测试，因为栈帧错误经常只在特定帧大小和 callee-saved 组合下出现。

## 常见坑

1. **把 16 字节对齐当成“每个局部变量都占 16 字节”。** 对齐的是整个栈帧和 `sp`，不是每个对象。`caller` 的 `ra` 只占 8 字节，帧仍然 16 字节；`big` 的 5000 字节数组也只按自身对齐要求放置。
2. **看到 `s0` 就认定它是帧指针。** `big` 的 `s0` 是跨调用保存实参的普通 callee-saved GPR。判断标准是 `addi s0, sp, ...` 和 `.cfi_def_cfa s0, ...`，不是寄存器编号。
3. **以为 `-frame-pointer=none` 能去掉所有帧指针。** 动态 alloca、取帧地址、栈重对齐、最大调用帧超过 2047 都会强制 `hasFP`。`dyn` 在 `-frame-pointer=none` 下仍有帧指针。
4. **把第一条 `addi sp, sp, -N` 当成完整帧大小。** 大帧会被拆成多次调整；`N` 可能只是 `2048 - StackAlign` 的第一次拆分。完整大小要看最后一次 `.cfi_def_cfa_offset` 或 epilogue 的累加。
5. **把 `.cfi_offset ra, -8` 当成 `ra` 的栈槽偏移。** 它是相对 CFA 的偏移；栈槽偏移是 `8(sp)`。CFA 可能是 `sp + 16`，也可能是 `s0 + 0`。
6. **在 RVV 函数里假设栈大小是常数。** `vlenb` 是运行时值，`getStackSizeWithRVVPadding` 只处理固定部分和 padding；真正的可伸缩部分由 `StackOffset::getScalable` 和 `.cfi_escape` 描述。
7. **忽略 RV32 的 32 位帧偏移上限。** `eliminateFrameIndex` 对 RV32 有显式 `isInt<32>` 检查；把 RV64 的大帧代码直接搬到 RV32 会触发 `reportFatalUsageError`，而不是生成一条宽指令。

## 自我检查

- [ ] 我能说出 `caller` 的 16 字节里，哪 8 字节被使用、哪 8 字节是对齐填充，以及为什么不是 8 字节帧。
- [ ] 我能解释 `sd ra, 8(sp)` 与 `.cfi_offset ra, -8` 的基准差异，并能从 CFA 定义反推两者。
- [ ] 我能判断一个函数里的 `s0` 是帧指针还是普通 callee-saved GPR，并给出两条判据。
- [ ] 我能解释 `-frame-pointer=none` 为什么对 `dyn` 无效，并指出 `hasFPImpl` 里的对应条件。
- [ ] 我能把 `big` 的三次 SP 调整相加，得到 5024，并解释 `2032` 的来源。
- [ ] 我能说明 `huge` 里 `lui + addi + sub` 中 10000 是怎么算出来的，以及为什么不是两条 `addi`。
- [ ] 我能说出 `getFrameIndexReference` 在什么情况下优先用 `SP` 而不是 `FP`。
- [ ] 我能解释 RVV 的 `.cfi_escape` 注释 `sp + 32 + 2 * vlenb` 中每个部分的来源。
- [ ] 我能指出 `.cfi_startproc`、`.cfi_def_cfa_offset`、`.cfi_offset` 分别由哪个组件发出。
- [ ] 我知道哪些栈帧改动必须重新构建 `llc` 才能验证，并会标 `guided`。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVFrameLowering.cpp`：本章的主线源码，重点读 `emitPrologue` 的 `allocateStack` 调用、`getFirstSPAdjustAmount` 的拆分条件、`getFrameIndexReference` 的 SP/FP 选择。
- `llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp`：`adjustReg` 与 `eliminateFrameIndex`，理解“偏移如何变成一条或两条指令”。
- `llvm/lib/Target/RISCV/RISCVCallingConv.cpp`：`getArgGPRs`、`getArgFPRs`、`CC_RISCV_Impl`，为练习 C 的 ABI 审计做准备。
- `llvm/test/CodeGen/RISCV/`：搜索 `frame-pointer`、`stack`、`rvv` 相关测试，观察不同 `-mattr` 组合下的 prologue。
- RISC-V ELF psABI：栈对齐、callee-saved 寄存器、参数传递的规范来源；读源码时用它校验“编译器行为”与“ABI 要求”是否一致。
- LLVM 的 `TargetFrameLowering` 接口文档与 `CFIInstBuilder`：理解 `determineFrameLayout`、`emitPrologue`、`eliminateFrameIndex` 的通用契约。

[上一章：调用约定](./12-calling-convention.md) | [下一章：练习 C：ABI 与栈帧审计](./practice-c-abi-audit.md)