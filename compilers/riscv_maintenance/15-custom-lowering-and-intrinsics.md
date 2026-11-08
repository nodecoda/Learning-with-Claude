# Ch 15 — 自定义 lowering 与 intrinsic

> **目标**：能判断一个操作该用 `Pat<>`、`setOperationAction(Custom)` 还是 intrinsic；能读懂 `LowerOperation` 的分派表；能解释 `RISCVISD::*` 目标节点从 TableGen 声明到 `Select` 处理的完整链路。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，HEAD `0fc5afa04c23`。

> **前置**：Ch 13（合法化：什么时候会走到 `Custom`）、Ch 14（pattern 与 `Select`）。本章讲"TableGen 表达不了时，人写的那部分"。

---

## 15.1 为什么需要逃生口

TableGen pattern 能表达"这个 DAG 形状对应这条指令"，但表达不了这些：

- **需要看多个节点才能决定**（比如 `select` 要不要变成一个分支）；
- **需要生成多条指令且顺序有讲究**（比如 RV32 的 64 位移位要 6 条指令 + 一个条件）；
- **需要访问 Subtarget 之外的机器信息**（比如 `va_start` 要知道当前栈帧）；
- **IR 直接点名了一个语义**（`llvm.riscv.vadd`、`llvm.thread.pointer`），没有对应的通用 ISD 节点。

这四类都走同一个出口：**`setOperationAction(Op, VT, Custom)`**，然后由 `LowerOperation` 接手。intrinsic 再加一条 `LowerINTRINSIC_WO_CHAIN`。

维护者遇到的 bug 有两类：

1. **该用 Custom 的用了 pattern**，结果生成的指令序列在某条边上错（比如 `select` 需要短路求值）；
2. **Custom 里忘记处理某个类型**，`LowerOperation` 的 `default` 分支直接 `reportFatalInternalError`——这是崩溃而不是错码，反而好查。

本章的三份代码：

| 位置 | 规模 | 作用 |
|---|---|---|
| `RISCVISelLowering.cpp` 的 `LowerOperation` | 8374 起 | 通用 ISD 操作的 Custom 分派 |
| 同文件的 `LowerINTRINSIC_WO_CHAIN` | 12944 起 | intrinsic 的 Custom 分派 |
| `RISCVInstrInfo.td` 的 `RVSDNode` | 18–24 | 目标 DAG 节点的**声明** |
| `llvm/include/llvm/IR/IntrinsicsRISCV.td` | 2,485 行 | intrinsic 的**签名** |

---

## 15.2 先建立直觉：三种节点的三种命运

```
           合法 DAG
               │
     ┌─────────┼──────────────┬─────────────────┐
     ▼         ▼              ▼                 ▼
  普通 ISD   普通 ISD       RISCVISD::X      INTRINSIC_WO_CHAIN
  (有 pattern) (Custom)      (目标节点)        (riscv.* / thread_pointer)
     │         │              │                 │
     │         ▼              │                 ▼
     │   LowerOperation ──────┤          LowerINTRINSIC_WO_CHAIN
     │   (可能生成 RISCVISD)   │          (可能生成 RISCVISD)
     │         │              │                 │
     └─────────┴──────────────┴─────────────────┘
                     │
                     ▼
              Select（RISCVISelDAGToDAG.cpp）
                     │
                     ▼
                MachineInstr
```

关键点：**`LowerOperation` 的返回值仍然是 DAG**。它可以返回一个普通 ISD 节点、一串运算、或者一个 `RISCVISD::*` 目标节点。真正"落地"永远发生在 `Select` 里。

`RISCVISD` 节点不是 C++ 里手写的 enum，而是在 `RISCVInstrInfo.td:18-24` 用 `RVSDNode` 声明：

```tablegen
class RVSDNode<string opcode, SDTypeProfile type_profile,
               list<SDNodeProperty> properties = []>
    : SDNode<"RISCVISD::" # opcode, type_profile, properties> {
  bit HasPassthruOp = false;
  bit HasMaskOp = false;
  let TSFlags{0} = HasPassthruOp;
  let TSFlags{1} = HasMaskOp;
}
```

生成的 enum 在 `$T/lib/Target/RISCV/RISCVGenSDNodeInfo.inc`：

