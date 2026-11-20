# Ch 15 — SCEV、AliasAnalysis 与 MemorySSA：三种「循环与内存」推理

> **目标**：把三个经常被混为一谈的分析拆开：SCEV 回答「这个循环变量第 k 次迭代是什么」，AliasAnalysis 回答「这两次内存访问会不会碰到同一块字节」，MemorySSA 回答「这条 load 之前，哪一次写到达了这里」。学完你能在 diff 里指出某个 `nsw`/TBAA/`inbounds` 依赖了哪条假设，以及 MemorySSA 的 walker 为什么能跳过不相关的 store。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`（`RelWithDebInfo` + assertions ON）。需要注意：该构建的 CMake 源树是 `/home/ubuntu/llvm/trees/pr-lane/llvm` @ `a5eaf194d8b6`，**不包含** 本章要讲的 `9ac5159b3c46`；涉及该 commit 的修复后行为会单独标「未实测」。其余 `opt` 输出都来自上面的构建。

## 为什么需要这个？

一个中端 pass 想删掉一次 store，通常要同时过三关：

- 它要知道循环会执行多少次，才能判断这次 store 是不是最后一次、能不能提出循环（SCEV）。
- 它要知道这次 store 和后面的 load 是否可能访问同一块字节（AliasAnalysis）。
- 它要知道从 store 到 load 之间，是否还有别的写把这次 store 覆盖掉（MemorySSA）。

这三关的「答案」都带前提。SCEV 的表达式可能只在循环不溢出时成立；AA 的 `NoAlias` 可能只在程序没有 UB 时成立；MemorySSA 的 def-use 链则依赖 AA 和 DominatorTree 的正确性。Reviewer 看到的 diff 往往只改了一行 flag 或一个 metadata，但那一行可能把整个推理的前提改掉。

```
        ┌──────────────────────────────┐
        │ SCEV：循环的代数形式          │
        │ {start,+,step}<nsw><%loop>   │
        └──────────────┬───────────────┘
                       │ 迭代次数 / 退出值
                       ▼
        ┌──────────────────────────────┐
        │ AA：两次访问是否可能重叠      │
        │ NoAlias / MayAlias / ...     │
        └──────────────┬───────────────┘
                       │ 逐对消歧
                       ▼
        ┌──────────────────────────────┐
        │ MemorySSA：内存的 SSA 版本    │
        │ MemoryDef / MemoryUse / Phi  │
        └──────────────────────────────┘
```

## 15.1 三个分析，三种不变量

| 分析 | 输入 | 输出 | 依赖的假设 | 假设被破坏时 |
| --- | --- | --- | --- | --- |
| ScalarEvolution | 循环、`Value` | `SCEV*` 表达式、trip count | 循环结构、`nsw`/`nuw`、支配关系 | trip count 算错，循环被删/展开错 |
| AliasAnalysis | 两个 `MemoryLocation` | `AliasResult` | 无 UB、TBAA、`noalias`、`inbounds` | 把会重叠的访问判成 `NoAlias` |
| MemorySSA | 函数/循环 + AA + DT | 内存 def-use 链 | AA 与 DT 正确、CFG 未变 | 走到错误的 clobber，DSE/GVN 出错 |

先记住一句话：**SCEV 描述的是「值随迭代变化的代数式」，AA 描述的是「两次访问的关系」，MemorySSA 描述的是「内存状态的版本链」。** 它们不是替代关系；MemorySSA 的 walker 在消歧时仍然调用 AA，而 AA 的查询又可能用到 SCEV（`SCEVAA`）。

## 15.2 SCEV：把循环变量变成表达式

`ScalarEvolution` 的核心是把一个 `Value` 映射到一个 `SCEV` 节点。入口是 `getSCEV`（声明 `ScalarEvolution.h:738`，实现 `ScalarEvolution.cpp:4419`）；循环退出次数由 `getBackedgeTakenCount` 给出（声明 `ScalarEvolution.h:1101`，实现 `ScalarEvolution.cpp:8393`）。

四个必须认识的节点类型（`ScalarEvolutionExpressions.h`）：

| 类型 | 定义位置 | 含义 | 例子 |
| --- | --- | --- | --- |
| `SCEVConstant` | `:61` | 编译期常量，在所有点都定义 | `42` |
| `SCEVAddRecExpr` | `:328` | 仿射递归 `{start,+,step}<flags><loop>` | `{0,+,1}<nuw><nsw><%loop>` |
| `SCEVUnknown` | `:562` | 分析看不穿的值（参数、load、不透明表达式） | `%n` |
| `SCEVUDivExpr` | `:293` | 无符号除法 | `(a /u b)` |

`SCEV` 基类在 `ScalarEvolution.h:277`；`SCEVTypes` 枚举在 `ScalarEvolutionExpressions.h:38`。还有一个容易忽略的节点：`SCEVSequentialMinMaxExpr`（`ScalarEvolutionExpressions.h:520`），表示 `umin_seq`/`smin_seq` 这类短路 min/max，15.3 会用到。

先确认 printer 的真实名字：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/opt --print-passes | rg 'scalar-evolution|aa-eval|memoryssa'
```

