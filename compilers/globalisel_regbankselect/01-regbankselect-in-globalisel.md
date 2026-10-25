# Ch 1 — GlobalISel 全景中的 RegBankSelect

> **目标**：把 RegBankSelect 放回 GlobalISel 管线的正确位置——它读什么、写什么、不负责什么。学完你能分清 **bank / regclass / LLT** 三个概念，说清它与 Legalizer、InstructionSelect、寄存器分配的边界，并解释“bank 看起来没问题”为什么仍可能在后面炸掉。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（实测）；`llvm-23.1.4-src` checkout（源码依据）。主 target AArch64，RISC-V 作对照。
>
> **证据约定**：标 `实测` 的 MIR 都来自本机 23.1.4 二进制；标 `源码依据` 的只引用源码行，没有跑构建；标 `guided` 的需要你本地复现。

## 为什么需要这个？

RegBankSelect 是 GlobalISel 里最容易被“跳过”的一站：输出不是汇编，`llc` 默认不打印；它不改变 IR 语义，只给虚拟寄存器贴标签；失败还经常被 fallback 吞掉。于是三种完全不同的情况在最终汇编里长得一样：bank 选对了；bank 选错了但 repair 插了 COPY 兜住；RegBankSelect 直接失败、整个函数退回 SelectionDAG。

更常见的困惑是概念混用。下面四句话都有人在 review 里说过，但只有一句是对的：

| 说法 | 对错 | 原因 |
| --- | --- | --- |
| “`%0:gpr(i64)` 里的 gpr 就是 x0–x30 这个寄存器类” | 错 | gpr 是 bank，不是 regclass |
| “`i32` 类型决定了它必须在 GPR” | 错 | 同一个 LLT 可以落在不同 bank（Ch 3 有 MIR 证据） |
| “RegBankSelect 之后寄存器就定了” | 错 | 只定了 bank，regclass 与物理寄存器都还没定 |
| “InstructionSelect 失败是 pattern 没写” | 不一定 | 很可能是上游给的 bank 无法选择 |

后面 Ch 5–10 讲数据模型与算法，Ch 11–15 讲真实语义，都以本章的职责划分为前提。

## 先建立直觉

把一次编译想成给货物安排仓库：

- **LLT**（Low-Level Type）是货物的**体积和形状**：`i32`、`f32`、`p0`、`<4 x s32>`。
- **bank** 是**园区**：AArch64 有 GPR 园区、FPR 园区、CC 园区。
- **regclass** 是园区里的**货架规格**：`gpr32`、`gpr64`、`fpr32`、`fpr128`。
- **物理寄存器**是**具体货位**：`$x0`、`$s0`、`$nzcv`。

Legalizer 负责把货物切成园区能收下的尺寸；RegBankSelect 只决定“进哪个园区”；InstructionSelect 决定“用哪台机器处理”；寄存器分配才决定“放哪个货位”。

```text
        LLVM IR
           │ IRTranslator
           ▼
   gMIR：%0:_(i32)        只有 LLT，无 bank、无 regclass
           │ Legalizer
           ▼
   legal gMIR：%0:_(i32)  操作与类型都合法，仍无 bank
           │ RegBankSelect        ← 本章主角
           ▼
   banked gMIR：%0:gpr(i32)      有 bank，无 regclass
           │ InstructionSelect
           ▼
   selected MIR：%0:gpr32        有 regclass（ADDWrr 这类目标指令）
           │ Register Allocator
           ▼
   final MIR：$w0                物理寄存器
```

一个关键事实：**一个虚拟寄存器的“类或 bank”字段是二选一的**。源码里它是 `PointerUnion`：

```cpp
// llvm/include/llvm/CodeGen/MachineRegisterInfo.h:46-48
/// Convenient type to represent either a register class or a register bank.
using RegClassOrRegBank =
    PointerUnion<const TargetRegisterClass *, const RegisterBank *>;
```

所以 `%0:gpr(i32)` 和 `%0:gpr32` 不是“同一个东西的两种写法”，而是同一个字段在两个阶段装的东西：RegBankSelect 写 `RegisterBank *`，InstructionSelect 之后换成 `TargetRegisterClass *`。调试时不能拿 ISel 之后的 regclass 去反推 RBS 的决策——中间已经被改写了。

## 核心概念

