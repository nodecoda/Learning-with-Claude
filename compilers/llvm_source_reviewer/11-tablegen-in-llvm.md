# Ch 11 — TableGen 在 LLVM 中的位置：`.td` 如何变成 `.inc`

> **目标**：把 TableGen 放回构建链里看：`.td` 是源文件，`llvm-tblgen` 是编译器，`.inc` 是生成物，C++ 只是最后的消费者。学完你能在 review 一个改 `.td` 的 PR 时，判断它会影响哪几个生成文件、哪些检查点必须补测试，以及为什么「tblgen 跑过了」不等于「行为正确」。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；构建 `/home/ubuntu/llvm/builds/pr-lane`（assertions ON，targets `X86;AArch64;RISCV`）。本章的 `llvm-tblgen` 命令都在这个构建上实跑；X86 的 `.td` 在 `llvm-project` 与构建对应的 `trees/pr-lane` 两个工作树之间无差异（`diff -rq` 的 `.td` 结果为 0），所以行号引用对两者都成立。

## 为什么需要这个？

LLVM 里改 backend 的 PR，十有八九要在 `.td` 里加一行。review 这类 diff 的难点不是语法，而是**生成物和源文件之间的距离**：

- diff 里只有 `def`/`let` 的几行，实际改动却落在二十几个 `.inc` 文件、几万个 enum 值和一张 DAG 匹配表上。
- `llvm-tblgen` 成功退出只证明「记录能解析、字段能展开」，不证明「新指令能被选中」「编码正确」「调度模型没退化」。
- 有些字段漏了会直接编译失败（例如 X86 的 `SchedRW`），有些漏了**什么都不会发生**，直到某个 target 的某个 CPU 上生成错误代码。

本章只讲 reviewer 需要的那一层：构建链、record 展开、生成物怎么对应回 `.td`、以及每个检查点该配什么测试。lexer/parser/`Init` 折叠这些内部机制在 [`tablegen_internals`](../tablegen_internals/README.md) 里，本章不重讲。

## 先建立直觉

TableGen 不是宏，也不是模板；它是一个**小语言 + 一组后端**。`.td` 文件被解析成一组 record，后端遍历 record 生成目标文件：

```
X86.td ──include──▶ X86InstrInfo.td ──include──▶ X86InstrArithmetic.td
   │                                                   │
   │  llvm-tblgen -gen-instr-info                      │  defm ADD / def 32rr
   ▼                                                   ▼
builds/pr-lane/lib/Target/X86/X86GenInstrInfo.inc   （9 MB 的 enum + 描述表）
   │
   │  #include 进 X86InstrInfo.cpp / X86MCTargetDesc.cpp
   ▼
libLLVMX86CodeGen.so ──▶ llc / llvm-mc
```

三段式要分清：

| 阶段 | 代码位置 | 输入 | 输出 |
| --- | --- | --- | --- |
| 前端（lexer/parser/record） | `llvm/lib/TableGen/` | `.td` 文本 | `RecordKeeper` |
| 后端（emitter） | `llvm/utils/TableGen/` | `RecordKeeper` | 文本片段 |
| 消费者 | `llvm/lib/Target/...` | `.inc` | C++ 编译单元 |

`llvm-tblgen` 的 `main` 极薄：`llvm/utils/TableGen/llvm-tblgen.cpp` 只有一行 `return tblgen_main(argc, argv);`，真正的 driver 在 `llvm/lib/TableGen/Main.cpp`（`TableGenMain` 在 241 行起）。每个后端通过静态对象注册自己的 flag，例如 `-gen-instr-info` 注册在 `llvm/utils/TableGen/InstrInfoEmitter.cpp:1485`：

```cpp
static TableGen::Emitter::OptClass<InstrInfoEmitter>
    X("gen-instr-info", "Generate instruction descriptions");
```

这行是理解「一个 `.td` 为什么能生成二十几个 `.inc`」的钥匙：同一个 `RecordKeeper`，不同的 emitter。

## 构建链：CMake 到 ninja 的真实命令

X86 的生成规则写在 `llvm/lib/Target/X86/CMakeLists.txt`。前几行就把「输入是 `X86.td`」钉死：

