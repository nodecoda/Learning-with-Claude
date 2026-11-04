# Ch 3 — 第一颗 kernel 的完整旅程

> 一颗 kernel 不是“被编译一次”，而是被拆成五个 stage，每个 stage 都可能命中缓存、被覆盖、被 dump。

## 目标

读完本章，你应该能回答：

- `add_kernel[grid](x, y, out, n, BLOCK_SIZE=1024)` 这一行到底触发了哪些 Python 调用？
- `ttir`、`ttgir`、`llir`、`ptx`、`cubin` 各自负责什么？为什么不是一步到位？
- `compiled.asm` 里为什么会有 `source` 这个 key？它和 `ttir` 有什么区别？
- `metadata` 里有哪些字段？`shared`、`name`、`num_warps` 分别在哪一层写入？
- `kernel.asm["sass"]` 从哪来？为什么懒加载？
- `_init_handles` 在第一次 launch 时做了什么，为什么会抛 `OutOfResources`？

本章的主线是 `python/triton/compiler/compiler.py` 的 `compile` 与 `CompiledKernel`，以及 `third_party/nvidia/backend/compiler.py` 的五个 `make_*`。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5 |
| 驱动 / CUDA | 535.216.01 / 12.4（`V12.4.131`） |
| 系统 Python | 3.10.12 |
| `import triton` | `ModuleNotFoundError` |

**实测**：`python3 -c "import triton"` 失败，仓库内 `.venv` 也没装 Triton。因此本章所有 IR/PTX/cubin/SASS 片段都是 **guided**：按源码路径推导“应该看到什么”，不是本机输出。想得到真实产物，按 Ch 0 装好 Triton，运行 `TRITON_KERNEL_DUMP=1 python add.py`，然后看 `~/.triton/dump/<src_hash>/`。

**源码依据**：`third_party/nvidia/backend/compiler.py:113-116` 把 capability 转成 `sm_{capability}`，T4 即 `sm_75`；`244-247` 拒绝 `num_ctas > 1`（要求 SM90+）；`241-242` 拒绝 `clc=True`（要求 SM100+）。这些 Hopper/Blackwell 专属配置在 T4 上不可用（guided）。

## 为什么需要这个

初学者最容易把“编译”理解成一个黑盒：Python 进去，cubin 出来。一旦出错，就不知道看哪一层：

- 形状/类型错，通常在 `ttir` 之前就报了；
- layout/共享内存错，通常在 `ttgir`；
- 寄存器/指令选择错，通常在 `llir`/`ptx`；
- `ptxas` 报错，才轮到 `cubin`。

不理解 stage 边界，就会在错误的层找 bug。比如把 `num_stages` 调到 8 却发现 T4 上毫无变化——因为 TTGIR 的 pipeline pass 分支根本不覆盖 sm_75（`third_party/nvidia/backend/compiler.py:332-359`）。

另一个原因是缓存：`compile` 在跑 stage 前先查磁盘缓存（`compiler.py:265-279`），命中时直接构造 `CompiledKernel`，一个 pass 都不跑。你以为在调编译器，其实在读旧文件。

## 先建立直觉

从 Python 调用到 cubin，主链路是：

```text
add_kernel[grid](x, y, out, n, BLOCK_SIZE=1024)
  -> KernelInterface.__getitem__            # jit.py:371-378
  -> JITFunction.run(grid=..., warmup=False) # jit.py:744-799
  -> binder(*args, **kwargs)                 # 生成 specialization + options
  -> compute_cache_key(...)                  # jit.py:622-631
  -> kernel_cache 命中？ -> 直接 launch
  -> _do_compile(...) -> ASTSource -> compile(...)   # jit.py:910-944 / compiler.py:226-363
       -> src.make_ir(...) -> TTIR           # ast_to_ttir
       -> for stage in stages: stage(module, metadata)
  -> CompiledKernel -> kernel.run(...)       # _init_handles -> load_binary -> launch
```

关键点：**Python 层有两级缓存**。`JITFunction.device_caches[device]` 里的 `kernel_cache` 是进程内缓存（`jit.py:757-769`）；`compile` 里的 `fn_cache_manager` 是磁盘缓存（`compiler.py:251-279`）。前者命中就完全不进 `compile`。

`CompiledKernel` 本身是“磁盘产物的句柄”：构造时只读 metadata 和 asm 文本，不加载 GPU 模块；`_init_handles` 才真正 `load_binary`（`compiler.py:452-492`）。

