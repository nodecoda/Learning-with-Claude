# Final Project：为 AArch64 增加一条带 GlobalISel 支持的 custom instruction

> **项目目标**：在一个可构建的 LLVM 23.1.4 源码树里，为 AArch64 增加一条受 feature gate 控制的 custom pseudo，让它从 `G_ADD` + `G_CONSTANT` 被 GlobalISel 选中，经过 pseudo expansion 变成真实的 `ADDXri`，并用 MIR/lit 测试覆盖开/关 feature、立即数边界和 fallback。
>
> **整合内容**：第 1–23 章的 pass 契约、gMIR、Legalizer/RegBankSelect、TableGen matcher、类型/bank/立即数/feature 谓词、pseudo expansion、MIR/lit、调试、fallback 与 review checklist。
>
> **环境**：LLVM 23.1.4，AArch64，CMake/Ninja。本文是 **guided project**：给出完整可复现的设计、diff 骨架、测试与验收标准；除非你本地真的构建了 LLVM，否则不要把示例命令的输出当成已经验证过的事实。

---

## 1. 项目规格

### 1.1 要做什么

新增一个 AArch64 codegen-only pseudo：

```text
ADDXri_Custom  Rd, Rn, #imm
```

语义与 `ADDXri` 相同，但满足以下条件：

1. 只在 subtarget 打开 `+custom-add` 时被 GlobalISel 选中；
2. 由 `G_ADD(GPR64, G_CONSTANT)` 形状匹配；
3. 立即数必须满足 `ADDXri` 的 12 位/左移 12 位编码约束；
4. 选中后通过 `PseudoInstExpansion` 展开成真正的 `ADDXri`；
5. feature 关闭时，仍然走原有的 `ADDXri` / `ADDXrr` 路径，行为不变；
6. 有 MIR 测试覆盖：feature on、feature off、立即数边界、无法编码的常量、以及一个 fallback/abort 场景。

### 1.2 为什么用 pseudo

直接新增一条真实编码指令需要：

- 分配 opcode；
- 定义 encoding；
- 更新 disassembler；
- 更新 MC 测试；
- 处理别名/汇编器/反汇编器一致性。

这些工作与本课的核心（InstructionSelect）关系不大，而且容易把项目变成“编码练习”。用 pseudo + expansion 可以：

- 保持编码层不变；
- 精确观察 GlobalISel 是否选中了 custom 指令；
- 在 `-stop-after=instruction-select` 的 MIR 里直接看到 pseudo；
- 在最终汇编里验证 expansion 正确。

这不是“偷懒”：pseudo 是 LLVM backend 里非常常见的分层手段。第 12 章已经讲过 `G_ADD_LOW`、`G_REV32` 这类 target-specific generic opcode；本项目用的是更靠近最终指令的 `Pseudo`。

### 1.3 非目标

本项目**不**要求：

- 新增真实机器编码；
- 修改 ABI 或调用约定；
- 支持向量/浮点；
- 支持所有立即数形态；
- 让 custom pseudo 进入 SelectionDAG 路径；
- 上游接受这条 pseudo（它是教学用 feature）。

---

## 2. 基线：先确认现有行为

在改任何代码之前，先建立基线。准备一个最小 IR：

```llvm
define i64 @add4095(i64 %x) {
  %r = add i64 %x, 4095
  ret i64 %r
}

define i64 @add4096(i64 %x) {
  %r = add i64 %x, 4096
  ret i64 %r
}

define i64 @add5000(i64 %x) {
  %r = add i64 %x, 5000
  ret i64 %r
}
```

跑：

```bash
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 \
    baseline.ll -o -
```

你应该看到（LLVM 23.1.4，AArch64）：

```asm
add4095:
        add     x0, x0, #4095
        ret
add4096:
        add     x0, x0, #1, lsl #12
        ret
add5000:
        mov     w8, #5000
        add     x0, x0, x8
        ret
```

再停在 InstructionSelect 之前：

```bash
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 \
    -stop-before=instruction-select baseline.ll -o baseline.mir
```

确认 `add4095` 的 gMIR 里有：

```mir
%1:gpr(s64) = G_CONSTANT i64 4095
%2:gpr(s64) = G_ADD %0, %1
```

