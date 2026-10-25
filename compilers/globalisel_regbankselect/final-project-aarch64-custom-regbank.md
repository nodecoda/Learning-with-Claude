# Final Project — 给 AArch64 加一个 feature-gated custom bank/mapping

> **目标**：把前 23 章和四个练习收束成一个可评审的改动：为 feature-gated 的 `G_AARCH64_CUSTOM_LOGIC`（`i32`/`i64` 二元逻辑）实现 GPR 默认 mapping 与 FPR alternative，覆盖 `copyCost`/repair、Fast/Greedy、feature gate、MIR 测试和 `-verify-machineinstrs`。只做到 `-stop-after=regbankselect`，不写 InstructionSelect。
>
> **环境**：LLVM 23.1.4，AArch64 主 target。现有 `G_OR` 的等价实验在官方预编译包上实测；新增 opcode 的 TableGen、mapping、构建与测试标 guided，不伪造构建/测试输出。
>
> **证据约定**：`实测` 是官方包上可复现的 MIR/asm；`源码依据` 是 23.1.4 的 `RegisterBankInfo`/`RegBankSelect`/AArch64 源码；`guided` 是未执行的构建与自定义 opcode 输出。

## 项目背景与目标

真实 target 里经常出现一种需求：某个新指令或 pseudo 在语义上是一个二元逻辑操作，但它的最佳 bank 取决于上下文。

- 当两个操作数来自整数计算时，它应该在 GPR；
- 当两个操作数来自浮点位模式（例如 `bitcast` 后的 `or`）时，它应该在 FPR，避免 FPR→GPR→FPR 的来回搬运；
- 当它出现在 `optnone` 或被强制 Fast 的函数里时，只应使用默认 mapping，不应承担 Greedy 的编译时间；
- 当 feature 关闭时，它根本不该出现；即使 MIR 手写了它，也不该被静默映射。

`G_OR` 在 AArch64 上已经是这个模式的现成例子：默认 GPR，alternative 提供 GPR/FPR，Greedy 可以把整条 bitcast 链留在 FPR。本项目把同样的结构复制到一个**新的、feature-gated 的 target pseudo** 上，但要求你自己写清楚每一层：

```text
TableGen 定义 opcode
        │
        ▼
feature gate（生成侧 + mapping 侧）
        │
        ▼
默认 mapping（GPR）           ← Fast 只走这里
        │
        ▼
alternative mappings（GPR/FPR）← Greedy 才比较
        │
        ▼
applyMappingImpl（ID 1/2 必须处理）
        │
        ▼
repair COPY + copyCost
        │
        ▼
MIR 测试（Fast / Greedy / 负向 / verifier）
```

项目的成功标准不是“汇编看起来对”，而是：**在任何一种模式下，你都能从 MIR 解释每个 vreg 的 bank 和每条跨 bank COPY 的来源。**

## 项目规格

### 2.1 opcode 语义

`G_AARCH64_CUSTOM_LOGIC` 是一个二元逻辑 pseudo：

- 三个操作数：`dst`、`src1`、`src2`，同为 `s32` 或 `s64`；
- 语义等价于按位的 `or`/`and`/`xor` 之一（本作业不区分具体布尔函数，用一个 opcode 代表）；
- `hasSideEffects = 0`，`isCommutable = 1`；
- 只允许标量 32/64 位；向量、`i128`、指针一律拒绝；
- 它是 post-legalizer pseudo：在 Legalizer 之后出现，MIR fixture 直接以 `legalized: true` 书写。

### 2.2 feature gate

- feature 名：`custom-logic`；
- 打开：`-mattr=+custom-logic`；
- 关闭：`-mattr=-custom-logic`；
- 生成侧：只有 feature 打开时，post-legalizer combine 才把 `G_OR` 替换成 `G_AARCH64_CUSTOM_LOGIC`；
- mapping 侧：feature 关闭时，`getInstrMapping` 返回 invalid，防止手写 MIR 绕过；
- 负向测试必须覆盖“feature 关闭但 MIR 手写 opcode”的情况。

### 2.3 mapping

| 模式 | mapping | ID | Cost | 操作数 bank | 谁使用 |
| --- | --- | --- | --- | --- | --- |
| Fast | 默认 GPR | `DefaultMappingID` | 1 | dst/src1/src2 全 GPR | `getInstrMapping` |
| Greedy | GPR alternative | 1 | 1 | 全 GPR | `getInstrAlternativeMappings` |
| Greedy | FPR alternative | 2 | 1 | 全 FPR | `getInstrAlternativeMappings` |