```cpp
namespace llvm::RISCVISD {
enum GenNodeType : unsigned {
  ABDS_VL = ISD::BUILTIN_OP_END,
  ABDU_VL,
  ...
  ZIP,
};
```

本树有 **427** 个目标节点（含首项 `ABDS_VL`）：

```bash
sed -n '/enum GenNodeType/,/};/p' \
  $T/lib/Target/RISCV/RISCVGenSDNodeInfo.inc \
  | grep -cE "^  [A-Z][A-Za-z0-9_]*,?$"
```

这个数字值得记住：它说明"目标节点"不是少数特例，而是 RISC-V 后端的常规工具。

---

## 15.3 `LowerOperation`：一张 60 多分支的分派表

`LowerOperation`（8374）开头就是 `default: reportFatalInternalError`——**没处理的操作会崩，不会静默错码**。这是设计选择，也是排查时的好消息。

分派表按操作族排列，读的时候可以分组：

| 组 | 例子 | 行号 | 干什么 |
|---|---|---|---|
| 访存/地址 | `GlobalAddress`、`BlockAddress`、`ConstantPool`、`JumpTable`、`GlobalTLSAddress` | 8386–8399 | 交给 Ch 11 的寻址模式 |
| 常量 | `Constant`、`ConstantFP` | 8402–8405 | `RISCVMatInt`（Ch 08） |
| 控制流 | `SELECT`、`BRCOND` | 8406–8409 | 本章 15.4 |
| 帧/变参 | `VASTART`、`FRAMEADDR`、`RETURNADDR` | 8410–8414 | 依赖 `MachineFrameInfo` |
| 拆分的 64 位 | `SHL_PARTS`、`SRA_PARTS`、`SRL_PARTS` | 8415–8421 | RV32 的 64 位移位 |
| 向量 | 大量 `ISD::*` + `RISCVISD::*` | 8422 起 | Ch 17 |

实测一个"必须 Custom"的例子——RV32 的 64 位左移：

```c
long long shl(long long a, int b) { return a << b; }
```

```bash
$T/bin/clang --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -O2 -S -o - sh.c
```

```asm
shl:
	addi	a4, a2, -32
	sll	a3, a0, a2
	bltz	a4, .LBB0_2
	mv	a1, a3
	j	.LBB0_3
.LBB0_2:
	not	a5, a2
	srli	a0, a0, 1
	sll	a1, a1, a2
	srl	a0, a0, a5
	or	a1, a1, a0
.LBB0_3:
	srai	a0, a4, 31
	and	a0, a0, a3
	ret
```

要点：

- 这不是 libcall（没有 `__ashldi3`），是 `lowerShiftLeftParts`（11078）展开的**内联序列**；
- `if (b >= 32)` 的分支在两头都用 `srai/and` 做无分支化（`srai a0, a4, 31` 生成全 0 或全 1 掩码）；
- 一个 64 位移位在 RV32 上变成 10 条指令——这就是"Custom 比 LibCall 快，但代价是代码膨胀"的典型权衡。

---

## 15.4 `lowerSELECT`：Zicond 的开关就在这一个函数里

`lowerSELECT`（10613）负责 `ISD::SELECT`。它首先看 `Zicond`：

- 有 `+zicond`：生成 `czero.eqz`/`czero.nez` + `or`；
- 没有：生成 `SELECT_CC` 或分支。

实测同一份 C：

```c
long sel(long c, long a, long b) { return c ? a : b; }
```

```asm
# -march=rv64gc
sel:
	beqz	a0, .LBB0_2
	mv	a2, a1
.LBB0_2:
	mv	a0, a2
	ret

# -march=rv64gc_zicond
sel:
	czero.eqz	a1, a1, a0
	czero.nez	a0, a2, a0
	or	a0, a0, a1
	ret
```

分支版有 4 条指令 + 2 个基本块；Zicond 版 4 条指令、单基本块。**代码大小差不多，但后者没有分支预测风险**——这正是 Zicond 存在的理由。

`lowerSELECT` 还要处理一个细节：如果两个值本来就是"同一个值的两种扩展"，它可以直接返回其中一个（避免生成 `select`）。这类"提前退出"在 `lowerSELECT` 开头占了很大篇幅，也是 review 时最容易看出问题的部分。

