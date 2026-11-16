# Ch 12 — Legalization

> **目标**：能读懂 `TargetLowering` 里的一整屏 `setOperationAction`，说清 Legal/Custom/Expand/LibCall/Promote 各自由哪个 pass 执行；能解释 MiniRV 为什么把 i8/i16/i32 提升到 i64、`*W` 指令如何补回 32 位语义，以及 i128 加法为什么内联展开而除法变成 `__divti3`。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`；本文命令均在该构建实跑。

## 为什么需要这个？

SelectionDAG 是从 IR 生成的：IR 里有 `i1/i8/i16/i32/i64/i128`、指针、`f32/f64`，还有 `select`、`va_arg`、`llvm.frameaddress` 这些目标不一定支持的节点。目标硬件只认有限的一组类型和操作，中间必须有一层“翻译规则”。

这层规则不能写成硬编码的 if-else，因为不同目标的合法集合差别巨大：x86 有 i8 寄存器，RV64 只有 64 位寄存器；RISC-V 有 M 扩展就有 `div`，没有就得调 `__divdi3`。LLVM 的做法是让目标在 `TargetLowering` 构造函数里声明一张“类型 × 操作 → 动作”的表，然后由两个通用 pass 执行：

- `LegalizeTypes`（`Type-legalized selection DAG`）：把不合法的**类型**变成合法类型。
- `LegalizeDAG`（`Legalized selection DAG`）：把不合法的**操作**展开、降为 libcall 或交给目标的 `LowerOperation`。

这一章就是这张表怎么填、两个 pass 怎么读它，以及 MiniRV 如何用最少的条目覆盖 RV64IM 的整数核心。

## 先建立直觉

先看一条 `add i32` 的完整旅程：

```text
IR:  %r = add i32 %a, %b
        │
        ▼
SelectionDAGBuilder
        │   i32 在 MiniRV 不合法（只有 i64 寄存器类）
        ▼
① LegalizeTypes ── PromoteIntegerResult(i32 → i64)
        │            动作是 Custom？先问 ReplaceNodeResults
        │            MiniRV 在这里把 add 换成 MiniRVISD::ADDW
        ▼
② LegalizeDAG ─── 操作已合法（ADDW 是目标节点），不再展开
        ▼
③ DAG Combine（优化 legalized DAG）
        ▼
④ ISel：MiniRVISD::ADDW → ADDW 指令
```

“合法”有两层，必须分开记：**类型合法**（这个 VT 能不能放进某个寄存器类）和**操作合法**（这个 opcode 在这个 VT 上有没有指令/展开规则）。MiniRV 的 i32 属于“类型不合法、但操作有 Custom 规则”，所以它在 ① 就被处理掉了，根本走不到 ②。

五个动作的含义（源码里的定义）：

```cpp
  enum LegalizeAction : uint8_t {
    Legal,      // The target natively supports this operation.
    Promote,    // This operation should be executed in a larger type.
    Expand,     // Try to expand this to other ops, otherwise use a libcall.
    LibCall,    // Don't try to expand this to other ops, always use a libcall.
    Custom      // Use the LowerOperation hook to implement custom lowering.
  };
```

| 动作 | 谁执行 | 结果长什么样 | MiniRV 例子 |
|---|---|---|---|
| `Legal` | 没人，直接进 ISel | 原样的 DAG 节点 | `mul i64` → `MUL` |
| `Promote` | `LegalizeTypes` | 用更大的类型算，再截回 | i8/i16 的算术先提到 i64 |
| `Expand` | `LegalizeTypes` 或 `LegalizeDAG` | 拆成更基础的节点；拆不出就 libcall | `select` → `select_cc`；`ctpop` → 移位/加法 |
| `LibCall` | `LegalizeDAG` | 一次 `CALL` 到运行时 | i128 除法 → `__divti3` |
| `Custom` | 目标的 `LowerOperation` / `ReplaceNodeResults` | 目标节点或任意 DAG | `MiniRVISD::ADDW`、`PseudoLA` |

## 主线 1：合法化的契约

### 合法类型从哪来

合法类型不是手写的列表，而是从**寄存器类**推出来的。MiniRV 只注册了一个：

```cpp
  addRegisterClass(MVT::i64, &MiniRV::GPRRegClass);
