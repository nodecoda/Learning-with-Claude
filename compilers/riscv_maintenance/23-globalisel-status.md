# Ch 23 — GlobalISel 现状：能跑到哪、不能跑到哪

> **目标**：把“RISC-V GlobalISel 已经能用了”这句话拆成可验证的判断：五个阶段各自完成到什么程度、fallback 在什么条件下发生、`-global-isel-abort=0/1/2` 分别改变什么，以及哪些缺口今天就能上手补。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（`X86;AArch64;RISCV`，assertions ON），源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`，分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`，LLVM `24.0.0git`。实验目录 `/tmp/ch23`。

> **前置**：读完 Ch 09（`CC_RISCV` 与 `LowerFormalArguments`/`LowerCall`/`LowerReturn`）、Ch 13（Legalization 的两张表）、Ch 14（TableGen 选指与 `MatcherTable`）。本章不重复 SDAG 的机制，只讲 GISel 的对应物和边界。

---

## 23.1 为什么维护者要关心 GISel，但它仍不是默认后端

GISel 是 LLVM 的长期方向，也是 RISC-V 后端里“可贡献缺口”最密集的地方。这棵树上的硬数字：

| 事实 | 实测值 | 命令 |
|---|---|---|
| GISel 专属源码 | 13 个文件、6,529 行（含 4 个头文件） | `wc -l llvm/lib/Target/RISCV/GISel/*` |
| 过去 12 个月改到 `GISel/` 的提交 | 88 条（累计 450 条） | `git log --since=2025-11-08 -- llvm/lib/Target/RISCV/GISel` |
| 过去 12 个月改到 GISel 测试的提交 | 118 条 | 同上，路径换成 `llvm/test/CodeGen/RISCV/GlobalISel` |
| GISel 测试 | 500 个文件（154 `.ll` + 346 `.mir`） | `find llvm/test/CodeGen/RISCV/GlobalISel -type f` |
| 明确承认“还不支持”的测试 | `riscv-unsupported.ll`，文件注释写着 *It should progressively shrink in size* | 见 23.9 |

同时必须讲清一半事实：**默认后端仍然是 SelectionDAG**。`-global-isel` 是 opt-in，`RISCVPassConfig::addInstSelector()`（`RISCVTargetMachine.cpp:449`）才是默认路径；`gisel-commandline-option.ll` 用 `-debug-pass=Structure` 明确检查了“不传 `-global-isel` 时没有 `IRTranslator`、只有 `RISC-V DAG->DAG Pattern Instruction Selection`”。

所以“改 GISel”不等于“改默认后端”。两者的共享面和非共享面要分开看：

| 层 | SDAG 与 GISel 是否共享 | 例子 |
|---|---|---|
| MC / 编码 / 汇编打印 | 共享 | `RISCVInstrInfo*.td`、`RISCVMCCodeEmitter.cpp` |
| ABI 与调用约定 | **规则共享、实现两份** | `RISCVCallingConv.td` + `CC_RISCV`；SDAG 在 `LowerCall`，GISel 在 `RISCVCallLowering` |
| 合法化 | 不共享 | `setOperationAction`（469 处）vs `getActionDefinitionsBuilder`（95 处） |
| 选指 | 不共享（pattern 源共享） | `RISCVGenDAGISel.inc` 61 MB vs `RISCVGenGlobalISel.inc` 45 MB |
| 寄存器分配之后的机器 pass | 共享 | Ch 19 的 `riscv-opt-w-instrs`、压缩、`frm`/`vxrm` |

这条边界决定了两类补丁的风险：动 `RISCVInstrInfo.td`、`RISCVRegisterInfo.td`、ABI 代码，会同时影响默认后端和 GISel；只动 `GISel/` 下的文件，默认 CI 的代码生成输出不变，但 GISel 测试会立刻告诉你有没有回归。另一个容易误解的点是“fallback 会不会兜底”。会，但**默认不兜底**：在 `llc` 里只写 `-global-isel` 时 `GlobalISelAbort` 取默认值 `Enable`（`TargetOptions.h:147`），遇到不支持就 `LLVM ERROR`。要它退回 SDAG，必须显式写 `-global-isel-abort=0` 或 `=2`。23.7 有实测。

---

## 23.2 先建立直觉：五阶段与 RISC-V 的完成度

教材口径的五站是：

```text
IRTranslator → Legalizer (RISCVLegalizerInfo) → RegBankSelect (RISCVRegisterBankInfo)
             → InstructionSelect (RISCVInstructionSelector) → PostLegalizerCombiner
```

但真实 pass 顺序不是这条直线。`-debug-pass=Structure -O2 -global-isel` 的实测顺序是：

```text
LLVM IR
  │ IRTranslator                        RISCVCallLowering / RISCVInlineAsmLowering
  ▼
gMIR：只有 LLT 类型，没有寄存器类        %0:_(s64) = G_ADD %1, %2
  │ RISCVPreLegalizerCombiner           all_combines（O0 换成 RISCVO0PreLegalizerCombiner）
  ▼
  │ Legalizer                            RISCVLegalizerInfo：95 个 builder
  ▼
合法 gMIR
  │ RISCVPostLegalizerCombiner          ← 注意：在 RegBankSelect 之前，不在最后
  ▼
  │ RegBankSelect                       RISCVRegisterBankInfo：GPRB / FPRB / VRB
  ▼
带 bank 的 gMIR                         %0:gprb(s64) = G_ADD %1, %2
  │ InstructionSelect                   RISCVInstructionSelector：selectImpl + 11 个手写钩子
  ▼
带寄存器类的 MIR                        %0:gpr(s64) = ADD %1, %2
  │ ResetMachineFunction                abort=0/2 时，失败函数在这里被重置并转交 SDAG
  ▼
（abort=1 到此为止；abort=0/2 且失败时才追加 RISC-V DAG->DAG）
```

一次 `add` 的 MIR 在三个阶段的变化可以直接看到（23.11 实验 E1）：

