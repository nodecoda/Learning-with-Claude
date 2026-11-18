# Ch 3 — PTX 契约（二）：版本、目标与特性门控

> **目标**：搞清 `sm_XX`、`ptxYY`、`.version`、`.target` 四个东西的关系，并能解释「为什么同一份 IR 换个 `-mcpu` 就编不过」。

## 为什么需要这个？

PTX 不是一份静态规格，它每年随 CUDA 长出新指令（`cp.async`、`wgmma`、`tcgen05`）。这些指令不能无条件发射，否则老驱动/老架构会拒收。于是 PTX 用两层版本控制：

- `.version N.M`：语法/指令集的版本（PTX ISA）。
- `.target sm_XX`：虚拟目标架构，决定哪些硬件特性可用。

LLVM 的 `-mcpu` 一次控制这两者，但**默认值可能不是你想要的**。本机 `llc` 默认 `sm_75` + PTX 6.3；clang 编 CUDA 时却默认 `+ptx84`。这个差异会让你对不上两边的产物。

## 先建立直觉

```
-mcpu=sm_75  ──►  .target sm_75   （能用什么硬件特性）
                └► .version 6.3   （最低语法版本）
-mattr=+ptx84 ──►  .version 8.4   （把语法版本抬高）
```

这不是「版本越高越好」：PTX 版本越高，能吃它的 `ptxas`/驱动越新。发行版要兼容老驱动，通常故意用较低的 `.version`。

## 一、架构到最低 PTX 版本的映射

`NVPTXSubtarget.cpp` 里有一张表（`minPTXVersion`），本机源码实测值：

| `-mcpu` | 最低 PTX | 本机实测 `.version` |
|---|---|---|
| `sm_70` | 6.0 | — |
| `sm_75` | 6.3 | `6.3` |
| `sm_80` | 7.0 | `7.0` |
| `sm_90` | 7.8 | — |
| `sm_90a` | 8.0 | `8.0` |
| `sm_100` | 8.6 | — |
| `sm_120` | 8.7 | — |

复现：

```bash
for cpu in sm_75 sm_80 sm_90a sm_100a; do
  echo "=== $cpu ==="
  $BIN/llc -mtriple=nvptx64 -mcpu=$cpu -O2 add.ll -o - | head -6
done
```

真实输出：

```
=== sm_80 ===
.version 7.0
.target sm_80
=== sm_90a ===
.version 8.0
.target sm_90a
=== sm_100a ===
.version 8.6
.target sm_100a
```

（`sm_75` 的 6.3 见上一章。）

## 二、手动抬高版本

`+ptxNN` 是一个 target feature，可以直接用 `-mattr` 打开：

```bash
$BIN/llc -mtriple=nvptx64 -mcpu=sm_75 -mattr=+ptx84 -O2 add.ll -o - | head -6
```

```ptx
.version 8.4
.target sm_75
```

版本上去了，目标没变。这正好用来做实验：同一份 IR、同一个 `sm_75`，只改 PTX 语法版本，看 `ptxas` 行为是否变化。

## 三、clang 为什么默认 8.4

```bash
$BIN/clang -x cuda --cuda-path=/usr/local/cuda --cuda-gpu-arch=sm_75 \
  --cuda-device-only -O3 -S -o /dev/null vadd.cu -### 2>&1 \
  | tr ' ' '\n' | grep -e ptx -e nvptx
```

真实输出里能看到：

```
"nvptx64-nvidia-cuda"
"+ptx84"
```

clang 24 在本机 CUDA 12.4 环境下选择 `+ptx84`，于是同样的 kernel：

| 生产者 | `.version` | 参数读取写法 |
|---|---|---|
| `llc`（默认） | `6.3` | `ld.param.b32 %r1, [vadd_param_3]` |
| `clang`（device-only） | `8.4` | `ld.param::entry.b32 %r1, [vadd_param_3]` |

`::entry` 是 PTX 8.4 才有的 entry 参数空间写法（LLVM 内部 addrspace 101）。**这不是优化开关，而是语法版本差异**；拿两份 PTX 对比时先对齐 `.version`，否则会得出错误结论。

## 四、特性门控的真实报错

动态 alloca 需要 PTX ≥ 7.3 + sm_52。用默认 6.3 编：

```bash
$BIN/llc -mtriple=nvptx64 -mcpu=sm_75 -O2 dyn.ll -o dyn.ptx
```

真实报错（逐字）：

```
error: <unknown>:0:0: in function dyn void (i64, ptr): Support for stackrestore requires PTX ISA version >= 7.3 and target >= sm_52.
error: <unknown>:0:0: in function dyn void (i64, ptr): Support for dynamic alloca introduced in PTX ISA version >= 7.3 and requires target sm_52.
error: <unknown>:0:0: in function dyn void (i64, ptr): Support for stacksave requires PTX ISA version >= 7.3 and target >= sm_52.
```

加上 `-mattr=+ptx73` 后成功，产物出现：

```ptx
.version 7.3
...
stacksave.u64 	%rd4;
alloca.u64 	%rd8, %rd7, 8;
stackrestore.u64 	%rd4;
```

