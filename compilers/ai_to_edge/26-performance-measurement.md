# Ch 26 性能测量：在设备上量什么、怎么量

> **目标**：把「这个 kernel 有多快」从一句感觉变成一个带误差棒的数字。学完这章，你能自己写一个可信的 bench 脚本，能算出 GFLOP/s 与 GB/s 并判断瓶颈在哪，也能识别出「这个数字根本不该被汇报」的场合。

> **环境**：Ubuntu 22.04 / AMD EPYC 7K83（28 vCPU，KVM 虚拟机）/ torch 2.9.1+cpu / gcc 11.4 / qemu-aarch64-static 6.2 / qemu-system-arm 6.2

## 为什么需要这个？

性能数字是工程里最容易被伪造、也最容易被相信的东西。它不是被恶意伪造的，而是被**测量方式**伪造的：

- 在 qemu 上跑一个 aarch64 二进制得到 1465 ms，写成「ARM 上要 1.4 秒」。
- 第一次调用 `torch.compile` 的产物，把几秒的编译时间算成推理时间。
- 跑一次 `time ./prog`，正好碰上别的进程抢 CPU，得到一个两倍的假数字。
- 测出 1.28 GB/s，就说「内存带宽吃满了」，其实同一台机器的单线程带宽有 17 GB/s。

这些错误的共同点：**数字本身是真的，结论是假的**。而读者无法分辨，因为他不在你的机器上。

这一章要建立的是判断力：看到一个性能数字，能立刻问出「在哪测的、测了几次、算进去什么了、瓶颈是什么」。四个问题答不上来的数字，不该进 PPT。

## 先建立直觉

把性能测量想成**用一把有弹性的尺子量东西**：

```
尺子本身的误差    = 计时器精度、频率抖动
被测物的形变      = 缓存冷热、编译时间、后台负载
读数的人          = 你选了 min 还是 mean，跑了 1 次还是 30 次
```

三个环节任何一环出错，结论就废了。而最常出错的是**第二个**：你测的不是你想测的东西。

一个具体的例子。同一个 kernel（20000×8192 次元素更新），实测：

```
native x86_64 : elapsed_ms=101.718
qemu-aarch64  : elapsed_ms=1465.162
```

比值 14.4×。如果汇报「ARM 上慢 14 倍」，这句话错得很彻底——qemu 是**逐条翻译指令**的模拟器：一条 ARM 指令变成几十条 x86 指令，还要维护 guest 的寄存器与内存映射。这 14.4× 里 **0% 是硬件差异**。

所以第一条原则：

> **测量必须在目标硬件上做。模拟器只能验证功能，不能测性能。**

## 底层机制

### 第一步：把时间切成三段

任何一次「跑一下」的时间都包含三段，混在一起就什么都测不出来：

| 段 | 内容 | 该不该算进性能 |
|---|---|---|
| 编译 / 加载 | 生成 kernel、JIT、动态链接、读权重 | 不算（除非你专门测首次延迟） |
| 预热 | 填满 cache、TLB、分支预测器、CPU 升频 | 不算，但要跑够次数让它稳定 |
| 稳态执行 | 反复执行同一份工作 | 算，这才是你要的数 |

实测证据，`torch.compile` 的首次调用（`code/10_perf/ind2.py`，4×64 输入）：

```
first  = 3775.0 ms
second = 0.16 ms
steady median = 0.09 ms  min = 0.08 ms
first / steady = 44331x
```

同一个模型、同一份输入，第一次 3775.0 ms，稳态 0.09 ms，**差四万倍**。第一次里几乎全部是 Inductor 生成代码、调 `g++` 编译、再加载 `.so` 的时间。把首次调用写进报告，等于在测编译器。

`44331x` 是用未舍入的 median 算的；拿显示值自己除 `3775.0 / 0.09` 只得 41944，**先舍入再相除会引入 5% 的假误差**。换一份空缓存重跑读到 `6796.9 / 0.18 = 38538x`——比值随缓存与编译器状态变。

清空缓存后连跑三次，读到 `3775.0 / 0.09`（44331×）、`3784.6 / 0.09`（43512×）、`3783.9 / 0.08`（44756×）。三次都在 3775–3785 ms 之间，比「换个缓存目录又变成 6800 ms」稳定得多——**但绝对值仍取决于编译器版本与文件系统缓存**：「慢四万倍」这个量级稳定，绝对值不稳定。

第二组证据说明「预热」不只是「第一次慢」。512³ matmul 单线程连跑 6 次：

