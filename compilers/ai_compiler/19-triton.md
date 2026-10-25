# Ch 19 Triton：block-level 编程模型

> **目标**：理解 Triton 为什么用“块（block）”而不是“线程（thread）”作为编程单位，掌握 `program_id`、`tl.arange`、mask、`tl.load/store`、`tl.dot` 的语义，并能读懂一个 vector-add 与一个 blocked matmul kernel。本机无 GPU、无 Triton runtime，所有执行结果标 `guided`。

## 为什么需要这个？

CUDA 的编程模型是 **thread-level**：你写一个线程做什么，然后启动几万个线程。为了性能，你必须手动处理：

- 哪个线程读哪个元素（合并访问）；
- 哪些数据放 shared memory；
- 哪里要 `__syncthreads()`；
- 每个线程算几个输出（register tiling）；
- 边界线程要不要提前 return。

这些细节决定了 10 倍性能差，但它们和“我要算一个 matmul”这件事本身无关。Triton 的出发点就是：**让程序员描述“一个块做什么”，把线程映射、shared memory、同步交给编译器**。

这也是 TorchInductor 在 GPU 上选择 Triton 作为 codegen 目标的原因（Ch 18）：Inductor 的 IR 本来就是循环级的，一个融合后的循环 nest 正好对应一个 Triton program。

## 先建立直觉：一个 program 处理一个 block

对比同一段 vector add：

```text
CUDA：
  grid = ceil(n / 256) 个 block，每 block 256 个 thread
  每个 thread：i = blockIdx.x * blockDim.x + threadIdx.x
               if (i < n) out[i] = x[i] + y[i]

Triton：
  grid = ceil(n / BLOCK) 个 program
  每个 program：offs = pid * BLOCK + arange(0, BLOCK)
                mask = offs < n
                out[offs] = x[offs] + y[offs]   （masked）
```

区别在于：

| 维度 | CUDA | Triton |
|---|---|---|
| 并行单位 | thread | program instance |
| 程序员看到的数据 | 标量（每线程一个） | block（一个 tensor 值） |
| 线程映射 | 手写 | 编译器决定 |
| shared memory | 手写 `__shared__` | 编译器自动分配 |
| 同步 | 手写 `__syncthreads()` | 编译器插入 |
| 边界 | 每线程 `if` | block 级 mask |
| 适合 | 极致控制、特殊指令 | 常见算子、快速迭代 |

关键心智模型：**Triton 里的 `x` 不是一个 float，而是一个长度为 BLOCK 的寄存器块**。`x + y` 是块级加法，不是标量加法。

## 核心概念一：program_id、grid 与 constexpr

`tl.program_id(axis)` 返回当前 program 在 launch grid 上的编号。源码依据（`python/triton/language/core.py:1599`，v3.5.0）：

```python
def program_id(axis, _semantic=None):
    """
    Returns the id of the current program instance along the given :code:`axis`.

    :param axis: The axis of the 3D launch grid. Must be 0, 1 or 2.
    """
```

三个要点：

1. **只有 0/1/2 三个轴**，对应 CUDA 的 `blockIdx.x/y/z`。
2. **它是 program 编号，不是线程编号**。一个 program 内部有多少线程由编译器和 `num_warps` 决定。
3. **grid 由 Python 侧传入**：`kernel[(grid_x, grid_y, grid_z)](...)`。

`BLOCK_SIZE: tl.constexpr` 是编译期常量。它必须能在编译期确定，因为 Triton 要据此分配寄存器和生成循环。常见错误是把它当运行时参数传，结果编译失败或退化成动态形状。

`tl.arange(start, end)` 生成 `[start, end)` 的连续值，返回一个 block。源码依据（`core.py:1635`）说明它是半开区间，且**起止值必须是 2 的幂**（`core.py:1646-1650`）；长度受 `TRITON_MAX_TENSOR_NUMEL` 限制，该常量在 `python/triton/_utils.py:11` 定义为 `1048576`。

## 核心概念二：block 与 mask

