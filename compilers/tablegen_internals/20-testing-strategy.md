# 第 20 章：测试策略——`llvm/test/TableGen` 的 496 个测试怎么用

> **目标**：把 `llvm/test/TableGen` 当成一套可读、可扩展的规格说明，而不是「跑绿就行」的黑盒。学完你能判断：一个改动该写哪种测试（默认 backend / `-gen-*` / 负向）、怎么用 `FileCheck` 锁定语义、`split-file` 解决什么问题，以及为什么 496 个测试仍然漏掉了 `-print-records` 的 round-trip bug（练习 B 的真实发现）。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。统计与输出均在本机实跑：`/tmp/tgbuild/bin/llvm-lit llvm/test/TableGen`（496/496 通过，约 13 s）。源码树未修改。

---

## 20.1 这套测试的规模与组织

先看真实数字：

```console
$ find llvm/test/TableGen -name '*.td' | wc -l
501
$ ls llvm/test/TableGen/*.td | wc -l
399
$ llvm-lit llvm/test/TableGen 2>&1 | tail -3
Total Discovered Tests: 496
  Passed: 496 (100.00%)
```

- **501 个 `.td` 文件**，其中根目录 399 个，其余在 6 个子目录（`DecoderEmitter/` 19、`GlobalISelEmitter/` 43、`GlobalISelCombinerEmitter/` 27、`SDNodeInfoEmitter/` 8、`CompressInstEmitter/` 1 等）。
- **`Common/` 下的 4 个 `.td` 不是测试**，是被其他测试 `include` 的公共片段。
- lit 实际发现 **496** 个测试。

配置只有几行：

```python
# llvm/test/TableGen/lit.local.cfg
import platform
import lit.formats

config.suffixes = [".td"]
config.excludes = ["Common", "Inputs"]

if platform.system() == "AIX":
    config.test_format = lit.formats.ShTest()
```

含义：**只有 `.td` 会被当成测试**；`Common/` 与 `Inputs/` 整个目录跳过。这也解释了一个常见困惑——你在 `Common/` 里加个 `.td`，lit 不会跑它，它只是被 `include` 的素材。

---

## 20.2 三种测试形态

按测试驱动的东西分，501 个文件大致是：

| 形态 | 文件数 | 驱动方式 | 主要抓什么 |
|---|---:|---|---|
| 默认 backend（`-print-records`） | 203（其中 81 个负向） | `llvm-tblgen %s` | parser、`Record`、`Init`、诊断 |
| 指定 backend | 298 | `llvm-tblgen -gen-xxx %s` | 某个 emitter 的输出 |
| 负向（`not llvm-tblgen`） | 170 | `not llvm-tblgen ... 2>&1 \| FileCheck` | 错误消息、位置、是否只报一条 |

统计命令（可复现）：

```console
$ grep -rL --include='*.td' -e '-gen-' llvm/test/TableGen | wc -l   # 203
$ grep -rl --include='*.td' -e '-gen-' llvm/test/TableGen | wc -l   # 298
$ grep -rl 'not llvm-tblgen' llvm/test/TableGen --include='*.td' | wc -l  # 170
```

### 20.2.1 形态一：默认 backend

不写 `-gen-*` 时，`llvm-tblgen` 走默认 backend，即 `-print-records`（`Basic/TableGen.cpp:59-61`，`ByDefault=true`）。最小的例子：

```text
// llvm/test/TableGen/isa.td
// RUN: llvm-tblgen --no-warn-on-unused-template-args %s | FileCheck %s

// CHECK: --- Defs ---

// CHECK: def X0 {
// CHECK:   int ret = 0;
// CHECK: }
```

```console
$ llvm-lit -v llvm/test/TableGen/isa.td
PASS: LLVM :: TableGen/isa.td (1 of 1)
```

这类测试适合：

- **语言语义**：`!isa`、`!cast`、`!foldl`、模板参数、继承、multiclass 展开；
- **Record 形状**：某个字段最终值是什么、匿名记录叫什么、打印顺序；
- **诊断**：错误在哪一行哪一列、消息文本。

它**不适合**验证某个 backend 的具体输出——那是形态二。

### 20.2.2 形态二：指定 backend

```text
// RUN: llvm-tblgen -gen-register-info -I %p/../../include %s | FileCheck %s
```

