# Ch 18 TorchInductor：从 FX 图到 Inductor IR，再到 C++ / Triton

> **目标**：把 `torch.compile(model, backend="inductor")` 拆开看——Dynamo 抓什么图、AOTAutograd 交给 Inductor 什么、Inductor 的 Scheduler 如何融合、CPU 上最终生成什么样的 C++，以及为什么 Windows 上第一步就可能失败。

## 为什么需要这个？

前面的章节里，TVM 是一条“显式”的编译链：你写 schedule、选 target、`tvm.build`、拿 module。TorchInductor 把这条链藏进了一个装饰器：

```python
cm = torch.compile(model, backend="inductor")
```

一行代码背后至少有四个组件在工作：

1. **TorchDynamo**：在 Python 字节码层面拦截执行，抓出 FX 图。
2. **AOTAutograd**：把前向图变成“前向 + 反向”的函数化图（推理时只留前向）。
3. **Inductor**：把 ATen 图降到自己的循环级 IR，做融合、调度、内存规划。
4. **Codegen + runtime**：CPU 生成 C++（编译成 `.pyd`），GPU 生成 Triton kernel。

这四层里，**只有 Inductor 是“编译器”意义上的后端**。Dynamo 是抓图器，AOTAutograd 是自动微分与函数化，Codegen 是它的输出端。理解这条分工，才能解释两类常见困惑：

- 为什么 `torch.compile` 有时候“没编译”——Dynamo 图断了，回退到 eager。
- 为什么 CPU 上生成的代码里看不到 matmul——Inductor 把 `mm/addmm/conv` 交给外部 BLAS，而不是自己生成。

本章所有能跑的实验都在 Windows CPU（torch 2.14.1+cpu）上完成；GPU 专属路径（Triton codegen、CUDA Graph）标 `guided`。

## 先建立直觉：三张图，两次下降

同一次 `torch.compile`，你会依次看到三张图：

```text
nn.Module（Python 代码）
   │ Dynamo：字节码 → FX Graph（ATen 算子级）
   ▼
FX Graph（forward）
   │ AOTAutograd：函数化、拆 forward/backward
   ▼
ATen Graph（仍然算子级，但已是纯函数）
   │ Inductor GraphLowering：逐节点 lower
   ▼
Inductor IR（循环级：Pointwise / Reduction / Scan + 内存缓冲）
   │ Scheduler：融合、重排、分区
   ▼
Kernel 代码（C++ / Triton）+ Python wrapper
```

前两次是“换表示”，最后一次是“换语言”。关键区别：

- **FX/ATen 图**里，一个节点是一个算子，比如 `aten.addmm`。
- **Inductor IR** 里，一个节点是一段**循环**，比如“对 32768 个元素做 `relu`，按 8 个一组向量化”。
- **Kernel 代码**里，循环变成 `for`、`#pragma omp`、`Vectorized<float>`。

从算子到循环，是 Inductor 真正做优化的地方。融合、向量化、并行、内存复用都发生在这一层。

## 核心概念一：Dynamo 只负责抓图，不负责优化

Dynamo 的产物是一张 FX 图，节点是 `torch.ops.aten.*`。它不做常量折叠，也不做融合；它只保证“这张图等价于刚才那段 Python 执行”。

三个必须知道的边界：

| 边界 | 表现 | 结果 |
|---|---|---|
| 数据依赖的控制流 | `if x.sum() > 0:` | 图断（graph break），分段编译 |
| Python 副作用 | 改全局变量、打印、`list.append` | 图断或副作用被图外执行 |
| 不支持的算子 | 自定义 C++ op、部分第三方库 | 图断，回退 eager |

图断不是错误，但会破坏融合。一个模型断成 5 段，就有 5 次 kernel launch 边界，跨段的 buffer 只能落回内存。用 `TORCH_LOGS="graph_breaks"` 可以看断点原因。

抓图之后，Dynamo 还会安装 **guards**：shape、dtype、stride、常量值、对象身份。guard 失效就重编译。这也是动态 shape 场景下“越跑越慢”的根源（Ch 23 展开）。

## 核心概念二：Inductor IR 是“循环级 IR”

