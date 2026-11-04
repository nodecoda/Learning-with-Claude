# Ch 30 — AMD 后端与可移植性

## 目标

把 Triton 的 AMD 路径和 NVIDIA 路径并排读一遍，能回答四个问题：TTIR/TTGIR 之后从哪里开始分叉；MFMA/WMMA 与 MMA/WGMMA 在 lowering 上差在哪；HIP/ROCm 的编译产物如何从 `libtriton_amd_codegen.so` 走到 `hsaco` 并加载；把一颗在 NVIDIA 上正确的 kernel 搬到 AMD 时，warp size、layout、atomic、cache 这四类坑分别会在哪一层炸。

## 环境/版本

Triton 3.9.0，commit `a972119e92a27b445cd160017ea461e56148590c`。本机只有 NVIDIA Tesla T4（`sm_75`），**没有 ROCm**。因此本章所有 AMD 运行时行为、ROCm 工具输出、MFMA/WMMA 实测性能一律标 `guided`；源码路径、pass 顺序、IR 结构按上述 commit 核对。`实测` 只用于本机能验证的源码事实与命令。

## 为什么需要这个

Triton 的卖点之一是“同一套 Python/TTGIR 前端，多个硬件后端”。但“同一套前端”不等于“同一套后端”。在源码里，AMD 不是 NVIDIA 后端的一个 flag，而是：

- 一个独立的 `HIPBackend`，有自己的 options、stage 名、binary 扩展名；
- 一套独立的 TTGIR pass 序列（`amd.passes.ttgpuir.*`）；
- 一个**独立 pin 的 LLVM/AMDGPU codegen 共享库**，通过 ctypes 调用；
- 一套独立的矩阵核心 layout：`AMDMfmaEncodingAttr` / `AMDWmmaEncodingAttr`；
- 一套独立的运行时加载路径：HIP module + `hsaco`。

这意味着可移植性问题很少是“把 `cuda` 改成 `hip`”就能解决的。更常见的是：layout 的线程映射假设了 32 线程/warp；atomic 的 scope 在 AMD 上不支持跨 CTA；cache modifier 的语义和编码不同；或者你写的 `tl.dot` 在 CDNA 上走 MFMA、在 RDNA 上走 WMMA、在 NVIDIA 上走 MMA/WGMMA，三者的 operand layout 和 K 维度约束都不一样。

本章的目标不是让你背 AMD 指令，而是让你在读到一个 AMD 相关 bug 时，能判断它属于“共享前端”“AMD TTGIR pass”“AMD lowering”“LLVM AMDGPU codegen”还是“HIP runtime”。

## 先建立直觉

先看两条流水线的形状。NVIDIA 的 stage 在 `third_party/nvidia/backend/compiler.py:660-672`，AMD 的在 `third_party/amd/backend/compiler.py:948-959`：

```text
NVIDIA:  ttir -> ttgir -> llir -> ptx -> cubin
AMD:     ttir -> ttgir -> llir -> amdgcn -> hsaco
```

`ttir` 和 `ttgir` 的名字相同，但 `ttgir` 的内部已经分叉：NVIDIA 的 `make_ttgir` 调用 `nvidia.passes.ttnvgpuir.*`，AMD 的 `make_ttgir` 调用 `amd.passes.ttgpuir.*`。再往下，NVIDIA 用 `llvm.translate_to_asm` 生成 PTX，再调 `ptxas`；AMD 用独立的 `libtriton_amd_codegen.so` 生成 AMDGCN 汇编，再用汇编器/链接器生成 `hsaco`。

一个有用的心智模型是：

> **共享的是“块级程序 + TTGIR 抽象”，分叉的是“矩阵核心 layout、目标相关 pass、LLVM 目标、二进制格式、加载器”。**

所以读 AMD 代码时，不要从 NVIDIA 的 pass 列表逐行对照；先问“这个阶段在 AMD 的 `add_stages` 里对应哪个函数”，再问“这个函数里哪些 pass 是 AMD 独有的”。

## 核心概念

### 1. `GPUTarget` 与 backend 契约

`python/triton/backends/compiler.py:9-15` 定义了后端选择的三元组：

```python
@dataclass(frozen=True)
class GPUTarget(object):
    backend: str      # "cuda" or "hip"
    arch: Union[int, str]  # 90, "gfx942", ...
    warp_size: int
```

