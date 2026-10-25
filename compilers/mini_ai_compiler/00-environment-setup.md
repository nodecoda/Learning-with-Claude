# Ch 0 环境与最小闭环

> 目标：搭好可复现的 Python + PyTorch + MSVC 环境，亲手把一个计算图编译成 Windows 动态链接库（DLL），再用 `ctypes` 调用它，得到与 Python 参考实现一致的数值结果。本章结束时，你应当能独立回答：源码在哪里、DLL 在哪里、参数怎样穿过 ABI（Application Binary Interface，应用二进制接口）、出错时先看哪一层。

> 环境：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；numpy 2.4.6；llvmlite 0.50.0；MSVC 14.43。以下命令在 PowerShell 中执行，工作目录为 `D:\Learning-with-Claude`。版本输出来自本机实际运行。

## 为什么需要

编译器课程容易停在“读得懂图，但跑不起来”。你会看到 IR（Intermediate Representation，中间表示）、pass（优化遍）、内存规划这些词，却不知道它们最终有没有变成机器能执行的东西。本章用一个很小但完整的闭环把这条链路钉死：

1. 用 Python 构造一个计算图。
2. 生成 C 源码。
3. 调用 MSVC 的 `cl` 编译出 DLL。
4. 用 `ctypes` 把 NumPy 数组的指针传进去。
5. 比较 DLL 输出与解释器输出。

这个闭环不依赖 CUDA，也不依赖 LLVM。它让你先看清“编译器输出的是一个有明确 ABI 的二进制”，后面读 LLVM、内存规划、Pass 时才有落点。

本章的验收标准不是“看完了”，而是你能重复下面的命令，并解释每一个路径和每一个返回值。

## 先建立直觉

```text
  Python 进程                                  Windows 进程
  ------------                                 -------------
  Graph(IR)
     |
     | generate_c(graph)
     v
  ch0_demo.c  ----------------------------->  cl /LD /O2 /fp:fast
                                                   |
                                                   v
                                             ch0_demo.dll
                                                   |
     +---------------------------------------------+
     |
     | ctypes.CDLL + argtypes
     v
  run_c_graph(graph, feed, dll_path)
     |
     | float* x0, float* out0
     v
  NumPy ndarray  <---- 返回码 0 / 1
```

关键点：Python 和 DLL 不共享 Python 对象。它们只共享一块按行主序排列的 `float32` 内存，以及一个整数返回码。IR 里的 shape、常量、临时缓冲区，最终都要落成 C 里的指针和偏移。

## 核心内容

### 0.1 目录与 Python 环境

课程代码根目录是 `compilers/mini_ai_compiler/code/`。本机使用已有的虚拟环境：

```powershell
$py = ".scratch\aic-env\Scripts\python.exe"
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& $py -c "import sys, torch, numpy, llvmlite; print(sys.version); print(torch.__version__, numpy.__version__, llvmlite.__version__)"
```

实际输出：

```text
3.11.12 (main, May 17 2025, 13:44:56) [MSC v.1943 64 bit (AMD64)]
2.14.1+cpu 2.4.6 0.50.0
```

如果你要从零建环境，README 给出的方式是：

```powershell
python -m venv .scratch\mini-ai-env
$py = ".scratch\mini-ai-env\Scripts\python.exe"
& $py -m pip install torch --index-url https://download.pytorch.org/whl/cpu
& $py -m pip install numpy llvmlite
```

`PYTHONPATH` 指向 `code/` 而不是 `code/mini_ai/`，因为导入语句是 `from mini_ai...`。路径写错时，第一个报错通常是 `ModuleNotFoundError: No module named 'mini_ai'`。

### 0.2 构造一个最小图

下面的脚本只做 `relu(x + b)`。它有 1 个输入、1 个常量、2 个 op、1 个输出，足够覆盖“输入指针、常量数组、临时缓冲区、输出拷贝”四件事。

