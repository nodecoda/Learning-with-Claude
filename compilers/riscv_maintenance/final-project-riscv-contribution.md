# Final Project — 交出一个可提交上游的 RISC-V 修复

> **目标**：把 Ch 27–31 组装成一次完整贡献：选一个真实缺口，复现它，找到根因，写出最小 patch 与 lit 测试，留下可复跑的验证证据，并写出 reviewer 愿意读的 PR 描述。
>
> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（`analysis/riscv-maintenance`，HEAD `0fc5afa04c23`，只读）；构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（下文用 `$T` 指代；LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。补丁演练在 `/tmp/final-work` 的 shared clone 完成，不改原树。
>
> **前置**：读完 [Ch 27](./27-lit-testing-and-update-scripts.md)、[Ch 28](./28-debugging-codegen.md)、[Ch 29](./29-contribution-workflow.md)、[Ch 30](./30-maintainership-and-triage.md)、[Ch 31](./31-contribution-map.md)。Final 不再教新工具；它检查你是否能把已有工具串成证据链。

---

## F.1 为什么 Final 不是“再讲一个知识点”

前面每章都在回答一个局部问题：pattern 为什么没选中、MIR 为什么多了一条 `sext.w`、lit 的 CHECK 为什么脆弱。Final 只问一个整体问题：

> **如果明天让你给 `llvm/lib/Target/RISCV` 发一个 patch，你能否在一天内交出“别人可以复跑、reviewer 可以合并”的东西？**

“知道 `sh1add` 是什么”不值钱；“能证明某段 IR 在 `+zba` 下漏选 `sh1add`，并用一个 4 行 pattern 修掉”才值钱。两者的差距不是知识，而是**证据链**。

Final 的交付物只有四件：

| 交付物 | 内容 | 合格线 |
|---|---|---|
| 一个 patch | 源码改动 + 新测试，能 `git apply --check` | 不夹带无关改动 |
| 一个 lit 测试 | 有 RUN 行、positive CHECK、negative CHECK | 未修复时应当失败 |
| 一段 PR 描述 | Problem / Root cause / Fix / Tests / Release note / Fixes | reviewer 读完知道为什么改 |
| 一份验证证据 | 命令、真实输出、未实测项 | 别人能照着复跑 |

完成定义不是“我觉得对了”，而是下面五条同时成立：

1. 你有一个**最小复现**，不是一段“大概会这样”的伪代码。
2. 你能指出根因在哪个文件、哪个函数、哪一行，以及为什么现有逻辑覆盖不到。
3. 你的修复只改必要的地方；测试能锁住“修复前失败、修复后通过”的行为。
4. 你能说清哪些验证做了、哪些没做；没做的给出替代验证。
5. 你的 PR 描述本身就是最终 squash commit message 的草稿。

Final 有一条硬边界：**源码树只读**。不 edit、不 commit、不 checkout `trees/ct-instr-trunk`。所有演练都在 `/tmp` 的 shared clone 里做；这样即使 patch 写错，也不会污染课程基线。

---

## F.2 先建立直觉：评分卡与交付物

把一次贡献想成一条装配线。每个工位都产出下一站能用的证据，最后一个工位产出 PR：

```text
选题 → 复现 → 根因 → 最小修复 → 测试 → 验证 → PR 描述 → 自评
  │       │       │        │        │       │        │
 Ch31   Ch28   Ch28/源码  Ch14/19  Ch27   Ch27/26  Ch29
```

这张图的重点不是“章节对应”，而是**箭头方向不可逆**：没有复现就不要谈根因；没有根因就不要写修复；没有测试就不要说验证；没有验证就不要写 PR 描述。任何一步跳过去，reviewer 都会在下一轮把你送回来。

### 100 分评分表

| 维度 | 分值 | 满分标准 | 常见扣分 |
|---|---:|---|---|
| 复现 | 15 | 最小 `.ll`/`.s`/`.mir` + 一条命令 + 真实输出 + 基线对照 | 只有文字描述，没有可跑输入（−10） |
| 根因 | 20 | 文件、函数、行号、现有逻辑为什么漏掉、期望指令为什么等价 | 说“这里没优化”，但指不出 pattern/条件（−15） |
| 修复 | 20 | 最小 diff；不改无关代码；语义等价；覆盖相关宽度/扩展 | 顺手格式化、改注释、加新抽象（−10） |
| 测试 | 25 | positive + negative；`-mattr`/ABI 矩阵；RUN 行可独立执行；CHECK 断言行为而非寄存器噪声 | 只有正例（−15）；只断言“能编译”（−20） |
| 验证 | 10 | 修复前/后 `llc`、`llvm-lit`、编码对照、`git apply --check` | 用“应该可以”代替输出（−8） |
| 描述 | 10 | Problem / Root cause / Fix / Tests / Release note / Fixes 六节完整 | 标题写“fix bug”，正文没有 why（−6） |

