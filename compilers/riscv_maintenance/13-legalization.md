# Ch 13 — 合法化：类型、操作与 RV32/RV64 的分裂

> **目标**：读懂 SelectionDAG 从"IR 的形状"变成"目标能执行的形状"这一步；能对任意 `(opcode, type)` 说出它在这棵树上被判成 Legal / Promote / Expand / LibCall / Custom 中的哪一个、由谁执行、失败时看哪张表。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `0fc5afa04c23`。

> **前置**：Ch 02（扩展标志与 `RISCVSubtarget`）、Ch 03（`-stop-after` 与 MIR）。本章假设你已经在 `RISCVISelLowering.cpp` 里迷过一次路。

---

## 13.1 为什么合法化是维护者的"第一现场"

前端交给后端的 DAG 只保证"语义正确"，不保证"RISC-V 能执行"。RISC-V 没有 `i8` 寄存器，没有 `i64` 寄存器（RV32），没有 `frem` 指令，也没有 `select` 指令。把这些形状修剪成"目标支持的子集"，就是 Legalization。

维护者遇到的 bug 有相当比例落在这里，而且是三种很不一样的形状：

1. **新扩展加进来了，表没跟着改。** 例如给 RV32 加了某个 64 位指令，但 `ISD::MUL, MVT::i64` 仍指向旧的 Custom 序列，于是新指令永远选不出来。表是"能力声明"，不是"优化建议"。
2. **表项的类型写错了。** `setOperationAction(ISD::ADD, MVT::i32, Custom)` 在 RV64 上有意义（保留 32 位 `addw` 语义），在 RV32 上就是多余甚至有害；反过来把 `XLenVT` 写成 `MVT::i32`，RV64 的行为立刻变。
3. **Custom 的 hook 与表项对不上。** 表里写了 `Custom`，但 `LowerOperation` / `ReplaceNodeResults` 没有对应 `case`，立刻得到 `Unimplemented RISCVTargetLowering::LowerOperation Case`（`RISCVISelLowering.cpp:8378`）或 `Don't know how to custom type legalize this operation!`（`:17156`）。

本章要建立的判断力是：**看到一条没选出来的指令，先问"它在表里是什么动作"，再问"哪个 legalizer 在什么时候执行它"，最后才去看 pattern。**

---

## 13.2 先建立直觉：非法 DAG 的四条出路

以 `add i64` 在 RV32 上为例。`i64` 不是合法类型，所以类型合法化先动手；四条出路（下面各用一个典型节点举例）就是五值枚举里除 `Legal` 之外的四个：

```
                    IR:  %r = add i64 %a, %b        triple=riscv32
                                     │
                       ┌─────────────▼──────────────┐
                       │ 类型表问：i64 合法吗？       │  ValueTypeActions[i64]
                       │ 答：TypeExpandInteger      │  = TypeExpandInteger
                       └─────────────┬──────────────┘
                                     │ 不合法
        ┌───────────────┬────────────┴────────────┬──────────────────┐
        ▼               ▼                         ▼                  ▼
    Promote          Expand                    Custom             LibCall
  i8/i16 → i32   i64 → 2×i32          MUL i64 → mul + mulhu    frem → fmodf
  TypePromote    TypeExpand           (ReplaceNodeResults)     OpActions=LibCall
        │               │                         │                  │
        └───────────────┴────────────┬────────────┴──────────────────┘
                                     ▼
              合法 DAG：只剩 i32 的 add / setcc / mul / call …
```

四条出路的判据（`TargetLowering.h:222-228` 的枚举注释就是官方定义）：

| 动作 | 枚举注释 | 语义 | RISC-V 实例 | 结果长什么样 |
|---|---|---|---|---|
| `Promote` | 用更大的类型执行 | 类型比机器字窄 | `i8 add`、`i1 setcc` | 一条 `add a0,a0,a1` |
| `Expand` | 先尝试拆成其他 op，否则 libcall | 一条指令做不了 | RV32 `i64 add`、无 M 的 `sdiv` | `add`+`sltu`，或 `__divdi3` |
| `LibCall` | 不尝试展开，总是调库 | 没有可接受的内联序列 | `frem float` | `tail fmodf` |
| `Custom` | 调 target 的 hook | 目标有专门序列/节点 | `select`、RV32 `mul i64` | `czero.*` / `mul`+`mulhu` |

注意最后两种的区别，这是读代码时最容易混的地方：`Expand` 的契约是"尽量展开，实在不行才 libcall"，所以无 M 的 `sdiv i32` 表里写 `Expand`，最终仍然产出 `__divsi3`；而 `LibCall` 是"别试了，直接调库"。

