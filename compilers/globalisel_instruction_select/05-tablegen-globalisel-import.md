# 第 5 章 TableGen 如何把 pattern 导入 GlobalISel

> **本章目标**：搞清 `G_ADD` 是怎样"自动"变成 `ADDXrr` 的——`.td` 里的 SelectionDAG pattern 如何被 `-gen-global-isel` 重新解释成 GlobalISel 匹配表，以及哪些 pattern 导不进来、要用手写替代品。
>
> **前置**：第 2、4 章。**环境**：LLVM 23.1.4。

---

## 5.1 一个 pattern 的两半

AArch64 的 `ADDXrr` 定义在 `AArch64InstrFormats.td` 的多类里：

```tablegen
class BaseAddSubRegPseudo<RegisterClass regtype, SDPatternOperator OpNode>
    : ...,
      [(set regtype:$Rd, (OpNode regtype:$Rn, regtype:$Rm))] ...;
```

`ADD` 这个 `defm` 把 `OpNode` 绑到 `add`，于是生成了"`add GPR64, GPR64` → `ADDXrr`"的 SelectionDAG pattern。

GlobalISel 完全不认识 `add` 这个 SDNode。它认识的是 `G_ADD`。两者的桥梁在 `SelectionDAGCompat.td`：

```tablegen
def : GINodeEquiv<G_ADD, add>;
```

可以把它读成一句话：

> "IR 里的 `add`（在 gMIR 里是 `G_ADD`）与 SelectionDAG 的 `add` 节点是同一个操作。"

有了这条声明，TableGen 的 GlobalISel 后端就能把 `ADDXrr` 的 pattern 重新解释为 `G_ADD → ADDXrr`。

### 5.1.1 导入的三个前提

一条 pattern 能被导入，至少要满足：

1. **根节点有 `GINodeEquiv`**。没有映射的 SDNode（比如某些 target 私有节点）不会被导入。
2. **类型能映射到 LLT**。SelectionDAG 的 `MVT`/`EVT` 要被翻译成 `sN`/`pN`/`<N x sM>`。
3. **谓词能用 GlobalISel 的形式表达**。`PatLeaf` 的 C++ 谓词基于 `SDNode`，无法直接复用。

第 3 条是移植时最大的坑，5.5 节展开。

---

## 5.2 导入流水线

`GlobalISelEmitter.cpp` 的文件头把流程写得很清楚：

```text
This file analyzes the patterns recognized by the SelectionDAGISel tablegen
backend, filters out the ones that are unsupported, maps
SelectionDAG-specific constructs to their GlobalISel counterpart
(when applicable: MVT to LLT; SDNode to generic Instruction).
```

四步：

1. **复用 SelectionDAG 的 pattern 解析**：同一套 `Pat<>`、`ComplexPattern`、`ImmLeaf`；
2. **过滤不支持的**：`PatLeaf`、无法映射的 SDNode、无法表达的谓词；
3. **映射**：`MVT → LLT`、`SDNode → G_*`、`RegisterClass → bank/regclass`；
4. **发射**：生成匹配表与 `selectImpl`。

后端的注册名是 `gen-global-isel`（`GlobalISelEmitter.cpp` 末尾）：

```cpp
static TableGen::Emitter::OptClass<GlobalISelEmitter>
    X("gen-global-isel", "Generate GlobalISel selector");
```

注意官方文档里写的是 `-gen-globalisel`，实际参数是 `-gen-global-isel`；以 `llvm-tblgen --help` 为准。

### 5.2.1 生成物只有一个方法

文件头注释：

```text
The generated file defines a single method:
    bool <Target>InstructionSelector::selectImpl(MachineInstr &I) const;
intended to be used in InstructionSelector::select as the first-step
selector for the patterns that don't require complex C++.
```

生成的文件叫 `<Target>GenGlobalISel.inc`，通过宏分段包含：

