# Final Project：给一个真实小模型做可验证的编译期改造

> **目标**：独立完成一次端到端的 Inductor 改造：读懂参考实现 `sum(relu(z)+relu(-z)) -> sum(abs(z))`，复现它的正确性、结构与时间证据；然后换一个**数学等价**的改写，自己写 post-grad pass、跑差分测试和隔离 benchmark，交一份能让别人复现的报告。
>
> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43，CPU 仅 AVX2。命令在仓库根目录 `D:\Learning-with-Claude` 执行，统一入口为 `powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本>`。参考实现为 `compilers\inductor_deep_dive\code\labs\final_project.py`，报告写入 `compilers\inductor_deep_dive\code\.cache\final_report.md`。参考轮次的实测结论是：生成 kernel 数 1，向量 tmp 指令 5→2，最大绝对误差 1.95e-3，耗时约为基线的 0.70x；这些数字都带单机单轮条件，不能当成普适加速承诺。

## 为什么

前面每一章都在拆一个局部：post-grad pass 怎么写、lowering 怎么注册、调度器怎么融合、kernel 怎么向量化、benchmark 怎么隔离。Final Project 要把它们串成一次完整的工程闭环：

```text
读懂模型
  -> 找到可证明等价的改写
  -> 在正确的图阶段实现
  -> 用差分测试证明数值
  -> 用生成代码证明结构
  -> 用隔离测量证明时间
  -> 写报告说明边界
```

参考改造选择 `relu(z) + relu(-z) -> abs(z)`，原因不是它“看起来简单”，而是它同时满足三个条件：

1. **数学上可证明**：对任意实数 `z`，`relu(z) + relu(-z) = |z|`；在 float32 上逐元素也相等（`lab_b` 实测误差 0）。
2. **结构上可见**：基线需要 load、clamp_min、neg、clamp_min、add 五条向量指令；改写后只剩 load、abs 两条。`tmp_ops` 从 5 降到 2，是生成代码层面的硬证据。
3. **端到端可验证**：模型里还有 `x @ w + b`，它走 `extern_kernels.addmm`，不会被这次改写影响；这让你能区分“pointwise 改写”和“matmul 外部调用”，不会把两者混在一起。

但端到端也带来一个新问题：`sum(dim=1)` 的归约顺序变了，最终最大绝对误差是 **1.95e-3**。这不是改写错了，而是 float32 求和的结合顺序不同。Final Project 的核心训练，就是**同时证明“结构变好了”和“数值仍然对”**，而不是只拿一个更小的毫秒数。

你还要换一个改写。参考实现只是一个模板；真正的能力是：面对一个新的等价关系，判断它是否能在 post-grad 图里被匹配、是否会被更早的 pass 消掉、是否会改变浮点语义、是否值得测。允许得到“没有加速”甚至“更慢”的结论，只要证据完整、解释成立。

## 主线

### 1. 参考实现拆解：pass 在什么阶段看到了什么

模型只有两行：

```python
def model(x, w, b):
    z = x @ w + b
    return (torch.relu(z) + torch.relu(-z)).sum(dim=1)
```

在 post-grad 图里，它大致对应：

```text
z = addmm(x, w, b)          # extern kernel
r1 = relu(z)
n  = neg(z)
r2 = relu(n)
s  = add(r1, r2)
out = sum(s, dim=1)
```

参考 pass 只匹配 `add.Tensor`，要求两个输入都是 `relu.default`，其中第二个 `relu` 的输入是 `neg.default`，并且 `neg` 的输入与第一个 `relu` 的输入是**同一个 FX 节点**。四个条件缺一不可：

```python
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
```

几个工程细节：

- **`is not a.args[0]` 是对象身份比较**，不是 `==`。FX 节点是对象，`==` 可能被重载；这里要确认“同一个生产者节点”。
- **`graph.inserting_before(node)`** 保证新节点插在 `add` 之前，拓扑顺序合法。
- **`node.replace_all_uses_with(new)`** 把 `sum` 的输入从 `add` 换成 `abs`；原来的 `relu/neg/relu/add` 变成死代码。
- **`eliminate_dead_code()`** 真正删除死节点；只替换不删除，图里仍留着它们。
- **`graph.lint()`** 检查图结构；在自定义 pass 里养成习惯，比等编译报错更早发现问题。
- **回调收到的是 `torch.fx.Graph`，不是 `GraphModule`**。写 `gm.graph` 会直接报错。

