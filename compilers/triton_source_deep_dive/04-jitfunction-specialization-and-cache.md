# Ch 4 — @triton.jit、JITFunction 与特化

> 同一个 Python 函数之所以会编译出多份 kernel，不是 Triton 任性，而是特化在替你换性能。

## 目标

读完本章，你应该能回答：

- `@triton.jit` 到底返回了什么？`@jit` 与 `@jit(do_not_specialize=[...])` 有什么区别？
- `JITFunction.run` 从拿到参数到 launch，中间经过哪些对象？
- binder 为什么要用 `exec` 生成一个 Python 函数？它返回的三样东西是什么？
- `n=1`、`n=16`、`n=1000` 为什么会得到不同的 kernel？
- cache key 到底由哪几段拼成？改 `num_warps` 会不会让 TTIR 变化？
- `TRITON_ALWAYS_COMPILE=1` 为什么有时“看起来没生效”？

本章的主线是 `python/triton/runtime/jit.py` 的 `JITFunction`，以及 3.9 新增的 `python/src/specialize.cc`。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5 |
| 驱动 / CUDA | 535.216.01 / 12.4（`V12.4.131`） |
| 系统 Python | 3.10.12 |
| `import triton` | `ModuleNotFoundError` |

**实测**：本机没有可导入的 `triton`，仓库内 `.venv` 也没装。因此本章所有 kernel 行为、cache key 和 TTIR 差异都是 **guided**：按源码推导“会发生什么”，不伪造运行输出。

**源码依据**：3.9 把 tensor specialization 从 Python 移到了 C++（`python/src/specialize.cc`）；`jit.py` 只保留 `native_specialize_impl` 的调用点和 binder 生成逻辑。

## 为什么需要这个

先看一个反直觉的事实：

```python
k[grid](p, 1,    BLOCK=1024)
k[grid](p, 16,   BLOCK=1024)
k[grid](p, 1000, BLOCK=1024)
```

同一个 Python 函数，三次调用，可能编译出**三份**不同的 kernel：

- `n=1` 被当成编译期常量，直接从 IR 签名里消失；
- `n=16` 是 `i32`，但带 `tt.divisibility = 16`；
- `n=1000` 是 `i32`，没有对齐属性。

这不是 bug，是特化（specialization）。它让编译器知道“这个整数一定是 1”“这个指针一定 16 字节对齐”，从而省掉运行时判断、启用向量化。代价是：**每多一种特化组合，就多一份 cubin、多一次编译、多一份 cache**。

不理解这套规则会遇到三类问题：**cache 爆炸**（autotune 扫 20 个配置，每个又因 `n=1` 多一份）、**性能诡异**（换个 tensor 就慢 30%，因为对齐属性没了）、**调试困惑**（没改代码却重编，或改了代码还跑旧 kernel）。

本章就是把“参数 → 特化 → cache key → 编译”这条链拆开。

## 先建立直觉

`@triton.jit` 是一个装饰器工厂（`jit.py:991-1047`）：

```python
def decorator(fn):
    if knobs.runtime.interpret:
        return InterpretedFunction(fn, ...)
    return JITFunction(fn, version=..., do_not_specialize=..., ...)
```

所以：

| 写法 | 结果 |
|---|---|
| `@triton.jit` | `JITFunction(fn)` |
| `@triton.jit(do_not_specialize=["n"])` | `JITFunction(fn, do_not_specialize=["n"])` |
| `TRITON_INTERPRET=1` + `@triton.jit` | `InterpretedFunction(fn)`，走 Python 解释执行 |

源码依据：`jit.py:1025-1041`。

`JITFunction` 的生命周期可以粗分为四段：

```text
定义期：JITCallable.__init__        # 取源码、签名、文件位置
  -> JITFunction.__init__           # 建 params、device_caches
调用期：kernel[grid](args)          # KernelInterface.__getitem__
  -> run(...)                       # binder -> cache key -> 编译或命中
编译期：_do_compile -> compile      # ASTSource -> 五个 stage
启动期：kernel.run(...)             # _init_handles -> load_binary -> launch
```

`JITFunction.__init__`（`804-843`）里几个字段值得记住：

```python
self.params = [KernelParam(i, param, dns, dns_oa) for ...]
self.device_caches = defaultdict(self.create_binder)   # 每个 device 一套
self.arg_names = [p.name for p in self.params]
self.constexprs = [p.num for p in self.params if p.is_constexpr]
self.pre_run_hooks = []
```

