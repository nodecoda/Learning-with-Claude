# Ch 20 — 原子操作与内存屏障

> **目标**：把 IR 的 `atomicrmw` / `cmpxchg` / `fence` 追到 AMO、LR/SC 循环、libcall 与 `.aq/.rl`，能说出每一步由哪个函数或 pattern 决定，并用 `llc`、`llvm-mc`、QEMU 验证。

> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，`llvm/lib/Target/RISCV` @ `0fc5afa04c23`，`X86;AArch64;RISCV`，assertions ON；QEMU 6.2 user-mode。

> **前置**：读完 Ch 15（自定义 lowering 与 intrinsic）、Ch 18（伪指令展开）。Ch 18 讲 `PseudoAtomic*` 何时展开；本章讲它为什么存在、展开成什么、顺序语义从哪来。

---

## 20.1 为什么原子是 RISC-V 后端的“政策重灾区”

原子 IR 语义固定，机器实现有四个自由度：**原子性来源**（AMO / LR/SC / libcall）、**顺序来源**（`.aq/.rl` / `fence` / 无）、**宽度**（32/64、8/16、超 XLEN）、**ABI**（A6S/A6C/A7、`amocas` 宽度）。同一个 `atomicrmw add i32 seq_cst` 在三种 `-march` 下是三条不同序列，但都必须正确。没有唯一正确 lowering，只有对当前 subtarget 与 ABI 正确的 lowering。

RVWMO 只需记住三件事：默认弱内存模型，普通 `lw/sw` 之间无顺序；`.aq`/`.rl` 是标在 AMO/LR/SC 上的标注，不是独立指令；`fence` 管数据，`fence.i` 管取指。base A 没有带标注的普通 load/store，所以 atomic load/store 必须靠 fence；带顺序的普通访存要等 Zalasr（`lw.aq`/`sw.rl`）。

`+a` 在本树已拆成两个子扩展（`RISCVFeatures.td:250`）：

```tablegen
def FeatureStdExtA : RISCVExtension<2, 1, "Atomic Instructions",
                     [FeatureStdExtZaamo, FeatureStdExtZalrsc]>, ...;
```

三个维护事实：

- **predicate 不统一。** AMO pattern 用 `HasStdExtA`（`RISCVInstrInfoA.td:203`），LR/SC 伪指令用 `HasStdExtZalrsc, NoStdExtZaamo`（`:319`），masked 路径只用 `HasStdExtZalrsc`（`:377`）。
- **前端会合成 `a`。** `RISCVISAInfo::updateCombination()`（`llvm/lib/TargetParser/RISCVISAInfo.cpp:990`，表在 `:984`）发现 `zaamo`+`zalrsc` 同时存在时补 `a`，所以 `-march=..._zaamo_zalrsc` 的 cc1 参数含 `+a`。
- **`llc -mattr` 不合成。** `+zaamo,+zalrsc` 没有 `+a`，两条 pattern 都不匹配，ISel 崩溃（20.9 实验 7）。

`RISCVUsage.md:168-172` 把 `Zaamo`/`Zalrsc` 标为 **Assembly Support**，只把 `A` 标为 Supported；但 pattern 确实会在 `+zalrsc,-a` 下生成 LR/SC。维护时以 `.td` 的 predicate 为准。

| # | 决策 | 位置 | 产物 |
|---|---|---|---|
| 1 | 扩展归一化 | `RISCVISAInfo.cpp:990`；`clang/lib/Basic/Targets/RISCV.h:193` | `+a`、`MaxAtomicInlineWidth` |
| 2 | IR 是否展开 | `RISCVISelLowering.cpp:30144`、`:30258` | `AtomicExpansionKind` |
| 3 | 是否插 fence | 同上 `:31044`、`:30110`、`:30126` | fence 位置与 pred/succ |
| 4 | AMO 还是 LR/SC | `RISCVInstrInfoA.td:203` / `:319` | AMO 或 `PseudoAtomic*` |
| 5 | ABI 修正与 ELF | `RISCVZacasABIFix.cpp:64`；`RISCVTargetStreamer.cpp:81` | 额外 fence、`atomic_abi` |

第 5 点最易漏：**ABI 不只由指令决定，还由 fence 位置和 ELF attribute 决定。**

---

## 20.2 先建立直觉：一个 `atomicrmw add` 的四种命运

```text
atomicrmw add i32 → 有 +a & zaamo ? AMOADD.W : (有 zalrsc ? LR/SC 循环 : libcall __atomic_fetch_add_4)
cmpxchg          → +zacas ? AMOCAS : LR/SC 循环
fence seq_cst    → fence rw,rw (前) + fence rw,rw (后) 视 ordering 而定
```

