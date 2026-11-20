# 练习 B — 给一段「看起来能跑」的 IR 找不变量违规

> **目标**：把 Ch 9 的 Verifier 知识变成可重复的排错动作。你会拿到六段 IR：前五段分别破坏支配、PHI、类型、EH、musttail/属性不变量；第六段能通过 Verifier，却在语义上踩了 poison。每段都要求你定位不变量、修好、用 `opt -passes=verify` 证明，并说清「为什么这条检查在这个位置」。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，assertions ON。练习目录建议用 `/home/ubuntu/llvm-lab/ch09/practice-b`，所有输出都在这个目录与构建上真实产生。

## 使用方式

先建目录，把每题的文件按名字保存：

```bash
mkdir -p /home/ubuntu/llvm-lab/ch09/practice-b
cd /home/ubuntu/llvm-lab/ch09/practice-b
B=/home/ubuntu/llvm/builds/pr-lane/bin
```

每题固定跑三件事：

1. 对原始 IR 跑 `$B/opt -passes=verify -disable-output <file>.ll`，记录完整报错。
2. 用 `rg '<报错文本>' /home/ubuntu/llvm/llvm-project/llvm/lib/IR/Verifier.cpp` 找到检查点，读上下文。
3. 修好后重跑同一条命令，确认退出 0；不要用 `-disable-verify` 绕过。

| 题号 | 主题 | 关键不变量 | 预期报错关键词 |
| --- | --- | --- | --- |
| 1 | 支配 | 定义必须支配所有使用 | `does not dominate all uses` |
| 2 | PHI | 入口数/前驱匹配/同前驱一致 | `PHI node entries`、`multiple entries` |
| 3 | 类型 | parser 类型检查、intrinsic 签名 | `expected 'i32'`、`intrinsic argument` |
| 4 | EH | landingpad 位置与 personality | `EH pad cannot be in entry block` |
| 5 | musttail/属性 | 尾调用形态、互斥属性 | `musttail call must precede a ret` |
| 6 | 语义 | poison 传播不在 Verifier 职责内 | 无报错，退出 0 |

---

## 任务 1 — 支配关系：定义没有覆盖所有路径

**任务规格**：下面函数在 `b` 块使用 `%x`，但 `entry` 可以绕过 `a` 直接到 `b`。要求你指出 Verifier 报的是哪条不变量、为什么 `%x` 在 `b` 不合法、给出两种修法（移动定义 / 改 CFG），并至少实现一种，最后用 verify 证明。

**起始 IR**：`b1-bad.ll`
```llvm
define i32 @g(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  %x = add i32 1, 2
  br label %b
b:
  %y = add i32 %x, 3
  ret i32 %y
}
```

**测试 1（非法输入）**
```bash
$B/opt -passes=verify -disable-output b1-bad.ll; echo "[exit=$?]"
```
```text
Instruction does not dominate all uses!
  %x = add i32 1, 2
  %y = add i32 %x, 3
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b1-bad.ll: error: input module is broken!
[exit=1]
```

**测试 2（修好后）**：把 `%x` 移到 `entry`，让它在两条路径上都已定义。

```bash
$B/opt -passes=verify -disable-output b1-good.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

**分段实作建议**
1. 先只读 IR，画出 `entry → a → b` 与 `entry → b` 两条路径，标出 `%x` 的定义点和使用点。
2. 跑 verify，把报错的两行值抄下来；用 `rg 'does not dominate all uses' /home/ubuntu/llvm/llvm-project/llvm/lib/IR/Verifier.cpp` 找到 `:5637`。
3. 读 `verifyDominatesUse`（`:5616`）的同块快速判断，解释为什么这里不能走同块捷径（`%x` 不在 `b`）。
4. 选一种修法：把 `%x` 移到 `entry`，或让 `b` 只能从 `a` 到达；写出修好的 IR。
5. 重跑 verify，确认没有输出且退出 0；再把使用点换成 `phi`，观察支配规则如何变成「入边支配」。

**卡点提示**
- 报错里的 `%x = add i32 1, 2` 是定义，`%y = add i32 %x, 3` 是使用；Verifier 打印的是 `Op` 与 `I`，不是行号。
- 不要用「把 `%x` 改成常量」绕过问题；那会改变题目要检查的 SSA 结构。
- 把定义移到 `entry` 后，`a` 块只剩 `br label %b`，这是合法的空转块。
- 第二种修法（让 `b` 只从 `a` 到达）也可以，但要保持 `ret` 在所有路径上可达。

<details><summary>参考解答</summary>

修法 A：把 `%x` 提升到 `entry`。

```llvm
define i32 @g(i1 %c) {
entry:
  %x = add i32 1, 2
  br i1 %c, label %a, label %b
a:
  br label %b
b:
  %y = add i32 %x, 3
  ret i32 %y
}
```

验证：
```bash
$B/opt -passes=verify -disable-output b1-good.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

