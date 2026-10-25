# Ch 22 正确性与 fallback：把“能跑”变成“跑对”

> **目标**：给 `torch.compile` 的输出建立一套可复现的正确性证据：会用 eager 与编译结果做差分测试，能按 dtype、归约长度和数学等价性选择容差，能覆盖边界 shape 与非连续布局，能用 `torch._dynamo.explain` 识别 graph break，能从 wrapper 源码和 metrics 识别 fallback 与 extern kernel。学完本章，你能判断一次“编译成功、形状也对”的运行到底有没有跑对。
>
> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43（`cl.exe`），CPU 仅 AVX2。命令在仓库根目录 `D:\Learning-with-Claude` 执行；所有课程脚本统一通过 `powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本>` 运行，由它导入 vcvars、设置 `TORCHINDUCTOR_CACHE_DIR=code/.cache` 与 `PYTHONPATH=code`。本章引用的 1.95e-3 误差、5→2 指令数和 1 个 kernel 都来自本机 `final_project.py` 的实际输出。

## 为什么

编译器的失败很少以“崩溃”的形式出现。更常见的情况是：程序正常返回，shape 和 dtype 都对，日志里也没有异常，但某个元素错了，或者整段计算悄悄退回 eager。你如果没有独立参照，根本不知道它错了。

`torch.compile` 让这个问题更隐蔽，因为它同时改变三件事：

1. **数值路径**：算子被分解、融合、向量化，浮点运算的结合顺序和中间存储都变了；
2. **图的边界**：Dynamo 可能在某个 Python 操作处断开图，剩下的部分继续用 eager 执行；
3. **执行位置**：没有 lowering 的算子留在 wrapper 里，或者整个子图交给 `extern_kernels`，看起来“编译过”，实际只有一部分进了生成 kernel。

这三件事都不一定报错。`final_project.py` 就是一个好例子：`relu(z) + relu(-z)` 改写成 `abs(z)` 在 float32 上逐元素精确相等，但 `sum(dim=1)` 的归约顺序变了，最终最大绝对误差是 **1.95e-3**。如果容差设成 `1e-6`，你会把一次正确改写判成失败；如果容差设成 `1e-1`，你又可能放过真正的索引错误。正确性工作的核心，是让“误差来自哪里”变得可解释。

本章不讨论“怎么让程序不报错”，而是讨论“怎么证明它算对了”。你需要同时看四类证据：数值、结构、边界和边界之外的控制流。

## 主线

### 1. 四层对照：不要只比较最后一个标量

差分测试的第一条原则是：**参照实现和被测算子必须独立，比较点要尽量靠近出错位置**。对 `final_project` 的模型，至少有四层可以对照：

| 层 | 参照 | 被检对象 | 能发现什么 |
|---|---|---|---|
| L1 数学恒等 | eager 的 `relu(z)+relu(-z)` | 改写后的 `abs(z)` | 改写规则本身是否等价 |
| L2 编译前后 | eager 的完整模型 | `torch.compile` 的完整模型 | 融合、向量化、归约顺序带来的差异 |
| L3 中间量 | eager 的 `z = x @ w + b` | 生成代码里的 `z` 或 `buf` | matmul 之外的 pointwise 是否被改错 |
| L4 结构 | 预期的 kernel 数、tmp 指令、extern 调用 | 实际 `output_code.py` | “数值碰巧对”但根本没融合/没改写 |

只比较 L2 的最终 `sum` 有一个致命问题：正负误差会互相抵消。一个索引偏一位的错误，可能在 512 项求和后只剩很小的差异；一个 `relu` 符号写反的错误，也可能因为数据分布对称而被平均掉。比较中间张量 `z` 能把问题缩小到 matmul 之后、归约之前。

一个最小的差分函数：

