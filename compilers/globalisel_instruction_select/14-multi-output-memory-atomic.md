# 第 14 章 多输出、变长、内存/原子、隐式操作数与 flags

> **本章目标**：把 matcher 从“一条指令、一个结果、没有内存副作用”的舒适区里拽出来。读完后你应该能回答四个具体问题：
>
> 1. 一条 gMIR 指令有多个 def 时，pattern 和 matcher 怎么表达“我要第 2 个结果”？
> 2. 操作数个数不固定的 `G_INTRINSIC`、`G_BUILD_VECTOR` 怎么匹配？
> 3. `G_LOAD`/`G_STORE` 的 `MachineMemOperand` 不是普通操作数，那 `GIM_CheckMemory*` 在检查什么？原子 ordering 又怎么落到 pattern 上？
> 4. `implicit-def $nzcv` 这类隐式操作数和 MI flag 是“渲染”出来的，还是“匹配”出来的？
>
> **前置**：第 2、5、6、7 章。**环境**：LLVM 23.1.4，AArch64 主线，RISC-V 作对照。

---

## 14.1 def 就是操作数：多结果的真实布局

第 6 章讲过 `GIM_RecordInsn` 把某个操作数的**定义者**记录成 `MIs[k]`。要理解多输出，必须先接受 gMIR 的一条底层约定：

> `MachineInstr` 的操作数数组里，**def 在前，use 在后**。第 k 个结果就是第 k 个操作数。

这不是 GlobalISel 的新发明，而是 `MachineInstr` 的通用布局。`G_UNMERGE_VALUES` 的定义把这一点写得很直白（`llvm/include/llvm/Target/GenericOpcodes.td`）：

```tablegen
// The output operands are always ordered from lowest bits to highest:
//   %bits_0_7:(s8), %bits_8_15:(s8),
//       %bits_16_23:(s8), %bits_24_31:(s8) = G_UNMERGE_VALUES %0:(s32)
def G_UNMERGE_VALUES : GenericInstruction {
  let OutOperandList = (outs type0:$dst0, variable_ops);
  let InOperandList = (ins type1:$src);
  let hasSideEffects = false;
  let variadicOpsType = type0;
  let GISelMatchGenericTypes = 1;
}
```

`InOperandList` 只有一个 `$src`，`OutOperandList` 是 `$dst0` 加 `variable_ops`。所以一条有 n 个结果的 `G_UNMERGE_VALUES`，操作数编号是 `0 … n-1` 为结果、`n` 为源寄存器。反方向的 `G_MERGE_VALUES` 正好相反：一个 def，后面跟可变个数的输入。

### 14.1.1 一个真实的 `G_UNMERGE_VALUES`

输入 IR（`load128_hi`，从 128 位值里取高 64 位）：

```llvm
define i64 @load128_hi(ptr %p) {
entry:
  %v = load i128, ptr %p, align 16
  %s = lshr i128 %v, 64
  %t = trunc i128 %s to i64
  ret i64 %t
}
```

跑到 `-stop-before=instruction-select`，gMIR 里出现：

```yaml
%0:gpr(p0) = COPY $x0
%1:fpr(i128) = G_LOAD %0(p0) :: (load (i128) from %ir.p)
%6:fpr(i64), %7:fpr(i64) = G_UNMERGE_VALUES %1(i128)
```

这条指令有 3 个操作数：`%6`（下标 0）、`%7`（下标 1）、`%1`（下标 2）。AArch64 的 `selectUnmergeValues` 就是按这个布局读源寄存器的：`unsigned NumElts = I.getNumOperands() - 1; Register SrcReg = I.getOperand(NumElts).getReg();`。

选中之后：

```yaml
%1:fpr128 = LDRQui %0, 0 :: (load (i128) from %ir.p)
%6:fpr64 = COPY %1.dsub
%7:fpr64 = DUPi64 %1, 1
```

第 0 个结果走子寄存器拷贝（`.dsub`），第 1 个结果走 `DUPi64 %1, 1`（把第 1 个 64 位 lane 复制出来）。**同一 gMIR 指令的两个 def，被拆成两条完全不同的机器指令**——这就是“多输出”在第一现场的样子。