```
call 0: 5.213 ms   call 1: 4.178 ms   call 2: 4.313 ms
call 3: 20.238 ms  call 4: 3.758 ms   call 5: 13.638 ms
```
第 3、5 次跳到 20 ms 与 13.6 ms——共享 KVM 虚拟机上（当时 `load average` 40 左右）别的租户在抢 CPU。所以**单次测量毫无意义**，必须多轮取样再统计。

### 第二步：GFLOP/s，公式与代入

性能的基本形式是「工作量 ÷ 时间」。对矩阵乘 `C[M,N] = A[M,K] @ B[K,N]`：

```
FLOPs   = 2 * M * N * K        （每个输出元素 K 次乘 + K 次加）
GFLOP/s = FLOPs / (毫秒/1000) / 1e9
        = 2*M*N*K / (ms * 1e6)
```

代入证据包的四组数（`torch.set_num_threads(1)`）：

| 尺寸 | 时间 | FLOPs | 计算 | GFLOP/s |
|---|---|---|---|---|
| 64³ | 0.0147 ms | 524288 | 524288 / 1.47e-5 | 35.65 |
| 256³ | 0.41 ms | 33554432 | 33554432 / 4.084e-4 | 82.16 |
| 512³ | 3.08 ms | 268435456 | 268435456 / 3.08e-3 | 87.04 |
| 1024³ | 22.20 ms | 2147483648 | 2147483648 / 2.22e-2 | 96.71 |

（64³ 原始计时被四舍五入成 `0.01 ms`；用 `524288 / 35.65e9` 反推得 `0.0147 ms`，表中用反推值，否则算不出 35.65。）

读这张表要读出两件事：

1. **小尺寸的 GFLOP/s 明显低**（35.65 vs 96.71）。64³ 只有 524288 次浮点运算，而调用开销、循环准备、内存搬运是固定的，跑不满流水线。
2. **尺寸越大越接近平台上限**，但 1024³ 也只到 96.71 GFLOP/s。

那 96.71 算高还是低？这台机器 AVX2、无 AVX-512，`/proc/cpuinfo` 报 2545 MHz。按「每周期 8 float × 一条 FMA（2 FLOP）」估上限是 `8*2*2.545e9 ≈ 40.7 GFLOP/s`；即使每周期发两条 256 位 FMA 也只有约 81 GFLOP/s。实测 96.71 高于两种估算，说明实际频率高于报值，或估算模型本身不准。**两个原因我都没有独立验证**；能确定的是 `cpu MHz` 在虚拟化下是估算值，拿它算理论峰值会差两倍以上。

所以更可靠的对照是**你自己的复跑**。同一脚本、同一台机器、几分钟后再跑：

```
matmul 64^3                 0.009    0.009    0.018    58.60
matmul 256^3                0.360    0.361    0.392    93.05
matmul 512^3                2.764    2.774    2.788    96.76
matmul 1024^3              21.894   21.952   22.098    97.82
```

（列依次是 min / median / max 毫秒，最后一列按 median 算。）

64³ 从 35.65 变成 58.60 GFLOP/s，**差了 64%**；1024³ 从 96.71 变成 97.82，只差 1%。原因是测量长度：`0.009 ms` 已接近计时器与调度抖动的量级，`22 ms` 则足够长。**越短的测量越不可信**——要么跑更多次，要么把循环套在大循环里测总时间。

### 第三步：GB/s，以及拿什么当标尺

对 elementwise 算子，瓶颈通常在内存。`relu(x*2+1)`，n = 2^24：

```
数据量 = n*4（读 x） + n*4（写 out） = 2*4*2^24 = 134217728 B ≈ 134.2 MB
GB/s   = 134217728 / (104.84/1000) / 1e9 = 1.28 GB/s
```

**1.28 GB/s 是快还是慢？** 不能凭感觉，要拿实测上限做标尺。我用一个最小 STREAM-triad 在**同一台机器、单线程、绑核**实测：

```c
/* a = b + 2*c，2 读 + 1 写，N = 2^24 个 float，取 5 轮里最快的一轮 */
for (long i = 0; i < N; i++) a[i] = b[i] + 2.0f * c[i];
/* bytes = 3 * N * sizeof(float) = 201326592 */
```

```
triad best = 0.0116 s -> 17.41 GB/s (2R+1W, 1 thread, sink=80)
triad best = 0.0122 s -> 16.54 GB/s
triad best = 0.0118 s -> 17.12 GB/s
```

