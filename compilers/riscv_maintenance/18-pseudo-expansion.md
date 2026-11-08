# Ch 18 — 伪指令展开：从 Pseudo 到真实指令

> **目标**：把 `Pseudo*` 当成“延迟决策的占位符”，而不是汇编器的语法糖；能按 PreRA / PostRA / PreEmit / Atomics 四个时机定位展开代码，并解释 `getInstSizeInBytes` 为什么必须提前知道尺寸。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，`llvm/lib/Target/RISCV` @ `0fc5afa04c23`，`X86;AArch64;RISCV`，assertions ON。
>
> **前置**：读完 Ch 03（流水线与 MIR）、Ch 08（常量物化）、Ch 13（合法化）。

---

## 18.1 为什么伪指令存在

伪指令不是“汇编器帮你展开的别名”这么简单。它真正的价值是**把某个决定推迟到信息最完整的时刻**。RISC-V 后端里至少有四类决定被推迟：

1. **寄存器形态不同**：PreRA 阶段只能创建虚拟寄存器（`PseudoLLA` 需要一个 scratch），PostRA 阶段必须直接写物理寄存器（`PseudoMovAddr` 的 `$x10`）。
2. **成对重定位必须一起生成**：`%pcrel_hi` / `%pcrel_lo` 不是两条独立指令，而是一对共享临时符号的序列。PreRA 的 `expandAuipcInstPair` 先用 `createNamedTempSymbol("pcrel_hi")` 造符号，再把 `MO_PCREL_LO` 绑到它上面。
3. **尺寸必须提前可知**：BranchRelaxation 在 `addPreEmitPass` 里按 `getInstSizeInBytes` 累加块大小；而 `PseudoCCMOVGPR` 要到更晚的 PreEmit 才变成 `branch + mv`。尺寸不对，分支就会越界或该放松的没放松。
4. **选择依赖 ABI / subtarget**：`PseudoCALL` 在 `+relax` 与 `-mno-relax` 下在目标文件里都要保持 `auipc+jalr` 的 8 字节形状，只是后者不再附带 `R_RISCV_RELAX`；`PseudoAtomic*` 只在“有 Zalrsc、没有 Zaamo”时出现；`PseudoCC*` 只在 SFB 或 conditional-move-fusion 打开时出现。

本树已经把旧的单文件 `RISCVExpandPseudoInsts.cpp` 拆成五个实现文件（`wc -l` 实测）：

| 文件 | 行数 | 职责 |
|---|---:|---|
| `RISCVExpandPseudoAtomics.cpp` | 920 | AMO / LR-SC 循环 |
| `RISCVExpandPseudoPreEmit.cpp` | 631 | AsmPrinter 前最后一批 |
| `RISCVExpandPseudoPostRA.cpp` | 232 | 物理寄存器已就绪的常量/地址 |
| `RISCVExpandPseudoPreRA.cpp` | 216 | 需要 vreg 的地址与 TLS |
| `RISCVExpandPseudoBase.{h,cpp}` | 61 + 59 | 公共骨架、大小断言 |

所以不要沿用网上“一个 `RISCVExpandPseudoInsts::expandPseudoInsts` 走天下”的旧结构；本树没有这个函数名。

---

## 18.2 先建立直觉：四类展开时机

```text
ISel → Pseudo 进 MIR
     ├─ PreRA  (RISCVExpandPseudoPreRA)   : 需要虚拟寄存器/还没有物理寄存器约束
     ├─ PostRA (RISCVExpandPseudoPostRA)  : 需要知道物理寄存器、栈布局
     ├─ PreEmit(RISCVExpandPseudoPreEmit) : AsmPrinter 前最后一步
     └─ Atomics(RISCVExpandPseudoAtomics) : AMO/LR-SC 循环，受 subtarget 影响
```

四个 pass 在 `RISCVPassConfig` 里的插入点（行号为本树实测）：

| 时机 | `-stop-after` 名 | 插入钩子 | 关键行 | 此时已知 | 典型 Pseudo |
|---|---|---|---|---:|---|
| PreRA | `riscv-expand-pseudo-pre-ra` | `addPreRegAlloc` | 569 | SSA、虚拟寄存器 | `PseudoLLA`、`PseudoLGA`、`PseudoLA_TLS_*` |
| PostRA | `riscv-expand-pseudo-post-ra` | `addPreSched2` | 489 | 物理寄存器、栈布局 | `PseudoMovImm`、`PseudoMovAddr`、`PseudoAddUpperImm`、`PseudoMERGE` |
| PreEmit | `riscv-expand-pseudo-pre-emit` | `addPreEmitPass2` | 523 | 已完成 RA、PEI、outliner、branch relaxation | `PseudoCC*`、`PseudoRV32Zdinx*`、`PseudoVMSET_M_*` |
| Atomics | `riscv-expand-pseudo-atomics` | `addPreEmitPass2` | 533 | 同上，且是最后一步 | `PseudoAtomic*`、`PseudoCmpXchg*` |

