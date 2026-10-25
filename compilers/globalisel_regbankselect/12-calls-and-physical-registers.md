# Ch 12 — 调用、返回与物理寄存器

> **目标**：把调用边界上的 bank 责任划清楚。学完你能回答：`G_CALL` 在 23.1.4 里到底存不存在；CallLowering 已经固定了哪些东西；RegBankSelect 还能改什么；为什么参数/返回值的物理寄存器 COPY 往往跨 bank 却不产生额外 repair；`G_INTRINSIC` 的 operand 又该如何映射。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（实测 MIR）；AArch64 主 target，RISC-V 对照。所有调用 MIR 来自 `call.ll` / `call-bc.ll` 的 `-stop-after=legalizer` 与 `-stop-after=regbankselect` 输出。
>
> **证据约定**：本章先纠正一个常见命名误解——23.1.4 的 `GenericOpcodes.td` 里没有 `G_CALL`。标“实测”的 MIR 是官方包输出；`G_CALL` 的历史/其他 target 情况只做源码层说明，不伪造 MIR。

## 为什么需要这个？

调用是函数里最不像“普通指令”的地方：

- 参数放哪个寄存器由 calling convention 决定，不由 bank 决定。
- 返回值通过物理寄存器传回，`BL` 的 implicit-def 列表里写着 `$s0` / `$x0`。
- 调用指令本身通常是 target-specific 的 `BL` / `PseudoCALL`，RegBankSelect 根本不会映射它。
- 真正被 RegBankSelect 处理的是调用前后的 `COPY`，以及 `G_INTRINSIC` 这类仍留在 generic 层的指令。

如果不把责任划清，就会出现两类错误：

1. 在 23.1.4 的 MIR 里找不到 `G_CALL`，以为 pipeline 少跑了一步。
2. 看到 `$w0 = COPY %1:fpr` 这样的跨 bank COPY，以为 RegBankSelect 选错了参数 bank，实际上这是 ABI 边界本来的形态。

这一章要把“CallLowering 已固定”与“RegBankSelect 仍可决定”分成两张清单。

## 先建立直觉

把调用想成寄快递：

- **CallLowering** 是打包员：它按 ABI 把参数放进指定的箱子（`w0`、`x0`、`s0`、`d0`），贴上运单，并生成“取回执”的 COPY。
- **BL / PseudoCALL** 是货车：它只负责出发，不关心箱子里是什么 bank。
- **RegBankSelect** 是仓库里给每个虚拟包裹贴“GPR/FPR”标签的人。它不能换箱子，只能保证送到打包台之前的标签正确。
- **InstructionSelect** 是最终装车的人：它把跨 bank 的 COPY 变成 `FMOV`、`MOV` 等真实指令。

```text
vreg 计算 (fpr) ──COPY──► $s0 (FPR) ──BL──► 被调函数
                      ▲
                      │ CallLowering 已固定 ABI
RegBankSelect 只给 COPY 的 def/use 贴 bank
```

所以调用边界的关键不是“选哪个 bank”，而是“哪些 COPY 是 ABI 固定的，哪些 vreg 还需要 RegBankSelect 贴标签”。

## 核心概念

### 1. 23.1.4 没有 `G_CALL`

在 23.1.4 的 `llvm/include/llvm/Target/GenericOpcodes.td`（以及源码树里的对应文件）中，generic 调用 opcode 并不存在。AArch64 的 `AArch64CallLowering::lowerCall` 直接构造 target 指令：

```cpp
unsigned Opc = getCallOpcode(MF, Info.Callee.isReg(), false, Info.PAI, MRI);
auto MIB = MIRBuilder.buildInstrNoInsert(Opc);
...
MIRBuilder.insertInstr(MIB);
```

对直接调用，`Opc` 是 `AArch64::BL`；对间接调用是 `BLR` 一类。RISC-V 同样直接构造 `RISCV::PseudoCALL` / `PseudoCALLIndirect`。

因此：

- `-stop-after=legalizer` 的 MIR 里已经是 `BL @ext_f32`，不是 `G_CALL`。
- RegBankSelect 遍历时看到 `BL`，会命中“跳过 target-specific 非 pre-isel 指令”的分支。
- 你在旧文章或旧 LLVM 版本里看到的 `G_CALL`，不能拿来解释 23.1.4 的输出。