**70 分是及格线，85 分是可以拿去发 PR 的线，95 分以上才是“reviewer 只需要挑格式”的线。** 自评时不要给自己“辛苦分”；按表逐项找证据，找不到证据就是 0 分。

### 交付物目录

建议在 `/tmp/final-work` 里留下这套文件（不要提交到源码树）：

```text
/tmp/final-work/          # patch 源码 + lit 测试
/tmp/final.patch          # git diff 输出
/tmp/final-PR.md          # PR 描述草稿
/tmp/final-evidence.txt   # 命令 + 真实输出
/tmp/final-self-review.md # 按 100 分表自评
```

其中 `final.patch` 必须能在一棵干净的 tree 上通过：

```bash
git apply --check /tmp/final.patch
```

这一步不是形式主义。它验证的是 patch 的上下文、文件路径、new file mode 和行号；reviewer 的 CI 也从这个动作开始。

---

## F.3 三个候选题目

下面三条都在本机构建上复现过。A 是完整 worked example；B、C 给出入口、真实输出与验收标准，供你换题或做第二题。

### 候选 A（入门，CodeGen pattern）：`(x << 2) - x` 漏选 `sh1add`

**站位**：SelectionDAG → TableGen pattern。**难度**：低。**适合**：第一次发 patch，想走完“IR → llc → pattern → lit”全链。

**入口**：

- `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td:724` 的 `ShxAddPat` 只匹配 `riscv_add_like (shl x, i), y` 与 `riscv_shl_add`；
- `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td:746` 的 `HasStdExtZba` 块；
- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp:4311` 的 `selectSHXADDOp`。

**复现命令**（完整过程见 F.4）：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+zba /tmp/sub-shl2.ll -o -
```

**当前真实输出**：

```asm
sub_shl2:
	slli	a1, a0, 2
	sub	a0, a1, a0
	ret
```

**期望**：`sh1add a0, a0, a0`。因为 `(x << 2) - x == 3 * x`，而 `sh1add rd, rs1, rs2 = rs1 * 2 + rs2`；令 `rd = rs1 = rs2 = x` 即得 `3x`。

**验收标准**：

1. `+zba` 下输出恰好一条 `sh1add`，不再有 `slli`/`sub`；
2. 无 `+zba` 时仍输出 `slli` + `sub`，不能把 baseline 改坏；
3. 新测试同时覆盖 `+zba` 与 baseline 两种 RUN；
4. patch 只改 `RISCVInstrInfoZb.td` 与一个新测试文件。

### 候选 B（中等，MC 层）：`rev8` 在 RV64 下诊断串出 RV32 编码

**站位**：MC assembler / instruction matcher。**难度**：中。**适合**：想理解“同一助记符两套编码”的匹配与诊断。

**入口**：

- `llvm/lib/Target/RISCV/RISCVInstrInfoZb.td:419-427` 定义了两个同名 `rev8`：`REV8_RV32`（`IsRV32`）与 `REV8_RV64`（`IsRV64`）；
- 测试位置：`llvm/test/MC/RISCV/rv64zbb-invalid.s` 的 RUN 带 `-mattr=+zbb`，不适合作为“缺 Zbb”的 negative；建议新建 `llvm/test/MC/RISCV/rv64-invalid.s`，RUN 写 `not llvm-mc -triple=riscv64 < %s 2>&1 | FileCheck %s`。

**复现命令与真实输出**：

```bash
$ printf 'rev8 a0, a1\n' > /tmp/rev8.s
$ $T/bin/llvm-mc -triple=riscv64 /tmp/rev8.s
/tmp/rev8.s:1:1: error: invalid instruction, any one of the following would fix this:
rev8 a0, a1
^
/tmp/rev8.s:1:1: note: instruction requires the following: 'Zbb' (Basic Bit-Manipulation) or 'Zbkb' (Bitmanip instructions for Cryptography), RV32I Base Instruction Set
rev8 a0, a1
^
/tmp/rev8.s:1:1: note: instruction requires the following: 'Zbb' (Basic Bit-Manipulation) or 'Zbkb' (Bitmanip instructions for Cryptography)
rev8 a0, a1
^
```

RV64 下第一条 note 的 `RV32I Base Instruction Set` 是不适用的候选：`REV8_RV32` 的 predicate 是 `IsRV32`。这就是“编码表里同一 mnemonic 两套 encoding，诊断没有按 triple 过滤”的不一致。

**验收标准**：

