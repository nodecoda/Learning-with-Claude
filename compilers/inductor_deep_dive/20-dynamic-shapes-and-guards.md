# Ch 20 动态 shape 与 guards：一张图，还是一次重编译

> **目标**：理解静态 shape、符号 shape、unbacked 三者的区别；能读懂 `torch._dynamo.explain` 的 `ExplainOutput`；能用 `unique_graphs` 计数器量化「喂 N 个 shape 编译了几张图」；知道 `automatic_dynamic_shapes` 默认行为以及 `dynamic=True/False` 的代价。
> **环境**：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu。命令都在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

编译系统的核心矛盾在这里最集中：**专用代码快，通用代码省编译**。guards 是两者之间的合同。

## 为什么

一个模型上线后，输入 shape 很少永远不变：batch size 随请求波动，序列长度随文本变化，图像尺寸随来源不同。于是编译系统必须回答两个问题：

1. 刚才编译的那张图，现在这个输入还能不能用？
2. 不能用的话，是重新编译一张，还是干脆回退到 eager？

Inductor 的答案写在 guards 里：编译产物附带一组条件表达式，运行时先求值；全部为真就复用，任一为假就重编译。这套机制带来三种成本：

- **编译时间**：每多一张图就多一次 5–15 秒的编译（本机首次编译的量级）。
- **缓存体积**：每张图对应一组 `.py`/`.pyd`/`.debug`。
- **代码质量**：动态 shape 的循环边界是符号，常量折叠、整除判断、向量化尾循环都会受影响。

反过来，全部走动态也有代价：生成代码更保守，固定 shape 下本可以折掉的常量留在了运行时。理解 guards，就是在「编译多少次」和「每张图多快」之间做有依据的取舍。

## 主线

### 1. 三种 shape 世界

Dynamo 看到一个张量时，对它的尺寸有三种处理方式：

| 类型 | 表示 | 进入图的形式 | 典型来源 |
| --- | --- | --- | --- |
| 静态（specialized） | 具体整数 | 常量 `16` | 默认行为；`dynamic=False` |
| 动态（backed symbol） | `SymInt`，如 `s0` | 符号，带取值范围与 hint | `mark_dynamic`、`dynamic=True`、自动转动态 |
| 无后备（unbacked） | `SymInt`，无具体值 | 符号，只有范围 | 由数据决定的输出尺寸，如 `nonzero().shape[0]` |

对应的 guard 形状也不同：

```text
# 静态
L['x'].size()[0] == 16

# 动态（形如）
2 <= L['x'].size()[0] <= 9223372036854775807
```

静态 guard 是等式，符号一变就失败；动态 guard 是区间，只要新尺寸落在区间内就能复用。这也是为什么「第一次 16、第二次 32」通常会触发重编译，而第三次 48 往往能复用第二张图。

控制入口有三个：

```python
cf = torch.compile(f, dynamic=False)   # 永不生成动态 kernel，全部特化
cf = torch.compile(f, dynamic=True)    # 一开始就尽量生成动态 kernel
cf = torch.compile(f)                  # 默认：先静态，发现 dynamism 后转动态

torch._dynamo.mark_dynamic(x, 0)       # 只把第 0 维标成动态
torch._dynamo.mark_static(x, 0)        # 反向操作
```

`torch.compile` 的 docstring 对 `dynamic` 写得很直接：

> When this is True, we will up-front attempt to generate a kernel that is as dynamic as possible to avoid recompilations when sizes change. This may not always work as some operations/optimizations will force specialization; use `TORCH_LOGS=dynamic` to debug overspecialization.

最后半句很重要：即使 `dynamic=True`，某些算子或优化仍会强制特化。不是「开了动态就一定只有一张图」。

### 2. automatic_dynamic_shapes：第二次撞墙才转动态