默认 mapping 走 `getSameKindOfOperandsMapping(MI)`；两个 alternative 走 `getValueMapping(PMI_FirstGPR/FPR, Size)`，`NumOperands = 3`。

### 2.4 copyCost 与 repair

AArch64 的 `copyCost`（23.1.4）：

- `GPR = COPY FPR` → 5；
- `FPR = COPY GPR` → 4；
- 同 bank → 0；
- 其他 → `RegisterBankInfo::copyCost`（同 bank 0，不同 bank 1）。

因此，两个 FPR 操作数 + FPR 结果的链，如果强行用 GPR mapping：

```text
G_AARCH64_CUSTOM_LOGIC 指令代价       1
src1 FPR -> GPR repair                5
src2 FPR -> GPR repair                5
结果 GPR -> FPR（返回/使用）          4
------------------------------------------
合计                                 15
```

FPR mapping 的同一链：

```text
指令代价                              1
repair                                0
结果同 bank                            0
------------------------------------------
合计                                  1
```

Greedy 应选 FPR；Fast 应留在默认 GPR，并产生 3 条跨 bank COPY。

### 2.5 范围

**包含**：

- TableGen opcode 定义；
- feature 定义与 `-mattr` 行为；
- `getInstrMapping` 默认 case；
- `getInstrAlternativeMappings` 的 GPR/FPR alternatives；
- `applyMappingImpl` 对 ID 1/2 的处理；
- MIR 测试（Fast/Greedy/负向/verifier）；
- 文档与 review checklist。

**不包含**：

- InstructionSelect / matcher pattern；
- 完整的 IR→opcode combine（可作为可选里程碑）；
- 寄存器分配、调度、性能基准；
- 真实硬件上的 correctness 证明（本作业只到 RegBankSelect）。

### 2.6 交付物

1. 一份 patch（guided 构建），至少改 3 个文件：`AArch64InstrGISel.td`、`AArch64Features.td`、`AArch64RegisterBankInfo.cpp`。
2. 三份 MIR：Fast、Greedy、负向（feature off 或向量）。
3. 一份测试运行记录：命令、`-verify-machineinstrs` 结果、期望 bank/COPY。
4. 一份 review checklist（见第 8 节）。
5. 可选：`PAIR` bank 的 i128 设计草案（见第 9 节）。

## 验收标准

| 编号 | 验收项 | 通过条件 | 证据级别 |
| --- | --- | --- | --- |
| A1 | opcode 定义 | `G_AARCH64_CUSTOM_LOGIC` 能在 MIR 中被解析 | guided |
| A2 | 默认 mapping | Fast 下 dst/src1/src2 全是 `gpr` | guided |
| A3 | FPR alternative | Greedy 在 FPR 输入下选 `fpr` | guided |
| A4 | COPY 数 | Fast 3 条跨 bank COPY，Greedy 0 条 | guided |
| A5 | copyCost | 代价表 15 vs 1，方向正确 | 源码依据 |
| A6 | applyMapping | Greedy 选 ID 1/2 时不崩溃，走 `applyDefaultMapping` | guided |
| A7 | feature gate | `-mattr=-custom-logic` 时无法映射/abort | guided |
| A8 | 类型限制 | 向量/i128 被拒绝，不落到 FPR fallback | guided |
| A9 | verifier | 所有正向 MIR 加 `-verify-machineinstrs` 通过 | guided |
| A10 | 回归 | 现有 `G_OR` 行为不变（Fast/Greedy baseline 仍成立） | 实测 |
| A11 | 范围 | 停在 RegBankSelect，没有 ISel 依赖 | guided |
| A12 | 可读性 | patch 有注释，测试名能表达意图 | review |

## 里程碑

### M0 — 锁定 baseline（实测）

先用现有 `G_OR` 的 hand-written MIR 锁定“Fast 3 条、Greedy 0 条”的形状。这一步不需要改源码，是后面所有预测的参照。

```yaml
--- |
  target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
  target triple = "aarch64-unknown-linux-gnu"
  define float @or_s(float %a, float %b) { ret float %a }
...
---
name:            or_s
alignment:       4
isSSA:           true
legalized:       true
regBankSelected: false
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: _ }
  - { id: 1, class: _ }
  - { id: 2, class: _ }
body:             |
  bb.0:
    liveins: $s0, $s1

    %0(s32) = COPY $s0
    %1(s32) = COPY $s1
    %2(s32) = G_OR %0, %1
    $s0 = COPY %2(s32)
    RET_ReallyLR implicit $s0
...
```

Fast 实测：