```

`computeRegisterProperties(STI.getRegisterInfo())` 会把 i64 标成 `TypeLegal`，把 i8/i16/i32 标成 `TypePromoteInteger`（提升到 i64），把 i128 标成 `TypeExpandInteger`（拆成两个 i64）。没有注册浮点寄存器类，所以 `f32/f64` 会走 `TypeSoftenFloat` → 软浮点 libcall（Ch 20）。这一步没有任何“i32 也能放”的余地：`GPR` 的合法 VT 只有 i64。

### 操作动作表的默认值

`TargetLowering` 内部是 `LegalizeAction OpActions[MVT::VALUETYPE_SIZE][ISD::BUILTIN_OP_END]`，构造时 `memset(OpActions, 0, ...)`，而 `Legal == 0`。所以：

> **没有显式 `setOperationAction` 的操作，默认就是 Legal。**

这是新手最容易踩的坑：以为“没设置 = 不支持”。实际相反——没设置会被当作支持，然后在 ISel 报 `Cannot select`。MiniRV 把 `CTPOP/CTLZ/CTTZ/BSWAP/BITREVERSE` 显式标成 `Expand`，就是这个原因：不标就会去选择器里找不存在的指令。

### 寻址模式也是契约

`isLegalAddressingMode` 告诉 LSR、DAG combine 和选择器“什么样的地址表达式值得保留”。MiniRV 只接受“基址 + 12 位有符号偏移”，带 index 的形式一律不合法：

```cpp
  if (AM.BaseGV || AM.ScalableOffset || !isInt<12>(AM.BaseOffs))
    return false;
  switch (AM.Scale) {
  case 0:
    break;
  case 1:
    if (!AM.HasBaseReg)
      break;
    return false;
  default:
    return false;
  }
```

配合 `isLegalICmpImmediate` / `isLegalAddImmediate`（都是 `isInt<12>`），LSR 就不会生成“基址+索引”的寻址，也就不会在 ISel 里失败。读者自己的目标如果只有 `[reg+imm]`，这三处必须一致；只改一处会出现“LSR 生成了合法 DAG 但选择器选不出”的怪状。

### 你会反复用到的查询

| 查询 | 回答什么 | 谁在用 |
|---|---|---|
| `isTypeLegal(VT)` | 该 VT 有没有寄存器类 | `LegalizeTypes`、DAG combine |
| `getTypeAction(VT)` | Promote/Expand/Soften… | `LegalizeTypes` |
| `getOperationAction(Op, VT)` | 这张表里的动作 | `LegalizeDAG` |
| `isOperationLegal(Op, VT)` | 是否恰好是 Legal | 选择器、combine |
| `getRegisterClassForType` | 该 VT 用哪个寄存器类 | 寄存器分配、COPY 生成 |
| `getSetCCResultType` | 比较结果用什么 VT | `SelectionDAGBuilder` |
| `ComputeNumSignBitsForTargetNode` | 目标节点已知的符号位 | DAG combine |

## 主线 2：走一遍 `MiniRVISelLowering` 构造函数

构造函数就是“声明这台机器会什么”。按段落读：

```cpp
  // Only XLEN is a legal integer type; i8/i16/i32 are promoted to i64. ...
  addRegisterClass(MVT::i64, &MiniRV::GPRRegClass);

  // RV64M supplies the multiply, divide and remainder instructions.
  setOperationAction(ISD::MUL, MVT::i64, Legal);
  setOperationAction(ISD::SDIV, MVT::i64, Legal);
  setOperationAction(ISD::UDIV, MVT::i64, Legal);
  setOperationAction(ISD::SREM, MVT::i64, Legal);
  setOperationAction(ISD::UREM, MVT::i64, Legal);
```

i64 的 M 扩展运算直接 Legal，因为语料里有 `mul/div/divu/rem/remu`。

```cpp
  // The *W instructions only exist for arithmetic and shifts. ...
  for (unsigned Opc :
       {ISD::ADD, ISD::SUB, ISD::SHL, ISD::SRL, ISD::SRA, ISD::MUL, ISD::SDIV,
        ISD::UDIV, ISD::SREM, ISD::UREM, ISD::AND, ISD::OR, ISD::XOR})
    setOperationAction(Opc, MVT::i32, Custom);
```

注意列表里有 `AND/OR/XOR`，但 RISC-V 没有 `andw`。它们也会走 Custom，只是 lowering 方式不同（64 位算 + 符号扩展），后面看 `LowerBits`。

```cpp
  // Narrow loads and stores are promoted to i64; the extend/truncate flavour
  // of the node selects LB/LBU/LH/LHU/LW/LWU and SB/SH/SW.
  setLoadExtAction({ISD::SEXTLOAD, ISD::ZEXTLOAD, ISD::EXTLOAD}, MVT::i64,
                   MVT::i8, Legal);
  setLoadExtAction({ISD::SEXTLOAD, ISD::ZEXTLOAD, ISD::EXTLOAD}, MVT::i64,
                   MVT::i16, Legal);
  setLoadExtAction({ISD::SEXTLOAD, ISD::ZEXTLOAD, ISD::EXTLOAD}, MVT::i64,
                   MVT::i32, Legal);
  setTruncStoreAction(MVT::i64, MVT::i8, Legal);
  setTruncStoreAction(MVT::i64, MVT::i16, Legal);
  setTruncStoreAction(MVT::i64, MVT::i32, Legal);
