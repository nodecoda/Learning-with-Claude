# 第 18 章 常见错误与诊断树

> **本章目标**：把“InstructionSelect 出问题了”拆成可验证的层次：先判断是选错、没选、验证失败、编码失败还是只是变慢，再沿 `-global-isel-abort=1` → `-stop-after` → `-run-pass=instruction-select` → `-verify-machineinstrs` → asm/MC 的顺序缩小范围。
>
> **前置**：第 2、5、6、7 章，练习 A。**环境**：LLVM 23.1.4，AArch64 主线，Windows x86_64 官方预编译包（Optimized build，assertions 关闭）。本章标“实测”的输出都在这个包上跑过；需要 assertions 才能看到的输出会标“未运行”。

---

## 18.1 先分症状：七类现象，七个观察点

GlobalISel 的故障很少直接以“selector 错了”的形式出现。先分类，能避免在错误的层里改代码。

| 症状 | 最直观的现象 | 第一观察点 | 第一命令 |
| --- | --- | --- | --- |
| 选错 opcode | 期望 `ADDXrr`，得到 `SUBXrr`/`SBFMXri` | `instruction-select` 后的 MIR | `-run-pass=instruction-select` |
| 选错位宽 | 期望 `ADDXrr`，得到 `ADDWrr` | gMIR 的 LLT 与 bank | `-stop-after=regbankselect` |
| 静默 fallback | 输出仍有 `G_*`，退出码 0 | `failedISel` 字段 | `-global-isel-abort=1` |
| verifier 失败 | `Bad machine code: ...` | verifier 报错行 | `-verify-machineinstrs` |
| MC 编码失败/截断 | 汇编里的立即数或寄存器不对 | 最终 `.s` 或 `llvm-mc` | `llc ... -o -` |
| 崩溃 | assert、`LLVM ERROR`、栈回溯 | 崩溃点与当前 MI | assertions 构建 + `-debug-only` |
| 性能退化 | 多出 `mov`、多一条 load、整函数 fallback | 汇编差异 | `-time-passes` + diff |

每一类现象对应一个不同的证据面。“输出仍然是 `G_FADD`”只说明选择失败，不能直接推出“pattern 写错了”；失败可能来自 bank、类型、feature、立即数或 fold 安全性。

### 18.1.1 成功标志不是 `selected: true`

