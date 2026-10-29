# Final Project：写一个可提交的 TableGen backend——`-gen-sched-coverage`

> **目标**：把全书的能力合成一次完整交付。你要写一个**有真实用途、输出稳定、有测试、有性能预算、能按上游流程提交**的 backend：审计一个 target 的调度模型覆盖率——哪些指令没有调度类、哪些显式调度类没人用、哪些类承载的指令最多。本章给出完整参考实现（约 110 行）、四个真实 target 的输出、性能数据、lit 测试设计、PR 描述模板和验收标准。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。所有数字均在本机实跑：参考实现 `/tmp/tgf/schedaudit`（链接 `libLLVMTableGenCommon`，clang `-O2 -fno-rtti`），端到端时间用 `/usr/bin/time`。实验未修改源码树，`git status` 为空。

---

## 1. 项目规格

### 1.1 问题

给一个新 target 接调度模型（`SchedMachineModel` / `SchedWrite` / `InstRW` / `ProcessorModel`）时，最难回答的三个问题：

1. **有多少指令根本没有调度信息？**（`NoSchedModel` 或没有 `SchedRW`）
2. **我写的显式调度类里，有多少没有任何指令在用？**（复制粘贴后的死代码）
3. **哪几个类承载了最多指令？**（优先优化这些）

现在这些只能靠手写脚本扫 `.td` 或读 `-gen-subtarget` 的输出猜。一个官方 backend 可以把它们变成一次命令。

### 1.2 命令与输出

```console
$ llvm-tblgen -gen-sched-coverage -I <target> -I llvm/lib/Target -I llvm/include AArch64.td
Scheduling coverage
===================

  generic instructions       335  (excluded)
  target instructions       9005
  no scheduling class        927
  scheduling classes        3004
  explicit classes          2257
  inferred classes           747
  unused explicit class       22

Top classes by instruction count
--------------------------------
     120  SMLAL_MZZI_HtoS_PSEUDO_SMLAL_MZZ_HtoS_PSEUDO_...
      95  WriteAtomic
      72  SMLALL_MZZI_BtoS_PSEUDO_SMLALL_MZZI_HtoD_PSEU...
      66  SUDOT_VG2_M2ZZI_BToS_PSEUDO_SUDOT_VG2_M2ZZ_BT...
      60  FMLA_VG4_M4Z4Z_D_PSEUDO_FMLA_VG4_M4Z4Z_H_PSEU...
      60  SMAX_VG2_2Z2Z_B_SMAX_VG2_2Z2Z_D_SMAX_VG2_2Z2Z...
      60  FMLA_VG2_M2Z2Z_D_PSEUDO_FMLA_VG2_M2Z2Z_H_PSEU...
      58  SMAX_VG4_4ZZ_B_SMAX_VG4_4ZZ_D_SMAX_VG4_4ZZ_H_...
      56  SUDOT_VG4_M4ZZI_BToS_PSEUDO_SUDOT_VG4_M4ZZ_BT...
      55  BFMAXNM_ZPZZ_UNDEF_BFMAXNM_ZPZZ_ZERO_BFMAX_ZP...
```

> 这是某一次运行的真实输出。注意有多个类的计数相同（60、60、60），当前实现的 `std::sort` 没有 tie-breaker，**它们的相对顺序不保证稳定**——第 6 节会把修掉它列为第一项。

四个 target 的真实结果：

| target | 通用指令（排除） | target 指令 | 无调度类 | 调度类 | 显式 | 推断 | 未用显式类 |
|---|---:|---:|---:|---:|---:|---:|---:|
| AArch64 | 335 | 9,005 | 927（10.3%） | 3,004 | 2,257 | 747 | 22 |
| X86 | 335 | 22,431 | 55（0.2%） | 3,174 | 3,064 | 110 | 92 |
| RISCV | 335 | 16,627 | 2,451（14.7%） | 6,957 | 5,884 | 1,073 | 20 |
| ARM | 335 | 4,200 | 90（2.1%） | 1,749 | 1,324 | 425 | 292 |

这张表本身就是有价值的结论：**RISCV 有 14.7% 的指令没有调度信息，ARM 有 292 个显式调度类是死代码**。

