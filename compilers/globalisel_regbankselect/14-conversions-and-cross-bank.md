# Ch 14 — 转换指令与跨 bank 语义

> **目标**：分清“换标签”和“换数值”。学完你能判断一条 `G_BITCAST` 是免费的寄存器重解释还是一定要跨 bank 的 COPY；知道 `G_ANYEXT` / `G_TRUNC` 在 legalizer 之后可能变成什么；理解 `G_FPEXT` / `G_FPTRUNC` / `G_FPTOSI` / `G_SITOFP` 的默认 bank 与 AArch64 的例外；并能用 `or.ll` 解释 Fast 的 3 条 COPY 为什么在 Greedy 下变成 0 条。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（实测 MIR 与汇编）；AArch64 主 target。`conv.ll`、`or.ll`、`trunc128.mir` 都在官方包上跑过。
>
> **证据约定**：标“实测”的是官方 23.1.4 输出；标“源码依据”的是 `AArch64RegisterBankInfo.cpp`、`AArch64GenRegisterBankInfo.def`、`LegalizerHelper` 的结论；手写 MIR 会明确说明是手写并在 `-run-pass=regbankselect` 上验证。

## 为什么需要这个？

“转换”在 LLVM IR 里是一大类看起来相似的指令：

- `bitcast`：位不变，只换解释。
- `zext` / `sext` / `trunc`：整数位宽变化。
- `fpext` / `fptrunc`：浮点位宽变化。
- `fptosi` / `sitofp`：整数与浮点之间做数值转换。

它们在 MIR 里的命运完全不同：

- `bitcast` 可能只是一次跨 bank COPY，也可能在同一个 bank 内被合并掉。
- `zext i8 -> i32` 可能被 legalizer 变成 `G_AND`，根本没有“扩展指令”。
- `fpext` 是一条真实的 `fcvt`，即使源和目的都在 FPR，也不是 COPY。
- `fptosi` 天然跨 bank（FPR→GPR），但 AArch64 在某些条件下会把结果留在 FPR。

如果把它们都当成“转换指令”，就会误判 COPY 数量、误判 bank、误判性能。

## 先建立直觉

把值想成一张写了二进制内容的卡片：

```text
bitcast:     同一张卡片，换一个标签
             [ 0x3F800000 ]  float 1.0  ↔  int 1065353216

fpext:       重新计算，写一张更大的卡片
             float 1.0  ──fcvt──►  double 1.0

fptosi:      重新计算，写一张整数卡片
             float 1.5  ──fcvtzs──►  int 3
```

- `bitcast` 不改变位，只改变“这些位代表什么类型”。在机器层，它通常是一次寄存器复制（同 bank 可 coalesce，跨 bank 要 FMOV）。
- `fpext` / `fptrunc` / `fptosi` / `sitofp` 改变数值表示，必须有一条真实指令。
- `zext` / `trunc` 改变位宽；小位宽在 AArch64 上可能没有独立指令，被 legalizer 改写成移位/掩码。

## 核心概念

### 1. 三大家族

| 家族 | 代表 | 是否改变位 | 典型 bank |
| --- | --- | --- | --- |
| 位重解释 | `G_BITCAST` | 否 | 源 bank（Fast），或 Greedy 选边 |
| 整数位宽 | `G_ANYEXT` / `G_TRUNC` / `G_SEXT` / `G_ZEXT` | 是（高位） | GPR |
| 浮点/整数转换 | `G_FPEXT` / `G_FPTRUNC` / `G_FPTOSI` / `G_SITOFP` | 是（数值） | FPR/FPR 或 FPR↔GPR |

这张表是本章的总纲。后面所有现象都能归到其中一行。

### 2. `G_BITCAST` 在 Fast 下复用源 bank

AArch64 的 `getInstrMapping` 对 `G_BITCAST` 有这样一段（源码依据）：