```python
import torch


def diff_report(got: torch.Tensor, ref: torch.Tensor, name: str) -> dict:
    if got.shape != ref.shape:
        raise AssertionError(f"{name}: shape {tuple(got.shape)} != {tuple(ref.shape)}")
    if got.dtype != ref.dtype:
        raise AssertionError(f"{name}: dtype {got.dtype} != {ref.dtype}")
    if got.isnan().any() or ref.isnan().any():
        nan_got = got.isnan().sum().item()
        nan_ref = ref.isnan().sum().item()
        if nan_got != nan_ref:
            raise AssertionError(f"{name}: NaN count {nan_got} != {nan_ref}")
        mask = ~torch.isnan(ref)
        got, ref = got[mask], ref[mask]
    delta = (got - ref).abs()
    denom = ref.abs().clamp_min(1e-12)
    return {
        "name": name,
        "max_abs": delta.max().item() if delta.numel() else 0.0,
        "max_rel": (delta / denom).max().item() if delta.numel() else 0.0,
        "mean_abs": delta.mean().item() if delta.numel() else 0.0,
        "numel": delta.numel(),
    }
```

这段代码做了三件容易被省略的事：先检查 shape/dtype，单独处理 NaN，再同时报告绝对误差和相对误差。`max_abs` 对接近零的值很敏感，`max_rel` 对大值很敏感；两个一起看，才能判断误差是“全局漂移”还是“某个小值附近的舍入”。

如果使用 `torch.testing.assert_close`，把容差和 NaN 策略显式写出来：

```python
torch.testing.assert_close(
    got,
    ref,
    atol=5e-3,
    rtol=1e-4,
    equal_nan=True,
    check_dtype=True,
    check_device=True,
)
```

不要依赖默认参数来“碰运气”。默认值适合常规单测，不一定适合一次归约顺序被重排的编译结果。

### 2. 容差怎么定：从 dtype 和归约长度推，不要从感觉推

float32 的有效位数约 7 位十进制，单次运算的相对误差在 `1e-7` 量级。对长度为 `K` 的归约，最坏情况会随 `K` 增长；即使输入是随机数，误差也常常落在 `sqrt(K) * eps * scale` 附近。`K=512` 时，`1e-3` 量级的绝对误差完全可能来自正常的浮点重排，而不是编译器 bug。

本章采用的容差分层：

| 场景 | 建议容差 | 理由 |
|---|---|---|
| 逐元素恒等改写，例如 `relu(x)+relu(-x) -> abs(x)` | `atol=0, rtol=0` | 同一组输入、同一 dtype 下逐元素结果应完全相等；`lab_b` 实测为 0 |
| 单层 pointwise，含 `sin/exp` 等超越函数 | `atol=1e-6, rtol=1e-5` | 允许实现库之间的最后一位差异 |
| 长度 512 的 float32 归约 | `atol=5e-3, rtol=1e-4` | `final_project` 实测最大绝对误差 1.95e-3，来自求和顺序 |
| 长度更大的归约 | 先跑 eager 与 float64 参照，按实测分布定 | 不能把 512 的阈值直接套到 4096 |
| 含 NaN/Inf 的输入 | 单独定义策略并显式传 `equal_nan` | `max_abs` 遇到 NaN 会变成 NaN，普通比较失效 |

这里有一个关键区分：**数学等价不等于逐位相等**。`relu(z)+relu(-z)` 和 `abs(z)` 在实数上是同一个函数，在 float32 上也逐元素相等；但 `sum` 把它们按不同顺序加起来，结果就不再逐位相同。前者可以用 `tol=0`，后者只能用归约容差。

同理，`(x + a) + b -> x + (a + b)` 在实数上成立，在 float32 上却**不**逐元素成立，因为加法不满足结合律。把它当成“精确改写”会得到错误结论。练习里会要求你为每个改写标注它是“逐元素精确”“数学等价但需容差”还是“只在特定输入分布下近似”。

