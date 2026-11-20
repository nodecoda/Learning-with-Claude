# Ch 9 — Verifier 与 IR 不变量：什么样的 IR 才算合法

> **目标**：把「这段 IR 能不能跑」拆成可检查的结构性条件。学完这章，你能从一条 Verifier 报错反推到 `llvm/lib/IR/Verifier.cpp` 的具体检查，能用 `opt -passes=verify` 与 `-verify-each` 区分「输入本来就坏」和「某个 pass 改坏了」，并清楚 Verifier 的职责边界：它证明 IR 结构合法，不证明程序语义正确。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，`RelWithDebInfo` + assertions ON。本章所有命令与输出都在这个构建上真实运行。

## 为什么需要 Verifier

LLVM 的 pass 之间靠 IR 的结构性契约协作：值在被使用前必须已定义且支配使用点；`phi` 的每个前驱必须恰好有一条入口；`musttail` 调用后面必须紧跟 `ret`；`landingpad` 必须位于带 personality 的函数的非入口块。任何一个 pass 破坏这些条件，下游 pass 就可能读到空指针、走到不存在的块，或者算出错误结果。

把检查集中到 Verifier，而不是散落在每个 pass 的 `assert` 里，有三个直接好处：单一判据（所有 pass 对「合法 IR」的理解来自同一份实现）、可开关（发布构建关掉逐 pass 验证，调试构建打开 `-verify-each`）、错误可归因（报错文本是源码里的字符串字面量，配合 `-verify-each` 能定位到具体 pass）。

Verifier 的头注释写得很克制：它做的是 well-formed 检查，不提供完整的安全验证（`llvm/lib/IR/Verifier.cpp:10-13`）。这句话是本章的边界，也是 Ch 30 的伏笔。

## 心智模型：两道门，不是一道

```
.ll 文本
   │
   ▼
[AsmParser / LLParser]  ── 语法、名字、操作数类型、SSA 名字解析
   │  通过
   ▼
[Verifier]              ── 支配、CFG、属性、metadata、EH、musttail、convergence
   │  通过
   ▼
合法 IR（结构合法）
   │
   ▼
[pass / 优化 / 代码生成] ── 语义正确性、poison、UB、别名，Verifier 不管
```

很多「类型不匹配」在文本解析阶段就被拒绝，根本到不了 Verifier；反过来，支配关系、`phi` 前驱数量这类跨基本块条件，解析器不做，必须由 Verifier 计算 DominatorTree 后检查。把两层混在一起，会导致错误的调试方向：看到类型报错却去翻 `Verifier.cpp`，或者看到支配报错却怀疑解析器。

## Verifier 的架构

### 入口：`verifyFunction` 与 `verifyModule`

公开入口在 `llvm/include/llvm/IR/Verifier.h:91` 和 `:103`：

```cpp
bool verifyFunction(const Function &F, raw_ostream *OS = nullptr);
bool verifyModule(const Module &M, raw_ostream *OS = nullptr,
                  bool *BrokenDebugInfo = nullptr);
```

返回值是反的：`true` 表示「坏了」。`verifyFunction` 的实现是 `return !V.verify(F);`（`llvm/lib/IR/Verifier.cpp:8055`），`verifyModule` 先逐个函数调用 `V.verify(F)`，再调用一次模块级的 `V.verify()`（`:8066`）。这就是为什么 `opt` 报 `input module is broken!`，而不是打印布尔值。

模块级检查包括全局变量、别名、ifunc、命名 metadata、comdat、module flags、ident、command line、errno TBAA、raw sections、compile units（`Verifier::verify()`，`Verifier.cpp:293-330`）。它**没有**走 `visitModule`；`InstVisitor` 虽然提供了 `visit(Module&)` 槽位，但 `Verifier` 的模块路径是手写的 `verify()`。读源码时以实际调用为准。

### 分派链：`visitFunction` → `visitBasicBlock` → `visit(Instruction&)`

`class Verifier : public InstVisitor<Verifier>, VerifierSupport`（`Verifier.cpp:149`）。`InstVisitor` 的三层分派在 `llvm/include/llvm/IR/InstVisitor.h:90-100`：

```cpp
void visit(Function &F) {
  static_cast<SubClass*>(this)->visitFunction(F);
  visit(F.begin(), F.end());
}
void visit(BasicBlock &BB) {
  static_cast<SubClass*>(this)->visitBasicBlock(BB);
  visit(BB.begin(), BB.end());
}
```