`-gen-*` 测试的 298 个文件里，最常见的 backend 是：

```console
$ # 按“包含该 backend 的测试文件数”统计（一个文件可能测多个 backend）
$ for f in global-isel register-info intrinsic-impl disassembler \
           dag-isel runtime-libcalls global-isel-combiner amdgpu-target-def; do
    printf '%3d -gen-%s\n' \
      "$(grep -rlP --include='*.td' -- "-gen-$f(?!-)" llvm/test/TableGen | wc -l)" "$f"
  done
 50 -gen-global-isel
 31 -gen-register-info
 14 -gen-intrinsic-impl
 25 -gen-disassembler
 22 -gen-dag-isel
 20 -gen-runtime-libcalls
 26 -gen-global-isel-combiner
  4 -gen-amdgpu-target-def
```

这类测试适合：

- emitter 生成代码的**形状**（函数签名、表项顺序、`ifdef` 段）；
- 某个 target 专属 backend 的校验逻辑（如 `-gen-amdgpu-target-def`）；
- 生成代码能否被 C++ 编译（更重的验证在 `llvm/test/CodeGen`，不在这个目录）。

### 20.2.3 形态三：负向

```text
// RUN: not llvm-tblgen %s 2>&1 | FileCheck %s
```

`not` 是 lit 的工具替换（`llvm/test/lit.cfg.py` 里的 `ToolSubst`），它把「非零退出」变成「成功」，于是你可以对错误消息做 `FileCheck`。

最讲究的负向测试会加 `--implicit-check-not`，保证**没有多余的 error**：

```text
// llvm/test/TableGen/template-arg-duplicate.td
// RUN: not llvm-tblgen -DCLASS %s 2>&1 | FileCheck %s --check-prefix=CLASS
// RUN: not llvm-tblgen -DMULTICLASS %s 2>&1 | FileCheck %s --check-prefix=MULTICLASS
// RUN: not llvm-tblgen -DTYPE %s 2>&1 | FileCheck %s --check-prefix=TYPE --implicit-check-not=error:
```

第三条 RUN 的意思是：除了 `TYPE:` 明确匹配的那一条错误，输出里**不允许再出现任何 `error:`**。这正好锁住了第 19 章那个行为——「重复参数且类型不同时只报一条错」。

---

## 20.3 lit 的替换符：`%s`、`%t`、`%p`

| 替换符 | 含义 | 典型用法 |
|---|---|---|
| `%s` | 当前测试文件路径 | `llvm-tblgen %s` |
| `%t` | 每个测试独立的临时路径（可当目录用） | `split-file %s %t`、`-o %t/out.inc` |
| `%p` | 当前测试所在目录 | `-I %p/../../include` |
| `%llvm-tblgen` | 无（本套测试不用它） | 直接用 `llvm-tblgen`，由 lit 做 PATH 替换 |

实测：`llvm/test/TableGen` 下 **362 个文件**用 `RUN: llvm-tblgen`，**0 个**用 `%llvm-tblgen`。工具替换来自 `llvm/test/lit.cfg.py` 的 `ToolSubst` 列表（第 308 行附近列了 `llvm-tblgen`），所以你写 `llvm-tblgen` 就会命中构建目录里的那个二进制。

`%t` 的两个关键性质：

1. **每个测试唯一**，可以并行跑（`lit -j`）；
2. **是路径前缀，不保证已存在**。要当目录用，先 `rm -rf %t && mkdir %t`，或者用 `split-file %s %t`（它会创建目录）。

---

## 20.4 `split-file`：一个测试文件装多个输入

6 个测试用了 `split-file`。它的作用是把一个 `.td` 按 `//--- name` 切成多个文件，每个文件可以独立喂给 `llvm-tblgen`。`AMDGPUTargetDefErrors.td` 是典型：

```text
// RUN: rm -rf %t && split-file %s %t
// RUN: not llvm-tblgen -gen-amdgpu-target-def -I %p/../../include %t/bad-alias.td 2>&1 \
// RUN:   | FileCheck %t/bad-alias.td -DFILE=%t/bad-alias.td --implicit-check-not="error:"
...
//--- bad-alias.td
include "llvm/Target/TargetSubtarget.td"
class AMDGPUGPUInfo<list<int> isa = []> { ... }
def : ProcessorModel<"gfx900", NoSchedModel, []>, AMDGPUGPUInfo<[9, 0, 0]>;
// CHECK: [[FILE]]:[[#@LINE+1]]:1: error: ProcessorAlias 'foo' aliases 'gfx-missing' ...
def : ProcessorAlias<"foo", "gfx-missing">;
```