```cpp
case TargetOpcode::G_BITCAST: {
  Register SrcReg = MI.getOperand(1).getReg();
  const RegisterBank *SrcRB = getRegBank(SrcReg, MRI, TRI);
  if (SrcRB) {
    TypeSize Size = getSizeInBits(SrcReg, MRI, TRI);
    return getInstructionMapping(
        DefaultMappingID, 0,
        getCopyMapping(SrcRB->getID(), SrcRB->getID(), Size),
        /*NumOperands=*/2);
  }
  [[fallthrough]];
}
```

含义：

- 如果源 vreg 已经有 bank，bitcast 的 def 直接用同一个 bank。
- 成本是 0：同 bank 的复制可以 coalesce，不额外收费。
- 如果源还没有 bank，才 fallthrough 到 `COPY` 的通用逻辑。

所以在 Fast 模式下，`float -> i32` 的 bitcast 结果通常还是 FPR；`i32 -> float` 的结果通常还是 GPR。它不会主动把值搬到“看起来更自然”的 bank。

### 3. Greedy 的 alternative：四种组合

AArch64 为 32/64 位 bitcast 提供四个 alternative（源码依据）：

| ID | 方向 | mapping cost | 说明 |
| --- | --- | --- | --- |
| 1 | GPR → GPR | 1 | 同 bank |
| 2 | FPR → FPR | 1 | 同 bank |
| 3 | GPR → FPR | 5 | 跨 bank |
| 3 | FPR → GPR | 5 | 跨 bank |

注意源码细节：**两个跨 bank alternative 都用了 `copyCost(GPR,FPR)`，也就是 5**；虽然 `copyCost(FPR, GPR)`（GPR→FPR）返回 4。读这段时不要以为第二个写错了——这是 23.1.4 的实际代码。`G_BITCAST` 没有专门的 `applyMappingImpl` 分支，最终走默认的 copy 应用逻辑；ID 只是 mapping 的标识。

Greedy 选择哪一个，不只取决于 bitcast 自己，还取决于它前面和后面的指令已经占了哪个 bank。

### 4. `G_ANYEXT` / `G_TRUNC` 是 GPR 上的位宽操作

AArch64 的默认映射会把标量整数放到 GPR。`G_ANYEXT` 和 `G_TRUNC` 不改变 bank，只改变 LLT 的位宽。它们可能是：

- 一条真实指令（例如 32→64 的 `G_ANYEXT`，在 AArch64 上可能直接 coalesce 或用 `mov w` 的零扩展语义）。
- 被 legalizer 改写成别的指令（见例子三）。
- 完全消除（例如 `trunc i64 -> i32` 只取低 32 位，最终可能没有指令）。

### 5. `G_FPEXT` / `G_FPTRUNC` 是 FPR 内的真实转换

AArch64 对 `G_FPEXT` 有专门映射（源码依据）：

```cpp
case TargetOpcode::G_FPEXT: {
  LLT DstTy = MRI.getType(MI.getOperand(0).getReg());
  LLT SrcTy = MRI.getType(MI.getOperand(1).getReg());
  return getInstructionMapping(
      DefaultMappingID, /*Cost*/ 1,
      getFPExtMapping(DstTy.getSizeInBits(), SrcTy.getSizeInBits()),
      /*NumOperands*/ 2);
}
```

`G_FPTRUNC` 没有专门分支，走默认逻辑：源和目的都是浮点标量，落在 FPR。两者的 mapping 都是 FPR/FPR，但它们是**真实指令**，不是 COPY：

```text
%1:fpr(f64) = G_FPEXT %0(f32)     ; fcvt d0, s0
%1:fpr(f32) = G_FPTRUNC %0(f64)   ; fcvt s0, d0
```

### 6. `G_FPTOSI` / `G_SITOFP` 默认跨 bank

- `G_SITOFP`：整数 → 浮点，默认 `{FPR, GPR}`。
- `G_FPTOSI`：浮点 → 整数，默认 `{GPR, FPR}`。

AArch64 有两个例外：

