# 练习 A — 加一条指令并跑通往返

> **目标**：在不改源码树的前提下，写出“新增一条 RISC-V 指令”的可迁移补丁形状；能独立验证 TableGen、编码、汇编器、反汇编器、printer 与 lit 各层；能手算 R/I 型编码并用 `llvm-mc` 对账。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（`analysis/riscv-maintenance`，HEAD `0fc5afa04c23`）；构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON）。本章“实测”均来自该树与该构建；需重编才能看到的步骤明确标“未实测”。

> **前置**：读完 **Ch 04**（`ALU_ri`/`ALU_rr`、`RVInst` 位切片、include 顺序）与 **Ch 06**（`InstBits[]`、`MCCodeEmitter`、fixup）。读过 Ch 07 的 AsmParser/InstPrinter/Disassembler 会更顺。

---

## A.1 为什么练习是“加一条指令”

一条新指令是后端最小、但最完整的全链路：TableGen 格式 → 指令定义 → 生成物 → 编码 → 汇编/反汇编/printer → lit。真实上游 PR 就是这个形状。本树可查两个样本（**实测**）：

```text
d3291c692c0a [RISCV][MC] Add ... zicond     11 files changed, 91 insertions(+)
e296cedcd686 [RISCV][MC] ... zalasr         13 files changed, 265 insertions(+)
```

比例很关键：**`.td` 只占 25–59 行，测试与 feature 接线占大头**。本树只读，不能 `edit`/`commit`/`checkout`，也不能重跑 `ninja` 重编 `llc`。所以本章训练两件事：**补丁形状**（允许改时改哪几个文件）与**每环独立验证**（哪些现在就能用 `llvm-mc`/`llvm-objdump`/`llvm-lit` 证明）。

> 证据约定：**实测** = 本机真实输出；**源码依据** = 本树行号；**未实测** = 需重编/真机/改源码。

---

## A.2 先建立直觉：一条指令要穿过 7 道门

```text
[1] RISCVInstrFormats.td        格式：RVInst / RVInstR / RVInstI / RVInstRAtomic
     field bits<32> Inst;  Inst{31-25}=funct7; Inst{14-12}=funct3; ...
              │ 被模具继承
              ▼
[2] RISCVInstrInfo*.td          class ALU_rr / ALU_ri / Shift_ri
     def FOO : ALU_rr<funct7, funct3, "foo">, Sched<[...]>;
     let Predicates = [HasStdExt...] in { ... }
     def : Pat<(IR 形状), (FOO ...)>;
              │ llvm-tblgen
              ▼
[3] TableGen 生成物（builds/ct-instr-trunk/lib/Target/RISCV/）
     RISCVGenInstrInfo.inc / RISCVGenAsmMatcher.inc / RISCVGenDAGISel.inc
     RISCVGenAsmWriter.inc / RISCVGenDisassemblerTables.inc
     RISCVGenMCCodeEmitter.inc（InstBits[] + getBinaryCodeForInstr）
              │
              ▼
[4] RISCVMCCodeEmitter.cpp       emitInstruction → getBinaryCodeForInstr + getImmOpValue
              │ 字节流 / MCInst
              ▼
[5] RISCVAsmParser.cpp           文本 → MCInst（operand class 匹配）
[6] RISCVInstPrinter.cpp         MCInst → 文本（先试 alias）
    RISCVDisassembler.cpp        字节 → MCInst（生成的解码表）
              │
              ▼
[7] lit：llvm/test/MC/RISCV/rv*-valid.s / rv*-invalid.s
         llvm/test/CodeGen/RISCV/*.ll / attributes.ll
```

| 门 | 你改什么 | 生成物/落点 | 最小验证 |
|---|---|---|---|
| 1 格式 | `RISCVInstrFormats.td` 选 `RVInstR`/`RVInstI` | `Inst{31-0}` 位切片 | 手算 + `InstBits[]` |
| 2 定义 | `RISCVInstrInfo*.td` 的 `def`/`Pat`/`Predicates` | `RISCVGenInstrInfo.inc` | `grep` opcode |
| 3 TableGen | 通常不改，只重跑 | `RISCVGen*.inc` | 看生成物 |
| 4 编码 | 多数不改 C++ | `RISCVMCCodeEmitter.cpp` | `-show-encoding` |
| 5 汇编 | 需要时加 operand class | `RISCVAsmParser.cpp` | `-mattr` 正/反例 |
| 6 打印/反汇编 | 需要时加 alias | `RISCVInstPrinter/Disassembler.cpp` | `llvm-objdump -d` |
| 7 测试 | 新增/扩展 `.s`、`.ll` | lit | `llvm-lit -sv` |

关键认知：**第 4–6 门通常不写 C++**。格式与 `def` 写对，编码器、printer、反汇编器都由 TableGen 生成；需要手写的是“新 operand 语法”“伪指令展开”“新 fixup”这类跨层语义。

---

## A.3 选题：三个候选，先 grep 再决定