```python
import numpy as np
from mini_ai.ir import Graph
from mini_ai.codegen_c import compile_graph_c, generate_c
from mini_ai.runtime import run_c_graph
from mini_ai.interpreter import evaluate_graph

g = Graph()
x = g.add_input("x", (2, 3))
b = g.add_const("b", np.array([1, 2, 3], np.float32))
y = g.add_op("add", [x, b], name="y")
r = g.add_op("relu", [y], name="r")
g.add_output(r, "out")

print(g.dump())
```

实际输出：

```text
inputs:
  x: tensor<2, 3>
constants:
  b: tensor<3> numel=3
ops:
  00 %y = add(x, b) : tensor<2, 3>
  01 %out = relu(y) : tensor<2, 3>
outputs:
  out: tensor<2, 3>
```

注意 `add_output(r, "out")` 把 `r` 这个值重命名成了 `out`，所以 dump 里显示 `%out = relu(y)`。这在 Ch 2 会展开讲，现在要知道：输出的名字由 `add_output` 决定。

### 0.3 生成 C 并编译 DLL

```python
res = compile_graph_c(g, ".scratch/mini-ai-ch0", name="ch0_demo")
print("C:", res.code_path)
print("DLL:", res.dll_path)
```

实际输出：

```text
C: .scratch\mini-ai-ch0\ch0_demo.c
DLL: .scratch\mini-ai-ch0\ch0_demo.dll
```

`compile_graph_c` 内部调用 `generate_c` 得到源码，再交给 `compile_c` 写入构建目录并编译。`compile_c` 会先创建目录并写入 `name.c`，然后做三件事：

1. 用 `find_vcvars64()` 找 `vcvars64.bat`。
2. 写一个 `build.bat`，内容包含 `call "<vcvars>"` 和 `cl` 命令。
3. 用 `subprocess.run(["cmd", "/c", "build.bat"], cwd=build)` 执行，并读取 `build.log`。

真正的编译命令是：

```text
cl /nologo /LD /O2 /fp:fast /Fe:ch0_demo.dll ch0_demo.c
```

`/LD` 表示生成 DLL，`/O2` 打开优化，`/fp:fast` 允许更激进的浮点优化。`MINI_EXPORT` 在 MSVC 下展开为 `__declspec(dllexport)`，函数才会出现在 DLL 的导出表里。

生成的核心 C 代码如下（节选，来自 `.scratch/mini-ai-ch0/ch0_demo.c`）：

```c
MINI_EXPORT int mini_forward(const float* x0, float* out0) {
    const size_t arena_bytes = 128;
#if defined(_MSC_VER)
    float* arena = (float*)_aligned_malloc(arena_bytes, 64);
#else
    float* arena = (float*)aligned_alloc(64, arena_bytes);
#endif
    if (!arena) return 1;

    const float* v_0_x = x0;
    const float* v_1_b = C_1;
    float* v_2_y = (float*)((char*)arena + 0);
    float* v_3_out = (float*)((char*)arena + 64);

    /* add: y */
    for (int i0_0 = 0; i0_0 < 2; ++i0_0) {
        for (int i0_1 = 0; i0_1 < 3; ++i0_1) {
            v_2_y[(i0_0)*3 + i0_1] = v_0_x[(i0_0)*3 + i0_1] + v_1_b[i0_1];
        }
    }

    /* relu: out */
    for (int i1_0 = 0; i1_0 < 2; ++i1_0) {
        for (int i1_1 = 0; i1_1 < 3; ++i1_1) {
            v_3_out[(i1_0)*3 + i1_1] = fmaxf(v_2_y[(i1_0)*3 + i1_1], 0.0f);
        }
    }

    for (int i = 0; i < 6; ++i) out0[i] = v_3_out[i];

    _aligned_free(arena);
    return 0;
}
```

这里有四个可以直接观察的事实：

- 函数签名是 `const float* x0, float* out0`，输入是只读指针，输出是可写指针。
- 临时值 `y` 和 `out` 不各自 `malloc`，而是从一块 64 字节对齐的 arena（预分配内存区）里切偏移。
- 输入 `x` 和常量 `b` 没有分配 arena，它们直接指向外部内存或静态数组。
- 输出值先写到 arena，再整体拷贝到 `out0`。这是为了不让调用方看到 arena 的生命周期。