这台机器的**单线程可用带宽约 17 GB/s**（三次绑不同核：17.41 / 16.54 / 17.12）。对比 1.28 GB/s：

> 只用到单线程带宽的 7%。单线程没饱和内存带宽，瓶颈在别处（指令吞吐、循环开销、或者没有向量化）。

这个判断决定优化方向：误以为「带宽吃满了」会去改数据布局，实际该先看生成的汇编有没有向量化。

**17 GB/s 这个标尺本身也会随环境变。** 同一台机器、同一份 `st3.c`、同样绑三个核，在 `load average` 涨到 26 之后再测：`7.45 / 7.93 / 8.11 GB/s`，只剩一半不到。这意味着 1.28 GB/s 的「7%」可能变成「16%」——**结论方向不变（没吃满带宽），但比例会错一倍**。所以引用任何标尺数都要带时间与负载条件：**单线程、这台虚拟机、这个数组大小、当时的 `load average`**；28 核全开能到多少我没有测。

### 第四步：为什么 qemu 测不出性能

- **user-mode（`qemu-aarch64-static`）**：把每条 ARM 指令翻译成一段 x86 再执行，翻译与系统调用都有开销。实测 14.4× 慢，倍率**取决于指令组合**，所以连「按比例换算」都不行。
- **system-mode（`qemu-system-arm`）**：还要模拟整个 SoC（时钟、外设、中断）。它连**周期数**都不是真周期——Cortex-M3 的指令耗时取决于 flash 等待周期与总线仲裁，qemu 里这些不存在。

qemu 能做什么？**验证功能、验证寄存器地址、观察程序卡在哪**（Ch 27 的主题）。性能一律真机测。

不方便拿真机时有个折中：**同机 A/B 对比**。两个版本都在 qemu 上跑，比值虽然被模拟开销污染，但同方向的变化有参考价值——要明确标注「相对比较，非绝对性能」。

### 第五步：三种计时器

| 场合 | 工具 | 精度 | 注意 |
|---|---|---|---|
| x86 用户态 | `clock_gettime(CLOCK_MONOTONIC)` | 纳秒 | 首选；不要用 `CLOCK_REALTIME`（会被校时干扰） |
| x86 周期级 | `rdtsc` | 1 周期 | 是**常数速率**计数器，不等于核心频率 |
| Cortex-M | `DWT->CYCCNT` | 1 周期 | 要先置 `DEMCR.TRCENA`，否则计数不动 |
| 通用 | `perf stat` | 硬件计数器 | 需要权限；被拒时可能报错，也可能**返回假值**（见第六步） |

`rdtsc` 实测（一个 1000 万次浮点累加的循环，连跑两次）：

```
cycles=112053528  wall_ns=44015419  cycles/ns=2.546
cycles=219938903  wall_ns=86401925  cycles/ns=2.546
```

两次绝对周期数差一倍（负载不同），`cycles/ns` 都是 **2.546**。TSC 按固定速率走（本机 2.546 GHz），**不随核心实际频率变化**。所以测时间用 `clock_gettime`；想测周期得乘上**当时的**核心频率，而那个频率你不知道（虚拟化下尤其不知道）。

Cortex-M 的 `DWT->CYCCNT` 没有这个问题：它就是核心时钟周期。要自己初始化：

```c
/* Cortex-M3/M4：使能 DWT 周期计数器 */
*(volatile uint32_t *)0xE000EDFC |= (1u << 24);   /* DEMCR.TRCENA */
*(volatile uint32_t *)0xE0001004 = 0;             /* DWT->CYCCNT = 0 */
*(volatile uint32_t *)0xE0001000 |= 1u;           /* DWT->CTRL.CYCCNTENA */
```

**这段未实测**：本机没有 Cortex-M 硬件；qemu 不模拟真实周期，读出来的值不能当性能数据。32 位计数器在 100 MHz 下约 43 秒溢出一次，长测量要处理回绕。

### 第六步：`perf stat` 的两种失败，长得很不一样

`perf stat` 读 PMU 硬件计数器，能看 `cycles`、`instructions`、`cache-misses`、`branch-misses`，回答「IPC 是多少」这类时间测不出来的问题。它有**两种**失败模式，多数教程只讲第一种。

**失败一：直接拒绝。** 本机 `perf_event_paranoid = 4`，非 root 跑硬件事件：

```bash
PERF=/usr/lib/linux-tools/5.15.0-126-generic/perf
$PERF stat -e cycles,instructions,branches,branch-misses true; echo "rc=$?"
```