```yaml
%0:fpr(s32) = COPY $s0
%1:fpr(s32) = COPY $s1
%3:gpr(i32) = COPY %0(s32)
%4:gpr(i32) = COPY %1(s32)
%2:gpr(s32) = G_OR %3, %4
$s0 = COPY %2(s32)
```

Greedy 实测：

```yaml
%0:fpr(s32) = COPY $s0
%1:fpr(s32) = COPY $s1
%2:fpr(s32) = G_OR %0, %1
$s0 = COPY %2(s32)
```

验收：Fast 的 `G_OR` 在 GPR，两个 FPR→GPR COPY；返回 `$s0 = COPY %2` 是 GPR→FPR。Greedy 全 FPR，0 条跨 bank COPY。把这个形状记下来，M2/M3 的自定义 opcode 应复制它。

### M1 — 定义 opcode 与 feature（guided）

在 `llvm/lib/Target/AArch64/AArch64InstrGISel.td` 加：

```tablegen
def G_AARCH64_CUSTOM_LOGIC : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type0:$src1, type0:$src2);
  let hasSideEffects = 0;
  let isCommutable = 1;
  let GISelMatchGenericTypes = 1;
}
```

在 `llvm/lib/Target/AArch64/AArch64Features.td` 加：

```tablegen
def FeatureCustomLogic : SubtargetFeature<"custom-logic", "HasCustomLogic",
  "true", "Enable the G_AARCH64_CUSTOM_LOGIC pseudo">;
```

验收：`llc -mattr=help -mtriple=aarch64 | Select-String custom-logic` 能看到 feature；MIR 解析器能识别 opcode。仅完成 M1 时，RegBankSelect 可能仍能靠 type fallback 映射标量，所以 M1 的测试不能作为最终通过标准。

### M2 — 默认 GPR mapping（guided）

在 `AArch64RegisterBankInfo::getInstrMapping` 的 switch 加：

```cpp
case AArch64::G_AARCH64_CUSTOM_LOGIC: {
  if (!STI.hasFeature(AArch64::FeatureCustomLogic))
    return getInvalidInstructionMapping();

  LLT Ty = MRI.getType(MI.getOperand(0).getReg());
  if (!Ty.isScalar() || (Ty.getSizeInBits() != 32 && Ty.getSizeInBits() != 64))
    return getInvalidInstructionMapping();

  return getSameKindOfOperandsMapping(MI);
}
```

验收：

- 标量 `s32`/`s64` Fast 输出 `gpr`；
- 向量/i128 输出无法映射；
- feature off 无法映射；
- `-verify-machineinstrs` 通过。

M2 只验证默认路径。Greedy 此时没有 FPR alternative，应与 Fast 相同（除了 GPR alternative 可能被选，但结果一样）。

### M3 — FPR alternative + applyMappingImpl（guided）

在 `getInstrAlternativeMappings` 加：

```cpp
case AArch64::G_AARCH64_CUSTOM_LOGIC: {
  if (!STI.hasFeature(AArch64::FeatureCustomLogic))
    break;

  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 32 && Size != 64)
    break;
  if (MI.getNumOperands() != 3)
    break;

  InstructionMappings AltMappings;
  const InstructionMapping &GPRMapping = getInstructionMapping(
      /*ID*/ 1, /*Cost*/ 1, getValueMapping(PMI_FirstGPR, Size),
      /*NumOperands*/ 3);
  const InstructionMapping &FPRMapping = getInstructionMapping(
      /*ID*/ 2, /*Cost*/ 1, getValueMapping(PMI_FirstFPR, Size),
      /*NumOperands*/ 3);

  AltMappings.push_back(&GPRMapping);
  AltMappings.push_back(&FPRMapping);
  return AltMappings;
}
```

**关键：** alternative 的 ID 不是 `DefaultMappingID`，Greedy 选中它时会调用 `applyMappingImpl`，而不是 `applyDefaultMapping`。AArch64 的 `applyMappingImpl` 默认分支是 `llvm_unreachable`，所以必须把新 opcode 加到 `G_OR`/`G_BITCAST` 的 case 里：

```cpp
case AArch64::G_AARCH64_CUSTOM_LOGIC:
case TargetOpcode::G_OR:
case TargetOpcode::G_BITCAST:
  // Those ID must match getInstrAlternativeMappings.
  assert((OpdMapper.getInstrMapping().getID() >= 1 &&
          OpdMapper.getInstrMapping().getID() <= 4) &&
         "Don't know how to handle that ID");
  return applyDefaultMapping(OpdMapper);
```

验收：

