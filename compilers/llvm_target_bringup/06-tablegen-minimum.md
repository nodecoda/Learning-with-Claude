# Ch 06 — 最小 TableGen

> **目标**：能从零写出一个 MC-only target 的最小 TableGen 集合，解释每个 `-gen-*` 生成物服务谁，并判断哪些 `.td` 是必需的、哪些是 CodeGen 阶段才需要。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。下文用 `$TREE`、`$BUILD`、`$B=$BUILD/bin` 指代。

## 为什么需要这个？

如果没有 TableGen，一个 target 要手写这些东西：寄存器编号与名字、寄存器类、每条指令的 opcode 编号、操作数个数与类型、汇编语法、汇编器匹配表、反汇编表、调用约定、DAG 选指模式。它们必须彼此一致：`ADD` 的 opcode 是 3，但反汇编表里写成 4，工具链不会在编译期报错，而是运行到某条指令才崩。更糟的是，这些表通常有几千行，靠人眼同步不现实。

TableGen 解决的是**单一事实来源**问题：`.td` 里写一次指令记录，`llvm-tblgen` 用不同 backend 生成不同消费者需要的表。但它不是魔法——生成的是**碎片**（`.inc`），C++ 类、工厂函数、TargetRegistry 注册仍然要手写。本章要建立的判断力是：

1. 哪些 `.td` 是 MC-only 里程碑的最小集合？
2. 每个 `-gen-*` 到底生成什么、被哪个 C++ 文件 `#include`？
3. MiniRV 的 `auto-td` 是实验树特有机制，普通 target 应该走哪条路？

## 先建立直觉

把 TableGen 想成“目标描述的编译器前端”：`.td` 是源文件，`llvm-tblgen` 是编译器，`-gen-*` 是选择后端，`.inc` 是目标文件，C++ 的 `#include` 是链接器。

```
MiniRV.td
 ├─ include MiniRVRegisterInfo.td ──────────────┐
 ├─ include MiniRVCallingConv.td ───────────────┤
 ├─ include MiniRVInstrFormats.td ──────────────┤
 └─ include MiniRVInstrInfo.td ─────────────────┤
        └─ include auto-td/AutoTdInstrInfo.inc  │  (实验树特有)
                                                ▼
                                          llvm-tblgen
        ┌──────────────┬──────────────┬──────────────┬──────────────┐
        │-gen-register │-gen-instr    │-gen-asm      │-gen-asm      │
        │  -info       │  -info       │  -writer     │  -matcher    │
        ▼              ▼              ▼              ▼              ▼
 GenRegisterInfo  GenInstrInfo   GenAsmWriter   GenAsmMatcher  GenDisassembler
      .inc             .inc           .inc           .inc        Tables.inc
        │              │              │              │              │
        ▼              ▼              ▼              ▼              ▼
 MCRegisterInfo    MCInstrInfo    MCInstPrinter  MCTargetAsm     MCDisassembler
 / TargetRegister  / InstrInfo    (打印汇编)      Parser(匹配)    (解码字节)
```

关键点：**生成物不是“完整实现”**。`GenRegisterInfo.inc` 只提供表和 `InitMCRegisterInfo()`，`MCRegisterInfo` 对象仍由 `MCTargetDesc` 里的工厂 `new` 出来；`GenAsmWriter.inc` 只提供 `printInstruction()` 的函数体，`MiniRVInstPrinter` 类仍要手写。

## 主线：一个 MC-only target 的最小 TableGen 集合

### 1. 十个概念，对应哪些生成器

| 职责 | `.td` 载体 | 生成器 | 生成文件 | 主要消费者 |
|---|---|---|---|---|
| 寄存器与寄存器类 | `MiniRVRegisterInfo.td` | `-gen-register-info` | `MiniRVGenRegisterInfo*.inc` | `MCRegisterInfo`、`TargetRegisterInfo` |
| 指令记录、操作数、标志 | `MiniRVInstrInfo.td` | `-gen-instr-info` | `MiniRVGenInstrInfo.inc` | `MCInstrInfo`、`MachineInstr` |
| 指令位格式 | `MiniRVInstrFormats.td` | 无 | 无 | 被指令记录继承 |
| 汇编打印 | `AsmString`、`AsmWriter` | `-gen-asm-writer` | `MiniRVGenAsmWriter.inc` | `MiniRVInstPrinter` |
| 汇编匹配 | `AsmString`、操作数类 | `-gen-asm-matcher` | `MiniRVGenAsmMatcher.inc` | `MiniRVAsmParser` |
| 反汇编 | 指令位 + `DecoderMethod` | `-gen-disassembler` | `MiniRVGenDisassemblerTables.inc` | `MiniRVDisassembler` |
| CPU、feature、调度 | `Processor`、`Feature` | `-gen-subtarget` | `MiniRVGenSubtargetInfo.inc` | `MCSubtargetInfo` |
| 调用约定 | `MiniRVCallingConv.td` | `-gen-callingconv` | `MiniRVGenCallingConv.inc` | `MiniRVISelLowering` |
| DAG 选指模式 | `Pat`、`Pattern` | `-gen-dag-isel` | `MiniRVGenDAGISel.inc` | `MiniRVISelDAGToDAG` |
| 伪指令降级 | `PseudoInstExpansion` | `-gen-pseudo-lowering` | `MiniRVGenMCPseudoLowering.inc` | `MiniRVAsmPrinter` |

还有两个不在标题里、但实际必需的生成物：

