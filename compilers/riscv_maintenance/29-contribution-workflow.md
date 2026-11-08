# Ch 29 — 上游贡献流程：从一个 [RISCV] 提交反推 PR

> **目标**：把"贡献流程"从口号变成可复制的动作。拿到任意一个 `[RISCV]` 提交，你能用 `git show` 在几分钟内说出：它属于哪一类、源码为什么只有几行、测试为什么有几百行、reviewer 会追问什么、要不要写 release note。

> **环境**：同 Ch 00。`TR=/home/ubuntu/llvm/trees/ct-instr-trunk`（分支 `analysis/riscv-maintenance`，HEAD `0fc5afa04c23`），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。本章对 `TR` 只做只读操作。

> **前置**：读完 [Ch 27 — lit 测试与 update 脚本](./27-lit-testing-and-update-scripts.md) 与 [Ch 28 — 调试 CodeGen](./28-debugging-codegen.md)。本章不教定位 bug，只教"修好之后怎么变成可合并的 PR"。

---

## 29.1 为什么"反推"比"背流程"有效

fork → branch → push → PR → merge 这张图对 X86、MLIR、lldb 都一样，没有信息量。决定补丁能否进上游的是 **diff 的形状**：改哪几个文件、源码几行、测试几行、有没有 release note。本树三个真实提交的形状差异极大：

| 提交 | 类型 | 源码 | 测试 | 其它 | 形状 |
|---|---|---|---|---|---|
| `a44f88533543` | bug fix | `RISCVFrameLowering.cpp` **+16/−3** | `shadow-stack-frame-opts.ll` +986/−20，新建 `-v.ll` +136 | 无 | 3 行逻辑，1122 行测试 |
| `885f35214738` | NFC | `RISCVBaseInfo.h` **+9/−2** | 无 | 无 | 纯结构改写，零测试 |
| `dc21109c01ad` | 新扩展 | 5 个后端文件 +49 | 6 个测试文件 +42 | `ReleaseNotes.md` +1、`RISCVUsage.md` +4 | 一行 note 收尾 |

```bash
$ cd $TR
$ git log --oneline --grep='\[RISCV\]' | wc -l                               # 10841
$ git log --oneline --grep='\[RISCV\]' -- llvm/lib/Target/RISCV | wc -l      # 7673
$ git log --oneline --since='6 months ago' -- llvm/lib/Target/RISCV | wc -l  # 817
```

6 个月 817 个提交，约每天 4.5 个。两个结论：**惯例被大量样本固化**；**`--grep` 有噪声**——10,841 个 `[RISCV]` 提交只有 7,673 个（70.8%）真的改 RISC-V 后端，其余 3,168 个是 BOLT / Clang / lld / compiler-rt：

```bash
$ git log --oneline --grep='\[RISCV\]' -30 | grep -E 'BOLT|Clang'
47f6a5343b6f [BOLT][RISCV][NFC] Add test for ELF header flags (#239235)
fae51e8f227c [Clang][RISCV] Add scalar widening zip intrinsics (#238758)
```

第一条纪律：**先用路径过滤，再读 diff**。

---

## 29.2 先建立直觉：一个 RISC-V 补丁的生命周期

```
 ①issue/复现    ②定位(Ch28)   ③最小修复    ④lit回归(Ch27)
┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│ 最小 .ll  │─▶│ 出错 pass │─▶│ .cpp/.td  │─▶│ .ll/.s 新 │
│ + llc 命令│  │ + 前后 MIR│  │ 改十几行  │  │ RUN/check │
└───────────┘  └───────────┘  └───────────┘  └───────────┘
  证据: 复现      证据: MIR 差    证据: diff      证据: lit PASS
═══════════════════════════════════════════════════════════▶
 ⑤PR 描述       ⑥review 往返  ⑦squash      ⑧note / revert
┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
│ Problem / │  │ negative  │  │ 1 commit  │  │ Notes 一行│
│ Root/Fix  │  │ test/ABI  │  │ = PR 正文 │  │ apply -R  │
└───────────┘  └───────────┘  └───────────┘  └───────────┘
  证据: PR 正文   证据: 线程     证据: log -1    证据: revert OK
```

每一步都必须留下**他人可复跑的产物**，否则在上游等于没做：

