# 练习 D：本来该选 AND，却选了三条 MOV

> **目标**：拿到一个"汇编正确但选择明显更差"的真实案例，走完"复现 → 缩小 → 定位 pass 边界 → 定位 matcher 条件 → 修复与回归"的完整流程。
>
> **产出**：一份可复现记录 + 五组变体 + 一份根因结论 + 一个能抓住它的回归测试。
> **时间**：3–5 小时。**环境**：LLVM 23.1.4 官方 Windows 包（Optimized build），AArch64。

> **证据约定**：标【实测】的输出都在本机跑过；标【源码】的是从 23.1.4 源码摘的；标【推断】的是基于前两者的判断，没有通过改源码验证。

---

## D.1 题目

### 输入

```llvm
define i64 @f(i64 %x) {
  %r = and i64 %x, 4294967295
  ret i64 %r
}
```

### 命令

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 d_case.ll -o -
```

### 实际输出【实测】

```asm
f:                                      // @f
	.cfi_startproc
// %bb.0:
	mov	w8, w0
	mov	w8, w8
	mov	w0, w8
	ret
```

### 期望输出

```asm
f:                                      // @f
	.cfi_startproc
// %bb.0:
	and	x0, x0, #0xffffffff
	ret
```

这是 SelectionDAG 在同一份 IR、同一 `-O0` 下的真实输出【实测】。

### 约束

| 约束 | 说明 |
| --- | --- |
| 正确性 | 结果必须等于 `zext(trunc(x))`；三条 mov 在语义上是对的 |
| 不能 fallback | `-global-isel-abort=1` 必须通过；不能靠 SelectionDAG 补救 |
| 目标 | 一条 `ANDXri`（逻辑立即数 `0xFFFFFFFF` 是合法编码） |
| 优化级别 | `-O0`，不能靠 `-O1` 的 combiner 掩盖 |
| 寄存器 | 全程 GPR，不涉及 FPR |

### 先回答三个问题，再往下看

1. 这是**选择失败**（fallback）还是**选择成功但选差了**？
2. 三条 mov 分别对应 MIR 里的哪条指令？
3. 如果你只跑 `llc -O0`（不加 `-global-isel`），输出会变吗？

---

## D.2 复现与第一轮排查

### D.2.1 先确认它没有 fallback【实测】

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 d_case.ll -o NUL
$ echo $LASTEXITCODE
0
```

`abort=1` 通过，说明 InstructionSelect **认为自己成功了**。再看选择后的 MIR：

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -stop-after=instruction-select d_case.ll -o - | findstr /C:"selected:" /C:"failedISel:"
selected:        true
failedISel:      false
```

### D.2.2 再确认它不是非法 codegen【实测】

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 -verify-machineinstrs d_case.ll -o NUL
$ echo $LASTEXITCODE
0
```

verifier 也不报错。于是可以排除三类问题：

- 不是 Legalizer 拒绝（没有 `unable to legalize`）；
- 不是 RegBankSelect 没分 bank（`abort=1` 能选出来）；
- 不是寄存器类/大小不合法（verifier 通过）。

**这是一次"合法但劣质"的选择。** 这类问题不会让 CI 变红，只会让代码变慢，所以最容易被忽略。

### D.2.3 用 `-print-after` 抓选择瞬间【实测】

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -O0 \
      -print-before=instruction-select -print-after=instruction-select d_case.ll -o NUL
```

关键两段：

```text
# *** IR Dump Before InstructionSelect (instruction-select) ***:
# Machine code for function f: IsSSA, TracksLiveness, Legalized, RegBankSelected
  %0:gpr(i64) = COPY $x0
  %1:gpr(i64) = G_CONSTANT i64 4294967295
  %2:gpr(i64) = G_AND %0:gpr, %1:gpr
  $x0 = COPY %2:gpr(i64)
