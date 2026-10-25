# Ch 15 向量化与 SIMD：从 `T.vectorized` 到 `ymm` 指令

> **目标**：搞清 TVM 的 `vectorize` 到底做了什么——它在 TIR 里声明了什么、在 LLVM IR 里变成什么、在 x86 上落成哪条指令；并用实测说明向量化何时有效（算术强度）、何时失效（访存受限、gather）、以及为什么长度不能被宽度整除时性能会掉一截。

## 为什么需要这个？

标量 CPU 一个时钟周期大体只能推进一条 `addss`；但一条 AVX2 的 `vfmadd` 一次就处理 8 个 float。对 `C = A * 2 + B` 这种 kernel，这意味着理论上的 8 倍差距。现实里拿不到 8 倍，是因为内存带宽、对齐、尾部、寄存器压力会轮番把收益吃回去。**能不能判断"这次的向量化到底值不值"，就是本章要练的手艺。**

更麻烦的是，`vectorize` 是 AI 编译器里最容易被误解的原语：

- 它写在 **TIR** 层，表达的是"这 W 次迭代可以打包成一条向量指令"；
- 它**不保证**最终生成 W 宽的指令——生成什么由 target 特性和 LLVM 的后端决策决定；
- 它**不保证**更快——访存受限的 kernel 向量化前后一模一样，gather 场景甚至更慢。

本章所有数字都来自本机实测（Intel i7-10510U，4 核 8 线程，AVX2、无 AVX-512，Windows 11，TVM 0.27.0.post1）。这台机器是共享的，负载会波动，所以同一份代码在不同时段能差 2–3 倍；表格里的数字取多轮 `min`/中位数，**看趋势，不要记绝对值**。

## 先建立直觉：一条指令干 8 份活

SIMD（Single Instruction Multiple Data）就是把多个数塞进一个宽寄存器，一条指令同时算：

```text
标量：  load a[0]  load b[0]  fma -> c[0]     （重复 8 次）
AVX2：  load <8 x float>(a)  load <8 x float>(b)  vfmadd -> <8 x float>  store
```

寄存器宽度决定了"一份活"有多宽：

| 指令集 | 寄存器 | float 通道数 | 本机支持 |
|---|---|---|---|
| SSE / SSE2 | `xmm`（128 位） | 4 | 是（默认基线） |
| AVX / AVX2 | `ymm`（256 位） | 8 | 是 |
| AVX-512 | `zmm`（512 位） | 16 | 否 |

向量化要成立，有三个前提，缺一个就会退化：

1. **连续（contiguous）**：相邻迭代访问相邻地址，否则只能用 gather/scatter，甚至退回标量。
2. **对齐（aligned）**：很多指令（`vmovaps`、`movaps`）要求地址按 16/32 字节对齐；不对齐要么换 `vmovups`，要么直接崩。
3. **长度可被宽度整除**：不能整除就要处理"尾巴"——标量尾循环、掩码，或者补齐。

## 核心概念一：宽度是"声明"，不是"事实"

先看 TVM 这一侧。一个 1024 元素的 elementwise kernel，按 8 切分后把内层标记为 `vectorize`：

```python
import tvm
from tvm import te

n = 1024
A = te.placeholder((n,), name="A")
B = te.placeholder((n,), name="B")
C = te.compute((n,), lambda i: A[i] * 2.0 + B[i], name="C")

sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))
(i,) = sch.get_loops(sch.get_sblock("C"))
io, ii = sch.split(i, factors=[None, 8])
sch.reorder(io, ii)
sch.vectorize(ii)
print(sch.mod.script())
```

实测 TIR（节选）：

```text
for i_0 in range(128):
    for i_1 in T.vectorized(8):
        with T.sblock("C"):
            v_i = T.axis.spatial(1024, i_0 * 8 + i_1)
            C[v_i] = A[v_i] * T.float32(2.0) + B[v_i]
```

`T.vectorized(8)` 就是那句"声明"。往下走一层，`tvm.build(..., target="llvm")` 生成的 LLVM IR 里出现了向量类型：

```text
%2 = load <8 x float>, ptr %1, align 64, !dbg !47, !tbaa !52
%4 = load <8 x float>, ptr %3, align 64, !dbg !47, !tbaa !55
%5 = tail call <8 x float> @llvm.fmuladd.v8f32(<8 x float> %2, <8 x float> splat (float 2.000000e+00), <8 x float> %4), !dbg !47
store <8 x float> %5, ptr %6, align 64, !dbg !47, !tbaa !57
```

