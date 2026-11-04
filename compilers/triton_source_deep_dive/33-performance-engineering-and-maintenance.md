# Ch 33 — 性能工程与长期维护

> 性能不是一次 benchmark 的数字，而是一条可复现、可解释、可回归的因果链。

## 目标

读完本章，你应该能：

- 用 `do_bench` 测出可信的 kernel 时间，并说清楚 warmup、L2 flush、median/quantile 各自解决什么问题；
- 用 `ncu` 的指标（或它的失败信息）判断一个 kernel 是 memory-bound 还是 compute-bound；
- 用 occupancy、寄存器、shared memory、spill 解释“为什么这个配置慢”；
- 在 T4 上正确解读 `num_warps`/`num_stages`：知道 `sm_75` 的 `tt.dot` 回退 FMA，且 `num_stages` 不进入 pipeline；
- 用 roofline 和算术强度判断优化方向，而不是随机调参；
- 理解维护一个编译器后端的长期负担：LLVM pin、API 变更、deprecation、backend 扩展、性能回归、issue triage、release notes；
- 判断“本地快”为什么不等于“上游可接受”。

本章的性能数字都来自本机 Tesla T4。Hopper/Blackwell 的 TMA、WGMMA、warp specialization、多 CTA 结论标 `guided`；AMD 相关标 `guided`。不要把 T4 的调参结论直接搬到 A100/H100。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92`，Triton 3.9.0 |
| GPU | Tesla T4，`sm_75`，40 个 SM，4 MiB L2，64K 寄存器/SM，1024 线程/SM |
| 驱动 / CUDA | 535.216.01 / 12.4 |
| 时钟 | 空闲 SM 660 MHz；最大 SM 1590 MHz；显存 5001 MHz |
| 实验环境 | `.venv`：Python 3.12.16，`torch 2.5.1+cu124`，`triton 3.9.0` wheel |
| `ncu` | `/usr/local/cuda/bin/ncu` 存在，但无权限读取 GPU 性能计数器 |
| 系统 `ptxas` | CUDA 12.4，支持 PTX 8.4；Triton wheel 自带更新的 ptxas |

**源码依据**：

- `python/triton/testing.py:288-358` 是 `do_bench` 的实现；
- `third_party/nvidia/backend/compiler.py:119-180` 是 `CUDAOptions`；
- `third_party/nvidia/backend/compiler.py:326` 调用 `add_accelerate_matmul`；
- `third_party/nvidia/backend/compiler.py:332-359` 决定是否进入 pipeline；
- `lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp:514-520` 明确写出 `sm < 80` 回退 FMA；
- `python/triton/runtime/autotuner.py:148-188` 是 autotune 的 benchmark 调用；
- `python/triton/knobs.py:383-384` 是 `TRITON_CACHE_AUTOTUNING` / `TRITON_PRINT_AUTOTUNING`。

**guided**：`ncu` 在本机返回 `ERR_NVGPUCTRPERM`，所以本章没有 ncu 指标输出。T4 没有 TMA/WGMMA/tcgen05，Hopper/Blackwell 的 pipeline 收益只能读源码或远端验证。

## 为什么需要这个

性能问题不会像编译错误那样给你一条 traceback。它表现为：

- 同一个 kernel 在 A100 上 0.2 ms，在 T4 上 1.7 ms；
- 同一份代码，`num_warps=4` 比 `num_warps=8` 慢 3 倍；
- 第一次跑 1.29 ms，第二次跑 0.62 ms，你以为“编译器变快了”；
- `num_stages` 从 2 调到 4，有时快、有时没变化，因为时钟在爬升；
- 本地 benchmark 赢了 5%，但上游 reviewer 说“复杂度不值得”。

如果没有方法，你会在“调参—重跑—再调参”的循环里浪费几天。性能工程的核心不是“多试几个配置”，而是：**先判断瓶颈类型，再选一个能证伪的假设，最后用可复现的实验验证**。

维护视角同样重要。Triton 不是一次性的编译器：它跟着 LLVM、PyTorch、CUDA/ROCm 和硬件一起演进。一个 backend 的长期成本包括：

- LLVM pin 升级带来的 API/IR 变化；
- `CUDAOptions`/`HIPOptions` 字段的兼容性；
- 旧 API 的 deprecation 周期；
- 新架构的 pipeline 分支；
- 性能回归的监控和 bisect；
- issue triage 和 release cherry-pick。

## 先建立直觉

先看 roofline 的三个量：

```text
算术强度 AI = FLOPs / Bytes
机器平衡点 ridge = peak_FLOPs / peak_Bytes_per_second
AI < ridge  -> memory-bound
AI > ridge  -> compute-bound
```

T4 的粗略规格（按 NVIDIA 规格）：FP32 FMA 峰值约 8.1 TFLOPS，显存带宽约 320 GB/s，ridge ≈ 25.3 FLOP/byte。

一颗 vector add：每个元素读 x(4B) + 读 y(4B) + 写 out(4B) = 12 B，做 1 次加法 = 1 FLOP，所以 `AI = 1/12 ≈ 0.083`。`0.083 << 25.3`，它几乎一定是 memory-bound；优化方向是 coalescing、向量化、减少读写，而不是换数学指令。

一颗 1024³ 的 fp16 matmul：`FLOPs = 2*1024^3 ≈ 2.15e9`，`Bytes ≈ 2*3*1024^2 ≈ 6.29e6`，`AI ≈ 341`。`341 >> 25.3`，理论上是 compute-bound；Triton 3.9 在 sm_75 上没有启用 Tensor Core 路径时，瓶颈变成 FP32 FMA 吞吐和寄存器压力。

再看时间轴。一个 kernel 的测量值包含：

```text
launch overhead + memory transfer + compute + synchronization + clock ramp
```

`do_bench` 用 CUDA event 包住 `fn()`，用 L2 flush 避免缓存命中，用 warmup 让时钟和 cache 进入稳定状态。它不能消除 launch overhead，也不能保证你的 GPU 没有降频。所以“一个数字”永远不够，至少要有 median、quantile 和重复实验。

## 核心概念

### 1. `do_bench`：测的是什么

`do_bench(fn, warmup=25, rep=100, ...)` 的默认单位是毫秒（`testing.py:288-311`）。流程是：先跑一次并 `synchronize()`（`317-318`）；用 5 次带 L2 flush 的调用估算单次时间（`322-331`）；用 `warmup/estimate_ms` 和 `rep/estimate_ms` 算 warmup/repeat 次数（`334-335`）；warmup 后，每次先 `clear_cache(cache)`，再 record start、`fn()`、record end（`349-354`）；最后 `synchronize()` 并汇总 event 时间（`355-358`）。

关键点：

- **L2 flush**：`runtime.driver.active.clear_cache(cache)`（`350`）会在每次计时前清 L2。T4 只有 4 MiB L2，而我们的 add 工作集是 64 MiB，flush 与否会影响结果。
- **median 比 mean 稳**：默认 `return_mode="mean"`，但 GPU 时钟、调度抖动会产生长尾；性能对比建议 `return_mode="median"` 或 `quantiles=[0.2,0.5,0.8]`。
- **warmup 是时间，不是次数**：`warmup=25` 是 25 ms，不是 25 次。
- **它测的是 GPU 时间，不是端到端时间**：Python 调用、参数打包、launch 前的 CPU 开销不在 event 之间。

### 2. `ncu`：看的是什么

`ncu` 能给你 `do_bench` 给不了的信息：DRAM 读写字节数、L2 hit rate、SM/DRAM throughput、occupancy、active warps、stall reasons、shared memory bank conflict、指令 mix（FFMA/HMMA/LDG/LDS）。

典型命令：

```bash
ncu --set full --kernel-name add_kernel --launch-count 1 \
  python your_script.py
