# 第 9 章 代数化简：把恒等式写成重写规则，再被浮点语义拦住

> **目标**：读懂 `algebraic_simplify` 的五组重写规则，理解 `_const_is` 的“整块常量”判定与 `replace_all_uses` 的替换语义；用真实输出证明哪些恒等式在 IEEE 754 下并不成立（`x*0`、`x-x`、`x+0`），并解释为什么同一份 IR 在 NumPy、C、LLVM 三个执行路径上会对 NaN 给出不同答案。

> **环境**：Windows 10/11 x86_64，Python 3.11.12，numpy 2.4.6，torch 2.14.1+cpu，MSVC 14.43（`cl` 19.43）。以下命令都在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

本章最后一节要编译 DLL 才能看到 C 后端的行为；`code/mini_ai/codegen_c.py` 会查找本机 `vcvars64.bat`，也可以先设置 `$env:VCVARS64`。除标注了导入的片段外，本章 Python 代码块按顺序在同一个会话中执行，后文沿用前文的导入。

## 为什么需要这个？

第 8 章的三件套（常量折叠、DCE、CSE）解决的是“结构重复”和“编译期可算”。代数化简（algebraic simplification）解决的是另一类浪费：图里存在语义上多余、但结构与常量都合法的算子。

```text
%scaled  = mul(x, one)       # one 是常量 1.0
%shifted = add(scaled, zero) # zero 是常量 0.0
%act     = relu(y)
```

`mul(x, 1.0)` 每执行一次就要读一遍 `x`、写一遍临时张量。对 `(4, 8)` 的 MLP 输入只有 128 字节，看不出代价；对 `(1024, 1024)` 的激活就是 4 MB 的读加 4 MB 的写。这类算子在 FX 抓图里很常见：权重初始化、归一化的 `x / 1.0`、某个分支被配置成恒等映射，都会留下这种节点。

麻烦在于，**代数化简是唯一一类“数学上恒成立、浮点上不一定”的 pass**。`x * 0 = 0` 在实数域成立，在 IEEE 754 下遇到 `NaN` 和 `Inf` 就错。所以这一章有两件事要同时做到：

1. 写对重写规则：匹配、替换、清理，一步不漏。
2. 知道每条规则的失效边界，并且能在真实输出上复现失效。

第 2 点比第 1 点重要。一条错误的化简规则不会崩，它会安静地返回一个数值上略有不同的结果，然后在某个下游 kernel 里放大成训练不收敛。

## 先建立直觉：三条规则的三种命运

同一张“看起来都对”的规则表，按 IEEE 754 拆开是三种不同的安全性：

```text
规则              改写              值语义安全？     失效场景
x + 0.0        ->  x                否              x = -0.0 时，-0.0 + 0.0 = +0.0
x * 1.0        ->  x                是（sNaN 除外）   signaling NaN 会被静默化
x / 1.0        ->  x                是（sNaN 除外）   同上
x * 0.0        ->  0.0              否              NaN * 0 = NaN，Inf * 0 = NaN
x - x          ->  0.0              否              NaN - NaN = NaN，Inf - Inf = NaN
relu(relu(x))  ->  relu(x)          是              relu 单调，两次与一次落点相同
```

这张表就是本章的地图。`algebraic_simplify` 实现了其中 5 条，**没有**实现 `x - x`；在实现的 5 条里，`x + 0` 与 `x * 0` 并不满足 IEEE 754 的逐位语义，其中 `x * 0` 是本仓库一处已知的保守性缺口，本章会用实测输出把它钉住。

再看一眼重写动作本身。每个 pass 都在做同一件事：

```text
匹配 (op.kind, 输入形状, 常量值)
   ↓
用已有 Value 或新常量替换 op.output 的所有使用
   ↓
从 graph.ops 里删掉这个 op
   ↓
扫尾：DCE 清掉没人用的常量与临时值
```

## 核心概念一：规则表就是“匹配 + 替换 + 删除”