本树是 2026-11 trunk，标量位操作几乎填满。**不要凭记忆选 `andn`**（**实测**）：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
grep -rn "def ANDN\|def ORN\|def XNOR" llvm/lib/Target/RISCV/
# Zb.td:266 ANDN / :268 ORN / :270 XNOR，全部已存在
```

`andn`/`orn`/`xnor` 已存在，不能当“新增”练习。下面三个候选都先验证过“本树不存在”。

### 候选 1（易）：`crc32.b/h/w/d` 的 `ALU_rr` 形状

```bash
grep -rn "crc32" llvm/lib/Target/RISCV/       # 无输出
printf 'crc32.w a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zbc -show-encoding
# <stdin>:1:1: error: unrecognized instruction mnemonic
```

改动：`RISCVInstrInfoZb.td` 的 `HasStdExtZbc` 块加 `ALU_rr`（`crc32.d` 加 `IsRV64`）；`llvm/test/MC/RISCV/rv64zbc-valid.s`（新建）与 `rv64zbc-invalid.s`；只做 MC 时不需要 `Pattern` 和 C++。

> ratified `Zbc` 最终只保留 `clmul*`，CRC32 是草案指令。这道题是**编码形状练习**，不是提议把 CRC32 塞回 `Zbc`；真要合入应新 feature（A.4.3）。

### 候选 2（中）：`Zalasr` 的 unsigned acquire loads

```bash
printf 'lb.aq a0, (a1)\n'  | $T/bin/llvm-mc -triple=riscv64 -mattr=+zalasr -show-encoding
# 	lb.aq	a0, (a1)                        # encoding: [0x2f,0x85,0x05,0x34]
printf 'lbu.aq a0, (a1)\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zalasr -show-encoding
# <stdin>:1:1: error: unrecognized instruction mnemonic, did you mean: lb.aq?
```

本树 `RISCVInstrInfoZalasr.td` 只实例化 `LB/LH/LW/SB/SH/SW`（RV32）与 `LD/SD`（RV64），没有 `lbu.aq`/`lhu.aq`/`lwu.aq`。改动：复用 `LAQ_r`/`LAQ_r_aq_rl` 加 `LBU/LHU`（`IsRV64` 下 `LWU`）；`HasStdExtZalasr` 已存在（`RISCVFeatures.td:289`）；扩展 `rv{32,64}zalasr-valid.s`；要 CodeGen 则补 `PatLAQ<acquiring_load<atomic_load_zext_8>>` 等 pattern。

### 候选 3（难）：`PseudoCCMULW` 的 Pseudo + 展开

```bash
grep -rn "PseudoCCMULW" llvm/lib/Target/RISCV/   # 无输出
grep -n "def PseudoCCMUL\b" llvm/lib/Target/RISCV/RISCVInstrInfoSFB.td
# 252:def PseudoCCMUL : SFBALU_rr;
```

改动：`RISCVInstrInfoSFB.td` 照 `PseudoCCMUL` 加 RV64-only 的 `PseudoCCMULW`；`RISCVExpandPseudoPreEmit.cpp` 的 `expandMI` 加 case；`RISCVInstrInfo.cpp::getInstSizeInBytes` 加 size case；CodeGen SFB 测试。它受 `HasShortForwardBranchIALU`（tune feature）门控，不是用户可见的 `+ext`。

**难度排序**：候选 1（只碰 TableGen + MC）→ 候选 2（feature 门控 + load pattern）→ 候选 3（Pseudo + C++ 展开 + size）。本章主线用候选 1 的 `crc32.w` 讲补丁形状；手算编码用真实存在的 `andn`，每步都能对账。

---

## A.4 候选 1 的补丁形状：一个 `ALU_rr` 指令

### A.4.1 选格式：`ALU_rr` 不是 `RVInstR`

`RISCVInstrFormats.td:387` 的 `RVInstR` 只补 `funct7`：

```tablegen
class RVInstR<bits<7> funct7, bits<3> funct3, RISCVOpcode opcode, dag outs,
              dag ins, string opcodestr, string argstr>
    : RVInstRBase<funct3, opcode, outs, ins, opcodestr, argstr> {
  let Inst{31-25} = funct7;
}
```

`RVInstRBase`（`:373`）放 `rs2/rs1/funct3/rd/opcode`。本树没有 `Shift_rr` 类：寄存器移位复用 `ALU_rr`，立即数移位用 `Shift_ri`（`:789`）。日常不要直接拿 `RVInstR` 写指令，而用 `RISCVInstrInfo.td:795` 的模具：

```tablegen
let hasSideEffects = 0, mayLoad = 0, mayStore = 0 in
class ALU_rr<bits<7> funct7, bits<3> funct3, string opcodestr,
             bit Commutable = 0>
    : RVInstR<funct7, funct3, OPC_OP, (outs GPR:$rd), (ins GPR:$rs1, GPR:$rs2),
              opcodestr, "$rd, $rs1, $rs2"> {
  let isCommutable = Commutable;
}
```

它一次给你：`OPC_OP`（`:149`，`0b0110011`）、`GPR` 输出 + 两个 `GPR` 输入、汇编串 `"$rd, $rs1, $rs2"`、`hasSideEffects=0/mayLoad=0/mayStore=0`。漏了最后一项，机器层会把你当有副作用的黑盒。

### A.4.2 `.td` 定义

把 `crc32.w` 放进 `RISCVInstrInfoZb.td:373` 的 `HasStdExtZbc` 块，紧挨 `CLMULR`：

```tablegen
let Predicates = [HasStdExtZbc] in {
def CLMULR : ALU_rr<0b0000101, 0b010, "clmulr", Commutable=1>,
             Sched<[WriteCLMUL, ReadCLMUL, ReadCLMUL]>;
// 练习：CRC32 草案形状；本树尚无，+zbc 下 llvm-mc 报 unrecognized
def CRC32_W : ALU_rr<0b0110000, 0b010, "crc32.w">,
              Sched<[WriteIALU, ReadIALU, ReadIALU]>;
} // Predicates = [HasStdExtZbc]
```

RV64-only 的 `crc32.d` 用本树 2026 年的 `let append Predicates`：

```tablegen
let Predicates = [HasStdExtZbc] in {
let append Predicates = [IsRV64] in
def CRC32_D : ALU_rr<0b0110000, 0b011, "crc32.d">,
              Sched<[WriteIALU, ReadIALU, ReadIALU]>;
} // Predicates = [HasStdExtZbc]
```

要点：`funct7=0b0110000`、`funct3=0b010` 是 `crc32.w` 的位；`crc32.b/h/d` 只换 `funct3`；`crc32` 不可交换，不要写 `Commutable=1`；`Sched<[...]` 不参与编码，先用通用 `WriteIALU/ReadIALU`。

<details>
<summary>折叠 1：`andn` 的完整 `.td` 定义、Predicates 与 Pattern</summary>

真实源码（`RISCVInstrInfoZb.td:265-272`、`:481-486`）：

```tablegen
let Predicates = [HasStdExtZbbOrZbkb] in {
def ANDN  : ALU_rr<0b0100000, 0b111, "andn">,
            Sched<[WriteIALU, ReadIALU, ReadIALU]>;
def ORN   : ALU_rr<0b0100000, 0b110, "orn">,
            Sched<[WriteIALU, ReadIALU, ReadIALU]>;
def XNOR  : ALU_rr<0b0100000, 0b100, "xnor", Commutable=1>,
            Sched<[WriteIALU, ReadIALU, ReadIALU]>;
} // Predicates = [HasStdExtZbbOrZbkb]

