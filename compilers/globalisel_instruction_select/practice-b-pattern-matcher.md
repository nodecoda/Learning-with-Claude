# 练习 B：改一个 pattern，预测 matcher 命中

> **目标**：给定几组 AArch64 IR/MIR，先预测 InstructionSelect 的输出，再只改一条 TableGen pattern 的立即数谓词，观察 matcher 命中如何变化。
>
> **产出**：一份“预测 vs 实际”表、一处最小 patch、一份失败/成功对照记录。**时间**：3–5 小时。**环境**：LLVM 23.1.4，AArch64，预编译 `llc`；若要真正重建 pattern，另需同版本源码与构建目录。

---

## B.0 任务规格

你要完成三件事：

1. 对下面 5 组用例，先写下选择结果（汇编 + 关键 MIR），再跑 `llc` 验证。
2. 修改 AArch64 的 64 位 `ashr` 立即数 pattern，把允许的立即数范围从 `imm0_63` 缩小到 `imm0_3`。
3. 预测并验证：哪些输入仍然命中 `SBFMXri`，哪些输入改走变量移位 `ASRVXr`，以及常量如何被物化。

**主实验对象是 `ashr`，不是 `shl`。** 原因见 B.3.1：AArch64 的 `earlySelectSHL` 会在 TableGen 之前截获所有 0–63 的 `shl` 立即数；改 `shl` pattern 很可能看不到任何变化。这个“改错地方”的坑本身就是本练习的一部分。

### B.0.1 成功标准

- 5 组用例都有“预测”和“实际”两列；
- 修改只涉及一个 pattern 的三处 `imm0_63`；
- 能解释 `ashr 3` 为什么仍命中、`ashr 4` 为什么不再命中；
- 能指出 `ASRVXr` 是变量移位 pattern，不是 `SBFMXri` 的某种编码；
- 能说明哪些结论是实跑，哪些是“基于实跑路径的理论预期”。

---

## B.1 准备实验目录

在仓库根目录执行：

```console
New-Item -ItemType Directory -Force .scratch/pb2 | Out-Null
```

设一个变量，后面反复使用：

```console
$llc = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin/llc.exe"
```

三条通用命令（PowerShell 写成一行）：

```console
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 in.ll -o -
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 -stop-after=regbankselect in.ll -o rbs.mir
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 -stop-after=instruction-select in.ll -o isel.mir
```

判断选择成功的唯一标准仍是：

```yaml
selected:   true
failedISel: false
```

`-global-isel-abort=1` 让失败变成 fatal，避免你误把 fallback 当成 matcher 命中。

---

## B.2 五组测试用例

先不要跑。把每组的预测写进 B.2.6 的表，再逐组验证。

### B.2.1 正常：`ashr i64 %a, 3`

```llvm
define i64 @ashr3(i64 %a) {
entry:
  %r = ashr i64 %a, 3
  ret i64 %r
}
```

RBS（实跑）：

```yaml
%0:gpr(i64) = COPY $x0
%1:gpr(i64) = G_CONSTANT i64 3
%2:gpr(i64) = G_ASHR %0, %1(i64)
```

ISel（实跑）：

```yaml
%0:gpr64 = COPY $x0
%2:gpr64 = SBFMXri %0, 3, 63
```

汇编（实跑）：

```asm
asr     x0, x0, #3
ret
```

要预测的是：`MIs[0]` 是 `G_ASHR`，`MIs[1]` 是 `G_CONSTANT 3`；`imm0_63` 通过，`SBFMXri` 的 `immr=3, imms=63` 正好编码算术右移 3。

### B.2.2 边界立即数：`ashr 4`、`ashr 5`、`ashr 63`

三份 IR 只改立即数：

```llvm
define i64 @ashr4(i64 %a) {
entry:
  %r = ashr i64 %a, 4
  ret i64 %r
}
```

`ashr5`、`ashr63` 同理。实跑基线：

| IR | ISel 关键指令 | 汇编 |
| --- | --- | --- |
| `ashr 4` | `%2:gpr64 = SBFMXri %0, 4, 63` | `asr x0, x0, #4` |
| `ashr 5` | `%2:gpr64 = SBFMXri %0, 5, 63` | `asr x0, x0, #5` |
| `ashr 63` | `%2:gpr64 = SBFMXri %0, 63, 63` | `asr x0, x0, #63` |

再加一组算术立即数作为“对照”，它们走的是 `ADDXri`，不是 `SBFMXri`：

