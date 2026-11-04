# Ch 0 — 环境、源码地图与可复现实验

> 先能重复地跑、稳定地拿到 IR，再谈读懂编译器。

## 目标

读完本章，你应该能：

- 在 `/home/ubuntu/aicomp/triton` 这个源码树上确认版本、commit、GPU 架构和 CUDA 工具链；
- 说清楚 `python/triton`、`third_party/nvidia`、`lib/`、`include/` 各自负责什么；
- 用 editable 安装或预编译 wheel 跑通一颗最小 vector-add kernel；
- 用 `TRITON_KERNEL_DUMP`、`TRITON_DUMP_DIR` 和 `kernel.asm` 收集 TTIR/TTGIR/LLIR/PTX/cubin/SASS；
- 在构建失败时，知道怎样退回 wheel、怎样只做前端实验。

本章所有涉及 Triton 运行时、CUDA launch、ptxas 的命令都标为 **guided**：当前机器的基础 Python 环境没有安装 `triton` 和 `torch`，我没有伪造运行输出。`nvidia-smi`、`nvcc`、`python3 --version`、`git log` 是 **实测**。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码目录 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5，显存 15360 MiB |
| 驱动 | 535.216.01 |
| CUDA toolkit | 12.4，`V12.4.131` |
| 系统 Python | 3.10.12 |
| 仓库内 `.venv` | 3.12.16 |
| 基础环境 `import triton` | `ModuleNotFoundError` |
| 基础环境 `import torch` | `ModuleNotFoundError` |
| 仓库 `.venv` 的 `triton` | 3.9.0 wheel（`site-packages`，可 import；源码构建另见下文） |
| 仓库 `.venv` 的 `torch` | 2.5.1+cu124，`torch.cuda.is_available() == True` |
| 仓库 `.venv` 的 `ptxas` | 系统 CUDA 12.4 自带的是 PTX 8.4；wheel 使用自带的 CUDA 工具链 |

**实测（2026-11-04 补录）**：在 `.venv` 里安装 3.9.0 wheel 后，`import triton` 可用；跑 vector-add（`n=1000, BLOCK=128`）得到 `max_err = 0.0`，`asm` key 为 `['cubin','llir','ptx','source','ttgir','ttir']`。完整产物见 [Ch 3 的例子 0](./03-first-kernel-end-to-end.md)。源码构建（`pip install -e .`）需要 LLVM 预编译包；本机已把 `llvm-18f9e623-ubuntu-x64-2.tar.gz` 放在 `~/.triton/archives/` 并用 `TRITON_CACHE_DEPENDENCY_DOWNLOADS=1` 复用，但构建产物是否完整以你本机为准。

**源码依据**：

- `python/triton/__init__.py:1-2` 写死 `__version__ = '3.9.0'`；
- `cmake/llvm-info.json` 记录该 commit 期望的 LLVM hash 是 `18f9e623e4c891ba31ff817c07197707ac9bc593`；
- T4 是 `sm_75`。`third_party/nvidia/backend/compiler.py:113-116` 把 capability 转成 `sm_{capability}`，所以本机是 `sm_75`；
- `num_ctas > 1` 在 `third_party/nvidia/backend/compiler.py:244-247` 被拒绝（要求 SM90+）；`clc` 在 `241-242` 被拒绝（要求 SM100+）。

**guided**：如果你的机器是 A100/H100/Blackwell，把本章的 `sm_75` 换成对应架构即可；不要照搬 T4 的 feature 结论。

## 为什么需要这个

Triton 的 bug 很容易被“层级错位”掩盖：

- 你以为是 layout 问题，其实是 build 用了旧 `libtriton.so`；
- 你以为是 pass 没跑，其实是 cache hit，根本没重新编译；
- 你以为是 PTX 生成错，其实是 `ptxas` 版本或 `TRITON_PTXAS_PATH` 指错；
- 你以为是 kernel 写错，其实是 `TRITON_KERNEL_DUMP` 没开，或者 dump 目录在别处。

所以第一章不急着读 pass，而是先建立一套可重复的实验基线：版本、架构、工具链、IR 产物、缓存位置都能对上。

## 先建立直觉

Triton 安装完以后，你得到的是两层东西：