```text
atomicrmw add i32, ptr %p, i32 %v   (seq_cst)
        │ IR: shouldExpandAtomicRMWInIR()  30144
        │     Size=32、非 FP、非 Nand、无 forced-atomics → None
        ▼ ISel 按 subtarget predicate 选 pattern
   ┌──────────────────┬────────────────────────┬───────────────────────────┐
   │ +a（含 zaamo）    │ +zalrsc 且 -zaamo       │ 无 a / 无 zalrsc           │
   │ AMOADD.W.AQRL    │ PseudoAtomicLoadAdd32  │ __atomic_fetch_add_4       │
   │ 一条指令          │ → lr.w / add / sc.w    │ libcall，顺序号 5           │
   └──────────────────┴────────────────────────┴───────────────────────────┘

cmpxchg i32
   ├─ +zacas   → AMOCAS.W[.aqrl]（seq_cst 失败序还要前置 fence）
   ├─ +zalrsc  → PseudoCmpXchg32 → lr.w / bne / sc.w
   └─ 都没有    → __atomic_compare_exchange_4

fence seq_cst（system scope）→ fence rw, rw；atomic load/store 的前后 fence 由 ordering 决定
```

| 命运 | 决定者 | 条件 |
|---|---|---|
| `AMOADD.W` | `AMOPat`（`RISCVInstrInfoA.td:203`） | `HasStdExtA`（Ztso 下不带 `.aq/.rl`） |
| LR/SC | `PseudoAMOPat`（`:294`）+ `expandAtomicBinOp` | `HasStdExtZalrsc, NoStdExtZaamo` |
| libcall | `setMaxAtomicSizeInBitsSupported(0)`（`:902`） | 无 Zalrsc、无 forced-atomics |
| masked 8/16 | `emitMaskedAtomicRMWIntrinsic`（`:30198`） | `Size < 32 && !Zabha` |

**`None` 不等于 AMO。** 它只表示“不在 IR 层展开”。反例是 `atomicrmw nand i32`：即使 `+a` 也没有 `AMONAND`，只能退回 LR/SC：`lr.w.aqrl / and / not / sc.w.rl / bnez`。

`AtomicExpansionKind` 完整枚举在 `llvm/include/llvm/CodeGen/TargetLowering.h:275`；RISC-V 只用 `None`、`CmpXChg`、`MaskedIntrinsic`。

---

## 20.3 `shouldInsertFencesForAtomic` / `emitLeadingFence` / `emitTrailingFence`

这三个 hook 属于 `AtomicExpandPass`，决定是否把带 ordering 的 atomic load/store 改写成“fence + 普通 load/store”。`shouldInsertFencesForAtomic`（`:31044`）的核心：

```cpp
if (Subtarget.hasStdExtZalasr()) {
  if (Subtarget.hasStdExtZtso()) { /* seq_cst load/store 返回 false，其余 load/store true */ }
  return false;                 // Zalasr：顺序交给 lw.aq / sw.rl
}
return isa<LoadInst>(I) || isa<StoreInst>(I);   // 默认：仅 load/store
```

三个必须记住的返回：默认（无 Zalasr）load/store 为 `true`、RMW/cmpxchg 为 `false`；有 Zalasr 且非 Ztso 时 load/store 也 `false`，Zalasr+Ztso 只对 seq_cst 返回 `false`；**cmpxchg 永远不插 fence**，顺序在 `.aqrl` 上，唯一额外 fence 由 `RISCVZacasABIFix` 补。

`emitLeadingFence`（`:30110`）/`emitTrailingFence`（`:30126`）负责位置：leading 对 seq_cst load 发完整 fence、对 release-or-stronger store 发 release fence；trailing 对 acquire-or-stronger load 发 acquire fence、对 seq_cst store 是否再发完整 fence 由 `enableTrailingSeqCstFence()` 决定（`:30137`，即 `+no-trailing-seq-cst-fence`）。实测（`-march=rv64gc`）：

| IR 操作 | 生成序列 |
|---|---|
| `load relaxed` | `lw` |
| `load acquire` | `lw` + `fence r, rw` |
| `load seq_cst` | `fence rw, rw` + `lw` + `fence r, rw` |
| `store release` | `fence rw, w` + `sw` |
| `store seq_cst` | `fence rw, w` + `sw` + `fence rw, rw` |
| `store seq_cst` + `-no-trailing-seq-cst-fence` | `fence rw, w` + `sw` |

纠正一个常见说法：**`+a` 下 AMO 的 ordering 不需要 fence**。AMO 自带 `.aq/.rl`；需要 fence 的是 atomic load/store（base A 没有带标注的普通访存）与 LR/SC 循环（顺序放在 `lr.w.aqrl`/`sc.w.rl` 上）。

---

## 20.4 `shouldExpandAtomicRMWInIR` / `shouldExpandAtomicCmpXchgInIR`

```cpp
// RISCVISelLowering.cpp:30144（节选）
if (AI->isFloatingPointOperation() || UIncWrap/UDecWrap/USubCond/USubSat)
  return AtomicExpansionKind::CmpXChg;      // FP 不能进 lr/sc 窗口
if (Subtarget.hasForcedAtomics()) return AtomicExpansionKind::None;  // __sync_*
if (AI->getOperation() == AtomicRMWInst::Nand) {
  if (Zacas && (Size >= 32 || Zabha)) return CmpXChg;
  if (Size < 32) return MaskedIntrinsic;
}
if (Size < 32 && !Subtarget.hasStdExtZabha()) return MaskedIntrinsic;
return AtomicExpansionKind::None;
```

