# Ch 4 — SelectionDAG 的数据结构：chain、glue、token

> **目标**：看懂 SelectionDAG 里的四种「边」：数据依赖、chain、glue、以及 `TokenFactor` 合并出来的顺序约束。学完这章，你能解释为什么 `add` 可以提前、`ld` 不能随便越过 `sd`、以及 `CopyToReg` 和 `RET_GLUE` 为什么必须粘在一起。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/builds/explore`，commit `700e551714cc`。DAG dump 用 `-view-dag-combine1-dags -no-open-dag-viewer`，文本 DAG 用 `-debug-only=isel`。

## 为什么需要这个？

SelectionDAG 不是「把 IR 指令换成 DAG 节点」这么简单。它还要表达三类约束：

1. **数据依赖**：`add` 要等两个操作数算好。
2. **内存/控制顺序**：`store` 不能随便越过 `load`，`call` 不能随便越过 `fence`。
3. **原子组**：某些指令必须一起发射，不能插到中间。

如果 DAG 只有数据依赖，scheduler 就会把 `store` 提前、把 `CopyToReg` 拆开，生成错误代码。`chain` 和 `glue` 就是 LLVM 用来表达「顺序」和「原子组」的机制。

面试里常见的追问：

- 「为什么这条 `add` 能提到 load 前面，而 store 不能？」
- 「`TokenFactor` 是什么？它和 data dependency 有什么区别？」
- 「glue 是什么？为什么 `RET_GLUE` 的操作数里有一个 `glue`？」

这章用真实的 DAG dump 回答。

## 先建立直觉

把 DAG 想成一张有向图，节点是指令/值，边有三种颜色：

```
数据边（实线）      :  value 被谁使用
chain 边（蓝色虚线）:  「先做我，再做它」的顺序令牌
glue 边（红色粗线） :  「我们是一个原子组，不能拆」
```

`chain` 不是值。它是一条「令牌」：`load` 接收一个 chain，产生一个新的 chain；`store` 接收 chain，产生新的 chain。这样 scheduler 就知道「这个 store 必须在这个 load 之后」。

`glue` 也不是普通值。它把多个节点粘成一个调度单元，典型场景是：

```
CopyToReg (ch, glue)
   └─ glue ──► CALL
```

`CALL` 必须紧跟在 `CopyToReg` 之后，因为参数寄存器已经设好，不能把别的指令插进来改寄存器。

## DAG 节点长什么样

一个 `SDNode` 有：

- **opcode**：`ISD::ADD`、`ISD::LOAD`、`RISCVISD::SELECT_CC`、`TargetOpcode::COPY` 等。
- **结果类型列表**：`SDVTList`，例如 `{i64, ch}`。
- **操作数**：`SDValue` 数组，每个 `SDValue` 指向一个 `SDNode` 的某个结果。
- **flags**：`NoFPExcept`、`NoUnsignedWrap` 等。

文本 DAG 里的一条：

```text
t7: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
```

读法：

- `t7` 是节点编号。
- 两个结果：`i64`（load 的值）和 `ch`（新的 chain）。
- 操作数：`t0`（输入 chain）、`t2`（地址）、`poison`（偏移）。
- `<...>` 是 MemoryVT/MemOperand 信息，不是操作数。

再看：

```text
t11: ch,glue = CopyToReg t0, Register:i64 $x10, t8
```

- 结果：`ch` 和 `glue`。
- 操作数：`t0` chain、`Register $x10`、`t8` 值。
- 它产生一个新的 chain 和一个 glue，供 `RET_GLUE` 使用。

## 真实 DAG：`idx` 的 dot 文件

生成 dot：

```bash
LLC=/home/ubuntu/llvm/builds/explore/bin/llc
mkdir -p /tmp/dags
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+zba -O2 \
  -view-dag-combine1-dags -no-open-dag-viewer \
  -dag-file-location=/tmp/dags idx.ll -o /dev/null
