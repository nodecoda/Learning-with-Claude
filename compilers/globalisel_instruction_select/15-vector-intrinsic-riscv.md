# 第 15 章 向量、intrinsic 与 RISC-V 边界

> **本章目标**：把第 14 章的“多结果 + 变长 + intrinsic”放进向量世界。读完后你应该能回答：
>
> 1. gMIR 的固定向量与 scalable vector 在类型、操作数和 selection 入口上差在哪？
> 2. AArch64 为什么把 `G_BUILD_VECTOR`、`G_SHUFFLE_VECTOR`、`G_EXTRACT_VECTOR_ELT`、`G_CONCAT_VECTORS` 放进 C++，而不是纯 pattern？
> 3. `G_INTRINSIC` 的 intrinsic ID 怎么参与匹配？为什么有的 intrinsic 能用 TableGen，有的必须写 C++？
> 4. RISC-V 的 GlobalISel 支持边界到底在哪？`+v` 是否等于“通用向量操作都能选”？
>
> **前置**：第 5、6、7、14 章。**环境**：LLVM 23.1.4，AArch64 主线，RISC-V 作对照。

---

## 15.1 gMIR 的向量类型与操作

GlobalISel 的向量类型只有两类：

- **固定向量**：`<4 x s32>`、`<8 x s16>`、`<2 x s64>`，元素个数是编译期常量；
- **scalable vector**：`<vscale x 4 x s32>`，元素个数是 `vscale * 4`，`vscale` 由目标在运行时决定。

这两类在 gMIR 里都是合法 LLT，但**能不能被 IRTranslator 接住、能不能被 Legalizer 处理、能不能被 InstructionSelect 选中，是三件独立的事**。本章后面的每个失败案例，都要先判断它发生在哪一阶段。

### 15.1.1 六个核心向量 opcode

```tablegen
def G_BUILD_VECTOR : GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src0, variable_ops);
}
def G_SHUFFLE_VECTOR : GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src1, type2:$src2, type3:$mask);
}
def G_EXTRACT_VECTOR_ELT : GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src, type2:$idx);
}
def G_INSERT_VECTOR_ELT : GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src, type2:$elt, type3:$idx);
}
def G_CONCAT_VECTORS : GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src1, variable_ops);
}
```

（`G_BUILD_VECTOR_TRUNC` 与 `G_BUILD_VECTOR` 同构，只是会把更宽的元素截断。）记忆要点：

- `G_BUILD_VECTOR` 的操作数个数 = 元素个数，是**变长 def/use**；
- `G_SHUFFLE_VECTOR` 的第 3 个操作数不是普通寄存器，而是 `shufflemask(...)` 立即数；
- `G_EXTRACT_VECTOR_ELT` / `G_INSERT_VECTOR_ELT` 的 `idx` 是**寄存器**，不是立即数；
- `G_CONCAT_VECTORS` 在 23.1.4 的 `GenericOpcodes.td` 里是 `variable_ops`，但实际选择路径通常只处理两个输入。

Legalizer 还会产生一批 AArch64 私有的向量 pseudo，例如 `G_ZIP1`/`G_ZIP2`、`G_UZP1`/`G_UZP2`、`G_TRN1`/`G_TRN2`、`G_REV32`/`G_REV64`。它们不是通用 opcode，只在 target 的 legalizer 与 selector 之间流动。

### 15.1.2 固定与 scalable 的分水岭

固定向量的 mask、lane index 都可以是编译期常量，因此 pattern 能写死元素个数；scalable vector 的元素个数不是常量，`G_BUILD_VECTOR` 不可能列出“全部元素”。这不是 TableGen 的限制，而是表示法本身的限制。

AArch64 的 IRTranslator 在 23.1.4 上**不接受 scalable vector 参数**。实测：

```console
llc -mtriple=aarch64-linux-gnu -O0 -mattr=+sve -global-isel \
    -global-isel-abort=1 svadd.ll -o -
```

```text
LLVM ERROR: unable to lower function: <vscale x 4 x i32>
  (<vscale x 4 x i32>, <vscale x 4 x i32>) (in function: svadd)
```

注意错误发生在 `IRTranslator`，连 gMIR 都没生成。加 `-global-isel-abort=0` 后整函数回退 SelectionDAG，才得到 SVE 汇编：

