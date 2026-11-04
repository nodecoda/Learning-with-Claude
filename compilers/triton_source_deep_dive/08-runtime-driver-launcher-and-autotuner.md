# Ch 8 — 运行时：driver、launcher、CompiledKernel、autotuner

> `kernel[grid](...)` 只是把 grid 记住；真正的 launch 发生在一次 C 函数调用里。

## 目标

读完本章，你应该能回答：

- `kernel[grid](args)` 从 Python 到 `cuLaunchKernelEx` 经过哪几层？第一次调用和后续调用差在哪？
- grid 写成 tuple 和写成 callable 有什么区别？callable 收到的 `META` 里到底有什么？
- `num_warps` / `num_stages` 是怎么进入 metadata 的？launcher 真正用到哪几个字段？
- `CompiledKernel` 的 `module` / `function` / `run` 为什么是惰性的？`_init_handles` 检查了什么？
- autotuner 的 key 由什么组成？为什么改一个 dtype 就会重新 tune？`TRITON_PRINT_AUTOTUNING` 打印的是什么？
- launcher 为什么用 C 扩展而不是 `ctypes`？

本章是“编译产物 → 可 launch 的 CUDA function”这一段。IR 怎么生成属于 Ch 3–7，cache key 的完整细节属于 Ch 9。

## 环境/版本

**实测**：

```text
$ nvidia-smi --query-gpu=name,compute_cap,driver_version --format=csv
name, compute_cap, driver_version
Tesla T4, 7.5, 535.216.01

$ /sbin/ldconfig -p | grep 'libcuda.so.1'
	libcuda.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libcuda.so.1
	libcuda.so.1 (libc6) => /lib32/libcuda.so.1

$ which ptxas cuobjdump nvdisasm
/usr/local/cuda/bin/ptxas
/usr/local/cuda/bin/cuobjdump
/usr/local/cuda/bin/nvdisasm
```

| 项目 | 值 |
|---|---|
| Triton | `/home/ubuntu/aicomp/triton`，`3.9.0`，commit `a972119e92a27b445cd160017ea461e56148590c` |
| GPU | Tesla T4，`sm_75` |
| 驱动 / CUDA | 535.216.01 / 12.4（`V12.4.131`） |
| Python | 3.10.12 |
| `import triton` | `ModuleNotFoundError`（系统环境与 `.venv` 均如此） |

本章**没有真实 launch**。凡涉及运行结果、autotune 输出、`CompiledKernel` 生命周期的具体行为，标 `guided`；凡从源码读出的字段、顺序、检查，标 `源码依据`。

**源码依据**：`runtime/jit.py` 1223 行；`compiler/compiler.py` 522 行；`runtime/autotuner.py` 527 行；`third_party/nvidia/backend/driver.py` 419 行；`third_party/nvidia/backend/driver.c` 1633 行。

## 为什么需要这个

因为“kernel 跑得慢”有一半原因不在 kernel 里：

- 每次 launch 的 Python 开销可能比 kernel 本身还大（小 kernel 场景）。
- 第一次调用包含 JIT 编译 + `cuModuleLoadData` + `cuModuleGetFunction`，不能拿它当稳态性能。
- autotune 的 key 写错，会导致“每次都重新 tune”或“永远不重新 tune”。
- `num_ctas`、`maxnreg`、shared memory 这些参数在错误的架构上会被后端直接拒绝，或者 launch 时 `OutOfResources`。

这一层也是你定位“到底编译了没有”“到底 launch 了没有”“到底用了哪个 config”的唯一入口。

## 先建立直觉

```text
kernel[grid](x, y, out, M, BLOCK=1024, num_warps=4)
  │
  ├─ KernelInterface.__getitem__(grid)        # 只记住 grid，返回 lambda
  │
  └─ JITFunction.run(*args, grid=..., warmup=False, **kwargs)
       ├─ driver.active.get_current_device() / get_current_stream()
       ├─ binder(*args, **kwargs)             # 生成 specialization
       ├─ compute_cache_key(...)              # 内存 cache
       ├─ [miss] _pack_args → _do_compile → compile() → CompiledKernel
       ├─ grid = grid(bound_args)  (若 callable)
       └─ kernel.run(grid0, grid1, grid2, stream, kernel.function,
                     kernel.packed_metadata, ..., *bound_args.values())
            │
            └─ CudaLauncher.__call__           # Python 侧准备 scratch
                 └─ driver.c: launchKernel     # 打包参数
                      └─ _launch → cuLaunchKernelEx
```

