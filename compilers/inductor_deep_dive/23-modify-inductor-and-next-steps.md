# Ch 23 改造 Inductor 与后续路线：从钩子到上游

> **目标**：按风险从低到高掌握三种改造 Inductor 的姿势——config 钩子、进程内 monkeypatch、修改 `site-packages`；能为每种姿势写出可回滚的 PowerShell 命令；知道什么时候该停下本地 hack、转向上游贡献；能说清 AOTInductor、Triton、MLIR 三条后续路线各自解决什么问题、需要什么前置知识。
>
> **环境**：Windows 10 x86_64，Python 3.11.12，torch 2.14.1+cpu，MSVC 14.43，CPU 仅 AVX2。课程实验统一入口为 `powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本>`；`site-packages` 演练假设 torch 安装在 `.scratch\aic-env\Lib\site-packages\torch`，如果你的环境不同，先用 `env_report.py` 确认路径。所有改源码的命令都给出备份、校验和回滚步骤，且默认不要求你执行。

## 为什么

学到第 23 章，你已经能读调度 IR、写 post-grad pass、注册 lowering、测量结构差异。接下来会遇到一个更现实的问题：**想改的行为没有被 config 暴露，怎么办？**

有人直接打开 `torch/_inductor/scheduler.py` 改两行，跑通了，然后在升级 torch 后忘记自己改过什么；有人在 notebook 里 monkeypatch 了一个内部函数，换一个进程就失效；有人把 config patch 写在 `torch.compile(...)` 外面，以为生效了，实际第一次调用发生在 patch 结束之后。这些都不是“技术不够”，而是改造姿势和回滚策略没有设计。

三种姿势的代价差异很大：

| 姿势 | 生效范围 | 风险 | 适合的场景 |
|---|---|---|---|
| config 钩子 | 当前进程的 `with` 块 | 低 | 官方暴露的开关、pass 回调、调试输出 |
| monkeypatch | 当前 Python 进程，直到恢复 | 中 | 临时拦截内部函数、做实验、验证假设 |
| 改 `site-packages` | 整个 venv，所有进程 | 高 | 需要持久行为、且暂时无法走源码构建/上游 |
| fork + 源码构建 + PR | 你的分支/上游 | 最高，但可维护 | 要长期使用或贡献给社区 |

选择姿势的标准不是“哪个改得快”，而是“改完之后怎么证明、怎么撤、谁来维护”。本章把三种姿势拆开讲，并在最后给出决策树。你会看到：**大多数需求停在 config 或 monkeypatch 就够了；改安装目录应该是最后手段，不是第一反应。**

## 主线

### 1. 姿势一：config 钩子，优先使用官方出口

Inductor 把大量行为做成了 config：pass 回调、调试产物、向量化开关、线程相关选项。`torch._inductor.config.patch(dict)` 是一个上下文管理器，只在 `with` 块内改值，退出后恢复。它是最安全的改造方式，因为它不替换任何内部函数。

本课程验证过的入口：

| 配置 | 作用 | 注意 |
|---|---|---|
| `post_grad_custom_pre_pass` | 在 post-grad 内置 pass 之前插入一个 `Graph -> Graph` 回调 | 回调收到 `torch.fx.Graph`，不是 `GraphModule` |
| `post_grad_custom_post_pass` | 在内置 pass 之后插入回调 | 位置不同，看到的图不同 |
| `trace.enabled` | 生成 `<hash>.debug/` 目录，含 FX 图、融合前后 IR、`output_code.py` | 产物多，适合调试，不适合长期开启 |
| `cpp.vec_isa_ok` | 关闭后 CPU kernel 退回标量 | 用来做结构对照，不是性能优化 |
| `cpp.simdlen` | 指定 SIMD 位宽 | 是**位宽**；本机 AVX2 上 512 会退回标量 |
| `dce` / pattern 相关开关 | 控制死代码消除、模式匹配 | 改之前先确认当前版本的真实键名 |