到这里看起来一切正常：8 宽、一条 FMA。**但 `<8 x float>` 只是 IR 层的类型，不是最终指令。** 本机默认 `target="llvm"` 的 CPU 基线是 SSE2，于是同一段 IR 落成的是 `xmm`（4 宽）指令：

```text
movaps	(%rcx,%rax), %xmm0
movaps	16(%rcx,%rax), %xmm1
addps	%xmm1, %xmm1
addps	16(%rdx,%rax), %xmm1
addps	%xmm0, %xmm0
addps	(%rdx,%rax), %xmm0
movaps	%xmm0, (%r8,%rax)
movaps	%xmm1, 16(%r8,%rax)
```

一条 `<8 x float>` 被拆成两段 16 字节的 `movaps/addps`。想看真正的 `ymm`，必须告诉 target 你用的是什么 CPU：

```python
avx2 = tvm.target.Target({"kind": "llvm", "mattr": ["+avx2", "+fma"]})
f = tvm.build(sch.mod, target=avx2)
print(str(f.inspect_source("asm")).count("ymm"))   # > 0
```

实测对比：

```text
默认 target="llvm"      : ymm 出现 0 次，xmm 出现 39 次，vfmadd 0 次
mattr=["+avx2","+fma"]  : ymm 出现 12 次，xmm 出现 16 次，vfmadd 2 次
```

顺带一个 0.27 的 API 变化：**target 的 CLI 字符串形式已经不支持了**。写 `"llvm -mcpu=native"` 会直接报错：

```text
Cannot parse target string "llvm -mcpu=native". CLI target string form
(e.g. "llvm -mcpu=xxx") is no longer supported. Please use JSON dict form
(e.g. {"kind": "llvm", "mcpu": "xxx"}) instead.
```

而 `{"kind": "llvm", "mcpu": "native"}` 在 Windows 上也不灵——TVM 会打印：

```text
Using LLVM 22.1.0 with `-mcpu=native` is not valid in `-mtriple=x86_64-pc-windows-msvc`,
using default `-mcpu=generic`
```

所以本机想拿到 AVX2，只能显式写 `mattr`。这三条加起来就是本章最重要的一句话：**向量宽度是 IR 的声明，实际宽度由 target 特性决定。**

## 核心概念二：对齐

`movaps` 的 `a` 是 aligned 的意思：地址必须是 16 字节的倍数，否则触发 GP fault。`vmovaps` 对 32 字节同理。所以向量化代码里有一条隐性契约：**数据起始地址按寄存器宽度对齐**。

TVM 在 IR 里直接把这条契约写成假设：

```text
%2 = load <8 x float>, ptr %1, align 64
```

这个 `align 64` 是编译器给 LLVM 的承诺，不是运行时检查。TVM 生成的 wrapper 只检查 `num_args`、`type_index`、shape、stride、`byte_offset == 0`、`dev_id`，**不检查地址对齐**：

```text
%69 = getelementptr inbounds nuw i8, ptr %A.handle, i64 40   ; DLTensor.byte_offset
%70 = load i64, ptr %69, align 8
%71 = icmp eq i64 %70, 0
br i1 %71, label %assert_end43, label %assert_fail42
```

好消息是 `tvm.runtime.tensor(...)` 分配的内存天然按 64 字节对齐，所以从 Python 侧调用基本踩不到这个坑：

```python
a = np.random.rand(1024).astype("float32")
ta = tvm.runtime.tensor(a, tvm.cpu())     # 复制进 TVM 自己的对齐缓冲区
print(ta.data_ptr() % 64)                 # 实测 0
```

实测三次分配（n=10、1024、8192）的地址都满足 `data_ptr() % 64 == 0`——这就是 IR 里敢写 `align 64` 的底气。

坏消息是这也意味着**你没法用 NumPy 的切片去构造"非对齐输入"**：`tvm.runtime.tensor` 会复制，切片的偏移在复制时被抹平；0.27 的 `Tensor` 也不支持 `__getitem__`（`TypeError: 'Tensor' object is not subscriptable`）。要真正测非对齐，得构造带 `byte_offset` 的 DLTensor 或从 C++ 侧传裸指针——本章不做，标记为 `guided`。

