# Ch 5 — RegisterBankInfo 数据模型
> **目标**：把 `PartialMapping`、`ValueMapping`、`InstructionMapping` 三层结构拆到字段级；弄清 operand index、mapping ID、cost 各自负责什么；能看着一条 MIR 还原出它对应的 mapping 数组。
>
> **环境**：LLVM 23.1.4 源码（`.scratch/llvm-23.1.4-src`）；MIR 片段来自官方 23.1.4 预编译包（实测）。本章不修改源码。
>
> **证据约定**：结构体字段、常量值、`verify` 不变量标为**源码依据**；`or.ll` / `add.ll` 的 MIR 标为**实测**；没有运行 `-debug-only`（官方包没有该能力），因此不伪造 mapping 的调试打印。
## 为什么需要这个？
RegBankSelect 的输入是 legal gMIR，输出是 banked gMIR。中间那一步“给每个操作数选 bank”并不是直接写进 `MachineRegisterInfo` 就结束：target 必须先告诉 pass **这条指令的每个操作数允许落在哪些 bank、代价是多少**。这套“告诉”的数据结构就是 `RegisterBankInfo` 的 mapping。

如果不理解这层结构，后面会反复卡在三个问题：

- 为什么 `COPY` 的 mapping 只写一个操作数，而 `G_OR` 要写三个？
- 为什么 `InstructionMapping` 里既有 `ID` 又有 `Cost`，它们分别给谁用？
- 为什么 target 的 `getInstrMapping` 返回一个“无效 mapping”时，pass 不是立刻崩，而是走到 `unable to map instruction`？

本章只回答数据模型。算法（Fast/Greedy）在 Ch 8–9，repair 在 Ch 10。先把“一条指令如何被描述”这件事讲清楚，后面的代价和 COPY 才有落点。
## 先建立直觉
把一条指令想成一张**登机牌**：
```text
InstructionMapping
  ├─ ID       : 这张登机牌的编号（target 和 apply 逻辑用它对齐）
  ├─ Cost     : 坐这个 bank 的“票价”（指令本身，不含搬运行李）
  └─ Operands : 每个操作数一张 ValueMapping
                  └─ BreakDown : 这个值由哪几块寄存器拼成
                                   └─ PartialMapping : {起始位, 长度, bank}
```
三层的关系是“从整条指令，逐层拆到一段 bit”：
```text
InstructionMapping
        │  OperandsMapping[i]
        ▼
ValueMapping  ── BreakDown[] ──► PartialMapping
        │                             │
        │                             ├─ StartIdx
        │                             ├─ Length
        │                             └─ RegBank
        └─ NumBreakDowns
```
大多数情况下，一个值只占一个寄存器，所以 `ValueMapping` 只有 1 个 `PartialMapping`。模型支持把一个值拆成多块（例如 64 位拆成两个 32 位 GPR），但 AArch64 在 23.1.4 的表里基本不用这种复杂映射。理解“模型支持”与“target 实际使用”之间的差距，是读源码时很重要的一条分界线。
## 核心概念
### PartialMapping：一段连续的 bit
源码定义在 `llvm/include/llvm/CodeGen/RegisterBankInfo.h`：
```cpp
struct PartialMapping {
  /// Number of bits at which this partial mapping starts in the
  /// original value.  The bits are counted from less significant
  /// bits to most significant bits.
  unsigned StartIdx;

  /// Length of this mapping in bits.
  unsigned Length;

  /// Register bank where the partial value lives.
  const RegisterBank *RegBank;
};
```
三个字段的语义：

| 字段 | 含义 | 易错点 |
| --- | --- | --- |
| `StartIdx` | 从**最低位**开始数的起始 bit | 不是寄存器编号，也不是 operand index |
| `Length` | 覆盖多少 bit | 不是字节数 |
| `RegBank` | 这一段值放在哪个 bank | 不是 regclass，也不是物理寄存器 |

`getHighBitIdx()` 返回 `StartIdx + Length - 1`。例如 `{0, 32, GPR}` 覆盖 bit 0–31，`getHighBitIdx()` 是 31。

`PartialMapping::verify` 在 assertions 打开时检查：