| 步骤 | 证据产物 | 检查方式 |
|---|---|---|
| ① 复现 | issue 里的最小 `.ll` / `llc` 命令 | 别人 clone 后跑出同样错误 |
| ② 定位 | 出错 pass 名 + 前后 MIR 差异 | `-print-after` / `-debug-only` |
| ③ 修复 | `git diff`（源码尽量 < 30 行） | 能一句话说清改了哪个不变量 |
| ④ 回归 | 新增/更新的 `.ll`、`.s`、`.mir` | `llvm-lit` 单文件 PASS |
| ⑤ 描述 + ⑥ review | PR 正文；回复线程 + 新 push 的 diff | 含 why/`Fixes #N`；每条意见有对应改动 |
| ⑦ merge | 单个 squash commit | `git log -1 --stat` |
| ⑧ 收尾 | release note 行 / revert commit | `grep -n RISCV ReleaseNotes.md` |

⑦ 有个硬约束，`llvm/docs/GitHub.md:133-137`：PR 标题与正文会成为最终 squash commit 的标题与正文，**PR 内部提交的信息全部丢弃**。你在 PR 里 push 的 17 个 "fix typo"、"address review" 都会消失——⑤ 必须单独当一步做。

---

## 29.3 解剖一：bug fix —— `a44f88533543`

```bash
$ git show -s --format='%H%n%s%n%n%b' a44f88533543
a44f88533543b79f664f73e50f17bccb14625018
[RISCV] Correctly restore libcall CSRs with SCS (#233338)

Fixes #233318

Before this change, we were restoring some callee-saved registers twice
when using save/restore libcalls with shadow call stack (SCS), and the
second restore was happening after the stack was deallocated using the
wrong register and offset calculation, which was causing lots of access
problems. ...

The problems were:
- We were using the entire list of Callee Saves rather than just the
  subset that would have been restored by the libcall. ...
- We were not using the correct offset calculation in eliminateFrameIndex.

Assisted-by: AI
Co-authored-by: Mitch Briles <mitchbriles@gmail.com>
```

```bash
$ git show --numstat --format='' a44f88533543
16	3	llvm/lib/Target/RISCV/RISCVFrameLowering.cpp
136	0	llvm/test/CodeGen/RISCV/shadow-stack-frame-opts-v.ll
986	20	llvm/test/CodeGen/RISCV/shadow-stack-frame-opts.ll
```

第一处：`getUnmanagedCSI` 把所有非负 frame index 当成"托管 CSI"，libcall 负责的寄存器被算两次：

```diff
@@ -589,7 +589,10 @@ getUnmanagedCSI(const MachineFunction &MF,
     int FI = CS.getFrameIdx();
-    if (FI >= 0 && MFI.getStackID(FI) == TargetStackID::Default)
+    // Callee saves with negative frame indices have fixed positions on the
+    // stack, usually relating to libcalls or push/pop.
+    bool IsManagedFI = (FI < 0);
+    if (!IsManagedFI && MFI.getStackID(FI) == TargetStackID::Default)
       NonLibcallCSI.push_back(CS);
```

第二处：`getFrameIndexReference` 的"是否相对 `sp`"判断漏掉 push/libcall 的负 frame index；第三处：手工恢复只恢复 libcall 那一份，并加断言锁前提：

```diff
@@ -1691,10 +1694,17 @@ getFrameIndexReference(const MachineFunction &MF, int FI,
-  if ((FI >= MinCSFI && FI <= MaxCSFI) || IsInterruptCSR) {
+  auto FIIsCalleeSave = [&](const CalleeSavedInfo &CS) -> bool {
+    return FI == CS.getFrameIdx();
+  };
+  bool IsInlineRestoredLibcallCSR = llvm::any_of(
+      getPushOrLibCallsSavedInfo(MF, MFI.getCalleeSavedInfo()), FIIsCalleeSave);
+  if ((FI >= MinCSFI && FI <= MaxCSFI) || IsInterruptCSR ||
+      IsInlineRestoredLibcallCSR) {
     FrameReg = SPReg;
...
   } else if (const char *RestoreLibCall = getRestoreLibCallName(*MF, CSI)) {
+    assert(RVFI->useSaveRestoreLibCalls(*MF) &&
+           "Expected restore with libcall to use save restore libcalls");
     if (RVFI->hasShadowStack(*MF)) {
-      loadRegFromStackSlot(CSI);
+      loadRegFromStackSlot(getPushOrLibCallsSavedInfo(*MF, CSI));
       return true;
     }
```

测试才是主体。`shadow-stack-frame-opts.ll` 新增两条 RUN 覆盖"有 FPR callee-save 的 ABI"；新建的 `-v.ll` 覆盖 RVV + 可伸缩栈帧（CFI 是 `sp + 32 + 2 * vlenb`）：

