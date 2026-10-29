# 第 22 章：上游流程——从一个可提交 PR 到 review

> **目标**：把前 21 章的能力变成一次真实的上游贡献。你会看到 LLVM 当前的评审流程（GitHub PR、squash、RFC）、TableGen 区域的活跃维护者与最近的真实 commit、两个可操作的候选改动（`instregex` 结果缓存、`GET_INSTRINFO_SCHED_ENUM` 清理），以及一份能直接套用的 PR 描述与 benchmark 表格模板。重点不是「怎么点按钮」，而是**怎么让 reviewer 相信你的改动值得合并**。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。文中的 commit 哈希、作者、diffstat、commit message 全部来自本机 `git log` / `git show`；政策引用来自 `llvm/docs/Contributing.md`、`CodeReview.md`、`DeveloperPolicy.md`、`RFCProcess.md`。源码树未修改。

---

## 22.1 现在的上游流程长什么样

LLVM 早在 2023 年就从 Phabricator 迁到了 GitHub Pull Request。当前政策（`llvm/docs/Contributing.md:101`）：

> The LLVM project uses GitHub Pull Requests for code review.

关键规则：

| 规则 | 出处 | 含义 |
|---|---|---|
| 选对 reviewer | `Contributing.md:107-112` | 找 `Maintainers.md` 里的维护者，或该区域的活跃开发者 |
| 一周后可 ping | `Contributing.md:119` | 「common courtesy 'ping' rate is once a week」 |
| 批准后 squash merge | `Contributing.md:140` | 线性历史，不允许 merge commit |
| 重大改动先 RFC | `DeveloperPolicy.md:541` | 设计共识在实现之前 |
| 增量开发 | `DeveloperPolicy.md:563` | 反对长期分支，鼓励小步进树 |

TableGen 当前的 maintainer（`llvm/Maintainers.md:451-454`）：

```text
#### TableGen

Rahul Joshi \
rjoshi@nvidia.com (email), [jurahul](https://github.com/jurahul) (GitHub)
```

但「谁实际在改」要看 commit 历史。最近 15 个 `llvm/lib/TableGen` 提交：

```console
$ git log --oneline -15 -- llvm/lib/TableGen
fcee33940765 [TableGen] Store template arguments separately from fields (#234933)
5a3396bd8c2a [TableGen] Use "parent class" terminology in TGParser. NFC. (#235106)
4bb80989749f [TableGen] Avoid shadowed variables in TGParser. NFC. (#235743)
00f0347f1778 [TableGen] Use TGParser::CurMultiClass instead of passing it around. NFC. (#235742)
0d5253f61778 [TableGen] Assert that RecordVal values are never null (#235077)
ca04e5a154b9 [SystemZ][z/OS] Open tablegen files as UTF-8 (#235035)
36e26fefb491 TableGen: Index defs by direct superclass for getAllDerivedDefinitions (#234952)
a12a9d9e4708 [TableGen] Remove unused AllowSelfAssignment parameter of SetValue. NFC. (#234917)
05113387a938 TableGen: Remember superclass results when finding derived definitions (#234807)
...
```

最近 50 个提交的作者分布：

```console
$ git log --format='%an' -50 -- llvm/lib/TableGen | sort | uniq -c | sort -rn | head
     11 Jay Foad
      8 Matt Arsenault
      6 Tomer Shafir
      5 Fangrui Song
      4 Kevin Bravo
```

两个信号：

1. **这个区域很活跃**，但不是「谁都能随便改」——核心是一小群熟悉 `Record`/`TGParser` 的人。
2. **`NFC` 提交很多**（No Functional Change）。上游接受「只改结构/命名、不改行为」的小步重构，这是新贡献者建立信任的低风险入口。

---

## 22.2 动手前的尽职调查

在写第一行代码前，做四件事：

### 22.2.1 确认特性不存在

第 18 章的教训：`!tolower`/`!toupper` 已经存在。先 grep：

```console
$ grep -rn '"min"' llvm/lib/TableGen llvm/include/llvm/TableGen
$ grep -rn 'instregex' llvm/utils/TableGen/Common/CodeGenSchedule.cpp
```

### 22.2.2 读最近的同区域提交

```console
$ git log --oneline -30 -- llvm/lib/TableGen llvm/utils/TableGen
$ git log -S 'GET_INSTRINFO_SCHED_ENUM' --oneline -- llvm
```

如果最近有人在做同一块（比如 Matt Arsenault 在 2026 年 10 月连续提交了 `#231091`、`#234807`、`#234952` 三个性能优化），先读他们的 diff 和 commit message。**重复劳动是 review 里最尴尬的拒绝理由。**

