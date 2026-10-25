# 第 9 章 嵌套匹配与折叠

> **本章目标**：理解 matcher 如何从“只匹配根指令”扩展到“把操作数的定义者也拉进同一条规则”，以及为什么“记录到定义者”不等于“可以删掉它”。读完后，你应该能解释 `GIM_RecordInsn` 与 `GIM_RecordInsnIgnoreCopies` 的差别，能看着一棵 gMIR 树说出 `MIs[0]`、`MIs[1]` 分别是谁，并能判断一次折叠是 TableGen 表、ComplexPattern 还是 C++ selector 做的。
>
> **前置**：第 5–8 章，尤其是第 6 章的 `MIs`/`GIR_*` 与第 7 章的立即数谓词。**环境**：LLVM 23.1.4，AArch64，预编译 `llc`。

---

## 9.1 根指令之外：为什么需要嵌套匹配

第 6 章的执行器只保证一件事：`State.MIs[0]` 是当前正在选择的根指令。只匹配根，能处理 `G_ADD %a, %b` 这种“操作数已经是 vreg”的情况；但真实 gMIR 里，操作数往往由另一条指令定义：

```llvm
define i64 @f(i64 %a) {
entry:
  %r = shl i64 %a, 3
  ret i64 %r
}
```

`-stop-after=regbankselect` 得到的不是“`G_SHL` 带立即数”，而是两条指令：

```yaml
%1:gpr(i64) = G_CONSTANT i64 3
%2:gpr(i64) = G_SHL %0, %1(i64)
```

只看 `MIs[0]`，matcher 只能看到“第二个操作数是 s64 vreg”，看不到“它由 `G_CONSTANT 3` 定义”。AArch64 的 `shl` 立即数规则需要后者，于是生成的表会：

1. 把根 `G_SHL` 放进 `MIs[0]`；
2. 把操作数 2 的定义者 `G_CONSTANT` 放进 `MIs[1]`；
3. 在 `MIs[1]` 上检查 opcode、类型和立即数范围，再渲染成 `UBFMXri`。

实跑（`-O0 -global-isel -global-isel-abort=1`）：

```asm
lsl     x0, x0, #3
ret
```

`-stop-after=instruction-select` 的 MIR 更能说明问题：

```yaml
%0:gpr64 = COPY $x0
%2:gpr64 = UBFMXri %0, 61, 60
$x0 = COPY %2
```

`G_CONSTANT` 消失了，`G_SHL` 变成了 `UBFMXri`。`61, 60` 由 `i64shift_a`/`i64shift_b` 算出，是把 `lsl #3` 映射到 UBFM 编码的结果。**嵌套匹配不是把树拍平，而是让一条规则同时拥有多个 `MIs` 槽位，并在这些槽位上分别检查。**

---

## 9.2 `State.MIs` 的编号与 `MIs[0]` 的不变性

`selectImpl` 的入口先把根放进 `MIs`：

```cpp
State.MIs.clear();
State.MIs.push_back(&I);
```

因此 `MIs[0]` 永远是当前根；`MIs[1]`、`MIs[2]`…… 是按生成表顺序记录的定义者；同一个 `MachineInstr*` 可以在多个槽位出现（共享常量很常见）；槽位编号由**表生成时**确定，不是运行时按地址或 vreg 编号分配的。

`GIM_RecordInsn` 的实现（`GIMatchTableExecutorImpl.h`）：

```cpp
case GIM_RecordInsn:
case GIM_RecordInsnIgnoreCopies: {
  uint64_t NewInsnID = readULEB();
  uint64_t InsnID = readULEB();
  uint64_t OpIdx = readULEB();

  // As an optimisation we require that MIs[0] is always the root. Refuse
  // any attempt to modify it.
  assert(NewInsnID != 0 && "Refusing to modify MIs[0]");

  MachineOperand &MO = State.MIs[InsnID]->getOperand(OpIdx);
  if (!MO.isReg()) { /* reject */ }
  if (MO.getReg().isPhysical()) { /* reject */ }

  MachineInstr *NewMI;
  if (MatcherOpcode == GIM_RecordInsnIgnoreCopies)
    NewMI = getDefIgnoringCopies(MO.getReg(), MRI);
  else
    NewMI = MRI.getVRegDef(MO.getReg());

  if ((size_t)NewInsnID < State.MIs.size())
    State.MIs[NewInsnID] = NewMI;
  else {
    assert((size_t)NewInsnID == State.MIs.size() &&
           "Expected to store MIs in order");
    State.MIs.push_back(NewMI);
  }
  break;
}
```

