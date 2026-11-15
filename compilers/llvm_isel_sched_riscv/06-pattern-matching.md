# Ch 6 — Pattern matching：从 `.td` 到 `Select()`

> **目标**：看懂一条 TableGen `Pat` 如何变成 matcher table，如何被 `SelectCode` 执行，以及匹配失败时 `-debug-only=isel` 的日志在说什么。学完这章，你能从 `(add (shl x, 3), y)` 追到 `SH3ADD`，能解释 `Skipped scope entry (due to false predicate)`，也能判断一个问题该改 `.td` pattern 还是该改 `RISCVISelDAGToDAG.cpp::Select()`。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章用 `idx.ll` 和 `RISCVInstrInfoZb.td` / `RISCVISelDAGToDAG.cpp`。生成的 matcher table 在构建目录的 `lib/Target/RISCV/RISCVGenDAGISel.inc`，它是构建产物，行号会随源码和构建变化。

## 为什么需要这个？

上一章结束时，DAG 已经被 combine 和 legalize 改成了 target 喜欢的形状。现在的问题是：**这张 DAG 怎么变成具体的 MachineInstr？**

表层答案是一句话：TableGen 把 `.td` 里的 `Pat` 编译成 matcher，`SelectCode` 用 matcher 匹配 DAG，匹配成功就 `MorphNodeTo` 成目标指令。但面试里真正会问的是：

- 「为什么 `(add (shl x, 3), y)` 选成 `sh3add`，而不是 `slli + add`？」
- 「为什么同一个函数，加 `-mattr=+zba` 之后指令变了？」
- 「`Match failed` 和 `Skipped scope entry` 有什么区别？」
- 「什么时候需要写 C++ 的 `Select()`，什么时候写 `.td` 就够了？」

这一章把 pattern matching 拆成三层：

```text
.td 源码
  -> TableGen 生成的 matcher table（编译期）
  -> DAGISelMatcher 解释执行（运行期）
  -> 匹配失败时回落到 RISCVDAGToDAGISel::Select()（C++）
```

## 先建立直觉

把 pattern matching 想成一次「带作用域的树匹配」：

```text
源 DAG                          matcher table
--------                        -------------
add                             Scope
 ├─ shl                           ├─ CheckOpcode ADD
 │   ├─ GPR x                    ├─ MoveParent / CheckOpcode SHL
 │   └─ 3                        ├─ CheckChild1Integer 3
 └─ GPR y                        ├─ CheckPatternPredicate HasZba
                                 └─ MorphNodeTo SH3ADD
```

匹配器从根节点开始，按 table 里的指令逐条检查：

- `CheckOpcode ADD`：当前节点是不是 `add`。
- `MoveParent` / `CheckOpcode SHL`：第一个操作数是不是 `shl`。
- `CheckChild1Integer 3`：`shl` 的第二个操作数是不是常量 3。
- `CheckPatternPredicate`：当前 subtarget 是否满足 `HasStdExtZba`。
- `MorphNodeTo SH3ADD`：把匹配到的子树替换成 `SH3ADD`。

如果某一步失败，matcher 会跳到 table 里的下一个候选（`Continuing at ...`）。如果整个 table 都失败，才进入 C++ 的 `Select()`。

## Pattern 的基本结构

看 `RISCVInstrInfoZb.td` 里的 `ShxAddPat`：

```tablegen
multiclass ShxAddPat<int i, Instruction shxadd> {
  def : Pat<(XLenVT (riscv_add_like_non_imm12 (shl GPR:$rs1, (XLenVT i)), GPR:$rs2)),
            (shxadd GPR:$rs1, GPR:$rs2)>;
  def : Pat<(XLenVT (riscv_shl_add GPR:$rs1, (XLenVT i), GPR:$rs2)),
            (shxadd GPR:$rs1, GPR:$rs2)>;

  defvar pat = !cast<ComplexPattern>("sh"#i#"add_op");
  def : Pat<(XLenVT (riscv_add_like_non_imm12 pat:$rs1, GPR:$rs2)),
            (shxadd pat:$rs1, GPR:$rs2)>;
}
```