| 阶段 | 寄存器类 | 指令 |
|---|---|---|
| `-stop-after=ir-translator` | `_` | `%2:_(s64) = G_ADD %0, %1` |
| `-stop-before=instruction-select` | `gprb` | `%2:gprb(s64) = G_ADD %0, %1` |
| `-stop-after=instruction-select` | `gpr` | `%2:gpr(s64) = ADD %0, %1` |

五个站点的 RISC-V 完成度，按“能不能独立跑完一个普通函数”判断：

| 阶段 | RISC-V 实现 | 判定 | 主要边界 |
|---|---|---|---|
| IRTranslator | `RISCVCallLowering`（671 行）+ `RISCVInlineAsmLowering`（68 行） | 部分支持（标量） | byval、i128 多寄存器 inline asm、RVV tuple 类型会失败 |
| Legalizer | `RISCVLegalizerInfo`（1,925 行，95 个 builder） | 部分支持（主力标量/RVV） | 定长向量、strict FP、EH、部分 atomic/minmax |
| RegBankSelect | `RISCVRegisterBankInfo`（636 行，3 个 bank） | 部分支持 | 向量 LMUL 只能靠 64/128/256/512 四种尺寸表达 |
| InstructionSelect | `RISCVInstructionSelector`（2,376 行 + 45 MB 生成匹配器） | 部分支持（失败靠 fallback） | TLS、定长向量、部分 intrinsic 直接 `cannot select` |
| PostLegalizerCombiner | `RISCVPostLegalizerCombiner`（253 行，1 条 RISC-V 专属规则） | 部分支持（靠通用规则） | 规则少，优化力度低于 SDAG DAGCombine |

`-global-isel-abort` 的三个值，测试里各有用途：

| 值 | 枚举（`TargetOptions.h:70-74`） | 行为 | 实测管线差异 |
|---|---|---|---|
| `0` | `Disable` | 静默回退，stderr 0 字节 | 追加 `RISC-V DAG->DAG` |
| `1` | `Enable`（**`llc` 只写 `-global-isel` 时的默认值**） | 失败即 `LLVM ERROR` | 不追加 DAG 选指 |
| `2` | `DisableWithDiag` | 回退并打印 `warning: Instruction selection used fallback path for <fn>` | 追加 `RISC-V DAG->DAG`，退出码仍为 0 |

测试里的分工：默认 `1` 用来锁死“这个函数不许 fallback”；`0` 只用于“已知会 fallback、但想检查最终汇编”的端到端用例；`2` 配合 `-pass-remarks-missed='gisel*'` 断言诊断内容。

---

## 23.3 `RISCVCallLowering`：ABI 的第二份实现

SDAG 与 GISel 的入口一一对应，但代码完全不共享：

| 职责 | SDAG（`RISCVISelLowering.cpp`） | GISel（`GISel/RISCVCallLowering.cpp`） |
|---|---|---|
| 入参 | `LowerFormalArguments` 28701 | `lowerFormalArguments` 526 |
| 返回值 | `LowerReturn` 29539；`CanLowerReturn` 29529 | `lowerReturn` 371；`canLowerReturn` 416 |
| 调用 | `LowerCall` 29013 | `lowerCall` 585 |
| 可变参数保存区 | `LowerFormalArguments` 内的 vararg 分支 | `saveVarArgRegisters` 429 |

类型闸门是两个 `static bool`，不是 AArch64 那种 `getUnsupportedArgTypes` 列表。在本树执行 `grep -n getUnsupportedArgTypes llvm/lib/Target/RISCV/GISel/RISCVCallLowering.cpp` 得到 **0 条**——RISC-V 用“返回 false”表达不支持：

| 函数 | 行 | 接受 | 拒绝 |
|---|---|---|---|
| `isSupportedArgumentType` | 320 | 整数、`half/float/double/fp128`、指针、数组；`+v` 下元素类型合法的 scalable vector；定长向量**返回 true** | 结构体按值、其他向量/tuple 类型 |
| `isSupportedReturnType` | 345 | 整数/FP/指针、数组、递归检查的结构体；`IsLowerRetVal=true` 时也接受 scalable vector；定长向量**返回 true** | 含不支持元素的聚合、其他向量/tuple |

两个闸门之上还有两个明确的拒绝点，外加一个“延后失败”的类型：

1. **byval 调用**：`lowerCall` 里 `if (AInfo.Flags[0].isByVal()) return false;`（596）。`lowerFormalArguments` 不查 byval，所以是“调用侧”先失败。
2. **尾调用**：`Info.IsTailCall = false;`（615-616），注释就是 `TODO: Support tail calls.`。这会把 `musttail` 也降级成普通调用——23.7 有实测，这是当前最值得修的语义缺口之一。
3. **定长向量**：`isSupportedArgumentType` 对定长向量返回 true（注释却写着 `TODO: Support fixed vector types`），真正的失败推到 Legalizer，报错位置因此更靠后。**结构体按值**则相反：参数闸门没有 struct 分支，`abort=1` 直接报 `LLVM ERROR: unable to lower arguments`（`abort=0` 仍静默 fallback）。

任一处 `return false`，在 `-global-isel-abort=2` 下 `IRTranslator` 的出口是可 grep 的 remark + warning；`=1` 把它变成 `LLVM ERROR: unable to translate instruction: ...`，`=0` 则完全静默：

```text
remark: <unknown>:0:0: unable to translate instruction: call: '  call void @callee(ptr byval(%struct.S) %p)' (in function: caller)
warning: Instruction selection used fallback path for caller
```

可变参数是**支持**的：`saveVarArgRegisters` 按 `CC_RISCV` 把 `a0–a7` 中未用完的寄存器写进 varargs save area，并保留“奇数个寄存器要补一个 slot 保证 2×XLEN 对齐”的规则；`vararg.ll` 覆盖了 `va_start`/`va_arg`。

