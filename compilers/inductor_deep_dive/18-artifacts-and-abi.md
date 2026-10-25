# Ch 18 产物与 ABI：从 output_code.py 走到 .pyd

> **目标**：能把一次 `torch.compile` 的产物从磁盘一路追到内存里的函数指针；读懂 `output_code.py` 的六段结构；说清 `.cpp` 到 `.pyd` 的绑定方式；知道一次 kernel 调用跨了哪些边界、每层边界由谁负责。
> **环境**：Windows 10 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe`）。命令都在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

这一章接着练习 C 的自定义 lowering。那时你关心的是「算子能不能进 kernel」；现在要关心「kernel 生成之后，怎么变成一个可以被 Python 调用的东西」。

## 为什么

编译器的产物不是「一个文件」，而是四层表示：

1. **Python 模块**：`<hash>.py`，也就是 `output_code.py`，它负责分配缓冲区、调用 kernel、返回结果。
2. **C++ 源码**：`<hash>.main.cpp`，里面是 `extern "C" __declspec(dllexport)` 的 kernel 和一段 CPython 绑定后缀。
3. **动态库**：`<hash>.main.pyd`，Windows 上的共享库，导出 `PyInit_kernel`。
4. **内存里的函数指针**：`<key>.kernel` 模块对象上的 `kernel` 函数，被 wrapper 直接调用。

如果只盯着第 1 层，会遇到三类问题：

- **改了 C++ 却看不到变化**：因为 Python 层缓存了模块对象，或者磁盘上那份 `.py` 根本没重新写。
- **改了生成代码但运行结果不对**：因为 `.pyd` 还是旧的，参数顺序或类型对不上时，错误会以崩溃或静默写坏内存的方式出现。
- **无法解释性能差异**：因为不知道一次调用里有多少时间花在 Python 边界、多少次落在 kernel 上。

这一章把四层拆开看，并给出一套「从下往上读产物」的顺序。读完之后，你应该能拿到任意一个 `.debug` 目录，在几分钟内说清它生成了几个 kernel、每个 kernel 的循环结构、以及 wrapper 是怎么把它们串起来的。

## 主线

### 1. 产物地图：hash 决定文件名，cache 决定目录

所有产物都在 `cache_dir()` 下面。`cache_dir()` 读环境变量 `TORCHINDUCTOR_CACHE_DIR`，没有设置时才退回系统默认目录。本课程由 `run.ps1` 统一设成 `code/.cache`，所以每次实验的产物都能在仓库里翻到。

写文件的逻辑集中在 `torch/_inductor/codecache.py` 的 `write()`：

```python
def get_path(basename: str, extension: str, specified_dir: str = "") -> tuple[str, str, str]:
    if specified_dir:
        ...
    else:
        subdir = os.path.join(cache_dir(), basename[1:3])
    path = os.path.join(subdir, f"{basename}.{extension}")
    return basename, subdir, path
```

两点值得记住：

- **文件名是内容哈希**。`write()` 先做 `get_hash(content.strip(), extra)`，把源码（去掉首尾空白）和 `extra` 一起哈希。内容一样、`extra` 一样，就命中同一个文件。
- **目录取哈希的第 2、3 个字符**。`basename[1:3]` 是分片，避免单目录塞几十万个文件。所以你会看到 `code/.cache/iu/ciuth...py` 这种形状。

`<hash>.debug/` 目录跟着 **Python 模块的 hash** 走，而 `.main.cpp` / `.main.pyd` 跟着 **C++ 源码加编译命令行的 hash** 走。它们在 cache 里通常不在同一个分片目录，这不是错误。

本机一次 `lab_a_pipeline.py` 运行产生的 `.debug` 目录如下（文件大小会随图和版本变化，这里只说明形状）：

| 文件 | 大小 | 作用 |
| --- | ---: | --- |
| `fx_graph_readable.py` | 955 B | AOTAutograd 之后的 core ATen 图，带源码行号注释 |
| `fx_graph_runnable.py` | 2932 B | 同一张图的可执行版本，带 `get_args` |
| `fx_graph_transformed.py` | 917 B | post-grad passes 之后的图 |
| `ir_pre_fusion.txt` | 2842 B | 调度器融合前的 IR |
| `ir_post_fusion.txt` | 3591 B | 融合后的 IR |
| `output_code.py` | 6226 B | 最终 Python 模块，含 C++ kernel 源码 |

课文反复提到的四件套是 `fx_graph_readable.py`、`ir_pre_fusion.txt`、`ir_post_fusion.txt`、`output_code.py`；这个版本还会多写 `fx_graph_runnable.py` 和 `fx_graph_transformed.py`，属于正常现象。

**`<hash>.debug/` 不是必然存在。** `DebugContext.__enter__` 里有一句：

```python
if not config.trace.enabled:
    return
