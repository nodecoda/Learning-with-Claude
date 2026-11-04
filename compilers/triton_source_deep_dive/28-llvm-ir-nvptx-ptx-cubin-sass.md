# Ch 28 — LLVM IR、NVPTX、PTX、cubin、SASS

> `llir` 不是“另一种 MLIR”，`ptx` 也不是“汇编的别名”；每跨一层，责任就从 Triton 转移到 LLVM，再转移到 `ptxas`。

## 目标

读完本章，你应该能回答：

- `make_llir` 里哪一行把 MLIR LLVM dialect 变成真正的 LLVM module？
- `llvm.to_module` 之后，哪些信息来自 MLIR 属性，哪些来自 `attach_datalayout`？
- `nvvm.kernel`、`ttg.shared`、`global_smem` 分别如何影响 PTX 的 `.visible`、`.shared`、`.reg`？
- `translate_to_asm` 和 `ptxas` 各做什么？为什么 PTX 正确不代表 SASS 正确？
- `ptxas -v` 的 `registers / smem / spill` 报告怎么读？
- 为什么 `ptxas` 版本会进入 cache key，并且可能改变正确性或性能？

本章只追一条链：

```text
TTGIR
  -> MLIR LLVM dialect
  -> llvm.to_module
  -> LLVM IR (in-memory)
  -> NVPTX backend
  -> PTX text
  -> ptxas
  -> cubin (ELF)
  -> cuobjdump / nvdisasm
  -> SASS
```

## 环境/版本

| 项目 | 值 | 证据 |
|---|---|---|
| Triton 源码 | `/home/ubuntu/aicomp/triton` | 本地树 |
| commit | `a972119e92a27b445cd160017ea461e56148590c` | `git log -1` |
| 版本 | 3.9.0 | `python/triton/__init__.py:2` |
| GPU | Tesla T4，sm_75 | `nvidia-smi` |
| 驱动 | 535.216.01 | `nvidia-smi` |
| CUDA / ptxas | 12.4，`V12.4.131` | `ptxas --version` |
| cuobjdump | 12.4，`V12.4.127` | `cuobjdump --version` |
| nvdisasm | 12.4，`V12.4.127` | `nvdisasm --version` |
| Triton 期望的 LLVM | `18f9e623e4c891ba31ff817c07197707ac9bc593` | `cmake/llvm-info.json:2` |
| 本章实验用的 llc | LLVM 15 | `llc --version` |

**实测**：`ptxas`、`cuobjdump`、`nvdisasm` 的版本和下面的 PTX/SASS 输出都来自本机真实执行。

**重要边界**：本章的 LLVM IR -> PTX 实验用系统 `llc` 15，而 Triton 3.9 用自己构建的 LLVM `18f9e623`。两者共享 NVPTX 后端契约，但**不是同一份 LLVM**。因此实验输出只用于认识 `llvm.to_module` 之后的工具链行为，不能当作 Triton 3.9 的逐字 IR。

**guided**：Hopper/Blackwell 的 PTX 特性（wgmma、tcgen05、TMA、cluster）在 T4 的 sm_75 上不可用；本章只讨论通用 NVPTX/PTX 路径，不伪造这些架构的 SASS。

## 为什么需要这个

Triton 的错误经常发生在“层与层之间”：

- TTGIR 看起来对，但 `llvm.to_module` 失败，因为某个 op 的 type 不能转成 LLVM；
- LLVM IR 看起来对，但 PTX 里 kernel 不是 `.entry`，因为 `nvvm.kernel` 没变成 annotation；
- PTX 看起来对，但 `ptxas` 报 register spill 或 shared memory 超限；
- cubin 能加载，但 SASS 里多了一条 `BAR.SYNC`，性能掉一半。

这四层分别由不同代码负责：

| 层 | 主要负责 | 出错时的典型症状 |
|---|---|---|
| MLIR LLVM dialect | Triton 的 `TritonGPUToLLVM` | op 无法 lower、type 不匹配 |
| LLVM IR / NVPTX | LLVM 后端 | PTX 语法、target feature、datalayout |
| PTX -> cubin | `ptxas` | 寄存器/spill、shared 超限、非法指令 |
| cubin -> SASS | `cuobjdump` / `nvdisasm` | 指令调度、bank conflict、barrier 位置 |

只看一层，很容易把锅甩错。

## 先建立直觉

