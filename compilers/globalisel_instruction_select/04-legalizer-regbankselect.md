# 第 4 章 Legalizer 与 RegBankSelect：把 gMIR 收紧

> **本章目标**：理解 InstructionSelect 之前的两道"收紧"工序——Legalizer 让每条指令都合法，RegBankSelect 给每个 vreg 选寄存器组。读完能解释"为什么我的 `i8` 加法变成了 `i32`"、"为什么多出两条 COPY"。
>
> **前置**：第 1–3 章。**环境**：LLVM 23.1.4 官方包，AArch64 为主、RISC-V 对照。

---

## 4.1 位置与属性

```text
IRTranslator → Legalizer → [LoadStoreOpt] → RegBankSelect → InstructionSelect
```

沿用第 2 章的属性机，三个 pass 的声明是：

| pass | 要求 | 设置 | 清除 |
| --- | --- | --- | --- |
| Legalizer | `IsSSA` | `Legalized` | `NoPHIs`、`NoVRegs` |
| RegBankSelect | `IsSSA`、`Legalized` | `RegBankSelected` | `NoPHIs` |
| InstructionSelect | `IsSSA`、`Legalized`、`RegBankSelected` | `Selected` | — |

Legalizer 清除 `NoPHIs` 说明它会引入 PHI（比如 libcall 的合流）；清除 `NoVRegs` 说明它必然产生新 vreg。

在 MIR 上观察：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=legalizer add.ll -o add-leg.mir
```

```yaml
legalized:       true
regBankSelected: false
selected:        false
```

```yaml
    %0:_(i64) = COPY $x0
    %1:_(i64) = COPY $x1
    %2:_(i64) = G_ADD %0, %1
```

对 `i64 add`，Legalizer 什么也没改，只是把属性置为 true——因为 AArch64 的 `i64` 加法本来就是合法类型。

---

## 4.2 LegalizerInfo：一张规则表

每个目标实现一个 `LegalizerInfo`，用链式 API 描述"什么合法、不合法时怎么改"。AArch64 的整数加减规则：

```cpp
getActionDefinitionsBuilder({G_ADD, G_SUB, G_AND, G_OR, G_XOR})
    .legalFor({i32, i64, v8i8, v16i8, v4i16, v8i16, v2i32, v4i32, v2i64})
    .widenScalarToNextPow2(0)
    .clampScalar(0, s32, s64)
    .clampMaxNumElements(0, s8, 16)
    .clampMaxNumElements(0, s16, 8)
    .clampNumElements(0, v2s32, v4s32)
    .clampNumElements(0, v2s64, v2s64)
    .minScalarOrEltIf(...);
```

逐句翻译：

- `legalFor({i32, i64, ...})`：这些类型的加法直接合法；
- `widenScalarToNextPow2(0)`：标量操作数（类型索引 0）先扩到下一个 2 的幂；
- `clampScalar(0, s32, s64)`：再夹到 `[s32, s64]` 区间；
- `clampMaxNumElements(...)`：向量元素数超过上限就拆；
- `clampNumElements(...)`：把元素数夹到合法区间。

规则**按顺序匹配**，第一条命中的决定动作。这就是为什么 `i8 add` 最终变成 `i32 add`：先 widen 到 8（已经是），再 clamp 到最小 32。

### 4.2.1 动作枚举

`LegalizerInfo.h` 定义了动作集合：

| 动作 | 含义 | 典型场景 |
| --- | --- | --- |
| `Legal` | 直接可选 | AArch64 `i64 add` |
| `WidenScalar` | 用更宽的标量实现 | `i8 add` → `i32 add` |
| `NarrowScalar` | 拆成更窄的多个操作 | `i128 add` → 两条 `i64` 带进位 |
| `FewerElements` | 向量拆成子向量 | `<8 x s64>` → 4 个 `<2 x s64>` |
| `MoreElements` | 向量补元素 | `<3 x s32>` → `<4 x s32>` |
| `Bitcast` | 换成等宽类型 | 某些向量/标量互换 |
| `Lower` | 用更简单的指令组合 | `G_SREM` → `G_SDIV` + 减法 |
| `Libcall` | 调用运行时库 | 无硬件除法时的 `__divdi3` |
| `Custom` | 目标自定义 C++ lowering | AArch64 `G_GLOBAL_VALUE` → `ADRP` + `G_ADD_LOW` |
| `Unsupported` | 无法合法化 | 触发 fallback |

查询结果不是单个动作，而是 `LegalizeActionStep`：

```cpp
struct LegalizeActionStep {
  LegalizeAction Action;
  unsigned TypeIdx;   // 要改哪个操作数的类型
  LLT NewType;        // 改成什么
};
```

所以"合法化"本质是一个**反复查询—改写**的定点过程：查一步、改一步、把新指令放回工作队列，直到全部 `Legal`。

---

## 4.3 实跑：`i8` 加法被拓宽

```llvm
define i8 @add8(i8 %a, i8 %b) {
  %r = add i8 %a, %b
  ret i8 %r
}
```

Legalizer 之后：

```yaml
    %2:_(i32) = COPY $w0
    %3:_(i32) = COPY $w1
    %8:_(i32) = G_ADD %2, %3
    $w0 = COPY %8(i32)