- `RegBank` 非空；
- `Length` 非零；
- `StartIdx` 不溢出；
- `RBI.getMaximumSize(RegBank->getID()) >= Length`。

最后一条很关键：AArch64 的 GPR 最大 128 bit、FPR 最大 512 bit、CC 最大 32 bit（这些值来自 `AArch64RegisterBankInfo` 构造函数的断言）。所以 `{0, 512, GPR}` 在 AArch64 上不可能通过 verify，而 `{0, 512, FPR}` 可以。
### ValueMapping：一个值由哪些 PartialMapping 拼成
```cpp
struct ValueMapping {
  /// How the value is broken down between the different register banks.
  const PartialMapping *BreakDown;

  /// Number of partial mapping to break down this value.
  unsigned NumBreakDowns;

  ValueMapping() : ValueMapping(nullptr, 0) {}

  bool isValid() const { return BreakDown && NumBreakDowns; }
};
```
默认构造是**无效**的：`BreakDown == nullptr`、`NumBreakDowns == 0`。这一点后面会反复出现——`InstructionMapping` 数组里遇到非寄存器操作数时，占位的就是这个无效 `ValueMapping`。

两个典型值：
```cpp
// 一个 32 位整数，整体放在 GPR。
ValueMapping vm32{&partials[0], 1};   // partials[0] = {0, 32, GPR}

// 一个 64 位值，拆成两个 32 位 GPR。
PartialMapping split[2] = {
  {0,  32, &GPR},
  {32, 32, &GPR},
};
ValueMapping vmSplit{split, 2};
```
`ValueMapping::verify` 要求所有 partial mapping **完整覆盖**目标值且**不重叠**：

- 取所有 partial 的最大 `getHighBitIdx() + 1` 作为原始值宽度；
- 每个 partial 的 bit mask 必须落在宽度内；
- 所有 mask 的并集必须全 1；
- 任意两个 partial 不能重叠。

所以 `{{0, 32, GPR}, {16, 32, GPR}}` 会因重叠失败，`{{0, 32, GPR}}` 对 64 位值会因覆盖不全失败。断言只在 assertions build 生效；release 包不会替你检查。
### InstructionMapping：一条指令的完整描述
```cpp
class InstructionMapping {
  unsigned ID = InvalidMappingID;
  unsigned Cost = 0;
  const ValueMapping *OperandsMapping = nullptr;
  unsigned NumOperands = 0;

public:
  InstructionMapping(unsigned ID, unsigned Cost,
                     const ValueMapping *OperandsMapping,
                     unsigned NumOperands);
  bool isValid() const {
    return getID() != InvalidMappingID && OperandsMapping;
  }
};
```
四个字段：

| 字段 | 作用 | 谁使用 |
| --- | --- | --- |
| `ID` | 告诉 `applyMapping` / target 这是哪一种映射 | `RegisterBankInfo::applyMapping`、target 的 `applyMappingImpl` |
| `Cost` | 指令落在该 bank 的代价，**不含**跨 bank 搬运 | `RegBankSelect::computeMapping` |
| `OperandsMapping` | 按 operand index 排列的 `ValueMapping` 数组 | `computeMapping`、`OperandsMapper` |
| `NumOperands` | 数组长度，必须与指令匹配 | `verify`、`getOperandMapping` |

两个哨兵 ID 定义在 `RegisterBankInfo.cpp`：
```cpp
const unsigned RegisterBankInfo::DefaultMappingID = UINT_MAX;
const unsigned RegisterBankInfo::InvalidMappingID = UINT_MAX - 1;
```
- `DefaultMappingID`：target 无关代码生成的“直接翻译”映射。它不改变 opcode，也不改变类型，只改操作数的 bank。
- `InvalidMappingID`：默认构造的无效 mapping，表示“我不知道怎么映射这条指令”。

target 自定义 ID 必须避开这两个值。AArch64 在 `AArch64RegisterBankInfo.cpp` 里用 `CustomMappingID = 1`，alternative mappings 用 1–3（ID 3 被两个方向共用）；`applyMappingImpl` 的断言把它放宽到 1–4。
### operand index：数组下标就是 MI 的操作数下标
`InstructionMapping` 的注释说得很直白：