---

## 15.5 `lowerVASTART` 与 `lowerBRCOND`

### `VASTART`

`va_start` 需要把"当前函数还剩哪些参数在寄存器里"记到 `va_list` 结构里。这不是 DAG 能做的手术，所以 `ISD::VASTART` 是 Custom：

```cpp
case ISD::VASTART:
  return lowerVASTART(Op, DAG);   // 11012
```

`lowerVASTART` 生成 `RISCVISD::VASTART` 节点，携带 `varArgsFrameIndex` 与 `varArgsSaveSize`（这两个值在 Ch 09 的 MIR 里见过）。到 `Select` 阶段再展开成真实的 `addi`/`sd` 序列。

### `BRCOND`

`lowerBRCOND`（10989）把 `brcond` 拆成"比较 + 条件分支"。RISC-V 没有独立的标志寄存器，所以比较必须融进分支指令（`beq`/`blt`/`bge` 等），或者在需要时先算出一个布尔值再 `bne`。

这也是 `select` 与 `brcond` 共用 `selectSETCC`（`RISCVISelDAGToDAG.cpp:4173`）的原因：两者最终都要回答"这个条件怎么变成一条 RISC-V 分支"。

---

## 15.6 `LowerINTRINSIC_WO_CHAIN`：IR 直接点名的语义

`IntrinsicsRISCV.td` 有 2,485 行，但只有一部分需要 Custom lowering。`LowerINTRINSIC_WO_CHAIN`（12944）的 `default` 是 `break`——**大部分 intrinsic 不需要 Custom**，它们靠 pattern 直接匹配成指令。

需要 Custom 的几类：

```cpp
switch (IntNo) {
default:
  break;  // Don't custom lower most intrinsics.
case Intrinsic::riscv_tuple_insert: { ... RISCVISD::TUPLE_INSERT ... }
case Intrinsic::riscv_tuple_extract: { ... RISCVISD::TUPLE_EXTRACT ... }
case Intrinsic::thread_pointer: {
  EVT PtrVT = getPointerTy(DAG.getDataLayout());
  return DAG.getRegister(RISCV::X4, PtrVT);   // tp
}
case Intrinsic::riscv_pmul_00: ...
```

`thread_pointer` 是最干净的例子：它不生成任何指令，直接返回物理寄存器 `x4`。实测：

```llvm
declare ptr @llvm.thread.pointer()
define ptr @gettp() { %p = call ptr @llvm.thread.pointer()  ret ptr %p }
```

```asm
gettp:
	mv	a0, tp
	ret
```

`mv a0, tp` 是寄存器分配器插入的 copy，`tp` 本身来自 `DAG.getRegister(RISCV::X4, ...)`。这类"intrinsic 直接映射到固定寄存器"的写法在 TLS、shadow stack、`rdcycle` 里都会出现。

顺便验证 `rdcycle`（走 `ISD::READCYCLECOUNTER` 的 Custom）：

```llvm
define i64 @cyc() { %c = call i64 @llvm.readcyclecounter()  ret i64 %c }
```

```asm
cyc:
	rdcycle	a0
	ret
```

---

## 15.7 `ReplaceNodeResults`：类型合法化的回调

`ReplaceNodeResults`（17150）和 `LowerOperation` 长得像，但触发时机不同：

| 回调 | 触发时机 | 典型任务 |
|---|---|---|
| `LowerOperation` | **操作**不合法（Custom） | 把 `select` 变成 `czero` 序列 |
| `ReplaceNodeResults` | **类型**不合法，但节点本身要保留 | RV32 上 `i64` 的 `add` 拆成两个 `i32` |

一个具体例子：RV32 的 `i64` 加法。类型合法化器发现 `i64` 不合法，会先尝试 `Expand`；但某些操作（如 `ADD`）在 RISC-V 上有更好的手工拆法（带进位），于是走 `ReplaceNodeResults`：

```cpp
void RISCVTargetLowering::ReplaceNodeResults(SDNode *N,
                                             SmallVectorImpl<SDValue> &Results,
                                             SelectionDAG &DAG) const {
  switch (N->getOpcode()) {
  ...
  case ISD::ADD: { /* 拆成 ADD + SLTU + ADD */ }
```

