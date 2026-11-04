# Ch 9 — 缓存、knobs、错误定位与前端调试

> 编译器的“玄学”大多不是玄学：要么命中了旧缓存，要么开关没生效，要么错误被前端吞掉了。

## 目标

读完本章，你应该能：

- 说清楚 Triton 的 cache key 由哪些部分拼成，为什么改一行 Python 前端就会让磁盘 cache 失效；
- 看懂 `~/.triton/cache/<base32>/` 里的 `__grp__*.json`、`*.json` metadata 和各级 IR 文件；
- 用 `TRITON_KERNEL_DUMP`、`TRITON_DUMP_DIR`、`TRITON_ALWAYS_COMPILE` 稳定地拿到一次“真正编译”的产物；
- 用 `TRITON_INTERPRET=1` 在没有 IR、没有 cubin 的情况下跑 kernel；
- 用 `TRITON_FRONT_END_DEBUGGING=1` 把被 `CompilationError` 包起来的原始 Python traceback 放出来；
- 判断什么时候该清 cache，什么时候绝不能清。

本章把三条路径分开：

```text
cache 路径：key -> 目录 -> __grp__ -> metadata/IR/cubin
dump 路径：src.hash -> dump 目录 -> 每级 IR + sass
错误路径：Python 异常 -> CompilationError -> filter_traceback
```

三条路径的 key、生命周期和副作用都不同。混在一起，就会得到“我明明改了代码，为什么还是旧结果”。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 `3.9.0`。
- 机器：Tesla T4（`sm_75`），驱动 535.216.01，CUDA 12.4，系统 Python 3.10.12。
- 基础 Python 环境没有安装 `triton`/`torch`，不能直接用 3.9.0 源码跑 kernel。
- 本机另有 T4 smoke venv：`/home/ubuntu/flagai/builds/t4-smoke/venv`，里面是 **triton 3.6.0** + torch 2.6.0。本章标 `实测` 的运行输出来自这个 venv；标 `源码依据` 的行号来自 3.9.0 源码。遇到版本差异会明确写出。

**实测**：

```bash
$ /home/ubuntu/flagai/builds/t4-smoke/venv/bin/python -c "import triton, torch; print(triton.__version__, torch.__version__, torch.cuda.get_device_name(0))"
3.6.0 2.6.0+cu124 Tesla T4
```

**源码依据**：

- `python/triton/knobs.py:342-353` 定义 `cache_knobs`；
- `python/triton/knobs.py:356-372` 定义 `compilation_knobs`；
- `python/triton/knobs.py:470-492` 定义 `runtime_knobs`，其中 `interpret` 在 `471`；
- `python/triton/runtime/cache.py:36-129` 是 `FileCacheManager`；
- `python/triton/compiler/compiler.py:226-363` 是 `compile`。

**命名约定**：本章的 `compiler.py` 指 `python/triton/compiler/compiler.py`，`cache.py` 指 `python/triton/runtime/cache.py`，`knobs.py` 指 `python/triton/knobs.py`。

## 为什么需要这个

三个真实会浪费半天的问题：

1. **改了 kernel，结果没变。**
   `JITFunction` 在内存里还有 `kernel_cache`；磁盘上还有 `~/.triton/cache`。只改调用处的 Python 逻辑，不会改变 kernel 的 `cache_key`，第二次调用可能直接命中内存 cache，连 `compile` 都不进。

2. **开了 `TRITON_KERNEL_DUMP=1`，dump 目录还是空的。**
   `compile` 在 `compiler.py:267-279` 先查磁盘 cache；命中就直接返回 `CompiledKernel`，后面的 stage 循环和 dump 根本不执行。必须同时加 `TRITON_ALWAYS_COMPILE=1`。

3. **报错只显示一行 `CompilationError`，看不到真正出错的位置。**
   `filter_traceback` 默认会把 `code_generator.py` 和 `ast.py` 的栈帧删掉，只留下用户 kernel 的源码片段。想知道 `semantic.py` 里哪一行抛的，就要开 `TRITON_FRONT_END_DEBUGGING=1`。

