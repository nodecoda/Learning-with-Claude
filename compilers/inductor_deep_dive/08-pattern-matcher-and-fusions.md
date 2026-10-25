# Ch 8 Pattern Matcher 与融合：让改写可检索、可复检、可组合

> **目标**：读完本章，你能说出 `pass_patterns` 为什么是三档、按什么顺序执行；能用 `register_replacement` 注册一个最小改写并解释它的五个参数；能读懂 pattern 的字符串表示；能列出匹配失败的六类原因并逐条排查；能判断一个改写该用手写 pass、`register_replacement` 还是 `register_lowering_pattern`。

> **环境**：Windows 10 x86_64；Python 3.11.12（`.scratch/aic-env`）；`torch 2.14.1+cpu`；MSVC 14.43（`cl.exe`）。本章命令在仓库根目录执行：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 .scratch\pattern_demo.py
> ```

## 为什么

第 7 章的手写 pass 能工作，但它把三件容易出错的事混在一个循环里：**找到结构、确认结构合法、替换结构**。

一旦模式变复杂，这三件事互相纠缠：

- `a + b` 和 `b + a` 是同一个数学，但遍历代码要写两遍；
- 中间节点如果有第二个使用者，能不能改写？手写代码通常忘了查 `node.users`；
- 模式里有常量 `1.0`，但 trace 时烧进去的是具体张量形状，形状换了还匹配吗？手写代码根本不考虑这层；
- 同一模式在多个地方复用，复制粘贴三份，改一处漏两处。

pattern matcher 的思路是把"模式"变成**数据**：你给两个函数（搜索式、替换式）和一组示例输入，框架 trace 出模式 AST，再拿 AST 去图上匹配。匹配成功后还要过一遍 `extra_check` 和形状复检，最后才替换。

这不是"更高级"，而是把容易漏的检查制度化。代价也要认：注册需要示例输入，匹配失败时那句 `FailedMatch` 可能出现在很深的地方，调试链路比手写长。

## 主线

### 8.1 三档 `pass_patterns`

`post_grad.py` 顶部有一句注释和三个容器：

```python
# First pass_patterns[0] are applied, then [1], then [2]
pass_patterns = [
    PatternMatcherPass(),
    PatternMatcherPass(),
    PatternMatcherPass(),
]
```

第 6 章的管线里，第 11 步就是按 `0 → 1 → 2` 依次 `apply`。为什么分三档而不是一个大容器？

- **档位表达优先级**。`register_lowering_pattern` 的签名里 `pass_number=1` 是默认值；框架自己的重量级模式会显式选档。`misc_patterns.py` 里有一行很直白：`post_grad_patterns = post_grad_patterns_all[1]  # medium priority`。
- **一档内可以反复匹配到不动点**。`PatternMatcherPass.apply` 内部会对每个节点尝试该 target 下的所有 entry，命中后继续看同一节点的其他 entry（若节点已被删则 `break`）。
- **顺序就是优先级**。源码只保证 `0 → 1 → 2`；框架内部按"低风险/低开销在前、需要更多上下文或代价更高的在后"来分配档位（`register_lowering_pattern` 默认第 1 档，`misc_patterns` 把第 1 档注释为 medium priority）。把昂贵模式放前面，会让每个图都先付一遍它的匹配开销。

注册新 pattern 时选哪一档，取决于它有多"通用"和"贵"。教材示例放第 0 档，是因为它只匹配两个相邻的 `neg`，代价极低。

### 8.2 `register_replacement` 的五个关键参数

签名（节选）：

```python
def register_replacement(
    search_fn, replace_fn, example_inputs, trace_fn, pass_dicts,
    extra_check=_return_true, scalar_workaround=None,
    exclusive_arg_names=(), search_fn_pattern=None,
    skip_duplicates=False, pattern_name=None, get_decomp_fn=select_decomp_table,
) -> bool:
```

