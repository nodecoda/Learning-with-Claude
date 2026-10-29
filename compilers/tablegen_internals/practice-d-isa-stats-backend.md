# 练习 D：写一个 `-gen-isa-stats` backend（并真正注册进 `llvm-tblgen`）

> **目标**：从零写一个 120 行的 TableGen backend，统计每个 target 的指令集形状——指令数、伪指令比例、操作数分布、flag 分布——然后把它**注册进 `llvm-tblgen` 本体**，用 `-gen-isa-stats` 跑四个真实 backend，补一个 lit 测试，并让 `llvm/test/TableGen` 全绿。这是全书唯一一个「完整走完一个 backend 生命周期」的练习：`Record` → 统计 → 注册 → 测试 → 全量回归。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。所有输出都在本机实跑。注册验证用的是 `/tmp/tgbuild`（`Release + LLVM_ENABLE_ASSERTIONS=ON`，`-DLLVM_TARGETS_TO_BUILD=""`）。实验期间临时改过 `llvm/utils/TableGen/CMakeLists.txt` 与新增两个文件，跑完立刻 `git checkout` 还原，**最终 `git status` 为空**。

---

## 任务规格

写一个 backend，输出下面这种报告（四个 target 都是真实输出）：

```console
$ llvm-tblgen -gen-isa-stats -I <target> -I llvm/lib/Target -I llvm/include AArch64.td
ISA statistics
==============

  instructions              9344
  pseudo                    1757
  barrier                     35
  variadic                     0
  no-operand                  67
  operand slots total      32830
  operand slots mean        3.51
  widest                       8  (CMP_SWAP_128)

Operand-count histogram
------------------------
    0 operands      67  
    1 operands     129  
    2 operands    1281  #####
    3 operands    3233  #############
    4 operands    3413  ##############
    5 operands     665  ##
    6 operands     435  #
    7 operands     115  
    8 operands       6  

Flag histogram
--------------
  mayLoad              1772
  isCodeGenOnly        1753
  mayStore             1526
  hasSideEffects        581
  isTerminator           91
  isAsCheapAsAMove       73
  isBranch               65
  isCall                 30
  isReturn               24
```

验收标准：

| # | 条件 |
|---|---|
| 1 | `llvm-tblgen --help` 的 `Action to perform:` 组里出现 `--gen-isa-stats` |
| 2 | 四个 target（AArch64 / X86 / RISCV / ARM）都能跑出报告，且数字与本章表格一致 |
| 3 | 新增 `llvm/test/TableGen/ISAstats.td`，`llvm-lit` 通过 |
| 4 | `llvm/test/TableGen` 全量回归通过（本机 497/497，含你的新测试） |
| 5 | 整个 backend 不使用 `CodeGenTarget`/`CodeGenInstruction`，只用 `Record` API |

第 5 条是刻意的：`CodeGenInstruction` 会做大量规范化（展开 operand list、解析 `let` 继承、检查 `Namespace`），适合 Ch 17 那种「重型」backend，但对你**第一次**写 backend 是负担。只用 `Record` API 意味着你的 backend 能跑在**任何** `.td` 上，包括没有 `Target.td` 的最小测试文件。

---

## Step 0（20 分钟）：搞清链接依赖

`-gen-isa-stats` 只要 `Record` + `raw_ostream`，理论上只需要 `libLLVMTableGen` + `libLLVMSupport`。但你想用 `TableGen::Emitter::Opt` 把 backend 挂到 `cl::opt` 上（Step 3 才需要），就得连 `libLLVMTableGenBasic`——`ApplyCallback` 与 `Opt` 的实现在那里。

本机实测，一个跑 `TableGenMain` + 自定义 backend 的独立程序，最小的可行链接集是**五个库**：

```bash
L=/home/ubuntu/llvm/llvm-dev/build/lib
g++ -std=c++17 -O2 \
  -I/home/ubuntu/llvm/llvm-project/llvm/include \
  -I/home/ubuntu/llvm/llvm-dev/build/include \
  ISAstats.cpp main.cpp -o isa-stats \
  $L/libLLVMTableGenCommon.a \
  $L/libLLVMTableGenBasic.a \
  $L/libLLVMTableGen.a \
  $L/libLLVMSupport.a \
  $L/libLLVMDemangle.a \
  -lpthread -lz -lzstd -ltinfo
```

`libLLVMTableGenCommon.a` 看着多余（你没用 `CodeGenTarget`），但 `TableGenBackend.cpp` 在 `Basic`，而 `Basic` 里 `TableGen.cpp.o` 的 `tblgen_main` 又引用了 `SetTheory`（在 `TableGen` 里）……实测：**少任何一个都会 `undefined reference`**，而多加 `GlobalISel` / `CodeGenTypes` 则是**不需要**的（加与不加都能链过，但不加产物小 30 KB）。

