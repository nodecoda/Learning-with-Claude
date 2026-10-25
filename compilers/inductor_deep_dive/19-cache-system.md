# Ch 19 缓存体系：第二次编译为什么快，以及它什么时候骗你

> **目标**：能说出 Inductor 一次编译要跨过哪几层缓存、每层的 key 由什么组成、命中与失效分别看什么信号；能解释「代码看起来对、跑的是旧 kernel」的成因，并用进程隔离和独立 cache 目录得到可信结果。
> **环境**：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43。命令都在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

同一份脚本第一次运行要编译 C++ kernel，第二次通常在一秒内结束。这个差异就是本章的主题。

## 为什么

缓存同时服务于两个目标：**省时间** 和 **保证结果可复现**。这两个目标并不总是一致的。

- 省时间：一旦编译结果落盘，下一次相同输入可以直接复用，编译时间从十几秒降到接近零。
- 可复现：如果复用的那一份并不是你以为的那一份，时间省下来了，结论却是错的。

在实际工程里，缓存相关的故障通常表现为三种：

1. **改了东西没生效**：改了 lowering、改了 config、改了 pass，跑出来还是旧行为。
2. **benchmark 说谎**：几个变体的耗时几乎一样，因为它们其实复用了同一份产物。
3. **缓存目录无限膨胀**：每改一次代码就多一份 `.cpp`/`.pyd`，几个月后几十 GB。

这一章先画出缓存的分层，再解释每层 key 的来源，最后给一套「怎么确认这次是新编译」的操作方法。第 21 章的性能工程会直接建立在这些结论上。

## 主线

### 1. 缓存不是一层，是一条链

从 `torch.compile` 到真正执行，至少经过下面这些缓存点：

| 层 | 位置 / 载体 | 存什么 | 命中时省掉什么 |
| --- | --- | --- | --- |
| Dynamo 帧缓存 | 进程内存，按 code object + guards | 这段 Python 该用哪张图 | 重新 trace |
| AOTAutograd 缓存 | `cache/aotautograd/<hash>/<hash>` | 前向/反向图的编译结果 | 重新做 decompositions 与分图 |
| FxGraphCache | `cache/fxgraph/<xx>/<key>/<entry>` | 编译后的图产物描述 + guards 表达式 | GraphLowering、调度、codegen |
| PyCodeCache | `cache/<xx>/<hash>.py` + 进程内 `modules` | 生成的 Python 模块 | 重新写文件、重新 import |
| CppCodeCache | `cache/<xx>/<hash>.main.cpp` / `.main.pyd` | C++ 源码与共享库 | 调用 `cl.exe` |
| VecISA 探测 | `cache/<xx>/<hash>.pyd.load_ok` | 本机能否加载某个 ISA 的探测库 | 重复编译探测程序 |
| Autotune 缓存 | 由 `config.autotune_local_cache` 控制的本地缓存 | 某个配置下选出的 kernel 参数 | 重新跑候选 |

本机 `code/.cache` 里能看到这些子目录：

```text
code/.cache/
  fxgraph/            FxGraphCache，按 key 的前两位分片
  aotautograd/        AOTAutograd 的前后向产物
  locks/              编译锁，避免多进程同时写同一份产物
  iu/ciuth....py      PyCodeCache 生成的模块
  iu/ciuth....debug/  对应的调试产物
  6k/c6kc....main.cpp / .main.pyd
  perf/               lab_d 为每个变体单独建的 cache 根
  final/              final_project 为每个变体单独建的 cache 根
```

注意 `.py` 和 `.pyd` 的 hash 不是同一个：前者是 Python 源码的哈希，后者是「C++ 源码 + 编译命令行」的哈希。它们落在不同分片目录是正常的。

在这台 Windows 机器上还有一条特殊事实：`config.decide_compile_threads()` 在 `win32` 上直接返回 1。于是 `AsyncCompile.cpp_pybinding` 走的是同步分支：

```python
def cpp_pybinding(self, argtypes: list[str], source_code: str):
    if get_compile_threads() <= 1:
        return CppPythonBindingsCodeCache.load_pybinding(argtypes, source_code)
    ...
```