`code/mini_ai/passes.py` 里的入口是 `algebraic_simplify(graph) -> int`，返回值是本轮成功改写的次数。核心结构是一个按拓扑序遍历 `list(graph.ops)` 的循环：

```python
def algebraic_simplify(graph: Graph) -> int:
    """Apply a small, deliberately conservative set of algebraic identities."""
    changed = 0
    for op in list(graph.ops):
        out = op.output
        if op.kind in {"add", "sub"} and len(op.inputs) == 2:
            a, b = op.inputs
            if _const_is(b, 0.0):
                graph.replace_all_uses(out, a)
                graph.remove_ops([op])
                changed += 1
                continue
            if op.kind == "add" and _const_is(a, 0.0):
                graph.replace_all_uses(out, b)
                graph.remove_ops([op])
                changed += 1
                continue
```

三个细节决定了它的行为：

1. `list(graph.ops)` 先拍快照。`remove_ops` 会重建 `graph.ops`，直接在原列表上遍历会跳过元素。
2. `replace_all_uses(out, a)` 同时改写所有 op 的 `inputs` 与 `graph.outputs`，并调用 `recompute_users()`。替换之后，后续 op 立刻看到新的输入。
3. 只有 `sub` 的右操作数是 0 才化简；`sub(0, x)` 保持原样，因为它等价于取负，不是恒等。

`mul` 与 `div` 分支同构，另外多了 `relu(relu(x))`：

```python
        if op.kind == "relu" and len(op.inputs) == 1:
            inner = op.inputs[0]
            if inner.producer is not None and inner.producer.kind == "relu":
                graph.replace_all_uses(out, inner)
                graph.remove_ops([op])
                changed += 1
                continue
    graph.dce()
    return changed
```

`relu` 的匹配没有常量参与，只看生产者。`inner.producer is not None` 这一句把输入值和常量排除在外：输入和常量没有 producer，不会误判。

## 核心概念二：在一张真图上跑一遍

先构造 `x * 1.0 + 0.0`，并且把结果直接当输出：

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.passes import algebraic_simplify
from mini_ai.interpreter import evaluate_graph

g = Graph()
x = g.add_input("x", (2, 3))
one = g.add_const("one", np.ones((), np.float32))
zero = g.add_const("zero", np.zeros((), np.float32))
y = g.add_op("mul", [x, one], name="scaled")
z = g.add_op("add", [y, zero], name="shifted")
g.add_output(z)

print(g.dump())
print("changed =", algebraic_simplify(g))
print(g.dump())
print(evaluate_graph(g, {"x": np.arange(6, dtype=np.float32).reshape(2, 3)})[0])
```

真实输出（本机执行）：

```text
inputs:
  x: tensor<2, 3>
constants:
  one: tensor<scalar> numel=1
  zero: tensor<scalar> numel=1
ops:
  00 %scaled = mul(x, one) : tensor<2, 3>
  01 %shifted = add(scaled, zero) : tensor<2, 3>
outputs:
  shifted: tensor<2, 3>
changed = 2
inputs:
  x: tensor<2, 3>
ops:
outputs:
  x: tensor<2, 3>
[[0. 1. 2.]
 [3. 4. 5.]]