容差不是越松越安全。一个偏一位的索引错误，可能让每个元素都取到邻居值，误差量级和输入同阶；`atol=1e-3` 挡不住它。反过来，`atol=0` 检查 512 项归约，会把正常编译结果判死。正确做法是：先用一个已知正确的实现测出误差分布，再把阈值放在分布尾部之外，并在报告里写出依据。

### 3. 边界 shape 与非连续布局：错误常藏在“最后一个元素”

Inductor 的 CPU 代码生成会处理向量化主循环和尾部循环。长度为 8 的倍数时，尾部可能不执行；长度为 2047 时，尾部循环才会暴露索引错误。只测 `(1024, 1024)` 会漏掉一整类问题。

建议的最小矩阵：

| 维度 | 用例 | 观察点 |
|---|---|---|
| 尾数 | `2047`、`2049`、`3` | 标量尾部是否正确，向量步长是否为 8 |
| 秩 | `(512,)`、`(1, 512)`、`(512, 1)` | 广播和 squeeze/unsqueeze 是否保持语义 |
| 广播 | `(512, 1) + (1, 512)` | 两个方向的 stride 是否都正确 |
| 非连续 | `x.t()`、`x[:, ::2]` | 是否插入 copy kernel，load 的 stride 是否正确 |
| 空张量 | `(0, 512)` | 是否返回空结果且不越界；以 eager 行为为准，不预设 |
| 动态 shape | 同一函数喂 3–4 个尺寸 | 是否重编译，结果是否仍一致 |

非连续输入尤其容易“看起来对”。`x.t()` 的 shape 没变，但 stride 从 `(2048, 1)` 变成 `(1, 2048)`；生成代码如果按连续布局读，会读到转置后的错误元素。比较时不要先 `.contiguous()` 再算参照，否则你把要测的布局信息丢掉了。正确顺序是：

```python
x = torch.randn(512, 512).t()      # 保持非连续视图
ref = model(x)                      # eager 也在这个视图上算
out = compiled(x)                   # 编译结果在同一个视图上算
diff_report(out, ref, "transpose")
```

如果你怀疑 Inductor 插入了一次 copy，去看 `ir_pre_fusion.txt` 和 `output_code.py` 里的 kernel 数。copy 不是错误，它是布局转换的合法实现；但它会改变“一个 kernel”的结构断言，也会影响计时。第 21 章的性能结论必须和这里的布局条件一起读。

### 4. graph break：编译“成功”但只编译了一半

graph break 是 Dynamo 在遇到不支持的 Python 控制流或副作用时，把图切开、剩下的部分交回 eager 的行为。它通常不影响数值正确性，但会让你以为“整段都被编译了”。识别它的入口是 `torch._dynamo.explain`：

```python
import torch


def f(x):
    y = torch.sin(x)
    if y.sum().item() > 0:     # .item() 把张量变成 Python 标量，常见断点
        return y + 1
    return y - 1


x = torch.randn(64, 64)
expl = torch._dynamo.explain(f)(x)
print(type(expl).__name__)
print("graphs:", expl.graph_count)
print("ops:", expl.op_count)
print("ops_per_graph:", expl.ops_per_graph)
for reason in expl.break_reasons:
    print("break:", reason)
```

`ExplainOutput` 是一个对象，不是字典或元组，不能写 `expl[0]`。本章环境里可用的字段包括 `graph_count`、`op_count`、`ops_per_graph`、`out_guards` 和 `break_reasons`。字段名比具体打印格式稳定，报告里记录字段值即可。

两种常用策略：

- **诊断**：正常 `torch.compile(f)`，用 `explain` 看断了几次、每次多少 op，再读 `break_reasons` 找触发点。
- **强制暴露**：`torch.compile(f, fullgraph=True)`，一旦有断点就抛错。它适合在 CI 里锁住“这个模型必须整图编译”的约束。

graph break 和 fallback 不是一回事：

