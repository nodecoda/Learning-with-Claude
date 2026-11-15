# Ch 5 — DAG Combine 与 Legalize：选择之前的战争

> **目标**：看懂 SelectionDAG 在 pattern matching 之前经历的六个阶段：三次 DAG combine、类型合法化、操作合法化。学完这章，你能解释「IR 里明明是 `mul`，汇编里为什么是 `slli`」「`store(load(p))` 为什么消失」「`i8` 加法为什么变成 64 位加法」「没有 `M` 扩展时 `mul` 为什么变成函数调用」，并说清这些改写如何改变调度输入。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。本章统一用 `-mtriple=riscv64 -mcpu=generic -O2`，feature 用 `-mattr` 单独控制。DAG 阶段用 `-debug-only=isel`，combine 细节用 `-debug-only=dagcombine`，合法化细节用 `-debug-only=legalize-types` 和 `-debug-only=legalizedag`。

## 为什么需要这个？

很多人把 instruction selection 想成一张表：

```text
IR 的 mul  ->  查表  ->  MUL 指令
```

真实情况是：

```text
IR 的 mul
  -> 可能被 IR 层 InstCombine 改成 shl
  -> 可能被 DAG combine 改成 shl
  -> 可能被类型合法化提升到 i64
  -> 可能被操作合法化展开成 shift/add
  -> 可能被操作合法化降级成 libcall
  -> 最后才轮到 pattern matching
```

也就是说，**pattern matching 看到的不是 IR，而是一张已经被改写过多轮的 DAG**。面试里问「这条指令是谁选的」，如果只回答「TableGen pattern」，通常会漏掉前面这些步骤。

这一章要建立三个判断：

1. **形状是谁改的**：`mul x, 2` 变成 `shl x, 1`，是 DAG combine，不是 pattern。
2. **类型是谁改的**：`i8` 加法变成 `i64` 加法，是 type legalization，不是 ABI。
3. **指令数量是谁改的**：没有 `M` 时 `mul` 变成 `__muldi3` 调用，是 operation legalization，不是 scheduler。

## 先建立直觉

`SelectionDAGISel::CodeGenAndEmitDAG()` 的顺序可以画成一条流水线。右边是 `-debug-only=isel` 打出的阶段标题，也是你调试时真正能看到的东西：

```text
SelectionDAGBuilder
   │
   ├─ Initial selection DAG
   │
   ├─ DAG combine #1（BeforeLegalizeTypes）
   ├─ Type legalization
   ├─ DAG combine #2（AfterLegalizeTypes）
   ├─ Operation legalization
   ├─ DAG combine #3（AfterLegalizeDAG）
   │
   ├─ Optimized lowered selection DAG
   ├─ Type-legalized selection DAG
   ├─ Optimized type-legalized selection DAG
   ├─ Legalized selection DAG
   ├─ Optimized legalized selection DAG
   │
   ├─ Instruction selection
   └─ Selected selection DAG
```

注意标题的顺序和 combine 的顺序不是一一对应的：

- `Optimized lowered selection DAG` 是 combine #1 之后。
- `Type-legalized selection DAG` 是类型合法化之后。
- `Optimized type-legalized selection DAG` 是 combine #2 之后。
- `Legalized selection DAG` 是操作合法化之后。
- `Optimized legalized selection DAG` 是 combine #3 之后。

`-debug-only=isel` 会把每个阶段的完整 DAG 打出来。调试时不要一次看全部，先 `grep` 函数名，再对比相邻两个阶段的差异。

## 六个阶段各自做什么

| 阶段 | 主要工作 | 典型改写 | 谁负责 |
|---|---|---|---|
| Initial selection DAG | 把 IR 降成 DAG：参数、GEP、load/store、调用、控制流 | `getelementptr` 变成 `add`；参数变成 `CopyFromReg` | `SelectionDAGBuilder` |
| DAG combine #1 | 在类型还可能是非法的时候做代数化简 | `mul x, 2` → `shl x, 1`；`add x, 0` → `x` | `DAGCombiner` |
| Type legalization | 把 target 不支持的 Type 变成支持的 Type | `i8`/`i16` → `i64`；`i128` → 两个 `i64` | `LegalizeTypes` |
| DAG combine #2 | 清理类型合法化产生的 extend/truncate | `any_extend(add(trunc, trunc))` → `add` | `DAGCombiner` |
| Operation legalization | 把 target 不支持的 operation 展开、降级或转 libcall | 无 `M` 的 `mul` → `__muldi3`；无 `Zbb` 的 `ctpop` → 位运算序列 | `SelectionDAGLegalize` |
| DAG combine #3 | 在合法 DAG 上做最后的 target 相关整理 | 常量乘展开成 `shl`/`add`/`sub`；地址折叠 | `DAGCombiner` + target hook |