```

两个 op 都没了，`graph.ops` 为空，输出直接指向输入 `x`。注意 `dump()` 里 `x` 同时出现在 `inputs:` 和 `outputs:` 下 —— `replace_all_uses` 把 `graph.outputs` 里的 `shifted` 换成了 `x`，而 `add_output` 当初把 `shifted.kind` 标成 `"output"`，`x` 自己的 `kind` 仍是 `"input"`。这份图是合法的：`validate()` 只要求输出值属于图。

这条“输出退化成输入”的路径会一路传到后端。`generate_c` 生成的函数体只剩一次拷贝：

```c
MINI_EXPORT int mini_forward(const float* x0, float* out0) {
    const size_t arena_bytes = 64;
#if defined(_MSC_VER)
    float* arena = (float*)_aligned_malloc(arena_bytes, 64);
#else
    float* arena = (float*)aligned_alloc(64, arena_bytes);
#endif
    if (!arena) return 1;

    const float* v_0_x = x0;

    for (int i = 0; i < 6; ++i) out0[i] = v_0_x[i];

#if defined(_MSC_VER)
    _aligned_free(arena);
#else
    free(arena);
#endif
    return 0;
}
```

编译并运行这条 DLL，`max_abs_error` 为 `0.0`。注意 `arena` 仍然被分配了 64 字节：`plan_memory` 在没有任何待分配临时值时把 `arena_bytes` 兜底到 `alignment`，`CCodegen` 又用 `max(64, self.plan.arena_bytes)` 兜底一次。arena 是空的，但指针非空。

第二个例子是幂等规则 `relu(relu(x))`：

```python
g = Graph()
x = g.add_input("x", (2, 3))
a = g.add_op("relu", [x], name="r1")
b = g.add_op("relu", [a], name="r2")
g.add_output(b)
print("changed =", algebraic_simplify(g), [op.kind for op in g.ops])
print(g.dump())
```

```text
changed = 1 ['relu']
inputs:
  x: tensor<2, 3>
ops:
  00 %r1 = relu(x) : tensor<2, 3>
outputs:
  r1: tensor<2, 3>
```

`changed=1`，`graph.ops` 只剩一个 `relu`，输出名从 `r2` 变成 `r1`。改名不是 bug，是 SSA 替换的直接结果；下游如果按名字取输出（`evaluate_named`），键会变。

## 核心概念三：哪些规则没写，为什么

`sub` 只处理 `x - 0`，不处理 `x - x`。下面这段代码的返回值是 0：

```python
g = Graph()
x = g.add_input("x", (2, 3))
y = g.add_op("sub", [x, x], name="diff")
g.add_output(y)
print("changed =", algebraic_simplify(g), [op.kind for op in g.ops])
```

```text
changed = 0 ['sub']
```

`x - x -> 0` 只差一条 `op.inputs[0] is op.inputs[1]` 的判断，但它在 `x` 为 `NaN` 或 `Inf` 时返回 `NaN`，不是 0。这条规则被有意留在外面。

反过来，`x * 0` 被实现了：

```python
            if _const_is(a, 0.0) or _const_is(b, 0.0):
                zero = graph.add_const(f"{out.name}_zero", np.zeros(out.type.shape, dtype=np.float32))
                graph.replace_all_uses(out, zero)
                graph.remove_ops([op])
```

它用一个形状与输出相同的零常量替换结果，而不是返回标量 0。形状必须与 `out.type.shape` 一致，否则广播语义会变（例如 `x` 是 `(2, 3)`、`b` 是 `(3,)` 时，标量 0 与 `(2, 3)` 的零在后续 op 里广播行为不同）。

这条规则与 `relu` 的语义不一致：`relu` 被写成保守的（只合并同构生产者），`mul` 却做了实数域才成立的改写。这不是笔误，是“先跑通、再收紧”的典型工程状态 —— 也是本章要你亲手验证的缺口。

## 底层机制：常量判定、替换、清理、装配位置

### `_const_is` 判的是“整块常量逐元素相等”

```python
def _const_is(v: Value, value: float) -> bool:
    return v.kind == "constant" and v.data is not None and bool(np.all(v.data == np.float32(value)))
```

`np.all` 意味着**所有元素**都必须等于目标值。于是：

- `ones(3)` 可以触发 `x * 1 -> x`，`changed=1`；
- `[1.0, 2.0, 3.0]` 不能，`changed=0`。

用三组常量对比（同一段代码，只换常量）：

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.passes import algebraic_simplify

cases = [
    ("mul by [1,2,3]", "mul", np.array([1, 2, 3], np.float32)),
    ("mul by ones(3)", "mul", np.ones((3,), np.float32)),
    ("div by 1.0", "div", np.ones((), np.float32)),
]
for label, kind, const in cases:
    g = Graph()
    x = g.add_input("x", (2, 3))
    c = g.add_const("c", const)
    y = g.add_op(kind, [x, c], name="y")
    g.add_output(y)
    print(f"{label}: changed={algebraic_simplify(g)} ops={[op.kind for op in g.ops]}")
```