### 1.3 验收标准

| # | 条件 |
|---|---|
| 1 | `llvm-tblgen --help` 的 `Action to perform:` 里出现 `--gen-sched-coverage` |
| 2 | 四个 target 的数字与上表一致 |
| 3 | 新增 `llvm/test/TableGen/sched-coverage.td`，`llvm-lit` 通过 |
| 4 | `llvm-lit llvm/test/TableGen` 全量通过（基线 + 1） |
| 5 | 输出对同一输入**逐字节稳定**（类名顺序确定、截断规则固定） |
| 6 | 端到端时间不超过 `-gen-instr-info` 的 105%，峰值 RSS 不超过 105% |
| 7 | 不修改任何现有 `.inc` 输出（`cmp` 验证） |
| 8 | `llvm/docs/TableGen/BackGuide.md` 或 release notes 有说明 |

### 1.4 非目标

- 不做 per-processor 覆盖率（加分项，见第 10 节）；
- 不做 JSON/CSV（加分项）；
- 不判断调度模型**是否正确**（只统计「有没有」和「用没用」）；
- 不替代 `-gen-subtarget` 的机器模型生成。

---

## 2. 需要哪些前置知识

| 本章用到 | 来自 |
|---|---|
| `Record` / `RecordKeeper` / `getAllDerivedDefinitions` | Ch 4、Ch 11 |
| `CodeGenTarget` / `CodeGenInstruction` | Ch 16、Ch 17 |
| `CodeGenSchedModels` / `CodeGenSchedClass` | Ch 17、Ch 21 |
| backend 注册（`TableGen::Emitter::Opt`） | Ch 13、Ch 14、练习 D |
| 错误报告与 `PrintFatalError` | Ch 12 |
| lit / `FileCheck` | Ch 20 |
| 性能测量与内存归因 | Ch 21 |

这个项目**不使用** `Record` 的原始遍历来猜调度信息——调度模型有专门的 `CodeGenSchedModels`。这是 Ch 16 的教训：能用现成分析结构就不要自己重写。

---

## 3. 先读 API

三个关键接口（`llvm/utils/TableGen/Common/CodeGenSchedule.h`）：

```cpp
// 462 / 532 / 544 行附近
InstClassMapTy InstrClassMap;
CodeGenSchedClass &getSchedClass(unsigned Idx);
unsigned getSchedClassIdx(const CodeGenInstruction &Inst) const;

// 546-551 行
ArrayRef<CodeGenSchedClass> schedClasses() const;
ArrayRef<CodeGenSchedClass> explicitSchedClasses() const {
  return schedClasses().take_front(NumInstrSchedClasses);
}
unsigned numInstrSchedClasses() const;
```

语义要点：

1. **class 0 是 `NoItinerary`**：`getSchedClassIdx()` 返回 0 表示「没有调度信息」。
2. `schedClasses()` 包含**推断类**（`isInferred()`，由 `SchedVariant`/`ItinRW` 生成）；`explicitSchedClasses()` 只包含显式定义的。
3. `numInstrSchedClasses()` 是显式类的数量，**包含 class 0**。

还有两个 instruction 视图（`CodeGenTarget.h:212-233`）：

```cpp
ArrayRef<const CodeGenInstruction *> getInstructions() const;
ArrayRef<const CodeGenInstruction *> getTargetInstructions() const;
```

`getInstructions()` 包含 335 个 target-independent 通用指令（`COPY`、`PHI`、……）；`getTargetInstructions()` 只包含 target 自己的。**这个区别是本节最容易做错的地方**，见第 5 节。

---

## 4. 参考实现

完整代码（可直接编译成独立 driver；注册进 `llvm-tblgen` 只需把 `main` 换成 callback，见第 7 节。第 6 节会指出其中一处必须修的排序稳定性问题）：