| IR | 汇编（实跑） |
| --- | --- |
| `add i64 %a, 4095` | `add x0, x0, #4095` |
| `add i64 %a, 4096` | `add x0, x0, #1, lsl #12` |
| `add i64 %a, 4097` | `mov w8, #4097` 后接 `add x0, x0, x8` |
| `and i64 %a, 255` | `and x0, x0, #0xff` |
| `and i64 %a, 257` | `mov w8, #257` 后接 `and x0, x0, x8` |

**边界组的重点**：`ashr 4` 是 patch 后的第一个“不再命中”的输入；`add`/`and` 组用于确认你没有把“所有立即数 pattern”都改坏。

### B.2.3 bank 不匹配：`G_FADD` 放在 `gpr`

下面是一份最小 MIR。它已经合法、已分 bank，但把 `f32` 的 vreg 故意放进了 `gpr`：

```yaml
--- |
  define float @fadd(float %a, float %b) {
  entry:
    %r = fadd float %a, %b
    ret float %r
  }
...
---
name:            fadd
alignment:       4
legalized:       true
regBankSelected: true
selected:        false
failedISel:      false
tracksRegLiveness: true
isSSA:           true
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }
liveins:
  - { reg: '$s0', virtual-reg: '' }
  - { reg: '$s1', virtual-reg: '' }
body:             |
  bb.1.entry:
    liveins: $s0, $s1

    %0:gpr(f32) = COPY $s0
    %1:gpr(f32) = COPY $s1
    %2:gpr(f32) = G_FADD %0, %1
    $s0 = COPY %2(f32)
    RET_ReallyLR implicit $s0
...
```

运行：

```console
& $llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select -pass-remarks-missed=gisel-select fadd-wrongbank.mir -o -
```

实跑输出（关键行）：

```text
remark: <unknown>:0:0: cannot select: %2:gpr(f32) = G_FADD %0:gpr, %1:gpr (in function: fadd)
selected:        true
failedISel:      true
%2:gpr(f32) = G_FADD %0, %1
```

问题不是“没有 `fadd` pattern”，而是 pattern 要求 FPR bank，`gpr(f32)` 过不了 bank 检查。`selected: true` 仍然出现，原因见第 10 章。

### B.2.4 多个 use：`sext` 被两个 `add` 使用

```llvm
define i64 @f(i64 %a, i64 %c, i32 %b) {
  %e = sext i32 %b to i64
  %r1 = add i64 %a, %e
  %r2 = add i64 %c, %e
  %r = add i64 %r1, %r2
  ret i64 %r
}
```

实跑汇编：

```asm
mov     w8, w2
sxtw    x9, w8
add     x8, x0, x9
add     x9, x1, x9
add     x0, x8, x9
ret
```

`sxtw` 只出现一次，两个 `add` 都是 `ADDXrr`，没有把 `G_SEXT` 折进 `ADDXrx`（汇编里省略了一行 `implicit-def: $x8` 注释）。把两个 `add` 改成一个，输出会变成 `add x0, x0, w1, sxtw`。这说明“多 use 阻止折叠”不一定来自 `GIM_CheckHasOneUse`，也可能来自 C++ 的 one-use 启发式。

### B.2.5 feature gating：`fadd half` 与 `+fullfp16`

```llvm
define half @f(half %a, half %b) {
  %r = fadd half %a, %b
  ret half %r
}
```

不带 `+fullfp16` 的 RBS（实跑）：

```yaml
%0:fpr(f16) = COPY $h0
%1:fpr(f16) = COPY $h1
%3:fpr(f32) = G_FPEXT %0(f16)
%4:fpr(f32) = G_FPEXT %1(f16)
%5:fpr(f32) = G_FADD %3, %4
%2:fpr(f16) = G_FPTRUNC %5(f32)
```

汇编（实跑）：

```asm
fcvt    s0, h0
fcvt    s1, h1
fadd    s0, s0, s1
fcvt    h0, s0
ret
```

加 `-mattr=+fullfp16` 后，RBS 变成：

```yaml
%0:fpr(f16) = COPY $h0
%1:fpr(f16) = COPY $h1
%2:fpr(f16) = G_FADD %0, %1
```

汇编（实跑）：

```asm
fadd    h0, h0, h1
ret
```

**feature gating 可能发生在 Legalizer，而不是 matcher。** 看到 `G_FPEXT`/`G_FPTRUNC` 时，先查 `-mattr` 和 Legalizer，不要直接去改 `fadd` pattern。