如果基线不一致，先不要改代码。你的 LLVM 版本、triple、feature 或 legalizer 行为可能不同；本项目以 23.1.4 为准。

---

## 3. 设计：feature gate + pseudo + pattern

### 3.1 组件清单

| 组件 | 文件 | 作用 |
| --- | --- | --- |
| subtarget feature | `AArch64Features.td`、`AArch64Subtarget.h/.cpp` | 定义 `+custom-add` |
| pseudo 定义 | `AArch64InstrGISel.td` 或 `AArch64InstrInfo.td` | `ADDXri_Custom` 与 expansion |
| pattern | 同上 | `G_ADD` + `G_CONSTANT` 的 SelectionDAG pattern |
| matcher 生成 | `AArch64.td` 的 `GlobalISel` 配置 | 让 `-gen-global-isel` 导入 pattern |
| 测试 | `llvm/test/CodeGen/AArch64/GlobalISel/` | MIR/lit |
| 文档/注释 | 源码注释 | 说明教学用途与 feature 语义 |

### 3.2 feature 的定义

在 `AArch64Features.td` 里加一个 feature：

```tablegen
def FeatureCustomAdd : SubtargetFeature<"custom-add",
    "HasCustomAdd", "true",
    "Enable the custom ADDXri pseudo for GlobalISel testing">;
```

在 `AArch64Subtarget.h` 加字段：

```cpp
bool HasCustomAdd = false;
```

在 `AArch64Subtarget.cpp` 的构造函数/初始化里确保 TableGen 生成的 feature 解析能设置它（按你本地 23.1.4 的 `AArch64Subtarget` 生成方式调整；有些字段由 `AArch64GenSubtargetInfo.inc` 自动处理，不需要手写赋值）。

在 `AArch64.td` 的 processor/feature 列表里，把 `FeatureCustomAdd` 加入一个测试用 CPU 或允许 `-mattr=+custom-add` 直接开启。大多数情况下 `-mattr` 不需要 processor 定义；确认 `llc -mattr=help` 能看到它。

> **检查点 1**：`llc -mtriple=aarch64-linux-gnu -mattr=help | grep custom-add` 能看到 feature。看不到就说明 TableGen 或 subtarget 接线不对，不要继续写 pattern。

### 3.3 pseudo 的定义

在 `AArch64InstrGISel.td`（或 `AArch64InstrInfo.td` 的合适位置）定义：

```tablegen
let Predicates = [HasCustomAdd] in {
def ADDXri_Custom : Pseudo<
    (outs GPR64sp:$Rd),
    (ins GPR64sp:$Rn, addsub_imm12:$imm),
    [(set GPR64sp:$Rd, (add GPR64sp:$Rn, addsub_imm12:$imm))]>,
    Sched<[WriteALU]> {
  let isCodeGenOnly = 1;
  let hasSideEffects = 0;
  let PseudoInstExpansion = (ADDXri GPR64sp:$Rd, GPR64sp:$Rn, addsub_imm12:$imm);
}
}
```

要点：

- `Pseudo` 不产生真实编码；`PseudoInstExpansion` 在后续 pass 展开为 `ADDXri`。
- `Predicates = [HasCustomAdd]` 让 matcher 规则带 feature 位；feature 关闭时规则在 `GIM_Try_CheckFeatures` 就被拒绝。
- `addsub_imm12` 是 AArch64 现有的立即数操作数类型；复用它，避免自己发明谓词。
- `isCodeGenOnly = 1` 防止汇编器/反汇编器把它当作用户可见指令。
- `hasSideEffects = 0` 让调度与删除逻辑知道它是纯计算。

> 具体类名/操作数类型以 23.1.4 的 `AArch64InstrInfo.td` 为准。`addsub_imm12` 可能存在不同的命名或位于 `AArch64InstrFormats.td`；先用 `grep` 确认，不要照抄一个不存在的类型。

### 3.4 为什么 pattern 能导入

`(add GPR64sp:$Rn, addsub_imm12:$imm)` 是 SelectionDAG `add` 的 pattern。GlobalISel importer 通过：

```tablegen
def : GINodeEquiv<G_ADD, add>;
```

把 `G_ADD` 映射到 `add`，于是这条 pattern 可以被 `-gen-global-isel` 导入。`addsub_imm12` 的谓词会把立即数约束带进 matcher；如果它本身是 `ImmLeaf`/`PatLeaf` 的组合，确认 GlobalISel 能导入（第 5、7 章）。