- `-gen-emitter` → `MiniRVGenMCCodeEmitter.inc`：把 `MCInst` 编成机器码。没有它，`llvm-mc -filetype=obj` 和 `llc -filetype=obj` 都出不来。
- `-gen-sd-node-info` → `MiniRVGenSDNodeInfo.inc`：目标自定义 `SDNode` 的枚举与类型信息。MiniRV 的 `MiniRVISD::BRCC`、`MiniRVISD::CALL` 就靠它。

所以“最小集合”要分阶段说：

- **MC-only（能汇编、反汇编、出 ELF）**：RegisterInfo、InstrInfo、AsmWriter、AsmMatcher、Disassembler、SubtargetInfo；再加 MCCodeEmitter。
- **能跑 `llc`**：再补 CallingConv、DAGISel、MCPseudoLowering、SDNodeInfo。
- **InstrFormats 不是生成器**：它只是 TableGen 类，把位域布局写进每条指令记录。

### 2. 走读 `MiniRV.td`

`MiniRV.td` 是整个 target 的入口，去掉版权头后核心只有二十多行：

```tablegen
include "llvm/Target/Target.td"
include "MiniRVRegisterInfo.td"

defm : RemapAllTargetPseudoPointerOperands<GPR>;

class Proc<string Name, list<SubtargetFeature> Features>
    : Processor<Name, NoItineraries, Features>;
def : Proc<"generic", []>;

include "MiniRVCallingConv.td"
include "MiniRVInstrInfo.td"

def MiniRVInstrInfo : InstrInfo;

def MiniRVAsmWriter : AsmWriter {
  string AsmWriterClassName = "InstPrinter";
}

def MiniRVAsmParser : AsmParser;

def MiniRV : Target {
  let InstructionSet = MiniRVInstrInfo;
  let AssemblyParsers = [MiniRVAsmParser];
}
```

**`def MiniRV : Target`**：这是 target 的顶层记录，不是 C++ 的 `Target` 对象。它告诉我们三件事：指令集描述是 `MiniRVInstrInfo`，汇编解析器是 `MiniRVAsmParser`，其余属性用默认值。`InstructionSet` 会驱动 `-gen-instr-info` 的所有输出；`AssemblyParsers` 决定 `-gen-asm-matcher` 把匹配函数放进哪个类。

**`MiniRVAsmWriter : AsmWriter`**：`AsmWriterClassName = "InstPrinter"` 是生成代码里的类名后缀。TableGen 会拼成 `MiniRVInstPrinter::printInstruction(...)`，所以这个名字必须和 `MiniRVInstPrinter.h` 里的 C++ 类名一致。注意：`def MiniRV : Target` **没有**写 `let AssemblyWriters = [MiniRVAsmWriter];`，所以这个记录目前没有被 `Target` 引用，生成器用的是默认的 `DefaultAsmWriter`。默认值同样是 `"InstPrinter"`，所以结果碰巧正确。这是本章后面要讲的坑之一。

**`MiniRVAsmParser : AsmParser`**：只声明“解析器类名后缀是 `AsmParser`”，拼出 `MiniRVAsmParser`。真正的 tokenizer、操作数谓词、`parseInstruction()` 都在 `AsmParser/MiniRVAsmParser.cpp` 手写；`-gen-asm-matcher` 只生成 `MatchInstructionImpl()` 等匹配表。

**`Processor`**：`class Proc<...> : Processor<Name, NoItineraries, Features>` 是一个薄包装，把 `Processor` 的第二个参数固定成“无 itinerary”。`def : Proc<"generic", []>;` 是一条匿名记录，注册名为 `generic` 的 CPU，不带任何 feature。它让 `llc -mcpu=generic` 和 `llvm-mc -mcpu=generic` 有合法值；传入不存在的 CPU 时工具只会警告：

```console
$ $B/llc -mtriple=minirv64 -mcpu=nope -O2 $TREE/llvm/test/CodeGen/MiniRV/alu.ll -o /dev/null
'nope' is not a recognized processor for this target (ignoring processor)
'nope' is not a recognized processor for this target (ignoring processor)
```

**`RemapAllTargetPseudoPointerOperands<GPR>`**：LLVM 的标准伪指令（`LOAD_STACK_GUARD`、`PREALLOCATED_ARG`、`PATCHABLE_EVENT_CALL`、`PATCHABLE_TYPED_EVENT_CALL`）的操作数里有一个 `ptr_rc` 占位类型。每个 target 必须把它映射到自己的指针寄存器类，否则 TableGen 生成/校验时会遇到“这个操作数没有具体寄存器类”的问题。MiniRV 只有 `GPR`，所以整类映射过去。`RemapAllTargetPseudoPointerOperands` 的定义在 `llvm/Target/Target.td`，它 foreach `PseudosWithPtrOps`，为每条标准伪指令生成一个 `RemapPointerOperands` 记录。

### 3. `MiniRVRegisterInfo.td`：32 个寄存器和一个类

```tablegen
class MiniRVReg<bits<5> num, string n> : Register<n> {
  let Namespace = "MiniRV";
  let HWEncoding{4-0} = num;
  let DwarfNumbers = [num];
}

foreach i = 0-31 in {
  def X#i : MiniRVReg<i, "x"#i>;
}

def GPR : RegisterClass<"MiniRV", [i64], 64, (sequence "X%u", 0, 31)>;
```

三行字段各有用途：

