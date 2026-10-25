# 第 7 章 Pass 基础设施：PassManager、顺序、固定点与不变量

> **目标**：读完本章，你能给 mini 编译器写一个符合契约的 pass；能解释 `PassManager` 的返回值、`changed` 的含义与 `verbose` 输出的每一列；能判断一组 pass 需要跑几遍才到固定点；能用 `dump()` 对比优化前后；能列出 pass 必须维持的不变量，并知道 `validate()` 抓不到哪一类破坏。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；`torch 2.14.1+cpu`；`numpy 2.4.6`。本章命令在仓库根目录执行：
>
> ```powershell
> $env:PYTHONPATH = "compilers\mini_ai_compiler\code"
> $env:PYTHONIOENCODING = "utf-8"
> & ".scratch/aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\demo_e2e.py"
> ```

## 为什么需要 PassManager

单个优化不难写：找到模式、替换、删节点。难的是让一组优化可靠地组合：

- 谁先跑？常量折叠放在代数化简前面还是后面，结果可能不同。
- 跑几遍？一次改写可能给下一次改写创造机会。
- 怎么知道它真的改了？如果 pass 返回 0，外层循环就停；返回错了，固定点判断就错。
- 改坏了怎么办？需要统一的验证钩子和可复现的中间状态。
- 怎么对比？需要稳定的 dump 和记录。

`PassManager` 把这些问题收敛成一个很小的契约：每个 pass 接收一张图、原地修改、返回“改了多少”，管理器负责顺序、记录、验证和调试输出。它不负责聪明，只负责让聪明的东西可组合。

## 先建立直觉：一条流水线，两个计数器

```text
        graph (valid)
          |
          v
  +----------------+     before = len(ops)
  | constant_fold  | --> changed = 0
  +----------------+     validate()
          |
  +----------------+     before = len(ops)
  | algebraic      | --> changed = 1
  +----------------+     validate()
          |
  +----------------+     before = len(ops)
  | cse            | --> changed = 0
  +----------------+     validate()
          |
  +----------------+     before = len(ops)
  | fuse_ops       | --> changed = 2
  +----------------+     validate()
          |
  +----------------+     before = len(ops)
  | dce            | --> changed = 0
  +----------------+     validate()
          |
          v
   records = [PassRecord(name, changed), ...]

  固定点判断：sum(changed) == 0 ?
     是 -> 停
     否 -> 再来一轮（有 max_iters 上限）
```

两个计数器要分清：

- `changed` 是 pass 自己报告的“改写次数”，语义由 pass 定义。
- `len(graph.ops)` 是图的规模，管理器只在 `verbose` 时打印前后值。

两者不等价。`fuse_ops` 可能 `changed=2`，但 ops 从 5 变成 2，因为它删了 3 个、插入了 2 个。拿 `changed` 当“删了多少节点”会得出错误结论。

## 核心内容

### 7.1 Pass 的契约

一个 pass 是一函数，签名如下：

```python
def my_pass(graph: Graph, **kwargs) -> int: ...
```

四条硬要求：

1. **原地修改同一张图**。不要 `return new_graph`；调用方持有原对象引用，替换对象会让 `records`、`plan_memory`、后端全部指向旧图。
2. **返回整数**。0 表示“没有发现可改写的地方”。非 0 的具体含义由 pass 定义，但同一 pass 内要一致。
3. **保持图合法**。至少要满足 `Graph.validate`；更强的要求是类型也一致（第 6 章的 `strict_verify`）。
4. **保持语义**。改写前后，对任意合法输入，输出在容差内相同。这一条验证器管不了，只能靠测试。

两条软要求：

- **确定性**。同样的图、同样的参数，跑两次得到同样的结果。遍历顺序、字典顺序、集合迭代都可能破坏它。
- **尽量幂等**。第二次跑同一个 pass 返回 0，或者至少让整条流水线收敛。

### 7.2 PassManager 与 PassRecord

实现只有二十多行：