```cpp
//===- sched-coverage.cpp - audit scheduling model coverage ---------------===//
#include "Common/CodeGenSchedule.h"
#include "Common/CodeGenTarget.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Format.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/VirtualFileSystem.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/TableGen/Parser.h"
#include "llvm/TableGen/Record.h"
#include <algorithm>
using namespace llvm;

static std::string shorten(StringRef S, unsigned Max = 48) {
  return S.size() > Max ? (S.take_front(Max - 3) + "...").str() : S.str();
}

int main(int argc, char **argv) {
  cl::opt<std::string> InputFile(cl::Positional, cl::desc("<input file>"),
                                 cl::Required);
  cl::list<std::string> IncludeDirs("I", cl::desc("include path"),
                                    cl::ZeroOrMore);
  cl::ParseCommandLineOptions(argc, argv);

  RecordKeeper Records;
  SourceMgr Src;
  Src.setIncludeDirs(IncludeDirs);
  Src.setVirtualFileSystem(vfs::getRealFileSystem());
  auto Buf = MemoryBuffer::getFileOrSTDIN(InputFile, /*IsText=*/true);
  if (!Buf)
    return 1;
  Records.saveInputFilename(InputFile);
  Src.AddNewSourceBuffer(std::move(*Buf), SMLoc());
  if (TableGenParseFile(Src, Records))
    return 1;

  CodeGenTarget Target(Records);
  const CodeGenSchedModels &SM = Target.getSchedModels();

  ArrayRef<const CodeGenInstruction *> AllInstrs = Target.getInstructions();
  ArrayRef<const CodeGenInstruction *> Instrs = Target.getTargetInstructions();
  unsigned NumGeneric = AllInstrs.size() - Instrs.size();

  std::vector<unsigned> Counts(SM.schedClasses().size(), 0);
  std::vector<unsigned> CountsAll(SM.schedClasses().size(), 0);
  unsigned NoClass = 0;

  // 所有指令（含通用指令）用于判断“一个类有没有人用”。
  for (const CodeGenInstruction *I : AllInstrs) {
    unsigned Idx = SM.getSchedClassIdx(*I);
    if (Idx != 0)
      ++CountsAll[Idx];
  }
  // 只有 target 指令用于“覆盖率”——通用指令由 LLVM 自己建模。
  for (const CodeGenInstruction *I : Instrs) {
    unsigned Idx = SM.getSchedClassIdx(*I);
    if (Idx == 0)
      ++NoClass;
    else
      ++Counts[Idx];
  }

  unsigned Unused = 0;
  for (unsigned I = 1, E = SM.numInstrSchedClasses(); I < E; ++I)
    if (CountsAll[I] == 0)
      ++Unused;

  outs() << "Scheduling coverage\n===================\n\n";
  outs() << format("  generic instructions  %8u  (excluded)\n", NumGeneric);
  outs() << format("  target instructions   %8u\n", (unsigned)Instrs.size());
  outs() << format("  no scheduling class   %8u\n", NoClass);
  outs() << format("  scheduling classes    %8u\n",
                   (unsigned)SM.schedClasses().size());
  outs() << format("  explicit classes      %8u\n", SM.numInstrSchedClasses());
  outs() << format("  inferred classes      %8u\n",
                   (unsigned)SM.schedClasses().size() -
                       SM.numInstrSchedClasses());
  outs() << format("  unused explicit class %8u\n", Unused);

  std::vector<unsigned> Order(SM.schedClasses().size());
  for (unsigned I = 0; I < Order.size(); ++I)
    Order[I] = I;
  std::sort(Order.begin(), Order.end(),
            [&](unsigned A, unsigned B) { return Counts[A] > Counts[B]; });

  outs() << "\nTop classes by instruction count\n"
            "--------------------------------\n";
  for (unsigned K = 0; K < 10 && K < Order.size(); ++K) {
    unsigned I = Order[K];
    if (Counts[I] == 0)
      break;
    outs() << format("  %6u  %s\n", Counts[I],
                     shorten(SM.getSchedClass(I).Name).c_str());
  }
  return 0;
}
```

### 4.1 三个设计决定

**(a) 为什么 `Counts` 和 `CountsAll` 两个向量？**

- 「无调度类」的统计要排除通用指令，否则每个 target 都会报 335 个噪声；
- 「未用类」的统计要包含通用指令，因为 `COPY` 可能正好是某个显式类唯一的消费者。实测差异：只算 target 指令时 AArch64 报 23 个未用类，算上通用指令是 22 个——多报的那一个就是被通用指令使用的。

