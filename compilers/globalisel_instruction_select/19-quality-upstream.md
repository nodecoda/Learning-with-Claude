# 第 19 章 质量与上游标准

> **本章目标**：把“这个 GlobalISel pattern 能跑”提升到“这个 pattern 可以进上游”。你会得到一套 review checklist、可复制的 lit/MIR 测试骨架、上游命令，以及判断 compile-time 代价的方法。
>
> **前置**：第 5–10、16–18 章。**环境**：LLVM 23.1.4，AArch64 主线。本章的 `llc` 输出与 `-time-passes` 在本机官方预编译包上实测；`ninja`、`llvm-lit`、`update_*_test_checks.py` 因为本机没有源码构建目录，只给标准命令并标注“未运行”。

---

## 19.1 质量模型：正确性优先，fallback 是契约的一部分

一个 GlobalISel pattern/selector 的质量可以拆成八个维度。它们不是并列的“加分项”：正确性是门槛，fallback 行为和测试是证据，compile-time 是预算。

| 维度 | 核心问题 | 失败后果 |
| --- | --- | --- |
| 正确性 | 选出的指令在所有权值/边界上都等价于 gMIR | 静默错误码 |
| fallback | 不支持的输入是否干净地返回 false | 崩溃、半选状态、整函数退化 |
| 类型/bank | LLT、寄存器 bank、regclass 是否一致 | 规则不命中或 verifier 失败 |
| 立即数边界 | 谓词是否精确覆盖编码能力 | 非法编码、截断、多指令 |
| 内存/原子 | MMO、ordering、对齐、地址空间是否保留 | 内存模型错误 |
| feature gating | subtarget 不支持的指令是否绝不会被选 | 在老 CPU 上生成非法指令 |
| 测试覆盖 | 正向、负向、边界、feature 变体是否都有 | 回归无法被发现 |
| compile-time | matcher 表大小、pattern 数量、谓词复杂度是否可控 | 编译变慢，review 被拒 |

### 19.1.1 正确性不等于“lit 过了”

一个 pattern 可能让 lit 通过，但在以下输入上出错：

- 立即数刚好在边界外；
- 操作数在另一个 bank；
- 指针地址空间不是 0；
- 访问是 atomic/volatile；
- 目标没有对应 feature；
- 结果有多个 use，折叠破坏了生产者。

所以测试必须**按维度**设计，而不是只挑一个“典型”输入。

---

## 19.2 Review checklist：每个维度都要有证据

下面这份表可以直接贴进 PR 描述。每一行的“证据”是 reviewer 应该能在 diff 或测试里看到的东西。

| # | 检查项 | Reviewer 要问 | 期望证据 |
| --- | --- | --- | --- |
| 1 | 语义等价 | 新指令在所有操作数组合上都等价于原 gMIR？ | 至少一个正向 lit；边界/特殊值 |
| 2 | fallback | 不匹配时是否 `return false`，而不是 assert/半改？ | 负向测试；`-global-isel-abort=1` |
| 3 | 类型 | LLT 是否精确匹配？是否处理指针/向量？ | MIR 里能看到 `sN`/`p0`/`v` |
| 4 | bank | 规则是否要求正确 bank？跨 bank COPY 是否处理？ | GPR/FPR 各一个测试 |
| 5 | 立即数 | 谓词是否覆盖编码边界？是否有符号扩展陷阱？ | 边界值正负向测试 |
| 6 | 内存/原子 | MMO 的 size/align/ordering/addrspace 是否保留？ | atomic/volatile 测试 |
| 7 | feature | 新指令是否被 `Predicates`/`Requires` 正确 gate？ | 有/无 feature 两套 RUN |
| 8 | 测试 | 是否同时有 MIR 和 asm 断言？是否 `-verify-machineinstrs`？ | lit 文件 + RUN 行 |
| 9 | compile-time | pattern 数量/谓词复杂度是否合理？ | matcher 表大小或 `-time-passes` |
| 10 | 可读性 | 是否能复用 `PatFrag`/`ImmLeaf`，而不是复制粘贴？ | diff 里没有大段重复 |

### 19.2.1 fallback：不要用 assert 处理合法输入