`device_caches` 是 `defaultdict`，value 是 `(kernel_cache, kernel_key_cache, target, backend, binder)`（`create_binder` 返回，`707-718`）。第一次在某个 device 上调用时，才创建 backend 和 binder。

## 核心概念

### KernelParam：注解的字符串化

`KernelParam`（`306-353`）把 `inspect.Parameter` 包一层，三个关键属性都是 `cached_property`：`annotation = _normalize_ty(...)`、`is_constexpr = "constexpr" in annotation`、`is_const = not is_constexpr and ("const" in annotation or annotation.startswith("*k"))`。源码依据：`jit.py:320-345`。

`_normalize_ty`（`280-303`）把 `tl.constexpr`、`"tl.constexpr"`、`*k f32` 等写法统一成字符串。`annotation_type`（`326-335`）先去掉指针星号，检查基类型是否是 `i32`/`fp32` 这类规范类型：是则返回原注解，否则返回空串；binder 用它决定是否跳过运行期特化。

### binder：用 exec 生成参数打包函数

`create_binder`（`707-718`）做三件事：

```python
target = driver.active.get_current_target()
backend = make_backend(target)
binder = create_function_from_signature(self.signature, self.params, backend)
return {}, {}, target, backend, binder      # kernel_cache, key_cache, target, backend, binder
```

`create_function_from_signature`（`406-474`）拼出一段 Python 源码再 `exec`：

```python
def dynamic_func(x_ptr, y_ptr, n, BLOCK, **options):
    params = {'x_ptr': x_ptr, 'y_ptr': y_ptr, 'n': n, 'BLOCK': BLOCK}
    specialization = [("specialize_impl(...)", ...), ...]
    return params, specialization, options
```

源码依据：`jit.py:451-456`、`470-474`。

为什么用 `exec`？因为 `sig.bind` + `apply_defaults` 在每次 launch 都要跑，是热路径。生成一个原生 Python 函数后，参数绑定、默认值填充、`**options` 收集都由 CPython 直接完成，比通用反射快得多。注释里写得很直白：“much of the kernel launch overhead”（`407-412`）。

`specialization` 的每个元素是 `(类型字符串, 特化值)`：

| 参数类型 | 生成的表达式 |
|---|---|
| constexpr | `("constexpr", BLOCK)` |
| 普通参数 | `specialize_impl(backend, n, is_const, specialize, align)` |
| 带 `annotation_type` 且不需要值特化 | `("i32", None)` |
| 带 `annotation_type` 且需要值特化 | `("i32",) + ret[1:]` |

源码依据：`jit.py:417-436`。注意 `u1`、`fp*`、`bf*` 会被强制 `specialize=False`（`427-429`）：**浮点和 bool 不做值特化**。

### `_pack_args`：把特化结果拆成四样东西

`_pack_args`（`720-742`）接收 binder 的输出，返回 `(options, signature, constexprs, attrs)`：

```python
options = backend.parse_options(kwargs)
sigvals = [x[0] for x in specialization]
signature = {p.name: v for p, v in zip(self.params, sigvals)}
constexprs = find_paths_if(sigvals, lambda _, val: val == "constexpr")
constexprs = {path: get_iterable_path(list(bound_args.values()), path) for path in constexprs}
attrvals = ['' if x[0] == 'constexpr' else x[1] for x in specialization]
attrs = {k: backend.parse_attr(get_iterable_path(attrvals, k))
         for k in find_paths_if(attrvals, lambda _, x: isinstance(x, str))}
```

四个产物：

- **options**：`CUDAOptions`，包含 `num_warps`、`num_stages`、`num_ctas`、`arch` 等；
- **signature**：`{参数名: 类型字符串}`，constexpr 参数的值就是 `"constexpr"`；
- **constexprs**：`{路径: 值}`，只包含 constexpr 参数；
- **attrs**：`{路径: [["tt.divisibility", 16]]}`，来自特化字符串里的 `"D"`。

源码依据：`jit.py:720-742`、`backends/compiler.py:76-81`。`parse_attr` 只处理包含 `"D"` 的描述，映射成 `tt.divisibility = 16`。

### 特化规则：C++ 侧到底看什么

`specialize.cc` 的入口是 `specialize_impl`（`648-687`），接收 5 个参数：`backend, arg, is_const, specialize_value, align`。然后按 `type(arg)` 分派到 handler（`543-582`）。

**整数**（`handle_long_type`，`288-330`）：