- `G_SITOFP` 的源已经在 FPR，且源/目的尺寸相同 → `{FPR, FPR}`。这对应“整数已经在浮点寄存器里”的场景。
- `G_FPTOSI` 的目的尺寸等于源尺寸，且结果的所有 use 都偏好 FP（或开了 `FeatureFPRCVT`）→ `{FPR, FPR}`。

这两个例外说明：bank 不是由 opcode 单独决定的，还取决于源 bank 和 use。

### 7. `G_TRUNC` 的 s128 特例

AArch64 对 `G_TRUNC` 有一个特殊分支（源码依据）：

```cpp
case TargetOpcode::G_TRUNC: {
  LLT SrcTy = MRI.getType(MI.getOperand(1).getReg());
  if (!SrcTy.isVector() && SrcTy.getSizeInBits() == 128)
    OpRegBankIdx = {PMI_FirstFPR, PMI_FirstFPR};
  break;
}
```

源是 128 位标量时，结果和源都放 FPR。原因和 AArch64 的 128 位寄存器布局有关：s128 通常走 Q 寄存器，而不是两个 GPR 的“天然”组合。例子三会看到真实 MIR。

## 底层机制

RegBankSelect 看到的不是“bitcast / zext / fpext”这些 IR 语义，而是 legalizer 留下的 `G_*` 指令和 LLT。bank 选择发生在两个层次：

1. **通用规则**：`RegisterBankInfo::getInstrMappingImpl` 用 operand 已有的 bank、regclass 约束和 copy-like 特例生成默认 mapping。
2. **target 覆盖**：AArch64 在 `getInstrMapping` 里为 `G_BITCAST`、`G_FPEXT`、`G_FPTOSI`、`G_SITOFP`、`G_TRUNC` 等 opcode 改写 operand 的 bank 索引。

legalizer 更早一步决定了“还有哪些指令”。`G_ZEXT` 可能被拆成 `G_ANYEXT` + `G_AND`，`G_TRUNC` 可能被消除，`G_BITCAST` 可能被合并。到了 RegBankSelect，再讨论“这条转换指令该放哪个 bank”才有意义。

## 例子一：`or.ll` 的 Fast vs Greedy（实测）

完整 IR：

```llvm
define float @or_f(float %a, float %b) {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
```

Fast 的 banked MIR：

```text
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%6:gpr(i32) = COPY %2(i32)
%7:gpr(i32) = COPY %3(i32)
%4:gpr(i32) = G_OR %6, %7
%5:gpr(f32) = G_BITCAST %4(i32)
```

Greedy 的 banked MIR：

```text
%2:fpr(i32) = G_BITCAST %0(f32)
%3:fpr(i32) = G_BITCAST %1(f32)
%4:fpr(i32) = G_OR %2, %3
%5:fpr(f32) = G_BITCAST %4(i32)
```

Fast 的最终汇编：

```asm
fmov w8, s0
fmov w9, s1
orr  w0, w8, w9
fmov s0, w0
ret
```

Greedy 的最终汇编：

```asm
orr v0.2s, v0.2s, v1.2s
ret
```

Fast 的 3 条 `fmov` 对应 MIR 里的 3 条 `COPY`（两个输入 + 一个输出）。Greedy 把 `G_OR` 放到 FPR 后，OR 直接在向量寄存器上做，输入输出都不需要搬。

这个例子同时说明了：

- `G_BITCAST` 在 Fast 下是“源 bank 的复制”，不是免费的数值转换。
- `G_OR` 的 alternative mapping 是 Greedy 能改变结果的关键。
- 统计 COPY 时，要把“操作数修复”和“结果修复”都算上。

## 例子二：四种数值转换的真实 MIR（实测）

```llvm
define double @fpext_f32_f64(float %x) { %r = fpext float %x to double; ret double %r }
define float @fptrunc_f64_f32(double %x) { %r = fptrunc double %x to float; ret float %r }
define i32 @fptosi_f32_i32(float %x) { %r = fptosi float %x to i32; ret i32 %r }
define float @sitofp_i32_f32(i32 %x) { %r = sitofp i32 %x to float; ret float %r }
```