```

注意三点：

1. 参数 COPY 的目标类型从 `i8` 变成了 `i32`（`$w0` 本来就是 32 位）；
2. `G_ADD` 在 `i32` 上做；
3. 返回也直接用 `i32`。

**没有任何截断指令**：AArch64 的 32 位寄存器写入会自动清零高 32 位，低 8 位就是结果，ABI 也只关心低 8 位。合法化"拓宽"时利用了目标语义，而不是机械地加 `G_TRUNC`。

这正是 Legalizer 需要目标知识的原因：同样的 `i8 add`，在另一个目标上可能被拆成 `G_ADD` + `G_AND 0xff`。

---

## 4.4 实跑：`i128` 加法被拆成带进位链

```llvm
define i128 @add128(i128 %a, i128 %b) {
  %r = add i128 %a, %b
  ret i128 %r
}
```

Legalizer 之后：

```yaml
    %2:_(i64) = COPY $x0
    %3:_(i64) = COPY $x1
    %4:_(i64) = COPY $x2
    %5:_(i64) = COPY $x3
    %13:_(i64), %19:_(i32) = G_UADDO %2, %4
    %15:_(i64), %18:_(i32) = G_UADDE %3, %5, %19
    $x0 = COPY %13(i64)
    $x1 = COPY %15(i64)
    RET_ReallyLR implicit $x0, implicit $x1
```

这是 `NarrowScalar` 的典型产物：

- `G_UADDO`：加法 + 溢出标志（两个结果）；
- `G_UADDE`：带进位输入的加法 + 溢出标志；
- 进位类型是 `i32`，因为 AArch64 的进位在 32 位状态寄存器里。

两个观察：

1. **合法化会产生多结果指令**（`%13` 和 `%19` 同时定义）。第 14 章会讲选择器如何匹配这种指令。
2. **vreg 编号出现大量空洞**（%6–%12 不存在），因为拆解过程中产生的中间值被 artifact 合并删掉了。

`i128` 的 ABI 是"两个 64 位寄存器传参/返回"，所以 `$x0/$x1` 和 `$x2/$x3` 成对出现。

---

## 4.5 实跑：没有硬件除法就调 libcall

RISC-V 去掉 M 扩展：

```console
llc -mtriple=riscv64-linux-gnu -mattr=-m -O0 -global-isel \
    -stop-after=legalizer div.ll -o div-leg.mir
```

```yaml
    ADJCALLSTACKDOWN 0, 0, implicit-def $x2, implicit $x2
    $x10 = COPY %0(s64)
    $x11 = COPY %1(s64)
    PseudoCALL target-flags(riscv-call) &__divdi3, csr_ilp32_lp64,
               implicit-def $x1, implicit $x10, implicit $x11, implicit-def $x10
    ADJCALLSTACKUP 0, 0, implicit-def $x2, implicit $x2
    %2:_(s64) = COPY $x10
```

`Libcall` 动作把一条 `G_SDIV` 变成完整的调用序列：参数搬运、栈调整、`PseudoCALL`、结果取回。这条路径完全由 `LibcallLoweringInfo` 驱动，不经过 InstructionSelect。

**这解释了一类"选择器里找不到除法 pattern"的困惑**：先确认目标 Legalizer 是否把 `G_SDIV` 标成了 `Libcall`，而不是 `Legal`。标成 `Libcall` 时，`G_SDIV` 根本不会留到选择阶段。

---

## 4.6 实跑：artifact 合并

```llvm
%v = load i8, ptr %p
%e = sext i8 %v to i32
```

IRTranslator 产生两条指令：

```yaml
%1:_(i8) = G_LOAD %0(p0) :: (load (i8) from %ir.p)
%2:_(i32) = G_SEXT %1(i8)
```

Legalizer 之后合成一条：

```yaml
%2:_(i32) = G_SEXTLOAD %0(p0) :: (load (i8) from %ir.p)
```

这不是"合法化动作"，而是 `LegalizationArtifactCombiner` 在合法化过程中顺手做的合并：`G_LOAD` 被拓宽/拆分时产生的 `G_SEXT` 属于"artifact"（合法化的副产物），能折回内存操作就折回。

工作队列里专门有一个 `ArtifactList`，并在无法合法化时把 artifact 放进 `RetryList`：

```cpp
      if (Res == LegalizerHelper::UnableToLegalize) {
        // Move illegal artifacts to RetryList instead of aborting because
        // legalizing InstList may generate artifacts that allow
        // ArtifactCombiner to combine away them.
        if (isArtifact(MI)) { RetryList.push_back(&MI); continue; }
        return {Changed, &MI};
      }