```cpp
if (specialize_value && (val == 1)) return {constexpr_str, arg};   // n == 1 -> constexpr
type_str = (val >= INT32_MIN && val <= INT32_MAX) ? i32_str : i64_str;
key_obj = (align && ((val & 15) == 0)) ? D_str : empty_str;        // 16 字节对齐 -> "D"
```

所以整数有四种结果：

| 值 | 类型 | 特化 key |
|---|---|---|
| `1` | `constexpr` | 值本身 |
| 其他 32 位整数，16 的倍数 | `i32` | `"D"` |
| 其他 32 位整数 | `i32` | `""` |
| 超出 32 位（最高到 64 位） | `i64`；超过 64 位为 `u64` | `"D"` 或 `""` |

**浮点**（`handle_float_type`，`408-412`）永远返回 `("fp32", None)`；**bool**（`401-406`）永远返回 `("u1", None)`。两者都不做值特化。

**tensor**（`handle_tensor`，`332-399`）：类型字符串来自 dtype，特化 key 来自 `data_ptr() & 15`：

```cpp
auto key_obj = (align && ((data_ptr & 15) == 0)) ? D_str : empty_str;
```

源码依据：`specialize.cc:387-388`。如果后端不支持 native tensor specialization，就退回 `backend.get_tensor_specialization(...)`（`389-396`）。

**constexpr 对象**（`426-430`）：直接返回 `("constexpr", arg)`。**JITCallable**（`432-439`）：返回 `("constexpr", cache_key)`，所以被调用 helper 的源码变化会让外层 key 变化。

**tuple / namedtuple**（`477-539`）：每个字段独立特化；namedtuple 可以通过 `__triton_do_not_specialize__` 指定某些字段跳过（`441-475`）。

### cache key：两套 key，别混

| 层级 | 组成 | 代码 |
|---|---|---|
| 进程内 | `str(specialization) + str(options)` | `compute_cache_key`（`622-631`） |
| 磁盘 | `triton_key + src.hash() + backend.hash() + options.hash() + env_vars` | `get_cache_key`（`runtime/cache.py:321-323`） |

`compute_cache_key` 有一个 memo：

```python
key = (tuple(specialization), str(options))
cache_key = kernel_key_cache.get(key, None)
if cache_key is not None:
    return cache_key
cache_key = str(_replace_jit_callables(specialization)) + str(options)
```

源码依据：`jit.py:622-631`。`_replace_jit_callables` 把 specialization 里的 `JITCallable` 换成它的 `cache_key`，这样 helper 源码变了，外层 key 也会变。

磁盘 key 的 `src.hash()` 来自 `ASTSource.hash`（`compiler.py:71-76`）：

```python
sorted_sig = [v for k, v in sorted(self.signature.items())]
constants_key = '-'.join([get_key(v) for k, v in sorted(self.constants.items())])
key = f"{self.fn.cache_key}-{self.attrs}-{sorted_sig}-{constants_key}"
```

注意它包含 **signature、constants、attrs**，所以 `n=1` 与 `n=16` 的磁盘 key 也不同。

`options.hash()` 来自 `CUDAOptions.hash`（`third_party/nvidia/backend/compiler.py:176-180`），把 dataclass 所有字段拼起来，`extern_libs` 还按文件内容 hash。

### `run`：一次调用的完整路径

`run`（`744-799`）：

```python
device = driver.active.get_current_device()
kernel_cache, kernel_key_cache, target, backend, binder = self.device_caches[device]
bound_args, specialization, options = binder(*args, **kwargs)
key = compute_cache_key(kernel_key_cache, specialization, options)
kernel = kernel_cache.get(key, None)
if kernel is None:
    options, signature, constexprs, attrs = self._pack_args(backend, kwargs, bound_args, specialization, options)
    kernel = self._do_compile(key, signature, device, constexprs, options, attrs, warmup)
kernel.run(grid_0, grid_1, grid_2, stream, ...)
```

注意 `options` 在 `compute_cache_key` 时还是**原始 kwargs dict**，到 `_pack_args` 才 `backend.parse_options`。所以进程内 key 的 `str(options)` 是 kwargs 的字符串表示，`num_warps=8` 和 `num_warps=4` 自然不同。

### `_do_compile`：真正进入编译

`_do_compile`（`910-944`）：

```python
if self._call_hook(knobs.runtime.jit_cache_hook, ...): return None
src = self.ASTSource(self, signature, constexprs, attrs)
kernel = self.compile(src, target=target, options=options.__dict__)
kernel_cache[key] = kernel
```

