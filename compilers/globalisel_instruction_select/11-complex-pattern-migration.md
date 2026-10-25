# 第 11 章 ComplexPattern 迁移：从 SDNode 匹配到 GIComplexOperandMatcher

> **本章目标**：把 SelectionDAG 的 `ComplexPattern` 翻译成 GlobalISel 的 `GIComplexOperandMatcher` + `GIComplexPatternEquiv`，读懂 `GIM_CheckComplexPattern` 的 `RendererID`/`PredicateID` 分工，并能判断哪些复杂模式根本不该迁移。
>
> **前置**：第 5–7 章，尤其 `GINodeEquiv` 与匹配表执行器。**环境**：LLVM 23.1.4，AArch64 主线，RISC-V 对照。

---

## 11.1 同一个地址模式，两套接口

AArch64 的 indexed addressing 在 SelectionDAG 侧定义于 `AArch64InstrFormats.td`：

```tablegen
def am_indexed8 : ComplexPattern<iPTR, 2, "SelectAddrModeIndexed8", []>;
```

三个关键信息：根操作数的类型是 `iPTR`；匹配后渲染 **2** 个机器操作数（base 与 scaled offset）；匹配函数 `SelectAddrModeIndexed8` 返回 `bool`，结果写进 `SmallVectorImpl<SDValue> &`。

GlobalISel 侧在同一个文件里另写一份：

```tablegen
def gi_am_indexed8 :
    GIComplexOperandMatcher<s64, "selectAddrModeIndexed<8>">,
    GIComplexPatternEquiv<am_indexed8>;
```

它把同一件事拆成两个接口：

- `GIComplexOperandMatcher<s64, ...>`：**怎么判**。根按 `s64` 这个 LLT 检查，函数返回 `ComplexRendererFns`；
- `GIComplexPatternEquiv<am_indexed8>`：**对应谁**。告诉导入器：SDAG 里用到 `am_indexed8` 的 pattern，可以改写成使用 `gi_am_indexed8` 的 GI pattern。

缺一不可。只写 matcher，不写 equiv，已有 SDAG pattern 不会自动带过来；只写 equiv，不写 matcher，导入器没有可调用的 GI 谓词。

### 11.1.1 返回值形状变了

SelectionDAG 的复杂模式返回 `bool`，并通过 `SmallVectorImpl<SDValue> &` 当场产出 DAG 节点；GlobalISel 的 matcher 返回一组延迟执行的渲染 lambda。匹配阶段不能建指令，只有匹配表走到 `GIR_BuildMI` / `GIR_ComplexRenderer` 时，lambda 才对新指令执行。这条约束是整章主线：**谓词只读，渲染后置**。

### 11.1.2 LLT 没有浮点类型

读 fixedpoint matcher 时会看到反直觉的写法：

```tablegen
def gi_fixedpoint_v4f32 : GIComplexOperandMatcher<v4s32, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v4f32>;
```

模式名是 `v4f32`，GI matcher 的类型却写 `v4s32`。LLT 只描述位宽与形状，不区分整数和浮点；`<4 x f32>` 在 MIR 里就是 `<4 x s32>`，浮点语义由寄存器 bank（FPR）和操作码决定。

迁移时不要照抄 SelectionDAG 的 `MVT` 名字。先问："这个值在 gMIR 里的 LLT 是什么？"

---

## 11.2 `GIM_CheckComplexPattern` 的四个操作数

匹配表里的指令格式：

```text
GIM_CheckComplexPattern InsnID OpIdx RendererID Pred(2)
```

执行器实现（`GIMatchTableExecutorImpl.h`，23.1.4）：

