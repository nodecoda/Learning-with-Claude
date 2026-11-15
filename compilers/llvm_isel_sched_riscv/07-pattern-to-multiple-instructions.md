# Ch 7 — 一条 pattern 如何生成多条指令

> **目标**：看懂「一条 IR 操作」和「一条 MachineInstr」不是一一对应的。学完这章，你能区分三种多指令来源：TableGen pattern 的嵌套输出、C++ `Select()` 里的手工序列、伪指令的延迟展开；也能说清它们分别发生在 isel、pre-RA、post-RA 的哪个时刻，以及这对 scheduler 意味着什么。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章用 `sh3c.ll`、`pseudo.ll`、`sel2.ll`，主要以 `-print-after-isel`、`-print-after=riscv-expand-pseudo` 和最终汇编为证据。

## 为什么需要这个？

面试里有一个很容易答错的问法：

> 「`sh3add` 的 pattern 只有一条指令，为什么 isel 之后 MIR 里有两条？」

或者反过来：

> 「`ret ptr @g` 在 isel 之后只有一条 `PseudoMovAddr`，为什么汇编里变成 `lui + addi`？」

这两个问题的共同点是：**多指令可以发生在不同阶段**。如果你只会说「pattern 生成指令」，就会把伪指令展开、常量物化、target lowering 的账都算到 pattern 头上。

这一章把「一条 IR 操作变成多条 MachineInstr」拆成三条路径：

```text
路径 A：TableGen pattern 的输出的 DAG 里嵌套了多条指令
        -> isel 时一次生成多条 MI

路径 B：C++ Select() 里手工 BuildMI / getMachineNode
        -> isel 时生成任意序列

路径 C：isel 只生成一条伪指令
        -> post-RA 再展开成多条真实指令
```

三条路径对 scheduler 的可见性不同，这是本章真正要建立的判断。

## 先建立直觉

三条路径的时间线：

```text
                 isel                pre-RA sched        RA        post-RA expand     post-RA sched
路径 A   ADDI + SH3ADD  ────────────► 看到两条 MI  ─────► ... ───► 无              ───► 看到两条
路径 B   LUI + ADDI     ────────────► 看到两条 MI  ─────► ... ───► 无              ───► 看到两条
路径 C   PseudoMovAddr  ────────────► 看到一条 MI  ─────► ... ───► LUI + ADDI      ───► 看到两条
```

关键结论先放在这里：

- **路径 A/B 的多条 MI 在 isel 结束时就存在**，pre-RA scheduler 能看到它们，可以分别排。
- **路径 C 的多条 MI 在 post-RA 才出现**，pre-RA scheduler 只看到一条伪指令，调度模型也只按一条指令算。
- 这不是实现细节，而是 RISC-V 后端明确的取舍：`.td` 里 `PseudoMovAddr` 的注释写着 `FIXME: The scheduling information does not reflect the multiple instructions.`

## 路径 A：嵌套输出 pattern

### 例子：`add r, 8200` + `Zba`

`sh3c.ll`：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @sh3c(i64 %r) {
  %x = add i64 %r, 8200
  ret i64 %x
}
```

跑：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zba -O2 sh3c.ll -o -
```

最终汇编：

```asm
sh3c:
	li	a1, 1025
	sh3add	a0, a1, a0
	ret
```

`li a1, 1025` 在 MIR 里不是一条 `LI` 指令，而是 `ADDI $x0, 1025`。`-print-after-isel`：

```text
# After Instruction Selection:
# Machine code for function sh3c: IsSSA, TracksLiveness

bb.0 (%ir-block.0):
  liveins: $x10
  %0:gpr = COPY $x10
  %1:gpr = ADDI $x0, 1025
  %2:gpr = SH3ADD %1:gpr, %0:gpr
  $x10 = COPY %2:gpr
  PseudoRET implicit $x10
```

两条 MI：

