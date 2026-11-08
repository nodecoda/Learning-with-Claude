# Ch 14 — SelectionDAG 选指与 pattern

> **目标**：读懂一个合法的 DAG 节点如何被匹配成一条 `MachineInstr`；能判断一个 `Pat<>` 为什么不生效，以及该把逻辑写在 TableGen 还是 `Select()` 里。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（HEAD `0fc5afa04c23`，只读），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（assertions ON）。

> **前置**：Ch 04（`def`/`class`/编码字段）、Ch 13（Legalize 之后 DAG 上还剩哪些节点）。本章假设 DAG 已经合法。

---

## 14.1 为什么维护者要单独读选指层

一条指令选错，症状只有一句“汇编不对”，根因却可能在四个地方：

| 症状 | 真正的位置 | 先看什么 |
|---|---|---|
| 该出 `sh3add`，出了 `slli`+`add` | pattern 的 predicate 或形状 | `RISCVInstrInfoZb.td` 的 `ShxAddPat` |
| 该出 `addi`，出了 `lui`+`addi` | lowering / combine 改了 DAG | Optimized legalized DAG |
| 该出 `seqz`，出了三条指令 | 手写 `Select` 的 custom 分支 | `RISCVISelDAGToDAG.cpp:4173` |
| 新加的 `Pat<>` 完全不触发 | 节点在匹配前被 Preprocess 改写 | `RISCVISelDAGToDAG.cpp:35` |

把这四类混成“pattern 没写对”，是维护中最常见的误判。本章给每一类找可观察的证据。

代价也在这里：DAG 选择器是本后端最大的单个生成物。

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
stat -c %s $T/lib/Target/RISCV/RISCVGenDAGISel.inc
wc -l $T/lib/Target/RISCV/RISCVGenDAGISel.inc
grep -c "// Src:" $T/lib/Target/RISCV/RISCVGenDAGISel.inc
```

实测（2026-11-08）：

```text
61114337
680322
42558
```

61,114,337 字节（`du -h` 显示 59M，按 1 MiB 计），68 万行，42,558 条 pattern 记录。同目录第二大的 `RISCVGenGlobalISel.inc` 是 44M，`RISCVGenInstrInfo.inc` 只有 6.7M。

成本来自四处：每条 `Pat<>` 展开成一段字节码加一对 `// Src:`/`// Dst:` 注释；同一形状按 `i32`/`i64` 两个 HwMode 各生成一份；42,558 条记录内联了 36,980 次 `OPC_CheckPatternPredicate`；表是线性字节数组，不是压缩自动机。这也是改一个 `.td` 要等一会儿的主因。

---

## 14.2 先建立直觉：一次匹配的全过程

```
  IR:  %r = add i64 %a, %b
            │
            ▼
  Legalize + DAGCombine
            │
            ▼
  Legal DAG:   t5: i64 = add t2, t4
            │
            ▼
  PreprocessISelDAG()   反序遍历，先改写 pattern 不好处理的节点
  · SPLAT_VECTOR → VMV_V_X_VL
  · FP_EXTEND    → FP_EXTEND_VL
  · (add X, C)   → (sub X, -C)（有条件）
            │
            ▼
  AssignTopologicalOrder()，从 root 往 entry 走
            ▼
  Select(Node)
   ├─ isMachineOpcode()? → 已选过，返回
   ├─ switch 命中 custom 分支? → 自己造 MachineNode，否则 break
   │    ISD::Constant / SHL / OR / LOAD / RISCVISD::ADDD ...
   └─ SelectCode(Node)：MatcherTable 字节码
        OPC_SwitchOpcode → OPC_Scope → OPC_CheckPatternPredicate
        → OPC_MorphNodeTo → OPC_CompleteMatch
            │
            ▼
  MachineSDNode:  ADD t2, t4
```

三个关键点：**Preprocess 在匹配之前**，能抢走节点；**custom `switch` 在 `SelectCode` 之前**，手写逻辑也能抢走节点；`SelectCode` 是**带回溯的字节码解释器**，第一条完整匹配胜出。

## 14.3 生成物：`RISCVGenDAGISel.inc` 里的三样东西