```cpp
case GIM_CheckComplexPattern: {
  uint64_t InsnID = readULEB();
  uint64_t OpIdx = readULEB();
  uint16_t RendererID = readU16();
  uint16_t ComplexPredicateID = readU16();

  assert(State.MIs[InsnID] != nullptr && "Used insn before defined");
  ComplexRendererFns Renderer =
      (Exec.*ExecInfo.ComplexPredicates[ComplexPredicateID])(
          State.MIs[InsnID]->getOperand(OpIdx));
  if (Renderer)
    State.Renderers[RendererID] = *Renderer;
  else if (handleReject() == RejectAndGiveUp)
    return false;
  break;
}
```

| 字段 | 含义 |
| --- | --- |
| `InsnID` | 已记录指令的编号，通常是 `MIs[0]` 或被 `GIM_RecordInsn` 记录的生产者 |
| `OpIdx` | 该指令的第几个操作数作为复杂模式根 |
| `RendererID` | 匹配成功后，渲染函数列表存到 `State.Renderers` 的哪个槽 |
| `PredicateID` | 调用 `ExecInfo.ComplexPredicates` 里的哪个 matcher |

### 11.2.1 为什么要有两个 ID

`PredicateID` 是**静态函数表**下标。TableGen 把所有 `GIComplexOperandMatcher` 的 `MatcherFn` 收集成一张表，执行器通过它调用 C++。

`RendererID` 是**本次匹配状态**下标。匹配成功时，matcher 返回的 lambda 列表存进 `State.Renderers[RendererID]`；后面 `GIR_ComplexRenderer` 按同一编号取出来：

```cpp
for (const auto &RenderOpFn : State.Renderers[RendererID])
  RenderOpFn(OutMIs[InsnID]);
```

同一个 matcher 可被多条规则复用，所以 `PredicateID` 相同；但复杂操作数在每条规则里的槽位不同，`RendererID` 必须按槽位分配。

### 11.2.2 失败就是回溯

matcher 返回 `std::nullopt` 时，执行器调用 `handleReject()`：回溯栈非空就跳到下一条候选规则，栈空才让 `selectImpl` 返回 false。复杂谓词必须"宁缺毋滥"——不确定能安全渲染时返回失败，不要把错误推迟到渲染阶段。

---

## 11.3 `GIComplexOperandMatcher` 的契约

`Target.td` 的注释：

```tablegen
class GIComplexOperandMatcher<LLT type, string matcherfn> {
  // The expected type of the root of the match.
  //
  // TODO: We should probably support, any-type, any-scalar, and multiple types
  //       in the future.
  LLT Type = type;

  // The function that determines whether the operand matches. It should be of
  // the form:
  //   ComplexRendererFn select(MachineOperand &Root) const;
  // where Root is the root of the match.  The function should return nullptr
  // on match failure, or a ComplexRendererFn that renders the operand in case
  // of a successful match.
  string MatcherFn = matcherfn;
}
```

四个约束：

1. **类型是精确 LLT**。23.1.4 不支持 `any`、`any-scalar` 或一个 matcher 覆盖多种类型；覆盖五种向量类型就写五个定义。
2. **函数是 `const`**。只能读 MRI、MachineFunction 和 subtarget，不能改 MIR。
3. **失败返回 `std::nullopt`**，成功返回非空 vector，长度与 pattern 期望的操作数个数一致。
4. **lambda 按值捕获**。matcher 返回后栈帧销毁，按引用捕获会悬空。

### 11.3.1 渲染函数的参数是 builder

renderer 的签名是 `void(MachineInstrBuilder &)`，只负责往正在构建的指令上追加一个操作数：

```cpp
return {{
    [=](MachineInstrBuilder &MIB) { MIB.add(Base); },
    [=](MachineInstrBuilder &MIB) { MIB.addImm(Offset >> Scale); },
}};
```

`Base` 是 `MachineOperand`，`MIB.add(Base)` 会复制它。不要改成 `MIB.addReg(Base.getReg())`，除非确认目标操作数只需要寄存器，否则会丢掉 subreg、target flags 等信息。

### 11.3.2 复杂模式可以只产出一个立即数