最后一个维护陷阱：RV64 上“入参 i32 已按 ABI 符号扩展”这个事实，GISel 用 `markSExt32Registers`（497）重新记录到 `RISCVMachineFunctionInfo`，供 `RISCVOptWInstrs` 使用。这是 ABI 规则的**第二份实现**。改 `CC_RISCV` 的扩展规则时，SDAG 的 `LowerFormalArguments` 和这里必须同步，否则一边删 `sext.w`、另一边不删，或者更糟——该扩展的没扩展。

---

## 23.4 `RISCVLegalizerInfo`：用 builder 写“能力声明”

SDAG 用 469 次 `setOperationAction(Op, VT, Action)` 往二维表里写；GISel 用 95 个 `getActionDefinitionsBuilder(...)` 链式声明“谓词 → 动作 → 类型变换”。风格对照：

```cpp
// SDAG：RISCVISelLowering.cpp:314-319
if (!Subtarget.hasStdExtZmmul())
  setOperationAction({ISD::MUL, ISD::MULHS, ISD::MULHU}, XLenVT, Expand);
else if (Subtarget.is64Bit()) {
  setOperationAction(ISD::MUL, MVT::i128, Custom);
  setOperationAction(ISD::MUL, MVT::i32, Custom);
}

// GISel：RISCVLegalizerInfo.cpp:519-522（没有 Zmmul 的分支）
getActionDefinitionsBuilder(G_MUL)
    .libcallFor({sXLen, sDoubleXLen})
    .widenScalarToNextPow2(0)
    .clampScalar(0, sXLen, sDoubleXLen);
```

五个真实 op 的对应关系（行号均可 `grep -n` 复核）：

| 操作 | SDAG | GISel | 后果 |
|---|---|---|---|
| `MUL`，无 Zmmul | `Expand`（315） | `libcallFor`（520） | SDAG 对常量乘展开成移位/加减；GISel 直接 `call __muldi3`（23.7 实测） |
| `SDIV/UDIV`，无 M | `Expand`（324） | `libcallFor`（550） | 两者都落到 `__divdi3`，但 SDAG 能 `tail`，GISel 不能 |
| `SELECT` | `Custom`（430） | `legalFor`（341-348），选指阶段交给 `selectSelect`（2125） | GISel 没有 select 的 legalize 动作，靠手写选指 |
| `CTLZ`，无 Zbb | `Expand`（408） | `maxScalar(...).lower()`（268） | 两边都展开，展开序列由各自实现决定 |
| `ATOMIC_FENCE` | `Custom`（905） | `G_FENCE` `alwaysLegal`（887）+ `emitFence`（2309） | GISel 在选指阶段直接发 `fence`/`fence.tso` |

API 的真实用法：

- `clampScalar(0, sXLen, sXLen)`：把第 0 类型的标量宽度夹到 `[XLEN, XLEN]`，中间宽度由 `widenScalarToNextPow2` 逐级提升；`G_ADD`（141-146）里 RV64 的 `s32` 随后由 `customFor` 接手。
- `widenScalarToNextPow2(0)`：把宽度提到下一个 2 的幂，再交给 `clamp`。`G_AND/G_OR/G_XOR`（148-152）就是这两条连用。
- `widenScalarOrEltToNextPow2OrMinSize(1, 8)`：对“标量或向量元素”同时生效，`G_ICMP`（333-339）用它把比较宽度至少提到 8 位；`scalarSameSizeAs(1, 0)` 则让第 1 个类型跟随第 0 个（`G_CTLZ`/`G_CTPOP`）。
- `.lowerForFP` 在本文件**不存在**（`grep -c lowerForFP` = 0）；FP 走 `legalFor` + `libcallFor` 组合，例如 FP 算术（593-600）与 `G_FREM`（608-612）。
- `.unsupported()` 是真正的“拒绝”：`G_CLMUL`（237-239）、`G_ATOMICRMW_MAX/MIN/UMAX/UMIN`（865-868）在没有对应扩展时直接标 `unsupported`，不会尝试展开。

Custom 动作落到 `legalizeCustom`（1707）的 `switch`，值得先读的四个：

| 函数 | 行 | 做什么 |
|---|---|---|
| `legalizeCustom` | 1707 | 所有 `customFor/customIf` 的总入口 |
| `legalizeReadCounter` | 1012 | RV32 读 64 位 `cycle/time`：拆高低半 + 重读高位循环 |
| `legalizeIntrinsic` | 894 | `llvm.riscv.*` intrinsic 的合法化与标量操作数处理 |
| `legalizeLoadStore` | 1274 | 向量/指针 load/store 的 bank 与类型修正 |

一个必须记住的语义：**builder 的规则是顺序敏感的**。先 `legalFor` 再 `clampScalar`，表示“先看是不是本来就合法，不合法才变换”；反过来写就会把合法类型也改掉。读任何一段 builder，先看第一条匹配规则，再看最后的 fallback 动作。

---

## 23.5 `RISCVRegisterBankInfo` + `RISCVRegisterBanks.td`：三个 bank 与向量之难

`RISCVRegisterBanks.td` 只有 19 行，定义三个 bank：

```tablegen
def GPRBRegBank : RegisterBank<"GPRB", [GPR, GPRPair]>;
def FPRBRegBank : RegisterBank<"FPRB", [FPR64]>;
def VRBRegBank  : RegisterBank<"VRB",  [VRM8]>;
```

`[GPR, GPRPair]` 里的类是“代表类”，TableGen 会展开覆盖率：`RISCVGenRegisterBank.inc` 的 `FPRBRegBankCoverageData` 同时列出了 `FPR16/FPR32/FPR16C/FPR32C/FPR64/FPR64C`，所以只写 `FPR64` 也能让所有 FPR 类归到 FPRB。GPR 侧另有 `VCSR` 特判：`getRegBankFromRegClass`（117-127）把 `VCSRRegClassID` 强制映射到 GPRB，其余交给生成代码。