```cpp
#define GET_GLOBALISEL_PREDICATE_BITSET
#include "AArch64GenGlobalISel.inc"
#undef GET_GLOBALISEL_PREDICATE_BITSET

class AArch64InstructionSelector : public InstructionSelector {
  ...
#define GET_GLOBALISEL_PREDICATES_DECL
#include "AArch64GenGlobalISel.inc"
#undef GET_GLOBALISEL_PREDICATES_DECL

#define GET_GLOBALISEL_TEMPORARIES_DECL
#include "AArch64GenGlobalISel.inc"
#undef GET_GLOBALISEL_TEMPORARIES_DECL
};

#define GET_GLOBALISEL_IMPL
#include "AArch64GenGlobalISel.inc"
#undef GET_GLOBALISEL_IMPL
```

宏的名字就是"这份 `.inc` 的哪一段"：谓词位集、谓词声明、临时变量声明、实现。

`selectImpl` 的实现由发射器拼出来（`GlobalISelEmitter.cpp`）：

```cpp
  OS << "  const PredicateBitset AvailableFeatures = getAvailableFeatures();\n"
     << "  MachineIRBuilder B(I);\n"
     << "  State.MIs.clear();\n"
     << "  State.MIs.push_back(&I);\n\n"
     << "  if (executeMatchTable(*this, State, ExecInfo, B"
     << ", getMatchTable(), TII, MF->getRegInfo(), TRI, RBI, AvailableFeatures"
     << ", &CoverageInfo)) {\n"
     << "    return true;\n"
     << "  }\n\n"
     << "  return false;\n";
```

所以 `selectImpl` 本身没有选择逻辑，它只负责：

1. 收集当前可用的 subtarget 特性（谓词位集）；
2. 把当前指令放进匹配状态；
3. 交给 `executeMatchTable` 跑表。

匹配表的内容与执行器是第 6 章的主题。

---

## 5.3 `GINodeEquiv` 的全部字段

`SelectionDAGCompat.td`：

```tablegen
class GINodeEquiv<Instruction i, SDNode node> {
  Instruction I = i;
  SDNode Node = node;

  bit CheckMMOIsNonAtomic = false;
  bit CheckMMOIsAtomic = false;

  Instruction IfSignExtend = ?;
  Instruction IfZeroExtend = ?;
  Instruction IfFPExtend = ?;
  Instruction IfFPTrunc = ?;

  Instruction IfFloatingPoint = ?;
}
```

除了"`i` 对应 `node`"，它还能表达 SelectionDAG 与 GlobalISel 的**结构差异**。

### 5.3.1 一个 SDNode 对多个 G_*

SelectionDAG 用一个 `ld` 节点加谓词表示所有加载；GlobalISel 用不同操作码。`SelectionDAGCompat.td` 的实际写法是：

```tablegen
def : GINodeEquiv<G_LOAD, ld> {
  let CheckMMOIsNonAtomic = true;
  let IfSignExtend = G_SEXTLOAD;
  let IfZeroExtend = G_ZEXTLOAD;
  let IfFPExtend = G_FPEXTLOAD;
}

def : GINodeEquiv<G_STORE, st> {
  let CheckMMOIsNonAtomic = true;
  let IfFPTrunc = G_FPTRUNCSTORE;
}
```

也就是说，同一条 SelectionDAG `ld` pattern，会按谓词分叉成 `G_LOAD`、`G_SEXTLOAD`、`G_ZEXTLOAD`、`G_FPEXTLOAD` 四种 gMIR 操作码。

同理，`setcc` 分叉成 `G_ICMP` / `G_FCMP`：

```tablegen
def : GINodeEquiv<G_ICMP, setcc> {
  let IfFloatingPoint = G_FCMP;
}
```

### 5.3.2 原子性

SelectionDAG 区分 `load` 与 `atomic_load`，GlobalISel 把原子性放在内存操作数（MMO）里。`CheckMMOIsAtomic` / `CheckMMOIsNonAtomic` 就是让导入的 pattern 额外检查 MMO 的标志。

这解释了一个常见现象：同一条 `.td` pattern 在 SelectionDAG 里匹配普通 load，在 GlobalISel 里需要额外的 MMO 条件才不会被误用。

---

## 5.4 每条 G_* 的映射表

`SelectionDAGCompat.td` 是一张长表，摘几条：

```tablegen
def : GINodeEquiv<G_CONSTANT, imm>;
def : GINodeEquiv<G_FCONSTANT, fpimm>;
def : GINodeEquiv<G_ADD, add>;
def : GINodeEquiv<G_SUB, sub>;
def : GINodeEquiv<G_MUL, mul>;
def : GINodeEquiv<G_SELECT, select>;
def : GINodeEquiv<G_SELECT, vselect>;
def : GINodeEquiv<G_ICMP, setcc> { let IfFloatingPoint = G_FCMP; }
```