共同点是：**Triton 有三套“记忆”**——进程内 kernel cache、磁盘编译 cache、dump 目录。它们各自的失效条件不同。

## 先建立直觉

把一次 `kernel[grid](...)` 想成下面这条流水线：

```text
JITFunction.run
  -> binder 算 signature / constexpr / specialization
  -> 内存 kernel_cache 查 key
  -> miss: ASTSource
  -> compile(src)
       -> get_cache_key(src, backend, options, env_vars)
       -> sha256 -> base32 -> ~/.triton/cache/<dir>
       -> get_group(<kernel>.json)
       -> hit: 直接构造 CompiledKernel，结束
       -> miss: make_ir -> ttir -> ttgir -> llir -> ptx -> cubin
               每个 stage 写 cache group，必要时写 dump
```

三个关键判断：

- **内存 cache 的 key** 是 `compute_cache_key(specialization, options)`，在 `python/triton/runtime/jit.py:622-631`。它关心参数特化、`constexpr`、`num_warps` 等。
- **磁盘 cache 的 key** 是 `get_cache_key(...)`，在 `python/triton/runtime/cache.py:321-323`。它额外关心 Triton 自身版本、前端源码、后端、编译选项、以及一组会改变代码生成的 env var。
- **dump 目录的 key** 是 `src.hash()`，在 `compiler.py:257-258`。它**不包含** `num_warps` 等 options，所以同一个 kernel 换 `num_warps` 会 dump 到同一个目录、覆盖同名文件。

记住这三句话，后面的“为什么没重新编译”“为什么 dump 被覆盖”都能自己解释。

## 核心概念

### 1. knobs 是 descriptor，不是全局变量

每个开关都是一个 descriptor：

```python
class cache_knobs(base_knobs):
    home_dir: env_str = env_str("TRITON_HOME", os.path.expanduser("~/"))
    dump_dir = env_str_callable_default("TRITON_DUMP_DIR", lambda: cache.get_triton_dir("dump"))
    dir = env_str_callable_default("TRITON_CACHE_DIR", lambda: cache.get_triton_dir("cache"))
```

**源码依据**：`python/triton/knobs.py:342-353`。

`env_base.__get__` 先看实例字典里的 Python 侧覆盖值，没有才读环境变量；`__set__` 同时写实例字典和 `os.environ`（`knobs.py:76-91`）。所以：

```python
triton.knobs.compilation.always_compile = True   # Python 侧覆盖，优先级更高
os.environ["TRITON_ALWAYS_COMPILE"] = "1"        # 只改环境变量
```

`env_bool.get()` 每次都读 `getenv_bool(self.key, self.default)`，没有缓存；`runtime.debug` 和 `compilation.instrumentation_mode` 是例外，在 `refresh_knobs()` 里主动缓存（`knobs.py:125-132`、`598-600`）。

`base_knobs.scope()` 会记录 descriptor 的 Python 值和环境变量，退出时恢复（`knobs.py:295-309`）：

```python
with triton.knobs.compilation.scope():
    triton.knobs.compilation.always_compile = True
    run_experiment()
```

### 2. 磁盘 cache 的目录名是 base32

`get_cache_manager(key)` 先 `_base32(key)`（`cache.py:255-257`、`260-262`）：

```python
def _base32(key):
    return base64.b32encode(bytes.fromhex(key)).decode("utf-8").rstrip("=")
```

SHA-256 是 32 字节，base32 后是 52 个 `A-Z2-7` 字符，不是 64 个十六进制字符。

**实测**：

```bash
$ d=$(find ~/.triton/cache -maxdepth 1 -mindepth 1 -type d | head -1)
$ basename "$d" | wc -c
52
$ basename "$d"
QVJ332QCW6TRPRBZICQSIGIZJ62A2DZTAIFCOFZZI5IDJXBLMV2Q
```

### 3. 一个 cache 条目里有三类文件