名字里的 async 在本机并没有带来并行编译。理解这一点，才不会把「编译时间没变短」误判成缓存失效。

### 2. key 由什么决定

不同层的 key 算法不同，但共同原则是：**凡是会影响产物的东西，都必须进入 key**。

**代码缓存（PyCodeCache / CppCodeCache）** 用的是内容哈希：

```python
def write(content, extension, extra="", hash_type="code", specified_dir="", key=None):
    if key is None:
        key = get_hash(content.strip(), extra, hash_type)
    basename, _subdir, path = get_path(key, extension, specified_dir)
    if not os.path.exists(path):
        write_atomic(path, content, make_dirs=True)
    return basename, path
```

`content.strip()` 意味着首尾空白的差异不会产生新文件。`extra` 是「内容之外还会影响结果的东西」：编译这条 C++ 时传的是 `f"{optimized_code} {main_cmd_line}"`，也就是分离编译的 kernel 源码加上完整的命令行。换了 `-O` 级别或 ISA 参数，`extra` 变，key 就变。

**FxGraphCache** 用的是「把图的相关信息序列化后取哈希」：

```python
def compiled_fx_graph_hash(gm, example_inputs, fx_kwargs, inputs_to_check):
    details = FxGraphHashDetails(gm, example_inputs, fx_kwargs, inputs_to_check)
    pickler = FxGraphCachePickler(gm, ...)
    key = pickler.get_key(details)
    return key, pickler.debug_lines(details)
```

`FxGraphHashDetails` 里包含图模块、输入元数据、`fx_kwargs`、`cache_key_tag`、嵌套的 inductor config patch，以及 `torch_version = torch_key()` 和 `system_info = CacheBase.get_system()`。换句话说，**换 torch 版本、换机器、换相关 config，key 都会变**。

`debug_lines()` 是排查 key 变化的好工具：它把每个字段单独哈希并打印。怀疑「为什么这次没命中」时，把两次的字段列表对一遍，差异通常一眼可见。

### 3. 一个 key 下可以有多个条目

FxGraphCache 的目录形状是：

```text
cache/fxgraph/<key[1:3]>/<key>/<serialized-metadata>
```

一个 key 对应一个目录，目录里可以有多份条目。源码注释写得很清楚：

> A given graph could have multiple compiled versions, corresponding to different sets of guards. Therefore, we store cache entries in the form: `<temp dir>/<fx graph hash>/<serialized metadata>`

lookup 时，`GuardedCache` 遍历目录里的每个条目，反序列化，然后在当前上下文里求值它的 guards 表达式：

- 求值成功：命中，并把这些 guards 加回当前上下文。
- 求值失败：继续看下一个条目。
- 全部失败：重新编译，再写一个新条目。

编译结束前，Inductor 会算出这份产物的 guards 表达式并存进去：

```python
guards = shape_env.get_pruned_guards(symints)
compiled_graph.guards_expr = shape_env.produce_guards_expression(
    placeholders=symints, guards=guards
)
```

这条机制把「缓存复用」和「动态 shape」连在了一起。第 20 章会看到：同一张图在不同 shape 下可能对应不同条目，能否复用全看 guards 是否满足。

### 4. 失效与隔离：三种不同粒度的「清缓存」

| 你想要的 | 用的工具 | 影响范围 |
| --- | --- | --- |
| 让 Dynamo 忘掉已 trace 的帧 | `torch._dynamo.reset()` | 进程内 Dynamo 状态，**不动文件系统缓存** |
| 只清内存里的代码缓存 | `torch._dynamo.reset_code_caches()` | 比 `reset()` 更窄，主要给测试用 |
| 换一个全新的 Inductor 缓存目录 | `torch._inductor.utils.fresh_inductor_cache()` | 进程内注册的 cache 全清 + 临时目录，退出时删除 |
| 只删 Python 模块文件和内存记录 | `PyCodeCache.cache_clear(purge=True)` | `.py` 文件与 `modules` 列表 |
| 只清 C++ 库的内存映射 | `CppCodeCache.cache_clear()` | 进程内 `cache` 字典 |
| 彻底隔离一次实验 | 子进程 + 独立 `TORCHINDUCTOR_CACHE_DIR` | 全部，且不影响父进程 |