读这张表能快速回答"这个 `G_*` 有没有 TableGen pattern"：有 `GINodeEquiv` 才可能有导入的 pattern；没有的只能靠 C++ 选择器。

例如 `G_PHI`、`COPY`、`G_BRCOND` 不在表里——它们是 MIR 层的控制流，由 C++ 或直接透传处理。

---

## 5.5 导不进来的东西

### 5.5.1 PatLeaf → ImmLeaf / IntImmLeaf / FPImmLeaf

`PatLeaf` 的谓词写成 C++，参数通常是 `SDNode`，GlobalISel 无法复用。文档建议：

> PatLeafs that handle immediate predicates should be replaced by `ImmLeaf`, `IntImmLeaf`, or `FPImmLeaf`.

需要补充的是版本差异：LLVM 23.1.4 的 `PatLeaf` 已经带一个可选的 `ImmediateCode`——它在 GlobalISel 里检查的是 `MachineOperand` 而不是 `SDNode`：

```tablegen
class PatLeaf<dag frag, code pred = [{}], SDNodeXForm xform = NOOP_SDNodeXForm>
  : PatFrag<(ops), frag, pred, xform> { ... let ImmediateCode = ?; }
```

但这只适用于"操作数立即数"这一类谓词，而且需要 target 主动填写。可移植的做法仍然是换成 `ImmLeaf` 家族：`ImmediateCode` 会被 `-gen-global-isel` 识别，`PredicateCode` 不会。

`ImmLeaf` 的谓词基于 `ConstantSDNode` 的整数值，可以用在两边。

### 5.5.2 ComplexPattern → GIComplexOperandMatcher

`ComplexPattern` 同样绑定 `SDNode`。替代品是：

```tablegen
def gi_fixedpoint_v2f64 : GIComplexOperandMatcher<v2s64, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v2f64>;
def gi_fixedpoint_v2f32 : GIComplexOperandMatcher<v2s32, "selectCVTFixedPointVec">,
                          GIComplexPatternEquiv<fixedpoint_v2f32>;
```

（`AArch64InstrInfo.td`）

- `GIComplexOperandMatcher<LLT, "函数名">`：声明一个针对特定 LLT 的复杂操作数匹配器；
- `GIComplexPatternEquiv<原ComplexPattern>`：告诉导入器"这个 GI 版本等价于那个 SDAG 版本"，于是引用它的 pattern 可以整体导入。

匹配函数的签名（`GlobalISel/Target.td`）：

```cpp
ComplexRendererFn select(MachineOperand &Root) const;
```

返回 `nullptr` 表示不匹配，否则返回一个渲染器，把匹配到的操作数写进结果指令。

文档给的三个实用谓词：

- `isBaseWithConstantOffset()`：识别 base+offset 结构；
- `isOperandImmEqual()`：判断特定常量；
- `isObviouslySafeToFold()`：判断能否安全折叠。

三条实现纪律（文档原话的转述）：

1. 谓词里**不要修改 MIR**；
2. 渲染 lambda **按值捕获**，因为它在谓词返回之后才执行；
3. 只在渲染器里创建指令——GlobalISel 不会清理你创建却没用的东西。

### 5.5.3 SDNodeXForm → GICustomOperandRenderer

`SDNodeXForm` 是"匹配后对操作数做变换"。GlobalISel 的对应物：

```tablegen
class GICustomOperandRenderer<string rendererfn> {
  string RendererFn = rendererfn;
}
```

签名：

```cpp
void render(MachineInstrBuilder &MIB, const MachineInstr &MI, int OpIdx = -1);
```

`OpIdx` 为负表示"用 MI 定义的值"，否则表示"用 MI 的第 OpIdx 个操作数"。

### 5.5.4 自定义 SDNode → GINodeEquiv + 伪指令

target 私有的 SDNode（比如 AArch64 的 `AArch64rev32`）没有对应的 gMIR 操作码。做法是：