- FPR 输入的 Greedy 选 FPR，0 条跨 bank COPY；
- GPR 输入的 Greedy 选 GPR，0 条或仅 ABI COPY；
- 不崩溃（说明 `applyMappingImpl` 的 case 生效）；
- Fast 不变，仍是 GPR + 3 条 COPY。

### M4 — 测试与负向覆盖（guided）

至少三份 MIR：

1. `custom-logic-fast.mir`：默认模式，CHECK `gpr` + 两条 FPR→GPR COPY + 返回 GPR→FPR。
2. `custom-logic-greedy.mir`：`-regbankselect-greedy`，CHECK `fpr` + 无跨 bank COPY。
3. `custom-logic-negative.mir`：feature off 或向量输入，CHECK `not` 失败。

每条 RUN 都加 `-verify-machineinstrs`。如果官方包没有 FileCheck，用 `Select-String` 做本地检查，lit 版本标 guided。

### M5 — 可选 PAIR bank（guided）

见第 9 节。只有在 M0–M4 全部稳定、且你愿意处理 bank coverage 冲突时才做。

## 期望 MIR

### Fast（guided）

```yaml
registers:
  - { id: 0, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 3, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 4, class: gpr, preferred-register: '', flags: [  ] }

body:             |
  bb.0:
    liveins: $s0, $s1

    %0:fpr(s32) = COPY $s0
    %1:fpr(s32) = COPY $s1
    %3:gpr(i32) = COPY %0(s32)
    %4:gpr(i32) = COPY %1(s32)
    %2:gpr(s32) = G_AARCH64_CUSTOM_LOGIC %3, %4
    $s0 = COPY %2(s32)
    RET_ReallyLR implicit $s0
...
```

### Greedy（guided）

```yaml
registers:
  - { id: 0, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: fpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: fpr, preferred-register: '', flags: [  ] }

body:             |
  bb.0:
    liveins: $s0, $s1

    %0:fpr(s32) = COPY $s0
    %1:fpr(s32) = COPY $s1
    %2:fpr(s32) = G_AARCH64_CUSTOM_LOGIC %0, %1
    $s0 = COPY %2(s32)
    RET_ReallyLR implicit $s0
...
```

### 形状依据（实测）

这两份期望不是凭空写的。用 `G_OR` 跑同样的 hand-written MIR，Fast/Greedy 的输出形状完全一致（只把 opcode 换成 `G_OR`）。所以自定义 opcode 的 mapping 如果与 `G_OR` 同构，输出也应同构；如果不同，优先怀疑：

- `getSameKindOfOperandsMapping` 把 `isPreISelGenericFloatingPointOpcode(Opc)` 判成了真；
- alternative 的 `Size`/`NumOperands` 条件没满足；
- `applyMappingImpl` 没处理 ID；
- feature gate 把某条路径提前拒绝了。

## 完整解答草图

<details>
<summary>展开：完整 patch、构建、运行与预期结果（guided）</summary>

### 1. 文件与改动清单

| 文件 | 改动 | 目的 |
| --- | --- | --- |
| `llvm/lib/Target/AArch64/AArch64InstrGISel.td` | 新增 opcode | 让 MIR/TableGen 认识它 |
| `llvm/lib/Target/AArch64/AArch64Features.td` | 新增 `FeatureCustomLogic` | `-mattr=+custom-logic` |
| `llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp` | 默认 case、alternative case、`applyMappingImpl` case | bank 选择与修复 |
| `llvm/test/CodeGen/AArch64/GlobalISel/` | 新增 3 份 MIR/lit | 回归 |
| （可选）`AArch64PostLegalizerLowering.cpp` | combine G_OR → custom | 端到端生成 |

### 2. 默认 mapping 的完整 case

```cpp
case AArch64::G_AARCH64_CUSTOM_LOGIC: {
  // Feature gate: defense in depth. The producer should also be gated.
  if (!STI.hasFeature(AArch64::FeatureCustomLogic))
    return getInvalidInstructionMapping();

  // Only scalar 32/64-bit. Vectors and i128 must not fall through to the
  // type-based fallback, which would put them in FPR.
  LLT Ty = MRI.getType(MI.getOperand(0).getReg());
  if (!Ty.isScalar() || (Ty.getSizeInBits() != 32 && Ty.getSizeInBits() != 64))
    return getInvalidInstructionMapping();

  return getSameKindOfOperandsMapping(MI);
}
```

### 3. alternative 的完整 case