self._path = self.create_debug_dir(get_aot_graph_name())
```

`config.trace.enabled` 为假时，`DebugContext` 只挂调试处理器，不建目录。`lab_a` 在文件顶部写了 `cfg.trace.enabled = True`，所以你能看到 `.debug`；`env_report.py` 没有开，就只会留下 `<hash>.py` 和 `.pyd`。

还有一条容易忽略的路径：`DebugContext` 最初创建的是 `get_debug_dir()/torchinductor/<graph_name>.<n>/`，其中 `get_debug_dir()` 落在**当前工作目录**的 `torch_compile_debug/run_<时间>-pid_<进程号>` 下。编译结束时，`graph.py` 才把它复制成 cache 里的 `<hash>.debug`：

```python
V.debug.output_code(path)
V.debug.copy(os.path.splitext(path)[0] + ".debug")
```

所以在仓库根目录看到 `torch_compile_debug/` 是正常的：那是原始目录，cache 里的 `.debug` 是副本。`run.ps1 -DebugArtifacts` 做的事情就是把 `TORCH_COMPILE_DEBUG=1` 传进去，让 `trace.enabled` 从进程启动时就是真。

### 2. output_code.py 的六段结构

打开任意一份 `output_code.py`，从上往下永远是这六段：

1. **头部 import**：`AsyncCompile`、`extern_kernels`、`empty_strided`、guards 相关函数。
2. **kernel 声明**：`async_compile.cpp_pybinding([...], r'''...''')`，C++ 源码以字符串形式内嵌。
3. **等待编译**：`async_compile.wait(globals())`，把 future 换成真正的可调用对象。
4. **`Runner` 类**：`__init__`、`recursively_apply_fns`、`call`。
5. **模块级绑定**：`runner = Runner(partitions=[])`、`call = runner.call`。
6. **自测入口**：`get_args()`、`benchmark_compiled_module()`、`if __name__ == "__main__"`。

下面这段来自本机 lab_a 的真实产物，删掉了部分重复行：

```python
cpp_fused_div_relu_sum_0 = async_compile.cpp_pybinding(['float*', 'const float*'], r'''
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C" __declspec(dllexport) void  kernel(float* in_out_ptr0,
                       const float* in_ptr0)
{
    std::atomic<int> inductor_cpu_integer_div_error{0};
    inductor_cpu_integer_div_error_flag = &inductor_cpu_integer_div_error;
    auto out_ptr0 = in_out_ptr0;
    #pragma omp parallel num_threads(4)
    {
        int tid = omp_get_thread_num();
        ...
    }
}
''')
```

三个细节：

- 函数名固定是 `kernel`，不是 `cpp_fused_div_relu_sum_0`。后者是 **Python 侧的变量名**，由调度器按融合内容命名（这里是 `div + relu + sum`）。
- 签名里输出兼原地缓冲区在前面（`in_out_ptr0`），只读输入在后面（`in_ptr0`）。参数顺序由 wrapper 的调用点决定，别凭「输入在前」的直觉去读。
- `out_ptr0 = in_out_ptr0` 是别名：这个 kernel 把归约结果写回同一个缓冲区，后面 `#pragma omp single` 段再原地除以 64。

`Runner.call` 是理解数据流的入口：

