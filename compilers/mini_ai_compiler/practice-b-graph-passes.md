# 练习 B：把图 pass 串成一条能验收的流水线

> **目标**：从一张 12 条 op 的固定 IR 出发，写出一条 `optimize(graph)` 流水线，把图压到 `matmul_bias_relu -> matmul_bias -> add_relu` 三条 op；同时满足四个硬指标：每轮 `validate()` 通过、数值与优化前逐位相同、第二遍跑出 0 改动、目标形态可断言。

> **环境**：Windows 10/11 x86_64，Python 3.11.12，numpy 2.4.6，torch 2.14.1+cpu。全部命令在仓库根目录 `D:\Learning-with-Claude` 执行。

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

本练习不需要 MSVC，也不需要 torch：只用 `mini_ai.ir`、`mini_ai.passes`、`mini_ai.interpreter` 三个模块。测试用例表里的构造片段沿用前文导入。

## 背景：单条 pass 能跑，组合起来才开始漏

第 7–10 章的每个 pass 单独看都能自证。真正难的是把它们串起来：谁先谁后、跑几轮、怎么证明“没改坏”。下面三个坑在本练习的固定 IR 里各埋了一个：

1. **DCE 会提前吃掉 CSE 的候选。** 固定 IR 里有一个重复的 `relu(h1)`，如果它没有任何用户，`constant_fold` 结尾的 `graph.dce()` 会先把它删掉，等 `cse` 跑到时已经没有可合并的东西。
2. **先融合再化简会漏一半。** 反过来把 `fuse_ops` 放在最前面，`add_relu` 与 `matmul_bias` 会先成形，把“三算子链”拆成两段，后面必须再融一轮才能补齐。
3. **融合的合法性取决于用户数，而用户数会被前面的 pass 改变。** `h1` 本来有两个用户（两个 `relu`），`cse` 合并之后才降到 1，`matmul + add + relu` 这时才可融。

这三条决定了流水线的顺序与“跑到不动点”的必要性。练习的验收标准不是“图变小了”，而是“每一步都能解释、每一条约束都有测试”。

## 任务规格

### 固定 IR

把下面的 `build_fixture()` 原样放进你的脚本。它包含：一对可折叠常量、一条 `x*1+0` 恒等链、一条 MLP 主链、一个重复的 `relu`、一条 `add + relu` 旁路、一条死分支。

```python
import numpy as np

from mini_ai.ir import Graph


def build_fixture(seed: int = 3) -> Graph:
    rng = np.random.default_rng(seed)
    g = Graph()
    x = g.add_input("x", (4, 8))
    one = g.add_const("one", np.ones((), np.float32))
    zero = g.add_const("zero", np.zeros((), np.float32))
    c1 = g.add_const("c1", rng.standard_normal((16,)).astype(np.float32))
    c2 = g.add_const("c2", rng.standard_normal((16,)).astype(np.float32))
    w1 = g.add_const("w1", rng.standard_normal((8, 16)).astype(np.float32))
    b1 = g.add_const("b1", rng.standard_normal((16,)).astype(np.float32))
    w2 = g.add_const("w2", rng.standard_normal((16, 4)).astype(np.float32))
    b2 = g.add_const("b2", rng.standard_normal((4,)).astype(np.float32))

    csum = g.add_op("add", [c1, c2], name="csum")
    scaled = g.add_op("mul", [x, one], name="scaled")
    shifted = g.add_op("add", [scaled, zero], name="shifted")
    mm1 = g.add_op("matmul", [shifted, w1], name="mm1")
    h1 = g.add_op("add", [mm1, b1], name="h1")
    a1 = g.add_op("relu", [h1], name="a1")
    a1b = g.add_op("relu", [h1], name="a1b")
    mm2 = g.add_op("matmul", [a1, w2], name="mm2")
    h2 = g.add_op("add", [mm2, b2], name="h2")
    z = g.add_op("add", [a1b, csum], name="z")
    act = g.add_op("relu", [z], name="act")
    dead = g.add_op("mul", [x, x], name="dead")
    g.add_output(h2, "y")
    g.add_output(act, "act")
    return g
```

