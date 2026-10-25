# Ch 26 正确性：golden、容差、模糊测试与数值稳定性

> **目标**：建立一套能抓住「编译后算错了」的测试方法：golden 怎么选、容差怎么定、差分测试和模糊测试各抓什么；并亲眼看到三个静默失败——顺序求和算出 0、int8 累加溢出成 0、空 reduction 返回未初始化内存。

## 为什么需要这个？

编译器最危险的 bug 不是崩溃，是**静默算错**。崩了你立刻知道；算错 1%，模型照样出 logits，照样能跑完评测，只是精度掉了半个点，而你会先去怀疑数据、超参、随机种子。

本章开头先给三个本机实测的静默失败，全部来自 `te.sum` 这一种最简单的算子：

| 输入 | TVM 编译后的结果 | 正确结果 | 现象 |
|---|---|---|---|
| float32，4096 个元素，`x[0]=1e8, x[-1]=-1e8` | `0.0` | `4094.0` | 相对误差 100% |
| int8，`(4,1024)` 全填 100 | `[0 0 0 0]` | `[102400 ...]` | 累加在 int8 里溢出 |
| float32，`(4,0)` 空 reduction | `2.46e-42`、`1.74e25`… | `[0 0 0 0]` | 输出 buffer 根本没写 |

三条都是「代码读起来完全正确」的 kernel。所以验收标准不能是「看起来对」，只能是对着 golden 量出来的误差。

## 先建立直觉：三种「错」，三种抓法

```text
崩溃 / 抛异常        -> 单元测试跑一遍就暴露
形状或 dtype 不对    -> 差分测试（对着 eager 比）暴露
数值错了但类型对     -> 只有 golden + 容差 + 构造输入能抓
```

第三种最难，因为它需要你先想清楚「正确答案是什么」。这一章的大部分篇幅都在解决这个问题。

## 核心概念一：golden 是「你信的那个实现」

golden（基准结果）不是随便找一个能跑的实现。按可信度从高到低：

| 来源 | 可信度 | 什么时候用 |
|---|---|---|
| 用 float64 重算同一公式 | 最高 | 能在 CPU 上重算就重算，精度高约 9 个数量级（eps: `2.2e-16` vs `1.2e-7`） |
| 框架 eager（torch CPU） | 高 | 公式复杂到 float64 也算不动，或本身就在验证「编译 vs eager」 |
| 同编译器、不同调度 | 中 | 只能证明「两个调度一致」，证明不了「两个都对」 |
| 历史快照 / 论文数字 | 低 | 只能当回归基线，不能当正确性证据 |

本章的差分实验同时算三份：TVM float32、torch eager float32、torch float64。**float64 那份才是判据**，另外两份是「待测对象」。

一个真实的反直觉结果：float32 下 TVM 与 eager 互相之间的误差（median `1.9e-6`）比它们各自对 float64 的误差（`1.6e-6`）还大。两个实现都错一点，方向不同，互相比较时误差会叠加——所以「和另一个 float32 实现对得上」不等于对。

## 核心概念二：容差是契约，不是松紧旋钮

`np.allclose(a, b, rtol, atol)` 的判据是**逐元素**的：

```text
|a[i] - b[i]| <= atol + rtol * |b[i]|
```

三个必须记住的语义：

1. **`atol` 是绝对量，和数值尺度无关。** 输出量级是 `1e-6` 时，`atol=1e-3` 等于允许 1000 倍误差；输出量级是 `1e5` 时，`atol=1e-3` 等于要求相对误差 `1e-8`，float32 根本做不到。
2. **`rtol` 是逐元素的相对量。** 遇到参考值接近 0 的元素（matmul 里经常有），`rtol * |b[i]|` 也接近 0，判据退化成 `atol`。所以矩阵乘法里逐元素 `allclose` 会误报。
3. **矩阵/张量级验收应该用全局归一化**，也就是 Ch 0 那个式子：

```python
maxerr = np.abs(got - ref).max()
ok = maxerr <= atol + rtol * np.abs(ref).max()
```

这里的 `rtol` 乘的是**整个 tensor 的最大值**，不是每个元素各乘各的。它回答的是「最大误差相对整体量级有多小」，这才是你想要的。

## 核心概念三：差分测试抓高频错，模糊测试抓边界

两者不是替代关系，抓的东西不一样：