### 22.2.3 找 maintainer 和最近改过该文件的人

```console
$ git log --format='%an <%ae>' -5 -- llvm/utils/TableGen/Common/CodeGenSchedule.cpp
```

reviewer 的默认选择是「maintainer + 最近改过这段代码的人」。后者知道上下文，能给出具体的技术意见。

### 22.2.4 判断要不要 RFC

`RFCProcess.md` 列了 RFC 必须包含的四项：

> * a detailed overview of the proposed changes,
> * the motivation for why the changes are being proposed,
> * the impact on different parts of the project, and
> * any open questions the community should address.

判断标准不是「代码量」，是**影响面**：

| 改动 | 要 RFC 吗 | 理由 |
|---|---|---|
| 修一个诊断消息的拼写 | 不要 | 无外部影响 |
| 加 `!min`/`!max` | 通常不要 | 纯加法，不破坏现有 `.td` |
| `instregex` 结果缓存 | 通常不要 | 输出不变，纯性能；但要在 PR 里给数据 |
| 改 `getValues()` 语义 | **要** | 所有 backend/工具受影响（第 19 章） |
| 删 `GET_INSTRINFO_SCHED_ENUM` | **要** | 树外 target 可能依赖 |

---

## 22.3 案例 A：`instregex` 结果缓存（性能 PR）

### 22.3.1 问题陈述（用数字，不用形容词）

第 21 章测到：

- AArch64 的 `.td` 里有 **8,661 次** `instregex` 出现，只有 **3,380 个**不同模式（61% 重复）；
- `SetTheory::expand` 的缓存 key 是 `InstRW` 的 `Record *`，不是模式字符串；
- `InstRegexOp::apply` 每次重新编译 `Regex`、重新 `equal_range`、重新逐条 `match`；
- AArch64 的 `CodeGenSchedModels` 构造占 `-gen-instr-info` 的绝大部分时间（Ch 17）。

一句话：**同一个正则被不同 `InstRW` 记录重复求值**。

### 22.3.2 设计（最小、可审查）

在 `InstRegexOp` 里加一个以模式字符串为 key 的缓存：

```cpp
// llvm/utils/TableGen/Common/CodeGenSchedule.cpp（示意）
struct InstRegexOp : public SetTheory::Operator {
  const CodeGenTarget &Target;
  // Pattern text -> matching instruction records. The target is fixed for
  // the lifetime of this operator, so the cache never goes stale.
  StringMap<SmallVector<const Record *, 8>> Cache;
  ...
};
```

三个必须回答的设计问题：

1. **key 的生命周期**：`StringRef` 来自 `.td` 的 `StringInit`，在 `RecordKeeper` 生命周期内稳定；`StringMap` 会拷贝 key，安全。
2. **正确性**：匹配只依赖 `(pattern, Target)`，而 `Target` 是 `const` 成员，构造后不变。所以缓存不会失效。
3. **内存**：AArch64 存 3,380 个 `SmallVector`，每个平均几十个指针，估计几 MB——相对于 295 MB 的峰值可以忽略。**但仍要在 PR 里给出实测 RSS 前后对比**。

### 22.3.3 测试策略

性能改动最容易被要求「证明输出没变」。三层验证：

1. **lit**：`llvm/test/TableGen` 全量（496 个）必须绿。
2. **逐字节对比**：四个 target 的 `-gen-instr-info` 输出 `cmp` 一致。这是 Ch 16/练习 B 用过的方法：

```console
$ for T in AArch64 X86 RISCV ARM; do
    D=llvm/lib/Target/$T
    llvm-tblgen -gen-instr-info -I $D -I llvm/lib/Target -I llvm/include $D/$T.td -o /tmp/after-$T.inc
    cmp /tmp/before-$T.inc /tmp/after-$T.inc
  done
```

3. **负向**：`emitInstrInfo` 的 sched 相关错误测试（`llvm/test/TableGen/CodeGenSchedule-*`）仍绿。

### 22.3.4 Benchmark 怎么写

不要写「快了 30%」。上游期望的是类似 `b4effb258020`（Matt Arsenault，`TableGen: Cache resolved class instantiations`）的格式：

```text
Gigainstructions executed by llvm-tblgen --null-backend

             before   after
  AMDGPU.td   71.46   17.78  -75.1%
  X86.td       6.04    6.09   +0.9%
  AArch64.td   3.27    3.31   +1.4%
  RISCV.td     8.12    8.21   +1.1%
```

