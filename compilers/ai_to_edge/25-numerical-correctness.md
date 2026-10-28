# Ch 25 数值正确性：逐层对齐

> **目标**：搞清楚「模型编译过之后数字变了」到底是哪一层变的，以及怎么用可复现的手段把它定位到具体算子。学完这章，你能自己推出一个容差，而不是抄一个 `1e-5`。

> **环境**：Ubuntu 22.04 / x86_64 AVX2 / torch 2.9.1+cpu（`/home/ubuntu/.venvs/learn/bin/python`）/ numpy 2.2.6 / gcc 11.4

## 为什么需要这个？

在 AI 编译器出现之前，数值误差是数值分析课的问题：写 BLAS 的人关心，调模型的人不关心。编译器把这个边界打破了——你写的是

```python
relu(x * 2 + y * 3 - z)
```

编译器看到的是一张图，它可以决定把三个算子融合成一个 kernel、把乘加变成 FMA、把归约切成 8 段并行累加。这些决定**会改变最终比特**。于是一个新问题出现了：

> 同一份权重、同一份输入，float32 参考实现和编译后的 kernel 输出不一样。是编译器错了，还是本来就该不一样？

要回答它有三个理由：**量化模型上线要过精度门**（业务说「掉点不超过 1%」，你得知道这 1% 里多少是量化、多少是编译器重排）；**回归测试需要容差**（拍到 CI 里的 `allclose` 用默认 1e-5，要么永远通过，要么被当 flaky 忽略）；**报 bug 需要证据**（说「编译器算错了」之前先证明差异不是浮点的正常行为）。

这一章的核心结论只有一句：

> **「融合」本身不改变数值，「重结合」才改变。**

记住它，后面所有现象都能归类：看到 bitwise 不等，先问「谁改了运算顺序」，而不是先怀疑编译器有 bug。

## 先建立直觉

想象一条装配线，三个工位依次加工一个零件：A 乘 2、B 加 `y*3`、C 减 `z`。

**融合（fusion）** 是把三个工位搬进同一个车间、零件不落地。加工步骤、每一步的精度、先后顺序**完全没变**，所以融合前后必须逐比特相同。

**重结合（reassociation）** 是另一个动作：把 `((x*2) + (y*3)) - z` 改成 `(x*2) + ((y*3) - z)`。顺序变了。浮点加减不满足结合律，所以结果会变：每一步都要把中间结果**舍入**到 float32 的格子上，两种顺序舍入的位置不同。

要把这件事讲清楚，得先知道 float32 的格子有多粗。

## 底层机制

### float32 的格子有多粗

float32 是 1 位符号 + 8 位指数 + 23 位尾数。尾数 23 位意味着：**在 1.0 附近，相邻两个可表示数的间距是 2^-23**。

实测（`numpy.finfo`）：

```
float32 eps = 1.1920929e-07  max = 3.4028235e+38  smallest_normal = 1.1754944e-38
float64 eps = 2.220446049250313e-16
spacing(1.0) f32 = 1.1920929e-07  f64 = 2.220446049250313e-16
spacing(1000.0) f32 = 6.1035156e-05
```

三个术语要分清，混用会导致后面所有判断都错：

- **eps（机器精度）**：1.0 与「下一个 float32」的相对间距，`2^-23 ≈ 1.19e-7`，是**相对**量。
- **ULP / spacing**：某个具体数值处相邻两数的间距，是**绝对**量，随指数变化。1.0 附近 `1.19e-7`，1000.0 附近 `6.10e-5`，1e8 附近 `8.0`（实测 `np.spacing(np.float32(1e8)) = 8.0`）。`spacing` 是 numpy 里的函数名。

记住 `spacing(1e8) = 8.0`，它马上会解释为什么 `1e8 + 1e-8` 什么也没发生。

### 为什么「同序融合」是逐比特相等的

证据包里最关键的对照（`code/09_numerics/numeric2.py`，实测）：