四个细节：

1. **不能记录物理寄存器。** 物理寄存器的定义者不在 `MRI` 的 SSA 图里，也没有“可折叠的定义”。
2. **不能记录非寄存器操作数。** 立即数、`MBB`、`CImm` 都会在 `!MO.isReg()` 处被拒绝；要匹配常量，必须记录定义它的 `G_CONSTANT`。
3. **编号必须按顺序增长。** 小于当前大小时允许覆盖（复用槽位）；否则必须正好等于 `size()`，保证 `MIs` 没有空洞。
4. **`NewInsnID != 0` 是生成表契约。** 大量快速路径假设 `MIs[0]` 是根：`GIM_RootCheckType` 省略 `InsnID`，`GIM_CheckIsSafeToFold` 从 `K = 1` 开始，渲染阶段也从 `MIs[0]` 取根操作数。release 构建里 assert 不存在，覆盖根不会立刻报错，而会在后续检查里产生难懂的匹配结果。写自定义 matcher 时，永远从 `MIs[1]` 开始分配。

---

## 9.3 `GIM_RecordInsn` 与 `GIM_RecordInsnIgnoreCopies`

两者的唯一差别在“拿到 vreg 之后怎么找定义者”：

| 操作码 | 找定义者的方式 | 看到 COPY 时 |
| --- | --- | --- |
| `GIM_RecordInsn` | `MRI.getVRegDef(reg)` | 记录 COPY 本身，后续 `GIM_CheckOpcode` 失败 |
| `GIM_RecordInsnIgnoreCopies` | `getDefIgnoringCopies(reg, MRI)` | 沿 COPY 链走到真正的定义者 |

`getDefIgnoringCopies` 在 `Utils.cpp` 里的核心循环是：

```cpp
MachineInstr *DefMI = MRI.getVRegDef(Reg);
unsigned Opc = DefMI->getOpcode();
while (Opc == TargetOpcode::COPY || isPreISelGenericOptimizationHint(Opc)) {
  Register SrcReg = DefMI->getOperand(1).getReg();
  if (!MRI.getType(SrcReg).isValid())
    break;
  DefMI = MRI.getVRegDef(SrcReg);
  Opc = DefMI->getOpcode();
}
```

它假设 SSA；只跳过 `COPY` 和 `PRE_ISEL_GENERIC_OPTIMIZATION_HINT`；**不跳过 `G_SEXT`、`G_ZEXT`、`G_ANYEXT`、`G_SHL`**；不携带 subregister 信息；中途类型无效时停止，返回当前定义。

### 9.3.1 实跑：COPY 阻断简单嵌套 pattern

把 9.1 的 MIR 改成：

```yaml
%1:gpr(i64) = G_CONSTANT i64 3
%2:gpr(i64) = COPY %1(i64)
%3:gpr(i64) = G_SHL %0, %2(i64)
```