### 3.5 matcher 会生成什么

概念上，`ADDXri_Custom` 的规则会生成：

```text
GIM_Try_CheckFeatures HasCustomAdd
GIM_CheckOpcode MIs[0], G_ADD
GIM_RootCheckType 1, s64
GIM_RootCheckType 2, s64
GIM_RecordInsn MIs[1], MIs[0], 2
GIM_CheckOpcode MIs[1], G_CONSTANT
GIM_CheckType MIs[1], 0, s64
GIM_CheckRegBankForClass MIs[1], 0, GPR64
GIM_CheckI64ImmPredicate MIs[1], addsub_imm12_pred
GIR_...
```

真实表里还会有 `CheckNumOperands`、`CheckIsSafeToFold`、bank/type 检查、`ConstrainOperandToRegClass` 等。关键是顺序：

1. feature 检查在规则入口；
2. opcode/结构检查；
3. 类型/bank；
4. 立即数谓词；
5. 渲染。

### 3.6 与内建 `ADDXri` 的竞争

AArch64 已经有 `ADDXri` 的 pattern。两条 pattern 形状相同，只有 feature gate 不同。matcher 的顺序可能让内建规则先匹配，因此：

- 必须用 `-mattr=+custom-add` 测试 custom 路径；
- 如果 custom 仍然不出现，检查 `AArch64GenGlobalISel.inc` 中两条规则的顺序，以及 feature 是否真的进入 `FeatureBitsets`；
- 必要时给 custom pattern 一个更具体的区分条件（例如只匹配某个 `PatFrag` 变体），但不要用“永远为真”的伪谓词掩盖顺序问题。

本项目故意保留这个竞争，因为它是一个很好的教学点：**pattern 的匹配不是“最后定义的赢”，而是 matcher 表顺序 + 谓词共同决定。**

---

## 4. 构建与生成

### 4.1 配置

在 LLVM 源码树外：

```bash
cmake -G Ninja -S llvm -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD=AArch64 \
  -DLLVM_ENABLE_PROJECTS="clang;lld"
ninja -C build llc llvm-tblgen llvm-lit
```

断言构建很重要：第 17 章讲过，官方预编译包是 Optimized build，`-debug-only=global-isel` 不可用。你自己构建时打开 assertions，才能看到 matcher 的 DEBUG 输出和内部断言。

### 4.2 重新生成

改 `.td` 后：

```bash
ninja -C build AArch64GenGlobalISel.inc
ninja -C build llc
```

如果只改 `.td` 而没重新生成，`llc` 仍然跑旧表——这是最常见的“为什么我的 pattern 没生效”之一。确认生成文件的时间戳晚于 `.td`。

### 4.3 检查生成物

```bash
grep -n "ADDXri_Custom" build/lib/Target/AArch64/AArch64GenGlobalISel.inc
```

你应该能在 matcher 表附近看到 custom opcode 的渲染；如果完全找不到，说明 pattern 没被 importer 接受。回到 `-gen-global-isel` 的 tblgen 日志，检查是否有“unsupported pattern”提示。

### 4.4 检查 feature 位

```bash
grep -n "custom-add\|HasCustomAdd\|FeatureCustomAdd" \
  build/lib/Target/AArch64/AArch64GenGlobalISel.inc
```

确认 feature 位集与 `GIM_Try_CheckFeatures` 的 ID 对得上。若 feature 没进表，`Predicates = [HasCustomAdd]` 可能写在了 importer 不读取的位置。

---

## 5. 测试设计

### 5.1 测试 1：feature 开启，选中 pseudo

`llvm/test/CodeGen/AArch64/GlobalISel/custom-add-pseudo.mir`：

```yaml
# RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 \
# RUN:     -mattr=+custom-add -O0 -run-pass=instruction-select %s -o - \
# RUN:   | FileCheck %s

--- |
  define i64 @add4095(i64 %x) {
    %r = add i64 %x, 4095
    ret i64 %r
  }
...
---
name:            add4095
alignment:       4
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: gpr, preferred-register: '' }
  - { id: 1, class: gpr, preferred-register: '' }
  - { id: 2, class: gpr, preferred-register: '' }
body:             |
  bb.1 (%ir-block.0):
    liveins: $x0

    %0:gpr(s64) = COPY $x0
    %1:gpr(s64) = G_CONSTANT i64 4095
    %2:gpr(s64) = G_ADD %0, %1
    $x0 = COPY %2(s64)
    RET_ReallyLR implicit $x0

# CHECK-LABEL: name: add4095
# CHECK: ADDXri_Custom
```

