# Ch 16 — Pass 基础设施与 NVIDIA pipeline

> Pass 不是一个个孤立的优化器，而是一条有前后置条件的流水线。

## 目标

读完本章，你应该能：

- 说清一个 pass 从 `Passes.td` 声明，到 `GEN_PASS_DECL` / `GEN_PASS_DEF` / `GEN_PASS_REGISTRATION`，再到 `triton-opt` 可见，完整经过哪几步；
- 读懂 `Pass<"cli-arg", "mlir::ModuleOp">`、`options`、`dependentDialects` 各自约束了什么；
- 沿着 `third_party/nvidia/backend/compiler.py` 的 `make_ttir` / `make_ttgir` / `make_llir` / `add_stages`，画出 NVIDIA 后端的真实 pipeline；
- 解释为什么 `assign-latencies → schedule-loops → pipeline`、`pipeline → tma-lowering`、`fence-insertion → lower-mma` 这些顺序不能随便换；
- 判断一次 pass 移动会破坏哪个前置条件，以及 analysis 什么时候失效；
- 用 `triton-opt --help`、`--mlir-print-ir-after-all` 定位“哪个 pass 改坏了 IR”。

本章不教你写某个具体优化，而是教你读懂“优化器是如何被组织和调度的”。Ch 17 再进入 TTIR 层的具体 rewrite pattern。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5 |
| 驱动 | 535.216.01 |
| CUDA | 12.4，`V12.4.131` |
| 系统 Python | 3.10.12 |

**源码依据**：

- `include/triton/Dialect/TritonGPU/Transforms/Passes.td:205-227`：`TritonGPUPrefetch` 的声明、description、`dependentDialects`。
- `include/triton/Dialect/TritonGPU/Transforms/Passes.h:13-18`：`GEN_PASS_DECL` 与 `GEN_PASS_REGISTRATION`。
- `lib/Dialect/TritonGPU/Transforms/Prefetch.cpp:56-57`、`727-761`：`GEN_PASS_DEF_TRITONGPUPREFETCH` 与 `impl::TritonGPUPrefetchBase`。
- `third_party/nvidia/backend/compiler.py:291-304`、`307-392`、`422-472`、`660-673`：`make_ttir`、`make_ttgir`、`make_llir`、`add_stages`。
- `bin/triton-opt.cpp:5-11`：`MlirOptMain` 入口。
- `bin/RegisterTritonDialects.h:69-75`：pass 注册。

**guided**：本机没有从该 commit 构建出的 `triton-opt`（`/home/ubuntu/aicomp/triton/build` 里没有可执行文件），所以本章所有 `triton-opt ...` 命令都标 `guided`。命令本身的语法来自 MLIR 的 `MlirOptMain` 约定，不是本机实测输出。

## 为什么需要这个

Triton 的 bug 经常不是“某个 pattern 写错了”，而是“pass 在错误的时机跑了”：

- `remove-layout-conversions` 在 `allocate-shared-memory` 之后跑，可能引入新的 shared 需求，但 offset 已经算完；
- `tma-lowering` 在 `pipeline` 之前跑，pipeliner 看不到高层的 `descriptor_load`，TMA copy 不会被多缓冲；
- `fence-insertion` 在 `lower-mma` 之后跑，可能漏掉 MMA 相关的 proxy fence；
- 新写的 pass 忘记 `dependentDialects`，在 `triton-opt` 里单独跑没事，放进多线程 pipeline 就崩；
- 新 pass 改了 CFG，却继续用旧的 `DominanceInfo`，结果 hoist 到错误的位置。

这些问题都不会在“单元测试单个 pattern”时暴露。你必须把 pass 放回 pipeline 里看。

## 先建立直觉

NVIDIA 后端的 stage 顺序在 `add_stages` 里写死（`compiler.py:660-673`）：

```text
ttir  -> make_ttir
ttgir -> make_ttgir
llir  -> make_llir
ptx   -> make_ptx
cubin -> make_cubin
```

每个 stage 是一个 Python 函数，接收上一 stage 的 module 和 metadata，返回新 module。`CompiledKernel` 按 stage 名字依次调用，cache key 也按 stage 组织。

把 `make_ttgir` 的 pass 按职责分组，可以得到这张图：

