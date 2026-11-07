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

## 18.9 下一步

下一章看两个“减少指令数”的机器层优化：`MergeBaseOffset`（合并基址）和 `OptWInstrs`（32 位指令优化）。