| | 差分测试 | 模糊测试 |
|---|---|---|
| 输入从哪来 | 随机但**分布正常** | 随机 **shape / dtype / 边界值** |
| 抓什么 | 实现写错了（索引、广播、公式） | 边界没处理（空、0 维、溢出、极值） |
| 抓不到 | 罕见输入结构引发的精度崩塌 | 常规输入下的系统性公式错误 |
| 成本 | 低，跑 100 组几秒钟 | 中，失败往往需要人工判断「这是 bug 还是约定」 |

本机实验里，100 组随机输入的最大相对误差是 `2.99e-5`——全部是 float32 顺序累加的正常噪声，其中 4 组超过 `1e-5` 阈值，也不是 bug。而一个 3 行构造出来的输入让相对误差变成 `1.0`。**随机测试给的是信心，不是保证。**

## 底层机制：调度就是数值

IEEE-754 浮点加法**不满足结合律**：`(a+b)+c != a+(b+c)`。而 reduction 的累加顺序，恰好是 schedule 决定的。

在 `1e8` 附近，float32 的间隔（ulp）是 8：

```python
np.spacing(np.float32(1e8))   # 8.0
```

意思是 `1e8 + 1.0` 在 float32 里**等于 `1e8`**——加数被整个吞掉。这就是本章第一个实验的原理：先累到 `1e8`，后面 4094 个 `1.0` 全部消失，最后减去 `1e8` 得到 0。

看一眼生成的 C 代码，就知道「谁在负责精度」（下面是 `(4,1024)` 的版本，1D 4096 只是少一层外层循环）：

```c
float* s = ((float*)(((DLTensor*)s_handle)[0].data));
for (int32_t i = 0; i < 4; ++i) {
  for (int32_t k = 0; k < 1024; ++k) {
    if (k == 0) { s[i] = 0.000000e+00f; }
    s[i] = (s[i] + x[((i * 1024) + k)]);
  }
}
```

`源码依据`：`tvm.build(sch.mod, target="c").inspect_source()` 的输出，TVM 0.27.0.post1。累加变量就是 `s[i]` 本身，dtype 由输出 buffer 决定——`int8` 的版本是 `int8_t* s`，`float16` 的版本是 `half* s`。**编译器忠实地按你声明的 dtype 算，不会自动升精度。**

## 示例一：看起来对的 sum，算出了 0

构造数据：4096 个 `1.0`，首元素改成 `1e8`，末元素改成 `-1e8`。float64 精确值是 `4094.0`。

```python
# .scratch\ai-compiler\ch26\exp1_reduce_order.py
import numpy as np, tvm
from tvm import te

N = 4096
x_np = np.ones(N, dtype=np.float32)
x_np[0] = 1e8
x_np[-1] = -1e8

def build(parts=1):
    x = te.placeholder((N,), name="x")
    rk = te.reduce_axis((0, N), name="k")
    s = te.compute((1,), lambda i: te.sum(x[rk], axis=rk), name="s")
    func = te.create_prim_func([x, s])
    sch = tvm.s_tir.Schedule(func)
    if parts > 1:                                  # 把 k 切成 parts 段，再 rfactor
        ko, ki = sch.split(sch.get_loops(sch.get_sblock("s"))[1], factors=[parts, None])
        sch.rfactor(ki, 0)
    return tvm.build(sch.mod, target="llvm")

for parts in (1, 4):
    f = build(parts)
    out = tvm.runtime.empty((1,), "float32", tvm.cpu())
    f(tvm.runtime.tensor(x_np, tvm.cpu()), out)
    print(parts, out.numpy()[0])
```

实测输出：

```text
1 0.0
4 0.0
```

`rfactor` 也没救回来。原因在它生成的 TIR 里：切分后得到 1024 个部分和，但最后那一步仍然是**顺序**遍历 1024 个部分和，而 `-1e8` 落在最后一个部分和里，前面积累的 `1e8` 到那一刻才被抵消。

把同样的数据喂给 7 种实现，一次性看清「顺序」值多少：