先记住每个 stage 的产物到底是什么：

| stage | 产物 | 生产者 | 消费者 | 怎么读 |
|---|---|---|---|---|
| `ttgir` | TTGIR | `make_ttgir` | `make_llir` | MLIR 语法 |
| `llir` | LLVM IR 文本 | `make_llir` | `make_ptx` | LLVM IR 语法 |
| `ptx` | PTX 文本 | `make_ptx` | `ptxas` | PTX ISA |
| `cubin` | ELF 二进制 | `make_cubin` | CUDA driver | `cuobjdump` |
| `sass` | 反汇编文本 | `cuobjdump` | 人 | `nvdisasm` |

**源码依据**：stage 表在 `third_party/nvidia/backend/compiler.py:660-673`：

```python
stages["llir"] = lambda src, metadata: self.make_llir(src, metadata, options, capability)
stages["ptx"] = lambda src, metadata: self.make_ptx(src, metadata, options, self.target.arch)
stages["cubin"] = lambda src, metadata: self.make_cubin(src, metadata, options, self.target.arch)
```

`compile` 按顺序执行这些 stage，把每一级写进 cache/dump（`python/triton/compiler/compiler.py:326-344`）。SASS 不是 stage；它是 `kernel.asm["sass"]` 的惰性派生（`compiler.py:342-344`、`390-400`）。

一个容易混的点：`make_llir` 的前半段操作的是 **MLIR 模块**，里面的 op 是 `llvm.func`、`llvm.mlir.global`、`nvvm.barrier` 这类 LLVM dialect op；`llvm.to_module` 之后才是 **LLVM module**；`str(llvm_mod)` 输出的是 **LLVM IR 文本**。所以 `.llir` 文件应该用 LLVM IR 的眼光读，不要拿 `triton-opt` 去解析它。

## 核心概念

### 1. MLIR LLVM dialect

在 `make_llir` 的 pass 列表里，`add_to_llvmir` 把 TTGIR 降到 LLVM dialect：

```python
nvidia.passes.ttnvgpuir.add_to_llvmir(pm, capability, ptx_version)
```

源码依据：`third_party/nvidia/backend/compiler.py:456`。

这时你还能看到：

```mlir
llvm.func @kernel(...) attributes {nvvm.kernel = 1 : ui1, nvvm.reqntid = array<i32: 128>}
llvm.mlir.global external @global_smem() {addr_space = 3 : i32, alignment = 16 : i64} : !llvm.array<0 x i8>
nvvm.barrier
```

**源码依据**：`test/Conversion/tritongpu_to_llvm.mlir:10-12`、`1552-1557`；`test/Conversion/lower_tensor_memory_to_llvm.mlir:17`。

### 2. LLVM IR module

`llvm.to_module` 把 MLIR 模块转成 LLVM 模块。之后：

- `llvm.attach_datalayout` 设置 triple、proc、features、datalayout；
- `llvm.optimize_module` 跑 LLVM O3；
- `str(llvm_mod)` 得到 `.llir` 文本。

源码依据：`third_party/nvidia/backend/compiler.py:492-544`。

### 3. NVPTX target

triple 和 proc 是写死的：

```python
triple = 'nvptx64-nvidia-cuda'
proc = sm_arch_from_capability(capability)
features = get_features(options, capability)
```

源码依据：`third_party/nvidia/backend/compiler.py:500-504`、`546-557`。

`sm_arch_from_capability` 对 `capability >= 90` 加 `a` 后缀：

```python
suffix = "a" if capability >= 90 else ""
return f"sm_{capability}{suffix}"
```

源码依据：`third_party/nvidia/backend/compiler.py:113-116`。T4 是 `sm_75`。

`get_features` 把 PTX 版本压到 LLVM 支持的上限：

```python
llvm_ptx_version = min(94, ptx_version)
features = f'+ptx{llvm_ptx_version}'
```

源码依据：`third_party/nvidia/backend/compiler.py:93-104`。注释明确说 LLVM `18f9e623` 最高支持 PTX 9.4。

### 4. PTX

PTX 是虚拟 ISA，不是 SASS。它保留虚拟寄存器（`.reg .f32 %f<4>`）、shared 声明（`.extern .shared`）、kernel 入口（`.visible .entry`），但不决定最终寄存器分配和指令调度。

### 5. cubin