运行：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -global-isel-abort=1 shl3-copy.mir -o shl3-copy.out.mir
```

输出变成：

```yaml
%0:gpr64 = COPY $x0
%4:gpr32 = MOVi32imm 3
%1:gpr64all = SUBREG_TO_REG %4, $subreg.sub_32
%2:gpr64 = COPY %1
%3:gpr64 = LSLVXr %0, %2
```

`G_SHL` 没有变成 `UBFMXri`，而是退化成变量移位 `LSLVXr`；常量也被物化。原因不是 COPY 让类型变了，而是 `shl` 的简单嵌套 pattern 记录的是 `%2` 的定义者 `COPY`，随后 `GIM_CheckOpcode(COPY, G_CONSTANT)` 失败。**`GIM_RecordInsn` 不负责看穿 COPY。**

### 9.3.2 实跑：ComplexPattern 会看穿 COPY

`add i64 %a, 1` 的 `ADDXri` 规则不是简单嵌套 pattern，而是 ComplexPattern `addsub_shifted_imm64`。它的 C++ 回调 `selectArithImmed` 内部使用 `getDefIgnoringCopies`。把 MIR 改成：

```yaml
%1:gpr(i64) = G_CONSTANT i64 1
%2:gpr(i64) = COPY %1(i64)
%3:gpr(i64) = G_ADD %0, %2(i64)
```

实跑输出：

```yaml
%0:gpr64sp = COPY $x0
%3:gpr64sp = ADDXri %0, 1, 0
```

`COPY` 和 `G_CONSTANT` 都消失了。这里不是 `GIM_RecordInsnIgnoreCopies` 的功劳，而是 ComplexPattern 回调自己看穿了 COPY。两者都会“忽略 COPY”，但发生在不同层：前者在执行器层，记录前看穿；后者在目标 C++ 回调层，匹配时看穿。**COPY 链是否被看穿，取决于具体 pattern 走的是哪条路径。**

---

## 9.4 多层记录：`rev16` 的真实例子

下面这段普通 IR：

```llvm
define i64 @f(i64 %x) {
  %s = lshr i64 %x, 8
  %a = and i64 %s, 71777214294589695
  %l = shl i64 %x, 8
  %b = and i64 %l, 18374966859414961920
  %o = or i64 %a, %b
  ret i64 %o
}
```

`-stop-after=regbankselect` 得到一棵多层树：

```yaml
%1:gpr(i64) = G_CONSTANT i64 8
%2:gpr(i64) = G_LSHR %0, %1(i64)
%3:gpr(i64) = G_CONSTANT i64 71777214294589695
%4:gpr(i64) = G_AND %2, %3(i64)
%5:gpr(i64) = G_SHL %0, %1(i64)
%6:gpr(i64) = G_CONSTANT i64 -71777214294589696
%7:gpr(i64) = G_AND %5, %6(i64)
%8:gpr(i64) = G_OR %4, %7(i64)
```

实跑汇编只有一条：

```asm
rev16   x0, x0
ret
```

`-stop-after=instruction-select`：

```yaml
%0:gpr64 = COPY $x0
%8:gpr64 = REV16Xr %0
$x0 = COPY %8
```

这条规则在 `AArch64InstrInfo.td` 里就是一棵显式的多层 pattern：

```tablegen
def : Pat<(or (and (srl GPR64:$Rn, (i64 8)), (i64 0x00ff00ff00ff00ff)),
              (and (shl GPR64:$Rn, (i64 8)), (i64 0xff00ff00ff00ff00))),
          (REV16Xr GPR64:$Rn)>;
```

按 pattern 前序深度优先，`MIs` 的槽位大致是：

| 槽位 | 指令 | 来源 |
| --- | --- | --- |
| `MIs[0]` | `G_OR` | 根 |
| `MIs[1]` | `G_AND`（左） | `G_OR` 操作数 1 |
| `MIs[2]` | `G_LSHR` | 左 `G_AND` 操作数 1 |
| `MIs[3]` | `G_CONSTANT 8` | `G_LSHR` 操作数 2 |
| `MIs[4]` | `G_CONSTANT mask1` | 左 `G_AND` 操作数 2 |
| `MIs[5]` | `G_AND`（右） | `G_OR` 操作数 2 |
| `MIs[6]` | `G_SHL` | 右 `G_AND` 操作数 1 |
| `MIs[7]` | `G_CONSTANT 8` | 右 `G_SHL` 操作数 2 |
| `MIs[8]` | `G_CONSTANT mask2` | 右 `G_AND` 操作数 2 |

两个教学点：共享的 `G_CONSTANT 8` 被两棵子树使用，生成器可能把它记录两次，也可能用 `GIM_CheckIsSameOperand` 复用；规则成功后，整棵树被一条 `REV16Xr` 替代，其他记录只提供操作数和检查。

> 槽位顺序是前序推导的示意；实际编号是生成器内部细节，本章不把它当成稳定 ABI。

---

## 9.5 记录到定义者，为什么不等于可以删除

`GIM_RecordInsn` 只把 `MachineInstr*` 放进 `MIs`。它不检查：这条指令有没有副作用、结果有没有别的使用者、和根是否同块、折叠后是否还需要存在。真正决定“能不能折”的是后续的 `GIM_CheckIsSafeToFold`，以及 pattern 自己带的 one-use 谓词。

`GIM_CheckIsSafeToFold` 的实现是：

```cpp
case GIM_CheckIsSafeToFold: {
  uint64_t NumInsn = MatchTable[CurrentIdx++];
  MachineInstr &Root = *State.MIs[0];
  for (unsigned K = 1, E = NumInsn + 1; K < E; ++K) {
    if (!isObviouslySafeToFold(*State.MIs[K], Root)) {
      if (handleReject() == RejectAndGiveUp)
        return false;
    }
  }
  break;
}
```

注意 `K = 1`：根不检查自己。`isObviouslySafeToFold` 的核心分支是：

```cpp
// Immediate neighbours are already folded.
if (MI.getParent() == IntoMI.getParent() &&
    std::next(MI.getIterator()) == IntoMI.getIterator())
  return true;