- `HWEncoding{4-0} = num`：机器码里的 5 位寄存器编号。`MCCodeEmitter::getMachineOpValue()` 通过 `MCRegisterInfo::getEncodingValue()` 取它。
- `DwarfNumbers = [num]`：DWARF 寄存器编号。后面的 CFI/unwind 会用到：`x1` 的 DWARF 号就是 1。
- `Namespace = "MiniRV"`：生成 `MiniRV::X0` 这样的枚举。

`GPR` 是 32 个 `i64` 寄存器的类，spill 大小 64 位。`CSR_MiniRV` 把 `X1`、`X8`、`X9`、`X18..X27` 标成 callee-saved。`X1` 在 psABI 里不是“调用后必须保持”的寄存器，但一个自己发起调用的函数必须把返回地址保存到某处；把它列进 callee-saved，寄存器分配器就会在函数确实破坏它时 spill。

### 4. `MiniRVInstrFormats.td`：只定义“形状”，不生成文件

```tablegen
class InstFormat<bits<5> val> { bits<5> Value = val; }
def InstFormatR : InstFormat<1>;
def InstFormatI : InstFormat<3>;
def InstFormatS : InstFormat<4>;
def InstFormatB : InstFormat<5>;
def InstFormatU : InstFormat<6>;
def InstFormatJ : InstFormat<7>;

class MiniRVInst<dag outs, dag ins, string opcodestr, string argstr,
                 list<dag> pattern, InstFormat format> : Instruction {
  field bits<32> Inst;
  InstFormat Format = format;
  let Namespace = "MiniRV";
  let Size = 4;
  dag OutOperandList = outs;
  dag InOperandList = ins;
  let AsmString = opcodestr # "\t" # argstr;
  let Pattern = pattern;
}
```

`MiniRVInst` 只声明“有一条 32 位 `Inst` 字段、固定 4 字节、有输出/输入操作数、有汇编字符串”。它**故意不写任何具体位域**：每条指令的 `Inst{31-25}`、`Inst{14-12}` 等由 `auto-td` 生成的记录填。`InstFormat` 这个字段在 MiniRV 里只是给读 `.td` 的人看的，C++ 侧没有消费者；注释说“与 `MiniRVBaseInfo.h` 保持同步”，但 MiniRV 目录里并没有 `MiniRVBaseInfo.h`。真正决定编码的是 `Inst` 位和生成的 `MCCodeEmitter` 表。

### 5. `MiniRVInstrInfo.td`：手写操作数类，自动生成指令记录

文件开头先定义汇编操作数类：

```tablegen
class MiniRVImmAsmOperand<string name, string predicate> : AsmOperandClass {
  let Name = name;
  let PredicateMethod = predicate;
  let RenderMethod = "addImmOperands";
  let DiagnosticType = "Invalid" # name;
}

def SImm12AsmOperand : MiniRVImmAsmOperand<"SImm12", "isSImm12">;
def UImm20AsmOperand : MiniRVImmAsmOperand<"UImm20", "isUImm20">;
def UImm6AsmOperand  : MiniRVImmAsmOperand<"UImm6", "isUImm6">;
def UImm5AsmOperand  : MiniRVImmAsmOperand<"UImm5", "isUImm5">;
```

`PredicateMethod` 是 `MiniRVAsmParser` 里的成员函数名。生成器把它写进匹配表：匹配到 `addi` 的第三个操作数时，调用 `isSImm12()`；失败就报 `InvalidSImm12` 对应的诊断。真正的谓词实现在 `AsmParser/MiniRVAsmParser.cpp`：

```cpp
bool isSImm12() const {
  if (Kind != k_Imm)
    return false;
  int64_t Val;
  return Imm->evaluateAsAbsolute(Val) && isInt<12>(Val);
}
```

#### 六个必须讲清楚的操作数

| 操作数 | 类型与谓词 | 汇编匹配类 | 编码/解码 | 语义 |
|---|---|---|---|---|
| `simm12` | `Operand<i64>` + `ImmLeaf<isInt<12>>` | `SImm12AsmOperand` | `DecodeSImm12` | 12 位有符号立即数；`addi`、load/store offset |
| `uimm20` | `Operand<i64>` | `UImm20AsmOperand` | `DecodeUImm20` | 20 位无符号字段；`lui`、`auipc` |
| `uimm6` | `Operand<i64>` + `ImmLeaf<isUInt<6>>` | `UImm6AsmOperand` | `DecodeUImm6` | RV64 的 6 位移位量 |
| `simm13_lsb0` | `Operand<OtherVT>` + `ImmLeaf<isShiftedInt<12,1>>` | `BranchTargetAsmOperand` | `DecodeSImm13Lsb0` | 分支偏移，低 1 位隐含为 0 |
| `simm21_lsb0` | `Operand<OtherVT>` + `ImmLeaf<isShiftedInt<20,1>>` | `JalTargetAsmOperand` | `DecodeSImm21Lsb0` | `jal` 偏移，低 1 位隐含为 0 |
| `bare_symbol` | `Operand<iPTR>` | `JalTargetAsmOperand` | 无 | 符号目标；只用于伪指令 |

几个容易忽略的细节：

