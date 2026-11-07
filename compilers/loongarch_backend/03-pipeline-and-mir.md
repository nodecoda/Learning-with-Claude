# Ch 03 — 流水线与 MIR：`llc` 到底按什么顺序跑

> 本章目标：把 `llc` 的黑盒拆成三段可观察的流水线：IR passes、ISel/机器 SSA、寄存器分配与 Post-RA。学会用 `-debug-pass=Structure`、`-stop-after`、`-run-pass` 把任意一条指令停在你想看的位置。

---

## 3.1 先记住三段

`llc` 的流水线可以粗分成：

```text
IR passes
  → ISel（SelectionDAG → MachineFunction）
    → 机器 SSA 优化（MachineCombiner/MachineCSE/MachineSink/Peephole...）
      → Pre-RA 展开与地址折叠
        → 寄存器分配（Greedy/Fast）
          → Post-RA 优化（LateBranchOpt/MemoryBarrierOpt...）
            → AsmPrinter / MC
```

LoongArch 的 target 专属 pass 集中在后三段：

| 阶段 | pass 名 | 文件 |
|---|---|---|
| IR | `loop-data-prefetch` | `LoongArchTargetMachine.cpp::addIRPasses` |
| IR | `atomic-expand` | 同上（`createAtomicExpandLegacyPass`） |
| IR | `type-promotion` | `addCodeGenPrepare` |
| ISel | `loongarch-isel` | `LoongArchISelDAGToDAG.cpp` |
| 机器 SSA | `machine-combiner` | `addILPOpts` |
| 机器 SSA | `loongarch-opt-w-instrs` | `addMachineSSAOptimization` |
| Pre-RA | `loongarch-prera-expand-pseudo` | `addPreRegAlloc` |
| Pre-RA | `loongarch-merge-base-offset` | `addPreRegAlloc` |
| RA 前后 | `loongarch-dead-defs` | `addRegAssignAndRewrite*` |
| Post-RA | `loongarch-late-branch-opt` | `addMachineLateOptimization` |
| Post-RA | `branch-relaxation` | `addPreEmitPass` |
| Post-RA | `loongarch-expand-pseudo` | `addPreEmitPass2` |
| Post-RA | `loongarch-expand-atomic-pseudo` | `addPreEmitPass2` |
| Post-RA | `loongarch-memory-barrier-opt` | `addPreEmitPass2` |

---

## 3.2 真实 pass 序列（`-O2`）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -debug-pass=Structure /tmp/la-smoke.ll -o /dev/null 2>&1 | head -3
```

实测第一行（Pass Arguments）非常长，关键片段按顺序是：

```text
... -loop-data-prefetch -atomic-expand -verify
... -type-promotion -codegenprepare -verify
... -loongarch-isel -finalize-isel
... -machine-combiner -early-machinelicm -machine-cse -machine-sink -peephole-opt
... -loongarch-opt-w-instrs -loongarch-prera-expand-pseudo -loongarch-merge-base-offset
... -register-coalescer -machine-scheduler -loongarch-dead-defs
... -greedy -virt-reg-rewriter ...
... -shrink-wrap -prolog-epilog -loongarch-late-branch-opt -machine-latecleanup
... -branch-relaxation
... -loongarch-expand-pseudo -loongarch-expand-atomic-pseudo -loongarch-memory-barrier-opt
```

把它和 `LoongArchTargetMachine.cpp` 的 `LoongArchPassConfig` 对照：

```cpp
void LoongArchPassConfig::addIRPasses() {
  if (TM->getOptLevel() != CodeGenOptLevel::None && EnableLoopDataPrefetch)
    addPass(createLoopDataPrefetchPass());
  addPass(createAtomicExpandLegacyPass());
  TargetPassConfig::addIRPasses();
}

bool LoongArchPassConfig::addInstSelector() {
  addPass(createLoongArchISelDag(getLoongArchTargetMachine(), getOptLevel()));
  return false;
}

void LoongArchPassConfig::addPreEmitPass() { addPass(&BranchRelaxationPassID); }