```

或者只看几个指标：

```bash
ncu --metrics gpu__time_duration.sum,dram__bytes_read.sum,dram__bytes_write.sum,\
sm__throughput.avg.pct_of_peak_sustained_elapsed,\
gpu__compute_memory_throughput.avg.pct_of_peak_sustained_elapsed \
  --kernel-name add_kernel --launch-count 1 python your_script.py
```

**实测**：本机运行 `ncu --set basic` 失败：

```text
==ERROR== ERR_NVGPUCTRPERM - The user does not have permission to access NVIDIA GPU
Performance Counters on the target device 0.
==WARNING== No kernels were profiled.
```

这不是命令写错，而是容器/驱动没有给 performance counter 权限。没有 ncu 时，退回到：

- `do_bench` + 字节数/FLOPs 手算 roofline；
- `ptxas -v` 看寄存器/spill；
- `nvdisasm` 看 SASS 指令 mix；
- `torch.cuda.get_device_properties` 看 SM/L2/寄存器上限。

### 3. occupancy：为什么“多开 warp”不一定快

occupancy 受三类资源限制：

```text
registers per thread * threads per CTA
shared memory per CTA
max threads / max CTAs per SM
```

T4 的硬限制（实测 `torch.cuda.get_device_properties`）：

```text
40 SMs
65536 registers / SM
1024 threads / SM
4 MiB L2
```

我们的 matmul（BM=64, BN=64, BK=32, fp16）：`num_warps=4` 时 `n_regs=255`、`n_spills=88`，`255*128=32640` regs/CTA，`65536/32640=2` CTAs/SM，256 threads/SM，occupancy 25%。换成 `num_warps=8`：`n_regs=183`、无 spill，`183*256=46848` regs/CTA，1 CTA/SM，仍是 256 threads/SM、25% occupancy。

两者 occupancy 都是 25%，但 `num_warps=8` 没有 spill，实测快 2.8 倍。这说明：**occupancy 不是唯一指标，spill 和指令级并行同样关键**。

### 4. roofline：先判断瓶颈，再调参

用本章的两个 kernel 做 roofline：

| kernel | 时间（实测） | 吞吐 | AI | 判断 |
|---|---|---|---|---|
| vector add，BLOCK=256，warps=8 | 0.769 ms | 261 GB/s | 0.083 FLOP/B | memory-bound |
| matmul 1024³ fp16，W=8 | 0.616 ms | 3.49 TFLOPS | 341 FLOP/B | compute-bound |

add 的 261 GB/s 是 T4 规格带宽 320 GB/s 的约 82%，说明它已经比较接近 memory-bound 的上限。继续调 `num_warps` 的收益有限；要再快，只能减少字节数（融合、复用、压缩）或改善访问模式。

matmul 的 3.49 TFLOPS 是 FP32 FMA 峰值 8.1 TFLOPS 的约 43%。它不是 memory-bound，但也没吃满 FP32 吞吐；而且 Tensor Core 的 65 TFLOPS 完全没用上，因为 T4 的 `tt.dot` 走了 FMA 路径。

### 5. Triton kernel 的常见瓶颈

| 症状 | 可能原因 | 先看什么 |
|---|---|---|
| 带宽低、时间随数据量线性增长 | 非 coalesced 访问、stride 太大 | `ttgir` 的 layout、`tt.load` 的指针表达式 |
| 多出很多 `ttg.convert_layout` | layout 冲突、dot operand 不匹配 | `TRITON_KERNEL_DUMP=1` 的 TTGIR |
| shared memory 用量大、occupancy 低 | pipeline 缓冲、layout conversion 走 shared | `metadata.shared`、`ttg.local_alloc` |
| `n_spills > 0`、时间突然变慢 | 寄存器压力、block 太大、warps 太少 | `ptxas -v`、`k.n_regs`/`k.n_spills` |
| 计算吞吐低 | FMA 回退、指令 mix 差、依赖链长 | PTX/SASS 的 `mma`/`FFMA` 计数 |
| 小 kernel 时间不随规模下降 | launch overhead、Python 开销 | `do_bench_cudagraph`、`do_bench_proton` |
| 第一次跑很慢，后面变快 | 时钟爬升、cache 预热 | 固定 warmup、重复实验 |

### 6. options：`num_warps`、`num_stages`、`maxnreg`

`CUDAOptions`（`third_party/nvidia/backend/compiler.py:119-180`）里和性能直接相关的字段：

| 字段 | 默认 | 作用 | 代价 |
|---|---|---|---|
| `num_warps` | 4 | 每个 CTA 的 warp 数 | 太大 → 寄存器/CTA 压力、occupancy 下降 |
| `num_stages` | 3 | pipeline 阶段数 | 只在支持 pipeline 的架构上有效；增加 shared memory |
| `num_ctas` | 1 | CTA cluster 大小 | SM90+ 才支持；T4 会直接报错（`244-247`） |
| `maxnreg` | None | 每线程最大寄存器数 | 限制太狠 → spill |
| `max_occupancy` | None | 通过预留 shared memory 限制驻留 CTA | 可能降低 occupancy |
| `ptx_version` | None | 指定 PTX 版本 | 与 ptxas/驱动不匹配会失败 |
| `enable_fp_fusion` | False* | 是否允许 FMA 融合 | 影响数值和性能；*3.9 的兼容默认会打开，见下文 warning |

`num_warps` 和 `num_ctas` 必须是 2 的幂（`173-174`）。`num_ctas > 1` 在 `capability < 90` 时被拒绝（`244-247`），所以 T4 上不要写 `num_ctas=2`。

### 7. pipeline：入口在哪，T4 为什么用不上

NVIDIA 后端的 TTGIR pipeline 在 `make_ttgir`：

- `add_accelerate_matmul(pm)`（`326`）决定 `tt.dot` 走 MMA 还是 FMA；
- `capability // 10 in [8, 9]` 时：`add_hopper_warpspec`、`add_assign_latencies`、`add_schedule_loops`、`add_pipeline`（`332-341`）；
- `capability // 10 >= 10` 时：Blackwell 分支，额外有 `add_warp_specialize`、TMEM 相关 pass（`342-352`）；
- 否则（包括 `sm_75`）：只跑 `add_triton_licm`（`358-359`）。