`-stop-after=regbankselect` 的 MIR：

```text
name: fpext_f32_f64
  %0:fpr(f32) = COPY $s0
  %1:fpr(f64) = G_FPEXT %0(f32)
  $d0 = COPY %1(f64)

name: fptrunc_f64_f32
  %0:fpr(f64) = COPY $d0
  %1:fpr(f32) = G_FPTRUNC %0(f64)
  $s0 = COPY %1(f32)

name: fptosi_f32_i32
  %0:fpr(f32) = COPY $s0
  %1:gpr(i32) = G_FPTOSI %0(f32)
  $w0 = COPY %1(i32)

name: sitofp_i32_f32
  %0:gpr(i32) = COPY $w0
  %1:fpr(f32) = G_SITOFP %0(i32)
  $s0 = COPY %1(f32)
```

对应汇编：

```asm
fcvt d0, s0        ; fpext
fcvt s0, d0        ; fptrunc
fcvtzs w0, s0      ; fptosi
scvtf s0, w0       ; sitofp
```

四条都是真实数值转换，和 `fmov`（bitcast）不是一回事。注意 `fptosi` / `sitofp` 的 def 与 source 分属不同 bank，这不是 repair，而是指令语义本身要求的跨寄存器文件传输。

## 例子三：整数宽度变化与 s128 特例（实测 + 手写 MIR）

```llvm
define i32 @zext_i8_i32(i8 %x) { %r = zext i8 %x to i32; ret i32 %r }
define i64 @zext_i8_i64(i8 %x) { %r = zext i8 %x to i64; ret i64 %r }
define i32 @trunc_i64_i32(i64 %x) { %r = trunc i64 %x to i32; ret i32 %r }
```

`-stop-after=regbankselect` 的 MIR：

```text
name: zext_i8_i32
  %1:gpr(i32) = COPY $w0
  %3:gpr(i32) = G_CONSTANT i32 255
  %2:gpr(i32) = G_AND %1, %3
  $w0 = COPY %2(i32)

name: zext_i8_i64
  %1:gpr(i32) = COPY $w0
  %3:gpr(i64) = G_ANYEXT %1(i32)
  %4:gpr(i64) = G_CONSTANT i64 255
  %2:gpr(i64) = G_AND %3, %4
  $x0 = COPY %2(i64)

name: trunc_i64_i32
  %0:gpr(i64) = COPY $x0
  %1:gpr(i32) = G_TRUNC %0(i64)
  $w0 = COPY %1(i32)
```

两条结论：

1. `zext` 没有变成“零扩展指令”，而是变成 `G_AND` 掩码。
2. `G_ANYEXT` 只负责把 32 位容器扩到 64 位，**高位是未定义的**；真正保证零扩展的是后面的 `G_AND`。

最终汇编里，`zext_i8_i32` 是 `and w0, w0, #0xff`；`zext_i8_i64` 的 `G_ANYEXT` 可能被 coalesce，`G_AND` 保留；`trunc_i64_i32` 可能只剩 `w0` 的低 32 位，没有独立指令。

### s128 trunc 的 FPR 特例

手写 MIR（不是从 IR 生成，已用官方包 `-run-pass=regbankselect` 验证）：

```yaml
--- |
  define i64 @trunc128(i128 %x) {
    ret i64 0
  }
---
name:            trunc128
legalized:       true
regBankSelected: false
tracksRegLiveness: true
body:             |
  bb.0:
    liveins: $q0
    %0:_(i128) = COPY $q0
    %1:_(i64) = G_TRUNC %0(i128)
    $x0 = COPY %1(i64)
    RET_ReallyLR implicit $x0
```

输出：

```text
bb.0:
  %0:fpr(i128) = COPY $q0
  %1:fpr(i64) = G_TRUNC %0(i128)
  $x0 = COPY %1(i64)
```

关键点：