```asm
svadd:
	add	z0.s, z0.s, z1.s
	ret
```

所以“AArch64 支持 SVE”与“AArch64 GlobalISel 支持 scalable vector”是两句完全不同的话。

---

## 15.2 AArch64：向量选择主要走 C++ 入口

`AArch64InstructionSelector::select` 的分派表里，四个结构操作有明确的 C++ 入口：`G_BUILD_VECTOR` → `selectBuildVector`，`G_SHUFFLE_VECTOR` → `selectShuffleVector`，`G_EXTRACT_VECTOR_ELT` → `selectExtractElt`，`G_CONCAT_VECTORS` → `selectConcatVectors`（`AArch64InstructionSelector.cpp`）。

`G_INSERT_VECTOR_ELT` **不在**这个通用 switch 里；它在 Legalizer 阶段就被展开/标量化，或在 target legalizer 里变成 AArch64 私有的 `G_INSERT_VECTOR_ELT` pseudo。读源码时不要把两者混为一谈。

### 15.2.1 `selectBuildVector`：先试优化，再退回 lane insert

源码开头写得很直白：

```cpp
  // Until we port more of the optimized selections, for now just use a vector
  // insert sequence.
```

实际流程是：

1. `tryOptConstantBuildVec`：全常量向量，尝试直接构造常量；
2. `tryOptBuildVecToSubregToReg`：只有一个非 undef 元素时，用 `SUBREG_TO_REG`；
3. 否则对每个 lane 发 `INSERT_SUBREG` / `INSvi*gpr`，元素宽度只支持 8/16/32/64。

手写一份 `G_BUILD_VECTOR` 的 MIR：

```yaml
%0:gpr(i32) = COPY $w0
%1:gpr(i32) = COPY $w1
%2:gpr(i32) = COPY $w2
%3:gpr(i32) = COPY $w3
%4:fpr(<4 x s32>) = G_BUILD_VECTOR %0(i32), %1(i32), %2(i32), %3(i32)
$q0 = COPY %4(<4 x s32>)
```

用 `llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select -verify-machineinstrs -global-isel-abort=1 buildvec.mir -o -` 实测：

```yaml
%5:fpr128 = IMPLICIT_DEF
%6:fpr128 = INSERT_SUBREG %5, %0, %subreg.ssub
%7:fpr128 = INSvi32gpr %6, 1, %1
%8:fpr128 = INSvi32gpr %7, 2, %2
%4:fpr128 = INSvi32gpr %8, 3, %3
$q0 = COPY %4
```

这正是源码里“vector insert sequence”的字面含义。它也能解释为什么常量 splat 会走另一条路：如果 IR 是 `insertelement` + 全零 shuffle，Legalizer 会先把它折成 `G_DUP`，最终得到 `dup v0.4s, w0`，而不是四条 insert。

`G_BUILD_VECTOR_TRUNC` 不在这条 C++ 路径里：`AArch64InstructionSelector.cpp` 的通用 switch 只列了 `G_BUILD_VECTOR`。它要么在 Legalizer 被窄化，要么需要导入的 pattern 处理；如果 `-stop-before=instruction-select` 里它仍然存在，先检查 legalize 组合。

### 15.2.2 `selectShuffleVector`：TBL + 常量池 mask

`selectShuffleVector` 的核心不是“找一条 shuffle 指令”，而是**把 lane mask 展开成字节 mask，再用 `TBL` 查表**：

```cpp
  unsigned BytesPerElt = DstTy.getElementType().getSizeInBits() / 8;
  for (int Byte = 0; Byte < BytesPerElt; ++Byte)
    NewMask.push_back(Byte + M * BytesPerElt);
  ...
  MachineInstr *IndexLoad = emitLoadFromConstantPool(CPVal, MIB);
```

它只处理 128 位和 64 位结果（源码里对 64 位有 `assert`），并且要求 mask 是编译期常量。实测 IR：

```llvm
define <4 x i32> @vshuf(<4 x i32> %a, <4 x i32> %b) {
entry:
  %r = shufflevector <4 x i32> %a, <4 x i32> %b,
                     <4 x i32> <i32 0, i32 5, i32 2, i32 7>
  ret <4 x i32> %r
}
```

gMIR 是 `G_SHUFFLE_VECTOR %0, %1, shufflemask(0, 5, 2, 7)`；选中后为：