```python
@dataclass
class PassRecord:
    name: str
    changed: int


class PassManager:
    def __init__(self) -> None:
        self.passes: list[tuple[str, Callable[..., int], dict]] = []

    def add(self, name: str, fn: Callable[..., int], **kwargs) -> "PassManager":
        self.passes.append((name, fn, kwargs))
        return self

    def run(self, graph: Graph, *, verbose: bool = False) -> list[PassRecord]:
        records: list[PassRecord] = []
        for name, fn, kwargs in self.passes:
            before = len(graph.ops)
            changed = int(fn(graph, **kwargs))
            graph.validate()
            records.append(PassRecord(name, changed))
            if verbose:
                print(f"{name:32s} ops {before:3d} -> {len(graph.ops):3d}  changed={changed}")
        return records
```

几个设计点：

- `add` 返回 `self`，支持链式调用，`standard_pipeline` 就是这样写的。
- `**kwargs` 在 `add` 时保存，在 `run` 时展开。同一 pass 可以注册多次，用不同参数（例如两次 `constant_fold`，一次 `max_numel=64`，一次默认）。
- `graph.validate()` 在每个 pass 之后执行。它没有 try/except，所以验证失败会直接中断整个流水线，异常类型是 `IRError`。
- `before` 在 pass 前取，`len(graph.ops)` 在 pass 后取。`changed` 不参与 ops 计数。
- `verbose` 的格式是 `name:32s`、`ops {before:3d} -> {len:3d}`、两个空格、`changed=...`。固定宽度是为了让多行输出对齐，方便肉眼比较。

本章开头跑 demo 得到的真实记录：

```text
constant_fold                    ops   5 ->   5  changed=0
algebraic_simplify               ops   5 ->   5  changed=0
cse                              ops   5 ->   5  changed=0
fuse_ops                         ops   5 ->   2  changed=2
dce                              ops   2 ->   2  changed=0
```

`run` 返回的对象：

```text
[PassRecord(name='constant_fold', changed=0),
 PassRecord(name='algebraic_simplify', changed=0),
 PassRecord(name='cse', changed=0),
 PassRecord(name='fuse_ops', changed=2),
 PassRecord(name='dce', changed=0)]
```

记录里没有时间、没有 ops 前后值、没有异常信息。需要这些信息时，自己包一层，不要改 `PassRecord` 的字段（下游可能依赖它的形状）。

### 7.3 standard_pipeline 的顺序与理由

```python
def standard_pipeline() -> PassManager:
    pm = PassManager()
    pm.add("constant_fold", constant_fold)
    pm.add("algebraic_simplify", algebraic_simplify)
    pm.add("cse", common_subexpression_elimination)
    pm.add("fuse_ops", fuse_ops)
    pm.add("dce", lambda g, **_: g.dce())
    return pm
```

顺序的理由可以压成一句：先把能静态确定的东西定下来，再化简、去重、融合，最后清理。`constant_fold` 让后面的 pass 看到更少的 op；`algebraic_simplify` 利用刚折出来的 `0`、`1`；`cse` 合并前面产生的重复子表达式；`fuse_ops` 放到化简之后，避免刚融合又被拆开；`dce` 收尾，清掉任何一步留下的死值。

最后一项用 `lambda g, **_: g.dce()` 是为了把 `Graph` 的方法适配成 pass 签名。`dce` 返回删除的 op 数，正好符合“返回 int”的契约。这个 lambda 也说明 `PassManager` 不要求 pass 是模块级函数，任何可调用对象都行。

顺序不是“最佳实践”，而是“当前实现下的一个选择”。7.6 节会用一个反例说明换顺序会得到不同结果。

### 7.4 用 dump 对比优化前后

`demo_e2e.py` 先打印原始图，再跑流水线，最后打印优化图。原始图的关键部分：

```text
ops:
  00 %linear_matmul = matmul(x0, linear_weight_t) : tensor<4, 16>
  01 %linear = add(linear_matmul, fc1_bias) : tensor<4, 16>
  02 %relu = relu(linear) : tensor<4, 16>
  03 %linear_1_matmul = matmul(relu, linear_1_weight_t) : tensor<4, 4>
  04 %linear_1 = add(linear_1_matmul, fc2_bias) : tensor<4, 4>
outputs:
  linear_1: tensor<4, 4>
```