```cmake
set(LLVM_TARGET_DEFINITIONS X86.td)          # CMakeLists.txt:3

llvm_tablegen(X86GenAsmMatcher.inc -gen-asm-matcher)   # :5
llvm_tablegen(X86GenAsmWriter.inc -gen-asm-writer)     # :6
llvm_tablegen(X86GenDAGISel.inc -gen-dag-isel)         # :9
llvm_tablegen(X86GenInstrInfo.inc -gen-instr-info      # :15
                                  -instr-info-expand-mi-operand-info=0)
llvm_tablegen(X86GenRegisterInfo.inc -gen-register-info) # :19
llvm_tablegen(X86GenSubtargetInfo.inc -gen-subtarget)    # :21
add_public_tablegen_target(X86CommonTableGen)            # :34
```

`llvm_tablegen` 是 `llvm/cmake/modules/TableGen.cmake:163` 的封装；它把输出文件加进 `TABLEGEN_OUTPUT`，`add_public_tablegen_target`（同文件 193）再把这些文件挂成一个 target。

ninja 里能查到完整的命令行（`builds/pr-lane/build.ninja:32332` 起）：

```text
COMMAND = cd .../builds/pr-lane/lib/Target/X86 && .../bin/llvm-tblgen
  -gen-instr-info -instr-info-expand-mi-operand-info=0
  -I.../llvm/lib/Target/X86 -I.../builds/pr-lane/include -I.../llvm/include
  -I .../llvm/lib/Target .../llvm/lib/Target/X86/X86.td
  --write-if-changed -o X86GenInstrInfo.inc -d X86GenInstrInfo.inc.d
```

三个 `-I` 分别指向：target 自己的目录（相对 include）、build 的生成头文件目录（`vt_gen`、intrinsics 的 `.inc` 在这里）、源码的 `llvm/include`。少任何一个都会得到 include 错误，下一节有实测。

生成物落在两类位置：

```bash
find /home/ubuntu/llvm/builds/pr-lane/include/llvm -name '*.inc' | wc -l
# 16
find /home/ubuntu/llvm/builds/pr-lane/lib/Target/X86 -name '*.inc' | wc -l
# 25
```

前者是跨 target 共享的（`IntrinsicEnums.inc`、`IntrinsicImpl.inc`、`Attributes.inc`、`GenVT.inc`、`RuntimeLibcalls.inc`），后者是 X86 专属的（`X86GenInstrInfo.inc` 9.0 MB、`X86GenDAGISel.inc` 31 MB）。`llvm/include/llvm/IR/Intrinsics.td` 的生成物不在 `lib/Target` 下，这就是为什么改 intrinsic 会影响所有 target 的重编译。

## 核心 `.td` 地图

review 要记住的是「哪个文件定义什么」：

| 文件 | 行数 | 定义什么 | 改动的影响面 |
| --- | --- | --- | --- |
| `llvm/include/llvm/Target/Target.td` | 2090 | 通用 `Instruction`、`InstrInfo`、`Register`、`SchedWrite` 等基类 | 所有 target |
| `llvm/include/llvm/IR/Intrinsics.td` | 2991 | 通用 intrinsic 与各 target 的 include | 所有 target + clang |
| `llvm/include/llvm/IR/Attributes.td` | 515 | 属性枚举（`NoUnwind` 等） | 所有 IR |
| `llvm/lib/Target/X86/X86.td` | 2354 | X86 的 `Target`、`ProcessorModel`、include 顺序 | 仅 X86 |
| `llvm/lib/Target/X86/X86InstrFormats.td` | 314 | X86 的 `X86Inst`、`Format`、`ImmType` | 仅 X86 编码 |
| `llvm/lib/Target/X86/X86InstrArithmetic.td` | 1526 | 算术指令的 `multiclass` 与 `defm` | 仅 X86 指令集 |

任务清单里提到的 `llvm/include/llvm/Target/TargetInstrFormats.td` 在 `7c7a9072f736` 上**不存在**：

```bash
ls llvm/include/llvm/Target/TargetInstrFormats.td
# ls: cannot access '...TargetInstrFormats.td': No such file or directory
find llvm -name '*InstrFormats*' | head -3
# ./lib/Target/BPF/BPFInstrFormats.td
# ./lib/Target/XCore/XCoreInstrFormats.td
```

通用部分已经并进 `Target.td`：`class InstructionEncoding` 在 545 行，`class Instruction : InstructionEncoding` 在 608 行。各 target 自己的格式类留在自己的目录，例如 `X86InstrFormats.td:206` 的 `class X86Inst`。读旧文章时看到 `TargetInstrFormats.td`，先确认它讲的是哪个版本。