`torch._dynamo.reset()` 的 docstring 值得逐字读：

> It does NOT affect any file system caches.

这句话解释了一个高频误解：以为 `reset()` 之后一定会重新编译。实际上它只清进程内的帧缓存；磁盘上的 FxGraphCache 和 `.pyd` 仍然在，下一次编译会命中它们。

`fresh_inductor_cache()` 是 `fresh_cache()` 的别名，行为是：

```python
@contextlib.contextmanager
def fresh_cache(cache_entries=None, dir=None, delete=True):
    clear_caches()
    inductor_cache_dir = normalize_path_separator(tempfile.mkdtemp(dir=dir))
    try:
        with _set_env("TORCHINDUCTOR_CACHE_DIR", inductor_cache_dir):
            with _set_env("TRITON_CACHE_DIR", os.path.join(inductor_cache_dir, "triton")):
                yield
                ...
        if delete:
            shutil.rmtree(inductor_cache_dir, ignore_errors=is_windows())
    finally:
        clear_caches()
```

三个后果：

- 进入时清一次注册过的缓存，退出时再清一次。
- `TORCHINDUCTOR_CACHE_DIR` 在 with 块内指向临时目录。
- 退出时删目录，**with 块外再读产物会找不到文件**。要保留产物，就在块内读出来或复制走。

`lab_c_lowering.py` 的 `compile_once` 正是这个用法：在 with 块内取 `modules_since(before)` 和源码，块外只使用已经读进内存的字符串。

### 5. 观测：这次到底是命中还是新编译

判断「跑的是不是刚生成的那份」，按可靠性从高到低：

1. **结构特征**：生成代码里的 `step`、`#pragma omp`、`extern_kernels.*`。这些是确定性的，不随机器抖动。
2. **模块快照**：`common.modules_snapshot()` 记数量，`common.modules_since(before)` 取新增模块，直接拿到本次编译写出的文件路径。
3. **缓存目录内容**：有没有新的 `<hash>.main.pyd`、`.debug` 目录。
4. **日志**：`TORCH_LOGS=inductor` 会打出 `fx graph cache hit for key ...` 这类信息；`+inductor` 是同一通道的 DEBUG 级别。
5. **耗时**：单次耗时只能作辅助证据。首次编译 5–15 秒与缓存命中不到 1 秒差距明显，但几秒级别的差异不足以判断。

本机可以这样组合使用：

```powershell
$env:TORCH_LOGS = "inductor"
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
Remove-Item Env:\TORCH_LOGS
```

日志里既有缓存命中信息，也有本次写出的文件路径。和 `modules_since()` 的返回值对照，就能确认两者指的是同一份产物。

### 6. 事故复盘：结构对、时间假

`lab_d_perf.py` 的注释记录了一个真实教训：

> 同一个进程里连续编译多个 config 变体时，Inductor/Dynamo 的缓存可能复用上一次的产物，导致「代码看起来是标量、跑起来仍是向量」。

复现路径可以还原成下面这样：

```python
# 反面写法：不要这样比较变体
for name, patches in VARIANTS.items():
    with cfg.patch(patches):
        fn = torch.compile(f)
        out = fn(x, y)      # 第二次以后可能复用上一次的产物
        bench(fn)
```

修法不是「记得清缓存」，而是**从结构上消除共享状态**：

```python
def run_child(name: str, tag: str) -> dict:
    env = dict(os.environ)
    cache = Path(os.environ["TORCHINDUCTOR_CACHE_DIR"]) / "perf" / tag
    if cache.exists():
        shutil.rmtree(cache)
    env["TORCHINDUCTOR_CACHE_DIR"] = str(cache)
    proc = subprocess.run([sys.executable, __file__, "--variant", name], env=env, ...)
    ...
```

每个变体一个新进程、一个新 cache 目录，`torch.set_num_threads` 的全局状态也不会串。`lab_d` 还额外跑了一次 `vec_again`，用两次 `vec` 的差异估计噪声。

至于「为什么同进程会复用」——具体落在哪一层 key 上，会随版本变化（Dynamo 帧缓存、AOTAutograd 缓存、FxGraphCache 的 key 组成都在演进）。工程上不需要赌它，隔离进程是成本最低的确定性方案。