```python
def call(self, args):
    arg0_1, arg1_1, arg2_1 = args
    args.clear()
    assert_size_stride_grouped((arg2_1, arg0_1, arg1_1), ((64, ), (64, 64), (64, 64)), ((1, ), (64, 1), (64, 1)), 'input')
    buf0 = empty_strided_cpu((64, 64), (64, 1), torch.float32)
    # Topologically Sorted Source Nodes: [add], Original ATen: [aten.add]
    extern_kernels.addmm(arg2_1, arg0_1, arg1_1, alpha=1, beta=1, out=buf0)
    del arg0_1
    del arg1_1
    del arg2_1
    buf1 = empty_strided_cpu((64, ), (1, ), torch.float32)
    buf2 = buf1; del buf1  # reuse
    cpp_fused_div_relu_sum_0(buf2, buf0)
    return (buf2, )
```

这段代码同时暴露了三件事：

- **`args.clear()`**：wrapper 会清空传进来的 list。后面要重复调用，就得每次重新构造 list（`benchmark_compiled_module` 里的 `call(list(args))` 就是为此）。
- **`assert_size_stride_grouped`**：形状和步长检查在 Python 侧，不在 kernel 里。检查失败会抛断言错误，而不是让 kernel 读写越界。
- **`buf1`/`buf2` 的 reuse**：`buf2 = buf1; del buf1` 是内存规划的结果，两个逻辑缓冲区复用同一块内存。`ir_post_fusion.txt` 里能看到对应的 `reuse` 标记。

### 3. cpp 到 pyd：CPython C-API 绑定，不是 pybind11

`async_compile.cpp_pybinding(argtypes, source)` 的背后是 `CppPythonBindingsCodeCache.load_pybinding_async`。它把一段固定的后缀拼到 kernel 源码后面，再交给 `CppBuilder` 编译成 `.pyd`。

后缀的关键部分（原文见 `codecache.py` 的 `suffix_template`）：

```cpp
// Python bindings to call kernel():
#define PY_SSIZE_T_CLEAN
#include <Python.h>

static void* (*_torchinductor_pyobject_tensor_data_ptr)(PyObject* obj);

template <typename T> static inline T parse_arg(PyObject* args, size_t n) {
    static_assert(std::is_pointer_v<T>, "arg type must be pointer or long");
    return static_cast<T>(_torchinductor_pyobject_tensor_data_ptr(PyTuple_GET_ITEM(args, n)));
}

static PyObject* kernel_py(PyObject* self, PyObject* args) {
    try {
        if(!PyTuple_CheckExact(args))
            throw std::runtime_error("tuple args required");
        if(PyTuple_GET_SIZE(args) != 2)
            throw std::runtime_error("requires 2 args");
        kernel(parse_arg<float*>(args, 0), parse_arg<const float*>(args, 1)); Py_RETURN_NONE;
    } catch(std::exception const& e) {
        PyErr_SetString(PyExc_RuntimeError, e.what());
        return nullptr;
    }
}

static PyMethodDef py_methods[] = {
    {"kernel", kernel_py, METH_VARARGS, ""},
    {NULL, NULL, 0, NULL}};

PyMODINIT_FUNC PyInit_kernel(void) {
    const char* str_addr = std::getenv("_TORCHINDUCTOR_PYOBJECT_TENSOR_DATA_PTR");
    ...
    _torchinductor_pyobject_tensor_data_ptr = reinterpret_cast<decltype(...)>(addr);
    return PyModule_Create(&py_module);
}
```

这不是 pybind11，是手写的 CPython C-API 模块。选择它的原因可以算一笔账：pybind11 需要额外头文件、模板实例化和更长的编译时间；这里真正需要的只有「从 `PyObject*` 拿 tensor 的 `data_ptr`」和「按位置取参数」两件事，手写更短也更快。

`data_ptr` 的取法很特别：模块初始化时从环境变量 `_TORCHINDUCTOR_PYOBJECT_TENSOR_DATA_PTR` 读一个**函数地址**，转成函数指针。这个地址由 `CppPythonBindingsCodeCache._load_library_inner` 在 import 之前写入：