注意 `selectUnmergeValues` 先要求第 0、1 个操作数都在 FPR bank；否则源码注释 “Unmerging vector-to-gpr and scalar-to-scalar currently unsupported” 对应的分支直接 `return false`。目标仍是向量时转给 `selectSplitVectorUnmerge`，最终仍可能失败并走 fallback。

### 14.1.2 `G_EXTRACT` / `G_INSERT` 是“按位切片”

`G_UNMERGE_VALUES` 处理等宽切片；`G_EXTRACT` 的操作数是 `($src, $idx)`，`G_INSERT` 是 `($src, $elt, $idx)`，第三个参数是**位偏移**，不是元素下标。手写一份 MIR，核心只有一行 `%1:gpr(i32) = G_EXTRACT %0(i64), 32`（外层照抄练习 A 的骨架）。用 `llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select -verify-machineinstrs -global-isel-abort=1 extract.mir -o -` 实测，选中结果：

```yaml
%0:gpr64 = COPY $x0
%2:gpr64 = UBFMXri %0, 32, 63
%1:gpr32 = COPY %2.sub_32
$w0 = COPY %1
```

`G_EXTRACT %0, 32` 变成“用 `UBFMXri` 把 [32,63] 位取出来，再拷到 32 位子寄存器”。结果类型和寄存器类仍由操作数位置决定。

---

## 14.2 matcher 怎么引用“第 2 个结果”

这是最容易望文生义的一节。先给结论：

> `GIM_RecordInsn` **没有 def index 参数**。它的 `OpIdx` 是“被记录指令的操作数下标”，记录的是这个操作数的**定义者整条 MI**。要约束第 k 个结果，就在 `MIs[NewInsnID]` 上用操作数下标 k 做后续检查。

`GIMatchTableExecutor.h` 的注释只列了三个参数：

```cpp
  /// - NewInsnID(ULEB128) - Instruction ID to define
  /// - InsnID(ULEB128) - Instruction ID
  /// - OpIdx(ULEB128) - Operand index
  GIM_RecordInsn,
```

执行实现（`GIMatchTableExecutorImpl.h`）也只做三件事：确认是虚拟寄存器、取定义者、存进 `MIs`：

```cpp
      MachineOperand &MO = State.MIs[InsnID]->getOperand(OpIdx);
      if (!MO.isReg()) { ... reject ... }
      if (MO.getReg().isPhysical()) { ... reject ... }
      MachineInstr *NewMI;
      if (MatcherOpcode == GIM_RecordInsnIgnoreCopies)
        NewMI = getDefIgnoringCopies(MO.getReg(), MRI);
      else
        NewMI = MRI.getVRegDef(MO.getReg());
```

于是“引用第 2 个结果”在表里长这样（示意，非真实偏移）：

```text
GIM_RecordInsn   MIs[1], MIs[0], 1          ; 记录 %0 的操作数 1 的定义者
GIM_CheckOpcode  MIs[1], G_UNMERGE_VALUES
GIM_CheckType    MIs[1], 1, s64             ; 第 2 个结果的类型
GIR_Copy         OutMIs[0], MIs[1], 1       ; 把第 2 个结果拷进新指令
```

### 14.2.1 pattern 不能随意嵌套多结果节点

`GlobalISelEmitter.cpp` 在导入嵌套节点时有一道硬门槛：

```cpp
    if (N.getNumResults() != 1)
      return failedImport("node does not have one result " + to_string(N));
```

也就是说：**SelectionDAG pattern 里作为子节点出现的操作必须恰好一个结果**。多结果节点只能出现在根位置（或被 `PatFrag` 包装成“只取第 0 个结果”）。这条限制解释了为什么很多多输出场景最终落到 C++ `select`，而不是 pattern。

根位置另有一套 def 计数规则：

```cpp
  // Count both implicit and explicit defs in the dst instruction.
  unsigned DstExpDefs = DstI.Operands.NumDefs,
           DstNumDefs = DstI.ImplicitDefs.size() + DstExpDefs,
           SrcNumDefs = Src.getExtTypes().size();
  if (DstNumDefs < SrcNumDefs) { ... failedImport ... }
```

两个细节值得记住：