`ASTSource` 的构造在 `compiler.py:52-69`：它把 `constexprs` 的 key 从参数名转成 `(arg_index,)` 路径，然后 `ast_to_ttir` 用这些路径覆盖 `arg_types`（`code_generator.py:1756-1769`）。

## 底层机制

### 例子 1：`n=1` / `n=16` / `n=1000`

```python
@triton.jit
def k(p, n, BLOCK: tl.constexpr):
    offs = tl.arange(0, BLOCK)
    mask = offs < n
    v = tl.load(p + offs, mask=mask)
    tl.store(p + offs, v, mask=mask)

k[(1,)](p, 1,    BLOCK=1024)
k[(1,)](p, 16,   BLOCK=1024)
k[(1,)](p, 1000, BLOCK=1024)
```

**guided**：三次调用的 specialization 大致是：

```text
n=1    -> ("constexpr", 1)
n=16   -> ("i32", "D")
n=1000 -> ("i32", "")
```

对应的 TTIR 差异：

```mlir
// n=1：n 从签名消失，mask 直接和常量比较
%mask = arith.cmpi slt, %offsets, %c1 : tensor<1024xi1>   // %c1 = arith.constant dense<1> : tensor<1024xi32>

// n=16 / n=1000：n 是参数，前者带 divisibility，后者不带
%n = tt.splat %n_elements : i32 -> tensor<1024xi32>
%mask = arith.cmpi slt, %offsets, %n : tensor<1024xi1>
// n=16 签名:   %n_elements: i32 {tt.divisibility = 16 : i32}
// n=1000 签名: %n_elements: i32
```

同一个 Python 函数，三份 cubin。这就是“为什么会编译出多份 kernel”的最直接答案。

**收益**：`n=1` 时 mask 的右操作数变成常量，便于常量折叠；`n=16` 时知道 `n` 对齐，可以做更宽的向量化。
**陷阱**：如果 `n` 是动态 batch size，用户偶尔传 1，你就多一份冷门 kernel，而且它可能长期占着 cache。

### 例子 2：`do_not_specialize`

```python
@triton.jit(do_not_specialize=["n"])
def k(p, n, BLOCK: tl.constexpr):
    ...
```

`JITFunction.__init__` 把 `n` 标记为 `do_not_specialize`（`821-823`），binder 生成时 `specialize = 'False'`（`422`）。于是 `specialize_impl` 的 `specialize_value=False`：

- `handle_long_type` 不再把 `1` 变成 constexpr（`298` 的条件不成立）；
- 不再计算 `"D"`（`306-308`）；
- 返回 `("i32", None)`。

**guided**：三次调用现在都得到 `("i32", None)`，TTIR 里 `n` 始终是运行期参数，且没有 `tt.divisibility`。代价是失去对齐优化，收益是**只有一份 kernel**。

### 例子 3：`num_warps` / `num_stages` 改变 key，但不改变 TTIR

```python
k[(1,)](p, 1024, BLOCK=1024, num_warps=4)
k[(1,)](p, 1024, BLOCK=1024, num_warps=8)
```

- 进程内 key：`str(options)` 不同（`{'num_warps': 4}` vs `{'num_warps': 8}`），两份 kernel；
- 磁盘 key：`CUDAOptions.hash()` 不同，两份 cubin；
- **TTIR 完全相同**：`num_warps` 不是 TTIR 的概念，它第一次出现在 TTGIR 的 `ttg.num-warps` 属性里（`make_ttgir` 的 `add_convert_to_ttgpuir`，`third_party/nvidia/backend/compiler.py:315`）。

**guided**：TTGIR 里能看到差异：

```mlir
// num_warps=4
module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32}
// num_warps=8
module attributes {"ttg.num-warps" = 8 : i32, "ttg.num-ctas" = 1 : i32}
```

`num_stages` 同理：它进入 options 和 key，但在 T4（sm_75）上不会触发 TTGIR 的 pipeline pass（Ch 3 已解释）。**key 变了，IR 未必变**——这是 cache 观察里最常见的误判。

### 例子 4：`constexpr` 参数改变形状

```python
k[(1,)](p, 1024, BLOCK=1024)
k[(1,)](p, 1024, BLOCK=2048)
```

`BLOCK` 是 constexpr，直接进入 `specialization` 的 `("constexpr", 1024)` / `("constexpr", 2048)`，也进入 `ASTSource.constants`。于是：

