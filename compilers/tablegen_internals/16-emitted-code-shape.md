# Ch 16 生成代码的形状：`#ifdef` 分段、结构体打包与字符串池

> **本章目标**：把「`.inc` 是一坨生成代码」变成「`.inc` 是一份**多入口的 C++ 头文件**」。读完你能：解释一个 `AArch64GenInstrInfo.inc` 为什么被 `#include` 了 8 次而不会重定义；看懂 `MCInstrDesc::operands()` 里那句 `this + Opcode + 1` 靠什么成立；算出 `ImplicitOps` 后面那 4 字节 `Padding` 是哪来的；判断一个字符串池到底省了多少字节。这四项是你之后改 `InstrInfoEmitter` 时最容易踩坏的地方。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`），`llvm-tblgen` 为 Release 构建。本章所有产物都由本机实跑生成，路径在 `/tmp/tg16/`：AArch64 的 `GenInstrInfo.inc`（3,270,328 字节）、X86（9,378,938 字节）、WebAssembly、以及一个 38 条指令的 `Mini` 玩具 target。所有 C++ 小实验都用真实 LLVM 头文件编译并运行。

---

## 16.1 为什么"生成代码的形状"值得单独一章

前面五章讲的是「怎么把 `Record` 变成字节」。这一章讲的是**字节排成什么形状**，以及为什么必须是这个形状。

理由很硬：`InstrInfoEmitter` 的输出不是给人读的文档，而是**被 C++ 编译器在 8 处消费的数据结构**（AArch64 后端 7 处 + `llvm-exegesis` 1 处，全仓库实测 `grep`）。你改一行 emitter，改的不是「文本好看不好看」，而是：

- 结构体的布局（影响 `offsetof`、影响 `static_assert`、影响能不能编译）
- 跨 translation unit 的符号可见性（`extern` 还是 `static`，决定 link 成功还是 `multiple definition`）
- 一个 `.inc` 被多次 include 时的宏状态（决定重定义还是正确分发）

这三件事里任何一件错了，报错都出现在**别人的 `.cpp` 里**，而不是你的 emitter 里。你会在 `AArch64MCTargetDesc.cpp` 收到一条「`redefinition of 'AArch64InstrTable'`」，然后在 `InstrInfoEmitter.cpp` 里找半天。

所以本章的组织方式是：先看真实产物的清单（16.2），再看分发机制（16.3–16.5），再逐个拆开三大「形状决策」（16.6 打包、16.7 字符串池、16.8 链接性），最后是读生成代码的方法论（16.9）。

## 16.2 真实产物清单：一个文件，十二个入口

先看事实。`AArch64GenInstrInfo.inc` 全文 44,629 行，里面按 `#ifdef` 切成 12 个 `GET_*` 段，外加 1 个 `ENABLE_INSTR_PREDICATE_VERIFIER` 段（实测，行号与字节数）：

| 段名 | 行范围 | 行数 | 字节数 |
| --- | --- | --- | --- |
| `GET_INSTRINFO_ENUM` | 9–9360 | 9,352 | 835,916 |
| `GET_INSTRINFO_SCHED_ENUM` | 9362–11630 | 2,269 | 214,655 |
| `GET_INSTRINFO_MC_DESC` | 11648–31637 | 19,990 | 1,701,141 |
| `GET_INSTRINFO_HEADER` | 31639–31676 | 38 | 1,515 |
| `GET_INSTRINFO_HELPER_DECLS` | 31678–31696 | 19 | 860 |
| `GET_INSTRINFO_HELPERS` | 31698–32247 | 550 | 21,585 |
| `GET_INSTRINFO_CTOR_DTOR` | 32249–32264 | 16 | 676 |
| `GET_INSTRINFO_MC_HELPER_DECLS` | 32266–32296 | 31 | 895 |
| `GET_INSTRINFO_MC_HELPERS` | 32298–32851 | 554 | 21,291 |
| `GET_COMPUTE_FEATURES` | 32857–43044 | 10,188 | 406,019 |
| `GET_AVAILABLE_OPCODE_CHECKER` | 43046–43062 | 17 | 529 |
| `GET_INSTRMAP_INFO` | 43274–44628 | 1,355 | 58,554 |

十二段加起来 3,263,636 字节，而文件是 3,270,329 字节——差的 6,693 字节是段间空行和 `#if`/`#endif` 本身。

**注意分布极不均匀**：`MC_DESC` 一段占 52%，`ENUM` + `COMPUTE_FEATURES` 再占 38%。剩下九段加起来只有 10%。这个比例直接决定了 `llvm-tblgen` 的编译时间分布——`MC_DESC` 里是一万九千行的聚合初始化大数组，这是整个 LLVM 构建里最让 clang 难受的文件之一（16.7 会回到这点）。

### 段的集合不是固定的

同一份 emitter，不同 target 产出不同段的集合（实测）：

| target | `GET_*` 段数 | 相对 AArch64 的差异 |
| --- | --- | --- |
| AArch64 | 12 | 基准 |
| X86 | 14 | +`OPERAND_TYPES_ENUM`、+`OPERAND_TYPE`、+`MEM_OPERAND_SIZE`、−`INSTRMAP_INFO` |
| WebAssembly | 14 | +`OPERAND_ENUM`、+`NAMED_OPS` |
| Mini（玩具） | 11 | −`INSTRMAP_INFO` |

（实测方式：`grep -c` 统计 `#ifdef GET_xxx` 紧跟 `#undef GET_xxx` 的唯一名字个数。）

原因在 emitter 里是三个**条件判断**，不是配置：

```cpp
// InstrInfoEmitter.cpp:445
void InstrInfoEmitter::emitOperandTypeMappings(...) {
  StringRef Namespace = Target.getInstNamespace();

  // These generated functions are used only by the X86 target
  // (in bolt/lib/Target/X86/X86MCPlusBuilder.cpp). So emit them only
  // for X86.
  if (Namespace != "X86")
    return;
```

`OPERAND_ENUM` / `NAMED_OPS` 则挂在 `HasUseNamedOperandTable` 上（扫一遍所有指令的 `UseNamedOperandTable` 位，只要有 true 就整段输出）；`INSTRMAP_INFO` 挂在「有没有 `InstrMapping` 派生记录」上。

这是本课反复出现的设计取向：**一个 `-gen-*` backend 不代表一份固定 schema，而是一个「按 input 特征自适应裁剪」的生成器**。所以你读生成代码时不能说「AArch64 有这段，所以我的 target 也该有」。

### 一个 38 条指令的 target 长什么样

我的 `Mini`（4 个寄存器、3 条自定义指令 + `Target.td` 的全部预定义 opcode）产出 1,701 行 / 135,556 字节。也就是说：**即使你只写 3 条指令，产物里 335 条都是 `Target.td` 的通用 opcode**，其中 283 条是 `G_*` 泛型指令。

```console
$ awk '/^#ifdef GET_INSTRINFO_ENUM/,/^#endif/' MiniGenInstrInfo.inc | grep -c '^    G_'
283
$ grep -n 'INSTRUCTION_LIST_END' MiniGenInstrInfo.inc
353:    INSTRUCTION_LIST_END = 338
```