```

i8/i16/i32 只作为**内存宽度**存在：load 的“扩展口味”和 store 的“截断口味”是 Legal 的，于是 `load i8` + `sext` 会合成 `SEXTLOAD i64, i8`，直接选 `lb`。这使得 DAG 里不需要真的出现 i8 值。

```cpp
  // There is no conditional move in the corpus: a select is first rewritten
  // to SELECT_CC, which is then lowered to mask arithmetic.
  setOperationAction(ISD::SELECT, MVT::i64, Expand);
  setOperationAction(ISD::SELECT, MVT::i32, Expand);
  setOperationAction(ISD::SELECT_CC, MVT::i64, Custom);
  setOperationAction(ISD::SELECT_CC, MVT::i32, Custom);
```

`SELECT` 先 Expand 成 `SELECT_CC`，再由 `LowerSELECT_CC` 变成掩码算术：`false ^ ((true ^ false) & (0 - cond))`。没有 `cmov` 也能做无分支选择。

```cpp
  setOperationAction(ISD::GlobalAddress, MVT::i64, Custom);
  setOperationAction(ISD::GlobalTLSAddress, MVT::i64, Custom);
  setOperationAction(ISD::INTRINSIC_WO_CHAIN, MVT::Other, Custom);
  setOperationAction({ISD::READCYCLECOUNTER, ISD::READSTEADYCOUNTER}, MVT::i64,
                     Custom);
  setOperationAction(ISD::LOCAL_RECOVER, MVT::i64, Custom);
  setOperationAction(ISD::JumpTable, MVT::i64, Custom);
  setOperationAction(ISD::BR_JT, MVT::Other, Expand);
```

`GlobalAddress` / `JumpTable` 都物化成 `PseudoLA`（`auipc+addi` 对）。`GlobalTLSAddress` 和 `READCYCLECOUNTER` 选 Custom 不是为了生成代码，而是为了在 `LowerOperation` 里**报出可读的诊断**，而不是让选择器抛 `Cannot select`。这也是 Custom 的一个正当用途：把“不支持”变成“说清楚为什么不支持”。

其余动作可以归类记：

| 类别 | 动作 | 为什么 |
|---|---|---|
| 位运算 intrinsic | `CTPOP/CTLZ/CTTZ/BSWAP/BITREVERSE` → Expand | 语料有移位和算术，展开即可 |
| 128 位乘法 | `UMUL_LOHI/SMUL_LOHI` → Expand | 拆成 `MUL` + `MULH/MULHU` |
| 128 位移位 | `SHL_PARTS/SRL_PARTS/SRA_PARTS` → Expand | 拆成两个 64 位移位 |
| 可变参数 | `VASTART/VAARG` Custom，`VACOPY/VAEND` Expand | va_list 就是指针 |
| 分支 | `BRCOND` Custom，`BR_CC` Expand | 先展开比较，再融回一条分支 |
| 栈 | `DYNAMIC_STACKALLOC`、`STACKSAVE/STACKRESTORE` Expand | 通用展开正好是 MiniRV 需要的序列 |
| 原子 | `setMaxAtomicSizeInBitsSupported(0)` + `ATOMIC_FENCE` Expand | 全部走 libatomic |
| 其他 | `setStackPointerRegisterToSaveRestore(X2)`、`setBooleanContents(ZeroOrOneBooleanContent)` | 通用展开/比较要知道 SP 和布尔表示 |

## 主线 3：i32 为什么要提升，`*W` 怎么补回来

### 为什么提升

RV64 的寄存器是 64 位，`GPR` 只声明了 i64。DAG 里如果保留 i32，寄存器分配、COPY、PHI 都要处理“子寄存器类型”，而硬件没有 32 位寄存器文件。提升到 i64 后，DAG 只需要一种整数宽度；i32 的语义只在两个边界上重要：

1. **内存边界**：`load i32` / `store i32` 用 `LW/SW`，由 load-ext / trunc-store 动作处理。
2. **算术边界**：32 位运算必须只保留低 32 位，并把 bit 31 符号扩展到高 32 位——这正是 RV64 的 `*W` 指令做的事（`addw` 等写回时符号扩展）。

### 谁来执行 Custom

i32 的**结果类型**不合法，所以 `LegalizeTypes` 的 `PromoteIntegerResult` 会先问目标：

```cpp
  // See if the target wants to custom expand this node.
  if (CustomLowerNode(N, N->getValueType(ResNo), true)) {
    LLVM_DEBUG(dbgs() << "Node has been custom expanded, done\n");
    return;
  }
