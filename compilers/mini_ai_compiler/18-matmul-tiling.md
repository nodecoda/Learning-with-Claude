# 第 18 章 矩阵乘法（matmul）与分块（tiling）：从三重循环到缓存友好的实现

> **目标**：理解矩阵乘法（matmul）为什么需要分块（tiling），能从循环顺序和工作集（working set）估算缓存（cache）行为；会读 `examples/autotune_matmul.py` 的 8 组实测结果，并能设计一次不把噪声当结论的 tile（分块尺寸）搜索。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；numpy 2.4.6；MSVC 19.43（`cl` 14.43）。命令在仓库根目录执行：

```powershell
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
$py = ".scratch\aic-env\Scripts\python.exe"
```

本章代码在 [`code/examples/autotune_matmul.py`](./code/examples/autotune_matmul.py)，朴素 matmul 的生成逻辑在 [`code/mini_ai/codegen_c.py`](./code/mini_ai/codegen_c.py) 的 `_emit_matmul`。上一章是 [Ch 17 循环代码生成](./17-loop-codegen.md)，下一章是 [Ch 19 向量化与编译选项](./19-vectorization-and-flags.md)。

## 为什么需要 tiling

`_emit_matmul` 生成的三重循环在语义上没有问题：

```c
for (int i = 0; i < M; ++i) {
    for (int j = 0; j < N; ++j) {
        float acc = 0.0f;
        for (int k = 0; k < K; ++k) {
            acc += A[i*K + k] * B[k*N + j];
        }
        C[i*N + j] = acc;
    }
}
```

它对每个 `(i,j)` 重新扫描一整行 `A` 和一整列 `B`。当矩阵小于缓存时，这些重复访问由缓存吸收；当矩阵大于缓存时，`B` 的列访问会不断把缓存行踢掉，CPU 大量时间花在等内存，而不是做乘加。

分块（tiling，也叫 blocking）把大矩阵切成能装进缓存的小块，让一个块内的 `A`、`B`、`C` 在短时间内被反复使用。它不减少算法层面的乘加次数，减少的是缓存未命中（cache miss）和内存流量。

## 先建立直觉

先看朴素 i-j-k 循环的访问路径：

```text
A (row-major)                 B (row-major)                 C
+------------------+          +------------------+          +------------------+
| a00 a01 a02 a03  |          | b00 b01 b02 b03  |          | c00 c01 c02 c03  |
| a10 a11 a12 a13  |          | b10 b11 b12 b13  |          | c10 c11 c12 c13  |
| a20 a21 a22 a23  |          | b20 b21 b22 b23  |          | c20 c21 c22 c23  |
| a30 a31 a32 a33  |          | b30 b31 b32 b33  |          | c30 c31 c32 c33  |
+------------------+          +------------------+          +------------------+

for i:  A 的第 i 行连续读，复用 K 次
  for j:  B 的第 j 列跨行读，步长 N
    for k:  A[i][k] * B[k][j] -> 一个标量 acc
```

tiling 把循环改成“块间 + 块内”两层：

```text
         N
     +----+----+----+----+
     | B00| B01| B02| B03|
 M   +----+----+----+----+
     | B10| B11| B12| B13|
     +----+----+----+----+
     | B20| B21| B22| B23|
     +----+----+----+----+

for ii in 0..M step TI:
  for jj in 0..N step TJ:
    for kk in 0..K step TK:
      A[ii:ii+TI, kk:kk+TK]  x  B[kk:kk+TK, jj:jj+TJ]
      -> C[ii:ii+TI, jj:jj+TJ]

一个 tile 的 working set:
  TI*TK 个 A 元素 + TK*TJ 个 B 元素 + TI*TJ 个 C 元素
```

块内的 `A` 块、`B` 块、`C` 块会在 `k` 方向被反复使用。只要它们能同时留在 L1 或 L2，乘加就能从缓存拿数据。

## 核心内容

### 1. 朴素实现：先看真实生成代码

`CCodegen._emit_matmul` 的核心是：

