# Practice B：再写一个自定义 Pass，并用结构断言锁住它

> **目标**：独立完成一次"从改写点识别到验收"的完整练习：实现 `neg(neg(x)) -> x` 的 post-grad pass；用三层参照做差分测试；用结构断言证明改写生效；再回答"为什么不顺手把 `(x+a)+b` 也折成 `x+(a+b)`"。

> **环境**：Windows 10 x86_64；Python 3.11.12（`.scratch/aic-env`）；`torch 2.14.1+cpu`；MSVC 14.43（`cl.exe`）。以下命令都在仓库根目录执行：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 .scratch\practice_b_neg.py
> ```

## 为什么

第 7 章的 lab_b 已经把流程走通了一遍。但"照着改一个字符串"和"能独立完成一次改写"之间隔着一层东西：你要自己回答**匹配什么、为什么等价、怎么证明、边界在哪**。

本练习选 `neg(neg(x)) -> x`，原因和上一章选 `relu(x)+relu(-x)` 一样，但更极端：

- 它**严格恒等**，连"加 0"都没有，浮点上也逐位相等；
- 它**只删节点、不加节点**，所以 `tmp_ops` 的下降是干净的 2 条；
- 它**有天然的负例**：单个 `neg`、`neg(sin(x))` 都不该被改写，用来检验你的匹配条件是否过宽。

而"顺手把 `(x+a)+b` 也折了"是一个陷阱。它在数学上成立，在 float32 上不成立。练习最后会让你亲手构造反例，把"等价"这件事从直觉拉到可验证的层面。

## 主线

### B.1 任务与验收标准

在 `lab_b_pass.py` 的基础上新建 `.scratch\practice_b_neg.py`，实现一个 post-grad pre pass，把相邻的两层 `aten.neg.default` 折叠掉，并完成自测。

验收标准（全部要满足）：

| 编号 | 标准 | 判定方式 |
| --- | --- | --- |
| A1 | 改写后与 eager 的误差 ≤ 1e-6 | `common.assert_close(..., tol=1e-6)` |
| A2 | 改写前后两次**编译**的输出逐位相同 | `torch.equal(base_out, opt_out) is True` |
| A3 | `tmp_ops` 至少下降 2 | `kernel_stats` 对比 |
| A4 | 改写后的 kernel 循环体里没有 `.neg()` | 在 `extract_kernel` 结果里查 `".neg()"` |
| A5 | kernel 数不变（仍是 1） | `metrics.generated_kernel_count` |
| A6 | 单个 `neg`、`neg(sin(x))` 不被改写 | 负例模型各编译一次，`tmp_ops` 不变 |
| A7 | 图合法 | pass 结尾调用 `graph.lint()`，不抛异常 |

注意 A6 需要额外两次编译，时间上不划算时可以先只做 A1–A5，把 A6 作为"读代码推理"完成：匹配条件里有 `inner.target is aten.neg.default`，单个 `neg` 的输入是 `sin`，不会命中。

### B.2 工程骨架

先用这个骨架把结构搭好，再填 pass：

```python
"""Practice B：neg(neg(x)) -> x。"""
import torch
import torch._inductor.config as cfg
from torch._inductor.utils import fresh_inductor_cache
from labs import common

aten = torch.ops.aten


def neg_neg_to_identity(graph):
    # TODO: 遍历、匹配、替换、清理、lint
    ...


def model(x):
    return torch.neg(torch.neg(torch.sin(x)))


x = torch.randn(1024)
ref = model(x)


def compile_once(patches, tag):
    torch._dynamo.reset()
    with fresh_inductor_cache(), cfg.patch(patches):
        before = common.modules_snapshot()
        fn = torch.compile(model, fullgraph=True)
        out = fn(x)
        mods = common.modules_since(before)
        src = common.read_text_any(mods[-1].__file__)
    return fn, out, src


