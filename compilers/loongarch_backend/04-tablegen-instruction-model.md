# Ch 04 — 指令的 TableGen 建模：`ADD_D` 是怎么被描述出来的

> 本章目标：把一条指令拆成三层描述：**格式**（32 位怎么摆）、**语义**（汇编怎么写、调度怎么写）、**pattern**（DAG 怎么选中它）。三层都对了，`llc` 才能把 `add i64` 变成 `add.d`。

---

## 4.1 一条指令的三层描述

以 `add.d $a0, $a1, $a2` 为例：

| 层 | 文件 | 内容 |
|---|---|---|
| 格式 | `LoongArchInstrFormats.td` | `Fmt3R`：opcode + `rk` + `rj` + `rd` 的位域 |
| 语义 | `LoongArchInstrInfo.td` | `ALU_3R<0x00108000>`：操作数、汇编串、调度 |
| pattern | 同文件 | `PatGprGpr<add, ADD_D>`：`ISD::ADD` → `ADD_D` |

三层的关系：

```text
ISD::ADD (DAG)
   │  PatGprGpr<add, ADD_D>
   ▼
ADD_D (MachineInstr)
   │  ALU_3R -> Fmt3R
   ▼
0x00108c84 (32-bit)
   │  MCCodeEmitter
   ▼
a4 98 10 00 (little-endian bytes)
```

---

## 4.2 `LAInst`：所有 LoongArch 指令的基类

`LoongArchInstrFormats.td`：

```tablegen
class LAInst<dag outs, dag ins, string opcstr, string opnstr,
             list<dag> pattern = []>
    : Instruction {
  field bits<32> Inst;

  let Namespace = "LoongArch";
  let Size = 4;
  let OutOperandList = outs;
  let InOperandList = ins;
  let AsmString = opcstr # "\t" # opnstr;
  let Pattern = pattern;

  bit IsSubjectToAMORdConstraint = 0;
  let TSFlags{0} = IsSubjectToAMORdConstraint;

  bit IsAMCAS = 0;
  let TSFlags{1} = IsAMCAS;
}
```

要点：

- **`field bits<32> Inst`** 是编码的“画布”；每个 `Fmt*` 类把自己那几位填进去。
- **`Size = 4`**：LoongArch 基础指令固定 4 字节（没有 RVC 那样的压缩指令）。
- **`TSFlags{0}`/`TSFlags{1}`** 是目标特有的 instruction flag，供 C++ 代码查询（`LoongArchII::isAMCAS()` 等）。
- **`Pattern`** 可以让指令自己带 pattern，但 LoongArch 更常用独立的 `def : Pat<...>`，便于复用 `PatGprGpr` 这类模板。

伪指令基类：

```tablegen
class Pseudo<dag outs, dag ins, list<dag> pattern = [], string opcstr = "",
             string opnstr = "">
    : LAInst<outs, ins, opcstr, opnstr, pattern> {
  let isPseudo = 1;
  let isCodeGenOnly = 1;
}
```

`PseudoLA_TLS_LE` 就是这一支；它不会出现在汇编里，必须在某个 pass 里被展开（Ch 18）。

---

## 4.3 `Fmt3R`：32 位怎么摆

```tablegen
class Fmt3R<bits<32> op, dag outs, dag ins, string opnstr,
            list<dag> pattern = []>
    : LAInst<outs, ins, deriveInsnMnemonic<NAME>.ret, opnstr, pattern> {
  bits<5> rk;
  bits<5> rj;
  bits<5> rd;

  let Inst{31-0} = op;
  let Inst{14-10} = rk;
  let Inst{9-5} = rj;
  let Inst{4-0} = rd;
}
```

位布局：

| 位 | 31–15 | 14–10 | 9–5 | 4–0 |
|---|---|---|---|---|
| 字段 | opcode 高位 | `rk` | `rj` | `rd` |
| `add.d $a0,$a1,$a2` | `0x00108000` | `6`（`$a2`） | `5`（`$a1`） | `4`（`$a0`） |

手算：