```cpp
case AArch64::G_AARCH64_CUSTOM_LOGIC: {
  if (!STI.hasFeature(AArch64::FeatureCustomLogic))
    break;

  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 32 && Size != 64)
    break;

  if (MI.getNumOperands() != 3)
    break;

  InstructionMappings AltMappings;
  const InstructionMapping &GPRMapping = getInstructionMapping(
      /*ID*/ 1, /*Cost*/ 1, getValueMapping(PMI_FirstGPR, Size),
      /*NumOperands*/ 3);
  const InstructionMapping &FPRMapping = getInstructionMapping(
      /*ID*/ 2, /*Cost*/ 1, getValueMapping(PMI_FirstFPR, Size),
      /*NumOperands*/ 3);

  AltMappings.push_back(&GPRMapping);
  AltMappings.push_back(&FPRMapping);
  return AltMappings;
}
```

### 4. applyMappingImpl 的完整 case

```cpp
case AArch64::G_AARCH64_CUSTOM_LOGIC:
case TargetOpcode::G_OR:
case TargetOpcode::G_BITCAST:
  assert((OpdMapper.getInstrMapping().getID() >= 1 &&
          OpdMapper.getInstrMapping().getID() <= 4) &&
         "Don't know how to handle that ID");
  return applyDefaultMapping(OpdMapper);
```

为什么必须加：`RegisterBankInfo::applyMapping` 只在 `ID == DefaultMappingID` 时直接 `applyDefaultMapping`；其他 ID 一律调 `applyMappingImpl`：

```cpp
if (OpdMapper.getInstrMapping().getID() == DefaultMappingID)
  return applyDefaultMapping(OpdMapper);
applyMappingImpl(Builder, OpdMapper);
```

没有这个 case，Greedy 一旦选中 ID 1/2，就会落到 `llvm_unreachable("Don't know how to handle that operation")`。这是本项目最容易漏的一步。

### 5. 可选的生成 combine

生成侧在 post-legalizer combine 里把 `G_OR` 替换成新 opcode，并用 `STI.hasFeature(AArch64::FeatureCustomLogic)` 包住。没有它，MIR 测试仍可手动写 opcode；有了它，IR 路径才能端到端生成。

### 6. 构建

```bash
cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AArch64" \
  -DLLVM_CCACHE_BUILD=ON \
  -DLLVM_USE_LINKER=lld \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++

ninja -C build llc FileCheck not count split-file
```

本作业没有执行这段构建；所有自定义 opcode 的输出都标 guided。构建后先跑 `llc --version` 和 `llc -mattr=help -mtriple=aarch64 | Select-String custom-logic`，确认 feature 存在。

### 7. 运行

这些命令只跑到 RegBankSelect（`-run-pass=regbankselect`），不会进入 InstructionSelect；对 `.ll` 输入等价地使用 `-stop-after=regbankselect`。

```powershell
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-src\build\bin\llc.exe"

# Fast
& $llc -mtriple=aarch64-linux-gnu -mattr=+custom-logic `
  -run-pass=regbankselect -verify-machineinstrs `
  final\custom-logic-fast.mir -o final\custom-logic-fast-rbs.mir

# Greedy
& $llc -mtriple=aarch64-linux-gnu -mattr=+custom-logic `
  -run-pass=regbankselect -regbankselect-greedy -verify-machineinstrs `
  final\custom-logic-greedy.mir -o final\custom-logic-greedy-rbs.mir

# feature off（应失败）
& $llc -mtriple=aarch64-linux-gnu -mattr=-custom-logic `
  -run-pass=regbankselect -global-isel-abort=1 -verify-machineinstrs `
  final\custom-logic-fast.mir -o final\custom-logic-nofeature.mir
```

### 8. 预期结果

| 运行 | 期望 | 说明 |
| --- | --- | --- |
| Fast | `%2:gpr`，`%3`/`%4` FPR→GPR，返回 GPR→FPR | 默认 GPR mapping |
| Greedy | `%2:fpr`，无跨 bank COPY | FPR alternative cost 1 |
| feature off | 报错/abort | mapping case 返回 invalid |
| 向量 | 报错/abort | 标量检查拒绝 |
| i128 | 报错/abort | 标量 32/64 检查拒绝 |
| verifier | 正向退出码 0 | 没有 bank/类型不匹配 |

### 9. 一个可提交的 lit 测试草图