## 语法最小闭环：`class` / `def` / `multiclass` / `defm` / `let`

用一个不依赖 LLVM 的玩具文件把五个关键字跑一遍：

```tablegen
class Op<string mnemonic, bits<8> opcode> {
  string Mnemonic = mnemonic;
  bits<8> Opcode = opcode;
  bit isCommutable = 0;
  string AsmString = mnemonic;
}

def NOP : Op<"nop", 0x90>;

multiclass BinOp<string mnemonic, bits<8> base> {
  def _rr : Op<mnemonic, base>;
  def _rm : Op<mnemonic, !add(base, 1)>;
  let isCommutable = 1 in
    def _ri : Op<mnemonic, !add(base, 2)>;
}

defm ADD : BinOp<"add", 0x00>;
```

`llvm-tblgen --print-records td/toy.td` 的真实输出（为篇幅把 `ADD_rm`/`ADD_rr`/`NOP` 压成单行，字段值照抄）：

```text
------------- Defs -----------------
def ADD_ri {	// Op
  string Mnemonic = "add";
  bits<8> Opcode = { 0, 0, 0, 0, 0, 0, 1, 0 };
  bit isCommutable = 1;
  string AsmString = "add";
}
def ADD_rm { ... Opcode = { 0, 0, 0, 0, 0, 0, 0, 1 }; bit isCommutable = 0; ... }
def ADD_rr { ... Opcode = { 0, 0, 0, 0, 0, 0, 0, 0 }; bit isCommutable = 0; ... }
def NOP { ... Opcode = { 1, 0, 0, 1, 0, 0, 0, 0 }; ... }
```

三件事在这里可见：

1. `defm ADD` 的前缀 `ADD_` 和 multiclass 里的 `_rr`/`_rm`/`_ri` 拼成三个 record 名。
2. `let isCommutable = 1 in def _ri` 只覆盖那一个 record，另外两个保持类里的默认值 0。
3. 输出按 record 名排序（`ADD_ri`、`ADD_rm`、`ADD_rr`、`NOP`），**不是**定义顺序（`NOP` 定义在最前）。`--print-records` 是给你看展开结果的，不要拿它推断文件里的书写顺序。

失败路径同样值得看。`let` 引用一个不存在的字段：

```tablegen
class Op<string mnemonic> { string Mnemonic = mnemonic; }
let isPseudo = 1 in
def FAKE : Op<"fake">;
```

```text
td/bad.td:6:5: error: Value 'isPseudo' unknown!
let isPseudo = 1 in
    ^
```

两个 `defm` 实例化出同名 record：

```tablegen
multiclass Dup<string m> { def X : Op<m>; }
defm A : Dup<"a">;
defm A : Dup<"b">;
```

```text
td/bad2.td:2:32: error: def already exists: AX
multiclass Dup<string m> { def X : Op<m>; }
                               ^
td/bad2.td:4:10: note: instantiated from multiclass
defm A : Dup<"b">;
```

第二条错误里的 `AX` 是 `defm A` + `def X` 拼出来的名字；它把「后缀拼接」的规则暴露得很清楚，也解释了为什么 X86 里同一 multiclass 的多个 `defm` 不能重名。

## `--print-records`：把 intrinsic 展开看

真实规模的文件用 `Intrinsics.td`。它需要 `-I` 才能找到 include：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
S=/home/ubuntu/llvm/llvm-project
$B/llvm-tblgen --print-records -I $S/llvm/include -I $S/llvm/lib/Target/X86 \
    $S/llvm/include/llvm/IR/Intrinsics.td > td/intrinsics.records.txt