```

本机实测输出：

```text
Writing '/tmp/dags/dag.idx-13de0f.dot'...  done.
Not opening graph viewer program as per options.
```

dot 的关键部分：

```dot
Node0x... [label="{EntryToken|t0|{<d0>ch|<d1>glue}}"];
Node0x... [label="{{<s0>0|<s1>1}|shl|t6|{<d0>i64}}"];
Node0x... [label="{{<s0>0|<s1>1}|add|t7|{<d0>i64}}"];
Node0x... [label="{{<s0>0|<s1>1|<s2>2}|load\<(load (s64) from %ir.q)\>|t10|{<d0>i64|<d1>ch}}"];
Node0x... [label="{{<s0>0|<s1>1}|CopyToReg|t12|{<d0>ch|<d1>glue}}"];
Node0x... [label="{{<s0>0|<s1>1|<s2>2}|RISCVISD::RET_GLUE|t13|{<d0>ch}}"];

Node...:s0 -> Node...:d0 [color=blue, style=dashed];   // chain
Node...:s1 -> Node...:d1 [color=red, style=bold];      // glue
```

读图：

- `EntryToken` 是根，带 `ch` 和 `glue`。
- `shl` / `add` 只有数据边（黑色实线），没有 chain。
- `load` 有 `ch` 输入和 `ch` 输出，所以它在 chain 上。
- `CopyToReg` 产生 `ch,glue`；`RET_GLUE` 用红色粗线接它的 glue。
- 蓝色虚线是 chain：`EntryToken → CopyToReg → RET_GLUE`。
- 红色粗线是 glue：`CopyToReg → RET_GLUE`，表示它们是一个调度单元。

**关键结论**：`shl`/`add` 没有 chain，所以 scheduler 可以自由移动；`load` 有 chain，所以不能随便越过其他内存操作；`CopyToReg`/`RET_GLUE` 有 glue，所以不能拆开。

## 真实 DAG：`TokenFactor` 合并两条 load 链

`idx` 只有一个 load，看不出 `TokenFactor`。构造一个有两个 load 再调用函数的例子：

```llvm
target triple = "riscv64-unknown-elf"

declare void @use(i64, i64)

define void @call2(ptr %p, ptr %q) {
  %a = load i64, ptr %p
  %b = load i64, ptr %q
  call void @use(i64 %a, i64 %b)
  ret void
}
```

用 `-debug-only=isel` 看初始 DAG：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -O2 -debug-only=isel tf2.ll -o /dev/null
```

实测（节选）：

```text
Initial selection DAG: %bb.0 'call2:'
SelectionDAG has 22 nodes:
  t0: ch,glue = EntryToken
  t5: i64 = Constant<0>
    t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t7: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
    t4: i64,ch = CopyFromReg t0, Register:i64 %1
  t8: i64,ch = load<(load (s64) from %ir.q)> t0, t4, poison:i64
  t9: i64 = GlobalAddress<ptr @use> 0
      t10: ch = TokenFactor t7:1, t8:1
    t12: ch,glue = callseq_start t10, TargetConstant:i64<0>, TargetConstant:i64<0>
  t14: ch,glue = CopyToReg t12, Register:i64 $x10, t7
  t16: ch,glue = CopyToReg t14, Register:i64 $x11, t8, t14:1
  t19: ch,glue = RISCVISD::CALL t16, TargetGlobalAddress:i64<ptr @use> 0 [TF=1], ...
    t20: ch,glue = callseq_end t19, TargetConstant:i64<0>, TargetConstant:i64<0>, t19:1
  t21: ch = RISCVISD::RET_GLUE t20
```

这里能看到完整的故事：

1. `t7` 和 `t8` 是两个 load，各自产生 `i64` 和 `ch`。
2. `t10: ch = TokenFactor t7:1, t8:1` 把两条 chain 合并成一条。
3. `callseq_start` 接收 `t10`，所以调用必须等两个 load 都完成。
4. `CopyToReg` 把 `t7`、`t8` 放进 `a0`、`a1`；第二个 `CopyToReg` 用 `t14:1` glue，表示它和第一个粘在一起。
5. `CALL` 用 `t16:1` glue，表示参数设置和调用是一个原子组。
6. `callseq_end` 和 `RET_GLUE` 继续 chain。

`TokenFactor` 的意义不是「并行」，而是「这些 chain 都完成后，才继续」。它让两个 load 可以并行/重排，但它们的**共同后继**必须等两者都完成。

## 文本 DAG 的读法：缩进、编号、结果类型

dot 文件适合看全局，真正调试时更常用的是 `-debug-only=isel` 打出的文本 DAG。它比 dot 更容易 grep，也更接近 `SDNode` 的内存结构。用 `min.ll` 跑一次：