`getInstrMapping`（243）的结构是“默认 + 特判”：

1. 非 generic 指令或 `G_PHI`：先试 `getInstrMappingImpl(MI)`（`RegisterBankInfo.cpp:152`），能推出 bank 就直接用。
2. 大 `switch` 覆盖 `G_ADD`/`G_SUB`/`G_MUL`/`G_FADD`/`G_LOAD`/`G_STORE`/`G_INTRINSIC`/`G_SPLAT_VECTOR` 等。
3. `default` 分支：标量全给 GPR，向量按尺寸给 VRB，FP opcode 给 FPR。

`getInstrMapping` 只决定 bank，不决定寄存器类。把 `(LLT, bank)` 变成具体寄存器类的是 `RISCVRegisterInfo::getRegClassForTypeOnBank`（`RISCVRegisterInfo.cpp:132`）：

| LLT 尺寸 | GPRB | FPRB | VRB |
|---|---|---|---|
| 16 / 32 | `GPR` | `FPR16` / `FPR32` | — |
| 64 | `GPR`（仅 RV64） | `FPR64` | `VR` |
| 128 | — | — | `VRM2` |
| 256 | — | — | `VRM4` |
| 512 | — | — | `VRM8` |

向量 bank 为什么复杂，集中在这四点：

1. **尺寸是“最小位宽”**。`<vscale x 4 x i32>` 的 `getSizeInBits()` 是 `TypeSize`，`getKnownMinValue()` 只有 128，实际运行时可能是 128/256/…。bank 本身不编码 LMUL。
2. **LMUL 只能用 64/128/256/512 表达**。`getVRBValueMapping`（225-241）对 `<=64` 一律给 VRB64，对 `>64` 且不是 128/256/512 的尺寸直接 `report_fatal_error("Invalid Size")`——这是崩溃，不是 fallback。
3. **RVV intrinsic 的操作数混合**。`G_INTRINSIC`（575-620）要区分向量操作数、标量操作数和 FP 标量操作数；`riscv_vsetvli`/`riscv_orc_b` 还要把所有寄存器操作数压回 GPR。
4. **标量可能来自 FPR 或 GPR**。`G_SPLAT_VECTOR`（560-577）要看标量定义者是不是 FP 指令，才能决定第二个操作数是 FPRB 还是 GPRB；`G_IMPLICIT_DEF` 也有同类 FIXME。

---

## 23.6 `RISCVInstructionSelector`：45 MB 生成匹配器 + 11 个手写钩子

2,376 行里，主体不是 pattern，而是“生成匹配器之外的补丁”。`select()`（1304）的顺序是：

```text
preISelLower(MI)            // 1701，选指前改写
  → selectImpl(MI, ...)     // TableGen 生成，命中即返回
  → switch (Opc)            // 手写钩子：copy/constant/global/brcond/select/fcmp/load/store/...
  → default: return false   // 选不出来 → FailedISel → fallback
```

`selectImpl` 来自 `RISCVGenGlobalISel.inc`：生成文件 45,550,453 字节、489,833 行，`selectImpl` 在第 1674 行，`getMatchTable()` 在 1699 行。对照 Ch 14 的 `RISCVGenDAGISel.inc`（61 MB、680,322 行）可以看出：GISel 匹配器小一些，但“手写补丁”的比例高得多。

三个真实的 `select*` 钩子（作业：各读 30 行，说出它替代了哪条 SDAG pattern）：

| 函数 | 行 | 触发条件 | 产出 |
|---|---|---|---|
| `selectAddr` | 2011 | `G_GLOBAL_VALUE` / `G_JUMP_TABLE` / `G_CONSTANT_POOL` | `PseudoLLA`/`PseudoLGA`/`PseudoLA*`，按 PIC 与 code model 分叉 |
| `selectSelect` | 2125 | `G_SELECT` | `Select_GPR_Using_CC_GPR` 或 `Select_FPR32/64_Using_CC_GPR` |
| `selectFPCompare` | 2206 | `G_FCMP` | `FLT/FLE/FEQ` 组合；不支持谓词用交换/取反或 `OR`/`AND` 展开 |

另外八个：`selectCopy`（1825）、`selectImplicitDef`（1850）、`selectMergeValues`（1654）、`selectUnmergeValues`（1596）、`selectIntrinsic`（1132）、`selectIntrinsicWithSideEffects`（892）、`selectExtractSubvector`（1206）、`selectInsertSubVector`（1246）。

地址折叠靠 `ComplexPattern` 的 C++ 渲染器，与 SDAG 的 `SelectAddr*` 平行：

| 渲染器 | 行 | 形状 |
|---|---|---|
| `selectAddrRegImm` | 600 | `FrameIndex`、`G_PTR_ADD + simm12`、全局地址 |
| `selectAddrRegImmLsb00000` | 679 | 低 5 位为 0（Zicbo 预取/缓存块） |
| `selectSHXADDOp` | 同文件 147 | `base + index*N`（Zba） |

`RISCVGISel.td` 里用 `GICustomOperandRenderer<"renderImmPlus1">` 把 SDAG 的 `ImmPlus1` 搬过来，这是“pattern 源共享、渲染器重写”的典型样本。

寄存器约束有三条不同路径，别混：

- `constrainSelectedInstRegOperands(...)`：选指阶段最常用，本文件 43 处。
- `RBI.constrainGenericRegister(...)`：`selectCopy`/`selectImplicitDef` 用，把 vreg 绑到具体类。
- `constrainOperandRegClass(...)`：本文件**没有**；唯一调用在 `RISCVCallLowering.cpp:650`，用来约束间接调用的 callee 操作数。

调试开关不是 `-debug-only=global-isel`（没有这个 `DEBUG_TYPE`）。本文件是 `#define DEBUG_TYPE "riscv-isel"`（30），所以用 `-debug-only=riscv-isel`；通用阶段分别用 `ir-translator`、`legalizer`、`reg-bank-select`、`instruction-select`。