把一份 bank 故意写错的 MIR 喂给 `instruction-select`（实测）：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select fadd-wrongbank.mir -o -
```

函数头是：

```yaml
selected:        true
failedISel:      true
```

函数体仍然是 `%2:gpr(f32) = G_FADD %0, %1`。`selected: true` 只表示 pass 执行完了。真正的成功条件是 `selected: true`、`failedISel: false`，且函数体里没有 `G_*`。

---

## 18.2 原因分类：把现象映射到机制

| 原因 | 典型症状 | 机制 | 确认手段 |
| --- | --- | --- | --- |
| 缺 `GINodeEquiv` | 自定义 `G_*` 永远不选，fallback | 导入器不知道这个节点对应哪个 SDNode | 看生成的匹配表有没有该 opcode |
| pattern 太窄 | 本可选的形态走了 fallback 或多条指令 | 结构、类型或立即数谓词拒绝了合法输入 | `-debug-only=instruction-select` 看 reject 点 |
| pattern 太宽 | 选出无法编码/语义错误的指令 | 谓词没有覆盖编码约束 | 边界立即数 + MC/verifier |
| LLT 不匹配 | 规则完全不命中 | `GIM_CheckType` 精确比较 LLT | 对照 `regbankselect` 后的 `%n:bank(sN)` |
| bank 不匹配 | `G_FADD` 在 GPR bank 上失败 | `GIM_CheckRegBankForClass` 失败 | 看 `%n:gpr`/`%n:fpr` |
| 立即数谓词错 | 4095 能选、4096 不能，或反过来 | `ImmLeaf` 范围与编码能力不一致 | 跑边界值，读 `.td` 的 `ImmLeaf` |
| 忘记记录 `G_CONSTANT` | 常量没折进指令，出现 `MOV` | matcher 没把常量定义者拉进 `MIs` | 看匹配表是否有 `GIM_RecordInsn` |
| 多个 use 阻止 fold | 常量/中间值没折进去 | fold 需要删除生产者，多 use 时不安全 | 数 use，构造单 use 对照 |
| 内存/原子属性不匹配 | 普通 `LDR` 代替 `LDAR`，或反过来 | MMO 的 ordering/atomic 谓词不匹配 | 看 MMO 与最终汇编 |
| 隐式操作数缺失 | `missing implicit register operand` | 指令定义要求 `$nzcv` 等隐式操作数 | `-verify-machineinstrs` |
| regclass 约束失败 | `VReg has no regclass` / 大小不符 | 渲染后没有 `constrainRegClass` | assertions 构建 + selected MIR |

### 18.2.1 为什么“pattern 太宽”比“太窄”更危险

太窄通常只是性能问题：本来能一条指令完成，现在多一条 `mov` 或整段 fallback，结果仍然正确。太宽则可能选出一条编码上非法的指令。匹配器只负责“我认为这条规则适用”，真正的编码合法性由 MC 层负责；两层不一致时，轻则 verifier 报错，重则汇编器静默截断。

AArch64 的 `ADDXri` 是典型：立即数不是任意 64 位常量，而是 12 位无符号立即数，可选左移 12。`4096` 可以表示成 `1 << 12`，`4097` 不行。第 7 章已用 `4095/4096/5000` 演示过这个边界。

---

## 18.3 诊断树：从 abort 开始，逐层缩小

```text
[0] 复现：-global-isel-abort=1
      |
      +-- 报 cannot select --> [1] 确认阶段与函数属性
      |        +-- MIR 本身不合法 --> 修 MIR / 修前序 pass
      |        +-- MIR 合法 --> [2] 只看 instruction-select
      |                 +-- 仍失败 --> [3] 看 matcher reject 点
      |                 +-- 成功但 asm 不对 --> [5] 看 MC/asm
      |
      +-- 不报错但结果不对 --> [4] 看 selected MIR 与 verifier
      |
      +-- 只是变慢 --> [6] 看 fallback、fold、time-passes
```

### 第 0 步：让失败立刻停下

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -global-isel-abort=1 in.ll -o NUL
```

期望观察：失败时非零退出并打印 `LLVM ERROR: cannot select: ...`；成功时无额外输出。`-global-isel-abort` 的三个值（23.1.4 `--help-hidden`）：

```text
=0  Disable the abort
=1  Enable the abort
=2  Disable the abort but emit a diagnostic on failure
```

日常调试用 `1`；批量收集所有失败函数时用 `2`。不要用 `0` 判断“有没有 bug”，它会把失败藏进 fallback。

### 第 1 步：确认失败发生在哪个阶段

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=irtranslator in.ll -o 1-ir.mir
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=legalizer in.ll -o 2-legal.mir
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=regbankselect in.ll -o 3-rbs.mir
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=instruction-select in.ll -o 4-sel.mir
```

期望观察：`2-legal.mir` 的 `legalized` 为 `true`；`3-rbs.mir` 的 `regBankSelected` 为 `true`，每个 vreg 有 `:gpr` 或 `:fpr`；`4-sel.mir` 的 `failedISel` 为 `false` 且没有 `G_*`。如果失败在前三步，selector 只是最后一个受害者。

### 第 2 步：只跑 instruction-select

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 -verify-machineinstrs 3-rbs.mir -o 4-sel.mir
```

期望观察：

- 解析期错误：`registers`、`liveins`、内嵌 IR 名字不一致；
- `cannot select`：matcher 或 C++ switch 没有覆盖；
- verifier 错误：选出了指令，但操作数/隐式寄存器/regclass 不对；
- 成功：`failedISel: false`，`G_*` 消失。

这条命令排除了 IRTranslator、Legalizer、RegBankSelect，是定位“哪条指令选不出来”的最短路径。