- **`ImmLeaf` 不是可有可无的装饰**。它让操作数可以作为 SelectionDAG 的立即数叶子参与匹配。`simm13_lsb0` 有 `ImmLeaf`，所以生成的分支指令记录用它当立即数操作数；`simm13_lsb0_bb` 故意没有 `ImmLeaf`，因为实际分支模式给的是基本块 `bb:$dst`，不是立即数。
- **`simm13_lsb0` 的 `EncoderMethod` 是 `getBranchTargetOpValue`**。B 格式的立即数在指令字里被打散成四段，不能靠普通的 `getMachineOpValue()` 直接塞进去。编码器在 `MiniRVMCCodeEmitter::getBranchTargetOpValue()` 里处理立即数或生成 fixup。
- **`bare_symbol` 没有 `EncoderMethod`**。它出现在 `PseudoCALL`、`PseudoTAIL`、`PseudoLA` 这些伪指令上；伪指令在 `MCPseudoLowering` 或 `AsmPrinter` 阶段就展开成真实指令，`bare_symbol` 不会走到编码器。
- **`uimm6` 与 `uimm5` 分开**：RV64 的 `slli/srli/srai` 用 6 位移位量，`*W` 形式用 5 位。混用会让 `slli x1, x2, 64` 这类输入在汇编器里被错误接受或拒绝。

操作数定义完之后才是：

```tablegen
include "auto-td/AutoTdInstrInfo.inc"
```

这一行把 62 条由 YAML 语料生成的指令记录插进来（`add`、`addi`、`ld`、`beq`、`jal`、`mul`、`div` 等）。再往后是手写的伪指令（`PseudoCALL`、`PseudoRET`、`PseudoTAIL`、`PseudoBR` 等）和 `Pat` 模式。也就是说，**MiniRV 的指令记录不是手写的，但把记录接到 SelectionDAG 的语义模式是手写的**。

### 6. 生成物地图：先找对目录

任务里写的是 `$BUILD/llvm/lib/Target/MiniRV/`，但这个 build 的实际布局是 `$BUILD/lib/Target/MiniRV/`（没有中间那层 `llvm/`）。先确认真实路径：

```console
$ cd $BUILD/lib/Target/MiniRV && ls -lh *.inc auto-td/*.inc
-rw-rw-r-- 1 ubuntu ubuntu  36K Nov 13 22:02 MiniRVGenAsmMatcher.inc
-rw-rw-r-- 1 ubuntu ubuntu  26K Nov 14 00:15 MiniRVGenAsmWriter.inc
-rw-rw-r-- 1 ubuntu ubuntu 3.1K Nov 14 01:43 MiniRVGenCallingConv.inc
-rw-rw-r-- 1 ubuntu ubuntu  81K Nov 14 03:22 MiniRVGenDAGISel.inc
-rw-rw-r-- 1 ubuntu ubuntu  28K Nov 14 00:15 MiniRVGenDisassemblerTables.inc
-rw-rw-r-- 1 ubuntu ubuntu 154K Nov 14 00:24 MiniRVGenInstrInfo.inc
-rw-rw-r-- 1 ubuntu ubuntu  11K Nov 14 00:15 MiniRVGenMCCodeEmitter.inc
-rw-rw-r-- 1 ubuntu ubuntu 2.8K Nov 14 00:15 MiniRVGenMCPseudoLowering.inc
-rw-rw-r-- 1 ubuntu ubuntu  523 Nov 13 18:02 MiniRVGenRegisterInfo.inc
-rw-rw-r-- 1 ubuntu ubuntu 1.4K Nov 13 18:02 MiniRVGenRegisterInfoEnums.inc
-rw-rw-r-- 1 ubuntu ubuntu 2.4K Nov 13 18:02 MiniRVGenRegisterInfoHeader.inc
-rw-rw-r-- 1 ubuntu ubuntu  11K Nov 13 18:02 MiniRVGenRegisterInfoMCDesc.inc
-rw-rw-r-- 1 ubuntu ubuntu 9.6K Nov 13 22:07 MiniRVGenRegisterInfoTargetDesc.inc
-rw-rw-r-- 1 ubuntu ubuntu 3.4K Nov 13 23:50 MiniRVGenSDNodeInfo.inc
-rw-rw-r-- 1 ubuntu ubuntu 9.7K Nov 13 18:02 MiniRVGenSubtargetInfo.inc
-rw-rw-r-- 1 ubuntu ubuntu  34K Nov 13 22:08 auto-td/AutoTdInstrInfo.inc
-rw-rw-r-- 1 ubuntu ubuntu   66 Nov 13 22:08 auto-td/AutoTdManifests.inc
```

