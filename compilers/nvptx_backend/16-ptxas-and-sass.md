# Ch 16 — ptxas 接手：PTX → SASS 的真实分配与调度

> **目标**：能看懂 `ptxas -v` 与 `cuobjdump -res-usage`，用寄存器数算 occupancy，并从 SASS 里读出 `ptxas` 做了哪些 PTX 层没做的事。

## 为什么需要这个？

LLVM 只到 PTX 为止（Ch 1）。真正的物理寄存器分配、spill、指令调度、编码都在 `ptxas`。如果你只看 PTX，会漏掉一半的优化与一半的坑。

这一章也是「CUDA 兼容」的验收关：同一份 PTX 在自研 GPU 上要跑出与 T4 相当的性能，你必须知道 `ptxas` 到底做了什么。

## 先建立直觉

```
PTX（虚拟） ──ptxas──► SASS（物理）
  %r<9>                  R0..R11
  无时序                  stall / yield / 双发射
  无 spill               LDL/STL
  逻辑指令               IMAD.WIDE / LDG.E.SYS / ...
```

## 一、`ptxas` 的命令行与输出

```bash
ptxas -arch=sm_75 add.ptx -o add.cubin -v
```

真实输出：

```
ptxas info    : Compiling entry function 'add' for 'sm_75'
ptxas info    : Function properties for add
    0 bytes stack frame, 0 bytes spill stores, 0 bytes spill loads
ptxas info    : Used 12 registers, 380 bytes cmem[0]
```

四个关键数字：

- **registers**：每线程物理寄存器数，直接决定 occupancy。
- **stack frame**：local 栈大小。
- **spill stores/loads**：溢出到 local 的字节数；非零意味着寄存器压力过大。
- **cmem[0]**：常量 bank 0 的用量（kernel 参数在这里）。

`cuobjdump -res-usage` 给同样的信息，更适合脚本化：

```
Function add:
  REG:12 STACK:0 SHARED:0 LOCAL:0 CONSTANT[0]:380
```

## 二、三个生产者，同样的资源

`vadd` 的三条路径（Ch 0、练习 B）：

| 来源 | PTX 行数 | REG | STACK | SHARED |
|---|---|---|---|---|
| llc（手写 IR） | ~40 | 12 | 0 | 0 |
| clang + llc | ~40 | 12 | 0 | 0 |
| nvcc | ~45 | 12 | 0 | 0 |

**PTX 的风格差异没有传导到资源用量。** 这说明 `ptxas` 会把不同写法归一化到相近的 SASS。对拍时，先比资源，再比指令。

## 三、从寄存器数算 occupancy

T4 的实测规格（`cudaDeviceProp`）：

```
SMs=40  regsPerSM=65536  maxThreadsPerSM=1024
sharedPerSM=65536  sharedPerBlock=49152
```

公式：

```
每 block 寄存器 = threads_per_block × regs_per_thread
regs 限制 block 数 = floor(regsPerSM / 每 block 寄存器)
线程限制 block 数 = floor(maxThreadsPerSM / threads_per_block)
实际 block 数 = min(两者, 架构的 block 上限)
occupancy = (实际 block 数 × threads_per_block) / maxThreadsPerSM
```

以 12 regs、256 threads 为例：

- 每 block 寄存器 = 256 × 12 = 3072
- regs 限制 = 65536 / 3072 = 21 个 block
- 线程限制 = 1024 / 256 = 4 个 block
- → 4 个 block，1024 线程，**100% occupancy**

把寄存器拉到 65：

- 每 block = 256 × 65 = 16640
- regs 限制 = 65536 / 16640 = 3 个 block
- → 768 线程，**75% occupancy**

这就是「寄存器预算」的真实含义：**它是 occupancy 的闸门**，不是「少用点寄存器更快」。

## 四、读 SASS：`ptxas` 做了什么

同一份 PTX 的 SASS（T4）：

```
MOV R1, c[0x0][0x28]
S2R R2, SR_CTAID.X
S2R R3, SR_TID.X
IMAD R2, R2, c[0x0][0x0], R3
ISETP.GE.AND P0, PT, R2, c[0x0][0x178], PT
@P0 EXIT
MOV R3, 0x4
IMAD.WIDE R4, R2, R3, c[0x0][0x168]
IMAD.WIDE R6, R2, R3, c[0x0][0x160]
LDG.E.SYS R5, [R4]
LDG.E.SYS R6, [R6]
IMAD.WIDE R2, R2, R3, c[0x0][0x170]
FADD R9, R6, R5
STG.E.SYS [R2], R9
EXIT
```

