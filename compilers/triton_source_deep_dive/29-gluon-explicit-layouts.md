# Ch 29 — Gluon：显式 layout 的前端

> Triton 让编译器替你选 layout；Gluon 把同一套后端交给你，但要求你自己说清楚每个 tensor 怎么分布、每块 shared memory 怎么放。

## 目标

读完本章，你应该能回答：

- Gluon 和 Triton 共用哪些后端 stage？`glir` 在流水线的哪个位置？
- `Language.GLUON` 如何改变 `add_stages` 的行为？
- Gluon 的 `BlockedLayout`、`NVMMASharedLayout` 与 TTGIR 的 encoding 是什么关系？
- `gl.allocate_shared_memory` 如何变成 `ttg.local_alloc`，又复用哪套 Allocation 分析？
- Gluon 如何绕过 Triton 的自动 layout 选择？
- 什么时候该用 Gluon？它的稳定性/兼容性风险在哪里？

本章的主线是“同一后端、不同前端契约”：

```text
Triton:  Python AST -> ttir -> convert_to_ttgpuir -> ttgir -> llir -> ptx -> cubin
Gluon:   Python AST -> glir -> gluon_to_ttgir     -> ttgir -> llir -> ptx -> cubin
```

## 环境/版本

| 项目 | 值 | 证据 |
|---|---|---|
| Triton 源码 | `/home/ubuntu/aicomp/triton` | 本地树 |
| commit | `a972119e92a27b445cd160017ea461e56148590c` | `git log -1` |
| 版本 | 3.9.0 | `python/triton/__init__.py:2` |
| GPU | Tesla T4，sm_75 | `nvidia-smi` |
| CUDA / ptxas | 12.4，`V12.4.131` | `ptxas --version` |
| Gluon 路径 | `python/triton/experimental/gluon/` | 源码树 |
| Gluon 教程 | `python/tutorials/gluon/01-intro.py` … `14-multicta.py` | 源码树 |
| 本章 Gluon 代码 | **guided**，未在 T4 上执行 | 无可用 Gluon 运行环境 |

**guided**：本章没有在 T4 上实际运行 Gluon kernel。Gluon 的基础 copy/layout 代码在 sm_75 上是否可用，取决于你安装的 Triton 构建；而 `warp_specialize`、TMA、WGMMA、TMEM 等特性明确要求 Hopper/Blackwell。下面所有 Gluon 代码和 `glir` 片段都按源码推导，标为 guided；标 `源码依据` 的结论直接来自源码。

## 为什么需要这个

Triton 的自动 layout 很强，但它是一个“黑盒”：

- 你写 `tl.dot`，编译器决定 A/B 用 `dot_op` 还是 `nvidia_mma`；
- 你写 `tl.load`，编译器决定用 `blocked` 还是 `coalesced`；
- 你写 `for` 循环，编译器决定 pipeline 的 stage 和 shared memory 复用。

当自动选择已经接近最优时，这很好；当你想手工调一个 GEMM 的 shared memory swizzle、想让某个 warp 专门发 TMA、想避免一次多余的 `convert_layout` 时，Triton 没有稳定的入口。

Gluon 的思路是：**不换后端，换前端契约**。它仍然生成 TTGIR，仍然走 `Allocation`、`Membar`、`TritonGPUToLLVM`、NVPTX、ptxas；但它要求你在 Python 侧显式写出 layout 和 shared memory。这样：

1. 你能控制 layout，避免自动传播带来的转换；
2. 你能控制 shared memory 的生命周期和 swizzle；
3. 你能显式写 warp specialization，而不是依赖 pass 推导；
4. 你仍然复用 Triton 的 lowering、runtime、cache 和 driver。

代价是：你也要自己承担 layout 合法性、bank conflict、barrier/fence 和硬件特性可用性。

## 先建立直觉

先看两条前端路径的差异：