| 实现 | 结果 | 绝对误差 | 相对误差 |
|---|---|---|---|
| float64 精确值 | 4094.0 | 0 | 0 |
| TVM 顺序累加 | 0.0 | 4094 | 100% |
| TVM `split(4)` + `rfactor` | 0.0 | 4094 | 100% |
| TVM 两阶段（4 个 chunk 部分和） | 3072.0 | 1022 | 25% |
| `np.sum`（float32） | 4080.0 | 14 | 0.34% |
| 手写 Kahan 补偿求和 | 4096.0 | 2 | 0.05% |
| TVM float64 累加器 | 4094.0 | 0 | 0 |

两阶段版本的关键差别在于「先在 chunk 内部抵消，再合并 chunk」——`+1e8` 和 `-1e8` 被分到同一个 chunk，抵消发生在 1e8 量级被污染之前：

```python
x2 = te.placeholder((4, 1024), name="x2")
rk2 = te.reduce_axis((0, 1024), name="k")
part = te.compute((4,), lambda c: te.sum(x2[c, rk2], axis=rk2), name="part")
rc = te.reduce_axis((0, 4), name="c")
tot = te.compute((1,), lambda i: te.sum(part[rc], axis=rc), name="tot")
```

Kahan 剩下 2 的误差是因为最后一步加法本身还有一次舍入，它压住的是**累积**误差，不是单次舍入。

float64 累加器的写法只有一行改动：

```python
s = te.compute((1,), lambda i: te.sum(x[rk].astype("float64"), axis=rk), name="s")
```

输出 `4094.0`，代价是 8 字节累加器和更慢的 reduce。**这是编译器不会替你做的决定**：它不知道你的数据有 8 个数量级的动态范围。

## 示例二：100 组随机输入 vs 一个构造输入

待测图：`out = sum(relu(X @ W + b), dim=1)`，`M=8, K=16, N=32`，随机缩放 `1e-3..1e3`。

```python
rows = []
for t in range(100):
    scale = float(10.0 ** rng.integers(-3, 4))
    a  = (rng.standard_normal((M, K)) * scale).astype(np.float32)
    b  = (rng.standard_normal((K, N)) / np.sqrt(K) * scale).astype(np.float32)
    bi = (rng.standard_normal(N) * scale).astype(np.float32)
    got  = run_tvm(a, b, bi)                       # TVM 编译结果
    ea   = eager(a, b, bi, np.float32)             # torch eager
    gold = eager(a, b, bi, np.float64)             # golden
    rows.append((np.abs(got-gold).max(), np.abs(ea-gold).max(), np.abs(got-ea).max()))
```

实测（100 组，单位是绝对误差）：

```text
tvm_vs_f64     median=1.587e-06 p90=1.286e-01 max=2.088e+00
eager_vs_f64   median=1.532e-06 p90=7.031e-02 max=1.545e+00
tvm_vs_eager   median=1.907e-06 p90=1.281e-01 max=3.000e+00
tvm 相对误差    median=9.66e-08  max=1.91e-07
```

注意 p90 已经到 `0.13`。这不是编译器的锅：`scale` 抽到 `1e3` 时输出量级在 `1e8` 附近，绝对误差自然跟着放大。**绝对误差必须和量级一起看**，这也是为什么表里要补一行相对误差。

再看同一个 `te.sum` kernel 的随机 vs 构造对比：

```text
100 组随机向量：max rel = 3.0e-5，其中 4/100 超过 1e-5 阈值（全是噪声）
构造输入：      rel = 1.000（结果 0，正确值 4094）
```

结论很直接：随机测试跑 100 组，只会告诉你「大致没问题」；它连 100% 的错误都没碰到，因为随机正态输入不会制造 `1e8` 与 `1` 混在同一个累加器里的局面。

## 示例三：模糊测试抓到的三个真实失败

模糊测试的输入空间是 `(shape, dtype, 填充值)`。同一个 harness：

```python
# .scratch\ai-compiler\ch26\exp3_fuzz_probe.py
def run_axis_sum(shape, dtype, fill):
    x = te.placeholder(shape, name="x", dtype=dtype)
    rk = te.reduce_axis((0, shape[-1]), name="k")
    s = te.compute(shape[:-1], lambda i: te.sum(x[i, rk], axis=rk), name="s")
    func = te.create_prim_func([x, s])
    f = tvm.build(tvm.s_tir.Schedule(func).mod, target="llvm")
    arr = np.full(shape, fill, dtype=dtype)
    o = tvm.runtime.empty(shape[:-1], dtype, tvm.cpu())
    f(tvm.runtime.tensor(arr, tvm.cpu()), o)
    return o.numpy()
```