工程上的三种应对：

1. **保证对齐**：让所有 buffer 从 64 字节边界开始（TVM 默认就是这么做的）。
2. **补齐（pad）**：把长度补到宽度的整数倍，多算几个无用元素。
3. **用非对齐指令**：即 `vmovups`，x86 上代价很小（跨 cache line 才有明显损失），但不是所有平台都便宜。

## 核心概念三：tail 与 mask

长度不能被宽度整除时，TVM 的 `split` 不会报错，它会生成"向上取整的外层 + 谓词"：

```python
n, W = 1000, 16
A = te.placeholder((n,), name="A")
B = te.placeholder((n,), name="B")
C = te.compute((n,), lambda i: A[i] * 2.0 + B[i], name="C")

sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))
(i,) = sch.get_loops(sch.get_sblock("C"))
io, ii = sch.split(i, factors=[None, W])
sch.reorder(io, ii)
sch.vectorize(ii)
print(sch.mod.script())
```

实测 TIR（节选）：

```text
for i_0 in range(63):                 # ceil(1000 / 16)
    for i_1 in T.vectorized(16):
        with T.sblock("C"):
            v_i = T.axis.spatial(1000, i_0 * 16 + i_1)
            T.where(i_0 * 16 + i_1 < 1000)
            C[v_i] = A[v_i] * T.float32(2.0) + B[v_i]
```

`T.where(...)` 是卫语句（predicate）。它保证了正确性（实测 `maxerr = 0.0`），但代价很实在：**LLVM 面对带谓词的向量循环时，会放弃 16 宽，退回到更窄的宽度组合。**

把 n=8192（整除）和 n=8190（不整除）放在一起看，差异在 IR 里一眼可见：

```text
n=8192, factor=16: IR 中的向量类型 = {16}
n=8190, factor=16: IR 中的向量类型 = {4, 2}    ← 没有 <16 x float>
```

实测耗时（n≈8192，factor=16，多轮中位数）：

```text
n=8192（无谓词）: 3.23 us   0.398 ns/elem
n=8190（带谓词）: 4.35 us   0.551 ns/elem     ← 每元素慢 38%（另一次重跑 +22%）
```

注意这里 n 只差 0.02%，性能却差 20–40%——**不是数据量的问题，是代码生成路径变了**。这就是为什么高性能 kernel 宁可"多算几个"（pad 到 8192），也不愿让谓词进入热循环。

## 核心概念四：`vectorize` 与 `unroll` 不是一回事

两个原语在 TIR 层的语义完全不同：

| | `sch.vectorize(loop)` | `sch.unroll(loop)` |
|---|---|---|
| TIR 形态 | `for i in T.vectorized(8)` | `for i in T.unroll(8)` |
| 语义 | 8 次迭代打包成一条向量操作 | 循环体复制 8 份 |
| 对访存的要求 | 必须能打包（连续、可映射到向量类型） | 无要求 |
| 典型收益 | 减少指令条数 | 减少循环开销、暴露指令级并行 |
| 典型风险 | 尾部/对齐/gather 导致退化 | 代码膨胀、i-cache 压力、寄存器压力 |

同一份 kernel 分别 `unroll(8)` 和 `vectorize(8)`，TIR 只差一个关键字：

```text
for i_0 in range(128):          # unroll
    for i_1 in T.unroll(8):     # vectorize 时这里是 T.vectorized(8)
        C[v_i] = A[v_i] * T.float32(2.0) + B[v_i]
```

但**在本机实测，两者打平**（chain=16，factor=8，generic target）：

```text
vectorize8: 37.78 us
unroll8   : 36.36 us
```

原因是 LLVM 的 SLP 向量化会把展开后的 8 份标量语句重新合并成向量操作——你在 TIR 层做的区分，到机器码层可能已经消失。**结论：不要假设"我写了 vectorize 就一定生成向量指令"，也不要假设"我写了 unroll 就一定还是标量"；看 IR、看 asm、看时间。**

## 底层机制：从 `T.vectorized` 到 `vfmadd`

完整链路是这样的：

