# 练习 B — Layout 与 TTGIR：从编码读出线程映射

> **目标**：给你一个 `#ttg.blocked` / `#ttg.slice` / `#ttg.dot_op` 编码，你能算出“哪个线程拿哪些元素”；给你一段 TTGIR，你能判断哪些 `ttg.convert_layout` 是必要的，哪些应该被 `RemoveLayoutConversions` 消掉。

> **环境**：Triton 3.9.0（commit `a972119e92`），T4/sm75 或任意 NVIDIA GPU；需要构建出 `triton-opt` 和 `triton-tensor-layout`。没有构建时，用 `guided` 方式读测试文件和源码，不要伪造工具输出。

## 背景与动机

TTIR 里的 tensor 只有 shape 和 element type；到了 TTGIR，每个 tensor 多了一个 **encoding**，它回答一个非常具体的问题：

> 这个逻辑 tensor 的每个元素，在物理上由哪个 CTA、哪个 warp、哪个 lane、第几个 register 持有？

Triton 的性能问题，很大一部分不是“算术写错了”，而是 layout 选错了：

- load 的 layout 不 coalesced，global memory 事务数翻倍；
- dot 的 operand layout 不匹配 MMA 指令，插入大量 `convert_layout`；
- reduce 的跨 warp 通信走了 shared memory，而本可以只用 warp shuffle；
- `convert_layout` 在循环里反复出现，流水线被打破。

这个练习先不谈优化，先练“读 layout”。读不懂 layout，后面所有 pass 都只能靠猜。

## 任务规格

给定一个 transpose + scale 的 kernel：

```python
@triton.jit
def transpose_scale(x_ptr, y_ptr, M, N, BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr):
    pid_m = tl.program_id(0)
    pid_n = tl.program_id(1)
    offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    mask = (offs_m[:, None] < M) & (offs_n[None, :] < N)
    x = tl.load(x_ptr + offs_m[:, None] * N + offs_n[None, :], mask=mask, other=0.0)
    y = tl.trans(x) * 2.0
    tl.store(y_ptr + offs_n[:, None] * M + offs_m[None, :], y, mask=tl.trans(mask))
```

你要交付：

1. 一份 TTGIR 节选，标出所有 `#ttg.blocked`、`#ttg.slice`、`ttg.convert_layout`；
2. 对 `#blocked0 = #ttg.blocked<{sizePerThread=[1], threadsPerWarp=[32], warpsPerCTA=[4], order=[0]}>` 和 `tensor<128xf32>`，画出 128 个元素到 128 个线程的映射；
3. 对 `tensor<64x64xf32>` 的 coalesced row-major layout，解释 `sizePerThread`、`threadsPerWarp`、`warpsPerCTA`、`order` 四个字段各自控制什么；
4. 故意把 store 的 layout 改成列优先，观察 `ttg.convert_layout` 是否增加；
5. 用 `triton-opt -tritongpu-coalesce` 和 `-tritongpu-remove-layout-conversions` 对比输出。

限制：

- 不许只看 PTX 的 `ld.global`；必须先看 TTGIR。
- 不许把 `tl.trans` 删掉来“消除 convert_layout”；transpose 是语义的一部分。
- 如果你在 T4 上跑，`BLOCK_M=BLOCK_N=64`，`M=N=64` 或 `128`；不要用超大 shape，否则 IR 会很难读。

## 期望输出示例

### 1D blocked layout 的线程映射

对：

```mlir
#blocked0 = #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>
```

`tensor<128xf32>` 的映射是：

| 线程 | warp | lane | 元素下标 |
|---|---:|---:|---|
| 0 | 0 | 0 | 0 |
| 1 | 0 | 1 | 1 |
| ... | ... | ... | ... |
| 31 | 0 | 31 | 31 |
| 32 | 1 | 0 | 32 |
| ... | ... | ... | ... |
| 127 | 3 | 31 | 127 |

因为 `sizePerThread=[1]`，每个线程只拿 1 个元素；`threadsPerWarp=[32]` 让 lane 沿第 0 维展开；`warpsPerCTA=[4]` 让 4 个 warp 沿第 0 维继续展开；`order=[0]` 表示第 0 维是 fastest-changing。