| 概念 | 类型 | 谁写 | MIR 里的样子 |
| --- | --- | --- | --- |
| LLT | `LLT`（`TypeSize` + kind + 元素） | IRTranslator 建 vreg 时写入 | `%0:_(i32)` |
| bank | `const RegisterBank *` | RegBankSelect | `%0:gpr(i32)` |
| regclass | `const TargetRegisterClass *` | InstructionSelect 约束 | `%0:gpr32` |
| 物理寄存器 | `Register`（编号） | 寄存器分配 | `$w0` |

三者的“语义方向”完全不同：

- **LLT 描述“值是什么”**：几个 bit、是不是指针、是不是向量。它贯穿整条管线，直到 `Selected` 之后才被清掉。
- **bank 描述“这个值在哪类寄存器文件里活动”**：GPR 还是 FPR。它不保证能分配，只保证指令选择时知道去哪边找 pattern。
- **regclass 描述“可以分配哪些物理寄存器”**：`gpr32` 是 32 位通用寄存器的集合。它比 bank 窄，是寄存器分配真正需要的输入。

一个容易忽略的点：**同一个 bank 覆盖多个 regclass**。AArch64 的 GPR bank 覆盖所有 GPR 子类（含 64 位寄存器对），FPR bank 覆盖 `FPR16/32/64/128` 与 `QQQQ`、`ZPR`。Ch 3、Ch 4 会把覆盖表拆开看。

### 各阶段的职责边界

| 阶段 | 输入 | 输出 | 不负责 |
| --- | --- | --- | --- |
| IRTranslator | LLVM IR | 带 LLT 的 gMIR | 合法性、bank、寄存器 |
| Legalizer | gMIR | 每条指令都合法的 gMIR | bank、regclass |
| **RegBankSelect** | legal gMIR | 每个 generic vreg 都有 bank 的 gMIR | regclass、物理寄存器、指令选择 |
| InstructionSelect | banked gMIR | 目标指令 + regclass | 物理寄存器、spill |
| Register Allocator | selected MIR | 物理寄存器 / spill 代码 | bank 的正确性（它只信上游） |

最重要的一列是最后一列：**下游不会替你修 bank**。寄存器分配看到的是已经约束好的 regclass，它只会抱怨“压力太大”或“无法分配”，不会回头说“你当初该选 FPR”。

## 底层机制

### pass 在管线里的位置

AArch64 的 GlobalISel 管线由 `TargetPassConfig::addCoreISelPasses` 串起来：

```cpp
// llvm/lib/CodeGen/TargetPassConfig.cpp:1038-1059（节选）
if (Selector == SelectorType::GlobalISel) {
  SaveAndRestore SavedAddingMachinePasses(AddingMachinePasses, true);
  if (addIRTranslator())
    return true;
  addPreLegalizeMachineIR();
  if (addLegalizeMachineIR())
    return true;
  // Before running the register bank selector, ask the target if it
  // wants to run some passes.
  addPreRegBankSelect();
  if (addRegBankSelect())
    return true;
  addPreGlobalInstructionSelect();
  if (addGlobalInstructionSelect())
    return true;
}
```

AArch64 的实现很直接：

```cpp
// llvm/lib/Target/AArch64/AArch64TargetMachine.cpp:811-814
bool AArch64PassConfig::addRegBankSelect() {
  addPass(new RegBankSelect());
  return false;
}
```

`new RegBankSelect()` 用的是默认参数，也就是 **Fast**（`RegBankSelect.h:618`：`RegBankSelect(Mode RunningMode = Fast);`）。这是 Ch 8 的主题，本章只需知道：管线里默认跑 Fast。

注意 `addPreRegBankSelect()` 里还会挂 combiner 与 lowering（`AArch64TargetMachine.cpp:800-809`）。RegBankSelect 看到的 gMIR 已被 target 预处理过，不是 Legalizer 的原始输出。

### 输入输出契约

```cpp
// llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h:624-634
MachineFunctionProperties getRequiredProperties() const override {
  return MachineFunctionProperties().setIsSSA().setLegalized();
}

MachineFunctionProperties getSetProperties() const override {
  return MachineFunctionProperties().setRegBankSelected();
}

MachineFunctionProperties getClearedProperties() const override {
  return MachineFunctionProperties().setNoPHIs();
}
```