`main.cpp` 就四行：

```cpp
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/InitLLVM.h"
#include "llvm/TableGen/Main.h"
using namespace llvm;
int main(int argc, char **argv) {
  InitLLVM X(argc, argv);
  cl::ParseCommandLineOptions(argc, argv);
  // 两个重载都有默认参数 nullptr，直接传 argv[0] 会歧义，必须显式选一个
  return TableGenMain(argv[0], MultiFileTableGenMainFn());
}
```

**`TableGenMain(argv[0])` 编译不过**——`TableGenMainFn` 与 `MultiFileTableGenMainFn` 都有默认值 `nullptr`，重载解析歧义：

```console
error: call of overloaded ‘TableGenMain(char*&)’ is ambiguous
```

这一条坑得很隐蔽，官方 `llvm-tblgen.cpp` 用的是 `tblgen_main()`，看不到这个歧义。显式写出 `MultiFileTableGenMainFn()` 即可（`SingleFileGenerator` 那条分支也走同一个入口）。

**验收**：`./isa-stats --help` 打印出 `-I` / `-o` / `--time-phases`，说明 `TableGenMain` 的公共选项已挂上。

---

## Step 1（30 分钟）：第一版，然后踩一个「dag 不是 list」的坑

最直觉的写法是「指令的操作数是个列表」：

```cpp
unsigned NOuts = I->getValueAsListOfDefs("OutOperandList").size();
unsigned NIns  = I->getValueAsListOfDefs("InOperandList").size();
```

在 AArch64 上，这一行会让 `llvm-tblgen` 直接退出：

```console
$ llvm-tblgen -gen-isa-stats ... AArch64.td
Included from /home/ubuntu/llvm/llvm-project/llvm/lib/Target/AArch64/AArch64.td:37:
Included from /home/ubuntu/llvm/llvm-project/llvm/lib/Target/AArch64/AArch64InstrInfo.td:1418:
/home/ubuntu/llvm/llvm-project/llvm/lib/Target/AArch64/AArch64InstrFormats.td:2526:7: error: Record `ABSWr', field `OutOperandList' exists but does not have a list value
  def Wr : BaseOneOperandData<0b0, 0b0, 0b00000, opc, GPR32, asm, node>;
      ^
Included from /home/ubuntu/llvm/llvm-project/llvm/lib/Target/AArch64/AArch64.td:37:
/home/ubuntu/llvm/llvm-project/llvm/lib/Target/AArch64/AArch64InstrInfo.td:12341:12: note: instantiated from multiclass
```

`OutOperandList` 的**声明类型是 `dag`**，不是 `list<dag>`。`getValueAsListOfDefs` 底层走 `getValueAsListInit`（`Record.cpp:2990-2996`），不是 `ListInit` 就 `PrintFatalError`，进程退出。

实测确认：

```console
$ ./probe ... AArch64.td
type of OutOperandList: dag
```

正确写法是同时认 `ListInit` 与 `DagInit`：

```cpp
static unsigned listSize(const Record &R, StringRef Field) {
  const RecordVal *V = R.getValue(Field);
  if (!V) return 0;
  if (isa<UnsetInit>(V->getValue())) return 0;
  if (const auto *L = dyn_cast<ListInit>(V->getValue())) return L->size();
  if (const auto *D = dyn_cast<DagInit>(V->getValue())) return D->getNumArgs();
  return 0;
}
```

三个防御都不是多余的：
- `R.getValue(Field)` 可能返回 `nullptr`（字段不存在——最小 `.td` 里常见）；
- 值可能是 `UnsetInit`（`?`）；
- 值可能是 `dag`（真实 backend 全是 `dag`）。

> **顺手记一个更深的坑**：`getValueAsInt` / `getValueAsListInit` 这些「强类型 getter」在类型不符时会调 `PrintFatalError(Record->getLoc(), ...)`，也就是**用记录的 `SMLoc` 打诊断**。而 `TableGenParseFile`（`Parser.cpp:53-58`）在返回前会把 buffer 从全局 `SrcMgr` 转移走并 `SrcMgr = SourceMgr()`。于是你**在解析完成之后**、在一个自己搭的 driver 里调用强类型 getter，一旦类型不符，`SrcMgr` 里没有 buffer，`PrintMessage` 解引用空指针——**段错误**，不是漂亮的错误信息。

本机实测两种 driver 的差异：

```console
# A. 自己搭 TableGenParseFile + 直接调 getValueAsInt（类型不符 → segfault）
$ ./badmain2 bad2.td
parse OK
--- ask a dag field for an int (triggers PrintFatalError with SMLoc) ---
Segmentation fault (core dumped)