这张表最重要的地方是：**combine 不是一次，而是三次**。每次 combine 能看到的东西不同：

- combine #1 在类型合法化之前，能利用高层类型信息，但不能假设所有类型都合法。
- combine #2 在类型合法化之后，能看到被提升/拆分的节点。
- combine #3 在操作合法化之后，能看到 target-specific 节点，也能做最后的指令形状准备。

## 主线一：DAG combine 改变算术语义

用三个小函数：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @addzero(i64 %a) {
  %r = add i64 %a, 0
  ret i64 %r
}

define i64 @mul2(i64 %a) {
  %r = mul i64 %a, 2
  ret i64 %r
}

define i64 @storeload(ptr %p) {
  %v = load i64, ptr %p
  store i64 %v, ptr %p
  ret i64 %v
}
```

### `mul x, 2` → `shl x, 1`

先看 `@mul2` 的初始 DAG 和 combine #1 之后的 DAG：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -debug-only=isel combine.ll -o /dev/null
```

本机实测（节选）：

```text
Initial selection DAG: %bb.0 'mul2:'
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t4: i64 = mul t2, Constant:i64<2>
  t6: ch,glue = CopyToReg t0, Register:i64 $x10, t4

Optimized lowered selection DAG: %bb.0 'mul2:'
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t9: i64 = shl t2, Constant:i64<1>
  t6: ch,glue = CopyToReg t0, Register:i64 $x10, t9
```

`mul` 节点消失了，换成了 `shl`。`-debug-only=dagcombine` 能直接看到这次改写：

```text
Combining: t4: i64 = mul t2, Constant:i64<2>
 ... into: t9: i64 = shl t2, Constant:i64<1>
```

最终汇编：

```asm
mul2:
	slli	a0, a0, 1
	ret
```

这里的因果链是：

```text
IR mul x, 2
  -> DAG combine: mul by power-of-two -> shl
  -> pattern matching: SHL -> SLLI
  -> 汇编: slli
```

如果你只看 pattern，会以为是 `SHL` 的 pattern 把 `mul` 选成了 `slli`。实际不是：`mul` 在 pattern 之前就已经不存在了。

### `store(load(p))` → 只保留 load

`@storeload` 更有意思。初始 DAG：

```text
Initial selection DAG: %bb.0 'storeload:'
  t5: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
  t6: ch = store<(store (s64) into %ir.p)> t5:1, t5, t2, poison:i64
  t8: ch,glue = CopyToReg t6, Register:i64 $x10, t5
```

Combine 之后：

```text
Optimized lowered selection DAG: %bb.0 'storeload:'
  t5: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
  t8: ch,glue = CopyToReg t5:1, Register:i64 $x10, t5
```

`store` 节点整个消失了。`-debug-only=dagcombine` 的输出：

```text
Combining: t6: ch = store<(store (s64) into %ir.p)> t5:1, t5, t2, poison:i64
 ... into: t5: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
```

原因是：把刚 load 出来的值原样 store 回同一地址，没有可观察的效果；store 被删除，load 的 chain 直接接到返回值。最终汇编只有一条 `ld`：

```asm
storeload:
	ld	a0, 0(a0)
	ret
```

这个改写对调度的影响很直接：原本有 `load → store` 的内存 chain 边，现在整条边消失，scheduler 少了一个 barrier。

### `-combiner-disabled` 不是「IR 原样」

很多人用 `-combiner-disabled` 想看「没有 combine 的 DAG」。它确实能关掉 generic combine，但不等于 IR 原样。实测 `@mul2`：

```asm
# -combiner-disabled
mul2:
	slli	a1, a0, 1
	slli	a0, a0, 2
	sub	a0, a0, a1
	ret
```

这不是 `x * 2` 的 `slli`，而是 `(x << 2) - (x << 1)`，也就是 `4x - 2x`。为什么？