```llvm
; RUN: llc -mtriple=riscv32 -mattr=+d,+zimop,+save-restore -target-abi=ilp32d < %s \
; RUN:   | FileCheck %s --check-prefix=RV32D-SAVE-RESTORE
; RUN: llc -mtriple=riscv64 -mattr=+d,+zimop,+save-restore -target-abi=lp64d < %s \
; RUN:   | FileCheck %s --check-prefix=RV64D-SAVE-RESTORE
```

**要点 / review 关注点**：根因两条就写两个 bullet，不写"fixed restore bug"；测试不是加一个 check，而是按 `-mattr` × ABI 建矩阵（`+d` 走 FPR callee-save，`+v` 走可伸缩栈）——reviewer 会先问 "does this reproduce without `+d`?"；986 行新增绝大多数由 `update_llc_test_checks.py` 生成，手写不可能也不应该。

---

## 29.4 解剖二：NFC —— `885f35214738`

```bash
$ git show --stat --format='%h %s' 885f35214738
885f35214738 [RISCV] Use a switch in RISCVABI::isRVE. NFC (#239001)
 llvm/lib/Target/RISCV/MCTargetDesc/RISCVBaseInfo.h | 11 +++++++++--
 1 file changed, 9 insertions(+), 2 deletions(-)
```

完整 diff 只有这一段；正文一句话："This matches the style of the FP functions."

```diff
 // Returns true for the ABIs that only use the reduced 16 register integer file.
 inline bool isRVE(ABI TargetABI) {
-  return TargetABI == ABI_ILP32E || TargetABI == ABI_IL32PC64E ||
-         TargetABI == ABI_LP64E || TargetABI == ABI_CHERIOT;
+  switch (TargetABI) {
+  case ABI_ILP32E:
+  case ABI_IL32PC64E:
+  case ABI_LP64E:
+  case ABI_CHERIOT:
+    return true;
+  default:
+    return false;
+  }
 }
```

**NFC 不是"随便改"**：它要能证明语义等价——同样的 4 个枚举值，`default` 返回 `false`，与 `||` 链逐值等价；额外收益是 `ABI` 改成 `enum class` 后新增枚举值会触发 `-Wswitch`。**NFC 通常不带测试**，它的"测试"是既有 `attributes.ll`、`calling-conv-*.ll` 全绿，评审看的是"有没有偷偷改行为"。NFC 标记有两种写法，本树都存在：前缀式 `[NFC][RISCV] Add curly braces ... (#239653)` 与后缀式 `... instead of Register. NFC (#239368)`。**review 关注点**：除 `-Wswitch` 带来的编译期保护外，不能再有任何可观察差异。

---

## 29.5 解剖三：新扩展 —— `dc21109c01ad`

```bash
$ git show --stat --format='' dc21109c01ad | tail -1
 14 files changed, 103 insertions(+), 6 deletions(-)
$ git show --numstat --format='' dc21109c01ad | grep -E 'docs/ReleaseNotes|RISCVFeatures\.td|Xqccmi\.td|RISCVISAInfo\.cpp'
1	0	llvm/docs/ReleaseNotes.md
9	0	llvm/lib/Target/RISCV/RISCVFeatures.td
30	0	llvm/lib/Target/RISCV/RISCVInstrInfoXqccmi.td
7	6	llvm/lib/TargetParser/RISCVISAInfo.cpp
```

**14 个文件、103 行**，这是"加一条指令"的真实成本。四个必改落点：

**(1) Feature 记录**（`RISCVFeatures.td`）——版本、依赖、汇编器报错文案都在这里：

```tablegen
+def FeatureVendorXqccmi
+    : RISCVExperimentalExtension<0, 2,
+                     "Qualcomm 16-bit Instruction Lookup Table",
+                     [FeatureStdExtZca, FeatureStdExtZicsr]>;
+def HasVendorXqccmi
+    : Predicate<"Subtarget->hasVendorXqccmi()">,
+      AssemblerPredicate<(all_of FeatureVendorXqccmi),
+                         "'Xqccmi' (Qualcomm 16-bit Instruction Lookup Table)">;
```

**(2) 指令定义**（新文件 `RISCVInstrInfoXqccmi.td`）——注意 `DecoderNamespace`、`IsRV32`、`mayLoad`：

```tablegen
+let DecoderNamespace = "Xqccmi", Predicates = [HasVendorXqccmi, IsRV32],
+    hasSideEffects = 1, mayLoad = 1, mayStore = 0 in {
+def QC_CM_ILUT : RVInst16<(outs), (ins uimm11:$ilut_index),
+                          "qc.cm.ilut", "$ilut_index", [], InstFormatOther> {
+  bits<11> ilut_index;
+  let Inst{15-13} = 0b001;
+  let Inst{12-2} = ilut_index;
+  let Inst{1-0} = 0b00;
+}
+}
```

