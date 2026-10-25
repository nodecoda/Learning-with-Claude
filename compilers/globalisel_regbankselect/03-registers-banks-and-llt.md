# Ch 3 — 寄存器、寄存器类、bank 与 LLT

> **目标**：把“寄存器”这个词拆成四层：LLT、bank、regclass、物理寄存器。学完你能解释 `RegisterBank` 到底存了什么、coverage 与 size 有什么区别、物理寄存器的 bank 从哪来，以及**为什么同一个 LLT 会出现在不同 bank**。
>
> **环境**：LLVM 23.1.4 官方预编译包（实测）；源码行来自 `llvm-23.1.4-src`（源码依据）。示例以 AArch64 为主，RISC-V 在 Ch 4 展开。
>
> **证据约定**：本章的 MIR 全部实测；`getMaximumSize` 的具体数值来自 AArch64 构造函数的断言（源码依据），没有通过 `-debug-only` 打印验证（官方包无该选项）。

## 为什么需要这个？

“寄存器”在 LLVM backend 里至少有四种含义，而且它们经常在同一段对话里混用：

- “这个值是 `i32`”——说的是 **LLT**；
- “它在 gpr 里”——说的是 **bank**；
- “它是 gpr32”——说的是 **regclass**；
- “它在 `$w0`”——说的是 **物理寄存器**。

混用的后果很具体。比如你看到 `%2:fpr(i32) = G_LOAD`，第一反应可能是“i32 怎么会进 FPR？是不是哪里写错了”。如果你知道 bank 是由指令与使用点协商出来的，就会去看 `AArch64RegisterBankInfo.cpp:1153-1173` 那段“load 的结果喂给浮点指令就放 FPR”的逻辑，而不是去翻 Legalizer。

再比如，看到 `%1:gpr(f32)` 就断言“这是 bug”，但它在 `G_BITCAST` 的默认映射里是预期行为（Ch 1 示例二）。**不知道 LLT 与 bank 是正交的，就会把正常代码当 bug 查。**

## 先建立直觉

把值想成书，把寄存器文件想成图书馆：

- **LLT** 是书的**开本和类别**：A4 还是 A5、是图册还是文字书。它决定“能不能放进某个书架”。
- **bank** 是**馆区**：中文区、外文区。它决定“这本书归哪个部门管”。
- **regclass** 是**书架规格**：三层还是五层、能不能放大开本。它决定“具体放哪一排”。
- **物理寄存器** 是**具体格位**。

```text
        ┌─────────────────────────────────────────────┐
        │  bank: GPR                                  │
        │   ├── regclass: gpr32  ── 物理: $w0,$w1,... │
        │   ├── regclass: gpr64  ── 物理: $x0,$x1,... │
        │   └── regclass: XSeqPairs ── 物理: x0_x1... │
        └─────────────────────────────────────────────┘
        ┌─────────────────────────────────────────────┐
        │  bank: FPR                                  │
        │   ├── regclass: fpr32  ── 物理: $s0,$s1,... │
        │   ├── regclass: fpr64  ── 物理: $d0,$d1,... │
        │   ├── regclass: fpr128 ── 物理: $q0,$q1,... │
        │   └── regclass: QQQQ   ── 物理: q0_q1_q2_q3│
        └─────────────────────────────────────────────┘
```

包含关系是：**bank ⊃ regclass ⊃ 物理寄存器**。反过来说：一个 bank 覆盖多个 regclass，一个 regclass 包含多个物理寄存器。LLT 不在这条链上，它是“值的属性”，与 bank 正交。

## 核心概念

### 四层各回答什么问题

| 层 | 回答的问题 | 存在哪 | 谁写 |
| --- | --- | --- | --- |
| LLT | 这个值有多少 bit、是不是指针/向量 | `MachineRegisterInfo` 的类型表 | IRTranslator |
| bank | 这个值在哪类寄存器文件里活动 | vreg 的 `RegClassOrRegBank` 字段 | RegBankSelect |
| regclass | 可以分配哪些物理寄存器 | 同一个字段（ISel 后） | InstructionSelect |
| 物理寄存器 | 最终落在哪个硬件寄存器 | 指令操作数 | Register Allocator |