## 核心概念

### 五个 stage 的职责

`add_stages` 按顺序注册（`third_party/nvidia/backend/compiler.py:660-672`）：

| stage | 输入 | 输出 | 主要职责 |
|---|---|---|---|
| `ttir` | `ast_to_ttir` 的 module | TTIR | 内联、规范化、CSE、循环展开 |
| `ttgir` | TTIR | TTGIR | 分配 layout/encoding、矩阵加速、流水线 |
| `llir` | TTGIR | LLVM dialect + LLVM IR | 共享内存分配、TritonGPU→LLVM、NVVM |
| `ptx` | LLIR | PTX 文本 | LLVM NVPTX 后端翻译 |
| `cubin` | PTX | cubin 字节 | `ptxas` 汇编 + 链接 |

源码依据：`660-672` 的 `stages["ttir"] = lambda ...: self.make_ttir(...)` 到 `stages["cubin"]`。`BaseBackend.add_stages` 的契约在 `python/triton/backends/compiler.py:48-59`：stage 按插入顺序执行，前一个的输出是后一个的输入，最后一个返回 `bytes`。

**重要**：`src.ext` 对 `ASTSource` 是 `"ttir"`（`compiler.py:57`），所以 `first_stage = 0`，循环从 `ttir` 开始。`make_ir` 的产物在循环之前被单独存成 `.source`（`compiler.py:312-317`），而 `.ttir` 是 **跑完 `make_ttir` pass 之后** 的版本（`compiler.py:326-339`）。

### `asm` 的真实 key 集合

`CompiledKernel.__init__` 把所有非 `.json` 的缓存文件读进 `AsmDict`（`compiler.py:424-429`）：

```python
asm_files = [Path(p) for c, p in metadata_group.items() if not c.endswith(".json")]
self.asm = AsmDict({file.suffix[1:]: file.read_bytes() if file.suffix[1:] == binary_ext
                    else file.read_text() for file in asm_files})
```

所以 `asm` 的 key 来自**实际写入缓存的文件名后缀**，不是硬编码列表。对 ASTSource + NVIDIA 后端，正常情况下是：

```text
['cubin', 'llir', 'ptx', 'source', 'ttgir', 'ttir']
```

**guided**：`source` 是 `ast_to_ttir` 直接产出的原始 TTIR；`ttir` 是 `make_ttir` 之后的 TTIR。两者可能只差常量折叠和 CSE，也可能差很多（比如循环展开）。

如果设了 `TRITON_STORE_BINARY_ONLY=1`，只有 `cubin` 会写盘（`compiler.py:337-339`），`asm` 就只剩 `{'cubin'}`，此时 `asm["ttgir"]` 直接 `KeyError`。

### `metadata` 里有什么

`compile` 初始化 metadata（`compiler.py:281-288`）：

```python
metadata = {"hash": hash, "target": target, **options.__dict__, **env_vars}
metadata["triton_version"] = __version__
```

之后各 stage 往里加字段：

| 字段 | 写入位置 | 含义 |
|---|---|---|
| `num_warps` / `num_ctas` / `num_stages` | `CUDAOptions.__dict__` | 启动配置 |
| `warp_size` / `arch` / `ptx_version` | `CUDAOptions.__dict__` | 目标信息 |
| `tensordesc_meta` | `make_ttgir` 末尾（`391`） | TMA descriptor 元数据 |
| `shared` | `make_llir`（`531`） | 动态共享内存字节数 |
| `tmem_size` | `make_llir`（`532`） | Blackwell TMEM 列数（T4 上为 None；guided） |
| `global_scratch_size/align` | `make_llir`（`533-534`） | 全局 scratch |
| `num_warps`（可被覆盖） | `make_llir`（`528-530`） | warp specialization 可能改 warp 数 |
| `name` | `make_ptx`（`561`） | 从 PTX `.visible .entry` 提取的 kernel 名 |

`CompiledKernel` 把 JSON 读回来变成 `namedtuple`（`compiler.py:409-419`），所以 `compiled.metadata.shared` 是属性访问，不是 dict 下标。`packed_metadata` 只有三个值：`(num_warps, num_ctas, shared)`（`265-270`），传给 launcher。

### `_init_handles`：第一次 launch 才做的事

`CompiledKernel` 构造时不碰 GPU。第一次访问 `.run` 属性或 `__getitem__` 时，`_init_handles`（`452-492`）做：