### 2. CallLowering 已经固定了什么

AArch64 的 `OutgoingArgHandler::assignValueToReg` 做两件事：

```cpp
void assignValueToReg(Register ValVReg, Register PhysReg,
                      const CCValAssign &VA, ISD::ArgFlagsTy Flags) override {
  MIB.addUse(PhysReg, RegState::Implicit);
  Register ExtReg = extendRegister(ValVReg, VA);
  MIRBuilder.buildCopy(PhysReg, ExtReg);
}
```

- 选物理寄存器：`PhysReg` 来自 CC assigner，RegBankSelect 无权更改。
- 加 implicit use：让 `BL` 的隐式操作数列表包含这个物理寄存器。
- 生成 `COPY PhysReg, vreg`：把值搬到 ABI 位置。

返回侧同理：`lowerReturn` 构造 `RET_ReallyLR`，并把返回值 COPY 到 `$s0` / `$x0`；调用侧用 `%vreg = COPY $s0` 取回。

所以下面这些是**已经固定**的：

| 项目 | 谁决定 | RegBankSelect 能否改 |
| --- | --- | --- |
| 参数放 `w0`/`x0`/`s0`/`d0` | calling convention | 不能 |
| 返回值从哪个物理寄存器取 | calling convention | 不能 |
| `BL` 的 implicit use/def 列表 | CallLowering | 不能（整条跳过） |
| 栈参数地址 | CallLowering / frame layout | 不能 |
| `COPY` 两端 vreg 的 bank | RegBankSelect | 能（但 copy-like 只映射 def） |

### 3. 物理寄存器的 bank 从哪来

`RegisterBankInfo::getRegBank` 对物理寄存器走：

```cpp
if (!Reg.isVirtual()) {
  const TargetRegisterClass *RC = getMinimalPhysRegClass(Reg, TRI);
  return RC ? &getRegBankFromRegClass(*RC, LLT()) : nullptr;
}
```

也就是说，`$w0` 通过它的最小寄存器类落到 GPR bank，`$s0` 落到 FPR bank，`$x0` 落到 GPR bank。这个映射是“寄存器类 → bank”的静态结果，不需要 RegBankSelect 做选择。

### 4. `COPY` 是 copy-like，ABI COPY 也不例外

`COPY` 和 `PHI` 一样属于 copy-like。`getInstrMappingImpl` 只映射 def；如果 def 是物理寄存器，就用物理寄存器的最小类决定 bank；如果 def 是 vreg，就用源 bank 或约束推导。

这意味着：`$w0 = COPY %1:fpr(i32)` 这条 ABI COPY 不会被 RegBankSelect 拆成“先 GPR copy 再赋值”。它只是把 `$w0` 标成 GPR，把 `%1` 保持 FPR；跨 bank 的事实留给 InstructionSelect 的 `selectCopy` 去选 `FMOV`。

### 5. `G_INTRINSIC` 的 operand 布局

`G_INTRINSIC` 的定义是：

```tablegen
def G_INTRINSIC : GenericInstruction {
  let OutOperandList = (outs);
  let InOperandList = (ins unknown:$intrin, variable_ops);
  let hasSideEffects = false;
}
```

打印出来是这样：

```text
%1:gpr(i32) = G_INTRINSIC intrinsic(@llvm.aarch64.neon.fcvtns), %0(f32)
```

operand 0 是结果 vreg，operand 1 是 intrinsic ID（立即数，不是寄存器），operand 2 才是第一个源操作数。AArch64 的 `getInstrMapping` 对 NEON 转换 intrinsic 有专门分支，会把源操作数放到 FPR、结果按类型放到 GPR 或 FPR。

## 底层机制

### RegBankSelect 如何跳过调用指令

`assignRegisterBanks` 的主循环：

```cpp
if (isTargetSpecificOpcode(MI.getOpcode()) && !MI.isPreISelOpcode())
  continue;
if (MI.isInlineAsm())
  continue;
if (MI.isImplicitDef())
  continue;
if (!assignInstr(MI)) { ... }
```

`BL`、`BLR`、`ADJCALLSTACKDOWN`、`ADJCALLSTACKUP`、`RET_ReallyLR` 都是 target-specific 且不是 pre-isel generic，所以整条跳过。它们的操作数不会进入 `computeMapping`，也不会产生 repair。