实测（节选）：

```
Error:
Access to performance monitoring and observability operations is limited.
Consider adjusting /proc/sys/kernel/perf_event_paranoid setting to open
access to performance monitoring and observability operations for processes
without CAP_PERFMON, CAP_SYS_PTRACE or CAP_SYS_ADMIN Linux capability.
rc=255
```

**stdout 是空的**，退出码 255。`perf` 还把统计结果全部写在 **stderr**——实测 `sudo -n $PERF stat -e cycles true >/tmp/o.txt 2>/tmp/e.txt` 得到 `0 /tmp/o.txt` 与 `233 /tmp/e.txt`。所以 `perf stat ... | grep cycles` 在管道里**什么都拿不到**，常被误判成「perf 坏了」。

**失败二：给数字，但数字是假的。** 加上 `sudo` 后命令不再报错，退出码 0，给出的却常是**饱和占位值**。连跑 12 次 `sudo $PERF stat -e cycles true`，读到的 `cycles` 依次是
`281474976710654 / 1878672 / 140737488355327 / 140737488355327 / 140737488355327 / 281474976710654 /
140737488355327 / 140737488355327 / 140737488355327 / 422212465065981 / 281474976710654 / 140737488355327`。
`140737488355327 = 2^47 - 1`，`281474976710654 = 2*(2^47-1)`，`422212465065981 = 3*(2^47-1)`：12 次里 11 次是它的整数倍。再采 20 次统计：

```
n = 20   exactly 2^47-1 : 9   multiple (k*S, k>=2) : 11 [k=2 十次, k=3 一次]   plausible : 0
```

**20 次一个可信值都没有**——内核拒绝后返回饱和值，`perf` 再按多路复用的比例 `k` 乘回去。底数已经是假的，缩放只会把假值放大。

这种输出的可怕之处是它**自带一致外观**：值看着很大、IPC 显示 1.00 或 1.85、branch-misses 显示 `100.00%`：

```
140737488355327      cycles
140737488355327      instructions              #    1.00  insn per cycle
140737488355327      branches
140737488355327      branch-misses             #  100.00% of all branches
```

同机另一次读到的 branch-misses 百分比是 `173.99%`、`165.15%`、`194.14%`——miss 不可能超过 branch 数，这种一眼能看出荒谬，但 `100.00%` 看不出来：

> 用 `perf stat` 的第一件事，是拿 `2^47-1` 去除每一个数。能整除，就是假的；整除的倍数 `k` 越大，说明多路复用得越凶，那个数越不可信。这条比「记得看警告」可操作得多，而且能写进脚本。

`task-clock` 是软件事件，`sudo` 下读到正常值 `224.82 msec task-clock # 0.972 CPUs utilized`；但**非 root 在本机连 `task-clock` 也被拒**（同样 rc=255）。所以判据要写成：*在能拿到数据的权限下*，`task-clock` 正常而硬件事件全是 `2^47-1` 的倍数，那是权限或虚拟化问题，不是你的程序慢。

### 第七步：什么时候看 P99 而不是均值

均值假设分布对称，性能分布不是。实测（256³ matmul，单线程，200 次）：

```
n=200 min=360.4 us p50=366.5 p90=377.0 p99=482.9 max=495.8 mean=372.6 us
```

p99 比 p50 慢 32%（482.9 vs 366.5），均值 372.6 看着「还好」，因为它把两次 480 us 的抖动摊平了。

口径选择：**吞吐/批处理**看均值或总时间（跑几百万次，尾部会被平均掉）；**交互式推理**看 **P99/P999**（用户感知的是最慢那次）；**实时控制**看 **max** 与超时次数（一次 500 ms 抖动可能意味着撞车）；**A/B 回归对比**看 **min**（排除外部干扰，反映代码路径的最快可能，对变化最敏感）。

负责的 bench 报告至少给三个数：min 说明代码潜力，median 说明典型表现，max 说明抖动幅度。只给一个，读者无法判断可信度。

## 可跑例子 1：带 warmup 与统计的 bench 脚本