---

## 23.7 fallback 的真实行为（全部在本机构建实测）

先固定样本：

```bash
mkdir -p /tmp/ch23 && cd /tmp/ch23
cat > simple.ll <<'IR'
define i64 @add(i64 %a, i64 %b) { %r = add i64 %a, %b  ret i64 %r }
IR
cat > fvec.ll <<'IR'
define <2 x i32> @vadd(<2 x i32> %a, <2 x i32> %b) { %r = add <2 x i32> %a, %b  ret <2 x i32> %r }
IR
cat > byval.ll <<'IR'
%struct.S = type { [64 x i8] }
declare void @callee(ptr byval(%struct.S))
define void @caller(ptr %p) { call void @callee(ptr byval(%struct.S) %p)  ret void }
IR
cat > tls.ll <<'IR'
@t = thread_local global i32 0, align 4
define i32 @rd() { %v = load i32, ptr @t  ret i32 %v }
IR
cat > diff.ll <<'IR'
define i64 @mulc(i64 %a) { %r = mul i64 %a, 100  ret i64 %r }
define i64 @divc(i64 %a) { %r = sdiv i64 %a, 7  ret i64 %r }
IR
cat > mt.ll <<'IR'
declare i64 @h(i64)
define i64 @mt(i64 %a) { %r = musttail call i64 @h(i64 %a)  ret i64 %r }
IR
```

**A. 能跑的函数：三个 abort 值输出相同，但管线不同。** `add` 在 `0/1/2` 下都产出 `add a0, a0, a1; ret`。差别只在“失败时有没有后路”：

```text
$T/bin/llc -mtriple=riscv64 -debug-pass=Structure -O1 -global-isel \
    -global-isel-abort=2 -verify-machineinstrs=0 simple.ll -o /dev/null 2>&1 | grep -E 'DAG->DAG|ResetMachine'
      ResetMachineFunction
      RISC-V DAG->DAG Pattern Instruction Selection
# abort=1 时同一命令只剩 ResetMachineFunction，没有 DAG 选指
```

**B. 不支持：定长向量。** `G_ADD <2 x s32>` 在 Legalizer 找不到规则：

```text
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 fvec.ll -o /dev/null
LLVM ERROR: unable to legalize instruction: %8:_(<2 x s32>) = G_ADD %0:_, %1:_ (in function: vadd)
```

换 `abort=2`，得到可 grep 的诊断，且退出码为 0：

```text
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=2 -pass-remarks-missed='gisel*' fvec.ll -o /dev/null
remark: <unknown>:0:0: unable to legalize instruction: %8:_(<2 x s32>) = G_ADD %0:_, %1:_ (in function: vadd)
warning: Instruction selection used fallback path for vadd
```

换 `abort=0`，stderr **0 字节**，汇编与 SDAG 逐字节相同（`diff` 无输出）：`addw a0,a0,a2` / `addw a1,a1,a3` / `ret`。这就是“静默 fallback”最危险的地方：你以为在测 GISel，其实测的是 SDAG。

**C. 不支持：byval 参数。** 失败点更早，在 IRTranslator：`remark: ... unable to translate instruction: call: '  call void @callee(ptr byval(%struct.S) %p)' (in function: caller)`，随后是 `fallback path for caller`。`abort=0` 下同样与 SDAG 输出完全一致。仓库里的 `riscv-unsupported.ll` 把这条、以及两条 i128 inline asm 用例固定成了回归测试。

**D. 不支持：线程局部存储。** `@t = thread_local global i32 0` 的 `G_GLOBAL_VALUE` 在选指阶段直接失败：

```text
LLVM ERROR: cannot select: %1:gpr(p0) = G_GLOBAL_VALUE @t (in function: rd)
```

原因在 `RISCVInstructionSelector.cpp:1482`：`if (GV->isThreadLocal()) { // TODO: implement this case. return false; }`。`abort=0` 时汇编与 SDAG 相同（`auipc %tls_ie_pcrel_hi` + `ld` + `add tp`）。

**E. 不支持：strict FP 与异常处理。** 这两类连 `G_*` 规则都没有：

```text
LLVM ERROR: unable to legalize instruction: %4:_(s32) = G_STRICT_FADD %0:_, %1:_ (in function: cf)
LLVM ERROR: unable to legalize instruction: G_INVOKE_REGION_START (in function: f)
```

`grep -c G_STRICT llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp` = 0，是“整类未实现”，不是某个宽度没覆盖。

**F. 同一 IR 的 SDAG / GISel 差异（不 fallback，两者都成功）。** 没有 `+m` 时：

```text
# SDAG：mul i64 %a, 100 —— DAGCombine 展开成移位/加减
	slli	a1, a0, 5
	slli	a2, a0, 2
	sub	a2, a2, a1
	slli	a0, a0, 7
	add	a0, a2, a0

# GISel：同一个函数 —— Legalizer 选了 libcall
	li	a1, 100
	call	__muldi3
```

除法同理：SDAG 输出 `tail __divdi3`，GISel 输出 `call __divdi3` + 保存/恢复 `ra`。原因分别是 23.4 的 `MUL -> libcallFor` 和 23.3 的 `Info.IsTailCall = false`（23.11 E6 可复跑）。

**G. `musttail` 被静默降级（最值得写成 issue 的一条，23.11 E7）。**

```llvm
define i64 @mt(i64 %a) {
  %r = musttail call i64 @h(i64 %a)
  ret i64 %r
}
```

SDAG 输出 `tail h`；GISel 输出 `call h`，并且**不报错、不 fallback、不警告**（`abort=1` 也照样成功）。`musttail` 的契约是“必须尾调用”，这不是代码质量问题，是语义问题。

---

## 23.8 测试与 CI 现状

`llvm/test/CodeGen/RISCV/GlobalISel/` 的组织是“按阶段分目录，按指令族分文件”：

