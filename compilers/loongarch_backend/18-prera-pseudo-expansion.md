# Ch 18 — 伪指令展开的三层：pre-RA、post-RA、MC

> 本章目标：知道 `PseudoLA_*`、`PseudoCALL_MEDIUM`、`PseudoAddTPRel_D` 分别在哪一层展开；理解“为什么 pre-RA 展开要创建虚拟寄存器”，以及为什么原子伪指令要拖到最后一刻。

---

## 18.1 三层展开

| 层 | pass / 位置 | 典型伪指令 | 为什么在这一层 |
|---|---|---|---|
| **pre-RA** | `loongarch-prera-expand-pseudo` | `PseudoLA_PCREL`、`PseudoLA_TLS_LE`、`PseudoCALL_SMALL`、`PseudoTAIL_SMALL` | 展开需要**虚拟寄存器**，让 RA 统一分配；展开后的指令数影响 spill/调度 |
| **post-RA** | `loongarch-expand-pseudo` | `PseudoCopyCFR`、`PseudoCALL_MEDIUM`、`PseudoTAIL_MEDIUM`、`PseudoAddUpperImm` | 用**物理寄存器**（`$ra`/`$r20`），且必须整体替换、不能被 RA 拆开 |
| **MC** | `MCCodeEmitter` / AsmParser | `PseudoAddTPRel_D`、`PseudoLI_W/D`、`la.*` | 需要 fixup/重定位，或者只在汇编文本层存在 |

pass 注册（`LoongArchTargetMachine.cpp`）：

```cpp
void LoongArchPassConfig::addPreRegAlloc() {
  addPass(createLoongArchPreRAExpandPseudoPass());
  if (OptLevel != None && EnableMergeBaseOffset)
    addPass(createLoongArchMergeBaseOffsetOptPass());
}

void LoongArchPassConfig::addPreEmitPass2() {
  addPass(createLoongArchExpandPseudoPass());
  // Schedule the expansion of AtomicPseudos at the last possible moment,
  // avoiding the possibility for other passes to break the requirements for
  // forward progress in the LL/SC block.
  addPass(createLoongArchExpandAtomicPseudoPass());
  if (OptLevel != None)
    addPass(createLoongArchMemoryBarrierOptPass());
}
```

**顺序**：pre-RA 在寄存器分配前，post-RA 在 `addPreEmitPass2`（几乎最后）。原子伪指令更晚，因为它要求 LL/SC 循环不被其它 pass 打断。

---

## 18.2 实测：`PseudoLA_TLS_LE` 的展开

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -stop-before=loongarch-prera-expand-pseudo one.ll -o -
```

```text
body:             |
    %0:gpr = PseudoLA_TLS_LE @t
    PseudoRET implicit $r4
```

展开后：

```console
$ llc ... -stop-after=loongarch-prera-expand-pseudo one.ll -o -
```

```text
body:             |
    %3:gpr = LU12I_W target-flags(loongarch-le-hi-r) @t
    %2:gpr = PseudoAddTPRel_D %3, $r2, target-flags(loongarch-le-add-r) @t
    PseudoRET implicit $r4