`-debug-pass=Structure` 的真实顺序是 PreRA → Prologue/Epilogue → PostRA → Branch relaxation → Outliner → PreEmit → Atomics → AsmPrinter。

两个容易看错的点：

- **PostRA 在 Prologue/Epilogue 之后**，所以它能看到最终栈布局；但它仍然在 post-RA scheduler 之前。
- **PreEmit 与 Atomics 在 Branch relaxation 之后**。这意味着它们展开出的新分支不会再被 BranchRelaxation 处理，尺寸必须由 Pseudo 的 `Size` 提前兜住。

本构建 `llc -help-hidden | grep expand` **不会列出 pass 名**（`-help-hidden` 只列选项）。确认 pass 名请用 `-debug-pass=Structure`，或直接看 `INITIALIZE_PASS` 的字符串。

### 还有两个“非 pass”展开层

`PseudoCALL` / `PseudoTAIL` / `PseudoJump` 不在这四个 pass 里。它们一路活到 MC 编码期，由 `RISCVMCCodeEmitter::expandFunctionCall` 展开；`PseudoLI` / `PseudoLA` 则在 AsmParser 解析时立即展开。把它们误当成 PreRA/PostRA 案例，是读旧文章最常见的偏差。

---

## 18.3 `RISCVExpandPseudoBase` 的骨架

真实类名是 `RISCVExpandPseudoImplBase`，不是 `RISCVExpandPseudo`。子类只实现一个虚函数：

```cpp
// RISCVExpandPseudoBase.h:26-57（节选）
class RISCVExpandPseudoImplBase {
  bool run(MachineFunction &MF);              // Base.cpp:28-46
  virtual bool expandMI(MachineBasicBlock &, MachineBasicBlock::iterator, MachineBasicBlock::iterator &) const;
  bool expandMBB(MachineBasicBlock &) const;  // Base.cpp:48-59
  const RISCVSubtarget *STI; const RISCVInstrInfo *TII;
};
```

- **入口是 `run`**（Base.cpp:28-46）：设置 `STI`/`TII`，遍历 `MF` 的每个 `MBB`。
- **分派是 `expandMBB`**（Base.cpp:48-59）：保存 `NMBBI = std::next(MBBI)`，调用 `expandMI`，然后 `MBBI = NMBBI`。
- **真实展开函数是 `expandMI`**，没有任何 `expandMInstruction` 或 `expandPseudo`。子类用 `switch (MBBI->getOpcode())` 分流。
- **MBB 拆分的契约**：一旦 `expandMI` 把当前 MBB 的尾部搬到新块，必须把 `NextMBBI` 设为 `MBB.end()`，否则外层循环会踩到已搬走的迭代器。PreEmit 的 `expandCCOp` 与 Atomics 的 `expandAtomicBinOp` 都这么做。

Base 里有一个很重要的调试断言：

```cpp
// RISCVExpandPseudoBase.cpp:32-43
assert(OldSize >= NewSize &&
       "Expanding Pseudos should not increase function size estimate");
```

它强制“展开后的估计尺寸不能变大”。这不是性能优化，而是 BranchRelaxation 的正确性前提：Pseudo 的 `Size` 必须不小于真实展开序列。

### `MachineFunctionProperties`

- PreRA 的 legacy pass 声明 `getRequiredProperties()` 返回 `setIsSSA()`（PreRA.cpp:63-65）；新 PM 的 `RISCVExpandPseudoPreRAPass` 同样声明 `setIsSSA()`（`RISCV.h:153-163`），并用 `MFPropsModifier` 包住 `run`。
- PostRA / PreEmit / Atomics 不再要求 SSA；它们直接操作物理寄存器。它们没有额外声明 `NoVRegs`，但调度位置已经在 RA 之后。

### MBB 拆分与 `LivePhysRegs`

只有会新建基本块的 pass 需要手动补 live-in：

- PreEmit 的 `expandCCOp` 建 `TrueBB` / `MergeBB`，`splice` 尾部、`transferSuccessors`，最后 `computeAndAddLiveIns(LiveRegs, *TrueBB)` 与 `*MergeBB`（PreEmit.cpp:285-299）。
- Atomics 的 `expandAtomicBinOp` 建 `LoopMBB` / `DoneMBB`，同样在末尾补 live-ins（Atomics.cpp:492-516）。
- PreRA 的 `expandAuipcInstPair` 不拆块，只在原位置插入两条指令，所以不需要 `LivePhysRegs`。

---

## 18.4 案例一：地址物化（`PseudoLLA` / `PseudoMovAddr` / `PseudoLI`）

### `PseudoLLA` → `auipc` + `addi`（PreRA）

PIC 且符号是 local/hidden 时，ISel 选 `PseudoLLA`：