### 第 3 步：看 matcher 在哪条规则上拒绝

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -debug-only=instruction-select 3-rbs.mir -o -
```

期望观察（示意）：`GIM_CheckOpcode ... Got=G_ADD` 之后，某条规则在 `GIM_CheckType` 或 `GIM_CheckRegBankForClass` 处 `Rejected`。如果所有规则都在 `GIM_CheckOpcode` 就失败，说明该 opcode 根本没进入导入表。

> **未运行**：本机是 Optimized build，`-debug-only` 输出被编译掉了。上面的格式来自第 6 章的源码阅读与上游调试输出。

没有 assertions 构建时，退而求其次：看 selected MIR 里残留的 `G_*`，再用 `.td` 对照。

### 第 4 步：读 selected MIR，不要直接读汇编

检查顺序：

1. `selected` 与 `failedISel`；
2. 每个 vreg 的 `class:` 是否为空；
3. 目标指令的操作数位宽是否与 LLT 一致；
4. 需要隐式操作数的指令是否带 `implicit $nzcv` 等；
5. 有没有残留 `G_*`。

### 第 5 步：再看 MC/asm

```console
llc -mtriple=aarch64-linux-gnu -O0 4-sel.mir -o - | tee 4-sel.s
```

如果 selected MIR 看起来正确、汇编不对，问题在 MC 编码、伪指令展开或汇编打印，不在 matcher。典型信号：立即数被截断成 0、寄存器从 `x` 变 `w`、伪指令没展开、汇编器报 `invalid operand`。

### 第 6 步：性能退化单独走一条路

```console
llc -mtriple=aarch64-linux-gnu -global-isel -O0 in.ll -o - > new.s
llc -mtriple=aarch64-linux-gnu -global-isel=false -O0 in.ll -o - > old.s
diff -u old.s new.s
llc -mtriple=aarch64-linux-gnu -global-isel -O0 -time-passes in.ll -o NUL
```

性能退化更多来自“多了一条指令”或“整函数 fallback”，而不是 matcher 本身慢。

---

## 18.4 反例一：bank 不匹配导致静默 fallback

输入是一份把 `f32` 放在 GPR bank 的 MIR（节选）：

```yaml
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
body:             |
  bb.1.entry:
    liveins: $s0, $s1

    %0:gpr(f32) = COPY $s0
    %1:gpr(f32) = COPY $s1
    %2:gpr(f32) = G_FADD %0, %1
    $s0 = COPY %2(f32)
    RET_ReallyLR implicit $s0
```

默认行为（实测）：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select fadd-wrongbank.mir -o -
```

```yaml
selected:        true
failedISel:      true
...
%2:gpr(f32) = G_FADD %0, %1
```

退出码是 0。加 `-global-isel-abort=1` 后（实测）：

```text
LLVM ERROR: cannot select: %2:gpr(f32) = G_FADD %0:gpr, %1:gpr (in function: fadd)
```

**根因**：AArch64 的 `G_FADD` 规则要求 FPR bank。`GIM_CheckRegBankForClass` 在 GPR bank 上失败，候选规则耗尽，`selectImpl` 返回 false；主 `switch (Opcode)` 没有 `G_FADD` 分支（它只在 `selectBinaryOp()` 的 FPR 分支里，bank 不对时不会被调用），最终整函数 fallback。

**修复**：不要在选择器里“兼容”错误的 bank。手写 MIR 改成 `%0:fpr(f32)`；编译器生成的则检查 RegBankSelect 的 mapping 和跨 bank `COPY`；pattern 问题则确认 `.td` 的寄存器类确实是 FPR。

---

## 18.5 反例二：位宽与 opcode

先看 IR：

```llvm
define i64 @add64_1(i64 %a) {
  %r = add i64 %a, 1
  ret i64 %r
}
define i32 @add32_1(i32 %a) {
  %r = add i32 %a, 1
  ret i32 %r
}
```

```console
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 widths.ll -o -
```

实测汇编：

```asm
add64_1:
    add x0, x0, #1
    ret
add32_1:
    add w0, w0, #1
    ret
```