fixedpoint 就是这种形态：pattern 里 `fixedpoint:$scale` 只贡献移位量，matcher 返回的 vector 只有一个 lambda。不要因为叫 "Complex" 就以为一定要渲染 base + offset；复杂度在判定，不在产出个数。

---

## 11.4 案例一：AArch64 fixedpoint 的 `v2f64` / `v2f32`

### 11.4.1 TableGen 侧

`AArch64InstrInfo.td`（23.1.4，约 9263 行）：

```tablegen
def fixedpoint_v2f64 : fixedpoint_vec_f64<v2f64>;
def fixedpoint_v2f32 : fixedpoint_vec_f32<v2f32>;
def fixedpoint_v4f32 : fixedpoint_vec_f32<v4f32>;
def fixedpoint_v4f16 : fixedpoint_vec_f16<v4f16>;
def fixedpoint_v8f16 : fixedpoint_vec_f16<v8f16>;

def gi_fixedpoint_v2f64 : GIComplexOperandMatcher<v2s64, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v2f64>;
def gi_fixedpoint_v2f32 : GIComplexOperandMatcher<v2s32, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v2f32>;
def gi_fixedpoint_v4f32 : GIComplexOperandMatcher<v4s32, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v4f32>;
def gi_fixedpoint_v4f16 : GIComplexOperandMatcher<v4s16, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v4f16>;
def gi_fixedpoint_v8f16 : GIComplexOperandMatcher<v8s16, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v8f16>;
```

`v2f64` 对应 `v2s64`，`v4f16` 对应 `v4s16`：LLT 只保留位宽与 lane 数。五个定义共享同一个 C++ matcher，因为判定逻辑相同，只有根类型不同。

被导入的 pattern 来自 `FCVTPat`：

```tablegen
def : Pat<(IVT (fp_to_sint (FVT (fmul FVT:$Vn, fixedpoint:$scale)))),
          (!cast<Instruction>("FCVTZS"#IVT#"_shift")
           $Vn, (fixedpoint_vec_xform fixedpoint:$scale))>;
```

根是 `fp_to_sint`，输入是 `fmul`，右操作数匹配 `fixedpoint`，匹配成功后通过 `fixedpoint_vec_xform` 变成移位立即数。

### 11.4.2 SDNodeXForm 也要有 GI 等价物

`fixedpoint_vec_xform` 是只改类型的 `SDNodeXForm`。GlobalISel 用两层替代：

```tablegen
def gi_fixedpoint_vec_xform : GICustomOperandRenderer<"renderFixedPointXForm">,
  GISDNodeXFormEquiv<fixedpoint_vec_xform>;
```

`GISDNodeXFormEquiv` 告诉导入器"这个 xform 有 GI 版本"，`GICustomOperandRenderer` 指定真正执行的 C++ 函数。23.1.4 的实现有一个说明问题的 FIXME：

```cpp
// 23.1.4 中该函数会重新调用 selectCVTFixedPointVecBase，
// 再执行返回的 renderer；源码里有对应 FIXME。
```

它没有复用 `GIM_CheckComplexPattern` 已算好的 renderer，而是重新匹配一遍。原因是 SDNodeXForm 在 TableGen 里是独立节点，导入器需要单独的类型检查路径。新增代码应把共享逻辑抽成 `...Base`，不要重复匹配。

### 11.4.3 C++ matcher 在做什么

`selectCVTFixedPointVec` 直接转调 `selectCVTFixedPointVecBase`，核心步骤：

```cpp
MachineInstr *Dup = getDefIgnoringCopies(Root.getReg(), MRI);
if (Dup->getOpcode() != AArch64::G_DUP)
  return std::nullopt;
auto CstVal = getAnyConstantVRegValWithLookThrough(Dup->getOperand(1).getReg(), MRI);
if (!CstVal)
  return std::nullopt;

// 按 16/32/64 位宽把整数位模式解释成 APFloat
if (unsigned FBits = CheckFixedPointOperandConstant(FVal, RegWidth, isReciprocal))
  return {{[=](MachineInstrBuilder &MIB) { MIB.addImm(FBits); }}};
return std::nullopt;
```

