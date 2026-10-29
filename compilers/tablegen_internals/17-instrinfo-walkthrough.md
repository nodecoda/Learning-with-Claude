# Ch 17 大型 backend 走读：`InstrInfoEmitter` 与那个「10 秒里的 9 秒」

> **本章目标**：把 `InstrInfoEmitter.cpp`（1,486 行）按「谁调用谁、谁吃时间」拆成三层，并且用**实测的计时数据**推翻一个常见的错误直觉——「`-gen-instr-info` 慢是因为它在生成三万行代码」。真相是：**生成只占 0.8 秒，9.3 秒花在 `CodeGenSchedModels` 的构造上，而其中 8.8 秒是 8,675 条 `instregex` 的字符串匹配**。读完你能定位任何 `-gen-*` 后端的真实热点，并知道为什么 `-time-phases` 会告诉你「只花了 0.77 秒」。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`），`llvm-tblgen` 为 Release 构建（`/home/ubuntu/llvm/llvm-dev/build`）。本章的性能数据全部来自本机 `/usr/bin/time` 与自写的计时探针；探针方式是把 `CodeGenSchedule.cpp` 复制到 `/tmp` 加 `TIMED(...)` 宏后单独编译链接（不改源码树）。AArch64 的 `.td` 全量输入。

---

## 17.1 先看一组会让你误判的数字

在 AArch64 上跑 `-gen-instr-info`，`llvm-tblgen` 自带的分阶段计时器给出这个报告：

```console
$ llvm-tblgen -gen-instr-info -time-phases ... AArch64.td -o /dev/null
  Total Execution Time: 0.7720 seconds (0.7720 wall clock)

   ---User Time---   --System Time--   --User+System--   ---Wall Time---  --- Name ---
   0.4288 ( 66.6%)   0.1007 ( 78.5%)   0.5294 ( 68.6%)   0.5294 ( 68.6%)  Parse, build records
   0.1169 ( 18.2%)   0.0160 ( 12.5%)   0.1329 ( 17.2%)   0.1329 ( 17.2%)  Emit map table
   0.0310 (  4.8%)   0.0040 (  3.1%)   0.0350 (  4.5%)   0.0350 (  4.5%)  Emit InstrDesc records
   0.0276 (  4.3%)   0.0000 (  0.0%)   0.0276 (  3.6%)   0.0276 (  3.6%)  Compute inferred register classes
   0.0184 (  2.9%)   0.0000 ( (  0.0%)   0.0184 (  2.4%)   0.0184 (  2.4%)  Collect operand info
   0.0048 (  0.8%)   0.0040 (  3.1%)   0.0089 (  1.1%)   0.0089 (  1.1%)  Analyze DAG patterns
   0.0057 (  0.9%)   0.0000 (  0.0%)   0.0057 (  0.7%)   0.0057 (  0.7%)  Emit instruction names
   ...
   0.6438 (100.0%)   0.1282 (100.0%)   0.7720 (100.0%)   0.7720 (100.0%)  Total
```

**报告的 Total 是 0.772 秒。实际 wall clock 是 10.19 秒。**

```console
$ /usr/bin/time -f "real=%es maxrss=%MkB" llvm-tblgen -gen-instr-info ... AArch64.td -o /dev/null
real=10.19s maxrss=295332kB
```

**92% 的时间不在报告里**（报告 0.772 s / 实际 10.19 s）。 这不是 bug，是 `TGTimer` 的设计：`startPhaseTiming()` 只在 `-time-phases` 时才创建 `TimingGroup`，而 `startTimer()` 的第一件事是 `if (!TimingGroup) return;`。所有**没有被 `startTimer` 显式包裹的代码**——包括 `InstrInfoEmitter` 构造函数里那两个成员初始化——都不计时。

本章的核心就是把这 9.4 秒（解析之外的分析层）找出来，并且定位到其中 8.1 秒的单一来源。

## 17.2 三层结构：emitter 其实很薄

先看类的形状（`InstrInfoEmitter.cpp:60`）：

```cpp
class InstrInfoEmitter {
  const RecordKeeper &Records;
  const CodeGenDAGPatterns CDP;              // ← 注意：这是值成员
  const CodeGenSchedModels &SchedModels;

public:
  InstrInfoEmitter(const RecordKeeper &R)
      : Records(R), CDP(R, /*ExpandHwMode=*/false),
        SchedModels(CDP.getTargetInfo().getSchedModels()) {}

  void run(raw_ostream &OS);
  ...
};
```

三个成员，两个在构造函数里初始化。`CDP` 是**值成员**——构造 `InstrInfoEmitter` 就构造整个 DAG 模式库。`SchedModels` 是引用，指向 `CDP.getTargetInfo().getSchedModels()` 的返回值，而后者（`CodeGenTarget.cpp:259`）是**惰性构造**：

```cpp
CodeGenSchedModels &CodeGenTarget::getSchedModels() const {
  if (!SchedModels)
    SchedModels = std::make_unique<CodeGenSchedModels>(Records, *this);
  return *SchedModels;
}
```

所以 `InstrInfoEmitter` 的构造函数实际上干了三件大事：

1. `CDP(R, false)` —— 解析全部 DAG pattern、推断指令 flag
2. `CDP.getTargetInfo()` —— 返回已经存在的 `CodeGenTarget`
3. `.getSchedModels()` —— **首次触发 `CodeGenSchedModels` 构造**

按 `InstrInfoEmitter` 的真实构造顺序写一个探针（`/tmp/tg17/h11.cpp`，直接 new 一个 `CodeGenDAGPatterns` 再摸 `getSchedModels()`），实测：

```console
$ /usr/bin/time -f "real=%es rss=%MkB" ./h11 ... AArch64.td
parse                          6.189807e+02 ms
CodeGenDAGPatterns             9.884529e+02 ms
getSchedModels (first touch)   8.110170e+03 ms
sum                            9.717604e+03 ms
real=9.89s rss=345684kB
```

**构造函数 9.10 秒，`run()` 只有 0.24 秒**（见 17.3 的算法）。一个 1,486 行的 emitter，97% 的成本花在它 `#include` 的三个分析类上。

**这就是「大型 backend」的真相**：`InstrInfoEmitter.cpp` 的 1,486 行里，真正「生成」的只有约 900 行，其余是操作数描述构造（`GetOperandInfo`、`CollectOperandInfo`）和四个可选的辅助段。它的复杂度不来自自己，而来自它**依赖的分析层**。

三个层次可以这样分：

| 层 | 代码 | 实测耗时 | 占 10.19 s 的比例 |
| --- | --- | --- | --- |
| 解析层 | `TGParser` + `RecordKeeper` | 0.62 s | 6% |
| 分析层 A | `CodeGenDAGPatterns` 构造 | 0.99 s | 10% |
| 分析层 B | `CodeGenSchedModels` 构造 | 8.11 s | **80%** |
| 生成层 | `InstrInfoEmitter::run()` 里除解析外的部分（0.772 − 0.529） | 0.24 s | 2% |
| 写文件 | `WriteOutput`（3.27 MB） | ~0.02 s | <1% |

四项相加约 9.98 s，与 `real=10.19 s` 相差的 0.2 s 是进程启动、`cl::ParseCommandLineOptions`、以及两个探针各自漏掉的边角。

**注意「生成层」只有 0.24 秒**——这是本课最重要的一个数字：你在 `InstrInfoEmitter.cpp` 里读到的 900 行生成逻辑，跑完只要 240 毫秒。

## 17.3 `run()` 的 13 个阶段

把 `InstrInfoEmitter::run()`（922–1293 行）按 `startTimer` 的调用切成 13 段，每段的实测耗时（AArch64，`-time-phases` 输出，已剔除 <0.0001s 的项）：

| # | timer 名 | 耗时 | 作用 |
| --- | --- | --- | --- |
| 1 | `Analyze DAG patterns` | 0.0089 s | 空壳——真正的分析在构造函数里 |
| 2 | `Collect operand info` | 0.0184 s | 建 `OperandInfo` 去重表 |
| 3 | `Collect uses/defs` | 0.0034 s | 建 implicit 列表去重表 |
| 4 | `Emit InstrDesc records` | 0.0350 s | 9,340 行 `MCInstrDesc` 初始化 |
| 5 | `Emit uses/defs` | ~0 | 103 行 `MCPhysReg` |
| 6 | `Emit operand info` | 0.0002 s | 2,752 个 `MCOperandInfo` |
| 7 | `Emit instruction names` | 0.0057 s | 字符串池 + 索引数组 |
| 8 | `Emit initialization routine` | 0.0010 s | `InitAArch64MCInstrInfo` |
| 9 | `Emit operand name mappings` | 0 | AArch64 不用（`UseNamedOperandTable`=0） |
| 10 | `Emit operand type mappings` | 0 | 只有 X86 用 |
| 11 | `Emit helper methods` | 0.0001 s | `TIIPredicate` 展开 |
| 12 | `Emit verifier methods` | 0.0055 s | `computeRequiredFeatures` |
| 13 | `Emit map table` | 0.1329 s | `-instrmap` 映射表（AArch64 特有） |

**第 1 项是个陷阱**：`Timer.startTimer("Analyze DAG patterns")` 出现在 `run()` 的第一行（924），但它下面紧接着就是 `emitSourceFileHeader` 和 `emitEnums`。真正的 `CodeGenDAGPatterns` 分析在构造函数里，早就跑完了。所以这个 0.0089 秒的「Analyze DAG patterns」只覆盖了「启动 timer 到下一个 timer 启动」之间的那几行代码——**它名字撒谎了**。

## 17.4 定位那 8.1 秒：一个手工探针

`-time-phases` 看不见构造函数，只能自己加探针。做法是：把 `CodeGenSchedule.cpp` 复制到 `/tmp`，在每个 `collectXxx()` 调用外加一个计时的宏，然后单独编译成 `.o` 与探针程序链接。

探针宏（`/tmp/tg17/sched/CodeGenSchedule.cpp`，**这是 `/tmp` 里的副本，源码树未改**）：

```cpp
static double tgNow() {
  return std::chrono::duration<double, std::milli>(
             std::chrono::steady_clock::now().time_since_epoch()).count();
}
#define TIMED(name, call) do { double _t0 = tgNow(); call; \
  fprintf(stderr, "TG-STAGE %-34s %9.2f ms\n", name, tgNow() - _t0); } while (0)
```

用在 `CodeGenSchedModels::CodeGenSchedModels()`（`CodeGenSchedule.cpp:182`）的 13 个阶段上。实测输出：

```text
TG-STAGE collectProcModels                     0.31 ms
TG-STAGE collectSchedRW                       75.86 ms
TG-STAGE collectSchedClasses                7543.32 ms
TG-STAGE collectProcItins                      0.00 ms
TG-STAGE collectProcItinRW                     0.00 ms
TG-STAGE collectProcUnsupportedFeatures        0.02 ms
TG-STAGE inferSchedClasses                   244.94 ms
TG-STAGE collectProcResources                 64.39 ms
TG-STAGE collectOptionalProcessorInfo        364.16 ms
TG-STAGE checkMCInstPredicates                 0.35 ms
TG-STAGE checkSTIPredicates                    0.43 ms
TG-STAGE collectSTIPredicates                  0.21 ms
TG-STAGE checkCompleteness                   357.43 ms
```

**`collectSchedClasses` 一项占 7.54 秒（93%）。** 其余 12 项加起来 0.58 秒。

### 再往下钻：`collectSchedClasses` 内部

`collectSchedClasses()`（829 行）做两件事：

```cpp
// 1. 每条指令 → 一个 sched class（重复的复用）
for (const CodeGenInstruction *Inst : Target.getInstructions()) {
  const Record *ItinDef = Inst->TheDef->getValueAsDef("Itinerary");
  IdxVec Writes, Reads;
  if (!Inst->TheDef->isValueUnset("SchedRW"))
    findRWs(Inst->TheDef->getValueAsListOfDefs("SchedRW"), Writes, Reads);
  unsigned SCIdx = addSchedClass(ItinDef, Writes, Reads, /*ProcIndices*/ {0});
  InstrClassMap[Inst->TheDef] = SCIdx;
}

// 2. 每个 InstRW 记录 → 可能新开 sched class
for (const Record *RWDef : Records.getAllDerivedDefinitions("InstRW"))
  createInstRWClass(RWDef);
```

两段各自计时（同一个探针文件）：

```text
TG per-instruction addSchedClass           14.65 ms
TG createInstRWClass loop                8210.16 ms   (11,532 次)
```

**第一步只花 15 ms，第二步花 8.2 秒。** 而这 8.2 秒里，`createInstRWClass` 的主体（`ClassInstrs` 分组、建新 class、`InstrClassMap` 重映射）只花 **21 ms**：

```text
createInstRWClass rest = 21.0 ms (11532 calls)
```

剩下的 8,190 ms 在哪？

```cpp
// createInstRWClass() 的第一行有效代码（CodeGenSchedule.cpp:1010）
const ConstRecVec *InstDefs = Sets.expand(InstRWDef);
```

**一行调用，8.2 秒。**

## 17.5 罪魁：8,675 条 `instregex`

`Sets.expand()` 是 `SetTheory` 的入口（`lib/TableGen/SetTheory.cpp:308`）。它做的事很朴素：

```cpp
const RecVec *SetTheory::expand(const Record *Set) {
  ExpandMap::iterator I = Expansions.find(Set);
  if (I != Expansions.end())
    return &I->second;   // 缓存命中

  for (const Record *SuperClass : Set->getSuperClasses()) {
    ...
    auto I = Expanders.find(SuperClass->getName());
    if (I == Expanders.end()) continue;
    RecVec &EltVec = Expansions[Set];
    RecSet Elts;
    I->second->expand(*this, Set, Elts);   // ← 真正干活
    EltVec.assign(Elts.begin(), Elts.end());
    return &EltVec;
  }
  return nullptr;
}
```

**缓存是按 `Record*` 做的**。每个 `InstRW` 都是独立的 `Record`，所以 11,532 个记录 = 11,532 次 expand，缓存一次都不命中。

展开器是 `InstRegexOp`（53 行），它的核心循环：

```cpp
// 抽出前缀，二分找到候选区间
auto Range1 = std::equal_range(Pseudos.begin(), Pseudos.end(), Prefix, Comp());
auto Range2 = std::equal_range(NonPseudos.begin(), NonPseudos.end(), Prefix, Comp());
// 对每个候选用正则再筛一遍
const auto HandleNonGeneric = [&](const CodeGenInstruction *Inst) {
  StringRef InstName = Inst->getName();
  if (!Regexpr || Regexpr->match(InstName.substr(Prefix.size()))) { ... }
};
```

看起来有前缀优化（`equal_range`），应该很快。但 AArch64 的 8,675 条 `instregex` 里，绝大多数以 `^` 开头且**紧跟元字符**，前缀是空的。实测统计（探针加在 `InstRegexOp::apply` 里）：

```text
SET-OP apply=12501  instregex=8809.9 ms  instrs=0.0 ms  candidates=38604303  regex-tries=34402760
```

- `apply` 被调用 **12,501** 次
- 累计考察候选指令 **38,604,303** 条
- 累计执行正则匹配 **34,402,760** 次

平均每次 `apply` 考察 3,088 条候选、跑 2,752 次正则。用独立基准测单次正则成本（`/tmp/tg17/re/r.cpp`）：

```console
$ ./r
9340000 regex matches in 6.415173e+02 ms (hits=1868000)
per match = 6.868493e-02 us
```

**每次 `Regex::match` 约 69 纳秒。** 34,402,760 × 69ns ≈ **2.37 秒**纯正则匹配；加上字符串构造（`InstName.substr(Prefix.size())` 每次都建 `StringRef`，不分配但仍有开销）、`RecSet`（`std::set<const Record*>`）插入、`EltVec.assign(Elts.begin(), Elts.end())` 的拷贝，8.2 秒是合理的。

### 为什么 AArch64 这么惨

对比四个架构的同一指标（实测）：

| target | `InstRW` 数 | `instregex` 数 | `-gen-instr-info` | `-gen-subtarget` |
| --- | --- | --- | --- | --- |
| AArch64 | 11,532 | 8,675 | **10.19 s** / 295 MB | **10.32 s** / 224 MB |
| X86 | — | — | 5.31 s / 463 MB | 4.06 s / 306 MB |
| RISCV | — | — | 6.27 s / 767 MB | 2.95 s / 498 MB |
| ARM | — | — | 1.11 s / 163 MB | 0.83 s / 124 MB |

AArch64 的调度模型要为 31 个 microarchitecture 各写一份 `InstRW`（Olympus 1,031 条、A64FX 606 条、C1Nano 583 条……），而每个模型倾向用 `instregex` 而不是逐条列举——于是 8,675 条正则 × 9,340 条指令。

**这不是「backend 慢」，是「模型表达方式 × 实现算法」的乘积问题。**

## 17.6 三个可以立刻验证的推论

### 推论一：换一个不用 sched model 的 backend，同样的输入应该只要 0.6 秒

```console
$ /usr/bin/time -f "real=%es rss=%MkB" llvm-tblgen --null-backend ... AArch64.td -o /dev/null
real=0.59s rss=170908kB
```

**0.59 秒**——只做解析，不构造任何分析层。这证实了 9.4 秒全部来自「构造 sched model」。

对照 `-gen-register-info`：

```console
$ /usr/bin/time -f "real=%es rss=%MkB" llvm-tblgen -gen-register-info ... AArch64.td -o /dev/null
real=0.80s rss=203940kB
```

**0.80 秒**。寄存器后端不需要 sched model，所以它也跳过了那 9.4 秒。**`RegisterInfoEmitter` 是 2,253 行，`InstrInfoEmitter` 是 1,486 行，但后者慢 12 倍**——差别完全不在行数上。

### 推论二：`-gen-instr-info` 和 `-gen-subtarget` 应该耗时几乎相同（都构造 sched model）

```console
$ llvm-tblgen -gen-instr-info ... ;  # 10.19 s
$ llvm-tblgen -gen-subtarget ...  ;  # 10.32 s
```

**差 0.13 秒。** 两个完全不同的 backend（一个 1,486 行、一个 1,232 行），耗时几乎一样，因为 90% 的时间是共同的前置分析。

### 推论三：`-gen-dag-isel` 的 3.32 秒里也应该有 ~0.6 秒是同一个前置

```console
$ llvm-tblgen -gen-dag-isel -time-phases ... AArch64.td
  Total Execution Time: 2.3105 seconds (2.3105 wall clock)
   1.0482 ( 45.4%)  Convert to matchers
   0.5473 ( 23.7%)  Parse, build records
   0.3807 ( 16.5%)  Emit matcher table
   0.1851 (  8.0%)  Sort patterns
   0.0927 (  4.0%)  Optimize matchers
```

注意：**`-gen-dag-isel` 的总耗时报告是 2.31 秒，但 `real` 是 3.32 秒**——差的 1 秒就是没被计时的构造析构。而且它**没有构造 `CodeGenSchedModels`**（`DAGISelMatcherEmitter` 不依赖它），所以那 9.4 秒完全不存在。三个 backend 的对比构成了完整的因果链：

| backend | 需要 sched model | 实测 |
| --- | --- | --- |
| `--null-backend` | 否 | 0.59 s |
| `-gen-register-info` | 否 | 0.80 s |
| `-gen-dag-isel` | 否 | 3.32 s（`Convert to matchers` 主导） |
| `-gen-instr-info` | **是** | **10.19 s** |
| `-gen-subtarget` | **是** | **10.32 s** |

## 17.7 两层缓存，两种收益

看到 `SetTheory::expand` 按 `Record*` 缓存，你的第一反应可能是「按表达式内容缓存」。这个优化是真的、可量化的：

```console
$ ./h10
InstRW total=11532  unique exprs=4401
unique with instregex=3602  (they cover 8675 records)
dedup would cut instregex expansions from 8675 to 3602
```

**8,675 → 3,602，减少 58%。** 我在 `/tmp` 副本上实现了这个缓存（`std::map<std::string, ConstRecVec>`，key 用 `getValue("Instrs")->getValue()->getAsString()`），实测：

```text
expand: miss=3936.9 ms (4401)  hit=0.0 ms (7131)
```

- 4,401 次未命中，累计 **3,937 ms**
- 7,131 次命中，累计 **0.0 ms**
- `createInstRWClass` 循环从 8,210 ms 降到 **4,335 ms**

但**总时间只从 10.19 s 降到 9.3 s**——因为剩下的 3.9 秒仍然是 3,602 条不同正则 × 3,088 条候选。

这说明两件事：

1. **缓存是必要条件，不是充分条件。** 58% 的调用被消掉，但每次调用的成本没变。
2. **真正的瓶颈是 `InstRegexOp` 的算法**：它对每条正则都扫一遍全部指令。34.4M 次正则匹配对应 3.6K 条不同正则 × 9.3K 条指令，这个乘积是本质的。

要再往下压，需要的是**反向索引**：把所有正则的「已知前缀」抽出来，建一张 `前缀 → 指令列表` 的表，让前缀非空的正则只扫自己的桶。上游已经有这个优化（`equal_range` + `Comp`），但它只对**字面前缀**有效；AArch64 的 `".*rs$"`、`"^LDRA[AB](indexed|writeback)"` 这类模式，前缀分别是 `""` 和 `"LDRA"`，前者退化成全扫。

这条路线是有真实上游价值的（Ch 22 会讲怎么把它变成可提交的 PR），但门槛比「加个缓存」高得多：你必须证明**结果完全一致**（正则语义、`RecSet` 的排序、错误消息里的 `instregex has no matches`），否则会静默改变 31 个 CPU 模型的调度表。

## 17.8 内存：另一个被忽略的维度

耗时之外，峰值 RSS 也值得记（`/usr/bin/time -f %M`）：

| target | backend | peak RSS |
| --- | --- | --- |
| AArch64 | `--null-backend` | 171 MB |
| AArch64 | `-gen-register-info` | 204 MB |
| AArch64 | `-gen-instr-info` | **295 MB** |
| X86 | `-gen-instr-info` | 463 MB |
| RISCV | `-gen-instr-info` | **767 MB** |
| ARM | `-gen-instr-info` | 163 MB |

RISCV 的 767 MB 很反常（它的指令数比 AArch64 少）。原因是 `CodeGenDAGPatterns` 会为每个 pattern 生成**变体**（`GenerateVariants()`，处理可交换操作数、类型推断），而 RISCV 的 pattern 数量与类型组合都很大。这一项在 `-gen-register-info` 里完全不存在，这也是为什么后者只要 204 MB。

**给 CI 的实用含义**：如果你的构建机内存紧，`llvm-tblgen -gen-instr-info` 是最容易 OOM 的一步，而它同时又是最慢的一步——调大 `-j` 会让多个 target 同时跑这一步，峰值叠加。这就是上游 CMake 里那些 `set_source_files_properties(... COMPILE_FLAGS ...)` 之类限制并发的来源之一。

## 17.9 读大 backend 的四步法

回到「怎么读这 1,486 行」。我用的是这个顺序，每一步都有明确产出：

**第一步：读构造函数和三行成员。**

```cpp
class InstrInfoEmitter {
  const RecordKeeper &Records;
  const CodeGenDAGPatterns CDP;
  const CodeGenSchedModels &SchedModels;
```

三行就暴露了全部依赖。**如果构造函数里有值成员，那些成员构造的开销一定不在 `run()` 的 timer 里。** 这是查性能的第一现场。

**第二步：列出 `run()` 里的 timer 名，按顺序读。**

```console
$ grep -n 'startTimer' llvm/utils/TableGen/InstrInfoEmitter.cpp
924:  Timer.startTimer("Analyze DAG patterns");
938:  Timer.startTimer("Collect operand info");
953:  Timer.startTimer("Collect uses/defs");
1016:    Timer.startTimer("Emit InstrDesc records");
1043:    Timer.startTimer("Emit uses/defs");
1059:    Timer.startTimer("Emit operand info");
1065:    Timer.startTimer("Emit instruction names");
1112:    Timer.startTimer("Emit initialization routine");
1273:    Timer.startTimer("Emit operand name mappings");
1277:  Timer.startTimer("Emit operand type mappings");
1281:    Timer.startTimer("Emit logical operand size mappings");
1285:    Timer.startTimer("Emit helper methods");
1288:  Timer.startTimer("Emit verifier methods");
1291:  Timer.startTimer("Emit map table");
```

14 个 timer（比 17.3 的表多一个 `Emit logical operand size mappings`，AArch64 不触发）。**这份清单就是 `run()` 的目录**——每一行对应一个 `{ ... }` 块，块里就是要读的代码。

**第三步：只读「hot 块」的细节。**

AArch64 上 hot 的只有三块：`Emit InstrDesc records`（35ms）、`Emit map table`（133ms）、`Collect operand info`（18ms）。其余 11 块加起来 20ms。**别平均用力。**

**第四步：对每个 hot 块问「它的输入是什么」。**

`Emit map table` 的输入是 `Records` 里的 `InstrMapping` 记录；`Emit InstrDesc records` 的输入是 `OperandInfoMap`；`Collect operand info` 的输入是 `Inst.Operands`。**顺着输入往回走**，就会走到 `CodeGenTarget` 和 `CodeGenSchedModels`——也就是 9.4 秒的所在。

## 17.10 检查清单

改动或排查 `InstrInfoEmitter` 之前：

- [ ] 先看构造函数。任何值成员（`CodeGenDAGPatterns CDP`）的构造开销都不在 `-time-phases` 里
- [ ] 用 `--null-backend` 测同输入的「解析底价」，任何 backend 的耗时都该减去它
- [ ] 用 `/usr/bin/time -v` 看 peak RSS，别只看时间
- [ ] 改 `run()` 里任何 `startTimer` 之间的代码时，确认新代码落在某个 timer 覆盖范围内（否则它会「消失」）
- [ ] 如果改了 `CodeGenSchedModels`，在所有有 `InstRW` 的 target 上跑（AArch64 8,675 条、X86 数百条），结果必须逐字节一致
- [ ] 如果加缓存，key 必须覆盖**全部**影响展开结果的输入（`InstRW` 的 `Instrs` 字段、`SchedModel`、`FullInstRWOverlapCheck`），而不只是表达式字符串
- [ ] 如果改 `InstRegexOp`，跑 `llvm/test/TableGen/` 里全部 `instregex` 相关测试（`InstRegex` 前缀有 20+ 个 `.td`）
- [ ] 性能结论必须有 `real` 数据，不能只看 `-time-phases` 的 Total

## 17.11 踩雷记录

**错误直觉 1：`-time-phases` 的 `Total` 是进程总耗时。**
正确认识：它是**所有 `startTimer` 覆盖区间之和**，不含构造函数、析构函数、`main` 里的初始化。实测 AArch64 `-gen-instr-info`：报告 0.772 s，`real` 10.19 s，**漏掉 92%**。判据：报告值 ≈ `--null-backend` 的耗时 + `run()` 里的生成开销时，说明构造函数被漏掉了。

**错误直觉 2：`InstrInfoEmitter` 慢是因为它要生成 30,000 行代码。**
正确认识：生成层只花 **0.24 s**（`-time-phases` 的 0.772 s 里有 0.529 s 是解析，已单列）。慢的是**构造 sched model**（8.11 s），而这跟生成多少行无关——`-gen-subtarget` 生成的东西完全不同，耗时却是 10.32 s。

**错误直觉 3：`Timer.startTimer("Analyze DAG patterns")` 计的是 DAG 分析。**
正确认识：`InstrInfoEmitter::run()` 第一行就 `startTimer("Analyze DAG patterns")`，但 `CodeGenDAGPatterns` 在**构造函数**里就建完了。这个 timer 实测只有 8.9 ms，它名不副实——真正的分析成本被算进了「两个 timer 之间的空隙」，也就是不被任何 timer 记录。这类「timer 名字撒谎」是读性能报告时最常见的坑。

**错误直觉 4：`SetTheory::expand` 有缓存，所以重复的 `InstRW` 不会有成本。**
正确认识：缓存 key 是 `Record*`（`Expansions.find(Set)`）。每个 `InstRW` 都是独立记录，11,532 个记录 = 11,532 次未命中。要按**表达式内容**缓存才有效——实测能省 58% 的调用（8,675 → 3,602），耗时从 8.2 s 降到 4.3 s。

**错误直觉 5：`instregex` 有前缀优化，`equal_range` 会把它压到 O(log n)。**
正确认识：只有**字面前缀非空**时才有效。AArch64 大量模式是 `".*rs$"`（前缀 `""`）、`"^DUPv.+gpr"`（前缀 `"DUPv"`）。实测 12,501 次 `apply` 累计考察 **38.6M 条候选**、执行 **34.4M 次正则匹配**。前缀为空时退化成全表扫描。

**错误直觉 6：`-gen-register-info` 比 `-gen-instr-info` 简单，所以快。**
正确认识：`RegisterInfoEmitter` 是 2,253 行（比 1,486 行更多），生成 7.7 MB 的 `TargetDesc.inc`。但它只要 0.80 s——因为它**不需要 sched model**。行数和耗时没有关系，依赖图才有。

**错误直觉 7：同一个 `.td` 输入，不同 backend 的耗时差异来自各自算法。**
正确认识：`-gen-instr-info`（10.19 s）与 `-gen-subtarget`（10.32 s）几乎相同，因为约 80% 是共同的 `CodeGenSchedModels` 构造。真正的差异只在最后的生成段。

**错误直觉 8：peak RSS 和耗时正相关。**
正确认识：RISCV `-gen-instr-info` 用 767 MB / 6.27 s，AArch64 用 295 MB / 10.19 s。内存由 `CodeGenDAGPatterns` 的 pattern 变体数量决定，时间由 `CodeGenSchedModels` 的 `instregex` 数量决定——两个正交维度。

## 17.12 延伸阅读

- **`llvm/utils/TableGen/InstrInfoEmitter.cpp` 的 922–1293 行（`run()`）**
  - 本章 17.3、17.9 的全部依据。重点看每个 `Timer.startTimer` 之后紧跟的第一个 `{`——那就是它的作用域边界；以及 1273–1291 那五个条件段（`HasUseNamedOperandTable` / X86 专用 / `HasUseLogicalOperandMappings`）。
- **`llvm/utils/TableGen/Common/CodeGenSchedule.cpp` 的 182–250 行（构造函数）**
  - 13 个 `collectXxx()` 的调用顺序。这个顺序不是随意的：`collectSchedClasses` 必须在 `collectProcItins` 之前（后者要遍历 `schedClasses()`），`inferSchedClasses` 必须在 `collectProcResources` 之前。17.4 的探针就是在这 13 行上加的。
- **`llvm/utils/TableGen/Common/CodeGenSchedule.cpp` 的 1003–1097 行（`createInstRWClass`）**
  - 一行 `Sets.expand(InstRWDef)` 吃掉 8.2 秒，其余 20 行共 21 ms。这是「一行调用背后藏着一个二次方算法」的教科书案例。
- **`llvm/utils/TableGen/Common/CodeGenSchedule.cpp` 的 53–175 行（`InstRegexOp`）**
  - 前缀提取（`find_first_of(RegexMetachars)`）、`removeParens` 的 `|`/`?` 检测、`equal_range` 的 `Comp`（注意它的 `LHS < RHS->getName() && !RHS->getName().starts_with(LHS)` 这个双条件——这是让「前缀恰好是某个完整名字」也能落入区间）。
- **`llvm/lib/TableGen/SetTheory.cpp` 的 282–332 行**
  - `evaluate` 与 `expand` 的全部逻辑。注意 `expand` 的缓存是 `DenseMap<const Record*, RecVec>`，以及「跳过匿名父类」那个 `isa<StringInit>(SuperClass->getNameInit())` 判断——后者是为了不把 `class Foo<...>` 当成 expander 名。
- **`llvm/lib/TableGen/TGTimer.cpp`（54 行，全读）**
  - 为什么 `-time-phases` 会漏时间：`startTimer` 的第一行 `if (!TimingGroup) return;`，以及 `startBackendTimer` 那个「如果 backend 自己启了 timer 就把 overall 清掉」的 `BackendTimer` 标志。
- **`llvm/lib/TableGen/Main.cpp` 的 150–228 行（`TableGenMain`）**
  - timer 的四个生命周期点：`startPhaseTiming`(156) → `startTimer("Parse, build records")`(159) → `startBackendTimer`(189) → `stopPhaseTiming`(225)。注意 189 行在 `ApplyCallback` **之前**，而 `InstrInfoEmitter` 的构造函数就在 `ApplyCallback` 里——所以构造耗时本应算进 `Backend overall`。实测 `-gen-subtarget` 的报告里确实有 `Backend overall 9.7285 s`（94.4%），但 `-gen-instr-info` 里没有，因为它在构造后立刻调了 `startTimer`，触发了 `BackendTimer` 的清除逻辑。**这是两个后端报告差异的唯一原因。**

## 下一章

到这里，Part 3 结束：你已经能读生成代码（Ch 16）、能定位生成器的真实成本（Ch 17）。下一部分进入「改源码」——从给 TableGen 语言加一条语法开始，走通 lexer → parser → `Init` → 记录 → backend 的完整链路。

[Ch 18 加一条语言特性：从语法到 `Init` 的全链路](./18-adding-a-language-feature.md)