```python
def _emit_matmul(self, op: Op, lines: list[str]) -> None:
    a, b = op.inputs[0], op.inputs[1]
    m, k = a.type.shape
    k2, n = b.type.shape
    if k != k2:
        raise IRError(f"matmul shape mismatch: {a.type.shape} @ {b.type.shape}")
    out = op.output
    bias = op.inputs[2] if len(op.inputs) == 3 else None
    i, j, kk = f"i{op.id}", f"j{op.id}", f"k{op.id}"
    lines.append(f"    for (int {i} = 0; {i} < {m}; ++{i}) {{")
    lines.append(f"        for (int {j} = 0; {j} < {n}; ++{j}) {{")
    lines.append("            float acc = 0.0f;")
    lines.append(f"            for (int {kk} = 0; {kk} < {k}; ++{kk}) {{")
    lines.append(
        f"                acc += {self.ptr(a)}[({i})*{k} + ({kk})] * "
        f"{self.ptr(b)}[({kk})*{n} + ({j})];"
    )
    lines.append("            }")
    if bias is not None:
        bias_idx = _broadcast_index(bias.type.shape, (m, n), [i, j])
        lines.append(f"            acc += {self.ptr(bias)}[{bias_idx}];")
    if op.kind == "matmul_bias_relu":
        lines.append("            acc = fmaxf(acc, 0.0f);")
    lines.append(f"            {self.ptr(out)}[({i})*{n} + ({j})] = acc;")
    lines.append("        }")
    lines.append("    }")
```

这段代码有三个特点：

- 循环顺序是 i-j-k。`C` 的一个元素在寄存器 `acc` 里累加，`A` 按行连续读，`B` 按列跨行读。
- `bias` 和 `relu` 是 epilogue（收尾），在 `k` 循环结束后执行。融合后的 `matmul_bias_relu` 不会额外生成 elementwise 循环。
- 所有下标都是 `i*K+k`、`k*N+j`、`i*N+j` 的静态形式。C 编译器能看到完整 trip count，但是否向量化取决于访存和依赖，Ch 19 再谈。

以 demo 里的第一层 `4x8 @ 8x16` 为例，实际生成的 C 是：

```c
for (int i5 = 0; i5 < 4; ++i5) {
    for (int j5 = 0; j5 < 16; ++j5) {
        float acc = 0.0f;
        for (int k5 = 0; k5 < 8; ++k5) {
            acc += v_0_x0[(i5)*8 + (k5)] * v_3_linear_weight_t[(k5)*16 + (j5)];
        }
        acc += v_2_fc1_bias[j5];
        acc = fmaxf(acc, 0.0f);
        v_12_relu_fused[(i5)*16 + (j5)] = acc;
    }
}
```

矩阵小，`B` 只有 512 字节，整列访问都在 L1 里，tiling 的收益有限。tiling 真正开始有意义，是矩阵大到单个矩阵都放不进 L1/L2 的时候。

### 2. 算术强度：先算清“每个字节做多少次乘加”

对 `M x K` 乘 `K x N`：

```text
乘加次数 = M * N * K
浮点运算数 = 2 * M * N * K        （一次乘 + 一次加）
最少内存字节 = 4 * (M*K + K*N + M*N)   （float32，各读/写一次）
算术强度 = 2*M*N*K / (4*(M*K + K*N + M*N))
```

对 `64x64x64`：

```text
flops = 2 * 64^3 = 524288
bytes = 4 * 3 * 64^2 = 49152
intensity = 524288 / 49152 = 10.67 flop/byte
```

对 `512x512x512`，强度约 85 flop/byte。现代 CPU 的浮点峰值远高于每字节可用的带宽，算术强度越低，越可能成为内存瓶颈。tiling 的目标不是改变这个比值，而是让重复访问命中缓存，把“最少内存字节”尽量变成实际内存字节。

### 3. 缓存层级与 working set

下面是典型量级，不是本机实测：

| 层级 | 典型容量 | 典型延迟 | 对 matmul 的意义 |
| --- | --- | --- | --- |
| 寄存器 | 几十到几百个 | 1 cycle | 累加器、少量 A/B 元素 |
| L1 | 32-48 KB | 4-5 cycles | 一个 tile 的 A/B/C 最好都放这里 |
| L2 | 512 KB-2 MB | 10-20 cycles | 放不下 L1 时的下一层 |
| L3 | 8-32 MB | 30-70 cycles | 多核共享，容量大但延迟高 |
| DRAM | GB 级 | 100-300 cycles | 尽量只碰一次 |

一个 tile 的 working set 是：

```text
TI*TK*4 字节（A） + TK*TJ*4 字节（B） + TI*TJ*4 字节（C）
```

对 `TI=TJ=TK=16`：`3 * 256 * 4 = 3072` 字节，能放进 L1。对 `32`：`3 * 1024 * 4 = 12288` 字节，仍然能放进 32 KB L1，但同时还要放其他数据。对 `64`：`3 * 4096 * 4 = 49152` 字节，超过常见 32 KB L1，tile 本身就会溢出到 L2。