- FP / saturating / wrap → `CmpXChg`：先变 `cmpxchg` 循环，再由后端选 LR/SC 或 `amocas`。
- `+forced-atomics` → `None`，配合 `setOperationAction(..., LibCall)`（`:1883`）强制 `__sync_*`。
- 8/16 位且无 Zabha → `MaskedIntrinsic`：用 32 位 LR/SC 做掩码读改写（`:30175`）。
- 其余 → `None`：交给 ISel 的 AMO 或伪指令。

`cmpxchg` 规则更短（`:30258`）：`+forced-atomics` → `None`；`!(Zabha && Zacas) && (Size==8||16)` → `MaskedIntrinsic`；否则 `None`。即 **8/16 位 `amocas.b/h` 必须两个扩展都有**。没有 Zalrsc 也没有 forced-atomics 时 `setMaxAtomicSizeInBitsSupported(0)`（`:902`），前端与 `AtomicExpand` 会把原子操作降成 libcall；`clang` 侧是 `MaxAtomicInlineWidth`（`clang/lib/Basic/Targets/RISCV.h:193`，RV64 在 `:224`）。

---

## 20.5 `RISCVExpandPseudoAtomics` 深入

### 为什么必须最后展开

文件头（`RISCVExpandPseudoAtomics.cpp:10-13`）：

```cpp
// This pass is run at the last possible moment, avoiding the possibility for
// other passes to break the requirements for forward progress in the LR/SC block.
```

ISA 规则（`RISCVFeatures.td:2073-2086`）：**constrained LR/SC loop 在 LR 与 SC 之间只允许 base I 指令**，不允许 load/store、backward jump、taken backward branch、`jalr`、`fence`、SYSTEM。寄存器分配若插入 spill/reload，或调度器插入 `fence`，循环就变成 unconstrained，SC 可能永远不成功。pass 名 `riscv-expand-pseudo-atomics`（`:906`），在 `addPreEmitPass2` 最后（`RISCVTargetMachine.cpp:533`）。

### `expandAtomicBinOp` 的循环形状

源码注释就是最终汇编（`:324-328`）：

```text
.loop:  lr.[w|d] dest, (addr)
        binop scratch, dest, val
        sc.[w|d] scratch, scratch, (addr)
        bnez scratch, loop
```

实测（`-mattr=-a,+zalrsc`，seq_cst）：

```asm
.LBB0_1:
	lr.w.aqrl	a2, (a0)
	add	a3, a2, a1
	sc.w.rl	a3, a3, (a0)
	bnez	a3, .LBB0_1
	mv	a0, a2
```
`sc.w` 成功写 0、失败写非 0，`bnez` 就是失败重试；`a2` 保存旧值作为返回值。`PseudoAMO` 的 `@earlyclobber`（`RISCVInstrInfoA.td:254-256`）保证 `$res`/`$scratch` 不与输入重叠。

### `.aq`/`.rl` 怎么选

`getLRForRMW32`（`:208`）/`getSCForRMW32`（`:230`），64 位在 `:252`/`:274`：

| `AtomicOrdering` | LR32 | SC32 | LR64 | SC64 |
|---|---|---|---|---|
| `Monotonic` | `LR_W` | `SC_W` | `LR_D` | `SC_D` |
| `Acquire` | `LR_W_AQ` | `SC_W` | `LR_D_AQ` | `SC_D` |
| `Release` | `LR_W` | `SC_W_RL` | `LR_D` | `SC_D_RL` |
| `AcquireRelease` | `LR_W_AQ` | `SC_W_RL` | `LR_D_AQ` | `SC_D_RL` |
| `SequentiallyConsistent` | `LR_W_AQRL` | `SC_W_RL` | `LR_D_AQRL` | `SC_D_RL` |

acquire 标在 LR，release 标在 SC；**seq_cst 是 `LR.aqrl` + `SC.rl`**，不是两边都 `aqrl`。`+ztso` 会删掉 acquire/acq_rel 的 `.aq`（`:215`、`:221`、`:259`、`:265`）。`AtomicOrdering` 的整数 2/4/5/6/7 被写进伪指令立即数（`PseudoAMOPat`，`RISCVInstrInfoA.td:294-308`），展开时 `static_cast` 读回；改枚举顺序会同时破坏 pattern 与展开。

### min/max 与 cmpxchg 的差别

`min/max` 默认**先比较，只有需要更新才进 SC 窗口**（`:533`）：

```asm
.LBB0_1: lr.w.aqrl a1, (a0)
         mv  a3, a1
         bge a3, a2, .LBB0_3
         mv  a3, a2
.LBB0_3: sc.w.rl a3, a3, (a0)
         bnez a3, .LBB0_1
```

