# 练习 C — 实现一个 custom mapping

> **目标**：为一个 feature-gated 的 AArch64 自定义 generic opcode `G_AARCH64_CUSTOM_LOGIC` 加上 GPR 默认 mapping，并写一个 MIR 测试。重点不是“让输出出现 gpr”，而是搞清楚：没有显式 case 时谁会兜底、显式 case 到底改变了什么、feature gate 应该放在哪一层。
>
> **环境**：LLVM 23.1.4 源码（`.scratch/llvm-23.1.4-src`）作为阅读依据；官方预编译包用于现有 target opcode 的 fallback 实测。新增 opcode 需要改 TableGen 并重建 `llc`，本练习的构建与自定义 MIR 输出标 guided。
>
> **证据约定**：`实测` 是官方包上可复现的现有 opcode 行为；`源码依据` 是 23.1.4 的 TableGen/RegBankSelect 代码；`guided` 是改源码后未执行的构建与测试，文中给出完整草图与预期。

## 背景与动机

“给新 opcode 加 RegBankSelect 支持”听起来像一件小事：在 `getInstrMapping` 里加一个 `case`，返回 GPR。但真正动手时，常见的问题不是不会写 `case`，而是：

- 新 opcode 定义在哪个 `.td`？它是 generic opcode 还是 target pseudo？
- 它需要经过 Legalizer 吗，还是像 `G_REV32` 一样在 legalization 之后才出现？
- 不加 `case` 时，RegBankSelect 会失败，还是会被某个 fallback 悄悄映射？
- 为什么 MIR 测试里 `%2:gpr` 不能证明你的 `case` 被执行了？
- feature gate 应该放在 opcode 生成处、Legalizer、还是 `getInstrMapping`？

这些问题不解决，你会得到一种很危险的“测试通过”：输出确实是 `gpr`，但走的是 type-based fallback，不是你的 mapping；等某天 fallback 改了，测试还绿，行为却变了。

练习 C 用一个最小的自定义 opcode 把这条路径走一遍。我们只做到 RegBankSelect：不写 InstructionSelect，不生成最终汇编。这样你可以把注意力放在“opcode 定义 → legalized MIR → mapping → bank”这一段。

## 先建立直观

把新 opcode 想成一家新开的店，RegBankSelect 是给它分配“从哪个航站楼接客”的调度员：

```text
                 ┌──────────────────────────────┐
                 │ G_AARCH64_CUSTOM_LOGIC       │
                 │ dst, src1, src2 都是 s32/s64 │
                 └──────────────┬───────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │                                   │
     没有显式 mapping                      有显式 mapping
              │                                   │
              ▼                                   ▼
   getInstrMappingImpl 失败?               case 命中
              │                                   │
     是 ──────┴────── 否                 返回 GPR mapping
     │                 │                         │
     ▼                 ▼                         ▼
  type fallback     regclass 约束          明确的 ID/cost
  （可能“碰巧”对）   （target 指令常见）     feature gate 可检查
```

关键点：AArch64 的 `getInstrMapping` 在 switch 之后还有一段“按类型猜 bank”的兜底。它会让很多标量 opcode “碰巧”得到 GPR。于是，**一个只检查 `gpr` 的 MIR 测试，无法区分“显式 case”与“fallback”**。练习 C 的测试设计必须解决这个可观测性问题。

## 任务规格

### 输入

1. 一个自定义 opcode 的 TableGen 定义草案。
2. 一个 feature gate（本练习用 `+custom-logic`）。
3. 一份 legalized MIR，里面直接出现 `G_AARCH64_CUSTOM_LOGIC`。
4. 一个显式 `getInstrMapping` case，返回 GPR 默认 mapping。

### 输出

- 源码 patch 草图：TableGen、feature、`getInstrMapping`。
- MIR fixture 与预期 MIR（guided）。
- 一组测试：标量正向、向量负向、feature-off 负向。
- 一段说明：为什么标量正向测试不足以证明 `case` 被执行，负向测试如何补上。

### 约束