- `-combiner-disabled` 只关掉 generic combine（`DisableGenericCombines`）。
- RISC-V 的 `PerformDAGCombine` 仍然会调用 `performMULCombine` → `expandMul`。
- 当 generic combine 没有把 `mul x, 2` 改成 `shl` 时，target combine 在合法化之后把它展开成「两个 shift + 一个 sub」，以避免 `MUL` 的延迟。

`@storeload` 也一样：

```asm
# -combiner-disabled
storeload:
	mv	a1, a0
	ld	a0, 0(a0)
	sd	a0, 0(a1)
	ret
```

store 没有被删除，因为删除 `store(load)` 的是 generic combine；target combine 不会做这件事。

这两个例子说明：**「有没有 combine」不是一个布尔值**。generic combine 和 target combine 是两层，`-combiner-disabled` 只关掉前一层。

## 主线二：type legalization 改变位宽

RISC-V RV64 的合法整数类型不是「所有整数类型」。`i8`、`i16`、`i1` 往往需要提升，`i128` 需要拆分。用下面的函数观察：

```llvm
target triple = "riscv64-unknown-elf"

define i8 @add8(i8 %a, i8 %b) { %r = add i8 %a, %b ret i8 %r }
define i16 @add16(i16 %a, i16 %b) { %r = add i16 %a, %b ret i16 %r }
define i32 @add32(i32 %a, i32 %b) { %r = add i32 %a, %b ret i32 %r }
define i1 @cmp(i64 %a, i64 %b) { %r = icmp ult i64 %a, %b ret i1 %r }
define i128 @add128(i128 %a, i128 %b) { %r = add i128 %a, %b ret i128 %r }
```

跑：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -debug-only=legalize-types types.ll -o /dev/null
```

`@add8` 的关键日志：

```text
Legalizing node: t7: i8 = add t5, t6
Analyzing result type: i8
Promote integer result: t7: i8 = add t5, t6

Legalizing node: t8: i64 = any_extend t7
Analyzing operand: t7: i8 = add t5, t6
Promote integer operand: t8: i64 = any_extend t7
Replacing: t8: i64 = any_extend t7
     with: t12: i64 = add t2, t4
```

读法：

1. `i8` 类型不合法，`LegalizeTypes` 选择 **Promote**，把 `i8` 提升成 `i64`。
2. 两个 `i8` 参数先被提升成 `i64`。
3. `add i8` 变成 `add i64`。
4. 原来的 `any_extend` 被消掉，因为结果已经是 `i64`。

最终 `@add8` 的汇编和 `@add16` 一样，都是 64 位 `add`：

```asm
add8:
	add	a0, a0, a1
	ret

add16:
	add	a0, a0, a1
	ret
```

`@add32` 不一样：

```asm
add32:
	addw	a0, a0, a1
	ret
```

`i32` 在 RV64 上是合法类型，但 32 位运算要用 `W` 形式（`addw`），因为它需要把结果符号扩展回 64 位。这里体现的是「类型合法」和「操作用哪种指令」是两件事。

`@add128` 则被拆成两个 `i64`，并生成进位链：

```asm
add128:
	add	a2, a0, a2
	add	a1, a1, a3
	sltu	a0, a2, a0
	add	a1, a1, a0
	mv	a0, a2
	ret
```

这段汇编值得逐条看：

- `add a2, a0, a2`：低 64 位相加。
- `add a1, a1, a3`：高 64 位相加。
- `sltu a0, a2, a0`：低 64 位是否溢出（无符号比较）。
- `add a1, a1, a0`：把进位加到高 64 位。
- `mv a0, a2`：把低 64 位放到返回寄存器。

这是一个真实的依赖链：低位的 `add` 和 `sltu` 必须在高位的最终 `add` 之前完成。type legalization 把一个 IR 加法变成了五条指令，scheduler 的输入也随之改变。

类型合法化的动作不只有 Promote，常见还有：

- **Expand**：拆成更小的合法类型（`i128` → 两个 `i64`）。
- **Soften**：把非法浮点类型转成整数运算。
- **Scalarize**：把向量元素拆成标量。
- **Split**：把向量拆成两半。

## 主线三：operation legalization 决定「有没有这条指令」

类型合法之后，还要检查每个 operation 在 target 上是否合法。用：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @mul(i64 %a, i64 %b) { %r = mul i64 %a, %b ret i64 %r }
define i64 @ctpop(i64 %a) { %r = call i64 @llvm.ctpop.i64(i64 %a) ret i64 %r }

declare i64 @llvm.ctpop.i64(i64)
```

