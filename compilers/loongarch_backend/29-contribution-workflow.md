# Ch 29 — 从 issue 到 PR：复盘 #239812 的完整修复

> 本章目标：把一个真实 bug 走完整条链——读 issue → 复现 → 定位 → 最小修复 → 回归测试 → commit message → PR 描述 → 预判 review 意见。所有数字都来自本树当前状态。

---

## 29.1 这棵树的真实状态

```text
HEAD        01df7c365d7c5420a2dd66ab941e3a0afb7293c9
branch      fix/issue-239812-relax-marker-order
upstream    https://gh-proxy.com/https://github.com/llvm/llvm-project.git (no_push)
origin      https://github.com/nodecoda/llvm-project.git  (fork)
upstream/main  8f639a8fa35dd4f3a79c8b9588f0cf59326fabd0
merge-base     5fe5e53cff518bb81e2debaff4747c8b9bac130b
ahead 1 / behind 19
```

改动的两个文件：

```text
lld/ELF/Arch/LoongArch.cpp                  | 24 +++++++-
lld/test/ELF/loongarch-tls-le-relax-order.s | 95 +++++++++++++++++++++
2 files changed, 116 insertions(+), 3 deletions(-)
```

只有一个提交，**还没有 PR**。这正好是“把本地修复整理成可提交 PR”的样本。

---

## 29.2 issue 的核心断言

GitHub issue #239812（作者 `basavesh`，label `lld:ELF`，状态 open）：

> LLD's LoongArch TLS local-exec relaxation depends on each normal relocation
> being immediately followed by its same-offset `R_LARCH_RELAX` marker.
>
> If the marker precedes its paired `R_LARCH_TLS_LE_LO12_R` relocation, LLD
> removes the TLS address-producing `lu12i.w` and `add.d` instructions but does
> not rewrite the remaining `addi.d` to use `tp`.

最小的 TLS LE 序列（三个重定位，offset 0/4/8）：

```asm
lu12i.w $a0, 0          # R_LARCH_TLS_LE_HI20_R   @0
add.d   $a0, $a0, $tp   # R_LARCH_TLS_LE_ADD_R    @4
addi.d  $a0, $a0, 0     # R_LARCH_TLS_LE_LO12_R   @8
ret
```

正常顺序（每条普通重定位后面紧跟 `R_LARCH_RELAX`）会被收缩成：

```asm
addi.d  $a0, $tp, 0
ret
```

而 issue 的输入只是把最后一对的顺序换成 **marker 在前**：

```text
.reloc tls_addr + 8, R_LARCH_RELAX, 0
.reloc tls_addr + 8, R_LARCH_TLS_LE_LO12_R, tvar
```

结果：前两条指令被删掉，`addi.d` 却没有改成 `$tp` 版本，函数返回的是进入时的 `$a0`。

**这个 bug 的本质不是“哪个顺序对”，而是“两个顺序都必须对”。** ABI 没有规定 marker 必须在后。

---

## 29.3 根因：`relaxable()` 只看下一个

```cpp
// lld/ELF/Arch/LoongArch.cpp:1105
static bool relaxable(ArrayRef<Relocation> relocs, size_t i) {
  return i + 1 < relocs.size() && relocs[i + 1].type == R_LARCH_RELAX;
}
```

整个 `relax()` 状态机（`:1286-1380`）对 TLS LE 的三个重定位分别调用 `relaxable(relocs, i)`：

- `R_LARCH_TLS_LE_HI20_R` @0：后面是 `R_LARCH_RELAX` → 松弛，删除 `lu12i.w`
- `R_LARCH_TLS_LE_ADD_R` @4：后面是 `R_LARCH_RELAX` → 松弛，删除 `add.d`
- `R_LARCH_TLS_LE_LO12_R` @8：**marker 在它前面**，所以 `relocs[i+1]` 不是 marker → **跳过**

于是“生产地址的两条指令被删了，消费地址的那条没被改写”。这种状态我称为 **half-relaxed**：不是全松弛，也不是不松弛，而是前后不一致。

---

## 29.4 修复：记住“刚才删到哪”

补丁引入一个局部游标（`:1290-1300`）：

```cpp
// Offset of the `lu12i.w` of the last TLS LE sequence whose address
// producing instructions were removed.
uint64_t tlsLeRelaxedAt = UINT64_MAX;
```

在 TLS LE 分支里（`:1349-1367`）：

```cpp
case R_LARCH_TLS_LE_HI20_R:
case R_LARCH_TLS_LE_ADD_R:
case R_LARCH_TLS_LE_LO12_R: {
  bool relaxConsumer = r.type == R_LARCH_TLS_LE_LO12_R &&
                       tlsLeRelaxedAt != UINT64_MAX &&
                       r.offset == tlsLeRelaxedAt + 8;
  if (!relaxable(relocs, i) && !relaxConsumer)
    break;
  relaxTlsLe(ctx, sec, i, loc, r, remove);
  if (remove == 4)
    tlsLeRelaxedAt =
        r.type == R_LARCH_TLS_LE_ADD_R ? r.offset - 4 : r.offset;
  break;
}
```