1. `self._run = driver.active.launcher_cls(self.src, self.metadata)`（`468`）——构造 `CudaLauncher`，解析签名、tensordesc ABI、scratch 大小；
2. 读 `shared`（launcher 可能因 `max_occupancy` 调大，`470`），与 `max_shared_mem(device)` 比较，超了抛 `OutOfResources`（`472-474`）；
3. 若有 `tmem_size`，与 512/576 列上限比较（`475-481`；T4 走不到，guided）；
4. `load_binary(name, cubin, shared, device)` 返回 `module, function, n_regs, n_spills, n_max_threads`（`485-486`）；
5. 检查 `num_warps * warp_size <= n_max_threads`（`488-490`）。

所以 `compiled.metadata.shared` 是编译期确定的，`compiled.n_regs` 是加载后从 cubin 拿到的。两者都不是“运行期统计”。

## 底层机制

### `compile` 逐段读

`compile`（`226-363`）可以拆成六段：

第一段（`226-251`）是后端、选项、缓存 key：`make_backend(target)`、`backend.parse_options(...)`、`get_cache_key(...)`，再对 key 做 sha256 得到缓存目录名。`IRSource` 分支允许你直接从 `.ttir`/`.ttgir`/`.llir`/`.ptx` 文件开始编译（`87-130`），这是后面调试 pass 的入口。

第二段是缓存判断（`265-279`）：如果 `metadata_path` 存在且没开 `always_compile`，直接 `CompiledKernel(src, metadata_group, hash)` 返回。

第三段是 metadata 与 stage 注册（`281-295`）：

```python
metadata = {"hash": hash, "target": target, **options.__dict__, **env_vars}
metadata["triton_version"] = __version__
stages = dict()
backend.add_stages(stages, options, src.language)
first_stage = list(stages.keys()).index(src.ext)
if ir_source:
    first_stage += 1
```

`first_stage` 决定从哪个 stage 开始。对 `ASTSource`，`src.ext == "ttir"`，从 0 开始；对 `.ttgir` 输入，跳过 `ttir` stage，从 1 开始（`294-295`）。

第四段是前端（`299-317`）：

```python
module = src.make_ir(target, options, codegen_fns, module_map, context)
ir_filename = f"{file_name}.source"
metadata_group[ir_filename] = fn_cache_manager.put(module, ir_filename)
```

`ASTSource.make_ir` 调 `ast_to_ttir`（`compiler.py:78-81`）。`put` 对非 bytes 做 `str(data)`（`runtime/cache.py:105-110`），所以 `.source` 就是模块的 MLIR 文本。

第五段是 stage 循环（`326-352`）：

```python
for ext, compile_ir in list(stages.items())[first_stage:]:
    next_module = compile_ir(module, metadata)
    ir_filename = f"{file_name}.{ext}"
    if fn_override_manager is None:
        if (ir_override := metadata.get("ir_override", None)) and ir_override.endswith(f".{ext}"):
            next_module = parse(ir_override, ext, context)
    elif full_name := fn_override_manager.get_file(ir_filename):
        next_module = parse(full_name, ext, context)
    if (not store_only_binary) or (ext in ("cubin", "hsaco", "json")):
        metadata_group[ir_filename] = fn_cache_manager.put(next_module, ir_filename)
    if fn_dump_manager is not None:
        fn_dump_manager.put(next_module, ir_filename)
        if ext == "cubin":
            sass = get_sass(next_module)
            fn_dump_manager.put(sass, file_name + ".sass")
    module = next_module
```

注意三件事：

- **override 在 stage 产出之后生效**（`332-336`），所以你可以只替换 `ttgir`，让后面的 `llir`/`ptx`/`cubin` 继续跑。
- **dump 会额外生成 `.sass`**（`342-344`），而缓存目录里通常没有 `.sass`。
- **`store_only_binary` 只保留 cubin**（`337-339`）。

第六段是写 metadata、通知 listener、返回句柄（`353-363`）。

### `make_ttir`：前端之后的第一轮清理

```python
passes.common.add_inliner(pm)
if capability // 10 < 9:
    passes.ttir.add_rewrite_tensor_descriptor_to_pointer(pm)
passes.common.add_canonicalizer(pm)
passes.ttir.add_combine(pm)
passes.ttir.add_reorder_broadcast(pm)
passes.common.add_cse(pm)
passes.common.add_symbol_dce(pm)
passes.ttir.add_loop_unroll(pm)
pm.run(mod, 'make_ttir')
```