```yaml
%5:qq = REG_SEQUENCE %0, %subreg.qsub0, %1, %subreg.qsub1
%2:fpr128 = TBLv16i8Two %5, %4
```

汇编（省略常量池内容）：

```asm
	adrp	x8, .LCPI1_0
	ldr	q2, [x8, :lo12:.LCPI1_0]
	tbl	v0.16b, { v0.16b, v1.16b }, v2.16b
	ret
```

`REG_SEQUENCE` 把两个 128 位源拼成 `qq`（256 位寄存器对），`TBLv16i8Two` 用常量池里的 16 字节 mask 做双表查找。**这也是 pattern 很难表达的部分**：常量池、寄存器对、mask 的字节展开都是 C++ 逻辑。

### 15.2.3 `selectExtractElt`：只接受常量下标和 FPR 结果

```cpp
  if (RBI.getRegBank(DstReg, MRI, TRI)->getID() != AArch64::FPRRegBankID)
    return false;
  auto VRegAndVal = getIConstantVRegValWithLookThrough(LaneIdxOp.getReg(), MRI);
  if (!VRegAndVal)
    return false;
```

两个限制：结果必须在 FPR bank；下标必须是可看穿的常量。实测 `extractelement <4 x i32> %a, i32 2` 的 gMIR 是 `G_EXTRACT_VECTOR_ELT %0, %2(i64)`，其中 `%2 = G_CONSTANT i64 2`；选中后：

```yaml
%1:fpr32 = DUPi32 %0, 2
```

```asm
	mov	s0, v0.s[2]
	fmov	w0, s0
```

动态下标则完全不同。实测 `extractelement <4 x i32> %v, i32 %idx` 在 `-stop-before=instruction-select` 看到的是 `G_FRAME_INDEX` + `G_STORE` 整向量 + `G_MUL idx,4` + `G_PTR_ADD` + 标量 `G_LOAD`。Legalizer 直接把向量 spill 到栈上，没有 `G_EXTRACT_VECTOR_ELT` 可匹配，自然也不需要 selector 支持动态下标。

### 15.2.4 `selectConcatVectors`

`G_CONCAT_VECTORS` 的实现只有一句 `emitVectorConcat(Dst, Op1, Op2, MIB)`。它要求两个源能拼进目标寄存器（64+64→128 或 128+128→256），不支持任意个 `variable_ops`。实测 `<2 x i32>` 拼成 `<4 x i32>`：

```yaml
%5:fpr128 = INSERT_SUBREG %4, %1, %subreg.dsub
%7:fpr128 = INSERT_SUBREG %6, %0, %subreg.dsub
%3:fpr128 = INSvi64lane %7, 1, %5, 0
```

### 15.2.5 可跑例子一：AArch64 固定向量

```llvm
define <4 x i32> @vadd(<4 x i32> %a, <4 x i32> %b) {
entry:
  %r = add <4 x i32> %a, %b
  ret <4 x i32> %r
}

define i32 @vext(<4 x i32> %a) {
entry:
  %r = extractelement <4 x i32> %a, i32 2
  ret i32 %r
}
```

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 vec.ll -o -
```

实测汇编（节选）：

```asm
vadd:
	add	v0.4s, v0.4s, v1.4s
	ret

vext:
	mov	s0, v0.s[2]
	fmov	w0, s0
	ret