1. `-triple=riscv64` 时只保留 RV64 候选，错误文本不再出现 `RV32I Base Instruction Set`；
2. `-triple=riscv32` 时只保留 RV32 候选；
3. 新 negative test 用 `not llvm-mc` + FileCheck 锁住 note 数量与关键字；
4. `+zbb` 下 `rev8` 仍能正常 `-show-encoding`，不能把正例改坏。

> 这条题的修复点可能在 MC matcher/诊断生成层，而不是简单改 `.td`。如果一时找不到最小改法，把它当“诊断质量 issue”提交也成立；但不要在 PR 里声称“修了编码”。

### 候选 C（进阶，机器层 pass）：`riscv-make-compressible` 不重写 load 目标寄存器

**站位**：post-RA machine pass。**难度**：高。**适合**：已经读过 Ch 19 / 练习 D，想碰物理寄存器与 liveness。

**入口**：

- `llvm/lib/Target/RISCV/RISCVMakeCompressible.cpp:297-335` 的 `getRegImmPairPreventingCompression`；
- 关键注释在 `:324`：*For loads, we can only change the base register since dest is defined rather than used.*
- 测试位置：`llvm/test/CodeGen/RISCV/make-compressible-rv64.mir`，用 `-mattr=+c -run-pass=riscv-make-compressible`。

**复现 MIR**（`/tmp/load-dead.mir`）：

```yaml
--- |
  define void @load_dead(ptr %p) {
    %v = load volatile i32, ptr %p, align 4
    ret void
  }
...
---
name:            load_dead
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $x10

    dead renamable $x5 = LW renamable $x10, 0 :: (volatile load (s32) from %ir.p)
    PseudoRET
...
```

**复现命令与真实输出**：

```bash
$ $T/bin/llc -mtriple=riscv64 -mattr=+c \
    -run-pass=riscv-make-compressible /tmp/load-dead.mir -o - | sed -n '/^body:/,$p'
body:             |
  bb.0:
    liveins: $x10

    dead renamable $x5 = LW renamable $x10, 0 :: (volatile load (s32) from %ir.p)
    PseudoRET
```

`$x10` 是 compressed register，`$x5` 不是；`$x15` 在 block 内空闲。pass 没有把 load 的目标改成 `$x15`，所以这条 `LW` 仍是 4 字节，而 `c.lw` 只需 2 字节。

**验收标准**：

1. 在目标寄存器 dead/可重命名、且存在空闲 compressed register 时，pass 选择 compressed dest；
2. 若 dest 有跨 block 使用、或没有安全 compressed register，必须放弃，不能破坏 liveness；
3. 新 MIR test 断言 `$x15 = LW`（或 `c.lw` 对应的机器指令），并保留一个 no-opt 反例；
4. `llvm-lit` 的 `make-compressible-rv64.mir` 全绿。

### 怎么选

| 你的状态 | 建议 |
|---|---|
| 第一次做上游 patch | A；改动最小，证据链最完整 |
| 想练 MC/诊断 | B；但修复可能比 A 难，先确认能定位 matcher |
| 想做机器层 | C；先补 liveness 与 `RegScavenger` 知识，别硬改 |
| 时间只有半天 | A；B/C 留作第二题 |

---

## F.4 完整 worked example：A

下面每一个命令都在本机构建上跑过；输出是原样摘录，省略了 `.cfi_*`、`.attribute` 等与问题无关的行。

### F.4.1 最小复现

`/tmp/sub-shl2.ll`：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @sub_shl2(i64 %x) {
  %shl = shl i64 %x, 2
  %sub = sub i64 %shl, %x
  ret i64 %sub
}
```

```bash
$ $T/bin/llc -mtriple=riscv64 -mattr=+zba /tmp/sub-shl2.ll -o -
sub_shl2:                               # @sub_shl2
	.cfi_startproc
# %bb.0:
	slli	a1, a0, 2
	sub	a0, a1, a0
	ret
```

同一份 IR 在 `-mattr=+m,+zba` 下也仍是 `slli` + `sub`（实测），所以这不是“没有 M 扩展所以只能这样”的问题；`sh1add` 本来就在 `+zba` 里。注意：`opt -passes=instcombine` 会把它折成 `mul x, 3`，而 `mul 3` 已有 pattern；因此本例题定位是 `llc` IR 层 pattern 训练，真实选题要先跑目标 pipeline 确认 gap 仍存在。

### F.4.2 根因

**(1) 代数等价。** `(x << 2) - x = 4x - x = 3x`。`sh1add rd, rs1, rs2` 的语义是 `rd = rs1 * 2 + rs2`；取 `rd = rs1 = rs2 = x`，得到 `2x + x = 3x`。因此一条 `sh1add` 足够。

**(2) 现有 pattern 只覆盖 add 形状。** `RISCVInstrInfoZb.td:724` 的 `ShxAddPat` 匹配：

```tablegen
def : Pat<(XLenVT (riscv_add_like_non_imm12 (shl GPR:$rs1, (XLenVT i)), GPR:$rs2)),
          (shxadd GPR:$rs1, GPR:$rs2)>;