源码依据：`291-304`。

这一层的职责是“把前端生成的啰嗦 IR 变干净”：内联 `tt.func private`、规范化、合并、CSE、删死符号、展开小循环。T4（cap 75）会走 `rewrite_tensor_descriptor_to_pointer`，因为 sm_75 没有 TMA，tensor descriptor 必须降回指针。

**guided**：`.source` 里可能出现 `tt.splat`、冗余的 `arith.extsi`/`arith.cmpi`（溢出检查）；`.ttir` 里这些会被 canonicalize/CSE 掉一部分。不要拿 `.source` 当“前端输出”去对照优化 pass，它只是 `ast_to_ttir` 的原始产物。

### `make_ttgir`：layout 从这里开始

`make_ttgir`（`307-392`）是五个 stage 里最长的一个。开头先设 `ttg.maxnreg`（可选）、算 `emuTF32`，再调 `add_convert_to_ttgpuir(pm, f"cuda:{capability}", opt.num_warps, 32, opt.num_ctas)`，可选 `add_to_clc`，然后 `add_coalesce`。源码依据：`308-319`。

`add_convert_to_ttgpuir` 把 TTIR 的 `tensor<1024xf32>` 变成带 encoding 的 `tensor<1024xf32, #blocked>`，同时给 module 加上 `ttg.num-warps`、`ttg.num-ctas`。从这一刻起，**每个 tensor 都有 layout**，后面的 pass 都在 layout 上做文章（Ch 12-14）。

之后是一长串优化：coalesce、`f32_dot_tc`、CGA layout、remove layout conversions、CTA/thread locality、accelerate matmul、dot operand 优化、descriptor encoding……（`319-330`）。

中间有一个按架构分叉的大块（`332-359`）：

```python
if capability // 10 in [8, 9]:
    ... fuse_nested_loops, licm, hopper_warpspec, assign_latencies,
        schedule_loops, pipeline ...
elif capability // 10 >= 10:
    ... fuse_nested_loops, licm, optimize_accumulator_init, hoist_tmem_alloc,
        promote_lhs_to_tmem, assign_latencies, schedule_loops,
        warp_specialize, pipeline, ...
else:
    passes.ttir.add_triton_licm(pm)
```

**T4 走 `else`**：只有 LICM，没有 `assign_latencies`/`schedule_loops`/`pipeline`。这就是为什么在 sm_75 上调 `num_stages` 基本没用——pipeline pass 根本没注册。这是一个 **源码依据 + T4 事实**，不是猜测。

结尾（`360-392`）再做 canonicalize、loop-aware CSE、`coalesce_async_copy`、TMEM layout 优化（T4 无 TMEM op，实际 no-op；guided）、TMA lowering（cap≥9，T4 不支持）、fence insertion、lower MMA、SCCP/CSE，最后：

```python
pm.run(mod, 'make_ttgir')
metadata["tensordesc_meta"] = mod.get_tensordesc_metadata()
```

### `make_llir`：从 TTGIR 到 LLVM

`make_llir`（`422-544`）先跑一串 TTGIR pass（下面只列主干，完整顺序见 `434-472`）：

```python
passes.convert.add_scf_to_cf(pm)
nvidia.passes.ttgpuir.add_allocate_shared_memory_nv(pm, capability, ptx_version)
nvidia.passes.ttnvgpuir.add_allocate_tensor_memory(pm)
nvidia.passes.ttnvgpuir.add_to_llvmir(pm, capability, ptx_version)
passes.convert.add_nvvm_to_llvm(pm)
pm.run(mod, 'make_llir')
```

源码依据：`434-472`。这里最关键的是 `allocate_shared_memory_nv`：它把 TTGIR 的 `ttg.local_alloc`/`ttg.local_load` 变成具体的 shared memory 偏移，并把总字节数写进 `ttg.shared`。`make_llir` 末尾读这个属性：

```python
metadata["shared"] = src.get_int_attr("ttg.shared")
metadata["tmem_size"] = src.get_int_attr("ttg.tensor_memory_size")
metadata["global_scratch_size"] = src.get_int_attr("ttg.global_scratch_memory_size") or 0
```

源码依据：`531-536`。

之后是 LLVM 侧（`492-524`）：