> MIR 的 `registers:` 和 `body:` 需要与你的 baseline MIR 完全一致；不要凭记忆手写寄存器 id。最稳的做法是从 `-stop-before=instruction-select` 生成，再删掉不需要的元数据。

### 5.2 测试 2：feature 关闭，不选 pseudo

```yaml
# RUN: llc ... -mattr=-custom-add ... | FileCheck %s
# CHECK-LABEL: name: add4095
# CHECK-NOT: ADDXri_Custom
# CHECK: ADDXri
```

`CHECK-NOT` 的作用域要放在 `CHECK-LABEL` 与下一个 `CHECK-LABEL` 之间，否则可能跨函数误判。

### 5.3 测试 3：立即数边界

对 `4095`、`4096` 各跑一次：

- `4095` 应选 custom pseudo（如果 pattern 接受），展开为 `add x0, x0, #4095`；
- `4096` 应展开为 `add x0, x0, #1, lsl #12`；
- `5000` 不应被 custom pattern 接受，最终仍是 `mov w8, #5000; add x0, x0, x8`。

用 IR→asm 测试更容易验证最终形态：

```yaml
# RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 \
# RUN:     -mattr=+custom-add -O0 %s -o - | FileCheck %s
# CHECK-LABEL: add4095:
# CHECK: add x0, x0, #4095
```

### 5.4 测试 4：fallback/abort

构造一个 custom pseudo 无法处理的形状，例如 `add i64 %x, %y`（非常量）：

```yaml
# RUN: not llc ... -global-isel-abort=1 ... 2>&1 | FileCheck %s
# CHECK: instruction selection failed
```

注意：`add i64 %x, %y` 本来就有 `ADDXrr` 路径，不会失败。要测试 custom 失败，应该用 `-mattr=+custom-add` 并暂时让 custom pattern 成为唯一候选，或者直接测试一个未支持的 `G_*`。更稳妥的做法是单独写一个“未支持 opcode”的 MIR，不要为了制造失败而破坏正常路径。

### 5.5 测试 5：pseudo expansion

`-stop-after=instruction-select` 应该看到 `ADDXri_Custom`；完整 `-O0` 汇编应该看到 `add`，而不是 `ADDXri_Custom`。这说明 expansion 生效。

```bash
llc ... -stop-after=instruction-select custom-add.ll -o - | grep ADDXri_Custom
llc ... -O0 custom-add.ll -o - | grep -E 'add\s+x0'
```

如果最终汇编里出现 `ADDXri_Custom`，说明 pseudo 没有 expansion，或者你停在了错误的 pass。

---

## 6. 调试路径

按以下顺序排查，不要跳步：

### 6.1 feature 没生效

- `llc -mattr=help` 是否列出 `custom-add`？
- `-mattr=+custom-add -print-after=instruction-select` 的 MIR 里，`selected` 是否为 true？
- `AArch64GenGlobalISel.inc` 里是否有 feature 位？
- 是否忘了 `ninja AArch64GenGlobalISel.inc`？

### 6.2 pattern 没进表

- `llvm-tblgen -gen-global-isel` 是否报 unsupported pattern？
- `addsub_imm12` 是否被 importer 接受？
- `GINodeEquiv<G_ADD, add>` 是否存在？
- pattern 是否被 `Predicates` 包在正确的 `let` 块里？

### 6.3 选中了错误规则

- `-stop-after=instruction-select` 看实际选了什么；
- 检查 matcher 表顺序；
- 确认 custom pattern 的 feature 条件没有把内建规则也一起 gate；
- 用 `-mattr=+custom-add,-...` 组合排除其他 feature 干扰。

### 6.4 选中 pseudo 但汇编错

- `-stop-after=instruction-select` 的 pseudo operand 是否正确？
- `PseudoInstExpansion` 的 operand 顺序/类型是否与 `ADDXri` 一致？
- 是否缺少 `let isCodeGenOnly = 1` 导致汇编器/反汇编器行为异常？
- `-verify-machineinstrs` 是否报 operand/regclass 错误？