它做三件事：确认根是 `G_DUP` 的 splat、取出标量常量并按位宽解释成 `APFloat`、检查这个常量是否是目标能编码的 2 的幂（或其倒数）。任一步失败就回溯。注意 `Root.getReg()` 是 `fmul` 的右操作数：如果 gMIR 把常量直接写成 `G_CONSTANT` 而不是 `G_DUP`，matcher 不会命中。

### 11.4.4 可跑的 MIR：`pr204118.mir`

上游测试 `llvm/test/CodeGen/AArch64/GlobalISel/pr204118.mir` 的 RUN 行：

```bash
llc -o - -mtriple arm64-apple-ios -run-pass=instruction-select %s | FileCheck %s
```

输入（节选）：

```mir
%1:fpr(<2 x i64>) = COPY $q0
%0:fpr(<4 x f32>) = G_BITCAST %1(<2 x i64>)
%3:fpr(f32) = G_FCONSTANT float 2.000000e+00
%2:fpr(<4 x f32>) = G_DUP %3(f32)
%4:fpr(<4 x f32>) = G_FMUL %2, %0
%5:fpr(<4 x i32>) = G_FPTOSI %4(<4 x f32>)
$q0 = COPY %5(<4 x i32>)
```

上游 `CHECK` 的期望输出：

```mir
[[COPY:%[0-9]+]]:fpr128 = COPY $q0
[[FCVTZSv4i32_shift:%[0-9]+]]:fpr128 = FCVTZSv4i32_shift [[COPY]], 1
$q0 = COPY [[FCVTZSv4i32_shift]]
```

`G_DUP 2.0f` 提供 `fixedpoint` 常量，`G_FMUL` 命中 pattern，`G_FPTOSI` 被选成带移位 1 的 `FCVTZSv4i32_shift`。这个例子说明：复杂模式匹配的是 `fmul` 的右操作数，却服务于 `fptosi` 的渲染。

> 说明：本章 MIR 输出来自 23.1.4 上游测试文件的 CHECK 期望。本机没有构建 `llc`，正文不声称这些命令已在本地执行。

## 11.5 案例二：indexed addressing 与 scaled offset

### 11.5.1 声明

`AArch64InstrFormats.td`：

```tablegen
def am_indexed8 : ComplexPattern<iPTR, 2, "SelectAddrModeIndexed8", []>;
def gi_am_indexed8 :
    GIComplexOperandMatcher<s64, "selectAddrModeIndexed<8>">,
    GIComplexPatternEquiv<am_indexed8>;

def am_unscaled8 : ComplexPattern<iPTR, 2, "SelectAddrModeUnscaled8", []>;
def gi_am_unscaled8 :
    GIComplexOperandMatcher<s64, "selectAddrModeUnscaled<8>">,
    GIComplexPatternEquiv<am_unscaled8>;
```

同一文件还有 `ro_Xindexed64` / `gi_ro_Xindexed64` 这样的 register-offset 模式。它们共同覆盖四类内存操作数：

1. `[base, #imm]`：unsigned scaled 12-bit；
2. `[base, #imm]!` / `[base], #imm`：unscaled 9-bit；
3. `[base, index]`：register offset；
4. `[base, index, lsl #s]`：shifted register offset。

### 11.5.2 `selectAddrModeIndexed` 的真实逻辑

`AArch64InstructionSelector.cpp`（约 7665 行）的关键分支：