# *** IR Dump After InstructionSelect (instruction-select) ***:
# Machine code for function f: IsSSA, TracksLiveness, Legalized, RegBankSelected, Selected
  %0:gpr64 = COPY $x0
  %3:gpr32 = COPY %0.sub_32
  %4:gpr32 = ORRWrr $wzr, %3
  %2:gpr64all = SUBREG_TO_REG %4, %subreg.sub_32
  $x0 = COPY %2
```

这一段比汇编更值钱：它明确告诉你"`G_AND` 被替换成了 `SUBREG_TO_REG(ORRWrr)`"，而不是"编译器某处插了两条 mov"。

---

## D.3 五组变体：先预测，再跑

每组都先写下预测，再执行。预测错了的地方，才是你真正学到的东西。

五组变体的结论先放在这里，细节在下面：

| 变体 | 变量 | 是否改变输出 | 说明 |
| --- | --- | --- | --- |
| V1 | 立即数 | ✅ | 只有 0xFFFFFFFF 触发 |
| V2 | 类型 i32/i64 | ✅ | 只有 i64 分歧 |
| V3 | use 数 | ❌ | 两条 mov 共享，不随 use 增长 |
| V4 | bank（FPR→GPR） | ❌ | 只多一条 `fmov` |
| V5 | feature / 优化级别 | 部分 | `-mattr` 无影响；`-O1` 变成一条 mov |

### V1 立即数边界

对每个常量各跑一次（把 `4294967295` 依次替换）：

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 d_case.ll -o -
```

实际结果【实测】：

| 常量 | GlobalISel `-O0` 输出 | 说明 |
| --- | --- | --- |
| 4294967294（0xFFFFFFFE） | `and x0, x0, #0xfffffffe` | 普通逻辑立即数 |
| **4294967295（0xFFFFFFFF）** | `mov w8, w0` / `mov w8, w8` / `mov w0, w8` | 命中特殊规则 |
| 4294967296（0x1_00000000） | `and x0, x0, #0x100000000` | 逻辑立即数 |
| -4294967296（0xFFFFFFFF00000000） | `and x0, x0, #0xffffffff00000000` | 逻辑立即数 |

**观察**：只有"低 32 位全 1、高 32 位全 0"这一个值出问题。它恰好是 `(and x, 0xFFFFFFFF)` 这个特殊 pattern 的匹配条件。相邻的两个值都走通用 `ANDXri` 路径，说明这不是"逻辑立即数编码不了"，而是**有一条更优先的规则**。

### V2 类型：i32 与 i64

i32 版本：

```llvm
define i32 @g(i32 %x) {
  %r = and i32 %x, -1
  ret i32 %r
}
```

实际输出【实测】：

```asm
	mov	w8, #-1                         // =0xffffffff
	and	w0, w0, w8
	ret
```

SelectionDAG 在同一输入下也是这两条【实测】，所以 **i32 没有分歧**。分歧只存在于 `i64`：只有 64 位版本才会触发"`and` 被解释成 zext"的那条规则。

### V3 多个 use

```llvm
define i64 @h(i64 %x) {
  %a = and i64 %x, 4294967295
  %b = add i64 %a, 1
  %c = mul i64 %a, 3
  %r = add i64 %b, %c
  ret i64 %r
}
```

实际输出【实测】：

```asm
	mov	w8, w0
	mov	w8, w8
                                        // kill: def $x8 killed $w8
	add	x10, x8, #1
	mov	w9, #3
	umaddl	x0, w8, w9, x10
	ret
```

**观察**：

- 两条 `mov` 只生成一次，被两个 use 共享，没有随 use 数线性增长；
- `mul %a, 3` 被选成 `umaddl`，说明 `%a` 的"已知 zext"属性被后续 matcher 利用了；
- 如果只看单 use 版本，你不会知道这条规则还承担着"喂给 UMADDL"的职责——这正是修复时最容易踩的坑。

### V4 bank：从 FPR 来

```llvm
define i64 @k(double %d) {
  %x = bitcast double %d to i64
  %r = and i64 %x, 4294967295
  ret i64 %r
}
```

