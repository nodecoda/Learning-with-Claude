# 第 21 章：性能——内存、重复匹配与变体爆炸

> **目标**：建立「时间 / 内存 / 输出体积」三维的性能观。第 17 章已经用 `-gen-instr-info` 证明「92% 的时间不在 `--time-phases` 报告里」；本章换一个正交视角：**同样的 10 秒，AArch64 只花 295 MB，RISCV 却要 767 MB**。你会看到内存花在哪、`instregex` 为什么重复匹配 61%、`ExpandHwMode` 如何把 RISCV 的 pattern 内存从 808 MB 推到 1.3 GB，以及为什么「库入口」比「命令行入口」多吃 80 MB。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。端到端数字来自 `/home/ubuntu/llvm/llvm-dev/build/bin/llvm-tblgen`（Release，assertions OFF）；分阶段数字来自一个链接 `libLLVMTableGenCommon` 的探针 `/tmp/tg21probe`（clang `-O2`，库入口 `TableGenParseFile`）。两者口径不同，文中会明确标注。源码树未修改。

---

## 21.1 三个维度，不要只盯时间

Ch 17 的结论是时间归因：`-gen-instr-info` 的 10 秒里，8 秒在 `CodeGenSchedModels` 的 `instregex` 匹配，生成代码只占 0.8 秒。

但「快」不是唯一指标。同一个 backend 在四个 target 上：

| target | `-gen-instr-info` 时间 | 峰值 RSS |
|---|---:|---:|
| AArch64 | 10.37 s | 295 MB |
| X86 | 8.24 s | 463 MB |
| RISCV | 6.28 s | **767 MB** |
| ARM | 1.11 s | 163 MB |

（三次取中位；X86 波动 5.3–8.7 s，机器有负载。）

RISCV **比 AArch64 快 40%，却多吃 2.6 倍内存**。如果你的 CI 机器只有 1 GB，RISCV 就是那个会 OOM 的 target。这一章回答三个问题：

1. 这些内存分别属于 parse、调度模型、还是 DAG pattern？
2. 哪些成本是**重复计算**（可以安全优化），哪些是**数据本身**（优化不了）？
3. 怎么测，才能不被 `--time-phases` 和「库入口 vs CLI 入口」骗？

---

## 21.2 基线：parse 的内存随记录数走

先只做 parse，不跑 backend：

```console
$ for T in AArch64 X86 RISCV ARM; do
    D=llvm/lib/Target/$T
    /usr/bin/time -f "$T %e s %M KB" llvm-tblgen -null-backend \
      -I $D -I llvm/lib/Target -I llvm/include $D/$T.td -o /dev/null
  done
AArch64 0.66 s 171380 KB
X86 ...
```

实测（assertions 构建 `/tmp/tgbuild` 与 Release 构建数字接近，这里用 Release）：

| target | parse 时间（负载波动） | parse RSS | def 数（练习 D） |
|---|---:|---:|---:|
| AArch64 | 0.66–0.94 s | 171 MB | 87,351 |
| X86 | 1.03–1.53 s | 252 MB | 87,794 |
| RISCV | 1.61–2.59 s | 412 MB | 260,265 |
| ARM | 0.41–0.63 s | 105 MB | 48,709 |

> 时间是同一台机器不同负载下的区间；**RSS 是稳定的**，所以内存归因比时间归因可靠。下面讨论「谁吃内存」时只看 RSS。

规律不完全是线性的：AArch64 约 2.0 MB/千 def，X86 约 2.9，RISCV 约 1.6，ARM 约 2.2。记录的类型构成（字段数、list/dag 嵌套）比单纯的数量更重要；但 RISCV 的记录数是 AArch64 的 3 倍，parse 内存也确实是 2.4 倍。

这部分内存是**数据本身**：每个 `Record`、`RecordVal`、`Init` 都在 bump allocator 里，parse 结束前不能释放。想省只能改数据布局（比如第 19 章那种「模板参数单独存」），不是加个缓存能解决的。

---

## 21.3 内存归因：`-gen-instr-info` 的三块

写一个探针，在同一进程里逐阶段打印 `getrusage(RUSAGE_SELF).ru_maxrss`：