1. 在 `AArch64InstrGISel.td` 定义一个 generic 伪指令 `G_REV32`；
2. 用 `GINodeEquiv<G_REV32, AArch64rev32>` 建立映射；
3. 确保**在选择之前**有 pass 把 `G_SHUFFLE_VECTOR` 之类的指令改写成 `G_REV32`（通常是 Legalizer 或 post-legalizer combine）。

```tablegen
def G_REV32 : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type0:$src);
  let hasSideEffects = 0;
  let GISelMatchGenericTypes = 1;
}

def : GINodeEquiv<G_REV32, AArch64rev32>;
```

`GISelMatchGenericTypes = 1` 告诉发射器：这条伪指令的 `type0` 等类型参数要与被匹配指令的 generic 类型一致，生成的匹配表会带上类型检查。

### 5.5.5 汇总表

| SelectionDAG 构造 | 能否直接导入 | GlobalISel 替代 |
| --- | --- | --- |
| `Pat<>` + 已映射 SDNode | 可以 | — |
| `ImmLeaf` / `IntImmLeaf` / `FPImmLeaf` | 可以 | — |
| `PatLeaf`（仅 `PredicateCode`） | 不可以 | 改成 `ImmLeaf` 家族，或写 C++ 选择 |
| `PatLeaf`（提供 `ImmediateCode`） | 可以（仅立即数谓词） | 保留，但需 target 自行实现 |
| `ComplexPattern` | 不可以 | `GIComplexOperandMatcher` + `GIComplexPatternEquiv` |
| `SDNodeXForm` | 不可以 | `GICustomOperandRenderer` + `GISDNodeXFormEquiv` |
| target 私有 SDNode | 不可以 | 定义 `G_*` 伪指令 + `GINodeEquiv` |

---

## 5.6 伪指令不是"假指令"

`AArch64InstrGISel.td` 里的 `G_ADD_LOW`、`G_REV32`、`G_UZP1` 等都是真正的 `GenericInstruction` 子类，会出现在 MIR 里、会被 TableGen 分配操作码、会参与匹配。

它们存在的理由是"给某个 target 概念一个 gMIR 名字"，例如：

```tablegen
// A pseudo to represent a relocatable add instruction as part of address
// computation.
def G_ADD_LOW : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src, type2:$imm);
  let hasSideEffects = 0;
}
```

`G_ADD_LOW` 由 Legalizer 的 `Custom` 动作产生（`G_GLOBAL_VALUE` → `ADRP` + `G_ADD_LOW`），再由选择器匹配成真正的 `ADDXri` 形式。

这形成了一条完整的链：

```text
Legalizer Custom 动作  →  产生 G_ADD_LOW  →  TableGen pattern 匹配  →  真实指令
```

**写新指令支持时，先问"它需不需要一个 G_ 伪指令"**：如果 Legalizer 阶段还没有对应表示，选择器就没有东西可匹配。

---

## 5.7 导入不是全部：C++ 的分工

`AArch64InstructionSelector::select` 的顺序：

```text
preISelLower → earlySelect → selectImpl(TableGen) → switch (手写)
```

有些操作 TableGen 表达不了，就在 `switch` 里手写。两个例子：

### 5.7.1 `G_CONSTANT 0` → `COPY XZR`

```cpp
case TargetOpcode::G_CONSTANT: {
  bool IsZero = ...;
  if (!IsZero)
    return false;                    // 非零常量交给 selectImpl
  Register DefReg = I.getOperand(0).getReg();
  LLT Ty = MRI.getType(DefReg);
  if (Ty.getSizeInBits() == 64) {
    I.getOperand(1).ChangeToRegister(AArch64::XZR, false);
    RBI.constrainGenericRegister(DefReg, AArch64::GPR64RegClass, MRI);
  } else if (Ty.getSizeInBits() <= 32) {
    I.getOperand(1).ChangeToRegister(AArch64::WZR, false);
    ...
  }
  I.setDesc(TII.get(TargetOpcode::COPY));
  return true;
}
```

这解释了第 4 章练习里 `G_CONSTANT 0` 为什么变成 `COPY $xzr`：不是 pattern，是手写代码。

### 5.7.2 `G_SELECT` → `CSEL/CSNEG/CSINV/CSINC`

