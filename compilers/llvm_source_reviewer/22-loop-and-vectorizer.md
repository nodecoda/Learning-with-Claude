# Ch 22 — 循环与向量化：`LoopUnroll`、`LICM`、`LoopVectorize`、TTI

> **目标**：看到一个循环优化或向量化的 PR 时，能判断循环是否处于 pass 需要的规范形式、这条变换依赖哪个 analysis、为什么同一段循环换 `-mcpu` 后从「向量化」变成「不向量化」，以及 remark 里的拒绝理由属于合法性、别名还是 cost model。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`，LLVM 24.0.0git，assertions 开启。构建目录 cache 指向 `/home/ubuntu/llvm/trees/pr-lane`（`a5eaf194d8b6`），与 7c7a 不是同一个 commit；行号按 7c7a 核实，输出来自这个二进制。向量化的行为对版本和 target 都敏感，下面每个结论都标了产生它的命令与 `-mcpu`。

## 为什么循环需要一整套 pass 栈

循环优化的前提不是「找到循环」，而是「循环长成 pass 期望的样子」。同一段循环，如果没有 preheader，LICM 没有地方放 hoist 出来的指令；如果没有 LCSSA，循环外的使用者拿不到循环内的定义；如果 header 有多条 backedge，trip count 和归纳变量分析都会变复杂。

LLVM 把这些前提拆成两层：

```text
function(loop-mssa(licm))
        │
        ├─ LoopAnalysis                找到 Loop*，建立 LoopInfo
        ├─ LoopSimplifyPass            补 preheader、dedicated exits、单 backedge
        ├─ LCSSAPass                   循环出口插 phi，循环外只见 LCSSA phi
        ├─ MemorySSAAnalysis           LICM 的内存前提
        ├─ AA / DT / LI / SE / TLI / TTI
        └─ LICMPass                    对每个 Loop 跑一次
