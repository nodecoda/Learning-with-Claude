# Ch 0 环境与基线：让 TorchInductor 在本机跑起来

> 目标：把 Python、PyTorch、MSVC 三件事钉在同一个可复现的入口上，跑出本课程的第一份基线：一次 64×64 矩阵运算的编译产物，以及它落在磁盘上的四个文件。读完本章，你应当能回答：为什么必须走 `run.ps1` 而不是直接 `python`；生成物写到哪；这台机器的向量化能力边界在哪；以及“产物看起来对、行为却可疑”时先查哪一层。

> 环境：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe`）。所有命令在仓库根目录 `D:\Learning-with-Claude` 下用 PowerShell 执行。下文所有版本号与输出块都来自本机实跑，换机器后数值会变，但结构不变。

## 为什么

`torch.compile` 看起来像装饰器，实际是一条在**第一次调用**时才真正启动的流水线。它在 CPU 上会调用 C++ 编译器，把生成的 kernel 编成动态库再加载回来。于是“实验能不能跑”取决于三件与编译原理无关的事：解释器里装的是哪个 torch；进程环境里有没有 `cl.exe`；生成物被写到了哪个目录。

这三件事任一错位，你拿到的报错都不是算法层面的问题：可能是 `ModuleNotFoundError`，可能是 `cl.exe not found`，可能是编译成功但你打不开产物，也可能是产物打开了、数字却是上一层版本的。后面每一章都要读具体产物、对比具体数字，所以本章先把“同一台机器、同一个缓存目录、同一条命令”钉死。

本章的验收不是“跑通”，而是你能在没有讲义的情况下把下面这条命令再跑一遍，并解释输出里每一行的来源：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

## 主线

### 0.1 一个入口收拢三个进程级前提

`compilers/inductor_deep_dive/code/tools/run.ps1` 只做三件事，但这三件事恰好覆盖了最容易出错的三层：

| 层次 | 问题 | run.ps1 的处理 |
| --- | --- | --- |
| 工具链 | `cl.exe` 不在 PATH，Inductor 编译 C++ 时失败 | 执行 `vcvars64.bat`，把输出里的 `KEY=VALUE` 逐行导回当前进程 |
| 隔离 | 产物散落在用户目录，课后找不到 | `TORCHINDUCTOR_CACHE_DIR=code/.cache` |
| 导入 | `from labs import common` 找不到 | `PYTHONPATH=code`，并用课程 venv 的 `python.exe` |

它没有用 `cmd /c "vcvars64.bat && python ..."` 这种嵌套方式，原因很实际：在 PowerShell 里拼引号会变成转义地狱，而且子 shell 里的变量设不进当前进程。脚本采用的做法是先让 `cmd` 只负责“吐出环境”，再由 PowerShell 逐行 `Set-Item env:`：

```powershell
$envLines = & cmd /c "`"$vcvars`" >nul 2>&1 && set"
foreach ($line in $envLines) {
    if ($line -match '^([^=]+)=(.*)$') {
        Set-Item -Path ("env:" + $matches[1]) -Value $matches[2]
    }
}
if (-not (Get-Command cl.exe -ErrorAction SilentlyContinue)) {
    throw "cl.exe still missing after vcvars. Check your MSVC installation."
}
```

这段代码里最值得学的是最后的检查。导入环境变量可能“看起来成功了”，但 `cl.exe` 仍然不在 PATH 里（例如 vcvars 路径指到了别的架构）。加一条显式断言，错误就停在这一步，而不是等到十分钟后某个 C++ 编译报错里才浮现。

脚本还留了两个环境变量出口：`VCVARS` 指向别的 `vcvars64.bat`，`IND_PY` 指向别的 `python.exe`。它们的用途是换机器，而不是让每次实验都改脚本。

### 0.2 基线自检：env_report 打印了什么

`code/tools/env_report.py` 是本课程的第一份证据。它先打印环境，再做一次最小编译。本机实际输出：

```text
============================================================
Python    : 3.11.12 D:\Learning-with-Claude\.scratch\aic-env\Scripts\python.exe
OS        : Windows-10-10.0.19045-SP0
torch     : 2.14.1+cpu
inductor  : D:\Learning-with-Claude\.scratch\aic-env\Lib\site-packages\torch\_inductor
cl.exe    : D:\VStudio\2022\Enterprise\VC\Tools\MSVC\14.43.34808\bin\HostX64\x64\cl.EXE
threads   : 4
cache dir : D:\Learning-with-Claude\compilers\inductor_deep_dive\code\.cache
CPU ISA   : ['VecAVX2'] -> picked VecAVX2
============================================================
最小编译自检 ...
first call : 5.85s
max abs err: 2.38e-07
OK：本机可以运行本课程的实验。
```