```

三个关键点：

1. **创建了两个虚拟寄存器 `%2`/`%3`**（`createVirtualRegister(&LoongArch::GPRRegClass)`）。这就是“为什么必须在 RA 前”：如果 post-RA 再创建虚拟寄存器，寄存器分配已经结束，没人给它们分配物理寄存器。
2. **`PseudoAddTPRel_D` 没有被展开**——它要留到 MC 层，因为它的编码需要 `R_LARCH_TLS_LE_ADD_R` 重定位，而重定位只能在 `MCCodeEmitter` 里生成。
3. **`target-flags(loongarch-le-hi-r)`** 是 `MachineOperand` 的 target flag，最终映射到 `R_LARCH_TLS_LE_HI20_R`。

再往后看 MC 层：`PseudoAddTPRel_D` 在 `LoongArchMCCodeEmitter.cpp:276` 被编码成 `ADD_D`，并带上 `R_LARCH_TLS_LE_ADD_R`。

---

## 18.3 为什么 `PseudoCALL_MEDIUM` 是 post-RA

`PseudoCALL_MEDIUM`（`Size = 8`，`Defs = [R1, R20]`）展开成：

```text
pcaddu18i $ra, %call36(func)
jirl      $ra, $ra, 0
```

它必须整体替换，且使用**物理** `$ra`。如果 pre-RA 展开：

- 两条指令之间可能被调度器插入别的指令（`jirl` 必须紧跟 `pcaddu18i` 吗？不一定，但 `$ra` 会被写两次）；
- RA 可能把 `pcaddu18i` 的目标寄存器分配成别的，破坏 `jirl` 的约定；
- `Defs=[R1,R20]` 的 clobber 信息在 RA 前没有意义。

所以放到 post-RA，用 `MI.setDesc()` 原地改写成两条固定指令。`expandFunctionCALL()`（`LoongArchExpandPseudoInsts.cpp:875`）里可以看到 `ScratchReg = IsTailCall ? R20 : R1`。

---

## 18.4 pre-RA 展开的“隐藏成本”

`expandPcaxxu12iInstPair()` 里：

```cpp
Register ScratchReg =
    MF->getRegInfo().createVirtualRegister(&LoongArch::GPRRegClass);