三个技巧值得学：

1. **`-DFILE=%t/bad-alias.td` + `[[FILE]]`**：错误消息里的绝对路径随临时目录变化，用变量接住，避免写死。
2. **`[[#@LINE+1]]`**：断言错误在「下一行」。这样你在上面插入注释/字段，行号自动跟着走，测试不会脆。
3. **`split-file` + 多个 RUN**：一个文件测 10 种错误，共享同一套 `class` 定义，但每种错误独立运行——比 10 个文件好维护，也比一个大文件「一次报 10 个错」更容易精确定位。

**什么时候用 `split-file`**：

- 多个负向用例需要相同的 preamble；
- 一个用例需要多个输入文件（比如 `include` 场景）；
- 你想让「每个用例独立进程」，避免错误之间互相影响。

**什么时候不用**：单个正向测试。直接 `llvm-tblgen %s` 最省事。

---

## 20.5 `FileCheck` 的四种断言

`llvm/test/TableGen` 里出现频率最高的指令：

| 指令 | 文件数 | 语义 |
|---|---:|---|
| `FileCheck` | 471（几乎所有） | 按顺序匹配一行 |
| `CHECK-NOT` | 32 | 在两个 CHECK 之间不允许出现 |
| `CHECK-DAG` | 11 | 一组匹配，顺序无关 |
| `CHECK-COUNT` | 1 | 精确匹配次数 |

对 TableGen 测试，最常用的组合是：

```text
// CHECK-LABEL: def Foo {
// CHECK-NEXT:    int x = 1;
// CHECK-NEXT:    int y = 2;
// CHECK: }
```

- `CHECK-LABEL` 把匹配切成独立区间，失败时报告「哪个 def 没匹配上」；
- `CHECK-NEXT` 锁住**紧邻下一行**，适合验证打印顺序（TableGen 的 `-print-records` 顺序是稳定契约）；
- 只用 `CHECK` 会允许中间插入任意行，适合「只关心存在性」。

**不要过度使用 `CHECK-NEXT`**：如果你在字段之间加了一个新字段，相邻的测试会全红。上游的惯例是「顺序重要时用 NEXT，否则用 CHECK」。

### 20.5.1 一个真实的反例

练习 B 发现 `-print-records` 的字符串不转义：`StringInit::getAsString()`（`Record.h:752-757`）直接输出原值。这意味着：

```text
def D { string s = "a\"b"; }
```

打印回来是 `string s = "a"b";`——**无法重新解析**。而 `llvm/test/TableGen` 里没有 round-trip 测试，所以 496 个测试全绿也发现不了。这个 bug 是练习 B 用四个真实 target、`cmp` + `md5` 逐字节对比才暴露的。

**教训**：测试形态决定你能发现什么。`-print-records | FileCheck` 只验证「输出包含某些行」，不验证「输出能被重新读入」。

---

## 20.6 什么时候写哪种测试：决策表

| 你改了什么 | 首选测试 | 例子 |
|---|---|---|
| lexer/parser 的语法 | 默认 backend 正向 | `isa.td`、`listconcat.td` |
| 类型检查/诊断 | 默认 backend 负向 | `template-arg-duplicate.td` |
| `Record` 语义 | 默认 backend + `--print-detailed-records` | `detailed-records.td` |
| 某个 emitter | 该 `-gen-*` 正向 | `-gen-register-info`、`-gen-dag-isel` |
| emitter 的错误处理 | `-gen-*` 负向 + `split-file` | `AMDGPUTargetDefErrors.td` |
| 性能/内存 | **不是 lit 的职责** | 用脚本 + `/usr/bin/time`（Ch 17/21） |
| 生成代码能编译 | `llvm/test/CodeGen/<target>` | 不在 `test/TableGen` |

这张表的关键行是最后两行：**lit 只验证「文本/退出码」，不验证性能，也不验证生成代码的可编译性**。后者由真实 target 的构建和 `test/CodeGen` 覆盖。

