# Ch 5 Lowering 规则：把 aten 算子翻译成 IR

> **目标**：读完本章，你能写一个正确的 `register_lowering`；能解释 `type_promotion_kind`、`broadcast`、`convert_input_to_bool` 三个开关分别在做什么；能完整复述"没注册 lowering"时的回退链路，并说清为什么 fallback 会把调用留在 wrapper 里；能用 lab_c 的两份生成代码证明"翻译成 IR"与"实现算子"是两件事。

> **环境**：Windows 10 x86_64；Python 3.11.12（`.scratch/aic-env`）；`torch 2.14.1+cpu`；MSVC 14.43（`cl.exe`）。本章命令在仓库根目录执行：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py
> ```

## 为什么

第 4 章把 FX 图变成了 buffer 与循环。可中间那一步——"一个 aten 节点具体变成哪几条 `ops.load`/`ops.mul`/`ops.store`"——才是编译器真正做翻译的地方。

这里有个容易走偏的心智模型：把 lowering 理解成"给算子写一个实现"。如果只是这样，那和 eager 的 CPU kernel 没区别，写出来的东西照样是一次读、一次算、一次写，调度器拿不到任何可以融合的结构。

Inductor 的 lowering 实际做的是**翻译**：把算子拆成 IR 里已有的基本操作，让它们的读写范围、迭代空间、下标映射都暴露出来。翻译得越彻底，调度器越有东西可优化；翻译得不彻底（或者干脆没翻译），这个算子就变成一块黑盒，只能整块调用。

本章用一个自定义算子 `mylab.affine(x, a, b) = x * a + b` 做对照：同一个数学，一份不注册 lowering，一份注册。差异不在"算得对不对"，而在"生成代码长什么样"。

## 主线

### 5.1 `register_lowering` 到底包了什么

公开 API 是个装饰器工厂，真正干活的是 `_register_lowering`：

```python
# torch/_inductor/lowering.py（节选，保留关键分支）
def _register_lowering(aten_fn, decomp_fn, broadcast, type_promotion_kind,
                       convert_input_to_bool, lowering_dict):
    @functools.wraps(decomp_fn)
    def wrapped(*args, **kwargs):
        args = list(args)
        ...
        if not all((fn in fallbacks or in_namespace(fn, "_c10d_functional")) for fn in aten_fn):
            if any(x == "out" for x in kwargs):
                raise AssertionError("out= ops aren't yet supported")

        args, kwargs = transform_args(
            args, kwargs, broadcast, type_promotion_kind, convert_input_to_bool,
        )
        out = decomp_fn(*args, **kwargs)
        validate_ir(out)
        return out

    aten_fn = get_overloads(aten_fn)
    lowering_dict.update(dict.fromkeys(aten_fn, wrapped))
    return wrapped
```

四个动作值得逐个说：

1. **展开单参数列表**。`aten.cat` 这类算子的参数是一个 list，包装器把它摊平后再交给 `decomp_fn`，回来时再包回去。
2. **拒绝 `out=`**。Inductor 的 IR 是函数式的，"写到调用者给的张量里"这种语义没有对应表示。
3. **`transform_args`**：按开关做类型提升与广播，这是 5.2 的主题。
4. **`validate_ir`**：检查返回值是不是合法的顶层 IR 节点。返回 `TensorBox`、`ExpandView`、`sympy` 表达式、`int` 都行；返回一个 Python `float` 会直接抛 `AssertionError: Found <class 'float'>, which is not a supported top level IR node`。

最后 `get_overloads(aten_fn)` 把 `aten.add` 这种 packet 展开成 `aten.add.Tensor`、`aten.add.Scalar` 等全部重载，一次性登记。所以注册时传 packet 还是具体 overload，覆盖范围不同。

还有一个容易忽略的点：`register_lowering` 的默认 `lowering_dict` 是模块级 `lowerings` 字典，也就是**进程级全局状态**。注册一次，之后所有图都受影响；反过来，已经编译并缓存过的图不会因为你后来注册了 lowering 而失效。

### 5.2 类型提升与广播：`transform_args` 的两个开关

`type_promotion_kind` 的默认值是 `ELEMENTWISE_TYPE_PROMOTION_KIND.DEFAULT`，可选值一共六个：

| 取值 | 语义 |
| --- | --- |
| `DEFAULT` | 按 PyTorch 的逐元素提升规则算公共 dtype |
| `NO_OPMATH` | 跳过 opmath 提升（低精度不被中途升到 fp32） |
| `INT_TO_FLOAT` | 整数输入按浮点参与运算 |
| `ALWAYS_BOOL` | 输出固定为 bool |
| `COMPLEX_TO_FLOAT` | 复数输入按实数处理 |
| `BOOL_TO_LONG` | bool 输入按 long 处理 |

本机 `lowering.py` 里能直接搜到的用法是 `INT_TO_FLOAT`：`div` 的注册是 `[aten.true_divide, aten.div.Tensor]` 且带 `type_promotion_kind=INT_TO_FLOAT`。其余取值在你使用的版本里先搜一遍确认真实用到，再决定是否套用；枚举名表达的是规则，不代表当前版本确实拿它注册过 lowering。

传 `None` 表示**完全不做类型提升**：参数原样交给 `decomp_fn`。实验 C 的自定义 lowering 就用了 `type_promotion_kind=None`：

```python
@register_lowering(torch.ops.mylab.affine, type_promotion_kind=None)
def _lower_affine(x, a, b):
    return L.add(L.mul(x, a), b)