```cpp
#define DEBUG_TYPE "riscv-isel"
#define GET_DAGISEL_BODY RISCVDAGToDAGISel
#include "RISCVGenDAGISel.inc"
```

| 产出 | 位置 | 作用 |
|---|---|---|
| `MatcherTable[]` | 第 58 行起 | 全部 pattern 的字节码，1,225,298 字节（约 1.2 MB） |
| `SelectCode(SDNode*)` | 第 51 行 | 把 `Node` 和表交给 `SelectCodeCommon` |
| `OPC_*` 枚举 | `llvm/include/llvm/CodeGen/SelectionDAGISel.h:351` | 字节码指令集，所有后端共用 |

`SelectCode` 的控制流只有十几行，其余 67 万行都是表数据；最后一句是 `SelectCodeCommon(N, MatcherTable, sizeof(MatcherTable), OperandLists);`。真正的解释器在 `SelectionDAGISel.cpp:3331`：按字节读表，`OPC_SwitchOpcode` 按根 opcode 分派，`OPC_Scope` 记录失败后的回跳点，`OPC_MorphNodeTo` 造机器节点，`OPC_CompleteMatch` 结束。

表的第一行就是根分派：

```text
/*      0*/ OPC_SwitchOpcode /*554 cases */, 16|128,84/*10768*/, TARGET_VAL(ISD::OR), // ->10773
```

554 个 case 覆盖所有参与匹配的根 opcode。`ISD::ADD` 的入口在偏移 24564（`grep -n "TARGET_VAL(ISD::ADD)" RISCVGenDAGISel.inc | head -1` 得到第 12384 行），向后 5 个字节就是 ADD 的作用域起点 24569。表尾的直方图说明这 61 MB 花在哪：

```text
  // #OPC_Scope                                = 11395
  // #OPC_CheckPatternPredicate                = 36980
  // #OPC_CheckComplexPat                      = 35434
  // #OPC_MorphNodeTo                          = 42555
```

## 14.4 TableGen 侧：`Pat<>`、`ComplexPattern`、`Predicates`

### 一个 `Pat<>` 就是一对 DAG 模板

```tablegen
def : PatGprGpr<add, ADD>;      // RISCVInstrInfo.td:1537
```

它展开成 `Pat<(vt (OpNode (vt GPR:$rs1), (vt GPR:$rs2))), (Inst GPR:$rs1, GPR:$rs2)>`（类定义在 1454）。读法固定：`Pat<源形状, 目标指令>`，`$rs1`/`$rs2` 是绑定变量，源和目标按名字对应。带立即数的版本是 `PatGprSimm12<add, ADDI>`（1538），所以 `add` 有两条 pattern：一条抢寄存器，一条抢 12 位立即数。

### 一个节点可能有多个“形状”

Zba 的 `SH3ADD` 有三条 pattern（`RISCVInstrInfoZb.td:724`）：

```tablegen
multiclass ShxAddPat<int i, Instruction shxadd> {
  def : Pat<(XLenVT (riscv_add_like_non_imm12 (shl GPR:$rs1, (XLenVT i)), GPR:$rs2)),
            (shxadd GPR:$rs1, GPR:$rs2)>;
  def : Pat<(XLenVT (riscv_shl_add GPR:$rs1, (XLenVT i), GPR:$rs2)),
            (shxadd GPR:$rs1, GPR:$rs2)>;
  defvar pat = !cast<ComplexPattern>("sh"#i#"add_op");
  def : Pat<(XLenVT (riscv_add_like_non_imm12 pat:$rs1, GPR:$rs2)),
            (shxadd pat:$rs1, GPR:$rs2)>;
}
```

第一条匹配 `(add (shl x, 3), y)` 的直白形状；第二条匹配 DAG combine 产出的 `RISCVISD::SHL_ADD`（`Zb.td:34`）；第三条把复杂形状交给 `ComplexPattern` 回调 C++。

`riscv_add_like_non_imm12` 不是普通 opcode，而是 `PatFrags`：`riscv_add_like`（`td:1557`）把 `riscv_or_disjoint` 和 `add` 并成一组，所以一条 pattern 能同时匹配 `add` 与“已知不重叠的 `or`”。