```text
%0:gpr = PseudoLLA @x
--- -stop-after=riscv-expand-pseudo-pre-ra ---
%1:gpr = AUIPC target-flags(riscv-pcrel-hi) @x, pre-instr-symbol <mcsymbol .Lpcrel_hi0>
%0:gpr = ADDI %1, target-flags(riscv-pcrel-lo) <mcsymbol .Lpcrel_hi0>
```

源码在 `RISCVExpandPseudoPreRA.cpp:98-132`：

- `createVirtualRegister(&RISCV::GPRRegClass)` 造 scratch（105-107）；
- 把原符号的 target flags 改成 `MO_PCREL_HI`（109-110）；
- `createNamedTempSymbol("pcrel_hi")`（111），并用 `setPreInstrSymbol` 绑到 `AUIPC` 上（113-115）；
- 第二条用 `MO_PCREL_LO` 引用同一个临时符号（117-120）；
- 若原 Pseudo 有 memory operand，转移给第二条（122-123）。

`PseudoLGA` / `PseudoLA_TLS_IE` / `PseudoLA_TLS_GD` 走同一个 `expandAuipcInstPair`，只是 hi flag 与第二条 opcode 不同（129-149）。`PseudoLA_TLSDESC` 更复杂，会生成 `auipc + ld + addi + PseudoTLSDESCCall + add`（151-194）。

### `PseudoMovAddr` → `lui` + `addi`（PostRA）

非 PIC、small code model 下，`ret ptr @x` 会先得到：

```text
%0:gpr = PseudoMovAddr target-flags(riscv-hi) @x, target-flags(riscv-lo) @x
--- PostRA 之后 ---
renamable $x10 = LUI target-flags(riscv-hi) @x
renamable $x10 = ADDI killed renamable $x10, target-flags(riscv-lo) @x
```

源码 `RISCVExpandPseudoPostRA.cpp:102-120`。它不需要 scratch vreg，因为目的寄存器已经是物理寄存器；`hi`/`lo` 也是现成的 target flags，所以只是机械地 `LUI` + `ADDI`。`PseudoMovImm`（`-riscv-use-rematerializable-movimm`）走 `expandMovImm` → `TII->movImm` → `RISCVMatInt::generateInstSeq`（85-100），与 Ch 08 的常量物化是同一套算法。

### `PseudoLI`：AsmParser 层，不是机器 pass

`PseudoLI` 的 TableGen 注释写得很清楚（`RISCVInstrInfo.td:1133-1140`）：`Size = 32`，因为 64 位任意常量最多 8 条 32 位指令；但它“currently only used in the AsmParser where it gets expanded to real instructions immediately”。

解析路径是 `RISCVAsmParser.cpp:4497-4518` → `emitLoadImm`（3743-3751）→ `RISCVMatInt::generateMCInstSeq`；机器层大常量默认在 ISel/`movImm` 阶段就用 `RISCVMatInt` 展开，`PseudoLI` 只服务汇编输入，32 字节 `Size` 不会进入 BranchRelaxation。

---

## 18.5 案例二：`PseudoCALL` 与 `PseudoTAIL`（MC 编码期）

`PseudoCALL` 在 PreRA、PostRA、PreEmit、Atomics 之后都还在。`-stop-after=prolog-epilog` 的 MIR：

```text
PseudoCALL target-flags(riscv-call) @foo, csr_ilp32_lp64, implicit-def dead $x1, implicit-def $x2
```

最终汇编仍然是 `call foo`；展开形状是 `auipc ra, %pcrel_hi(foo)` + `jalr ra, %pcrel_lo(ra)`，目标文件里是两条指令：

```text
call foo
--- obj ---
8:  auipc ra, 0x0
c:  jalr  ra <bar+0x8>
```

真正的展开函数是 `RISCVMCCodeEmitter::expandFunctionCall`（`RISCVMCCodeEmitter.cpp:169-227`）：先发 `AUIPC Ra, Func`（212-215），再按 opcode 发 `JALR Ra, Ra, 0` 或 `JALR X0, Ra, 0`（217-224）。

`PseudoCALL` 的 `Ra = X1`（185-187），`PseudoTAIL` 的 `Ra` 由 `getTailExpandUseRegNo` 决定（176-178），默认是 `t1`，所以：

```text
PseudoTAIL @foo  →  auipc t1, %pcrel_hi(foo)
                    jalr  zero, t1, %pcrel_lo(...)
```

`PseudoTAIL` 的 TableGen 声明故意 clobber `X6` 和 `X7`（`RISCVInstrInfo.td:1958-1965`），因为 Zicfilp 可能选不同的临时寄存器。

### `+relax` 与 `-mno-relax` 的差别

两者在目标文件里都是 8 字节的 `auipc+jalr`，差别只在重定位：