实测（截取）：

```text
  aa-eval
  print<memoryssa-walker>
  print<memoryssa><no-ensure-optimized-uses>
  print<scalar-evolution>
  verify<scalar-evolution>
```

准备一段带循环的 IR（存为 `sum.ll`）：

```llvm
define i32 @sum(i32 %n) {
entry:
  br label %loop
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %acc = phi i32 [ 0, %entry ], [ %acc.next, %loop ]
  %acc.next = add i32 %acc, %i
  %i.next = add nsw i32 %i, 1
  %cmp = icmp slt i32 %i.next, %n
  br i1 %cmp, label %loop, label %exit
exit:
  ret i32 %acc.next
}
```

```bash
$B/opt -passes='print<scalar-evolution>' -disable-output sum.ll
```

实测（保留 `%i` 与 trip count，`%acc` 的递推已省去）：

```text
Classifying expressions for: @sum
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  -->  {0,+,1}<nuw><nsw><%loop> U: [0,2147483647) S: [0,2147483647)		Exits: (-1 + (1 smax %n))<nsw>		LoopDispositions: { %loop: Computable }
  %i.next = add nsw i32 %i, 1
  -->  {1,+,1}<nuw><nsw><%loop> U: [1,-2147483648) S: [1,-2147483648)		Exits: (1 smax %n)		LoopDispositions: { %loop: Computable }
Determining loop execution counts for: @sum
Loop %loop: backedge-taken count is (-1 + (1 smax %n))<nsw>
Loop %loop: constant max backedge-taken count is i32 2147483646
Loop %loop: symbolic max backedge-taken count is (-1 + (1 smax %n))<nsw>
Loop %loop: Trip multiple is 1
```

逐项读：

- `{0,+,1}<nuw><nsw><%loop>`：从 0 开始，每次加 1；`nuw`/`nsw` 表示不无符号回绕、不有符号回绕。
- `U: [0,2147483647)` 是无符号范围，左闭右开；`S:` 是符号范围。它是「在 SCEV 假设下」的推断，不是运行时保证。
- `Exits: (-1 + (1 smax %n))<nsw>`：退出值是 `smax(n,1)-1`；`n=0` 时循环仍先进入一次，所以是 0。
- `constant max ... 2147483646` 是常量上界，不是精确 trip count；精确值看 `backedge-taken count`。

`getBackedgeTakenCount` 失败时返回 `SCEVCouldNotCompute`，调用方必须用 `isa<SCEVCouldNotCompute>` 检查。多出口循环的精确 trip count 可能不可计算，只能给 max。

`SCEVUDivExpr` 有一个别处没有的性质：它是目前唯一可能 trap 的 SCEV（`ScalarEvolution.h:103-109` 的注释说「currently the only expression type which can trap」）。看到 SCEV 把除法 hoist 到循环外时，要问「这个除法在原位置是否一定执行」。