```

`a`、`b` 是 Python 浮点常量，`L.mul(x, a)` 内部会通过 `promote_constants` 把标量变成与 `x` 同 dtype 的 `ir.Constant`，再广播到 `x` 的形状。如果这里强行开 `DEFAULT` 提升，包装器会先算一遍"公共 dtype"，对纯常量参数没有意义，还可能把标量转成张量常量，白费一次转换。

`broadcast=True` 则是另一件事：包装器调用 `broadcast_tensors(*tensor_inputs)`，把所有张量参数扩到同一个形状，常量参数用 `ExpandView.create` 跟上。扩展本身不复制数据，只是在 IR 里记一层下标映射；真正的下标计算发生在 codegen。

形状推导在 `broadcast_symbolic_shapes` 里，规则和 NumPy 一致，但对符号形状有两个额外处理：

```python
def broadcast_symbolic_shapes(a, b):
    b = tuple(b)
    if not a or a == b:
        return b
    output = []
    for x, y in itertools.zip_longest(reversed(a), reversed(b), fillvalue=sympy.S.One):
        if V.graph.sizevars.is_size_one_or_false(y):
            output.append(x)
        elif V.graph.sizevars.is_size_one_or_false(x):
            output.append(y)
        else:
            V.graph.sizevars.check_equals(x, y)
            ...
```

- 遇到 1 就取另一边，这是常规广播；
- 两个都非 1 时不是"取大的"，而是 `check_equals`：**要求可证明相等**，证不出来就报错。符号形状下"看起来一样"不够，必须能代数化简到同一个式子；
- 选中哪一边也有讲究：注释写着 `prefer shorter formula`，优先保留自由符号更少的那条表达式，避免下标里堆出一长串乘加。

### 5.3 没注册 lowering 时发生了什么

这是本章最该记牢的链路。`GraphLowering.call_function` 在 `target not in lowerings` 时：

```python
base_name = target.name().split(".")[0]
if base_name in FALLBACK_ALLOW_LIST:
    make_fallback(target, warn=False, ...)
elif config.implicit_fallbacks:
    ...
    make_fallback(target, layout_constraint=decided_constraint, ...)
```

`config.implicit_fallbacks` 本机默认 `True`，所以自定义算子会走进 `make_fallback`。而 `make_fallback` 的主体是：

```python
def register_fallback(op_overload):
    add_needs_realized_inputs(op_overload)
    if layout_constraint is not None:
        add_layout_constraint(op_overload, layout_constraint)
    return register_lowering(op_overload, type_promotion_kind=None)(
        fallback_handler(op_overload)
    )