```cpp
if (!Root.isReg())
  return std::nullopt;

MachineInstr *RootDef = MRI.getVRegDef(Root.getReg());
if (RootDef->getOpcode() == TargetOpcode::G_FRAME_INDEX)
  return {{ add(RootDef->getOperand(1)), addImm(0) }};

if (CM == CodeModel::Small) {
  if (auto OpFns = tryFoldAddLowIntoImm(*RootDef, Size, MRI))
    return OpFns;
}

if (isBaseWithConstantOffset(Root, MRI)) {
  int64_t RHSC = (int64_t)RHSDef->getOperand(1).getCImm()->getZExtValue();
  unsigned Scale = Log2_32(Size);
  if ((RHSC & (Size - 1)) == 0 && RHSC >= 0 && RHSC < (0x1000 << Scale))
    return {{ add(LHS), addImm(RHSC >> Scale) }};
}

if (selectAddrModeUnscaled(Root, Size))
  return std::nullopt;

return {{ add(Root), addImm(0) }};
```

四个判定点：

| 条件 | 处理 |
| --- | --- |
| `G_FRAME_INDEX` | 直接渲染 `fi` + `0` |
| small code model 的 `ADRP + G_ADD_LOW` | 尝试把 low offset 折进立即数 |
| `G_ADD base, const` 且对齐、非负、在 scaled 12-bit 内 | base + `RHSC >> log2(Size)` |
| 以上都不满足，但 unscaled 能处理 | 返回 `nullopt`，让 indexed 规则拒绝，交给 unscaled 规则 |
| 仍是普通寄存器 | base + `0` |

`Size` 是**访问宽度（字节）**，来自 load/store 的 memory size，不是猜测的 `sizeof`。`scale = log2(Size)`，上界是 `0x1000 << Scale`，即编码字段 12 位乘以访问宽度。

### 11.5.3 可查的 MIR：偏移按访问宽度缩放

上游 `select-load.mir`：

```mir
; i64 load at byte offset 128
%val:gpr(i64) = G_LOAD %ptr(p0) :: (load (i64) from %ir.addr)
```

期望：

```mir
[[LDRXui:%[0-9]+]]:gpr64 = LDRXui [[COPY]], 16 :: (load (i64) from %ir.addr)
```

128 字节 / 8 字节 = 16。i32 load 在字节偏移 512 时得到 `LDRWui [[COPY]], 128`，因为 512 / 4 = 128。直接照抄字节偏移会编码溢出或地址错位。

### 11.5.4 可查的 MIR：register offset

`load-addressing-modes.mir`：

```mir
%addr:gpr(p0) = COPY $x0
%idx:gpr(i64) = COPY $x1
%ptr:gpr(p0) = G_PTR_ADD %addr, %idx
%val:gpr(i64) = G_LOAD %ptr(p0) :: (load (s64) from %ir.addr)
```

期望：

```mir
[[LDRXroX:%[0-9]+]]:gpr64 = LDRXroX [[COPY]], [[COPY1]], 0, 0 :: (load (s64) from %ir.addr)
```

末尾两个 `0` 是 shift type 与 shift amount。如果 `G_PTR_ADD` 的结果还有别的 use，选择器不会把它折进 load，而是先选一条 `ADDXrr`，再用 `LDRXui ... 0`；上游用 `more_than_one_use` 系列用例覆盖这个边界。

### 11.5.5 `return std::nullopt` 不是失败，是换规则

`selectAddrModeIndexed` 在 unscaled 能处理时返回空，实际是**主动让位**：当前 pattern 要求 indexed 形式，而这个地址更适合 unscaled，所以 indexed 规则应当拒绝，让带 `gi_am_unscaled8` 的规则命中。一个 matcher 只负责自己那一类编码；不要为了多匹配把不同编码塞进同一个 matcher，否则渲染出的操作数与目标指令的编码约束对不上。

---

## 11.6 `G_ADD_LOW`、LEA 类模式与地址折叠

`G_ADD_LOW` 不是 `ComplexPattern`，但它是理解"复杂地址为什么不能只靠 pattern"的关键。

### 11.6.1 谁产生 `G_ADD_LOW`

`AArch64InstrGISel.td`：