```

这解释了为什么 Legalizer 的代码比"查表—替换"复杂得多：它要处理大量临时产物。

---

## 4.7 RegBankSelect：选寄存器组

### 4.7.1 什么是 bank

AArch64 有三个 bank：

| bank | 内容 | 典型指令 |
| --- | --- | --- |
| `gpr` | 通用寄存器 `x0–x30`、`w0–w30`、`sp` | `G_ADD`、地址运算 |
| `fpr` | 浮点/向量寄存器 `q0–q31` | `G_FADD`、向量运算 |
| `ccr` | 条件码 | 比较结果的部分用法 |

bank 比寄存器类粗：`gpr` 包含 `gpr32`、`gpr64`、`gpr64sp` 等多个类。选 bank 只是决定"用哪个寄存器堆"，具体用哪个类要等 InstructionSelect。

### 4.7.2 两种模式

```text
-regbankselect-fast    默认映射（默认）
-regbankselect-greedy  枚举所有可能映射，按代价选最优
```

源码里 Fast 模式直接取 `getInstrMapping(MI)`，只算一次代价用于判可行；Greedy 模式拿 `getInstrPossibleMappings(MI)` 再 `findBestMapping`。

一个容易踩的坑：

```cpp
  Mode SaveOptMode = OptMode;
  if (F.hasOptNone())
    OptMode = Mode::Fast;
```

**带 `optnone` 的函数强制 Fast**。`-O0` 生成的函数默认带 `optnone`，所以在 `-O0` 下加 `-regbankselect-greedy` 不生效。要观察 Greedy，必须用 `-O1 -global-isel`。

### 4.7.3 遍历与跳过

```cpp
  // Use a RPOT to make sure all registers are assigned before we choose
  // the best mapping of the current instruction.
  ReversePostOrderTraversal<MachineFunction*> RPOT(&MF);
  for (MachineBasicBlock *MBB : RPOT) {
    SmallVector<MachineInstr *> WorkList(make_pointer_range(reverse(MBB->instrs())));
    while (!WorkList.empty()) {
      MachineInstr &MI = *WorkList.pop_back_val();
      if (isTargetSpecificOpcode(MI.getOpcode()) && !MI.isPreISelOpcode())
        continue;                       // BL、RET_ReallyLR 等
      if (MI.isInlineAsm()) continue;
      if (MI.isImplicitDef()) continue;
      if (!assignInstr(MI)) { reportGISelFailure(...); return false; }
    }
  }
```

`reverse(...)` + `pop_back_val()` 的组合效果是**块内正序**，配合 RPOT 保证"定义先于使用"，与 IRTranslator 一致，与 InstructionSelect 相反。

跳过三类指令：

- target-specific 指令（`BL`、`RET_ReallyLR`）已经用寄存器类，不需要 bank；
- inline asm 的约束由汇编模板决定；
- `IMPLICIT_DEF` 必须有寄存器类。

优化提示（`G_ASSERT_ZEXT` 等）有特殊规则：**跟随源寄存器的 bank**。

```cpp
    MRI->setRegBank(MI.getOperand(0).getReg(), *RB);