```cpp
// /tmp/tg21probe.cpp（核心）
RecordKeeper Records;
// ... TableGenParseFile ...
outs() << "parse: " << rssMB() << " MB\n";

CodeGenTarget Target(Records);
outs() << "CodeGenTarget: " << rssMB() << " MB\n";

const CodeGenSchedModels &SM = Target.getSchedModels();
outs() << "SchedModels: " << rssMB() << " MB, classes "
       << SM.schedClasses().size() << "\n";

CodeGenDAGPatterns CDP(Records, /*ExpandHwMode=*/false);
outs() << "DAGPatterns: " << rssMB() << " MB\n";
```

实跑（同一台机器、同一个探针；时间有 ±30% 负载波动，看数量级）：

| target | parse | +`CodeGenTarget` | +`SchedModels` | +`DAGPatterns` |
|---|---:|---:|---:|---:|
| AArch64 | 254 MB | 254 MB | 255 MB | **349 MB** |
| X86 | 391 MB | 391 MB | 391 MB | **533 MB** |
| RISCV | 544 MB | 544 MB | 545 MB | **808 MB** |
| ARM | 174 MB | 174 MB | 176 MB | **221 MB** |

三个结论：

1. **`CodeGenTarget` 几乎不花内存**（+0–1 MB）。它只是包了一层索引，指向已有的 `Record`。
2. **`SchedModels` 只花 1–2 MB**——尽管它是 AArch64 那 10 秒的元凶（Ch 17）。时间和内存在这里完全解耦：它做的是**海量字符串匹配**，不是**建大结构**。
3. **`DAGPatterns` 才是内存大头**：AArch64 +94 MB、X86 +142 MB、RISCV +263 MB、ARM +47 MB。

探针的 parse 比 CLI 高（AArch64 254 vs 171 MB），原因在 21.6：库入口默认开启 reference-loc 追踪。**所以不要把探针的绝对值和 CLI 的绝对值混在一张表里比较**；同一列内部的增量是可信的。

### 21.3.1 为什么 `DAGPatterns` 这么贵

`CodeGenDAGPatterns` 把每个 instruction 的 `Pattern` 展开成 `TreePattern` 节点树，还要为每个 pattern 建 operand、type、predicate 的映射。RISCV 有 16,962 条指令，且大量指令带多个 pattern/变体，所以 +263 MB。

`InstrInfoEmitter` 的构造函数里有一个成员：

```cpp
// llvm/utils/TableGen/InstrInfoEmitter.cpp:56-63
class InstrInfoEmitter {
  const RecordKeeper &Records;
  const CodeGenDAGPatterns CDP;
  const CodeGenSchedModels &SchedModels;
public:
  InstrInfoEmitter(const RecordKeeper &R)
      : Records(R), CDP(R, /*ExpandHwMode=*/false),
        SchedModels(CDP.getTargetInfo().getSchedModels()) {}
```

注意 `-gen-instr-info` **并不需要**完整的 pattern 树来打印指令表，但它无条件构造了 `CDP`——因为 `CodeGenTarget` 和 `SchedModels` 都挂在它上面。这是「为了拿 A，顺手构造了 A+B+C」的典型。真要省内存，可以把 `CDP` 拆成按需构造，但那是跨文件重构，不是本章范围。

---

## 21.4 时间和内存不是一回事

把探针的时间也列出来（AArch64 的 SchedModels 四次测量在 11.4–16.4 s 之间波动，取区间）：

| target | SchedModels 时间 | SchedModels 内存 | DAGPatterns 时间 | DAGPatterns 内存 |
|---|---:|---:|---:|---:|
| AArch64 | **11–16 s** | +1 MB | 1.3 s | +94 MB |
| X86 | 3.2–5.0 s | +0 MB | 2.7 s | +142 MB |
| RISCV | 1.3–1.7 s | +1 MB | 3.4 s | +263 MB |
| ARM | 0.4–0.8 s | +2 MB | 0.3 s | +47 MB |