Inductor 的 IR 不是另一套算子图，而是**带循环结构的计算描述**。核心类在 `torch/_inductor/ir.py`，调度侧把它们包装成 `SchedulerNode`。

一次 lowering 大致是：

1. `GraphLowering` 逐节点遍历 FX 图（`torch/_inductor/graph.py:386`，类注释原文是 “Lowers an FX graph to Inductor IR and drives backend code generation”）。
2. 每个 ATen 节点被 lower 成 `Pointwise`、`Reduction`、`Scan`、`ExternKernel` 等 IR。
3. lowering 结束后创建 `Scheduler`（`graph.py:2992`）。
4. Scheduler 把 IR 节点变成 `SchedulerNode`，做依赖分析、融合、分区。
5. `Scheduler.codegen()` 把每个分区交给对应后端（CPU: `CppScheduling`；GPU: `TritonScheduling`）。

这里有一个容易忽略的事实：**Inductor 的“算子”概念没有消失，只是变成了 `ExternKernel`**。`addmm`、`mm`、`bmm`、`conv` 这些有高度优化库实现的算子，默认不生成循环，而是生成一次外部调用。你在生成代码里看到 `extern_kernels.addmm(...)`，就是这条路径。

## 核心概念三：Scheduler 负责融合与分区，Codegen 只负责翻译

Scheduler 的类注释写得很直接（`torch/_inductor/scheduler.py:4242`）：

> A Scheduler is a graph of BaseSchedulerNodes. It is responsible for optimizations such as fusion, reorder, and graph partition.

三件事分工如下：

| 阶段 | 代码位置 | 做的事 |
|---|---|---|
| 依赖分析 | `scheduler.py` 的 `compute_dependencies` | 建 producer-consumer 边，判断能否融合 |
| 融合 | `scheduler.py:5353` 的 `fuse_nodes` | 把合法节点合成 `FusedSchedulerNode` |
| 分区 | `scheduler.py` 的 `codegen` 路径 | 按后端能力切 kernel，决定 launch 边界 |
| CPU codegen | `codegen/cpp.py:5114` 的 `CppScheduling` | 生成 SIMD + OpenMP 的 C++ |
| GPU codegen | `codegen/triton.py:8072` 的 `TritonScheduling` | 生成 Triton kernel |

融合的合法性判断不是“看起来能合就合”，而是要检查：

- 读写依赖是否形成环；
- 融合后是否引入重复计算；
- reduction 的迭代域是否兼容；
- 是否会破坏 extern kernel 的调用约定。

所以 Inductor 的融合是**保守的**：elementwise 链几乎总能合；reduction 要小心；matmul 基本不合（它要交给 BLAS）。

## 底层机制：一次调用到底发生了什么

把一次 `cm(x)` 拆成时间线：

```text
第 1 次调用
  Dynamo 捕获字节码 → FX Graph
  guards 安装
  AOTAutograd 函数化 → forward graph
  GraphLowering → Inductor IR
  Scheduler → 融合后的分区
  CppScheduling → C++ 源码
  async_compile → cl.exe 编译 → .pyd
  加载 .pyd → 执行 wrapper → 调 kernel

第 2..N 次调用
  命中 guards → 直接执行缓存的 wrapper
```

首次成本几乎全在“抓图 + 编译 C++”。稳态成本才是 kernel 本身。

生成物落在 `TORCHINDUCTOR_CACHE_DIR` 下。本机实测目录结构：

```text
inductor_cache/
  fxgraph/...        # FX 图缓存（Dynamo/AOTAutograd 的图）
  aotautograd/...    # AOTAutograd 缓存
  su/....py          # Python wrapper + 内嵌 C++ 源码
  5e/....cpp/.pyd    # 编译产物
  ki/....main.cpp    # cpp wrapper（若启用）
```

`su/....py` 是最值得读的文件：它同时包含 kernel 源码、调用顺序、buffer 分配和 stride 断言。

## 示例一：Windows CPU 上真跑（先失败，再成功）

测试模型：`Linear(256→512) → ReLU → Linear(512→128)`，batch 64，float32。