三件事：

1. **要求**：`IsSSA` + `Legalized`。没合法化的输入不属于它的职责范围。
2. **保证**：跑完置 `RegBankSelected`。
3. **清除**：`NoPHIs`。因为 repair 可能拆临界边（`RegBankSelect.cpp:952-963` 调用 `SplitCriticalEdge`），拆完就可能需要新的 PHI，所以它不能承诺“没有 PHI”。

这里的“要求”在 release 构建里**不会被检查**，Ch 2 用实测说明后果。

### 主循环做了什么

```cpp
// llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:697-735（节选）
bool RegBankSelect::assignRegisterBanks(MachineFunction &MF) {
  // Walk the function and assign register banks to all operands.
  // Use a RPOT to make sure all registers are assigned before we choose
  // the best mapping of the current instruction.
  ReversePostOrderTraversal<MachineFunction*> RPOT(&MF);
  for (MachineBasicBlock *MBB : RPOT) {
    ...
    while (!WorkList.empty()) {
      MachineInstr &MI = *WorkList.pop_back_val();
      // Ignore target-specific post-isel instructions: they should use proper
      // regclasses.
      if (isTargetSpecificOpcode(MI.getOpcode()) && !MI.isPreISelOpcode())
        continue;
      // Ignore inline asm instructions: they should use physical
      // registers/regclasses
      if (MI.isInlineAsm())
        continue;
      // Ignore IMPLICIT_DEF which must have a regclass.
      if (MI.isImplicitDef())
        continue;
      if (!assignInstr(MI)) {
        reportGISelFailure(MF, *MORE, "gisel-regbankselect",
                           "unable to map instruction", MI);
        return false;
      }
    }
  }
  return true;
}
```

三个细节：

- **RPOT 顺序**：沿逆后序走，保证使用点之前定义点已被赋过 bank。这也是它只能做“局部 + 已见信息”决策的原因。
- **跳过三类指令**：目标相关指令（已有 regclass）、inline asm、`IMPLICIT_DEF`。MIR 里看起来“没被处理”的指令，很多属于这三类。
- **一条失败就整函数失败**：不跳过、不猜，直接 `reportGISelFailure`。这个失败如何被兜住，是 Ch 2 的内容。

### 单条指令的决策与写回

```cpp
// llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp:674-694（节选）
if (OptMode == RegBankSelect::Mode::Fast) {
  BestMapping = &RBI->getInstrMapping(MI);
  MappingCost DefaultCost = computeMapping(MI, *BestMapping, RepairPts);
  (void)DefaultCost;
  if (DefaultCost == MappingCost::ImpossibleCost())
    return false;
} else {
  RegisterBankInfo::InstructionMappings PossibleMappings =
      RBI->getInstrPossibleMappings(MI);
  if (PossibleMappings.empty())
    return false;
  BestMapping = &findBestMapping(MI, PossibleMappings, RepairPts);
}
assert(BestMapping->verify(MI) && "Invalid instruction mapping");
```

Fast 只问一次“默认映射是什么”；Greedy 问“所有候选里哪个最便宜”。两者都不做跨指令搜索，差别只在候选集合与代价模型。随后 `applyMapping` 写回：需要换 bank 的操作数插 `G_COPY`，其余直接 `MRI->setRegBank`（`RegBankSelect.cpp:614-632`）。这解释了两类完全不同的“赋值”：`%3:gpr(i32) = G_ADD` 是原指令被重新标注，`%9:gpr(i32) = COPY %2(i32)` 是 repair 新造的指令。

## 示例一：同一条函数走完四个阶段（实测）

```llvm
; .scratch/part0/mixp.ll
define float @mixp(float %a, i32 %n) {
entry:
  %i = add i32 %n, 1
  %f = sitofp i32 %i to float
  %r = fadd float %f, %a
  ret float %r
}
```

`llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=<pass>` 依次停在四个阶段：