**失败 1：int8 累加溢出。** `run_axis_sum((4, 1024), "int8", 100)`

```text
TVM:   [0 0 0 0]
numpy: [102400 102400 102400 102400]
torch: [102400 102400 102400 102400]
```

`100 * 1024 = 102400`，在 int8 里回绕成 0。生成的 C 里累加变量是 `int8_t* s`，逐次 `s[i] = s[i] + x[...]` 全部按 8 位算。numpy 和 torch 的整型 reduction 会升到 `int64`，TVM 不会——除非你在图里显式写 cast。

**失败 2：float16 累加精度崩塌。** `run_axis_sum((4, 2048), "float16", 0.3)`

```text
TVM:   560.5
torch: 614.5
精确:   614.4
```

误差 8.8%。float16 在 512 附近的 ulp 是 0.5，累加超过 512 后每次加 0.3 会被舍到 0 或 0.5，累加器「卡住」。用 numpy 模拟两种累加方式，两个数都能精确复现：

```python
a = np.full(2048, np.float16(0.3))
acc = np.float16(0)
for x in a:
    acc = np.float16(acc + x)                                  # 560.5，和 TVM 一致
np.float16(np.float32(np.float16(0.3)) * np.float32(2048))     # 614.5，和 torch 一致
```

同一个算子，两份实现，两个语义——这种差异只能靠差分测试发现，靠读代码永远看不出来。

**失败 3：空 reduction 返回未初始化内存。** `run_axis_sum((4, 0), "float32", 0.0)`（连续跑 5 次，这里挑 4 次）

```text
第 0 次: [-1.386e-10, 7.217e-43, -9.746e-08, 7.217e-43]
第 1 次: [-9.488e-09, 7.217e-43,  5.045e-44, 0.0]
第 2 次: [ 0.0,       0.0,        2.46e-42,  0.0]
第 4 次: [ 1.741e+25, 1.703e+25,  1.541e-43, 0.0]
```

numpy 和 torch 都返回 `[0 0 0 0]`。原因在 TIR 里一目了然：

```text
for i, k in T.grid(4, 0):        # 循环体一次都不执行
    with T.sblock("s"):
        with T.init():
            s[v_i] = T.float32(0.0)     # 初始化也被困在循环体里
```

`T.init()` 写在 block 内部，而 block 在 `k` 循环里；`k` 上界是 0，所以初始化语句永不执行，输出 buffer 保持 `tvm.runtime.empty` 分配时的垃圾值。**这既是正确性 bug，也是非确定性**：同一份代码每次跑出不同结果。

## 示例四：同一组输出，三种容差给出三个判决

固定 `M=K=N=16`，三个变体：`same-ord`（正确）、`splitK`（正确但换了累加顺序）、`relu-bug`（多做了 `max(x,0)`，语义错误）。三档输入缩放。

```text
scale  variant    maxerr     rel       逐元素atol1e-3  全局atol1e-3  全局atol+rtol
1e-06  same-ord   1.86e-18   1.19e-07      True         True          True
1e-06  relu-bug   1.56e-11   1.00e+00      True         True          True   <-- 漏报
1e+00  same-ord   1.54e-06   9.87e-08      True         True          True
1e+00  relu-bug   1.56e+01   1.00e+00     False        False         False
1e+05  same-ord   8.88e+03   5.70e-08     False        False          True   <-- 只有它判对
1e+05  splitK     7.94e+03   5.10e-08     False        False          True
1e+05  relu-bug   1.56e+11   1.00e+00     False        False         False
```

三行结论：

- `1e-6` 那行：一个**100% 相对误差**的 bug，`atol=1e-3` 判 True。绝对容差在小量级上等于没检查。
- `1e5` 那行：**正确的 kernel** 被 `atol=1e-3` 判 False。因为 matmul 有抵消，少数元素接近 0，而绝对误差随量级放大到 `8.8e3`。这是误报。
- 只有 `maxerr <= atol + rtol * |ref|.max()` 这一种判据在全部 7 行上给出了正确判决。

所以容差要反过来定：先写「允许的相对误差」（float32 累积误差通常 `1e-5`~`1e-6`），再取 `atol` 兜住「接近 0 的输出」，最后用一组**跨量级**的输入验证这个判据本身没有漏洞（上面这 7 行就是这种验证）。

