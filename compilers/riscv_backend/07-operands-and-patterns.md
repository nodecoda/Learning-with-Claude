# Ch 07 操作数与 Pattern：为什么这条指令没被选上

> 目标：把“操作数类 + DAG pattern + ComplexPattern + 谓词”连成一条可排查的链。读完本章，你看到一个 `PatGprGpr<add, ADD>` 时，能说出它匹配什么类型、绑定哪些操作数、在什么 subtarget 上有效；当 llc 选了别的指令或根本没选时，你能按类型、谓词、操作数、复杂度四个方向定位。

> 环境：LLVM 23.1.4 预编译版，Windows x86_64。源码锚点来自同版本 sparse checkout；`-print-records` 和 `-gen-dag-isel` 产物来自本机重跑。`llc -debug-only=isel` 在本机不可用，本文会给出真实报错和替代路径。

## 为什么

SelectionDAG 的选择过程经常被描述成“模式匹配”。这个说法容易让人以为 pattern 是一组字符串规则：写 `(add GPR, GPR)`，看到 `add` 就选 `ADD`。实际比这复杂。

一条 pattern 至少包含四层信息：

1. **节点形状**：`(add ...)`、`(load ...)`、`(or (shl ...) ...)`，这是 DAG 的语法。
2. **值类型**：`XLenVT`、`i32`、`i64`、`iPTR`，这决定 RV32/RV64 和标量/向量。
3. **操作数约束**：`GPR`、`GPRNoX0`、`simm12_lo`、`uimm5`、`AddrRegImm`，这决定寄存器类、立即数范围、地址模式。
4. **谓词**：`IsRV64`、`HasStdExtM`、`HasOneUse`、`hasAllWUsers`、`hasAllHUsers`，这决定 subtarget 和上下文。

任何一层不满足，pattern 就不会选。更麻烦的是，失败可能发生在更早的 legalization：节点已经被扩成库调用或别的 DAG，pattern 根本看不到它。于是“为什么没选上”不能只查 pattern 文件，要按流水线顺序查。

本章用 `ADD` 作为主线。它是最简单的 R 型指令之一，但已经把类型、操作数、谓词、复杂度、匿名 Pattern、matcher 排序都串起来了。理解 `ADD` 之后，再看 `LdPat`/`StPat` 的 `AddrRegImm`，就能理解为什么地址模式必须用 C++ 的 `ComplexPattern`。

## 主线

### 7.1 Operand、RegisterClass、DAGOperand：三种“操作数”

RISC-V 的 `.td` 里，`GPR`、`simm12`、`uimm5` 都出现在 `(ins ...)` 里，但它们不是同一种东西。

- `GPR` 是 `RegisterClass`，记录头是 `DAGOperand RegisterClassLike RegisterClass RISCVRegisterClass GPRRegisterClass`。它约束“这个位置必须是一个通用寄存器”，并参与寄存器分配。
- `simm12` 是 `Operand` + `ImmLeaf`，记录头是 `DAGOperand Operand RISCVOp RISCVSImmOp SDPatternOperator PatFrags PatFrag ImmLeaf RISCVSImmLeafOp`。它约束“这个位置必须是一个 12 位有符号立即数”，同时让选择器能匹配立即数。
- `AddrRegImm` 是 `ComplexPattern`，记录头是 `ComplexPattern`。它不是单个操作数，而是“一个地址表达式可以拆成 base + offset 两个结果”，由 C++ 函数 `SelectAddrRegImm` 判断。

`Operand` 基类在 `Target.td` 第 1115 行，`RegisterClass` 在第 292 行。`Operand` 的字段决定汇编器、反汇编器、打印器怎么处理这个位置：

| 字段 | 作用 | `simm12` 的值 |
| --- | --- | --- |
| `OperandType` | MC 层的操作数类型 | `OPERAND_SIMM12` |
| `ParserMatchClass` | 汇编器用哪个类匹配 | `SImmAsmOperand<12>` 的实例 |
| `EncoderMethod` | 编码时怎么取值 | `getImmOpValue` |
| `DecoderMethod` | 解码时怎么构造 | `decodeSImmOperand<12>` |
| `PrintMethod` | 打印时怎么输出 | `printOperand` |
| `MCOperandPredicate` | MC 层是否接受这个值 | `isInt<12>(Imm)` |

`GPR` 没有 `EncoderMethod`，因为寄存器编码走 `getMachineOpValue`，不需要立即数转换。它的 `RegTypes` 包含 `XLenVT`、`XLenFVT`、`XLenVecI8VT` 等，说明同一个寄存器类可以在不同值类型下使用；具体到某条指令，`(ins GPR:$rs1)` 的类型由 pattern 的值类型进一步约束。