```text
# -mattr=+relax
0x8 R_RISCV_CALL_PLT foo 0x0
0x8 R_RISCV_RELAX - 0x0

# -mattr=-relax
0x8 R_RISCV_CALL_PLT foo 0x0
```

注意源码注释里写的是 `R_RISCV_CALL`，但 `RISCVELFObjectWriter.cpp:98-101` 把 `fixup_riscv_call` 与 `fixup_riscv_call_plt` 都映射成 `R_RISCV_CALL_PLT`。这是读源码时的一个小陷阱。

链接期 `ld.lld --relax` 才会把 8 字节缩成 4 字节 `jal`；`-mattr=-relax` 的目标文件没有 `R_RISCV_RELAX`，即使链接器加 `--relax` 也保持 8 字节。**所以 `getInstSizeInBytes` 必须按 8 字节预算，不能按链接后的 4 字节预算。**

---

## 18.6 案例三：`PseudoCCMOVGPR` / `PseudoCCADD*`（PreEmit）

先纠正一个常见误解：**Zicond 不走这些 Pseudo**。`+zicond` 时 ISel 直接把 `select` 降成 `czero.eqz` / `czero.nez`：

```asm
slt      a1, a0, a1
czero.eqz a0, a0, a1
```

`PseudoCCMOVGPR` 走 SFB（`HasShortForwardBranchIALU`），`PseudoCCMOVGPRNoX0` 走 conditional-move-fusion（`HasConditionalMoveFusion`），见 `RISCVInstrInfoSFB.td:82-113`。打开 `+short-forward-branch-ialu` 后，MIR 里会看到：

```text
renamable $x10 = PseudoCCMOVGPR killed $x10, killed $x12, 14188, $x10, killed $x11
```

PreEmit 的 `expandCCOp`（`RISCVExpandPseudoPreEmit.cpp:175-301`）做四件事：

1. 先尝试 `expandCCOpToCMov`（304 行起）：只有 vendor `Xqcicm` 才走条件搬移，否则返回 false。
2. 建 `TrueBB` / `MergeBB`，把 `PseudoCC*` 之后的尾部 `splice` 到 `MergeBB`（187-190、287-288）。
3. 在 `MBB` 末尾发条件分支，目标先写 `MergeBB`；true 值在 `TrueBB` 里算。
4. `NextMBBI = MBB.end()`，删掉 Pseudo，再用 `LivePhysRegs` 给两个新块补 live-ins（293-299）。

展开后的 MIR 见实验 7：`BLT ... %bb.2`，`bb.1` 的 live-in 是 `$x12`，里面是 `ADDI`。

`PseudoCCADD` / `PseudoCCSUB` / `PseudoCCAND` 等走同一个 `expandCCOp`，只是 `TrueBB` 里的 opcode 换成对应的真实 ALU 指令（`RISCVExpandPseudoPreEmit.cpp:218-266`）。它们不会变成 `czero`；`czero` 是 Zicond 在 ISel 的选择结果。

一个尺寸细节：`PseudoCCMOVGPR` 的 `getInstSizeInBytes` 是**动态**的——分支尺寸加 4（`RISCVInstrInfo.cpp:2131-2175`）；`PseudoCCMOVGPRNoX0` 是分支尺寸加 2（2127-2130）。所以 TableGen 里没有给它一个固定 `Size`，而是靠 `getInstSizeInBytes` 特判。

---

## 18.7 案例四：`PseudoAtomic*`（Atomics）

`PseudoAtomic*` 的 predicate 是 `HasStdExtZalrsc, NoStdExtZaamo`（`RISCVInstrInfoA.td:319-334`）：

- `+a` 同时带来 Zaamo 与 Zalrsc，ISel 直接选 `amoadd.w`，不会留下 Pseudo；
- `+zalrsc,-zaamo` 才会选 `PseudoAtomicLoadAdd32`，由 Atomics pass 展开成 LR/SC 循环；
- `+a,-zalrsc` 在本构建退化为 `call __atomic_fetch_add_4`，所以测试要显式写 `+zalrsc,-zaamo`。

`-mattr=+zalrsc,-zaamo` 的 PreEmit 之后、Atomics 之前：

```text
$x12, $x13 = PseudoAtomicLoadAdd32 killed $x10, killed $x11, 2
```

`-stop-after=riscv-expand-pseudo-atomics` 之后：

```text
$x12 = LR_W $x10
$x13 = ADD $x12, $x11
$x13 = SC_W $x13, $x10
BNE  $x13, $x0, %bb.1
```

展开后的 MIR 见实验 6；最终汇编就是 `lr.w` / `add` / `sc.w` / `bnez` 四步循环。

源码分三层：