# B. 同样的 backend 走 TableGenMain（正常报错并退出）
$ ./badtblgen -gen-bad ... AArch64.td
-- asking ABSWr.OutOperandList for a list of defs --
.../AArch64InstrFormats.td:2526:7: error: Record `ABSWr', field `OutOperandList' exists but does not have a list value
```

**结论**：写 backend 就老老实实走 `TableGenMain`（它内部直接用全局 `SrcMgr` 解析、解析完不重置），别自己拼 `TableGenParseFile` + 调强类型 getter。这是「练习 B 可以直接用 `TableGenParseFile`，练习 D 不行」的原因。

---

## Step 2（40 分钟）：把统计写全

完整的 `ISAstats.cpp`（本机实测，四个 target 都跑通）：

```cpp
//===- ISAstats.cpp - print statistics about the instruction set ----------===//
#include "llvm/ADT/StringMap.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TableGen/Record.h"
#include "llvm/TableGen/TableGenBackend.h"
#include <algorithm>
#include <map>
#include <vector>

using namespace llvm;

namespace {
class ISAstatsEmitter {
  const RecordKeeper &Records;

  // Returns the number of defs in a list field, or 0 if the field is unset.
  static unsigned listSize(const Record &R, StringRef Field) {
    const RecordVal *V = R.getValue(Field);
    if (!V) return 0;
    if (isa<UnsetInit>(V->getValue())) return 0;
    if (const auto *L = dyn_cast<ListInit>(V->getValue())) return L->size();
    if (const auto *D = dyn_cast<DagInit>(V->getValue())) return D->getNumArgs();
    return 0;
  }

  // Returns the value of a bit field, or false if unset/missing.
  static bool bit(const Record &R, StringRef Field) {
    const RecordVal *V = R.getValue(Field);
    if (!V) return false;
    if (isa<UnsetInit>(V->getValue())) return false;
    return R.getValueAsBit(Field);
  }

public:
  explicit ISAstatsEmitter(const RecordKeeper &RK) : Records(RK) {}
  void run(raw_ostream &OS);
};
} // namespace

void ISAstatsEmitter::run(raw_ostream &OS) {
  std::vector<const Record *> Instrs =
      Records.getAllDerivedDefinitions("Instruction");
  std::sort(Instrs.begin(), Instrs.end(), [](const Record *A, const Record *B) {
    return A->getName() < B->getName();
  });

  unsigned NumPseudo = 0, NumBarrier = 0, NumVariadic = 0;
  unsigned NumNoOperands = 0;
  uint64_t TotalOperands = 0;
  std::map<unsigned, unsigned> OperandHistogram;
  std::map<std::string, unsigned> Flags;
  const Record *Widest = nullptr;
  unsigned WidestOps = 0;

  for (const Record *I : Instrs) {
    unsigned N = listSize(*I, "OutOperandList") +
                 listSize(*I, "InOperandList");
    TotalOperands += N;
    OperandHistogram[N]++;
    if (N == 0) NumNoOperands++;
    if (N > WidestOps) { WidestOps = N; Widest = I; }
    if (bit(*I, "isPseudo")) NumPseudo++;
    if (bit(*I, "isBarrier")) NumBarrier++;
    if (bit(*I, "isVariadic")) NumVariadic++;
    for (StringRef F : {"hasSideEffects", "isTerminator", "isBranch",
                        "isCall", "isReturn", "mayLoad", "mayStore",
                        "isCodeGenOnly", "isAsCheapAsAMove"})
      if (bit(*I, F)) Flags[F.str()]++;
  }

  OS << "ISA statistics\n";
  OS << "==============\n\n";
  OS << format("  instructions          %8u\n", (unsigned)Instrs.size());
  OS << format("  pseudo                %8u\n", NumPseudo);
  OS << format("  barrier               %8u\n", NumBarrier);
  OS << format("  variadic              %8u\n", NumVariadic);
  OS << format("  no-operand            %8u\n", NumNoOperands);
  OS << format("  operand slots total   %8llu\n",
               (unsigned long long)TotalOperands);
  if (!Instrs.empty())
    OS << format("  operand slots mean    %8.2f\n",
                 double(TotalOperands) / double(Instrs.size()));
  if (Widest)
    OS << format("  widest                %8u  (%s)\n", WidestOps,
                 Widest->getNameInitAsString().c_str());
  OS << "\nOperand-count histogram\n------------------------\n";
  for (auto &KV : OperandHistogram)
    OS << format("  %3u operands  %6u  ", KV.first, KV.second)
       << std::string(40 * KV.second / std::max(1u, (unsigned)Instrs.size()), '#')
       << "\n";
  OS << "\nFlag histogram\n--------------\n";
  std::vector<std::pair<std::string, unsigned>> FV(Flags.begin(), Flags.end());
  std::sort(FV.begin(), FV.end(),
            [](auto &A, auto &B) { return A.second > B.second; });
  for (auto &KV : FV)
    OS << format("  %-18s %6u\n", KV.c_str(), KV.second);
}