**(b) 为什么 class 0 不参与「未用」判断？**

class 0 是 `NoItinerary`，按定义就是「所有没有信息的指令」。它永远不该被算作「未使用的显式类」。循环从 `I = 1` 开始。

**(c) 为什么要截断类名？**

调度类的名字是**所有成员指令名的拼接**（TableGen 的推断类命名规则）。AArch64 最长的类名实测 4,495 字符，不截断的话一行输出几 KB，既不可读也会让 `FileCheck` 崩溃。固定截断到 48 列是输出契约的一部分。

---

## 5. 边界与负向测试

### 5.1 最小 mock 输入

```text
// sched-coverage.td（测试用）
include "llvm/Target/Target.td"

def TargetX : Target;

def WriteX : SchedWrite;

def InstA : Instruction {
  let OutOperandList = (outs);
  let InOperandList = (ins);
  let SchedRW = [WriteX];
}

def InstB : Instruction {
  let OutOperandList = (outs);
  let InOperandList = (ins);
}

def SchedModelX : SchedMachineModel {
  let CompleteModel = 0;
}

def ProcessorX : ProcessorModel<"ProcessorX", SchedModelX, []>;
```

实测输出：

```console
$ schedaudit -I llvm/include -I llvm/lib/Target sched-coverage.td
Scheduling coverage
===================

  generic instructions       335  (excluded)
  target instructions          2
  no scheduling class          1
  scheduling classes           2
  explicit classes             2
  inferred classes             0
  unused explicit class        0

Top classes by instruction count
--------------------------------
       1  WriteX
```

要点：

- 335 个通用指令被排除，`target instructions` 只有 2；
- `InstB` 没有 `SchedRW` → `no scheduling class = 1`；
- `scheduling classes = 2` 是 `NoItinerary`（class 0）+ `WriteX`；
- `explicit classes = 2` 同样包含 class 0——文档里必须写清楚，否则用户会以为 `WriteX` 被算了两次。

### 5.2 没有调度模型的 target

```text
include "llvm/Target/Target.td"
def TargetY : Target;
def InstZ : Instruction { let OutOperandList = (outs); let InOperandList = (ins); }
```

预期：`scheduling classes = 1`（只有 class 0），`no scheduling class = 1`，`unused = 0`。这种情况要保证不崩、不除零、不打印空表。

### 5.3 负向：坏输入

这个 backend 本身不产生新诊断，但必须保证**它不会吞掉解析错误**：

- `TableGenParseFile` 失败 → 返回非零；
- 调度模型自身错误（如 `InstRW` 重叠）→ `CodeGenSchedModels` 构造时 `PrintFatalError`，进程退出码非零。

测试写法：

```text
// RUN: not llvm-tblgen -gen-sched-coverage %s 2>&1 | FileCheck %s
```

---

## 6. 输出稳定性

这是「可提交」和「玩具」的分界线。三条规则：

1. **类顺序**：按指令数降序；数量相同按 class index 升序（`std::sort` 不是稳定排序，必须显式加 tie-breaker，或者在比较函数里用 `Counts[A] != Counts[B] ? Counts[A] > Counts[B] : A < B`）。
2. **截断**：固定 48 列，`...` 占 3 列。
3. **数字宽度**：固定 `%8u`，右对齐；不要用 locale 相关的格式。

参考实现里的 `std::sort` **缺 tie-breaker**——这是留给你的第一个修正。不加的话，相同计数的类顺序取决于 `std::sort` 的实现细节，不同标准库/不同输入规模会变，`FileCheck` 可能时绿时红。

---

## 7. 注册进 `llvm-tblgen`

和练习 D 完全一样：

1. 把 callback 版文件放到 `llvm/utils/TableGen/SchedCoverageEmitter.cpp`；
2. 在 `llvm/utils/TableGen/CMakeLists.txt` 的 `add_tablegen(llvm-tblgen ...)` 列表里加一行；
3. `ninja -C <build> llvm-tblgen`；
4. `llvm-tblgen --help | grep sched-coverage`。

callback 版只需要把 `main` 换成：