为什么合法：`entry` 支配 `b`，所以定义在 `entry` 的 `%x` 支配 `b` 中的使用。修法 B 是把 `entry` 的条件分支改成只到 `a`，再由 `a` 无条件到 `b`；这样 `%x` 仍然只在 `a` 定义，而 `a` 支配 `b`。两种修法都保持 SSA，区别是前者扩大定义的作用域，后者收窄控制流。

</details>

---

## 任务 2 — PHI：前驱数量、前驱集合、重复入口

**任务规格**：`join` 有两个前驱 `%a` 和 `%b`，但 `phi` 写成了 `[1, %a], [2, %entry]`。要求你说明 Verifier 先检查哪条、再检查哪条，修好主问题；然后额外解释边界情况：同一前驱出现两条不同入口值时，报错为什么不是「不匹配」而是「multiple entries」。

**起始 IR**：`b2-bad.ll`
```llvm
define i32 @g(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  br label %join
b:
  br label %join
join:
  %p = phi i32 [ 1, %a ], [ 2, %entry ]
  ret i32 %p
}
```

**测试 1（非法输入）**
```bash
$B/opt -passes=verify -disable-output b2-bad.ll; echo "[exit=$?]"
```
```text
PHI node entries do not match predecessors!
  %p = phi i32 [ 1, %a ], [ 2, %entry ]
label %entry
label %a
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b2-bad.ll: error: input module is broken!
[exit=1]
```

**测试 2（修好后）**
```bash
$B/opt -passes=verify -disable-output b2-good.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

**测试 3（边界：同一前驱两条不同值）**
```bash
$B/opt -passes=verify -disable-output b2-edge.ll; echo "[exit=$?]"
```
```text
PHI node has multiple entries for the same basic block with different incoming values!
  %p = phi i32 [ 1, %a ], [ 2, %a ]
label %a
i32 2
i32 1
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b2-edge.ll: error: input module is broken!
[exit=1]
```

**分段实作建议**
1. 先数 `join` 的前驱：`%a` 与 `%b`；再数列出的入口：`%a` 与 `%entry`。
2. 跑 verify，注意报错先打印 `%entry` 再打印 `%a`，这是排序后的比较结果，不是源码顺序。
3. 读 `visitBasicBlock`（`Verifier.cpp:3727`）：先 `getNumIncomingValues() == Preds.size()`（`:3738`），再排序比较（`:3756`、`:3764`）。
4. 把 `%entry` 改成 `%b`，重跑 verify。
5. 造一个 `[1, %a], [2, %a]` 的版本，确认入口数仍是 2，但触发「multiple entries」而不是「do not match」。

**卡点提示**
- `phi` 的入口顺序不重要，Verifier 会排序；报错里 `label %entry` 在前，只是因为排序。
- 入口数不等于前驱数时，先触发 `PHINode should have one entry for each predecessor`，后面的匹配检查不一定执行。
- 同一前驱允许多条入口，但值必须完全相同；这是为了 `phi` 的语义在临界边上仍然唯一。
- `%b` 和 `%entry` 都是合法标签，区别在于它们是不是 `join` 的实际前驱。

<details><summary>参考解答</summary>

把第二条入口改成 `%b`：

```llvm
define i32 @g(i1 %c) {
entry:
  br i1 %c, label %a, label %b
a:
  br label %join
b:
  br label %join
join:
  %p = phi i32 [ 1, %a ], [ 2, %b ]
  ret i32 %p
}
```

验证：与测试 2 相同的命令作用于 `b2-good.ll`，输出 `[exit=0]`。

边界版本 `b2-edge.ll` 的入口数是 2，和前驱数相等，所以跳过数量检查；排序后同一 `%a` 出现两次，值 `1` 与 `2` 不同，命中 `Verifier.cpp:3756-3760` 的 "multiple entries" 检查。把两条值改成相同的 `1`，这个边界版本也会通过。

</details>

---

## 任务 3 — 类型：parser 与 Verifier 各管一层

**任务规格**：主 IR 把 `i64` 的 `%z` 直接喂给 `add i32`。要求你判断这条错误由谁报告、为什么 `opt` 给的是列号而不是 `input module is broken!`；修好后再构造一个 parser 不负责、由 Verifier 报告的 intrinsic 签名错误，说明两层类型检查的分工。

**起始 IR**：`b3-bad.ll`
```llvm
define i32 @g(i32 %x, i64 %z) {
entry:
  %y = add i32 %x, %z
  ret i32 %y
}
```

**测试 1（非法输入：parser 层）**
```bash
$B/opt -passes=verify -disable-output b3-bad.ll; echo "[exit=$?]"
```
```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b3-bad.ll:3:20: error: '%z' defined with type 'i64' but expected 'i32'
  %y = add i32 %x, %z
                   ^