## 示例五：NaN/Inf 语义与「重写毒药库」

`allclose` 对 NaN/Inf 的处理不统一，实测：

```python
np.allclose([nan], [nan])                     # False
np.allclose([nan], [nan], equal_nan=True)     # True
np.testing.assert_allclose([nan], [nan])      # PASS（默认 equal_nan=True）
torch.allclose(t, t_nan)                      # False
torch.allclose(t, t_nan, equal_nan=True)      # True
np.allclose([inf], [inf])                     # True
np.allclose([inf], [1e308])                   # False
np.allclose([inf], [-inf])                    # False
np.float32('inf') - np.float32('inf')         # nan
```

`np.testing.assert_allclose` 默认把 `nan == nan` 当通过，`np.allclose` 和 `torch.allclose` 默认不通过。**同一份测试换个断言函数，NaN 的判决就反了。** 如果你的 kernel 可能产出 NaN，「NaN 出现在正确的位置」和「NaN 不该出现」是两种完全不同的契约，要显式写。

图级优化还要额外防一类错：代数重写改变了 IEEE 语义。拿 10 个「毒药输入」跑 5 条常见重写：

| 重写 | nan | inf | -inf | 3e38 | -0.0 | 结论 |
|---|---|---|---|---|---|---|
| `x*0 -> 0` | 值错 | 值错 | 值错 | ok | 位错 | 只有有限输入安全 |
| `x-x -> 0` | 值错 | 值错 | 值错 | ok | ok | 同上 |
| `x+0 -> x` | ok | ok | ok | ok | 位错 | 值等价，位不等价 |
| `x*1 -> x` | ok | ok | ok | ok | ok | 完全安全 |
| `(x*2)/2 -> x` | ok | ok | ok | 值错 | ok | 溢出边界破坏等价 |

`-0.0` 的「位错」是 `-0.0 * 0 = -0.0`，而常量 `0.0` 是 `+0.0`；`(-0.0) + 0.0 = +0.0`。数值上相等，bitwise 不等——如果你的验收用 bitwise 比较，这类重写会全部失败。

每个重写规则配一组毒药输入做回归，成本极低（上面整张表 50 次比较，整个脚本 0.5 秒内跑完），收益是防止 Ch 6 那种 `x*0 -> 0` 把 NaN 变成 0。

## 示例六：确定性——同一输入两次跑，是不是同一串 bit

先给结论：**确定性不是默认属性，它取决于并行策略和初始化**。

```python
# TVM：64 行，每行 4096 个元素求和，外层 parallel
outs = [run() for _ in range(3)]
all(np.array_equal(outs[0], o) for o in outs[1:])     # True
```

TVM 这个 kernel 三次运行 bitwise 相同。但换成 torch 的大 reduction，只改线程数：

```text
threads=1  sum = -990.9303588867188
threads=2  sum = -990.930419921875
threads=4  sum = -990.930908203125
threads=8  sum = -990.9305419921875     # 四个值互不相同
```

同一份输入、同一个库，只是线程数变了，结果在 `-990.93036` 到 `-990.93091` 之间漂（相对差 `5.5e-7`）。而 512×512 的 matmul 在线程数 1 和 4 下 bitwise 完全相同——因为每个输出元素的累加顺序不随线程数变。

实用判据：

- **要 bitwise 复现**：固定线程数、固定输入、避免并行 reduction，或者接受 float64。
- **要数值可复现**：用 `allclose` 加合理容差，而不是 `==`。
- **空 buffer 是确定性的噩梦**：示例三的 `(4,0)` 每次输出都不同，任何「重跑一次看看」的调试方法都会失效。

## 对比与取舍

| 手段 | 额外成本 | 抓到的错误类型 | 上线前必须？ |
|---|---|---|---|
| float64 golden + `maxerr` | 低 | 系统性数值错误 | 是 |
| 100 组随机差分 | 低 | 索引/公式/广播错误 | 是 |
| 跨量级输入（1e-6/1/1e5） | 很低 | 容差判据本身的漏洞 | 是 |
| 毒药输入（nan/inf/-0/极值） | 极低 | 重写规则的语义破坏 | 是（图优化必做） |
| 模糊测试（shape/dtype 组合） | 中 | 空张量、溢出、精度崩塌 | 建议 |
| bitwise 回归 | 低但脆 | 非确定性、未初始化 | 只在需要时 |

