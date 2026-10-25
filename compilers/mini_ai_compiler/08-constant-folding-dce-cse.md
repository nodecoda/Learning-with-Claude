# 第 8 章 常量折叠、DCE 与 CSE

> **目标**：读完本章，你能独立实现并解释 `constant_fold`、`dce`、`common_subexpression_elimination` 三个优化；能说清它们的迭代顺序为什么能在一趟内传播；能识别“纯函数假设”被破坏时哪一条优化会出错；能用真实 pass 记录判断每个优化改了多少；能解释为什么 `changed=0` 时图仍可能变化。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；`numpy 2.4.6`；`torch 2.14.1+cpu`。本章命令在仓库根目录执行：
>
> ```powershell
> $env:PYTHONPATH = "compilers\mini_ai_compiler\code"
> $env:PYTHONIOENCODING = "utf-8"
> & ".scratch/aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\demo_e2e.py"
> ```

## 为什么需要这三个优化

前端抓出来的图会带有三类“编译器自己制造的低效”：

1. **能静态算出的子图**。权重初始化、常量表达式、形状计算，都可以在编译期算完，不必留到运行期。常量折叠（constant folding）负责这件事。
2. **没人使用的值**。前端可能先建常量再复制/转置出真正要用的常量，原常量就成了死值；改写也可能留下孤儿链。死代码消除（DCE, dead code elimination）负责清理。
3. **重复的子表达式**。同一个权重被两条分支复用、同一个 `relu(x)` 写了两次，都会让运行期重复计算。公共子表达式消除（CSE, common subexpression elimination）负责合并。

三者都是局部的、保守的、便宜的。它们不追求最优，只追求“在不改变语义的前提下，把图变小、变简单”，为后面的融合、内存规划、代码生成减少工作量。

## 先建立直觉：三种“删除/替换”

```text
常量折叠
  c1(2)   c2(3)                 c12(5)
      \   /          ==>           |
      add                        mul(x, c12)
        \                          |
        mul(x, add)              (add 消失)

死代码消除
  x -> relu(a) -> relu(b) -> exp(c)      x -> relu(d)
  x -> relu(d)  (输出)          ==>      (a, b, c 全删)

公共子表达式消除
  x -> relu(a) --+
  x -> relu(b) --+--> add        ==>     x -> relu(a) -> add(a, a)
```

三个变换的共同点：都只改“值之间的连接”，不新增数学语义。常量折叠把一条子链压成一个常量；DCE 删掉不影响输出的链；CSE 把结构相同的两条链并成一条。

## 核心内容

### 8.1 constant_fold 的实现

完整实现如下：

```python
def constant_fold(graph: Graph, *, max_numel: int = 4096) -> int:
    """Replace operators whose inputs are all constants with constants."""
    folded = 0
    for op in list(graph.ops):
        if not op.inputs or not all(v.kind == "constant" for v in op.inputs):
            continue
        if op.output.type.numel > max_numel:
            continue
        arrays = [v.data for v in op.inputs]
        assert all(a is not None for a in arrays)
        value = apply_op(op.kind, arrays, op.attrs)
        const = graph.add_const(f"{op.output.name}_folded", value)
        graph.replace_all_uses(op.output, const)
        graph.remove_ops([op])
        folded += 1
    graph.dce()
    return folded
```

逐条读：

- `list(graph.ops)`：先做快照。循环里会 `remove_ops`，直接遍历原列表会跳元素。
- `not op.inputs`：没有输入的 op 不折。当前算子集里没有零输入算子，这一条是防御性的。如果将来加了 `rand`、`zeros`，它们不该在编译期被折成固定值。
- `all(v.kind == "constant" ...)`：只折全常量输入。部分常量（例如 `x + 1`）不属于常量折叠，属于常量传播，需要另一套分析。
- `op.output.type.numel > max_numel`：结果太大就跳过。默认 4096 个元素，防止编译期把一个大矩阵乘算一遍。
- `arrays = [v.data ...]`：直接拿常量数组。`assert` 在 `-O` 模式下会被去掉，生产代码应改成显式异常。
- `apply_op(op.kind, arrays, op.attrs)`：与解释器共用语义。这是第 5 章“唯一实现”的直接收益：折叠结果与运行期结果不会因为两套算子代码而分叉。
- `graph.add_const(f"{op.output.name}_folded", value)`：新常量命名带 `_folded` 后缀，方便在 dump 里追踪来源。
- `replace_all_uses` + `remove_ops([op])`：先把所有使用者（包括 `graph.outputs`）指向新常量，再删 op。顺序不能反。
- `graph.dce()`：折叠可能让上游常量变成死值，收尾清一次。注意这会改变 `graph.values`，即使 `folded == 0`。
- 返回 `folded`，不是删除的 op 数（这里二者相同，但语义上是“折叠次数”）。