一行一行读：

- `multiclass ShxAddPat<int i, Instruction shxadd>`：一个模板，`i` 是移位数，`shxadd` 是目标指令。
- `def : Pat<源模式, 目标模式>`：这是 pattern 的核心。左边是 DAG 形状，右边是生成的指令。
- `GPR:$rs1` / `GPR:$rs2`：命名操作数。`GPR` 是寄存器类约束，`$rs1` 是名字，右边用同名变量引用。
- `(XLenVT i)`：把 `i` 作为常量嵌入模式；`XLenVT` 是 target 的整数类型（RV64 是 `i64`）。
- `riscv_add_like_non_imm12`：一个 `PatFrag`，匹配 `add` 或「位不相交的 `or`」，但排除 12 位立即数。
- 第三个 `Pat` 用 `ComplexPattern`：`sh3add_op` 这类 C++ 回调可以匹配更复杂的地址/移位组合。

`let Predicates = [HasStdExtZba] in { ... }` 把整组 pattern 包在 feature gate 里：

```tablegen
let Predicates = [HasStdExtZba] in {
foreach i = {1,2,3} in {
  defvar shxadd = !cast<Instruction>("SH"#i#"ADD");
  defm : ShxAddPat<i, shxadd>;
}
}
```

`foreach` 生成三组 pattern：`SH1ADD`、`SH2ADD`、`SH3ADD`。`!cast<Instruction>("SH"#i#"ADD")` 把字符串拼成指令记录名。最终 `.td` 里只有一份模板，TableGen 展开成三份 matcher。

## 真实匹配：`idx.ll` 的 `SH3ADD`

`idx.ll`：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @idx(ptr %p, i64 %i) {
  %q = getelementptr i64, ptr %p, i64 %i
  %v = load i64, ptr %q
  ret i64 %v
}
```

加 `+zba` 跑：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zba -O2 \
  -debug-only=isel idx.ll -o /dev/null
```

初始 DAG（节选）：

```text
Initial selection DAG: %bb.0 'idx:'
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64,ch = CopyFromReg t0, Register:i64 %1
  t6: i64 = shl t4, Constant:i64<3>
  t7: i64 = add t2, t6
  t10: i64,ch = load<(load (s64) from %ir.q)> t0, t7, poison:i64
```

`t7` 的形状正好是：

```text
add(t2, shl(t4, 3))
```

也就是 `base + index * 8`。`ShxAddPat<3, SH3ADD>` 的第一条 pattern 就是匹配这个形状。

`-debug-only=isel` 的日志（节选）：

```text
ISEL: Starting selection on root node: t7: i64 = add t2, t6
ISEL: Starting pattern match
  Initial Opcode index to 24569
  Match failed at index 24574
  Continuing at 24744
  ...
  Skipped scope entry (due to false predicate) at index 27521, continuing at 27541
  Skipped scope entry (due to false predicate) at index 27542, continuing at 27562
  Morphed node: t7: i64 = SH3ADD t4, t2
ISEL: Match complete!
```

逐行解释：

- `Starting selection on root node: t7`：从 `t7` 开始选。`SelectCode` 通常从 DAG 的根往下递归，但每个节点都会走一次 pattern match。
- `Initial Opcode index to 24569`：matcher table 的起始偏移。这个数字是 TableGen 生成的，不同构建会变。
- `Match failed at index 24574`：某个候选在检查 opcode/操作数时失败。
- `Continuing at 24744`：跳到下一个候选，不是整个匹配失败。
- `Skipped scope entry (due to false predicate)`：进入一个 `Scope` 时，前面的 `CheckPatternPredicate` 为 false，整段候选被跳过。这里的 predicate 可能是 `HasStdExtZba`、`IsRV64` 等。
- `Morphed node: t7: i64 = SH3ADD t4, t2`：匹配成功，`t7` 被替换成 `SH3ADD`，操作数是 `t4`（index）和 `t2`（base）。
- `Match complete!`：这个节点选完了。

最终汇编：

```asm
idx:
	sh3add	a0, a1, a0
	ld	a0, 0(a0)
	ret
```