| 参数 | 作用 | 常见错误 |
| --- | --- | --- |
| `search_fn` | 要被匹配的表达式，用普通 torch 代码写 | 写成 `aten.neg` 直调，trace 出来的图与目标不一致 |
| `replace_fn` | 等价替换式；参数名必须与 `search_fn` 一致 | 参数顺序不同，替换后语义错位 |
| `example_inputs` | 一组真实张量，用于 trace 与形状复检 | 用 `torch.empty(0)` 或 meta 张量，trace 阶段就失败 |
| `trace_fn` | `fwd_only`（前向）或 `joint_fwd_bwd`（前向+反向） | 推理图用 `joint_fwd_bwd`，模式里混进反向节点，永远不匹配 |
| `pass_dicts` | 目标 `PatternMatcherPass`，可传列表 | 传成 `pass_patterns`（列表本身）而不是 `pass_patterns[0]` |

内部流程可以概括成四步：

1. **trace 搜索式**：在 fake mode 下把 `example_inputs` 喂给 `search_fn`，得到一张小 FX 图，再把它转成 `PatternExpr`。叶子节点变成 `KeywordArg('x')` 这类占位符，中间节点变成 `CallFunction(op, ...)`。
2. **trace 替换式**：同样 trace `replace_fn`，得到"命中后要构建的子图"。
3. **组装 `check_fn`**：匹配时先按模式做一次粗匹配（这一步会忽略部分类型差异），再用**真实命中的 shape** 重新跑一遍参数，确认结果合法。注释的原话是"Often shapes get burned into the pattern, so our initial match ran with `ignore_types=(int, ...)`"。
4. **注册 entry**：把 pattern、replacement、`extra_check` 组合成 `ReplacementPatternEntry`，塞进 `pass_dicts`。

`skip_duplicates=True` 是防御性参数：同一个模式重复注册时直接跳过，返回 `False`。开发 pass 时反复 import 同一个模块，很容易重复注册，加上它省事。

### 8.3 最小可跑示例：`neg(neg(x)) -> x`

先跑一次**没有注册 pattern** 的基线，再注册、再跑，才能把差异归因给 pattern 本身。脚本结构如下（保存为 `.scratch\pattern_demo.py`）：

```python
import torch
from torch._inductor.utils import fresh_inductor_cache
from torch._inductor.pattern_matcher import register_replacement, fwd_only
from torch._inductor.fx_passes.post_grad import pass_patterns
from torch._inductor import metrics
from labs import common


def model(x):
    return torch.neg(torch.neg(torch.sin(x)))


def compile_once(tag: str):
    torch._dynamo.reset()
    metrics.reset()
    with fresh_inductor_cache():
        before = common.modules_snapshot()
        fn = torch.compile(model, fullgraph=True)
        out = fn(x)
        mods = common.modules_since(before)
        src = common.read_text_any(mods[-1].__file__)
    body = common.extract_kernel(src)
    stats = common.kernel_stats(src)
    print(f"[{tag}] kernels={metrics.generated_kernel_count} tmp_ops={stats['tmp_ops']}")
    for line in body.splitlines():
        if "auto tmp" in line:
            print("   ", line.strip())
    return out


x = torch.randn(1024)
ref = model(x)
base_out = compile_once("baseline")
print(f"max_abs_err baseline={(base_out - ref).abs().max().item():.3e}")


def search(x):
    return torch.neg(torch.neg(x))


def replace(x):
    return x


ok = register_replacement(
    search, replace, [torch.empty(4, 8)], fwd_only, pass_patterns[0],
    skip_duplicates=True,
)
print("registered:", ok, "| pass_patterns[0] buckets:", len(pass_patterns[0].patterns))

opt_out = compile_once("pattern")
print(f"max_abs_err pattern={(opt_out - ref).abs().max().item():.3e}")
print("bitwise equal baseline vs pattern:", torch.equal(base_out, opt_out))
```

几个设计点：

- `example_inputs` 用 `torch.empty(4, 8)`，与真正编译的 1024 长度不同。这正好验证了"形状不会被硬编码进模式"——替换在 1024 上照样生效。
- `replace(x) = x` 返回的就是输入节点本身，替换图退化成一条边。pattern matcher 支持这种退化替换。
- 注册发生在两次编译之间，第一次编译不受影响；这依赖 `fresh_inductor_cache()` 与 `torch._dynamo.reset()`（第 7 章 7.6）。