```tablegen
def G_ADD_LOW : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src, type2:$imm);
  let hasSideEffects = 0;
}
```

Legalizer 的 `legalizeSmallCMGlobalValue` 把小代码模型的 `G_GLOBAL_VALUE` 拆成：

```text
%page = ADRP target-flags(aarch64-page) @g
%addr = G_ADD_LOW %page, target-flags(aarch64-pageoff, aarch64-nc) @g
```

它没有 `GINodeEquiv`，不是从 SDNode pattern 导入的，而是 target 在 legalize 阶段产生的 GI 伪指令。

### 11.6.2 地址 matcher 如何利用它

`selectAddrModeIndexed` 在 small code model 下调用 `tryFoldAddLowIntoImm`。后者检查：根是 `G_ADD_LOW`；其来源是 `ADRP`；`offset % Size == 0`；全局变量非 TLS；指针对齐至少 `Size`。满足后返回两个 renderer：一个使用 ADRP 的目标寄存器，一个添加带 `MO_PAGEOFF | MO_NC` 的全局地址。于是 load/store 直接写成 `[xN, #:lo12:symbol]`，少一条 `ADDXri`。

这就是 AArch64 上的 LEA 类复杂模式：legalize 产生可折叠伪指令，C++ matcher 在内存操作数上做最后判断，expand 再处理剩余 pseudo。迁移时优先采用"pseudo + matcher + expand"三段式，而不是一个巨型 ComplexPattern。

### 11.6.3 RISC-V 对照

RISC-V 的寻址拆分与 AArch64 不同，不能假设 `am_indexed8` 这类名字或等价 matcher 一定存在。迁移前先 grep 目标后端的 `GIComplexOperandMatcher`，再读 Legalizer 对 `G_GLOBAL_VALUE` / `G_PTR_ADD` 的处理。若只有 SDAG 的 `ComplexPattern` 而没有 GI 对应物，正确做法是补 matcher 或保留 fallback，而不是硬搬 SDAG 的函数签名。

---

## 11.7 迁移步骤

**第一步：确认等价 predicate。** 在目标后端搜索 `GIComplexOperandMatcher`，再对照 SDAG 的 `ComplexPattern`。如果已有 matcher，直接复用；如果没有，先判断它能否用现有 `GIM_*` 检查（opcode、类型、bank、立即数）表达。能表达就不需要新 matcher。

**第二步：确定根操作数的 LLT。** 不要照抄 `iPTR` / `v4f32`。从 legalizer 后的 gMIR 取实际类型：指针常是 `p0`，浮点向量是 `sN` 的向量，标量常量可能是 `s32` 或 `s64`。`GIComplexOperandMatcher` 的 `Type` 必须与 `GIM_CheckType` 的期望一致。

**第三步：写 matcher。** 函数签名固定为 `ComplexRendererFns f(MachineOperand &Root) const`。流程通常是：

1. 检查 `Root.isReg()`；
2. 通过 `MRI.getVRegDef` / `getDefIgnoringCopies` 找到生产者；
3. 检查 opcode 与操作数形状；
4. 检查常量范围、对齐、bank；
5. 返回按值捕获的 renderer 列表；失败返回 `std::nullopt`。

不要在 matcher 里 `buildInstr`、`eraseFromParent` 或修改 MRI。需要多步变换时，先返回一个 renderer，把变换留给渲染阶段或后续 combine。

**第四步：注册 equiv。**

```tablegen
def gi_my_pattern : GIComplexOperandMatcher<s64, "selectMyPattern">,
                    GIComplexPatternEquiv<my_pattern>;
```

`GIComplexPatternEquiv` 的模板参数必须是已有的 SDAG `ComplexPattern`。名字不必相同，但类型与操作数个数必须匹配。

**第五步：处理 SDNodeXForm。** 如果 pattern 里还有 `SDNodeXForm`，只注册 `GIComplexPatternEquiv` 不够。需要：