> 口径提醒：探针的 `SchedModels` 绝对值比 Ch 17 的 8.11 s 更高（11–16 s），这与库入口开启 reference-loc 追踪、以及测量时机器有负载都有关；CLI 的端到端仍然是 10.37 s。**绝对时间会随构建和入口变化，量级与「谁花时间」的结论不变**。内存增量（+1 MB）在所有运行里都稳定。

两个 target 恰好相反：

- **AArch64 是时间问题**：调度模型做了数千次 `instregex` 匹配（本章统计 `.td` 文本中 8,661 次出现；Ch 17 的运行时计数是 8,675），但不建大结构。
- **RISCV 是内存问题**：调度模型只有 12 条 `instregex`（见 21.5），但 16,962 条指令的 pattern 树吃掉 263 MB。

所以「优化 TableGen 性能」这句话没有意义。你必须先回答：**优化哪个 target 的哪个维度**。

---

## 21.5 重复匹配：`instregex` 的缓存 key 是 Record，不是字符串

Ch 17 说 AArch64 的 8.8 秒花在 `instregex` 匹配上。本章再往下挖一层：**其中很大一部分是重复的**。

### 21.5.1 机制

`SetTheory::expand` 有一层缓存：

```cpp
// llvm/lib/TableGen/SetTheory.cpp:309-316
const RecVec *SetTheory::expand(const Record *Set) {
  // Check existing entries for Set and return early.
  ExpandMap::iterator I = Expansions.find(Set);
  if (I != Expansions.end())
    return &I->second;
  ...
```

`Expansions` 的 key 是 **`const Record *`**（也就是 `InstRW` 那条记录），不是正则字符串。同一个 `InstRW` 记录被展开两次会命中缓存；但**两个不同的 `InstRW` 记录写了同一个正则**，会各算一遍。

而 `InstRegexOp::apply` 每次调用都会：

1. 从模式里找前缀（`find_first_of(RegexMetachars)`）；
2. **新建一个 `Regex` 对象**（`Regexpr = Regex(pat)`，编译正则）；
3. 对 pseudo / non-pseudo 两段有序区间各做一次 `std::equal_range`；
4. 对区间内每个指令名做 `Regexpr->match(...)`。

```cpp
// llvm/utils/TableGen/Common/CodeGenSchedule.cpp:119-127（节选）
      std::optional<Regex> Regexpr;
      StringRef Prefix = Original.substr(0, FirstMeta);
      StringRef PatStr = Original.substr(FirstMeta);
      if (!PatStr.empty()) {
        std::string pat = PatStr.str();
        ...
        Regexpr = Regex(pat);
      }
```

正则对象、前缀、区间都是**局部变量**，调用结束即销毁。没有任何跨调用的记忆化。

### 21.5.2 实测重复率

统计四个 target 的 `.td` 里 `instregex "..."` 的出现次数与去重后的模式数：

```console
$ for T in AArch64 X86 RISCV ARM; do
    echo "== $T"
    echo "total:  $(grep -rho 'instregex "[^"]*"' llvm/lib/Target/$T --include='*.td' | wc -l)"
    echo "unique: $(grep -rho 'instregex "[^"]*"' llvm/lib/Target/$T --include='*.td' | sort -u | wc -l)"
  done
```

| target | `instregex` 出现次数 | 不同模式数 | 重复率 |
|---|---:|---:|---:|
| AArch64 | **8,661** | 3,380 | **61%** |
| X86 | 1,581 | 843 | 47% |
| RISCV | **12** | 5 | 58%（基数极小） |
| ARM | 920 | 732 | 20% |

AArch64 最极端的几条：

```text
     20 instregex "ST2Twov(8b|4h|2s)$"
     19 instregex "ST1i(8|16|32|64)$"
     18 instregex "^SHA1[CMP]"
     18 instregex "^DUPv.+gpr"
```

同一个模式在 20 条 `InstRW` 记录里出现，就编译 20 次正则、扫 20 遍指令区间。RISCV 只有 12 条 `instregex`，这也解释了为什么它的 `SchedModels` 只要 1.3 秒——它用的是显式 `instrs` 列表，不走正则。

### 21.5.3 一个安全的优化方向（未实现，仅分析）