---

## 13.3 五个问题与两张表

维护者读任何一个 case，都按这五问走：

| # | 问题 | 代码里的答案 | 表/落点 |
|---|---|---|---|
| 1 | 这个**类型**合法吗？ | `isTypeLegal(VT)` → `getTypeAction` | `ValueTypeActions[VT]`（`TargetLowering.h:1143`） |
| 2 | 这个**操作**合法吗？ | `getOperationAction(Op, VT)` | `OpActions[VT][Op]`（`TargetLowering.h:3977`） |
| 3 | 不合法时选哪个动作？ | `Legal` / `Promote` / `Expand` / `LibCall` / `Custom` | 同一张 `OpActions` |
| 4 | **谁决定**？ | `RISCVTargetLowering` 构造函数 | `RISCVISelLowering.cpp:67`，469 处 `setOperationAction(` |
| 5 | **谁执行**？ | `DAGTypeLegalizer`（类型）→ `SelectionDAGLegalize`（操作） | `LegalizeTypes.cpp` / `LegalizeDAG.cpp` |

两张表的写入与读取都很朴素：

```cpp
// 写：TargetLowering.h:2814
void setOperationAction(unsigned Op, MVT VT, LegalizeAction Action) {
  assert(Op < std::size(OpActions[0]) && "Table isn't big enough!");
  OpActions[(unsigned)VT.SimpleTy][Op] = Action;
}
// 读：TargetLowering.h:1366
LegalizeAction getOperationAction(unsigned Op, EVT VT) const {
  if (Op >= std::size(OpActions[0])) return Custom;   // target 私有 op 必须 Custom
  if (VT.isExtended())              return Expand;   // 扩展类型默认拆
  return OpActions[(unsigned)VT.getSimpleVT().SimpleTy][Op];
}
```

一个必须记住的默认值：`OpActions` 在 `TargetLoweringBase.cpp:767` 被 `memset` 成 0，而枚举第一项就是 `Legal`。**没有写进表 = 声称合法**。所以新增指令后忘了改表，症状不是"退化成慢代码"，而是 isel 阶段 `Cannot select`。

同理，`ValueTypeActions` 默认 `TypeLegal`（`TargetLowering.h:1146`），由 `computeRegisterProperties` 在 `RISCVTargetLowering` 构造函数第 253 行根据 `addRegisterClass` 的结果填出来。**类型表不是手写的，是寄存器类推导出来的**——这是下一节的主线。

---

## 13.4 构造函数：合法类型集合从哪来

`RISCVISelLowering.cpp:82-101` 就是 RISC-V 的"合法标量类型清单"：

| 类型 | 使它能合法的扩展 | 寄存器类 | 行号 |
|---|---|---|---|
| `XLenVT`（RV32 的 i32 / RV64 的 i64） | 总是 | `GPR` | 82 |
| `f16` | `Zfhmin`（含 `Zfh`） | `FPR16` | 84-85 |
| `bf16` | `Zfbfmin` 或 Andes `BFHCvt` | `FPR16` | 86-87 |
| `f32` | `F` | `FPR32` | 88-89 |
| `f64` | `D` | `FPR64` | 90-91 |
| `f16` | `Zhinxmin` | `GPRF16` | 92-93 |
| `f32` | `Zfinx` | `GPRF32` | 94-95 |
| `f64` | `Zdinx` 且 RV64 | `GPR` | 96-98 |
| `f64` | `Zdinx` 且 RV32 | `GPRPair` | 99-100 |

本章的 `llc` 实验共用一个样本文件，先建好它（后面只截取对应函数那一段输出）：