关键点：**同一个字段在两个阶段装不同的东西**。`MachineRegisterInfo.h:47-48` 把它定义成 `PointerUnion<const TargetRegisterClass *, const RegisterBank *>`，所以一个 vreg 要么有 regclass、要么有 bank，不会同时有。

### `RegisterBank` 里到底存了什么

```cpp
// llvm/include/llvm/CodeGen/RegisterBank.h:30-35
class RegisterBank {
private:
  unsigned ID;
  unsigned NumRegClasses;
  const char *Name;
  const uint32_t *CoveredClasses;
```

只有四个字段：

- `ID`：全局唯一编号，`RegisterBankInfo` 用它索引；
- `Name`：打印用（MIR 里的 `gpr` / `fpr` / `cc`）；
- `CoveredClasses`：一个**位图**，第 `RC.getID()` 位为 1 表示“这个 bank 覆盖该 regclass”；
- `NumRegClasses`：位图有多少位，用于遍历与打印。

`covers()` 就是一次位测试：

```cpp
// llvm/lib/CodeGen/RegisterBank.cpp:52-54
bool RegisterBank::covers(const TargetRegisterClass &RC) const {
  return (CoveredClasses[RC.getID() / 32] & (1U << RC.getID() % 32)) != 0;
}
```

**没有“寄存器列表”字段。** bank 不拥有寄存器，它只是“这个类属于我”的集合标记。真正的寄存器集合在 regclass（`MCRegisterClass`）里。

### coverage 与 size 是两件事

| 概念 | 含义 | 存储 | 查询 |
| --- | --- | --- | --- |
| coverage | 哪些 regclass 属于这个 bank | `CoveredClasses` 位图 | `covers()` |
| size | 这个 bank 能容纳的最大位数 | `RegisterBankInfo::Sizes[]` | `getMaximumSize()` |

```cpp
// llvm/include/llvm/CodeGen/RegisterBankInfo.h:596-599
/// Get the maximum size in bits that fits in the given register bank.
unsigned getMaximumSize(unsigned RegBankID) const {
  return Sizes[RegBankID + HwMode * NumRegBanks];
}
```

AArch64 的三个值写在构造函数的断言里（`AArch64RegisterBankInfo.cpp:82-97`）：

| bank | 覆盖的关键 regclass | `getMaximumSize` | 断言原文 |
| --- | --- | --- | --- |
| GPR | `GPR32`、`GPR64`、`XSeqPairsClass` | 128 | "GPRs should hold up to 128-bit" |
| FPR | `FPR64`、`QQQQ`、`ZPR` | 512 | "FPRs should hold up to 512-bit via QQQQ sequence" |
| CC | `CCR` | 32 | "CCR should hold up to 32-bit" |

注意 GPR 的 128 与 FPR 的 512 **都不是单个寄存器的位宽**：GPR 靠 64 位寄存器对（`XSeqPairsClass`，`AArch64RegisterInfo.td:812-815`，`Size = 128`），FPR 靠四个 Q 寄存器拼接（`QQQQ`，`591-594`，`Size = 512`）。所以“bank 能放 128 位”和“有一个 128 位的 GPR”是两回事。

## 底层机制

### 虚拟寄存器：直接读字段

```cpp
// llvm/lib/CodeGen/RegisterBankInfo.cpp:93-99
const RegClassOrRegBank &RegClassOrBank = MRI.getRegClassOrRegBank(Reg);
if (auto *RB = dyn_cast_if_present<const RegisterBank *>(RegClassOrBank))
  return RB;
if (auto *RC =
        dyn_cast_if_present<const TargetRegisterClass *>(RegClassOrRegBank))
  return &getRegBankFromRegClass(*RC, MRI.getType(Reg));
return nullptr;
```

三种情况：已经是 bank → 直接返回；是 regclass → 通过 target 的 `getRegBankFromRegClass` 换算；两者都没有 → `nullptr`（RBS 之前就是这个状态）。

### 物理寄存器：没有字段，按需推导

```cpp
// llvm/lib/CodeGen/RegisterBankInfo.cpp:86-91
if (!Reg.isVirtual()) {
  // FIXME: This was probably a copy to a virtual register that does have a
  // type we could use.
  const TargetRegisterClass *RC = getMinimalPhysRegClass(Reg, TRI);
  return RC ? &getRegBankFromRegClass(*RC, LLT()) : nullptr;
}
```

