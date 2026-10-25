# 练习 D — 调试一个 bank 选错的案例

> **目标**：从一个真实可见的 `G_PHI` 混 bank 案例出发，走完“现象 → 证据 → 根因 → 修复 → 回归测试”的全过程；再用 `optnone` 变体练习区分“真的选错”和“模式被强制”。练的是诊断顺序，不是猜。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包。IR→MIR 与完整汇编均为实测；lit/FileCheck 回归测试标 guided（官方包没有 FileCheck/llvm-lit）。
>
> **证据约定**：`实测` 是官方包上跑出的 MIR/asm；`源码依据` 是 23.1.4 的 copy-like/PHI 处理；`guided` 是需要自建 lit 或改源码的部分。

## 背景与动机

“bank 选错”通常不会以崩溃的形式出现。更常见的是：

- 某个 `G_PHI` 在 `fpr`，但一条入边在 `gpr`；RegBankSelect 的 MIR 里看不到 repair COPY；
- 返回路径多了一条 `fmov`，但你说不清它是 ABI 还是 bank 修复；
- 同一个函数加不加 `optnone`，`G_OR` 的 bank 完全不同；
- `-verify-machineinstrs` 明明通过了，性能却不对。

这些现象的共同点是：**bank 不一致可能被下游默默吸收**。RegBankSelect 只保证它自己映射的指令合法；PHI 的入边 bank 一致性不在它的局部 repair 合同里。于是，一份“verifier 通过”的 MIR 仍可能让 phi elimination 或寄存器分配替你做跨 bank 搬运。

练习 D 选了一个最小、可复现的案例：一个 `i32` PHI，一条入边来自浮点 bitcast（FPR），另一条来自整数加法（GPR）。RegBankSelect 把 PHI 放在 FPR，但没有为 GPR 入边插入 COPY。我们先证明它确实发生，再从源码解释为什么，最后用“在 merge 前把值归一化到 GPR”修复，并写出能锁住行为的回归测试。

## 先建立直觉

把 PHI 想成两条传送带的汇合口：

```text
   then 路径：G_BITCAST(float) ──▶ %10:fpr(i32) ──┐
                                                   ├──▶ %11:G_PHI ──▶ $w0
   else 路径：G_ADD(i32) ───────▶ %6:gpr(i32)  ──┘

   如果 PHI 选了 fpr：
     %10 可以直接进；
     %6  需要一次 GPR→FPR 搬运，但 RegBankSelect 没有在 MIR 里插这条 COPY。
```

这不是“PHI 不能跨 bank”，而是“PHI 的 mapping 只描述定义，不描述每条入边”。你看到的 `%11:fpr(i32)` 是 PHI 定义的 bank；`%6:gpr(i32)` 是入边自己的 bank。两者不一致时，RegBankSelect 不会像普通指令那样为每个操作数做局部 repair，因为 copy-like 指令的 mapping 只覆盖定义。

诊断时要先问三个问题：

1. 这条指令是不是 copy-like（`COPY`/`G_PHI`/`REG_SEQUENCE`）？
2. 它的 bank 是从哪条已 bank 的入边继承的？
3. 下游有没有替它补 COPY（看 asm 的 `fmov`，或看 phi elimination 后的 MIR）？

## 任务规格

### 输入

保存为 `practice-d/phi-mixed.ll`：

```llvm
define i32 @phi_mixed(i32 %a, i32 %b, i1 %c) {
entry:
  br i1 %c, label %t, label %e
t:
  %fa = bitcast i32 %a to float
  %fb = bitcast i32 %b to float
  %ft = fadd float %fa, %fb
  %ti = bitcast float %ft to i32
  br label %m
e:
  %ei = add i32 %a, %b
  br label %m
m:
  %p = phi i32 [ %ti, %t ], [ %ei, %e ]
  ret i32 %p
}
```

先用 `-O0`（不要给函数加 `optnone`）跑：

```powershell
$llc = "D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin\llc.exe"

& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  practice-d\phi-mixed.ll -o practice-d\phi-mixed-rbs.mir
```