static void emitISAstats(const RecordKeeper &Records, raw_ostream &OS) {
  ISAstatsEmitter(Records).run(OS);
}

static TableGen::Emitter::Opt
    X("gen-isa-stats", emitISAstats,
      "Print instruction-set statistics");
```

三个设计点：

**(a) `getAllDerivedDefinitions("Instruction")` 只返回 def，不返回 class。** 它内部（`Record.cpp:3244-3276`）遍历的是 `DefsByDirectSuperClass`——这个索引在 `addDef`（`Record.h:2126-2136`）里维护，class 走的是 `addClass`，不进这个表。所以你拿到的是 9,344 个 `def`，而不是「9,344 个 def + 若干中间 class」。

**(b) 排序要自己做。** `getAllDerivedDefinitions` 返回的是按 `LessRecord()`（`Record.h:2184-2188`，`compare_numeric`）排序的 `std::vector`，**不是字典序**——`compare_numeric` 会把数字当数字比。想和 `-print-records` 的壳一致就自己 `std::sort(Name < Name)`。这个 backend 里其实只有 `widest` 一项依赖顺序（同分取先遇到的），但写测试时你会需要一个确定的答案。

**(c) `format` 的宽度是「总宽度」不是「精度」。** `%8u` 右对齐到 8 列；`%8.2f` 是总宽 8、小数 2 位。`%-18s` 左对齐 18 列。写错会得到对齐错乱但内容正确的输出——`FileCheck` 的 `CHECK:` 会放过你，只有人眼看得出来。

---

## Step 3（40 分钟）：注册进 `llvm-tblgen`，实测 `--help` 里出现

`static TableGen::Emitter::Opt X(...)` 这一行**在静态初始化期**就把 backend 挂进 `cl::opt`（`TableGenBackend.cpp:64-70` 的 `Opt::Opt` 把 `FnT` 注册到 `CallbackFunction`）。所以只要这个 `.cpp` 被链进 `llvm-tblgen`，它就自动可用。

两步改动，本机实测通过：

**3.1 把文件放进 `llvm/utils/TableGen/`**

```console
$ cp /tmp/tgpd/ISAstats.cpp llvm/utils/TableGen/ISAstatsEmitter.cpp
```

**3.2 在 `llvm/utils/TableGen/CMakeLists.txt` 的 `add_tablegen(llvm-tblgen ...)` 源列表里加一行**

```cmake
  InstrDocsEmitter.cpp
+ ISAstatsEmitter.cpp
  llvm-tblgen.cpp
```

注意是加在**`llvm-tblgen`** 那个列表里，不是 `llvm-min-tblgen` 的。`llvm-min-tblgen`（`CMakeLists.txt:26-32`）只编 `llvm-min-tblgen.cpp` + `$<TARGET_OBJECTS:obj.LLVMTableGenBasic>`，它的职责是「编出 `llvm/include` 需要的那些 `.inc`」，链 `TableGenCommon` 是禁止的（会循环依赖）。

然后：

```console
$ cmake -S llvm -B /tmp/tgbuild >/dev/null && ninja -C /tmp/tgbuild llvm-tblgen
$ /tmp/tgbuild/bin/llvm-tblgen --help | grep -E "isa-stats|print-records"
      --print-records                         - Print all records to stdout (default)
      --gen-isa-stats                         - Print instruction-set statistics
```

**验收成功**。注意 `--gen-isa-stats`（两个连字符）——`cl::opt` 的现代写法，`Opt` 的构造参数不带前导 `-`。

> **为什么不用 `OptClass<EmitterC>`？** `InstrInfoEmitter.cpp:1485` 用的是 `OptClass`，它要求你的类有一个静态 `run(StringRef FilenamePrefix, const RecordKeeper &)` 且返回 `TableGenOutputFiles`。单文件 backend 用裸 `Opt` + 函数指针就够了（`InstrDocsEmitter.cpp:228` 就是这种写法）。等你需要**一次输出多个文件**（像 `RegisterInfoEmitter` 产 4 个 `.inc`）再换 `OptClass`。

---

## Step 4（30 分钟）：四个 target 的真实数字

```console
$ for T in AArch64 X86 RISCV ARM; do
    D=llvm/lib/Target/$T
    llvm-tblgen -gen-isa-stats -I $D -I llvm/lib/Target -I llvm/include $D/$T.td > stats_$T.txt
  done
