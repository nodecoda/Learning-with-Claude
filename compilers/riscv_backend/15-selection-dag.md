# 第 15 章 SelectionDAG 选指：从 DAG 到机器指令

合法化之后，函数体已经是一张“目标能接受的 DAG”。下一步是把每个 `ISD` 节点换成真实的机器指令：`mul` 换成 `MUL`，`add` 换成 `ADDI`，`load` 换成 `LD`，`select` 换成一条带条件码的伪指令。这个阶段叫指令选择（instruction selection），RISC-V 的实现由两部分组成：TableGen 生成的匹配表，以及手写的 `RISCVDAGToDAGISel::Select`。

这一章用 MIR 和 `-print-after` 的证据把这条链路拆开。重点不是背 API，而是知道：

- 一个 `ISD` 节点有哪些“隐藏输入”（chain、glue），为什么 store 后面的 load 有时会消失。
- `-stop-after=riscv-isel` 和 `-stop-after=finalize-isel` 之间到底发生了什么。
- TableGen 的 `Pat<...>` 怎样变成 `RISCVGenDAGISel.inc` 里的匹配项，C++ 的 `SelectNodeTo` 又在什么时候接管。
- 地址选择和常量选择为什么单独存在，`PseudoLI` 为什么不在代码生成里出现。
- 选指失败时先看什么，`Cannot select` 到底该往哪一层查。

## 为什么

选指是后端里“最像编译器”的一段：前面是类型和合法化，后面是寄存器和调度，只有这里同时看到 IR 的语义和 ISA 的编码。

看一个真实函数：

```llvm
define i64 @mul_add(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  %r = add i64 %m, 7
  ret i64 %r
}
```

`-O2` 的汇编是：

```asm
mul	a0, a0, a1
addi	a0, a0, 7
ret
```

但 `-O0` 的同一个函数会多出栈帧和 `sd`/`ld`。这不是“优化器把代码变快了”这么简单，而是 DAG 在 `-O0` 下没有被 combine，选指阶段看到的是更原始的节点，寄存器分配也只能照着 spill。

再看 `select`：

```asm
sel:
	andi	a2, a2, 1
	bnez	a2, .LBB1_2
	mv	a0, a1
.LBB1_2:
	ret
```

这里没有 `SELECT` 指令，也没有 `CMOV`。RISC-V 的基础 ISA 没有条件移动，`SELECT` 在选指阶段先变成 `Select_GPR_Using_CC_GPR` 伪指令，再在 `finalize-isel` 里展开成“分支 + PHI”的三角形控制流。只读最终汇编，会以为后端直接生成了分支；读 MIR，才能看到中间形态。

选指还有一个容易忽略的输入：常量。`mul_add` 的 `7` 直接进了 `ADDI`，但 `1311768467463790320` 这样的大常量会走常量池，汇编里出现 `.LCPI0_0: .quad ...`。为什么不是 `li` 展开成八条指令？因为代码生成阶段和汇编器阶段的策略不同。`PseudoLI` 的注释写得很清楚：它只在 AsmParser 里使用，并且“size is 32 because up to 8 32-bit instructions are needed”。

所以这一章的价值在于：能把“最终汇编的一行”对应回“DAG 的一个节点”和“TableGen 的一条 pattern”。做不到这一点，遇到 `Cannot select` 只能猜。

## 主线

### 15.1 从 IR 到 DAG：节点、chain、glue

SelectionDAG 的节点有两类值：普通值和特殊值。普通值有类型，例如 `i64`、`f64`、`<4 x i32>`；特殊值有三种常见角色：

- `chain`：类型是 `MVT::Other`，把所有有副作用的操作串起来。`load`、`store`、`call`、`br` 都吃一个 chain，再产出一个新 chain。
- `glue`：把若干节点粘在一起，禁止调度器在它们之间插入其他指令。调用序列、`vsetvli` 和它前面的向量指令常用 glue。
- `flag`：条件码或异常标志，例如 `SETCC` 的结果、浮点异常标志。

一个 store 后面跟一个 load，DAG 上大致是：

```text
EntryToken -> ... -> Store(chain, ptr, value) -> Load(chain, ptr) -> ...
```

