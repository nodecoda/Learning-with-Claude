# 第 8 章 谓词系统 II：subtarget feature、内存/原子与 fold 安全性

> **本章目标**：把 matcher 的守卫条件从“单个操作数”扩展到三类跨操作数/跨指令约束：subtarget feature 位集、内存操作数（MMO）的 size/alignment/address space/ordering，以及 use-def 图上的 fold 安全性。读完后你应该能解释 `+fullfp16` 为什么改变 `half fadd` 的选择、普通 `ldr` pattern 为什么不能吞掉 atomic load，以及“匹配到定义者”为什么不等于“可以删掉它”。
>
> **前置**：第 5–7 章。**环境**：LLVM 23.1.4，AArch64；本章汇编均来自实际 `llc` 运行。

---
## 8.1 为什么类型、bank、立即数还不够

第 7 章的三类谓词只回答“这个操作数长什么样”：LLT 是不是 `s64`、bank 是不是 GPR、立即数是否可编码。它们回答不了另外三个问题：

1. **当前 subtarget 支不支持这条指令？** 同一份 AArch64 pattern 表同时服务 ARMv8.0 和 ARMv8.2，`FADDHrr` 只在 `+fullfp16` 时可用。
2. **这个 `G_LOAD`/`G_STORE` 访问多大、对齐多少、地址空间是什么、是不是 atomic？** 这些不在普通操作数列表里，而在 `MachineMemOperand` 里。
3. **把某个定义者折进根指令会不会改变语义？** 它有没有副作用、有没有别的 use、能不能跨基本块移动？

```text
subtarget ──▶ rule-level feature bits
operands  ──▶ 类型 / bank / 立即数谓词
MMO       ──▶ size / align / addrspace / ordering
use-def   ──▶ fold 安全性 / use 数
                         │
                         ▼
                 render 或 reject
```

| 约束 | 检查对象 | 典型指令 |
| --- | --- | --- |
| subtarget feature | 函数/模块的 feature 位集 | `GIM_Try_CheckFeatures` |
| 内存语义 | `MachineMemOperand` | `GIM_CheckMemory*` |
| 原子顺序 | `MMO->getMergedOrdering()` | `GIM_CheckAtomicOrdering*` |
| 折叠安全性 | 被记录指令的副作用/CFG 位置 | `GIM_CheckIsSafeToFold` |
| 使用次数 | 第一个结果的 use 数 | `GIM_CheckHasOneUse` / `HasNoUse` |

失败仍走第 6 章的 `handleReject()`：谓词不匹配只是“这条规则不适用”，不是编译错误。

---
## 8.2 Feature 谓词：一张全局位图

### 8.2.1 从 `Predicate` 到 `PredicateBitset`

AArch64 的 `HasFullFP16` 定义在 `AArch64InstrInfo.td`：

```tablegen
def HasFullFP16 : Predicate<"Subtarget->hasFullFP16()">,
                  AssemblerPredicateWithAll<(all_of FeatureFullFP16), "fullfp16">;
def HasNoFullFP16 : Predicate<"!Subtarget->hasFullFP16()">;
```

`Predicate` 的 `CondString` 是一段 C++ 条件。GlobalISel importer 把它登记成 feature，而不是改写成操作数谓词：

```cpp
for (const Record *Pred : Predicates) {
  if (Pred->getValueAsString("CondString").empty())
    continue;
  declareSubtargetFeature(Pred);
  M.addRequiredFeature(Pred);
}
```

`HasNoFullFP16` 也有自己的一位：它不是“把 `HasFullFP16` 取反”，而是另一条 `CondString` 算出的独立 bit。

位集大小由 `emitPredicateBitset` 决定：

```cpp
unsigned Size = SubtargetFeatures.size() + HwModes.size();
OS << "const unsigned MAX_SUBTARGET_PREDICATES = " << Size << ";\n"
   << "using PredicateBitset = "
      "llvm::Bitset<MAX_SUBTARGET_PREDICATES>;\n";
```

位集里不只有 feature，还有 HwMode；`FeatureBitsets[]` 会为每条规则记录所需位，HwMode 用 `Feature_HwMode<N>Bit` 表示。

### 8.2.2 运行时如何得到可用 feature

生成的 selector 有两个位集：