`torch._dynamo.config.automatic_dynamic_shapes` 默认为 `True`，可用环境变量 `TORCH_DYNAMO_AUTOMATIC_DYNAMIC_SHAPES=0` 关掉。它和 `assume_static_by_default=True` 配合，形成这样的节奏：

1. 第一次调用：按静态 shape 编译，得到一张专用图。
2. 第二次调用、shape 不同：guard 失败，重新编译；这次 Detector 已经看到 dynamism，于是生成动态图。
3. 后续调用：只要落在动态图的 guard 范围内，就复用。

所以喂三个不同 shape，得到的不是三张图，而是两张：

```text
shape 16  -> 静态图 #1
shape 32  -> guard 失败 -> 动态图 #2
shape 48  -> 命中动态图 #2
unique_graphs = 2
```

如果一开始就 `dynamic=True`，四个 shape 共用一张：

```text
shape 16 / 32 / 48 / 64  -> 动态图 #1
unique_graphs = 1
```

这两组数字是本机实测结果，下面的实验会复现它们。

计数器来自 `torch._dynamo.utils.counters["stats"]["unique_graphs"]`，在 `output_graph.py` 里每编译出一张图加一。它统计的是「这个进程里编译出的不同图的数量」，不是「调用次数」。用之前先 `torch._dynamo.reset()` 和 `counters.clear()`，否则会把上一次实验的数字算进来。

还有两个上限要知道：

- `torch._dynamo.config.recompile_limit` 默认 8。同一个 code object 编译到第 8 个版本后，再失败就回退 eager。
- `torch._dynamo.config.accumulated_recompile_limit` 默认 256，是整个进程的累计上限。

`torch.compile` 的 docstring 还提醒：编译缓存是 **按 code object** 存的，不是按帧。同一个函数动态生成多份副本，会共享同一个 code cache。

### 3. guards 怎么看：ExplainOutput

`torch._dynamo.explain(fn)(x)` 返回一个 `ExplainOutput` 对象，字段包括：

| 字段 | 含义 |
| --- | --- |
| `graphs` | 捕获到的 `GraphModule` 列表 |
| `graph_count` | 图的数量 |
| `graph_break_count` | `graph_count - 1` |
| `break_reasons` | 每个 graph break 的原因 |
| `op_count` | 所有图里的算子总数 |
| `ops_per_graph` | 每张图的算子列表 |
| `out_guards` | 本次编译产生的 guard 列表 |
| `compile_times` | 各阶段耗时字符串 |

调用形式是 `explain(fn)(x)`，不是 `explain(fn, x)`；后者已废弃并会发 `FutureWarning`。

`ExplainOutput` **不能用下标访问**。它没有实现 `__getitem__`，写 `out[0]` 会得到 `TypeError`。按字段取：

```python
out = torch._dynamo.explain(f)(torch.randn(16, 16))
print(out.graph_count, out.op_count, len(out.out_guards))
print(str(out).splitlines()[:6])   # 第一行就是 Graph Count: N
```

`str(out)` 会按固定格式打印图数量、break 原因、每图算子、guards。做课程作业或写报告时，直接保存这段文本比手抄字段可靠。

### 4. 符号进入 Inductor 之后

Dynamo 层的符号是 `SymInt`；进入 Inductor 后由 `SizeVarAllocator`（`torch/_inductor/sizevars.py`）管理。它提供的能力大致分三类：

- **化简**：`simplify`、`_simplify_with_ranges`，把 `s0 * 1`、`s0 + 0` 这类表达式收掉。
- **判定**：`statically_known_true`、`statically_known_leq`、`statically_known_multiple_of`，在不知道具体值的前提下回答「是否一定成立」。
- **取值**：`guard_int`、`evaluate_min`、`evaluate_max`。`guard_int` 需要具体数值，一旦调用就意味着增加一条 guard。

这些能力直接决定生成代码的形态：