`lab_b_pass.py` 的用法是标准范式：

```python
import torch
import torch._inductor.config as cfg

with cfg.patch({"post_grad_custom_pre_pass": relu_pair_to_abs}):
    before = common.modules_snapshot()
    fn = torch.compile(model, fullgraph=True)
    out = fn(x, y)          # 第一次调用必须发生在 patch 作用域内
    mods = common.modules_since(before)
```

这段接在 `lab_b_pass.py` 的模型、`common` 导入和 `x`/`y` 定义之后，用来观察 config 的作用域。

这里有三个容易踩的点：

1. **`torch.compile` 是惰性的。** 它只返回一个包装对象，真正编译发生在第一次调用。patch 只包住 `torch.compile(...)`、没包住 `fn(...)`，回调不会生效。
2. **回调参数是 `Graph`。** 写 `gm.graph` 会报 `AttributeError`；直接用 `for node in graph.nodes`、`graph.inserting_before(...)`、`graph.call_function(...)`。
3. **config 不是线程安全的全局状态。** 多线程同时编译不同 config 会互相污染；实验用独立进程，生产环境避免在请求路径里临时 patch。

config 的边界也要认清：它只能改“作者已经留出的旋钮”。想改变 pass 顺序、替换调度决策、修改 lowering 实现，config 里没有对应键，硬找只会浪费时间。此时进入第二种姿势。

### 2. 姿势二：monkeypatch，进程内拦截内部函数

monkeypatch 的本质是：在 Python 运行时把模块属性替换成自己的函数，调用结束后再换回来。它比改文件轻，能覆盖 config 没暴露的内部行为；代价是你依赖了内部符号名，升级后可能失效。

一个安全的上下文管理器：

```python
from contextlib import contextmanager

import torch
import torch._inductor.lowering as L
from torch._inductor.utils import fresh_inductor_cache


@contextmanager
def patched(obj, name, replacement):
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    torch._dynamo.reset()
    try:
        with fresh_inductor_cache():
            yield
    finally:
        setattr(obj, name, original)
        torch._dynamo.reset()
```

用它拦截 lowering 里的 `add`，验证 `lab_c` 的 `affine` 确实被翻译成了 `mul/add`：

```python
import torch
import torch._inductor.lowering as L

orig_add = L.add


def logging_add(a, b):
    print("[lowering] add called")
    return orig_add(a, b)


with patched(L, "add", logging_add):
    fn = torch.compile(model_lowered, fullgraph=True)
    fn(x)
```

这段接在 `lab_c_lowering.py` 的 `model_lowered` 和 `x` 定义之后；单独复制运行时，需要先注册 `mylab` 算子。

这里能生效，是因为 `_lower_affine` 在 lowering 时通过模块全局 `L.add` 查找函数；替换模块属性会改变查找结果。换成已经绑定到局部变量、或在 import 时就被闭包捕获的函数，patch 可能无效。判断方法只有一个：跑一次，看日志有没有出现；不要假设“替换了名字就一定生效”。

monkeypatch 的硬约束：

- **必须在 `finally` 里恢复**，否则后续代码继续用你的版本；
- **必须清 Dynamo/Inductor 缓存**，否则旧图或旧 kernel 可能绕过新函数；实验里用 `fresh_inductor_cache()`，跨变体用子进程；
- **不要 patch 生成代码里的字符串**，那是产物，不是行为；
- **记录你 patch 的模块、属性、原值来源**，升级 torch 后逐个复核；
- **不要在生产服务里长期 patch 内部符号**，它没有 API 稳定性保证。

monkeypatch 适合回答“如果我改这个函数，结构会不会变”。它不适合作为交付物。验证完假设后，应该把结论落成 config 回调、lowering 注册，或者上游补丁。

### 3. 姿势三：改 `site-packages`，最后手段