```cpp
PredicateBitset AvailableModuleFeatures;
mutable PredicateBitset AvailableFunctionFeatures;
PredicateBitset getAvailableFeatures() const {
  return AvailableModuleFeatures | AvailableFunctionFeatures;
}
```

`AvailableModuleFeatures` 在构造时计算，`AvailableFunctionFeatures` 在 `setupGeneratedPerFunctionState(MF)` 中按函数重算；`getAvailableFeatures()` 返回两者的并集。

`GIM_Try_CheckFeatures` 做子集判断：

```cpp
if ((AvailableFeatures & ExecInfo.FeatureBitsets[ExpectedBitsetID]) !=
    ExecInfo.FeatureBitsets[ExpectedBitsetID]) {
  CurrentIdx = OnFail;
} else {
  OnFailResumeAt.push_back(OnFail);
}
```

规则要求的每一位都必须在可用集合里；多出来的 feature 不影响匹配，缺任意一位就 reject。实现还做了优化：feature 不匹配时直接跳到 `OnFail`，不压回溯栈；只有通过才压栈。一条规则要求多个 feature 时是合取关系，不是“有任意一个就行”。

### 8.2.3 实验：`half fadd` 的两条路

```llvm
define half @faddh(half %a, half %b) {
  %r = fadd half %a, %b
  ret half %r
}
```

默认跑 InstructionSelect：

```console
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 \
    -stop-after=instruction-select faddh.ll -o -
```

实际 MIR（只摘指令行）：

```mir
%3:fpr32 = nofpexcept FCVTSHr %0, implicit $fpcr
%4:fpr32 = nofpexcept FCVTSHr %1, implicit $fpcr
%5:fpr32 = nofpexcept FADDSrr %3, %4, implicit $fpcr
%2:fpr16 = nofpexcept FCVTHSr %5, implicit $fpcr
```

最终汇编：

```asm
fcvt s0, h0
fcvt s1, h1
fadd s0, s0, s1
fcvt h0, s0
```

加 `-mattr=+fullfp16` 后，MIR 变成：

```mir
%2:fpr16 = nofpexcept FADDHrr %0, %1, implicit $fpcr
```

汇编变成：

```asm
fadd h0, h0, h1
```

这不是 Legalizer 改了类型，而是 `FADDHrr` 的 pattern 带 `HasFullFP16`（`TwoOperandFPData` 的 `Hrr` 变体设置了 `let Predicates = [HasFullFP16];`）。没有该 feature 时，`GIM_Try_CheckFeatures` 在规则入口 reject，selector 只能选 half→float→half 的替代规则。`-global-isel-abort=1` 保证这里没有悄悄 fallback 到 SelectionDAG。

> MIR 里的 `FCVTSHr`、`FADDSrr`、`FCVTHSr` 是机器指令 opcode；汇编里显示为 `fcvt`、`fadd`。

---
## 8.3 内存谓词：MMO 是第二套操作数

### 8.3.1 MMO 不在普通操作数列表里

`MI.memoperands()` 保存 `MachineMemOperand`，它不是 `getOperand(OpIdx)` 的普通操作数。`GIM_CheckMemory*` 的 `MMOIdx` 是**第几个内存操作数**（从 0 开始），不是机器操作数下标。带 `LLT` 的变体多一个 `OpIdx`，那才是普通操作数下标。

| 谓词 | 参数 | 检查 |
| --- | --- | --- |
| `GIM_CheckMemorySizeEqualTo` | `InsnID, MMOIdx, Size(4)` | `MMO->getSize() == Size`，字节 |
| `GIM_CheckMemoryAddressSpace` | `InsnID, MMOIdx, NumAddrSpace, ...` | 地址空间在允许列表里 |
| `GIM_CheckMemoryAlignment` | `InsnID, MMOIdx, MinAlign(1)` | `MMO->getAlign() >= MinAlign` |
| `GIM_CheckMemorySizeEqualToLLT` | `InsnID, MMOIdx, OpIdx` | `MMO 位宽 == LLT 位宽` |
| `GIM_CheckMemorySizeLessThanLLT` | 同上 | `MMO 位宽 < LLT 位宽` |
| `GIM_CheckMemorySizeGreaterThanLLT` | 同上 | `MMO 位宽 > LLT 位宽` |