```text
convert_to_ttgpuir            # TTIR -> TTGIR，分配 layout
  -> coalesce                 # 合并访问
  -> accelerate_matmul        # dot -> MMA
  -> remove_layout_conversions
  -> optimize_dot_operands
  -> loop_aware_cse
  -> [sm80] prefetch
  -> [sm90+] tma_lowering
  -> fence_insertion
  -> lower_mma
  -> ...
```

注意这不是严格线性：`remove_layout_conversions` 在 `make_ttgir` 里出现多次（`322`、`324`、`328`、`372`），因为每次 layout 变化后都需要再清一遍转换。pipeline 是一个“反复收敛”的过程，而不是“每个 pass 只跑一次”。

## 核心概念

### 1. Pass 的三种身份

一个 pass 在源码里有三个身份：

| 身份 | 在哪里 | 作用 |
|---|---|---|
| 声明 | `Passes.td` 的 `def X : Pass<...>` | 生成 CLI 名、options、dependentDialects |
| 定义 | `X.cpp` 的 `GEN_PASS_DEF_X` + `class XPass : impl::XBase<XPass>` | 实现 `runOnOperation` |
| 注册 | `Passes.h` 的 `GEN_PASS_REGISTRATION` + `RegisterTritonDialects.h` | 让 `triton-opt` / pass manager 能找到它 |

三者缺一不可。只写 `Passes.td` 不写 `GEN_PASS_DEF`，链接时找不到 base class；只写实现不注册，`triton-opt --help` 里看不到，`compiler.py` 也调不到。

### 2. `Passes.td` 的解剖

以 `TritonGPUPrefetch` 为例（`Passes.td:205-227`）：

```tablegen
def TritonGPUPrefetch : Pass<"tritongpu-prefetch", "mlir::ModuleOp"> {
  let summary = "prefetch";
  let description = [{ ... }];
  let dependentDialects = ["mlir::triton::gpu::TritonGPUDialect",
                           "mlir::scf::SCFDialect",
                           "mlir::arith::ArithDialect"];
}
```

- `Pass<"tritongpu-prefetch", "mlir::ModuleOp">`：第一个参数是 CLI flag，第二个是 pass 作用的 op 类型。这里要求整个 module，所以 pass manager 会把它放在 module 层。
- `summary` / `description`：`--help` 里显示，也是文档来源。
- `dependentDialects`：pass 运行前必须加载的 dialect。Prefetch 会创建 `scf.for`、`arith` 和 `ttg` op，所以三个都要列。
- `options`：可选。比如 `TritonGPUPipeline` 有 `numStages` 和 `dumpIntermediateSteps`（`Passes.td:26-33`）。

带 option 的 pass 在 CLI 上可以这样传：

```bash
# guided
triton-opt input.ttgir --tritongpu-pipeline='num-stages=4'
```

### 3. `GEN_PASS_DECL` / `GEN_PASS_DEF` / `GEN_PASS_REGISTRATION`

`Passes.h` 通常长这样（`Passes.h:13-18`）：

```cpp
#define GEN_PASS_DECL
#include "triton/Dialect/TritonGPU/Transforms/Passes.h.inc"

#define GEN_PASS_REGISTRATION
#include "triton/Dialect/TritonGPU/Transforms/Passes.h.inc"
```

- `GEN_PASS_DECL`：为 `Passes.td` 里的每个 pass 生成**声明**（包括 `createX()` 和 option struct）。
- `GEN_PASS_REGISTRATION`：生成 `registerTritonGPUPasses()`，把所有 pass 注册到 `PassRegistry`。
- `GEN_PASS_DEF_<NAME>`：在实现文件里生成 `impl::<Name>Base`，你的 pass 类继承它并实现 `runOnOperation`。

`Prefetch.cpp:56-57` 先 `#define GEN_PASS_DEF_TRITONGPUPREFETCH` 再 include `Passes.h.inc`，生成 `impl::TritonGPUPrefetchBase`；`727-761` 的 `PrefetchPass` 继承它并实现 `runOnOperation`。命名规则是“`def` 名全大写，非字母数字变下划线”：`TritonGPUPrefetch` → `GEN_PASS_DEF_TRITONGPUPREFETCH`。写错一个字母，编译器会告诉你 base class 不存在。

