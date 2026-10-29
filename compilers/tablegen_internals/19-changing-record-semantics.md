# 第 19 章：改记录语义——兼容性、模板参数与「沉默的破坏」

> **目标**：学会判断「改 `Record` 的语义」和「加一个 backend」的风险差异；用一个真实的上游 commit（`fcee33940765`，2026-10-26，Jay Foad）拆解一次语义迁移：模板参数从 `Values` 里独立出去，`getValues()` 的契约从「字段 + 模板参数」收窄为「只有字段」。你会看到它为什么必须同时改 parser、打印、JSON、详细记录和 MLIR 的 LSP server，以及一个 39 行的运算符补丁为什么反而更安全。

> **环境**：LLVM `24.0.0git`（commit `ae47e0668aed`）。commit `fcee33940765` 已在当前历史中（`git merge-base --is-ancestor` 验证）。所有输出都在本机实跑：`/tmp/tgbuild/bin/llvm-tblgen`（assertions 构建）与一个链接 `libLLVMTableGenCommon` 的小探针。源码树未被修改，`git status` 为空。

---

## 19.1 什么叫「改记录语义」

先划清三类改动：

| 类型 | 例子 | 谁受影响 |
|---|---|---|
| **加东西** | 新增 `!min`/`!max` 运算符 | 只有用新语法的人 |
| **改契约** | `getValues()` 不再返回模板参数 | 所有 backend、所有工具、所有 `.td` 的读者 |
| **改语法** | 模板参数默认值的作用域规则 | 所有写 class 的人 |

第 18 章是第一种。本章讲第二、三种——它们才是「改源码」里真正危险的部分。

判断一次改动是不是「语义改动」，不要看 diff 大小，看这个问题：

> **有没有一个已经编译好的消费者，在代码不变的情况下，行为会改变？**

- `Record::getValues()` 的返回值集合变了 → 有。
- `Record::getTemplateArgs()` 的元素类型从 `const Init *` 变成 `RecordVal` → 有。
- 新增 `getTemplateArg()` → 没有（只要旧的还在）。
- `--print-detailed-records` 多打一行 `Fields: (none)` → 有（快照测试、脚本会受影响）。

---

## 19.2 案例：把模板参数从字段里搬走

### 19.2.1 旧世界

在 `fcee33940765` 之前，`Record` 的内部大致是：

```cpp
// 旧版（示意，来自 commit 的 diff 上下文）
SmallVector<const Init *, 0> TemplateArgs;  // 只有“名字”
SmallVector<RecordVal, 0> Values;           // 字段 + 模板参数的值
```

`Values` 同时装两类东西，靠 `TemplateArgs` 这个「名字列表」区分。后果是**每个消费者都要自己跳过模板参数**：

- 继承时：从父类复制 `Values`，再把模板参数删掉；
- 实例化时：复制、替换、再 `removeValue`；
- 打印时：两遍循环，各自判断 `!R.isTemplateArg(...)`；
- dump/JSON 同理。

commit message 里有一句很直白的话：

> `VarDefInit::instantiate` no longer copies template args from the class to the def only to remove them again.

翻译：以前实例化一个 def，要先把模板参数**复制过去**，再**删掉**。这是典型的「数据结构没表达出真实不变量，于是每个使用点都要补偿」。

### 19.2.2 新世界

```cpp
// llvm/include/llvm/TableGen/Record.h:1709（当前代码）
  // Template arguments, in order.
  SmallVector<RecordVal, 0> TemplateArgs;
  // Fields, including the ones inherited from superclasses.
  SmallVector<RecordVal, 0> Values;
```

API 也跟着分裂：

```cpp
// llvm/include/llvm/TableGen/Record.h:1818-1821
  /// Return the template arguments of this class or multiclass.
  ArrayRef<RecordVal> getTemplateArgs() const { return TemplateArgs; }

  /// Return the fields of this record.
  ArrayRef<RecordVal> getValues() const { return Values; }
```

以及一个新查询：

```cpp
/// Return the template argument with the given name, or null if there is
/// none.
const RecordVal *getTemplateArg(const Init *Name) const;
```

**注意签名**：`getTemplateArg` 只有 `const Init *` 重载，没有 `StringRef` 便利重载；而 `getValue` 两个都有（`Record.h:1873`）。这个不对称是有意的——模板参数的查找发生在 parser 内部，那里手里本来就是 `Init *`；给外部工具加 `StringRef` 重载没有需求。但它会让第一次写探针的人编译失败（见 19.4）。