| 维度 | Triton | Gluon |
|---|---|---|
| decorator | `@triton.jit` | `@gluon.jit` |
| 首级 IR | `ttir` | `glir` |
| layout | 编译器自动分配 | 用户显式指定 |
| 进入 TTGIR 的 pass | `convert_to_ttgpuir` + 优化 | `gluon_to_ttgir` |
| shared memory | 编译器插 `local_alloc` | 用户 `allocate_shared_memory` |
| warp specialization | 编译器 pass | 用户 `warp_specialize` |
| 后端 | 同一套 NVIDIA backend | 同一套 NVIDIA backend |

关键差异只有一处：**Gluon 不跑 `convert_to_ttgpuir`**。Triton 的 `make_ttgir` 第一件事就是：

```python
passes.ttir.add_convert_to_ttgpuir(pm, f"cuda:{capability}", opt.num_warps, 32, opt.num_ctas)
```

源码依据：`third_party/nvidia/backend/compiler.py:307-315`。

Gluon 的 `gluon_to_ttgir` 没有这一步：

```python
passes.gluon.add_inliner(pm)
passes.gluon.add_infer_coalesced_encodings(pm)
passes.gluon.add_resolve_auto_encodings(pm)
nvidia.passes.ttnvgpuir.add_tma_lowering(pm)
passes.gluon.add_canonicalizer(pm)
...
```

源码依据：`third_party/nvidia/backend/compiler.py:394-420`。

所以 Gluon 的 `glir` 里已经带了 encoding；`gluon_to_ttgir` 只做 inlining、补齐 `AutoLayout`/`CoalescedLayout`、TMA lowering 和规范化，不会重新分配 layout。

## 核心概念

### 1. `Language.GLUON` 与 `GluonASTSource`

后端抽象里只有两个前端：`Language.TRITON` 和 `Language.GLUON`，源码在 `python/triton/backends/compiler.py:17-20`。

`GluonASTSource` 继承 `ASTSource`，只把 `self.language` 改成 `Language.GLUON`、`self.ext` 改成 `"glir"`（`_runtime.py:13-24`）；`GluonJITFunction.create_binder` 再把 `self.ASTSource` 换成 `GluonASTSource`（`_runtime.py:51-59`）。

### 2. `glir` 是什么

`glir` 是 Gluon 的首级 IR。它和 TTIR 很像，都是 `tt.func`/`tt.load`/`tt.store`/`tt.dot`，但 tensor 类型上已经带 encoding：

```mlir
tensor<128xi32, #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>>
```

如果用户写 `gl.AutoLayout()`，类型里会出现：

```mlir
tensor<16xi32, #gluon.auto_encoding>
```

源码依据：`test/Gluon/inlining.mlir:6-17`、`include/triton/Dialect/Gluon/IR/GluonAttrDefs.td:7-13`。

### 3. 显式 distributed layout

Gluon 的 `_layouts.py` 把 layout 做成 Python 对象，每个对象都能 `_to_ir(builder)`：

| Python 类 | 对应 TTGIR encoding | 用途 |
|---|---|---|
| `BlockedLayout` | `#ttg.blocked` | 通用 tile 分布、coalesced load/store |
| `SliceLayout` | `#ttg.slice` | `expand_dims`/slice 后的布局 |
| `DotOperandLayout` | `#ttg.dot_op` | dot 的 A/B operand |
| `NVMMADistributedLayout` | `#ttg.nvidia_mma` | MMA 结果布局 |
| `DistributedLinearLayout` | `#ttg.linear` | 显式线性布局 |
| `AutoLayout` | `#gluon.auto_encoding` | 让 Gluon 推断 |
| `CoalescedLayout` | `#gluon.coalesced_encoding` | 让 Gluon 按 load/store 推断 |

`BlockedLayout` 的字段就是 TTGIR 的四个参数：`size_per_thread`、`threads_per_warp`、`warps_per_cta`、`order`，外加可选的 `cga_layout`（`_layouts.py:60-118`）。教程里的 `BlockedLayout([2,4], [16,2], [2,2], [1,0])` block shape 是 `[64,16]`，`order=[1,0]` 表示行优先（`python/tutorials/gluon/02-layouts.py:21-28`）。

### 4. 显式 shared layout

shared memory 的布局和 distributed layout 是两套东西：

