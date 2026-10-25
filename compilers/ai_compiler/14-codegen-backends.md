# Ch 14 后端代码生成：LLVM IR、C 与 CUDA

> **目标**：看懂 `target="llvm"`、`target="c"`、`target="cuda"` 三条 codegen 路径的产物；分清 host module 与 device module；亲手导出模块，理解「codegen 成功 ≠ runtime 能加载」。

## 为什么需要这个？

前面几章都在改 IR：图优化改图，schedule 改循环。到了 codegen，IR 要第一次变成「机器能执行的东西」。这一步最容易产生三个误解：

1. **以为 `tvm.build` 只是「翻译」**：它还要选择 target、组装 host/device 模块、调用后端编译器。
2. **以为 `target="cuda"` 成功就等于能跑 GPU**：codegen 成功、编译成功、runtime 加载成功是三件事。
3. **以为 `target="c"` 的产物可以直接调用**：C 模块只是源码，不是可执行函数。

这一章用本机真实输出把这三件事拆开。先说明一个和任务 brief 不一致的事实：

> **本机并非 brief 假设的纯 CPU-only**。实测有 NVIDIA GeForce MX250（compute capability 6.1，sm_61）、驱动 582.78、`tvm.support.libinfo()["USE_CUDA"] == "ON"`、`tvm.cuda(0).exist == True`。但本机没有 `nvcc`，Python 环境也没有 `cuda-bindings`，所以 CUDA 的**编译链**仍然不完整。下面会贴真实报错，不伪造 GPU 结果。

## 先建立直觉：target 是合同，build 是施工

把 `tvm.build` 想成一次施工：

- **target 是合同**：告诉 TVM 目标指令集、架构、ABI、可用特性。
- **TIR 是图纸**：循环、buffer、block 都已经确定。
- **codegen 是施工队**：把 TIR 翻译成 LLVM IR、C 或 CUDA C。
- **runtime module 是交付物**：host 代码 + device 代码 + 常量 + 元数据，能被 TVM runtime 加载。

合同换了，施工队和交付物都换：

| target | codegen 产物 | 谁来编译 | 本机状态 |
|---|---|---|---|
| `llvm` | LLVM IR | TVM 内置 LLVM JIT | 可用 |
| `c` | C 源码 | 外部 C 编译器 / 你 | 可生成，不能直接调用 |
| `cuda` | CUDA C / PTX | NVRTC 或 nvcc | 缺 `cuda-bindings`，失败 |

## 核心概念一：target 字符串的解剖

`tvm.target.Target("cuda")` 在 0.27 里解析成：

```text
{"kind":"cuda","tag":"","keys":["cuda","gpu"],"max_num_threads":1024,"thread_warp_size":32,"arch":"sm_61"}
```

几个字段：

- `kind`：后端类型（`llvm`、`c`、`cuda`）。
- `keys`：调度/算子选择用的标签（`cpu`、`gpu`、`cuda`）。
- `arch`：从驱动/设备查询到的架构；本机 MX250 是 `sm_61`。
- `max_num_threads`、`thread_warp_size`：CUDA 的线程模型参数。

target 还可以带选项，但 0.27 **不再接受 CLI 字符串**。下面两行在本机会直接报错：

```python
tvm.target.Target("llvm -mtriple=x86_64-pc-windows-msvc")
tvm.target.Target("cuda -arch=sm_61")
```

真实报错（节选，原文为单行）：

```text
ValueError: Cannot parse target string "llvm -mtriple=x86_64-pc-windows-msvc".
CLI target string form (e.g. "llvm -mcpu=xxx") is no longer supported.
Please use JSON dict form (e.g. {"kind": "llvm", "mcpu": "xxx"}) instead.
```

正确写法是 JSON dict：

```python
tvm.target.Target({"kind": "llvm", "mtriple": "x86_64-pc-windows-msvc"})
tvm.target.Target({"kind": "cuda", "arch": "sm_61"})
```