`warp_size` 不是装饰：它进入 `convert_to_ttgpuir`，进入 TTGIR 的 `ttg.threads-per-warp`，进入 LLVM 的 `__oclc_wavefrontsize64`，也进入 launcher 的线程数检查。NVIDIA 固定 32；AMD 在 `HIPOptions.__post_init__` 里按架构算：

```python
# third_party/amd/backend/compiler.py:431-433
gfx_major = int(base_arch[3:-2])
warp_size = 32 if gfx_major >= 10 else 64
```

也就是说，CDNA（gfx9xx）是 wave64，RDNA3/4（gfx10xx/11xx/12xx）是 wave32。这个差异会贯穿整章。

### 2. `HIPOptions`：AMD 的编译旋钮

`third_party/amd/backend/compiler.py:376-458` 的 `HIPOptions` 和 `CUDAOptions`（`third_party/nvidia/backend/compiler.py:120-180`）不是一一对应。几个关键字段：

| 字段 | 默认 | 作用 | 与 NVIDIA 的差异 |
|---|---|---|---|
| `num_stages` | 2 | 软件流水线深度 | NVIDIA 默认 3 |
| `waves_per_eu` | 0 | 每个 SIMD 的 wave 数 | NVIDIA 用 occupancy/`maxnreg` 表达 |
| `matrix_instr_nonkdim` | 0 | 非 K 维矩阵指令尺寸 | NVIDIA 无此旋钮 |
| `kpack` | 1 | MFMA K 维打包 | gfx950 上已弃用并强制为 1（`:437-441`） |
| `schedule_hint` | `''` | 仅 `mfma-schedule`，gfx942/gfx950 | NVIDIA 没有同名机制 |
| `wgp_cu_mode` | `"wgp"` | gfx11/gfx120x 的 WGP/CU 模式 | NVIDIA 无 |
| `allow_flush_denorm` | `False` | 控制 f32 denorm flush | NVIDIA 走 `enable_reflect_ftz` |
| `default_dot_input_precision` | `"ieee"` | `tl.dot` 默认精度 | NVIDIA 默认 `"tf32"` |

这些默认值本身就是可移植性风险：同一个 `tl.dot` 在 NVIDIA 上默认 TF32，在 AMD 上默认 IEEE；同一个 `num_stages=3` 在 AMD 上可能根本不会启用你预期的流水线。

### 3. `HIPBackend`：目标、产物与 codegen

`HIPBackend` 在 `third_party/amd/backend/compiler.py:459-537`。几个必须记住的点：

- `supports_target` 只认 `target.backend == 'hip'`（`:463-465`）。
- `binary_ext = "hsaco"`（`:478`），不是 `cubin`。
- `supports_native_tensor_specialization = False`（`:460`），AMD 不做 NVIDIA 那套原生 tensor 特化。
- `get_codegen_implementation` 返回 `min_dot_size`（`:519-520`），用于语义层检查 `tl.dot` 的最小 shape。
- `get_module_map` 把 `triton.language.extra.libdevice` 映射到 `triton.language.extra.hip.libdevice`（`:522-526`），所以 `libdevice` 调用在 AMD 上走 HIP 实现。
- `parse_options` 会在 ConSan 打开时强制 `debug=True`、`sanitize_overflow=False`（`:483-487`），并拒绝不支持的 `num_ctas > 1`（`:494-495`）。

### 4. stage 契约

`add_stages` 的契约在 `python/triton/backends/compiler.py:37-52`：每个 stage 是 `(src, metadata) -> str|bytes`，按插入顺序执行，最后一个 stage 返回 `bytes` 给 launcher。AMD 的实现是：

```python
# third_party/amd/backend/compiler.py:948-959
stages["ttir"] = ...
stages["ttgir"] = ...
stages["llir"] = ...
stages["amdgcn"] = ...
stages["hsaco"] = ...
```

注意 `amdgcn` 是文本汇编，`hsaco` 是二进制。NVIDIA 对应的是 `ptx` 和 `cubin`。这个命名差异会直接出现在 `compiled.asm` 的 key 和 dump 文件后缀里。

## 底层机制

### 1. TTIR：先做目标无关的清理

AMD 的 `make_ttir` 在 `third_party/amd/backend/compiler.py:570-585`：

```python
passes.common.add_inliner(pm)
if not amd.supports_tdm(options.arch):
    passes.ttir.add_rewrite_tensor_descriptor_to_pointer(pm)
passes.common.add_canonicalizer(pm)
passes.ttir.add_combine(pm)
passes.ttir.add_reorder_broadcast(pm)
passes.common.add_cse(pm)
passes.ttir.add_triton_licm(pm)
passes.common.add_symbol_dce(pm)
passes.ttir.add_loop_unroll(pm)
```