这不是 bug，而是正确结果。真正的位宽 bug 出现在“IR 是 i64，gMIR 却是 s32”的情况。诊断：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=regbankselect widths.ll -o - | grep -E 'G_ADD|G_CONSTANT'
```

期望看到 `%2:gpr(s64) = G_ADD %0, %1`。如果看到 `s32`，往上游查 Legalizer/IRTranslator，而不是改 `ADDXrr` 的 pattern。看到 `gpr64sp` 也不要立刻认为 regclass 错了：`ADDXri` 允许 SP 作为基址，选择器会选更宽的类。

### 18.5.1 手工注入错误 opcode

这一节故意直接手写 MIR，把 `G_ZEXT` 改成 `G_SEXT`。内嵌 IR 仍写着 `zext`，但 selector 只信 MIR：

```yaml
%0:gpr(i32) = COPY $w0
%1:gpr(i64) = G_SEXT %0(i32)
$x0 = COPY %1(i64)
RET_ReallyLR implicit $x0
```

`G_ZEXT` 输出（实测）：

```yaml
%0:gpr32 = COPY $w0
%2:gpr32 = ORRWrs $wzr, %0, 0
%1:gpr64 = SUBREG_TO_REG %2, %subreg.sub_32
```

汇编：

```asm
mov  w8, w0
mov  w0, w8
ret
```

`G_SEXT` 输出（实测）：

```yaml
%0:gpr32 = COPY $w0
%2:gpr64all = IMPLICIT_DEF
%3:gpr64 = INSERT_SUBREG %2, %0, %subreg.sub_32
%1:gpr64 = SBFMXri %3, 0, 31
```

汇编：

```asm
mov   w8, w0
sxtw  x0, w8
ret
```

这说明 InstructionSelect 不会拿 IR 去交叉验证 MIR。诊断时先确认 `-stop-after=regbankselect` 的 `G_*` 是否与 IR 语义一致，再怀疑 pattern。这个例子是**手工注入**，不是 AArch64 selector 的真实 bug。

---

## 18.6 反例三：立即数边界，4095 / 4096 / 4097

```llvm
define i64 @add64_4095(i64 %a) {
  %r = add i64 %a, 4095
  ret i64 %r
}
define i64 @add64_4096(i64 %a) {
  %r = add i64 %a, 4096
  ret i64 %r
}
define i64 @add64_4097(i64 %a) {
  %r = add i64 %a, 4097
  ret i64 %r
}
```

```console
llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 imm.ll -o -
```

实测汇编：

```asm
add64_4095:
    add x0, x0, #4095
    ret
add64_4096:
    add x0, x0, #1, lsl #12      // =4096
    ret
add64_4097:
    mov w8, #4097                // =0x1001
    add x0, x0, x8
    ret
```

- `4095`：12 位无符号立即数，直接编码；
- `4096`：`1 << 12`，用 `imm12=1, shift=12` 编码；
- `4097`：不能用一个 12 位立即数加一个 12 位移位表示，必须先物化常量。

如果 pattern 太窄（只允许 `[0,4095]`），`4096` 会退化成“物化 + `ADDXrr`”，结果正确但多一条 `mov`，属于性能退化。如果 pattern 太宽（任意 `i64`），`4097` 可能被选成 `ADDXri`，matcher 通过但编码层只能截断或报错。

---

## 18.7 反例四：隐式操作数缺失，verifier 抓住

`CSELXr` 需要 `$nzcv`。手写 selected MIR 时漏掉：

```yaml
%3:gpr64 = CSELXr %0, %1, 1
```

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=finalize-isel -verify-machineinstrs missing-nzcv.mir -o -
```

实测报错：

```text
error: missing-nzcv.mir:34:32: missing implicit register operand 'implicit $nzcv'
    %3:gpr64 = CSELXr %0, %1, 1
                               ^
```

根因是指令定义的操作数列表包含隐式 `$nzcv`，渲染时没有 `addImplicitDefUseOperands` 或没有显式 `implicit $nzcv`。修复：补隐式操作数，或检查 `.td` 的 `Defs`/`Uses` 是否包含 `NZCV`。

---

## 18.8 反例五：MC 层不会替你兜底

构造 selected MIR，故意把 `ADDXri` 的立即数写成 `4096`、shift 写成 `0`：

```yaml
%0:gpr64sp = COPY $x0
%1:gpr64sp = ADDXri %0, 4096, 0
$x0 = COPY %1
```