`cubin` 是 ELF。`ptxas` 把 PTX 编译成 cubin，做寄存器分配、指令选择、调度、shared memory 布局。`cuobjdump -sass` 和 `nvdisasm` 只是把 cubin 里的机器码反汇编出来。

### 6. SASS

SASS 是真正的机器指令。你看到的 `LDG.E.SYS`、`FFMA`、`BAR.SYNC`、`STS`、`LDS.U`、`STG.E.SYS` 都是 `ptxas` 调度后的结果。PTX 的顺序不等于 SASS 的顺序。

## 底层机制

### 1. `make_llir` 的 pass 顺序

`make_llir` 先做 TTGIR 级别的收尾，再降到 LLVM：

```python
nvidia.passes.ttgpuir.add_allocate_shared_memory_nv(pm, capability, ptx_version)
nvidia.passes.ttnvgpuir.add_allocate_tensor_memory(pm)
...
nvidia.passes.ttgpuir.add_membar(pm, capability, ptx_version)
...
nvidia.passes.ttnvgpuir.add_to_llvmir(pm, capability, ptx_version)
...
passes.convert.add_nvvm_to_llvm(pm)
pm.run(mod, 'make_llir')
```

源码依据：`third_party/nvidia/backend/compiler.py:422-472`。

关键点：`add_allocate_shared_memory_nv` 和 `add_membar` 在 `add_to_llvmir` **之前**。所以 LLVM lowering 看到的是已经带 `allocation.offset` 和 `ttg.barrier local` 的 TTGIR。

### 2. `llvm.to_module` 是边界

```python
llvm.init_targets()
context = llvm.context()
...
llvm_mod = llvm.to_module(mod, context)
```

源码依据：`third_party/nvidia/backend/compiler.py:492-498`。

这一步做的是 MLIR LLVM dialect -> LLVM module 的翻译。翻译失败通常不是“PTX 生成错”，而是某个 LLVM dialect op 的 type/attribute 不合法，或者 `llvm.func` 的签名和调用约定对不上。

### 3. `attach_datalayout`

```python
triple = 'nvptx64-nvidia-cuda'
flags = get_llvm_flags()
llvm.attach_datalayout(llvm_mod, triple, proc, features, flags, abi="shortptr")
```

源码依据：`third_party/nvidia/backend/compiler.py:502-504`。

`abi="shortptr"` 会影响指针表示和地址空间布局；`proc`/`features` 决定 NVPTX 后端能选哪些指令。T4 的 `proc=sm_75`，所以不会生成 Hopper/Blackwell 指令。

### 4. `optimize_module`

```python
llvm.optimize_module(
    llvm_mod,
    llvm.OPTIMIZE_O3,
    flags=flags,
    disable_slp_vectorizer=not use_slp_vectorizer,
    expand_masked_div_rem=True,
    nvptx_compute_capability=0 if use_slp_vectorizer else capability,
    enable_nvptx_tti=True,
)
```

源码依据：`third_party/nvidia/backend/compiler.py:514-524`。

这里会做向量化、masked div/rem 展开、NVPTX target transform。你看到的 PTX 指令选择，很多是在这一步决定的，不是 `translate_to_asm` 才决定。

### 5. metadata 从哪来

```python
metadata["shared"] = src.get_int_attr("ttg.shared")
metadata["tmem_size"] = src.get_int_attr("ttg.tensor_memory_size")
metadata["global_scratch_size"] = src.get_int_attr("ttg.global_scratch_memory_size") or 0
```

源码依据：`third_party/nvidia/backend/compiler.py:528-536`。

这些 metadata 不进 PTX，而是进 `CompiledKernel.metadata`，launch 时由 driver 使用。例如 `metadata.shared` 决定 dynamic shared memory 大小（`driver.py:273`、`349`）。

### 6. `make_ptx`

```python
ret = llvm.translate_to_asm(src, triple, proc, features, flags, opt.enable_fp_fusion, False, enable_fpsan,
                            sched4reg=opt.sched4reg, enable_fpsan=enable_fpsan, disable_lsr=opt.disable_lsr,
                            abi="shortptr")
names = re.findall(r".visible .entry ([a-zA-Z_][a-zA-Z0-9_]*)", ret)
assert len(names) == 1
```

源码依据：`third_party/nvidia/backend/compiler.py:546-560`。

然后它重写 PTX 头和 target：

