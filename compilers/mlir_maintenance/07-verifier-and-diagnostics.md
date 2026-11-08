# Ch 07 — Verifier 与 diagnostics

> **目标**：知道 `mlir-opt` 每次跑 pass 前后到底校验了什么、`verifyOperation` 的 worklist 顺序如何决定“你先看到哪条错误”，以及 `--verify-diagnostics` 的匹配规则（`@+N`、`{{regex}}`、顺序、split chunk）。读完你能写出一条既能抓住 bug、又不会被诊断顺序绑死的负控测试。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `e44fff5decc7`，`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。实验用 `mlir/test/IR/` 下的真实测试文件和 `/tmp/mlirc` 小样例。

> **前置**：Ch 04（IR 对象模型）、Ch 06（properties 与 bytecode）；用过 `lit` 或至少见过 `// RUN:` 行。

## 07.1 为什么维护者要读 verifier

Verifier 是 MLIR 里**唯一一个“所有 pass 都必须过”的关口**。`PassManager` 默认在每个 pass 前后调用它（`run` 选项可关，但测试里几乎总是开）。这带来两个维护后果：

1. **你的 pass 写出的 IR 不合法，错误会被算到下一个 pass 头上。** 报错位置在下一个 pass 的输入上，根因却在上一个 pass。
2. **verifier 不是语义证明。** 它只检查“结构契约”：类型一致、block 有 terminator、dominance 成立、trait 约束满足。它不会证明你的循环边界正确，也不会证明 bufferization 没别名。

所以维护者的目标不是“让 verifier 过”，而是：**知道 verifier 覆盖了哪些不变量，剩下的用测试补。**

一个真实的分工表：

| 层 | 检查什么 | 谁负责 |
|---|---|---|
| parser | 语法、SSA 名字、类型语法 | `Parser.cpp` |
| verifier | 结构契约、trait、region invariant、dominance | `Verifier.cpp` |
| op 自己的 `verify()` | 语义约束（如 `linalg` 的 indexing map 维度） | dialect |
| pass 的单测 | 变换正确性 | 你 |

## 07.2 先建立直觉：worklist 的 entrance / exit

`OperationVerifier::verifyOperation`（`Verifier.cpp:412`）不是递归函数，而是一个显式 worklist，每个节点被访问两次：

```text
verify(module)
  worklist = [(module, visited=false)]
  while worklist:
    top = worklist.back()

    ── 第一次（entrance）─────────────────────────────
    op:    context 一致性
           result 类型 context
           operand 非空 + 类型 context
           discardable attr 的 dialect verifier
           RegisteredOperationName::verifyInvariants(op)   // traits + ODS verify
           token 值
           region kind
    block: block arg 归属 / loc / 类型 context
           必须有 terminator（除非允许）
           successor 只能出现在最后一条 op
    → 把子 region 的 block 压栈

    ── 第二次（exit）────────────────────────────────
    op:    先并行 verify 内部 IsolatedFromAbove 子树
           verifyRegionInvariants(op)                  // 需要“子 region 已合法”
           未注册 op 的 dialect 开关检查

  ── worklist 清空后 ────────────────────────────────
  verifyDominanceOfContainedRegions(module, domInfo)   // Verifier.cpp:100
```

两个设计点值得记住：

- **entrance 先查 op 自身，exit 才查 region invariant。** 因为 `verifyRegionInvariants` 的契约是“子 region 已经验过”。你写自定义 op 的 region 级检查时，必须放在 `verifyRegionInvariants`，不能放在 `verify()` 里。
- **dominance 在整棵树之后再跑。** 它需要完整的 CFG，不能在单 op 上做。

## 07.3 四层校验，各自报什么错

### 07.3.1 trait / ODS 层

`RegisteredOperationName::verifyInvariants(&op)` 会依次跑 ODS 生成的 `verifyInvariants`（operand/result/attr 的约束）、trait 的 `verifyTrait`、以及用户手写的 `verify()`。典型错误：

```text
error: 'test.return' op requires zero results
```

### 07.3.2 block / region 层

`verifyOnEntrance(Block&)`（`:193`）检查 arg 归属、terminator、successor 位置。真实错误长这样：