### 输出

你需要交出：

1. 一份出问题的 MIR 片段，标出 PHI 定义 bank 与两条入边 bank。
2. 一条诊断链：为什么 verifier 通过、为什么没有 repair COPY、下游哪里付了代价。
3. 一个修复方案与修复后的 MIR。
4. 一个回归测试，能锁住“PHI 与其入边同 bank”。
5. 一个 `optnone` 变体的对照，说明如何区分“真错”与“被强制 Fast”。

### 约束

- 诊断完成前不要改 IR；先把现象和证据固定下来。
- 判定以 `-stop-after=regbankselect` 的 MIR 为准；完整 asm 只作为下游代价的旁证。
- 不要用 `-simplify-mir`；它会隐藏 `registers:` 和 `liveins:`。
- 回归测试必须能失败：如果修复被回退，测试要能指出 PHI bank 或入边 bank 不一致。
- lit/FileCheck 部分标 guided；本地用 `Select-String` 检查也可以。

### 验收标准

- 能指出 `%11:fpr(i32)` 和 `%6:gpr(i32)` 的 bank 冲突。
- 能解释为什么 RegBankSelect 没有为 `%6` 插入 COPY（源码级原因）。
- 修复后 `%13:gpr(i32) = G_PHI`，两条入边都是 GPR。
- 能说出显式 repair COPY 数与隐式 PHI 边搬运的区别。
- 能复现 `optnone` 下 `-regbankselect-greedy` 被强制 Fast 的现象，并正确归因。

## 期望输出

### 修复前（实测）

```yaml
body:             |
  bb.1.entry:
    successors: %bb.2(0x40000000), %bb.3(0x40000000)
    liveins: $w0, $w1, $w2

    %0:gpr(i32) = COPY $w0
    %1:gpr(i32) = COPY $w1
    %4:gpr(i32) = COPY $w2
    %14:gpr(i32) = G_CONSTANT i32 1
    %12:gpr(i32) = G_AND %4, %14
    G_BRCOND %12(i32), %bb.2
    G_BR %bb.3

  bb.2.t:
    successors: %bb.4(0x80000000)

    %7:gpr(f32) = G_BITCAST %0(i32)
    %8:gpr(f32) = G_BITCAST %1(i32)
    %15:fpr(f32) = COPY %7(f32)
    %16:fpr(f32) = COPY %8(f32)
    %9:fpr(f32) = G_FADD %15, %16
    %10:fpr(i32) = G_BITCAST %9(f32)
    G_BR %bb.4

  bb.3.e:
    successors: %bb.4(0x80000000)

    %6:gpr(i32) = G_ADD %0, %1
    G_BR %bb.4

  bb.4.m:
    %11:fpr(i32) = G_PHI %10(i32), %bb.2, %6(i32), %bb.3
    $w0 = COPY %11(i32)
    RET_ReallyLR implicit $w0
...
```

冲突点：

- `%11` 的定义 bank 是 `fpr`；
- 第一条入边 `%10` 是 `fpr`；
- 第二条入边 `%6` 是 `gpr`；
- MIR 里没有 `%?:gpr = COPY ...` 或 `%?:fpr = COPY ...` 来修复 `%6`。

### 修复后（实测）