```text
T.vectorized(8)
   │  LowerTIR：循环改写为 Ramp(base, stride, 8) 访问
   ▼
TIR 表达式：C[T.Ramp(i*8, 1, 8)] = A[Ramp(...)] * 2 + B[Ramp(...)]
   │  CodeGenLLVM：Ramp → <8 x float>，Broadcast → splat
   ▼
LLVM IR：load <8 x float> / llvm.fmuladd.v8f32 / store <8 x float>
   │  SelectionDAG + 指令选择（受 target 的 mattr 约束）
   ▼
x86：vmovups + vfmadd213ps（AVX2）  或  movaps + addps ×2（SSE2）
```

几个可以对照源码确认的点：

- `vectorize` 的前置条件写在 `tvm/s_tir/schedule/schedule.py:1188` 的 docstring 里：循环下的 block 必须是 complete block 或 reduction block，且绑定必须是仿射的。
- `unroll` 的实现入口在同文件 `schedule.py:1306`，docstring 明确写 "It requires nothing"——任何循环都能展开。
- `llvm.fmuladd` 是 LLVM 的融合乘加 intrinsic；它能不能落成一条 `vfmadd`，取决于 target 是否开了 FMA。

## 示例一：一个 elementwise kernel 的完整观测

把上面的片段拼起来，一次跑完 TIR、IR、asm：

```python
import tvm
from tvm import te

n = 1024
A = te.placeholder((n,), name="A")
B = te.placeholder((n,), name="B")
C = te.compute((n,), lambda i: A[i] * 2.0 + B[i], name="C")

sch = tvm.s_tir.Schedule(te.create_prim_func([A, B, C]))
(i,) = sch.get_loops(sch.get_sblock("C"))
io, ii = sch.split(i, factors=[None, 8])
sch.reorder(io, ii)
sch.vectorize(ii)

f = tvm.build(sch.mod, target="llvm")
print(str(f.inspect_source()).count("<8 x float>"))   # IR 里的向量类型
print(str(f.inspect_source("asm")).count("xmm"))      # 真实指令用的是 xmm
```

实测输出：

```text
18
39
```

18 处 `<8 x float>`，落到机器码却是 `xmm`——中间那一步 legalization 把 256 位的向量拆成了两个 128 位操作。**这就是"声明"和"事实"的差别。**

## 示例二：算术强度决定向量化收益

固定 n=8192，横轴取"每个元素做多少次链式 FMA"（chain=0 是 `A*2+B`，chain=16 是再叠 16 次 `acc*1.0001+B[i]`），纵轴取耗时。表格单位是微秒，多轮中位数：

| target | chain | scalar | vec4 | vec8 | vec16 | vec32 |
|---|---|---|---|---|---|---|
| generic(SSE2) | 0 | 3.98 | 4.28 | 4.44 | 3.92 | 3.94 |
| generic(SSE2) | 16 | 58.70 | 54.70 | 46.79 | **33.44** | 52.80 |
| +avx2+fma | 0 | 2.40 | 4.01 | 2.40 | 2.93 | 2.45 |
| +avx2+fma | 16 | 12.96 | 23.94 | 12.78 | 11.57 | **8.23** |

读这张表要注意四件事：

1. **chain=0 时怎么改都没用。** 每元素只做 1 次 FMA，却要读写 12 字节；瓶颈是访存端口和 L2 带宽，不是 ALU。向量化减少的是指令数，不是内存流量。
2. **chain=16 时差距出现。** 算术强度上去了，ALU 成为瓶颈，宽度才有意义：generic 下 58.7 → 33.4（1.75×）。
3. **开 AVX2+FMA 是最大的一档收益。** 同样是 vec8，generic 46.8 us，AVX2 12.8 us（3.7×）——因为一条 `vfmadd` 顶掉"mul + add + 两次 128 位拆分"。
4. **宽度不是越大越好。** AVX2 下 vec4（23.94）反而比标量（12.96）慢，vec32（8.23）又比 vec16 快。一个合理解释是：4 宽配 AVX2 只用了一半通道、循环次数却更多；而 32 = 4×`ymm`，给了 LLVM 更多可交错的独立链。这只是一种解释——**具体拐点必须在你的机器上测，不能抄。**

## 示例三：gather 让向量化变成负优化

把 `A[i]` 改成 `A[i*2]`（跨步读，stride=2），其他不变：

```python
A = te.placeholder((2 * n,), name="A")
C = te.compute((n,), lambda i: A[i * 2] + B[i], name="C")
# split + reorder + vectorize 同前
```