| Python 类 | 对应 encoding | 用途 |
|---|---|---|
| `NVMMASharedLayout` | `#ttg.nvmma_shared` | MMA/TMA 友好的 swizzle |
| `SwizzledSharedLayout` | `#ttg.swizzled_shared` | 通用 swizzle |
| `PaddedSharedLayout` | `#ttg.padded_shared` | padding 破 bank conflict |
| `SharedLinearLayout` | `#ttg.shared_linear` | 显式线性布局 |

`NVMMASharedLayout` 的字段是 `swizzle_byte_width`、`element_bitwidth`、`rank`、`transposed`、`fp4_padded`、`cga_layout`（`_layouts.py:357-405`）。`get_default_for` 按 contiguous 维度的字节数选 128/64/32/0 字节 swizzle：能整除 128 就 128，否则 64，再否则 32，都不满足就 0（`_layouts.py:407-444`）。

### 5. shared memory descriptor

`gl.allocate_shared_memory(element_ty, shape, layout, value=None)` 构造 `shared_memory_descriptor_type`，再调 `create_local_alloc`，有 `value` 时把它作为初始化写进 shared（`_semantic.py:310-320`）。`shared_memory_descriptor` 有 `load(layout)`、`store(value)`、`slice`、`index`、`permute`、`reshape`、`reinterpret` 等方法（`_core.py:373-606`）；`load` 显式要求目标 distributed layout，内部调 `create_local_load`（`_semantic.py:322-327`）。

### 6. 显式 warp specialization

`gl.warp_specialize(functions_and_args, worker_num_warps, worker_num_regs=None)` 把执行分成 default partition 和若干 worker partition（`_core.py:741-765`）。`GluonSemantic.warp_specialize` 会：

1. 先编译 default partition，拿到 result types；
2. 创建 `WarpSpecializeOp`；
3. 为每个 worker 建 region，设置 `worker_num_warps` 和可选的 `worker_num_regs`；
4. 在 default region 里 `warp_yield`，在 worker region 里 `warp_return`。

源码依据：`python/triton/experimental/gluon/language/_semantic.py:638-696`。

教程明确说：Gluon 的 warp specialization 只在 Hopper 及更新 GPU 上支持（`python/tutorials/gluon/08-warp-specialization.py:20`）。T4 上不要尝试。

### 7. AutoLayout / CoalescedLayout 是逃生口

Gluon 不是“所有 layout 都必须手写”。你可以写 `AutoLayout()`，让 `ResolveAutoEncodings` 从相邻 op 推断；也可以写 `CoalescedLayout()`，让 `InferCoalescedEncodings` 按 load/store 选 coalesced layout。

`ResolveAutoEncodings` 用 `set_auto_layout` 作为种子，收集 `SetAutoLayoutOp` 的 src 和 encoding 再传播（`ResolveAutoEncodings.cpp:25-40`）。`InferCoalescedEncodings` 只处理 `CoalescedEncodingAttr` 的指针 tensor（`InferCoalescedEncodings.cpp:36-92`）。

## 底层机制

### 1. 从 Python 到 `glir`

`GluonASTSource.make_ir` 先建 module，并设置 `ttg.target`、`ttg.num-warps`、`ttg.num-ctas`、`ttg.threads-per-warp` 四个属性（`_runtime.py:26-48`）。

然后调用 `ast_to_ttir`，把 Python AST 直接生成带 Gluon encoding 的模块。注意它复用了 Triton 的 `ast_to_ttir` 和 `CodeGenerator`，只是 `GluonSemantic` 负责 layout 检查和 op 构造。

### 2. `glir -> ttgir` 的 stage 表

NVIDIA 后端的 `add_stages` 按 language 分叉：Triton 注册 `ttir`/`ttgir`；Gluon 注册 `glir`（恒等）和 `glir -> ttgir` 的 `gluon_to_ttgir`；之后的 `llir`/`ptx`/`cubin` 完全共用（`compiler.py:660-670`）。

`glir` stage 是恒等函数，因为 `src` 已经是解析好的模块。之后 `ttgir` 开始就与 Triton 汇合。

