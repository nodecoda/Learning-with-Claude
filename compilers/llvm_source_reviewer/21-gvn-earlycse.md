# Ch 21 — `GVN` / `EarlyCSE` / `NewGVN`：冗余消除的三种代价模型

> **目标**：拿到一段 IR 或一个改 `GVN.cpp` / `EarlyCSE.cpp` / `NewGVN.cpp` 的 PR 时，能判断哪个 pass 应该抓到这条冗余、它依赖哪个分析、为什么另一个 pass 抓不到，以及这条优化成立的前提（别名、`readonly`、`undef`、`poison`）有没有被测试锁住。

> **环境**：源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；工具 `/home/ubuntu/llvm/builds/pr-lane/bin`，LLVM 24.0.0git，assertions 开启（`llvm-config --assertion-mode` 输出 `ON`）。这个构建目录的 CMake cache 指向 `/home/ubuntu/llvm/trees/pr-lane`（`a5eaf194d8b6`），与 7c7a 不是同一个 commit；本章行号按 7c7a 核实，命令输出来自这个二进制。`-stats` 和 `-debug-only` 在 release 构建里会被编译掉，先确认 assertions 再怀疑 pass 没有日志。

## 为什么需要三个冗余消除 pass

冗余消除的核心问题可以写成一句话：**两个表达式在什么条件下一定算出同一个值？** 最简单的答案是「同一条指令」，但那只能抓 `%a = add i32 %x, %y` 紧跟着再来一次。真正值钱的是跨基本块、跨 `phi`、跨内存操作的等价。

三个 pass 的差别不是「新旧」，而是**愿意为多少证据付多少编译时间**：

```text
 EarlyCSE                 GVN                       NewGVN
 ────────                 ───                       ──────
 支配树 DFS               支配树 + 值编号             SSA + MemorySSA
 作用域哈希表             leader table               congruence class
 内存: generation 计数    load elimination + PRE     内存也进值编号
 遇到多前驱块保守          可以跨块、可以插 load       全局不动点
 便宜                     中等                       贵
```

历史脉络值得知道：值编号（value numbering）在 SSA 之前就有了，Alpern、Wegman、Zadeck 在 1988 年把它做成对 CFG 的全局分析；LLVM 的 `GVN` 是这套思路在支配树上的工程实现。`NewGVN` 的源码头注释直接点名了 Gargi 的 sparse predicated GVN、Simpson 的 SCC-based value numbering、Pai 的 SSA 冗余表达式检测——它不是 `GVN` 的增量补丁，而是另一套算法谱系（`NewGVN.cpp:17-46`）。

## 先看三种定位

| | `EarlyCSEPass` | `GVNPass` | `NewGVNPass` |
|---|---|---|---|
| 源码 | `llvm/lib/Transforms/Scalar/EarlyCSE.cpp` | `llvm/lib/Transforms/Scalar/GVN.cpp` | `llvm/lib/Transforms/Scalar/NewGVN.cpp` |
| 入口 | `EarlyCSEPass::run`（`EarlyCSE.cpp:1738`） | `GVNPass::run`（`GVN.cpp:1170`） | `NewGVNPass::run`（`NewGVN.cpp:4220`） |
| 遍历 | 支配树深度优先 | 支配树 + 迭代到不动点 | 可达块 RPO + 稀疏不动点 |
| 内存分析 | 默认无，`memssa` 可选 | 默认 MemDep，`gvn<memoryssa>` 可切 | 强制 MemorySSA |
| 典型强项 | 局部表达式、支配性 load、trivial dead store | 跨块 load、load PRE、标量 PRE | 全局 congruence、store-to-same-value |
| 代价 | 低 | 中 | 高 |

`EarlyCSEPass` 的构造函数默认 `UseMemorySSA = false`（`llvm/include/llvm/Transforms/Scalar/EarlyCSE.h:32`），`opt` 里用 `early-cse<memssa>` 打开。`GVNPass` 的默认走 MemDep：`ScalarOptions.td:116-118` 里 `enable-gvn-memdep` 是空的可选值、`enable-gvn-memoryssa` 默认 `false`；`gvn<memoryssa>` 会同时关掉 MemDep（`PassBuilder.cpp:1414-1428`）。`NewGVNPass::run` 则无条件 `AM.getResult<MemorySSAAnalysis>(F)`（`NewGVN.cpp:4228`）。