`GIM_CheckMemorySizeEqualTo` 的核心：

```cpp
MachineMemOperand *MMO =
    *(State.MIs[InsnID]->memoperands_begin() + MMOIdx);
if (MMO->getSize() != Size)
  if (handleReject() == RejectAndGiveUp)
    return false;
```

`*LLT` 版本先要求 `OpIdx` 是寄存器：

```cpp
MachineOperand &MO = State.MIs[InsnID]->getOperand(OpIdx);
if (!MO.isReg()) { /* reject */ }
MachineMemOperand *MMO =
    *(State.MIs[InsnID]->memoperands_begin() + MMOIdx);
const TypeSize Size = MRI.getType(MO.getReg()).getSizeInBits();
if (MatcherOpcode == GIM_CheckMemorySizeEqualToLLT &&
    MMO->getSizeInBits() != Size) { /* reject */ }
```

`LessThanLLT` 在 `MMO >= LLT` 时 reject，`GreaterThanLLT` 在 `MMO <= LLT` 时 reject，都是严格不等式。`i1` 这类 `MemVT` 在 importer 里先 `alignTo(sizeInBits, 8)` 再除以 8，所以按 1 字节处理。

对齐是最低要求，不是精确值：

```cpp
if (MMO->getAlign() < MinAlign && handleReject() == RejectAndGiveUp)
  return false;
```

`MinAlign` 按字节编码，不是 log2；`MinAlign=4` 表示至少 4 字节对齐，16 字节对齐也能通过。地址空间谓词接受列表，任一匹配即可；越界 `MMOIdx` 直接 reject。

### 8.3.2 `GINodeEquiv` 的 atomic 标志

`G_LOAD`/`G_STORE` 同时承载普通和原子访问，而 SelectionDAG 有独立的 `ISD::LOAD`、`ISD::ATOMIC_LOAD`。`SelectionDAGCompat.td` 用字段区分：

```tablegen
bit CheckMMOIsNonAtomic = false;
bit CheckMMOIsAtomic = false;

def : GINodeEquiv<G_LOAD, ld> {
  let CheckMMOIsNonAtomic = true;
  let IfSignExtend = G_SEXTLOAD;
  let IfZeroExtend = G_ZEXTLOAD;
}
def : GINodeEquiv<G_LOAD, atomic_load> {
  let CheckMMOIsNonAtomic = false;
  let CheckMMOIsAtomic = true;
}
```

`GlobalISelEmitter` 在 opcode 检查后立刻把它们变成原子性谓词：

```cpp
if (SrcGIEquivOrNull->getValueAsBit("CheckMMOIsNonAtomic"))
  InsnMatcher.addPredicate<AtomicOrderingMMOPredicateMatcher>("NotAtomic");
else if (SrcGIEquivOrNull->getValueAsBit("CheckMMOIsAtomic"))
  InsnMatcher.addPredicate<AtomicOrderingMMOPredicateMatcher>(
      "Unordered", AtomicOrderingMMOPredicateMatcher::AO_OrStronger);
```

普通 `ldr` pattern 不是“碰巧没匹配到 atomic”：opcode 检查之后，它还明确要求 MMO 是 `NotAtomic`。原子 load 的 ordering 不是 `NotAtomic`，自然被拒绝。

---

## 8.4 原子谓词：ordering 是第三个维度

原子访问即使 size、align、addrspace 相同，ordering 不同也不能混用。executor 提供三种检查：

```cpp
case GIM_CheckAtomicOrdering: {
  auto Ordering = (AtomicOrdering)MatchTable[CurrentIdx++];
  if (!State.MIs[InsnID]->hasOneMemOperand())
    /* reject */;
  for (const auto &MMO : State.MIs[InsnID]->memoperands())
    if (MMO->getMergedOrdering() != Ordering)
      /* reject */;
  break;
}
```

另外两种只改比较方式：

```cpp
// MMO ordering 至少和 Ordering 一样强
if (!isAtLeastOrStrongerThan(MMO->getMergedOrdering(), Ordering))
  /* reject */;

// Ordering 比 MMO ordering 更强，即 MMO 严格更弱
if (!isStrongerThan(Ordering, MMO->getMergedOrdering()))
  /* reject */;
```

三个细节：