```python
ret = re.sub(r'\.version \d+\.\d+', f'.version {ptx_version}', ret, flags=re.MULTILINE)
ret = re.sub(r'\.target sm_\d+', f'.target sm_{capability}', ret, flags=re.MULTILINE)
```

源码依据：`third_party/nvidia/backend/compiler.py:563-565`。

注意：`translate_to_asm` 生成的是 PTX 文本；`make_ptx` 只做版本/target 的文本修正，不做指令选择。

### 7. `make_cubin`

```python
ptxas = get_ptxas(self.target.arch).path
...
ptxas_cmd = [
    ptxas, *debug_info, *fmad, '-v', *disable_opt, *reg_alloc, *ptx_extra_options, f'--gpu-name={arch}',
    fsrc.name, '-o', fbin
]
subprocess.run(ptxas_cmd, check=True, close_fds=False, stderr=flog)
```

源码依据：`third_party/nvidia/backend/compiler.py:576-617`。

几个细节：

- `-v` 永远开，所以 `TRITON_DUMP_PTXAS_LOG=1` 能看到寄存器/shared 报告；
- `--fmad=false` 在 `opt.enable_fp_fusion=False` 时加入（`595`）；
- `--opt-level 0` 在 `TRITON_DISABLE_PTXAS_OPT` 时加入（`599`）；
- `--regAllocOptLevel=2` 是给 ptxas 13.x 的 workaround（`609-610`）；
- `--gpu-name` 用 `sm_arch_from_capability(capability)`（`596`）。

### 8. cubin 如何加载

Python 侧：

```python
self.load_binary = mod.load_binary
```

源码依据：`third_party/nvidia/backend/driver.py:126`。

C 侧：

```c
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuModuleLoadData(&mod, data));
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuModuleGetFunction(&fun, mod, name));
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuFuncGetAttribute(&n_regs, CU_FUNC_ATTRIBUTE_NUM_REGS, fun));
CUDA_CHECK_AND_RETURN_NULL_ALLOW_THREADS(cuFuncGetAttribute(&n_spills, CU_FUNC_ATTRIBUTE_LOCAL_SIZE_BYTES, fun));
```

源码依据：`third_party/nvidia/backend/driver.c:316-324`。

`n_spills` 除以 4 得到 spill 的 32-bit 字数（`driver.c:324`）。shared memory 的 opt-in 在 `driver.c:327` 之后处理。

### 9. SASS 的两种看法

- `cuobjdump -sass demo.cubin`：直接给指令文本；
- `nvdisasm demo.cubin`：给 ELF section、`.nv.info`、寄存器数、`.text.demo` 等更底层信息。

源码依据：`python/triton/knobs.py:505-506` 定义 `cuobjdump`/`nvdisasm` 的路径 knob；`python/triton/tools/disasm.py:67-87` 的 `get_sass` 用 `cuobjdump`。

## 进一步用法/重要变体

### 1. 实测：最小 LLVM IR -> PTX -> cubin -> SASS

下面这段 LLVM IR 是我手写的最小样例，**不是** Triton 的 `make_llir` 输出。它只包含本章要观察的四个元素：`global_smem`、`nvvm.barrier0`、`fma`、kernel annotation。

```llvm
target triple = "nvptx64-nvidia-cuda"

@global_smem = external addrspace(3) global [256 x i8], align 16

define void @demo(ptr %p, ptr %q) #0 {
entry:
  %v = load float, ptr %p, align 4
  %f = call float @llvm.fma.f32(float %v, float 2.0, float 1.0)
  call void @llvm.nvvm.barrier0()
  %sm = getelementptr [256 x i8], ptr addrspace(3) @global_smem, i32 0, i32 0
  store float %f, ptr addrspace(3) %sm, align 4
  call void @llvm.nvvm.barrier0()
  %r = load float, ptr addrspace(3) %sm, align 4
  store float %r, ptr %q, align 4
  ret void
}

declare void @llvm.nvvm.barrier0()
declare float @llvm.fma.f32(float, float, float)

attributes #0 = { "nvvm.kernel"="1" }

!nvvm.annotations = !{!0}
!0 = !{ptr @demo, !"kernel", i32 1}
```

编译命令（**实测**）：

```bash
/usr/lib/llvm-15/bin/llc -march=nvptx64 -mcpu=sm_75 demo.ll -o demo.ptx
/usr/local/cuda/bin/ptxas -arch=sm_75 -v demo.ptx -o demo.cubin
/usr/local/cuda/bin/cuobjdump -sass demo.cubin
```