### 3. `gluon_to_ttgir` 不重新选 layout

`gluon_to_ttgir` 的 pass 列表（`compiler.py:394-420`）包括：

- `gluon.add_inliner`
- `gluon.add_infer_coalesced_encodings`
- `gluon.add_resolve_auto_encodings`
- `ttnvgpuir.add_tma_lowering`
- `gluon.add_canonicalizer`
- `common.add_sccp`
- `ttir.add_loop_aware_cse`
- `ttgpuir.add_combine_tensor_select_and_if`
- `common.add_symbol_dce`
- `ttnvgpuir.add_check_matmul_two_cta`

它**没有** `ttir.add_convert_to_ttgpuir`，也**没有** `ttgpuir.add_coalesce`、`add_accelerate_matmul`、`add_remove_layout_conversions` 这一整套自动优化。Gluon 的 layout 在 `glir` 里已经确定；这些 pass 只做局部修补和规范化。

### 4. layout 合法性在 Python 侧就检查

`distributed_type.__init__` 检查 layout 必须是 `DistributedLayout`，且非 `AutoLayout`/`CoalescedLayout` 时 rank 必须等于 tensor rank（`_core.py:233-245`）。`shared_memory_descriptor_type.__init__` 检查 layout 必须是 `SharedLayout`（`_core.py:267-277`）。

`GluonSemantic.allocate_shared` 还会检查 `shape` 是 int list、`layout` 是 `SharedLayout`（`_semantic.py:310-320`）。这些检查发生在生成 IR 之前，错误信息比 MLIR verifier 更直接。

### 5. shared memory 仍然走同一套 Allocation

`gl.allocate_shared_memory` 最终调用 `create_local_alloc`，生成的就是 `ttg.local_alloc`。所以 Ch 27 的 `Allocation`、`Membar`、`AllocateSharedMemory` 全部复用：

```text
gl.allocate_shared_memory
  -> ttg.local_alloc
  -> AllocateSharedMemory pass
  -> allocation.offset / ttg.shared
  -> LLVM global_smem + GEP
```

**源码依据**：`_semantic.py:310-320` 的 `create_local_alloc`；`third_party/nvidia/backend/compiler.py:442-447` 的 pass 顺序。

区别只在于：Triton 的 `local_alloc` 多由 `convert_to_ttgpuir`/pipeline pass 自动插入；Gluon 的 `local_alloc` 由用户显式调用 `allocate_shared_memory` 产生。

### 6. warp specialization 的 lowering

`GluonSemantic.warp_specialize` 创建 `WarpSpecializeOp`，把 default block 放进 default region，设置 `requested_registers`，再创建 `WarpSpecializePartitionsOp` 并为每个 worker 建 region（`_semantic.py:669-692`）。

后续由 `add_warp_specialize_to_llvm`、`add_nvgpu_to_llvm` 等 pass 降低（`compiler.py:462-463`）。这条路径要求 Hopper+ 的 warpgroup/register reallocation 支持；T4 不满足。

### 7. 绕过自动 layout 选择的具体含义

Triton 的 `make_ttgir` 一开始就跑 `convert_to_ttgpuir`，把 TTIR 的“无 encoding tensor”变成 TTGIR 的“有 encoding tensor”。这个 pass 会根据 op、shape、num_warps、coalesce 启发式选 layout。

Gluon 的 `glir` 里 tensor 已经有 encoding，`gluon_to_ttgir` 不会再跑 `convert_to_ttgpuir`。因此：

- 你写 `BlockedLayout([1], [32], [4], [0])`，TTGIR 就是 `#ttg.blocked<...>`；
- 你写 `NVMMASharedLayout(128, 16, 2)`，shared memory 就是 128 字节 swizzle；
- 你写 `AutoLayout()`，只有这部分会由 `ResolveAutoEncodings` 推断；
- 你写 `CoalescedLayout()`，只有 load/store 相关部分会由 `InferCoalescedEncodings` 推断。

这就是“显式 layout 前端”的核心：**默认不猜，猜的地方由你显式标记**。