```

`CustomLowerNode` 看到 `getOperationAction(Op, i32) == Custom`，就调用 `ReplaceNodeResults`。MiniRV 的实现：

```cpp
  auto LowerW = [&](unsigned Opcode) {
    SDValue LHS = DAG.getNode(ISD::ANY_EXTEND, DL, MVT::i64, N->getOperand(0));
    SDValue RHS = DAG.getNode(ISD::ANY_EXTEND, DL, MVT::i64, N->getOperand(1));
    SDValue Res = DAG.getNode(Opcode, DL, MVT::i64, LHS, RHS);
    Results.push_back(DAG.getNode(ISD::TRUNCATE, DL, N->getValueType(0), Res));
  };
```

三个细节：

- 操作数是 `ANY_EXTEND` 而不是 `SIGN_EXTEND`：`*W` 只读低 32 位，高 32 位是什么无所谓。
- 结果建成 i64 的目标节点（`MiniRVISD::ADDW` 等），再 `TRUNCATE` 回 i32；外层原本的 `any_extend` 会被 combine 掉。
- `MiniRVISD::*W` 在 `MiniRVInstrInfo.td` 里有 pattern 直接选到 `ADDW/ADDIW/...`，所以它不是伪指令。

位运算走另一条：

```cpp
  auto LowerBits = [&](unsigned Opcode) {
    auto Promoted = [&](SDValue V) { ... };
    SDValue LHS = Promoted(N->getOperand(0));
    SDValue RHS = Promoted(N->getOperand(1));
    SDValue Res = DAG.getNode(Opcode, DL, MVT::i64, LHS, RHS);
    Res = DAG.getNode(ISD::SIGN_EXTEND_INREG, DL, MVT::i64, Res,
                      DAG.getValueType(MVT::i32));
    Results.push_back(DAG.getNode(ISD::TRUNCATE, DL, N->getValueType(0), Res));
  };
```

没有 `andw/orw/xorw`，所以先在 64 位上算，再用 `SIGN_EXTEND_INREG(i32)` 把 bit 31 铺到高位，恢复“提升后的 i32 表示”。常量操作数还要先 `trunc(32).sext(64)`，否则类型合法化可能给出零扩展的常量，高位不为 1 的 32 位数会破坏这个表示。

### 实测：四条 i32 运算

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch12/i32.ll -o - | grep -E '^(add32|mul32|udiv32|and32):|^[[:space:]]+(addw|mulw|divuw|and)[[:space:]]'
add32:                                  # @add32
	addw	x10, x10, x11
mul32:                                  # @mul32
	mulw	x10, x10, x11
udiv32:                                 # @udiv32
	divuw	x10, x10, x11
and32:                                  # @and32
	and	x10, x10, x11
```

`addw/mulw/divuw` 都是“算低 32 位、写回时符号扩展”。`and32` 却是 64 位的 `and`：`and` 只关心低 32 位，而返回值的高位是 don't-care，DAG combine 在类型合法化前就把 `truncate(and)` 折成了 64 位 `and`。这说明 **`LowerBits` 存在，但不一定每次都被触发**——优化先一步吃掉了节点。

`-debug-only=legalize-types` 能看到提升与 Custom 的交接：

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=legalize-types /tmp/ch12/i32.ll -o /dev/null 2>&1 | sed -n '47,52p'
Legalizing node: t7: i32 = add t5, t6
Analyzing result type: i32
Promote integer result: t7: i32 = add t5, t6
Node has been custom expanded, done

Legalizing node: t12: i64 = MiniRVISD::ADDW t2, t4
```

第一行是原始节点，第三行说明结果类型要提升，第四行是 `ReplaceNodeResults` 成功接管；下一段已经在合法化 `MiniRVISD::ADDW` 了。

## 主线 4：初始 DAG 与合法化 DAG

`-debug-only=isel` 会打印每个阶段的 DAG。以 `add32` 为例（节选，空行与后续阶段略）：

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=isel /tmp/ch12/i32.ll -o /dev/null 2>&1 | sed -n '/Initial selection DAG/,/^$/p' | head -11
Initial selection DAG: %bb.0 'add32:'
SelectionDAG has 12 nodes:
    t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64,ch = CopyFromReg t2:1, Register:i64 %1
        t5: i32 = truncate t2
        t6: i32 = truncate t4
      t7: i32 = add t5, t6
    t8: i64 = any_extend t7
  t10: ch,glue = CopyToReg t4:1, Register:i64 $x10, t8
  t11: ch = MiniRVISD::RET t10, Register:i64 $x10, t10:1
```

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=isel /tmp/ch12/i32.ll -o /dev/null 2>&1 | sed -n '/Type-legalized selection DAG/,/^$/p' | head -8
Type-legalized selection DAG: %bb.0 'add32:'
SelectionDAG has 9 nodes:
    t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64,ch = CopyFromReg t2:1, Register:i64 %1
    t12: i64 = MiniRVISD::ADDW t2, t4
  t10: ch,glue = CopyToReg t4:1, Register:i64 $x10, t12
  t11: ch = MiniRVISD::RET t10, Register:i64 $x10, t10:1