let Predicates = [HasStdExtZbbOrZbkb] in {
def : Pat<(XLenVT (and GPR:$rs1, (not GPR:$rs2))), (ANDN GPR:$rs1, GPR:$rs2)>;
def : Pat<(XLenVT (or  GPR:$rs1, (not GPR:$rs2))), (ORN  GPR:$rs1, GPR:$rs2)>;
def : Pat<(XLenVT (not (riscv_xor_like GPR:$rs1, GPR:$rs2))),
          (XNOR GPR:$rs1, GPR:$rs2)>;
def : Pat<(XLenVT (and GPR:$rs1, invLogicImm:$rs2)), (ANDN GPR:$rs1, invLogicImm:$rs2)>;
def : Pat<(XLenVT (or  GPR:$rs1, invLogicImm:$rs2)), (ORN  GPR:$rs1, invLogicImm:$rs2)>;
def : Pat<(XLenVT (xor GPR:$rs1, invLogicImm:$rs2)), (XNOR GPR:$rs1, invLogicImm:$rs2)>;
} // Predicates = [HasStdExtZbbOrZbkb]
```

`ANDN` 没有 `Commutable=1`，因为 `not` 只作用在 `rs2`；`Pat` 把 `(and x, (not y))` 选成 `ANDN`。**实测**：`llc -mattr=+zbb` 生成 `andn a0, a0, a1`，默认 target 生成 `not` + `and`。

这也解释了 `crc32.w` 为什么可以“没有 pattern”：它没有对应的 LLVM IR 节点；只做 MC 时 `def` 就够了。要 CodeGen，必须先有 intrinsic/ISD 节点（Ch 15），再写 `Pat`。

</details>

### A.4.3 若需要新 feature：照 Zicond 的形状

若 `crc32` 要独立成 `+zcrc`，在 `RISCVFeatures.td` 写三件套。本树 `Zicond` 就是模板（`:144-148`）：

```tablegen
def FeatureStdExtZicond
    : RISCVExtension<1, 0, "Integer Conditional Operations">,
      RISCVExtensionBitmask<0, 38>;
def HasStdExtZicond : Predicate<"Subtarget->hasStdExtZicond()">,
                      AssemblerPredicate<(all_of FeatureStdExtZicond),
                          "(Integer Conditional Operations)">;
```

- `RISCVExtension`（`:29`）负责 feature 名、版本、隐含扩展；
- `RISCVExtensionBitmask`（`:56`）的 `groupID/bitPos` 来自 psABI 的 `__riscv_feature_bits` 分配，**不能随手填**；
- `HasStdExt*` 同时是 CodeGen `Predicate` 与 `AssemblerPredicate`；
- 新文件还要在 `RISCVInstrInfo.td` 的 include 列表加一行（Zicond 在 `:2481`）。

**实测**（现有 Zicond 的门控）：

```text
$ printf 'czero.eqz a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zicond -show-encoding
	czero.eqz	a0, a1, a2              # encoding: [0x33,0xd5,0xc5,0x0e]