Triton 没有“越界线程”的概念，只有“越界元素”。所以边界处理靠 mask：

```python
offsets = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
mask = offsets < n_elements
x = tl.load(x_ptr + offsets, mask=mask, other=0.0)
```

三个细节：

- `mask` 是一个 block 级的布尔张量，形状和 `offsets` 一样。
- `other=0.0` 指定被 mask 掉的元素读成什么。**不写 `other` 时，被 mask 的元素值是未定义的**，参与加法会污染结果。
- `tl.store` 也要传 mask，否则会写越界地址。

为什么 vector-add 里 `other` 用 0 就对？因为越界元素的输出会被 store 的 mask 丢掉，读成什么都不影响结果。但在 reduction（比如 `sum`）里，`other=0.0` 是正确性的必要条件；如果算的是 `max`，就该用 `-inf`。

block 形状的约束：

- 实践中 `BLOCK_SIZE` 取 2 的幂（256、512、1024）；
- block 的总元素数不能超过 `TRITON_MAX_TENSOR_NUMEL = 1048576`；
- 形状约束由编译器/后端检查，违反时在编译期报错，而不是运行期。

## 核心概念三：tl.load / tl.store / tl.dot 的语义

### load / store

`tl.load(pointer, mask=None, other=None, ...)`（`core.py:2070`）支持两种形态：

1. **标量指针** → 读一个标量；
2. **N 维指针张量** → 读一个 N 维 block。

指针张量是“地址的数组”。所以 `x_ptr + offsets` 不是“解引用”，而是生成一组地址；`tl.load` 才真正访存。`tl.store` 对称（`core.py:2146`）。

### dot

`tl.dot(input, other, acc=None, ...)`（`core.py:1987`）的文档写得很清楚：

> Returns the matrix product of two blocks. The two blocks must both be two-dimensional or three-dimensional and have compatible inner dimensions.

三个约束：

- 输入必须是 2D 或 3D block；
- 内维必须匹配；
- 支持的类型是 int8 / fp8 / fp16 / bf16 等；`acc` 支持 fp16 / fp32 / int32。

`acc` 参数是**累加器**：`acc = tl.dot(a, b, acc)` 等价于 `acc += a @ b`，但只产生一次矩阵乘指令。这是 K 维循环里最重要的写法，避免每轮都新建累加器。

fp32 输入在支持 Tensor Core 的设备上默认走 TF32（`core.py:2002-2005` 的 `input_precision` 说明）。这意味着**结果精度低于真正的 fp32 matmul**，做数值对齐时要放宽容差。

## 底层机制：Triton 编译器怎么把 block 变成机器码

Triton 的编译管线大致是：

```text
Python AST（@triton.jit）
   │ AST → TTIR（Triton IR，block 级、无 layout）
   ▼
TTIR
   │ 优化：CSE、常量折叠、循环变换
   ▼
TTGIR（Triton GPU IR，带 layout / encoding）
   │ 决定每个 block 如何映射到 warp、shared memory、寄存器
   ▼
LLVM IR
   │ NVPTX / AMDGPU 后端
   ▼
PTX / cubin / HSACO
```

关键在 **TTGIR**：TTIR 只描述“块级计算”，TTGIR 才决定“这个块用几个 warp、每 warp 处理哪部分、数据怎么在 shared memory 里排布”。这一步类似 TVM 的 schedule，但由编译器自动完成。

`num_warps` 和 `num_stages` 是你能控制的两个旋钮：

- `num_warps`：每个 program 用几个 warp（影响并行度和寄存器压力）；
- `num_stages`：软件流水线的级数（影响 global → shared 的预取）。

本机没有 Triton，也没有 NVIDIA GPU，所以这条管线**无法实测**。上面的阶段划分来自 Triton 官方文档与源码结构，执行标 `guided`。

## 示例一：vector-add kernel（guided）

下面这段代码语法已用 `ast.parse` 验证（`实测`：语法通过），但**没有在 GPU 上执行**——本机无 Triton runtime。

