# 第 20 章 运行时（runtime）ABI 与动态链接库（DLL）：让 Python 安全地调用生成的 kernel

> **目标**：理解生成的动态链接库（DLL）暴露了什么 ABI（Application Binary Interface，应用程序二进制接口），会用 `ctypes` 的 `argtypes`/`restype` 正确调用，能设计错误码和失败路径，并用 median/min/max 而不是单次数字做基准测试（benchmark）。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；numpy 2.4.6；torch 2.14.1+cpu；MSVC 19.43（`cl` 14.43）。命令在仓库根目录执行：

```powershell
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
$vc = "D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat"
```

本章使用 [`code/mini_ai/runtime.py`](./code/mini_ai/runtime.py) 和 [`code/mini_ai/codegen_c.py`](./code/mini_ai/codegen_c.py)。上一章是 [Ch 19 向量化与编译选项](./19-vectorization-and-flags.md)，下一章是 [练习 D：C 后端](./practice-d-c-backend.md)。

## 为什么需要 ABI

C 后端生成的 DLL 不是“一个 Python 函数”。它是一段机器码，遵循 Windows x64 的调用约定，通过导出符号暴露一个入口。Python 侧要知道四件事才能安全调用：

1. 符号名是什么，参数按什么顺序排列。
2. 每个参数是值、指针还是数组，宽度是多少。
3. 返回值是什么类型，非零代表什么。
4. 内存由谁分配、谁释放，调用期间哪些对象必须保持存活。

这四件事合起来就是 ABI 契约。契约错一位，可能出现三种结果：立刻崩溃、静默写坏内存、或者返回一个看起来合理的错误结果。`ctypes` 不会替你做完整的类型检查；它按你声明的 `argtypes` 把参数塞进寄存器或栈。

## 先建立直觉

```text
Python 进程
+-----------------------------+
| numpy ndarray (float32)      |
|   data pointer  ------------+----+
+-----------------------------+    |
                                   v
+-----------------------------+   +----------------------+
| ctypes.CDLL("kernel.dll")   |   | kernel.dll           |
|   fn.argtypes = [ptr, ...]  |-->| mini_forward:        |
|   fn.restype = c_int        |   |   RCX = x0           |
|   rc = fn(*args)            |   |   RDX = out0         |
+-----------------------------+   |   ...                |
                                   |   arena = malloc(64) |
                                   |   loops              |
                                   |   copy to out0       |
                                   +----------+-----------+
                                              |
                                              v
                                   +----------------------+
                                   | return 0 / 1         |
                                   +----------------------+
```

调用链是：Python 准备指针 -> `ctypes` 按 ABI 传参 -> DLL 在 arena 里计算 -> 把输出拷到调用者 buffer -> 返回错误码 -> Python 检查返回值。

## 核心内容

### 1. DLL 导出了什么

生成代码的签名由 `graph.inputs` 和 `graph.outputs` 动态拼接：

```python
sig_inputs = [f"const float* x{i}" for i in range(len(self.graph.inputs))]
sig_outputs = [f"float* out{i}" for i in range(len(self.graph.outputs))]
lines.append(f"MINI_EXPORT int {self.function_name}({', '.join(sig_inputs + sig_outputs)}) {{")
```

`MINI_EXPORT` 在 MSVC 下展开为 `__declspec(dllexport)`：

```c
#if defined(_MSC_VER)
#define MINI_EXPORT __declspec(dllexport)
#else
#define MINI_EXPORT
#endif
```

用 `dumpbin /exports` 查看实际导出符号：

```powershell
cmd /c "call `"$vc`" >nul && dumpbin /exports .scratch\mini-ai-demo\mlp.dll"
```

实际输出（节选）：

```text
ordinal hint RVA      name

      1    0 00001000 mini_forward