**(3) 冲突与 RV32-only 校验**（`RISCVISAInfo.cpp`）——新扩展和 `Zcd` 抢同一段编码：

```diff
   static constexpr StringLiteral ZcdOverlaps[] = {
-      {"zcmt"}, {"zcmp"}, {"xqccmp"}, {"xqccmt"}, {"xqciac"}, {"xqcicm"},
+      {"zcmt"},   {"zcmp"},   {"xqccmi"}, {"xqccmp"},
+      {"xqccmt"}, {"xqciac"}, {"xqcicm"},
   };
```

**(4) 文档层**——`ReleaseNotes.md` 加一行，`RISCVUsage.md` 加完整段落（含 spec 链接）。测试面按 valid/invalid 分开：

| 文件 | 行数 | 覆盖什么 |
|---|---|---|
| `llvm/test/MC/RISCV/xqccmi-valid.s` | +23 | 编码 `[0x00,0x20]`、边界 2047、两个新 CSR |
| `llvm/test/MC/RISCV/xqccmi-invalid.s` | +8 | `2048`、`-1` 越界诊断 |
| `attribute-arch.s` / `features-info.ll` | +3/+1 | `.attribute arch` 与 `-march=help` |
| `print-supported-extensions-riscv.c` / `RISCVISAInfoTest.cpp` | +1/+6 | clang 侧可见；依赖与冲突单测 |

`+experimental-` 前缀是未冻结扩展的硬约定，报错文案直接来自 `AssemblerPredicate` 第三参数——**改 Feature 定义等于改用户看到的错误信息**（实测见 29.11 E6）。**review 关注点**：`ZcdOverlaps` 与 `RV32Only` 两张表是否同步、valid/invalid 测试是否成对。

---

## 29.6 提交信息规范：前缀、NFC、Fixes

`llvm/docs/DeveloperPolicy.md:344-349` 是唯一权威表述：

> When the changes are restricted to a specific part of the code ..., **it is customary** to add a tag to the beginning of the line in square brackets. ... This helps email filters and searches for post-commit reviews.

关键词是 **customary**：`[RISCV]` 是惯例不是强制。CI 不检查，但不写就从 `git log --grep='\[RISCV\]'` 里消失。最近 2000 个改到该目录的提交，前缀分布（实测，节选）：

| 前缀 | 条数 | 前缀 | 条数 |
|---|---|---|---|
| `[RISCV]` | 1167 | `[RISCV][GlobalISel]` | 34 |
| `[RISCV][P-ext]` | 152 | `[RISCV][MC]` | 26 |
| `[RISCV][GISel]` | 47 | `[llvm][RISCV]` | 24 |
| `[RISCV][NFC]` | 41 | `[RISC-V]` | 19 |
| `[RISCV][llvm]` | 38 | `[CodeGen]` | 19 |

10 条真实标题分类：

| # | 标题 | 类型 | 信号词 | 测试 |
|---|---|---|---|---|
| 1 | `[RISCV] Correctly restore libcall CSRs with SCS (#233338)` | bug fix | `Correctly` + `Fixes` | 有 |
| 2 | `[RISCV] Correct the CFA offsets for the stack probe loop. (#230322)` | bug fix | `Correct` | 有 |
| 3 | `[RISCV] Make sure ADDI isn't a frame index in ...foldLargeOffset. (#228635)` | bug fix | `Make sure ... isn't` | 有 |
| 4 | `[RISCV] Check hasStdExtZba() instead of hasShlAdd(3) ... (#239031)` | bug fix | `instead of` | 有 |
| 5 | `[RISCV] Use a switch in RISCVABI::isRVE. NFC (#239001)` | NFC | `. NFC` | 无 |
| 6 | `[NFC][RISCV] Add curly braces around case statement (#239653)` | NFC | `[NFC]` 前缀 | 无 |
| 7 | `[RISCV] Make findVRegWithEncoding return MCRegister ... NFC (#239368)` | NFC | `. NFC` | 无 |
| 8 | `[RISCV] Add assembler support for the Qualcomm Xqccmi ... (#225119)` | 新功能 | `Add ... support` | 有 |
| 9 | `[RISC-V][MC][RVY] Introduce support for the Zyhybrid extension (#233969)` | 新功能 | `Introduce support` | 有 |
| 10 | `[RISCV][P-ext] Support Packed Multiplication with ... intrinsics (#236303)` | 新功能 | `Support` | 有 |