```text
0x00108000
| (rk << 10) = 6 << 10 = 0x00001800
| (rj << 5)  = 5 << 5  = 0x000000a0
| (rd)       = 4       = 0x00000004
= 0x001098a4
```

小端字节：

```text
a4 98 10 00
```

实测：

```bash
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding /tmp/enc.s
```

```text
add.d $a0, $a1, $a2   # encoding: [0xa4,0x98,0x10,0x00]
```

**注意 `deriveInsnMnemonic`**：

```tablegen
class deriveInsnMnemonic<string name> {
  string ret = !tolower(!subst("@", "_", !subst("_", ".", !subst("__", "@", name))));
}
```

它把 TableGen 的 `ADD_D` 变成汇编的 `add.d`：先保护 `__`，再把 `_` 换成 `.`，最后转小写。这就是为什么你不需要为每条指令手写 `AsmString` 的助记符部分。

---

## 4.4 `ALU_3R` 与 `ADD_D`

`LoongArchInstrInfo.td`：

```tablegen
class ALU_3R<bits<32> op>
    : Fmt3R<op, (outs GPR:$rd), (ins GPR:$rj, GPR:$rk), "$rd, $rj, $rk">;
```

`ADD_D`：

```tablegen
let Predicates = [IsLA64] in {
def ADD_D : ALU_3R<0x00108000>,
            Sched<[WriteIALU, ReadIALU, ReadIALU]>;
def SUB_D : ALU_3R<0x00118000>,
            Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

三件事同时发生：

1. **`IsLA64`**：只有 LA64 才能选/汇编这条指令。
2. **`ALU_3R<0x00108000>`**：操作数是 `rd, rj, rk`，汇编串是 `"$rd, $rj, $rk"`。
3. **`Sched<[WriteIALU, ReadIALU, ReadIALU]>`**：调度模型把它归到整数 ALU 的写/读资源（Ch 23）。

`ADDI_D` 是另一种格式：

```tablegen
def ADDI_D : ALU_2RI12<0x02c00000, simm12_addlike>,
             Sched<[WriteIALU, ReadIALU]>;
```

`ALU_2RI12` 的位域是 `rj` + 12 位立即数；`simm12_addlike` 是一个 `Operand`，它约束立即数的范围和打印方式。**指令格式不是随便选的：`addi.d` 只有 12 位立即数，超出范围就必须先物化到寄存器，或者用 `lu12i.w` 组合。**

---

## 4.5 pattern：DAG 怎么选中 `ADD_D`

`PatGprGpr`：

```tablegen
class PatGprGpr<SDPatternOperator OpNode, LAInst Inst>
    : Pat<(OpNode GPR:$rj, GPR:$rk), (Inst GPR:$rj, GPR:$rk)>;
```

`ADD_D` 的 pattern：

```tablegen
def : PatGprGpr<add, ADD_D>;
```

它等价于：

```tablegen
def : Pat<(add GPR:$rj, GPR:$rk), (ADD_D GPR:$rj, GPR:$rk)>;
```

匹配流程：

1. 合法化后的 DAG 有 `ISD::ADD` 节点，两个操作数都是 `GPR`（`i64`/`i32`）。
2. TableGen 生成的 matcher 在 `LoongArchGenDAGISel.inc` 里找到对应的 `OPC_MorphNodeToByHwMode`。
3. 生成 `ADD_D` 的 `MachineSDNode`。

实测生成物：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/llvm-tblgen -gen-dag-isel \
  -I llvm/lib/Target/LoongArch -I llvm/lib/Target -I llvm/include -I $T/include \
  llvm/lib/Target/LoongArch/LoongArch.td -o /tmp/LoongArchGenDAGISel.inc
wc -l /tmp/LoongArchGenDAGISel.inc
ls -lh /tmp/LoongArchGenDAGISel.inc
```

实测：

```text
56559 /tmp/LoongArchGenDAGISel.inc
-rw-rw-r-- 1 ubuntu ubuntu 4.1M ...
```

`ADD_D` 在表里的片段：

```text
/*  19056*/     OPC_MorphNodeToByHwMode, TARGET_VAL(LoongArch::ADD_D), 0,
                // Dst: (ADD_D:{ *:[i32] m1:[i64] } GPR:{ *:[i32] m1:[i64] }:$rj, GPR:{ *:[i32] m1:[i64] }:$rk)
```