也就是说，**T4 上 `num_stages` 不会进入软件流水线**。这不是“调得不够大”，而是 pipeline 分支根本没进。实测也支持这一点：时钟预热后，`num_stages=2` 和 `4` 的时间几乎一样（见下一节）。

`tt.dot` 的回退在 `AccelerateMatmul.cpp:514-520`：

```cpp
if (computeCapability < 80) {
  dotOp.emitRemark()
      << "Dot op using MMA for compute capability " << computeCapability
      << " has been deprecated. It falls back to the FMA path.";
  return failure();
}
```

所以 T4 上看到 `FFMA` 而不是 `HMMA` 是预期行为，不是编译失败。

### 8. 维护一个后端：长期负担

维护不是“把代码合进去”就结束。你还要处理：

- **LLVM pin**：`cmake/llvm-info.json:2` 记录期望的 LLVM hash；升级 pin 会带来 MLIR API、pass、dialect 的变化，必须重新构建和跑全量测试。
- **API 兼容**：`CUDAOptions` 的字段会被 cache key、metadata、launcher 使用；加字段要更新 `hash()`（`176-180`）和反序列化路径。
- **deprecation**：例如 autotuner 的 `warmup`/`rep`/`use_cuda_graph` 参数已经 deprecated（`autotuner.py:119-123`），但仍保留旧行为；移除前要给出迁移路径。
- **backend 扩展**：`BaseBackend.add_stages` 的契约在 `python/triton/backends/compiler.py:48-59`：stage 按插入顺序执行，中间 stage 返回 `str`，最后一个返回 `bytes`。新后端要遵守这个契约，否则 launcher 接不上。
- **性能回归**：CI 的 microbenchmark “never fail”（`.github/workflows/integration-tests-nvidia.yml:119-121`），它只记录趋势，不阻塞合并。你需要自己的回归基线。
- **issue triage**：先分层（frontend/TTIR/TTGIR/LLIR/PTX/runtime），再要最小 reproducer；不要在没有环境信息的情况下猜。
- **release notes**：`RELEASE.md:32-45` 说明发布节奏；`RELEASE.md:77-88` 说明 cherry-pick 只接受回归修复、关键修复、新特性修复、隔离的新硬件支持、文档和 release 分支专用改动，**feature work 不允许 cherry-pick**。