$ printf 'czero.eqz a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
<stdin>:1:1: error: instruction requires the following: (Integer Conditional Operations)
```

错误文本就是 `AssemblerPredicate` 的第二个参数。新 feature 描述写错，这条错误也会跟着错；测试 `CHECK` 要抄真实输出。

### A.4.4 Pattern：什么时候需要

| 情况 | 需要 `Pat` 吗 | 例子 |
|---|---|---|
| 只有汇编/MC 语义，没有 IR 节点 | 不需要 | `crc32.w`（练习） |
| 有等价 IR 形状，能被 DAG 组合 | 需要 | `ANDN` 的 `(and x, (not y))` |
| 需要新 intrinsic/ISD 节点 | 先加节点，再写 `Pat` | `czero.eqz` 的 `riscv_czero_eqz` |
| Pseudo 只在机器层展开 | 不需要 DAG pattern | `PseudoCCMOVGPR` |

`czero` 的例子（`RISCVInstrInfoZicond.td:19,39`）：先 `def riscv_czero_eqz : RVSDNode<...>`，再 `def : Pat<(riscv_czero_eqz ...), (CZERO_EQZ ...)>`。没有 IR 形状就不要硬写 `Pat`。

### A.4.5 AsmParser / InstPrinter / Disassembler 要不要改

**AsmParser：多数不用。** `ALU_rr` 两个输入都是 `GPR`；`RISCVAsmParser.cpp:523` 的 `isGPR()` 已能匹配。需要新 operand class 的典型场景：立即数 `ALU_ri` 用 `simm12_lo`（`:904` `isSImm12LO`）；移位量 `Shift_ri` 用 `uimmlog2xlen`（`:775` `isUImmLog2XLen`）；内存 `GPRMemZeroOffset`（`RISCVInstrInfo.td:235`）只接受 `(a1)` 零偏移；伪指令如 `PseudoZEXT_H` 在 `:4638` 有自定义 `emitPseudoExtend`。

**InstPrinter：多数不用。** `RISCVInstPrinter.cpp:67` 的 `printInst` 先试 `printAliasInstr`，失败才 `printInstruction`。只有要打印成另一个助记符（如 `zext.b` → `andi rd, rs, 255`，`RISCVInstrInfo.td:1317`）时才需要 `InstAlias` 或自定义 print。`crc32.w` 零改动。

**Disassembler：多数不用。** 解码表由 `RISCVGenDisassemblerTables.inc` 生成；`RISCVDisassembler.cpp:53` 的 `getInstruction32` 只是入口，`GPR` 解码在 `:132`。格式字段对，反汇编自动可用。

### A.4.6 测试放哪，怎么写

| 测试 | 文件 | 作用 |
|---|---|---|
| MC 正例 | `llvm/test/MC/RISCV/rv64zbc-valid.s` | 汇编 + 编码 + 反汇编往返 |
| MC 负例 | `llvm/test/MC/RISCV/rv64zbc-invalid.s` | 缺 feature / 缺操作数 / 错寄存器 |
| CodeGen | `llvm/test/CodeGen/RISCV/rv64zbb.ll`（若加 pattern） | 选择到新指令 |
| attributes | `llvm/test/CodeGen/RISCV/attributes.ll`（若加 feature） | `.attribute arch` |
| arch 解析 | `llvm/test/MC/RISCV/attribute-arch.s`（若加 feature） | `-march` 字符串 |

MC 正例骨架（`rv64zalasr-valid.s:1-8` 真实写法）：

```asm
# RUN: llvm-mc %s -triple=riscv64 -mattr=+zalasr -M no-aliases -show-encoding \
# RUN:     | FileCheck -check-prefixes=CHECK-ASM,CHECK-ASM-AND-OBJ %s
# RUN: llvm-mc -filetype=obj -triple=riscv64 -mattr=+zalasr < %s \
# RUN:     | llvm-objdump --mattr=+zalasr -M no-aliases -d -r - \
# RUN:     | FileCheck --check-prefix=CHECK-ASM-AND-OBJ %s
```

`CHECK-ASM` 抓 `encoding: [...]`，`CHECK-ASM-AND-OBJ` 抓 objdump 文本；一份 `.s` 同时锁汇编器与反汇编器。只抓助记符只能证明“能解析”，不能证明“编码对”。

<details>
<summary>折叠 3：新增 `crc32.w` 的完整 MC 测试（预期值，未实测）</summary>

```asm
# RUN: llvm-mc %s -triple=riscv64 -mattr=+zbc -M no-aliases -show-encoding \
# RUN:     | FileCheck -check-prefixes=CHECK-ASM,CHECK-ASM-AND-OBJ %s
# RUN: llvm-mc -filetype=obj -triple=riscv64 -mattr=+zbc < %s \
# RUN:     | llvm-objdump --mattr=+zbc -M no-aliases -d -r - \
# RUN:     | FileCheck --check-prefix=CHECK-ASM-AND-OBJ %s

# CHECK-ASM-AND-OBJ: crc32.w t0, t1, t2
# CHECK-ASM: encoding: [0xb3,0x22,0x73,0x60]
crc32.w t0, t1, t2
```

推导见折叠 2：`t0=x5`、`t1=x6`、`t2=x7`，32 位值 `0x607322B3`，小端 `b3 22 73 60`。负例：

```asm
# RUN: not llvm-mc -triple=riscv64 < %s 2>&1 | FileCheck %s
# CHECK: error: instruction requires the following: 'Zbc' (Carry-Less Multiplication)
crc32.w t0, t1, t2
```

**未实测**：本树没有 `crc32.w`，`llvm-mc` 只会报 `unrecognized instruction mnemonic`；以上编码与错误文本是“补丁落地后应看到”的预期。

</details>

---

## A.5 编码手算：`andn` 实测，`crc32.w` 推演

### A.5.1 真实指令 `andn`

```bash
printf 'andn a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zbb -show-encoding
# 	andn	a0, a0, a1                      # encoding: [0x33,0x75,0xb5,0x40]
```

小端拼成 `0x40B57533`。按 R 型拆：

| 字段 | 位 | 值 | 含义 |
|---|---|---|---|
| `funct7` | 31–25 | `0100000` | `ANDN` 的 funct7 |
| `rs2` | 24–20 | `01011` | `x11`（`a1`） |
| `rs1` | 19–15 | `01010` | `x10`（`a0`） |
| `funct3` | 14–12 | `111` | `ANDN` 的 funct3 |
| `rd` | 11–7 | `01010` | `x10`（`a0`） |
| `opcode` | 6–0 | `0110011` | `OPC_OP`（`0x33`） |

生成物给第二份证据（**实测**）：`RISCVGenInstrInfo.inc:14189` 是 `ANDN = 14174`，`RISCVGenMCCodeEmitter.inc:417` 是 `UINT64_C(1073770547)`。`1073770547 = 0x40007033`，即 `InstBits[ANDN]` 的固定位（funct7/funct3/opcode）；编码器再把 `rd/rs1/rs2` OR 进去得到 `0x40B57533`。这与 Ch 06 的 `InstBits[ADD]=51` 同机制。

### A.5.2 带 feature 的真实指令 `czero.eqz`

```text
$ printf 'czero.eqz a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zicond -show-encoding
	czero.eqz	a0, a1, a2              # encoding: [0x33,0xd5,0xc5,0x0e]