```text
; ---- irtranslator ----
%0:_(f32) = COPY $s0
%1:_(i32) = COPY $w0
%2:_(i32) = G_CONSTANT i32 1
%3:_(i32) = G_ADD %1, %2
%4:_(f32) = G_SITOFP %3(i32)
%5:_(f32) = G_FADD %4, %0
$s0 = COPY %5(f32)

; ---- legalizer ----（本例逐行与上一阶段相同，说明它已经合法）
; 省略重复的 7 行

; ---- regbankselect ----
%0:fpr(f32) = COPY $s0
%1:gpr(i32) = COPY $w0
%2:gpr(i32) = G_CONSTANT i32 1
%3:gpr(i32) = G_ADD %1, %2
%4:fpr(f32) = G_SITOFP %3(i32)
%5:fpr(f32) = G_FADD %4, %0
$s0 = COPY %5(f32)

; ---- instruction-select ----
%0:fpr32 = COPY $s0
%1:gpr32sp = COPY $w0
%3:gpr32common = ADDWri %1, 1, 0
%4:fpr32 = nofpexcept SCVTFUWSri %3, implicit $fpcr
%5:fpr32 = nofpexcept FADDSrr %4, %0, implicit $fpcr
$s0 = COPY %5
```

四个结论，都能直接从输出读出来：

1. **Legalizer 可以什么都不改**。“跑过 Legalizer”不等于“MIR 会变”，只保证“每条指令都合法”。
2. **RBS 只加 bank**：`_` 变成 `gpr` / `fpr`，指令与操作数结构完全没动。
3. **ISel 换 opcode 并给出 regclass**：`G_ADD` → `ADDWri`（常量被折叠），`G_SITOFP` → `SCVTFUWSri`。
4. **同一个 vreg 的 LLT 不变**：`%3` 从 `_(i32)` 到 `gpr(i32)` 再到 `gpr32common`，`i32` 一直都在，变的是另外两个字段。

注意 `%1:gpr32sp` 与 `%3:gpr32common`：这是 ISel 根据具体指令约束收紧出来的子类。RegBankSelect 阶段它们都只是 `gpr`。

## 示例二：bank 不是 regclass，也不由 LLT 决定（实测）

```llvm
; .scratch/part0/bitcast.ll
define float @bc(i32 %x) {
entry:
  %f = bitcast i32 %x to float
  %r = fadd float %f, %f
  ret float %r
}
```

```text
; -stop-after=regbankselect（Fast）
%0:gpr(i32) = COPY $w0
%1:gpr(f32) = G_BITCAST %0(i32)     ; ← f32 落在 GPR！
%3:fpr(f32) = COPY %1(f32)
%4:fpr(f32) = COPY %1(f32)
%2:fpr(f32) = G_FADD %3, %4
$s0 = COPY %2(f32)
```

三处反直觉：

- `%1:gpr(f32)`：一个 `f32` 的 LLT 被放进 GPR bank。原因在 `AArch64RegisterBankInfo.cpp:948-964`：`G_BITCAST` 的默认映射按 `DstIsGPR = !DstTy.isVector() && DstTy.getSizeInBits() <= 64` 判定，`f32` 满足条件，于是默认放 GPR，bitcast 本身退化成零代价。
- `%3` / `%4` 两条 COPY 不是“重复”，而是**两个使用点各修一次**。repair 按操作数位置做，不按值做。
- 加 `-regbankselect-greedy` 重跑，输出**完全相同**（实测）。因为 FPR 版本的 alternative mapping 要额外付 repair 代价。别把“跑了 greedy”当成“结果一定变”。

这一条同时说明了本章的核心命题：**bank 是“指令与目标约束”协商出来的，不是 LLT 的单值函数**。

## 示例三：bank 是 ISel 的硬前提（实测）

把示例一（`mixp`）的 RBS 输出里的 `%4:fpr(f32)` 改成 `%4:_(f32)`（相当于假装 RBS 漏掉了它），再喂给 InstructionSelect：

```console
$ llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
      -global-isel-abort=1 nobank.mir -o NUL
error: nobank.mir:84:8: conflicting generic register banks
    %4:_(f32) = G_SITOFP %3(i32)
       ^
```

连 MIR 解析都没过，直接在 `%4` 上报“bank 冲突”。这说明：bank 注解是 MIR 的一部分；def 与 use 的 bank 必须一致；ISel 没有“猜 bank”的能力，也不会退回 Legalizer 去补。

真实 bug 里看到 ISel 的 “cannot select” 或 verifier 的 “generic instruction”，先检查这条链上有没有 vreg 缺 bank 或 bank 不一致，而不是先怀疑 pattern。

