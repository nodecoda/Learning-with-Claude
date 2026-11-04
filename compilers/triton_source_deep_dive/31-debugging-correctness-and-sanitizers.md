# Ch 31 — 调试、正确性与 sanitizer

## 目标

建立一套分层调试方法，能从“Python 里看起来对、GPU 上结果错”一路定位到 frontend、TTIR、TTGIR、LLIR、PTX/SASS 或 runtime。学完本章，你应该能：用 interpreter 缩小语义问题；用 IR dump 找到第一个坏掉的 pass；用 GSan/ConSan/FPSan 区分“数据竞争”“同步错误”“浮点重写错误”；用最小 reproducer 把问题交给别人。

## 环境/版本

Triton 3.9.0，commit `a972119e92a27b445cd160017ea461e56148590c`；本机 NVIDIA T4（`sm_75`），CUDA 12.4，驱动 535.216.01。本章的 Triton 命令都需要一个可 import 的 Triton 构建；当前工作区只有源码，没有安装好的 `triton`，因此命令示例标 `guided`，源码路径和 pass 顺序标 `源码依据`。AMD 相关运行时一律 `guided`。

## 为什么需要这个

Triton 的错误很少“在出错的那一行”。一颗 kernel 的结果错误，可能是：

- Python 侧 `mask` 写错、`BLOCK` 不是 constexpr、`num_warps` 被 autotuner 覆盖；
- TTIR 的 `tt.load` 少了 `other`，或者 `tt.dot` 的精度被默认成 TF32；
- TTGIR 的 `convert_layout` 把一个本应保持的 layout 转坏，或者 pipeline 把 load 挪过了 barrier；
- LLIR 的 `llvm.amdgcn.sched.barrier`/`membar` 被优化掉；
- PTX 的 `.maxnreg` 导致 spill，或 `ptxas` 的 `-O3` 触发 miscompile；
- runtime 的 launcher 把参数顺序、shared memory 大小、stream 传错。

如果你一上来就看 PTX，很可能在最后一层找一个前一层造成的问题。分层调试的价值是：**每一层只回答一个问题，并且留下可复现的证据**。

## 先建立直觉

把 Triton 的编译链想成一条“证据阶梯”：

```text
Python / frontend
  -> TTIR
    -> TTGIR
      -> LLIR (MLIR LLVM dialect)
        -> LLVM IR
          -> PTX (NVIDIA) / AMDGCN (AMD)
            -> cubin / hsaco
              -> runtime launch
```

每一层都有对应的观察手段：

| 层 | 你看到什么 | 主要工具 | 典型症状 |
|---|---|---|---|
| Python/frontend | AST、specialization、cache key | `TRITON_FRONT_END_DEBUGGING=1`、interpreter、`pdb` | 参数没特化、mask 错、`constexpr` 没生效 |
| TTIR | `tt.*` op、类型、shape | `TRITON_KERNEL_DUMP=1`、`triton-opt` | 前端语义错、broadcast/类型提升错 |
| TTGIR | layout、`ttg.convert_layout`、pipeline | `MLIR_ENABLE_DUMP`、`triton-opt` | layout 冲突、barrier 缺失、流水线错位 |
| LLIR | `llvm.*`、地址空间、intrinsic | `LLVM_IR_ENABLE_DUMP=1` | 地址计算错、intrinsic 参数错 |
| PTX/SASS | 指令、寄存器、spill | `ptxas -v`、`nvdisasm`、`cuobjdump` | 寄存器压力、指令选择、非法指令 |
| runtime | launch 参数、stream、错误码 | `compute-sanitizer`、driver hook | 参数顺序、shared memory、越界 |

关键原则：**先证明问题在哪一层，再读那一层的代码**。不要用“我觉得应该是 layout 问题”代替证据。

## 核心概念

### 1. 可复现性三件套

在开始调试前，先把三件事固定：