```text
Python package:  python/triton/**        # 前端、runtime、cache、driver 绑定
C++ extension:   libtriton.so / .so      # MLIR dialects, passes, lowering, LLVM/NVPTX
```

一颗 kernel 的生命周期可以粗暴分成四段：

```text
Python source
  -> Python AST
  -> TTIR / TTGIR / LLIR        (MLIR)
  -> PTX / cubin                (LLVM NVPTX + ptxas)
  -> GPU launch
```

本章只要求你把这四段“在哪里发生、产物在哪里”对上。具体每一段长什么样，Ch 3 再展开。

## 核心概念

### 源码树地图

| 路径 | 作用 | 先看哪里 |
|---|---|---|
| `python/triton/runtime/jit.py` | `@triton.jit`、`JITFunction`、binder、launch | `646-971` 类主体；`744-800` `run`；`707-743` binder 相关 |
| `python/triton/compiler/compiler.py` | `ASTSource`、`IRSource`、`compile`、`CompiledKernel` | `52-85`、`87-131`、`226-364`、`407-522` |
| `python/triton/compiler/code_generator.py` | Python AST → TTIR | `1745-1789` 入口；`294-390` `CodeGenerator` |
| `python/triton/language/core.py` | `tl.*` 的 Python 对象模型 | `885-926` `tensor`；`1845-1861` `program_id`；`2424-2466` `load` |
| `python/triton/language/semantic.py` | `tl.*` → MLIR op 的语义实现 | `26-53` 编程模型；`1047-1102` `load`；`1248-...` `store` |
| `python/triton/backends/compiler.py` | 后端抽象 `BaseBackend` | `add_stages` 契约在 `56-70` |
| `third_party/nvidia/backend/compiler.py` | NVIDIA 后端的五个 stage | `291-305`、`307-393`、`422-532`、`546-574`、`576-653`、`660-673` |
| `third_party/nvidia/language/cuda` | CUDA/libdevice 语言扩展 | 先看 `libdevice.py` 的导出 |
| `lib/Dialect/Triton*` | TTIR/TTGIR/TritonNvidiaGPU dialect 实现 | 从 `lib/Dialect/Triton/IR` 开始 |
| `lib/Conversion/TritonToTritonGPU` | TTIR → TTGIR 的 layout 分配 | 和 Ch 12–14 一起读 |
| `lib/Conversion/TritonGPUToLLVM` | TTGIR → LLVM dialect | 和 Ch 22–28 一起读 |
| `include/triton/**` | C++ 头文件、op 定义、analysis 接口 | 先看 `include/triton/Dialect/Triton/IR/TritonOps.td` |
| `python/tutorials/01-vector-add.py` | 官方最小 kernel | `29-54` kernel，`62-78` launch |
| `docs/programming-guide/` | 官方编程模型说明 | `chapter-1/introduction.rst` |

**源码依据**：文件行数可以用 `wc -l` 复核。当前树里 `jit.py` 1223 行、`compiler.py` 522 行、`code_generator.py` 1790 行、`semantic.py` 2059 行、`core.py` 3901 行、NVIDIA backend `677` 行。不要把“行号”当成永久地址；换 commit 后要重新 `nl -ba`。

### 三种安装方式

| 方式 | 命令 | 适合 | 代价 |
|---|---|---|---|
| 预编译 wheel | `pip install triton==3.9.0` | 只想跑 kernel、看 IR | 看不到本地 C++ 改动；wheel 的 CUDA/ptxas 兼容性要试 |
| editable 源码 | `pip install -e .` | 要改 Python/C++、跟源码 | 首次编译久；LLVM 下载/链接可能失败 |
| 自定义 LLVM | 设 `LLVM_SYSPATH` 等再 `pip install -e .` | 要改 LLVM/MLIR | 最重；LLVM hash 必须匹配 |

**源码依据**：`README.md:41-62` 给出 editable 安装；`README.md:64-117` 给出自定义 LLVM；`README.md:119-137` 给出 `TRITON_BUILD_WITH_CLANG_LLD`、`TRITON_BUILD_WITH_CCACHE`、`MAX_JOBS`、`--no-build-isolation`。`pyproject.toml:1-5` 的 build-system 依赖是 `setuptools`、`cmake`、`ninja`、`nanobind`。