如果两者地址相同、类型相同、中间没有别名写入，DAGCombine 可以把 `Load` 的 chain 直接接到 `Store` 之前的 chain，把 load 的值替换成 store 的值。这就是 `ldst` 函数在 MIR 里只剩 `SD` 而没有 `LD` 的原因：

```text
%1:gpr = COPY $x11
%0:gpr = COPY $x10
SD %1, %0, 0 :: (store (s64) into %ir.p)
$x10 = COPY %1
PseudoRET implicit $x10
```

`SD` 的第三个操作数是偏移 0，`:: (store (s64) into %ir.p)` 是 MachineMemOperand 的注释。没有 `LD` 不是“漏了”，而是 store-to-load forwarding 在 DAG 层完成了。

`chain` 还解释了为什么 `caller` 的 MIR 里有 `ADJCALLSTACKDOWN`/`ADJCALLSTACKUP`：

```text
ADJCALLSTACKDOWN 0, 0, implicit-def dead $x2, implicit $x2
%1:gpr = ADDI $x0, 3
PseudoCALL target-flags(riscv-call) @mul_add, csr_ilp32d_lp64d, ...
ADJCALLSTACKUP 0, 0, implicit-def dead $x2, implicit $x2
```

`ADJCALLSTACKDOWN 0` 表示这个调用不需要在栈上额外分配参数区（参数都在寄存器里），但它仍然要更新 `sp` 的模型，保证调用前后栈指针一致。`PseudoCALL` 是伪指令，最终由 MC 层展开成调用序列。

### 15.2 用 `-stop-after=riscv-isel` 看选指结果

`llc` 的 `-stop-after` 可以在指定 pass 后停下并打印 MIR。选指阶段的 pass 名是 `riscv-isel`：

```powershell
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 `
  -stop-after=riscv-isel -simplify-mir basic.ll -o after-isel.mir
```

`sel` 函数在 `after-isel.mir` 里的核心是：

```text
%2:gpr = COPY $x12
%1:gpr = COPY $x11
%0:gpr = COPY $x10
%3:gpr = ANDI %2, 1
%4:gpr = Select_GPR_Using_CC_GPR killed %3, $x0, 1, %0, %1
```

逐行读：

- 三个 `COPY` 把物理参数寄存器 `$x10`/`$x11`/`$x12` 复制到虚拟寄存器 `%0`/`%1`/`%2`。`%0` 是 `a`，`%1` 是 `b`，`%2` 是条件 `c`。
- `ANDI %2, 1` 把 `i1` 条件截到最低位。IR 的 `i1` 在寄存器里可能高位不干净，选指前要显式 `andi`。
- `Select_GPR_Using_CC_GPR` 是一个伪指令，操作数是：条件寄存器 `%3`、比较对象 `$x0`、条件码 `1`、真值 `%0`、假值 `%1`。它不是最终指令，`usesCustomInserter = 1` 会在后面的 pass 里展开。

这个伪指令的定义在 `RISCVInstrInfo.td` 的 `SelectCC_GPR_rrirr` 里：

```tablegen
multiclass SelectCC_GPR_rrirr<DAGOperand valty, ValueType vt,
                              ValueType cmpvt = XLenVT> {
  let usesCustomInserter = 1 in
  def _Using_CC_GPR : Pseudo<(outs valty:$dst),
    (ins GPR:$lhs, GPR:$rhs, cond_code:$cc,
     valty:$truev, valty:$falsev),
    [(set valty:$dst,
      (riscv_selectcc (cmpvt GPR:$lhs), GPR:$rhs, riscv_cond:$cc,
                      (vt valty:$truev), valty:$falsev))]>;
}
```

`riscv_selectcc` 是 TableGen 里的模式操作符，不是 IR 指令。它把 IR 的 `select` + `setcc` 组合成一个 DAG 节点，再由 `Select_GPR_Using_CC_GPR` 接管。`defm Select_GPR : SelectCC_GPR_rrirr<GPR, XLenVT>;` 负责生成具体的 `Select_GPR_Using_CC_GPR`。

### 15.3 `-stop-after=finalize-isel`：伪指令展开成 CFG

再跑一次，停在 `finalize-isel`：

```powershell
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 `
  -stop-after=finalize-isel -simplify-mir basic.ll -o final-isel.mir
```

`sel` 的核心变成：