### `ComplexPattern` 是 TableGen 调用 C++ 的接口

```tablegen
def AddrRegImm : ComplexPattern<iPTR, 2, "SelectAddrRegImm">;               // 618
def sh3add_op : ComplexPattern<XLenVT, 1, "selectSHXADDOp<3>", [], [], 6>;  // Zb:223
```

三个参数：输入类型、**结果个数**（必须等于 pattern 里 `$` 绑定数）、C++ 函数名（可带模板实参）。最后一个 6 是 complexity 加权，会影响它在同一 opcode 作用域里的尝试顺序。函数返回 `false` 时匹配器**静默**换下一条——这是“pattern 写了但不生效”最常见的无声失败。

### `Predicates` 决定这条 pattern 在不在表里

```tablegen
let Predicates = [HasStdExtZba] in {     // RISCVInstrInfoZb.td:274
def SH3ADD : ALU_rr<0b0010000, 0b110, "sh3add">, ...
}
```

同一文件 746 行又写了一次，因为那次作用域里是 `def : Pat<...>`。**`Predicates` 不跨作用域继承**：在别处补 `def : Pat`，必须自己再写，或用 `Requires<[...]>`。到了生成物里它就是一行 `OPC_CheckPatternPredicate`（见 14.8 实验 2）。

### 顺序是 TableGen 排好的

`DAGISelEmitter.cpp:84` 的 `PatternSortingPredicate` 规定优先级：标量先于向量、整数先于 FP；然后 complexity 大的先；再按结果指令 cost、结果 pattern 长度排序。排序在 192 行。所以“新 pattern 没生效”经常不是没匹配，而是被一条更特化的 pattern 先吃掉了。

## 14.5 手写侧：`Select()` 的五个入口

### `PreprocessISelDAG()`（35）

在任何匹配之前倒序遍历 DAG，只处理四种节点：

| 行 | 节点 | 改写 |
|---|---|---|
| 46 | `ISD::SPLAT_VECTOR` | → `RISCVISD::VMV_V_X_VL` / `VFMV_V_F_VL`（`+p` 时跳过） |
| 63 | `RISCVISD::SPLAT_VECTOR_SPLIT_I64_VL` | → 两次 store + 一次 stride-0 load |
| 110 | `ISD::FP_EXTEND`（向量） | → `RISCVISD::FP_EXTEND_VL` |
| 123 | `ISD::ADD`（宽常量） | → `ISD::SUB`，条件是 `-C` 已在 DAG 里且更便宜 |

ADD 那段注释说透了理由：

```cpp
// Turn (add X, C) into (sub X, -C) when a constant node holding -C
// already exists in the DAG, so both share one materialization. Do this
// before selection, while both are still ConstantSDNodes: by selection
// time -C may already have been selected into instructions.
```

一旦改成 `SUB`，所有 `(add GPR, imm)` 的 pattern 都看不到这个节点了。

### `Select()`（1178）

开头先挡掉已选节点（`isMachineOpcode()`，1180），然后是函数内 75 个 `case` 标签（含嵌套）。它处理两类东西：

- 通用节点里 pattern 表达不了的：`ISD::Constant`（1196）、`ConstantFP`（1248）、部分 `SHL/SRL/SRA/OR/XOR/AND/MUL/LOAD`；
- target 私有节点：`RISCVISD::ADDD`（2202）、`BuildPairF64`（1320）、`LD_RV32`（2121）等。

每个 case 都可以 `break` 落到函数末尾的 `SelectCode(Node)`（3447）。分工边界是：**手写分支只做“表做不到或做起来表太大”的事；做不了就还给 TableGen。**

注意“target 私有节点”不等于“必须手写”：`RISCVISD::SHL_ADD` 没有 `case`，由 `Zb.td:727` 的 pattern 匹配。

### `selectSETCC()`（4173）

RISC-V 没有通用的 `setne`/`seteq`，这里把“与常量比较”改写成“与 0 比较”：常量 0 用 LHS；`-2048` 用 `xori`；`[-2047,2048]` 用 `addi x, -C`；2 的幂且有 Zbs 用 `binvi`；否则 `xor`。它由 `ComplexPattern` 调用（`td:1711–1714`），pattern 只做最后一步：