1. **必须恰好一个 MMO**。`hasOneMemOperand()` 不成立时直接 reject。
2. **比较的是 `getMergedOrdering()`**。有多个 MMO 时，任何一个不满足都 reject。
3. **Acquire 与 Release 不可比**。LLVM 的格是 `NotAtomic < Unordered < Monotonic`，然后 Acquire 与 Release 分别汇入 AcquireRelease，最后是 SequentiallyConsistent。所以 `OrStrongerThan(Acquire)` 不接受 Release，`WeakerThan(Acquire)` 也不接受 Release。

### 8.4.1 实验：普通访问与原子访问

```llvm
define i32 @plain_load(ptr %p) {
  %v = load i32, ptr %p, align 4
  ret i32 %v
}
define i32 @acq_load(ptr %p) {
  %v = load atomic i32, ptr %p acquire, align 4
  ret i32 %v
}
define void @plain_store(ptr %p, i32 %v) {
  store i32 %v, ptr %p, align 4
  ret void
}
define void @rel_store(ptr %p, i32 %v) {
  store atomic i32 %v, ptr %p release, align 4
  ret void
}
```

```console
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 memory.ll -o -
```

实际汇编：

```asm
plain_load:   ldr   w0, [x0]
acq_load:     ldar  w0, [x0]
plain_store:  str   w1, [x0]
rel_store:    stlr  w1, [x0]
```

InstructionSelect 后的 MIR 也能看到 MMO ordering：

```mir
%1:gpr32 = LDRWui %0, 0 :: (load (i32) from %ir.p)
%1:gpr32 = LDARW %0 :: (load acquire (i32) from %ir.p)
STRWui %1, %0, 0 :: (store (i32) into %ir.p)
STLRW %1, %0 :: (store release (i32) into %ir.p)
```

`LDRWui` 与 `LDARW` 的地址寄存器、结果类型、访问大小都一样；把它们分开的是 MMO ordering 以及规则里的 `GIM_CheckAtomicOrdering` / `CheckMMOIsNonAtomic`。如果普通 pattern 没有这层 MMO 检查，只看 opcode 和 LLT，就会把 acquire load 选成普通 load，直接丢语义。

---

## 8.5 Fold 安全性：匹配到定义者 ≠ 可以删掉它

### 8.5.1 executor 只做“显然安全”的检查

`GIM_CheckIsSafeToFold` 表示把 `MIs[1]` 到 `MIs[NumInsn]` 这些被记录的定义者折进根 `MIs[0]`：

```cpp
case GIM_CheckIsSafeToFold: {
  uint64_t NumInsn = MatchTable[CurrentIdx++];
  MachineInstr &Root = *State.MIs[0];
  for (unsigned K = 1, E = NumInsn + 1; K < E; ++K) {
    if (!isObviouslySafeToFold(*State.MIs[K], Root)) {
      if (handleReject() == RejectAndGiveUp)
        return false;
    }
  }
  break;
}
```

`isObviouslySafeToFold` 的策略：

```cpp
// 紧邻 root 的定义者：没有移动，直接安全
if (MI.getParent() == IntoMI.getParent() &&
    std::next(MI.getIterator()) == IntoMI.getIterator())
  return true;
// convergent 指令不能跨基本块移动
if (MI.isConvergent() && MI.getParent() != IntoMI.getParent())
  return false;
if (MI.isLoadFoldBarrier())
  return false;

// 同块 load：不能 atomic/volatile，中间不能有 barrier，最多扫描 20 条
if (MI.mayLoad() && MI.getParent() == IntoMI.getParent()) {
  if (MI.memoperands_empty())
    return false;
  auto &MMO = **(MI.memoperands_begin());
  if (MMO.isAtomic() || MMO.isVolatile())
    return false;
  unsigned Iter = 0;
  for (auto &CurrMI :
       instructionsWithoutDebug(MI.getIterator(), IntoMI.getIterator())) {
    if (CurrMI.isLoadFoldBarrier())
      return false;
    if (Iter++ == 20)
      return false;
  }
  return true;
}
return !MI.mayLoad();
```