### 没有 `M`：`mul` 变成 libcall

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -O2 -debug-only=legalizedag legal.ll -o /dev/null
```

关键日志：

```text
Legalizing: t5: i64 = mul t2, t4
Trying to expand node
Cannot expand node
Trying to convert node to libcall
Created tailcall: t15: ch,glue = RISCVISD::TAIL t12,
  TargetExternalSymbol:i64'__muldi3' [TF=1], ...
Successfully converted node to libcall
 ... replacing: t5: i64 = mul t2, t4
     with:      t15: ch,glue = RISCVISD::TAIL ... '__muldi3' ...
```

最终汇编：

```asm
mul:
	tail	__muldi3
```

加上 `-mattr=+m` 之后：

```asm
mul:
	mul	a0, a0, a1
	ret
```

同一个 IR `mul`，在两个 feature 配置下走的是完全不同的路径：

- 没有 `M`：`getOperationAction(MUL, i64)` 不是 `Legal`，先尝试 expand，失败后转 libcall。
- 有 `M`：`MUL` 是 legal operation，直接进入 pattern matching，选成 `MUL`。

这对调度的影响是结构性的：libcall 版本有调用边界、有 `ra` 保存/恢复、有参数寄存器设置；`MUL` 版本只有一条数据指令。两者的 scheduling region 都不一样。

### 没有 `Zbb`：`ctpop` 展开成位运算

`ctpop` 在没有 `Zbb` 时也不是 libcall，而是被展开成一段 shift/mask/add 序列。日志：

```text
Legalizing: t3: i64 = ctpop t2
Trying to expand node
Successfully expanded node
 ... replacing: t3: i64 = ctpop t2
     with:      t33: i64 = srl t31, Constant:i64<56>
```

最终汇编（节选）：

```asm
ctpop:
	lui	a1, 349525
	addi	a1, a1, 1365
	lui	a2, 209715
	slli	a3, a1, 32
	add	a1, a1, a3
	srli	a3, a0, 1
	addi	a2, a2, 819
	and	a1, a3, a1
	...
	mul	a0, a0, a1
	srli	a0, a0, 56
	ret
```

加上 `+zbb` 之后：

```asm
ctpop:
	cpop	a0, a0
	ret
```

这里有三层信息：

1. `ctpop` 在 RISC-V 基线不是一条指令，operation legalization 选择 Expand。
2. 展开后的序列里有 `mul`，所以它又依赖 `M` 扩展；没有 `M` 时 `mul` 还会继续变成 libcall。
3. 有 `Zbb` 时，`ctpop` 被 custom legalize 成 `CPOP`，序列长度从二十多条变成一条。

`LegalizeDAG` 的日志会明确告诉你走了哪条路：

- `Legal node: nothing to do`：operation 已经合法。
- `Trying to expand node` + `Successfully expanded node`：展开成多条指令。
- `Trying to convert node to libcall` + `Successfully converted node to libcall`：降级成函数调用。
- `Trying custom legalization`：走 target 的 `ReplaceNodeResults` / `LowerOperation`。

## 底层机制

### 阶段调度：`CodeGenAndEmitDAG`

`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp` 的 `SelectionDAGISel::CodeGenAndEmitDAG()`（约 942 行）是这条流水线的主函数。关键调用：

| 行号 | 调用 | 作用 |
|---|---|---|
| 约 983 行 | `CurDAG->Combine(BeforeLegalizeTypes, ...)` | combine #1 |
| 约 1005 行 | `CurDAG->LegalizeTypes()` | 类型合法化 |
| 约 1029 行 | `CurDAG->Combine(AfterLegalizeTypes, ...)` | combine #2 |
| 约 1083 行 | `CurDAG->Combine(AfterLegalizeVectorOps, ...)` | 向量操作合法化之后的 combine |
| 约 1103 行 | `CurDAG->Legalize()` | 操作合法化 |
| 约 1123 行 | `CurDAG->Combine(AfterLegalizeDAG, ...)` | combine #3 |
| 约 1139 行 | `DoInstructionSelection()` | pattern matching |

注意 `LegalizeVectors()` 和 `LegalizeTypes()` 可能被调用两次：一次在向量操作合法化之前，一次在之后。标量函数通常只走一条直线，但读源码时不要假设只有一次。

### DAG combine：`DAGCombiner`

`llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp`：

- `DAGCombiner::Run(CombineLevel AtLevel)`（约 1863 行）：设置 `LegalTypes`、`LegalOperations`、`LegalDAG` 三个开关，然后遍历 worklist。
- `visit(SDNode *N)`：按 opcode 分派到 `visitADD`、`visitMUL`、`visitLOAD`、`visitSTORE` 等。
- `CombineTo(...)`（约 959 行）：把旧节点替换成新节点，并把新节点加回 worklist。
- `DisableGenericCombines`（约 278 行）：`-combiner-disabled` 和 target 的 `disableGenericCombines` 都会设置它；它只影响 generic combine。

target 自己的 combine 在：

- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp` 的 `RISCVTargetLowering::PerformDAGCombine`（约 25344 行）。
- `performMULCombine`（约 21195 行）→ `expandMul`（约 20997 行）→ `expandMulToAddOrSubOfShl`（约 20889 行）。
- `expandMul` 的先决条件里有一条：`(!DCI.isBeforeLegalize() && !DCI.isCalledByLegalizer()) || !Subtarget.hasStdExtZmmul()`。这解释了为什么关了 generic combine 之后，target combine 仍然会在合法化之后展开常量乘。