## 对比与取舍

**为什么不让 Legalizer 顺手选 bank？** 技术上可行，工程上不可取。Legalizer 的职责是“让每条指令合法”，它会大量拆解、重建指令（`G_ADD i128` 拆成 `G_UADDO`/`G_UADDE`，见 Ch 3 进阶的 `add128` 实测）。如果在拆解过程中就定 bank，等于把“指令形态”与“寄存器文件”绑死：后续 combiner 一旦想合并两条指令，就得回滚 bank。

**为什么不让 ISel 自己选 bank？** 因为 ISel 的 pattern 是按 regclass 写的：`ADDWrr` 要 `GPR32`，`FADDSrr` 要 `FPR32`。如果 ISel 同时负责选 bank，它得为每个 opcode 维护“GPR 版 / FPR 版 / 跨 bank 版”的组合爆炸，而且全局的 COPY 代价没人统一算。抽出“选园区”这一层，ISel 只处理“园区内的货架”。

**RBS 与 RA 的边界**：`MachineFunction.h:170-176` 对 `Selected` 的定义是，ISel 之后所有 pre-isel generic 指令消失、generic vreg 被约束成 regclass、附加 size 被清掉。也就是说 **LLT 在 RA 之前就退场了**，RA 只看 regclass 与活跃区间。所以 RBS 选错 bank 的代价通常表现为“多了 COPY、压力变大、spill 变多”，而不是 RA 直接报错。

## 踩雷集锦

**1. 把 `gpr` 当成“x0–x30”**

`gpr` 是 bank，覆盖一堆 regclass（含 64 位寄存器对）。`x0–x30` 是物理寄存器；`gpr32` / `gpr64` 才是 regclass。看到 `%0:gpr(i64)` 就说“它已经是 x 寄存器了”，会让后面所有关于 RA 的推理都错位。

**2. 以为 RegBankSelect 会改指令或做全局优化**

它只做两件事：给 vreg 贴 bank、必要时插 `G_COPY`。它按 RPOT 逐条处理，Greedy 也只是“在候选映射里选局部最便宜的”。指望它把 `G_OR` 从 GPR 挪到 FPR 以消掉三条 COPY，只在目标提供了对应 alternative mapping 时才可能发生（Ch 9 会看到 `or.ll` 的 Fast=3 条 COPY、Greedy=0 条）。

**3. 用“编译通过”判断 RBS 成功**

`-run-pass` 下失败也可能 exit 0，只是 MIR 里多了 `failedISel: true`；全管线里失败会 fallback 到 SelectionDAG，汇编照样出得来。判定标准只能是 MIR 属性 + 显式 abort，Ch 2 给完整判据。

**4. 以为 `i1` 会进 CC bank**

在普通 AArch64 GlobalISel 路径上，`G_ICMP` / `G_FCMP` 的结果在 Legalizer 就被扩成 `i32`，RegBankSelect 把它放进 **GPR**；`$nzcv` 是 ISel 之后才出现的物理寄存器。Ch 4 有完整 MIR 证据。

## 进阶

- **Fast 与 Greedy 的分析依赖不同**。`RegBankSelect.cpp:94-100` 只在非 Fast 时才取 `MachineBlockFrequencyInfo` / `MachineBranchProbabilityInfo`；`getAnalysisUsage`（105-114）同样只在这时 `addRequired`。Fast 模式连块频率都不看，天然不可能做“按热路径权衡”的决策。
- **hint 指令走特例**。`assignInstr` 开头对 `G_ASSERT_ZEXT` / `G_ASSERT_SEXT` / `G_ASSERT_ALIGN` 直接复制源寄存器的 bank（`RegBankSelect.cpp:653-668`），不查 mapping。这类指令在 MIR 里很常见，别把它们当成“RBS 没处理”。
- **RISC-V 对照**：同一个 `float` 参数在 AArch64 是 `%0:fpr(f32)`，在 RISC-V（`-mattr=+f`）是 `%0:fprb(s32)`。LLT 都不一样（`f32` vs `s32`），bank 名字也不一样。bank/LLT 的对应关系是**每个 target 自己定义的**，不能跨 target 套用直觉。

## 动手练习