选择前 MIR【实测】：

```yaml
  bb.1 (%ir-block.0):
    %0:fpr(f64) = COPY $d0
    %1:fpr(i64) = G_BITCAST %0(f64)
    %2:gpr(i64) = G_CONSTANT i64 4294967295
    %4:gpr(i64) = COPY %1(i64)
    %3:gpr(i64) = G_AND %4, %2
    $x0 = COPY %3(i64)
```

实际汇编【实测】：

```asm
	fmov	x8, d0
                                        // kill: def $w8 killed $w8 killed $x8
	mov	w8, w8
	mov	w0, w8
	ret
```

**观察**：RegBankSelect 正确地插入了跨 bank COPY（`fmov`），`G_AND` 的操作数已经是 `gpr`。**bank 不是原因**；三条 mov 的形态甚至因为 COPY 合并少了一条。这一步的价值是排除 bank 嫌疑。

### V5 feature 与优化级别

| 变体 | 命令 | 结果【实测】 |
| --- | --- | --- |
| 加一个无关 feature | `-mattr=+lse` | 三条 mov，**完全不变** |
| `-O1` | `-global-isel -O1` | `mov w0, w0` + `ret` |
| `-O2` | `-global-isel -O2` | `mov w0, w0` + `ret` |
| 默认 triple（不写 `-global-isel`） | `llc -O0` | 三条 mov（AArch64 `-O0` 默认走 GlobalISel） |
| 强制 SelectionDAG | `-global-isel=0 -O0` | `and x0, x0, #0xffffffff` |

**观察**：

- `-mattr` 不影响，说明不是 feature 谓词；
- `-O1` 只剩一条 `mov w0, w0`，说明后续 pass（或 copy 合并）能消掉两条，但**没有把它优化回 `and`**；
- 默认 triple 也是三条 mov，说明这是 AArch64 `-O0` 的默认体验，不是"忘了开 GlobalISel"。

---

## D.4 五步根因分析

### 第 1 步：复现并固定变量

记录四件事：triple、优化级别、abort 模式、退出码。

```text
$ llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 d_case.ll -o -
$ echo $LASTEXITCODE
0
```

用 `-stop-after=instruction-select` 存一份 MIR，后面所有对比都以它为基准。

### 第 2 步：缩小

`llvm-reduce` 在官方包里可用，但 `--test` 需要一个可执行文件。本机实测：传 `.bat` 或 `python check.py` 都会报

```text
error: running interesting-ness test: program not executable
```

编译一个 20 行的 C 包装器（跑 `llc`，输出含 `mov\tw8, w8` 就退出 0），然后：

```text
$ llvm-reduce --test=C:\isel-lab\interesting.exe C:\isel-lab\red\t.ll -o C:\isel-lab\red\out.ll
...
 **** SUCCESS | Saved new best reduction to C:\isel-lab\red\out.ll
Done reducing! Reduced testcase: C:\isel-lab\red\out.ll
```

结果【实测】：

```llvm
define i64 @keep(i64 %x) {
  %r = and i64 %x, 4294967295
  ret i64 %r
}
```

三个函数缩到一个，噪声（`mul`、`xor`、额外参数）全没了。注意 interestingness 的退出码方向：**0 = interesting**。

### 第 3 步：看 pass 边界

四个阶段的真实 MIR【实测】：

```yaml
# irtranslator
%0:_(i64) = COPY $x0
%1:_(i64) = G_CONSTANT i64 4294967295
%2:_(i64) = G_AND %0, %1
$x0 = COPY %2(i64)

# legalizer —— 与 irtranslator 完全相同
%0:_(i64) = COPY $x0
%1:_(i64) = G_CONSTANT i64 4294967295
%2:_(i64) = G_AND %0, %1
$x0 = COPY %2(i64)

# regbankselect —— 只是加上了 bank
%0:gpr(i64) = COPY $x0
%1:gpr(i64) = G_CONSTANT i64 4294967295
%2:gpr(i64) = G_AND %0, %1
$x0 = COPY %2(i64)

# instruction-select
%0:gpr64 = COPY $x0
%3:gpr32 = COPY %0.sub_32
%4:gpr32 = ORRWrr $wzr, %3
%2:gpr64all = SUBREG_TO_REG %4, %subreg.sub_32
$x0 = COPY %2
```