### 8.2 正常路径：两个常量相加

```python
g = Graph()
x = g.add_input("x", (2,))
c1 = g.add_const("c1", np.array([2.0, 2.0], np.float32))
c2 = g.add_const("c2", np.array([3.0, 3.0], np.float32))
y = g.add_op("add", [c1, c2])
z = g.add_op("mul", [x, y])
g.add_output(z)

print("before ops:", [op.kind for op in g.ops])
print("folded:", constant_fold(g))
print("after ops:", [op.kind for op in g.ops])
print(g.dump())
```

本机输出：

```text
before ops: ['add', 'mul']
folded: 1 ops: ['mul']
inputs:
  x: tensor<2>
constants:
  add_0_folded: tensor<2> numel=2
ops:
  00 %mul_1 = mul(x, add_0_folded) : tensor<2>
outputs:
  mul_1: tensor<2>
```

三个细节：

- 原本的 `c1`、`c2` 消失了。`dce` 在折叠后清掉了它们，因为除了已删除的 `add` 没人使用。
- 新常量名是 `add_0_folded`，来自被折叠 op 的输出名 `add_0`。
- `mul` 保留，因为它有非常量输入 `x`。常量折叠不会把它换成常量。

### 8.3 边界路径：max_numel 的静默跳过

```python
for n in (64, 65):
    gg = Graph()
    A = gg.add_const("A", np.ones((n, 64), np.float32))
    B = gg.add_const("B", np.ones((64, 64), np.float32))
    mm = gg.add_op("matmul", [A, B])
    gg.add_output(mm)
    print(f"matmul {n}x64x64 numel={n * 64}: folded={constant_fold(gg)} ops={[op.kind for op in gg.ops]}")
```

本机输出：

```text
matmul 64x64x64 numel=4096: folded=1 ops=[]
matmul 65x64x64 numel=4160: folded=0 ops=['matmul']
```

`64 * 64 = 4096` 正好等于上限，允许折叠；`65 * 64 = 4160` 超过上限，跳过。跳过是**静默的**：不报错、不警告，`folded=0`。这符合“优化不能失败”的设计，但会让“为什么这个常量没被折”变成一个需要查源码的问题。调大 `max_numel` 前先想清楚编译时间预算。

### 8.4 一趟内的链式折叠

常量折叠会沿着 op 列表向前传播，因为 `replace_all_uses` 立即改写后续 op 的输入：

```python
g2 = Graph()
a = g2.add_const("a", np.full((2,), 2.0, np.float32))
b = g2.add_const("b", np.full((2,), 3.0, np.float32))
s = g2.add_op("add", [a, b])
p = g2.add_op("mul", [s, g2.add_const("d", np.full((2,), 4.0, np.float32))])
g2.add_output(p)
print("chain folded:", constant_fold(g2), "ops:", [op.kind for op in g2.ops], "out:", g2.outputs[0].data.tolist())
```

本机输出：

```text
chain folded: 2 ops: [] out: [20.0, 20.0]
```

处理顺序：

1. 处理 `add`：输入 `a`、`b` 都是常量，折成 `add_0_folded = 5`，所有使用者（`mul`）的输入被替换。
2. 处理 `mul`：快照里的 `mul` 对象仍在，它的输入已经被替换成 `add_0_folded` 与 `d`，两者都是常量，于是折成 `20`。
3. `dce` 清掉 `a`、`b`、`add_0_folded`、`d` 等死值。

所以“一趟”足够处理向右的常量链。这依赖两个条件：op 列表是拓扑序，且替换是全局立即生效的。缺少任何一个，就需要外层固定点循环。

### 8.5 DCE 的实现

