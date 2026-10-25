# Ch 05 生成物解剖：六个 generator 与一条 ADD 的旅行

> 目标：把 `llvm-tblgen` 的输出从“一堆不能读的 .inc”变成可检索的证据链。读完本章，你能用六条命令重建 RISC-V 的全部 TableGen 产物，能在一分钟内找到 `ADD` 的枚举、名字、matcher、汇编匹配项和编码分支，并能用 `.td` 副本做一次可回滚的“改一行、重生成、diff”实验。

> 环境：LLVM 23.1.4 预编译版，Windows x86_64；源码是同版本 sparse checkout。生成物写到 `.scratch/riscv-tblgen-ev/`，不写源码树。本文所有大小、行数、行号都来自本机 2026-10-25 的重跑。

## 为什么

`.td` 是源文件，`.inc` 是生成物。后端开发者每天改的是 `.td`，但编译器真正编译的是 `.inc`。这两者之间隔着一个 TableGen backend：同一个 `def ADD` 会被不同 backend 解释成完全不同的东西。

- 对 `-gen-instr-info`，`ADD` 是一个枚举值 `13725` 和一条指令描述。
- 对 `-gen-asm-matcher`，`ADD` 是 mnemonic `"add"` 和一条操作数匹配规则。
- 对 `-gen-disassembler`，`ADD` 是 32 位位模式的一条解码分支。
- 对 `-gen-emitter`，`ADD` 是一个固定位 `0x33` 加上三个寄存器字段的移位拼接。
- 对 `-gen-subtarget`，`ADD` 可能出现在特性/CPU 的可用指令集合里。
- 对 `-gen-dag-isel`，`ADD` 是 `MatcherTable` 里的一个 `TARGET_VAL(RISCV::ADD)` 动作。

同一行 `def ADD`，六种产物，六个不同的检索入口。理解这一点，才能回答“我改了 `.td`，为什么 llc 没变”“为什么生成物 diff 这么大”“为什么不能直接改 `.inc`”。

生成物的体量也会误导人。`-gen-dag-isel` 在本机产出 `60,063,284` 字节、`667,685` 行；`-gen-subtarget` 产出 `25,838,745` 字节、`177,991` 行。把它们当普通源码读，会被噪声淹没。正确做法是先知道每个文件回答什么问题，再用锚点检索。

本章的路线是：先跑六个 generator，建立尺寸基线；再按 `ADD` 追踪枚举、名字表、matcher、汇编匹配和编码；最后用 `.td` 副本加一条 `addi.imm`，观察一次真实 diff。

## 主线

### 5.1 六个 generator：命令与真实大小

在仓库根目录执行下面的命令。`$bin`、`$inc`、`$td` 与第 04 章相同；输出目录先建好。

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$out = ".scratch/riscv-tblgen-ev"
New-Item -ItemType Directory -Force -Path $out | Out-Null