[exit=1]
```

**测试 2（修好后：显式截断）**
```bash
$B/opt -passes=verify -disable-output b3-good.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

**测试 3（边界：intrinsic 原型错误，Verifier 层）**
```bash
$B/opt -passes=verify -disable-output b3-intrinsic-bad.ll; echo "[exit=$?]"
```
```text
intrinsic argument 3 type expected i1, but got i32
declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i32)
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b3-intrinsic-bad.ll: error: input module is broken!
[exit=1]
```

**分段实作建议**
1. 先看 `b3-bad.ll` 的报错格式：有文件名、行号、列号、插入符，说明错误发生在解析/构造阶段，不是 Verifier 的 `Check`。
2. 用 `rg 'defined with type' /home/ubuntu/llvm/llvm-project/llvm/lib/AsmParser` 找 parser 的类型检查；确认它在 `add` 构造时就拒绝。
3. 修主 IR：用 `trunc i64 %z to i32` 显式转换，重跑 verify。
4. 构造 intrinsic 版本：把 `llvm.memcpy.p0.p0.i64` 的第四个参数从 `i1` 写成 `i32`，观察报错变成 `input module is broken!`。
5. 读 `Verifier.cpp:4194-4209` 与 `Intrinsics.cpp:1358`，说明为什么调用参数类型与 intrinsic 原型属于 Verifier 层。

**卡点提示**
- 主 IR 的报错没有 `input module is broken!`，这是判断「parser 层」的快速信号。
- `trunc` 会改变值域；如果源语义需要保留 64 位，应该改函数签名或把 `add` 改成 `i64`，不要无条件截断。
- intrinsic 名字里的 `i64` 是 mangling 的一部分，但参数顺序与类型仍要和 `Intrinsics.td` 的声明一致。
- 修 intrinsic 时改调用点不够，声明本身也要改；Verifier 检查的是 `Callee->getFunctionType() == FTy`。

<details><summary>参考解答</summary>

主 IR 的修法：显式截断，保持 `add i32`。

```llvm
define i32 @g(i32 %x, i64 %z) {
entry:
  %z32 = trunc i64 %z to i32
  %y = add i32 %x, %z32
  ret i32 %y
}
```

验证：与测试 2 相同的命令作用于 `b3-good.ll`，输出 `[exit=0]`。

intrinsic 的修法：把声明和调用点的第四参数都改成 `i1`。

```llvm
declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i1)

define void @g(ptr %d, ptr %s) {
entry:
  call void @llvm.memcpy.p0.p0.i64(ptr %d, ptr %s, i64 4, i1 false)
  ret void
}
```

验证：用 `b3-intrinsic-good.ll` 跑同一条命令，输出 `[exit=0]`。

分工：parser 在构造指令时就知道 `add i32` 的操作数必须是 `i32`，所以能在列号处直接拒绝；Verifier 的 `visitCallBase` 检查 `Call.getArgOperand(i)->getType() == FTy->getParamType(i)`（`Verifier.cpp:4194`），intrinsic 还要求 callee 的函数类型与调用点一致（`:4207`），原型本身再由 `Intrinsic::isSignatureValid` 按 `IITDescriptor` 表匹配（`Intrinsics.cpp:1358`）。同叫「类型错误」，责任层不同。

</details>

---

## 任务 4 — EH：landingpad 不能在 entry，也不能缺 personality