```text
mul by [1,2,3]: changed=0 ops=['mul']
mul by ones(3): changed=1 ops=[]
div by 1.0: changed=1 ops=[]
```

第一行保留了 `mul`。如果把“逐元素恒等”和“全局常量”混为一谈，就会误以为 `x * [1,2,3]` 该被化简：只有整块都等于 1 才是恒等映射。

### 替换与删除必须成对

`replace_all_uses` 只改使用点，不删 op。删 op 靠 `remove_ops([op])`。两步都做，`changed` 才 +1。少做第二步，图里留下一个没有任何用户、却仍在 `graph.ops` 里的节点；它的输出没人读，但 `plan_memory` 仍会给它分配 buffer（`plan_memory` 遍历的是 `graph.values` 里有 producer 的值，不检查 users）。这类“幽灵分配”会让 arena 变大，且从 IR dump 上很难看出来。

少做第一步更糟：`validate()` 会报 `uses ... before it is defined` 或 `stale producer`。

### pass 的顺序：清完常量再化简

`standard_pipeline` 把化简放在常量折叠之后：

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

顺序的理由很直接：`constant_fold` 可能把一个 `add` 折成常量，而这个常量正是化简要匹配的 `0` 或 `1`。反过来，化简把 `mul(x, 1)` 删掉之后，它的常量输入 `one` 变成孤儿，`dce()` 才有东西可清。`algebraic_simplify` 自己在结尾调用 `graph.dce()`，所以即使单独调用它，孤儿常量也会被清掉。

### 单遍够不够？

在本仓库里，单遍足够处理任意长度的恒等链，原因是 op 列表按拓扑序排列：`mul(x, 1)` 先被处理，`replace_all_uses` 立刻把后续 `add` 的输入换成 `x`，等循环走到 `add` 时看到的就是 `x + 0`。`relu(relu(relu(x)))` 同理，从内到外逐个塌缩。

单遍**不够**的是跨 pass 的机会：化简产生的新常量需要再折一轮，融合产生的新 `matmul_bias` 又可能被下一轮化简影响。Practice B 会用不动点循环处理这件事。

## 浮点语义：同一条 IR，三个后端，两种答案

### `x * 0` 在 NaN/Inf 上的实测

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.passes import algebraic_simplify
from mini_ai.interpreter import evaluate_graph