```yaml
# RUN: llc -mtriple=aarch64-- -mattr=+custom-logic -run-pass=regbankselect \
# RUN:   -verify-machineinstrs %s -o - | FileCheck %s --check-prefix=FAST
# RUN: llc -mtriple=aarch64-- -mattr=+custom-logic -run-pass=regbankselect \
# RUN:   -regbankselect-greedy -verify-machineinstrs %s -o - \
# RUN:   | FileCheck %s --check-prefix=GREEDY

# FAST-LABEL: name: custom_logic
# FAST: %[[A:.*]]:fpr(s32) = COPY $s0
# FAST: %[[B:.*]]:fpr(s32) = COPY $s1
# FAST: %[[C:.*]]:gpr(i32) = COPY %[[A]]
# FAST: %[[D:.*]]:gpr(i32) = COPY %[[B]]
# FAST: %[[R:.*]]:gpr(s32) = G_AARCH64_CUSTOM_LOGIC %[[C]], %[[D]]
# FAST: $s0 = COPY %[[R]]

# GREEDY-LABEL: name: custom_logic
# GREEDY: %[[A:.*]]:fpr(s32) = COPY $s0
# GREEDY: %[[B:.*]]:fpr(s32) = COPY $s1
# GREEDY: %[[R:.*]]:fpr(s32) = G_AARCH64_CUSTOM_LOGIC %[[A]], %[[B]]
# GREEDY: $s0 = COPY %[[R]]
```

用 `utils/update_mir_test_checks.py` 生成实际 CHECK 更稳；手写 CHECK 要注意 vreg 编号和 `i32`/`s32` 打印差异。官方包没有 FileCheck，lit 部分标 guided。

### 10. 负向 lit

```yaml
# RUN: not llc -mtriple=aarch64-- -mattr=-custom-logic -run-pass=regbankselect \
# RUN:   -global-isel-abort=1 -verify-machineinstrs %s -o /dev/null
```

向量版本同理，只把 `-mattr=-custom-logic` 换成 `+custom-logic`，MIR 用向量类型。`not` 来自 LLVM 的 `not` 工具；官方 Windows 包可能没有，需自建或用 `$LASTEXITCODE` 检查。不要伪造失败输出。

</details>

## 可选扩展：为 i128 设计 PAIR bank

这是一个设计练习，不是必做项。AArch64 的 `i128` 目前由 GPR bank 覆盖到 128 位（`GPR128` partial mapping + `XSeqPairsClass`）。如果你要一个独立的 `PAIR` bank，先回答四个问题：

1. **coverage 冲突**：`XSeqPairsClass` 已经被 `GPRRegBank` 覆盖。再定义 `PAIRRegBank : RegisterBank<"PAIR", [XSeqPairsClass]>` 会让同一个寄存器类同时属于两个 bank。`getRegBankFromRegClass` 会选谁？生成的 `BankIDToCopyMapIdx` 怎么填？
2. **GPR 的最大位宽**：如果从 GPR 移除 `XSeqPairsClass`，GPR 的 `getMaximumSize` 会从 128 降到 64，所有 i128 的 GPR mapping 都要改走 PAIR；这是一个全局改动。
3. **partial mapping**：i128 在 PAIR 里是“一个 128 位值”还是“两个 64 位 partial”？如果是两个，`ValueMapping` 要写 `{0,64,PAIR},{64,64,PAIR}`，`assignmentMatch` 的 `NumBreakDowns != 1` 会走 breakdown 路径，repair 更复杂。
4. **copyCost/ABI**：PAIR↔GPR、PAIR↔FPR 的 copyCost 是多少？ABI 的 i128 参数在 `x0/x1` 还是 pair 寄存器？copyCost 不能拍脑袋，要有指令/调度依据。

一个最小草案（guided，不保证可编译/通过 verifier）：

```tablegen
// AArch64RegisterBanks.td（草案）
def PAIRRegBank : RegisterBank<"PAIR", [XSeqPairsClass]>;
```

```cpp
// mapping 草案：仅 i128 的自定义 opcode
case AArch64::G_AARCH64_CUSTOM_LOGIC: {
  LLT Ty = MRI.getType(MI.getOperand(0).getReg());
  if (Ty.isScalar() && Ty.getSizeInBits() == 128)
    return getInstructionMapping(DefaultMappingID, 1,
                                 getValueMapping(PMI_FirstPAIR, Ty.getSizeInBits()),
                                 3);
  // ...
}
```

风险：

- `PMI_FirstPAIR` 需要新的 partial mapping 表项和 `getValueMapping` 支持；
- `AArch64GenRegisterBankInfo.def` 的 `BankIDToCopyMapIdx` 要加 PAIR；
- `AArch64RegisterBankInfo` 构造函数的 `assert` 要更新；
- `getRegBankFromRegClass` 的生成逻辑可能产生歧义；
- `GPR` 的 128 位覆盖被拆掉后，现有 i128 测试可能失败。

