# Ch 03 — 流水线与 MIR：`RISCVPassConfig` 的真实 pass 名单

> **目标**：能从源码和运行时两个方向说清“一个 RISC-V 函数在 `llc` 里经过了哪些 pass、顺序是什么、哪些在 `-O0` 会被跳过”；能读懂 MIR，并能用 `-run-pass` 把单个机器 pass 拎出来单独跑。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，assertions ON，HEAD `0fc5afa04c23`。

---

## 3.1 为什么维护者必须掌握流水线

三个真实场景：

1. **“这个优化为什么没跑？”** 可能它根本没被加到 pipeline 里，或者被 `if (OptLevel != None)` 跳过了。
2. **“这条指令是谁改的？”** 只有知道 pass 顺序，才能用二分法定位（Ch 01 的停点法）。
3. **“我想单独测这个 pass。”** 需要 MIR + `-run-pass`，而不是每次都跑完整编译。

LLVM 的 codegen pipeline 有两套：legacy `TargetPassConfig` 和 new PM `CodeGenPassBuilder`。RISC-V 目前默认仍走 legacy（`createPassConfig`），`RISCVCodeGenPassBuilder.cpp` 也在，但本章以实际默认路径为准。

---

## 3.2 先建立直觉：流水线是“形态转换”

```text
LLVM IR
  │  IR passes（AtomicExpand / RISC-V CodeGenPrepare / ...）
  ▼
SelectionDAG
  │  Legalize → Combine → ISel
  ▼
MachineInstr（虚拟寄存器，SSA）
  │  Machine SSA 优化（MachineCombiner / OptWInstrs / VLOptimizer）
  ▼
MachineInstr（PreRA 伪指令、地址折叠）
  │  RegAlloc
  ▼
MachineInstr（物理寄存器）
  │  PostRA / PreEmit（伪指令展开、压缩、push/pop）
  ▼
MachineInstr（可发射）
  │  AsmPrinter → MCInst → 编码
  ▼
ELF
```

每个箭头都是一组 pass。`RISCVTargetMachine.cpp` 的 `RISCVPassConfig` 决定这组 pass 里有哪些 RISC-V 专属项。

---

## 3.3 真实 pass 顺序

### 3.3.1 `-O2` 的 RISC-V pass

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 \
  -debug-pass=Structure /tmp/riscv-smoke.ll -o /dev/null 2>&1 | head -1
```

把 `Pass Arguments` 行按空格拆开，只留 `riscv`：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -debug-pass=Structure \
  /tmp/riscv-smoke.ll -o /dev/null 2>&1 | head -1 | tr ' ' '\n' | grep riscv
```

实测（`-O2`）：

```text
-riscv-zacas-abi-fix
-riscv-gather-scatter-lowering
-riscv-codegenprepare
-riscv-promote-constant
-riscv-isel
-riscv-vl-optimizer
-riscv-vector-peephole
-riscv-fold-mem-offset
-riscv-opt-w-instrs
-riscv-expand-pseudo-pre-ra
-riscv-merge-base-offset
-riscv-prera-zilsd-opt
-riscv-insert-read-write-csr
-riscv-insert-write-vxrm
-riscv-vmv0-elimination
-riscv-insert-vsetvli
-riscv-dead-defs
-riscv-copyelim
-riscv-expand-pseudo-post-ra
-riscv-load-store-opt
-riscv-late-branch-opt
-riscv-indirect-branch-tracking
-riscv-make-compressible
-riscv-move-merge
-riscv-push-pop-opt
-riscv-expand-pseudo-pre-emit
-riscv-expand-pseudo-atomics
-riscv-asm-printer
```

### 3.3.2 `-O0` 只剩 13 个

```bash
# 同一条命令，改 -O0
```

实测：

```text
-riscv-zacas-abi-fix
-riscv-isel
-riscv-expand-pseudo-pre-ra
-riscv-insert-read-write-csr
-riscv-insert-write-vxrm
-riscv-vmv0-elimination
-riscv-insert-vsetvli
-riscv-expand-pseudo-post-ra
-riscv-indirect-branch-tracking
-riscv-make-compressible
-riscv-expand-pseudo-pre-emit
-riscv-expand-pseudo-atomics
-riscv-asm-printer
```

### 3.3.3 `-O2` 独有的 15 个

```bash
comm -13 <(sort /tmp/p0.txt) <(sort /tmp/p2.txt)
```

实测：

```text
-riscv-codegenprepare
-riscv-copyelim
-riscv-dead-defs
-riscv-fold-mem-offset
-riscv-gather-scatter-lowering
-riscv-late-branch-opt
-riscv-load-store-opt
-riscv-merge-base-offset
-riscv-move-merge
-riscv-opt-w-instrs
-riscv-prera-zilsd-opt
-riscv-promote-constant
-riscv-push-pop-opt
-riscv-vector-peephole
-riscv-vl-optimizer
```