为什么放在 `post_grad_custom_pre_pass`，而不是更晚？因为此时图还是 ATen 级别的 `relu/neg/add`，模式清楚；再晚，Inductor 可能已经做了分解、融合或代数化简，模式可能被拆散。放在更早的 Dynamo 图也不合适：那里可能还有 Python 语义和非 ATen 节点。

### 2. 预期结果：结构证据比毫秒数更硬

参考实现用两个子进程分别编译 `baseline` 和 `optimized`，每个变体一个独立 `TORCHINDUCTOR_CACHE_DIR`，避免缓存复用。`child()` 里先算 eager 参照，再在 `cfg.patch` 内完成第一次调用；然后用 `common.bench_ms(..., iters=30, warmup=8)` 测稳态时间，用 `common.kernel_stats(src)` 解析结构。

参考轮次的核心数字：

| 指标 | baseline | optimized | 说明 |
|---|---:|---:|---|
| 生成 kernel 数 | 1 | 1 | pointwise+reduction 融成一个生成 kernel |
| 向量 kernel 数 | 1 | 1 | 两者都走 AVX2 向量化 |
| 向量 tmp 指令 | 5 | 2 | `relu/neg/relu/add` 被 `abs` 取代 |
| max abs err | 1.95e-3 | 1.95e-3 | 来自 512 项 float32 求和顺序，不是改写引入 |
| 外部 kernel | `addmm` | `addmm` | matmul 仍走 `extern_kernels.addmm` |
| 相对耗时 | 1.00x | 约 0.70x | 课程记录的参考轮次；单机单轮，噪声可达 20%–50% |

这组数字要这样读：

1. **kernel 数没有下降，仍然是 1。** 改写优化的是 kernel **内部指令**，不是 kernel 数量。两个 `relu` 和一个 `add` 本来就在同一个融合 kernel 里；`abs` 让这个 kernel 少做三条指令。
2. **tmp 5→2 是结构证据。** 它直接来自 `output_code.py` 的 `auto tmp...` 计数，不受 CPU 频率和噪声影响。即使时间测不出来，结构也证明改写生效了。
3. **1.95e-3 不是“abs 写错了”。** baseline 和 optimized 的误差相同，说明差异来自两条路径共享的归约顺序；`abs` 本身在 `lab_b` 里是逐元素 0 误差。
4. **0.70x 不是承诺。** 模型里 `addmm` 占了不少时间，pointwise 指令减少只影响剩余部分；同一脚本换一轮可能得到明显不同的比值。仓库里旧的 `final_report.md` 就记录过 1.19x。报告必须写条件，不能把一次测量写成“稳定加速 30%”。

参考实现的断言也是按这个逻辑写的：

```python
common.check("数学等价（fp32 归约容差）", opt["max_err"] <= 5e-3, ...)
common.check("kernel 数不增加", opt["kernels"] <= base["kernels"], ...)
common.check("融合 kernel 指令数下降", opt["tmp_ops"] < base["tmp_ops"], ...)
common.check("matmul 仍走 extern kernel", "addmm" in base["extern"], ...)
```

这些断言覆盖了正确性、结构、外部调用三个维度，但没有把“必须更快”写成硬断言，这是对的：噪声大的机器上，时间断言会变成 flaky test。

### 3. 你的任务：换一个等价改写

在参考实现的基础上，选择一个**新的数学等价改写**，实现为 post-grad pass，并交一份报告。可以从下面的候选里选一个，也可以自己设计，但必须先证明等价性：

| 候选 | 改写 | 等价性 | 预期难点 |
|---|---|---|---|
| A | `relu(x) - relu(-x) -> x` | 对实数逐点成立；float32 也逐点成立 | Inductor 可能更早化简掉，pass 未必看得到 |
| B | `relu(x) + relu(-x) -> abs(x)` | 参考实现 | 已做过，不能重复交 |
| C | `neg(neg(x)) -> x` | 逐点精确 | 很可能在 post-grad 前已被消除，匹配数为 0 |
| D | `x * sigmoid(x) -> x / (1 + exp(-x))` | 数学等价，float32 需容差 | 不保证指令更少，可能是负结果 |
| E | `softplus(x) - softplus(-x) -> x` | 数学恒等，但数值稳定性不同 | 需要仔细定义容差和 NaN/溢出 |

候选 A 的实现骨架：