### 失败路径（不配置 MSVC）

直接运行：

```python
import torch

class MLP(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = torch.nn.Linear(256, 512)
        self.fc2 = torch.nn.Linear(512, 128)
    def forward(self, x):
        return torch.relu(self.fc2(torch.relu(self.fc1(x))))

m = MLP().eval()
x = torch.randn(64, 256)
with torch.no_grad():
    cm = torch.compile(m, backend="inductor")
    print(cm(x).shape)
```

本机真实报错（节选）：

```text
File "...\torch\_inductor\cpp_builder.py", line 185, in check_compiler_exist_windows
    raise exc.InvalidCxxCompiler(compiler) from e
torch._inductor.exc.InductorError: InvalidCxxCompiler: Compiler: cl is not found.
Set TORCHDYNAMO_VERBOSE=1 for the internal stack trace (please do this especially if you're reporting a bug to PyTorch).
```

这不是“没装 Visual Studio”，而是 **`cl.exe` 不在 `PATH`**。Inductor 的 CPU 后端要现场编译 C++，找不到编译器就直接失败。`cpp_builder.py:428` 的 `get_cpp_compiler()` 是决策点。

### 成功路径（先加载 vcvars64）

```powershell
cmd /c "call D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat && set PYTHONIOENCODING=utf-8 && D:\Learning-with-Claude\.scratch\aic-env\Scripts\python.exe your_script.py"
```

本机实测输出：

```text
first call sec: 36.832
maxerr: 0.0
shapes: torch.Size([64, 128])
20 calls sec: 0.009
```

解读：

- **36.8 s** 是首次编译（Dynamo + Inductor + cl.exe + 链接）。这个数字随机器、缓存状态波动很大，不是常数。
- **maxerr = 0.0** 不代表“编译器比 eager 更准”，而是两边最终都调用了同一个 BLAS `addmm`，ReLU 又是逐元素确定性运算。换成 TVM 或手写 kernel，误差通常在小 `1e-5` 量级。
- **20 次调用 9 ms** 是稳态：平均约 0.45 ms/次。对这么小的模型，eager 也差不多，编译不一定更快。

## 示例二：看它生成了什么

打开 `TORCH_LOGS`（注意 PowerShell 下 `set X=Y &&` 会把尾随空格写进变量）：

```powershell
cmd /c "call D:\VStudio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat && set PYTHONIOENCODING=utf-8 && set TORCH_LOGS=+inductor&& D:\Learning-with-Claude\.scratch\aic-env\Scripts\python.exe your_script.py"
```

本机真实日志（节选，行号来自日志前缀）：

```text
V... torch\_inductor\compile_fx.py:2852] [0/0] [__pre_grad_graphs] def forward(self, L_a_, L_b_):
V... torch\_inductor\compile_fx.py:2852] [0/0] [__pre_grad_graphs]     mul = l_a_ * 2.0
V... torch\_inductor\compile_fx.py:2852] [0/0] [__pre_grad_graphs]     add = mul + l_b_
V... torch\_inductor\compile_fx.py:2852] [0/0] [__pre_grad_graphs]     relu = torch.relu(add)
V... torch\_inductor\graph.py:3084] [0/0] Output code written to: ...\inductor_cache4\fl\cflw....py
```

生成的融合 kernel（真实文件内容，节选）：

```cpp
cpp_fused_add_mul_relu_0 = async_compile.cpp_pybinding(['const float*', 'const float*', 'float*'], r'''
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C" __declspec(dllexport) void  kernel(const float* in_ptr0,
                       const float* in_ptr1,
                       float* out_ptr0)
{
    std::atomic<int> inductor_cpu_integer_div_error{0};
    inductor_cpu_integer_div_error_flag = &inductor_cpu_integer_div_error;
    {
        for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(64LL); x0+=static_cast<int64_t>(8LL))
        {
            {
                if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(64LL)))
                {
                    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                    auto tmp4 = at::vec::Vectorized<float>::loadu(in_ptr1 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                    auto tmp1 = static_cast<float>(2.0);
                    auto tmp2 = at::vec::Vectorized<float>(tmp1);
                    auto tmp3 = tmp0 * tmp2;
                    auto tmp5 = tmp3 + tmp4;
                    auto tmp6 = at::vec::clamp_min(tmp5, decltype(tmp5)(0));
                    tmp6.store(out_ptr0 + static_cast<int64_t>(x0));
                }
            }
        }
    }
    inductor_cpu_integer_div_error_flag = nullptr;
    inductor_cpu_throw_if_integer_div_error(inductor_cpu_integer_div_error);
}
''')
```