## 15.3 `SCEVUse` 与 use flags：同一个 SCEV，不同位置不同前提

`9ac5159b3c46` 只改了 11 行，但改的是一个很容易被误读的边界。`SCEVUseT`（`ScalarEvolution.h:124`）把指针和 2 bit 的 use flags 打包在 `PointerIntPair` 里：

- `hasUseFlags()`：`:149`，判断这个 use 是否带 use-specific flags。
- `isCanonical()`：`:146`，判断「这个 use 及其操作数都没有 use flags」。
- `using SCEVUse = SCEVUseT<const SCEV *>`：`:200`。

头文件 `:93-102` 规定：SCEV 节点上的 flag 必须在该 SCEV 的**整个定义域**成立；`SCEVAddRec` 从循环头开始定义，`SCEVUnknown` 从 `Value` 的定义点开始。**节点 flag 是全局承诺，`SCEVUse` flag 只在 use 点成立。** 把 use flags 当成节点 flags 往上提，就会把「循环内不溢出」搬到 preheader。

`impliesPoison(AssumedPoison, S)` 的职责是：假设 `AssumedPoison` 已经是 poison，判断 `S` 是否也一定是 poison。它被用来决定能不能把 `umin_seq` 折成 `umin`（`ScalarEvolution.cpp:4179-4189`）：

```cpp
// We can replace %x umin_seq %y with %x umin %y if either:
//  * %y being poison implies %x is also poison.
//  * %x cannot be the saturating value (e.g. zero for umin).
if (::impliesPoison(Ops[i], Ops[i - 1]) || ...)
```

`umin_seq` 的语义是「先看第一个操作数；第一个是 0 就直接得 0，否则再算第二个」。所以第二个操作数的 poison 只在第一个非 0 时才可能传播；改成普通 `umin` 等于假设「y 的 poison 一定被 x 接住」。不成立时，preheader 里展开的 exit count 会变成 poison，而原程序在「先走另一个出口」时不会碰到它。

`9ac5159b3c46` 的修复（`ScalarEvolution.cpp:4009-4013`）：

```cpp
static bool impliesPoison(SCEVUse AssumedPoison, const SCEV *S) {
  // AssumedPoison may be poison due to its use flags alone.
  if (!AssumedPoison.isCanonical())
    return false;
  ...
}
```

`SCEVUse` 参数替换了原来的 `const SCEV *`，函数能看见 use flags；非 canonical 就保守返回 false，保留 `umin_seq`。同一段上方的注释（`:3981-3986`）补了一句：「The flags of a SCEVUse can, as they only hold where it is used.」

测试 `exit-value-nowrap-flags.ll:385` 的期望是：

```text
Exits: (%n umin_seq (-1 + (1 umax (100 umin (9 + %start)<u nuw>)))<nsw>)
```

本机 `pr-lane` 构建（源树 `a5eaf194d8b6`，早于该 commit）跑同一函数，得到（截取该行）：

```text
Exits: ((-1 + (1 umax (100 umin (9 + %start)<u nuw>)))<nsw> umin %n)
```

```bash
$B/opt -passes='print<scalar-evolution>' -disable-output \
  /home/ubuntu/llvm/llvm-project/llvm/test/Analysis/ScalarEvolution/exit-value-nowrap-flags.ll
```

这不是测试写错，而是**构建停在修复前**：旧代码把 `umin_seq` 折成了 `umin`。修复后的 `umin_seq` 输出需要包含 `9ac5159b3c46` 的构建才能实测；本机没有这样的构建，这一段标「未实测」。源码证据（diff + 测试 CHECK + 当前 `impliesPoison` 实现）足以说明修复逻辑。

> 横向链接：poison 的传播与 `select` 的例外见 [Ch 30 — 正确性 review 二](./30-correctness-review-poison-ub.md)；本节的特殊点是「同一个 SCEV 在不同 use 点有不同 flag」。