`mtriple` 影响 LLVM 生成的目标文件格式和 ABI；`arch` 影响 CUDA 生成的指令集。**target 写错，codegen 可能仍然“成功”，但产物加载不了或跑错**。

## 核心概念二：host module 与 device module

`tvm.build` 返回的是一个 `Module`，它可能是「一个 host 模块 + 若干 imported device 模块」：

```python
f = tvm.build(sch.mod, target="llvm")
print(f.imports)          # ()
print(f.entry_name)       # main
print(f.inspect_source()) # LLVM IR
```

对纯 CPU target，`imports` 通常是空元组；对 CUDA target，host 模块会 import 一个 device 模块，device 模块里才是 CUDA C/PTX。`inspect_source()` 默认看的是**顶层 host 模块**；要看 device 源码，得对 imported module 调用 `inspect_source("cuda")`（前提是它真的存在）。

这就是为什么「`target="cuda"` 但 `inspect_source()` 显示 x86 LLVM IR」不是 bug：你看到的是 host 侧，device 侧可能根本没生成。

## 核心概念三：codegen 成功 ≠ runtime 能加载

三种「成功」要分开：

| 阶段 | 成功意味着什么 | 失败长什么样 |
|---|---|---|
| codegen | TIR 被翻译成目标语言 | `ScheduleError` / `TVMError` |
| compile | 目标语言被编译成机器码/ cubin | 找不到 nvcc / NVRTC / clang |
| load | runtime 把模块映射进进程，暴露函数 | `Module has no function 'main'` / device mismatch |

本机三个真实例子：

1. `target="c"`：`tvm.build` 成功，`inspect_source()` 有 C 代码；但 `f(...)` 报 `Module has no function 'main'`，因为它还没被编译成可调用模块。
2. `target="cuda"` + 串行 schedule：`tvm.build` “成功”，但 `imports == ()`，`inspect_source()` 是 x86 LLVM IR；用 CPU tensor 调用会报 `expected cuda`。
3. `target="cuda"` + 真正的 GPU schedule：`tvm.build` 在 NVRTC 编译阶段失败，因为缺 `cuda-bindings`。

## 底层机制：tvm.build 做了什么

大致四步：

```text
TIR
 │ 1. Lower：展开 schedule，插入边界检查、分配、初始化
 ▼
Lowered TIR
 │ 2. Codegen：按 target 生成 LLVM IR / C / CUDA C
 ▼
Source / IR module
 │ 3. Compile：LLVM JIT、NVRTC、nvcc、clang 等
 ▼
Runtime module（host + device）
 │ 4. Load：映射进进程，暴露 entry function
 ▼
f(ta, tb, tc)
```

关键点：**第 3 步不一定在 `tvm.build` 里完成**。C target 只生成源码；CUDA target 可能把 device 编译延迟到 load/export；LLVM target 通常当场 JIT。所以「build 返回了模块」不代表「所有东西都编译好了」。

## 示例一：LLVM IR

用一个 64 元素的 vector add，`split` + `vectorize(8)`：

```python
import numpy as np
import tvm
from tvm import te, s_tir

N = 64
A = te.placeholder((N,), name="A", dtype="float32")
B = te.placeholder((N,), name="B", dtype="float32")
C = te.compute((N,), lambda i: A[i] + B[i], name="C")

sch = s_tir.Schedule(te.create_prim_func([A, B, C]))
i = sch.get_loops(sch.get_sblock("C"))[0]
io, ii = sch.split(i, factors=[None, 8])
sch.vectorize(ii)

f = tvm.build(sch.mod, target="llvm")
print(f.inspect_source())
```

`inspect_source()` 有 8 万多字符，大部分是 TVM 的运行时参数检查。真正的 kernel 是 `main_compute_`（节选）：