在 `InstRegexOp` 里加一个以模式字符串为 key 的 `StringMap<RecVec>`：

```cpp
// 示意，不是现有代码
struct InstRegexOp : SetTheory::Operator {
  const CodeGenTarget &Target;
  StringMap<SmallVector<const Record *, 8>> Cache;  // pattern -> matches
  void apply(...) {
    for (const Init *Arg : Expr->getArgs()) {
      StringRef Pat = cast<StringInit>(Arg)->getValue();
      auto &Matches = Cache[Pat];
      if (Matches.empty()) { /* 原来的匹配逻辑，填 Matches */ }
      Elts.insert_range(Matches);
    }
  }
};
```

正确性上这是安全的：匹配结果只依赖 `Pat` 和 `Target`，而 `Target` 在一次 `CodeGenSchedModels` 生命周期内不变。AArch64 的 8,661 次可以降到 3,380 次，**理论上限减少 61% 的匹配工作**。

但**不要直接承诺 61% 的时间收益**：

- 前缀优化已经把很多模式变成 `equal_range` + 少量 `match`，单次成本差异很大；
- 缓存本身要存 3,380 个 vector，内存会涨；
- 真正的瓶颈要用采样确认（见 21.7）。

这就是「性能优化」的正确姿势：**先测机制，再估上限，最后用 profile 验证**。Ch 22 会把这个优化当成一个可提交 PR 的案例来走流程。

---

## 21.6 `ExpandHwMode`：组合爆炸

`CodeGenDAGPatterns` 的构造函数有一个 `ExpandHwMode` 参数。`-gen-instr-info` 传 `false`，`-gen-dag-isel` 传默认的 `true`。差别有多大？

用同一个探针，把 `CDP(Records, false)` 改成 `CDP(Records, true)`：

| target | `ExpandHwMode=false` | `ExpandHwMode=true` |
|---|---:|---:|
| AArch64 | 1.3 s / 349 MB | 1.8 s / 349 MB |
| X86 | 2.7 s / 533 MB | 2.5 s / 538 MB |
| RISCV | **3.4 s / 808 MB** | **7.7 s / 1,334 MB** |
| ARM | 0.3 s / 221 MB | 0.5 s / 222 MB |

RISCV 的内存直接翻到 **1.3 GB**。原因：hwmode（按 CPU 特性区分的模式）会把同一个 pattern 按每种硬件模式复制一份；RISCV 的扩展组合多，pattern 树就被乘以一个很大的因子。

端到端验证：

```console
$ llvm-tblgen -gen-dag-isel ... AArch64.td -o /dev/null
3.33 s / 356 MB
$ llvm-tblgen -gen-dag-isel ... RISCV.td -o /dev/null
8.77 s / 893 MB
```

`-gen-dag-isel` 的 893 MB 比探针的 1,334 MB 低，因为 CLI 入口不追踪 reference loc、且在 emit 过程中逐步释放。**同一个「阶段」，不同入口、不同参数，数字可以差 50%**——这就是为什么 21.1 强调口径。

---

## 21.7 测量方法论：怎么不骗自己

### 21.7.1 `--time-phases` 会漏，而且会「清掉」backend 计时

Ch 17 已详细解释 `TGTimer` 的设计。这里补一个关键细节：

```cpp
// llvm/lib/TableGen/TGTimer.cpp:26-36
void TGTimer::startTimer(StringRef Name) {
  if (!TimingGroup) return;
  if (LastTimer && LastTimer->isRunning()) {
    LastTimer->stopTimer();
    if (BackendTimer) {
      LastTimer->clear();     // ← “Backend overall” 这一行被删掉
      BackendTimer = false;
    }
  }
  ...
```

backend 一旦启动自己的子计时器，`Backend overall` 就从报告里**消失**。所以你会看到「Total 1.25 s」而实际 18 s，且报告里连 `Backend overall` 行都没有。**报告的总数不是进程总数。**

正确做法：

- 用 `/usr/bin/time -v` 拿 `Elapsed` / `User time` / `Maximum resident set size`；
- 用 `--time-phases` 只看**已命名的阶段之间的相对比例**；
- 对缺失的部分用 `gdb` 采样（21.7.3）。