base_fn, base_out, base_src = compile_once({}, "baseline")
opt_fn, opt_out, opt_src = compile_once(
    {"post_grad_custom_pre_pass": neg_neg_to_identity}, "custom"
)
```

骨架里的两个决策值得先想清楚：

- **模型用 `neg(neg(sin(x)))`**，不用 `neg(neg(x))`。如果整个模型只有两层 `neg`，输出就是 `x`，Inductor 可能直接把整张图优化成一次拷贝，`tmp_ops` 的对照就失去意义。加一层 `sin` 是为了让 kernel 里留下可观测的"真实计算"。
- **`compile_once` 里 `fn(x)` 必须在 `cfg.patch` 内**。第 7 章 7.6 讲过：`torch.compile` 惰性，编译发生在第一次调用。

### B.3 四个必须自己回答的问题

**问题一：从哪一层节点出发匹配？**

可以从外层 `neg` 出发，也可以从内层出发。从外层出发的好处是"一次替换就能收敛"：外层 `neg` 的所有使用者直接改用 `x`，内层与它一起变成死节点。从内层出发则要判断内层的使用者是否恰好只有一个 `neg`，还要处理多使用者，逻辑更绕。

**问题二：怎么确认 `x` 是同一个节点？**

`inner.args[0]` 就是被两次取负的那个值。不需要额外比较——因为你在匹配的就是"外层的输入恰好是内层的输出"，链条天然共享同一个源。这一点和 lab_b 不同：那里要额外检查 `neg.args[0] is a.args[0]`，因为两个 `relu` 可能读不同的张量。

**问题三：三连 `neg` 怎么办？**

`neg(neg(neg(x)))` 数学上等于 `neg(x)`。按"从外层出发、边遍历边替换"的写法，遍历到中间那层时外层会被接上 `x`，随后外层不再满足"输入是 `neg`"的条件，于是最终留下一个 `neg`——结果正确。但这是**遍历顺序的副作用**，不是设计出来的不变量。想让它稳定，可以先把所有匹配对收集到列表，再统一替换（"先收集后提交"）。

**问题四：清理与校验的顺序？**

先 `replace_all_uses_with`，再 `eliminate_dead_code()`，最后 `lint()`。顺序反了会怎样：先 `lint` 再 DCE，孤立节点还在，`lint` 也能过，但你会带着死节点继续后面的流程；先 DCE 再替换则没有意义，因为替换会制造新的死节点。

### B.4 差分测试的三层参照

一个改写要证明"没改坏"，参照物不能只有一层：

| 层 | 参照 | 能证明什么 | 本例取值 |
| --- | --- | --- | --- |
| L1 | eager `model(x)` | 语义没跑偏 | 误差 `5.960e-08` |
| L2 | 未改写编译产物 | 改写没引入额外数值变化 | `torch.equal(...) == True` |
| L3 | 结构特征 | 改写确实发生了 | `tmp_ops` 4 → 2 |

只做 L1 会把"向量化 `sin` 与 eager 的差异"误判成改写 bug；只做 L2 无法排除"两次都被同一个错误优化"；只做 L3 无法保证语义。三层一起，才有完整证据链。

L1 的容差怎么定？本例里 `sin` 在 Inductor 里走 `at::vec::Vectorized<float>::sin`，与 eager 的标量实现有约 `6e-8` 的差异，取 `1e-6` 有足够余量又不会掩盖真实错误。**不要因为"数学上恒等"就把 L1 的容差设成 0**——除非你确认两条路径的实现完全一致。

### B.5 参考实现

<details>
<summary>点开看 `neg_neg_to_identity` 与完整自测脚本</summary>

```python
"""Practice B 参考实现：neg(neg(x)) -> x。"""
import torch
import torch._inductor.config as cfg
from torch._inductor.utils import fresh_inductor_cache
from labs import common

aten = torch.ops.aten


def neg_neg_to_identity(graph):
    removed = 0
    for node in list(graph.nodes):
        if node.op != "call_function" or node.target is not aten.neg.default:
            continue
        inner = node.args[0]
        if not (isinstance(inner, torch.fx.Node) and inner.op == "call_function"
                and inner.target is aten.neg.default):
            continue
        node.replace_all_uses_with(inner.args[0])
        removed += 1

    graph.eliminate_dead_code()
    graph.lint()
    return removed


def model(x):
    return torch.neg(torch.neg(torch.sin(x)))


x = torch.randn(1024)
ref = model(x)


def compile_once(patches, tag):
    torch._dynamo.reset()
    with fresh_inductor_cache(), cfg.patch(patches):
        before = common.modules_snapshot()
        fn = torch.compile(model, fullgraph=True)
        out = fn(x)
        mods = common.modules_since(before)
        src = common.read_text_any(mods[-1].__file__)
    body = common.extract_kernel(src)
    stats = common.kernel_stats(src)
    print(f"[{tag}] tmp_ops={stats['tmp_ops']} "
          f"has_neg={'.neg()' in body} max_err={(out - ref).abs().max().item():.3e}")
    return fn, out


base_fn, base_out = compile_once({}, "baseline")
opt_fn, opt_out = compile_once(
    {"post_grad_custom_pre_pass": neg_neg_to_identity}, "custom"
)