```

---

## 4.8 实跑：浮点运算落到 FPR

```yaml
%0:fpr(f32) = COPY $s0
%1:fpr(f32) = COPY $s1
%2:fpr(f32) = G_FADD %0, %1
$s0 = COPY %2(f32)
```

`G_FADD` 只有 FPR 映射，于是整条链都在 `fpr`。

## 4.9 实跑：跨 bank 的两种处理

### 情形一：指令本身跨 bank（不插 COPY）

```llvm
%i = fptosi float %f to i32
```

```yaml
%0:fpr(f32) = COPY $s0
%1:gpr(i32) = G_FPTOSI %0(f32)
$w0 = COPY %1(i32)
```

`G_FPTOSI` 的映射允许"输入 FPR、输出 GPR"，所以一条指令横跨两个 bank，没有 COPY。

### 情形二：值需要在 bank 之间搬运（每个使用点一条 COPY）

```llvm
%f = bitcast i32 %x to float
%r = fadd float %f, %f
```

```yaml
%0:gpr(i32) = COPY $w0
%1:gpr(f32) = G_BITCAST %0(i32)
%3:fpr(f32) = COPY %1(f32)
%4:fpr(f32) = COPY %1(f32)
%2:fpr(f32) = G_FADD %3, %4
```

三个细节：

1. `G_BITCAST` 留在 `gpr`（它本质是"同一寄存器的不同解释"，AArch64 上可以是空操作）；
2. `%f` 被 `fadd` 用了**两次**，于是插入**两条** COPY，而不是一条；
3. 这正是 Fast 模式的典型产物——它不计算"把 bitcast 整体搬到 fpr 是否更便宜"。

Greedy 模式会尝试避免这种重复搬运，但受限于目标提供的候选映射；不是所有情况都能优化掉。

### 4.9.1 COPY 的代价是不对称的

`AArch64RegisterBankInfo.cpp`：

```cpp
if (&A == &AArch64::GPRRegBank && &B == &AArch64::FPRRegBank)
  return 5;   // FMOVXDr / FMOVWSr
if (&A == &AArch64::FPRRegBank && &B == &AArch64::GPRRegBank)
  return 4;   // FMOVDXr / FMOVSWr
```

GPR→FPR 代价 5，FPR→GPR 代价 4。代价模型是目标自定义的，不是"一次 COPY = 1"。

### 4.9.2 修复方式只有两种

`applyMapping` 里的 `switch`：

```cpp
case RepairingPlacement::Reassign:
  MRI->setRegBank(Reg, *ValMapping.BreakDown[0].RegBank);   // 直接改 bank
  break;
case RepairingPlacement::Insert:
  OpdMapper->createVRegs(OpIdx);
  repairReg(MO, ValMapping, RepairPt, OpdMapper->getVRegs(OpIdx));  // 插 COPY/拆分
  break;
