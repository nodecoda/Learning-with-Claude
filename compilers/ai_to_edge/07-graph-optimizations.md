# Ch 7 — 图级优化：融合、常量折叠与 layout

> **目标**：看懂图这一层能做的三类改写——算子融合（operator fusion）、常量折叠（constant folding）、layout 变换，并判断一次改写省下的到底是内存往返、kernel 启动，还是两者都有。学完这章，你能从一段生成的 kernel 名和循环体，反推编译器在图级做了哪些决定。

> **环境**：torch 2.9.1+cpu、Python 3.10、x86_64（AVX2）、Ubuntu 22.04。GPU（Triton 后端）的融合形态不同，本章只在对照处提及。

> **证据分级**：标「实测」的输出，来自 Ch 6 同环境与 `code/06_elemwise/fused_3in.py` 的真实产物。标「预期行为」「理论如此」的，是依 Inductor 设计与公开源码推断、未在本机逐步验证的部分。自己跑一遍，永远比信这段文字可靠。

## 为什么需要这个？

在 Ch 6 里我们看到，`relu(x @ y + 1.0)` 编出来是一个叫 `cpp_fused_add_relu_0` 的函数。名字里的 `fused` 就是这个世界的核心词。

先算清代价。取三个 256 元素的 `float32` 向量，算 `relu(x*2 + y*3 - z)`。每个向量 1 KB。

不融合时，PyTorch eager 逐算子执行：

| 步骤 | 读 | 写 |
|---|---|---|
| `x * 2` | 1 KB | 1 KB |
| `y * 3` | 1 KB | 1 KB |
| `+` | 2 KB | 1 KB |
| `- z` | 2 KB | 1 KB |
| `relu` | 1 KB | 1 KB |
| 合计 | 7 KB | 5 KB |

12 KB 流量、5 次 kernel 调用。融合之后：

| 步骤 | 读 | 写 |
|---|---|---|
| 一个循环体 | 3 KB | 1 KB |

4 KB 流量、1 次 kernel 调用。

**这不是「快了三倍」的承诺**——12/4 只是流量比，实际时间还受 launch 开销、cache 命中、频率影响。但方向是确定的。算一下算术强度（arithmetic intensity，每字节搬运对应多少次浮点运算）：融合版本搬运 4096 字节，做 2×256 次乘加与 256 次 `clamp`，合计约 768 次运算，也就是 768 / 4096 ≈ 0.19 次运算每字节。对比 Ch 8 会讲的矩阵乘（每块数据被复用几十次），elementwise 的运算量相对搬运量小得多，**时间主要花在搬数据上**。搬得少，就是这里唯一有效的优化方向。

于是图级优化的目标可以写成两条：

1. 减少内存往返：让中间结果不落内存
2. 减少 kernel 启动：让更多算子进同一个循环体

## 先建立直觉

把图级优化想成流水线改布局。

```
不融合（逐算子执行）：

  x ─▶[乘 2]─▶ 写回 A ─▶[读 A][加 y*3]─▶ 写回 B ─▶[读 B][减 z]─▶ 写回 C ─▶[读 C][relu]─▶ 输出
        ▲ 每个方括号是一个 kernel，中间结果都要落一次内存

融合（Inductor）：

  x ─┐
  y ─┼─▶ 一个循环体：三次 load → 算 → clamp → 一次 store
  z ─┘
        ▲ 中间值只活在向量寄存器里，从不落内存
```

流水线的比方有一个限制，值得先说清：**不是所有工位都能合并**。合并要求上游工位的产出只被下游这一个工位消费，而且两边的「节拍」（形状、索引方式）对得上。这就是后面「融合合法性」的全部内容。

## 底层机制一：算子融合

### 融合在图上的样子

FX Graph 是一张有向无环图（DAG），节点是算子，边是 tensor。融合就是**把一条链上的多个节点标成同一组（fusion group）**，调度器再为这一组生成一个循环嵌套（loop nest）。