**任务规格**：下面函数把 `landingpad` 放在 `entry`，而且没有 personality。要求你解释 Verifier 为什么一次报两条、两条分别对应哪条 CFG/EH 规则；修成一个合法的 `invoke` + `landingpad` + `resume` 结构，再用边界版本说明「landingpad 不是块内首个非 PHI 指令」时的报错。

**起始 IR**：`b4-bad.ll`
```llvm
define i32 @g() {
entry:
  %lp = landingpad { ptr, i32 } cleanup
  ret i32 0
}
```

**测试 1（非法输入）**
```bash
$B/opt -passes=verify -disable-output b4-bad.ll; echo "[exit=$?]"
```
```text
EH pad cannot be in entry block.
  %lp = landingpad { ptr, i32 }
          cleanup
LandingPadInst needs to be in a function with a personality.
  %lp = landingpad { ptr, i32 }
          cleanup
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b4-bad.ll: error: input module is broken!
[exit=1]
```

**测试 2（修好后）**
```bash
$B/opt -passes=verify -disable-output b4-good.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

**测试 3（边界：landingpad 不是首个非 PHI）**
```bash
$B/opt -passes=verify -disable-output b4-edge.ll; echo "[exit=$?]"
```
```text
The unwind destination does not have an exception handling instruction!
  invoke void @may_throw()
          to label %ok unwind label %lpad
LandingPadInst not the first non-PHI instruction in the block.
  %lp = landingpad { ptr, i32 }
          cleanup
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b4-edge.ll: error: input module is broken!
[exit=1]
```

**分段实作建议**
1. 先读 `Verifier.cpp:5205-5222`，确认 `EH pad cannot be in entry block` 来自 `visitEHPadPredecessors`。
2. 再读 `:5288-5311`，确认 personality、clause/cleanup、首个非 PHI 三条检查各自的位置。
3. 写出 `invoke` 结构：`entry` 只做 `invoke`，`ok` 做 `ret`，`lpad` 做 `landingpad` + `resume`。
4. 给函数加 personality，声明 `@__gxx_personality_v0`，重跑 verify。
5. 在 `lpad` 的 `landingpad` 前插一条 `add`，观察边界报错；再删掉 `add` 恢复通过。

**卡点提示**
- `landingpad` 不是普通指令，它定义「这个块是 landing pad」，所以块的位置和入口边都受限。
- 只有 `invoke` 的 unwind 边能进入 landing pad；普通 `br` 进去会被 `Block containing LandingPadInst must be jumped to only by the unwind edge of an invoke` 拒绝。
- 函数级 personality 写在 `define ... personality ptr @...`，不是给 `landingpad` 单独加属性。
- `resume` 的操作数类型必须和 `landingpad` 的结果类型一致；这是另一条类型不变量。

<details><summary>参考解答</summary>

合法结构：

```llvm
declare void @may_throw()
declare i32 @__gxx_personality_v0(...)

define i32 @g() personality ptr @__gxx_personality_v0 {
entry:
  invoke void @may_throw() to label %ok unwind label %lpad
ok:
  ret i32 0
lpad:
  %lp = landingpad { ptr, i32 } cleanup
  resume { ptr, i32 } %lp
}
```

验证：与测试 2 相同的命令作用于 `b4-good.ll`，输出 `[exit=0]`。

两条报错的来源：`visitEHPadPredecessors` 的 `Check(BB != &F->getEntryBlock(), ...)`（`Verifier.cpp:5211`）先报 entry 块；`visitLandingPadInst` 的 `Check(F->hasPersonalityFn(), ...)`（`:5306`）再报缺 personality。边界版本在 `landingpad` 前插入 `%x = add i32 1, 2`，`getLandingPadInst()` 不再返回这条 `landingpad`，于是命中 `:5311` 的 "not the first non-PHI instruction"。同时，`invoke` 的 unwind 目标块因为首指令不是 EH pad，也会被报 "does not have an exception handling instruction"。

</details>

---

## 任务 5 — musttail 与属性：两条独立的契约

**任务规格**：下面 `caller` 同时带 `alwaysinline` 与 `noinline`，并且 `musttail` 调用后面还有一条 `add`。要求你分别指出两条不变量、说明 Verifier 的报错顺序、修好后再用边界版本验证「返回类型不匹配」也会被拒绝。

**起始 IR**：`b5-bad.ll`
```llvm
declare i32 @callee(i32)