### 常用环境变量

| 变量 | 作用 | 源码 |
|---|---|---|
| `TRITON_KERNEL_DUMP=1` | 打开 IR dump | `python/triton/knobs.py:358` |
| `TRITON_DUMP_DIR=/path` | dump 根目录，默认 `~/.triton/dump` | `knobs.py:345`、`352-353` |
| `TRITON_ALWAYS_COMPILE=1` | 绕过磁盘 cache，强制重编译 | `knobs.py:361` |
| `TRITON_KERNEL_OVERRIDE=1` | 允许用外部 IR 覆盖某个 stage | `knobs.py:357` |
| `TRITON_OVERRIDE_DIR=/path` | override 根目录 | `knobs.py:346` |
| `TRITON_STORE_BINARY_ONLY=1` | cache 只存 cubin/hsaco/json | `knobs.py:360`、`compiler.py:337-339` |
| `TRITON_INTERPRET=1` | 用 Python interpreter 执行，不生成 IR | `knobs.py:471`、`jit.py:1025-1030` |
| `TRITON_PTXAS_PATH=/path/ptxas` | 指定 ptxas | `knobs.py:194-217`、`505-507` |
| `TRITON_HOME=/path` | 改 `.triton` 根目录 | `README.md:126-129` |

## 底层机制

### 构建链

`pip install -e .` 不是“装一个纯 Python 包”。它会：

1. 读 `pyproject.toml` 的 build-system，准备 `cmake`/`ninja`/`nanobind`；
2. 由 `setup.py`/CMake 编译 C++，产出 `python/triton/_C/libtriton*.so`；
3. 把 Python 包以 editable 方式挂到当前环境；
4. 首次 import 时，`python/triton/__init__.py` 加载 runtime、language、compiler 等子模块。

如果本机没有 LLVM，构建脚本会下载 `cmake/llvm-info.json` 指定的预编译 LLVM。想用自己编的 LLVM，就设：

```bash
export LLVM_BUILD_DIR=$HOME/llvm-project/build
LLVM_INCLUDE_DIRS=$LLVM_BUILD_DIR/include \
LLVM_LIBRARY_DIR=$LLVM_BUILD_DIR/lib \
LLVM_SYSPATH=$LLVM_BUILD_DIR \
pip install -e .
```

**源码依据**：`README.md:64-117`。注意 `llvm_hash` 必须匹配，LLVM 没有稳定 C++ API，随便换版本会在链接或运行期炸。

### 编译链

`JITFunction.run` 先 binder，再算 key，再查内存 cache；miss 时走 `_do_compile`，构造 `ASTSource`，调用 `compile`。`compile` 做三件关键事：

1. `backend.add_stages(stages, options, src.language)` 拿到有序 stage 表；
2. 从 `src.ext` 决定从哪一级开始（AST 是 `ttir`，IRSource 可以是 `ttir/ttgir/llir/ptx`）；
3. 依次执行 stage，把每一级 IR 写进 cache group，必要时写进 dump manager。

**源码依据**：`python/triton/runtime/jit.py:744-800`、`910-945`；`python/triton/compiler/compiler.py:226-364`，尤其 `290-295` 的 `first_stage` 和 `326-352` 的 stage 循环。

NVIDIA 后端的 stage 顺序是：

```text
ttir -> ttgir -> llir -> ptx -> cubin
```

**源码依据**：`third_party/nvidia/backend/compiler.py:660-673`。SASS 不是 stage，它是 `cubin` 的派生产物：`AsmDict.__missing__` 在访问 `kernel.asm["sass"]` 时调用 `get_sass`（`compiler.py:390-400`）。

### dump 链

`compile` 里：

```python
enable_ir_dump = knobs.compilation.dump_ir
fn_dump_manager = get_dump_manager(src.hash()) if enable_ir_dump else None
```

之后每个 stage 产出 `next_module`，都会：

```python
fn_dump_manager.put(next_module, ir_filename)
```

如果 `ext == "cubin"`，还会：

```python
sass = get_sass(next_module)
fn_dump_manager.put(sass, file_name + ".sass")
```

**源码依据**：`compiler.py:252-258`、`326-344`。dump 目录是 `TRITON_DUMP_DIR/<base32(src.hash())>/`，文件名是 `<kernel>.<ext>`；`get_dump_manager` 会做 `_base32`（`runtime/cache.py:255-272`）。