```
        ┌─────────┐     ┌─────────┐     ┌─────────┐
   x ──▶│  mul 2  │────▶│   add   │────▶│  clamp  │──▶ out
        └─────────┘     └────▲────┘     └─────────┘
                             │
   y ──▶┌─────────┐          │
        │  mul 3  │──────────┘
        └─────────┘

   融合判定的问题：这四个节点能不能并成一组？
   条件：中间值没有别的消费者、没有归约边、索引映射兼容
```

合法性条件，按重要性排：

1. **单一消费者**：`mul 2` 的结果只被 `add` 用。如果还有别的节点读它，就得留一份物化结果
2. **不是归约边界**：`sum` / `max` 这类要遍历完整个维度才能出结果的算子，会把循环切开
3. **索引映射兼容**：广播（broadcast）、转置、切片都要求两边能写出同一个循环变量到各自地址的映射
4. **不跨越外部 kernel**：`matmul`、`conv` 往往交给外部库（BLAS / oneDNN），编译器不往里塞代码

### 融合的三种类型

| 类型 | 典型形态 | 合法性条件 | 省掉什么 |
|---|---|---|---|
| elementwise 融合 | `add` + `mul` + `relu` | 单一消费者、索引映射一致 | 中间 tensor 的写回与重读 |
| reduction 融合 | `sum(dim=-1)` 之后接 `/ n` | 逐元素部分可以挂在归约的收尾阶段 | 归约结果的一次落盘 |
| matmul 后融合（epilogue） | `mm` + `bias` + `relu` | 后端 matmul 模板支持 epilogue | 矩阵结果的一次写回与重读 |

前两类是 Inductor 调度器的常规工作。第三类要看后端能力，**这一点是很多人搞混的地方**，下面用两个例子分开说：一个能确证的部分，一个需要你自己跑一遍才能确认的部分。

### 实测一：`x @ y + 1.0` 只融合了后半段

`code/03_inductor/demo.py` 里的函数是 `relu(x @ y + 1.0)`。Ch 6 跑出来的 kernel 名是：

```
cpp_fused_add_relu_0
```

拆开看：

- `cpp`：C++ 后端
- `fused`：融合过
- `add_relu`：融合的是 `add` 和 `relu` 两个算子
- `_0`：第 0 个 kernel

**注意 `matmul` 不在名字里。**名字只覆盖 `add` 与 `relu`，说明那一次矩阵乘没有被塞进这个循环体。至于它去了哪，看产物最直接：

```bash
# 数一下这个图生成了几个 C++ kernel，以及有没有 extern kernel 调用
grep -cE 'async_compile\.cpp_pybinding' cache/*/*.py
grep -oE 'extern_kernels\.[a-z_]+' cache/*/*.py | sort -u
```

预期行为（按 Inductor CPU 的常规实现推断，本章未逐步验证）：会看到一个 `extern_kernels` 调用（矩阵乘交给外部库），加上一个 `cpp_fused_add_relu_0`。也就是「两个阶段」而不是「一个 kernel 包办全部」：

```
阶段 1：extern kernel（矩阵乘，交给外部库）
阶段 2：cpp_fused_add_relu_0（读乘完的结果，加 1，clamp，写出）
```

上方这段是**示意**，不是某次运行的原始输出。要确认在你的环境里到底几个 kernel，跑上面那两条 `grep`，别信这段文字。

如果确认是两段，那它就是「融合不跨外部 kernel」的一个例证。GPU 的 Triton 模板路线可以在 matmul 的 epilogue 里做 `bias + relu`，形态不同，本章不展开。

### 实测二：三输入融合，三读一写

`code/06_elemwise/fused_3in.py` 是 `def f(x, y, z): return torch.relu(x*2 + y*3 - z)`，输入是三个 256 元素向量。数值对比实测输出：

```
diff: 0.0
```

生成的 kernel（原文节选，函数名与签名都照抄）：