## EarlyCSE：沿支配树做一次带 generation 的扫描

`EarlyCSE` 的主体是一个支配树 DFS（`EarlyCSE.cpp:1687` 的 `EarlyCSE::run`）。它维护几张作用域哈希表：

- `AvailableValues`：简单表达式的值（`SimpleValue`）；
- `AvailableLoads`：指针 -> 最近一次 load 及其 generation；
- `AvailableCalls`：只读调用的结果；
- `AvailableInvariants`：`!invariant.load` 位置。

关键在 `CurrentGeneration`。任何可能写内存的操作都会把它加一；load 只有在 generation 相同、或者 MemorySSA 能证明中间没有 clobber 时，才能和之前的 load 合并（`EarlyCSE.cpp:995` 的 `isSameMemGeneration`）。没有 MemorySSA 时，代码对多前驱块有一个明确的保守规则：

```cpp
// EarlyCSE.cpp:1256-1262（注释大意的转写）
// 多前驱块可能让父节点 live-out 的内存值失效；这里选择保守：
if (!BB->getSinglePredecessor())
  ++CurrentGeneration;
```

这句话解释了后面实测里最反直觉的一幕：同一个 load，在直线块里能合并，到了两条前驱汇合的 `merge` 块就合不了；加上 `memssa` 又能合。

### 实测：三种 pass 跑同一段 IR

```llvm
; redundancy.ll
define i32 @cse_local(i32 %a, i32 %b) {
entry:
  %sum1 = add i32 %a, %b
  %sum2 = add i32 %a, %b
  %mul = mul i32 %sum1, %sum2
  ret i32 %mul
}

define i32 @merge_two(ptr %p, i1 %c) {
entry:
  %v = load i32, ptr %p
  br i1 %c, label %left, label %right
left:
  br label %merge
right:
  br label %merge
merge:
  %w = load i32, ptr %p
  %r = add i32 %v, %w
  ret i32 %r
}

define void @store_same(ptr %p) {
entry:
  %v = load i32, ptr %p
  store i32 %v, ptr %p
  ret void
}
```

命令与关键输出（`-S -o -`，只摘函数体）：

```bash
$ opt -passes='function(early-cse)' -S redundancy.ll -o -
```

```llvm
define i32 @cse_local(i32 %a, i32 %b) {
entry:
  %sum1 = add i32 %a, %b
  %mul = mul i32 %sum1, %sum1
  ret i32 %mul
}

define i32 @merge_two(ptr %p, i1 %c) {
entry:
  %v = load i32, ptr %p, align 4
  ...
merge:
  %w = load i32, ptr %p, align 4
  %r = add i32 %v, %w
  ret i32 %r
}

define void @store_same(ptr %p) {
entry:
  %v = load i32, ptr %p, align 4
  ret void
}
```

`early-cse<memssa>` 的 `merge_two` 变成：

```llvm
merge:
  %r = add i32 %v, %v
  ret i32 %r
```

`gvn` 和 `newgvn` 的 `merge_two` 也都是 `%r = add i32 %v, %v`。但 `store_same` 出现分叉：`early-cse` 和 `newgvn` 删掉了那条「把刚读出的值写回去」的 store，`gvn` 保留：

```llvm
; gvn 的输出
define void @store_same(ptr %p) {
entry:
  %v = load i32, ptr %p, align 4
  store i32 %v, ptr %p, align 4
  ret void
}
```

这张表可以直接背下来，但更重要的是知道每一格的原因：

| 测试 | `early-cse` | `early-cse<memssa>` | `gvn` | `newgvn` |
|---|---|---|---|---|
| `cse_local` | 合并 | 合并 | 合并 | 合并 |
| `merge_two` 的 load | **不合并** | 合并 | 合并 | 合并 |
| `store_same` 的 store | 删除 | 删除 | **保留** | 删除 |

`store_same` 不是 GVN 的 bug。GVN 的职责在头注释里写得很窄：「eliminate fully redundant instructions」加「simple dead load elimination」（`GVN.cpp:9-11`）；删除「store 的值等于内存里已有的值」属于另一类推理，`NewGVN` 在 `runGVN` 的收尾阶段专门做（`NewGVN.cpp:4150` 打印 `Marking dead store`）。