优化后：

```text
ops:
  00 %relu_fused = matmul_bias_relu(x0, linear_weight_t, fc1_bias) : tensor<4, 16>
  01 %linear_1_fused = matmul_bias(relu_fused, linear_1_weight_t, fc2_bias) : tensor<4, 4>
outputs:
  linear_1_fused: tensor<4, 4>
```

读 dump 时注意：

- 常量段少了 `fc1_weight`、`fc2_weight`。它们从未被任何 op 使用：前端把 `vals[1].data.T` 复制成一个新的 `linear_weight_t`，原权重只是建常量的原料。`constant_fold` 结尾调用 `graph.dce()`，即使一个 op 都没折叠，也会 `prune_dead_values()` 清掉这些常量。
- 输出名字从 `linear_1` 变成 `linear_1_fused`。下游如果按名字取输出，需要跟着改；本仓库的 `evaluate_graph` 按 `graph.outputs` 顺序取值，不受影响。
- `dump()` 的 op 编号是列表下标，不是稳定 ID。优化后编号会重排，别用编号做跨版本的断言。

`dump()` 的格式是固定的四段：`inputs`、`constants`（可能省略）、`ops`、`outputs`。每个 op 一行，包含 `%name = kind(inputs) attrs : type`。这个格式故意接近 MLIR/LLVM 的可读 IR，方便肉眼 diff。

### 7.5 固定点：跑一遍不够

固定点（fixed point）的意思是：再跑一轮，所有 pass 都返回 0，图不再变化。

对 demo 的图，第一轮之后已经到固定点。真实输出：

```text
second run:
constant_fold                    ops   2 ->   2  changed=0
algebraic_simplify               ops   2 ->   2  changed=0
cse                              ops   2 ->   2  changed=0
fuse_ops                         ops   2 ->   2  changed=0
dce                              ops   2 ->   2  changed=0
```

但这不是普遍规律。看这张图：

```python
g = Graph()
x = g.add_input("x", (2,))
z = g.add_const("zero", np.zeros((2,), np.float32))
m = g.add_op("mul", [x, z])
e = g.add_op("exp", [m])
g.add_output(e)
```

第一轮里，`algebraic_simplify` 把 `mul(x, 0)` 换成零常量；`exp(0)` 现在有全常量输入，但 `constant_fold` 已经跑过了。第二轮，`constant_fold` 才把 `exp(0)` 折成 `1`。第三轮无事可做。

用一个显式的固定点循环观察：

```python
def run_fixed(pm, graph, max_iters=8):
    total = 0
    for i in range(1, max_iters + 1):
        recs = pm.run(graph)
        changed = sum(r.changed for r in recs)
        total += changed
        print(f"  iter {i}: changed={changed}")
        if changed == 0:
            return i, total
    return max_iters, total


iters, total = run_fixed(standard_pipeline(), g)
print("fixed point iters:", iters, "total changed:", total, "ops:", len(g.ops))
```

本机输出：

```text
  iter 1: changed=1
  iter 2: changed=1
  iter 3: changed=0
fixed point iters: 3 total changed: 2 ops: 0
```

最终 `ops: 0`，输出变成常量 `[1.0, 1.0]`。这个例子说明：

- “跑一遍 `standard_pipeline`”不保证到固定点。
- 固定点循环要有 `max_iters`，否则一个振荡的 pass 会让编译卡死。
- 计数到 0 的那一轮也要算进迭代次数，因为它证明了“没有变化”。

### 7.6 顺序会改变结果

固定一张图：`x -> add(x, 0) -> relu`。用两种顺序跑：