**实测**（3.6.0 的一次 vector-add）：

```text
cache/<52字符>/
  __grp__add_kernel.json
  add_kernel.source
  add_kernel.ttir / .ttgir / .llir / .ptx / .cubin
  add_kernel.json
```

- `*.source`：`ASTSource` 的 Python 源码（`compiler.py:316-317`）；
- 各级 IR/cubin：每个 stage 的产物（`compiler.py:326-339`）；
- `*.json`：metadata，含 `hash`、`target`、`num_warps`、`extern_libs`、`shared`（`compiler.py:281-288`、`353-356`）；
- `__grp__*.json`：group 清单，告诉 `CompiledKernel` 这个 kernel 由哪些文件组成。

### 4. dump 目录和 cache 目录的 key 不同

dump manager 用 `src.hash()`，不是完整 cache key（`compiler.py:252-258`）：

```python
fn_dump_manager = get_dump_manager(src.hash()) if enable_ir_dump else None
```

`ASTSource.hash()` 包含函数依赖、参数签名、`constexpr`，但**不包含** `num_warps`、`num_stages`、`enable_fp_fusion` 等后端 options（`compiler.py:71-76`）。所以改 `num_warps=8` 时 cache 目录会变，dump 目录不变；dump 文件名固定是 `<kernel>.<ext>`，后一次覆盖前一次。

## 底层机制

### 1. cache key 的五段拼接

```python
key = f"{triton_key()}-{src.hash()}-{backend.hash()}-{backend_options.hash()}-{str(sorted(env_vars.items()))}"
```

**源码依据**：`cache.py:321-323`。

| 段 | 来源 | 变了会怎样 |
|---|---|---|
| `triton_key()` | 版本 + `runtime/cache.py` + `compiler/` + `backends/` + `language/` 源码 hash + `libtriton.so` hash | 改任何前端/C++ 扩展都换目录 |
| `src.hash()` | `JITFunction.cache_key` + attrs + signature + constants | 改 kernel、参数类型、`constexpr` 换目录 |
| `backend.hash()` | `ptxas` 版本 + `target.arch` | 换 ptxas/架构换目录 |
| `backend_options.hash()` | `CUDAOptions` 所有字段 + extern lib 文件 hash | 改 `num_warps`、`num_stages`、libdevice 换目录 |
| `env_vars` | `CACHE_INVALIDATING_ENV_VARS` 的非空值 | 改 `TRITON_DISABLE_LINE_INFO` 等换目录 |

**源码依据**：`cache.py:285-318`、`compiler.py:71-76`、`third_party/nvidia/backend/compiler.py:176-180`、`674-677`、`include/triton/Tools/Sys/GetEnv.h:15-59`。

`triton_key()` 会遍历 `compiler/`、`backends/`、`language/` 逐文件算 SHA-256，再把 `libtriton.so` 分块读入；`@functools.lru_cache()` 保证同一进程只算一次（`cache.py:285-318`）。

### 2. cache hit 发生在哪里

```python
metadata_filename = f"{file_name}.json"
metadata_group = fn_cache_manager.get_group(metadata_filename) or {}
metadata_path = metadata_group.get(metadata_filename)
if not knobs.compilation.always_compile and metadata_path is not None:
    res = CompiledKernel(src, metadata_group, hash)
    ...
    return res
```

**源码依据**：`compiler.py:263-279`。

细节：

- `file_name = src.name[:150]`，避免文件名过长（`compiler.py:259-263`）；
- `get_group` 读 `__grp__<filename>`，不是直接读 `<filename>`（`cache.py:73-94`）；
- 3.9.0 的 `get_group` 会检查 `child_paths` 里每个文件是否存在，缺一个就返回 `None`（`cache.py:88-93`）；
- `TRITON_ALWAYS_COMPILE=1` 只跳过命中判断，不删旧文件，编译完成后覆盖同名文件。

### 3. `__grp__` 的格式在 3.9.0 变了

3.9.0 的 `put_group` 写的是**文件名列表**（`cache.py:97-103`）：