```

每个 `la.pcrel` 都可能吃一个临时虚拟寄存器。大函数里大量 `la.*` 会显著提高寄存器压力，甚至导致 spill。

这就是为什么 `MergeBaseOffset`（Ch 19）要在 pre-RA 展开**之后**跑：它要把多个 `la` 的基址合并，减少重复的 `pcalau12i`，从而降低压力。

---

## 18.5 两个 pass 的名字容易混

| pass 名 | 用途 |
|---|---|
| `loongarch-prera-expand-pseudo` | pre-RA 展开（本层） |
| `loongarch-expand-pseudo` | post-RA 展开 |
| `loongarch-expand-atomic-pseudo` | 原子伪指令（更晚） |

用 `-stop-before=`/`-stop-after=` 时写错名字会静默地停在不该停的位置（`llc` 会报 “unknown pass name” 吗？新 PM 下会报错，但 MIR 解析路径可能不同）。**先 `llc --help-hidden | grep loongarch` 确认名字**。

---

## 18.6 反直觉点

1. **`PseudoAddTPRel_D` 一直活到 MC 层**，不是 pre-RA 展开完就没了。
2. **pre-RA 展开会创建虚拟寄存器**，所以它必须在 RA 前；post-RA 展开只能用物理寄存器。
3. **`PseudoCALL_MEDIUM` 是 post-RA**，因为它是固定指令对 + 物理 `$ra`。
4. **原子伪指令最晚展开**，为了保证 LL/SC 循环的原子性。
5. **`PseudoLA_PCREL` 展开成 `PseudoLA_PCREL` 的两条指令后，还可能被 `MergeBaseOffset` 再优化**（Ch 19）。
6. **同一个 `.td` 里的 `Pseudo` 不等于“马上展开”**，展开时机由 pass 决定。
7. **`-stop-after` 看到的是“所有 pass 跑完这一层”的 MIR**，不是单条指令的中间态。

---

## 18.7 本章证据

| 结论 | 源码/命令 |
|---|---|
| pre-RA 展开清单 | `LoongArchExpandPseudoInsts.cpp:137-180` |
| post-RA 展开清单 | `:805-820` |
| pass 注册顺序 | `LoongArchTargetMachine.cpp:206-226` |
| `PseudoLA_TLS_LE` 实测 | 本章 18.2 |
| `PseudoAddTPRel_D` 到 MC | `LoongArchMCCodeEmitter.cpp:276,306` |
| `expandFunctionCALL` | `LoongArchExpandPseudoInsts.cpp:875` |
| 原子 pass | `LoongArchTargetMachine.cpp:209-212` |

---

## 18.8 练习

1. 用 `-stop-before`/`-stop-after` 观察 `PseudoCALL_MEDIUM` 在 post-RA 展开前后的 MIR。
2. 找一个会创建多个虚拟寄存器的 `la.*` 函数，数一数展开后新增了多少 vreg。
3. 解释为什么 `PseudoLI_W` 不在 pre-RA 展开（提示：它在 AsmParser 的 `processInstruction` 里）。
4. 读 `expandFunctionCALL` 的 medium 分支，说明 `ScratchReg` 为什么尾调用用 `$r20`。
5. 把 `-stop-after=loongarch-prera-expand-pseudo` 的输出喂给 `llc -run-pass=...`，验证 MIR 可往返。

---

## 18.9 延伸阅读

1. **MIR 单 pass 测试的官方流程** — `llvm/docs/MIRLangRef.md`
   - 读什么：`Testing Individual Code Generation Passes` 小节：用 `-stop-after`/`-stop-before` 生成输入、用 `-run-pass` 只跑一个 pass、用 `-simplify-mir` 精简，以及同一 pass 多次运行时 `dead-mi-elimination,1` 的 run index 写法
   - 学到什么：本章 18.2/18.5 用 `-stop-before`/`-stop-after` 观察三层展开；这份手册补上“怎样把观察固化成 MIR 回归测试”，以及哪些字段（`liveins`、`successors` 概率、IR 引用）可以删。这样你不必每次靠整段 `llc` 输出判断某一层的行为。
   - 前提：读过本章 18.1 的三层表，知道 `loongarch-prera-expand-pseudo`、`loongarch-expand-pseudo` 等 pass 名。

2. **MC 层里谁负责展开 pseudo** — `llvm/docs/CodeGenerator.md`
   - 读什么：`Code Emission` 小节中讲 `MCInstLower` 的那一段：把 `MachineInstr` 降到 `MCInst`，并展开 code generator 使用的 pseudo ops
   - 学到什么：本章把第三层笼统称为“MC”；文档具体指出这一步通常落在 `MCInstLower`，之后才交给 `MCStreamer`/`MCCodeEmitter`。补上 `PseudoAddTPRel_D` 为什么能一路活到编码器，以及“MC 层”内部还分 lowering 与 encoding 两段——这决定你该在 `-stop-after` 的哪一步找它。
   - 前提：知道 `MachineInstr` → `MCInst` 的基本关系。

3. **TableGen 的 PseudoLowering backend** — `llvm/docs/TableGen/BackEnds.md`
   - 读什么：`PseudoLowering` 小节的 Purpose/Output/Usage 三行
   - 学到什么：除了手写 `LoongArchExpandPseudoInsts.cpp`，LLVM 还有一条由 TableGen 生成 `emitPseudoExpansionLowering()` 的声明式路线，挂在 AsmPrinter 上而不是独立 pass。补上本章没提的第三种实现方式，也能解释为什么有些目标的分支/调用 pseudo 在 `.td` 里就写好了展开模板。
   - 前提：知道 TableGen backend 会生成 C++ 片段；读过本章 18.1。

4. **`la.*` 汇编宏展开与重定位标记** — `llvm/test/MC/LoongArch/Macros/macros-la.s`
   - 读什么：`la.abs`、`la.pcrel`、`la.tls.le`、`la.tls.ie` 各段后面的 `CHECK` 与 `RELOC`/`RELAX` 检查
   - 学到什么：本章 18.2 从 MIR 看 `PseudoLA_TLS_LE`；这个 MC 测试从汇编文本看同一族宏的展开：`la.pcrel` 是 `pcalau12i` + `addi.d`，`la.tls.le` 是 `lu12i.w` + `ori`，`la.tls.ie` 是 `pcalau12i` + `ld.d`，并给出对应的 `R_LARCH_*` 重定位；部分段在 `+relax` 下还会多出 `R_LARCH_RELAX`。补上“第三层的宏展开和重定位标记是一起产生的”，也为 Ch 19 的 `MO_RELAX` 埋线。
   - 前提：会用 `llvm-mc` 与 `llvm-readobj -r`，知道 hi20/lo12 的分工。

## 18.10 下一步

下一章看两个“减少指令数”的机器层优化：`MergeBaseOffset`（合并基址）和 `OptWInstrs`（32 位指令优化）。