`a1` 与 `a1b` 是结构完全相同的两条 `relu`：`a1` 喂给 `mm2`，`a1b` 喂给 `z`。两条都有用户，所以 DCE 不会删它们，`cse` 才有机会合并。

### 目标形态

流水线跑完后，`graph.ops` 的类型序列必须严格等于：

```python
TARGET = ["matmul_bias_relu", "matmul_bias", "add_relu"]
```

对应的 IR：

```text
ops:
  00 %a1_fused = matmul_bias_relu(x, w1, b1) : tensor<4, 16>
  01 %y_fused = matmul_bias(a1_fused, w2, b2) : tensor<4, 4>
  02 %act_fused = add_relu(a1_fused, csum_folded) : tensor<4, 16>
outputs:
  y_fused: tensor<4, 4>
  act_fused: tensor<4, 16>
```

### 要实现的接口

```python
def optimize(graph: Graph, *, max_rounds: int = 4, verbose: bool = False) -> list[list[int]]: ...
def check_target(graph: Graph) -> None: ...
```

- `optimize` 返回每一轮每条 pass 的 `changed` 列表，例如 `[[1, 2, 1, 3, 0], [0, 0, 0, 0, 0]]`。
- `check_target` 在形态不符时抛 `AssertionError`，消息里带上实际的 `kind` 列表。
- 每轮结束后 `graph.validate()` 必须通过（`PassManager.run` 已经内置了这一步）。

### 约束

- 不修改 `compilers/mini_ai_compiler/code/` 下的任何文件，只写自己的脚本。
- 不引入新依赖。
- 数值验收用优化前的 `graph.clone()` 做基线，`max_abs_error == 0.0`。
- 第二遍 `optimize` 必须返回全 0。
- 不得按输出名做断言（融合会改名）。

### 验收方式

把脚本存成仓库根目录下的 `optimize_b.py`，然后：

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
& ".scratch\aic-env\Scripts\python.exe" .\optimize_b.py
```

## 期望输出

参考解答在本机跑出的完整输出如下。行宽与空格由 `PassManager.run(verbose=True)` 的格式串决定：

```text
ops before: 12
constant_fold                    ops  12 ->  10  changed=1
algebraic_simplify               ops  10 ->   8  changed=2
cse                              ops   8 ->   7  changed=1
fuse_ops                         ops   7 ->   3  changed=3
dce                              ops   3 ->   3  changed=0
constant_fold                    ops   3 ->   3  changed=0
algebraic_simplify               ops   3 ->   3  changed=0
cse                              ops   3 ->   3  changed=0
fuse_ops                         ops   3 ->   3  changed=0
dce                              ops   3 ->   3  changed=0
rounds: [[1, 2, 1, 3, 0], [0, 0, 0, 0, 0]]
inputs:
  x: tensor<4, 8>
constants:
  w1: tensor<8, 16> numel=128
  b1: tensor<16> numel=16
  w2: tensor<16, 4> numel=64
  b2: tensor<4> numel=4
  csum_folded: tensor<16> numel=16
ops:
  00 %a1_fused = matmul_bias_relu(x, w1, b1) : tensor<4, 16>
  01 %y_fused = matmul_bias(a1_fused, w2, b2) : tensor<4, 4>
  02 %act_fused = add_relu(a1_fused, csum_folded) : tensor<4, 16>
outputs:
  y_fused: tensor<4, 4>
  act_fused: tensor<4, 16>