## 底层机制

### `do_bench` 的统计链

`do_bench` 的返回值不是“GPU 的绝对速度”，而是“在给定 warmup/rep/L2 状态下，`fn()` 的 event 时间分布”。这解释了三个常见现象：

1. **第一次跑很慢**：GPU 从 660 MHz idle 爬到 1590 MHz 需要时间；`warmup=25` ms 可能不够。实测 matmul 第一轮 1.29 ms，后续 0.62 ms。
2. **mean 被长尾拉高**：一次调度抖动或时钟下降会拉高平均值；median 更稳。
3. **L2 命中会骗你**：不 flush 时，重复读同一块数据可能从 L2 命中；`do_bench` 默认 flush，所以更接近真实冷读。

### T4 上的 matmul：FMA 回退的完整证据

**实测**：1024³ fp16 matmul，BM=64/BN=64/BK=32，`num_warps=8`，`num_stages=4`：

```text
PTX:  mma.sync=0  wmma=0  fma.rn=1024  ldmatrix=0  cp.async=0  shared=8192
SASS: FFMA=512  HMMA=0  IMMA=0  LDG=2  LDS=69  STS=8  BAR=6
```

这不是“Triton 没优化”，而是 `AccelerateMatmul.cpp:514-520` 对 `computeCapability < 80` 明确回退 FMA。T4 的 Tensor Core 在这条路径上完全没被使用。