> `NumOperands` must be equal to number of all the operands of the related instruction. The rationale is that it is more efficient for the optimizers to be able to assume that the mapping of the ith operand is at the index i.

也就是说，`OperandsMapping[i]` 对应 `MI.getOperand(i)`，**不是**“第 i 个寄存器操作数”。非寄存器操作数也要占一个位置，只是它的 `ValueMapping` 是无效的。

几个真实例子：

| 指令 | MI 操作数 | mapping 数组 |
| --- | --- | --- |
| `%4:gpr(i32) = G_OR %6, %7` | 0=def, 1=use, 2=use | 3 个有效 ValueMapping |
| `%0:gpr(i1) = G_CONSTANT i1 0` | 0=def reg, 1=imm | `{V(GPR), invalid}` |
| `G_BRCOND %0(i1), %bb.2` | 0=cond reg, 1=MBB | `{V(GPR), invalid}` |
| `%0:fpr(f32) = COPY $s0` | 0=def, 1=phys reg | copy-like：只映射 def，`NumOperands = 1` |
| `%0 = G_PHI ...` | 0=def, 1=incoming value, 2=incoming MBB, ... | copy-like：只映射 def，`NumOperands = 1` |

最后两行是最大的坑：`COPY` 在 MIR 里明明有两个操作数，但 `InstructionMapping` 只要求 1 个。`InstructionMapping::verify` 里写死了这条规则：
```cpp
assert(NumOperands == (isCopyLike(MI) ? 1 : MI.getNumOperands()) &&
       "NumOperands must match, see constructor");
```
`isCopyLike` 在 `RegisterBankInfo.cpp` 里定义为：
```cpp
static bool isCopyLike(const MachineInstr &MI) {
  return MI.isCopy() || MI.isPHI() ||
         MI.getOpcode() == TargetOpcode::REG_SEQUENCE;
}
```
所以 `COPY`、`PHI`、`REG_SEQUENCE` 的 mapping 都只描述**定义**。其余操作数的 bank 由指令语义或后续 repair 处理，不在 mapping 数组里。
### 一条完整 mapping 是怎么拼出来的
以 `%4:gpr(i32) = G_OR %6, %7` 为例，AArch64 的 `getSameKindOfOperandsMapping` 会：

1. 看 `MI.getOperand(0)` 的类型：`i32`，不是 vector；
2. 看 opcode：`G_OR` 不是浮点 generic opcode；
3. 选 `PMI_FirstGPR`；
4. 调 `getValueMapping(PMI_FirstGPR, 32)`，拿到一个指向 GPR32 组的 `ValueMapping*`；
5. 用同一个指针作为 3 个操作数的 mapping，构造 `InstructionMapping(DefaultMappingID, 1, ptr, 3)`。

用伪代码表示：
```cpp
const ValueMapping &vm = getValueMapping(PMI_FirstGPR, 32); // {0,32,GPR}
const ValueMapping *ops[3] = {&vm, &vm, &vm};

InstructionMapping m(
    RegisterBankInfo::DefaultMappingID,
    /*Cost=*/1,
    getOperandsMapping(ops, ops + 3),
    /*NumOperands=*/3);
```
真实代码不需要本地数组：AArch64 的 `ValMappings` 表里，每个 `(bank, size)` 组连续放 3 个相同的 `ValueMapping`，所以 `getValueMapping` 返回的指针可以直接当长度为 3 的数组用。这是 Ch 7 要拆的表布局。

如果有一个非寄存器操作数，就把对应位置写成 `nullptr`：
```cpp
const ValueMapping *ops[2] = {&vmGPR1, nullptr};

InstructionMapping m(
    RegisterBankInfo::DefaultMappingID,
    /*Cost=*/1,
    getOperandsMapping(ops, ops + 2),
    /*NumOperands=*/2);
```
`getOperandsMapping` 会把 `nullptr` 留成默认构造的无效 `ValueMapping`。`InstructionMapping::verify` 对非寄存器操作数的要求正好相反：
```cpp
if (!MO.isReg()) {
  assert(!getOperandMapping(Idx).isValid() &&
         "We should not care about non-reg mapping");
  continue;
}
```
也就是说：**非寄存器位置必须是无效 mapping；寄存器位置必须是有效 mapping**。这两个断言合起来，能抓住绝大多数“数组错位”的 bug。
## 底层机制
### 四层工厂与哈希缓存
`RegisterBankInfo` 不是让 target 到处 `new`，而是提供四个工厂：