`uimm5`、`uimmlog2xlen`、`simm12_lo` 都是同一套路。`uimmlog2xlen` 的谓词依赖 XLEN：

```text
if (Subtarget->is64Bit())
  return isUInt<6>(Imm);
return isUInt<5>(Imm);
```

所以 `slli` 的移位量在 RV32 是 5 位、RV64 是 6 位。你在 `.td` 里看到 `uimmlog2xlen`，就知道它不是一个固定宽度的立即数。

`GPRNoX0` 是另一个常见类。它排除 `x0`，因为 `x0` 是硬连线零，不能作为某些指令的目标或基址。`c.lwsp` 的 `rd` 用 `GPRNoX0`，`c.lw` 的 `rd'` 用 `GPRC`（压缩寄存器组）。这些类的差别不是“风格”，而是编码和语义约束。

### 7.2 PatLeaf、ImmLeaf、PatFrag、ComplexPattern：四种匹配器

Pattern 家族里最容易混的是这四个。它们都能出现在 pattern 的操作数位置，但职责不同。

| 类型 | 作用 | 例子 | 生成物里的表现 |
| --- | --- | --- | --- |
| `ImmLeaf` | 匹配一个立即数，并附加 C++ 谓词 | `simm12` | `Predicate_simm12_lo` |
| `PatLeaf` | 匹配一个叶子节点，并附加 C++ 谓词 | `33signbits_node` | `Predicate_33signbits_node` |
| `PatFrag` | 给一个子图起名字，可附加谓词和 `HasOneUse` | `binop_oneuse`、`binop_allwusers` | `Predicate_anonymous_...` |
| `ComplexPattern` | 调用 C++ 选择函数，返回多个结果 | `AddrRegImm` | `OPC_CheckComplexPat` |

`ImmLeaf` 的 `Fragments` 是 `[(XLenVT imm)]`，`ImmediateCode` 是 `return isInt<12>(Imm);`。它匹配的是常量节点，不匹配任意 DAG。

`PatLeaf` 的 `Fragments` 可以是一个具体的 DAG，例如 `33signbits_node`：

```tablegen
def 33signbits_node : PatLeaf<(i64 GPR:$src), [{
  return CurDAG->ComputeNumSignBits(Op) > 32;
}]>;
```

它匹配一个 `i64` 的 `GPR`，但只有当这个值的符号位数大于 32 时才成立。真实记录：

```text
def 33signbits_node {	// SDPatternOperator PatFrags PatFrag PatLeaf
  dag Fragments = [(i64 GPR:$src)];
  code PredicateCode = [{
  return CurDAG->ComputeNumSignBits(Op) > 32;
}];
}
```

`PatFrag` 用来给子图起名字。`binop_oneuse` 的定义：

```tablegen
class binop_oneuse<SDPatternOperator operator>
    : PatFrag<(ops node:$A, node:$B),
              (operator node:$A, node:$B)> {
  let HasOneUse = 1;
}
def and_oneuse : binop_oneuse<and>;
def mul_oneuse : binop_oneuse<mul>;
```

`HasOneUse = 1` 会变成 matcher 里的 use-count 检查。一个 `mul` 如果有两个用户，`mul_oneuse` 就不匹配，哪怕节点形状和类型都对。

`binop_allwusers` 是更复杂的 `PatFrag`：

```tablegen
class binop_allwusers<SDPatternOperator operator>
    : PatFrag<(ops node:$lhs, node:$rhs), (i64 (operator node:$lhs, node:$rhs)),
              [{ return hasAllWUsers(N); }]> {
  let GISelPredicateCode = [{ return hasAllWUsers(MI); }];
}
```

它要求 `i64` 的 `add`/`sub`/`mul`/`shl` 的所有用户都只使用低 32 位，这样才能安全地选 `ADDW`/`SUBW`/`MULW`/`SLLW`。这就是为什么 `i32` 的 `add` 在 RV64 上会选 `ADDW`，而 `i64` 的 `add` 选 `ADD`：不是靠助记符，而是靠 `hasAllWUsers`。

`ComplexPattern` 是 C++ 逃生口。`AddrRegImm` 的定义只有一行：

```tablegen
def AddrRegImm : ComplexPattern<iPTR, 2, "SelectAddrRegImm">;
```

真实记录：

```text
def AddrRegImm {	// ComplexPattern
  ValueType Ty = iPTR;
  int NumOperands = 2;
  string SelectFunc = "SelectAddrRegImm";
  list<SDNode> RootNodes = [];
  int Complexity = -1;
  bit WantsRoot = 0;
  bit WantsParent = 0;
}
```