max err: [0.0, 0.0]
second pass changed: [0, 0, 0, 0, 0]
```

三处细节值得对照：

- `constant_fold` 的 `changed=1`，但 op 数掉了 2。`add(c1, c2)` 被折成一个常量（+1），同时 `csum` 自己从 `graph.ops` 里被删掉；`dead` 是同一轮 `dce()` 清掉的，不计入 `changed`。`changed` 统计的是“成功改写的 op 数”，不是“净减少数”，两者不必相等。
- `cse` 的 `changed=1` 之后 op 数只掉 1，因为它只合并 `a1b -> a1`，没有顺带删常量。
- `fuse_ops` 一次报 `changed=3`：`matmul+add+relu`、`matmul+add`、`add+relu` 各一处，`7 -> 3` 是 7 条里消掉 6 条、插入 3 条的结果。

## 先建立直觉：每个 pass 解锁下一个

```text
12 ops
  │
  ├─ constant_fold      csum(add(c1,c2)) 折成常量，dead(mul(x,x)) 被 DCE 清掉
  ▼  10 ops
  ├─ algebraic_simplify mul(x,1) 与 add(s,0) 消失，mm1 直接吃 x
  ▼   8 ops
  ├─ cse                a1b 合并进 a1，h1 的用户从 2 降到 1
  ▼   7 ops
  ├─ fuse_ops           模式 1 现在可匹配（h1 单用户）；模式 2、3 各一处
  ▼   3 ops
  └─ dce                已无可删