1. **隐式 def 也算数**。`implicit-def $nzcv` 会让 `DstNumDefs` 变大，从而允许导入“源有多个结果”的 pattern。
2. 只对 `min(DstExpDefs, SrcNumDefs)` 个结果做寄存器类约束；多出来的 def 由渲染器按目标指令的固定操作数表补齐。

### 14.2.2 原子 cmpxchg：一个 def 还是两个 def

`GenericOpcodes.td` 把两件事分开定义：

```tablegen
// Generic atomic cmpxchg with internal success check.
def G_ATOMIC_CMPXCHG_WITH_SUCCESS : GenericInstruction {
  let OutOperandList = (outs type0:$oldval, type1:$success);
  let InOperandList = (ins type2:$addr, type0:$cmpval, type0:$newval);
}
def G_ATOMIC_CMPXCHG : GenericInstruction {
  let OutOperandList = (outs type0:$oldval);
  let InOperandList = (ins ptype1:$addr, type0:$cmpval, type0:$newval);
}
```

AArch64 的 Legalizer 在 23.1.4 上选择**不用** `WITH_SUCCESS`，而是选 `G_ATOMIC_CMPXCHG` 再用 `G_ICMP` 重算成功位。实测 gMIR：

```yaml
%7:gpr(i32) = G_ATOMIC_CMPXCHG %0(p0), %1, %2 :: (load store seq_cst seq_cst (i32) on %ir.p)
%8:gpr(i32) = G_ICMP intpred(eq), %7(i32), %1
```

结论很实际：**在本 target 上，多结果是通过“返回旧值 + 自己比较”实现的，而不是靠第二条 def**。照抄别的 target 的 `WITH_SUCCESS` 经验会在这里对不上。

---

## 14.3 变长操作数：intrinsic 与 build_vector

### 14.3.1 四个 intrinsic opcode

```tablegen
def G_INTRINSIC : GenericInstruction {
  let OutOperandList = (outs);
  let InOperandList = (ins unknown:$intrin, variable_ops);
  let hasSideEffects = false;
}
def G_INTRINSIC_W_SIDE_EFFECTS : GenericInstruction {
  let OutOperandList = (outs);
  let InOperandList = (ins unknown:$intrin, variable_ops);
  let hasSideEffects = true; let mayLoad = true; let mayStore = true;
}
```

另外还有 `G_INTRINSIC_CONVERGENT` / `G_INTRINSIC_CONVERGENT_W_SIDE_EFFECTS`（多一个 `isConvergent`）。

共同点：**def 列表在 TableGen 里写成空，实际 def 由指令的 `$dst` 前缀决定；操作数 0 永远是 intrinsic ID，其余是 `variable_ops`**。所以 intrinsic 的 operand 数 = 1（ID）+ 实参个数；匹配时不能写死一个值，只能写“至少 N”或按具体 intrinsic 的签名算。

### 14.3.2 `GIM_CheckNumOperandsLE/GE` 的真实状态

执行器里确实有这三条（`GIMatchTableExecutor.h`）：`GIM_CheckNumOperands`、`GIM_CheckNumOperandsLE`、`GIM_CheckNumOperandsGE`。但要注意一个版本事实：**在 23.1.4 的 ISel emitter（`GlobalISelEmitter.cpp`）里搜不到发射 `GIM_CheckNumOperandsLE/GE` 的代码**。它们是执行器能力，目前主要由手写 matcher / combiner 一侧使用。ISel 侧处理变长操作数的方式是“按 pattern 里实际列出的子节点个数逐个 `GIM_CheckType`”，再加上 `variadicOpsType` 带来的类型一致性检查。

这条事实很重要：不要因为头文件里有 `LE/GE`，就在文章或 review 里声称“TableGen 会为变长 pattern 自动生成 GE 检查”。以你手上的源码为准。

### 14.3.3 `G_BUILD_VECTOR` 家族

```tablegen
def G_BUILD_VECTOR : GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src0, variable_ops);
  ...
  let variadicOpsType = type1;
}
def G_BUILD_VECTOR_TRUNC : GenericInstruction { ... }
def G_CONCAT_VECTORS : GenericInstruction { ... }
```

