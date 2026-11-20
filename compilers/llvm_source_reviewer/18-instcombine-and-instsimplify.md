# Ch 18 — `InstCombine` 与 `InstSimplify`：canonicalization、worklist、fixpoint

> **目标**：读懂中端最忙的两个 pass 的分工与执行模型：`InstSimplify` 为什么只回答「这个式子能不能变成已有值」，`InstCombine` 为什么必须维护 worklist 与迭代上限，以及一个 fold 在什么条件下是合法的（one-use、flags、poison）。学完你能对一条 InstCombine 改动说出「它依赖哪个不变量、在哪一轮生效、什么时候会不收敛」。
>
> **环境**：LLVM 24.0.0git，源码 `main` @ `7c7a9072f736`（2026-11-20）；断言构建 `/home/ubuntu/llvm/builds/pr-lane`（`RelWithDebInfo`，assertions ON，targets `X86;AArch64;RISCV`）。本章所有输出都在这个构建上真实运行。

## 为什么需要这个

InstCombine 同时承担四种角色：常量折叠、规范化（canonicalization）、指令合并、死代码清理，因此它的每条 pattern 都要同时满足正确性、收益与收敛三个条件。一个 patch 看起来只是「多加一条 pattern」，实际可能改掉后续十几个 pass 依赖的 IR 形状，或者引入 poison，或者让 worklist 不收敛。

读它的代码，先要接受一个事实：**它不是「把 IR 变简单」的 pass，而是「把 IR 变成一个约定形状」的 pass**。很多变换在指令数上是 1 换 1，甚至 1 换 2；它们存在是为了让后面的 pattern 能匹配、让 CSE 能合并、让目标后端只写一份 lowering。理解这一点，才能理解 `mul x, 8` 为什么必须变成 `shl x, 3`。

## 先建立直觉

```
        InstSimplify（纯查询，无副作用）
        ┌─────────────────────────────────────────┐
        │ simplifyAddInst(Op0, Op1, nsw, nuw, Q)  │
        │   → 返回 Constant / 已存在的 Value      │
        │   → 或 nullptr（不知道）                │
        │   不创建指令，不删除指令，不改 IR       │
        └─────────────────────────────────────────┘
                          │ 查询
                          ▼
        InstCombine（重写引擎）
        ┌─────────────────────────────────────────┐
        │ worklist: 初始装整个函数的指令           │
        │  pop 一条 → visit(I)                    │
        │    ├─ 先问 InstSimplify（能白拿就白拿）  │
        │    ├─ 再跑各 opcode 的 fold（可建指令）  │
        │    └─ 返回新指令 / 修改原指令 / nullptr  │
        │  替换、插入、ERASE，把用户和操作数重新入队│
        └─────────────────────────────────────────┘
                          │
                    迭代到不动点（或撞上限）
```

两个 pass 的关系不是「简化版 / 完整版」，而是「query / transform」。`InstSimplify` 的返回值有一个硬约束，写在 `llvm/include/llvm/Analysis/InstructionSimplify.h:8-20` 的头注释里：这些函数**不创建新指令**；如果返回的是一条指令，它必须支配原指令。这个约束让它可以被任何 pass 在任意位置安全调用，代价是它做不了分配律、结合律这类需要新指令的重写。

## 分工：一个只回答，一个动手