```text
%3:gpr = ANDI %2, 1
BNE %3, $x0, %bb.2

bb.1.entry:

bb.2.entry:
  %4:gpr = PHI %0, %bb.0, %1, %bb.1
  $x10 = COPY %4
  PseudoRET implicit $x10
```

`Select_GPR_Using_CC_GPR` 不见了，换成了 `BNE` + 两个基本块 + `PHI`。这个展开由 `RISCVTargetLowering::EmitInstrWithCustomInserter` 完成：

```cpp
case RISCV::Select_GPR_Using_CC_GPR:
case RISCV::Select_FPR32_Using_CC_GPR:
...
  return emitSelectPseudo(MI, BB, Subtarget);
```

`emitSelectPseudo` 在 `RISCVISelLowering.cpp` 里构造三角形控制流：一个分支跳到真值块，另一个落到假值块，最后在汇合块用 `PHI` 合并。MIR 里的 `bb.1` 是空块，`bb.2` 是汇合块；`PHI %0, %bb.0, %1, %bb.1` 表示从入口来取值 `%0`，从 `bb.1` 来取值 `%1`。

这也解释了为什么 `-stop-after=riscv-isel` 和 `-stop-after=finalize-isel` 的输出差这么多：前者是“选指完成但伪指令还在”，后者是“伪指令展开、CFG 已经改变”。

`-simplify-mir` 只影响打印，不影响代码。同一份 MIR，带 `-simplify-mir` 是 4184 字节，不带是 8593 字节。差的不是指令，而是 `-simplify-mir` 省略了默认字段、空 liveins、冗余的寄存器类信息。读 MIR 时建议先带 `-simplify-mir`，需要查某个字段时再去掉。

### 15.4 TableGen pattern 与 C++ `SelectNodeTo`

RISC-V 的选指入口是 `RISCVDAGToDAGISel::Select`。它先检查节点是否已经是机器指令：

```cpp
void RISCVDAGToDAGISel::Select(SDNode *Node) {
  if (Node->isMachineOpcode()) {
    Node->setNodeId(-1);
    return;
  }
  unsigned Opcode = Node->getOpcode();
  ...
  switch (Opcode) {
  case ISD::Constant: { ... }
  case ISD::ConstantFP: { ... }
  ...
  }
  SelectCode(Node);
}
```

`SelectCode(Node)` 是 TableGen 生成的匹配函数。它按 opcode 和类型查表，找到 pattern 后调用 `SelectNodeTo` 把 `ISD` 节点替换成机器节点。`SelectionDAG.h` 里有一组重载：

```cpp
SDNode *SelectNodeTo(SDNode *N, unsigned MachineOpc, EVT VT);
SDNode *SelectNodeTo(SDNode *N, unsigned MachineOpc, EVT VT, SDValue Op1);
...
SDNode *SelectNodeTo(SDNode *N, unsigned MachineOpc, SDVTList VTs,
                     ArrayRef<SDValue> Ops);
```

`MUL` 的 pattern 是一个很短的例子：

```tablegen
class PatGprGpr<SDPatternOperator OpNode, RVInst Inst, ValueType vt = XLenVT>
    : Pat<(vt (OpNode (vt GPR:$rs1), (vt GPR:$rs2))),
          (Inst GPR:$rs1, GPR:$rs2)>;

def : PatGprGpr<mul, MUL>;
```

第一行定义“两个 GPR 操作数、结果同类型”的通用 pattern；第二行把它实例化成 `mul` 到 `MUL` 的映射。生成物 `RISCVGenDAGISel.inc` 里对应：

```text
// Src: (mul:{ *:[i32] m1:[i64] } GPR:{ *:[i32] m1:[i64] }:$rs1, GPR:{ *:[i32] m1:[i64] }:$rs2) - Complexity = 3
// Dst: (MUL:{ *:[i32] m1:[i64] } GPR:{ *:[i32] m1:[i64] }:$rs1, GPR:{ *:[i32] m1:[i64] }:$rs2)
```

匹配项前面还有一个 predicate：

```text
OPC_CheckPatternPredicate, 152, // (Subtarget->hasStdExtZmmul())
```

也就是说，`MUL` 这条 pattern 只在 `Zmmul` 存在时可用。没有 `Zmmul` 时，`MUL` 在合法化阶段就被 `Expand` 成 libcall（第 14 章的 `tail __muldi3`），不会走到这条 pattern。