注意几个细节：

- 指标是 **instructions**，不是 wall clock（不受机器负载影响）；
- 每列是**同一个输入**的前后对比；
- 明确标出**变慢的 target**（+1.4%），不藏；
- 如果是全局影响，再补一组「全 target 构建」的宏观数据（`05113387a938` 用了 366 次 tablegen run 的 CPU seconds）。

对你的 `instregex` 缓存，表格应该长这样：

```text
llvm-tblgen -gen-instr-info, instructions (perf stat)

             before      after
  AArch64     N1          N2
  X86         ...
  RISCV       ...   (expected ~no change: only 12 instregex)
  ARM         ...
```

**主动说明 RISCV 不会受益**，比被 reviewer 问出来好。

### 22.3.5 预期 review 意见

| reviewer 可能问 | 准备什么 |
|---|---|
| 为什么不直接改 `SetTheory` 的缓存 key？ | `SetTheory` 是通用机制，key 从 `Record*` 改成表达式会让所有 operator 受影响；只在 `InstRegexOp` 内部缓存更局部 |
| 缓存会不会无限增长？ | 上限是不同模式数（AArch64 3,380）；给出实测 RSS |
| 和 hwmode 有关吗？ | 匹配发生在 hwmode 展开之前，缓存的是指令集合，与 hwmode 无关 |
| 为什么不用 `DenseMap<StringRef,...>`？ | key 来自稳定 buffer，可以；但 `StringMap` 语义更清晰，先按简单方案 |
| 有没有更简单的办法？ | 有：把重复的 `instregex` 合并成一条 `instrs` 列表（改 `.td`），但那是 target 维护者的选择，不是通用优化 |

---

## 22.4 案例 B：删掉 `GET_INSTRINFO_SCHED_ENUM` 死产物（需要 RFC）

第 16 章量过：AArch64 的 `-gen-instr-info` 输出里，`GET_INSTRINFO_SCHED_ENUM` 段占 **2,269 行 / 214,655 字节**；全仓库只有 3 个 target 消费：

```console
$ grep -rn "GET_INSTRINFO_SCHED_ENUM" llvm/lib/Target
llvm/lib/Target/Hexagon/MCTargetDesc/HexagonMCTargetDesc.h:114
llvm/lib/Target/AMDGPU/MCTargetDesc/R600MCTargetDesc.h:35
llvm/lib/Target/PowerPC/MCTargetDesc/PPCMCTargetDesc.h:188
```

### 22.4.1 为什么必须 RFC

因为**树外 target 可能定义同一个宏**。这不是「代码里没人用」，而是「我们看不到的人在不在用」。RFC 要回答：

1. 这个段是什么、谁在用（3 个 in-tree 消费者）；
2. 删除/改为 opt-in 后，树外 target 如何迁移；
3. 兼容期多长；
4. 预期收益（AArch64 的 214 KB、每次生成的时间）。

### 22.4.2 三种实现方案

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A. 直接删 | 去掉 `IfDefEmitter` 调用 | 最简单 | 树外立即坏 |
| B. opt-in flag | 新增 `-gen-instr-info-emit-sched-enum`，默认关 | 兼容 | 多一个 flag，长期负担 |
| C. 独立 backend | 移到 `-gen-instr-info-sched-enum` | 职责清晰 | 消费者要改两处 build 规则 |

上游通常偏好 **B 或 C + 一个 release cycle 的弃用期**。直接把「删」作为第一版 PR，大概率被要求改成 B。

### 22.4.3 证据清单

- `git log -S GET_INSTRINFO_SCHED_ENUM -- llvm`：这个段是什么时候加的、为什么；
- 全部 in-tree 消费者（3 个）；
- 删掉后 `ninja check-llvm` + 全 target build；
- 输出体积前后对比（AArch64 214,655 B）；
- release notes 条目。

---

## 22.5 一次 PR 的真实时间线

以本章的缓存为例，一个合理的节奏：

```text
Day 0   fork + 本地实现 + 自测（496 lit + 四 target cmp）
Day 1   开 draft PR，描述里贴 before/after 表
Day 2   CI 绿，@maintainer + @最近改过该文件的人
Day 3-7 根据 review 改：命名、注释、benchmark 格式、边界
Day 8   ping 一次（如果没人回）
Day 9+  approval → squash merge → 自动 commit
```

几个容易踩的流程点：