| 工厂 | 产物 | 哈希键 |
| --- | --- | --- |
| `getPartialMapping(Start, Len, Bank)` | `PartialMapping` | `(Start, Len, BankID)` |
| `getValueMapping(BreakDown, N)` | `ValueMapping` | breakdown 的哈希 |
| `getOperandsMapping(begin, end)` | `ValueMapping[]` | 指针数组的哈希 |
| `getInstructionMapping(ID, Cost, Ops, N)` | `InstructionMapping` | `(ID, Cost, Ops, N)` |

每个工厂都维护一个 `DenseMap<hash_code, unique_ptr<...>>`。相同参数第二次调用会返回同一个对象。这样做的好处：

- `InstructionMapping` 可以按指针比较/打印，不需要逐字段深比较；
- target 表里的静态对象和运行时构造的对象可以共享；
- `OperandsMapper` 可以安全地持有一份 mapping 引用，而不必拷贝。

代价是：**传给工厂的指针必须指向生命周期足够长的对象**。`ValueMapping` 的注释明确要求 `BreakDown` 的生命周期至少和 `ValueMapping` 一样长。不要在栈上构造一个 `ValueMapping`，然后把指针塞进 `getOperandsMapping`——函数返回后栈对象就没了，哈希表里留下悬空指针。
### 无效 mapping 是一个规范化单例
`getInvalidInstructionMapping()` 返回：
```cpp
getInstructionMappingImpl(/*IsInvalid=*/true);
// ID = InvalidMappingID, Cost = 0, OperandsMapping = nullptr, NumOperands = 0
```
它的 `isValid()` 为 false。target 的 `getInstrMapping` 在“我放弃”时返回它，而不是返回一个半填的 mapping。pass 看到 `!InstrMapping.isValid()` 就直接把代价判为 `ImpossibleCost`，不会去读空指针。
### OperandsMapper：把 mapping 应用到指令上
`OperandsMapper` 是 `InstructionMapping` 的“执行器”。它持有：
```cpp
SmallVector<int, 8> OpToNewVRegIdx;
SmallVector<Register, 8> NewVRegs;
MachineRegisterInfo &MRI;
MachineInstr &MI;
const InstructionMapping &InstrMapping;
```
`createVRegs(OpIdx)` 按该操作数的 `NumBreakDowns` 创建新 vreg；`setVRegs` 填入；`getVRegs` 取出。`applyMapping` 用它来重写指令操作数：
```text
repair 前:  ... = op Reg
repair 后:  NewRegs = COPY/EXTRACT Reg
            ... = op NewRegs
```
如果 mapping 是 `DefaultMappingID` 且没有任何 repair 产生新 vreg，`applyMapping` 会直接返回，不重写指令：
```cpp
if (!OpdMapper && InstrMapping.getID() == RegisterBankInfo::DefaultMappingID)
  return true;
```
这就是“默认映射只改 bank、不改指令”的实现含义。
## 示例一：把 `or.ll` 的 Fast MIR 还原成 mapping
【实测】官方 23.1.4 包，`-O2 -global-isel -stop-after=regbankselect`：
```yaml
body:             |
  bb.1 (%ir-block.0):
    liveins: $s0, $s1

    %0:fpr(f32) = COPY $s0
    %1:fpr(f32) = COPY $s1
    %2:fpr(i32) = G_BITCAST %0(f32)
    %3:fpr(i32) = G_BITCAST %1(f32)
    %6:gpr(i32) = COPY %2(i32)
    %7:gpr(i32) = COPY %3(i32)
    %4:gpr(i32) = G_OR %6, %7
    %5:gpr(f32) = G_BITCAST %4(i32)
    $s0 = COPY %5(f32)
    RET_ReallyLR implicit $s0
```
挑三条指令还原：