### 19.2.3 新旧语义的实测差异

写一个最小探针（链接 `libLLVMTableGenCommon + libLLVMTableGenBasic + libLLVMTableGen + libLLVMSupport + libLLVMDemangle`）：

```cpp
// /tmp/tg19probe.cpp（节选）
RecordKeeper Records;
auto Buf = MemoryBuffer::getFile("/tmp/tg19.td");
SourceMgr Src;
Src.AddNewSourceBuffer(std::move(*Buf), SMLoc());
if (TableGenParseFile(Src, Records)) return 1;

const Record *D = Records.getDef("D");
outs() << "D values: " << D->getValues().size() << "\n";
outs() << "D template args: " << D->getTemplateArgs().size() << "\n";
outs() << "D->getValue(\"n\") = "
       << (D->getValue("n") ? "non-null" : "null") << "\n";
```

输入：

```text
class C<int n, string s = "x"> {
  int v = n;
}
def D : C<5>;
class Empty<int n> {}
def E : Empty<3>;
```

输出（省略 `unused template argument` 警告）：

```console
$ ./tg19probe
D values: 1
D template args: 0
D->getValue("n") = null
D->getTemplateArg("n") = null
  field: v = 5
C values: 1, args: 2
```

逐条读：

- **`D`（def）没有模板参数**：`template args: 0`。实例化时参数已经被替换进字段，def 不保留它们。
- **`D->getValue("n")` 是 null**：新语义下 `getValue` 只查字段，`n` 不是字段。
- **`C`（class）有 2 个模板参数、1 个字段**：`values: 1, args: 2`。`int v` 在 `Values` 里，`n`/`s` 在 `TemplateArgs` 里。
- 旧代码若遍历 `getValues()` 期望看到 `n`，现在会**静默少一项**——不报错、不崩，只是结果不对。这就是「沉默的破坏」。

### 19.2.4 输出层面的唯一变化

commit message 说：

> The only change in output is that `--print-detailed-records` prints `"Fields: (none)"` for a class with template arguments but no fields.

实测：

```console
$ llvm-tblgen --print-detailed-records /tmp/tg19.td
...
C  |tg19.td:1|
  Template args:
    int C:n = ?  |tg19.td:1|
    string C:s = "x"  |tg19.td:1|
  Superclasses: (none)
  Fields:
    int v = C:n  |tg19.td:2|

Empty  |tg19.td:5|
  Template args:
    int Empty:n = ?  |tg19.td:5|
  Superclasses: (none)
  Fields: (none)
```

`Empty` 只有模板参数、没有字段，所以打印 `Fields: (none)`。旧代码里模板参数也在 `Values` 中，`printFields` 会先打印 `Fields:` 标题、再在循环里把它们逐个跳过，结果是一个**标题下面什么都没有**的空段；新代码因为 `getValues()` 真的为空，走了 `Fields: (none)` 分支。**这一行就是这次迁移唯一的可见输出变化**——但快照测试会因此失败，所以 commit 里同步改了 `detailed-records.td`（+8 行）。

---

## 19.3 为什么 parser 也必须改

数据结构变了，解析顺序和诊断时机就跟着变。commit 里有两个不显然的语义决定：

### 19.3.1 模板参数的默认值里不能引用自己

新逻辑是「**先解析默认值，再把参数加进作用域**」：

```cpp
// TGParser::ParseDeclaration（commit 后，节选）
if (ParsingTemplateArgs) {
  ...
  if (!Rec->addTemplateArg(
          RecordVal(DeclName, IdLoc, Type, RecordVal::FK_TemplateArg)))
    return true;
}
```

于是下面这段合法：

```text
class Op;
def store : Op;

class StoreU<Op store = store> {
  Op Store = store;
}
```

实测输出：

```console
$ llvm-tblgen llvm/test/TableGen/template-arg-default-scope.td
class Op {
}
class StoreU<Op StoreU:store = store> {
  Op Store = StoreU:store;
}
...
def FromClass {	// StoreU
  Op Store = store;
}
```

`class StoreU<Op store = store>` 里的默认值 `store` 解析到的是**全局的 `def store`**，不是参数自己。如果先加参数再解析默认值，就会变成自引用（unset 或循环）。

同时，**前面的**模板参数在**后面的**默认值里是可见的：