```python
def build():
    g = Graph()
    x = g.add_input("x", (4,))
    z = g.add_const("zero", np.zeros((4,), np.float32))
    a = g.add_op("add", [x, z])
    r = g.add_op("relu", [a])
    g.add_output(r)
    return g

g1 = build()
r1 = standard_pipeline().run(g1)
print("standard kinds:", [op.kind for op in g1.ops])
print("records:", [(r.name, r.changed) for r in r1])

g2 = build()
pm2 = PassManager().add("fuse_ops", fuse_ops).add("algebraic_simplify", algebraic_simplify).add("dce", lambda gr, **_: gr.dce())
r2 = pm2.run(g2)
print("fuse-first kinds:", [op.kind for op in g2.ops])
print("records:", [(r.name, r.changed) for r in r2])
```

本机输出：

```text
standard kinds: ['relu']
records: [('constant_fold', 0), ('algebraic_simplify', 1), ('cse', 0), ('fuse_ops', 0), ('dce', 0)]
fuse-first kinds: ['add_relu']
records: [('fuse_ops', 1), ('algebraic_simplify', 0), ('dce', 0)]
```

两个结果数值等价，但质量不同：

- 标准顺序先用 `algebraic_simplify` 把 `x + 0` 消掉，得到 `relu(x)`，只剩一个 op。
- 先融合时，`add_relu(x, 0)` 成为一个整体，后续的代数化简看不到里面的 `+ 0`，于是保留了一次加法。

这就是“pass 顺序会改变优化结果”的最小例子。它也解释了为什么 `standard_pipeline` 把 `algebraic_simplify` 放在 `fuse_ops` 前面。

### 7.7 Pass 不变量清单

把 pass 当作一种“受约束的程序变换”，需要维持的不变量如下：

| 不变量 | 检查手段 | 违反后果 |
| --- | --- | --- |
| SSA：每个值只定义一次 | `validate` | 后续改写指向错误的值 |
| 拓扑序：输入先于使用 | `validate` | DCE、融合匹配静默失败 |
| 值归属：输入/输出属于图 | `validate` | 克隆或序列化崩溃 |
| 输出非空且属于图 | `validate` | 后端无法决定返回值 |
| 输出有存活的 producer | 自定义检查 | codegen 找不到生产者 |
| 类型与算子一致 | `strict_verify` | 内存越界或形状错误 |
| `users` 索引与 `ops` 一致 | `recompute_users` + 断言 | DCE 删错 |
| 语义等价 | 差分测试 | 数值错误 |
| 确定性 | 同输入跑两次比较 dump | 不可复现的构建 |
| `changed` 诚实 | 固定点循环 | 提前停止或死循环 |

前四项 `validate` 覆盖，第五、六项要自己补，后四项只能靠测试。

两个真实反例。坏 pass 删掉最后一个 op 但保留输出时，`validate` 通过，输出仍打印为 `Value(relu_1, tensor<4>, output)`，其 producer 指向已经被移出 `graph.ops` 的 op，进入 codegen 才暴露；删掉一个仍被下游使用的 op 时，`validate` 会抓到 `IRError: op exp uses relu_0 before it is defined`，因为下游输入不再有更早的 producer。第 6 章的 `check_outputs_live` 就是为前一个缺口准备的。

### 7.8 失败时的原子性

`PassManager` 是原地修改，没有回滚。如果第三个 pass 抛异常，前两个 pass 的修改已经生效，图处于“部分优化”状态。这在交互式调试时很烦：你想重跑，但起点已经变了。

两个应对方式：

1. **在克隆上试跑**。`graph.clone()` 会复制输入、常量、op 和输出，产生全新的 `Value`/`Op` 对象，名字保留。真实对比：

   ```text
   original before: ['add', 'relu'] clone: ['add', 'relu']
   clone after: ['relu'] changed: [('constant_fold', 0), ('algebraic_simplify', 1), ('cse', 0), ('fuse_ops', 0), ('dce', 0)]
   original after: ['add', 'relu'] out: relu_1 clone out: relu_1
   same object? False
   ```

   克隆图优化到 `['relu']`，原图仍是 `['add', 'relu']`；两者输出同名但不是同一个对象。

2. **在 pass 内部先收集、再统一提交**。例如 `fuse_ops` 先算出 `consumed`、`replacements`、`insert_at`，最后一次性重写 `graph.ops`。这样中途匹配失败不会留下半成品。代价是实现更复杂，需要额外的字典。