这解释了一个常见现象：tile 不是越大越好。大 tile 减少了块间循环次数，但 working set 超过 L1 后，块内复用会退化；小 tile 能留在 L1，但块间循环和边界检查的开销上升。

### 4. autotune 脚本：8 组 tile 的真实搜索

`examples/autotune_matmul.py` 生成一个 `64x64x64` 的 tiled matmul，对 8 组 `(TI,TJ,TK)` 分别编译 DLL、跑正确性检查、再用 `benchmark` 取中位数。核心生成器是：

```python
def kernel(ti: int, tj: int, tk: int) -> str:
    return f'''#include <stddef.h>
#if defined(_MSC_VER)
#define MINI_EXPORT __declspec(dllexport)
#else
#define MINI_EXPORT
#endif
MINI_EXPORT int mini_forward(const float* A, const float* B, float* C) {{
    for (int i = 0; i < 64; ++i) for (int j = 0; j < 64; ++j) C[i*64+j] = 0.0f;
    for (int ii = 0; ii < 64; ii += {ti}) {{
        for (int jj = 0; jj < 64; jj += {tj}) {{
            for (int kk = 0; kk < 64; kk += {tk}) {{
                int i_end = ii + {ti} < 64 ? ii + {ti} : 64;
                int j_end = jj + {tj} < 64 ? jj + {tj} : 64;
                int k_end = kk + {tk} < 64 ? kk + {tk} : 64;
                for (int i = ii; i < i_end; ++i) {{
                    for (int j = jj; j < j_end; ++j) {{
                        float acc = C[i*64+j];
                        for (int k = kk; k < k_end; ++k) acc += A[i*64+k] * B[k*64+j];
                        C[i*64+j] = acc;
                    }}
                }}
            }}
        }}
    }}
    return 0;
}}
'''
```

这段代码有几个必须理解的点：

- `C` 先整体清零。tiled 版本在 `kk` 循环里读 `C[i*64+j]` 再累加，不清零会把未初始化值加进去。
- 三个 `_end` 用 `min(ii+TI, 64)` 做边界保护。当前 8 组 tile 都能整除 64，所以保护没有触发；换成 `48` 或 `20` 就会触发，不能删。
- `acc` 在 `k` 循环外从 `C` 读入，在 `k` 循环内累加，循环结束后写回。这保证跨 `kk` 块的累加是连续的。
- 编译发生在计时之外。`benchmark` 只包住 `fn([a,b], ...)`，不包含 `cl` 的编译时间。

脚本对每组配置执行：

```python
stats = benchmark(lambda: fn([a, b], [(64, 64)]), warmup=3, repeat=20)
```

课程记录的一次完整输出如下（同一台 Windows x86_64 机器，MSVC 14.43，`64x64x64`，float32）：

| tile `(TI,TJ,TK)` | median ms | 备注 |
| --- | ---: | --- |
| (64,64,64) | 0.4267 | 单个 tile 覆盖全矩阵，working set 约 48 KB |
| (32,32,32) | 0.2677 | working set 约 12 KB |
| **(16,16,16)** | **0.2132** | **本次记录最佳** |
| (32,64,32) | 0.3561 | B/C 块偏大 |
| (64,32,32) | 0.2826 | A/C 块偏大 |
| (16,32,16) | 0.2289 | 接近最佳 |
| (32,16,32) | 0.3157 | 块形状不均衡 |
| (8,8,8) | 0.2251 | working set 小，但块间开销上升 |

课程记录的首行给出 `maxerr=5.722e-06`；写章复跑时，8 组的最大绝对误差都是这个值（相对于 float32 的 `a @ b`）。这个误差来自 float32 累加顺序和 BLAS/NumPy 参考实现的差异，不是 tile 写错。`5.722e-06` 小于 `assert_close` 默认的 `1e-5`，但已经接近边界；如果换成更长的 `K`，误差会继续累积，容差要重新评估。

### 5. 复跑记录：同一脚本，不同的排名

写本章时在同一台机器上再次运行 `python examples/autotune_matmul.py`，得到：

```text
tile=(64,64,64) median_ms=0.5224 maxerr=5.722e-06
tile=(32,32,32) median_ms=0.5175 maxerr=5.722e-06
tile=(16,16,16) median_ms=0.5027 maxerr=5.722e-06
tile=(32,64,32) median_ms=0.4548 maxerr=5.722e-06
tile=(64,32,32) median_ms=0.4759 maxerr=5.722e-06
tile=(16,32,16) median_ms=0.4936 maxerr=5.722e-06
tile=(32,16,32) median_ms=0.4723 maxerr=5.722e-06
tile=( 8, 8, 8) median_ms=0.5773 maxerr=5.722e-06
best: (32, 64, 32) median_ms= 0.4548
```