```
A 同序 stepwise vs fused: bitwise_equal =  True  max|diff| = 0.0
```

两次计算的表达式树：`stepwise: t1=x*2; t2=t1+y*3; t3=t2-z; relu(t3)`，`fused: relu(((x*2)+(y*3))-z)`。括号位置完全一样，只是「是否分成多个 kernel」不同。结果逐比特相同。

这说明：**融合 pass 只要不碰结合顺序，数值上是恒等变换**。Inductor 的 `fuse` 属于这一类——把算子塞进同一个循环体，循环体内的表达式顺序照抄图。所以「编译后精度掉了」的锅通常不该记在融合头上。真正危险的是重结合。

### 为什么重结合会变：8 个 ULP 从哪来

同一个表达式，只改括号（`code/09_numerics/numeric2.py`，实测）：

```
B 重结合 (x*2 + (y*3-z)): bitwise_equal = False  max|diff| = 9.5367431640625e-07  不同元素数 = 36 /256
```

三个数字都值得读：

- `9.5367431640625e-07` 正好是 `2^-20`；除以 eps 得 `8.0`，**正好 8 个 ULP**。
- 256 个元素里 36 个不同，约 14%。差异是**输入相关**的：只有某一步的舍入正好跨过格子边界才体现出来。

换一组随机输入用 numpy 复现同一结构（数字会不同，这是正常的）：`A vs C: bitwise equal = False max|diff| = 4.76837158203125e-07 不同元素 = 32 /256`，即 4.0 ULP。**不要指望 ULP 数稳定，要指望它和 eps 同量级**。

### 吸收与相消：为什么 `1e8 + 1e-8 - 1e8 = 0`

证据包（实测）：

```
float32: 1e8 + 1e-8 - 1e8 = 0.0
float64: 1e8 + 1e-8 - 1e8 = 1.4901161193847656e-08
```

原因就在 `spacing(1e8) = 8.0`：1e8 附近相邻可表示数相差 8.0。`1e-8` 比半个格子（4.0）小 9 个数量级，加进去**完全无法表示**，被舍入回 1e8。再减 1e8，得到 0。

float64 能保住它，因为 `spacing(1e8) f64 = 1.4901161193847656e-08`，和 `1e-8` 同量级。它也不是精确的 `1e-8`——被量化到了最近的格子上。

工程含义：**某层输出量级 1e4 以上，后面又要累加 1e-6 级修正，float32 会把修正吃掉**。LayerNorm 的 eps、attention 的大 mask 偏置、softmax 前的 logits 都在这个风险区。

同样的机制在归约里表现为「大数吃小数」。实测（`absorb2.c`，一个 1e8 加 4095 个 1.0）：`seq = 100000000 / pairwise = 100004088 / double ref = 100004095`。顺序累加把 4095 个 1.0 全吃掉，两两归约保住 4088。**同一份数据，三种加法顺序，三个答案。**

### 归约顺序：为什么「翻转输入」不是可靠的探针

教科书会说「改变求和顺序会改变结果」。这条在**你自己写的循环**里成立（上面 `absorb2.c` 就是），但在**库函数**里会变得不可预测。证据包实测：

```
dot  n=100000
  float64 ref   = -520.9780407362
  float32 顺序  = -520.9781494141  rel_err=2.086e-07
  float32 逆序  = -520.9781494141  rel_err=2.086e-07
  顺序 vs 逆序差 = 0.000000e+00  (= 0.000e+00 相对)
```

把输入翻转之后，float32 点积**一位都没变**。可能的原因是 torch 的 `dot` 不是单线程顺序累加，而是分块 + 多线程归约：每个线程算自己那块的部分和，块内还可能向量化成 4 路或 8 路。翻转整个数组时，每个处理器拿到的仍是同一批数，于是差别被抵消。

**但这个 0.0 不是普遍规律。** 我把同一个实验换了条件和重复次数，实测：