TVM 不会报错，它生成的是"手工 gather"——8 次标量 load，再用 8 条 `insertelement` 拼成一个 `<8 x float>`：

```text
%19 = insertelement <8 x float> poison, float %4, i64 0
%20 = insertelement <8 x float> %19, float %6, i64 1
...
%27 = fadd <8 x float> %2, %26
```

实测（n=65536）：

```text
stride=2 scalar: 54.43 us   0.831 ns/elem
stride=2 vec8  : 88.26 us   1.347 ns/elem    ← 慢 62%（另一次 +46%）
```

原因很直白：向量指令省下的 7 条加法，被 8 条标量 load + 8 条 `insertelement` 全吃回去了，还多了寄存器搬运。**跨步访问是向量化的天敌**；正确做法是先改 layout（Ch 8）或先做一次 transpose 到连续，再向量化。

## 对比与取舍

| 场景 | 建议 | 理由 |
|---|---|---|
| 连续、长度整齐的 elementwise | 开 target 特性 + 适度向量化 | 收益最大，风险最低 |
| 长度不整除 | 先 pad 到整数倍，再向量化 | 谓词会让 LLVM 收窄宽度（实测 +22–38%） |
| 跨步/gather | 先改 layout，别硬向量化 | 实测慢 46–62% |
| 访存受限（AI < 1） | 优先融合/减少流量 | 向量化前后一样快（chain=0 表） |
| 计算密集（AI 高） | 向量化 + FMA + 合适宽度 | 1.7×–4.6× |
| 想减少循环开销 | 先试 `unroll` | 语义更简单，不要求连续 |

## 踩雷集锦

1. **以为 `vectorize` 一定生成宽指令。** 本机默认 SSE2，`<8 x float>` 变两条 `movaps`。先 `inspect_source("asm")` 确认。
2. **用 `"llvm -mcpu=native"`。** 0.27 已禁用 CLI target 字符串，必须用 `tvm.target.Target({...})`。
3. **在 Windows 上依赖 `mcpu=native`。** TVM 会回退到 generic 并打日志，你以为什么都没变，其实 AVX2 没开。
4. **长度不整除还硬上 16 宽。** 谓词让 IR 里连 `<16 x float>` 都不剩，性能掉 20–40%。
5. **对跨步访问 vectorize。** 生成 `insertelement` 拼装，实测更慢。
6. **只测一次就下结论。** 本机共享负载下同一份代码能差 2–3 倍，必须多轮取中位数并看最小值。

## 进阶：再往深一层

- 用 `f.inspect_source("asm")` 对比 `+avx2`、`+avx512f` 的指令差异（本机无 AVX-512，会退化）。
- 把 `mattr` 换成 `["+sse4.2"]`，观察 `<8 x float>` 如何被 legalize 成 2×`<4 x float>`。
- 给循环加 `sch.annotate(..., "pragma_auto_unroll_max_step", 16)`，看它和 `vectorize` 如何叠加。
- 用 `TVM_LOG_DEBUG=1` 打开 codegen 日志，观察 Ramp → vector type 的那一步（release 包可能没有，属 `guided`）。
- 读 LLVM 的 `LoopVectorize` 成本模型，理解为什么 tile=32 时它突然不向量化了（Ch 16 会给出这个现象）。

## 动手练习

### 练习 1：把默认 target 换成 AVX2，量化收益

用同一个 `C = A*2+B`（n=1<<20），分别用 `target="llvm"` 和 `mattr=["+avx2","+fma"]` 编译，记录耗时和 GB/s。解释为什么收益远小于 2 倍。

<details>
<summary>参考思路</summary>

n=1M 时数组共 12MB，超出 L3，kernel 是 DRAM 带宽受限（实测 5–8 GB/s 量级）。AVX2 只能减少指令数，不能减少字节数，所以提升有限。要观察指令级收益，请把 n 降到 8192（96KB，驻留 L2），并给 kernel 增加算术量。

</details>

### 练习 2：构造一个"向量化变慢"的例子

写一个 kernel，使 `vectorize(8)` 比标量慢，并解释原因。

<details>
<summary>参考思路</summary>

两种做法：(a) 跨步访问 `A[i*2]`，观察 `insertelement` 拼装（实测慢 46–62%）；(b) 把 vectorize 加在 reduction 轴上，TVM 会报调度错误或生成串行归约。核心判据是"能否用一条连续向量 load 覆盖这 8 次访问"。