`G_SELECT` 的选择要先判断"另一个操作数是不是取负/取反/加一"，再挑对应的条件选择指令。这类多形态判断用 C++ 写更直接（`mi_match` 组合），TableGen 表达起来会很啰嗦。

**经验**：TableGen 负责"形态规整的 1:1 或 1:N 映射"，C++ 负责"需要看上下文、需要多次试探"的选择。

---

## 5.8 诊断导入质量

官方文档给了两个开关，需要直接调用 `llvm-tblgen`：

```console
# Linux/macOS；Windows 把 -o /dev/null 换成 -o NUL
llvm-tblgen -gen-global-isel -warn-on-skipped-patterns \
    -I llvm/lib/Target/AArch64 -I llvm/include \
    llvm/lib/Target/AArch64/AArch64.td -o /dev/null
```

- `-warn-on-skipped-patterns`：逐条解释为什么没导入；
- `--stats`：统计导入成功率。

文档建议从 `ninja -v` 里复制真实的 tblgen 命令行再改，因为 include 路径和 `-D` 定义很多，手写容易漏。

输出里最常见的拒绝原因：

| 原因 | 含义 | 处理 |
| --- | --- | --- |
| `PatLeaf` not supported | 谓词基于 SDNode | 改 `ImmLeaf` 或写 C++ |
| `ComplexPattern` not supported | 同上 | 用 `GIComplexOperandMatcher` |
| could not infer type | 类型无法映射到 LLT | 补 `GINodeEquiv`/伪指令类型 |
| unsupported node | 没有 `GINodeEquiv` | 定义伪指令并映射 |

---

## 5.9 完整推演：`G_ADD` → `ADDXrr`

把前面的碎片拼起来：

1. `AArch64InstrFormats.td` 的 `BaseAddSubRegPseudo<GPR64, add>` 生成 pattern：
   `(set GPR64:$Rd, (add GPR64:$Rn, GPR64:$Rm))` → `ADDXrr`；
2. `SelectionDAGCompat.td` 声明 `GINodeEquiv<G_ADD, add>`；
3. `-gen-global-isel` 把 `add` 节点替换成 `G_ADD`，把 `GPR64` 映射成 `s64` + `gpr` bank；
4. 生成匹配表：检查操作码是 `G_ADD`、两个操作数类型是 `s64`、然后渲染 `ADDXrr`；
5. `selectImpl` 在运行时命中，`G_ADD` 变 `ADDXrr`。

这也解释了第 4 章练习 A.2 的输出：为什么 `G_ADD` 有时选 `ADDXri`（立即数形式）而不是 `ADDXrr`——`G_CONSTANT` 折进 pattern 后命中了带立即数的那条规则。

---

## 进阶延伸

- 读 `GlobalISelEmitter` 的 unsupported-pattern 诊断，学会判断一条 pattern 是“没导入”还是“导入了但没命中”。
- 研究 `GINodeEquiv` 的 `IfSignExtend`/`IfZeroExtend`/`IfFloatingPoint` 如何把同一个 SDNode 拆成多个 `G_*`。
- 思考：如果让你把一条 SelectionDAG `PatLeaf` 迁移到 GlobalISel，应该先改谓词还是先改 pattern 结构？
## 踩雷清单

1. **以为有 `.td` pattern 就一定能导入**。只有 `PredicateCode` 的 `PatLeaf`、`ComplexPattern`、私有 SDNode 都要额外适配。
2. **忘了 `GINodeEquiv`**。pattern 写得再好，没有节点映射就不会进入 GlobalISel 匹配表。
3. **把 `-gen-globalisel` 当成真实参数**。实际是 `-gen-global-isel`。
4. **在 GI 匹配器里修改 MIR**。谓词阶段只能读；创建指令只能在渲染器里做。
5. **渲染 lambda 按引用捕获**。谓词返回后引用可能悬空，必须按值捕获。
6. **定义了 `G_*` 伪指令却没人在选择前产生它**。匹配表里有规则，但 MIR 里永远不会出现这个操作码。
7. **以为 `selectImpl` 包含全部选择逻辑**。它只是匹配表入口；大量特例会落到 C++ `switch`。

---

## 动手练习

### 练习 5.1 追一条映射