// Convergent instructions cannot be moved in the CFG.
if (MI.isConvergent() && MI.getParent() != IntoMI.getParent())
  return false;
if (MI.isLoadFoldBarrier())
  return false;
if (MI.mayLoad() && MI.getParent() == IntoMI.getParent()) {
  if (MI.memoperands_empty())
    return false;
  auto &MMO = **(MI.memoperands_begin());
  if (MMO.isAtomic() || MMO.isVolatile())
    return false;
  unsigned Iter = 0, MaxIter = 20;
  for (auto &CurrMI :
       instructionsWithoutDebug(MI.getIterator(), IntoMI.getIterator())) {
    if (CurrMI.isLoadFoldBarrier())
      return false;
    if (Iter++ == MaxIter)
      return false;
  }
  return true;
}
return !MI.mayLoad();
```

它检查的是“显然安全”，不是“完全安全”：相邻指令放行；跨块 convergent 不能移动；load fold barrier 不能跨；同块 load 必须有 memoperand、不能 atomic/volatile，中间最多 20 条非 debug 指令；跨块 load 直接返回 `false`；非 load 的纯计算可以跨块。它不回答“结果有没有别的 use”——那是 one-use 谓词的职责。

### 9.5.1 实跑：多 use 时定义者必须保留

把 `add i64 %a, 1` 的 MIR 改成常量有两个使用者：

```yaml
%1:gpr(i64) = G_CONSTANT i64 1
%2:gpr(i64) = G_ADD %0, %1
$x0 = COPY %2(i64)
$x1 = COPY %1(i64)
```

选择后：

```yaml
%0:gpr64sp = COPY $x0
%3:gpr32 = MOVi32imm 1
%1:gpr64all = SUBREG_TO_REG %3, $subreg.sub_32
%2:gpr64sp = ADDXri %0, 1, 0
$x0 = COPY %2
$x1 = COPY %1
```

`ADDXri` 仍然折进了立即数 1，但 `%1` 还被 `$x1` 使用，所以它必须留下，并被单独选成 `MOVi32imm` + `SUBREG_TO_REG`。**折叠和删除是两件事**：折叠是把 `%1` 的值编码进 `ADDXri` 的立即数字段；删除只发生在 `selectInstr` 入口的 `isTriviallyDead(MI, MRI)` 分支，而它要求没有副作用、所有 def 都死。执行器本身没有删除任何东西。

### 9.5.2 副作用、debug use 与跨块

- **副作用**：`G_STORE`、`G_INTRINSIC_W_SIDE_EFFECTS`、原子操作不能因为“结果没人用”就删。store 没有可记录的结果，通常不会成为被折叠的孩子。
- **debug use**：`GIM_CheckHasOneUse` 用 `hasOneNonDBGUse`，`GIM_CheckHasNoUse` 用 `use_nodbg_empty`。纯 debug use 不阻止折叠；如果指令最终被删，`salvageDebugInfo` 会尝试把 debug 信息转移到替代值上。
- **跨块**：纯计算可以跨块折，因为 SSA 定义支配使用；load 不允许跨块折，因为跨块会改变内存访问的执行条件；convergent 指令也不允许跨块。

---

## 9.6 One-use、No-use 与 `GIM_CheckCanReplaceReg`

### 9.6.1 `GIM_CheckHasOneUse` / `GIM_CheckHasNoUse`

源码：

```cpp
case GIM_CheckHasNoUse: {
  const MachineInstr *MI = State.MIs[InsnID];
  const Register Res = MI->getOperand(0).getReg();
  if (!MRI.use_nodbg_empty(Res)) { /* reject */ }
  break;
}
case GIM_CheckHasOneUse: {
  const MachineInstr *MI = State.MIs[InsnID];
  const Register Res = MI->getOperand(0).getReg();
  if (!MRI.hasOneNonDBGUse(Res)) { /* reject */ }
  break;
}
```

它们只检查**第一个结果**（`getOperand(0)`），并且都忽略 debug use。它们通常来自 `PatFrag` 的 `HasOneUse`/`HasNoUse` 属性；发射器在 `GlobalISelEmitter.cpp` 里把 `NoUsePredicateMatcher`/`OneUsePredicateMatcher` 变成这两个 `GIM_*`。`HasNoUse` 还有一个结构性用途：当目标指令没有 def、而源 pattern 有 def 时，发射器要求 pattern 带 `HasNoUse`，否则拒绝导入。

### 9.6.2 AArch64 的真实 one-use 行为

AArch64 的扩展寄存器折叠在 C++ 里也遵守 one-use。`isWorthFoldingIntoExtendedReg` 在 `hasOneNonDBGUse(DefReg)` 或函数 `hasOptSize()` 时返回 true，否则放弃折叠。

实跑 `sext` 有两个使用者：

```llvm
define i64 @f(i64 %a, i64 %c, i32 %b) {
  %e = sext i32 %b to i64
  %r1 = add i64 %a, %e
  %r2 = add i64 %c, %e
  %r = add i64 %r1, %r2
  ret i64 %r
}
```

输出：

```asm
mov     w8, w2
sxtw    x9, w8
add     x8, x0, x9
add     x9, x1, x9
add     x0, x8, x9
ret
```

`sxtw` 只出现一次，两个 `add` 都用 `ADDXrr`，没有把 `G_SEXT` 折进 `ADDXrx`。把两个 `add` 改成一个，输出会变成 `add x0, x0, w1, sxtw`。这不是 `GIM_CheckHasOneUse`，而是 ComplexPattern 回调里的启发式；但结论相同：**多 use 会让“把定义者拉进规则”变得不划算，甚至不合法。**

### 9.6.3 `GIM_CheckCanReplaceReg`

`GIM_CheckCanReplaceReg` 是给“想把一个已匹配操作数替换成另一个 vreg”的 matcher 用的。执行器调用 `canReplaceReg(Old, New, MRI)`：

```cpp
if (DstReg.isPhysical() || SrcReg.isPhysical())
  return false;