第一次调用会走完整的 `compile()`：AST → TTIR → TTGIR → LLIR → PTX → cubin → cache。第二次调用命中 `kernel_cache`，只剩 binder + launcher。

`源码依据`：`jit.py:371-377`、`744-799`；`compiler.py:226-364`；`driver.py:306-353`；`driver.c:1448-1550`。

## 核心概念

### 1. `kernel[grid]` 不 launch，只是绑定 grid

```python
class KernelInterface:
    def __getitem__(self, grid):
        return lambda *args, **kwargs: self.run(grid=grid, warmup=False, *args, **kwargs)
```

`源码依据`：`jit.py:371-377`。

所以 `kernel[grid]` 是一个普通 lambda，真正的逻辑全在 `run`。这也意味着 `kernel[grid]` 本身没有副作用，可以存下来复用。

### 2. grid：tuple 或 callable

`run` 里（`787-794`）：

```python
if callable(grid):
    grid = grid(bound_args)
grid_size = len(grid)
grid_0 = grid[0]
grid_1 = grid[1] if grid_size > 1 else 1
grid_2 = grid[2] if grid_size > 2 else 1
```

`源码依据`：`jit.py:787-794`。

- tuple/list：直接取前三个元素，缺的补 1。
- callable：入参是 `bound_args`，即**按参数名索引的 kernel 实参字典**（含 `constexpr` 值）。它**不包含** `num_warps` / `num_stages`，这些在 `options` 里。
- 超过 3 维不会报错，多余元素被静默忽略。

典型写法：

```python
grid = lambda META: (triton.cdiv(M, META["BLOCK"]),)
kernel[grid](x, y, out, M, BLOCK=1024)
```

### 3. specialization → cache key

binder 由 `create_function_from_signature` 用 `exec` 生成（`jit.py:406-467`），它返回 `(params, specialization, options)`。`specialization` 是 `(类型字符串, 特化值)` 的列表，示意：

```text
[("*fp32", "D"), ("i32", ""), ("constexpr", 1024)]
```

指针的 key 是 `"D"` 或空串（`specialize.cc:387`），整数同理（`307`）；`constexpr` 参数直接把值放进第二个位置（`417-419`）。

`源码依据`：`jit.py:417-436`；`D` 的含义是“值能被 16 整除”（`python/src/specialize.cc:307`、`323`、`387`），值等于 1 的整数会被特化成 `constexpr`（`specialize.cc:298-300`）。

`compute_cache_key`（`jit.py:622-630`）把 specialization 和 options 拼成字符串 key。因此**改 `num_warps` 会换 key、改指针 dtype 会换 key、改 constexpr 值也会换 key**。

### 4. `CompiledKernel` 是编译产物的句柄

`CompiledKernel.__init__`（`compiler.py:407-449`）做四件事：

1. 从 `metadata_group` 里找 `.json`，反序列化成 `KernelMetadata`；
2. `backend.pack_metadata(metadata)` 得到 launcher 用的紧凑 tuple；
3. 把所有非 json 文件读进 `self.asm`（键是后缀，如 `source`、`ttir`、`ttgir`、`llir`、`ptx`、`cubin`）；
4. `self.kernel = self.asm[binary_ext]`，`module` / `function` / `_run` 全部置空，**惰性初始化**。

`源码依据`：`compiler.py:407-449`。

惰性的原因写在注释里：加载二进制要做运行时的事（检查 shared memory、`cuModuleLoadData`），不该在 cache hit 的构造路径上做。

### 5. launcher 是 C 扩展

`CudaUtils.__init__`（`driver.py:108-150`）在首次使用时编译 `driver.c`：

```python
mod = compile_module_from_file(
    src_path=os.path.join(dirname, "driver.c"),
    name="cuda_utils",
    library_dirs=library_dirs(),
    include_dirs=include_dirs,
    libraries=['libcuda.so.1'],
)
```

`源码依据`：`driver.py:108-121`；编译命令是 `cc -O3 -shared -fPIC -Wno-psabi -o <name>.so`（`runtime/build.py:77-88`）。