不建议用 `copy.deepcopy(graph)` 代替 `clone()`：深拷贝依赖 `Value`、`Op`、`users`、`producer` 的内部引用图一起被正确复制，结果通常能用，但语义依赖 dataclass 的字段结构。`clone()` 通过 `add_input` / `add_const` / `add_op` 显式重建，顺带重新推导类型并校验形状，行为更可控。

### 7.9 changed 的语义由 pass 定义

`changed` 不是统一单位：

- `constant_fold`：折叠的 op 数。
- `algebraic_simplify`：应用的恒等式次数。
- `cse`：删除的重复 op 数。
- `fuse_ops`：应用的融合模式数。
- `dce`：删除的 op 数。

因此不能跨 pass 比较“谁更有效”。固定点循环只关心“是否为 0”，不关心数值大小。

`changed` 只统计该 pass 声明的“改写”，不保证图没有其他变化。真实例子：对 demo 的原始 MLP 跑 `constant_fold`，返回 0，但常量从 6 个减到 4 个，`values` 从 12 减到 10：

```text
consts before: ['fc1_weight', 'fc1_bias', 'linear_weight_t', 'fc2_weight', 'fc2_bias', 'linear_1_weight_t']
folded: 0 ops: 5
consts after: ['fc1_bias', 'linear_weight_t', 'fc2_bias', 'linear_1_weight_t']
values: 12 -> 10
```

原因是 `constant_fold` 末尾调用了 `graph.dce()`。所以 `changed=0` 不等于“图没变”，只等于“没有折叠”。固定点循环仍然能收敛，因为下一轮还是没有可折叠的 op；但“changed=0 就跳过后续记录”的优化是错的。

`changed` 还必须诚实。一个反例：

```python
def sneaky(graph, **_):
    y = graph.add_op("relu", [graph.outputs[0]], name="extra")
    graph.add_output(y, "extra")
    return 0
```

它每次调用都往图里加一个 op，却返回 0。固定点循环第一轮就停：

```text
  iter 1: changed=0
sneaky iters: 1 ops: 2
```

图里有 2 个 op，但循环认为已经收敛。这类 bug 不会让编译器报错，只会让优化少跑几轮，或者让“幂等性测试”假通过。

## 底层机制

### 7.10 遍历时修改列表

Python 的 `for op in graph.ops` 遍历的是列表本身。在循环里 `graph.ops.remove(op)` 会跳过元素。现有 pass 分两类处理：`constant_fold`、`algebraic_simplify` 用 `list(graph.ops)` 做快照再批量删；`cse`、`fuse_ops` 先收集结果，最后一次性替换 `graph.ops`。写新 pass 时选一种，不要在遍历中增删同一个列表。

### 7.11 replace_all_uses 与 users

`replace_all_uses(old, new)` 做三件事：改所有 op 的 `inputs`、改 `graph.outputs`、重算 `users`。它是大多数改写的安全出口。

手工改 `op.inputs` 而不调用它，会留下两个问题：其他使用者仍指向旧值；`users` 索引过期。前者让优化不彻底，后者让 DCE 判断错误。验证器不检查 `users`，所以这类错误会安静地传播。

### 7.12 验证的成本与位置

`validate` 是 O(values + ops)。对 5 个 op 的图，成本可以忽略；对上千 op 的图，每个 pass 后跑一次仍然可接受（毫秒级），但固定点循环跑十轮就会累加。

三种策略：总是验证（当前实现）、只在 debug 模式验证、只在可能破坏结构的 pass 后验证。本课程选择“总是验证”，因为图小，且教学阶段错误定位比编译速度重要。

### 7.13 异常与记录

如果 pass 抛异常，`run` 不会为该 pass 追加 `PassRecord`，也不会执行 `validate`。调用方拿到的是异常，不是部分记录。这意味着：

- 不能用 `records` 的长度判断“跑到第几个 pass”。
- 需要定位时，在 `run` 外面包一层，或者给 `PassManager` 加日志。异常链里没有 pass 名，是当前实现的一个可用性缺口。

## 进一步用法与变体