建议把这个扩展作为独立 RFC，而不是塞进主 patch。先做“只读设计文档 + 一个不改变 GPR 的 PAIR 实验”，确认 coverage/verifier 行为，再决定是否落地。

## 测试用例

| 用例 | 输入 | 命令 | 期望 | 证据级别 |
| --- | --- | --- | --- | --- |
| G_OR Fast 形状 | `or_s.mir` | `-run-pass=regbankselect` | GPR + 3 条跨 bank COPY | 实测 |
| G_OR Greedy 形状 | `or_s.mir` | `-regbankselect-greedy` | FPR + 0 条 | 实测 |
| 自定义 Fast | `custom-logic-fast.mir` | `-mattr=+custom-logic` | `gpr` + 3 条 | guided |
| 自定义 Greedy | `custom-logic-greedy.mir` | `-regbankselect-greedy` | `fpr` + 0 条 | guided |
| 自定义 i64 Fast | `custom-logic-i64.mir` | `-mattr=+custom-logic` | `gpr(s64)` + 3 条 | guided |
| 自定义 i64 Greedy | 同上 | `-regbankselect-greedy` | `fpr(s64)` + 0 条 | guided |
| GPR 输入 Greedy | `$x0/$x1` 版本 | `-regbankselect-greedy` | `gpr`，0 条 | guided |
| feature off | 标量 MIR | `-mattr=-custom-logic` | 失败/abort | guided |
| 向量负向 | `<2 x s32>` | `-mattr=+custom-logic` | 失败/abort | guided |
| i128 负向 | `s128` | `-mattr=+custom-logic` | 失败/abort | guided |
| verifier | 正向 MIR | `-verify-machineinstrs` | 退出码 0 | guided |
| 现有 G_OR 回归 | `or.ll` | Fast/Greedy | 3/0 条不变 | 实测 |
| applyMapping ID | Greedy 选 ID 1/2 | 运行 | 不崩溃 | guided |
| PAIR i128 | 草案 | 未执行 | 设计评审 | guided |

## 评审清单

### 语义与范围

- [ ] opcode 的语义、类型限制（仅 `s32`/`s64`）、commutativity 写清楚了
- [ ] 明确它是 post-legalizer pseudo，还是 legalizer 可见 opcode；两者只能选一个
- [ ] 没有把 InstructionSelect/pattern 混进本 patch
- [ ] 没有改变现有 `G_OR`/`G_BITCAST`/`G_LOAD` 的行为

### mapping

- [ ] 默认 mapping 是 GPR，且 Fast 只走默认
- [ ] alternative 的 ID 唯一，Cost 有依据
- [ ] `getInstrAlternativeMappings` 检查 feature、Size、NumOperands
- [ ] `applyMappingImpl` 处理了新的 ID，不会落到 `llvm_unreachable`
- [ ] `copyCost` 的方向（Dst, Src）写对，5/4 没有被写反

### feature

- [ ] 生成侧 gate 和 mapping 侧 gate 都存在
- [ ] `-mattr=+custom-logic` / `-mattr=-custom-logic` 都有测试
- [ ] feature 关闭时不会被 type fallback 静默映射
- [ ] feature 关闭时 IR 路径也不会生成该 opcode

### 测试

- [ ] Fast/Greedy 各一份 MIR，CHECK 锁 bank 与 COPY
- [ ] 负向测试能失败（feature off、向量、i128）
- [ ] 所有正向测试带 `-verify-machineinstrs`
- [ ] 测试不依赖 `-simplify-mir`
- [ ] lit 测试用 `update_mir_test_checks.py` 生成，避免手写 vreg 编号
- [ ] 没有把 ABI 边界 COPY 误报成 repair

### 质量与上游

- [ ] patch 有注释，解释为什么需要 FPR alternative
- [ ] 编译时间/性能有单独测量，不在本 patch 里拍脑袋
- [ ] 遵循 LLVM Coding Standards，`clang-format` 通过
- [ ] commit message 说明动机、范围、测试
- [ ] 如果做 PAIR，单独 RFC，附 coverage/verifier 分析

## 常见失败模式

