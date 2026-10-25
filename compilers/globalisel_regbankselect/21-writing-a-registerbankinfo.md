# Ch 21 — 为新 target / 新 bank 写 RegisterBankInfo

> **目标**：从一个只有寄存器类、没有 GlobalISel bank 支持的玩具 target 出发，写出最小可运行的 `RegisterBankInfo`：bank 定义、生成表、`PartMappings` / `ValueMappings`、`getRegBankFromRegClass`、`copyCost`、默认 mapping、alternative mapping、`applyMappingImpl` 和 MIR 测试。读完你手里应有一份可以照着填的骨架，以及一份 review 前必须逐项打勾的清单。
>
> **环境**：LLVM 23.1.4。本章的 Toy target 是**假设的**，不在 LLVM 源码树里；所有构建、运行、lit 输出都标 `guided`，没有伪造结果。AArch64 与 RISC-V 的真实代码作为 `源码依据` 对照。
>
> **证据约定**：API 签名与行为来自 `.scratch/llvm-23.1.4-src`；Toy 代码是可编译形状的骨架，不声称已在某个 target 上跑通。

## 为什么需要这个？

新 target 接 GlobalISel 时，通常按 `IRTranslator → Legalizer → RegBankSelect → InstructionSelect` 补支持。前三步里，RegBankSelect 最容易被低估：它既要知道“值该放哪个寄存器文件”，又要知道“放错了搬一次多贵”。这两件事没有通用答案，只能由 target 写。

接口又分散在四五个地方：TableGen 的 bank 定义、生成的 `<Target>GenRegisterBank.inc`、手写的 mapping 表、`getInstrMapping` / alternatives、`copyCost` / `getRegBankFromRegClass` / `applyMappingImpl`，再加上 Subtarget 与 PassConfig 的接线。少任何一环，症状都不是“编译报错”，而是运行到某个函数时 `llvm_unreachable`、mapping invalid，或者悄悄 fallback 到 SelectionDAG。

## 先建立直觉

把这件事想成给一座新机场做转机系统：

```text
1. 划航站楼        RegisterBank 定义（.td）——有几座楼、每座楼接纳哪些登机口
2. 发楼号与归属表   -gen-register-bank——机器生成 ID、容量、regclass→bank
3. 排座位图        PartMappings / ValueMappings——哪种位宽的值坐哪座楼
4. 定摆渡车票价     copyCost——跨楼搬一次多贵
5. 写调度规则       getInstrMapping / alternatives——默认停哪、备选停哪
6. 接进塔台        Subtarget::getRegBankInfo + PassConfig::addRegBankSelect
```

最容易犯的错，是跳过 1–3 直接写 5。结果 `getInstrMapping` 返回了一个指向不存在座位的 mapping：assertions build 在 `verify()` 就炸，release build 则可能选到错误 bank，直到 InstructionSelect 才报 “cannot select”。

三条硬约束先记住：

1. **bank 必须覆盖所有会被查询的寄存器类**，否则生成的 `getRegBankFromRegClass` 会 `llvm_unreachable`。
2. **mapping 的每一段都必须落在该 bank 能容纳的位宽内**，否则 `getValueMapping` 返回 invalid。
3. **非默认 `InstructionMapping::ID` 必须有人处理**，否则 `applyMappingImpl` 会 unreachable。

## 核心概念

### 最小可运行集合

| 组件 | 位置 | 少了会怎样 |
| --- | --- | --- |
| bank 定义 | `ToyRegisterBanks.td` | TableGen 报 “No register banks defined” |
| 生成规则 | `CMakeLists.txt` | 没有 `<Target>GenRegisterBank.inc` |
| 生成类接入 | `ToyRegisterBankInfo.h` | 找不到 `Toy::GPRBRegBankID` |
| mapping 表 | `.cpp` 或 `.def` | `getValueMapping` 返回 invalid |
| 默认 mapping | `getInstrMapping` | 指令无法映射 |
| 备选 mapping | `getInstrAlternativeMappings` | Greedy 退化成 Fast |
| 代价 | `copyCost` | 跨 bank 一律按 1 处理 |
| 接线 | Subtarget / PassConfig | `Cannot work without RegisterBankInfo` |