| 情况 | 结果 | 原因 |
| --- | --- | --- |
| 紧挨在 root 前面 | 安全 | 没有真正移动 |
| 跨块的 convergent 指令 | 不安全 | 可能改变执行次数/位置 |
| `MI.isLoadFoldBarrier()` | 不安全 | 目标禁止跨过它折叠 |
| 同块 atomic/volatile load | 不安全 | 有可观察的副作用/顺序 |
| 同块 load，中间有 barrier | 不安全 | 折叠会跨过 barrier |
| 同块 load，中间超过 20 条 | 不安全 | 编译期成本上限 |
| 纯计算、非 load | 默认安全 | 但仍要求 `MI` 在 root 之前 |

最后一行很重要：`isObviouslySafeToFold` 不是通用副作用分析器。它主要对 load 做保守检查；如果自定义 selector 记录的是 call、intrinsic 或其他有副作用的定义者，不能因为“不是 load”就假设安全。API 注释还要求 `MI` 必须出现在 `IntoMI` 之前；实现默认调用者按自底向上的顺序记录，不会重新做 dominance 证明。

### 8.5.2 `GIM_CheckHasOneUse` 与 `GIM_CheckHasNoUse`

有些 pattern 假设中间值只有当前这个使用者。TableGen 用 `HasOneUse` / `HasNoUse` 表达：

```tablegen
let HasOneUse = 1 in
def anyext_oneuse: PatFrag<(ops node:$src0), (anyext $src0)>;
```

importer 把它变成 `OneUsePredicateMatcher`，最终发射成：

```cpp
case GIM_CheckHasOneUse: {
  const MachineInstr *MI = State.MIs[InsnID];
  const Register Res = MI->getOperand(0).getReg();
  if (!MRI.hasOneNonDBGUse(Res)) {
    if (handleReject() == RejectAndGiveUp)
      return false;
  }
  break;
}
```

`GIM_CheckHasNoUse` 则用 `MRI.use_nodbg_empty(Res)`。两者都只看**第一个结果** `getOperand(0)`，且忽略 debug use（`DBG_VALUE` 不算）。它们统计真实 use，不是“有几个操作数”：同一条指令把同一个 vreg 用两次，就是两个 use。

debug use 不计入，意味着 `DBG_VALUE` 不会阻止 `HasNoUse`/`HasOneUse`；但删除或替换指令时，debug 信息仍要由调试指令处理逻辑维护。谓词只回答“语义上还有没有使用者”。

### 8.5.3 实验：一个常量，两个 use

```llvm
define i64 @shared_const7(i64 %x) {
  %a = add i64 %x, 7
  %b = mul i64 %a, 7
  ret i64 %b
}
```

RegBankSelect 之后、InstructionSelect 之前，只有一个 `G_CONSTANT 7`：

```mir
%0:gpr(i64) = COPY $x0
%1:gpr(i64) = G_CONSTANT i64 7
%2:gpr(i64) = G_ADD %0, %1
%3:gpr(i64) = G_MUL %2, %1
$x0 = COPY %3(i64)
```

InstructionSelect 之后：

```mir
%4:gpr32 = MOVi32imm 7
%1:gpr64 = SUBREG_TO_REG %4, %subreg.sub_32
%2:gpr64common = ADDXri %0, 7, 0
%3:gpr64 = MADDXrrr %2, %1, $xzr
```

最终汇编：

```asm
mov w8, #7
mov w9, w8
add x8, x0, #7
mul x0, x8, x9
```

这里发生了两件事：

1. `G_ADD` 把常量 7 折成 `ADDXri` 的立即数操作数；
2. `G_MUL` 仍需要真正的寄存器操作数，所以 `G_CONSTANT 7` 必须保留，最终变成 `MOVi32imm` + `SUBREG_TO_REG`。

这证明了本章的核心：**匹配到定义者，只说明它的值可以被当前 pattern 使用；不等于这个定义者可以从 MIR 里消失。** 决定它能不能消失的是“还有没有别的 use”，不是“它有没有被 `GIM_RecordInsn` 记录”。

对照只有一个 use 的版本：

```llvm
define i64 @dead_const(i64 %x) {
  %a = add i64 %x, 8
  ret i64 %a
}
```

最终汇编只有：

```asm
add x0, x0, #8
ret
```

没有 `mov` 来物化 8。这个常量在 `ADDXri` 里已经完成使命，后面没有 use，最终被清理掉。