```

`G_ADD <4 x i32>` 能命中导入的 `ADDv4i32` pattern；`G_EXTRACT_VECTOR_ELT` 则走 `selectExtractElt` 的 `DUPi32`。同一个文件里两条完全不同的路径，正好说明“向量选择”不是单一机制。

---

## 15.3 intrinsic：ID 是一个操作数，但不是普通参数

### 15.3.1 两种 intrinsic opcode

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

`G_INTRINSIC_W_SIDE_EFFECTS` 的 `mayLoad = true; mayStore = true` 不是装饰：它让指令携带 `MachineMemOperand`，匹配时就能用第 14 章的 `GIM_CheckMemory*` 与 ordering 检查；`G_INTRINSIC` 则没有这层内存语义。

实际 gMIR 里 def 仍然排在最前面（`outs` 为空是 TableGen 描述符的写法；MIR 文本里的 `%dst =` 仍会生成操作数 0），所以 `%1:fpr(i32) = G_INTRINSIC intrinsic(@llvm.aarch64.neon.saddv), %0` 的操作数布局是：下标 0 为结果 `%1`，下标 1 为 intrinsic ID，下标 2 起为实参。这就是为什么 C++ 里读第一个实参要用 `getOperand(2)`。

### 15.3.2 导入器怎么处理 intrinsic ID

`GlobalISelEmitter.cpp` 在导入 intrinsic pattern 时，对第一个子操作数加 `IntrinsicIDOperandMatcher`，并额外处理指针与 `immarg` 参数：

```cpp
    if (IsIntrinsic) {
      if (I == 0) {
        OperandMatcher &OM = InsnMatcher.addOperand(OpIdx++, ...);
        OM.addPredicate<IntrinsicIDOperandMatcher>(II);
        continue;
      }
      OperandIsAPointer |= II->isParamAPointer(I - 1);
      OperandIsImmArg |= II->isParamImmArg(I - 1);
    }
```

注意 `I - 1`：intrinsic 的参数表不包含 ID，所以 pattern 子节点下标与 `IntrinsicInfo` 的参数下标差一。

### 15.3.3 简单 intrinsic 可以是纯 TableGen

AArch64 的 `saddv` 就是纯 pattern（`AArch64InstrGISel.td`）：

```tablegen
multiclass SIMDAcrossLanesSignedIntrinsicBHS<string baseOpc, Intrinsic intOp> {
  def : Pat<(i8 (intOp (v16i8 V128:$Rn))),
        (!cast<Instruction>(!strconcat(baseOpc, "v16i8v")) V128:$Rn)>;
  ...
}
defm : SIMDAcrossLanesSignedIntrinsicBHS<"ADDV", int_aarch64_neon_saddv>;
```

实测：

```llvm
define i32 @saddv(<4 x i32> %a) {
entry:
  %r = call i32 @llvm.aarch64.neon.saddv.i32.v4i32(<4 x i32> %a)
  ret i32 %r
}
declare i32 @llvm.aarch64.neon.saddv.i32.v4i32(<4 x i32>)
```

gMIR 是 `%1:fpr(i32) = G_INTRINSIC intrinsic(@llvm.aarch64.neon.saddv), %0(<4 x i32>)`；选中后为 `%2:fpr32 = ADDVv4i32v %0`，再经 `INSERT_SUBREG ... ssub` 返回。汇编：

```asm
	addv	s1, v0.4s
	fmov	s0, s1
	fmov	w0, s0
	ret
```

`ADDVv4i32v` 的结果在 FPR32，返回 `i32` 需要经 FPR 再搬到 GPR，所以有两条 `fmov`。这不是 selector 啰嗦，而是寄存器 bank 决定的。

### 15.3.4 什么时候必须写 C++

纯 pattern 能工作的前提是：一个 intrinsic 节点、一个目标指令、参数与结果一一对应、没有额外的目标伪指令。一旦出现下面任一情况，pattern 就会变得勉强甚至无法导入：

- 参数是 `immarg`、`llvm_anyptr_ty` 或多态类型，pattern 的类型推导对不上；
- 一个 intrinsic 要展开成多条指令，或需要分配 scratch 寄存器；
- 结果不是单个寄存器，或需要读 `MachineMemOperand` 的 ordering；
- 需要按 subtarget feature 选择不同的指令序列。

`selectIntrinsic` 的 C++ switch 就是为这些情况准备的，例如 `ptrauth_resign` 会展开成多次 copy + `AUTPAC`。intrinsic 的**匹配**可以自动，但**展开**经常只能手写。

---

## 15.4 RISC-V：支持边界不是“有没有 +v”

RISC-V 的 GlobalISel 支持是**分类型、分阶段**的。最容易犯的错误是：看到 `+v` 和 `vadd.vv`，就以为通用固定向量操作都能选。

### 15.4.1 固定向量：Legalizer 就过不去

不带 `+v` 时：

```console
llc -mtriple=riscv64-linux-gnu -O0 -global-isel -global-isel-abort=1 vec.ll -o -
```

```text
LLVM ERROR: unable to legalize instruction:
  %5:_(<4 x s32>) = G_ADD %0:_, %1:_ (in function: vadd)