```

x64 的 C 函数没有 32 位 x86 那种前导下划线装饰，导出名就是 `mini_forward`。如果写成 C++ 且不加 `extern "C"`，编译器会做名字修饰（name mangling），`getattr(lib, "mini_forward")` 就找不到符号。

### 2. Windows x64 调用约定

当前 DLL 是 C 函数，默认 `__cdecl`。在 Windows x64 上，整数和指针参数使用统一的调用约定：

| 参数位置 | 传递方式 |
| --- | --- |
| 第 1 个 | `RCX` |
| 第 2 个 | `RDX` |
| 第 3 个 | `R8` |
| 第 4 个 | `R9` |
| 第 5 个及以后 | 栈 |
| 返回值 | `EAX`（本函数返回 `int`） |

调用者还要为被调用者预留 32 字节的 shadow space，并保证栈对齐。`ctypes` 和 MSVC 生成的代码会处理这些细节；你不需要手写汇编，但需要知道参数顺序不能错。

`ctypes.CDLL` 使用 `cdecl`。在 x64 上 `__cdecl` 和 `__stdcall` 的调用序列已经统一，所以这里不需要 `WinDLL`。如果以后把 ABI 改成 32 位或使用其他约定，才需要重新检查。

### 3. `ctypes` 包装：`argtypes` 与 `restype`

配套的 `load_c_function` 做了四件事：

```python
def load_c_function(dll_path: str | Path, n_inputs: int, n_outputs: int, *, symbol: str = "mini_forward"):
    lib = ctypes.CDLL(str(Path(dll_path)))
    fn = getattr(lib, symbol)
    ptr = ctypes.POINTER(ctypes.c_float)
    fn.argtypes = [ptr] * (n_inputs + n_outputs)
    fn.restype = ctypes.c_int

    def run(inputs: Sequence[np.ndarray], output_shapes: Sequence[tuple[int, ...]]):
        if len(inputs) != n_inputs:
            raise ValueError(f"expected {n_inputs} inputs, got {len(inputs)}")
        ins = [_f32(a) for a in inputs]
        outs = [np.zeros(s, dtype=np.float32) for s in output_shapes]
        args = [a.ctypes.data_as(ptr) for a in ins]
        args += [a.ctypes.data_as(ptr) for a in outs]
        rc = fn(*args)
        if rc != 0:
            raise RuntimeError(f"{symbol} returned error code {rc}")
        return outs

    run.lib = lib  # keep the shared library alive
    return run
```

逐项解释：

- `ctypes.CDLL` 加载 DLL。路径必须是实际存在的 DLL，不能只给目录。
- `getattr(lib, symbol)` 按符号名取函数。符号不存在会抛 `AttributeError`。
- `fn.argtypes = [ptr] * (n_inputs + n_outputs)` 告诉 `ctypes`：每个参数都是 `float*`。没有这一行，`ctypes` 可能把 Python 整数按 `c_int` 处理，把 64 位地址截断，或者把 `float64` 数据按错误的宽度解释。
- `fn.restype = ctypes.c_int` 告诉 `ctypes` 返回值是 32 位有符号整数。默认返回类型也是 `c_int`，但显式写出更清楚，也避免以后改签名时忘记。
- `_f32` 把输入转成 `np.float32` 的 C-contiguous 数组。DLL 只看指针，不知道 NumPy 的 dtype 和 strides；传 float64 或转置视图会得到错误结果。
- `outs` 是调用者分配的 `np.zeros`。DLL 只写这些 buffer，不返回内部指针。
- `a.ctypes.data_as(ptr)` 把 ndarray 的地址转成 `float*`。这个指针不拥有内存，数组必须在调用期间保持存活。
- `run.lib = lib` 把 DLL 对象挂在闭包上，显式表明闭包持有库引用。ctypes 的函数对象本身也会通过 `_objects` 持有库，但不要依赖这个实现细节。
- `rc != 0` 抛 `RuntimeError`。不要忽略返回值继续读 `outs`，失败时输出内容没有定义。

### 4. 正常路径：matmul 的实际调用

用 `(2,3) @ (3,4)` 的图：

```python
rng = np.random.default_rng(3)
g = Graph()
a = g.add_input("a", (2, 3))
b = g.add_input("b", (3, 4))
c = g.add_op("matmul", [a, b])
g.add_output(c)