**why / what 的顺序**：`DeveloperPolicy.md:323-326` 要求"简短说 what，但真正强调 why"。`a44f88533543` 标题说 what，正文先说坏在哪（"Before this change, we were restoring ... twice"）再列两个根因；`dc21109c01ad` 标题说 what，正文先说为什么这么建模（encoding 与 `c.fld` 重叠 → 与 `Zcd` 互斥）再附 spec。

**`Fixes #N`** 的规范形来自 `llvm/docs/BugLifeCycle.md:100-104`：commit message 里独占一行写 `Fixes #<issue number>`，GitHub 自动关闭并回链。实测 2000 个提交里 76 个带 `Fixes`（3.8%）——低是因为大量提交是 NFC、重构、新功能，本来就没有 issue 可关。

---

## 29.7 PR 描述模板

六个小节对应 reviewer 的六个疑问，可直接复制：

```markdown
## Problem
<用户可观察到的错误现象。一句话 + 一个最小复现命令或 .ll 片段。>

## Root cause
<为什么会这样。定位到文件/函数/不变量，而不是"逻辑有问题"。>

## Fix
<改了什么不变量。逐条列，和 diff 的 hunk 一一对应。>

## Tests
<新增/更新了哪些测试；覆盖了哪些 -mattr / ABI / RV32-RV64 组合。>

## Release note
<有：贴出 ReleaseNotes.md 新增行。无：说明为什么不算 user-facing。>

Fixes #<issue>
```

用 29.3 的 bug fix 填一遍（**从 commit message 与 diff 重建，不是 PR 页面原文**；离线环境无法访问 GitHub）：

```markdown
## Problem
With `+save-restore` (libcall callee saves) plus a shadow call stack,
`caller_sw` restores some callee-saved registers twice, and the second
restore runs after the stack is deallocated with a wrong base + offset.
Issue #233318 reports the resulting bad accesses.

## Root cause
1. `restoreCalleeSavedRegisters` used the full CSI, not the libcall subset.
2. `getFrameIndexReference` treated only `[MinCSFI, MaxCSFI]` FIs as `sp`-relative.

## Fix
- Use `getPushOrLibCallsSavedInfo` for the inline (shadow-stack) restore.
- Treat FIs belonging to a push/libcall CSI as `sp`-relative.
- Assert that a restore libcall implies `useSaveRestoreLibCalls`.

## Tests
`shadow-stack-frame-opts.ll`: add `RV32D-SAVE-RESTORE` / `RV64D-SAVE-RESTORE`
RUN lines (ABI with FPR callee saves), checks regenerated by
`update_llc_test_checks.py`; new `-v.ll`: RVV + scalable stack frame.

## Release note
None. Fixes a miscompile for an experimental feature combination and adds no
new option, extension, or diagnostic.

Fixes #233318
```

模板的价值不在格式，而在**强制你把"我改了什么"翻译成"别人怎么验证"**。写不出 `## Tests` 里的 `-mattr` 矩阵，说明你还没想清楚修复的边界。

---

## 29.8 review 关注点：从测试形状反推

reviewer 先看测试文件列表，再看源码。三个提交的形状对应四类追问：

**(1) negative test 在哪？** `dc21109c01ad` 若只交 `xqccmi-valid.s`，第一问必然是"越界立即数怎么办"。作者交了 `xqccmi-invalid.s`：

```asm
# CHECK-ERROR: error: immediate must be an integer in the range [0, 2047]
qc.cm.ilut 2048
qc.cm.ilut -1
```

只要改动引入新诊断、新互斥组合、RV32-only 约束，就必须有 `not llvm-mc` / `not llc` 用例。

**(2) `-mattr` 组合够不够？** `a44f88533543` 涉及 FPR callee-save 与可伸缩栈帧两条路径，矩阵必须扩到 `+d -target-abi=ilp32d/lp64d` 和 `+v`；原来的 `+zimop,+save-restore` 根本走不到坏代码。典型问法是 "does this reproduce without `+d`?"。

**(3) ABI / 用户可见影响？** `885f35214738` 能零测试过审，因为它只改实现不碰 ABI 判定结果。反之，动 callee-saved 集合、`e_flags`、calling convention 的改动都要回答"会不会让已有 `.o` 链接失败"。`27a2da4d2716` 把 `X5` 从 `CSR_RT_MostRegs` 移除，就同时改了 `RISCVCallingConv.td`、测试与 release note。

**(4) release note 呢？** 见 29.9；`DeveloperPolicy.md:264-266` 鼓励 reviewer 提要求。