```

带 `+v` 后，固定向量的 ABI 会先把参数表示成 scalable 类型，再切出固定子向量（`G_EXTRACT_SUBVECTOR` / `G_INSERT_SUBVECTOR`），但 Legalizer 仍然失败：

```text
LLVM ERROR: unable to legalize instruction:
  %6:_(<vscale x 2 x s32>) = G_INSERT_SUBVECTOR %5:_,
  %4:_(<4 x s32>), 0 (in function: vadd)
```

这不是“`vadd.vv` 选不出来”，而是**固定向量与 RVV scalable 寄存器之间的来回转换没有 legalize 规则**。所以 RISC-V 的固定向量 GlobalISel 在 23.1.4 上不能当作可用路径。

### 15.4.2 回退之后才有向量汇编

把 abort 关掉：

```console
llc -mtriple=riscv64-linux-gnu -O0 -mattr=+v -global-isel \
    -global-isel-abort=0 vec_generic.ll -o -
```

整函数回退 SelectionDAG，得到（节选）：

```asm
vadd:
	vsetivli	zero, 4, e32, m1, ta, ma
	vmv1r.v	v10, v9
	vmv1r.v	v9, v8
	vadd.vv	v8, v9, v10
	ret

vext:
	vsetivli	zero, 1, e32, m1, ta, ma
	vmv1r.v	v9, v8
	vslidedown.vi	v8, v9, 2
	vmv.x.s	a0, v8
	ret
```

这几条 `vmv1r.v` 是 ABI 把参数搬进 `v8` 约定寄存器，不是 GlobalISel 的产物。看到 `-global-isel-abort=0` 的汇编时，必须先确认它到底来自 GlobalISel 还是 SelectionDAG 回退。

### 15.4.3 scalable vector：`G_ADD` 可以，`G_MUL` 不行

把 IR 换成 `<vscale x 4 x i32>` 的 `add`：

```llvm
define <vscale x 4 x i32> @svadd(<vscale x 4 x i32> %a,
                                <vscale x 4 x i32> %b) {
entry:
  %r = add <vscale x 4 x i32> %a, %b
  ret <vscale x 4 x i32> %r
}
```

`-mattr=+v -global-isel -global-isel-abort=1` 能成功，选中结果是：

```yaml
%2:vrm2 = PseudoVADD_VV_M2 %3, %0, %1, -1 /* vl=VLMAX */, 5 /* e32 */, 3 /* ta, ma */
```

汇编里能看到 `vsetvli a0, zero, e32, m2, ta, ma` 与 `vadd.vv v8, v10, v12`；前后还有 `vs2r.v` / `vl2r.v` 的栈 spill，因为 scalable 向量按 ABI 是间接返回。

但把 `add` 换成 `mul`，同一个文件立刻在 IRTranslator 失败：

```text
LLVM ERROR: unable to translate instruction: mul (in function: svmul)
```

结论要说得足够窄：**RISC-V 23.1.4 的 GlobalISel 能处理一部分 scalable `G_ADD`，但 `G_MUL` 连翻译都没有**。不要把它推广成“RISC-V 向量 GlobalISel 已支持”。

### 15.4.4 源码与测试怎么对照

`RISCVInstrGISel.td` 的主体是 target 私有 GI pseudo 与 `GINodeEquiv`，例如 `G_SRAW`、`G_SRLW`、`G_DIVW`、`G_BREV8`、`G_CLZW`、`G_FCVT_*`、`G_VSLIDEDOWN_VL`。它不是 AArch64 那种“向量 pattern 大全”。

`RISCVInstructionSelector.cpp` 的 `select()` 先调用 `selectImpl`，再进入手写 switch。switch 里有 `G_CONSTANT`、`G_GLOBAL_VALUE`、`G_BRCOND`、`G_SELECT`、`G_IMPLICIT_DEF`、`G_UNMERGE_VALUES`、`G_LOAD`、`G_STORE`、`G_INTRINSIC`、`G_INTRINSIC_W_SIDE_EFFECTS`、`G_EXTRACT_SUBVECTOR`、`G_INSERT_SUBVECTOR`，但**没有** `G_BUILD_VECTOR`、`G_SHUFFLE_VECTOR`、`G_EXTRACT_VECTOR_ELT`、`G_INSERT_VECTOR_ELT`、`G_CONCAT_VECTORS`。这与实测一致：固定向量的结构操作没有 C++ 入口。

23.1.4 的测试树里有这些直接相关的文件（路径来自 `llvmorg-23.1.4`）：

- `llvm/test/CodeGen/RISCV/GlobalISel/rvv/vadd.ll` — 用 `llvm.riscv.vadd` intrinsic 测 RVV 加法；
- `llvm/test/CodeGen/RISCV/GlobalISel/instruction-select/rvv/add.mir` — 用 scalable `G_ADD` 测 `PseudoVADD_VV_*`；
- `llvm/test/CodeGen/RISCV/GlobalISel/rvv/fallback-rv64.ll` — 明确记录哪些 RVV 形态走 fallback；
- `llvm/test/CodeGen/RISCV/GlobalISel/riscv-unsupported.ll` — 记录 `byval` 参数、多寄存器 inline asm 等已知不支持项。

本章不编造覆盖率数字。能确认的只有：**RVV intrinsic 与部分 scalable 操作有测试；通用固定向量路径没有。**

### 15.4.5 标量 GlobalISel 是另一条线

不要因为向量失败就以为 RISC-V GlobalISel 整体不可用。标量实测：

```asm
add64:
	add	a0, a0, a1
	ret