## 进一步用法/重要变体

### 1. 一颗最小 Gluon kernel（guided）

```python
from triton.experimental import gluon
from triton.experimental.gluon import language as gl

@gluon.jit
def copy_tile(src, dst, BLOCK: gl.constexpr):
    layout: gl.constexpr = gl.BlockedLayout([1], [32], [4], [0])
    offs = gl.arange(0, BLOCK, layout=layout)
    x = gl.load(src + offs)
    gl.store(dst + offs, x)
```

对应的 `glir` 形状（**guided**，不是实际 dump）：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>

module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32,
                   "ttg.target" = "cuda:75", "ttg.threads-per-warp" = 32 : i32} {
  tt.func public @copy_tile(%src: !tt.ptr<f32>, %dst: !tt.ptr<f32>) {
    %pid = tt.get_program_id x : i32
    %offs = tt.make_range {start = 0 : i32, end = 128 : i32} : tensor<128xi32, #blocked>
    %base = arith.muli %pid, %c128 : i32
    %s = tt.splat %base : i32 -> tensor<128xi32, #blocked>
    %idx = arith.addi %s, %offs : tensor<128xi32, #blocked>
    %sp = tt.splat %src : !tt.ptr<f32> -> tensor<128x!tt.ptr<f32>, #blocked>
    %dp = tt.splat %dst : !tt.ptr<f32> -> tensor<128x!tt.ptr<f32>, #blocked>
    %sa = tt.addptr %sp, %idx : tensor<128x!tt.ptr<f32>, #blocked>, tensor<128xi32, #blocked>
    %da = tt.addptr %dp, %idx : tensor<128x!tt.ptr<f32>, #blocked>, tensor<128xi32, #blocked>
    %v = tt.load %sa : tensor<128x!tt.ptr<f32>, #blocked>
    tt.store %da, %v : tensor<128x!tt.ptr<f32>, #blocked>
    tt.return
  }
}
```

这段 `glir` 的关键不是逐字正确，而是：`tensor` 类型上已经有 `#blocked`；没有 `ttg.convert_layout`；`tt.func` 的 module 属性里带 `ttg.target`/`ttg.num-warps`。

### 2. 显式 shared memory（guided）

```python
@gluon.jit
def copy_through_smem(src, dst, BLOCK: gl.constexpr):
    layout: gl.constexpr = gl.BlockedLayout([1], [32], [4], [0])
    smem_layout: gl.constexpr = gl.SwizzledSharedLayout(4, 1, 8, [0])
    offs = gl.arange(0, BLOCK, layout=layout)
    x = gl.load(src + offs)
    smem = gl.allocate_shared_memory(gl.float32, [BLOCK], smem_layout, x)
    gl.barrier()
    y = smem.load(layout)
    gl.store(dst + offs, y)
```

这里发生了三件事：

1. `allocate_shared_memory` 生成 `ttg.local_alloc`，`value=x` 表示把 `x` 写进 shared；
2. `gl.barrier()` 生成 CTA barrier，保证其他 warp 能读到；
3. `smem.load(layout)` 生成 `ttg.local_load`，目标 layout 由你指定。

**guided**：`gl.barrier()` 在 `_core.py:785-796`，`num_ctas==1` 时走 `debug_barrier`；多 CTA 且 `cluster=True` 时才走 cluster barrier。

### 3. `NVMMASharedLayout`（guided）

```python
smem_layout: gl.constexpr = gl.NVMMASharedLayout(
    swizzle_byte_width=128,
    element_bitwidth=16,
    rank=2,
)
smem = gl.allocate_shared_memory(gl.float16, [128, 64], smem_layout)
```

`NVMMASharedLayout` 的断言：`element_bitwidth in [8,16,32,64]`，`swizzle_byte_width in [0,32,64,128]`；`fp4_padded` 只允许 128 字节 swizzle 和 8-bit 元素（`_layouts.py:390-395`）。

如果你不想手算 swizzle，可以用：

```python
smem_layout = gl.NVMMASharedLayout.get_default_for([128, 64], gl.float16)
```