## 可跑实验

### 实验 1：第一次与第二次的差别

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

脚本会打印 `first call: X.XXs`。第一次运行记录这个数；紧接着再跑一次同一条命令，对比第二次。缓存命中时，最小编译自检会明显变快。

注意 `env_report.py` 每次都新建进程，所以第二次命中靠的是磁盘缓存，不是进程内状态。

### 实验 2：数一数各层缓存

```powershell
$cache = "compilers\inductor_deep_dive\code\.cache"
"py   : " + (Get-ChildItem -LiteralPath $cache -Recurse -Filter *.py | Measure-Object).Count
"pyd  : " + (Get-ChildItem -LiteralPath $cache -Recurse -Filter *.pyd | Measure-Object).Count
"fxgraph entries: " + (Get-ChildItem -LiteralPath (Join-Path $cache "fxgraph") -Recurse -File | Measure-Object).Count
"debug dirs: " + (Get-ChildItem -LiteralPath $cache -Recurse -Directory -Filter *.debug | Measure-Object).Count
```

这些数字不是「编译次数」，而是「不同内容的数量」。同一段代码编译十次，仍然只有一个 `.pyd`。

### 实验 3：观察 key 的组成

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\find_source.py torch._inductor.codecache FxGraphHashDetails
```

把输出和 `FxGraphCachePickler.debug_lines()` 放在一起读：前者列出进入 key 的字段，后者说明每个字段如何单独哈希。

### 实验 4：进程隔离 benchmark

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_d_perf.py
```

这个实验要跑四个子进程、每个子进程独立编译，大约一分钟。它同时演示三件事：结构化对比、子进程隔离、重复测量估噪声。结果解读放在第 21 章。