```console
llc -mtriple=aarch64-linux-gnu -O0 badimm.mir -o -
```

实测输出：

```asm
add x0, x0, #0
ret
```

`4096` 变成 `#0`，没有报错，退出码 0。这说明 MC 编码器/汇编打印器不保证对非法 selected MIR 做完整验证；release 构建里某些非法立即数会被静默截断。因此“能生成汇编”不等于“选择正确”。这也是第 19 章要求 `-verify-machineinstrs` 和边界正负向测试的原因。

这个反例是**手工构造的 selected MIR**，不是 selector 正常生成的输出；它演示 MC 层边界，不是 selector bug。

---

## 18.9 原子/内存与多 use：两个容易误判的点

### 18.9.1 原子属性

```llvm
define i64 @load_mono(ptr %p) {
  %v = load atomic i64, ptr %p monotonic, align 8
  ret i64 %v
}
define i64 @load_acq(ptr %p) {
  %v = load atomic i64, ptr %p acquire, align 8
  ret i64 %v
}
define void @store_rel(ptr %p, i64 %v) {
  store atomic i64 %v, ptr %p release, align 8
  ret void
}
```

实测汇编：

```asm
load_mono:
    ldr x0, [x0]
    ret
load_acq:
    ldar x0, [x0]
    ret
store_rel:
    stlr x1, [x0]
    ret
```

`monotonic` 走普通 `LDR/STR`；`acquire`/`release` 及以上走 `LDAR/STLR`；128 位原子由 Legalizer 特判。如果 pattern 或 C++ 路径忽略 MMO，就可能把 `acquire` 选成普通 `ldr`。这类 bug 在 x86 上可能看不出来，在 AArch64 上是内存模型错误。诊断时必须看 MIR 的 `:: (load atomic ...)` 注释。

### 18.9.2 多 use 不是“绝对不 fold”

AArch64 的 `G_ADD` 可以把常量立即数复制到多条指令里。实测 MIR：

```yaml
%0:gpr(i64) = COPY $x0
%1:gpr(i64) = G_CONSTANT i64 1
%2:gpr(i64) = G_ADD %0, %1
$x0 = COPY %2(i64)
$x1 = COPY %1(i64)
```

输出：

```yaml
%0:gpr64sp = COPY $x0
%3:gpr32 = MOVi32imm 1
%1:gpr64all = SUBREG_TO_REG %3, %subreg.sub_32
%2:gpr64sp = ADDXri %0, 1, 0
$x0 = COPY %2
$x1 = COPY %1
```

`G_ADD` 仍然折了，`G_CONSTANT` 也保留下来给 `$x1`。多 use 影响的是“折叠后能否删除生产者”以及目标 pattern 的 `hasOneUse` 检查。真正需要担心的是某个 pattern 依赖 `GIM_CheckHasOneUse`，而你的 MIR 有两个 use，于是规则被拒绝，走了更保守的路径。

---

## 18.10 踩雷清单

> **崩溃说明**：本次没有在 release 构建中复现崩溃。官方预编译包把 assert 编译掉了，很多“本应 assert”的路径会变成静默错误；真正的崩溃通常需要 assertions 构建、调试器或 `-debug-only` 才能定位。不要把“没崩”当成“没问题”。

1. **只看 `selected: true`**。必须同时看 `failedISel` 和函数体里有没有 `G_*`。
2. **在 release 构建里期待 `-debug-only`**。官方预编译包没有调试输出；需要 assertions 构建。
3. **把 selected MIR 当成 selector 的真实输出**。手写 MIR 能演示症状，但不能证明 selector 会生成它。
4. **认为 fallback 是无害的**。fallback 可能让整函数退回 SelectionDAG，性能和行为都变了；`-global-isel-abort=1` 应成为默认调试开关。
5. **在 MC 层报错前就认定 matcher 错了**。先看 selected MIR；selected MIR 已经错了才是 selector 的问题。

---

## 18.11 进阶：把诊断做成可重复的实验