```python
import torch
import triton
import triton.language as tl


@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n_elements,
               BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(axis=0)
    offsets = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offsets < n_elements
    x = tl.load(x_ptr + offsets, mask=mask, other=0.0)
    y = tl.load(y_ptr + offsets, mask=mask, other=0.0)
    tl.store(out_ptr + offsets, x + y, mask=mask)


def add(x, y, BLOCK_SIZE=1024):
    out = torch.empty_like(x)
    n = x.numel()
    grid = (triton.cdiv(n, BLOCK_SIZE),)
    add_kernel[grid](x, y, out, n, BLOCK_SIZE=BLOCK_SIZE)
    return out


if __name__ == "__main__":
    torch.manual_seed(0)
    x = torch.randn(98432, device="cuda")
    y = torch.randn(98432, device="cuda")
    torch.testing.assert_close(add(x, y), x + y)
    print("ok")
```

逐行解释：

- `add_kernel[grid](...)`：方括号里是 launch grid，圆括号里是 kernel 参数。这是 Triton 的调用约定。
- `triton.cdiv` 是向上取整除法，避免最后一个 program 越界。
- `n_elements` 是运行时标量；`BLOCK_SIZE` 是编译期常量，所以放在 `constexpr`。
- `x + y` 是 block 级加法；编译器把它展开成每个线程若干元素的 SIMD 操作。
- 98432 不是 1024 的倍数（98432 = 96×1024 + 128），所以最后一个 program 会走 mask 路径。这正是我们要覆盖的边界。

这个 kernel 在 GPU 上的预期行为是：每个 program 处理 1024 个元素，最后一个 program 只处理 128 个。**具体耗时取决于 GPU 型号，本章不给数字。**

## 示例二：blocked matmul kernel（guided）

matmul 比 vector-add 多三件事：二维索引、K 维循环、累加器。

```python
import torch
import triton
import triton.language as tl


@triton.jit
def matmul_kernel(a_ptr, b_ptr, c_ptr,
                  M, N, K,
                  stride_am, stride_ak,
                  stride_bk, stride_bn,
                  stride_cm, stride_cn,
                  BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr,
                  BLOCK_K: tl.constexpr):
    pid = tl.program_id(axis=0)
    num_pid_m = tl.cdiv(M, BLOCK_M)
    num_pid_n = tl.cdiv(N, BLOCK_N)
    pid_m = pid // num_pid_n
    pid_n = pid % num_pid_n

    offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    offs_k = tl.arange(0, BLOCK_K)

    a_ptrs = a_ptr + offs_m[:, None] * stride_am + offs_k[None, :] * stride_ak
    b_ptrs = b_ptr + offs_k[:, None] * stride_bk + offs_n[None, :] * stride_bn

    acc = tl.zeros((BLOCK_M, BLOCK_N), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_K)):
        a = tl.load(a_ptrs, mask=offs_k[None, :] < K - k * BLOCK_K, other=0.0)
        b = tl.load(b_ptrs, mask=offs_k[:, None] < K - k * BLOCK_K, other=0.0)
        acc = tl.dot(a, b, acc)
        a_ptrs += BLOCK_K * stride_ak
        b_ptrs += BLOCK_K * stride_bk

    c = acc.to(tl.float32)
    c_ptrs = c_ptr + offs_m[:, None] * stride_cm + offs_n[None, :] * stride_cn
    mask = (offs_m[:, None] < M) & (offs_n[None, :] < N)
    tl.store(c_ptrs, c, mask=mask)


def matmul(a, b):
    M, K = a.shape
    K2, N = b.shape
    assert K == K2
    c = torch.empty((M, N), device=a.device, dtype=torch.float32)
    grid = (triton.cdiv(M, 64) * triton.cdiv(N, 64),)
    matmul_kernel[grid](a, b, c, M, N, K,
                        a.stride(0), a.stride(1),
                        b.stride(0), b.stride(1),
                        c.stride(0), c.stride(1),
                        BLOCK_M=64, BLOCK_N=64, BLOCK_K=32)
    return c


if __name__ == "__main__":
    torch.manual_seed(0)
    a = torch.randn(256, 512, device="cuda")
    b = torch.randn(512, 128, device="cuda")
    torch.testing.assert_close(matmul(a, b), a @ b, atol=1e-2, rtol=1e-2)
    print("ok")
```