```yaml
body:             |
  bb.0.entry:
    successors: %bb.1(0x40000000), %bb.2(0x40000000)
    liveins: $w0, $w1, $w2

    %0:gpr(i32) = COPY $w0
    %1:gpr(i32) = COPY $w1
    %4:gpr(i32) = COPY $w2
    %18:gpr(i32) = G_CONSTANT i32 1
    %16:gpr(i32) = G_AND %4, %18
    G_BRCOND %16(i32), %bb.1
    G_BR %bb.2

  bb.1.t:
    successors: %bb.3(0x80000000)

    %8:gpr(f32) = G_BITCAST %0(i32)
    %9:gpr(f32) = G_BITCAST %1(i32)
    %19:fpr(f32) = COPY %8(f32)
    %20:fpr(f32) = COPY %9(f32)
    %10:fpr(f32) = G_FADD %19, %20
    %11:fpr(i32) = G_BITCAST %10(f32)
    %15:gpr(i32) = G_CONSTANT i32 255
    %21:gpr(i32) = COPY %11(i32)
    %12:gpr(i32) = G_AND %21, %15
    G_BR %bb.3

  bb.2.e:
    successors: %bb.3(0x80000000)

    %14:gpr(i32) = G_CONSTANT i32 255
    %7:gpr(i32) = G_AND %1, %14
    G_BR %bb.3

  bb.3.m:
    %13:gpr(i32) = G_PHI %12(i32), %bb.1, %7(i32), %bb.2
    $w0 = COPY %13(i32)
    RET_ReallyLR implicit $w0
...
```

修复后：

- PHI `%13` 是 `gpr`；
- 两条入边 `%12`、`%7` 都是 `gpr`；
- merge 块里没有跨 bank PHI 入边；
- 返回 `$w0 = COPY %13` 是同 bank，代价 0。

### 代价对比

| 阶段 | PHI bank | 入边 bank | 显式跨 bank COPY | PHI 边隐式搬运 | 完整 asm 的 `fmov` 数 |
| --- | --- | --- | --- | --- | --- |
| 修复前 | `fpr` | `fpr` / `gpr` | 3 | 1（GPR→FPR） | 4 |
| 修复后 | `gpr` | `gpr` / `gpr` | 3 | 0 | 3 |

“显式跨 bank COPY”在修复前后都是 3 条：两条 GPR→FPR 把整数参数送进 `G_FADD`，一条 FPR→GPR 把浮点结果送回整数域。修复没有消除这三条；它消除的是 PHI 另一条入边的隐式搬运，并把返回 COPY 变成同 bank。

完整汇编旁证（实测，节选）：

```asm
# 修复前：else 路径多一条 fmov，merge 再 fmov 回 GPR
.LBB0_2:
    add w8, w8, w9
    fmov s0, w8          # GPR -> FPR，为 PHI 的 fpr 定义补搬运
    str s0, [sp, #4]
.LBB0_3:
    ldr s0, [sp, #4]
    fmov w0, s0          # FPR -> GPR，返回
```

```asm
# 修复后：else 路径直接以 GPR 存回，merge 不需要 fmov
.LBB0_2:
    and w8, w8, #0xff
    str w8, [sp, #4]
.LBB0_3:
    ldr w0, [sp, #4]
```

这段 asm 不是判定依据，但它证明“MIR 里没有 COPY”不等于“机器码里没有搬运”。

## 如果你卡住了

1. **只看 PHI 定义，不看入边**：`%11:fpr(i32)` 只说明定义在 FPR。把每条入边的 vreg 也标出来：`%10:fpr`、`%6:gpr`。冲突在入边，不在定义。
2. **以为 verifier 会报错**：`-verify-machineinstrs` 对这份 MIR 通过（实测）。PHI 的 mapping 只覆盖定义，verifier 也按这个合同检查；入边 bank 不一致不是它这一层的错误。
3. **把 `$w0 = COPY %11` 当成唯一问题**：这条是 FPR→GPR 的返回搬运；即使 PHI 改成 GPR，仍可能有一条 FPR→GPR 在别处（修复后的 `%21`）。要区分“返回路径”和“PHI 边”。
4. **用重排 PHI 入边来“修复”**：把 GPR 入边放前面，PHI 会选 GPR，但 FPR 入边又变成不一致；你只是把搬运换到另一条路径，没有消除冲突。
5. **把 `optnone` 当成 `-O0`**：真正强制 Fast 的是函数属性 `optnone`，不是 `llc -O0`。先看 IR 有没有 `attributes #0 = { optnone ... }`，再看 `runOnMachineFunction` 的 `if (F.hasOptNone())`。

## 实作步骤建议