```

三个后果，一条比一条重要：

1. **`add_needs_realized_inputs`**：这个算子的输入必须先是"已物化"的 buffer。也就是说，前一个算子的结果不能停在 `Pointwise` 里等融合，必须落成 `ComputedBuffer`。融合边界在注册的那一刻就被画出来了。
2. **`fallback_handler`** 把调用包成 `ir.FallbackKernel.create(...)`，并把这个 op 记进 `fallbacks` 集合。`FallbackKernel` 在调度器眼里是 extern 节点，读写依赖只能是星号依赖（第 4 章的 `StarDep`）。
3. **`register_lowering(..., type_promotion_kind=None)`**：fallback 不做任何参数变换，原样把参数交给 `FallbackKernel`。

于是 IR 里出现的是一个不透明节点，codegen 时它被翻译成 wrapper 里的一次真实调用。实测（lab_c 的 fallback 分支）：

```python
buf0 = torch.ops.mylab.affine_naive.default(arg0_1, 2.0, 1.0)
assert_tensor_metadata(buf0, (256, 256), (256, 1), torch.float32, 'torch.ops.mylab.affine_naive.default')
```

调用对象是 **eager 的 CPU 实现**（`torch.library.impl` 注册的那个 `_impl`）。它算得对，但它是黑盒：`relu` 只能在它之后单独起一个 kernel，因为 `buf0` 必须已经存在。

这条链路还解释了另一个现象：为什么 fallback 的数值误差实测是 `0.000e+00`。因为它走的就是 eager 路径，逐元素完全一致。

### 5.4 对照读两份生成代码

同一份数学 `y = relu(x * 2 + 1)`，注册 lowering 之后的 kernel 循环体（实测节选）：

```text
for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(65536LL); x0+=static_cast<int64_t>(8LL))
auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
auto tmp1 = static_cast<float>(2.0);
auto tmp2 = at::vec::Vectorized<float>(tmp1);
auto tmp3 = tmp0 * tmp2;
auto tmp4 = static_cast<float>(1.0);
auto tmp5 = at::vec::Vectorized<float>(tmp4);
auto tmp6 = tmp3 + tmp5;
auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0));
```

八条临时变量，正好对应一次 `load`、两次常量广播、一次 `mul`、一次 `add`、一次 `relu`（`clamp_min` 是 relu 的代码形态）。整段在**一个** loop nest 里，`x0 += 8` 说明按 AVX2 的 8 个 float 向量化。

对比两份产物的结构差异：

| 维度 | 未注册 lowering | 注册 lowering |
| --- | --- | --- |
| wrapper 里的调用 | `torch.ops.mylab.affine_naive.default(...)` | 无自定义算子 |
| 算子如何执行 | eager CPU kernel | 展开成 IR 的 `mul`/`add` |
| 生成的 C++ kernel 数 | 1（只有 relu）+ 1 次 eager 调用 | 1（融合） |
| 中间 buffer | 必须物化 `buf0` | 无中间 buffer |
| 向量化机会 | 各自独立 | 整条链一起向量化 |
| 数值最大误差 | 0（eager 路径） | 0（同一表达式） |

注意最后一行：**性能差异与数值正确性无关**。两种写法都算对，差别只在结构。所以判断一个 lowering 是否"值得写"，要看它能不能消除黑盒、把计算并进邻居的 loop nest，而不是看数值。

### 5.5 写 lowering 的检查清单

按顺序过一遍，能挡掉大多数返工：

1. **返回值必须是 IR 节点**。要用 `L.*` 里的组合子（`L.mul`、`L.add`、`L.relu`、`make_pointwise`）拼，不要自己算 Python 数值。
2. **不要 `realize()`**。中间结果一旦物化，融合边界立刻出现。只有语义要求"必须先落到内存"时才考虑。
3. **处理 kwargs 与可选参数**。`alpha`、`dim`、`keepdim` 这些如果不接住，包装器会把它们原样塞给 `decomp_fn`，直接 `TypeError`。
4. **dtype/device 从输入推**。不要硬编码 `torch.float32` 或 `"cpu"`；要跟随 `x.get_dtype()` / `x.get_device()`，否则混合精度或跨设备会静默出错。
5. **`broadcast` 只在确实需要时开**。开了会额外做形状推算与 `ExpandView`；对本来形状一致的算子只是多一层包装。
6. **`type_promotion_kind` 与算子语义对齐**。比较类用 `ALWAYS_BOOL`，纯浮点自定义算子常用 `None`。
7. **注册时机早于首次编译**。模块导入时注册，别等到图已经编译过再补。
8. **有 decomposition 时优先用 decomposition**。`make_fallback` 里有一条硬检查：同一个 op 既有 decomposition 又有 fallback 会直接 `AssertionError`。这条约束在提醒你，图级替换比 lowering 层兜底更靠前、更便宜。

### 5.6 什么时候不该写 lowering

三种情况，写了反而更糟：

- **算子有副作用或状态**（原地写、随机数、跨设备同步）。IR 是函数式的，硬塞进来会破坏依赖分析。
- **有现成的 decomposition**。图级拆解之后，每一小步都能被已有 lowering 处理，融合机会更多。自己写一个整体 lowering，等于把拆解机会藏起来。
- **后端已经有成熟库**。矩阵乘走 `extern_kernels.addmm`（oneDNN/MKL）通常比生成的 C++ 循环快；把它翻译成 IR 再让 Inductor 生成三重循环，是拿短板去碰长板。第 17 章会讲 extern kernel 的边界。

## 可跑实验

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py
```