1. **强制重新编译**：`TRITON_ALWAYS_COMPILE=1`，否则 cache hit 会让你看不到 dump，也会让“改了代码没变化”成为假象。
2. **固定输入和 shape**：一颗 kernel、一个 shape、一个 `num_warps`/`num_stages`；不要用 autotuner 的 20 个 config 一起调。
3. **固定环境**：Triton commit、GPU 架构、驱动、CUDA/ROCm 版本、Python 版本。bug report 里这些是必填项（`.github/ISSUE_TEMPLATE/bug.yml:39-48`）。

### 2. 观察手段的层次

- **interpreter**：不编译，按 Triton op 的 numpy 语义逐程序实例执行；适合查“语义/索引/mask”问题。
- **IR dump**：看编译器实际生成了什么；适合查“前端到 IR”和“pass 改了什么”。
- **sanitizer**：在编译期插入检查；适合查“并发/同步/浮点重写”问题。
- **profiler**：看时间和硬件计数器；适合查“对不对但慢”的问题（Ch 33）。

这四类工具不是替代关系。正确顺序通常是：interpreter → IR dump → sanitizer → profiler。

### 3. 最小 reproducer 的标准

一个好的 reproducer 应该满足：

- 一个文件能跑；
- 只依赖 Triton + PyTorch（或 numpy）；
- 包含 kernel、launch、输入构造、期望结果、实际结果；
- 包含环境信息；
- 能用一个命令复现，最好能在 CPU interpreter 或 `triton-opt` 上复现。

`.github/ISSUE_TEMPLATE/bug.yml:22-26` 明确要求“kernel 和 launching code 都要有，错误要给完整 traceback”。这不是形式主义：没有 launch code，维护者无法判断是编译错还是调用错。

## 底层机制

### 1. Frontend：interpreter 与 AST

interpreter 的入口在 `python/triton/runtime/interpreter.py:1674-1704`：

```python
class InterpretedFunction(KernelInterface[T]):
    def run(self, *args, grid, warmup, **kwargs):
        if warmup:
            return
        fn = self.rewrite()
        debug = kwargs.get("debug", self.kwargs.get("debug")) or triton.knobs.runtime.debug
        interpreter_builder.options = InterpreterOptions(debug=bool(debug))
        return GridExecutor(fn, self.arg_names, grid, self.pre_run_hooks)(*args, **kwargs)
```

`FunctionRewriter` 把 `@triton.jit` 函数改写成 numpy 可执行的 Python，然后 `GridExecutor` 逐 program 实例执行。启用方式是 `TRITON_INTERPRET=1`（`docs/programming-guide/chapter-3/debugging.rst:29-33`）。

interpreter 的已知限制（同文件 `:66-74`）：

- 不支持 `bfloat16` 运算，需要先 `tl.cast` 到 `float32`；
- 不支持间接内存访问（`ptr = tl.load(ptr); x = tl.load(ptr)`）。

这意味着：interpreter 说“对”，不等于 GPU 上“对”；interpreter 说“错”，通常能快速定位到 Python 语义。它是**排除前端语义错误**的工具，不是正确性的最终证据。

### 2. TTIR/TTGIR：IR dump

Triton 的 dump 有两套互补的开关：

- `MLIR_ENABLE_DUMP=1`：在每个 MLIR pass 前 dump IR；`MLIR_ENABLE_DUMP=kernelName` 只 dump 指定 kernel（`README.md:182-185`）。这是“看 pass 前后差异”的主力。
- `TRITON_KERNEL_DUMP=1` + `TRITON_DUMP_DIR=<dir>`：把每个 stage 的 IR 和最终 PTX/AMDGCN 落到文件（`README.md:243-244`）。这是“拿到稳定产物”的主力。

`python/triton/knobs.py:356-371` 定义了这些 knob：`dump_ir`、`always_compile`、`dump_ir_extract_di_local_variables`、`use_ir_loc`、`instrumentation_mode` 等。`test_debug_dump.py:16-47` 是这些行为的最小回归测试：它用 `MLIR_ENABLE_DUMP` 捕获 stderr，断言出现 `IR Dump Before` 和 `tt.func public @_kernel`；再用一个不匹配的 kernel 名断言不出现。这个测试告诉你 dump 的语义是“pass 前”，不是“pass 后”。