`NumOperands = 2` 表示它返回两个结果。`SelectFunc = "SelectAddrRegImm"` 是 C++ 函数名。`Complexity = -1` 表示复杂度由 backend 估算。它不出现在 `(ins ...)` 里，而是作为 pattern 的一个“子模式”出现：

```tablegen
class LdPat<PatFrag LoadOp, RVInst Inst, ValueType vt = XLenVT,
            ValueType PtrVT = XLenVT>
    : Pat<(vt (LoadOp (AddrRegImm (PtrVT BasePtr:$rs1), simm12_lo:$imm12))),
          (Inst BasePtr:$rs1, simm12_lo:$imm12)>;
```

这里 `AddrRegImm` 吃掉整个地址表达式，返回 `BasePtr` 和 `simm12_lo` 两个绑定；后面的 `Inst` 用这两个绑定生成指令。`LdPat`/`StPat` 在 `RISCVInstrInfo.td` 第 2014/2029 行。

### 7.3 DAG pattern 语法：从 `(add GPR:$rs1, GPR:$rs2)` 读起

`PatGprGpr` 是最小的二元寄存器 pattern：

```tablegen
class PatGprGpr<SDPatternOperator OpNode, RVInst Inst, ValueType vt = XLenVT>
    : Pat<(vt (OpNode (vt GPR:$rs1), (vt GPR:$rs2))), (Inst GPR:$rs1, GPR:$rs2)>;
```

拆开看：

- `(vt (OpNode ...))`：结果类型是 `vt`，默认 `XLenVT`。
- `(OpNode (vt GPR:$rs1), (vt GPR:$rs2))`：两个操作数都是 `vt` 类型的 `GPR`，分别绑定到 `$rs1`、`$rs2`。
- `(Inst GPR:$rs1, GPR:$rs2)`：生成 `Inst` 指令，把两个绑定放进对应位置。

`def : PatGprGpr<add, ADD>;` 展开成匿名记录：

```text
def anonymous_15183 {	// Pattern Pat PatGprGpr
  dag PatternToMatch = (XLenVT (add (XLenVT GPR:$rs1), (XLenVT GPR:$rs2)));
  list<dag> ResultInstrs = [(ADD GPR:$rs1, GPR:$rs2)];
  list<Predicate> Predicates = [];
  int AddedComplexity = 0;
}
```

`PatternToMatch` 是源，`ResultInstrs` 是目标。`Predicates` 为空，说明这条规则不依赖 subtarget。`AddedComplexity = 0`，而 matcher 里算出的复杂度是 3。

带立即数的版本：

```tablegen
class PatGprImm<SDPatternOperator OpNode, RVInst Inst, SDPatternOperator ImmType,
                ValueType vt = XLenVT>
    : Pat<(vt (OpNode (vt GPR:$rs1), ImmType:$imm)),
          (Inst GPR:$rs1, ImmType:$imm)>;
class PatGprSimm12<SDPatternOperator OpNode, RVInstI Inst>
    : PatGprImm<OpNode, Inst, simm12_lo>;
```

`PatGprSimm12<add, ADDI>` 生成 `(add GPR, simm12_lo)` -> `ADDI`。这里的 `simm12_lo` 不是普通操作数，而是 `ImmLeaf`；它既约束立即数范围，又把立即数值绑定到 `$imm`。

模式可以嵌套。`riscv_add_like` 把 `or` 和 `add` 合成一个 `PatFrags`：

```tablegen
def riscv_add_like : PatFrags<(ops node:$lhs, node:$rhs),
                              [(riscv_or_disjoint node:$lhs, node:$rhs),
                               (add  node:$lhs, node:$rhs)]>;
```

`PatFrags` 的每个 fragment 都会生成候选；`riscv_or_disjoint` 还带 `orDisjoint(N)` 谓词。这样 `(or r, imm)` 在低位不冲突时可以走 `ADDI`，提高可压缩性。读这种 pattern 时，先展开 `PatFrags`，再看每个 fragment 的谓词。

### 7.4 多结果 pattern：为什么地址模式必须用 ComplexPattern

一个地址表达式可能长成很多样子：

- `frameindex`：栈帧索引。
- `add base, constant`：基址加常量偏移。
- `global address`：全局符号的高低位。
- `add (shl index, scale), base`：基址加变址。
- `add base, large_imm`：超出 12 位但还能拆成 `ADDI + load/store` 的偏移。