```text
define internal fastcc void @main_compute_(ptr noalias readonly align 64 captures(none) %A, ptr noalias readonly align 64 captures(none) %B, ptr noalias writeonly align 64 captures(none) %C) unnamed_addr #3 !dbg !39 {
entry:
  call void @llvm.assume(i1 true) [ "align"(ptr %A, i64 64) ], !dbg !47
  call void @llvm.assume(i1 true) [ "align"(ptr %B, i64 64) ], !dbg !47
  call void @llvm.assume(i1 true) [ "align"(ptr %C, i64 64) ], !dbg !47
  %0 = load <8 x float>, ptr %B, align 64, !dbg !47, !tbaa !52
  %1 = load <8 x float>, ptr %A, align 64, !dbg !47, !tbaa !55
  %2 = fadd <8 x float> %0, %1, !dbg !47
  store <8 x float> %2, ptr %C, align 64, !dbg !47, !tbaa !57
}
```

这段 IR 说明三件事：

- `vectorize(8)` 真的生成了 `<8 x float>`，不是标量循环。
- `align 64` 是 TVM 对 buffer 对齐的假设；如果实际指针不满足 64 字节对齐，行为可能出错（Ch 15 会讲对齐）。
- `main_compute_` 是内部函数；对外入口是 `__tvm_ffi_main`，它做参数检查后调用 `main_compute_`。

LLVM target 的模块由 TVM 内置的 LLVM JIT 直接加载，所以 `f(ta, tb, tc)` 可以立刻调用。这是本机唯一一条「build 完就能跑」的路径。

## 示例二：C 代码

同一个 `sch.mod`，换 `target="c"`：

```python
fc = tvm.build(sch.mod, target="c")
print(fc.inspect_source())
```

真实 C 源码的开头：

```c
// tvm target: {"kind":"c","tag":"","keys":["cpu"]}
#define TVM_EXPORTS
#include "tvm/ffi/extra/c_env_api.h"
#include "tvm/runtime/base.h"
#include "tvm/runtime/c_backend_api.h"
#include "tvm/ffi/c_api.h"
#include <math.h>
#include <stdbool.h>
void* __tvm_ffi__library_ctx = NULL;
```

真正计算的部分（长行已为可读性换行）：

```c
float* A = ((float*)((float*)(((DLTensor*)A_handle)[0].data)));
float* B = ((float*)((float*)(((DLTensor*)B_handle)[0].data)));
float* C = ((float*)((float*)(((DLTensor*)C_handle)[0].data)));
for (int32_t i_0 = 0; i_0 < 8; ++i_0) {
  int32_t cse_v1 = (i_0 * 8);
  int32_t8 v_ = int32_t8((cse_v1)+(1*0), (cse_v1)+(1*1), (cse_v1)+(1*2), (cse_v1)+(1*3),
                        (cse_v1)+(1*4), (cse_v1)+(1*5), (cse_v1)+(1*6), (cse_v1)+(1*7));
  *(float8*)(C + cse_v1) = ((float8(A[v_.s0],A[v_.s1],A[v_.s2],A[v_.s3],
                                  A[v_.s4],A[v_.s5],A[v_.s6],A[v_.s7])) +
                            (float8(B[v_.s0],B[v_.s1],B[v_.s2],B[v_.s3],
                                    B[v_.s4],B[v_.s5],B[v_.s6],B[v_.s7])));
}
return 0;
```

注意：

- C 代码里有 `int32_t8`、`float8` 这种 TVM 自定义的向量类型；它们不是标准 C，需要 TVM 的头文件/编译器支持。
- C target 的产物是**源码**，不是可执行文件；`fc(...)` 直接调用会报：

```text
AttributeError: Module has no function 'main'
```

- 要用它，必须把 C 源码交给编译器（clang/gcc/MSVC）编译成共享库，再由 TVM runtime 加载；或者用 `export_library` 打包。Ch 17 会讲 runtime 加载。

## 示例三：CUDA 的真实报错

### 先看硬件与 target