- **draft PR 要早开**。让 reviewer 在你写测试之前就能说「方向不对」，比写完 500 行再被拒好。
- **CI 不是本地 lit**。LLVM 的 buildkite 会跑全量构建；TableGen 改动会触发所有 target 的 `.inc` 重新生成，CI 时间可能很长。
- **squash 之后 commit message 由 PR 标题 + 描述生成**，所以标题要写成 `[TableGen] ...`，描述里的 benchmark 表会被保留。
- **不要 force-push 一个正在 review 的分支**到「看不出改了什么」；增量 commit 更利于 review，合并时再 squash。

---

## 22.6 PR 描述模板

```markdown
[TableGen] Cache instregex results in CodeGenSchedModels

`InstRegexOp::apply` recompiles the regex and rescans the instruction
ranges for every `InstRW` record. AArch64 has 8,661 `instregex`
occurrences but only 3,380 distinct patterns, so 61% of the matching
work is redundant. Cache the matched records keyed by the pattern text;
the target is immutable for the lifetime of the operator, so the cache
cannot go stale.

Benchmark (perf stat, instructions):
| target  | before | after | delta |
|---------|-------:|------:|------:|
| AArch64 |  ...   |  ...  |  ...  |
| X86     |  ...   |  ...  |  ...  |
| RISCV   |  ...   |  ...  |  ...  |
| ARM     |  ...   |  ...  |  ...  |

Peak RSS (AArch64, -gen-instr-info): ... MB -> ... MB.

Tests: llvm-lit llvm/test/TableGen (496/496); byte-identical
`-gen-instr-info` output for AArch64/X86/RISCV/ARM (`cmp`).
```

模板的每一段都对应一个 review 关注点：

- 第一段：**问题 + 机制**（不是「优化一下」）；
- 第二段：**数据**（reviewer 可以复现）；
- 第三段：**正确性证据**（输出没变）；
- 第四段：**回归证据**（测试全绿）。

---

## 22.7 Commit message 的写法

LLVM 的 commit message 通常由 PR 标题 + 描述生成。观察真实例子：

```text
[TableGen] Store template arguments separately from fields (#234933)

Record used to store template arguments as RecordVals in Values, along
with the fields, and keep a separate list of their names. Store them in
their own vector of RecordVals instead. getValues() now only returns
fields, ...

The only change in output is that --print-detailed-records prints
"Fields: (none)" for a class with template arguments but no fields.

Parser memory usage decreases by about 1% on AMDGPU.td ...
```

结构：

1. **标题**：`[TableGen]` + 动词开头的短句 + `(#PR)`；
2. **第一段**：旧行为 → 新行为；
3. **行为变化**：明确「唯一的变化是什么」；
4. **度量**：内存/时间/体积；
5. **Co-authored-by**（如果用了 AI 或结对）。

特别注意第 3 点：**主动声明行为变化**。第 19 章的 commit 之所以容易 review，就是因为它把「唯一输出变化」写在了 message 里，reviewer 不用自己找。

---

## 22.8 Review 会问的十个问题（TableGen 特化）

1. 这个改动**改变了哪个已有 API 的契约**？有没有更新所有调用点？
2. 树外消费者怎么办？（TableGen 被 MLIR/CIRCT 等直接 include）
3. 输出（`-print-records`、`-gen-*`、JSON、detailed）有没有变？变了有没有测试？
4. 内存/时间有没有实测？数字能不能复现？
5. 新代码在 `assertions` 构建下会不会触发断言？空值、未解析值处理了吗？
6. 新增的 `StringRef`/`Init *` 的生命周期安全吗？
7. 有没有更简单的做法（改 `.td`、改调用方）？
8. 错误路径的测试呢？（`not llvm-tblgen` + `--implicit-check-not`）
9. 命名符合 LLVM 风格吗？（`camelCase`、`PascalCase`、`NFC` 提交分离）
10. 需要 release notes 吗？（新语法/行为变化 → 要）

---

## 22.9 新手最常被拒的六种 PR

| 症状 | reviewer 的反应 | 正确做法 |
|---|---|---|
| 只写「优化性能」没有数字 | 「Please provide measurements」 | 按 22.3.4 的表格给 before/after |
| 一个 PR 里混了重构 + 行为变化 | 「Please split」 | 先 NFC 重构，再行为改动 |
| 改了输出但没改测试 | CI 红 / 「update tests」 | 同步改 `FileCheck`，说明变化 |
| 加了 API 没有消费者 | 「Who will use this?」 | 同 PR 带上调用点，或先 RFC |
| 破坏性改动没有迁移说明 | 「This needs an RFC」 | 按 22.4 的清单准备 |
| 测试只写 happy path | 「Add a negative test」 | 正/负各一，参考第 20 章 |