`Graph.dce` 是方法，不是独立 pass 函数：

```python
def dce(self) -> int:
    """Remove operators whose results are neither outputs nor used."""
    removed = 0
    while True:
        self.recompute_users()
        live: set[int] = {id(v) for v in self.outputs}
        keep: list[Op] = []
        for op in reversed(self.ops):
            needed = any(id(v) in live or v.users for v in op.outputs)
            if needed:
                keep.append(op)
                for v in op.inputs:
                    live.add(id(v))
            else:
                removed += 1
        keep.reverse()
        if len(keep) == len(self.ops):
            break
        self.ops = keep
    self.prune_dead_values()
    self.recompute_users()
    return removed
```

算法：

1. 从输出值开始，反向扫描 op 列表。
2. 一个 op 的输出如果“是图输出”或“还有使用者”，就保留它，并把它的输入加入 `live`。
3. 否则删掉，计数加一。
4. 如果这一轮没有删任何 op，退出循环。
5. `prune_dead_values()` 清掉不在任何 op 输入/输出、也不是图输入/输出的值。
6. 重算 `users`。

### 8.6 为什么 DCE 要循环

反向扫描看起来一趟就够，但 `v.users` 是“当前列表里的使用者”，其中可能包含本轮即将被删的 op。真实例子：

```python
g2 = Graph()
xx = g2.add_input("x", (2,))
a = g2.add_op("relu", [xx])
b = g2.add_op("relu", [a])
c = g2.add_op("exp", [b])
d = g2.add_op("relu", [xx])
g2.add_output(d)
print("dce removed:", g2.dce(), "kinds:", [op.kind for op in g2.ops], "values:", len(g2.values))
```

本机输出：

```text
dce removed: 3 kinds: ['relu'] values: 2
```

三轮的过程：

- 第一轮：`c` 没有使用者、不是输出，删；`b` 的输出被 `c` 使用（`c` 还在列表里），保留；`a` 被 `b` 使用，保留；`d` 是输出，保留。
- 第二轮：`b` 的使用者 `c` 已删，`b` 被删；`a` 仍被 `b` 使用（`b` 在本轮快照里），保留。
- 第三轮：`a` 的使用者 `b` 已删，`a` 被删。

三轮各删一个，第四轮确认无删除后退出；如果只扫一趟，会留下 `a`、`b` 两个孤儿。循环的退出条件是“本轮没删任何 op”，不是“没有未访问的 op”。

### 8.7 CSE 的实现

```python
def common_subexpression_elimination(graph: Graph) -> int:
    """Remove duplicate pure operators with identical inputs and attrs."""
    seen: dict[tuple, Value] = {}
    removed: list[Op] = []
    for op in graph.ops:
        key = (
            op.kind,
            tuple(id(v) for v in op.inputs),
            repr(sorted(op.attrs.items())),
            op.output.type.shape,
        )
        if key in seen:
            graph.replace_all_uses(op.output, seen[key])
            removed.append(op)
        else:
            seen[key] = op.output
    graph.remove_ops(removed)
    graph.dce()
    return len(removed)
```

键的四个组成部分：

- `op.kind`：算子种类。
- `tuple(id(v) for v in op.inputs)`：输入值的对象身份。顺序敏感，`add(a, b)` 与 `add(b, a)` 不合并。
- `repr(sorted(op.attrs.items()))`：属性字典的稳定字符串。排序保证 `{"axis": 1, "keepdims": True}` 与相反插入顺序得到同一个键。
- `op.output.type.shape`：输出形状。dtype 固定 float32，所以不需要。

真实例子一，两个相同的 `relu`：

```python
g = Graph()
x = g.add_input("x", (2,))
a = g.add_op("relu", [x])
b = g.add_op("relu", [x])
c = g.add_op("add", [a, b])
g.add_output(c)
print("cse removed:", common_subexpression_elimination(g), "ops:", [op.kind for op in g.ops])
```

```text
cse removed: 1 ops: ['relu', 'add']
```

真实例子二，链式重复：