`Select_GPR_Using_CC_GPR` 也在生成物里：

```text
// Dst: (Select_GPR_Using_CC_GPR:{ *:[i32] m1:[i64] } GPR:...:$lhs, X0:...,
//       (CCtoRISCVCC:{ *:[i32] m1:[i64] } ?:...:$cc), GPR:...:$truev, GPR:...:$falsev)
```

`CCtoRISCVCC` 是 TableGen 里的转换操作符，把 `ISD::CondCode` 映射成 RISC-V 的条件码。`X0` 表示比较对象是零寄存器；`select` 的条件通常来自 `setcc`，所以比较对象可以是任意 GPR 或立即数。

C++ 和 TableGen 的分工可以这样记：

- TableGen 负责“结构匹配”：什么 opcode、什么类型、什么操作数形态、什么 predicate，匹配后生成什么机器指令。
- C++ `Select` 负责“TableGen 表达不了的事情”：常量物化、地址折叠、伪指令、需要多步构造的复杂模式。
- `SelectNodeTo` 是两者共同的出口：不管谁匹配成功，最后都调用它替换 DAG 节点。

### 15.5 地址选择、常量选择与 `PseudoLI`

地址选择在 `SelectAddrRegImm` 里：

```cpp
bool RISCVDAGToDAGISel::SelectAddrRegImm(SDValue Addr, SDValue &Base,
                                         SDValue &Offset) {
  if (SelectAddrFrameIndex(Addr, Base, Offset))
    return true;
  ...
  if (Addr.getOpcode() == RISCVISD::ADD_LO) { ... }
  if (CurDAG->isBaseWithConstantOffset(Addr)) {
    int64_t CVal = cast<ConstantSDNode>(Addr.getOperand(1))->getSExtValue();
    if (isInt<12>(CVal)) {
      Base = Addr.getOperand(0);
      ...
    }
  }
}
```

顺序很重要：

1. 先试 frame index：栈槽地址变成 `sp`/`s0` 加偏移。
2. 再试 `RISCVISD::ADD_LO`：全局地址的 `%lo` 部分已经是一个 DAG 节点，可以折叠进 load/store 的 12 位偏移。
3. 最后试“基址 + 常数偏移”，只有 `isInt<12>(CVal)` 才折叠。超过 12 位的偏移不能直接进 `LD`/`SD`，要先用 `selectImm` 物化到临时寄存器。

常量选择在 `Select` 的 `ISD::Constant` 分支：

```cpp
int64_t Imm = ConstNode->getSExtValue();
...
ReplaceNode(Node, selectImm(CurDAG, DL, VT, Imm, *Subtarget).getNode());
```

`selectImm` 调用 `RISCVMatInt::generateInstSeq` 生成一条指令序列，再用 `selectImmSeq` 把序列变成 DAG 节点。`selectImmSeq` 会根据序列长度决定用 `LUI`+`ADDI`、`LUI`+`ADDIW`、`SLLI` 组合，还是干脆放常量池。

`retbig` 函数返回 `1311768467463790320`，MIR 是：

```text
%0:gpr = LUI target-flags(riscv-hi) %const.0
%1:gpr = LD killed %0:gpr, target-flags(riscv-lo) %const.0 :: (load (s64) from constant-pool)
```

汇编是：

```asm
.section	.rodata.cst8,"aM",@progbits,8
.LCPI0_0:
	.quad	1311768467463790320
...
	lui	a0, %hi(.LCPI0_0)
	ld	a0, %lo(.LCPI0_0)(a0)
```

这不是 `li` 展开，而是常量池加载。`RISCVMatInt::generateInstSeq` 对 64 位常量的成本估计超过阈值时，后端选择常量池；如果用户显式写 `li`，那是汇编器的事。

`llvm-mc` 里的 `li a0, 1311768467463790320` 会展开成八条：

```asm
lui	a0, 583
addi	a0, a0, -1875
slli	a0, a0, 14
addi	a0, a0, -947
slli	a0, a0, 12
addi	a0, a0, 1511
slli	a0, a0, 13
addi	a0, a0, -272
```

`PseudoLI` 的定义在 `RISCVInstrInfo.td`：