`Verifier::visitFunction`（`Verifier.cpp:3366`）先检查函数签名、链接属性、调用约定、参数属性、personality；`InstVisitor::visit(Function&)` 在它返回后继续遍历基本块。`Verifier::visitBasicBlock`（`:3727`）清空「本块已见指令」集合，调用 convergence helper，然后检查 `phi` 的前驱匹配。每条指令进入 `Verifier::visit(Instruction&)`（`:565`）：

```cpp
void Verifier::visit(Instruction &I) {
  visitDbgRecords(I);
  for (unsigned i = 0, e = I.getNumOperands(); i != e; ++i)
    Check(I.getOperand(i) != nullptr, "Operand is null", &I);
  InstVisitor<Verifier>::visit(I);
}
```

最后的 `InstVisitor<Verifier>::visit(I)` 是一个按 opcode 展开的 `switch`（`InstVisitor.h:119-125`），把 `Add` 分派到 `visitBinaryOperator`，把 `PHI` 分派到 `visitPHINode`，把 `Call` 分派到 `visitCallBase`。`Verifier` 只覆盖关心的 opcode；没覆盖的落到 `visitInstruction`。


### 为什么自己重算 DominatorTree

`verify(F)` 在遍历前执行 `DT.recalculate(F)`（`Verifier.cpp:265`）。注释说明了原因：Verifier 不想依赖 pass manager 提供的 DominatorTree，因为那棵树可能已经过期；自己重算可以把 Verifier 与「analysis 是否被正确失效」解耦，也能在 pass manager 之外单独调用。**Verifier 不相信任何缓存分析**。

## 不变量分类

| 类别 | 典型问题 | 源码锚点 |
| --- | --- | --- |
| SSA 支配 | 使用点不在定义点的支配范围内 | `Verifier.cpp:5616-5637` |
| 类型一致 | 二元运算操作数类型不同、调用参数不匹配、intrinsic 签名错误 | `Verifier.cpp:4194-4209`；`Intrinsics.cpp:1358` |
| CFG | 没有 terminator、terminator 在块中间、入口块有前驱 | `Verifier.cpp:248-260`、`:3782-3785` |
| PHI | 前驱数量不匹配、同一前驱多条不同入口值 | `Verifier.cpp:3738-3766` |
| 属性 | `noundef`/`nonnull` 用在非法类型、互斥属性并存 | `Verifier.cpp:2344-2438` |
| Metadata | `!range` 挂在非 load/call/phi 上、metadata 形状错误 | `Verifier.cpp:6215-6217` |
| EH / landingpad | entry 块出现 EH pad、缺 personality、不是首个非 PHI 指令 | `Verifier.cpp:5205-5222`、`:5288-5311` |
| musttail | 后面不是 `ret`、返回类型/调用约定不匹配 | `Verifier.cpp:4533-4560` |
| convergence | token 产生/使用位置、支配、嵌套、循环 heart | `ConvergenceVerifier.cpp:41-68`；`GenericConvergenceVerifierImpl.h:145-183` |

### SSA 支配

`verifyDominatesUse`（`Verifier.cpp:5616`）先做同块快速判断：如果定义已经在当前块出现过，且当前指令不是 `phi`，就直接返回。`phi` 的使用发生在入边上，不是指令位置，所以不能套用同块规则。跨块情况落到：

```cpp
Check(DT.dominates(Op, U), "Instruction does not dominate all uses!", Op, &I);
```

这就是 `Verifier.cpp:5637`。这条检查只回答「定义是否支配使用」，不回答「值是否可能是 poison」。`add nsw` 溢出产生 poison 时，SSA 支配完全成立，Verifier 不会报错。

### 类型一致

类型检查分两层。文本解析器会拒绝 `%y = add i32 %x, %z` 里 `%z` 是 `i64` 的情况；到了 Verifier，`visitCallBase` 还会检查调用参数类型与函数签名一致（`Verifier.cpp:4194`），intrinsic 的原型则由 `Intrinsic::isSignatureValid` 按 `IITDescriptor` 表匹配（`llvm/lib/IR/Intrinsics.cpp:1358`）。所以「类型不匹配」既可能是 parser error，也可能是 verifier error，取决于错误发生在哪一层。

### CFG 与 PHI

`verify(F)` 在算支配树之前先确认每个非空基本块都以 terminator 结尾（`Verifier.cpp:248-260`）；没有 terminator 连 CFG 都不成立，直接返回。`visitTerminator` 再检查 terminator 只能出现在块尾（`:3782-3785`）。