## GVN：leader table、phi 翻译与 load elimination

GVN 的值编号分成两层：

- `ValueNumbering`：`Value *` -> 值编号；
- `ExpressionNumbering`：`Expression` -> 值编号。`Expression` 由 opcode、类型、操作数的值编号组成；交换律指令会把前两个操作数的编号排序（`GVN.cpp:612-620`），所以 `add %a, %b` 和 `add %b, %a` 拿到同一个编号。

只有编号还不够，因为要把「这个编号在某个前驱上是什么」翻译过 `phi`。这就是 leader table：

```cpp
// GVN.cpp:159-170
/// A mapping from value numbers to lists of Value*'s that
/// have that value number. Use getLeaders to query it.
class llvm::GVNLeaderMap {
public:
  struct LeaderTableEntry {
    AssertingVH<Value> Val;
    const BasicBlock *BB;
    ...
  };
```

每个 leader 都带 `BB`。`phiTranslateImpl` 先看 `NumberingPhi`：如果编号对应一个 `phi`，且 `phi` 就在当前汇合块，就沿着给定前驱取 incoming value 的编号；否则看 leader 是不是全在 `PhiBlock` 里（`GVN.cpp:3311-3360`）。`areAllValsInBB` 那个提前退出很关键：只要有一个 leader 在别的块，就不能把表达式穿过这个 `phi` 继续翻译。

load elimination 走 `processLoad`（`GVN.cpp:3113`）：

1. 有序/volatile load 直接放弃；
2. 用 MemDep 或 MemorySSA 找 reaching value；
3. 本地能消就消；
4. 跨块则进 `processNonLocalLoad`（`GVN.cpp:2414`），分析每个前驱的可用值，必要时插入 load 或做标量 PRE。

打开调试输出能看到这条路径：

```bash
$ opt -passes='function(gvn)' -debug-only=gvn -S redundancy.ll -o /dev/null
```

```text
GVN iteration: 0
GVN iteration: 1
GVN iteration: 0
GVN: load i32 %v has unknown dependence
GVN REMOVING NONLOCAL LOAD:   %w = load i32, ptr %p, align 4
GVN iteration: 1
GVN: load i32 %v has unknown dependence
GVN iteration: 0
GVN: load i32 %v has unknown dependence
```

`GVN iteration: 0/1` 是 pass 内部的不动点迭代，不是 pipeline 跑了两次。`unknown dependence` 说明 `%v` 这个 load 在某个方向上找不到可用的 reaching value；到 `merge_two` 的 `%w` 时，两个前驱都提供了同一个值，于是打印 `REMOVING NONLOCAL LOAD`。

统计输出能把「删了几条」和「为什么删」分开：

```bash
$ opt -passes='function(gvn)' -stats -S redundancy.ll -o /dev/null
```

```text
1 aa     - Number of MustAlias results
2 gvn    - Number of instructions deleted
1 gvn    - Number of loads deleted
3 memdep - Number of uncached non-local ptr responses
```

同一个输入换 `newgvn`：

```text
1 newgvn - Number of redundant/dead stores eliminated
2 newgvn - Maximum Number of iterations it took to converge GVN
```

`newgvn` 的统计里没有 load 计数，但有 dead store；`gvn` 的统计里有 load，没有 dead store。这比「NewGVN 更强」这种口号有用。

## NewGVN：把值和内存都放进 congruence class

`NewGVN` 的核心数据结构是 `CongruenceClass`（`NewGVN.cpp:276`）：一个类里的所有成员在每次执行时都相等。`NewGVN` 维护 `Value` 到类、`MemoryAccess` 到类的映射，然后反复处理「被触碰」的指令，直到没有类发生变化（`runGVN`，`NewGVN.cpp:3421`）。

它和 GVN 最大的设计差异在源码头注释里：

- GVN 只记 `operation -> value number`，一个编号变了就要重扫；
- NewGVN 同时记 `value number -> operations`，只重扫受影响的部分（`NewGVN.cpp:27-38`）；
- 为了处理 `phi(a+b, c+d)` 与 `phi(a,c) + phi(b,d)` 的等价，它生成 `phi of ops`（`NewGVN.cpp:40-45`）。