```python
g3 = Graph()
x3 = g3.add_input("x", (2,))
a3 = g3.add_op("relu", [x3])
b3 = g3.add_op("relu", [x3])
c3 = g3.add_op("add", [a3, b3])
r3 = g3.add_op("relu", [c3])
s3 = g3.add_op("relu", [c3])
out3 = g3.add_op("add", [r3, s3])
g3.add_output(out3)
print("cse removed:", common_subexpression_elimination(g3), "ops:", [op.kind for op in g3.ops])
```

```text
cse removed: 2 ops: ['relu', 'add', 'relu', 'add']
```

第一个重复是 `b3`（与 `a3` 相同），合并后 `c3` 的输入变成 `(a3, a3)`。第二个重复是 `s3`（与 `r3` 相同），合并后 `out3` 的输入变成 `(r3, r3)`。一趟扫描足够，因为 `replace_all_uses` 立即改写了后续 op 的输入。

### 8.8 真实 pass 记录

demo 的流水线记录：

```text
constant_fold                    ops   5 ->   5  changed=0
algebraic_simplify               ops   5 ->   5  changed=0
cse                              ops   5 ->   5  changed=0
fuse_ops                         ops   5 ->   2  changed=2
dce                              ops   2 ->   2  changed=0
```

这张 MLP 图里没有可折的常量（权重是常量，但 `matmul` 的另一个输入是激活值），没有重复子表达式，也没有死 op。三个优化都返回 0，真正改变图的是 `fuse_ops`。这说明：

- “pass 没改”是正常结果，不是失败。
- 优化收益要在有对应模式的图上测，不能拿一个 demo 断定某个 pass 无用。
- 记录里的 `ops` 前后值只统计 op，不统计常量；常量折叠可能在不改 ops 的情况下删常量（下一节）。

## 底层机制

### 8.9 迭代顺序与全局替换

三个优化都依赖两个前提：

1. **op 列表是拓扑序**。`constant_fold` 和 `cse` 正向扫描，只有在“输入已经处理过”时才能做出正确判断。
2. **替换是全局立即生效的**。`replace_all_uses` 改所有 op 的 `inputs` 和 `graph.outputs`，所以后续 op 看到的总是最新值。

这两个前提让“一趟 + 快照”能处理大多数级联。反例是“后续 pass 创造新机会”：`algebraic_simplify` 把 `mul(x, 0)` 变成常量，但 `constant_fold` 已经跑过了，`exp(常量)` 要等下一轮。真实的两轮记录：

```text
run1: [('algebraic_simplify', 1)] kinds: ['exp'] out: output
run2: [('constant_fold', 1)] kinds: [] out: constant data: [1.0, 1.0]
run3: []
```

所以单个 pass 内部能做到“同向传播”，跨 pass 的机会要靠外层固定点（第 7 章）。

### 8.10 纯函数假设

CSE 的合法性依赖：两个 op 的 kind、输入、attrs 相同，就产生相同结果。这个假设要求算子：

- **确定性**：同样输入永远同样输出。`rand`、`time`、读环境变量的算子不满足。
- **无副作用**：不写全局状态、不改输入数组。原地算子不满足。
- **无别名依赖**：结果不依赖输入是否与其他值共享内存。

当前算子集全部是纯 NumPy 运算，所以成立。如果你加一个零输入的 `rand`：

- `constant_fold` 会跳过它（`not op.inputs`），这是保护。
- `cse` 会把两个 `rand` 合并成一个（key 相同：kind 相同、输入为空、attrs 相同、形状相同），这是错误。

这说明“加算子”不只是补 `apply_op`，还要审查每个 pass 的假设。第 10 章的融合、第 14 章的 in-place 都会再次碰到这条。

### 8.11 常量折叠与浮点语义

`constant_fold` 用 `apply_op` 算，解释器也用 `apply_op` 算，所以折叠结果与解释器一致。但与 C 后端可能不一致：

- C 后端用 `/fp:fast` 编译（第 19 章），允许重排、允许把 `a * b + c` 变成 FMA。折叠时用的是 NumPy 的顺序，运行期可能用另一条顺序。
- 折叠把结果固化成常量，后端不再有机会“按自己的方式重算”。这在数值上通常更稳定（少一次运行期运算），但意味着“折叠后的图”和“未折叠的图经 C 后端执行”可能差几个 ulp。
- 如果折叠产生 `inf` 或 `nan`（例如除零、`exp` 溢出），常量会带着这些值进入 codegen。不同后端对 NaN payload 的处理可能不同，测试要用 `isnan` 而不是相等比较。