define i32 @caller(i32 %x) alwaysinline noinline {
entry:
  %r = musttail call i32 @callee(i32 %x)
  %s = add i32 %r, 1
  ret i32 %s
}
```

**测试 1（非法输入）**
```bash
$B/opt -passes=verify -disable-output b5-bad.ll; echo "[exit=$?]"
```
```text
Attributes 'noinline and alwaysinline' are incompatible!
ptr @caller
musttail call must precede a ret
  %r = musttail call i32 @callee(i32 %x)
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b5-bad.ll: error: input module is broken!
[exit=1]
```

**测试 2（修好后）**
```bash
$B/opt -passes=verify -disable-output b5-good.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

**测试 3（边界：musttail 返回类型不匹配）**
```bash
$B/opt -passes=verify -disable-output b5-edge.ll; echo "[exit=$?]"
```
```text
cannot guarantee tail call due to mismatched return types
  %r = musttail call i64 @callee64(i32 %x)
/home/ubuntu/llvm/builds/pr-lane/bin/opt: b5-edge.ll: error: input module is broken!
[exit=1]
```

**分段实作建议**
1. 先跑 verify，注意属性错误在前、musttail 错误在后；这说明函数级属性检查发生在指令遍历之前。
2. 用 `rg "noinline and alwaysinline" /home/ubuntu/llvm/llvm-project/llvm/lib/IR/Verifier.cpp` 找到 `:2418`；用 `rg "musttail call must precede a ret"` 找到 `:4554`。
3. 读 `verifyMustTailCall`（`:4533`），列出它检查的五件事：非 inline asm、varargs、返回类型、调用约定、后接 `ret`。
4. 删掉两个互斥属性，把 `musttail` 调用改成后接 `ret i32 %r`，重跑 verify。
5. 构造返回类型不匹配的边界版本，确认报错换成 `mismatched return types`。

**卡点提示**
- `alwaysinline` 与 `noinline` 是函数级属性，Verifier 在 `visitFunction` 里就报，不会等到遍历指令。
- `musttail` 要求调用结果被 `ret` 直接返回；`%s = add` 插在中间会同时破坏「紧邻 ret」和「结果被返回」。
- 返回类型不匹配属于「无法保证尾调用」而不是「语法错误」；修法是让 caller/callee 的返回类型一致。
- `musttail` 不是「建议优化」，是语义承诺；后端不能安全消除尾调用时，IR 本身就不合法。

<details><summary>参考解答</summary>

修好的版本：

```llvm
declare i32 @callee(i32)

define i32 @caller(i32 %x) {
entry:
  %r = musttail call i32 @callee(i32 %x)
  ret i32 %r
}
```

验证：与测试 2 相同的命令作用于 `b5-good.ll`，输出 `[exit=0]`。

属性错误的源码在 `verifyParameterAttrs` 的互斥属性检查（`Verifier.cpp:2418`）；`musttail` 的「必须紧跟 ret」在 `verifyMustTailCall`（`:4554`）。前者在 `visitFunction` 阶段触发，后者在 `visitCallBase` 阶段触发，所以同一文件里先看到属性错误。边界版本把 callee 返回类型改成 `i64`、caller 仍是 `i32`，命中 `:4541-4542` 的 `mismatched return types`。

</details>

---

## 任务 6 — Verifier 通过，但语义已经坏了

**任务规格**：下面函数能通过 `opt -passes=verify`，在 `%x == INT_MAX` 时却会传播 poison 并触发 UB。要求你：确认 Verifier 退出 0；逐步说明 poison 如何从 `add nsw` 传到返回值；解释 Verifier 为什么抓不到；给出至少一种修复并验证。再跑一个 `nonnull` 传 `null` 的边界例子，说明属性契约也不在 Verifier 的检查范围内。

**起始 IR**：`b6-poison.ll`
```llvm
define i32 @poison_demo(i32 %x) {
entry:
  %a = add nsw i32 %x, 1
  %b = icmp eq i32 %a, 0
  %r = select i1 %b, i32 42, i32 7
  ret i32 %r
}
```

**测试 1（Verifier 放行）**
```bash
$B/opt -passes=verify -disable-output b6-poison.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

**测试 2（优化器看到的东西）**
```bash
$B/opt -passes=instcombine -S b6-poison.ll
```
```text
; ModuleID = 'b6-poison.ll'
source_filename = "b6-poison.ll"