结论非常干净：**`G_AND` 原封不动地活到了 InstructionSelect**。Legalizer 没动它，RegBankSelect 只加了 bank。问题 100% 在这一 pass 内。

如果第 3 步发现 `G_AND` 在 legalizer 就变成了别的东西，后面两步都不用做——这就是"先看边界"的价值。

### 第 4 步：看 matcher 条件

选择后的三条指令是：

```yaml
%3:gpr32 = COPY %0.sub_32
%4:gpr32 = ORRWrr $wzr, %3
%2:gpr64all = SUBREG_TO_REG %4, %subreg.sub_32
```

`ORRWrr $wzr, x` 就是"把 32 位值原样搬一遍"，`SUBREG_TO_REG` 就是零扩展。换句话说，matcher 把 `and x, 0xFFFFFFFF` 理解成了 `zext(trunc(x))`，而不是一条 `ANDXri`。

源码里能直接找到这条规则（`AArch64InstrInfo.td`）【源码】：

```tablegen
// Emit (and 0xFFFFFFFF) as a ORRWrr move which may be eliminated.
let AddedComplexity = 6 in
def : Pat<(i64 (and GPR64:$Rn, 0xffffffff)),
          (SUBREG_TO_REG (ORRWrr WZR, (EXTRACT_SUBREG GPR64:$Rn, sub_32)), sub_32)>;
```

三个关键点：

1. **`AddedComplexity = 6`** 让它压过通用的"AND + 逻辑立即数"规则。
2. **注释写明意图**："which may be eliminated"——这是 SelectionDAG 时代的假设：`ORRWrr WZR, x` 这种 move 会被后续 copy 合并消掉。
3. GlobalISel 也会导入这条 pattern，并且按同样的复杂度排序。`GlobalISelEmitter.cpp`【源码】：

```cpp
  int Score = P.getPatternComplexity(CGP);
  ...
  llvm::stable_sort(Rules, [&](const RuleMatcher &A, const RuleMatcher &B) {
```

而 `getPatternComplexity` 的定义是【源码】：

```cpp
int PatternToMatch::getPatternComplexity(const CodeGenDAGPatterns &CGP) const {
  return getPatternSize(getSrcPattern(), CGP) + getAddedComplexity();
}
```

所以 `+6` 在 GlobalISel 的规则排序里同样生效。**推断**：这条规则赢了通用 `ANDXri` 规则，于是 `selectImpl` 成功返回，`abort=1` 也不会报错。

反向验证这个推断：把 mask 换成 0xFFFFFFFE，规则不再匹配，输出立刻变成 `and x0, x0, #0xfffffffe`【实测】；换回 0xFFFFFFFF 又变回三条 mov。一个立即数之差切换两条完全不同的路径，说明命中点就是这条 pattern，而不是某个通用的"64 位 AND 处理"。

为什么 SDAG 上没这个问题？本机实测 SDAG 的输出只有一条 `and x0, x0, #0xffffffff`，那条 `ORRWrr` 在 SDAG 路径上被消掉了。GlobalISel `-O0` 的 `-time-passes` 显示用的是 Fast Register Allocator，实测两条 move 都留了下来；具体由哪个 pass 消除，本条没有继续深挖。

### 第 5 步：修复与回归测试

**先明确：不要为了这一个 case 删掉整条 pattern。** V3 显示它还承担着"把 64 位值标记成已知 zext、喂给 UMADDL"的职责；直接删可能让 `mul` 的代码生成变差，甚至让某些 pattern 不再匹配。