## 15.4 AliasAnalysis：AA 回答的是「可能」，不是「事实」

AA 的接口在 `llvm/include/llvm/Analysis/AliasAnalysis.h`：

- `AliasResult::Kind` 在 `:92`，四个值：`NoAlias`、`MayAlias`、`PartialAlias`、`MustAlias`；`NoAlias = 0`，布尔上下文里只有它转 false。
- 头文件 `:28-33`：**`NoAlias` 不蕴含指针不相等**。两个指向常量内存的相等指针可以既是 `NoAlias` 又是 `MustAlias`；API 只能返回一个结果。
- `AAResults` 在 `:339`；`AAManager` 在 `:981`，负责把注册的 analysis 结果加进 `AAResults`。

默认 pipeline 里的三个组件：

| 组件 | 声明位置 | 依赖的假设 |
| --- | --- | --- |
| `BasicAA` | `BasicAliasAnalysis.h:179`（结果 `:44`） | 对象边界、`inbounds`、`dereferenceable`、捕获分析 |
| `TypeBasedAA` | `TypeBasedAliasAnalysis.h:79`（结果 `:34`） | `!tbaa` 类型标签；不同类型不别名 |
| `ScopedNoAliasAA` | `ScopedNoAliasAA.h:77`（结果 `:33`） | `!noalias` / `!alias.scope` 的作用域 |

`MemoryLocation`（`MemoryLocation.h:217`）是 `{Ptr, Size, AATags}`，不是「一个指针」：`MemoryLocation::get(const LoadInst*)` 在 `:244`，`getBeforeOrAfter` 在 `:285`。**AA 查询必须带 size**；指针相等但访问宽度不同，结果可能是 `PartialAlias`。`AliasAnalysis.md` 的 `char C[2]` 例子说明：1 字节 store 与 2 字节 store 是否别名，取决于 size。

```llvm
define i32 @alias(ptr %p, ptr %q) {
entry:
  store i32 1, ptr %p, align 4
  %v = load i32, ptr %q, align 4
  ret i32 %v
}

define i32 @inbounds_oob(ptr %p) {
entry:
  %q = getelementptr inbounds [2 x i32], ptr %p, i64 0, i64 1
  store i64 0, ptr %p, align 4
  %v = load i32, ptr %q, align 4
  ret i32 %v
}
```

```bash
$B/opt -passes='aa-eval' -disable-output alias.ll
```

实测（省去 mod/ref 汇总）：

```text
===== Alias Analysis Evaluator Report =====
  2 Total Alias Queries Performed
  0 no alias responses (0.0%)
  1 may alias responses (50.0%)
  1 partial alias responses (50.0%)
  0 must alias responses (0.0%)
  Alias Analysis Evaluator Pointer Alias Summary: 0%/50%/50%/0%
```

`%p`/`%q` 是函数参数，AA 没有信息证明不别名，所以 `MayAlias`。第二个函数里 `%q = p+4`，i64 store 覆盖 8 字节，load 覆盖其中 4 字节，所以是 `PartialAlias`。`inbounds` 在这里没有让 AA 直接给出 `NoAlias`：它只保证 GEP 结果在对象内（或 one-past），不保证两个访问不重叠。

TBAA 则会把「类型不同」变成「不别名」：

```llvm
define i32 @tbaa(ptr %p, ptr %q) {
entry:
  store i32 1, ptr %p, align 4, !tbaa !0
  store float 0.0, ptr %q, align 4, !tbaa !1
  %v = load i32, ptr %p, align 4, !tbaa !0
  ret i32 %v
}

define i32 @notbaa(ptr %p, ptr %q) {
entry:
  store i32 1, ptr %p, align 4
  store float 0.0, ptr %q, align 4
  %v = load i32, ptr %p, align 4
  ret i32 %v
}

!0 = !{!2, !2, i64 0, i64 4}
!1 = !{!3, !3, i64 0, i64 4}
!2 = !{!4, i64 4, !"int"}
!3 = !{!4, i64 4, !"float"}
!4 = !{!"root"}
```