这也解释了它为什么慢、为什么至今不是默认管道。`PassBuilderPipelines.cpp:657-660` 在 `Opts.enable_newgvn` 为真时才插 `NewGVNPass`，否则插 `GVNPass`；这个开关默认 `false`（`PassesOptions.td:71`）。源码里 `enable-phi-of-ops` 的注释还留着「可能带来正确性问题」的字样（`ScalarOptions.td:473`），review 时不要把 `newgvn` 当成 `gvn` 的 drop-in replacement。

## 失败例子一：`readonly` 说谎，GVN 照单全收

先看一段**能通过 verifier** 的 IR：

```llvm
; lie.ll
define void @writer(ptr %p) readonly {
  store i32 42, ptr %p
  ret void
}

define i32 @across_call(ptr %p) {
  %v = load i32, ptr %p
  call void @writer(ptr %p)
  %w = load i32, ptr %p
  %r = add i32 %v, %w
  ret i32 %r
}
```

```bash
$ opt -passes=verify -S lie.ll -o /dev/null
$ echo $?
0
```

verifier 不会拿函数体去反驳 `readonly`。GVN 看到的调用带 `memory(read)`，于是认为第二次 load 冗余：

```bash
$ opt -passes='function(gvn)' -S lie.ll -o -
```

```llvm
; Function Attrs: memory(read)
define void @writer(ptr %p) #0 {
  store i32 42, ptr %p, align 4
  ret void
}

define i32 @across_call(ptr %p) {
  %v = load i32, ptr %p, align 4
  call void @writer(ptr %p)
  %r = add i32 %v, %v
  ret i32 %r
}
```

如果 `@writer` 真的写了 42，原程序返回 `%v + 42`，变换后返回 `%v + %v`。这不是 GVN「忽略了写」，而是 IR 自己承诺了「这里没有写」。`readonly`、`memory(...)`、`noalias`、`dereferenceable` 都是承诺；加错一个，优化器就会把错误放大。`EarlyCSE` 在这段 IR 上也会删掉第二次 load，所以这不是某个 pass 独有的坑。

review 时看到 PR 修改调用或函数上的内存属性，至少问三句：被调用的函数体真的满足吗？调用点有没有通过指针捕获写内存？测试里有没有一个「函数体确实写」的负例？

## 失败例子二：未初始化 load 被折成常量，但它是合法的

```llvm
; undef-load.ll
%struct.anon = type { i32, i8, i8, i8, i8 }

define i32 @a() {
entry:
  %c = alloca %struct.anon
  %tmp = getelementptr %struct.anon, ptr %c, i32 0, i32 0
  %tmp1 = getelementptr i32, ptr %tmp, i32 1
  %tmp2 = load i32, ptr %tmp1, align 4
  %tmp3 = or i32 %tmp2, 11
  %tmp4 = and i32 %tmp3, -21
  store i32 %tmp4, ptr %tmp1, align 4
  %call = call i32 (...) @x(ptr %c)
  ret i32 undef
}

declare i32 @x(...)
```

```bash
$ opt -passes='function(gvn)' -S undef-load.ll -o -
```

```llvm
define i32 @a() {
entry:
  %c = alloca %struct.anon, align 8
  %tmp1 = getelementptr i32, ptr %c, i32 1
  store i32 -21, ptr %tmp1, align 4
  %call = call i32 (...) @x(ptr %c)
  ret i32 undef
}
```

第一眼像 miscompile：load 没了，`(x | 11) & -21` 变成了常量 `-21`。但 `%c` 是未初始化的 alloca，`load` 的结果是 `undef`，不是 UB；`undef` 的每次使用可以是任意值；`11 = 0b1011`、`-21 = 0xFFFFFFEB`，`-21` 的 bit 0/1/3 为 1、bit 2/4 为 0，正好满足这两个掩码，所以 `(undef | 11) & -21` 的可行集合包含 `-21`，GVN 选了一个具体值。这是 refinement，不是错误。LangRef 对 `undef` 的定义是这条推理的唯一依据。

