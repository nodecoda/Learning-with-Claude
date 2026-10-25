# Ch 11 — PHI、循环与跨块 bank 一致性

> **目标**：搞清楚 RegBankSelect 在 CFG 汇合点到底做了什么。学完你能解释：为什么 `G_PHI` 的 def 有 bank、incoming operand 却可以各自不同；为什么循环里的 PHI 通常很稳定；跨块使用时 COPY 插在定义块还是使用块；以及临界边（critical edge）到底在什么条件下才会被拆开。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（实测 MIR）；AArch64 主 target。临界边拆分路径以 23.1.4 源码为依据，未在官方包上构造出触发用例，文中明确标注。
>
> **证据约定**：标“实测”的 MIR 都来自本章 `phi.ll` / `phi-cross.ll` 在官方 23.1.4 `llc` 上的输出；标“源码依据”的结论来自 `RegBankSelect.cpp`、`RegisterBankInfo.cpp` 与 AArch64 selector 源码；不伪造 split 后的 MIR。

## 为什么需要这个？

前面的章节都在看直线代码：一条指令、几个操作数、插几条 COPY。真实函数几乎一定有分支和循环，而 PHI 是 SSA 与 CFG 的交界处。这里最容易出现三种误判：

- 看到 `G_PHI` 的两个 incoming 一个在 `gpr`、一个在 `fpr`，就认为 MIR 坏了。
- 以为 RegBankSelect 会在每个前驱块末尾插一条 COPY，把 incoming 统一成 PHI 的 bank。
- 看到 PHI 后面有一条跨 bank COPY，就把它当成“PHI 选错 bank”，而它可能只是 PHI 结果被某个 use 拉到了另一个 bank。

这三种误判都会把调试方向带偏。PHI 的特殊之处在于：它在语义上是“每条入边各复制一次”，但在 RegBankSelect 的数据模型里，`G_PHI` 被当成 **copy-like** 指令，默认只映射定义，不映射 incoming operand。把这一点记住，后面的现象都能解释。

## 先建立直觉

把 PHI 想成一个多入口的汇合大厅：

```text
        bb.entry                 bb.other
      %a:gpr(i32)             %b:fpr(i32)
           │                       │
           └──────────┬────────────┘
                      ▼
                 bb.join
        %p:gpr(i32) = G_PHI %a, %bb.entry, %b, %bb.other
                      │
                      ▼
              %u:fpr(i32) = COPY %p
              G_FADD %u, ...
```

大厅只规定“从这里出去的人拿哪种票”（PHI 的 def bank），不负责把每个入口的人先换衣服。入口的人穿什么（incoming 的 bank），由入口自己决定；真要统一服装，是在进入大厅的最后一段路（ISel 的 PHI fixup）或大厅里面（PHI 之后的 COPY）完成。

这带来两个直接推论：

1. **PHI 的 def bank 是局部选择**：它通常来自第一个已经带 bank 的 incoming，或者来自 PHI 被使用时需要的 bank。
2. **incoming 的 bank 不必等于 def bank**：RegBankSelect 输出里允许这种“不整齐”，MachineVerifier 也不会因此报错；AArch64 会在 InstructionSelect 阶段补 COPY。

## 核心概念

### 1. PHI 的 def 与 use 是两个独立的映射问题

`G_PHI` 的 operand 0 是定义，后面是成对的 `(value, predecessor)`。RegBankSelect 只关心 operand 0 的 bank；operand 1、3、5… 是“入边上的值”，它们各自的 bank 已经在定义它们的地方决定了。

所以下面这段是合法且常见的：

```text
%9:gpr(f32) = G_PHI %8(f32), %bb.2, %7(f32), %bb.3
```

`%8` 的 bank 是 `gpr`，`%7` 的 bank 是 `fpr`，而 PHI 的 def 是 `gpr`。这里没有“隐式转换”，只是把“入边复制”留到后面。

### 2. copy-like：只映射 def

`RegisterBankInfo.cpp` 里的 `isCopyLike` 明确把 `COPY`、`PHI`、`REG_SEQUENCE` 归为一类：