```tablegen
// Note that the size is 32 because up to 8 32-bit instructions are needed to
// generate an arbitrary 64-bit immediate. However, the size does not really
// matter since PseudoLI is currently only used in the AsmParser where it gets
// expanded to real instructions immediately.
let hasSideEffects = 0, mayLoad = 0, mayStore = 0, Size = 32,
    isCodeGenOnly = 0 in
def PseudoLI : Pseudo<(outs GPR:$rd), (ins ixlenimm_li:$imm), [],
                      "li", "$rd, $imm">;
```

`AsmParser/RISCVAsmParser.cpp` 的 `case RISCV::PseudoLI` 调用 `emitLoadImm`，后者用 `RISCVMatInt::generateMCInstSeq` 生成 MC 指令序列。代码生成不依赖 `PseudoLI`，所以它在 MIR 里通常不出现；如果你在 MIR 里看到 `PseudoLI`，先确认是不是从汇编输入或某个特殊路径来的。

全局地址的物化走 `PseudoMovAddr`：

```text
%0:gpr = PseudoMovAddr target-flags(riscv-hi) @g, target-flags(riscv-lo) @g
```

最终：

```asm
lui	a0, %hi(g)
addi	a0, a0, %lo(g)
ret
```

`PseudoMovAddr` 是一个伪指令，后面会展开成 `LUI` + `ADDI`。它比直接生成两条机器指令更容易让后面的优化识别“这是一对 hi/lo”，也方便 relax。

### 15.6 `-O0` 与 `-O2` 的 DAG 差异

同一个 `sel`，`-O0` 的汇编：

```asm
sel:
	addi	sp, sp, -16
	mv	a3, a2
	mv	a2, a1
	sd	a2, 0(sp)
	mv	a1, a0
	andi	a0, a3, 1
	sd	a1, 8(sp)
	bnez	a0, .LBB1_2
	ld	a0, 0(sp)
	sd	a0, 8(sp)
.LBB1_2:
	ld	a0, 8(sp)
	addi	sp, sp, 16
	ret
```

`-O2` 的汇编：

```asm
sel:
	andi	a2, a2, 1
	bnez	a2, .LBB1_2
	mv	a0, a1
.LBB1_2:
	ret
```

差异来自两层：

- DAGCombine 在 `-O2` 会把 `select` 的两个值直接送进伪指令，不需要先存到栈上；`-O0` 的 DAG 保留了更多原始节点，寄存器分配时把值 spill 到 16 字节栈帧。
- `-O2` 的伪指令展开后，`PHI` 和 `COPY` 被寄存器合并消掉，只剩 `mv a0, a1`；`-O0` 的 `PHI` 通过栈槽传递，出现 `ld`/`sd` 往返。

注意 `-O0` 不是“不做选指”，它仍然走 SelectionDAG 和同样的 pattern；它只是关掉了大部分 DAGCombine 和机器层优化。MIR 里仍然能看到 `Select_GPR_Using_CC_GPR`，只是周围多了 spill。

### 15.7 选指失败的诊断路径

选指失败最常见的错误是：

```text
LLVM ERROR: Cannot select: intrinsic %llvm.coro.frame
```

这条信息来自 `SelectionDAGISel`，不是 Legalizer。处理顺序：

1. 先确认 IR 是否已经跑过必要的前置 pass。`llvm.coro.frame` 需要协程 lowering；直接把未处理的 intrinsic 丢给 llc，合法化器认为它合法，选指找不到 pattern。
2. 用 `-print-after=riscv-isel` 看崩溃前最后一个成功的函数。如果是多函数模块，先拆成单函数。
3. 用 `-stop-after=finalize-isel` 看能否停在 MIR；如果崩溃发生在选指内部，这个命令也会失败。
4. 用 `-mattr` 确认扩展是否开启。有些 pattern 带 predicate，扩展没开时节点不会匹配，最终以 `Cannot select` 或 libcall 的形式表现。
5. 最后才怀疑 TableGen pattern 缺失。

另一个常见错误是编译器的调试选项不可用：

```text
llc.exe: Unknown command line argument '-debug-only=isel'.
llc.exe: Did you mean '--debug-pass=isel'?
```