```bash
mkdir -p /tmp/ch13 && cd /tmp/ch13
cat > ch13.ll <<'EOF'
define i64 @add64(i64 %a, i64 %b) { %r = add i64 %a, %b  ret i64 %r }
define i64 @mul64(i64 %a, i64 %b) { %r = mul i64 %a, %b  ret i64 %r }
define i64 @div64(i64 %a, i64 %b) { %r = sdiv i64 %a, %b  ret i64 %r }
define i32 @rem32(i32 %a, i32 %b) { %r = urem i32 %a, %b  ret i32 %r }
define float @frem32(float %a, float %b) { %r = frem float %a, %b  ret float %r }
define float @fadd32(float %a, float %b) { %r = fadd float %a, %b  ret float %r }
define double @dadd(double %a, double %b) { %r = fadd double %a, %b  ret double %r }
define half @hadd(half %a, half %b) { %r = fadd half %a, %b  ret half %r }
define i64 @sel(i1 %c, i64 %a, i64 %b) { %r = select i1 %c, i64 %a, i64 %b  ret i64 %r }
define i32 @div32(i32 %a, i32 %b) { %r = sdiv i32 %a, %b  ret i32 %r }
EOF
cat > i128.c <<'EOF'
__int128 add128(__int128 a, __int128 b) { return a + b; }
EOF
cat > c8.c <<'EOF'
signed char f8(signed char a, signed char b) { return a + b; }
EOF
cat > i128.ll <<'EOF'
define i128 @add128(i128 %a, i128 %b) { %r = add i128 %a, %b  ret i128 %r }
define i128 @mul128(i128 %a, i128 %b) { %r = mul i128 %a, %b  ret i128 %r }
EOF
```
以下命令都在 `/tmp/ch13` 下执行。

"四种来源"（`Zfhmin`、`F`、`Zfinx`、`Zdinx`，外加 `Zhinxmin` 变体）归成两条路：**F/D 家族进 FPR，Zfinx/Zdinx 家族进 GPR**。同一份 IR 在两条路上会打印同一条指令名，但寄存器完全不同。实测（`-O2`）：

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+zfinx -O2 ch13.ll -o - | sed -n '/^fadd32:/,/ret/p'
$T/bin/llc -mtriple=riscv32 -mattr=+f     -O2 ch13.ll -o - | sed -n '/^fadd32:/,/ret/p'
$T/bin/llc -mtriple=riscv32 -mattr=+zdinx -O2 ch13.ll -o - | sed -n '/^dadd:/,/ret/p'
```

```asm
	fadd.s	a0, a0, a1          # GPRF32：操作数在整数寄存器
	fadd.s	fa0, fa0, fa1       # FPR32
	fadd.d	a0, a0, a2          # GPRPair：一对 GPR 当一个 f64