```tablegen
def : Pat<(riscv_seteq (XLenVT GPR:$rs1)), (SLTIU GPR:$rs1, 1)>;   // 1723
```

### `selectSHXADDOp()`（4311）

处理“先移位、再掩码”的形状，把掩码折算成一次 `srli`/`srliw` 再交给 `SHXADD`。它在匹配途中**直接创建机器节点**：

```cpp
Val = SDValue(CurDAG->getMachineNode(Opcode, DL, VT, N0.getOperand(0),
                                     CurDAG->getTargetConstant(ShAmt, DL, VT)), 0);
```

`-debug-only=isel` 把这种节点打成 `Created node:`，与最终 `Morphed node:` 分开。

### 地址匹配（3478–4096）

| 函数 | 行 | 形状 | 谁在用 |
|---|---|---|---|
| `SelectAddrFrameIndex` | 3478 | `FrameIndex` | 所有 load/store |
| `SelectAddrRegImm` | 3630 | `reg + simm12`、`ADD_LO`、全局地址 | `AddrRegImm`（`td:618`） |
| `SelectBrindRegImm` | 3738 | 只要 reg/reg+imm | 间接跳转 |
| `SelectAddrRegImm26` | 3763 | `reg + simm26` | Xqci（`Xqci.td:208`） |
| `SelectAddrRegImm9` | 3825 | `reg + uimm9` | XMips（`XMips.td:24`） |
| `SelectAddrRegImmLsb00000` | 3852 | 低 5 位为 0 | Zicbo（`Zicbo.td:72`） |
| `SelectAddrRegRegScale` | 3977 | `base + index*N` | `AddrRegRegScale<N>` |
| `SelectAddrRegZextRegScale` | 4064 | `base + zext(index)*N` | `sh3add.uw` 类 |
| `SelectAddrRegReg` | 4083 | 纯 `reg + reg` | 少数指令 |

`ld a0, 24(a0)` 里的 24 不是汇编器算的，是 `SelectAddrRegImm` 在匹配阶段填进 `Offset` 的。

## 14.6 匹配顺序：Preprocess → Custom → Table

`SelectionDAGISel.cpp:1277` 的 `DoInstructionSelection` 把顺序写死：先 `PreprocessISelDAG();`（1281），再 `AssignTopologicalOrder()`，然后从表尾往表头走（1303）。注释写得很直白：`Visit the nodes by starting at the end of the list (the root of the graph)`。所以先选返回值、再选操作数；`-debug-only=isel` 的输出顺序就是 `PseudoRET → CopyToReg → add`。

每次 `Select` 内部再分三层：`isMachineOpcode()` 跳过已选；`switch` 的 custom 分支能造就造，否则 `break`；`SelectCode` 按字节码试 pattern，失败回溯到下一个 `OPC_Scope`：

```text
  Match failed at index 27370
  Continuing at 27380
```

“第一条完整匹配胜出”意味着：**pattern 的书写顺序不重要，TableGen 的排序重要。**

两个容易忽略的前置：`SelectCodeCommon` 对 `EntryToken`、`Register`、`TargetConstant`、`CopyFromReg` 等节点直接标记已选，根本不进表（`SelectionDAGISel.cpp:3331`）；`PreprocessISelDAG` 的改写发生在拓扑排序之前，所以它改完的 DAG 才是匹配器看到的 DAG。

## 14.7 源码落点