$jobs = @(
  @('-gen-instr-info',   'RISCVGenInstrInfo.inc'),
  @('-gen-asm-matcher',  'RISCVGenAsmMatcher.inc'),
  @('-gen-disassembler', 'RISCVGenDisassemblerTables.inc'),
  @('-gen-emitter',      'RISCVGenMCCodeEmitter.inc'),
  @('-gen-subtarget',    'RISCVGenSubtargetInfo.inc'),
  @('-gen-dag-isel',     'RISCVGenDAGISel.inc')
)
foreach ($j in $jobs) {
  & "$bin/llvm-tblgen.exe" -I $inc -I $td $j[0] "$td/RISCV.td" -o (Join-Path $out $j[1])
  if ($LASTEXITCODE -ne 0) { throw "tblgen failed: $($j[0])" }
  $f = Get-Item (Join-Path $out $j[1])
  '{0,-22} {1,12:N0} bytes {2,10:N0} lines' -f $j[0], $f.Length, ((Get-Content -LiteralPath $f.FullName).Count)
}
```

本机真实输出：

| generator | 产物 | 字节数 | 行数 |
| --- | --- | ---: | ---: |
| `-gen-instr-info` | `RISCVGenInstrInfo.inc` | 6,881,397 | 80,026 |
| `-gen-asm-matcher` | `RISCVGenAsmMatcher.inc` | 1,097,394 | 13,623 |
| `-gen-disassembler` | `RISCVGenDisassemblerTables.inc` | 1,485,949 | 20,926 |
| `-gen-emitter` | `RISCVGenMCCodeEmitter.inc` | 450,182 | 15,812 |
| `-gen-subtarget` | `RISCVGenSubtargetInfo.inc` | 25,838,745 | 177,991 |
| `-gen-dag-isel` | `RISCVGenDAGISel.inc` | 60,063,284 | 667,685 |

合计 `95,816,951` 字节，约 95.8 MB。六个产物里，DAGISel 和 Subtarget 占了 85.9 MB，剩下四个加起来约 9.9 MB。这个分布和职责一致：选择器和 subtarget 特性表要覆盖大量组合，指令信息、汇编器、反汇编器、编码器相对紧凑。

每个 generator 回答的问题不同：

| generator | 回答的问题 | 典型检索 |
| --- | --- | --- |
| `-gen-instr-info` | 这条指令的枚举号、名字、属性是什么 | `ADD =`、`RISCVInstrNameData` |
| `-gen-asm-matcher` | 汇编文本如何匹配到 opcode 和操作数 | `MatchTable0`、`addi.imm` |
| `-gen-disassembler` | 32/16 位位模式如何解码成 MCInst | `decodeInstruction`、`DecodeTable` |
| `-gen-emitter` | MCInst 如何变成字节 | `InstBits`、`getBinaryCodeForInstr` |
| `-gen-subtarget` | 某 CPU/特性组合下哪些指令可用 | `FeatureBitset`、`MCSubtargetInfo` |
| `-gen-dag-isel` | DAG 节点如何匹配到指令 | `MatcherTable`、`RISCV::ADD` |

一次跑六个 generator 大约 80 秒（本机耗时：instr-info 27.5s、asm-matcher 4.5s、disassembler 5.9s、emitter 5.9s、subtarget 9.9s、dag-isel 28.4s）。这个时间也说明为什么完整重建 llc 不适合反复做；日常改 `.td` 时，先跑对应 generator 做快速验证。

### 5.2 `GET_INSTRINFO_ENUM`：枚举、编号与注释

`RISCVGenInstrInfo.inc` 不是一个普通头文件，而是一个“多模式包含”文件。开头是：

```cpp
#ifdef GET_INSTRINFO_ENUM
#undef GET_INSTRINFO_ENUM

namespace llvm::RISCV {