| 文件 | 行数（`wc -l`） | 里面有什么 |
|---|---:|---|
| `MiniRVGenInstrInfo.inc` | 1980 | `GET_INSTRINFO_ENUM` 的 opcode 枚举、`MiniRVInstrTable`/`MiniRVDescs`、`InitMiniRVMCInstrInfo()`、操作数信息 |
| `MiniRVGenDAGISel.inc` | 1412 | `MiniRVDAGToDAGISel::Select()` 的状态机匹配表 |
| `MiniRVGenAsmMatcher.inc` | 938 | `MatchInstructionImpl()`、`convertToMCInst()`、诊断类型、寄存器名匹配 |
| `MiniRVGenAsmWriter.inc` | 1104 | `getMnemonic()`、`printInstruction()`、`AsmStrs` 字符串池 |
| `MiniRVGenDisassemblerTables.inc` | 483 | `DecoderTable32`、`decodeInstruction()`、`decodeToMCInst()` |
| `MiniRVGenMCCodeEmitter.inc` | 407 | `getBinaryCodeForInstr()` 和 `InstBits[]` |
| `MiniRVGenRegisterInfoMCDesc.inc` | 419 | `MiniRVRegDesc`、寄存器类表、`InitMiniRVMCRegisterInfo()` |
| `MiniRVGenRegisterInfoTargetDesc.inc` | 296 | `TargetRegisterInfo` 用的 VT 表、subreg 表、寄存器类存储 |
| `MiniRVGenSubtargetInfo.inc` | 283 | feature 枚举、CPU 表、`createMiniRVMCSubtargetInfoImpl()`、调度表 |
| `MiniRVGenSDNodeInfo.inc` | 112 | `MiniRVISD::BRCC` 等目标节点的枚举/类型 |
| `MiniRVGenMCPseudoLowering.inc` | 94 | `MiniRVAsmPrinter::lowerPseudoInstExpansion()` |
| `MiniRVGenCallingConv.inc` | 90 | `CC_MiniRV()`、`RetCC_MiniRV()` |
| `MiniRVGenRegisterInfoEnums.inc` | 73 | `X0..X31`、`GPRRegClassID`、压力集枚举 |
| `MiniRVGenRegisterInfoHeader.inc` | 48 | `MiniRVGenRegisterInfo` 类声明 |
| `MiniRVGenRegisterInfo.inc` | 28 | 只是一个按 `#ifdef` 分发到上面四个文件的包装 |
| `auto-td/AutoTdInstrInfo.inc` | 1303 | 62 条 `def ADD : MiniRVInst<...>` 记录 |

`MiniRVGenRegisterInfo.inc` 本身只有 28 行，因为 `-gen-register-info` 现在会一次产出五个文件，包装文件只负责分发：

```cpp
#ifdef GET_REGINFO_ENUM
#include "MiniRVGenRegisterInfoEnums.inc"
#endif
#ifdef GET_REGINFO_MC_DESC
#include "MiniRVGenRegisterInfoMCDesc.inc"
#endif
#ifdef GET_REGINFO_HEADER
#include "MiniRVGenRegisterInfoHeader.inc"
#endif
#ifdef GET_REGINFO_TARGET_DESC
#include "MiniRVGenRegisterInfoTargetDesc.inc"
#endif
```

这也是为什么 CMake 只写了一行 `llvm_tablegen(MiniRVGenRegisterInfo.inc -gen-register-info)`，构建目录里却多出四个 `.inc`：它们是同一个 custom command 的副产物。

### 7. 重新生成：确认生成器名字和可重复性

先确认这个 `llvm-tblgen` 支持的 backend 名字：

```console
$ $B/llvm-tblgen --help | grep -E 'gen-(register-info|instr-info|asm-matcher|asm-writer|disassembler|emitter|subtarget|callingconv|dag-isel|pseudo-lowering|sd-node-info)'
      --gen-asm-matcher                       - Generate assembly instruction matcher
      --gen-asm-writer                        - Generate assembly writer
      --gen-callingconv                       - Generate calling convention descriptions
      --gen-emitter                           - Generate machine code emitter
      --gen-dag-isel                          - Generate a DAG instruction selector
      --gen-disassembler                      - Generate disassembler
      --gen-instr-info                        - Generate instruction descriptions
      --gen-pseudo-lowering                   - Generate pseudo instruction lowering
      --gen-register-info                     - Generate registers and register classes info
      --gen-sd-node-info                      - Generate target SDNode descriptions
      --gen-subtarget                         - Generate subtarget enumerations
```

再按构建系统里的真实命令行重新生成一个文件。`build.ninja` 里 `-gen-instr-info` 的 include 路径是：源码 target 目录、build 的 `include/`、源码 `llvm/include/`、build 的 target 目录、源码 `llvm/lib/Target/`。其中 build 的 target 目录必须有，否则 `auto-td/AutoTdInstrInfo.inc` 找不到：

```console
$ cd $BUILD/lib/Target/MiniRV
$ $B/llvm-tblgen -gen-instr-info \
    -I$TREE/llvm/lib/Target/MiniRV \
    -I$BUILD/include \
    -I$TREE/llvm/include \
    -I$BUILD/lib/Target/MiniRV \
    -I$TREE/llvm/lib/Target \
    $TREE/llvm/lib/Target/MiniRV/MiniRV.td \
    -o /tmp/MiniRVGenInstrInfo.regen.inc
$ cmp /tmp/MiniRVGenInstrInfo.regen.inc MiniRVGenInstrInfo.inc && echo BYTE-IDENTICAL
BYTE-IDENTICAL
```

同样的方法可以验证其他四个 MC 生成器：

```console
$ T=$TREE/llvm/lib/Target/MiniRV
$ for spec in "asm-matcher MiniRVGenAsmMatcher.inc" "asm-writer MiniRVGenAsmWriter.inc" \
               "disassembler MiniRVGenDisassemblerTables.inc"; do
    set -- $spec
    $B/llvm-tblgen -gen-$1 -I$T -I$BUILD/include -I$TREE/llvm/include \
      -I$BUILD/lib/Target/MiniRV -I$TREE/llvm/lib/Target \
      $T/MiniRV.td -o /tmp/regen.inc
    cmp -s /tmp/regen.inc $BUILD/lib/Target/MiniRV/$2 && echo "$1: identical"
  done
asm-matcher: identical
asm-writer: identical
disassembler: identical
```

`-gen-register-info` 会写五个文件，单独在空目录里验证：