```
20 个 seed，n=100000，28 线程：exactly equal 2/20，median diff 1.068115234375e-04，max diff 4.2724609375e-04
固定 seed 0，改变线程数：
  threads= 1 f=-520.9782714844 r=-520.9783935547 diff=1.221e-04
  threads= 2 f=-520.9781494141 r=-520.9779052734 diff=2.441e-04
  threads= 4 f=-520.9781494141 r=-520.9783935547 diff=2.441e-04
  threads= 8 f=-520.9783935547 r=-520.9780273438 diff=3.662e-04
  threads=28 f=-520.9781494141 r=-520.9781494141 diff=0.000e+00
```

所以证据包那个 0.0 是「seed 0 + 28 线程」的巧合：20 个 seed 里只有 2 个能复现。更要紧的是——**无论 diff 是 0 还是 1e-4，它都不能告诉你归约按什么顺序做**。

> **你无法通过「调换输入顺序」来验证一个库函数的归约顺序。**

理由：翻转只改变「哪一批数进哪个处理器」，不改变分块策略本身。diff = 0 说明这一组数据恰好落在「翻转后每块的部分和不变」的情形，不代表顺序无关；diff ≠ 0 也只说明某个块的部分和变了，推不出具体顺序。想真的验证归约结构，只能读源码、看生成的 kernel（`TORCH_LOGS=output_code`），或者用让分块边界可预测的长度。

它同时给出一个正面结论：**归约误差由「分块策略 + 累加器精度」决定，不由输入顺序决定**。比较两个后端时别拿「顺序不同」当解释。误差本身呢？`rel_err = 2.086e-07`，除以 eps 是 1.75 ULP——10 万元素的点积误差不到 2 ULP，相当好。

### 归约长度与误差：三个实测一起看

```
sum(4096): f64ref=-65.820831446  f32=-65.820823669 (rel 1.18e-07)  两两=-65.820816040 (rel 2.34e-07)
sum(1000): float32=-16.74737358 float64=-16.74737038 rel=1.909e-07
```

换算成 ULP（用该量级处的 spacing）：

| 用例 | 绝对差 | 该量级 spacing | ULP |
|---|---|---|---|
| sum(1000) | 3.20e-06 | 1.9073486e-06 | 1.68 |
| sum(4096) f32 | 7.78e-06 | 7.6293945e-06 | 1.02 |
| sum(4096) 两两 | 1.54e-05 | 7.6293945e-06 | 2.02 |
| dot(100000) | 1.09e-04 | 6.1035156e-05 | 1.78 |

规律：**误差都在 1–2 ULP**，而且「两两归约」并不比「库的默认归约」更准（2.02 vs 1.02 ULP），因为它只是另一种顺序，不是更高精度。想真的更准，得换累加器（Kahan 补偿求和、或者 float64 累加），而不是换顺序。

### 误差怎么定量：五个指标各管什么

不要只报一个数。不同指标回答不同问题：

| 指标 | 公式 | 回答什么问题 | 什么时候用 |
|---|---|---|---|
| 绝对误差 | `max|a-b|` | 最坏一个元素偏了多少 | 输出有物理单位（像素、dB）时 |
| 相对误差 | `max|a-b|/|a|` | 偏了百分之几 | 输出跨多个量级时 |
| ULP | `|a-b| / spacing(a)` | 偏了几个格子 | 判断「是否只是舍入」 |
| 余弦相似度 | `a·b/(‖a‖‖b‖)` | 方向是否还一致 | embedding、attention 输出 |
| top-1 一致率 | `argmax(a)==argmax(b)` 的比例 | 分类结果是否还一样 | 分类模型上线前 |

它们会给出**互相矛盾**的结论，这很正常。实测（给输出加 0.5% 的相对噪声，模拟量化）：`allclose default (rtol=1e-5, atol=1e-8): False`、`allclose 1e-3: False`、`实际 max rel err = 0.017367836087942123`、`余弦相似度 = 0.9999871253967285`、`top-1 一致率 = True`。