```cpp
cpp_fused_add_mul_relu_sub_0 = async_compile.cpp_pybinding(['const float*', 'const float*', 'const float*', 'float*'], '''
#include <torch/csrc/inductor/cpp_prefix.h>
extern "C"  void  kernel(const float* in_ptr0,
                       const float* in_ptr1,
                       const float* in_ptr2,
                       float* out_ptr0)
{
    {
        for(int64_t x0=static_cast<int64_t>(0L); x0<static_cast<int64_t>(256L); x0+=static_cast<int64_t>(8L))
        {
            {
                if(C10_LIKELY(x0 >= static_cast<int64_t>(0) && x0 < static_cast<int64_t>(256L)))
                {
                    auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                    auto tmp4 = at::vec::Vectorized<float>::loadu(in_ptr1 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                    auto tmp9 = at::vec::Vectorized<float>::loadu(in_ptr2 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
                    auto tmp1 = static_cast<float>(2.0);
                    auto tmp2 = at::vec::Vectorized<float>(tmp1);
                    auto tmp3 = tmp0 * tmp2;
                    auto tmp5 = static_cast<float>(3.0);
                    auto tmp6 = at::vec::Vectorized<float>(tmp5);
                    auto tmp7 = tmp4 * tmp6;
                    auto tmp8 = tmp3 + tmp7;
                    auto tmp10 = tmp8 - tmp9;
                    auto tmp11 = at::vec::clamp_min(tmp10, decltype(tmp10)(0));
                    tmp11.store(out_ptr0 + static_cast<int64_t>(x0));
                }
            }
        }
    }
}
''')
```

三个信息点：

1. **签名区分读写**：三个 `const float*` 是输入，一个 `float*` 是输出。`const` 让后端知道这些地址不会被写，可以放心复用寄存器、不必考虑别名
2. **循环体里三次 `loadu(..., 8)`、一次 `tmp11.store(...)`**：中间值 `tmp3`、`tmp7`、`tmp8`、`tmp10` 全是局部变量（向量寄存器），一次都没有落内存
3. **函数名列出全部四个算子**：`add_mul_relu_sub`。名字就是图级决策的日志

顺带看 Python 封装层：

```python
assert_size_stride(arg0_1, (256, ), (1, ))
assert_size_stride(arg1_1, (256, ), (1, ))
assert_size_stride(arg2_1, (256, ), (1, ))
buf0 = empty_strided_cpu((256, ), (1, ), torch.float32)
```

`empty_strided_cpu` 说明输出布局也是编译期定的；`assert_size_stride` 说明形状和步长都进了运行期断言（guard），这部分在本章最后一节展开。

### 不融合的代价，用字节数说

把上面的例子放大到 1024 元素（每向量 4 KB）：

```
不融合：读 28 KB，写 20 KB，5 次 launch
融合：  读 12 KB，写  4 KB，1 次 launch
```

在 DRAM 带宽 20 GB/s 的嵌入式 SoC 上，48 KB 对 16 KB 的差别大约是 2.4 微秒对 0.8 微秒——这个数字依赖带宽假设，**理论估算，不是实测**。但比例关系成立：融合在这里省下的主要不是计算，是搬运。

### 边界：什么时候融合不成立

三个真实会挡住融合的形态：

```python
# 1. 中间值有第二个消费者：无法只算一次
t = x * 2
a = t + 1
b = t - 1        # t 有两个消费者，t 必须物化

# 2. 归约把循环切开
s = (x * 2).sum()     # mul 可以融进归约的读入阶段
                      # 但 sum 的结果要等整个维度走完

# 3. 数据依赖：shape 由运行期值决定
if x.sum() > 0:       # .item() 式的取值会触发图断（graph break）
    y = x + 1
```

第 3 种不是「融合失败」，是**图根本断成两段**，两段之间回到 eager。判断方法在 Ch 6 说过：看 `TORCH_LOGS=graph_breaks`，别猜。

## 底层机制二：常量折叠

### 权重为什么是常量

Ch 3 讲过，`ExportedProgram` 除了 `graph` 还带 `graph_signature` 和 `state_dict`。`graph_signature` 的 `input_specs` 里，权重被标成 `InputKind.PARAMETER`，普通输入是 `InputKind.USER_INPUT`。

Inductor 拿到这张图时，`PARAMETER` 对应的节点会变成 `get_attr`，而 `get_attr` 的值在编译期是已知的。**「已知」是折叠的前提。**