还要注意，导入 pattern 的渲染动作只显式 erase **根**指令（`GlobalISelEmitter.cpp` 里的 `EraseInstAction` 针对 `RootInsnID`）。被记录的定义者不会因为“被匹配”就自动 erase；如果它还有 use，它必须留下。自定义 selector 如果调用 `GIR_EraseFromParent` 删除非根指令，就必须自己保证没有其他 use。

---

## 8.6 踩雷清单

1. **把 feature 检查当成精确相等**。`GIM_Try_CheckFeatures` 是子集判断；多出来的 feature 不影响，缺任意一位就 reject。`HasNoFullFP16` 也是独立的一位，不是 `HasFullFP16` 的取反。
2. **把 `MMOIdx` 当普通操作数下标**。`MMOIdx` 是 `memoperands()` 的序号；带 `LLT` 的谓词里的 `OpIdx` 才是 `getOperand()` 的下标。
3. **把字节和位混用**。`GIM_CheckMemorySizeEqualTo` 用字节；`*LLT` 系列用 `getSizeInBits()`。`i1` 的 `MemVT` 在 importer 里按 8 bit 对齐。
4. **用普通 `ldr`/`str` pattern 匹配 atomic**。`G_LOAD`/`G_STORE` 是共享 opcode，必须靠 `CheckMMOIsNonAtomic` 和 ordering 谓词把 atomic 挡掉；`GIM_CheckAtomicOrdering` 是精确比较；而 `OrStronger`/`Weaker` 的强弱关系里 Acquire 与 Release 不可比。
5. **以为 fold 会删除被匹配的定义者**。共享常量实验已经说明：折叠的是编码/操作数，定义者是否留下由剩余 use 决定；`isObviouslySafeToFold` 也不是通用副作用证明，非 load 定义者默认放行不代表任意有副作用的指令都能折。

---

## 8.7 进阶：还能往哪里挖

- **整条指令谓词**：`GIM_CheckCxxInsnPredicate` 调用 `testMIPredicate_MI`，适合看整条 MI 的多个操作数/flag；TableGen 入口是 `GISelPredicateCode`。
- **target flag**：`GIM_CheckSimplePredicate` 走 `testSimplePredicate`，可表达不适合塞进 feature 位集的 target 私有状态。
- **HwMode 共用位集**：`MAX_SUBTARGET_PREDICATES` 包含 HwModes，HwMode 切换也体现为 `PredicateBitset` 的一位。
- **load fold 的 20 条上限**：这是编译期成本与优化收益的折中，不是正确性问题；超过窗口就不再尝试折叠。

---

## 8.8 动手练习

### 练习 8.1 复现 feature 分叉

用 `faddh.ll` 分别跑带与不带 `-mattr=+fullfp16` 的 `llc`，再用 `-stop-after=instruction-select` 看 MIR。回答：没有 `+fullfp16` 时，`FADDHrr` 是在哪一步被拒绝的？去掉 `-global-isel-abort=1` 后，输出还可能来自哪条路径？

### 练习 8.2 改 atomic ordering

把 `acq_load` 改成 `seq_cst`，把 `rel_store` 改成 `monotonic`，重新生成汇编。AArch64 是否还只用一条 `ldar`/`stlr`？如果出现 `dmb`，用 `-stop-after=instruction-select` 判断它是在 InstructionSelect 选的，还是后续 pass 插入的。

### 练习 8.3 观察共享常量

让同一个 `G_CONSTANT` 有 1、2、3 个 use，其中一个是 `G_ADD`，另外的是 `G_MUL` 或 `G_STORE`。看 InstructionSelect 后的 MIR：常量什么时候折成立即数？什么时候必须物化？哪个 use 决定了它不能消失？

### 练习 8.4 改 MMO 的 alignment

选一条带 `:: (load (i32) ...)` 的 MIR，把 MMO 的 alignment 从 4 改成 1，再跑 `-run-pass=instruction-select`。如果仍然匹配，说明该 pattern 没有 `MinAlign` 约束，而不是 alignment 谓词失效。

### 练习 8.5 读源码