`phi` 的检查在 `visitBasicBlock`（`:3727`）：先要求入口数等于前驱数（`:3738`），再排序后检查同一前驱的多条入口值是否相同（`:3756`），最后检查入口块集合与前驱集合逐项匹配（`:3764`）。这三条分别对应三种不同的坏 IR，报错文本也不同。

### 属性：`noundef` / `nonnull`

`nonnull` 和 `noundef` 都是参数/返回值属性，定义在 `llvm/include/llvm/IR/Attributes.td:240` 与 `:110`。`verifyParameterAttrs`（`Verifier.cpp:2339`）先检查属性是否允许出现在参数上，再用 `AttributeFuncs::typeIncompatible` 判断类型是否兼容（`:2431-2437`）。例如 `nonnull` 只能用于指针类型，`noundef` 不能用于 `void`。

这类检查只保证属性摆放合法，不保证调用者真的满足契约。把 `null` 传给 `nonnull` 参数、把 `undef` 传给 `noundef` 参数，都是 LangRef 意义上的 UB，但 Verifier 会放行。后面「合法 IR 不等于语义正确」会给出真实输出。

### Metadata 与 EH

`visitInstruction` 的 metadata 分支检查「这种 metadata 能挂在哪类指令上」以及操作数形状。`!range` 只允许出现在 load、call、invoke、phi 上（`Verifier.cpp:6215-6217`）；`!nonnull` 只允许 load/phi；`!noundef` 只允许 load/phi（`:6243-6255`）。这些检查不验证 metadata 声称的事实是否真实，只验证附着点与编码是否合法。

`visitEHPadPredecessors`（`Verifier.cpp:5205`）要求 EH pad 不能位于 entry 块（`:5211`），并且 landingpad 所在块只能由 `invoke` 的 unwind 边到达（`:5218-5222`）。`visitLandingPadInst`（`:5288`）要求至少一个 clause 或 `cleanup`（`:5291`）、函数有 personality（`:5306`）、landingpad 是块内首个非 PHI 指令（`:5311`），同一函数内所有 landingpad 结果类型一致（`:5299`）。

### musttail 与 convergence

`verifyMustTailCall`（`Verifier.cpp:4533`）检查：不能是 inline asm、varargs 匹配、返回类型匹配、调用约定匹配、后面必须紧跟 `ret`、`ret` 必须返回调用结果或 void（返回类型在 `:4541`，`ret` 形态在 `:4554-4557`）。`tailcc`/`swifttailcc` 还有更细的 ABI 属性限制（`:4560-4578`）。这些条件都是为了让后端能安全地做尾调用消除；少一条，代码生成就可能无法保证栈帧复用。

Convergence 的静态规则放在 `ConvergenceVerifier`：`Verifier` 在 `verify(F)` 里初始化它（`Verifier.cpp:270`），在 `visitBasicBlock` 与 `visitCallBase` 里喂给它（`:3729`、`:4493`），最后统一 `verify(DT)`（`:277-278`）。`ConvergenceVerifier.cpp:41-68` 检查 `convergencectrl` bundle 的数量、token 类型、token 是否由 convergence intrinsic 产生；`GenericConvergenceVerifierImpl.h:145-183` 检查 token 支配、嵌套、循环 heart。这是「Verifier 也会调用专门 helper」的例子。

## 实验矩阵：坏 IR 与真实报错

每段都在 `/home/ubuntu/llvm-lab/ch09` 下保存为 `.ll`，统一用：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt -passes=verify -disable-output <file>.ll
```

### E1 支配违规

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

```text
Instruction does not dominate all uses!
  %x = add i32 1, 2
  %y = add i32 %x, 3
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e1-dom.ll: error: input module is broken!
```

### E2 PHI 前驱不匹配

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

```text
PHI node entries do not match predecessors!
  %p = phi i32 [ 1, %a ], [ 2, %entry ]
label %entry
label %a
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e2-phi.ll: error: input module is broken!
```

### E3 类型不匹配：parser 先拦住

```llvm
define i32 @g(i32 %x, i64 %z) {
entry:
  %y = add i32 %x, %z
  ret i32 %y
}
```

```text
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e3b-type.ll:3:20: error: '%z' defined with type 'i64' but expected 'i32'
  %y = add i32 %x, %z
                   ^