`G_BUILD_VECTOR` 的操作数个数 = 向量元素个数，所以固定向量在 pattern 里可以写死，scalable vector 则不能。AArch64 的 selector 对它有专门的 C++ 入口（`selectBuildVector`），而不是纯 pattern，原因会在第 15 章展开。

---

## 14.4 内存操作：MMO 不在操作数列表里

`G_LOAD`/`G_STORE` 的 `MachineMemOperand`（MMO）是挂在指令上的**附属信息**，不是第 N 个操作数。因此：

- `GIM_CheckType` 看不到它；
- 只能用专门的 `GIM_CheckMemory*`；
- 这些指令的参数是 `MMOIdx`（第几个 MMO），不是操作数下标。

`GIMatchTableExecutor.h` 里的三个基础检查：

```cpp
  GIM_CheckMemorySizeEqualTo,      // InsnID, MMOIdx, Size(4)
  GIM_CheckMemoryAddressSpace,     // NumAddrSpace(1), AddrSpaceN
  GIM_CheckMemoryAlignment,        // MinAlign(1)
```

以及三个“和操作数类型比大小”的版本：`GIM_CheckMemorySizeEqualToLLT`、`GIM_CheckMemorySizeLessThanLLT`、`GIM_CheckMemorySizeGreaterThanLLT`。

普通 `G_LOAD`/`G_STORE` 的选择由导入的 `LDRWui` / `STRWui` 等 pattern 完成；它们的地址操作数正是练习 C 的 `am_indexed32`，而 MMO 上的 ordering 由本节这些检查决定。

### 14.4.1 `CheckMMOIsNonAtomic` 到底做了什么

它不是一个 `GIM_*` 名字，而是 `GINodeEquiv` 的字段（`SelectionDAGCompat.td`）：

```tablegen
def : GINodeEquiv<G_LOAD, ld> {
  let CheckMMOIsNonAtomic = true;
  let IfSignExtend = G_SEXTLOAD;
  let IfZeroExtend = G_ZEXTLOAD;
  let IfFPExtend = G_FPEXTLOAD;
};
def : GINodeEquiv<G_LOAD, atomic_load> {
  let CheckMMOIsNonAtomic = false; let CheckMMOIsAtomic = true;
};
def : GINodeEquiv<G_STORE, atomic_store> {
  let CheckMMOIsNonAtomic = false; let CheckMMOIsAtomic = true;
};
```

导入时（`GlobalISelEmitter.cpp`）：

```cpp
  if (SrcGIEquivOrNull->getValueAsBit("CheckMMOIsNonAtomic"))
    InsnMatcher.addPredicate<AtomicOrderingMMOPredicateMatcher>("NotAtomic");
```

它最终落到 `GIM_CheckAtomicOrdering`。执行器要求**恰好一个 MMO**，然后比较合并后的 ordering：

```cpp
      if (!State.MIs[InsnID]->hasOneMemOperand())
        ... reject ...
      for (const auto &MMO : State.MIs[InsnID]->memoperands())
        if (MMO->getMergedOrdering() != Ordering)
          ... reject ...
```

所以“普通 load 不能匹配原子 load”不是靠 opcode，而是靠 MMO 的 ordering。

### 14.4.2 一个容易误解的实测：monotonic 就是普通 `ldr`

```llvm
define i32 @aload(ptr %p) {
  %v = load atomic i32, ptr %p monotonic, align 4
  ret i32 %v
}
```

AArch64 实测汇编：

```asm
aload:
	ldr	w0, [x0]
	ret
```

gMIR 里明明是 `G_LOAD ... :: (load monotonic (i32) ...)`，为什么还能命中“非原子”的 `LDRWui`？因为 AArch64 专门为 relaxed 原子写了一套 pattern（`AArch64InstrAtomics.td`）：

```tablegen
def : Pat<(relaxed_load<atomic_load_nonext_32>
               (am_indexed32 GPR64sp:$Rn, uimm12s4:$offset)),
          (LDRWui GPR64sp:$Rn, uimm12s4:$offset)>;
def : Pat<(seq_cst_load<atomic_load_nonext_32> GPR64sp:$ptr),
          (LDARW GPR64sp:$ptr)>;
def : Pat<(acquiring_load<atomic_load_nonext_32> GPR64sp:$ptr),
          (LDARW GPR64sp:$ptr)>;
```