**1. `%2:fpr(i32) = G_BITCAST %0(f32)`**

AArch64 的 `G_BITCAST` 分支先看源操作数 `%0` 的 bank。此时 `%0` 已经是 FPR，于是：
```cpp
Size = 32;
getCopyMapping(FPR, FPR, 32);   // 同 bank
Cost = 0;
NumOperands = 2;
ID = DefaultMappingID;
```
还原成结构：
```text
InstructionMapping {
  ID   = DefaultMappingID
  Cost = 0
  NumOperands = 2
  Operands[0] = ValueMapping { BreakDown = {0, 32, FPR}, N = 1 }  // def
  Operands[1] = ValueMapping { BreakDown = {0, 32, FPR}, N = 1 }  // use
}
```
**2. `%4:gpr(i32) = G_OR %6, %7`**

`getSameKindOfOperandsMapping` 选 GPR，三个操作数都用 GPR32：
```text
InstructionMapping {
  ID   = DefaultMappingID
  Cost = 1
  NumOperands = 3
  Operands[0] = {0, 32, GPR}
  Operands[1] = {0, 32, GPR}
  Operands[2] = {0, 32, GPR}
}
```
**3. `%0:fpr(f32) = COPY $s0`**

这是 copy-like。`$s0` 是物理寄存器，`getRegBank` 会走“最小物理寄存器类 → `getRegBankFromRegClass`”的路径，得到 FPR；generic def 也是 FPR。mapping 只描述 def：
```text
InstructionMapping {
  ID   = DefaultMappingID
  Cost = 0                         // 同 bank copy
  NumOperands = 1                  // 注意不是 2
  Operands[0] = {0, 32, FPR}
}
```
这里 cost 为 0 是因为 AArch64 的 COPY 分支对同 bank copy 调用了 `copyCost(FPR, FPR, 32)`，基类实现返回 `&A != &B`，即 0。物理寄存器 `$s0` 本身不需要在 mapping 里占一个位置。

> 注意：`%6:gpr(i32) = COPY %2(i32)` 是 RegBankSelect 为 `G_OR` 插入的 repair copy，不是 target 为某条 IR 指令生成的 mapping。它的存在本身是 Ch 10 的主题；本章只把它当作“数组里可能出现的新 vreg”看待。
## 示例二：非寄存器操作数占位
【源码依据】AArch64 的 `G_CONSTANT` 分支：
```cpp
case TargetOpcode::G_CONSTANT: {
  ...
  return getInstructionMapping(
      DefaultMappingID, /*Cost=*/1,
      getOperandsMapping(
          {getValueMapping(
               PMI_FirstGPR,
               MRI.getType(MI.getOperand(0).getReg()).getSizeInBits()),
           nullptr}),
      /*NumOperands=*/2);
}
```
`G_CONSTANT` 的 operand 0 是 def reg，operand 1 是立即数。数组写成 `{GPR mapping, nullptr}`，长度 2。`getOperandsMapping` 把 `nullptr` 变成无效 `ValueMapping`；`verify` 检查 operand 1 不是寄存器，因此要求它的 mapping **无效**。

如果把数组写成 `{GPR mapping, GPR mapping}`，assertions build 会在 `InstructionMapping::verify` 里报：
```text
We should not care about non-reg mapping
```
如果把长度写成 1，会先在 `NumOperands must match` 挂掉。这两个断言是读 mapping 代码时最值得记住的“护栏”。
## 示例三：copy-like 的 `NumOperands = 1`
【源码依据】`getInstrMappingImpl` 对 copy-like 的处理：
```cpp
bool IsCopyLike = isCopyLike(MI);
unsigned NumOperandsForMapping = IsCopyLike ? 1 : MI.getNumOperands();
```
对 `COPY`、`PHI`、`REG_SEQUENCE`，它只填 `OperandsMapping[0]`，其余操作数不参与 mapping。原因在源码注释里：

> For copy like instruction, only the mapping of the definition is important. The rest is not constrained.