```python
import time, statistics, torch
torch.manual_seed(0)
torch.set_num_threads(1)          # 关掉多线程，否则测的是并行效率

def bench(fn, warmup=5, reps=30):
    for _ in range(warmup):       # 预热：填 cache、让 BLAS 选好 kernel
        fn()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1e3)
    return min(ts), statistics.median(ts), max(ts)

print(f"{'case':24s} {'min/ms':>8s} {'med/ms':>8s} {'max/ms':>8s} {'GFLOP/s':>8s} {'GB/s':>6s}")
for M in (64, 256, 512, 1024):
    a = torch.randn(M, M); b = torch.randn(M, M)
    mn, md, mx = bench(lambda: a @ b)
    print(f"matmul {M}^3{'':13s} {mn:8.3f} {md:8.3f} {mx:8.3f} {2*M**3/(md/1e3)/1e9:8.2f} {'-':>6s}")
n = 1 << 24
x = torch.randn(n)
mn, md, mx = bench(lambda: torch.relu(x * 2 + 1), warmup=3, reps=15)
print(f"elementwise n=2^24{'':8s} {mn:8.2f} {md:8.2f} {mx:8.2f} {'-':>8s} {(n*4*2)/(md/1e3)/1e9:6.2f}")
```

实测输出：

```
case                       min/ms   med/ms   max/ms  GFLOP/s   GB/s
matmul 64^3                 0.009    0.009    0.018    58.60      -
matmul 256^3                0.360    0.361    0.392    93.05      -
matmul 512^3                2.764    2.774    2.788    96.76      -
matmul 1024^3              21.894   21.952   22.098    97.82      -
elementwise n=2^24         111.36   113.35   116.44        -   1.18
```

（同一脚本再跑一次读到 `60.64 / 93.17 / 95.87 / 97.86 GFLOP/s`。后来机器 `load average` 从 17 升到 26，同一份代码再跑变成 `42.91 / 77.22 / 61.69 / 64.32`，`elementwise 141.60 ms / 0.90 GB/s`——**GFLOP/s 掉了一半**。所以引用性能数字时必须连 `load average` 一起给。）

四个设计点各有理由：

- **`warmup=5`**：让 BLAS 完成 kernel 选择、让 cache 预热。少了这步，第一次的数明显偏高。
- **`reps=30`**：样本够算中位数，也够看出 `max` 有多离谱。1 次是赌博，3 次只能看趋势。
- **`statistics.median`**：中位数不被离群值带偏；均值会被一次 20 ms 的抖动毁掉。
- **同时报 min 与 max**：`max/min` 是**抖动系数**。512³ 是 `2.788/2.764 = 1.01`，很稳；64³ 是 `0.018/0.009 = 2.0`，说明这个尺寸不可信。

GFLOP/s 用的是 `md` 不是 `mn`。报「峰值能力」用 `mn`，报「典型表现」用 `md`——**别混用**，这是最常见的口径混乱。

## 可跑例子 2：第一次调用 vs 稳态

```python
import time, torch
torch.manual_seed(0); torch.set_num_threads(1)

class M(torch.nn.Module):
    def __init__(s):
        super().__init__(); s.a = torch.nn.Linear(128, 128); s.b = torch.nn.Linear(128, 128)
    def forward(s, x): return s.b(torch.relu(s.a(x)))

m = M().eval(); x = torch.randn(64, 128)
cm = torch.compile(m)

def t():
    t0 = time.perf_counter(); cm(x); return (time.perf_counter() - t0) * 1e3

print("first  = %.1f ms" % t())
ts = [t() for _ in range(20)]
print("second = %.2f ms" % ts[0])
print("steady median = %.2f ms  min = %.2f ms" % (sorted(ts)[10], min(ts)))
```

实测（每次用新的空 `TORCHINDUCTOR_CACHE_DIR`，连续跑两遍）：

```
first  = 6837.5 ms   second = 0.57 ms   steady median = 0.32 ms  min = 0.30 ms
---
first  = 6755.3 ms   second = 0.51 ms   steady median = 0.36 ms  min = 0.31 ms
---
first  = 7290.7 ms   second = 0.53 ms   steady median = 0.30 ms  min = 0.28 ms
```

三次的稳态都在 0.28–0.36 ms，首次都在 6.8–7.3 秒：**稳态高度可重复，首次只稳定到「秒级」**。

三个可直接用的结论：

1. **编译 6.8–7.3 秒，稳态约 0.3 毫秒。** 只跑一次（离线批处理）时编译占 99.99% 以上，`torch.compile` 是净亏；跑十万次才摊得薄。
2. **第二次（0.51–0.57 ms）仍比稳态（0.28–0.36 ms）慢 42–78%。** 还有懒初始化（分配输出缓冲、首次 icache miss），所以 warmup 至少 2–3 次。
3. **缓存目录决定要不要重编。** 同一目录重启进程会读缓存；换个空目录又回到秒级，容器每次重建文件系统缓存就没了。