```cpp
static bool isCopyLike(const MachineInstr &MI) {
  return MI.isCopy() || MI.isPHI() ||
         MI.getOpcode() == TargetOpcode::REG_SEQUENCE;
}
```

对 copy-like 指令，`getInstrMappingImpl` 把 `NumOperandsForMapping` 设为 1：

```cpp
unsigned NumOperandsForMapping = IsCopyLike ? 1 : MI.getNumOperands();
```

它仍然会扫描所有 operand，用第一个能确定的 bank 来构造 def 的 `ValueMapping`，也会用 `cannotCopy` 检查明显不可能的复制；但最终返回的 `InstructionMapping` 只描述 operand 0。`computeMapping` 随后只遍历这一个 operand，因此不会为 incoming 逐个计算 repair。

这就是“PHI 两边 bank 不一致却没人报错”的根源。

### 3. 循环里的 RPO 顺序

RegBankSelect 按 `ReversePostOrderTraversal` 遍历基本块：

```cpp
ReversePostOrderTraversal<MachineFunction*> RPOT(&MF);
for (MachineBasicBlock *MBB : RPOT) {
  ...
}
```

在循环里，header 通常先于 backedge 所在块被访问。访问到 `G_PHI` 时，来自 entry 的 incoming 已经有 bank，来自 backededge 的 incoming 可能还没有。copy-like 的默认逻辑会用“第一个能确定的 bank”作为 PHI def 的 bank；backedge 上的值等到它自己的定义被访问时才确定。

这解释了一个常见现象：循环 PHI 的 bank 往往由“循环外第一次进入”的那个值决定，而不是由循环体内最热的那条路径决定。Greedy 模式会重新计算 alternative mapping 和 repair 代价，但它仍然是在 PHI 被访问的那一刻做局部决定，不会做全局循环分析。

### 4. 跨块 repair 是局部的

RegBankSelect 的 repair 是 local repairing：它不在定义块里提前插 COPY，而是在**使用点附近**插。对普通指令，插入点是“指令之前”（use）或“指令之后”（def）。因此：

- 值在 `bb.A` 定义、在 `bb.B` 使用，bank 不匹配时，COPY 通常插在 `bb.B` 的使用指令之前。
- 这会增加 `bb.B` 的寄存器压力，而不是 `bb.A` 的。
- PHI 是例外之一：它的 def 修复要插在“最后一个 PHI 之后”，incoming 的修复则涉及前驱块或临界边。

## 底层机制

### `RepairingPlacement` 如何为 PHI 选插入点

`RegBankSelect.cpp` 的 `RepairingPlacement` 构造函数先判断操作数是 def 还是 use：

```cpp
bool Before = !MO.isDef();
...
if (MI.isPHI()) {
  if (!Before) {
    MachineBasicBlock::iterator It = MI.getParent()->getFirstNonPHI();
    if (It != MI.getParent()->end())
      addInsertPoint(*It, /*Before*/ true);
    else
      addInsertPoint(*(--It), /*Before*/ false);
    return;
  }
  MachineBasicBlock &Pred = *MI.getOperand(OpIdx + 1).getMBB();
  ...
}
```

- **PHI 的 def 需要修复**：插在最后一个 PHI 之后，避免把普通指令插进 PHI 串。
- **PHI 的 incoming 需要修复**：先看前驱块能否在 terminator 之前插入。它从 `getLastNonDebugInstr()` 开始向前走，跳过 terminator；如果某个 terminator `modifiesRegister(Reg)`，就无法把 COPY 提到 terminator 前面，只能拆边。

但请注意：AArch64 的默认 `G_PHI` mapping 只有 1 个 operand，所以普通 PHI 的 incoming 根本不会进入这个 repair 分支。这段代码是通用机制，主要服务于 target 自定义 mapping、terminator 的 def，以及其他 copy-like 场景。不要因为读到了 `split` 代码，就以为每个 PHI 都会拆临界边。

### 临界边与 5% bias

当确实需要拆边时，`InsertPoint::insert` 会调用：

```cpp
MachineBasicBlock *NewBB = Src.SplitCriticalEdge(DstOrSplit, P);
```