| 内容 | 路径 | 关键行/符号 |
|---|---|---|
| custom 入口 | `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp` | `Select` 1178；`SelectCode` 调用 3447 |
| 选前改写 | 同上 | `PreprocessISelDAG` 35；ADD 123 |
| 选后 peephole | 同上 | `PostprocessISelDAG` 216 |
| setcc | 同上 | `selectSETCC` 4173；模板见 `RISCVISelDAGToDAG.h:121–127` |
| SHXADD | 同上 | `selectSHXADDOp` 4311 |
| 地址匹配 | 同上 | 3478–4096 |
| pattern 宏 | `llvm/lib/Target/RISCV/RISCVInstrInfo.td` | `PatGprGpr` 1454；`add→ADD` 1537 |
| 立即数 pattern | 同上 | `PatGprSimm12` 1461；`add→ADDI` 1538 |
| `PatFrags` | 同上 | `riscv_add_like` 1557 |
| setcc pattern | 同上 | `riscv_seteq` 1711；`SLTIU` 1723 |
| 地址 ComplexPattern | 同上 | `AddrRegImm` 618 |
| Zba 指令 | `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td` | `Predicates` 274；`SH3ADD` 279 |
| Zba pattern | 同上 | `ShxAddPat` 724；`Predicates` 746 |
| SHXADD ComplexPattern | 同上 | 221–223 |
| 生成物 | `$T/lib/Target/RISCV/RISCVGenDAGISel.inc` | `SelectCode` 51；`MatcherTable` 58 |
| 排序 | `llvm/utils/TableGen/DAGISelEmitter.cpp` | 84；192 |
| 解释器 | `llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp` | `SelectCodeCommon` 3331 |
| 选项 | `llvm/lib/Target/RISCV/RISCVOptions.td` | 14 |

---

## 14.8 可跑实验

以下全部在本机构建上跑过，输出原样粘贴（长输出用 `…` 省略）。

### 实验 1：把 `-debug-only=isel` 的偏移对回生成物

输入 `add.ll`：

```llvm
define i64 @add(i64 %a, i64 %b) { %r = add i64 %a, %b
  ret i64 %r }
```

```bash
$T/bin/llc -mtriple=riscv64 -debug-only=isel add.ll -o - 2>&1 \
  | awk '/Starting selection on root node: t5/{f=1} f&&/Match complete/{print; exit} f' \
  | grep -E "Starting selection|Initial Opcode|Morphed node|Match complete"
```

```text
ISEL: Starting selection on root node: t5: i64 = add t2, t4
  Initial Opcode index to 24569
  Morphed node: t5: i64 = ADD t2, t4
ISEL: Match complete!
```

`24569` 不是行号，是 `MatcherTable` 的**字节偏移**。每行生成物都带偏移注释，可直接反查：

```bash
grep -n " 24569\*/" $T/lib/Target/RISCV/RISCVGenDAGISel.inc
```

```text
12385:/*  24569*/  OPC_Scope /*245 children */, 44|128,1/*172*/, // ->24744
```

一行解释了中间那几十条 `Match failed / Continuing`：ADD 的作用域里有 245 个候选。

### 实验 2：`+zba` 与 `-zba` 的分界就是一行 predicate

输入 `sh3.ll`：

```llvm
define i64 @sh3(i64 %x, i64 %y) { %s = shl i64 %x, 3
  %r = add i64 %s, %y
  ret i64 %r }
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+zba sh3.ll -o - | sed -n '/sh3:/,/ret/p'
$T/bin/llc -mtriple=riscv64 sh3.ll -o - | sed -n '/sh3:/,/ret/p'
```

两次运行合并显示：

```text
# -mattr=+zba
sh3:
	sh3add	a0, a0, a1
	ret
# 无 -mattr
sh3:
	slli	a0, a0, 3
	add	a0, a0, a1
	ret
```

没有 Zba 时，匹配器在 predicate 处被挡住：

```bash
$T/bin/llc -mtriple=riscv64 -debug-only=isel sh3.ll -o - 2>&1 \
  | grep -E "27370|Morphed node: t7"
```

```text
  Match failed at index 27370
  Morphed node: t7: i64 = ADD t6, t4
```

偏移 27370 在生成物 13924 行：`OPC_CheckPatternPredicate, 77, // (Subtarget->hasStdExtZba())`。有 Zba 时同一作用域走到 27372 的 `MorphNodeTo`，于是 `Morphed node: t7: i64 = SH3ADD t2, t4`。

### 实验 3：Preprocess 抢走 `add`，pattern 根本没机会

输入 `pre2.ll`：

```llvm
define i64 @pre(i64 %x, i64 %y, i1 %c) {
  %a = add i64 %x, 8589934592
  %b = add i64 %y, -8589934592
  %r = select i1 %c, i64 %a, i64 %b
  ret i64 %r
}
```