`relaxed_load` / `acquiring_load` / `seq_cst_load` 这几个 `PatFrag` 在定义里带了 `IsAtomic` 与 ordering 位（`TargetSelectionDAG.td` 的 `binary_atomic_op_ord` / `ternary_atomic_op_ord`），导入后变成 `GIM_CheckAtomicOrdering*` 系列。实测对照：

| IR | 汇编 |
| --- | --- |
| `load atomic ... monotonic` | `ldr w0, [x0]` |
| `load atomic ... acquire` | `ldar w0, [x0]` |
| `load atomic ... acquire` + `-mattr=+rcpc-immo` | `ldapr w0, [x0]` |

第三行来自 `AArch64InstrAtomics.td` 里 `Predicates = [HasRCPC_IMMO]` 的 `LDAPURi` 族 pattern。

---

## 14.5 真实案例：AArch64 的 atomic cmpxchg

### 14.5.1 两套 pattern

**没有 LSE 时**（`AArch64InstrGISel.td`，`let Predicates = [HasNoLSE]`）：

```tablegen
def : Pat<(atomic_cmp_swap_i32 GPR64:$addr, GPR32:$desired, GPR32:$new),
          (CMP_SWAP_32 GPR64:$addr, GPR32:$desired, GPR32:$new)>;
```

（`i8` / `i16` / `i64` 各有一条同构 pattern。）注意 `CMP_SWAP_*` 的声明是**两个** out（`AArch64InstrAtomics.td`）：

```tablegen
let Constraints = "@earlyclobber $Rd,@earlyclobber $scratch",
    mayLoad = 1, mayStore = 1 in {
def CMP_SWAP_32 : Pseudo<(outs GPR32:$Rd, GPR32:$scratch),
                         (ins GPR64:$addr, GPR32:$desired, GPR32:$new), []>,
                  Sched<[WriteAtomic]>;
}
```

一个真结果（旧值）+ 一个 scratch。`early-clobber` 是硬性要求：`LDXR/STXR` 循环里 `$scratch` 会和 `$Rd` 同时活跃。

**有 LSE 时**（`AArch64InstrAtomics.td`）只有一行 `defm : CASregister_patterns<"CAS", "atomic_cmp_swap">;`，它在 `AArch64InstrFormats.td` 展开成按 ordering 分派的五条 pattern：

```tablegen
let Predicates = [HasLSE] in
multiclass CASregister_patterns_ord_dag<string inst, string suffix, string op,
                                        ValueType vt, dag OLD, dag NEW> {
  def : Pat<(!cast<PatFrag>(op#"_"#vt#"_monotonic") GPR64sp:$Rn, OLD, NEW),
            (!cast<Instruction>(inst # suffix) OLD, NEW, GPR64sp:$Rn)>;
  def : Pat<(!cast<PatFrag>(op#"_"#vt#"_acquire") GPR64sp:$Rn, OLD, NEW),
            (!cast<Instruction>(inst # "A" # suffix) OLD, NEW, GPR64sp:$Rn)>;
  ...
}
```

`atomic_cmp_swap` 映射到 `G_ATOMIC_CMPXCHG`（`SelectionDAGCompat.td`），所以这一整套 SelectionDAG pattern 会被 `-gen-global-isel` 重新导入。

### 14.5.2 可跑实验一：cmpxchg 的 gMIR 与汇编

```llvm
define i32 @cas_i32(ptr %p, i32 %cmp, i32 %new) {
entry:
  %pair = cmpxchg ptr %p, i32 %cmp, i32 %new seq_cst seq_cst
  %old = extractvalue { i32, i1 } %pair, 0
  ret i32 %old
}
```

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 \
    -stop-before=instruction-select cas.ll -o cas-pre.mir