`library_dirs()` 依赖 `libcuda_dirs()`：先看 `TRITON_LIBCUDA_PATH`，否则用 `ldconfig -p` 找 `libcuda.so.1`（`driver.py:34-56`）。上面 **实测** 的 `ldconfig` 输出就是这条路径的来源。

### 6. `Autotuner` 也是 `KernelInterface`

`@triton.autotune` 返回一个 `Autotuner`（`autotuner.py:424-490`），它同样实现 `run` / `__getitem__` / `warmup`。所以 `kernel[grid](...)` 对 autotuned kernel 和普通 kernel 的写法完全一样，区别在 `run` 里多了一层“选 config”。

## 底层机制

### 1. `run()` 的九步

`JITFunction.run`（`jit.py:744-799`）按顺序：

1. 注入 `debug` / `instrumentation_mode` / `fpsan_homomorphic_casts` 到 kwargs（`745-747`）；
2. `get_current_device()` + `get_current_stream(device)`（`750-751`）；
3. 跑 `pre_run_hooks`（`753-755`）；
4. 取 `device_caches[device]`，调用 binder（`757-760`）；
5. `compute_cache_key`，查内存 cache（`768-769`）；
6. miss → `_pack_args` + `_do_compile`（`772-778`）；
7. 检查 `used_global_vals` 是否被改（`780-785`）；
8. `warmup` 为真则直接返回 kernel（`787`）；
9. 规范化 grid，`kernel.launch_metadata(...)`，`kernel.run(...)`（`787-798`）。

注意第 9 步里 `kernel.run` 是 `property`：

```python
@property
def run(self):
    if self._run is None:
        self._init_handles()
    return self._run
```

`源码依据`：`compiler.py:494-498`。

Python 先求值 `kernel.run` 再求值参数，所以 `kernel.function` 在传参时已经由 `_init_handles` 填好了。这个顺序不是巧合，去掉 property 就会拿到 `None`。

### 2. `_init_handles`：从 cubin 到 CUfunction

`compiler.py:452-493` 的关键步骤：

```python
device = driver.active.get_current_device()
self._run = driver.active.launcher_cls(self.src, self.metadata)
shared = getattr(self._run, "shared", self.metadata.shared)
max_shared = max_shared_mem(device)
if shared > max_shared:
    raise_(OutOfResources(shared, max_shared, "shared memory"))
...
self.module, self.function, self.n_regs, self.n_spills, self.n_max_threads = \
    driver.active.utils.load_binary(self.name, self.kernel, shared, device)
warp_size = self.metadata.warp_size
if self.metadata.num_warps * warp_size > self.n_max_threads:
    raise_(OutOfResources(self.metadata.num_warps * warp_size, self.n_max_threads, "threads"))
```

`源码依据`：`compiler.py:452-493`。

三件事值得记：

- **shared memory 先检查再加载**。`shared` 可能被 launcher 调大（`CudaLauncher.__init__` 的 `max_occupancy` 分支，`driver.py:274-289`），所以取 `getattr(self._run, "shared", ...)`。
- `load_binary` 返回 `(module, function, n_regs, n_spills, n_max_threads)`，`n_regs` / `n_spills` 来自 `cuFuncGetAttribute`。
- 线程数检查用的是 `num_warps * warp_size`，T4 的 `warp_size` 是 32，所以 `num_warps=4` 是 128 线程。

`tmem_size` 的检查只在 metadata 有该字段时执行（`475-481`），T4 上不会触发。

### 3. `driver.c`：参数打包与 `cuLaunchKernelEx`

`launchKernel`（`driver.c:1448-1550`）的流程：

```c
ensureCudaContext();
PyArg_ParseTuple(args, "iiiKKpp(iii)OOOOOOy*O", ...);
if (!launchHook(launch_enter_hook, launch_metadata)) goto cleanup;
extractArgs(args_data, &list_idx, kernel_args, arg_annotations);   // 跳过 constexpr、展开 tuple
int num_params = num_args + 2;                                     // + global/profile scratch
void **params = (void **)alloca(num_params * sizeof(void *));
for (i...) {
    Extractor extractor = getExtractor(extractor_data[i]);
    params[params_idx] = alloca(extractor.size);                   // 对齐版本略
    extractor.extract(params[params_idx++], args_data[i]);
}
params[params_idx++] = global_scratch;
params[params_idx++] = profile_scratch;
Py_BEGIN_ALLOW_THREADS;
_launch(gridX, gridY, gridZ, num_warps, num_ctas, ..., (CUstream)_stream, (CUfunction)_function, params);
Py_END_ALLOW_THREADS;
```