```text
%1:gpr = ADDI $x0, 1025     ; 物化 1025
%2:gpr = SH3ADD %1:gpr, %0  ; (1025 << 3) + r = 8200 + r
```

`-debug-only=isel` 的对应日志：

```text
Skipped scope entry (due to false predicate) at index 30039, continuing at 30069
Created node: t11: i64 = ADDI Register:i64 $x0, TargetConstant:i64<1025>
Morphed node: t4: i64 = SH3ADD t11, t2
ISEL: Match complete!
```

注意 `Created node` 这一行：matcher 在 `MorphNodeTo` 之前先创建了 `ADDI` 节点，然后 `SH3ADD` 以它为第一个操作数。这不是 isel 之后某个 pass 加的，而是 pattern 的输出 DAG 里本来就有的。

### 对应的 `.td` pattern

`RISCVInstrInfoZb.td`：

```tablegen
class CSImm12MulBy8Pat<Instruction sh3add>
    : Pat<(riscv_add_like (XLenVT GPR:$r), CSImm12MulBy8:$i),
          (sh3add (XLenVT (ADDI (XLenVT X0), CSImm12MulBy8:$i)),
                  GPR:$r)>;
```

右边不是一条指令，而是一棵小树：

```text
SH3ADD
 ├─ ADDI X0, imm
 └─ GPR r
```

TableGen 会把 `ADDI` 作为 `SH3ADD` 的子节点生成。`CSImm12MulBy8` 的 predicate 决定 `8200` 能不能进来：

```tablegen
def CSImm12MulBy8 : PatLeaf<(imm), [{
  if (!N->hasOneUse())
    return false;
  int64_t C = N->getSExtValue();
  return !isInt<14>(C) && !isShiftedInt<20, 12>(C) && isShiftedInt<12, 3>(C);
}], SimmShiftRightBy3XForm>;
```

对 `8200`：

- `hasOneUse()`：常量只被这个 `add` 使用，满足。
- `!isInt<14>(8200)`：`8200 > 8191`，满足。
- `!isShiftedInt<20, 12>(8200)`：`8200` 不是 `k * 4096`，满足。
- `isShiftedInt<12, 3>(8200)`：`8200 = 1025 * 8`，且 `1025` 在 signed 12-bit 范围内，满足。

所以 `CSImm12MulBy8Pat` 匹配，`CSImm12MulBy8` 的 `SDNodeXForm` 把常量右移 3 位变成 `1025`，输出 `ADDI X0, 1025`。

为什么不用 `li`？`li` 是汇编器的伪指令；MIR 层的规范形式是 `ADDI x0, imm` 或 `LUI + ADDI`。最终打印成 `li` 是 AsmPrinter 的选择。

### 另一个候选：`8000` 为什么走 `SH2ADD`

同样结构的 `add r, 8000`，实测输出：

```asm
li	a1, 2000
sh2add	a0, a1, a0
```

因为 `8000 = 2000 * 4`，`CSImm12MulBy4Pat` 先匹配，`2000` 在 simm12 内，生成 `ADDI x0, 2000` + `SH2ADD`。`8192` 又不同：`8192 = 2 << 12`，被 `isShiftedInt<20, 12>` 排除，最终走 `lui + add`。这些差异都来自 predicate 的数值边界，不是 scheduler。

## 路径 B：C++ `Select()` 手工生成序列

### 例子：`ret i64 305419896`

```llvm
define i64 @big2() {
  ret i64 305419896
}
```

最终汇编：

```asm
big2:
	lui	a0, 74565
	addi	a0, a0, 1656
	ret
```

`-print-after-isel`：

```text
bb.0 (%ir-block.0):
  %0:gpr = LUI 74565
  %1:gpr = ADDI %0:gpr, 1656
  $x10 = COPY %1:gpr
  PseudoRET implicit $x10
```

两条 MI 都是 isel 时生成的。它们不是来自 `Pat`，而是来自 `RISCVDAGToDAGISel::Select()` 的 `ISD::Constant` 分支：