```

gMIR（节选）：

```yaml
%0:gpr(p0) = COPY $x0
%1:gpr(i32) = COPY $w1
%2:gpr(i32) = COPY $w2
%5:gpr(i32) = G_ATOMIC_CMPXCHG %0(p0), %1, %2 :: (load store seq_cst seq_cst (i32) on %ir.p)
%3:gpr(i32) = COPY %5(i32)
$w0 = COPY %3(i32)
```

选中后：

```yaml
%0:gpr64 = COPY $x0
%1:gpr32 = COPY $w1
%2:gpr32 = COPY $w2
early-clobber %5:gpr32, dead early-clobber %6:gpr32 = CMP_SWAP_32 %0, %1, %2 :: (load store seq_cst seq_cst (i32) on %ir.p)
%3:gpr32all = COPY %5
$w0 = COPY %3
```

汇编（默认，无 LSE）：

```asm
cas_i32:
	mov	x9, x0
.LBB0_1:
	ldaxr	w0, [x9]
	cmp	w0, w1
	b.ne	.LBB0_3
	stlxr	w8, w2, [x9]
	cbnz	w8, .LBB0_1
.LBB0_3:
	ret
```

加 `-mattr=+lse`：

```asm
cas_i32:
	mov	x8, x0
	mov	w0, w1
	casal	w0, w2, [x8]
	ret
```

这两段输出把三件事同时证明了：

1. **pattern 是按 feature 选路的**：`HasNoLSE` 与 `HasLSE` 两组 pattern 互斥；
2. **多 def 的机器指令确实存在**：`CMP_SWAP_32` 的两个 def 在 MIR 里可见，第二个是 dead 的 scratch；
3. **MMO 的 ordering 参与匹配**：`seq_cst seq_cst` 进了 `CASAL`，而不是 `CAS`。

### 14.5.3 可跑实验二：多 def 与隐式 `$nzcv`

```llvm
define i1 @sadd_flag(i64 %a, i64 %b) {
entry:
  %r = call { i64, i1 } @llvm.sadd.with.overflow.i64(i64 %a, i64 %b)
  %f = extractvalue { i64, i1 } %r, 1
  ret i1 %f
}
```

gMIR：

```yaml
%2:gpr(i64), %6:gpr(i32) = G_SADDO %0, %1
$w0 = COPY %6(i32)
```

选中后：

```yaml
%2:gpr64 = ADDSXrr %0, %1, implicit-def $nzcv
%6:gpr32 = CSINCWr $wzr, $wzr, 7, implicit $nzcv
```

汇编：

```asm
sadd_flag:
	adds	x8, x0, x1
	cset	w0, vs
	ret
```

这里没有“第 2 个结果的寄存器”：`G_SADDO` 的第 1 个 def 被**降维**成了 `$nzcv`，再由 `CSINCWr`（`cset` 的别名，条件码 7 = VS）读回来。这就是为什么多输出不能只靠“多写一个 def”来理解：目标 ISA 可能根本没有第二个物理结果。

---

## 14.6 隐式 def/use 与 flags 是渲染出来的

匹配阶段只能读。所有写操作都在 `GIR_*` 里。和隐式操作数相关的完整清单（`GIMatchTableExecutor.h`）：

| 名称（23.1.4 实际拼写） | 作用 |
| --- | --- |
| `GIR_AddImplicitDef` | 给目标 MI 加一个隐式 def（带 RegNum + Flags） |
| `GIR_AddImplicitUse` | 加一个隐式 use |
| `GIR_AddRegister` / `GIR_AddIntrinsicID` | 加普通寄存器 / intrinsic ID 操作数 |
| `GIR_SetImplicitDefDead` | 把第 N 个隐式 def 标记为 dead |
| `GIR_SetMIFlags` / `GIR_UnsetMIFlags` | 设置/清除 MI flag（注意是复数） |

两个容易写错的点：

1. **是 `GIR_SetMIFlags`，不是 `GIR_SetMIFlag`**。写错就编译不过。
2. `GIR_SetImplicitDefDead` 的 `OpIdx` 从 0 开始数“第几个隐式 def”，不是操作数下标。

### 14.6.1 `propagateFlags` 与 `NoFPExcept`

每条规则成功时（`GIR_Done` / `GIR_EraseRootFromParent_Done` / `GIR_DoneWithCustomAction`）都会调用 `propagateFlags()`（节选，省略 Observer 回调）：

```cpp
  bool NoFPException = !State.MIs[0]->getDesc().mayRaiseFPException();
  const uint32_t Flags = State.MIs[0]->getFlags();
  const auto propagateFlags = [&]() {
    for (auto MIB : OutMIs) {
      uint32_t MIBFlags = Flags | MIB.getInstr()->getFlags();
      if (NoFPException && MIB->mayRaiseFPException())
        MIBFlags |= MachineInstr::NoFPExcept;
      MIB.setMIFlags(MIBFlags);
    }
  };