拆解：

1. **一维 grid 拆成二维 program 坐标**：`pid_m = pid // num_pid_n`，`pid_n = pid % num_pid_n`。也可以直接用二维 grid，但一维写法便于加 `GROUP_SIZE_M` 做 L2 重排。
2. **广播出指针矩阵**：`offs_m[:, None] * stride_am + offs_k[None, :] * stride_ak` 得到 `[BLOCK_M, BLOCK_K]` 的地址块。`None` 是 Triton 的广播语法，等价于 NumPy 的 `newaxis`。
3. **K 维分块**：每次加载 `[BLOCK_M, BLOCK_K]` 和 `[BLOCK_K, BLOCK_N]`，`tl.dot` 累加到 `acc`。
4. **K 维 mask**：`offs_k < K - k * BLOCK_K` 处理 K 不是 `BLOCK_K` 倍数的情况。注意这里用减法而不是直接比较绝对索引，避免指针已经前移后算错。
5. **输出 mask**：M/N 不是块大小的倍数时，只写合法区域。

这个 kernel 与 Triton 官方 tutorial 的 `03-matrix-multiplication.py` 同构（官方版额外加了 `GROUP_SIZE_M`、autotune 配置和 fp16 路径）。本机没有 GPU，**没有跑过，也不声称性能**。

## 示例三：本机到底能不能跑 Triton

先探测，不猜：

```python
import importlib.util
for m in ["triton", "torch"]:
    print(m, importlib.util.find_spec(m) is not None)
```

本机真实输出：

```text
triton False
torch True
```

再探测 CPU 替代：

```powershell
& $py -m pip index versions triton-cpu
```

本机真实输出（节选）：

```text
ERROR: No matching distribution found for triton-cpu
```

结论：

- 本机 **没有安装 Triton**（`find_spec` 为 `None`）；
- PyPI 当前索引里 **没有 `triton-cpu`** 这个包；
- 因此本章 kernel 只能做语法检查，不能执行。

如果要在本机之外跑，需要：

1. NVIDIA GPU（Compute Capability 7.0+ 较稳妥）；
2. 安装匹配的 `triton` wheel（通常随 `torch` 的 CUDA wheel 一起提供）；
3. `torch.cuda.is_available()` 为 `True`；
4. 把上面的 `device="cuda"` 换成你的设备。

这三条缺一不可。**不要用 CPU 张量调用 Triton kernel**——它不会“自动回退”，而是直接报没有可用后端。

## 对比与取舍

| 维度 | CUDA C++ | Triton | TVM TIR | Inductor |
|---|---|---|---|---|
| 编程单位 | thread | block / program | loop nest | 自动生成 |
| 线程映射 | 手写 | 编译器 | 调度原语 | 编译器 |
| shared memory | 手写 | 自动 | 手写/调度 | 自动 |
| 边界处理 | 每线程分支 | block mask | 谓词/边界检查 | 自动 |
| 上手速度 | 慢 | 快 | 中 | 最快 |
| 性能上限 | 最高 | 高 | 高（需调优） | 高（受模板限制） |
| 调试难度 | 高 | 中 | 中 | 低（看生成代码） |
| 本机可跑 | 否 | 否 | 是（CPU） | 是（CPU） |

选择建议：

- 写一个新算子、要快速接近库性能：Triton。
- 要精确控制 shared memory / 特殊指令 / 架构特性：CUDA。
- 要在 CPU 上做循环级优化、或需要完整调度搜索：TVM。
- 只是想让 PyTorch 模型快一点：先试 Inductor，别手写 kernel。

## 踩雷集锦