1. **复现**：保存 `phi-mixed.ll`，用 `-O0 -global-isel -regbankselect-greedy -stop-after=regbankselect` 跑出 MIR，确认 `%11:fpr` 与 `%6:gpr`。
2. **确认 verifier 态度**：加 `-verify-machineinstrs` 再跑一次，记录它是否报错。通过也要记录，因为这说明问题不在 verifier 层。
3. **找下游代价**：用完整 `llc -O0 -global-isel` 生成 asm，数 `fmov`。修复前应为 4 条；这是“隐式搬运”存在的旁证。
4. **改 IR**：在浮点路径的 bitcast 之后加一个 GPR-only 的整数操作（本解答用 `and i32 %ti, 255`），让两条入边都在 GPR；重新跑 MIR，确认 PHI 变 `gpr`。
5. **写回归**：把修复后的 legalized MIR 存成 fixture，用 `-run-pass=regbankselect -regbankselect-greedy` 跑；检查 PHI 是 `gpr`，且 merge 块没有 `:fpr(i32) = G_PHI`。lit 版本标 guided。

## 完整参考解答

<details>
<summary>展开：诊断树、源码根因、修复与回归测试</summary>

### 1. 诊断树

```text
现象：某个 vreg 的 bank 与预期不符
│
├─ 1. pass 真的跑了吗？
│    ├─ regBankSelected: true ?          否 → 检查命令/属性
│    └─ selected: false ?                否 → 你跑到了 ISel 之后
│
├─ 2. 指令是 copy-like 吗？
│    ├─ COPY / G_PHI / REG_SEQUENCE
│    │    ├─ bank 来自第一个已 bank 的 operand
│    │    ├─ 检查所有入边是否同 bank
│    │    └─ 入边不一致 → 下游 phi elimination/RA 付代价
│    └─ 普通指令
│         ├─ getInstrMapping 有显式 case？ → 看 case 的 mapping
│         └─ 没有 → type fallback / regclass 约束
│
├─ 3. 函数有 optnone 吗？
│    └─ 有 → RegBankSelect 强制 Fast，Greedy 不生效
│
├─ 4. 当前是 Fast 还是 Greedy？
│    ├─ Fast → 只看默认 mapping
│    └─ Greedy → 默认 + alternatives，按 MappingCost 选
│
├─ 5. 有 repair COPY 吗？
│    ├─ 有 → 看 copyCost、操作数方向
│    └─ 没有 → 可能是同 bank、PHI 入边、或 fallback
│
└─ 6. 是否 fallback 到 SelectionDAG？
     └─ failedISel / -global-isel-abort=1 的行为
```

本案例落在第 2 支：`G_PHI` 是 copy-like，定义 bank 来自 `%10:fpr`，第二条入边 `%6:gpr` 没有被检查。

### 2. 源码根因

`RegisterBankInfo::getInstrMappingImpl` 对 copy-like 指令只映射定义：

```cpp
static bool isCopyLike(const MachineInstr &MI) {
  return MI.isCopy() || MI.isPHI() ||
         MI.getOpcode() == TargetOpcode::REG_SEQUENCE;
}

bool IsCopyLike = isCopyLike(MI);
// For copy like instruction, only the mapping of the definition
// is important. The rest is not constrained.
unsigned NumOperandsForMapping = IsCopyLike ? 1 : MI.getNumOperands();
```

对 PHI，`getInstrMappingImpl` 会遍历 operand，找第一个已经带 bank 的寄存器作为 `CurRegBank`，然后把这个 bank 用作定义的 mapping：

```cpp
const RegisterBank *AltRegBank = getRegBank(Reg, MRI, TRI);
const RegisterBank *CurRegBank = IsCopyLike ? AltRegBank : nullptr;
// ...
if (IsCopyLike) {
  if (!OperandsMapping[0]) {
    OperandsMapping[0] = ValMapping;
  }
  // 只检查 cannotCopy，不检查所有入边是否同 bank
  // ...
  CompleteMapping = true;
  break;
}
```