g = Graph()
x = g.add_input("x", (2, 3))
zero = g.add_const("zero", np.zeros((), np.float32))
y = g.add_op("mul", [x, zero], name="zeroed")
g.add_output(y)
print("changed =", algebraic_simplify(g))
out = evaluate_graph(g, {"x": np.array([[np.nan, np.inf, -1.0], [2.0, 3.0, 4.0]], np.float32)})[0]
print(out.reshape(-1))
```

```text
changed = 1
[0. 0. 0. 0. 0. 0.]
```

未经优化时，`[[nan, inf, -1], [2, 3, 4]] * 0` 应该是 `[nan, nan, -0, 0, 0, 0]`。优化后全部变成 `0.`。这就是“数学上对、浮点上错”的完整证据链：`changed=1` 说明规则生效，输出说明结果被改变。

### `x + 0` 踩的是有符号零

`-0.0 + 0.0` 在 IEEE 754 下等于 `+0.0`，而 `x + 0 -> x` 会保留 `-0.0`。实测符号位：

```text
add0 changed: 1
before signbit: False after signbit: True
```

这个差异不会影响 `max_abs_error`（`|-0.0 - 0.0| = 0`），所以数值对齐测试抓不到它。它只在两类场景暴露：`1 / x` 得到 `-inf` 而不是 `+inf`，以及把 `signbit` 当特征用的代码。

### `relu` 的 NaN 分歧：规范里没写死的地方

同一个只含 `relu` 的图，喂 `[nan, -0.0, inf, -1.0]`：

```text
interpreter: [nan  0. inf  0.]
c:           [ 0.  0. inf  0.]
llvm:        [ 0.  0. inf  0.]
```

解释器用 `np.maximum(x, 0.0)`，NumPy 的 `maximum` 对 NaN 返回 NaN。C 后端用 `fmaxf(x, 0.0f)`，LLVM 后端用 `fcmp_ordered(">", x, 0.0)` 再 `select`，两者对 NaN 都返回 0。三个执行路径对同一份 IR 给出不同答案。

这条不是化简 pass 的 bug，但它决定了化简规则的安全边界：`relu(relu(x)) -> relu(x)` 之所以安全，恰恰是因为两个后端的 `relu` 都把 NaN 映射到 0，而解释器把 NaN 映射到 NaN，两次应用与一次应用结果相同（`relu(relu(nan)) = nan`，`relu(nan) = nan`）。规则的安全性依赖的是“幂等”，不是“NaN 无关”。

生产编译器会把这类差异显式化：LLVM 的 `fast-math` flag、MLIR 的 `arith.fastmath`、以及 NumPy 的 `np.errstate`，都是在说“我接受哪一档语义”。本课程的 IR 没有这个字段，所以默认策略只能是保守。

## 进一步用法与变体

**变体 1：给规则加开关。** 把 `x*0` 从默认路径挪到 `algebraic_simplify(graph, *, assume_finite=False)`，只在调用方声明输入有限时启用。真实编译器用属性（`nnan`、`ninf`）而不是布尔参数，但语义一样。

**变体 2：让折叠与化简化交替。** 一轮 `constant_fold` + `algebraic_simplify` 之后再跑一轮，能覆盖“化简产生新常量、新常量又能被折”的链。Practice B 的不动点循环就是这件事的通用版本。

**变体 3：把规则写成表。** 现在每个分支是手写的 `if`。改成 `(kind, pattern, replacement)` 的规则列表，匹配器统一处理，新增规则不需要改主循环。代价是调试时栈更深，需要给每条规则一个名字用于日志。

**变体 4：用生产者信息做更强的化简。** `relu` 分支已经读 `inner.producer.kind`，这条路径可以扩展成小模式匹配。注意 `relu(x + c) != relu(x) + c`，这类改写不能用；真正可行的是 `exp(0) -> 1`、`mul(x, 2) -> add(x, x)` 这类常数级改写，收益主要在下游 kernel 的匹配。

## 对比与取舍

| 取舍点 | 保守（当前实现） | 激进（fast-math） |
|---|---|---|
| `x*0` | 不做才对；当前实现做了 | 做，并假设无 NaN/Inf |
| `x+0` | 做，接受 `-0.0` 差异 | 做，接受同样的差异 |
| `x-x` | 不做 | 做，假设有限 |
| 重结合 `(a+b)+c` | 不做 | 做，误差可能变大 |
| 收益 | 数值可复现，跨后端差异可控 | 图更小，下游融合机会更多 |
| 代价 | 漏掉一部分优化 | 需要一整套“输入有限”的证明或用户声明 |

第二张表是“写在哪一层”的取舍：

| 方案 | 优点 | 缺点 |
|---|---|---|
| 图级重写（本章） | 与后端无关，一次改写所有后端受益 | 看不到循环/寄存器，收益上限低 |
| 调度级重写（TVM schedule） | 能改循环、tile、向量化 | 与具体后端绑定，pass 不可复用 |
| 后端优化器（`/O2`、LLVM `-O3`） | 免费，成熟 | 看不到图级语义（哪些是 bias、哪些是激活） |

本课程三层都有：本章是图级，第 17–19 章是循环与编译选项，第 21–22 章是 LLVM。三层会重复做一部分工作，这很正常；工业编译器也这样。

## 踩雷集锦

1. **把 `x * 0` 当安全恒等式**：本章实测 `[nan, inf, -1, 2, 3, 4] * 0` 被改成全 0。只要输入可能来自 `div`、`log`、`exp` 溢出，这条规则就会改答案。要么不做，要么先证明有限。
2. **以为 `x + 0` 逐位不变**：`-0.0 + 0.0 = +0.0`，改写后保留 `-0.0`。`max_abs_error` 抓不到，`np.signbit` 能抓到。
3. **用 `==` 判断浮点常量**：`_const_is` 用的是 `np.all(v.data == np.float32(value))`。对 `NaN` 常量这条永远为假（`nan != nan`），所以“把 NaN 常量折叠掉”这类规则不能靠它匹配；要先判 `np.isnan`。
4. **边遍历边删 op**：`for op in graph.ops` 配合 `remove_ops` 会跳过元素。本实现用 `list(graph.ops)` 快照，写新规则时要照抄这个习惯。
5. **替换了但没删 op**：图里留下无用户的 op，`plan_memory` 照样给它分配 buffer，arena 悄悄变大，dump 里也看不出它已经死了。`replace_all_uses` 与 `remove_ops` 必须成对出现。

## 进阶：再往深一层

- **NaN 传播分析**：给每个 `Value` 挂一个 `may_be_nan` 位，`exp`/`div`/`log` 的输出置位，`relu` 的输出清零（在当前后端语义下 `relu` 吃掉 NaN）。有了这个位，`x*0` 才能在“确定非 NaN”时启用。这是范围分析（range analysis）的最小版本。
- **IEEE 754 的 total order**：`np.maximum` 与 `fmaxf` 的差异来自“NaN 是否参与比较”。`maximum` 传播 NaN，`fmax` 忽略 NaN。想统一语义，就要在 IR 层把 `relu` 定义成 `max(x, 0)` 的一种具体实现，而不是留给后端。
- **canonicalization 的不动点**：MLIR 的 canonicalizer 会把 pattern 反复跑到不动点，并且要求每条 pattern 严格降低某种度量，否则会死循环。本章的单遍实现没有这个保证，写新规则时要自己确认“改写后不会产生能再次匹配同一规则的结构”。
- **fast-math 的传递性**：LLVM 里 `fast` 标记会沿着指令传播，一个 `nnan` 可能让上游的 `fcmp` 也变成无序比较。图级 pass 引入同类标记时，要考虑它如何穿过融合后的 op。
- **源码对照**：读 `code/mini_ai/passes.py` 全文（约 270 行），重点看 `_const_is`、`algebraic_simplify`、`constant_fold` 三处的常量处理差异 —— 它们对“常量”的定义并不完全一致。

## 动手练习

### 练习 1：加一条 `x - x -> 0`，并证明它不安全

在 `algebraic_simplify` 的 `sub` 分支里加：

```python
if a is b:
    zero = graph.add_const(f"{out.name}_zero", np.zeros(out.type.shape, dtype=np.float32))
    graph.replace_all_uses(out, zero)
    graph.remove_ops([op])
    changed += 1
    continue