**谁在 review**。`llvm/Maintainers.md:357-366` 列出 4 位：

| 姓名 | 邮箱 | GitHub |
|---|---|---|
| Alex Bradbury | `asb@igalia.com` | `asb` |
| Craig Topper | `craig.topper@sifive.com` | `topperc` |
| Philip Reames | `listmail@philipreames.com` | `preames` |
| Sam Elliott | `aelliott@qti.qualcomm.com` | `lenary` |

`a44f88533543` 的作者正是 Sam Elliott——**list 上的 reviewer 也提交补丁，但作者的补丁仍需另一位 reviewer 批准**。"找 maintainer 私聊"不构成 review。

---

## 29.9 release note：什么时候必须写

文件是 `llvm/docs/ReleaseNotes.md`（本树已是 `.md`；`DeveloperPolicy.md:240` 正文仍写 `ReleaseNotes.rst`，那是未更新的文字，以文件系统为准）。RISC-V 小节在 `ReleaseNotes.md:378`：

```markdown
### Changes to the RISC-V Backend

* Added experimental MC support for the ACLIC v0.20 extensions: `Smidctrl`,
  `Ssidctrl`, ...
* Removed X5 from the preserve_most register list.
```

格式是**一个 bullet 一句话，动词开头，扩展名用反引号**；两个真实提交加的行：

- `27a2da4d2716`：`* Removed X5 from the preserve_most register list.`
- `dc21109c01ad`：``* Adds experimental assembler support for the `Xqccmi` (Qualcomm 16-bit Instruction Lookup Table) vendor extension.``

**何时必须写**，`DeveloperPolicy.md:238-266` 的清单（非穷举）：增删改命令行选项；增删/重组诊断；修复有显著用户影响的 bug（链接 issue）；增删有广泛影响的优化；修改 C 稳定 API；预告未来版本的破坏性变更（放 `Potentially Breaking Changes`）。

RISC-V 侧的判据更简单：**用户能在 `-march` / `-mcpu` / 命令行 / 汇编语法里感知到的变化，就写一行**——新扩展、新 CPU、新 profile、experimental 转正、删除 vendor 扩展。反过来 `a44f88533543` 这种纯 miscompile 修复不写，它不改变任何接口。细节别塞进 note：扩展说明放 `llvm/docs/RISCVUsage.md:544` 附近（`dc21109c01ad` 在那里加了 4 行含 spec 链接）。

---

## 29.10 文档与源码落点

| 主题 | 文档 | 代码/数据 |
|---|---|---|
| RISC-V reviewer 名单 | `llvm/Maintainers.md:357-366` | — |
| commit message 规范 | `llvm/docs/DeveloperPolicy.md:315-379` | — |
| release note 判定标准 | `llvm/docs/DeveloperPolicy.md:238-266` | `llvm/docs/ReleaseNotes.md:378` |
| review 礼仪 / LGTM | `llvm/docs/CodeReview.md` | — |
| squash / `Fixes #N` / revert | `GitHub.md:128-172`；`BugLifeCycle.md:92-108`；`DeveloperPolicy.md:425-450` | — |
| 扩展文档 / 新指令模板 | `llvm/docs/RISCVUsage.md` | `RISCVFeatures.td`；`RISCVInstrInfoXqccmi.td`（30 行范例） |
| 扩展冲突 / RV32-only | — | `llvm/lib/TargetParser/RISCVISAInfo.cpp`（`ZcdOverlaps`/`RV32Only`） |
| bug fix / 测试矩阵范例 | — | `RISCVFrameLowering.cpp`（`getUnmanagedCSI`）；`shadow-stack-frame-opts*.ll` |

---

## 29.11 可跑实验

以下 7 个实验均在本树实测，输出为真实截取（长输出用 `...` 省略）。

**E1 — 统计与分类 `[RISCV]` 提交**

```bash
$ git log --oneline --grep='\[RISCV\]' | wc -l                            # 10841
$ git log --oneline --grep='\[RISCV\]' -- llvm/lib/Target/RISCV | wc -l   # 7673
$ git log --format='%s' -2000 -- llvm/lib/Target/RISCV | sed -E 's/^(\[[^]]*\](\[[^]]*\])?).*/\1/' | sort | uniq -c | sort -rn | head -3
   1167 [RISCV]
    152 [RISCV][P-ext]
     47 [RISCV][GISel]
```

**E2 — 三个提交的 `--stat`**