本案例的 operand 顺序是：`%11`（定义，还没有 bank）、`%10`（fpr）、`%bb.2`、`%6`（gpr）、`%bb.3`。第一条带 bank 的寄存器是 `%10`，所以 `%11` 被定为 FPR。之后的循环会继续扫过 `%6`，但只调用 `cannotCopy(fpr, gpr)` 判断“能不能搬”，不会要求入边同 bank；`cannotCopy` 返回 false，于是 `CompleteMapping = true` 后 `break`。

`RegBankSelect::computeMapping` 又只遍历 mapping 的 `NumOperands`：

```cpp
for (unsigned OpIdx = 0, EndOpIdx = InstrMapping.getNumOperands();
     OpIdx != EndOpIdx; ++OpIdx) {
  // ...
}
```

PHI 的 `NumOperands` 是 1（只有定义），所以 `%6` 根本不在 repair 检查范围内。`InstructionMapping::verify` 也断言：

```cpp
assert(NumOperands == (isCopyLike(MI) ? 1 : MI.getNumOperands()) &&
       "NumOperands must match, see constructor");
```

于是 verifier 通过，MIR 里也没有为 `%6` 插 COPY。这不是“RegBankSelect 漏了一个显然的 bug”，而是它的合同：**copy-like 指令只保证定义的 bank；入边一致性由 PHI 的消费者/下游处理。** 代价是下游可能插一条你没有预算的跨 bank 搬运。

### 3. 修复：在 merge 前归一化

修复思路：不要让 PHI 汇合两个不同 bank 的值。如果这个值在语义上是整数，就在浮点路径上把它转回 GPR，再做 PHI。

```llvm
t:
  %fa = bitcast i32 %a to float
  %fb = bitcast i32 %b to float
  %ft = fadd float %fa, %fb
  %ti = bitcast float %ft to i32
  %ti2 = and i32 %ti, 255
  br label %m
e:
  %ei = and i32 %b, 255
  br label %m
m:
  %p = phi i32 [ %ti2, %t ], [ %ei, %e ]
  ret i32 %p
```

这里用 `and i32 ..., 255` 有两个作用：

- 它是 GPR-only 的整数操作，RegBankSelect 会为 `%ti` 插入 FPR→GPR 的 repair；`%ti2` 明确在 GPR。
- 它让两条入边同 bank，PHI 选 GPR，merge 后不再有跨 bank 边。

代价与取舍：

- 修复前：PHI 边一次隐式 GPR→FPR，返回一次 FPR→GPR；asm 4 条 `fmov`。
- 修复后：浮点结果先显式 FPR→GPR（`%21`），PHI 边不再搬，返回同 bank；asm 3 条 `fmov`。
- 但修复多了一条 `and`。如果这个掩码本来就是算法的一部分，它是免费的；如果只是为了改 bank 而加，它可能不值得。**不要无条件把“PHI 同 bank”当成必须；先看哪条路径更热、这条搬运是否真的出现在热路径上。**

其他修复思路：

| 方案 | 效果 | 风险 |
| --- | --- | --- |
| merge 前用 GPR-only 操作归一化 | PHI 同 bank，消除隐式边搬运 | 可能多一条指令 |
| 重排 PHI 入边 | PHI 改选另一 bank | 只是把搬运换到另一条路径 |
| 改成 `select` | 可能 if-convert，银行由 select 的启发式决定 | 改变控制流/代价，需重新测量 |
| 保持 FPR PHI，接受一条边搬运 | 代码少 | 热路径可能多一次 `fmov` |

本案例选择第一种，因为 `and` 可以并入真实语义；如果它不能，应该先测量再决定。

### 4. 回归测试

#### 4.1 本地 MIR 检查（实测）

从修复后的 IR 生成 legalized MIR：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O0 -global-isel `
  -global-isel-abort=1 -stop-after=legalizer `
  practice-d\phi-mixed-fixed.ll -o practice-d\phi-mixed-fixed-legal.mir

& $llc -mtriple=aarch64-linux-gnu -run-pass=regbankselect `
  -regbankselect-greedy -verify-machineinstrs `
  practice-d\phi-mixed-fixed-legal.mir -o practice-d\phi-mixed-fixed-rbs.mir