```cpp
case ISD::Constant: {
  auto *ConstNode = cast<ConstantSDNode>(Node);
  int64_t Imm = ConstNode->getSExtValue();
  ...
  ReplaceNode(Node, selectImm(CurDAG, DL, VT, Imm, *Subtarget).getNode());
  return;
}
```

`selectImm()`（`RISCVISelDAGToDAG.cpp` 约 276 行）调用 `RISCVMatInt::generateInstSeq`：

```cpp
static SDValue selectImm(SelectionDAG *CurDAG, const SDLoc &DL, const MVT VT,
                         int64_t Imm, const RISCVSubtarget &Subtarget) {
  RISCVMatInt::InstSeq Seq = RISCVMatInt::generateInstSeq(Imm, Subtarget);
  ...
}
```

`generateInstSeq` 把常量拆成 `LUI`、`ADDI`、`ADDIW`、`SLLI` 等序列，`selectImmSeq` 用 `getMachineNode` 逐条建节点，后一条以前一条为输入。`305419896 = 0x12345678`：

```text
LUI  a0, 0x12345     ; a0 = 0x12345000
ADDI a0, a0, 0x678   ; a0 = 0x12345678
```

`LUI` 的立即数是 `74565`（`0x12345`），`ADDI` 的立即数是 `1656`（`0x678`）。

这条路和路径 A 的区别：

- 路径 A 的 `ADDI + SH3ADD` 由 TableGen 生成的 matcher 创建。
- 路径 B 的 `LUI + ADDI` 由 C++ 的 `selectImm` 创建。
- 两者都发生在 isel 阶段，pre-RA scheduler 都能看到两条 MI。

### 什么时候走伪指令 `PseudoMovImm`

`selectImm` 里还有一条分支：

```cpp
if (Seq.size() == 2 && Subtarget.getCLOpts().use_rematerializable_movimm)
  return SDValue(CurDAG->getMachineNode(RISCV::PseudoMovImm, DL, VT,
                                        CurDAG->getSignedTargetConstant(Imm, DL, VT)), 0);
```

如果序列正好两条，并且打开了 `use_rematerializable_movimm`，`selectImm` 不会直接生成 `LUI + ADDI`，而是生成一条 `PseudoMovImm`。这条伪指令在 post-RA 展开。它属于路径 C。

## 路径 C：伪指令延迟展开

### 例子：`ret ptr @g`

```llvm
@g = global i64 0

define ptr @addr() {
  ret ptr @g
}
```

`-print-after-isel`：

```text
bb.0 (%ir-block.0):
  %0:gpr = PseudoMovAddr target-flags(riscv-hi) @g, target-flags(riscv-lo) @g
  $x10 = COPY %0:gpr
  PseudoRET implicit $x10
```

最终汇编：

```asm
addr:
	lui	a0, %hi(g)
	addi	a0, a0, %lo(g)
	ret
```

`PseudoMovAddr` 的定义在 `RISCVInstrInfo.td` 约 1662 行：

```tablegen
// Pseudo for a rematerializable LUI+ADDI sequence for loading an address.
// It will be expanded after register allocation.
// FIXME: The scheduling information does not reflect the multiple instructions.
let Size = 8, isReMaterializable = 1 in
def PseudoMovAddr : Pseudo<(outs GPR:$dst), (ins uimm20_lui:$hi, simm12_lo:$lo), []>,
                    Sched<[WriteIALU]>;
```

三个信息很关键：

1. `isReMaterializable = 1`：寄存器分配器可以在需要时重新物化它，而不是 spill。
2. `Size = 8`：它代表最终两条 4 字节指令的大小。
3. `Sched<[WriteIALU]>`：调度模型只按一条 `WriteIALU` 算；注释明确承认这和实际两条指令不符。

展开发生在 `RISCVExpandPseudoPostRA.cpp` 的 `expandMovAddr()`（约 102 行）：