- `G_TRUNC` 的源和结果都在 FPR，符合源码的 s128 特例。
- `$x0 = COPY %1` 是 FPR→GPR 的跨 bank COPY；RegBankSelect 没有把它拆成额外的 vreg，selector 会处理。
- 这个例子说明：`G_TRUNC` 不一定在 GPR。源是 128 位标量时，AArch64 把它放 FPR。

## 对比与取舍

| 指令 | 是否真实指令 | 默认 bank | 跨 bank 时发生什么 |
| --- | --- | --- | --- |
| `G_BITCAST` | 否（copy-like） | 源 bank（Fast） | FMOV/MOV；alternative 成本 5 |
| `G_ANYEXT` | 可能是/可能被消除 | GPR | 仍在 GPR，可能 coalesce |
| `G_TRUNC` | 可能是/可能被消除 | GPR（s128 例外 FPR） | s128 结果在 FPR，use 需跨 bank |
| `G_FPEXT` | 是（fcvt） | FPR/FPR | 不跨 bank |
| `G_FPTRUNC` | 是（fcvt） | FPR/FPR | 不跨 bank |
| `G_FPTOSI` | 是（fcvtzs） | GPR/FPR | 源 FPR、结果 GPR |
| `G_SITOFP` | 是（scvtf） | FPR/GPR | 源 GPR、结果 FPR |

注意最后两行的“跨 bank”是语义的一部分，不是 repair；它们本来就把值从一种表示搬到另一种表示。

## 踩雷集锦

1. **把 `G_BITCAST` 当成免费指令**  
   同 bank 时它可以 coalesce，但跨 bank 时要一条 FMOV，成本是 5 或 4。`or.ll` 的 Fast 三条 COPY 就是代价。

2. **把 `G_ANYEXT` 当成 `zext`**  
   `G_ANYEXT` 的高位未定义。`zext_i8_i64` 里真正保证零扩展的是后面的 `G_AND 255`。只看 `G_ANYEXT` 会得出错误语义。

3. **以为 `G_TRUNC` 永远在 GPR**  
   源是 s128 时，AArch64 把源和结果都放 FPR。手写 MIR 就是反例。

4. **以为 `G_FPTOSI` 一定是 GPR/FPR**  
   AArch64 在目的尺寸等于源尺寸、且结果所有 use 都偏好 FP（或开了 FPRCVT）时，会把结果留在 FPR。先看 use，再断言 bank。

5. **只数转换指令附近的 COPY**  
   Greedy 可能改变上游 `G_OR` 的 bank，从而让整条链的 COPY 消失。统计时要看整个数据流，不要只盯 `G_BITCAST`。

## 进阶

1. **向量 bitcast**  
   `<4 x i32> -> <2 x i64>` 的 bitcast 不改变 128 位总宽，AArch64 会把它留在 FPR。构造一个向量 bitcast，观察是否产生指令。

2. **跨 bank 的 128 位复制**  
   `AArch64GenRegisterBankInfo.def` 里 128/256/512 位的跨 bank copy 是无效项。试着构造 `i128` 在 GPR 与 FPR 之间直接 COPY，观察 RegBankSelect 是否拒绝，以及 legalizer 如何用 `G_MERGE_VALUES` / `G_UNMERGE_VALUES` 绕过。

3. **`G_FPTOSI` 的 FPR 结果**  
   构造一个 `fptosi float -> i32`，结果只被 `sitofp i32 -> float` 使用。看 AArch64 是否把中间结果留在 FPR，以及 `FeatureFPRCVT` 是否影响选择。

## 动手练习

1. 对 `or.ll` 的 Fast MIR 逐条标注：哪三条是跨 bank COPY，分别修复哪个 operand；再和 Greedy 的 0 条对比。
2. 把 `fpext` 换成 `fptrunc`，确认 MIR 里仍然是 `G_FPEXT` / `G_FPTRUNC`，而不是 COPY。
3. 把 `zext i8 -> i64` 改成 `sext i8 -> i64`，观察 legalizer 是保留 `G_SEXT` 还是改写成移位/掩码。
4. 手写一条 `%1:_(i128) = G_TRUNC %0:_(i128)` 的 MIR，跑 `-run-pass=regbankselect`，验证源和结果都在 FPR。
5. 把 `bitcast_i32_f32` 的源改成已经在 FPR 的 `f32`，观察 Fast 下 bitcast 结果是否仍在 FPR。