这张图里 `mul / add / relu` 三个节点合成 **一个** kernel，这就是纵向融合（Ch 7）。三个细节：

- `Vectorized<float>::loadu(..., 8)`：一次处理 8 个 float，AVX2 宽度（本机 `torch.backends.cpu.get_cpu_capability()` 返回 `AVX2`）。
- `clamp_min(x, 0)` 就是 ReLU，没有额外函数调用。
- 小张量（64 个元素）没有 `#pragma omp parallel`；大张量才会并行。

再看 MLP 的 wrapper（同一个 `.py` 文件）：

```python
buf0 = empty_strided_cpu((64, 512), (512, 1), torch.float32)
#Topologically Sorted Source Nodes: [linear], Original ATen: [aten.t, aten.addmm]
extern_kernels.addmm(arg1_1, arg2_1, reinterpret_tensor(arg0_1, (256, 512), (1, 256), 0),
                     alpha=1, beta=1, out=buf0)
del arg0_1
del arg1_1
del arg2_1
buf1 = buf0; del buf0  # reuse
cpp_fused_relu_0(buf1)
```

三个关键信息：

1. **matmul 没生成循环**：`extern_kernels.addmm` 直接调用 BLAS。
2. **ReLU 被融合成单独 kernel**：它没有和 `addmm` 合成一个 epilogue。CPU 后端在这条路径上把 addmm 当黑盒，融合边界就在这里。
3. **buffer 复用**：`buf1 = buf0; del buf0  # reuse`，ReLU 原地写回，省一次分配（Ch 10）。

## 示例三：对照 TVM，谁负责哪一段

同一个矩阵乘加 ReLU，两条栈的分工完全不同：

| 环节 | TVM | TorchInductor |
|---|---|---|
| 输入 IR | Relax / TE / TIR | FX + ATen（Dynamo/AOTAutograd） |
| 融合决策 | 调度里显式 `compute_at`，或 Relax pass | Scheduler 自动 `fuse_nodes` |
| 调度 | 用户手写 / `meta_schedule` 搜索 | 启发式规则 + 少量 autotune |
| matmul 实现 | 可生成 TIR，也可 `extern` | 默认 `extern`（BLAS） |
| codegen | LLVM IR / C / CUDA | C++ / Triton |
| 首次成本 | `tvm.build` 秒级 | 首次数十秒（含 C++ 编译） |
| 动态 shape | 有限支持 | SymInt + guards + 重编译 |

一句话总结：

- **TVM 把“融合/调度/codegen”三件事都暴露给用户**，你能精确控制，但要自己写。
- **Inductor 把“融合/分区”自动化**，把“调度”压缩成启发式，把 matmul 交给库。
- 所以 Inductor 在常见 transformer 图上开箱即用；TVM 在需要特殊 schedule 的场景更强。

## 对比与取舍

| 维度 | 选 Inductor | 选 TVM |
|---|---|---|
| 目标 | PyTorch 模型直接加速 | 自定义 kernel / 特殊硬件 |
| 上手成本 | 一行 `torch.compile` | 学 Relax/TIR/schedule |
| 融合能力 | 自动，保守 | 手动，精确 |
| matmul | 依赖 BLAS | 可生成、可调优 |
| 调试 | 看生成代码 + TORCH_LOGS | 看 TIR + `inspect_source` |
| 动态 shape | 原生 SymInt | 需要专门处理 |
| 生产部署 | 需要 Python + torch | 可导出独立 module |

**不要用 Inductor 替代 TVM 的教学价值**：Inductor 帮你做了选择，但你看不到“为什么这样切 tile”。学完 TVM 的 schedule，再回来看 Inductor 的 `CppKernel`，会发现它就是在自动生成你手写过的那类循环。