```python
import tvm
print(tvm.target.Target("cuda"))
print(tvm.cuda(0).device_name, tvm.cuda(0).compute_version, tvm.cuda(0).exist)
print(tvm.support.libinfo()["USE_CUDA"])
```

真实输出：

```text
{"kind":"cuda","tag":"","keys":["cuda","gpu"],"max_num_threads":1024,"thread_warp_size":32,"arch":"sm_61"}
NVIDIA GeForce MX250 6.1 True
ON
```

所以「设备存在」和「驱动可用」都没问题。问题在编译链。

### 陷阱：串行 schedule + target="cuda"

如果直接对 Ch 11 的串行 matmul 调 `tvm.build(..., target="cuda")`，它会“成功”：

```python
f = tvm.build(sch.mod, target="cuda")
print(f.imports)          # ()
print(f.inspect_source()) # x86 LLVM IR，不是 CUDA C
```

然后你用 CPU tensor 调用：

```text
ValueError: Mismatched A.device_type on argument #0 when calling:
  `main(A: Tensor([64, 64], float32), B: Tensor([64, 64], float32), C: Tensor([64, 64], float32))`,
  expected cuda
```

原因：**target 只是合同，schedule 才决定有没有 device code**。串行 schedule 没有 `blockIdx/threadIdx` 绑定，CUDA codegen 没有 kernel 可生成，于是只生成了 host 模块；但模块仍然按 CUDA 设备类型注册，所以 CPU tensor 被拒绝。这是一个非常容易误判的「假成功」。

### 真正的 GPU schedule

要让 CUDA codegen 生成 device kernel，需要把循环 bind 到 GPU 线程：

```python
sch = s_tir.Schedule(te.create_prim_func([A, B, C]))
i, j, kk = sch.get_loops(sch.get_sblock("C"))
bi, ti = sch.split(i, factors=[None, 16])
bj, tj = sch.split(j, factors=[None, 16])
sch.reorder(bi, bj, ti, tj, kk)
sch.bind(bi, "blockIdx.x")
sch.bind(bj, "blockIdx.y")
sch.bind(ti, "threadIdx.y")
sch.bind(tj, "threadIdx.x")
```

TIR 里会出现线程绑定：

```text
for i_0 in T.thread_binding(4, thread="blockIdx.x"):
    for j_0 in T.thread_binding(4, thread="blockIdx.y"):
        for i_1 in T.thread_binding(16, thread="threadIdx.y"):
            for j_1 in T.thread_binding(16, thread="threadIdx.x"):
                for k in range(64):
                    with T.sblock("C"):
                        ...
```

再 `tvm.build(sch.mod, target="cuda")`，本机真实报错：

```text
RuntimeError:
Failed to compile CUDA with NVRTC because the `cuda-bindings` package is not available.
Please install it with: pip install cuda-bindings
See: https://nvidia.github.io/cuda-python/
```

源码依据：`tvm/support/nvcc.py:342-350`，`from cuda.bindings import nvrtc` 失败后抛出这个 `RuntimeError`。本机没有 `cuda-bindings`，也没有 `nvcc`（`where nvcc` 找不到），所以 CUDA 端到端标 `guided`。

这不是「TVM 不支持 CUDA」：`USE_CUDA=ON`，target 也解析出了 `sm_61`；缺的是**把 CUDA C 编译成 cubin 的工具链**。装 `cuda-bindings` 后，这条 NVRTC 路径才有可能跑通；CUDA Toolkit 的 nvcc 是另一条编译路径，取决于 TVM 的构建配置。

## 示例四：export_library

`export_library` 是「打包/链接」，不是「codegen」。它把 host 模块、device 模块、常量写成一个共享库或 tar 包。

### LLVM 模块导出 .dll

```python
f = tvm.build(sch.mod, target="llvm")
f.export_library("matmul.dll")
```

本机真实报错：

```text
RuntimeError: Can not find the LLVM clang for Windows clang.exe).Make sure it's installed and the installation directory is in the %PATH% environment variable. Prebuilt binaries can be found at: https://llvm.org/
```