### bank ID 与覆盖

`RegisterBank` 的 TableGen 类只有两个字段：

```tablegen
class RegisterBank<string name, list<RegisterClass> classes> {
  string Name = name;
  list<RegisterClass> RegisterClasses = classes;
}
```

ID 由 emitter 按遍历顺序分配，从 0 开始，`NumRegisterBanks` 收尾。**不要手写 ID**，也不要在代码里假设“GPR 一定是 0”；只依赖 `Toy::GPRBRegBankID` 这样的符号。

覆盖会递归包含显式类、子类和 subreg class。Toy 的定义：

```tablegen
def GPRBRegBank : RegisterBank<"GPRB", [GPR32, GPR64, GPR64Pair]>;
def VECBRegBank : RegisterBank<"VECB", [VEC64, VEC128]>;
```

如果 Toy 还有一个 `GPR64sp` 类没被任何 bank 覆盖、但 ABI/选择器会查询它，就必须在 `getRegBankFromRegClass` 里特判。AArch64 对 `GPR64sponly`、RISC-V 对 `VCSR` 都是这么做的。

### mapping 表的两种写法

**静态数组（AArch64 / RISC-V 风格）**：零运行时分配、可被 `verify()` 检查、地址稳定；代价是加位宽/加 bank 要手工扩表，索引容易错。

**动态构造（基类 `getValueMapping`）**：

```cpp
const ValueMapping &VM = getValueMapping(0, 64, Toy::GPRBRegBank);
```

基类按 `(StartIdx, Length, Bank)` 哈希缓存，代码短、组合灵活；代价是每次查询有哈希开销，返回对象的生命周期由 RBI 管理。Toy 用静态表，便于 review。

### `copyCost` 的语义

`copyCost(A, B, Size)` 表示“从 B 搬到 A 的代价”。基类默认 `return &A != &B;`：同 bank 为 0，不同 bank 为 1。Toy 假设跨 GPRB/VECB 需要一条 FMOV 类指令，代价 2：

```cpp
unsigned ToyRegisterBankInfo::copyCost(const RegisterBank &A,
                                       const RegisterBank &B,
                                       TypeSize Size) const {
  if (&A == &B)
    return 0;
  if ((&A == &Toy::GPRBRegBank && &B == &Toy::VECBRegBank) ||
      (&A == &Toy::VECBRegBank && &B == &Toy::GPRBRegBank))
    return 2;
  return RegisterBankInfo::copyCost(A, B, Size);
}
```

这个数不是周期数，只是相对权重；它影响 Greedy 的候选比较，也影响 Fast/Greedy 的 repair 代价与放置，但 Fast 不比较候选。

## 底层机制

### 1. TableGen 与生成类

在 `Toy.td` 里 include `ToyRegisterInfo.td` 和 `ToyRegisterBanks.td`，在 `CMakeLists.txt` 里加：

```cmake
tablegen(LLVM ToyGenRegisterBank.inc -gen-register-bank)
```

生成物提供三段：`GET_REGBANK_DECLARATIONS`（bank ID 枚举）、`GET_TARGET_REGBANK_CLASS`（`RegBanks[]`、`Sizes[]`、`getRegBankFromRegClass` 声明、生成基类构造函数）、`GET_TARGET_REGBANK_IMPL`（bank 实例、覆盖位图、查表实现）。

头文件的标准接法：