1. **把 `tl.program_id` 当线程号**：它是 program 编号；一个 program 内部还有很多线程。
2. **masked load 不写 `other`**：被 mask 的元素是未定义值，进入 `sum`/`max` 会污染结果。
3. **block 大小不是 2 的幂**：多数后端不接受；从 256/512/1024 开始试。
4. **`tl.dot` 的 K 维不匹配**：`a` 的最后一维必须等于 `b` 的倒数第二维；3D 时还要对齐 batch 维。
5. **忘记 `acc` 导致每轮新建累加器**：`acc = tl.dot(a, b)` 在循环里会丢掉上一轮结果，必须 `acc = tl.dot(a, b, acc)`。
6. **fp32 输入默认 TF32**：数值对齐时 `1e-5` 的容差可能不够，要用 `1e-2` 或显式设置 `input_precision`。
7. **以为 Triton 会自动用 CPU**：没有 CPU 后端就报错，不会静默回退。
8. **在 `@triton.jit` 里用普通 Python 控制流**：只有编译期可求值的 `if`/`range` 才安全；数据依赖分支要改写。

## 进阶：再往深一层

- **`GROUP_SIZE_M`**：把一维 pid 重排成“按列分组”，提高 L2 命中率。官方 matmul tutorial 用 8 组，值得对照读。
- **`num_stages` 软件流水线**：让 global → shared 的加载与计算重叠；stage 太多会增加 shared memory 压力。
- **`tl.dot` 的 `input_precision`**：`"tf32"` / `"tf32x3"` / `"ieee"`，在精度和吞吐之间取舍。
- **Triton 的 layout（TTGIR）**：读 `lib/Conversion/TritonGPUToLLVM` 能理解 block 如何落到 warp 和 shared memory；本机无源码构建，`guided`。
- **与 Inductor 的关系**：Inductor 的 `TritonScheduling` 就是生成这类 kernel；你在 Ch 18 看到的 C++ 对应 CPU，GPU 上则换成 Triton。

## 动手练习

### 练习 1：改 BLOCK_SIZE，推演边界

把 vector-add 的 `BLOCK_SIZE` 从 1024 改成 256，n 仍为 98432。回答：grid 变成多少？最后一个 program 处理多少元素？mask 覆盖了哪些位置？

<details>
<summary>参考思路</summary>

`ceil(98432 / 256) = 385`（因为 385×256 = 98560，多出 128）。最后一个 program 的 offsets 是 `384×256 ... 385×256-1`，合法元素只有前 128 个。mask 为 `offsets < 98432`。这个问题在 GPU 上跑才能验证，属 `guided`；但算术可以在本机用 Python 验证。
</details>

### 练习 2：把一维 grid 改成二维

改写 `matmul_kernel` 的调用方式，使用 `grid = (num_pid_m, num_pid_n)` 和 `pid_m = tl.program_id(0)`、`pid_n = tl.program_id(1)`。回答：哪种写法更适合加 `GROUP_SIZE_M`？为什么？

<details>
<summary>参考思路</summary>

二维 grid 更直观，但一维 grid 可以把 pid 重新映射成“分组列优先”，从而让同一组的 C 块复用 A/B 的 L2 数据。官方 tutorial 选择一维 + 重映射，就是为了这个优化。两种写法语义等价，性能不同。
</details>

### 练习 3：数值对齐的容差

如果 `a`、`b` 都是 fp32，`tl.dot` 在 Ampere 上默认 TF32。预测 `assert_close` 的合理容差，并解释为什么不能直接用 `atol=1e-6`。

<details>
<summary>参考思路</summary>

TF32 只有 10 位尾数，单次乘加的相对误差约 `1e-3`；K=512 累加后误差可能到 `1e-2` 量级。合理做法是 `atol=1e-2, rtol=1e-2`，或设置 `input_precision="ieee"` 后再收紧。具体数值需在 GPU 上实测，本章不伪造。
</details>

### 练习 4：把 ReLU 融进 matmul

在 matmul kernel 的 `tl.store` 之前加一行 `c = tl.maximum(c, 0.0)`。回答：这相当于在什么位置做融合？如果改成在 PyTorch 里 `torch.relu(matmul(a, b))`，会多几次 global memory 读写？

