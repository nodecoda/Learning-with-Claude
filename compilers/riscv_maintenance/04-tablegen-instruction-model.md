# Ch 04 — 指令的 TableGen 建模：格式、扩展文件与 P-ext 案例

> **目标**：读懂 `RISCVInstrFormats.td` 与 `RISCVInstrInfo*.td` 的组织方式；能从一条指令的 `def` 推出它的编码字段；能判断新增一条指令需要改哪些文件、会触发哪些生成物。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，HEAD `0fc5afa04c23`。

---

## 4.1 为什么维护者要读 TableGen

RISC-V 后端的指令表完全由 TableGen 生成：编码、汇编器匹配表、反汇编器解码表、DAG 选择表、MC 指令描述、调度类，全部来自 `.td`。这意味着：

- **改一条指令的编码，不需要碰 C++**，改 `funct3`/`funct7` 即可；
- **加一条指令，也不需要写匹配代码**，加 `def` + `Pat<>` 即可；
- **但任何字段写错，错误会在生成物里放大**：汇编器、反汇编器、CodeGen 会同时错，而且报错位置往往离 `.td` 很远。

上游最近 12 个月，`RISCVInstrInfoP.td` 被改 **184 次**，`RISCVInstrInfo.td` 65 次。P-ext 是这个 trunk 最活跃的 TableGen 战场。

---

## 4.2 先建立直觉：class 是模具，def 是零件