338 = 335 个 `Target.td` 预定义 + 3 个自己的。这就是为什么 `Mini::ADD` 的枚举值是 335 而不是 0：

```console
$ ./z   # 链接 trivial，只打印枚举
PHI=0 INSTRUCTION_LIST_END=338
ADD=335 SUB=337 RET=336
```

`ADD=335`、`RET=336`、`SUB=337`——注意 `RET` 和 `SUB` 的顺序：`ComputeInstrsByEnum()`（`CodeGenTarget.cpp:302`）先按「是不是 pseudo」分组，再按名字排序。`ADD`/`SUB`/`RET` 都不是 pseudo，按字节序是 `ADD < RET < SUB`。所以 `SUB` 拿到最大值。

**这条排序规则是生成代码形状的隐形输入**：所有下游的数组、字符串池、`INSTRUCTION_LIST_END` 都跟着它走。改排序规则 = 全 LLVM 所有 `.inc` 内容重排 = 巨大的 diff。这解释了为什么上游对这类改动极度保守（Ch 19 会再谈）。

## 16.3 `IfDefEmitter`：分发机制的全部实现

`llvm/include/llvm/TableGen/CodeGenHelpers.h:27` 只有 20 行：

```cpp
class IfDefEmitter {
public:
  IfDefEmitter(raw_ostream &OS, StringRef Name, bool LateUndef = false)
      : Name(Name.str()), OS(OS), LateUndef(LateUndef) {
    OS << "#ifdef " << Name << "\n";
    if (!LateUndef)
      OS << "#undef " << Name << "\n";
    OS << "\n";
  }
  ...
  ~IfDefEmitter() {
    OS << "\n";
    if (LateUndef)
      OS << "#undef " << Name << "\n";
    OS << "#endif // " << Name << "\n\n";
  }
};
```

就这。它的全部工作是"进作用域时打印 `#ifdef` + 可选 `#undef`，出作用域时打印 `#endif // 名字`"。

**`#undef` 是这套机制的关键**：它是「自消耗的守卫」。所以你写

```cpp
#define GET_INSTRINFO_ENUM
#include "AArch64GenInstrInfo.inc"
#define GET_INSTRINFO_ENUM_ADJACENT   // 再定义一个别的
#include "AArch64GenInstrInfo.inc"
```

第二次 include 时 `GET_INSTRINFO_ENUM` 已经被第一次 include 里的 `#undef` 清掉了，所以 ENUM 段不会再展开——**这是特性，不是 bug**。同一份文件被多次 include 正是设计目标。

反过来，同一个宏连续 define 两次（我实测）：

```cpp
#define GET_INSTRINFO_ENUM
#include "MiniGenInstrInfo.inc"
#define GET_INSTRINFO_ENUM
#include "MiniGenInstrInfo.inc"
```

```text
./MiniGenInstrInfo.inc:15:5: error: redefinition of enumerator 'PHI'
```

`#undef` 让第二个 `#define` 重新生效，于是同一个匿名 enum 被展开两次。同理 `GET_INSTRINFO_HEADER` 两次 → `redefinition of 'MiniGenInstrInfo'`，`GET_INSTRINFO_MC_DESC` 两次 → `redefinition of 'MiniInstrTable'`。

### 三种 guard 生成器

`CodeGenHelpers.h` 里还有三个同族 RAII 类，输出格式各不相同：

```cpp
IfGuardEmitter     // #if <cond>      ... #endif // <cond>
IfDefGuardEmitter  // #ifdef <cond>   ... #endif // <cond>
IfNDefGuardEmitter // #ifndef <cond>  ... #endif // <cond>
```

它们都继承 `IfGuardEmitterBase`，只改关键字。**注意它们不输出 `#undef`**——因为它们不用于「一次性分发」，而用于普通的条件编译。

实际使用分布很不均匀（实测 `grep` 统计 `llvm/utils/TableGen` 下的使用次数）：

| 类 | 使用次数 | 主要用户 |
| --- | --- | --- |
| `IfDefEmitter` | 88 | 几乎所有 backend 的主力 |
| `NamespaceEmitter` | 57 | 同上 |
| `IfDefGuardEmitter` | 9 | 全部在 GlobalISel（`GlobalISelCombinerEmitter.cpp`、`GlobalISelMatchTableExecutorEmitter.cpp`） |
| `IncludeGuardEmitter` | 2 | 多文件输出时给子文件加 guard |
| `IfGuardEmitter` | 1 | 只有 `InstrInfoEmitter.cpp:972` |
| `IfNDefGuardEmitter` | **0** | **没人用** |

`IfNDefGuardEmitter` 零使用——它在库里躺着，等下一篇「清理未使用 API」的 PR。`IfGuardEmitter` 只有一个用户，但那个用户（结构体定义被两段共用）**非它不可**。

`InstrInfoEmitter.cpp:972` 的用法：

```cpp
IfGuardEmitter IfGuard(
    OS,
    "defined(GET_INSTRINFO_MC_DESC) || defined(GET_INSTRINFO_CTOR_DTOR)");
```

产出（AArch64 第 11632 行，实测）：

```cpp
#if defined(GET_INSTRINFO_MC_DESC) || defined(GET_INSTRINFO_CTOR_DTOR)

namespace llvm {

struct AArch64InstrTable {
  ...
};
} // namespace llvm

#endif // defined(GET_INSTRINFO_MC_DESC) || defined(GET_INSTRINFO_CTOR_DTOR)
```

**为什么这里必须是复合条件、而且不能是 `IfDefEmitter`？** 两个原因，都很实质：

1. 结构体定义被两个段共用。`MC_DESC` 段需要它来定义 `AArch64Descs` 数组；`CTOR_DTOR` 段需要它来写 `MiniDescs.Insts` 的成员访问。如果结构体只放在 `MC_DESC` 段里，那么只定义 `GET_INSTRINFO_CTOR_DTOR` 的 TU 就看不到类型。
2. 如果它是 `IfDefEmitter`，一个同时定义 `GET_INSTRINFO_MC_DESC` 和 `GET_INSTRINFO_CTOR_DTOR` 的 TU 会让结构体展开两次 → 重定义。复合 `#if` 保证只展开一次。

现实里两个宏确实分居两个 TU：`MCTargetDesc.cpp` 要 `MC_DESC`（它要建 `MCInstrInfo`），`<Target>InstrInfo.cpp` 要 `CTOR_DTOR`（它要实现 `TargetInstrInfo` 子类构造）。所以这个防御性写法目前没有被触发——但它是**正确的**，你要是把它改成一个 `IfDefEmitter`，今天也能过测试，明天就会有人在 CI 上炸。

### `NamespaceEmitter`：空名字不输出

同一个头文件里的 `NamespaceEmitter`（`CodeGenHelpers.h:126`）：

```cpp
NamespaceEmitter(raw_ostream &OS, const Twine &NameUntrimmed)
    : Name(trim(NameUntrimmed.str()).str()), OS(OS) {
  if (!Name.empty())
    OS << "namespace " << Name << " {\n\n";
}
```