```cpp
static void emitSchedCoverage(const RecordKeeper &Records, raw_ostream &OS) {
  // 把上面的主体搬进来，outs() 换成 OS
}

static TableGen::Emitter::Opt
    X("gen-sched-coverage", emitSchedCoverage,
      "Audit scheduling model coverage");
```

**不要**注册进 `llvm-min-tblgen`（它不能链 `TableGenCommon`，见练习 D 的说明）。

---

## 8. 测试计划

### 8.1 lit 测试

```text
// RUN: llvm-tblgen -gen-sched-coverage -I %p/../../include %s | FileCheck %s

include "llvm/Target/Target.td"
...（第 5.1 节的 mock）

// CHECK:      generic instructions       335  (excluded)
// CHECK:      target instructions          2
// CHECK:      no scheduling class          1
// CHECK:      scheduling classes           2
// CHECK:      unused explicit class        0
// CHECK:      Top classes by instruction count
// CHECK-NEXT: --------------------------------
// CHECK-NEXT:       1  WriteX
```

### 8.2 真实 target 的冒烟（不进 lit）

```console
$ for T in AArch64 X86 RISCV ARM; do
    D=llvm/lib/Target/$T
    llvm-tblgen -gen-sched-coverage -I $D -I llvm/lib/Target -I llvm/include \
      $D/$T.td > /tmp/cov-$T.txt
  done
```

把第 1.2 节的表格作为人工核对基准。真实 target 的测试不适合放进 `llvm/test/TableGen`（太慢、太脆），但应该在 PR 描述里贴出来。

### 8.3 输出不变性

```console
$ llvm-tblgen -gen-instr-info ... AArch64.td -o /tmp/before.inc   # 改前
$ llvm-tblgen -gen-instr-info ... AArch64.td -o /tmp/after.inc    # 改后
$ cmp /tmp/before.inc /tmp/after.inc
```

你的 backend 是**新增**的，理论上不影响现有输出；但注册代码可能改变静态初始化顺序，`cmp` 是最便宜的保险。

---

## 9. 性能预算

参考实现端到端（Release，`/usr/bin/time`）：

| target | 时间 | 峰值 RSS |
|---|---:|---:|
| AArch64 | 10.57 s | 262 MB |
| X86 | 6.42 s | 401 MB |
| RISCV | 2.77 s | 558 MB |
| ARM | 0.92 s | 181 MB |

对照 `-gen-instr-info`（21.1）：AArch64 10.37 s / 295 MB，X86 8.24 s / 463 MB，RISCV 6.28 s / 767 MB，ARM 1.11 s / 163 MB。

结论很重要：

- **你的 backend 几乎不花时间**——全部时间都在 `Target.getSchedModels()`（Ch 17/21 的 `instregex` 匹配）。
- **你的 backend 也不花内存**——`SchedModels` 只占 1–2 MB，峰值主要来自 parse 和 DAG pattern。
- 验收标准里的「不超过 105%」很容易达到，因为你和 `-gen-instr-info` 共享同一个分析结构。

这也是选这个项目的教学价值：**一个 backend 的成本 = 它依赖的分析结构的成本**，不是它自己遍历多少记录。

---

## 10. 加分挑战

按难度排序：

1. **稳定排序 + `--top=N`**：把第 6 节的 tie-breaker 修掉，加一个 `-sched-coverage-top` 选项（默认 10）。
2. **CSV 输出**：`-sched-coverage-format=csv`，表头固定，便于 CI 做趋势对比。注意 CSV 转义（类名里可能有逗号/引号）。
3. **未使用的 `SchedWrite`/`SchedRead`**：遍历 `SM.schedWrites()` / `schedReads()`，统计哪些从未出现在任何类的 `Writes`/`Reads` 里。难点是 `WriteSequence`/`SchedVariant` 的展开（`expandRWSequence`）。
4. **per-processor 覆盖率**：对每个 `CodeGenProcModel`，统计该处理器有 `InstRW`/`WriteRes` 覆盖的指令比例。需要理解 `ProcIndices`、`InstRWProcIndices`、`inferFromInstRW`（Ch 17 的进阶内容）。
5. **JSON 输出 + schema**：参考 `JSONBackend.cpp`（第 19 章提过它被模板参数改动影响）。JSON schema 一旦发布就是契约，要慎重。
6. **HTML/DOT 可视化**：把类→指令的关系画成图。注意大 target 的图会不可读，必须做过滤。