ld32:
	lw	a0, 0(a0)
	ret
```

`select i1` 也能选，只是 `-O0` 下伴随大量 spill；这说明标量 Legalizer/Selector 已经能跑通，问题集中在向量类型与 ABI 边界。

---

## 15.5 踩雷清单

1. **把“目标支持 SVE/RVV”当成“GlobalISel 支持 scalable vector”**。AArch64 的 scalable 参数在 IRTranslator 就失败；RISC-V 的 scalable `G_MUL` 也在 IRTranslator 失败。
2. **以为 `+v` 就能选固定向量**。RISC-V 固定向量在 Legalizer 的 `G_INSERT_SUBVECTOR` 处失败，`vadd.vv` 是回退 SelectionDAG 的结果。
3. **把 `G_EXTRACT_VECTOR_ELT` 的动态下标交给 selector**。AArch64 的 `selectExtractElt` 只接受可看穿的常量下标和 FPR 结果；动态下标在 Legalizer 就被 spill 到栈上。
4. **把 intrinsic ID 当成第一个实参**。gMIR 里 def 在前，ID 是操作数 1（有结果时），C++ 读第一个实参要用 `getOperand(2)`。
5. **在 `selectShuffleVector` 里忽略字节展开**。mask 是元素下标，`TBL` 要的是字节下标；`BytesPerElt > 1` 时必须逐字节展开，否则 mask 全错。

---

## 15.6 进阶

- **读 `emitVectorConcat`**：它决定 64+64 与 128+128 两种拼接形态，也是 `G_CONCAT_VECTORS` 唯一被支持的入口。超过两个输入时，先问 Legalizer 是否已经拆开。
- **对照 AArch64 的 `tryOptConstantBuildVec`**：它把全常量 `G_BUILD_VECTOR` 直接变成常量池加载或 `MOVI`，绕过逐 lane insert。这是“C++ selector 里也做 peephole”的典型。
- **用 `-pass-remarks-missed='gisel*'` 看 fallback 原因**：RISC-V 的测试大量使用 `-global-isel-abort=2` 配合 remark，比只看最终汇编更能定位失败阶段。

---

## 15.7 动手练习

### 练习 15.1 观察 `G_BUILD_VECTOR` 的两条路径

把 15.2.1 的 MIR 改成四个相同输入 `%0`，先预测会不会变成 `G_DUP`，再跑 `-run-pass=instruction-select`。如果仍然是四条 insert，说明 `tryOptConstantBuildVec` 只处理常量，不处理“同寄存器重复”。

### 练习 15.2 让动态下标走常量路径

把 15.2.3 的 `%idx` 换成 `i32 2` 的 `G_CONSTANT`，对比 `-stop-before=instruction-select` 的输出：`G_EXTRACT_VECTOR_ELT` 是否重新出现？如果仍然被 spill，检查是不是 Legalizer 在更早的阶段就做了标量化。

### 练习 15.3 对比 AArch64 与 RISC-V 的 scalable add

分别运行：

```console
llc -mtriple=aarch64-linux-gnu -O0 -mattr=+sve -global-isel \
    -global-isel-abort=1 svadd.ll -o -