在 `SelectionDAGCompat.td` 里找到 `G_MUL`、`G_AND`、`G_SDIV` 的映射，再在 AArch64 的 `.td` 里找到它们对应的真实指令 pattern。写出"IR 操作 → G_* → 机器指令"的完整链条。

### 练习 5.2 找一条导不进来的 pattern

在 `AArch64InstrInfo.td` 里找一条使用 `PatLeaf` 或 `ComplexPattern` 的 pattern，说明它为什么不能直接导入，以及需要什么替代品。

### 练习 5.3 伪指令的生命周期

选一个 `AArch64InstrGISel.td` 里的 `G_*` 伪指令（如 `G_REV32`），找出：(a) 谁产生它；(b) 它匹配成哪条真实指令；(c) 如果没有 Legalizer 那一步会发生什么。

### 练习 5.4 观察 C++ 兜底

用第 4 章的 MIR 分别测试 `G_CONSTANT 0` 与 `G_CONSTANT 42`，对比输出。解释为什么一个变成 `COPY $xzr`、另一个走 TableGen。

### 练习 5.5 读生成文件（可选）

如果你有源码构建，找到 `build/lib/Target/AArch64/AArch64GenGlobalISel.inc`，搜索 `selectImpl` 与 `getMatchTable`，看匹配表的实际形态。没有构建也行：读 `GlobalISelEmitter.cpp` 里拼接 `selectImpl` 的那段代码。

---

## 重点整理

- GlobalISel 复用 SelectionDAG 的 pattern，通过 `GINodeEquiv` 把 SDNode 映射到 `G_*`。
- `-gen-global-isel` 生成 `<Target>GenGlobalISel.inc`，核心是 `selectImpl` + 匹配表。
- `GINodeEquiv` 还能表达一个 SDNode 分叉成多个 G_*（load 家族、icmp/fcmp）和原子性检查。
- `PatLeaf`、`ComplexPattern`、`SDNodeXForm`、私有 SDNode 都不能直接导入，各有替代机制。
- target 可以定义 `G_*` 伪指令，由 Legalizer 产生、由 pattern 匹配。
- TableGen 只管规整映射；上下文相关的选择落在 C++ `switch`。

---

## 自我检核

1. `GINodeEquiv` 的两个核心字段是什么？额外字段解决什么结构差异？
2. `-gen-global-isel` 生成的文件里，`selectImpl` 做了哪三件事？
3. 为什么 `PatLeaf` 不能直接导入？替代品是什么？
4. `GIComplexPatternEquiv` 的作用是什么？没有它会怎样？
5. 定义一个 `G_*` 伪指令后，为什么还要保证它在选择前被产生？
6. `G_CONSTANT 0` 为什么走 C++ 而不是 TableGen？
7. 匹配表里的类型检查来自哪里？

---

## 延伸阅读

- `llvm/utils/TableGen/GlobalISelEmitter.cpp`：导入器本体，文件头注释是最好的流程说明。
- `llvm/include/llvm/Target/GlobalISel/SelectionDAGCompat.td`：`GINodeEquiv` 定义与全部映射。
- `llvm/include/llvm/Target/GlobalISel/Target.td`：`GIComplexOperandMatcher`、`GICustomOperandRenderer` 的接口契约。
- `llvm/lib/Target/AArch64/AArch64InstrGISel.td`：AArch64 的 `G_*` 伪指令与 `GINodeEquiv` 实例。
- `llvm/lib/Target/AArch64/AArch64InstrInfo.td`：`GIComplexOperandMatcher` 的真实用法（fixedpoint）。
- `llvm/lib/Target/AArch64/GISel/AArch64InstructionSelector.cpp`：生成文件的包含方式与 C++ 兜底。
- 官方文档《InstructionSelect》的 "SelectionDAG Rule Imports" 一节：<https://llvm.org/docs/GlobalISel/InstructionSelect.html> —— 官方对 pattern 导入边界的说明；对照本章 5.5 的“导不进来”清单。

---

## 下一章

第 6 章打开生成物：**匹配表执行器**。看 `GIM_CheckOpcode`、`GIM_Try`、`GIM_Match` 这些字节码如何被 `executeMatchTable` 解释，以及一条规则命中后操作数怎样被渲染成目标指令。

[第 6 章 匹配表执行器](./06-matcher-table-executor.md)