| 现象 | 发生位置 | 后果 | 识别方式 |
|---|---|---|---|
| graph break | Dynamo 前端 | 图被切成多段，段间回到 eager | `explain` 的 `graph_count`/`break_reasons` |
| fallback | Inductor lowering | 单个算子留在 wrapper，图中其他部分仍可融合 | wrapper 源码里的 `torch.ops.*` 调用 |
| extern kernel | Inductor 调度 | 子图交给外部库（如 `addmm`） | wrapper 里的 `extern_kernels.*` 调用 |

把三者混为一谈，会导致错误的优化方向：graph break 要改前端写法或加 Dynamo 支持；fallback 要补 lowering；extern kernel 往往是刻意选择，不该盲目“消灭”。

### 5. fallback 识别：从 wrapper 源码和 metrics 找证据

`lab_c_lowering.py` 给了最干净的对照。未注册 lowering 的自定义算子 `mylab.affine_naive` 会以 `torch.ops.mylab.affine_naive.default(...)` 的形式留在 wrapper 里；注册 lowering 后，`mylab.affine` 被翻译成 IR 的 `mul/add`，和 `relu` 融进同一个 kernel。

一个可复用的扫描器：

```python
import re


def scan_generated(src: str) -> dict:
    custom = sorted(set(re.findall(r"torch\.ops\.[\w.]+", src)))
    extern = sorted(set(re.findall(r"extern_kernels\.(\w+)\(", src)))
    kernels = src.count('extern "C" __declspec(dllexport)')
    return {
        "custom_ops": custom,
        "extern_kernels": extern,
        "kernel_count": kernels,
    }
```

判读规则：

1. **`custom_ops` 非空**：至少有一个算子没有 lowering，正在走隐式 fallback。看它是否在热点路径上；如果是，补 lowering（第 05、17 章）。
2. **`extern_kernels` 含 `addmm`/`mm`/`bmm`**：矩阵乘走了外部库，这是常见且合理的选择。`final_project` 的 `x @ w + b` 就生成 `extern_kernels.addmm(...)`，而 pointwise+reduction 仍是 1 个生成 kernel。
3. **`kernel_count` 与预期不符**：可能是 fallback 把图切开，也可能是布局 copy、reduction 拆分或 extern 子图造成的。不要只看数字，回到 `ir_pre_fusion.txt` 看节点。

`torch._inductor.metrics` 提供另一个角度：

```python
import torch._inductor.metrics as metrics

metrics.reset()
# ... 在这里做第一次编译 ...
print("generated kernels:", metrics.generated_kernel_count)
print("vectorized kernels:", metrics.generated_cpp_vec_kernel_count)
```

`generated_kernel_count` 统计的是本次编译生成的 kernel 数。`final_project` 的基线是 1，改写后仍是 1；这说明改写优化的是 kernel **内部指令**，不是 kernel 数量。`generated_cpp_vec_kernel_count` 则告诉你其中有多少走了向量化路径。两者都要和源码结构对照，单看一个数字容易误判。

fallback 不一定必须消灭。一个只调用一次、占比极低的冷算子，补 lowering 的维护成本可能高于收益；一个语义复杂、没有合适 IR 表示的算子，留在 wrapper 里反而更安全。判断标准是：它在不在热点路径、是否阻断邻接融合、调用开销是否可测。把这些写进报告，比“看到 `torch.ops` 就优化”更专业。

### 6. 一张可复用的正确性清单

把上面的检查压成一张清单，每次改 pass、改 lowering、升级 torch 后都跑一遍：

```text
[ ] 固定 seed，记录 dtype/device/shape/stride
[ ] eager 参照与编译结果在同一输入、同一布局上计算
[ ] 先比较中间张量，再比较最终归约
[ ] shape/dtype/NaN 计数单独检查
[ ] 容差有依据：逐元素精确 / 归约容差 / 分布相关
[ ] 覆盖尾数、广播、非连续、空张量、动态 shape
[ ] explain 的 graph_count 与 break_reasons 已记录
[ ] wrapper 的 custom_ops / extern_kernels / kernel_count 已记录
[ ] 变体在独立子进程和独立 cache 下运行
[ ] 报告里写清“未覆盖什么”
```