- 循环边界。静态图里是 `x0 < 64`；动态图里可能是 `x0 < s0`。
- 索引表达式。广播、步长、偏移都可能带符号项，IR 里看到 `s0`、`s1` 属于正常。
- 尾循环与边界判断。能否证明长度是向量宽度的整数倍，会影响循环怎么切。
- 原地与对齐判断。依赖具体字节数的优化在动态 shape 下更难成立。

`ir_pre_fusion.txt` / `ir_post_fusion.txt` 里的 `var_ranges` 是观察点：静态时循环变量对应常量范围，动态时会看到符号表达式。把同一个函数在 `dynamic=False` 与 `dynamic=True` 下各生成一份产物，对照 `var_ranges` 和 kernel 里的循环边界，差异会直接摆在眼前。

FxGraphCache 也参与这套机制。每个缓存条目都带一个 `guards_expr`，lookup 时由 `shape_env.evaluate_guards_expression(code, args)` 在当前输入上求值：

```python
# codecache.py，保存条目时
guards = shape_env.get_pruned_guards(symints)
compiled_graph.guards_expr = shape_env.produce_guards_expression(
    placeholders=symints, guards=guards
)

# 查找条目时
evaluate_guards = shape_env.evaluate_guards_expression
```

所以「同一张图、不同 shape」在缓存里可能对应同一个 key 下的多个条目，也可能对应不同 key；能不能复用，最终由 guards 决定。

### 5. 工程取舍：什么时候该静态，什么时候该动态

| 场景 | 建议 | 理由 |
| --- | --- | --- |
| 固定 batch 的离线推理 | `dynamic=False` | 常量全折，循环边界固定，代码最短 |
| batch 在 1–64 之间波动 | 默认配置或 `mark_dynamic(x, 0)` | 让动态只发生在真正会变的维度 |
| 序列长度变化剧烈 | `dynamic=True` 或对长度维 `mark_dynamic` | 避免每个长度一张图 |
| 只变一维、其余固定 | `mark_dynamic` 指定维度 | 保住其余维度的静态优化 |
| 编译时间敏感（冷启动） | 预热 + 固定 shape | 把编译放在服务启动阶段 |
| 编译时间不敏感、QPS 高 | 动态图 + 监控 `unique_graphs` | 用少量图覆盖大量 shape |

监控指标建议至少两项：**unique_graphs**（编译出多少张图）和 **单次调用耗时**。只看其中一个会得出片面结论：图少了但每张更慢，或者图快了但每次请求都在编译。

`TORCH_LOGS=dynamic` 可以观察特化决策（`+dynamic` 为 DEBUG，信息更细）：

```powershell
$env:TORCH_LOGS = "dynamic"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
Remove-Item Env:\TORCH_LOGS
```

日志会显示哪些维度被特化、哪些被符号化。把它和 `out_guards` 对照，能解释「为什么我以为开了动态，还是重编译了」。

## 可跑实验

下面的探针脚本写到系统临时目录，不污染课程目录；运行仍然走统一入口 `run.ps1`。

```powershell
$probe = Join-Path $env:TEMP "inductor_shapes_probe.py"
$lines = @(
    "import torch"
    "import torch._dynamo as dynamo"
    "from torch._dynamo.utils import counters"
    ""
    "def f(x):"
    "    return torch.relu(x * 2.0 + 1.0)"
    ""
    "def unique(shapes, dynamic=None):"
    "    dynamo.reset()"
    "    counters.clear()"
    "    cf = torch.compile(f, dynamic=dynamic)"
    "    for n in shapes:"
    "        cf(torch.randn(n))"
    "    return counters['stats']['unique_graphs']"
    ""
    "print('default, 3 shapes :', unique([16, 32, 48]))"
    "print('dynamic, 4 shapes :', unique([16, 32, 48, 64], dynamic=True))"
    ""
    "dynamo.reset()"
    "out = dynamo.explain(f)(torch.randn(16))"
    "print('graph_count :', out.graph_count)"
    "print('op_count    :', out.op_count)"
    "print('guards      :', len(out.out_guards))"
)
[IO.File]::WriteAllLines($probe, $lines, (New-Object System.Text.UTF8Encoding($false)))
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 $probe
Remove-Item -LiteralPath $probe
```