这些不是单纯的语法结构，需要看对齐、符号、是否 `ADD_LO`、是否是 frame index。DAG pattern 语言表达不了这些语义判断，所以用 `ComplexPattern` 调到 C++。

`SelectAddrRegImm` 在 `RISCVISelDAGToDAG.cpp` 第 3480 行。它的真实逻辑（节选）：

```cpp
bool RISCVDAGToDAGISel::SelectAddrRegImm(SDValue Addr, SDValue &Base,
                                         SDValue &Offset) {
  if (SelectAddrFrameIndex(Addr, Base, Offset))
    return true;

  SDLoc DL(Addr);
  MVT VT = Addr.getSimpleValueType();

  if (Addr.getOpcode() == RISCVISD::ADD_LO) {
    bool CanFold = true;
    if (auto *GA = dyn_cast<GlobalAddressSDNode>(Addr.getOperand(1))) {
      const DataLayout &DL = CurDAG->getDataLayout();
      Align Alignment = commonAlignment(
          GA->getGlobal()->getPointerAlignment(DL), GA->getOffset());
      if (!areOffsetsWithinAlignment(Addr, Alignment))
        CanFold = false;
    }
    if (CanFold) {
      Base = Addr.getOperand(0);
      Offset = Addr.getOperand(1);
      return true;
    }
  }

  if (CurDAG->isBaseWithConstantOffset(Addr)) {
    int64_t CVal = cast<ConstantSDNode>(Addr.getOperand(1))->getSExtValue();
    if (isInt<12>(CVal)) {
      Base = Addr.getOperand(0);
      ...
      Offset = CurDAG->getSignedTargetConstant(CVal, DL, VT);
      return true;
    }
  }

  // Handle ADD with large immediates.
  if (Addr.getOpcode() == ISD::ADD && isa<ConstantSDNode>(Addr.getOperand(1))) {
    int64_t CVal = cast<ConstantSDNode>(Addr.getOperand(1))->getSExtValue();
    assert(!isInt<12>(CVal) && "simm12 not already handled?");
    if (CVal >= -4096 && CVal <= 4094) {
      int64_t Adj = CVal < 0 ? -2048 : 2047;
      ...
    }
  }
  ...
}
```

这段代码解释了为什么不能用简单 pattern：

- `SelectAddrFrameIndex` 先处理 frame index；普通 pattern 没有“这个值是不是栈帧”的谓词。
- `RISCVISD::ADD_LO` 要检查全局符号对齐，决定能不能折叠偏移。
- `isBaseWithConstantOffset` + `isInt<12>` 处理基址加 12 位偏移。
- `CVal` 在 `[-4096, 4094]` 时，还要拆成 `ADDI + load/store`，这涉及两条指令的选择，不是单条 pattern 能表达的。

生成物里对应的是：

```cpp
/*  87116*/    OPC_CheckComplexPat, /*CP*/12, /*#*/1, // SelectAddrRegImm:$ #2 #3
```

`CP 12` 是 `AddrRegImm` 在 ComplexPattern 表里的索引，`#2 #3` 是两个结果。`OPC_CheckComplexPat` 会调用 C++ 选择函数；成功则把结果绑定到后续操作数，失败则整条 pattern 失败。

其他 ComplexPattern 也遵循同一模式：

- `SelectAddrRegImm26`（第 3590 行）处理 26 位偏移。
- `SelectAddrRegImm9`（第 3652 行）处理 9 位偏移。
- `SelectAddrRegImmLsb00000`（第 3679 行）要求低 5 位为 0。
- `SelectAddrRegRegScale`（第 3800 行）返回 3 个结果：base、index、scale。
- `selectShiftMask`（第 3915 行）处理移位量掩码。
- `selectSETCC`（第 3992 行）把 `setcc` 改写成目标条件码。

这些函数的共同点是：它们需要看节点内容、对齐、位模式或 subtarget，无法用纯语法 pattern 表达。

### 7.5 Pattern 顺序、复杂度与覆盖

源码顺序不是 matcher 顺序。`ADDW` 的 pattern 在 `RISCVInstrInfo.td` 第 2255 行：

```tablegen
def : PatGprGpr<binop_allwusers<add>, ADDW>;
```

`ADD` 的 pattern 在第 1470 行。源码里 `ADD` 在前，但 matcher 里 `ADDW` 先试：

