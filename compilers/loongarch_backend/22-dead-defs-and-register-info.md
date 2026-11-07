# Ch 22 — 死定义与寄存器信息：`$zero` 作为写目标

> 本章目标：理解 `DeadRegisterDefinitions` 为什么把“没人用的 load 结果”改写成 `$zero`；知道 `getReservedRegs` 的五个保留寄存器及原因；并能读懂 `eliminateFrameIndex` 里 `LDPTR → LD` 的降级逻辑。

---

## 22.1 一个奇怪的现象：`ld.d $zero, $a0, 0`

```llvm
define void @vd(ptr %p) {
  %v = load volatile i64, ptr %p
  ret void
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
vd:
	ld.d	$zero, $a0, 0
	ret
```

`%v` 没人用，但 `volatile` load **不能删**（有副作用）。于是编译器保留 load，把目标寄存器改成 `$zero`——结果被丢弃，但访存仍然发生。

```console
$ llc ... -stats dd.ll -o /dev/null
 1 loongarch-dead-defs - Number of dead definitions replaced
```

这个计数来自 `LoongArchDeadRegisterDefinitions.cpp`。

---

## 22.2 pass 的判定条件

```cpp
// We only handle non-computational instructions.
const MCInstrDesc &Desc = MI.getDesc();
if (!Desc.mayLoad() && !Desc.mayStore() && !Desc.hasUnmodeledSideEffects())
  continue;
```

注意这个 `continue` 的方向：**只有“可能访存或有未知副作用”的指令才处理**。纯计算指令（`add.d`）如果结果是死的，会被通用的死代码消除直接删掉，轮不到这里。

接着：

```cpp
for (int I = 0, E = Desc.getNumDefs(); I != E; ++I) {
  MachineOperand &MO = MI.getOperand(I);
  if (!MO.isReg() || !MO.isDef() || MO.isEarlyClobber())
    continue;
  if (MI.isRegTiedToUseOperand(I)) { ... continue; }   // 别动 tied operand
  Register Reg = MO.getReg();
  if (!Reg.isVirtual() || !MO.isDead())
    continue;
  const TargetRegisterClass *RC = TII->getRegClass(Desc, I);
  if (!(RC && RC->contains(LoongArch::R0)))
    continue;                                          // 只有 GPR 能写 $zero
  LIS.removeInterval(Reg);
  MO.setReg(LoongArch::R0);
}
```

五道门槛：

1. **有副作用**（load/store/unmodeled）；
2. **不是 early-clobber**（early-clobber 的定义不能随便改）；
3. **不是 tied operand**（`$rd = $rj` 这类约束，改了会破坏语义）；
4. **是虚拟寄存器且已标记 dead**（由 LiveIntervals 判定）；
5. **寄存器类包含 `$r0`**（FPR/向量寄存器不能写 `$zero`）。

少任何一条都会生成错误代码。这也是为什么这个 pass 只有 104 行却要引用 `LiveIntervals`。

---

## 22.3 `getReservedRegs`：五个保留寄存器

`LoongArchRegisterInfo.cpp:95`：

```cpp
markSuperRegs(Reserved, LoongArch::R0);   // zero
markSuperRegs(Reserved, LoongArch::R2);   // tp
markSuperRegs(Reserved, LoongArch::R3);   // sp
markSuperRegs(Reserved, LoongArch::R21);  // non-allocatable
if (TFI->hasFP(MF))
  markSuperRegs(Reserved, LoongArch::R22); // fp
if (TFI->hasBP(MF))
  markSuperRegs(Reserved, LoongArchABI::getBPReg()); // bp
```

| 寄存器 | 原因 |
|---|---|
| `$r0` | 恒零，写它等于丢弃 |
| `$r2` | `$tp` 线程指针（TLS） |
| `$r3` | `$sp` 栈指针 |
| `$r21` | ABI 保留，不可分配 |
| `$r22` | `$fp`，仅 `hasFP` 时保留 |
| `$r23` | `$bp`，仅栈重对齐 + 动态 alloca（`hasBP`）时保留 |

`markSuperRegs` 会连别名一起保留（比如 `$a0` 是 `$r4` 的别名，保留 `$r4` 会连 `$a0` 一起标记）。这就是为什么用 `markSuperRegs` 而不是 `Reserved.set()`。

---

## 22.4 `getFrameRegister` 与 BP

```cpp
Register LoongArchRegisterInfo::getFrameRegister(const MachineFunction &MF) const {
  return TFI->hasFP(MF) ? LoongArch::R22 : LoongArch::R3;
}
```