它按 contiguous 维度的字节数选最大兼容 swizzle（`_layouts.py:407-444`）。

### 4. 显式 warp specialization（guided，Hopper+）

```python
@gluon.jit
def default_partition(a, b, out):
    ...

@gluon.jit
def load_partition(a, b, buffers, barriers):
    ...

@gluon.jit
def kernel(a, b, out):
    gl.warp_specialize(
        [(default_partition, (a, b, out)),
         (load_partition, (a, b, buffers, barriers))],
        [1],        # worker_num_warps
        [24],       # worker_num_regs
    )
```

规则：

- `functions_and_args[0]` 是 default partition，必须有；它使用 kernel 的 `num_warps`；
- 后面的都是 worker partition，`worker_num_warps` 的长度必须等于 worker 数量；
- `worker_num_regs` 可选，长度也必须匹配；
- 不能递归调用 `warp_specialize`。

源码依据：`_core.py:741-765`、`_semantic.py:638-696`；教程 `08-warp-specialization.py:67-120`。

### 5. AutoLayout / CoalescedLayout 的用法（guided）

```python
@gluon.jit
def copy_auto(src, dst, BLOCK: gl.constexpr):
    offs = gl.arange(0, BLOCK, layout=gl.AutoLayout())
    x = gl.load(src + offs)
    x = gl.set_auto_layout(x, gl.BlockedLayout([1], [32], [4], [0]))
    gl.store(dst + offs, x)
```

`set_auto_layout` 只能用于 `AutoLayout` 的输入：

```python
_check(isinstance(src_ty.layout, AutoLayout), ...)
handle = self.builder.create_set_auto_layout(layout._to_ir(self.builder), value.handle)
```

源码依据：`_semantic.py:459-467`。

`CoalescedLayout` 更适合 load/store：

```python
offs = gl.arange(0, BLOCK, layout=gl.CoalescedLayout())
```

它会让 `InferCoalescedEncodings` 根据指针的 contiguity/divisibility 选 layout（`InferCoalescedEncodings.cpp:36-92`）。

### 6. 用 `.glir` 做 IRSource

`IRSource` 根据扩展名决定 language：

```python
self.language = Language.GLUON if self.ext == "glir" else Language.TRITON
```

源码依据：`python/triton/compiler/compiler.py:87-93`。

`parse_options` 对 `glir`/`ttgir` 读取 `ttg.num-warps` 和 `ttg.num-ctas`：

```python
if self.ext in ("glir", "ttgir"):
    num_warps = self.module.get_int_attr("ttg.num-warps")
    assert num_warps is not None, "Unable to parse ttg.num-warps attribute"
```

源码依据：`python/triton/compiler/compiler.py:121-129`。

这意味着你可以把 `glir` dump 出来，改一改，再用 `IRSource` 从 `glir` 开始编译。但 `glir` 的 module 属性必须齐全，否则 `parse_options` 会断言。

## 对比与取舍

| 维度 | Triton | Gluon | 取舍 |
|---|---|---|---|
| layout | 自动 | 显式 | Gluon 可控但易错 |
| shared memory | 编译器插 | 用户分配 | Gluon 可复用/可 swizzle |
| warp specialization | 自动 pass | 显式 region | Gluon 可控但要求 Hopper+ |
| 首级 IR | `ttir` | `glir` | Gluon 调试要读带 encoding 的 IR |
| 后端 | 同一套 | 同一套 | 性能上限相同，差距在控制力 |
| 稳定性 | 较高 | `experimental` | Gluon API 可能变 |
| T4 | 支持基础特性 | 基础代码 guided | Hopper/Blackwell 特性不可用 |

**什么时候该用 Gluon**：

- 你已经在 Triton 上调过 layout，知道瓶颈是某次 `convert_layout` 或 bank conflict；
- 你要手写 pipeline/shared memory 复用，Triton 的自动分配不满足；
- 你要做 warp specialization、TMA、WGMMA/TMEM 等底层实验；
- 你在做后端/编译器研究，需要稳定的 IR 入口。

**什么时候不该用**：