真正需要小心的是 `poison` 和 `!noundef`：`!noundef` 会把「元数据不成立」提升成 UB，GVN 在合并 load 时必须正确处理。`AvailableValue::MaterializeAdjustedValue` 在强制转换 load 类型时会丢掉一批元数据，只有原 load 带 `!noundef` 才保留（`GVN.cpp:1432-1470`）；`llvm/test/Transforms/GVN/noundef.ll` 就是锁这条行为的测试。

## Reviewer 视角：这条 PR 该看什么

拿到改这三个 pass 的 diff，按下面的顺序看，比从缩进开始有用：

1. **新抓的冗余依赖什么前提？** 别名、`readonly`、`dereferenceable`、`!invariant.load`、`!noundef` 各是哪一条？
2. **前提不成立时会不会 miscompile？** 构造一个「前提为假」的 IR，跑 `verify` 和 `opt`，看输出是否仍然语义等价。
3. **metadata 怎么合并？** load 被替换/强制转换时，`!tbaa`、`!alias.scope`、`!noalias`、`!noundef` 是否按 `combineMetadataForCSE` 的规则处理。
4. **analysis 有没有正确保留？** `GVNPass::run` 在改动后保留 `DominatorTreeAnalysis`、`LoopAnalysis`、`TargetLibraryAnalysis`，有 MSSA 时保留 `MemorySSAAnalysis`（`GVN.cpp:1195-1200`）；`NewGVNPass::run` 只保留 DT（`NewGVN.cpp:4234-4236`）。
5. **测试是否锁住了「不该优化」的方向？** 只加 `CHECK: ret i32 %v` 不够，负例要能证明前提被破坏时优化不会发生。

## 踩雷集锦

1. **「`-passes='function(gvn)'` 只跑一遍 GVN」**：`-debug-only=gvn` 会打印多轮 `GVN iteration`。GVN 内部迭代到不动点，统计数字是累计的，不要拿它推算 pipeline 调用次数。
2. **「`-stats` 没输出就是没优化」**：先看构建有没有统计支持。release 构建可能把统计编译掉；`llvm-config --assertion-mode` 是 `ON` 不代表 `LLVM_ENABLE_STATS` 一定开，但本课的 `pr-lane` 构建两者都可用。
3. **「EarlyCSE 只做块内 CSE」**：它沿支配树传播，直线块和单前驱块都能跨块；真正让它失效的是多前驱块的 generation 保守规则（`EarlyCSE.cpp:1261`）。加 `memssa` 才能用 MemorySSA 证明中间没有 clobber。
4. **「GVN 会删掉所有冗余 store」**：在这棵树上，`store i32 %v, ptr %p` 紧跟在 `%v = load ptr %p` 后面，GVN 保留，NewGVN 删除。换 pass 之前先确认你要的是 load elimination 还是 store elimination。
5. **「NewGVN 是 GVN 的新版本，可以无脑替换」**：算法、内存模型、编译时间都不同；默认管道仍走 GVN（`PassBuilderPipelines.cpp:657-660`），`enable-phi-of-ops` 的源码注释还标着正确性风险。

## 动手练习

1. 把 `redundancy.ll` 的 `merge_two` 改成三个前驱，分别跑 `early-cse`、`early-cse<memssa>`、`gvn`、`newgvn`，解释每个输出。
2. 在 `merge_two` 的 `left`/`right` 里各加一条 `store i32 0, ptr %p`，观察哪些 pass 还会合并 `merge` 的 load；用 `-debug-only=gvn` 找出拒绝的理由。
3. 给 `store_same` 的 store 换成 `store i32 0, ptr %p`，再跑 `newgvn`，确认它不再删；解释「same value」和「same location」的区别。
4. 用 `-stats` 比较 `gvn` 与 `newgvn` 在 `redundancy.ll` 上的计数，指出哪一个统计项对应 `store_same`。
5. 把 `lie.ll` 的 `readonly` 去掉，重跑 `gvn`，确认第二次 load 保留；再用 `opt -passes='function(gvn)' -verify-each` 确认变换后的 IR 合法。
6. 读 `llvm/test/Transforms/NewGVN/deadstore.ll`，找出它锁住的 store 删除条件，并写一个你自己的反例。

## 本章重点整理