```
nn.Module
   │  state_dict（编译期已知的 tensor）
   ▼
FX Graph
   │  get_attr 节点 + 普通 placeholder 节点
   ▼
常量折叠：把只依赖 get_attr 与字面量的子图，在编译期算掉
   │
   ▼
更小的图 + 预打包（prepack）的常量
```

### 折叠的三类对象

| 对象 | 例子 | 收益 |
|---|---|---|
| 纯标量运算 | `2.0 * 3.0`、`1/255.0` | 免掉运行期一次乘法 |
| 形状运算 | `s0 // 8`、`view` 链推导 | 循环边界变成字面量，向量化与展开才可能 |
| 权重预处理 | `linear` 的权重转置、量化打包 | 每次推理都省一次大 tensor 变换 |

第三类最值钱。`nn.Linear` 在数学上是 `x @ W.T + b`，eager 下有的路径每次都要处理转置；编译器可以在编译期把 `W` 存成已经转置好的布局（预期行为，具体是否发生取决于后端与算子选择）。

### 折叠的边界条件

```python
class M(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.randn(64, 64))

    def forward(self, x, extra):
        return x @ self.w + extra * 0    # extra * 0 能折成 0 吗？
```

`extra * 0` 折成 `0` 在数学上成立，但如果 `extra` 是 `inf` 或 `nan`，`inf * 0 = nan`。**浮点常量折叠必须保留这个语义**，所以编译器要么不折，要么只在能证明有限性时折。你在生成代码里看到「本该折掉却没折」的乘法，先怀疑这条规则，别怀疑编译器坏了。

另一条边界是**常量身份**。同样是权重，写成 `forward(self, x, w)` 的参数就变成 `USER_INPUT`，编译器无权折叠，也不能把它放进 `.rodata`。Ch 3 的踩雷第 3 条说的就是这件事。

### 折叠的代价：二进制体积

权重折进代码，意味着它出现在 `.rodata`（只读数据段）。对一个 7B 模型，那是 14 GB 的 `float16`——这条路走不通。工程上的分界是：

- **小常量**（标量、shape、量化 scale/zero-point）：折
- **大权重**：不折进代码，作为独立数据段或文件，运行时按指针传入

在 MCU 上这条线更紧：Flash 512 KB 的板子，权重本身就占掉大部分，`.rodata` 里再放一份副本直接装不下。Ch 11（量化）与 Ch 22（内存规划）会接着这条线走。

## 底层机制三：layout 变换

### transpose 不搬数据，代价在下游

`x.t()` 在 PyTorch 里是**视图（view）**：只改 stride，不动内存。

```python
x = torch.randn(32, 8)      # shape (32, 8), stride (8, 1), 连续
y = x.t()                   # shape (8, 32), stride (1, 8), 非连续
```

`y` 和 `x` 指向同一块内存。真正的选择发生在**下一个算子**：

```
路线 A：先 materialize 成连续布局
  x(32,8) ──[copy kernel]──▶ y_contig(8,32) ──[elementwise]──▶ out
             多一次 1 KB 读 + 1 KB 写

路线 B：就地按 stride 算
  x(32,8) ──[elementwise，按 stride=(1,8) 索引]──▶ out
             不搬数据，但每次 load 都不连续
```

两条路线都不免费：

- A 多一轮内存往返，但之后每个 kernel 都能用 `loadu` 连续读
- B 省掉拷贝，但向量化会退化——`Vectorized<float>::loadu` 要求连续 8 个 float，跨行取数只能退回标量循环

**判断哪种更好，取决于下游有几个消费者**：只有一个 elementwise 消费者时，B 往往赢；下游是 `matmul`（要求特定布局）时，A 几乎是必须的。这类决定在 Inductor 里叫 layout 优化，配置项在 `torch/_inductor/config.py` 的 `layout_optimization` 一带。

### 实测：stride 进了 guard

Dynamo 的 guard 会把形状和步长都记下来。实测中 guard 表达式里出现：

```
stride=[8, 1]
```