```bash
$B/opt -passes='aa-eval' -evaluate-aa-metadata \
  -print-no-aliases -print-may-aliases -disable-output tbaa.ll
```

实测（截取；标签打印时会重新编号）：

```text
Function: tbaa: 2 pointers, 0 call sites
  MayAlias:	i32* %p, float* %q
  NoAlias:   %v = load i32, ptr %p, align 4, !tbaa !3 <->   store float 0.000000e+00, ptr %q, align 4, !tbaa !5
  NoAlias:   store float 0.000000e+00, ptr %q, align 4, !tbaa !5 <->   store i32 1, ptr %p, align 4, !tbaa !3
Function: notbaa: 2 pointers, 0 call sites
  MayAlias:	i32* %p, float* %q
  MayAlias:   %v = load i32, ptr %p, align 4 <->   store float 0.000000e+00, ptr %q, align 4
```

指针级查询仍是 `MayAlias`，因为标签在指令上；带标签的 load/store 对才变成 `NoAlias`。`gvn` 随即把 load 折叠：

```bash
$B/opt -passes='gvn' -S tbaa.ll
```

实测（只保留带 TBAA 的函数；`notbaa` 的 load 未被折叠）：

```llvm
define i32 @tbaa(ptr %p, ptr %q) {
entry:
  store i32 1, ptr %p, align 4, !tbaa !0
  store float 0.000000e+00, ptr %q, align 4, !tbaa !3
  ret i32 1
}
```

如果 `%p == %q`，这条推理就错了——但那是**程序违反了 TBAA 契约**，IR 语义把这种程序当成 UB。AA 的 `NoAlias` 是「IR 合法且没有 UB」前提下的结论，不是对任意机器码的保证。

> 横向链接：TBAA 元数据的结构与 verifier 检查见 [Ch 8](./08-metadata-and-debuginfo.md)；`inbounds` 的语义见 [Ch 30](./30-correctness-review-poison-ub.md) 的 GEP 一节。

## 15.5 MemorySSA：把内存状态变成 SSA

传统 AA 是逐对查询：一个 load 要问前面每一个 store 是否别名，循环里容易退化成平方级。MemorySSA 给「整个内存」建立 SSA 版本：

- `MemoryDef`：可能写内存的指令（store、非 readonly call、fence 等），每个 def 引入新版本并指向旧版本。
- `MemoryUse`：只读内存的指令（load、readonly call）。
- `MemoryPhi`：在控制流汇合点合并多个可能到达的 def。

源码锚点（`MemorySSA.h`）：

| 符号 | 位置 | 说明 |
| --- | --- | --- |
| `MemoryAccess` | `:142` | 三种 access 的基类 |
| `MemoryUse` | `:312` | 只读访问 |
| `MemoryDef` | `:369` | 写访问；def 也带一个 use，指向最近到达的 def |
| `MemoryPhi` | `:473` | 基本块入口的版本合并 |
| `MemorySSA` | `:689` | 分析对象；构造需要 `Function/Loop + AA + DT` |
| `getWalker()` | `:700` | 取得默认 walker |
| `MemorySSAWalker` | `:1020` | 回答「最近的 clobber 是谁」 |
| `getClobberingMemoryAccess` | `:1049` | walker 的主查询 |

构造/构建在 `MemorySSA.cpp:1211`（Function）、`:1230`（Loop）、`:1508`（`buildMemorySSA`）。`MemorySSA.md` 说它的目标是替代 `MemoryDependenceAnalysis`，因为后者「unless you're very careful」会导致平方级算法；MemorySSA 提供 def-use/use-def 链，让「快速判断某件事一定不会发生」变便宜。

```llvm
define i32 @sum(ptr %a, i32 %n) {
entry:
  br label %loop
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %p = getelementptr inbounds i32, ptr %a, i32 %i
  %v = load i32, ptr %p, align 4
  store i32 %v, ptr %p, align 4
  %i.next = add nuw nsw i32 %i, 1
  %cmp = icmp slt i32 %i.next, %n
  br i1 %cmp, label %loop, label %exit
exit:
  ret i32 %v
}
```