- `tensor<1024xi32>` vs `tensor<2048xi32>`；
- `tt.make_range` 的 `end` 不同；
- `metadata.shared`、寄存器压力、occupancy 都可能不同。

**guided**：两份 TTIR 的 `make_range`：

```mlir
%r = tt.make_range {start = 0 : i32, end = 1024 : i32} : tensor<1024xi32>
%r = tt.make_range {start = 0 : i32, end = 2048 : i32} : tensor<2048xi32>
```

这不是“同一 kernel 的两个配置”，而是**两个不同的 kernel**：形状是类型的一部分。

### 例子 5：`TRITON_ALWAYS_COMPILE`

`knobs.compilation.always_compile` 只在 `compile` 里读取（`compiler.py:267-268`）：

```python
always_compile = knobs.compilation.always_compile
if not always_compile and metadata_path is not None:
    return CompiledKernel(...)   # 缓存命中
```

它绕过的是**磁盘缓存**。`JITFunction.run` 的 `kernel_cache`（`jit.py:768-769`）不受影响。所以：

- 新进程 + `TRITON_ALWAYS_COMPILE=1` → 每个 kernel 都重跑 stage；
- 同进程内重复调用 → 仍然命中 `kernel_cache`，不会重编。

## 进一步用法/重要变体

- **`do_not_specialize_on_alignment`**：只关闭对齐特化，保留 `n=1` 的常量特化。binder 里 `align='False'`（`jit.py:423`），`handle_long_type` 的 `D` 判断被跳过，但 `val == 1` 仍然生效。
- **namedtuple 字段级控制**：`handle_tuple`（`477-539`）会对每个字段独立特化；在 namedtuple 类型上设 `__triton_do_not_specialize__ = ("field",)` 可跳过指定字段（`441-475`）。
- **`const` 注解**：`KernelParam.is_const` 认 `const` 或 `*k` 前缀（`342-345`），它影响指针类型的 const 属性，不参与值特化。

## 对比与取舍

### 特化的收益与代价

| 维度 | 开启特化 | `do_not_specialize` |
|---|---|---|
| kernel 数量 | 每种值/对齐一份 | 一种 |
| 编译时间 | 随组合增长 | 固定 |
| 对齐优化 | 有 `tt.divisibility=16` | 无 |
| `n=1` 常量折叠 | 有 | 无 |

### 三种“key”不要混

| key | 变化来源 | 影响 |
|---|---|---|
| `specialization` | 参数值/类型/对齐 | 进程内 kernel 选择 |
| `ASTSource.hash()` | 源码、signature、constants、attrs | 磁盘缓存目录 |
| `CUDAOptions.hash()` | `num_warps`/`num_stages`/`arch`/... | 磁盘缓存目录 |

一个常见误判是“我改了 `num_warps`，TTIR 怎么没变？”——因为 `num_warps` 不在 TTIR 里，它在 TTGIR 才出现。

### constexpr vs 普通参数

| 维度 | constexpr 参数 | 普通参数 |
|---|---|---|
| 进入签名 | 否 | 是 |
| 可否做 shape | 能 | 不能 |
| 改变是否重编 | 是 | 否（但可能触发特化） |

## 踩雷集锦

### 1. 传了 `n=1`，结果多出一份 kernel

`handle_long_type` 把 `val == 1` 直接当 constexpr（`specialize.cc:298-300`）。如果 `n` 是动态 batch size，某个请求恰好传 1，就会编译一份“n 烘死”的 kernel。

**纠正**：对动态 size 用 `@triton.jit(do_not_specialize=["n"])`；或者确保调用侧不会传 1（不现实，所以前者更稳）。

### 2. 以为 constexpr 参数会出现在签名里

constexpr 参数的 `_flatten_ir_types` 是空实现（`core.py:214-215`），它不会产生 IR 参数。所以 `k.asm["ttir"]` 里根本找不到 `BLOCK`。

**纠正**：把 constexpr 参数理解成“编译期宏”，不是“运行时入参”。调试签名问题时先看 `k.metadata` 的 `signature` 或 TTIR 的 `tt.func` 参数表。

### 3. 同时标 `constexpr` 和 `do_not_specialize`

```python
@triton.jit(do_not_specialize=["BLOCK"])
def k(p, BLOCK: tl.constexpr): ...
```

`visit_arg` 直接报错：`BLOCK marked as constexpr and listed in do_not_specialize/do_not_specialize_on_alignment`（`code_generator.py:713-717`）。