这张表解释了很多“为什么 `-O0` 下行为不同”：

- `MULW → MUL` 不发生，因为 `riscv-opt-w-instrs` 被跳过；
- 向量代码更啰嗦，因为 `riscv-vl-optimizer`、`riscv-vector-peephole` 被跳过；
- 压缩指令更少，因为 `riscv-make-compressible` 仍在，但前面的优化没做；
- 栈帧更保守，因为 `riscv-push-pop-opt`、`riscv-move-merge` 被跳过。

> 维护提示：如果一个 bug 只在 `-O2` 出现，先确认是哪个 RISC-V pass 引入的；如果只在 `-O0` 出现，通常不是优化 bug，而是伪指令展开/ABI 的问题。

---

## 3.4 MIR 的解剖

`-stop-after=finalize-isel` 的输出是一份 YAML：

```yaml
---
name:            mul_add
alignment:       4
exposesReturnsTwice: false
legalized:       false
regBankSelected: false
selected:        false
failedISel:      false
tracksRegLiveness: true
noPhis:          false
isSSA:           true
noVRegs:         false
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 3, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 4, class: gpr, preferred-register: '', flags: [  ] }
liveins:
  - { reg: '$x10', virtual-reg: '%0' }
  - { reg: '$x11', virtual-reg: '%1' }
frameInfo:
  ...
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11, $x12

    %3:gpr = MULW %0, %1
    %4:gpr = ADDW %3, %2
    PseudoRET implicit $x10
```

逐项读：

| 字段 | 含义 |
|---|---|
| `name` | 函数名 |
| `selected` | 是否已经完成指令选择；本树在 `-stop-after=finalize-isel` 时仍打印 `false`，所以判断是否选指完成要看指令形态，不要只信这个字段 |
| `isSSA` | 当前是否 SSA 形式；寄存器分配后变 `false` |
| `noVRegs` | 是否已无虚拟寄存器；`prolog-epilog` 后变 `true` |
| `registers` | 虚拟寄存器编号与寄存器类（`gpr`、`fpr64`、`vr`） |
| `liveins` | 物理寄存器到虚拟寄存器的映射 |
| `body` | 机器基本块；`bb.0` 后面括号里是来源 IR block |
| `%3:gpr` | 虚拟寄存器 3，类 `gpr` |
| `$x10` | 物理寄存器 `a0` |
| `PseudoRET` | 伪指令；PreEmit 后才变 `RET` |

几个容易忽略的细节：

- `renamable`：表示这个操作数可以被重命名（寄存器分配后常见）。
- `killed`：表示这是该虚拟/物理寄存器的最后一次使用。
- `implicit $x10`：`PseudoRET` 隐式使用返回寄存器。
- `dead`：结果没人用，例如 `dead $x0 = PseudoVSETIVLI ...`。

---

## 3.5 用 `-run-pass` 单独跑一个机器 pass

这是维护机器层 bug 的核心技能。步骤：

### 3.5.1 生成 MIR

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 \
  -stop-after=finalize-isel /tmp/riscv-smoke.ll -o /tmp/isel.out

# 输出里有两份 YAML：第一份是 IR，第二份是 MIR；取第二份
awk 'BEGIN{n=0} /^---$/{n++} n>=2' /tmp/isel.out > /tmp/isel.mir
```

### 3.5.2 直接跑会踩三个坑

**坑 1：`varArgsSaveSize` / `varArgsFrameIndex` 不认识**

```text
error: YAML:63:3: unknown key 'varArgsSaveSize'
```

这是 `-stop-after` 打印了 MIR parser 当前不接受的字段。删掉：

```bash
grep -v -E 'varArgsSaveSize|varArgsFrameIndex' /tmp/isel.mir > /tmp/isel2.mir
```

**坑 2：寄存器名不认识**

```text
error: /tmp/isel2.mir:31:13: unknown register name 'x10'
```

MIR 里的 `$x10` 需要目标信息才能解析：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -run-pass=riscv-opt-w-instrs /tmp/isel2.mir -o -
```

**坑 3：`%ir-block.0` 未定义**

```text
error: /tmp/isel3.mir:63:9: use of undefined IR block '%ir-block.0'
```

去掉 IR block 标注：

```bash
sed 's/ (%ir-block\.[0-9]*)//' /tmp/isel2.mir > /tmp/isel3.mir
```