### 21.7.2 库入口 vs CLI 入口

`TableGenParseFile`（`llvm/lib/TableGen/Parser.cpp:35`）把 `TrackReferenceLocs` 硬编码为 `true`：

```cpp
  TGParser Parser(SrcMgr, /*Macros=*/{}, Records,
                  /*NoWarnOnUnusedTemplateArgs=*/false,
                  /*TrackReferenceLocs=*/true);
```

而 `llvm-tblgen` 的 `TableGenMain` 用 `TGParser` 的默认值 `false`。后果：

| AArch64 | RSS |
|---|---:|
| `llvm-tblgen -null-backend`（CLI） | 171 MB |
| 探针 `TableGenParseFile`（库入口） | 253 MB |

**+82 MB（+48%）**，全部用于记录每个引用的 `SMRange`。练习 B/D 的 faithful 实现如果链接库入口，内存会比官方 CLI 高——这不是你代码写得差，是入口不同。

### 21.7.3 用 gdb 采样找「报告外」的热点

`ptrace_scope=1` 时不能 attach 已运行进程，但可以让 gdb **启动**进程，然后给 gdb 发 `SIGINT`：

```console
$ gdb -q -batch -ex 'set pagination off' -ex run -ex 'bt 15' \
    --args llvm-tblgen -gen-instr-info -I ... AArch64.td -o /dev/null &
$ sleep 4; kill -INT $!
```

实测栈顶（简化）：

```text
#0 lstep ()
#1 llvm_regexec ()
#2 llvm::Regex::match(...)
#3 InstRegexOp::apply(...)
#4 llvm::SetTheory::expand(...)
#5 CodeGenSchedModels::createInstRWClass(...)
#6 CodeGenSchedModels::collectSchedClasses()
#7 CodeGenSchedModels::CodeGenSchedModels(...)
#8 CodeGenTarget::getSchedModels()
#9 InstrInfoEmitter::run(...)
```

这一条栈就把「10 秒花在哪」钉死了，比任何计时器都直接。

### 21.7.4 不要用 assertions 构建做性能结论

`/tmp/tgbuild` 是 `Release + assertions`，`llvm-dev/build` 是纯 Release。同一命令在两者上可以差 10–20%。做性能对比时：

- 固定 build 类型；
- 固定机器、固定负载（至少跑 3 次取中位）；
- 报告 `user time` 而不是只在 shell 里 `time` 看 `real`。

---

## 21.8 一张「优化决策」表

| 观察到的现象 | 先查什么 | 可能的优化 | 风险 |
|---|---|---|---|
| 某个 target 特别慢 | `gdb` 采样栈 | 针对热点加缓存 | 语义变化、内存上升 |
| RSS 特别高 | 分阶段 `ru_maxrss` | 延迟构造 / 拆分结构 | 改 API、跨文件 |
| 报告时间远小于 wall | `TGTimer` 的 `clear()` | 修计时器或自己加探针 | 低（工具改动） |
| 输出很大 | `-print-records` 字节数 | 流式写、避免拼接 | 输出格式不能变 |
| 重复计算 | 统计输入里的重复模式 | 记忆化（key 要选对） | 缓存失效、内存 |

本章的 RISCV 内存问题属于第二行；AArch64 的时间问题属于第一行；`instregex` 重复属于第五行。

---

## 21.9 检查清单

做 TableGen 性能分析时：

- [ ] 明确维度：时间 / 内存 / 输出体积
- [ ] 用 `/usr/bin/time -v` 记录 wall、user、maxrss
- [ ] 不用 `--time-phases` 的 Total 当进程总时间
- [ ] 区分 CLI 入口与库入口（`TrackReferenceLocs`）
- [ ] 固定 build 类型与机器负载，至少 3 次取中位
- [ ] 分阶段测 RSS，而不是只看最终峰值
- [ ] 用 gdb 采样验证热点，而不是猜
- [ ] 优化前先算「重复率上限」，并说明哪些部分不可优化

---

## 21.10 踩雷记录