---

## 20.7 496 个测试的覆盖缺口

这套测试很强，但有三个结构性盲区：

### 20.7.1 真实 target 的 `.td` 只被「构建」覆盖

`llvm/test/TableGen` 用的是**小型 mock `.td`**（几十行），不是 AArch64/X86 那种几十万条记录的真实输入。真实 `.td` 的验证发生在**构建 LLVM 时**：`llvm-tblgen` 必须成功生成所有 `.inc`，否则整个 build 失败。

这意味着：

- 语法错误、类型错误 → build 会抓到；
- **性能退化** → build 不会抓到（10 s 变 30 s 只是变慢）；
- **生成代码的形状退化** → build 不会抓到，除非导致编译错误；
- **语义细微变化**（比如某个字段含义变了）→ build 完全抓不到。

练习 B 的 63 MB 输出、练习 D 的四个 target 统计，都是在补这个盲区。

### 20.7.2 没有 fuzzing

TableGen 的 parser 没有 libFuzzer target。相比之下，LLVM IR parser 有 `llvm/test/fuzzer` 之类。这意味着：

- 畸形输入（超长嵌套、深递归 `!foldl`、巨大 `bits<N>`）主要靠人工测试；
- 崩溃类 bug（assertion、栈溢出）不容易提前发现。

如果你要参与上游，**「给 TableGen 加一个 fuzzer」本身就是有价值的贡献方向**——但要注意它需要新的 build target 和 CI 配置，不是一个小 PR。

### 20.7.3 输出格式的「稳定性」没有统一约定

`-print-records` 的顺序、匿名记录命名、缩进，都是事实上的契约，但没有一份文档说「这些保证不变」。测试里大量 `CHECK-NEXT` 隐式锁定了它们。改打印格式时，你会看到几十个测试一起红——这是好事（提醒你格式是契约），也是坏事（很难判断哪些红是预期的）。

---

## 20.8 加一个测试的标准流程

1. **先决定形态**：默认 backend 还是 `-gen-*`？正向还是负向？参考 20.6。
2. **写最小输入**：能触发目标行为的最小 `.td`，不要复制整个 target。
3. **写 RUN 行**：正向用 `llvm-tblgen %s | FileCheck %s`；负向用 `not llvm-tblgen %s 2>&1 | FileCheck %s`。
4. **用 `CHECK-LABEL` 分段**：每个 def/class 一个 label，失败信息才可读。
5. **负向测试加 `--implicit-check-not=error:`**：确保只报预期的错误。
6. **跑单测**：`llvm-lit -v <file>`，先确认它 PASS。
7. **故意改坏实现**（可选但强烈建议）：确认测试会 FAIL——否则你写的是「永远绿」的测试。
8. **跑全量**：`llvm-lit llvm/test/TableGen`，确认数字 = 基线 + 新增。

第 7 步是区分「写了测试」和「测试有效」的关键。练习 D 里我们就用「删掉 backend 注册行 → `--help` 里没有 `--gen-isa-stats`」验证过测试确实会失败。

---

## 20.9 常用命令

```console
# 全量
$ llvm-lit llvm/test/TableGen

# 单个文件，显示详细
$ llvm-lit -v llvm/test/TableGen/isa.td

# 只跑名字含 instr-info 的
$ llvm-lit -v --filter='instr-info' llvm/test/TableGen

# 看某个测试实际执行的命令（不运行）
$ llvm-lit -a llvm/test/TableGen/isa.td    # -a 显示输出
$ llvm-lit --show-tests llvm/test/TableGen | wc -l
```

`--show-tests` 在本机返回 **497**，而实际运行报告 **496**——差 1 个是 lit 的发现/执行口径差异（`Common/` 的 4 个已排除）。**以运行输出的 `Total Discovered Tests` 为准**，不要拿 `find | wc -l` 对账。

---

## 20.10 检查清单

写/审一个 TableGen 测试时：

- [ ] RUN 行用了正确的形态（默认 / `-gen-*` / `not`）
- [ ] `%s`、`%t`、`%p` 用对了；需要目录时先 `rm -rf %t`
- [ ] 正向测试有 `CHECK-LABEL` 分段
- [ ] 负向测试锁定了行号/列号（`[[#@LINE+1]]`）和消息文本
- [ ] 负向测试加了 `--implicit-check-not=error:`（如果只期望一条错）
- [ ] 故意改坏实现时测试会红
- [ ] 全量数字 = 基线 + 新增
- [ ] 没有把 `Common/` 当测试目录
- [ ] 知道这个测试**不**覆盖什么（性能、真实 target、可编译性）

