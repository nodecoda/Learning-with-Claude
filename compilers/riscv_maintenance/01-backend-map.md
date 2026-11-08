# Ch 01 — 后端全景：一条 RISC-V 指令经过的十二个站位

> **目标**：建立一张“从 IR 到 ELF”的地图。之后每章只深入一个站位，但你始终知道它在整条链的哪里；看到一个 bug 症状时，能先判断该去哪个站位找，而不是从 31,346 行的 `RISCVISelLowering.cpp` 第一行开始读。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM `24.0.0git`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`。

---

## 1.1 为什么维护者先要全景

一个 RISC-V 后端的 issue，通常以“汇编不对”的形式出现：

```asm
# 期望
mul  a0, a0, a1
# 实际
mulw a0, a0, a1
```

如果你不知道 `mulw` 是在 `riscv-opt-w-instrs` 里被改成 `mul` 的，就会去 `RISCVISelDAGToDAG.cpp` 里找一个根本不存在的 pattern，或者怀疑 TableGen 的 `Pat<>` 写错。实际上选指阶段确实生成了 `MULW`，是后面的机器优化发现高 32 位没人用，才把它降成 `MUL`。

全景图的作用就是：**把“症状”映射到“站位”，再把“站位”映射到“文件 + 观察命令”**。

本章会用同一个小函数，在五个不同停点打印真实 MIR，让你亲眼看到指令形态怎么变化。这比背十二个文件名有用得多。

---

## 1.2 先建立直觉：`mul_add` 的旅程

```llvm
define signext i32 @mul_add(i32 %a, i32 %b, i32 %c) {
  %m = mul i32 %a, %b
  %r = add i32 %m, %c
  ret i32 %r
}
```

用 `-mtriple=riscv64 -mattr=+m -O2` 编译。下面是一条真实的时间线（每一步都是本机实测输出）：

```text
LLVM IR
  %m = mul i32 %a, %b
  %r = add i32 %m, %c
        │
        │  ① IR passes / ② 合法化 / ③ DAG 构建 / ④ 自定义 lowering
        ▼
SelectionDAG（i32 在 RV64 上要合法化成 i64 操作 + 截断）
        │
        │  ⑤ 指令选择：TableGen pattern + RISCVISelDAGToDAG::Select
        ▼
finalize-isel 后的 MIR
  %3:gpr = MULW %0, %1
  %4:gpr = ADDW %3, %2
  PseudoRET implicit $x10
        │
        │  ⑥ 机器 SSA 优化：riscv-opt-w-instrs
        ▼
  %3:gpr = MUL %0, %1        ← 高 32 位没人用，MULW 降成 MUL
  %4:gpr = ADDW %3, %2
        │
        │  ⑦ PreRA 伪指令 / ⑧ 寄存器分配 / ⑨ PostRA / ⑩ 调度
        ▼
prolog-epilog 后的 MIR
  renamable $x10 = MUL killed renamable $x10, killed renamable $x11
  renamable $x10 = ADDW killed renamable $x10, killed renamable $x12
  PseudoRET implicit $x10
        │
        │  ⑪ MC 编码：AsmPrinter → MCInst → MCCodeEmitter
        ▼
汇编
  mul  a0, a0, a1
  addw a0, a0, a2
  ret
        │
        │  ⑫ 目标文件 / ELF
        ▼