```

含义：

- 原 generic 指令的 **MI flags 会被带到所有新指令**上（比如 `FrameSetup`、`NoMerge`）；
- 如果原指令**不可能**抛 FP 异常，而新指令**可能**，则给新指令打上 `NoFPExcept`，避免后续 pass 把它当成会改 FP 状态的操作。

这不是“可选优化”：漏掉它会让 `strictfp` 语义在 ISel 后被悄悄放松。

### 14.6.2 `$nzcv` 的隐式 def 从哪来

`ADDSXrr` 的 TableGen 定义里带 `implicit-def $nzcv`（`AArch64InstrFormats.td` 的 add/sub 类），所以渲染器会发射 `GIR_AddImplicitDef`；而 `CSINCWr` 的定义里带 `implicit $nzcv`（use），于是发射 `GIR_AddImplicitUse`。这些都不需要 pattern 作者手写，只要目标指令的 `.td` 声明正确。

反过来，如果给一条**没有** `implicit-def $nzcv` 的指令硬加 `implicit-def $nzcv`，MachineVerifier 会报“implicit def not in instruction descriptor”一类错误；如果漏加，后续 `CSEL` 读到的就是未定义的条件码。

---

## 14.7 踩雷清单

1. **把 `GIM_RecordInsn` 的 `OpIdx` 当成 def index**。它是“use 的操作数下标”，记录的是定义者整条 MI。要第 k 个结果，就在记录后的 MI 上用下标 k。
2. **在 pattern 里嵌套多结果节点**。`GlobalISelEmitter.cpp` 直接 `failedImport("node does not have one result")`，你会得到一条“pattern 没导入”的静默缺失。
3. **把 MMO 当普通操作数**。`G_LOAD` 的 `%ptr` 是操作数 0，MMO 不是操作数 1；`GIM_CheckMemory*` 的 `MMOIdx` 从 0 数 MMO，不是从操作数数。
4. **以为 `CheckMMOIsNonAtomic` 是一个 `GIM_*` opcode**。它是 `GINodeEquiv` 的 bit 字段，落到 `GIM_CheckAtomicOrdering`。
5. **给 `CMP_SWAP_*` 手写 `implicit-def $nzcv`**。它没有这个隐式 def；成功位是 `G_ICMP` 重新算的。硬加会让 verifier 或后续 pass 行为异常。

---

## 14.8 进阶

- **读 `selectUnmergeValues` 的边界**：vector-to-gpr 与 scalar-to-scalar 会直接 `return false`；向量拆子向量转给 `selectSplitVectorUnmerge`。这是一个真实的、可上游贡献的 TODO。
- **看 `G_UNMERGE_VALUES` 的 `variadicOpsType`**：它保证所有 def 同类型，但**不保证** def 个数与源宽度匹配——那是 verifier/legalizer 的责任。

---

## 14.9 动手练习

### 练习 14.1 预测 `G_UNMERGE_VALUES` 的选择

把 14.1.1 的 `load128_hi` 改成 `load128_lo`（取低 64 位），先预测 `G_UNMERGE_VALUES` 会不会出现，再跑：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 \
    -stop-before=instruction-select mem.ll -o -
```

对照：低半部分走的是 `G_TRUNC` + `COPY %1.dsub`，而不是 `G_UNMERGE_VALUES`；如果改成先 `lshr` 再 `trunc`（取高半部分），结果会不会不同？为什么？

### 练习 14.2 观察 ordering 分派

对同一段 `cmpxchg` IR，分别用 `monotonic`、`acquire`、`release`、`acq_rel`、`seq_cst`，在 `-mattr=+lse` 下跑，列出 `CAS` / `CASA` / `CASL` / `CASAL` 的对应关系。提示：看 `CASregister_patterns_ord_dag` 的后缀拼接规则。