void LoongArchPassConfig::addPreEmitPass2() {
  addPass(createLoongArchExpandPseudoPass());
  addPass(createLoongArchExpandAtomicPseudoPass());
  if (TM->getOptLevel() != CodeGenOptLevel::None)
    addPass(createLoongArchMemoryBarrierOptPass());
}
```

注意 `addInstSelector()` 返回 `false`：这告诉 `TargetPassConfig` **不要再追加默认的 ISel pass**，因为 LoongArch 自己注册了 `loongarch-isel`。

---

## 3.3 `-O0` 少了什么

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O0 \
  -debug-pass=Structure /tmp/la-smoke.ll -o /dev/null 2>&1 | head -1
```

实测 `-O0` 的 Pass Arguments：

```text
... -atomic-expand -verify ... -loongarch-isel -finalize-isel -localstackalloc
-loongarch-prera-expand-pseudo -phi-node-elimination -regallocfast
... -prolog-epilog ... -branch-relaxation ... -loongarch-expand-pseudo
-loongarch-expand-atomic-pseudo
```

对比 `-O2`：

| pass | `-O2` | `-O0` | 原因 |
|---|---|---|---|
| `loop-data-prefetch` | 有 | 无 | `getOptLevel() != None` 才加 |
| `type-promotion` | 有 | 无 | 同上 |
| `machine-combiner` | 有 | 无 | `addILPOpts` 只在优化级别调用 |
| `loongarch-opt-w-instrs` | 有 | 无 | `addMachineSSAOptimization` 里的优化分支 |
| `loongarch-merge-base-offset` | 有 | 无 | Pre-RA 优化分支 |
| `loongarch-dead-defs` | 有 | 无 | `addRegAssignAndRewrite*` 的优化分支 |
| `loongarch-late-branch-opt` | 有 | 无 | `addMachineLateOptimization` 的优化分支 |
| `loongarch-memory-barrier-opt` | 有 | 无 | `addPreEmitPass2` 的优化分支 |
| `regallocfast` vs `greedy` | `greedy` | `regallocfast` | O0 用 Fast RA |

这也解释了 `llvm/test/CodeGen/LoongArch/O0-pipeline.ll` 的存在：它专门锁定 O0 的 pass 结构，防止有人在 `add*Pass` 里无意中把优化 pass 加到 O0。

---

## 3.4 MIR：把编译停在任意一站

### 4.1 `-stop-after` / `-stop-before`

```bash
# 停在 ISel 之后
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+relax \
  -stop-after=loongarch-isel /tmp/tlsle.ll -o /tmp/tlsle-isel.mir

# 停在 Pre-RA 伪指令展开之后
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+relax \
  -stop-after=loongarch-prera-expand-pseudo /tmp/tlsle.ll -o /tmp/tlsle-prera.mir
```

### 4.2 真实 MIR：`PseudoLA_TLS_LE` 的展开

ISel 之后（`tlsle-isel.mir` 的 `body`）：

```yaml
body:             |
  bb.0 (%ir-block.0):
    %0:gpr = PseudoLA_TLS_LE @tvar
    $r4 = COPY %0
    PseudoRET implicit $r4
```

Pre-RA 展开之后：

```yaml
body:             |
  bb.0 (%ir-block.0):
    %2:gpr = LU12I_W target-flags(loongarch-le-hi-r) @tvar
    %1:gpr = PseudoAddTPRel_D %2, $r2, target-flags(loongarch-le-add-r) @tvar
    %0:gpr = ADDI_D %1, target-flags(loongarch-le-lo-r) @tvar
    $r4 = COPY %0
    PseudoRET implicit $r4
```

这三行就是 Ch 00 里被 `ld.lld` 收缩的序列的“机器层形态”：

- `LU12I_W` + `loongarch-le-hi-r` → `R_LARCH_TLS_LE_HI20_R`
- `PseudoAddTPRel_D` + `loongarch-le-add-r` → 后面展开成 `add.d $rd, $rd, $tp` + `R_LARCH_TLS_LE_ADD_R`
- `ADDI_D` + `loongarch-le-lo-r` → `R_LARCH_TLS_LE_LO12_R`

`target-flags(loongarch-le-hi-r)` 是 `MO_LE_HI_R` 在 MIR 里的文本形式；Ch 11 会把它和 `LoongArchBaseInfo.h` 的枚举一一对上。

### 4.3 `-run-pass`：只跑一个机器 pass