```tablegen
def gi_my_xform : GICustomOperandRenderer<"renderMyXForm">,
                  GISDNodeXFormEquiv<my_xform>;
```

并在 C++ 里实现 `renderMyXForm(MachineInstrBuilder &, const MachineInstr &, int OpIdx)`。注意它的 `OpIdx` 语义：`-1` 表示匹配的是指令定义值，非负表示源操作数。

**第六步：先跑 `-gen-global-isel`。**

```bash
llvm-tblgen -gen-global-isel -I ... AArch64.td -o /tmp/AArch64GenGlobalISel.inc
```

如果 equiv 类型或操作数个数不匹配，这一阶段就会报错，比在 `llc` 里看 fallback 快得多。生成文件里搜索 `GIM_CheckComplexPattern`，确认 `RendererID` 与后续 `GIR_ComplexRenderer` 的编号成对出现。

**第七步：写 MIR 测试。** 最小测试应包含：

```yaml
# RUN: llc -mtriple=aarch64 -run-pass=instruction-select \
# RUN:   -verify-machineinstrs %s -o - | FileCheck %s
```

加 `-global-isel-abort=1` 可以把"悄悄 fallback 到 SelectionDAG"变成失败，适合 CI；本地调试时可以先不加，观察 fallback 行为。

**第八步：覆盖失败路径。** 至少补三类反例：类型不对、常量不在编码范围、地址有多个 use。复杂模式最常见的回归不是"能匹配"，而是"不该匹配时也匹配了"，导致后续 pass 生成非法指令。

---

## 11.8 哪些 ComplexPattern 无法直接迁移

以下情形通常不能靠 `GIComplexOperandMatcher` 直接迁移：

1. **依赖 SDNode 的链或 glue**。GI 没有 SDAG 的 chain/glue 概念，内存副作用由 MMO 与指令本身表达。
2. **一个模式产出多条指令**。matcher 只能返回操作数 renderer，不能建多条 MI；这种逻辑应放到 C++ selector 或 pre-legalizer combine。
3. **类型是 `any` / 多类型**。23.1.4 的 `GIComplexOperandMatcher` 只支持精确 LLT，需要拆成多个定义。
4. **依赖 `SDValue` 的 `getNode()`、`getOperand()` 或 DAG 组合器**。GI 的根是 `MachineOperand`，没有等价的 DAG 视图。
5. **匹配过程中要修改 DAG 或创建节点**。违反"谓词只读"约束，应改为渲染阶段或独立 pass。

遇到这些情况，优先问："这个模式的目标是选择一条指令，还是完成一次 IR 级变换？" 后者通常属于 legalizer/combine，不属于 InstructionSelect。

---

## 踩雷清单

1. **把 `GIComplexPatternEquiv` 当成运行时映射**。它只影响 TableGen 导入；运行时匹配靠 matcher 与生成表。
2. **`Type` 写成 `i64` 或 `v4f32`**。TableGen 的 GI 类型是 `s64`、`v4s32` 这类 LLT 名，不是 MVT 名。
3. **renderer 按引用捕获局部变量**。matcher 返回后引用悬空，表现为随机崩溃或错误操作数。
4. **在 matcher 里建指令**。匹配阶段只能读；建指令会让回溯后的状态不一致。
5. **忘记 `GIComplexPatternEquiv`**。matcher 定义了，但原 SDAG pattern 不会进入 GI 匹配表，表现为"代码写了却没生效"。

---

## 进阶

- 读生成文件里的 `GIM_CheckComplexPattern` 与 `GIR_ComplexRenderer`，确认 `RendererID` 的分配是否符合"一个复杂操作数一个槽"。
- 给 matcher 加 `LLVM_DEBUG`，用 `-debug-only=instruction-select` 观察它在哪一步拒绝。注意需要 assertions 构建。

---

## 动手练习

### 练习 11.1 追一条 fixedpoint 规则