拆边有额外代价。`computeMapping` 给 split 的插入点加了一个 5% 的 bias：

```cpp
const uint64_t PercentageForBias = 5;
uint64_t Bias = (RepairCost * PercentageForBias + 99) / 100;
...
CostForInsertPt += Bias;
uint64_t PtCost = InsertPt->frequency(*this) * CostForInsertPt;
```

直觉是：能在前驱块里插就不要拆边；拆边会新建基本块、改变 CFG，还会影响后续优化。Greedy 在比较 mapping 时会把这份非局部代价算进去。

### AArch64 的 PHI 善后：`fixupPHIOpBanks`

真正把 incoming 统一到 PHI def bank 的，是 AArch64 InstructionSelect：

```cpp
static void fixupPHIOpBanks(MachineInstr &MI, MachineRegisterInfo &MRI,
                            const AArch64RegisterBankInfo &RBI) {
  Register DstReg = MI.getOperand(0).getReg();
  const RegisterBank *DstRB = MRI.getRegBankOrNull(DstReg);
  ...
  for (MachineOperand &MO : llvm::drop_begin(MI.operands())) {
    ...
    if (RB != DstRB) {
      auto *OpDef = MRI.getVRegDef(OpReg);
      ...
      MachineBasicBlock::iterator InsertPt = std::next(OpDef->getIterator());
      if (InsertPt != OpDefBB.end() && InsertPt->isPHI())
        InsertPt = OpDefBB.getFirstNonPHI();
      MIB.setInsertPt(*OpDef->getParent(), InsertPt);
      auto Copy = MIB.buildCopy(Ty, OpReg);
      MRI.setRegBank(Copy.getReg(0), *DstRB);
      MO.setReg(Copy.getReg(0));
    }
  }
}
```

它做三件事：

1. 以 PHI def 的 bank 为目标。
2. 在 incoming 的**定义指令之后**插跨 bank COPY；如果定义后面紧跟 PHI，就跳到第一个非 PHI 指令。
3. 把 PHI operand 改成新 COPY 的结果。

`processPHIs` 还处理另一类边角：小于 32 位的标量 PHI，如果 incoming 里同时有 GPR 和 FPR，会统一到 def 的 bank。源码注释明确说，理想情况下 RegBankSelect 不应该选出异质 bank，但 selector 仍要能兜底。

## 例子一：循环 PHI 的 bank 是稳定的（实测）

输入 `phi.ll`：

```llvm
define float @loop_phi(i32 %n, float %init) {
entry:
  br label %loop
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %acc = phi float [ %init, %entry ], [ %acc.next, %loop ]
  %acc.next = fadd float %acc, 1.0
  %i.next = add i32 %i, 1
  %cond = icmp slt i32 %i.next, %n
  br i1 %cond, label %loop, label %exit
exit:
  ret float %acc.next
}
```

`llc -O0 -global-isel -stop-after=regbankselect` 的输出（节选）：

```text
bb.2.loop:
  successors: %bb.2, %bb.3

  %2:gpr(i32) = G_PHI %9(i32), %bb.1, %7(i32), %bb.2
  %3:fpr(f32) = G_PHI %1(f32), %bb.1, %5(f32), %bb.2
  %11:fpr(f32) = G_FCONSTANT float 1.000000e+00
  %5:fpr(f32) = G_FADD %3, %11
  %10:gpr(i32) = G_CONSTANT i32 1
  %7:gpr(i32) = G_ADD %2, %10
  %13:gpr(i32) = G_ICMP intpred(slt), %7(i32), %0
  G_BRCOND %13(i32), %bb.2
  G_BR %bb.3
```

观察：

- 整数 PHI `%2:gpr`，backedge 的 `%7:gpr` 也是 GPR。
- 浮点 PHI `%3:fpr`，backedge 的 `%5:fpr` 也是 FPR。
- 没有跨 bank COPY。因为循环体的定义 bank 与 PHI 的 def bank 天然一致。

这不是 RegBankSelect “看懂了循环”，而是每条 `G_ADD` / `G_FADD` 的默认 mapping 恰好与 PHI 的 def 一致。若循环体里出现把值搬到另一个 bank 的运算，稳定性就会被打破。