因此，常量折叠的测试应该同时覆盖：折叠前后解释器输出一致、折叠前后 C 后端输出在容差内一致。

### 8.12 CSE 的键是身份，不是值

`tuple(id(v) for v in op.inputs)` 意味着两个内容相同但对象不同的常量不会被合并。真实例子：

```python
g4 = Graph()
x4 = g4.add_input("x", (2,))
k1 = g4.add_const("k1", np.ones((2,), np.float32))
k2 = g4.add_const("k2", np.ones((2,), np.float32))
u = g4.add_op("add", [x4, k1])
v = g4.add_op("add", [x4, k2])
w = g4.add_op("add", [u, v])
g4.add_output(w)
print("cse distinct consts removed:", common_subexpression_elimination(g4), "kinds:", [op.kind for op in g4.ops])
```

```text
cse distinct consts removed: 0 kinds: ['add', 'add', 'add']
```

`k1` 与 `k2` 数值相同，但 `id` 不同，所以 `add(x4, k1)` 与 `add(x4, k2)` 的键不同。这种设计简单、保守，也避免对大常量做逐元素比较。要合并它们，需要“结构化值编号”：把常量按 dtype、shape、内容哈希成键，再查表。代价是哈希计算和内存，收益是更多 CSE 机会。练习里会做一个小版本。

### 8.13 changed=0 不等于图没变

`constant_fold` 末尾调用 `graph.dce()`。对 demo 的原始 MLP，折叠数为 0，但两个从未被使用的原始权重常量被清掉：

```text
consts before: ['fc1_weight', 'fc1_bias', 'linear_weight_t', 'fc2_weight', 'fc2_bias', 'linear_1_weight_t']
folded: 0 ops: 5
consts after: ['fc1_bias', 'linear_weight_t', 'fc2_bias', 'linear_1_weight_t']
values: 12 -> 10
```

`PassManager` 的记录只显示 `changed=0`，ops 数也不变（5 -> 5），但 `graph.values` 少了 2 个。读记录时要记住：`ops` 与 `changed` 都不覆盖常量表。需要完整状态时，用 `dump()` 或直接统计 `len(graph.values)`。

### 8.14 DCE 与输出存活

`dce` 的 `live` 集合从 `graph.outputs` 开始。如果一个 op 的输出是图输出，它一定保留，即使没有使用者。这保证了“输出不被删”。

但它不检查“输出的 producer 是否还在 `ops` 里”。如果先用坏 pass 删掉最后一个 op，再调用 `dce`，输出值的 `producer` 指向一个不在列表里的 op，`dce` 不会修复这个指针；`validate` 也可能通过（第 6 章 6.7）。这类破坏要在 pass 契约层面避免，不要指望 DCE 兜底。

## 进一步用法与变体

- **结构化常量键**。给 CSE 的 key 加一个常量摘要：`(dtype, shape, data.tobytes())`。小常量直接哈希内容；大常量可以只哈希前 N 个元素加长度，或者跳过。要在“更多合并”和“哈希成本”之间取平衡。
- **交换律规范化**。对 `add`、`mul`，按输入的结构键排序后再比较，让 `add(a, b)` 与 `add(b, a)` 合并。浮点加法可交换（忽略 NaN payload），但不可结合；只排序操作数，不重排归约链。
- **折叠预算**。把 `max_numel` 暴露成 pipeline 参数，对小图用大预算、对大图用小预算。记录被跳过的 op 数，避免“以为折了其实没折”。
- **折叠后立即验证数值**。在测试里对同一张图跑“折叠前解释器”和“折叠后解释器”，比较输出；再跑 C 后端，比较容差。三层对照能区分“折叠错”和“后端错”。
- **DCE 与内存规划联动**。DCE 删掉的 op 不再占 arena；第 13 章的峰值会因此下降。对比“DCE 前/后”的 `plan_memory().summary()`，能直观看到收益。
- **CSE 后再 DCE**。CSE 合并后，原来的重复 op 变成死值，必须 `dce` 才能清掉；当前实现已经在末尾调用 `graph.dce()`。如果你自己写 CSE，别忘了这一步。