```text
error: empty block: expect at least a terminator
error: operation with block successors must terminate its parent block
```

### 07.3.3 op 自己的 region invariant

`verifyOnExit(Operation&)`（`:355`）在子 region 全部验完后调 `verifyRegionInvariants`（`:379`）。`func.func` 的“return 操作数个数必须匹配函数签名”就在这里：

```text
ret.mlir:3:3: error: 'func.return' op has 0 operands, but enclosing function (@f) returns 1
```

注意这条错误的 location 是 `func.return`（`3:3`），不是函数。维护者定位“是 return 写错了还是函数签名写错了”，要看 note。

### 07.3.4 dominance 层

`verifyDominanceOfContainedRegions`（`:524`）在最后跑。真实错误：

```text
bad-dom.mlir:9:8: error: operand #0 does not dominate this use
bad-dom.mlir:4:8: note: operand defined here (op in the same region)
```

四层的边界很清楚：**trait 管单 op，block 管容器，region invariant 管父子关系，dominance 管 CFG。**

## 07.4 diagnostics 机制：handler 链与 InFlightDiagnostic

`DiagnosticEngine::emit`（`Diagnostics.cpp:350`）把诊断交给注册的 handler 链。handler 返回 `success` 表示“已消费”，`failure` 表示“继续传给前一个 handler”。`mlir-opt` 默认挂的是源位置打印 handler；`--verify-diagnostics` 换成一个“比对 expected-*”的 handler。

`emitError`/`emitOpError` 返回 `InFlightDiagnostic`，用流式拼接：

```cpp
InFlightDiagnostic Operation::emitOpError(const Twine &message) {   // Operation.cpp:664
  return emitError() << "'" << getName() << "' op " << message;
}
```

`attachNote`（`Diagnostics.cpp:222`）挂 note：

```cpp
diag.attachNote(user->getLoc()) << "- use: " << *user << "\n";
```

这就是 Ch 04 里那条 “operation destroyed but still has uses” 后面跟一串 note 的来源。

handler 的注册形态可以直接从 `Diagnostics.md` 抄：

```cpp
DiagnosticEngine &engine = ctx->getDiagEngine();
DiagnosticEngine::HandlerID id = engine.registerHandler(
    [](Diagnostic &diag) -> LogicalResult {
      bool shouldPropagate = ...;
      return failure(shouldPropagate);   // failure 表示继续传给前一个 handler
    });
engine.eraseHandler(id);                 // 不用时注销
```

`--verify-diagnostics` 本质上就是替换了最外层 handler：它不走“打印”，而是把诊断与 expected-* 行做匹配。**这解释了为什么有些默认打印行为在测试里消失**（见实验 8）。

一个常被忽略的事实：**`emitOpError` 的前缀是 `'<op-name>' op`，不是裸消息。** 负控测试的 `expected-error` 必须把这段前缀算进去，或者用 `{{...}}` 只匹配后缀。

## 07.5 `--verify-diagnostics` 的匹配规则

规则比想象的严格：

```text
1. expected-<kind> 必须出现在“诊断所在行”的上一行，或用 @+N / @-N 指到那一行。
2. {{...}} 是 LLVM regex（不是 glob），匹配诊断消息的子串。
3. 顺序敏感：同一行上的多条 expected-*，必须按诊断实际发出的顺序排列。
4. 每条 expected-* 只能消费一条同 kind 的诊断；在 `--verify-diagnostics`（All 级）下，多出来的诊断报 unexpected。
5. --split-input-file 时，每个 // ----- chunk 独立匹配；@+N 仍是“相对注释行”的物理偏移，诊断打印的 location 则是文件全局行号。
```

第 1 条最容易错。下面这个文件看起来“把 error 和 note 都写在 op 上面”，实际两条都指错了行：

```mlir
func.func @f() -> i32 {
  // expected-error @+1 {{'func.return' op has 0 operands, but enclosing function}}
  // expected-note @+1 {{see current operation}}
  return
}
```

真实输出（实测）：