```text
class Two<int a, int b = !add(a, 1)> { int B = b; }
def TwoFromClass : Two<5>;   // B = 6
```

这条「顺序可见性」规则不是新加的，但存储方式改变后必须显式保证——因为现在参数和字段分开存，作用域查找不能再靠 `Values` 的顺序。

### 19.3.2 重复参数在声明处诊断

旧代码要等到把值塞进 `Values`、发现同名时才报错，而且如果第二个参数类型不同，会先报一个**误导性的类型错误**。新代码在 `ParseDeclaration` 里用 `addTemplateArg` 的返回值直接诊断：

```console
$ cat /tmp/tg19dup.td
class C<int a, int a>;
$ llvm-tblgen /tmp/tg19dup.td
/tmp/tg19dup.td:1:16: error: template argument with the same name has already been defined
class C<int a, int a>;
               ^
```

```console
$ cat /tmp/tg19dup2.td
class D<int a, string a = "x">;
$ llvm-tblgen /tmp/tg19dup2.td
/tmp/tg19dup2.td:1:16: error: template argument with the same name has already been defined
class D<int a, string a = "x">;
               ^
```

第二种只报**一条**错误，而不是「类型不匹配 + 重复定义」。这两个行为都写进了新测试 `template-arg-duplicate.td`（24 行，用 `-DCLASS` / `-DMULTICLASS` / `-DTYPE` 三路 `FileCheck`）。

**教训**：改存储结构不只是「搬家」，它会强迫你重新回答「什么时候做检查」。这些回答必须写进测试，否则下一个改 parser 的人会把它们改回去。

---

## 19.4 兼容性影响面：谁在用 `Record` API

这次 commit 改了 10 个文件（`+226/−168`，其中 7 个生产代码、3 个测试）：

```console
$ git show --numstat fcee33940765
29   7   llvm/include/llvm/TableGen/Record.h
10  13   llvm/lib/TableGen/DetailedRecordsBackend.cpp
 4   6   llvm/lib/TableGen/JSONBackend.cpp
19  22   llvm/lib/TableGen/Record.cpp
84 117   llvm/lib/TableGen/TGParser.cpp
 1   1   llvm/lib/TableGen/TGParser.h
 8   0   llvm/test/TableGen/detailed-records.td
44   0   llvm/test/TableGen/template-arg-default-scope.td
24   0   llvm/test/TableGen/template-arg-duplicate.td
 3   2   mlir/lib/Tools/tblgen-lsp-server/TableGenServer.cpp
```

最后一行是重点：**MLIR 的 TableGen LSP server 也要改**。它不在 `llvm/` 下，但它 `#include "llvm/TableGen/Record.h"`，所以它也是「消费者」。

这就是判断影响面的方法——不是看目录，是看**谁 include 了头文件、谁调用了被改的 API**：

```console
# 找所有调用点（在源码树里）
$ grep -rn "getTemplateArgs()\|getValues()\|isTemplateArg" \
    llvm/lib llvm/utils mlir clang flang 2>/dev/null | wc -l
```

在本机当前树上，这个数字是 **334**。真正的上游改动还会遇到**树外**消费者：CIRCT、MLIR 的独立 build、各家 downstream backend。你无法 grep 到它们，只能靠：

1. 保守地保留旧 API（如果语义还能成立）；
2. 在 release notes 里写清迁移方式；
3. 用 `LLVM_ABI` / deprecation 机制给缓冲期（TableGen 的 API 通常不承诺稳定，但仍然要在 PR 描述里说明）。

---

## 19.5 决策：什么时候「改旧的」，什么时候「加新的」

这个 commit 的选择是：

- `getValues()`：**收窄语义**（只返回字段）。这是破坏性的。
- `getTemplateArgs()`：**改元素类型**（`const Init *` → `RecordVal`）。破坏性的。
- `getTemplateArg()`：**新增**。非破坏性的。

为什么敢收窄 `getValues()`？因为绝大多数调用者本来就想跳过模板参数——旧代码里到处是 `if (!isTemplateArg(...))` 的补偿。把「90% 的人想要的语义」变成默认，剩下 10% 用新 API，是合理的。

反过来，如果只有 10% 的调用者想要新语义，正确做法是**加新 API，旧 API 保持不变**。第 18 章的 `!min` 就是纯加法；而这个 commit 是「重新划边界」。

一个可操作的判断表：