### 4. `triton-opt` 如何注册

`bin/triton-opt.cpp:5-11` 只做三件事：建 `DialectRegistry`、调 `registerTritonDialects(registry)`、把 registry 交给 `mlir::MlirOptMain`。真正的注册在 `RegisterTritonDialects.h:69-75`：

```cpp
inline void registerTritonDialects(mlir::DialectRegistry &registry) {
  mlir::triton::NVIDIA::registerTargetInfo();
  mlir::triton::AMD::registerTargetInfo();
  mlir::registerAllPasses();
  mlir::triton::registerTritonPasses();
  mlir::triton::gpu::registerTritonGPUPasses();
  mlir::triton::nvidia_gpu::registerTritonNvidiaGPUPasses();
  ...
}
```

所以 `triton-opt --help` 里能看到的 pass，来自 `registerAllPasses()`（MLIR 自带）加上三个 Triton 注册函数。你新加的 pass 只要进了对应 group 的 `GEN_PASS_REGISTRATION`，就会自动出现。

### 5. `add_stages` 的契约

`add_stages` 不执行编译，只把 stage 名字映射到函数（`compiler.py:660-673`）：

```text
# 伪代码：省略号代表真实参数；关键是 stage 名到函数的映射
stages["ttir"]  = lambda src, md: self.make_ttir(...)
stages["ttgir"] = lambda src, md: self.make_ttgir(...)
stages["llir"]  = lambda src, md: self.make_llir(...)
stages["ptx"]   = lambda src, md: self.make_ptx(...)
stages["cubin"] = lambda src, md: self.make_cubin(...)
```

关键点：

- Triton 和 Gluon 共享 `llir` / `ptx` / `cubin`，但前半段不同；
- `glir` 是 Gluon 的输入 stage，直接返回 `src`；
- 每个 lambda 的签名是 `(src, metadata)`，metadata 在 stage 之间累积（比如 `shared`、`num_warps`、`tensordesc_meta`）；
- `add_stages_inspection_hook` 允许外部在 stage 注册后插入检查（`671-672`）。

## 底层机制

### 1. `make_ttir`：TTIR 层的固定顺序

`make_ttir` 只有十行左右（`compiler.py:291-304`），但顺序很有讲究：

```text
# 伪代码：省略 pm/passes 前缀
add_inliner
[capability // 10 < 9] add_rewrite_tensor_descriptor_to_pointer
add_canonicalizer -> add_combine -> add_reorder_broadcast
add_cse -> add_symbol_dce -> add_loop_unroll
pm.run(mod, 'make_ttir')
```

逐步解释：

1. **inliner**：先把 `tt.call` 内联，否则后面的 pattern 看不到跨函数的值。
2. **descriptor → pointer（sm_90 以下）**：T4 没有 TMA，`tt.descriptor_load` 在这里被改写成普通 `tt.load`。
3. **canonicalizer**：清理常量、死 op、结构冗余，为 combine 提供规范形态。
4. **combine + reorder-broadcast**：Triton 特有的代数重写与 broadcast/splat 推迟（Ch 17 详述）。
5. **CSE + symbol-dce**：合并重复计算，删掉无用符号。
6. **loop-unroll**：只对带 `tt.loop_unroll_factor` 的循环生效。

顺序理由：先内联才能跨函数 combine；先 canonicalize 才能让 pattern 匹配规范形态；先 combine/reorder 再做 CSE，CSE 才能看到更多等价表达式；unroll 放最后，避免过早放大 IR。

### 2. `make_ttgir`：layout、循环、异步原语的交汇

`make_ttgir` 很长（`compiler.py:307-392`），可以按阶段读。

**第一段：进入 TTGIR 并分配 layout（315-331）**

```python
passes.ttir.add_convert_to_ttgpuir(pm, f"cuda:{capability}", opt.num_warps, 32, opt.num_ctas)
passes.ttgpuir.add_coalesce(pm)
passes.ttgpuir.add_f32_dot_tc(pm, emuTF32)
nvidia.passes.ttnvgpuir.add_assign_cga_layouts(pm)
passes.ttgpuir.add_remove_layout_conversions(pm)
nvidia.passes.ttnvgpuir.add_optimize_cta_locality(pm)
passes.ttgpuir.add_remove_layout_conversions(pm)
passes.ttgpuir.add_optimize_thread_locality(pm)
passes.ttgpuir.add_accelerate_matmul(pm)
nvidia.passes.ttnvgpuir.add_check_matmul_two_cta(pm)
passes.ttgpuir.add_remove_layout_conversions(pm)
passes.ttgpuir.add_optimize_dot_operands(pm, capability >= 80)
nvidia.passes.ttnvgpuir.add_optimize_descriptor_encoding(pm)
passes.ttir.add_loop_aware_cse(pm)
```