```bash
"$LLC" -mtriple=riscv64 -mcpu=generic -mattr=+m -O2 \
  -debug-only=isel min.ll -o /dev/null
```

本机实测的初始 DAG（节选，保留缩进）：

```text
Initial selection DAG: %bb.0 'f:'
SelectionDAG has 25 nodes:
  t0: ch,glue = EntryToken
  t2: i64,ch = CopyFromReg t0, Register:i64 %0
  t11: i64 = Constant<0>
          t10: i64,ch = CopyFromReg t0, Register:i64 %4
          t6: i64,ch = CopyFromReg t0, Register:i64 %2
        t19: i64 = add t10, t6
          t13: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
          t4: i64,ch = CopyFromReg t0, Register:i64 %1
        t14: i64 = mul t13, t4
      t20: i64 = add t19, t14
          t16: i64 = add t2, Constant:i64<8>
        t17: i64,ch = load<(load (s64) from %ir.p1)> t0, t16, poison:i64
        t8: i64,ch = CopyFromReg t0, Register:i64 %3
      t18: i64 = mul t17, t8
    t21: i64 = add t20, t18
  t23: ch,glue = CopyToReg t0, Register:i64 $x10, t21
  t24: ch = RISCVISD::RET_GLUE t23, Register:i64 $x10, t23:1
```

逐条读：

| 行 | 读法 |
|---|---|
| `t0: ch,glue = EntryToken` | 图的根。它同时提供 `ch` 和 `glue` 两个结果，后面所有「没有前置顺序」的节点都可以接它。 |
| `t2: i64,ch = CopyFromReg t0, Register:i64 %0` | 读 IR 参数 `%0`（也就是 `%p`）到虚拟寄存器。结果有两个：`i64` 是值，`ch` 是新的 chain。操作数 `t0` 是输入 chain。 |
| `t19: i64 = add t10, t6` | 纯数据节点。它只有 `i64` 一个结果，没有 `ch`，因此 scheduler 可以把它移动到任何「两个操作数都可用」的位置。 |
| `t13: i64,ch = load<...> t0, t2, poison:i64` | 操作数依次是输入 chain `t0`、地址 `t2`、偏移 `poison`。尖括号里的 `Mem:(load (s64) from %ir.p)` 是内存描述，不是操作数。 |
| `t16: i64 = add t2, Constant:i64<8>` | 地址计算本身是纯数据节点；它没有 chain，所以可以和 load 分开调度。 |
| `t17: i64,ch = load<...> t0, t16, poison:i64` | 第二个 load 的输入 chain 也是 `t0`，不是 `t13:1`。说明它和第一个 load 之间没有顺序约束。 |
| `t23: ch,glue = CopyToReg t0, Register:i64 $x10, t21` | 把返回值写进物理寄存器 `$x10`（`a0`）。它产生 `ch,glue`，供返回节点使用。 |
| `t24: ch = RISCVISD::RET_GLUE t23, Register:i64 $x10, t23:1` | target-specific 返回节点。第三个操作数 `t23:1` 是 glue，表示它和 `CopyToReg` 必须一起发射。 |

三条容易忽略的规则：

1. **`tN` 是节点编号，不是执行顺序。** printer 按拓扑序输出，但同一拓扑层内可以换序；真正决定执行顺序的是 scheduler 和 chain。
2. **缩进是 printer 的排版，不是语法。** 一个节点会在第一次被使用之前打印；缩进深度表示它离当前使用点的距离。读 DAG 时不要数缩进，直接看 `tN` 的操作数。
3. **一个节点可以有多个结果。** `load` 同时产生值和 chain；`CopyToReg` 同时产生 chain 和 glue；`EntryToken` 同时产生 chain 和 glue。结果用 `tN:0`、`tN:1` 引用，文本里常写成 `t7:1`。

## 为什么多个 `CopyFromReg` 都接 `EntryToken`

`min.ll` 里五个 `CopyFromReg`（`%0` 到 `%4`）的输入 chain 都是 `t0`。这不是 printer 省略，而是 DAG 真的没有给它们之间加顺序边。