物理寄存器**不携带 bank**，它是通过“最小物理寄存器类”反推的：`$x0` 的最小类是 `GPR64` → GPR bank；`$s0` → `FPR32` → FPR bank；`$nzcv` → `CCR` → CC bank。

这也是为什么不能对物理寄存器调用 `MRI.getRegBank(Reg)`：那个函数直接索引 `VRegInfo[Reg.id()]`（`MachineRegisterInfo.h:693-695`），只对虚拟寄存器有意义。文档写得很直白：“This shouldn't be used directly unless Reg has a register bank.”

### target 端的换算与自检

```cpp
// llvm/lib/CodeGen/RegisterBankInfo.cpp:118-128（节选）
const TargetRegisterClass *RC = MI.getRegClassConstraint(OpIdx, &TII, TRI);
if (!RC)
  return nullptr;
Register Reg = MI.getOperand(OpIdx).getReg();
const RegisterBank &RegBank = getRegBankFromRegClass(*RC, MRI.getType(Reg));
// Check that the target properly implemented getRegBankFromRegClass.
assert(RegBank.covers(*RC) &&
       "The mapping of the register bank does not make sense");
```

这里有一个隐含约束：**`getRegBankFromRegClass` 返回的 bank 必须 `covers()` 那个 regclass**，否则断言失败。这条约束把 coverage 从“文档”变成了“可验证的不变量”。

### 同一个 bank 内的不同尺寸

`getValueMapping(bank, size)` 按尺寸选 partial mapping：

```cpp
// llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def:182-204（节选）
unsigned AArch64GenRegisterBankInfo::getRegBankBaseIdxOffset(unsigned RBIdx,
                                                             TypeSize Size) {
  if (RBIdx == PMI_FirstGPR) {
    if (Size <= 32)
      return 0;
    if (Size <= 64)
      return 1;
    if (Size <= 128)
      return 2;
    return -1;
  }
  if (RBIdx == PMI_FirstFPR) {
    ...
    if (Size.isScalable())
      return 3;
    if (MinSize <= 16)
      return 0;
    ...
    if (MinSize <= 512)
      return 5;
    return -1;
  }
  return -1;
}
```

返回 `-1` 就落到 `ValMappings[InvalidIdx]`，也就是“这个 bank 装不下这个尺寸”。Ch 2 示例三里 `i768` 的失败，本质就是 GPR（>128）和 FPR（>512）都返回 `-1`，没有任何 mapping 可用。

### 同一个 LLT 为什么能落到不同 bank

三类原因，按出现频率排：

1. **指令语义**。`G_LOAD` 的结果默认放 GPR，但如果它喂给浮点指令，就改放 FPR（`AArch64RegisterBankInfo.cpp:1153-1173`）。
2. **默认映射的实现细节**。`G_BITCAST` 的默认映射按“目标尺寸 ≤ 64 且非向量”判 GPR（`948-964`），于是 `f32` 的 bitcast 结果在 GPR。
3. **repair**。即使指令本身选了某个 bank，使用点仍可能在不同 bank，repair 会插 COPY 造出跨 bank 的值（Ch 10）。

换句话说：**LLT 只决定“候选集合”，不决定最终选择**。

## 示例一：同一个函数里，`i32` 在两个 bank（实测）

```llvm
; .scratch/part0/du.ll
define float @du(ptr %p, ptr %q) {
entry:
  %v = load i32, ptr %p
  %i1 = add i32 %v, 1
  %i2 = xor i32 %i1, %v
  store i32 %i2, ptr %q
  %f = bitcast i32 %v to float
  %g = fadd float %f, 1.0
  ret float %g
}
```

```text
; -stop-after=regbankselect
%0:gpr(p0) = COPY $x0
%1:gpr(p0) = COPY $x1
%2:fpr(i32) = G_LOAD %0(p0) :: (load (i32) from %ir.p)   ; ← i32 在 FPR
%3:gpr(i32) = G_CONSTANT i32 1
%9:gpr(i32) = COPY %2(i32)                               ; ← 为 G_ADD 修复
%4:gpr(i32) = G_ADD %9, %3
%10:gpr(i32) = COPY %2(i32)                              ; ← 为 G_XOR 修复
%5:gpr(i32) = G_XOR %4, %10
G_STORE %5(i32), %1(p0) :: (store (i32) into %ir.q)
%6:fpr(f32) = G_BITCAST %2(i32)
%7:fpr(f32) = G_FCONSTANT float 1.000000e+00
%8:fpr(f32) = G_FADD %6, %7
$s0 = COPY %8(f32)
```