### 练习 14.3 手写一份带 MMO 的 `G_LOAD`

从 `aload` 的 `-stop-before=instruction-select` 输出里复制函数文档，删掉 IR，改成手写 MIR：保留 `G_LOAD ... :: (load monotonic (i32) ...)`，再把 MMO 的 `monotonic` 改成 `seq_cst`。用 `-run-pass=instruction-select -verify-machineinstrs` 跑两遍，解释为什么 `seq_cst` 不再命中 `LDRWui`，而是落到 `LDARW`。

---

## 14.10 重点整理

- gMIR 的 def 就是操作数前缀；第 k 个结果 = 第 k 个操作数。
- `GIM_RecordInsn` 记录的是“操作数的定义者整条 MI”，没有 def index；引用第 k 个结果靠后续操作数下标。
- pattern 里嵌套的节点必须单结果；多结果只能出现在根，或被 `PatFrag` 收窄成一个结果。
- `G_UNMERGE_VALUES`/`G_MERGE_VALUES` 是变长 def/use；`G_INTRINSIC*` 是“ID + variable_ops”。
- MMO 不是操作数；`GIM_CheckMemorySizeEqualTo/AddressSpace/Alignment` 用 `MMOIdx`，ordering 检查要求恰好一个 MMO。
- `CheckMMOIsNonAtomic` 是 `GINodeEquiv` 字段，最终变成 `GIM_CheckAtomicOrdering`。
- AArch64 的 cmpxchg 有两套 pattern：`HasNoLSE` 的 `CMP_SWAP_*`（两 def + early-clobber）与 `HasLSE` 的 `CAS*`（按 ordering 分派）。
- 隐式 def/use 与 MI flag 由 `GIR_AddImplicit*` / `GIR_SetMIFlags` 渲染，`propagateFlags` 负责继承与 `NoFPExcept`。

---

## 14.11 自我检核

1. 一条 `G_UNMERGE_VALUES` 有 3 个结果，源寄存器是第几个操作数？
2. `GIM_RecordInsn` 的三个参数分别是什么？为什么没有 def index？
3. 什么情况下 `GlobalISelEmitter` 会因为“多结果”拒绝导入一条 pattern？
4. `G_ATOMIC_CMPXCHG` 与 `G_ATOMIC_CMPXCHG_WITH_SUCCESS` 的 def 数分别是多少？AArch64 用哪个？
5. `GIM_CheckMemorySizeEqualTo` 的第二个参数是什么？为什么不是操作数下标？
6. `CMP_SWAP_32` 为什么需要 `early-clobber` 的 scratch？
7. `propagateFlags` 在什么条件下设置 `NoFPExcept`？

---

## 14.12 延伸阅读

- `llvm/include/llvm/Target/GenericOpcodes.td` — `G_UNMERGE_VALUES`、`G_INTRINSIC*`、`G_ATOMIC_CMPXCHG*` 的操作数布局，本章所有“第几个操作数”的依据。
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutor.h` 与 `GIMatchTableExecutorImpl.h` — `GIM_RecordInsn`、`GIM_CheckMemory*`、`GIR_AddImplicit*`、`propagateFlags` 的实现。
- `llvm/lib/Target/AArch64/AArch64InstrAtomics.td` — `CMP_SWAP_*`、`relaxed_load`、`CASregister_patterns` 的真实 pattern。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp` — `selectUnmergeValues`、`selectMergeValues` 的 C++ 侧实现。
- `llvm/utils/TableGen/GlobalISelEmitter.cpp` — 多结果导入限制（`node does not have one result`）与 def 计数规则。

---

## 下一章

[第 15 章 向量、intrinsic 与 RISC-V 边界](./15-vector-intrinsic-riscv.md) —— 把本章的“多结果 + 变长 + intrinsic”放进向量世界：`G_BUILD_VECTOR`/`G_SHUFFLE_VECTOR` 在 AArch64 上如何落地，scalable vector 为什么连 IRTranslator 都过不去，以及 RISC-V 的 GlobalISel 支持边界到底在哪。