assert 只用于“编译器自身不变量”，不用于“用户输入可能长这样”。合法但暂不支持的输入应 `return false`，由 pass 决定 fallback 或 abort；用 assert 会把 fallback 变成崩溃。

### 19.2.2 类型与 bank：看 gMIR，不看 IR

review 时要求作者贴 `-stop-after=regbankselect` 的 MIR，而不是只贴 IR。需要确认：

- `G_ADD` 的操作数是 `s64` 还是 `s32`；
- 是 `gpr` 还是 `fpr`；
- 指针是 `p0` 还是其他地址空间；
- 结果 vreg 是否在正确的 bank。

### 19.2.3 立即数：边界必须成对测试

以 `ADDXri` 为例，至少测试：

```text
4095  -> 直接 imm12
4096  -> imm12 + lsl #12
4097  -> 物化常量 + ADDXrr
```

如果 pattern 涉及有符号立即数，还要测试 `-1`、`-256`、`255`、`256` 这类符号扩展边界。第 7 章的 `ImmLeaf` 符号扩展陷阱在这里会直接变成 review 问题。

### 19.2.4 内存/原子：MMO 是第二套操作数

检查：

- `G_LOAD`/`G_STORE` 的 MMO 是否被 `cloneMemRefs` 或显式保留；
- atomic ordering 是否被读取；
- volatile 是否被保留；
- 对齐是否与目标指令要求一致；
- 地址空间是否被谓词限制。

一个常见的错误是“只检查 opcode 和类型，忘了 MMO”，结果 `acquire` 被选成普通 `ldr`。第 18 章已经用实测汇编展示过 `ldr`/`ldar`/`stlr` 的差别。

### 19.2.5 feature gating：没有 feature 就必须拒绝

TableGen 侧用 `Predicates` 或 `Requires`；C++ 侧用 `STI.hasXXX()`。review 时要看到：

- 有 feature 的 RUN 行；
- 无 feature 的 RUN 行；
- 无 feature 时不是“崩溃”，而是 fallback 或清晰的 diagnostic。

---

## 19.3 测试要求：MIR + lit，正向 + 负向 + 边界 + feature

### 19.3.1 一个正向 asm 测试

```llvm
; RUN: llc -mtriple=aarch64-linux-gnu -global-isel -global-isel-abort=1 \
; RUN:     -verify-machineinstrs -O0 < %s | FileCheck %s

define i64 @add4096(i64 %a) {
  %r = add i64 %a, 4096
  ret i64 %r
}

; CHECK-LABEL: add4096:
; CHECK:       add x0, x0, #1, lsl #12
; CHECK-NEXT:  ret
```

这个测试同时锁定了：

- 选择成功（`-global-isel-abort=1` 会让失败直接退出）；
- MIR 合法（`-verify-machineinstrs`）；
- 指令形态（`ADDXri` 的移位形式）。

### 19.3.2 一个负向 verifier 测试

下面是一份 `.mir` 测试的关键行（完整可跑文件见第 18 章 18.7）：

```yaml
# RUN: not llc -mtriple=aarch64-linux-gnu -O0 -run-pass=finalize-isel \
# RUN:     -verify-machineinstrs %s -o /dev/null 2>&1 | FileCheck %s \
# RUN:     --check-prefix=ERROR
...
%3:gpr64 = CSELXr %0, %1, 1
# ERROR: missing implicit register operand 'implicit $nzcv'
```

负向测试的价值是锁住“错误必须被抓住”，而不是锁住具体报错文本的每个字符。`--check-prefix` 只匹配关键片段，避免 LLVM 改一行措辞就碎。

### 19.3.3 一个 MIR 测试

```yaml
# RUN: llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
# RUN:     -global-isel-abort=1 -verify-machineinstrs %s -o - | FileCheck %s
--- |
  target triple = "aarch64-unknown-linux-gnu"
  define i64 @add1(i64 %a) {
  entry:
    %r = add i64 %a, 1
    ret i64 %r
  }
...
---
name:            add1
alignment:       4
isSSA:           true
legalized:       true
regBankSelected: true
selected:        false
tracksRegLiveness: true
registers:
  - { id: 0, class: gpr }
  - { id: 1, class: gpr }
  - { id: 2, class: gpr }
liveins:
  - { reg: '$x0', virtual-reg: '' }
body:             |
  bb.0.entry:
    liveins: $x0

    %0:gpr(i64) = COPY $x0
    %1:gpr(i64) = G_CONSTANT i64 1
    %2:gpr(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
...
# CHECK-LABEL: name: add1
# CHECK:       %[[DST:[0-9]+]]:gpr64sp = ADDXri %[[SRC:[0-9]+]], 1, 0
```