本机的 23.1.4 是 Optimized build，`-debug-only` 没有编进去。可用的替代是 `-print-after=riscv-isel`、`-print-after=finalize-isel`、`-debug-pass=Structure`。不要因为 `-debug-only` 报错就认为 llc 坏了。

还有一种错误发生在 `Custom` lowering 里：

```text
LLVM ERROR: Unimplemented RISCVTargetLowering::LowerOperation Case
```

它说明某个节点被标成 `Custom`，但 `LowerOperation` 的 `switch` 没有实现。它和 `Cannot select` 的区别是：错误发生在选指之前的 lowering 回调，不在 pattern 匹配阶段。

## 可跑实验

所有实验都可以通过统一入口跑：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'D:\Learning-with-Claude\compilers\riscv_backend\code\tools\run.ps1' 'labs\15-selection-dag.ps1'
```

脚本 `code/labs/15-selection-dag.ps1` 会生成 `basic.ll`，输出 `after-isel.mir`、`final-isel.mir`、`final-full.mir`、`O0.s`、`O2.s` 和 `isel-print.txt`。下面是拆开的命令。

### 实验 1：两阶段 MIR 与 `-simplify-mir`

```powershell
$LLVM = 'D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin'
$w = Join-Path $env:TEMP 'riscv-course-lab15'
New-Item -ItemType Directory -Force -Path $w | Out-Null
Set-Location $w

@"
target triple = "riscv64-unknown-elf"
define i64 @mul_add(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  %r = add i64 %m, 7
  ret i64 %r
}
define signext i32 @sel(i32 signext %a, i32 signext %b, i1 %c) {
  %s = select i1 %c, i32 %a, i32 %b
  ret i32 %s
}
"@ | Set-Content -Encoding ascii basic.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 `
  -stop-after=riscv-isel -simplify-mir basic.ll -o after-isel.mir
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 `
  -stop-after=finalize-isel -simplify-mir basic.ll -o final-isel.mir
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 `
  -stop-after=finalize-isel basic.ll -o final-full.mir

Get-Content after-isel.mir | Select-String 'name: *sel' -Context 0,30
Get-Content final-isel.mir | Select-String 'name: *sel' -Context 0,30
"full=$((Get-Item final-full.mir).Length) simplified=$((Get-Item final-isel.mir).Length)"
```

预期：`after-isel.mir` 里有 `Select_GPR_Using_CC_GPR`，`final-isel.mir` 里有 `BNE`、两个空/非空基本块和 `PHI`。两个文件大小分别是 8593 和 4184 字节（本机实测，可能随路径名微调）。

### 实验 2：`-O0` 与 `-O2` 的 DAG 差异

```powershell
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O0 basic.ll -o O0.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 basic.ll -o O2.s
Get-Content O0.s | Select-String '^sel:' -Context 0,18
Get-Content O2.s | Select-String '^sel:' -Context 0,10
```

预期：`O0.s` 有 16 字节栈帧和 `sd`/`ld`；`O2.s` 只有 `andi`、`bnez`、`mv`、`ret`。如果把 `-O0` 换成 `-O1`，通常已经能看到大部分 DAGCombine 的效果；`-O0` 是观察“未优化 DAG”的窗口。

### 实验 3：`-print-after`、常量池与地址物化

```powershell
@"
target triple = "riscv64-unknown-elf"
define i64 @retbig() {
  ret i64 1311768467463790320
}
@g = external global i32
define ptr @addr() {
  ret ptr @g
}
define double @fp(double %a, double %b) {
  %m = fmul double %a, %b
  %r = fadd double %m, 1.5
  ret double %r
}
"@ | Set-Content -Encoding ascii extra.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+a,+f,+d' -O2 `
  -print-after=riscv-isel extra.ll -o extra.s 2> extra-print.txt
Get-Content extra-print.txt | Select-String 'Machine code for function|LUI|LD |PseudoMovAddr|FMUL|FADD|SLLI|ADDI \$x0' |
  Select-Object -First 40