在 23.1.4 的 `AArch64InstrInfo.td` 找到 `gi_fixedpoint_v4f32`，写出它对应的 SDAG `ComplexPattern`、`SDNodeXForm`、GI matcher、GI renderer 四个名字，并说明哪一个负责类型检查、哪一个负责产出立即数。

### 练习 11.2 手算 scaled offset

对 `load i64` 的字节偏移 128、256、4096，分别算出 `LDRXui` 的立即数字段；再算 `load i32` 的字节偏移 512。哪些会超出 unsigned scaled 12-bit？超出后应由哪个 matcher 接手？

### 练习 11.3 构造失败用例

写一个 MIR，把 fixedpoint matcher 的 `G_DUP` 换成 `G_CONSTANT`，观察 `-global-isel-abort=1` 下是否 fallback。解释 matcher 的哪一行拒绝。

### 练习 11.4 RISC-V 审计

在 RISC-V 后端 grep `GIComplexOperandMatcher`，挑一个 SDAG `ComplexPattern`，判断它是否有 GI 等价物。如果没有，写出你会补 matcher 还是保留 fallback，并说明理由。

---

## 重点整理

- SelectionDAG `ComplexPattern` 是"当场产出 SDValue"；GI 版本是"返回延迟渲染 lambda"。
- `GIM_CheckComplexPattern` 的 `PredicateID` 选 matcher，`RendererID` 选本次匹配的 renderer 槽。
- `GIComplexOperandMatcher` 的 `Type` 是精确 LLT；LLT 不区分整数/浮点。
- `GIComplexPatternEquiv` 负责导入，`GISDNodeXFormEquiv` + `GICustomOperandRenderer` 负责 xform。
- 地址模式的 `Size` 是访问宽度，offset 要按 `log2(Size)` 缩放。
- `G_ADD_LOW` 走的是 pseudo + matcher + expand，不是 ComplexPattern 的直接替代。
- 无法迁移的常见原因：chain/glue、多指令产出、any 类型、DAG 专属 API。

---

## 自我检核

1. `GIM_CheckComplexPattern` 的四个操作数分别是什么？
2. 为什么 `PredicateID` 与 `RendererID` 不能合并？
3. fixedpoint matcher 为什么用 `v4s32` 而不是 `v4f32`？
4. `pr204118.mir` 里 `FCVTZSv4i32_shift` 的移位 1 是从哪里来的？
5. `LDRXui` 的立即数 16 对应多少字节偏移？为什么？
6. `selectAddrModeIndexed` 在 unscaled 能处理时为什么返回 `nullopt`？

---

## 延伸阅读

- `llvm/lib/Target/AArch64/AArch64InstrInfo.td`：fixedpoint 的 `ComplexPattern`、`SDNodeXForm`、`FCVTPat` 与 GI 等价定义。
- `llvm/lib/Target/AArch64/AArch64InstrFormats.td`：`am_indexed8`、`am_unscaled8`、`ro_Xindexed*` 及对应 GI matcher。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`：`selectAddrModeIndexed`、`tryFoldAddLowIntoImm`、fixedpoint matcher。
- `llvm/include/llvm/Target/GlobalISel/Target.td`：`GIComplexOperandMatcher` 与 `GICustomOperandRenderer` 的接口注释。
- `llvm/include/llvm/CodeGen/GlobalISel/GIMatchTableExecutorImpl.h`：`GIM_CheckComplexPattern` 与 `GIR_ComplexRenderer` 的执行语义。

---

## 下一章

第 12 章进入 target-specific 世界：**Custom SDNode、pseudo 与 `GINodeEquiv`**。我们会看 `G_REV32` 如何从 `G_SHUFFLE_VECTOR` 产生、`G_ADD_LOW` 如何变成 `MOVaddr`，以及 `GINodeEquiv` 的每个字段在 23.1.4 里的真实含义。继续阅读：[`./12-custom-node-pseudo.md`](./12-custom-node-pseudo.md)。