原因：函数入口读参数寄存器，彼此之间没有内存或控制依赖。`%p` 在 `$x10`、`%a` 在 `$x11`，读哪个先读哪个后不影响结果。把它们都接 `EntryToken`，scheduler 就能自由排列，甚至把某个 `CopyFromReg` 推迟到第一次使用前。

但这条规则有边界：

- 如果读物理寄存器之前有一个可能 clobber 它的调用、内联汇编或 `setjmp`，`SelectionDAGBuilder` 会把这个读接到相应的 chain 后面，而不是 `EntryToken`。
- `CopyFromReg` 产生的新 chain 主要用于「读寄存器」这件事本身的顺序；它不会自动给后面的纯数据节点加约束。
- 参数寄存器的 `CopyFromReg` 在寄存器分配后通常会被消掉或合并成 `COPY`，所以最终汇编里看不到它们。

这就是为什么在 `-print-before=machine-scheduler` 里能看到一串 `COPY $x14`、`COPY $x13`，而在最终汇编里它们大多消失。DAG 层的 `CopyFromReg` 是「读入参数」的抽象，不是一条一定要发射的指令。

## alias analysis：什么时候两条内存操作可以共享 `EntryToken`

chain 不是「所有内存操作一律串行」。如果 alias analysis 能证明两个内存操作不别名，它们可以共享同一个输入 chain，顺序由 scheduler 决定；最后再用 `TokenFactor` 把分支合并。

构造一个对比：

```llvm
target triple = "riscv64-unknown-elf"

define i64 @alias(ptr %p, ptr %q, i64 %v) {
  %a = load i64, ptr %p
  store i64 %v, ptr %q
  %b = load i64, ptr %p
  %s = add i64 %a, %b
  ret i64 %s
}

define i64 @noalias(ptr noalias %p, ptr noalias %q, i64 %v) {
  %a = load i64, ptr %p
  store i64 %v, ptr %q
  %b = load i64, ptr %p
  %s = add i64 %a, %b
  ret i64 %s
}
```

先看 `@alias` 的 DAG（`-debug-only=isel`，节选）：

```text
t9: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
t10: ch = store<(store (s64) into %ir.q)> t9:1, t6, t4, poison:i64
t11: i64,ch = load<(load (s64) from %ir.p)> t10, t2, poison:i64
```

读法：

- 第一个 load 接 `t0`。
- store 的输入 chain 是 `t9:1`，所以它必须等第一个 load。
- 第二个 load 的输入 chain 是 `t10`，所以它必须等 store。

这是一条严格的串行链：`load → store → load`。即使第二个 load 和 store 可能不别名，编译器在 `@alias` 里没有证据，只能保守处理。

再看 `@noalias` 经过 DAG combine 后的 DAG：

```text
t9: i64,ch = load<(load (s64) from %ir.p)> t0, t2, poison:i64
t17: ch = store<(store (s64) into %ir.q)> t0, t6, t4, poison:i64
t18: ch = TokenFactor t9:1, t17
t12: i64 = add t9, t9
```

变化有三处：

1. store 的输入 chain 也变成 `t0`，和 load 并列，没有 `load → store` 的边。
2. 第二个 load 消失了，`add` 直接使用 `t9` 两次。这是 GVN/CSE 在 IR 层或 DAG 层发现「两次读同一地址，中间没有写 `%p`」的结果；`noalias` 让 store 不被认为可能写 `%p`。
3. 返回值之前用 `TokenFactor t9:1, t17` 把 load 和 store 的 chain 合并，保证 `RET_GLUE` 等两者都完成。

最终汇编的差异：

```asm
# @alias
ld   a3, 0(a0)
sd   a2, 0(a1)
ld   a0, 0(a0)
add  a0, a3, a0
ret

# @noalias
ld   a0, 0(a0)
add  a0, a0, a0
sd   a2, 0(a1)
ret
```

`@alias` 里第二条 `ld` 不能越过 `sd`；`@noalias` 里不仅 `ld` 可以提前，第二条 `ld` 还被 CSE 掉了。这个例子同时说明两件事：

- **chain 决定 scheduler 的自由度**：有 `load → store → load` 的边，第二条 load 就不能提前。
- **alias analysis 决定 chain 的形状**：`noalias` 是 IR 层的承诺，DAG builder 和 combine 依据它决定要不要加边。