MIR 测试更适合锁定“选择阶段”的行为；asm 测试更适合锁定最终指令形态。两者不要互相替代。

### 19.3.4 不同 subtarget feature

`half fadd` 是一个最小例子。同一份 IR：

```llvm
define half @hadd(half %a, half %b) {
  %r = fadd half %a, %b
  ret half %r
}
```

实测：

```console
llc -mtriple=aarch64-linux-gnu -global-isel -O0 hadd.ll -o -
llc -mtriple=aarch64-linux-gnu -global-isel -O0 -mattr=+fullfp16 hadd.ll -o -
```

默认：

```asm
fadd s0, s0, s1
```

`+fullfp16`：

```asm
fadd h0, h0, h1
```

feature 测试必须成对出现，否则你无法证明 gate 真的生效。

### 19.3.5 如何避免脆弱的 CHECK

- 用 `CHECK-LABEL` 锁定函数边界，避免跨函数误匹配。
- 用正则捕获 vreg：`%[[DST:[0-9]+]]`，不要写死 `%2`。
- 只在序列稳定的地方用 `CHECK-NEXT`；中间可能插入 `COPY`/`KILL` 时用 `CHECK`。
- 不要检查物理寄存器分配结果，除非测试专门针对 RA。
- 不要匹配整段汇编；匹配指令和关键操作数。
- MIR 测试优先检查 opcode、bank、regclass，不检查无意义的空格。
- 对 diagnostic 测试只匹配关键短语，不匹配完整路径/行号。

反例：

```text
; 不好：vreg 编号、寄存器分配、注释都可能变
; CHECK: %2:gpr64sp = ADDXri %0, 1, 0
; CHECK: add x0, x0, #1
```

更好的写法：

```text
; CHECK-LABEL: name: add1
; CHECK: %[[DST:[0-9]+]]:gpr64sp = ADDXri %[[SRC:[0-9]+]], 1, 0
```

---

## 19.4 代码质量：pattern 可读性、复用与 C++ 边界

### 19.4.1 先问“能不能用 TableGen 表达”

优先级：

1. 能用现有 `Pat`/`PatFrag`/`ImmLeaf` 表达 → 用 TableGen；
2. 需要多指令、隐式操作数、状态查询 → C++；
3. 两者都能做 → 优先 TableGen，除非有明确的 compile-time 或可读性理由。

过度 C++ 的代价：匹配逻辑分散、fallback 行为难验证、GlobalISel 与 SelectionDAG 的 pattern 不一致。

### 19.4.2 用 PatFrag 命名谓词，而不是复制粘贴

示意（不是 AArch64 现有代码）：

```tablegen
def addsub_imm12 : ImmLeaf<i64, [{
  return Imm >= 0 && Imm < 4096;
}]>;

def addsub_imm12_shifted : ImmLeaf<i64, [{
  return Imm >= 0 && Imm < 4096;
}]>;
```

如果两个 pattern 共享同一个谓词，把它抽成 `PatFrag`/`ImmLeaf`，让名字表达语义。复制粘贴的谓词最容易在边界修复时只改一处。

### 19.4.3 constrain 的顺序

典型顺序：

1. 构建/改写目标指令；
2. 设置 opcode/操作数；
3. 调用 `constrainSelectedInstRegOperands` 或 `RBI.constrainGenericRegister`；
4. 确认所有 def 都有 regclass 后再 `return true`。

不要先 constrain 再改操作数；也不要返回 true 时留下未约束的 vreg。第 2 章的错误 `VReg has no regclass after selection` 就是后者。

### 19.4.4 错误处理