```

这不是约定，是代码。`LoopPassManager` 是 `PassManager<Loop, LoopAnalysisManager, LoopStandardAnalysisResults &, LPMUpdater &>` 的 typedef（`LoopPassManager.h:172`）；`FunctionToLoopPassAdaptor` 负责函数层到循环层的桥接，它的构造函数直接加了两条 canonicalization pass：

```cpp
// llvm/include/llvm/Transforms/Scalar/LoopPassManager.h:402-403
LoopCanonicalizationFPM.addPass(LoopSimplifyPass());
LoopCanonicalizationFPM.addPass(LCSSAPass());
```

然后在 `run` 里先跑这个小 pipeline，再构造 `LoopStandardAnalysisResults`（`LoopPassManager.cpp:193` 起）。`LoopStandardAnalysisResults` 的字段就是循环 pass 能免费拿到的分析集合（`LoopAnalysisManager.h:52-61`）：

```cpp
struct LoopStandardAnalysisResults {
  AAResults &AA;
  AssumptionCache &AC;
  DominatorTree &DT;
  LoopInfo &LI;
  ScalarEvolution &SE;
  TargetLibraryInfo &TLI;
  TargetTransformInfo &TTI;
  MemorySSA *MSSA;
};
```

`MSSA` 是裸指针，可以是 `nullptr`；这就是下一节 `loop(licm)` 直接报错的原因。

`LoopAnalysis` 本身很薄：`LoopAnalysis::run` 调 `LI.analyze` 建 `LoopInfo`（`LoopInfo.cpp:1026`）。复杂的是 `LoopSimplifyPass`（`LoopSimplify.cpp:845`）和 `LCSSAPass`（`LCSSA.cpp:557`），它们改 CFG 和 phi，所以 preserve 的 analysis 必须精确。

## LICM：为什么 `loop(licm)` 会直接 fatal

先跑教材里最常见的命令：

```bash
$ opt -passes='loop(licm)' -S licm.ll -o /dev/null
```

```text
LLVM ERROR: LICM requires MemorySSA (loop-mssa)
```

进程退出码是 1。原因在 `LICMPass::run`：

```cpp
// llvm/lib/Transforms/Scalar/LICM.cpp:252-255
PreservedAnalyses LICMPass::run(Loop &L, LoopAnalysisManager &AM,
                                LoopStandardAnalysisResults &AR, LPMUpdater &) {
  if (!AR.MSSA)
    reportFatalUsageError("LICM requires MemorySSA (loop-mssa)");
```

`loop(...)` 适配器构造 `LoopStandardAnalysisResults` 时把 `MSSA` 设为 `nullptr`；只有 `loop-mssa(...)` 才会去取 `MemorySSAAnalysis`（`LoopPassManager.cpp:218-226`）。所以命令要写成：

```bash
$ opt -passes='loop-mssa(licm)' -S licm.ll -o -
```

`-debug-pass-manager` 能把适配器偷偷加的 pass 打出来：

```bash
$ opt -passes='loop-mssa(licm)' -debug-pass-manager -S licm.ll -o /dev/null
```

```text
Running analysis: LoopAnalysis on may_alias
Running pass: LoopSimplifyPass on may_alias (10 instructions)
Running pass: LCSSAPass on may_alias (12 instructions)
Running analysis: MemorySSAAnalysis on may_alias
Running analysis: AAManager on may_alias
Running pass: LICMPass on loop %loop in function may_alias
```

这段输出同时证明了三件事：适配器会补 `LoopSimplify` 和 `LCSSA`；`MemorySSA` 是 loop pass 的显式分析；`LICMPass` 是按 `Loop*` 跑的。

### LICM 的两个方向：hoist load，或 promotion

输入 `licm.ll`：

```llvm
define void @may_alias(ptr %p, i32 %n) {
entry:
  %cmp0 = icmp sgt i32 %n, 0
  br i1 %cmp0, label %loop, label %exit
loop:
  %i = phi i32 [ 0, %entry ], [ %i.next, %loop ]
  %x = load i32, ptr %p
  %s = add i32 %x, %i
  store i32 %s, ptr %p
  %i.next = add i32 %i, 1
  %cmp = icmp slt i32 %i.next, %n
  br i1 %cmp, label %loop, label %exit
exit:
  ret void
}

; @no_alias 与上面相同，只改三处：
;   ptr noalias %p, ptr noalias %q, i32 %n
;   store i32 %s, ptr %q
```

`loop-mssa(licm)` 对 `@may_alias` 的输出（节选）：

```llvm
loop.preheader:
  %p.promoted = load i32, ptr %p, align 4
  br label %loop
loop:
  %s1 = phi i32 [ %s, %loop ], [ %p.promoted, %loop.preheader ]
  %i = phi i32 [ %i.next, %loop ], [ 0, %loop.preheader ]
  %s = add i32 %s1, %i
exit.loopexit:
  %s.lcssa = phi i32 [ %s, %loop ]
  store i32 %s.lcssa, ptr %p, align 4
```

同一块内存上的 load 和 store 当然可能互相覆盖，所以 `%x` 不能 hoist；LICM 改做 store promotion：把循环内的 load/store 对提升成一个 phi，循环出口只写一次。对 `@no_alias`：

```llvm
loop.preheader:
  %x = load i32, ptr %p, align 4
  br label %loop
loop:
  %i = phi i32 [ %i.next, %loop ], [ 0, %loop.preheader ]
  %s = add i32 %x, %i
exit.loopexit:
  %s.lcssa = phi i32 [ %s, %loop ]
  store i32 %s.lcssa, ptr %q, align 4
```

`noalias` 让 AA 证明 load 和 store 不冲突，`%x` 直接 hoist 到 preheader，store 沉到出口。`canSinkOrHoistInst`（`LICM.cpp:922`）就是做这个判断的地方：它用 AA、safety info 和循环的 memory 行为决定一条指令能不能动。

## LoopUnroll：默认不动，`-unroll-count` 才动

```bash
$ opt -passes='loop-unroll' -S unroll.ll -o -
```

这段循环 trip count 未知，默认输出只是补了 preheader/LCSSA，循环体没有展开。换一个用户强制的展开数：

```bash
$ opt -passes='loop-unroll' -unroll-count=4 -S unroll.ll -o -
```

```llvm
loop:
  %i = phi i64 [ 0, %loop.preheader ], [ %i.next.3, %loop.3 ]
  %x = load i32, ptr %pa, align 4
  %y = add i32 %x, 3
  store i32 %y, ptr %pb, align 4
  ; %i.next / %cond 的定义略
  br i1 %cond, label %loop.1, label %exit.loopexit
...
loop.3:
  %i.next.3 = add nsw i64 %i, 4
  br i1 %cond.3, label %loop, label %exit.loopexit, !llvm.loop !0
```

四个副本各自带 `cond` 检查，不是把 4 次迭代无条件塞进一个块。`-unroll-count=4` 是强制覆盖；默认决策在 `computeUnrollCount`（`LoopUnrollPass.cpp:970`），它要同时看 trip count、代码大小、寄存器压力和 target 的 unroll 偏好。`LoopUnrollPass::run` 在 `LoopUnrollPass.cpp:1619`。

## LoopVectorize：合法性、cost model、VPlan

`LoopVectorizePass` 是**函数 pass**，不是 loop pass（`llvm/lib/Passes/PassRegistry.def:650`，`llvm/include/llvm/Transforms/Vectorize/LoopVectorize.h:129`）。它的 `runImpl` 先做一件很多人不知道的事：

```cpp
// llvm/lib/Transforms/Vectorize/LoopVectorize.cpp:8040-8044
// The vectorizer requires loops to be in simplified form.
// Since simplification may add new inner loops, it has to run before the
// legality and profitability checks.
for (const auto &L : *LI)
  Changed |= CFGChanged |= simplifyLoop(L, DT, LI, SE, AC, nullptr,
                                        false /* PreserveLCSSA */, TTI);
```

然后 `collectSupportedLoops` 收集候选，`formLCSSARecursively` 补 LCSSA，`processLoop` 里跑合法性检查和 cost model。合法性回答「能不能向量化」：类型、控制流、call、reduction、alias；cost model 回答「值不值得」：VF、interleave、scalar epilogue 的开销。

### 实测：同一段循环，三种 `-mcpu`

```llvm
; vec.ll
define void @vec(ptr noalias %a, ptr noalias %b, i64 %n) {
entry:
  %cmp0 = icmp sgt i64 %n, 0
  br i1 %cmp0, label %loop, label %exit
loop:
  %i = phi i64 [ 0, %entry ], [ %i.next, %loop ]
  %pa = getelementptr inbounds i32, ptr %a, i64 %i
  %pb = getelementptr inbounds i32, ptr %b, i64 %i
  %x = load i32, ptr %pa
  %y = mul i32 %x, 2
  store i32 %y, ptr %pb
  %i.next = add i64 %i, 1
  %cond = icmp slt i64 %i.next, %n
  br i1 %cond, label %loop, label %exit
exit:
  ret void
}
```

三条命令只换 `-mcpu`，其余参数相同：

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=<cpu> \
      -pass-remarks=loop-vectorize -S vec.ll -o /dev/null
```

| `-mcpu` | remark |
|---|---|
| `generic` | `vectorized loop (vectorization width: 4, interleaved count: 2)` |
| `skylake-avx512` | `vectorized loop (vectorization width: 8, interleaved count: 4)` |
| `znver4` | `vectorized loop (vectorization width: 16, interleaved count: 4)` |

对应的向量 IR 也不同。`-mcpu=skylake-avx512` 的循环体里有 `%wide.load = load <8 x i32>` 和 `shl <8 x i32>`；`-mcpu=znver4` 则是 `<16 x i32>`。

`-mcpu=znver4` 的首条向量 load 是 `load <16 x i32>`。skylake-avx512 有 512-bit 寄存器，却选了 8×i32=256-bit；寄存器宽度是上限，不是命令，cost model 可以选更小的 VF。

如果连 `-mtriple` 都不给，`opt` 没有可用的 target 信息，会得到：

```text
remark: <unknown>:0:0: vectorization is not possible
remark: <unknown>:0:0: the cost-model indicates that interleaving is not beneficial
```

`<unknown>:0:0` 不是「没找到函数」，是这段 IR 没有 debug location；加 `-pass-remarks-output=rem.yaml` 后，YAML 里会写 `Function: vec` 和 `VectorizationFactor: '8'`。

### `-debug-only=loop-vectorize` 看决策过程

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=skylake-avx512 \
      -debug-only=loop-vectorize -S small.ll -o /dev/null
```

```text
LV: Checking a loop in 'trip2' from small.ll
LV: Found a loop: loop
LV: Loop passed LoopVectorizationLegality checks!
LV: Found trip count: 2
LV: The Widest register safe to use is: 256 bits.
LV: Clamping the MaxVF to maximum power of two not exceeding the constant trip count: 2
LV: VPlan created successfully. Loop can be vectorized.
LV: Memory widening: can be widened normally with cost 1
```

`Found trip count: 0` 在未知 trip count 的循环里表示「没有取到小常量」，不是循环跑 0 次；`trip2` 这段因为上界是常量 2，打印的是 2。合法性通过之后才进 VF 选择和 VPlan。

## 失败例子一：别名不是「不能向量化」，而是「要多少 runtime check」

把 `vec.ll` 的 `noalias` 去掉：

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=skylake-avx512 \
      -pass-remarks=loop-vectorize -S alias.ll -o alias-out.ll
remark: <unknown>:0:0: vectorized loop (vectorization width: 8, interleaved count: 4)
```

它仍然向量化了，只是多了一个 `vector.memcheck`：

```llvm
  br i1 %min.iters.check, label %vec.epilog.scalar.ph, label %vector.memcheck
vector.memcheck:
  ...
  br i1 %diff.check, label %vec.epilog.scalar.ph, label %vector.main.loop.iter.check
```

真正的门是 runtime check 的数量。把阈值降到 0，让这个门可见：

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=skylake-avx512 \
      -vectorize-memory-check-threshold=0 -pass-remarks-missed=loop-vectorize \
      -pass-remarks-output=alias-rem.yaml -S alias.ll -o /dev/null
```

```yaml
# alias.ll 的函数名仍是 @vec（文件由 vec.ll 改出）
--- !AnalysisAliasing
Name:            TooManyMemoryRuntimeChecks
Function:        vec
Args: [ 'loop not vectorized: too many memory checks needed' ]
```

默认阈值下这段 IR 会带 memcheck 向量化；阈值不允许时，同一个循环退回标量。review 时看到「加了 `noalias` 就差这么多」的性能 PR，先问 runtime check 的数量和位置，不要只看 `noalias` 两个字。

## 失败例子二：cost model 拒绝 gather

```llvm
; gather.ll
define void @gather(ptr noalias %a, ptr noalias %b, ptr noalias %idxp, i64 %n) {
entry:
  %cmp0 = icmp sgt i64 %n, 0
  br i1 %cmp0, label %loop, label %exit
loop:
  %i = phi i64 [ 0, %entry ], [ %i.next, %loop ]
  %pib = getelementptr inbounds i32, ptr %idxp, i64 %i
  %idx = load i32, ptr %pib
  %idx64 = sext i32 %idx to i64
  %pa = getelementptr inbounds i32, ptr %a, i64 %idx64
  %pb = getelementptr inbounds i32, ptr %b, i64 %i
  %x = load i32, ptr %pa
  %y = add i32 %x, 1
  store i32 %y, ptr %pb
  %i.next = add i64 %i, 1
  %cond = icmp slt i64 %i.next, %n
  br i1 %cond, label %loop, label %exit
exit:
  ret void
}
```

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=generic \
      -pass-remarks-missed=loop-vectorize -S gather.ll -o /dev/null
remark: <unknown>:0:0: the cost-model indicates that vectorization is not beneficial
remark: <unknown>:0:0: the cost-model indicates that interleaving is not beneficial
```

同一个文件换 `-mcpu=skylake-avx512`：

```text
remark: <unknown>:0:0: vectorized loop (vectorization width: 8, interleaved count: 4)
```

向量 IR 里出现 masked gather：

```llvm
%wide.masked.gather = call <8 x i32> @llvm.masked.gather.v8i32.v8p0(
    <8 x ptr> align 4 %wide.gep, <8 x i1> splat (i1 true), <8 x i32> poison)
```

`generic` 不是「不认识 gather 指令」，而是 cost model 认为展开成标量更便宜。这就是为什么同一段循环在不同 `-mcpu` 下结果不同：合法性可能都过，盈利判断不同。

还有一个纯合法性失败，和 cost model 无关：

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=skylake-avx512 \
      -pass-remarks-missed=loop-vectorize -S bad.ll -o /dev/null
```

```text
remark: <unknown>:0:0: loop not vectorized: call instruction cannot be vectorized
remark: <unknown>:0:0: loop not vectorized: instruction cannot be vectorized
remark: <unknown>:0:0: loop not vectorized
```

调用了一个没有向量化版本的 `@blackbox`，合法性阶段就拒绝，调 cost model 也救不回来。

## SLP：循环外的直线代码

`SLPVectorizerPass` 也是函数 pass（`SLPVectorizer.h:59`，`run` 在 `SLPVectorizer.cpp:30336`）。它处理的是基本块里的同构指令链，不是循环：

```llvm
; slp.ll（节选：4 条同构 load/add/store 链）
%x0 = load i32, ptr %pa0
%x1 = load i32, ptr %pa1
%x2 = load i32, ptr %pa2
%x3 = load i32, ptr %pa3
%y0 = add i32 %x0, 1   ; %y1..%y3 同理
store i32 %y0, ptr %pb0 ; %pb1..%pb3 同理
```

```bash
$ opt -passes='slp-vectorizer' -mtriple=x86_64-unknown-linux-gnu -mcpu=skylake-avx512 -S slp.ll -o -
```

```llvm
entry:
  %0 = load <4 x i32>, ptr %pa0, align 4
  %1 = add <4 x i32> %0, splat (i32 1)
  store <4 x i32> %1, ptr %pb0, align 4
```

同一段 IR 用 `-mtriple=i386-unknown-linux-gnu -mcpu=i386`，输出保持 4 条标量 load/add/store：target 没有可用的向量寄存器，SLP 不会硬造。`SLPVectorizerPass::runImpl` 在 `SLPVectorizer.cpp:30362`，它和 loop vectorizer 共用 TTI，但候选来源不同：一个从循环的 induction 出发，一个从 BB 内的同构树出发。

## TTI：为什么 `-mcpu` 能改变 IR

`TargetTransformInfo` 是 target 对中端的报价单。`getInstructionCost` 只是转发：

```cpp
// llvm/lib/Analysis/TargetTransformInfo.cpp:264
InstructionCost Cost = TTIImpl->getInstructionCost(U, Operands, CostKind);
```

真正的数字来自 target。x86 的 `X86TTIImpl::getArithmeticInstrCost` 在 `X86TargetTransformInfo.cpp:225`，`getNumberOfRegisters` 在 `:143`，`getMaxInterleaveFactor` 在 `:206`。向量化器用这些数字算：

- 这个 VF 的向量指令要几条、拆不拆；
- interleave 之后寄存器压力多大；
- scalar epilogue 和 runtime check 值不值。

所以「同一段循环，`-mcpu=generic` 宽 4、`skylake-avx512` 宽 8、`znver4` 宽 16」不是编译器随机，而是三份 cost table 和三组寄存器/交错参数的结果。反过来，看到一个向量化 PR 只改了 cost table，review 要问：这个数字对应哪条真实指令序列？有没有 benchmark 或 `llvm-mca` 证据？

## 一个被夸大的前提：preheader 不满足

很多资料说「没有 preheader 就不能向量化」。在这棵树上，`loop-vectorize` 会自己调 `simplifyLoop`（`LoopVectorize.cpp:8042`）。构造一个 header 有两条前驱的循环：

```llvm
define void @no_preheader(ptr noalias %a, ptr noalias %b, i64 %n, i1 %c) {
entry:
  br i1 %c, label %loop, label %side
side:
  br label %loop
loop:
  %i = phi i64 [ 0, %entry ], [ 0, %side ], [ %i.next, %loop ]
  ...
}
```

```bash
$ opt -passes='loop-vectorize' -mtriple=x86_64-unknown-linux-gnu -mcpu=skylake-avx512 \
      -pass-remarks=loop-vectorize -S bad.ll -o /dev/null
# 只摘 @no_preheader 的那一条；同文件里的 @with_call 会打印 call 拒绝理由
remark: <unknown>:0:0: vectorized loop (vectorization width: 8, interleaved count: 4)
```

它照样向量化了，`simplifyLoop` 补出了 preheader。这个结论是版本特定的：如果你直接调 `processLoop`、或者在一个已经假定 canonical form 的自定义 pipeline 里跑，preheader 仍然是前提。不要把它推广成「preheader 永远不重要」。

## Reviewer 视角：循环优化 PR 的检查顺序

1. **pass 类型对不对？** loop pass 需要 `LoopStandardAnalysisResults`，拿不到 MSSA 就会 fatal。
2. **canonical form 谁负责？** 直接读 `L->getLoopPreheader()` 的代码，调用点有没有先跑 `LoopSimplify`？
3. **analysis 保留是否精确？** 改了 CFG 却 preserve `LoopAnalysis`/`DominatorTreeAnalysis`，下一个 pass 会读脏数据。
4. **拒绝理由属于哪一类？** `CantVectorize*`（合法性）、`TooManyMemoryRuntimeChecks`（别名/版本化）、`VectorizationNotBeneficial`（cost model）的最小修复完全不同。
5. **测试是否覆盖反方向？** 向量化测试只 CHECK 向量指令，不 CHECK 标量回退；拒绝测试只 CHECK remark，不 CHECK IR 没变。

## 踩雷集锦

1. **「`loop(licm)` 是标准写法」**：在这个版本会直接 `LLVM ERROR: LICM requires MemorySSA`。用 `loop-mssa(licm)`；`loop(...)` 不会替你算 MemorySSA。
2. **「没有 preheader 就不能向量化」**：`LoopVectorizePass::runImpl` 自己调 `simplifyLoop`（`LoopVectorize.cpp:8042`）。先用 `-pass-remarks` 看真实拒绝理由，不要凭 preheader 猜。
3. **「没写 `noalias` 就不能向量化」**：它会生成 `vector.memcheck` 继续向量化；真正的门是 runtime check 数量（`TooManyMemoryRuntimeChecks`）。别名分析的结果是「要检查」，不是「放弃」。
4. **「`-mcpu` 只影响后端指令选择」**：`-mcpu` 改变 TTI 的寄存器数、交错因子和 cost table，中端向量化决策在 IR 层就分叉了。向量宽度 4/8/16 的差异发生在 `opt`，不是 `llc`。
5. **「`-pass-remarks` 没输出就是没分析」**：`-pass-remarks` 只报成功的 remark；拒绝理由要加 `-pass-remarks-missed`，部分分析细节要加 `-pass-remarks-analysis`（是否输出取决于 pass）。`<unknown>:0:0` 只是没有 debug location。
6. **「`loop-unroll` 默认会展开」**：trip count 未知、没有 profile 时默认不展开；`-unroll-count=4` 是强制覆盖，不是默认行为。

## 动手练习

1. 把 `licm.ll` 的 `@no_alias` 改成 `%p`/`%q` 可能别名，重跑 `loop-mssa(licm)`，解释为什么 load 不再 hoist。
2. 用 `-debug-pass-manager` 跑 `loop-mssa(licm)`，数一数适配器在 `LICMPass` 之前加了哪些 pass、算了哪些 analysis。
3. 把 `vec.ll` 的 `-mcpu` 换成 `nehalem`、`atom`、`znver4`，记录 VF/interleave，并用 `-pass-remarks-output` 保存三份 YAML 对比。
4. 给 `gather.ll` 加 `-mcpu=generic` 和 `-mcpu=skylake-avx512`，用 `-pass-remarks-missed` 解释为什么同一个循环一边拒绝一边接受。
5. 把 `alias.ll` 的 `-vectorize-memory-check-threshold` 从 0 调到 2，找出这段循环需要多少 runtime check 才能过。
6. 用 `-passes='slp-vectorizer'` 跑 `slp.ll`，再删掉两条 store，观察 SLP 什么时候放弃；构造一个 trip count 为 1 的循环，读 `-debug-only=loop-vectorize` 里关于 scalar epilogue 的行。

## 本章重点整理

- `loop(...)`/`loop-mssa(...)` 适配器会自动跑 `LoopSimplify` 和 `LCSSA`，并构造 `LoopStandardAnalysisResults`；`MSSA` 只在 `loop-mssa` 下非空。
- `LICMPass` 强制要求 MemorySSA；`loop(licm)` 会 fatal。LICM 能做 hoist、sink 和 store promotion，具体选哪个由 AA 和 safety info 决定。
- `LoopUnrollPass` 是 function pass，默认展开决策在 `computeUnrollCount`；`-unroll-count` 是强制覆盖。
- `LoopVectorizePass` 是 function pass，`runImpl` 自己 `simplifyLoop` + `formLCSSARecursively`，然后走合法性、cost model、VPlan。
- 别名不是「不能向量化」，而是「要不要 runtime check」；check 数量本身是成本。
- `-mcpu` 通过 TTI 的寄存器数、交错因子和 cost table 改变中端决策，所以同一段 IR 的 `opt` 输出可能不同。
- remark 分三类：合法性（`CantVectorize*`）、别名/版本化（`TooManyMemoryRuntimeChecks`）、盈利性（`VectorizationNotBeneficial`）。

## 自我检核

- [ ] 不看笔记，能画出 `loop-mssa(licm)` 从 `LoopAnalysis` 到 `LICMPass` 的 pass 顺序
- [ ] 能解释 `loop(licm)` 为什么 fatal，以及 `LoopStandardAnalysisResults::MSSA` 什么时候是 `nullptr`
- [ ] 能说明 `LoopSimplify` 和 `LCSSA` 各自补什么，为什么 loop pass 需要它们
- [ ] 能解释 LICM 在 `@may_alias` 和 `@no_alias` 上为什么做出不同变换
- [ ] 能读懂 `-debug-only=loop-vectorize` 里的合法性、VF clamp、VPlan 三类消息
- [ ] 能解释 `noalias` 去掉后为什么仍然向量化，以及 `vector.memcheck` 的角色
- [ ] 能解释 gather 循环在 `generic` 上被拒、在 `skylake-avx512` 上通过的原因
- [ ] 能说清 `-mcpu` 改变的是 TTI 的哪些量，而不是后端哪条指令
- [ ] 能指出「preheader 不满足就不能向量化」在这棵树上的反例和版本边界
- [ ] 拿到一个向量化 PR，能按合法性、别名、cost model 三类分别提问

## 延伸阅读

- **[LLVM Auto-Vectorization in LLVM](https://llvm.org/docs/Vectorizers.html)**
  - **读哪里**：`The Loop Vectorizer`（`llvm/docs/Vectorizers.md:16`）、`Runtime Checks of Pointers`（`:149`）、`Scatter / Gather`（`:254`）、`The SLP Vectorizer`（`:453`）
  - **能学到什么**：本章 remark 和 IR 形态的官方解释；runtime check、gather、SLP 的边界
  - **前提**：能读 `opt` 的 `-passes` 和基本 IR
- **[LLVM Loop Terminology (and Canonical Forms)](https://llvm.org/docs/LoopTerminology.html)**
  - **读哪里**：`Loop Simplify Form`（`llvm/docs/LoopTerminology.md:277`）和 `Loop Closed SSA (LCSSA)`（`:296`）
  - **能学到什么**：preheader、dedicated exits、单 backedge、LCSSA phi 的精确定义；本章 pass 栈的前提
  - **前提**：知道 `phi` 和支配关系
- **[LLVM Vectorization Plan](https://llvm.org/docs/VectorizationPlan.html)**
  - **读哪里**：`Vectorization Workflow`（`llvm/docs/VectorizationPlan.md:67`）和 `The Planning Process and VPlan Roadmap`（`:217`）
  - **能学到什么**：VPlan 为什么存在、cost model 在哪个阶段介入、VF/interleave 如何变成计划
  - **前提**：读过本章 `LoopVectorize` 一节；不要求读 VPlan 源码

→ [Ch 23 — CodeGen 流水线源码入口：ISel → Machine IR → RA → MC](./23-codegen-pipeline.md)