这就是「门控」的完整闭环：**IR 合法不代表 PTX 合法**；后端会在发射前检查版本，不满足就报错而不是生成坏代码。

## 五、32 位与 64 位地址

`nvptx` 与 `nvptx64` 的差别不只是 triple：

```bash
$BIN/llc -mtriple=nvptx -mcpu=sm_75 -O2 add.ll -o - | sed -n '1,18p'
```

```ptx
.version 6.3
.target sm_75
.address_size 32
...
	.param .u32 .ptr .align 1 add_param_0,
```

指针参数从 `.u64` 变成 `.u32`，`.address_size` 也变成 32。真实 GPU 上几乎总是用 `nvptx64`；`nvptx` 主要用于早期架构或特殊模拟环境。

## 六、`ptxas -arch` 和 `.target` 的关系

一个容易误解的点：`ptxas -arch` 可以比 `.target` 高。实测：

```bash
ptxas -arch=sm_80 add.ptx -o x.cubin     # add.ptx 里是 .target sm_75
cuobjdump -sass x.cubin | head -4
```

```
code for sm_80
.headerflags "EF_CUDA_TEXMODE_UNIFIED EF_CUDA_64BIT_ADDRESS EF_CUDA_SM80 EF_CUDA_VIRTUAL_SM(EF_CUDA_SM75)"
```

`-arch` 决定**物理目标**（SASS 编给谁），`.target` 记录**虚拟目标**（PTX 假设的指令集）。所以：

- 想要 sm_80 的指令选择，必须让 `llc` 输出 `.target sm_80`。
- 只把 `ptxas -arch` 调高，不会自动获得新指令，只会在新架构上跑旧指令集。

## 踩雷集锦

1. **以为 `.version` 越高越好**：高版本 PTX 在老驱动上会被拒；发行版要按最低支持驱动选版本。
2. **拿 clang 和 llc 的 PTX 直接 diff**：`.version`、`ld.param::entry`、参数顺序都会不同，先对齐再比。
3. **忘记 `-mattr=+ptx73` 就编动态 alloca**：会得到上面那串错误，而不是「自动升级」。
4. **以为 `-mcpu=sm_90a` 只是换个名字**：`a` 后缀是架构加速特性集（accelerated），和 `sm_90` 不是同一个目标。
5. **把 `-arch` 当成「重定向 PTX」**：它只影响 ptxas 的物理目标，不改 PTX 里的虚拟目标。

## 进阶：版本选择是产品决策

对自研 GPU 或 CUDA 兼容层，`.version` 的选择等价于「最低支持哪版驱动」：

- 选低了：新指令用不了，性能吃亏。
- 选高了：老用户升级驱动，兼容性投诉。

常见做法是**双产物**：一份低版本 PTX 保底，一份高版本 cubin 走快速路径（fatbin 里同时装）。这正是 `-gencode` 背后的逻辑。

## 动手练习

1. 跑一遍 `sm_75/80/90a/100a` 的矩阵，把 `.version` 和 `.target` 记成表。
2. 用 `-mattr=+ptx73` 编 `dyn.ll`，找出产物里三个新增指令。
3. 用 `-mattr=+ptx80` 但 `-mcpu=sm_75`，看是否报错；解释为什么。
4. 把 `ptxas -arch=sm_90` 用在 `.target sm_75` 的 PTX 上，看 `headerflags` 的 `VIRTUAL_SM` 是什么。

## 本章重点整理

- `.version` 管语法，`.target` 管硬件特性，`-mcpu` 一次决定两者。
- 默认 `sm_75` + PTX 6.3；clang 默认 `+ptx84`，对比前先对齐。
- 版本门控在发射期检查，不满足直接报错。
- `ptxas -arch` 是物理目标，不改变 PTX 的虚拟目标。

## 自我检核

- [ ] 能说出 `sm_75` 对应哪个最低 PTX 版本
- [ ] 能解释 `+ptx84` 与 `-mcpu=sm_80` 的区别
- [ ] 知道动态 alloca 为什么需要 PTX 7.3
- [ ] 能解释 `EF_CUDA_VIRTUAL_SM` 的含义

## 延伸阅读

- **[PTX ISA: Release Notes](https://docs.nvidia.com/cuda/parallel-thread-execution/#release-notes)** — NVIDIA
  - 查每个 PTX 版本新增了哪些指令；本章的 `alloca.u64` 就在 7.3 的条目里。
- **[LLVM NVPTX Usage](https://llvm.org/docs/NVPTXUsage.html)** — LLVM 官方
  - 读「CPU Architectures」与「Target Features」两节，确认 `+ptxNN` 的写法。
- **[CUDA Compatibility](https://docs.nvidia.com/deploy/cuda-compatibility/)** — NVIDIA
  - 讲驱动/工具链/PTX 版本的三方兼容矩阵；做产品路线时必读。

版本对齐之后，才能谈 ABI：kernel 参数到底放在哪、怎么传、返回怎么回。

→ [Ch 4 内核 ABI 与调用约定](./04-kernel-abi.md)