```bash
$T/bin/llc -mtriple=riscv64 -debug-only=riscv-isel pre2.ll -o - 2>&1 | head -5
```

```text
RISC-V DAG preprocessing replacing:
Old:    t11: i64 = add t4, Constant:i64<-8589934592>

New: t22: i64 = sub t4, Constant:i64<8589934592>
```

原来的两个宽常量 `add` 现在一个是 `add`、一个是 `sub`，常量不再出现在 `add` 的立即数位置。写新 Pat 前的第一课，就是先在 `-debug-only=isel` 里确认节点还叫不叫 `add`：

```text
	sub	a0, a1, a2
	…
	add	a0, a0, a2
```

注意 `-debug-only=isel` **看不到**这条日志：`PreprocessISelDAG` 的 `DEBUG_TYPE` 是 `riscv-isel`（`RISCVISelDAGToDAG.cpp:29`）。要两边就写 `-debug-only=isel,riscv-isel`。

同类的“谁先谁后”在 `Select` 里也有明文：`ISD::SHL` 分支在 `TrailingZeros == 0 && LeadingZeros != 32` 等条件下自己造 `slli`+`srli` 并 `return`；注释说明 `LeadingZeros == 32` 要留给 TableGen（无 Zba 用 `RISCVInstrInfo.td:2296`，有 Zba 用 `Zb.td:825`）。

### 实验 4：真实 `cl::opt` 开关观察选择结果

```bash
grep -rn "cl::opt" llvm/lib/Target/RISCV/
```

实测只有 1 条：

```text
llvm/lib/Target/RISCV/RISCVTargetMachine.cpp:281:static cl::opt<RVVRegisterRegAlloc::FunctionPassCtor, false,
```

不是笔误：大部分开关已迁到 `RISCVOptions.td`，用 `defm : BoolField<...>` 描述，访问方式是 `Subtarget.getCLOpts()`。例如 `RISCVOptions.td:14` 的 `riscv-use-rematerializable-movimm`（默认 `"false"`）。它只影响 `selectImm`，但影响“选哪条指令”：

```llvm
define i64 @imm() { ret i64 74565 }
```

```bash
$T/bin/llc -mtriple=riscv64 -stop-after=finalize-isel imm.ll -o - \
  | sed -n '/bb.0/,/PseudoRET/p'
$T/bin/llc -mtriple=riscv64 -riscv-use-rematerializable-movimm \
  -stop-after=finalize-isel imm.ll -o - | sed -n '/bb.0/,/PseudoRET/p'
```

两次运行合并显示：

```text
# 默认
    %0:gpr = LUI 18
    %1:gpr = ADDI %0, 837
    $x10 = COPY %1
# -riscv-use-rematerializable-movimm
    %0:gpr = PseudoMovImm 74565
    $x10 = COPY %0
```
`PseudoMovImm` 在展开阶段还会变回同样的 `lui`+`addi`，所以只看 `.s` 看不出来，必须停在 `finalize-isel`。

### 实验 5：一个 ComplexPattern 决定四种寻址

输入 `addr.ll`（含 `@g = global i64 0` 与三个函数）：

```llvm
define i64 @ld_off(ptr %p) { %q = getelementptr i64, ptr %p, i64 3
  %v = load i64, ptr %q
  ret i64 %v }
define i64 @ld_idx(ptr %p, i64 %i) { %q = getelementptr i64, ptr %p, i64 %i
  %v = load i64, ptr %q
  ret i64 %v }
define i64 @ld_glob() { %v = load i64, ptr @g
  ret i64 %v }
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+zba addr.ll -o - | grep -E "^ld_|ld[[:space:]]|sh3add|lui"
```

```text
ld_off:
	ld	a0, 24(a0)
ld_idx:
	sh3add	a0, a1, a0
	ld	a0, 0(a0)
ld_glob:
	lui	a0, %hi(g)
	ld	a0, %lo(g)(a0)
```

对应的 `Morphed node`：

```text
  Morphed node: t7: i64,ch = LD<Mem:(load (s64) from %ir.q)> t2, TargetConstant:i64<24>, t0
  Morphed node: t7: i64 = SH3ADD t4, t2
```

