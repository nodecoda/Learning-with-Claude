# Ch 7 写一个自定义 Pass：从 relu 对到 abs

> **目标**：读完本章，你能独立写一个 `post_grad_custom_pre_pass`：知道回调拿到的是 `torch.fx.Graph` 而不是 `GraphModule`，知道 `inserting_before`、`call_function`、`replace_all_uses_with`、`eliminate_dead_code`、`lint` 各自负责什么；能解释为什么教材例子选 `relu(x) + relu(-x) -> abs(x)` 而不是 `x * 1`；能用"数值 + 结构"两类断言证明改写生效；并知道什么时候该改用第 8 章的 pattern matcher。

> **环境**：Windows 10 x86_64；Python 3.11.12（`.scratch/aic-env`）；`torch 2.14.1+cpu`；MSVC 14.43（`cl.exe`）。本章命令在仓库根目录执行：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py
> ```

## 为什么

编译器不认识你的领域等式。它知道 `relu` 是 `clamp_min(x, 0)`，也知道 `neg` 是取负，但它不会主动把 `relu(x) + relu(-x)` 看成 `abs(x)`——这是三条指令换一条的等价关系，需要有人告诉它。

图级改写是成本最低的介入方式：不用碰调度器，不用写 C++，只要在正确的时机把图换成等价形式，后面的融合、向量化、内存规划全部自动继承。

但"正确的时机 + 正确的图变换"有两道坎：

1. **对象不对**。很多教程写 `gm.graph`，而 Inductor 的回调给的是 `graph`。第一行代码就 `AttributeError`，然后开始怀疑人生。
2. **例子不对**。随手写个 `x * 1 -> x`，跑起来发现 pass 里根本没看到 `mul`——因为它在到达你之前已经被折叠了。于是误以为"改写没生效"，其实是没有东西可改。

本章用 lab_b 把这两道坎一次走完，并给出可复现的数字：5 条向量指令变 2 条，误差严格为 0。

## 主线

### 7.1 接口：一个可调用对象，收到一张图

配置项是 `torch._inductor.config.post_grad_custom_pre_pass`，类型别名在 `torch/_inductor/custom_graph_pass.py`：

```python
CustomGraphPassCallable = CustomGraphPass | Callable[[torch.fx.graph.Graph], None]
CustomGraphPassType = (
    CustomGraphPassCallable | list[CustomGraphPassCallable]
    | tuple[CustomGraphPassCallable, ...] | None
)
```

三个要点：

- **参数是 `Graph`**，因为 `GraphTransformObserver.apply_graph_pass` 传的是 `self.gm.graph`（第 6 章）。
- **可以传列表**。`get_custom_graph_passes` 会把 `None`、单个 callable、list/tuple 统一成 tuple，然后按顺序执行。多个 pass 组合时不要自己嵌套调用，交给列表。
- **返回值没人看**。`apply_graph_pass` 会把 `pass_fn` 的返回值原样返回，但 `post_grad_passes` 不接。写 `return graph` 是习惯问题，不是契约；按类型别名写 `-> None` 更诚实。

同一个接口还有 `post_grad_custom_post_pass`（第 6 章表的第 17 行）。选 pre 还是 post，等于选"我要在常规 pattern 化简之前还是之后动手"。

### 7.2 五个 API，各管一件事

lab_b 的 pass 只用了五个图操作，逐个说清：

| API | 作用 | 用错会怎样 |
| --- | --- | --- |
| `graph.nodes` | 节点迭代器 | 边遍历边插/删会漏节点，必须先 `list(...)` 快照 |
| `node.op` / `node.target` | 节点种类与算子 | `target` 是 `OpOverload` 对象，用 `is` 比较；拿 packet 比会失败 |
| `graph.inserting_before(node)` | 上下文管理器，指定后续插入点 | 不包住 `call_function` 就插到图尾，可能在使用者之后 |
| `node.replace_all_uses_with(new)` | 把所有使用者的输入换成 `new` | 只改一部分使用者，会留下半新半旧的图 |
| `graph.eliminate_dead_code()` | 删掉没有使用者的节点 | 不删则孤立的 `relu`/`neg` 仍留在图里，白占 lowering 开销 |
| `graph.lint()` | 检查节点顺序、`users`、`args` 一致性 | 不查则错误延后到 lowering 才炸，报错点离原因很远 |

两个细节值得强调。

**`inserting_before` 是上下文管理器，不是一次性开关。** 它的实现是在 `with` 块内临时改 `graph._insert`，退出时恢复。所以下面这种写法是错的：

```python
graph.inserting_before(node)          # 没有 with，什么也没发生
new = graph.call_function(...)        # 插到图尾
```

**`node.target is aten.relu.default` 里的 `is` 是有意的。** FX 图里存的 `target` 就是注册表里的那个 `OpOverload` 单例，身份比较比 `==` 更快也更严格。但要注意 packet 与 overload 的区别：`aten.relu` 是 packet，`aten.relu.default` 是 overload；图里出现的是后者。

### 7.3 lab_b 的改写，逐行读

核心函数只有二十来行：

```python
def relu_pair_to_abs(graph):
    names = [str(getattr(n, "target", n.op)) for n in graph.nodes]
    SEEN.setdefault("graphs", []).append(names)

    for node in list(graph.nodes):
        if node.op != "call_function" or node.target is not aten.add.Tensor:
            continue
        a, b = node.args[:2]
        if not (isinstance(a, torch.fx.Node) and isinstance(b, torch.fx.Node)):
            continue
        if a.op != "call_function" or a.target is not aten.relu.default:
            continue
        if b.op != "call_function" or b.target is not aten.relu.default:
            continue
        neg = b.args[0]
        if not (isinstance(neg, torch.fx.Node) and neg.op == "call_function"
                and neg.target is aten.neg.default):
            continue
        if neg.args[0] is not a.args[0]:
            continue
        with graph.inserting_before(node):
            new = graph.call_function(aten.abs.default, (a.args[0],))
        node.replace_all_uses_with(new)

    graph.eliminate_dead_code()
    graph.lint()
    return graph