用同一段 IR 分别跑两个 pass：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin; mkdir -p ~/llvm-lab/ch18 && cd ~/llvm-lab/ch18
cat > simp.ll <<'IR'
define i32 @f(i32 %x) {
  %a = add i32 %x, 1
  %b = mul i32 %a, 2
  ret i32 %b
}
IR
$B/opt -passes=instsimplify -S simp.ll -o -
# 实测：
define i32 @f(i32 %x) {
  %a = add i32 %x, 1
  %b = mul i32 %a, 2
  ret i32 %b
}
$B/opt -passes=instcombine -S simp.ll -o -
# 实测：
define i32 @f(i32 %x) {
  %a = shl i32 %x, 1
  %b = add i32 %a, 2
  ret i32 %b
}
```

`instsimplify` 原样返回：它没有「把乘法分配进去」的能力，因为那要新建一条 `shl` 和一条 `add`。`instcombine` 做了 1 换 2 的展开，指令数没有减少，但形状变成了后端更容易处理的形式（`shl` 与常量加法）。

`InstCombine` 的「动手」有三条路径，都在 `InstructionCombining.cpp` 的主循环里：

- `replaceInstUsesWith(I, V)`：用已有值替换，之后原指令通常被删（`InstCombiner.h:421-442`）。
- 返回一条新指令：主循环把它插到原位置、RAUW、删旧指令（`:5937-5975`）。
- `eraseInstFromFunction(I)`：直接删，配合 `salvageDebugInfo`（`InstCombineInternal.h:507-522`）。

## 一条 `X + 0` 的完整路径

`InstCombinerImpl::visitAdd` 在 `llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp:1590`，第一件事就是把操作数交给 `InstSimplify`：

```cpp
Instruction *InstCombinerImpl::visitAdd(BinaryOperator &I) {
  if (Value *V = simplifyAddInst(I.getOperand(0), I.getOperand(1),
                                 I.hasNoSignedWrap(), I.hasNoUnsignedWrap(),
                                 SQ.getWithInstruction(&I)))
    return replaceInstUsesWith(I, V);
```

`simplifyAddInst` 的定义在 `llvm/lib/Analysis/InstructionSimplify.cpp:594`，`X + 0 -> X` 在 `:607-608`：

```cpp
  // X + 0 -> X
  if (match(Op1, m_Zero()))
    return Op0;
```

再往下是 poison 与 undef 的处理（`:599-605`）：`X + poison -> poison`、`X + undef -> undef`。顺序有意义：常量折叠（`foldOrCommuteConstant`，`:566`）先做，然后 poison/undef，然后才是代数恒等式。`foldOrCommuteConstant` 里还有一条 canonicalization：交换性运算把常量挪到右边（`:585-587`），所以 `add i32 5, %x` 进来时，`Op1` 已经是 `%x`，`m_Zero()` 的判断对象才是正确的。

实测整条链：

```bash
cat > zero.ll <<'IR'
define i32 @f(i32 %x) {
  %y = add i32 %x, 0
  ret i32 %y
}
IR
$B/opt -passes=instcombine -debug-only=instcombine -disable-output zero.ll
# 实测（节选）：
INSTCOMBINE ITERATION #1 on f
ADD:   ret i32 %y
ADD:   %y = add i32 %x, 0
IC: Visiting:   %y = add i32 %x, 0
IC: Replacing   %y = add i32 %x, 0
    with i32 %x
IC: Mod =   %y = add i32 %x, 0
    New =   %y = add i32 %x, 0
IC: ERASE   %y = add i32 %x, 0
IC: Visiting:   ret i32 %x

INSTCOMBINE ITERATION #2 on f
```

三段日志对应三条路径：`IC: Replacing`（`InstCombiner.h:433`，RAUW 到已有值）、`IC: Mod`（返回 `&I`，原地改）、`IC: ERASE`（`InstCombineInternal.h:508`，真删）。`ADD:` 不是 add 指令，是 worklist 的 push 日志（`InstructionWorklist.h:63`）——第一次读很容易认错。

`-print-after` 给的是同一件事的另一种证据，不含解释、只有形状：

```bash
$B/opt -passes=instcombine -print-after=instcombine -S simp.ll -o /dev/null
# 实测：
; *** IR Dump After InstCombinePass on f ***
define i32 @f(i32 %x) {
  %a = shl i32 %x, 1
  %b = add i32 %a, 2
  ret i32 %b
}
$B/opt -passes=instcombine -stats -disable-output simp.ll
# 实测（节选）：
2 instcombine       - Number of insts combined
1 instcombine       - Number of dead inst eliminated
2 instcombine       - Number of instruction combining iterations performed
```

这里 `iterations performed` 是 2，原因就是后面「迭代上限与 fixpoint」一节的 `verify-fixpoint`。`insts combined` 与 `dead inst eliminated` 分开计数：前者统计替换/改写，后者统计被 DCE 掉的指令。

## `foldAddWithConstant`：需要新建指令的那一半

`InstSimplify` 答不上来的情况，`visitAdd` 继续往下走，其中一条是 `foldAddWithConstant`（`InstCombineAddSub.cpp:861`）。它处理「一个操作数是立即常量」的加法，例如：

```cpp
  // add (sub C1, X), C2 --> sub (add C1, C2), X
  if (match(Op0, m_Sub(m_Constant(Op00C), m_Value(X))))
    return BinaryOperator::CreateSub(ConstantExpr::getAdd(Op00C, Op1C), X);
```

实测：

```bash
cat > fac.ll <<'IR'
define i32 @f(i32 %x) {
  %s = sub i32 10, %x
  %a = add i32 %s, 5
  ret i32 %a
}
IR
$B/opt -passes=instcombine -S fac.ll -o -
# 实测输出：%a = sub i32 15, %x
```

如果再加 `-debug-only=instcombine`，日志会依次出现 `IC: Old`、`New = <badref>`、`ADD DEFERRED`、`IC: ERASE`。`<badref>` 是正常的：打印发生在指令插入基本块之前，它还没有名字和父节点；`ADD DEFERRED` 来自 `InstructionWorklist::add`（`:41-43`），表示等当前这条处理完再访问。

## worklist 怎么转

worklist 的实现是 `llvm/include/llvm/Transforms/Utils/InstructionWorklist.h`，三条规则决定它的行为：

1. `push(I)` 用 `WorklistMap` 去重，同一个指令不会重复入栈（`:58-66`）。日志里的 `ADD:` 就是这里。
2. `pushUsersToWorkList(I)` 在替换后把所有 user 入队，因为操作数变了，user 可能能再折（`:106-110`）。
3. `handleUseCountDecrement(V)` 在删指令、减 use 之后调用；如果 `V` 只剩一个 use，把那个 user 也入队（`:112-120`）。注释写得很直白：`Many folds have one-use limitations. If there's only one use left, revisit that use.`

第三条是 one-use fold 能及时触发的原因：一条指令从两 use 变成一 use 的瞬间，依赖 `hasOneUse()` 的 fold 才变得合法/有利，worklist 必须重新看它的 user。

## 迭代上限与 fixpoint

`llvm/include/llvm/Transforms/InstCombine/InstCombine.h:29` 写着：

```cpp
static constexpr unsigned InstCombineDefaultMaxIterations = 1;
```

只读这一行会以为「默认只跑一轮」。但 `opt -passes=instcombine` 走的是文本 pipeline 解析，`llvm/lib/Passes/PassBuilder.cpp:1270-1275` 的 `parseInstCombineOptions` 先把 `VerifyFixpoint` 设成 `true`：

```cpp
Expected<InstCombineOptions> parseInstCombineOptions(StringRef Params) {
  InstCombineOptions Result;
  // When specifying "instcombine" in -passes enable fix-point verification by
  // default, as this is what most tests should use.
  Result.setVerifyFixpoint(true);
```

于是主循环（`InstructionCombining.cpp:6265-6300`）在第一轮改动后不会立刻停：`MaxIterations=1` 让它在第 2 轮开头满足 `Iteration >= MaxIterations`，但 `VerifyFixpoint` 为真，循环继续跑一轮「确认没有新改动」；如果第 2 轮还在改，就触发 fatal error。用 `-print-pipeline-passes` 能看到默认参数：

```bash
$B/opt -passes='instcombine' -print-pipeline-passes -disable-output zero.ll
# 实测：
function(instcombine<max-iterations=1;verify-fixpoint>),verify
```

失败例子用上游 `llvm/test/Transforms/InstCombine/pr55228.ll` 的形态：全局初始化器不是折叠形式时，InstCombine 每轮都能再改一点，收敛不了。把测试里的 `"instcombine-no-verify-fixpoint"` 属性去掉：

```bash
cat > fixpoint.ll <<'IR'
target datalayout = "p:8:8"
@g = external global i8
@c = constant ptr getelementptr inbounds (i8, ptr @g, i64 1)
define i1 @test(ptr %p) {
  %alloca = alloca ptr
  call void @llvm.memcpy.p0.p0.i32(ptr %alloca, ptr @c, i32 0, i1 false)
  %load = load ptr, ptr %alloca
  %cmp = icmp eq ptr %p, %load
  ret i1 %cmp
}
declare void @llvm.memcpy.p0.p0.i32(ptr noalias nocapture writeonly, ptr noalias nocapture readonly, i32, i1 immarg)
IR
$B/opt -passes=instcombine -disable-output fixpoint.ll; echo "EXIT=$?"
# 实测：
LLVM ERROR: Instruction Combining on test did not reach a fixpoint after 1 iterations.

If you encountered this error while working on InstCombine, determine the cause and either fix it, or add the "instcombine-no-verify-fixpoint" attribute to the test, with a comment explaining the reason for the convergence failure.

If you encountered this error in any other situation, use 'instcombine<no-verify-fixpoint>' and do *not* file an issue.
EXIT=1
```

这段错误文本是 review 里的高频信号：看到 `instcombine-no-verify-fixpoint` 属性，就要问「为什么收敛不了，是 fold 振荡还是 pass 顺序问题」。给测试加属性是止血，不是解释。

## canonicalization：为什么形状比指令数重要

两条实测的规范化：

```bash
cat > canon.ll <<'IR'
define i32 @mul8(i32 %x) {
  %m = mul i32 %x, 8
  ret i32 %m
}
define i1 @cmp(i32 %x) {
  %c = icmp ugt i32 10, %x
  ret i1 %c
}
IR
$B/opt -passes=instcombine -S canon.ll -o -
# 实测：
define i32 @mul8(i32 %x) {
  %m = shl i32 %x, 3
  ret i32 %m
}
define i1 @cmp(i32 %x) {
  %c = icmp ult i32 %x, 10
  ret i1 %c
}
```

两条规则：乘法常量变成移位；比较谓词与操作数顺序归一（`10 > x` 变成 `x < 10`）。它们都不减少指令数，甚至可能增加（`mul` 与 `shl` 都是一条，但 `shl` 的立即数形式对后端更友好）。交换性运算的常量归右由 `foldOrCommuteConstant` 负责，前面已经见过。

为什么值得做：

- **pattern 可以只写一半**。`InstSimplify` 的 `simplifyAddInst` 只检查 `Op1` 是不是 0（`:607-608`），因为 `foldOrCommuteConstant` 已经把常量挪到右边。如果每个 pass 都要处理 `0 + x` 与 `x + 0` 两种，代码量翻倍，漏掉一种就是 bug。
- **CSE 与 GVN 才有稳定的 key**。`x + 5` 与 `5 + x` 归一到同一形状，Ch 21 的冗余消除才能合并。
- **后端只写一份 lowering**。`shl` 的常量 shift 与 `mul` 的常量乘，在不同 target 上走不同的 legalization；统一成 `shl` 让 X86 的 `shl` 匹配、AArch64 的 `lsl` 匹配都只写一套。

代价是 canonical form 本身成为一种契约：改它等于改所有下游 pass 的输入。`InstCombineContributorGuide` 里专门有一节 `Canonicalization and target-independence`，讲的就是这条线。

## one-use：fold 的收益门槛

`m_OneUse` 在 `llvm/include/llvm/IR/PatternMatch.h:79`，实现是 `OneUse_match`（`:65-76`）：先 `V->hasOneUse()`，再匹配子模式。它不是语法糖，是收益判断——如果操作数还有别的 user，复制它会让指令数增加，寄存器压力上升。

`foldAddWithConstant` 里的例子（`InstCombineAddSub.cpp:883-886`）：

```cpp
  // add (sub X, Y), -1 --> add (not Y), X
  if (match(Op0, m_OneUse(m_Sub(m_Value(X), m_Value(Y)))) &&
      match(Op1, m_AllOnes()))
    return BinaryOperator::CreateAdd(Builder.CreateNot(Y), X);
```

实测单 use 与双 use 的差别：

```bash
cat > oneuse.ll <<'IR'
define i32 @one(i32 %x, i32 %y) {
  %s = sub i32 %x, %y
  %a = add i32 %s, -1
  ret i32 %a
}
define i32 @two(i32 %x, i32 %y) {
  %s = sub i32 %x, %y
  %a = add i32 %s, -1
  %b = mul i32 %s, %a
  ret i32 %b
}
IR
$B/opt -passes=instcombine -S oneuse.ll -o -
# 实测：
define i32 @one(i32 %x, i32 %y) {
  %1 = xor i32 %y, -1
  %a = add i32 %x, %1
  ret i32 %a
}
```

`one` 里 `sub` 只被 `add` 使用，重写后 `sub` 消失；`two` 的输出与输入相同（此处从略）：`sub` 还被 `mul` 使用，重写会把 `sub` 留下来再加一条 `xor`，指令数从 3 变 4，所以不做。

review 时看到新 pattern 用了 `m_OneUse`，要问两件事：**收益论证在哪**（有没有注释或统计说明为什么一 use 才划算），以及**多 use 时会不会错过明显优化**（该不该同时写一条代价可接受的通用路径）。反过来，把 `m_OneUse` 删掉换更广的匹配，必须有指令数或寄存器压力的证据，不能只说「覆盖更多情况」。

## flags、metadata 与 poison

### `nsw` / `nuw` 不是装饰

加法的 `nsw` 表示「有符号溢出时结果是 poison」。规范化时保留还是丢弃，是一条正确性判断：

```bash
cat > flags.ll <<'IR'
define i8 @reassoc(i8 %x) {
  %a = add nsw i8 %x, 1
  %b = add nsw i8 %a, 1
  ret i8 %b
}
define i32 @drop(i32 %x) {
  %n = xor i32 %x, -1
  %a = add nsw i32 %n, -2147483648
  ret i32 %a
}
IR
$B/opt -passes=instcombine -S flags.ll -o -
# 实测：
define i8 @reassoc(i8 %x) {
  %b = add nsw i8 %x, 2
  ret i8 %b
}
define i32 @drop(i32 %x) {
  %a = sub i32 2147483647, %x
  ret i32 %a
}
```

第一组保留了 `nsw`：两次 `+1` 合成 `+2`，溢出条件等价，可以保留。第二组丢弃了 `nsw`：`~x + INT_MIN` 被改写成 `INT_MAX - x`，`(C-1) - X` 的 `nsw` 需要 `willNotOverflowSignedSub(Op1C, 1, Add)` 成立，这里 `INT_MIN - 1` 会下溢，所以新指令不能带 `nsw`（源码在 `InstCombineAddSub.cpp:899-908`）。

**保留 `nsw` 会引入新的 poison。** 新指令如果不带 `nsw`，只是少了一个优化机会；带错了，原来有定义的程序会变成 UB。review 时看到新构造的 `BinaryOperator`，先看 `setHasNoSignedWrap` / `setHasNoUnsignedWrap` 的实参，再看它依赖的证明函数（`willNotOverflowSignedAdd/Sub`、`computeKnownBits`）是否覆盖了所有输入。

### metadata 与 DebugLoc

替换时 InstCombine 显式搬运两类信息（`InstructionCombining.cpp:5943-5945`）：`Result->setDebugLoc(Result->getDebugLoc().orElse(I->getDebugLoc()))` 与 `Result->copyMetadata(*I, LLVMContext::MD_annotation)`。前者保留调试位置（新指令自己带了就优先用它），后者只复制 `!annotation`。其余 metadata（`!tbaa`、`!range`、`!nonnull` 等）**不会**无条件复制——它们依附于具体指令语义，复制到新指令上可能变成错误断言。Ch 17 里 `%a = shl i32 %x, 1, !dbg !13` 就是 `setDebugLoc` 的实测结果；Ch 8 讲这些 metadata 的语义，Ch 29 讲 review 时怎么判断复制是否合法。

### `select` 的 arm poison

`select` 的特殊之处：它只传播**被选中**那个 arm 的 poison。`select true, 1, poison` 等于 1，不是 poison。所以「`select c, 1, X` 改写成 `zext(c) | X`」看着像省一条指令，实际是把未选中的 arm 拉进了数据流。

先看本构建的 InstCombine 怎么做：

```bash
cat > sel.ll <<'IR'
define i32 @sel(i1 %c, i32 %x) {
  %s = select i1 %c, i32 1, i32 %x
  ret i32 %s
}
IR
$B/opt -passes=instcombine -S sel.ll -o -
# 实测：
define i32 @sel(i1 %c, i32 %x) {
  %s = select i1 %c, i32 1, i32 %x
  ret i32 %s
}
```

`-debug-only=instcombine` 的输出里只有两条 `IC: Visiting`，没有 `Old`/`New`，也就是没有发生替换。这不能证明「它是因为 poison 才拒绝」——也可能只是没有这条 pattern；但可以确定这个 fold 不在它的清单里。用 `lli` 把两种写法都跑一遍，看它们是否还算同一个函数：

```bash
cat > good.ll <<'IR'
define i32 @main() {
  %p = add nsw i32 2147483646, 2
  %s = select i1 true, i32 1, i32 %p
  ret i32 %s
}
IR
cat > bad.ll <<'IR'
define i32 @main() {
  %p = add nsw i32 2147483646, 2
  %z = zext i1 true to i32
  %o = or i32 %z, %p
  %c = icmp eq i32 %o, 1
  %r = select i1 %c, i32 0, i32 7
  ret i32 %r
}
IR
$B/lli good.ll; echo "good exit=$?"
$B/lli bad.ll; echo "bad exit=$?"
# 实测：
good exit=1
bad exit=7
```

`%p` 在运行时溢出成 `INT_MIN`（poison 在真实机器上就是这个环绕值）。good 里 select 选的是 1，poison 不参与；bad 里 `or` 把 poison 拉进来，后面的比较与 select 全部中毒。**`bad.ll` 的行为是 UB，7 只是这次运行的结果，不是保证**；真正的论据是 LangRef 的 select 语义（Ch 30 展开）。这次运行证明的是：两条 IR 已经不是同一个函数。

### `sub X, X` 的边界

`simplifySubInst` 里 `X - X -> 0` 之前还有 poison/undef 检查（`InstructionSimplify.cpp:751-759`）：

```bash
cat > xx.ll <<'IR'
define i32 @same(i32 %x) {
  %r = sub i32 %x, %x
  ret i32 %r
}
define i32 @u() {
  %r = sub i32 undef, undef
  ret i32 %r
}
define i32 @p() {
  %r = sub i32 poison, poison
  ret i32 %r
}
IR
$B/opt -passes=instsimplify -S xx.ll -o -
# 实测：
define i32 @same(i32 %x) {
  ret i32 0
}
define i32 @u() {
  ret i32 undef
}
define i32 @p() {
  ret i32 poison
}
```

三个结果不一样，因为检查顺序：`undef` 与 `poison` 在前，`Op0 == Op1` 在后。把 `sub X, X` 一律折成 0 会漏掉「两个操作数是 undef 时结果仍是 undef」这条语义。这个例子说明 fold 的**匹配条件**与**检查顺序**本身就是正确性的一部分；改顺序不是重构。

## 踩雷集锦

1. **拿 `-debug-only=instcombine` 的 `ADD:` 当成 add 指令。**
   它是 worklist 的 push 日志（`InstructionWorklist.h:63`），`ret`、`sub`、`select` 入队时也会打印 `ADD:`。真正的 add 处理从 `IC: Visiting:` 开始。

2. **只测单 use 的 IR 就提交 one-use fold。**
   实测 `oneuse.ll`：单 use 触发 `sub`+`-1` 的重写，双 use 不触发。测试只覆盖单 use，等于没锁住 guard；反过来删掉 `m_OneUse` 又没测指令数增长。上游 contributor guide 专门要求 `Add multi-use tests`。

3. **把 `nsw` 当成可以随手复制的标记。**
   实测 `drop()`：`~x + INT_MIN` 变成 `INT_MAX - x` 时 `nsw` 被丢弃，因为中间值会下溢。复制 `nsw` 到新指令上，会把有定义的输入变成 poison。

## 本章重点整理

- `InstSimplify` 是 query：不建指令、不删指令，返回值要么是常量，要么是支配原指令的已有值。
- 文本 pipeline 的 `instcombine` 默认带 `verify-fixpoint`，默认上限 1 轮的实际表现是「1 轮改写 + 1 轮确认」。

## 自我检核

- [ ] 不看源码，说出 `InstSimplify` 的返回值约束，以及它为什么不能做分配律。
- [ ] 画出 `visitAdd` → `simplifyAddInst` → `replaceInstUsesWith` 的调用链，并指出 `X + 0` 的判据在哪两行。
- [ ] 解释 `-passes=instcombine` 为什么在 `-stats` 里显示 2 轮，而 `InstCombine.h` 写的是 1。
- [ ] 说明 `ADD:`、`ADD DEFERRED:`、`IC: Visiting:`、`IC: ERASE` 分别由哪段代码打印。
- [ ] 给出一个必须用 `m_OneUse` 的理由，以及一个不该用 `m_OneUse` 的反例；用 `%s` 有 1、2、3 个 use 的 IR 验证你的判断。
- [ ] 说明 `-print-after=instcombine` 与 `-debug-only=instcombine` 各自能回答什么，不能回答什么。
- [ ] 说明 `select` 的 arm poison 规则，以及为什么 `zext(c) | X` 不是等价改写。
- [ ] 看到新 fold 给 `BinaryOperator` 设置 `nsw`，你会检查哪两件事？

## 延伸阅读

- **[InstCombine Contributor Guide](https://llvm.org/docs/InstCombineContributorGuide.html)**
  - **读什么**：`Canonicalization and target-independence`、`Multi-use handling`、`Flag handling`、`Guidelines for reviewers` 四节
  - **能学到什么**：本章只给了原则，这份文档给的是上游实际执行的规则：什么时候该改 canonical form、多 use 什么时候允许、flag 怎么测、reviewer 会问什么
  - **前提**：读完本章，知道 worklist、one-use、`nsw` 的基本语义
- **[LLVM Language Reference — Poison Values](https://llvm.org/docs/LangRef.html#poison-values)**
  - **读什么**：`Poison Values` 整节，以及 `select` 指令条目里关于未选中 arm 的段落
  - **能学到什么**：本章 `select` 例子背后的完整规则；`nsw` 溢出为什么产生 poison 而不是 UB
  - **前提**：能区分 `undef` 与 `poison`，知道 `freeze` 的作用
- **[Alive2: Bounded Translation Validation for LLVM](https://doi.org/10.1145/3453483.3454030)**（PLDI 2021）
  - **读什么**：第 3–4 节的语义模型与 refinement 定义，以及第 6 节的 InstCombine 验证案例
  - **能学到什么**：本章用 `lli` 做的观察只是「两条 IR 行为不同」，Alive2 给的是「对所有输入是否等价」的有界证明；理解 refinement 方向（允许 poison 变成具体值，不允许反过来）
  - **前提**：读过 LangRef 的 poison/undef，能读 SMT 风格的反例描述

→ [Ch 19 — `SimplifyCFG` 与 CFG 变换：`DomTreeUpdater` 的正确用法](./19-simplifycfg-and-cfg-transforms.md)