```

实测输出 349,372 行。`int_smax` 的 record（第 303941 行起）：

```text
def int_smax {	// SDPatternOperator Intrinsic DefaultAttrsIntrinsic
  list<SDNodeProperty> Properties = [];
  string LLVMName = "";
  string TargetPrefix = "";
  string TargetFeatures = "";
  list<LLVMType> RetTypes = [llvm_anyint_ty];
  list<LLVMType> ParamTypes = [anonymous_40, anonymous_40];
  list<IntrinsicProperty> IntrProperties = [IntrNoMem, IntrSpeculatable, IntrNoCreateUndefOrPoison];
  bit DisableDefaultAttributes = 0;
  TypeInfoGen TypeInfo = anonymous_184;
}
```

这个 dump 把 class 继承链上的所有字段都摊平了：`DefaultAttrsIntrinsic` 带来的 `IntrProperties`、`Intrinsic` 带来的 `RetTypes`/`ParamTypes`，在 `.td` 里只写了两三行。review 时如果怀疑「某个属性到底有没有生效」，`--print-records` + `rg 'def int_xxx'` 比在 include 链里翻文件快。

漏 `-I` 的错误：

```text
.../Intrinsics.td:13:9: error: could not find include file 'llvm/CodeGen/ValueTypes.td'
include "llvm/CodeGen/ValueTypes.td"
        ^
```

注意它报的是**被 include 的文件**，不是命令行。看到这个错误先查 `-I`，不要怀疑 `.td` 内容。

## 从 record 到生成物：X86 的一条可追踪链

挑一条真实指令，从 `.td` 追到生成文件。`ADD32rr` 的来源是三层：

```tablegen
// X86InstrArithmetic.td:1100
defm ADD : ArithBinOp_RF<0x01, 0x03, 0x05, "add", MRM0r, MRM0m,
                         X86add_flag, add, 1, 1, 1>;
```

`defm ADD` 展开 `multiclass ArithBinOp_RF`（同文件 638），其中一条是：

```tablegen
// X86InstrArithmetic.td:648
def 32rr : BinOpRR_RF<BaseOpc, mnemonic, Xi32, opnodeflag>, OpSize32;
```

`BinOpRR_RF` 继承 `BinOpRR`（`X86InstrUtils.td:997` / 974），`BinOpRR` 带 `Sched<[WriteALU]>`（同文件 976），所以这条指令自动获得 `SchedRW` 和 `(set GR32:$dst, EFLAGS, (add ...))` 的 pattern。

生成物里对应三处：

```text
// X86GenInstrInfo.inc:660
    ADD32rr                               = 645, // X86InstrArithmetic.td:648

// X86GenAsmMatcher.inc:12394
  { 100 /* addl */, X86::ADD32rr, Convert__Reg1_1__Tie0_2_2__Reg1_0,
    AMFBS_None, { MCK_GR32, MCK_GR32 }, },

// X86GenInstrInfo.inc:22803
    WriteALU	= 1,
```

三件事：

1. enum 值的注释直接写了源 `.td` 和行号。review 时拿生成文件反查 `.td` 是最快的路径。
2. `AsmMatcher` 的表项把 `AsmString`、目标指令、operand 转换器、feature 掩码绑在一起；`AMFBS_None` 和 `AMFBS_In64BitMode` 是谓词的落点。
3. `WriteALU = 1` 是 `SchedRW` 落成的调度 class 编号；`llvm-mca` 读的就是这张表。

### 生成是可复现的

用同一组 `-I` 重新生成 `X86GenInstrInfo.inc`，和 build 里的文件逐字节比较：

```bash
$B/llvm-tblgen -gen-instr-info -instr-info-expand-mi-operand-info=0 \
  -I$S/llvm/lib/Target/X86 -I/home/ubuntu/llvm/builds/pr-lane/include \
  -I$S/llvm/include -I$S/llvm/lib/Target \
  $S/llvm/lib/Target/X86/X86.td -o /tmp/X86GenInstrInfo.regen.inc
cmp /tmp/X86GenInstrInfo.regen.inc \
  /home/ubuntu/llvm/builds/pr-lane/lib/Target/X86/X86GenInstrInfo.inc