---

## 20.11 踩雷记录

1. **错误直觉**：`llvm/test/TableGen` 里所有 `.td` 都会被 lit 跑。
   **正确认识**：`lit.local.cfg` 排除了 `Common/` 和 `Inputs/`。`Common/` 下的 4 个文件是 `include` 片段，不是测试。

2. **错误直觉**：不写 `-gen-*` 就没有 backend，测试只验证解析。
   **正确认识**：默认 backend 是 `-print-records`（`ByDefault=true`），它会遍历并打印整个 `RecordKeeper`。所以 203 个「无 `-gen-`」文件其实在验证**打印 + 记录模型**，覆盖面比想象中大。

3. **错误直觉**：`not llvm-tblgen ... | FileCheck` 只要匹配到错误行就算通过。
   **正确认识**：默认允许多余错误。要锁「只有一条错」，必须加 `--implicit-check-not=error:`；否则实现多报一条错，测试仍然绿。

4. **错误直觉**：`%t` 是一个已存在的临时目录。
   **正确认识**：`%t` 只是唯一路径前缀。直接 `-o %t/out.inc` 可能失败，因为目录不存在；`split-file %s %t` 会创建它，或者你自己 `mkdir`。

5. **错误直觉**：496 个测试全绿，说明 `-print-records` 的输出可信。
   **正确认识**：测试只做「包含匹配」，不做 round-trip。字符串不转义这种 bug（练习 B）可以完全逃过测试套件。

6. **错误直觉**：加测试就是加一个 `.td`，随便写个 `CHECK`。
   **正确认识**：有效的测试要能**在实现坏掉时失败**。写完先故意改坏一次，确认它红；否则你只是增加了一个永远通过的断言。

---

## 20.12 延伸阅读

1. **`llvm/test/TableGen/lit.local.cfg` + `llvm/test/lit.cfg.py`**
   读什么：后缀、排除目录、`ToolSubst` 列表、`%t`/`%p` 的替换实现。
   学什么：lit 如何发现测试、工具替换从哪来、为什么 `llvm-tblgen` 能直接写。
   和本章的关系：20.1–20.3 的所有规则都来自这两个文件。

2. **`llvm/test/TableGen/template-arg-duplicate.td`**
   读什么：三条 RUN、`-D` 分支、`--implicit-check-not=error:`。
   学什么：如何用 24 行锁定「重复参数只报一条错」这种微妙语义。
   和本章的关系：负向测试的范本。

3. **`llvm/test/TableGen/AMDGPUTargetDefErrors.td`**
   读什么：`split-file`、`-DFILE=`、`[[#@LINE+1]]`、10 个独立负向用例。
   学什么：一个文件如何组织多个互不干扰的错误场景。
   和本章的关系：`split-file` 的完整用法。

4. **`llvm/test/TableGen/GlobalISelEmitter/`（43 个文件）**
   读什么：大型 emitter 的测试如何分层（输入 pattern、期望的 match table、错误）。
   学什么：当 backend 输出几百行 C++ 时，`FileCheck` 怎么保持可维护。
   和本章的关系：形态二的规模化样本。

5. **练习 B：用 C++ 复刻 `-print-records`（逐字节一致版）**
   读什么：四个 target 的 `cmp`/`md5` 对比、round-trip 失败的四个原因。
   学什么：lit 抓不到的东西，怎么用外部脚本和真实输入抓到。
   和本章的关系：20.7 的三个盲区的实证。

---

## 下一章

[第 21 章：性能——parse、folding、变体爆炸与内存](21-performance.md)

测试能保证「对不对」，保证不了「快不快」。下一章进入性能：RISCV 的 `-gen-instr-info` 为什么吃 767 MB、`GenerateVariants` 的组合爆炸从哪来、`SetTheory` 的缓存 key 为什么会导致重复计算，以及怎么用 `--time-phases` 和 `/usr/bin/time` 把「感觉慢」变成可归因的数字。
