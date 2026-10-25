# 第 3 章 IRTranslator 与 gMIR：把 SSA IR 变成可选择的机器指令

> **本章目标**：能读懂 `-stop-after=irtranslator` 的输出，知道每条 `G_*` 指令从哪来、每个 vreg 的 LLT 是什么、哪些指令其实已经是 target-specific 的。
>
> **前置**：第 1、2 章。**验证环境**：LLVM 23.1.4 官方包，AArch64 为主、RISC-V 对照。

---

## 3.1 一次翻译的全貌

```llvm
define i64 @add(i64 %a, i64 %b) {
entry:
  %r = add i64 %a, %b
  ret i64 %r
}
```

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=irtranslator add.ll -o add-it.mir
```

```yaml
legalized:       false
regBankSelected: false
selected:        false
isSSA:           true
```

```yaml
body:             |
  bb.1.entry:
    liveins: $x0, $x1

    %0:_(i64) = COPY $x0
    %1:_(i64) = COPY $x1
    %2:_(i64) = G_ADD %0, %1
    $x0 = COPY %2(i64)
    RET_ReallyLR implicit $x0
```

逐行读：

| MIR | 来源 | 说明 |
| --- | --- | --- |
| `liveins: $x0, $x1` | ABI | 函数参数通过物理寄存器传入 |
| `%0:_(i64) = COPY $x0` | 形参 `%a` | 每个形参一个 vreg |
| `%2:_(i64) = G_ADD %0, %1` | `add` 指令 | 目标无关的 generic opcode |
| `$x0 = COPY %2(i64)` | `ret` | 返回值搬回 ABI 规定的寄存器 |
| `RET_ReallyLR` | AArch64 | 已经是 target-specific 伪指令 |

`%0:_(i64)` 里的 `_` 表示**还没有寄存器 bank**。bank 要到 RegBankSelect 才出现（第 2 章的 `gpr`），寄存器类要到 InstructionSelect 才出现（`gpr64`）。

注意最后一条：**gMIR 不是纯 generic 的**。返回、调用、栈调整这些与 ABI 强相关的部分，IRTranslator 直接生成 target 指令或伪指令。

---

## 3.2 gMIR 的三条规则

官方文档 `GMIR.rst` 的定义是："gMIR 与 MIR 共享数据结构，但约束更松；随着流水线推进，约束逐步收紧，直到变成 MIR。"

### 规则一：操作码是 `G_*`

gMIR 定义了一大族目标无关操作码：`G_ADD`、`G_LOAD`、`G_STORE`、`G_PHI`、`G_ICMP`、`G_SELECT`、`G_PTR_ADD`……它们的编号落在 `PRE_ISEL_GENERIC_OPCODE_START..END` 区间内，也就是第 2 章说的"选择后不许留下"的那一族。

### 规则二：vreg 从 LLT 开始，逐步收紧

generic vreg 的生命周期：

```text
LLT（s64 / p0 / <4 x s32>）
  → + register bank（gpr / fpr）        RegBankSelect
  → + register class（gpr64 / fpr32）   InstructionSelect