这里最值得注意的是 `supports_tdm` 分支：支持 TDM（Tensor Data Movement）的 AMD 架构保留 tensor descriptor，不支持的架构先降成 pointer。这和 NVIDIA 在 `sm_90` 以下把 descriptor 降成 pointer 的逻辑（`third_party/nvidia/backend/compiler.py:291-304`）是同一个设计意图：**descriptor 是目标能力，不是前端语义**。

### 2. TTGIR：AMD 特有的矩阵、流水线和 buffer 优化

AMD 的 `make_ttgir` 在 `third_party/amd/backend/compiler.py:587-651`。先看入口：

```python
passes.ttir.add_convert_to_ttgpuir(pm, f"hip:{options.arch}",
                                   options.num_warps, options.warp_size,
                                   options.num_ctas)
```

这里把 `warp_size` 显式传进去，所以后面所有 layout 的 `threadsPerWarp` 都可能是 64。紧接着是 AMD 的 pass 序列：

```python
passes.ttgpuir.add_coalesce(pm)
passes.ttgpuir.add_f32_dot_tc(pm, emuTF32)
passes.ttgpuir.add_remove_layout_conversions(pm)
passes.ttgpuir.add_optimize_thread_locality(pm)
amd.passes.ttgpuir.add_accelerate_matmul(pm, options.arch,
                                         options.matrix_instr_nonkdim,
                                         options.kpack)
passes.ttgpuir.add_remove_layout_conversions(pm)
amd.passes.ttgpuir.add_optimize_epilogue(pm)
amd.passes.ttgpuir.add_optimize_dot_operands(pm, options.arch)
amd.passes.ttgpuir.add_hoist_layout_conversions(pm)
amd.passes.ttgpuir.add_sink_layout_conversions(pm)
```

`add_accelerate_matmul` 是 MFMA/WMMA 选择的核心入口；`add_optimize_dot_operands` 负责把 dot operand 变成矩阵指令需要的 layout；`hoist/sink layout conversions` 是 AMD 特有的“把 convert_layout 挪到更便宜的位置”的优化。再往后：

```python
amd.passes.ttgpuir.add_schedule_loops(pm, options.num_stages)
amd.passes.ttgpuir.add_pipeline(pm, use_async_copy, use_block_pingpong)
...
amd.passes.ttgpuir.add_convert_to_tensor_ops(pm)
...
if knobs.amd.use_buffer_ops:
    amd.passes.ttgpuir.add_canonicalize_pointers(pm)
    amd.passes.ttgpuir.add_convert_to_buffer_ops(pm, options.arch,
                                                 knobs.amd.use_buffer_atomics,
                                                 knobs.amd.buffer_ops_analyze_small_tensor_range)
```

`convert_to_buffer_ops` 是 AMD 的重要变体：它把普通 `tt.load/store` 变成 `amdg.buffer_load/store`，利用 AMD 的 buffer descriptor 和 cache policy。这个 pass 受 `AMDGCN_USE_BUFFER_OPS` 控制（`python/triton/knobs.py:520-527`），默认开启。它也是“同一段 TTGIR 在 AMD 上长得不一样”的典型原因。

### 3. LLIR：目标属性、ABI、外部库与 MFMA 调度

AMD 的 `make_llir` 在 `third_party/amd/backend/compiler.py:681-877`。前半段是 MLIR 层的 lowering：

```python
amd.passes.ttgpuir.add_update_async_wait_count(pm, options.arch)
amd.passes.ttgpuir.add_warp_pipeline_conversion(pm, options.arch)
passes.convert.add_scf_to_cf(pm)
passes.convert.add_index_to_llvmir(pm)
...
amd.passes.ttgpuir.add_allocate_shared_memory(pm, options.arch)
...
amd.passes.ttgpuir.add_membar(pm, options.arch)
...
amd.passes.ttgpuir.add_to_llvmir(pm, options.arch, __HIP_FTZ,
                                 options.wgp_cu_mode == "cu")
```

这里和 NVIDIA 的 `add_to_llvmir` + `add_nvgpu_to_llvm` 是平行关系，但 AMD 多了 `warp_pipeline_conversion`、`update_async_wait_count`、`membar` 的目标实现，以及 `__HIP_FTZ` 这个控制 denorm 的常量。

后半段是 LLVM 模块级设置：