### 0.4 用 ctypes 调进去

`runtime.py` 里的 `load_c_function` 做了三件事：加载 DLL、声明参数类型、把 NumPy 数组转成指针。

```python
ptr = ctypes.POINTER(ctypes.c_float)
fn.argtypes = [ptr] * (n_inputs + n_outputs)
fn.restype = ctypes.c_int
```

`argtypes` 的顺序就是生成的 C 签名顺序：先所有输入，再所有输出。`restype` 是 `int`，对应 `mini_forward` 的返回码。`run` 内部会把每个数组转成连续的 `float32`，用 `a.ctypes.data_as(ptr)` 取指针，然后调用 `fn(*args)`。

完整调用：

```python
feed = {"x": np.array([[-1, 0, 1], [2, -3, 4]], np.float32)}
ref = evaluate_graph(g, feed)[0]
out = run_c_graph(g, feed, res.dll_path)[0]

print("out:", out.tolist())
print("ref:", ref.tolist())
print("max_abs_error:", float(np.max(np.abs(out - ref))))
```

实际输出：

```text
out: [[0.0, 2.0, 4.0], [3.0, 0.0, 7.0]]
ref: [[0.0, 2.0, 4.0], [3.0, 0.0, 7.0]]
max_abs_error: 0.0
```

`max_abs_error` 为 0 不是巧合：`add` 和 `relu` 都是逐元素运算，没有归约顺序差异，输入又是小整数，`float32` 能精确表示。换成 matmul 后，误差会变成 `1e-8` 量级，那是浮点累加顺序造成的，不是编译器错误。

### 0.5 两条失败路径

第一条：feed 里少了输入。`run_c_graph` 按 `graph.inputs` 的名字取数组，不会按位置猜。

```python
run_c_graph(g, {}, res.dll_path)
```

实际报错：

```text
KeyError: 'x'
```

第二条：C 后端不支持某个算子。解释器支持 `softmax`，C 后端目前明确拒绝：

```python
g2 = Graph()
x2 = g2.add_input("x", (2, 3))
y2 = g2.add_op("softmax", [x2], {"axis": -1})
g2.add_output(y2)
generate_c(g2)
```

实际报错：

```text
IRError: C backend does not implement softmax yet; use sum/exp/div
```

这两条失败路径说明了一件事：编译器的能力边界必须显式报错。静默生成错误代码，比拒绝编译危险得多。

## 底层机制

### 从 Python 到 DLL 的实际进程边界

`compile_c` 不直接调用 `cl.exe`，而是先写 `build.bat`，再通过 `cmd /c` 执行。原因是 `vcvars64.bat` 是一批环境变量设置脚本，必须在同一个 `cmd` 进程里 `call` 之后，`cl` 才能找到头文件和库。直接 `subprocess.run(["cl", ...])` 在没初始化的环境里会失败。

构建目录里会出现：

```text
.scratch/mini-ai-ch0/
  ch0_demo.c
  ch0_demo.dll
  ch0_demo.exp
  ch0_demo.lib
  build.bat
  build.log
```

`build.log` 是排错入口。`compile_c` 在返回码非 0 或 DLL 不存在时，会把 `build.log`、stdout、stderr 一起放进 `RuntimeError`。先读 `build.log`，不要先猜 Python 层。

### ctypes 的指针与生命周期

`load_c_function` 返回的 `run` 函数上挂了 `run.lib = lib`。这一行看起来不起眼，实际是生命周期保险：如果 `lib` 被垃圾回收，后面再调用函数指针可能崩溃。把 `lib` 绑在闭包返回的函数上，调用方只要还持有 `run`，DLL 就不会被卸载。

另一个细节是输出数组由 Python 分配：

```python
outs = [np.zeros(s, dtype=np.float32) for s in output_shapes]
```