### 寄存器压力：W=4 与 W=8 的差别

**实测**：同一颗 matmul，`ptxas -v`（使用 Triton wheel 自带的 ptxas）：

```text
W=4: 255 registers, 352 bytes stack frame, 508 bytes spill stores, 372 bytes spill loads
W=8: 183 registers, 0 bytes stack frame, 0 bytes spill stores, 0 bytes spill loads
```

性能：

```text
W=4: 1.712 ms, 1.25 TFLOPS
W=8: 0.616 ms, 3.49 TFLOPS
```

W=4 的 255 寄存器已经到上限，ptxas 只能 spill；W=8 把每个线程的工作量摊薄，寄存器降到 183，spill 消失。**“更多 warp”在这里不是提高 occupancy，而是降低每线程寄存器压力**。

### `num_stages` 在 T4 上为什么无效

**实测**：先做 1.5 秒时钟预热，再跑 `do_bench`：

```text
stages=2: 0.6209 ms, 3.459 TFLOPS
stages=4: 0.6213 ms, 3.456 TFLOPS
```

差异在噪声范围内。源码依据是 `third_party/nvidia/backend/compiler.py:358-359`：`capability // 10` 不是 8/9/10 时，只跑 LICM，不进入 `add_pipeline`。T4 的 `75 // 10 = 7`，所以 `num_stages` 没有 pipeline 可调。

如果你不做时钟预热，会看到“stages=4 比 2 快一倍”的假象：

```text
round 0: st2=1.2908 ms, st4=1.2915 ms
round 1: st2=0.6192 ms, st4=0.6148 ms
round 2: st2=0.6103 ms, st4=0.6101 ms
```

第一轮是时钟爬升，不是 stages 的功劳。

### memory-bound 的 add：261 GB/s 意味着什么

**实测**：`n = 2^24`，BLOCK=256，`num_warps=8`，median 0.769 ms，带宽 `3*2^24*4/0.769ms ≈ 261 GB/s`。T4 规格带宽约 320 GB/s，达成率约 82%，说明访问是 coalesced 的；继续加 `num_warps` 或 `BLOCK` 的收益很小，真正的优化是减少字节数（融合、复用、更窄类型）。同一颗 add 用 BLOCK=1024、warps=4 只有 237 GB/s；小 block + 多 warp 在 T4 上更好。

### autotune：它到底在比什么

`Autotuner._bench` 调用 `do_bench(kernel_call, quantiles=(0.5, 0.2, 0.8))`（`autotuner.py:183-184`），比较的是每个 config 的 kernel 时间，不是端到端时间。**实测** `TRITON_PRINT_AUTOTUNING=1` 输出：三组 config 依次 tune，`key` 是 `(4194304, 'torch.float32', 'torch.float32', 'torch.float32')`，0.82 s 后选中 `BLOCK=256, num_warps=4`。换 shape 会重新 tune；`TRITON_CACHE_AUTOTUNING=1` 会写磁盘（`autotuner.py:190-230`），但 cache key 含 Triton key、backend hash、函数 cache key 和环境变量，升级后可能失效。

## 进一步用法/重要变体

### `do_bench` 的三个变体

| 变体 | 适合 | 代价 |
|---|---|---|
| `do_bench` | 常规 kernel、单次 launch | 包含 launch overhead；短 kernel 不准 |
| `do_bench_cudagraph` | 短 kernel、想消除 launch overhead | 需要 CUDA graph；capture 期间不能有非法操作 |
| `do_bench_proton` | 想看 kernel 级时间、排除 memcpy/sync | 需要 Proton；只支持 NVIDIA/AMD |

源码：`testing.py:140-207`（cudagraph）、`testing.py:361-435`（proton）。

### `maxnreg` 和 `max_occupancy`

```python
kernel[grid](..., maxnreg=128)
kernel[grid](..., max_occupancy=2)
```

- `maxnreg` 限制每线程寄存器数，可能把 spill 从“少量”变成“大量”；先用 `ptxas -v` 看当前用量，再决定。
- `max_occupancy` 通过预留 shared memory 限制驻留 CTA（`compiler.py:128-130`），不是“提高 occupancy”，而是“限制 occupancy”。名字容易误解。

### PTX/SASS 检查

```bash
python -c "k=...; open('/tmp/k.ptx','w').write(k.asm['ptx'])"
$TRITON_VENV/lib/python3.12/site-packages/triton/backends/nvidia/bin/ptxas \
  -v -arch=sm_75 /tmp/k.ptx -o /tmp/k.cubin
/usr/local/cuda/bin/nvdisasm -c /tmp/k.cubin | grep -c FFMA
```