`源码依据`：`driver.c:1448-1550`。

`_launch`（`driver.c:999-1076`）把 grid/block 维度算出来：

```c
config.gridDimX = gridX * num_ctas;
config.gridDimY = gridY;
config.gridDimZ = gridZ;
config.blockDimX = 32 * num_warps;
config.blockDimY = 1;
config.blockDimZ = 1;
config.sharedMemBytes = shared_memory;
config.hStream = stream;
...
CUDA_CHECK(cuLaunchKernelExHandle(&config, function, params, 0));
```

`源码依据`：`driver.c:1000-1075`。

两个容易忽略的点：

- `gridDimX = gridX * num_ctas`：`num_ctas` 是 cluster 大小，launch 时把 cluster 摊进 x 维；`num_ctas != 1` 时还会加 `CU_LAUNCH_ATTRIBUTE_CLUSTER_DIMENSION`（`1050-1067`）。
- `blockDimX = 32 * num_warps` 是**硬编码 warp size 32**。AMD 后端有自己的 launcher，不走这里（Ch 30）。

### 4. metadata：`num_warps` / `num_stages` 怎么流

`compile()` 在 `python/triton/compiler/compiler.py:279-285` 初始化 metadata：

```python
metadata = {"hash": hash, "target": target, **options.__dict__, **env_vars}
```

`options` 是 `CUDAOptions`，其中 `num_warps=4`、`num_stages=3`、`num_ctas=1` 是默认值（`third_party/nvidia/backend/compiler.py:120-124`）。之后：

- `num_warps` / `num_stages` 被 pass 消费：`add_convert_to_ttgpuir(..., opt.num_warps, 32, opt.num_ctas)`、`add_pipeline(pm, opt.num_stages, ...)`（`third_party/nvidia/backend/compiler.py:315`、`338-352`）。
- warp specialization 可能改写 `num_warps`：从 `ttg.total-num-warps` 回写 metadata（`third_party/nvidia/backend/compiler.py:526-530`）。
- launcher 只关心 `pack_metadata` 的返回值：

```python
def pack_metadata(self, metadata):
    return (metadata.num_warps, metadata.num_ctas, metadata.shared)
```

`源码依据`：`third_party/nvidia/backend/compiler.py:265-270`；`CudaLauncher.__call__` 里 `num_warps, num_ctas, _ = kernel_metadata`（`driver.py:348`）。

**`num_stages` 不会传给 launcher**。它只影响编译期的流水线，运行时不可见。

### 5. autotune：bench、cache、print

`Autotuner.run`（`autotuner.py:232-297`）的核心：

```python
if len(self.configs) > 1:
    all_args = {**self.nargs, **kwargs}
    _args = {k: v for (k, v) in all_args.items() if k in self.arg_names}
    key = [_args[key] for key in self.keys if key in _args]
    for _, arg in _args.items():
        if hasattr(arg, "dtype"):
            key.append(str(arg.dtype))
    key = tuple(key)
    if key not in self.cache:
        pruned_configs = self.prune_configs(kwargs)
        ...benchmark...
```

`源码依据`：`autotuner.py:236-265`。

key 由两部分组成：

1. `self.keys` 里列出的参数值（按名字取）；
2. **所有带 `dtype` 属性的参数**的 dtype 字符串（顺序是 `_args` 的插入顺序，通常是参数声明顺序）。

所以 `key=["M", "N"]` 时，改 `M` 会重新 tune，改输入 tensor 的 dtype 也会重新 tune。这也解释了为什么“只改了一个 dtype，结果跑了很久”。

`_bench`（`148-188`）对每个 config 调 `self.fn.run(..., **config.all_kwargs())`，用 `do_bench` 取 `(0.5, 0.2, 0.8)` 分位；遇到 `OutOfResources` / `CompileTimeAssertionFailure` / `PTXASError` 返回 `[inf, inf, inf]`（`180-186`）。`do_bench` 默认来自 `driver.active.get_benchmarker()`，NVIDIA 后端返回 `triton.testing.do_bench`（`driver.py:405-407`）。

`Config.all_kwargs()`（`384-397`）把 `num_warps` / `num_ctas` / `num_stages` / `maxnreg` / `ir_override` 合并进 kwargs；`None` 值被跳过。