ELF 文本节 + 重定位 + e_flags
```

注意 `MULW → MUL` 和 `PseudoRET → ret` 都不是“选指”做的。选指只负责把 DAG 变成机器指令；后面还有一长串 pass 会改你刚生成的指令。

---

## 1.3 十二个站位

| # | 站位 | 输入 → 输出 | 主要文件 | 本课章节 |
|---:|---|---|---|---|
| 1 | IR passes | LLVM IR → 更规范的 IR | `RISCVCodeGenPrepare.cpp`、`RISCVGatherScatterLowering.cpp`、`RISCVInterleavedAccess.cpp` | Ch 03、17 |
| 2 | 合法化 | 目标无关类型/操作 → 目标合法类型/操作 | `RISCVISelLowering.cpp` 构造函数（67 行起） | Ch 13 |
| 3 | DAG 构建与自定义 lowering | IR → `SelectionDAG` → 目标节点 | `LowerOperation`（8374）、`LowerINTRINSIC_*`（12944 起） | Ch 13–15 |
| 4 | DAG combine | 目标 DAG → 更便宜的 DAG | `PerformDAGCombine`（25386）、`computeKnownBitsForTargetNode`（27318） | Ch 16 |
| 5 | 指令选择 | DAG → 机器指令（虚拟寄存器） | `RISCVInstrInfo*.td`、`RISCVISelDAGToDAG.cpp:Select`（1178） | Ch 04、14 |
| 6 | 机器 SSA 优化 | 机器指令 → 更少/更便宜的机器指令 | `RISCVOptWInstrs.cpp`、`RISCVVLOptimizer.cpp`、`RISCVVectorPeephole.cpp`、`RISCVFoldMemOffset.cpp` | Ch 17、19 |
| 7 | PreRA 伪指令与地址折叠 | 伪指令 → 真实指令；地址 → 基址+偏移 | `RISCVExpandPseudoPreRA.cpp`、`RISCVMergeBaseOffset.cpp`、`RISCVInsertReadWriteCSR.cpp`、`RISCVInsertWriteVXRM.cpp` | Ch 18、19、20 |
| 8 | 寄存器分配 | 虚拟寄存器 → 物理寄存器 | `RISCVRegisterInfo.cpp`、`RISCVInsertVSETVLI.cpp`、RVV regalloc | Ch 05、17、18 |
| 9 | PostRA / PreEmit | 物理寄存器机器指令 → 可发射指令 | `RISCVExpandPseudoPostRA.cpp`、`RISCVExpandPseudoPreEmit.cpp`、`RISCVExpandPseudoAtomics.cpp`、`RISCVMakeCompressible.cpp`、`RISCVPushPopOptimizer.cpp` | Ch 18–20 |
| 10 | 调度 | 机器指令顺序 → 调度后的顺序 | `RISCVSchedule*.td`、`RISCVMachineScheduler.cpp`、各 `RISCVSched*.td` | Ch 21 |
| 11 | MC 编码 | 机器指令 → `MCInst` → 字节 | `RISCVAsmPrinter.cpp`、`MCTargetDesc/RISCVMCCodeEmitter.cpp`、`RISCVAsmBackend.cpp` | Ch 06、07、22 |
| 12 | 目标文件 / ELF | 字节 + 重定位 + 属性 → `.o` | `MCTargetDesc/RISCVELFObjectWriter.cpp`、`RISCVELFStreamer.cpp`、`lld/ELF/Arch/RISCV.cpp` | Ch 11、12、22 |

这张表不是按源码目录分的，而是按**数据形态**分的。维护时最重要的是记住“输入输出是什么形态”：

- 到第 5 站为止，指令还是“目标相关的 DAG 节点”；
- 第 5 站之后，指令是 `MachineInstr`，可以 `-run-pass`、可以手写 MIR；
- 第 11 站之后，指令是 `MCInst`，可以用 `llvm-mc -show-inst` 看；
- 第 12 站之后，只剩字节和重定位，要用 `llvm-readobj` / `llvm-objdump` 看。

---

## 1.4 真实 pass 顺序：`-debug-pass=Structure`

不要背文档里的“大概顺序”，直接让 `llc` 打印。本机是 assertions 构建，可以用隐藏选项：

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 \
  -debug-pass=Structure /tmp/riscv-smoke.ll -o /dev/null 2>&1 | head -2
```

实测的 `Pass Arguments` 行（节选，`…` 为省略）：