Ch 13 讲的是"什么时候会走到这里"，本章讲的是"走到这里之后谁写代码"。

---

## 15.8 从 intrinsic 到 Pseudo：一条完整的 RVV 链

以 `__riscv_vadd_vv_i32m1` 为例，四步：

1. **clang builtin** → `llvm.riscv.vadd` intrinsic（`IntrinsicsRISCV.td` 里声明，带 `LLVMMatchType`、LMUL 相关参数）；
2. **SelectionDAG** 把 intrinsic 变成一个带 `RISCVISD::` 前缀的节点，或者直接匹配 pattern；
3. **`LowerINTRINSIC_*`** 处理需要看 LMUL/策略（`ta`/`ma`）的那些，生成 `RISCVISD::VADD_VV` 之类的节点；
4. **`Select`** 把它选成 `PseudoVADD_VV_M1`（Ch 18 讲伪指令展开）。

实测能看到第 4 步的产物：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=finalize-isel -simplify-mir vadd.ll -o -
```

```mir
    %3:vr = PseudoVADD_VV_M1 $noreg, %0, %1, %2 /* vl */, 5 /* e32 */, 0 /* tu, mu */
```

四个要点：

- `PseudoVADD_VV_M1` 是**伪指令**，`_M1` 已经把 LMUL 写进 opcode 名字（Ch 18 讲展开）；
- `$noreg` 是 passthru（本例是 `undef`），对应 `RVSDNode` 的 `HasPassthruOp`；
- `5 /* e32 */` 与 `0 /* tu, mu */` 是 vtype 与策略的立即数，注释由打印器补上——编码细节在 Ch 17；
- 这个 intrinsic 在本树是 **4 个参数**（passthru、两个向量、`vl`），不是旧版的 6 个。写测试时以 `IntrinsicsRISCV.td` 的声明为准。

---

## 15.9 源码落点

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**（31,346 行）
  - 8374–8421：`LowerOperation` 的分派表与前 20 个 case。
  - 10266：`lowerGlobalAddress`（Ch 11 的主角）。
  - 10613：`lowerSELECT`，Zicond 分支的入口。
  - 10989：`lowerBRCOND`。
  - 11012：`lowerVASTART`。
  - 11078 / 11151：`lowerShiftLeftParts` / `lowerShiftRightParts`。
  - 12944：`LowerINTRINSIC_WO_CHAIN`。
  - 17150：`ReplaceNodeResults`。

- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - 18–24：`RVSDNode` 类，所有目标节点的声明模具。
  - 26 起：`SDT_*` 类型剖面（`SDT_RISCVCall`、`SDT_RISCVSelectCC` 等）。

- **`$T/lib/Target/RISCV/RISCVGenSDNodeInfo.inc`**
  - `enum GenNodeType`：427 个节点的编号，`ABDS_VL = ISD::BUILTIN_OP_END` 起算。

- **`llvm/include/llvm/IR/IntrinsicsRISCV.td`**（2,485 行）
  - 按扩展分组；`llvm.riscv.vadd`、`llvm.riscv.sf.vc*`、`llvm.riscv.th.*` 都在这里。
  - 读法：先看 `def int_riscv_<name> : Intrinsic<...>` 的签名，再看 `IntrinsicsRISCV.td` 里的 `LLVMMatchType` 约束。

- **测试**
  - `llvm/test/CodeGen/RISCV/select-*.ll`：`lowerSELECT` 的正面用例。
  - `llvm/test/CodeGen/RISCV/vararg.ll`：`VASTART`。
  - `llvm/test/CodeGen/RISCV/rv32shift*.ll`：`SHL_PARTS` 系列。
  - `llvm/test/CodeGen/RISCV/rvv/*intrinsic*.ll`：intrinsic → Pseudo 的链路。

---

## 15.10 可跑实验

### 实验 1：`select` 的两种命运

```bash
cd /tmp && cat > s.c <<'EOF'
long sel(long c, long a, long b) { return c ? a : b; }
EOF
for M in "-march=rv64gc" "-march=rv64gc_zicond"; do
  echo "--- $M ---"
  $T/bin/clang --target=riscv64-linux-gnu $M -mabi=lp64d -O2 -S -o - s.c | sed -n '/sel:/,/ret/p'
done
```

### 实验 2：`thread_pointer` 与 `rdcycle`

```bash
cd /tmp && cat > tp.ll <<'EOF'
declare ptr @llvm.thread.pointer()
declare i64 @llvm.readcyclecounter()
define ptr @gettp() { %p = call ptr @llvm.thread.pointer() ret ptr %p }
define i64 @cyc() { %c = call i64 @llvm.readcyclecounter() ret i64 %c }
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+c tp.ll -o - | sed -n '1,30p'
```

### 实验 3：RV32 的 64 位移位

```bash
cd /tmp && cat > sh.c <<'EOF'
long long shl(long long a, int b) { return a << b; }
EOF
$T/bin/clang --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -O2 -S -o - sh.c | sed -n '/shl:/,/ret/p'
```

数一数有多少条指令、有几个分支。再把 `-march=rv32i` 改成 `rv32im`：移位序列不变（`M` 只影响乘除）。

### 实验 4：数一数目标节点

```bash
sed -n '/enum GenNodeType/,/};/p' \
  $T/lib/Target/RISCV/RISCVGenSDNodeInfo.inc \
  | grep -cE "^  [A-Z][A-Za-z0-9_]*,?$"
```

应该得到 426——比总数少 1，因为首项 `ABDS_VL = ISD::BUILTIN_OP_END` 带初始化器，不匹配这个正则。要数全就再加一条 `grep -cE '^  [A-Z][A-Za-z0-9_]*(,| = ISD::BUILTIN_OP_END,)$'`，得到 427。再 `grep -c "def SDT_" llvm/lib/Target/RISCV/RISCVInstrInfo.td` 看类型剖面的数量——两者不是一一对应（多个节点可共用一个剖面）。

### 实验 5：intrinsic 的 Custom 分支

```bash
grep -n "case Intrinsic::riscv_" llvm/lib/Target/RISCV/RISCVISelLowering.cpp \
  | sed -n '1,40p'
```

对照 `IntrinsicsRISCV.td` 里的 `def int_riscv_*`，找出"有 intrinsic 但没有 Custom case"的那些——它们走 pattern。

### 实验 6：亲手复现 15.8 节的 MIR

```bash
cd /tmp && cat > vadd.ll <<'EOF'
define <vscale x 2 x i32> @vadd(<vscale x 2 x i32> %a, <vscale x 2 x i32> %b, i64 %vl) {
  %r = call <vscale x 2 x i32> @llvm.riscv.vadd.nxv2i32.nxv2i32(
      <vscale x 2 x i32> undef, <vscale x 2 x i32> %a, <vscale x 2 x i32> %b, i64 %vl)
  ret <vscale x 2 x i32> %r
}
declare <vscale x 2 x i32> @llvm.riscv.vadd.nxv2i32.nxv2i32(
    <vscale x 2 x i32>, <vscale x 2 x i32>, <vscale x 2 x i32>, i64)
EOF
$T/bin/llc -mtriple=riscv64 -mattr=+v -stop-after=finalize-isel -simplify-mir vadd.ll -o -
```

如果 llc 报 `intrinsic has incorrect number of args`，说明你用的 intrinsic 签名和本树不一致——`grep -n "int_riscv_vadd" llvm/include/llvm/IR/IntrinsicsRISCV.td` 查真实签名。

---

## 15.11 踩雷集锦

1. **`setOperationAction(Custom)` 了但 `LowerOperation` 没加 case。** 运行时 `reportFatalInternalError`，栈上能看到 `Unimplemented RISCVTargetLowering::LowerOperation Case`。
2. **在 `LowerOperation` 里返回了非法类型。** 合法化器不会二次检查你的返回值，`Select` 阶段会崩在 `getRegClassFor`。
3. **`lowerSELECT` 里假设两个值同类型。** `select` 的 arms 可能经过不同类型合法化，先 `getValueType()` 再动手。
4. **`RVSDNode` 的 `TSFlags{0}`/`{1}` 被别的类覆盖。** `HasPassthruOp`/`HasMaskOp` 是全局 TSFlags 位，新增节点类时要确认没和已有语义冲突。
5. **intrinsic 的 `LLVMMatchType` 写错。** 比如把 LMUL 参数写成普通 `llvm_anyvector_ty`，会在 verifier 阶段才发现。
6. **以为所有 intrinsic 都要 Custom。** `LowerINTRINSIC_WO_CHAIN` 的 `default` 是 `break`；大多数 intrinsic 靠 pattern 直接匹配，加 case 反而会绕过 pattern。
7. **`thread_pointer` 当成普通 TLS 访问。** 它直接返回 `x4`，不需要 `%tprel_*`；写成 TLS 序列会多出几条无意义指令。
8. **`ReplaceNodeResults` 与 `LowerOperation` 混用。** 前者只在类型合法化时被调，后者只在操作合法化时被调；放错地方等于没写。

---

## 15.12 本章重点整理

- 四种"TableGen 表达不了"的场景 → 一个出口：`setOperationAction(..., Custom)` + `LowerOperation`。
- `LowerOperation` 的返回值仍是 DAG，可以包含普通 ISD 节点、`RISCVISD::*` 目标节点或一串运算。
- `RISCVISD` 节点在 `RISCVInstrInfo.td` 用 `RVSDNode` 声明，enum 由 TableGen 生成（本树 427 个；上文正则因首项带初始化器只数到 426）。
- `lowerSELECT` 是 Zicond 的开关：无 Zicond 生成分支，有 Zicond 生成 `czero` + `or`。
- `lowerVASTART`/`lowerBRCOND`/`lower*Parts` 都需要机器信息或特殊序列，所以必须 Custom。
- `LowerINTRINSIC_WO_CHAIN` 只处理少数 intrinsic；`thread_pointer` 直接返回 `x4`，是最干净的样例。
- `ReplaceNodeResults` 是**类型**合法化回调，`LowerOperation` 是**操作**合法化回调，别混。
- 最终落地永远在 `Select`；Custom 只是把 DAG 改写成更容易匹配的形状。

---

## 15.13 自我检核

- [ ] 能说出四种必须用 Custom 的场景，并各举一个 RISC-V 例子
- [ ] 能解释 `LowerOperation` 与 `ReplaceNodeResults` 的触发时机差异
- [ ] 能画出 `select` 在有无 Zicond 时的两种汇编
- [ ] 能解释 `llvm.thread.pointer` 为什么只生成 `mv a0, tp`
- [ ] 能说出 `RISCVISD` 节点在哪里声明、enum 在哪里生成、数量大约多少
- [ ] 能解释 RV32 的 64 位移位为什么不用 libcall
- [ ] 能说出 intrinsic 从 IR 到 `Pseudo*` 的四个步骤
- [ ] 能解释为什么 `LowerINTRINSIC_WO_CHAIN` 的 `default` 是 `break` 而不是报错

---

## 15.14 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**
  - 读哪里：8374–8421（分派表）、10613（`lowerSELECT`）、12944（intrinsic）。
  - 为什么读：这三处覆盖了 90% 的 Custom 逻辑。
- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - 读哪里：18–24（`RVSDNode`）、26–120（`SDT_*` 剖面）。
  - 为什么读：目标节点的"类型契约"全在这几十行里。
- **`llvm/include/llvm/IR/IntrinsicsRISCV.td`**（2,485 行）
  - 读哪里：按扩展分组浏览，先看 `vadd`、`sf.vc*`、`th.*` 三组。
  - 为什么读：intrinsic 是 IR 与后端之间的公开契约，改签名等于改 ABI。
- **`llvm/docs/CodeGenerator.md`**
  - 读哪里：`LowerOperation` 与 `ReplaceNodeResults` 的职责描述。
  - 为什么读：把本章的 RISC-V 实例放回 SelectionDAG 的通用模型。
- **`llvm/test/CodeGen/RISCV/select-*.ll`**
  - 读哪里：有无 `+zicond` 的两组 CHECK。
  - 为什么读：改 `lowerSELECT` 时的第一道回归。

---

→ 下一章：[Ch 16 — DAG combine 与 known bits](./16-dag-combine-and-known-bits.md)
