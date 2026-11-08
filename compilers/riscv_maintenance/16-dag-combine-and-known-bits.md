# Ch 16 — DAG combine、known bits 与目标节点

> **目标**：能回答「同一段 IR，为什么 O2 下少一条指令」——判断那条指令是被 DAG combine 消掉的、被 ISel pattern 直接选掉的，还是根本没进 DAG；能在 `PerformDAGCombine` 里按 opcode 找到分派点；能写出正确的 `computeKnownBitsForTargetNode` / `ComputeNumSignBitsForTargetNode`，并知道答错会变成 miscompile 而不是「少优化一条」。

> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`。本章所有「实测」都来自这套构建（`Optimized build with assertions`）；跑不了的会标「未实测」。

> **前置**：Ch 13（合法化：三次 combine 机会的由来）、Ch 14（pattern 与 `Select`）、Ch 15（`RISCVISD::*` 目标节点与 `LowerOperation`）。

---

## 16.1 为什么 DAG combine 是维护者必读

三个理由，每个都能在 issue tracker 里找到对应样本：

1. **它决定了「同一段 IR 为什么 O2 下少一条指令」。** 16.6 的例子：两个 `shl` 相加，`+zba` 下 3 条、无 `+zba` 4 条，这是 combine 的功劳；而 `(x<<1)+y` 的 1 条 vs 2 条是 ISel pattern 的功劳。分不清这两类，就会在错误的地方改代码。
2. **它是 bug 高发区，而且错法很隐蔽。** DAG combine 的输入输出都是**无类型语义的 DAG 片段**：没有 IR verifier，没有 `MachineVerifier` 兜底。combine 把 i64 的形状套到 i32 语义上、或忘记保持 poison，编译器不会报错，只会生成错码。
3. **「我的 combine 不生效」经常根本不是 combine 的事。** 16.9 给出三个反例：`sh1add`、`zext.h`、`bseti` 在简单形状下都由 ISel pattern 直接选掉，`PerformDAGCombine` **从未被调用**——加 `-debug-only=dagcombine` 看不到任何 `into:` 行。

先记住五个规模数字，它们决定了你该往哪读：

| 事实 | 数值 | 核对方式 |
|---|---|---|
| `RISCVISelLowering.cpp` 总行数 | 31,346 | `wc -l` |
| `PerformDAGCombine` 函数体 | 25,386–27,297，约 1,900 行 | `grep -n` + `sed` |
| 构造函数里 `setTargetDAGCombine` 调用 | 14 次 | `grep -n` |
| 实际注册的**唯一** `ISD::` opcode | **60** 个 | `grep -oE "ISD::[A-Z_0-9]+" \| sort -u \| wc -l` |
| `computeKnownBitsForTargetNode` 覆盖的 target 节点 | 20 个 case 标签 | `awk` 抓 `case` |

---

## 16.2 先建立直觉：三次机会（四次 Combine 调用）

SelectionDAG 的合法化不是一次，而是分阶段推进；每次推进**前后**都跑一遍 DAGCombiner。入口在 `SelectionDAGISel.cpp`，四个 `CurDAG->Combine(...)` 调用就是全部：

```
        SelectionDAGBuilder 建出的初始 DAG
                     │
                     ▼
   ①  Combine(BeforeLegalizeTypes)          ← pre-legalize（类型未合法）
                     │                          SelectionDAGISel.cpp:977
                     ▼
        LegalizeTypes（类型 / 向量类型合法化）
                     │
                     ▼
   ②  Combine(AfterLegalizeTypes)           ← 类型已合法，操作还没
       Combine(AfterLegalizeVectorOps)         SelectionDAGISel.cpp:1023 / 1077
                     │
                     ▼
        LegalizeDAG（操作合法化：Custom → LowerOperation → RISCVISD::*）
                     │
                     ▼
   ③  Combine(AfterLegalizeDAG)             ← post-legalize（唯一能自由造目标节点）
                     │                          SelectionDAGISel.cpp:1117
                     ▼
        Instruction Select（pattern / ComplexPattern / selectSHXADDOp）
                     │
                     ▼
                  MachineInstr

  主线：DAGCombiner (pre-legalize) → LegalizeDAG → DAGCombiner (post-legalize) → ISel
```

「三次机会」指三个语义阶段；`②` 里其实是两次调用（type-legalize 后、vector-legalize 后），所以一共四次。它们**共享同一份 `PerformDAGCombine`**，靠 `DAGCombinerInfo` 里的 `Level` 区分自由度：

```cpp
// llvm/include/llvm/CodeGen/DAGCombine.h:15-20
enum CombineLevel { BeforeLegalizeTypes, AfterLegalizeTypes,
                    AfterLegalizeVectorOps, AfterLegalizeDAG };