`convert_to_ttgpuir` 是分水岭：在此之前是 TTIR（没有 encoding），在此之后每个 tensor 都带 `#blocked` / `#mma` / `#dot_op` 等 encoding。后面的 `coalesce`、`remove_layout_conversions`、`optimize_dot_operands` 都在这个 encoding 空间里工作。

`optimize_descriptor_encoding` 必须在 `tma_lowering` 之前：它决定 descriptor 的 shared layout（swizzle），TMA lowering 再根据这个 layout 生成 `box_dim` 和 `swizzle_mode`。

**第二段：循环与 pipeline（332-361）**

```python
if capability // 10 in [8, 9]:
    passes.ttgpuir.add_fuse_nested_loops(pm)
    passes.common.add_canonicalizer(pm)
    passes.ttir.add_triton_licm(pm)
    passes.common.add_canonicalizer(pm)
    passes.ttgpuir.add_combine_tensor_select_and_if(pm)
    nvidia.passes.hopper.add_hopper_warpspec(pm, opt.num_stages, dump_enabled)
    passes.ttgpuir.add_assign_latencies(pm, opt.num_stages)
    passes.ttgpuir.add_schedule_loops(pm)
    passes.ttgpuir.add_pipeline(pm, opt.num_stages, dump_enabled)
elif capability // 10 >= 10:
    ...
else:
    passes.ttir.add_triton_licm(pm)
```

对 sm_80/90，顺序是 `fuse_nested_loops -> LICM -> combine_tensor_select_and_if -> hopper_warpspec (sm90) -> assign_latencies -> schedule_loops -> pipeline`。`assign_latencies` 给 load/dot 等 op 标注延迟，`schedule_loops` 根据延迟排循环体，`pipeline` 再按 `numStages` 展开。把 `pipeline` 提前，前两步的信息还没有，pipeliner 只能放弃或生成错误的 schedule。

**第三段：sm80 的 prefetch 与 TMA（362-371）**

```python
if capability // 10 == 8:
    passes.ttgpuir.add_prefetch(pm)
passes.ttgpuir.add_optimize_dot_operands(pm, capability >= 80)
passes.ttgpuir.add_coalesce_async_copy(pm)
nvidia.passes.ttnvgpuir.add_optimize_tmem_layouts(pm)
nvidia.passes.ttnvgpuir.add_tmem_load_reduce(pm)
if capability // 10 >= 9:
    nvidia.passes.ttnvgpuir.add_tma_lowering(pm)
if capability // 10 >= 10:
    nvidia.passes.ttnvgpuir.add_lower_clc(pm)
```

注意 `prefetch` 只在 sm_80 加；sm_90 用 warp specialization + TMA，不走这个 pass。`tma_lowering` 在 `pipeline` 之后，这样 pipeliner 先看到高层 load/descriptor，并把它们多缓冲；TMA lowering 再把高层 op 换成 `async_tma_copy_global_to_local` + mbarrier。

**第四段：收尾（372-392）**

```python
passes.ttgpuir.add_remove_layout_conversions(pm)
nvidia.passes.ttnvgpuir.add_interleave_tmem(pm)
passes.ttgpuir.add_reduce_data_duplication(pm)
passes.ttgpuir.add_reorder_instructions(pm)
passes.ttir.add_loop_aware_cse(pm)
passes.common.add_symbol_dce(pm)
nvidia.passes.ttnvgpuir.add_fence_insertion(pm, capability)
nvidia.passes.ttnvgpuir.add_lower_mma(pm)
nvidia.passes.ttnvgpuir.add_hoist_mbarrier_lifecycle(pm, capability)
passes.common.add_sccp(pm)
passes.common.add_cse(pm)
passes.common.add_canonicalizer(pm)
```