```cpp
/*  25735*/      OPC_CheckPredicate, 20, // Predicate_anonymous_15302
/*  25737*/      OPC_CheckPatternPredicate, 62, // (Subtarget->is64Bit())
/*  25739*/      OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::ADDW), 0,
             // Src: (add ... )<<P:Predicate_anonymous_15302>> - Complexity = 4
             // Dst: (ADDW ...)
/*  25747*/     0, // End of Scope
/*  25748*/    /*Scope*/ 9, // ->25758
/*  25749*/     OPC_CheckTypeByHwMode0/*{(*:i32),(m1:i64)}*/,
/*  25750*/     OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::ADD), 0,
             // Src: (add ... ) - Complexity = 3
             // Dst: (ADD ...)
```

排序由几个因素决定：

- **复杂度**：更具体的 pattern 复杂度更高，先试。`ADDW` 的 4 比 `ADD` 的 3 高。
- **谓词**：带 `hasAllWUsers` 的规则先试，失败后再落到通用规则。
- **类型/HwMode**：RV32/RV64、标量/向量的类型检查会影响分支。
- **`AddedComplexity`**：可以在 `.td` 里手动加，用于覆盖默认排序。
- **`Requires` / `Predicates`**：不满足的规则不会进入候选。

`ADD` 的 `isCommutable = 1` 还会让 matcher 尝试交换两个操作数。对 `add` 来说，交换不影响语义；对 `sub` 来说，`isCommutable = 0`，不能交换。这个属性来自 `ALU_rr` 的 `Commutable` 参数，写在 `def ADD : ALU_rr<..., Commutable=1>` 里。

覆盖关系也要注意。一个节点可能同时匹配多条规则：

- `add GPR, GPR` 匹配 `ADD`。
- `add GPR, simm12` 匹配 `ADDI`。
- `add` 的低 32 位用户匹配 `ADDW`。
- `add` 带大立即数可能先被 `AddiPair` 拆成两条 `ADDI`。
- 向量 `add` 匹配 RVV 的 `PseudoVADD`。

最终选哪条，取决于类型、谓词、复杂度和 legalization 后的节点形状。不要用“源码里谁先写”来判断。

### 7.6 为什么有些节点必须 C++ 选

纯 DAG pattern 能表达的是“树形结构 + 类型 + 简单谓词”。以下情况必须 C++：

1. **地址模式**：base+offset、frame index、全局符号对齐、变址缩放。`SelectAddrRegImm` 是典型。
2. **条件码重写**：`setcc` 的谓词和 RISC-V 分支条件不是一一对应，需要 `selectSETCC`。
3. **移位量掩码**：RV32/RV64 的移位量位宽不同，`selectShiftMask` 要按 XLEN 处理。
4. **向量 splat/标量广播**：`selectVSplat`、`selectVSplatSimm5` 要判断常量是否可广播。
5. **内联汇编内存操作数**：`SelectInlineAsmMemoryOperand` 要解析 `%lo` 等修饰符。
6. **自定义节点**：`RISCVDAGToDAGISel::Select`（第 1100 行）里对 `RISCVISD::*` 节点的特殊处理。

这不是“TableGen 不够强”，而是这些判断依赖 C++ 数据结构和 subtarget 状态。Pattern 是声明式的，C++ 是命令式的；两者边界由 `ComplexPattern` 和 `Select` 函数划定。

### 7.7 调试“为什么没选上”

先给结论：在本机的 Optimized build 上，`-debug-only=isel` 不可用。真实报错：

```text
llc.exe: Unknown command line argument '-debug-only=isel'.  Try: '...\llc.exe --help'
llc.exe: Did you mean '--debug-pass=isel'?
```

`-debug-pass=isel` 也不是你要的：

```text
llc.exe: for the --debug-pass option: Cannot find option named 'isel'!
```

所以不要依赖 debug 日志。用下面三步替代。

**第一步：看最终选了什么。** `-stop-after=finalize-isel` 输出 MIR：

```powershell
llc -mtriple=riscv64 -O2 -stop-after=finalize-isel add.ll -o -
```

真实输出（节选）：

```text
body:             |
  bb.0 (%ir-block.0):
    liveins: $x10, $x11
    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %2:gpr = ADD %0, %1
    $x10 = COPY %2
    PseudoRET implicit $x10
```

看到 `ADD`，说明 pattern 至少选到了基础规则。注意本机 MIR 头里仍会打印 `selected: false`，但 body 已经是 `ADD`；判断是否选中看 body 的指令，不看这个布尔字段。如果看到 `PseudoCALL` 或 `tail __muldi3`，说明节点在 legalization 阶段已经变成库调用，pattern 根本没参与。

**第二步：看 pattern 是否存在。** 在 `RISCVGenDAGISel.inc` 里搜 `Src:`/`Dst:`：