- **覆盖率**：assertions 构建可用 `-coverage` 观察匹配表命中；先确认期望规则是否真的被执行。
- **导入质量**：用 `llvm-tblgen -gen-global-isel` 生成 `.inc`，搜索目标 opcode；没有出现就查 `GINodeEquiv` 和 pattern 导入前提。
- **对照组**：同一份 IR 跑 `-global-isel=false`，比较 SelectionDAG 的选择；它能告诉你“合法输入本来应该长什么样”。
- **二分 feature**：用 `-mattr=+feat` / `-mattr=-feat` 各跑一次，判断是不是 feature predicate 把规则挡掉了。
- **二分 MIR**：把失败函数缩到一条指令，再手工修 bank/type/立即数，看哪一处改变让 `failedISel` 翻转。

---

## 18.12 动手练习

### 练习 18.1 复现静默 fallback

用手写 MIR 构造 `G_FADD` 在 GPR bank 上的函数，分别用 `-global-isel-abort=0/1/2` 跑，记录退出码、`failedISel` 和输出差异。

### 练习 18.2 位宽追踪

写 `i64 add` 与 `i32 add` 两个函数，导出 `regbankselect` 的 MIR，指出每个 `G_ADD` 的 LLT 和最终目标指令的位宽，解释 `gpr64sp` 出现的原因。

### 练习 18.3 立即数边界

把 `add i64 %a, N` 的 `N` 取 `4095/4096/4097/8191/8192`，先预测汇编，再跑 `llc` 验证。找出哪几个值需要物化常量。

### 练习 18.4 制造一次 verifier 失败

拿一份正常 selected MIR，删掉 `CSELXr` 的 `implicit $nzcv`，跑 `-verify-machineinstrs`，记录报错行号与修复后的差异。

### 练习 18.5 原子语义

把 `load atomic ... monotonic` 改成 `acquire`、`seq_cst`，观察汇编从 `ldr` 变成 `ldar` 的条件，并解释为什么不能只看 opcode。

---

## 18.13 重点整理

- 先分症状，再猜原因；七类症状对应七个观察点。
- `selected: true` 不等于成功，`failedISel: false` 且无 `G_*` 才是成功。
- 诊断顺序：`abort=1` → `stop-after` → 单跑 `instruction-select` → verifier → selected MIR → asm/MC。
- 缺 `GINodeEquiv`、pattern 太窄/太宽、LLT/bank 不匹配、立即数谓词错误、忘记记录 `G_CONSTANT`、多 use、atomic/内存属性、隐式操作数、regclass 约束，是十类高频根因。
- MC 层可能静默截断非法 selected MIR；能生成汇编不等于选择正确。
- `-debug-only` 需要 assertions 构建；release 构建只能靠 MIR/asm 和边界实验。

---

## 18.14 自我检核

1. 一份 MIR 同时有 `selected: true` 和 `failedISel: true`，发生了什么？
2. `-global-isel-abort=0/1/2` 分别适合什么场景？
3. 为什么 `G_FADD` 在 GPR bank 上会失败？修复点在哪一层？
4. `4096` 和 `4097` 在 AArch64 `ADDXri` 上的差别是什么？
5. 手写 selected MIR 的 `ADDXri 4096, 0` 为什么可能静默变成 `#0`？
6. 多 use 一定会阻止常量折叠吗？举一个反例。
7. verifier 报 `missing implicit register operand` 时，应该先查什么？

---

## 18.15 延伸阅读

- `llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`：失败路径、`failedISel` 与 fallback 的入口。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`：`select()`、`earlySelect()`、`selectCompareBranch()` 的真实分支。
- `llvm/lib/Target/AArch64/AArch64InstrFormats.td`：`ADDXri` 的立即数操作数与 `ImmLeaf` 边界。
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`：`handleReject()` 与各 `GIM_Check*` 的失败行为。
- 官方文档《InstructionSelect》与《Generic Opcode》：<https://llvm.org/docs/GlobalISel/InstructionSelect.html>

---

## 下一章

[第 19 章 质量与上游标准](./19-quality-upstream.md) —— 把本章的诊断经验变成 review checklist、lit 测试和 compile-time 预算：怎样证明一个 pattern 不仅“能跑”，而且在上游站得住。