1. 用 `mixp.ll`（或自选一个整数与浮点混合的函数）跑四个 `-stop-after`，把每个 vreg 的 LLT、bank、regclass 填成一张三列表。重点找：哪个 vreg 的 LLT 在三个阶段都没变，哪两个字段变了。
2. 把 `mixp.ll` 里的 `sitofp` 换成 `fptosi`（`float` → `i32`），预测 `%3` 与 `%4` 的 bank，再跑 `-stop-after=regbankselect` 验证。如果结果与预测不同，从 `AArch64RegisterBankInfo.cpp` 里找对应的 opcode 分支解释。
3. 手工把某条 RBS 输出里的 `:gpr` 删掉，跑 `-run-pass=instruction-select`，记录报错文本。再想想：为什么报的是 “conflicting generic register banks” 而不是 “missing bank”？（提示：看 def 与 use 两侧。）

## 本章重点整理

- RegBankSelect 的输入是 legal gMIR，输出是 **banked gMIR**：有 bank、无 regclass、仍是 `G_*`。
- 一个 vreg 的“类或 bank”字段是 `PointerUnion`，**bank 与 regclass 互斥**，由不同 pass 先后写入。
- LLT 描述值本身，bank 描述寄存器文件，regclass 描述可分配的物理寄存器集合；三者不能互相替代。
- RBS 的位置固定：Legalizer 之后、InstructionSelect 之前，由 `addRegBankSelect()` 挂载；AArch64 用默认的 Fast。
- 它按 RPOT 逐条处理，跳过 target-specific / inline asm / `IMPLICIT_DEF`，一条失败即整函数失败。
- 下游不会替你修 bank：ISel 要 regclass 级的 pattern，RA 只看 regclass。

## 自我检核

- [ ] 能不看笔记说出 bank、regclass、LLT 各由哪个 pass 写入
- [ ] 能解释为什么 `%1:gpr(f32)` 是合法的，以及它为什么会导致两条 COPY
- [ ] 能说出 `getRequiredProperties` / `getSetProperties` / `getClearedProperties` 各返回什么
- [ ] 能解释为什么 RegBankSelect 不放在 Legalizer 里
- [ ] 能解释“RBS 成功”与“最终汇编正确”为什么不是一回事
- [ ] 拿到一份 ISel 后的 MIR，能指出哪些信息是 RBS 给的、哪些是 ISel 新加的

## 延伸阅读

- **[LLVM GlobalISel — Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - **读哪里**：pipeline 总览图与 “RegBankSelect” 小节。
  - **学什么**：每个 pass 的前置/输出属性，以及官方对 RegBankSelect 职责的一句话定义。
  - **和本章的关联**：职责边界表就是这张图的细化；差异处（例如属性检查在 release 下不生效）Ch 2 会补。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：Machine Function 的 YAML 字段、`registers` 的 `class:` 字段、`-run-pass`。
  - **学什么**：`%0:gpr(i32)` 这种写法的语法定义，以及 `class:` 与 bank 的关系。
  - **和本章的关联**：不会读 MIR，本章三个示例都只能看个热闹。
- **[RegBankSelect.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/RegBankSelect.h)**
  - **读哪里**：文件头注释、`enum Mode`（96-103）、构造函数默认值（618）、属性三连（624-634）。
  - **学什么**：pass 的自我定位与契约；文件头那段 `cost(I, RegBank)` 公式是 Ch 9 的起点。
  - **和本章的关联**：本章所有“契约”结论都出自这里。
- **[TargetPassConfig.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/TargetPassConfig.cpp)**
  - **读哪里**：`addCoreISelPasses`（995-1080）。
  - **学什么**：GlobalISel 五段如何串起来，fallback 的 SelectionDAG 在哪一行被挂上。
  - **和本章的关联**：解释“为什么 RBS 之前还有 combiner/lowering”。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`G_BITCAST` 的默认映射（948-965）与 `getInstrAlternativeMappings`（259-364）。
  - **学什么**：为什么 `f32` 会进 GPR，以及为什么 greedy 在本例中不改变结果。
  - **和本章的关联**：示例二的直接依据。

下一章把镜头对准 pass 契约本身：`Legalized` 到 `RegBankSelected` 的状态如何流转，`-stop-after` 与 `-run-pass` 的区别，以及失败到底长什么样。

→ [Ch 2 pass 契约、MIR 属性与失败模式](./02-pass-contract-and-mir-state.md)