- 只做到 `-run-pass=regbankselect` 或 `-stop-after=regbankselect`；不写 InstructionSelect。
- 不要求 opcode 从 LLVM IR 自动生成；MIR fixture 代表“post-legalizer combine 之后”的状态。
- 不修改 `G_OR` 等现有 opcode 的语义。
- 新增 opcode 的构建与运行标 guided；官方包上的 fallback 实验标实测。

### 验收标准

- `G_AARCH64_CUSTOM_LOGIC` 的 `s32`/`s64` 版本在 Fast 下映射到 GPR。
- `+custom-logic` 关闭时，同一 MIR 不会被静默映射；应失败或 abort。
- 向量/i128 等非 32/64 标量输入被拒绝，而不是落到 FPR fallback。
- 能解释 `G_ADD_LOW` 的实测结果为什么不能直接证明“新 opcode 的 case 生效”。
- MIR 测试带 `-verify-machineinstrs`。

## 期望输出

标量 `s64` 的期望 MIR（guided，改源码并重建后应看到）：

```yaml
body:             |
  bb.0:
    liveins: $x0, $x1

    %0:gpr(s64) = COPY $x0
    %1:gpr(s64) = COPY $x1
    %2:gpr(s64) = G_AARCH64_CUSTOM_LOGIC %0, %1
    $x0 = COPY %2(s64)
    RET_ReallyLR implicit $x0
...
```

标量 `s32` 同理，寄存器变成 `$w0`/`$w1`，bank 仍是 `gpr`：

```yaml
%0:gpr(s32) = COPY $w0
%1:gpr(s32) = COPY $w1
%2:gpr(s32) = G_AARCH64_CUSTOM_LOGIC %0, %1
$w0 = COPY %2(s32)
```

没有跨 bank COPY，因为默认 mapping 把 dst/src1/src2 都放在 GPR，而入参 COPY 也来自 GPR 物理寄存器。

## 如果你卡住了

1. **不知道 opcode 定义在哪**：AArch64 的 target generic pseudo 在 `llvm/lib/Target/AArch64/AArch64InstrGISel.td`。文件里已有 `AArch64GenericInstruction` 基类和 `G_REV32`、`G_UZP1`、`G_ADD_LOW` 等例子。先照抄一个三元 opcode 的形状。
2. **以为一定要改 Legalizer**：不一定。像 `G_REV32` 这样的 pseudo 是在 legalization 之后由 combine 生成的，不会走 Legalizer。本练习把 `G_AARCH64_CUSTOM_LOGIC` 设计成 post-legalizer pseudo，MIR 直接以 `legalized: true` 出现；Legalizer 规则可以留到端到端版本再加。
3. **不加 case 也能得到 gpr**：这正是本练习的陷阱。AArch64 `getInstrMapping` 末尾有按类型兜底；标量 i32/i64 会落到 GPR。你需要一个 fallback 会做错的负向测试（向量或 i128）来证明 case 生效。
4. **feature gate 放错层**：如果只在生成 combine 里 gate，MIR 直接写 opcode 时仍能绕过；如果只在 mapping 里 gate，IR 路径仍可能生成不该有的 opcode。生产实现通常两处都做：生成处 gate + mapping 处防御。
5. **MIR 里写 `s64` 还是 `i64`**：两者都能被解析；手写用 `s64` 更接近 LLT 的打印，来自 IR 的 MIR 常见 `i64`。不要因为打印不同就以为 bank 不同。

## 实作步骤建议

1. **先做 fallback 实验**：用现有 `G_ADD_LOW` 写一份 legalized MIR，跑 `-run-pass=regbankselect`，确认标量 target pseudo 在没有任何显式 case 时也会得到 `gpr`。这一步是实测，见参考解答。
2. **定义 opcode**：在 `AArch64InstrGISel.td` 加 `G_AARCH64_CUSTOM_LOGIC`，三个 `type0` 操作数，`hasSideEffects = 0`，`GISelMatchGenericTypes = 1`。
3. **加 feature**：在 `AArch64Features.td` 加 `FeatureCustomLogic`，用 `-mattr=+custom-logic` 打开。先不写生成 combine，只让 MIR 能手动写这个 opcode。
4. **加 mapping case**：在 `AArch64RegisterBankInfo::getInstrMapping` 里加 case，检查 feature、检查标量 32/64，然后 `return getSameKindOfOperandsMapping(MI);`。
5. **写三组 MIR 测试**：标量正向、向量负向、feature-off 负向；重建 `llc` 后跑 `-run-pass=regbankselect -verify-machineinstrs`，确认正向是 gpr、负向失败。