`trim` 只做一件事：去掉前导 `::`。我一并实测了四个行为（`/tmp/tg16/emit/e.cpp`，真实输出）：

```console
#ifdef GET_FOO
#undef GET_FOO

namespace llvm::Mini {

int x = 1;

} // namespace llvm::Mini

#endif // GET_FOO
```

- 空名字：整块不输出（实测 `int no_ns = 2;` 前后没有任何 namespace 行）——这就是为什么 `Mini` 的 `GET_INSTRINFO_ENUM` 段里，一个 `Target.td` 预定义 opcode 和一条自己的 opcode 能放进同一个包围块：**`Target.td` 里 `Namespace` 是 `TargetOpcode`，但 emitter 用的是 `Target.getInstNamespace()`，取第一个非 `TargetOpcode` 的**（`CodeGenTarget.cpp:109`），所以整段只有一个 namespace。
- 前导 `::`：`"::llvm::toy"` → `namespace llvm::toy {`（实测）。
- `LateUndef=true`：`#undef` 跑到 `#endif` 前（实测 `GET_LATE` 的输出顺序是 `#ifdef` / 空行 / 内容 / `#undef` / `#endif`）。目前只有一个用户：`SubtargetEmitter.cpp:173` 的 `GET_SUBTARGETINFO_MACRO`——因为那个宏在文件内被展开成多行代码时，还要保持「宏在作用域内」的语义。
- `IncludeGuardEmitter`：标准的 `#ifndef`/`#define`/`#endif`，用于给生成的头文件加 include guard（实测格式正确）。

### 一个空 namespace 是真的会出现的

看 `Mini` 的 `GET_INSTRINFO_HEADER` 段（实测）：

```cpp
namespace llvm {

struct MiniGenInstrInfo : public TargetInstrInfo {
  ...
};

} // namespace llvm

namespace llvm::Mini {


} // namespace llvm::Mini

#endif // GET_INSTRINFO_HEADER
```

中间那个**空的 `namespace llvm::Mini { }`** 是 `SUBOP_*` 常量的家：

```cpp
// InstrInfoEmitter.cpp:1196
NamespaceEmitter InstNS(OS, ("llvm::" + Target.getInstNamespace()).str());
for (const Record *R : Records.getAllDerivedDefinitions("Operand")) {
  if (R->isAnonymous())
    continue;
  const DagInit *D = R->getValueAsDag("MIOperandInfo");
  if (!D)
    continue;
  for (const auto &[Idx, Name] : enumerate(D->getArgNames()))
    if (Name)
      OS << "constexpr unsigned SUBOP_" << R->getName() << "_"
         << Name->getValue() << " = " << Idx << ";\n";
}
```

`SUBOP_*` 是「组合操作数内部子操作数的下标常量」。AArch64 有大量复合操作数（`ro_Wextend8` 这类），所以有 30 个：

```cpp
constexpr unsigned SUBOP_ro_Wextend8_signed = 0;
constexpr unsigned SUBOP_ro_Wextend8_doshift = 1;
...
```

X86 的 `MIOperandInfo` 不带参数名，所以一个都没有——于是留下一个空的 namespace。**这就是为什么上游 reviewer 会问「你这段是不是没测过空输入」**：空 namespace 在 C++ 里合法，所以编译通过、测试也通过，只是产物里多两行噪音。

## 16.4 消费端：谁 define 哪个宏

看真实代码。`AArch64MCTargetDesc.h:93`（实测 grep）：

```cpp
#define GET_INSTRINFO_ENUM
#define GET_INSTRINFO_MC_HELPER_DECLS
#include "AArch64GenInstrInfo.inc"
```

`AArch64MCTargetDesc.cpp:39`：

```cpp
#define GET_INSTRINFO_MC_DESC
#define GET_INSTRINFO_MC_HELPERS
#define ENABLE_INSTR_PREDICATE_VERIFIER
#include "AArch64GenInstrInfo.inc"
```

`AArch64InstrInfo.h:23`：

```cpp
#define GET_INSTRINFO_HEADER
#include "AArch64GenInstrInfo.inc"
```

`AArch64InstrInfo.h:609`（类定义内部！）：

```cpp
#define GET_INSTRINFO_HELPER_DECLS
#include "AArch64GenInstrInfo.inc"
```

`AArch64InstrInfo.cpp:66` 与 `:12502`：

```cpp
#define GET_INSTRINFO_CTOR_DTOR
#include "AArch64GenInstrInfo.inc"
...
#define GET_INSTRINFO_HELPERS
#define GET_INSTRMAP_INFO
#include "AArch64GenInstrInfo.inc"
```

`AArch64Disassembler.cpp:1628`（不带任何 `GET_*`，纯粹为了用文件里已有的枚举）和 `llvm-exegesis/lib/AArch64/Target.cpp:29`（`GET_AVAILABLE_OPCODE_CHECKER`）各一处。

**全仓库实测共 8 个 include 点**（`grep -rn '#include "AArch64GenInstrInfo.inc"' llvm/lib llvm/tools` 得到 8 行），覆盖 9 个 `GET_*` 宏。**这张映射表就是「产物形状」的规格说明**。你要新增一段，必须同时想清楚三件事：这段归谁消费？放在哪个 TU？和已有段有没有类型依赖？

`HELPER_DECLS` 在类内部展开这个细节尤其值得注意：

```cpp
class AArch64InstrInfo : public AArch64GenInstrInfo {
  ...
#define GET_INSTRINFO_HELPER_DECLS
#include "AArch64GenInstrInfo.inc"
  ...
};
```

也就是说生成器会产出**类成员声明**（`bool isExynosArithFast(const MachineInstr &MI);` 这种），而定义在 `.cpp` 里以 `AArch64InstrInfo::` 前缀展开。这套「声明/定义分离」是靠 `emitTIIHelperMethods(OS, TargetName, ExpandDefinition)` 的一个 `bool` 参数做的：

```cpp
// InstrInfoEmitter.cpp:880
OS << (ExpandDefinition ? "" : "static ") << "bool ";
if (ExpandDefinition)
  OS << TargetName << "InstrInfo::";
```

`ExpandDefinition=false` 时加 `static`，产出的正是 AArch64 里那 14 行：

```cpp
// GET_INSTRINFO_HELPER_DECLS 段，实测原文
static bool isExynosArithFast(const MachineInstr &MI);
static bool isExynosCheapAsMove(const MachineInstr &MI);
...
static bool isScaledAddr(const MachineInstr &MI);
```

这**不是**笔误。它们出现在 `class AArch64InstrInfo` 的 `protected:` 区（`AArch64InstrInfo.h:609` 处），所以 `static` 声明的是**静态成员函数**；而 `GET_INSTRINFO_HELPERS` 段的定义写的是

```cpp
bool AArch64InstrInfo::isExynosArithFast(const MachineInstr &MI) {
```

**类外定义不加 `static`** —— 这正是 C++ 的规则（`static` 只出现在类内声明）。所以这段生成代码是规范的：一个 `Drop_Static` 式的「小清理」如果只是一边加 `static`、一边不加，就说明你没搞清成员函数和自由函数的区别。