<details>
<summary>参考思路</summary>

这是 epilogue 融合：ReLU 在结果写回前作用于寄存器块，不额外读写 global memory。PyTorch 版本会先写 matmul 结果，再读出来做 ReLU，再多写一次；多两次全量读写。Triton 版本只在 GPU 上可验证，属 `guided`。
</details>

### 练习 5：探测你的环境

写一个脚本，输出 `torch.cuda.is_available()`、`triton.__version__`（若可导入）、GPU 名称。根据输出判断本章哪些代码你能跑。

<details>
<summary>参考思路</summary>

```python
import torch, importlib.util
print("cuda", torch.cuda.is_available())
print("triton", importlib.util.find_spec("triton") is not None)
if torch.cuda.is_available():
    print(torch.cuda.get_device_name(0))
```

如果 `cuda=False` 或 `triton=None`，本章所有 kernel 都只能读、不能跑；不要试图用 CPU 张量强行调用。
</details>

## 本章重点整理

- Triton 的并行单位是 **program**，一个 program 处理一个 block；线程映射由编译器负责。
- `tl.program_id` 只有 0/1/2 三个轴，返回 program 编号，不是线程号。
- `tl.arange` 生成 block 内的连续索引；block 形状应是 2 的幂，且不超过 1048576 个元素。
- 边界靠 mask；masked load 必须给 `other`，否则值是未定义的。
- `tl.dot` 是块级矩阵乘，`acc` 参数用于 K 维循环累加；fp32 默认可能走 TF32。
- Triton 编译管线是 TTIR → TTGIR → LLVM IR → PTX，layout 决策在 TTGIR。
- 本机无 Triton、无 GPU、PyPI 无 `triton-cpu`，所有执行标 `guided`；只有语法经过 `ast.parse` 验证。
- Inductor 的 GPU 后端就是生成 Triton kernel；两者是上下游关系。

## 自我检核

- [ ] 我能用自己的话解释“block 而不是 thread”意味着什么
- [ ] 我知道 `tl.program_id` 和 CUDA 的 `threadIdx` 的区别
- [ ] 我能写出带 mask 和 `other` 的 vector-add kernel
- [ ] 我能解释 `acc = tl.dot(a, b, acc)` 里 `acc` 的作用
- [ ] 我知道 fp32 `tl.dot` 为什么可能不满足 `1e-6` 容差
- [ ] 我能说出 Triton 编译管线的四个阶段
- [ ] 我知道本机为什么跑不了本章代码，以及需要什么条件
- [ ] 我能把 matmul 的一维 grid 改写成二维 grid 并解释取舍

## 延伸阅读

- **Triton 官方 tutorial: Vector Addition**（https://triton-lang.org/main/getting-started/tutorials/01-vector-add.html）：读 kernel 定义与 launch 语法。与本章关联：示例一的来源。
- **Triton 官方 tutorial: Matrix Multiplication**（https://triton-lang.org/main/getting-started/tutorials/03-matrix-multiplication.html）：读 `GROUP_SIZE_M`、autotune、fp16 路径。与本章关联：示例二的完整版。
- **Triton 源码 `python/triton/language/core.py`**（v3.5.0，本机已下载到 `.scratch\ai-compiler\ch19\triton_core_v3.5.0.py`）：读 `program_id`、`load`、`store`、`dot` 的 docstring。与本章关联：语义的权威依据。
- **Triton 论文**（https://www.eecs.harvard.edu/~htk/publication/2019-mapl-tillet-kung-cox.pdf）：读 block-level 抽象的设计动机。与本章关联：为什么不是另一套 CUDA 封装。
- **TorchInductor 源码 `torch/_inductor/codegen/triton.py`**：读 `TritonKernel` 与 `TritonScheduling`。与本章关联：Inductor 如何生成 Triton。

---

→ [Ch 20 XLA / StableHLO：函数式图与 HLO 管线](./20-xla-stablehlo.md)