```cpp
bool RISCVExpandPseudoPostRAImpl::expandMovAddr(
    MachineBasicBlock &MBB, MachineBasicBlock::iterator MBBI) const {
  Register DstReg = MBBI->getOperand(0).getReg();
  BuildMI(MBB, MBBI, DL, TII->get(RISCV::LUI))
      .addDef(DstReg, ...)
      .add(MBBI->getOperand(1));
  BuildMI(MBB, MBBI, DL, TII->get(RISCV::ADDI))
      .addDef(DstReg, ...)
      .addReg(DstReg, RegState::Kill | ...)
      .add(MBBI->getOperand(2));
  MBBI->eraseFromParent();
  return true;
}
```

它把一条 `PseudoMovAddr` 替换成 `LUI` + `ADDI`。`RISCVPassConfig::addPreSched2()` 里注册这个 pass：

```cpp
void RISCVPassConfig::addPreSched2() {
  addPass(createRISCVExpandPseudoPostRALegacyPass());
  ...
}
```

`addPreSched2` 在寄存器分配之后、post-RA scheduler 之前。所以时间线是：

```text
pre-RA MachineScheduler: 看到 PseudoMovAddr（一条）
RA:                      按一条伪指令分配寄存器
addPreSched2:            展开成 LUI + ADDI（两条）
post-RA scheduler:       看到 LUI + ADDI（如果启用了 post-RA 调度）
```

这解释了一个常见的困惑：为什么 `-print-after=machine-scheduler` 里只有 `PseudoMovAddr`，最终汇编却有两条。因为 pre-RA scheduler 在展开之前运行。

### `PseudoMovAddr` 为什么值得存在

如果 isel 直接生成 `LUI + ADDI`，会有什么问题？

- 两条 MI 都要参与寄存器分配，`LUI` 的目标寄存器可能被 spill，增加压力。
- 地址物化是 rematerializable 的：需要时重新算一次，比 spill/reload 便宜。
- 用一条伪指令表达「hi/lo 对」，可以保留重定位信息，方便链接器 relaxation。

代价是调度模型不准确：pre-RA scheduler 以为它是一条 `WriteIALU`，实际是两条有依赖的指令。对大多数函数影响不大，但在紧循环里，`LUI` 的延迟可能没有被提前隐藏。

## 反例：`select` 不是「一条 pattern 生成三条指令」

`select` 常被拿来问「RISC-V 没有条件移动，怎么选」。它的多指令不是 pattern 生成的，而是 target lowering 改结构。

无 `Zicond`：

```asm
sel:
	andi	a3, a0, 1
	mv	a0, a1
	bnez	a3, .LBB0_2
	mv	a0, a2
.LBB0_2:
	ret
```

`-print-after-isel` 显示它已经变成控制流：

```text
%3:gpr = ANDI %0:gpr, 1
BNE %3:gpr, $x0, %bb.2
...
%4:gpr = PHI %1:gpr, %bb.0, %2:gpr, %bb.1
```

有 `+zicond`：

```asm
sel:
	andi	a0, a0, 1
	czero.nez	a2, a2, a0
	czero.eqz	a0, a1, a0
	or	a0, a0, a2
	ret
```

`-print-after-isel`：

```text
%3:gpr = ANDI %0:gpr, 1
%4:gpr = CZERO_NEZ %2:gpr, %3:gpr
%5:gpr = CZERO_EQZ %1:gpr, %3:gpr
%6:gpr = disjoint OR %5:gpr, %4:gpr
```

两条路径都不是「一个 `select` pattern 生成一条指令」：

- 无 `Zicond`：`select` 在 `RISCVISelLowering` 里被 lowering 成分支 + PHI，基本块结构变了。
- 有 `Zicond`：`select` 被 lowering 成 `CZERO_NEZ + CZERO_EQZ + OR`，三条 MI 在 isel 时已经存在。

相关源码在 `RISCVISelLowering.cpp` 约 10470 行（`HasZicond` 判断）和约 10689 行（`CZERO_EQZ`/`CZERO_NEZ` 生成）。这也是第 3 章讲过的 ISA 约束在 selection 层的体现。