注意：`noalias` 不是「优化提示」，而是程序必须满足的语义约束。如果 `%p` 和 `%q` 实际指向同一块内存，`@noalias` 的行为是未定义。面试里如果被问「为什么这里编译器敢把 store 排到 load 后面」，答案就是「因为 IR 给了 `noalias`，违反它是用户的错」。

## chain 的三种来源

### 1. 内存操作

`load`/`store` 接收 chain、产生 chain。它们之间的顺序由 chain 决定。如果 alias analysis 认为两个内存操作不别名，DAG 可能让它们共享同一个输入 chain，而不是串行。

### 2. 物理寄存器

`CopyFromReg` / `CopyToReg` 用 chain 表示「读/写物理寄存器」的顺序。它们还产生 glue，用于把参数设置和调用粘在一起。

### 3. 控制流

`brcond`、`ret`、`call`、`fence` 等控制节点也带 chain。`RET_GLUE` 是一个典型的 target-specific 控制节点，它接收返回值的 glue。

## glue 和 chain 的区别

| 维度 | chain | glue |
|---|---|---|
| 是不是值 | 是 `MVT::Other` 的令牌 | 是特殊的 glue 值 |
| 作用 | 顺序约束，允许中间插入独立指令 | 原子组，节点必须一起发射 |
| 典型节点 | `load`、`store`、`callseq_start` | `CopyToReg`、`RET_GLUE`、`CALL` |
| 调度器怎么看 | 建 `Order`/`Memory` 依赖边 | 建 `Glue` 依赖边，不能被拆 |
| 例子 | `t10: ch = TokenFactor t7:1, t8:1` | `t21: ch = RET_GLUE t20` |

`chain` 可以「穿过」独立指令：`load` 和 `store` 之间可以插 `add`，只要 `add` 不访问内存。`glue` 不行：`CopyToReg` 和 `CALL` 之间不能插任何东西。

## 源码落点