```

然后构造 `sub(x, x)`，喂 `x = [nan, inf, 1.0]`，对比加规则前后的输出。要求：

- 加规则前输出 `[nan, nan, 0.]`；
- 加规则后输出 `[0., 0., 0.]`；
- 用 `np.array_equal(..., equal_nan=True)` 断言两者不同。

这道题的目的是让你亲手制造一次“优化改变数值”，而不是只在书上看到。

### 练习 2：给 `x * 0` 加开关

把 `algebraic_simplify` 的签名改成 `algebraic_simplify(graph, *, assume_finite: bool = False)`，`x*0` 分支只在 `assume_finite=True` 时生效。要求：

- 默认参数下，`mul(x, 0)` 的 `changed` 为 0；
- `assume_finite=True` 时为 1；
- `standard_pipeline` 仍然用默认值，跑 `demo_e2e.py` 的输出与改动前一致（`fuse_ops 5 -> 2 changed=2`）。

### 练习 3：数一数单遍能塌缩几层

构造 `relu(relu(relu(relu(x))))`，在 `algebraic_simplify` 内部打印每次匹配时的 `op.output.name`。回答两个问题：

1. 一次调用能删掉几个 `relu`？
2. 如果 op 列表是逆拓扑序（手工构造 `graph.ops.reverse()`），会发生什么？

第 2 问会触发 `validate()` 的 `uses ... before it is defined`，这正好说明“拓扑序”不是可选项。

### 练习 4：让 `_const_is` 支持 NaN 常量

写 `_const_is_nan(v)`，用 `np.all(np.isnan(v.data))` 判断，然后新增规则 `mul(x, nan) -> nan_const`。注意：结果形状必须与 `out.type.shape` 一致，不能直接用 `v`（`v` 可能是 `(3,)`，输出是 `(2, 3)`）。写完后用解释器验证 `NaN` 仍然传播。

## 本章重点

- `algebraic_simplify` 按拓扑序单遍扫描，匹配 `(kind, 常量)`，用 `replace_all_uses` + `remove_ops` 成对改写，结尾 `dce()` 清孤儿。
- `_const_is` 要求整块常量逐元素相等，所以 `ones(3)` 触发化简，`[1,2,3]` 不触发。
- 输出可以退化成输入（`graph.outputs` 里的值 `kind` 仍是 `"input"`），后端会生成一次纯拷贝。
- `x*0` 在 NaN/Inf 下不成立，而当前实现做了这条改写；`x-x` 不成立，当前实现没做。两者体现了同一份代码里保守与激进并存。
- `x+0` 改动的是 `-0.0` 的符号位，数值对齐测试抓不到。
- 同一份 IR 在 NumPy / C / LLVM 上对 `relu(NaN)` 给出不同答案，所以“安全”必须绑定到具体后端语义。

## 自我检核

- [ ] 我能说出 `algebraic_simplify` 实现了哪 5 组规则，以及每组匹配的条件
- [ ] 我能解释 `_const_is` 为什么对 `[1,2,3]` 返回 False
- [ ] 我能复现 `mul(x, 0)` 把 `NaN/Inf` 变成 0 的实测输出
- [ ] 我能解释 `x + 0 -> x` 对有符号零的影响，并知道用 `np.signbit` 验证
- [ ] 我知道输出退化成输入时，C 后端会生成什么代码
- [ ] 我能解释为什么单遍扫描在拓扑序下足够，以及跨 pass 为什么需要不动点
- [ ] 我能说出至少两个“化简后必须调用 DCE”的理由

## 延伸阅读

- **IEEE 754-2019 浮点标准**（https://ieeexplore.ieee.org/document/8766229）：读第 6 章 （NaN、有符号零、无穷的运算规则）。与本章的关联：`x*0`、`x-x`、`x+0` 三条规则为什么不成立。
- **`numpy.maximum` 文档**（https://numpy.org/doc/stable/reference/generated/numpy.maximum.html）：读 Notes 一节对 NaN 传播的说明。与本章的关联：解释器与 C 后端 `relu` 分歧的来源。
- **C 标准库 `fmax`**（https://en.cppreference.com/w/c/numeric/math/fmax）：读“If one argument is NaN, the other is returned”。与本章的关联：`codegen_c.py` 里 `fmaxf(acc, 0.0f)` 的语义依据。
- **MLIR Canonicalization**（https://mlir.llvm.org/docs/Canonicalization/）：读“Pattern Rewriting”与“Application”两节，注意它如何保证 driver 收敛。与本章的关联：把本章的单遍循环扩展成不动点 pattern 系统。
- **TVM `FoldConstant` 与 `CanonicalSimplify`**（https://tvm.apache.org/docs/）：搜 `tir.transform.Simplify`。与本章的关联：同一组恒等式在 TIR 上的实现，以及它对 `fastmath` 的处理。

---

下一章：[第 10 章 算子融合：少一次内存往返，还是多一份寄存器压力](./10-operator-fusion.md)