**实测踩坑**：系统 `/usr/local/cuda/bin/ptxas` 是 CUDA 12.4 的 8.4，读 Triton 3.9 生成的 PTX 8.8 会报：

```text
ptxas: Unsupported .version 8.8; current version is '8.4'
```

用 Triton wheel 自带的 ptxas，或设置 `TRITON_PTXAS_PATH` 指向它。不要把“系统 ptxas 太旧”误判成“Triton 生成了非法 PTX”。

**实测 warning**：Triton 3.9 运行时会提示 `TRITON_DEFAULT_FP_FUSION is unset: defaulting to FP fusion ENABLED for backward compatibility ... will change to DISABLED in Triton 3.10`。这是 `knobs.py:497-501` 的兼容逻辑，不是你的 kernel 写错；升级 3.10 前要显式设置，否则数值/性能可能变化。

### 性能回归测试

一个可用的回归测试应该：

1. 固定 GPU 型号、驱动、Triton commit、`num_warps`/`num_stages`；
2. 固定输入 shape 和 dtype；
3. 先做时钟预热，再跑 `do_bench`；
4. 用 median 或 p50，不用 mean；
5. 记录带宽/TFLOPS，不只记录 ms；
6. 设置一个宽松阈值（例如 10%），避免噪声导致 flaky；
7. 在 CI 里标记为“趋势”而不是“阻塞”，除非你有稳定的 runner。

### 维护 checklist

- [ ] LLVM pin 是否变化？`cmake/llvm-info.json:2`。
- [ ] `CUDAOptions` 新字段是否进入 `hash()`？旧参数是否还在 deprecation 窗口？
- [ ] 新架构分支是否影响 `sm_75` 的 else 分支？
- [ ] lit/pytest/AMD 测试是否覆盖新路径？
- [ ] microbenchmark 是否有基线？release notes 是否说明 breaking change？
- [ ] cherry-pick 是否符合 `RELEASE.md:77-88`？

## 对比与取舍

| 方法 | 能看到什么 | 看不到什么 | 什么时候用 |
|---|---|---|---|
| `do_bench` | 时间分布、带宽/TFLOPS | 瓶颈原因 | 日常对比、回归 |
| `ncu` |  stall、occupancy、DRAM/L2、指令 mix | 需要权限；有采样开销 | 定位瓶颈 |
| `ptxas -v` | 寄存器、spill、stack | 运行时行为 | 调 `num_warps`/`maxnreg` |
| `nvdisasm` | SASS 指令 mix | 源码映射（无 line info 时） | 确认 FMA/MMA、访存指令 |
| roofline | 理论上限、优化方向 | 实际 stall、调度 | 先判断 memory/compute |

调参取舍：

- **`num_warps`**：小 block 多 warp 在 T4 的 add 上更好；大 block 多 warp 可能增加寄存器/CTA 压力。不要只看 occupancy。
- **`num_stages`**：在 Hopper/Blackwell 上可能显著提高吞吐，但增加 shared memory；在 T4 上无效。
- **`maxnreg`**：能救 occupancy，但可能引入 spill；先看 `ptxas -v`。
- **`num_ctas`**：SM90+ 才有意义；T4 直接拒绝。
- **autotune**：配置越多，编译时间越长；用 `prune_configs_by` 和 `key` 控制搜索空间。

“本地快”不等于“上游可接受”：

- 你的 T4 结论可能不适用于 A100/H100；
- 你的 shape 可能只覆盖一个大 case；
- 你的配置可能只对某个驱动/时钟状态有效；
- 上游还要看代码复杂度、测试覆盖、跨后端影响和维护成本。`CONTRIBUTING.md:68` 明确说性能改进如果“复杂度/收益”不划算会被拒。

## 踩雷集锦

1. **第一次 benchmark 当真理。**
   实测 matmul 第一轮 1.29 ms，预热后 0.62 ms。GPU 从 660 MHz 爬到 1590 MHz 需要时间；先做 1–2 秒时钟预热，再跑 `do_bench`。

2. **用 mean 比较两个只差 3% 的 kernel。**
   长尾和时钟抖动会淹没差异。用 median 或 p20/p50/p80，并重复 3 次。

3. **忘记 L2 flush。**
   重复读同一块数据会命中 L2（T4 只有 4 MiB，但小 kernel 仍可能命中）。`do_bench` 默认 flush；自己写 benchmark 时要手动 `clear_cache`。