  enum {
    PHI                                        = 0, // Target.td:1327
    INLINEASM                                  = 1, // Target.td:1333
    ...
```

同一个文件里还有 `GET_INSTRINFO_MC_DESC`、`GET_INSTRINFO_CTOR_DTOR` 等区段，由不同的宏开关选择。你在 `RISCVInstrInfo.cpp` 里看到的 `#define GET_INSTRINFO_ENUM` + `#include "RISCVGenInstrInfo.inc"` 就是在打开枚举区段。

`ADD` 的真实行：

```cpp
ADD                                        = 13725, // RISCVInstrInfo.td:818
ADDI                                       = 13727, // RISCVInstrInfo.td:797
```

`ADDIW` 是 `13728`，`ADDW` 是 `13729`，`ADD_UW` 是 `13730`。注释里的 `RISCVInstrInfo.td:818` 是定义位置，不是生成位置。这个注释极有价值：你在 `.inc` 里看到任何一条指令，都能直接跳回 `.td` 的那一行。

编号不是稳定的 ABI。TableGen 按记录集合分配枚举，插入一条新指令会让它后面的编号整体位移。第 5.6 节会实跑：在 `ADDI` 后插入 `ADDI_IMM`，它拿到 `13729`，原来的 `ADDW` 从 `13729` 变成 `13730`，`ADD_UW` 从 `13730` 变成 `13731`。所以不要在任何外部接口里硬编码 `13725` 这种数字；它是本次构建的内部编号。

`InitMCInstrInfo` 的第六个参数（`NO`，`NumOpcodes`）给出了指令总数：

```cpp
II->InitMCInstrInfo(RISCVDescs.Insts, RISCVInstrNameIndices, RISCVInstrNameData,
                    nullptr, nullptr, 16883, nullptr, 0);
```

本机这个值是 `16883`。它包含真实指令、伪指令、压缩指令和供应商扩展指令，不是“RISC-V 基础 ISA 的指令数”。看到这个数字时不要拿它和 ISA 手册的指令条数对比。

检索命令：

```powershell
Select-String -LiteralPath "$out\RISCVGenInstrInfo.inc" -Pattern '^\s*ADD\s+=' 
Select-String -LiteralPath "$out\RISCVGenInstrInfo.inc" -Pattern '^\s*ADDI(W|_UW|_IMM)?\s+='
```

第一条只命中 `ADD = 13725`，不会误中 `ADDI`、`ADDW`、`ADD_UW`。这就是锚点 `^\s*ADD\s+=` 的作用；如果只写 `ADD`，会得到几百条。

### 5.3 `InstrNameData` / `InstrNameIndices`：名字表

指令名不是以 `const char *Names[]` 的形式存的，而是压缩成一个字符块加一个索引数组：

```cpp
extern const char RISCVInstrNameData[] = {
  /* 0 */ "PMUL_H_B00\000"
  /* 11 */ "PMULU_H_B00\000"
  ...
};

extern const unsigned RISCVInstrNameIndices[] = {
    180455U, 355374U, 358439U, ...
};
```

`RISCVInstrNameIndices[Opcode]` 给出该指令名在 `RISCVInstrNameData` 里的偏移。`RISCVInstrNameData` 里用 `\000` 分隔字符串，偏移是字节偏移，不是数组下标。这样做的原因是节省空间和重定位：一个 `const char *[]` 需要为每个指针生成一个重定位项，而一个大字符块 + 一个 `unsigned[]` 更紧凑。

`MCInstrInfo::getName(Opcode)` 最终就是拿 `InstrNameIndices` 和 `InstrNameData` 拼出 `StringRef`。你在错误信息里看到 `Attempting to emit` 后面的指令名，也是从这张表来的。

检索名字：

```powershell
Select-String -LiteralPath "$out\RISCVGenInstrInfo.inc" -Pattern '"ADD\\000"|"ADDI\\000"|"ADDI_IMM\\000"'
```

注意 PowerShell 的 `Select-String` 用正则，`\000` 在文件里是字面反斜杠加三个零，所以模式里要写 `\\000`。如果只写 `ADD`，会命中 `ADDW`、`ADD_UW`、`PMUL...` 等大量名字。

名字表也解释了为什么“改 mnemonic”不一定改变枚举。把 `"add"` 改成 `"addx"`，枚举名仍是 `ADD`，编号也不变，但 `InstrNameData` 里的字符串和 `AsmString` 会变，`AsmMatcher` 也会变。判断影响面要看具体 generator，不能只看枚举。

### 5.4 DAGISel matcher：58 MB 里到底是什么

`RISCVGenDAGISel.inc` 的核心是 `MatcherTable`。文件开头：

```cpp
void DAGISEL_CLASS_COLONCOLON SelectCode(SDNode *N)
{
  static const uint8_t MatcherTable[] = {
/*      0*/ OPC_SwitchOpcode /*449 cases */, 57|128,83/*10681*/, TARGET_VAL(ISD::OR), // ->10686
/*      5*/  OPC_Scope /*26 children */, 38|128,4/*550*/, // ->558
/*      8*/   OPC_MoveChild0,
/*      9*/   OPC_SwitchOpcode /*3 cases */, ...
```

这是一段字节码，不是 C++ 控制流。每个 `OPC_*` 是一个字节操作，后面的字节是参数。`TARGET_VAL(ISD::OR)` 把目标相关值拆成两个字节。注释里的 `->10686` 是跳转偏移，`/*449 cases*/` 是 switch 的分支数。

`ADD` 的真实匹配段在偏移 `25750`：

```cpp
/*  25749*/     OPC_CheckTypeByHwMode0/*{(*:i32),(m1:i64)}*/,
/*  25750*/     OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::ADD), 0,
                1/*#VTs*/, /*{(*:i32),(m1:i64)}*/0, 2/*#Ops*/, /*OperandList*/14,
             // Src: (add:{ *:[i32] m1:[i64] } GPR:{ *:[i32] m1:[i64] }:$rs1, GPR:{ *:[i32] m1:[i64] }:$rs2) - Complexity = 3
             // Dst: (ADD:{ *:[i32] m1:[i64] } GPR:{ *:[i32] m1:[i64] }:$rs1, GPR:{ *:[i32] m1:[i64] }:$rs2)
```

这段字节码的含义是：先检查节点类型匹配 `i32` 或 `i64`（由 HwMode 选择），然后生成 `RISCV::ADD`，带两个操作数。`Complexity = 3` 是 TableGen 给这条规则的复杂度评分。`Src`/`Dst` 注释是 backend 自动生成的，不是人写的；它们是你调试“为什么没选上”的第一手材料。

紧挨在 `ADD` 前面的是 `ADDW`：

```cpp
/*  25739*/      OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::ADDW), 0,
                 // Src: (add:{ *:[] m1:[i64] } GPR ... )<<P:Predicate_anonymous_15302>> - Complexity = 4
                 // Dst: (ADDW ...)
/*  25747*/     0, // End of Scope
/*  25748*/    /*Scope*/ 9, // ->25758
/*  25749*/     OPC_CheckTypeByHwMode0 ...
/*  25750*/     OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::ADD), 0,
```

`ADDW` 的复杂度是 4，`ADD` 是 3；matcher 先尝试更具体的 `ADDW`，带 `Predicate_anonymous_15302` 的规则失败后再落到 `ADD`。这说明“源码里 `ADD` 写在 `ADDW` 前面”不代表 matcher 先试 `ADD`。排序由类型、谓词和复杂度共同决定。

58 MB 的来源不是“代码写得啰嗦”，而是组合量：

- 每种 DAG 操作（`add`、`or`、`shl`、`load`、`store`、向量操作等）都有多条规则。
- 每条规则要检查操作数类型、HwMode、谓词、ComplexPattern、立即数范围。
- RISC-V 有 RV32/RV64、标量/向量、多个供应商扩展，同一个节点可能生成几十条候选。
- 每条候选都要编码成字节码，并附带 `Src`/`Dst` 注释。

本机 `OPC_CheckComplexPat` 出现 `34,565` 次。这个数字可以作为“地址模式和复杂操作数检查有多密集”的粗略指标。它不是精确的规则数，因为一个规则可以包含多个检查。复现命令：

```powershell
(Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern 'OPC_CheckComplexPat' -AllMatches |
  ForEach-Object { $_.Matches.Count } | Measure-Object -Sum).Sum
```

检索 matcher：

```powershell
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern 'TARGET_VAL\(RISCV::ADD\)' -Context 3,3
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern '// Src: \(add:' | Select-Object -First 10
```

第一条直接定位 `ADD` 的生成动作；第二条看所有 `add` 源模式，包括 `ADDW`、`ADD_UW`、供应商扩展。读 matcher 时先看 `Src`/`Dst`，再看 `Complexity`，最后才看字节码。

### 5.5 AsmMatcher、Disassembler、Emitter：另外三条链

`RISCVGenAsmMatcher.inc` 的核心是 `MatchTable0` 和 `MatchEntry`：

```cpp
struct MatchEntry {
  uint16_t Mnemonic;
  uint32_t Opcode;
  uint16_t ConvertFn;
  uint8_t RequiredFeaturesIdx;
  uint16_t Classes[8];
};
static const MatchEntry MatchTable0[] = {
  ...
  { 260 /* addi.imm */, RISCV::ADDI_IMM, Convert__Reg1_0__Reg1_1__SImm12LO1_2,
    AMFBS_None, { MCK_GPR, MCK_GPR, MCK_SImm12LO }, },
```

mnemonic 不是逐字符比较，而是先压成一个字符串表：

```text
"dd\005aaddu\003abs\004absw\003add\006add.uw\004addd\004addi\010addi.imm"
```

`\010` 是长度前缀，表示后面的 `addi.imm` 有 10 个字符。`MatchEntry` 的 `Mnemonic` 是这张表里的偏移。`Convert__Reg1_0__Reg1_1__SImm12LO1_2` 是 TableGen 生成的操作数转换函数名：第 0 个操作数是 `Reg1`，第 1 个是 `Reg1`，第 2 个是 `SImm12LO1`。

`RISCVGenDisassemblerTables.inc` 负责反方向：给定 16/32 位位模式，找到 opcode 并构造 MCInst。它和 asm matcher 是两套表，不要混。`-gen-disassembler` 产物 1,485,949 字节，比 asm matcher 大，因为解码要覆盖位模式和操作数解码函数。

`RISCVGenMCCodeEmitter.inc` 负责把 MCInst 变成字节。开头是：

```cpp
uint64_t RISCVMCCodeEmitter::getBinaryCodeForInstr(...) const {
  static const uint64_t InstBits[] = {
    UINT64_C(2583691323),	// AADD
    ...
    UINT64_C(51),	// ADD
    UINT64_C(2248171547),	// ADDD
    UINT64_C(19),	// ADDI
```

`51` 就是 `0x33`，`ADD` 的 opcode 固定位。后面的 switch 只补可变字段：

```cpp
case RISCV::ADD:
...
  // op: rs2
  op = getMachineOpValue(MI, MI.getOperand(2), Fixups, STI);
  Value |= (op & 0x1f) << 20;
  // op: rs1
  op = getMachineOpValue(MI, MI.getOperand(1), Fixups, STI);
  Value |= (op & 0x1f) << 15;
  // op: rd
  op = getMachineOpValue(MI, MI.getOperand(0), Fixups, STI);
  Value |= (op & 0x1f) << 7;
```

固定位来自 `.td` 的 `Inst` 字段，可变字段来自操作数。这个分工在第 06 章会用真实字节逐位拆开。

`-gen-subtarget` 产物 25,838,745 字节，包含特性位、CPU 表、调度模型索引等。它回答“在 `-mcpu=sifive-u74` 或 `-mattr=+m,+a` 下，哪些指令和调度资源可用”。`-gen-instr-info` 回答“指令是什么”，`-gen-subtarget` 回答“这条指令在这个 subtarget 上是否可用”。两者不要混。

### 5.6 闭环：复制 `.td`、加一条 `addi.imm`、重新生成、diff

目标：在不改源码树的前提下，观察一条新指令对生成物的真实影响。

第一步，复制整个 RISCV `.td` 目录。不要只复制 `RISCVInstrInfo.td`，因为它的 `include` 会解析到原目录，导致“副本”其实混用了原文件。

```powershell
$src  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$copy = ".scratch/riscv-td-copy-04"
if (-not (Test-Path $copy)) { Copy-Item -LiteralPath $src -Destination $copy -Recurse }
```

本机副本大小 `5,964,313` 字节；RISCV 根目录有 92 个 `.td`，`GISel/` 子目录里另有 1 个，共 93 个。副本是一次性实验目录，不要提交。

第二步，在副本的 `RISCVInstrInfo.td` 第 797 行 `ADDI` 后面插入一行：

```tablegen
def ADDI_IMM : ALU_ri<0b000, "addi.imm">;
```

插入后：

```text
797: def ADDI  : ALU_ri<0b000, "addi">;
798: def ADDI_IMM : ALU_ri<0b000, "addi.imm">;
```

`ALU_ri` 的 `funct3` 仍是 `0b000`，opcode 仍是 `OPC_OP_IMM`，操作数仍是 `GPR, simm12_lo`。所以它和 `ADDI` 编码相同；记录层面还差 mnemonic，以及 `ADDI` 自己的两个重物化标志（它们不会自动传给新 `def`）。这正是练习 B 要的最小改动。

第三步，用副本作为 include 根重新生成：

```powershell
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/copy-InstrInfo.inc"
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-asm-matcher "$copy/RISCV.td" -o "$out/copy-AsmMatcher.inc"
```

注意 `-I $copy` 要排在所有指向原源码树的 `-I` 之前。TableGen 按 `-I` 顺序找 include；如果同时传了 `-I $td -I $copy`，原目录会先命中，副本的 `ADDI_IMM` 就被忽略。本实验只传 `-I $copy -I $inc`，`$inc` 里没有这些 `.td`，所以不会抢。

第四步，看枚举。本机真实输出：

```text
13742: ADDI                                       = 13727, // RISCVInstrInfo.td:797
13743: ADDIW                                      = 13728, // RISCVInstrInfo.td:912
13744: ADDI_IMM                                   = 13729, // RISCVInstrInfo.td:798
13745: ADDW                                       = 13730, // RISCVInstrInfo.td:924
13746: ADD_UW                                     = 13731, // RISCVInstrInfoZb.td:285
```

对照基线：

```text
base 13744: ADDW                                       = 13729, // RISCVInstrInfo.td:923
base 13745: ADD_UW                                     = 13730, // RISCVInstrInfoZb.td:285
```

`ADDI_IMM` 插入后，`ADDW`、`ADD_UW` 以及它们后面的所有指令编号都加一。`ADDIW` 的源码行也从 911 变成 912（副本里插入点在 798，后面行号整体加一；本机显示 `ADDIW` 定义在 912，而基线是 911，这是插入一行造成的位移）。这类位移是生成物 diff 巨大的主要原因之一。

第五步，看汇编匹配。本机真实输出：

```text
6326: "dd\005aaddu\003abs\004absw\003add\006add.uw\004addd\004addi\010addi.imm"
7406: { 260 /* addi.imm */, RISCV::ADDI_IMM, Convert__Reg1_0__Reg1_1__SImm12LO1_2, AMFBS_None, { MCK_GPR, MCK_GPR, MCK_SImm12LO }, },
```

`addi.imm` 进入了 mnemonic 字符串表，`MatchEntry` 指向 `RISCV::ADDI_IMM`，转换函数是 `Convert__Reg1_0__Reg1_1__SImm12LO1_2`。这说明 MC 层的汇编器已经认识这条新指令；但 `llc` 不会自动选择它，因为没有任何 `Pat` 指向 `ADDI_IMM`。这是第 5.7 节和练习 B 的关键边界。

第六步，diff。用 `git diff --no-index --numstat` 做行级统计：

```powershell
git diff --no-index --numstat -- "$out/RISCVGenInstrInfo.inc" "$out/copy-InstrInfo.inc"
git diff --no-index --numstat -- "$out/RISCVGenAsmMatcher.inc" "$out/copy-AsmMatcher.inc"
```

本机真实结果（省略了目录和 git 的 `{旧 => 新}` 花括号）：

```text
9228    9224    RISCVGenInstrInfo.inc => copy-InstrInfo.inc
6227    6226    RISCVGenAsmMatcher.inc => copy-AsmMatcher.inc
```

插入一行 `.td`，`InstrInfo` 变了 9,228 行，`AsmMatcher` 变了 6,227 行。不是 TableGen 出错，而是枚举位移、名字表偏移、指令描述数组、匹配表偏移都跟着变。这也是为什么生成物不能手改：你改一处，下一次生成就会覆盖，而且 diff 无法审查。

第七步，确认副本没有污染源码树：

```powershell
Select-String -LiteralPath "$src/RISCVInstrInfo.td" -Pattern 'ADDI_IMM'
```

预期没有输出。副本里才有 `ADDI_IMM`，源码树保持原样。实验结束可以保留副本用于对照，也可以删除；不要在源码树里做同样的修改。

### 5.7 为什么生成物不进版本库

生成物不进版本库，原因不是“文件太大”这一条，而是四条一起：

1. **可重建。** 输入是 `.td`、include 路径和 `llvm-tblgen` 版本；输出由构建系统生成。版本库保存输入即可。
2. **体量大。** 六个产物合计 95.8 MB，单是 DAGISel 就 60 MB。每次改 `.td` 都提交一份，会把仓库历史迅速撑大。
3. **不稳定。** 枚举编号、名字表偏移、matcher 偏移会随插入顺序变化；不同构建目录、不同 line ending 也可能产生无意义 diff。代码审查会被噪声淹没。
4. **容易过期。** 手改 `.inc` 后，下一次 `cmake --build` 会重新生成并覆盖；如果生成物被提交，别人拉到的可能是和 `.td` 不一致的版本。

生成的每个文件头都写着：

```text
Automatically generated file, do not edit!
```

这不是客套话。正确的工作流是改 `.td`，跑对应 generator 做快速检查，再让构建系统生成 `.inc`。版本库里保存 `.td`、测试和构建脚本，不保存 `.inc`。

## 可跑实验

### 实验 1：重建六个生成物

目标：建立尺寸基线，确认工具链可用。

```powershell
$bin = ".scratch/llvm-23.1.4-root/clang+llvm-23.1.4-x86_64-pc-windows-msvc/bin"
$inc = "$bin/../include"
$td  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$out = ".scratch/riscv-tblgen-ev"
New-Item -ItemType Directory -Force -Path $out | Out-Null
$jobs = @(
  @('-gen-instr-info',   'RISCVGenInstrInfo.inc'),
  @('-gen-asm-matcher',  'RISCVGenAsmMatcher.inc'),
  @('-gen-disassembler', 'RISCVGenDisassemblerTables.inc'),
  @('-gen-emitter',      'RISCVGenMCCodeEmitter.inc'),
  @('-gen-subtarget',    'RISCVGenSubtargetInfo.inc'),
  @('-gen-dag-isel',     'RISCVGenDAGISel.inc')
)
foreach ($j in $jobs) {
  & "$bin/llvm-tblgen.exe" -I $inc -I $td $j[0] "$td/RISCV.td" -o (Join-Path $out $j[1])
  if ($LASTEXITCODE -ne 0) { throw "failed: $($j[0])" }
  $f = Get-Item (Join-Path $out $j[1])
  '{0,-22} {1,12:N0} bytes {2,10:N0} lines' -f $j[0], $f.Length, ((Get-Content -LiteralPath $f.FullName).Count)
}
```

预期：六行输出，大小与 5.1 表一致；`-gen-dag-isel` 约 60 MB，`-gen-subtarget` 约 25 MB。

### 实验 2：三条 grep 命中 ADD

目标：练习用锚点而不是全文搜索。

```powershell
Select-String -LiteralPath "$out\RISCVGenInstrInfo.inc" -Pattern '^\s*ADD\s+='
Select-String -LiteralPath "$out\RISCVGenInstrInfo.inc" -Pattern '"ADD\\000"'
Select-String -LiteralPath "$out\RISCVGenDAGISel.inc" -Pattern 'TARGET_VAL\(RISCV::ADD\)' -Context 3,3
```

预期：

- 第一条命中 `ADD = 13725, // RISCVInstrInfo.td:818`。
- 第二条命中名字表里的 `ADD\000`。
- 第三条命中 `OPC_MorphNodeToByHwMode, TARGET_VAL(RISCV::ADD)`，上下文里有 `Src: (add ...)` 和 `Dst: (ADD ...)`。

### 实验 3：副本加一条指令并 diff

目标：观察一条 `.td` 改动对枚举和汇编匹配的真实影响。

```powershell
$src  = ".scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV"
$copy = ".scratch/riscv-td-copy-04"
if (-not (Test-Path $copy)) { Copy-Item -LiteralPath $src -Destination $copy -Recurse }
$f = Join-Path $copy 'RISCVInstrInfo.td'
$lines = [IO.File]::ReadAllLines($f)
$idx = -1
for ($i = 0; $i -lt $lines.Count; $i++) {
  if ($lines[$i] -eq 'def ADDI  : ALU_ri<0b000, "addi">;') { $idx = $i; break }
}
if ($idx -lt 0) { throw 'ADDI anchor not found' }
$new = New-Object System.Collections.Generic.List[string]
$new.AddRange([string[]]$lines[0..$idx])
$new.Add('def ADDI_IMM : ALU_ri<0b000, "addi.imm">;')
if ($idx + 1 -lt $lines.Count) { $new.AddRange([string[]]$lines[($idx+1)..($lines.Count-1)]) }
[IO.File]::WriteAllText($f, (($new -join "`n")+"`n"), (New-Object Text.UTF8Encoding($false)))
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/copy-InstrInfo.inc"
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-asm-matcher "$copy/RISCV.td" -o "$out/copy-AsmMatcher.inc"
Select-String -LiteralPath "$out/copy-InstrInfo.inc" -Pattern 'ADDI_IMM|^\s*ADDW\s+='
Select-String -LiteralPath "$out/copy-AsmMatcher.inc" -Pattern 'addi\.imm'
git diff --no-index --numstat -- "$out/RISCVGenInstrInfo.inc" "$out/copy-InstrInfo.inc"
```

预期：`ADDI_IMM = 13729`，`ADDW` 变成 `13730`；`addi.imm` 出现在 mnemonic 表和 `MatchEntry`；`git diff --numstat` 显示约 9,228/9,224 行变化。

### 实验 4：失败例子——副本里的重名定义

目标：确认“加指令”不是随便写一个 `def` 就行，名字冲突会在 tblgen 阶段失败。

在副本里把 `ADDI_IMM` 改成 `ADDI`（或者再写一条 `def ADDI`），然后运行：

```powershell
& "$bin/llvm-tblgen.exe" -I $copy -I $inc -gen-instr-info "$copy/RISCV.td" -o "$out/dup.inc"
```

本机等价的最小实验报错是：

```text
error: def already exists: D
def D : C;
    ^
note: location of previous definition
```

真实 RISC-V 副本里的重名会给出类似的 `def already exists: ADDI`。这类错误不会生成半个 `.inc`；tblgen 在解析阶段就退出，返回非零。看到失败时先查名字，不要先怀疑 include 路径。

## 源码落点

| 位置 | 符号/产物 | 说明 |
| --- | --- | --- |
| `RISCV.td` | `RISCV : Target` | 六个 generator 的共同入口 |
| `RISCVInstrInfo.td:797` | `def ADDI` | `ADDI = 13727` 的来源 |
| `RISCVInstrInfo.td:818` | `def ADD` | `ADD = 13725` 的来源 |
| `RISCVInstrInfo.td:1470` | `PatGprGpr<add, ADD>` | matcher 里 `RISCV::ADD` 动作的来源 |
| `RISCVInstrFormats.td:387` | `RVInstR` | `Inst` 固定位的来源 |
| `RISCVGenInstrInfo.inc` | `GET_INSTRINFO_ENUM` | 枚举；`ADD = 13725` 在生成物第 13740 行 |
| `RISCVGenInstrInfo.inc` | `RISCVInstrNameData` | 压缩名字表，生成物第 41372 行起 |
| `RISCVGenInstrInfo.inc` | `RISCVInstrNameIndices` | 名字偏移表，生成物第 58032 行起 |
| `RISCVGenInstrInfo.inc` | `InitMCInstrInfo(..., 16883, ...)` | 指令总数，生成物第 60147 行 |
| `RISCVGenAsmMatcher.inc` | `MatchTable0` | 生成物第 7363 行起 |
| `RISCVGenAsmMatcher.inc` | `addi.imm` mnemonic | 生成物第 6326 行 |
| `RISCVGenDAGISel.inc` | `MatcherTable` | 生成物第 58 行起 |
| `RISCVGenDAGISel.inc` | `TARGET_VAL(RISCV::ADD)` | 生成物第 12961 行，偏移 25750 |
| `RISCVGenMCCodeEmitter.inc` | `InstBits[]` | `ADD` 的固定位 `UINT64_C(51)` |
| `RISCVGenMCCodeEmitter.inc` | `case RISCV::ADD` | 生成物第 7065 行起 |
| `RISCVGenSubtargetInfo.inc` | 特性/CPU 表 | 25,838,745 字节，177,991 行 |
| `RISCVGenDisassemblerTables.inc` | 解码表 | 1,485,949 字节，20,926 行 |

注意最后六个 `.inc` 的路径是生成物，不是源码树里的文件；它们由构建系统放在 build 目录或临时输出目录。本文为了实验把它们写到 `.scratch/riscv-tblgen-ev/`，不要提交。

## 常见坑

1. **直接改 `.inc`。** 下一次 `cmake --build` 会重新生成并覆盖；即使暂时能编译，源码树和生成物也会不一致。改 `.td`，不要改 `.inc`。

2. **硬编码枚举号。** `ADD = 13725` 只是本次构建的值。插入一条指令后，后面的编号整体位移；在插件、脚本或外部工具里硬编码会静默错位。

3. **grep `ADD` 不带锚点。** `ADD` 会命中 `ADDI`、`ADDW`、`ADD_UW`、`PMUL...`。用 `^\s*ADD\s+=` 或 `TARGET_VAL\(RISCV::ADD\)`。

4. **把 AsmMatcher 的 `OPC_` 和 DAGISel 的 `OPC_` 混在一起。** `RISCVGenAsmMatcher.inc` 用的是 `MatchEntry`、`MCK_*`、`Convert__*`；`RISCVGenDAGISel.inc` 才是 `OPC_*` 字节码。两者不是同一套表。

5. **只重生成 `-gen-instr-info`。** 加一条指令还会影响 asm matcher、disassembler、emitter、subtarget、dag-isel。只跑一个 generator 会漏掉其他产物的变化。

6. **只复制 `RISCVInstrInfo.td`。** 它的 `include` 会解析到原目录，副本会混用原文件；复制整个 RISCV 目录，并把 `-I $copy` 放在最前面。

7. **以为加了指令 llc 就会选它。** 生成物里有 `ADDI_IMM` 只说明 MC 层认识它；没有 `Pat` 指向它，`llc` 不会主动生成。要让 llc 选中，需要额外的 pattern 或自定义 lowering，这是练习 B 的 guided 部分。

8. **用编辑器打开 60 MB 的 DAGISel。** 会卡顿，而且没有上下文。用 `Select-String` 定位 `TARGET_VAL`、`Src:`、`Dst:`，再看前后 10 行。

9. **把生成物大小当成代码复杂度。** 60 MB 里包含大量注释、字节码表、HwMode 组合和名字表；它不是“手写代码量”，不能拿来评估后端质量。

10. **忽略 line ending 差异。** Windows 上生成物可能是 LF，git 可能提示 CRLF 转换；做 diff 前先确认没有纯换行噪声，否则会把 9,000 行变化误判成 20,000 行。

## 自我检查

- [ ] 我能说出六个 generator 各自回答什么问题，并把它们和产物文件名对上。
- [ ] 我能用 `^\s*ADD\s+=` 找到 `ADD = 13725`，并解释注释里的 `RISCVInstrInfo.td:818` 是什么。
- [ ] 我能解释 `RISCVInstrNameData` 和 `RISCVInstrNameIndices` 为什么要分成两张表。
- [ ] 我能在 `RISCVGenDAGISel.inc` 里定位 `TARGET_VAL(RISCV::ADD)`，并读出 `Src`/`Dst` 和 `Complexity`。
- [ ] 我能解释 `InstBits[]` 里的 `UINT64_C(51)` 和 `case RISCV::ADD` 里的移位分别负责什么。
- [ ] 我能用 `.td` 副本加一条 `addi.imm`，并解释为什么 `ADDW` 的编号会从 13729 变成 13730。
- [ ] 我能说出生成物不进版本库的至少三个理由。
- [ ] 我知道“MC 层能汇编”和“llc 会选择”是两件事。

## 延伸阅读

- LLVM TableGen Backends：<https://llvm.org/docs/TableGen/BackEnds.html>
- LLVM 后端开发指南：<https://llvm.org/docs/CodeGenerator.html>
- LLVM MC 层文档：<https://llvm.org/docs/CodeGenerator.html#the-mc-layer>
- `llvm/lib/Target/RISCV/RISCV.td`
- `llvm/lib/Target/RISCV/RISCVInstrInfo.td`
- `llvm/lib/Target/RISCV/RISCVInstrFormats.td`
- `llvm/lib/Target/RISCV/MCTargetDesc/RISCVMCCodeEmitter.cpp`
- `llvm/utils/TableGen/`（TableGen backend 源码；本机 sparse checkout 未包含，阅读时以同版本为准）
- `llvm/test/CodeGen/RISCV/` 与 `llvm/test/MC/RISCV/`（生成物的行为回归）

[上一章：TableGen 语言](./04-tablegen-language.md) | [下一章：指令编码](./06-instruction-encoding.md)