- **计时包装**。不改 pass，写一个装饰器记录 `perf_counter`，把名字和耗时写进日志。注意计时包含 `validate` 的时间，不要把它算进 pass 本身。
- **dump diff**。每轮 pass 后把 `graph.dump()` 写到临时文件，用 `difflib.unified_diff` 比较。比肉眼看两段 dump 可靠，尤其是 op 顺序变化时。
- **条件 pass**。只在 `len(graph.ops) > N` 时跑融合，避免小图上的匹配开销。条件判断放在外层，不要塞进 pass 内部。
- **分阶段流水线**。图级化简、融合、内存相关改写分成三个 `PassManager`，每个阶段结束后做一次 `strict_verify`。比一条长流水线更容易定位问题。
- **记录序列化**。把 `PassRecord` 转成 JSON，附上 `before_ops`、`after_ops`、耗时，作为构建产物的一部分。复现性能问题时，这份记录比日志有用。
- **幂等性测试**。对一批图跑两遍流水线，断言第二遍的 `sum(changed) == 0`。它不能证明语义正确，但能抓到“pass 永远在改”的振荡。

## 对比与取舍

| 方案 | 编译时间 | 优化质量 | 可调试性 | 适用 |
| --- | --- | --- | --- | --- |
| 单次流水线 | 低 | 取决于顺序，可能停在半固定点 | 高 | 教学 demo、快速构建 |
| 固定点循环 | 中到高 | 更彻底 | 中 | 需要稳定形态的产物 |
| 每 pass 后严格验证 | 高 | 不变 | 最高 | 开发期、调试 miscompile |
| 克隆后 dry run | 高（复制图） | 不变 | 高 | 实验性 pass、回归对比 |

三个取舍：

- **固定点 vs 单次**：固定点会让编译时间随“改写链长度”增长；单次便宜，但结果依赖顺序。生产编译器通常给固定点设上限，并对“达到上限仍未收敛”报警。
- **验证频率**：每个 pass 后验证能最早定位破坏者；只在流水线末尾验证更快，但错误可能被后续 pass 放大。
- **原地 vs 克隆**：原地省内存、实现简单；克隆适合试错和 A/B 对比。图很大时，克隆的成本不可忽略。

## 踩雷集锦

1. **把 `changed` 当成“删了多少 op”**。`fuse_ops` 的 `changed=2` 对应 ops 5 -> 2，不是删了 2 个。需要规模变化时，自己记录 `len(graph.ops)`。
2. **pass 增加了节点却返回 0**。固定点循环会提前停止。7.9 的 `sneaky` pass 是实跑反例：一轮就停，图里却多了 op。
3. **遍历 `graph.ops` 时删除**。会跳过元素，甚至漏掉本应处理的 op。用快照或“先收集后提交”。
4. **以为 `validate` 能抓住所有破坏**。删掉最后一个 op 但保留输出时，它通过；类型被篡改时，它也通过。补输出 producer 检查与 `strict_verify`。
5. **换 pass 顺序后期望同样的 IR**。7.6 的 `relu` vs `add_relu` 是真实差异。顺序是设计的一部分，改顺序要重跑形态断言。

## 进阶

- **给 `PassManager` 加固定点模式**。`run_to_fixed_point(graph, max_iters=8)` 返回 `(iters, records_per_iter)`；达到上限时抛异常或打 warning，不要静默返回半成品。
- **加 pass 级异常上下文**。捕获 `IRError`，重新抛出为 `IRError(f"pass {name} produced an invalid graph: {exc}")`，保留 `from exc` 的异常链。
- **实现 dump diff 工具**。输入两张图，输出按 op 对齐的差异（新增、删除、类型变化）。op 名字不稳定时，用“输入集合 + 算子种类”做近似匹配。
- **测量每个 pass 的收益**。在固定图上记录每轮 `changed` 与 ops 数，找出“跑了很多轮但只改一处”的 pass，考虑合并或前置。
- **做顺序敏感性测试**。对同一批图，随机打乱 pass 顺序，比较最终 ops 数与数值误差。能发现“只在某个顺序下正确”的隐式依赖。