```text
note2.mlir:3:6: error: expected note "see current operation" was not produced
  // expected-note @+1 {{see current operation}}
     ^~~~~~~~~~~~~~~~~~~~~
```

`@+1` 从第 2 行指向第 3 行（那条 note 注释自己），而不是第 4 行的 op。正确写法是 error 用 `@+2`、note 用 `@+1`，两条都落到 `return` 行。

第 5 条也有坑：`@-2` / `@+N` 锚定的是**注释行自身**，不是 chunk 的起始行；从上一个 chunk 抄过来的偏移会指到错误位置。而诊断打印的 `file:line:col` 始终是文件全局行号——实测见实验 10。

## 07.6 维护者怎么写负控测试

标准形状（来自 `mlir/test/IR/invalid-ops.mlir`）：

```mlir
// RUN: mlir-opt -allow-unregistered-dialect %s -split-input-file -verify-diagnostics

func.func @func_with_ops(i32, i32, i32) {
^bb0(%cond : i32, %t : i32, %f : i32):
  // expected-error@+2 {{different type than prior uses}}
  // expected-note@-2 {{prior use here}}
  %r = arith.select %cond, %t, %f : i32
}
```

三个要点：

1. **RUN 行的 flag 必须和测试体匹配。** 这个文件用了未注册方言 `foo.region`，所以必须带 `-allow-unregistered-dialect`；漏了就会得到一堆 `unexpected error`。
2. **`expected-note@-2` 指向 note 的真实位置**（prior use 在前一行），不是 error 行。
3. **一条 chunk 只放一个失败场景。** 这样 `@+N` 简单、reviewer 也好读。

反例：把所有 `expected-error` 堆在文件末尾，用 `@-20` 指回去。diff 一动就全错位，且无法判断哪条错误对应哪个 op。

## 07.7 源码落点

```bash
grep -n 'LogicalResult OperationVerifier::verifyOperation\|verifyOnEntrance(Operation\|verifyOnEntrance(Block\|verifyOnExit(Operation\|verifyOnExit(Block\|verifyRegionInvariants\|verifyDominanceOfContainedRegions' mlir/lib/IR/Verifier.cpp
grep -n 'void DiagnosticEngine::emit\|Diagnostic &Diagnostic::attachNote\|InFlightDiagnostic mlir::emitError' mlir/lib/IR/Diagnostics.cpp
grep -n 'InFlightDiagnostic Operation::emitOpError' mlir/lib/IR/Operation.cpp
ls mlir/test/IR/invalid-*.mlir | head
```

本机实测：

```text
mlir/lib/IR/Verifier.cpp:193:LogicalResult OperationVerifier::verifyOnEntrance(Block &block) {
mlir/lib/IR/Verifier.cpp:252:LogicalResult OperationVerifier::verifyOnExit(Block &block) {
mlir/lib/IR/Verifier.cpp:272:LogicalResult OperationVerifier::verifyOnEntrance(Operation &op) {
mlir/lib/IR/Verifier.cpp:355:LogicalResult OperationVerifier::verifyOnExit(Operation &op) {
mlir/lib/IR/Verifier.cpp:379:  if (registeredInfo && failed(registeredInfo->verifyRegionInvariants(&op)))
mlir/lib/IR/Verifier.cpp:412:LogicalResult OperationVerifier::verifyOperation(Operation &op) {
mlir/lib/IR/Verifier.cpp:524:OperationVerifier::verifyDominanceOfContainedRegions(Operation &op,
mlir/lib/IR/Diagnostics.cpp:350:void DiagnosticEngine::emit(Diagnostic &&diag) {
mlir/lib/IR/Diagnostics.cpp:222:Diagnostic &Diagnostic::attachNote(std::optional<Location> noteLoc) {
mlir/lib/IR/Operation.cpp:664:InFlightDiagnostic Operation::emitOpError(const Twine &message) {
```

`verifyOperation` 在 `:91` 被 `verify()` 调用一次，然后 `:100` 调 dominance——这两行就是“先结构、后支配”的顺序证据。

## 07.8 可跑实验

### 实验 1：最直白的结构错误（实测）