```cpp
#define GET_REGBANK_DECLARATIONS
#include "ToyGenRegisterBank.inc"

class ToyGenRegisterBankInfo : public RegisterBankInfo {
protected:
  enum PartialMappingIdx { PMI_GPR32 = 0, PMI_GPR64, PMI_VEC64, PMI_VEC128 };
  static const RegisterBankInfo::PartialMapping PartMappings[];
  static const RegisterBankInfo::ValueMapping ValMappings[];
  static const RegisterBankInfo::ValueMapping *getMap(bool VEC, TypeSize Size);

#define GET_TARGET_REGBANK_CLASS
#include "ToyGenRegisterBank.inc"
};
```

`.cpp` 里 `#define GET_TARGET_REGBANK_IMPL` 再 include 一次。生成基类的构造函数是 `ToyGenRegisterBankInfo(unsigned HwMode = 0)`，它把 `RegBanks`、`NumRegisterBanks`、`Sizes` 交给 `RegisterBankInfo`；子类只做断言和特判。

### 2. mapping 表的下标约定

Toy 用 3 操作数成组的布局：`0` 为 invalid，`1..3` 是 GPR32，`4..6` 是 GPR64，`7..9` 是 VEC64，`10..12` 是 VEC128。

```cpp
const RegisterBankInfo::PartialMapping ToyGenRegisterBankInfo::PartMappings[] = {
    {0, 32, Toy::GPRBRegBank}, {0, 64, Toy::GPRBRegBank},
    {0, 64, Toy::VECBRegBank}, {0, 128, Toy::VECBRegBank},
};

const RegisterBankInfo::ValueMapping ToyGenRegisterBankInfo::ValMappings[] = {
    {nullptr, 0},
    {&PartMappings[0], 1}, {&PartMappings[0], 1}, {&PartMappings[0], 1},
    {&PartMappings[1], 1}, {&PartMappings[1], 1}, {&PartMappings[1], 1},
    {&PartMappings[2], 1}, {&PartMappings[2], 1}, {&PartMappings[2], 1},
    {&PartMappings[3], 1}, {&PartMappings[3], 1}, {&PartMappings[3], 1},
};
```

```cpp
const RegisterBankInfo::ValueMapping *
ToyGenRegisterBankInfo::getMap(bool VEC, TypeSize Size) {
  if (Size.isScalable())
    return &ValMappings[0];
  unsigned Bits = Size.getFixedValue();
  if (!VEC)
    return Bits <= 32 ? &ValMappings[1] : Bits == 64 ? &ValMappings[4]
                                                     : &ValMappings[0];
  return Bits == 64 ? &ValMappings[7] : Bits == 128 ? &ValMappings[10]
                                                    : &ValMappings[0];
}
```

GPR 对 ≤32 位返回 32 位 mapping，这是 AArch64 也采用的“窄值放 32 位寄存器”策略；但真正让 GPR 接受 s8/s16 还需要 InstructionSelect 支持。mapping 合法不等于选择合法。

### 3. `getInstrMapping`：兜底 + 分派

```cpp
const RegisterBankInfo::InstructionMapping &
ToyRegisterBankInfo::getInstrMapping(const MachineInstr &MI) const {
  const unsigned Opc = MI.getOpcode();
  if ((Opc != TargetOpcode::COPY && !isPreISelGenericOpcode(Opc)) ||
      Opc == TargetOpcode::G_PHI) {
    const auto &Mapping = getInstrMappingImpl(MI);
    if (Mapping.isValid())
      return Mapping;
  }

  const MachineFunction &MF = *MI.getParent()->getParent();
  const MachineRegisterInfo &MRI = MF.getRegInfo();

  switch (Opc) {
  case TargetOpcode::G_ADD: case TargetOpcode::G_SUB:
  case TargetOpcode::G_AND: case TargetOpcode::G_OR:
  case TargetOpcode::G_XOR: case TargetOpcode::G_FADD:
  case TargetOpcode::G_FSUB: {
    LLT Ty = MRI.getType(MI.getOperand(0).getReg());
    bool IsVEC = Ty.isVector() || isPreISelGenericFloatingPointOpcode(Opc);
    return getInstructionMapping(DefaultMappingID, /*Cost*/ 1,
                                 getMap(IsVEC, Ty.getSizeInBits()),
                                 MI.getNumOperands());
  }
  case TargetOpcode::G_LOAD: {
    LLT Ty = MRI.getType(MI.getOperand(0).getReg());
    const ValueMapping *Dst = getMap(/*VEC*/ false, Ty.getSizeInBits());
    const ValueMapping *Addr = getMap(/*VEC*/ false, TypeSize::getFixed(64));
    return getInstructionMapping(DefaultMappingID, /*Cost*/ 1,
                                 getOperandsMapping({Dst, Addr}), 2);
  }
  default:
    break;
  }
  return getInstrMappingImpl(MI); // COPY / PHI / 目标相关指令的最后兜底
}
```