- 普通 elementwise/reduction/attention，Triton 的自动布局已经够好；
- 你需要跨 NVIDIA/AMD 的可移植性；
- 你不能接受 `experimental` API 的变动；
- 你的目标硬件是 T4，而你想用 Hopper/Blackwell 特性。

## 踩雷集锦

1. **layout rank 和 tensor rank 不匹配。**
   `distributed_type.__init__` 会直接 assert：`tensor shape and layout rank mismatch`（`_core.py:242-245`）。`BlockedLayout` 的 `size_per_thread`/`threads_per_warp`/`warps_per_cta`/`order` 长度都必须等于 rank。

2. **写了 `AutoLayout` 却没有 `set_auto_layout`。**
   `ResolveAutoEncodings` 需要种子；如果整张图都是 `AutoLayout` 且没有 `set_auto_layout`，推断可能失败或给出你不想要的结果。显式写一个 `set_auto_layout` 作为锚点。

3. **把 `shared_memory_descriptor` 的 `shape` 和 `alloc_shape` 混了。**
   `allocate_shared_memory` 当前把两者都设成 `shape`；但 `memdesc_subslice`/`index` 会保留 `alloc_shape`。`nbytes_per_cta` 用 `shape` 和 layout 算（`_core.py:297-312`），不是简单 `numel * bitwidth`。

4. **在 T4 上跑 `warp_specialize`。**
   教程明确要求 Hopper 或更新（`08-warp-specialization.py:20`）。T4 没有 warpgroup/register reallocation 那套硬件支持，失败是预期行为。

5. **忘了显式 barrier/fence。**
   Gluon 把 shared memory 的控制权交给你，也把同步责任交给你。`allocate_shared_memory(value=x)` 之后直接 `load`，跨 warp 读可能读到旧数据；该加 `gl.barrier()` 就加。

6. **把 Gluon 当稳定 API。**
   它在 `python/triton/experimental/gluon/` 下，教程和 IR 都在快速演进；升级 Triton 时先跑 `python/tutorials/gluon/` 的测试，不要假设 `glir` 逐字兼容。

7. **用 `.glir` 做 IRSource 但漏了 module 属性。**
   `parse_options` 断言 `ttg.num-warps` 存在（`compiler.py:121-129`）。手写 `glir` 时至少要有 `ttg.num-warps`、`ttg.num-ctas`、`ttg.target`、`ttg.threads-per-warp`。

8. **以为 Gluon 会帮你做自动 layout 优化。**
   `gluon_to_ttgir` 不跑 `convert_to_ttgpuir`，也不跑 `coalesce`/`accelerate_matmul` 那一套。你写什么 layout，基本就是什么 layout。

## 进阶

- 读 `python/triton/experimental/gluon/language/_layouts.py` 的全部类，理解每个 layout 的 `_to_ir` 和 `mangle`。
- 读 `lib/Dialect/Gluon/Transforms/InferLayoutUtils.cpp`，看 `inferLayout` 如何前向/后向传播 encoding。
- 读 `python/triton/tools/triton_to_gluon_translator/`，它能把 Triton kernel 翻译成 Gluon，是学习 layout 对照的好工具。
- 读 `python/tutorials/gluon/02-layouts.py` 的 SASS 对比，理解 `BlockedLayout` 的 `size_per_thread` 如何影响 `LDG` 宽度。
- 读 `python/tutorials/gluon/08-warp-specialization.py`，看 mbarrier/fence 如何跨 partition 同步。

## 动手练习

1. **写一颗 Gluon copy**：用 `BlockedLayout([1], [32], [4], [0])` 写 1D copy，dump `glir`，确认没有 `ttg.convert_layout`。
2. **改 layout**：把 `size_per_thread` 从 `[1]` 改成 `[4]`，观察 `glir` 和 SASS 的 `LDG` 宽度变化（需要可运行环境）。
3. **显式 shared memory**：用 `allocate_shared_memory` + `barrier` + `load` 写一颗 through-smem copy，解释每个 barrier 防的是什么。
4. **NVMMASharedLayout**：用 `get_default_for([128, 64], gl.float16)` 生成 layout，打印 `swizzle_byte_width`，再手算 contiguous 维度的字节数验证。
5. **AutoLayout**：把 `arange` 的 layout 改成 `AutoLayout()`，加一个 `set_auto_layout`，对比 `glir` 里 encoding 是否被补齐。