### 类型合法化：`LegalizeTypes`

`llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp` 和 `LegalizeIntegerTypes.cpp`：

- 入口是 `SelectionDAG::LegalizeTypes()`。
- 每个节点走 `LegalizeOp`，根据 `TargetLowering::getTypeAction` 选择 Promote/Expand/Soften/Scalarize/Split。
- `-debug-only=legalize-types` 的 `Legalizing node:` / `Promote integer result:` / `Replacing:` 就是这条路径。

一个容易忽略的点：类型合法化不是「把 IR 类型改掉」，而是「在 DAG 里插入 extend/truncate 并替换节点」。所以 combine #2 才有活干：它要把这些 extend/truncate 清理掉。

### 操作合法化：`LegalizeDAG`

`llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp`：

- `SelectionDAGLegalize::LegalizeOp(SDNode *Node)`（约 983 行）：根据 `getOperationAction` 分派。
- `ExpandNode(SDNode *Node)`（约 3183 行）：把节点展开成多条指令。
- 如果 expand 失败，进入 `ConvertNodeToLibcall`。
- target 可以通过 `setOperationAction` 把某个 operation 标成 `Custom`，然后走 `LowerOperation` 或 `ReplaceNodeResults`。

RISC-V 的 `setOperationAction` 调用在 `RISCVISelLowering.cpp` 的构造函数附近（约 276 行起）。读某个 operation 的行为时，先在这里搜它的 action，再去 `LowerOperation` 找实现。

## 这些改写如何改变调度

把本章和 Part 1 的主题接起来：

1. **节点数量变了**：`store(load)` 删除后，少一个内存节点和一条 chain 边。scheduler 的自由度变大。
2. **依赖链长度变了**：`mul x, 2` 变成 `shl`，从一条多周期乘法变成一条单周期移位；关键路径缩短。
3. **指令形状变了**：`i128 add` 变成五条指令的进位链；scheduler 必须把 `sltu` 放在两个 `add` 之间。
4. **调用边界变了**：无 `M` 的 `mul` 变成 libcall，基本块被调用切分，scheduling region 变小。
5. **register pressure 变了**：`i8`/`i16` 提升到 `i64` 会占用完整 GPR；`i128` 拆分需要两个寄存器。这些都会影响 pre-RA scheduler 的 pressure 判断。

所以「选指令」和「排指令」不是两个独立阶段。**legalize 和 combine 已经决定了 scheduler 能看到的 DAG 形状**。这也是为什么面试里问「为什么这条 `add` 能提前」，有时答案不在 scheduler，而在前面的 combine 把一条边删掉了。

## 踩雷集锦

1. **把 `Initial selection DAG` 当成 IR**：它已经经过 SelectionDAGBuilder 的 lowering：GEP 变成 `add`、参数变成 `CopyFromReg`、调用变成 `callseq_start/CALL/callseq_end`。要看原始 IR，回 `.ll` 文件。