## 例子二：跨 bank PHI 与 selector 补 COPY（实测）

输入 `phi-cross.ll` 的第一个函数：

```llvm
define float @phi_bitcast(i1 %c, i32 %a, float %b) {
entry:
  br i1 %c, label %t, label %f
t:
  %x = bitcast i32 %a to float
  br label %join
f:
  %y = fadd float %b, 1.0
  br label %join
join:
  %p = phi float [ %x, %t ], [ %y, %f ]
  %r = fadd float %p, 2.0
  ret float %r
}
```

Fast 模式的 `-stop-after=regbankselect` 输出（节选）：

```text
bb.2.t:
  %8:gpr(f32) = G_BITCAST %1(i32)
  G_BR %bb.4

bb.3.f:
  %13:fpr(f32) = G_FCONSTANT float 1.000000e+00
  %7:fpr(f32) = G_FADD %2, %13
  G_BR %bb.4

bb.4.join:
  %9:gpr(f32) = G_PHI %8(f32), %bb.2, %7(f32), %bb.3
  %12:fpr(f32) = G_FCONSTANT float 2.000000e+00
  %17:fpr(f32) = COPY %9(f32)
  %11:fpr(f32) = G_FADD %17, %12
```

逐行读：

1. `%8` 由 `G_BITCAST` 定义。Fast 模式下 `G_BITCAST` 复用源 bank，源 `%1` 是 GPR，所以 `%8:gpr`。
2. `%7` 由 `G_FADD` 定义，天然是 FPR。
3. PHI 的 def 被选成 `gpr`，因为第一个带 bank 的 incoming `%8` 是 GPR。
4. PHI 的结果被 `G_FADD` 使用，需要 FPR，于是 RegBankSelect 在 PHI 之后插了 `%17 = COPY %9`。
5. `bb.3.f` 里**没有** `%7 -> gpr` 的 COPY。incoming 的 bank 不匹配被留到 ISel。

最终汇编（节选）证实 selector 补了这一步：

```asm
.LBB0_2:                                // %f
  ldr s0, [sp, #12]
  fmov s1, #1.00000000
  fadd s0, s0, s1
  fmov w8, s0
  str w8, [sp, #4]
  b .LBB0_3
.LBB0_3:                                // %join
  ldr w8, [sp, #4]
  fmov s0, w8
```

`fmov w8, s0` 就是 FPR→GPR 的跨 bank 复制；它出现在 `%f` 块里、`b .LBB0_3` 之前，正是 `fixupPHIOpBanks` 在定义 `%7` 的指令之后插入的 COPY 被 selector 选中后的结果。

第二个函数 `phi_fp_int` 是镜像：

```text
bb.4.join:
  %9:fpr(i64) = G_PHI %8(fpr(i64)), %bb.2, %7(gpr(i64)), %bb.3
  %17:gpr(i64) = COPY %9(i64)
  %11:gpr(i64) = G_ADD %17, %12
```

PHI def 是 FPR，incoming 一个是 FPR、一个是 GPR；PHI 结果被整数 `G_ADD` 使用，所以在 PHI 后插 FPR→GPR COPY。selector 再在 `%f` 块里把 `%7` 的 GPR 值搬到 FPR。

这两个例子说明：**PHI 的 def bank 和 incoming bank 是两套账**。RegBankSelect 只结第一套，第二套由 selector 的 PHI 善后结清。

## 例子三：临界边拆分的源码条件（源码依据）

考虑一个 PHI 的 incoming 需要修复，而前驱块里的 terminator 又修改了同一个寄存器。`RepairingPlacement` 会走到：

```cpp
MachineBasicBlock &Pred = *MI.getOperand(OpIdx + 1).getMBB();
Register Reg = MO.getReg();
MachineBasicBlock::iterator It = Pred.getLastNonDebugInstr();
for (auto Begin = Pred.begin(); It != Begin && It->isTerminator(); --It)
  if (It->modifiesRegister(Reg, &TRI)) {
    // We cannot hoist the repairing code in the predecessor.
    // Split the edge.
    addInsertPoint(Pred, *MI.getParent());
    return;
  }
```