C 侧只负责往这些指针里写，不负责分配输出。这样调用方可以控制输出内存，也让 ABI 更简单：所有参数都是裸指针，没有结构体、没有回调、没有 Python 对象。

### arena 与对齐

生成的 C 用 `_aligned_malloc(arena_bytes, 64)`。64 字节对齐通常对应一个 cache line，方便后面做向量化和对齐读。`arena_bytes` 由内存规划器给出；本例中两个 2x3 的 `float32` 张量各 24 字节，按 64 字节对齐后各占 64 字节，总共 128 字节。

注意 `arena` 在函数返回前被 `_aligned_free`。如果输出值直接指向 arena，调用方拿到的就是悬空指针。所以代码在释放前把输出逐个拷到 `out0`、`out1`。这个拷贝是正确性要求，不是可选的优化。

## 进一步用法与变体

### 直接加载任意符号

`compile_graph_c` 默认生成 `mini_forward`。如果你自己写了 C 函数，可以绕过图，直接用 `load_c_function`。复用 0.3 的 `res` 和 0.4 的 `feed`：

```python
from mini_ai.runtime import load_c_function

fn = load_c_function(res.dll_path, n_inputs=1, n_outputs=1, symbol="mini_forward")
out = fn([feed["x"]], [(2, 3)])[0]
```

`n_inputs` 和 `n_outputs` 必须与 C 签名一致。参数数量对不上时，`ctypes` 不会帮你检查语义，只会按你声明的类型传指针，轻则返回错误码，重则访问越界。

### 自定义函数名

`generate_c(graph, function_name="mlp_forward")` 和 `compile_graph_c(..., name="mlp")` 控制的是两个不同层面：前者控制 C 函数名，后者控制文件名。`load_c_function` 的 `symbol` 要跟 C 函数名一致，不是文件名。

### 多输出

图有多个输出时，生成的签名是 `(x0, ..., out0, out1, ...)`。`run_c_graph` 按 `graph.outputs` 的顺序返回列表。调用方不需要知道输出名字，但需要知道顺序。

### 用 benchmark 看真实耗时

`runtime.benchmark` 返回 `median_ms`、`min_ms`、`max_ms`。默认 5 次 warmup、30 次计时。注意 `run_c_graph` 每次都会重新 `CDLL` 加载 DLL；要测纯执行，先用 `load_c_function` 拿到 `run`，再对 `run` 计时。

## 对比与取舍

| 路径 | 启动成本 | 单次执行 | 可调试性 | 适用场景 |
| --- | --- | --- | --- | --- |
| NumPy 解释器 | 低 | 中 | 高，能逐 op 打印 | 语义金标准、调试 |
| C DLL | 中，需要 `cl` | 低 | 中，能看 C 和汇编 | 固定 shape 的部署 |
| LLVM JIT | 中高，需要构建 IR | 低 | 中高，能看 LLVM IR | 研究 lowering 与优化 |
| PyTorch eager | 低 | 中 | 高 | 对照基线 |

本章选择 C DLL 作为第一个闭环，原因是它的中间产物少：C 源码可读、DLL 边界清楚、`ctypes` 调用直观。LLVM 路径的抽象层更多，适合在你看懂 ABI 和内存之后再进入。

## 踩雷集锦

1. **`PYTHONPATH` 指错层级。** 指向 `code/mini_ai` 会得到 `ModuleNotFoundError: No module named 'mini_ai'`；应指向 `code`。
2. **`cl` 不在 PATH。** 不要手动改 PATH 去猜，先确认 `find_vcvars64()` 能找到 `vcvars64.bat`，或者设置 `VCVARS64` 环境变量。
3. **DLL 被占用后无法覆盖。** 同一进程里已经 `CDLL` 加载了旧 DLL，再编译同名文件可能失败。换 `name`，或者结束占用进程。
4. **把 `run_c_graph` 当成会检查 shape。** 它只检查输入个数，不检查每个数组的 shape。形状不对时可能算出错误结果而不是报错。Ch 4 会用一个转置输入复现这一点。
5. **忘记输出拷贝。** 如果修改 codegen，让 `out0` 直接指向 arena，函数返回后内存被释放，调用方读到的是垃圾值。