```

检查：

```powershell
Select-String -Path practice-d\phi-mixed-fixed-rbs.mir `
  -Pattern "G_PHI|:fpr\(i32\) = COPY"
```

期望：

- 有一行 `%13:gpr(i32) = G_PHI %12(i32), %bb.1, %7(i32), %bb.2`；
- 没有 `:fpr(i32) = G_PHI`；
- 没有 merge 块内的跨 bank COPY。

#### 4.2 lit 测试草图（guided）

官方包没有 FileCheck/llvm-lit；下面假设 `%s` 是 4.1 的 legalized MIR，只是可提交的形状：

```yaml
# RUN: llc -mtriple=aarch64-- -run-pass=regbankselect \
# RUN:   -regbankselect-greedy -verify-machineinstrs %s -o - | FileCheck %s

# CHECK-LABEL: name: phi_mixed_fixed
# CHECK: %[[PHI:.*]]:gpr(i32) = G_PHI
# CHECK-SAME: %bb.1
# CHECK-SAME: %bb.2
# CHECK-NOT: :fpr(i32) = G_PHI
```

如果一个测试只 CHECK `%[[PHI]]:gpr`，它可能被 fallback 或别的路径满足；`CHECK-NOT: :fpr(i32) = G_PHI` 和入边检查更接近真正的行为锁。

### 5. `optnone` 变体：另一种“bank 不对”

准备 `or-optnone.ll`：

```llvm
define float @or_f(float %a, float %b) #0 {
  %ai = bitcast float %a to i32
  %bi = bitcast float %b to i32
  %r = or i32 %ai, %bi
  %re = bitcast i32 %r to float
  ret float %re
}
attributes #0 = { optnone noinline }
```

跑：

```powershell
& $llc -mtriple=aarch64-linux-gnu -O2 -global-isel `
  -global-isel-abort=1 -regbankselect-greedy -stop-after=regbankselect `
  practice-d\or-optnone.ll -o practice-d\or-optnone-greedy.mir
```

输出（实测）是 Fast 的结果：`G_OR` 在 GPR，3 条跨 bank COPY。为什么？

```cpp
// RegBankSelect::runOnMachineFunction
const Function &F = MF.getFunction();
Mode SaveOptMode = OptMode;
if (F.hasOptNone())
  OptMode = Mode::Fast;
```

`optnone` 覆盖命令行 `-regbankselect-greedy`。这和 `llc -O0` 无关：`-O0` 本身不会给函数加 `optnone`；只有 IR 里显式写了属性，或 `clang -O0` 生成时带了，才会触发。

诊断分叉：

- 如果 `G_OR` 在 GPR 且函数有 `optnone`：这是“被强制 Fast”，不是 mapping 选错；修复是去掉 `optnone` 或接受 Fast。
- 如果 `G_OR` 在 GPR 且函数没有 `optnone`、命令是 Greedy：回去看 alternative mapping、cost 和操作数 bank；这才可能是真正的选择问题。

回归测试可以写两个函数：一个带 `optnone`，一个不带，分别 CHECK `G_OR` 的 bank。这样下次有人把 `optnone` 加到热函数上，测试能指出行为变化。

</details>

## 测试用例