```powershell
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern '// Src: \(add:' -Context 0,2
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern 'TARGET_VAL\(RISCV::ADD\)' -Context 3,3
```

如果 `Src` 里的类型、谓词和你的节点对不上，问题在 pattern 覆盖，不在 llc。如果根本没有 `Src`，说明这条规则没被生成，回去查 `.td` 和 generator。

**第三步：看谓词和特性。** 用 `-mattr` 切换特性，看行为是否变化。真实例子：

```powershell
# +m：选 mul
llc -mtriple=riscv64 -mattr=+m -O2 mul.ll -o -

# -m：legalization 变成库调用
llc -mtriple=riscv64 -mattr=-m -O2 mul.ll -o -
```

`+m` 的输出：

```asm
mul a0, a0, a1
ret
```

`-m` 的输出：

```asm
tail __muldi3
```

这不是 pattern 写错了，而是 `mul i64` 在没有 `+m` 时被 legalization 扩展成 `__muldi3` 调用。`HasStdExtM` 谓词保护了 `MUL` 的 pattern；特性关闭，候选集里就没有 `MUL`。

再看类型。`i32` 的 `add` 在 RV64 上选 `ADDW`：

```text
%2:gpr = ADDW %0, %1
```

`i64` 的 `add` 选 `ADD`：

```text
%2:gpr = ADD %0, %1
```

原因是 `binop_allwusers<add>` 匹配“结果只被低 32 位使用”的 `i64 add`，而 `PatGprGpr<add, ADD>` 匹配通用 `XLenVT`。类型和使用者信息都在 pattern 里，不看 DAG 就会误判。

常见的失败顺序可以压成一张表：

| 现象 | 优先检查 | 真实例子 |
| --- | --- | --- |
| 变成库调用 | legalization / 特性 | `-mattr=-m` -> `tail __muldi3` |
| 选了更窄的 W 指令 | `hasAllWUsers` / 类型 | `i32 add` -> `ADDW` |
| 选了 `ADDI` 而不是 `ADD` | 立即数 pattern 优先 | `add GPR, simm12` |
| 地址没有折叠 | `ComplexPattern` | `SelectAddrRegImm` 对齐检查 |
| 规则完全没出现 | `.td`/generator | `-gen-dag-isel` 无 `Src` |
| 规则出现但没命中 | 类型/谓词/操作数类 | `GPRNoX0`、`uimm5`、`HasOneUse` |

## 可跑实验

### 实验 1：读 ADD 的 Operand 和 Pattern 记录

目标：确认 `ADD` 的操作数列表、空 Pattern 和匿名匹配记录。

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$out = ".scratch/riscv-tblgen-ev"
& "$bin/llvm-tblgen.exe" -I $inc -I $td -print-records "$td/RISCV.td" -o "$out/RISCV.records.td"
Select-String -LiteralPath "$out/RISCV.records.td" -Pattern '^def ADD\s*\{' -Context 0,24
Select-String -LiteralPath "$out/RISCV.records.td" -Pattern 'PatternToMatch = \(XLenVT \(add \(XLenVT GPR:\$rs1\), \(XLenVT GPR:\$rs2\)\)\)' -Context 0,4 | Select-Object -First 1
```

预期：`ADD` 的 `OutOperandList = (outs GPR:$rd)`、`InOperandList = (ins GPR:$rs1, GPR:$rs2)`、`Pattern = []`；匿名记录 `anonymous_15183` 的 `PatternToMatch` 是 `(XLenVT (add ...))`。

### 实验 2：看 ComplexPattern 的真实记录

目标：确认 `AddrRegImm` 返回两个结果，并调用 `SelectAddrRegImm`。

```powershell
Select-String -LiteralPath "$out/RISCV.records.td" -Pattern '^def AddrRegImm\s*\{' -Context 0,9
Select-String -LiteralPath "$out/RISCV.records.td" -Pattern '^def simm12\s*\{' -Context 0,16
```

预期：`AddrRegImm` 的 `NumOperands = 2`、`SelectFunc = "SelectAddrRegImm"`；`simm12` 的 `OperandType = "OPERAND_SIMM12"`、`ImmediateCode = "return isInt<12>(Imm);"`。

### 实验 3：看 matcher 里的 ComplexPattern 调用

目标：把 `.td` 的 `AddrRegImm` 和生成的 `OPC_CheckComplexPat` 对上。

```powershell
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern 'SelectAddrRegImm:\$' -Context 2,2 | Select-Object -First 5
```

预期：看到 `OPC_CheckComplexPat, /*CP*/12, /*#*/1, // SelectAddrRegImm:$ #2 #3`。`CP 12` 是索引，`#2 #3` 是两个结果。