在 `GIMatchTableExecutorImpl.h` 找出：`GIM_CheckMemorySizeEqualToLLT` 在哪里检查 `MO.isReg()`；`GIM_CheckAtomicOrdering` 为什么要求 `hasOneMemOperand()`；`GIM_CheckHasOneUse` 用的是 `hasOneNonDBGUse` 还是 `hasOneUse`。再把 `isObviouslySafeToFold` 的返回路径画成流程图。

---

## 8.9 重点整理

- Feature 谓词是规则级的：`CondString` 变成 bit，`GIM_Try_CheckFeatures` 做子集判断；缺任一 feature 就 reject。
- `MAX_SUBTARGET_PREDICATES = SubtargetFeatures.size() + HwModes.size()`；`getAvailableFeatures()` 返回 module 与 function 位集的并集。
- MMO 是第二套操作数：`MMOIdx` 与 `OpIdx` 不同；size 有字节和位两种谓词；alignment 是最低对齐；address space 是允许列表。
- `GINodeEquiv` 的 `CheckMMOIsNonAtomic` / `CheckMMOIsAtomic` 让共享 `G_LOAD`/`G_STORE` 在普通与原子语义之间分流。
- `GIM_CheckIsSafeToFold` 只检查“显然安全”：紧邻、非 convergent、load barrier、atomic/volatile load、20 条指令窗口；`HasOneUse`/`HasNoUse` 只看第一个结果并忽略 debug use。
- 匹配到定义者 ≠ 删除定义者；共享常量会折叠进 `ADDXri`，同时为 `G_MUL` 保留物化。

---

## 8.10 自我检核

- [ ] 为什么 `GIM_Try_CheckFeatures` 用子集判断，而不是 `AvailableFeatures == FeatureBitsets[ID]`？
- [ ] `HasFullFP16` 和 `HasNoFullFP16` 在位集里是什么关系？为什么不能只存一个 bit 然后取反？
- [ ] `GIM_CheckMemorySizeEqualTo` 和 `*LLT` 系列分别用什么单位？`MMOIdx` 和 `OpIdx` 各指什么？
- [ ] 普通 `ldr` pattern 如何从 `GINodeEquiv` 走到 `CheckMMOIsNonAtomic`，从而拒绝 acquire load？
- [ ] `GIM_CheckAtomicOrderingOrStrongerThan(Acquire)` 会接受 Release 吗？为什么？
- [ ] `isObviouslySafeToFold` 对同块 load 检查了哪些条件？为什么有 20 条指令上限？
- [ ] 共享常量实验里，为什么 `G_MUL` 仍然需要 `MOVi32imm`？删掉这个常量会怎样？

---

## 8.11 延伸阅读

- **LLVM 文档：InstructionSelect** — <https://llvm.org/docs/GlobalISel/InstructionSelect.html>。先读 “SelectionDAG Rule Imports” 和 “API: InstructionSelector”，理解 pattern 导入与 `selectImpl` 的分工。
- **`GIMatchTableExecutor.h`** — `GIM_*` enum 的权威注释，重点看 `GIM_Try_CheckFeatures`、`GIM_CheckMemory*`、`GIM_CheckAtomicOrdering*`、`GIM_CheckIsSafeToFold` 的参数编码。
- **`GIMatchTableExecutorImpl.h`** — 每个谓词的执行实现；对照读 `GIM_CheckMemorySizeEqualToLLT` 的 `TypeSize` 比较和 `GIM_CheckAtomicOrderingWeakerThan` 的 `isStrongerThan` 方向。
- **`GIMatchTableExecutor.cpp`** — `isObviouslySafeToFold` 的完整实现；把“紧邻/跨块/convergent/load barrier/20 条上限”五条路径画成流程图。
- **`SelectionDAGCompat.td` 与 `GlobalISelEmitter.cpp`** — 前者定义 `CheckMMOIsNonAtomic`/`CheckMMOIsAtomic`，后者把字段翻译成原子性谓词；配合 `AArch64InstrInfo.td` 搜 `HasFullFP16`、`anyext_oneuse` 对照本章例子。

---

## 下一章

[第 9 章 嵌套匹配与折叠](./09-nested-match-fold.md) —— 本章讲了“能不能折”的守卫条件；下一章进入嵌套结构本身：`GIM_RecordInsn` 如何给多层定义者编号、`GIM_CheckIsSameOperand` 如何约束重复操作数，以及一条 pattern 如何从树状匹配走到多条输出指令。