同一个 `i32`，两条命运：

- `%2` 是 load 的结果，它有一个浮点使用点（经 `G_BITCAST` 到 `f32`），默认映射把它放在 **FPR**；
- `%4`、`%5`、`%9`、`%10` 是整数运算的结果，放在 **GPR**。

代价是两条 COPY。这两条不是“多余”，而是“让一个 FPR 值参与两次 GPR 运算”的必要成本。如果这里改成 Greedy，结果**不变**（实测）：把 load 挪回 GPR 会消掉这两条 COPY，但要在 bitcast 处再插一条 GPR→FPR 的 COPY，净成本相同或更高。

这一例最能说明“LLT 不决定 bank”：**同一个函数、同一个 `i32`、两个 bank，全都有明确理由。**

## 示例二：`f32` 在 GPR（实测）

```llvm
; .scratch/part0/bitcast.ll
define float @bc(i32 %x) {
entry:
  %f = bitcast i32 %x to float
  %r = fadd float %f, %f
  ret float %r
}
```

```text
%0:gpr(i32) = COPY $w0
%1:gpr(f32) = G_BITCAST %0(i32)
%3:fpr(f32) = COPY %1(f32)
%4:fpr(f32) = COPY %1(f32)
%2:fpr(f32) = G_FADD %3, %4
$s0 = COPY %2(f32)
```

`%1` 的 LLT 是 `f32`，bank 是 `gpr`。这不是 bug：`G_BITCAST` 在 GPR 里是零代价（同一条寄存器），真正的跨 bank 发生在两个使用点。把它当成“LLT 决定 bank”的反例记下来。

## 示例三：`i128` 在 FPR，`i8` 消失（实测）

同一份 `wide.ll` 里两个函数：

```llvm
define i128 @ldi128(ptr %p) {
entry:
  %v = load i128, ptr %p
  ret i128 %v
}

define <2 x i64> @ldv2(ptr %p) {
entry:
  %v = load <2 x i64>, ptr %p
  ret <2 x i64> %v
}
```

```text
; ldi128, -stop-after=regbankselect
%0:gpr(p0) = COPY $x0
%1:fpr(i128) = G_LOAD %0(p0) :: (load (i128) from %ir.p)
%2:fpr(i64), %3:fpr(i64) = G_UNMERGE_VALUES %1(i128)
$x0 = COPY %2(i64)
$x1 = COPY %3(i64)

; ldv2, -stop-after=regbankselect
%0:gpr(p0) = COPY $x0
%1:fpr(<2 x i64>) = G_LOAD %0(p0) :: (load (<2 x i64>) from %ir.p)
$q0 = COPY %1(<2 x i64>)
```

两个都反直觉：

- **标量 `i128` 进了 FPR**。原因不是“约束推导”，而是 AArch64 在 `getInstrMapping` 顶部的粗猜：向量、浮点指令的操作数、以及**超过 64 位的标量**先记成 FPR（`AArch64RegisterBankInfo.cpp:1006-1017`），再按尺寸取 partial mapping。`i128` 正好命中 `> 64` 这一条。
- **`<2 x i64>` 也在 FPR**。向量在 AArch64 上归 FPR bank，这与“FPR = 浮点”是两种不同的心智模型。

再看 `i8`：

```llvm
define i8 @add8(i8 %a, i8 %b) {
entry:
  %r = add i8 %a, %b
  ret i8 %r
}
```

legalized MIR 里已经看不到 `i8`：

```text
%2:_(i32) = COPY $w0
%3:_(i32) = COPY $w1
%8:_(i32) = G_ADD %2, %3
$w0 = COPY %8(i32)
```

`i8` 的加法在 GPR 的 32 位子寄存器上完成。也就是说，**“LLT 有多少种”与“bank 里能见到多少种”不是一回事**：Legalizer 会先做一轮类型归一化（Ch 8、Ch 15 会讲边界情况）。