```python
llvm_mod = llvm.to_module(mod, context)
proc = sm_arch_from_capability(capability)      # "sm_75"
llvm.attach_datalayout(llvm_mod, 'nvptx64-nvidia-cuda', proc, features, flags, abi="shortptr")
llvm.optimize_module(llvm_mod, llvm.OPTIMIZE_O3, ...)
ret = str(llvm_mod)
```

所以 `.llir` 是**文本形式的 LLVM IR**，不是 MLIR。里面会看到 `define`/`declare`、`@llvm.nvvm.read.ptx.sreg.tid.x` 之类的 intrinsic，以及 `addrspace(3)` 的 shared 全局声明。

### `make_ptx`：翻译并改目标

```python
ret = llvm.translate_to_asm(src, triple, proc, features, flags, ...)
names = re.findall(r".visible .entry ([a-zA-Z_][a-zA-Z0-9_]*)", ret)
metadata["name"] = names[0]                      # kernel 名来自 PTX
ret = re.sub(r'\.target sm_\d+', f'.target sm_{capability}', ret, flags=re.MULTILINE)
```

源码依据：`546-574`。

两个细节：

- **kernel 名字来自 PTX**，不是 Python 函数名。`metadata["name"]` 之后用于 `load_binary`。
- **`.version` 和 `.target` 被强制改写**。`.target` 由 capability 决定（T4 是 `sm_75`）；`.version` 由 `get_ptx_version_from_options` 决定，CUDA 12.4 下 `ptx_get_version("12.4")` 返回 `84`，即 `.version 8.4`（`56-65`、`562-565`）。

**guided**：T4 上 `.ptx` 的开头应类似：

```ptx
.version 8.4
.target sm_75
.address_size 64
.visible .entry add_kernel(.param .u64 add_kernel_param_0, ...)
```

### `make_cubin`：ptxas 是最后一道关

```python
ptxas_cmd = [ptxas, *debug_info, *fmad, '-v', *disable_opt, *reg_alloc,
             *ptx_extra_options, f'--gpu-name={arch}', fsrc.name, '-o', fbin]
subprocess.run(ptxas_cmd, check=True, close_fds=False, stderr=flog)
...
with open(fbin, 'rb') as f:
    cubin = f.read()
return cubin
```

源码依据：`612-658`。

`arch = sm_arch_from_capability(capability)`，T4 是 `sm_75`。`--fmad=false` 只在 `enable_fp_fusion` 为假时加（`595`）。`-lineinfo` 默认开启（`584-593`），除非 `TRITON_DISABLE_LINE_INFO=1`。

失败时 `make_cubin` 会把 ptxas stderr 和复现命令一起塞进 `PTXASError`（`626-652`）。这是排查“PTX 能生成但 cubin 失败”的第一手材料。

### 例子 0：一次真实的五层产物（实测）

下面这段是 **实测**：在 `/home/ubuntu/aicomp/triton/.venv` 里安装的 Triton 3.9.0 wheel + torch 2.5.1+cu124 上，跑 `n=1000, BLOCK=128` 的 vector-add，然后直接打印 `k.asm`。源码行号仍以 commit `a972119e92` 为准；wheel 可能对应相邻 commit，IR 细节若有差异，以你本机构建为准。

```python
h = add_kernel[(triton.cdiv(n, 128),)](x, y, out, n, BLOCK=128)
torch.cuda.synchronize()
print((out - (x + y)).abs().max().item())   # 0.0
print(sorted(h.asm.keys()))
```

真实输出：

```text
0.0
['cubin', 'llir', 'ptx', 'source', 'ttgir', 'ttir']
```

TTIR（节选，保留了 `tt.splat`/`tt.make_range`/`arith.cmpi`/`tt.load`/`arith.addf`/`tt.store`）：

```mlir
%offs_0 = arith.muli %pid, %offs : i32
%offs_1 = tt.make_range {end = 128 : i32, start = 0 : i32} : tensor<128xi32>
%offs_2 = tt.splat %offs_0 : i32 -> tensor<128xi32>
%offs_3 = arith.addi %offs_2, %offs_1 : tensor<128xi32>
%mask = tt.splat %n : i32 -> tensor<128xi32>
%mask_4 = arith.cmpi slt, %offs_3, %mask : tensor<128xi32>
%x = tt.splat %x_ptr : !tt.ptr<f32> -> tensor<128x!tt.ptr<f32>>
%x_5 = tt.addptr %x, %offs_3 : tensor<128x!tt.ptr<f32>>, tensor<128xi32>
%x_6 = tt.load %x_5, %mask_4 : tensor<128x!tt.ptr<f32>>
%2 = arith.addf %x_6, %y_8 : tensor<128xf32>
tt.store %1, %2, %mask_4 : tensor<128x!tt.ptr<f32>>
```