PTX 输出（**实测**，节选）：

```ptx
.version 6.3
.target sm_75
.address_size 64

.extern .shared .align 16 .b8 global_smem[256];

.visible .entry demo(
    .param .u64 demo_param_0,
    .param .u64 demo_param_1
)
{
    .reg .f32 %f<4>;
    .reg .b64 %rd<5>;

    ld.param.u64 %rd1, [demo_param_0];
    ld.param.u64 %rd2, [demo_param_1];
    cvta.to.global.u64 %rd3, %rd2;
    cvta.to.global.u64 %rd4, %rd1;
    ld.global.f32 %f1, [%rd4];
    fma.rn.f32 %f2, %f1, 0f40000000, 0f3F800000;
    bar.sync 0;
    st.shared.f32 [global_smem], %f2;
    bar.sync 0;
    ld.shared.f32 %f3, [global_smem];
    st.global.f32 [%rd3], %f3;
    ret;
}
```

`ptxas -v`（**实测**）：

```text
ptxas info    : 0 bytes gmem
ptxas info    : Compiling entry function 'demo' for 'sm_75'
ptxas info    : Function properties for demo
    0 bytes stack frame, 0 bytes spill stores, 0 bytes spill loads
ptxas info    : Used 6 registers, 256 bytes smem, 368 bytes cmem[0]
```

SASS（**实测**，节选）：

```text
/*0020*/  LDG.E.SYS R0, [UR4] ;
/*0050*/  BAR.SYNC 0x0 ;
/*0060*/  FFMA R0, R0, R3, 1 ;
/*0070*/  STS [RZ], R0 ;
/*0080*/  BAR.SYNC 0x0 ;
/*0090*/  LDS.U R2, [RZ] ;
/*00a0*/  STG.E.SYS [UR4], R2 ;
```

### 2. 从这段实验能确认什么

- `.extern .shared ... global_smem[256]` 来自 LLVM IR 的 `addrspace(3)` global；`ptxas` 报告 `256 bytes smem`。
- `.reg .f32 %f<4>` / `.reg .b64 %rd<5>` 是**虚拟**寄存器；`ptxas` 实际分配了 6 个物理寄存器。
- `.visible .entry` 不是 `"nvvm.kernel"="1"` 单独产生的。**实测**：只留 attribute 时 `llc` 输出 `.visible .func`；加上 `!nvvm.annotations = !{!0}` 后才变成 `.visible .entry`。这解释了为什么 LLIR 里 `nvvm.kernel` 必须和 annotation 一起看。
- PTX 里 `fma` 在第一个 `bar.sync` 之前，SASS 里 `FFMA` 在 `BAR.SYNC` 之后。`ptxas` 会重排指令；不要用 PTX 顺序推断 SASS 调度。
- `0 bytes spill` 说明没有寄存器溢出到 local memory；如果这里非 0，性能通常明显变差。

### 3. `ptxas -v` 报告怎么读

| 字段 | 含义 | 关注点 |
|---|---|---|
| `bytes gmem` | 静态 global memory | 通常 0；动态分配不算在这里 |
| `stack frame` | 栈帧 | 递归/大局部数组会变大 |
| `spill stores/loads` | 寄存器溢出 | 非 0 通常意味着 occupancy 或性能问题 |
| `registers` | 每线程物理寄存器 | 影响 occupancy |
| `smem` | 静态 shared memory | 不含 dynamic shared memory |
| `cmem[0]` | constant bank 0 | kernel 参数等 |

T4 的 shared memory 约束见 Ch 27：每 SM 64 KiB，每 block 默认 48 KiB。`ptxas -v` 报的是静态 `smem`；如果 kernel 还用 dynamic shared memory，实际 launch 的 shared 是两者之和。

### 4. 重要变体

- `TRITON_DISABLE_PTXAS_OPT=1`：加 `--opt-level 0`，方便看未优化代码，但性能不可用。
- `TRITON_PTXAS_OPTIONS`：追加 ptxas 参数（`knobs.py:512`）。
- `TRITON_DUMP_PTXAS_LOG=1`：打印 `-v` 日志（`knobs.py:514`）。
- `TRITON_PTXAS_PATH`：指定 ptxas（`knobs.py:194-217`）。
- `enable_fp_fusion=False`：加 `--fmad=false`，禁止 FMA 融合，数值更“严格”但更慢。
- `TRITON_DISABLE_LINE_INFO`：控制 `-lineinfo`，影响调试信息，不影响语义。