三个方向，按风险从低到高：

1. **让这条规则不进 GlobalISel**：给该 `Pat` 加 `GISelShouldIgnore = 1`（字段真实存在，`TargetSelectionDAG.td` 中定义，emitter 里有 `if (Pat.getGISelShouldIgnore())` 跳过）。风险：依赖它做 UMADDL 匹配的路径要另找规则。
2. **改渲染**：让 GlobalISel 走这条规则时直接生成 `SUBREG_TO_REG(COPY)`，少一条 `ORRWrr`。风险：SDAG 侧的输出可能变化。
3. **加 post-select 合并**：在 AArch64 的 post-select 阶段把 `SUBREG_TO_REG(ORRWrr WZR, X.sub_32)` 折回 `ANDXri` 或 `COPY`。风险：要证明对所有 use 都安全。

三者都**没有在本机验证**（需要重新构建 LLVM），属于方向建议，不是已完成的修复。

回归测试（MIR 层，能精确抓住它）。注意：这条测试在未修复的 23.1.4 上会失败，它是**修复后的验收测试**，不是现状快照：

```text
# RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 -O0 -run-pass=instruction-select %s -o - | FileCheck %s

# CHECK:      selected:        true
# CHECK:      failedISel:      false
# CHECK-NOT:  ORRWrr
# CHECK:      ANDXri
```

**别在 `ANDXri` 后面写 `4294967295`**。MIR 打印的是编码后的 13 位位掩码立即数，不是逻辑值：本机实测 `and i64 %x, 0xFFFFFFFE` 在选择后打印为 `ANDXri %0, 8158`。想断言具体立即数，先在目标版本上导出一次 MIR 再抄；否则只断言 `ANDXri` 与 `CHECK-NOT: ORRWrr` 就够了。

或者 IR 层（更贴近用户可见行为）：

```text
; CHECK-LABEL: f:
; CHECK-NOT:    mov w8, w8
; CHECK:        and x0, x0, #0xffffffff
```

`CHECK-NOT: ORRWrr` 是关键：只断言"有一条 `and`"不够，因为未来可能既生成 `and` 又保留多余的 move。

---

## D.5 参考解答

<details>
<summary>点开看完整结论（先自己做一遍）</summary>

### 结论

`and i64 %x, 0xFFFFFFFF` 在 AArch64 GlobalISel `-O0` 下被选成

```text
SUBREG_TO_REG (ORRWrr WZR, (EXTRACT_SUBREG x, sub_32)), sub_32
```

而不是 `ANDXri x, x, #0xffffffff`。原因是 `AArch64InstrInfo.td` 中一条 `AddedComplexity = 6` 的 SelectionDAG pattern 被导入到 GlobalISel matcher，并因复杂度更高而优先命中。这条 pattern 的设计假设（`ORRWrr` move 会被消除）在 GlobalISel `-O0` 的 FastRA 下不成立，于是留下两条多余的 `mov`。

### 证据链

| 结论 | 证据 | 等级 |
| --- | --- | --- |
| 不是 fallback | `abort=1` 退出 0，`failedISel: false` | 实测 |
| 不是 legalizer/regbankselect | 四阶段 MIR 中 `G_AND` 原样存活 | 实测 |
| 是 matcher 选了另一条规则 | 选择后是 `ORRWrr` + `SUBREG_TO_REG` | 实测 |
| 规则来自 `AArch64InstrInfo.td` | 源码 pattern 的 RHS 形状与输出完全一致 | 源码 |
| 复杂度排序让它在 GISel 也优先 | emitter 用 `getPatternSize + getAddedComplexity` 排序 | 源码 |
| SDAG 假设 move 会被消掉 | pattern 上方注释 + SDAG 实际输出只有一条 `and` | 源码 + 实测 |
| 具体是哪条规则命中 | 没有 `-debug-only`（Optimized build），没做源码级验证 | 推断 |

### 为什么不是"逻辑立即数编码不了"