若 `+permissive-zalrsc`（`RISCVFeatures.td:2087`）并且有 Zbb，`expandAtomicMinMaxOp` 改走 `expandAtomicBinOp`，直接 `max a3, a1, a2`（`:687-690`）。这是**对微架构 forward-progress 承诺的信任**，默认路径只用 base I 指令所以更保守。

`expandAtomicCmpXchg`（`:802`）生成 `LoopHead`/`LoopTail`/`Done` 三块：`lr.w.aqrl` → `bne`（失败直接结束，不写内存）→ `sc.w.rl` → `bnez` 重试。`bne` 在 SC 之前，所以返回值天然是旧值。`tryToFoldBNEOnCmpXchgResult`（`:748`）把 cmpxchg 后那条 `bne`（masked 是 `and`+`bne`）折进循环头，前提是该 BNE 是结果唯一使用者且后面无其他指令。masked 版本多一层 `insertMaskedMerge`（`:402`）。

`cmpxchg` 的 `weak` 标志在这里没有分支：SC 失败仍然重试，所以 RISC-V 上 weak 实际和 strong 一样强（20.10 踩雷 4）。8/16 位无 Zabha 时走 `doMaskedAtomicBinOpExpansion`（`:423`）：`andi addr,-4`、`slli` 算位移、`lr.w`、运算、`xor/and/xor` 掩码、`sc.w`——借 32 位 LR/SC 保护邻居字节。

---

## 20.6 Zacas / Zabha / Zawrs / Zalasr

| 扩展 | 指令 | 定义 | 编译器生成 |
|---|---|---|---|
| Zacas | `amocas.w` / `amocas.d` | `RISCVInstrInfoZa.td:59` / `:67` | 会（i32 / RV64 i64） |
| Zacas | `amocas.q` | `:68`（RV64） | 不会，仅汇编 |
| Zabha | `amoadd.b/h` | `:146`、`:165` | 会（8/16 位 RMW） |
| Zabha+Zacas | `amocas.b/h` | `:185-186` | 会（8/16 位 cmpxchg） |
| Zawrs | `wrs.nto`/`wrs.sto` | `:135-136` | 不会，仅汇编 |
| Zalasr | `lw.aq`/`sw.rl` | `RISCVInstrInfoZalasr.td:48-60` | 会 |

`AMOCASPat`（`:71`）只有 i32→`amocas.w`、i64→RV64 的 `amocas.d`（`:119-120`）。`amocas.q` 与 RV32 的 `amocas.d` 没有 pattern；`RISCVUsage.md:326-328` 给的理由是 **ABI 兼容性**，手写汇编可以，编译器不生成。

**`RISCVZacasABIFix` 不是“老核替换 pass”。** 它只做一件事（`RISCVZacasABIFix.cpp:64-71`）：

```cpp
if (I.getFailureOrdering() != AtomicOrdering::SequentiallyConsistent) return false;
Builder.CreateFence(AtomicOrdering::SequentiallyConsistent);
```

即 **只在 `+zacas` 且 cmpxchg 的 failure ordering 是 `seq_cst` 时，在 cmpxchg 前插一条 `fence seq_cst`**。提交 `0ee10e946660` 说明：psABI 的 A6C/A6S mapping 要求这种 cmpxchg 额外加 fence；`AtomicExpandPass` 的 hook 假设“插 fence 后可以把 ordering 从指令删掉”，这里不能删，所以单独写 IR pass。顺序在 `RISCVTargetMachine.cpp:401-402`：先 `AtomicExpand`，再 `ZacasABIFix`。实测：

```asm
	fence	rw, rw
	amocas.d.aqrl	a1, a2, (a0)
```
这条 fence 不是冗余，删掉就是 ABI break。`RISCVTargetStreamer.cpp:81-89` 据此发 `Tag_RISCV_atomic_abi`：默认 `+a` → A6S（`Atomic ABI is 2`）；`+no-trailing-seq-cst-fence` → A6C（1）；`+zalasr` → A7（3）。默认不输出，要 `-riscv-abi-attributes`（`RISCVMCOptions.td:34`）。`RISCVUsage.md:330-334` 说明默认 A6S，且因老 binutils 会崩，attribute 默认不写。**A6S 与 A6C 是不同 mapping**；混用会改变 seq_cst store 的保证。

Zawrs 是“在 reservation set 上等待”的省电 spin-wait，本树只有汇编支持（无 intrinsic、无 pattern）；`wrs.nto` 编码 `0x00d00073`，`wrs.sto` 编码 `0x01d00073`（`llvm-mc -show-encoding` 实测）。Zalasr 让 `shouldInsertFencesForAtomic` 返回 `false`，ISel 用 `LW_AQ`/`SW_RL`（`:78-110`），ABI tag 推到 A7：

```asm
# -mattr=+a,+zalasr
acq:  lw.aq  a0, (a0)
rel:  sw.rl  a1, (a0)
```

---

## 20.7 `fence` 与 `fence.i`