## 完整参考解答

<details>
<summary>展开：fallback 实测、TableGen/feature/mapping patch 与测试</summary>

### 1. 先证明 fallback 存在（实测）

用现有 target pseudo `G_ADD_LOW` 写一份最小 MIR。它不是新 opcode，但同样是 `AArch64GenericInstruction`，同样没有在 `getInstrMapping` 里被显式处理：

```yaml
--- |
  target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
  target triple = "aarch64-unknown-linux-gnu"
  define i64 @addlow(i64 %a) { ret i64 %a }
...
---
name:            addlow
alignment:       4
isSSA:           true
legalized:       true
regBankSelected: false
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: _ }
  - { id: 1, class: _ }
body:             |
  bb.0:
    liveins: $x0

    %0(s64) = COPY $x0
    %1(s64) = G_ADD_LOW %0(s64), 4
    $x0 = COPY %1(s64)
    RET_ReallyLR implicit $x0
...
```

```powershell
& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -verify-machineinstrs practice-c\addlow.mir -o practice-c\addlow-rbs.mir
```

输出（实测）：

```yaml
body:             |
  bb.0:
    liveins: $x0

    %0:gpr(s64) = COPY $x0
    %1:gpr(s64) = G_ADD_LOW %0(s64), 4
    $x0 = COPY %1(s64)
    RET_ReallyLR implicit $x0
...
```

结论：**没有显式 case 的标量 target pseudo，也会被 type fallback 映射到 GPR。** 因此，新 opcode 的标量正向测试如果只 CHECK `gpr`，通过与否和你的 case 无关。

为什么 fallback 会这样？`AArch64RegisterBankInfo::getInstrMapping` 先对非 generic opcode 调 `getInstrMappingImpl`：

```cpp
if ((Opc != TargetOpcode::COPY && !isPreISelGenericOpcode(Opc)) ||
    Opc == TargetOpcode::G_PHI) {
  const RegisterBankInfo::InstructionMapping &Mapping = getInstrMappingImpl(MI);
  if (Mapping.isValid())
    return Mapping;
}
```

`G_ADD_LOW` 不是 `isPreISelGenericOpcode` 范围内的 generic opcode，所以会先试 `getInstrMappingImpl`。它没有寄存器类约束，`getRegBankFromConstraints` 失败，返回 invalid。然后 switch 没有 case，落到函数末尾的按类型兜底：

```cpp
// AArch64RegisterBankInfo::getInstrMapping 末尾（简化）
if (Ty.isVector())
  OpRegBankIdx[Idx] = PMI_FirstFPR;
else if (isPreISelGenericFloatingPointOpcode(Opc) || ... )
  OpRegBankIdx[Idx] = PMI_FirstFPR;
else
  OpRegBankIdx[Idx] = PMI_FirstGPR;
```

所以 `s64` → GPR。这段兜底是“按类型猜”，不是“你的 opcode 有语义”。它不会检查 feature，也不会为你提供 FPR alternative。

### 2. TableGen：定义 opcode

文件：`llvm/lib/Target/AArch64/AArch64InstrGISel.td`。文件顶部已经有：

```tablegen
class AArch64GenericInstruction : GenericInstruction {
  let Namespace = "AArch64";
}
```

在文件里加：

```tablegen
// A feature-gated bitwise logic pseudo. Produced post-legalization from
// G_OR when +custom-logic is enabled. Stops at RegBankSelect in this lab.
def G_AARCH64_CUSTOM_LOGIC : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type0:$src1, type0:$src2);
  let hasSideEffects = 0;
  let isCommutable = 1;
  let GISelMatchGenericTypes = 1;
}
```