## 本章重点

- Gluon 与 Triton 共用 NVIDIA backend、Allocation/Membar、LLVM/NVPTX/ptxas；差异在首级 IR 和 layout 契约。
- `Language.GLUON` + `GluonASTSource` 产生 `glir`；stage 表是 `glir -> ttgir -> llir -> ptx -> cubin`。
- `gluon_to_ttgir` 不跑 `convert_to_ttgpuir`，所以显式 layout 不会被自动重选。
- `BlockedLayout`/`NVMMASharedLayout`/`SwizzledSharedLayout` 直接对应 TTGIR encoding。
- `allocate_shared_memory` 生成 `ttg.local_alloc`，复用 Ch 27 的 Allocation/Membar。
- `warp_specialize` 是显式 region，要求 Hopper+；T4 不可用。
- Gluon 在 `experimental` 下，API/IR 稳定性风险高；用前先跑教程和测试。

## 自我检核

1. Gluon 和 Triton 的 stage 表在哪一行分叉？`glir` 之后哪一级汇合？
2. `gluon_to_ttgir` 为什么不跑 `convert_to_ttgpuir`？这带来什么自由和什么责任？
3. `BlockedLayout([1], [32], [4], [0])` 对应 TTGIR 的哪个 encoding？rank 必须满足什么？
4. `allocate_shared_memory` 最终生成哪个 TTGIR op？它复用哪套分配分析？
5. 什么硬件条件下才能用 `warp_specialize`？T4 上会发生什么？
6. Gluon 的 `experimental` 状态对升级 Triton 意味着什么？

## 延伸阅读

1. **`python/triton/experimental/gluon/language/_layouts.py:60-118`、`357-501`**
   - 读哪里：`BlockedLayout`、`NVMMASharedLayout`、`SwizzledSharedLayout`、`PaddedSharedLayout`。
   - 学什么：Python layout 对象如何映射到 TTGIR encoding，swizzle 参数的含义。
   - 与本章关系：这是 Gluon 显式 layout 的类型系统。

2. **`python/triton/experimental/gluon/language/_semantic.py:295-335`、`459-467`、`638-696`**
   - 读哪里：`convert_layout`、`allocate_shared`、`shared_load`、`set_auto_layout`、`warp_specialize`。
   - 学什么：Gluon 的 op 构造和合法性检查。
   - 与本章关系：这是 Python API 到 `glir` 的桥。

3. **`third_party/nvidia/backend/compiler.py:394-420`、`660-670`**
   - 读哪里：`gluon_to_ttgir` 和 `add_stages`。
   - 学什么：Gluon 的 pass 列表与 Triton 的差异，stage 如何分叉/汇合。
   - 与本章关系：解释“同一后端、不同前端契约”。

4. **`python/tutorials/gluon/02-layouts.py:164-300`**
   - 读哪里：`memcpy_1d_kernel` 和 SASS 对比。
   - 学什么：`BlockedLayout` 的 `size_per_thread` 如何影响 `LDG` 宽度和吞吐。
   - 与本章关系：用真实教程建立 layout 直觉。

5. **`python/tutorials/gluon/08-warp-specialization.py:67-180`**
   - 读哪里：partition 划分、mbarrier、fence。
   - 学什么：显式 warp specialization 的协议和同步责任。
   - 与本章关系：Gluon 把 Triton 自动 pass 的职责交还给用户。

## 下一章

下一章离开 NVIDIA 的独占路径，看 AMD 后端如何复用同一套 Triton 前端和 MLIR 基础设施，以及 `hsaco`、`amdgcn`、wavefront 大小、LDS 与 NVIDIA 的差异。

→ [Ch 30 AMD 后端与可移植性](./30-amd-backend-and-portability.md)