### B.2.6 先填预测表

| # | 用例 | patch 前预测 | patch 后预测 | 实际（跑完再填） |
| --- | --- | --- | --- | --- |
| 1 | `ashr 3` |  |  |  |
| 2 | `ashr 4` |  |  |  |
| 3 | `ashr 5` |  |  |  |
| 4 | `ashr 63` |  |  |  |
| 5 | `add 4095/4096/4097` |  |  |  |
| 6 | `and 255/257` |  |  |  |
| 7 | `fadd` on `gpr` |  |  |  |
| 8 | `sext` two uses |  |  |  |
| 9 | `fadd half` no feature |  |  |  |
| 10 | `fadd half` +fullfp16 |  |  |  |

预测时至少写清楚：最终指令名、是否出现常量物化、`selected`/`failedISel`。

---

## B.3 修改任务：把 `ashr` 的立即数谓词缩到 0–3

### B.3.1 为什么不用 `shl`

`AArch64InstructionSelector::earlySelectSHL` 在 imported selector 之前运行：

```cpp
auto VRegAndVal = getIConstantVRegVal(MO.getReg(), MRI);
if (!VRegAndVal)
  return false;
...
auto Imm1Fn = Is64Bit ? selectShiftA_64(MO) : selectShiftA_32(MO);
auto Imm2Fn = Is64Bit ? selectShiftB_64(MO) : selectShiftB_32(MO);
```

`selectShiftA_64`/`selectShiftB_64` 只拒绝 `> 63` 的立即数。因此 `shl 4`、`shl 63` 根本不依赖 TableGen 的 `imm0_63` pattern；即使你把那条 pattern 改成 `imm0_3`，C++ 仍然会先把它选成 `UBFMXri`。**这个练习要验证 matcher 命中，必须选一个没有 C++ 提前截获的 opcode。**

`G_ASHR` 不在提前选择列表里，走 imported matcher，适合做实验。

### B.3.2 目标 pattern

`AArch64InstrInfo.td` 里的 64 位算术右移立即数规则：

```tablegen
let AddedComplexity = 10 in {
def : Pat<(sra GPR64:$Rn, (i64 imm0_63:$imm)),
          (SBFMXri GPR64:$Rn, imm0_63:$imm, 63)>;
}
```

`Pat` 源里的 `sra` 对应 IR 的 `ashr`/gMIR 的 `G_ASHR`。`imm0_63` 是 `ImmLeaf`：

```tablegen
def imm0_63 : Operand<i64>, ImmLeaf<i64, [{
  return ((uint64_t)Imm) < 64;
}]> {
  let ParserMatchClass = Imm0_63Operand;
}
```

### B.3.3 最小 patch

把三处 `imm0_63` 都改成 `imm0_3`：

```tablegen
let AddedComplexity = 10 in {
def : Pat<(sra GPR64:$Rn, (i64 imm0_3:$imm)),
          (SBFMXri GPR64:$Rn, imm0_3:$imm, 63)>;
}
```

`imm0_3` 的定义是 `Imm < 4`。只改源 pattern、不改结果里的 `imm0_63:$imm`，TableGen 可能接受（它只是复用已绑定的操作数），也可能报约束不一致；参考解统一改成 `imm0_3`，避免歧义。**本机没有重建 LLVM，patch 的编译可行性未验证。**

**这是教学补丁，不是上游建议。** 真实的 `sra` 需要 0–63；缩小到 0–3 只会让更多输入走变量移位，性能更差。练习的目的是观察 matcher，不是优化 AArch64。

### B.3.4 重建

如果你有同版本 LLVM 构建目录：

```console
ninja -C build llc
```

只改 `.td` 时，TableGen 会重新生成 `.inc`，然后重编 `llc`。确认你运行的是新二进制：

```console
& $llc --version
```

输出必须是 23.1.4，且路径指向你的构建目录，而不是预编译包。

如果没有构建目录，**不要伪造 patch 后的输出**。用 B.4.2 的 COPY 阻断实验观察同一条变量移位路径，并在报告里明确写“patch 后结果是理论预期，未重建 LLVM”。

---

## B.4 参考解答

<details>
<summary>点击展开：完整 patch、预测表与验证命令</summary>

### B.4.1 patch 前基线（全部实跑）