TTGIR（节选；注意 encoding 已经出现，`ttg.target` 是 `cuda:75`）：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>
module attributes {"ttg.num-ctas" = 1 : i32, "ttg.num-warps" = 4 : i32,
                   ttg.target = "cuda:75", "ttg.threads-per-warp" = 32 : i32} {
  tt.func public @add_kernel(...) {
    %offs_0 = tt.make_range {end = 128 : i32, start = 0 : i32} : tensor<128xi32, #blocked>
    %x_6 = tt.load %x_5, %mask_3 : tensor<128x!tt.ptr<f32>, #blocked>
    %2 = arith.addf %x_5, %y_7 : tensor<128xf32, #blocked>
    tt.store %1, %2, %mask_3 : tensor<128x!tt.ptr<f32>, #blocked>
  }
}
```

LLIR（节选；已经是 LLVM IR，kernel 带 `ptx_kernel` 调用约定）：

```llvm
target datalayout = "e-p3:32:32-p4:32:32-p5:32:32-p6:32:32-p7:32:32-p101:32:32-i64:64-..."
define ptx_kernel void @add_kernel(ptr addrspace(1) %0, ptr addrspace(1) %1,
                                   ptr addrspace(1) %2, i32 %3, ...) {
  %7 = tail call i32 @llvm.nvvm.read.ptx.sreg.ctaid.x()
  %8 = shl i32 %7, 7
  ...
}
```

PTX（节选；`.version 8.8`、`.target sm_75`、`.reqntid 128`）：

```ptx
.version 8.8
.target sm_75
.address_size 64
.visible .entry add_kernel(
    .param .u64 .ptr .global .align 1 add_kernel_param_0,
    ...
)
.reqntid 128
{
    .reg .pred %p<2>;
    .reg .b32  %r<10>;
    .reg .b64  %rd<8>;
    ld.param.b64 %rd4, [add_kernel_param_0];
    mov.u32 %r4, %ctaid.x;
    shl.b32 %r5, %r4, 7;
    ...
}
```

两个实测结论：

1. `asm` 的 key 确实来自实际写盘的文件后缀，和“例子 1”的 `guided` 预期一致；
2. 本机系统 `ptxas` 是 CUDA 12.4 的 PTX 8.4，**不能**汇编这段 PTX 8.8；wheel 用的是它自己下载的 CUDA 工具链。所以“PTX 能生成、cubin 失败”时，先看 `ptxas --version`，不要先怀疑 lowering（**实测**：`ptxas` 报 `Unsupported .version 8.8; current version is '8.4'`）。
3. 这个 wheel 的 `asm["sass"]` 不可用（`AsmDict.__missing__` 找不到 `nvdisasm` 时不会返回 SASS）；要 SASS 就用 `cuobjdump -sass` 对 `cubin` 反汇编。

### 例子 1：拿到所有层

```python
import torch, triton, triton.language as tl

@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask)
    y = tl.load(y_ptr + offs, mask=mask)
    tl.store(out_ptr + offs, x + y, mask=mask)