**`TRITON_PRINT_AUTOTUNING=1`** 对应 `knobs.autotuning.print`（`knobs.py:382-384`）。打印发生在两处：

- `_bench` 开头：`Autotuning kernel <name> with config <config>`（`148-152`）；
- `run` 结束：`Triton autotuning for function <name>, with key as <key>, finished after <t>s, best config selected: <config>`（`285-287`）。

`guided`：实际输出会包含具体 kernel 名、key 元组和 config 字段，本机未运行，不粘贴伪造输出。

**磁盘 cache**：`cache_results`（`TRITON_CACHE_AUTOTUNING=1`）时走 `check_disk_cache`（`190-230`），key 是 `triton_key() + backend.hash() + fn.cache_key + env_vars + tuning_key + configs` 的 sha256，文件是 `<name>.autotune.json`。`pre_hook` 存在时直接放弃磁盘 cache（`192-194`）。

## 进一步用法/重要变体

**`warmup`**：`KernelInterface.warmup`（`jit.py:365-366`）用 `MockTensor.wrap_dtype` 把参数换成 mock，`run(warmup=True)` 只编译不 launch。autotuner 的 `warmup`（`autotuner.py:330-341`）会对每个 pruned config 各 warmup 一次。

**`prune_configs_by`**：在 bench 之前缩小 config 集合。它是一个 dict，三个可选字段（`autotuner.py:99-105` 取值，默认 `perf_model=None`、`configs_top_k=1.0`、`early_config_prune=None`）：

- `early_config_prune(configs, nargs, **kwargs)`：拿到全部 config 和命名参数，返回子集。返回空列表会抛 `AutotunerError`（`autotuner.py:301-305`），因为后面的 `min()` 假定至少有一个 config。
- `perf_model(**nargs, **kwargs, **config.all_kwargs())`：返回该 config 的**预估**运行时间（不是实测值），只用于排序。
- `top_k`：只在有 `perf_model` 时生效。`int` 保留前 k 个；`float <= 1.0` 按比例保留，内部换算成 `max(1, int(len(pruned_configs) * top_k))`，避免小集合向下取整成 0（`autotuner.py:307-315`）；其他类型抛 `TypeError`。

`run` 里的顺序是：key miss → `prune_configs(kwargs)`（`autotuner.py:245`）→ 若只剩 1 个 config 就直接写 cache、**不 benchmark**（`247-250`）→ 否则 `_bench` 全部剩余 config。这给了一个低成本的加速手段：先用便宜的 `perf_model` 排序，再只 bench top-k，而不是把几十个 config 全跑一遍。

**stream**：默认 `driver.active.get_current_stream(device)`；也可以直接调 `CompiledKernel.__getitem__` 返回的 runner 并传 `stream=`（`compiler.py:511-521`），但那条路径要求 grid 是 3 元组。

**`launch_cooperative_grid` / `launch_pdl`**：分别对应 `CU_LAUNCH_ATTRIBUTE_COOPERATIVE` 和 programmatic stream serialization（`driver.c:1035-1048`）。需要硬件/驱动支持。

**`maxnreg`**：进入 `CUDAOptions.maxnreg`，最终变成 PTX 的 `.maxnreg`，launcher 不直接使用。

**`heuristics`**：`@triton.heuristics`（`autotuner.py:493-527`）是“无 benchmark 的 config 选择”，适合由形状直接推出 block size 的场景。

**`reset_to_zero` / `restore_value`**：autotune 前后重置/恢复 buffer（`autotuner.py:64-107`），benchmark 多个 config 时避免相互污染。

## 对比与取舍

| 选择 | 优点 | 代价 | 什么时候用 |
|---|---|---|---|
| tuple grid | 零开销，直观 | 形状变化要改 Python | 静态形状、单测 |
| callable grid | 随 meta 参数自动适配 | 每次 launch 多一次 Python 调用 | 生产 kernel、autotune |
| C 扩展 launcher | 参数打包快、可释放 GIL、支持 TMA/对齐 | 首次使用时编译 `driver.c` | 默认路径 |
| `ctypes` 调 `cuLaunchKernel` | 无需编译扩展 | 每个参数都要 Python 侧打包，无法按类型走编译期 extractor，`CUtensorMap` 的结构布局/对齐难处理 | 不推荐 |
| autotune | 自动找最优 config | 首次 tune 很慢，key 设计不当会反复 tune | 形状/硬件变化大 |
| heuristics | 无 benchmark 开销 | 依赖人工规则，可能不是最优 | 规则明确的 kernel |
| `cache_results` | 跨进程复用 tune 结果 | 依赖 cache key 稳定性，`pre_hook` 时失效 | 反复启动的推理服务 |