这也解释了为什么 `COPY` 的 mapping 可以“继承”某个操作数已有的 bank：它只需要给 def 一个 bank，剩下的由 copy 语义处理。`PHI` 更特殊：它的 def 和 incoming value 必须在同一条边上兼容，Ch 11 会展开。
## 对比与取舍
设计上的核心取舍是：下标即 operand index，查表快，但非寄存器也要占位；多 partial 能描述拆值，但 target 表和 repair 更复杂；mapping 规范化便于指针比较与复用，但生命周期要求严格；ID/Cost 分离让 apply 与代价解耦，但 target 必须保持同步。copy-like 只映射 def，避免数组爆炸，代价是 `NumOperands` 与 MI 操作数数不一致。

AArch64 在 23.1.4 里几乎只使用单 partial 的 `ValueMapping`。多 partial 的模型能力主要留给未来或特殊 target；读 AArch64 表时不要因为看到 `NumBreakDowns` 就以为它真的会拆值。
## 踩雷集锦
### 1. 把 `InstructionMapping::NumOperands` 当成“寄存器操作数个数”
错：
```cpp
// G_BRCOND 有两个操作数：cond reg + MBB
InstructionMapping m(ID, 1, ops, /*NumOperands=*/1);
```
对。`NumOperands` 必须等于 `MI.getNumOperands()`（copy-like 除外），非寄存器操作数也要占位。`G_BRCOND` 应该是 2，数组第二项是无效 mapping。
### 2. 对 `COPY` 写两个操作数的 mapping
错：
```cpp
// 以为 COPY 和普通二元指令一样
InstructionMapping m(ID, cost, ops, /*NumOperands=*/2);
```
对。`COPY` 是 copy-like，`NumOperands` 必须是 1，只映射 def。写 2 会在 `verify` 里触发 `NumOperands must match`。
### 3. 用栈上的 `ValueMapping` 构造 `getOperandsMapping`
错：
```cpp
ValueMapping vm{&partial, 1};
const ValueMapping *ops[3] = {&vm, &vm, &vm};
auto *arr = getOperandsMapping(ops, ops + 3);
// vm 离开作用域，arr 里留下悬空指针
```
对。用 `getValueMapping(...)` 返回的规范化对象，或使用静态表里的对象。mapping 的生命周期由 `RegisterBankInfo` 的缓存管理。
### 4. 把 `Cost` 当成“包含 COPY 的总代价”
错：
```cpp
// 以为 mapping Cost 已经把跨 bank copy 算进去了
InstructionMapping m(ID, /*Cost=*/11, ops, 3);
```
对。`InstructionMapping::Cost` 只是指令落在该 bank 的代价；跨 bank copy 由 `RegBankSelect::computeMapping` 的 `getRepairCost` 另外累加。Ch 9 会给出完整公式。
### 5. 认为 `ID` 唯一标识一个 mapping
错：
```cpp
// 以为同一个 ID 只能对应一个 mapping
assert(mappingA.getID() != mappingB.getID());
```
对。AArch64 的 `G_BITCAST` alternative mappings 里，`GPRToFPRMapping` 和 `FPRToGPRMapping` 都用了 ID 3。ID 表达的是“用哪种 apply 逻辑”，不是“哪一个具体 mapping”。具体 mapping 由指针/哈希区分。
## 进阶
- **读 `OperandsMapper::createVRegs`**：它按 `NumBreakDowns` 创建新 vreg，类型是“正确宽度的 plain scalar”，真正的类型在 apply 时再设。这是多 partial repair 的基础。
- **读 `ValueMapping::partsAllUniform`**：`repairReg` 只支持“每块长度和 bank 都相同”的多 partial 分解；不规则分解会 assert。AArch64 目前不触发这条路径，但写新 target 时会遇到。
- **读 `RegisterBankInfo::getOperandsMapping` 的哈希注释**：它用 `ValueMapping*` 的地址做哈希，因此同一个内容但不同地址的 `ValueMapping` 不会被合并。这是“必须用工厂”的另一个原因。
## 动手练习
1. 【实测】用 `or.ll` 跑 `-stop-after=regbankselect`，把 `G_OR` 的三操作数 mapping 写成 `InstructionMapping` 伪代码。不要看答案，先写 `ID`、`Cost`、每个 `PartialMapping`。
2. 【实测】给 `add.ll` 加一个 `G_CONSTANT`（例如 `%c = add i64 %a, 7` 经 Legalizer 后可能出现 `G_CONSTANT`），在 MIR 里找到它，指出哪个 operand index 是非寄存器、对应 mapping 应该是什么。
3. 【源码依据】在 `RegisterBankInfo.cpp` 里找到 `InstructionMapping::verify`，逐条解释其中每条 assert 分别在防什么错误（不止三个）。
4. 【guided】如果你自己构建 assertions 版本，用 `-debug-only=registerbankinfo` 观察 `getOperandsMapping` 的缓存命中；官方预编译包没有这个能力，不要伪造输出。
## 本章重点整理
- `PartialMapping = {StartIdx, Length, RegBank}`，描述一段连续 bit；`StartIdx` 从最低位算。
- `ValueMapping = {BreakDown, NumBreakDowns}`，描述一个值如何由多块组成；默认构造无效。
- `InstructionMapping = {ID, Cost, OperandsMapping, NumOperands}`；`Cost` 不含跨 bank copy。
- operand index 就是 MI 操作数下标；非寄存器位置必须放无效 `ValueMapping`。
- `COPY` / `PHI` / `REG_SEQUENCE` 是 copy-like，`NumOperands = 1`，只映射 def。
- `DefaultMappingID = UINT_MAX`、`InvalidMappingID = UINT_MAX - 1`；target 自定义 ID 要避开它们。
- mapping 对象由工厂规范化，指针生命周期必须足够长；不要传栈对象地址。
## 自我检核
- [ ] 能不看源码写出三个结构体的字段，并解释 `StartIdx` 的计数方向
- [ ] 能解释为什么 `G_BRCOND` 的 mapping 数组长度是 2，而 `COPY` 是 1
- [ ] 能说出 `InstructionMapping::Cost` 与 `getRepairCost` 的分工
- [ ] 能解释 `DefaultMappingID` 与 `InvalidMappingID` 的区别
- [ ] 能指出 `getOperandsMapping` 为什么要求指针生命周期
- [ ] 能看着 `%2:fpr(i32) = G_BITCAST %0(f32)` 说出它的 mapping 大致形状
## 延伸阅读
### 源码
- **[RegisterBankInfo.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/RegisterBankInfo.h)**
  - **读哪里**：`PartialMapping`、`ValueMapping`、`InstructionMapping` 三个结构体，以及 `getInstrMapping` 的注释。
  - **学什么**：字段语义、`isValid` 的条件、`NumOperands` 的约定。
  - **和本章的关联**：本章的字段表直接来自这里。