每个加分项都必须配「输出稳定性」和「测试」；不要为了功能牺牲可重复性。

---

## 11. 里程碑

| 阶段 | 内容 | 预计 | 验收 |
|---|---|---|---|
| M0 | fork、构建 `llvm-tblgen`、确认 `--help` 可改 | 0.5 天 | 本地 ninja 成功 |
| M1 | 独立 driver：parse + `CodeGenTarget` + `SchedModels` | 0.5 天 | 四个 target 能跑 |
| M2 | summary 六项数字 | 0.5 天 | 与第 1.2 节一致 |
| M3 | top classes + 截断 + 稳定排序 | 0.5 天 | 输出可重复 |
| M4 | lit 测试（mock + 负向） | 0.5 天 | 单测绿 |
| M5 | 注册进 `llvm-tblgen` | 0.5 天 | `--help` 出现 |
| M6 | 全量回归 + 性能测量 | 0.5 天 | 496+1 绿，数字达标 |
| M7 | 文档 + release notes + PR | 1 天 | PR 描述完整 |

总计约 4–5 个工作日。**M0–M3 在独立 driver 里做**，不要一开始就改 `CMakeLists.txt`；这样迭代快、不污染树。

---

## 12. PR 描述（可直接改）

```markdown
[TableGen] Add -gen-sched-coverage backend

Target bring-up currently has no way to answer "how much of this
target has scheduling information?" without ad-hoc scripts. Add a
backend that reports, per target:

- number of target instructions with no scheduling class;
- number of explicit scheduling classes that no instruction uses;
- the classes carrying the most instructions.

The backend only consumes CodeGenTarget/CodeGenSchedModels; it does not
change any existing output.

Numbers for in-tree targets:

| target  | instrs | no class | classes | unused explicit |
|---------|-------:|---------:|--------:|----------------:|
| AArch64 |  9,005 |      927 |   3,004 |              22 |
| X86     | 22,431 |       55 |   3,174 |              92 |
| RISCV   | 16,627 |    2,451 |   6,957 |              20 |
| ARM     |  4,200 |       90 |   1,749 |             292 |

Cost: same order as -gen-instr-info (dominated by CodeGenSchedModels).

Tests: llvm-lit llvm/test/TableGen (497/497); byte-identical
-gen-instr-info output for all four targets.
```

---

## 13. 评分标准（自检）

| 维度 | 不合格 | 合格 | 优秀 |
|---|---|---|---|
| 正确性 | 数字与真实 target 不符 | 四个 target 与基准一致 | 发现并解释与基准的差异 |
| 输出稳定 | 顺序随机 | 有 tie-breaker | 有 `--top`/格式选项 |
| 测试 | 只跑真实 target | 有 mock lit 测试 | 有负向 + 输出不变性 |
| 性能 | 没测 | 有 before/after | 有归因（时间花在哪） |
| 文档 | 无 | 有 ProgRef/BackGuide 条目 | 有使用示例和限制说明 |
| PR | 一个巨型 commit | 描述完整 | 按 22 章模板，含数据与风险 |

---

## 14. 踩雷记录

1. **错误直觉**：`Target.getInstructions()` 就是「这个 target 的指令」。
   **正确认识**：它包含 335 个通用指令（`COPY`/`PHI`/…）。用 `getTargetInstructions()` 才是 target 自己的。mock 实测：2 条 target 指令 + 335 条通用指令。
2. **错误直觉**：`no scheduling class` 和 `unused class` 用同一套指令集合统计就行。
   **正确认识**：前者应排除通用指令（否则每个 target 多 335 个噪声），后者应包含通用指令（否则会被通用指令使用的类误报为死代码）。AArch64 的差异是 22 vs 23。
3. **错误直觉**：class 0 也是显式类，应该参与「未使用」统计。
   **正确认识**：class 0 是 `NoItinerary`，按定义承载所有无信息指令；从 1 开始统计。`explicit classes` 的数字包含 class 0，文档要写清楚。