如果 `sizePerThread=[4]`，则每个线程拿 4 个连续元素，映射变成：

| 线程 | 元素下标 |
|---|---|
| 0 | 0,1,2,3 |
| 1 | 4,5,6,7 |
| ... | ... |
| 31 | 124,125,126,127 |

注意：这里说的是 **register 内的元素顺序**；实际 vectorized load 能不能合并成一条 `ld.global.v4`，还要看对齐、mask、divisibility 和 backend 的 `LoadOpConversion`。

### 2D coalesced layout

`test/TritonGPU/coalesce.mlir` 里有一个真实的 row-major load 期望：

```mlir
// CHECK: [[row_layout:#.*]] = #ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [2, 16], warpsPerCTA = [4, 1], order = [1, 0]}>
// CHECK: [[col_layout:#.*]] = #ttg.blocked<{sizePerThread = [4, 1], threadsPerWarp = [16, 2], warpsPerCTA = [1, 4], order = [0, 1]}>
```

这两个 layout 是同一个 64×64 tensor 的两种“好”布局：

- `row_layout`：`order=[1,0]`，第 1 维（列）最快变化；`sizePerThread=[1,4]` 表示每个线程在列方向拿 4 个连续元素；`threadsPerWarp=[2,16]` 表示 2 行 × 16 列；`warpsPerCTA=[4,1]` 表示 4 个 warp 沿行方向铺开。它适合 row-major 的 global load/store。
- `col_layout`：`order=[0,1]`，第 0 维（行）最快变化；`sizePerThread=[4,1]` 表示每个线程在行方向拿 4 个连续元素；适合列优先访问。

`tt.trans` 会把“逻辑维度”换掉，但不会自动把物理 layout 变成另一个；所以 store 之前通常会出现 `ttg.convert_layout`，或者 `Coalesce` 会为 store 重新选一个 layout。

## 如果你卡住了

1. **不知道 encoding 字段含义**：先读 `include/triton/Dialect/TritonGPU/IR/TritonGPUAttrDefs.td` 的 `BlockedEncodingAttr`（约 L752–915），重点看 `sizePerThread`、`threadsPerWarp`、`warpsPerCTA`、`order` 的注释。
2. **算不出线程映射**：先用 1D、`sizePerThread=[1]`、`threadsPerWarp=[32]`、`warpsPerCTA=[4]` 的最简单情形；把 `thread_id = warp * 32 + lane` 写出来，再乘 `sizePerThread`。
3. **看不到 TTGIR**：`compiled.asm["ttgir"]`；如果没有，设置 `TRITON_KERNEL_DUMP=1 TRITON_DUMP_DIR=/tmp/triton-dump`，在目录里找 `*.ttgir`。
4. **convert_layout 太多**：先确认 `tl.trans` 是否必要；再试 `-tritongpu-remove-layout-conversions`，看它消掉了哪些、保留了哪些。保留的通常有语义或性能原因。
5. **`triton-opt` 不存在**：先完成 Ch 0 的构建；没有构建时，直接读 `test/TritonGPU/coalesce.mlir` 的 CHECK 行，把期望 layout 抄下来做静态分析，并标注 `guided`。

## 实作步骤建议

### Step 1：编译并 dump TTGIR

```python
import torch
import triton
import triton.language as tl


@triton.jit
def transpose_scale(x_ptr, y_ptr, M, N, BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr):
    pid_m = tl.program_id(0)
    pid_n = tl.program_id(1)
    offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    mask = (offs_m[:, None] < M) & (offs_n[None, :] < N)
    x = tl.load(x_ptr + offs_m[:, None] * N + offs_n[None, :], mask=mask, other=0.0)
    y = tl.trans(x) * 2.0
    tl.store(y_ptr + offs_n[:, None] * M + offs_m[None, :], y, mask=tl.trans(mask))


M = N = 64
BLOCK_M = BLOCK_N = 64
x = torch.randn((M, N), device="cuda", dtype=torch.float32)
y = torch.empty((N, M), device="cuda", dtype=torch.float32)
compiled = transpose_scale.warmup(
    x, y, M, N, BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N, grid=(1, 1)
)
print(compiled.asm["ttgir"])
```