本机实测输出（节选，首次编译约 10–20 秒）：

```text
[compile] fallback: 1 module(s)
[compile] lowering: 1 module(s)

========================================================================
1) 未注册 lowering：调用留在 wrapper 里，无法融合
========================================================================
  buf0 = torch.ops.mylab.affine_naive.default(arg0_1, 2.0, 1.0)
  assert_tensor_metadata(buf0, (256, 256), (256, 1), torch.float32, 'torch.ops.mylab.affine_naive.default')

========================================================================
2) 注册 lowering：算子展开成 IR，与 relu 同 kernel
========================================================================
  for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(65536LL); x0+=static_cast<int64_t>(8LL))
  auto tmp0 = at::vec::Vectorized<float>::loadu(...)
  ...
  auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0))

========================================================================
3) 断言
========================================================================
[PASS] fallback 数值  (max_abs_err=0.000e+00 tol=1e-05)
[PASS] lowering 数值  (max_abs_err=0.000e+00 tol=1e-05)
[PASS] fallback 保留自定义算子调用
[PASS] lowering 后 wrapper 不再调用自定义算子
[PASS] lowering 后只有一个 kernel  (1)
[PASS] affine 的乘法/加法进入 kernel  (tmp_ops=8)
```

观察顺序建议：

1. 先看第 1 节，确认 fallback 的调用**字面**出现在 wrapper 里；
2. 再看第 2 节的循环，数一数 `auto tmp` 有几条，和 5.4 的八条对上；
3. 最后看第 3 节，注意 `lowering 后只有一个 kernel  (1)` 与 `tmp_ops=8` 是两个独立断言——kernel 数说明融合发生，指令数说明翻译彻底。

想进一步确认因果，可以把 `register_lowering` 那段注释掉再跑一次，第 2 节会退回 fallback 形态；也可以在 lowering 里故意 `return 0.0`，观察 `validate_ir` 抛出的错误信息。

## 源码落点

| 文件 | 符号 | 读什么 |
| --- | --- | --- |
| `torch/_inductor/lowering.py` | `_register_lowering` | 包装器四个动作：摊平 list、拒绝 `out=`、`transform_args`、`validate_ir` |
| `torch/_inductor/lowering.py` | `register_lowering` | 装饰器签名与 `lowering_dict` 默认值（全局 `lowerings`） |
| `torch/_inductor/lowering.py` | `transform_args` | 类型提升与广播的分支；`promote` 只对 `TensorBox`/`ir.Constant` 生效 |
| `torch/_inductor/lowering.py` | `broadcast_symbolic_shapes` | 1 的处理、`check_equals`、`prefer shorter formula` |
| `torch/_inductor/lowering.py` | `make_fallback` / `fallback_handler` | `add_needs_realized_inputs`、`FallbackKernel.create`、`fallbacks.add` |
| `torch/_inductor/lowering.py` | `make_pointwise` | 自己写逐元素 lowering 时最常用的组合子 |
| `torch/_inductor/graph.py` | `call_function` | 分派顺序与 `implicit_fallbacks` 分支 |
| `torch/_inductor/ir.py` | `validate_ir` | 哪些类型算合法顶层节点 |
| `torch/_prims_common` | `ELEMENTWISE_TYPE_PROMOTION_KIND` | 六个取值的准确定义 |