只有“不能把 COPY 提到 terminator 前面”时才拆边。拆边由 `InsertPoint::insert` 执行：

```cpp
MachineBasicBlock *NewBB = Src.SplitCriticalEdge(DstOrSplit, P);
```

`assert(Src.succ_size() > 1 && DstOrSplit->pred_size() > 1 && "Edge is not critical");` 说明：只有真正的临界边才需要新块；非临界边可以在前驱或后继里找到插入点。

本章没有在官方包上构造出触发这条路径的 AArch64 用例，原因正是前面说的：普通 `G_PHI` 的 incoming 不走 repair。你可以把这段当“机制存在且条件明确”，不要把它当成日常现象。

## 对比与取舍

| 方案 | COPY 位置 | 优点 | 代价 / 风险 |
| --- | --- | --- | --- |
| PHI def 修复 | 最后一个 PHI 之后 | 局部、简单，不改变 CFG | 增加 join 块的活跃范围 |
| incoming 在 selector 修复 | 定义指令之后、前驱块内 | 不拆边，复制靠近定义 | 可能多次复制同一个值 |
| 拆临界边 | 新建边块 | 可在边块里集中修复 | 改变 CFG，增加块和分支，代价带 5% bias |
| 让 PHI 直接用某个 bank | 无 COPY | 最省 | 只在该 bank 对所有 use 都合适时成立 |

AArch64 的现实选择是：RegBankSelect 先把 def 定下来，selector 再统一 incoming。这个分工让 RegBankSelect 保持局部和简单，但把“异质 PHI”的复杂度推到了 ISel。

## 踩雷集锦

1. **以为 PHI 的 incoming 必须和 def 同 bank**  
   错。`G_PHI %8(gpr), %bb.2, %7(fpr), %bb.3` 在 `-stop-after=regbankselect` 输出里完全合法；AArch64 会在 `fixupPHIOpBanks` 里补 COPY。判断 MIR 是否坏，要看 verifier，不要看“整齐不整齐”。

2. **以为 RegBankSelect 会在每个前驱插 COPY**  
   错。copy-like 的 `InstructionMapping` 只有 operand 0；incoming 的修复不在它的映射范围里。你在 `-stop-after=regbankselect` 里看不到前驱 COPY，不代表后面不会生成。

3. **把 PHI 后面的 COPY 当成 PHI 选错 bank**  
   它可能只是 PHI 结果的某个 use 需要另一个 bank。先看 use 的映射，再判断 PHI 的 def 是否真的不合理。`%17:fpr = COPY %9:gpr` 的成因通常是 `G_FADD` 需要 FPR，而不是 PHI 本身错了。

4. **在循环里假设 backedge 的 bank 已经确定**  
   RPO 下 header 可能先被访问，backedge 的值还没 bank。PHI 的 def 往往由 entry incoming 决定；不要用“循环体最热路径”去解释它的 bank。

5. **为了找临界边拆分而硬造 MIR**  
   普通 `G_PHI` 的默认路径不会走 incoming repair；split 需要“PHI/terminator 的操作数必须修复且无法在前驱插入”这一组合条件。没有真实触发时，应该标源码依据，而不是伪造一段 split MIR。

## 进阶

三个值得继续挖的方向：

1. **Greedy 下的 PHI 代价**  
   PHI 的 mapping 只有 def，但它的 use 会参与后续指令的 repair 代价。试着把 `phi-cross.ll` 跑 `-regbankselect-greedy`，观察 PHI def 是否改变、COPY 数量是否变化。

2. **小于 32 位的 PHI**  
   `processPHIs` 对 s8/s16 的 GPR/FPR 混合有专门处理。构造 `phi i16`，一条入边来自 `G_LOAD`，一条来自 `G_FADD`，看 selector 如何统一。

3. **target 自定义 PHI mapping**  
   如果某个 target 给 `G_PHI` 返回多于 1 个 operand 的 mapping，`RepairingPlacement` 的 incoming 分支才会真正参与代价计算。读 Ch 21 写自定义 `RegisterBankInfo` 时，要意识到这个契约。