## 本章重点整理

- `G_BITCAST` 是 copy-like：Fast 复用源 bank，Greedy 有 GPR/GPR、FPR/FPR、GPR→FPR、FPR→GPR 四种 alternative。
- `G_ANYEXT` 不是 `zext`；高位未定义，zero-extend 由后续 `G_AND` 保证。
- `G_TRUNC` 通常在 GPR，但源为 s128 时 AArch64 把源和结果都放 FPR。
- `G_FPEXT` / `G_FPTRUNC` 是 FPR 内的真实 `fcvt`，不是 COPY。
- `G_FPTOSI` / `G_SITOFP` 默认跨 bank；AArch64 会按源 bank 和 use 做例外。

## 自我检核

- [ ] 能解释 `or.ll` Fast 的 3 条 COPY 分别来自哪里，Greedy 为什么是 0 条
- [ ] 能说出 `G_BITCAST` 在 Fast 和 Greedy 下的 bank 决策差异
- [ ] 能解释 `G_ANYEXT` 为什么不是 `zext`
- [ ] 能说出 `G_TRUNC` s128 特例的源/结果 bank
- [ ] 能区分 `G_FPEXT` 与 `G_BITCAST` 的机器层差异

## 延伸阅读

### 源码

- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`G_BITCAST` 的 `getInstrMapping` 与 `getInstrAlternativeMappings`，`G_FPEXT`、`G_FPTOSI`、`G_SITOFP`、`G_TRUNC` 的分支。
  - **学什么**：四种 bitcast alternative、FPR/FPR 例外、s128 trunc 特例。
  - **和本章的关联**：本章几乎所有源码引用都来自这里。

- **[AArch64GenRegisterBankInfo.def](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def)**
  - **读哪里**：`PartMappings` 的 FPR16..FPR512、GPR32..GPR128，以及 cross-register-bank copy 项。
  - **学什么**：为什么 128 位跨 bank copy 是无效项，`G_MERGE_VALUES` 为什么要绕路。
  - **和本章的关联**：例子三和“进阶 2”的依据。

- **[LegalizerHelper.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/LegalizerHelper.cpp)**
  - **读哪里**：`G_ZEXT` / `G_ANYEXT` / `G_TRUNC` 的 legalization 规则。
  - **学什么**：`zext i8` 为什么会变成 `G_AND`，`trunc` 什么时候被消除。
  - **和本章的关联**：例子三的改写结果。

### 文档

- **[LLVM IR Language Reference — Conversion Operations](https://llvm.org/docs/LangRef.html#conversion-operations)**
  - **读哪里**：`bitcast`、`zext`、`fpext`、`fptosi` 的语义。
  - **学什么**：哪些转换改变位、哪些改变数值。
  - **和本章的关联**：先分清 IR 语义，再看 MIR 实现。

- **[LLVM GlobalISel — Generic Opcode](https://llvm.org/docs/GlobalISel/GenericOpcode.html)**
  - **读哪里**：`G_BITCAST`、`G_ANYEXT`、`G_TRUNC`、`G_FPEXT`、`G_FPTOSI`、`G_SITOFP`。
  - **学什么**：operand 顺序和类型约束。
  - **和本章的关联**：读 MIR 时的语法依据。

下一章进入向量、聚合与边角类型：同样是 128 位，可能是 GPR pair、FPR Q 寄存器，也可能是被 legalizer 拆开的两个 i64；同样是向量，固定长度和 scalable 的 bank 也可能不同。

→ [Ch 15 向量、聚合与边角类型](./15-vectors-aggregates-edge-types.md)