0xFFFFFFFF 是合法的 AArch64 逻辑立即数（64 位元素、32 个 1、`immr=0`），相邻的 0xFFFFFFFE 和 0x100000000 都能生成 `ANDXri`。这条规则是**主动选择**，不是编码器拒绝。

### 正确修复的判断标准

- 输出必须只剩一条 `ANDXri`（或一条等价的 32 位 move）；
- V3 的 `umaddl` 不能退化；
- `-global-isel-abort=1` 与 `-verify-machineinstrs` 都要通过；
- 要有 `CHECK-NOT: ORRWrr` 的回归测试。

### 如果是你向上游报 bug

一个最小、可行动的 report 应该包含：

1. 一段能直接 `llc` 的 IR（就是 D.1 那五行）；
2. 实际输出与期望输出（贴汇编，说明期望来自同版本 SelectionDAG）；
3. 版本与 triple（`llc --version` 的 `Optimized build` 也要写，方便对方判断你能不能跑 `-debug-only`）；
4. 你已经排除的层：`abort=1` 通过、`-verify-machineinstrs` 通过、四阶段 MIR 显示 `G_AND` 原样到达 ISel；
5. 你怀疑的规则（`AArch64InstrInfo.td` 里那条 `AddedComplexity = 6`），并明确标注这是推断。

</details>

---

## D.6 常见卡点

1. **看到三条 mov 就以为 fallback**。先跑 `abort=1`；通过就说明选择成功，方向是"选差了"而不是"选不出来"。
2. **直接改 pattern 的 `AddedComplexity`**。降到 0 也许能让 `ANDXri` 赢，但会同时改变 SDAG 的规则顺序，回归面比你想象的大。
3. **只测单 use**。V3 显示两个 use 共享同一次 move；只测单 use 会误判"删掉规则没影响"。
4. **用 `-O1` 的输出来证明修好了**。`-O1` 本来就会把三条 mov 压成一条 `mov w0, w0`，它掩盖了问题，而不是修复了问题。
5. **在 Optimized build 上找 `-debug-only`**。参数不存在，见第 17 章；要么构建 assertions，要么用 `-print-after` + 源码推断。

---

## D.7 延伸挑战

**挑战 1**：把 mask 换成 `0xFFFFFFFF00000000`，预测并验证 GlobalISel 的输出。它为什么不受这条规则影响？

**挑战 2**：构造一个 `mul i64 (and i64 %x, 0xFFFFFFFF), 3`，对比 GlobalISel 与 SelectionDAG。两条 `mov` 是否仍然存在？`umaddl` 是否仍然生成？

**挑战 3**：在源码构建里给这条 pattern 加 `GISelShouldIgnore = 1`，重新生成 `AArch64GenGlobalISel.inc`，跑 AArch64 GlobalISel 测试套件，统计有多少测试变化。这是"修一条规则要付多少代价"的真实答案。

**挑战 4**：写一个 `-run-pass=instruction-select` 的 MIR 测试，同时断言 `ANDXri` 存在、`ORRWrr` 不存在、且 `umaddl` 场景仍然正确。

---

## D.8 结论与边界

- 这是一个**真实存在**的 AArch64 GlobalISel `-O0` 选择质量问题【实测】。
- 根因定位到一条从 SelectionDAG 导入的 pattern【源码 + 实测输出形状】，但"具体哪条规则在 matcher 表里胜出"没有用 `-debug-only` 直接验证【推断】。
- 修复方向有三条，均未在本机实施【未验证】。
- 这类问题不会让测试变红，只会让代码变慢，所以回归测试必须写 `CHECK-NOT`，而不是只写"应该有 `and`"。

下一章进入 AArch64 源码全走读：把本练习的诊断路径对应到 `select()`、`selectImpl()` 和真实的 `.td` pattern；上游质量标准已在第 19 章展开。

[第 20 章 AArch64 源码全走读](./20-aarch64-source-walkthrough.md)