## 对比与取舍

| 优化 | 主要收益 | 代价 | 何时无效 |
| --- | --- | --- | --- |
| 常量折叠 | 减少运行期 op、把权重预处理到编译期 | 编译期计算；大常量可能很慢 | 输入含激活值；超过 `max_numel` |
| DCE | 缩小图、减少内存峰值、让后续 pass 更快 | 每轮 O(n) 扫描；多轮 | 图里没有死值 |
| CSE | 减少重复计算；为融合创造更简单的图 | 键构造与哈希；身份键漏合并 | 重复项输入对象不同；有副作用算子 |
| 融合（第 10 章） | 减少中间 buffer 与访存 | 模式匹配复杂；合法性约束多 | 链上有多个使用者；形状不匹配 |

三个取舍：

- **CSE vs 融合**：CSE 减少“算几次”，融合减少“写几次内存”。一个重复的 `matmul` 被 CSE 合并后只算一次；一个 `matmul + bias + relu` 被融合后只写一次输出。两者互补，顺序上通常先 CSE 再融合。
- **折叠 vs 运行期计算**：折叠把工作搬到编译期。对只跑一次的图，可能不划算；对反复调用的推理服务，收益明显。`max_numel` 是这条权衡的旋钮。
- **保守 vs 激进**：身份键 CSE、全常量折叠都是保守策略，正确性容易保证。结构化键、部分常量传播、代数重写更激进，收益更高，但需要处理更多浮点与别名边界（第 9 章会展开）。

## 踩雷集锦

1. **以为 CSE 会合并数值相同的常量**。它用 `id(v)` 做键，`k1` 与 `k2` 即使内容一样也不合并。真实结果：`removed=0`，三个 `add` 全在。要合并就换成结构化键。
2. **用 `changed` 判断图有没有变**。`constant_fold` 返回 0 时仍可能通过内部 `dce` 删掉常量（12 -> 10 个值）。`ops` 数也不反映常量表。需要完整状态时看 `dump()` 或 `len(graph.values)`。
3. **以为超过 `max_numel` 会报错**。它会静默跳过，`folded=0`。排查“为什么没折”时先看输出 `numel` 是否大于 4096。
4. **没注意 CSE 会改输出名**。真实例子：`relu_1` 作为输出，与 `relu_0` 合并后，输出名变成 `relu_0`。按名字取输出的下游代码会断；按 `graph.outputs` 顺序取则不受影响。
5. **给 IR 加有副作用或随机算子后继续用 CSE**。两个 `rand` 会被合并成一个，语义从“两次采样”变成“一次采样用两次”。加算子时要审查 `constant_fold`、`cse`、`fuse_ops` 的纯函数假设。

## 进阶

- **实现结构化 CSE**。给常量做内容键，给非常量保留身份键；跑本章的三个例子，确认 `k1`/`k2` 被合并，且 `op` 数从 3 降到 2。注意大常量的哈希成本，设置尺寸上限。
- **实现交换律合并**。对 `add`、`mul` 的输入按 `(kind, shape, id/常量摘要)` 排序，再构造键。验证 `add(a, b)` 与 `add(b, a)` 合并，且数值不变。
- **给折叠加统计**。返回 `(folded, skipped_large, skipped_partial)`，或者写一个旁路记录。这样“没折”有可解释的原因。
- **做折叠前后三层对照**。解释器（折叠前）、解释器（折叠后）、C 后端（折叠后），三者两两比较。第 19 章的 `/fp:fast` 差异会在这里第一次出现。
- **DCE 的增量化**。当前实现每轮全图扫描；可以维护一个“死值队列”，只重算受影响的 op。图大时收益明显，但实现复杂度上升。
- **CSE 与支配关系**。当前 CSE 是全局的，依赖 SSA 和拓扑序。如果将来引入控制流，CSE 需要支配树分析；第 2 章的平坦图设计刻意避开了这件事。

## 动手练习