`allclose` 两个容差都判「不等」，余弦相似度 0.99999，top-1 完全一致。只报 `allclose`，结论是「精度崩了」；只报 top-1，结论是「没问题」。**两个都对，取决于你要回答什么**。分类关心 top-1，检索关心余弦，逐元素对齐关心 ULP。

### 容差怎么推，而不是猜

`torch.allclose` 默认 `rtol=1e-5, atol=1e-8`。这两个数在 float32 下意味着什么？

实测换算：

```
default rtol 1e-5 in f32 ULP = 83.88608
default atol 1e-8 vs spacing(1.0): 1e-08 1.1920929e-07
```

- `rtol=1e-5` 相当于**允许 84 个 ULP**的偏差。对「只是舍入差异」的场景，这个容差松了 40 倍以上，几乎必然通过。
- `atol=1e-8` **比 1.0 附近的 spacing（1.19e-7）还小**。也就是说，在 1.0 附近，只要两个数不等，就至少差 1 个 ULP = 1.19e-7 > atol。这一项在数值 1.0 附近**完全不起作用**，只有数值接近 0 时才有意义。

实测三个 float32 数，看默认容差能放过多少：

```
a = 1.0  b = 1.0000001192092896  equal: False    allclose default: True
c = 1.0000009536743164           allclose default: True    rtol=1e-7 allclose: False
```

`b` 是 `a` 的下一个 float32（差 1 ULP），`c` 比 `a` 大 8 ULP。默认 `allclose` 对两个都判「相等」；把 `rtol` 收紧到 `1e-7` 才把 `c` 判为不等。所以用默认容差做回归测试，**8 ULP 以内的偏差是检测不到的**。

正确的做法是从 dtype 和归约长度推。设单次运算的相对误差上界是 `eps`：**逐元素算子**的误差累积按表达式深度走（`relu(x*2 + y*3 - z)` 深度 3，取 `4*eps ≈ 4.8e-7`）；**长度 n 的顺序归约**最坏按 `n*eps`，随机符号下按 `sqrt(n)*eps`，取后者更贴近实际；**分块/并行归约**约为 `(n/b + log2 b) * eps`，b 通常个位数到几十，量级远小于 n。

代入本章的数字验证：

```
n=   1000  eps*sqrt(n)=3.770e-06  n*eps=1.192e-04
n=   4096  eps*sqrt(n)=7.629e-06  n*eps=4.883e-04
n= 100000  eps*sqrt(n)=3.770e-05  n*eps=1.192e-02
```

实测相对误差：sum(1000) = 1.909e-07、sum(4096) = 1.18e-07、dot(100000) = 2.086e-07，全部**远小于** `eps*sqrt(n)`。所以 `sqrt(n)*eps` 是安全**上界**：正常实现必然通过，真出错（少乘一个数、累加器用了 fp16）必然被抓住。

所以推荐写法（注意 `atol` 要按输出量级缩放）：

```python
def tol_for(n, dtype=torch.float32, depth=3):
    eps = torch.finfo(dtype).eps
    return (depth + n ** 0.5) * eps        # 逐元素深度 + 归约项

def close(a, b, n, depth=3):
    t = tol_for(n, depth=depth)            # 相对容差
    scale = a.abs().max().item()           # 输出量级
    return torch.allclose(a, b, rtol=t, atol=t * scale)
```

`tol_for(100000)` 给 `3.805e-05`，比默认 `1e-5` 松，但比「84 ULP」有依据。`atol` 不能拍 1e-8，它必须随输出量级走。

同一组数据（float32 网络 vs float64 参考，最大偏差只有 8.2e-08）实测两种写法：

```
rel = 2.779210626957984e-06  scale = 0.34293311834335327
rtol only, atol=0:            False
rtol + atol=rel*scale:        True     (atol = 9.530833668356873e-07)
```