print("exact equal baseline vs opt:", torch.equal(base_out, opt_out))
print("opt still correct:", torch.allclose(opt_out, ref, atol=1e-6))
```

关键点：

- `list(graph.nodes)`：遍历期间会删节点。
- `node.target is not aten.neg.default`：图里是 overload，不是 packet。
- `inner.args[0]` 直接作为替换值：不需要新建节点，因为 `neg(neg(v))` 的结果就是 `v` 本身。
- 返回值 `removed` 只用于观察；Inductor 不看它（第 7 章 7.1）。
</details>

### B.6 实测结果

<details>
<summary>点开看参考实现的本机输出</summary>

```text
[baseline] tmp_ops=4 has_neg=True max_err=5.960e-08
[custom] tmp_ops=2 has_neg=False max_err=5.960e-08
exact equal baseline vs opt: True
opt still correct: True
```

对应关系：

- `tmp_ops 4 → 2`：`load`、`sin`、`neg`、`neg` → `load`、`sin`；
- `has_neg True → False`：循环体里 `.neg()` 消失；
- `max_err` 两行相同：差异来自向量化 `sin`，与改写无关；
- `exact equal ... True`：两次编译逐位相同，改写没有引入任何数值变化。

如果只看到 `tmp_ops=4` 两次，先检查三件事：`fn(x)` 是否在 `cfg.patch` 内、是否调用了 `torch._dynamo.reset()`、是否用了 `fresh_inductor_cache()`。
</details>

### B.7 常见错误对照

<details>
<summary>点开看五种典型错误与现象</summary>

| 写法 | 现象 | 原因 |
| --- | --- | --- |
| `gm.graph` | `AttributeError: 'Graph' object has no attribute 'graph'` | 回调收到的是 `Graph` |
| `node.target is aten.neg` | 从不命中，`tmp_ops` 不变 | 图里是 `aten.neg.default` |
| 直接 `for node in graph.nodes` | 偶尔漏改、偶发 `RuntimeError` | 遍历时删除节点 |
| 不调 `eliminate_dead_code()` | 数值对，但 `tmp_ops` 仍偏大 | 死节点仍被 lowering |
| 把 `ref` 写成 `torch.sin(x)` | 自测"通过"但什么都没验证 | 用改写后的语义当参照，循环论证 |
</details>

### B.8 进阶：为什么不要顺手折 `(x+a)+b`

`(x+a)+b` 与 `x+(a+b)` 在实数上恒等，在 float32 上不是。可验证的反例：

```python
import torch
x = torch.tensor([1.0])
a = torch.tensor([16777216.0])   # 2**24
b = torch.tensor([2.0])
left = (x + a) + b
right = x + (a + b)
print(left.tolist(), right.tolist(), torch.equal(left, right))
```

本机实测：

```text
[16777218.0] [16777220.0] False
```

原因：`2**24` 附近 float32 的间隔是 2，`1 + 2**24` 会舍入回 `2**24`；两种结合顺序舍入的时机不同，最终差 2。

所以这条改写只有在下面任一条件下才可以考虑：

- 常量是 2 的幂且不涉及溢出/下溢（缩放类改写，且要证明范围）；
- 改写只发生在整数/定点语义上；
- 你接受"数值不再逐位相等"，并把 L1 容差放宽、把 L2 从 `torch.equal` 降级为 `allclose`——同时要在报告里写清风险。

练习里请把这条改写**标记为不推荐**，并写下你选择 `neg(neg)` 的理由。这正是"编译器优化必须对浮点保持保守"的一个具体例子。

### B.9 提交清单

- [ ] `.scratch\practice_b_neg.py` 可独立运行，退出码 0；
- [ ] A1–A5 全部通过，输出里能看到 `tmp_ops` 与 `has_neg` 的对照；
- [ ] 用一段话解释 L1/L2/L3 三层参照各自的必要性；
- [ ] 用一段话解释三连 `neg` 的行为，以及"先收集后提交"如何让它稳定；
- [ ] 写出 `(x+a)+b` 的反例数值（`16777218.0` 与 `16777220.0`）与舍入原因；
- [ ] 没有把 `ref` 写成改写后的等价式。

## 可跑实验

把 B.5 里的脚本保存为 .scratch\practice_b_neg.py（它已经包含完整自测），然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 .scratch\practice_b_neg.py
```

预期输出（参考实现，首次编译约 10–20 秒，两次编译共约 30 秒）：

```text
[baseline] tmp_ops=4 has_neg=True max_err=5.960e-08
[custom] tmp_ops=2 has_neg=False max_err=5.960e-08
exact equal baseline vs opt: True
opt still correct: True
```