- **[RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/CodeGen/RegisterBankInfo.cpp)**
  - **读哪里**：`getPartialMapping` / `getValueMapping` / `getOperandsMapping` / `getInstructionMappingImpl`，以及 `InstructionMapping::verify`。
  - **学什么 / 关联**：规范化缓存、无效 mapping、数组下标不变量，以及“为什么必须用工厂”。
### 文档
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`registers`、`body`、`%0:gpr(i64)` 的语法。
  - **学什么**：MIR 操作数顺序、虚拟寄存器与物理寄存器的写法。
  - **和本章的关联**：不会数 operand index，就无法把 MIR 还原成 mapping 数组。
- **[LLVM GlobalISel — RegBankSelect](https://llvm.org/docs/GlobalISel/RegBankSelect.html)**
  - **读哪里**：Mapping 的概念段与 Fast/Greedy 概述。
  - **学什么**：官方文档如何描述“每条指令的 mapping”。
  - **和本章的关联**：先建立概念，再用本章的字段把它落地。

下一章进入 `getInstrMappingImpl`：当 target 没有为某个 opcode 写专门映射时，LLVM 如何从 regclass 约束和已有 bank 推导出一个默认 mapping，以及什么时候它只能返回 invalid。

→ [Ch 6 默认 mapping：约束、regclass 与 fallback](./06-default-mapping.md)