```

汇总表（本机实测）：

| 指标 | AArch64 | X86 | RISCV | ARM |
|---|---:|---:|---:|---:|
| instructions | 9,344 | 22,770 | 16,966 | 4,539 |
| pseudo | 1,757 (18.8%) | 452 (2.0%) | **13,774 (81.2%)** | 821 (18.1%) |
| barrier | 35 | 71 | 46 | 67 |
| variadic | 0 | 0 | 0 | 0 |
| no-operand | 67 | 271 | 61 | 33 |
| operand slots | 32,830 | 78,374 | 104,102 | 17,563 |
| 平均操作数 | 3.51 | 3.44 | **6.14** | 3.87 |
| widest | 8 (`CMP_SWAP_128`) | 7 (`PATCHPOINT`) | 9 (`PseudoNDS_VFPMADB_VFPR16_M1_MASK`) | **13 (`VLD4LNd16_UPD`)** |

几件值得停下来看的事：

**RISCV 的 81.2% 是伪指令。** 16,966 - 13,774 = 3,192 条「真」指令，其余全是 `Pseudo*`。这不是 RISCV backend 写得差，而是它的 vector 扩展用伪指令承载 `vsetvli` 的配置（Ch 17 提到过 RISCV 的 `-print-records` 输出 151 MB，2,138 个 class 里 1,641 个带模板参数——同一件事的另一个侧面）。

**X86 的 `mayLoad` 有 8,445 条**，占 37%。这是 x86 的典型形状：绝大多数指令形式都是访存变体（`ADD32rm`、`ADD64mr`、`MOV8mr`……），而 AArch64 是 load/store 架构，`mayLoad` 只有 1,772（19%）。

**ARM 的 widest 是 13 个操作数**（`VLD4LNd16_UPD`，4 个寄存器 + 基址 + 写回 + 谓词……）。AArch64 最大 8（`CMP_SWAP_128`，因为是 128 位原子操作要拆成 4 个寄存器对）。这两个数字差异的来源是 API 设计：ARM 把 NEON 多寄存器 load 展开成显式操作数，AArch64 用 `RegisterTuple`。

**`variadic` 四个全是 0。** 这条最有教学价值：`isVariadic` 在 LLVM 里是**给汇编器/解析器**用的标志，不是「操作数个数可变」的意思。你在 TableGen 里数 `InOperandList` 得到的是**声明时列出的槽位数**，运行时才确定个数的指令（如 `INLINEASM`）根本不在 `Instruction` 的子类里。**统计口径决定了你看到的「事实」**——这是本章最想让你记住的一句。

---

## Step 5（30 分钟）：补 lit 测试

`llvm/test/TableGen/ISAstats.td`（本机实测通过）：

```console
// RUN: llvm-tblgen -gen-isa-stats %s | FileCheck %s

def ops;
class Instruction {
  dag OutOperandList = (ops);
  dag InOperandList = (ops);
  bit isPseudo = 0;
  bit isBarrier = 0;
  bit isVariadic = 0;
  bit hasSideEffects = 0;
  bit isTerminator = 0;
  bit isBranch = 0;
  bit isCall = 0;
  bit isReturn = 0;
  bit mayLoad = 0;
  bit mayStore = 0;
  bit isCodeGenOnly = 0;
  bit isAsCheapAsAMove = 0;
}

def GPR;
def Imm;

def I_ADD : Instruction {
  let OutOperandList = (ops GPR:$rd);
  let InOperandList = (ops GPR:$rs1, GPR:$rs2);
}
def I_ADDI : Instruction {
  let OutOperandList = (ops GPR:$rd);
  let InOperandList = (ops GPR:$rs1, Imm:$imm);
  let isCodeGenOnly = 1;
  let mayLoad = 1;
}
def I_TRAP : Instruction {
  let isPseudo = 1;
  let hasSideEffects = 1;
}