## 对比与取舍

| 选择 | 优点 | 缺点 | 什么时候用 |
|---|---|---|---|
| 看 LLIR | 能看到 LLVM 优化前的结构 | 不是最终指令 | 查 type/lowering 问题 |
| 看 PTX | 能看到虚拟寄存器、`.shared`、`.entry` | 不是最终调度 | 查 kernel 入口、PTX 版本 |
| 看 SASS | 真实指令、真实寄存器 | 难读、依赖 ptxas 版本 | 查性能、barrier、spill |
| `cuobjdump -sass` | 快、直接 | 信息比 `nvdisasm` 少 | 日常看指令 |
| `nvdisasm` | ELF/`.nv.info`/寄存器数 | 输出更长 | 查 cubin 元信息 |
| `-O3` | 性能最好 | 难对照源码 | 默认 |
| `--opt-level 0` | 易读 | 性能差 | 调试 lowering |

## 踩雷集锦

1. **把 `.llir` 当 MLIR 喂给 `triton-opt`。**
   `make_llir` 在 `llvm.to_module` 之后返回 `str(llvm_mod)`，`.llir` 是 LLVM IR 文本，不是 MLIR LLVM dialect。要改 MLIR 请改 `.ttgir` 或用 `TRITON_KERNEL_OVERRIDE` 在 `llir` 之前介入。

2. **只写 `"nvvm.kernel"="1"` 就以为一定是 `.entry`。**
   **实测**（LLVM 15）：只有 attribute 时输出 `.visible .func`；加 `!nvvm.annotations` 才是 `.visible .entry`。Triton 自己的 LLIR 里同时有 `nvvm.kernel` 和 annotation 语义，读的时候不要只看一个。

3. **忽略 ptxas 版本。**
   `CUDABackend.hash()` 把 `get_ptxas_version()` 拼进 backend hash（`third_party/nvidia/backend/compiler.py:674-677`）。换 ptxas 会让 cache 失效；不同 ptxas 还可能改变寄存器分配、调度，甚至触发已知 bug（`604-610` 的 ptxas 13.x workaround）。

4. **把 `ptxas -v` 的 `smem` 当成 launch 的 dynamic shared memory。**
   `-v` 报的是静态 shared；`metadata.shared` 是 `ttg.shared` 算出的 CTA 用量，launch 时通过 driver 设置。两者不是同一个数。

5. **用 PTX 顺序推断 SASS 调度。**
   上面的实验里 `FFMA` 在 PTX 中位于 `bar.sync` 前，SASS 中却在 `BAR.SYNC` 后。性能分析必须看 SASS 或 profiler，不能只看 PTX。

6. **PTX 版本比驱动新。**
   `ptx_get_version` 按 CUDA 版本映射（`compiler.py:55-75`）：CUDA 12.4 -> PTX 8.4。用更新的 ptxas 生成更高版本 PTX，旧驱动可能加载失败。`TRITON_PTXAS_PATH` 指向新 ptxas 时要一起看驱动。

7. **`--fmad=false` 后数值对了，性能却掉了。**
   它禁止 FMA 融合，`a*b+c` 变成两条指令；这是正确性/性能的取舍，不是 bug。

## 进阶

- 读 `third_party/nvidia/backend/compiler.py:422-544`，把 `make_llir` 的每个 pass 和它解决的问题对应起来。
- 读 `python/triton/backends/compiler.py:48-59` 的 `add_stages` 契约，理解 stage 之间只能通过 `src`/`metadata` 通信。
- 用 `TRITON_KERNEL_DUMP=1 TRITON_ALWAYS_COMPILE=1` 导出一颗 matmul 的 `.llir`/`.ptx`/`.cubin`/`.sass`，对照 `ptxas -v` 的寄存器和 smem。
- 读 PTX ISA 的 `.visible`、`.entry`、`.shared`、`.reg` 语义，再回看 `make_ptx` 的正则重写。
- 在 T4 上比较 `--opt-level 0` 和默认 `-O3` 的 SASS，观察指令数和 spill 变化。

## 动手练习