## 踩雷集锦

**1. T4 上 `num_ctas > 1` 直接报错。** `parse_options` 在 `capability < 90` 时抛：

```text
num_ctas > 1 requires NVIDIA SM90+ (Hopper). Current target is sm_75. ...
```

`源码依据`：`third_party/nvidia/backend/compiler.py:244-248`。`clc=True` 在 `capability < 100` 时同样被拒（`241-242`）。

**2. 给 callable grid 传 `META["num_warps"]`。** grid 的入参只有 kernel 参数（`jit.py:790`），`num_warps` 在 `options` 里，不在 `bound_args` 里。要按 `num_warps` 算 grid，只能在闭包里捕获，或改用 tuple grid。

**3. grid 超过 3 维被静默忽略。** `grid_size = len(grid)` 只用来判断有没有第 2/3 维，第 4 个元素永远不会被读（`jit.py:791-794`）。写 `(gx, gy, gz, extra)` 不会报错，但 `extra` 不生效。

**4. 传 `stream=` / `device=` 给 `kernel[grid](...)`。** `_pack_args` 里有断言：

```python
assert "stream" not in kwargs, "stream option is deprecated; current stream will be used"
```

`源码依据`：`jit.py:728-730`。同理 `device` / `device_type`。要用自定义 stream，走 `CompiledKernel.__getitem__` 的 runner。

**5. autotune 的 `key` 写了不存在的参数名。** 3.9 会在 `Autotuner.__init__` 直接抛 `ValueError`（`autotuner.py:52-57`），而不是静默忽略。这是好事：老版本的“永远只 tune 一次”问题在这里被堵住了。

**6. 编译后修改了 kernel 依赖的全局变量。** `run` 每次都会检查 `used_global_vals`：

```text
Global variable <name> has changed since we compiled this kernel, from <old> to <new>
```

`源码依据`：`jit.py:780-785`。`constexpr` 全局变量参与 cache key，普通全局变量不参与，所以只能运行时检查。

**7. 把第一次调用当稳态性能。** 第一次包含 `compile()`、`load_binary`、`cuModuleLoadData`。测性能前先 `warmup` 或至少跑一次；autotune 的 `do_bench` 自己会先 warmup（`triton/testing.py:288-339` 的 `do_bench`）。

**8. shared memory 超限。** `_init_handles` 在加载前检查 `shared > max_shared`，抛 `OutOfResources(shared, max_shared, "shared memory")`（`python/triton/compiler/compiler.py:472-474`）。T4 的 shared memory 上限比 A100/H100 小，`num_stages` 开太大很容易撞上。

## 进阶

- 读 `_pack_args`（`jit.py:720-742`）如何把 `constexpr` 和 `attrs` 分开：`find_paths_if(sigvals, lambda _, val: val == "constexpr")` 找出 constexpr 路径，`backend.parse_attr` 解析 `tl.constexpr` 之外的分支。
- 读 `driver.c:1391-1427` 的 `extractArgs`，理解 `ARG_CONSTEXPR` 为什么只 `arg_idx++` 而不写入 `params`。
- 读 `autotuner.py:190-230` 的磁盘 cache key 组成，试着解释“只改了 `num_stages` 默认值”为什么会 miss。
- 读 `python/triton/compiler/compiler.py:511-521` 的 `CompiledKernel.__getitem__`，对比 `JITFunction.run` 的 grid 规范化：前者要求 3 元组，后者允许 1–3 元组。

## 动手练习