### 6.5 测试脆弱

- 不要 CHECK 整个 MIR；只 CHECK 关键指令与 operand。
- 用 `CHECK-LABEL` 限定函数。
- 对汇编使用 `CHECK-NEXT` 时，注意注释/对齐伪指令。
- 用 `update_llc_test_checks.py` 生成初稿，但必须人工审查；它会掩盖你不想断言的细节。

---

## 7. 设计复盘：这个项目整合了什么

| 课程概念 | 在项目中的体现 |
| --- | --- |
| pass 契约（Ch2） | MIR 的 `legalized/regBankSelected/selected` |
| gMIR（Ch3） | `G_ADD`、`G_CONSTANT`、`COPY` |
| Legalizer（Ch4） | 常量保持 `s64`，不提升 |
| RegBankSelect（Ch4） | `gpr` bank 的 `%0/%1/%2` |
| TableGen 导入（Ch5） | `GINodeEquiv<G_ADD, add>` |
| matcher 执行（Ch6） | `GIM_Check*` / `GIR_*` |
| 类型/bank/立即数（Ch7） | `s64`、GPR、`addsub_imm12` |
| feature/内存/fold（Ch8） | `HasCustomAdd`、无 MMO、常量 fold |
| 嵌套匹配（Ch9） | `GIM_RecordInsn` 拉入 `G_CONSTANT` |
| 失败/fallback（Ch10） | `-global-isel-abort=1`、feature off |
| ComplexPattern（Ch11） | 本项目的立即数是简单谓词，不需要 ComplexPattern |
| pseudo/GINodeEquiv（Ch12） | `ADDXri_Custom` + `PseudoInstExpansion` |
| C++ selector（Ch13） | 本项目优先 pattern；若自定义条件无法表达，再考虑 C++ |
| 多输出/内存/atomic（Ch14） | 本项目无 MMO/多输出；可作为延伸 |
| 向量/intrinsic（Ch15） | 非目标；可作为延伸 |
| MIR/lit（Ch16） | 5 组测试 |
| 调试（Ch17） | `-stop-after`、`-print-after`、assertions |
| 常见 bug（Ch18） | 6.1–6.5 诊断路径 |
| 质量/上游（Ch19） | feature gate、测试、review checklist |
| AArch64 源码（Ch20） | `AArch64InstructionSelector`、`.td`、生成物 |
| 端到端（Ch21） | 本项目本身 |
| RISC-V 对照（Ch22） | 思考同一 pseudo 在 RISC-V 上如何实现 |
| 设计空间（Ch23） | 为什么选 pseudo 而不是真实编码/C++ |

---

## 8. 验收标准

### 8.1 必须通过

- [ ] 基线 `add4095/4096/5000` 与本项目开始前一致。
- [ ] `-mattr=+custom-add` 时，`-stop-after=instruction-select` 能看到 `ADDXri_Custom`。
- [ ] `-mattr=-custom-add` 时，不出现 `ADDXri_Custom`。
- [ ] 最终汇编中 pseudo 已展开为 `ADDXri`。
- [ ] `4095`、`4096` 的边界行为正确；`5000` 不被 custom pattern 误选。
- [ ] `-verify-machineinstrs` 无错误。
- [ ] 至少 4 个 lit 测试（on/off/boundary/expansion），全部通过。
- [ ] 没有修改无关文件；diff 可读、可回滚。

### 8.2 加分项

- [ ] 用 `-debug-only=global-isel`（assertions build）展示 matcher 命中轨迹。
- [ ] 统计 feature on/off 的 matcher 表大小差异。
- [ ] 给 custom pseudo 加一个 `GISelPredicateCode`，只匹配特定的 `MachineInstr` flag。
- [ ] 在 RISC-V 上实现一个等价的最小 pseudo，比较两边的工作量。
- [ ] 写一段 benchmark，比较 feature on/off 的编译时间。

### 8.3 评分 rubric

| 维度 | 权重 | 说明 |
| --- | --- | --- |
| 正确性 | 30% | 语义、边界、feature off 行为 |
| 测试 | 25% | MIR/lit 覆盖、CHECK 稳定性 |
| 设计 | 20% | 为什么用 pseudo、pattern 是否最小 |
| 调试 | 15% | 能否解释每次失败的原因 |
| 质量 | 10% | diff、注释、无关改动、review 可读性 |