**纠正**：两者互斥。constexpr 本来就是“完全特化”，没有“不要特化”的语义。

### 4. 指针没对齐，性能突然掉一截

`handle_tensor` 只在 `data_ptr() & 15 == 0` 时给 `"D"`（`specialize.cc:387-388`）。切片、`narrow`、非对齐 allocator 都可能让指针不再 16 字节对齐，于是同一 kernel 编译出第二份、且失去向量化。

**纠正**：尽量让 tensor 从 16 字节对齐的基址开始；调试时打印 `t.data_ptr() % 16`，并对比两份 TTIR 的 `tt.divisibility`。

## 进阶

- 读 `_replace_jit_callables`（`jit.py:609-619`）。它递归把 list/tuple/namedtuple 里的 `JITCallable` 换成 `cache_key`，这是“helper 源码变化会让外层失效”的实现。
- 读 `serialize_specialization_data`（`jit.py:381-403`）与 `preload`（`845-908`）。它们把一次特化序列化成 JSON，用于跨进程预编译；注意 `constexpr`、`JITFunction`、`type` 都有专门的编码分支。

## 动手练习

1. **数 kernel**：对同一个 kernel 依次传 `n=1`、`n=16`、`n=1000`，打印 `id(k)` 或 cache 目录数量，验证“三份 kernel”的结论。
2. **关特化**：给 `n` 加 `do_not_specialize`，重复练习 1，确认只剩一份，并对比 TTIR 里 `n` 的签名属性。
3. **改 warps**：同一组参数分别用 `num_warps=4` 和 `8`，对比 `.ttir` 与 `.ttgir`，解释哪一层开始出现差异。
4. **制造对齐差异**：用 `t[1:]` 造一个非 16 字节对齐的指针，对比 TTIR 的 `tt.divisibility`。
## 本章重点

- `@triton.jit` 返回 `JITFunction`；`TRITON_INTERPRET=1` 时返回 `InterpretedFunction`。
- binder 用 `exec` 生成参数打包函数，返回 `params`、`specialization`、`options`。
- 特化规则在 C++：整数 `1` → constexpr，16 的倍数 → `"D"`；浮点/bool 不做值特化；tensor 看 `data_ptr() & 15`。
- `_pack_args` 把特化拆成 `options`、`signature`、`constexprs`、`attrs`，`attrs` 最终变成 `tt.divisibility`。
- 两套 key：进程内看 `specialization + options`，磁盘看 `src.hash + backend.hash + options.hash + env_vars`。
- `num_warps`/`num_stages` 改变 key，但不改变 TTIR；差异从 TTGIR 开始。
- `TRITON_ALWAYS_COMPILE=1` 只绕过磁盘缓存，不绕过进程内 `kernel_cache`。

## 自我检核

- 你能说出 `@jit` 与 `@jit(do_not_specialize=[...])` 在 `JITFunction.__init__` 里的差异吗？
- 你能解释 `n=1` 为什么会让参数从签名里消失吗？
- 你能列出整数特化的四种结果，并说出各自的 key 吗？
- 你能解释 `num_warps` 改变后，为什么 TTIR 不变而 TTGIR 变吗？
- 你能说出进程内 key 与磁盘 key 的组成差异吗？

## 延伸阅读

1. **`python/triton/runtime/jit.py:646-843`**
   - 读哪里：`JITFunction` 类主体、`__init__`、`create_binder`、`_pack_args`、`run`。
   - 学什么：参数如何变成 specialization，kernel_cache 如何工作；这是本章的主干。

2. **`python/triton/runtime/jit.py:406-474`**
   - 读哪里：`create_function_from_signature`。
   - 学什么：binder 生成的源码长什么样，constexpr/annotation_type 如何影响 specialization；对应本章“binder”与“特化规则”两节。

3. **`python/src/specialize.cc:288-539`**
   - 读哪里：`handle_long_type`、`handle_tensor`、`handle_constexpr_type`、`handle_jit_callable`、`handle_tuple`。
   - 学什么：3.9 的特化规则全在 C++；重点看 `val == 1`、`& 15` 和 `specialize_value` 的分支，它们直接决定本章的 cache key。

## 下一章

下一章进入 `CodeGenerator` 的内部：AST 节点如何被翻译成 TTIR op，`visit_*` 如何与语义层协作，以及一个表达式从 dunder 到 `ir.builder.create_*` 的完整路径。

→ [Ch 5 从 AST 到 TTIR：CodeGenerator](./05-ast-to-ttir-code-generator.md)