| 用例 | RBS 关键行 | ISel 关键指令 | 汇编 |
| --- | --- | --- | --- |
| `ashr 3` | `G_CONSTANT 3` + `G_ASHR` | `SBFMXri %0, 3, 63` | `asr x0, x0, #3` |
| `ashr 4` | `G_CONSTANT 4` + `G_ASHR` | `SBFMXri %0, 4, 63` | `asr x0, x0, #4` |
| `ashr 5` | `G_CONSTANT 5` + `G_ASHR` | `SBFMXri %0, 5, 63` | `asr x0, x0, #5` |
| `ashr 63` | `G_CONSTANT 63` + `G_ASHR` | `SBFMXri %0, 63, 63` | `asr x0, x0, #63` |
| `add 4095` | `G_CONSTANT 4095` + `G_ADD` | `ADDXri %0, 4095, 0` | `add x0, x0, #4095` |
| `add 4096` | `G_CONSTANT 4096` + `G_ADD` | `ADDXri %0, 1, 12` | `add x0, x0, #1, lsl #12` |
| `add 4097` | `G_CONSTANT 4097` + `G_ADD` | 常量物化 + `ADDXrr` | `mov w8, #4097` + `add x0, x0, x8` |
| `fadd` on `gpr` | `gpr(f32)` + `G_FADD` | 无（`cannot select`） | fallback/失败 |
| `sext` two uses | `G_SEXT` 两个 use | 一个 `SXTW` + 三个 `ADDXrr` | 见 B.2.4 |
| `fadd half` 无 feature | `G_FPEXT` + `G_FADD f32` + `G_FPTRUNC` | `FCVT` + `FADDS` | 见 B.2.5 |
| `fadd half` +fullfp16 | `G_FADD f16` | `FADDH` | `fadd h0, h0, h1` |

### B.4.2 patch 后的理论预期

| 用例 | 预期 | 理由 |
| --- | --- | --- |
| `ashr 3` | 仍 `SBFMXri %0, 3, 63`，汇编 `asr #3` | `3 < 4`，`imm0_3` 通过 |
| `ashr 4` | 不再命中 `SBFMXri`；变量移位 `ASRVXr` + 常量物化 | `4` 不满足 `Imm < 4`，imported 规则回退到 `(sra GPR64, i64:$Rm)` |
| `ashr 5` | 同上，`ASRVXr` + 常量物化 | 同上 |
| `ashr 63` | 同上，`ASRVXr` + 常量物化 | 同上 |
| `add 4095/4096/4097` | 不变 | 走 `ADDXri`/`ADDXrr`，与本 patch 无关 |
| `and 255/257` | 不变 | 走 logical immediate/`ANDXrr`，与本 patch 无关 |
| `fadd` on `gpr` | 不变：`cannot select` | bank 检查失败，与本 patch 无关 |
| `sext` two uses | 不变：一个 `SXTW` + 三个 `ADDXrr` | C++ one-use 启发式，与本 patch 无关 |
| `fadd half` 无 feature | 不变：`FCVT` + `FADDS` | 失败/改写发生在 Legalizer 的 feature gating |
| `fadd half` +fullfp16 | 不变：`FADDH` | 同上 |

**为什么 `ashr 4` 会走 `ASRVXr`？** 因为 AArch64 还有一条变量移位 pattern，形如（由 `Shift` multiclass 生成）：

```tablegen
def : Pat<(i64 (sra GPR64:$Rn, i64:$Rm)),
          (ASRVXr GPR64:$Rn, GPR64:$Rm)>;
```

`G_CONSTANT 4` 的结果本身是一个 vreg，可以作为 `$Rm`。于是选择器需要把 4 物化到寄存器，再发 `ASRVXr`。

### B.4.3 用 COPY 阻断来实跑验证这条路径

在预编译 `llc` 上无法改 pattern，但可以人为阻断 `SBFMXri` 的嵌套匹配：在 `G_CONSTANT` 和 `G_ASHR` 之间插入 COPY。这与第 9 章的 `shl` COPY 实验同构。

从 `ashr4.rbs.mir` 出发，把：

```yaml
%2:gpr(i64) = G_ASHR %0, %1(i64)
```

改成：

```yaml
%3:gpr(i64) = COPY %1(i64)
%2:gpr(i64) = G_ASHR %0, %3(i64)
```

运行：

```console
& $llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select ashr4-copy.mir -o -
```

实跑 ISel 关键行：