## 常见坑

1. **注册太晚。** `torch.compile` 是惰性的，缓存的产物不会因为你后补了 lowering 而失效。实验脚本里用 `fresh_inductor_cache()` 加 `torch._dynamo.reset()`，就是为了让第二次编译真的重跑。生产环境里改 lowering 之后，记得清缓存（第 19 章）。
2. **以为 `type_promotion_kind=None` 等于"什么都不做"。** 包装器不做提升，但 `promote_constants` 仍会把 Python 标量变成与张量同 dtype 的常量；`None` 的准确含义是"不做统一的 dtype 计算"。
3. **`broadcast=False` 却传了形状不同的张量。** 包装器不广播，`make_pointwise` 里 `inputs[0].get_size()` 就是输出形状，后续读第二个输入时越界或形状错配，报错点通常离真正原因很远。
4. **lowering 返回 Python 数值。** `validate_ir` 会抛 `Found <class 'float'>, which is not a supported top level IR node`。要返回 `ir.Constant` 或 `L.*` 的结果。
5. **自定义算子没写 Meta 实现。** 没有 Meta kernel，AOTAutograd 追踪阶段就拿不到输出元数据，`fullgraph=True` 下会直接失败。lab_c 里 `torch.library.impl(lib, name, "Meta")` 那两行不是装饰。
6. **既写了 decomposition 又写了 fallback。** `make_fallback` 会 `AssertionError: both a fallback and a decomp for same op`。选一条路，别两条都占。
7. **用 fallback 的数值正确性证明 lowering 正确。** 两者都算对才说明语义没错；要证明 lowering 有价值，得看结构（kernel 数、tmp 指令数、是否还有 extern 调用）。

## 自我检查

- [ ] 我能默写 `_register_lowering` 的四个动作，并说出各自防止了哪类错误。
- [ ] 我能解释六个 `ELEMENTWISE_TYPE_PROMOTION_KIND` 的差别，以及 `None` 与 `DEFAULT` 的区别。
- [ ] 我能画出"未注册 lowering → `make_fallback` → `FallbackKernel` → wrapper 调用"的完整链路。
- [ ] 我能说出 `add_needs_realized_inputs` 为什么等于提前画了一条融合边界。
- [ ] 我能对着 lab_c 的两份生成代码，指出"relu 单独成 kernel + 一次 eager 调用"与"单个融合 kernel"的区别，以及 "tmp 0 → 8" 证明了什么。
- [ ] 我知道 `broadcast_symbolic_shapes` 在两个非 1 维度上要求 `check_equals`，而不是取较大值。
- [ ] 我能列出三种不该自己写 lowering 的情况，并给出替代方案。
- [ ] 我知道改完 lowering 之后为什么必须清缓存或换 `fresh_inductor_cache()`。

## 延伸阅读

- **`torch/_inductor/lowering.py` 的 `_register_lowering`、`transform_args`、`make_fallback`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/lowering.py>）：按本章 5.1–5.3 的线索读三个函数，特别注意 `add_needs_realized_inputs` 的位置。
- **`torch.library` 自定义算子文档**（<https://pytorch.org/docs/stable/library.html>）：读 `Library.define`、`impl` 与 Meta 注册；理解为什么没有 Meta 实现就无法在 `fullgraph=True` 下编译。
- **`torch._prims_common.ELEMENTWISE_TYPE_PROMOTION_KIND`**（<https://github.com/pytorch/pytorch/blob/main/torch/_prims_common/__init__.py>）：看六个枚举值各自的注释，对照 5.2 的表格。
- **`torch/_inductor/ir.py` 的 `validate_ir`**（<https://github.com/pytorch/pytorch/blob/main/torch/_inductor/ir.py>）：读合法类型清单，理解"IR 是函数式的"这条约束如何体现在类型检查上。

---

上一章：[GraphLowering 与 IR](./04-graph-lowering-and-ir.md) ｜ 下一章：post-grad pass 管线的真实顺序。

[上一章：GraphLowering 与 IR](./04-graph-lowering-and-ir.md) | [下一章：Post-grad Pass 管线](./06-post-grad-pass-pipeline.md)