注意：cache hit 会在 `compiler.py:267-279` 直接返回，不会进入 stage 循环，所以也不会 dump。要强制 dump，加 `TRITON_ALWAYS_COMPILE=1`。

## 进一步用法/重要变体

### 最小 vector-add kernel

**guided**：下面代码本身是合法的 Triton 3.9 写法，和 `python/tutorials/01-vector-add.py:29-54` 同构；但本机没有安装 `triton`/`torch`，我没有执行它。

```python
import torch
import triton
import triton.language as tl

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask)
    y = tl.load(y_ptr + offs, mask=mask)
    tl.store(out_ptr + offs, x + y, mask=mask)

def add(x, y):
    out = torch.empty_like(x)
    n = out.numel()
    grid = lambda meta: (triton.cdiv(n, meta["BLOCK"]),)
    add_kernel[grid](x, y, out, n, BLOCK=1024)
    return out
```

**源码依据**：`python/tutorials/01-vector-add.py:29-54`；`tl.program_id` 在 `core.py:1845-1861` 调到 `TritonSemantic.program_id`，后者创建 `tt.get_program_id`（`semantic.py:45-48`）；`tl.arange` 在 `core.py:1881-1885` 调到 `semantic.arange`，后者要求 range 是 2 的幂并创建 `tt.make_range`（`semantic.py:621-635`）。

### 跑起来并打印 asm

**guided**：

```python
import torch
from add import add

x = torch.randn(98432, device="cuda", dtype=torch.float32)
y = torch.randn(98432, device="cuda", dtype=torch.float32)
out = add(x, y)
torch.cuda.synchronize()
print(torch.max(torch.abs(out - (x + y))).item())
```

拿到 kernel handle 的方式是直接调用 `JITFunction.run` 或 `warmup`；`kernel[grid](...)` 返回的是 launch 结果，不是 kernel。要拿 handle，可以这样：

```python
k = add_kernel.warmup(x, y, out, out.numel(), BLOCK=1024, grid=(1,))
print(k.asm.keys())
print(k.metadata)
print(k.asm["ttir"][:2000])
```

**源码依据**：`KernelInterface.warmup` 在 `jit.py:365-366`，它把 `warmup=True` 传给 `run`；`run` 在 `744-799` 里只在 `not warmup` 时 launch；`CompiledKernel.asm` 在 `compiler.py:423-431` 从 cache group 构造；`AsmDict` 的 `sass` 惰性求值在 `compiler.py:390-400`。

### 收集 dump

**guided**：

```bash
cd /home/ubuntu/aicomp/triton
TRITON_KERNEL_DUMP=1 \
TRITON_DUMP_DIR=/tmp/triton-dump \
TRITON_ALWAYS_COMPILE=1 \
python /path/to/add_demo.py

find /tmp/triton-dump -maxdepth 2 -type f | sort
```

你会看到类似：

```text
/tmp/triton-dump/<hash>/add_kernel.ttir
/tmp/triton-dump/<hash>/add_kernel.ttgir
/tmp/triton-dump/<hash>/add_kernel.llir
/tmp/triton-dump/<hash>/add_kernel.ptx
/tmp/triton-dump/<hash>/add_kernel.cubin
/tmp/triton-dump/<hash>/add_kernel.sass
```

`<hash>` 是 `base32(src.hash())`，不是 cache key；它只跟源码相关，方便你按 kernel 源码追踪。

### SASS 的两种拿法

```python
k = add_kernel.warmup(x, y, out, out.numel(), BLOCK=1024, grid=(1,))
print(k.asm["sass"][:2000])
```

或者从 dump 目录直接读 `add_kernel.sass`。前者要求 `cuobjdump` 在 `TRITON_CUOBJDUMP_PATH` 或默认路径可用；后者是 `compile` 在 dump 时已经调用过 `get_sass`。

**源码依据**：`compiler.py:342-344`、`390-400`；`python/triton/tools/disasm.py:67-87`。

### 用 IR override 做实验

**guided**：

```bash
TRITON_KERNEL_OVERRIDE=1 \
TRITON_OVERRIDE_DIR=/tmp/triton-override \
TRITON_ALWAYS_COMPILE=1 \
python /path/to/add_demo.py
```