```python
grp_contents = json.dumps({"child_paths": sorted(group)})
```

注释说明：只存文件名，不存绝对路径，这样复制或移动 cache 目录仍然能用。

**实测（3.6.0）**：旧版的 `__grp__add_kernel.json` 值是绝对路径：

```json
{"child_paths": {"add_kernel.source": "/tmp/triton-ch9/cache/.../add_kernel.source", ...}}
```

所以把 3.6.0 的 cache 目录复制到别处，`__grp__` 仍指向原目录；原目录被删后，`get_group` 会认为文件缺失。3.9.0 改成存文件名后，复制/移动 cache 才是安全的。

### 4. 写文件是“临时目录 + 原子 replace”

```python
temp_dir = os.path.join(self.cache_dir, f"tmp.pid_{pid}_{rnd_id}")
os.makedirs(temp_dir, exist_ok=True)
temp_path = os.path.join(temp_dir, filename)
...
os.replace(temp_path, filepath)
os.removedirs(temp_dir)
```

**源码依据**：`cache.py:105-129`。

`os.replace` 在 POSIX 上是原子的，并发读者不会看到半个文件。`lock_path` 只在 `put` 里被 `assert self.lock_path is not None` 检查，并没有真正加锁；并发安全主要靠原子 rename（`cache.py:111`、`125-127`）。

### 5. dump 在 stage 循环里发生

```python
for ext, compile_ir in list(stages.items())[first_stage:]:
    next_module = compile_ir(module, metadata)
    ir_filename = f"{file_name}.{ext}"
    ...
    if fn_dump_manager is not None:
        fn_dump_manager.put(next_module, ir_filename)
        if ext == "cubin":
            fn_dump_manager.put(get_sass(next_module), file_name + ".sass")
```

**源码依据**：`compiler.py:326-344`。

所以 cache hit 不进循环、不 dump；`TRITON_ALWAYS_COMPILE=1` 进循环、dump；dump 目录里没有 `.source`，因为它在循环之前写（`compiler.py:315-317`）；`.sass` 只在 `ext == "cubin"` 时生成，依赖 `nvdisasm`（`compiler.py:342-344`）。

### 6. 前端错误是怎么被包装的

`CodeGenerator.visit` 捕获所有非 `CompilationError` 异常，包装成带源码片段的 `CompilationError`：

```python
except Exception as e:
    if knobs.compilation.front_end_debugging:
        raise
    raise CompilationError(self.jit_fn.src, self.cur_node, repr(e)) from None
```

**源码依据**：`code_generator.py:1677-1686`。

`CompilationError._format_message` 打印 `at <line>:<col>`、最多 12 行源码、以及 `^` 指向出错列（`errors.py:6-38`）。`compile` 在 `make_ir` 外层调用 `filter_traceback`（`compiler.py:306-310`）；后者默认删除 `/triton/compiler/code_generator.py` 和 `/ast.py` 的帧，让 traceback 只指向用户代码。`TRITON_FRONT_END_DEBUGGING=1` 时它直接 `return`，什么都不删（`compiler.py:149-184`）。

### 7. `TRITON_DISABLE_LINE_INFO=1` 改的是 location

Python 侧 builder 有 `lineInfoEnabled` 开关：

```cpp
bool lineInfoEnabled =
    !mlir::triton::tools::getBoolEnv("TRITON_DISABLE_LINE_INFO");
```

**源码依据**：`python/src/ir.h:104-108`。LLVM 侧的 debug info 也读同一个变量（`lib/Target/LLVMIR/LLVMDIScope.cpp:66-69`）。

**实测**：同一个 kernel，正常 dump 的 TTIR 有 50 处 `loc(`；加 `TRITON_DISABLE_LINE_INFO=1` 后只剩 27 处，`loc` 从 `loc("add_demo.py":6:0)` 退化成 `loc("x_ptr")`。它不是关闭优化，而是关闭源码位置信息；IR 更小、编译更快一点，但 profiler/调试器会失去行号。