- **`llvm/include/llvm/CodeGen/SelectionDAGNodes.h`**
  - `SDNode`、`SDValue`、`SDVTList`、`ISD::NodeType`。
  - `ISD::TokenFactor`、`ISD::EntryToken` 的定义。
  - **读哪里**：`class SDNode` 的结果/操作数接口；`ISD::NodeType` 里 `TokenFactor`、`EntryToken`、`CopyToReg`、`CopyFromReg`。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp`**
  - `updateRoot()`（约 1139 行）：把 `PendingLoads` 合并成 `TokenFactor`。
  - `RegsForValue::getCopyToRegs()`（约 1000–1040 行）：多个 `CopyToReg` 的 glue/chain 处理。
  - **和本章的关联**：`call2` 的 `TokenFactor` 就是 `updateRoot()` 生成的。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - `AddSchedEdges()`（约 441 行）：把 chain/glue 变成 SUnit 的依赖边。
  - `BuildSchedGraph()`（约 543 行）：建调度图。
  - 约 626 行：`TokenFactor` 的 latency 处理。
  - **和本章的关联**：chain/glue 最终变成 scheduler 的 `Order`/`Glue` 边。

## 踩雷集锦

1. **把 chain 当成普通数据值**：chain 的类型是 `MVT::Other`，不能参与算术。它只表达顺序，不表达「值」。

2. **以为 `TokenFactor` 表示并行执行**：它只是把多个 chain 合并成一个「全部完成」的令牌。被合并的节点可以重排，但共同后继要等全部完成。

3. **以为 glue 是数据依赖**：glue 不传递值，只传递「不可拆分」的约束。`CopyToReg` 的 glue 不是参数值，是调度约束。

4. **只看最终汇编，不看 DAG**：最终汇编里 chain/glue 已经被 scheduler 消费掉了。要理解顺序，必须看 `-debug-only=isel` 或 dot。

5. **`-view-dag-combine1-dags` 会尝试打开图形窗口**：在无 GUI 环境加 `-no-open-dag-viewer`，否则会报错或卡住。dot 文件仍然会写出来。

6. **dot 文件里的节点编号每次不同**：`Node0x...` 是内存地址，不是稳定 ID。看 `t7`、`t8` 这种 `tN` 编号，或者看 label 里的 opcode。

## 动手练习

1. 用 `idx.ll` 生成 dot，找出：
   - 哪个节点是 `EntryToken`；
   - 哪条边是 chain（蓝色虚线）；
   - 哪条边是 glue（红色粗线）；
   - `SH3ADD` 在 dot 里长什么样。

2. 用 `tf2.ll` 跑 `-debug-only=isel`，找出 `TokenFactor`。把两个 load 改成三个 load，`TokenFactor` 的操作数会怎么变？

3. 把 `call2` 的 `call void @use(i64 %a, i64 %b)` 改成 `call void @use(i64 %a, i64 %a)`，看 `TokenFactor` 和 `CopyToReg` 是否变化。为什么？

4. 在 `mem.ll` 里加一条 `fence seq_cst`，重新生成 DAG。`fence` 是否产生新的 chain 节点？它和 store/load 的 chain 怎么连？

5. 把 `idx.ll` 的 `load` 改成 `volatile load`，再看 DAG。`volatile` 会改变 chain 的数量吗？会改变 scheduler 的自由度吗？

## 本章重点整理

- `SDNode` 是 DAG 节点，`SDValue` 指向节点的某个结果；结果类型列表可以同时包含 `i64`、`ch`、`glue`。
- `chain` 是顺序令牌，不是值；`load`/`store`/`call` 用它表达内存和控制顺序。
- `TokenFactor` 合并多条 chain，表示「这些 chain 都完成后才继续」。
- `glue` 把节点粘成原子组，典型是 `CopyToReg → CALL`、`CopyToReg → RET_GLUE`。
- scheduler 把 chain/glue 变成 `Order`/`Memory`/`Glue` 依赖边；这是它不能随便移动指令的根本原因。

## 自我检核

- [ ] 能解释 `SDValue` 和 `SDNode` 的关系，以及一个节点为什么可以有多个结果
- [ ] 能说出 chain 和 glue 的区别，并各举一个节点例子
- [ ] 能解释 `TokenFactor` 的语义，以及它为什么不是「并行」
- [ ] 能从 `-debug-only=isel` 的 DAG 文本里找出 `EntryToken`、`CopyFromReg`、`load`、`CopyToReg`、`RET_GLUE`
- [ ] 能解释为什么 `add` 可以跨过 `load`，但 `store` 不能
- [ ] 知道 dot 文件里蓝虚线、红粗线分别代表什么

## 延伸阅读

### 官方文档

- **[LLVM Code Generator](https://llvm.org/docs/CodeGenerator.html)**
  - **读哪里**：「SelectionDAG」小节的节点、chain、glue 说明。
  - **和本章的关联**：官方对 DAG 结构的定义；本章的 dot 例子是它的实例。

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`SDValue`、`SDNode`、`ilist` 相关段落。
  - **和本章的关联**：理解 LLVM 的节点/值所有权模型。

### 源码

- **`llvm/include/llvm/CodeGen/SelectionDAGNodes.h`**
  - **读哪里**：`class SDNode`、`class SDValue`、`ISD::NodeType` 的 `TokenFactor`/`EntryToken`。
  - **和本章的关联**：本章所有节点/边的类型定义。

- **`llvm/lib/CodeGen/SelectionDAG/SelectionDAGBuilder.cpp`**
  - **读哪里**：`updateRoot()`（约 1139 行）、`getCopyToRegs()`（约 1000 行）。
  - **和本章的关联**：`TokenFactor` 和 glue 的生成。

- **`llvm/lib/CodeGen/SelectionDAG/ScheduleDAGSDNodes.cpp`**
  - **读哪里**：`AddSchedEdges()`（约 441 行）、`BuildSchedGraph()`（约 543 行）、约 626 行。
  - **和本章的关联**：chain/glue 如何变成调度依赖。

### 博客 / 演讲

- **"SelectionDAG: A Flexible IR for Instruction Selection"**（LLVM 相关演讲/博客）
  - **这讲什么**：SelectionDAG 的设计动机、chain/glue 的作用。
  - **为什么值得看**：先理解为什么不用普通 IR 做 ISel，再看代码会顺很多。

下一章看 DAG Combine 和 Legalize：在选择之前，DAG 已经被改写了很多次。你会看到 `mul x, 2` 怎么变成 `shl`，一个 `store(load(p))` 怎么被删掉，以及为什么 `-combiner-disabled` 会让最终汇编完全变样。

→ [Ch 5 DAG Combine 与 Legalize：选择之前的战争](./05-dag-combine-and-legalize.md)