```

三个容易忽略的点：

- **`Zfhmin` 不是 `Zfh`。** `Zfhmin` 只提供 f16 与 f32 的转换，算术在 `:688` 被整批 `Promote` 到 f32；只有 `Zfh` 才在 `:679` 把 `FPLegalNodeTypes` 设成 `Legal`。实测见 13.6.4。
- **`addRegisterClass` 只登记"这个类型有家"。** 真正的类型动作由 `computeRegisterProperties`（`TargetLoweringBase.cpp:1416`）推导：比最大整数寄存器大的类型逐个 `TypeExpandInteger`（`:1433-1440`），比它小的逐个 `TypePromoteInteger`（`:1442-1453`）。
- **`Zdinx` 下 RV32 的 `f64` 是 `GPRPair`，不是"两个独立 i32"。** 它作为一个 f64 值被 legalize，落到一对寄存器，而不是先被拆成两个标量。

---

## 13.5 `setOperationAction`：九条真实条目

下面每一条都来自本树构造函数，行号可直接 `grep -n` 复核。

| 行 | 条目 | 为什么 |
|---|---|---|
| 268 | `ISD::BRCOND, MVT::Other, Custom` | `brcond` 的 cond 通常是一个 `setcc`；`lowerBRCOND`（`:10989`）把它折成 `RISCVISD::BR_CC`，并用 `translateSetCCForBranch` 把 `slt` 翻成 `bge` 之类分支直接支持的 CC |
| 269 | `ISD::SELECT_CC, XLenVT, Expand` | RISC-V 没有 `select_cc` 指令；默认展开成 `select` + 分支，之后交给 `SELECT` 的 Custom |
| 430 | `ISD::SELECT, XLenVT, Custom` | Zicond 下 `lowerSELECT` 出 `RISCVISD::CZERO_*`；有 XTHeadCondMov 或 `XMIPSCMov`（`useMIPSCCMovInsn`）时这条 Custom **根本不设**，交给 pattern 直接选 `th.mveqz`/`th.mvnez`；其余情况出 `RISCVISD::SELECT_CC` 再变分支 |
| 320 | `ISD::MUL, MVT::i64, Custom` | RV32 + Zmmul（M 隐含 Zmmul）：把 64 位乘拆成 3 条 `mul` + 1 条 `mulhu` + 2 条 `add`（走 `ReplaceNodeResults:17343`） |
| 315 | `{MUL, MULHS, MULHU}, XLenVT, Expand` | 没有 Zmmul：展开也拼不出可接受的序列，最终 `__muldi3`/`__mulsi3` |
| 324 | `{SDIV, UDIV, SREM, UREM}, XLenVT, Expand` | 没有 M：除法没有可接受的 inline 序列，落到 `__divsi3`/`__umodsi3` |
| 752/813 | `ISD::FREM, f32/f64, LibCall` | 没有 `frem` 指令，也没有"拆成几条"的合理方案，直接 `fmodf`/`fmod` |
| 294 | `SIGN_EXTEND_INREG, {i8,i16}, Expand` | 没有 Zbb/XTHeadBb 等时，没有 `sext.b`，用 `slli`+`srai` 两条凑 |
| 837 | `{FP_TO_SINT, ...}, MVT::i64, Custom`（RV32 且 ZfhminOrZhinxmin） | RV32 上 i64 是非法类型，f16→i64 走 `ReplaceNodeResults` 的专用路径，避免调用 compiler-rt 没有的 `__fixhfhdi` 之类 |

一段代表性代码，原文照抄（`RISCVISelLowering.cpp`）：

```cpp
// :314-321  乘法：有 Zmmul 才谈 Custom，RV64 管 i128/i32，RV32 管 i64
if (!Subtarget.hasStdExtZmmul()) {
  setOperationAction({ISD::MUL, ISD::MULHS, ISD::MULHU}, XLenVT, Expand);
} else if (Subtarget.is64Bit()) {
  setOperationAction(ISD::MUL, MVT::i128, Custom);
  setOperationAction(ISD::MUL, MVT::i32, Custom);
} else {
  setOperationAction(ISD::MUL, MVT::i64, Custom);
}
```

同一张表还有几个"维度不同"的兄弟 API，读代码时别混：`setCondCodeAction`（条件码，如 `SETULE` Expand）、`setLoadExtAction`/`setTruncStoreAction`（load/store 的扩展/截断）、`setIndexedLoadAction`（pre/post inc）。它们各自写自己的表，但决策方式完全一样：**目标声明能力，legalizer 执行**。

---

## 13.6 类型合法化的四个样本

### 13.6.1 窄整数：i1 / i8 / i16 → XLen

RISC-V 没有 8/16 位寄存器，所以 `i8`、`i16`、`i1` 全部 `TypePromoteInteger` 到 `XLenVT`。C 代码最能说明问题：

```bash
$T/bin/clang --target=riscv64-unknown-elf -march=rv64gc -O2 -S -o - c8.c
```

```asm
f8:                                     # signed char f8(signed char a, signed char b)
	add	a0, a0, a1
	slli	a0, a0, 56
	srai	a0, a0, 56
	ret
```

`add` 是提升后的 i32/i64 运算；`slli`+`srai` 是 `SIGN_EXTEND_INREG i8`（`i16` 同理，移位量 48）在 `:294` 被设成 `Expand` 的结果。

### 13.6.2 RV32：i64 → 2×i32

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 ch13.ll -o - | sed -n '/^add64:/,/ret/p'
```

```asm
add64:                                  # i64 @add64(i64 %a, i64 %b)
	add	a2, a0, a2          # 低 32 位相加
	add	a1, a1, a3          # 高 32 位相加
	sltu	a0, a2, a0          # 低位的进位
	add	a1, a1, a0          # 补进高位
	mv	a0, a2
	ret
```

debug 输出直接点名了那次变换：

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 -debug-only=legalize-types ch13.ll -o /dev/null
```

```
Legalizing node: t11: i64 = add t9, t10
Analyzing result type: i64
Expand integer result: t11: i64 = add t9, t10
```

### 13.6.3 i128：2×i64，在 RV32 上就是 4×i32

先记一个容易踩的事实：**clang 在 RISC-V 上不提供 `__int128`**，不是 legalizer 拒绝，是前端就不认：

```bash
$T/bin/clang --target=riscv32-unknown-elf -march=rv32im -O2 -S -o - i128.c
```

```
i128.c:1:17: error: __int128 is not supported on this target
```

这行错误说明 `i128` 在 clang 层就不可用。要看它必须手写 IR；类型表的规则是"每大一档，寄存器数翻倍"（`TargetLoweringBase.cpp:1433-1440`），所以 RV32 上 `i128 → 2×i64 → 4×i32`：

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 i128.ll -o - | sed -n '/^add128:/,/ret/p'
```