```python
llvm_mod = llvm.to_module(mod, context)
target_triple = amd.get_target_triple(options.arch)
amd.attach_target_triple(llvm_mod, options.arch)
llvm.attach_datalayout(llvm_mod, target_triple, options.arch, target_features, ...)
...
amd.set_bool_control_constant(llvm_mod, "__oclc_wavefrontsize64",
                              options.warp_size == 64)
```

`__oclc_wavefrontsize64` 直接由 `warp_size` 决定（`:769`）。这就是为什么“warp size 只是运行时参数”是错的：它进入 LLVM 模块的控制常量，影响 device library 的实现选择。

函数属性也在这里设置：

```python
kernel_fn.add_fn_attr("amdgpu-flat-work-group-size",
                      f"1,{total_warps_num*options.warp_size}")
kernel_fn.add_fn_attr("uniform-work-group-size", "true")
if options.waves_per_eu != 0:
    kernel_fn.add_fn_attr("amdgpu-waves-per-eu",
                          f"{options.waves_per_eu},{options.waves_per_eu}")
```

`amdgpu-flat-work-group-size` 的上界是 `num_warps * warp_size`，不是 `num_warps * 32`。如果你在 AMD 上照抄 NVIDIA 的 occupancy 计算，就会少算一半线程。

### 4. AMDGCN 与 HSACO：独立 codegen 库

`make_amdgcn` 在 `third_party/amd/backend/compiler.py:880-931`。它先找 kernel 名，然后调用 `compile_amdgpu`：

```python
amdgcn = compile_amdgpu(src, target_triple, options.arch, features,
                        flags=flags,
                        enable_fp_fusion=options.enable_fp_fusion,
                        disable_optimization=disable_llvm_opt,
                        canonicalize_gep=False,
                        disabled_passes=disabled_passes,
                        dump_ir=dump_llvm_ir,
                        enable_timing=...)
```

`compile_amdgpu` 不是 Triton 主 LLVM 里的函数，而是通过 `libtriton_amd_codegen.so` 暴露的 C API。加载逻辑在 `third_party/amd/backend/compiler.py:20-118`：

```python
library = ctypes.CDLL(path, mode=RTLD_LOCAL | RTLD_NOW)
library.triton_amdgpu_compile.argtypes = [...]
```

这条设计非常重要：**AMD 后端可以 pin 一个与 Triton 主 LLVM 不同的 AMDGPU codegen 版本**。`get_amd_codegen_revision()`（`:109-110`）把这个 revision 纳入 backend hash（`:961-963`）。所以“同一个 Triton commit，AMD 产物可能随 codegen 库版本变化”是预期行为，不是缓存 bug。

`make_hsaco` 在 `:934-946`：先把 AMDGCN 汇编成 object，再 `amd.link_hsaco` 链接成 `hsaco`。ASAN 时加 `+xnack`（`:936-937`）。

### 5. `.so` 加载与 HIP runtime

编译期的 `.so` 是 `libtriton_amd_codegen.so`；运行期的 `.so` 是 `libhip`。运行期加载在 `third_party/amd/backend/driver.py`：

- `HIPUtils.__init__` 读 `driver.c`，替换 `libhip` 搜索路径，编译成 `hip_utils` 扩展（`:155-163`）。
- 暴露 `load_binary`、`launch`、`get_device_properties`、`create_tdm_descriptor`（`:163-170`）。
- `HIPLauncher` 在 `:276-332`，负责参数展开、scratch 分配、调用 `utils.launch`。

NVIDIA 的对应物是 `third_party/nvidia/backend/driver.py:264-353` 的 `CudaLauncher`。两者结构相似，但 AMD 的 launcher 多一个 `warp_size` 参数（`:321-323`），因为 launcher 要知道 wave 大小。

### 6. TritonGPU → LLVM：pattern 的优先级

`third_party/amd/lib/TritonAMDGPUToLLVM/TritonGPUToLLVM.cpp:71-235` 是 AMD 的 conversion pass。几个细节：

- `option.overrideIndexBitwidth(32)`（`:97-98`）：AMD 的 index 位宽默认 32。
- `initSharedMemory` 在 call/ret 转换之前（`:116-119`），因为 call 需要知道 shared memory base。
- AMD pattern 的 benefit 是 `commonBenefit + 1`（`:139-142`），确保 AMD 专用 pattern 先于通用 pattern 匹配。
- 注册了 `populateGpuToROCDLConversionPatterns(..., mlir::gpu::amd::HIP, ...)`（`:215-217`），把 GPU dialect 降到 ROCDL。
- 最后 `adjustModeRegister`、`fixUpLoopAnnotation`、`makeAllWarpGroupsIsolatedFromAbove`（`:233-236`）。