这也解释了为什么你不需要给 `BL` 写 `RegisterBankInfo` mapping：它已经在 target 层用物理寄存器和 regclass 约束好了。

### 间接调用的 callee 寄存器

间接调用会构造 `BLR %callee`，然后：

```cpp
if (MIB->getOperand(0).isReg())
  constrainOperandRegClass(MF, *TRI, MRI, *MF.getSubtarget().getInstrInfo(),
                           *MF.getSubtarget().getRegBankInfo(), *MIB, MIB->getDesc(),
                           MIB->getOperand(0), 0);
```

callee 的 vreg 在 CallLowering 阶段就被约束到 GPR regclass。RegBankSelect 跳过整条 `BLR`，所以它不会重新决定 callee 的 bank。这个约束是“已经发生的事实”，不是 repair。

### `G_INTRINSIC` 的默认映射与特殊映射

AArch64 的 `getInstrMapping` 先做一个通用猜测：

- 向量 → FPR
- 浮点 opcode，或只定义/只使用 FP 的指令 → FPR
- 其他标量 → GPR
- 大于 64 位的标量 → FPR

然后对 `G_INTRINSIC` / `G_INTRINSIC_W_SIDE_EFFECTS` 按 intrinsic ID 微调。例如：

```cpp
case Intrinsic::aarch64_neon_fcvtns:
...
  OpRegBankIdx[2] = PMI_FirstFPR;
  if (MRI.getType(MI.getOperand(0).getReg()).isVector()) {
    OpRegBankIdx[0] = PMI_FirstFPR;
    break;
  }
  TypeSize DstSize = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  TypeSize SrcSize = getSizeInBits(MI.getOperand(2).getReg(), MRI, TRI);
  if (DstSize == 16 || ...)
    OpRegBankIdx[0] = PMI_FirstFPR;
  else
    OpRegBankIdx[0] = PMI_FirstGPR;
```

索引 2 而不是 1，正是因为 operand 1 是 intrinsic ID。

### 返回值与 i128

i128 返回是一个很好的反例：AArch64 的 FPR bank 覆盖 128 位，但 ABI 仍然用 `x0`/`x1` 两个 GPR 返回。

实测 MIR：

```text
bb.1 (%ir-block.0):
  liveins: $x0, $x1
  %1:gpr(i64) = COPY $x0
  %2:gpr(i64) = COPY $x1
  ADJCALLSTACKDOWN 0, 0, implicit-def $sp, implicit $sp
  $x0 = COPY %1(i64)
  $x1 = COPY %2(i64)
  BL @ext_i128, csr_aarch64_aapcs, implicit-def $lr, implicit $sp,
     implicit $x0, implicit $x1, implicit-def $x0, implicit-def $x1
  ADJCALLSTACKUP 0, 0, implicit-def $sp, implicit $sp
  %6:gpr(i64) = COPY $x0
  %7:gpr(i64) = COPY $x1
  $x0 = COPY %6(i64)
  $x1 = COPY %7(i64)
```

注意：

- `BL` 的 implicit-def 是 `$x0`、`$x1`，不是 `$q0`。
- 取回值是两条 `COPY $x0` / `COPY $x1`，都是 GPR。
- 128 位的“bank 覆盖能力”不等于“ABI 选择”。bank 只说明 FPR 能装 128 位，ABI 说这个值必须走两个 GPR。

## 例子一：float 参数与返回（实测）

输入：

```llvm
declare float @ext_f32(float, i32)

define float @call_f32(i32 %a, float %b) {
  %r = call float @ext_f32(float %b, i32 %a)
  ret float %r
}
```

`-stop-after=regbankselect` 的输出：

```text
bb.1 (%ir-block.0):
  liveins: $s0, $w0

  %0:gpr(i32) = COPY $w0
  %1:fpr(f32) = COPY $s0
  ADJCALLSTACKDOWN 0, 0, implicit-def $sp, implicit $sp
  $s0 = COPY %1(f32)
  $w0 = COPY %0(i32)
  BL @ext_f32, csr_aarch64_aapcs, implicit-def $lr, implicit $sp,
     implicit $s0, implicit $w0, implicit-def $s0
  ADJCALLSTACKUP 0, 0, implicit-def $sp, implicit $sp
  %2:fpr(f32) = COPY $s0
  $s0 = COPY %2(f32)
  RET_ReallyLR implicit $s0
```