```

关键因果链只有一条：**`cse` 把 `h1` 的用户数降到 1，模式 1 的门槛才通过**。把 `cse` 移到 `fuse_ops` 后面，链条就断了；把 `constant_fold` 去掉，`csum` 会以一个 `add` op 的形态留在图里，目标形态永远达不到。

## 卡点提示

1. **`a1b` 可能根本活不到 `cse`。** 如果你把 `z` 改成读 `a1` 而不是 `a1b`，`a1b` 就成了无用户节点，`constant_fold` 结尾的 `dce()` 会直接删掉它，`cse` 报 `changed=0`。我第一版就是这样：`cse 7 -> 7 changed=0`，后面靠 `fuse_ops` 一次融三处，看起来也能到目标，但“cse 没起作用”这件事被掩盖了。想让 CSE 真正工作，重复节点必须有用户。
2. **`fuse_ops` 放最前面会漏一轮。** 实测顺序 `fuse -> cse -> fuse` 的结果是 `2, 1, 1`：第一轮融掉 `matmul_bias` 与 `add_relu`，`cse` 合并重复 `relu`，第二轮才融出 `matmul_bias_relu`。少跑第二轮，目标不达。
3. **`add_relu` 不接受 matmul 输入。** 如果你把旁路改成 `z = add(mm2, csum)`，模式 3 会被 `any(v.producer.kind == "matmul" ...)` 拒绝，`z` 与 `act` 留在图里。
4. **不要按输出名断言。** 融合把 `y` 改成 `y_fused`、`act` 改成 `act_fused`。用 `len(graph.outputs)` 与 `op.kind` 断言，用 `evaluate_named` 也拿不到旧键名。
5. **基线要用 `graph.clone()`。** `optimize` 会原地改图；先 `evaluate_graph(graph.clone(), feed)` 存基线，再优化原图，否则你比较的是同一份被改过的图。
6. **`max_rounds` 与“0 改动”是两件事。** `max_rounds` 防止死循环，“全 0 就 break”才是收敛判据。参考解答两个都写：轮数上限 4，遇到全 0 提前退出。

## 错序实测：把 `fuse_ops` 提到最前面

“顺序重要”这句话需要用数据支撑。把同一个固定 IR 交给 `fuse -> cse -> fuse` 三段手工调用，实测：

```text
fuse changed: 2
kinds: ['add', 'mul', 'add', 'matmul', 'add', 'relu', 'relu', 'matmul_bias', 'add_relu']
cse after: 1
kinds: ['add', 'mul', 'add', 'matmul', 'add', 'relu', 'matmul_bias', 'add_relu']
fuse again: 1
kinds: ['add', 'mul', 'add', 'matmul_bias_relu', 'matmul_bias', 'add_relu']
```

读这段输出要看三件事：

1. 第一轮 `fuse` 只改了 2 处。`matmul_bias`（`mm2 + b2`）与 `add_relu`（`z + act`）先成形，`matmul + add + relu` 那条链因为 `h1` 仍有两个 `relu` 用户，被门槛拒绝。
2. `cse` 合并重复 `relu` 之后，用户数才降到 1；第三行的 `fuse again: 1` 就是补上的那一次。
3. 最终 6 条 op 里还剩 `add`、`mul`、`add` —— 那是常量折叠与代数化简没跑的结果。**只调顺序、不加不动点、不补齐 pass，目标形态永远到不了。**

把这段与“期望输出”的 `[[1, 2, 1, 3, 0], [0, 0, 0, 0, 0]]` 对照，你会发现正确顺序一轮就收敛，而错误顺序需要“融两次”，且仍缺三条 op。这就是“pass 顺序本身就是优化决策”的实证。
## 分步实现建议

### 第 1 步：先让固定 IR 能建出来并跑通

只调用 `build_fixture()`、`graph.validate()`、`evaluate_graph`，确认 12 条 op、两个输出、无异常。这一步不做任何优化，目的是把基线数值拿到手。

### 第 2 步：用 `PassManager` 手写顺序

```python
pm = PassManager()
pm.add("constant_fold", constant_fold)
pm.add("algebraic_simplify", algebraic_simplify)
pm.add("cse", common_subexpression_elimination)
pm.add("fuse_ops", fuse_ops)
pm.add("dce", lambda g, **_: g.dce())
```

用 `pm.run(graph, verbose=True)` 跑一遍，把输出与“期望输出”的前五行逐字对照。对不上就先别往下走。

### 第 3 步：加不动点循环

把 `pm.run` 包进 `for _ in range(max_rounds)`，每轮记录 `[r.changed for r in records]`，全 0 时 break。注意每轮都复用同一个 `pm`：`PassManager` 本身无状态，复用安全。

### 第 4 步：写 `check_target`

三条断言：`kind` 序列、输出个数、融合 op 的输入个数（matmul 系 3 个，`add_relu` 2 个）。断言消息里打印实际值，失败时不用二次调试。

### 第 5 步：补数值与幂等验收

`max_abs_error` 对每个输出逐一比较，必须为 `0.0`；再跑一次 `optimize`，要求返回 `[[0, 0, 0, 0, 0]]`。两条都过，这个练习才算完成。

## 验收清单

动手前先抄下这张表，每完成一项打勾。它比“跑起来没报错”严格，也是你以后给别人的 pass 做 review 时的最小提问集。

| # | 检查项 | 判据 | 对应证据 |
|---:|---|---|---|
| 1 | 图的合法性 | 每轮 `optimize` 后 `validate()` 不抛异常 | `PassManager.run` 内置 |
| 2 | 目标形态 | `[op.kind for op in graph.ops] == TARGET` | `check_target` |
| 3 | 数值不变 | 每个输出 `max_abs_error == 0.0` | 优化前后各跑一次解释器 |
| 4 | 幂等 | 第二次 `optimize` 返回全 0 | `rounds[0] == [0, 0, 0, 0, 0]` |
| 5 | 不依赖输出名 | 断言里没有出现 `y_fused`、`act_fused` | 代码走查 |
| 6 | 有失败路径 | 目标不可达的图会让 `check_target` 抛错 | 用例 5 |

四项硬指标里，“数值不变”最常被漏掉：忘了 `clone()` 就会拿优化后的图跟自己比，`max_abs_error` 永远是 0。这种“永远通过”的测试比没有测试更危险。
## 参考解答

<details>
<summary>点击展开：完整可运行脚本</summary>

```python
import numpy as np

from mini_ai.interpreter import evaluate_graph, max_abs_error
from mini_ai.ir import Graph
from mini_ai.passes import (
    PassManager,
    algebraic_simplify,
    common_subexpression_elimination,
    constant_fold,
    fuse_ops,
)

TARGET = ["matmul_bias_relu", "matmul_bias", "add_relu"]