// llvm/include/llvm/CodeGen/TargetLowering.h:4757-4761
bool isBeforeLegalize()    const { return Level == BeforeLegalizeTypes; }
bool isBeforeLegalizeOps() const { return Level <  AfterLegalizeVectorOps; }
bool isAfterLegalizeDAG()  const { return Level >= AfterLegalizeDAG; }
bool isCalledByLegalizer() const { return CalledByLegalizer; }
```

四个谓词的自由度差异，是本章最容易踩错的一张表：

| 上下文 | 判定 | 你可以建什么 | 你不可以做什么 |
|---|---|---|---|
| pre-legalize | `isBeforeLegalize()` | 任意**类型**的普通 ISD 节点；之后 `LegalizeTypes` 会兜底 | 建 `RISCVISD::*` 目标节点；建非法类型却指望后面有人修 |
| post-type / post-vector | `!isBeforeLegalize() && !isAfterLegalizeDAG()` | 只能建**已合法类型**的普通 ISD 节点 | 建目标节点（操作还没合法化，`Select` 之前没人认识它） |
| post-legalize | `isAfterLegalizeDAG()` | 普通节点 + `RISCVISD::*` | 建非法类型 / 非法操作 |
| legalizer 内部回调 | `isCalledByLegalizer()` | 只做「就地改写」 | 假设 DAG 已稳定；做需要遍历整张图的重量级重写 |

两个反直觉点，源码里明写着：

- **`isBeforeLegalize()` 只等于 `BeforeLegalizeTypes`。** 所以 RISC-V 里常见的 `!DCI.isBeforeLegalize() && !DCI.isCalledByLegalizer()`（`RISCVISelLowering.cpp:20010`）意思是「类型已经合法了」，**不是**「已经 post-legalize」。
- **类型合法的强制从 `combine1` 之后开始。** `SelectionDAGISel.cpp:1013` 在 `combine_lt` 之前设 `CurDAG->NewNodesMustHaveLegalTypes = true;`，assertions 构建下 `SelectionDAG.cpp:1734` 等处会直接断言。这就是「pre-legalize 不能乱造节点」的机器证据，不是口头约定。

---

## 16.3 目标钩子注册：`setTargetDAGCombine`

generic opcode 想进 `PerformDAGCombine`，必须在 `RISCVTargetLowering` 构造函数里报名。本树全部 14 次调用、60 个唯一 opcode，按行号整理如下（完整代码见 `RISCVISelLowering.cpp:1964-2030`）：

| 行 | 条件 | opcode |
|---|---|---|
| 1964 | 无条件 | `INTRINSIC_VOID`、`INTRINSIC_W_CHAIN`、`INTRINSIC_WO_CHAIN`、`ADD`、`SUB`、`MUL`、`AND`、`OR`、`XOR`、`SETCC`、`SELECT`、`SRA` |
| 1968 | 无条件 | `SIGN_EXTEND_INREG` |
| 1971 | `F` 或 `Zfinx` | `FADD`、`FMAXNUM`、`FMINNUM`、`FMUL` |
| 1975 / 1977 | `V` / `V` 或 `P` | `UMAX`/`UMIN`；`SMAX`/`SMIN` |
| 1981 | `Zbs`+RV64、`V` 或 `P` | `TRUNCATE` |
| 1984 / 1987 | `Zbkb` / `Zbc`+RV64 | `BITREVERSE` / `SRL` |
| 1990 | `F` 或 `Zfinx` | `ZERO_EXTEND`、`FP_TO_SINT`、`FP_TO_UINT`、`FP_TO_SINT_SAT`、`FP_TO_UINT_SAT` |
| 1993 | `V` | 28 个向量相关 opcode（`MGATHER`…`VECTOR_SPLICE_RIGHT`） |
| 2023 | `Zvzip` | `VECTOR_INTERLEAVE` |
| 2026 | `XTHeadMemPair` | `LOAD`、`STORE` |
| 2028 / 2030 | 固定向量 RVV 或 `P` / `P`+RV64 | `BITCAST` / `ZERO_EXTEND_VECTOR_INREG` |

分派发生在 `PerformDAGCombine`（`25386`）的 `switch (N->getOpcode())`（`25405`）。但**是否调用**由 DAGCombiner 决定，而且逻辑有一个很多人不知道的短路：

```cpp
// llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp:2196-2204
if (N->getOpcode() >= ISD::BUILTIN_OP_END ||
    TLI.hasTargetDAGCombine((ISD::NodeType)N->getOpcode())) {
  TargetLowering::DAGCombinerInfo DagCombineInfo(DAG, Level, false, this);
  RV = TLI.PerformDAGCombine(N, DagCombineInfo);
}
```

`N->getOpcode() >= ISD::BUILTIN_OP_END` 是关键：**`RISCVISD::*` 目标节点永远会进 `PerformDAGCombine`，不需要注册**；注册只对 generic `ISD::*` 生效（实现见 `TargetLowering.h:2010` 的位图查询）。所以：

- 「我的 target 节点 combine 不生效」→ 看 `switch` 有没有对应 `case`，不是看注册。
- 「我的 `ISD::FOO` combine 不生效」→ 先看 `setTargetDAGCombine` 漏没漏，再看条件（很多注册是 `if (Subtarget.hasVInstructions())` 包着的）。

---

## 16.4 `PerformDAGCombine` 的骨架：分派在 `switch`，逻辑在 `perform*`

`PerformDAGCombine` 本体的 `switch` 只做三件事：处理 `RISCVISD::*`、跳进 `perform*` 辅助函数、`default: break`。真正的判断都在 `performADDCombine`（`20002`）、`performANDCombine`（`20570`）、`performORCombine`（`20789`）、`performSIGN_EXTEND_INREGCombine`（`21693`）这些 `static` 函数里。

```cpp
switch (N->getOpcode()) {
default:
  break;                                    // 25405-25406：不认识的什么都不做
case RISCVISD::SplitF64: { ... }            // 25409
case ISD::ADD: {                            // 25843
  if (SDValue V = combineOp_VLToVWOp_VL(N, DCI, Subtarget)) return V;
  if (SDValue V = combineToVWMACC(N, DAG, Subtarget))        return V;
  if (SDValue V = combineVdot4aAccum(N, DAG, Subtarget))     return V;
  return performADDCombine(N, DCI, Subtarget);
}
```

于是**阶段门**散落在各个 `perform*` 里，这就是「为什么我的 combine 只在某些 `-O` 下生效」的答案。本树出现的三处典型写法：

```cpp
// 20008-20015：transformAddShlImm / combineShlAddIAdd 只在类型合法化之后跑
if (!DCI.isBeforeLegalize() && !DCI.isCalledByLegalizer()) {
  if (SDValue V = transformAddShlImm(N, DAG, Subtarget))  return V;
  if (SDValue V = combineShlAddIAdd(N, DAG, Subtarget))   return V;
}
// 25939-25941：pre-legalize 只做展开，不追求优化
case ISD::VECTOR_MATCH:
  if (DCI.isBeforeLegalize()) return expandVectorMatch(N, DAG);
  return SDValue();
// 25010-25013：只在 pre-legalize 才能改 scatter/gather 的 index 类型
if (!DCI.isBeforeLegalize()) return false;
```

读代码时的实用规则：**先看这个 `perform*` 有没有 `DCI` 参数**。签名里没有 `DCI` 的函数（如 `combineSubShiftToOrcB`、`performTRUNCATECombine`）不可能做阶段判断，它们要么在所有阶段都安全，要么由调用方替它挡。

---

## 16.5 known bits 三件套

generic DAG（`DAGCombiner`）只知道 `ISD::*` 的语义。你造了一个 `RISCVISD::SLLW`，它不知道结果是「低 32 位运算后符号扩展」，于是所有依赖位信息的折叠都会失效。三个 hook 就是你补交语义的地方，声明在 `TargetLowering.h:4600/4642/4669`：

| hook | 本树位置 | 回答的问题 | 答错的后果 |
|---|---|---|---|
| `computeKnownBitsForTargetNode` | `27318` | 哪些位已知 0 / 已知 1 | 答「已知 1」错了 → 折叠出错误常量 → miscompile |
| `ComputeNumSignBitsForTargetNode` | `27536` | 从 MSB 起有多少位等于符号位 | 答多了 → `sext`/`ashr` 被消掉 → miscompile |
| `SimplifyDemandedBitsForTargetNode` | `27632` | 只需要某些位时，能不能改操作数 | 答错了 → 丢掉仍有用的位 → miscompile |

`computeKnownBitsForTargetNode` 的第一行是 `Known.resetAll()`（`27332`），`switch` 的 `default: break` 就代表「全未知」。**默认值是保守正确的，任何 case 都是在放宽。** 20 个 case 里最值得读的两处：

```cpp
// 27351-27357：CZERO_EQZ/NEZ 的结果要么全 0、要么等于 op0
Known = DAG.computeKnownBits(Op.getOperand(0), Depth + 1);
// Result is either all zero or operand 0. We can propagate zeros, but not ones.
Known.One.clearAllBits();

// 27405-27412：SHL_ADD = (op0 << shamt) + op2，位信息要跟着移位走
Known <<= ShAmt;
Known.Zero.setLowBits(ShAmt);   // the <<= operator left these bits unknown
Known = KnownBits::add(Known, Known2);
```

`ComputeNumSignBitsForTargetNode` 的核心是「把 target 节点的语义翻译成符号位个数」。`RISCVISD::SLLW` 一族直接返回 33（`27572-27584`）：

```cpp
case RISCVISD::SLLW: case RISCVISD::SRLW: case RISCVISD::DIVW:
  // TODO: As the result is sign-extended, this is conservatively correct.
  return 33;
```

33 = 32 位结果 + 1 个符号位，即「bit 31 是符号位，bit 32..63 全是它的复制」。正是这个 33 让 `(sext_inreg (SLLW x, y), i32)` 这类形状被识别为冗余。`RISCVISD::VMV_X_S`（`27590`）更细：按元素宽度算 `XLen - EltBits + 1`，因为 `vmv.x.s` 会把元素符号扩展到 XLEN。

**为什么 generic DAG 不知道 `PseudoVADD` 的高位？** 因为 `PseudoVADD` 根本不是 DAG 节点——它是机器伪指令，`Select` 之后才存在。DAG 层承载向量加法语义的是 `RISCVISD::ADD_VL`（由 `6032`、`15556` 等少数 lowering 路径手工构造，不是 `llvm.riscv.vadd` 那条 pattern 路径），而它**不在** `computeKnownBitsForTargetNode` 的 case 列表里，于是落到 `default`，`Known` 全未知。也就是说「vadd 结果的高 24 位来自 `zext`」这类信息 generic DAG 拿不到：对 `ADD_VL` 做 `trunc` 或 `and` 掩码，不会有任何基于 known bits 的折叠。

这不是 bug，是「保守正确答案」。想让它变成优化，就得在 `27318` 加 case；加 case 之前先读 16.12 的第 5、6 条——你回答的每一位都会直接进入 `SimplifyDemandedBits` 和常量折叠。

`SimplifyDemandedBitsForTargetNode`（`27632`）本树只处理两个节点：`RISCVISD::BREV8` 和 `RISCVISD::ORC_B`，其余 `return TargetLowering::SimplifyDemandedBitsForTargetNode(...)`（`27661`，基类默认 `false`）。它必须把「被需求的位」按位排列**变换过去**，否则会误删操作数上仍有用的位。

还有一个容易漏的第四个 hook：`canCreateUndefOrPoisonForTargetNode`（`27665`）。它不回答位信息，只回答「这个 target 节点会不会凭空产生 poison」。`SLLW`/`SRAW`/`SRLW`/`RORW`/`ROLW` 返回 `false`，理由是注释里那句：只读 RHS 的低 5 位，移位量天然有界（`27673-27680`）。

---

## 16.6 案例 A：`(add (shl x,C0), (shl y,C1))` → `SHL_ADD`

这是本章最值得精读的一条，因为它同时展示了「combine 造目标节点」和「同一指令还有 pattern 捷径」。

**输入 DAG**（`-debug-only=isel` 的 `Optimized lowered selection DAG`，pre-legalize combine 之后）：

```
t6: i64 = shl t2, Constant:i64<1>
t8: i64 = shl t4, Constant:i64<3>
t9: i64 = add t6, t8
```

**触发条件**（`transformAddShlImm`，`19244-19285`）：`hasShlAdd(3)`（`+zba`/`+xandesperf`/`+xqciac`/`+xtheadba`）；两个操作数都是 `SHL` 且各只有一个 use；两个移位量都是正常量；`|C0-C1| <= 3` 且该差值也被 `hasShlAdd` 支持。**调用点还有阶段门**：`!DCI.isBeforeLegalize() && !DCI.isCalledByLegalizer()`（`20010`）。

**输出 DAG**：

```
t14: i64 = RISCVISD::SHL_ADD t4, TargetConstant:i64<2>, t2
t15: i64 = shl t14, Constant:i64<1>
```

然后把公共的 `min(C0,C1)` 用一条 `slli` 补回来，`SHL_ADD` 由 TableGen 在 `RISCVInstrInfoZb.td:727` 匹配成 `SH2ADD`。最终两条计算指令（外加 `ret`）。

**但注意**：如果你写的是最朴素的 `(x << 1) + y`，`transformAddShlImm` 根本不会被调用——它要求两个操作数都是 `SHL`。这条路径由 `RISCVInstrInfoZb.td:725` 的 `riscv_add_like_non_imm12` pattern 在 ISel 直接选掉。同一助记符，三个入口：

| 输入形状 | 谁干的 | 源码 |
|---|---|---|
| `(add (shl x,i), y)`，i∈{1,2,3} | ISel pattern | `RISCVInstrInfoZb.td:725`（`ShxAddPat`） |
| `(add (shl x,C0), (shl y,C1))` | DAG combine → `SHL_ADD` → pattern | `19244` + `RISCVInstrInfoZb.td:727` |
| `(add (and (shl y,c2),c1), x)`，掩码是 shifted mask | ISel ComplexPattern，建 MachineNode | `RISCVISelDAGToDAG.cpp:4311` `selectSHXADDOp` |

第三行是本章唯一「combine 依赖选择阶段」的例子：`selectSHXADDOp` 里直接 `CurDAG->getMachineNode(RISCV::SRLI, ...)`（`4363-4366`），也就是在 ISel 期间手工造机器节点。它不走 DAG 层，因此不受 `PerformDAGCombine` 的阶段约束，也不能被 `-debug-only=dagcombine` 看到。

---

## 16.7 案例 B：`(sub (shl X,8-Y), (srl X,Y))` → `ORC_B`（known bits 当闸门）

`combineSubShiftToOrcB`（`20087`）由 `performSUBCombine`（`20163`）在 `20216` 调用。目标是识别「每个字节里只有第 Y 位可能为 1」的形状（`0b0000_1000_0000_1000` 合法，`0b0000_1000_0000_0100` 不合法），换成一条 `orc.b`。

**输入 → 输出**：`sub (shl X, 8-Y), (srl X, Y)`（Y 可为 0）→ `RISCVISD::ORC_B X`。

**触发条件**：`hasStdExtZbb()`；VT 是 `XLenVT`/`i32`/`i16`；两侧操作数同一来源；至少一侧 one-use。最关键的一步是**用 known bits 证明形状成立**：

```cpp
// 20130-20135
APInt Mask = APInt::getSplat(VT.getSizeInBits(), APInt(8, 0x1));
Mask <<= ShiftedAmount;
if (!DAG.MaskedValueIsZero(LeftShiftOperand, ~Mask))
  return SDValue();
```

`MaskedValueIsZero` 就是 `computeKnownBits` 的薄封装。于是同一段 `sub(shl,·)` 有两种命运，差别只在上游有没有 `and` 把 X 约束住——这个差别在汇编上直接可见（16.11 实验 4）。这也解释了为什么「我加了一条明显正确的 combine，但测试没变」：你的闸门用的是 known bits，测试里没有给它 known bits。

---

## 16.8 案例 C：`sext_inreg (shl X, Y), i32` → `SLLW`，然后 demanded bits 收尾

两段逻辑串起来看，能同时理解「known bits 做闸门」和「demanded bits 做清理」。

**第一段**，`performSIGN_EXTEND_INREGCombine`（`21693`）：

```cpp
// 21708-21714
// Fold (sext_inreg (shl X, Y), i32) -> (sllw X, Y) iff Y u< 32
if (Opc == ISD::SHL && Subtarget.is64Bit() && SrcVT == MVT::i32 &&
    VT == MVT::i64 && !isa<ConstantSDNode>(Src.getOperand(1)) &&
    DAG.computeKnownBits(Src.getOperand(1)).countMaxActiveBits() <= 5)
  return DAG.getNode(RISCVISD::SLLW, DL, VT, Src.getOperand(0),
                     Src.getOperand(1));
```

`countMaxActiveBits() <= 5` 的意思是「移位量最多 5 位宽，即 < 32」。这就是为什么 `%n` 直接来自参数时不折叠、`%n = and %n0, 31` 之后折叠：known bits 从「未知」变成「高 59 位为 0」。

**第二段**，`PerformDAGCombine` 里的 lambda `SimplifyDemandedLowBitsHelper`（`25395-25405`）：

```cpp
auto SimplifyDemandedLowBitsHelper = [&](unsigned OpNo, unsigned LowBits) {
  SDValue Op = N->getOperand(OpNo);
  APInt Mask = APInt::getLowBitsSet(Op.getValueSizeInBits(), LowBits);
  if (!SimplifyDemandedBits(Op, Mask, DCI)) return false;
  if (N->getOpcode() != ISD::DELETED_NODE) DCI.AddToWorklist(N);
  return true;
};
```

调用点（`25455-25465`）对 `SLLW/SRAW/SRLW/RORW/ROLW` 声明「只读 op0 的低 32 位、op1 的低 5 位」：

```cpp
case RISCVISD::SLLW: case RISCVISD::SRAW: case RISCVISD::SRLW:
case RISCVISD::RORW: case RISCVISD::ROLW: {
  // Only the lower 32 bits of LHS and lower 5 bits of RHS are read.
  if (SimplifyDemandedLowBitsHelper(0, 32) ||
      SimplifyDemandedLowBitsHelper(1, 5))
    return SDValue(N, 0);
  break;
}
```

效果是上游的 `and x, 0xFFFFFFFF` 和 `and n, 31` 都被判为「多需求」而删掉，只剩一条 `sllw`。注意 `return SDValue(N, 0)` 而不是 `SDValue()`：这是「节点还在、但已改过，请重新入队」的约定，写错会导致死循环或漏优化。

---

## 16.9 案例 D（对照）：三种「看起来像 combine、其实不是」

维护者最常见的误判，是把 ISel pattern 的功劳/责任算到 combine 头上。三条实测对照（输出见 16.11）：

| 形状 | 直觉归因 | 真实落点 | 证据 |
|---|---|---|---|
| `and x, 0xFFFF` → `zext.h` | `performANDCombine` | TableGen pattern，`RISCVInstrInfoZb.td:720/722` | `-debug-only=dagcombine` 无 `into:` 输出 |
| `or x, 1<<C`（C>11）→ `bseti` | `performORCombine` | TableGen pattern，`RISCVInstrInfoZb.td:532` 的 `SingleBitSetMask` | 同上 |
| `(shl x,1)+y` → `sh1add` | `performADDCombine` | `RISCVInstrInfoZb.td:725` | 同上 |

判据很简单：**加 `-debug-only=dagcombine` 看有没有 `... into:` 行。** 没有，就说明 DAG 没被改写，功劳在 `Select`。这也是为什么改 `performANDCombine` 修不了 `and 0xffff` 相关的错码——那个形状压根没进 combine。

例外是 `(and (srl X, Y), 1)`：它**确实**在 `performANDCombine` 里被改写（`20576-20603`），但只在 RV64+`Zbs` 且结果是 `i32` 时。注释把理由写得很清楚：

```cpp
// If we wait until type legalization, we'll create RISCVISD::SRLW and we
// can't recover it to use a BEXT instruction.
```

也就是说，这条 combine 故意在 **pre-legalize** 跑，用 `ANY_EXTEND` + `i64` 的 `SRL`/`AND` + `TRUNCATE` 保住形状，让 ISel 还能匹配 `BEXT`。pre-legalize 阶段造普通 ISD 节点是合法的（16.2 的表），造 `RISCVISD::*` 才不合法。这条代码是「阶段自由度」的正向用法样板。

---

## 16.10 源码落点

| 主题 | 位置 | 读什么 |
|---|---|---|
| 注册表 | `RISCVISelLowering.cpp:1964-2030` | 14 次 `setTargetDAGCombine`，注意 `if (Subtarget.hasVInstructions())` 包住的大段 |
| 分派 | `RISCVISelLowering.cpp:25386`、`25405` | `switch (N->getOpcode())`；`default: break` 的语义 |
| 阶段门 | `RISCVISelLowering.cpp:20010`、`25010`、`25939` | `isBeforeLegalize` / `isCalledByLegalizer` / `isAfterLegalizeDAG` |
| demanded bits | `RISCVISelLowering.cpp:25395`、`25455` | lambda + `SLLW` 族的低 32/5 位声明 |
| known bits | `RISCVISelLowering.cpp:27318` | `Known.resetAll()`、20 个 case、`SHL_ADD` 的移位传播 |
| 符号位 | `RISCVISelLowering.cpp:27536` | `SLLW` 一族返回 33；`VMV_X_S` 按元素宽度算 |
| demanded bits（target） | `RISCVISelLowering.cpp:27632` | 只有 `BREV8`/`ORC_B`，其余委托基类 |
| poison 边界 | `RISCVISelLowering.cpp:27665` | `canCreateUndefOrPoisonForTargetNode` |
| 选择阶段辅助 | `RISCVISelDAGToDAG.cpp:4311`、`4446` | `selectSHXADDOp` / `selectSHXADD_UWOp` 造 MachineNode |
| 地址选择 | `RISCVISelDAGToDAG.cpp:3630`、`3977`、`4064` | `SelectAddrRegImm` / `SelectAddrRegRegScale` / `SelectAddrRegZextRegScale` |
| 目标节点声明 | `RISCVInstrInfoZb.td:29-34` | `SDTIntShiftAddOp`、`riscv_shl_add` |
| 指令 pattern | `RISCVInstrInfoZb.td:720-734` | `zext.h`、`ShxAddPat`（725 直接、727 `SHL_ADD`、732 ComplexPattern） |
| 位操作 pattern | `RISCVInstrInfoZb.td:520-542` | `BEXT`/`BCLRI`/`BSETI` 的 `PatLeaf` 与 `Pat` |
| 三次机会 | `SelectionDAG/SelectionDAGISel.cpp:977/1023/1077/1117` | 四个 `CurDAG->Combine(...)` |
| 类型强制 | `SelectionDAG/SelectionDAGISel.cpp:1013` | `NewNodesMustHaveLegalTypes = true` |
| 调用判据 | `SelectionDAG/DAGCombiner.cpp:2196-2204` | target 节点短路：`opcode >= ISD::BUILTIN_OP_END` |
| 谓词定义 | `CodeGen/TargetLowering.h:4757-4761` | `isBeforeLegalize()` 只等于 `BeforeLegalizeTypes` |
| hook 声明 | `CodeGen/TargetLowering.h:4600/4642/4669` | 三个虚函数的契约注释 |

---

## 16.11 可跑实验

统一前置（一次即可）：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
cd /tmp
```

**实验 1：`(shl+add)` → `shNadd`，有/无 `+zba`**

```bash
cat > shadd.ll <<'EOF'
define i64 @sh1add(i64 %x, i64 %y) { %s = shl i64 %x, 1  %r = add i64 %s, %y  ret i64 %r }
define i64 @sh3add(i64 %x, i64 %y) { %s = shl i64 %x, 3  %r = add i64 %s, %y  ret i64 %r }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zba -O2 shadd.ll -o - | sed -n '/^sh1add:/,/ret/p'
$T/bin/llc -mtriple=riscv64 -mattr=+m       -O2 shadd.ll -o - | sed -n '/^sh1add:/,/ret/p'
```

实测 `+zba`：`sh1add a0, a0, a1` + `ret`；实测 `+m`：`slli a0, a0, 1` + `add a0, a0, a1` + `ret`。这条路径**没有** `into:` 行（`RISCVInstrInfoZb.td:725` 的 pattern），combine 版本见实验 2。

**实验 2：两个 `shl` 相加 → `RISCVISD::SHL_ADD`**

```bash
cat > two.ll <<'EOF'
define i64 @two_shl(i64 %x, i64 %y) {
  %a = shl i64 %x, 1
  %b = shl i64 %y, 3
  %r = add i64 %a, %b
  ret i64 %r
}
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zba -O2 -debug-only=dagcombine two.ll -o /dev/null 2>&1 | grep -E "into:|SHL_ADD"
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zba -O2 -debug-only=isel two.ll -o /dev/null 2>&1 | grep -A6 "Optimized legalized selection DAG"
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zba -O2 two.ll -o -
```

实测（前两行来自 `dagcombine`，后两行来自 `isel-dump`）：

```
 ... into: t15: i64 = shl t14, Constant:i64<1>
Combining: t14: i64 = RISCVISD::SHL_ADD t4, TargetConstant:i64<2>, t2
      t14: i64 = RISCVISD::SHL_ADD t4, TargetConstant:i64<2>, t2
    t15: i64 = shl t14, Constant:i64<1>
```

实测汇编：`sh2add a0, a1, a0` + `slli a0, a0, 1` + `ret`；无 `+zba` 时 `slli`+`slli`+`add`+`ret`（3 条 vs 4 条）。

**实验 3：`and` 掩码 → `zext.h`（pattern，不是 combine）**

```bash
cat > mask.ll <<'EOF'
define i64 @zext16(i64 %x) { %r = and i64 %x, 65535  ret i64 %r }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zbb -O2 mask.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+m       -O2 mask.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zbb -O2 -debug-only=dagcombine mask.ll -o /dev/null
```

实测：`+zbb` 给 `zext.h a0, a0`；无 `+zbb` 给 `slli a0, a0, 48` + `srli a0, a0, 48`。第三条命令**无任何 `into:` 输出**——`zext.h` 来自 `RISCVInstrInfoZb.td:722`。同族对照：`or i64 %x, 1048576` 在 `+zbs` 下是 `bseti a0, a0, 20`，否则 `lui a1, 256` + `or`，同样无 combine 输出。

**实验 4：known bits 当闸门——`orc.b` 折/不折**

```bash
cat > orc.ll <<'EOF'
define i64 @orcb_plain(i64 %x) { %s = shl i64 %x, 8  %r = sub i64 %s, %x  ret i64 %r }
define i64 @orcb_masked(i64 %x) {
  %m = and i64 %x, 72340172838076673   ; 0x0101010101010101
  %s = shl i64 %m, 8
  %r = sub i64 %s, %m
  ret i64 %r
}
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zbb -O2 orc.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zbb -O2 -debug-only=dagcombine orc.ll -o /dev/null 2>&1 | grep -E "into:|ORC_B"
```

实测：`orcb_plain` **不折**（`slli`+`sub`），`orcb_masked` 折成 `orc.b a0, a0`；debug 只有一行 `... into: t11: i64 = RISCVISD::ORC_B t4`。唯一差别是 `MaskedValueIsZero(LeftShiftOperand, ~Mask)`（`20134`）能不能过。

**实验 5：known bits 决定 `SLLW`，demanded bits 顺手删 `and`**

```bash
cat > sllw.ll <<'EOF'
define i64 @sllw_masked(i64 %x, i64 %n0) {
  %m = and i64 %x, 4294967295
  %n = and i64 %n0, 31
  %s = shl i64 %m, %n
  %t = trunc i64 %s to i32
  %r = sext i32 %t to i64
  ret i64 %r
}
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 sllw.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 -debug-only=dagcombine sllw.ll -o /dev/null 2>&1 | grep -E "SLLW|Replacing"
```

实测汇编只有 `sllw a0, a0, a1` + `ret`；debug：

```
Replacing.2 t6: i64 = and t2, Constant:i64<4294967295>
 ... into: t17: i64 = RISCVISD::SLLW t2, t8
Replacing.2 t8: i64 = and t4, Constant:i64<31>
```

去掉 `%n = and %n0, 31`，输出立刻变成 `sll` + `sext.w`：`countMaxActiveBits() <= 5` 不再成立。

**实验 6：pre-promote 的 `bext`（`i32` + `Zbs`）**

```bash
cat > bext.ll <<'EOF'
define i32 @bext(i32 %x, i32 %y) { %s = lshr i32 %x, %y  %r = and i32 %s, 1  ret i32 %r }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+zbs -O2 bext.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+m       -O2 bext.ll -o -
```

实测：`+zbs` 给 `bext a0, a0, a1`；否则 `srlw` + `andi`。这条是**真的** `performANDCombine`（`20576-20603`），只在 pre-legalize 有效。

**实验 7（负结果，实测）：RVV `vslideup`/`vslidedown` 不触发 `performVSlideUpDownCombine`**

```bash
cat > slide.ll <<'EOF'
declare <vscale x 4 x i32> @llvm.riscv.vslidedown.nxv4i32(<vscale x 4 x i32>, <vscale x 4 x i32>, i64, i64, i64)
declare <vscale x 4 x i32> @llvm.riscv.vslideup.nxv4i32(<vscale x 4 x i32>, <vscale x 4 x i32>, i64, i64, i64)
define <vscale x 4 x i32> @slide_const(<vscale x 4 x i32> %val) {
  %down = call <vscale x 4 x i32> @llvm.riscv.vslidedown.nxv4i32(<vscale x 4 x i32> undef, <vscale x 4 x i32> %val, i64 2, i64 4, i64 1)
  %up = call <vscale x 4 x i32> @llvm.riscv.vslideup.nxv4i32(<vscale x 4 x i32> %val, <vscale x 4 x i32> %down, i64 2, i64 4, i64 1)
  ret <vscale x 4 x i32> %up
}
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 slide.ll -o -
$T/bin/llc -mtriple=riscv64 -mattr=+v -O2 -debug-only=dagcombine slide.ll -o /dev/null 2>&1 | grep -c VSLIDE
```

实测：输出仍是 `vsetivli zero, 4, e32, m2, ta, ma` + `vslidedown.vi` + `vslideup.vi`；`VSLIDE` 计数为 **0**，只有 `llvm.riscv.vslideup/vslidedown` intrinsic 节点。原因：这两个 intrinsic 在 `RISCVInstrInfoVPseudos.td:7372` 有直接 pattern，不经过 `getVSlideup`/`getVSlidedown`（`4074`/`4062`），所以 `performVSlideUpDownCombine`（`23532`）没有输入。**未实测**的是「用 `BUILD_VECTOR`/shuffle 构造真正触发 `VSLIDE*_VL` 的 IR」——需另造 shuffle 形状，本章未做。

> `-print-before/after=dagcombine` 在本树**无效**（`dagcombine` 不是 pass 名，命令静默无输出）；`-mdebug-pass` 也不是 llc 选项（实测报 `Unknown command line argument '-mdebug-pass=Structure'`，提示 `--debug-pass=Structure`）。可靠开关：`-debug-only=dagcombine`、`-debug-only=isel`、`-debug-only=riscv-isel`（`DEBUG_TYPE` 见 `RISCVISelDAGToDAG.cpp:29`）。

---

## 16.12 踩雷集锦

1. **在 `isBeforeLegalize()` 下建 `RISCVISD::*`。** 那时 `Select` 还早、操作合法化没跑；轻则后续 legalize 把形状改回去，重则 `Select` 阶段直接报 `Cannot select:`（`SelectionDAGISel.cpp:4598`）。要造目标节点，用 `isAfterLegalizeDAG()` 门住。
2. **把 `!isBeforeLegalize()` 当成「已 post-legalize」。** 它只表示「过了 type legalization」。`transformAddShlImm` 的调用点（`20010`）就是这个含义，所以它在 vector-legalize 后、`LegalizeDAG` 前也会跑。
3. **注册漏了，怪 combine 不生效。** generic opcode 必须先 `setTargetDAGCombine`（`TargetLowering.h:2010`）；但 target 节点（`opcode >= ISD::BUILTIN_OP_END`）**不注册也会被调用**（`DAGCombiner.cpp:2197`），所以两种情况要分开查。
4. **改完 combine 只跑 `llc -O2`。** `DAGCombiner` 里大量 generic 折叠用 `OptLevel == CodeGenOptLevel::None` 直接关掉（如 `2308`、`10107`），所以 `-O0` 与 `-O2` 不是同一套规则；RISC-V 自己的 `transformAddShlImm` 又被阶段门挡着。回归要覆盖 `-O0`/`-O2` 两档。
5. **known bits 答「已知 1」。** `CZERO_EQZ` 的注释（`27351-27357`）就是前车之鉴：结果要么全 0、要么等于 op0，只能传播 zero，不能传播 one。多答一个 one，`(x | y)` / `icmp` 就会折成常量。
6. **`ComputeNumSignBitsForTargetNode` 返回过大的数。** `SLLW` 返回 33 是「32 位结果 + 符号位」；若你给 `ROLW` 之类的 32 位结果返回 64，`sext`/`ashr` 会被误删。
7. **忘记 poison/flags。** combine 改写必须保持 poison 语义：`DAG.getFreeze(...)`（`21738`）不是装饰；`SDNodeFlags::Disjoint`（`20647`）也不是注释。`canCreateUndefOrPoisonForTargetNode`（`27665`）答错同样会 miscompile。

---

## 16.13 本章重点整理

- DAGCombiner 有 **4 次运行 / 3 个语义阶段**：`BeforeLegalizeTypes`（977）、`AfterLegalizeTypes`（1023）、`AfterLegalizeVectorOps`（1077）、`AfterLegalizeDAG`（1117）。
- `isBeforeLegalize()` 只等于 `BeforeLegalizeTypes`；`isAfterLegalizeDAG()` 是 `Level >= AfterLegalizeDAG`；`isCalledByLegalizer()` 是 LegalizeDAG 内部回调。三者互不覆盖。
- 类型强制的机器证据是 `SelectionDAGISel.cpp:1013` 的 `NewNodesMustHaveLegalTypes = true`（在 `combine1` 之后设置）。
- 注册表 14 次调用、60 个唯一 opcode；但 target 节点不需要注册（`DAGCombiner.cpp:2197` 的短路）。
- known bits 三件套 + 一个 poison hook：`27318` / `27536` / `27632` / `27665`；默认值都是保守正确，case 才是放宽。
- 同一助记符可能有三个入口：直接 pattern（`Zb.td:725`）、combine 造节点再 pattern（`19244` + `Zb.td:727`）、ComplexPattern 在 ISel 造 MachineNode（`RISCVISelDAGToDAG.cpp:4311`）。
- `SimplifyDemandedLowBitsHelper`（`25395`）是「target 节点只读部分位」的统一出口，`return SDValue(N, 0)` 是重新入队的约定。
- 判据：`-debug-only=dagcombine` 有没有 `... into:`，决定你该改 combine 还是改 pattern。

---

## 16.14 自我检核

- [ ] 能不看源码说出四次 `CurDAG->Combine(...)` 的 `CombineLevel` 与行号，并解释哪一次之后 `NewNodesMustHaveLegalTypes` 打开
- [ ] 能解释 `isBeforeLegalize()` 与 `isBeforeLegalizeOps()` 的区别，以及为什么 `!isBeforeLegalize()` 不等于 post-legalize
- [ ] 能说出 `setTargetDAGCombine` 漏注册时哪些 opcode 仍会进 `PerformDAGCombine`，以及判据在 `DAGCombiner.cpp` 哪一行
- [ ] 能判断「我的 combine 不生效」属于注册缺失、阶段门不满足、形状不匹配，还是根本被 ISel pattern 抢先
- [ ] 能手写 `computeKnownBitsForTargetNode` 的一个 case，并说明 `Known.resetAll()` 与 `default: break` 的保守语义
- [ ] 能解释 `SLLW` 为什么返回 33、返回 1 或 64 各会坏在哪
- [ ] 能解释 `SimplifiedDemandedLowBitsHelper(0, 32)` 为什么会删掉上游的 `and x, 0xffffffff`
- [ ] 能举一个「known bits 闸门」导致 combine 不触发的真实例子（`orc.b`），并设计能触发的测试输入
- [ ] 能说出 `selectSHXADDOp` 为什么不归 `PerformDAGCombine` 管

---

## 16.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelLowering.cpp`** — 读 1964-2030（注册）、25386-25470（骨架 + demanded bits）、25843-26000（ADD/AND/OR/SETCC 分派）、27318-27690（known bits 三件套 + poison）。本章所有结论的原始出处；`default: break` 与 `Known.resetAll()` 决定了「不写就是保守」。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`** — 读 29-34（`riscv_shl_add`）、520-542（`BEXT`/`BSETI`/`BCLRI` pattern）、715-760（`zext.h` 与 `ShxAddPat`）。16.9 的三个「不是 combine」反例全在这几十行里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`** — 读 3630（`SelectAddrRegImm`）、4311（`selectSHXADDOp`）、4446（`selectSHXADD_UWOp`）。`selectSHXADDOp` 在 ISel 期间直接 `getMachineNode`，是「combine 依赖选择阶段」的唯一形态。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp`** — 读 1865（`Run`）、2196-2204（target 分派短路）。把「注册」与「调用」分开，是排查 combine 不生效的第一步。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`** — 读 977、1013、1023、1077、1117。三次机会的顺序与 `NewNodesMustHaveLegalTypes` 的时机都在这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/include/llvm/CodeGen/TargetLowering.h`** — 读 2010（`hasTargetDAGCombine`）、4600/4642/4669（hook 契约）、4746-4762（四个谓词）。谓词定义比任何二手描述都准，尤其 `==` 与 `>=` 的区别。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/`** — 读 `zba*`/`zbb*`/`zbs*` 的 RUN 行，再对照 CHECK 找「哪条指令来自哪个 combine」。改 combine 时的第一道回归。

→ 下一章：[Ch 17 — RVV lowering](./17-rvv-lowering.md)