Get-Content extra.s | Select-String '^retbig:|^addr:|^fp:|.LCPI0_0|lui|ld|fmul|fadd'
```

预期：`retbig` 的 MIR 是 `LUI` + `LD`，汇编里出现 `.LCPI0_0: .quad 1311768467463790320`；`addr` 的 MIR 是 `PseudoMovAddr`，汇编是 `lui` + `addi`；`fp` 的 MIR 里有 `FMUL_D`、`ADDI $x0, 2047`、`SLLI ..., 51`、`FADD_D`。`2047 << 51` 就是 `1.5` 的 IEEE754 位模式。

再看汇编器的 `li`：

```powershell
'li a0, 1311768467463790320' | & "$LLVM\llvm-mc.exe" -triple=riscv64 '-mattr=+m' -show-encoding
```

预期：八条指令，从 `lui a0, 583` 到 `addi a0, a0, -272`。这是 MC 层的 `PseudoLI` 展开，不是 llc 的常量池路径。

### 实验 4：边界与失败

`-debug-only` 在 Optimized build 不可用：

```powershell
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 -debug-only=isel basic.ll -o NUL
```

预期：`Unknown command line argument '-debug-only=isel'`，退出码 1。改用 `-print-after=riscv-isel`。

`Cannot select`：

```powershell
@"
target triple = "riscv64-unknown-elf"
define ptr @f(ptr %hdl) {
  %r = call ptr @llvm.coro.frame()
  ret ptr %r
}
declare ptr @llvm.coro.frame()
"@ | Set-Content -Encoding ascii coro.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+f,+d' -O2 coro.ll -o coro.s
```

预期：`LLVM ERROR: Cannot select: intrinsic %llvm.coro.frame`，退出码 `-1073741795`。这个例子的价值是让你看到：选指失败不一定是 pattern 写错，也可能是前置 pass 没跑。

## 源码落点

- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`
  - `RISCVDAGToDAGISel::Select`：总入口，先处理 `ISD::Constant`/`ConstantFP`，再调用 `SelectCode`。
  - `selectImm`：用 `RISCVMatInt::generateInstSeq` 把常量变成指令序列。
  - `selectImmSeq`：把 `RISCVMatInt::InstSeq` 变成 DAG 节点。
  - `SelectAddrRegImm`：frame index、`RISCVISD::ADD_LO`、基址+12 位偏移的地址折叠。
  - `SelectAddrFrameIndex`：栈槽地址选择。
  - `selectConstantAddr`：全局地址的 `%hi`/`%lo` 物化。
  - `PreprocessISelDAG` / `PostprocessISelDAG`：选指前后的 DAG 预处理。
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`
  - `class PatGprGpr`：两个 GPR 操作数的通用 pattern。
  - `PseudoLI`：注释说明它只在 AsmParser 使用，`Size = 32`。
  - `multiclass SelectCC_GPR_rrirr`：`usesCustomInserter = 1` 的 `Select_GPR_Using_CC_GPR`。
  - `class RVSDNode`：`RISCVISD::` 自定义节点的 TableGen 定义方式。
- `llvm/lib/Target/RISCV/RISCVInstrInfoM.td`
  - `def : PatGprGpr<mul, MUL>;`：`mul` 到 `MUL` 的映射。
- `.scratch/riscv-tblgen-ev/RISCVGenDAGISel.inc`
  - `TARGET_VAL(RISCV::MUL)` 附近的匹配项：`Src: (mul ...)`、`Dst: (MUL ...)`，predicate 是 `hasStdExtZmmul`。
  - `TARGET_VAL(RISCV::Select_GPR_Using_CC_GPR)` 附近的匹配项：`CCtoRISCVCC` 条件码转换。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`
  - `RISCVTargetLowering::lowerSELECT`：`select` 的 lowering 入口。
  - `lowerSelectToBinOp`：能转成二元运算的 `select` 快捷路径。
  - `emitSelectPseudo`：把 `Select_GPR_Using_CC_GPR` 展开成 `BNE`/`BEQ` + `PHI`。
  - `EmitInstrWithCustomInserter`：`usesCustomInserter` 伪指令的分发点。
- `llvm/include/llvm/CodeGen/SelectionDAG.h`
  - `SelectNodeTo` 的一组重载：选指匹配成功后替换节点的统一出口。
- `llvm/include/llvm/CodeGen/SelectionDAGNodes.h`
  - `ISD::NodeType`：`EntryToken`、`TokenFactor`、`ADD`、`MUL`、`LOAD`、`STORE` 等节点编号。
- `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.cpp`
  - `RISCVMatInt::generateInstSeq`：常量物化的成本模型和指令序列生成。

## 常见坑