## 踩雷集锦

1. **`cl is not found` 就以为要装 CUDA**：CPU 后端要的是 MSVC 的 `cl.exe`，不是 GPU 工具链。
2. **在 PowerShell 里直接 `set TORCH_LOGS=inductor && ...`**：尾随空格会进入变量，报 `Invalid log settings: inductor `。用 `set TORCH_LOGS=inductor&&` 或先单独设置。
3. **把 `maxerr=0.0` 当成普遍结论**：这里只是两边都走同一个 BLAS；换后端后误差会回到 `1e-5` 量级。
4. **以为 `torch.compile` 一定更快**：小模型上 wrapper 开销和编译成本可能占主导，实测 Inductor 稳态不一定赢 eager。
5. **忽略图断**：`print`、`.item()`、数据依赖分支都会断图；断图后融合消失，性能回到 eager。
6. **把 `inductor_cache` 当永久产物**：它和 torch 版本、编译器版本、环境变量绑定，升级后可能全部失效。
7. **在 notebook 里测首次成本**：内核启动、缓存、JIT 预热都会污染数字；至少分开报告首次与稳态。
8. **以为 CPU 生成的是“机器码文件”**：生成的是 C++ 源码，再由 `cl.exe` 编译成 `.pyd`；中间任何一步失败都会回退或报错。

## 进阶：再往深一层

- **`cpp_wrapper=True`**：生成 C++ 的 host wrapper，减少 Python 侧调用开销；会在缓存里多出 `*.main.cpp`。本机缓存目录里已经能看到这类文件。
- **`mode="max-autotune"`**：让 Inductor 对 matmul/conv 尝试模板和 Triton 配置；CPU 上收益有限，GPU 上才明显（`guided`）。
- **`fullgraph=True`**：禁止图断，遇到不支持的结构直接报错。适合确认“我的模型到底能不能整图编译”。
- **`TORCH_LOGS="output_code"`**：直接把生成代码打到终端，不用去缓存目录翻文件。
- **`torch._inductor.config.triton.cudagraphs=True`**：CUDA Graph 捕获，消除 launch 开销；本机无 CUDA，`guided`。

## 动手练习

### 练习 1：制造一次图断

把模型改成 `if x.sum().item() > 0: return x + 1` 否则 `return x - 1`，用 `backend="eager"` 运行，并开启 `TORCH_LOGS="graph_breaks"`。回答：断在哪里？断成了几段？

<details>
<summary>参考思路</summary>

`x.sum().item()` 把 tensor 变成 Python 标量，Dynamo 无法在图中表达数据依赖分支，于是在 `if` 处断图。日志会给出行号和原因。用 `backend="eager"` 可以避开 C++ 编译器，专注看 Dynamo 行为。

如果把条件改成 `if x.shape[0] > 1`（只依赖静态 shape），通常不会断图。
</details>

### 练习 2：读生成代码，找向量宽度

运行本章的 `relu(a * 2 + b)` 例子，打开缓存里的 `su/*.py`，回答：向量宽度是多少？为什么是这个宽度？如果把输入改成 1000 个元素，循环边界和 tail 处理会怎么变？

<details>
<summary>参考思路</summary>

本机是 AVX2，宽度 8（`Vectorized<float>::loadu(..., 8)`）。1000 不是 8 的倍数，Inductor 会生成主循环 + tail 循环（或 masked load），你会在源码里看到两个循环或一个 `if` 分支。对比 Ch 15 的 tail handling。
</details>

### 练习 3：冷缓存 vs 热缓存

设置 `TORCHINDUCTOR_CACHE_DIR` 到一个新目录，记录首次编译时间；再用同一目录跑第二次，记录时间。解释差异来自哪几个阶段。

<details>
<summary>参考思路</summary>

首次包含 Dynamo 抓图、Inductor lowering、C++ 编译、链接、加载；第二次命中 `fxgraph` 和编译产物缓存，主要剩 Python 导入和 guards 检查。本机首次约几十秒，热缓存通常降到几秒以内，但具体数字随模型和机器波动。
</details>