边缘设备上很多场景是**一次性进程**（开机跑一次推理），这时候该优化编译产物的大小与加载速度，不是稳态吞吐。

## 可跑例子 3：qemu 与原生，同一个 kernel

```bash
#!/usr/bin/env bash
set -euo pipefail
cat > /tmp/k.c <<'C'
#include <stdio.h>
#include <time.h>
float buf[8192];
int main(){ struct timespec a,b;
  clock_gettime(CLOCK_MONOTONIC,&a);
  for(int it=0;it<20000;it++)
    for(int i=0;i<8192;i++) buf[i]=buf[i]*1.0001f+0.5f;
  clock_gettime(CLOCK_MONOTONIC,&b);
  double ms=(b.tv_sec-a.tv_sec)*1e3+(b.tv_nsec-a.tv_nsec)/1e6;
  printf("elapsed_ms=%.3f\n",ms); return 0;}
C
gcc -O2 /tmp/k.c -o /tmp/k_native
aarch64-linux-gnu-gcc -O2 -static /tmp/k.c -o /tmp/k_a64
echo -n "native x86_64 : "; /tmp/k_native
echo -n "qemu-aarch64  : "; qemu-aarch64-static /tmp/k_a64
```

实测（证据包）：

```
native x86_64 : elapsed_ms=101.718
qemu-aarch64  : elapsed_ms=1465.162
```

复跑两次（低负载）：`native 101.559 / qemu 1368.565`、`native 101.473 / qemu 1376.237`，原生 101.5 ± 0.1 ms，比值 13.5–14.4。

`load average` 26 时同一份二进制：`native 253.246 / qemu 2725.835`，比值 10.8。两个数字都涨了两倍多，比值却从 14.4 掉下来——模拟开销与宿主争抢不按同一比例缩放。

两个观察：**原生可重复性远好于模拟侧**（qemu 的翻译缓存命中率、宿主负载都会影响它）；**比值只能同机、同时段对比，不能反推硬件**——翻译成本取决于指令组合，「A 慢 10 倍、B 慢 12 倍」推不出「A 比 B 快 20%」。

## 可跑例子 4：分配开销如何淹没计算

前面三个例子在讲「怎么测准」，这个例子讲更严重的一件事：**被测的到底是什么**。

`relu(x*2+1)`，`n = 2^24`（64 MB）。按 29 GB/s 带宽，理论上应 4.4 ms 跑完。实测（`code/10_perf/alloc_cost.py`，`taskset -c 0`，15 次取中位数）：

| 写法 | 中位数 | 折算 GB/s |
|---|---|---|
| `x.mul_(1.0)`（原地） | 2.65 ms | 50.73 |
| `torch.add(x, 1, out=buf)`（预分配） | 5.86 ms | 22.91 |
| `x * 2`（每次新分配） | 36.83 ms | 3.64 |
| `x + 1`（每次新分配） | 37.23 ms | 3.61 |
| `relu(x)`（每次新分配） | 37.31 ms | 3.60 |
| `relu(x*2+1)`（3 次新分配） | 112.51 ms | 1.19 |
| 三段链（全预分配） | 17.11 ms | 15.69 |

**读 n 个、算一次、写 n 个，从 2.65 ms 到 112.51 ms，差 42 倍。浮点运算量完全相同。**

三个要点：

**一、`relu(x)` 与 `x + 1` 几乎一样快（37.31 vs 37.23 ms）。** 两者都是「一次分配 + 一次读写」。这反证瓶颈在**分配**不在运算：`relu` 是比较、`+1` 是加法，成本本该有差别，却被分配开销完全淹没。

**二、原地版本 50.73 GB/s 超过了前面测的 29 GB/s。** 不是测错：`memcpy` 那 256 MB 远超本机 64 MB L3，而这里 `x` 只有 64 MB、还跨轮次复用。**带宽不是常数，取决于工作集装不装得进 cache。**

**三、最慢那行 1.19 GB/s，离 29 GB/s 差 24 倍。** 唯一的结构差别是三次 `malloc` 一个 64 MB 的新 tensor——向内核要新页、首次触碰缺页、页清零。这个成本远大于 1600 万次乘法。

自然写法 / 三段预分配 = **6.5x**（比绝对值稳定得多）。

### 对前面章节的印证

- **Ch 7 融合**：省的不只 2 次内存往返，还有 2 次分配。上面 112 ms → 17 ms 是这个收益的上限
- **Ch 8 in-place**：Inductor 把 `relu(x+1)` 编成单指针 kernel（`in_out_ptr0` 既读又写），正是把最慢写法变成最快写法
- **Ch 22 arena**：一次 `malloc` 大块、全用偏移量，目标就是消掉前四行与后四行的差距