```tablegen
class ALU_rr<bits<7> funct7, bits<3> funct3, string opcodestr,
             bit Commutable = 0>
    : RVInstR<funct7, funct3, OPC_OP, (outs GPR:$rd), (ins GPR:$rs1, GPR:$rs2),
              opcodestr, "$rd, $rs1, $rs2"> {
  let isCommutable = Commutable;
}

def ADD : ALU_rr<0b0000000, 0b000, "add", Commutable=1>,
          Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

读法：

- `ALU_rr` 是模具：它固定了“R 型、GPR 输出、两个 GPR 输入、汇编串 `$rd, $rs1, $rs2`”；
- `ADD` 是零件：只填 `funct7=0`、`funct3=0`、名字 `add`；
- `Sched<...>` 是调度信息，不是编码；
- `Commutable=1` 让机器层可以交换 `rs1`/`rs2`。

同样，`ADDI` 用的是 `ALU_ri`：

```tablegen
let isReMaterializable = 1, isAsCheapAsAMove = 1 in
def ADDI  : ALU_ri<0b000, "addi">;
```

`ALU_ri` 固定了“I 型、GPR 输出、GPR + `simm12_lo` 输入”。

---

## 4.3 格式类：bits 是怎么落到 32 位上的

`RISCVInstrFormats.td` 定义了几十个格式类。最小的是：

```tablegen
class RVInst<dag outs, dag ins, string opcodestr, string argstr,
             list<dag> pattern, InstFormat format>
    : RVInstCommon<outs, ins, opcodestr, argstr, pattern, format> {
  field bits<32> Inst;
  let Size = 4;
}
```

`field bits<32> Inst` 就是最终编码。R 型在 `RVInstR` 里继续切：

```tablegen
class RVInstR<bits<7> funct7, bits<3> funct3, RISCVOpcode opcode, dag outs,
              dag ins, string opcodestr, string argstr>
    : RVInstRBase<funct3, opcode, outs, ins, opcodestr, argstr> {
  let Inst{31-25} = funct7;
}
```

`RVInstRBase` 负责 `funct3`、`opcode`、`rd`、`rs1`、`rs2`；`RVInstR` 只补 `funct7`。I 型则用 `RVInstI`（467 行），把 `imm[11:0]` 放到 `Inst{31-20}`。

RISC-V 的 opcode 本身也是 TableGen 记录（`RISCVInstrFormats.td:141-149`）：

```tablegen
def OPC_OP_IMM    : RISCVOpcode<"OP_IMM",    0b0010011>;
def OPC_OP_IMM_32 : RISCVOpcode<"OP_IMM_32", 0b0011011>;
def OPC_OP        : RISCVOpcode<"OP",        0b0110011>;
```

---

## 4.4 手算三条指令的编码

### 4.4.1 `ADD`

`ADD = ALU_rr<0b0000000, 0b000, "add">`，对应：

```bash
printf 'add a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
```

实测：

```text
add a0, a0, a1    # encoding: [0x33,0x05,0xb5,0x00]
```

小端字节拼成 32 位是 `0x00B50533`。按 R 型拆：

| 字段 | 位 | 值 | 含义 |
|---|---|---|---|
| `funct7` | 31–25 | `0000000` | ADD 的 funct7 |
| `rs2` | 24–20 | `01011` | `x11`（`a1`） |
| `rs1` | 19–15 | `01010` | `x10`（`a0`） |
| `funct3` | 14–12 | `000` | ADD 的 funct3 |
| `rd` | 11–7 | `01010` | `x10`（`a0`） |
| `opcode` | 6–0 | `0110011` | `OPC_OP` |

### 4.4.2 `ADDI`

`ADDI = ALU_ri<0b000, "addi">`，编码：

```text
addi a0, a0, 1    # encoding: [0x13,0x05,0x15,0x00]
```

`0x00150513`，按 I 型拆：

| 字段 | 位 | 值 |
|---|---|---|
| `imm[11:0]` | 31–20 | `000000000001` |
| `rs1` | 19–15 | `01010` |
| `funct3` | 14–12 | `000` |
| `rd` | 11–7 | `01010` |
| `opcode` | 6–0 | `0010011`（`OPC_OP_IMM`） |

### 4.4.3 `SH3ADD`

`SH3ADD` 来自 Zba，funct7=`0010000`，funct3=`110`：

```text
sh3add a0, a0, a1 # encoding: [0x33,0x65,0xb5,0x20]
```

`0x20B56533`：

| 字段 | 位 | 值 |
|---|---|---|
| `funct7` | 31–25 | `0010000` |
| `rs2` | 24–20 | `01011` |
| `rs1` | 19–15 | `01010` |
| `funct3` | 14–12 | `110` |
| `rd` | 11–7 | `01010` |
| `opcode` | 6–0 | `0110011` |

三条指令的 opcode 相同或相邻，区别全在 `funct7`/`funct3`/`imm`。这就是 RISC-V 指令表能用一个 `ALU_rr` 模具批量生成的原因。

---

## 4.5 文件组织：谁 include 谁

`RISCV.td` 是总入口（111 行），按固定顺序 include：

```tablegen
// 1. features / profiles / system operands
// 2. register info / schedule / calling conv
// 3. instruction info（主文件）
// 4. GISel banks / predicates / macro fusion
// 5. 各 CPU 调度模型 / processors
```

指令文件的入口是 `RISCVInstrInfo.td`（2,553 行），它在 2,463–2,535 行按扩展 include：

```tablegen
// Multiply and Division
include "RISCVInstrInfoM.td"
// Atomic
include "RISCVInstrInfoA.td"
include "RISCVInstrInfoZa.td"
...
// Scalar FP
include "RISCVInstrInfoF.td"
include "RISCVInstrInfoD.td"
...
// Scalar bitmanip and cryptography
include "RISCVInstrInfoZb.td"
include "RISCVInstrInfoZk.td"
// Vector
include "RISCVInstrInfoV.td"
...
// Packed SIMD
include "RISCVInstrInfoP.td"
// Compressed
include "RISCVInstrInfoC.td"
...
// Vendor
include "RISCVInstrInfoXTHead.td"
include "RISCVInstrInfoXSf.td"
...
```

### 4.5.1 include 顺序决定 opcode 编号

生成的 `RISCVGenInstrInfo.inc` 里，编号是按 include/def 顺序分配的：

```text
    ABSW    = 13773, // RISCVInstrInfoP.td:577
    ADD     = 13774, // RISCVInstrInfo.td:915
    ADDD    = 13775, // RISCVInstrInfoP.td:1476
    ADDI    = 13776, // RISCVInstrInfo.td:894
    ADDIW   = 13777, // RISCVInstrInfo.td:1008
    ADDW    = 13778, // RISCVInstrInfo.td:1020
    ADD_UW  = 13779, // RISCVInstrInfoZb.td:285
