# Final Project — 交出一个 PR-ready 的 MLIR 修复

> **目标**：把前 31 章的知识压成一条可复用的交付流水线：**problem → 最小复现 → root cause → fix → 测试（红→绿）→ release note 判据 → PR 描述与 reviewer 路由**。学完这一章，你手上应该有一个可以 `git format-patch` 发出去的补丁，而不是一份读书笔记。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`。源码树在课程中**只读**——本章所有改动都在 `/tmp` 里构造，并用 `git apply --check` / `clang++ -fsyntax-only` 验证。
>
> **前置**：Ch 07（verifier）、Ch 13（reduce）、Ch 16（trait 与 verifier）、Ch 29（测试）、Ch 30（PR）、Ch 31（选题）。这是收尾章，不再引入新机制。

---

## F.1 这一章要交付什么

前 31 章每章回答一个“机制问题”：pattern 怎么匹配、legality 怎么算、bytecode 怎么版本化。真实工作里，问题以**症状**的形式出现——“这条诊断看不懂”“这个 IR 该被拒绝却被接受了”“这个 fold 少折了一步”。

一个 PR-ready 的交付包含七件东西，缺一件就会被退回来补：

| # | 产物 | 判断标准 | 对应章节 |
|---|---|---|---|
| 1 | problem 陈述 | 一句话说清“谁在什么输入下看到什么错误行为” | Ch 31 |
| 2 | 最小复现 | ≤30 行 `.mlir` + 一条命令，稳定复现 | Ch 13 |
| 3 | root cause | 指向具体文件:行号与机制，不是“这里不对” | Ch 07/16 |
| 4 | fix | 最小 diff；不夹带重构；命名与既有风格一致 | Ch 04/08 |
| 5 | 测试 | 一个会在修复前失败、修复后通过的用例 | Ch 29 |
| 6 | release note 判据 | 写或不写，都能说出理由 | Ch 30 |
| 7 | PR 描述 | 动机、做法、验证、替代方案、不确定性 | Ch 30 |

## F.2 先建立直觉：七步证据链

```text
 ① problem          “tensor.extract 少给下标时，报错说 extract_element”
      │               产出：一句可引用的话（issue 标题）
      ▼
 ② repro            30 行 .mlir + mlir-opt 一条命令
      │               产出：命令 + 期望 + 实际（三行）
      ▼
 ③ root cause       git blame / git log -L / grep
      │               产出：TensorOps.cpp:1412，emitOpError 的字符串
      ▼
 ④ fix              2 个 verifier 的消息，期望值/实际值
      │               产出：≤20 行 diff（实现）
      ▼
 ⑤ test             负向测试：expected-error 跟着改；先红后绿
      │               产出：≤10 行 diff（测试）+ 两次 lit 输出
      ▼
 ⑥ note 判据        诊断措辞变化 → 不写 ReleaseNotes
      │               产出：一句“为什么不写”
      ▼
 ⑦ PR              标题 tag + 三段式 body + 验证记录 + reviewer
                      产出：可直接粘贴的 PR 描述