- 合法但不支持：`return false`；
- 内部不变量破坏：`assert` / `llvm_unreachable`；
- 已经改了 MIR 才发现不能选：尽量在修改前完成所有检查，避免半选状态；
- 需要多指令输出时，先构建完整序列，再删旧指令。

### 19.4.5 注释写“为什么”

```cpp
// 不好：重复代码
// Select ADDXri.

// 好：解释边界/契约
// ADDXri encodes a 12-bit unsigned immediate optionally shifted by 12.
// 4097 cannot be represented, so leave it to the generic constant path.
```

reviewer 不需要你解释 `setDesc` 做了什么；需要你解释为什么这条规则只在这个条件下成立。

---

## 19.5 上游流程：从本地 lit 到 review

> **未运行说明**：本机只有官方预编译包，没有 LLVM 源码构建目录，因此下面的 `ninja`/`llvm-lit`/`update_*` 命令没有实际执行。命令本身是 LLVM 23.1.4 的标准工作流。

### 19.5.1 构建与测试

```console
ninja -C build check-llvm-codegen-aarch64
llvm-lit -sv build/test/CodeGen/AArch64/GlobalISel/your-test.mir
llvm-lit -sv build/test/CodeGen/AArch64/GlobalISel
```

`check-llvm-codegen-aarch64` 会跑整个 AArch64 codegen 测试集；单文件 `llvm-lit` 适合迭代。提交前至少跑：

```console
ninja -C build check-llvm-codegen-aarch64
```

如果改动涉及 target-independent 的 matcher，再跑：

```console
ninja -C build check-llvm-codegen-global-isel
```

### 19.5.2 更新 CHECK

asm 测试：

```console
python3 llvm/utils/update_llc_test_checks.py \
    --llc-binary build/bin/llc \
    llvm/test/CodeGen/AArch64/GlobalISel/your-test.ll
```

MIR 测试：

```console
python3 llvm/utils/update_mir_test_checks.py \
    --llc-binary build/bin/llc \
    llvm/test/CodeGen/AArch64/GlobalISel/your-test.mir
```

更新脚本会重写 CHECK 行。**不要盲跑**：先看 diff，确认没有把错误输出固化成期望值。

### 19.5.3 格式化

```console
git clang-format --diff HEAD~1
git clang-format HEAD~1
```

`git clang-format` 只格式化改动行，避免整文件噪声。C++ 改动还要过 `clang-tidy`/编译器警告；TableGen 改动至少保证 `llvm-tblgen` 能生成。

### 19.5.4 review 常见问题

- “这个 pattern 在没有 `+feature` 时会不会也匹配？”
- “为什么不用 `PatFrag` 复用已有的谓词？”
- “`GIM_CheckType` 检查的是 LLT，不是 IR 类型；你确认过 `s32`/`s64` 吗？”
- “立即数边界测试在哪？”
- “fallback 时会不会留下半改的 MIR？”
- “atomic/volatile 的 MMO 保留了吗？”
- “这个 C++ case 能不能用 TableGen 表达？”
- “matcher 表增加了多少？有 compile-time 数据吗？”

### 19.5.5 commit message

```text
[AArch64][GlobalISel] Select ADDXri for shifted 12-bit immediates

The imported ADDXri pattern only covered the unshifted imm12 form, so
values like 4096 were materialized into a register before ADDXrr. Add a
predicate for the shifted form and cover the 4095/4096/4097 boundaries.

Tests: llvm/test/CodeGen/AArch64/GlobalISel/addsub-imm.ll
```

commit message 要回答“改了什么、为什么、怎么验证”，而不是复述 diff。

---

## 19.6 compile-time：matcher 表大小、pattern 数量、谓词复杂度

### 19.6.1 三个成本来源

| 来源 | 观察点 | 控制手段 |
| --- | --- | --- |
| pattern 数量 | `.td` 里新增的 `Pat`/`PatFrag` 数量 | 合并共享结构，复用 `PatFrag` |
| matcher 表大小 | `AArch64GenGlobalISel.inc` 的字节数/`GIM_*` 数量 | 避免无谓的候选规则和重复检查 |
| 谓词复杂度 | `GISelPredicateCode`、`ImmLeaf`、`ComplexPattern` | 把昂贵检查前移/缓存，避免每条规则重复 |