---

## 22.10 检查清单

提交前逐条打勾：

- [ ] grep 过现有实现，确认不是重复
- [ ] 读过最近 30 个同区域 commit，知道谁在改
- [ ] 判断过是否需要 RFC（影响面，不是代码量）
- [ ] PR 描述包含：问题、机制、before/after 数据、测试证据
- [ ] 本地 `llvm-lit llvm/test/TableGen` 全绿
- [ ] 四个 target 的真实 `.td` 仍能生成（至少 `-null-backend` + 受影响 backend）
- [ ] 输出变化已声明，并有测试锁定
- [ ] 破坏性改动有 release notes / 迁移说明
- [ ] commit message 是 `[TableGen] ...` 格式
- [ ] 选了正确的 reviewer（maintainer + 最近改过该文件的人）

---

## 22.11 踩雷记录

1. **错误直觉**：LLVM 还在用 Phabricator / 邮件列表审代码。
   **正确认识**：现在是 GitHub PR（`Contributing.md:101`），批准后 squash merge，线性历史；设计讨论在 Discourse RFC。

2. **错误直觉**：只要代码对，reviewer 就会收。
   **正确认识**：TableGen 区域由一小群活跃开发者维护（最近 50 个提交里 Jay Foad 11 次、Matt Arsenault 8 次），他们对性能数字、输出变化、树外影响非常敏感。没有数据的「优化」基本会被打回。

3. **错误直觉**：性能 PR 只要贴 wall clock 就够了。
   **正确认识**：上游惯例是用 `perf stat` 的 instructions（如 `#231091` 的 Gigainstructions 表），因为 wall clock 受机器负载影响。还要主动列出**变慢**的 target。

4. **错误直觉**：删掉没人用的代码不需要 RFC。
   **正确认识**：`GET_INSTRINFO_SCHED_ENUM` 在 in-tree 只有 3 个消费者，但树外 target 可能依赖。删除生成物 = 改接口，要 RFC + 兼容期。

5. **错误直觉**：一个 PR 里顺手重构一下更省事。
   **正确认识**：上游偏好 `NFC` 提交与行为改动分离（最近历史里有多个 `NFC.` 提交）。混在一起会让 review 无法判断哪部分改变了行为。

6. **错误直觉**：PR 开完等 merge 就行。
   **正确认识**：一周没回应可以 ping 一次；批准后如果自己没 merge 权限，要**明确请 reviewer 代 merge**——reviewer 默认以为你能自己合。

---

## 22.12 延伸阅读

1. **`llvm/docs/Contributing.md`（90–160 行）**
   读什么：GitHub PR 流程、reviewer 选择、ping 频率、squash merge、线性历史。
   学什么：一次贡献的官方规则；哪些是硬性要求、哪些是礼貌。
   和本章的关系：22.1 与 22.5 的政策依据。

2. **`llvm/docs/RFCProcess.md`（全文约 60 行）**
   读什么：RFC 的四段结构、反馈期、rough consensus、增量落地。
   学什么：什么时候需要 RFC；RFC 里必须写什么。
   和本章的关系：22.4 的流程依据。

3. **commit `b4effb258020`（`TableGen: Cache resolved class instantiations`）与 `05113387a938`（`Remember superclass results`）**
   读什么：commit message 里的 before/after 表、宏观/微观两套 benchmark、变更说明。
   学什么：上游接受性能改动的证据标准。
   和本章的关系：22.3.4 的模板来源。

4. **commit `fcee33940765`（`Store template arguments separately from fields`）**
   读什么：破坏性语义改动的描述方式——「唯一输出变化是什么」「内存降 1%」。
   学什么：怎么把一个影响面很大的改动写得让 reviewer 敢收。
   和本章的关系：22.7 的范例。

5. **`llvm/Maintainers.md` 的 TableGen 段**
   读什么：当前 maintainer 与联系方式。
   学什么：reviewer 的第一选择；也提醒你「这个区域有人负责」。
   和本章的关系：22.1 的 reviewer 策略。

---

## 下一章

[Final Project：写一个可提交的 TableGen backend](final-project-contributable-backend.md)

前面都是「改 TableGen 本身」或「读懂它」。最后一个项目反过来：用你学到的 `Record` API、类型系统、折叠、错误报告、测试与性能方法，从零写一个**有真实用途、能被上游接受**的 backend，并走完本章的 PR 流程。