```asm
add128:                                 # i128 @add128(i128 %a, i128 %b)
	lw	a4, 0(a2)           # b.word0
	lw	t0, 0(a1)           # a.word0
	lw	a7, 4(a1)
	lw	t1, 4(a2)
	...
	add	a1, t0, a4
	add	a4, a7, t1
	sltu	t0, a1, t0          # 逐字进位
	add	a4, a4, t0
	...
	sw	a1, 0(a0)           # 结果 16 字节，4 个 sw
	sw	a4, 4(a0)
	sw	t0, 8(a0)
	sw	a2, 12(a0)
	ret
```

`i128` 超过 2×XLEN：返回值走 `sret`（`a0`），两个参数各以一个指针传入（`a1`/`a2`），函数从指针各读 4 个字——这正是 Ch 09 的调用约定与本章类型分裂的交叉点。

### 13.6.4 f16 与 f64：没有扩展时到底调什么

`f16` 没有 `Zfhmin`/`Zhinxmin` 时不是 `TypeSoftenFloat`，而是 `TypeSoftPromoteHalf`（`TargetLoweringBase.cpp:1508-1524`）。注释写得很直白：*promote it to f32, because there are no f16 library calls (except for conversions)*。所以**不会**出现 `__addhf3`：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+f -O2 ch13.ll -o - | sed -n '/^hadd:/,/ret/p'
```

```asm
hadd:                                   # half @hadd(half %a, half %b)
	fmv.s	fs0, fa0
	fmv.s	fa0, fa1
	call	__extendhfsf2
	fmv.s	fs1, fa0
	fmv.s	fa0, fs0
	call	__extendhfsf2
	fadd.s	fa0, fa0, fs1           # 算术在 f32 上做
	call	__truncsfhf2
```

有 `Zfhmin` 时 f16 成为合法类型（`FPR16`），但算术仍被 Promote，只是转换变成了指令：

```asm
hadd:                                   # -mattr=+f,+zfhmin
	fcvt.s.h	fa5, fa1
	fcvt.s.h	fa4, fa0
	fadd.s	fa5, fa4, fa5
	fcvt.h.s	fa0, fa5
	ret
```

`f64` 没有 D 时是标准的 `TypeSoftenFloat`（`TargetLoweringBase.cpp:1501-1506`），在 RV32 上被换成 `i32`（两个寄存器），在 RV64 上换成 `i64`：

```asm
dadd:                                   # -mtriple=riscv32 -mattr=+f
	call	__adddf3