含义是：这个二维 tensor 的行步长是 8、列步长是 1，也就是 `(N, 8)` 的行主序连续布局。同一个 `forward`，如果传进来一个 `transpose` 过的视图（形状变 `(8, N)`、stride 变 `(1, 8)`），guard 不匹配，**触发重编译**。

这不是缺陷，是正确性要求：生成的循环把「第 i 行第 j 列在 `base + i*8 + j`」硬编码进了地址计算，换布局就必须换代码。

推论有三条：

1. 训练/推理脚本里反复 `transpose`、`permute`、`narrow` 再喂给同一个编译函数，可能反复重编译
2. `torch.compile` 的缓存命中率，和输入布局的稳定性直接相关
3. 边缘部署时把输入固定成一种布局（NHWC 或 NCHW，选一个），能省掉一整类重编译

> 动态 shape（Ch 5）放宽的是**尺寸**，不是**布局**。`dynamic=True` 让 `s0` 变成符号，但 stride 关系仍然是特化的。这两件事经常被混为一谈。

## 对比例子：三份代码，一个语义

同样算 `relu(x*2 + y*3 - z)`，三种写法在「融合」这件事上的差别：

| 写法 | 生成的 kernel 数 | 说明 |
|---|---|---|
| 分别调用 5 个 op | 5 | 每个中间结果落内存 |
| `torch.compile` 包住整段 | 1 | 三读一写，实测为 `diff: 0.0` |
| 手写 C++ 循环 + `Vectorized` | 1 | 与第二行等价，但要自己写边界处理 |

第二行和第三行**生成的机器码可以一样**。这就是 AI 编译器的定位：它不发明新的算法，它把你的算子序列改写成「本来要专家手写才有的循环」。

## 踩雷集锦

1. **以为融合越多越好**：融合的收益是省流量，成本是**寄存器压力**。AVX2 只有 16 个 256 位向量寄存器，每个装 8 个 `float`，合计 128 个 `float` 的寄存器空间。一个融合了 20 个中间值的 kernel，活跃值超过这个数就会溢出（spill）到栈上——而栈也在内存里，省下的往返又还回去了。看到 `vmovups` 往 `rsp` 附近存取，就是 spill。
2. **以为融合一定不改变浮点结果**：逐元素融合**保持**算子间的依赖顺序，`(a+b)+c` 还是 `(a+b)+c`。但跨归约的融合会改变**结合顺序**：把一个 `sum` 拆成多段部分和再相加，`(a+b)+(c+d)` 与 `((a+b)+c)+d` 的舍入结果不同。FMA 收缩（`a*b+c` 合成一条指令，中间少一次舍入）也会改结果。这两件事都会让「融合前后逐位相同」的预期落空，而 elementwise 融合本身不会。
3. **以为权重一定会被折叠**：只有当它在 `graph_signature` 里是 `PARAMETER` / `get_attr` 时才折。写成 `forward` 参数的权重是 `USER_INPUT`，编译期未知，折不了。
4. **以为 `transpose` 是免费的**：视图本身不搬数据，但下游要么付一次拷贝，要么付非连续访问的代价。而且布局进了 guard，换布局就重编译。
5. **以为融合能跨归约、跨外部 kernel**：`sum` 之后接 `div` 能融合（`div` 挂在归约收尾），但两个 `sum` 之间通常不能；`matmul` 走外部库时，epilogue 能不能融合取决于那个库/模板，不取决于 Inductor 想不想。

## 本章重点整理

- 图级优化的两条目标：减少内存往返、减少 kernel 启动。elementwise 场景下前者是主要收益。
- 融合合法性四条件：单一消费者、非归约边界、索引映射兼容、不跨外部 kernel。
- 三类融合：elementwise、reduction、matmul epilogue。第三类受后端能力限制，CPU 的 extern matmul 不给 epilogue 机会。
- `cpp_fused_add_mul_relu_sub_0` 这样的函数名，是图级决策的可读日志；签名里的 `const` 与指针数量同样是信息。
- 常量折叠的前提是「编译期已知」：`state_dict` 权重可以折，`forward` 参数不行。大权重不能折进代码，只能作为数据段。
- layout 变换的成本不在 `transpose` 本身，而在下游：拷贝 or 非连续访问，二选一。stride 进 guard，布局变了就重编译。