## 动手练习

1. 给 `phi-cross.ll` 的 `phi_bitcast` 加第三个前驱，让三条入边分别是 GPR、FPR、GPR。先手写预测：PHI def 在哪个 bank？PHI 后会不会有 COPY？再跑 `-stop-after=regbankselect` 验证。
2. 把 `phi_fp_int` 的最终 `G_ADD` 换成 `G_FADD`（需要把类型改成 double），观察 PHI def 和 COPY 位置如何变化。
3. 用 `-stop-after=legalizer` 保存 `phi.ll` 的 MIR，再用 `-run-pass=regbankselect -regbankselect-greedy` 重跑；比较 Fast/Greedy 下 PHI 的 def bank 和 COPY 数量。
4. 写一个带自环和 entry 两个前驱的 `phi i32`，其中一个 incoming 由 `G_BITCAST` 从 float 而来。预测 selector 会把 COPY 插在哪个块、哪条指令之后。

## 本章重点整理

- `G_PHI` 是 copy-like；默认 `InstructionMapping` 只描述 def，不描述 incoming。
- PHI 的 def bank 与 incoming bank 可以不同；这不是错误，AArch64 在 ISel 的 `fixupPHIOpBanks` 里补 COPY。
- 循环 PHI 的 bank 常由 entry incoming 决定；RPO 下 backedge 的值可能尚未分配。
- RegBankSelect 的 repair 是局部的：普通 use 的 COPY 插在使用块，PHI def 的 COPY 插在最后一个 PHI 之后。
- 临界边拆分只在“必须修复且无法在前驱插入”时发生；AArch64 普通 G_PHI 路径很少触发，相关结论标源码依据。

## 自我检核

- [ ] 能解释为什么 `%9:gpr = G_PHI %8:gpr, %bb.2, %7:fpr, %bb.3` 在 RegBankSelect 输出里是合法的
- [ ] 能说出 copy-like 指令的 `NumOperandsForMapping` 为什么是 1
- [ ] 能指出 PHI 后面的 COPY 是“def 修复”还是“use 修复”
- [ ] 能描述 `fixupPHIOpBanks` 把 COPY 插在什么位置，以及为什么遇到 PHI 要跳到第一个非 PHI
- [ ] 能说出临界边拆分的两个前提，并解释 5% bias 的作用

## 延伸阅读

### 源码

- **[RegBankSelect.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/GlobalISel/RegBankSelect.cpp)**
  - **读哪里**：`RepairingPlacement::RepairingPlacement`、`tryAvoidingSplit`、`computeMapping` 里的 split bias。
  - **学什么**：PHI/terminator 的插入点选择，以及临界边拆分的精确条件。
  - **和本章的关联**：例子三和“5% bias”直接来自这些函数。

- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - **读哪里**：`isCopyLike` 与 `getInstrMappingImpl` 的 copy-like 分支。
  - **学什么**：为什么 PHI 只映射 def，以及 `cannotCopy` 检查做了什么。
  - **和本章的关联**：这是“incoming 不必同 bank”的数据模型依据。

- **[AArch64InstructionSelector.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp)**
  - **读哪里**：`fixupPHIOpBanks` 与 `processPHIs`。
  - **学什么**：selector 如何在定义之后补跨 bank COPY，以及小于 32 位的 PHI 如何处理。
  - **和本章的关联**：例子二的最终汇编就来自这里。

### 文档

- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：PHI 的 MIR 语法、`bb` 标签、`successors`。
  - **学什么**：`G_PHI %x, %bb.1` 的 operand 配对规则。
  - **和本章的关联**：不会读 PHI 的 predecessor 列表，就无法判断 COPY 该插在哪。

下一章把视角从 CFG 转到 ABI：调用、返回、物理寄存器和隐式操作数。你会看到 RegBankSelect 在调用边界上几乎没有选择权，因为它看到的是 CallLowering 已经固定好的物理寄存器 COPY。

→ [Ch 12 调用、返回与物理寄存器](./12-calls-and-physical-registers.md)