| 目录 | 文件数 | 典型 RUN 行 | 测什么 |
|---|---|---|---|
| 根目录 | 102 | `-global-isel -global-isel-abort=1` | 端到端 `.ll`：算术、调用、原子、RVV |
| `irtranslator/` | 33 | `-global-isel -stop-after=ir-translator` | 只测 IR→gMIR，含 ABI 各种组合 |
| `legalizer/` | 151 | `-run-pass=legalizer`（MIR） | 单条 `G_*` 的合法化结果 |
| `regbankselect/` | 50 | `-run-pass=reg-bank-select`（MIR） | bank 分配 |
| `instruction-select/` | 84 | `-run-pass=instruction-select -simplify-mir` | 生成匹配器与手写钩子 |
| `rvv/` 及三个 `*/rvv/` | 23 + 28 + 16 + 13 | `-global-isel -mattr=+v,...` | 可伸缩向量 |

几个容易被误读的统计：

- `find ... -type f | wc -l` = **500**，其中 `.ll` 154、`.mir` 346。
- `grep -rl 'global-isel\|GlobalISel' llvm/test/CodeGen/RISCV` = **162**。其中 158 个在 `GlobalISel/` 目录内，4 个在目录外（`indirectbr.ll`、`allow-check.ll`、`nomerge.ll`、`llvm.sincos.ll`）。
- 为什么 500 个测试只有 162 个提到 `global-isel`？因为 346 个 `.mir` 用 `-run-pass=<pass>` 直接跑机器 pass，MIR 里的 `legalized:`/`regBankSelected:` 标志就是输入契约，不需要 `-global-isel`。
- 显式写 abort 的 `.ll` 只有 19 个（按取值计 `=0` 1 个、`=1` 14 个、`=2` 5 个；`fp128.ll` 同时用了 1 和 2），其余 135 个依赖“默认 = 1”；`.mir` 中另有 4 个显式 `=0`。这意味着**任何新增 fallback 都会让测试直接失败**，这是好事。

`gisel-commandline-option.ll` 是管线契约测试，值得单独读：

- 不传 `-global-isel`：`DISABLED-NOT: IRTranslator`，并检查 `RISC-V DAG->DAG Pattern Instruction Selection`。
- 传 `-global-isel`（`llc` 默认 abort=1）：检查 `IRTranslator → RISCVPreLegalizerCombiner → Legalizer → RISCVPostLegalizerCombiner → RegBankSelect → InstructionSelect`，且 `NOFALLBACK-NOT` 断言没有 DAG 选指。
- 传 `-global-isel-abort=2`：同一个空函数也允许 `FALLBACK`，因为 DAG 选指 pass 被排进了管线（不是因为它失败了）。

实测：`llvm-lit -sv -j16 llvm/test/CodeGen/RISCV/GlobalISel/` → **500/500 passed，2.37s**。

为什么“没跑 GISel 也不影响默认 CI”：默认 codegen 测试不传 `-global-isel`，走 `addInstSelector()` 的 SDAG 路径；GISel 测试全部显式 opt-in。但共享层（`RISCVInstrInfo*.td`、ABI、寄存器类）的改动仍会同时影响两边，所以“默认 CI 绿”不等于“GISel 没坏”。

---

## 23.9 可贡献缺口清单（按难度 × 影响排序）

入口都以本树行号为准；验证方式尽量复用现有测试，避免“新写一个没人跑的 case”。

| # | 缺口 | 难度 | 影响 | 从哪入手 | 用什么验证 |
|---|---|---|---|---|---|
| 1 | byval 参数/调用 | 中 | 高（C 结构体传参常见） | `RISCVCallLowering.cpp:596`，`lowerCall` 拒绝；需要栈拷贝 + 指针传递 | 从 `riscv-unsupported.ll` 移出该函数，另加正向 `byval.ll` |
| 2 | i128 多寄存器 inline asm | 中 | 高（内核/原子库） | `RISCVInlineAsmLowering.cpp` + `lowerCall` 的多寄存器约束 | 同上文件的后两个函数 |
| 3 | RVV tuple 类型作参数/返回值 | 高 | 中（segment load/store intrinsic） | `isSupportedArgumentType` 320 / `isSupportedReturnType` 345 | `GlobalISel/rvv/fallback-rv{32,64}.ll` |
| 4 | TLS `G_GLOBAL_VALUE` | 中 | 高（`thread_local` 很常见） | `RISCVInstructionSelector.cpp:1482`，仿 `selectAddr` 加 TLS 分支 | 新增 `GlobalISel/tls.ll`（23.11 E5 的样本） |
| 5 | 定长向量合法化 | 高 | 高（`<2 x i32>` 这类 ABI 边界） | `RISCVLegalizerInfo.cpp:141` 的 `G_ADD` builder；需要 fixed→scalable 或标量拆分策略 | 新增 `legalizer/legalize-fixed-vector-*.mir` + 端到端 `.ll` |
| 6 | 尾调用 / `musttail` | 高 | 高（语义正确性） | `RISCVCallLowering.cpp:615-616`，需要 `PseudoTAIL` 与栈复用 | 新增 `GlobalISel/musttail.ll`，检查 `tail h` 且 `abort=1` 不报错 |
| 7 | 无 Zmmul 时 `G_MUL` 的质量 | 低 | 中（代码质量，非正确性） | `RISCVLegalizerInfo.cpp:519-522`，把常量乘改成 `lower` 或加 combine | `GlobalISel/` 新增 `.ll`，对照 SDAG 的移位序列 |

难度判断标准：低 = 只改一条 builder/一个 `case`；中 = 需要新的 lowering 路径但接口已存在；高 = 涉及 ABI/栈布局/新伪指令或多阶段协作。