def : Pat<(XLenVT (riscv_shl_add GPR:$rs1, (XLenVT i), GPR:$rs2)),
          (shxadd GPR:$rs1, GPR:$rs2)>;
```

第一个是 `y + (x << i)`，第二个是等价的 shl-add 形状；第三个 complex pattern 走 `selectSHXADDOp`（`RISCVISelDAGToDAG.cpp:4311`），仍然要求 add-like。没有 `(x << 2) - x` 的 entry。

**(3) 为什么不是 DAGCombine 的活。** DAGCombine 不会把这个 sub 折成 `mul x, 3`；`llc` 输出的 `slli` + `sub` 证明它把两个节点原样交给 ISel。最小修复点因此在 TableGen pattern，而不是新增 combine。

### F.4.3 最小修复（真实 unified diff）

在 `/tmp/final-work` 的 clone 里改 `RISCVInstrInfoZb.td`，并新增测试。`/tmp/final.patch` 全文如下（`git apply --check` 已在干净 clone 通过）：

```diff
diff --git a/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td b/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td
index dbc2e77c895e..706d1debe7b3 100644
--- a/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td
+++ b/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td
@@ -744,6 +744,10 @@ class CSImm12MulBy8Pat<Instruction sh3add>
                   GPR:$r)>;
 
 let Predicates = [HasStdExtZba] in {
+// (x << 2) - x == 3 * x, a single sh1add.
+def : Pat<(XLenVT (sub (shl GPR:$rs1, (XLenVT 2)), GPR:$rs1)),
+          (SH1ADD GPR:$rs1, GPR:$rs1)>;
+
 foreach i = {1,2,3} in {
   defvar shxadd = !cast<Instruction>("SH"#i#"ADD");
   defm : ShxAddPat<i, shxadd>;
diff --git a/llvm/test/CodeGen/RISCV/rv64zba-sub-shl.ll b/llvm/test/CodeGen/RISCV/rv64zba-sub-shl.ll
new file mode 100644
index 000000000000..30f76d4686c4
--- /dev/null
+++ b/llvm/test/CodeGen/RISCV/rv64zba-sub-shl.ll
@@ -0,0 +1,20 @@
+; RUN: llc -mtriple=riscv64 -mattr=+zba -verify-machineinstrs < %s \
+; RUN:   | FileCheck %s --check-prefix=ZBA
+; RUN: llc -mtriple=riscv64 -verify-machineinstrs < %s \
+; RUN:   | FileCheck %s --check-prefix=NOZBA
+
+define i64 @sub_shl2(i64 %x) {
+; ZBA-LABEL: sub_shl2:
+; ZBA:       # %bb.0:
+; ZBA-NEXT:    sh1add a0, a0, a0
+; ZBA-NEXT:    ret
+;
+; NOZBA-LABEL: sub_shl2:
+; NOZBA:       # %bb.0:
+; NOZBA-NEXT:    slli a1, a0, 2
+; NOZBA-NEXT:    sub a0, a1, a0
+; NOZBA-NEXT:    ret
+  %shl = shl i64 %x, 2
+  %sub = sub i64 %shl, %x
+  ret i64 %sub
+}
```

**为什么这样写是“最小”**：

- 只加一个 `Pat`；不新增 `SDNode`、不改 `selectSHXADDOp`、不动 legalization。
- 模式限定 `shl` 的立即数必须是 `2`，不误伤 `(x << 3) - x`（那是 `7x`，一条 `sh1add` 不够，应该保持两条指令）。
- 立即数 `2` 用 `(XLenVT 2)`，与同文件 `:725` 的 `(XLenVT i)` 写法一致；pattern 用 `XLenVT` 泛化，但本课只实测 RV64，RV32 需要另加 RUN。
- 测试的 `NOZBA` RUN 是 negative control：没有 `+zba` 时不能出现 `sh1add`，否则说明 predicate 失效。

### F.4.4 测试

新文件放在 `llvm/test/CodeGen/RISCV/rv64zba-sub-shl.ll`，理由是同目录已有 `rv64zba.ll`，按扩展拆小文件比往 500 行大文件追加更好 review。RUN 行只依赖 `llc` 与 `FileCheck`，不需要 `%clang`。

测试三个要点：**positive**（`+zba` 下必须出现 `sh1add a0, a0, a0`）、**negative**（无 `+zba` 下必须仍是 `slli` + `sub`，且不能出现 `sh1add`）、**顺序**（`CHECK-NEXT` 锁住 `sh1add` 后紧接 `ret`，避免“有 sh1add 但还多一条 sub”的假通过）。未修复时 ZBA 会因缺少 `sh1add` 失败，NOZBA 仍通过，这是预期的 red/green 对照。

如果本地重建了 `llc`，验证命令是：

```bash
$T/bin/llvm-lit -sv llvm/test/CodeGen/RISCV/rv64zba-sub-shl.ll
```

> **未实测**：本课不重建 `llc`，所以上面这条新测试没有在本机跑过。可跑的替代是：`llvm-tblgen -gen-dag-isel` 语法检查（F.8 实验 8）、`git apply --check`（实验 2）、以及现有 `rv64zba.ll` 的 lit 基线（实验 4）。新测试的 `ZBA` 期望来自 `llvm-mc` 对 `sh1add` 的编码对照（实验 3），不是编造。

### F.4.5 验证矩阵

| 检查 | 命令 | 本机结果 |
|---|---|---|
| 现状复现 | `llc -mtriple=riscv64 -mattr=+zba` | 实测：`slli` + `sub` |
| 无 Zba 基线 | `llc -mtriple=riscv64` | 实测：`slli` + `sub` |
| 期望指令编码 | `llvm-mc -mattr=+zba -show-encoding` | 实测：`[0x33,0x25,0xa5,0x20]` |
| patch 可应用 | `git apply --check /tmp/final.patch` | 实测：OK |
| TableGen 语法 | `llvm-tblgen -gen-dag-isel ...` | 实测：RC=0 |
| 现有 lit | `llvm-lit -sv llvm/test/CodeGen/RISCV/rv64zba.ll` | 实测：1/1 PASS |
| 新 lit | `llvm-lit -sv .../rv64zba-sub-shl.ll` | **未实测**：需要重建 `llc` |
| 修复后汇编 | `llc ... \| FileCheck` | **未实测**：需要重建 `llc` |

### F.4.6 PR 描述草稿

```markdown
[RISCV] Select sh1add for (x << 2) - x

Problem:
For RV64 with +zba, (x << 2) - x is not selected as sh1add:

  %shl = shl i64 %x, 2
  %sub = sub i64 %shl, %x
  ret i64 %sub

llc -mtriple=riscv64 -mattr=+zba currently emits:

  slli a1, a0, 2
  sub  a0, a1, a0

but the expression is 3 * x, and sh1add x, x, x computes 2 * x + x
in one instruction.

Root cause:
RISCVInstrInfoZb.td only has ShxAddPat entries for add-like forms:
  add (shl x, i), y
  riscv_shl_add x, i, y
and the complex pattern selectSHXADDOp also requires an add-like node.
There is no entry for sub (shl x, 2), x.

Fix:
Add a HasStdExtZba pattern for sub (shl x, 2), x -> SH1ADD x, x.
The shift amount is fixed at 2; larger shifts are not 3 * x and are
intentionally left alone.

Tests:
- New llvm/test/CodeGen/RISCV/rv64zba-sub-shl.ll
- +zba RUN checks for a single sh1add
- baseline RUN checks that slli + sub remain without +zba

Release note:
None. This is a narrow codegen improvement, not a new user-visible
option, diagnostic, or ABI change.

Fixes #XXXXX
```

`Fixes #XXXXX` 必须换成真实 issue；没有 issue 就删掉整行，不要留占位符。

### F.4.7 为什么这个例子值得作为模板

它同时覆盖了 Final 想考的四件事：**复现**（5 行 IR，任何人都能跑）、**根因**（现有 pattern 只认 add 形状）、**修复**（4 行 TableGen，一行代数可解释）、**测试**（positive + negative，断言指令行为而非寄存器编号）。

A 的 patch 小，但它没有回避真实工作：你仍然要跑 `llc`、读 pattern、写 RUN、算编码、在干净 clone 里验证 apply。把 A 做完整，比草率地选一个“看起来高级”的 C 更有价值。

---

## F.5 “不能做”的边界

本课的环境约束必须写进证据，而不是藏起来：

| 不能做 | 原因 | 替代验证 |
|---|---|---|
| 重编 `llc` | 构建 39G，重建 RISCV target 不在本课实验范围；任务明确要求不重编 | `llvm-tblgen -gen-dag-isel` 验语法；`git apply --check` 验 patch 形状 |
| 跑新 lit 测试 | 新测试需要新 `llc` 才会产生新汇编 | 现有 `rv64zba.ll` lit 基线；`llvm-mc` 验证期望编码 |
| 真机运行 | 没有 RISC-V 板卡；本 patch 也不改变运行时语义 | 手算 `3x` 等价；`llvm-mc`/`llvm-objdump` 对照编码 |
| 真机 ABI/浮点验证 | 本 patch 不碰 ABI/FP；不要为了“显得完整”编造 qemu 输出 | 明确写“不适用”，不要伪造 |
| 上游 PR | 没有凭据，也不应该由课程环境代发 | 交 `final-PR.md` + `final.patch`，由你本人发 |

**“未实测”不是减分项，伪装成实测才是。** 一份诚实的验证矩阵，比一段假的 `PASS` 输出更接近上游 review 的要求。

---

## F.6 提交检查清单

发 PR 前逐条打勾：

- [ ] `git apply --check /tmp/final.patch` 在干净 clone 通过（不是只在改过的树上通过）。
- [ ] `git diff --stat` 只有预期文件；没有 `clang-format` 顺手改的无关行。
- [ ] 测试文件有 `RUN:`，且能用 `llvm-lit -sv <file>` 单独跑。
- [ ] positive CHECK 覆盖修复后的指令；negative CHECK 覆盖未启用扩展/未修复路径。
- [ ] `-mattr` 至少覆盖“有扩展”和“无扩展”两档；涉及 ABI 时再加 `-target-abi`。
- [ ] 新测试在未修复的 `llc` 上应当失败；如果没重建，明确写“未实测”，不要写“已验证”。
- [ ] release note 有判断：新选项/新诊断/广泛影响的优化才写；窄 codegen 改动通常不写。
- [ ] 没有 C++ 改动就不跑 `clang-format`；有 C++ 改动则跑 `git clang-format` 并只格式化自己的 hunk。
- [ ] commit/PR 标题用 `[RISCV]` 前缀；正文先 why 后 what。
- [ ] `Fixes #NNNNN` 独占一行，且 issue 号真实；没有就删除。
- [ ] reviewer 从 `llvm/Maintainers.md:357-366` 的 RISC-V 列表选：Alex Bradbury、Craig Topper、Philip Reames、Sam Elliott。
- [ ] PR 正文就是 squash 后的 commit message；不要在正文里写“address review”这类会在 squash 后消失的话。

---

## F.7 交付物清单

最终提交（课程作业或真实 PR）应包含：

| 文件 | 必需 | 说明 |
|---|---|---|
| `final.patch` | 是 | `git diff` 或 `git format-patch -1` 输出；能 `git apply --check` |
| 新/改 lit 测试 | 是 | 与 patch 在同一个 commit 中；有 RUN 与 CHECK |
| `final-PR.md` | 是 | 六节：Problem / Root cause / Fix / Tests / Release note / Fixes |
| `final-evidence.txt` | 是 | 命令 + 真实输出；明确标注未实测项 |
| `final-self-review.md` | 建议 | 按 F.2 的 100 分表逐项自评，附证据行号 |
| 录屏/终端日志 | 可选 | 不是必需；有则更好复跑 |

一个最小合格的提交示例：

```text
final.patch                 24 insertions(+)
final-PR.md                 6 sections
final-evidence.txt          llc / llvm-mc / lit / apply-check outputs
final-self-review.md        82/100, 扣分项: 新测试未实测
```

---

## F.8 可跑实验

以下 8 个实验都在本机真实执行过；命令中的 `$T` 是构建目录，`$TR` 是源码树。

```bash
source /home/ubuntu/llvm/toolset/env.sh
TR=/home/ubuntu/llvm/trees/ct-instr-trunk
T=/home/ubuntu/llvm/builds/ct-instr-trunk
```

### 实验 1 — 复现 A 的漏优化

```bash
cat > /tmp/sub-shl2.ll <<'LL'
target triple = "riscv64-unknown-elf"
define i64 @sub_shl2(i64 %x) {
  %shl = shl i64 %x, 2
  %sub = sub i64 %shl, %x
  ret i64 %sub
}
LL
$T/bin/llc -mtriple=riscv64 -mattr=+zba /tmp/sub-shl2.ll -o - | sed -n '/^sub_shl2:/,/^\s*ret/p'
```

真实输出：

```text
sub_shl2:                               # @sub_shl2
# %bb.0:
	slli	a1, a0, 2
	sub	a0, a1, a0
	ret
```

### 实验 2 — 在干净 clone 里 `git apply --check`

```bash
git clone --shared $TR /tmp/final-check
cd /tmp/final-check
git apply --check /tmp/final.patch && echo 'APPLY-CHECK: OK'
git apply /tmp/final.patch
git apply --stat /tmp/final.patch
```

真实输出（节选）：

```text
APPLY-CHECK: OK
 2 files changed, 24 insertions(+)
```

### 实验 3 — 手算编码 vs `llvm-mc`

`sh1add a0, a0, a0` 的字段：

```text
funct7=0x20000000  rs2=0x00A00000  rs1=0x00050000
funct3=0x00002000  rd=0x00000500   opcode=0x00000033
sum=0x20A52533 -> little-endian bytes 33 25 a5 20
```

```bash
$ printf 'sh1add a0, a0, a0\n' > /tmp/sh1.s
$ $T/bin/llvm-mc -triple=riscv64 -mattr=+zba -show-encoding /tmp/sh1.s
```

真实输出：

```text
	sh1add	a0, a0, a0                      # encoding: [0x33,0x25,0xa5,0x20]
```

手算与工具一致。这是“不能重编也能验证期望指令”的关键替代证据。

### 实验 4 — 现有 lit 基线

```bash
export PATH=$T/bin:$PATH
cd $TR
$T/bin/llvm-lit -sv llvm/test/CodeGen/RISCV/rv64zba.ll
```

真实输出：

```text
-- Testing: 1 tests, 1 workers --
Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

这条实验证明“现有测试环境是好的”，但不能证明新 pattern 生效；不要把两者混为一谈。

### 实验 5 — `git show` 参考提交

```bash
cd $TR
git show --stat --format='%h %s' a44f88533543 | head -12
```

真实输出：

```text
a44f88533543 [RISCV] Correctly restore libcall CSRs with SCS (#233338)

 llvm/lib/Target/RISCV/RISCVFrameLowering.cpp       |   19 +-
 llvm/test/CodeGen/RISCV/shadow-stack-frame-opts.ll | 1006 +++++++++++++++++++-
 3 files changed, 1138 insertions(+), 23 deletions(-)
```

这条实验练的是“读真实补丁的形状”：3 行逻辑 + 1138 行测试也可以是合格补丁；不要用代码行数判断贡献价值。

### 实验 6 — B 的诊断复现

```bash
printf 'rev8 a0, a1\n' > /tmp/rev8.s
$T/bin/llvm-mc -triple=riscv64 /tmp/rev8.s
```

真实输出（节选，完整输出见 B 小节）：

```text
/tmp/rev8.s:1:1: note: instruction requires the following: 'Zbb' (Basic Bit-Manipulation) or 'Zbkb' (Bitmanip instructions for Cryptography), RV32I Base Instruction Set
```

RV64 下出现 RV32-only 候选，就是这条题的入口。

### 实验 7 — C 的 MIR 复现

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+c \
  -run-pass=riscv-make-compressible /tmp/load-dead.mir -o - | sed -n '/^body:/,$p'
```

真实输出：

```text
body:             |
  bb.0:
    liveins: $x10

    dead renamable $x5 = LW renamable $x10, 0 :: (volatile load (s32) from %ir.p)
    PseudoRET
```

pass 没有改目标寄存器；`$x5` 仍不是 compressed register。

### 实验 8 — TableGen 语法检查

```bash
$T/bin/llvm-tblgen -gen-dag-isel \
  -I/tmp/final-work/llvm/lib/Target/RISCV \
  -I$T/include \
  -I/tmp/final-work/llvm/include \
  -I/tmp/final-work/llvm/lib/Target \
  /tmp/final-work/llvm/lib/Target/RISCV/RISCV.td -o /tmp/RISCVGenDAGISel.inc
echo RC=$?
```

真实输出：

```text
RC=0
```

它证明新 pattern 能被 TableGen 接受，**不**证明生成的 matcher 在运行时一定选中它；后者需要重建 `llc`，已标“未实测”。

---

## F.9 踩雷集锦

1. **把“能编译”当“正确”。** `llvm-tblgen` 通过、`git apply` 通过，只能说明语法和上下文没问题；pattern 是否真的命中，必须看 `llc` 输出。本课用 `llvm-mc` 和手算补上编码证据，但不把它冒充“codegen 已验证”。

2. **测试只覆盖正例。** 只写 `+zba` 的 `sh1add`，不写 baseline，就发现不了“predicate 写错，导致无 Zba 也选 sh1add”这种回归。A 的 `NOZBA` RUN 就是为此存在。

3. **patch 含无关改动。** 顺手 `clang-format` 整个文件、改注释、重排 include，reviewer 第一眼就会要求拆分。只改与根因直接相关的 hunk；格式化留给独立的 NFC。

4. **`git apply` 上下文不匹配。** 在改过的树上生成 diff，再往干净树 apply，行号/上下文可能已经漂移。始终在 `git clone --shared` 的干净树上 `git apply --check`；失败就重新 `git diff`，不要手改 hunk 行号。

5. **忘记 `-mattr`。** 测试只写 `llc -mtriple=riscv64`，修复前后的输出可能完全一样，测试永远绿。每个扩展相关测试至少要有“启用扩展”和“不启用扩展”两档。

6. **release note 漏写或乱写。** 新选项、新诊断、广泛影响的优化要写；一个窄 pattern 通常不写。判断标准是“用户能否感知”，不是“我改了很久”。

7. **把 `[NFC]` 写进行为改动。** NFC 承诺逐位等价；你改了 pattern 命中，就不再是 NFC。标题写 `[RISCV]`，正文说清行为变化。

8. **断言寄存器分配噪声。** `sh1add a0, a0, a0` 在别的优化下可能变成 `a1`；测试应优先断言指令与关键操作数，必要时用 `CHECK-DAG`，不要把整段寄存器编号当契约。

---

## F.10 本章重点整理

- Final 的产物不是“知识总结”，而是 **patch + lit test + PR 描述 + 验证证据** 四件套。
- 评分卡的重心在测试（25）与根因（20）；源码行数不是价值指标。
- 选题优先选**可最小复现**的缺口；A 的 `(x << 2) - x` 是最适合第一次贡献的模板。
- 根因要落到文件、函数、行号和“现有 pattern 只覆盖什么形状”，而不是“这里没优化”。
- 最小修复优先加一个 `Pat`，不要为了一个 4 行改动引入新 pass 或新抽象。
- 测试要有 positive + negative；`-mattr` 矩阵是 RISC-V 测试的基本盘。
- 不能重编就诚实标注“未实测”，并用 `llvm-tblgen`、`llvm-mc`、`git apply --check`、现有 lit 做替代验证。
- PR 描述按 Problem / Root cause / Fix / Tests / Release note / Fixes 六节写；它会在 squash 后成为 commit message。

---

## F.11 自我检核

- [ ] 我的 patch 能被 `git apply --check` 通过（在干净 clone，不是改过的树）。
- [ ] 我的测试能在未修复时失败；如果没重建 `llc`，我在证据里明确写了“未实测”。
- [ ] 我的测试同时覆盖 positive（有扩展/修复后）与 negative（无扩展/修复前）。
- [ ] 我能指出根因所在的文件、函数、行号，并解释现有逻辑为什么覆盖不到。
- [ ] 我的修复只改必要文件；`git diff --stat` 没有无关格式化。
- [ ] 我的 `-mattr` 覆盖至少两档，且没有把 baseline 改坏。
- [ ] 我能手算期望指令的编码，并用 `llvm-mc -show-encoding` 对照。
- [ ] 我知道这条 patch 要不要 release note，并能说出判断依据。
- [ ] 我的 PR 描述包含 Problem / Root cause / Fix / Tests / Release note / Fixes 六节。
- [ ] 我的 `Fixes #NNNNN` 是真实 issue；没有就删掉，没有占位符。
- [ ] 我能说出至少两位 RISC-V reviewer，并知道从 `llvm/Maintainers.md` 查。
- [ ] 我能区分“现有 lit 全绿”和“新测试通过”，不把前者当后者。

---

## F.12 延伸阅读

- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/27-lit-testing-and-update-scripts.md`：RUN/CHECK、update 脚本、`not`/`split-file`。
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/28-debugging-codegen.md`：`-debug-only=isel`、`-print-after`、MIR 定位。
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/29-contribution-workflow.md`：真实提交解剖、PR 正文、release note、revert。
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/30-maintainership-and-triage.md`：triage 与 reviewer 视角（本章完成后回读）。
- `/home/ubuntu/Learning-with-Claude/compilers/riscv_maintenance/31-contribution-map.md`：按 churn/issue 选题的地图（本章完成后回读）。
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`：`ShxAddPat`（724）、`HasStdExtZba` 块（746）、`REV8_RV32`/`REV8_RV64`（419-427）。
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`：`selectSHXADDOp`（4311）。
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Target/RISCV/RISCVMakeCompressible.cpp`：`getRegImmPairPreventingCompression`（297-335）。
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/test/CodeGen/RISCV/rv64zba.ll` 与 `rv64zba-sub-shl.ll`（本课新增示例）。
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/DeveloperPolicy.md`：release note 判断（238-266）、commit message（315-379）。
- `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/Maintainers.md`：RISC-V reviewer 列表（357-366）；`llvm/docs/ReleaseNotes.md`：RISC-V 小节（378 附近）。

→ 回到：[课程索引](./README.md)