这和 NVIDIA 的 `TritonGPUToLLVM` 共享框架，但 pattern 集合不同。你看到的“同一个 `ttg.convert_layout` 在两边生成不同代码”，很可能就是这里选了不同的 AMD/通用 pattern。

### 7. MFMA/WMMA：dot 的 lowering 选择

`third_party/amd/lib/TritonAMDGPUToLLVM/DotOpToLLVM.cpp:36-104` 是 `tt.dot` 的分派逻辑：

```cpp
if (isa<AMDWmmaEncodingAttr>(dEncoding))
  return AMD::convertWMMA(...);

if (!isPermutationMatrixLayout(toLinearLayout(dType.getShape(), dEncoding)))
  return rewriter.notifyMatchFailure(op,
      "Non-WMMA DotOp result encoding must have a permutation-matrix linear layout");

if (isa<AMDMfmaEncodingAttr>(dEncoding))
  return AMD::convertMFMA(...);

if (isa<BlockedEncodingAttr>(dEncoding))
  return AMD::convertAMDFMADot(...);
```

这段代码告诉你三件事：

1. **WMMA 先处理**，因为它是唯一验证过非 permutation（例如 swizzled-warp）encoding 的路径。
2. **MFMA 要求结果是 permutation-matrix linear layout**，否则直接 match failure。这就是 layout 可移植性问题的源头之一。
3. **Blocked encoding 走 `convertAMDFMADot`**，也就是没有矩阵核心 layout 时的 FMA 路径。

`AMDMfmaEncodingAttr` 的字段在 `include/triton/Dialect/TritonGPU/IR/TritonGPUAttrDefs.td:916-930`：`version`（CDNA2/3/4）、`warpsPerCTA`、`instrShape`、`isTransposed`、`tilesPerWarp`、`elementBitWidth`。`AMDWmmaEncodingAttr` 在 `:1089-1102`：`version`（RDNA3/RDNA4/CDNA5）、`ctaLayout`、`instrShape`、`isTransposed`。

选择逻辑在 `third_party/amd/lib/TritonAMDGPUTransforms/AccelerateAMDMatmul.cpp`：`chooseMfmaInstruction`、`getMfmaVersion`、`warpsPerTile`、`isTransposed`、`tilesPerWarp`。例如 `:626-648` 会根据 chain-dot 的消费者决定 `isTransposed` 和 `tilesPerWarp`，目的是让 MFMA 结果能直接作为下一个 dot 的 operand，而不经过 shared memory。

### 8. MIR 调度：MFMA 与内存的交错

`third_party/amd/lib/Target/MFMASchedule/MFMASchedule.cpp:1-27` 的注释就是最好的说明：

> 这是一个 opt-in 的 LLVM-IR pass，在指令选择前，把矩阵核心热循环里的 MFMA 与独立的内存操作交错，让矩阵单元在 load/LDS 发射时保持忙碌；用 `llvm.amdgcn.sched.barrier` 固定顺序，让 LLVM 的 machine scheduler 不破坏它。

实现上，它把 basic block 切成 span（`:69-97`），在每个 span 内 hoist MFMA 输入准备、sink 结果 extract、按吞吐模型把 MFMA 排到内存 anchor 周围，并在 anchor 前插入 `sched.barrier`。每个 block 是事务性的：snapshot → schedule → verify → 失败回滚（`:24-26`、`:592-630`）。

启用方式是 `HIPOptions.schedule_hint = "mfma-schedule"`，且只允许 `gfx942`/`gfx950`（`third_party/amd/backend/compiler.py:858-860`）。这是实验性 hint，不是通用优化开关。

## 进一步用法/重要变体

### NVIDIA 与 AMD 的并排对比