**报告规则**：报模型推理性能必须写清分配策略。eager 默认写法与预分配写法能差一个数量级，只给一个数字的报告读者无法复现。

## 踩雷集锦

1. **不 warmup 就测**
   错误直觉：第一次和后面一样。实测 `torch.compile` 首次 3775.0 ms、稳态 0.09 ms，差四万倍；连普通 512³ matmul 第 0 次也比第 1 次慢 25%（5.213 vs 4.178 ms）。纠正：warmup 至少 3 次，首次单独报成「冷启动」。

2. **用 qemu 数字汇报硬件性能**
   错误直觉：「模拟器上 1465 ms，所以 ARM 要 1.4 秒」。实测同 kernel 原生 101.718 ms，这 14.4× 由指令翻译与 guest 状态维护构成，**不含目标硬件信息**。纠正：模拟器只做功能验证；性能必须真机，或明确标「相对比较，非绝对性能」。

3. **只测一次，也不看环境基准与频率**
   错误直觉：跑一遍就够，机器状态恒定。实测 512³ 连跑 6 次是 `5.213 / 4.178 / 4.313 / 20.238 / 3.758 / 13.638`，抖动 5 倍；同一脚本在 `load average` 17 与 26 下测得 `96.76` 与 `61.69 GFLOP/s`，差 36%。纠正：至少 30 次并报 min/median/max，`max/min > 1.5` 判环境不干净；引用数字时连 `load average` 与绑核方式一起写；真机上先看 `cpufreq`（`scaling_governor` 是 `powersave` 还是 `performance`），再做 `taskset` 绑核。

4. **把编译时间算进运行时间**
   错误直觉：`time python x.py` 就是推理时间。实测 Inductor 编译 6.8 秒、稳态 0.3 ms，编译占 99.99% 以上。纠正：编译与执行分开计时，用 `TORCHINDUCTOR_CACHE_DIR` 复用缓存，分别报冷/热启动。

5. **把内存带宽当算力，或反过来**
   错误直觉：1.28 GB/s 说明带宽吃满了。实测同机单线程 triad 低负载时 17.41 GB/s、高负载时 7.45 GB/s，1.28 只是 7% 到 16%。纠正：先算算术强度（`relu(x*2+1)` 约 `3 FLOP / 8 B ≈ 0.4`，典型 memory-bound 形态），再拿实测带宽当标尺。这里是「形态是 memory-bound，但单线程连带宽都没吃满」，瓶颈在指令吞吐，该先看有没有向量化。

## 本章重点整理

- 一次「跑一下」分三段：**编译/加载、预热、稳态**。混着测什么都测不出来。实测 `torch.compile` 3775.0 ms（首次）vs 0.09 ms（稳态），差四万倍。
- `GFLOP/s = 2*M*N*K / (ms*1e6)`，代入 512³/3.08 ms 得 87.04；`GB/s = 字节数/(ms*1e6)`，`2^24` 的 `relu(x*2+1)` 得 1.28 GB/s。
- 判断快慢要拿**实测上限**当标尺：同机单线程 triad 在低负载时约 17 GB/s，1.28 只用到 7%，瓶颈不在内存而在指令吞吐；`load average` 升到 26 后同一标尺掉到 8 GB/s，比例会差一倍。
- **qemu 不能测性能**：同 kernel 原生 101.718 ms、qemu 1465.162 ms；它的合理用途是同机 A/B 与功能验证。
- 计时器分工：`clock_gettime(CLOCK_MONOTONIC)` 测时间；`rdtsc` 测周期（常数速率，实测 2.546 cycles/ns）；Cortex-M 用 `DWT->CYCCNT`（本章未实测）。
- `perf stat` 有两种失败：非 root 直接报错 rc=255（结果只写 stderr，管道里拿不到）；有权限时却可能给出 `2^47-1` 的整数倍假值（20 次采样 0 个可信）。判据是拿 `2^47-1` 去除。
- 口径：吞吐看均值，交互看 P99，实时看 max，A/B 看 min。实测 256³ 的 p99 比 p50 慢 32%（482.9 vs 366.5 us）。
- 越短的测量越不可信：64³ 从 35.65 到 58.60 GFLOP/s（+64%），1024³ 只差 1%。
- 引用数字要带环境：同一脚本在 `load average` 17 与 26 下，512³ 读到 96.76 与 61.69 GFLOP/s，triad 标尺从 17 掉到 8 GB/s。
- **分配开销能淹没计算**：`relu(x*2+1)` 每次新分配 112.51 ms，三段全预分配 17.11 ms，原地 2.65 ms——同一份数学差 42 倍。`relu(x)` 与 `x+1` 耗时几乎相同，反证瓶颈在分配不在运算。
- 带宽不是常数：64 MB 原地操作测得 50.73 GB/s，256 MB 的 `memcpy` 只有 29 GB/s。工作集装不装得进 L3 决定这个数。