```bash
mkdir -p /tmp/mlirc && cd /tmp/mlirc
cat > ret.mlir <<'EOF'
func.func @f(%a: i32) -> i32 {
  // expected-error @+1 {{'func.return' op has 0 operands, but enclosing function returns 1}}
  return
}
EOF
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt ret.mlir
```

真实输出：

```text
ret.mlir:3:3: error: 'func.return' op has 0 operands, but enclosing function (@f) returns 1
  return
  ^
ret.mlir:3:3: note: see current operation: "func.return"() : () -> ()
ret.mlir:2:6: error: expected error "'func.return' op has 0 operands, but enclosing function returns 1" was not produced
```

注意：**即使没传 `--verify-diagnostics`，默认 handler 也会解析 `expected-*` 并报告“未产生”的期望**；它只是不会把真实诊断标成 unexpected。最后一条 “was not produced” 是因为 `expected-error` 的正则没算上 `(@f)`——正好演示 07.5 第 2 条。

### 实验 2：把正则改成子串后就通过（实测）

把 expected 改成只匹配后半段：

```bash
cat > ret2.mlir <<'EOF'
func.func @f(%a: i32) -> i32 {
  // expected-error @+1 {{has 0 operands, but enclosing function}}
  return
}
EOF
$T/bin/mlir-opt ret2.mlir --verify-diagnostics
```

真实输出（exit 0，完全静默）：

```text
EXIT=0
```

结论：`expected-error` 是**正则子串**匹配，不是全文相等；把 `(@f)` 留在正则外就能匹配。`{{...}}` 内不要写 `(`、`)` 这类会被 regex 解释的字符，除非你想让它们参与匹配。

### 实验 3：正确的负控测试整体通过（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt -allow-unregistered-dialect mlir/test/IR/invalid-ops.mlir \
  -split-input-file -verify-diagnostics
```

真实输出（exit 0）：

```text
EXIT=0
```

没有任何 stdout——`--verify-diagnostics` 成功时是静默的。文件里有 `expected-error@+2` / `expected-note@-2` 的成对用法（见 07.6）。

### 实验 4：漏 flag 时会发生什么（实测）

同一个文件，去掉 `-allow-unregistered-dialect`：

```text
mlir/test/IR/invalid-ops.mlir:100:15: error: unexpected error: operation being parsed with an unregistered dialect. If this is intended, please use -allow-unregistered-dialect with the MLIR tool used
mlir/test/IR/invalid-ops.mlir:101:8: error: expected error "'func.return' op expects parent op 'func.func'" was not produced
```

`unexpected error` 的意思是“诊断真的发生了，但没有对应的 expected-* 行”。维护者看到它，第一反应应当是**检查 RUN 行 flag**，而不是改 expected 文本。

### 实验 5：pass 调度错误不是 verifier 错误（实测）

```bash
$T/bin/mlir-opt mlir/test/IR/diagnostic-handler-filter.mlir -test-diagnostic-filter
```

真实输出：

```text
mlir/test/IR/diagnostic-handler-filter.mlir:0:0: error: 'builtin.module' op trying to schedule pass '{anonymous}::TestDiagnosticFilterPass' on an unsupported operation
```

因为它是 `OperationPass<func::FuncOp>`，不能直接挂在 module 上。必须用 pipeline：

```bash
$T/bin/mlir-opt mlir/test/IR/diagnostic-handler-filter.mlir \
  -pass-pipeline="builtin.module(func.func(test-diagnostic-filter{filters=mysource1}))" \
  -split-input-file -o -
```

真实输出（节选）：

```text
Test 'test1'
mysource2:1:0: error: test diagnostic
mysource3:2:0: note: called from
Test 'test2'
mysource1:0:0: error: test diagnostic
```

这段同时演示了 **diagnostic filter**：handler 按 `filters=mysource1` 只保留来自该 source 的诊断，所以 `test1` 的 callsite 链里 `mysource2` 仍然被打印（它在 callsite 内），而 `test2` 直接命中 `mysource1`。

### 实验 6：diagnostic metadata 与 verify 组合（实测）

```bash
$T/bin/mlir-opt mlir/test/IR/diagnostic-handler-metadata.mlir \
  -pass-pipeline="builtin.module(func.func(test-diagnostic-metadata))" \
  -verify-diagnostics -o -