### 实验 4：观察 ADDW 与 ADD 的顺序

目标：确认 matcher 先试带谓词的 `ADDW`，再落到通用 `ADD`。

```powershell
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern 'TARGET_VAL\(RISCV::ADDW\)|TARGET_VAL\(RISCV::ADD\)' -Context 4,4 | Select-Object -First 30
```

预期：`ADDW` 先出现，带 `Predicate_anonymous_15302` 和 `Subtarget->is64Bit()`；随后是 `ADD` 的 `OPC_CheckTypeByHwMode0` 和 `Complexity = 3`。

### 实验 5：用 MIR 看最终选择

目标：在无法使用 `-debug-only=isel` 的构建上，仍能看到 `ADD`/`ADDW`。

```powershell
$ll = @(
  'target triple = "riscv64-unknown-elf"'
  'define i64 @f(i64 %a, i64 %b) {'
  '  %x = add i64 %a, %b'
  '  ret i64 %x'
  '}'
)
[IO.File]::WriteAllText("$env:TEMP\add64.ll", ($ll -join "`n")+"`n", (New-Object Text.UTF8Encoding($false)))
llc -mtriple=riscv64 -O2 -stop-after=finalize-isel "$env:TEMP\add64.ll" -o -
```

预期：`%2:gpr = ADD %0, %1`。把 `i64` 改成 `i32`，会得到 `ADDW`。

### 实验 6：失败例子——`-debug-only=isel` 不可用

目标：确认调试路径，而不是盲试参数。

```powershell
llc -mtriple=riscv64 -O2 -debug-only=isel "$env:TEMP\add64.ll" -o -
```

本机真实报错：

```text
llc.exe: Unknown command line argument '-debug-only=isel'.
llc.exe: Did you mean '--debug-pass=isel'?
```

预期：返回 1。改用 `-stop-after=finalize-isel` 或 `-gen-dag-isel` 的 `Src`/`Dst` 注释。

## 源码落点

| 位置 | 符号 | 说明 |
| --- | --- | --- |
| `RISCVInstrInfo.td:528` | `AddrRegImm` | 2 结果 ComplexPattern |
| `RISCVInstrInfo.td:1387` | `class PatGprGpr` | 二元寄存器 pattern |
| `RISCVInstrInfo.td:1390` | `class PatGprImm` | 寄存器+立即数 pattern |
| `RISCVInstrInfo.td:1394` | `class PatGprSimm12` | 12 位有符号立即数特化 |
| `RISCVInstrInfo.td:1401` | `assertsexti32` | PatFrag，检查 `assertsext` 类型 |
| `RISCVInstrInfo.td:1404` | `sexti16` | ComplexPattern，调用 `selectSExtBits<16>` |
| `RISCVInstrInfo.td:1470` | `PatGprGpr<add, ADD>` | `ADD` 的匹配规则 |
| `RISCVInstrInfo.td:1485` | `riscv_or_disjoint` | `or` 的 PatFrag 谓词 |
| `RISCVInstrInfo.td:1490` | `riscv_add_like` | `or`/`add` 的 PatFrags |
| `RISCVInstrInfo.td:1517` | `shiftMaskXLen` | 移位量 ComplexPattern |
| `RISCVInstrInfo.td:2014` | `class LdPat` | 使用 `AddrRegImm` 的加载 pattern |
| `RISCVInstrInfo.td:2029` | `class StPat` | 使用 `AddrRegImm` 的存储 pattern |
| `RISCVInstrInfo.td:2255` | `PatGprGpr<binop_allwusers<add>, ADDW>` | W 指令选择 |
| `RISCVISelDAGToDAG.cpp:1100` | `RISCVDAGToDAGISel::Select` | 自定义节点选择入口 |
| `RISCVISelDAGToDAG.cpp:3328` | `SelectAddrFrameIndex` | 栈帧地址 |
| `RISCVISelDAGToDAG.cpp:3480` | `SelectAddrRegImm` | base+offset 地址选择 |
| `RISCVISelDAGToDAG.cpp:3590` | `SelectAddrRegImm26` | 26 位偏移 |
| `RISCVISelDAGToDAG.cpp:3652` | `SelectAddrRegImm9` | 9 位偏移 |
| `RISCVISelDAGToDAG.cpp:3679` | `SelectAddrRegImmLsb00000` | 低 5 位为 0 |
| `RISCVISelDAGToDAG.cpp:3800` | `SelectAddrRegRegScale` | 变址+缩放，3 结果 |
| `RISCVISelDAGToDAG.cpp:3915` | `selectShiftMask` | 移位量掩码 |
| `RISCVISelDAGToDAG.cpp:3992` | `selectSETCC` | 条件码重写 |
| `RISCVGenDAGISel.inc:12952` | `Predicate_anonymous_15302` | `ADDW` 的 `hasAllWUsers` 谓词 |
| `RISCVGenDAGISel.inc:12961` | `TARGET_VAL(RISCV::ADD)` | `ADD` 的生成动作 |
| `RISCVGenDAGISel.inc:44926` | `SelectAddrRegImm:$ #2 #3` | `OPC_CheckComplexPat` 实例 |