```bash
$B/opt -passes='print<memoryssa>' -disable-output memloop.ll
```

实测（省去与内存无关的 `%i.next`/`%cmp`）：

```text
MemorySSA for function: sum
loop:                                             ; preds = %loop, %entry
; 2 = MemoryPhi({entry,liveOnEntry},{loop,1})
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %p = getelementptr inbounds i32, ptr %a, i32 %i
; MemoryUse(2)
  %v = load i32, ptr %p, align 4
; 1 = MemoryDef(2)
  store i32 %v, ptr %p, align 4
```

读法：进入 `loop` 时，内存版本可能来自 entry（`liveOnEntry`），也可能来自上一轮的 `1` 号 def；load 读 `2` 号版本；store 基于 `2` 号版本产生 `1` 号新版本。

```bash
$B/opt -passes='print<memoryssa-walker>' -disable-output memloop.ll
```

实测（截取）：

```text
MemorySSA (walker) for function: sum
; MemoryUse(2) - clobbered by 2 = MemoryPhi({entry,liveOnEntry},{loop,1})
  %v = load i32, ptr %p, align 4
; 1 = MemoryDef(2)->2 - clobbered by 2 = MemoryPhi({entry,liveOnEntry},{loop,1})
  store i32 %v, ptr %p, align 4
```

walker 不直接说「前面每条 store 都可能 clobber」，而是沿 def 链跳过 AA 证明不别名的那些，返回最近的、确实可能 clobber 的 access。`MemorySSAWalker` 的注释（`MemorySSA.h:1027-1030`）说它会「by skipping any def which AA can prove does not alias the location(s) accessed by the instruction」。

| 维度 | 传统 AA | MemorySSA |
| --- | --- | --- |
| 查询单位 | 两个 `MemoryLocation` | 一个 access 的 def 链 |
| 结果 | `AliasResult` | 最近的 clobber / 版本 |
| 复杂度风险 | 每个 load 扫前面所有 store，可能平方级 | 一次构建，链式 walk |
| 消歧 | 每次调用 AA | walker 内部调用 AA，但只走可能路径 |
| 更新 | 无状态 | `MemorySSAUpdater` 维护 CFG 变更 |

MemorySSA **不是** AA 的替代品：它依赖 AA 消歧、依赖 DT 构造，也有自己的失效问题（Ch 16）。看到 `MemorySSA` 被缓存后 CFG 又变了，先问 `MemorySSAUpdater` 有没有被调用。

## 15.6 失败例子：假设被违反时

### 例子一：`nsw` 加在会回绕的计数器上

```llvm
define i8 @wrap(i8 %n) {
entry:
  br label %loop
loop:
  %i = phi i8 [ 0, %entry ], [ %i.next, %loop ]
  %i.next = add nsw i8 %i, 1
  %cmp = icmp ne i8 %i.next, %n
  br i1 %cmp, label %loop, label %exit
exit:
  ret i8 %i.next
}
```

SCEV 实测：

```text
  %i = phi i8 [ 0, %entry ], [ %i.next, %loop ]
  -->  {0,+,1}<nuw><nsw><%loop> U: [0,-128) S: [0,-128)		Exits: (-1 + %n)
  %i.next = add nsw i8 %i, 1
  -->  {1,+,1}<nuw><nsw><%loop> U: [1,-128) S: [1,-128)		Exits: %n
...
Loop %loop: backedge-taken count is (-1 + %n)
```

`U: [1,-128)` 的意思是「从 1 到 127」：`nsw` 让 SCEV 相信 `i8` 加法不会从 127 跨到 -128。于是 `indvars` 直接判定循环条件恒假：

```bash
$B/opt -passes='indvars' -S wrap.ll
```

实测输出：