| SASS | 对应 PTX | `ptxas` 的动作 |
|---|---|---|
| `IMAD.WIDE` | `mul.wide.s32` + `add.s64` | 把乘加融合成一条 |
| `LDG.E.SYS` | `ld.global.b32` | 选带缓存/一致性修饰的 load |
| `MOV R3, 0x4` | 常量 4 | 常量物化 |
| `@P0 EXIT` | `@%p1 bra` | 用谓词退出替代分支 |
| 控制位（不显示） | — | stall、yield、双发射 |

## 五、`ptxas` 的版本敏感性

同一份 PTX，不同 `ptxas` 版本可能给出不同 REG、不同 SASS，甚至触发不同的 bug。Triton 把 `get_ptxas_version()` 拼进 backend hash，就是因为这个（`compilers/triton_source_deep_dive` Ch 28）。

对自研 GPU 的启示：

- 你的「ptxas 等价物」必须版本化，并且可回归。
- 不要把 PTX 层的资源用量当成最终性能指标。
- 对拍要固定工具链版本。

## 六、驱动 JIT

`cuModuleLoad` 吃 cubin；`cuModuleLoadData` 可以直接吃 PTX 字符串，由驱动内部 JIT。这条路径的代价是启动时的编译时间，收益是「一份 PTX 适配多代 GPU」。

版本不匹配时（PTX 版本高于驱动支持），会得到 `CUDA_ERROR_UNSUPPORTED_PTX_VERSION` 一类错误。发行版要为此准备降级路径。

## 踩雷集锦

1. **用 PTX 的 `.reg` 数量算 occupancy**：要用 cubin 的 `REG`。
2. **以为 `--maxrregcount` 一定生效**：低于 ABI/实现下限时会被顶住（实测 cap=8 仍用 12）。
3. **比较不同 `ptxas` 版本的 REG**：没有意义，先固定版本。
4. **以为 SASS 顺序等于 PTX 顺序**：`ptxas` 会重排、融合、换指令。
5. **忽略 spill 统计**：`spill stores/loads` 非零时，性能可能断崖。

## 进阶：自己算 occupancy 与 roofline

把 `REG`、`SHARED`、`threads_per_block` 代入上面的公式，先判断「是不是 occupancy 限制」；再算算术强度（FLOP/byte）判断 compute-bound 还是 memory-bound。两步做完，再决定优化方向——这比盲调 kernel 有效得多。

## 动手练习

1. 用 `ptxas -v` 编三个 kernel（vadd、shared tile、alloca），记录 REG/spill。
2. 用上面的公式算 128/256/512 threads 下的 occupancy。
3. 把 `__launch_bounds__(128,4)` 加上，看 REG 是否被压到 16 以下。
4. 找一条 `ptxas` 融合出来的 SASS（例如 `IMAD.WIDE`），写出它对应的 PTX 组合。

## 本章重点整理

- `ptxas` 负责物理分配、spill、调度、编码；PTX 只是它的输入。
- `REG` 是 occupancy 的闸门；用公式算，不靠猜。
- SASS 会把 PTX 的乘加、地址计算、分支进一步融合。
- 工具链版本会改变资源用量，必须固定版本对拍。

## 自我检核

- [ ] 能读懂 `ptxas -v` 的四个数字
- [ ] 能独立算出给定 REG/threads 的 occupancy
- [ ] 能举出三个 `ptxas` 在 PTX 之上做的优化
- [ ] 知道驱动 JIT 的代价与版本限制

## 延伸阅读

- **[CUDA Binary Utilities](https://docs.nvidia.com/cuda/cuda-binary-utilities/)** — NVIDIA
  - 读 `cuobjdump -res-usage`、`-sass` 两节；本章命令的官方说明。
- **[CUDA C++ Best Practices: Occupancy](https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/#occupancy)** — NVIDIA
  - 读 occupancy 计算与寄存器限制两节，对照本章公式。
- **[Triton 源码：ptxas 版本与 cache](https://github.com/triton-lang/triton)** — Triton
  - 搜 `get_ptxas_version`，看工业级项目如何处理工具链差异。

最后两章：怎么给 NVPTX 加东西，以及它的能力边界在哪里。

→ [Ch 17 扩展与边界：加一个 intrinsic，以及自研 GPU 还差什么](./17-extending-and-limits.md)