三个设计点：

1. **`+8` 是序列几何**：`HI20` 在 `lu12i.w`（offset X），`ADD` 在 `add.d`（X+4），`LO12` 在 `addi.d`（X+8）。所以消费者在 `tlsLeRelaxedAt + 8`。
2. **`remove == 4` 才记录**：只有真的删掉了 4 字节指令才认为“这一对已松弛”，避免把别的重定位误记进来。
3. **`ADD_R` 记录 `r.offset - 4`**：因为它的 offset 是 X+4，要回退到序列起点 X，才能和 `LO12` 的 `X+8` 对齐。

整个补丁 24 行（含注释），没有改 `relaxable()` 本身——这是刻意的：`relaxable()` 是“这一条后面有没有 marker”，语义没变；新逻辑是“上一条生产指令是否已被删”。**改语义最小、可回滚**，符合 LLVM 对 lld 补丁的偏好。

---

## 29.5 回归测试：为什么必须用 `yaml2obj`

`lld/test/ELF/loongarch-tls-le-relax-order.s`（95 行）的开头：

```text
# REQUIRES: loongarch
# RUN: yaml2obj %s -o %t.o
# RUN: ld.lld %t.o -o %t
# RUN: llvm-objdump --no-print-imm-hex -d --no-show-raw-insn %t | FileCheck %s --check-prefixes=CANONICAL,HINT-FIRST
```

测试里定义两个符号 `canonical` 和 `hint_first`，**指令完全相同、重定位类型/偏移/addend 完全相同，只有 offset 8 处的两条重定位顺序不同**。断言两边都必须是：

```text
# CANONICAL-LABEL: <canonical>:
# CANONICAL-NEXT:    addi.d  $a0, $tp, 0
# CANONICAL-NEXT:    ret
# HINT-FIRST-LABEL: <hint_first>:
# HINT-FIRST-NEXT:    addi.d  $a0, $tp, 0
# HINT-FIRST-NEXT:    ret
```

用汇编器写不出这个输入：`llvm-mc` 永远把 `R_LARCH_RELAX` 放在配对重定位之后。所以只能 `yaml2obj` 直接描述 `.rela.text`。**这是“测试必须能表达 bug 的触发条件”的典型例子**。

---

## 29.6 commit message 解剖

```text
[lld][LoongArch] Don't half-relax TLS LE sequences

R_LARCH_RELAX is paired with another relocation at the same offset, and the
LoongArch ELF ABI does not require the marker to follow the relocation it
annotates.  lld only looked forward, so when the marker of the
R_LARCH_TLS_LE_LO12_R came first, the lu12i.w/add.d relocations were still
relaxed (their instructions removed) while the consumer was not rewritten to
use $tp.  The code then computed the TLS address from a register that is no
longer initialized.

Relax the consumer whenever the address producing instructions have been
removed, so the sequence stays consistent no matter on which side of the
relocation the marker is emitted.

Fixes #239812
Assisted-by: Codex (deepseek-v4.1-flash)
```

四段结构：

1. **标题**：`[组件][子目标] 祈使句`，72 列内，不用句号。`Don't half-relax` 直接说清“不做什么”。
2. **问题段**：先给 ABI 事实（marker 不要求在后），再说 lld 的错误假设，最后说可观测后果（读未初始化寄存器）。
3. **修复段**：一句话说策略（“只要生产指令被删，就一并松弛消费者”），强调**顺序无关**。
4. **尾注**：`Fixes #239812` 让 GitHub 自动关 issue；`Assisted-by:` 是 LLVM `AIToolPolicy.md:25-26` 推荐的 AI 披露写法。

`Fixes` 而不是 `Closes`：LLVM 两者都用，但 `Fixes` 在 lld 提交里更常见；关键是必须带 `#` 号。

---

## 29.7 提交前 checklist（本树可执行）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk

# 1. 只改了两个文件
git diff --stat $(git merge-base HEAD upstream/main)..HEAD

# 2. 目标测试单个通过
$T/bin/llvm-lit lld/test/ELF/loongarch-tls-le-relax-order.s

# 3. 整个 lld 的 LoongArch 子集
$T/bin/llvm-lit -q lld/test/ELF --filter=loongarch

# 4. 格式
git clang-format --diff $(git merge-base HEAD upstream/main)

# 5. 重新基于最新 main（本树 behind 19，提交前必须做）
git fetch upstream
git rebase upstream/main
```

第 5 步是硬要求：behind 19 意味着当前分支可能和 `lld/ELF/Arch/LoongArch.cpp` 的其他改动冲突。rebase 后要重跑第 2/3 步。

---

## 29.8 PR 描述草稿

```markdown
### Problem

`R_LARCH_RELAX` is paired with another relocation at the same offset, and the
LoongArch ELF ABI does not require the marker to follow the relocation it
annotates.  `lld`'s TLS LE relaxation only looked at `relocs[i + 1]`, so when
the marker of the `R_LARCH_TLS_LE_LO12_R` came first, the `lu12i.w`/`add.d`
relocations were still relaxed (their instructions removed) while the consumer
was not rewritten to use `$tp`.  The result reads a register that is no longer
initialized.