```text
-targetlibinfo … -atomic-expand -riscv-zacas-abi-fix …
-loop-data-prefetch -riscv-gather-scatter-lowering -interleaved-access
-riscv-codegenprepare … -riscv-promote-constant -barrier -global-merge …
-riscv-isel -finalize-isel … -early-machinelicm -riscv-vl-optimizer
-riscv-vector-peephole -riscv-fold-mem-offset … -machine-combiner …
-riscv-opt-w-instrs -riscv-expand-pseudo-pre-ra -riscv-merge-base-offset
-riscv-prera-zilsd-opt -riscv-insert-read-write-csr -riscv-insert-write-vxrm
-riscv-vmv0-elimination … -riscv-insert-vsetvli -riscv-dead-defs …
-riscv-copyelim … -prolog-epilog … -post-ra-pseudos
-riscv-expand-pseudo-post-ra -kcfi -riscv-load-store-opt … -riscv-late-branch-opt
-riscv-indirect-branch-tracking -branch-relaxation -riscv-make-compressible …
-riscv-move-merge -riscv-push-pop-opt -riscv-expand-pseudo-pre-emit
-riscv-expand-pseudo-atomics … -riscv-asm-printer
```

把它按“谁加的”归类，就得到 `RISCVTargetMachine.cpp` 的骨架：

| 回调 | 行 | 加入的 RISC-V pass（节选） |
|---|---:|---|
| `addIRPasses` | 400 | `AtomicExpand`、`RISCVZacasABIFix`、`RISCVGatherScatterLowering`、`InterleavedAccess`、`RISCVCodeGenPrepare`（`-O0` 时跳过部分） |
| `addPreISel` | 419 | `RISCVPromoteConstant`、`GlobalMerge`（默认开，`MaxOffset=2047`） |
| `addInstSelector` | 449 | `RISCVISelDag` |
| `addMachineSSAOptimization` | 544 | `EarlyMachineLICM`、`RISCVVLOptimizer`、`RISCVVectorPeephole`、`RISCVFoldMemOffset`、`RISCVOptWInstrs`（RV64） |
| `addPreRegAlloc` | 568 | `RISCVExpandPseudoPreRA`、`RISCVMergeBaseOffset`、`RISCVPreAllocZilsdOpt`、`RISCVInsertReadWriteCSR`、`RISCVInsertWriteVXRM` |
| `addRegAssignAndRewriteOptimized` | 391 | RVV greedy regalloc、`VirtRegRewriter`、`RISCVInsertVSETVLI`、`RISCVDeadRegisterDefinitions` |
| `addPreSched2` | 488 | `RISCVExpandPseudoPostRA`、`KCFI`、`RISCVLoadStoreOpt` |
| `addPreEmitPass` | 497 | `RISCVLateBranchOpt`、`RISCVIndirectBranchTracking`、`BranchRelaxation`、`RISCVMakeCompressible` |
| `addPreEmitPass2` | 516 | `RISCVMoveMerge`、`RISCVPushPopOpt`、`RISCVExpandPseudoPreEmit`、`RISCVExpandPseudoAtomics` |

这张表在后续章节会反复出现。现在你只需要记住一件事：**`RISCVTargetMachine.cpp` 是 pass 顺序的唯一正交来源**，任何“为什么我的优化没跑”的问题，先来这里看它有没有被加进去、在哪个优化级别被条件跳过。

---

## 1.5 用 `-stop-after` / `-stop-before` 定点观察

LLVM 允许在任意 pass 之后停下并打印 MIR。语法是 `-stop-after=<pass-name>`，`<pass-name>` 用上表中的名字（不是文件名）。

### 1.5.1 第 5 站之后：选指完成

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 \
  -stop-after=finalize-isel /tmp/riscv-smoke.ll -o - \
  | sed -n '/name: *mul_add/,/^\.\.\./p' | grep -E 'MUL|ADDW|PseudoRET'
```

实测：

```text
  %3:gpr = MULW %0, %1
  %4:gpr = ADDW %3, %2
  PseudoRET implicit $x10
```

这一刻还是虚拟寄存器，指令是 `MULW`/`ADDW`，返回还是 `PseudoRET`。这就是“选指刚做完”的样子。

### 1.5.2 第 6 站：`MULW` 被降成 `MUL`

```bash
# 停在该 pass 之前
$T/bin/llc ... -stop-before=riscv-opt-w-instrs /tmp/riscv-smoke.ll -o - | grep -E 'MUL|ADDW'
# 停在该 pass 之后
$T/bin/llc ... -stop-after=riscv-opt-w-instrs  /tmp/riscv-smoke.ll -o - | grep -E 'MUL|ADDW'
```

实测：

```text
# stop-before
  %3:gpr = MULW %0, %1
  %4:gpr = ADDW %3, %2