一个 guided 的典型命令：

```bash
TRITON_ALWAYS_COMPILE=1 \
MLIR_ENABLE_DUMP=_your_kernel \
MLIR_DUMP_PATH=/tmp/triton-ir \
python your_repro.py
```

预期目录里会出现按 pass 命名的 `.mlir` 文件；具体文件名和数量随 Triton 版本变化，不要背字符串。

### 3. LLIR：LLVM 层

`LLVM_IR_ENABLE_DUMP=1` 在每个 LLVM pass 前 dump LLVM IR（`README.md:186`）。`USE_IR_LOC=ttir|ttgir` 会把 IR 的 location 重新映射到对应 IR 文件的行号，而不是 Python 行号（`README.md:216-218`），这在用性能工具做指令归因时很有用。

如果问题只在 LLVM 优化后出现，一个有效手段是：

```bash
DISABLE_LLVM_OPT=1 python your_repro.py
```

`DISABLE_LLVM_OPT` 在 AMD 的 `make_amdgcn` 里被读取（`third_party/amd/backend/compiler.py:894-900`），NVIDIA 路径也支持同名环境变量。如果关掉优化后结果正确，问题大概率在某个 LLVM pass 或 `ptxas`，而不是 Triton 前端。

### 4. PTX/SASS：最后一层

NVIDIA 的 `make_ptx` 和 `make_cubin` 在 `third_party/nvidia/backend/compiler.py:546-659`。`ptxas` 的 `-v` 会输出寄存器使用和 spill 信息；`PTXAS_OPTIONS` 可以追加参数（`README.md:250`）。`n_regs`/`n_spills` 最终由 `load_binary` 返回（`python/triton/compiler/compiler.py:484-486`）。

guided 的检查命令：

```bash
# 从 dump 目录拿 ptx，或设置 TRITON_KERNEL_DUMP=1
ptxas -v -arch=sm_75 your_kernel.ptx -o /tmp/your_kernel.cubin
cuobjdump -sass /tmp/your_kernel.cubin | head
```

如果 SASS 里出现大量 `LDL/STL`（local memory），说明寄存器压力导致 spill；这时应该回到 TTGIR/LLIR 看 `num_warps`、`maxnreg`、layout 是否让每个线程持有了过多元素。

### 5. Runtime：launcher 与参数

NVIDIA 的 launcher 在 `third_party/nvidia/backend/driver.py:264-353`。几个容易出错的点：

- `CudaLauncher.__init__` 会根据 `max_occupancy` 重新计算 `self.shared`（`:273-286`）；如果你手算 shared memory，可能和 launcher 实际预留的不一致。
- GSan 会往 signature 里追加 `_gsan_globals_ptr`、`_gsan_launch_table_ptr`、`_gsan_launch_index`（`:287-290`），并在 `__call__` 里追加参数（`:332-346`）。手写 launch 或包装 launcher 时，参数个数会变。
- `kernel_metadata` 最终是 `(num_warps, num_ctas, shared)`（`:348-349`）。

`CompiledKernel._init_handles` 还会检查 shared memory、TMEM、线程数上限（`python/triton/compiler/compiler.py:466-490`）。如果你看到 `OutOfResources`，先看这三个检查，而不是怀疑 kernel 逻辑。

### 6. GSan：全局内存数据竞争

GSan 是 Triton/Gluon 的全局内存竞争检测器，设计文档在 `python/triton/experimental/gsan/README.md:1-40`。它的目标是检测未同步的 read-after-write、write-after-write、write-after-read，支持 NVLink 内的 peer-to-peer 访问，覆盖 `ld/st`、`atom`、`cp.async`、TMA。

实现上，GSan 在 TTGIR 层插入 `tti.experimental_gsan_*` 访问标记，并用 shadow memory + vector clock 记录 happens-before。NVIDIA 的接入点在 `third_party/nvidia/backend/compiler.py:430-432`：