```llvm
define i8 @wrap(i8 %n) {
entry:
  br label %loop

loop:                                             ; preds = %loop, %entry
  br i1 false, label %loop, label %exit

exit:                                             ; preds = %loop
  ret i8 %n
}
```

如果原程序的本意是「`i8` 计数器从 0 数到 200，允许回绕」，这段 IR 已经错了：`add nsw i8` 在 127→128 时产生 poison，`br` 的条件是 poison，整条执行是 UB。LLVM 把循环删掉不是 bug，**bug 是那个 `nsw`**。Frontend 只有在源语言的算术确实不允许溢出时才能加 `nsw`；给 unsigned 计数器加 `nsw` 是把 UB 引进了一个本来 defined 的程序。

Review 时看到「删除循环 / trip count 变成常量 / 分支变成 `br i1 false`」的 diff，第一反应应该是去找 `nsw`/`nuw` 的来源，而不是怀疑 SCEV。

### 例子二：TBAA 与 type punning

15.4 的 `gvn` 输出已经展示了：带 TBAA 的版本把 load 折叠成 `1`。如果源程序用 `int*` 和 `float*` 访问同一块对象（例如 union 或 `memcpy` 之外的强制转换），C/C++ 的 TBAA 规则认为这是 UB；frontend 仍然给两次访问贴了不同的 `!tbaa` 标签，优化器就会按「不别名」处理。错误的不是 GVN 的 fold，而是**给一个会 type-pun 的访问贴了不兼容的 TBAA**。

判断方法：看 `!tbaa` 的 base type/access type 是否来自同一个对象，所有访问是否都通过兼容类型（`char`/`std::byte` 例外），以及是否有 `memcpy`、union、`reinterpret_cast` 路径绕过了标签。

## 踩雷集锦

1. **把 `U: [a,b)` 当成运行时保证**：SCEV 的范围是在循环结构、`nsw`/`nuw`、支配关系都成立时的推断；程序一旦在这些点 UB，范围就不再约束实际机器码。
2. **把 use flags 当成节点 flags**：`SCEVUse` 上的 `nsw` 只在 use 点成立；把它提升到 preheader 会把「循环内不溢出」误当成「函数入口不溢出」。`9ac5159b3c46` 就是修这个。
3. **把 `NoAlias` 当成「指针不相等」**：头文件明确说 `NoAlias` 不蕴含 inequal pointers；常量内存的相等指针可以同时是 `NoAlias` 和 `MustAlias`。
4. **AA 查询不带 size**：`MemoryLocation` 是 `{ptr, size, tags}`；只比较指针值会把 `PartialAlias` 误判成 `MustAlias`，也会让本来能证明的 `NoAlias` 退化。
5. **把 MemorySSA 当成 AA 的替代品**：MemorySSA 的 walker 仍然调用 AA；它解决的是「版本链/最近 clobber」，不是「两个指针是否别名」。
6. **用 release 构建验证 SCEV/AA 的断言**：本课的 `assert`、`-debug-only`、`verify<...>` 都依赖 assertions ON；release 构建会静默跳过。

## 动手练习

1. 把 `sum.ll` 的 `add nsw` 改成 `add nuw`，再跑 `print<scalar-evolution>`；解释 `U:`/`S:` 范围和 trip count 哪些变了，哪些没变。
2. 给 `alias.ll` 的两个参数加 `noalias`，重跑 `aa-eval`；解释为什么结果从 `MayAlias` 变成 `NoAlias`，以及这依赖调用者的什么承诺。
3. 把 `tbaa.ll` 中 `float` 的 tag 改成 `int`，重跑 `gvn`；解释为什么 load 不再被折叠。
4. 用 `-passes='print<memoryssa-walker>'` 对比 `-passes='print<memoryssa>'`，指出 walker 多做了哪一步消歧。

## 本章重点整理