一个“别当低垂果实”的缺口：`G_CLMUL` 在无 Zbkc 时是 `.unsupported()`（237-239），但 `llvm.clmul` 是稳定 IR 入口、SDAG 会内联展开，实测 `abort=1` 直接报 `unable to legalize`。要做就得自己实现展开（LegalizerHelper 没有现成的 `G_CLMUL` lowering）。另一个 `G_ATOMICRMW_MAX/MIN` 的 `.unsupported()`（865-868）在 `llc` 层通常到不了，`AtomicExpandPass` 会先改成 cmpxchg 循环。

---

## 23.10 源码落点

| 内容 | 路径 | 关键行/符号 |
|---|---|---|
| 五个阶段接线 | `llvm/lib/Target/RISCV/RISCVSubtarget.cpp` | `getInlineAsmLowering` 120；`getCallLowering` 127；`getInstructionSelector` 133；`getLegalizerInfo` 142；`getRegBankInfo` 148 |
| pass 顺序 | `llvm/lib/Target/RISCV/RISCVTargetMachine.cpp` | `addInstSelector` 449；GISel 六个钩子 455-488 |
| 调用 lowering / 类型闸门 | `llvm/lib/Target/RISCV/GISel/RISCVCallLowering.cpp` | `lowerReturn` 371；`canLowerReturn` 416；`saveVarArgRegisters` 429；`lowerFormalArguments` 526；`lowerCall` 585；byval 596；tail 615；`isSupportedArgumentType` 320；`isSupportedReturnType` 345 |
| inline asm | `llvm/lib/Target/RISCV/GISel/RISCVInlineAsmLowering.cpp` | `lowerAsmOperandForConstraint` 26（`I/J/K/S`） |
| 合法化 | `llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp` | builder 141 起；`G_MUL` 506-528；`G_SDIV` 536-553；`legalizeCustom` 1707；`legalizeReadCounter` 1012 |
| bank / 定义 | `RISCVRegisterBankInfo.cpp`、`RISCVRegisterBanks.td` | partial mappings 29-41；`getInstrMapping` 243；`getVRBValueMapping` 225；三个 `RegisterBank` |
| LLT→寄存器类 | `llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp` | `getRegClassForTypeOnBank` 132 |
| 选指 | `llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp` | `select` 1304；`preISelLower` 1701；TLS 1482；`selectAddr` 2011；`selectSelect` 2125；`selectFPCompare` 2206 |
| combiner 规则 | `llvm/lib/Target/RISCV/RISCVCombine.td` | `RISCVPreLegalizerCombiner` 14；`RISCVO0PreLegalizerCombiner` 18；`fold_fp_zero_store` 24；post 组 32-37 |
| 生成匹配器 | `$T/lib/Target/RISCV/RISCVGenGlobalISel.inc` | `selectImpl` 1674；`getMatchTable` 1699；45,550,453 字节 |
| 契约测试 | `riscv-unsupported.ll`、`gisel-commandline-option.ll` | byval + 两条 i128 asm；`ENABLED`/`NOFALLBACK`/`FALLBACK` |

---

## 23.11 可跑实验

全部命令在 `/tmp/ch23` 下可复现；`$T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

**E1 看三个阶段**（预期：`_` → `gprb` → `gpr`，`G_ADD` → `ADD`）：

```bash
$T/bin/llc -mtriple=riscv64 -global-isel -stop-after=ir-translator -simplify-mir simple.ll -o - | sed -n '/^body:/,$p'
$T/bin/llc -mtriple=riscv64 -global-isel -stop-before=instruction-select -simplify-mir simple.ll -o - | sed -n '/^body:/,$p'
$T/bin/llc -mtriple=riscv64 -global-isel -stop-after=instruction-select -simplify-mir simple.ll -o - | sed -n '/^body:/,$p'
```

**E2 三个 abort 值**（预期：输出相同；只有 abort=2 的管线含 DAG 选指）：

```bash
for v in 0 1 2; do $T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=$v simple.ll -o - | tail -6; done
$T/bin/llc -mtriple=riscv64 -debug-pass=Structure -O1 -global-isel -global-isel-abort=2 -verify-machineinstrs=0 simple.ll -o /dev/null 2>&1 | grep 'DAG->DAG'
```

**E3 定长向量 fallback**（预期：abort=1 报错；abort=2 有 remark + warning；abort=0 与 SDAG `diff` 为空）：

```bash
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 fvec.ll -o /dev/null
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=2 -pass-remarks-missed='gisel*' fvec.ll -o /dev/null
$T/bin/llc -mtriple=riscv64 fvec.ll -o - > sdag.s
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=0 fvec.ll -o - > gisel.s
diff sdag.s gisel.s && echo IDENTICAL
```

**E4 byval 在 IRTranslator 失败**（预期：`unable to translate instruction: call`）：

```bash
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=2 -pass-remarks-missed='gisel*' byval.ll -o /dev/null
```

**E5 TLS 在选指阶段失败**（预期：`cannot select: G_GLOBAL_VALUE @t`；abort=0 与 SDAG 相同）：

```bash
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 tls.ll -o /dev/null
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=0 tls.ll -o - | sed -n '/rd:/,/End function/p'
```

**E6 SDAG vs GISel 的质量差**（预期：`mulc` 是 `__muldi3` vs 移位序列；`divc` 的 `tail`/`call` 差异见 23.7 F）：

```bash
$T/bin/llc -mtriple=riscv64 diff.ll -o - | sed -n '/^mulc:/,/End function/p'
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 diff.ll -o - | sed -n '/^mulc:/,/End function/p'
```

**E7 `musttail` 被降级**（预期：SDAG `tail h`，GISel `call h`，且 abort=1 不报错）：

```bash
$T/bin/llc -mtriple=riscv64 mt.ll -o - | grep -E 'tail|call'
$T/bin/llc -mtriple=riscv64 -global-isel -global-isel-abort=1 mt.ll -o - | grep -E 'tail|call'
```

**E8 测试套件与管线**（预期：500/500；O0 用 O0 combiner，O2 用 pre+post）：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk && $T/bin/llvm-lit -sv -j16 llvm/test/CodeGen/RISCV/GlobalISel/
for O in 0 2; do $T/bin/llc -mtriple=riscv64 -O$O -global-isel -debug-pass=Structure -verify-machineinstrs=0 simple.ll -o /dev/null 2>&1 | grep -E 'Combiner|Legalizer'; done
```