```python
if is_enabled(options, "gsan") or is_enabled(options, "gsan-profile"):
    # GSan introduces layout conversions, so it must run before shared-memory allocation.
    passes.ttgpuir.add_global_sanitizer(pm)
```

`CUDAOptions.__post_init__` 会在 GSan 打开时链接 `gsan.ll`（`third_party/nvidia/backend/compiler.py:165-170`），`parse_options` 会把 `max_occupancy` 设为 1（`:236-238`），因为 GSan 用 SM id 标识 CTA。`CudaLauncher` 还会拒绝 CUDA graph capture（`driver.py:336-338`）。

GSan 的边界（`python/triton/experimental/gsan/README.md:13-30`）：

- 不证明正确性，只做随机检测；
- 不检测同一 CTA 内线程之间的竞争（没有 barrier 的 read/write）；
- 不覆盖 inline asm 通信；
- 不检测与非 Triton/Gluon 代码的竞争。

所以 GSan 报 race 是强证据；GSan 不报 race 不是“没问题”。

### 7. ConSan：并发与同步

ConSan 检查的是 Triton 程序内部的并发/同步协议，尤其是 barrier、mbarrier、TMA、warp specialization 的配对。它在 NVIDIA 和 AMD 都有接入：

- NVIDIA：`third_party/nvidia/backend/compiler.py:440-453`，先 `add_prepare_consan_captures(pm, "nvidia")`，再在 shared memory 分配后 `add_concurrency_sanitizer(pm)`。
- AMD：`third_party/amd/backend/compiler.py:692-702`，同样先 `add_prepare_consan_captures(pm, "amd")`，再 `add_concurrency_sanitizer(pm)`。

`HIPBackend.parse_options` 和 `CUDABackend.parse_options` 都会在 ConSan 打开时强制 `debug=True`、`sanitize_overflow=False`（AMD `:483-487`；NVIDIA `:226-229`），避免 device-side assertion 被优化掉。

ConSan 的 lit 测试在 `test/TritonGPU/consan.mlir` 和 `test/TritonGPU/amd/amd-consan.mlir`；它们会检查 `__triton_consan_verify_*`、`ttng.init_barrier`、`barrier_expect` 等是否被正确插入。读这些测试比读 pass 实现更快理解 ConSan 的契约。

### 8. FPSan：浮点重写检查

FPSan 不是 IEEE 模拟器，而是把浮点位模式映射到整数 payload，在整数域执行“保持代数结构”的运算，再映射回浮点（`docs/programming-guide/chapter-3/fpsan.rst:9-23`）。它的用途是：**比较两个在数学上应该等价的 kernel 版本**，看优化后的版本是否仍然保持符号等价。

启用方式（`fpsan.rst:29-51`）：

```python
import triton
triton.knobs.compilation.instrumentation_mode = "fpsan"
# compile and run kernels here
triton.knobs.compilation.instrumentation_mode = ""
```

或：

```bash
TRITON_INSTRUMENTATION_MODE=fpsan python your_script.py
```

关键注意：

- FPSan 是编译器特性，**interpreter 模式不适用**（`fpsan.rst:49`）。
- AMD 上只对 `gfx942`、`gfx950`、`gfx1250` 启用（`fpsan.rst:50-51`）。
- FPSan 的结果只能和 FPSan 的结果比较，不能和普通浮点结果比较（`fpsan.rst:57-60`）。

接入点在 NVIDIA `third_party/nvidia/backend/compiler.py:407-410`，AMD `third_party/amd/backend/compiler.py:646-648`。

### 9. 最小 reproducer：用 MLIR reproducer 锁住失败 pass

`TRITON_REPRODUCER_PATH=<prefix>` 会在每个 MLIR stage 前生成 `.repro.mlir`；如果某个 stage 失败，`<prefix>.<stage>.repro.mlir` 就是失败 pass 前的本地复现（`README.md:187-189`）。

`python/test/unit/language/test_reproducer.py:6-38` 是它的回归测试：