```bash
$ for s in a44f88533543 885f35214738 dc21109c01ad; do git show --stat --format='%s' $s | tail -1; done
3 files changed, 1138 insertions(+), 23 deletions(-)
1 file changed, 9 insertions(+), 2 deletions(-)
14 files changed, 103 insertions(+), 6 deletions(-)
```

**E3 — 导出提交里的测试并用 `llvm-lit` 重跑**

`llvm-lit` 需要 config；给 `/tmp` 写最小 `lit.cfg.py`（LLVM 23+ 起 `ShTest` 必须用内部 shell）：

```bash
$ D=$(mktemp -d /tmp/riscv-lit.XXXXXX)
$ git -C $TR show dc21109c01ad:llvm/test/MC/RISCV/xqccmi-valid.s > $D/xqccmi-valid.s
$ cat > $D/lit.cfg.py <<EOF
import os, lit.formats
config.name = 'riscv-standalone'
config.test_format = lit.formats.ShTest(execute_external=False)
config.suffixes = ['.s', '.ll']
config.test_source_root = '$D'; config.test_exec_root = '$D'
config.environment['PATH'] = '$T/bin:' + os.environ['PATH']
EOF
$ $T/bin/llvm-lit -v $D/xqccmi-valid.s
-- Testing: 1 tests, 1 workers --
PASS: riscv-standalone :: xqccmi-valid.s (1 of 1)
```

**不 checkout 整个提交，也能验证"这个测试在今天的 HEAD 上还成立"**。PASS 变 FAIL 说明中间有回归，`git log <sha>..HEAD -- <test>` 就是嫌疑名单。

**E4 — 用 `-S` 追溯一个函数的引入**

```bash
$ git log --oneline -2 -S'getPushOrLibCallsSavedInfo' -- llvm/lib/Target/RISCV
a44f88533543 [RISCV] Correctly restore libcall CSRs with SCS (#233338)
66a38a5001a5 [RISCV] Ensure Shadow Stack checks correct RA (#226309)
```

**E5 — 看提交节奏**

```bash
$ git log --format='%h %ad %s' --date=short -5 -- llvm/lib/Target/RISCV
56feb676d393 2026-11-07 [RISCV] isLegalToVectorizeReduction - fix MSVC ...
7fd3ba20fb28 2026-11-06 RISCV: Remove clearKillFlags calls (#239838)
7c465e3cf0dc 2026-11-06 RISCV: Remove clearKillFlags calls on virtual registers ...
216a14a209d9 2026-11-06 [RISCV] Report errors for user reserving argument/return ...
fa61fe89a147 2026-11-06 [RISCV] Drop MachineDominatorTreeWrapperPass in Zilsd ...
```

一天 5 个左右且大量是 NFC/清理：PR 排队慢不是没人看，是队列长——先把 diff 做小。

**E6 — 指令编码与诊断**

```bash
$ printf 'qc.cm.ilut 2047\n' > /tmp/one.s
$ $T/bin/llvm-mc /tmp/one.s -triple=riscv32 -mattr=+experimental-xqccmi -show-encoding
	qc.cm.ilut	2047                    # encoding: [0xfc,0x3f]
$ $T/bin/llvm-mc /tmp/one.s -triple=riscv32 -show-encoding
/tmp/one.s:1:1: error: instruction requires the following: 'Xqccmi' (Qualcomm 16-bit Instruction Lookup Table)
```

`0x3ffc` ↔ `2047` 往返一致，说明 `Inst{12-2}` 没写错位；报错文案就是 `AssemblerPredicate` 第三参数。

**E7 — revert 的 dry-run（不改树）**

`git revert` 会写工作树，绝对不要在分析树上做。用 `git apply --check -R` 在 `/tmp` 验证：

```bash
$ D=$(mktemp -d /tmp/revert-check.XXXXXX) && cd $D
$ git -C $TR show --name-only --format='' a44f88533543 | while read f; do
>   mkdir -p "$(dirname "$f")"; git -C $TR show a44f88533543:"$f" > "$f"; done
$ git init -q . && git add -A && git -c user.email=a@b -c user.name=c commit -qm base
$ git -C $TR show a44f88533543 | git apply --check -R - && echo OK
OK
$ git -C $TR show a44f88533543 | git apply --check -
error: patch failed: llvm/lib/Target/RISCV/RISCVFrameLowering.cpp:589
error: llvm/test/CodeGen/RISCV/shadow-stack-frame-opts-v.ll: already exists
```

反向能干净应用、正向不能——这是"可干净 revert"的机器证明。真正 revert 时还要在 commit message 写原因（`DeveloperPolicy.md:437`）。

---

## 29.12 踩雷集锦