## 对比与取舍

### bank 与 regclass 的分工

| 维度 | bank | regclass |
| --- | --- | --- |
| 粒度 | 粗（GPR / FPR / CC） | 细（gpr32 / gpr64 / fpr32 …） |
| 何时确定 | RegBankSelect | InstructionSelect |
| 谁消费 | 指令选择（选 pattern 方向） | 寄存器分配（选物理寄存器） |
| 能否跨 bank | 不能（跨 bank 要 COPY） | 同一 bank 内可约束/收窄 |
| 数量级 | 个位数 | 几十到上百 |

为什么需要 bank 这一层？两个原因：

1. **pattern 数量**。如果 ISel 直接按 regclass 匹配，每个 opcode 都要为每个可能的 regclass 写一份 pattern；有了 bank，可以先按“园区”分流，pattern 只需覆盖“园区内的货架”。
2. **RA 之前不能定死 regclass**。Legalizer 与 combiner 还会改指令形态，过早把 vreg 约束成 `gpr32common` 会限制后续优化。bank 是“够用且不锁死”的中间粒度。

### coverage 与 size 的分工

- `covers()` 回答“这个 regclass 属不属于我”，用于校验与查表；
- `getMaximumSize()` 回答“我最大能装多少位”，用于在 `getValueMapping` 里快速判非法。

两者不能互相替代：GPR 覆盖 `XSeqPairsClass`（size 128），但它也覆盖 `GPR32`（size 32）；只看 size 会以为 GPR 只能放 128 位，只看 coverage 又不知道上限。

## 踩雷集锦

**1. “`i32` 一定在 GPR”**

`du.ll` 里 `%2:fpr(i32)` 就是反例。判断依据应该是指令与使用点，不是 LLT。

**2. “FPR 只放浮点”**

`<2 x i64>`、`i128`、甚至 `f32` 的 bitcast 中间值都可能出现在 FPR 或 GPR。AArch64 的 FPR bank 覆盖 `FPR16/32/64/128`、`QQQQ`、`ZPR`，它同时承担标量浮点、向量、可伸缩向量。

**3. “bank 的 size 就是寄存器位宽”**

GPR 的 128 来自 `XSeqPairsClass`（寄存器对），FPR 的 512 来自 `QQQQ`（四个 Q 拼接）。单个 `$x0` 只有 64 位，单个 `$q0` 只有 128 位。

**4. “物理寄存器也有 bank 字段”**

没有。物理寄存器的 bank 是按最小物理寄存器类**现算**的（`RegisterBankInfo.cpp:86-91`）。对物理寄存器调 `MRI.getRegBank()` 是未定义行为。

**5. “一个 vreg 可以同时有 regclass 和 bank”**

字段是 `PointerUnion`，二选一。RBS 写 bank，ISel 之后换成 regclass；两者不会共存。

## 进阶

- **`Sizes[]` 是二维的**：`Sizes[RegBankID + HwMode * NumRegBanks]`（`RegisterBankInfo.h:597-599`）。同一 target 在不同 HwMode 下可以有不同上限，这对 ARM/Thumb 这类双模式 target 有意义。
- **bank 的 ID 顺序不是直觉顺序**。AArch64 的 `BankIDToCopyMapIdx` 按 ID 索引，注释依次是 `CCR / FPR / GPR`（`AArch64GenRegisterBankInfo.def:218-222`），说明 ID 是 0=CC、1=FPR、2=GPR。TableGen 按名字排序生成，别假设“GPR 是 0”。
- **`i128` 有两种命运**：算术会被 Legalizer 拆成 `G_UADDO` / `G_UADDE`，但 load/store 可以保留 `i128` 的 LLT。所以“i128 会不会出现在 RBS”取决于指令，不取决于类型。
  下面是对 `add128` 的实测（`-stop-after=legalizer`）：

  ```text
  %13:_(i64), %19:_(i32) = G_UADDO %2, %4
  %15:_(i64), %18:_(i32) = G_UADDE %3, %5, %19
  ```
- **partial mapping 预告**：`ValueMapping` 可以有多个 `PartialMapping`（`NumBreakDowns > 1`），用于把一个宽值拆到多个寄存器。Ch 5 讲数据模型，Ch 15 讲向量与聚合。