1. **错误直觉**：`-gen-instr-info` 慢的 target 也一定吃内存多。
   **正确认识**：AArch64 慢（10 s）但只吃 295 MB；RISCV 快（6.3 s）却吃 767 MB。时间瓶颈在 `instregex` 匹配（不建结构），内存瓶颈在 `DAGPatterns`（建大树）。两个维度要分开测。

2. **错误直觉**：`--time-phases` 的 Total 就是进程耗时。
   **正确认识**：backend 启动子计时器时，`Backend overall` 会被 `LastTimer->clear()` 删掉；未包裹的代码完全不计。实测报告 1.25 s、实际 18.6 s。

3. **错误直觉**：`SetTheory` 有缓存，所以同一个 `instregex` 不会重复算。
   **正确认识**：`Expansions` 的 key 是 `InstRW` 的 `Record *`，不是模式字符串。两个记录写同一个正则会各算一遍；AArch64 的 8,661 次出现只有 3,380 个不同模式。

4. **错误直觉**：库入口和 CLI 解析同一个文件，内存应该一样。
   **正确认识**：`TableGenParseFile` 强制 `TrackReferenceLocs=true`，AArch64 多 82 MB（171→253）。跨入口比较数字没有意义。

5. **错误直觉**：`ExpandHwMode` 只影响 pattern 分析，不影响内存。
   **正确认识**：RISCV 从 808 MB 涨到 1,334 MB，接近翻倍。hwmode 会按硬件模式复制 pattern 树。

6. **错误直觉**：既然 61% 的 `instregex` 是重复的，加缓存就能快 61%。
   **正确认识**：这是**工作量的上限**，不是时间的上限。前缀二分已经把很多匹配压得很便宜；缓存还有内存成本。必须先 profile 再承诺收益。

---

## 21.11 延伸阅读

1. **`llvm/lib/TableGen/TGTimer.cpp`（全文 50 行）+ `llvm/include/llvm/TableGen/TGTimer.h`**
   读什么：`startTimer` 的 `clear()`、`startBackendTimer` 的语义、`stopPhaseTiming` 的打印时机。
   学什么：为什么报告会「吃掉」backend 行；如何设计一个不会误导人的计时器。
   和本章的关系：21.7.1 的所有结论都来自这 50 行。

2. **`llvm/utils/TableGen/Common/CodeGenSchedule.cpp` 的 `InstRegexOp`（45–158 行）**
   读什么：前缀提取、括号处理、`equal_range`、`Regex` 构造。
   学什么：一个「字符串匹配」热点如何被写成 100 行；哪些优化已经做了、哪些没做。
   和本章的关系：21.5 的机制与优化方向。

3. **`llvm/lib/TableGen/SetTheory.cpp` 的 `expand` / `evaluate`（240–340 行）**
   读什么：`Expansions` 的 key、递归保护、`RecSet` 去重。
   学什么：缓存 key 选错会怎样；为什么「按 Record 缓存」不等于「按表达式缓存」。
   和本章的关系：解释 61% 重复率的根因。

4. **`llvm/utils/TableGen/Common/CodeGenDAGPatterns.cpp` 的构造函数**
   读什么：`ExpandHwMode` 如何控制 `TypeSetByHwMode` 的展开、pattern 树何时复制。
   学什么：一个 bool 参数如何造成 500 MB 差异。
   和本章的关系：21.6 的组合爆炸。

5. **练习 B 的性能测量（AArch64：parse 1.0 s / 遍历 0.13 s / 格式化 0.6 s）**
   读什么：同一个 `RecordKeeper` 上，不同消费者的成本结构。
   学什么：parse 是固定成本，backend 的差异全在「额外构造什么」。
   和本章的关系：21.3 的「三块内存」在 `-print-records` 上的对照。

---

## 下一章

[第 22 章：上游流程——从一个可提交 PR 到 review](22-upstream-process.md)

知道了热点，下一步是把它变成上游能接受的改动。下一章用本章的两个候选——`instregex` 结果缓存和 `GET_INSTRINFO_SCHED_ENUM` 死产物清理——走一遍 RFC、实现、测试、review 的完整流程，并给出每个阶段该附什么证据。