## 进一步用法/重要变体

### 1. 一次性拿全 dump

**实测**（3.6.0，命令结构对 3.9.0 相同）：

```bash
TRITON_CACHE_DIR=/tmp/triton-ch9/cache \
TRITON_KERNEL_DUMP=1 TRITON_DUMP_DIR=/tmp/triton-ch9/dump \
TRITON_ALWAYS_COMPILE=1 \
/home/ubuntu/flagai/builds/t4-smoke/venv/bin/python add_demo.py
```

**实测输出**：`asm keys: ['cubin', 'llir', 'ptx', 'source', 'ttgir', 'ttir']`。

**实测 dump 目录**：

```text
dump/RGBQDJC3L5UUIVKSI2MHKZQOWUYCTJ7UYKEDIVXV5PLA4WHPOD2Q/
  add_kernel.ttir / .ttgir / .llir / .ptx / .cubin / .sass
```

`asm keys` 里没有 `sass`：`AsmDict.__missing__` 只有在访问 `k.asm["sass"]` 时才调用 `get_sass`，`keys()` 不触发（`compiler.py:390-400`、`423-431`）。

### 2. 验证 cache hit 不 dump

**实测**：复用同一个 cache，换空 `TRITON_DUMP_DIR=/tmp/triton-ch9/dump2` 再跑，输出仍是 `asm keys: [...]`，但 `find /tmp/triton-ch9/dump2 -type f | wc -l` 是 `0`。原因就是 `compiler.py:267-279` 的 cache hit 提前返回。

### 3. 改 `BLOCK` 会换 cache 目录

**实测**：`BLOCK=1024` 和 `BLOCK=512` 各跑一次，`find /tmp/triton-ch9/cache -name '__grp__add_kernel.json' -printf '%h\n'` 得到两个目录：

```text
.../6SX5QYDY7W6S65GRYDAAKYMNCT46RRW477NTPWZP2TX4F7EYP7RQ
.../GBCHH6DHQDNYBJ7BF477PAWRAWDZ62ESSHIBXHPD3IGNU2SZXZIA
```

因为 `BLOCK` 是 `constexpr`，进了 `ASTSource.hash()` 的 `constants_key`。两个 kernel 的 `hash` 前缀分别是 `f4afd86078fdbd2f` 和 `304473f86780db80`。

### 4. `TRITON_INTERPRET=1` 完全不产生 IR/cache

**实测**：

```bash
TRITON_INTERPRET=1 TRITON_CACHE_DIR=/tmp/triton-ch9/cache_interp python interp_demo.py
```

输出 `max err: 0.0`；`find /tmp/triton-ch9/cache_interp -maxdepth 1 -mindepth 1 -type d | wc -l` 是 `0`。

**源码依据**：`knobs.py:471`；`jit.py:1025-1030` 直接返回 `InterpretedFunction`；`interpreter.py:1516-1525` 逐 grid 调用 Python 函数。解释器用 NumPy/PyTorch 在 host 上逐元素执行 `tl.*`，适合验证语义/shape/mask，不适合看 layout/PTX/性能；warp、shared memory、barrier 没有真实对应。

### 5. 前端错误：开不开 `TRITON_FRONT_END_DEBUGGING`

**实测**：kernel 里写 `y = tl.load(x)`，其中 `x` 是 `tensor<128xf32>` 而不是指针。默认输出（截断）：

```text
triton.compiler.errors.CompilationError: at 4:8:
    y = tl.load(x)          # x is a tensor, not a pointer
        ^
Unsupported ptr type <['128'], fp32> in `tl.load`
```

加 `TRITON_FRONT_END_DEBUGGING=1` 后，完整 traceback 最后落到：

```text
File ".../triton/language/semantic.py", line 1055, in _load_legacy
    raise ValueError(f"Unsupported ptr type {ptr.type.__repr__()} in `tl.load`")
ValueError: Unsupported ptr type <['128'], fp32> in `tl.load`
```