## 动手练习

1. 构造 `exp(mul(x, 0))` 的图，用 `run_fixed` 跑到收敛，记录每轮 `changed`。再把 `max_iters` 设为 2，观察返回值和图的最终状态。
2. 把 7.6 的两种顺序都跑一遍，用 `dump()` 保存两份 IR，写出它们数值等价但 op 数不同的理由。
3. 写一个坏 pass，删除一个仍被下游使用的 op，确认 `PassManager` 报错；再写一个删除末尾 op 的坏 pass，确认它不报错。给第二个补上输出 producer 检查。
4. 写一个 pass，统计并打印每类 op 的数量，返回 0。把它插到 `standard_pipeline` 末尾，确认它不改变图，也不影响固定点判断。
5. 给 `PassManager` 加一个 `dry_run(graph)` 方法：在 `graph.clone()` 上跑完整流水线，返回 `(records, cloned_graph)`，原图不变。用 7.8 的例子验证。

## 本章重点

- Pass 契约：原地修改、返回 int、保持合法、保持语义、确定性、尽量幂等。
- `PassManager` 负责顺序、`validate`、记录与 verbose 输出；`PassRecord` 只有 `name` 和 `changed`。
- `changed` 的语义由 pass 定义，不能跨 pass 比较，也不能当 ops 变化量。
- `standard_pipeline` 的顺序是 fold -> algebraic -> cse -> fuse -> dce；顺序会影响优化结果。
- 跑一遍不保证到固定点；固定点循环要有 `max_iters`，并把“0 变化”的那一轮算进去。
- `validate` 在每个 pass 后执行，但不覆盖类型复检、输出 producer 存活、`users` 一致性。
- 原地流水线没有回滚；试错用 `graph.clone()`。
- `dump()` 是四段固定格式，适合 diff，但 op 编号不稳定。

## 自我检核

- [ ] 我能写出一个符合契约的 pass，并说明四条硬要求。
- [ ] 我能解释 `changed=2` 与 ops `5 -> 2` 为什么不矛盾。
- [ ] 我能说出 `standard_pipeline` 五个阶段的顺序及至少两条理由。
- [ ] 我能构造一张需要两轮以上才到固定点的图。
- [ ] 我能解释为什么“0 变化”的那一轮也要计入迭代。
- [ ] 我能举出一个 pass 顺序改变最终 IR 的例子。
- [ ] 我知道 `validate` 抓不到“删末 op 但保留输出”，并知道怎么补。
- [ ] 我能用 `clone()` 做 dry run，并确认原图未被修改。
- [ ] 我能说出 `changed` 不诚实时固定点循环会怎样失败。

## 延伸阅读

- **LLVM PassManager 文档**（<https://llvm.org/docs/Passes.html> 与 `llvm/docs/NewPassManager.rst`）：读 pass 分组、analysis preservation 与 `PreservedAnalyses`。关联本章的“pass 后验证”与“pass 之间共享分析结果”的取舍。
- **MLIR Pass Management**（<https://mlir.llvm.org/docs/PassManagement/>）：读 pass 嵌套、`runOnOperation` 与 pass 失败语义。关联本章的“流水线顺序”和“失败时原子性”；MLIR 用 signalPassFailure 表达失败，比返回 int 更显式。
- **TVM `tvm.ir.transform`**（<https://tvm.apache.org/docs/reference/api/python/ir.html>）：读 `Sequential`、`PassContext` 与 `PassInfo`。关联 `PassManager` 的最小实现，看看工业级 pass 基础设施多了哪些元数据。
- **`code/examples/demo_e2e.py` 与 `code/mini_ai/passes.py`**：对照 demo 的五行走印与 `standard_pipeline` 的定义，确认每个数字来自哪个 pass。关联 7.2、7.3、7.4。

---

下一章进入三个最常用的图级优化：常量折叠、死代码消除与公共子表达式消除。它们看起来独立，实际共享“纯函数”与“迭代顺序”两个前提。

[第 8 章 常量折叠、DCE 与 CSE](./08-constant-folding-dce-cse.md)