### 练习 4：让 matmul 也生成循环（观察边界）

把 `nn.Linear` 换成手写 `torch.matmul(x, w.t())`，矩阵规模设成 8×8，运行并看生成代码。回答：Inductor 这次生成了循环还是仍然 `extern`？

<details>
<summary>参考思路</summary>

小矩阵上 Inductor 仍可能选择 `extern_kernels.mm`，也可能生成模板；这取决于启发式和后端。重点不是猜结果，而是学会在 `su/*.py` 里找 `extern_kernels.` 或 `for(int64_t` 来判断。把规模改大改小各试一次，记录变化。
</details>

### 练习 5：失败路径复现

在不加载 `vcvars64.bat` 的新终端里运行同一个脚本，保存完整报错。然后只把 `cl.exe` 所在目录加进 `PATH`（不设其他环境变量），看是否够用。

<details>
<summary>参考思路</summary>

通常不够：`cl.exe` 还需要 `INCLUDE`（Windows SDK / MSVC 头文件）和 `LIB`（库路径）。`vcvars64.bat` 一次性设置这些变量。这个练习的目的是让你分清“找到编译器”和“编译器能用”是两件事。
</details>

## 本章重点整理

- `torch.compile` = Dynamo（抓图）+ AOTAutograd（函数化）+ Inductor（lowering/融合/codegen）+ runtime。
- Inductor IR 是循环级 IR；融合与分区由 `Scheduler` 负责，codegen 由 `CppScheduling` / `TritonScheduling` 负责。
- CPU 后端默认把 `mm/addmm/conv` 交给外部库（BLAS / oneDNN），自己只生成 elementwise/reduction kernel。
- Windows 上 Inductor 需要 MSVC 的 `cl.exe` 及完整环境变量；否则报 `InvalidCxxCompiler`。
- 首次编译成本与稳态成本必须分开报告；小模型上编译不一定带来加速。
- `TORCH_LOGS=+inductor` 能看到 pre/post grad 图和生成代码路径。
- 与 TVM 的分工：TVM 暴露融合/调度/codegen，Inductor 自动化前三者中的前两项，第三项交给 C++/Triton。

## 自我检核

- [ ] 我能说出 Dynamo、AOTAutograd、Inductor、Codegen 各自负责什么
- [ ] 我能解释为什么生成代码里 matmul 是 `extern_kernels.addmm`
- [ ] 我知道 `InvalidCxxCompiler` 的根因和修复方式
- [ ] 我能从 `su/*.py` 里找到向量宽度、OpenMP 并行和 buffer 复用
- [ ] 我能解释 `maxerr=0.0` 为什么不代表“编译器更准”
- [ ] 我能区分首次编译时间与稳态 kernel 时间
- [ ] 我能画出 Inductor 从 FX 到 kernel 的五个阶段
- [ ] 我能说出至少两个会导致图断的 Python 写法

## 延伸阅读

- **PyTorch 官方 torch.compile 文档**（https://pytorch.org/docs/stable/torch.compiler.html）：读 “TorchDynamo”“AOTAutograd”“Inductor” 三节。与本章关联：四层分工的官方定义。
- **PyTorch Dev Discuss: TorchInductor**（https://dev-discuss.pytorch.org/）：搜索 Inductor 设计贴，重点看 fusion 与 codegen 的取舍。与本章关联：Scheduler 的设计动机。
- **源码 `torch/_inductor/scheduler.py`**（本机路径 `.scratch\aic-env\Lib\site-packages\torch\_inductor\scheduler.py`）：读 `Scheduler` 类注释和 `fuse_nodes`。与本章关联：融合的真实实现。
- **源码 `torch/_inductor/codegen/cpp.py`**：读 `CppScheduling`、`CppKernel`、`CppVecKernel`。与本章关联：CPU codegen 的循环与向量化。
- **TVM 论文 (OSDI 2018)**（https://www.usenix.org/conference/osdi18/presentation/chen）：对比“计算与调度分离”与 Inductor 的自动调度。与本章关联：两种设计哲学的差异。

---

→ [Ch 19 Triton：block-level 编程模型](./19-triton.md)