- 有 `$fp` 用 `$r22`；
- 否则用 `$sp`（`$r3`）做基址。

注意 `$bp`（`$r23`）不是 frame register，它只在“栈重对齐 + 动态分配”时记录重对齐后的 `$sp`（Ch 10 的 `hasBP`）。

---

## 22.5 `eliminateFrameIndex`：`LDPTR` 的降级

`eliminateFrameIndex()`（`:121`）负责把 `FrameIndex` 操作数换成“基址寄存器 + 偏移”。开头有一段：

```cpp
// Frame index elimination may change a previously encodable displacement
// into one that does not fit si14 << 2. Use the equivalent unscaled
// instruction and let the existing si12 handling legalize the final offset.
if (!isShiftedInt<14, 2>(FixedOffset)) {
  switch (MIOpc) {
  case LoongArch::LDPTR_W: NewOpc = LoongArch::LD_W; break;
  case LoongArch::LDPTR_D: NewOpc = LoongArch::LD_D; break;
  case LoongArch::STPTR_W: NewOpc = LoongArch::ST_W; break;
  case LoongArch::STPTR_D: NewOpc = LoongArch::ST_D; break;
  }
  ...
}
```

背景（Ch 14）：`ldptr.d` 的偏移是 `simm14 << 2`（±32KB）。但 `FrameIndex` 消除后，实际偏移可能变成 `sp + 20000` 这种超出 si14<<2 的值。这时不能硬编码，要：

1. 把 `ldptr.d` 降级成 `ld.d`（12 位偏移）；
2. 偏移仍然超出 12 位时，再由后续逻辑物化到临时寄存器 / 用 `ldx.d`。

**这是“选指时合法、帧消除后不合法”的典型例子**：pattern 阶段偏移是编译期常量，看起来能编码；帧布局确定后，同一个偏移可能变大。

---

## 22.6 文件头注释已经过时

`eliminateFrameIndex` 开头还留着：

```cpp
// TODO: this implementation is a temporary placeholder which does just
// enough to allow other aspects of code generation to be tested.
```

但函数体已经有 200+ 行，处理了 `LDPTR` 降级、si12 合法性、scavenging 等。**这类过时注释是低风险文档补丁的好目标**：改动小、容易 review、能直接贡献。

（提交前先 `git log -S "temporary placeholder"` 确认没有历史原因。）

---

## 22.7 反直觉点

1. **这里的“dead def”不是“死代码”**，而是“有副作用指令的死结果”；纯计算指令根本不会进来。
2. **`$zero` 是合法写目标**，写它等于丢弃结果，但访存/副作用仍然发生。
3. **tied operand 不能改**，否则 `$rd = $rj` 的约束被破坏。
4. **只有 GPR 类能写 `$zero`**，FPR/向量寄存器没有零寄存器。
5. **`$r21` 是 ABI 保留**，不是“暂时没用”。
6. **`$r23`（BP）只在特定条件下保留**，不是永远。
7. **`eliminateFrameIndex` 会改 opcode**（`LDPTR` → `LD`），不只是改偏移。
8. **文件头注释可能骗你**——`temporary placeholder` 早已不成立。

---

## 22.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| `ld.d $zero` 实测 | 本章 22.1 |
| `-stats` 计数 | 本章 22.1 |
| 判定条件 | `LoongArchDeadRegisterDefinitions.cpp:60-95` |
| 保留寄存器 | `LoongArchRegisterInfo.cpp:95-113` |
| frame register | `:116-119` |
| `LDPTR → LD` | `:121-175` |
| pass 开关 | `LoongArchTargetMachine.cpp:50-54,238-240` |

---

## 22.9 练习

1. 把 `volatile` 去掉，观察 load 是否被完全删除（`$zero` 也不出现）。
2. 构造一个 tied operand 的死定义（如 `%r = add %a, 1` 后不使用），确认 pass 不改它。
3. 用 `-loongarch-enable-dead-defs=false` 对比输出。
4. 找一个 `FrameIndex` 偏移超过 si14<<2 的例子，观察 `LDPTR → LD` 的降级。
5. 检查 `$r21` 在 ABI 文档里的用途，解释为什么 LLVM 直接保留它。

---

## 22.10 下一步

下一章看调度模型与 `llvm-mca`：`SchedLA464.td`/`SchedLA664.td` 怎么写、`-mcpu=la664` 如何改变指令选择与调度、以及怎么用 `llvm-mca` 评估一段循环。