### 实验 5：确认「清 Dynamo 不等于清磁盘」

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\env_report.py
```

连续跑两次，第二次依然很快。即便脚本内部调用 `torch._dynamo.reset()`（当前脚本没有调用），磁盘缓存仍在。想真正从零开始，要用 `fresh_inductor_cache()` 或换 `TORCHINDUCTOR_CACHE_DIR`。

## 源码落点

| 位置 | 读什么 | 为什么读 |
| --- | --- | --- |
| `torch/_inductor/codecache.py` 的 `write` / `get_path` / `get_hash` | 内容哈希与分片 | 解释 `.py` / `.cpp` / `.pyd` 命名 |
| `torch/_inductor/codecache.py` 的 `FxGraphCache` | 类 docstring、`prepare_key`、`load_with_key` | 一张图多条目、guards 校验 |
| `torch/_inductor/codecache.py` 的 `FxGraphHashDetails` / `FxGraphCachePickler` | `__init__`、`debug_lines` | key 的字段清单 |
| `torch/_inductor/codecache.py` 的 `GuardedCache` | `iterate_over_candidates`、`find_guarded_entry` | 多条目如何筛 |
| `torch/_inductor/codecache.py` 的 `PyCodeCache` | `modules`、`modules_no_attr`、`cache_clear(purge)` | 进程内模块复用 |
| `torch/_inductor/codecache.py` 的 `CppCodeCache.load_async` | `cls.cache[key]`、`FileLock`、`_load_library` | 编译锁与加载 |
| `torch/_inductor/utils.py` 的 `fresh_cache` | 临时目录、`clear_caches`、退出删除 | 隔离实验的标准写法 |
| `torch/_dynamo/__init__.py` 的 `reset` | docstring 与实现 | 为什么 `reset()` 不动磁盘 |
| `torch/_inductor/cpu_vec_isa.py` 的 `__bool__impl` | `load_ok_marker`、`_probe_load` | `.pyd.load_ok` 的来历 |

`CppCodeCache.load_async` 里有一个值得记住的细节：当 `key` 不在内存 `cache` 里时，它会先算 `locks/<key>.lock`。只有在 `submit_fn` 不为空（异步编译路径）时，才会拿这把锁并检查 `binary_path` 是否存在，把提交串行化；本机 `compile_threads == 1`，走同步分支，不会加这把锁。`locks/` 目录服务于并发编译场景，不能把它当成「本机一定不会重复编译」的保证。

## 常见坑

1. **以为 `torch._dynamo.reset()` 会让下一次必定重新编译。** docstring 明说不动文件系统缓存。磁盘上的 `fxgraph` 与 `.pyd` 仍在，下一次照样命中。要真正冷启动，用 `fresh_inductor_cache()` 或独立 cache 目录。

2. **在 `fresh_inductor_cache()` 块外读产物。** 退出时临时目录被删掉，`newest_debug_dir()` 会找不到东西。在块内把源码读成字符串，或者把需要的文件复制出来。

3. **把 `PyCodeCache.modules` 当字典用。** 本版本它是 `list`；`modules_no_attr` 才是按路径索引的字典。课程里的 `modules_snapshot()` / `modules_since()` 两种形态都兼容，自己写代码时别假设。

4. **用文件修改时间证明「这次是新编译」。** `write()` 只在文件不存在时写；命中时 mtime 保持旧值，而某些工具链操作会 touch 文件。可靠的证据是 `modules_since()` 的返回值和生成代码里的结构特征。

5. **多个实验共享 cache 目录又不隔离。** 不同 config、不同线程数、甚至不同编译器版本的产物可能混在一起。像 `lab_d`/`final_project` 那样给每个变体一个 `perf/<tag>` 或 `final/<variant>` 子目录。

6. **只看命中日志，不看产物内容。** 日志说命中，不代表命中是你期望的那一条（同一 key 下可能有多份带不同 guards 的条目）。用 `debug_lines()` 或生成代码的结构做二次确认。

7. **把 `async_compile` 当成必然并行。** 本机 `compile_threads == 1`，`cpp_pybinding` 是同步调用。编译时间没有随核数下降，属于预期行为。

8. **以为缓存目录可以随便删。** 删掉本身安全（最坏是重新编译），但如果此时有进程正在编译，锁文件与半成品可能留下。清理前先确保没有正在运行的编译任务。

## 自我检查

- [ ] 我能列出至少五层缓存，并说出每层存在哪、命中时省掉什么。
- [ ] 我能解释 `.py` 的 hash 与 `.main.pyd` 的 hash 为什么不同。
- [ ] 我能说出 FxGraphHashDetails 里至少四个进入 key 的字段。
- [ ] 我能解释同一个 key 下为什么会有多个条目，以及 guards 在 lookup 时的作用。
- [ ] 我能区分 `torch._dynamo.reset()`、`reset_code_caches()`、`fresh_inductor_cache()` 的影响范围。
- [ ] 我知道 `fresh_inductor_cache()` 退出时会删目录，并知道怎么在块内保存产物。
- [ ] 我能用 `modules_since()` 证明某份代码是本次编译生成的。
- [ ] 我能说明 `lab_d` 为什么要给每个变体单独起进程和单独 cache 目录。

## 延伸阅读

- **`torch/_inductor/codecache.py` 的 `FxGraphCache` docstring**：读哪里——strategy 列表与目录形状；学什么——多条目 + guards 的设计动机；关联——第 20 章的动态 shape。
- **`torch/_inductor/codecache.py` 的 `FxGraphCachePickler.debug_lines`**：读哪里——逐字段哈希与打印；学什么——如何定位 key 差异；关联——「为什么没命中」的排查。
- **PyTorch 官方 `torch.compiler` troubleshooting 文档**：读哪里——recompilation、cache 相关章节；学什么——官方推荐的观测方式；关联——本章的日志与计数。
- **`torch/_dynamo/__init__.py` 的 `reset` / `reset_code_caches`**：读哪里——docstring 与清理清单；学什么——内存状态与磁盘状态的边界；关联——常见坑 1。
- **`torch/_inductor/utils.py` 的 `fresh_cache`**：读哪里——临时目录、环境变量切换、退出删除；学什么——隔离实验的标准模板；关联——`lab_c` 与 `lab_d` 的写法。

[上一章：Ch 18 产物与 ABI](./18-artifacts-and-abi.md) | [下一章：Ch 20 动态 shape 与 guards](./20-dynamic-shapes-and-guards.md)