## 动手练习

1. 给 `du.ll` 的 load 加第二个整数使用点（比如再 `add` 一次），重新跑 `-stop-after=regbankselect`。观察 COPY 数量变化，并解释为什么变化（或为什么不变）。
2. 写一个 `<4 x i32>` 的 load 与一个 `i64` 的 load，各跑一次 RBS，记录它们的 bank 与 LLT。再用 `-stop-after=instruction-select` 看 regclass 是怎么收窄的。
3. 把 `bitcast.ll` 改成 `bitcast i64 -> double`，预测 `%1` 的 bank，跑一次验证。如果结果与 `f32` 版本相同，说明什么？如果不同，又说明什么？

## 本章重点整理

- 四层：LLT（值的属性）、bank（寄存器文件）、regclass（可分配集合）、物理寄存器（具体格位）。
- `RegisterBank` 只有 ID、Name、coverage 位图、位图长度；没有寄存器列表。
- coverage 用 `covers()` 做位测试；size 用 `getMaximumSize()` 查 `Sizes[]`；两者不能互相替代。
- 虚拟寄存器的 bank 直接存在 `RegClassOrRegBank` 字段；物理寄存器的 bank 按最小物理寄存器类现算。
- 同一个 LLT 可以落在不同 bank：指令语义、默认映射实现、repair 三类原因。
- AArch64 的上限：GPR 128（寄存器对）、FPR 512（QQQQ）、CC 32。

## 自我检核

- [ ] 能说出 `RegisterBank` 的四个字段，并解释为什么没有寄存器列表
- [ ] 能区分 coverage 与 size，并各举一个用错的例子
- [ ] 能解释 `%2:fpr(i32)` 与 `%1:gpr(f32)` 为什么都合法
- [ ] 能说出物理寄存器的 bank 是怎么来的，以及为什么不能对物理寄存器用 `MRI.getRegBank`
- [ ] 能解释 GPR 的 128 位与 FPR 的 512 位分别来自哪个 regclass
- [ ] 能列出“同一 LLT 落到不同 bank”的三类原因

## 延伸阅读

- **[RegisterBank.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/RegisterBank.h)**
  - **读哪里**：类的四个字段、`covers()`、`verify()` 的注释。
  - **学什么**：bank 是“regclass 的集合标记”，不是寄存器容器。
  - **和本章的关联**：本章“`RegisterBank` 里到底存了什么”一节的直接来源。
- **[RegisterBank.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBank.cpp)**
  - **读哪里**：`covers()`（52-54）、`print()` 里的覆盖类统计（71-90）。
  - **学什么**：位图实现与调试打印格式。
  - **和本章的关联**：coverage 不是抽象概念，就是一次位测试。
- **[RegisterBankInfo.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/RegisterBankInfo.h)**
  - **读哪里**：`getMaximumSize`（596-599）、`getRegBank`（601-607）、`copyCost`（634-647）。
  - **学什么**：size 的 HwMode 维度；跨 bank 拷贝的默认代价是 0/1。
  - **和本章的关联**：size 与 copyCost 的接口定义。
- **[AArch64RegisterInfo.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterInfo.td)**
  - **读哪里**：`XSeqPairsClass`（812-815）、`QQQQ`（591-594）、`CCR`（309-314）。
  - **学什么**：128 / 512 / 32 这三个上限的物理来源。
  - **和本章的关联**：解释“bank size ≠ 单寄存器位宽”。
- **[AArch64GenRegisterBankInfo.def](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def)**
  - **读哪里**：`PartMappings`（15-33）、`getRegBankBaseIdxOffset`（182-204）、`BankIDToCopyMapIdx`（218-222）。
  - **学什么**：尺寸到 partial mapping 的映射规则，以及 bank ID 的实际顺序。
  - **和本章的关联**：`-1` 分支解释了 Ch 2 里 `i768` 为什么映射失败。

下一章把这套模型落到 AArch64 的三个具体 bank 上，并解释 GPR→FPR 与 FPR→GPR 的代价为什么不对称。

→ [Ch 4 AArch64 的 GPR / FPR / CC bank（RISC-V 对照）](./04-aarch64-banks.md)