x, y = torch.randn(1024, device="cuda"), torch.randn(1024, device="cuda")
out = torch.empty_like(x)
k = add_kernel[(1,)](x, y, out, 1024, BLOCK=1024)
print(sorted(k.asm.keys()))
print(k.metadata)
```

**guided**：`sorted(k.asm.keys())` 应输出：

```text
['cubin', 'llir', 'ptx', 'source', 'ttgir', 'ttir']
```

**guided**：`k.metadata` 是 `KernelMetadata(...)` namedtuple，至少包含 `hash`、`target`、`num_warps`、`num_ctas`、`num_stages`、`warp_size`、`arch`、`ptx_version`、`triton_version`、`shared`、`name`、`tensordesc_meta`、`global_scratch_size/align`、`profile_scratch_size/align`。T4 上 `tmem_size` 为 `None`（guided）。

### 例子 2：逐层看关键片段

**guided**：`.ttgir` 会看到 encoding 和 `ttg.num-warps`（下面的 layout 常量是示意，具体值由 coalesce 决定）：

```mlir
#blocked = #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>
module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32} {
  tt.func public @add_kernel(%arg0: !tt.ptr<f32>, ...) {
    %0 = tt.make_range {end = 1024 : i32, start = 0 : i32} : tensor<1024xi32, #blocked>
    ...
```

**guided**：`.llir` 是文本 LLVM IR，会看到 `define void @add_kernel(...)` 和 `call i32 @llvm.nvvm.read.ptx.sreg.tid.x()` 这类 NVVM intrinsic。

**guided**：`.ptx` 会看到 `.visible .entry`、`.reg`、`ld.global`/`add.f32`/`st.global`；`.cubin` 是 ELF 字节，`file` 命令会识别为 `ELF 64-bit LSB shared object, *CUDA*`。

**guided**：`k.asm["sass"]` 是 `cuobjdump -sass` 的文本，里面按 `/*地址*/ 指令` 排列，能看到 `LDG.E`、`FADD`、`STG.E` 一类指令。不要把它当成 PTX 的逐行翻译：ptxas 会做寄存器分配、指令调度和 peephole。

### 例子 3：SASS 从哪来

`AsmDict.__missing__`（`compiler.py:390-400`）：

```python
def __missing__(self, key):
    if key == "sass":
        value = get_sass(self["cubin"])
    else:
        raise KeyError("Unknown key: '%s'" % key)
    self[key] = value
    return value
```

`get_sass`（`python/triton/tools/disasm.py:67-75`）把 cubin 写到临时文件，再调 `extract`：

```python
cuobjdump = path_to_cuobjdump()
sass_str = subprocess.check_output([cuobjdump, "-sass", file_path])
```

源码依据：`disasm.py:83-88`。`path_to_cuobjdump()` 读 `knobs.nvidia.cuobjdump.path`（`78-80`），默认是 Triton 包内的 `backends/nvidia/bin/cuobjdump`（`knobs.py:194-200`）。

所以：

- `asm["sass"]` **不在缓存里**，是首次访问时现算的；
- 它需要 `cuobjdump` 可执行；
- 它算完会缓存在 `AsmDict` 实例上（`399`），同一进程第二次访问不再跑命令。

## 进一步用法/重要变体

- **从 IR 开始编译**：`triton.compile("kernel.ttgir")` 会构造 `IRSource`（`compiler.py:87-130`），跳过前面的 stage。这是调 pass 的最快路径。
- **只替换某一层**：设 `TRITON_KERNEL_OVERRIDE=1` + `TRITON_OVERRIDE_DIR`，把 `kernel.ttgir` 放进对应目录；`compile` 会在该 stage 用你的文件替换（`334-336`）。autotune config 里的 `ir_override` 也走同一逻辑（`332-333`）。
- **只看不覆盖**：`TRITON_KERNEL_DUMP=1` + `TRITON_DUMP_DIR`，dump 目录按 `src.hash()` 分（`257-258`），并额外写 `.sass`（`342-344`）。

## 对比与取舍

### 每一层适合查什么

| 现象 | 先看哪层 | 原因 |
|---|---|---|
| 形状/类型/广播错 | `.source` / `.ttir` | 语义层在前端就报错 |
| layout 转换过多 | `.ttgir` | encoding 与 `convert_layout` 都在这层 |
| 共享内存超限 | `.ttgir` / `metadata.shared` | `allocate_shared_memory_nv` 在这层定大小 |
| 寄存器压力/指令选择 | `.llir` / `.ptx` | LLVM 优化与 NVPTX 后端 |
| ptxas 报错 / 实际指令 | `.ptx` / `.sass` | 前者可喂给 ptxas 复现，后者是最终机器码 |

## 踩雷集锦

### 1. 以为 `asm` 只有五个 key

```python
print(k.asm["ttir"])   # 有
print(k.asm["source"]) # 也有，但很多人不知道
```

`asm` 的 key 来自缓存文件名后缀（`compiler.py:424-429`），`.source` 也是文件。

**纠正**：先 `print(sorted(k.asm.keys()))`，不要假设。

### 2. 开了 `TRITON_ALWAYS_COMPILE=1` 却没重编

同一进程里第二次调用同一个 kernel，`JITFunction.run` 先查 `kernel_cache`（`jit.py:768-769`），命中就直接 launch，根本不进 `compile`。

**纠正**：在**新进程**里设环境变量；或者显式清 `JITFunction.device_caches`（仅调试用）。

### 3. 把 `compiled[grid]` 当成 `kernel[grid]`

`CompiledKernel.__getitem__`（`511-522`）直接取 `grid[0]`、`grid[1]`、`grid[2]`，要求三元组；而 `JITFunction.__getitem__` 接受 1/2/3 元组并在 `run` 里补齐（`jit.py:787-794`）。

**纠正**：`compiled[(g0, g1, g2)]`，或者直接用 `compiled.run(...)`。

## 进阶

- 读 `CompileTimer`（`compiler.py:187-223`）。它把编译拆成 `ir_initialization`、每个 stage、`store_results`，配合 `knobs.compilation.listener` 可以量化每个 pass 的耗时。
- 读 `IRSource.__init__`（`87-130`）。它演示了如何从 `.ptx` 反推签名（正则解析 `.param`），以及 `.ttgir` 如何从 `ttg.num-warps` 恢复 options。
- 读 `CudaLauncher.__init__`（`third_party/nvidia/backend/driver.py:264-304`）：它把 `signature` 展开成 launcher 参数注解，并处理 `max_occupancy` 对 shared memory 的预留。

## 动手练习

1. **数 key**：装好 Triton 后跑例子 1，打印 `sorted(k.asm.keys())`，再设 `TRITON_STORE_BINARY_ONLY=1` 跑一次，比较差异并解释原因。
2. **对照三层**：在 `.ttir`、`.ttgir`、`.llir` 里各找一次 `add`（`arith.addf` / `ttg.convert_layout` 后的 `arith.addf` / `llvm.fadd`），说明每层多了什么信息。
3. **制造 cache hit**：连续两次运行同一个 kernel，第二次打印 `k.hash` 并确认没有重新编译；再用 `TRITON_ALWAYS_COMPILE=1` 在新进程里跑，观察 dump 目录变化。
## 本章重点

- 一颗 kernel 经过五个 stage：`ttir -> ttgir -> llir -> ptx -> cubin`，每个 stage 都可缓存、覆盖、dump。
- `.source` 是 `ast_to_ttir` 的原始 TTIR，`.ttir` 是 `make_ttir` 之后的版本，两者不是一回事。
- `asm` 的 key 来自实际缓存文件后缀，正常是 `cubin/llir/ptx/source/ttgir/ttir`，`sass` 是懒加载。
- `metadata` 由 options、env vars、各 stage 共同填充；`shared` 在 `make_llir` 写入，`name` 在 `make_ptx` 写入。
- `CompiledKernel` 构造时不碰 GPU；`_init_handles` 才构造 launcher、检查 shared memory、`load_binary`。
- T4 上 `num_stages` 不触发 TTGIR pipeline pass：`make_ttgir` 的架构分支不覆盖 sm_75。
- `TRITON_ALWAYS_COMPILE=1` 只绕过磁盘缓存，不绕过进程内 `kernel_cache`。

## 自我检核

- 你能说出 `.source` 与 `.ttir` 的产出位置差异吗？
- 你能解释为什么 `asm` 里会有 `source`，而 `sass` 不在缓存里吗？
- 你能指出 `metadata["shared"]`/`metadata["name"]` 由哪个 stage 写入，以及 `_init_handles` 为什么不在 `__init__` 里调用吗？

## 延伸阅读

1. **`python/triton/compiler/compiler.py:226-363`**
   - 读哪里：`compile` 全文。
   - 学什么：缓存判断、stage 循环、override/dump 的插入点；这是整条流水线的调度器。

2. **`python/triton/compiler/compiler.py:390-522`**
   - 读哪里：`AsmDict`、`CompiledKernel.__init__`、`_init_handles`。
   - 学什么：句柄如何从磁盘产物构造，SASS 为什么懒加载，launch 前检查什么；对应本章 `CompiledKernel`/`asm`/`_init_handles` 三段。

3. **`third_party/nvidia/backend/compiler.py:291-392`**
   - 读哪里：`make_ttir`、`make_ttgir`。
   - 学什么：TTIR 清理与 TTGIR layout/pipeline 的 pass 顺序，以及架构分支；对应本章五个 stage 的前两个。

## 下一章

下一章回到 Python 层，拆开 `@triton.jit` 与 `JITFunction`：binder 如何打包参数、特化如何决定 cache key、为什么同一个 Python 函数会编译出多份 kernel。

→ [Ch 4 @triton.jit、JITFunction 与特化](./04-jitfunction-specialization-and-cache.md)