```python
def relu_diff_to_identity(graph):
    aten = torch.ops.aten
    for node in list(graph.nodes):
        if node.op != "call_function" or node.target is not aten.sub.Tensor:
            continue
        a, b = node.args[:2]
        if not (isinstance(a, torch.fx.Node) and isinstance(b, torch.fx.Node)):
            continue
        if a.op != "call_function" or a.target is not aten.relu.default:
            continue
        if b.op != "call_function" or b.target is not aten.relu.default:
            continue
        if not isinstance(a.args[0], torch.fx.Node):
            continue
        neg = b.args[0]
        if not (isinstance(neg, torch.fx.Node) and neg.op == "call_function"
                and neg.target is aten.neg.default):
            continue
        if neg.args[0] is not a.args[0]:
            continue
        node.replace_all_uses_with(a.args[0])   # relu(x) - relu(-x) == x
    graph.eliminate_dead_code()
    graph.lint()
    return graph
```

注意：`replace_all_uses_with(a.args[0])` 直接把消费者接到 `x`，不需要新建节点。这个改写如果成功，tmp 指令可能从 3 条降到 0 条，但它也可能根本没机会触发，因为 Inductor 的内置化简先把模式消掉了。**匹配数为 0 也是有效结果**，只要你能用 `fx_graph_readable.py` 或 `post_grad_graphs` 证明模式在 pass 之前已经不存在。

候选 D 的实现提示：先匹配 `mul.Tensor`，其中一个输入是 `sigmoid.default`，另一个输入与 `sigmoid` 的输入是同一节点；然后用 `exp`、`add`、`div` 重建。它不保证更快，可能因为多了一个 `exp` 而更慢；报告要如实写“结构变差/时间不可区分”，并解释为什么。

无论选哪个，任务要求：

1. **等价性证明**：写出为什么在实数上成立；在 float32 上逐点还是需要容差；NaN/Inf 的行为是否一致。
2. **pass 实现**：用 `post_grad_custom_pre_pass` 或 `post_grad_custom_post_pass`；回调参数是 `Graph`；替换后调用 `eliminate_dead_code()` 和 `lint()`。
3. **匹配计数**：在 pass 里统计实际替换次数；如果为 0，解释为什么（被更早的 pass 消掉、图阶段不对、模式不匹配）。
4. **差分测试**：至少 3 种 shape（含一个非 8 倍数尾部）、2 种布局（连续 + 非连续）、float32；与 eager 对照；容差有依据。
5. **结构断言**：kernel 数不增加；tmp 指令数下降，或解释为什么结构没变；`addmm` 仍走 extern；记录 `generated_kernel_count` / `vec_kernel_count`。
6. **隔离 benchmark**：baseline 与 optimized 各自子进程、各自 cache；warmup 在计时外；至少 5 轮 × 20 次，报告中位数和 min/max；编译时间单独记录。
7. **诚实结论**：如果时间区间重叠，写“不可区分”；如果更慢，写“负结果 + 原因”；不要为了凑加速改容差或删用例。
8. **可复现**：提交脚本 + 报告 + 运行命令；不修改 `site-packages`，不依赖手工改 cache。

### 4. 评分标准（100 分）

| 项目 | 分值 | 优秀（满分档） | 合格 | 不合格 |
|---|---:|---|---|---|
| 等价性与正确性 | 30 | 证明清晰；逐点/容差分类正确；3 shape × 2 布局全过；NaN/Inf 有说明 | 主要用例通过，容差有依据 | 只比最终标量；容差拍脑袋；非连续未测 |
| 结构证据 | 20 | 匹配次数、kernel 数、tmp、extern 全有；能解释每个数字 | 有 kernel/tmp 对照 | 只看时间；把 addmm 当失败；没证明 pass 触发 |
| 性能方法 | 20 | 子进程隔离；多轮；warmup；median/min/max；编译与运行分开；结论与噪声匹配 | 有基线和优化对比 | 单轮；同进程复用 cache；把编译时间混进 runtime |
| 报告质量 | 15 | 条件、命令、产物、限制、复现步骤齐全；结论可反驳 | 有表格和结论 | 只有截图/一句“更快了” |
| 可复现性 | 10 | 固定 seed；脚本一键跑；报告路径明确；无环境 hack | 命令基本可跑 | 依赖手工改安装目录；缺输入 |
| 代码质量 | 5 | pass 小而清晰；恢复/清理到位；无调试残留 | 能读 | 复制粘贴、全局变量污染、print 泛滥 |