节点编号按函数重置：这两条分别属于 `ld_off` 与 `ld_idx`。`ld_glob` 里则是 `LUI(TF=4)` + `LD(TF=3)`。三件事同时发生：`24` 被 `SelectAddrRegImm` 折进 `Offset`；索引缩放被 `sh3add` 吃掉；全局地址被拆成 hi/lo 两步。FrameIndex 走 `SelectAddrFrameIndex`，debug 里是 `LD<Mem:(volatile dereferenceable load (s64) from %ir.a)> TargetFrameIndex:i64<0>, TargetConstant:i64<0>`。

### 实验 6：`selectSETCC` 在匹配途中造 `ADDI`

```llvm
define i64 @eq5(i64 %x) { %c = icmp eq i64 %x, 5
  %r = zext i1 %c to i64
  ret i64 %r }
```

```bash
$T/bin/llc -mtriple=riscv64 -debug-only=isel eq5.ll -o - 2>&1 \
  | grep -E "^  Morphed node: t12|^      t16: i64 = ADDI"
```

```text
  Morphed node: t12: i64 = SLTIU t16, TargetConstant:i64<1>
      t16: i64 = ADDI t2, TargetConstant:i64<-5>
```

DAG 里只有一个 `setcc`，`ADDI` 是 `selectSETCC` 在匹配时创建的；`riscv_seteq` 的 pattern 再包成 `SLTIU`，也就是汇编里的 `seqz`：`addi a0, a0, -5` / `seqz a0, a0`。

### 实验 7：`selectSHXADDOp` 的 `Created node`

```llvm
define signext i16 @srliw_1_sh1add(ptr %p, i32 signext %x) {
  %s = lshr i32 %x, 1
  %z = zext i32 %s to i64
  %q = getelementptr inbounds i16, ptr %p, i64 %z
  %v = load i16, ptr %q
  ret i16 %v }
```

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+zba -debug-only=isel srlsh1.ll -o - 2>&1 \
  | grep -E "Created node: t32|Morphed node: t13"
```

```text
  Created node: t32: i64 = SRLIW t6, TargetConstant:i64<1>
  Morphed node: t13: i64 = SH1ADD t32, t2