```console
$ mkdir -p /tmp/regtest && cd /tmp/regtest
$ T=$TREE/llvm/lib/Target/MiniRV
$ $B/llvm-tblgen -gen-register-info -I$T -I$BUILD/include -I$TREE/llvm/include \
    -I$BUILD/lib/Target/MiniRV -I$TREE/llvm/lib/Target \
    $T/MiniRV.td -o MiniRVGenRegisterInfo.inc
$ for f in *.inc; do cmp $f $BUILD/lib/Target/MiniRV/$f && echo "$f identical"; done
MiniRVGenRegisterInfo.inc identical
MiniRVGenRegisterInfoEnums.inc identical
MiniRVGenRegisterInfoHeader.inc identical
MiniRVGenRegisterInfoMCDesc.inc identical
MiniRVGenRegisterInfoTargetDesc.inc identical
```

这说明生成物是**可重复的构建产物**，不是谁手工改出来的。调试 TableGen 时，最有效的办法就是改 `.td`，重跑这条命令，直接 diff `.inc`。

### 8. `auto-td`：实验树特有，不要当成 LLVM 标准

MiniRV 的 `CMakeLists.txt` 开头有：

```cmake
include(AutoTD)
llvm_autotd_generate(MiniRV
  TARGET_ROOT ${CMAKE_CURRENT_SOURCE_DIR}
  RISCV_OPCODES ${CMAKE_CURRENT_SOURCE_DIR}/auto-td/opcodes)
set(LLVM_TARGET_DEPENDS ${MiniRV_AUTOTD_INSTRINFO})
```

`llvm_autotd_generate()` 定义在 `llvm/cmake/modules/AutoTD.cmake`。它用 Python 脚本 `llvm/utils/auto-td/auto-td-gen` 读取：

```
auto-td/config.yaml
auto-td/instructions/rv-i/*.yaml
auto-td/instructions/rv-m/*.yaml
auto-td/opcodes/arg_lut.csv
auto-td/opcodes/extensions/rv_i, rv_m
auto-td/taxonomy/core.yaml
```

生成 `AutoTdInstrInfo.inc`、空的 `AutoTdManifests.inc` 和 `AutoTdCoverage.md`。语料很小，一条 YAML 只有五个字段：

```yaml
mnemonic: add
opcode: {repo: riscv-opcodes, extension: rv_i, key: add}
spec: minirv.core.r_type
domain: rv-i
status: auto_full
```

生成出来的记录长这样：

```tablegen
// auto-td: add
//   opcode: riscv-opcodes/rv_i/add
//   spec:   minirv.core.r_type
def ADD : MiniRVInst<(outs GPR:$rd), (ins GPR:$rs1, GPR:$rs2), "add", "$rd, $rs1, $rs2", [], InstFormatR> {
  let Predicates = [];
  let hasSideEffects = 0;
  let mayLoad = 0;
  let mayStore = 0;
  let hasNoSchedulingInfo = 1;
  bits<5> rs2;
  bits<5> rs1;
  bits<5> rd;
  let Inst{31-25} = 0b0000000;
  let Inst{24-20} = rs2;
  let Inst{19-15} = rs1;
  let Inst{14-12} = 0b000;
  let Inst{11-7} = rd;
  let Inst{6-2} = 0b01100;
  let Inst{1-0} = 0b11;
}
```

统计一下：`auto-td/instructions` 下有 62 个 `.yaml`，`AutoTdInstrInfo.inc` 里正好 62 个 `^def `。覆盖报告 `AutoTdCoverage.md` 也写 `auto_full: 62`。

**这不是普通 LLVM target 的做法。** 普通 target 的路径是手写 TableGen 类，再用 `def`/`multiclass` 实例化。以 RISC-V 为例：

```tablegen
// RISCVInstrFormats.td
class RVInstR<bits<7> funct7, bits<3> funct3, RISCVOpcode opcode, dag outs,
              dag ins, string opcodestr, string argstr>
    : RVInstRBase<funct3, opcode, outs, ins, opcodestr, argstr> {
  let Inst{31-25} = funct7;
}

// RISCVInstrInfo.td
let hasSideEffects = 0, mayLoad = 0, mayStore = 0 in
class ALU_rr<bits<7> funct7, bits<3> funct3, string opcodestr,
             bit Commutable = 0>
    : RVInstR<funct7, funct3, OPC_OP, (outs GPR:$rd), (ins GPR:$rs1, GPR:$rs2),
              opcodestr, "$rd, $rs1, $rs2"> {
  let isCommutable = Commutable;
}

def ADD : ALU_rr<0b0000000, 0b000, "add", Commutable=1>,
          Sched<[WriteIALU, ReadIALU, ReadIALU]>;
```

RISC-V 的 `RISCVInstrInfo.td` 在文件末尾 `include` 了三十多个 `RISCVInstrInfo*.td`（`M`、`A`、`F`、`D`、`V`……），每个扩展一个文件，全部手写。MiniRV 只用一个 `AutoTdInstrInfo.inc` 替代了这一层。你在自己的 target 里应该先走 RISC-V 那条路：**格式类 + 指令 def + Pat 模式**，除非你也在做“从机器可读 opcode 数据库生成记录”的实验。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| Target 入口 | `llvm/lib/Target/MiniRV/MiniRV.td` | `def MiniRV : Target`、`MiniRVAsmWriter`、`Proc` | 顶层记录如何引用 InstrInfo/AsmParser |
| 寄存器 | `MiniRVRegisterInfo.td` | `MiniRVReg`、`GPR`、`CSR_MiniRV` | `HWEncoding`、`DwarfNumbers`、callee-saved |
| 格式 | `MiniRVInstrFormats.td` | `MiniRVInst`、`InstFormat*` | `field bits<32> Inst` 与 `AsmString` |
| 指令与操作数 | `MiniRVInstrInfo.td` | `simm12`、`simm13_lsb0`、`bare_symbol`、`include auto-td` | 手写操作数类与生成记录的边界 |
| 生成记录 | `auto-td/instructions/*.yaml` | `mnemonic`、`opcode`、`spec` | YAML 如何描述一条指令 |
| 自动生成 | `llvm/cmake/modules/AutoTD.cmake` | `llvm_autotd_generate` | 实验树特有的构建钩子 |
| 生成物 | `$BUILD/lib/Target/MiniRV/*.inc` | `InitMiniRVMCInstrInfo`、`MatchInstructionImpl`、`DecoderTable32` | 每个 `.inc` 服务谁 |
| 参照实现 | `RISCVInstrFormats.td`、`RISCVInstrInfo.td` | `RVInstR`、`ALU_rr`、`def ADD` | 标准手写路径 |