顺带解释为什么需要 `static`：如果去掉它，这些就是**非静态成员函数**，`GET_INSTRINFO_HELPERS` 段里的定义就得能访问 `this`；而这些 predicate 只吃 `MachineInstr`，做成静态函数更省（不需要对象）。

## 16.5 `MCInstrDesc` 的 `this + Opcode + 1`：反序排列的真正原因

这是整个生成代码里最反直觉的一件事。先看消费端（`llvm/include/llvm/MC/MCInstrDesc.h:242`）：

```cpp
ArrayRef<MCOperandInfo> operands() const {
  auto OpInfo = reinterpret_cast<const MCOperandInfo *>(this + Opcode + 1);
  return ArrayRef(OpInfo + OpInfoOffset, NumOperands);
}
```

`this + Opcode + 1`。`this` 指向**一条** `MCInstrDesc`，加上 `Opcode` 就跳到了「表里第 `Opcode` 条之后」的位置。这在「按 opcode 升序排放」的数组里是**错的**——`this` 本来就指向第 `Opcode` 条，再加 `Opcode` 会跳过头。

所以生成器必须**反着填**（`InstrInfoEmitter.cpp:1028`，`Num` 的初值在 1027）：

```cpp
// Emit all of the MCInstrDesc records in reverse ENUM ordering.
for (const CodeGenInstruction *Inst : reverse(NumberedInstructions)) {
  ...
  emitRecord(*Inst, --Num, InstrInfo, EmittedLists, OperandInfoMap, OS);
}
```

`Num` 从 `NumberedInstructions.size()` 开始，每轮 `--Num`。于是数组第 0 项拿到 `Num = N-1`（最大 opcode），最后一项拿到 0。实测验证（AArch64 的 9340 条记录，第一条和最后一条）：

```text
first 5: [9339, 9338, 9337, 9336, 9335] last 5: [4, 3, 2, 1, 0]
strictly decreasing by 1: True
```

于是对「记录 `R`（位于下标 `k`、opcode 为 `N-1-k`）」：

```text
this + Opcode + 1  =  &Insts[k] + (N-1-k) + 1  =  &Insts[N]  =  Insts 数组末尾
```

**对所有 opcode 都落在同一个地址**：紧接在 `Insts` 数组之后。再 `reinterpret_cast<MCOperandInfo*>` 从这里开始按 6 字节步进，加上 `OpInfoOffset` 就进入 `OperandInfo` 数组。

### 全量数值验证

我写了一版独立的 `MCInstrDesc` 复刻（`/tmp/tg16/pack/q.cpp`），用同一套布局，分别按反序和正序填充，然后对 9340 个 opcode 全量检查 `operands()` 是否落在金标准位置：

```console
# 反序填充（真实生成器的做法）
opcode -> operands() 地址映射全部正确: YES (mismatch=0)

# 正序填充
opcode -> operands() 地址映射全部正确: NO (mismatch=9340)
```

正序时 9340 条全部错位——**不是「偏一点」，是整体失效**。这就是为什么这个 `reverse()` 不能随手改。

再用真实生成代码把每个地址打出来（`/tmp/tg16/real/base.cpp`，链接 `libLLVMMC`，`Mini` 的 `ImplicitOps[1]` 版本，`MiniOpInfoBase = 1`）：

```console
&MiniDescs            = +0
&ImplicitOps[0]       = +10816
&OperandInfo[0]       = +10822
this+Opcode+1         = +10816
差异 (vs OperandInfo) = 6 bytes = 1 个 MCOperandInfo
OpInfoBase = 1
ADD operands() 相对 OperandInfo[0] = 151
```

读法：

1. `this + Opcode + 1` 落在 `+10816`，也就是 `&ImplicitOps[0]`，**不是** `&OperandInfo[0]`（`+10822`）。两者差 6 字节 = 1 个 `MCOperandInfo`。
2. `ADD` 的 `OpInfoOffset` 字段值是 `MiniOpInfoBase + 151 = 152`。
3. `operands()` 算出来是 `(MCOperandInfo*)(+10816) + 152`；而 `(MCOperandInfo*)(+10816) + 1 == &OperandInfo[0]`，所以最终落点是 `&OperandInfo[152 - 1]` = `OperandInfo[151]`（实测打印 151）。

于是 `OpInfoBase` 的语义清楚了：**它是「从 `ImplicitOps` 起点走到 `OperandInfo` 起点要几个槽」**，即 `(sizeof ImplicitOps + sizeof Padding) / sizeof(MCOperandInfo)`。所有 `OpInfoOffset` 都加上它，恰好抵消这 1 个槽（或 AArch64 的 35 个槽）的落差。

这个设计的收益是：`operands()` 的计算式里**不需要 `OperandInfo` 数组的名字**——`MCInstrDesc` 根本不知道那个数组叫什么，它只知道自己后面跟着一大片数据。整个「表内自引用」靠的就是这个恒定的基址偏置。

### 三个 `static_assert` 在防什么

`MCInstrDesc` 的字段布局是**跨 TU、跨编译器的 ABI**。生成器不敢假设，只敢断言（实测 `offsetof`）：

```text
offsetof Opcode=0 NumOperands=4 NumDefs=6 Size=7 SchedClass=8
         NumImplicitUses=10 NumImplicitDefs=11 OpInfoOffset=12 ImplicitOffset=14
         Flags=16 TSFlags=24
sizeof=32 align=8
sizeof(MCOperandInfo)=6 align=2
```

于是生成的三个断言：

```cpp
// 1. 结构体内
static_assert(alignof(MCInstrDesc) >= alignof(MCPhysReg),
              "Unwanted padding between Insts and ImplicitOps");
static_assert(alignof(MCInstrDesc) >= alignof(MCOperandInfo),
              "Unwanted padding between Insts and OperandInfo");

// 2. MC_DESC 段开头
static_assert((sizeof AArch64InstrTable::ImplicitOps +
               sizeof AArch64InstrTable::Padding) %
                  sizeof(MCOperandInfo) == 0);
```

`alignof(MCInstrDesc)=8`、`alignof(MCPhysReg)=2`、`alignof(MCOperandInfo)=2`，所以两个断言现在都成立：`Insts` 之后**没有**对齐填充，`&Insts[N]` 严格等于 `&ImplicitOps[0]`。如果哪天有人给 `MCInstrDesc` 加了个 `alignas(16)` 的成员、或者把某个字段改成 `__int128`，第二段那个断言会先炸——**它炸在生成的 `.inc` 里，指向 `InstrInfoEmitter.cpp:972` 的 `IfGuardEmitter`**，报错文本里写着 "Unwanted padding"，让你一眼知道是布局问题。

第二个断言（`OpInfoBase` 那个）保证 `ImplicitOps + Padding` 正好是整数个 `MCOperandInfo`。这直接引出 16.6。

## 16.6 `Padding` 的算术：一个必须看数字才懂的技巧

生成器写：

```cpp
OS << "  char Padding[sizeof(MCOperandInfo) - sizeof ImplicitOps % "
      "sizeof(MCOperandInfo)];\n";
```