# stop-after
  %3:gpr = MUL %0, %1
  %4:gpr = ADDW %3, %2
```

只差一个 pass，`MULW` 就变成了 `MUL`。为什么可以？因为 `MULW` 的高 32 位结果只被 `ADDW` 的低 32 位消费，而 `ADDW` 本身会截断。这个变换的合法性判断在 `RISCVOptWInstrs.cpp`，Ch 19 会完整拆。

### 1.5.3 第 8–9 站之后：物理寄存器与 prologue

```bash
$T/bin/llc ... -stop-after=prolog-epilog /tmp/riscv-smoke.ll -o - \
  | sed -n '/name: *mul_add/,/^\.\.\./p' | grep -E 'MUL|ADDW|PseudoRET'
```

实测：

```text
  renamable $x10 = MUL killed renamable $x10, killed renamable $x11
  renamable $x10 = ADDW killed renamable $x10, killed renamable $x12
  PseudoRET implicit $x10
```

虚拟寄存器消失了，`killed` 标记出现了，但 `PseudoRET` 还在。它要到 PreEmit 才被展开成真正的 `RET`。

### 1.5.4 RVV：`vsetvli` 不是选指生成的

用一个小向量函数：

```llvm
define <4 x i32> @vadd(<4 x i32> %a, <4 x i32> %b) {
  %r = add <4 x i32> %a, %b
  ret <4 x i32> %r
}
```

最终汇编（`clang -target riscv64 -march=rv64gcv -mabi=lp64d -O2`）：

```asm
vadd:
	vsetivli	zero, 4, e32, m1, ta, ma
	vadd.vv	v8, v9, v8
	ret
```

停在寄存器分配后的 `riscv-insert-vsetvli`：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -target-abi=lp64d -O2 \
  -stop-after=riscv-insert-vsetvli /tmp/rvv-smoke.ll -o - \
  | sed -n '/^body:/,$p'
```

实测：

```text
bb.0 (%ir-block.0):
  liveins: $v8, $v9

  dead $x0 = PseudoVSETIVLI 4, 208 /* e32, m1, ta, ma */, implicit-def $vl, implicit-def $vtype
  renamable $v8 = PseudoVADD_VV_M1 undef renamable $v8, killed renamable $v8, killed renamable $v9, 4 /* vl */, 5 /* e32 */, 1 /* ta, mu */, implicit $vl, implicit $vtype
  PseudoRET implicit $v8
```

注意两件事：

- `PseudoVSETIVLI` 是在**寄存器分配之后**由 `riscv-insert-vsetvli` 插入的，不是选指阶段；
- `vadd` 的第三个操作数顺序是 `undef, v8, v9`，和汇编里的 `v9, v8` 看起来不一致——这是 RVV 的 `passthru`/`mask` 操作数约定，Ch 17 会解释。

### 1.5.5 第 11 站：MCInst

`-stop-after` 看到的是 `MachineInstr`，不是最终编码。要看 MC 层，用 `llvm-mc -show-inst`：

```bash
printf 'addi a0, a0, 1\n' | $T/bin/llvm-mc -triple=riscv64 -show-inst
```

实测：

```text
	addi	a0, a0, 1                       # <MCInst #13776 ADDI
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Reg:X10>
                                        #  <MCOperand Imm:1>>
```

`#13776` 是 TableGen 生成的 opcode 编号。同一个 `addi`，在 MIR 里叫 `ADDI`，在 MC 层叫 `MCInst #13776`，在汇编文本里叫 `addi`——三种名字，一条指令。

### 1.5.6 第 12 站：重定位

```bash
printf '.globl g\nld a0, g\n' | $T/bin/llvm-mc -triple=riscv64 -filetype=obj -o /tmp/rel.o -
$T/bin/llvm-readobj -r /tmp/rel.o
```

实测：