```python
os.environ["_TORCHINDUCTOR_PYOBJECT_TENSOR_DATA_PTR"] = str(
    torch._C._dynamo.guards._torchinductor_pyobject_tensor_data_ptr
)
module_name = f"{key}.{cls.entry_function}"
...
spec = importlib.util.spec_from_file_location(module_name, path)
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
```

这条设计的后果：`.pyd` 不需要在符号表里找到 torch 的 C++ 接口，只要拿到一个函数指针；模块名必须是 `<key>.kernel`，否则 `sys.modules` 里的复用逻辑对不上。

调用链可以画成这样：

```text
Python                          C++ / CPython
call(args)                      PyInit_kernel()  （import 时执行一次）
  cpp_fused_..._0(buf2, buf0)   kernel_py(self, args)
                                  PyTuple_GET_ITEM(args, 0) -> PyObject*
                                  parse_arg<float*>(args, 0)
                                    _torchinductor_pyobject_tensor_data_ptr(obj)
                                  kernel(in_out_ptr0, in_ptr0)
                                  Py_RETURN_NONE
```

注意 `parse_arg` 的重载：指针类型走函数指针取址，`int64_t` 走 `PyLong_AsSsize_t`，`float` 走 `PyFloat_AsDouble`。自定义 lowering 如果引入标量参数，会体现在 `cpp_pybinding` 的 `argtypes` 列表里。

### 4. 一次调用的边界账

把一次 `call` 拆成边界，能看清时间都花在哪里：

| 边界 | 谁负责 | 出错时的表现 |
| --- | --- | --- |
| Python wrapper → 模块函数 | CPython 调用协议 | `TypeError`、`RuntimeError` |
| tuple 参数 → `PyObject*` | `PyTuple_GET_ITEM` | 参数个数不符会抛 `requires N args` |
| `PyObject*` → `data_ptr` | 预装的函数指针 | 取错对象会拿到非法地址 |
| `data_ptr` → kernel 参数 | `parse_arg<T>` | 类型不匹配时可能截断指针 |
| kernel 内部循环 | 生成的 C++ | 越界写、数值错误 |
| 返回值 | wrapper 的 `return (buf2, )` | 返回了被复用的缓冲区 |

一次调用里 Python 侧的固定开销包括：list 解包、`assert_size_stride_grouped`、若干次 `empty_strided_cpu`、一次 extern kernel 调用、一次生成 kernel 调用。kernel 里的循环不经过 Python，这是 Inductor 相对于逐 op eager 的主要收益来源。

但「Python 开销」不等于「可以忽略」。当一个 kernel 只有几十微秒时，wrapper 的几次分配和断言就会占可见比例；这也是第 21 章要把「结构证据」和「时间证据」分开的原因。

### 5. 手工读产物的顺序

拿到一个 `.debug` 目录，按下面顺序读，比从 `output_code.py` 第一行硬啃效率高：

1. **先看 `Runner.call`**：数一数有几个 `extern_kernels.*` 和几个生成 kernel 调用。这决定了「融合成了几段」。
2. **再看 kernel 函数体**：`kernel_stats` 关心的四件事——`Vectorized<T>`、`x0 += N`、`#pragma omp`、`auto tmpN =`。
3. **回到 `ir_post_fusion.txt`**：确认 buffer 的 `reuse`、每个 `SchedulerNode` 的 `writes`、以及 `unmet_dependencies` 是否跨 kernel。
4. **最后看 `fx_graph_readable.py`**：确认语义层是不是你写的那几个 aten 算子。语义不对，后面的结构分析没有意义。

这四步对应第 1 章讲的「同一段语义、四种表示」：

```text
fx_graph_readable.py  →  ir_pre_fusion.txt  →  ir_post_fusion.txt  →  output_code.py
   （语义）                （调度前 IR）          （融合后 IR）          （代码 + 绑定）
```

手册里有个实用技巧：先用 `common.kernel_stats(src)` 打印结构特征，再带着「step 是几、有没有 omp、tmp 有几条」去 kernel 里找对应行，比逐行读快得多。

## 可跑实验