1. **读 LLIR**：导出任意 kernel 的 `.llir`，找到 `global_smem`、`nvvm.barrier`、`nvvm.kernel`，说明它们分别来自哪个 TTGIR 属性/op。
2. **改 PTX 版本**：用 `TRITON_MOCK_PTX_VERSION` 或 `TRITON_PTXAS_PATH` 改一次 PTX 版本，观察 `.version` 和 cache key 是否变化。
3. **读 ptxas 报告**：对同一个 kernel 分别用 `num_warps=4` 和 `8` 编译，比较 `registers`、`smem`、`spill`。
4. **看 SASS**：用 `cuobjdump -sass` 找 `LDG`、`FFMA`、`BAR.SYNC`，再用 `nvdisasm` 看 `.nv.info` 的 `EIATTR_REGCOUNT`。
5. **制造一次 `.func`**：把最小 LLVM IR 的 `!nvvm.annotations` 删掉，重新 `llc`，确认输出从 `.entry` 变成 `.func`。

## 本章重点

- `make_llir` 的前半段是 MLIR LLVM dialect，`llvm.to_module`（`compiler.py:498`）之后才是 LLVM module；`.llir` 是 LLVM IR 文本。
- `attach_datalayout` 决定 triple/proc/features/datalayout；`optimize_module` 在 O3 下做向量化和 NVPTX 优化。
- `make_ptx` 用 `translate_to_asm` 生成 PTX，再重写 `.version`/`.target`。
- `make_cubin` 调 `ptxas -v`，`--gpu-name` 来自 capability；`ptxas` 版本进入 backend hash。
- `nvvm.kernel`/annotation 决定 `.visible .entry`；`global_smem` 决定 `.extern .shared`；虚拟 `.reg` 由 ptxas 分配。
- SASS 是 `ptxas` 调度后的机器码，不能用 PTX 顺序推断。

## 自我检核

1. `make_llir` 里哪一行把 MLIR 变成 LLVM module？之后 `.llir` 是什么格式？
2. `nvvm.kernel` 和 `!nvvm.annotations` 各自影响 PTX 的什么？
3. `ptxas -v` 的 `registers`、`smem`、`spill` 分别意味着什么？
4. 为什么换 `ptxas` 会让 Triton 的 cache 失效？
5. PTX 里 `FFMA` 在 `BAR.SYNC` 前，SASS 里却在后，说明什么？

## 延伸阅读

1. **`third_party/nvidia/backend/compiler.py:422-544`**
   - 读哪里：`make_llir` 全流程，尤其 `492-524`。
   - 学什么：MLIR LLVM dialect、`llvm.to_module`、datalayout、O3 优化、metadata 提取。
   - 与本章关系：这是 LLIR 的来源。

2. **`third_party/nvidia/backend/compiler.py:546-658`**
   - 读哪里：`make_ptx` 和 `make_cubin`。
   - 学什么：`translate_to_asm` 参数、PTX 头重写、`ptxas` 命令行和错误处理。
   - 与本章关系：这是 PTX/cubin 的来源。

3. **`third_party/nvidia/backend/driver.c:290-330`**
   - 读哪里：`loadBinary`。
   - 学什么：`cuModuleLoadData`、`cuModuleGetFunction`、regs/spills、shared opt-in。
   - 与本章关系：cubin 如何进入 runtime。

4. **PTX ISA 文档的 `.visible` / `.entry` / `.shared` / `.reg` 章节**
   - 读哪里：PTX 指令集文档的 module/function 语法。
   - 学什么：PTX 的虚拟寄存器、地址空间、kernel 入口语义。
   - 与本章关系：读懂 `make_ptx` 的输出。

5. **`test/Conversion/tritongpu_to_llvm.mlir:10-12`、`1552-1557`**
   - 读哪里：`nvvm.kernel`、`nvvm.reqntid`、`nvvm.barrier` 的 FileCheck。
   - 学什么：LLIR 里这些 op 的实际形态。
   - 与本章关系：用测试固定 LLIR 的关键特征。

## 下一章

下一章换一个前端：Gluon。它和 Triton 共用同一套 backend/stage，但把 layout、shared memory、warp specialization 的选择权交给用户。你会看到 `glir` 如何绕过 `convert_to_ttgpuir` 的自动 layout 分配。

→ [Ch 29 Gluon：显式 layout 的前端](./29-gluon-explicit-layouts.md)