本机实测输出（数值稳定，op 数会随版本略有变化）：

```text
default, 3 shapes : 2
dynamic, 4 shapes : 1
graph_count : 1
op_count    : 3
guards      : 10
```

三个结论：

1. 默认配置下 3 个 shape 编译了 **2** 张图。第一次静态，第二次转动态，第三次复用。
2. `dynamic=True` 下 4 个 shape 只编译 **1** 张图。
3. 一个看起来很小的函数，`out_guards` 已经有若干条（本机是 10 条），包含 dtype、device、shape、stride 等。guards 的数量不等于图的数量，别把两者混为一谈。

这里有一个实测出来的细节：如果输入是 `(n, 16)` 这种二维张量，`dynamic=True` 可能多出一张图。原因是 duck sizing——第一个 shape `(16, 16)` 让 Dynamo 认为两个维度相等，于是加了 `L['x'].size()[1] == L['x'].size()[0]` 这条 guard；换成 `(32, 16)` 时这条 guard 失败，重新编译。用 1D 输入（上面的写法）能避开这个干扰。guard 原文来自 `TORCH_LOGS=guards`，消息里还给了逃生口：`torch.fx.experimental._config.use_duck_shape = False`。

这条现象说明一件事：**「喂了几个 shape」不足以描述实验**。张量的维数、维度之间是否相等、stride 是否连续，都会进入 guards。写报告时把输入形状和 dtype 一并写清楚。

想看生成代码的差异，把 `dynamic` 两组的产物分别留下来对照：

```powershell
$cache = "compilers\inductor_deep_dive\code\.cache"
Get-ChildItem -LiteralPath $cache -Recurse -Directory -Filter *.debug | Sort-Object LastWriteTime | Select-Object -Last 3 FullName
```

打开最新一份的 `ir_post_fusion.txt`，找 `var_ranges`；再打开 `output_code.py`，看循环边界是常量还是符号。这就是「动态 shape 落到代码上」的具体样子。

## 源码落点

| 位置 | 读什么 | 为什么读 |
| --- | --- | --- |
| `torch/_dynamo/config.py` | `automatic_dynamic_shapes`、`assume_static_by_default`、`recompile_limit`、`accumulated_recompile_limit` | 行为的开关与上限 |
| `torch/_dynamo/eval_frame.py` 的 `explain` | 内部如何累计 `graphs`、`out_guards` | 解释 `ExplainOutput` 的字段来源 |
| `torch/_dynamo/backends/debugging.py` 的 `ExplainOutput` | 字段定义与 `__str__` | 为什么不能下标 |
| `torch/_dynamo/output_graph.py` | `counters["stats"]["unique_graphs"] += 1` | 计数器的语义 |
| `torch/fx/experimental/symbolic_shapes.py` 的 `ShapeEnv` | `produce_guards_expression`、`evaluate_guards_expression`、`get_pruned_guards` | guards 的生成与求值 |
| `torch/_inductor/sizevars.py` 的 `SizeVarAllocator` | `simplify`、`statically_known_*`、`guard_int` | 符号在 IR 里的化简与判定 |
| `torch/_inductor/codecache.py` 的 `FxGraphCache` | `guards_expr` 的保存与 `evaluate_guards` | 缓存条目怎么复用 |

读 `symbolic_shapes.py` 时，先看 `produce_guards_expression` 与 `evaluate_guards_expression` 这一对函数，它们构成「生成合同」与「验合同」的两端；中间的 `ShapeGuard` 结构决定合同里有哪些条款。

## 常见坑

1. **把 `ExplainOutput` 当 tuple 用。** `out[0]` 会报 `TypeError: 'ExplainOutput' object is not subscriptable`。用 `out.graphs[0]`、`out.graph_count` 这样的字段访问。