---

## 9. 参考实现骨架

<details>
<summary>展开：feature + pseudo + pattern 的参考 diff 骨架</summary>

```diff
diff --git a/llvm/lib/Target/AArch64/AArch64Features.td b/llvm/lib/Target/AArch64/AArch64Features.td
@@
+def FeatureCustomAdd : SubtargetFeature<"custom-add",
+    "HasCustomAdd", "true",
+    "Enable the custom ADDXri pseudo for GlobalISel testing">;
```

```diff
diff --git a/llvm/lib/Target/AArch64/AArch64Subtarget.h b/llvm/lib/Target/AArch64/AArch64Subtarget.h
@@
+  bool HasCustomAdd = false;
```

```diff
diff --git a/llvm/lib/Target/AArch64/AArch64InstrGISel.td b/llvm/lib/Target/AArch64/AArch64InstrGISel.td
@@
+let Predicates = [HasCustomAdd] in {
+def ADDXri_Custom : Pseudo<
+    (outs GPR64sp:$Rd),
+    (ins GPR64sp:$Rn, addsub_imm12:$imm),
+    [(set GPR64sp:$Rd, (add GPR64sp:$Rn, addsub_imm12:$imm))]>,
+    Sched<[WriteALU]> {
+  let isCodeGenOnly = 1;
+  let hasSideEffects = 0;
+  let PseudoInstExpansion = (ADDXri GPR64sp:$Rd, GPR64sp:$Rn, addsub_imm12:$imm);
+}
+}
```

```diff
diff --git a/llvm/test/CodeGen/AArch64/GlobalISel/custom-add-pseudo.mir b/llvm/test/CodeGen/AArch64/GlobalISel/custom-add-pseudo.mir
new file mode 100644
@@
+# RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 \
+# RUN:     -mattr=+custom-add -O0 -run-pass=instruction-select %s -o - \
+# RUN:   | FileCheck %s
+--- |
+  define i64 @add4095(i64 %x) {
+    %r = add i64 %x, 4095
+    ret i64 %r
+  }
+...
+---
+name:            add4095
+alignment:       4
+legalized:       true
+regBankSelected: true
+selected:        false
+tracksRegLiveness: true
+body:             |
+  bb.1 (%ir-block.0):
+    liveins: $x0
+    %0:gpr(s64) = COPY $x0
+    %1:gpr(s64) = G_CONSTANT i64 4095
+    %2:gpr(s64) = G_ADD %0, %1
+    $x0 = COPY %2(s64)
+    RET_ReallyLR implicit $x0
+
+# CHECK-LABEL: name: add4095
+# CHECK: ADDXri_Custom
```

> 这个骨架**不保证**在任意 23.1.4 checkout 上直接编译：`addsub_imm12` 的实际名称、`Pseudo` 的模板参数、`PseudoInstExpansion` 的语法、feature 字段的生成方式都可能因源码分支而异。先用 `grep` 找同类定义，再按本地源码调整。参考实现的价值是展示结构，不是替代你读源码。

</details>

---

## 10. 常见失败与修复

### 失败 1：`llc` 说 unknown feature `custom-add`

- `AArch64Features.td` 没被包含；
- `AArch64.td` 没有把 feature 加入 `ProcessorModel`/`FeatureSet`；
- 没有重新生成 `AArch64GenSubtargetInfo.inc`；
- `-mattr=help` 看不到时，先修 feature 接线，不要写 pattern。

### 失败 2：pattern 生成了，但 matcher 里没有

- `-gen-global-isel` 跳过了 pattern（不支持的 SDNode/谓词）；
- `GINodeEquiv<G_ADD, add>` 缺失；
- `Predicates` 的 feature 没进 importer；
- `.td` 没被 `AArch64.td` 的 `GlobalISel` 配置包含。

### 失败 3：feature on 仍然选内建 `ADDXri`

- 两条 pattern 形状相同，内建规则先匹配；
- custom pattern 的 feature 没有真正进入 `FeatureBitsets`；
- matcher 表里 custom 规则被放在不可达分支；
- 用 `-print-after=instruction-select` 确认实际选中的 opcode，再调整顺序/区分条件。