## 实测：生成器、生成物与可重复性

本章已经跑了三组实测：

1. `llvm-tblgen --help` 确认 `-gen-register-info`、`-gen-instr-info`、`-gen-asm-matcher`、`-gen-asm-writer`、`-gen-disassembler` 都存在。
2. 用 `build.ninja` 里的真实 include 路径重新生成 `-gen-instr-info`，与构建目录里的文件 `cmp` 得到 `BYTE-IDENTICAL`。
3. 在空目录重新生成 `-gen-register-info` 的五个文件，逐个 `cmp` 全部一致。

如果你想自己确认某个生成物的内容，不要读整个 154K 的文件，直接抓关键符号：

```console
$ grep -n 'InitMiniRVMCInstrInfo' $BUILD/lib/Target/MiniRV/MiniRVGenInstrInfo.inc
1394:static inline void InitMiniRVMCInstrInfo(MCInstrInfo *II) {
$ grep -n 'return_address_register\|InitMiniRVMCRegisterInfo' $BUILD/lib/Target/MiniRV/MiniRVGenRegisterInfoMCDesc.inc
384:static inline void InitMiniRVMCRegisterInfo(MCRegisterInfo *RI, unsigned RA, unsigned DwarfFlavour = 0, unsigned EHFlavour = 0, unsigned PC = 0) {
$ grep -n 'DecoderTable32' $BUILD/lib/Target/MiniRV/MiniRVGenDisassemblerTables.inc
28:static const uint8_t DecoderTable32[499] = {
```

这三个符号分别对应 MC 层的三个核心对象：`MCInstrInfo`、`MCRegisterInfo`、`MCDisassembler`。下一章会把它们串起来。

## 反直觉与踩雷

1. **错误直觉**：`def MiniRVAsmWriter : AsmWriter { ... }` 定义了，生成器就一定用它。→ **正确认识**：`-gen-asm-writer` 读的是 `Target.AssemblyWriters` 列表，而 `def MiniRV : Target` 没有把 `MiniRVAsmWriter` 放进去，所以实际用的是默认 `DefaultAsmWriter`。两者类名恰好都是 `"InstPrinter"`，所以代码能编过。如果你把 `MiniRVAsmWriter` 的类名改成别的，生成代码仍会按默认名生成，手写 C++ 类就对不上。要么加 `let AssemblyWriters = [MiniRVAsmWriter];`，要么删掉这个未使用的记录。

2. **错误直觉**：`MiniRVInstrFormats.td` 会生成一个 `GenInstrFormats.inc`。→ **正确认识**：没有 `-gen-instr-formats` 这个 backend。`InstrFormats.td` 只是 TableGen 类库，位域通过 `let Inst{...}` 写进每条指令记录，最终进入 `GenInstrInfo.inc` 和 `GenMCCodeEmitter.inc`。MiniRV 的 `Format` 字段甚至没有 C++ 消费者，注释里提到的 `MiniRVBaseInfo.h` 在 MiniRV 目录中不存在。

3. **错误直觉**：`-gen-asm-matcher` 会生成汇编解析器。→ **正确认识**：它只生成匹配表、`convertToMCInst()`、诊断类型和寄存器名匹配。`MiniRVAsmParser` 的 tokenizer、`parseInstruction()`、`isSImm12()` 等全部手写在 `AsmParser/MiniRVAsmParser.cpp`。`AsmParser` 记录只是告诉生成器“匹配函数放进哪个类”。

4. **错误直觉**：`.inc` 应该在源码树里，和 `.td` 放在一起。→ **正确认识**：`.inc` 是构建产物，在 `$BUILD/lib/Target/MiniRV/`（注意这个 build 没有 `$BUILD/llvm/` 这一层）。源码树里只有 `.td` 和 `auto-td/` 语料。清理构建目录会把它们全部删掉，重新 `ninja` 才回来。

5. **错误直觉**：`auto-td` 是 LLVM 的通用能力，可以照搬到上游 target。→ **正确认识**：`llvm_autotd_generate`、`utils/auto-td/auto-td-gen`、YAML 语料都是这棵实验树特有的。上游 target 走手写 `.td`（RISC-V 路径），或者用 LLVM 自带的 TableGen backend。把 `AutoTD.cmake` 带进上游 PR 会被要求拆掉。

6. **错误直觉**：`RemapAllTargetPseudoPointerOperands<GPR>` 只是可选优化。→ **正确认识**：标准伪指令的 `ptr_rc` 占位操作数需要具体寄存器类；不映射时，TableGen/SelectionDAG 无法为这些伪指令确定物理寄存器类。它是“能编过”的必要条件，不是锦上添花。