注意：`G_FADD` 的 LLT 仍是 `s64`/`s32`，LLT 不区分整数与浮点，靠 `isPreISelGenericFloatingPointOpcode` 判断。最后再次调用 `getInstrMappingImpl`，让 `COPY` 和能从约束推断的指令有兜底；若它返回 invalid，`RegBankSelect` 会报 “unable to map instruction”。

### 4. alternative 与 `applyMappingImpl`

Toy 让 64 位 `G_LOAD` 的结果可以进 VECB：

```cpp
RegisterBankInfo::InstructionMappings
ToyRegisterBankInfo::getInstrAlternativeMappings(const MachineInstr &MI) const {
  if (MI.getOpcode() != TargetOpcode::G_LOAD)
    return {};

  const MachineFunction &MF = *MI.getParent()->getParent();
  const MachineRegisterInfo &MRI = MF.getRegInfo();
  const TargetRegisterInfo &TRI = *MF.getSubtarget().getRegisterInfo();
  TypeSize Size = getSizeInBits(MI.getOperand(0).getReg(), MRI, TRI);
  if (Size != 64 || MI.getNumOperands() != 2)
    return {};

  InstructionMappings Alts;
  Alts.push_back(&getInstructionMapping(
      /*ID*/ 2, /*Cost*/ 1,
      getOperandsMapping({getMap(/*VEC*/ true, Size),
                          getMap(/*VEC*/ false, TypeSize::getFixed(64))}),
      /*NumOperands*/ 2));
  return Alts;
}
```

只要返回非默认 ID，就必须实现：

```cpp
void ToyRegisterBankInfo::applyMappingImpl(
    MachineIRBuilder &Builder, const OperandsMapper &OpdMapper) const {
  switch (OpdMapper.getInstrMapping().getID()) {
  case 2:
    return applyDefaultMapping(OpdMapper);
  default:
    llvm_unreachable("Unknown Toy mapping ID");
  }
}
```

`applyDefaultMapping` 要求每个操作数 `NumBreakDowns == 1`（有 assert）；Toy 满足。若以后引入把 i128 拆成两个 GPR 的 mapping，就不能再直接用它。

### 5. 接线与自检

```cpp
const RegisterBankInfo *ToySubtarget::getRegBankInfo() const {
  if (!RegBankInfo)
    RegBankInfo.reset(new ToyRegisterBankInfo(*getRegisterInfo()));
  return RegBankInfo.get();
}

bool ToyPassConfig::addRegBankSelect() {
  addPass(new RegBankSelect());
  return false;
}
```

`RegBankSelect::init` 里有 `RBI = MF.getSubtarget().getRegBankInfo(); assert(RBI && ...)`。Subtarget 返回空，pass 会在第一个函数上直接 assert。

构造函数至少要检查覆盖与容量：