最后一条经常被忽略。正确性报告的价值不在于写“全部通过”，而在于写清“在哪些条件下通过、哪些条件没有测”。一个只测了 `(1024, 1024)` 连续 float32 的结论，不能推广到转置输入和动态 shape。

## 可跑实验

下面四条命令都用统一入口运行，输出可以直接作为本章的证据。第一次编译需要调用 `cl.exe`，耗时约 5–15 秒；后续命中缓存会更快。

```powershell
# 1. 确认环境与最小编译闭环
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py

# 2. fallback 与 lowering 的对照
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py

# 3. 逐元素精确改写：relu(x)+relu(-x) -> abs(x)
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py

# 4. 归约容差：final_project 的差分测试与基准
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py

# 5. 读自动生成的报告
Get-Content -Encoding UTF8 compilers\inductor_deep_dive\code\.cache\final_report.md
```

预期证据：

| 实验 | 应看到的结构 | 应看到的数值 |
|---|---|---|
| `env_report.py` | `CPU ISA` 里出现 `VecAVX2`，`cl.exe` 有路径 | 最小编译结果与 eager 一致，打印 `OK` |
| `lab_c_lowering.py` | 未注册 lowering 的源码保留 `torch.ops.mylab.affine_naive`；注册后 wrapper 不再调用 `torch.ops.mylab`，只有 1 个 kernel，8 条 tmp 指令 | 两个变体都与 eager 一致 |
| `lab_b_pass.py` | 基线 5 条向量 tmp 指令，改写后 2 条 | 基线、改写后都与 eager **逐元素**一致，误差 0 |
| `final_project.py` | 两个变体都是 1 个生成 kernel、1 个向量 kernel；`tmp ops` 5→2；`extern kernels: addmm` | 最大绝对误差 1.95e-3，来自 512 项 float32 求和顺序 |

把这些输出连起来读，能得到一条完整的证据链：

1. `lab_b` 证明“改写规则本身”是精确的，所以误差 0 是合理的；
2. `final_project` 证明“改写 + 归约”产生 1.95e-3 的差异，差异来自求和顺序，而不是 `abs` 写错；
3. `lab_c` 证明 fallback 和 extern kernel 是两种不同的结构，不能只凭“有外部调用”就判定失败；
4. `env_report` 证明当前机器和工具链能复现这些结果。

再做一个观察练习：打开 `code/.cache/final_report.md`，确认 `baseline` 和 `optimized` 的 `max abs err` 是否都等于 1.95e-3。如果两者相同，说明误差不是改写引入的，而是两次运行共享的归约路径差异；如果只有 `optimized` 更大，才需要怀疑改写改变了数值语义。