## 常见坑

1. **把 `GPR` 当 `Operand` 子类。** `GPR` 是 `RegisterClass`，`simm12` 才是 `Operand` + `ImmLeaf`。两者都能出现在 `ins` 里，但约束方式不同。

2. **把 `PatFrag` 当 `PatLeaf`。** `PatFrag` 匹配子图并可以有 `HasOneUse`；`PatLeaf` 匹配叶子并带 C++ 谓词。`binop_oneuse` 是 PatFrag，`33signbits_node` 是 PatLeaf。

3. **以为 `ComplexPattern` 只返回一个值。** `AddrRegImm` 返回 2 个，`AddrRegRegScale` 返回 3 个；生成的 `OPC_CheckComplexPat` 后面会跟多个结果编号。

4. **忽略 `HasOneUse`。** `mul_oneuse`、`and_oneuse` 要求节点只有一个用户；多用户时 pattern 不匹配，即使形状和类型都对。

5. **忽略 `hasAllWUsers`。** `ADDW` 不是“i32 的 add 一定选”，而是“i64 add 的结果只被低 32 位使用时才选”。看 MIR 的类型和使用者。

6. **用源码顺序判断 matcher 顺序。** `ADDW` 在源码里位于 `ADD` 之后，matcher 里却先试。排序由复杂度、谓词、类型和 HwMode 决定。

7. **在 Optimized build 上找 `-debug-only=isel`。** 本机没有这个选项；用 `-stop-after=finalize-isel` 看 MIR，用 `-gen-dag-isel` 的 `Src`/`Dst` 看规则。

8. **把 legalization 失败当成 pattern 失败。** `-mattr=-m` 时 `mul` 变成 `__muldi3`，pattern 根本没参与。先确认节点还在不在。

9. **忘记 `Requires` 和 `Predicates` 的区别。** `Requires<[IsRV32]>` 限制 Pattern；`let Predicates = [IsRV64]` 限制指令。两者都会影响候选集，但作用的记录不同。

10. **在地址 pattern 里硬套简单树。** 全局符号对齐、frame index、大偏移拆分都要 C++；`SelectAddrRegImm` 不是“多余的代码”，而是 pattern 语言表达不了的部分。

## 自我检查

- [ ] 我能区分 `GPR`、`simm12`、`AddrRegImm` 三种“操作数”在记录里的基类。
- [ ] 我能说出 `ImmLeaf`、`PatLeaf`、`PatFrag`、`ComplexPattern` 各自的职责和一个真实例子。
- [ ] 我能把 `(add GPR:$rs1, GPR:$rs2)` 拆成节点、类型、操作数绑定和结果指令。
- [ ] 我能解释 `AddrRegImm` 为什么需要 2 个结果，以及生成物里的 `#2 #3` 是什么。
- [ ] 我能说出 `ADDW` 为什么在 matcher 里先于 `ADD`，以及 `Complexity` 的作用。
- [ ] 我能解释 `-mattr=-m` 时 `mul` 为什么变成 `tail __muldi3`。
- [ ] 我知道本机不能用 `-debug-only=isel`，并会用 `-stop-after=finalize-isel` 替代。
- [ ] 我能按“legalization -> 类型 -> 谓词 -> 操作数 -> 复杂度”的顺序排查“没选上”。

## 延伸阅读

- LLVM 后端开发指南（SelectionDAG）：<https://llvm.org/docs/CodeGenerator.html#selectiondag>
- LLVM TableGen 文档：<https://llvm.org/docs/TableGen/>
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`
- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`
- `llvm/lib/Target/RISCV/RISCVRegisterInfo.td`
- `llvm/test/CodeGen/RISCV/`（选择器和 pattern 回归测试）
- `llvm/test/CodeGen/RISCV/rv64-opt.ll` 等 MIR/汇编测试

[上一章：指令编码](./06-instruction-encoding.md) | [下一章：练习 B：给后端加一条自定义指令](./practice-b-custom-instruction.md)