1. **画 launch 时序**：给 `kernel[(4, 2, 1)](x, y, BLOCK=128, num_warps=4)` 画出从 `__getitem__` 到 `cuLaunchKernelEx` 的调用栈，标出每一步在哪个文件。**guided**。
2. **grid 三态**：分别用 tuple、callable、4 元组 grid 启动同一个 kernel，说明哪一个的第 4 维会被静默忽略、哪一个不会。**guided**。
3. **autotune key**：写一个 `key=["M"]` 的 autotuned kernel，列出“改 N”“改 dtype”“改 BLOCK 默认值”三种情况下哪些会触发重新 tune。**guided**。
4. **metadata 追踪**：用 `TRITON_KERNEL_DUMP=1` 编译一次，打开 `.json`，找出 `num_warps` / `num_stages` / `shared` 三个字段，说明它们分别被谁消费。**guided**。
5. **launcher 定位**：在 `driver.py:34-56` 的 `libcuda_dirs` 里加一条打印，确认本机解析到的 `libcuda.so.1` 路径与 `ldconfig` 输出一致。**guided**（需要可用的 triton 安装）。
6. **C 扩展证据**：读 `runtime/build.py:77-88`，写出 `driver.c` 被编译成什么文件、用了哪些 flag、缓存在哪里。**源码依据**。

## 本章重点

- `kernel[grid]` 只绑定 grid；`JITFunction.run` 负责 binder、cache、编译、launch。
- grid 可以是 tuple 或 callable；callable 只收到 kernel 参数，收不到 `num_warps`；超过 3 维被忽略。
- `CompiledKernel` 惰性加载二进制；`kernel.run` 是 property，第一次访问触发 `_init_handles`。
- `_init_handles` 检查 shared memory 和线程数，然后 `load_binary` 得到 `(module, function, n_regs, n_spills, n_max_threads)`。
- launcher 是 C 扩展：`extractArgs` 跳过 constexpr、展开 tuple，逐类型 `extractor` 写进栈上 `params`，释放 GIL 后 `cuLaunchKernelEx`。
- `gridDimX = gridX * num_ctas`，`blockDimX = 32 * num_warps`；`num_stages` 只影响编译期。
- autotune key = 指定参数值 + 所有带 dtype 参数的 dtype；磁盘 cache 另有一套 key。

## 自我检核

1. 为什么 `kernel.run` 必须是 property？如果改成普通方法，`JITFunction.run` 里的参数求值顺序会出什么问题？
2. `num_warps` 在 metadata、`pack_metadata`、launcher 三处分别以什么形式出现？
3. 为什么 `num_stages` 不需要传给 launcher？它最终影响的是哪一层？
4. autotune 的 key 为什么要追加所有带 `dtype` 的参数，而不只是 `key=` 里列的？
5. launcher 用 C 扩展而不是 `ctypes`，具体省掉了哪些 Python 层工作？
6. `gridDimX = gridX * num_ctas` 里的 `num_ctas` 在 sm_75 上为什么永远不会大于 1？

## 延伸阅读

1. **`python/triton/runtime/jit.py:744-799`**
   - 读哪里：`run` 全文，配合 `720-742` 的 `_pack_args`。
   - 学什么：binder → cache → compile → launch 的完整顺序，以及 grid 规范化。
   - 与本章关系：本章主线；所有其他组件都在这个函数里被串起来。

2. **`python/triton/compiler/compiler.py:407-521`**
   - 读哪里：`CompiledKernel.__init__`、`_init_handles`、`run`、`__getitem__`。
   - 学什么：编译产物如何变成可 launch 的句柄，惰性加载的边界在哪。
   - 与本章关系：连接“编译”和“launch”的中间层。

3. **`third_party/nvidia/backend/driver.py:264-353` 与 `driver.c:999-1076`、`1448-1550`**
   - 读哪里：`CudaLauncher.__init__` / `__call__`、`_launch`、`launchKernel`。
   - 学什么：参数打包、`alloca` 参数数组、`cuLaunchKernelEx` 属性、GIL 释放。
   - 与本章关系：解释“为什么 launch 这么快”，以及 T4 上哪些属性不可用。

4. **`python/triton/runtime/autotuner.py:148-297`**
   - 读哪里：`_bench`、`run`、`prune_configs`。
   - 学什么：key 的组成、config 的裁剪、benchmark 失败的处理。
   - 与本章关系：运行时最后一层“选哪个 config 编译/launch”。

## 下一章

launch 路径清楚了，但“为什么这次没重新编译”“dump 为什么是空的”“报错栈为什么指向 code_generator.py”还没回答。下一章进入 cache、knobs 和前端调试。

→ [Ch 9 缓存、knobs、错误定位与前端调试](./09-cache-knobs-and-frontend-debugging.md)