1. **把 `[NFC]` 当"随便改"**。NFC 承诺行为逐位等价，reviewer 会全量跑 `llvm-lit`。`885f35214738` 的 9 行能过审，是因为连枚举覆盖顺序都没变；顺手改 `if` 顺序或 `unsigned`→`int` 就不叫 NFC。
2. **PR 里混入无关格式化**。`dc21109c01ad` 的 `RISCVISAInfo.cpp` 因插入更长字符串重排整个数组（+7/−6 只为 1 个新增），clang-format 下可接受；但你顺手 format 没碰过的函数，reviewer 会让你拆出去。
3. **测试只写 positive**。`xqccmi-invalid.s` 的存在不是巧合：新诊断、新互斥、RV32-only 约束，每条都要 `not` 用例。
4. **release note 漏写**。新扩展/新 CPU/新选项漏了，一句 "release note?" 就打回；反过来纯 bug fix 写了也会被要求删掉。
5. **`Fixes #` 写歪**。标准形是独占一行的 `Fixes #<issue number>`（`BugLifeCycle.md:100-104`）。写成 `Fix: 233318`、`fixes 233318`，或埋进 "This also fixes #233318" 这类长句，自动关闭就可能不触发——照抄标准形，别赌解析器。
6. **squash 后丢失 review 上下文**。`GitHub.md:133-137`：只有 PR 标题和正文进 `main`，"Addressed review comments"、"fix build" 全部消失。merge 前自己过一遍正文。
7. **revert 不写原因 / `git show` 输出被截断**。`DeveloperPolicy.md:433-441` 要求 revert commit 解释为什么回滚并附公开复现；`--stat` 会把长路径缩成 `.../shadow-stack-frame-opts-v.ll`，路径写错的 `git show <sha> -- <path>` 会静默输出空 diff——先 `--name-only`，再 `--numstat`。

---

## 29.13 本章重点整理

- **样本要过滤**：`git log --grep='\[RISCV\]'` 有 29% 噪声，必须加 `-- llvm/lib/Target/RISCV`。
- **diff 形状先于流程**：bug fix="几行源码 + 上千行测试"（+16/−3 vs +1122/−20），NFC="一个文件零测试"（+9/−2），新扩展="14 文件、约 100 行、1 行 release note"（+103/−6）。
- **每步留证据**（复现有最小 `.ll`、定位有 pass 名、修复有 diff、回归有 lit PASS、merge 有单个 squash commit）；**`[RISCV]` 是惯例**（`DeveloperPolicy.md:345` "it is customary"，2000 个提交里 1167 个这么写）；**正文先 why 后 what**，`Fixes #N` 独占一行。
- **PR 正文 = 最终 commit message**，merge 前当文档写；reviewer 先看测试形状（negative test、`-mattr` 矩阵、ABI、release note）。
- **release note 判据是"用户能否感知"**（`ReleaseNotes.md:378`）；**revert 也用工具验证**：`git show <sha> | git apply --check -R`。

---

## 29.14 自我检核

- [ ] 我能说出 `[RISCV]` 前缀在 `DeveloperPolicy.md` 里的原话是 "customary"，并解释为什么仍要写。
- [ ] 我能不看文档写出 `git log --grep='\[RISCV\]' -- llvm/lib/Target/RISCV` 并说明为什么要加路径过滤。
- [ ] 我能解释 `a44f88533543` 的两个 root cause 分别对应哪个 hunk。
- [ ] 我能列出新增一条 RISC-V 指令必须改的四个落点。
- [ ] 我能写出可复制的 PR 模板，并填满 `Tests` 与 `Release note` 两节。
- [ ] 我能在 `/tmp` 用最小 `lit.cfg.py` 跑通一个从历史提交导出的测试。
- [ ] 我能用 `git apply --check -R` 判断提交是否可干净 revert，并说明为什么不在工作树上直接 `git revert`。

---

## 29.15 延伸阅读

- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/DeveloperPolicy.md`（commit message: 315-379；release notes: 238-266；revert: 425-450）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/GitHub.md`（squash 与 landing: 128-172）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/BugLifeCycle.md`（`Fixes #N`: 92-108）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/CodeReview.md`、`llvm/Maintainers.md`（RISC-V reviewer: 357-366）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/ReleaseNotes.md`（RISC-V 小节: 378）
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/RISCVUsage.md`（扩展文档: 544 附近）
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/27-lit-testing-and-update-scripts.md`、`28-debugging-codegen.md`

→ 下一章：[Ch 30 — 维护者视角：triage、review 与 revert](./30-maintainership-and-triage.md)