```

实测：

```text
elapsed=4.76s maxrss=449196KB
IDENTICAL
```

9 MB 输出、4.8 秒、450 MB 峰值内存。这个数字决定了「改一行 `.td` 的迭代成本」：单个 target 的 instr-info 重生成是秒级，但后续 C++ 重编译不是。review 里看到「顺手重构 X86InstrArithmetic.td」的大 diff，要意识到它会让所有下游 `.inc` 变脏。

## Reviewer 检查点

改 `.td` 的 PR，按下面六项过一遍。每项都给出「漏了会怎样」和「该看哪类测试」。

| 检查点 | 具体看什么 | 漏了会怎样 | 测试 |
| --- | --- | --- | --- |
| opcode 编号 | 新增/重命名 `def` 后 enum 平移 | 生成物 diff 巨大；依赖数字的代码/脚本错位 | 不需要专门测试，但要确认 diff 只来自预期改动 |
| `Predicates` | target feature / 模式 gate | 在不支持的 CPU 上生成该指令，assembler 拒绝或 CPU 报 illegal instruction | `llvm/test/CodeGen/X86/*.ll` 带 `-mcpu`/`-mattr` |
| `SchedRW` | 调度 class 是否覆盖新指令 | 性能静默退化；或 X86 直接 build error | `llvm/test/tools/llvm-mca/X86/*` |
| `AsmString` | operand 顺序、`$src` 名、AT&T/Intel 两套写法 | 汇编 round-trip 失败，或打印出无法重新汇编的文本 | `llvm/test/MC/X86/*.s` 的 `-show-encoding` |
| 编码域 | `Inst`、`Opcode`、`Form`、`OpSize`、REX2/EVEX 位 | 字节错误、长度错误、解码歧义 | `llvm/test/MC/X86/*.s` + `llvm-mc -disassemble` |
| 指令属性 | `isCodeGenOnly`、`hasSideEffects`、`mayLoad`、`mayStore` | 优化把有副作用的指令删掉或 CSE | `llvm/test/CodeGen/X86/*.ll` 的 `-verify-machineinstrs` |

### 为什么 opcode 编号会平移

`CodeGenTarget::getInstructions()` 的注释（`llvm/utils/TableGen/Common/CodeGenTarget.h:213`）规定了 enum 顺序：

```text
- fixed / generic instructions as declared in TargetOpcodes.def, in order;
- pseudo instructions in lexicographical order sorted by name;
- other instructions in lexicographical order sorted by name.
```

也就是说编号是**按名字字典序**排出来的，不是按 `.td` 定义顺序。实测：在 X86 的副本里加一条 `ADD32rr_ZZ`（名字排在 `ADD32rr_REV` 之后），重新生成：

```text
ADD32rr                               = 645, // 不变
ADD32rr_ZZ                            = 655, // 新增
ADD64i32                              = 656, // 原 655
ADD8rr                                = 720, // 原 719
```

`INSTRUCTION_LIST_END = 22767`：新指令占据 655，原来 655 及之后的 22,112 个 opcode 各 +1。这不是 ABI 问题（MIR、汇编、C++ 都按名字引用），但它意味着：**生成物 diff 大是正常的**，review 时不要被行数吓到；反过来，如果有人手工编辑 `.inc` 让 diff 变小，那是错误做法，下次 build 会被覆盖。

### 漏 `SchedRW` 会在 X86 上直接失败

同一个实验里，先不写 `SchedRW`：

```text
td/x86copy/X86InstrArithmetic.td:1529:5: error: No schedule information for
instruction 'ADD32rr_ZZ' in SchedMachineModel 'C864GM4Model'
...
Incomplete schedule models found.
- Instructions should usually have Sched<[...]> as a superclass, you may
  temporarily use an empty list.
error: Incomplete schedule model
```

X86 的部分 schedule model 设了 `CompleteModel = 1`（例如 `X86ScheduleZnver3.td:61`、`X86ScheduleZnver4.td:59`），这些模型要求每条指令都有调度信息，错误里列出的正是它们。补上 `let SchedRW = [WriteALU] in` 后生成成功。这条错误的实用价值：**只要改 X86 指令，`llvm-tblgen` 就会替你检查 SchedRW**；但其他 target 未必这么严，不能把「tblgen 过了」当成「调度信息齐全」。

### 漏 `hasSideEffects` 会静默改变优化行为

同一个实验里，最小定义的 `ADD32rr_ZZ` 生成出来带 `UnmodeledSideEffects`：

```text
{ 655, 3, 1, 0, 1, 0, 0, X86OpInfoBase + 238, 0,
  0|(1ULL<<MCID::UnmodeledSideEffects), 0x80000028ULL }, // ADD32rr_ZZ  （节选）
```

原因是 `Target.td:1194` 的注释：`mayLoad`/`mayStore`/`hasSideEffects` 默认未设置，TableGen 会从 pattern 推断；推断不出来时，`guessInstructionProperties = true`（`Target.td:1207`）会猜一个**安全值**，对 `hasSideEffects` 就是 true。安全值的方向是「不敢优化」：这条指令不会被删、不会被 CSE、不会被 hoist。功能测试全绿，性能退化没人发现。所以新指令要么给 pattern，要么显式写 `hasSideEffects = 0`，并在 PR 里说明。

### 端到端证据：从 `.ll` 到字节

`.td` 改对没改对，最后要看 `llc` 和 `llvm-mc`：

```bash
$B/llc -mtriple=x86_64-unknown-linux-gnu -O2 add.ll -o -
printf 'addl %%esi, %%edi\n' | $B/llvm-mc -triple=x86_64-unknown-linux-gnu -show-encoding
```

实测：

```text
	leal	(%rdi,%rsi), %eax
	retq
	addl	%esi, %edi                      # encoding: [0x01,0xf7]
```

`leal` 出现的原因是 x86 的 `add` 是二地址指令：后端把 `%a + %b` 折进 LEA 的寻址模式，省掉一次 copy；`addl %esi, %edi` 的编码 `0x01 0xf7` 里，`0x01` 是 `ADD r/m32, r32` 的 opcode，`0xf7` 是 ModRM（mod=11、reg=110=ESI、r/m=111=EDI）。这两个字节来自 `X86InstrArithmetic.td:1100` 的 `0x01` 和 `AsmString` 的 operand 顺序。如果 PR 改的是 `ADD` 的 `AsmString` 或编码域，这条 `llvm-mc` 命令就是最小验证。

## 与 `tablegen_internals` 的分工

两门课看的是同一个工具的不同层：

| 问题 | 去哪门课 |
| --- | --- |
| `TGParser` 怎么解析 `multiclass`、`Init` 怎么 fold | `tablegen_internals` Ch 3–9 |
| `Record`/`RecordKeeper` 的内存布局、`defm` 展开算法 | `tablegen_internals` Ch 4、8 |
| 怎么加一个 backend、backend 注册机制 | `tablegen_internals` Ch 13–17 |
| 改 `.td` 会影响哪些 `.inc`、review 该看什么 | 本章 |
| 为什么 `.td` 改动要配 CodeGen/MC 测试 | 本章 |

本章刻意不展开 lexer/parser：reviewer 不需要知道 token 怎么切，需要的是 record 展开成什么、生成物怎么反查、哪类错误在 tblgen 阶段暴露。

## 踩雷集锦

1. **把 `llvm-tblgen` 成功当成行为正确**。错误直觉：「生成成功就说明指令定义没问题。」正确认识：tblgen 只验证解析、字段类型和（X86 的）调度完整性；isel 是否选得中、编码是否正确、汇编能否 round-trip，都要 `llc`/`llvm-mc` 的 lit 测试。
2. **手工编辑 `.inc` 让 diff 变小**。错误直觉：「生成物太吵，我改一下让 review 干净。」正确认识：`.inc` 是构建产物，下次 `ninja` 会被覆盖；review 应该看 `.td` 和生成物的**对应关系**，而不是要求生成物 diff 小。
3. **以为 opcode 编号按定义顺序**。错误直觉：「我把新 `def` 加在文件末尾，编号就加在末尾。」正确认识：enum 按名字字典序排（`CodeGenTarget.h:213`），插在中间会让后面全部 +1；实测一条指令平移 22,112 个编号。
4. **新指令只写 `AsmString`，不写 `Predicates`**。错误直觉：「汇编能打印就行。」正确认识：没有 feature gate，isel 会在不支持该指令的 CPU 上选中它；测试必须带 `-mcpu`/`-mattr` 才能暴露。
5. **忽略 `hasSideEffects`/`mayLoad`/`mayStore` 的推断结果**。错误直觉：「没写就是 false。」正确认识：默认未设置时 TableGen 会推断，推断不出就猜安全值（`Target.td:1207`），结果是 `UnmodeledSideEffects`，优化被过度保守。
6. **在多处 `let` 覆盖同一字段**。错误直觉：「后写的 `let` 会叠加。」正确认识：同名字段是覆盖，不是合并；`Predicates`/`SchedRW` 这类 list 字段最终只有最后一个 `let` 生效，只有 `--print-records` 能确认。

## 本章重点整理

- `.td` → `llvm-tblgen` → `.inc` 是一条构建链：CMake 的 `set(LLVM_TARGET_DEFINITIONS)` + `llvm_tablegen(...)` 决定输入和生成器，ninja 里的命令行决定 include 路径。
- 生成物分两类：`builds/pr-lane/include/llvm/**`（跨 target）和 `builds/pr-lane/lib/Target/<T>/**`（target 专属）；X86 有 25 个 `.inc`。
- `TargetInstrFormats.td` 在 `7c7a9072f736` 已不存在，通用格式类在 `Target.td:545`/`608`，X86 专属在 `X86InstrFormats.td`。
- `class`/`def`/`multiclass`/`defm`/`let` 的展开结果只能用 `--print-records` 确认；dump 按名字排序，不是定义顺序。
- opcode enum 按「generic → pseudo 字典序 → 其他字典序」生成（`CodeGenTarget.h:213`），新增 `def` 会平移后面的编号。
- X86 部分模型的 `CompleteModel` 让漏 `SchedRW` 变成硬错误；`hasSideEffects` 漏写则静默变成 `UnmodeledSideEffects`，方向是过度保守。
- `.td` 改动必须配 lit：isel 看 `llvm/test/CodeGen/X86/*.ll`，编码看 `llvm/test/MC/X86/*.s` 的 `-show-encoding`，调度看 `llvm/test/tools/llvm-mca/X86/*`。

## 自我检核

- [ ] 能在不看 CMake 的情况下说出 `X86GenInstrInfo.inc` 的输入文件、生成 flag、输出目录
- [ ] 能解释 `-I` 三个路径各自解决什么问题，以及漏掉 build include 会报什么错
- [ ] 能写出 `class` + `multiclass` + `defm` 的最小例子，并预测 record 名和字段值
- [ ] 能说明 `--print-records` 的输出顺序由什么决定，为什么不能用它推断定义顺序
- [ ] 能从一个生成物里的 `// X86InstrArithmetic.td:648` 注释反查到 `.td` 的那一行
- [ ] 能解释为什么新增一条 X86 指令会让后面两万多个 opcode 编号 +1
- [ ] 能说出漏 `SchedRW` 在 X86 上是硬错误、在别的 target 上可能不是，以及为什么
- [ ] 能说出 `hasSideEffects` 未设置时会发生什么，以及为什么这是「静默的性能问题」
- [ ] 能给出 `.td` 改动要配的三类 lit 测试，并各举一个目录
- [ ] 能画出 `.td` → `llvm-tblgen` → `.inc` → `llc`/`llvm-mc` 的完整链路

## 延伸阅读

- **[TableGen Overview（LLVM 官方）](https://llvm.org/docs/TableGen/index.html)**
  - **读哪里**：Introduction 与 “Source Files” 两节
  - **能学到什么**：TableGen 的定位（domain-specific record 描述）和前后端分工，补上本章「三段式」的官方措辞
  - **前提知识**：读过任意一个 backend 的 `.td`；不需要 C++ 内部实现
- **[TableGen Programmer's Reference](https://llvm.org/docs/TableGen/ProgRef.html)**
  - **读哪里**：`class`（640 起）、`def`（799 起）、`let`（962 起）、`multiclass`（1021 起）、`defm`（1070 起）
  - **能学到什么**：本章玩具例子背后的完整语义，尤其是 `let` 的覆盖规则和 multiclass 的参数化
  - **前提知识**：能读本章的 `toy.td`；读到 `!add`/`!eq` 时对照 bang operators 一节
- **[TableGen BackEnds](https://llvm.org/docs/TableGen/BackEnds.html)**
  - **读哪里**：LLVM BackEnds 一节里与 `-gen-instr-info`、`-gen-dag-isel`、`-gen-asm-matcher` 相关的段落
  - **能学到什么**：每个 `.inc` 的职责边界，以及为什么同一个 record 需要多个 backend
  - **前提知识**：知道 `RecordKeeper` 是什么；配合 `utils/TableGen/InstrInfoEmitter.cpp` 读
- **[`tablegen_internals` 课程](../tablegen_internals/README.md)**
  - **读哪里**：Ch 1（三段式架构）、Ch 8（multiclass 展开）、Ch 17（InstrInfoEmitter walkthrough）
  - **能学到什么**：本章跳过的 lexer/parser/`Init` 折叠与 backend 内部实现，读完能自己加 backend
  - **前提知识**：C++17、模板、能读几千行 C++；先做完本章的 `--print-records` 实验

→ [Ch 12 — 库分层、CMake 与 include 规则：为什么这个依赖不能加](./12-cmake-layering-and-build.md)