4. **`ncu` 没权限就以为 kernel 没问题。**
   `ERR_NVGPUCTRPERM` 是环境限制，不是性能结论。退回 roofline + `ptxas -v` + SASS。

5. **用系统 ptxas 编译 Triton 的 PTX。**
   CUDA 12.4 的 ptxas 只支持 PTX 8.4，Triton 3.9 生成 8.8，直接报 `Unsupported .version 8.8`。用 wheel 自带 ptxas 或 `TRITON_PTXAS_PATH`。

6. **在 T4 上调 `num_stages`。**
   `sm_75` 不进 pipeline 分支（`compiler.py:358-359`），`num_stages` 没有效果。实测 stages 2/4 在时钟预热后都是 0.62 ms。

7. **以为 `num_warps` 越大越好。**
   W=2 的 matmul 出现 996–2822 bytes spill，性能掉到 0.06–0.1 TFLOPS；W=8 才是 3.49 TFLOPS。要看寄存器/spill，不要盲加。

8. **`TRITON_PRINT_AUTOTUNING` 没输出。**
   它只在 `not used_cached_result` 时打印（`autotuner.py:285`）；autotune 结果命中磁盘 cache 时不会打印。清 cache 或换 key。

9. **把 `max_occupancy` 当成“提高 occupancy”。**
   它是通过预留 shared memory 限制驻留 CTA（`compiler.py:128-130`），语义是“上限”，不是“目标”。

10. **本地快 5% 就发 PR。**
   上游会问：测试呢？跨架构呢？复杂度值得吗？没有 lit/pytest 和跨后端说明的性能 PR，通常会被要求补证据。

## 进阶

- **用 roofline 指导优化**：先算 AI，再决定是优化访存还是计算；不要对 memory-bound kernel 调 `num_stages`。
- **用 ncu 的 stall reasons**：如果 `stall_long_scoreboard` 高，通常是内存延迟；如果 `stall_mio_throttle` 高，可能是 shared memory/特殊函数；如果 `stall_wait` 高，可能是依赖链。
- **guided：Hopper/Blackwell 的 pipeline**：在 SM90+ 上比较 `num_stages=2/4`，观察 `cp.async`/TMA、shared memory 和 WGMMA 的交错；T4 没有这些路径。
- **guided：warp specialization**：读 `add_hopper_warpspec` 和 `add_warp_specialize` 的入口（`compiler.py:338`、`351`），理解 producer/consumer 分区。
- **性能 bisect**：固定 benchmark 脚本，用 `git bisect run` 找到第一个变慢的 commit；先确认是代码变化还是时钟/驱动变化。
- **LLVM pin 升级**：先构建、跑 `test-lit`、`test-cpp`、`test-unit`，再比较 microbenchmark；不要只看编译通过。
- **release notes**：读 `RELEASE.md:77-88` 的 cherry-pick 标准，理解为什么 feature work 不能进 release 分支。

## 动手练习

1. **复现 add 的 roofline**：用 `n=2^24`、BLOCK=256、warps=8 跑 `do_bench`，计算 GB/s 和 AI，判断它离 320 GB/s 有多远。
2. **扫 `num_warps`**：对 add 跑 BLOCK∈{256,1024,4096}、warps∈{4,8}，记录 ms、GB/s、`n_regs`，解释为什么 BLOCK=256/warps=8 最好。
3. **复现 matmul 的 spill**：用 BM=64/BN=64/BK=32、fp16、warps=4 和 8 各跑一次，`ptxas -v` 记录寄存器和 spill，解释 2.8 倍差异。
4. **验证 `num_stages` 无效**：先做 1.5 秒时钟预热，再比较 stages=2/4；如果不预热，记录第一轮和后续轮的差异。
5. **ncu 失败记录**：运行本章的 ncu 命令，记录错误；然后写出没有 ncu 时你会看的三个替代指标。
6. **做一个 roofline 表**：给 add、matmul、reduce 三个 kernel 算 AI 和理论上限，判断各自是 memory-bound 还是 compute-bound。
7. **写一个性能回归测试**：固定 shape、dtype、config，先预热，再取 median，设置 10% 阈值，输出带宽/TFLOPS。
8. **维护演练**：假设要把 LLVM pin 从当前 hash 升到下一个版本，列出你会跑的测试和可能受影响的文件。

## 本章重点