```

注意 `ADDD` 夹在 `ADD` 和 `ADDI` 之间，因为它来自 `RISCVInstrInfoP.td`，而 P 文件是在 `RISCVInstrInfo.td` 之后 include 的。**opcode 编号不是 ABI，不要依赖它的稳定性**；它每次构建都可能变。

### 4.5.2 生成物规模

```bash
ls -lS "$T"/lib/Target/RISCV/RISCVGen*.inc | head
```

实测（字节）：

| 文件 | 大小 | 内容 |
|---|---:|---|
| `RISCVGenDAGISel.inc` | 61,114,337 | SelectionDAG 匹配表（约 61 MB） |
| `RISCVGenGlobalISel.inc` | 45,550,453 | GlobalISel 匹配表（约 45 MB） |
| `RISCVGenSubtargetInfo.inc` | 28,463,803 | feature/CPU 解析表 |
| `RISCVGenInstrInfo.inc` | 7,017,624 | 指令枚举与 MC 描述 |
| `RISCVGenSearchableTables.inc` | 2,495,224 | 可搜索表 |
| `RISCVGenAsmWriter.inc` | 1,945,886 | 汇编打印 |
| `RISCVGenDisassemblerTables.inc` | 1,507,441 | 反汇编解码 |
| `RISCVGenAsmMatcher.inc` | 1,110,001 | 汇编器匹配 |

`RISCVGenDAGISel.inc` 有 61 MB，这解释了为什么改一行 `.td` 后重新编译 `RISCVISelDAGToDAG.cpp` 可能要几十秒：它在重新编译一个巨大的 `switch`。

---

## 4.6 predicate：指令什么时候可用

`Predicates` 是 TableGen 的可用性条件，最终同时影响 Assembler 与 CodeGen。

```tablegen
let Predicates = [HasStdExtZba] in {
def SH3ADD : ALU_rr<0b0010000, 0b110, "sh3add">,
             Sched<[WriteSHXADD, ReadSHXADD, ReadSHXADD]>;
...
} // Predicates = [HasStdExtZba]
```

常见 predicate：

| predicate | 含义 | 典型用法 |
|---|---|---|
| `HasStdExtZba` | 有 Zba | `SH3ADD` |
| `IsRV64` | XLEN=64 | `ADD_UW`、`SLLI_UW` |
| `NoStdExtZba` | 没有 Zba | 退化 pattern |
| `HasStdExtV` | 有 V | 所有 RVV 指令 |
| `IsRV32` | XLEN=32 | RV32 专属 |

维护提示：

- **Assembler 与 CodeGen 用的是同一个 predicate**，但错误的报错位置不同：`llvm-mc` 会说 “instruction requires the following: 'Zba'”，`llc` 则可能静默选不到 pattern。
- **`let Predicates` 可以嵌套**：外层 `HasStdExtZba`，内层 `IsRV64`，`SH3ADD` 就要求两者同时成立。
- **不要用 `Predicates` 表达“优化更好”**，它只表达“合法/可用”；性能选择应该在 pattern 优先级或 C++ combine 里做。

---

## 4.7 新增一条指令的最小闭环

假设你要加一条 Zba 风格的 `sh4add`（不存在，仅作示例）。

### 4.7.1 指令 def

在 `RISCVInstrInfoZb.td` 的 `HasStdExtZba` 块里加：

```tablegen
def SH4ADD : ALU_rr<0b0010001, 0b110, "sh4add">,
             Sched<[WriteSHXADD, ReadSHXADD, ReadSHXADD]>;
```

### 4.7.2 pattern（如果要 CodeGen 自动选）

```tablegen
def : Pat<(add (shl GPR:$rs1, 4), GPR:$rs2),
          (SH4ADD GPR:$rs1, GPR:$rs2)>;
```

### 4.7.3 汇编器测试

`llvm/test/MC/RISCV/rv64zba-valid.s`：

```asm
# RUN: llvm-mc %s -triple=riscv64 -mattr=+zba -show-encoding \
# RUN:   | FileCheck -check-prefixes=CHECK-ENCODING,CHECK-INST %s
sh4add a0, a0, a1
# CHECK-INST: sh4add a0, a0, a1
# CHECK-ENCODING: encoding: [0x33,0x65,0xb5,0x22]
```

（编码字节必须用 `llvm-mc -show-encoding` 重新生成，不要手算。）

### 4.7.4 反汇编测试

`rv64zba-invalid.s` 或 `*-valid.s` 的 `CHECK-OBJ` 分支：

```asm
# RUN: llvm-mc -filetype=obj -triple=riscv64 -mattr=+zba < %s \
# RUN:   | llvm-objdump -d - | FileCheck %s
```

### 4.7.5 CodeGen 测试

`llvm/test/CodeGen/RISCV/rv64zba.ll` 里加：

```llvm
; RUN: llc -mtriple=riscv64 -mattr=+zba < %s | FileCheck %s
define i64 @sh4add(i64 %x, i64 %y) {
  %s = shl i64 %x, 4
  %r = add i64 %s, %y
  ret i64 %r
}
; CHECK-LABEL: sh4add:
; CHECK: sh4add a0, a0, a1
```

### 4.7.6 属性测试

`llvm/test/CodeGen/RISCV/attributes.ll` 会检查 `.attribute arch`。如果扩展名/版本变了，这里要更新。

### 4.7.7 重新生成与验证

```bash
cd "$T"
ninja -j24 llc llvm-mc llvm-tblgen
```

如果只改了 `.td`，`ninja` 会自动重跑 `llvm-tblgen`；通常 1–3 分钟。然后用：

```bash
$T/bin/llvm-lit -sv llvm/test/MC/RISCV/rv64zba-valid.s \
  llvm/test/CodeGen/RISCV/rv64zba.ll