def build_fixture(seed: int = 3) -> Graph:
    rng = np.random.default_rng(seed)
    g = Graph()
    x = g.add_input("x", (4, 8))
    one = g.add_const("one", np.ones((), np.float32))
    zero = g.add_const("zero", np.zeros((), np.float32))
    c1 = g.add_const("c1", rng.standard_normal((16,)).astype(np.float32))
    c2 = g.add_const("c2", rng.standard_normal((16,)).astype(np.float32))
    w1 = g.add_const("w1", rng.standard_normal((8, 16)).astype(np.float32))
    b1 = g.add_const("b1", rng.standard_normal((16,)).astype(np.float32))
    w2 = g.add_const("w2", rng.standard_normal((16, 4)).astype(np.float32))
    b2 = g.add_const("b2", rng.standard_normal((4,)).astype(np.float32))

    csum = g.add_op("add", [c1, c2], name="csum")
    scaled = g.add_op("mul", [x, one], name="scaled")
    shifted = g.add_op("add", [scaled, zero], name="shifted")
    mm1 = g.add_op("matmul", [shifted, w1], name="mm1")
    h1 = g.add_op("add", [mm1, b1], name="h1")
    a1 = g.add_op("relu", [h1], name="a1")
    a1b = g.add_op("relu", [h1], name="a1b")
    mm2 = g.add_op("matmul", [a1, w2], name="mm2")
    h2 = g.add_op("add", [mm2, b2], name="h2")
    z = g.add_op("add", [a1b, csum], name="z")
    act = g.add_op("relu", [z], name="act")
    dead = g.add_op("mul", [x, x], name="dead")
    g.add_output(h2, "y")
    g.add_output(act, "act")
    return g


def make_pipeline() -> PassManager:
    pm = PassManager()
    pm.add("constant_fold", constant_fold)
    pm.add("algebraic_simplify", algebraic_simplify)
    pm.add("cse", common_subexpression_elimination)
    pm.add("fuse_ops", fuse_ops)
    pm.add("dce", lambda g, **_: g.dce())
    return pm


def optimize(graph: Graph, *, max_rounds: int = 4, verbose: bool = False) -> list[list[int]]:
    pm = make_pipeline()
    history: list[list[int]] = []
    for _ in range(max_rounds):
        records = pm.run(graph, verbose=verbose)
        history.append([r.changed for r in records])
        if all(r.changed == 0 for r in records):
            break
    return history


def check_target(graph: Graph) -> None:
    kinds = [op.kind for op in graph.ops]
    assert kinds == TARGET, f"expected {TARGET}, got {kinds}"
    assert len(graph.outputs) == 2, f"expected 2 outputs, got {len(graph.outputs)}"
    for op in graph.ops:
        expected_inputs = 3 if op.kind.startswith("matmul") else 2
        assert len(op.inputs) == expected_inputs, f"{op.kind} has {len(op.inputs)} inputs"


if __name__ == "__main__":
    graph = build_fixture()
    feed = {"x": np.random.default_rng(11).standard_normal((4, 8)).astype(np.float32)}
    before = evaluate_graph(graph.clone(), feed)
    print("ops before:", len(graph.ops))
    history = optimize(graph, verbose=True)
    print("rounds:", history)
    print(graph.dump())
    check_target(graph)
    after = evaluate_graph(graph, feed)
    print("max err:", [max_abs_error(a, b) for a, b in zip(after, before)])
    second = optimize(graph)
    print("second pass changed:", second[0])