```

这段 IR 没有到达 Verifier。解析器在构造 `add` 时就检查操作数类型，错误定位到列号。要看到 Verifier 层的类型检查，得用 intrinsic 原型这类 parser 不负责的契约，见 E7。

### E4 重复 block 名：parser 把两个标签解析成同一个块

```llvm
define i32 @g(i1 %c) {
entry:
  br label %dup
dup:
  ret i32 0
dup:
  ret i32 1
}
```

```text
Terminator found in the middle of a basic block!
label %dup
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e4c-dupbb.ll: error: input module is broken!
```

文本里的第二个 `dup:` 没有创建第二个 `BasicBlock`；`LLParser::PerFunctionState::defineBB` 通过 `getBB` 拿到已有块（`llvm/lib/AsmParser/LLParser.cpp:4215-4240`），把它移到函数末尾后继续追加指令，于是 Verifier 看到的是「terminator 在块中间」。这条实验说明：**同一段坏 IR 的报错文本，取决于 parser 如何构造对象**，不能只看文本猜检查点。

### E5 坏的 musttail

```llvm
declare i32 @callee(i32)

define i32 @caller(i32 %x) {
entry:
  %r = musttail call i32 @callee(i32 %x)
  %s = add i32 %r, 1
  ret i32 %s
}
```

```text
musttail call must precede a ret
  %r = musttail call i32 @callee(i32 %x)
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e5-musttail.ll: error: input module is broken!
```

### E6 非法属性组合

```llvm
define i32 @g(i32 %x) alwaysinline noinline {
entry:
  ret i32 %x
}
```

```text
Attributes 'noinline and alwaysinline' are incompatible!
ptr @g
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e6-attr.ll: error: input module is broken!
```

### E7 intrinsic 签名

intrinsic 签名错误则走 Verifier 的 intrinsic 匹配：

```llvm
declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i32)

define void @g(ptr %d, ptr %s) {
entry:
  call void @llvm.memcpy.p0.p0.i64(ptr %d, ptr %s, i64 4, i32 0)
  ret void
}
```

```text
intrinsic argument 3 type expected i1, but got i32
declare void @llvm.memcpy.p0.p0.i64(ptr, ptr, i64, i32)
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e11b-intrinsic.ll: error: input module is broken!
```

### E8 landingpad

```llvm
define i32 @g() {
entry:
  %lp = landingpad { ptr, i32 } cleanup
  ret i32 0
}
```

```text
EH pad cannot be in entry block.
  %lp = landingpad { ptr, i32 }
          cleanup
LandingPadInst needs to be in a function with a personality.
  %lp = landingpad { ptr, i32 }
          cleanup
/home/ubuntu/llvm/builds/pr-lane/bin/opt: e8-landingpad.ll: error: input module is broken!
```

## `-verify-each`：把坏 IR 归因到 pass

`opt` 默认在输入和输出各验证一次。`optdriver.cpp:810-825` 把默认值设为 `VerifierKind::InputOutput`；只有传入 `-verify-each` 才切换到 `EachPass`。新 pass manager 侧的注册在 `StandardInstrumentations.cpp:2510`，具体回调在 `:1427-1460`：每个 pass 结束后调用 `verifyFunction` 或 `verifyModule`，失败就用 `report_fatal_error` 报出 pass 名。

用一个故意破坏函数的测试 pass 演示（`llvm/lib/Passes/PassRegistry.def:560`）：

```bash
$B/opt -disable-output -disable-verify -passes='function(trigger-verifier-error)' ok.ll
$B/opt -disable-output -debug-pass-manager -verify-each \
  -passes='function(trigger-verifier-error)' ok.ll 2>&1 | head -6
```

```text
Running pass: TriggerVerifierErrorPass on g (2 instructions)
Verifying function g
Terminator found in the middle of a basic block!
label %entry
LLVM ERROR: Broken function found after pass "TriggerVerifierErrorPass", compilation aborted!
```

第一行命令带 `-disable-verify`，退出码 0，什么也不报；第二行在 pass 后立刻抓到，并把 pass 名写进错误。为便于阅读只保留了前 6 行，完整运行还会打印 stack dump 并 abort。

为什么它能区分「pass 产生」与「输入本来就坏」？因为 `opt` 在跑 pipeline 之前已经验证过输入。把 E1 的坏 IR 交给 `-verify-each`，它会在任何 pass 运行前退出：

```text
Instruction does not dominate all uses!
  %x = add i32 1, 2
  %y = add i32 %x, 3