七行环境信息各有用途，不是装饰：

- `Python` / `torch`：确认不是系统 Python、不是别的 torch 版本。`+cpu` 后缀说明这是 CPU-only 构建，后面所有 kernel 都是 C++ 而不是 CUDA。
- `inductor`：给出 `torch/_inductor` 的实际路径。第 4 章之后要频繁点进这个目录读源码，路径因机器而异，所以由脚本打印而不是写死在讲义里。
- `cl.exe`：给出编译器绝对路径。版本号 `14.43.34808` 决定了可用语言特性，也解释了为什么生成的代码里会见到某些写法。
- `threads`：OpenMP 并行度来自 `torch.get_num_threads()`。本机是 4，后面读 `#pragma omp parallel num_threads(4)` 时，这个 4 就是它。
- `CPU ISA`：`valid_vec_isa_list()` 返回 `['VecAVX2']`，`pick_vec_isa()` 选中的也是 `VecAVX2`。这行决定了第 15 章向量化的上限：本机没有 AVX512。
- `first call`：首次编译耗时。本机 5.85 秒，落在 5–15 秒这个区间。它不是性能指标，而是“你在等编译器”的证据。
- `max abs err`：编译结果与 eager 的最大绝对误差，本机 `2.38e-07`。容差判据是 `1e-5`，通过。

自检函数本身值得一读，因为它同时展示了三件事：如何定义一个小计算、如何只编译一次、如何用 eager 作为金标准：

```python
def f(x):
    return torch.relu(torch.sin(x) + 1.0) * 2.0

cf = torch.compile(f)
x = torch.randn(64, 64)
out = cf(x)
err = (out - f(x)).abs().max().item()
```

注意 `cf = torch.compile(f)` 这一步并不会编译。真正触发编译的是 `cf(x)`。这一点在第 0.5 节会以“常见坑”的形式再出现一次，因为它是本课程里最容易让人误判缓存与 config 的地方。

### 0.3 第一份基线产物：四份文件，四种表示

环境自检只告诉你“能编译”。真正的基线是 `lab_a_pipeline.py`：它让 Inductor 把中间产物写盘，然后逐段打印。运行后本机得到的产物目录与大小：

```text
D:\Learning-with-Claude\compilers\inductor_deep_dive\code\.cache\iu\ciuthffwhfhayqfvotrqims4knkmv4djwqtmdr6j553vjzor6b4d.debug
  fx_graph_readable.py       955 B
  ir_pre_fusion.txt         2842 B
  ir_post_fusion.txt        3591 B
  output_code.py            6226 B
```

目录名里的 `iu` 是两级哈希目录，后面那串是内容哈希。四份文件的字数差异本身就是信息：

| 文件 | 体量 | 它回答的问题 |
| --- | --- | --- |
| `fx_graph_readable.py` | 955 B | 进入 Inductor 的算子序列长什么样 |
| `ir_pre_fusion.txt` | 2842 B | 每个调度节点、每个循环体、每个 buffer 的布局 |
| `ir_post_fusion.txt` | 3591 B | 融合之后节点合并成了什么 |
| `output_code.py` | 6226 B | 最终交给 `cl.exe` 的 C++ 与 Python wrapper |

一个 5 行的 PyTorch 函数，展开成 6226 字节的产物。这些字节里绝大部分不是数学，而是调度信息：buffer 分配、循环边界、向量化宽度、并行 pragma、调用顺序。这正是“编译器”这门课要读的东西。

产物目录来自 `cfg.trace.enabled = True`：

```python
import torch._inductor.config as cfg
cfg.trace.enabled = True
```

这行必须写在**编译发生之前**。如果写在 `compiled(x, w, b)` 之后，本次编译不会留下 `.debug` 目录，你只会在缓存里看到 `<hash>.py` 和 `<hash>.cpp`。

### 0.4 缓存目录：为什么“代码对、行为怪”

所有实验共用 `code/.cache`。跑几次之后，它会呈现出稳定的结构：

```text
code/.cache/
  iu/ciuthff...debug/      trace 打开的产物目录（含四份文件）
  5e/c5esvy...cpp          C++ kernel 源码
  5e/c5esvy...pyd          编译出的动态库
  aotautograd/<hash>/...   AOTAutograd 的图缓存
  final/baseline/...       某个实验自己的子目录
```

这套结构带来一个真实的风险：**同进程、同 config 的编译会被复用**。如果上一次编译产出的 kernel 还在缓存里，你改了 Python 里的表达式但 config 没变，Inductor 完全有可能直接命中旧产物，于是“代码看起来对，跑的是旧 kernel”。

本课程的做法是两条：