- `expandMI` 把 33 个 Pseudo opcode 分到 `expandAtomicBinOp` / `expandAtomicMinMaxOp` / `expandAtomicCmpXchg`（`RISCVExpandPseudoAtomics.cpp:103-205`）；
- `expandAtomicBinOp` 建 `LoopMBB` / `DoneMBB`，搬尾部、接 successor、补 live-ins（486-519）；
- `doAtomicBinOpExpansion` 真正发 `LR` / `BINOP` / `SC` / `BNE`（313-400）。

为什么放在最后？注释写得很直接（`RISCVTargetMachine.cpp:530-533`）：AMO 展开必须避开其他 pass 在 LR/SC 之间插入指令，否则前向进展（forward progress）保证会被破坏。`Size = 16` 对应 4 条 32 位指令；min/max 变体是 `Size = 24`（`RISCVInstrInfoA.td:321-334`）。

---

## 18.8 边界：RVV 的 `PseudoVADD_VV_M1`

RVV 的 Pseudo 不走这四个 pass。`PseudoVADD_VV_M1` 进入 MIR 后一直保留；`-stop-after=riscv-vector-peephole` 仍然能看到它，Peephole 做的是伪指令级优化，不是 opcode 替换：

```text
%2:vr = PseudoVADD_VV_M1 $noreg, %0, %1, -1 /* vl=VLMAX */, 6 /* e64 */, 3 /* ta, ma */
```

到 AsmPrinter 才由 `lowerRISCVVMachineInstrToMCInst`（`RISCVAsmPrinter.cpp:1205`）通过 `RISCVVPseudosTable` 把 opcode 换成真实指令，再由 `lowerToMCInst`（1318）发出：

```asm
vsetvli a0, zero, e64, m1, ta, ma
vadd.vv v8, v8, v9
```

这里只需要记住边界：**RVV 的“展开”发生在 AsmPrinter/MC 层，不在 PreRA/PostRA/PreEmit/Atomics**。细节留给 Ch 17。

---

## 18.9 `getInstSizeInBytes` 与 branch relaxation 的耦合

BranchRelaxation 在 `addPreEmitPass` 里（`RISCVTargetMachine.cpp:512`），位置在 PreEmit 与 Atomics 之前。它按 `TII->getInstSizeInBytes(MI)` 累加块大小（`llvm/lib/CodeGen/BranchRelaxation.cpp:221`、`239`、`578`），决定分支是否要反转/加长。

`RISCVInstrInfo::getInstSizeInBytes` 在 `RISCVInstrInfo.cpp:2090`，默认分支是：

```cpp
// 2213-2214
default:
  return get(Opcode).getSize();
```

所以绝大多数 Pseudo 的尺寸直接来自 TableGen 的 `Size`：

| Pseudo | `Size` / 返回值 | 依据 |
|---|---:|---|
| `PseudoCALL` | 8 | `RISCVInstrInfo.td:1898` |
| `PseudoTAIL` | 8 | `RISCVInstrInfo.td:1958-1965` |
| `PseudoMovImm` | 8 | `RISCVInstrInfo.td:2011-2014` |
| `PseudoMovAddr` | 8 | `RISCVInstrInfo.td:1661-1663` |
| `PseudoLLA` | 8 | `RISCVInstrInfo.td:2024-2027` |
| `PseudoLI` | 32 | `RISCVInstrInfo.td:1137-1140`（仅 AsmParser） |
| `PseudoAtomicLoadAdd32` | 16 | `RISCVInstrInfoA.td:321-328` |
| `PseudoAtomicLoadMax32` | 24 | `RISCVInstrInfoA.td:329-334` |
| `PseudoCCMOVGPR` | 分支尺寸 + 4 | `RISCVInstrInfo.cpp:2131-2175` |
| `PseudoCCMOVGPRNoX0` | 分支尺寸 + 2 | `RISCVInstrInfo.cpp:2127-2130` |

为什么 `PseudoCALL` 不能按 4 字节算？因为编译器看到的永远是 8 字节的 `auipc+jalr`；4 字节 `jal` 只在链接器 relaxation 之后存在。`-mattr=-relax` 时连 `R_RISCV_RELAX` 都没有，链接器不会缩。把 `Size` 改成 4 会让 BranchRelaxation 低估块大小，近处分支可能因此越界。

为什么 PreEmit/Atomics 的 Pseudo 也必须在 BranchRelaxation 之前就有正确尺寸？因为这两个 pass 在 relaxation **之后**才跑，它们新建的分支不会再被 relaxation 处理。`PseudoCCMOVGPR` 的 `Size` 里已经包含“分支 + true 值指令”，Atomics 的 `Size` 已经包含整个 LR/SC 循环；Base 的 `OldSize >= NewSize` 断言就是防止有人把展开序列写得比 `Size` 还大。

---

## 18.10 源码落点