1. 构造 `c1 + c2 -> mul(x, ...)` 的图，手动推演 `constant_fold` 的每一步，写出 `graph.values` 在折叠前后的名字集合。用 `dump()` 验证。
2. 构造一条 `relu -> relu -> exp` 的死链，外加一个独立输出，运行 `dce()`。记录每一轮删了哪个 op，解释为什么要三轮。
3. 构造两个内容相同的常量 `k1`、`k2`，验证 CSE 不合并；再把 CSE 的键改成包含 `data.tobytes()`，验证合并后 `ops` 数与数值。
4. 把 `max_numel` 设为 4，跑一个 `(4,) + (4,)` 和一个 `(8,) + (8,)` 的常量加法，记录 `folded` 与最终常量数。
5. 对同一张图分别跑 `constant_fold` 和 `algebraic_simplify` 两轮，复现 `exp(mul(x, 0))` 需要两轮才折成常量的现象，并解释第二轮是谁创造的折叠机会。
6. 写一个测试：对随机图跑 `standard_pipeline`，比较优化前后解释器输出，误差 <= 1e-5；再断言第二遍 pipeline 的 `sum(changed) == 0`。

## 本章重点

- `constant_fold` 只折全常量输入、结果 `numel <= max_numel` 的 op，用 `apply_op` 保证语义一致，末尾调用 `dce`。
- `max_numel` 超限是静默跳过；`changed=0` 不代表图没变。
- DCE 反向扫描、按输出与 users 判定存活，必须循环到“本轮无删除”，否则会留下孤儿链。
- CSE 的键是 `(kind, 输入 id 元组, attrs 的稳定 repr, 输出 shape)`；身份键不会合并内容相同的常量。
- 三个优化都依赖拓扑序、全局替换和纯函数假设。
- 一趟扫描能处理同向级联；跨 pass 的新机会需要固定点循环。
- CSE 可能改变输出名；按名字取输出的代码要小心。
- 常量折叠把浮点结果固化，可能和后端 `/fp:fast` 产生末位差异。

## 自我检核

- [ ] 我能默写 `constant_fold` 的五个判断与三个动作。
- [ ] 我能解释 `max_numel` 为什么存在，以及超限时的行为。
- [ ] 我能说清 DCE 为什么需要 `while True`，并举出三轮删除的例子。
- [ ] 我能写出 CSE 的键，并解释每个字段的作用。
- [ ] 我知道 CSE 不合并内容相同的常量，并能给出真实计数。
- [ ] 我能解释 `changed=0` 但 `values` 从 12 减到 10 的原因。
- [ ] 我能说出 CSE 改变输出名的一个真实例子。
- [ ] 我能指出给 IR 加 `rand` 后哪个优化会出错，为什么。
- [ ] 我能设计一个测试，同时验证折叠前后解释器与 C 后端的一致性。

## 延伸阅读

- **LLVM `InstCombine` 文档与源码**（<https://llvm.org/docs/Passes.html#instcombine>，`llvm/lib/Transforms/InstCombine/`）：读常量折叠与指令化简的组织方式。关联本章的 `constant_fold`，看看工业级实现如何把“折叠”和“代数化简”放进同一个 worklist。
- **LLVM GVN 与 DCE 文档**（<https://llvm.org/docs/Passes.html#gvn>）：读全局值编号（GVN）如何用值编号做 CSE。关联 8.7 的身份键与 8.12 的结构化键，理解“值编号”是 CSE 的推广。
- **MLIR Canonicalization**（<https://mlir.llvm.org/docs/Canonicalization/>）：读 `fold` hook、`canonicalize` pass 与固定点驱动。关联本章的“一趟 vs 固定点”和“op 级折叠 hook”设计。
- **TVM Relay `FoldConstant`**（<https://tvm.apache.org/docs/reference/api/python/relay/transform.html>）：读它对常量传播与死代码的处理。关联 `constant_fold` + `dce` 的组合，以及大常量预算的取舍。
- **《Engineering a Compiler》**（Cooper & Torczon）第 9 章“Data-Flow Analysis”：读到达定值、活跃变量与常量传播。关联 DCE 的 `live` 集合与 CSE 的可用表达式分析，理解本章的“局部版本”省略了哪些全局分析。

---

下一章处理代数化简：`x + 0`、`x * 1`、`relu(relu(x))` 看起来无害，但在浮点语义下并不总是等价。NaN、Inf、负零会让“看起来成立”的恒等式失效。

[第 9 章 代数化简](./09-algebraic-simplification.md)