## 踩雷集锦

1. **拿另一个 float32 实现当 golden**：两边误差方向不同，互相比较可能比各自对 float64 的误差更大。
2. **只给 `atol`**：小量级漏报、大量级误报，两个方向都真实发生过（示例四）。
3. **逐元素 `allclose` 验 matmul**：参考值接近 0 的元素让 `rtol` 失效，正确 kernel 被误判。
4. **假设整型 reduction 会自动升位**：TVM 按 buffer dtype 累加，int8 求和会回绕。
5. **假设 fp16 reduction 精度够**：本机 `560.5` vs `614.5`，误差 8.8%。
6. **用随机测试代替边界测试**：100 组随机输入没抓到 100% 的错误。
7. **忘记空 reduction**：`(4,0)` 返回未初始化内存，且每次不同。
8. **用 `==` 比较浮点输出**：必然失败，且失败信息毫无信息量。

## 进阶：再往深一层

- **ULP 与误差预算**：把「允许误差」换算成 ULP。float32 的 `eps=1.19e-7`，K 长度 4096 的顺序累加最坏误差上界约 `K*eps*sum|x|`，这就是为什么长 reduction 必须换累加顺序或升精度。
- **Pairwise / Kahan / Neumaier**：成对求和把误差从 `O(K)` 降到 `O(log K)`；Kahan 用补偿项把误差压到 `O(1)`；Neumaier 修掉了 Kahan 在「大数吞小数」时的失效分支。
- **Metamorphic testing**：没有 golden 时，测「变换关系」——`sum(x)` 与 `sum(concat(x, 0))` 必须相等，`relu(x)` 与 `relu(-x)` 的逐元素和必须等于 `|x|` 的和。AI 编译器常用这招验证没有参考实现的算子。
- **`rfactor` 的顺序陷阱**：`rfactor` 只改变「谁并行、谁合并」，不保证最终合并是树形；要真正的成对归约得自己写两阶段或三阶段。
- **确定性开关**：torch 有 `torch.use_deterministic_algorithms(True)`，但它只覆盖部分算子，且常常以性能为代价；TVM 侧没有等价开关，只能控制调度。

## 动手练习

### 练习 1：给 sum 加一个 Kahan 版本，和顺序累加对比

任务：用 `te.compute` 写两阶段 kernel，第一阶段每个元素输出 `(sum, compensation)`，第二阶段合并；在示例一的数据上验证结果是否等于 4094。

<details>
<summary>参考解答</summary>

手写 Kahan 在 TE 里很别扭（需要循环携带两个状态，而 `te.sum` 只带一个累加器）。更实际的做法是在 Python 侧先验证公式（示例一的表里「手写 Kahan」一项是 `4096.0`），再在 TVM 里用 float64 累加器达到同样的精度（`4094.0`）。如果一定要在 kernel 里做 Kahan，用 `te.scan` 或直接写 TIR 的 `for` 循环携带两个 buffer。

</details>

### 练习 2：把差分测试扩到 1000 组，看分布的尾部

任务：把示例二的 `range(100)` 改成 `range(1000)`，统计 p99 和 max，并回答：尾部的误差是量级放大还是真的 bug？

<details>
<summary>参考解答</summary>

先把每组输入按 `scale` 分组统计，再看「同一 scale 内」的误差分布。如果尾部的绝对误差总是出现在 `scale=1e3` 的组里，而相对误差分布没有变宽，那就是量级放大而不是 bug。判断标准始终是相对误差，不是绝对误差。

</details>

### 练习 3：写一个必须失败的测试

任务：为 `(4,0)` 的空 reduction 写一个 pytest 用例，断言输出全为 0。跑一遍，确认它现在会失败，并把失败信息记录下来。

<details>
<summary>参考解答</summary>

```python
def test_empty_reduce_returns_zero():
    out = run_axis_sum((4, 0), "float32", 0.0)
    assert np.all(out == 0), out
```

它会以一定概率失败（未初始化内存里恰好有 0 的槽位时通过）。这种「概率性失败」本身就是 bug 的证据——如果它总是通过，说明 buffer 恰好被清零过。修复方式是在 kernel 外层显式把输出清零，或者在 IR 层把 `T.init()` 挪到 reduction 循环之外。