```cpp
ToyRegisterBankInfo::ToyRegisterBankInfo(const TargetRegisterInfo &TRI)
    : ToyGenRegisterBankInfo() {
  const RegisterBank &RBGPR = getRegBank(Toy::GPRBRegBankID);
  const RegisterBank &RBVEC = getRegBank(Toy::VECBRegBankID);
  assert(RBGPR.covers(*TRI.getRegClass(Toy::GPR32RegClassID)));
  assert(RBGPR.covers(*TRI.getRegClass(Toy::GPR64RegClassID)));
  assert(RBVEC.covers(*TRI.getRegClass(Toy::VEC64RegClassID)));
  assert(getMaximumSize(RBGPR.getID()) == 64);
  assert(getMaximumSize(RBVEC.getID()) == 128);
  assert(verify(TRI) && "Invalid register bank information");
}
```

`getMaximumSize` 来自生成物 `Sizes[]`，由 bank 中 spill size 最大的寄存器类决定。

## 三个例子

### 例 1：Toy 的 bank 定义与生成结果（guided）

```tablegen
// ToyRegisterBanks.td
def GPRBRegBank : RegisterBank<"GPRB", [GPR32, GPR64, GPR64Pair]>;
def VECBRegBank : RegisterBank<"VECB", [VEC64, VEC128]>;
```

生成后**预期**出现：

```cpp
enum : unsigned {
  InvalidRegBankID = ~0u,
  GPRBRegBankID = 0,
  VECBRegBankID = 1,
  NumRegisterBanks,
};
```

这是形状，不是实跑输出：Toy 不存在于 23.1.4。ID 顺序取决于 emitter 遍历顺序，代码里一律用符号。

### 例 2：默认 mapping 与 alternative 的配合（guided）

输入 MIR 假设为 `G_LOAD s64` 后接 `G_FADD`。Toy 的**预期**行为：

| 模式 | `G_LOAD` 结果 bank | 跨 bank COPY | 原因 |
| --- | --- | --- | --- |
| Fast | GPRB | 1 条 GPRB→VECB | 默认 mapping 把标量 load 放 GPR |
| Greedy | VECB | 0 条 | alternative 让 load 直接进 VEC，喂给 FADD |

这和 AArch64 的 `regbankselect-fp-loads.mir` 是同一思路：默认 mapping + 少量 alternative，让 Greedy 消掉跨 bank COPY。此处没有运行 Toy，结论是推导值。

### 例 3：MIR 测试骨架（guided）

```yaml
# RUN: llc -mtriple=toy-- -O0 -run-pass=regbankselect -verify-machineinstrs %s -o - \
# RUN:   | FileCheck %s --check-prefix=FAST
# RUN: llc -mtriple=toy-- -O0 -run-pass=regbankselect -regbankselect-greedy \
# RUN:   -verify-machineinstrs %s -o - | FileCheck %s --check-prefix=GREEDY

--- |
  define void @load_add(ptr %p) { ret void }
...
---
name:            load_add
legalized:       true
registers:
  - { id: 0, class: _ }
  - { id: 1, class: _ }
  - { id: 2, class: _ }
body: |
  bb.0:
    liveins: $x0
    ; FAST: %[[P:.*]]:gprb(p0) = COPY $x0
    ; FAST: %[[L:.*]]:gprb(s64) = G_LOAD %[[P]](p0)
    ; FAST: %[[C:.*]]:vecb(s64) = COPY %[[L]]
    ; FAST: %[[F:.*]]:vecb(s64) = G_FADD %[[C]], %[[C]]
    ; GREEDY: %[[L:.*]]:vecb(s64) = G_LOAD
    ; GREEDY: %[[F:.*]]:vecb(s64) = G_FADD %[[L]], %[[L]]
    %0:_(p0) = COPY $x0
    %1:_(s64) = G_LOAD %0(p0) :: (load (s64))
    %2:_(s64) = G_FADD %1, %1
    RET_ReallyLR
...
```

