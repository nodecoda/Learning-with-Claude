# 练习 B — 改一个 mapping，比较 Fast/Greedy

> **目标**：先测出 `G_OR` 在 Fast/Greedy 下的真实 COPY 数，再用一个受控的源码改动（改 alternative 的 `Cost`、删 alternative、改 `copyCost`）预测行为变化。练的是“mapping 表 + 代价模型”如何共同决定 bank，而不是背 Fast/Greedy 的口诀。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（baseline MIR 实测）。修改 `AArch64RegisterBankInfo.cpp` 后重编译 `llc` 的部分标 guided，不伪造改后输出。
>
> **证据约定**：`实测` 是官方包上跑出的 MIR；`源码依据` 是 23.1.4 源码里的 mapping 表与代价公式；`guided` 是需要自建/未执行的改动，文中给出预测和验证方法。

## 背景与动机

Ch 0 已经看到一个现象：同一个 `or i32`，Fast 把 `G_OR` 放在 GPR，插 3 条跨 bank COPY；Greedy 把它留在 FPR，0 条。很多人到这里会背结论：“Greedy 更聪明，会把 bitcast 链留在 FPR。”但这句话既不完整，也不可迁移：

- 如果 `G_OR` 的 FPR alternative 被删掉，Greedy 还能留在 FPR 吗？
- 如果把 FPR alternative 的 `Cost` 从 1 改成 16，Greedy 会怎么选？
- 如果只改 `copyCost` 的不对称值（GPR→FPR=4、FPR→GPR=5），结果会翻转吗？
- 为什么 Fast 明明也调用 `computeMapping`，却不会选 FPR？

这些问题只有把 **mapping 表**和**代价模型**分开看才能回答。练习 B 的做法是：先锁定一个可复现的 baseline，再只改一个旋钮，预测输出，最后用 MIR 和 COPY 数验证。你不需要一次改对所有东西；你需要知道每个旋钮影响哪一项代价。

## 先建立直觉

把 RegBankSelect 想成两个柜台：

```text
                      ┌─────────────────────────────┐
   legal gMIR ───────▶│  柜台 1：mapping 表          │
   %2:fpr(i32)        │  G_OR 有没有 FPR 版本？      │
   %3:fpr(i32)        │  默认版本是谁？              │
                      └──────────────┬──────────────┘
                                     │ 候选 mapping
                                     ▼
                      ┌─────────────────────────────┐
                      │  柜台 2：代价模型            │
                      │  指令代价 + 操作数搬运代价   │
                      │  + 非局部/边修复代价         │
                      └──────────────┬──────────────┘
                                     │ 选最小
                                     ▼
                      ┌─────────────────────────────┐
                      │  applyMapping：插 COPY       │
                      └─────────────────────────────┘
```

Fast 和 Greedy 的差别不在“柜台 1”，而在“柜台 2 用不用”：

- **Fast**：只取 `getInstrMapping` 的默认 mapping，然后仍然用 `computeMapping` 算它可不可实现；它不比较 alternative。
- **Greedy**：取 `getInstrPossibleMappings`（默认 mapping + alternatives），对每个候选算 `MappingCost`，选最小的。

所以，改 alternative 的 `Cost` 只会影响 Greedy；改默认 mapping 会同时影响 Fast 和 Greedy；改 `copyCost` 会影响所有需要 repair 的候选。

## 任务规格

### 输入

使用 Ch 0 的 `or.ll`：

```llvm
define float @or_f(float %a, float %b) {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
```

baseline 命令：

```powershell
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"

# Fast（默认）
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -stop-after=regbankselect `
  practice-b\or.ll -o practice-b\or-fast.mir

# Greedy
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  practice-b\or.ll -o practice-b\or-greedy.mir
```

然后从 legalized MIR 再跑一次，确认 `-run-pass` 与 `-stop-after` 的结果一致：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -stop-after=legalizer `
  practice-b\or.ll -o practice-b\or-legal.mir

& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -verify-machineinstrs practice-b\or-legal.mir -o practice-b\or-legal-rbs.mir
```

### 输出

你需要交出：

1. Fast/Greedy 两份 `G_OR` 附近的 MIR，标出每条跨 bank COPY 的方向。
2. 一张代价表：指令代价、操作数修复代价、后续返回 COPY 代价、总计。
3. 一组 patch 预测：至少 3 个变体，每个变体说明“改了哪一行、Fast 是否变、Greedy 是否变、为什么”。
4. 一段结论：在什么条件下 Greedy 会退回 GPR，在什么条件下改 `copyCost` 不会改变选择。

### 约束

- 一次只改一个旋钮；不要同时改 alternative cost 和 `copyCost`，否则无法归因。
- 不要改默认 `G_OR` mapping 来“让 Greedy 选 FPR”；那会连 Fast 一起改，实验失去对照。
- 改源码后的输出必须标 guided；官方包不能重编译，不要把预测写成实测。
- COPY 计数只数 **vreg 与 vreg 之间、bank 不同** 的 repair；ABI 边界 `$s0`/`$s1` 的 COPY 单独列出。

### 验收标准

- Fast 的 `G_OR` 在 GPR，跨 bank COPY 为 3（2 条操作数 repair + 1 条返回路径）；纯 vreg↔vreg repair 为 2。
- Greedy 的 `G_OR` 在 FPR，跨 bank repair COPY 为 0。
- 能算出 Fast 链的代价 15（1 + 5 + 5 + 4），Greedy 链的代价 1。
- 能预测 FPR alternative `Cost` 改成 16 后 Greedy 退回 GPR。
- 能解释为什么把 `copyCost` 从 5/4 改成 7/6 仍然不会翻转 `or.ll`。

## 期望输出

### baseline 实测

Fast（实测）：

```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %6:gpr(i32) = COPY %2(i32)
    %7:gpr(i32) = COPY %3(i32)
    %4:gpr(i32) = G_OR %6, %7
    %5:gpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
...
```

Greedy（实测）：

```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %4:fpr(i32) = G_OR %2, %3
    %5:fpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
...
```

### COPY 计数

| 模式 | `G_OR` bank | 跨 bank COPY（含返回） | ABI 边界 COPY | 说明 |
| --- | --- | --- | --- | --- |
| Fast | `gpr` | 3：`%6`、`%7` 是 FPR→GPR，`%5` 到 `$s0` 是 GPR→FPR | 2：`$s0`、`$s1` 进参 | `G_OR` 用默认 GPR mapping |
| Greedy | `fpr` | 0 | 2 | FPR alternative 胜出 |

严格说，`%5` 到 `$s0` 是 vreg→物理寄存器，不是 vreg↔vreg。把它算进“跨 bank 搬运”是因为它确实是一条 GPR→FPR 的 `COPY`，代价模型也会为它付钱；但报告时最好分开写，避免和 repair 混淆。

### 代价表

| 方案 | 指令代价 | 操作数修复 | 返回路径 | 总计 | 谁选它 |
| --- | --- | --- | --- | --- | --- |
| Fast：`G_OR` GPR | 1 | 5 + 5 = 10 | `$s0 = COPY %5`：GPR→FPR = 4 | 15 | Fast 固定默认 |
| Greedy：`G_OR` FPR | 1 | 0 | `$s0 = COPY %5`：FPR→FPR = 0 | 1 | Greedy 最小 |
| Greedy 的 GPR alternative | 1 | 10 | 4 | 15 | 输给 FPR |
| patch：FPR `Cost=16` | 16 | 0 | 0 | 16 | 输给 GPR 的 15 |

注意 `copyCost(A, B)` 的定义是“`A = COPY B` 的代价”，不是“从 A 到 B”。AArch64 的实现：

```cpp
// AArch64RegisterBankInfo.cpp（23.1.4，copyCost）
if (&A == &AArch64::GPRRegBank && &B == &AArch64::FPRRegBank)
  return 5;   // GPR = COPY FPR，FMOVXDr/FMOVWSr
if (&A == &AArch64::FPRRegBank && &B == &AArch64::GPRRegBank)
  return 4;   // FPR = COPY GPR，FMOVDXr/FMOVSWr