### Fix

Track the offset of the last TLS LE sequence whose address-producing
instructions were removed, and relax the consumer at `offset + 8` even when its
marker precedes it.  The sequence is now consistent regardless of marker
position.

### Test

`lld/test/ELF/loongarch-tls-le-relax-order.s` uses `yaml2obj` to build two
objects with identical instructions/relocation types/offsets/addends that differ
only in the order of the two relocations at offset 8.  Both must relax to
`addi.d $a0, $tp, 0; ret`.  The assembler cannot produce the hint-first order,
hence the YAML input.

Fixes #239812
Assisted-by: Codex (deepseek-v4.1-flash)
```

PR 描述**不要复制粘贴整段 diff**；reviewer 看的是“问题是否成立、修复是否最小、测试是否能防回归”。

---

## 29.9 reviewer 会问什么（按 lld 的惯例）

从 `lld/ELF/Arch/LoongArch.cpp` 最近 30 条提交的作者分布看，活跃贡献者集中在 Fangrui Song、Zhaoxin Yang、hev、wanglei（作者不等于 reviewer，但这些人最可能出现在 review 里）。可预期的意见：

| 可能意见 | 预备回答 |
|---|---|
| “ABI 真的允许 marker 在前吗？” | issue 已给出反例；`R_LARCH_RELAX` 是“同 offset 的配对标”，不是“后缀” |
| “为什么不改 `relaxable()`？” | 改它会波及所有 R_LARCH 松弛；本修复只处理 TLS LE 的顺序无关性 |
| “`+8` 是硬编码，能否用 `relocs[i]` 反查？” | 可以用上一次 `ADD_R` 的 offset 推，但当前写法更直接；如果 reviewer 要求，可改成记录 `MachineReloc` 索引 |
| “测试能否用 `.s`？” | 不行，汇编器固定 marker 在后；`yaml2obj` 是唯一能表达输入的方式 |
| “会不会误伤 canonical 顺序？” | 测试的 `CANONICAL` 前缀就是防这个 |
| “`remove == 4` 是否够 robust？” | 它表示“删了一条 4 字节指令”，与 `relaxTlsLe` 的语义一致 |
| “性能？” | 只多一个 `uint64_t` 比较，且在 relax 循环内，可忽略 |

**review 的核心不是“你写得对不对”，而是“你的不变量说清楚了吗”。** 本补丁的不变量是一句话：*生产地址的指令被删了，消费地址的指令就必须被改写*。

---

## 29.10 反直觉点

1. **behind 19 也要先 rebase 再开 PR**，不是“能编译就行”。
2. **回归测试可能不能用汇编器写**；`yaml2obj` 是正式工具，不是 hack。
3. **`Fixes #N` 必须带 `#`**，否则不会自动关联。
4. **`Assisted-by:` 是政策要求**（`llvm/docs/AIToolPolicy.md:25-26`），不是可选项。
5. **补丁越小越容易被接受**；24 行比“重构整个 relax 状态机”更容易 review。
6. **`relaxable()` 没改**：修 bug 不一定要改看起来“错”的那个函数。
7. **测试里两个符号必须产生完全相同的指令**，否则无法证明“只有顺序不同”。

---

## 29.11 本章证据

| 结论 | 来源 |
|---|---|
| 树状态 / ahead 1 behind 19 | `git rev-parse`、`git rev-list --count` 实测 |
| 改动 2 文件 +116/−3 | `git show --stat` |
| issue 标题/作者/label | `gh issue view 239812 --json` 实测 |
| `relaxable()` 只看下一个 | `lld/ELF/Arch/LoongArch.cpp:1105` |
| 修复游标与 `+8` | 同文件 `:1290-1367` |
| 测试 95 行 + yaml2obj | `wc -l`、测试头 |
| `Assisted-by` 政策 | `llvm/docs/AIToolPolicy.md:25-26` |
| reviewer 分布 | `git log upstream/main -30 -- lld/ELF/Arch/LoongArch.cpp` |

---

## 29.12 练习

1. 把 `tlsLeRelaxedAt` 改成记录 `size_t` 索引，重新跑测试，比较可读性。
2. 构造一个“marker 在 `ADD_R` 之前”的 YAML，验证现有补丁是否也能处理（提示：`relaxable` 对 `ADD_R` 也失效）。
3. 用 `git rebase upstream/main` 后重跑 lld 子集，记录是否冲突。
4. 给 PR 描述补一段 “Alternatives considered”，解释为什么不在 `scanSectionImpl` 阶段重排重定位。
5. 找一个 `[LoongArch]` 的 open issue，用本章流程写一份“根因假设 + 验证计划”，不要写代码。

---

## 29.13 下一步

最后一章讲“长期维护”：怎么 triage、怎么选 issue、怎么判断一个 bug 该归给后端还是归给 lld、以及如何避免成为“只会改测试”的贡献者。