把想改的 IR 放到 `/tmp/triton-override/<src.hash()>/add_kernel.ttgir`，`compile` 会在对应 stage 用你的文件替换。**源码依据**：`compiler.py:257`、`329-336`；`knobs.py:346-357`。

### 只做前端实验

如果只是验证 AST/语义，不关心 IR：

```bash
TRITON_INTERPRET=1 python /path/to/add_demo.py
```

**源码依据**：`jit.py:1025-1030` 在 `knobs.runtime.interpret` 为真时返回 `InterpretedFunction`，不走 `CodeGenerator`。它慢，而且不能用来观察 TTIR/TTGIR/PTX。

## 对比与取舍

| 选择 | 优点 | 缺点 | 什么时候用 |
|---|---|---|---|
| wheel | 快、依赖少 | 不能改 C++；版本/工具链耦合 | 跑教程、收集 IR、做对比 |
| editable | 能改 Python/C++；调试符号可控 | 首次构建久；LLVM 下载可能失败 | 本课大部分源码实验 |
| 自定义 LLVM | 能改 MLIR/LLVM | 最重；hash 必须匹配 | 读/改 lowering 或 dialect |
| dump | 不改代码就能看每级 IR | 占磁盘；cache hit 不触发 | 日常定位 |
| override | 可单独改一级 IR | 需要知道 stage 文件名和 hash | 验证某个 pass 的作用 |

T4 的取舍也要说清楚：它是 `sm_75`，没有 TMA、没有 Hopper 的 warp specialization、没有 Blackwell 的 TMEM/tcgen05。你在 T4 上能稳定练的是 elementwise、reduction、基础 matmul、layout 传播和 LLVM/PTX 阅读；Hopper/Blackwell 特性只能读源码或用 **guided** 的远端环境。

## 踩雷集锦

1. **`TRITON_KERNEL_DUMP=1` 但目录是空的。**
   先确认是不是 cache hit。`compile` 在 `compiler.py:267-279` 命中就直接返回，不跑 stage。加 `TRITON_ALWAYS_COMPILE=1`，或者换 `TRITON_DUMP_DIR`/清 cache。

2. **`kernel.asm.keys()` 里没有 `sass`，就以为没有 SASS。**
   `sass` 是 `AsmDict.__missing__` 惰性生成的（`compiler.py:390-400`），`keys()` 在第一次访问前不会列出来。直接 `k.asm["sass"]`。

3. **`pip install -e .` 每次都很慢。**
   看 `README.md:135-137`：加 `--no-build-isolation` 可以避免每次换 cmake symlink 导致大量 `.a` 重编。再配合 `TRITON_BUILD_WITH_CCACHE=true`。

4. **T4 上开 `num_ctas=2` 直接报错。**
   `third_party/nvidia/backend/compiler.py:244-247` 明确要求 SM90+。这不是 kernel 写错，是硬件/后端限制。

5. **`ptxas` 找不到或版本不对。**
   `make_cubin` 用 `get_ptxas(self.target.arch).path`（`third_party/nvidia/backend/compiler.py:576-577`），路径来自 `TRITON_PTXAS_PATH` 或默认 `python/triton/backends/nvidia/bin/ptxas`（`knobs.py:194-217`）。CUDA 12.4 的 ptxas 和 driver 535 要能对上。

6. **把 `.triton/dump` 当成 cache。**
   dump 是 `TRITON_DUMP_DIR`，cache 是 `TRITON_CACHE_DIR`/`~/.triton/cache`；两者生命周期和 key 都不同。`TRITON_STORE_BINARY_ONLY=1` 只影响 cache，不影响 dump 的全部 stage。

## 进阶

- 读 `compile` 的 `CompilationListener` 分支（`compiler.py:226-229`、`324-325`、`358-361`），自己挂一个 listener 记录每个 stage 的耗时；`CompileTimer` 在 `compiler.py:187-223`。
- 用 `TRITON_KERNEL_OVERRIDE` 把 TTIR 改成故意错误的 op，观察 verifier/parser 在哪一层报错。
- 在 T4 和（如果有）H100 上跑同一颗 vector-add，比较 TTGIR 的 layout 和 PTX 的指令选择；不要假设两边一样。
- 读 `python/triton/runtime/cache.py:285-310` 的 `triton_key`，理解为什么改 Python 前端也会让磁盘 cache 失效。