```

这也是为什么同一份 MIR 在不同阶段打印出 `%2:_(i64)`、`%2:gpr(i64)`、`%2:gpr64`。

### 规则三：立即数和物理寄存器都必须经过 vreg

文档明确写了两条：

- 不能用立即数操作数，要用 `G_CONSTANT` / `G_FCONSTANT` 物化到一个 vreg（`G_SEXT_INREG` 是少数例外）；
- 不能用物理寄存器操作数，要用 `COPY` 从物理寄存器定义 vreg，或用 `COPY` 把 vreg 写回物理寄存器。

这条规则让"每个操作数都是 vreg"成为统一假设，选择器和后续 pass 不需要处理多种操作数形态。代价是常量会占 vreg，产生较长的活跃区间（官方文档也承认这在 -O0 下可能造成不必要的 spill，需要 Localizer 缓解）。

---

## 3.3 LLT：只保留必要信息

`LLT` 是 generic vreg 的类型，形式只有三种：

- `sN`：标量，N 位；
- `pN`：指针，地址空间 N；
- `<N x sM>`：向量。

它**不区分**有符号/无符号，也**不区分**整数/浮点。理由（文档原话）：指令本身已经编码了语义（`add` vs `fadd`、`sdiv` vs `udiv`），类型系统再存一份没有收益。

| LLT | IR 类型 |
| --- | --- |
| `s1` | `i1` |
| `s32` | `i32`、`float` |
| `s17` | `i17` |
| `p0` | 任意 addrspace(0) 指针 |
| `p2` | addrspace(2) 指针 |
| `<4 x s32>` | `<4 x i32>`、`<4 x float>` |
| `<3 x s32>` | `<3 x i32>` |

指针按地址空间区分，这一点比 SelectionDAG 更贴近 IR：DAG 里地址空间是操作上的属性，LLT 里是类型的一部分，因此不同地址空间可以有不同指针宽度。

一个容易困惑的点：`i32` 和 `float` 都是 `s32`。所以 `%0:_(s32)` 到底是整数还是浮点，只能看用它的指令是 `G_ADD` 还是 `G_FADD`。读 MIR 时不要试图从类型行判断。

---

## 3.4 翻译流程

`IRTranslator::runOnMachineFunction` 的骨架（省略错误处理）：

```cpp
  // 1. 目标是否支持这个函数的 ABI lowering
  if (CLI->fallBackToDAGISel(*MF)) { reportTranslationError(...); return false; }

  // 2. 可选 CSE builder
  EntryBuilder = EnableCSE ? new CSEMIRBuilder(CurMF) : new MachineIRBuilder();

  // 3. 先建好所有 MBB，保持 IR 的块顺序
  for (const BasicBlock &BB : F) {
    MBB = MF->CreateMachineBasicBlock(&BB);
    MF->push_back(MBB);
  }

  // 4. 临时的"参数与常量"入口块
  EntryBB = MF->CreateMachineBasicBlock();
  MF->push_back(EntryBB);
  EntryBB->addSuccessor(&getMBB(F.front()));

  // 5. 形参 lowering 到 EntryBB
  CLI->lowerFormalArguments(*EntryBuilder, F, VRegArgs, FuncInfo);

  // 6. 按逆后序（RPO）翻译指令
  ReversePostOrderTraversal<const Function *> RPOT(&F);
  for (const BasicBlock *BB : RPOT)
    for (const Instruction &Inst : *BB)
      if (!translate(Inst)) { reportTranslationError(...); return false; }

  // 7. 回填 PHI 占位、合并入口块
  finishPendingPhis();
  NewEntryBB.splice(NewEntryBB.begin(), EntryBB, EntryBB->begin(), EntryBB->end());
  MF->remove(EntryBB); MF->deleteMachineBasicBlock(EntryBB);