```

---

## 4.8 踩雷集锦

1. **编码字段写错，汇编器和反汇编器会同时错。** 先用 `llvm-mc -show-encoding` 验证，再写测试。
2. **`Predicates` 漏写，`-mattr=-zba` 也能汇编。** 一定要加 negative test。
3. **`Sched` 漏写，`llvm-mca` 会报 “no scheduling information”。** 可以暂时用 `NoSched`，但上游 review 会要求补。
4. **`isCommutable` 设错，机器层会错误交换操作数。** 只有真正满足交换律的指令才能设。
5. **`Inst{31-25}` 与 `Inst{31-20}` 混用。** R 型用前者，I 型用后者；写反了编码会错但汇编器不一定报错。
6. **`def` 名字与 `opcodestr` 不一致。** 生成的 C++ 符号用 `def` 名，汇编文本用 `opcodestr`；两者不要求相同，但维护时会很痛苦。
7. **忘记更新 `RISCVGenInstrInfo.inc` 的依赖。** 只改 `.td` 不重编，`llc` 还是旧表；`ninja` 的 depfile 通常能处理，但跨目录复制 `.td` 时容易漏。
8. **把 opcode 编号写进测试。** 编号会随 include 顺序变化；测试里只应出现指令名或编码字节。

---

## 4.9 可跑实验

> 以下三条命令都在本机 `builds/ct-instr-trunk`（HEAD `0fc5afa04c23`）真跑，输出原样粘贴。

### 实验 1：手算 vs 工具（实测）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
printf 'add a0, a0, a1\naddi a0, a0, 1\nsh3add a0, a0, a1\n' \
  | $T/bin/llvm-mc -triple=riscv64 -mattr=+zba -show-encoding
```

实测输出：

```text
	add	a0, a0, a1                      # encoding: [0x33,0x05,0xb5,0x00]
	addi	a0, a0, 1                       # encoding: [0x13,0x05,0x15,0x00]
	sh3add	a0, a0, a1                      # encoding: [0x33,0x65,0xb5,0x20]
```

小端拼成 32 位再手拆：

- `ADD`：`0x00b50533` → opcode `0110011`、rd `a0=10`、funct3 `000`、rs1 `a0=10`、rs2 `a1=11`、funct7 `0000000`。
- `ADDI`：`0x00150513` → opcode `0010011`、rd `10`、funct3 `000`、rs1 `10`、imm `1`。
- `SH3ADD`：`0x20b56533` → opcode `0110011`、funct3 `110`、funct7 `0010000`，正是 `RISCVInstrInfoZb.td:279` 的 `ALU_rr<0b0010000, 0b110, "sh3add">`。

做三次，你就能不查表拆 R/I 型。

### 实验 2：找到一条指令的 def 与生成编号（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
grep -rn "def SH3ADD" llvm/lib/Target/RISCV/
grep -n "ADDI " "$T/lib/Target/RISCV/RISCVGenInstrInfo.inc" | head -2
```

实测输出：

```text
llvm/lib/Target/RISCV/RISCVInstrInfoZb.td:279:def SH3ADD : ALU_rr<0b0010000, 0b110, "sh3add">,
llvm/lib/Target/RISCV/RISCVInstrInfoZb.td:291:def SH3ADD_UW : ALUW_rr<0b0010000, 0b110, "sh3add.uw">,
...
    ADDI                                       = 13776, // RISCVInstrInfo.td:894
```

注意 `SH3ADD_UW` 复用同一个 `funct7`/`funct3`，只换 opcode 字段（`ALUW_rr` 是 RV64 专属的 `OP-32`），这是"模具复用"的典型。

### 实验 3：观察 predicate 的效果（实测）

```bash
printf 'sh3add a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -show-encoding
printf 'sh3add a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv64 -mattr=+zba -show-encoding
printf 'sh3add a0, a0, a1\n' | $T/bin/llvm-mc -triple=riscv32 -mattr=+zba -show-encoding
printf 'add.uw a0, a0, a1\n'   | $T/bin/llvm-mc -triple=riscv32 -mattr=+zba -show-encoding
```

实测输出（第 1、4 条故意失败）：

```text
<stdin>:1:1: error: instruction requires the following: 'Zba' (Address Generation Instructions)
sh3add a0, a0, a1
^
	sh3add	a0, a0, a1                      # encoding: [0x33,0x65,0xb5,0x20]
	sh3add	a0, a0, a1                      # encoding: [0x33,0x65,0xb5,0x20]