```text
Relocations [
  Section (3) .rela.text {
    0x0 R_RISCV_PCREL_HI20 g 0x0
    0x4 R_RISCV_PCREL_LO12_I .Lpcrel_hi0 0x0
  }
]
```

一条 `ld` 变成两条重定位，因为 RV64 的 PC-relative 地址需要 `auipc` + `ld` 两条指令配合，而汇编器把 `ld a0, g` 展开成了这个序列。Ch 11–12 会从 `lowerGlobalAddress` 一路追到 lld 的 `relax()`。

---

## 1.6 症状 → 站位：triage 速查表

这张表是本章最实用的部分。看到 issue 时，先按症状定位，不要先读代码。

| 症状 | 先看站位 | 首先打开的文件 | 第一条命令 |
|---|---:|---|---|
| 选错指令 / 该选没选 | 5 | `RISCVInstrInfo*.td`、`RISCVISelDAGToDAG.cpp` | `llc -stop-after=finalize-isel` |
| 多了一条 `addw` / `mulw` | 6 | `RISCVOptWInstrs.cpp` | `-stop-before/after=riscv-opt-w-instrs` |
| 多了一条 `vsetvli` | 6/8 | `RISCVInsertVSETVLI.cpp`、`RISCVVectorPeephole.cpp` | `-stop-after=riscv-insert-vsetvli` |
| prologue/epilogue 错 | 9 | `RISCVFrameLowering.cpp` | `-stop-after=prolog-epilog` |
| 参数位置错 / varargs 错 | 3 | `RISCVISelLowering.cpp` 的 `LowerFormalArguments`（28701） | `llc -stop-after=finalize-isel` |
| 汇编语法错 / 别名不对 | 11 | `RISCVAsmPrinter.cpp`、`RISCVInstPrinter.cpp` | 直接看 `llc -o -` |
| 编码字节错 | 11 | `RISCVMCCodeEmitter.cpp` | `llvm-mc -show-encoding` |
| 反汇编错 | 11 | `RISCVDisassembler.cpp` | `llvm-mc -disassemble` |
| 重定位错 / 链接失败 | 12 | `RISCVELFObjectWriter.cpp`、`lld/ELF/Arch/RISCV.cpp` | `llvm-readobj -r` |
| ELF flags / ABI 错 | 12 | `RISCVELFStreamer.cpp`、`RISCVAsmBackend.cpp` | `llvm-readobj --file-headers` |
| 合法化崩溃 / 类型不合法 | 2 | `RISCVISelLowering.cpp` 构造函数 | `llc -debug-only=legalize-dag` |
| DAG combine 错删/错改 | 4 | `RISCVISelLowering.cpp` 的 `PerformDAGCombine` | `-stop-after=riscv-isel` 前后对比 |

> 表中的行号是本章写作时的 trunk 行号。行号会漂移，函数名不会；优先用符号搜索。

---

## 1.7 三个 monolith 的读法

### 1.7.1 `RISCVISelLowering.cpp`（31,346 行）

不要从头读。按自然边界切：

| 顺序 | 行范围（约） | 内容 |
|---:|---|---|
| 1 | 67–2062 | 构造函数：寄存器类、合法类型、`setOperationAction`、libcall |
| 2 | 2062–3070 | 查询钩子：`isLegalAddressingMode`、`isTruncateFree` |
| 3 | 3071–3599 | RVV 类型胶水：LMUL、容器类型、VL |
| 4 | 3599–5386 | 标量/FP/向量 lowering helper |
| 5 | 5386–7460 | shuffle lowering（`lowerVECTOR_SHUFFLE` 在 6731） |
| 6 | 7461–8374 | CTLZ/CTTZ、非对齐 RVV、常量、`IS_FPCLASS` |
| 7 | 8374–9853 | `LowerOperation` 大 switch：查“哪个 DAG 节点去哪” |
| 8 | 9853–11078 | 全局地址、TLS、trampoline |
| 9 | 11078–15116 | select、shift、mask、insert/extract |
| 10 | 15116–17091 | reduction、interleave/deinterleave、masked load/store |
| 11 | 17091–18997 | `ReplaceNodeResults` |
| 12 | 18997–25386 | combine helper + 按 opcode 的 combine |
| 13 | 25386–27736 | `PerformDAGCombine`、known bits |
| 14 | 27736–28520 | 自定义 inserter |
| 15 | 28520–30335 | 调用约定、内联汇编、原子 |
| 16 | 30335–31346 | 非对齐访问、stack probe、动态分配 |