`sh3add a0, a1, a0` 的语义是 `a0 = (a1 << 3) + a0`，正好对应 `base + index * 8`。

## matcher table 里到底有什么

构建目录里的 `lib/Target/RISCV/RISCVGenDAGISel.inc` 是 TableGen 生成的 matcher table。它非常大（本机构建约 68 万行），但关键片段可读。`SH3ADD` 附近：

```text
/*  27359*/      OPC_CheckChild1Integer, 3,
/*  27361*/      OPC_CheckChild1TypeByHwMode0/*{(*:i32),(m1:i64)}*/,
/*  27362*/      OPC_MoveParent,
/*  27363*/      OPC_RecordChild1, // #2 = $rs2
/*  27364*/      OPC_CheckPredicateWithOperands, 2/*#Ops*/, 0, 2, 0,
                 // Predicate_riscv_add_like_non_imm12
/*  27369*/      OPC_CheckTypeByHwMode0/*{(*:i32),(m1:i64)}*/,
/*  27370*/      OPC_CheckPatternPredicate, 77, // (Subtarget->hasStdExtZba())
/*  27372*/      OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::SH3ADD), 0,
                 1/*#VTs*/, /*{(*:i32),(m1:i64)}*/0, 2/*#Ops*/, /*OperandList*/42,
                 // Src: (add:{ *:[i32] m1:[i64] }
                 //   (shl:{ *:[i32] m1:[i64] } GPR:$rs1, 3):$pred:5:x,
                 //   GPR:$rs2):$pred:5:y)
                 //   <<P:5:Predicate_riscv_add_like_non_imm12>>
                 //   - Complexity = 12
                 // Dst: (SH3ADD GPR:$rs1, GPR:$rs2)
```

这段 table 把 pattern 的每个检查步骤都摊开了：

1. `CheckChild1Integer, 3`：`shl` 的移位数必须是 3。
2. `CheckChild1Type`：类型必须是 `i32` 或 `i64`（由 HwMode 决定）。
3. `MoveParent`：从 `shl` 回到父节点 `add`。
4. `RecordChild1`：记录 `add` 的第二个操作数 `$rs2`。
5. `CheckPredicateWithOperands`：执行 `riscv_add_like_non_imm12` 的 C++ predicate。
6. `CheckPatternPredicate, 77`：检查 `hasStdExtZba()`。
7. `MorphNodeTo`：生成 `SH3ADD`，操作数是 `$rs1` 和 `$rs2`。

`Complexity = 12` 是 pattern 的匹配优先级。TableGen 会把复杂度高的 pattern 排在前面，避免被更通用的 `ADD` pattern 抢先匹配。`SH3ADD` 的 pattern 有多个节点和 predicate，复杂度高于单条 `ADD`，所以它先试。

注意：`-debug-only=isel` 里的 `index 27372` 和 `.inc` 里的 `/* 27372*/` 是同一个 table 偏移。这是把日志和生成代码对上的最短路径：

```bash
grep -n "Morphed node: t7: i64 = SH3ADD" /tmp/isel.log
# 然后在 RISCVGenDAGISel.inc 里找相邻的 index
```

## 没有 `Zba` 时发生什么

同一个 `idx.ll`，不加 `+zba`：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -O2 \
  -debug-only=isel idx.ll -o /dev/null
```

日志尾部：

```text
Match failed at index 30335
Continuing at 30353
Skipped scope entry (due to false predicate) at index 30365, continuing at 30378
Skipped scope entry (due to false predicate) at index 30379, continuing at 30391
Match failed at index 30363
Continuing at 30392
Morphed node: t7: i64 = ADD t2, t6
ISEL: Match complete!
```

最终汇编：

```asm
idx:
	slli	a1, a1, 3
	add	a0, a0, a1
	ld	a0, 0(a0)
	ret