```python
monkeypatch.setenv("TRITON_ALWAYS_COMPILE", "1")
repro_path = tmp_path / "repro_prefix"
monkeypatch.setenv("TRITON_REPRODUCER_PATH", str(repro_path))
triton_[(1, )]()
```

测试断言 `make_ttir`、`make_ttgir`、`make_llir` 都生成了 `.repro.mlir`，并且文件里包含 `mlir_reproducer` 和 `pipeline:`。你可以用 `triton-opt` 直接跑这个 reproducer：

```bash
triton-opt repro_prefix.make_ttgir.repro.mlir \
  --pass-pipeline='builtin.module(...)'
```

具体 pipeline 从文件里的 `pipeline:` 字段抄，不要手写。

### 10. 失败分层 triage 表

| 症状 | 最可能层 | 第一动作 | 证据 |
|---|---|---|---|
| Python 异常、参数没特化 | frontend | `TRITON_FRONT_END_DEBUGGING=1`，打印 `ASTSource`/cache key | traceback、specialization key |
| interpreter 对、GPU 错 | TTGIR 或 LLIR | `TRITON_KERNEL_DUMP=1` 对比 ttir/ttgir/llir | 第一处语义变化的 IR |
| 结果偶尔错、固定 shape 偶发 | 并发/同步 | ConSan；检查 barrier/TMA/mbarrier | ConSan 报错、`consan.mlir` 形态 |
| 多 CTA/多 stream 才错 | GSan 或 runtime | GSan；检查 launch/stream | race 报告、launch 参数 |
| 优化后错、`DISABLE_LLVM_OPT=1` 对 | LLVM/PTX | `LLVM_IR_ENABLE_DUMP=1`，对比 opt 前后 | 第一个坏掉的 LLVM pass |
| 只有某个 `num_stages` 错 | pipeline | 固定 `num_stages`，dump TTGIR | load/barrier 的相对位置 |
| 只有某个 `num_warps` 错 | layout/occupancy | 对比 TTGIR 的 `threadsPerWarp`/`warpsPerCTA` | layout 变化 |
| SASS 有大量 local memory | 寄存器压力 | `ptxas -v`、`n_regs`/`n_spills` | spill 统计 |
| `OutOfResources` | runtime/资源 | 看 shared/TMEM/threads 检查 | `compiler.py:466-490` |
| 只有 AMD 错 | AMD backend | 查 MFMA layout/atomic/cache | Ch 30 的 checklist |

## 进一步用法/重要变体

### interpreter + `pdb`

`docs/programming-guide/chapter-3/debugging.rst:39-60` 给了两种方式：

```bash
TRITON_INTERPRET=1 pdb main.py
```

或在 kernel 里 `pdb.set_trace()`。interpreter 逐 program 实例执行，所以你可以直接看每个 `tl.load` 之后的 tensor 值。

### `TRITON_KERNEL_OVERRIDE`：手工替换某一层

`TRITON_KERNEL_OVERRIDE=1` + `TRITON_OVERRIDE_DIR=<dir>` 可以让你用自己改过的 `.ttir/.ttgir/.llir/.ptx` 替换某一层（`README.md:243-270`）。这是定位“是不是这个 pass 的锅”的利器：把 dump 出来的 TTGIR 改一行，再跑，看行为是否变化。

### `add_stages_inspection_hook`：在 pipeline 里插桩

`python/test/unit/test_stages_inspection.py:11-85` 展示了如何用 `knobs.runtime.add_stages_inspection_hook` 包装 `make_ttgir`：

```python
def inspect_stages_hook(self=None, stages=None, options=None, language=None, capability=None):
    ...
    stages["ttgir"] = lambda src, metadata: make_ttgir_wrapper(...)
```

这个 hook 在 `add_stages` 末尾被调用（NVIDIA `:671-672`，AMD `:958-959`）。它适合做“在真实 pipeline 里插入一个分析 pass”，也适合验证“我的 pass 是否真的被调用”。

### `triton-opt`：脱离 Python 复现 IR 问题