- `do_bench` 测的是带 warmup、L2 flush、CUDA event 的时间分布；默认 mean，性能对比建议 median/quantile。
- 第一次测量可能被时钟爬升污染；先预热，再重复。
- roofline 先用 AI 判断 memory-bound 还是 compute-bound，再选优化方向。
- T4 的 add 达到约 261 GB/s（规格 320 GB/s 的 82%），是 memory-bound；matmul 是 compute-bound，但走 FMA 回退。
- `AccelerateMatmul.cpp:514-520` 明确 `sm < 80` 回退 FMA；实测 PTX `mma.sync=0`、SASS `HMMA=0`、`FFMA=512`。
- `num_warps` 的关键不是 occupancy，而是寄存器压力；W=4 spill 508 B，W=8 无 spill，快 2.8 倍。
- `num_stages` 在 T4 不进入 pipeline（`compiler.py:358-359`），调它没有意义。
- 维护后端要管 LLVM pin、options/hash、deprecation、backend 契约、性能回归、triage、release notes。
- “本地快”不等于“上游可接受”；上游看测试、跨架构、复杂度和长期维护成本。

## 自我检核

1. `do_bench` 的 warmup 是时间还是次数？L2 flush 在哪一行？为什么它会影响结果？
2. 一颗 add 的 AI 是 0.083 FLOP/B，T4 的 ridge 约 25.3 FLOP/B，它属于哪一类？优化方向是什么？
3. 为什么 T4 上 `tt.dot` 生成 `FFMA` 而不是 `HMMA`？给出源码行号。
4. W=4 和 W=8 的 matmul，哪个 spill 更多？为什么 W=8 更快？
5. 为什么 `num_stages=4` 在 T4 上没有收益？pipeline 分支的条件是什么？
6. `ncu` 报 `ERR_NVGPUCTRPERM` 时，你会用什么替代手段？
7. 为什么系统 ptxas 编译 Triton PTX 会失败？怎么解决？
8. 一个性能 PR 要满足哪些非性能条件，才可能被上游接受？

## 延伸阅读

1. **`python/triton/testing.py:288-358`**
   - **读哪里**：`do_bench` 的 warmup 估算、L2 flush、event 计时、`_summarize_statistics`。
   - **学什么**：Triton 官方 benchmark 的统计口径；为什么 median/quantile 更稳。
   - **与本章关系**：本章所有性能数字的测量基础；自己写 benchmark 时应复用同一口径。

2. **`third_party/nvidia/backend/compiler.py:119-180`、`326-359`**
   - **读哪里**：`CUDAOptions` 字段、`add_accelerate_matmul`、pipeline 分支。
   - **学什么**：`num_warps`/`num_stages`/`maxnreg` 如何进入编译；`sm_75` 为什么走 else 分支。
   - **与本章关系**：解释 T4 上“调参无效”的源码依据。

3. **`lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp:514-520`**
   - **读哪里**：`computeCapability < 80` 的 FMA 回退。
   - **学什么**：为什么 T4 没有 MMAv2；这个决定是 deprecation，不是 bug。
   - **与本章关系**：PTX/SASS 中 `HMMA=0` 的直接原因。

4. **NVIDIA Nsight Compute 文档**
   - **读哪里**：`https://docs.nvidia.com/nsight-compute/` 的 metrics、occupancy、roofline 章节。
   - **学什么**：`stall_long_scoreboard`、`stall_mio_throttle`、`dram__throughput` 等指标的含义。
   - **与本章关系**：本机 ncu 无权限，但方法仍然适用；有权限的机器上按这些指标定位。

5. **Triton `RELEASE.md` 与 `CONTRIBUTING.md`**
   - **读哪里**：`RELEASE.md:32-45` 的发布节奏、`77-88` 的 cherry-pick 标准；`CONTRIBUTING.md:68` 的性能取舍。
   - **学什么**：上游如何决定“什么能进 release”“什么样的性能改进值得合并”。
   - **与本章关系**：把“本地优化”放到长期维护和发布流程里看。

## 下一章

到这里，课程的主线已经走完：从 Python AST 到 TTIR/TTGIR/LLIR/PTX/SASS，从单颗 kernel 到测试、性能和长期维护。最后一章把这些能力收束成一个可交付的工程：新增一个可验证的 Triton op/pass/lowering，带 lit + pytest、benchmark、跨后端说明和上游风格的设计文档。

→ [Final Project：全栈 Triton 扩展](./final-project-full-stack-triton-extension.md)