`fence_insertion` 在 `lower_mma` 之前：它要看到 async copy / TMA / shared 访问，才能判断哪里需要 proxy fence；`lower_mma` 之后这些高层 op 已经变成 LLVM/PTX 形态，fence 插入就太晚了。

### 3. `make_llir`：从 TTGIR 到 LLVM dialect

`make_llir`（`compiler.py:422-472`）的关键顺序：

```text
# 伪代码：箭头表示先后顺序
allocate_warp_groups -> scf_to_cf
allocate_shared_memory_nv -> allocate_tensor_memory
proxy_fence_insertion -> membar -> tmem_barrier_insertion
optimize_mbarrier_arrivals -> cluster_barrier_mbar_allocator
allocate_global_scratch_memory -> to_llvmir
```

几个硬约束：

- `allocate_shared_memory_nv` 必须在所有 shared 分配/layout 决策之后。它把 memdesc 的 liveness 变成具体的 `allocation.offset`；之后再引入新的 `local_alloc`，offset 就不对了。
- `allocate_tensor_memory` 同理，只对 sm_100+ 有意义。
- `membar` 和 `tmem_barrier_insertion` 要在 `to_llvmir` 之前，因为它们还要识别高层 op。
- `to_llvmir` 之后是 LLVM dialect，再经过 `nvvm_to_llvm`、`di_scope` 等，最后 `llvm.to_module` 转成 LLVM IR（`492-524`）。

### 4. `add_stages` 与 cache

`add_stages` 只是注册 stage 函数；真正执行在 `CompiledKernel` 的 `run` 里。每个 stage 的产物会进 cache，cache key 包含源码、options、环境、后端 hash。改一个 pass 的实现，后端 `.so` 变了，cache 自然失效；只改 Python 前端，`triton_key` 也会变（Ch 0/9）。

调试时常用 `TRITON_ALWAYS_COMPILE=1 TRITON_KERNEL_DUMP=1 python kernel.py`（guided）：强制重新编译，避免 cache hit 看不到 IR。

### 5. 为什么 pass 顺序不能乱

把依赖关系写成一张表：

| 后置 pass | 依赖的前置条件 | 破坏后的症状 |
|---|---|---|
| `pipeline` | `assign_latencies`、`schedule_loops` 已跑 | 没有 latency 信息，循环不展开或 schedule 错误 |
| `tma_lowering` | `optimize_descriptor_encoding` 已定 shared layout | descriptor 的 swizzle mode / box dim 不对 |
| `tma_lowering` | `pipeline` 已多缓冲 | TMA copy 只有单缓冲，性能回退 |
| `fence_insertion` | async/TMA/shared op 还在高层 | 漏 fence 或 fence 位置错 |
| `lower_mma` | fence/membar 已插入 | MMA 与 async copy 之间的可见性错误 |
| `allocate_shared_memory_nv` | 所有 `local_alloc` 已出现 | offset 冲突 / shared 超限 |
| `to_llvmir` | 所有 TTGIR op 已 lower | 未注册的 op 残留，conversion 失败 |

一个具体的“移动 pass”实验：把 `add_tma_lowering` 从 `pipeline` 之后移到 `pipeline` 之前。TMA lowering 会把 `tt.descriptor_load` 换成 `async_tma_copy_global_to_local` + mbarrier；pipeliner 原本可以在高层把 load 分成多个 stage，现在看到的是已经带 barrier 的 TMA op，它要么不识别，要么无法正确分配 phase。结果可能仍然正确，但没有多缓冲；也可能因为 phase 不匹配而挂死。

### 6. analysis 失效与 `dependentDialects`

**analysis 失效**：MLIR 的 pass manager 会在 pass 修改 IR 后让缓存的 analysis 失效，除非 pass 显式 `markAnalysesPreserved`。`LoopAwareCSE` 是一个很好的例子（`LoopAwareCSE.cpp:136-145`）：

```cpp
auto &domInfo = getAnalysis<DominanceInfo>();
eliminateCommonSubexpressions(rewriter, domInfo, getOperation());
getOperation().walk(loopCSE);
eliminateCommonSubExpressions(rewriter, domInfo, getOperation());
```