1. 需要干净环境时用 `torch._inductor.utils.fresh_inductor_cache()`，它把缓存换到新目录，代价是重新编译。
2. 需要比较不同 config 变体时用**子进程隔离**。`lab_d_perf.py` 就是这么实现的：每个变体一个新进程，进程间不共享 `PyCodeCache` 与已加载的动态库。

第 19 章会把缓存键的构成拆开讲。现在你只要记住一个判据：如果两次运行的结构断言完全一致、耗时却差出一个数量级，先怀疑缓存，而不是怀疑自己的改写。

### 0.5 本机能力边界：AVX2、4 线程、simdlen 的位宽语义

本机 `valid_vec_isa_list()` 返回 `['VecAVX2']`，没有 AVX512。这个事实在后面会反复出现，因为 Inductor 生成代码时会按 ISA 决定向量宽度。

课程里会用到的三个 config 杠杆，以及本机的实测表现：

| 配置 | 生成代码的变化 | 结论 |
| --- | --- | --- |
| 默认 | `at::vec::Vectorized<float>`、`x0 += 8`、`#pragma omp parallel num_threads(4)` | 向量化 + 4 线程 |
| `cpp.vec_isa_ok = False` | 标量，`x0 += 1`，无 `Vectorized` | 关掉向量化 |
| `threads = 1` | 仍然向量化，但没有 omp pragma | 并行与向量化是两件事 |
| `cpp.simdlen = 512` | 退回标量 | 本机只有 AVX2，256 位 |
| `cpp.simdlen = 128` | 同样不可用 | 小于 ISA 宽度也不合法 |

其中 `cpp.simdlen` 最容易误读：它是**位宽**，不是元素个数。256 位对 `float32` 是 8 个元素，所以 `x0 += 8`；写成 8 会被理解成 8 位，直接不可用。第 15 章会从 `CppVecKernel` 的实现把这条语义钉死。

另一个容易被误读的数字是 `threads = 4`。它来自 `torch.get_num_threads()`，也来自 OpenMP 的默认设置。生成代码里的 4 是编译期常量，运行时不会再变；如果你在别的机器上看到 8，那不是“优化更激进”，只是这台机器核数不同。

## 可跑实验

第一步，环境自检。命令就是本章开头那条，指向 `env_report.py`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

本机输出（节选）：

```text
cl.exe    : D:\VStudio\2022\Enterprise\VC\Tools\MSVC\14.43.34808\bin\HostX64\x64\cl.EXE
threads   : 4
CPU ISA   : ['VecAVX2'] -> picked VecAVX2
first call : 5.85s
max abs err: 2.38e-07
OK：本机可以运行本课程的实验。
```

第二步，基线产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

本机输出（节选，保留原始中文）：

```text
[PASS] 数值与 eager 一致  (max_abs_err=4.768e-07 tol=1e-05)

1) 编译产物位置
D:\Learning-with-Claude\compilers\inductor_deep_dive\code\.cache\iu\ciuthff...debug
  fx_graph_readable.py       955 B
  ir_pre_fusion.txt         2842 B
  ir_post_fusion.txt        3591 B
  output_code.py            6226 B
```

拿到这四行之后，做三件小事：

1. 用 `Get-Content` 打开 `fx_graph_readable.py`，数一数里面有几种 `torch.ops.aten.*`。
2. 打开 `output_code.py`，搜 `#pragma omp parallel`，把 `num_threads` 后面的数字和 `env_report` 里的 `threads` 对上。
3. 打开 `ir_pre_fusion.txt`，确认里面有 `SchedulerNode` 字样。脚本的最后一段断言就在检查这件事。

第三步，确认 trace 开关的作用。把 `lab_a_pipeline.py` 里的 `cfg.trace.enabled = True` 注释掉再跑一次，观察结果：脚本不会报错，`common.newest_debug_dir()` 会返回**上一次**留下的那个 `.debug` 目录。

这一步值得亲手做，因为它演示了本课程最隐蔽的坑：目录存在、内容完整、断言全过，但它与你刚才那次编译无关。判断依据只有一个——目录的修改时间是不是本次运行产生的。把这一点记牢，后面比较 config 变体时才不会被旧产物骗。

把这三步的结果记成一张自查表，后面每章都用同一格式汇报：

| 检查项 | 从哪一行看 | 本机结果 |
| --- | --- | --- |
| 编译器可用 | `env_report.py` 的 `cl.exe` | 14.43.34808 |
| 并行度 | `env_report.py` 的 `threads` | 4 |
| ISA 上限 | `env_report.py` 的 `CPU ISA` | `VecAVX2` |
| 产物齐全 | `lab_a_pipeline.py` 的四行大小 | 955 / 2842 / 3591 / 6226 B |
| 数值一致 | `lab_a_pipeline.py` 的 `[PASS]` 行 | 4.768e-07 |