几点说明：

- `type0` 让三个操作数同类型；本练习只允许 `s32`/`s64`，由 mapping case 再检查。
- `GISelMatchGenericTypes = 1` 与 `G_REV32`/`G_UZP1` 的写法一致，帮助 TableGen 生成匹配信息；它不替代 RegBankSelect 的 mapping。
- 这个 pseudo 是 post-legalizer 的；MIR fixture 直接以 `legalized: true` 写它，不需要 Legalizer 规则。
- 如果你想让 IR 路径自动生成它，再加一个 post-legalizer combine（见本节末尾的 guided 草图）。

### 3. Feature：定义 gate

文件：`llvm/lib/Target/AArch64/AArch64Features.td`，在合适位置加：

```tablegen
def FeatureCustomLogic : SubtargetFeature<"custom-logic", "HasCustomLogic",
  "true", "Enable the G_AARCH64_CUSTOM_LOGIC pseudo">;
```

`AArch64Subtarget.h` 里的 bool 成员由 `AArch64GenSubtargetInfo.inc` 的宏自动生成，不需要手写 `bool HasCustomLogic;`。命令行用 `-mattr=+custom-logic` 打开，`-mattr=-custom-logic` 关闭。

如果你只是做实验、不想动 feature 表，也可以用一个 `cl::opt<bool>`；但生产实现应使用 subtarget feature，这样 `-mattr`、target attribute、`-mcpu` 才能一致地控制它。

### 4. RegBankSelect：显式 case

文件：`llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp`，`getInstrMapping` 的 switch 里加：

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

为什么用 `getSameKindOfOperandsMapping`：

- 它检查 `NumOperands <= 3`，本 opcode 正好 3 个；
- 它根据 dst 类型决定 GPR/FPR：标量整数走 GPR；
- 它返回 `DefaultMappingID`、cost 1、三个同 bank 的 partial mapping；
- 它带断言，检查操作数类型/位宽一致。

为什么还要显式检查 feature 和标量：

- 只靠 fallback 也能得到 GPR，但 fallback 不会在 feature off 时拒绝，也不会拒绝向量/i128；
- 显式 case 让负向测试有确定的失败点；
- 以后加 FPR alternative 时，这个 case 是默认 mapping 的锚点。

### 5. MIR fixture：标量正向

```yaml
--- |
  target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i8:8:32-i16:16:32-i64:64-i128:128-n32:64-S128-Fn32"
  target triple = "aarch64-unknown-linux-gnu"
  define i64 @custom_logic(i64 %a, i64 %b) { ret i64 %a }
...
---
name:            custom_logic
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
    liveins: $x0, $x1

    %0(s64) = COPY $x0
    %1(s64) = COPY $x1
    %2(s64) = G_AARCH64_CUSTOM_LOGIC %0, %1
    $x0 = COPY %2(s64)
    RET_ReallyLR implicit $x0
...
```

命令（guided，需先重建带新 opcode 的 `llc`）：

```powershell
& $llc -mtriple=aarch64-linux-gnu -mattr=+custom-logic `
  -run-pass=regbankselect -verify-machineinstrs `
  practice-c\custom-logic.mir -o practice-c\custom-logic-rbs.mir
```

预期输出（guided）：

```yaml
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }

body:             |
  bb.0:
    liveins: $x0, $x1

    %0:gpr(s64) = COPY $x0
    %1:gpr(s64) = COPY $x1
    %2:gpr(s64) = G_AARCH64_CUSTOM_LOGIC %0, %1
    $x0 = COPY %2(s64)
    RET_ReallyLR implicit $x0
...
```

### 6. 负向测试：证明 case 真的生效

#### 6.1 向量输入

把 `%0/%1/%2` 改成 `<2 x s32>`，物理寄存器用 `$d0/$d1`：

```yaml
%0(<2 x s32>) = COPY $d0
%1(<2 x s32>) = COPY $d1
%2(<2 x s32>) = G_AARCH64_CUSTOM_LOGIC %0, %1
$d0 = COPY %2(<2 x s32>)
```