**源码依据**：3.9.0 的错误文本来自 `semantic.py:1050-1051`；上面 traceback 的 `line 1055` 是 3.6.0 的行号，包装逻辑来自 `code_generator.py:1677-1686` 和 `compiler.py:149-184`。

### 6. 复现 cache 污染与安全清理

3.9.0 的 `get_group` 对损坏的 group 容错（`cache.py:78-83`），并要求 `child_paths` 里每个文件都存在（`cache.py:84-93`）。安全复现：

```bash
export TRITON_CACHE_DIR=/tmp/triton-cache-lab
python add_demo.py
d=$(find "$TRITON_CACHE_DIR" -name '__grp__add_kernel.json' -printf '%h\n' | head -1)
rm "$d/add_kernel.ttir"     # 只删临时实验目录里的 child
python add_demo.py           # get_group 发现缺失 -> None -> 完整重编译
```

**该清 cache**：`__grp__`/child 损坏、磁盘被旧实验占满、确定要复现冷启动。**不该清**：正在定位“为什么没重新编译”（先看 key）、性能回归对比、给别人报 bug（IR/PTX 是证据）、CI/共享 cache。更安全的替代是 `TRITON_CACHE_DIR=/tmp/triton-cache-lab` 隔离，或 `TRITON_ALWAYS_COMPILE=1` 强制重编译但保留旧文件。

### 7. `TRITON_HOME` 和远程 cache

`TRITON_HOME` 改 `.triton` 根目录（`knobs.py:343-353`）：

```python
home_dir = env_str("TRITON_HOME", os.path.expanduser("~/"))
def get_triton_dir(self, dirname):
    return os.path.join(self.home_dir, ".triton", dirname)
```

所以 `TRITON_HOME=/tmp/triton-home` 会让 cache/dump/override 都落到 `/tmp/triton-home/.triton/` 下。3.9.0 还支持远程 cache：`TRITON_CACHE_MANAGER`、`TRITON_REMOTE_CACHE_BACKEND`、`TRITON_REDIS_HOST/PORT`；`RemoteCacheManager` 仍用本地 `FileCacheManager` 物化文件（`knobs.py:349-350`、`cache.py:171-252`）。

## 对比与取舍

| 做法 | 优点 | 代价 | 适用 |
|---|---|---|---|
| 清 `~/.triton/cache` | 彻底、简单 | 丢证据、冷启动、影响共享环境 | 确认损坏/磁盘压力 |
| `TRITON_ALWAYS_COMPILE=1` | 只影响本次、保留旧文件 | 每次都编译，慢 | 调试 dump、验证 pass |
| `TRITON_CACHE_DIR=/tmp/...` | 完全隔离 | 需要自己管理目录 | 实验、复现、CI |
| `TRITON_INTERPRET=1` | 无需 GPU 编译、语义直观 | 没有 layout/PTX/性能 | 验证 shape/mask/数值 |
| `TRITON_KERNEL_DUMP=1` | 拿到每级 IR | 只读、不改变编译 | 对照 pass/IR |
| `TRITON_FRONT_END_DEBUGGING=1` | 完整 traceback | 输出很长、暴露内部帧 | 定位前端异常 |

实用组合：独立 cache + 完整 dump + 强制重编译 + 完整 traceback。

```bash
TRITON_CACHE_DIR=/tmp/triton-lab/cache \
TRITON_KERNEL_DUMP=1 TRITON_DUMP_DIR=/tmp/triton-lab/dump \
TRITON_ALWAYS_COMPILE=1 TRITON_FRONT_END_DEBUGGING=1 \
python run.py
```

缺点是慢、输出多，适合“一次性取证”，不适合日常。

## 踩雷集锦