`lab_c` 的 fallback 也值得逐行读。你会看到 `torch.ops.mylab.affine_naive.default(...)` 留在 wrapper 里，`relu` 单独形成一个 kernel；注册 lowering 后，`affine` 变成 `mul/add`，和 `relu` 进入同一个 loop nest。这个对照比任何文字定义都更能说明“lowering 的本质是把算子翻译成 IR”。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/compile_fx.py` | `compile_fx`、`compile_fx_inner`、`fx_codegen_and_compile` | 编译入口如何串起 AOTAutograd、post-grad pass、GraphLowering、Scheduler 和 codegen |
| `torch/_inductor/graph.py` | `GraphLowering`、`GraphLowering.run` | 图节点如何被 lower，fallback 在哪个阶段产生 |
| `torch/_inductor/lowering.py` | `register_lowering`、`fallback_handler`、`make_pointwise` | 注册表如何查表，未注册算子如何留在 wrapper |
| `torch/_inductor/metrics.py` | `generated_kernel_count`、`generated_cpp_vec_kernel_count`、`reset` | kernel 计数在什么时候累加，为什么 final 是 1 |
| `torch/_inductor/codecache.py` | `PyCodeCache` | `modules` 是 list，如何拿到本次编译新增的模块 |
| `torch/_inductor/utils.py` | `fresh_inductor_cache` | 如何为一次实验隔离缓存 |
| `torch/_dynamo/` | `explain` 入口 | `ExplainOutput` 的字段和 graph break 记录 |

阅读顺序建议：先看 `fallback_handler`，确认“没有 lowering 时会发生什么”；再看 `GraphLowering.run` 里调用 lowering 的位置；最后看 `metrics` 的计数点。不要把 `metrics` 当成语义定义，它只是观测工具，真正的结构证据在 `output_graph` 和 `output_code.py`。

`PyCodeCache.modules` 在本机是 list，不是 dict。`labs/common.py` 里的 `modules_snapshot()` / `modules_since()` 已经处理了两种形态；自己写脚本时不要直接 `PyCodeCache.modules[-1]` 就假设它是模块对象，先过滤 `__file__` 存在的项。

## 常见坑

1. **只比较最终标量。** 512 项求和会抵消局部错误；先比 `z`，再比 `sum`。如果只保留一个检查点，选输入到 reduction 之前的张量。
2. **用逐元素容差检查归约。** `atol=1e-6` 检查 512 项 float32 求和，会把正常结果判失败；反过来，`atol=1e-3` 检查 pointwise 改写，会掩盖索引错误。容差要跟场景绑定。
3. **把 graph break 当成“编译成功”。** `torch.compile` 返回可调用对象不代表整图都进了 Inductor。用 `explain` 看 `graph_count`，或者用 `fullgraph=True` 把断点变成错误。
4. **把 extern kernel 当成 fallback。** `addmm`、`mm`、`bmm` 走 `extern_kernels` 是设计选择；真正的 fallback 是 `torch.ops.*` 留在 wrapper 里。两者的优化手段不同。
5. **非连续输入先 `.contiguous()` 再比较。** 这样会把要测的 stride 信息抹掉。参照和编译结果都应使用同一个非连续视图。
6. **NaN 让最大误差失去意义。** `(got - ref).abs().max()` 在含 NaN 时返回 NaN，比较条件可能恒为假。先数 NaN 个数，再在有效元素上比较。
7. **缓存复用让“旧 kernel”冒充新结果。** 改 config 或 pass 后如果没有隔离缓存，可能测到上一次编译的产物。实验用 `fresh_inductor_cache()`，跨变体用独立子进程和独立 `TORCHINDUCTOR_CACHE_DIR`。
8. **报告只写“通过”。** 没写 shape、dtype、stride、容差依据和未覆盖项，别人无法复现，也无法判断结论的边界。

## 练习：给 final_project 加一张正确性矩阵

目标：在不修改 `final_project.py` 参考实现的前提下，复制一份自己的脚本，给 `relu-pair -> abs` 加上 shape/布局矩阵，输出一张可粘贴进报告的表格。

要求：

1. 至少覆盖 4 种 shape：`(512, 512)`、`(1, 512)`、`(3, 7)`、`(2047,)` 或你自己设计的等价边界；
2. 至少覆盖 2 种布局：连续和 `x.t()`；如果模型要求二维，转置后仍保持同一 shape；
3. 每种组合记录 `max_abs`、`max_rel`、`kernel_count`、`tmp_ops`、`max_err`；
4. 对逐元素改写使用 `atol=0`；对最终 `sum` 使用有依据的归约容差；
5. 在报告里写出哪一组用例暴露了结构差异（例如尾部循环、copy kernel），哪一组没有。

<details>
<summary>参考做法与判读要点</summary>

参考做法：

- 把 `final_project.py` 复制为 `code/labs/final_matrix.py`，保留 `relu_pair_to_abs` 和 `child` 的骨架；
- 把 `x = torch.randn(512, 512)` 换成 `make_case(name)`，返回 `(x, w, b)`；`w` 的第二维要与 `x` 的最后一维匹配；
- 每个 case 在独立子进程里编译，避免 `post_grad_custom_pre_pass` 的 config 状态和缓存互相污染；
- 结果写成 JSON 行，父进程汇总成 Markdown 表格。

判读要点：

- `(2047,)` 这种一维输入不适合直接复用 `x @ w + b` 的二维模型；可以把模型改成 `sum(relu(z)+relu(-z))` 的一维版本，或只对 pointwise 部分做差分。关键是把“测什么”写清楚。
- `x.t()` 的 shape 不变但 stride 变了。如果 `kernel_count` 从 1 变成 2，先怀疑布局 copy，而不是改写失败；去看 `output_code.py` 里是否有额外的 copy kernel。
- 如果某个 case 的 `max_abs` 明显大于 1.95e-3，先确认它的归约长度是否也是 512。长度变了，容差也要重算。
- 如果所有 case 的误差都是 0，检查你是不是不小心把 `sum` 去掉了，或者参照和编译结果用了同一个被修改过的张量。

</details>

## 自我检查

- [ ] 我能说出 L1–L4 四层对照分别比较什么，以及为什么不能只比最终标量。
- [ ] 我能解释 `relu(z)+relu(-z) -> abs(z)` 为什么可以用 `tol=0`，而 `sum` 为什么需要 1e-3 量级容差。
- [ ] 我能为一个新的归约长度设计容差，而不是直接套用 5e-3。
- [ ] 我能写出覆盖尾数、广播、非连续布局和空张量的 shape 矩阵。
- [ ] 我能用 `torch._dynamo.explain` 读出 `graph_count` 和 `break_reasons`，并说明它和 fallback 的区别。
- [ ] 我能从 `output_code.py` 中识别 `torch.ops.*`、`extern_kernels.*` 和生成 kernel 的数量。
- [ ] 我知道 `generated_kernel_count` 为什么在 `final_project` 里是 1，即使还有 `addmm`。
- [ ] 我能为一个失败用例保存 seed、shape、stride、dtype 和最小复现脚本。
- [ ] 我会在报告里写“未覆盖什么”，而不是只写“全部通过”。

## 延伸阅读

- **PyTorch 文档，`torch.testing.assert_close`**：读 `rtol`、`atol`、`equal_nan`、`check_dtype` 的语义；本章的容差表和 NaN 处理直接对应这些参数。
- **PyTorch 文档，`torch.compile` troubleshooting / graph breaks**：读 graph break 的常见触发方式和 `fullgraph` 的用法；配合 `torch._dynamo.explain` 一起看。
- **PyTorch 文档，TorchInductor debugging / `TORCH_LOGS`**：读 `inductor`、`output_code`、`fusion` 等通道；`channel` 是 INFO，`+channel` 是 DEBUG。
- **`torch/_inductor/metrics.py` 与 `torch/_inductor/lowering.py` 源码**：读计数点和 `fallback_handler`；这是“结构证据”而不是“数值证据”的来源。
- **PyTorch 文档，Numerical accuracy**：读浮点归约、`float32` 精度和 `torch.use_deterministic_algorithms` 的说明；用来解释为什么同一数学在 GPU/CPU、不同线程数下会有差异。
- **Zeller, “Why Programs Fail” 与 Delta Debugging 论文**：读最小化失败输入的思路；本章的 shape 矩阵和 seed 保存是它的工程化版本。
- **`compilers/mini_ai_compiler/24-debugging-and-differential-testing.md`**：读分阶段二分和 op 级隔离；那门课在自己的 IR 上做，本章在生产 Inductor 上做，方法可以迁移。

---

[上一章：Ch 21 性能工程](./21-performance-engineering.md) | [下一章：Ch 23 改造 Inductor 与后续路线](./23-modify-inductor-and-next-steps.md)