当行为既没有 config，也无法通过 monkeypatch 稳定拦截，才考虑直接改安装目录里的 Python 文件。这个动作会影响整个 venv 的所有进程，必须按“备份 → 修改 → 验证 → 回滚”的流程做。

先定位文件。统一入口下的 `find_source.py` 会打印源码路径和函数起始行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\find_source.py torch._inductor.scheduler Scheduler.can_fuse_vertical
```

假设输出里的路径在 `.scratch\aic-env\Lib\site-packages\torch`，先确认目标和版本：

```powershell
$site = ".scratch\aic-env\Lib\site-packages\torch"
$target = Join-Path $site "_inductor\scheduler.py"
Test-Path -LiteralPath $target
(Get-Item -LiteralPath $target).LastWriteTime
```

备份到课程缓存目录，并在原文件旁留一份 `.orig`。两份都要保留：缓存目录那份用于跨会话恢复，`.orig` 用于快速回滚。

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bak = "compilers\inductor_deep_dive\code\.cache\backup\$stamp"
New-Item -ItemType Directory -Force -Path $bak | Out-Null
Copy-Item -LiteralPath $target -Destination (Join-Path $bak "scheduler.py")
Copy-Item -LiteralPath $target -Destination "$target.orig"
Get-FileHash -LiteralPath $target -Algorithm SHA256
Get-FileHash -LiteralPath "$target.orig" -Algorithm SHA256
```

两个 SHA256 必须一致。如果不一致，先停下，不要改文件。

修改后，清除课程缓存并重新跑一次冒烟实验。`Remove-Item` 只作用于课程自己的 `.cache`，执行前先解析并核对绝对路径：