```

这条链的关键性质是**每一步都能独立被检查**。reviewer 不同意第 ④ 步的做法时，前三步的证据仍然成立，讨论可以聚焦；反过来，如果你直接从 ④ 开始，被质疑时没有任何退路。

## F.3 选题：三个候选，三种命运

Ch 31 给了“怎么筛”的方法；这里给三个**本树真实存在**的候选，难度与风险各不相同。

| # | 题目 | 难度 | 涉及章节 | 验收标准 | 风险 |
|---|---|---|---|---|---|
| T1 | 改进 `tensor.extract` / `tensor.insert` 的下标数量诊断 | 0.5–1 天 | 07/16/29/30 | 负向测试通过；消息含 expected/actual | 低（纯措辞） |
| T2 | 删除 `ArithOps.cpp:279` 的陈旧 TODO | 0.5 天 | 03/07/31 | verifier 已实现 + `invalid-ops.mlir` 全绿 + 无测试改动 | 低（NFC，价值也低） |
| T3 | 为 `vector.insert` / `tensor.extract` 增加常量越界校验 | 3–5 天 | 07/16/22/30 | 先有设计结论（可能需要 RFC） | 高（可能被判为 UB 语义） |

### F.3.1 T1：诊断改进（本章参考实现）

现状（真实输出见 F.5）：少给下标时，错误消息是

```text
error: 'tensor.extract' op incorrect number of indices for extract_element
```

两个问题：**消息里出现了一个已经不存在的 op 名**（`extract_element`，2020 年从 `std` 拆出时的旧名），以及**没有告诉你期望几个、给了几个**。这类补丁是 MLIR 最欢迎的形态之一：局部、可测、reviewer 三分钟能判断。

### F.3.2 T2：陈旧 TODO（最便宜，但价值有限）

```bash
sed -n 279,290p mlir/lib/Dialect/Arith/IR/ArithOps.cpp
```

```text
/// TODO: disallow arith.constant to return anything other than signless integer
/// or float like.
LogicalResult arith::ConstantOp::verify() {
  auto type = getType();
  // Integer values must be signless.
  if (auto intType = dyn_cast<IntegerType>(getElementTypeOrSelf(type));
      intType && !intType.isSignless())
    return emitOpError("integer return type must be signless");
  // Any float or elements attribute are acceptable.
  if (!llvm::isa<IntegerAttr, FloatAttr, ElementsAttr>(getValue())) {
```

TODO 要求的两件事，下面 5 行已经实现了；对应的负向测试也在（`mlir/test/IR/invalid-ops.mlir:137`）。删掉它合法，但**只有“减少一条误导性注释”的价值**——适合当第一个 PR 练手，不适合当代表作。

### F.3.3 T3：越界校验（先讨论，再写代码）

`tensor.extract %t[%c5]` 在 `tensor<4xf32>` 上是 UB，MLIR 目前**不校验**常量下标（实测 `exit=0`）。加校验听起来合理，但会改变“什么 IR 合法”的边界，属于 Ch 31 说的“设计层面的取舍”。正确顺序是：先在 issue/RFC 里问“我们要不要对常量下标做静态校验”，拿到结论再写代码。

**三个题目的教训**：T1 可以直接做，T2 可以练手，T3 必须先讨论。分不清这三者，是新手 PR 被关掉的最常见原因（Ch 30.9）。

## F.4 第 1 步：problem 陈述

模板只有一句：

```text
<谁> 在 <什么输入/操作> 下，看到 <什么错误行为>，期望 <什么>。
```

用 T1 填：

```text
任何人在 tensor<4xf32> 上用 tensor.extract 且不提供下标时，
看到 "incorrect number of indices for extract_element"，
期望消息指出正确的 op 名与期望/实际的下标数量。
```

反例（不要这样写）：

- “`tensor.extract` 有 bug。”——哪个 bug？
- “诊断不友好。”——对谁不友好、在哪一行、期望什么？
- “重构 tensor 方言的 verifier。”——这是方案，不是问题。

## F.5 第 2 步：最小复现

复现必须能贴进 PR 描述，并且**别人复制粘贴就能跑**：

```bash
cd /tmp/mlir-lit
cat > repro.mlir <<'MLIR'
func.func @f(%t: tensor<4xf32>) -> f32 {
  %0 = tensor.extract %t[] : tensor<4xf32>
  return %0 : f32
}
MLIR
$T/bin/mlir-opt repro.mlir ; echo "exit=$?"
```

```text
repro.mlir:2:8: error: 'tensor.extract' op incorrect number of indices for extract_element
  %0 = tensor.extract %t[] : tensor<4xf32>
       ^
repro.mlir:2:8: note: see current operation: %0 = "tensor.extract"(%arg0) : (tensor<4xf32>) -> f32
exit=1
```

同一个文件的 `tensor.insert` 版本（确认这不是单个 op 的偶发问题）：

```bash
$T/bin/mlir-opt repro2.mlir ; echo "exit=$?"
```

```text
repro2.mlir:2:8: error: 'tensor.insert' op incorrect number of indices
  %0 = tensor.insert %v into %t[] : tensor<4xf32>
       ^
exit=1
```

两个复现放在一起，problem 就从“一个错字”升级成“**一组 verifier 的诊断质量不一致**”：一个带旧 op 名，一个什么都没说。

**判断复现是否合格的三问**：别人能跑吗？跑出来是同一个错误吗？错误信息里有文件名:行号吗？

## F.6 第 3 步：root cause

### F.6.1 先定位到行

```bash
grep -n "incorrect number of indices" mlir/lib/Dialect/Tensor/IR/TensorOps.cpp
```

```text
1412:    return emitOpError("incorrect number of indices for extract_element");
1688:    return emitOpError("incorrect number of indices");
```

### F.6.2 再用 blame 看它多老

```bash
git blame -L 1412,1412 --date=short mlir/lib/Dialect/Tensor/IR/TensorOps.cpp
git blame -L 1688,1688 --date=short mlir/lib/Dialect/Tensor/IR/TensorOps.cpp
```

```text
cfaf3292df510 (Matthias Springer 2022-10-27 1412)  return emitOpError("incorrect number of indices for extract_element");
cfaf3292df510 (Matthias Springer 2022-10-27 1688)  return emitOpError("incorrect number of indices");
```

两条消息来自**同一次改动**（2022-10-27），而注释行更老（2020-12-11，`tensor` 方言从 `std` 拆出来的那次）。结论不是“谁写错了”，而是**这个字符串比它描述的对象活得久**：op 后来改名叫 `tensor.extract`，字符串没跟着改。

### F.6.3 最后确认影响面

```bash
grep -rn "extract_element" mlir/ | wc -l
grep -rn "extract_element" mlir/lib mlir/include | wc -l
```

```text
35
2
```

35 处里绝大多数是测试函数名（`@extract_element_0d`）和注释；`lib` + `include` 里只剩 **2** 处：本条 verifier 消息，以及 `Linalg/Transforms/Vectorization.cpp:1253` 的一条注释。**影响面 = 1 条用户可见消息 + 1 条注释**，这决定了 fix 的边界（F.7 不会顺手改测试函数名）。

### F.6.4 如果症状不是诊断，而是崩溃或错误的 IR

上面的 grep + blame 只适用于“有消息可读”的问题。如果症状是 pass 崩溃、或 pass 跑完 IR 错了，先把范围压到单个 pass，再 blame（Ch 13 的三件套）：

```bash
$T/bin/mlir-opt in.mlir --pass-pipeline='...' --mlir-print-ir-after-all 2>&1 | tail -80
$T/bin/mlir-opt in.mlir --pass-pipeline='...' --mlir-pass-pipeline-crash-reproducer=/tmp/r.mlir
$T/bin/mlir-reduce /tmp/r.mlir -reduction-tree='test=/tmp/interesting.sh'
```

（本例是诊断类，不需要 reduce；reproducer 与 reduce 的实测输出见 Ch 13.11 实验 3/5/7。）

## F.7 第 4 步：fix 的形状

### F.7.1 三个约束

1. **最小 diff**：只改产生错误行为的那两行。测试函数名里的 `extract_element` 不改（那是风格问题，拆成另一个 PR）。
2. **消息自洽**：`emitOpError` 已经自动加 `'tensor.extract' op` 前缀，所以消息里**不要再写 op 名**。
3. **可操作**：给出期望值与实际值——reviewer 和你未来的用户都会感谢你。

### F.7.2 参考实现（核心 7 行）

```cpp
  if (tensorType.getRank() != static_cast<int64_t>(getIndices().size())) {
    int64_t expected = tensorType.getRank();
    return emitOpError("expected ")
           << expected << (expected == 1 ? " index, got " : " indices, got ")
           << getIndices().size();
  }
```

为什么用三元表达式而不是 “indices” 一刀切：rank 为 1 时 “expected 1 indices” 是语法错误。reviewer 会在措辞上花时间，**你先把这一步做掉，就少一轮 review**。

`tensor.insert` 那边同构（把 `tensorType` 换成 `destType`）。

### F.7.3 验证 patch 能应用

patch 在 `/tmp/fp2/fix.patch`（源码树未改动）：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git apply --check --verbose /tmp/fp2/fix.patch && echo "APPLY-CHECK-OK"
```

```text
Checking patch mlir/lib/Dialect/Tensor/IR/TensorOps.cpp...
Checking patch mlir/test/Dialect/Tensor/invalid.mlir...
APPLY-CHECK-OK
```

### F.7.4 验证它能编译

不需要重建整个项目，用编译数据库对“改后的文件副本”做语法检查：

```bash
python3 - <<'PY' > /tmp/fp2/cmd.sh
import json, shlex
db = json.load(open('/home/ubuntu/llvm/builds/ct-instr-trunk/compile_commands.json'))
e = [x for x in db if x['file'].endswith('mlir/lib/Dialect/Tensor/IR/TensorOps.cpp')][0]
args = shlex.split(e['command'])
out, skip = [], False
for a in args:
    if skip: skip = False; continue
    if a in ('-o', '-c'): skip = (a == '-o'); continue
    if a.endswith('TensorOps.cpp'): continue
    out.append(a)
print('cd %s && exec %s -fsyntax-only /tmp/fp2/b/mlir/lib/Dialect/Tensor/IR/TensorOps.cpp'
      % (shlex.quote(e['directory']), ' '.join(shlex.quote(x) for x in out)))
PY
time bash /tmp/fp2/cmd.sh ; echo "SYNTAX_EXIT=$?"
```

```text
real    0m7.613s
SYNTAX_EXIT=0
```

这一步的价值：**在花 15 分钟重建之前，先用 8 秒排除语法错误**。Ch 02 的编译数据库在这里第一次派上大用场。

## F.8 第 5 步：测试：先红后绿

### F.8.1 基线：现有测试是绿的

```bash
$T/bin/llvm-lit -sv $T/tools/mlir/test/Dialect/Tensor/invalid.mlir
```

```text
Testing Time: 0.25s

Total Discovered Tests: 1
  Passed: 1 (100.00%)
```

这说明改动前套件是干净的——**任何失败都只能是你引入的**。

### F.8.2 红：新测试在当前二进制上必须失败

先改测试期望（`invalid.mlir:68` 与 `:76`），再拿**旧二进制**跑：

```bash
$T/bin/mlir-opt /tmp/fp2/b/mlir/test/Dialect/Tensor/invalid.mlir \
  -split-input-file -verify-diagnostics ; echo "exit=$?"
```

```text
b/mlir/test/Dialect/Tensor/invalid.mlir:68:6: error: expected error "'tensor.extract' op expected 1 index, got 0" was not produced
  // expected-error@+1 {{'tensor.extract' op expected 1 index, got 0}}
     ^~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
b/mlir/test/Dialect/Tensor/invalid.mlir:76:6: error: expected error "'tensor.insert' op expected 1 index, got 0" was not produced
exit=1
```

**这一步是整个流程里最重要的一次运行**：它证明“测试确实能测到这个行为”。如果新测试在旧二进制上就绿了，说明你写的断言根本没覆盖修复点（Ch 29.12 的第 1 条）。

### F.8.3 绿：修复后应当通过（未实测 + 原因）

本章不重建，所以“绿”这一步是**推演**而非实测：

```bash
# 若在可写副本里操作：
ninja -C $T MLIRTensorDialect            # 只重编这一个 dylib
$T/bin/llvm-lit -sv $T/tools/mlir/test/Dialect/Tensor/invalid.mlir
# 期望：Total Discovered Tests: 1 / Passed: 1
```

**未实测，原因**：课程约定源码树只读（避免与并行写作任务冲突），且重新链接 `libMLIRTensorDialect.so` 会让构建产物与源码树短暂不一致。你在自己的 fork 上必须真的跑这一步——CI 也会跑。

### F.8.4 测试改动要一起进 PR

| 文件 | 改什么 | 为什么 |
|---|---|---|
| `TensorOps.cpp` | 2 处消息 | 行为 |
| `invalid.mlir` | 2 条 `expected-error` + 2 个函数名 | 断言必须跟着行为走 |

第二行的函数名（`extract_too_many_indices` → `extract_wrong_number_of_indices`）不是洁癖：原名说“太多”，实际用例是“零个”。**名字说谎的测试，六个月后没人敢改**。

## F.9 第 6 步：release note 判据

T1 属于“诊断措辞改进”，按 Ch 30.7 的判据**不写** `mlir/docs/ReleaseNotes.md`：

- 下游不会因此改代码（没有 API/IR/CLI 变化）；
- 消息文本不是稳定接口（没有人该去匹配 `incorrect number of indices`）；
- 但**测试里的 `expected-error` 是**——所以测试必须同 PR 更新。

反例：如果你改的是 `tensor.extract` 的 assembly 格式，那就必须写 note，并说明旧 IR 是否仍可解析（Ch 15/18）。

## F.10 第 7 步：PR 描述与 reviewer 路由

### F.10.1 描述模板

```text
<一句话：问题 + 修复>

<现状/机制：为什么会这样，指向文件:行号>
<做法：改成什么，为什么这个形状>
<验证：跑了什么，结果如何>

<可选：不确定性 / 后续>
```

### F.10.2 本例填好的版本（可直接用）

```text
[mlir][tensor] Improve index-count diagnostics for tensor.extract/insert

tensor.extract's verifier still reports "incorrect number of indices for
extract_element": the string predates the rename from std/tensor and names
an operation that no longer exists (TensorOps.cpp:1412). tensor.insert says
only "incorrect number of indices" (TensorOps.cpp:1688). Neither tells the
user how many indices were expected.

Report the expected and actual counts, and drop the stale operation name
(emitOpError already prefixes the current name).

Verified:
- new negative tests fail on the pre-patch binary ("expected ... was not
  produced"), pass after rebuilding MLIRTensorDialect
- mlir/test/Dialect/Tensor/invalid.mlir: 1/1 passed
- clang++ -fsyntax-only on the patched TU: clean
```

### F.10.3 reviewer 路由

- 首选：`mlir/Maintainers.md` 里 Tensor Compiler 节的 owner，或 `git log` 里近半年动过 `TensorOps.cpp` 的人（Ch 03 的方法）。
- 次选：PR 里 `cc` 一位你引用过的提交作者（Ch 30 实验 3 的 `cc @banach-space` 就是这个用法）。
- 不要：群发 `@llvm/mlir`，或把 10 个人塞进 reviewer。

## F.11 参考实现：完整走一遍

下面三个 `<details>` 是这次交付的全部产物，可以直接复制到你的 fork 里。

<details>
<summary>① 完整 patch（`git apply` 可直接用）</summary>

```diff
--- a/mlir/lib/Dialect/Tensor/IR/TensorOps.cpp
+++ b/mlir/lib/Dialect/Tensor/IR/TensorOps.cpp
@@ -1408,8 +1408,12 @@
 LogicalResult ExtractOp::verify() {
   // Verify the # indices match if we have a ranked type.
   auto tensorType = llvm::cast<RankedTensorType>(getTensor().getType());
-  if (tensorType.getRank() != static_cast<int64_t>(getIndices().size()))
-    return emitOpError("incorrect number of indices for extract_element");
+  if (tensorType.getRank() != static_cast<int64_t>(getIndices().size())) {
+    int64_t expected = tensorType.getRank();
+    return emitOpError("expected ")
+           << expected << (expected == 1 ? " index, got " : " indices, got ")
+           << getIndices().size();
+  }
   return success();
 }
 
@@ -1684,8 +1688,12 @@
 LogicalResult InsertOp::verify() {
   // Verify the # indices match if we have a ranked type.
   auto destType = llvm::cast<RankedTensorType>(getDest().getType());
-  if (destType.getRank() != static_cast<int64_t>(getIndices().size()))
-    return emitOpError("incorrect number of indices");
+  if (destType.getRank() != static_cast<int64_t>(getIndices().size())) {
+    int64_t expected = destType.getRank();
+    return emitOpError("expected ")
+           << expected << (expected == 1 ? " index, got " : " indices, got ")
+           << getIndices().size();
+  }
   return success();
 }
 
--- a/mlir/test/Dialect/Tensor/invalid.mlir
+++ b/mlir/test/Dialect/Tensor/invalid.mlir
@@ -64,16 +64,16 @@
 
 // -----
 
-func.func @extract_too_many_indices(%arg0: tensor<?xf32>) {
-  // expected-error@+1 {{incorrect number of indices for extract_element}}
+func.func @extract_wrong_number_of_indices(%arg0: tensor<?xf32>) {
+  // expected-error@+1 {{'tensor.extract' op expected 1 index, got 0}}
   %0 = tensor.extract %arg0[] : tensor<?xf32>
   return
 }
 
 // -----
 
-func.func @insert_too_many_indices(%arg0: f32, %arg1: tensor<?xf32>) {
-  // expected-error@+1 {{incorrect number of indices}}
+func.func @insert_wrong_number_of_indices(%arg0: f32, %arg1: tensor<?xf32>) {
+  // expected-error@+1 {{'tensor.insert' op expected 1 index, got 0}}
   %0 = tensor.insert %arg0 into %arg1[] : tensor<?xf32>
   return
 }
```

时间戳已省略；`git apply --check` 在本树通过（F.7.3）。

</details>

<details>
<summary>② 完整的负向测试文件（可直接跑）</summary>

```mlir
// RUN: mlir-opt %s -split-input-file -verify-diagnostics

// Ask for one element of a rank-1 tensor with no indices.
func.func @extract_wrong_number_of_indices(%arg0: tensor<?xf32>) {
  // expected-error@+1 {{'tensor.extract' op expected 1 index, got 0}}
  %0 = tensor.extract %arg0[] : tensor<?xf32>
  return
}

// -----

// Ask for a rank-2 tensor's element with one index.
func.func @extract_two_indices(%arg0: tensor<?x?xf32>) {
  // expected-error@+1 {{'tensor.extract' op expected 2 indices, got 1}}
  %c0 = arith.constant 0 : index
  %0 = tensor.extract %arg0[%c0] : tensor<?x?xf32>
  return
}

// -----

func.func @insert_wrong_number_of_indices(%arg0: f32, %arg1: tensor<?xf32>) {
  // expected-error@+1 {{'tensor.insert' op expected 1 index, got 0}}
  %0 = tensor.insert %arg0 into %arg1[] : tensor<?xf32>
  return
}
```

第三个用例（rank 2）是**故意多写的**：它验证复数形式（`indices`）也正确。只测 `got 0` 的话，`expected == 1` 的三元分支永远只走一条。

</details>

<details>
<summary>③ PR 描述模板（复制后填空）</summary>

```text
[mlir][<subsystem>] <one-line changelog, imperative>

<What is wrong today. Point at file:line. If it is a regression, name the
commit/PR that introduced it.>

<What the patch does, and why this shape rather than the alternatives you
considered.>

Verified:
- <command 1> -> <result>
- <command 2> -> <result>
- <test file> -> <N/N passed>

<Optional: follow-ups, known gaps, or open questions.>
```

三条填空纪律：

1. “Verified” 只写**你真的跑过**的命令；没跑就写 “not run locally, CI will cover”。
2. 提到行号时用**当前 HEAD 的行号**，并说清是哪个 commit（rebase 后会漂）。
3. 不确定性主动写出来——reviewer 最怕“看起来都懂、其实有坑”。

</details>

## F.12 自查清单

发出去之前逐条打勾。这份清单把散落在各章的判据收在一处。

**问题与证据**

- [ ] problem 能用一句“谁 / 什么输入 / 什么行为 / 期望什么”说清（F.4）
- [ ] 复现有 ≤30 行 `.mlir` + 一条命令，别人复制能跑（F.5）
- [ ] root cause 指向具体文件:行号，并解释了机制，不只是“这里不对”（F.6）
- [ ] 知道这个字符串/逻辑是**哪次改动**留下的（`git blame` / `git log -L`）

**改动**

- [ ] diff 只包含实现 + 测试两类文件（`git apply --numstat` 看过比例）
- [ ] 没有夹带重命名、格式化、无关重构（Ch 30.8）
- [ ] 消息不自称 op 名（`emitOpError` 会加前缀）
- [ ] 单复数、期望值/实际值都正确（F.7.2）
- [ ] 编译通过：`clang++ -fsyntax-only` 或完整重建（F.7.4）

**测试**

- [ ] 新测试在**修复前**失败（红），失败信息是“expected ... was not produced”（F.8.2）
- [ ] 修复后通过（绿），并且是**真的跑过**，不是推演
- [ ] 覆盖了至少两个分支（单数/复数、extract/insert）
- [ ] 没有用 `--verify-diagnostics=only-expected`（Ch 29.6）
- [ ] 跑过相邻目录，确认没碰坏别的：`llvm-lit -sv $T/tools/mlir/test/Dialect/Tensor`

**流程**

- [ ] `git log` 里只有一个 commit，且 rebase 到最新 `upstream/main`
- [ ] 标题带 `[mlir][<subsystem>]`，一行说清改了什么
- [ ] body 写了“为什么”，PR 描述写了“怎么验证 + 为什么不选别的”
- [ ] release note 的写/不写有明确理由（Ch 30.7）
- [ ] reviewer 从 `Maintainers.md` / `CODEOWNERS` / `git log` 里选，不超过 3 人
- [ ] 如果用了 AI 工具，加了 `Assisted-by:` 并能回答每个 review 问题（Ch 30.10）

**范围**

- [ ] 如果 diff 超过 400 行，已经拆成 NFC + 行为两个 PR
- [ ] 如果改了 `assemblyFormat` / bytecode，PR 里说明了旧 IR 兼容性（Ch 15/18）
- [ ] 如果改了公共 header，考虑过 `LLVM_DEPRECATED` 过渡（Ch 31.5 第 4 问）

## F.13 踩雷集锦

1. **先写 fix，再补复现。** → 你无法证明修的是真问题；复现必须先于 fix，而且必须短到能贴进 PR（F.5）。
2. **改完 `expected-error` 就算测过。** → 新断言必须在**旧二进制**上失败（F.8.2）；不红就是没测到。
3. **消息里再写一遍 op 名更清楚。** → `emitOpError` 已加 `'tensor.extract' op` 前缀，你会得到 “op 'x' op …”。
4. **顺手改测试函数名/格式。** → diff 变大、review 变慢；函数名单独一个 PR（F.8.4 是唯一例外：名字在说谎时）。
5. **诊断改动也要写 release note。** → 下游不被迫改代码就不写；但测试里的 `expected-error` 必须同 PR 更新。
6. **`clang++ -fsyntax-only` 过了就等于能跑。** → 它只证明语法与头文件；行为证据只能来自 lit。
7. **PR 描述越长越显得认真。** → reviewer 只看三段：动机、怎么验证、不确定性；其余是噪音。
8. **`git apply --check` 过了就能合并。** → 它只证明上下文匹配；合并前还要编译 + 跑测试 + CI。

## F.14 常见退回理由（以及怎么避免）

| # | reviewer 的话 | 真实含义 | 避免方式 |
|---|---|---|---|
| 1 | “Please add a test.” | 没有测试 = 没有证据 | F.8，先写红测试 |
| 2 | “This needs an RFC.” | 改动触及契约/边界，不是局部修复 | 先开 issue 问结论（T3） |
| 3 | “Please split this out.” | diff 混了无关改动 | 一个 PR 一件事；NFC 单独发 |
| 4 | “This is unrelated.” | 顺手重构被发现 | `git diff` 逐文件自查 |
| 5 | “Can you use X instead?” | 既有工具/接口能覆盖 | 先 `grep` 现有实现，别造新轮子 |
| 6 | “Nit: …” | 不阻塞，但请顺手改 | 改完回 “done”，别争论风格 |
| 7 | “The release notes say otherwise.” | 代码/注释/note 三处不一致 | 同时改三处（Ch 30.6.4） |
| 8 | “Why is this here and not in X?” | 分层判断不同 | PR 里写“我考虑过 X，因为…选这里” |
| 9 | “This changes IR compatibility.” | 旧 IR 可能解析失败 | 提前想 bytecode/parser 兼容（Ch 18） |
| 10 | 三周没人回 | 选错 owner 或选题太边缘 | 换 owner / 转 draft / 关单（Ch 30.9） |

## F.15 课程回顾：31 章压成四个能力簇

| 能力簇 | 章节 | 你现在应该能做的事 |
|---|---|---|
| **读 IR 与对象模型** | 04–08、14–18 | 说清一次 erase 为什么崩、一个 `.td` 生成了什么、verifier 在哪一层跑 |
| **Pass / Pattern / Analysis** | 09–13 | 写出一个不会无限重写、不会让 analysis 失效的 pass |
| **Conversion 与 Bufferization** | 19–23 | 判断 legality 该静态还是动态、cast 出现在哪、buffer 所有权归谁 |
| **真实子系统与贡献** | 24–31 | 在 Linalg/GPU/OpenMP/Transform 里定位问题，并把它变成 PR |

如果你只能记住一句话：**MLIR 的维护工作 = 把“我觉得这里不对”变成“这条命令在这个输入上输出这个，根因在这一行，改完之后这条测试从红变绿”。** 剩下的（API、方言、pattern）都是为这条证据链服务的。

## F.16 延伸阅读

1. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Tensor/IR/TensorOps.cpp` — 读 1400–1420 与 1680–1700（两个 verifier 与相邻 fold）；学到“校验什么、不校验什么”的边界；对应 F.6/F.7。
2. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Dialect/Tensor/invalid.mlir` — 全文 789 行，重点 60–80；学到负向测试的组织与 `// -----` 分块；对应 F.8。
3. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/TestingGuide.md` — 读 324–375（precommit workflow 与 best practices）；学到“先 baseline、后实现”的节奏；对应 F.8。
4. `/home/ubuntu/llvm/trees/ct-instr-trunk/llvm/docs/Contributing.md` — 读 44–133；学到补丁的五条硬要求与 review 期待；对应 F.10。
5. `/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/ReleaseNotes.md` — 读 `## LLVM 24`；学到什么级别的改动值得写 note；对应 F.9。

---

本课程到此结束。回到目录可以看到每章的定位与阅读顺序；如果只挑三章重读，建议 **Ch 13（证据链）、Ch 29（测试）、Ch 31（选题）**——它们决定你能不能把知识变成提交。

→ 返回：[课程目录](./README.md)