逐条读：

1. `%0:gpr(i32) = COPY $w0`：形参 `%a` 从 `w0` 取回，物理寄存器给 def 定 bank。
2. `%1:fpr(f32) = COPY $s0`：形参 `%b` 从 `s0` 取回，FPR。
3. `$s0 = COPY %1` / `$w0 = COPY %0`：按调用约定把实参放进物理寄存器。这里两边 bank 恰好一致，没有额外的跨 bank 复制。
4. `BL` 的 implicit use 列表里有 `$s0`、`$w0`，implicit-def 里有 `$s0`；这些是 ABI 信息，不是 RegBankSelect 生成的。
5. `%2:fpr = COPY $s0`：取回 float 返回值。
6. `$s0 = COPY %2`：为 `RET_ReallyLR` 准备返回寄存器。

这条链上，RegBankSelect 只做了“给 `%0`、`%1`、`%2` 贴 GPR/FPR 标签”。参数的物理位置、implicit 列表、返回寄存器都不是它决定的。

## 例子二：ABI COPY 本身就是跨 bank（实测）

输入：

```llvm
declare void @ext_i(i32)

define void @call_bitcast(float %f) {
  %i = bitcast float %f to i32
  call void @ext_i(i32 %i)
  ret void
}
```

`-stop-after=regbankselect` 的输出：

```text
bb.1 (%ir-block.0):
  %0:fpr(f32) = COPY $s0
  %1:fpr(i32) = G_BITCAST %0(f32)
  ADJCALLSTACKDOWN 0, 0, implicit-def $sp, implicit $sp
  $w0 = COPY %1(i32)
  BL @ext_i, csr_aarch64_aapcs, implicit-def $lr, implicit $sp, implicit $w0
  ADJCALLSTACKUP 0, 0, implicit-def $sp, implicit $sp
```

关键点：

- Fast 模式下 `G_BITCAST` 复用源 bank，`%1` 是 `fpr(i32)`。
- `$w0 = COPY %1` 的 def 是 GPR、use 是 FPR，这是一条跨 bank COPY。
- RegBankSelect 没有在它前面再插一条 `%tmp:gpr = COPY %1`；因为 `COPY` 是 copy-like，只映射 def，跨 bank 的事实留给 selector。
- 最终 selector 会把这条 COPY 选成 `FMOV W0, S0` 一类的指令。

所以“ABI COPY 跨 bank”是正常形态，不是 repair 失败。判断一条 COPY 的来源，要看它是不是 CallLowering 在参数/返回位置生成的，而不是看它跨不跨 bank。

## 例子三：`G_INTRINSIC` 与 RISC-V 的调用边界（实测）

### `G_INTRINSIC` 的 operand 索引

输入：

```llvm
define i32 @call_round(float %x) {
  %r = tail call i32 @llvm.aarch64.neon.fcvtns.i32.f32(float %x)
  ret i32 %r
}
```

`-stop-after=regbankselect` 的输出：

```text
bb.1 (%ir-block.0):
  liveins: $s0
  %0:fpr(f32) = COPY $s0
  %1:gpr(i32) = G_INTRINSIC intrinsic(@llvm.aarch64.neon.fcvtns), %0(f32)
  $w0 = COPY %1(i32)
  RET_ReallyLR implicit $w0
```

解释：

- `%1` 是结果，bank 为 GPR，因为 `fcvtns` 返回 i32，且没有“结果尺寸等于源尺寸且所有 use 都偏好 FP”的条件。
- `%0` 是源，bank 为 FPR。
- `G_INTRINSIC` 没有被 lower 成 target 指令，所以它会被 RegBankSelect 映射；这正是 `G_INTRINSIC` 与 `BL` 的分界线。
- 如果源/结果尺寸相同且 use 偏好 FP，AArch64 可能把结果也放 FPR；这不是通用规则，要看 `getInstrMapping` 的具体分支。

### RISC-V 的对照

同一份 `call.ll` 在 `riscv64-linux-gnu` 上：