```yaml
%0:gpr64 = COPY $x0
%4:gpr32 = MOVi32imm 4
%1:gpr64all = SUBREG_TO_REG %4, $subreg.sub_32
%3:gpr64 = COPY %1
%2:gpr64 = ASRVXr %0, %3
```

实跑汇编：

```asm
mov     w8, #4
asr     x0, x0, x8
ret
```

这证明“简单 pattern 被阻断后，变量移位路径确实会接管”。patch 后不会有那条 COPY，因此预期是 `ASRVXr %0, %1`，但常量物化的形状相同。**这是从实跑路径推导出的理论预期；本次没有重建 LLVM 来执行 patch 后的二进制。**

### B.4.4 验证命令清单

```console
# 1. 基线：直接看汇编
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 ashr4.ll -o -

# 2. 基线：看 RBS 和 ISel
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 -stop-after=regbankselect ashr4.ll -o ashr4.rbs.mir
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 -stop-after=instruction-select ashr4.ll -o ashr4.isel.mir

# 3. COPY 阻断：模拟简单 pattern 失败
& $llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select ashr4-copy.mir -o ashr4-copy.isel.mir

# 4. 重建后：确认二进制版本，再重复 1–2
& $llc --version
```

### B.4.5 读输出时看什么

- **`SBFMXri` 出现**：简单立即数 pattern 命中；
- **`ASRVXr` 出现 + `MOVi32imm`/`SUBREG_TO_REG`**：变量移位 pattern 命中，常量被物化；
- **`cannot select`**：连变量移位都没匹配，通常说明 bank/LLT 或 opcode 不受支持；
- **`selected: true, failedISel: true`**：发生 fallback，不是 matcher 成功；
- **`UBFMXri`/`LSLVXr`**：如果你在测 `shl`，先想想 `earlySelectSHL`。

### B.4.6 建议的 lit 测试

把 `ashr 3` 和 `ashr 4` 都放进一个 MIR 测试，patch 后应检查：

```llvm
; RUN: llc -mtriple=aarch64 -run-pass=instruction-select %s -o - | FileCheck %s
; CHECK-LABEL: name: ashr3
; CHECK: SBFMXri
; CHECK-LABEL: name: ashr4
; CHECK-NOT: SBFMXri
; CHECK: ASRVXr
```

注意：`CHECK-NOT` 的作用域是相邻两个 `CHECK` 之间；实际写测试时要把 `CHECK-NOT` 放在 `ashr4` 的 label 与 `ASRVXr` 之间，避免误伤其他函数。真正提交时还应加 `-verify-machineinstrs`。

</details>

---

## B.5 五步实现建议

1. **捕获基线**：对 5 组用例分别保存 `.ll`、RBS MIR、ISel MIR 和汇编。不要只存汇编；matcher 的证据在 MIR 里。
2. **先写预测**：在 B.2.6 的表里填“指令名 + 是否物化常量 + `failedISel`”。预测错了不要改表，保留错误，后面分析。
3. **只改一个 pattern**：把 `ashr` 的三处 `imm0_63` 改成 `imm0_3`。不要同时改 `shl`、`add`、`and`，否则无法归因。
4. **重建并确认版本**：`ninja -C build llc` 后跑 `llc --version`；确认不是 PATH 里的旧二进制。没有构建目录就停在 COPY 阻断实验，并标注理论预期。
5. **对比与解释**：逐组比较 patch 前后；对每个变化回答“是哪个 `GIM_*` 检查拒绝了它，下一条命中的规则是什么”。

---

## B.6 常见卡点

1. **改了 `shl`，结果毫无变化。** `earlySelectSHL` 在 TableGen 之前截获了 0–63 的立即数；这不是 pattern 没生效，而是根本没轮到它。
2. **只改了源 pattern，没改结果里的 `imm0_63:$imm`。** 可能编译失败，也可能因 TableGen 复用绑定而通过；参考解统一改成 `imm0_3`，避免歧义。
3. **用 `-run-pass=instruction-select` 直接跑 `.ll`。** 这个 pass 需要 MIR；跑 IR 请去掉 `-run-pass`，或先 `-stop-after=regbankselect` 生成 MIR。
4. **把 `selected: true` 当成命中。** 失败时它仍为 true；必须看 `failedISel`。
5. **把 Legalizer 的 feature gating 当成 matcher 失败。** `fadd half` 的 `G_FPEXT`/`G_FPTRUNC` 是 Legalizer 根据 `+fullfp16` 生成的，改 `fadd` pattern 不会影响它。