```

四个值得记住的设计点：

1. **MBB 先按 IR 顺序建好**，保证机器码块布局与源码一致（`to preserve the layout`）。
2. **常量与形参先放进临时 EntryBB**，最后合并进 IR 入口块，注释说"我们希望基本块是极大的"（`We want the basic block to be maximal`）。
3. **指令按 RPO 翻译**，注释是 `Need to visit defs before uses when translating instructions`——与 InstructionSelect 的 uses-before-defs 正好相反。
4. **PHI 先占位、后回填**，因为翻译 PHI 时前驱块的 vreg 可能还不存在。

### 3.4.1 两种遍历顺序的对照

| 阶段 | 顺序 | 原因 |
| --- | --- | --- |
| IRTranslator | RPO（前驱/定义在前） | 翻译使用者时需要定义已存在 |
| InstructionSelect | 后序块 + 块内逆序（使用在前） | 折叠生产者、即时 DCE |

这个对照能解释很多"为什么这里能折、那里不能折"的问题。

---

## 3.5 具体 lowering 规则（全部实跑）

### 3.5.1 二元运算

`add i64` → `G_ADD`，`fadd float` → `G_FADD`，`icmp ult` → `G_ICMP intpred(ult)`，`zext i1` → `G_ZEXT`。谓词以 `intpred(...)` / `floatpred(...)` 的形式作为操作数出现。

```yaml
%2:_(i1) = G_ICMP intpred(ult), %0(i32), %1(i32)
%3:_(i32) = G_ZEXT %2(i1)
```

### 3.5.2 常量进入口块

```llvm
define i64 @add1(i64 %a) {
  %r = add i64 %a, 1
  ret i64 %r
}
```

```yaml
%0:_(i64) = COPY $x0
%1:_(i64) = G_CONSTANT i64 1
%2:_(i64) = G_ADD %0, %1
```

常量被物化在入口块，即使第一次使用在循环深处。这样做的收益是：InstructionSelect 可以把常量折进立即数，同时昂贵的常量只materialize 一次；代价是长活跃区间。LLVM 提供 `Localizer` pass 在 -O0 下把常量搬到使用点附近。

### 3.5.3 内存操作

```llvm
@g = global i32 7
define i32 @mem(i32 %x, ptr %p) {
  %a = alloca i32, align 4
  store i32 %x, ptr %a
  %v = load i32, ptr %p
  ...
}
```

```yaml
%0:_(i32) = COPY $w0
%1:_(p0) = COPY $x1
%7:_(p0) = G_GLOBAL_VALUE @g
%2:_(p0) = G_FRAME_INDEX %stack.0.a
G_STORE %0(i32), %2(p0) :: (store (i32) into %ir.a)
%3:_(i32) = G_LOAD %1(p0) :: (load (i32) from %ir.p)
%6:_(i32) = G_LOAD %7(p0) :: (load (i32) from @g)
```

- `alloca` → `G_FRAME_INDEX`，同时登记 `%stack.0.a`；
- 全局变量地址 → `G_GLOBAL_VALUE`；
- `load`/`store` → `G_LOAD`/`G_STORE`，并保留内存操作数 metadata（`:: (load (i32) from ...)`）。

这些 metadata 是后续别名分析和 load/store 优化的依据，手写 MIR 时不要随手删。

### 3.5.4 GEP

`getelementptr i32, ptr %p, i64 %idx` 被展开成"常量 + 乘法 + 指针加法"：

```yaml
%13:_(i64) = G_CONSTANT i64 4
%14:_(i64) = G_MUL %4, %13
%15:_(p0) = G_PTR_ADD %3, %14(i64)
%16:_(p0) = COPY %15(p0)
```

元素大小 4 被显式乘进去，说明**gMIR 里没有"带类型的地址运算"**，所有地址算术都是字节级的。最后的 `COPY` 是因为 IR 的 `%q` 有自己的 vreg，而 `G_PTR_ADD` 直接写进了临时 vreg。

### 3.5.5 向量与 select

```llvm
%sel = select i1 %c, <4 x i32> %v, <4 x i32> zeroinitializer
%e = extractelement <4 x i32> %sel, i32 2
%ins = insertelement <4 x i32> %sel, i32 %e, i32 1
```

```yaml
%11:_(<4 x i32>) = G_BUILD_VECTOR %12(i32), %12(i32), %12(i32), %12(i32)
%10:_(<4 x i32>) = G_SELECT %0(i1), %2, %11
%20:_(i32) = G_EXTRACT_VECTOR_ELT %10(<4 x i32>), %21(i64)
%22:_(<4 x i32>) = G_INSERT_VECTOR_ELT %10, %20(i32), %23(i64)
```

`zeroinitializer` 变成 `G_BUILD_VECTOR`（四个相同的 0），而不是"清零"指令。向量索引被提升成 `i64` 常量。

### 3.5.6 undef

`undef` 出现在操作数位置时变成 `G_IMPLICIT_DEF`：

```yaml
%17:_(i32) = G_IMPLICIT_DEF
%19:_(i32) = G_ADD %17, %18
```

它告诉后端"这个值随便"，后续可能被折叠成任意值，也可能强制物化。

### 3.5.7 i1 参数：ABI 的痕迹

一个 `i1` 形参在 AArch64 上的翻译是：

```yaml
%6:_(i32) = COPY $w0
%5:_(i8) = G_TRUNC %6(i32)
%7:_(i8) = G_ASSERT_ZEXT %5, 1
%0:_(i1) = G_TRUNC %7(i8)
```

三步：从 32 位物理寄存器取、截到 8 位、告诉优化器"第 0 位以上是零"、再截到 1 位。

`G_ASSERT_ZEXT` 是 CallLowering 按 ABI 规则插入的优化提示，正是第 2 章 `selectInstr` 会删除的那类指令。看到它就知道：gMIR 里的"多余"指令往往有 ABI 原因，不要急着当成冗余。

### 3.5.8 控制流

`switch` 在 -O0 下被降成比较链，并**新增基本块**：

```yaml
bb.2.one:
  ADJCALLSTACKDOWN 0, 0, implicit-def $sp, implicit $sp
  $w0 = COPY %1(i32)
  BL @callee, csr_aarch64_aapcs, implicit-def $lr, implicit $sp, ...
  ADJCALLSTACKUP 0, 0, implicit-def $sp, implicit $sp
  %5:_(i32) = COPY $w0
  $w0 = COPY %5(i32)
  RET_ReallyLR implicit $w0