### 19.6.2 观察 matcher 表

```console
ninja -C build lib/Target/AArch64/AArch64GenGlobalISel.inc
wc -c build/lib/Target/AArch64/AArch64GenGlobalISel.inc
grep -c 'GIM_' build/lib/Target/AArch64/AArch64GenGlobalISel.inc
```

TableGen 的 `-stats` 需要 assertions 构建：

```console
build/bin/llvm-tblgen -gen-global-isel -stats \
    -I llvm/lib/Target/AArch64 -I llvm/include/llvm/Target \
    llvm/lib/Target/AArch64/AArch64.td -o /tmp/AArch64GenGlobalISel.inc
```

> 未运行：本机没有源码构建目录。`llvm-tblgen --help-hidden` 确认 23.1.4 支持 `-stats`，但该选项标注 “available with Asserts”。

### 19.6.3 观察运行时

```console
llc -mtriple=aarch64-linux-gnu -global-isel -O0 -time-passes in.ll -o NUL
```

本机在一个很小的多函数 IR 上实测（节选）：

```text
Total Execution Time: 0.0156 seconds (0.0058 wall clock)
   ---Wall Time---  --- Name ---
   0.0018 ( 30.6%)  AArch64 Assembly Printer
   0.0015 ( 26.2%)  Expand IR instructions
   0.0005 (  8.4%)  IRTranslator
   0.0003 (  4.4%)  InstructionSelect
```

小样本的绝对时间没有意义，但比例能告诉你瓶颈在哪。InstructionSelect 本身通常不是大头；matcher 表膨胀更多影响编译时间和代码体积。

### 19.6.4 什么时候需要担心

- 新增 pattern 让 `AArch64GenGlobalISel.inc` 明显变大；
- 同一 opcode 的候选规则从几条变成几十条；
- 谓词里做了昂贵的 `getVRegDef` 链式查找；
- 新增规则只影响极罕见的输入，却排在热门规则前面；
- `InstructionSelect` 在大型编译单元上占比明显上升。

如果只是多了一条精确的边界规则，通常不值得过度优化；如果新增了一族 pattern，应该给出表大小或 `-time-passes` 的前后对比。

---

## 19.7 两个可操作清单

### 19.7.1 新 pattern PR 清单

- [ ] 正向测试：目标指令出现，且 `-global-isel-abort=1` 通过。
- [ ] 负向测试：不该匹配的输入走 fallback/旧路径。
- [ ] 边界测试：立即数、位宽、bank 各至少一个边界。
- [ ] feature 测试：有/无 `+feature` 两套 RUN。
- [ ] atomic/volatile：MMO 保留，ordering 正确。
- [ ] `-verify-machineinstrs` 出现在 RUN 行里。
- [ ] CHECK 用 `CHECK-LABEL` + 正则，不写死 vreg。
- [ ] `git clang-format --diff` 无意外改动。
- [ ] `ninja check-llvm-codegen-aarch64` 通过。
- [ ] PR 描述里有 compile-time 影响（或明确说“无显著影响”）。

### 19.7.2 MIR 测试 triage 清单

- [ ] `failedISel` 是 `false`，不是只看 `selected`。
- [ ] MIR 的 `registers`/`liveins` 与内嵌 IR 一致。
- [ ] `-stop-after=regbankselect` 的 LLT/bank 与 pattern 预期一致。
- [ ] 失败时用 `-global-isel-abort=1` 复现，而不是静默 fallback。
- [ ] 需要断言时用 `-verify-machineinstrs`。
- [ ] 负向测试只匹配关键 diagnostic。
- [ ] 更新 CHECK 后人工 review diff。
- [ ] 测试文件名/目录符合 `GlobalISel/` 现有命名。

---

## 19.8 踩雷清单

1. **只测一个“典型值”**。立即数 bug 几乎都在边界上；至少测边界前、边界、边界后。
2. **只用 asm 测试，不用 MIR 测试**。asm 会把 RA/调度/伪指令展开混进来，定位成本高。
3. **用 `CHECK-NEXT` 锁整段输出**。中间插一条 `COPY` 就碎；只在稳定序列上用。
4. **把 `assert` 当 fallback**。合法输入触发 assert 是编译器 bug，不是“不支持”。
5. **没有 feature 负向测试**。老 CPU 上生成新指令，测试却全绿。