```

`0x0EC5D533`：`funct7=0000111`、`rs2=01100`（`a2`）、`rs1=01011`（`a1`）、`funct3=101`、`rd=01010`（`a0`）、`opcode=0110011`。

### A.5.3 练习目标 `crc32.w`（推演，未实测）

按 `ALU_rr<0b0110000, 0b010, "crc32.w">`，取 `a0,a1,a2`：

| 字段 | 位 | 值 |
|---|---|---|
| `funct7` | 31–25 | `0110000`（`0x30`） |
| `rs2` | 24–20 | `01100`（`a2=x12`） |
| `rs1` | 19–15 | `01011`（`a1=x11`） |
| `funct3` | 14–12 | `010` |
| `rd` | 11–7 | `01010`（`a0=x10`） |
| `opcode` | 6–0 | `0110011` |

```text
0x30<<25 = 0x60000000 ; 0x0C<<20 = 0x00C00000 ; 0x0B<<15 = 0x00058000
0x02<<12 = 0x00002000 ; 0x0A<<7  = 0x00000500 ; opcode    = 0x00000033
-------------------------------------------------------------------
                              0x60C5A533  → 小端 [0x33,0xa5,0xc5,0x60]
```

**未实测**：本树没有 `crc32.w`；这个值只说明“字段怎么落位”，不是本机输出。

<details>
<summary>折叠 2：手算编码的完整位域推导（`andn` + `crc32.w`）</summary>

`andn a0, a0, a1`：

```text
funct7 0100000<<25 = 0x40000000 ; rs2 x11=01011<<20 = 0x00B00000
rs1    x10=01010<<15 = 0x00050000 ; funct3 111<<12 = 0x00007000
rd     x10=01010<<7  = 0x00000500 ; opcode 0110011 = 0x00000033
-------------------------------------------------------------
                       0x40B57533  → 小端 33 75 b5 40
```

`crc32.w t0, t1, t2`：

```text
funct7 0110000<<25 = 0x60000000 ; rs2 x7=00111<<20 = 0x00700000
rs1    x6=00110<<15 = 0x00030000 ; funct3 010<<12 = 0x00002000
rd     x5=00101<<7  = 0x00000280 ; opcode 0110011 = 0x00000033
-------------------------------------------------------------
                       0x607322B3  → 小端 b3 22 73 60
```

两个例子覆盖 R 型全部六个字段；换成 I 型时把 `funct7` 换成 `imm[11:0]`，见 `RVInstI`（`RISCVInstrFormats.td:467`）。

</details>

---

## A.6 每一步的独立验证手段

| 门 | 命令 | 你应该看到 |
|---|---|---|
| 格式/定义 | `grep -n "def CRC32_W" llvm/lib/Target/RISCV/RISCVInstrInfoZb.td` | 改完才有的 `def` |
| TableGen | `grep -n "CRC32_W" $T/lib/Target/RISCV/RISCVGenInstrInfo.inc` | 新 opcode（重编后） |
| 编码 | `llvm-mc -show-encoding` | `encoding: [..]` |
| 反汇编 | `llvm-mc -filetype=obj` + `llvm-objdump -d` | 同一条指令文本 |
| feature | `-mattr=+x` vs 不写 | 接受 vs `instruction requires...` |
| alias/伪指令 | `llvm-mc -M no-aliases -show-encoding` | alias 展开后的真实指令 |
| lit | `llvm-lit -sv <test>` | `Passed: 1` |

### A.6.1 编码与反汇编

```bash
printf '.text\nandn a0, a0, a1\nczero.eqz a0, a1, a2\n' > /tmp/pa.s
$T/bin/llvm-mc -triple=riscv64 -mattr=+zbb,+zicond -filetype=obj /tmp/pa.s -o /tmp/pa.o
$T/bin/llvm-objdump -d /tmp/pa.o
```

**实测**：

```text
0: 40b57533     	andn	a0, a0, a1
4: 0ec5d533     	czero.eqz	a0, a1, a2
```

注意：`llvm-objdump` **没有** `--mattr=+zbb,+zicond` 也能解码。反汇编表不检查 feature predicate；feature 只约束汇编器/CodeGen（踩雷 8）。

### A.6.2 feature 门控

```bash
printf 'andn a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zbb -show-encoding
printf 'andn a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=-zbb -show-encoding
```

**实测**：第二条：

```text
<stdin>:1:1: error: instruction requires the following: 'Zbb' (Basic Bit-Manipulation) or 'Zbkb' (Bitmanip instructions for Cryptography)
```

负例测试要抄整句，不要只写 `error`；文本来自 `AssemblerPredicate`（`RISCVFeatures.td:506/542`）。

### A.6.3 别名/伪指令

```bash
printf 'li a0, 42\nla a0, foo\ncall foo\n' \
  | $T/bin/llvm-mc -triple=riscv64 -mattr=+relax -show-encoding
```

**实测**（节选）：

```text
	li	a0, 42                          # encoding: [0x13,0x05,0xa0,0x02]
.Lpcrel_hi0:
	auipc	a0, %pcrel_hi(foo)              # fixup_riscv_pcrel_hi20
	addi	a0, a0, %pcrel_lo(.Lpcrel_hi0)  # fixup_riscv_pcrel_lo12_i
	call	foo                             # fixup_riscv_call_plt