## 三条路径对调度的不同影响

| 维度 | 路径 A：嵌套 pattern | 路径 B：`Select()` 序列 | 路径 C：伪指令 |
|---|---|---|---|
| 多指令出现时刻 | isel | isel | post-RA（`addPreSched2`） |
| pre-RA scheduler 看到几条 | 多条 | 多条 | 一条 |
| sched model 是否准确 | 是（每条都有 `SchedRW`） | 是 | 否（`.td` 有 FIXME） |
| 寄存器分配压力 | 多条 MI 都参与 | 多条 MI 都参与 | 只按一条伪指令算 |
| 可否 rematerialize | 通常否 | 通常否 | 是（`isReMaterializable`） |
| 典型例子 | `ADDI + SH3ADD` | `LUI + ADDI` | `PseudoMovAddr`、`PseudoMovImm` |

这张表回答了一个很实际的问题：**为什么有些「两条指令」在 `llvm-mca` 里只按一条算？** 因为 mca 看的是最终汇编，通常两条都会建模；但如果你的分析停在 MIR，或者用了不展开伪指令的路径，就会低估。反过来，pre-RA scheduler 的日志里只有一条 `PseudoMovAddr`，也不能据此认为最终只有一条指令。

## 源码落点

- **`RISCVInstrInfoZb.td`**
  - `CSImm12MulBy4Pat` / `CSImm12MulBy8Pat`（约 736–744 行）：嵌套输出 pattern。
  - `CSImm12MulBy4` / `CSImm12MulBy8`（约 170–186 行）：常量 predicate。
  - **读哪里**：先看 predicate 的数值边界，再看输出 DAG 的嵌套结构。

- **`RISCVISelDAGToDAG.cpp`**
  - `selectImmSeq()`（约 240 行起）：把 `RISCVMatInt::InstSeq` 变成 DAG 节点。
  - `selectImm()`（约 276 行）：常量物化的入口，含 `PseudoMovImm` 分支。
  - `Select()` 的 `ISD::Constant` 分支（约 1196 行）。
  - **读哪里**：看 `getMachineNode` 如何逐条建节点，以及 `PseudoMovImm` 的条件。

- **`RISCVMatInt.cpp`**
  - `generateInstSeq()`（约 214 行）：常量拆分的核心。
  - **读哪里**：`LUI`、`ADDI`、`ADDIW`、`SLLI` 的优先级和边界。

- **`RISCVInstrInfo.td`**
  - `PseudoMovAddr`（约 1662 行）：伪指令定义、`Size`、`isReMaterializable`、`Sched`。
  - `PseudoMovImm`（约 2010 行）：常量版伪指令。
  - **读哪里**：注释里的 `FIXME` 是调度模型不准确的直接证据。

- **`RISCVExpandPseudoPostRA.cpp`**
  - `expandMovAddr()`（约 102 行）：`LUI + ADDI` 展开。
  - `expandMovImm()`（约 88 行）：常量伪指令展开。
  - **读哪里**：`BuildMI` 的顺序和 `eraseFromParent()`。

- **`RISCVTargetMachine.cpp`**
  - `addPreSched2()`（约 488 行）：注册 post-RA 伪指令展开。
  - **读哪里**：确认展开发生在 post-RA scheduler 之前。

## 踩雷集锦

1. **以为 `li` 是一条指令**：MIR 里是 `ADDI x0, imm` 或 `LUI + ADDI`；`li` 是汇编器的别名。看 `-print-after-isel` 不要找 `LI`。

2. **以为 `Morphed node` 只能生成一条 MI**：`Morphed node: t4 = SH3ADD t11, t2` 的背后还有一个 `Created node: t11 = ADDI ...`。一条 pattern 可以生成一棵指令树。