```text
bb.1 (%ir-block.0):
  %2:_(s64) = COPY $x10
  %3:_(s64) = COPY $x11
  ADJCALLSTACKDOWN 0, 0, implicit-def $x2, implicit $x2
  $x10 = COPY %3(s64)
  $x11 = COPY %2(s64)
  PseudoCALL target-flags(riscv-call) @ext_f32, csr_ilp32_lp64,
     implicit-def $x1, implicit $x10, implicit $x11, implicit-def $x10
  ADJCALLSTACKUP 0, 0, implicit-def $x2, implicit $x2
  %7:_(s64) = COPY $x10
  $x10 = COPY %7(s64)
```

RISC-V 也在 CallLowering 阶段直接生成 `PseudoCALL`，并且参数走 `x10`/`x11`（该 triple 下 float 也按整数寄存器传递）。RegBankSelect 同样跳过 `PseudoCALL` 和 `ADJCALLSTACK*`，只处理前后的 COPY。不同 target 的差异在 ABI 和 target opcode，不在 RegBankSelect 的算法。

## 对比与取舍

| 设计点 | CallLowering 负责 | RegBankSelect 负责 | InstructionSelect 负责 |
| --- | --- | --- | --- |
| 参数物理寄存器 | 是 | 否 | 否 |
| implicit use/def | 是 | 否（整条跳过） | 消费 |
| 参数/返回 COPY 的 def bank | 提供物理寄存器 | 是（copy-like 只映射 def） | 选真实 copy 指令 |
| 跨 bank ABI COPY | 生成 COPY | 保留跨 bank 事实 | 选 FMOV/MOV |
| `G_INTRINSIC` 的 bank | 否 | 是 | 选 target 指令 |
| 间接 callee 的 regclass | 是（constrain） | 否（跳过 BLR） | 消费 |

取舍很清楚：把 ABI 固定在 CallLowering，可以让 RegBankSelect 不必理解 calling convention；代价是调用边界的 COPY 可能天然跨 bank，selector 必须能处理。

## 踩雷集锦

1. **在 23.1.4 里找 `G_CALL`**  
   找不到是正常的。AArch64 生成 `BL`/`BLR`，RISC-V 生成 `PseudoCALL`。看到 `-stop-after=legalizer` 里已经是 `BL`，说明 CallLowering 已经在 IRTranslator 阶段完成了调用 lowering，不是 pipeline 缺阶段。

2. **以为 RegBankSelect 能把参数从 GPR 改到 FPR**  
   不能。参数位置由 calling convention 决定，`$w0`/`$s0` 是 CallLowering 选好的。RegBankSelect 只能给 vreg 贴 bank，然后让 COPY 跨 bank。

3. **把 ABI COPY 当成 repair COPY**  
   `$w0 = COPY %1:fpr` 可能来自 CallLowering 的参数搬运，不是 RegBankSelect 为了修复 bank 插入的。区分方法：看它连接的是不是物理寄存器、是否在调用前后、是否由 `buildCopy(PhysReg, ...)` 生成。

4. **忽略 implicit-def，直接看 BL 的显式 operand**  
   `BL` 的显式 operand 是 callee、栈调整立即数、regmask 等；返回值在 implicit-def 列表里。真正的 bank 信息在 `%2 = COPY $s0` 这样的 COPY 上，不在 `BL` 的文本里。

5. **因为 FPR 覆盖 128 位就以为 i128 返回走 FPR**  
   ABI 用 `x0`/`x1`。bank 的 coverage 是“能装下”，calling convention 是“必须放哪”。两者不是一回事。

## 进阶

1. **间接调用与 callee 约束**  
   构造一个函数指针调用，观察 `BLR %callee` 和 `constrainOperandRegClass`。RegBankSelect 跳过 BLR，但 callee 的 regclass 已经在 CallLowering 阶段固定。

2. **可变参数与栈参数**  
   加一个 `...` 调用，观察 `ADJCALLSTACKDOWN`、栈参数地址和 `G_PTR_ADD`。栈参数的值仍然要先经过 COPY 到内存，bank 由数据宽度决定。

3. **`G_INTRINSIC_W_SIDE_EFFECTS`**  
   找一条有 side effect 的 AArch64 NEON intrinsic，观察它与 `G_INTRINSIC` 的 mapping 差异。AArch64 对两者共用一部分 intrinsic ID 分支。

## 动手练习