实测输出：

```text
[baseline] kernels=1 tmp_ops=4
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
    auto tmp1 = tmp0.sin();
    auto tmp2 = tmp1.neg();
    auto tmp3 = tmp2.neg();
max_abs_err baseline=5.960e-08
registered: CallFunction(aten.neg.default, CallFunction(aten.neg.default, KeywordArg('x')), _users=0) | pass_patterns[0] buckets: 1
[pattern] kernels=1 tmp_ops=2
    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
    auto tmp1 = tmp0.sin();
max_abs_err pattern=5.960e-08
bitwise equal baseline vs pattern: True
```

逐行读：

- `registered:` 后面那串就是 trace 出来的模式。外层 `CallFunction(aten.neg.default, ...)` 是第二个 `neg`，内层 `CallFunction(aten.neg.default, KeywordArg('x'))` 是第一个，`KeywordArg('x')` 是参数占位符。模式里没有形状、没有 dtype——它们是匹配时从图上读的。
- 两次的 `kernels=1`：`neg` 本来就是逐元素操作，删掉它不会改变 kernel 数，只改循环体。
- `tmp_ops 4 → 2`：`load`、`sin`、`neg`、`neg` 变成 `load`、`sin`。
- 误差两行相同（`5.960e-08`）：这不是 pattern 引入的，而是**向量化 `sin` 与 eager 标量 `sin` 的实现差异**。证据是基线与 pattern 的误差一模一样，且 `bitwise equal ... True`——两次编译的输出逐位相同。

这一段值得记住：**"与 eager 有误差"不等于"改写错了"**。要做差分测试，正确参照是"未改写的编译产物 vs 改写后的编译产物"，再加一条"与 eager 在容差内"。

### 8.4 匹配失败的六类原因

`PatternMatcherPass.apply` 在尝试每个 entry 前后有一串检查，任何一条不满足都会 `continue`。按排查顺序：

| # | 原因 | 源码线索 | 症状 / 排查 |
| --- | --- | --- | --- |
| 1 | 目标算子不对 | entry 按 `(node.op, target)` 分桶 | 断点打在 `search_fn` 没用；先打印图里的 `target`，确认是 `aten.neg.default` 而不是 `aten.neg` |
| 2 | 多使用者 | `_match_users`：中间节点的 `users` 必须等于模式记录的数（默认 1） | 中间结果被两处消费时不匹配；需要显式放宽为 `MULTIPLE` |
| 3 | 形状/类型复检失败 | `check_fn` 用真实 shape 重跑参数 | 粗匹配通过、细匹配 `FailedMatch`；常见于把常量形状烧进模式 |
| 4 | 跨 mutation region | `get_mutation_region_id` 不唯一则丢弃 | 图里有原地写时，跨区的模式被保守跳过 |
| 5 | 跨 stream / mempool | `meta["custom"]["stream"]`、`mempool` 不一致则丢弃 | CUDA 多流场景常见；CPU 上通常不触发 |
| 6 | 输入类型不受支持 | `fallback_node_due_to_unsupported_type(node, allow_cpu_inputs=False)` | 某些类型（如 complex）上的节点被直接跳过 |

还有两个"不在匹配器里、但常被误判为匹配失败"的原因：

- **pass 顺序**。pattern 在 `post_grad_custom_pre_pass` **之后**执行（第 6 章），如果前一个自定义 pass 已经把结构改掉，这里自然匹配不到。
- **`config.pattern_matcher=False`**。整块跳过，注册了也不会跑。

### 8.5 三种改写方式的选择

| 维度 | 手写 pass（第 7 章） | `register_replacement` | `register_lowering_pattern` |
| --- | --- | --- | --- |
| 输入 | `Graph` | `search_fn` / `replace_fn` + 示例输入 | `PatternExpr` + 处理函数 |
| 输出 | 改后的 `Graph` | 改后的 `Graph`（aten 层） | 直接产出 Inductor IR |
| 对称性 | 自己写两遍 | 模式里用 `KeywordArg` 统一 | 同左 |
| 形状复检 | 自己做 | 框架 `check_fn` 自动做 | 自己用 `extra_check` 做 |
| 适用场景 | 依赖复杂上下文的一次性改写 | 通用 aten 等价式 | 某段模式该整体映射成一个 IR 构建函数 |
| 调试难度 | 低（自己控制） | 中（`FailedMatch` 层次深） | 高（跨 aten→IR 边界） |