```

对比两组日志的一个可量化差异：`t7` 的匹配过程中，`+zba` 版本有 18 次 `Skipped scope`，无 `Zba` 版本有 47 次。原因是 `SH3ADD` 所在的 scope 在无 `Zba` 时被整段跳过；有 `Zba` 时 predicate 通过，进入更深的候选，所以跳过的 scope 反而少了。

这说明 `Skipped scope entry` 不是错误，而是 matcher 的正常剪枝。看到几十行这种日志不要慌，先看最后一行 `Morphed node`。

## `ComplexPattern`：地址模式为什么不在 `.td` 里展开

`SH3ADD` 的 pattern 是纯结构匹配。load/store 的地址匹配更复杂，因为 RISC-V 需要把 `base + offset` 拆成两个操作数。`RISCVInstrInfo.td` 里：

```tablegen
def AddrRegImm : ComplexPattern<iPTR, 2, "SelectAddrRegImm">;

class LdPat<PatFrag LoadOp, RVInst Inst, ValueType vt = XLenVT,
            ValueType PtrVT = XLenVT>
    : Pat<(vt (LoadOp (AddrRegImm (PtrVT BasePtr:$rs1), simm12_lo:$imm12))),
          (Inst BasePtr:$rs1, simm12_lo:$imm12)>;

def : LdPat<load, LD, i64>;
```

读法：

- `ComplexPattern<iPTR, 2, "SelectAddrRegImm">`：这个 pattern 匹配一个指针，返回两个值（`base` 和 `offset`），具体逻辑在 C++ 的 `SelectAddrRegImm`。
- `AddrRegImm (PtrVT BasePtr:$rs1), simm12_lo:$imm12`：把两个返回值绑定到 `$rs1` 和 `$imm12`。
- `LdPat<load, LD, i64>`：匹配 64 位 `load`，生成 `LD`。

C++ 侧：

```cpp
bool RISCVDAGToDAGISel::SelectAddrRegImm(SDValue Addr, SDValue &Base,
                                         SDValue &Offset) {
  if (SelectAddrFrameIndex(Addr, Base, Offset))
    return true;

  if (CurDAG->isBaseWithConstantOffset(Addr)) {
    int64_t CVal = cast<ConstantSDNode>(Addr.getOperand(1))->getSExtValue();
    if (isInt<12>(CVal)) {
      Base = Addr.getOperand(0);
      ...
      Offset = CurDAG->getSignedTargetConstant(CVal, DL, VT);
      return true;
    }
  }

  if (Addr.getOpcode() == ISD::ADD && isa<ConstantSDNode>(Addr.getOperand(1))) {
    int64_t CVal = cast<ConstantSDNode>(Addr.getOperand(1))->getSExtValue();
    ...
    if (CVal >= -4096 && CVal <= 4094) {
      int64_t Adj = CVal < 0 ? -2048 : 2047;
      Base = SDValue(CurDAG->getMachineNode(RISCV::ADDI, DL, VT,
                                           Addr.getOperand(0),
                                           CurDAG->getSignedTargetConstant(Adj, DL, VT)), 0);
      Offset = CurDAG->getSignedTargetConstant(CVal - Adj, DL, VT);
      return true;
    }
  }
  ...
}
```

这段代码解释了三个现象：

1. `p[1]` 可以折叠成 `ld a0, 8(a0)`，因为 `isBaseWithConstantOffset` 且 `isInt<12>(8)`。
2. `p[i]` 不能折叠，因为 `i` 不是常量，`SelectAddrRegImm` 返回 false，`LdPat` 不匹配，最后由 `Select()` 里的 `ISD::LOAD` 分支处理成「先算地址，再 load」。
3. 偏移在 `[-4096, -2049]` 或 `[2048, 4094]` 时，`SelectAddrRegImm` 可以先生成一条 `ADDI`，把剩余偏移折进 load/store。这是一条 `ComplexPattern` 生成多条指令的例子，第 7 章会展开。

`SelectAddrRegImm` 在 `RISCVISelDAGToDAG.cpp` 约 3630 行。`AddrRegImm` 的定义在 `RISCVInstrInfo.td` 约 618 行，`LdPat` 约 2120 行。

## 什么时候需要 C++ 的 `Select()`

TableGen 适合「结构固定、用 DAG 形状能描述」的匹配。以下情况通常要写 `Select()`：

- 需要根据常量值生成不同指令序列（`ISD::Constant` → `LUI + ADDI`、`ADDI`、`li`）。
- 需要多结果节点、复杂副作用或 target-specific 伪指令。
- 需要访问 `Subtarget` 的 feature，而 predicate 无法表达。
- 需要生成一条伪指令，等 post-RA 再展开（`PseudoMovAddr`、`PseudoMovImm`）。

`RISCVDAGToDAGISel::Select(SDNode *Node)` 在 `RISCVISelDAGToDAG.cpp` 约 1178 行。它的结构是一个大 `switch (Opcode)`：

```cpp
void RISCVDAGToDAGISel::Select(SDNode *Node) {
  if (Node->isMachineOpcode()) {
    Node->setNodeId(-1);
    return;
  }

  unsigned Opcode = Node->getOpcode();
  switch (Opcode) {
  case ISD::Constant: {
    ...
    ReplaceNode(Node, selectImm(CurDAG, DL, VT, Imm, *Subtarget).getNode());
    return;
  }
  case ISD::SHL:
    ...
  case ISD::LOAD:
    ...
  }
}
```

执行顺序是：

```text
SelectCode(Node)
  -> 先跑 TableGen matcher
  -> 如果 matcher 返回 false，进入 RISCVDAGToDAGISel::Select(Node)
  -> Select() 里再按 opcode 处理