2. **只看 `graph_count` 不看 `out_guards`。** 图的数量少不代表 guards 宽松；一张动态图也可能有十几条 guard。判断「能不能覆盖新 shape」，要看 guards 内容或直接跑一次看是否重编译。

3. **计数器不清零。** `counters` 是进程级累计值。同一个进程里做两组实验不 `clear()`，第二组会继承第一组的数字，结论直接错。

4. **以为 `dynamic=True` 一定更快。** 它减少重编译，但生成的是更通用的代码：循环边界是符号、常量折叠受限。固定 shape 的高 QPS 场景，静态图往往更快。要用同一套隔离 benchmark 验证。

5. **在模型里写依赖具体尺寸的 Python 分支。** 例如 `if x.shape[0] > 1:` 会在 Dynamo 层产生 guard 或 graph break；前者导致重编译，后者把图切断。能改写成张量运算就改写。

6. **日志变量带尾随空格。** `+dynamic` 本身有效（DEBUG）；在 cmd 里 `set TORCH_LOGS=dynamic && ...` 会带进空格并报 `Invalid log settings`。

7. **忽略 `recompile_limit`。** 默认 8 个版本后回退 eager。线上如果看到「某段代码突然变慢」，先确认是不是撞了上限，而不是先怀疑 kernel 生成质量。

8. **以为动态 shape 下缓存一定命中。** FxGraphCache 的条目要过 guards 求值；新 shape 超出区间就会重新编译并新增条目。cache 目录变大是这类场景的常见现象。

9. **忽略 duck sizing。** 第一个输入如果恰好让两个维度相等，Dynamo 会加一条「这两个维度相等」的 guard。后面换一个不相等的 shape，即使开了 `dynamic=True` 也会重编译。做 shape 实验时把输入维度和取值写全，别只写 batch size。

## 自我检查

- [ ] 我能区分静态、动态、unbacked 三种 shape，并各举一个来源。
- [ ] 我能写出静态 guard 与动态 guard 的形状差异。
- [ ] 我能解释为什么 3 个 shape 默认得到 2 张图，而 `dynamic=True` 下 4 个 shape 只要 1 张。
- [ ] 我能说出 `automatic_dynamic_shapes` 的默认值，以及关掉它的环境变量。
- [ ] 我能列出 `ExplainOutput` 的至少五个字段，并知道它不能下标。
- [ ] 我能用 `unique_graphs` 计数器设计一个「喂 N 个 shape 看编译几张图」的实验。
- [ ] 我能解释 `SizeVarAllocator.guard_int` 为什么会降低通用性。
- [ ] 我知道 `recompile_limit` 与 `accumulated_recompile_limit` 的默认值和后果。

## 延伸阅读

- **`torch.compile` 的 docstring（`dynamic` 参数）**：读哪里——`dynamic=True/False/None` 三段；学什么——官方对三种策略的准确表述；关联——本章第 1 节。
- **`torch._dynamo.explain` 与 `ExplainOutput`**：读哪里——`eval_frame.py` 的累计逻辑、`debugging.py` 的字段；学什么——如何读编译决策；关联——本章第 3 节。
- **`torch/fx/experimental/symbolic_shapes.py` 的 `ShapeEnv`**：读哪里——`produce_guards_expression`、`evaluate_guards_expression`；学什么——guards 的生成与验证；关联——缓存复用。
- **`torch/_inductor/sizevars.py`**：读哪里——`statically_known_*` 系列与 `guard_int`；学什么——符号在 codegen 里的化简边界；关联——动态图的代码质量。
- **`torch/_dynamo/config.py`**：读哪里——`recompile_limit`、`accumulated_recompile_limit`、`automatic_dynamic_shapes`；学什么——默认值与调优入口；关联——线上重编译排查。

[上一章：Ch 19 缓存体系](./19-cache-system.md) | [下一章：Ch 21 性能工程](./21-performance-engineering.md)