```

`Created node` 是 `ComplexPattern` 的回调产物，`Morphed node` 才是 pattern 的最终结果；汇编是 `srliw a1, a1, 1` / `sh1add a0, a1, a0`。

## 14.9 踩雷集锦

1. **用 `-debug-only=isel` 找 Preprocess 的日志。** 找不到；`DEBUG_TYPE` 是 `riscv-isel`（`RISCVISelDAGToDAG.cpp:29`），要写 `-debug-only=isel,riscv-isel`。
2. **把 `Initial Opcode index to 24569` 当成行号。** 它是 `MatcherTable` 的字节偏移；行号靠 `grep -n " 24569\*/"` 反查（本例 12385）。
3. **只加 `+zba` 测试，不写 `-zba` 反例。** predicate 写错时正向测试照样过；`Match failed at index 27370` 只有在关闭扩展时才出现。
4. **以为 `Pat<>` 看到的是 IR 形状。** 它看到的是 Legalize + DAGCombine + Preprocess 之后的形状；写 pattern 前先看 Optimized legalized DAG。
5. **在 `Select` 的 custom 分支里 `return`。** 一旦 `return` 就没有机会落回 `SelectCode`；只有 `break` 才是“我处理不了，交给表”。
6. **`Predicates` 写在指令 `def` 上，以为同文件的 `def : Pat` 会继承。** 不会；每个 `let` 作用域独立，`def : Pat` 要么再包一层，要么用 `Requires<[...]>`。
7. **`ComplexPattern` 的结果个数与 pattern 绑定数不一致。** 把 `AddrRegImm` 的 2 写成 1，tblgen 可能报错，也可能生成永远 `false` 的匹配；后者只表现为“指令质量变差”。
8. **改了 `.td` 只在旧构建里跑 `llc`。** 生成物没重跑时你看的是上一版 `MatcherTable`；确认 `ninja llc` 触发了 `RISCVGenDAGISel.inc` 的重新生成。

## 14.10 本章重点整理

- 一次选指 = `PreprocessISelDAG` → 拓扑排序 → 从 root 往 entry 的 `Select` → `SelectCode` 字节码匹配。
- `RISCVGenDAGISel.inc` 的三个产物：`MatcherTable`（约 1.2 MB 字节码）、`SelectCode`（薄封装）、共用的 `OPC_*` 枚举；61 MB 的主要成本是 42,558 条 pattern 的字节码与注释。
- `Pat<源, 目标>` 的源形状是 Legalize/Combine/Preprocess 之后的 DAG；`$name` 是绑定，不是变量声明。
- `ComplexPattern` 是 TableGen 调 C++ 的接口；返回 `false` 时静默失败，结果个数必须与绑定数一致。
- `Predicates` 在生成物里就是 `OPC_CheckPatternPredicate`；作用域不继承，`def : Pat` 要自己写。尝试顺序由 `PatternSortingPredicate` 决定：标量/整数优先，再按 complexity、结果 cost、结果长度排序。
- 手写 `Select` 只处理“表做不到”的节点；custom 分支必须 `break` 才能回落到 `SelectCode`。
- `PreprocessISelDAG` 先于一切匹配，被它改写掉的节点，对应 pattern 永远不会触发；`(add X, C) → (sub X, -C)` 是最典型的例子。
- 地址匹配集中在 `SelectAddr*`（3478–4096）；`ld a0, 24(a0)` 的 24 是匹配阶段填的。
- 排错顺序：先看 Optimized legalized DAG，再看 `Initial Opcode index` 对应的生成物行，最后才怀疑 pattern 写错。

## 14.11 自我检核

- [ ] 能说出 `PreprocessISelDAG`、`Select`、`SelectCode` 三者的执行顺序
- [ ] 能解释 `Initial Opcode index to 24569` 与生成物第 12385 行的关系
- [ ] 给定 `(add (shl x, 3), y)`，能指出它由 `ShxAddPat` 的哪一条匹配，以及 predicate 在生成物里的位置
- [ ] 能说出 `ComplexPattern` 三个参数各是什么，返回 `false` 会发生什么
- [ ] 能解释为什么 `let Predicates` 不会自动作用于另一处写的 `def : Pat`
- [ ] 能说出 custom `Select` 分支与 `SelectCode` 的分界，以及 `break` 与 `return` 的差别
- [ ] 能举出一个“pattern 写了但永远匹配不到”的例子，并给出观察它的 debug 类型
- [ ] 能用 `-stop-after=finalize-isel` 观察 `cl::opt` 对选指结果的影响

## 14.12 延伸阅读

- **`RISCVISelDAGToDAG.cpp`**（5,295 行）：读 35–215 的 `PreprocessISelDAG`、1178–3448 的 `Select`、3478–4096 的地址匹配、4173 与 4311 两个 `select*`。所有“表做不到”的逻辑都在这里。
- **`RISCVInstrInfo.td`**（2,553 行）：读 1452–1462 的 pattern 宏、1537–1538 的 `add` 两条 pattern、1557 的 `PatFrags`、1711–1724 的 setcc。这是一条指令有哪些 DAG 入口的字典。
- **`RISCVInstrInfoZb.td`**（869 行）：读 221–223、274 与 746 两处 `Predicates`、724–731 的 `ShxAddPat`。Zba 是“同一语义、多种 DAG 形状”的最小完整案例。
- **`llvm/utils/TableGen/DAGISelEmitter.cpp`**：读 81–140 的 `PatternSortingPredicate`、185–196 的排序与 matcher 生成。它决定你的 pattern 排在第几。
- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**：读 1276–1340 的 `DoInstructionSelection`、3331 起的 `SelectCodeCommon`。把后端行为放回通用框架。
- **`llvm/test/CodeGen/RISCV/rv64zba.ll`**：读 `srliw_1_sh1add` 与 `sh3adduw_ptrdiff` 两组 CHECK，它们是 `selectSHXADDOp` 与地址缩放的现成回归样本。

---

→ 下一章：[Ch 15 — 自定义 lowering 与 intrinsic](./15-custom-lowering-and-intrinsics.md)