```

所以你在 `.td` 里找不到的指令，不一定没有 pattern；它可能在 `Select()` 里。反过来，`Select()` 里处理不了的节点，最后会触发 `Cannot select`。

## Pattern 的顺序、复杂度和优先级

TableGen 生成 matcher 时会排序候选。排序的关键因素：

1. **复杂度（Complexity）**：节点越多、predicate 越多，复杂度越高，越先试。`SH3ADD` 的 `Complexity = 12`，高于通用 `ADD`。
2. **`AddedComplexity`**：在 `.td` 里手动加权重，让某个 pattern 优先。
3. **`Predicates` / `Requires`**：feature gate；不满足时整个 scope 被跳过。
4. **`hasOneUse` / `oneuse`**：限制操作数只能被当前节点使用，避免生成重复计算的指令。
5. **pattern 的书写顺序**：在复杂度相同的情况下，先定义的通常先试，但不要依赖这一点；需要确定性时用 `AddedComplexity`。

`riscv_add_like_non_imm12` 就是一个「排除立即数」的 predicate：

```tablegen
def riscv_add_like_non_imm12 : binop_with_non_imm12<riscv_add_like>;
```

它要求第二个操作数不是 12 位立即数。原因是 `addi` 已经能处理 simm12；如果 `SH3ADD` 抢了 `add x, 100`，反而会多一条常量物化指令。这个 predicate 把「立即数加法」留给 `ADDI`，把「移位加法」留给 `SH3ADD`。

## 调试 pattern matching 的固定流程

1. **先看最终汇编**：确定目标指令是什么，比如 `sh3add`。
2. **看 isel 后的 MIR**：`-print-after-isel`，确认是 isel 选的，不是 later pass 改的。
3. **开 `-debug-only=isel`**：找到 `Starting selection on root node`，看 `Morphed node`。
4. **定位 pattern**：在 `.td` 里搜指令名或 `Morphed node` 的操作数形状。
5. **看生成 table**：在 `RISCVGenDAGISel.inc` 里搜 `TARGET_VAL(RISCV::SH3ADD)`，读 `Src:` / `Dst:` / `Complexity`。
6. **改 feature 对比**：`-mattr=+zba` vs 无 `Zba`，看哪些 scope 被跳过。
7. **如果 matcher 全失败**：看 `Select()` 对应 opcode 分支，或加 `-debug-only=isel` 看 `Cannot select`。

一个常用命令组合：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zba -O2 \
  -debug-only=isel idx.ll -o /dev/null 2>&1 \
  | sed -n '/Starting selection on root node: t7/,/Match complete/p'
```

## 踩雷集锦

1. **把 matcher index 当成稳定编号**：`Initial Opcode index to 24569` 是生成 table 的偏移，源码一改就变。不要在测试里断言这个数字。