### 实验 1：生成产物并定位

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py
```

脚本会打印 `.debug` 目录的绝对路径、四个文件的大小、core ATen 图节选、`ir_pre_fusion.txt` 节选、kernel 结构、wrapper 调用，并做四条断言。预期看到：

- `extern kernels: ['addmm']`，说明 matmul 走 extern kernel；
- kernel 名类似 `cpp_fused_div_relu_sum_0`；
- `vectorized=True`，因为归约内层用了 `Vectorized<float>`；
- `step=1`。这里的 `step` 抓的是最外层 `x0+=...`，lab_a 的归约 kernel 外层按 1 走、内层按 8 走；lab_d 的纯 pointwise kernel 才会出现 `step=8`。

### 实验 2：列出 cache 里的分层

```powershell
Get-ChildItem -LiteralPath compilers\inductor_deep_dive\code\.cache -Directory | Select-Object Name
Get-ChildItem -LiteralPath compilers\inductor_deep_dive\code\.cache\fxgraph -Recurse -File | Select-Object -First 5 FullName
Get-ChildItem -LiteralPath compilers\inductor_deep_dive\code\.cache -Recurse -Filter *.main.pyd | Select-Object FullName, Length
```

你会看到 `fxgraph/`、`aotautograd/`、`locks/`、按两位十六进制分片的目录，以及 `perf/`、`final/` 这类由实验自己造的子树。`.main.pyd` 的数量通常等于「这份课程跑过的不同 kernel 配置数」，不是「编译次数」。

### 实验 3：让产物复制到仓库根

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_a_pipeline.py -DebugArtifacts
```

`run.ps1` 会把 `TORCH_COMPILE_DEBUG` 设成 `1`。此时 `torch_compile_debug/run_<时间>-pid_<进程号>/torchinductor/<图名>.<n>/` 里会保留原始产物。cache 里的 `.debug` 与它内容一致，区别是后者按内容哈希命名，便于程序定位。

### 实验 4：按符号找源码

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\tools\find_source.py torch._inductor.codecache CppPythonBindingsCodeCache
```

输出会给出定义文件和起始行号。行号会随版本漂移，函数名不会，所以课文里的锚点都以函数名为准。

## 源码落点

| 位置 | 读什么 | 为什么读 |
| --- | --- | --- |
| `torch/_inductor/codecache.py` 的 `write` / `get_path` / `get_hash` | 哈希与分片规则 | 解释「为什么文件名是一串哈希」 |
| `torch/_inductor/codecache.py` 的 `CppPythonBindingsCodeCache` | `suffix_template`、`entry_function`、`call_entry_function` | 绑定的全部协议都在这里 |
| `torch/_inductor/codecache.py` 的 `CppCodeCache.load_async` | `write(main_code, "main.cpp", extra=...)`、`_load_library` | cpp 到 pyd 的编译与加载 |
| `torch/_inductor/codegen/wrapper.py` | `PythonWrapperCodegen` | `Runner.call` 是怎么拼出来的 |
| `torch/_inductor/graph.py` 的 `graph_codegen` | `PyCodeCache.write`、`V.debug.copy` | `.py` 与 `.debug` 的关系 |
| `torch/_inductor/debug.py` 的 `DebugContext` | `create_debug_dir`、`__enter__`、`copy` | 调试目录的生命周期 |
| `torch/_inductor/compile_fx.py` | `DebugContext()` 的进入点 | 一次编译从哪开始写产物 |

读 `suffix_template` 时，注意 `{arg_len}`、`{call_entry_func}`、`{entry_func}` 三个占位符：它们决定参数个数检查、调用语句和模块初始化函数名。改自定义后端时会动到这几个值。

## 常见坑

1. **以为 `.debug` 永远存在。** 只有 `config.trace.enabled` 为真才建目录。不开 trace 时 cache 里只有 `<hash>.py` 和 `.pyd`，找不到 `ir_pre_fusion.txt` 是正常现象。

2. **把 `.debug/output_code.py` 当源码改。** 它是快照，下次编译按内容哈希写新文件；而且 `PyCodeCache` 可能直接复用磁盘上的旧 `.py`。要改行为，去改 lowering、pass 或 config，不要编辑产物。

3. **用 UTF-8 硬解产物。** 本机生成的中间文件里，中文注释可能按 cp936 写出。用 `common.read_text_any` 的兜底逻辑，不要 `read_text(encoding="utf-8")` 硬读，否则会看到一片替换字符。

4. **把 `cpp_pybinding` 当成 pybind11。** 生成的是手写 CPython C-API 模块，入口是 `PyInit_kernel`，参数解析用 `PyTuple_GET_ITEM` 和 `parse_arg<T>`。用 pybind11 的经验去套，会找不到 `py::arg` 之类的痕迹。

5. **把 `include_pytorch=False` 读成「不依赖 torch 运行时」。** 生成代码本身就 `#include <torch/csrc/inductor/cpp_prefix.h>`。它只是一个构建选项字段；实际包含路径与链接参数由 `CppBuilder` 根据 `BuildOption` 组装，不要凭字段名推断依赖关系。