- `EarlyCSE` 是支配树上的快速扫描；表达式 CSE 跨块有效，内存 CSE 受 generation 计数约束，多前驱块在没有 MemorySSA 时会保守失效。
- `GVN` 用 `ValueNumbering` + `ExpressionNumbering` 做值编号，用 leader table 的 `BB` 标签做 phi 翻译，用 MemDep 或 MemorySSA 做 load elimination 和 PRE。
- `NewGVN` 把值和内存访问都放进 congruence class，用稀疏不动点求全局等价；它能删 store-to-same-value，代价是编译时间和实现复杂度。
- `readonly`/`memory(...)`/`noalias`/`dereferenceable` 是承诺，不是提示；verifier 不验证函数体是否遵守。
- 未初始化 alloca 的 load 是 `undef`，不是 UB；把 `undef` 折成具体值是合法 refinement。
- 三个 pass 的输出差异是「证据强度」的差异，不是「谁更先进」的排名。

## 自我检核

- [ ] 不看笔记，能说出三个 pass 各自的内存模型和典型强项
- [ ] 能解释 `EarlyCSE.cpp:1261` 为什么让 `merge_two` 的 load 合不了，以及 `memssa` 改变了什么
- [ ] 能画出 leader table 的结构，并说明 `BB` 字段在 `phiTranslate` 里的作用
- [ ] 能读懂 `-debug-only=gvn` 的 `GVN iteration` 和 `REMOVING NONLOCAL LOAD`，并对应到具体指令
- [ ] 能构造一个 GVN 保留、NewGVN 删除的冗余 store，并解释原因
- [ ] 能解释 `readonly` 说谎为什么会让 GVN 产出错误代码，以及 verifier 为什么放行
- [ ] 能解释未初始化 load 被折成 `-21` 为什么不是 miscompile
- [ ] 拿到一个改 GVN 的 PR，能列出至少三个关于前提、metadata、analysis 保留的问题
- [ ] 能根据一段 IR 选择 `early-cse`、`gvn` 还是 `newgvn`，并说明你预期的输出

## 延伸阅读

- **[LLVM Language Reference: Undef Values](https://llvm.org/docs/LangRef.html#undef-values)**
  - **读哪里**：整节，尤其是 `xor undef, undef` 和「每次使用可以不同」的措辞
  - **能学到什么**：为什么未初始化 load 的折叠是合法的，以及 `undef` 与 `poison` 的边界
  - **前提**：先读完本章「失败例子二」
- **[LLVM Language Reference: Poison Values](https://llvm.org/docs/LangRef.html#poison-values)**
  - **读哪里**：整节，包括 immediate UB 的列表和 `and poison, 0`
  - **能学到什么**：GVN 在合并 load/metadata 时哪些行为是 refinement，哪些会制造 UB
  - **前提**：知道 `freeze` 的作用
- **[NewGVN.cpp 文件头注释](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Transforms/Scalar/NewGVN.cpp)**
  - **读哪里**：`NewGVN.cpp:17-46`，以及 `CongruenceClass` 和 `runGVN` 的注释
  - **能学到什么**：sparse predicated GVN、`phi of ops`、为什么它和 GVN 不是同一套算法
  - **前提**：读过本章的 leader table 一节；不需要先读论文
- **[Alpern, Wegman, Zadeck, "Detecting Equality of Variables in Programs" (POPL 1988)](https://doi.org/10.1145/73560.73561)**
  - **读哪里**：值编号的定义和 CFG 上的传播规则
  - **能学到什么**：GVN 这类算法的原始问题形状；读完再看 `GVNValueTable::createExpr` 会清楚很多
  - **前提**：能读基本的 dataflow 记号
- **[`llvm/test/Transforms/GVN/noundef.ll`](https://github.com/llvm/llvm-project/blob/main/llvm/test/Transforms/GVN/noundef.ll) 与 [`llvm/test/Transforms/NewGVN/deadstore.ll`](https://github.com/llvm/llvm-project/blob/main/llvm/test/Transforms/NewGVN/deadstore.ll)**
  - **读哪里**：`RUN:` 行与每个 `CHECK` 的对应关系
  - **能学到什么**：metadata 合并和 dead store 删除的真实契约；测试就是最精确的规格说明
  - **前提**：会读 FileCheck

→ [Ch 22 — 循环与向量化：`LoopUnroll`、`LICM`、`LoopVectorize`、TTI](./22-loop-and-vectorizer.md)