## 进阶

- 在 `vcvars64` 初始化的命令行里用 `dumpbin /exports .scratch\mini-ai-ch0\ch0_demo.dll` 看导出符号，确认 `mini_forward` 的修饰名和调用约定。
- 在 `vcvars64` 环境里用 `cl /FAs` 生成汇编，观察 `/O2` 是否把逐元素循环向量化。Ch 19 会系统讲这件事。
- 修改 `generate_c` 的 `function_name`，用 `load_c_function(..., symbol=...)` 调不同名字的函数。
- 故意把 `arena_bytes` 改小，观察越界写是否立刻崩溃。不要在生产代码里这样做，只在隔离的构建目录里做实验。

## 动手练习

1. **跑通 demo。** 执行 `& $py compilers\mini_ai_compiler\code\examples\demo_e2e.py`，记录 C 和 LLVM 的 `max_abs_error`。期望两者都在 `1e-7` 量级或更小。
2. **换一个算子。** 把 `relu` 换成 `exp`，重新生成 C，观察 `expf` 出现在哪里，输出是否与 `evaluate_graph` 一致。
3. **制造缺失输入。** 用 `run_c_graph(g, {}, res.dll_path)`，确认报错是 `KeyError: 'x'`，并解释为什么不是返回码错误。
4. **制造不支持算子。** 构造 `softmax` 图，调用 `generate_c`，确认错误信息包含 `use sum/exp/div`。
5. **看构建日志。** 故意在生成的 C 里删一个分号，把文本读进字符串再调用 `compile_c`，从 `build.log` 里找到 `error C2143` 一类信息。

## 本章重点

- 编译器的最小闭环是：IR -> C 源码 -> DLL -> 指针调用 -> 数值对比。
- `compile_graph_c` 负责“生成 + 编译”，`run_c_graph` 负责“按图输入顺序 + 输出 shape 调用”。
- ABI 只有指针和返回码，Python 对象不跨边界。
- 临时值走 arena，输出在释放前拷贝。
- 失败要显式：缺输入报 `KeyError`，不支持算子报 `IRError`。

## 自我检核

- [ ] 我能说清 `code/`、`mini_ai/`、构建目录三者的关系。
- [ ] 我能独立运行 Ch 0 的图并得到 `max_abs_error: 0.0`。
- [ ] 我知道 `argtypes` 为什么是 `n_inputs + n_outputs` 个指针。
- [ ] 我能解释 `run.lib = lib` 的作用。
- [ ] 我能从 `build.log` 定位一个 C 语法错误。
- [ ] 我知道 `softmax` 在 C 后端会失败，并知道错误信息。

## 延伸阅读

- **MSVC `cl` 命令行参考**：读 Microsoft Learn 的 “Compiler options” 与 `/LD`、`/O2`、`/fp:fast` 条目；学每个 flag 对代码生成的影响；关联本章的 `build.bat` 和 Ch 19 的性能讨论。
- **Python `ctypes` 文档**：读 “Calling functions” 与 `argtypes`、`restype` 小节；学指针参数和数组内存布局；关联 `runtime.load_c_function` 的实现。
- **Microsoft “What is a DLL”**：读 DLL 的导出表与加载过程；学为什么需要 `__declspec(dllexport)`；关联 `MINI_EXPORT` 和 `dumpbin /exports`。
- **`code/mini_ai/codegen_c.py`**：读 `generate_c`、`compile_c`、`compile_graph_c`；学 C 代码生成与构建流程；关联本章的 C 片段和后续 Ch 16。
- **`code/mini_ai/runtime.py`**：读 `load_c_function`、`run_c_graph`、`benchmark`；学 ABI 封装和计时方法；关联 Ch 20 的 DLL 调用细节。

[下一章：我们要造什么](./01-what-we-are-building.md)