3. **在 `-print-after-isel` 里找不到 `LUI + ADDI` 就以为没生成**：如果走了 `PseudoMovImm`/`PseudoMovAddr`，isel 后只有一条伪指令，展开在 post-RA。

4. **用 pre-RA scheduler 的日志推断最终指令数**：`PseudoMovAddr` 在 pre-RA 是一条，最终是两条。要看最终汇编或 post-RA 展开后的 MIR。

5. **以为伪指令展开一定在 post-RA scheduler 之后**：`RISCVExpandPseudoPostRA` 在 `addPreSched2`，也就是 post-RA scheduler 之前。名字里的 PostRA 是「寄存器分配之后」，不是「post-RA 调度之后」。

6. **把 `select` 当成单条 pattern**：无 `Zicond` 时它变成分支 + PHI；有 `Zicond` 时是三条 MI。它的多指令来自 target lowering，不是 `Pat`。

7. **忽略 `hasOneUse`**：`CSImm12MulBy8` 要求常量只有一个 use。如果常量被多处使用，pattern 不匹配，可能走 `LUI + ADDI` 或常量池。

8. **拿 `PseudoMovAddr` 的 `Sched<[WriteIALU]>` 当真实延迟**：`.td` 自己写了 FIXME。做性能分析时要用展开后的 `LUI + ADDI` 重新建模。

## 动手练习

1. 用 `sh3c.ll` 跑 `-print-after-isel` 和 `-debug-only=isel`，找出 `Created node` 和 `Morphed node` 两行。把 `8200` 改成 `8000`、`8192`、`8184`，记录每种常量走哪条路径。

2. 用 `pseudo.ll` 跑 `-print-after-isel` 和 `-print-after=riscv-expand-pseudo`（如果 pass 名不匹配，用 `-print-after-all` 找）。确认 `PseudoMovAddr` 展开成 `LUI + ADDI` 的时刻。

3. 给 `big2` 的常量换成 `0x7fffffff`、`0x80000000`、`0x123456789`，看 `selectImm` 生成几条 MI，`LUI`/`ADDI`/`ADDIW`/`SLLI` 如何组合。

4. 用 `sel2.ll` 跑 `-mattr=+m` 和 `-mattr=+m,+zicond`，对比 `-print-after-isel`。解释为什么前者有 `BNE` 和 `PHI`，后者没有。

5. 用 `-print-before=machine-scheduler` 看 `PseudoMovAddr` 在 pre-RA scheduler 里是几条 MI；再用最终汇编确认展开后是几条。

6. **guided**：把 `PseudoMovAddr` 的 `Sched<[WriteIALU]>` 改成 `Sched<[WriteIALU, WriteIALU]>`，重新构建，跑 `llvm-mca` 对比。观察 mca 是否把两条都建模；不要提交这个改动。

## 本章重点整理

- 一条 pattern 的输出可以是一棵指令树；`CSImm12MulBy8Pat` 生成 `ADDI + SH3ADD` 两条 MI。
- C++ `Select()` 可以手工生成任意序列；`selectImm` 生成 `LUI + ADDI`，也支持 `PseudoMovImm`。
- 伪指令把多指令延迟到 post-RA；`PseudoMovAddr` 在 pre-RA 是一条，展开后是 `LUI + ADDI`。
- 三条路径对 scheduler 的可见性不同：A/B 在 isel 就可见，C 在 `addPreSched2` 才可见。
- `PseudoMovAddr` 的 `Sched<[WriteIALU]>` 不反映两条指令；`.td` 的 FIXME 是性能分析时要注意的坑。
- `select` 的多指令来自 target lowering（分支或 `czero` 序列），不是单条 `Pat`。

## 自我检核