// CHECK:      instructions                  3
// CHECK-NEXT: pseudo                        1
// CHECK-NEXT: barrier                       0
// CHECK-NEXT: variadic                      0
// CHECK-NEXT: no-operand                    1
// CHECK-NEXT: operand slots total           6
// CHECK-NEXT: operand slots mean         2.00
// CHECK-NEXT: widest                        3  (I_ADD)
// CHECK:      0 operands       1
// CHECK:      3 operands       2
// CHECK:      hasSideEffects          1
// CHECK:      isCodeGenOnly           1
// CHECK:      mayLoad                 1
```

三个细节：

**(a) 不能用 `include "llvm/Target/Target.td"`。** 那会带进 335 条 `TargetOpcode` 的通用指令（`PHI`、`INLINEASM`……），你的 `CHECK` 数字会变成 338。**最小自包含的 `.td` 更好测**——这也是「只用 `Record` API」的回报。

**(b) `CHECK-NEXT` 会抓对齐。** `FileCheck` 默认把连续空白折叠成一个空格，所以 `instructions                  3` 与 `instructions 3` 都能过——但 `CHECK-NEXT` 会抓**顺序**。本测试故意混用：数值块用 `CHECK-NEXT`（顺序固定），直方图和 flag 用 `CHECK`（顺序由 `std::map` 的键序 + 计数排序决定，不该锁死）。

**(c) 第一次运行会失败。** 我自己写的 `CHECK-NEXT: operand slots total 5` 就错了：`I_ADDI` 有 3 个操作数（`$rd`、`$rs1`、`$imm`），总槽位是 1+2+3 = 6。真实报错是：

```console
# |            9:  operand slots total 6 
# | next:44'2                       ?   possible intended match
# error: command failed with exit status: 1
```

`FileCheck` 把你的期望值贴在 `next:` 旁边、把实际值印在上一行——**读这段 diff 的正确方式是「上一行是真实输出，下一行是我的期望」**。

---

## Step 6（20 分钟）：全量回归

```console
$ /tmp/tgbuild/bin/llvm-lit llvm/test/TableGen
...
Total Discovered Tests: 497
  Passed: 497 (100.00%)
```

（不含你的新测试时是 496。练习 C 的 498 是因为它新增了 **2** 个测试（496 + 2），这里只新增 1 个（496 + 1 = 497）。**以你自己环境的数字为准**，别抄这个数。）

---

## Step 7（20 分钟）：这个 backend 有多贵

先把上一节临时注册的 backend 编进 `llvm-tblgen`（就是 Step 3 那两步，改完记得还原），然后用**同一个二进制** `/tmp/tgbuild/bin/llvm-tblgen`（`Release + assertions`）跑三种 backend，各三次取中位（本机实测，wall clock 与峰值 RSS）：

| target | `-null-backend` | `-gen-isa-stats` | 增量 | `-print-records` |
|---|---:|---:|---:|---:|
| AArch64 | 0.62 s / 172 MB | 0.64 s / 173 MB | **+20 ms** | 1.03 s / 257 MB |
| X86 | 1.06 s / 253 MB | 1.09 s / 254 MB | +30 ms | 1.74 s / 419 MB |
| RISCV | 1.61 s / 417 MB | 1.63 s / 418 MB | +20 ms | 2.57 s / 586 MB |
| ARM | 0.41 s / 106 MB | 0.41 s / 106 MB | +0–10 ms | 0.63 s / 149 MB |

> 数字会随机器、构建类型（assertions 开关）浮动 5%–10%。**关键是三列之间的关系**，不是小数点后两位。用同一台机器、同一个二进制、同一个输入对比才有意义——这正是上面强调「同一个二进制」的原因。

结论：**你的 backend 只占 2%–3%**。`-gen-isa-stats` 的全部成本几乎都是 parse。这跟 Ch 17 的 `-gen-instr-info`（10.19 s，其中 80% 是 `CodeGenSchedModels`）形成鲜明对照——**重 backend 的重量来自「它额外构造的分析结构」，不来自「它遍历多少记录」**。你这里遍历 9,344 条记录 + 60 万个字段，只要 15 毫秒。

`--time-phases` 的对照更直观：

```console
$ llvm-tblgen -gen-isa-stats --time-phases AArch64.td
  Total Execution Time: 0.5871 seconds
  0.5721 ( 97.4%)  Parse, build records
  0.0150 (  2.6%)  Backend overall
$ llvm-tblgen -print-records --time-phases AArch64.td
  Total Execution Time: 0.9649 seconds
  0.5771 ( 59.8%)  Parse, build records
  0.3878 ( 40.2%)  Backend overall