MIR 可以直接喂给 `llc`，只跑指定 pass：

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu \
  -run-pass=loongarch-opt-w-instrs /tmp/tlsle-prera.mir -o -
```

这是写机器 pass 回归测试的标准方式：测试文件里放一段 MIR，`RUN: llc -run-pass=... | FileCheck`。`llvm/test/CodeGen/LoongArch/*.mir`（如 `cfr-copy.mir`、`merge-base-offset-debug-only.mir`）都是这个模式。

### 4.4 MIR 语法最小集

| 片段 | 含义 |
|---|---|
| `---` / `...` | YAML 文档分隔 |
| `name: tls_addr` | 函数名 |
| `tracksRegLiveness: true` | 是否跟踪活性 |
| `registers:` | 虚拟寄存器列表（id/class/flags） |
| `body: \|` | 机器指令块 |
| `%0:gpr = ...` | 虚拟寄存器定义 |
| `$r4` | 物理寄存器 |
| `target-flags(loongarch-le-hi-r)` | 目标操作数标志 |
| `implicit $r4` | 隐式使用/定义 |
| `PseudoRET` | 伪返回 |

---

## 3.5 一个 pass 是怎么被“看见”的

`-debug-pass=Structure` 打印的是 **PassManager 的结构**，不是“每条 IR 被哪个 pass 改了什么”。要看内容，用：

```bash
# 打印 ISel 之后的机器码
$T/bin/llc -mtriple=loongarch64 -print-after=loongarch-isel /tmp/la-smoke.ll -o /dev/null

# 打印某个 IR pass 前后
$T/bin/llc -mtriple=loongarch64 -print-before=codegenprepare -print-after=codegenprepare ...
```

实测 `-print-after=loongarch-isel`：

```text
# *** IR Dump After LoongArch DAG->DAG Pattern Instruction Selection (loongarch-isel) ***:
# Machine code for function add: IsSSA, TracksLiveness
Function Live Ins: $r4 in %0, $r5 in %1

bb.0 (%ir-block.0):
  liveins: $r4, $r5
  %1:gpr = COPY $r5
  %0:gpr = COPY $r4
  %2:gpr = ADD_D %0:gpr, %1:gpr
  $r4 = COPY %2:gpr
  PseudoRET implicit $r4
```

注意 `ADD_D` 在 ISel 之后就已经出现了，但参数还是虚拟寄存器 `%0/%1/%2`；物理寄存器 `$r4/$r5` 是 ABI lowering 的 live-in/return 约定。

---

## 3.6 验证流水线没有被人改坏

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llvm-lit -sv llvm/test/CodeGen/LoongArch/O0-pipeline.ll llvm/test/CodeGen/LoongArch/opt-pipeline.ll
```

这两个测试用 `opt`/`llc` 的 `-debug-pass=Structure` 输出做 FileCheck，是“pass 集合”的回归锁。改 `LoongArchTargetMachine.cpp` 的 `add*Pass` 时，先跑它们。

---

## 3.7 反直觉点

1. **两个 expand-pseudo 分居寄存器分配前后**，`prera-expand-pseudo` 和 `expand-pseudo` 不是同一个 pass 的两个名字（3.1）。
2. **`loongarch-late-branch-opt` 在 `shrink-wrap` 之后**：那时 prologue/epilogue 已经插入，分支折叠必须考虑这些指令（3.2）。
3. **`-run-pass` 只吃 MIR，不吃 `.ll`**；`-stop-after` 的输出正好可以喂回去（3.4）。
4. **MIR 里没有 `$zero`、没有 `RET`**：用 `$r0`、`PseudoRET`（Ch 28 有完整实测报错）。
5. **`-O0` 不是“没有 pass”**：它仍跑 legalize、isel、RA，只是砍掉优化分支并改用 `regallocfast`（3.3）。
6. **`O0-pipeline.ll` 专门锁 pass 结构**：在 `add*Pass` 里加一个优化 pass，可能同时改变 O0 的行为并被这个测试抓到。

---

## 3.8 本章证据

| 结论 | 命令/源码 |
|---|---|
| O2 pass 序列 | `llc -O2 -debug-pass=Structure` |
| O0 pass 序列 | `llc -O0 -debug-pass=Structure` |
| target pass 注册 | `LoongArchTargetMachine.cpp::LoongArchPassConfig` |
| ISel 后 MIR | `llc -stop-after=loongarch-isel` |
| Pre-RA 后 MIR | `llc -stop-after=loongarch-prera-expand-pseudo` |
| 单 pass 运行 | `llc -run-pass=...` |
| pass 结构回归 | `O0-pipeline.ll`、`opt-pipeline.ll` |

---

## 3.9 练习

1. 用 `-stop-after=finalize-isel`、`-stop-after=machine-combiner`、`-stop-after=greedy` 各输出一份 MIR，按顺序观察 `ADD_D` 的操作数从虚拟寄存器变成物理寄存器。
2. 把 `tlsle.ll` 的 `localexec` 改成 `initialexec`，看 ISel 后的伪指令变成什么（提示：`PseudoLA_TLS_IE`）。
3. 用 `-run-pass=loongarch-merge-base-offset` 跑一个带全局地址的 MIR，确认 `pcalau12i + addi` 被折叠。
4. 读 `O0-pipeline.ll` 的 `RUN` 行，解释它为什么能在不运行完整 `llc` 的情况下检查 pass 集合。

---

## 3.10 延伸阅读

1. **MIR 语法与单 pass 测试** — `llvm/docs/MIRLangRef.md`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`## MIR Testing Guide`（`-run-pass`、`-stop-after`/`-stop-before`、`-simplify-mir`）、`## High Level Structure`、`### Machine Instructions`、`### Machine Operands`。
   - 学到什么：把 3.4 的最小 MIR 集补全：`target-flags(...)`、`implicit`、`registers:` 在官方语法中的位置，以及如何把 `-stop-after` 输出改写成稳定的单 pass 测试。
   - 前提：用 `llc -stop-after=loongarch-isel` 看过一份 MIR（本章 3.4）。

2. **七阶段模型与 target pass 的插入点** — `llvm/docs/CodeGenerator.md`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`### The high-level design of the code generator` 的阶段列表，以及 `### Instruction Selection`、`### SSA-based Machine Code Optimizations`、`### Register Allocation`、`### Late Machine Code Optimizations`、`### Code Emission` 的导言。
   - 学到什么：解释 3.1 的三段式为什么还能再细分，以及 `addILPOpts`、`addPreRegAlloc`、`addPreEmitPass2` 各自落在哪个官方阶段。
   - 前提：知道 `MachineFunction`/`MachineInstr` 是什么（本章 3.4 或 Ch 01）。

3. **pass 注册的真实代码** — `llvm/lib/Target/LoongArch/LoongArchTargetMachine.cpp`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：`LoongArchPassConfig` 的 `addIRPasses()`、`addInstSelector()`、`addMachineSSAOptimization()`、`addPreRegAlloc()`、`addMachineLateOptimization()`、`addPreEmitPass()`、`addPreEmitPass2()`。
   - 学到什么：把 3.2 的实测序列和源码逐行对上；重点看 `addInstSelector()` 为什么返回 `false`。
   - 前提：读过本章 3.2 的 `-debug-pass=Structure` 输出。

4. **pass 结构回归锁的实际形态** — `llvm/test/CodeGen/LoongArch/O0-pipeline.ll`（路径相对 /home/ubuntu/llvm/trees/ct-instr-trunk）
   - 读什么：两条 RUN 行（loongarch32/loongarch64 + `-O0 --debug-pass=Structure`）、`REQUIRES: asserts`，以及 `CHECK-NEXT` 链里 `Fast Register Allocator`、`LoongArch Pre-RA pseudo instruction expansion pass`、`Post-RA pseudo instruction expansion pass` 等关键行。
   - 学到什么：理解 3.6 的“改 `add*Pass` 会被测试抓到”具体怎么抓；同时看到 `-O0` 仍保留哪些 pass。
   - 前提：知道 FileCheck 的 `CHECK-NEXT` 语义（可先读 `llvm/docs/CommandGuide/FileCheck.md`）。

## 3.11 下一步

Part 0 结束。接下来进入 Part 1：从 `LoongArchInstrFormats.td` 开始，看一条指令的 32 位编码、汇编文本、fixup 和重定位是怎么被 TableGen 生成的。