return RegisterBankInfo::copyCost(A, B, Size);
```

所以：

- `%6:gpr = COPY %2:fpr` 是 `copyCost(GPR, FPR)` = 5；
- `$s0 = COPY %5:gpr` 是 `copyCost(FPR, GPR)` = 4。

把方向写反，代价表就会算成 5+5+5=15 或 4+4+4=12，结论会漂移。练习时先在纸上写 `Dst = COPY Src`，再套 `copyCost(DstBank, SrcBank)`。

## 如果你卡住了

1. **把 `$s0 = COPY %5` 当成 ABI COPY，不算代价**：它确实是 ABI 边界，但它的 bank 由 `%5` 决定。`%5` 在 GPR 时，这条 COPY 是 GPR→FPR，代价 4；`%5` 在 FPR 时，它是 FPR→FPR，代价 0。ABI 边界不免费，只是它不由 `G_OR` 的 mapping 直接插入。
2. **只数 vreg↔vreg COPY，漏掉返回路径**：Fast 的 3 条里，有 2 条是操作数修复，1 条是返回路径。少算一条，就会得出 Fast 代价 11、Greedy 1，仍然选 FPR，但阈值会算错（会以为 alternative cost 改成 11 就翻转）。
3. **把 `G_OR` 的 GPR alternative 当成“另一个候选”**：默认 mapping 已经是 GPR；`getInstrAlternativeMappings` 又 push 了一个 GPR mapping（ID 1）。它是重复候选，不会让 GPR 更便宜，也不会改变 Fast。
4. **以为 Fast 不算代价**：Fast 也调用 `computeMapping`，只是不比较 alternatives；如果默认 mapping 的 repair 不可能（`ImpossibleCost`），Fast 也会失败。区别是“只算一个”还是“算多个选最小”。
5. **改完源码不重建就宣称结果**：`llc` 是预编译二进制，改 `.cpp` 不会影响它。必须重新构建 `llc`（guided），或者用 `-debug-only` 观察候选（需要 assertions build）。不要把预测写成实测。

## 实作步骤建议

1. **锁定 baseline**：跑 Fast/Greedy，保存两份 MIR；用 `Select-String` 或编辑器确认 `G_OR` 的 bank 和 COPY 行。先不碰源码。
2. **手算代价**：在纸上写 `G_OR` 默认 mapping 的 1，两个操作数各 5，返回 COPY 4，总计 15；FPR mapping 的 1。确认和输出一致。
3. **做第一个 patch**：把 `FPRMapping` 的 `Cost` 从 1 改成 16。重建 `llc`（guided），跑 Greedy，预测输出应和 Fast 相同（GPR，3 条跨 bank COPY）。
4. **做第二个 patch**：删掉 `AltMappings.push_back(&FPRMapping);`，重建，跑 Greedy。预测：Greedy 只剩 GPR 候选，输出与 Fast 相同；这证明“Greedy 的 FPR 能力来自 alternative，不是它自己会造 mapping”。
5. **做第三个 patch（可选）**：把 `copyCost` 的 GPR→FPR 改成 7、FPR→GPR 改成 6，重建，跑 Greedy。预测：GPR 总计 1+7+7+6=21，FPR 仍为 1，选择不变；说明只改 copy 代价、不改 alternative 代价，不一定能翻转。

## 完整参考解答

<details>
<summary>展开：源码位置、patch 草图、预测表与验证方法</summary>

### 1. baseline 的源码依据

`G_OR` 的 alternative mappings 在 `AArch64RegisterBankInfo::getInstrAlternativeMappings`（23.1.4）：

```cpp
case TargetOpcode::G_OR: {
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

默认 mapping 来自 `getInstrMapping` 的 `G_OR` case，走 `getSameKindOfOperandsMapping`，对非向量、非浮点 opcode 选 GPR：

```cpp
case TargetOpcode::G_AND:
case TargetOpcode::G_OR:
case TargetOpcode::G_XOR:
  return getSameKindOfOperandsMapping(MI);
```

`getSameKindOfOperandsMapping` 里：

```cpp
bool IsFPR = Ty.isVector() || isPreISelGenericFloatingPointOpcode(Opc);
PartialMappingIdx RBIdx = IsFPR ? PMI_FirstFPR : PMI_FirstGPR;
return getInstructionMapping(DefaultMappingID, 1,
                             getValueMapping(RBIdx, Size), NumOperands);
```

所以 Fast 的默认是 GPR、cost 1；Greedy 的候选是“默认 GPR + alternative GPR + alternative FPR”，其中 FPR cost 1。

### 2. Greedy 为什么选 FPR

`RegisterBankInfo::getInstrPossibleMappings` 先放默认 mapping，再追加 alternatives：

```cpp
PossibleMappings.push_back(&Mapping);          // 默认
InstructionMappings AltMappings = getInstrAlternativeMappings(MI);
append_range(PossibleMappings, AltMappings);   // alternatives
```

`RegBankSelect::findBestMapping` 对每个候选调用 `computeMapping`，只有严格更小才替换：

```cpp
for (const InstructionMapping *CurMapping : PossibleMappings) {
  MappingCost CurCost = computeMapping(MI, *CurMapping, LocalRepairPts, &Cost);
  if (CurCost < Cost) {
    Cost = CurCost;
    BestMapping = CurMapping;
    // ...
  }
}
```

因此：

- 默认 GPR：1 + 两个操作数 FPR→GPR（5+5）+ 返回 GPR→FPR（4）= 15；
- FPR alternative：1 + 0 + 0 = 1；
- 1 < 15，Greedy 选 FPR。

`computeMapping` 的局部代价只处理 `InstrMapping.getNumOperands()` 个操作数；对 `G_OR` 是 3 个（dst、src1、src2），所以两个源操作数的 repair 都被算进去。返回路径的 `$s0 = COPY %5` 是另一条 COPY 指令，单独计价。

### 3. patch 草图

#### 变体 A：把 FPR alternative 的 Cost 改成 16

文件：`llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp`，`getInstrAlternativeMappings` 的 `G_OR` case。

```diff
   const InstructionMapping &FPRMapping = getInstructionMapping(
-      /*ID*/ 2, /*Cost*/ 1, getValueMapping(PMI_FirstFPR, Size),
+      /*ID*/ 2, /*Cost*/ 16, getValueMapping(PMI_FirstFPR, Size),
       /*NumOperands*/ 3);
```

重建（guided）：

```bash
cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AArch64" \
  -DLLVM_CCACHE_BUILD=ON \
  -DLLVM_USE_LINKER=lld
ninja -C build llc
```

预测（guided，未执行）：

- Fast：默认 GPR，不变；3 条跨 bank COPY。
- Greedy：FPR 候选总代价 16，GPR 候选 15；`16 < 15` 为假，GPR 先被选中，最终输出与 Fast 相同。
- 如果 FPR cost 改成 15：`15 < 15` 为假，仍然 GPR；这是“平局保留先出现候选”的细节。
- 如果 FPR cost 改成 14：`14 < 15` 为真，Greedy 仍选 FPR，输出与 baseline Greedy 相同。

#### 变体 B：删掉 FPR alternative

```diff
   AltMappings.push_back(&GPRMapping);
-  AltMappings.push_back(&FPRMapping);
   return AltMappings;
```

预测（guided）：

- 默认 GPR 仍然存在；alternative 只剩 GPR。
- Greedy 没有 FPR 候选，输出与 Fast 相同。
- 这个变体证明：Greedy 不会“自己造”一个 FPR mapping；它只在 target 提供的候选里选。

#### 变体 C：只改 copyCost 的不对称

```diff
 unsigned AArch64RegisterBankInfo::copyCost(const RegisterBank &A,
                                            const RegisterBank &B,
                                            const TypeSize Size) const {
   if (&A == &AArch64::GPRRegBank && &B == &AArch64::FPRRegBank)
-    return 5;
+    return 7;
   if (&A == &AArch64::FPRRegBank && &B == &AArch64::GPRRegBank)
-    return 4;
+    return 6;
   return RegisterBankInfo::copyCost(A, B, Size);
 }
```

预测（guided）：

- Fast 的 GPR 链：1 + 7 + 7 + 6 = 21；
- Greedy 的 FPR 链：1 + 0 + 0 = 1；
- 仍然选 FPR。
- 这说明：`or.ll` 的 FPR 优势太大（15 vs 1），只把 copy 代价从 5/4 调到 7/6 不足以翻转；要翻转，需要让 FPR alternative 本身变贵，或者让 GPR 链的 copy 代价超过 14 的差距。

#### 变体 D：改默认 mapping（不推荐）

如果把 `getSameKindOfOperandsMapping` 里 `G_OR` 的默认 bank 改成 FPR，Fast 也会选 FPR，baseline 对照就消失了。这个变体可以用来验证“默认 mapping 同时影响 Fast/Greedy”，但不适合作为“只改 alternative”的实验。

### 4. 预测汇总表

| 变体 | Fast 的 `G_OR` | Greedy 的 `G_OR` | Fast COPY | Greedy COPY | 结论 |
| --- | --- | --- | --- | --- | --- |
| baseline | GPR | FPR | 3 | 0 | alternative 生效 |
| A：FPR cost 16 | GPR | GPR | 3 | 3 | FPR 变贵后输给 GPR |
| A'：FPR cost 15 | GPR | GPR | 3 | 3 | 平局保留先出现候选 |
| A''：FPR cost 14 | GPR | FPR | 3 | 0 | 仍然 FPR |
| B：删 FPR alt | GPR | GPR | 3 | 3 | Greedy 只剩 GPR |
| C：copyCost 7/6 | GPR | FPR | 3 | 0 | FPR 优势仍大 |
| D：默认改 FPR | FPR | FPR | 0 | 0 | Fast 也被改掉 |

### 5. 怎么验证改后结果

如果你已经构建了 assertions 版本，最直接的是：

```bash
llc -mtriple=aarch64-linux-gnu -O2 -global-isel \
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect \
  -debug-only=regbankselect or.ll -o -
```

`-debug-only=regbankselect` 会打印每条指令的候选 mapping、`Evaluating mapping cost`、`New best` 和最终 `Best Mapping`。官方预编译包是 Optimized build，没有这个选项；不要把“没有输出”当成 pass 没跑。

没有 assertions build 时，用 MIR 对照：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  practice-b\or.ll -o practice-b\or-greedy-patched.mir
```

检查三件事：

1. `%4` 的 bank 是 `gpr` 还是 `fpr`；
2. `%6`/`%7` 两条 FPR→GPR COPY 是否存在；
3. `$s0 = COPY %5` 的源 vreg 是 GPR 还是 FPR。

### 6. 一个可提交的 MIR 回归测试

如果要给 upstream 提测试，不要只写“输出长这样”；要锁定决策点。示例（lit 形式，官方包没有 FileCheck，标 guided）：

```yaml
# RUN: llc -mtriple=aarch64-- -O2 -global-isel -global-isel-abort=1 \
# RUN:   -regbankselect-greedy -stop-after=regbankselect %s -o - | FileCheck %s

# CHECK-LABEL: name: or_f
# CHECK: %[[A:.*]]:fpr(i32) = G_BITCAST
# CHECK: %[[B:.*]]:fpr(i32) = G_BITCAST
# CHECK: %[[OR:.*]]:fpr(i32) = G_OR %[[A]], %[[B]]
```

这个测试锁定“Greedy 把 `G_OR` 留在 FPR”。如果哪天有人删掉 FPR alternative，CHECK 会失败，review 时能立刻看到行为变化。

</details>

## 测试用例

| 用例 | 输入/操作 | 期望 | 证据级别 |
| --- | --- | --- | --- |
| Fast baseline | `or.ll` + 默认 | `%4:gpr`，3 条跨 bank COPY | 实测 |
| Greedy baseline | `or.ll` + `-regbankselect-greedy` | `%4:fpr`，0 条跨 bank COPY | 实测 |
| 显式 Fast | `or.ll` + `-regbankselect-fast` | 与默认 Fast 逐字节相同（SHA-256 相同） | 实测 |
| `-run-pass` 等价 | legalized MIR + `-run-pass=regbankselect` | 与 `-stop-after` 的 bank 选择一致 | 实测 |
| FPR cost 16 | patch A + 重建 + Greedy | `%4:gpr`，3 条 COPY | guided |
| 删 FPR alt | patch B + 重建 + Greedy | `%4:gpr`，3 条 COPY | guided |
| copyCost 7/6 | patch C + 重建 + Greedy | `%4:fpr`，0 条 COPY | guided |
| 默认改 FPR | patch D + 重建 + Fast | `%4:fpr`，0 条 COPY | guided |
| 阈值边界 | FPR cost 15 | 平局，GPR 先出现，仍 GPR | guided |
| verifier | 所有 MIR + `-verify-machineinstrs` | 退出码 0 | 实测/guided 分别标注 |

## 延伸挑战

1. **把 `G_OR` 换成 `G_AND`**：AArch64 没有 `G_AND` 的 alternative。预测 Fast/Greedy 是否相同；如果相同，说明“没有 alternative 时 Greedy 退化成 Fast 的候选集”，而不是“Greedy 一定更好”。
2. **把 `or.ll` 改成 64 位**：`G_OR s64` 也在 alternative 覆盖范围内（`Size == 32 || Size == 64`）。预测 COPY 数是否仍为 3；注意 `copyCost` 对 64 位仍是 5/4，不随位宽变化。
3. **加入 `G_BITCAST` 的跨 bank alternative**：`G_BITCAST` 有 ID 3/4 的 cross-bank mappings，cost 5。构造一个 bitcast 两侧 bank 不同的例子，预测 Greedy 会不会选 cross-bank mapping。提示：这会把“一条 bitcast”变成“带代价的 copy”，不是免费的。
4. **只看局部代价的局限**：`G_OR` 在循环里，Greedy 仍然只按当前指令和操作数算局部代价；它不会因为循环整体更热就改变选择。构造一个循环里的 `or`，用 `-stop-after=regbankselect` 观察，思考为什么 `MappingCost` 有 block frequency 但 Greedy 仍不是全局最优。
5. **改 `G_LOAD` 的 alternative（guided）**：把 `G_LOAD` 的 FPR mapping cost 从 1 改成 3，找一个 load 后接浮点使用的例子，预测 Greedy 是否仍选 FPR。注意 `G_LOAD` 的默认 mapping 已经会根据 FP 使用选 FPR，改 alternative 不一定有可见效果；先读 `getInstrMapping` 的 `G_LOAD` case 再动手。

## 延伸阅读

- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`findBestMapping`、`computeMapping`、`getRepairCost`、`MappingCost::operator<`。
  - **学什么**：候选如何排序、局部/非局部代价如何相加、平局如何处理。
  - **和本练习的关联**：FPR cost 16 与 15 的阈值就来自这里的 `<` 比较。
- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - **读哪里**：`getInstrPossibleMappings`、`getInstrMappingImpl`、`copyCost` 的默认实现。
  - **学什么**：默认 mapping 与 alternative 的顺序、copy-like 特例、同 bank copy 为 0。
  - **和本练习的关联**：Greedy 的候选集不是它自己生成的，而是这里组装的。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`copyCost`、`getInstrAlternativeMappings` 的 `G_OR`/`G_BITCAST`/`G_LOAD`、`getSameKindOfOperandsMapping`。
  - **学什么**：AArch64 的 5/4 不对称代价、alternative 的 ID/cost/operand mapping。
  - **和本练习的关联**：patch A/B/C 都改在这个文件里。

## 自我检核

- [ ] 我能不看源码说出 Fast 和 Greedy 在候选集上的区别
- [ ] 我能算出 `or.ll` Fast 链的 15 和 Greedy 链的 1，并解释每一项
- [ ] 我能解释 `copyCost(A, B)` 的参数顺序，不会把 5 和 4 写反
- [ ] 我知道 FPR alternative cost 改成 15 时为什么仍是 GPR（平局规则）
- [ ] 我知道删掉 FPR alternative 后 Greedy 为什么退化成 Fast 的结果
- [ ] 我能区分“ABI 边界 COPY”和“repair COPY”，但在代价表里都不漏算
- [ ] 我能说出改默认 mapping 与改 alternative 的影响范围差异
- [ ] 我知道官方包没有 `-debug-only`，改源码后的结论必须标 guided

练习 B 到此结束。下一站回到 Part 2：PHI、循环与跨块 bank 一致性——为什么 PHI 的 mapping 只描述 def、incoming edge 上的 repair 如何影响循环、critical edge 为什么会逼出 split。

→ [Ch 11 PHI、循环与跨块 bank 一致性](./11-phi-and-control-flow.md)