`-run-pass=regbankselect` 直接喂 legalized MIR；`-verify-machineinstrs` 在每个 pass 后检查机器指令。Fast/Greedy 各一条 RUN，用 `update_mir_test_checks.py` 生成 CHECK，不要手写猜测的 vreg 编号。真跑前先确认 Toy 的 `-mtriple` 已注册。

## 接入清单

- [ ] `.td` 里每个 bank 的 `RegisterClasses` 覆盖所有会被查询的类；未覆盖的有特判，bank 名不与 regclass 名冲突。
- [ ] `CMakeLists.txt` 里有 `-gen-register-bank`，生成物被加入构建依赖。
- [ ] 头文件用 `GET_REGBANK_DECLARATIONS` + `GET_TARGET_REGBANK_CLASS`，`.cpp` 用 `GET_TARGET_REGBANK_IMPL`。
- [ ] `PartMappings` / `ValMappings` 的下标与 `getMap` 的返回值一致。
- [ ] `getInstrMapping` 处理了 `G_PHI` 与 `COPY` 的兜底。
- [ ] 每个非默认 `InstructionMapping::ID` 都在 `applyMappingImpl` 里有分支。
- [ ] `copyCost` 同 bank 返回 0，跨 bank 的方向与数值有依据。
- [ ] Subtarget 的 `getRegBankInfo()` 返回非空，PassConfig 已 `addRegBankSelect()`。
- [ ] assertions build 里 `verify(TRI)` 通过。
- [ ] MIR 测试覆盖 Fast、Greedy、`-verify-machineinstrs` 与一个失败/边界场景。

## 对比与取舍

| 选择 | 优点 | 代价 | 适用 |
| --- | --- | --- | --- |
| 静态 mapping 表 | 可 verify、无分配、地址稳定 | 手工维护、索引易错 | bank/位宽固定、review 友好 |
| 动态 `getValueMapping` | 代码短、组合灵活 | 哈希开销、生命周期隐式 | 组合多、原型阶段 |
| AArch64 式 `.def` | 位宽扩展集中 | 下标运算复杂 | 多 bank、多位宽成熟 target |
| RISC-V 式 `.cpp` 数组 | 直观 | 扩展要改多处 | bank 少、位宽少 |
| 多 bank | 更贴近硬件 | 候选与 COPY 增多 | 寄存器文件确实分离 |
| 少 bank | 编译快、mapping 简单 | 无法表达硬件约束 | 原型、早期 bring-up |

Toy 采用静态表 + 少量 alternative，是“能 review、能扩展”的中间路线。真实 target 不要一次加很多 bank；每加一个，就要为它定义 mapping、copyCost、alternative，并承担额外测试与编译时间。

## 踩雷集锦

1. **照抄 AArch64 的偏移运算，却没同步枚举边界。**
   `getValueMapping` 的下标依赖 `First3OpsIdx`、`Last3OpsIdx`、`PMI_Min`；加 bank 或位宽而没改这些常量，就会越界或返回错误 mapping。Toy 用显式查表，牺牲紧凑性换可读性。

2. **给 GPR 返回 128 位 mapping。**
   `getMap(false, 128)` 在 Toy 里返回 invalid，这是故意的：GPR 最多 64 位。若直接复用 `PartMappings` 最后一项，会把 128 位值映射到 GPR，assertions build 会在 `getValueMapping` 或 `verify` 里失败。

3. **返回了非默认 ID，却没实现 `applyMappingImpl`。**
   基类的 `applyMappingImpl` 是 `llvm_unreachable`。`applyMapping` 只对 `DefaultMappingID` 自动走 `applyDefaultMapping`；ID 1/2/3 都必须由 target 处理。

4. **bank 名字与寄存器类名字冲突。**
   `RegisterBankEmitter` 会对大小写不敏感的冲突发警告：MIR 里的名字可能同时被解析成 bank 或 regclass。给 bank 起名时避开已有 regclass 名。