两次运行的最佳 tile 不同，所有数字也整体抬高了。这并不说明第一次记录是假的，也不说明第二次脚本坏了；它说明单次、单机、20 次重复的 tile 搜索会受到后台负载、频率调节和内存状态影响。课程记录的价值是给出一个可复现的基准，不是宣布 `16/16/16` 在所有机器上都最优。

写性能结论时至少要做到：

- 固定 CPU、编译器版本、编译选项和 shape。
- 固定 warmup/repeat，报告 median，不拿单次最小值当结论。
- 同一配置重复多轮，观察排名是否稳定。
- 差异小于 1.5 倍时，不宣称一方更快；这是本课 README 的约定。

### 6. 边界：tile 不整除与 tile=0

当前脚本的 `i_end`、`j_end`、`k_end` 已经处理了不整除的情况。把 `ti` 改成 `20`，最后一个 tile 的 `i_end` 会是 64，循环仍然只访问合法范围。这个保护是 tiled matmul 必须有的，不能因为“当前 64 能被 16 整除”就删掉。

实际用 `(20,20,20)` 跑同一段生成器（只把配置换成 20，其余不变），得到：

```text
tile=(20,20,20) maxerr= 5.7220458984375e-06
median_ms= 0.2666
```

`64` 不是 `20` 的倍数，最后一个 tile 仍然被 `i_end/j_end/k_end` 截到 64，结果正确。这个实验验证了边界保护的必要性，也说明 tile 不必整除矩阵维度。

`tile=0` 没有保护：`ii += 0` 会让外层循环永远不前进。脚本只接受正整数配置，但生成器本身没有校验。做自动搜索时，参数空间要先过滤掉 0 和负数。

另一个边界是 tile 大于矩阵维度。例如 `TI=128` 对 `M=64`：第一次 `ii=0`，`i_end=64`，内层只跑 64 行，循环结束后 `ii=128`，外层退出。结果正确，但 `TI` 的实际有效值是 64，搜索空间里再放更大的值只会重复编译同一份有效代码。

## 底层机制

### 循环顺序为什么影响缓存

以 row-major 的 `B` 为例：

```text
i-j-k:  for i, for j, for k:  B[k*N + j]
        j 固定，k 变化 -> 地址每次加 N*4 字节，跨行跳
i-k-j:  for i, for k, for j:  B[k*N + j]
        k 固定，j 变化 -> 地址连续，缓存行利用率高
```

`i-j-k` 把 `C[i][j]` 放在寄存器里，`A` 行连续，`B` 列跨行。`i-k-j` 把 `B` 行连续读，但 `C[i][j]` 要在 `k` 循环里反复读写内存，除非编译器把它提升到寄存器。两种顺序各有代价，选择取决于矩阵大小、编译器能力和是否手写寄存器分块。

| 循环顺序 | A 访问 | B 访问 | C 访问 | 特点 |
| --- | --- | --- | --- | --- |
| i-j-k | 行连续 | 列跨行 | 标量累加 | 当前生成器；B 列不友好 |
| i-k-j | 标量复用 | 行连续 | 行反复读写 | B 友好，C 需要累加器 |
| j-i-k | 列跨行 | 标量复用 | 列跨行 | 对 row-major 不友好 |
| 分块 i-j-k | 块内连续 | 块内跨行 | 块内连续 | 当前 autotune；块小到能进缓存 |

tiling 没有改变 i-j-k 的块内顺序，它改变的是块的大小和块间顺序。块内 `B` 仍然按列访问，但块只有 `TK x TJ`，整个块能留在 L1，跨行访问的代价被缓存吸收。

### 为什么 16 常常是好起点

对 `64x64x64`，`16` 的 working set 是 3 KB，远小于 32 KB L1；块间循环次数是 `4*4*4=64`。`32` 的 working set 是 12 KB，块间循环次数是 8；`64` 的 working set 是 48 KB，块间循环只有 1 次，但 tile 本身溢出 L1。

这是一组典型的权衡：

- tile 太小：块间循环多，边界检查和索引计算占比上升。
- tile 太大：working set 超过缓存，块内复用失效，退化成近似朴素循环。
- tile 形状不均衡：某个矩阵块过大，仍可能溢出；`(32,16,32)` 的 0.3157 就是一个例子。