| 维度 | NVIDIA | AMD |
|---|---|---|
| backend 名 | `cuda` | `hip` |
| stage | `ttir/ttgir/llir/ptx/cubin` | `ttir/ttgir/llir/amdgcn/hsaco` |
| warp size | 32 | CDNA 64，RDNA 32（`compiler.py:431-433`） |
| 矩阵指令 | MMAv2/v3/v5、WGMMA、tcgen05 | MFMA（CDNA）、WMMA（RDNA/CDNA5） |
| 矩阵 layout | `MMAEncodingAttr`、`NVMMASharedEncodingAttr` | `AMDMfmaEncodingAttr`、`AMDWmmaEncodingAttr` |
| shared memory | `ttg.shared` + TMEM（Blackwell） | LDS，`amdgpu-flat-work-group-size` |
| 目标 codegen | 主 LLVM + `ptxas` | 独立 `libtriton_amd_codegen.so` + `hsaco` 链接 |
| 运行时加载 | CUDA driver `load_binary` | HIP `load_binary`（`driver.py:155-170`） |
| 默认 dot 精度 | `tf32` | `ieee` |
| 默认 stage 数 | 3 | 2 |
| 调度 hint | 无同名机制 | `mfma-schedule`（gfx942/950） |

### 可移植性陷阱 1：warp size 与线程映射

任何手写 layout、手算 `threadsPerWarp`、手写 `tl.arange` 分块的代码，都必须把 `warp_size` 当参数。AMD CDNA 是 64，不是 32。`convert_to_ttgpuir` 已经把 `options.warp_size` 传进去（`compiler.py:590-591`），但如果你在 Python 侧用 `num_warps * 32` 估算线程数，就会错。

### 可移植性陷阱 2：layout 不是“随便转”

MFMA 要求 permutation-matrix linear layout（`DotOpToLLVM.cpp:45-48`）。WMMA 对非 permutation encoding 更宽容，但也不是所有 layout 都免费。跨后端时，`ttg.convert_layout` 的数量和位置会变；在 NVIDIA 上被 `RemoveLayoutConversions` 消掉的转换，在 AMD 上可能因为 MFMA 的 `isTransposed`/`tilesPerWarp` 约束而保留。

### 可移植性陷阱 3：atomic 的 scope 与跨 CTA

AMD 的 lowering 明确限制了一些组合：

- `third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp:2390`：多 CTA kernel 不支持 scalar atomic CAS。
- 同文件 `:2489`：多 CTA kernel 不支持 scalar atomic RMW。
- `third_party/amd/lib/TritonAMDGPUToLLVM/MemoryOpToLLVM.cpp:586`：不支持 cross-CTA shared atomics。

这些不是“性能差”，而是“直接报错/不支持”。把 NVIDIA 的跨 CTA 同步方案原样搬到 AMD 之前，先查这三条。

### 可移植性陷阱 4：cache modifier 的编码不同

AMD 的 buffer ops 用 `cachePolicy` 和 `aux` 值表达 `.cg/.cs/.cv/.wt`。例如 `test/TritonGPU/amd/amd-buffer-cache-modifiers-rdna3.mlir:1-40` 里：

- `.cg` → `aux = 1`（GLC）
- `.cs` → `aux = 7`（GLC|SLC|DLC）
- `.cv` → `aux = 7`
- store `.cs/.wt` → `aux = 7`

这些是 RDNA3/3.5 的编码，不是 NVIDIA 的 `cache_modifier` 语义。`tt.load(..., cache_modifier=".cg")` 在两边可能都合法，但生成的指令和性能含义不同。

### 可移植性陷阱 5：buffer ops 与 2GB 范围

`HIPBackend.get_tensor_specialization` 和 `is_within_2gb`（`third_party/amd/backend/compiler.py:540-560`）会在指针范围不超过 2GB 时加 `"S"` 特化，配合 buffer ops 使用。`is_within_2gb` 对 torch tensor 走 `untyped_storage().size()`，对普通参数走 `ptr_range()`。如果你的 kernel 在大 tensor 上突然从 buffer ops 退回普通 load/store，性能会变，但语义不变——这类“性能可移植性”问题很容易被误判成 regression。

### 可移植性陷阱 6：fp8 与 denorm

`HIPOptions.supported_fp8_dtypes`（`:385-390`）说明：OCP fp8 在 CDNA4/RDNA4 原生支持，更早的架构软件模拟；UZ fp8 在 CDNA3 原生，其他架构模拟。`allow_flush_denorm` 控制 f32 denorm flush（`:397`），而 `__HIP_FTZ` 在 `make_llir` 里固定为 `True`（`:704-712`），只对 `exp2` 有特殊行为。不要把 NVIDIA 的 FTZ 语义直接套过来。

### 可移植性陷阱 7：`num_ctas` 与 cluster

`parse_options` 在 `num_ctas > 1` 且 `amd.supports_multi_cta_launch` 为假时直接报错（`:494-495`）。NVIDIA 在 `sm_90+` 支持 cluster，AMD 的支持范围不同。跨后端时，`num_ctas` 不是“调大一点试试”的旋钮。