维护时的入口通常是第 7 块（`LowerOperation`）和第 13 块（`PerformDAGCombine`），而不是第 1 块。

### 1.7.2 `RISCVInstrInfoVPseudos.td`（7,392 行）

读法：先读 `LMULInfo`（175）和 `VTypeInfo`（291），再读 `RISCVVPseudo`（576）与 `RISCVMaskedPseudo`（622），最后才看 6,121 行之后的 `defm PseudoVL...`。前面的模板决定了后面的实例长什么样。

### 1.7.3 `RISCVISelDAGToDAG.cpp`（5,295 行）

读法：`PreprocessISelDAG`（35）→ `Select`（1178）→ 地址匹配器（3478–4096）→ `selectSETCC`（4173）/`selectSHXADDOp`（4311）。`Select` 是一个巨大的 `switch (N->getOpcode())`，用 `grep -n "case RISCVISD::"` 建索引，不要顺序读。

---

## 1.8 踩雷集锦

1. **pass 名字不是文件名。** `RISCVOptWInstrs.cpp` 的 pass 名是 `riscv-opt-w-instrs`；`RISCVExpandPseudoPreRA.cpp` 是 `riscv-expand-pseudo-pre-ra`。用 `llc -debug-pass=Structure` 或 `llc --help-hidden | grep riscv` 查。
2. **`-stop-after` 必须在 pass 名精确匹配时才有输出。** 写错名字时 `llc` 会报 `Unknown pass name`，而不是静默继续。
3. **`llc` 没有 `-mabi`。** ABI 用 `-target-abi=lp64d`；`-mattr` 只控制扩展特性。`-mabi` 是 `clang` driver 的选项。
4. **MIR 输出前面有一大段 IR。** `-stop-after` 默认先打印模块 IR，再打印 MIR。用 `sed -n '/^body:/,$p'` 或 `grep` 过滤。
5. **GlobalISel 是另一条管线。** `-global-isel` 时 pass 名变成 `irtranslator`、`legalizer`、`regbankselect`、`instruction-select`，`riscv-isel` 不会出现。Ch 23 单独讲。
6. **`-O0` 会跳过很多 RISC-V pass。** 上面的表里很多 pass 有 `if (OptLevel != CodeGenOptLevel::None)`；用 `-O0` 复现不了 `-O2` 的行为，反之亦然。
7. **`prolog-epilog` 不是 `prologue-epilogue`。** pass 名用连字符，且 `prolog` 少一个 `ue`。
8. **`-debug-pass=Structure` 需要断言构建或 `-debug` 支持。** 本课的构建是 assertions ON；release 构建可能看不到。

---

## 1.9 可跑实验

### 实验 1：复现五个停点（约 30 秒）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cd /tmp

$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=finalize-isel riscv-smoke.ll -o - \
  | grep -E 'MUL|ADDW|PseudoRET'
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -stop-before=riscv-opt-w-instrs riscv-smoke.ll -o - \
  | grep -E 'MUL|ADDW'
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=riscv-opt-w-instrs riscv-smoke.ll -o - \
  | grep -E 'MUL|ADDW'
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=prolog-epilog riscv-smoke.ll -o - \
  | grep -E 'MUL|ADDW|PseudoRET'
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 riscv-smoke.ll -o - | sed -n '/^mul_add:/,/ret/p'
```

预期：`MULW/ADDW` → `MUL/ADDW` → 物理寄存器 → `mul/addw/ret`。

### 实验 2：比较 `-O0` 与 `-O2` 的 pass 列表

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O0 -debug-pass=Structure /tmp/riscv-smoke.ll -o /dev/null 2>&1 | head -1 > /tmp/p0.txt
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -debug-pass=Structure /tmp/riscv-smoke.ll -o /dev/null 2>&1 | head -1 > /tmp/p2.txt
diff <(tr ' ' '\n' < /tmp/p0.txt) <(tr ' ' '\n' < /tmp/p2.txt) | grep riscv
```