扣分项：

- 改写不是数学等价，却用大容差掩盖（正确性 0 分起步）。
- pass 没有真正触发，却声称“结构优化”（结构证据 0 分）。
- 只跑一轮就宣称“显著加速”，且没有区间（性能方法最多 8 分）。
- 修改 `site-packages` 且没有回滚说明（可复现性 0 分）。
- 报告没有写 shape、dtype、stride、线程数（报告质量最多 8 分）。

加分项（最多 10 分，总分不超过 100）：

- 用 `torch._dynamo.explain` 证明 graph break 情况，或证明 `fullgraph=True` 可整图编译；
- 用 `ir_pre_fusion.txt` / `ir_post_fusion.txt` 解释为什么模式在某个阶段消失；
- 对负结果给出可验证的下一步实验，而不是“以后再说”；
- 把改写做成 pattern matcher 规则，并说明为什么比手写遍历安全。

### 5. 提交模板与建议节奏

报告模板：

````markdown
# Final Project：<你的改写名>

## 1. 结论摘要
- 改写：
- 是否等价（逐点/容差）：
- 匹配次数：
- kernel 数 / tmp 指令：
- 时间结论（含区间）：
- 最大误差：

## 2. 等价性
- 数学证明：
- float32 行为：
- NaN/Inf：

## 3. 实现
- pass 位置：
- 匹配条件：
- 替换与 DCE：
- 如何统计匹配次数：

## 4. 正确性
| shape | layout | dtype | max_err | tolerance | 结论 |

## 5. 结构
| variant | kernels | vec kernels | tmp | extern | 备注 |

## 6. 时间
- 条件：CPU / torch / threads / rounds / iters / warmup
| variant | median ms | min | max | drift | 相对 |

## 7. 解释与限制
- 为什么快/不快：
- 哪些阶段可能消掉模式：
- 未覆盖：

## 8. 复现
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\<你的脚本>.py
```
````

建议节奏（总计 4–6 小时，不含首次环境搭建）：

1. **30 分钟**：跑参考实现，读 `final_report.md`，确认结构数字能复现；
2. **60 分钟**：选改写，写等价性证明，先用 eager 小脚本验证候选模式；
3. **60 分钟**：写 pass，打印 `fx_graph_readable.py` 或匹配次数，确认 pass 真的看到图；
4. **60 分钟**：补 shape/布局矩阵和差分测试；
5. **60 分钟**：子进程隔离 benchmark，多轮采样；
6. **45 分钟**：写报告，检查每个数字都有来源。

## 可跑实验

先复现参考实现：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py
```

预期输出包含：

- 两行 `RESULT {...}`，分别是 `baseline` 和 `optimized` 的 JSON；
- 一张表：两个变体的 `kernels=1`、`vec=1`、`tmp ops` 从 5 到 2、`max err=1.95e-03`；
- 四条 `PASS`：数学等价、kernel 数不增加、指令数下降、matmul 仍走 extern；
- 报告路径 `compilers\inductor_deep_dive\code\.cache\final_report.md`。

读报告：

```powershell
Get-Content -Encoding UTF8 compilers\inductor_deep_dive\code\.cache\final_report.md
```

报告里的 `optimized / baseline` 是相对耗时。课程记录的参考轮次约为 **0.70x**；如果你跑出 1.0x 以上，不代表实现错了，先看两次测量的区间和 `addmm` 的占比，再决定是否重跑。时间只作为辅助证据，`tmp 5→2` 和 `max err 1.95e-3` 才是可重复的结构/数值证据。