## 进阶：再往深一层

- **看匹配表怎么变成状态机**。`MiniRVGenAsmMatcher.inc` 的 `MatchInstructionImpl()` 是一棵按 mnemonic 长度和字符分派的树，最后落到 `MatchClass` 和 `convertToMCInst()`。`tablegen_internals` 课程会拆 TableGen 的记录模型；本课只需要知道“生成的是表，不是解析器”。
- **看 DAGISel 的体积从哪来**。`MiniRVGenDAGISel.inc` 有 1412 行，比 AsmMatcher 还大，因为每条 `Pat` 都被编译成一个带 `OPC_*` 操作码的匹配程序。指令记录只有 62 条，但模式可以有很多组合（寄存器/立即数、`x0` 特化、load/store 的 sext/zext 变体）。
- **看覆盖门禁**。`auto-td/config.yaml` 里 `coverage.extensions: [rv_i, rv_m]` 要求每一个 opcode 数据库行都有对应的 YAML 指令；`retained_handwritten: []` 表示没有保留手写记录。`check-autotd` 目标会跑这个门禁和生成器单测。这是“语料驱动”相对“手写”的额外工程成本。
- **看 `-print-records`**。`llvm-tblgen -print-records MiniRV.td` 会打印所有记录（包括继承后的字段），是确认“某个 `let` 到底生效没有”的最快方式。

## 本章重点整理

- MC-only 最小集：RegisterInfo、InstrInfo、AsmWriter、AsmMatcher、Disassembler、SubtargetInfo；出机器码还需要 MCCodeEmitter。
- `InstrFormats` 不是生成器，是位域类库；真正编码在指令记录的 `Inst` 位里。
- `AsmWriter` 负责打印，`AsmMatcher` 负责匹配，`AsmParser` 是手写的；三者不要混为一谈。
- MiniRV 的 62 条指令记录来自 `auto-td/AutoTdInstrInfo.inc`，手写文件只保留操作数类、伪指令和 `Pat`。
- 标准路径是 RISC-V 的 `RVInstR` + `ALU_rr` + `def ADD`；`auto-td` 是实验树特有机制。
- `.inc` 在 `$BUILD/lib/Target/MiniRV/`，可用 `llvm-tblgen` 按 `build.ninja` 的 include 路径逐字节复现。

## 自我检核

- [ ] 不看笔记，说出 `-gen-register-info`、`-gen-instr-info`、`-gen-asm-matcher`、`-gen-asm-writer`、`-gen-disassembler` 各自生成什么、被哪个 C++ 文件包含。
- [ ] 解释为什么 `simm13_lsb0` 需要 `EncoderMethod`，而普通 `simm12` 不需要。
- [ ] 解释 `simm13_lsb0` 与 `simm13_lsb0_bb` 的区别，以及为什么后者没有 `ImmLeaf`。
- [ ] 解释 `def MiniRV : Target` 里 `InstructionSet` 和 `AssemblyParsers` 分别驱动哪个生成器。
- [ ] 解释 `MiniRVAsmWriter` 为什么当前没有被使用，以及怎样让它生效。
- [ ] 说出 `RemapAllTargetPseudoPointerOperands<GPR>` 解决的具体问题。
- [ ] 说出 `auto-td` 与 RISC-V 手写路径的三个差异（输入、生成物、上游可用性）。

## 延伸阅读

- **[TableGen Backends](https://llvm.org/docs/TableGen/BackEnds.html)** — LLVM 官方
  - **读哪里**：`-gen-instr-info`、`-gen-asm-matcher`、`-gen-asm-writer`、`-gen-disassembler`、`-gen-register-info` 各节。
  - **能学到什么**：每个 backend 的输入记录、输出符号和约束；为什么有些 backend 需要额外字段（如 `DecoderMethod`）。
  - **前提**：先能读懂本章的 `MiniRV.td` 和 `MiniRVInstrInfo.td`。

- **[TableGen Programmer's Reference](https://llvm.org/docs/TableGen/ProgRef.html)** — LLVM 官方
  - **读哪里**：`class`、`multiclass`、`defm`、`let`、`field`、`dag` 类型，以及 `!foreach`/`!foldl`。
  - **能学到什么**：`RemapAllTargetPseudoPointerOperands` 这类泛型代码为什么能工作；如何写自己的 `multiclass` 减少重复。
  - **前提**：有本章的寄存器/指令定义经验。

- **[`tablegen_internals` 课程](../tablegen_internals/README.md)** — 本仓库
  - **读哪里**：记录模型、backend 遍历、`RecordKeeper` 与生成器实现。
  - **能学到什么**：`.td` 如何变成内存中的记录、`-gen-*` 如何遍历记录并输出 `.inc`。
  - **前提**：本章的“生成物地图”已经能对上号。

- **[`RISCVInstrInfo.td`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/RISCV/RISCVInstrInfo.td)** — LLVM 上游
  - **读哪里**：`RVInstR`/`ALU_rr` 的定义，以及文件末尾对 `RISCVInstrInfoM.td` 等扩展文件的 `include`。
  - **能学到什么**：成熟 target 如何用手写 TableGen 组织几百条指令；如何按扩展拆分文件。
  - **前提**：知道 R/I/S/B/U/J 格式的基本位域。

→ [Ch 07 MC 层五件套](./07-mc-layer-five-pieces.md)