## 动手练习

1. **构建基线**：在仓库内新建 venv，按 `README.md:41-62` 做 editable 安装；记录 `python -c "import triton; print(triton.__version__)"` 的输出。**guided**。
2. **跑 vector-add**：用本章 kernel，跑 `98432` 个元素，和 `x + y` 对比最大误差。**guided**。
3. **收集 IR**：用 `TRITON_KERNEL_DUMP=1` + `TRITON_ALWAYS_COMPILE=1`，列出 dump 目录里所有文件，并解释每个后缀来自哪个 stage。**guided**。
4. **看 SASS**：分别用 `k.asm["sass"]` 和 dump 的 `.sass` 拿一次，确认两者内容一致（或至少同源）。**guided**。
5. **制造一次 cache hit**：先正常运行一次，再运行一次，观察第二次没有新的 dump；然后加 `TRITON_ALWAYS_COMPILE=1` 再跑，观察 dump 重新出现。**guided**。
6. **失败回退**：故意把 `LLVM_SYSPATH` 指到一个不存在的目录，记录构建错误；然后退回 wheel 或清空环境变量重新构建。**guided**。

## 本章重点

- 版本、commit、GPU 架构、CUDA/ptxas 必须一起记录，否则 IR 对比没有意义。
- `python/triton` 是前端+runtime，`libtriton` 是 C++/MLIR/LLVM；editable 安装把两者连起来。
- NVIDIA stage 顺序是 `ttir -> ttgir -> llir -> ptx -> cubin`；SASS 是 cubin 的惰性派生。
- dump 只在真正编译时发生；cache hit 不 dump。
- T4 是 `sm_75`，很多 Hopper/Blackwell 特性在本机不可用，读源码时要注意 gate。

## 自我检核

1. `TRITON_KERNEL_DUMP=1` 开了但 dump 目录为空，最可能的原因是什么？
2. `kernel.asm.keys()` 为什么可能没有 `sass`？怎样拿到 SASS？
3. `compile` 里 `first_stage` 由什么决定？`IRSource` 从 `ttgir` 开始时，`ttir` stage 会跑吗？
4. `TRITON_DUMP_DIR` 和 `TRITON_OVERRIDE_DIR` 分别影响哪条路径？
5. 在 T4 上，`num_ctas=2` 和 `clc=True` 分别会在哪里被拒绝？

## 延伸阅读

- **Triton README 的 Install from source / Building with a custom LLVM**
  - **读哪里**：`README.md:41-137`。
  - **学什么**：editable 安装、预编译 LLVM、`LLVM_SYSPATH`、`MAX_JOBS`、`--no-build-isolation`。
  - **前提**：能在本机装 CMake/Ninja，且接受首次构建较慢。
- **Triton 官方安装文档**
  - **读哪里**：`docs/getting-started/installation.rst:1-52`。
  - **学什么**：wheel 与 source 的差异、`make test`/`make test-nogpu` 的用途。
  - **前提**：先有可用的 Python 3.10–3.14 环境。
- **`python/tutorials/01-vector-add.py`**
  - **读哪里**：`29-54` kernel，`62-78` launch。
  - **学什么**：最小 SPMD 模型、`program_id`、`arange`、mask、grid。
  - **前提**：装好 `torch` 和 `triton`。
- **`docs/programming-guide/chapter-1/introduction.rst`**
  - **读哪里**：整章。
  - **学什么**：Triton 的 tile-level 编程模型与 CUDA 的对照。
  - **前提**：知道 grid/block/warp 的基本概念。
- **MLIR 论文**
  - **读哪里**：第 2–3 节（IR 对象模型、dialect）。
  - **学什么**：为什么 TTIR/TTGIR/LLIR 可以是同一套基础设施上的多级 IR。
  - **前提**：读过至少一个 MLIR 小例子，或先看 [`mlir_core`](../mlir_core/README.md)。

## 下一章

→ [Ch 1 Triton 在 AI 编译器栈中的位置](./01-triton-in-the-ai-compiler-stack.md)