`register_lowering_pattern` 的关键在于它给被装饰函数打上 `_inductor_lowering_function = True`。第 4 章的 `GraphLowering.call_function` 看到这个标记会**直接调用函数**，跳过 `lowerings` 查表。于是你可以让某个模式在 lowering 阶段直接构建 IR，而不必先替换成已有 aten 算子再走常规 lowering。

这条路径适合"整体比拆开更优"的场景，比如需要一次生成特定布局或特殊 kernel 的模式融合。代价是它绕过了 lowering 层的通用逻辑（类型提升、广播、fallback 判定），正确性责任回到你身上。

### 8.6 调试 pattern 的四个抓手

1. **计数**。匹配成功会累加 `counters["inductor"]["pattern_matcher_count"]` 与 `pattern_matcher_nodes`。命中数为 0 时，先确认"有没有跑"，再查"为什么没匹配"。
2. **按名字调试**。`TORCHINDUCTOR_PATTERN_MATCH_DEBUG=1` 打开后，`apply` 会按 `pattern_name`（未设置则用类名 + target）记录每个模式的命中情况。
3. **看图**。`TORCH_LOGS=post_grad_graphs` 打印每次关键改写后的图；`fx_graph_transformed.py` 是管线结束时的快照。两者对照能看出 pattern 有没有生效。
4. **二分**。`TORCHINDUCTOR_DISABLED_PASSES` 按 pass 名关闭；`config.pattern_matcher=False` 关闭整块。前者用于定位单个 pass，后者用于确认"问题是否来自 pattern 段"。

## 可跑实验

把 8.3 的脚本保存到仓库根的 `.scratch\pattern_demo.py`（目录已存在），然后：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 .scratch\pattern_demo.py
```

本机实测输出见 8.3 的代码块。三次观察点：

1. **注册前后各编译一次**，且各自 `fresh_inductor_cache()`。同一进程里复用缓存会读到上一轮的 `output_code.py`，`tmp_ops` 不变，看起来像"pattern 没生效"。
2. **`pass_patterns[0] buckets: 1`**：只注册了一个模式，落在一个 `(op, target)` 桶里。`patterns` 是 `defaultdict`，打印长度是桶数而不是 entry 数；向同一 target 再注册第二个模式时，这个数字不会变。
3. **`bitwise equal ... True`**：这是最强的正确性证据。两次编译的输出逐位相同，说明改写没有引入任何数值变化；与 eager 的 `5.960e-08` 差异在两次里都存在，来源是 `sin` 的向量化实现。

想进一步验证"匹配失败"长什么样，可以把 `search` 改成 `torch.neg(torch.sin(x))`（少一层 `neg`），重新跑：`registered` 仍为真（模式合法），但第二次编译的 `tmp_ops` 仍是 4，说明匹配没命中。这是一个安全的反向实验，不会改仓库里的任何文件。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `torch/_inductor/fx_passes/post_grad.py` | `pass_patterns` 定义与三档 `apply` | 三档顺序、`pattern_matcher` 总开关 |
| `torch/_inductor/fx_passes/post_grad.py` | `register_lowering_pattern` | `pass_number=1` 默认值、`_inductor_lowering_function` 标记 |
| `torch/_inductor/pattern_matcher.py` | `register_replacement` | 五个关键参数与 `check_fn` 的组装 |
| `torch/_inductor/pattern_matcher.py` | `gen_pattern` / `gen_pattern_and_search_gm` | trace 搜索式、`ignore_types`、`scalar_workaround` |
| `torch/_inductor/pattern_matcher.py` | `PatternMatcherPass.apply` | 分桶、反向遍历、mutation region、stream、`extra_check` |
| `torch/_inductor/pattern_matcher.py` | `CallFunction`、`KeywordArg`、`_match_users` | 模式 AST 与"多使用者"约束 |
| `torch/_inductor/fx_passes/misc_patterns.py` | `_misc_patterns_init` | 真实 pattern 的注册样本；`pass_patterns[1]  # medium priority` |
| `torch/_inductor/graph.py` | `call_function` 的 `_inductor_lowering_function` 分支 | pattern 直接产出 IR 时的分派路径 |