---

## B.7 延伸挑战

1. **32 位版本**：把 `sra GPR32` 的 `imm0_31` 改成 `imm0_3`，观察 `SBFMWri` 与 `ASRVWr` 的切换。注意 32 位变量移位是 `ASRVWr`。
2. **改 `AddedComplexity`**：把 `ashr` pattern 的 `AddedComplexity` 从 10 改成 0，观察是否被其他规则抢先；解释 matcher 的规则顺序不等于源码顺序。
3. **改 `rev16`**：把第 9 章 `rev16` pattern 里的 `(i64 8)` 改成 `(i64 4)`，预测多级匹配是否还成立，并观察输出退化。
4. **加 MIR 测试**：为 `ashr 3`/`ashr 4` 写一个带 `FileCheck` 的 `-run-pass=instruction-select` 测试，确保 patch 后行为被锁定。
5. **比较两套选择器**：同一份 `ashr 4` 分别跑 `-global-isel` 和默认 SelectionDAG，diff 汇编；思考为什么两者可能选择不同的物化方式。

---

## B.8 重点整理

- `ashr` 的 64 位立即数规则是 `(sra GPR64, (i64 imm0_63:$imm)) -> SBFMXri`；`imm0_63` 是 `ImmLeaf`，不是寄存器类。
- 把谓词缩到 `imm0_3` 后，`0–3` 仍命中 `SBFMXri`，`4–63` 改走变量移位 `ASRVXr` 并物化常量。
- `shl` 不适合做这个实验：`earlySelectSHL` 会在 TableGen 前截获 0–63。
- `selected: true` 不代表命中；`failedISel` 才是失败标记。
- bank 不匹配、feature gating、多 use 都可能让“看起来该命中”的规则不命中；先看 RBS MIR，再改 pattern。
- 本练习的 patch 后结果若未重建 LLVM，必须标注为理论预期；COPY 阻断实验只能证明变量移位路径存在，不能替代真实 patch 验证。

---

## B.9 自我检核

1. `ashr 3` 的 `MIs[0]`、`MIs[1]` 分别是什么？`imm0_3` 在哪一步检查？
2. 为什么 `ashr 4` 在 patch 后不再命中 `SBFMXri`？下一条命中的 pattern 是什么？
3. `ASRVXr` 的操作数来自哪里？为什么需要 `MOVi32imm`/`SUBREG_TO_REG`？
4. 为什么改 `shl` pattern 可能完全看不到变化？`earlySelectSHL` 检查的范围是多少？
5. `fadd half` 的 `FCVT` 是 matcher 生成的，还是 Legalizer 生成的？如何用 `-mattr` 验证？
6. 多 use 的 `sext` 为什么没有折进 `ADDXrx`？这是 `GIM_CheckHasOneUse` 还是 C++ 启发式？
7. 你如何证明自己的 patch 真的被加载了，而不是跑了 PATH 里的旧 `llc`？

---

## B.10 延伸阅读

- **`llvm/lib/Target/AArch64/AArch64InstrInfo.td`** — `sra` 立即数 pattern、`imm0_63`/`imm0_3` 的使用，以及 `AddedComplexity`。对照 `SBFMXri` 的 `immr/imms` 编码读。
- **`llvm/lib/Target/AArch64/AArch64InstrFormats.td`** — `Shift` multiclass 生成的变量移位 pattern（`ASRVXr` 等）。这是 patch 后 `ashr 4` 的落点。
- **`llvm/lib/Target/AArch64/AArch64InstructionSelector.cpp`** — `earlySelectSHL` 与 `selectShiftA_64`/`selectShiftB_64`。理解为什么 `shl` 不走 TableGen 的立即数规则。
- **`llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`** — `GIM_RecordInsn` 与立即数检查的执行路径；把 `ashr` 的 `MIs[1]` 对上源码。
- **`llvm/test/CodeGen/AArch64/GlobalISel/`** — 搜索 `SBFMXri`、`ASRVXr` 和 `select-ashr` 相关 MIR 测试，学习如何用 `-run-pass=instruction-select` 锁定 matcher 行为。

---

## 下一章

[第 11 章 ComplexPattern 迁移：从 SDNode 匹配到 GIComplexOperandMatcher](./11-complex-pattern-migration.md) —— 当一条简单 pattern 无法表达地址、立即数或复杂操作数时，下一章进入 ComplexPattern 的匹配与渲染契约。