TVM 在 Windows 上找的是 `clang.exe`，不是 MSVC 的 `cl`。本机 PATH 里没有 clang，所以共享库链接失败。

### LLVM 模块导出 .tar

```python
f.export_library("matmul.tar")
```

本机成功，文件 4168 字节。`.tar` 是 TVM 的模块包，不是可执行共享库；它可以被 TVM runtime 重新加载，但不能直接 `LoadLibrary`。

### C 模块导出

```python
fc = tvm.build(sch.mod, target="c")
fc.export_library("add.dll")
```

本机真实报错：

```text
AssertionError: Target triple should not be empty
```

C target 没有默认 triple，导出共享库时需要目标平台信息；`fc.export_library("add.tar")` 则成功（1542 字节），因为 tar 只打包源码/元数据。

这三个结果说明：**导出成功不等于可执行，导出失败也不等于 codegen 失败**。`export_library` 的成败取决于「目标格式 + 本机编译器/链接器」，而不是 TIR 本身。

## 对比与取舍

| 维度 | LLVM | C | CUDA |
|---|---|---|---|
| 产物 | LLVM IR → JIT 机器码 | C 源码 | CUDA C / PTX / cubin |
| 本机可 build | 是 | 是 | 串行 schedule“是”，GPU schedule 否 |
| 本机可直接调用 | 是 | 否（需编译） | 否（缺 NVRTC） |
| 导出共享库 | 需 clang.exe，本机失败 | 需 triple/编译器，本机失败 | 需 nvcc/NVRTC |
| 导出 tar | 成功 | 成功 | 未验证 |
| 典型用途 | CPU 推理、调试 IR | 嵌入式、交叉编译、教学 | GPU 推理/训练 |

选择建议：

- **先看 target 是否真的可用**：不只看 `tvm.cuda(0).exist`，还要看 `USE_CUDA`、NVRTC/nvcc、`cuda-bindings`。
- **调试 codegen 先看 LLVM IR**：它最接近机器，且不需要外部编译器。
- **C 适合「拿到源码再集成」**：比如嵌入到已有构建系统，或交叉编译到没有 TVM runtime 的平台。
- **CUDA 的坑在工具链**：codegen、compile、load 三段都可能断；不要用「build 成功」推断「GPU 能跑」。

## 踩雷集锦

1. **`target="cuda"` 串行 schedule “成功”**：`imports == ()`，产物是 host LLVM IR，CPU tensor 还会报 `expected cuda`。看到 build 成功先查 `imports` 和 `inspect_source()`。
2. **把 `tvm.cuda(0).exist` 当成「能编译 CUDA」**：它只说明驱动/设备可见；编译还需要 NVRTC/nvcc。
3. **直接调用 C 模块**：`AttributeError: Module has no function 'main'`；C 模块是源码，不是可调用函数。
4. **以为 `export_library(".dll")` 一定成功**：Windows 上要找 `clang.exe`；Linux 上要找 `gcc`/`clang`；本机都没有。
5. **把 `.tar` 当成共享库**：`.tar` 是 TVM 模块包，不能直接给系统加载器。
6. **用 CLI 形式写 target 选项**：0.27 拒绝 `"cuda -arch=sm_61"` / `"llvm -mtriple=..."`，报 `ValueError: ... CLI target string form ... no longer supported`；改用 `{"kind":"cuda","arch":"sm_61"}`。跨设备部署也要显式 arch。
7. **在 host 模块上找 CUDA 源码**：`inspect_source()` 默认看顶层；device 源码在 imported module 里。
8. **忽略 64 字节对齐**：LLVM IR 里的 `align 64` 是假设；传非对齐 buffer 可能崩或算错。

## 进阶：再往深一层