- SCEV 的核心类型是 `SCEVConstant`、`SCEVAddRecExpr`、`SCEVUnknown`、`SCEVUDivExpr`；`SCEVUDivExpr` 是唯一可能 trap 的节点。
- `getBackedgeTakenCount` 可能返回 `SCEVCouldNotCompute`；`constant max` 与 `symbolic max` 是两种上界。
- `SCEVUse` 把 use flags 和节点指针打包；`isCanonical()` 为 false 时，`impliesPoison` 保守返回 false，保留 `umin_seq`。
- AA 的 `NoAlias` 依赖「程序没有 UB」；TBAA、`inbounds`、`noalias` 都是承诺，不是提示。
- `MemoryLocation` 包含 size；AA 查询不能只传指针。
- MemorySSA 用 `MemoryDef`/`MemoryUse`/`MemoryPhi` 建立内存 SSA；walker 用 AA 跳过不别名的 def，返回最近的 clobber。
- 三个分析的失效条件不同；Ch 16 会把「pass 返回什么 PA」与这些条件接起来。

## 自我检核

- [ ] 不看笔记，能画出 SCEV、AA、MemorySSA 各自回答的问题，并各举一个 review 场景
- [ ] 能解释 `{0,+,1}<nuw><nsw><%loop>` 的每个部分，以及 `U:`/`S:` 范围的前提
- [ ] 能说出 `SCEVUse` 与 `SCEV` 节点在 flag 作用域上的区别
- [ ] 能复述 `9ac5159b3c46` 为什么对 non-canonical use 返回 false，以及 `umin_seq` 的短路语义
- [ ] 能解释为什么 `NoAlias` 不蕴含指针不相等
- [ ] 能说出 TBAA 在什么情况下会把一个 defined 程序变成 UB，以及 review 时怎么发现
- [ ] 能解释 MemorySSA 的 `MemoryPhi` 为什么出现在循环头，以及 walker 如何跳过不相关 def
- [ ] 知道本机 `pr-lane` 构建不含 `9ac5159b3c46`，以及哪些结论因此标了「未实测」

## 延伸阅读

- **[LLVM Alias Analysis Infrastructure](https://llvm.org/docs/AliasAnalysis.html)**
  - **读哪里**：`Representation of Pointers`、`The alias method`、`Must, May, or No` 三节
  - **能学到什么**：size 为什么是 AA 查询的一部分；`NoAlias`/`MustAlias` 的精确语义；本章 15.4 的表格以此为准
  - **前提知识**：知道 `load`/`store`/`getelementptr` 的基本形式
- **[LLVM MemorySSA](https://llvm.org/docs/MemorySSA.html)**
  - **读哪里**：`MemorySSA Structure`、`The walker`、`Default walker APIs`
  - **能学到什么**：`MemoryDef`/`MemoryUse`/`MemoryPhi` 的版本语义；walker 如何用 AA 跳过 def；本章 15.5 的图可以直接对照文档里的例子
  - **前提知识**：SSA 的 phi 概念；读过 Ch 14 的支配树/循环分析
- **[LLVM Language Reference: Poison Values](https://llvm.org/docs/LangRef.html#poison-values)**
  - **读哪里**：整节，尤其是 `nsw` 溢出、`select` 传播、immediate UB 列表
  - **能学到什么**：为什么 SCEV 的 `nsw` 假设被破坏后，优化器可以删除整个循环；本章 15.6 的失败例子以此为依据
  - **前提知识**：读过 Ch 30 的 undef/poison/UB 三层模型
- **[Alive2: Bounded Translation Validation for LLVM](https://plmlab.server.tudelft.nl/alive2/)**
  - **读哪里**：论文的 introduction 与 `alive-tv` 使用示例；重点看它如何把「源程序 defined、目标程序 UB」判为不合法
  - **能学到什么**：review 一个 fold 时，如何区分「refinement」与「引入 UB」；和本章的 AA/SCEV 假设直接相关
  - **前提知识**：基本的 SMT/量词直觉；不要求会写 Alive 语言

→ [Ch 16 — PreservedAnalyses 与失效：pass 说「没坏」之前，先证明](./16-preserved-analyses-and-invalidation.md)