```

同一个二进制、同一个输入，`Backend overall` 从 15 ms 变 388 ms——**这就是「打印 63 MB」的价格**。注意两个 `Parse` 值几乎相同（0.572 vs 0.577），说明 parse 不受 backend 影响，这也从侧面验证了 TableGen 的分层：解析一次，任意多个 backend 复用同一份 `RecordKeeper`。

---

## 完整参考实现

<details>
<summary>点开：`ISAstatsEmitter.cpp` 全文（就是 Step 2 的代码，加上文件头注释）</summary>

```cpp
//===- ISAstatsEmitter.cpp - print statistics about the instruction set ---===//
//
// Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//
//
// This backend prints a summary of the instruction set described by a
// target's TableGen files: how many instructions there are, how they are
// distributed by operand count, and which instructional flags are set.
//
//===----------------------------------------------------------------------===//

#include "llvm/ADT/StringMap.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TableGen/Record.h"
#include "llvm/TableGen/TableGenBackend.h"
#include <algorithm>
#include <map>
#include <vector>

using namespace llvm;

namespace {
class ISAstatsEmitter {
  const RecordKeeper &Records;

  static unsigned listSize(const Record &R, StringRef Field) {
    const RecordVal *V = R.getValue(Field);
    if (!V) return 0;
    if (isa<UnsetInit>(V->getValue())) return 0;
    if (const auto *L = dyn_cast<ListInit>(V->getValue())) return L->size();
    if (const auto *D = dyn_cast<DagInit>(V->getValue())) return D->getNumArgs();
    return 0;
  }

  static bool bit(const Record &R, StringRef Field) {
    const RecordVal *V = R.getValue(Field);
    if (!V) return false;
    if (isa<UnsetInit>(V->getValue())) return false;
    return R.getValueAsBit(Field);
  }

public:
  explicit ISAstatsEmitter(const RecordKeeper &RK) : Records(RK) {}
  void run(raw_ostream &OS);
};
} // namespace

void ISAstatsEmitter::run(raw_ostream &OS) {
  std::vector<const Record *> Instrs =
      Records.getAllDerivedDefinitions("Instruction");
  std::sort(Instrs.begin(), Instrs.end(), [](const Record *A, const Record *B) {
    return A->getName() < B->getName();
  });
  // ... (body as in Step 2)
}

static void emitISAstats(const RecordKeeper &Records, raw_ostream &OS) {
  ISAstatsEmitter(Records).run(OS);
}

static TableGen::Emitter::Opt
    X("gen-isa-stats", emitISAstats,
      "Print instruction-set statistics");