- **读 `nvcc.py`**：`tvm/support/nvcc.py:342-350` 是 NVRTC 的入口；看它如何选择 `target_format`（cubin/fatbin/ptx）以及如何处理 `cuda-bindings` 缺失。
- **读 `tvm_ffi/module.py:301`**：`inspect_source(fmt="")` 如何根据模块类型选择默认格式；这解释了为什么 CUDA host 模块显示 LLVM IR。
- **看 `imports` 树**：`f.imports` 和 `f.imports_` 能看到 imported device 模块；Ch 17 会讲 runtime 如何遍历这棵树。
- **试 `target={"kind":"c","mtriple":"x86_64-pc-windows-msvc"}`**：给 C target 一个 triple，再试 `export_library`；观察错误是否从 `Target triple should not be empty` 变成编译器缺失。
- **试 `target={"kind":"llvm","mtriple":"..."}`**：对比不同 triple 生成的 LLVM IR 头部；这是交叉编译的入口。

## 动手练习

### 练习 1：三条路径的源码对照

**任务**：对同一个 vector add，分别 build `llvm`、`c`、`cuda`（串行 schedule），打印 `inspect_source()` 的前 10 行和 `imports`，填一张三列表。

**测试用例**：LLVM 显示 `target triple = "x86_64-pc-windows-msvc"`；C 显示 `// tvm target: {"kind":"c",...}`；CUDA 显示 x86 LLVM IR 且 `imports == ()`。

**步骤**：

1. 建 N=64 的 vector add。
2. 三个 target 各 build 一次。
3. 记录 `type(f)`、`f.imports`、源码头。
4. 解释 CUDA 为什么看起来像 LLVM。

<details>
<summary>参考解答</summary>

串行 schedule 没有 GPU 线程绑定，CUDA codegen 没有 device kernel 可生成，只生成了 host 模块；`inspect_source()` 默认看 host，所以是 LLVM IR。`imports == ()` 说明没有 device 模块。

</details>

**常见卡点**：不要用 Ch 11 的 `parallel` 代替 `bind`；`parallel` 是 CPU 线程池，不是 CUDA 的 block/thread。

### 练习 2：给 C target 一个 triple

**任务**：把 `target="c"` 改成 `target={"kind":"c","mtriple":"x86_64-pc-windows-msvc"}`，重新 build 并尝试 `export_library("add.dll")`。

**测试用例**：记录错误是否仍是 `Target triple should not be empty`；如果是编译器缺失，记录完整报错。

**步骤**：

1. 复制示例二的 C build。
2. 改 target 字符串。
3. 打印 `fc.inspect_source()` 头部，确认 target 元数据变化。
4. 尝试导出，记录结果。

<details>
<summary>参考解答</summary>

本机没有验证过带 triple 的导出成功；预期是 triple 错误消失，转而报找不到 C 编译器/链接器。无论结果如何，都要记录原始报错，不要把它写成「成功」。

</details>

**常见卡点**：不要写成 `"c -mtriple=..."`；0.27 会拒绝 CLI 形式，必须用 JSON dict。

### 练习 3：让 CUDA 真正生成 device kernel

**任务**：用 `bind` 写一个 64×64 matmul 的 GPU schedule，build `target="cuda"`，记录报错；如果本机装了 `cuda-bindings`，再尝试运行。

**测试用例**：TIR 中出现 `T.thread_binding(..., thread="blockIdx.x")`；未装 `cuda-bindings` 时，报错必须是本章贴的那条 NVRTC 错误。

**步骤**：

1. 复制示例三的 GPU schedule。
2. `print(sch.mod.script())`，确认线程绑定。
3. `tvm.build(sch.mod, target="cuda")`，捕获异常。
4. 把异常原文记下来，和 `nvcc.py:342-350` 对照。

<details>
<summary>参考解答</summary>

本机真实报错：

```text
Failed to compile CUDA with NVRTC because the `cuda-bindings` package is not available.
Please install it with: pip install cuda-bindings
```

这说明 codegen 已经走到 NVRTC 编译阶段；缺的是 Python 侧的 CUDA 绑定，不是 TIR 或 target。