define i32 @poison_demo(i32 %x) {
entry:
  %b = icmp eq i32 %x, -1
  %r = select i1 %b, i32 42, i32 7
  ret i32 %r
}
[exit=0]
```

**测试 3（边界：nonnull 传 null）**
```bash
$B/opt -passes=verify -disable-output b6-nonnull.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

`b6-nonnull.ll` 的内容：

```llvm
declare void @f(ptr nonnull)

define void @g() {
entry:
  call void @f(ptr null)
  ret void
}
```

**分段实作建议**
1. 先跑 verify，确认退出 0；把「Verifier 通过」和「程序正确」分开记录。
2. 在纸上展开 `%x = INT_MAX`：`add nsw` 溢出 → `%a` 是 poison；`icmp` 读 poison → `%b` 是 poison；`select` 的条件是 poison → `%r` 是 poison；`ret` 返回 poison → UB。
3. 用 `opt -passes=instcombine -S` 看优化器如何把 `add nsw + icmp` 折叠成 `%x == -1`，理解它依赖「`nsw` 承诺不溢出」。
4. 选一种修复：去掉 `nsw`（改成环绕语义），或加 `freeze`/`llvm.assume` 限制输入；写出修好的 IR。
5. 重跑 verify，确认修复版退出 0；再讨论「退出 0 仍然不代表语义符合源程序意图」。

**卡点提示**
- `nsw` 是「no signed wrap」承诺；承诺被违反时结果是 poison，不是「按二进制回绕」。
- Verifier 不做常量折叠、不枚举输入、不跟踪 poison；它只检查指令结构、类型、支配、属性摆放。
- 把 `nsw` 删掉会改变溢出时的语义；如果源程序依赖回绕，这才是正确修法；如果源程序保证不溢出，应该修的是产生 `nsw` 的前端或 pass。
- `nonnull`/`noundef` 是契约属性；Verifier 只检查它们是否放在合法类型上，不检查调用点是否遵守。
- 这类问题属于 Ch 30 的审查范围；Verifier 通过只是进入语义审查的门票。

<details><summary>参考解答</summary>

Verifier 结果见测试 1：`[exit=0]`。

poison 链：`%x = INT_MAX` 时，`add nsw i32 %x, 1` 的有符号结果无法表示，产生 poison；`icmp eq` 的操作数是 poison，结果也是 poison；`select` 的条件是 poison，结果在两条臂之间「无法确定」，按 LangRef 仍然是 poison；`ret` 返回 poison 触发 UB。Verifier 看不到这条链，因为它不执行函数，也没有值域/poison 传播模型。

一种修复是去掉 `nsw`，明确采用二进制回绕：

```llvm
define i32 @poison_demo_fixed(i32 %x) {
entry:
  %a = add i32 %x, 1
  %b = icmp eq i32 %a, 0
  %r = select i1 %b, i32 42, i32 7
  ret i32 %r
}
```

验证：
```bash
$B/opt -passes=verify -disable-output b6-fixed.ll; echo "[exit=$?]"
```
```text
[exit=0]
```

另一种修复是保留 `nsw`，但在使用前 `freeze`，把 poison 变成任意但确定的非 poison 值；这能消除 UB，却不保证得到源程序想要的结果。选哪种取决于源语义：要回绕就去 `nsw`，要「绝不溢出」就应在更早的地方证明输入范围，而不是把 `nsw` 当装饰。

`nonnull` 边界同理：`b6-nonnull.ll` 里 `call void @f(ptr null)` 通过 Verifier，因为属性只被检查「能否放在 `ptr` 参数上」；调用者是否满足 `nonnull` 是运行时/语义层的问题。Ch 30 会把 poison、`freeze`、`undef`、`nsw`/`nuw` 和 UB 的审查方法展开。

</details>

---

## 提交前自检

- [ ] 六题都能在 `practice-b` 目录下用同一条 `opt -passes=verify -disable-output` 复现。
- [ ] 每题至少保留了「非法输入」与「修好后」两组输出；第 2、3、4、5、6 题还保留了边界输出。
- [ ] 能用 `rg '<报错文本>' llvm/lib/IR/Verifier.cpp` 找到至少三条检查的源码行。
- [ ] 能解释任务 3 的 parser/Verifier 分层，以及任务 6 的「Verifier 通过但语义错误」。
- [ ] 没有用 `-disable-verify` 掩盖失败；修复版都是退出 0。

→ [Ch 10](10-adt-and-support.md)