1. **只开 `TRITON_KERNEL_DUMP=1`，dump 目录为空。** cache hit 在 `compiler.py:267-279` 提前返回；加 `TRITON_ALWAYS_COMPILE=1` 或换空 `TRITON_CACHE_DIR`。
2. **改了 `num_warps`，dump 文件被覆盖。** dump 用 `src.hash()`，不含 options（`compiler.py:252-258`）；cache 用完整 key，含 options（`cache.py:321-323`）。给每次实验换 `TRITON_DUMP_DIR`。
3. **把 `__grp__` 当成 metadata。** `__grp__*.json` 只是 child 清单；真正的 `num_warps`、`target`、`shared` 在 `<kernel>.json` 里，`CompiledKernel` 用它构造 metadata（`compiler.py:409-417`）。
4. **复制旧版 cache 目录后全部 miss。** 3.6.0 的 `__grp__` 存绝对路径；3.9.0 改成存文件名（`cache.py:100-103`）。跨版本/跨机器复制 cache 不可靠。
5. **以为 `TRITON_DISABLE_LINE_INFO=1` 会改变数值结果。** 它只关闭 location/debug info（`python/src/ir.h:107-108`、`LLVMDIScope.cpp:68-69`）。**实测** TTIR 的 `loc(` 从 50 降到 27，但 kernel 仍正确。
6. **在 `TRITON_INTERPRET=1` 下找 PTX。** 解释器根本不进 `compile`（`jit.py:1025-1030`），没有 TTIR/PTX/cubin；**实测** cache 目录数为 0。
7. **用 `TRITON_FRONT_END_DEBUGGING=1` 期待 cache 失效。** 它不在 `CACHE_INVALIDATING_ENV_VARS` 里（`GetEnv.h:15-59`），只影响错误包装。想强制重编译用 `TRITON_ALWAYS_COMPILE`。
8. **在 `~/.triton/cache` 上做破坏性实验。** 先设 `TRITON_CACHE_DIR`/`TRITON_HOME` 隔离；默认 cache 里可能有你之后还要对照的 `.ttir/.ptx`。

## 进阶

- 读 `triton_key()` 的 `pkgutil.walk_packages`（`cache.py:285-318`），数一数改一个 `language/` 文件会让多少 hash 变化。
- 给 `knobs.compilation.listener` 挂 `CompilationListener`，在 `cache_hit=True/False` 时打印 `CompileTimes`；`CompileTimer` 在 `compiler.py:187-223`。
- 用 `TRITON_KERNEL_OVERRIDE=1` + `TRITON_OVERRIDE_DIR` 替换某一级 IR，观察 verifier 在哪一层报错；override 的 key 是 `src.hash()`（`compiler.py:252-258`）。
- 读 `RemoteCacheManager.get_group`（`cache.py:215-243`），看它如何从远端拉 child 再用本地 `FileCacheManager` 物化。

## 动手练习

1. **画 key**：分别写出只改 `BLOCK`、只改 `num_warps`、只设 `TRITON_DISABLE_LINE_INFO=1` 时 cache key 的哪几段会变，并用 `TRITON_CACHE_DIR=/tmp/...` 实测目录数量。**guided（3.9.0）/ 可用 3.6.0 复现现象**。
2. **dump 对照**：同一个 kernel 以 `num_warps=4` 和 `num_warps=8` dump 到两个目录，比较 `.ttgir` 的 `ttg.num-warps` 和 `.ptx` 的 `.reqntid`。**guided**。
3. **cache hit 证据**：正常跑一次，再用空 dump 目录跑第二次，确认 dump 文件数为 0；加 `TRITON_ALWAYS_COMPILE=1` 后确认 dump 重新出现。**实测（3.6.0）**。
4. **前端错误**：写 `tl.load(x)`（`x` 是 tensor）的 kernel，分别在默认和 `TRITON_FRONT_END_DEBUGGING=1` 下运行，记录 traceback 帧数差异。**实测（3.6.0）**。
5. **解释器边界**：用 `TRITON_INTERPRET=1` 跑一个带 `tl.dot` 的小 kernel，确认数值正确且 cache 目录为空。**guided**。
6. **安全清理**：在 `/tmp/triton-cache-lab` 制造缺失 child 的 cache 条目，确认 3.9.0 会 miss 重编译，然后只删这个临时目录。**guided（3.9.0 源码行为）**。