1. **Greedy 崩溃**：只加了 `getInstrAlternativeMappings`，没加 `applyMappingImpl`。Greedy 选中 ID 1/2 后落到 `llvm_unreachable`。修复：把新 opcode 加到 `G_OR`/`G_BITCAST` 的 case。
2. **Fast 看起来对，Greedy 没变化**：只加了默认 mapping，没加 alternative；或者 alternative 的 `NumOperands`/`Size` 条件不满足，直接 `break`。检查 `MI.getNumOperands()` 是否为 3，`Size` 是否 32/64。
3. **feature off 仍然能映射**：只在生成 combine 里 gate，MIR 手写 opcode 绕过了；或者 `getInstrMapping` 的 case 没有返回 invalid，而是 `break` 到 type fallback。修复：case 内显式 `return getInvalidInstructionMapping()`。
4. **向量/i128 落到 FPR**：没有标量/位宽检查，type fallback 把向量或 >64 位值映射到 FPR。修复：在 case 内检查 `Ty.isScalar()` 和 32/64。
5. **COPY 数对不上**：把 ABI 边界 COPY 算进 repair，或漏掉返回路径的 GPR→FPR。修复：先列 `Dst = COPY Src`，再套 `copyCost(DstBank, SrcBank)`。
6. **`optnone` 让 Greedy 失效**：测试函数带了 `optnone`，`runOnMachineFunction` 强制 Fast。修复：测试函数不要带 `optnone`，或专门写一个 `optnone` 对照测试。
7. **用 `-O0` 解释 Fast**：`-O0` 本身不强制 Fast；真正的原因是 `optnone`。不要把两者混为一谈。
8. **测试只 CHECK `gpr`**：type fallback 也能给出 `gpr`，测试没有证明你的 case 生效。修复：加 feature-off/向量负向测试，或检查 mapping ID。

## 延伸挑战

1. **让 opcode 从 IR 生成**：在 post-legalizer combine 里把 `G_OR` 替换成 `G_AARCH64_CUSTOM_LOGIC`，用 `-stop-after=regbankselect` 跑一个 `.ll`，确认 Fast/Greedy 的 bank 与 MIR fixture 一致。
2. **把 FPR alternative 的 Cost 改成 16**：观察 Greedy 是否退回 GPR。用练习 B 的代价表预测，再实测。注意这会影响所有使用该 opcode 的函数。
3. **加一个 `G_AARCH64_CUSTOM_LOGIC` 的 `G_FCMP` 变体**：只允许浮点比较结果进 GPR，练习“不同 opcode 的默认 bank 不同”。
4. **做 PAIR bank 的只读 RFC**：不改代码，先写 coverage/verifier/copyCost 分析，评审后再决定是否实现。
5. **测量编译时间**：在同一台机器上，对一个大函数分别跑 Fast/Greedy，记录 `llc` 时间。不要在没有测量的情况下声称 Greedy “更慢一点”。

## 延伸阅读

- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：mapping、Fast/Greedy、repair 的官方说明。
  - **学什么**：本项目各层职责的官方语境。
  - **和本项目的关联**：规格设计的对照基线；注意文档与源码默认值可能不一致，以源码为准。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`getInstrMapping`、`getInstrAlternativeMappings`、`applyMappingImpl`、`copyCost`。
  - **学什么**：`G_OR` 的完整实现，是本项目最好的模板。
  - **和本项目的关联**：M2/M3 的 patch 与它同构。
- **[RegisterBankInfo.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/RegisterBankInfo.h)**
  - **读哪里**：`PartialMapping`、`ValueMapping`、`InstructionMapping`、`applyMapping`。
  - **学什么**：mapping 的数据模型，ID 与 `applyMappingImpl` 的分派。
  - **和本项目的关联**：不读这个，容易漏掉 ID 1/2 的 `applyMappingImpl`。
- **[AArch64RegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterBanks.td)**
  - **读哪里**：GPR/FPR/CC 的 coverage。
  - **学什么**：PAIR bank 扩展为什么会和 GPR 的 `XSeqPairsClass` 冲突。
  - **和本项目的关联**：可选扩展的设计约束。

## 自我检核

- [ ] 我能画出从 TableGen 到 MIR 测试的完整路径，并说出每一步的输入/输出
- [ ] 我能解释 Fast 为什么只走默认 mapping，Greedy 为什么要比较 alternatives
- [ ] 我能算出 GPR 链的 15 和 FPR 链的 1，并说清每一项的方向
- [ ] 我知道 alternative ID 1/2 必须由 `applyMappingImpl` 处理，否则会崩溃
- [ ] 我能设计 feature-off、向量、i128 三个负向测试，并说明它们分别锁住什么
- [ ] 我能解释为什么“测试只 CHECK gpr”可能被 type fallback 欺骗
- [ ] 我知道 `optnone` 会强制 Fast，而 `-O0` 不会
- [ ] 我能说出 PAIR bank 扩展至少三个风险，不会直接把它塞进主 patch

→ [返回课程目录](./README.md)