```

`Reassign` 用于"这个 vreg 还没定 bank，直接定成需要的"；`Insert` 用于"已经定了、但和这里要求不一致"，需要复制或拆分。

---

## 4.10 两个阶段的分工

| 问题 | 由谁回答 | 失败后果 |
| --- | --- | --- |
| 这条指令在目标上能不能选？ | Legalizer | 无法合法化 → fallback |
| 这个值放哪个寄存器堆？ | RegBankSelect | 无可行映射 → fallback |
| 具体用哪条指令、哪个寄存器类？ | InstructionSelect | 无匹配 pattern → fallback |

一个实用的推论：**如果 MIR 在 `-stop-after=legalizer` 就已经异常（指令被拆得面目全非、出现了意外的 libcall），不要去看选择器的 pattern**，问题在合法性规则。

---

## 进阶延伸

- 读 `LegalizeAction::Custom` 与 `LegalizerHelper`，理解 target 自定义 legalization 的入口。
- 对比 greedy 与 fast RegBankSelect 在同一 gMIR 上的 COPY 数量与代价。
- 思考：artifact combine 与 Legalizer 的职责边界在哪里？为什么不让 InstructionSelect 处理 `G_SEXT(G_LOAD)`？
## 踩雷清单

1. **以为 Legalizer 只做"合法/不合法"二选一**。它还会拓宽、缩窄、拆向量、生成 libcall、合并 artifact，改动量可能远大于选择阶段。
2. **在 `-O0` 下用 `-regbankselect-greedy` 期待差异**。`optnone` 会强制 Fast。
3. **把每条 COPY 都当成冗余**。跨 bank 的 COPY 是 RegBankSelect 的合法产物，删掉会破坏寄存器类约束；重复 COPY 是 Fast 模式的已知代价。
4. **用 `G_SDIV` 找不到 pattern 就改选择器**。先确认 Legalizer 是否把它标成 `Libcall`，那样它根本不会到选择阶段。
5. **假设 bank 等于寄存器类**。`gpr` 覆盖 32/64 位和 SP 多个类；`gpr` 到 `gpr64` 的收紧发生在 InstructionSelect。
6. **忽略 `G_UADDO`/`G_UADDE` 的多结果**。写选择器 pattern 时只匹配单结果形式，会漏掉大整数加法。

---

## 动手练习

### 练习 4.1 读规则表

打开 `AArch64LegalizerInfo.cpp`，找到 `G_LOAD`、`G_STORE`、`G_SEXTLOAD` 的规则，说明：(a) 哪些类型直接合法；(b) 不合法时第一条命中的动作是什么。

### 练习 4.2 拓宽的边界

分别编译 `i8`、`i16`、`i32`、`i64` 的加法，列出 Legalizer 后的实际类型。解释为什么 `i8`/`i16` 都变成 `i32`，而 `i64` 不变。

### 练习 4.3 缩窄的产物

编译 `i128` 的 `add`、`sub`、`mul`，对比 Legalizer 后的指令数量与结果个数。`mul` 是否也变成两条指令？为什么？

### 练习 4.4 libcall 判定

用 `-mtriple=riscv64-linux-gnu -mattr=-m` 编译 `sdiv`、`udiv`、`srem`、`urem`，列出哪些走 libcall、库函数名分别是什么。再打开 M 扩展对比。

### 练习 4.5 观察 COPY 数量

构造 `bitcast` 后多次使用同一浮点值的函数，数一数 RegBankSelect 插入了多少条 COPY。再用 `-O1 -global-isel -regbankselect-greedy` 对比，解释差异或说明为什么没有差异。

### 练习 4.6 bank 冲突

构造一个同时被整数运算和浮点运算使用的值（提示：`bitcast` 或 `load` 后两种用法），观察 Fast 模式选择哪个 bank、在每个使用点插入了什么。

---

## 重点整理

- Legalizer 用目标规则表把 gMIR 变成"每条指令都合法"，动作包括 widen/narrow/拆向量/libcall/custom。
- `i8 add` 在 AArch64 上被拓宽到 `i32`，且不需要截断，因为 32 位写入自动清零高位。
- `i128 add` 被拆成 `G_UADDO` + `G_UADDE`，产生多结果与进位。
- 无硬件除法时 `Libcall` 直接生成调用序列，`G_SDIV` 不会进入选择阶段。
- Legalizer 内部有 artifact 合并（`G_LOAD`+`G_SEXT` → `G_SEXTLOAD`）。
- RegBankSelect 用 RPOT + 块内正序，保证定义先于使用；Fast 是默认，Greedy 需要非 `optnone`。
- 跨 bank 有两种处理：指令本身跨 bank（如 `G_FPTOSI`）、在使用点插 COPY。
- COPY 代价由目标定义，AArch64 上 GPR→FPR 与 FPR→GPR 不同。

---

## 自我检核

1. Legalizer 要求/设置/清除哪些属性？清除 `NoPHIs` 意味着什么？
2. `legalFor`、`widenScalarToNextPow2`、`clampScalar` 三者的执行顺序如何影响 `i8 add`？
3. 为什么 `i128 add` 的进位类型是 `i32` 而不是 `i1`？
4. 什么情况下 `G_SDIV` 不会出现在 `-stop-after=legalizer` 的输出里？
5. RegBankSelect 的 Fast 与 Greedy 有什么差别？为什么 `-O0` 下看不到差别？
6. `G_FPTOSI` 为什么不需要 COPY，而 `bitcast` 后的 `fadd` 需要？
7. 为什么同一个值被用两次会产生两条 COPY？

---

## 延伸阅读

- `llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h`：动作枚举、`LegalizeActionStep`、`LegalizeRuleSet` 链式 API。
- `llvm/lib/CodeGen/GlobalISel/Legalizer.cpp`：工作队列、artifact 重试、失败返回。
- `llvm/lib/CodeGen/GlobalISel/LegalizerHelper.cpp`：每个动作的具体实现（widen/narrow/libcall）。
- `llvm/lib/Target/AArch64/GISel/AArch64LegalizerInfo.cpp`：真实规则表，注意 `G_ADD` 与 `G_GLOBAL_VALUE` 的对比。
- `llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp`：`assignRegisterBanks`、`findBestMapping`、`applyMapping`。
- `llvm/include/llvm/CodeGen/RegisterBankInfo.h`：`InstructionMapping`、`ValueMapping`、`MappingCost`。
- `llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp`：GPR/FPR/CCR 三个 bank 与 COPY 代价。
- 官方文档《Generic MIR》的 Register Bank 一节：<https://llvm.org/docs/GlobalISel/GMIR.html#register-bank>

---

## 下一章

第 5 章开始进入选择器内部：**TableGen 如何把 `.td` 里的 pattern 导入 GlobalISel**，`GIInstructionSelector` 生成了什么，`GINodeEquiv` 起什么作用。

[第 5 章 TableGen 如何把 pattern 导入 GlobalISel](./05-tablegen-globalisel-import.md)