`16` 不是魔法数字。它只是在 `64x64x64`、float32、这台机器和这版编译器下，working set 和循环开销的一个平衡点。换矩阵大小、换数据类型、换 CPU，最佳值会变。

### bias 与 relu 的位置

`matmul_bias` 和 `matmul_bias_relu` 把 bias 和 relu 放在 `k` 循环之后：

```c
acc += bias[j];
acc = fmaxf(acc, 0.0f);
C[i*N+j] = acc;
```

这个位置不能提前到 `k` 循环里。bias 只加一次，放进 `k` 循环会加 `K` 次；relu 必须在完整累加后做，提前做会改变负数部分的和。融合 op 的 epilogue 顺序是 `matmul -> bias -> relu`，和 IR 的语义顺序一致。

### 内存布局与 packing

当前实现直接读原始 `A`、`B`，没有 packing（把 tile 拷贝成连续的小块）。packing 能让块内访问完全连续，也能让 SIMD 加载更整齐，代价是额外的拷贝和缓冲区。BLAS 库通常会用 packing + 寄存器分块 + 多线程，把 matmul 推到接近峰值；本课的实现停在“能解释 tiling 的效果”这一层。

## 进一步用法与变体

### 改变循环顺序

把脚本的内层改成 i-k-j。下面只是循环结构的示意，没有加入配套脚本；改完要用同一套差分测试和 benchmark 验证：

```c
for (int i = ii; i < i_end; ++i) {
    for (int k = kk; k < k_end; ++k) {
        float a = A[i*64+k];
        for (int j = jj; j < j_end; ++j) {
            C[i*64+j] += a * B[k*64+j];
        }
    }
}
```

这段代码的 `B` 行连续，`C` 行也连续，但 `C` 在 `k` 循环里被反复读写。对 `64x64`，`C` 行只有 256 字节，能留在 L1；对更大的 `N`，需要把 `C` 的一行分块或放进寄存器。改完要用同一套差分测试和 benchmark，不能只看汇编。

### 寄存器分块

在 tile 内部再固定几个 `i`、`j`，例如一次算 `4x4` 个 `C` 元素，把 16 个累加器放在寄存器里。这样 `A` 的每个元素可以被 4 个 `j` 复用，`B` 的每个元素可以被 4 个 `i` 复用。寄存器分块通常和 SIMD 一起用，属于 Ch 19 之后的优化。

### 多线程

把 `ii` 或 `jj` 循环分配给多个线程，每个线程写不同的 `C` 块。当前生成器没有线程库依赖，arena 也不是线程共享的；要做多线程，先确定每个线程的输出分区和只读输入，再处理线程创建开销。对小矩阵，线程开销会超过收益。

### 用 BLAS 作为上限

NumPy 的 `a @ b` 通常调用 BLAS。把 BLAS 的时间作为参考上限，可以判断自己的 tiled 版本还有多少空间。注意 BLAS 可能使用多线程和更宽的 SIMD，比较时要固定线程数和编译选项，否则不是同一场比赛。

## 对比与取舍

| 方案 | 优点 | 代价 | 适用场景 |
| --- | --- | --- | --- |
| 朴素 i-j-k | 代码短，语义直接 | 大矩阵缓存不友好 | 教学、极小矩阵 |
| tiled i-j-k | 块内复用，改动局部 | 需要边界保护，tile 要调 | 本课 autotune |
| i-k-j | B 行连续 | C 反复读写 | B 很大、C 能留缓存 |
| packing + 寄存器分块 | 接近 BLAS，SIMD 友好 | 实现复杂，额外内存 | 生产级 kernel |
| 直接调 BLAS | 性能最好，维护少 | 依赖外部库，难改语义 | 不想自己写 kernel |

本课选择 tiled i-j-k，因为它在现有 `_emit_matmul` 上只增加三层循环，读者能看清“块间/块内”的区别。它不适合直接拿去和生产 BLAS 比绝对值，但足够解释 tiling 的原理和测量方法。

## 踩雷集锦