| 用例 | 输入/命令 | 期望 | 证据级别 |
| --- | --- | --- | --- |
| 复现混 bank | `phi-mixed.ll` + Greedy | `%11:fpr(i32)`，`%6:gpr(i32)` | 实测 |
| verifier 态度 | 同上 + `-verify-machineinstrs` | 通过（不报错） | 实测 |
| 下游代价（前） | 完整 `llc -O0 -global-isel` | 4 条 `fmov` | 实测 |
| 修复后 PHI | `phi-mixed-fixed.ll` + Greedy | `%13:gpr(i32) = G_PHI` | 实测 |
| 修复后入边 | 同上 | `%12:gpr`、`%7:gpr` | 实测 |
| 下游代价（后） | 完整 `llc -O0 -global-isel` | 3 条 `fmov` | 实测 |
| 回归检查 | `Select-String ':fpr\(i32\) = G_PHI' 应为空；':gpr\(i32\) = G_PHI' 1 条 | 只有 gpr PHI，无 fpr PHI | 实测 |
| lit 回归 | FileCheck 版本 | CHECK gpr PHI + CHECK-NOT fpr PHI | guided |
| `optnone` 变体 | `or-optnone.ll` + Greedy | `G_OR` 在 GPR，3 条 COPY | 实测 |
| 去掉 `optnone` | `or.ll` + Greedy | `G_OR` 在 FPR，0 条 COPY | 实测 |

## 延伸挑战

1. **循环 PHI**：把 diamond 改成 loop，让 PHI 的一条入边来自循环回边。预测 RegBankSelect 的 bank 选择是否仍由“第一条已 bank 入边”决定；注意 RPOT 处理顺序会影响哪条入边先被看到。
2. **关键边**：让 PHI 的前驱有多个后继（critical edge），观察 repair 是否触发边分裂。`RepairingPlacement` 对 PHI 入边的插入点有特殊处理；本案例没有走到那条路径，因为 PHI 入边根本没被检查。
3. **`-global-isel-abort=2`**：对比 `=1` 与 `=2` 的失败行为。`=1` 在无法映射时 abort，`=2` 会 fallback 到 SelectionDAG；用一份故意无法映射的 MIR 观察差异。
4. **`optnone` + `-O0` 交叉实验**：四个组合（有无 `optnone` × `-O0`/`-O2`）都跑一遍，确认只有 `optnone` 强制 Fast。把结果做成表，和 Ch 0 的结论对齐。
5. **用 `-debug-only=regbankselect` 看 PHI 的 mapping**（guided，需 assertions build）：观察 `getInstrMappingImpl` 选的是哪条入边、`computeMapping` 是否只打印一个 operand。官方包没有这个选项。

## 延伸阅读

- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - **读哪里**：`isCopyLike`、`getInstrMappingImpl` 的 copy-like 分支。
  - **学什么**：为什么 PHI 只映射定义、为什么第一条已 bank 入边决定 bank。
  - **和本练习的关联**：根因就在这段代码。
- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`computeMapping`、`RepairingPlacement` 的 PHI 分支、`runOnMachineFunction` 的 `optnone`。
  - **学什么**：PHI 的 repair 插入点、为什么 `computeMapping` 不检查入边、`optnone` 如何强制 Fast。
  - **和本练习的关联**：解释了“verifier 通过但没有 COPY”和 `optnone` 变体。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`G_PHI` 的 MIR 语法、`registers`、`liveins`。
  - **学什么**：PHI 入边的 `%vreg, %bb` 写法，bank 在定义上的位置。
  - **和本练习的关联**：不会读 PHI 的 MIR，就无法定位入边 bank。

## 自我检核

- [ ] 我能指出本案例的 PHI 定义 bank 与两条入边 bank，并说出冲突在哪
- [ ] 我能解释为什么 `-verify-machineinstrs` 通过，但 MIR 里仍可能有不一致的 PHI 入边
- [ ] 我能从源码说出 `getInstrMappingImpl` 为什么只映射 PHI 的定义
- [ ] 我能解释 `computeMapping` 为什么不会为第二条入边插 COPY
- [ ] 我能设计一个修复，使 PHI 与两条入边同 bank，并说出它的代价
- [ ] 我能区分显式 repair COPY、PHI 边隐式搬运、ABI 边界 COPY
- [ ] 我能复现 `optnone` 强制 Fast，并正确归因到属性而不是 `-O0`
- [ ] 我知道回归测试要锁 PHI bank 和入边，而不只是锁一个 `gpr`

练习 D 到此结束。下一站进入源码走读：把 AArch64 的 `RegisterBankInfo` 从 `copyCost` 到 alternative mapping 逐段读完。

→ [Ch 20 AArch64 RegisterBankInfo 源码走读](./20-aarch64-source-walkthrough.md)