```

按顺序解释每一段的必要性：

1. **`SEEN` 只用于观察**，把 pass 看到的节点序列记下来，脚本第 1 节会打印。生产 pass 里不该有这个。
2. **`list(graph.nodes)`**：后面会插入并删除节点，直接迭代 `graph.nodes` 是未定义行为。
3. **先匹配 `add.Tensor`**，而不是从 `relu` 出发。因为要改写的是"加法这个位置"，它的两个输入才是一对；从 `relu` 出发会漏掉"两个 relu 有多个使用者"的情况判断。
4. **`a.args[:2]`**：`add.Tensor` 的签名是 `(self, other, alpha=1)`，前两个是张量。`alpha` 这里刻意不处理：只要 `alpha != 1`，匹配就会漏掉，这是保守选择；要覆盖它，得把改写式换成 `x + y * alpha` 并在替换时构造乘法。
5. **`isinstance(a, torch.fx.Node)`**：参数可能是常量（`x + 1` 的 `1`）。不做这一步，`a.op` 直接 `AttributeError`。
6. **两个 relu 都要查 `target is aten.relu.default`**。只查一个，会把 `relu(x) + tanh(x)` 也匹配进来。
7. **`neg.args[0] is not a.args[0]`**：这一行是正确性的核心。`relu(x) + relu(-y)` 不等于 `abs(x)`；只有两个分支读同一个 `x` 时恒等式才成立。用 `is` 而不是 `==`，因为要的是"同一个节点"。
8. **`inserting_before(node)` 里 `call_function`**：新节点插在 `add` 之前，保证它先于使用者出现。
9. **`replace_all_uses_with(new)`**：`add` 的所有下游改用 `abs`。这一步不会删 `add` 本身，只是让它没有使用者。
10. **`eliminate_dead_code()`**：删掉 `add`、两个 `relu`、`neg` 四个孤立节点。不删的话，它们仍会被 `GraphLowering` 解释，虽然不出现在输出里，但会留下无谓的 IR 与依赖。
11. **`lint()`**：检查图的合法性。改写出错时，这一步比让 lowering 崩掉更容易定位。

### 7.4 为什么选这个例子，而不是 `x * 1`

三个理由，缺一不可。

**理由一：它真的在图上。** 实测 lab_b 第 1 节打印的节点序列：

```text
节点: arg0_1 -> aten.relu.default -> aten.neg.default -> aten.relu.default -> aten.add.Tensor -> output
```

五个节点，`add` 的两个输入正是两个 `relu`，第二个 `relu` 的输入是 `neg`。改写要匹配的结构原样存在。`x * 1` 则早在到达 post-grad 之前就被折叠掉了，pass 里看不到，写了也验证不了。

**理由二：等式在浮点上严格成立。** `relu(x) + relu(-x)` 的每一项要么等于 `|x|`，要么等于 `0`：

- `x > 0`：`relu(x) = x`，`relu(-x) = 0`，和为 `x = |x|`；
- `x < 0`：`relu(x) = 0`，`relu(-x) = -x = |x|`；
- `x = 0`：两项都是 0。

整个过程只有"选择"和"加 0"，没有舍入，所以两条路径逐位相等。实测断言用的是 `tol=0.0`，误差 `0.000e+00`。如果换成一个涉及乘除的等价式（比如 `(x+a)+b -> x+(a+b)`），浮点结合律不成立，就得放宽容差，验证会弱一档。

**理由三：收益可以量化成结构差异。** 基线的向量指令：

```text
auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
auto tmp2 = tmp0.neg();
auto tmp3 = at::vec::clamp_min(tmp2, decltype(tmp2)(0));
auto tmp4 = tmp1 + tmp3;
```

改写后：

```text
auto tmp1 = tmp0.abs();
```

同一份 1024×1024 输入、同一个 kernel 数、同一套向量化参数，差别只在循环体。这种"只变结构、不变其他"的对照，才能证明改写本身的价值。

### 7.5 验证：三类断言

lab_b 第 4 节做了四件事，可以当模板：

```python
common.assert_close("基线数值", y_base, ref, tol=0.0)
common.assert_close("改写后数值", y_opt, ref, tol=0.0)
common.check("指令数下降", o["tmp_ops"] < b["tmp_ops"], f"{b['tmp_ops']} -> {o['tmp_ops']}")
common.check("使用 abs", "abs(" in opt_body, str(o["uses_abs"]))
```

对应三类断言：

| 类别 | 断言什么 | 本例取值 |
| --- | --- | --- |
| 数值 | 与 eager 逐位一致 | `max_abs_err=0`，`tol=0` |
| 结构（负向） | 该少的东西少了 | `tmp_ops` 5 → 2 |
| 结构（正向） | 该出现的东西出现了 | 循环体里有 `abs(` |

只有数值断言不够：一个"什么都没改"的 pass 也能过。只有结构断言也不够：删掉一条指令很容易让结果错。两类都要。

**差分测试的写法值得单独说。** 本例的 `ref` 是用 eager 算的 `torch.relu(x) + torch.relu(-x)`，不是"改写后的公式 `abs(x)`"。如果拿 `abs(x)` 当参考，你就在用同一个等式验证自己，等于循环论证。参考实现必须来自**未改写**的原始语义。

### 7.6 缓存：为什么 lab 里要 `fresh_inductor_cache()`

lab_b 的 `compile_variant` 做了三件事：

```python
torch._dynamo.reset()
with fresh_inductor_cache(), cfg.patch(patches):
    fn = torch.compile(make_model(), fullgraph=True)
    fn(x)
```

- `torch._dynamo.reset()`：清掉 Dynamo 的编译缓存，否则第二次 `torch.compile` 直接复用第一次的 FX 图与 guards。
- `fresh_inductor_cache()`：换一个干净的 Inductor 缓存目录，避免"读到的 `output_code.py` 是上一轮的"。
- **预热调用必须在 `cfg.patch` 作用域内**：`torch.compile` 是惰性的，只包住 `torch.compile(...)` 而把 `fn(x)` 放到外面，编译会按默认 config 发生，`post_grad_custom_pre_pass` 根本没生效。

还有一个隐藏行为：自定义 pass 是个普通函数（不是 `CustomGraphPass`）时，`FxGraphCache._check_custom_passes` 会 `bypass("Unsupported post grad custom pass")`。也就是说**这次编译的 FxGraphCache 缓存被整个跳过**，因为它无法把这个函数纳入缓存键。想让自定义 pass 可缓存，就得实现 `CustomGraphPass` 并给一个稳定的 `uuid()`（通常用 `get_hash_for_files((__file__,))`）。

### 7.7 什么时候该改用 pattern

手写遍历适合"一次性、依赖上下文"的改写。出现下面任一信号，就该换第 8 章的 pattern matcher：

- 模式有对称性（`a + b` 与 `b + a` 都要匹配）；
- 需要处理多使用者、`_users` 计数；
- 需要按真实 shape 复检（模式里的形状是 trace 时烧进去的）；
- 同一模式要在多个 pass 档位复用；
- 需要统计命中次数、按名字开关。

pattern matcher 不是"更高级的写法"，它是把"匹配 + 复检 + 替换"这套容易写错的逻辑收进框架。代价是注册时要提供示例输入、调试链路更长（第 8 章会讲匹配失败的六种典型原因）。

## 可跑实验

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py
```

本机实测输出：

```text
========================================================================
1) pass 看到的图
========================================================================
  基线调用次数: 1
  节点: arg0_1 -> aten.relu.default -> aten.neg.default -> aten.relu.default -> aten.add.Tensor -> output

========================================================================
2) 基线 kernel 的向量指令
========================================================================
  auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
  auto tmp1 = at::vec::clamp_min(tmp0, decltype(tmp0)(0));
  auto tmp2 = tmp0.neg();
  auto tmp3 = at::vec::clamp_min(tmp2, decltype(tmp2)(0));
  auto tmp4 = tmp1 + tmp3;

========================================================================
3) 改写后 kernel 的向量指令
========================================================================
  auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
  auto tmp1 = tmp0.abs();

========================================================================
4) 数字与结论
========================================================================
[PASS] 基线数值  (max_abs_err=0.000e+00 tol=0)
[PASS] 改写后数值  (max_abs_err=0.000e+00 tol=0)
[PASS] 指令数下降  (5 -> 2)
[PASS] 使用 abs  (True)

kernel 行数：75 -> 72
```

三处值得停下来看：

1. `基线调用次数: 1`：pass 只被调用一次（推理图）。训练场景下前向/反向各一次，会看到 2。
2. 第 2、3 节的对照：`load` 之后的四条指令变成一条 `abs()`。`neg` 也从"向量指令"里消失了。
3. `kernel 行数：75 -> 72`：只少 3 行，而指令从 5 条降到 2 条。行数包含函数签名、循环骨架与断言，和指令数不是线性关系；判断改写收益优先看 `tmp_ops`，行数只作参考。

想确认改写是唯一变量，可以做一次反向实验：把 `relu_pair_to_abs` 直接 `return graph` 不做事，第 3 节会退回五条指令的形态，第 4 节的"指令数下降"会 FAIL——这条负向验证能证明断言真的在起作用。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `torch/_inductor/custom_graph_pass.py` | `CustomGraphPass`、`CustomGraphPassType`、`get_custom_graph_passes` | 回调签名、列表支持、`uuid()` 的缓存含义 |
| `torch/_inductor/fx_passes/post_grad.py` | custom pre/post 的调用处 | 你的 pass 在管线里的位置（第 6 章表第 4、17 行） |
| `torch/fx/graph.py` | `Graph.inserting_before`、`Graph.call_function`、`Node.replace_all_uses_with`、`Graph.eliminate_dead_code`、`Graph.lint` | 五个 API 的准确语义与副作用 |
| `torch/_inductor/codecache.py` | `FxGraphCache._check_custom_passes` | 普通 callable 为何让缓存 bypass |
| `compilers/inductor_deep_dive/code/labs/lab_b_pass.py` | `relu_pair_to_abs`、`compile_variant` | 本章的可运行样本；注意预热位置与 `fresh_inductor_cache` |
| `compilers/inductor_deep_dive/code/labs/common.py` | `kernel_stats`、`extract_kernel`、`assert_close` | 结构断言与数值断言怎么取数 |

## 常见坑

1. **写 `gm.graph`。** 回调收到的是 `torch.fx.Graph`，`gm` 并不存在。`AttributeError: 'Graph' object has no attribute 'graph'` 是这一章最常见的第一个错误。
2. **改写式子在图上不存在。** `x * 1`、`x + 0` 这类会在到达 post-grad 之前被折叠。先用 `SEEN` 打印实际节点，再决定改写什么。
3. **边遍历边插删。** 直接 `for node in graph.nodes` 并在循环里插入/删除节点，会跳过节点甚至抛 `RuntimeError`。用 `list(graph.nodes)` 快照。
4. **忘了 `eliminate_dead_code()`。** 图能跑、数值也对，但被替换掉的 `relu`/`neg` 还留在图里，降低时白算一遍，还会污染 IR 的依赖关系图。
5. **`target` 比较用了 packet。** 图里是 `aten.relu.default`，不是 `aten.relu`。用 `is aten.relu` 永远不匹配，而 `==` 在某些情况下会走重载比较，行为更难猜。
6. **只验证数值。** 一个空 pass 数值也全对。必须有结构断言，而且要有正反两类（少了什么、多了什么）。
7. **预热在 `cfg.patch` 外面。** `torch.compile` 惰性，编译发生在第一次调用；patch 没包住调用，config 就没生效。
8. **以为改写省了时间就一定是好事。** 本例只改指令数，没有测时间。真实收益要用交错测量 + 子进程隔离（第 21 章），单轮计时在噪声范围（本机 20%–50%）内说明不了问题。

## 自我检查

- [ ] 我能解释为什么 `post_grad_custom_pre_pass` 的参数是 `Graph` 而不是 `GraphModule`。
- [ ] 我能写出 `inserting_before` 的正确用法，并指出漏掉 `with` 会插到哪里。
- [ ] 我能说清 `replace_all_uses_with` 之后为什么还要 `eliminate_dead_code()`。
- [ ] 我能解释 `neg.args[0] is not a.args[0]` 这一行防住了哪个错误改写。
- [ ] 我能说明为什么 `relu(x)+relu(-x) -> abs(x)` 可以用 `tol=0.0`，而 `(x+a)+b` 不行。
- [ ] 我能列出数值、结构负向、结构正向三类断言，并各举一例。
- [ ] 我知道 `fresh_inductor_cache()` 与"预热必须在 patch 内"分别解决什么问题。
- [ ] 我知道普通 callable 的 pass 会让 FxGraphCache bypass，以及怎样让它可缓存。
- [ ] 我能说出至少三个"该改用 pattern matcher"的信号。

## 延伸阅读

- **`torch/fx/graph.py` 的 `Graph`/`Node` API**（<https://pytorch.org/docs/stable/fx.html>）：读 `inserting_before`、`call_function`、`replace_all_uses_with`、`eliminate_dead_code`、`lint` 的文档字符串，理解每个操作的图不变量。
- **`torch/_inductor/custom_graph_pass.py`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/custom_graph_pass.py>）：读 `CustomPassBase.uuid` 的注释，理解"自定义 pass 如何参与缓存键"。
- **`torch/_inductor/codecache.py::FxGraphCache._check_custom_passes`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/codecache.py>）：看清 `bypass("Unsupported post grad custom pass")` 的触发条件。
- **`compilers/inductor_deep_dive/code/labs/lab_b_pass.py`**：把 pass 与 `compile_variant` 一起读，注意观察点（`SEEN`）与验证点（`kernel_stats`）的分工。

---

上一章：[Post-grad Pass 管线](./06-post-grad-pass-pipeline.md) ｜ 下一章：把同样的改写交给 pattern matcher，看它怎么处理对称性与复检。

[上一章：Post-grad Pass 管线](./06-post-grad-pass-pipeline.md) | [下一章：Pattern Matcher 与融合](./08-pattern-matcher-and-fusions.md)