/home/ubuntu/llvm/builds/pr-lane/bin/opt: t1-bad.ll: error: input module is broken!
```

错误里没有 `after pass "..."`，说明它发生在 pipeline 之前。反过来，带 `after pass "TriggerVerifierErrorPass"` 的错误，输入必然已经通过验证，坏 IR 只能来自这个 pass。没有 `-verify-each` 时，`opt` 仍会在末尾验证并 abort，但错误只说 `Broken module found`，你已经失去了「哪个 pass」这条信息。

## MachineVerifier 与 IR Verifier 的分工

IR Verifier 管机器无关 IR；进入 `llc` 后，`MachineFunction` 有自己的不变量：虚拟寄存器只能定义一次、定义必须先于使用、操作数必须符合指令描述、bundle 标志一致、MBB 前驱/后继一致、live interval 与 slot index 合理。`MachineVerifier.cpp:11-20` 的头注释列了其中几类。

| 维度 | IR Verifier | MachineVerifier |
| --- | --- | --- |
| 输入 | `Module` / `Function` | `MachineFunction` |
| 入口 | `verifyModule` / `verifyFunction` | `MachineFunction::verify`（`MachineVerifier.cpp:433-442`） |
| 主要检查 | SSA、类型、CFG、属性、metadata、EH、musttail、convergence | 寄存器类、虚拟寄存器定义/使用、live interval、bundle、MBB 结构 |
| 打开方式 | `opt -passes=verify`、`-verify-each` | `llc -verify-machineinstrs`、`-verify-each` |
| 源码 | `llvm/lib/IR/Verifier.cpp` | `llvm/lib/CodeGen/MachineVerifier.cpp` |

`MachineVerifier::verify`（`:473`）先处理 `FailedISel` 与 `NoVRegs` 这类属性，再 `visitMachineFunctionBefore()`（`:691`）、逐 MBB/MI 遍历、`visitMachineFunctionAfter()`。`report`（`:581`）负责把错误连同函数、MBB、指令上下文打出来。一个 reviewer 需要知道的细节：`runOnMachineFunction` 会跳过带 `fails-verification` 属性的函数（`:389-397`），`verify` 对 `FailedISel` 也直接返回 `true`（`:487-488`）。「MachineVerifier 没报错」有时是因为它被明确跳过了，不一定是机器 IR 真的干净。Ch 24 会展开这些属性与 MIR 的关系。

## 合法 IR 不等于语义正确

Verifier 通过，只能说明结构合法。下面两段 IR 都通过 `opt -passes=verify`：

```llvm
define void @f(i32 noundef %x) {
entry:
  ret void
}

define void @g() {
entry:
  call void @f(i32 undef)
  ret void
}
```

```llvm
declare void @f(ptr nonnull)