### 失败 4：pseudo 泄漏到汇编

- `PseudoInstExpansion` 写错；
- 停在 `-stop-after=instruction-select`（此时本来就该看到 pseudo）；
- `isCodeGenOnly` 缺失导致汇编器把 pseudo 当真实指令；
- expansion 的 operand 类型与 `ADDXri` 不匹配。

### 失败 5：`4096` 没走 custom pseudo

- `addsub_imm12` 的谓词不接受“12 位值 + 左移 12”；
- custom pattern 只接受 `ImmLeaf` 的符号扩展形式；
- 需要 `IntImmLeaf` 或更宽的谓词；
- 先对比内建 `ADDXri` 的 pattern，不要自己发明编码规则。

---

## 11. 提交前的 review checklist

- [ ] feature 默认关闭，不影响现有代码；
- [ ] pseudo 只在 feature on 时被选中；
- [ ] 没有改变 `ADDXri`/`ADDXrr` 的原有行为；
- [ ] 测试覆盖 feature on/off、边界、expansion；
- [ ] MIR 测试的 `registers:`/`body:` 来自真实 baseline；
- [ ] CHECK 行不依赖无关寄存器编号/注释；
- [ ] `-verify-machineinstrs` 通过；
- [ ] diff 中没有调试残留、无关格式化；
- [ ] 注释解释“这是教学 pseudo”，避免被误认为生产指令；
- [ ] 如果提交上游，先讨论是否值得新增 feature，而不是直接发 PR。

---

## 12. 延伸挑战

1. **换成真实编码**：把 pseudo 换成一条真实指令，补 encoding/disassembler/MC 测试。比较工作量。
2. **加 C++ 谓词**：只对带 `MachineInstr::Disjoint` 的 `G_OR` 选 custom pseudo，观察 `GISelPredicateCode` 的作用域。
3. **加 ComplexPattern**：把 `addsub_imm12` 换成一个自定义寻址模式，走第 11 章的迁移流程。
4. **RISC-V 对照**：在 RISC-V 上实现 `ADDI_Custom`，比较两边的 Legalizer/RegBank/pattern 差异。
5. **Compile-time**：统计 feature on/off 时 `AArch64GenGlobalISel.inc` 的大小与 `llc -time-passes` 的差异。

---

## 13. 项目总结

这个项目最重要的不是“新增了一条 pseudo”，而是完整走了一遍：

```text
spec → 设计选择 → feature gate → pattern → matcher table
     → MIR 测试 → 调试 → expansion → review
```

如果你能独立解释每一步为什么这样做、失败时该看哪一层，那么你已经具备在真实 backend 里补 GlobalISel 支持的基本能力。下一步是选一个真实的 target 缺口，按第 19 章的 review 标准提交一个小而完整的 patch。

---

## 14. 延伸阅读

- **[LLVM 文档：InstructionSelect](https://llvm.org/docs/GlobalISel/InstructionSelect.html)** — `select()`、`selectImpl()`、pattern 导入的总览。项目中的 pseudo 选择与 fallback 都以这里为准。
- **[LLVM 文档：Porting GlobalISel to a New Target](https://llvm.org/docs/GlobalISel/Porting.html)** — 新 target 需要补的组件清单。把本项目的 feature/pseudo/pattern 放回这个清单里理解。
- **[LLVM 源码：`AArch64InstrGISel.td`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64InstrGISel.td)** — 真实的 AArch64 GI pseudo、`GINodeEquiv`、atomic pattern。写 pseudo 前先读这里的同类定义。
- **[LLVM 源码：`AArch64InstrInfo.td`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64InstrInfo.td)** — `ADDXri`、`addsub_imm12`、`HasFullFP16` 等真实定义。项目的 pattern 必须与这里的类型/谓词对齐。
- **[LLVM 源码：`AArch64InstructionSelector.cpp`](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp)** — 当 pattern 表达不了时，C++ selector 的落点。读 `select()` 与 `selectImpl()` 的分工。

---

## 完成之后

回到 [课程首页](./README.md)，把本项目的测试结果、踩过的坑和最终 diff 记在你的学习笔记里。如果你完成了真实构建，建议再跑一遍第 22 章的 RISC-V 对照，看看同一个设计在另一个 target 上会卡在哪一层。