1. **把 `-stop-after=riscv-isel` 的输出当成最终指令。** 这里还有 `Select_GPR_Using_CC_GPR`、`PseudoCALL`、`PseudoMovAddr` 等伪指令。要看展开后的形态，用 `-stop-after=finalize-isel` 或直接看汇编。

2. **以为 `-simplify-mir` 会改变代码。** 它只省略默认字段和空 liveins，MIR 的指令序列不变。大小差异（8593 对 4184 字节）来自文本，不是代码。

3. **在 `-O0` 下找 `Select_GPR_Using_CC_GPR` 找不到。** `-O0` 仍然会选指，但周围有 spill，`Select_GPR_Using_CC_GPR` 仍然存在；如果你只 `Select-String 'select'`，可能被大小写或函数名干扰。按 `name: *sel` 定位函数体。

4. **在 PowerShell 里不加引号地传 `-mattr=+m,+a,+f,+d`。** 逗号会被解析成数组分隔符，llc 收到的参数不完整。所有含逗号的 `-mattr` 都加单引号。

5. **把 `PseudoLI` 当成 llc 会生成的伪指令。** 23.1.4 的 `PseudoLI` 注释明确说它只在 AsmParser 使用。llc 对大常量走 `RISCVMatInt` 或常量池；`llvm-mc` 的 `li` 才会展开成八条。

6. **把 `Cannot select` 归因于 TableGen。** 先确认前置 pass 和扩展 predicate。`llvm.coro.frame` 是前置 pass 缺失，不是 pattern 缺失；`MUL` 在无 `Zmmul` 时根本到不了 pattern，因为合法化已经把它变成 libcall。

7. **用 `-debug-only=isel` 调试 Optimized build。** 该选项没有编进去，会直接报 `Unknown command line argument`。用 `-print-after=riscv-isel` 和 `-debug-pass=Structure` 替代。

## 自我检查

- [ ] 能画出 `mul_add` 从 IR 到 `MUL`/`ADDI` 的 DAG，并指出 chain 和 glue 各在哪里。
- [ ] 能解释 `ldst` 的 MIR 为什么只有 `SD` 没有 `LD`。
- [ ] 能说出 `-stop-after=riscv-isel` 与 `-stop-after=finalize-isel` 之间新增了哪些 pass、哪些伪指令被展开。
- [ ] 能在 `RISCVInstrInfoM.td` 找到 `PatGprGpr<mul, MUL>`，并在 `RISCVGenDAGISel.inc` 找到对应的 `Src`/`Dst`。
- [ ] 能解释 `Select_GPR_Using_CC_GPR` 的五个操作数分别是什么，以及它为什么带 `usesCustomInserter = 1`。
- [ ] 能说出 `SelectAddrRegImm` 的三级匹配顺序，以及为什么 12 位偏移是边界。
- [ ] 能解释 `PseudoLI` 为什么不出现在 llc 的 MIR 里，以及 `llvm-mc` 的 `li` 展开成几条指令。
- [ ] 能用 `-print-after=riscv-isel` 找到 `1.5` 常量的 `ADDI` + `SLLI` + `FMV_D_X` 序列。

## 延伸阅读

- `llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`：选指主逻辑、常量物化、地址折叠。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`：`lowerSELECT`、`emitSelectPseudo`、`EmitInstrWithCustomInserter`。
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`：`PatGprGpr`、`SelectCC_GPR_rrirr`、`PseudoLI`、`RVSDNode`。
- `llvm/lib/Target/RISCV/RISCVInstrInfoM.td`：`mul`/`div`/`rem` 的 pattern。
- `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMatInt.cpp`：常量序列生成和成本模型。
- `llvm/include/llvm/CodeGen/SelectionDAG.h`：`SelectNodeTo` 重载。
- `llvm/include/llvm/CodeGen/SelectionDAGNodes.h`：`ISD` 节点类型和 `SDNode` 结构。
- `llvm/test/CodeGen/RISCV/`：同版本的 lit 用例，可按 `select`、`mul`、`constant` 搜索；本机 sparse clone 只含 `lib/Target/RISCV`，测试目录需要完整源码树。

[上一章：合法化](./14-legalization.md) | [下一章：自定义 lowering 与 intrinsic](./16-custom-lowering-and-intrinsics.md)