### Step 2：用 `triton-tensor-layout` 打印映射

构建完成后：

```bash
triton-tensor-layout \
  -l "#ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>" \
  -t "tensor<128xf32>"
```

对 2D：

```bash
triton-tensor-layout \
  -l "#ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [2, 16], warpsPerCTA = [4, 1], order = [1, 0]}>" \
  -t "tensor<64x64xf32>"
```

如果你加了 `-use-hw-view`，输出会从 warp 的视角看；不加则是从 tensor 的视角看“每个元素属于哪个线程”。做映射表时先用默认视角。

### Step 3：用 `triton-opt` 观察 pass

`test/TritonGPU/coalesce.mlir` 的 RUN 行是：

```mlir
// RUN: triton-opt %s -split-input-file -tritongpu-coalesce | FileCheck %s
```

你可以直接跑：

```bash
triton-opt test/TritonGPU/coalesce.mlir -split-input-file -tritongpu-coalesce | less
```

再对比：

```bash
triton-opt test/TritonGPU/coalesce.mlir -split-input-file \
  -tritongpu-coalesce -tritongpu-remove-layout-conversions | less
```

观察：

- `Coalesce` 把 load/store 的 encoding 改成什么；
- 它在 load/store 前后插入了哪些 `ttg.convert_layout`；
- `RemoveLayoutConversions` 消掉了哪些，保留了哪些；
- 保留的 conversion 是否连接了“两个都是 anchor 的 layout”（例如 load 的 coalesced layout 和 dot 的 MMA layout）。

### Step 4：故意制造 layout 冲突

把 store 的索引改成列优先（或者直接把 `y = tl.trans(x)` 去掉），重新 dump TTGIR。你会看到：

- 如果 load 和 store 需要的 layout 不同，`Coalesce` 可能分别给它们选 layout；
- 两者之间的值需要 `ttg.convert_layout`；
- 如果 `convert_layout` 在循环里，可能影响流水线。

记录“改动前/改动后”的 conversion 数量。不要只看数量，还要看它们连接的是哪两个 encoding。

### Step 5：写一张“layout 决策表”

至少包含：

| 场景 | 首选 layout | 原因 | 可能的代价 |
|---|---|---|---|
| 连续 global load/store | Blocked, order 匹配内存 | coalescing | 与 dot operand 不匹配时需转换 |
| dot 的 A/B operand | DotOperand(parent=MMA) | 匹配 MMA 指令的寄存器/共享内存布局 | 从 Blocked 转换有成本 |
| shared memory staging | SwizzledShared / NVMMAShared | 避免 bank conflict，匹配 TMA/WGMMA | swizzle 参数难调 |
| reduce 的跨 warp | Slice/Blocked + shared | warp shuffle 不够时走 shared | barrier + shared 容量 |
| TMEM（SM100） | TensorMemoryEncoding | tcgen05 的 accumulator 存放 | 仅 Blackwell |

## 完整参考解答

**写完之后再看。** 下面的脚本只负责 dump 和统计，不替你解释 layout；解释部分必须自己写。

<details>
<summary>点开参考实现</summary>