`m1:[i64]` 是 `HwMode` 的编码：同一份 pattern 在 LA32/LA64 下映射到不同的值类型。`OPC_MorphNodeToByHwMode` 说明这条 pattern 受 `HwMode` 影响，不能简单当成“固定 opcode”。

---

## 4.6 不是所有节点都靠 pattern

有些操作没有一条指令能直接表达，或者需要特殊寄存器约束，就写成 `LoongArchISD::*` 自定义节点，并在 C++ 里 lower：

```tablegen
def loongarch_call : SDNode<"LoongArchISD::CALL", SDT_LoongArchCall,
                            [SDNPHasChain, SDNPOptInGlue, SDNPOutGlue,
                             SDNPVariadic]>;
def loongarch_selectcc : SDNode<"LoongArchISD::SELECT_CC", SDT_LoongArchSelectCC>;
def loongarch_bstrins : SDNode<"LoongArchISD::BSTRINS", SDT_LoongArchBStrIns>;
```

这些节点：

- 在 `LowerOperation` 里被创建（Ch 15）；
- 在 `PerformDAGCombine` 里被优化（Ch 16）；
- 最后由 `.td` 的 pattern 或 `LoongArchISelDAGToDAG::Select` 选中（Ch 14）。

**判断该用哪种方式**：

| 情况 | 方式 |
|---|---|
| 一条指令能直接表达，操作数是普通寄存器/立即数 | `Pat<...>` |
| 需要 C++ 计算地址、拆分立即数、检查特征 | `ComplexPattern` + `Select` |
| 需要多指令序列、伪指令、特殊 ABI | `LoongArchISD::*` + `LowerOperation` |
| 只影响调度/编码，不影响选择 | 只改 `Sched<>`/`Fmt*` |

---

## 4.7 生成物地图

`llvm-tblgen` 从 `LoongArch.td` 生成的文件（在构建目录 `lib/Target/LoongArch/`）：

| 生成物 | generator | 内容 | 规模（本树） |
|---|---|---|---|
| `LoongArchGenDAGISel.inc` | `-gen-dag-isel` | DAG matcher 表 | 56,559 行 / 4.1 MB |
| `LoongArchGenInstrInfo.inc` | `-gen-instr-info` | opcode 枚举、指令描述 | 636 KB |
| `LoongArchGenRegisterInfo.inc` | `-gen-register-info` | 寄存器/寄存器类/CSR | 见构建目录 |
| `LoongArchGenSubtargetInfo.inc` | `-gen-subtarget` | Feature/CPU 解析 | 见构建目录 |
| `LoongArchGenAsmWriter.inc` | `-gen-asm-writer` | InstPrinter | 见构建目录 |
| `LoongArchGenAsmMatcher.inc` | `-gen-asm-matcher` | 汇编器匹配表 | 见构建目录 |
| `LoongArchGenDisassemblerTables.inc` | `-gen-disassembler` | 反汇编表 | 见构建目录 |
| `LoongArchGenMCPseudoLowering.inc` | `-gen-mc-pseudo-lowering` | MC 层伪指令展开 | 见构建目录 |

实测 `ADD_D` 的 opcode 编号：

```text
ADD_D = 456, // LoongArchInstrInfo.td:1075
```

这个编号是**生成的 enum**，不是 ISA 规定的；不要把它写死到测试或外部工具里。

---

## 4.8 从 `add` 到 `ADD_D` 的完整追踪

```bash
# 1. 看 DAG 构建后的节点
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -print-before=loongarch-isel /tmp/la-smoke.ll -o /dev/null 2>&1 | grep -A8 'add'

# 2. 看选指后的机器指令
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -stop-after=loongarch-isel /tmp/la-smoke.ll -o - 2>/dev/null | grep ADD_D

# 3. 看最终编码
$T/bin/llvm-mc -triple=loongarch64-unknown-linux-gnu -show-encoding /tmp/one.s
```

实测第 2 步：