```

加 `-M no-aliases` 后 `li` 变 `addi a0, zero, 42`（**实测**）。测试不加 `-M no-aliases`，`CHECK` 可能抓到 `li` 而不是 `addi`。

### A.6.4 lit

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/llvm-lit -sv llvm/test/MC/RISCV/rv64zbb-valid.s
$T/bin/llvm-lit -sv llvm/test/CodeGen/RISCV/rv64zbb.ll
```

**实测**：两个都是 `Total Discovered Tests: 1 / Passed: 1 (100.00%)`。新增测试：`CHECK-ASM` 抓 `encoding:`，`CHECK-ASM-AND-OBJ` 抓 objdump 文本，负例用 `not llvm-mc ...` + `CHECK: error: instruction requires...`；triple 与 `-mattr` 必须同时写。

### A.6.5 `update_llc_test_checks.py`

```bash
python3 /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/utils/update_llc_test_checks.py \
  --llc-binary=$T/bin/llc /tmp/pa3.ll
```

**实测**（脚本重写整个文件，不是只加一行）：

```llvm
; NOTE: Assertions have been autogenerated by utils/update_llc_test_checks.py UTC_ARGS: --version 6
; RUN: llc < %s -mtriple=riscv64 | FileCheck %s
define i64 @and64(i64 %a, i64 %b) {
; CHECK-LABEL: and64:
; CHECK:       # %bb.0:
; CHECK-NEXT:    and a0, a0, a1
; CHECK-NEXT:    ret
```

没有 `; RUN: llc ...` 行时它只加 `NOTE`、不生成 `CHECK`（**实测**：第一次跑无 RUN 行，文件只多 NOTE）。永远在 `/tmp` 副本上跑，用 `--function` 限制范围。

## A.7 如果真要落地：重建与验证序列（未实测）

> **未实测：源码树只读，重建 `llc` 不在本课实验范围内。** 以下是“应该跑什么 + 预期看到什么”。

```bash
ninja -C $T llc llvm-mc llvm-objdump llvm-lit
grep -n "CRC32_W" $T/lib/Target/RISCV/RISCVGenInstrInfo.inc
printf 'crc32.w a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zbc -show-encoding
printf 'crc32.w a0, a1, a2\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding  # 应报 requires 'Zbc'
$T/bin/llvm-lit -sv llvm/test/MC/RISCV/rv64zbc-valid.s
$T/bin/llvm-lit -sv llvm/test/MC/RISCV/rv64zbc-invalid.s
```

预期：`RISCVGenInstrInfo.inc` 出现 `CRC32_W = <新编号>`；`RISCVGenMCCodeEmitter.inc` 的 `InstBits[]` 出现 `0x60002033`（funct7=0x30、funct3=0b010、opcode=0x33 的固定位）；`-show-encoding` 输出 `[0x33,0xa5,0xc5,0x60]`（与 A.5.3 一致）；无 `-mattr` 时报 `'Zbc' (Carry-Less Multiplication)`；lit 全绿。若字节不符，先查 `funct7`/`funct3` 是否写反，再查 `InstBits[]`，不要先怀疑 `MCCodeEmitter`。

> **真机执行同样未实测**：本章只验证到 MC/objdump 层，不在 RISC-V 硬件或 qemu 上运行二进制。

---
## A.8 源码落点