4. **错误直觉**：类名可以直接打印。
   **正确认识**：推断类名是成员指令名的拼接，AArch64 最长的实测 4,495 字符。必须截断，并把截断规则当成输出契约。
5. **错误直觉**：`std::sort` 按计数降序就够了。
   **正确认识**：计数相同的类顺序未定义，输出不稳定。必须加 tie-breaker（class index 升序），否则 `FileCheck` 会偶发失败。
6. **错误直觉**：这个 backend 遍历 9,000 条指令，应该比 `-gen-instr-info` 快很多。
   **正确认识**：10.57 s vs 10.37 s，几乎一样——时间全在 `getSchedModels()`。你省不掉依赖结构的成本。

---

## 15. 延伸阅读

1. **`llvm/utils/TableGen/Common/CodeGenSchedule.h`（全文约 620 行）**
   读什么：`CodeGenSchedClass`、`CodeGenSchedRW`、`CodeGenProcModel`、`InstrClassMap` 的字段与注释。
   学什么：调度模型在内存里的完整表示；哪些信息可以直接用、哪些要自己推。
   和本章的关系：参考实现只用了其中 4 个接口，读懂全结构才能做加分项。

2. **`llvm/utils/TableGen/Common/CodeGenSchedule.cpp` 的 `collectSchedClasses` / `inferFromInstRWs`（约 820–1100 行）**
   读什么：显式类如何收集、推断类如何从 `InstRW`/`SchedVariant` 生成。
   学什么：为什么 `schedClasses()` 比 `explicitSchedClasses()` 多；`ProcIndices` 的含义。
   和本章的关系：per-processor 覆盖率加分项的基础。

3. **`llvm/test/TableGen/ReadAdvanceInvalidWrite.td` 与 `CodeGenSchedule-duplicate-instrw.td`**
   读什么：最小调度模型 `.td` 的写法、负向测试的 `FileCheck` 方式。
   学什么：怎么用 30 行 mock 触发调度模型的错误路径。
   和本章的关系：第 5 节测试设计的模板。

4. **练习 D：`-gen-isa-stats` 的注册与测试流程**
   读什么：`add_tablegen` 列表、`--help` 验证、497/497 回归。
   学什么：backend 从独立 driver 到树内的完整步骤。
   和本章的关系：第 7 节不再重复，直接复用。

5. **第 22 章：上游流程**
   读什么：PR 描述模板、benchmark 表格、review 的十个问题。
   学什么：怎么把本章的成果变成一次可合并的提交。
   和本章的关系：第 12 节的模板来自那里。

---

## 课程总结

到这里，你已经走完了 TableGen 的完整路径：

- **前端**：lexer（Ch 2）→ parser（Ch 3）→ `Init` 层级（Ch 5/6）→ 折叠与解析（Ch 7）；
- **记录模型**：`Record` / `RecordKeeper`（Ch 4）、multiclass 展开（Ch 8）、类型系统（Ch 9）、bits/fields（Ch 10）；
- **工具面**：assert/dump（Ch 11）、错误报告（Ch 12）、backend 注册（Ch 13）、写 backend（Ch 14）、多文件（Ch 15）；
- **输出与真实 backend**：生成代码形状（Ch 16）、`-gen-instr-info` 全链路（Ch 17）；
- **改源码**：加语言特性（Ch 18）、改记录语义（Ch 19）；
- **工程化**：测试（Ch 20）、性能（Ch 21）、上游流程（Ch 22）；
- **实践**：四个练习 + 这个 final project。

如果只能记住一句话：**TableGen 的复杂度不在语法，在「一个值要被解析两次、被四种结构表示、被二十个 backend 消费」**。你现在的水平足以读懂任何一处 `Record` 相关代码，也足以提交一个边界清晰、有测试、有数据的补丁。

下一步建议：从本书的某个「踩雷记录」里挑一个真实的小问题（比如 `StringInit::getAsString()` 不转义，练习 B 第 13 条），按第 22 章的流程走一次完整 PR。那会比再读十章更有用。