### 3.5.3 成功运行

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -run-pass=riscv-opt-w-instrs /tmp/isel3.mir -o -
```

实测（`mul_add` 部分）：

```text
%3:gpr = MUL %0, %1
%4:gpr = ADDW %3, %2
PseudoRET implicit $x10
```

输入 MIR 里是 `MULW`，输出变成 `MUL`。这就是 `riscv-opt-w-instrs` 的全部职责；你不需要跑完整 pipeline 就能验证它。

> 实践建议：把这三个坑写进一个 `mir-extract.sh` 小脚本，后面调试会反复用到。Ch 28 会给一个更完整的版本。

---

## 3.6 `-print-after` / `-print-before`：不中断流水线

`-stop-after` 会**停止**编译；`-print-after` 只打印，然后继续。

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 \
  -print-before=riscv-opt-w-instrs /tmp/riscv-smoke.ll -o /dev/null 2>&1 \
  | grep -E "IR Dump Before|MULW"
```

实测：

```text
# *** IR Dump Before RISC-V Optimize W Instructions (riscv-opt-w-instrs) ***:
  %3:gpr = MULW %0:gpr, %1:gpr
```

`-print-after` 同理：

```text
# *** IR Dump After RISC-V Optimize W Instructions (riscv-opt-w-instrs) ***:
  %3:gpr = MUL %0:gpr, %1:gpr
  %4:gpr = ADDW %3:gpr, %2:gpr
```

两者的差别：

| 选项 | 是否继续编译 | 适合场景 |
|---|---|---|
| `-stop-after` | 否 | 取一份稳定的 MIR 做输入 |
| `-stop-before` | 否 | 取该 pass 的输入 MIR |
| `-print-after` | 是 | 观察某个 pass 前后差异，同时保留最终汇编 |
| `-print-before` | 是 | 观察输入，且不打断后续优化 |

---

## 3.7 pipeline 的源码结构

`RISCVTargetMachine.cpp` 的 `RISCVPassConfig`（318 行）继承 `TargetPassConfig`，重写回调。每个回调返回时，pass 已经按调用顺序加入。

| 回调 | 行 | 作用 |
|---|---:|---|
| `addIRPasses` | 400 | IR 层：原子、RISC-V 特有 IR 优化、通用 IR passes |
| `addPreISel` | 419 | ISel 前：常量提升、GlobalMerge |
| `addCodeGenPrepare` | 443 | 类型提升 |
| `addInstSelector` | 449 | SelectionDAG 选指 |
| `addIRTranslator` | 455 | GlobalISel：IR → gMIR |
| `addPreLegalizeMachineIR` | 460 | GISel combiner |
| `addLegalizeMachineIR` | 468 | GISel 合法化 |
| `addPreRegBankSelect` | 473 | GISel post-legalize combiner |
| `addRegBankSelect` | 478 | GISel 寄存器组选择 |
| `addGlobalInstructionSelect` | 483 | GISel 指令选择 |
| `addMachineSSAOptimization` | 544 | 机器 SSA 优化 |
| `addPreRegAlloc` | 568 | PreRA 伪指令/地址折叠 |
| `addRegAssignAndRewriteFast` | 383 | fast regalloc + `InsertVSETVLI` |
| `addRegAssignAndRewriteOptimized` | 391 | greedy regalloc + `InsertVSETVLI` |
| `addPostRegAlloc` | 587 | PostRA 冗余拷贝消除 |
| `addPreSched2` | 488 | 第二调度前：PostRA 伪指令 |
| `addPreEmitPass` | 497 | 发射前：分支、压缩 |
| `addPreEmitPass2` | 516 | 发射前第二轮：move/push-pop/原子 |
| `addILPOpts` | 592 | MachineCombiner（可选） |

**维护入口**：如果你想加一个 RISC-V 专属机器 pass，99% 的情况是改这里，而不是改通用 `TargetPassConfig`。加完后，记得更新 `RISCVPassRegistry.def`（如果它走 new PM 注册）和 `llvm/test/CodeGen/RISCV/O3-pipeline.ll` 这类 pipeline 结构测试。

---

## 3.8 踩雷集锦

1. **`-debug-pass=Structure` 的输出有两部分。** 第一行 `Pass Arguments:` 是扁平列表；后面是树形结构。用 `head -1` 抓列表最方便。
2. **pass 名与文件名不同。** 见 Ch 01 的表；用 `grep -n "create.*Pass"` 或 `llc --help-hidden` 对照。
3. **`-O0` 不是“把 `-O2` 的 pass 全关掉”。** 仍有 13 个 RISC-V pass 必须跑（伪指令展开、VSETVLI、汇编输出）。
4. **MIR 不是稳定格式。** `-stop-after` 的输出可能包含当前 parser 不接受的字段；提取脚本要能容错。
5. **`-run-pass` 不会自动补分析。** 有些 pass 需要 `-verify-machineinstrs` 或特定 analyses；失败时先看 pass 的 `getRequiredProperties`。
6. **`isSSA` / `noVRegs` 是判断阶段的好指标。** `isSSA=false` 说明已经过分寄存器分配前的某个破坏 SSA 的 pass；`noVRegs=true` 说明已完成分配。
7. **new PM 的 pass 名不同。** 如果加 `-enable-new-pm=1`，看到的是 `riscv-isel` 之外的另一套名字；本课默认 legacy。