2. **以为 `-combiner-disabled` 会保留 IR 形状**：它只关 generic combine，target combine 仍然会跑。`mul x, 2` 会变成 `4x - 2x`，不是原样。

3. **以为 `i8` 加法一定用 8 位指令**：RISC-V RV64 没有 8 位 GPR 运算；`i8`/`i16` 被 Promote 到 `i64`，最终汇编是 64 位 `add`。

4. **以为 `i32` 和 `i64` 只是位宽不同**：`i32` 是合法类型，但运算要用 `addw`/`sllw` 等 W 形式，结果要符号扩展。类型合法不等于指令相同。

5. **以为 `mul` 没有 `M` 就一定会 libcall**：常量乘可能被 target combine 展开成 shift/add 序列；只有变量乘或无法展开的情况才走 `__muldi3`。

6. **把 `Optimized lowered` 和 `Optimized legalized` 搞混**：前者是 combine #1 之后，后者是 combine #3 之后。两者的节点形状可能完全不同。

7. **只看 `-debug-only=isel` 不看 `-debug-only=dagcombine`**：阶段 dump 只告诉你「变成了什么」，combine 日志才告诉你「哪条规则做的」。

8. **在 `-O0` 下期待同样的 combine**：`-O0` 仍然会跑 SelectionDAG 和合法化，但 IR 层的优化少了，进入 DAG 的形状不同；不要拿 `-O0` 的 DAG 解释 `-O2` 的汇编。

## 动手练习

1. 用 `combine.ll` 分别跑 `-O2`、`-O2 -combiner-disabled`、`-O2 -mattr=+m` 和无 `M`，记录 `mul2`、`storeload` 的汇编差异。解释每一处差异是 generic combine、target combine 还是 operation legalization 造成的。

2. 把 `mul2` 改成 `mul i64 %a, 3`、`5`、`9`，分别在无 `Zba` 和有 `Zba` 下跑。哪些常量变成 `sh1add/sh2add/sh3add`？哪些仍然走 `mul`？用 `-debug-only=dagcombine` 找 `expandMulToShlAddShlAdd` 的痕迹。

3. 用 `types.ll` 跑 `-debug-only=legalize-types`，找出 `i8`、`i16`、`i32`、`i128` 各自的 legalization action。把 `add128` 改成 `sub128`，进位链会怎么变？

4. 用 `legal.ll` 跑 `-mattr=`、`-mattr=+m`、`-mattr=+m,+zbb`，记录 `mul`、`ctpop`、`bswap` 的输出。用 `-debug-only=legalizedag` 找出 `Successfully expanded node` 和 `Successfully converted node to libcall` 各出现几次。

5. 写一个 `i16` 的 `sdiv`，在无 `M` 和有 `M` 下对比。`sdiv` 是直接选成 `DIV`，还是走 libcall？为什么和 `mul` 不一样？

6. **guided**：在 `RISCVISelLowering.cpp` 的 `PerformDAGCombine` 里加一条 `dbgs()`，打印进入 `performMULCombine` 的常量。重新构建后跑 `mul x, 2`，确认 generic combine 和 target combine 的先后顺序。不要提交这条调试代码。

## 本章重点整理

- SelectionDAG 在 pattern matching 之前经历三次 combine、类型合法化、操作合法化；`-debug-only=isel` 的阶段标题就是这条流水线的路标。
- DAG combine 改变算术形状：`mul x, 2` → `shl x, 1`，`store(load(p))` → `load(p)`。
- 类型合法化改变位宽：`i8`/`i16` Promote 到 `i64`，`i128` Expand 成两个 `i64`；`i32` 合法但使用 W 形式指令。
- 操作合法化决定「有没有这条指令」：无 `M` 的变量 `mul` 走 `__muldi3`，无 `Zbb` 的 `ctpop` 展开成位运算序列。
- `-combiner-disabled` 只关 generic combine；target combine 仍会运行，所以不能把它当成「IR 原样」。
- 这些改写直接改变 scheduler 的输入：节点数、chain 边、依赖链长度、调用边界、寄存器压力。

## 自我检核