```powershell
$cache = (Resolve-Path -LiteralPath "compilers\inductor_deep_dive\code\.cache").Path
if ($cache -notlike "*\compilers\inductor_deep_dive\code\.cache") {
    throw "拒绝删除意外路径: $cache"
}
Remove-Item -LiteralPath $cache -Recurse -Force

powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

回滚：

```powershell
$site = ".scratch\aic-env\Lib\site-packages\torch"
$target = Join-Path $site "_inductor\scheduler.py"
Copy-Item -LiteralPath "$target.orig" -Destination $target -Force
Remove-Item -LiteralPath "$target.orig" -Force
(Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
```

回滚后再次运行 `env_report.py`，确认导入和编译都正常。如果你在多个 venv 里都改了文件，逐个回滚；`pip install --force-reinstall torch` 会覆盖这些改动，但它是更重的操作，且可能顺带升级版本。

改 `site-packages` 的隐性成本：

- **升级即丢失**：`pip install -U torch` 覆盖文件，你的修改无声消失；
- **不可复现**：同事拿到同一份代码，行为不同；
- **缓存错位**：旧 kernel 可能仍在 cache 里，表现为“改了没生效”或“回滚了还生效”；
- **调试困难**：traceback 指向安装目录，容易和上游源码混淆；
- **许可证/支持**：修改发行版文件不是长期维护方案。

所以它只适合“一次性验证假设”或“临时绕过阻塞”，验证完应尽快落成可维护的补丁。

### 4. 从本地 hack 到上游贡献

当你的改造对别人也有价值，下一步是上游，而不是把 `site-packages` 改动复制给同事。一个可被 review 的贡献通常包含五部分：

1. **最小复现**：固定 torch 版本、shape、dtype、布局、线程数；能用一个脚本稳定触发。
2. **定位证据**：`torch._dynamo.explain` 的图数量、`TORCH_LOGS` 的相关通道、`ir_pre_fusion.txt` / `ir_post_fusion.txt`、`output_code.py` 的前后结构。
3. **回归测试**：放在 PyTorch 源码树的 `test/inductor/` 下，覆盖正确性（eager 对照）和结构（kernel 数、是否融合、是否 fallback）。
4. **最小补丁**：优先改 pass、pattern 或 lowering 的通用逻辑，不在生成代码里打字符串补丁；行为变化配 config 开关和文档。
5. **说明取舍**：为什么不用现有 config、为什么不在前端改写、对编译时间/运行时/动态 shape 的影响。

上游流程（需要 PyTorch 源码构建环境，本课不伪造运行结果）：

```text
fork pytorch/pytorch
  -> 在 main 上确认问题仍存在
  -> 写失败测试（先红）
  -> 最小补丁（后绿）
  -> 跑相关 test/inductor 用例
  -> lint / 格式化
  -> 提交 PR，描述复现、根因、测试、风险
  -> 回应 review，补动态 shape / 不同 dtype 的覆盖
```

本地验证通常用源码树里的测试文件，例如：

```powershell
# 需要 PyTorch 源码 checkout 和已构建的开发版；本机课程环境不包含这些
python test\inductor\test_torchinductor.py -k your_test_name
```

这条命令标记为 **guided**：它要求你自备源码构建，课程不提供输出。不要在没有源码树的环境里照抄后声称“测试通过”。

贡献时最容易被打回的点：

- 只修了一个 shape，没有覆盖动态 shape 和不同布局；
- 测试只断言数值，没有断言“问题真的被修了”（例如仍然 fallback）；
- 用 `site-packages` hack 的思路改内部字符串，而不是改语义层；
- 没有说明对编译时间的影响；
- 把可选的激进优化默认打开，改变现有用户行为。

### 5. 后续路线：AOTInductor、Triton、MLIR

Inductor 不是终点。根据你接下来想解决的问题，有三条常见路线：

| 路线 | 解决什么 | 前置知识 | 和本课的关系 |
|---|---|---|---|
| AOTInductor | 把编译结果提前导出成可部署产物，减少 Python/编译开销 | `torch.export`、动态 shape、ABI、包管理 | 第 18 章产物与 ABI、第 19 章缓存、第 20 章 guards |
| Triton | 在 GPU 上写块级 kernel，控制 tile、shared memory、warp | CUDA 编程、GPU 内存层级、并行归约 | 第 03 章 loop-level IR 对比、第 15/16 章向量化与并行 |
| MLIR / StableHLO | 多层 dialect、可组合 pass、跨硬件 lowering | MLIR 基础设施、Linalg、bufferization | 第 03 章 IR 取舍、第 04 章 layout、第 06 章 pass 管线 |

**AOTInductor** 的核心问题是“部署时不想再跑 Python 编译”。它把图导出、编译、打包串起来，让推理进程加载预编译产物。你需要关心：导出的图是否稳定、动态 shape 的 guards 是否可接受、产物和 torch 版本的 ABI 是否匹配。本课第 18–20 章已经把 `output_code.py`、`.pyd`、cache key 和 guards 拆开讲过，读 AOTInductor 文档时会顺很多。

**Triton** 的核心问题是“GPU 上的 block 怎么切”。Inductor 的 GPU 后端会生成 Triton kernel；CPU 后端则走 C++/OpenMP。你在本课学到的融合决策、tile 边界、归约限制，在 Triton 里会以 `tl.arange`、mask、`tl.sum` 等形式再次出现。本机只有 CPU，本章不提供 Triton 实测，标记为 guided。

**MLIR / StableHLO** 的核心问题是“如何用一套可组合的基础设施表达多层抽象”。Torch-MLIR 把 PyTorch 图转到 StableHLO/Linalg，再往下做 bufferization 和硬件 lowering。它和 Inductor 的 loop-level IR 是两条不同的工程路线：Inductor 选择 fusion-first、把 tile 交给模板/后端；MLIR 更强调 dialect 分层和 pass 组合。想深入这条线，接着读 `compilers/mlir_core/README.md`。

### 6. 决策树：先问“要不要改”，再问“怎么改”

```text
需要改变 Inductor 的行为
├─ 只是调试/观测
│   ├─ config 有开关（trace、TORCH_LOGS） -> 用 config
│   └─ 没有开关 -> monkeypatch 一个纯 Python 函数，记录并恢复
├─ 只是实验一个 pass / lowering
│   ├─ post-grad 改写 -> post_grad_custom_pre_pass
│   └─ 自定义算子 -> register_lowering
├─ 需要所有进程持久生效
│   ├─ 能改源码构建 -> 本地 fork + editable install
│   └─ 不能 -> site-packages 备份后临时改，限期回滚
└─ 对社区有价值
    -> 最小复现 + 测试 + 上游 PR
```

一个实用原则：**先写“如果不改内部，能不能用 config/回调实现”的版本；写不出来，再降一级。** 每降一级，回滚成本和维护成本都上升。能在 config 层解决的问题，不要用 monkeypatch；能用 monkeypatch 验证的问题，不要改安装目录。

## 可跑实验

下面的实验覆盖“config 钩子”和“注册 lowering”两种安全姿势；`site-packages` 只给演练命令，不在课程里自动执行。

```powershell
# 1. 确认环境；同时用于每次改源码/回滚后的冒烟测试
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py

# 2. config 钩子：注册 post_grad_custom_pre_pass
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py

# 3. 注册 lowering：自定义算子从 fallback 进入融合 kernel
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py

# 4. 端到端改造与报告
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py

# 5. 定位一个内部函数，供 monkeypatch 或源码阅读使用
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\find_source.py torch._inductor.scheduler Scheduler.can_fuse_vertical
```

预期证据：

- `lab_b_pass.py`：pass 看到的节点序列里有 `relu -> neg -> relu -> add`；基线 5 条向量 tmp 指令，改写后 2 条；两个变体数值都与 eager 逐元素相等。
- `lab_c_lowering.py`：未注册 lowering 时 wrapper 保留 `torch.ops.mylab.affine_naive.default(...)`；注册后不再出现 `torch.ops.mylab`，只有 1 个 kernel，8 条 tmp 指令。
- `final_project.py`：两个变体都是 1 个生成 kernel，`tmp ops` 5→2，`extern kernels: addmm`，最大绝对误差 1.95e-3。
- `find_source.py`：打印模块名、文件路径、函数起始行和函数体前 2000 个字符。路径应位于当前 venv 的 `site-packages\torch\_inductor`，而不是仓库里的某个副本。

`site-packages` 演练只做“备份 + 校验 + 回滚”，不修改文件。先执行备份段落，再执行下面的回滚段落，确认 SHA256 回到原值：

```powershell
$site = ".scratch\aic-env\Lib\site-packages\torch"
$target = Join-Path $site "_inductor\scheduler.py"
$before = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash

# 备份（演练时也可以给 Copy-Item 加 -WhatIf 先看目标）
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bak = "compilers\inductor_deep_dive\code\.cache\backup\$stamp"
New-Item -ItemType Directory -Force -Path $bak | Out-Null
Copy-Item -LiteralPath $target -Destination (Join-Path $bak "scheduler.py")
Copy-Item -LiteralPath $target -Destination "$target.orig"

# 回滚
Copy-Item -LiteralPath "$target.orig" -Destination $target -Force
Remove-Item -LiteralPath "$target.orig" -Force
$after = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
"before=$before"
"after =$after"
if ($before -ne $after) { throw "回滚后哈希不一致" }
```

如果你在演练中真的改了 `scheduler.py`，回滚后还要清课程缓存并跑一次 `env_report.py`。否则旧 kernel 可能仍从 `.cache` 里被加载，给你“回滚没生效”的错觉。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/config.py` | `patch`、各配置组 | 哪些行为有官方开关；`patch` 的作用域如何实现 |
| `torch/_inductor/compile_fx.py` | `compile_fx`、`compile_fx_inner` | 编译入口在哪个阶段读取 config、调用自定义 pass |
| `torch/_inductor/lowering.py` | `register_lowering`、`fallback_handler`、`add` | 注册表、fallback 路径、可作为 monkeypatch 目标的纯 Python 函数 |
| `torch/_inductor/fx_passes/post_grad.py` | `post_grad_passes` | 自定义 pre/post pass 在管线中的真实位置 |
| `torch/_inductor/scheduler.py` | `Scheduler`、`can_fuse_vertical`、`fuse_nodes`；`BaseScheduling.can_fuse_horizontal` 接口 | 融合决策；`Scheduler` 负责垂直融合，水平融合由后端 `Scheduling` 实现 |
| `torch/_inductor/codegen/cpp.py` | `CppKernel`、`CppVecKernel`、`CppScheduling` | 向量化与循环生成；config 与源码行为如何对应 |
| `torch/_inductor/metrics.py` | `generated_kernel_count`、`generated_cpp_vec_kernel_count` | 改造前后结构断言的计数器 |
| `torch/_inductor/utils.py` | `fresh_inductor_cache` | 每次改行为后隔离缓存 |
| `torch/_inductor/codecache.py` | `PyCodeCache`、`CppCodeCache` | 为什么旧产物会绕过你的修改 |

阅读顺序：先看 `config.py` 里你关心的键，确认有没有官方出口；没有再看 `post_grad.py` 和 `scheduler.py` 的调用点；最后才看 `codegen/cpp.py`。不要从生成代码倒推所有行为，生成代码是结果，不是配置源。

上游测试的位置在 PyTorch 源码树的 `test/inductor/`，不在安装目录里。安装版 wheel 通常不包含完整测试；需要贡献时，克隆源码并按其 `CONTRIBUTING.md` 构建。本章不提供源码构建步骤的实测输出，避免把“未运行”写成“已通过”。

## 常见坑

1. **patch 只包住 `torch.compile`，没包住第一次调用。** 编译是惰性的，回调不会执行。把 `fn(x)` 放进 `with` 块。
2. **monkeypatch 后没有清缓存。** 旧图或旧 kernel 可能直接命中；实验里同时用 `torch._dynamo.reset()` 和 `fresh_inductor_cache()`，跨变体用子进程。
3. **monkeypatch 没有在 `finally` 恢复。** 一次异常就会把整个进程后续的编译行为改掉；用上下文管理器，不要裸赋值。
4. **把 `site-packages` 的 `.orig` 放在缓存清理范围内。** 清 `.cache` 时把唯一备份删掉；备份至少留两份，并记录 SHA256。
5. **升级 torch 后继续用旧结论。** 内部函数签名、pass 顺序、config 键都可能变；升级后重跑结构断言，不只看数值。
6. **改生成代码而不是改生成逻辑。** 改 `output_code.py` 只影响当前 cache 条目，下一次编译就没了；正确位置是 pass、lowering 或 codegen。
7. **在本机 AVX2 上设置 `cpp.simdlen=512` 期待 AVX512。** 本机只有 AVX2，512 会退回标量；这是配置与硬件不匹配，不是编译器 bug。
8. **把可选的激进优化默认打开。** 上游贡献里，行为变化需要开关、测试和说明；默认改变所有用户的结果，review 很难通过。
9. **用 `pip install --force-reinstall` 当回滚。** 它可能顺带换版本、覆盖其他本地改动；优先用 `.orig` 精确回滚，重装只作为最后手段。

## 练习：同一需求的三种姿势

目标：记录 `lab_c_lowering.py` 里 `affine` 的 lowering 调用，分别用 monkeypatch 和“改源码”两种方式实现，并比较风险。

要求：

1. **monkeypatch 版**：用本章的 `patched(L, "add", logging_add)`，在 lowering 时打印一次 `add` 调用；确认 `affine` 仍然融合成 1 个 kernel；
2. **源码版（可选，需回滚）**：在 `torch/_inductor/lowering.py` 的 `add` 定义里加一行 `print`，备份、修改、清缓存、运行 `lab_c_lowering.py`，然后按本章命令回滚；
3. **config 版对照**：说明为什么这个需求没有现成 config 键；如果你能找到等价开关，写出键名和证据；
4. 三种方式各写一句“生效范围、失效条件、回滚成本”；
5. 报告里附上 `git status` 之外的证据：修改前后的 SHA256、运行输出、回滚后的 SHA256。

<details>
<summary>参考结论与判读</summary>

- **config 版**：`post_grad_custom_pre_pass` 能改图，但不能拦截 lowering 函数本身；`trace.enabled` 只能给产物，不能打印每次 `add`。这个需求没有直接 config 键。
- **monkeypatch 版**：只在当前进程生效；如果 `_lower_affine` 通过 `L.add` 查找，拦截成功；如果某条路径在 import 时已经绑定函数，拦截可能漏掉。验证方式是看日志是否在 lowering 阶段出现，而不是看最终数值。
- **源码版**：所有进程生效，但升级即丢失、同事不可复现、缓存可能让旧 kernel 继续服务。它适合一次性确认“问题确实在这个函数”，不适合交付。
- **回滚判据**：回滚前后 SHA256 一致只是文件层证据；还要跑 `env_report.py` 和 `lab_c_lowering.py`，确认行为回到基线。文件对了、cache 没清，行为仍可能不对。

</details>

## 自我检查

- [ ] 我能说出 config、monkeypatch、`site-packages` 三种姿势的生效范围和回滚成本。
- [ ] 我知道 `torch.compile` 是惰性的，config patch 必须包住第一次调用。
- [ ] 我能写出一个带 `finally` 恢复的 monkeypatch 上下文管理器。
- [ ] 我知道 monkeypatch 后为什么要同时清 Dynamo 状态和 Inductor cache。
- [ ] 我能用 `find_source.py` 定位内部函数，并确认路径属于当前 venv。
- [ ] 我能完成 `site-packages` 文件的备份、SHA256 校验和回滚。
- [ ] 我知道清缓存前要解析并核对绝对路径，避免误删。
- [ ] 我能把一个本地 hack 拆成“复现、测试、最小补丁、风险说明”四部分，转成上游 PR。
- [ ] 我能解释 AOTInductor、Triton、MLIR 各自解决什么问题，以及本课哪些章节是它们的前置。
- [ ] 我不会把没有源码构建环境的测试命令写成“已实测”。

## 延伸阅读

- **PyTorch 文档，`torch.compile` 与 TorchInductor**：读编译流程、config 参考和调试入口；先把官方出口找全，再考虑改源码。
- **`torch/_inductor/config.py` 源码**：读 `patch` 的实现和配置分组；确认某个行为到底是 config、pass 还是 codegen 决策。
- **PyTorch 文档，AOTInductor**：读导出、打包、加载和动态 shape 的限制；配合本课第 18–20 章。
- **Triton 官方文档，`triton.jit` 与 block-level programming**：读 `tl.arange`、mask、`tl.sum`；对照本课第 15/16 章的向量化与并行。
- **MLIR / StableHLO 文档与 `compilers/mlir_core/README.md`**：读 dialect、pass pipeline、bufferization；理解另一条 lowering 路线的取舍。
- **PyTorch `CONTRIBUTING.md` 与 `test/inductor/`**：读测试组织、lint、PR 要求；本章的上游流程以它为准。
- **`compilers/mini_ai_compiler/README.md`**：读“自己造一个编译器”的路线；先用小实现理解概念，再回到 Inductor 读生产代码，成本更低。

---

[上一章：Ch 22 正确性与 fallback](./22-correctness-and-fallbacks.md) | [下一章：练习 D 性能扫掠](./practice-d-performance-sweep.md)