它在 `loopCSE` 之后继续用同一个 `domInfo`，因为 `loopCSE` 只替换 iter arg / yield 值，不改变 CFG，dominance 关系不变。如果 `loopCSE` 删了 block 或改了分支，这个复用就是 bug。写新 pass 时，问自己：“我改了 CFG 吗？改了就必须重新 `getAnalysis`，或者明确 preserve。”

**`dependentDialects`**：pass 在 `runOnOperation` 里创建 op 时，dialect 必须已经加载。`Passes.td:224-226` 给 Prefetch 列了 `TritonGPUDialect`、`SCFDialect`、`ArithDialect`，因为它的 `runOnOperation` 会创建 `scf.for`、`arith` 和 `ttg` op。漏列的症状通常是：

- 单线程 `triton-opt` 偶尔能跑（dialect 被别的 pass 顺带加载了）；
- 多线程 pass manager 下断言失败，或 verifier 报 “operation is not registered”；
- 生成的 IR 在 `--verify-each` 下失败。

## 进一步用法/重要变体

### 用 `triton-opt` 找 pass

```bash
# guided
triton-opt --help | grep -E 'triton-(combine|reorder-broadcast|loop-unroll|licm)'
triton-opt --help | grep tritongpu-
```

`--help` 会列出所有注册的 pass 和它们的 option。想确认一个 pass 是否真的注册了，这是最快的方法。

### 看每个 pass 前后的 IR

```bash
# guided
triton-opt input.mlir --triton-combine --mlir-print-ir-after-all 2> combine.log
```

`--mlir-print-ir-after-all` 会把每个 pass 之后的 IR 打到 stderr。IR 很大时，用 `--mlir-print-ir-after=<pass-name>` 只看某个 pass；用 `--mlir-print-ir-before-all` 看输入。

### 只跑一个 pass

```bash
# guided
triton-opt input.ttgir --tritongpu-pipeline='num-stages=3' -o out.ttgir
```

`triton-opt` 默认会验证输入/输出；加 `-verify-each` 可以在每个 pass 后验证，定位“哪个 pass 产生了非法 IR”。

### 插入一个新 pass 的检查清单

1. 在 `Passes.td` 加 `def`，写清 `summary`、`description`、`options`、`dependentDialects`；
2. 在 `.cpp` 写 `#define GEN_PASS_DEF_<NAME>`，继承 `impl::<Name>Base<YourPass>`；
3. 在 `RegisterTritonDialects.h` 确认所属 group 的 `register...Passes()` 被调用；
4. 在 `compiler.py` 的对应 stage 加 `passes...add_<name>(pm, ...)`；
5. 用 `triton-opt --help` 确认可见，用 `--mlir-print-ir-after-all` 确认真的改了 IR，再跑 lit/端到端测试。

### 变体：ModuleOp pass vs FuncOp pass

`Pass<"name", "mlir::ModuleOp">` 在 module 层跑，能看到所有函数和全局符号；`Pass<"name", "mlir::func::FuncOp">` 在函数层跑，适合只关心单个 kernel 的优化。Triton 的多数 TTGIR pass 是 module pass，因为 layout/CGA/cluster 信息在 module 属性上（`ttg.num-warps`、`ttg.num-ctas`、`ttg.target`）。

## 对比与取舍

| 选择 | 优点 | 代价 | 什么时候用 |
|---|---|---|---|
| TableGen `Pass` | 自动生成 CLI、options、注册 | 改 td 要重新生成 | 绝大多数 pass |
| 手写 `PassWrapper` | 灵活，适合实验 | 注册/option 要自己写 | 临时原型、外部插件 |
| ModuleOp pass | 能看全局信息 | 作用域大，容易误改 | layout、CGA、TMA |
| FuncOp pass | 作用域小，易并行 | 看不到 module 属性 | 纯函数内优化 |
| canonicalizer | 永远安全、自动收敛 | 只做局部规范化 | 结构清理 |
| 专用 pass | 可做有利润假设的变换 | 需要自己证明合法性 | combine、pipeline、TMA |

canonicalizer 和专用 pass 的分工在 Ch 17 展开。这里只记住：canonicalizer 是“每个 op 自己知道怎么简化自己”，专用 pass 是“跨 op 的代数/调度变换”。

## 踩雷集锦

