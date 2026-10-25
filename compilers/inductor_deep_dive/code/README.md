# 实验代码：TorchInductor 深潜

这里的脚本都可以直接运行，和正文里的命令一一对应。全部实验只依赖本机环境：

- Python 3.11.12（`.scratch/aic-env`）
- torch 2.14.1+cpu
- MSVC 14.43（`cl.exe`，由 `vcvars64.bat` 导入）

## 统一入口

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

`run.ps1` 做三件事：

1. 执行 `vcvars64.bat` 并把变量导入当前进程（Inductor 在 Windows 上要用 `cl.exe` 编译 C++ kernel）；
2. 设置 `TORCHINDUCTOR_CACHE_DIR=code/.cache`，所有生成物集中在这里，方便课后翻；
3. 用课程 venv 运行脚本，并设置 `PYTHONPATH=code`。

可选参数 `-DebugArtifacts` 会额外设置 `TORCH_COMPILE_DEBUG=1`，把产物再复制到仓库根的 `torch_compile_debug/`。

## 目录

```text
code/
  tools/
    run.ps1        统一运行入口（MSVC + cache + venv）
    env_report.py  打印环境并做一次最小编译自检
    find_source.py 按符号名定位 torch/_inductor 源码
  labs/
    common.py      找产物、解析 kernel、计时、断言
    lab_a_pipeline.py   一次编译的 6 段产物（FX / 调度 IR / C++ / wrapper）
    lab_b_pass.py       自定义 post-grad pass：relu(x)+relu(-x) -> abs(x)
    lab_c_lowering.py   自定义算子：fallback vs register_lowering
    lab_d_perf.py       向量化/线程数：结构差异 + 子进程隔离 benchmark
    final_project.py    端到端改造 + 自动生成报告
```

## 运行顺序

```powershell
# 0. 环境自检
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py

# A. 一次编译的产物
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py

# B. 写一个 pass
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_b_pass.py

# C. 写一个 lowering
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py

# D. 性能对比（约 1 分钟）
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py

# Final
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\final_project.py
```

## 产物在哪

- `code/.cache/<xx>/<hash>.debug/`：`cfg.trace.enabled = True` 时生成，含
  `fx_graph_readable.py`、`ir_pre_fusion.txt`、`ir_post_fusion.txt`、`output_code.py`。
- `code/.cache/<xx>/<hash>.py`：`output_code.py`（没有开 trace 时）。
- `code/.cache/<xx>/<hash>.cpp` / `.pyd`：C++ kernel 源码与编译出的动态库。
- `code/.cache/final_report.md`：Final Project 生成的前后对比报告。

## 注意事项

- 首次编译 5–15 秒是正常的；之后会命中缓存。
- 同一进程里连续编译不同 config 变体可能复用旧产物，benchmark 必须像 `lab_d_perf.py` 那样子进程隔离。
- 生成的中间文件在中文 Windows 上可能是 cp936 编码，`common.read_text_any` 已做兜底。
- 本机 CPU 只支持 AVX2；`cpp.simdlen=512` 会退回标量，这是预期行为。