| 内容 | 路径 | 本树行号/符号 |
|---|---|---|
| 指令基类 | `llvm/lib/Target/RISCV/RISCVInstrFormats.td` | `RVInst` 306；`Pseudo` 328；`RVInstRBase` 373；`RVInstR` 387；`RVInstRAtomic` 393；`RVInstI` 467；`RVInstIShift` 475 |
| opcode 表 | 同上 | `OPC_OP_IMM` 141；`OPC_AMO` 148；`OPC_OP` 149；`OPC_SYSTEM` 164 |
| ALU 模具 | `llvm/lib/Target/RISCV/RISCVInstrInfo.td` | `UImmLog2XLenAsmOperand` 366；`ALU_ri` 783；`ALU_rr` 795 |
| 基础指令 | 同上 | `ADDI` 894；`ANDI` 906；`ADD` 915；`AND` 935 |
| 伪指令 | 同上 | `PseudoLI` 1139；`li` alias 1159；`zext.b` alias 1317；`PseudoCALL` 1899；`PseudoLA` 2046；`PseudoSEXT_B` 2107；`PseudoZEXT_H` 2111；`PseudoZEXT_W` 2116 |
| include 顺序 | 同上 | `M` 2471；`A` 2474；`Za` 2475；`Zalasr` 2476；`Zimop` 2479；`Zicbo` 2480；`Zicond` 2481；`Zb` 2499 |
| Zb 指令 | `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td` | `ANDN` 266；`ORN` 268；`XNOR` 270；`SH3ADD` 279；`CLMULR` 374；`PACK` 397；`ANDN` pattern 481/486 |
| Zicond | `llvm/lib/Target/RISCV/RISCVInstrInfoZicond.td` | `CZERO_EQZ` 27；`CZERO_NEZ` 29；`Pat` 39；`defm CondOpsPat` 63 |
| Zalasr | `llvm/lib/Target/RISCV/RISCVInstrInfoZalasr.td` | `LAQ_r` 19；`SRL_r` 27；`LAQ_r_aq_rl` 34；`defm LB` 49；`defm LD` 58 |
| SFB 伪指令 | `llvm/lib/Target/RISCV/RISCVInstrInfoSFB.td` | `PseudoCCMOVGPR` 87；`PseudoCCADDW` 208；`PseudoCCMUL` 252 |
| Feature | `llvm/lib/Target/RISCV/RISCVFeatures.td` | `RISCVExtension` 29；`RISCVExtensionBitmask` 56；`Zicond` 144；`Zalasr` 289；`Zcmp` 462；`Zba` 495；`Zbb` 503；`HasStdExtZbbOrZbkb` 542；`Zbc` 562；SFB tune 2219 |
| 汇编器 | `llvm/lib/Target/RISCV/AsmParser/RISCVAsmParser.cpp` | `isGPR` 523；`isUImmLog2XLen` 775；`isSImm12LO` 904；`parseOperand` 3159；`PseudoSEXT_B` 4638 |
| printer | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVInstPrinter.cpp` | `printInst` 67；`printAliasInstr` 78；`printRegName` 83 |
| 反汇编器 | `llvm/lib/Target/RISCV/Disassembler/RISCVDisassembler.cpp` | `getInstruction32` 53；`DecodeGPRRegisterClass` 132 |
| 编码器 | `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp` | `getBinaryCodeForInstr` 85；`getImmOpValue` 112 |
| 指令大小 | `llvm/lib/Target/RISCV/RISCVInstrInfo.cpp` | `getInstSizeInBytes` 2090；PseudoCC size cases 2121；`foldMemoryOperandImpl` 955 |
| Pseudo 展开 | `llvm/lib/Target/RISCV/RISCVExpandPseudoPreEmit.cpp` | `expandMI` 81；`PseudoCCMOVGPR` case 97 |
| 测试更新 | `llvm/utils/update_llc_test_checks.py` | `--llc-binary` / `--function` / `--enable` |

生成物（**实测**）：`RISCVGenInstrInfo.inc` 7.0 MB，`ANDN = 14174`（第 14189 行）、`ADDI = 13776`（第 13791 行）；`RISCVGenMCCodeEmitter.inc` 454 KB，`InstBits[ANDN]=1073770547`（`0x40007033`，第 417 行）；`RISCVGenAsmMatcher.inc` 1.1 MB；`RISCVGenDAGISel.inc` 61 MB；`RISCVGenAsmWriter.inc` 1.9 MB；`RISCVGenDisassemblerTables.inc` 1.5 MB。

## A.9 可跑实验

1. **三层对账**：`llvm-mc -mattr=+zbb -show-encoding` → `[0x33,0x75,0xb5,0x40]`；`grep "^    ANDN " RISCVGenInstrInfo.inc` → `ANDN = 14174`；`grep ANDN RISCVGenMCCodeEmitter.inc` → `UINT64_C(1073770547)`。
2. **往返**：`llvm-mc -filetype=obj` + `llvm-objdump -d`，预期 `0: 40b57533 andn a0, a0, a1`。
3. **feature 门控**：`+zbb` 成功；`-zbb` 与不写都报 `'Zbb' ... or 'Zbkb' ...`；`czero.eqz` 同理报 `(Integer Conditional Operations)`。
4. **别名与 lit**：`li` 默认打印 `li`，`-M no-aliases` 打印 `addi`；`llvm-lit -sv` 跑 `rv64zbb-valid.s`/`rvzicond-valid.s`/`rv64zalasr-valid.s`/`rv64zbb.ll`，四个都 `Passed: 1`（**实测**）。
5. **update 脚本与 PR 形状**：`--function=and64` 只改一个函数；`git show --stat d3291c692c0a`/`e296cedcd686` 对照 11/13 files 的比例。

## A.10 踩雷集锦

1. **include 顺序影响 opcode 编号。** `ADDI=13776`、`ADD=13774`、`ANDN=14174` 都是生成时编号；新文件 include 在 `Zb` 前/后编号会变。测试只写指令名/字节，不写死编号。
2. **忘了 `hasSideEffects=0`。** `ALU_rr` 已继承，但新写格式类漏了它，机器层会当你有副作用：DCE 不敢删、调度不敢重排、`MachineCSE` 不敢合并。症状是“生成了但优化不动”，不是汇编报错。
3. **`isCommutable` 与 pattern 的关系。** 只有操作数顺序真的可交换才写；`crc32` 不可交换，`andn` 的 `not` 只作用在 `rs2` 也不可交换。写了 `Commutable=1` 但 pattern 只覆盖一种顺序，会让 DAG 组合出错误结果。
4. **AsmParser 的 operand 匹配优先级。** `GPR`、`GPRNoX0`、`GPRMemZeroOffset`、`simm12_lo`、`uimmlog2xlen` 是不同 `AsmOperandClass`；`GPR` 能匹配的文本，`GPRMemZeroOffset` 可能拒绝。先看现有类（`RISCVInstrInfo.td:235`），不要自己写更宽的类。
5. **`-mattr` 拼写/工具差异。** `llvm-mc` 用 `-mattr=+zbc`，不是 `-march`：**实测** `-march=rv64gc_zbb` 报 `Unknown command line argument` 并提示 `--arch`；`clang` 才用 `-march=rv64gc_zbc`。`+Zbb` 在本构建被接受（大小写不敏感），别把它当负例。
6. **`update_*_test_checks.py` 会重写整个文件。** 它不是 diff 工具，是按当前 `llc` 输出重排 `CHECK`；在仓库测试上直接跑会改掉无关函数。用 `/tmp` 副本 + `--function`。
7. **别名打印导致 `CHECK` 行意外。** `li a0, 42` 默认打印 `li`，`-M no-aliases` 才打印 `addi a0, zero, 42`；`llvm-objdump` 默认也可能打印 alias。测试统一加 `-M no-aliases`，否则换一个 immediate 就抓到不同文本。
8. **测试 triple 与 `-mattr` 必须同时写，且反汇编不查 feature。** `-triple=riscv32 -mattr=+zba` 下 `sh3add` 可用，但 `add.uw` 报 `instruction requires the following: RV64I Base Instruction Set`（**实测**）。RV64-only 指令必须在 `.td` 加 `IsRV64`，测试同时写 `-triple=riscv64` 与 RV32 负例。另：`llvm-objdump` 无 `--mattr` 也能解 `andn`/`czero.eqz`（**实测**），所以“objdump 能解”不能证明 feature 接对了。

<details>
<summary>折叠 4：忘了加 `Predicates` 会在哪一层暴露</summary>

假设 `crc32.w` 的 `def` 写对了，但忘了 `let Predicates = [HasStdExtZbc]`：

1. **汇编器先暴露“太宽松”**：`llvm-mc` 不带 `+zbc` 也接受，`rv64zbc-invalid.s` 期望的 `instruction requires...` 不出现，lit 失败。
2. **CodeGen 更危险**：`llc` 没有 `+zbc` 也可能选中它，生成目标不支持的指令；只在真机/模拟器上炸。
3. **反汇编器不会暴露**：解码表不检查 predicate（A.6.1 已实测 `andn`/`czero.eqz` 无 `--mattr` 也能解），所以“objdump 能解”不能证明 feature 接对了。
4. **正确修法**：在 `def` 外层加 `let Predicates = [...] in { ... }`，然后跑 `-mattr=+zbc`（成功）与不写（失败）两条命令；错误文本应与 `HasStdExtZbc` 的 `AssemblerPredicate` 描述一致。若不一致，先查 `RISCVFeatures.td`，不是查编码。

</details>

## A.11 本章重点整理

- 新增一条指令 = 格式（`RISCVInstrFormats.td`）+ 定义（`RISCVInstrInfo*.td`）+ 生成物（`RISCVGen*.inc`）+ MC 验证 + lit，缺一层都不算“跑通”。
- `ALU_rr` 是 R 型最省事的模具：固定 `OPC_OP`、三个 `GPR`、汇编串和 `hasSideEffects=0`；你只填 `funct7`/`funct3`/助记符。
- `Predicates` 同时约束汇编器和 CodeGen；漏了它，最危险的失败是“CodeGen 生成了目标不支持的指令”，而不是汇编报错。
- `Pattern` 不是每条指令都必须有：没有 IR 形状的 MC-only 指令（如 `crc32`）不需要；有 IR 形状的（如 `ANDN`）才写 `Pat`。
- 编码最短验证是 `llvm-mc -show-encoding`；往返验证是 `-filetype=obj` + `llvm-objdump -d`；两者都不需要重编。
- 反汇编器不检查 feature predicate；不要用“objdump 能解”证明 feature 接对了。
- 测试比例要像上游：`.td` 几十行，测试与 feature 接线占大头；`git show --stat` 是最直接的对齐工具。
- 需要重编才能验证的步骤（新 opcode、新生成物、lit 全绿）必须标“未实测”，不要伪造日志。

## A.12 自我检核

- [ ] 能画出“一条指令穿过 7 道门”的图，并说出每道门对应哪个文件和哪个生成物
- [ ] 能解释 `RVInstR` 与 `ALU_rr` 的关系，以及 `hasSideEffects`/`isCommutable` 各自影响什么
- [ ] 能手算 `andn a0, a0, a1` 的 32 位值，并用 `llvm-mc -show-encoding` 对账
- [ ] 能写出 `crc32.w` 的 `def`，并说明为什么它不需要 `Pattern`
- [ ] 能区分“汇编器接受”“反汇编器能解”“CodeGen 能选”三件事，并各给一条命令
- [ ] 能写出 MC 正例/负例的 RUN 行，知道 `CHECK-ASM` 与 `CHECK-ASM-AND-OBJ` 各抓什么
- [ ] 能说出 `-M no-aliases` 为什么对稳定测试是必需的
- [ ] 知道 `update_llc_test_checks.py` 会重写整个文件，并会在 `/tmp` 副本上使用
- [ ] 能说出 `Predicates` 漏掉时，汇编器、CodeGen、反汇编器分别会/不会怎么暴露

---

## A.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZicond.td`**（63 行）
  - 读哪里：27–29 的 `CZERO_EQZ/CZERO_NEZ`，39–63 的 `Pat` 与 `CondOpsPat`。
  - 为什么读：feature 门控 + 两条 R 型 + pattern 的最小完整样本，与候选 1/2 同形。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZalasr.td`**（113 行）
  - 读哪里：19–42 的 `LAQ_r`/`SRL_r`/multiclass，49–60 的 `defm`。
  - 为什么读：候选 2 就是照这个文件补 `LBU/LHU/LWU`；同时能看到 `IsRV64` 与 `HasStdExtZalasr` 的叠加。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`**（869 行）
  - 读哪里：265–272 的 `ANDN/ORN/XNOR`，373–376 的 `Zbc` 块，481–488 的 pattern。
  - 为什么读：练习 A 的主线模板；`ANDN` 的 `def` 与 `Pat` 是“同一条指令的 MC 面与 CodeGen 面”。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/utils/update_llc_test_checks.py`**
  - 读哪里：`--llc-binary`、`--function`、`--enable` 的参数解析与 `run_llc`。
  - 为什么读：理解它会重写整个文件，避免在仓库测试上误跑。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/MC/RISCV/rv64zalasr-valid.s`**
  - 读哪里：1–8 的 RUN 行，以及 `CHECK-ASM`/`CHECK-ASM-AND-OBJ`/`CHECK-NO-EXT` 三组前缀。
  - 为什么读：新增 MC 测试最完整的骨架；正例、负例、feature 门控写在同一个文件里。

---

→ 下一章：[Ch 09 — 调用约定 lowering](./09-calling-convention-lowering.md)