## 对比与取舍

| 场景 | 建议 | 原因 |
|---|---|---|
| 只跑 NVIDIA | 读 NVIDIA pipeline，AMD 只作为对照 | 避免被 AMD 特有 pass 干扰 |
| 要支持 AMD | 先锁 TTIR/TTGIR 语义，再看 AMD pass 差异 | 前端和 TTIR 是共享契约 |
| 写自定义 layout | 同时检查 `threadsPerWarp` 和 MFMA permutation 约束 | 32/64 和 layout 约束会同时咬人 |
| 写 atomic | 先查 AMD 的 multi-CTA/cross-CTA 限制 | 不支持组合会直接失败 |
| 调性能 | 先确认走的是 MFMA/WMMA 还是 FMA | 三条路径的 roofline 完全不同 |
| 调 cache | 用 AMD buffer ops 的 cache policy 测试 | NVIDIA 的 modifier 语义不能照搬 |

## 踩雷集锦

1. **把 `num_warps * 32` 当线程数**。CDNA 是 wave64，`amdgpu-flat-work-group-size` 用 `num_warps * warp_size`（`compiler.py:787`）。错误估计会导致 occupancy 和 shared memory 预算全错。
2. **假设 `ttg.convert_layout` 在两边同价**。MFMA 要求 permutation-matrix linear layout（`DotOpToLLVM.cpp:45-48`）；AMD 还有 `hoist/sink layout conversions` 专属 pass（`compiler.py:604-605`）。同一个 TTGIR 在 AMD 上可能多出或少掉转换。
3. **把 NVIDIA 的跨 CTA atomic 方案直接搬过去**。AMD 明确不支持 multi-CTA scalar atomic CAS/RMW（`LoadStoreOpToLLVM.cpp:2390/2489`）和 cross-CTA shared atomics（`MemoryOpToLLVM.cpp:586`）。
4. **把 `.cg/.cs/.cv` 当成跨后端一致的 cache 语义**。AMD 用 `cachePolicy` + `aux` 编码（`test/TritonGPU/amd/amd-buffer-cache-modifiers-rdna3.mlir:1-40`），和 NVIDIA 的 modifier 不是同一套。
5. **以为 `schedule_hint="mfma-schedule"` 是通用开关**。它只允许 `gfx942`/`gfx950`（`compiler.py:858-860`），而且只对含 MFMA 热循环的 kernel 有效（`MFMASchedule.cpp:1-27`）。

## 进阶

- **读 `AccelerateAMDMatmul.cpp` 的 `chooseMfmaInstruction`**：看它如何按 dtype、shape、arch 选 MFMA/WMMA/FMA，以及 `isTransposed`/`tilesPerWarp` 如何服务 chain-dot。
- **用 `TRITON_DUMP_MIR`/`TRITON_SWAP_MIR` 做 MIR 级实验**：`python/triton/knobs.py:538-543`。这是 guided，需要 ROCm 工具链；但它能让你看到 machine scheduler 前后的差异。
- **读 `test/TritonGPU/amd/` 和 `test/Conversion/amd/`**：这些 lit 测试是 AMD 行为的可执行规格，比文档更准。
- **Gluon 的 `cd_regclass`**：MFMA 调度器会识别 accumulator 的 register-class pin（`MFMASchedule.cpp:20-22`），这是 Gluon 显式控制与 AMD 调度器的交界。

## 动手练习

> 以下练习中，凡涉及运行 AMD 产物的部分都是 `guided`；本机只能做源码阅读和 IR 结构分析。

1. **画分叉图**：从 `add_stages` 出发，画出 NVIDIA 和 AMD 的 stage 图，标出每个 stage 的输入/输出类型（str/bytes）。
2. **对照 pass 序列**：把 `third_party/nvidia/backend/compiler.py:307-420` 和 `third_party/amd/backend/compiler.py:587-651` 并排，列出“同名但实现不同”的 pass（至少 5 个）。
3. **找 layout 约束**：在 `DotOpToLLVM.cpp` 里找到 MFMA 的 permutation 检查，解释为什么 WMMA 可以先处理。
4. **读一个 AMD lit 测试**：选 `test/TritonGPU/amd/accelerate-amd-matmul-mfma.mlir`，指出 `threads-per-warp = 64` 出现在哪里，以及它如何影响 layout。
5. **写可移植性 checklist**：给你自己的一颗 kernel，列出 warp size、layout、atomic、cache、fp8、num_ctas 六项检查。
6. **guided**：如果有 ROCm 机器，用 `TRITON_DUMP_MIR=1` 跑一颗 matmul，比较 `schedule_hint=""` 和 `"mfma-schedule"` 的 MIR；记录 MFMA 与 LDS 的交错变化。