| 文件 | 关键行 | 看什么 |
|---|---|---|
| `llvm/lib/Target/RISCV/RISCVExpandPseudoBase.{h,cpp}` | 26-57 / 28-59 | `RISCVExpandPseudoImplBase`、`run`、大小断言、迭代器契约 |
| `llvm/lib/Target/RISCV/RISCVExpandPseudoPreRA.cpp` | 78-194 | `PseudoLLA/LGA/TLS*`、`expandAuipcInstPair` |
| `llvm/lib/Target/RISCV/RISCVExpandPseudoPostRA.cpp` | 68-211 | `PseudoMovImm/MovAddr/AddUpperImm/MERGE` |
| `llvm/lib/Target/RISCV/RISCVExpandPseudoPreEmit.cpp` | 81-429 | `PseudoCC*`、Zdinx、VMSET、MBB 拆分 |
| `llvm/lib/Target/RISCV/RISCVExpandPseudoAtomics.cpp` | 103-519 | 33 个 atomic pseudo、LR/SC 循环 |
| `llvm/lib/Target/RISCV/RISCVTargetMachine.cpp` | 488-533, 568-579 | 四个 pass 的插入点 |
| `llvm/lib/Target/RISCV/RISCVInstrInfo.cpp` | 2090-2216 | `getInstSizeInBytes` 特判与默认 `Size` |
| `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp` | 169-227, 867-931 | `PseudoCALL/TAIL/Jump` 的 MC 展开与 relax 标记 |
| `llvm/lib/Target/RISCV/MCTargetDesc/RISCVELFObjectWriter.cpp` | 98-101 | `R_RISCV_CALL` → `R_RISCV_CALL_PLT` |
| `llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp` | 3743-3751, 4497-4518 | `PseudoLI` / `PseudoLA` 的解析期展开 |
| `llvm/lib/Target/RISCV/RISCVAsmPrinter.cpp` | 390-400, 1205, 1318 | Pseudo 打印、RVV 的 opcode 替换 |
| `llvm/lib/Target/RISCV/RISCVInstrInfo.td` | 1133-1140, 1658-1663, 1898-1901, 1958-1965, 2011-2027 | `Size` 与 Pseudo 声明 |
| `llvm/lib/Target/RISCV/RISCVInstrInfoA.td` | 319-334 | atomic Pseudo 的 feature predicate 与 `Size` |
| `llvm/lib/Target/RISCV/RISCVInstrInfoSFB.td` | 82-113, 186-214 | SFB / conditional-move Pseudo |

---

## 18.11 可跑实验

以下命令都在 `ct-instr-trunk` 上实测。公共输入：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cd /home/ubuntu/llvm/trees/ct-instr-trunk
cat >/tmp/ch18.ll <<'IR'
target triple = "riscv64-unknown-linux-gnu"
declare void @foo()
define void @bar() { call void @foo()
  ret void }
IR
```

### 实验 1：PreRA 停点看 `PseudoCALL` 还在

```bash
$T/bin/llc -mtriple=riscv64 -stop-after=riscv-expand-pseudo-pre-ra /tmp/ch18.ll -o - \
  | grep -E 'ADJCALL|PseudoCALL'
```

```text
ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
PseudoCALL target-flags(riscv-call) @foo, csr_ilp32_lp64, implicit-def dead $x1, implicit-def $x2
```

### 实验 2：PostRA 看 `PseudoMovAddr` 已展开

```bash
cat >/tmp/ch18-p.ll <<'IR'
target triple = "riscv64-unknown-linux-gnu"
@x = global i32 0
define ptr @addr() { ret ptr @x }
IR
$T/bin/llc -mtriple=riscv64 -O2 -stop-after=riscv-expand-pseudo-post-ra /tmp/ch18-p.ll -o - \
  | sed -n '/name:            addr/,/^\.\.\./p' | grep -E 'LUI|ADDI'
```

```text
renamable $x10 = LUI target-flags(riscv-hi) @x
renamable $x10 = ADDI killed renamable $x10, target-flags(riscv-lo) @x
```

### 实验 3：`prolog-epilog` 与最终汇编

```bash
$T/bin/llc -mtriple=riscv64 -stop-after=prolog-epilog /tmp/ch18.ll -o - | grep PseudoCALL
$T/bin/llc -mtriple=riscv64 /tmp/ch18.ll -o - | sed -n '/bar:/,/Lfunc_end/p'
```

```text
PseudoCALL target-flags(riscv-call) @foo, csr_ilp32_lp64, implicit-def dead $x1, implicit-def $x2
```

```asm
addi  sp, sp, -16
sd    ra, 8(sp)
call  foo
ld    ra, 8(sp)
addi  sp, sp, 16
ret
```

### 实验 4：`+relax` vs `-relax` 的重定位

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+relax  -filetype=obj /tmp/ch18.ll -o /tmp/r.o
$T/bin/llc -mtriple=riscv64 -mattr=-relax -filetype=obj /tmp/ch18.ll -o /tmp/n.o
$T/bin/llvm-readobj -r /tmp/r.o | sed -n '/\.rela.text/,/}/p'
$T/bin/llvm-readobj -r /tmp/n.o | sed -n '/\.rela.text/,/}/p'
```