llc -mtriple=riscv64-linux-gnu -O0 -mattr=+v -global-isel \
    -global-isel-abort=1 svadd.ll -o -
```

记录两者失败/成功的阶段，并解释为什么同一个 IR 在 AArch64 连 IRTranslator 都过不去。

### 练习 15.4 找到 `saddv` 的 pattern 展开

在 `AArch64InstrGISel.td` 里读 `SIMDAcrossLanesSignedIntrinsicBHS`，回答：`v4i32` 的结果为什么要经过 `EXTRACT_SUBREG(..., ssub)`？如果去掉这层，寄存器 bank 会出什么问题？

---

## 15.8 重点整理

- gMIR 向量类型分固定与 scalable；固定向量的 lane 数是常量，scalable 的不是。
- AArch64 的 `G_BUILD_VECTOR`/`G_SHUFFLE_VECTOR`/`G_EXTRACT_VECTOR_ELT`/`G_CONCAT_VECTORS` 有 C++ 入口；`G_INSERT_VECTOR_ELT` 在 Legalizer 阶段被展开或标量化。
- `selectBuildVector` 默认发 lane insert；常量 splat 可能先被 Legalizer 折成 `G_DUP`。
- `selectShuffleVector` 用 `TBL` + 常量池，mask 必须按元素宽度展开成字节下标。
- `selectExtractElt` 只接受常量下标和 FPR 结果；动态下标会 spill 到栈上。
- intrinsic 的 ID 是操作数 1（有结果时），导入器用 `IntrinsicIDOperandMatcher` 匹配；简单 intrinsic 可以是纯 pattern，复杂展开要 C++。
- AArch64 的 scalable vector 在 IRTranslator 失败；`+sve` 只对 SelectionDAG 路径有效。
- RISC-V 的 `+v` 不等于通用向量 GlobalISel：固定向量在 Legalizer 失败，部分 scalable `G_ADD` 可用，`G_MUL` 在 IRTranslator 失败。

---

## 15.9 自我检核

1. `<4 x s32>` 与 `<vscale x 4 x s32>` 在 gMIR 里的根本区别是什么？
2. `G_SHUFFLE_VECTOR` 的第 3 个操作数是什么类型？为什么它不能是普通寄存器？
3. AArch64 为什么把 `G_BUILD_VECTOR` 放在 C++ 而不是纯 pattern？源码里的注释怎么说？
4. `selectExtractElt` 的两个硬限制是什么？
5. intrinsic ID 在 gMIR 操作数里的下标是多少？C++ 读第一个实参为什么用 2？
6. RISC-V 的固定向量 GlobalISel 在哪个指令上失败？这说明什么？
7. 为什么 `-global-isel-abort=0` 得到的 RISC-V 向量汇编不能直接当作 GlobalISel 的成功证据？

---

## 15.10 延伸阅读

- `llvm/include/llvm/Target/GenericOpcodes.td` — `G_BUILD_VECTOR`、`G_SHUFFLE_VECTOR`、`G_EXTRACT_VECTOR_ELT`、`G_CONCAT_VECTORS` 的操作数定义。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp` — `selectBuildVector`、`selectShuffleVector`、`selectExtractElt`、`selectConcatVectors`、`selectIntrinsic` 的真实实现。
- `llvm/lib/Target/AArch64/AArch64InstrGISel.td` — `SIMDAcrossLanesSignedIntrinsicBHS` 等 intrinsic pattern。
- `llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp` — `select()` 的 switch 与 `selectImpl` 的分工。
- `llvm/test/CodeGen/RISCV/GlobalISel/` — `rvv/vadd.ll`、`instruction-select/rvv/add.mir`、`riscv-unsupported.ll` 等实测边界。

---

## 下一章

[练习 C：ComplexPattern 迁移或手写 select case](./practice-c-complex-pattern-or-cpp-select.md) —— 把本章看到的“C++ 入口 + 常量池 + 目标伪指令”落到一个可验收的练习：以 AArch64 的寻址 ComplexPattern 为主线，补 `GIComplexOperandMatcher`、写测试、跑 `llc`，并解释为什么少一条 `ComplexPatternEquiv` 就会导入失败。