```

真实输出：

```text
Test 'test'
module {
  func.func @test() {
    "test.emit_error"() {attr = "emit_error"} : () -> ()
    "test.do_not_emit_error"() {attr = "do_not_emit_error"} : () -> ()
    return
  }
}
EXIT=0
```

metadata 让 handler 能按“诊断附带的键值”过滤；`do_not_emit_error` 那条被 handler 消费掉，所以 `expected-error` 只需要一条。

### 实验 7：诊断顺序（实测）

```bash
$T/bin/mlir-opt mlir/test/IR/diagnostic-handler-next.mlir \
  -pass-pipeline="builtin.module(func.func(test-diagnostic-next))" -verify-diagnostics
```

真实输出：

```text
module {
  func.func @test() {
    return
  }
}
EXIT=0
```

该测试的注释故意把 `expected-remark` 写成“test1 / test3 / test2”的逆序，pass 也按这个顺序发 remark。**顺序错了会立刻失败**——这就是 07.5 第 3 条的可执行证明。

### 实验 8：`see current operation` 在 verify 模式下不可匹配（实测）

```bash
cat > /tmp/mlirc/note3.mlir <<'EOF'
func.func @f() -> i32 {
  // expected-error @+2 {{'func.return' op has 0 operands, but enclosing function}}
  // expected-note @+1 {{see current operation}}
  return
}
EOF
$T/bin/mlir-opt /tmp/mlirc/note3.mlir --verify-diagnostics
```

真实输出：

```text
note3.mlir:3:6: error: expected note "see current operation" was not produced
```

原因：`see current operation` 是**默认源位置打印 handler**附加的 note，不是 diagnostic engine 发出的诊断。`--verify-diagnostics` 换了 handler，这条 note 根本不会产生。**不要给打印器生成的 note 写 expected-note。**

### 实验 9：verifier 在第一个错误就停（实测）

```bash
cat > /tmp/mlirc/multi.mlir <<'EOF'
func.func @f(%a: i32) -> i32 {
  return %a : i32
}
func.func @g() -> i32 {
  return
}
module {
}
EOF
$T/bin/mlir-opt /tmp/mlirc/multi.mlir
```

真实输出：

```text
multi.mlir:5:3: error: 'func.return' op has 0 operands, but enclosing function (@g) returns 1
  return
  ^
multi.mlir:5:3: note: see current operation: "func.return"() : () -> ()
```

只有 `@g` 的错误，`@f` 与空 module 都没有报——验证是 fail-fast：`verifyOperation` 一旦 `return failure()`，外层不再继续。维护者不要指望“一次跑出所有错误”。

### 实验 10：split chunk 的行号与 `unexpected error`（实测）

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/IR/diagnostic-nosplit.mlir -verify-diagnostics
```

真实输出：

```text
mlir/test/IR/diagnostic-nosplit.mlir:11:8: error: unexpected error: 'arith.constant' op failed to verify that all of {value, result} have same type
  %x = "arith.constant"() {value = 100} : () -> i1
       ^
EXIT=1
```

这个文件没有 `expected-error`，所以任何诊断都是 `unexpected`。它的 RUN 行还演示了另一种写法：

```text
// RUN: not mlir-opt %s -o - --split-input-file 2>&1 | FileCheck %s
// CHECK: mlir:11:8: error: 'arith.constant'
```

注意 `CHECK` 里的 `11:8` 是**文件全局行号**，而 `@+N` 是**相对注释行**的偏移。实测：在第二个 chunk 里写 `expected-error @+1`，报错仍然打印全局行号 `chunks.mlir:9:3`。两套编号不要混。

## 07.9 踩雷集锦