```

</details>

这份解答的输出就是上一节“期望输出”里那段，逐字一致（`seed=3`、`feed` 用 `default_rng(11)`）。如果你换了 seed，`changed` 与 op 数不变，只有 `dump()` 里的常量值会变。

## 测试用例表

| # | 用例 | 构造 | 断言 | 本机实测 |
|---:|---|---|---|---|
| 1 | 主流水线 | `build_fixture()` + `optimize` | `kind` 序列等于 `TARGET`，`len(outputs) == 2` | `12 -> 3`，`rounds=[[1,2,1,3,0],[0,0,0,0,0]]` |
| 2 | 数值一致 | 优化前后各跑一次解释器 | 每个输出 `max_abs_error == 0.0` | `[0.0, 0.0]` |
| 3 | 幂等 | 对结果再跑一次 `optimize` | 返回值全 0，op 数不变 | `[0,0,0,0,0]`，仍是 3 条 |
| 4 | 模式 3 单独生效 | `add(x, c) -> relu` 两节点图 | `fuse_ops` 返回 1，kind 变 `add_relu` | `changed=1` |
| 5 | 目标不可达 | matmul 结果多一个 `sum` 用户 | `fuse_ops` 返回 0，`check_target` 抛 `AssertionError` | `changed=0`，kind 仍 4 条 |
| 6 | 恒等链清空 | `x*1` 后接 `x+0`，输出接在后者 | 优化后 0 条 op，输出就是输入 `x` | `changed=2`，`ops=[]` |

用例 5 的构造：

```python
g = Graph()
x = g.add_input("x", (2, 4))
w = g.add_const("w", np.ones((4, 3), np.float32))
b = g.add_const("b", np.ones((3,), np.float32))
mm = g.add_op("matmul", [x, w], name="mm")
add = g.add_op("add", [mm, b], name="bias")
r = g.add_op("relu", [add], name="act")
s = g.add_op("sum", [mm], {"axis": 1}, name="mm_sum")
g.add_output(r)
g.add_output(s)
print("changed:", fuse_ops(g))
print([op.kind for op in g.ops])
```

```text
changed: 0
['matmul', 'add', 'relu', 'sum']
```

这个用例的价值在于：目标形态**不可达**时，`check_target` 必须失败而不是“差不多就行”。生产流水线里同样要有“目标不达就报错”的兜底。

## 延伸挑战

1. **让顺序无关。** 写第二条流水线 `optimize_fuse_first`，顺序是 `fuse -> cse -> fuse -> fold -> simplify -> dce`，用不动点循环跑到全 0。实测中间态是 `fuse=2, cse=1, fuse=1`，最终也能到 3 条 op。比较两条流水线的轮数与总 `changed`。
2. **加一个 `report(graph)`。** 在每条 pass 前后打印 `[op.kind for op in graph.ops]`，生成一张“op 序列演变表”。用它验证“`cse` 之后 `h1` 的用户数从 2 变 1”这个因果链。
3. **给 `check_target` 加诊断。** 目标不可达时，输出“还差哪些 op”“哪些 op 多用户”。把用例 5 的失败信息从 `AssertionError` 升级成可读报告。
4. **讨论四合一。** 构造一个“`relu` 之后直接乘常量再加 bias”的链，讨论为什么本仓库的三条模式做不到四合一，以及要新增哪些 op 与匹配条件。
5. **接入内存规划。** 对优化后的图跑 `plan_memory`，对比优化前的 `arena_bytes`。记录融合减少了多少临时 buffer，并解释为什么 `peak_live_bytes` 不一定等比例下降。

## 自我检核

- [ ] 我能不看代码写出五条 pass 的顺序，并说明每条为什么在这个位置
- [ ] 我能解释 `changed` 与“op 净减少数”为什么不相等
- [ ] 我能复现“CSE 合并后 `h1` 用户数从 2 降到 1，融合才通过”这条因果链
- [ ] 我能让流水线跑到全 0，并说明 `max_rounds` 与收敛判据的分工
- [ ] 我能构造一个目标不可达的图，并让 `check_target` 失败
- [ ] 我知道为什么基线要用 `graph.clone()`，以及为什么不能按输出名断言
- [ ] 我能说出 `fuse_ops` 放在 `cse` 之前会多花几轮

---

下一章：[第 11 章 布局与 stride：shape 是视图，stride 才是内存](./11-layout-and-strides.md)