```text
Section (3) .rela.text {
  0x8 R_RISCV_CALL_PLT foo 0x0
  0x8 R_RISCV_RELAX - 0x0
}
Section (3) .rela.text {
  0x8 R_RISCV_CALL_PLT foo 0x0
}
```

两者汇编文本都是 `call foo`，对象都是 8 字节 `auipc+jalr`；差别只在 `R_RISCV_RELAX` 与链接期是否缩成 `jal`。

### 实验 5：MC 层直接展开 `call` 与 `li`

```bash
printf 'call foo\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
printf 'li a0, 0x123456789abcdef0\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
```

```text
call foo    # encoding: [0x97'A',A,A,A,0xe7'A',0x80'A',A,A]
            # fixup A - offset: 0, value: foo, kind: fixup_riscv_call_plt
li a0, 0x123456789abcdef0
  # lui a0, 583; addi a0, a0, -1875; slli a0, a0, 14; addi a0, a0, -947
  # slli a0, a0, 12; addi a0, a0, 1511; slli a0, a0, 13; addi a0, a0, -272
```

`li` 实际输出 8 条 32 位指令，正好对上 `PseudoLI` 的 `Size = 32`。

### 实验 6：atomics 的 LR/SC 展开

```bash
cat >/tmp/ch18-a.ll <<'IR'
target triple = "riscv64-unknown-linux-gnu"
define i32 @atomic_add(ptr %p, i32 %v) {
  %old = atomicrmw add ptr %p, i32 %v monotonic
  ret i32 %old }
IR
$T/bin/llc -mtriple=riscv64 -mattr=+zalrsc,-zaamo -stop-after=riscv-expand-pseudo-atomics \
  /tmp/ch18-a.ll -o - | sed -n '/body:/,$p' | grep -E 'LR_W| ADD |SC_W|BNE'
```

```text
$x12 = LR_W $x10
$x13 = ADD $x12, $x11
$x13 = SC_W $x13, $x10
BNE  $x13, $x0, %bb.1
```

对照 `+a`：`amoadd.w a0, a1, (a0)`，没有 LR/SC 循环。

### 实验 7：`PseudoCCMOVGPR` 的 PreEmit 拆分

```bash
cat >/tmp/ch18-s.ll <<'IR'
target triple = "riscv64-unknown-linux-gnu"
define i64 @sel(i64 %a, i64 %b, i64 %c) {
  %cmp = icmp slt i64 %a, %b
  %r = select i1 %cmp, i64 %a, i64 %c
  ret i64 %r }
IR
$T/bin/llc -mtriple=riscv64 -O2 -mattr=+short-forward-branch-ialu \
  -stop-before=riscv-expand-pseudo-pre-emit /tmp/ch18-s.ll -o - | grep PseudoCCMOVGPR
$T/bin/llc -mtriple=riscv64 -O2 -mattr=+short-forward-branch-ialu \
  -stop-after=riscv-expand-pseudo-pre-emit /tmp/ch18-s.ll -o - | grep -E 'BLT|ADDI|bb\.[12]|liveins'
```

```text
renamable $x10 = PseudoCCMOVGPR killed renamable $x10, killed renamable $x12, 14188, renamable $x10, killed renamable $x11
BLT renamable $x10, killed renamable $x11, %bb.2
bb.1 (%ir-block.0):
  liveins: $x12
  $x10 = ADDI killed renamable $x12, 0
bb.2 (%ir-block.0):
  liveins: $x10
```

### 实验 8：RVV 的边界

```bash
cat >/tmp/ch18-v.ll <<'IR'
target triple = "riscv64-unknown-linux-gnu"
define <vscale x 1 x i64> @vadd(<vscale x 1 x i64> %a, <vscale x 1 x i64> %b) {
  %r = add <vscale x 1 x i64> %a, %b
  ret <vscale x 1 x i64> %r }
IR
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-before=riscv-expand-pseudo-pre-ra \
  /tmp/ch18-v.ll -o - | grep PseudoVADD
```

```text
%2:vr = PseudoVADD_VV_M1 $noreg, %0, %1, -1 /* vl=VLMAX */, 6 /* e64 */, 3 /* ta, ma */
```

最终汇编是 `vsetvli a0, zero, e64, m1, ta, ma` + `vadd.vv v8, v8, v9`。

`PseudoLLA` 的 PreRA 前后对照见 18.4；`PseudoMovAddr` 的 PostRA 展开同样见 18.4。链接期实测：`-mattr=+relax` 的 `call foo` 在 `ld.lld --relax` 后变成 4 字节 `jal`，`-mattr=-relax` 的版本仍是 8 字节 `auipc+jalr`。

## 18.12 踩雷集锦