只给 `rtol` 仍判 False，因为 `out` 里有元素接近 0。**容差不是一个数，是一对。**

如果业务要求比 `tol_for` 更严，说明你需要 float64 累加或 Kahan 求和，而不是继续调容差。

## 可跑例子 1：四组对照一次跑完

保存为 `numeric_check.py`，用课程 venv 跑：

```python
import torch
torch.manual_seed(0)

# 1) 同序融合 vs 分步：应当逐比特相同
x = torch.randn(256); y = torch.randn(256); z = torch.randn(256)
step = torch.relu((x*2 + y*3) - z)
fused = torch.relu(x*2 + y*3 - z)
print("fusion   equal:", torch.equal(step, fused), " max|d|:", (step-fused).abs().max().item())

# 2) 重结合：把 (y*3 - z) 先算
reassoc = torch.relu(x*2 + (y*3 - z))
print("reassoc  equal:", torch.equal(step, reassoc),
      " max|d|:", (step-reassoc).abs().max().item(),
      " diff count:", int((step != reassoc).sum()), "/256")

# 3) 吸收：1e-8 在 1e8 面前不存在
big = torch.tensor([1e8], dtype=torch.float32)
small = torch.tensor([1e-8], dtype=torch.float32)
print("f32 1e8+1e-8-1e8 =", (big + small - big).item())
print("f64 1e8+1e-8-1e8 =", (big.double() + small.double() - big.double()).item())

# 4) 归约：翻转输入不是可靠探针（这里 seed 0 + 28 线程恰好差 0.0，属巧合）
torch.manual_seed(0)
n = 100000
a = torch.randn(n); b = torch.randn(n)
ref = torch.dot(a.double(), b.double()).item()
f = torch.dot(a, b).item()
r = torch.dot(a.flip(0).contiguous(), b.flip(0).contiguous()).item()
print(f"dot ref={ref:.10f} f32={f:.10f} rel={abs(f-ref)/abs(ref):.3e}")
print(f"fwd vs rev diff = {abs(f-r):.6e}")
```

实测输出：

```
fusion   equal: True  max|d|: 0.0
reassoc  equal: False  max|d|: 9.5367431640625e-07  diff count: 36 /256
f32 1e8+1e-8-1e8 = 0.0
f64 1e8+1e-8-1e8 = 1.4901161193847656e-08
dot ref=-520.9780407362 f32=-520.9781494141 rel=2.086e-07
fwd vs rev diff = 0.000000e+00
```

前四行与证据包逐字一致。最后一行是这组数据的巧合：把 `torch.manual_seed(0)` 换成 1，同一条 `print` 给出 `0.0003662109375`。

## 可跑例子 2：逐层 dump 并比对

整模型对不上时，第一个动作是**定位到层**。下面这个工具把每层输出都抓出来，同时用 float64 版本做参考：

```python
import torch
torch.manual_seed(0)

class Net(torch.nn.Module):
    def __init__(s):
        super().__init__()
        s.fc1 = torch.nn.Linear(64, 128)
        s.fc2 = torch.nn.Linear(128, 32)
        s.out = torch.nn.Linear(32, 8)
    def forward(s, x, dump=None):
        h = torch.relu(s.fc1(x))
        if dump is not None: dump["fc1"] = h
        h = torch.relu(s.fc2(h))
        if dump is not None: dump["fc2"] = h
        y = s.out(h)
        if dump is not None: dump["out"] = y
        return y

m32 = Net().eval()
m64 = Net().double().eval()
m64.load_state_dict({k: v.double() for k, v in m32.state_dict().items()})
x = torch.randn(16, 64)
d32, d64 = {}, {}
with torch.no_grad():
    y32 = m32(x, d32)
    y64 = m64(x.double(), d64)

print(f"{'layer':6s} {'max|d|':>10s} {'d/|max|':>10s} {'rel_max':>10s} {'cos':>13s}")
for k in d64:
    a, b = d64[k].double(), d32[k].double()
    d = (a - b).abs()
    scale = a.abs().max().item()
    cos = torch.nn.functional.cosine_similarity(a.flatten(), b.flatten(), dim=0).item()
    print(f"{k:6s} {d.max().item():10.3e} {d.max().item()/scale:10.3e} "
          f"{(d/a.abs().clamp_min(1e-12)).max().item():10.3e} {cos:13.9f}")
print("final allclose(default):", torch.allclose(y64.float(), y32))
```