```

对比要点：初始 DAG 有 `truncate → add i32 → any_extend` 三件套；类型合法化后它们整体消失，只剩一个 i64 的 `MiniRVISD::ADDW`。**`add i32` 从来没有出现在 LegalizeDAG 阶段**，因为它在类型合法化时就被 Custom 规则换掉了。

## 主线 5：i128 展开与 libcall

i128 是 `TypeExpandInteger`：拆成两个 i64（低半 `Lo`、高半 `Hi`）。`build_pair`/`extract_element` 在合法化后消失，所有运算按半字展开。

### 加法：内联三条指令

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch12/i128.ll -o - | sed -n '/^add128:/,/jalr/p'
add128:                                 # @add128
	.cfi_startproc
# %bb.0:
	add	x4, x11, x13
	add	x3, x10, x12
	sltu	x7, x3, x10
	add	x11, x4, x7
	addi	x10, x3, 0
	jalr	x0, 0(x1)
```

i128 的 add 是 `Lo = a.Lo + b.Lo`、`carry = (Lo <u a.Lo)`、`Hi = a.Hi + b.Hi + carry`。`sltu` 就是进位。DAG 里能直接看到这个结构：

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=isel /tmp/ch12/i128.ll -o /dev/null 2>&1 | sed -n '/Type-legalized selection DAG/,/^$/p' | head -13
Type-legalized selection DAG: %bb.0 'add128:'
SelectionDAG has 19 nodes:
    t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64,ch = CopyFromReg t2:1, Register:i64 %1
  t6: i64,ch = CopyFromReg t4:1, Register:i64 %2
  t8: i64,ch = CopyFromReg t6:1, Register:i64 %3
  t17: ch,glue = CopyToReg t8:1, Register:i64 $x10, t21
      t24: i64 = add t4, t8
      t23: i64 = setcc t21, t2, setult:ch
    t25: i64 = add t24, t23
  t19: ch,glue = CopyToReg t17, Register:i64 $x11, t25, t17:1
  t21: i64 = add t2, t6
```

`t23` 带 `:ch`（glue），因为高半的加法依赖低半的进位；`setcc ... setult` 就是 `sltu`。没有 `__addti3`——加法总能拆。

### 除法：`__divti3` / `__udivti3`

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch12/sdiv128.ll -o - | sed -n '/^div128:/,/jalr/p'
div128:                                 # @div128
	.cfi_startproc
# %bb.0:
	addi	x2, x2, -16
	.cfi_def_cfa_offset 16
	sd	x1, 8(x2)
	.cfi_offset x1, -8
	jal	x1, __divti3
	ld	x1, 8(x2)
	.cfi_restore x1
	addi	x2, x2, 16
	.cfi_def_cfa_offset 0
	jalr	x0, 0(x1)
```

无符号版本是 `__udivti3`（把 `sdiv` 换成 `udiv` 即可复现）。DAG 里是一次真正的调用：

```console
$ $B/llc -mtriple=minirv64 -O2 -debug-only=isel /tmp/ch12/i128.ll -o /dev/null 2>&1 | sed -n '/div128/,$p' | sed -n '/Type-legalized selection DAG/,/^$/p' | head -19
Type-legalized selection DAG: %bb.0 'div128:'
SelectionDAG has 28 nodes:
  t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64,ch = CopyFromReg t2:1, Register:i64 %1
  t6: i64,ch = CopyFromReg t4:1, Register:i64 %2
  t8: i64,ch = CopyFromReg t6:1, Register:i64 %3
  t17: ch,glue = CopyToReg t8:1, Register:i64 $x10, t34
    t35: i64,ch,glue = CopyFromReg t34:1, Register:i64 $x11, t34:2
  t19: ch,glue = CopyToReg t17, Register:i64 $x11, t35, t17:1
    t23: ch,glue = callseq_start t0, TargetConstant:i64<0>, TargetConstant:i64<0>
  t25: ch,glue = CopyToReg t23, Register:i64 $x10, t2
  t26: ch,glue = CopyToReg t25, Register:i64 $x11, t4, t25:1
  t28: ch,glue = CopyToReg t26, Register:i64 $x12, t6, t26:1
  t30: ch,glue = CopyToReg t28, Register:i64 $x13, t8, t28:1
  t32: ch,glue = MiniRVISD::CALL t30, TargetExternalSymbol:i64'__udivti3', Register:i64 $x10, Register:i64 $x11, Register:i64 $x12, Register:i64 $x13, RegisterMask:Untyped, t30:1
  t33: ch,glue = callseq_end t32, TargetConstant:i64<0>, TargetConstant:i64<0>, t32:1
  t34: i64,ch,glue = CopyFromReg t33, Register:i64 $x10, t33:1
  t20: ch = MiniRVISD::RET t19, Register:i64 $x10, Register:i64 $x11, t19:1
```