</details>

**常见卡点**：`bind` 的线程维度必须和 `split` 的循环对应；`blockIdx.x` 绑外层，`threadIdx.x` 绑内层。

### 练习 4：解包 tar

**任务**：导出 LLVM 模块为 `.tar`，用 `tar -tf` 列出内容，解释每个文件的作用。

**测试用例**：`tar -tf matmul.tar` 能列出文件；不应包含 `.dll`/`.so`。

**步骤**：

1. `f.export_library("matmul.tar")`。
2. `tar -tf matmul.tar`。
3. 记录文件列表。
4. 解释为什么它不能直接给系统加载器。

<details>
<summary>参考解答</summary>

本机实测：LLVM 的 `matmul.tar` 里只有一个 `lib0.o`；C 的 `add.tar` 里只有一个 `lib0.c`。它是 TVM runtime 的打包格式，不是操作系统的共享库格式。加载要用 `tvm.runtime.load_module("matmul.tar")`，不是 `LoadLibrary`。

</details>

**常见卡点**：Windows 自带 `tar.exe` 可以解 `.tar`；如果 PATH 没有，用 7-Zip 也行。

## 本章重点整理

- target 是合同：`llvm`、`c`、`cuda` 走不同 codegen；target 选项影响 triple/arch。
- `tvm.build` 返回 host 模块，device 代码在 `imports` 里；`inspect_source()` 默认看 host。
- LLVM 模块可直接 JIT 调用；C 模块只是源码；CUDA 需要 NVRTC/nvcc。
- 本机有 MX250、`USE_CUDA=ON`，但缺 `cuda-bindings`/nvcc，CUDA 端到端标 `guided`。
- 串行 schedule + `target="cuda"` 会“成功”但只生成 host 代码；这是假成功。
- `export_library` 是打包/链接，不是 codegen；本机 `.dll` 因缺 clang 失败，`.tar` 成功。
- codegen 成功 ≠ compile 成功 ≠ runtime 加载成功；三段要分别验证。

## 自我检核

- [ ] 我能解释 `target`、`tvm.build`、`imports`、`inspect_source` 的分工
- [ ] 我能说出 LLVM/C/CUDA 三条路径的产物和本机可用性
- [ ] 我能解释为什么串行 schedule 的 CUDA build 看起来像 LLVM
- [ ] 我能复现 NVRTC 缺 `cuda-bindings` 的报错
- [ ] 我知道 `tvm.cuda(0).exist` 不等于「能编译 CUDA」
- [ ] 我能解释 C 模块为什么不能直接调用
- [ ] 我能说出 `export_library(".dll")` 和 `.tar` 的区别
- [ ] 我能列出至少三条「codegen 成功但 runtime 加载失败」的原因

## 延伸阅读

- **TVM Target 文档**（https://tvm.apache.org/docs/reference/api/python/target.html）：读 `Target` 的 kind/keys/arch 和选项语法。与本课关联：target 字符串的权威说明。
- **TVM 源码 `support/nvcc.py`**（https://github.com/apache/tvm/blob/main/python/tvm/support/nvcc.py）：读 NVRTC 编译和 `cuda-bindings` 导入。与本课关联：本章 CUDA 报错的行号依据。
- **TVM 源码 `runtime/module.py`**（https://github.com/apache/tvm/blob/main/python/tvm/runtime/module.py）：读 `export_library` 和 `imports`。与本课关联：模块打包与加载。
- **LLVM Language Reference**（https://llvm.org/docs/LangRef.html）：读 `load`、`store`、`fadd`、向量类型。与本课关联：看懂 `inspect_source()` 的 LLVM IR。
- **CUDA C Programming Guide**（https://docs.nvidia.com/cuda/cuda-c-programming-guide/）：读 thread/block/grid 模型。与本课关联：理解 `bind` 生成的线程绑定。

---

→ [Ch 15 向量化与 SIMD](./15-vectorization.md)