`FENCE` 编码在 `RISCVInstrInfo.td:939`：`imm12 = {0b0000, pred, succ}`，pred/succ 各 4 位，`i/o/r/w = 0b1000/0100/0010/0001`（解析器在 `RISCVAsmParser.cpp:2851`）。因此 `fence rw, rw` → `imm12=0x033`，编码 `0x0330000f`；`fence.tso`（`:950`）→ `imm12=0x833`，编码 `0x8330000f`；`fence.i`（`:956`）→ `funct3=001`、`imm12=0`，编码 `0x0000100f`。

`fence.tso` 不需要 `+ztso` 也能汇编（`llvm/test/MC/RISCV/rvi-valid.s:328`），`+ztso` 只是让它成为必须实现并改变 lowering 策略。四种 thread fence 的实测映射（`RISCVInstrInfo.td:2152-2158`）：

```asm
f_acq:  fence   r, rw       # __ATOMIC_ACQUIRE
f_rel:  fence   rw, w       # __ATOMIC_RELEASE
f_ar:   fence.tso           # __ATOMIC_ACQ_REL
f_sc:   fence   rw, rw      # __ATOMIC_SEQ_CST
```

`LowerATOMIC_FENCE`（`RISCVISelLowering.cpp:7816`）还处理两种退化：`syncscope("singlethread")` → `MEMBARRIER`（空操作，只防编译器重排）；`+ztso` 下只有 seq_cst + system scope 需要指令。

`fence.i` 不是 `fence`：前者管取指，让本 hart 之后的 fetch 看到之前的 store；后者管数据可见顺序。编译器**不会**为 `__builtin___clear_cache` 直接发 `fence.i`，而是发 libcall：

```asm
# riscv64-linux-gnu
	li	a2, 0
	call	__riscv_flush_icache
# riscv64-unknown-elf
	call	__clear_cache
```

原因：`fence.i` 只同步当前 hart；多 hart 要运行时/内核在所有 hart 上做 I-cache 同步。Linux 的 syscall 与裸机的 `__clear_cache` 承担这件事，这也是 `:910` 只在 Linux 上自定义 `llvm.clear_cache` 的原因。

---

## 20.8 源码落点

| 主题 | 文件:行 | 读什么 |
|---|---|---|
| IR 展开策略 | `RISCVISelLowering.cpp:30144` | `shouldExpandAtomicRMWInIR` |
| cmpxchg 策略 | 同上 `:30258` | 8/16 位与 Zabha+Zacas |
| fence 插入 | 同上 `:31044`、`:30110`、`:30126` | 默认/Zalasr/Ztso |
| fence lowering | 同上 `:7816` | singlethread 与 Ztso 退化 |
| 原子宽度 | 同上 `:899-902` | `setMaxAtomicSizeInBitsSupported(0)` |
| forced atomics | 同上 `:1883` | `__sync_*` LibCall |
| masked RMW | 同上 `:30198` | intrinsic 与符号扩展 |
| LR/SC 展开 | `RISCVExpandPseudoAtomics.cpp:103`、`:313`、`:486` | `expandMI`、`expandAtomicBinOp` |
| min/max | 同上 `:533`、`:687` | 分支循环与 permissive 快路径 |
| cmpxchg | 同上 `:802` | 三块 CFG 与 BNE 折叠 |
| `.aq/.rl` | 同上 `:208`、`:230`、`:252`、`:274` | 四张 switch |
| pass 名/时机 | 同上 `:906`；`RISCVTargetMachine.cpp:533` | `riscv-expand-pseudo-atomics` |
| AMO/LR/SC | `RISCVInstrInfoA.td:27-121`、`:203`、`:294-469` | `.aq/.rl` 变体与 pattern |
| Zacas/Zabha/Zawrs | `RISCVInstrInfoZa.td:43-186` | 指令与 pattern |
| Zalasr | `RISCVInstrInfoZalasr.td:19-110` | `LAQ_r`、`SRL_r` |
| FENCE | `RISCVInstrInfo.td:939-962`、`:2146-2158` | 位域与四条 pattern |
| ABI | `RISCVZacasABIFix.cpp:64`；`RISCVTargetStreamer.cpp:81` | fence 与 A6S/A6C/A7 |
| 扩展 | `RISCVFeatures.td:233-303`、`:2047-2090` | 八个 Feature |
| 文档 | `RISCVUsage.md:168-174`、`:326-334` | 支持状态与 atomics ABI |

测试入口：`atomic-rmw.ll`、`atomic-rmw-minmax.ll`、`atomic-cmpxchg.ll`、`atomic-cmpxchg-branch-on-result.ll`、`atomic-fence.ll`、`atomic-load-store.ll`、`expand-atomic-pseudo.mir`、`riscv-zacas-abi-fix.ll`；MC 侧 `rvzacas-valid.s`、`rvzabha-zacas-valid.s`、`rvzalrsc-valid.s`、`Ztso.s`、`Zawrs-valid.s`。

---

## 20.9 可跑实验