| 问题 | 是 → | 否 → |
|---|---|---|
| 旧 API 的名字还准确描述新行为吗？ | 可以改 | 加新名字 |
| 大多数调用点是否本来就在补偿旧语义？ | 可以改 | 保留旧 API |
| 能否在编译期抓到所有调用点？ | 可以改（编译器帮你找） | 加新 API + 弃用 |
| 输出/序列化格式会变吗？ | 必须同步改测试和文档 | 风险较低 |

---

## 19.6 另一个方向的案例：加法式改动也会破坏消费者

不是只有「改 API」才危险。**改生成的代码形状**同样会破坏下游，因为 `.inc` 文件本身就是接口。

第 16 章量过一个具体产物：`GET_INSTRINFO_SCHED_ENUM`。

- `InstrInfoEmitter.cpp:1473` 用 `IfDefEmitter` **无条件**生成这个段；
- AArch64 的 `-gen-instr-info` 输出里，它占 **2,269 行 / 214,655 字节**；
- 全仓库只有 3 个 target 消费它：

```console
$ grep -rn "GET_INSTRINFO_SCHED_ENUM" llvm/lib/Target
llvm/lib/Target/Hexagon/MCTargetDesc/HexagonMCTargetDesc.h:114:#define GET_INSTRINFO_SCHED_ENUM
llvm/lib/Target/AMDGPU/MCTargetDesc/R600MCTargetDesc.h:35:#define GET_INSTRINFO_SCHED_ENUM
llvm/lib/Target/PowerPC/MCTargetDesc/PPCMCTargetDesc.h:188:#define GET_INSTRINFO_SCHED_ENUM
```

对 AArch64 / X86 / RISCV 来说，这两千多行是死代码：生成了、编译了、没人用。

如果你想「优化掉它」，正确的做法不是直接删——**树外 target 可能定义了同一个宏**。你需要：

1. 先在 `llvm-dev` 发 RFC，说明「这个段只有 3 个 in-tree 消费者」；
2. 给出迁移方案（比如改成按需生成，或移到单独的 backend）；
3. 保留一个 release cycle 的兼容期；
4. 在 release notes 里写清楚。

**这就是「加法式改动也会破坏消费者」的含义**：你以为只是少生成一段，但下游可能正好依赖它。判断标准和 19.1 一样——有没有一个代码不变的消费者，行为会变？

---

## 19.7 安全改语义的七步流程

把上面的经验压成流程。任何一次「改 `Record` 语义」都建议走完：

1. **写下旧契约**：用一句话描述 API 现在的保证，例如「`getValues()` 返回字段和模板参数」。写不出来说明你还没理解它。
2. **找所有读者**：`grep` 头文件 include、API 调用点；列出 in-tree 的 `llvm/`、`mlir/`、`clang/`、`flang/`，并标注树外风险。
3. **决定兼容策略**：改旧的 / 加新的 / 两阶段弃用。参考 19.5 的表。
4. **改数据结构，让不变量可表达**：像这个 commit 一样，把「模板参数不是字段」变成类型系统里的事实，而不是每个使用点的 `if`。
5. **同步所有输出面**：`-print-records`、`--print-detailed-records`、JSON、dump、诊断消息。输出也是 API。
6. **写三类测试**：行为（新语义）、输出（快照/FileCheck）、负向（错误消息和位置）。
7. **度量并记录**：内存、时间、输出大小；在 commit message / release notes 里写清「唯一的行为变化是什么」。

这个 commit 的第 7 步做得很好：commit message 明确写了「唯一输出变化是 `Fields: (none)`」和「AMDGPU 解析内存降约 1%」。**没有度量的性能声明等于没有声明**。

---

## 19.8 检查清单

改 `Record` 语义前，逐条确认：

- [ ] 能用一句话写出旧契约和新契约
- [ ] `grep` 过 `getValues` / `getValue` / `getTemplateArgs` / `isTemplateArg` 的所有调用点
- [ ] 检查了 `mlir/` 等「非 llvm/ 但在树内」的消费者
- [ ] 决定了旧 API 是收窄、保留还是弃用
- [ ] `--print-detailed-records`、JSON、dump 的输出变化已确认
- [ ] 新增/修改了行为测试、输出测试、负向测试
- [ ] 测过内存/时间，数字写进 commit message
- [ ] release notes 或迁移说明已写
- [ ] 四个真实 target 仍能生成（至少 `-null-backend` + 一个重 backend）

---

## 19.9 踩雷记录