1. **忘记 `dependentDialects`。**
   pass 里 `scf::ForOp::create`，但 `Passes.td` 没列 `SCFDialect`。单跑可能没事，多线程 pipeline 下会崩。Prefetch 的 `dependentDialects` 是正面例子（`Passes.td:224-226`）。

2. **`GEN_PASS_DEF` 名字写错。**
   `TritonGPUPrefetch` 对应 `GEN_PASS_DEF_TRITONGPUPREFETCH`，不是 `GEN_PASS_DEF_TRITONGPUPREFETCHPASS`。写错会报 `impl::...Base` 未定义。

3. **pass 注册了，但没加进 `compiler.py`。**
   `triton-opt --help` 能看到，`triton.compile` 永远不跑。检查 `make_ttir` / `make_ttgir` / `make_llir` 里有没有 `add_<name>`。

4. **把 layout-changing pass 放到 `allocate_shared_memory_nv` 之后。**
   `make_llir` 里 `allocate_shared_memory_nv` 在 `442`，之后还有 `to_llvmir`。如果你在 `allocate_shared_memory_nv` 后面插入 `remove_layout_conversions`，它可能引入新的 `local_alloc`，但 shared offset 已经算完，IR 会非法或越界。

5. **把 `pipeline` 放在 `assign_latencies` / `schedule_loops` 之前。**
   `assign_latencies` 给 op 标延迟，`schedule_loops` 排循环体，`pipeline` 依赖这两步（`compiler.py:339-341`）。顺序反了，pipeliner 没有 schedule 可用。

6. **在 sm_90 上强行加 sm_80 的 `prefetch`。**
   `compiler.py:362-363` 只在 `capability // 10 == 8` 时加 prefetch。sm_90 走 Hopper warp specialization + TMA；强行加 prefetch 可能和 TMA barrier 的 phase 冲突。

7. **改了 CFG 还复用旧 `DominanceInfo`。**
   `LoopAwareCSE` 能复用是因为它不改 CFG（`LoopAwareCSE.cpp:136-145`）。你的 pass 如果删了 block/改了分支，必须重新 `getAnalysis<DominanceInfo>()`，否则 hoist 到错误位置。另外，`--mlir-print-ir-after-all` 输出在 stderr 且包含所有 pass；用 `2> log` 重定向，或 `--mlir-print-ir-after=<pass>` 精确看一个。

## 进阶

- 读 MLIR 的 `PassManager` / `OpPassManager` 文档，理解 nested pass、`addNestedPass`、`runPipeline` 的语义。Triton 的 `pm` 是 module 级，但内部可以嵌套函数级 pass。
- 读 `PassInstrumentation` 和 `compiler.py` 里的 `instrument(pm, point=...)`（`445`、`461`），理解 Triton 如何在不改 pass 的情况下插入调试/扩展点。
- 用 `-mlir-timing` 或 `-mlir-print-op-stats` 看每个 pass 的耗时/IR 规模，找出 pipeline 里的热点。
- 读 `nvidia/hopper/include/Transforms/Passes.h` 和 `hopper_warpspec`，对比 sm_90 warp specialization 与 sm_80 prefetch 如何替代彼此。

## 动手练习

1. **列 pass**：用 `triton-opt --help | grep tritongpu-` 列出所有 TTGIR pass，按 `make_ttgir` 的顺序标注它们出现的位置。**guided**。
2. **看 combine 前后**：写一个含 `addptr(addptr)` 的 TTIR，跑 `triton-opt --triton-combine --mlir-print-ir-after-all`，确认 `arith.addi` 出现。**guided**。
3. **移动 pass**：复制一份 `compiler.py`（不要改源码），把 `add_tma_lowering` 移到 `add_pipeline` 之前，预测会发生什么，再用 `--mlir-print-ir-after-all` 验证。**guided**。
4. **加一个空 pass**：在 `Passes.td` 加 `TritonGPUMyNoop`，实现 `runOnOperation` 只打印函数名，注册后在 `triton-opt` 里跑通。**guided**（需要构建）。
5. **analysis 实验**：写一个 pass，先 `getAnalysis<DominanceInfo>()`，再删一个 block，然后继续用旧 analysis，观察断言/错误；改成重新获取，确认修复。**guided**。
6. **读 pipeline**：不看注释，自己画出 `make_ttgir` 的 pass 依赖图，标出至少 5 条“必须先于”的边，再和本章的表对照。