全部在本树构建实测；`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

### 实验 1：三种 march，三种命运

```bash
cat > /tmp/ch20-rmw.c <<'EOF'
int add32(int *p, int v) { return __atomic_fetch_add(p, v, __ATOMIC_SEQ_CST); }
EOF
for m in rv64gc rv64imfdc_zicsr_zifencei_zalrsc rv64imfdc_zicsr_zifencei; do
  echo "== $m"; $T/bin/clang --target=riscv64-linux-gnu -march=$m -mabi=lp64d \
    -Wno-atomic-alignment -O2 -S -o - /tmp/ch20-rmw.c | sed -n '/^add32:/,/^\.Lfunc_end/p'
done
```

```asm
# rv64gc（g 已含 a）
	amoadd.w.aqrl	a0, a1, (a0)
	ret
# rv64imfdc_zicsr_zifencei_zalrsc（无 a，有 zalrsc）
	lr.w.aqrl	a2, (a0)
	add	a3, a2, a1
	sc.w.rl	a3, a3, (a0)
	bnez	a3, .LBB0_1
	mv	a0, a2
	ret
# rv64imfdc_zicsr_zifencei（无 a，无 zalrsc）
	li	a2, 5
	call	__atomic_fetch_add_4
```

`rv64gc_zalrsc` 仍是 AMO，因为 `g` 已含 `a`；强制 LR/SC 要去掉 `a`，或用 `llc -mattr=+a,-zaamo`。`5` 是 GCC 的 `__ATOMIC_SEQ_CST` 值。

### 实验 2：停在 `riscv-expand-pseudo-atomics`

```bash
cat > /tmp/ch20-rmw.ll <<'EOF'
define i32 @add32(ptr %p, i32 %v) { %r = atomicrmw add ptr %p, i32 %v seq_cst
  ret i32 %r }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=-a,+zalrsc \
  -stop-after=riscv-expand-pseudo-atomics /tmp/ch20-rmw.ll -o -
```

```yaml
bb.1:
  $x12 = LR_W_AQRL $x10
  $x13 = ADD $x12, $x11
  $x13 = SC_W_RL $x13, $x10
  BNE $x13, $x0, %bb.1
```

`PseudoAtomicLoadAdd32` 已消失；`LR_W_AQRL`/`SC_W_RL` 就是 20.5 表里的选择结果。

### 实验 3：`llvm-mc -show-encoding`

```bash
printf 'amoadd.w.aqrl a0, a1, (a2)\nlr.w.aq a0, (a1)\nsc.d.rl a0, a1, (a2)\nfence rw, rw\nfence.tso\namocas.d.aqrl a0, a1, (a2)\nfence.i\n' \
 | $T/bin/llvm-mc -triple=riscv64 -mattr=+a,+zacas -show-encoding
```

```text
amoadd.w.aqrl  a0, a1, (a2)   # [0x2f,0x25,0xb6,0x06]
lr.w.aq        a0, (a1)       # [0x2f,0xa5,0x05,0x14]
sc.d.rl        a0, a1, (a2)   # [0x2f,0x35,0xb6,0x1a]
fence          rw, rw         # [0x0f,0x00,0x30,0x03]
fence.tso                     # [0x0f,0x00,0x30,0x83]
amocas.d.aqrl  a0, a1, (a2)   # [0x2f,0x35,0xb6,0x2e]
fence.i                       # [0x0f,0x10,0x00,0x00]
```

`fence rw,rw` 的 `0x0330000f` 与 20.7 的位域推导一致。

### 实验 4：seq_cst 与 relaxed 的差别

```bash
cat > /tmp/ch20-fence.c <<'EOF'
int rel(int *p) { return __atomic_load_n(p, __ATOMIC_RELAXED); }
int acq(int *p) { return __atomic_load_n(p, __ATOMIC_ACQUIRE); }
void scst(int *p, int v) { __atomic_store_n(p, v, __ATOMIC_SEQ_CST); }
void relst(int *p, int v) { __atomic_store_n(p, v, __ATOMIC_RELEASE); }
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O2 -S -o - /tmp/ch20-fence.c
```

```asm
rel:    lw      a0, 0(a0)
acq:    lw      a0, 0(a0)
        fence   r, rw
scst:   fence   rw, w
        sw      a1, 0(a0)
        fence   rw, rw
relst:  fence   rw, w
        sw      a1, 0(a0)
```

`scst` 比 `relst` 多一条 trailing 完整 fence；加 `+no-trailing-seq-cst-fence` 后它消失，ABI 从 A6S 变 A6C。

### 实验 5：8/16 位 RMW，有无 Zabha

```bash
cat > /tmp/ch20-b.c <<'EOF'
unsigned char add8(unsigned char *p, unsigned char v) { return __atomic_fetch_add(p, v, __ATOMIC_SEQ_CST); }
unsigned short add16(unsigned short *p, unsigned short v) { return __atomic_fetch_add(p, v, __ATOMIC_SEQ_CST); }
EOF
for m in rv64gc rv64gc_zabha; do
  $T/bin/clang --target=riscv64-linux-gnu -march=$m -mabi=lp64d -O2 -S -o - /tmp/ch20-b.c