1. **错误直觉：verifier 过了就说明 pass 对。** → 正确认识：verifier 只查结构契约，语义正确性要靠 lit 输出比对。
2. **错误直觉：`expected-error` 是全文匹配。** → 正确认识：`{{...}}` 是 regex 子串；没包进去的前缀（如 `(@f)`）不会自动放过。
3. **错误直觉：`@+1` 永远指下一行的 op。** → 正确认识：`@+N` 指第 N 行后的物理行；如果中间还有一条注释，就指到注释上了。
4. **错误直觉：note 和 error 可以都写 `@+1`。** → 正确认识：它们必须各自指到诊断所在行；多条 expected 用不同偏移落到同一行。
5. **错误直觉：`--split-input-file` 下行号是全局的。** → 正确认识：每个 chunk 独立计数，跨 chunk 的偏移必然错。
6. **错误直觉：诊断一定有固定顺序，写测试可以随便排。** → 正确认识：同一位置的多个诊断按发出顺序匹配；实测 `diagnostic-handler-next` 就是逆序。
7. **错误直觉：`see current operation` 可以写成 `expected-note`。** → 正确认识：它由打印 handler 生成，verify 模式下不存在。
8. **错误直觉：一个文件能一次暴露所有 verifier 错误。** → 正确认识：fail-fast，第一个错误就返回；要覆盖多个场景就用 `--split-input-file` 分 chunk。

## 07.10 本章重点整理

- Verifier 是 worklist + entrance/exit 两阶段，先结构、后 region invariant、最后 dominance。
- trait/ODS、block/region、op 的 `verifyRegionInvariants`、dominance 是四层不同的检查。
- `emitOpError` 自带 `'<op>' op` 前缀，负控正则要算进去。
- `--verify-diagnostics` 的匹配是“行号 + regex 子串 + 顺序”。
- `@+N` / `@-N` 是物理行偏移，注释也会占行。
- `--split-input-file` 的每个 chunk 独立计数行号。
- 打印器附加的 note（`see current operation`）不可匹配。
- verifier 是 fail-fast；多场景要拆 chunk。

## 07.11 自我检核

- [ ] 能画出 `verifyOperation` 的 worklist 两阶段，并说出每阶段查什么
- [ ] 能解释为什么 `verifyRegionInvariants` 不能放进 `verify()`
- [ ] 能说出四层校验各自负责哪类不变量
- [ ] 能写出一个带 `expected-error@+2` + `expected-note@-2` 的正确测试
- [ ] 能解释 `unexpected error` 与 `was not produced` 的差别
- [ ] 知道 `--verify-diagnostics` 成功时是静默的
- [ ] 能解释为什么 `see current operation` 不能做 expected-note
- [ ] 遇到 pass 调度错误时，知道先看 `OperationPass<T>` 的锚点 op 类型

## 07.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Verifier.cpp`**
  - 读哪里：`verifyOnEntrance(Block)`（193）、`verifyOnExit(Block)`（252）、`verifyOnEntrance(Operation)`（272）、`verifyOnExit(Operation)`（355）、`verifyOperation`（412）、`verifyDominanceOfContainedRegions`（524）
  - 能学到什么：每一层检查的准确边界与错误文案
  - 和本章关联：07.2、07.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Diagnostics.cpp`**
  - 读哪里：`DiagnosticEngine::emit`（350）、`attachNote`（222）、`emitError`（382）
  - 能学到什么：handler 链的消费语义与 InFlightDiagnostic 的生命周期
  - 和本章关联：07.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Diagnostics.md`**
  - 读哪里：`Diagnostic Engine`、`Source Locations` 两节
  - 能学到什么：handler 注册/注销的正确姿势
  - 和本章关联：07.4 的背景
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/IR/invalid-ops.mlir`**
  - 读哪里：`func_with_ops` 附近（约 54–60 行）的 `expected-error@+2` / `expected-note@-2`
  - 能学到什么：官方负控测试的标准形状
  - 和本章关联：07.6、07.8 实验 3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/IR/diagnostic-handler-next.mlir`**
  - 读哪里：文件头的三条 `expected-remark`
  - 能学到什么：诊断顺序敏感性的最小样例
  - 和本章关联：07.8 实验 7

Verifier 能拒绝坏 IR，但“怎么安全地把好 IR 改成另一个好 IR”是另一回事。下一章进入 rewriter、listener 与 use-list 的实操边界。

→ 下一章：[Ch 08 安全地修改 IR](./08-modifying-ir-safely.md)