feed = {
    "a": rng.standard_normal((2, 3), dtype=np.float32),
    "b": rng.standard_normal((3, 4), dtype=np.float32),
}
ref = evaluate_graph(g, feed)[0]
result = compile_graph_c(g, Path(".scratch/practice-d"), name="case2")
out = run_c_graph(g, feed, result.dll_path)[0]
print(out.shape)
print("max_abs_error:", max_abs_error(out, ref))
```

实际输出：

```text
(2, 4)
max_abs_error: 1.1920928955078125e-07
```

这个误差是 float32 累加顺序差异，不是 ABI 错位。ABI 错位通常表现为崩溃、shape 正确但数值完全乱掉，或者输出 buffer 根本没被写。

### 5. 失败路径：错误码与参数个数

用一个故意返回 7 的测试 DLL：

```c
#define MINI_EXPORT __declspec(dllexport)
MINI_EXPORT int mini_forward(const float* x, float* out) { return 7; }
```

通过 `load_c_function` 调用：

```text
RuntimeError: mini_forward returned error code 7
ValueError: expected 1 inputs, got 2
```

第一行验证了 `rc != 0` 的路径；第二行验证了 Python 包装层的参数个数检查。两个错误都在 Python 侧可见，不会被静默吞掉。

当前生成器实际只会返回两个码：

| 返回值 | 含义 | 产生位置 |
| ---: | --- | --- |
| `0` | 成功 | 所有 op 执行完并拷贝输出 |
| `1` | arena 分配失败 | `_aligned_malloc` 返回 `NULL` |

可以扩展出更细的错误码，但要在 C 和 Python 两侧同时定义：

| 建议返回值 | 含义 | 备注 |
| ---: | --- | --- |
| `2` | 输入或输出指针为空 | 当前生成器不检查，调用方传 `NULL` 会崩 |
| `3` | 不支持的 op 或布局 | 当前在生成阶段就抛 `IRError`，不会进 DLL |
| `4` | shape/参数个数不匹配 | 当前由 Python 包装层检查 |

错误码不是越多越好。每一个都要有稳定的语义、测试和文档；返回一个笼统的 `1` 但打印日志，通常比一堆没有测试的码更可靠。

### 6. benchmark：median、min、max 与噪声

`benchmark` 的实现是：

```python
def benchmark(fn, *, warmup=5, repeat=30):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "repeat": float(repeat),
    }
```

三个统计量的用途不同：

- `median_ms`：主指标。对少量离群值不敏感，适合比较两个版本。
- `min_ms`：理想情况下的下界。可以用来诊断噪声，但不能单独拿来宣布加速。
- `max_ms`：最坏情况。系统调度、频率变化、其他进程会把它抬高。

`demo_e2e.py` 的 benchmark 里，C 路径调用的是 `run_c_graph`，而 `run_c_graph` 每次都会重新 `load_c_function` -> `ctypes.CDLL`。因此 demo 的 C 数字包含 DLL 加载开销，不是纯 kernel 时间。实际两次运行：

```text
第一次：C median ms: 0.1059, LLVM median ms: 0.0302, PyTorch median ms: 0.1722
第二次：C median ms: 0.1418, LLVM median ms: 0.0445, PyTorch median ms: 0.1534
```

课程简报记录的一次运行是：

```text
benchmark: C 0.0567 ms, LLVM 0.0272 ms, PyTorch 0.0716 ms
```

三组数字都是真实输出，但它们的口径不同，不能直接混在一起比较。把 C 函数加载一次、复用同一个函数对象后再测，结果会明显不同。写本章时做了一组 `warmup=20, repeat=200` 的对照：

```text
C(load-once) median 0.0264 ms, min 0.0155 ms, max 0.2745 ms
LLVM(JIT-once) median 0.0349 ms, min 0.0169 ms, max 4.3729 ms
PyTorch median 0.1281 ms, min 0.1037 ms, max 0.2714 ms
```

把四组数字放在一起：

| 来源 | C median ms | LLVM median ms | PyTorch median ms | 口径 |
| --- | ---: | ---: | ---: | --- |
| 课程简报记录 | 0.0567 | 0.0272 | 0.0716 | `demo_e2e.py`，warmup 5 / repeat 30 |
| 本次复跑 1 | 0.1059 | 0.0302 | 0.1722 | 同一 demo，机器负载不同 |
| 本次复跑 2 | 0.1418 | 0.0445 | 0.1534 | 同一 demo，再次运行 |
| 加载一次对照 | 0.0264 | 0.0349 | 0.1281 | warmup 20 / repeat 200，C 函数只加载一次 |

这组数字说明两件事：

1. demo 里的 C 0.1059/0.1418 包含每轮 DLL 加载、参数检查和输出分配；加载一次后 median 降到 0.0264。
2. 即使是同一台机器，max 也可能是 median 的十几倍。只看 min 会得到过于乐观的结论，只看单次运行会得到不可复现的结论。

报告 benchmark 时至少写清楚：CPU、shape、dtype、编译选项、warmup/repeat、是否包含 DLL 加载、输出分配和 Python 包装。少一项，别人就无法复现。

### 7. `run_c_graph` 的便利与代价

`run_c_graph` 是很薄的便利函数：

```python
def run_c_graph(graph, feed, dll_path):
    inputs = [feed[v.name] for v in graph.inputs]
    shapes = [v.type.shape for v in graph.outputs]
    fn = load_c_function(dll_path, len(inputs), len(shapes))
    return fn(inputs, shapes)