done
```

```asm
# 无 Zabha（masked LR/SC，节选）
andi	a2, a0, -4
slli	a0, a0, 3
lr.w.aqrl a4, (a2)
add	a5, a4, a1
xor	a5, a5, a4
and	a5, a5, a3
xor	a5, a5, a4
sc.w.rl	a5, a5, (a2)
# 有 Zabha
add8:   amoadd.b.aqrl  a0, a1, (a0)
        zext.b         a0, a0
add16:  amoadd.h.aqrl  a0, a1, (a0)
        slli           a0, a0, 48
```

`amoadd.b/h` 返回值按符号扩展到 XLEN，无符号结果还要再清高位。

### 实验 6：QEMU 实跑 LR/SC 与 AMO

```bash
cat > /tmp/ch20-qemu.c <<'EOF'
static char out[64];
static long wr(long fd, const char *b, unsigned long n) {
  register long a0 asm("a0") = fd; register const char *a1 asm("a1") = b;
  register unsigned long a2 asm("a2") = n; register long a7 asm("a7") = 64;
  asm volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory"); return a0; }
static void ex(long c) { register long a0 asm("a0") = c; register long a7 asm("a7") = 93;
  asm volatile("ecall" : : "r"(a0), "r"(a7) : "memory"); __builtin_unreachable(); }
static char *num(char *e, unsigned long v) { *--e = '\n';
  do { *--e = (char)('0' + v % 10); v /= 10; } while (v); return e; }
static volatile int c;
void _start(void) { long s = 0;
  for (int i = 0; i < 1000; ++i) s += __atomic_fetch_add(&c, 1, __ATOMIC_SEQ_CST);
  char *p = num(out + 64, (unsigned long)c); p = num(p, (unsigned long)s);
  wr(1, p, (unsigned long)(out + 64 - p)); ex(0); }
EOF
for cfg in "amo:rv64gc" "lrsc:rv64imfdc_zicsr_zifencei_zalrsc"; do
  n=${cfg%%:*}; m=${cfg#*:}
  $T/bin/clang --target=riscv64-linux-gnu -march=$m -mabi=lp64 -O2 \
    -ffreestanding -fno-builtin -nostdlib -static -fuse-ld=lld \
    --ld-path=$T/bin/ld.lld -Wl,-e,_start -o /tmp/ch20-$n /tmp/ch20-qemu.c
  /usr/bin/qemu-riscv64-static /tmp/ch20-$n
done
```

真实输出（两版本一致，第一行 0+1+...+999，第二行计数器）：

```text
499500
1000
```

**只验证了单线程语义与展开形状**：无竞争时 SC 第一次就成功，没有覆盖重试路径。

### 实验 7：Zacas cmpxchg 与 `+zaamo,+zalrsc` 崩溃

```bash
cat > /tmp/ch20-cas.ll <<'EOF'
define i32 @cas32(ptr %p, i32 %c, i32 %n) {
  %r = cmpxchg ptr %p, i32 %c, i32 %n seq_cst seq_cst
  %v = extractvalue { i32, i1 } %r, 0
  ret i32 %v }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+a,+zacas /tmp/ch20-cas.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+zaamo,+zalrsc /tmp/ch20-rmw.ll -o -
```

```asm
	fence	rw, rw
	amocas.w.aqrl	a1, a2, (a0)
	mv	a0, a1
	ret
```

第二个命令真实报错 `Cannot select: ... AtomicLoadAdd ...`。`-march` 路径不会，因为 `updateCombination()` 会把 `zaamo`+`zalrsc` 合成 `a`；`llc -mattr` 不会。

### 未实测项

- **多线程竞争下的 LR/SC 重试**：无可用 RISC-V pthread/libc 环境；QEMU user-mode 6.2 的测试程序是单线程，SC 一次成功。
- **真机内存模型 / forward progress**：RVWMO litmus 需要多 hart 与可观测 store buffer，本机只有 user-mode QEMU。
- **Zabha / Zacas 执行**：QEMU 6.2 不支持；实验 5、7 只到汇编/编码层。
- **Zawrs 等待语义**：依赖 reservation set 与中断，QEMU 6.2 不支持，误用会挂起。
- **`fence.i` 的真实 I-cache 行为**：user-mode QEMU 不模拟分立 I-cache，只验证了发 libcall 的形状。

---
## 20.10 踩雷集锦

1. **`+a` 与 `zaamo`/`zalrsc` 的隐含关系。** `-march=rv64gca` 报 `duplicated standard user-level extension 'a'`（`g` 已含 `a`）；`rv64gc_zalrsc` 仍是 AMO；`+a,-zaamo` 会连 `A` 一起关掉并退回 LR/SC；`+a,-zalrsc` 直接掉 libcall；在 `llc -mattr` 写 `+zaamo,+zalrsc` 而没有 `+a` 则 `Cannot select`。前端 `-march` 会合成 `a`，`llc`/MIR 不会。
2. **`.aq`/`.rl` 加错位置。** acquire 在 LR，release 在 SC；seq_cst 是 `LR.aqrl` + `SC.rl`。给 LR 加 `.rl` 不产生 release，给 SC 加 `.aq` 不补 acquire。
3. **`fence` 放错位置。** seq_cst store 是 `fence rw,w` 在前、`sw` 在中、`fence rw,rw` 在后；把 trailing fence 挪到 store 之前，就失去与后续操作的顺序。关掉 trailing fence（`+no-trailing-seq-cst-fence`）还会把 ABI 从 A6S 变成 A6C。
4. **以为 weak cmpxchg 更便宜。** `expandAtomicCmpXchg` 无 weak 分支，SC 失败仍 `bnez` 重试；本树实测 strong/weak 汇编相同。weak 只影响 IR“允许失败”。
5. **把 `RISCVZacasABIFix` 当老核兼容 pass。** 它不替换 `amocas`，只在 `+zacas` 且 failure ordering 为 `seq_cst` 时补前导 `fence seq_cst`；删掉就是 ABI break。
6. **手写 `amocas.q`（RV64）或 `amocas.d`（RV32）。** 汇编器接受，编译器不生成；这两个宽度不在当前 atomics ABI 保证内（`RISCVUsage.md:326-328`）。
7. **`-mno-a` 下忘了 libcall 实现。** `__atomic_fetch_add_4` 来自 `compiler-rt/lib/builtins/atomic.c`（宏在 `:341`），裸机链接要带 compiler-rt 或 libatomic；加 `+forced-atomics` 会改发 `__sync_*`。
8. **把 `fence.i` 当数据屏障，或在 LR/SC 窗口里塞函数调用。** `fence.i` 同步取指，不参与 `.aq/.rl`；constrained LR/SC 只允许 base I 指令，`jalr`、load/store、`fence` 都会让它变 unconstrained。`atomicrmw fadd` 的 `__addsf3` 被 LLVM 放在两层循环外层，正是为了不破坏规则。

---

## 20.11 本章重点整理

- 原子 lowering 是**政策**：AMO / LR/SC / libcall 三选一，顺序由 `.aq/.rl` 或 `fence` 表达，宽度与 ABI 再叠加一层。
- `+a` = `Zaamo` + `Zalrsc`；前端 `updateCombination()` 补 `a`，`llc -mattr` 不补。
- `shouldExpandAtomicRMWInIR` 只返回 `None`/`CmpXChg`/`MaskedIntrinsic`；`None` 之后还有 ISel 的 AMO 与伪指令两条路。
- `shouldInsertFencesForAtomic` 默认只对 load/store 返回 true；RMW/cmpxchg 的顺序在 `.aq/.rl` 上。
- seq_cst store 的 trailing fence 由 `enableTrailingSeqCstFence()` 控制，关掉它把 ABI 从 A6S 变 A6C。
- `RISCVExpandPseudoAtomics` 最后运行是为了保住 constrained LR/SC 的 forward progress；`lr → 运算 → sc → bnez` 是固定形状。
- min/max 默认“先比较再 SC”；`+permissive-zalrsc` + Zbb 才允许直接用 `min/max`。
- `RISCVZacasABIFix` 是 ABI fence pass，不是指令替换 pass。
- `fence.i` 管取指，`fence` 管数据；`__builtin___clear_cache` 发 libcall。

---

## 20.12 自我检核

- [ ] 能画出 `atomicrmw add i32` 在 `+a`、`+zalrsc,-zaamo`、无 a 三种配置下的完整路径与每个决定函数
- [ ] 能解释为什么 `shouldExpandAtomicRMWInIR` 返回 `None` 仍可能生成 LR/SC 或 libcall
- [ ] 能默写 `getLRForRMW32`/`getSCForRMW32` 在五种 ordering 下的 opcode
- [ ] 能解释 seq_cst 为什么是 `LR.aqrl` + `SC.rl`，而不是两边都 `aqrl`
- [ ] 能说出 `shouldInsertFencesForAtomic` 对 load、store、cmpxchg 分别返回什么，以及 Zalasr 的影响
- [ ] 能解释 trailing seq_cst fence 与 A6S/A6C 的关系，并知道 `Tag_RISCV_atomic_abi` 怎么打开
- [ ] 能解释 `RISCVZacasABIFix` 的触发条件，以及它为什么不是“老核兼容”
- [ ] 能说出 `fence rw,rw`、`fence.tso`、`fence.i` 的编码与用途差异
- [ ] 能解释 LR/SC 循环里为什么不能出现函数调用、load/store 或 `fence`

---

## 20.13 延伸阅读

- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVExpandPseudoAtomics.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelLowering.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoA.td`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZa.td`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZalasr.td`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVZacasABIFix.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVFeatures.td`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/MCTargetDesc/RISCVTargetStreamer.cpp`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/RISCVUsage.md`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/atomic-rmw.ll`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/atomic-cmpxchg.ll`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/expand-atomic-pseudo.mir`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/rvzacas-valid.s`
- `/home/ubuntu/llvm/trees/ct-instr-trunk/compiler-rt/lib/builtins/atomic.c`

→ 下一章：[Ch 21 — 调度模型与 llvm-mca](./21-scheduling-models-and-mca.md)