1. **错误直觉**：`getValues()` 一直返回「所有值」，模板参数当然也在里面。
   **正确认识**：`fcee33940765` 之后它只返回字段。探针实测 `D values: 1, template args: 0`；旧代码遍历 `getValues()` 会静默少项，不报错。

2. **错误直觉**：def 实例化后应该保留模板参数，方便查询。
   **正确认识**：def 不保留。`D->getTemplateArgs().size() == 0`，`getValue("n")` 和 `getTemplateArg("n")` 都是 null；参数已经被替换进字段 `v = 5`。要查「这个 def 从哪个 class、用什么参数实例化」，得走 `VarDefInit` / 记录名，而不是 `Record`。

3. **错误直觉**：`getTemplateArg("n")` 应该和 `getValue("n")` 一样能编译。
   **正确认识**：`getTemplateArg` 只有 `const Init *` 重载（`Record.h:1853`），必须写 `getTemplateArg(StringInit::get(Records, "n"))`。这是本次探针真实踩到的编译错误，不是理论问题。

4. **错误直觉**：输出没变，就没有兼容性问题。
   **正确认识**：`--print-detailed-records` 对「有模板参数、无字段」的 class 会打印 `Fields: (none)`；JSON backend 也改了 10 行。快照测试和下游脚本都会受影响。

5. **错误直觉**：只改 `llvm/` 就够，`mlir/` 是另一个项目。
   **正确认识**：`mlir/lib/Tools/tblgen-lsp-server/TableGenServer.cpp` 直接 include `llvm/TableGen/Record.h`，这次 commit 必须同步改它（+3/−2）。影响面看 include，不看目录。

6. **错误直觉**：改存储结构只是重构，行为不变。
   **正确认识**：它改变了作用域规则（默认值不能引用自己）和诊断时机（重复参数在声明处报错，且不同型也只报一条）。这些都被写进了两个新测试，说明它们是**有意的语义变化**，不是副作用。

---

## 19.10 延伸阅读

1. **commit `fcee33940765` 的完整 diff**
   读什么：7 个文件的每一处改动，尤其是 `TGParser.cpp` 的 `ParseDeclaration`（84+/117−）和 `Record.cpp` 的 `VarDefInit::instantiate`。
   学什么：一次语义迁移如何同时改数据结构、解析顺序、打印、测试。
   和本章的关系：这是全章的主案例，19.2–19.4 的所有结论都来自它。

2. **`llvm/include/llvm/TableGen/Record.h` 的 `Record` 类（1700–1900 行）**
   读什么：`TemplateArgs` / `Values` 两个 vector、`getTemplateArgs` / `getValues` / `getTemplateArg` / `getValue` 的注释。
   学什么：用类型和注释表达不变量；为什么 `getValue` 的注释要专门写「Use getTemplateArg() to look up template arguments」。
   和本章的关系：这是新契约的正式定义。

3. **`llvm/lib/TableGen/TGParser.cpp` 的 `ParseDeclaration` 与 `ParseTemplateArgList`**
   读什么：默认值何时解析、参数何时进作用域、`addTemplateArg` 失败如何处理。
   学什么：解析顺序本身就是语义；作用域规则必须用测试锁定。
   和本章的关系：解释 19.3 的两个行为变化。

4. **`llvm/test/TableGen/template-arg-default-scope.td` 与 `template-arg-duplicate.td`**
   读什么：44 行和 24 行的测试，如何用 `-D` 分支测三种重复参数场景。
   学什么：语义改动的测试应该长什么样；负向测试如何用 `--implicit-check-not=error:` 保证只有一条错误。
   和本章的关系：这是「第 6 步：写三类测试」的范本。

5. **`mlir/lib/Tools/tblgen-lsp-server/TableGenServer.cpp`**
   读什么：它如何用 `RecordKeeper` 做补全/跳转。
   学什么：树内跨项目的消费者长什么样；为什么改 `Record.h` 要考虑 MLIR。
   和本章的关系：19.4 的「影响面看 include」的实证。

---

## 下一章

[第 20 章：测试策略——`llvm/test/TableGen` 的 496 个测试怎么用](20-testing-strategy.md)

本章已经多次提到「测试锁定语义」。下一章把 `llvm/test/TableGen` 拆开看：它到底覆盖了什么、`split-file` 和 `not` 怎么用、什么时候该写 `-gen-*` 测试而不是 `-print-records`，以及为什么 496 个测试仍然抓不住某些 bug。