进一步练习：把模型换成 `torch.neg(torch.sin(x))`，预期两次都是 `tmp_ops=3`、`has_neg=True`——匹配条件的宽度检查。再换成 `torch.neg(torch.neg(torch.neg(torch.sin(x))))`，观察 `tmp_ops` 是否回到 3（保留一层 `neg`），并解释为什么。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `compilers/inductor_deep_dive/code/labs/lab_b_pass.py` | `relu_pair_to_abs`、`compile_variant` | 本章骨架的来源；对照"新建节点"与"直接复用输入"两种替换 |
| `torch/fx/graph.py` | `Node.replace_all_uses_with`、`Graph.eliminate_dead_code`、`Graph.lint` | 替换、清理、校验三件套的语义 |
| `torch/_inductor/custom_graph_pass.py` | `CustomGraphPassType`、`get_custom_graph_passes` | 回调签名与列表支持 |
| `torch/_inductor/fx_passes/post_grad.py` | custom pre pass 调用处 | 你的 pass 在管线中的位置 |
| `compilers/inductor_deep_dive/code/labs/common.py` | `kernel_stats`、`extract_kernel`、`assert_close` | 结构断言与数值断言的取数方式 |

## 常见坑

1. **模型太简单，改写无从观察。** 若模型就是 `neg(neg(x))`，整图可能被优化成一次拷贝，`tmp_ops` 对照失效。留一层 `sin` 或 `add` 作为"真实计算"。
2. **参照写成了改写后的语义。** 用 `torch.sin(x)` 当 `ref` 等于自证。参照必须是未改写的 `model(x)`。
3. **`torch.equal` 与 `allclose` 混用。** L2 用 `torch.equal`（逐位），L1 用 `allclose`（容差）。把 L2 也放成 `allclose`，就丢掉了"改写没有引入数值变化"这条最强证据。
4. **三连 `neg` 依赖遍历顺序。** 边遍历边替换能得到正确结果，但那是顺序的副作用。需要稳定行为时，先收集匹配对再统一提交。
5. **忘了 `lint()`。** 图结构错了，报错会推迟到 lowering，堆栈里全是 `ir.py` 的行，和你的 pass 无关。
6. **在 CPU 上期待 `<2e-7` 的误差。** `sin` 的向量化实现与 eager 不是同一份代码，`5.96e-08` 是常态。容差按实测定，不按直觉定。

## 自我检查

- [ ] 我能不看参考实现，写出 `neg(neg(x)) -> x` 的 pass，并解释每一行为什么必要。
- [ ] 我能说清 L1/L2/L3 三层参照各自排除了哪类错误。
- [ ] 我能解释为什么参考实现直接复用 `inner.args[0]`，而 lab_b 要新建 `abs` 节点。
- [ ] 我能预测三连 `neg` 的最终形态，并说明结果为何依赖遍历顺序。
- [ ] 我能复现 `(x+a)+b` 的 float32 反例，并解释 `2**24` 附近的舍入间隔。
- [ ] 我知道 `tmp_ops` 下降不等于性能提升，性能要用交错测量 + 子进程隔离验证。
- [ ] 我能列出至少三个"改写没生效"的排查点（patch 作用域、dynamo reset、缓存目录）。

## 延伸阅读

- **`torch/fx/graph.py` 的 `Node`/`Graph` API**（<https://pytorch.org/docs/stable/fx.html>）：读 `replace_all_uses_with`、`eliminate_dead_code`、`lint` 的文档，确认各自维护哪些不变量。
- **浮点结合律与舍入**：IEEE 754 的 round-to-nearest-even 规则是本章反例的根源；读任意一本数值计算教材中"浮点加法不满足结合律"的小节，并用本机 float32 复现。
- **`compilers/inductor_deep_dive/code/labs/lab_b_pass.py`**：把"新建节点 + 替换"与本章"直接复用输入 + 替换"两种写法对照，理解替换值的来源可以是什么。
- **`torch/_inductor/custom_graph_pass.py`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/custom_graph_pass.py>）：完成练习后读 `uuid()` 的说明，思考怎样让这个 pass 参与 FxGraphCache。

---

上一章：[Pattern Matcher 与融合](./08-pattern-matcher-and-fusions.md) ｜ 下一章：进入调度器，看融合决策建立在什么数据结构上。

[上一章：Pattern Matcher 与融合](./08-pattern-matcher-and-fusions.md) | [下一章：调度器数据结构](./09-scheduler-data-structures.md)