---

## 23.12 踩雷集锦

1. **`-global-isel` 不等于“自动 fallback”。** 在 `llc` 里省略 `-global-isel-abort` 时默认是 `Enable`（1），不支持就 `LLVM ERROR`。教学脚本里要写 `=0` 或 `=2`，否则读者第一次跑就被“GISel 不能用了”误导。
2. **`-global-isel-abort=2` 不是“更严格”。** 它是“回退 + 诊断”；只有 `=1` 会中止。想用 CI 抓“意外 fallback”，应当用 `=1` 做主体、用 `=2` + `-pass-remarks-missed='gisel*'` 专门测试已知 fallback。
3. **静默 fallback 会让你测错后端。** `abort=0` 下失败函数交给 SDAG，汇编可以逐字节相同。判断依据是 `-debug-pass=Structure` 里有没有 `RISC-V DAG->DAG`、stderr 有没有 remark/warning，不是“能编译”。
4. **MIR 测试不写 `-global-isel`，新 `.ll` 测试必须写。** 346 个 `.mir` 用 `-run-pass=legalizer` 等直接跑，`legalized:`/`regBankSelected:` 才是开关；默认管线是 SDAG，`.ll` 忘了写就永远绿，但根本没测 GISel。
5. **ABI 有两份实现。** `CC_RISCV` + `LowerFormalArguments` 与 `RISCVCallLowering` + `markSExt32Registers` 必须同步；只改一边，轻则少删 `sext.w`，重则参数扩展错误。
6. **`-O0 -global-isel` 不是“关掉 GISel”。** 它仍然走 GISel，只是 pre-legalizer combiner 换成 `RISCVO0PreLegalizerCombiner`（`optnone_combines`）。RISC-V 没有 FastISel，所以 `-O0` 的 SDAG 路径也不会被 FastISel 截走。
7. **向量尺寸不合法是崩溃，不是 fallback。** `getVRBValueMapping` 对 `>64` 且不是 128/256/512 的尺寸直接 `report_fatal_error`（`<=64` 一律映射 VRB64）；写 RVV 测试时先确认 LLT 尺寸，别把崩溃当“不支持”。

---

## 23.13 本章重点整理

- 默认后端是 SDAG；GISel 是 opt-in。`llc -global-isel` 默认 abort=1，**不自动回退**；回退要 `=0`（静默）或 `=2`（带诊断）。
- 真实 pass 顺序是 `IRTranslator → PreLegalizerCombiner → Legalizer → PostLegalizerCombiner → RegBankSelect → InstructionSelect`；`PostLegalizerCombiner` 不在最后。
- 五阶段的完成度不均衡：Legalizer 与 InstructionSelect 覆盖最广，CallLowering 的边界最硬（byval、i128 asm、RVV tuple），Combiner 规则最少（1 条 RISC-V 专属）。
- `RISCVCallLowering` 用 `isSupportedArgumentType`/`isSupportedReturnType` 返回 false 表达不支持，没有 `getUnsupportedArgTypes`；尾调用被无条件关闭（`Info.IsTailCall = false`），`musttail` 因此被静默降级。
- Legalizer 的 builder 是“谓词 → 动作 → 类型变换”的链；`clampScalar`/`widenScalarToNextPow2`/`scalarSameSizeAs` 是主力 API，本文件没有 `lowerForFP`。
- 三个 bank 是 GPRB/FPRB/VRB；向量复杂度来自 LMUL 只能用 64/128/256/512 表达，以及 RVV intrinsic 的混合操作数。`selectImpl` 是 45 MB 生成匹配器，手写钩子有 11 个。
- 缺口优先看 `riscv-unsupported.ll` 和 `rvv/fallback-*.ll`：文件变短就是进度。

---

## 23.14 自我检核

- [ ] 能说出 `-global-isel` 不带 abort 参数时的默认值，并解释为什么“GISel 会自动 fallback”是错的
- [ ] 能画出真实 pass 顺序，并指出 `PostLegalizerCombiner` 在 `RegBankSelect` 之前
- [ ] 能对 `mul i64`、`select`、`ctlz` 各说出 SDAG 与 GISel 的动作差异及对应行号
- [ ] 能说出三个 bank 的名字、代表寄存器类，以及 `getRegClassForTypeOnBank` 如何把 LLT 变成 `VRM2/VRM4/VRM8`
- [ ] 能列举至少三个手写 `select*` 钩子并说明各自替代的 SDAG pattern
- [ ] 能解释为什么 `musttail` 在 GISel 下是语义缺口而不是优化缺口
- [ ] 能从 `riscv-unsupported.ll` 挑一个用例，写出“改哪个文件、加哪个测试”的最小计划

---

## 23.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVCallLowering.cpp`**：320-367（类型闸门）、526-583（入参）、585-671（调用）；ABI 与 fallback 的第一现场。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp`**：141-350（标量主力规则）、506-600（乘除与 libcall）、849-890（atomic/fence/prefetch）、1707-1925（`legalizeCustom` 全表）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVRegisterBankInfo.cpp`**：29-107（mapping 表）、225-241（向量尺寸）、243-636（`getInstrMapping` 全分支）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp`**：1304-1596（`select` 主分派）、1701-1800（`preISelLower`）、2011-2206（地址/select/fcmp 三个钩子）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/GlobalISel/riscv-unsupported.ll`**、**`gisel-commandline-option.ll`**、**`rvv/fallback-rv64.ll`**：三个“现状契约”测试，改 GISel 前先跑它们。

---

→ 下一章：[练习 D — 用 MIR 驱动机器 pass](./practice-d-machine-passes.md)