```

这印证了第 2 章那条限制：**IRTranslator 可以建块，InstructionSelect 不行**。

同时注意调用序列：`ADJCALLSTACKDOWN`、`BL`、`ADJCALLSTACKUP` 都是 target-specific 指令，在 gMIR 阶段就已经出现。所以"gMIR 里全是 G_*"是错误印象。

### 3.5.9 PHI

```llvm
loop:
  %i = phi i64 [ 0, %entry ], [ %next, %loop ]
  %acc = phi i64 [ 0, %entry ], [ %add, %loop ]
```

```yaml
bb.2.loop:
  %1:_(i64) = G_PHI %7(i64), %bb.1, %5(i64), %bb.2
  %2:_(i64) = G_PHI %7(i64), %bb.1, %3(i64), %bb.2
```

每个 PHI 节点一个 vreg，操作数成对出现（值, 前驱块）。源码里 PHI 是**先建占位指令、等所有 MBB 建好后回填**的：

```cpp
// List of stubbed PHI instructions, for values and basic blocks to be filled
// in once all MachineBasicBlocks have been created.
SmallVector<std::pair<const PHINode *, SmallVector<MachineInstr *, 1>>, 4>
    PendingPHIs;
```

`finishPendingPhis()` 遍历每个 PHI 的入边，用 `getMachinePredBBs()` 把 IR 前驱映射到机器前驱。这个映射在 switch 降级把一块拆成多块时尤其重要：一个 IR 前驱可能对应多个机器前驱。

---

## 3.6 同一份 IR，不同目标

同一个 `cmp.ll`，AArch64：

```yaml
%0:_(i32) = COPY $w0
%1:_(i32) = COPY $w1
%2:_(i1) = G_ICMP intpred(ult), %0(i32), %1(i32)
%3:_(i32) = G_ZEXT %2(i1)
$w0 = COPY %3(i32)
RET_ReallyLR implicit $w0
```

RISC-V：

```yaml
%2:_(s64) = COPY $x10
%0:_(s32) = G_TRUNC %2(s64)
%3:_(s64) = COPY $x11
%1:_(s32) = G_TRUNC %3(s64)
%4:_(s1) = G_ICMP intpred(ult), %0(s32), %1(s32)
%5:_(s32) = G_ZEXT %4(s1)
%6:_(s64) = G_ANYEXT %5(s32)
$x10 = COPY %6(s64)
PseudoRET implicit $x10
```

核心操作完全一致（`G_ICMP` + `G_ZEXT`），差异全在 ABI 脚手架：RISC-V 的 32 位参数放在 64 位寄存器里，需要 `G_TRUNC`；返回值要 `G_ANYEXT` 回 64 位。

**结论**：gMIR 的"目标无关"指的是*操作语义*，不包括调用约定。调试时看到奇怪的 COPY/TRUNC，先怀疑 ABI，而不是选择器。

---

## 3.7 为什么读 gMIR 是调试第一步

选择失败的排查顺序应该是：

```text
1. -stop-after=irtranslator   → gMIR 是否已经错了？
2. -stop-after=legalizer      → 是否有指令没被合法化？
3. -stop-after=regbankselect  → bank 是否合理？
4. -stop-after=instruction-select / -global-isel-abort=1 → 哪条选不出来？
```

常见的"其实错在翻译阶段"：

- 前端生成了后端不支持的 IR（如特殊 intrinsic、必须走 libcall 的大整数运算）；
- ABI lowering 产生了目标不支持的参数类型（大结构体按值传递）；
- PHI 与 switch 降级组合出复杂前驱关系，导致 `G_PHI` 的前驱不匹配。

如果第 1 步的 gMIR 已经不对，后面三步都是在错误输入上找原因。

---

## 3.8 翻译失败会怎样

`reportTranslationError` 的第一件事与选择失败相同：

```cpp
static void reportTranslationError(MachineFunction &MF, ...) {
  MF.getProperties().setFailedISel();
  bool IsGlobalISelAbortEnabled =
      MF.getTarget().Options.GlobalISelAbort == GlobalISelAbortMode::Enable;
  ...
}
```

三类失败源：

| 失败源 | 触发条件 | 报错文本 |
| --- | --- | --- |
| ABI 不支持 | `CLI->fallBackToDAGISel(*MF)` 为真 | `unable to lower function: ...` |
| 大端 | 目标未 `enableBigEndian()` 且数据布局是大端 | `unable to translate in big endian mode` |
| 指令无法翻译 | `translate(Inst)` 返回 false | `unable to translate instruction: ...` |

失败后同样由 `-global-isel-abort` 决定是静默回退还是报错。

一个反直觉的例子：`-mattr=+strict-align` 下，IRTranslator **完全成功**，gMIR 与平常一模一样；失败发生在 InstructionSelect。这说明"翻译通过"不代表"能选出来"，两件事必须分开验证。

---

## 进阶延伸

- 读 `IRTranslator` 对 `invoke`/`landingpad`、inline asm、byval 参数的处理，理解“翻译失败”与“选择失败”的边界。
- 对比 AArch64 与 RISC-V 的调用约定 lowering，观察同一个 IR 如何产生不同的 `COPY`/`PseudoCALL`。
- 思考：为什么 PHI 必须延迟回填，而普通二元运算可以按 RPO 立即翻译？
## 踩雷清单

1. **以为 gMIR 全是 `G_*`**。调用、返回、栈调整在翻译阶段就是 target 指令（`BL`、`ADJCALLSTACKDOWN`、`RET_ReallyLR`）。
2. **从 LLT 推断符号或浮点**。`s32` 既可能是 `int` 也可能是 `float`，只能看使用它的指令。
3. **把常量当成"没有 vreg"**。常量占一个 vreg，有活跃区间，会影响 -O0 的 spill。
4. **以为 vreg 编号等于 IR 槽位**。编号按创建顺序分配，常量会插进来，出现"空洞"是正常的。
5. **以为 PHI 翻译是即时的**。PHI 先占位，`finishPendingPhis()` 在所有块建好后回填；前驱映射可能是一对多。
6. **在翻译阶段就怀疑选择器**。先用 `-stop-after=irtranslator` 确认输入是对的。

---

## 动手练习

### 练习 3.1 逐行对照

写一个包含 `add`、`mul`、`icmp`、`select`、`br` 的函数，导出 `-stop-after=irtranslator` 的 MIR，为每条 `G_*` 标注对应的 IR 指令。注意哪些 IR 值没有独立 vreg，哪些 IR 值对应多个 vreg。

### 练习 3.2 常量的位置

写一个在循环体内使用 `3.14` 的函数，确认 `G_FCONSTANT` 是否出现在入口块，并解释为什么。再用 `-O1` 观察是否不同。

### 练习 3.3 ABI 脚手架

把同一个 `i32 @f(i32)` 分别用 `-mtriple=aarch64-linux-gnu` 和 `-mtriple=riscv64-linux-gnu` 翻译，列出所有 COPY/TRUNC/ANYEXT，并对照两者的调用约定解释差异。

### 练习 3.4 PHI 与多前驱

构造一个 `switch` 后接 `phi` 的函数，观察 `G_PHI` 的操作数个数与 IR 的入边个数是否一致。如果不一致，用 `getMachinePredBBs` 的逻辑解释原因。

### 练习 3.5 失败边界

用 `-mattr=+strict-align` 对比 `-stop-after=irtranslator` 与 `-stop-after=instruction-select` 的结果，说明失败发生在哪个阶段、为什么。

---

## 重点整理

- gMIR 与 MIR 共用数据结构，但约束更松；流水线逐步收紧直到变成 MIR。
- 三条规则：`G_*` 操作码、vreg 从 LLT 起步、立即数与物理寄存器都经 vreg。
- LLT 只有 `sN`/`pN`/`<N x sM>`，不区分符号与浮点。
- IRTranslator 按 IR 顺序建块、按 RPO 翻译指令（定义先于使用），最后回填 PHI、合并入口块。
- 常量物化在入口块；alloca/global/load/store/gep 各有固定映射。
- ABI lowering 会产生 target 指令与 `G_ASSERT_ZEXT` 之类的提示，gMIR 不是纯 generic。
- 翻译失败与选择失败是两条独立路径，都设 `failedISel`。

---

## 自我检核

1. `-stop-after=irtranslator` 的输出里，`legalized` / `regBankSelected` / `selected` 各是什么？为什么？
2. generic vreg 的约束按什么顺序收紧？每个阶段加上什么信息？
3. LLT 为什么不区分有符号/无符号和整数/浮点？读 MIR 时如何判断一个 `s32` 是浮点？
4. 为什么常量要放在入口块？代价是什么？LLVM 用什么 pass 缓解？
5. IRTranslator 的遍历顺序与 InstructionSelect 有什么不同？各自的原因是什么？
6. 为什么 gMIR 里会出现 `BL` 和 `RET_ReallyLR`？这与"目标无关"矛盾吗？
7. PHI 为什么要延迟回填？switch 降级如何影响前驱映射？

---

## 延伸阅读

- `llvm/docs/GlobalISel/GMIR.rst`：gMIR 的权威说明，含 LLT 对照表与"立即数必须经 vreg"的规则。
- `llvm/docs/GlobalISel/IRTranslator.rst`：翻译策略、常量、调用约定 lowering 的概览。
- `llvm/lib/CodeGen/GlobalISel/IRTranslator.cpp`：`runOnMachineFunction` 的建块/RPO/入口块合并逻辑，`finishPendingPhis` 的回填算法。
- `llvm/include/llvm/CodeGen/GlobalISel/IRTranslator.h`：`PendingPHIs`、`translate()` 家族与 `ValueToVRegInfo`。
- `llvm/include/llvm/CodeGen/GlobalISel/MachineIRBuilder.h`：`buildConstant`、`buildUndef`、`buildGlobalValue`、`buildBrCond` 等构造入口。
- `llvm/include/llvm/CodeGen/GlobalISel/GenericMachineInstrs.h`：`G_*` 指令的类型化封装（`GPhi`、`GLoad` 等），写 C++ 选择器时会大量使用。
- `llvm/include/llvm/CodeGen/LowLevelType.h`：LLT 的完整 API。
- 官方文档《Generic MIR》：<https://llvm.org/docs/GlobalISel/GMIR.html> —— 查 `G_*` 操作数约定、LLT 与 PHI 表示；读 gMIR 时的字典。

---

## 下一章

第 4 章进入 **Legalizer 与 RegBankSelect**：看 gMIR 如何从"目标无关但可能不合法"变成"每条指令都合法、每个 vreg 都有 bank"，以及这两个阶段如何决定 InstructionSelect 能拿到什么样的输入。

[第 4 章 Legalizer 与 RegBankSelect](./04-legalizer-regbankselect.md)