1. 对 `call_f32` 逐条标注：哪条 COPY 是 CallLowering 生成的，哪条是 RegBankSelect 贴 bank 的，哪条是 selector 会变成 FMOV 的。
2. 把 `call_bitcast` 的 `bitcast` 换成 `fptosi`，重新跑 `-stop-after=regbankselect`，预测 `%i` 的 bank 和 `$w0 = COPY` 是否跨 bank。
3. 写一个间接调用 `%fp`，用 `-stop-after=regbankselect` 观察 `BLR` 的 callee vreg 是否已经被约束到 GPR。
4. 把 `call_i128` 的返回类型改成 `<2 x i64>`，比较 ABI 位置和 bank：`x0/x1` 还是 `q0`？
5. 在 RISC-V 上跑同一个 `call.ll`，比较 `PseudoCALL` 与 AArch64 `BL` 的 implicit 列表。

## 本章重点整理

- 23.1.4 没有 `G_CALL`；AArch64 用 `BL`/`BLR`，RISC-V 用 `PseudoCALL`，都在 CallLowering 阶段生成。
- RegBankSelect 跳过 target-specific 的调用指令和栈调整指令；它处理的是调用前后的 `COPY` 与 `G_INTRINSIC`。
- 物理寄存器的 bank 来自最小寄存器类；ABI 位置由 calling convention 固定，RegBankSelect 无权更改。
- `COPY` 是 copy-like，只映射 def；ABI COPY 可以天然跨 bank，selector 负责选 FMOV/MOV。
- i128 返回走 `x0`/`x1`，不因为 FPR 覆盖 128 位而改变。

## 自我检核

- [ ] 能解释为什么 23.1.4 的 legalized MIR 里是 `BL` 而不是 `G_CALL`
- [ ] 能列出至少三项 CallLowering 已经固定、RegBankSelect 不能改的东西
- [ ] 能说明 `$w0 = COPY %1:fpr` 为什么不会触发额外的 repair COPY
- [ ] 能读出 `G_INTRINSIC` 的 operand 1 是 intrinsic ID，而不是源操作数
- [ ] 能解释 i128 返回为什么是 GPR pair 而不是 FPR

## 延伸阅读

### 源码

- **[CallLowering.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/GlobalISel/CallLowering.h)**
  - **读哪里**：`lowerCall`、`lowerReturn`、`OutgoingValueHandler` 的接口。
  - **学什么**：参数/返回值如何从 IR 值变成物理寄存器 COPY。
  - **和本章的关联**：这是“CallLowering 已固定”的总入口。

- **[AArch64CallLowering.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64CallLowering.cpp)**
  - **读哪里**：`OutgoingArgHandler::assignValueToReg`、`lowerReturn`、`lowerCall` 里的 `buildInstrNoInsert(Opc)`。
  - **学什么**：`BL` 如何被构造，implicit use/def 如何添加，参数 COPY 插在哪。
  - **和本章的关联**：例子一的每条 COPY 都能在这里找到来源。

- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`assignRegisterBanks` 里的 target-specific / inline asm / implicit def 跳过逻辑。
  - **学什么**：为什么 `BL`、`ADJCALLSTACKDOWN`、`RET_ReallyLR` 不参与 mapping。
  - **和本章的关联**：这是“RegBankSelect 看不到调用指令”的直接依据。

- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：`G_INTRINSIC` / `G_INTRINSIC_W_SIDE_EFFECTS` 的 intrinsic ID 分支。
  - **学什么**：operand 2 为什么是第一个源操作数，结果何时放 FPR。
  - **和本章的关联**：例子三的 `fcvtns` mapping 就来自这里。

### 文档

- **[LLVM GlobalISel — Core Pipeline](https://llvm.org/docs/GlobalISel/Pipeline.html)**
  - **读哪里**：IRTranslator 与 call lowering 的关系。
  - **学什么**：为什么调用 lowering 不是独立 pass，而是在 IRTranslator 里完成。
  - **和本章的关联**：理解 `G_CALL` 为什么不存在。

下一章进入内存：地址永远走 GPR，数据可能走 GPR 或 FPR；对齐和偏移不改变 bank，但会改变 selector 能折叠出什么寻址模式。

→ [Ch 13 内存操作与地址 bank](./13-memory-and-addressing.md)