```text
%2:gpr = ADD_D %0:gpr, %1:gpr
```

第 3 步：

```text
add.d $a0, $a1, $a2   # encoding: [0xa4,0x98,0x10,0x00]
```

---

## 4.9 加一条新指令时要改哪里（练习 A 的预览）

以“加一条 `add.d` 的变体”为例：

1. **格式**：如果新指令的位域和 `Fmt3R` 一样，直接复用；否则在 `LoongArchInstrFormats.td` 加 `Fmt*`。
2. **指令**：在 `LoongArchInstrInfo.td` 加 `def`，给 opcode、操作数、`Sched<>`、`Predicates`。
3. **pattern**：如果希望选指用它，加 `Pat<...>` 或 `PatGprGpr<...>`。
4. **编码**：`MCCodeEmitter` 通常不用改；如果操作数是特殊类型，可能要加 `getImmOpValue` 分支。
5. **汇编器**：`AsmMatcher` 会自动生成；如果助记符/操作数有歧义，可能要改 `AsmParser`。
6. **测试**：加 `llvm/test/MC/LoongArch/...` 和 `llvm/test/CodeGen/LoongArch/...`。

练习 A 会带你实际做一遍，并解释“为什么改了 `.td` 必须重编 `llc` 和 `llvm-mc`”。

---

## 4.10 反直觉点

1. **opcode 是自动编号的**：在 `.td` 中间插入一个 `def`，后面所有指令的 opcode 会整体后移。练习 A 实测新指令 `add.du` 的 opcode 是 457，就是这种平移的结果。
2. **不是所有 DAG 节点都靠 pattern**：复杂寻址、多指令序列、特殊 ABI 走 `LoongArchISD::*` + `LowerOperation`（4.6）。
3. **漏写 `Sched<>` 不会编译失败**：`CompleteModel = 0` 时只是静默降级，性能问题不会在测试里暴露（Ch 23）。
4. **编码不是 pattern 生成的**：pattern 只决定“选哪条指令”，32 位编码由 `Fmt*` 的位域加 `MCCodeEmitter` 拼出来（4.1）。
5. **同一个助记符可以有多个 `def`**（别名/不同操作数形式），读 `.td` 时不能假设“一助记符一指令”。
6. **pattern 匹配失败不一定报错**：可能落到 `Select()` 里的 `ComplexPattern` 分支，或者被 `LowerOperation` 提前改写。

---

## 4.11 本章证据

| 结论 | 命令/源码 |
|---|---|
| `LAInst` 字段 | `LoongArchInstrFormats.td` |
| `Fmt3R` 位域 | 同上 |
| `ADD_D` 定义 | `LoongArchInstrInfo.td:1075` |
| `PatGprGpr` | `LoongArchInstrInfo.td:1343` |
| DAGISel 表规模 | `llvm-tblgen -gen-dag-isel` + `wc -l` |
| `ADD_D` opcode | `LoongArchGenInstrInfo.inc:471` |
| 编码 | `llvm-mc -show-encoding` |

---

## 4.12 练习

1. 手算 `sub.d $a3,$a4,$a5` 的 32 位编码，再用 `llvm-mc -show-encoding` 验证。
2. 在 `/tmp` 里复制一份 `LoongArchInstrInfo.td`，把 `ADD_D` 的 opcode 改成 `0x00108001`，重新生成 `-gen-instr-info`，观察 `ADD_D` 的 opcode 编号和编码是否变化（不要改真实树）。
3. 找出 `ADDI_D` 的 pattern（`PatGprImm<add, ADDI_D, simm12>`），解释为什么 `add i64 %a, 100` 选 `ADDI_D`，而 `add i64 %a, 100000` 可能选 `LU12I_W + ADD_D`。
4. 用 `-print-before=loongarch-isel` 看 `ISD::ADD` 的操作数类型，确认 `GPR` 在 LA32/LA64 下分别是 `i32`/`i64`。

---

## 4.13 下一步

下一章看寄存器与调用约定：`GPR`/`FPR`/`CFR` 的层级、callee-saved 集合，以及它们如何决定 prologue 里存哪些寄存器。