AArch64 代入真实数字：`ImplicitOps[103]`（103 个 `MCPhysReg`，每个 2 字节）= 206 字节。`206 % 6 = 2`，所以 `Padding[6 - 2] = Padding[4]`。于是 `206 + 4 = 210 = 35 × 6`，`OpInfoBase = 35`。

我实编译量了五种规模（`clang++` 直接编译生成的 `.inc`，读 `sizeof`）：

| `ImplicitOps` | 字节 | `Padding` | 和 | `OpInfoBase` | 「最小需要槽数」 |
| --- | --- | --- | --- | --- | --- |
| `[1]` | 2 | 4 | 6 | 1 | 1 |
| `[3]` | 6 | 6 | 12 | 2 | 1 |
| `[4]` | 8 | 4 | 12 | 2 | 2 |
| `[6]` | 12 | 6 | 18 | 3 | 2 |
| `[103]` | 206 | 4 | 210 | 35 | 35 |

看第 2 行和第 4 行：当 `sizeof(ImplicitOps)` 已经是 6 的倍数时，公式给出**完整一个 `MCOperandInfo`** 的填充，于是 `OpInfoBase` 比「最小需要」多 1。实测确认这真的会出现在产物里：

```console
$ ./chk_Pad3
Pad3: sizeof(ImplicitOps)=6 sizeof(Padding)=6 OpInfoBase=2 实际需要=1
$ ./chk_Pad6
Pad6: sizeof(ImplicitOps)=12 sizeof(Padding)=6 OpInfoBase=3 实际需要=2
```

**白扔 6 字节**。为什么这么写？代码注释只解释了「让它们凑成整数个」，没解释为什么不用更省的写法。我的推断（**未在代码里找到依据，属于推测**）：`char Padding[0]` 不是标准 C++。实测 `-pedantic`：

```console
$ printf 'struct A { char pad[0]; int x; };\nint main(){ return sizeof(A); }\n' > zla.cpp
$ clang++ -std=c++17 -pedantic -c zla.cpp -o /dev/null
zla.cpp:1:21: warning: zero size arrays are an extension [-Wzero-length-array]
```

所以作者选了一个「永不为 0」的公式，代价是整除时浪费一整格。注意 LLVM 自己的构建**不开** `-pedantic`（所以直接写 `[0]` 也能过），说明这个公式不是"被编译器逼出来的"，而是作者自选的保守写法——这意味着它**可以被优化**，只要你在 PR 里解释清楚新的整除方案仍然不需要零长数组。

这条值得记住，因为它是**「读生成代码能发现真实优化机会」的范例**：6 字节 × 每个 target 一份，看起来微不足道；但如果你要做的是「让 `.inc` 更小」，你必须先知道这 6 字节的存在和它的历史原因，才不会在 review 里被一句「why? git blame 说是为了避免零长数组」打回。

顺便看 `ImplicitOps` 数组本身（实测 `Pad.inc`，给 `ADD` 加了 `let Uses = [R0, R1, R2]; let Defs = [R3];`）：

```cpp
  }, {
    /* 0 */
    /* 0 */ Mini::R0, Mini::R1, Mini::R2, Mini::R3,
  }, {
```

四行注释/数据，`/* 0 */` 是**去重后的列表号**不会重复出现——`EmittedLists` 是 `std::map<std::vector<const Record*>, unsigned>`，相同 implicit 列表在整个表里只存一份：

```cpp
// InstrInfoEmitter.cpp:954
std::map<std::vector<const Record *>, unsigned> EmittedLists;
...
if (EmittedLists.try_emplace(ImplicitOps, ImplicitListSize).second) {
  ImplicitLists.push_back(ImplicitOps);
  ImplicitListSize += ImplicitOps.size();
}
```

而每条记录的 `ImplicitOffset` 列填的就是 `EmittedLists[ImplicitOps]`（`emitRecord` 里 1329 行 `OS << EmittedLists[ImplicitOps] << ",\t0";`）。

两个顺序要分清：

- **offset 的赋值顺序**：`ImplicitListSize` 单调累加，谁先被塞进 map 谁先拿号。
- **表的实际输出顺序**：`for (auto &List : ImplicitLists)`——`ImplicitLists` 是 `push_back` 进来的，所以就是「首次出现顺序」。

两者一致，因此**输出是确定的**：它由 `NumberedInstructions` 的顺序（名字序，见 16.2）唯一决定。`std::map` 的比较器是 `std::less<std::vector<const Record*>>`，逐元素比**指针值**——这个 comparator 只影响查找，不影响输出顺序，所以「指针地址序」不会泄漏到产物里。这是它安全的原因，也是它能通过确定性测试的原因。

反例值得记住：如果你把 `EmittedLists` 换成 `DenseMap` 并直接遍历它来输出，产物顺序就会变成哈希序 → 每次跑出来的 `.inc` 都不同 → `--write-if-changed` 永远认为变了 → 全量重编。这正是上游对「输出前必须排序」这条规则的由来。

## 16.7 字符串池：两种不同的去重算法

`InstrInfoEmitter` 用的是 `SequenceToOffsetTable`（`llvm/utils/TableGen/Basic/SequenceToOffsetTable.h`），不是 `StringToOffsetTable`。两者算法完全不同，混淆它们会导致你算错产物大小。

### `SequenceToOffsetTable`：按**后缀**合并

核心思路是把序列按**反转字典序**排，这样「一个序列是另一个的后缀」就变成相邻关系：

```cpp
struct SeqLess {
  Less L;
  bool operator()(const SeqT &A, const SeqT &B) const {
    return std::lexicographical_compare(A.rbegin(), A.rend(), B.rbegin(),
                                        B.rend(), L);
  }
};
using SeqMap = std::map<SeqT, unsigned, SeqLess>;
```

`add()` 时如果新序列是已有序列的后缀，直接丢弃（返回）；如果已有序列是新序列的后缀，删掉那个旧的。最终每个序列只存"独有的尾巴"。

我写了个最小实验（`/tmp/tg16/sot/t.cpp`，真实输出）：

```console
$ ./t
const char Tab[] = {
  /* 0 */ "ZZZ\000"
  /* 4 */ "ADDXri\000"
  /* 11 */ "SUBXrr\000"
  /* 18 */ "ADDXrr\000"
};

total size = 25
ADDXrr -> 18
SUBXrr -> 11
Xrr -> 14
ZZZ -> 0
```

注意 `Xrr -> 14`：这个序列**没有自己的 entry**，它指向 `SUBXrr`（offset 11）的中间——`"SUBXrr"` 的第 3 个字符起正好是 `"Xrr"`。这就是后缀共享，`get()` 返回的是「偏移量」，消费者直接 `Data + offset` 就能拿到完整字符串。

### AArch64 的真实数字

用真实产物算（脚本解析 `AArch64InstrNameData[]` 与 `AArch64InstrNameIndices[]`）：

```text
opcodes: 9340   stored entries: 8658   shared(suffix) opcodes: 682
naive: 127049   actual: 117014   saved: 10035 (7.9%)
```

682 个 opcode 的名字没有自己的 entry，它们的 offset 落在别人中间。前几个：