你的脚本写好后，用同一个入口运行（把 `<你的脚本>` 换成实际文件名）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\<你的脚本>.py
```

如果你的脚本把报告写到 `code\.cache\<你的报告>.md`，用同样的 `Get-Content -Encoding UTF8` 读取。不要修改参考脚本，保留它作为对照。


## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `compilers/inductor_deep_dive/code/labs/final_project.py` | `relu_pair_to_abs`、`model`、`child`、`run_child`、`main` | 参考 pass、子进程隔离、报告生成和四条断言 |
| `compilers/inductor_deep_dive/code/labs/common.py` | `bench_ms`、`kernel_stats`、`modules_since`、`read_text_any` | 计时、结构解析、产物定位和编码兜底 |
| `torch/_inductor/config.py` | `post_grad_custom_pre_pass`、`patch` | 回调注册点和作用域 |
| `torch/_inductor/fx_passes/post_grad.py` | `post_grad_passes` | 自定义 pre pass 在内置 pass 之前的位置 |
| `torch/_inductor/graph.py` | `GraphLowering`、`GraphLowering.run` | post-grad 图如何进入 IR，`addmm` 如何变成 extern |
| `torch/_inductor/scheduler.py` | `Scheduler`、`fuse_nodes`、`SchedulerNode` | pointwise+reduction 如何融成一个 kernel |
| `torch/_inductor/codegen/cpp.py` | `CppKernel`、`CppVecKernel`、`CppScheduling` | `tmp` 指令、向量化、`step=8` 的来源 |
| `torch/_inductor/metrics.py` | `generated_kernel_count`、`generated_cpp_vec_kernel_count` | 报告里的 kernel 计数 |
| `torch/_inductor/codecache.py` | `PyCodeCache` | 为什么每个变体要独立 cache |
| `torch/_inductor/utils.py` | `fresh_inductor_cache` | 课程实验的缓存隔离 |

阅读顺序：先读 `final_project.py` 的 pass 和 `child`，再看 `post_grad.py` 确认 pass 位置，最后看 `codegen/cpp.py` 的 `tmp` 生成。报告里的每个数字都应该能在这条链上找到来源；找不到来源的数字，不要写进结论。

## 常见坑

1. **回调里写 `gm.graph`。** `post_grad_custom_pre_pass` 收到的是 `torch.fx.Graph`；直接遍历 `graph.nodes`，不要假设它是 `GraphModule`。
2. **config patch 没包住第一次调用。** `torch.compile` 是惰性的；`fn(x)` 必须在 `with cfg.patch(...)` 内。
3. **baseline 和 optimized 共用同一进程/缓存。** 第二次编译可能复用第一次的产物；用独立子进程和独立 `TORCHINDUCTOR_CACHE_DIR`，参考实现的 `run_child` 已经这样做。
4. **只比较最终 `sum`。** 归约会抵消局部错误；至少比较 `z` 或 `relu` 之后的中间量，再看最终误差。
5. **把 1.95e-3 当成改写错误。** baseline 和 optimized 共享同一归约路径，误差相同；如果只有 optimized 变大，才怀疑改写。
6. **把 kernel 数 1 理解成“没有 matmul”。** `addmm` 走 `extern_kernels`，不计入生成的 pointwise kernel；结构表要同时写 `kernels` 和 `extern`。
7. **用 0.70x 宣称普适加速。** 参考轮次是单机单轮，模型里 `addmm` 占比高，pointwise 指令减少只影响一部分；报告要写区间、条件和负结果可能。
8. **为了让 pass 触发而改模型语义。** 例如把 `relu(-z)` 改成 `-relu(z)` 再匹配，数学上可能不等价；先证明等价，再写匹配条件。
9. **忽略更早的 pass 已经消掉模式。** `neg(neg(x))`、部分 `relu` 恒等式可能在 post-grad 之前就被处理；匹配数为 0 时，用 `post_grad_graphs` 或 `fx_graph_readable.py` 证明模式不存在，而不是硬说 pass 没生效。
10. **报告只写“更快”。** 没有 shape、dtype、stride、线程、轮次、区间和误差，别人无法复现；这种报告按不及格处理。

## 交付检查与答辩

提交前逐项检查：

- [ ] 脚本能从仓库根目录用统一入口一次跑完；
- [ ] 固定 seed，baseline 与 optimized 使用同一组输入；
- [ ] 至少 3 种 shape、2 种布局、float32；
- [ ] 逐点改写用 `tol=0`，需要容差的改写写出依据；
- [ ] pass 有匹配计数，且报告里写了实际次数；
- [ ] kernel 数、tmp 指令、extern 调用都有前后对照；
- [ ] benchmark 子进程隔离、warmup、至少 5 轮 × 20 次；
- [ ] 报告有 median/min/max、编译时间、结论和限制；
- [ ] 没有修改 `site-packages`，没有手工改 cache；
- [ ] 参考脚本保持原样，自己的脚本可独立运行。

<details>
<summary>答辩问题与参考回答要点</summary>

**问：为什么 kernel 数还是 1，却说优化有效？**

答：优化发生在 kernel 内部。`relu/neg/relu/add` 原本就和 reduction 在同一个融合 kernel 里；`abs` 把五条向量 tmp 指令降到两条。kernel 数不变，但每条元素的指令数下降。

**问：1.95e-3 的误差为什么可以接受？**

答：它来自 512 项 float32 求和的顺序差异，baseline 和 optimized 都有；改写本身在 `lab_b` 里逐元素误差为 0。容差 5e-3 是按归约长度和实测分布定的，不是为了让测试通过而临时放宽。

**问：0.70x 能说明这个改写值得上线吗？**

答：不能单独说明。它是单机单轮结果，且 `addmm` 占了不少时间；同一脚本另一轮可能得到 1.19x。要上线需要多轮、多机器、真实模型占比和端到端指标。结构上的 5→2 是稳定收益，时间收益要看场景。

**问：如果候选 A 的匹配数是 0，是不是失败了？**

答：不一定。它说明模式在 post-grad 图里已经不存在，可能被更早的 pass 化简。用 `post_grad_graphs` 或 `fx_graph_readable.py` 证明这一点，并解释为什么这个改写没有额外收益；这比伪造一个匹配更有价值。

**问：为什么不能把 `(x+a)+b` 改成 `x+(a+b)` 当作精确改写？**

答：float32 加法不满足结合律，两个表达式的舍入不同。它可以作为“数学等价但需容差”的候选，但不能声称逐点精确；容差要按实测分布定，并说明输入范围。

**问：为什么不能直接改 `output_code.py`？**

答：它是缓存产物，下一次编译会被覆盖，而且不改变编译器行为。正确位置是 post-grad pass、lowering 或 codegen；改产物只能用于一次性验证假设。

</details>

## 自我检查

- [ ] 我能复述参考模型的 `addmm -> relu/neg/relu/add -> sum` 结构，并指出哪部分是 extern。
- [ ] 我能解释 `relu_pair_to_abs` 的四个匹配条件，以及 `is not` 为什么不能用 `!=` 替代。
- [ ] 我知道 `post_grad_custom_pre_pass` 收到的是 `Graph`，替换后要 `eliminate_dead_code()` 和 `lint()`。
- [ ] 我能复现 kernel 数 1、tmp 5→2、误差 1.95e-3、参考轮次约 0.70x，并说明每个数字的条件。
- [ ] 我能设计一个数学等价的新改写，并写清楚它在 float32 上是否需要容差。
- [ ] 我能证明 pass 是否真的触发；匹配数为 0 时能解释原因。
- [ ] 我能用子进程隔离和多轮测量做 benchmark，并报告区间而不是单点。
- [ ] 我能在时间没有改善时写出负结果和原因，而不是修改容差或删用例。
- [ ] 我的报告包含条件、命令、产物路径、限制和复现步骤。
- [ ] 我没有修改 `site-packages`，也没有把未运行的上游测试写成“已通过”。

## 延伸阅读

- **PyTorch 文档，`torch.compile` 与 TorchInductor**：读整体流程、config 和调试入口；Final Project 的每个阶段都能在文档里找到对应概念。
- **PyTorch 文档，Torch FX `Graph` / `Node`**：读 `inserting_before`、`replace_all_uses_with`、`eliminate_dead_code`、`lint`；这是写 pass 的最小 API 集。
- **PyTorch 文档，`torch.testing.assert_close` 与数值精度**：读 `atol`/`rtol`/NaN 语义；用来给 1.95e-3 定容差。
- **PyTorch 文档，`torch._dynamo.explain` 与 graph break**：读 `graph_count`、`break_reasons`；用来证明 `fullgraph=True` 的边界。
- **TorchInductor 源码，`torch/_inductor/fx_passes/post_grad.py` 与 `pattern_matcher.py`**：读内置 pass 顺序和 pattern replacement；理解为什么手写遍历要尽量小、可证明。
- **TorchInductor 源码，`torch/_inductor/codegen/cpp.py`**：读 `CppVecKernel` 和 `CppKernel`；把 `tmp 5→2` 对应到具体生成行。
- **`compilers/inductor_deep_dive/07-write-a-custom-pass.md` 与 `21-performance-engineering.md`**：复习 pass 写法与测量方法；Final Project 是这两章的组合。
- **`compilers/mini_ai_compiler/final-project-mini-ai-compiler.md`**：看另一门课如何做端到端交付；对照“自己造编译器”和“改造生产编译器”的报告结构。

---

[上一章：练习 D 性能扫掠](./practice-d-performance-sweep.md) | [下一章：课程 README](./README.md)