---

## 19.9 进阶：把质量指标自动化

在 CI 里同时跑 assertions 与 release 构建：前者抓 verifier/assert，后者抓静默截断。对新增 pattern 跑 `llvm-tblgen -stats` 并把 matcher 表大小作为 PR 附件；用 `-time-passes` 对大型 `.ll` 做前后对比；用 `-mattr=+feat/-feat` 矩阵确保 gate 双向有效；atomic/volatile 单独建测试目录。

---

## 19.10 动手练习

### 练习 19.1 写一个边界 lit

为 `add i64 %a, 4096` 写一个 asm lit 测试，要求同时匹配 `add x0, x0, #1, lsl #12` 和 `ret`，并加 `-verify-machineinstrs`。

### 练习 19.2 写一个负向测试

构造缺少 `implicit $nzcv` 的 selected MIR，用 `not llc ... -verify-machineinstrs` 写一个只匹配关键报错的 lit。

### 练习 19.3 检查 CHECK 脆弱性

找一份现有 AArch64 GlobalISel 测试，把其中的 vreg 编号写死，再跑一次；解释为什么它会在无关改动后失败。

### 练习 19.4 feature 矩阵

为 `half fadd` 写两个 RUN 行，分别带 `-mattr=+fullfp16` 和不带，断言 `fadd h` 与 `fadd s`。

### 练习 19.5 compile-time 记录

用 `-time-passes` 跑一个包含 100 个 `add` 的 IR，记录 `InstructionSelect` 占比；再故意增加一条宽 pattern（如果本地有源码构建），比较表大小。

---

## 19.11 重点整理

- 质量维度：正确性、fallback、类型/bank、立即数、atomic/内存、feature、测试、compile-time。
- 正向测试证明“能选”，负向测试证明“不会乱选”；两者缺一不可。
- MIR 测试锁选择阶段，asm 测试锁最终形态；都要 `-verify-machineinstrs`。
- CHECK 要锁指令与关键操作数，不锁 vreg 编号、RA 结果和整段输出。
- TableGen 优先，C++ 只用于多指令/隐式操作数/状态查询。
- constrain 在操作数最终确定之后；返回 true 前确保 def 有 regclass。
- 上游流程：`ninja check-llvm-codegen-aarch64`、`llvm-lit`、`update_*_test_checks.py`、`git clang-format`。
- compile-time 看 pattern 数量、matcher 表大小、谓词复杂度，不只看单次运行时间。

---

## 19.12 自我检核

1. 为什么只写正向测试不够？负向测试应该断言什么？
2. `CHECK-NEXT` 在什么情况下容易碎？替代方案是什么？
3. `assert` 和 `return false` 的边界在哪里？
4. 一个 pattern 需要哪些证据才能证明 feature gate 正确？
5. `constrainSelectedInstRegOperands` 应该放在什么位置？
6. matcher 表变大时，应该观察哪些指标？
7. 为什么 MIR 测试和 asm 测试不能互相替代？

---

## 19.13 延伸阅读

- `llvm/docs/GlobalISel/InstructionSelect.rst`：InstructionSelect 的官方契约与测试建议。
- `llvm/test/CodeGen/AArch64/GlobalISel/`：AArch64 GlobalISel 的真实测试语料，重点看 `instruction-select-*` 与 `legalizer-*`。
- `llvm/utils/update_llc_test_checks.py` / `update_mir_test_checks.py`：CHECK 更新脚本，读一遍参数说明。
- `clang/tools/clang-format/git-clang-format`：只格式化改动行的上游工具。
- `llvm/docs/TestingGuide.rst`：lit、FileCheck、`not`、`-verify-machineinstrs` 的通用约定。

---

## 下一章

[第 20 章 AArch64 源码全走读](./20-aarch64-source-walkthrough.md) —— 把本章的 checklist 落到真实源码：从 `select()` 的分派，到 `selectImpl()` 的生成物，再到 `AArch64InstrGISel.td` 的 pseudo 与 `AArch64InstrInfo.td` 的 pattern。