6. **参数顺序凭直觉。** 指针参数按 wrapper 的调用顺序排，输不一定是「输入在前」。自定义 lowering 引入新签名时，先看 `cpp_pybinding` 的第一个 list，再对照 kernel 定义。

7. **忽略 `args.clear()`。** wrapper 会清空传入的 list。重复调用同一个 list 会得到空参数错误；benchmark 要每次 `list(args)`。

8. **只看 `output_code.py` 不看 IR。** 缓冲区复用、融合边界、依赖关系在 `ir_post_fusion.txt` 里；只读代码会漏掉「这两个 buf 其实是同一块内存」这类事实。

## 自我检查

- [ ] 我能说出 `<hash>` 是怎么算出来的，以及为什么目录名取哈希的第 2、3 个字符。
- [ ] 我能按顺序列出 `output_code.py` 的六段结构，并指出 `Runner.call` 在其中的位置。
- [ ] 我能解释 `.debug` 目录与 `torch_compile_debug/run_...` 目录的关系，以及为什么后者在仓库根。
- [ ] 我能写出 `kernel_py` 接收参数、取出 `data_ptr`、调用 `kernel` 的完整路径。
- [ ] 我能说明 `_TORCHINDUCTOR_PYOBJECT_TENSOR_DATA_PTR` 的作用，以及为什么模块名必须是 `<key>.kernel`。
- [ ] 我能从 `Runner.call` 里数出 extern kernel 数量和生成 kernel 数量。
- [ ] 我能解释 `buf1`/`buf2` 的 reuse 在 IR 里的对应写法。
- [ ] 我知道为什么「编辑 output_code.py」不是正确的修改方式。

## 延伸阅读

- **CPython C-API 文档**：读哪里——`PyModule_Create`、`PyMethodDef`、`PyTuple_GET_ITEM`、`PyErr_SetString`；学什么——手写扩展模块的最小协议；关联——`suffix_template` 的每一行。
- **`torch/_inductor/codecache.py` 的 `suffix_template`**：读哪里——`parse_arg` 的四个重载；学什么——指针、`int64_t`、`uintptr_t`、`float` 分别怎么解析；关联——自定义 lowering 的标量参数。
- **`torch/_inductor/codegen/wrapper.py`**：读哪里——`PythonWrapperCodegen` 如何生成 `Runner.call`、`empty_strided`、`del`；学什么——内存规划在代码层的落点；关联——第 12 章的 buffer 复用。
- **`torch/_inductor/debug.py`**：读哪里——`DebugContext`、`DebugFormatter`、`_write_ir`；学什么——`.debug` 里每个文件由谁写；关联——本章的产物地图。
- **Microsoft “Building C/C++ programs” 文档**：读哪里——`/LD`、导出符号、运行时库；学什么——`.pyd` 在 Windows 上是普通 DLL；关联——`cl.exe` 编译失败时的排查方向。

[上一章：练习 C：自定义 lowering](./practice-c-custom-lowering.md) | [下一章：Ch 19 缓存体系](./19-cache-system.md)