```python
# labs/practice_b.py
import re
import torch
import triton
import triton.language as tl


@triton.jit
def transpose_scale(x_ptr, y_ptr, M, N, BLOCK_M: tl.constexpr, BLOCK_N: tl.constexpr):
    pid_m = tl.program_id(0)
    pid_n = tl.program_id(1)
    offs_m = pid_m * BLOCK_M + tl.arange(0, BLOCK_M)
    offs_n = pid_n * BLOCK_N + tl.arange(0, BLOCK_N)
    mask = (offs_m[:, None] < M) & (offs_n[None, :] < N)
    x = tl.load(x_ptr + offs_m[:, None] * N + offs_n[None, :], mask=mask, other=0.0)
    y = tl.trans(x) * 2.0
    tl.store(y_ptr + offs_n[:, None] * M + offs_m[None, :], y, mask=tl.trans(mask))


def compile_and_dump():
    M = N = 64
    BLOCK_M = BLOCK_N = 64
    x = torch.randn((M, N), device="cuda", dtype=torch.float32)
    y = torch.empty((N, M), device="cuda", dtype=torch.float32)
    compiled = transpose_scale.warmup(
        x, y, M, N, BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N, grid=(1, 1)
    )
    ttgir = compiled.asm["ttgir"]
    print(ttgir)

    # 统计 encoding 和 convert_layout
    encodings = sorted(set(re.findall(r"#ttg\.[a-zA-Z_]+<[^>]*>", ttgir)))
    print("\nENCODINGS:")
    for e in encodings:
        print(" ", e)
    print("\nconvert_layout count:", ttgir.count("ttg.convert_layout"))

    # 真正跑一次，确认语义
    transpose_scale[(1, 1)](x, y, M, N, BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N)
    torch.cuda.synchronize()
    ref = x.t() * 2.0
    print("max abs err:", (y - ref).abs().max().item())


if __name__ == "__main__":
    compile_and_dump()
```

**解答说明**：

- `warmup` 只编译，不启动；`ttgir` 是 `CompiledKernel.asm` 里的一个文本产物。
- `re.findall` 只是粗略统计，encoding 可能跨行；真正分析请用 `triton-opt` 或 `triton-tensor-layout`。
- `x.t()` 是 PyTorch 的转置视图；`y` 是 contiguous 的 `(N, M)`，所以 `ref = x.t() * 2.0` 的 shape 和 `y` 一致。
- 如果 `max abs err` 不为 0，先检查 mask 的 `tl.trans(mask)` 和 store 的索引，再检查 `other=0.0` 是否覆盖未命中元素。

</details>

## 测试用例

| 输入 | 预期 | 说明 |
|---|---|---|
| `M=N=64, BLOCK=64` | 数值一致，TTGIR 有 1–2 个 `convert_layout` | 最小 2D transpose |
| `M=65, N=63, BLOCK=64` | 数值一致，mask 保留 | 非整除边界 |
| `M=N=128, BLOCK=64` | grid=(2,2)，每个 block 一个 tile | 观察 CTA 间无转换 |
| `num_warps=4` vs `8` | TTIR 不变，TTGIR encoding 变 | 说明 layout 是后端选择 |
| 去掉 `tl.trans` | conversion 数量变化 | 说明 trans 的语义和 layout 代价 |

## 延伸挑战（加分）

- 用 `triton-tensor-layout -use-hw-view` 打印同一个 layout，解释“tensor 视角”和“hardware 视角”的差异。
- 给 `transpose_scale` 加一个 `tl.dot`，观察 `AccelerateMatmul` 之后 A/B operand 的 `DotOperandEncodingAttr`，以及它和 Blocked layout 之间的 conversion。
- 把 `BLOCK_M/BLOCK_N` 改成 32/128，观察 `Coalesce` 选出的 `sizePerThread` 是否变化；用 `test/TritonGPU/coalesce.mlir` 的 CHECK 行做对照。
- 手算一个 `#ttg.slice<{dim=1, parent=...}>` 的映射：为什么 slice 的 rank 比 parent 少一维？它和 `tt.reduce` 的 axis 有什么关系？

## 自我检核

- [ ] 能解释 `sizePerThread`、`threadsPerWarp`、`warpsPerCTA`、`order` 四个字段
- [ ] 能对 1D/2D blocked layout 手算至少 8 个线程的元素映射
- [ ] 能说出 `SliceEncodingAttr` 和 `DotOperandEncodingAttr` 为什么存在
- [ ] 能解释 `Coalesce` 插入的 `convert_layout` 不一定是坏事
- [ ] 能说出至少两个“layout 选错”的性能症状

→ [Ch 16 — Pass 基础设施与 NVIDIA pipeline](./16-pass-infrastructure-and-nvidia-pipeline.md)