## 源码落点

- `compilers/inductor_deep_dive/code/tools/run.ps1`：读它做 vcvars 导入的那十行，理解为什么用 `cmd /c ... && set` 取环境而不是嵌套执行。
- `compilers/inductor_deep_dive/code/tools/env_report.py`：读 `main`，看环境打印、`shutil.which("cl")` 的显式失败、以及一次编译的计时方式。
- `compilers/inductor_deep_dive/code/labs/common.py`：读 `require_msvc`、`cache_dir`、`newest_debug_dir`、`read_text_any`。最后一个函数会在第 18 章之后一直用到，因为生成物在中文 Windows 上可能是 cp936。
- `torch/_inductor/cpu_vec_isa.py`：读 `valid_vec_isa_list` 与 `pick_vec_isa`，看 ISA 列表怎么来的、为什么本机只有 `VecAVX2`。
- `torch/_inductor/config.py`：搜 `trace`、`cpp.vec_isa_ok`、`cpp.simdlen`、`threads`，确认这些名字是 config 的字段而不是函数参数。
- `torch/_inductor/utils.py`：搜 `fresh_inductor_cache`，读它怎么切换缓存目录。

## 常见坑

1. **直接 `python code\labs\lab_a_pipeline.py`。** 报错通常是 `cl.exe` 找不到，或者 `ModuleNotFoundError: No module named 'labs'`。前者缺 MSVC 环境，后者缺 `PYTHONPATH=code`。两者都由 `run.ps1` 负责。
2. **把 `torch.compile(f)` 当成编译。** 它是惰性的，第一次调用才编译。所以“在 `torch.compile` 外面 patch config”是无效的，patch 必须包住第一次调用。
3. **trace 开关写晚了一步。** `cfg.trace.enabled = True` 必须在第一次调用之前。写在后面只会让下一次编译生效，而你已经在看上一次的缓存。
4. **拿旧产物当新结果。** 同进程连续编译不同 config 变体会复用产物；benchmark 必须子进程隔离。看到“改了代码但结构断言一模一样”，先清缓存。
5. **用错日志语法。** `TORCH_LOGS=fusion` 是 INFO 级，`TORCH_LOGS=+fusion` 是 DEBUG 级，两者都可用。Windows 上真正的坑是 `set TORCH_LOGS=fusion && ...`：尾随空格会进入变量，直接报 `Invalid log settings`。
6. **以为 `simdlen` 是元素个数。** 它是位宽：256 位在 `float32` 上等于 8 个元素。本机写 512 会退回标量，写 128 也不可用。
7. **用编辑器默认编码读产物。** 生成物里可能含 cp936 编码的中文注释，`Get-Content` 不加编码参数时会显示成乱码。课程代码统一用 `read_text_any` 兜底。

## 自我检查

- [ ] 我能不看讲义，用一条 `run.ps1` 命令跑出环境自检，并说出输出里每一行的含义。
- [ ] 我能解释为什么 `run.ps1` 要用 `cmd /c "... && set"` 再逐行导入，而不是嵌套执行 Python。
- [ ] 我知道 `first call` 的 5.85 秒花在哪里，以及为什么第二次运行会明显变快。
- [ ] 我能说出 `code/.cache` 下 `.debug` 目录是由哪个 config 字段产生的。
- [ ] 我知道本机的 ISA 列表是什么，以及它决定了生成代码里哪个数字。
- [ ] 我能举出至少两个“代码对、行为怪”的场景，并说出各自的排查顺序。

- [ ] 我能说清 955 / 2842 / 3591 / 6226 这四个字节数分别对应哪一层表示。
- [ ] 我知道关掉 trace 之后缓存里还剩哪些文件。

## 延伸阅读

- **PyTorch 官方 `torch.compile` 教程**：读 “Introduction to torch.compile” 一节，重点看图缓存与首次编译的时机；对应本章 0.5 节的惰性行为。
- **`torch/_inductor/cpu_vec_isa.py`**：读 `VecAVX2` 类与 `valid_vec_isa_list`，看 ISA 能力怎么被探测；对应第 15 章的向量宽度讨论。
- **`torch/_inductor/config.py`**：读 `trace`、`cpp`、`cpu_backend` 三组字段的注释，理解哪些开关会影响落盘产物；本章用到的四个开关都在这里。
- **MSVC `vcvars64.bat` 文档**：读 “Use the MSVC toolset from the command line”，理解环境变量导入的意义；对应 0.1 节的实现。
- **Python `pathlib` 文档**：读 `rglob` 与 `stat`，理解 `newest_debug_dir` 的排序依据；第 18 章读产物时会再用到。

[下一章：架构地图：一次 torch.compile 的六段旅程](./01-inductor-architecture-map.md)