为什么加法不调 libcall、除法要调？因为 **Expand 的语义是“先尝试拆成更基础的操作”**：加减乘、移位、比较都能拆成 64 位指令；128 位除法没有可接受的展开（软件除法序列太长），于是 `ExpandIntRes_SDIV` 直接 `makeLibCall(RTLIB::SDIV_I128, ...)`，映射到 compiler-rt 的 `__divti3`。`LibCall` 动作则是“别试了，直接调”，两者最终都可能落到同一个运行时函数，区别在于是否先尝试展开。

i128 的返回是寄存器对 `x10/x11`（commit `a61c0cb`），所以调用点只需读 `$x10`/`$x11`；`__divti3` 的栈帧只有 16 字节，因为只有 x1 要保存。相关提交：`97ed4d6`（Expand 128-bit integer types）、`a61c0cb`（128-bit return in register pair）、`10b7b4a`（128-bit division → compiler-rt）。

### 窄内存访问：load-ext / trunc-store 真的生效

```console
$ $B/llc -mtriple=minirv64 -O2 /tmp/ch12/mem.ll -o - | grep -E '^(loads|store32):|^[[:space:]]+(lb|lbu|sw|sd|add)[[:space:]]'
loads:                                  # @loads
	lb	x3, 0(x10)
	lbu	x4, 0(x11)
	add	x10, x3, x4
store32:                                # @store32
	sw	x11, 0(x10)
```

`sext i8` + `load` 合成 `SEXTLOAD` → `lb`；`zext i8` → `ZEXTLOAD` → `lbu`；`store i32` → `TRUNCSTORE` → `sw`。DAG 里没有 i8/i32 的临时值，只有 i64 和“带口味的 load/store”。

## 主线 6：和 RISC-V 对照

`RISCVTargetLowering::RISCVTargetLowering` 的结构与 MiniRV 同源，但合法集合大得多：

```cpp
  MVT XLenVT = Subtarget.getXLenVT();
  addRegisterClass(XLenVT, &RISCV::GPRRegClass);
  if (Subtarget.hasStdExtZfhmin())
    addRegisterClass(MVT::f16, &RISCV::FPR16RegClass);
  if (Subtarget.hasStdExtF())
    addRegisterClass(MVT::f32, &RISCV::FPR32RegClass);
  if (Subtarget.hasStdExtD())
    addRegisterClass(MVT::f64, &RISCV::FPR64RegClass);
```

- RV32 时 `XLenVT` 是 i32，RV64 是 i64；`f16/bf16/f32/f64` 只有在对应扩展存在时才注册 `FPR*`，否则软化。
- `Zfinx/Zdinx` 把浮点映射到 `GPRF32/GPR`，说明“合法类型”完全由寄存器类决定，而不是由 ISA 名义决定。
- RVV 开启时，`nxv*` 系列类型会注册到 `VRN*M*` 寄存器类，成为合法类型；没有 V 时这些类型走 Split/Widen。
- 同样有 i32 的 Custom 运算（RV64 下 `ADD/SUB/SHL/SRA/SRL` 等），但策略不同：RISC-V 把 i32 提升成 i64 后，type-legalized DAG 是

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 -debug-only=isel /tmp/ch12/i32.ll -o /dev/null 2>&1 | sed -n '/Type-legalized selection DAG/,/^$/p' | head -7
Type-legalized selection DAG: %bb.0 'add32:'
SelectionDAG has 11 nodes:
  t0: ch,glue = EntryToken
        t2: i64,ch = CopyFromReg t0, Register:i64 %0
        t4: i64,ch = CopyFromReg t0, Register:i64 %1
      t12: i64 = add t2, t4
    t14: i64 = sign_extend_inreg t12, ValueType:ch:i32
```

它没有在这里造目标节点，而是留到选择阶段，用 `binop_allwusers<add>`（`hasAllNBitUsers(Node, 32)`）判断“所有使用者只要低 32 位”，命中才选 `ADDW`。最终两条路径都产出：

```console
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 /tmp/ch12/i32.ll -o - | grep -E '^(add32|mul32|udiv32|and32):|^[[:space:]]+(addw|mulw|divuw|and)[[:space:]]'
add32:                                  # @add32
	addw	a0, a0, a1