## 自我检核

- [ ] 不看笔记，能写出 GFLOP/s 与 GB/s 的公式，并各代入一组数字验证
- [ ] 能解释 `torch.compile` 首次 3775.0 ms 与稳态 0.09 ms 的差别来自哪一段，以及什么场景该优化哪一段
- [ ] 能说出两个「qemu 数字不能当硬件性能」的理由，以及 qemu 唯一合理的用途
- [ ] 看到 `perf stat` 输出四个事件都是 `140737488355327`，能判断问题出在哪、怎么用 `2^47-1` 验证
- [ ] 给定「p50=366.5 us、p99=482.9 us」，能说明该用哪个指标以及为什么
- [ ] 能设计一个 bench：warmup 几次、reps 几次、报哪几个统计量、抖动系数多大就判定环境不干净
- [ ] `relu(x*2+1)` 自然写法 112 ms、原地 2.65 ms，浮点运算量相同。说出差距来源，以及你会用什么实验证明它

## 延伸阅读

- **[Systems Performance: Enterprise and the Cloud](https://www.brendangregg.com/systems-performance-2nd-edition-book.html)** — Brendan Gregg, 2nd ed., 2020
  - **读哪里**：第 2 章 `Methodology`（`Performance Counting` 与 `Benchmarking` 两节）、第 6 章 `CPUs`
  - **学什么**：怎么设计不带偏差的基准、为什么「主动 benchmark」容易失真、`perf` 的方法论
  - **和本章关联**：本章的测量陷阱（warmup、单次、口径混用）在这本书里有系统化框架。前提是懂 Linux 基本工具。

- **[perf Examples](https://www.brendangregg.com/perf.html)** — Brendan Gregg
  - **读哪里**：`perf stat` 一节，以及 `Common Problems`
  - **学什么**：各事件的物理含义、`IPC` 怎么读、什么情况下计数器不可信
  - **和本章关联**：本章实测的 `2^47-1` 占位值就是这类问题的现场。读完能判断「这个 perf 输出是不是假的」。

- **[Intel SDM Vol. 3B, Ch. 18 `Performance Monitoring`](https://www.intel.com/content/www/us/en/developer/articles/technical/intel-sdm.html)**
  - **读哪里**：18.1 概览、18.2 architectural performance monitoring、18.17 `rdtsc`/`rdpmc`
  - **学什么**：PMU 计数器怎么配、`rdtsc` 为什么是「常数速率」而非核心频率、事件多路复用的误差来源
  - **和本章关联**：解释实测 `cycles/ns` 恒为 2.546 的原因，以及为什么用 `rdtsc` 除核心频率经常算错。

- **[ARM Cortex-M3 Technical Reference Manual, Ch. 11 `Debug`](https://developer.arm.com/documentation/ddi0337/latest/)**
  - **读哪里**：11.4 `DWT` 一节，`CYCCNT`、`CTRL.CYCCNTENA` 与 `DEMCR.TRCENA`
  - **学什么**：Cortex-M 上唯一的周期级计时器怎么开、为什么必须先开 `TRCENA`、溢出周期
  - **和本章关联**：给本章 `DWT->CYCCNT` 初始化代码提供依据。32 位计数器在 100 MHz 下约 43 秒溢出一次。

- **[STREAM: Sustainable Memory Bandwidth](https://www.cs.virginia.edu/stream/)** — John McCalpin
  - **读哪里**：`What is STREAM?` 与 `Run Rules` 两节
  - **学什么**：怎么规范测内存带宽、为什么 `Copy/Scale/Add/Triad` 结果不同
  - **和本章关联**：本章那个 17.41 GB/s 就是它的极简版。想测多核带宽上限，用它而不是自己拍。

数字可信了，下一个问题是：数字不对的时候，怎么找到是哪一层错了。

→ [Ch 27 调试：从错误现象反推出错层](./27-debugging.md)