```text
op    0 'PHI'                  off 52859 <- 'DBG_PHI'(off 52855)
op   10 'IMPLICIT_DEF'         off 41238 <- 'G_IMPLICIT_DEF'(off 41236)
op   74 'G_PHI'                off 52857 <- 'DBG_PHI'(off 52855)
op  335 'ABS_ZPmZ_B_UNDEF'     off 36083 <- 'SQABS_ZPmZ_B_UNDEF'(off 36081)
op  338 'ABS_ZPmZ_S_UNDEF'     off 40526 <- 'FABS_ZPmZ_H_UNDEF'(off 40525)
```

`G_PHI` 和 `PHI` 都藏在 `DBG_PHI` 里，一个 `DBG_PHI` 养了三条 opcode。命名规律（`G_` 前缀、`F`/`SQ` 前缀、`_UNDEF` 后缀）让这种合并命中率很高。

**但这不是普适的**。同一套算法跑在 X86 和 WebAssembly 上（实测）：

```text
X86GenInstrInfo.inc: entries=21705 table=326550 naive=326550 saved=0
WasmGenInstrInfo.inc: entries=1827 table=29840 naive=29840 saved=0
```

**0 节省**。X86 的助记符是 `ADD8ri`、`ADD16ri` 这种，彼此都不构成后缀关系；WebAssembly 的是 `I32Add`、`LocalGet`，同样。所以「后缀合并」是一个**赌命名规律的启发式**，赌赢了省 8%，赌输了完全不省，代价只是 `std::map` 的插入开销。

这个事实对你有两个实用含义：

1. **别用「生成文件大小」当性能指标去优化 `SequenceToOffsetTable` 的算法**——你改了之后 X86 一点不变，AArch64 变几个百分点，很难说服 reviewer。
2. 写测试时**不能假设 offset 与 opcode 有线性关系**。`AArch64InstrNameIndices[0] = 52859` 而 `[74] = 52857`——索引是递减的。任何「index[i] < index[i+1]」的断言都是错的。

### `StringToOffsetTable`：按**完整字符串**去重

`llvm/lib/TableGen/StringToOffsetTable.cpp` 是另一个类，用于 `IntrinsicEmitter`、`OptionParserEmitter`、`SearchableTableEmitter` 等（实测 grep 到 12 处 `EmitStringTableDef` 调用）。它用 `StringMap<unsigned>` 做**精确去重**，不做后缀合并，但有三个值得记住的性质：

**性质一：空字符串强制在 offset 0。**

```cpp
StringToOffsetTable(bool AppendZero = true, ...) {
  // Ensure we always put the empty string at offset zero. That lets empty
  // initialization also be zero initialization for offsets into the table.
  GetOrAddStringOffset("");
}
```

`{}` 初始化出来的 offset 全是 0，于是「没有名字」天然解析为空串，不需要额外的哨兵值。

**性质二：超过 64KB 且禁用长字面量时，降级成 char 数组。**

```cpp
bool UseChars = !EmitLongStrLiterals && AggregateString.size() > (64 * 1024);
```

`EmitLongStrLiterals` 是 `-long-string-literals` 选项（`lib/TableGen/Main.cpp:70`，默认 true），CMake 在检测到 MSVC 时会传 `false`。注释写得很清楚：*"MSVC silently miscompiles string literals longer than 64k in some circumstances."* ——**silently miscompiles**，这是最可怕的一类 bug，所以宁可让产物变丑变慢。

我实测了同一个 Mini target 的两种形态：

```console
$ llvm-tblgen -gen-instr-info -long-string-literals=false ... -o MiniNoLit.inc
```

```cpp
extern const char MiniInstrNameData[] = {
  /* 0 */ 'G', '_', 'F', 'L', 'O', 'G', '1', '0', 0,
  /* 9 */ 'G', '_', 'F', 'E', 'X', 'P', '1', '0', 0,
```

对比默认形态：

```cpp
  /* 0 */ "G_FLOG10\000"
  /* 9 */ "G_FEXP10\000"
```

两者**字节内容完全等价**（都含 `\0` 终止），只是 C++ 语法不同。我顺手量了一下编译代价（只编译这个数组定义的翻译单元，无优化）：

```console
== nm_lit   wall=0.03s  maxrss=69916kB
== nm_chars wall=0.18s  maxrss=76228kB
```

**6 倍差距**。这就是为什么默认值是 `true`——长字面量对 clang 来说是一次「memcpy 一段常量」，而 char 数组是逐个字符的初始化列表。注意这个实验的规模很小（117KB 数据），不能外推到整个 `.inc`；但方向是清楚的，也和注释里 "compile-time performance win" 的说法一致。

**性质三：`#pragma GCC diagnostic ignored "-Woverlength-strings"` 不是装饰。**

C++ 标准只要求编译器支持 65536 字节的字符串字面量。AArch64 的池子是 117,014 字节。我把那段 `#pragma` 剥掉、用 `-pedantic` 编译（实测）：

```text
warning: string literal of length 117014 exceeds maximum length 65536 that C++
         compilers are required to support [-Woverlength-strings]
```

保留 `#pragma` 时 `-pedantic` 干净通过（`PEDANTIC_EXIT=0`）。注意 clang 也认 `#pragma GCC diagnostic`，所以这个「GCC」字样不影响 clang。

而 `InstrInfoEmitter` 走的是 `SequenceToOffsetTable::emitStringLiteralDef`，它的 pragma 是**硬编码在 emit 函数里**的（`SequenceToOffsetTable.h:144`），不是可配置项。所以任何用 `SequenceToOffsetTable` 输出字符串的 backend 都自动获得这个保护——这是一个好设计，值得你仿效。

## 16.8 `extern const` vs `static`：一个 TU 还是多个 TU

`MC_DESC` 段的产物是定义（不是声明）：

```cpp
extern const AArch64InstrTable AArch64Descs = { ... };
extern const char AArch64InstrNameData[] = { ... };
extern const unsigned AArch64InstrNameIndices[] = { ... };
```

`extern` + 初始化 = **外部链接的定义**。这意味着它只能出现在一个 TU 里。我实测了违反这条规则的后果——把 `MiniGenInstrInfo.inc` 的 `MC_DESC` 段编进两个 `.cpp`：

```text
/usr/bin/ld: u2.o:(.rodata+0x0): multiple definition of `llvm::MiniDescs';
              u1.o:(.rodata+0x0): first defined here