if (MRI.getType(DstReg) != MRI.getType(SrcReg))
  return false;
const auto &DstRBC = MRI.getRegClassOrRegBank(DstReg);
if (!DstRBC || DstRBC == MRI.getRegClassOrRegBank(SrcReg))
  return true;
return isa<const RegisterBank *>(DstRBC) && MRI.getRegClassOrNull(SrcReg) &&
       cast<const RegisterBank *>(DstRBC)->covers(
           *MRI.getRegClassOrNull(SrcReg));
```

它保证：两边都不是物理寄存器；LLT 完全相同；目的寄存器没有约束，或约束相同；目的只是一个 bank 时，源的具体 regclass 必须被这个 bank 覆盖。它不插入 COPY，也不改变 LLT；类型不同就直接拒绝。**不要把它当成类型转换工具。**

---

## 9.7 AArch64 上，嵌套到底发生在哪一层

AArch64 的 selector 不只用 TableGen 表。`shl`/`rev16` 属于表驱动：pattern 里直接出现 `(i64 imm...)` 或 `(i64 8)`，`MIs[1]` 等槽位由 matcher table 记录。

**地址模式**：

```llvm
define i64 @f(ptr %p) {
  %q = getelementptr i64, ptr %p, i64 3
  %r = load i64, ptr %q
  ret i64 %r
}
```

RBS：

```yaml
%1:gpr(i64) = G_CONSTANT i64 24
%2:gpr(p0) = G_PTR_ADD %0, %1(i64)
%3:gpr(i64) = G_LOAD %2(p0) :: (load (i64) from %ir.q)
```

实跑：

```asm
ldr     x0, [x0, #24]
ret
```

ISel：

```yaml
%0:gpr64sp = COPY $x0
%3:gpr64 = LDRXui %0, 3 :: (load (i64) from %ir.q)
```

看起来像“`G_LOAD` 把 `G_PTR_ADD` 和 `G_CONSTANT` 折进了同一条规则”。但在 AArch64 23.1.4 里，这条路径主要是 C++ 的 `selectLoad`/`selectAddrModeIndexed`，通过 ComplexPattern `am_indexed64` 暴露给 matcher。`GIM_RecordInsn` 不是这里的主角；地址模式回调用 `getDefIgnoringCopies` 和 `selectAddrModeIndexed` 完成同样的“看穿定义者”。

**被 Legalizer 提前折叠：`G_SEXT(G_LOAD)`**：`load i32` 后接 `sext` 在 RBS 里已经变成 `%2:gpr(i64) = G_SEXTLOAD %0(p0)`，实跑只有 `ldrsw x0, [x0]`。`G_SEXTLOAD` 是 Legalizer/combiner 形成的扩展 load，ISel 只需要匹配根。**分析“嵌套没命中”之前，先看 RBS MIR；Legalizer 可能已经把你想找的树改写了。**

**C++ ComplexPattern：`add` + `G_SEXT`**：RBS 是 `%2:gpr(i64) = G_SEXT %1(i32)`、`%3:gpr(i64) = G_ADD %0, %2`，实跑：

```asm
add     x0, x0, w1, sxtw
ret
```

`ADDXrx` 的模式使用 ComplexPattern `arith_extended_reg32to64`。`selectArithExtendedRegister` 先 `getDefIgnoringCopies`，再用 `isWorthFoldingIntoExtendedReg` 检查 one-use，最后看 `getExtendTypeForInst` 和 shift 是否不超过 4。这不是 `MIs[1]`，但仍然是“嵌套匹配”，只是发生在 target 的 C++ 回调里。

**不折叠：`add (load p), x`**：实跑是 `ldr x8, [x0]` 后接 `add x0, x8, x1`。AArch64 没有“从内存读一个操作数并做 ADD”的指令，所以 `G_LOAD` 不会被折进 `G_ADD`；这不是 matcher 的 bug，而是**根本没有这条规则**。

**定位表**：

| 现象 | 最可能发生的层 | 先看什么 |
| --- | --- | --- |
| `shl` 立即数没折 | TableGen 简单 pattern / COPY | RBS MIR 里是否有 COPY |
| `add` 立即数没折 | ComplexPattern `selectArithImmed` | 常量是否可编码、是否被 COPY 包住 |
| `load` 地址没折 | C++ `selectAddrModeIndexed` | 偏移范围、对齐、地址空间 |
| `sext` 没折进 `add` | C++ `selectArithExtendedRegister` | 是否 one-use、shift 是否 ≤ 4 |
| `G_SEXT` 不见了 | Legalizer | RBS 是否已是 `G_SEXTLOAD` |
| `add(load)` 没折 | 没有对应指令 | 目标 ISA 是否支持内存操作数 |

---

## 9.8 踩雷清单

1. **把“记录”当成“删除”。** `GIM_RecordInsn` 只填 `MIs`；删除是 `isTriviallyDead` 之后的独立步骤。多 use、副作用、debug use 都会改变结果。
2. **以为 `GIM_RecordInsn` 会看穿 COPY。** 它用 `getVRegDef`，看到的就是 COPY。只有 `GIM_RecordInsnIgnoreCopies` 或目标 C++ 回调才会继续走。
3. **以为 `getDefIgnoringCopies` 会看穿所有转换。** 它只跳过 COPY 和 pre-isel hint；`G_SEXT`、`G_ZEXT`、`G_SHL` 都是真实定义。
4. **把 `GIM_CheckIsSafeToFold` 当成“无副作用”检查。** 它检查的是“折进根是否显然安全”，不检查 use 数量；后者要靠 `HasOneUse`/`HasNoUse` 或 C++ 启发式。
5. **在 release 构建里依赖 assert。** `assert(NewInsnID != 0)` 只在 assertions 构建里生效；覆盖 `MIs[0]` 可能在 release 里静默产生错误匹配。

---

## 9.9 进阶：从“能折”到“该不该折”

- **代码体积**：把常量折进指令通常减少一条物化指令；把小计算折进消费者可能让同一段计算在多个消费者里重复。
- **调度与寄存器压力**：load 折叠会把内存访问移动到消费者附近；跨块 load 被禁止，因为这会改变执行条件。多 use 的定义者不会因折叠被删除，反而可能同时保留“物化版本”和“折叠版本”。
- **可测试性**：同时检查 `selected` 和最终指令；只检查汇编会漏掉“选择成功但 vreg 没约束”的情况。

---

## 9.10 动手练习

### 练习 9.1 制造 COPY 差异

分别用 `shl` 和 `add` 的 MIR，在常量与使用者之间插入 `%2:gpr(i64) = COPY %1(i64)`。预测哪一个仍然折叠，哪一个退化。跑完后用 `getDefIgnoringCopies` 的源码解释差异。

### 练习 9.2 拆掉 `rev16` 的一个常量

把 9.4 的 `rev16` IR 中第二个 mask 改成 `0xffffffffffffffff`，预测 pattern 是否还命中。跑 `llc` 验证，并解释是“谓词失败”还是“结构不匹配”。

### 练习 9.3 多 use 与 debug use

把 `add i64 %a, 1` 的常量再加一个使用者，观察 `ADDXri` 是否仍然折叠、常量是否保留。再给常量加一个 `llvm.dbg.value`，观察 debug use 是否改变结果。

---

## 9.11 重点整理

- `MIs[0]` 固定是根；`MIs[1..]` 是按生成表顺序记录的定义者；`assert(NewInsnID != 0)` 保护这个不变量。
- `GIM_RecordInsn` 用 `MRI.getVRegDef`，COPY 会阻断；`GIM_RecordInsnIgnoreCopies` 用 `getDefIgnoringCopies`，只跳过 COPY 和 pre-isel hint。
- 嵌套匹配可以发生在 TableGen 表、ComplexPattern 回调或 Legalizer；先定位层，再改代码。
- `GIM_CheckIsSafeToFold` 只检查 `MIs[1..N]`，且只做“显然安全”的判断；它不检查 use 数量。
- 记录不等于删除；多 use、副作用、debug use、跨块都会影响最终是否保留定义者。
- `GIM_CheckCanReplaceReg` 检查类型和寄存器约束，不插入 COPY。
- AArch64 的 `shl` 立即数、`rev16` 是表驱动的多层匹配；`add` 立即数、`load` 地址、`add+sext` 更多依赖 ComplexPattern/C++。

---

## 9.12 自我检核

1. 为什么 `MIs[0]` 不能通过 `GIM_RecordInsn` 覆盖？如果没有这条 assert，哪些快速路径会失效？
2. `GIM_RecordInsn` 和 `GIM_RecordInsnIgnoreCopies` 在遇到 `%2 = COPY %1` 时分别记录什么？
3. `getDefIgnoringCopies` 会不会看穿 `G_SEXT`？为什么？
4. `rev16` 的 `MIs` 树里为什么可能出现两个槽位指向同一条 `G_CONSTANT`？
5. `isObviouslySafeToFold` 对“同块 load”检查了哪些条件？为什么跨块 load 直接失败？
6. 多 use 的常量在 `ADDXri` 折叠后为什么还留在 MIR 里？
7. `GIM_CheckCanReplaceReg` 与 `constrainOperandRegClass` 的职责有什么不同？

---

## 9.13 延伸阅读

- **`llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`** — `GIM_RecordInsn`、`GIM_CheckIsSafeToFold`、`GIM_CheckHasOneUse`、`GIM_CheckCanReplaceReg` 的真实实现。先读 178–221 行和 984–1040 行。
- **`llvm/lib/CodeGen/GlobalISel/GIMatchTableExecutor.cpp`** — `isObviouslySafeToFold` 的完整逻辑。重点看“相邻指令直接放行”和“同块 load 最多 20 条指令”。
- **`llvm/lib/CodeGen/GlobalISel/Utils.cpp`** — `getDefIgnoringCopies`/`getSrcRegIgnoringCopies`；注意它不跳过扩展指令。
- **`llvm/lib/Target/AArch64/AArch64InstructionSelector.cpp` 与 `AArch64InstrInfo.td`** — 前者展示 `selectArithExtendedRegister`、`selectLoad`、`selectAddrModeIndexed` 的“表外嵌套”；后者给出 `shl` 立即数、`rev16` 多层 pattern 的声明。
- **官方文档《InstructionSelect》**：<https://llvm.org/docs/GlobalISel/InstructionSelect.html> — 先看 “SelectionDAG Rule Imports” 和 “ComplexPattern” 两节。

---

## 下一章

[第 10 章 失败路径与 fallback](./10-failure-and-fallback.md) —— 选择失败时，为什么 MIR 里会同时出现 `selected: true` 和 `failedISel: true`？MachineVerifier 为什么放过了仍然含 `G_*` 的函数？`-global-isel-abort=1` 到底改变了什么？下一章用真实输出拆开这条路径。