预期（guided）：RegBankSelect 报无法映射 / abort。原因：我们的 case 检查 `Ty.isScalar()`，向量直接 `getInvalidInstructionMapping()`。如果没有这个 case，type fallback 会把向量映射到 FPR，测试反而“通过”。这就是负向测试的价值。

#### 6.2 feature off

同一份标量 MIR，命令改成：

```powershell
& $llc -mtriple=aarch64-linux-gnu -mattr=-custom-logic `
  -run-pass=regbankselect -global-isel-abort=1 -verify-machineinstrs `
  practice-c\custom-logic.mir -o practice-c\custom-logic-nofeature.mir
```

预期（guided）：失败/abort。原因：case 命中后 `!STI.hasFeature(...)` 返回 invalid；没有 fallback 机会，因为 case 直接 return 了 invalid mapping。

#### 6.3 用 `-debug-only` 看 mapping ID（guided，需要 assertions build）

```bash
llc -mtriple=aarch64-linux-gnu -mattr=+custom-logic \
  -run-pass=regbankselect -debug-only=regbankselect \
  custom-logic.mir -o -
```

你应能看到这条指令的 `Best Mapping` 和 mapping ID。若只看到 fallback 的痕迹，说明 case 没命中或 feature 没打开。官方 Optimized 包没有 `-debug-only`，这一步必须自建。

### 7. 可选：让 IR 路径生成这个 opcode（guided）

如果你要让 `or i64` 在 `+custom-logic` 下自动变成 `G_AARCH64_CUSTOM_LOGIC`，需要在 post-legalizer combine 里做模式匹配并替换 opcode。草图：

```cpp
// 伪代码，位置：AArch64PostLegalizerLowering 或一个新的 post-legalizer combine
if (STI.hasFeature(AArch64::FeatureCustomLogic) &&
    MI.getOpcode() == TargetOpcode::G_OR) {
  LLT Ty = MRI.getType(MI.getOperand(0).getReg());
  if (Ty.isScalar() && (Ty.getSizeInBits() == 32 || Ty.getSizeInBits() == 64)) {
    MI.setDesc(TII.get(AArch64::G_AARCH64_CUSTOM_LOGIC));
    return true;
  }
}
```

这一步不属于 RegBankSelect，但它是 feature gate 的“生成侧”。生产实现应同时做生成侧 gate 和 mapping 侧防御；只做一侧，另一条路径就能绕过。

### 8. 为什么不用 Legalizer 规则

`G_AARCH64_CUSTOM_LOGIC` 设计成 post-legalizer pseudo：它在 Legalizer 之后才出现，所以 `legalized: true` 的 MIR 里出现它是合法的。你不需要在 `AArch64LegalizerInfo` 里为它写 `legalFor`。

如果你改成“从 IR 直接进入 Legalizer 的 generic opcode”，那就必须加 Legalizer 规则，否则 Legalizer 会报 unsupported。两条路线的取舍：

| 路线 | 优点 | 代价 |
| --- | --- | --- |
| post-legalizer pseudo（本练习） | 改动小，专注 RegBankSelect | 需要 combine 才能从 IR 生成 |
| legalizer 可见的 generic opcode | IR 路径直接 | 要写 legality/类型规则，影响面大 |

对于“学习 RegBankSelect”这个目标，post-legalizer pseudo 是更小的实验面。

</details>

## 测试用例

| 用例 | 输入 | 命令 | 期望 | 证据级别 |
| --- | --- | --- | --- | --- |
| 标量 `s64` 正向 | `custom-logic.mir` | `-mattr=+custom-logic -run-pass=regbankselect` | `%2:gpr(s64)`，无跨 bank COPY | guided |
| 标量 `s32` 正向 | `$w0/$w1` 版本 | 同上 | `%2:gpr(s32)` | guided |
| 向量负向 | `<2 x s32>` 版本 | 同上 | 无法映射/abort | guided |
| feature off 负向 | 标量 MIR | `-mattr=-custom-logic` | 无法映射/abort | guided |
| fallback 对照 | `G_ADD_LOW` MIR | 默认 `-run-pass` | `%1:gpr(s64)` | 实测 |
| verifier | 正向 MIR | 加 `-verify-machineinstrs` | 退出码 0 | guided |
| debug 观察 | 正向 MIR | `-debug-only=regbankselect` | 看到 case 的 mapping ID | guided（需 assertions build） |