```
</details>

---

## 卡点提示

**卡点 1：`Record 'ABSWr', field 'OutOperandList' exists but does not have a list value`。**
`OutOperandList` 是 `dag` 不是 `list`。用 `DagInit::getNumArgs()`（见 Step 1）。

**卡点 2：`TableGenMain(argv[0])` 报 `ambiguous`。**
两个重载都有默认参数。写 `TableGenMain(argv[0], MultiFileTableGenMainFn())`。

**卡点 3：`--help` 里没有 `--gen-isa-stats`。**
三选一：文件没进 `add_tablegen(llvm-tblgen ...)` 的列表；你编的是 `llvm-min-tblgen`（它不链 `TableGenCommon`，也不该带这个 backend）；你忘了重跑 `cmake`（`CMakeLists.txt` 改了要 reconfigure，`ninja` 不会自动读源列表）。

**卡点 4：数字和本章不一致。**
先确认 include 路径（三个 `-I`），再确认你用的是**同一个 build**（assertions 开/关会影响解析出的 records 吗？不会，但不同 commit 会）。最后确认 `getAllDerivedDefinitions` 的类名——是 `"Instruction"` 不是 `"InstrInfo"`。

**卡点 5：`FileCheck` 说 `no match found in search range` 但你肉眼看输出一样。**
`CHECK-NEXT` 在抓顺序或**中间插了一行空行**。把期望的 `CHECK-NEXT:` 改成 `CHECK:` 定位问题，再逐步收紧。

**卡点 6：自己搭 driver 调用强类型 getter，程序段错误而不是报错。**
这就是 Step 1 结尾那个坑：`TableGenParseFile` 把 buffer 从全局 `SrcMgr` 拿走了，之后任何带 `SMLoc` 的诊断都会崩。用 `TableGenMain`。

---

## 加分挑战

**挑战 1：改成按 namespace 分组。** 现在 `instructions` 把 `TargetOpcode`（335 条通用指令）也算进去了。加一列 `namespace` 分组，你会发现 AArch64 的 9,344 = 335 (`TargetOpcode`) + 9,005 (`AArch64`) + 4（匿名、无 namespace 的 `TargetSpecializedStandardPseudoInstruction`）。后者正好解释 Ch 16 里 `AArch64GenInstrInfo.inc` 的 enum 只有 **9,340** 项：`CodeGenTarget::ComputeInstrsByEnum`（`CodeGenTarget.cpp:302-330`）会跳过那 4 条。

**挑战 2：统计 encoding 位宽。** 每条指令的 `Inst` 字段（`bits<32>`）里有多少位被 `1`/`0` 固定、多少位是 `?`。AArch64 上你会得到「平均每条指令固定多少位」——这是汇编器/反汇编器表压缩的直接输入，也是 Ch 10 讲过的 `bits`/`field` 的真实用途。

**挑战 3：输出 JSON。** 加 `-gen-isa-stats-json`，用 `llvm::json::OStream`（`llvm/Support/JSON.h`）输出。这样你能拿它跟 `-dump-json`（`JSONBackend.cpp`）的输出对照，验证两套序列化对同一个 `RecordKeeper` 的理解一致。

**挑战 4（真·上游可提交）：给 `-print-detailed-records` 加 `--stats`。**
`DetailedRecordsBackend.cpp` 已经有 `printAllocationStats`。把本章的操作数分布加进它的报告，是那种「40 行、零风险、reviewer 很难拒绝」的 PR。前提是你能说清「为什么这个统计对所有 target 都有意义」——**这就是 Ch 22 要教的事**。

---

## 完成后你应该能回答

1. `OutOperandList` 为什么是 `dag` 而不是 `list<dag>`？这个选择对 backend 作者意味着什么？
2. `getAllDerivedDefinitions("Instruction")` 为什么不会返回 class？它是怎么做到只遍历 def 的（提示：`DefsByDirectSuperClass`）？
3. RISCV 有 81% 的伪指令、X86 有 37% 的 `mayLoad` 指令——这两个数字各自反映了什么设计现实？
4. 为什么 `-gen-isa-stats` 只要 10–40 ms，而 `-gen-instr-info` 要 10 s？用「遍历多少记录」解释不通，用什么解释通？
5. `static TableGen::Emitter::Opt X(...)` 这一行为什么不需要在 `main` 里注册任何东西？如果这个 `.cpp` 没被链进 `llvm-tblgen`，会发生什么？

---

## 延伸阅读

- **`llvm/utils/TableGen/InstrDocsEmitter.cpp`（全文 230 行）**
  - 和本章最像的官方 backend：一个文件、一个类、一个 `Opt`、输出纯文本报告。对照它的 `run()` 与本章的 `run()`，看官方怎么组织「多段输出」（它用 `EmitDocs`/`printInstr` 两段）。
- **`llvm/include/llvm/TableGen/TableGenBackend.h`（1–100 行）**
  - `FnT` 的双指针设计（`SingleFileGenerator` / `MultiFileGenerator`）、`OptClass<EmitterC>` 的模板适配、以及 `Opt` 的 `ByDefault` 语义。本章 Step 3 的「为什么用裸 `Opt`」就出自这里。
- **`llvm/lib/TableGen/Record.cpp` 的 3244–3285 行（`getAllDerivedDefinitions` 两个重载）**
  - 第一个重载走 `DefsByDirectSuperClass` 索引 + `SubClassOfCache`，第二个（`IfDefined`）只是先查 `getClass`。读它你会明白为什么「遍历 class 的子类」在 TableGen 里是 O(defs) 而不是 O(defs × classes)。
- **`llvm/lib/TableGen/Error.cpp`（全文 190 行）+ `Parser.cpp` 的 20–60 行**
  - `PrintFatalError` → `fatal_exit()` 的路径，以及 `TableGenParseFile` 为什么会在返回前把 `SrcMgr` 清空。Step 1 结尾那个段错误的完整解释在这两个文件里。
- **`llvm/test/TableGen/detailed-records.td`（全文 60 行）**
  - 官方怎么给「报告型 backend」写测试：`CHECK` 段头、`CHECK-NEXT` 打模板参数、`CHECK` 抓字段。你的 `ISAstats.td` 应该照这个密度写。
- **`llvm/utils/TableGen/CMakeLists.txt`（1–60 行）**
  - `llvm-min-tblgen` 与 `llvm-tblgen` 的源列表分工。看懂这一段，你就不会把新 backend 加错地方，也不会被 `TableGenCommon` 的循环依赖咬到。

---

## 下一章

两个练习都做完了：你能复刻一个 backend 的输出（B），也能写出并注册一个新 backend（D）。接下来进入 Part 4——**改语言本身**。Ch 18 会走通 lexer → parser → `Init` → 记录 → backend 的全链路，给 TableGen 加一条真正的新语法。

[Ch 18 加一条语言特性：从语法到 `Init` 的全链路](./18-adding-a-language-feature.md)