</details>

### 练习 4：设计一个能抓住 relu bug 的判据

任务：示例四里 `atol=1e-3` 在 `scale=1e-6` 时漏报了 100% 的错误。设计一个判据，让它在三档 scale 上都能抓住 `relu-bug`、同时不误报 `splitK`。

<details>
<summary>参考解答</summary>

用全局归一的 `maxerr <= atol + rtol*|ref|.max()`，并把 `atol` 设成「输出量级 × 1e-6」而不是固定 1e-3。或者更稳的做法：先算 `maxerr / |ref|.max()`，要求它小于 `1e-5`；再单独检查符号一致性，即 `(got < 0) == (ref < 0)` 的比例。relu bug 的本质是符号被改，不是数值偏移，符号检查比容差更直接。

</details>

### 练习 5：给你的重写规则找毒药输入

任务：从 Ch 6 的四个 pass 里挑一个重写，找出至少一个能让它改变结果的输入，并写进回归用例。

<details>
<summary>参考解答</summary>

`x*0 -> 0` 用 `inf` 就够：`inf*0 = nan`，改成 `0` 就是错的。`x+0 -> x` 用 `-0.0`：值不变但 bit 变了。`(x*2)/2` 用 `3e38`：`3e38*2` 溢出成 `inf`，再除 2 还是 `inf`。回归用例要同时断言「重写后与重写前结果一致」，而不只是「重写后能跑」。

</details>

## 本章重点整理

- 编译器最危险的失败是静默算错；验收必须对着 golden 量误差。
- golden 优先用 float64 重算；另一个 float32 实现只能当待测对象，不能当真理。
- `allclose` 是逐元素判据；张量级验收用 `maxerr <= atol + rtol*|ref|.max()`。
- 固定 `atol` 在不同量级上含义完全不同：小量级漏报，大量级误报，两个方向都实测到了。
- 累加顺序由 schedule 决定，所以调度就是数值；`1e8` 附近 float32 的 ulp 是 8。
- 整型 reduction 不会自动升位，fp16 reduction 的累加精度可能差 8.8%。
- 空 reduction 可以返回未初始化内存，这既是正确性 bug 也是非确定性来源。
- 随机测试给信心，构造输入和毒药库才给保证。

## 自我检核

- [ ] 我能说出三种「错」以及各自该用哪种测试抓
- [ ] 我能写出 float64 golden 的差分测试骨架
- [ ] 我能解释为什么 `atol=1e-3` 在小量级漏报、在大量级误报
- [ ] 我知道为什么逐元素 `allclose` 验 matmul 会误报
- [ ] 我能解释 `1e8 + 1.0 == 1e8` 与 `np.spacing(1e8)` 的关系
- [ ] 我知道 int8 求和为什么会得到 0，以及生成的 C 长什么样
- [ ] 我能说出 `np.allclose` 与 `np.testing.assert_allclose` 在 NaN 上的区别
- [ ] 我知道什么情况下结果会随线程数变而 bitwise 不同

## 延伸阅读

- **Goldberg, What Every Computer Scientist Should Know About Floating-Point Arithmetic**（https://docs.oracle.com/cd/E19957-01/806-3568/ncg_goldberg.html）：读「Rounding Error」与「The Fused Multiply-Add」两节。与本章的关联：ulp、结合律失效、误差上界的原始推导。
- **numpy `allclose` 文档**（https://numpy.org/doc/stable/reference/generated/numpy.allclose.html）：读参数 `rtol`/`atol`/`equal_nan` 的默认值与判据公式。与本章的关联：容差语义的权威定义。
- **PyTorch 可复现性说明**（https://pytorch.org/docs/stable/notes/randomness.html）：读「Reproducibility」一节。与本章的关联：确定性的工程边界。
- **TVM schedule 文档**（https://tvm.apache.org/docs/）：读 `rfactor` 与 `split` 的配合。与本章的关联：为什么换调度会换数值。
- **Kahan, Further Remarks on Reducing Truncation Errors (1965)**：读补偿求和的公式与证明思路。与本章的关联：顺序之外的第二条路。

---

→ [Ch 27 性能工程：profiling、瓶颈定位与基准回归](./27-performance-engineering.md)