实测输出：

```
layer      max|d|    d/|max|    rel_max           cos
fc1     4.068e-07  1.707e-07  4.450e-05   1.000000000
fc2     1.601e-07  2.420e-07  1.121e-04   1.000000000
out     8.321e-08  2.427e-07  5.888e-05   1.000000000
final allclose(default): False
```

三个读法：

1. **`d/|max|` 稳定在 1e-7 量级**（1.7e-7、2.4e-7、2.4e-7），和 eps 同量级。说明每一层都只差几个 ULP，**没有哪一层突然放大误差**。这是「健康」的形状。
2. **`rel_max` 高达 1e-4**，但这是因为分母里有接近 0 的元素（`clamp_min(1e-12)` 之后仍然很小）。**逐元素相对误差在接近 0 时没有意义**，用 `d/|max|` 或 ULP 代替。
3. **`allclose(default)` 判 False**，尽管最大偏差只有 8.3e-08。原因还是 `atol=1e-8` 太小、`out` 里有接近 0 的元素。这就是「用默认容差判小模型」的典型误报。

实测那个被判 False 的元素：

```
a= 0.00022561686637345701  b= 0.00022560358047485352  |d|= 1.3285898603498936e-08  tol= 1.2256168524515942e-08
```

`|d|` 只比 `tol` 大 8%，而它的绝对值是 2.2e-4——非常接近 0，正是 `atol` 主导的区间。结论：**这个 False 不是 bug，是容差选错了**。换成前面 `close()` 的写法（`t = tol_for(128, depth=12) = 2.779e-06`，`atol = t * 0.343 = 9.53e-07`）就通过。

如果某层真的坏了（比如量化 scale 设错、某个算子少了 bias），形状会不一样：误差会在**特定一层突然跳一个数量级**，然后被后面的层继承。这就是「逐层对齐」的价值——它把「模型不对」变成「第 3 层不对」。

## 可跑例子 3：`-ffast-math` 打开重结合

前两个例子是 Python 层。编译器层的开关在 C 这边。同一份源码、同一份输入：

```c
#include <stdio.h>
static float v[4096];
int main(void){
  unsigned s = 12345u;
  for (int i = 0; i < 4096; i++) { s = s*1103515245u + 12345u; v[i] = (float)((int)(s>>16) % 2001 - 1000) / 1000.0f; }
  float sum_a = 0.0f;
  for (int i = 0; i < 4096; i++) sum_a += v[i];
  float sum_b = 0.0f;
  for (int i = 4095; i >= 0; i--) sum_b += v[i];
  printf("%.9g %.9g\n", sum_a, sum_b);
  return 0;
}
```

四组开关，实测：

```
gcc -O2     -> -40.326046  -40.3260117
gcc -O3     -> -40.326046  -40.3260117
gcc -O3 -ffast-math -> -40.3259964  -40.326004
```

顺手看生成的循环体：`-O3` 下 `addps` 出现 0 次，`-O3 -ffast-math` 下出现 6 次。`addps` 是 4 路并行的 SIMD 加法——**编译器把 4 个独立的累加器并起来了**，这一步就是重结合，必须由 `-ffast-math`（或显式 `-fassociative-math`）授权。

三件事一起说明：不开 fast-math 时 `-O2` 与 `-O3` 结果完全一样（向量化没改求和顺序）；开了之后从 `-40.326046` 变成 `-40.3259964`，差 `4.96e-5`，约 **13 ULP**（该量级 spacing 是 3.8146973e-06）；连 `-O3` 的正序/逆序都不同，因为顺序累加本身就是语义。