define void @g() {
entry:
  call void @f(ptr null)
  ret void
}
```

两段都退出 0，没有输出。`noundef` 表示「不能是 undef/poison」，`nonnull` 表示「不能是 null」；把 `undef` 或 `null` 传进去是调用点的 UB，但 Verifier 不做值分析，也不追溯调用者是否满足契约。它检查的是属性摆放合法，不是属性被遵守。

poison 更典型：

```llvm
define i32 @poison_demo(i32 %x) {
entry:
  %a = add nsw i32 %x, 1
  %b = icmp eq i32 %a, 0
  %r = select i1 %b, i32 42, i32 7
  ret i32 %r
}
```

`opt -passes=verify -disable-output t6-poison.ll` 退出 0。但当 `%x == INT_MAX` 时，`add nsw` 溢出产生 poison，`icmp` 的结果是 poison，`select` 的条件是 poison，返回值也是 poison；返回 poison 触发 UB。Verifier 不执行这段函数，不枚举输入，也不传播 poison，所以它抓不到。

`opt -passes=instcombine -S t6-poison.ll` 会把它改写成：

```llvm
define i32 @poison_demo(i32 %x) {
entry:
  %b = icmp eq i32 %x, -1
  %r = select i1 %b, i32 42, i32 7
  ret i32 %r
}
```

这个改写对 `x != INT_MAX` 的定义域是等价的；`x == INT_MAX` 时原程序本来就是 UB，优化器可以把 UB 收缩成任意行为。问题不在改写，而在源 IR 已经写下了 `nsw` 这个「不会溢出」的承诺。谁该为这个承诺负责？写 `nsw` 的 pass 或前端。Verifier 不审承诺是否合理，只审 IR 是否 well-formed。Ch 30 会把 poison、`freeze`、`undef` 和 UB 的审查方法展开。

## 本章重点整理

- Verifier 的入口是 `verifyFunction` / `verifyModule`，返回 `true` 表示坏；模块级检查走 `Verifier::verify()`，不是 `visitModule`。
- 分派链是 `visitFunction` → `visitBasicBlock` → `visit(Instruction&)` → `InstVisitor` 的 opcode switch；Verifier 自己重算 DominatorTree，不信任缓存。
- 报错文本就是源码里的字符串字面量；用 `rg '<报错文本>' llvm/lib/IR/Verifier.cpp` 能直接定位检查点。
- 不是所有坏 IR 都到 Verifier：类型/名字/语法问题常被 parser 先拦住；重复 block 名会被 parser 解析成同一个块，最终以「terminator 在块中间」出现。
- `-verify-each` 在每个 pass 后验证；因为输入已经先验证过，带 `after pass "..."` 的错误可以归因到该 pass。
- IR Verifier 管结构，MachineVerifier 管机器 IR；后者会跳过带 `fails-verification` 或 `FailedISel` 的函数，不能把「没报错」当成绝对保证。
- Verifier 不检查 poison、UB、别名、值域；合法 IR 仍然可能语义错误。

## 自我检核

- [ ] 不看源码，画出 `verifyModule` → `verify(F)` → `visitFunction` → `visitBasicBlock` → `visit(Instruction&)` 的调用链，并说出每一步负责哪类检查。
- [ ] 解释为什么 `verifyFunction` 返回 `true` 表示坏，以及这个反直觉返回值在 `opt` 报错里如何体现。
- [ ] 拿一条 `Instruction does not dominate all uses!`，用 `rg` 找到 `Verifier.cpp` 的检查行，并解释同块快速判断与 `phi` 例外的原因。
- [ ] 区分「parser 拒绝」与「Verifier 拒绝」：各举一个本章实验，说明报错格式的差异。
- [ ] 解释 `-verify-each` 为什么能定位到 pass，而默认的末尾验证不能；说出 `-disable-verify` 会隐藏什么。
- [ ] 给出一个 Verifier 通过但语义错误的 IR，并说明 Verifier 缺少哪类信息才抓不到它。
- [ ] 说出 IR Verifier 与 MachineVerifier 各自检查的两类不变量，以及 MachineVerifier 可能跳过函数的原因。
- [ ] 能说出 Verifier 不做哪些检查（语义、poison 传播、性能），并各举一个例子

## 延伸阅读

- **[LLVM Language Reference: Poison Values](https://llvm.org/docs/LangRef.html#poisonvalues)**
  - **读哪里**：`poison`、`undef`、`freeze`、`nsw`/`nuw` 的语义段
  - **能学到什么**：补齐本章故意留白的语义层；读完再回看 `add nsw` 的例子，能说清「为什么 Verifier 放行」
  - **前提知识**：本章的 SSA 与支配概念
- **[LLVM Language Reference: Parameter Attributes](https://llvm.org/docs/LangRef.html#paramattrs)**
  - **读哪里**：`noundef`、`nonnull`、`dereferenceable` 条目
  - **能学到什么**：属性是调用者与被调用者之间的契约，Verifier 只查摆放；Ch 30 会审契约是否被满足
  - **前提知识**：Ch 7 的 `AttributeList`
- **[LLVM 源码：`llvm/lib/IR/Verifier.cpp`](https://github.com/llvm/llvm-project/blob/main/llvm/lib/IR/Verifier.cpp)**
  - **读哪里**：文件头注释、`verify(F)`、`visitBasicBlock`、`visitCallBase`、`verifyMustTailCall`
  - **能学到什么**：把本章的报错文本逐条对回检查；注意行号会随版本漂移，用符号搜索
  - **前提知识**：C++ 的 `switch`、`dyn_cast`、`InstVisitor` 模板
- **[Alive2: Bounded Translation Validation for LLVM](https://doi.org/10.1145/3453483.3454030)**
  - **读哪里**：Introduction 与 case studies；重点看它如何把「IR 合法」与「变换语义正确」分开
  - **能学到什么**：Verifier 之外的语义验证思路；Ch 30 讨论 poison/UB 时的工具背景
  - **前提知识**：LLVM IR 语义、基本的 SMT/翻译验证概念

→ [练习 B — 给一段「看起来能跑」的 IR 找不变量违规](./practice-b-ir-invariant-hunt.md)