2. **看到 `Match failed` 就以为匹配失败**：它只是当前候选失败，matcher 会 `Continuing at ...` 试下一个。真正失败是最后没有 `Morphed node`，或者报 `Cannot select`。

3. **把 `Skipped scope entry` 当成 bug**：它是 predicate 为 false 时的正常剪枝。feature 没开时，相关 pattern 本来就不该匹配。

4. **以为 `.td` 里没有就说明没 pattern**：`Select()` 里还有一大套 C++ 匹配。常量物化、地址计算、伪指令都在那里。

5. **忽略 `Complexity` 和 `AddedComplexity`**：两个 pattern 都能匹配时，谁先试由复杂度决定。改 pattern 后指令选择变了，先看排序，不要只看形状。

6. **以为 `Pat` 的源模式就是 IR**：源模式匹配的是 combine/legalize 之后的 DAG。`mul x, 2` 在 pattern 看来可能已经是 `shl x, 1`。

7. **在 `-O0` 下调试 pattern**：`-O0` 的 DAG 形状和 `-O2` 不同，pattern 可能匹配到不同分支。要复现 `-O2` 的选择，就用 `-O2`。

8. **只看 `Morphed node` 不看操作数顺序**：`SH3ADD t4, t2` 和 `SH3ADD t2, t4` 的语义不同。RISC-V 的 `sh3add rd, rs1, rs2` 是 `rd = (rs1 << 3) + rs2`；日志里的顺序要和最终汇编对照。

## 动手练习

1. 用 `idx.ll` 跑 `-mattr=+zba` 和无 `Zba`，把 `t7` 的完整 isel 日志保存下来。数一数 `Match failed` 和 `Skipped scope` 各多少次，解释差异。

2. 在 `RISCVGenDAGISel.inc` 里找到 `TARGET_VAL(RISCV::SH3ADD)`，把周围的 `Src:` / `Dst:` / `Complexity` 抄下来。对照 `-debug-only=isel` 的 index，确认它们是同一段。

3. 把 `idx.ll` 的 `getelementptr i64` 改成 `getelementptr i8`，再跑 `+zba`。`sh3add` 还在吗？如果不在，是 pattern 不匹配，还是地址计算变成了别的形状？

4. 写一个 `add i64 %r, 100` 和一个 `add i64 %r, %s`（`%s = shl %i, 3`），分别跑 `+zba`。为什么前者不走 `SH3ADD`？用 `riscv_add_like_non_imm12` 的 predicate 解释。

5. 用 `p[1]`、`p[i]`、`p[1000]` 三个例子跑 `-debug-only=isel`，观察 `SelectAddrRegImm` 的三种结果：折叠成 offset、返回 false、生成 `ADDI` 再折叠。

6. **guided**：在 `RISCVInstrInfoZb.td` 里给 `Sh3AddPat` 加一个 `AddedComplexity = 1`，重新生成 `RISCVGenDAGISel.inc`，看 `SH3ADD` 的 matcher 顺序是否变化。不要提交这个改动。

## 本章重点整理

- `.td` 的 `Pat<源, 目标>` 由 TableGen 编译成 matcher table；运行期由 `SelectCode` 解释执行。
- `-debug-only=isel` 的 `Initial Opcode index` / `Match failed` / `Skipped scope` / `Morphed node` 分别对应 table 偏移、候选失败、predicate 剪枝、成功替换。
- `ComplexPattern` 把地址等复杂匹配放到 C++；`AddrRegImm` 返回 `base` 和 `offset`，`SelectAddrRegImm` 决定能否折叠。
- `Select()` 是 TableGen 之外的 C++ 回落路径，处理常量物化、伪指令、特殊 opcode。
- pattern 的顺序由 `Complexity`、`AddedComplexity`、`Predicates`、`hasOneUse` 共同决定；`SH3ADD` 的 `Complexity = 12` 让它先于通用 `ADD`。
- 同一个 IR，在 `+zba` 和无 `Zba` 下会走不同的 matcher scope；指令差异不是 scheduler 造成的。

## 自我检核