## 本章重点

- knobs 是 descriptor：Python 侧覆盖值优先于环境变量，`scope()` 用于可恢复实验。
- 磁盘 cache 目录名是 SHA-256 的 base32（52 字符）；key 由 Triton 自身、源码、后端、options、env vars 五段拼成。
- cache hit 在 `compile` 早期返回，不会 dump、不会跑 stage；`TRITON_ALWAYS_COMPILE=1` 是调试 dump 的标配。
- dump 用 `src.hash()`，不含 options；换 `num_warps` 会覆盖同名 dump 文件。
- `__grp__` 在 3.9.0 存文件名，支持 cache 搬迁；旧版存绝对路径。
- 前端错误默认被 `CompilationError` + `filter_traceback` 收窄；`TRITON_FRONT_END_DEBUGGING=1` 放出完整 traceback。
- `TRITON_DISABLE_LINE_INFO=1` 只影响 location/debug info；`TRITON_INTERPRET=1` 绕过整个编译链。

## 自我检核

1. `TRITON_KERNEL_DUMP=1` 开了但 dump 为空，最可能的原因是什么？怎么用一条命令验证？
2. 为什么改 `num_warps` 会让 cache 目录变，但 dump 目录不变？
3. `__grp__*.json` 和 `<kernel>.json` 各自存什么？`CompiledKernel` 先读哪个？
4. `TRITON_FRONT_END_DEBUGGING=1` 会不会改变 cache key？为什么？
5. `TRITON_INTERPRET=1` 下 `kernel.asm` 还有内容吗？为什么？
6. 什么时候应该用 `TRITON_CACHE_DIR` 而不是 `rm -rf ~/.triton/cache`？

## 延伸阅读

1. **`python/triton/runtime/cache.py:255-323`**
   - **读哪里**：`_base32`、`get_cache_manager`、`triton_key`、`get_cache_key`。
   - **学什么**：cache key 的完整构造，以及为什么改 Python 前端会让磁盘 cache 失效。
   - **与本章关系**：这是“为什么没重新编译”的第一手答案。

2. **`python/triton/compiler/compiler.py:226-363`**
   - **读哪里**：`compile` 的 cache hit、stage 循环、dump、metadata 写回。
   - **学什么**：一次编译的完整生命周期，以及 dump 为什么只在 miss 时发生。
   - **与本章关系**：把 cache 路径和 dump 路径连起来，也是 Ch 3/Ch 16 的入口。

3. **`python/triton/knobs.py:270-309`、`342-372`、`470-492`**
   - **读哪里**：`base_knobs`、`cache_knobs`、`compilation_knobs`、`runtime_knobs`。
   - **学什么**：descriptor 如何读写环境变量，`scope()` 如何恢复现场。
   - **与本章关系**：解释“我设了环境变量为什么没生效”。

4. **`python/triton/compiler/code_generator.py:1664-1686`、`errors.py:6-38`**
   - **读哪里**：`visit` 的异常包装、`CompilationError` 的源码片段格式化。
   - **学什么**：前端错误如何变成用户看到的 `at line:col`。
   - **与本章关系**：`TRITON_FRONT_END_DEBUGGING` 的另一半。

5. **`include/triton/Tools/Sys/GetEnv.h:15-59`**
   - **读哪里**：`CACHE_INVALIDATING_ENV_VARS` 和 `CACHE_NEUTRAL_ENV_VARS`。
   - **学什么**：哪些环境变量会改变 cache key，哪些只是诊断开关。
   - **与本章关系**：避免“开了调试开关却期待重编译”。

## 下一章

下一章进入 TTIR 方言本身：`tt.ptr`、`tensor`、`tt.load/store/dot/reduce`、descriptor op 的 ODS 定义，以及 verifier 如何在 parser、trait、interface、region 四个层次上拦截错误。

→ [Ch 10 TTIR 方言：类型、op、属性与验证器](./10-ttir-dialect-and-verifiers.md)