</details>

### 练习 3：尾部处理的三种写法

对 n=8190、宽度 16，分别用 (a) 直接 split+vectorize、(b) pad 到 8192、(c) 主循环 + 标量尾循环，比较 IR 里的向量宽度和耗时。

<details>
<summary>参考思路</summary>

(a) 会插入 `T.where`，IR 里只剩 `<4 x float>`/`<2 x float>`，实测 0.551 ns/elem；(b) 恢复 `<16 x float>`，0.398 ns/elem，代价是多算 2 个元素；(c) 在 TVM 里要用两次 `split`（`ceil` 与 `exact`）或手写 tail block，最灵活但代码最啰嗦。生产中 (b) 最常见。

</details>

### 练习 4：`unroll` 与 `vectorize` 的 IR 差异

对同一 kernel 分别 `unroll(8)`、`vectorize(8)`，打印两者的 LLVM IR，找出向量类型的出现次数，并解释为什么在本机上两者耗时接近。

<details>
<summary>参考思路</summary>

`vectorize` 版本里 `<8 x float>` 出现 18 次；`unroll` 版本里出现 8 次——LLVM 的 SLP 向量化把展开后的标量语句重新合并了。耗时接近（37.8 vs 36.4 us）正说明"TIR 原语 ≠ 机器指令"，中间隔着后端优化。

</details>

## 本章重点整理

- `vectorize` 是 TIR 层的**声明**：把 W 次迭代打包；实际宽度由 target 的 `mattr`/`mcpu` 和 LLVM legalization 决定。
- 本机默认 `target="llvm"` 是 SSE2 基线，`<8 x float>` 会落成两条 `movaps/addps`；加 `mattr=["+avx2","+fma"]` 才有 `ymm` + `vfmadd`。
- 向量化的三个前提：连续、对齐、长度可整除；任何一个不满足都会退化。
- 谓词（`T.where`）会把宽向量拆窄，实测每元素慢 22–38%；优先 pad。
- gather/scatter 是负收益区，实测慢 46–62%；先改 layout。
- 收益由算术强度决定：chain=0 时各宽度打平；chain=16 时同 target 内 1.7–2.9×，换到 AVX2+FMA 后最多 7×（generic 标量 → AVX2 vec32）。
- `vectorize` 与 `unroll` 语义不同，但在本机经 LLVM 优化后可能殊途同归。

## 自我检核

- [ ] 我能解释 `<8 x float>` 为什么在默认 target 上变成 `xmm` 指令
- [ ] 我能写出 `tvm.target.Target({"kind": "llvm", "mattr": [...]})` 并验证 asm
- [ ] 我知道向量化的三个前提，并能各举一个反例
- [ ] 我能解释 `T.where` 为什么会让 LLVM 放弃 16 宽
- [ ] 我能说出 `vectorize` 与 `unroll` 在 TIR 和机器码两层的区别
- [ ] 我能判断一个 kernel 是访存受限还是计算受限（用算术强度）
- [ ] 我知道本机为什么 `mcpu=native` 不生效

## 延伸阅读

- **TVM `Schedule.vectorize` 文档**（`tvm/s_tir/schedule/schedule.py:1188`，或官网 API 页）：读前置条件那一段。与本章的关联：解释什么循环"能"被向量化。
- **LLVM Language Reference: Vector Types**（https://llvm.org/docs/LangRef.html#vector-type）：读 `<N x T>` 与 `insertelement`/`shufflevector`。与本章的关联：看懂 IR 里的向量类型和 gather 拼装。
- **Intel Intrinsics Guide**（https://www.intel.com/content/www/us/en/docs/intrinsics-guide/）：查 `_mm256_fmadd_ps` 与 `vmovaps` 的对齐要求。与本章的关联：对齐那一节的硬件依据。
- **Agner Fog, Optimizing subroutines in assembly**（https://www.agner.org/optimize/）：读"Vectorization"一章。与本章的关联：为什么宽度不是越大越好、寄存器压力从哪来。
- **TVM 论文（OSDI 2018）第 4 节**：看 schedule 原语如何被设计成"声明式"。与本章的关联：`vectorize` 只是众多原语之一，语义都是"声明变换"而非"直接生成"。

---

→ [Ch 16 内存层次与 Roofline：为什么 tile 大小能决定生死](./16-memory-and-roofline.md)