1. **tiled 版本忘记清零 C。** `acc = C[i*64+j]` 读的是上一次累加的结果；如果 C 未初始化，结果会随机。当前脚本在最早的循环里整体清零，这个顺序不能省。
2. **删除 tile 边界保护。** 只要 tile 不整除矩阵维度，`i_end` 就会越界。`min(ii+TI, M)` 是正确性的一部分，不是优化。
3. **把编译时间算进 benchmark。** `cl` 编译一次几百毫秒到几秒，和 kernel 的微秒级执行不是一个量级。编译必须在计时循环外。
4. **用单次最小值宣布加速。** 最小值和 median 可能差很多；当前 `benchmark` 同时返回 median/min/max，就是让你看到噪声。小于 1.5 倍的差异不要下结论。
5. **只看 tile 大小，不看 tile 形状。** `(32,16,32)` 和 `(16,32,16)` 的 working set 相近，实测却差出一截；块形状影响访问顺序和边界，不能只比元素个数。

## 进阶

- 把 `autotune_matmul.py` 扩展到 `128x128x128` 和 `256x256x256`，观察最佳 tile 是否随矩阵变大而移动。
- 在脚本里加入 i-k-j 变体，对同一组 tile 比较两种循环顺序。
- 用 `perf` 或 Windows 的性能计数器统计 L1/L2 miss，验证 working set 的估算。
- 实现 4x4 寄存器分块，和 tiled 版本做差分测试与 benchmark。
- 把 tile 搜索从固定 8 组改成随机采样，并记录每组的编译时间、执行时间和最大误差。

## 动手练习

1. 运行 `python examples/autotune_matmul.py`，把输出保存到文件；间隔几分钟再跑一次，比较两次的 median 和最佳 tile。
2. 把 `ti` 改成 `20`，确认边界保护仍然正确；再把 `ti` 改成 `0`，观察脚本的行为，并补一个参数校验。
3. 手算 `(TI,TJ,TK)=(16,16,16)` 的 working set 字节数，和 `(64,64,64)` 对比，解释为什么后者可能溢出 L1。
4. 把内层循环改成 i-k-j，保持 tile 不变，跑同一套测试，记录 maxerr 和 median。
5. 选一个 tile 配置，手工把 `C` 的清零删掉，观察错误如何出现；恢复后再跑一次，确认结果回到 `5.722e-06` 附近。

## 本章重点

- matmul 的朴素三重循环在矩阵大于缓存时受内存流量限制；tiling 通过缩小 working set 提高块内复用。
- 当前生成器使用 i-j-k；tiled 版本保留块内顺序，只增加块间循环和边界保护。
- `64x64x64` 的课程记录中，`(16,16,16)` 的 median 是 `0.2132 ms`，8 组最大误差都是 `5.722e-06`。
- 同一脚本复跑可能得到不同排名；tile 结论必须绑定机器、编译器、shape 和测量口径。
- tile 太小增加循环开销，太大溢出缓存；`16` 是这台机器和这个 shape 下的平衡点，不是通用常数。

## 自我检核

- [ ] 我能写出 `M x K` 乘 `K x N` 的乘加次数和算术强度公式。
- [ ] 我能解释 i-j-k 中 `B` 为什么按列跨行访问。
- [ ] 我能算出 `(16,16,16)` 和 `(64,64,64)` 的 working set，并判断哪个更可能溢出 L1。
- [ ] 我能说明 tiled 版本为什么必须先清零 `C`，以及 `i_end` 保护为什么不能删。
- [ ] 我能复述课程记录的 8 组结果，并解释复跑排名变化说明什么。
- [ ] 我知道小于 1.5 倍的差异不能作为性能结论。

## 延伸阅读

- **`code/examples/autotune_matmul.py`**：读哪里——`kernel` 生成器和 `main` 的 8 组配置；学什么——tile 参数如何进入 C 代码、如何编译和计时；关联——本章所有实测数字。
- **`code/mini_ai/codegen_c.py` 的 `_emit_matmul`**：读哪里——i-j-k 循环和 bias/relu epilogue；学什么——朴素 matmul 的生成方式；关联——tiling 的改造起点。
- **`code/mini_ai/runtime.py` 的 `benchmark`**：读哪里——warmup、repeat、median/min/max；学什么——为什么中位数是主指标；关联——Ch 20 的 benchmark 方法。
- **BLAS 参考实现（OpenBLAS 或 Intel MKL 文档）**：读哪里——GEMM 的 packing、register blocking、多线程章节；学什么——生产级 matmul 的分层结构；关联——本章“进一步用法”的上限。
- **《Computer Architecture: A Quantitative Approach》缓存章节**：读哪里——cache miss 分类、working set、循环分块；学什么——从访问模式估算缓存行为；关联——tiling 的理论依据。

下一章：[Ch 19 向量化与编译选项](./19-vectorization-and-flags.md)