- [ ] 能解释 `Pat` 的左右两边分别是什么，`GPR:$rs1` 这种写法的作用
- [ ] 能读懂 `-debug-only=isel` 的 `Match failed` / `Continuing` / `Skipped scope` / `Morphed node`
- [ ] 能说出 `ComplexPattern` 和普通 `Pat` 的区别，并举出 `AddrRegImm` 的例子
- [ ] 能解释 `riscv_add_like_non_imm12` 为什么要排除 12 位立即数
- [ ] 能说出什么时候该改 `.td`，什么时候该改 `Select()`
- [ ] 能从 `RISCVGenDAGISel.inc` 的 `Src:` / `Dst:` / `Complexity` 反推 pattern
- [ ] 能解释 `+zba` 前后 `Skipped scope` 数量变化的原因

## 延伸阅读

### 官方文档

- **[LLVM TableGen Backends](https://llvm.org/docs/TableGen/BackEnds.html)**
  - **读哪里**：DAG instruction selector backend 的输出格式、matcher 指令。
  - **和本章的关联**：`RISCVGenDAGISel.inc` 里的 `OPC_*` 就是这些 matcher 指令。

- **[LLVM TableGen Programmer's Reference](https://llvm.org/docs/TableGen/ProgRef.html)**
  - **读哪里**：`Pat`、`PatFrag`、`ComplexPattern`、`multiclass`、`foreach`。
  - **和本章的关联**：`ShxAddPat` 的模板展开和 predicate 语义。

- **[LLVM Code Generator — Instruction Selection](https://llvm.org/docs/CodeGenerator.html#instruction-selection-section)**
  - **读哪里**：SelectionDAG 的 pattern matching 和 `SelectCode` 流程。
  - **和本章的关联**：本章的运行期视角。

### 源码

- **`llvm/lib/Target/RISCV/RISCVInstrInfoZb.td`**
  - **读哪里**：`ShxAddPat`（约 724 行）、`CSImm12MulBy8Pat`（约 741 行）、`Predicates = [HasStdExtZba]` 块（约 748 行）。
  - **和本章的关联**：`SH3ADD` 的 pattern 定义。

- **`llvm/lib/Target/RISCV/RISCVInstrInfo.td`**
  - **读哪里**：`AddrRegImm`（约 618 行）、`LdPat`（约 2120 行）、`StPat`（约 2135 行）、`riscv_add_like`（约 1557 行）。
  - **和本章的关联**：load/store 地址匹配和 `add_like` 的定义。

- **`llvm/lib/Target/RISCV/RISCVISelDAGToDAG.cpp`**
  - **读哪里**：`Select()`（约 1178 行）、`SelectAddrRegImm()`（约 3630 行）。
  - **和本章的关联**：C++ 回落路径和 ComplexPattern 的实现。

- **`llvm/utils/TableGen/DAGISelMatcherGen.cpp` / `DAGISelMatcherOpt.cpp`**
  - **读哪里**：pattern 如何变成 matcher、scope 如何优化。
  - **和本章的关联**：`RISCVGenDAGISel.inc` 的生成过程。

### 文章 / 演讲

- **"TableGen and Instruction Selection"**（LLVM Developers' Meeting 相关演讲）
  - **这讲什么**：`Pat`、`PatFrag`、`ComplexPattern` 的设计和常见坑。
  - **为什么值得看**：先理解 TableGen 的抽象，再读生成 table 不会迷路。

- **"How to Write a Backend"**（LLVM 后端教程）
  - **这讲什么**：从零写一个 target 的 instruction selection。
  - **和本章的关联**：pattern 的顺序、predicate、`Select()` 的取舍。

下一章看不那么「一条 pattern 一条指令」的情况：一条 pattern 如何生成多条 MachineInstr，或者先生成伪指令、等 post-RA 再展开。你会看到 `SH3ADD` 的 `ADDI x0, 1025` 兄弟、`LUI + ADDI` 常量物化，以及 `PseudoMovAddr` 的延迟展开。

→ [Ch 7 一条 pattern 如何生成多条指令](./07-pattern-to-multiple-instructions.md)