## 本章重点

- AMD 是独立 backend：`HIPOptions`、`HIPBackend`、`ttir/ttgir/llir/amdgcn/hsaco`、`hsaco` 产物。
- warp size 是编译期常量：CDNA 64，RDNA 32；它进入 TTGIR layout、LLVM 控制常量和 launch 属性。
- AMD 有独立 codegen `.so`（`libtriton_amd_codegen.so`），通过 ctypes 加载，revision 进入 backend hash。
- MFMA/WMMA 的选择和 layout 约束在 `DotOpToLLVM.cpp` 与 `AccelerateAMDMatmul.cpp`；MFMA 要求 permutation-matrix linear layout。
- MFMA 调度是 opt-in、按 span 的 LLVM-IR pass，只对 gfx942/gfx950 生效。
- 可移植性四类坑：warp size、layout、atomic scope、cache policy；外加 buffer ops 2GB、fp8、denorm、num_ctas。

## 自我检核

1. 为什么 `ttgir` 在 NVIDIA 和 AMD 上同名但不能逐 pass 对照？
2. `warp_size` 在 AMD 的哪些地方出现？至少说出三处。
3. `libtriton_amd_codegen.so` 和 `libhip` 分别在哪一层被加载？
4. MFMA 为什么要求 permutation-matrix linear layout？WMMA 为什么可以先处理？
5. 哪三类 atomic 组合在 AMD 上明确不支持？
6. `schedule_hint="mfma-schedule"` 的适用架构和 pass 作用域是什么？
7. 如果一颗 kernel 在 NVIDIA 上正确、AMD 上数值错误，你会先查哪四层？

## 延伸阅读

1. **LLVM AMDGPU Backend User Guide** — `https://llvm.org/docs/AMDGPUUsage.html`
   - **读哪里**：AMDGPU target triple、calling convention、wavefront size、`amdgpu-*` 函数属性。
   - **学什么**：把本章的 `amdgpu-flat-work-group-size`、`__oclc_wavefrontsize64`、`hsaco` 对应到 LLVM 的正式语义。
   - **前提**：读过 LLVM IR 和基本 codegen 流程。
2. **ROCm Documentation — HIP Programming Model** — `https://rocm.docs.amd.com/`
   - **读哪里**：HIP module loading、`hipModuleLaunchKernel`、wavefront 概念。
   - **学什么**：`third_party/amd/backend/driver.py` 的 `load_binary`/`launch` 在 runtime 侧做了什么。
   - **前提**：有 CUDA runtime 基础；没有 ROCm 机器也可以读。
3. **AMD CDNA / RDNA ISA Reference** — `https://www.amd.com/en/developer/resources/rocm-hub/hip-sdk.html`
   - **读哪里**：MFMA/WMMA 指令的 operand layout、`v_mfma_*`/`v_wmma_*` 的 shape 和 register 约束。
   - **学什么**：为什么 `instrShape`、`isTransposed`、`tilesPerWarp` 长这样。
   - **前提**：知道矩阵核心的基本概念；不需要会写汇编。
4. **Triton AMD backend tests** — `test/TritonGPU/amd/`、`test/Conversion/amd/`
   - **读哪里**：`accelerate-amd-matmul-*.mlir`、`buffer_load_store.mlir`、`amd-buffer-cache-modifiers-rdna3.mlir`。
   - **学什么**：AMD pass 和 lowering 的可执行规格；比二手文档可靠。
   - **前提**：会读 MLIR lit 测试的 `RUN`/`CHECK`。
5. **Triton 源码** — `third_party/amd/backend/compiler.py`、`third_party/amd/lib/TritonAMDGPUToLLVM/`
   - **读哪里**：`HIPOptions`、`make_ttgir`、`make_llir`、`TritonGPUToLLVM.cpp`、`DotOpToLLVM.cpp`。
   - **学什么**：本章所有源码依据的原始上下文。
   - **前提**：C++17、MLIR dialect conversion 基础。

## 下一章

[Ch 31 调试、正确性与 sanitizer](./31-debugging-correctness-and-sanitizers.md)