1. **在 PreRA 里读物理寄存器**。PreRA 的契约是 SSA + vreg；`PseudoLLA` 的 scratch 必须 `createVirtualRegister`。读 `$x10` 这类物理寄存器，轻则断言，重则拿到未分配的垃圾。
2. **拆了 MBB 却忘了 `NextMBBI = MBB.end()`**。外层 `expandMBB` 用 `std::next` 保存的下一条迭代器可能已经被 `splice` 搬走，继续走就是 use-after-move。
3. **拆分后不补 live-ins**。PreEmit / Atomics 新建的 `TrueBB`、`LoopMBB` 必须 `computeAndAddLiveIns`；否则后续 pass 看到的 live-in 是空的，寄存器分配/调度会给出错误结果。
4. **Pseudo 的 `Size` 小于真实展开序列**。Base 的 `OldSize >= NewSize` 只在这个构建（assertions ON）里炸；release 构建可能静默通过，然后在 BranchRelaxation 里低估块大小，造成分支越界。
5. **以为 `-mno-relax` 的 `PseudoCALL` 是 4 字节**。它仍然是 8 字节 `auipc+jalr`，只是没有 `R_RISCV_RELAX`；链接器不会把它缩成 `jal`。
6. **`MachineFunctionProperties` 声明错**。PreRA 需要 `setIsSSA()`；把它挪到 RA 之后，或者去掉 SSA 声明，都会在创建 vreg 时违反属性契约。PostRA/PreEmit/Atomics 不要冒充 PreRA。
7. **在 PreEmit 里做 RA 之后不能做的事**。不要建虚拟寄存器、不要跑需要 `LiveIntervals` 的分析、不要改栈帧大小、不要插入需要分配的 spill；这里只能发物理寄存器指令。
8. **测试里 `-stop-after` 名字写错**。写成 `riscv-expand-pseudo` 会直接报 “pass is not registered”；更阴险的是写成另一个合法但更早的 pass，FileCheck 仍然通过，实际检查的不是你以为的阶段。先跑 `-debug-pass=Structure` 对齐名字。

---

## 18.13 本章重点整理

- `Pseudo` 是延迟决策：PreRA 要 vreg，PostRA 要物理寄存器，PreEmit 要等 outliner/branch relaxation，Atomics 要等最后。
- 真实骨架是 `RISCVExpandPseudoImplBase::run` → `expandMBB` → 子类 `expandMI`；没有 `expandMInstruction` / `expandPseudo`。
- 四个插入点：PreRA `addPreRegAlloc:569`、PostRA `addPreSched2:489`、PreEmit `addPreEmitPass2:523`、Atomics `addPreEmitPass2:533`。
- `PseudoCALL` / `PseudoTAIL` / `PseudoJump` 不在四个 pass 里，由 `RISCVMCCodeEmitter::expandFunctionCall` 在编码期展开；`PseudoLI` / `PseudoLA` 在 AsmParser 展开。
- `PseudoLLA` 用 `pcrel_hi` 临时符号把 `auipc+addi` 绑成一对；`PseudoMovAddr` 在 PostRA 直接 `lui+addi`。
- `PseudoCCMOVGPR` / `PseudoCCADD*` 是 SFB/conditional-move 路径，不是 Zicond；Zicond 在 ISel 就变成 `czero`。
- `PseudoAtomic*` 只在 `HasStdExtZalrsc && !HasStdExtZaamo` 时出现，展开为 LR/SC 循环，放在最后是为了 forward progress。
- `getInstSizeInBytes` 是 BranchRelaxation 的输入；`PseudoCALL` 必须按 8 字节预算，PreEmit/Atomics 的 `Size` 必须覆盖它们新建的分支。

---

## 18.14 自我检核

- [ ] 能说出四个展开 pass 的名字、插入钩子和 `-stop-after` 名。
- [ ] 能解释为什么 `PseudoCALL` 不在 PreRA/PostRA 里展开，而是在 MC 编码期。
- [ ] 能说明 `PseudoLI` 是 AsmParser 展开，且 `Size = 32` 不会进入 BranchRelaxation。
- [ ] 能区分 Zicond 的 `czero` 路径与 `PseudoCCMOVGPR` 的 SFB 路径。
- [ ] 能解释 `+zalrsc,-zaamo` 为什么产生 `PseudoAtomicLoadAdd32`，而 `+a` 直接产生 `amoadd.w`。
- [ ] 能说明 `getInstSizeInBytes` 估小会怎样影响 BranchRelaxation，以及 Base 的断言为什么存在。
- [ ] 能用 `-debug-pass=Structure` 验证 pass 顺序，而不是只信网上文章。

---

## 18.15 延伸阅读

- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoBase.h`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoPreRA.cpp`；`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoPostRA.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoPreEmit.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoAtomics.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/BranchRelaxation.cpp`

→ 下一章：[Ch 19 — 机器层优化 pass](./19-machine-optimizations.md)