mul32:                                  # @mul32
	mulw	a0, a0, a1
udiv32:                                 # @udiv32
	divuw	a0, a0, a1
and32:                                  # @and32
	and	a0, a0, a1
```

差别只在“何时、由谁把 i32 语义补回来”：MiniRV 在类型合法化阶段显式造 `MiniRVISD::ADDW`，RISC-V 在 DAG 选择阶段用 PatFrag 匹配 `add + sext_inreg`。前者规则直观、易调试，后者能复用已有的 i64 DAG 和 combine；成熟后端通常选后者，因为模式更多、优化机会更多。

RISC-V 的完整合法化表（浮点、RVV、原子、`SELECT_CC` 的租约、`setCondCodeAction`）见 [../riscv_backend/README.md](../riscv_backend/README.md)，这里不再重复。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 契约 | `llvm/include/llvm/CodeGen/TargetLowering.h` | `LegalizeAction`、`LegalizeTypeAction`、`setOperationAction`、`getOperationAction`、`isLegalAddressingMode` | 动作枚举与查询接口 |
| 类型合法化 | `llvm/lib/CodeGen/SelectionDAG/LegalizeIntegerTypes.cpp` | `PromoteIntegerResult`、`ExpandIntRes_SDIV/UDIV` | Custom 如何转给 `ReplaceNodeResults`；i128 如何落到 libcall |
| 操作合法化 | `llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp` | `LegalizeOp`、`ExpandNode`、`ExpandLibCall` | Expand/LibCall/Custom 的分派 |
| MiniRV 声明 | `llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp` | 构造函数、`LowerOperation`、`ReplaceNodeResults`、`isLegalAddressingMode` | 每个动作的理由 |
| MiniRV 指令 | `llvm/lib/Target/MiniRV/MiniRVInstrInfo.td` | `MiniRVISD::ADDW`、`Pat<(MiniRVaddw ...)>` | 目标节点如何变成 `ADDW` |
| 测试 | `llvm/test/CodeGen/MiniRV/` 下的 `i32-arith.ll`、`i32-memory.ll`、`i128.ll`、`i128-div.ll`、`select.ll` | `CHECK` 行 | 每个动作的可观察结果 |

## 反直觉与踩雷

1. **错误直觉**：没写 `setOperationAction` 的操作就是不支持。
   **正确认识**：动作表 `memset(..., 0)` 且 `Legal == 0`，**没写就是 Legal**。不支持的操作必须显式 `Expand`/`LibCall`/`Custom`，否则会在 ISel 报 `Cannot select`。

2. **错误直觉**：MiniRV 生成了 `addw`，说明 i32 是合法类型。
   **正确认识**：i32 不合法。`*W` 是类型合法化阶段（`ReplaceNodeResults`）生成的 i64 目标节点，语义上代表“提升后的 i32”。`-debug-only=legalize-types` 的 `Promote integer result` 就是证据。

3. **错误直觉**：Custom 就是实现 `LowerOperation`。
   **正确认识**：结果类型不合法时走 `ReplaceNodeResults`；结果类型合法、只有操作不合法时才走 `LowerOperation`。MiniRV 的 i32 属于前者，`GlobalAddress` 属于后者。

4. **错误直觉**：i128 的一切都要调 compiler-rt。
   **正确认识**：加、减、乘、移位都能展开成 64 位指令；只有除法/取余这类没有可接受展开的操作才调 `__divti3/__udivti3`。

5. **错误直觉**：`SELECT` 标 Expand 会变成分支。
   **正确认识**：它先变成 `SELECT_CC`，再由 `LowerSELECT_CC` 变成掩码算术（无分支）。想看分支，得看 `BRCOND`。

6. **错误直觉**：`and i32` 一定会走 `LowerBits` 的“64 位算 + 符号扩展”。
   **正确认识**：如果高位是 don't-care，combine 会在合法化前把 `truncate(and)` 折成 64 位 `and`。规则存在，不代表每次触发。

## 进阶：再往深一层

- **两个 pass 的边界**：`LegalizeTypes` 只保证“类型合法”，产出的 DAG 仍可能含 `udiv i64`（操作不合法）；`LegalizeDAG` 再处理操作。调试图里 `Type-legalized` 和 `Legalized` 两个阶段名字不同，就是为了让你分清是哪一层出的问题。
- **`ComputeNumSignBitsForTargetNode`**：MiniRV 对所有 `*W` 节点返回 33（bit 63..31 都等于 bit 31），于是 combine 可以删掉显式的 `sign_extend_inreg`，也能做更激进的位运算优化。自定义节点如果漏了这个，会白白多出一条 `slli/srai`。
- **`setLoadExtAction` / `setTruncStoreAction` 是二维表**：行是内存类型、列是值类型。MiniRV 只填了“i64 ← i8/i16/i32”和“i64 → i8/i16/i32”，所以 `f32` 的窄访问会落到 `Expand` 或软浮点路径。填表前先想清楚“哪些组合真的存在”。
- **寻址模式与 LSR**：`isLegalAddressingMode` 返回 false 的形式，LSR 会主动拆成“先算地址再访存”。MiniRV 因此不需要 `[base+index]` 的指令，也不需要为 GEP 写复杂的模式。

## 本章重点整理

- 合法类型由寄存器类推出；MiniRV 只有 i64，i8/i16/i32 提升、i128 拆分、浮点软化。
- 动作表默认 Legal（`memset 0`），不支持的操作必须显式声明。
- `Custom` 有两条入口：`ReplaceNodeResults`（结果类型不合法）与 `LowerOperation`（操作不合法）。
- i32 的 `*W` 语义靠“ANY_EXTEND 操作数 + W 目标节点 + TRUNCATE 结果”补回；位运算用 64 位算 + `SIGN_EXTEND_INREG`。
- i128 加法展开成 `add/sltu/add`；除法走 `__divti3/__udivti3`，因为 Expand 没有可接受的序列。
- RISC-V 与 MiniRV 目标一致、路径不同：前者靠 `binop_allwusers` PatFrag，后者靠类型合法化期的目标节点。

## 自我检核

- [ ] 不看源码，写出五个 `LegalizeAction` 的含义，并各举一个 MiniRV 例子。
- [ ] 解释“默认 Legal”会怎样导致 `Cannot select`，并举一个 MiniRV 显式标 Expand 的例子。
- [ ] 画出 `add i32` 从 IR 到 `addw` 的 DAG 变化，指出哪一步生成了 `MiniRVISD::ADDW`。
- [ ] 说明 `ReplaceNodeResults` 与 `LowerOperation` 的触发条件差异。
- [ ] 解释 i128 加法为什么不用 libcall、除法为什么用，以及 `__divti3` 与 `__udivti3` 的分界。
- [ ] 对照 RISC-V 的 type-legalized DAG，说明 `binop_allwusers` 在哪个阶段起作用。

## 延伸阅读

- **[LLVM 源码：LegalizeIntegerTypes.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/SelectionDAG/LegalizeIntegerTypes.cpp)** — LLVM 项目
  - **读哪里**：`PromoteIntegerResult`、`ExpandIntRes_ADD/SUB/MUL/SDIV/UDIV`、`ExpandIntRes_Shift`。
  - **能学到什么**：类型提升/展开的精确算法，Custom 如何插入，libcall 在什么条件下被选择。
  - **前提**：读过本章的 `add32`/`i128` 例子。

- **[LLVM 源码：LegalizeDAG.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp)** — LLVM 项目
  - **读哪里**：`LegalizeOp` 的 `switch (Action)`、`ExpandNode`、`ExpandLibCall`。
  - **能学到什么**：Expand 与 LibCall 的分派，以及为什么 `Custom` 最终调用 `LowerOperation`。
  - **前提**：知道 `SDValue`/`SDNode` 的基本结构。

- **[LLVM 源码：TargetLowering.h](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/CodeGen/TargetLowering.h)** — LLVM 项目
  - **读哪里**：`LegalizeAction`、`LegalizeTypeAction`、`setOperationAction`、`setLoadExtAction`、`isLegalAddressingMode` 的注释。
  - **能学到什么**：每个查询的语义边界；哪些动作是“提示”而不是“命令”。
  - **前提**：无。

- **[课程：RISC-V 后端全貌](../riscv_backend/README.md)** — 本仓库
  - **读哪里**：`RISCVTargetLowering` 的合法化表、RVV 类型、`binop_allwusers` 相关模式。
  - **能学到什么**：成熟后端如何用同一套机制覆盖整数、浮点、向量和原子。
  - **前提**：读完本章；RVV 部分可先跳过。

- **[LLVM 文档：SelectionDAG Legalization](https://llvm.org/docs/CodeGenerator.html#selectiondag-legalize-phase)** — LLVM 项目
  - **读哪里**：SelectionDAG Legalize 一节。
  - **能学到什么**：两个 legalize pass 在整条代码生成流水线中的位置，以及和 ISel 的先后关系。
  - **前提**：无。

→ [Ch 13 SelectionDAG 模式：从 TableGen 到指令](./13-selectiondag-patterns.md)