5. **覆盖不全，直到某个函数才 unreachable。**
   生成的 `getRegBankFromRegClass` 对未覆盖类直接 `llvm_unreachable`。加一个会被 ABI/选择器查询的 regclass 时，要么加进 bank 的显式列表，要么在 target 的 `getRegBankFromRegClass` 里特判。

## 进阶

- **用 `-verify-machineinstrs` 和 assertions build 双保险。** 前者查机器指令，后者查 `verify(TRI)`、mapping 合法性与 `applyDefaultMapping` 的 `NumBreakDowns == 1` 断言。
- **把 copyCost 写成可解释的表。** 每个非 1 的数值都应能对应一条真实指令或 scheduling 资源；Toy 的 2 只是占位。

## 动手练习

1. 给 Toy 加第三个 bank `PREDB`（覆盖 `PRED` 类），写出 `.td` 定义、`PartMappings` 项和 `G_ICMP` 的映射。先预测 MIR 里的 bank 名。
2. 把 Toy 的 `G_LOAD` alternative 扩展成“结果 GPRB 或 VECB，地址 GPRB”，补上 ID 处理，并写出 Fast/Greedy 两条 RUN。
3. 故意把 `getMap(false, 64)` 改成返回 128 位 mapping，在 assertions build 里跑一条 `G_ADD s64`，记录第一个断言出现在哪里，再改回。

## 本章重点整理

- 最小集合：`.td` bank 定义、`-gen-register-bank`、生成类接入、mapping 表、`getInstrMapping`、`copyCost`、Subtarget 接线。
- bank ID 由 TableGen 生成，代码里用符号，不写死数字。
- 覆盖会递归包含子类与 subreg class；未覆盖但会被查询的类必须特判。
- Fast 只依赖默认 mapping；Greedy 依赖 `getInstrAlternativeMappings`。
- 非默认 `InstructionMapping::ID` 必须在 `applyMappingImpl` 里处理。

## 自我检核

- [ ] 能说出从 `.td` 到 `ToyGenRegisterBank.inc` 的完整生成链
- [ ] 能解释为什么 `getRegBankFromRegClass` 可能对某个类 unreachable
- [ ] 能写出一个 3 操作数指令的 `ValueMapping` 下标布局
- [ ] 能解释 `copyCost(A, B)` 的参数方向
- [ ] 能说出 Fast 与 Greedy 分别依赖哪个虚函数
- [ ] 能解释为什么 alternative 的 ID 不能取 `DefaultMappingID`，并列出 review 一个新 RBI 时必问的五个问题

## 延伸阅读

- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：构造函数、`getInstrMapping`、`getInstrAlternativeMappings`、`applyMappingImpl`。
  - **学什么**：成熟 target 如何组合默认策略与少量 alternative；对照 Toy 看哪些是必需的。
- **[RISCVRegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/RISCV/GISel/RISCVRegisterBankInfo.cpp)**
  - **读哪里**：`PartMappings`、`ValueMappings`、`getInstrMapping`。
  - **学什么**：最小实现能短到什么程度，以及没有 alternative 的后果。
- **[RegisterBankInfo.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/include/llvm/CodeGen/RegisterBankInfo.h)**
  - **读哪里**：`copyCost`、`getBreakDownCost`、`applyMapping`、`getInstrPossibleMappings` 的注释。
  - **学什么**：每个虚函数的契约；特别是 `applyMapping` 对 `DefaultMappingID` 的特判。
- **[MIR Language Reference](https://llvm.org/docs/MIRLangRef.html)**
  - **读哪里**：`registers`、`legalized`、`-run-pass`、`liveins`。
  - **学什么**：如何手写 legalized MIR 作为 RegBankSelect 的输入。

下一章讨论一个比“怎么接”更难的问题：bank 该分多细、mapping 该多具体、代价该怎么定，以及这些选择如何影响编译时间与最终代码。

→ [Ch 22 bank 设计、代价模型与性能权衡](./22-bank-design-and-performance.md)