---

## 3.9 可跑实验

### 实验 1：打印并对比 `-O0` / `-O2`

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
for O in 0 2; do
  $T/bin/llc -mtriple=riscv64 -mattr=+m -O$O -debug-pass=Structure \
    /tmp/riscv-smoke.ll -o /dev/null 2>&1 | head -1 | tr ' ' '\n' | grep riscv | sort > /tmp/p$O.txt
done
echo "O2 only:"; comm -13 /tmp/p0.txt /tmp/p2.txt
```

### 实验 2：提取 MIR 并单跑一个 pass

按 3.5 的三步做一遍，最后确认输入 `MULW`、输出 `MUL`。

### 实验 3：观察 `isSSA` / `noVRegs` 的阶段变化

```bash
for p in finalize-isel riscv-opt-w-instrs prolog-epilog; do
  echo "== $p =="
  $T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -stop-after=$p /tmp/riscv-smoke.ll -o - \
    | grep -E "isSSA|noVRegs|registers:" | head -3
done
```

预期：`finalize-isel` 时 `isSSA=true, noVRegs=false`；`prolog-epilog` 后 `isSSA=false, noVRegs=true`。

---

## 3.10 本章重点整理

- RISC-V 默认走 legacy `TargetPassConfig`；`RISCVPassConfig` 是 pass 顺序的唯一来源。
- `-O2` 有 28 个 RISC-V 相关 pass，`-O0` 只有 13 个；差的 15 个决定了大量行为差异。
- MIR 的关键字段是 `isSSA`、`noVRegs`、`registers`、`liveins`、`body`。
- `-run-pass` 需要 MIR + `-mtriple`，并要处理三个打印/解析坑。
- `-print-after` 用于观察，`-stop-after` 用于取输入；两者不要混用。

## 3.11 自我检核

- [ ] 能说出 `-O2` 独有的至少 5 个 RISC-V pass，以及它们各自的作用
- [ ] 能解释 `isSSA` 和 `noVRegs` 在不同阶段的值
- [ ] 能把 `-stop-after=finalize-isel` 的输出变成可被 `-run-pass` 接受的 MIR
- [ ] 知道 `-print-after` 与 `-stop-after` 的区别
- [ ] 能在 `RISCVTargetMachine.cpp` 里找到某个 pass 是在哪个回调加入的
- [ ] 能解释为什么“只在 `-O0` 出现的 bug”通常不是优化 bug

---

## 3.12 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`**（本树，617 行）
  - **读哪里**：318–600 行 `RISCVPassConfig`。
  - **为什么读**：本章所有 pass 顺序的源码依据；新增 pass 的唯一正确入口。
- **`llvm/docs/MIRLangRef.md`**（本树）
  - **读哪里**：`Machine Function` 的字段表（`isSSA`、`noVRegs`、`tracksRegLiveness`）、`Machine Instruction` 的 `renamable`/`killed`/`implicit`。
  - **为什么读**：MIR 是机器层调试的通用语言；字段含义在这里有权威定义。
- **`llvm/docs/CodeGenerator.md`**（本树）
  - **读哪里**：`### The high-level design of the code generator`（98）、`## Machine code description classes`（304）。
  - **为什么读**：把本章的 RISC-V 实例放回 LLVM 通用 codegen 架构。
- **`llvm/test/CodeGen/RISCV/O3-pipeline.ll`**（本树）
  - **读哪里**：整份文件；它用 FileCheck 固定了 `-O3` 的 pass 顺序。
  - **为什么读**：加/删 pass 时，这个测试会直接告诉你“pipeline 结构变了”。
- **`llvm/test/CodeGen/RISCV/O0-pipeline.ll`** 与 `O1-newpm-pipeline.ll`
  - **读哪里**：与 `O3-pipeline.ll` 对照。
  - **为什么读**：理解不同优化级别的结构差异，以及 new PM 与 legacy 的命名差异。

---

→ 下一章：[Ch 04 — 指令的 TableGen 建模：格式、扩展文件与 P-ext 案例](./04-tablegen-instruction-model.md)