## 自我检核

- [ ] 不看笔记，说出融合的四个合法性条件，并各举一个违反它的例子
- [ ] 给你一个 kernel 名 `cpp_fused_add_mul_relu_sub_0`，你能说出图里至少有哪些算子、以及哪个算子**不在**里面
- [ ] 解释为什么 `x @ y + 1.0` 在这条 CPU 路径上是两个 kernel 而不是一个
- [ ] 用字节数估算「5 个 elementwise 算子融合成 1 个」省下多少流量，并说明这个估算忽略了什么
- [ ] 说出常量折叠的三类对象，以及「大权重不折」的两个理由（一个关于语义，一个关于体积）
- [ ] 解释 `stride=[8, 1]` 为什么会导致重编译，以及边缘部署时怎么减少这类重编译

## 延伸阅读

- **[torch/_inductor/scheduler.py](https://github.com/pytorch/pytorch/blob/main/torch/_inductor/scheduler.py)**
  - **读哪里**：`Scheduler` 类里构造融合组的部分（搜 `fuse`、`can_fuse`）
  - **学什么**：本章「融合合法性四条件」的代码形态；哪些是硬约束、哪些是启发式打分
  - **关联**：读完再回看 `cpp_fused_add_mul_relu_sub_0`，能指出每个 `tmp` 来自哪个 FX 节点
  - **前提**：读过 Ch 6，知道 FX Graph 与 GraphLowering 的关系

- **[torch/_inductor/config.py](https://github.com/pytorch/pytorch/blob/main/torch/_inductor/config.py)**
  - **读哪里**：`layout_optimization` 与 `cpp` 段下的相关开关
  - **学什么**：哪些图级决策可以单独关掉，关掉后产物怎么变——这是验证「默认值为什么是这个」的最快路径
  - **关联**：本章 layout 一节里「拷贝 or 非连续访问」的选择，开关就在这里
  - **前提**：能在本机能跑通 Ch 6 的 demo，否则改了配置也看不出差别

- **[TASO: Optimizing Deep Learning Computation with Automatic Generation of Graph Substitutions](https://dl.acm.org/doi/10.1145/3341301.3359630)** — Jia et al., SOSP 2019
  - **读哪里**：Section 3（graph substitution 的形式化）与 Section 5（evaluation）
  - **学什么**：把图改写规则形式化成可自动验证的替换，解决「手写规则互相冲突、无法证明等价」的问题
  - **关联**：本章的融合是其中一类替换；这篇解释了为什么生产编译器需要「规则 + 验证」而不是一堆 if
  - **前提**：能读懂一阶逻辑表达式；不熟的话先看 Section 2 的例子再回读

- **[MLIR Linalg Dialect](https://mlir.llvm.org/docs/Dialects/Linalg/)**
  - **读哪里**：elementwise / reduction 的 iterator type 说明，以及 fusion 相关段落
  - **学什么**：融合如何用 iterator 与 indexing map 表达；比 FX 的「节点合并」形式化得多
  - **关联**：Ch 10 会把同一条链降到 `linalg.generic`，这里是提前对表
  - **前提**：知道 SSA 与 affine 映射的基本概念，否则 iterator type 那段读不下去

- **《Computer Systems: A Programmer's Perspective》** — Bryant & O'Hallaron（第 3 版，2015）
  - **读哪里**：第 6 章（存储器层次结构），特别是 locality 与 cache line 两节
  - **学什么**：cache line 是 64 字节、一次 miss 拉多少数据——「省一次写回值多少」要靠这个换算
  - **关联**：本章「不融合的代价，用字节数说」那段的估算依据
  - **前提**：无；第 6 章自成一体，不需要先读前五章

融合把「几个 kernel」压到「一个 kernel」，但没有回答「这个 kernel 内部的循环怎么排」。256 个元素的 elementwise 没什么可排的；一旦是矩阵乘或归约，循环顺序、分块大小、向量宽度就决定了一切。

→ [Ch 8 调度：为什么融合之后还要 tiling](./08-scheduling.md)