```

---

## 13.7 两个回调：`LowerOperation` 与 `ReplaceNodeResults`

表里只有一个 `Custom` 位，但目标有两个 hook。分派规则不在 RISC-V 里，在 generic legalizer 里：

| | `LowerOperation` | `ReplaceNodeResults` |
|---|---|---|
| 行号 | `RISCVISelLowering.cpp:8374` | `:17150` |
| 触发阶段 | **操作**合法化（`SelectionDAGLegalize`） | **类型**合法化（`DAGTypeLegalizer`） |
| 调用者 | `LegalizeDAG.cpp:518-523` → `LowerOperationWrapper`（`SelectionDAGBuilder.cpp:11946`） | `LegalizeTypes.cpp:908-917` 的 `CustomLowerNode(..., LegalizeResult=true)` |
| 签名 | `SDValue (SDValue Op, SelectionDAG &DAG)` | `void (SDNode *N, SmallVectorImpl<SDValue> &Results, SelectionDAG &DAG)` |
| 前提 | 结果的**类型合法**，操作不合法 | 结果的**类型不合法** |
| 返回空 | 节点留在 DAG → isel `Cannot select` | 放弃自定义 → 回落默认展开/libcall |
| 典型 case | `SELECT`(8398)、`BRCOND`(8400)、`VASTART` | RV32 `MUL i64`(17343)、RV32 `FP_TO_SINT i64`(17157) |

`CustomLowerNode` 是分岔口，代码只有二十多行：

```cpp
// LegalizeTypes.cpp:908
bool DAGTypeLegalizer::CustomLowerNode(SDNode *N, EVT VT, bool LegalizeResult) {
  if (TLI.getOperationAction(N->getOpcode(), VT) != TargetLowering::Custom)
    return false;
  SmallVector<SDValue, 8> Results;
  if (LegalizeResult)
    TLI.ReplaceNodeResults(N, Results, DAG);   // 结果类型非法
  else
    TLI.LowerOperationWrapper(N, Results, DAG); // 操作数非法 / 操作非法
  ...
}
```

所以"同一个 `Custom`"在 RV32 的 `mul i64` 上落到 `ReplaceNodeResults:17343`，在 RV64 的 `select i64` 上落到 `LowerOperation:8398`——**区别不在 opcode，而在被合法化的那个类型是否合法**。

`ReplaceNodeResults` 的 MUL case 值得读一遍，因为它把"类型分裂"和"操作选择"揉在了一起：

```cpp
// :17343
case ISD::MUL: {
  unsigned Size = N->getSimpleValueType(0).getSizeInBits();
  unsigned XLen = Subtarget.getXLen();
  if (Size > XLen) {                    // RV32 的 i64、RV64 的 i128
    ...
    if (LHSIsU && RHSIsU)
      Results.push_back(MakeMULPair(LHS, RHS, ISD::MULHU, ISD::UMUL_LOHI));
  }
```

`MakeMULPair` 里就是 `TRUNCATE` 到 XLEN、`MUL` + `MULHU`/`MULHS`、再 `BUILD_PAIR`。它不是 pattern 能表达的东西，所以只能写在 C++ 回调里。

反过来看 `LowerOperation` 的分派表，它同时服务三类节点：地址类（`GlobalAddress`/`ConstantPool`/`JumpTable`）、指令选择类（`SELECT`/`BRCOND`）、以及 pseudo 类（`SHL_PARTS`/`VASTART`）。共同点是**这些节点的类型已经合法**，只是没有单条指令能直接选。

---

## 13.8 源码落点

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**（本树 31,346 行）
  - 67–253：构造函数开头（寄存器类与派生属性）；82–101 `addRegisterClass`，253 `computeRegisterProperties`。
  - 264–331：最早的一批 `setOperationAction`；268 `BRCOND`、269 `SELECT_CC`、294 `SIGN_EXTEND_INREG`、297 `LOAD i64`(Zilsd)、315/320 `MUL`、324 `SDIV`。
  - 430：`SELECT` 的 Custom 条件；487/752/813：`FREM` 的 `LibCall`。
  - 837：RV32 + ZfhminOrZhinxmin 的 `FP_TO_SINT/UINT i64` Custom；8374–8398：`LowerOperation` 与 `case ISD::SELECT`；10989 `lowerBRCOND`；10613 `lowerSELECT`，10695 Zicond。
  - 17150–18993：`ReplaceNodeResults`；17156 default unreachable，17157 `FP_TO_SINT`，17343 `MUL`。
- **`llvm/include/llvm/CodeGen/TargetLowering.h`**
  - 222–228：`LegalizeAction` 五值（`Legal/Promote/Expand/LibCall/Custom`）。
  - 232–249：`LegalizeTypeAction`（含 `TypeSoftPromoteHalf`）。
  - 1143/1146：`ValueTypeActions` 与其默认 `TypeLegal`。
  - 1366：`getOperationAction`；2814：`setOperationAction`；3977：`OpActions` 数组。
- **`llvm/lib/CodeGen/TargetLoweringBase.cpp`**
  - 767：`memset(OpActions, 0, ...)`，默认即 `Legal`。
  - 1416–1453：整数类型的 Promote/Expand 推导；1501–1524：`f32`/`f16` 的软化规则。
- **`llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp`**
  - 908–930：`CustomLowerNode`，两个 hook 的分岔。
- **`llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp`**
  - 518–524：`Custom` → `LowerOperation`；4018–4032 与 5245–5265：整数 div/rem 的 Expand 与 libcall 表。
- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - 999：`CurDAG->LegalizeTypes()`；1097：`CurDAG->Legalize()`（操作合法化），顺序不能反。

---

## 13.9 可跑实验

### 实验 1：看一眼 legalize 前后的 DAG

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 -debug-only=isel-dump ch13.ll -o /dev/null
```

真实输出（节选，缩进是 DAG 的 use-def 层级）：

```
Initial selection DAG: %bb.0 'add64:'
      t2: i32,ch = CopyFromReg t0, Register:i32 %0
    t9: i64 = build_pair t2, t4
  t11: i64 = add t9, t10

Type-legalized selection DAG: %bb.0 'add64:'
  t21: i32 = add t2, t6
      t23: i32 = setcc t21, t2, setult:ch
    t25: i32 = add t24, t23
```

`-debug-only=legalize-types,dagcombine` 也可以，但 `isel-dump` 的信息密度最高：它直接给你"前后两张图"。

### 实验 2：RV32 的 i64 乘法，+m 与 -m 是两个世界

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 ch13.ll -o -  | sed -n '/^mul64:/,/ret/p'
$T/bin/llc -mtriple=riscv32 -mattr=-m -O2 ch13.ll -o - | sed -n '/^mul64:/,/ret/p'
```

```asm
# +m（Zmmul 随 M 打开，命中 :320 的 Custom）
	mul	a3, a0, a3
	mulhu	a4, a0, a2
	mul	a1, a1, a2
	mul	a0, a0, a2
	add	a3, a4, a3
	add	a1, a3, a1

# -m（:315 的 Expand，落成 libcall）
	call	__muldi3
```

`-mattr=+m` 时 `.attribute 5` 是 `"rv32i2p1_m2p0_zmmul1p0"`；`-mattr=-m` 时只剩 `"rv32i2p1"`。这一行属性就解释了为什么两条路不同。

### 实验 3：libcall 动物园：除法、取余、frem

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 ch13.ll -o -  | sed -n '/^div64:/,/ret/p'   # i64 sdiv
$T/bin/llc -mtriple=riscv32 -mattr= -O2 ch13.ll -o -    | sed -n '/^rem32:/,/ret/p'   # i32 urem
$T/bin/llc -mtriple=riscv64 -mattr=+f -O2 ch13.ll -o -  | sed -n '/^frem32:/,/ret/p'  # float frem
$T/bin/llc -mtriple=riscv64 -mattr=+m -O2 ch13.ll -o -  | sed -n '/^div32:/,/ret/p'   # i32 sdiv
```

```asm
div64:   call __divdi3          # RV32 + M：i64 除法仍是 libcall
rem32:   tail __umodsi3         # 无 M：Expand → libcall
frem32:  tail fmodf             # LibCall：不尝试展开
div32:   divw a0, a0, a1        # RV64 + M：i32 Custom → W 指令
```

最后一行是 RV64 的对照组：同一个 `i32 sdiv`，RV64 走 `:327` 的 Custom 选中 `divw`。如果只看 `ISD::SDIV` 不看类型，会以为"RISC-V 除法都是 libcall"。

### 实验 4：`select` 的两副面孔

```bash
$T/bin/llc -mtriple=riscv64 -O2 ch13.ll -o -                 | sed -n '/^sel:/,/ret/p'
$T/bin/llc -mtriple=riscv64 -mattr=+zicond -O2 ch13.ll -o - | sed -n '/^sel:/,/ret/p'
```

```asm
# 默认：lowerSELECT 产出 RISCVISD::SELECT_CC，最终是分支
	andi	a3, a0, 1
	mv	a0, a1
	bnez	a3, .LBB8_2
	mv	a0, a2

# +zicond：同一个 Custom 走另一条分支
	andi	a0, a0, 1
	czero.nez	a2, a2, a0
	czero.eqz	a0, a1, a0
	or	a0, a0, a2
```

再看操作合法化阶段怎么说：

```bash
$T/bin/llc -mtriple=riscv64 -O2 -debug-only=legalizedag ch13.ll -o /dev/null
```

```
Legalizing: t8: i64 = select t14, t4, t6
Trying custom legalization
 ... replacing: t8: i64 = select t14, t4, t6
```

注意 debug 文本是 `Trying custom legalization`（`LegalizeDAG.cpp:519`），不是 `Trying custom lowering`——按字符串搜代码时别搜错。

### 实验 5：合法化之后的 MIR

```bash
$T/bin/llc -mtriple=riscv32 -mattr=+m -O2 -stop-after=finalize-isel -simplify-mir ch13.ll -o - | sed -n '/name: *add64/,/^\.\.\./p'
```

```
liveins:
  - { reg: '$x10', virtual-reg: '%0' }
  - { reg: '$x11', virtual-reg: '%1' }
  - { reg: '$x12', virtual-reg: '%2' }
  - { reg: '$x13', virtual-reg: '%3' }
```

一个 `i64` 参数占两个 GPR：两个参数就是 4 个 `liveins`。到 MIR 这一层，类型已经"消失"了，只剩下寄存器类（`gpr`）。

---

## 13.10 踩雷集锦

1. **忘了写表项，以为会退化。** `OpActions` 默认 `Legal`（`TargetLoweringBase.cpp:767`）；漏写的结果是 isel `Cannot select`，不是慢代码。
2. **把 `Custom` 写给了错误类型。** RV64 的 `i32` 操作靠 Custom 在类型合法化时换成 W 指令（`addw`/`divw`/`sllw`）；改成 `XLenVT` 后这些节点先被提升到 i64，`sllw` 的 5 位移位量语义与 W 编码优势都没了。
3. **`LowerOperation` 返回空 `SDValue`。** `LegalizeDAG.cpp:521` 只在 `Res && Res != Node` 时替换，空值等于"什么也没做"，节点原样留到 isel。`ReplaceNodeResults` 返回空则是合法回落（例如 `:17174` 的 `if (Op0.getValueType() != MVT::f16) return;`）。
4. **以为 `Expand` 一定内联。** `SDIV` 在 RV64 的窄类型上是 Custom，没 M 时是 Expand，而 Expand 的终点是 `__divsi3`。判断"会不会调库"要看 legalizer 有没有可用的展开路径，不能只看枚举名。
5. **把 `Zfhmin` 当 `Zfh`。** Zfhmin 只保证 f16↔f32 转换；算术被 `:688` Promote 到 f32。实测里看到 `fcvt.s.h`/`fcvt.h.s` 而不是 `fadd.h`，就是这条。
6. **以为 f16 会调 `__addhf3`。** LLVM 的 `TypeSoftPromoteHalf` 注释明确说没有 f16 算术 libcall；实际是 `__extendhfsf2` + `fadd.s` + `__truncsfhf2`。
7. **给 RV32 的 `i64` 写普通 pattern。** `i64` 在类型合法化阶段就被拆成两个 `i32`，pattern 永远等不到 `i64` 节点；RV32 的 64 位操作要么走 Custom（`ReplaceNodeResults`），要么走 libcall。

---

## 13.11 本章重点整理

- Legalization 的目标是"把 DAG 修剪成目标支持的子集"，不是"生成好代码"；好代码是 DAGCombine 和 isel 的事。
- 两张表：`ValueTypeActions[VT]`（由 `addRegisterClass` + `computeRegisterProperties` 推导）与 `OpActions[VT][Op]`（由构造函数里 469 处 `setOperationAction` 写）。
- RISC-V 的合法标量类型来自 `addRegisterClass`：`XLenVT` 恒合法；`f16/f32/f64` 分别可由 `Zfhmin`/`F`/`D` 或 `Zhinxmin`/`Zfinx`/`Zdinx` 提供，后者进 GPR。
- 窄整数（i1/i8/i16）→ `XLenVT`；RV32 的 i64 → 2×i32；i128 → 2×i64（RV32 净效果 4×i32）。
- f16 无 `Zfhmin` 且无 `Zhinxmin` 是 `TypeSoftPromoteHalf`（f32 算术 + 转换 libcall），f64 无 D 是 `TypeSoftenFloat`（`__adddf3`）。
- `Custom` 有两个落点：类型合法化 → `ReplaceNodeResults`（17150）；操作合法化 → `LowerOperation`（8374）。

---

## 13.12 自我检核

- [ ] 能说出 `OpActions` 和 `ValueTypeActions` 分别在哪个头文件、默认值是什么，以及"漏写表项"的后果
- [ ] 给定 `-mtriple=riscv32 -mattr=+m`，能说出 `mul i64`、`sdiv i64`、`sdiv i32` 各自的结局
- [ ] 能解释 `Zfhmin` 与 `Zfh` 的区别，以及为什么 `fadd half` 在只有 Zfhmin 时是 `fcvt`+`fadd.s`
- [ ] 能说出 `LowerOperation` 与 `ReplaceNodeResults` 各自被哪个 legalizer、哪一行调用

---

## 13.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**：67–331（构造函数与表）、17150–18993（`ReplaceNodeResults`）；RISC-V 对"我能执行什么"的完整声明，也是所有 legalize bug 的第一现场。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/TargetLoweringBase.cpp`**：1416–1534；类型动作不是 RISC-V 手写的，是这里从寄存器类推导出来的，读懂才能预测"没加 addRegisterClass 会怎样"。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp`**：196–330、908–930；它决定 `ReplaceNodeResults` 什么时候被调用。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp`**：518–524、4018–4032、5245–5265；`Custom` → `LowerOperation`、`Expand` → libcall 两条主干。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/`**：`mul.ll`、`zmmul.ll`、`div.ll`、`select.ll`、`half-arith.ll`、`fp16-promote.ll`；用 `-debug-only` 重跑这些测试，是最快的对照实验。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/include/llvm/IR/RuntimeLibcalls.td`**：541–551 定义 `FPEXT_F16_F32` 等签名，1355–1361 是 `__extendhfsf2`/`__truncsfhf2`；libcall 的名字与参数类型以这里为准。

---

→ 下一章：[Ch 14 — SelectionDAG 选指与 pattern](./14-selection-dag-patterns.md)