```

它按 `graph.inputs` 的名字取输入，按 `graph.outputs` 的 shape 分配输出，然后调用 `load_c_function`。便利之处是调用者不用记参数个数；代价是每次调用都重新加载 DLL、重新设置 `argtypes`、重新分配输出。

在正确性测试里，这个代价无所谓；在 benchmark 里，它会污染结果。要测 kernel，应该先做一次：

```python
fn = load_c_function(dll_path, n_inputs, n_outputs)
```

然后把 `fn([a, b], [(64, 64)])` 放进计时循环。即使这样，`load_c_function` 的包装层仍然每轮 `np.zeros` 分配输出；要再干净一些，就绕过包装层，用预分配的 `ctypes` 指针直接调用 `fn`。

## 底层机制

### 内存所有权

DLL 内部的所有临时 buffer 都在 arena 里，函数返回前释放。输入和输出 buffer 由调用者拥有：

```text
输入 ndarray  -> 调用者分配，DLL 只读
输出 ndarray  -> 调用者分配，DLL 写入
arena         -> DLL 分配，DLL 释放
DLL 句柄      -> Python 持有（run.lib 显式保存）
```

这条边界让 DLL 不需要知道 Python 的内存管理，也让调用者可以在多次调用之间复用输入/输出。代价是调用者必须保证 buffer 大小、dtype、连续性和生命周期。

### 输出拷贝

生成的 C 在所有 op 之后执行：

```c
for (int i = 0; i < 16; ++i) out0[i] = v_13_linear_1_fused[i];
```

为什么不让 `out0` 直接作为最后一个 op 的目标？因为内存规划器把输出 value 也当作 arena 里的 buffer，可能和别的 value 复用；直接把 `out0` 混进规划会破坏 live interval 和别名假设。统一拷贝的代价是一次线性复制，换来清晰的 ABI 边界。

### GIL 与并发

`ctypes.CDLL` 调用外部函数时会释放 GIL（Global Interpreter Lock，全局解释器锁），所以多个 Python 线程可以同时进入不共享可变状态的 DLL。当前生成代码每次调用都分配自己的 arena，常量是只读的，理论上可以并发调用。前提是调用者不给同一个输出 buffer 并发写。

`ctypes.PyDLL` 不释放 GIL，适合需要回调 Python 的场景；本课的 kernel 不需要回调，使用 `CDLL` 更合适。

### 栈、寄存器与浮点状态

x64 调用约定要求调用者预留 shadow space，并保持 16 字节栈对齐。`ctypes` 和 `cl` 生成的 prologue 会处理这些。浮点控制字、MXCSR 等状态由运行时维护；`/fp:fast` 可能改变舍入和异常行为，跨语言比较时要允许末位差异。

### 符号可见性

`__declspec(dllexport)` 把符号放进 DLL 的导出表。没有它，函数仍然存在，但外部看不到。可以用 `dumpbin /exports` 验证；如果导出表为空，`ctypes` 会抛 `AttributeError`，而不是在调用时崩溃。

## 进一步用法与变体

### 多输出

把 `sig_outputs` 扩展成多个 `float*`，每个输出一个 `out{i}`。`ctypes` 侧的 `argtypes` 长度变成 `n_inputs + n_outputs`，`outs` 也按 `graph.outputs` 的顺序分配。多输出图要逐个比较，不能只看第一个。

### C++ 与 `extern "C"`

如果后端用 C++ 写，导出函数要加：

```cpp
extern "C" __declspec(dllexport) int mini_forward(const float* x0, float* out0);
```

`extern "C"` 禁止 C++ 名字修饰，保证符号名是 `mini_forward`。函数体内部仍然可以用 C++，但 ABI 边界上的类型要保持 C 兼容。

### `stdcall` 与其他约定

32 位 Windows 上 `__stdcall` 和 `__cdecl` 的栈清理方不同，`ctypes` 要用 `WinDLL` 或 `WINFUNCTYPE`。x64 已经统一，不需要改。跨平台时，Linux 的 `.so` 用 `ctypes.CDLL`，导出符号默认可见，但要注意 `-fvisibility=hidden` 的构建选项。

### 与 LLVM JIT 的 ABI 对照

LLVM 后端的 JIT 函数签名是：

```text
(inputs..., outputs..., workspace)
```

它多一个 `workspace` 参数，由 Python 侧分配 `np.zeros(arena_bytes // 4)` 传入。C DLL 的 arena 在函数内部用 `_aligned_malloc` 分配。两者都把输出放在调用者提供的 buffer 里，但 workspace 的所有权不同。Ch 22 会展开这个对照。

## 对比与取舍

| 方案 | 优点 | 代价 | 适用场景 |
| --- | --- | --- | --- |
| C DLL + `ctypes` | 无第三方依赖，ABI 清晰 | 手动管理类型和生命周期 | 本课、小型推理 kernel |
| C++ + pybind11 | 类型转换自动，接口友好 | 引入依赖，编译配置复杂 | 大型扩展模块 |
| LLVM JIT + `ctypes` | 无需落盘 DLL，迭代快 | workspace 所有权在 Python，启动成本高 | 开发期、动态图 |
| `cffi` | 更接近 C 声明，类型检查强 | 额外依赖，构建步骤多 | 需要稳定 ABI 的项目 |
| 直接调 BLAS | 性能好，维护少 | 无法定制融合和布局 | 只做标准 matmul |

本课选择 C DLL，是因为它把 ABI、错误码、内存所有权都暴露出来，适合学习。生产环境是否选它，要看部署方式、依赖策略和性能目标，不能只因为“能跑”就定。

## 踩雷集锦

1. **不设置 `argtypes`。** `ctypes` 会按默认规则猜类型，64 位指针可能被截断，`float64` 可能被当成两个 `float32`。先声明 `[ptr] * (n_inputs + n_outputs)`，再调用。
2. **传非连续或非 float32 的数组。** DLL 只看指针和静态 shape，不知道 strides。用 `np.ascontiguousarray(..., dtype=np.float32)`，或者明确拒绝不支持的布局。
3. **忽略返回码。** 返回 1 时 arena 没有分配，输出 buffer 内容未定义；继续读会得到垃圾值。把 `rc != 0` 变成异常，是最低要求。
4. **输出 buffer 被提前释放。** 只保存 `ctypes` 指针、不保存 ndarray，Python 可能在调用前回收数组内存，DLL 就会写入已释放的地址。让输入/输出数组与调用处于同一作用域，或显式持有引用。
5. **把 demo 的 C 数字当纯 kernel 时间。** `run_c_graph` 每次都 `load_c_function`，包含 DLL 加载和输出分配。要比较 kernel，先加载一次、预分配输出，再计时。

## 进阶

- 给 `mini_forward` 增加空指针检查，定义错误码 2，并在 Python 侧映射成 `ValueError`。
- 写一个 `benchmark_kernel(fn, inputs, outputs)`，预分配输出，只计 `fn(*args)` 的时间。
- 用 `dumpbin /exports` 和 `dumpbin /dependents` 检查导出符号和运行时依赖。
- 把 C DLL 和 LLVM JIT 放在同一张表里比较，注明 workspace 和加载开销。
- 用多线程同时调用同一个 DLL，确认每次调用使用独立 arena，输出不互相覆盖。

## 动手练习

1. 编译一个返回 7 的测试 DLL，用 `load_c_function` 调用，确认抛出 `RuntimeError`。
2. 用 `dumpbin /exports` 查看自己生成的 DLL，确认符号名和参数顺序。
3. 把 `run_c_graph` 改成“加载一次、调用多次”，比较 demo 数字和加载一次后的 median。
4. 传一个转置后的 NumPy 视图给 DLL，观察错误；再用 `np.ascontiguousarray` 修复。
5. 设计一套错误码（0/1/2/3），在 C 和 Python 两侧实现并写测试。

## 本章重点

- DLL 的 ABI 由符号名、参数顺序、指针宽度、返回类型和内存所有权共同决定。
- `argtypes`/`restype` 是 `ctypes` 的类型契约；不设置会把错误延迟到运行时。
- 输入/输出由调用者拥有，arena 由 DLL 拥有；输出统一拷贝，边界清晰。
- 当前错误码只有 0（成功）和 1（arena 分配失败）；扩展错误码要同步 C 和 Python。
- benchmark 用 median 作主指标，同时报告 min/max、warmup/repeat 和是否包含加载/分配开销。
- demo 的 C/LLVM/PyTorch 数字与加载一次后的数字口径不同，不能直接混比；小于 1.5 倍的差异不下结论。

## 自我检核

- [ ] 我能写出 `mini_forward` 的参数顺序，并解释输入为什么在输出前面。
- [ ] 我能说明 Windows x64 前四个指针参数放在哪些寄存器。
- [ ] 我能解释 `argtypes`、`restype` 不设置时可能发生什么。
- [ ] 我能列出输入、输出、arena、DLL 句柄各自的所有者。
- [ ] 我能解释返回码 0/1 的含义，并知道扩展错误码要同步两侧。
- [ ] 我能复述 demo 两次运行和加载一次后的 C/LLVM/PyTorch median，并说明差异来源。
- [ ] 我知道 benchmark 要报告环境、shape、选项、warmup/repeat 和统计量。

## 延伸阅读

- **CPython `ctypes` 文档**：读哪里——`CDLL`、`argtypes`、`restype`、`POINTER`、`c_float`；学什么——Python 类型到 C 类型的映射；关联——本章的运行时包装。
- **Microsoft “x64 calling convention” 文档**：读哪里——寄存器参数、shadow space、栈对齐、返回值；学什么——DLL 入口的机器级契约；关联——为什么参数顺序不能错。
- **`code/mini_ai/runtime.py`**：读哪里——`load_c_function`、`run_c_graph`、`benchmark`；学什么——实际包装和计时实现；关联——本章所有调用示例。
- **`code/mini_ai/codegen_c.py` 的 `compile_c`**：读哪里——`cl /LD /O2 /fp:fast` 和构建目录；学什么——DLL 如何生成；关联——ABI 的另一半。
- **Ch 23 自动调优 mini 版**：读哪里——tile 搜索、成本模型、测量噪声；学什么——如何把本章的 benchmark 方法用于参数搜索；关联——median/min/max 的进一步使用。

下一章：[练习 D：实现 C 后端](./practice-d-c-backend.md)