- [ ] 能按顺序说出六个 DAG 阶段，以及每个阶段对应的 dump 标题
- [ ] 能解释 `mul x, 2` 为什么在汇编里变成 `slli`，并指出是哪个阶段做的
- [ ] 能解释 `store(load(p))` 为什么被删除，以及删除后 chain 边怎么变
- [ ] 能解释 `i8` 加法为什么变成 64 位 `add`，`i32` 为什么用 `addw`
- [ ] 能解释 `i128` 加法的进位链，以及它对调度的影响
- [ ] 能说出 `mul` 在无 `M` 时的两条路径：target combine 展开 vs libcall
- [ ] 能解释 `-combiner-disabled` 为什么不会保留 IR 形状
- [ ] 能说出 `getTypeAction` 和 `getOperationAction` 的区别

## 延伸阅读

### 官方文档

- **[LLVM Code Generator — SelectionDAG](https://llvm.org/docs/CodeGenerator.html#selectiondag)**
  - **读哪里**：SelectionDAG 的构建、合法化、选择流程。
  - **和本章的关联**：官方对阶段的定义；本章用真实 dump 把它落到 RISC-V。

- **[LLVM TableGen — Instruction Selection](https://llvm.org/docs/TableGen/BackendGuides.html)**
  - **读哪里**：`Pat`、`PatFrag`、`ComplexPattern` 的语义。
  - **和本章的关联**：本章讲的是 pattern 之前的 DAG 形状；下一章讲 pattern 如何匹配这张 DAG。

- **[LLVM Language Reference — undef and poison](https://llvm.org/docs/LangRef.html#undefined-values)**
  - **读哪里**：`undef`、`poison` 的语义。
  - **和本章的关联**：`expandMul` 的注释明确提到它在 `undef` 语义上做了「 knowingly incorrect」的假设；理解这一点能看懂 target combine 的取舍。

### 源码

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`**
  - **读哪里**：`CodeGenAndEmitDAG()`（约 942 行）的调用顺序。
  - **和本章的关联**：三次 combine 和两次合法化的主调度。

- **`llvm/lib/CodeGen/SelectionDAG/DAGCombiner.cpp`**
  - **读哪里**：`DAGCombiner::Run()`（约 1863 行）、`visitMUL`、`visitSTORE`、`CombineTo()`。
  - **和本章的关联**：`mul → shl`、`store(load) → load` 的具体实现。

- **`llvm/lib/CodeGen/SelectionDAG/LegalizeDAG.cpp`**
  - **读哪里**：`SelectionDAGLegalize::LegalizeOp()`（约 983 行）、`ExpandNode()`（约 3183 行）。
  - **和本章的关联**：`mul` 的 expand/libcall 决策，`ctpop` 的展开。

- **`llvm/lib/CodeGen/SelectionDAG/LegalizeTypes.cpp`**
  - **读哪里**：`SelectionDAG::LegalizeTypes()`、`DAGTypeLegalizer::PromoteIntegerResult`。
  - **和本章的关联**：`i8`/`i16` 提升、`i128` 拆分。

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**
  - **读哪里**：`PerformDAGCombine()`（约 25344 行）、`performMULCombine()`（约 21195 行）、`expandMul()`（约 20997 行）、构造函数里的 `setOperationAction`（约 276 行起）。
  - **和本章的关联**：RISC-V 特有的常量乘展开和 operation action。

### 演讲 / 博客

- **"DAG Combine and Legalization in LLVM"**（LLVM Developers' Meeting 相关演讲）
  - **这讲什么**：DAG combine 的工作list、合法化阶段的顺序。
  - **为什么值得看**：先建立阶段感，再读 `DAGCombiner.cpp` 会轻松很多。

- **"Instruction Selection in LLVM"**（LLVM 后端入门演讲）
  - **这讲什么**：从 IR 到 SelectionDAG 再到 MachineInstr 的全流程。
  - **和本章的关联**：把本章的六个阶段放回整条后端流水线。

下一章进入 pattern matching：DAG 已经被改写好了，现在要把它匹配成一条条 MachineInstr。你会看到 `.td` 里的 `Pat` 如何变成 matcher table，`-debug-only=isel` 的 `Match failed` / `Skipped scope` 在说什么，以及 `SH3ADD` 是怎么从 `(add (shl x, 3), y)` 里被匹配出来的。

→ [Ch 6 Pattern matching：从 `.td` 到 `Select()`](./06-pattern-matching.md)