`bin/triton-opt.cpp:1-11` 是 MLIR 的 `MlirOptMain` 包装；`bin/RegisterTritonDialects.h` 注册了 Triton、TritonGPU、TritonNvidiaGPU、Gluon、Proton、AMD 的 dialect 和 pass。lit 测试里的 `RUN: triton-opt ...` 就是它的用法。把 reproducer 的 `.repro.mlir` 丢给 `triton-opt`，可以在不跑 Python/GPU 的情况下复现 pass 崩溃。

## 对比与取舍

| 工具 | 能回答 | 不能回答 | 成本 |
|---|---|---|---|
| interpreter | Python 语义、索引、mask 是否正确 | GPU layout、并发、性能 | 低，但慢且有限制 |
| IR dump | 编译器实际生成/改了什么 | 运行时竞争 | 中，输出量大 |
| GSan | 全局内存是否有未同步访问 | 同 CTA 内竞争、正确性证明 | 高，需要 shadow memory |
| ConSan | barrier/mbarrier/TMA 协议是否配对 | 数值正确性 | 高，改变 IR |
| FPSan | 两个 kernel 版本是否符号等价 | IEEE 精确结果 | 高，只用于对比 |
| `compute-sanitizer` | 越界、非法访问、race | 编译器逻辑错误 | 中，外部工具 |
| profiler | 时间花在哪、是否 memory-bound | 正确性 | 中，Ch 33 |

## 踩雷集锦

1. **cache hit 导致 dump 为空**。`MLIR_ENABLE_DUMP` 只在真正编译时触发；先 `TRITON_ALWAYS_COMPILE=1`，必要时清 `~/.triton/cache`（`README.md:182-184`）。
2. **把 `MLIR_ENABLE_DUMP` 当成“pass 后”**。它是 pass 前 dump；你要看某个 pass 的效果，得看下一个 pass 的 dump，或直接跑 `triton-opt`（`test_debug_dump.py:16-47`）。
3. **用 interpreter 验证 bf16/间接 load**。interpreter 不支持 bf16 运算和 `ptr = tl.load(ptr)` 这类间接访问（`debugging.rst:66-74`）；这类 kernel 必须上 GPU 或改写。
4. **把 FPSan 结果和普通浮点结果比较**。FPSan 是 payload 代数，不是 IEEE 模拟；只能 FPSan vs FPSan（`fpsan.rst:57-60`）。
5. **把 GSan 不报 race 当成“没有 race”**。GSan 是随机检测，且不覆盖同 CTA 内竞争、inline asm、非 Triton 代码（`gsan/README.md:13-30`）。

## 进阶

- **用 `triton-reduce` 做自动缩小**：源码树有 `bin/triton-reduce.cpp`，它可以把一个失败的 MLIR reproducer 缩到更小。具体用法是 guided，取决于你的构建。
- **读 `test/TritonGPU/gsan.mlir`、`consan.mlir`、`fpsan.mlir`**：这些是 sanitizer 的可执行规格，能看到每个 pass 期望插入什么 op。
- **把 `USE_IR_LOC=ttgir` 和 profiler 结合**：让性能工具的指令归因落到 TTGIR 行号，而不是 Python 行号（`README.md:216-218`）。
- **二分 pass**：用 `triton-opt --mlir-print-ir-after-all` 或 `MLIR_ENABLE_DUMP` 逐步缩小到第一个产生错误 IR 的 pass；这比读 1000 行 pass 实现快。

## 动手练习

> 本机没有可 import 的 Triton，以下运行步骤标 `guided`；源码阅读部分可以直接做。