/usr/bin/ld: u2.o:(.rodata+0x2de0): multiple definition of `llvm::MiniInstrNameData';
/usr/bin/ld: u2.o:(.rodata+0x3e80): multiple definition of `llvm::MiniInstrNameIndices';
```

三个符号全炸。这就是为什么 `MC_DESC` 只在 `MCTargetDesc.cpp` 里 define（每个 target 恰好一个 TU）——**不是约定，是链接器的硬约束**。

对照：`InitAArch64MCInstrInfo` 是 `static inline`（实测产物）：

```cpp
static inline void InitAArch64MCInstrInfo(MCInstrInfo *II) {
  II->InitMCInstrInfo(AArch64Descs.Insts, AArch64InstrNameIndices,
                      AArch64InstrNameData, nullptr, nullptr, 9340, nullptr, 0);
}
```

`static` 给每个 TU 一份自己的副本，`inline` 让「没用到就不生成」成立。**这是「数据要唯一、函数要多份」的标准配对**。

再对照 `CTOR_DTOR` 段：它的前 4 行是**声明**（`extern const ...;`），第 5 行起是**定义**（构造函数）。所以它既不能放进头文件（构造函数会重复定义），也不能放进两个 TU。它所在的 `AArch64InstrInfo.cpp` 恰好只有一个。

### 一个可以直接拿来理解链接性的最小实验

我写了一个两文件实验，`tab.h` 里是 `extern const T x = {...};`（外部链接定义），`a.cpp`/`b.cpp` 都 include：

```text
0000000000000000 R x     # a.o：R = 只读数据，全局符号
0000000000000000 R x     # b.o：同上
/usr/bin/ld: b.o:(.rodata+0x0): multiple definition of `x';
              a.o:(.rodata+0x0): first defined here
```

然后改成 `const T x = {...};`（C++ 里 const 默认内部链接）：

```text
0000000000000000 r x     # 小写 r = 本地符号，不参与链接
```

**大小写就是链接性的全部区别**。`extern const` 是大写 `R`（全局只读），`const` 是小写 `r`（本地只读）。你以后在生成的 `.inc` 里看到 `extern const`，就知道「这个文件只能被一个 TU include 这一段的定义」。

一个真正容易搞混的例子：`GET_INSTRINFO_ENUM` 段是**匿名 enum + 常量**，没有链接性问题，所以可以随便多次 include 到不同 TU（我实测两个 TU 各自 include 后链接成功，两个函数都返回 335）。**枚举没有符号**——这是它成为「跨 TU 共享常量」最廉价方式的原因。LLVM 里大量 `enum { OPCODE = N }` 就是利用这一点。

`GET_INSTRINFO_HEADER` 段（结构体声明）同样可以重复 include 到不同 TU（实测两个 TU 各 include 一次，链接正常）——只要**同一个 TU 内**不重复（16.3 已实测那个 `redefinition` 报错）。

## 16.9 读生成代码的方法论

生成文件动辄三万行，硬读是不可能的。我总结了一套由粗到细的四步，全部基于本章的实验：

**第一步：只看段边界。**

```bash
grep -n '#ifdef GET_\|#if defined(GET_\|#endif // GET_' File.inc
```

三十秒内知道这个文件有几个入口、每个入口多长。这一步决定了你后面要读哪几段——90% 的调试只需要 `ENUM` + `MC_DESC`。

**第二步：看结构体定义。**

```bash
sed -n '/^struct .*InstrTable {/,/^};/p' File.inc
```

四个数组的大小、`static_assert` 的内容、`Padding` 的大小，全在这里。**这是整个文件的「布局契约」**，比任何一段数据都重要。

**第三步：抽一条记录，逐字段对回 `emitRecord`。**

比如 `ZIP_VG4_4Z4Z_S`：

```cpp
{ 9339, 2, 1, 4, 958, 0, 0, AArch64OpInfoBase + 1457, 0,
  0|(1ULL<<MCID::UnmodeledSideEffects), 0x0ULL },  // ZIP_VG4_4Z4Z_S
```

对回 `InstrInfoEmitter.cpp:1311` 的顺序：`Num, MinOperands, DefOperands, Size, SchedClass, NumImplicitUses, NumImplicitDefs, OpInfoOffset, ImplicitOffset, Flags, TSFlags`。于是你读出了「2 个操作数、1 个 def、4 字节、调度类 958、无 implicit、操作数描述在 `OperandInfo[1457]`、有 side effects、无 target-specific flags」。

**这一步是理解 `.td` → 机器指令这块的最短路径**：你在 `.td` 里改 `let Size = 2`，就在这行看到 `4` 变 `2`。

**第三步半：查这一段到底有没有人用。**

这一步能发现真正的「死产物」：`GET_INSTRINFO_SCHED_ENUM` 在 AArch64 的产物里占 2,269 行（214,655 字节），但全仓库没有任何 AArch64 代码 define 它（实测 `grep -rn GET_INSTRINFO_SCHED_ENUM llvm/` 只命中 `Hexagon`、`AMDGPU`、`PowerPC` 三处，都是 `MCTargetDesc.h`）。也就是说 **AArch64 每次都生成、每次都不编译，纯占体积**。

```console
$ for t in AArch64 X86 RISCV ARM PowerPC Hexagon AMDGPU; do
    printf "%-10s %s\n" $t $(grep -rn GET_INSTRINFO_SCHED_ENUM llvm/lib/Target/$t | wc -l); done
AArch64    0
X86        0
RISCV      0
ARM        0
PowerPC    1
Hexagon    1
AMDGPU     1
```

原因很朴素：`emitEnums` 无条件输出 SCHED_ENUM 段（`InstrInfoEmitter.cpp:1473`），只有「这个 target 用 `InstrItineraryData`」时才有人 include 它。这类「无条件输出、条件消费」的段是生成器里最典型的技术债——它不会编译失败，只会让每个 target 的 `.inc` 白白大几百 KB。想做贡献的话，这是**低风险、可量化、有明确验收标准**的一类（Ch 22 会讲怎么把它写成一个能被接受的 PR）。

**第四步：看被共享的数据。**

```bash
# implicit 列表的共享
awk '/}, {/,/}, {/' File.inc | head -20
# 字符串池的共享（找没有自己 entry 的 offset）
python3 - <<'EOF'
# 见 16.7 的脚本
EOF
```

共享结构是「读起来不直观、但改起来最危险」的部分——你改一个 implicit 列表，可能同时影响十几条指令。

## 16.10 检查清单

改 `InstrInfoEmitter`（或任何生成结构体的 backend）之前，逐条确认：

- [ ] 新段用了 `IfDefEmitter`，名字唯一，且**没有**和已有段共用宏名
- [ ] 如果新段要在头文件里展开，确认它只含声明（不含 `extern const X = {...}` 这种定义）
- [ ] 如果新段会产出定义，确认它只会被一个 TU include（grep 全仓库确认）
- [ ] 如果改了 `Insts`/`ImplicitOps`/`OperandInfo` 的布局，同步改三个 `static_assert`，并跑一遍所有 target 的 `-gen-instr-info`（`Padding` 在不同 `ImplicitOps` 长度下行为不同）
- [ ] 如果改了 `reverse(NumberedInstructions)` 或 `--Num`，**立刻**检查 `MCInstrDesc::operands()` 的地址算术是否仍成立
- [ ] 如果用字符串池，确认空串在 offset 0，且确认产物是否需要 `-Woverlength-strings` 抑制
- [ ] 输出顺序依赖 `std::map` 时，确认比较器是名字序（可复现）而不是地址序（不可复现）
- [ ] 空输入的产物合法（空 namespace、`ImplicitOps[1]` 而不是 `[0]`、空 dict）

## 16.11 踩雷记录