## 延伸挑战

1. **给 `G_AARCH64_CUSTOM_LOGIC` 加 FPR alternative**：这是 Final Project 的核心。先在 `getInstrAlternativeMappings` 里返回 GPR/FPR 两套 mapping，再比较 Fast/Greedy。练习 C 只做默认 mapping，不要提前把 alternative 混进来。
2. **加一个真正的 Legalizer 规则**：把 opcode 改成 legalizer 可见，写 `legalFor({s32, s64})`，观察 `-stop-after=legalizer` 的 MIR 是否保留它。注意这会影响 IR 路径和 legalizer 的 fallback。
3. **把 feature 换成 `cl::opt`**：用一个命令行 flag 控制 mapping，比较 `-mattr` 与 `cl::opt` 在测试中的差异。生产代码不应依赖全局 flag 做 per-function 决策。
4. **测 i128**：把标量检查放宽到 128 位，观察 fallback 会把 i128 映射到 FPR（因为 `Ty.getSizeInBits() > 64`）。这解释为什么“只按类型兜底”对宽整数是危险的；Final Project 的可选 PAIR bank 会回到这个问题。
5. **写一个 lit 测试（guided）**：用 `update_mir_test_checks.py` 生成 CHECK，把 `-mattr=+custom-logic` 和 `-mattr=-custom-logic` 两条 RUN 都写进去；官方包没有 FileCheck/llvm-lit，需自建。

## 延伸阅读

- **[AArch64InstrGISel.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64InstrGISel.td)**
  - **读哪里**：`AArch64GenericInstruction`、`G_REV32`、`G_UZP1`、`G_ADD_LOW`。
  - **学什么**：target generic pseudo 的 TableGen 形状、`Namespace`、`GISelMatchGenericTypes`。
  - **和本练习的关联**：`G_AARCH64_CUSTOM_LOGIC` 的定义照这个模式写。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`getInstrMapping` 的入口、switch 末尾的类型兜底、`getSameKindOfOperandsMapping`。
  - **学什么**：显式 case 与 fallback 的先后顺序，为什么标量 target pseudo 会“碰巧”得到 GPR。
  - **和本练习的关联**：patch 就加在这个 switch 里。
- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - **读哪里**：`getInstrMappingImpl`、`getRegBankFromConstraints`、`isCopyLike`。
  - **学什么**：为什么没有 regclass 约束的 opcode 会返回 invalid，从而落到 target 的类型兜底。
  - **和本练习的关联**：`G_ADD_LOW` 的实测结果就是这条路径的产物。

## 自我检核

- [ ] 我能说出 `G_AARCH64_CUSTOM_LOGIC` 定义在哪个 `.td`，以及三个操作数为什么都用 `type0`
- [ ] 我能解释为什么“标量正向测试得到 gpr”不能证明显式 case 生效
- [ ] 我能设计一个 fallback 会做错、而我的 case 会拒绝的负向测试
- [ ] 我知道 feature gate 应该同时放在生成侧和 mapping 侧，为什么
- [ ] 我能解释 post-legalizer pseudo 与 legalizer 可见 opcode 的取舍
- [ ] 我知道 `getSameKindOfOperandsMapping` 对操作数数量和类型有什么要求
- [ ] 我能说出 `-mattr=+custom-logic` 与 `cl::opt` 在测试/生产中的差异
- [ ] 我知道本练习为什么不需要写 InstructionSelect

练习 C 到此结束。下一站开始 Part 3：先把 RegBankSelect 变成可观察对象——用 `-stop-after`、`-run-pass`、`-print-after` 和 verifier 把 bank 决策、COPY 插入与失败点逐层打印出来。

→ [Ch 16 观察 RegBankSelect：MIR 与 verifier](./16-observing-regbankselect.md)