1. **interpreter 练习**：写一颗带 mask 的 `add` kernel，用 `TRITON_INTERPRET=1` 跑一个不被 `BLOCK` 整除的 `n`，打印每个 program 的 `offs` 和 `mask`。
2. **dump 练习**：用 `TRITON_KERNEL_DUMP=1` 拿一颗 matmul 的 `ttir/ttgir/llir/ptx`，指出 `tt.dot` 在哪一层变成 `ttng.warp_group_dot` 或 MMA layout。
3. **reproducer 练习**：设置 `TRITON_REPRODUCER_PATH`，让一颗最小 kernel 生成 `.repro.mlir`，然后用 `triton-opt` 跑文件里的 pipeline。
4. **ConSan 阅读**：在 `test/TritonGPU/consan.mlir` 里找 `__triton_consan_verify_and_update_barrier_state`，解释它检查了什么。
5. **FPSan 设计**：读 `docs/programming-guide/chapter-3/fpsan.rst` 的 payload 模型，说明为什么 FPSan 不能替代数值测试。
6. **triage 表**：给你遇到过的三个 bug，按本章的表填写“症状/层/第一动作/证据”。

## 本章重点

- 分层调试：frontend → TTIR → TTGIR → LLIR → PTX/SASS → runtime；每层只回答一个问题。
- interpreter 查 Python 语义，但有 bf16/间接访问限制；不是 GPU 正确性的最终证据。
- `MLIR_ENABLE_DUMP` 是 pass 前 dump；`TRITON_KERNEL_DUMP` 是 stage 产物；`TRITON_REPRODUCER_PATH` 锁失败 pass。
- GSan 查全局内存竞争；ConSan 查 barrier/mbarrier/TMA 协议；FPSan 查浮点重写的符号等价。
- 最小 reproducer 必须包含 kernel、launch、输入、期望/实际、环境、完整 traceback。
- triage 表把“症状”映射到“层 + 第一动作 + 证据”。

## 自我检核

1. 为什么 `MLIR_ENABLE_DUMP` 看不到某个 pass 的“之后”？
2. interpreter 的两个主要限制是什么？为什么它们会误导正确性判断？
3. GSan、ConSan、FPSan 各自检查什么？哪些问题它们不覆盖？
4. `TRITON_REPRODUCER_PATH` 生成的文件里，`pipeline:` 字段有什么用？
5. 如果 `DISABLE_LLVM_OPT=1` 后结果正确，你会先查哪一层？
6. 一个 `OutOfResources` 错误，应该先看哪三个检查？
7. 写出一个最小 reproducer 的六个必要组成部分。

## 延伸阅读

1. **Triton Debugging Guide** — `docs/programming-guide/chapter-3/debugging.rst`
   - **读哪里**：interpreter 的三种用法、限制、第三方工具。
   - **学什么**：本章 frontend/interpreter 部分的官方版本。
   - **前提**：能跑一颗 Triton kernel；没有构建也可以读。
2. **Triton FpSan Guide** — `docs/programming-guide/chapter-3/fpsan.rst`
   - **读哪里**：payload 模型、启用方式、使用场景。
   - **学什么**：为什么 FPSan 只能做“版本对比”，不能做数值正确性证明。
   - **前提**：了解浮点位模式和基本代数。
3. **GSan Design README** — `python/triton/experimental/gsan/README.md`
   - **读哪里**：目标/非目标、vector clock、shadow memory、限制。
   - **学什么**：GSan 能检测什么、为什么它是随机检测。
   - **前提**：了解 happens-before 和 GPU 内存模型。
4. **MLIR Reproducer 文档** — `https://mlir.llvm.org/docs/PassManagement/#crash-and-reproducer`
   - **读哪里**：`-mlir-pass-pipeline-crash-reproducer`、`pipeline:` 字段。
   - **学什么**：`TRITON_REPRODUCER_PATH` 生成的 `.repro.mlir` 如何独立复现。
   - **前提**：知道 MLIR pass pipeline 的基本语法。
5. **NVIDIA Compute Sanitizer** — `https://docs.nvidia.com/cuda/compute-sanitizer/`
   - **读哪里**：memcheck、racecheck、initcheck、synccheck。
   - **学什么**：GSan/ConSan 之外的通用 GPU 内存/竞争检查。
   - **前提**：有 NVIDIA GPU 和 CUDA 工具链。

## 下一章

[Ch 32 测试与上游贡献](./32-testing-and-upstream-contribution.md)