- [ ] 能说出「一条 pattern 生成多条指令」的两种 isel 时机制，以及一种 post-RA 机制
- [ ] 能解释 `CSImm12MulBy8Pat` 为什么生成 `ADDI + SH3ADD`，以及 `8200` 为什么满足 predicate
- [ ] 能解释 `li` 在 MIR 里是什么，`LUI + ADDI` 如何物化 `0x12345678`
- [ ] 能说出 `PseudoMovAddr` 的三个关键属性：`Size`、`isReMaterializable`、`Sched`
- [ ] 能解释 `addPreSched2` 和 post-RA scheduler 的先后顺序
- [ ] 能解释 `select` 在有无 `Zicond` 时的两种 lowering
- [ ] 能用 `-print-after-isel` 和最终汇编区分「isel 时多条」和「post-RA 展开多条」

## 延伸阅读

### 官方文档

- **[LLVM Code Generator — Instruction Selection](https://llvm.org/docs/CodeGenerator.html#instruction-selection-section)**
  - **读哪里**：pattern 的输出、`MorphNodeTo`、伪指令的概念。
  - **和本章的关联**：路径 A/B 的官方背景。

- **[LLVM Machine Code Layer](https://llvm.org/docs/CodeGenerator.html#machine-code)**
  - **读哪里**：MachineInstr、伪指令、post-RA pass 的顺序。
  - **和本章的关联**：路径 C 的 pass 时序。

- **[RISC-V ELF psABI](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)**
  - **读哪里**：`%hi`/`%lo` 重定位、代码模型。
  - **和本章的关联**：`PseudoMovAddr` 为什么保留 hi/lo 两个 operand。

### 源码

- **`llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`**
  - **读哪里**：`CSImm12MulBy4Pat` / `CSImm12MulBy8Pat`（约 736–744 行）、`CSImm12MulBy8` predicate（约 178 行）。
  - **和本章的关联**：路径 A 的 pattern 和数值边界。

- **`llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`**
  - **读哪里**：`selectImmSeq()`（约 240 行）、`selectImm()`（约 276 行）、`Select()` 的 `ISD::Constant`（约 1196 行）。
  - **和本章的关联**：路径 B 的常量物化。

- **`llvm/lib/Target/RISCV/RISCVMatInt.cpp`**
  - **读哪里**：`generateInstSeq()`（约 214 行）、`generateInstSeqImpl()`（约 53 行）。
  - **和本章的关联**：`LUI`/`ADDI`/`ADDIW`/`SLLI` 的选择逻辑。

- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - **读哪里**：`PseudoMovAddr`（约 1662 行）、`PseudoMovImm`（约 2010 行）。
  - **和本章的关联**：路径 C 的伪指令定义和 FIXME。

- **`llvm/lib/Target/RISCV/RISCVExpandPseudoPostRA.cpp`**
  - **读哪里**：`expandMovAddr()`（约 102 行）、`expandMovImm()`（约 88 行）。
  - **和本章的关联**：post-RA 展开的具体 BuildMI 序列。

- **`llvm/lib/Target/RISCV/RISCVTargetMachine.cpp`**
  - **读哪里**：`addPreSched2()`（约 488 行）。
  - **和本章的关联**：展开发生在 post-RA scheduler 之前。

### 文章 / 演讲

- **"Pseudo Instructions in LLVM"**（LLVM 后端相关演讲/博客）
  - **这讲什么**：伪指令的生命周期、展开时机、对 RA 和调度的影响。
  - **为什么值得看**：本章路径 C 的通用版本。

- **"RISC-V Code Model and Relocations"**（RISC-V LLVM 相关演讲）
  - **这讲什么**：`%hi`/`%lo`、`medlow`/`medany`、链接器 relaxation。
  - **和本章的关联**：`PseudoMovAddr` 为什么不能用普通 `LUI + ADDI` 直接替代。

下一章回到调度：DAG 已经被选成 MachineInstr，`ScheduleDAGSDNodes` 如何建 SUnit 图、如何算 Depth/Height、如何做 bottom-up list scheduling，以及为什么同一段 MIR 在 `-enable-misched=false` 下会得到另一个顺序。

→ [Ch 8 SelectionDAG 的调度器：`ScheduleDAGSDNodes`](./08-selectiondag-scheduler.md)