<stdin>:1:1: error: instruction requires the following: RV64I Base Instruction Set
add.uw a0, a0, a1
^
```

三条结论：

1. 没有 `-mattr=+zba` 时汇编器直接拒绝，说明 `Predicates` 同时约束汇编器；
2. `SH3ADD` 在 RV32 与 RV64 上都可用（`ALU_rr` 不是 RV64 专属）；
3. `ADD.UW` 在 RV32 上报 "RV64I Base Instruction Set"，因为它挂在 `ALUW_rr` 模具下。

---

## 4.10 源码落点

| 内容 | 路径 | 关键行/符号 |
|---|---|---|
| 总入口 | `llvm/lib/Target/RISCV/RISCV.td` | 1–88 的 include 顺序 |
| 格式基类 | `llvm/lib/Target/RISCV/RISCVInstrFormats.td` | `RVInst` 306；`RVInstR` 387；`RVInstI` 467；`RVInstIShift` 475；`OPC_OP` 149 |
| ALU 模具 | `llvm/lib/Target/RISCV/RISCVInstrInfo.td` | `ALU_ri` 783；`ALU_rr` 795 |
| ADD/ADDI | 同上 | `ADDI` 894；`ADD` 915 |
| 扩展 include | 同上 | 2463–2535 |
| Zba 指令 | `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td` | `Predicates` 274；`SH3ADD` 279 |
| P-ext | `llvm/lib/Target/RISCV/RISCVInstrInfoP.td` | 3,859 行；实验性扩展 |
| 生成物 | `builds/ct-instr-trunk/lib/Target/RISCV/RISCVGen*.inc` | 见 4.5.2 |
| 指令枚举 | `RISCVGenInstrInfo.inc` | `ADDI = 13776` |

---

## 4.11 本章重点整理

- `.td` 是 RISC-V 指令表的唯一来源；C++ 只负责“什么时候用哪条”。
- `RVInst` 的 `field bits<32> Inst` 就是最终编码；R/I/S/B/U/J 型只是不同的位切片。
- `ALU_rr`/`ALU_ri` 这类模具把格式固定，`def` 只填 funct7/funct3/助记符。
- `Predicates` 同时约束汇编器与 CodeGen；新增指令必须补 positive/negative 两类测试。
- 生成物很大（DAGISel 61 MB），改 `.td` 的编译成本主要在这里。
- opcode 编号不稳定，测试里不要写死编号。

## 4.12 自我检核

- [ ] 能从 `ADD` 的 `def` 推出它的 6 个编码字段
- [ ] 能说出 `ALU_rr` 与 `ALU_ri` 的区别，以及各自对应哪种格式
- [ ] 知道 `RISCVInstrInfo.td` 按什么顺序 include 扩展文件
- [ ] 能解释为什么 `ADDD` 的 opcode 编号夹在 `ADD` 和 `ADDI` 之间
- [ ] 能列出新增一条指令至少需要改哪些测试
- [ ] 知道 `HasStdExtZba` 与 `NoStdExtZba` 各自用在哪里

---

## 4.13 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVInstrFormats.td`**（本树，703 行）
  - **读哪里**：306–500 行的 `RVInst`/`RVInstR`/`RVInstI`；141–149 行的 opcode。
  - **为什么读**：所有编码错误的根都在这里；读懂位切片，才能手算编码。
- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**（本树，2,553 行）
  - **读哪里**：740–850 行的 ALU 模具；2463–2535 行的 include 列表。
  - **为什么读**：它是指令文件的总入口，决定哪些扩展参与生成。
- **`llvm/lib/Target/RISCV/RISCVInstrInfoP.td`**（本树，3,859 行）
  - **读哪里**：1–20 行的实验性声明；18–130 行的操作数；130–559 行的模板。
  - **为什么读**：P-ext 是当前最活跃的 TableGen 案例，适合观察“一个新扩展如何落地”。
- **`llvm/docs/TableGen/`**（本树）
  - **读哪里**：`Backends.md` 的指令编码与 DAG ISel 生成；`LangRef.md` 的 `class`/`def`/`let`。
  - **为什么读**：把本章的 RISC-V 实例放回 TableGen 通用语义。
- **`llvm/test/MC/RISCV/rv64zba-valid.s`** 与 `llvm/test/CodeGen/RISCV/rv64zba.ll`
  - **读哪里**：RUN 行与 CHECK 前缀。
  - **为什么读**：这是新增指令时最直接的测试模板。

---

→ 下一章：[Ch 05 — 寄存器、寄存器类与调用约定](./05-registers-and-calling-convention.md)