所以「kernel 和参考实现对不上」的第一个排查点不是算法，是**两边的编译开关是否一致**。尤其当一边是 PyTorch 预编译的 `.so`（可能带 fast-math），另一边是你自己 `gcc -O2` 编的。

## 踩雷集锦

1. **用 `torch.allclose` 默认容差判断量化模型**
   错误直觉：返回 True 就是精度没问题。实测默认 `rtol=1e-5` 相当于 84 ULP，8 ULP 的偏差被判相等；`atol=1e-8` 又小于 1.0 处的 spacing，近 0 元素会误报 False。纠正：用 `tol_for(n, depth)` 推容差，至少同时报「ULP 分布 + top-1 一致率」。

2. **把「bitwise 不等」当成 bug**
   错误直觉：两个 kernel 输出不同比特 = 编译器算错。实测同序融合是 `bitwise_equal = True, max|diff| = 0.0`，而只改括号就变成 `False, 9.5e-07`（8 ULP）。纠正：先问「谁改了结合顺序」——是 fast-math、是 FMA、是归约分块、还是你自己换了表达式。8 ULP 内的差异是浮点语义允许的，不是 bug。

3. **在 float32 下比较 1e-8 级差异**
   错误直觉：`1e-8` 的差异说明「精度更高」。实测 `1e8 + 1e-8 - 1e8` 在 float32 下是 `0.0`，因为 `spacing(1e8) = 8.0`。纠正：先算 `spacing(量级)`，小于半个 spacing 的差异在 float32 里**根本不存在**，谈不上「更准」。

4. **忘了 `-ffast-math` 会打开重结合**
   错误直觉：`-O2` 和 `-O3 -ffast-math` 只是优化程度不同，结果应该一样。实测结果差 13 ULP，因为 `addps` 把累加器拆成 4 路。纠正：任何「参考实现 vs 优化实现」的比对，先把两边的 `-ffast-math` / `-ffp-contract` / `-fassociative-math` 状态对齐，再谈数值差异。

5. **用「调换输入顺序」验证归约顺序**
   错误直觉：把数组翻转，结果变了就说明是顺序累加。实测 20 个 seed 里只有 2 个正逆序完全相等（seed 0 恰好是其中之一），其余差 1e-4 量级。纠正：这个探针两个方向都不可靠——相等不代表顺序无关，不等也推不出顺序。验证归约结构只能读源码或看生成的 kernel。

## 本章重点整理

- **融合不改变数值，重结合才改变**。同序融合实测 `bitwise_equal = True, max|diff| = 0.0`；只改括号就变 `False, 9.5367431640625e-07`（8 ULP，36/256 元素不同）。
- float32 的 eps 是 `1.1920929e-07`（相对），ULP 随量级变：1.0 处 `1.19e-7`，1000 处 `6.10e-5`，1e8 处 `8.0`。**绝对误差必须带量级**。
- 吸收/相消：`1e8 + 1e-8 - 1e8` 在 float32 得 `0.0`，float64 得 `1.4901161193847656e-08`。
- 归约误差在 1–2 ULP（sum(4096) 1.02 ULP，dot(100000) 1.78 ULP），上界可用 `sqrt(n) * eps` 估。换顺序不会更准，换累加器才会。
- 「翻转输入」不是可靠的归约探针：20 个 seed 只有 2 个正逆序相等，相等也不代表顺序无关。
- 容差要推：`(表达式深度 + sqrt(n)) * eps`，并且 `atol` 要随输出量级缩放。默认 `rtol=1e-5` 是 84 ULP（太松），`atol=1e-8` 在 1.0 附近失效。
- 定位方法：**逐算子 → 逐层 → 整模型**。逐层 dump 看 `d/|max|` 是否稳定在 eps 量级，而不是看逐元素相对误差（近 0 处会爆炸）。

## 自我检核