## 常见坑

1. **把 `pass_patterns` 整个列表传给 `pass_dicts`。** 参数是"某一个 `PatternMatcherPass`"，可以是列表但元素得是 pass；传外层列表会在注册时炸类型。
2. **`replace_fn` 的参数名与 `search_fn` 不一致。** trace 靠参数名绑定占位符，名字不同会得到 `keyword argument` 相关错误，或者更糟：静默匹配错节点。
3. **示例输入用了编译期之外的 dtype。** 用 `torch.empty(4, 8)`（fp32）注册模式，却想匹配 fp16 图，复检阶段会被类型挡住。要么用对应 dtype 的示例，要么确认模式对 dtype 不敏感。
4. **以为注册了就一定匹配。** 六类失败原因里，"多使用者"和"形状复检"最常见。先打印命中计数，再逐个排查。
5. **在 CPU 图上期待 pattern 生效于 fallback 节点。** `apply` 里对 `fallback_node_due_to_unsupported_type(..., allow_cpu_inputs=False)` 的节点直接跳过；自定义 fallback 算子附近的结构不会被匹配。
6. **用 `pattern_matcher=False` 做性能对比。** 关掉之后图结构完全不同，测出来的时间不能归因到单个 pattern。要比就比"注册前 vs 注册后"。
7. **忘记 `skip_duplicates=True`。** 反复 import 或重复注册会让同一个模式进多个桶，命中计数翻倍，调试时容易误判。

## 自我检查

- [ ] 我能解释 `pass_patterns` 为什么是三档，以及"`pass_number=1` 是默认值"意味着什么。
- [ ] 我能列出 `register_replacement` 的五个关键参数，并说出每个的常见错误。
- [ ] 我能读懂 `CallFunction(aten.neg.default, CallFunction(aten.neg.default, KeywordArg('x')), ...)` 这串表示。
- [ ] 我能解释 `check_fn` 为什么存在：模式里的形状是 trace 时烧进去的。
- [ ] 我能说出至少四类匹配失败原因，并给出对应的排查手段。
- [ ] 我能解释 `tmp_ops 4 → 2` 与 `kernels=1` 为什么可以同时成立。
- [ ] 我知道 `5.960e-08` 的误差来自向量化 `sin`，并能用 `bitwise equal` 证明改写本身无误差。
- [ ] 我能说清 `register_lowering_pattern` 与 `register_replacement` 的输出层次差异。
- [ ] 我知道 `pattern_matcher=False` 与 `disabled_passes` 在调试上的不同用途。

## 延伸阅读

- **`torch/_inductor/pattern_matcher.py`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/pattern_matcher.py>）：先读 `register_replacement` 与 `PatternMatcherPass.apply`，再回头读 `CallFunction`/`KeywordArg` 的匹配实现。
- **`torch/_inductor/fx_passes/misc_patterns.py`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/fx_passes/misc_patterns.py>）：一个真实模块里如何组织多个 pattern、如何用 `extra_check` 限定设备与 dtype。
- **`torch/_inductor/fx_passes/post_grad.py` 的 `register_lowering_pattern`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/fx_passes/post_grad.py>）：读它如何把 `pattern_matcher` 的注册与 `pass_patterns[pass_number]` 绑起来。
- **`torch/_inductor/graph.py` 的 `_inductor_lowering_function` 分支**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/graph.py>）：理解"pattern 直接产 IR"为什么能跳过 lowering 查表。

---

上一章：[写一个自定义 Pass](./07-write-a-custom-pass.md) ｜ 下一章：把本章的改写做成一份可提交的练习。

[上一章：写一个自定义 Pass](./07-write-a-custom-pass.md) | [下一章：Practice B 自定义 Pass](./practice-b-custom-pass.md)