**错误直觉 1：一个 `.inc` 只能被 include 一次（像普通头文件那样）。**
正确认识：`IfDefEmitter` 会输出 `#undef`，主动让守卫「自消耗」。同一文件被 include 8 次是设计目标（实测 AArch64 的 8 个 include 点）。被 include 两次而**且**两次都定义了同一个宏才是错误（实测 `redefinition of enumerator 'PHI'`）。

**错误直觉 2：`#if defined(GET_A) || defined(GET_B)` 那一段也是 `IfDefEmitter` 生成的。**
正确认识：它没有 `#undef`、`#endif` 后面跟的是完整条件表达式，那是 `IfGuardEmitter`。混用会炸：如果结构体块用了 `IfDefEmitter`，同时定义两个宏的 TU 会看到结构体展开两次。

**错误直觉 3：`this + Opcode + 1` 里的 `Opcode` 是「第几条」。**
正确认识：它是**枚举值**，而数组是反序填的，所以 `this + Opcode + 1` 对任意 opcode 都落在 `Insts` 数组末尾。正序填充时 9340 条全部错位（实测 mismatch=9340），不是「偏几位」。

**错误直觉 4：`char Padding[sizeof(MCOperandInfo) - sizeof ImplicitOps % sizeof(MCOperandInfo)]` 是「补齐到最小整数倍」。**
正确认识：当已经整除时它给**一整个** `MCOperandInfo` 的填充（`ImplicitOps[3]` → `Padding[6]`，`OpInfoBase=2` 而实际只需 1）。推断原因是为了避免非标准的零长数组；推断依据是零长数组在 `-pedantic` 下会被警告。这 6 字节是有意的浪费。

**错误直觉 5：`SequenceToOffsetTable` 会去重相同的字符串。**
正确认识：它只合并**后缀**，用反转字典序排列来找后缀关系。完全相同的字符串当然也是「后缀」，所以顺带去了重；但它的主目标是 `Xrr` 能藏进 `SUBXrr`。实测 AArch64 省 7.9%，X86 和 WebAssembly 省 **0%**——算法效果完全取决于命名规律。

**错误直觉 6：`InstrNameIndices` 是单调递增的。**
正确认识：它是「每个 opcode 名字在池子里的偏移」，而池子按反转字典序排列。实测 AArch64 的 9339 个相邻对里 **3572 对是递减、5767 对是递增**——完全无序。例如 `[0] = 52859`（`G_FLOG10`）而 `[74] = 52857`（`G_PHI`），因为 `G_PHI` 是 `DBG_PHI` 的后缀。任何「索引有序」的假设都会写出错误测试。

**错误直觉 7：`extern const X = {...}` 和 `const X = {...}` 只是风格差异。**
正确认识：前者是外部链接定义（全局符号 `R`），后者是内部链接（本地符号 `r`）。前者放进两个 TU 会 `multiple definition`（实测）。生成器用 `extern const` 给「唯一数据」、用 `static inline` 给「可多份的函数」，这个配对是有原因的。

**错误直觉 8：段集合对所有 target 一样。**
正确认识：`OPERAND_ENUM`/`NAMED_OPS` 挂在 `UseNamedOperandTable`，`OPERAND_TYPE*` 硬编码只在 `Namespace == "X86"` 时输出，`INSTRMAP_INFO` 挂在是否有 `InstrMapping` 记录。实测 AArch64 12 段、X86 12 段、Wasm 12 段，但**集合不同**；玩具 Mini 只有 10 段。

**错误直觉 9：`#pragma GCC diagnostic` 在 clang 下没用。**
正确认识：clang 完全支持 `#pragma GCC diagnostic push/pop/ignored`。实测：剥掉 pragma 后 `-pedantic` 报 `string literal of length 117014 exceeds maximum length 65536`；保留时干净通过。

**错误直觉 10：空 namespace 会被编译器报错或优化掉。**
正确认识：合法、保留、真实出现在产物里（Mini 的 `GET_INSTRINFO_HEADER` 段）。它是「该 target 没有带参数名的 `MIOperandInfo`」的自然结果，不是 bug。

## 16.12 延伸阅读

- **`llvm/include/llvm/TableGen/CodeGenHelpers.h`（全 160 行）**
  - 五个 RAII emitter 的完整实现。重点读 `IfDefEmitter` 的 `LateUndef` 语义、`NamespaceEmitter` 的 `trim`（为什么要吃前导 `::`）、`AnonNamespaceEmitter`。本章 16.3 的所有行为都出自这里。
- **`llvm/utils/TableGen/Basic/SequenceToOffsetTable.h`（189 行，全读）**
  - 后缀合并的全部算法。重点：`SeqLess`（反转字典序）、`add()` 里的两个 `isSuffix` 判断、`layout()` 里 `Terminator` 的计入方式、`emitStringLiteralDef` 的 pragma。第 135 行那个 `if (!EmitLongStrLiterals || Size == 0)` 分支就是 MSVC 降级路径。
- **`llvm/lib/TableGen/StringToOffsetTable.cpp`（195 行）**
  - 另一套去重。重点：构造函数里 `GetOrAddStringOffset("")` 那句注释（空串 → offset 0 让 `{}` 等价于零初始化）、`EmitStringTableStorageDef` 的 64KB 判断、`EmitString` 的 70 字符折行与「八进制转义后紧跟数字时断开字符串」的 MSVC C4125 规避。
- **`llvm/include/llvm/MC/MCInstrDesc.h` 的 `operands()` / `implicit_uses()` / `implicit_defs()`（242、569、583 行）**
  - 三处都用了 `this + Opcode + 1`。对照读能看出这个技巧是「统一基址」而不是「某个函数的巧合」。
- **`llvm/utils/TableGen/InstrInfoEmitter.cpp` 的 `run()`（922–1293）**
  - 一次读完九个段的发射顺序。重点看每个 `IfDefEmitter` 的作用域边界、`buildTargetSpecializedPseudoInstsMap()` 为什么在 `CollectOperandInfo` 之前、`EmitMapTable` 为什么在最后。
- **`llvm/utils/TableGen/Common/CodeGenTarget.cpp` 的 `ComputeInstrsByEnum()`（302–349）**
  - 排序规则（pseudo 优先、然后按名字）决定了整个文件里所有数组的顺序。改这里 = 全 LLVM 重排，理解它你才理解「为什么生成代码的顺序看起来这么怪」。
- **`llvm/lib/Target/AArch64/AArch64InstrInfo.h` 的 609 行附近 + `AArch64InstrInfo.cpp` 的 12502 行附近**
  - `HELPER_DECLS` 在类内展开、`HELPERS` 在 `.cpp` 展开的完整现场。这是「声明/定义分离」在生成代码里的标准做法。

## 下一章

本章讲了「产物长什么样」。下一章走进产物的生产者：`InstrInfoEmitter.cpp` 1500 行的完整走读——九个 timer 阶段各做什么、`CodeGenDAGPatterns` 这个依赖为什么会吞掉 90% 的时间、以及为什么 `-time-phases` 报 0.79 秒而真实耗时 10.4 秒。

[Ch 17 大型 backend 走读：`InstrInfoEmitter` 与它的 timer 阶段](./17-instrinfo-walkthrough.md)