- [ ] 不看笔记，能说出为什么「同序融合」必须逐比特相等，以及哪个动作才会改变数值
- [ ] 给定一个 float32 数值 1000.0，能算出它附近的 ULP 是 6.10e-5，并解释为什么 1e-8 的修正加不进去
- [ ] 能解释为什么「翻转输入后结果相同」不能证明归约与顺序无关，并说出正确的验证手段
- [ ] 给定 `n=4096`、float32、表达式深度 3，能推出一个容差并说明它的来源
- [ ] 能说出 `torch.allclose` 默认的 `rtol=1e-5` 相当于多少 ULP，以及 `atol=1e-8` 在什么区间失效
- [ ] 拿到「整模型输出不一致」，能写出逐层 dump 的比对脚本，并从 `d/|max|` 的形状判断是哪一层出的问题

## 延伸阅读

- **[What Every Computer Scientist Should Know About Floating-Point Arithmetic](https://docs.oracle.com/cd/E19957-01/806-3568/ncg_goldberg.html)** — David Goldberg, ACM Computing Surveys, 1991
  - **读哪里**：`Rounding Error`、`Guard Digits` 两节，以及 `The Associativity of Addition`（直接对应本章重结合）
  - **学什么**：ULP、吸收、相消的严格定义；「结合律不成立」是表示方式的必然，不是实现缺陷
  - **和本章关联**：本章「8 ULP」「1e-8 被吃掉」都有形式化推导。前提是能读简单误差界公式。

- **[IEEE 754-2019 Standard for Floating-Point Arithmetic](https://ieeexplore.ieee.org/document/8766229)**
  - **读哪里**：第 4 节（rounding attributes）、第 5 节（operations 舍入要求）、第 9 节（含 `fusedMultiplyAdd`）
  - **学什么**：`+ - * /` 必须正确舍入，但**不规定结合顺序**；FMA 是一次舍入不是两次
  - **和本章关联**：「重结合合法但结果会变」的规范依据。有人说「编译器违约」时先查这条。

- **[Numerical Computation Guide (Oracle Studio)](https://docs.oracle.com/cd/E19957-01/806-3568/)** — Sun Microsystems
  - **读哪里**：第 2 章 `IEEE Arithmetic` 的 `ulp` 表、第 4 章 `Exceptions and Denormalized Numbers`
  - **学什么**：各精度各量级的 ULP 查表；非规格化数对边缘设备的意义（很多 DSP 把 denormal 刷成 0）
  - **和本章关联**：「ULP 随量级变」的现成查表。前提是知道 float 位布局。

- **[PyTorch — Numerical accuracy](https://pytorch.org/docs/stable/notes/numerical_accuracy.html)**
  - **读哪里**：`TensorFloat-32`、`Reduced Precision Reduction` 两节，以及 `float32_matmul_precision`
  - **学什么**：torch 什么时候**主动降低**中间精度（TF32、bf16 归约），怎么关掉
  - **和本章关联**：解释「同一份代码在 A100 和 CPU 上结果不同」。本章环境是 CPU（`torch 2.9.1+cpu`），TF32 不生效。

- **[Further Remarks on Reducing Truncation Errors](https://dl.acm.org/doi/10.1145/363707.363723)** — William Kahan, CACM 8(1), 1965
  - **读哪里**：全文两页；核心是那个带补偿项的求和循环
  - **学什么**：为什么「换顺序」不能提升精度、「补偿舍入误差」可以；代价是多一次减法和一个寄存器
  - **和本章关联**：解释两两归约（2.02 ULP）为何不比默认归约（1.02 ULP）更准——它只换了顺序。MCU 上做高精度累加的标准手段。

数值对了，下一个问题是快不快。但「快」在边缘设备上是个陷阱词：你测的可能是模拟器、可能是冷启动、可能只是内存带宽。

→ [Ch 26 性能测量：在设备上量什么、怎么量](./26-performance-measurement.md)