## 本章重点

- 一个 pass 有声明（`Passes.td`）、定义（`GEN_PASS_DEF` + base class）、注册（`GEN_PASS_REGISTRATION` + `RegisterTritonDialects.h`）三个身份。
- `Pass<"cli", "Op">` 的第二个参数决定 pass 作用域；`options` 决定 CLI 参数；`dependentDialects` 决定运行前要加载哪些 dialect。
- `triton-opt` 通过 `MlirOptMain` + `registerTritonDialects` 暴露所有注册 pass。
- NVIDIA pipeline 的主干是 `ttir → ttgir → llir → ptx → cubin`，每个 stage 内部的 pass 顺序有硬依赖。
- `assign_latencies → schedule_loops → pipeline`、`pipeline → tma_lowering`、`fence_insertion → lower_mma`、`allocate_shared_memory_nv` 最后，是四条最重要的顺序约束。
- analysis 在 CFG 改变后必须重新获取；`dependentDialects` 漏写会在多线程/验证时暴露。

## 自我检核

1. `GEN_PASS_DECL`、`GEN_PASS_DEF_TRITONGPUPREFETCH`、`GEN_PASS_REGISTRATION` 分别生成什么？
2. 为什么 `TritonGPUPrefetch` 的 `dependentDialects` 要列 `SCFDialect` 和 `ArithDialect`？
3. `triton-opt` 的 pass 列表来自哪几个注册函数？
4. `make_ttir` 里为什么先 canonicalize 再 combine？反过来会怎样？
5. `tma_lowering` 为什么必须在 `pipeline` 之后？如果提前，最可能的结果是什么？
6. `allocate_shared_memory_nv` 之后为什么不能再引入新的 `local_alloc`？
7. `LoopAwareCSE` 为什么可以在 `loopCSE` 后复用 `DominanceInfo`？什么情况下不能复用？

## 延伸阅读

1. **`third_party/nvidia/backend/compiler.py:291-392`**
   - **读哪里**：`make_ttir` 和 `make_ttgir` 全文。
   - **学什么**：NVIDIA 后端每个 stage 的真实 pass 顺序，以及 sm_80/90/100 的分支差异。
   - **与本章关系**：这是本章的主线；先能逐行解释每个 `add_*` 为什么在那。

2. **`include/triton/Dialect/TritonGPU/Transforms/Passes.td:1-227`**
   - **读哪里**：`TritonGPUPipeline`、`TritonGPUPrefetch`、`TritonGPUAccelerateMatmul`。
   - **学什么**：pass 声明、options、`dependentDialects` 的写法。
   - **与本章关系**：把“pass 的三种身份”落到具体 TableGen。

3. **`lib/Dialect/TritonGPU/Transforms/Prefetch.cpp:56-57`、`727-761`**
   - **读哪里**：`GEN_PASS_DEF` 和 `PrefetchPass`。
   - **学什么**：一个 pass 如何从 TableGen base class 继承并实现 `runOnOperation`。
   - **与本章关系**：最小可读的 pass 定义样本。

4. **`bin/RegisterTritonDialects.h:69-75`**
   - **读哪里**：`registerTritonDialects` 的前几行。
   - **学什么**：`triton-opt` 如何把 MLIR 自带 pass 和 Triton 三组 pass 注册到一起。
   - **与本章关系**：解释“为什么 `--help` 里能看到某个 pass”。

5. **MLIR `Pass Management` 文档**
   - **读哪里**：PassManager、OpPassManager、PassInstrumentation、AnalysisManager。
   - **学什么**：analysis 失效、nested pass、instrumentation 的通用规则。
   - **与本章关系**：Triton 的 pass 基础设施是 MLIR 的直接使用；通用规则能解释本章的具体现象。

## 下一章

→ [Ch 17 TTIR 优化与 rewrite pattern](./17-ttir-optimizations-and-rewrite-patterns.md)：本章的 `make_ttir` 里已经出现了 `canonicalizer`、`combine`、`reorder-broadcast`、`loop-unroll`；下一章拆开这些 pass，看 `matchAndRewrite` 的契约、benefit 和 TableGen pattern 到底怎么工作。