你会看到哪些 RISC-V pass 在 `-O0` 被跳过。这直接解释了“为什么 `-O0` 下没有这条优化”。

### 实验 3：找出“哪一站改了这条指令”

把 `-stop-after` 当作二分工具：

1. 先用 `finalize-isel` 确认选指输出；
2. 如果最终汇编不同，从 pipeline 中点（例如 `prolog-epilog`）停一次；
3. 根据差异方向，在前后半段继续二分；
4. 找到第一个让指令形态变化的 pass 名。

这个流程比读代码快，而且是**证据驱动**的。Ch 28 会把它系统化成调试方法论。

---

## 1.10 本章重点整理

- RISC-V 后端的十二个站位按**数据形态**划分：DAG → MachineInstr → MCInst → ELF。
- `RISCVTargetMachine.cpp` 是 pass 顺序的唯一来源；`-debug-pass=Structure` 可以把它打印出来。
- 指令形态会在选指之后继续变化：`MULW → MUL` 发生在 `riscv-opt-w-instrs`，`vsetvli` 发生在 `riscv-insert-vsetvli`。
- `-stop-after`/`-stop-before` 是维护者最强的定位工具；配合 `-run-pass` 和 MIR，可以把任何优化单独拎出来观察。
- 症状 → 站位的 triage 表能让你在打开 31k 行文件之前就知道该找什么。

## 1.11 自我检核

- [ ] 能画出 IR → DAG → MIR → MC → ELF 五层，并说出每层的指令表示
- [ ] 能说出至少三个“选指之后才发生”的指令变换
- [ ] 会用 `-debug-pass=Structure` 查真实 pass 顺序
- [ ] 会用 `-stop-before`/`-stop-after` 二分定位一条指令被谁改掉
- [ ] 知道 `llc` 用 `-target-abi` 而不是 `-mabi`
- [ ] 看到“汇编不对”时，能先给出两个最可能的站位，而不是直接读 `LowerOperation`

---

## 1.12 延伸阅读

- **`llvm/docs/CodeGenerator.md`**（本树）
  - **读哪里**：`### Required components in the code generator`（71 行）、`### The high-level design of the code generator`（98 行）、`## Machine code description classes`（304 行）、`## The "MC" Layer`（563 行）。
  - **为什么读**：本章的十二站位是 RISC-V 实例，这份文档是 LLVM 的通用抽象。先看通用层，再看 RISC-V 怎么填。
- **`llvm/docs/RISCV/RISCVVectorExtension.md`**（本树，320 行）
  - **读哪里**：`## SelectionDAG lowering`（92 行）、`### Fixed-length vectors`（106 行）、`## RISCVInsertVSETVLI`（283 行）。
  - **为什么读**：它直接解释了本章 1.5.4 的 `PseudoVSETIVLI` 和容器类型，是 RVV 站的官方设计说明。
- **`llvm/docs/MIRLangRef.md`**（本树）
  - **读哪里**：`Machine Function`、`Machine Basic Block`、`Machine Instruction`、`Register` 四节。
  - **为什么读**：`-stop-after` 的输出就是 MIR；不会读 MIR，就无法用停点法定位问题。
- **[SelectionDAG 官方文档](https://llvm.org/docs/CodeGenerator.html#selectiondag-selector)**（`CodeGenerator.md` 的 SelectionDAG 小节）
  - **读哪里**：`SelectionDAG Selector` 与 `SelectionDAG Legalize`。
  - **为什么读**：Ch 13–16 会反复回到 DAG 的合法化与匹配；先把官方术语对齐。
- **`llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`**（本树，617 行）
  - **读哪里**：318–600 行的 `RISCVPassConfig`。
  - **为什么读**：这是本章所有 pass 顺序的源码依据；以后任何“这个 pass 为什么没跑”的问题，都从这里开始查。

---

→ 下一章：[Ch 02 — ISA、扩展与 Subtarget：从 `+zba` 到 `hasStdExtZba()`](./02-isa-extensions-subtarget.md)
