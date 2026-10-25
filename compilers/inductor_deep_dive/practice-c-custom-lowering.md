# 练习 C 自定义 lowering 实战：给同一个算子族加第二个 lowering

> **目标**：在 lab_c 的基础上再写两个 lowering，一个处理广播（broadcast），一个处理 `int64`。通过对比生成代码，理解“lowering 把算子翻译成 IR”之后，广播如何变成 stride 0 的标量 load，整型如何改变向量 lane 数，以及为什么结构断言比时间断言更适合这类练习。

> **环境**：Windows 10/11 x86_64；Python 3.11.12；torch 2.14.1+cpu；MSVC 14.43（`cl.exe` 14.43）。命令都在仓库根目录 `D:\Learning-with-Claude` 下执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 <脚本路径>
```

先读 [`code/labs/lab_c_lowering.py`](./code/labs/lab_c_lowering.py)，本章的脚本以它为模板。前一章是 [Ch 17 extern kernel 与模板](./17-extern-kernels-and-templates.md)，下一章是 [Ch 18 产物与 ABI](./18-artifacts-and-abi.md)。

## 为什么

lab_c 只做了一个 lowering：`affine(x, a, b) = x * a + b`。它证明了两件事：

1. 不注册 lowering 时，自定义算子只能留在 wrapper 里，逐 op 走 eager，无法融合。
2. 注册 lowering 后，算子被翻译成 `L.mul`/`L.add`，与 `relu` 进同一个 loop body、同一个 kernel。

但一个 lowering 的练习很容易停在“照抄模板”。真正需要掌握的是：**换一种输入形态，lowering 的翻译结果会怎样改变生成代码**。广播和 `int64` 是两个很好的探针：

- 广播会引入 stride 0 的维度。`CppVecKernel.load` 看到 stride 0 时不会做向量 load，而是标量 load + 延迟广播。生成代码里会出现 `Vectorized<float>(tmp)`，而不是 `loadu`。
- `int64` 会把 `Vectorized<float>` 换成 `VectorizedN<int64_t,2>`。硬件上每个 256 位寄存器放 4 个 int64，但 `TilingSelect` 仍以 float 的 8 作为 tiling_factor，所以一次迭代处理 8 个 int64，由两个向量寄存器拼成 `VectorizedN`。这会改变类型名、常量的构造方式与 `loadu` 的长度。

这两个变化都不是“lowering 里写不写 broadcast”能直接控制的。lowering 只负责把自定义算子翻译成标准 IR 算子；广播与 dtype 的代码形态由 `TilingSelect`、`CppVecKernel`、`CppVecOverrides` 决定。练习的目的就是把这条边界看清楚。

本练习不涉及 autotune，也不要求测时间。所有断言都是结构或数值断言，结果在同一台机器上可复现。

## 主线

### 1. 任务：两个算子，三种对比

在 `code/labs/` 下新建 `practice_c_second_lowering.py`，定义两个自定义算子：

```python
lib.define("affine_bcast(Tensor x, Tensor a, Tensor b) -> Tensor")
lib.define("scale_int64(Tensor x, int k) -> Tensor")
```

语义：

- `affine_bcast(x, a, b) = x * a + b`，三个输入都允许广播。典型 shape 是 `x: (64, 1)`、`a: (1, 64)`、`b: (1, 64)`，输出 `(64, 64)`。
- `scale_int64(x, k) = x * k`，`x` 是 `int64`，`k` 是 Python `int`，输出 `int64`。

需要做三组对比：

| 对比 | 观察点 | 预期结构 |
|---|---|---|
| `affine_bcast` vs eager | 数值正确、wrapper 无自定义调用 | 一个融合 kernel |
| `affine_bcast` 的生成代码 | 广播维如何 load | stride 0 → 标量 load + `Vectorized<float>(...)` |
| `scale_int64` vs eager | 数值正确、dtype 保持 | 一个 kernel，`VectorizedN<int64_t,2>` 或标量 `int64_t` |

不要一开始就追求“广播一定向量化”。`TilingSelect` 会根据连续维选择 tiling；`x: (64, 1)` 在最后一维的 stride 是 0，`a: (1, 64)` 的 stride 是 0 或 1，具体取决于广播后的索引。生成代码可能只向量化 `a` 这一侧，也可能因为 op 数太少而放弃 tiling。练习要求你记录实际结果，而不是猜。

### 2. 先写 op 与 lowering

完整的参考实现放在本章末尾的折叠块里。核心结构如下：

```python
lib = torch.library.Library("mylab2", "DEF")
lib.define("affine_bcast(Tensor x, Tensor a, Tensor b) -> Tensor")
lib.define("scale_int64(Tensor x, int k) -> Tensor")

def _bcast(x, a, b):
    return x * a + b

def _scale(x, k):
    return x * k

torch.library.impl(lib, "affine_bcast", "CPU")(_bcast)
torch.library.impl(lib, "scale_int64", "CPU")(_scale)

torch.library.impl(lib, "affine_bcast", "Meta")(
    lambda x, a, b: torch.empty(
        torch.broadcast_shapes(x.shape, a.shape, b.shape),
        device=x.device, dtype=x.dtype,
    )
)
torch.library.impl(lib, "scale_int64", "Meta")(lambda x, k: torch.empty_like(x))

@register_lowering(torch.ops.mylab2.affine_bcast, type_promotion_kind=None)
def _lower_bcast(x, a, b):
    return L.add(L.mul(x, a), b)

@register_lowering(torch.ops.mylab2.scale_int64, type_promotion_kind=None)
def _lower_scale(x, k):
    return L.mul(x, k)
```

三个细节：

1. `type_promotion_kind=None` 表示不做自动类型提升。`affine_bcast` 的三个浮点输入同 dtype，不需要提升；`scale_int64` 的 `k` 是 Python int，`L.mul` 会把它变成常量，不会把结果提升成 float。
2. `Meta` 实现必须返回正确 shape。`affine_bcast` 用 `torch.broadcast_shapes`，`scale_int64` 用 `empty_like`。没有 Meta 实现，`torch.compile(fullgraph=True)` 会在 FakeTensor 传播阶段失败。
3. lowering 必须在第一次编译前注册。已经编译过的图不会自动失效；每次实验前调用 `torch._dynamo.reset()` 并用 `fresh_inductor_cache()`。

`L.mul(x, a)` 与 `L.add(..., b)` 都是标准 pointwise lowering。它们接受 TensorBox 或标量常量，返回 TensorBox；广播语义由 `make_pointwise` 和 IR 的 layout 处理，不需要在 lowering 里手写 `expand`。

### 3. broadcast 的生成代码：找 stride 0

编译模型：

```python
def model_bcast(x, a, b):
    return torch.relu(torch.ops.mylab2.affine_bcast(x, a, b))
```

用 `(64, 1)`、`(1, 64)`、`(1, 64)` 三个输入，输出 `(64, 64)`。运行后从 `output_code.py` 提取 kernel，重点看三类行：

```python
for line in body.splitlines():
    s = line.strip()
    if s.startswith("for(int64_t") or "loadu" in s or "Vectorized<float>(" in s or "clamp_min" in s:
        print("  " + s)
```

可能看到两种形态：

**形态 A：`x` 走标量 load，`a` 走向量 load。**

```cpp
auto tmp0 = in_ptr0[static_cast<int64_t>(x0)];
auto tmp1 = static_cast<float>(tmp0);
auto tmp2 = at::vec::Vectorized<float>(tmp1);
auto tmp3 = at::vec::Vectorized<float>::loadu(in_ptr1 + static_cast<int64_t>(x1), 8);
auto tmp4 = tmp2 * tmp3;
```

`x` 在最后一维的 stride 是 0，`CppVecKernel.load` 走 `stride == 0` 分支，调用 `super().load` 做一次标量读取；后续 `broadcast` 生成 `Vectorized<float>(tmp1)`。`a` 的最后一维连续，走 `loadu`。

**形态 B：两侧都走非连续路径或整体标量。**

如果 `TilingSelect` 判断连续维不够、或非连续索引太多，可能完全没有 `Vectorized`：

```cpp
for(int64_t x0=...; x0<...; x0+=1)
{
    auto tmp0 = in_ptr0[...];
    auto tmp1 = in_ptr1[...];
    auto tmp2 = float(tmp0 * tmp1);
    auto tmp3 = float(tmp2 + ...);
    ...
}
```

这不是 lowering 写错了，而是代码生成策略认为向量化不划算。练习要求你记录实际形态，并解释是哪一个条件触发的（连续维选择、非连续比例、op 数、范围大小）。

**形态 C：二维循环。**

如果广播导致两个维度都参与索引，可能出现 `x0` 与 `x1` 两层循环。外层可能是 64 行，内层 64 列；`x` 只随 `x0` 变化，`a` 只随 `x1` 变化。此时 `x` 的 load 在 `x1` 维的 stride 是 0，`a` 在 `x0` 维的 stride 是 0。两层循环都可能被 tiling，具体看 `_select_tiling_indices` 选了哪一维。

不管是哪种形态，数值必须与 `torch.relu(x * a + b)` 一致。先保证正确，再解释结构。

### 4. int64 的生成代码：lane 数与常量

编译模型：

```python
def model_int(x):
    return torch.ops.mylab2.scale_int64(x, 3)
```

输入 `torch.randint(-10, 10, (256, 256), dtype=torch.int64)`。`scale_int64` 的 lowering 是 `L.mul(x, 3)`，生成代码的重点是：

```python
for line in body.splitlines():
    s = line.strip()
    if "int64_t" in s or "Vectorized" in s or "x0+=" in s:
        print("  " + s)
```

可能出现：

```cpp
for(int64_t x0=0; x0<65536; x0+=8)
{
    auto tmp0 = at::vec::VectorizedN<int64_t,2>::loadu(in_ptr0 + x0, 8);
    auto tmp1 = static_cast<int64_t>(3);
    auto tmp2 = at::vec::VectorizedN<int64_t,2>(tmp1);
    auto tmp3 = tmp0 * tmp2;
    tmp3.store(out_ptr0 + x0);
}
```

这段代码来自本机实测。`x0 += 8` 不是笔误：`TilingSelect` 先用 `torch.float` 的 `nelements=8` 作为 `tiling_factor`，`CppVecKernel._get_num_vectors(int64)` 再算出 `ceil(8 * 8 * 8 / 256) = 2`，于是生成 `VectorizedN<int64_t,2>`：两个 256 位寄存器，每个装 4 个 int64，一次迭代共 8 个元素。硬件 lane 数（4）与代码生成 tiling（8）不是同一个数，这是本章最容易看错的地方。另一个容易误判的点是 `common.kernel_stats`：它只检查 `Vectorized<float>`，所以这份 `VectorizedN<int64_t,2>` 代码会被报告为 `vectorized=False`。判断整型向量化要直接搜 `VectorizedN<` 与 `x0+=`。

也可能出现标量形态：

```cpp
for(int64_t x0=0; x0<65536; x0+=1)
{
    auto tmp0 = in_ptr0[x0];
    auto tmp1 = static_cast<int64_t>(3);
    auto tmp2 = tmp0 * tmp1;
    out_ptr0[x0] = tmp2;
}
```

如果看到标量，检查 `cpp.vec_isa_ok`、`cpp.simdlen`、输入是否连续、op 数是否太少。不要为了让 `int64` 向量化而改 lowering；lowering 只决定 IR 是 `mul`，向量化是后端策略。

把 `float` 的 `affine_bcast` 与 `int64` 的 `scale_int64` 放在一起，你会得到一张 lane 数对照表：

| dtype | 位宽 | lane 数 | 生成类型 | 步长 |
|---|---:|---:|---|---:|
| `float32` | 256 | 8 | `Vectorized<float>` | 8 |
| `int64` | 256 | 4（每寄存器） | `VectorizedN<int64_t,2>` | 8（两个寄存器） |
| `bfloat16` | 256 | 16 | `Vectorized<bfloat16>` | 16 |
| `uint8` | 256 | 32 | `Vectorized<uint8_t>`（实际 tiling 受对齐影响） | 视 tiling |

这张表来自 `VecAVX2._dtype_nelements` 与 `CppVecKernel._get_num_vectors` 的公式，但实际是否生成向量代码还取决于 `TilingSelect`。

### 5. 断言与报告

数值断言：

```python
common.assert_close("broadcast 数值", out_b, ref_b)
common.assert_close("int64 数值", out_i, ref_i)
```

`assert_close` 默认容差 `1e-5`；`int64` 的误差应为 0。

结构断言：

```python
common.check("broadcast 单 kernel", src_b.count('extern "C" __declspec(dllexport)') == 1,
             str(src_b.count('extern "C" __declspec(dllexport)')))
common.check("broadcast 无自定义调用", "torch.ops.mylab2" not in src_b)
common.check("int64 单 kernel", src_i.count('extern "C" __declspec(dllexport)') == 1)
common.check("int64 无自定义调用", "torch.ops.mylab2" not in src_i)
common.check("int64 kernel 含 int64_t", "int64_t" in body_i)
```

不要断言 `step` 一定等于 4 或 8，也不要断言 `VectorizedN<int64_t,2>` 一定存在；这些取决于后端策略与 shape。把它们打印出来，写进报告，作为观察结果。

报告模板：

```text
| 变体 | kernel 数 | vectorized | step | omp threads | tmp_ops | max err |
|---|---:|---|---:|---|---:|---:|
| affine_bcast (64,1)x(1,64) | ? | ? | ? | ? | ? | ? |
| scale_int64 (256,256) | ? | ? | ? | ? | ? | ? |
```

再回答三个问题：

1. `affine_bcast` 的哪一个输入走了 `Vectorized<float>(tmp)` 而不是 `loadu`？为什么？
2. `scale_int64` 的步长是 8 还是 4？为什么硬件每寄存器只有 4 个 int64，代码却一次前进 8 个元素？
3. 如果 `affine_bcast` 完全没有向量化，是 `TilingSelect` 的哪条规则触发的？给出源码依据。

## 可跑实验

### 实验 1：先跑 lab_c，建立基线

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\lab_c_lowering.py
```

真实输出（节选）：

```text
[compile] fallback: 1 module(s)
[compile] lowering: 1 module(s)

1) 未注册 lowering：调用留在 wrapper 里，无法融合
  buf0 = torch.ops.mylab.affine_naive.default(arg0_1, 2.0, 1.0)
  assert_tensor_metadata(buf0, (256, 256), (256, 1), torch.float32, 'torch.ops.mylab.affine_naive.default')

2) 注册 lowering：算子展开成 IR，与 relu 同 kernel
  for(int64_t x0=static_cast<int64_t>(0LL); x0<static_cast<int64_t>(65536LL); x0+=static_cast<int64_t>(8LL))
  auto tmp0 = at::vec::Vectorized<float>::loadu(in_ptr0 + static_cast<int64_t>(x0), static_cast<int64_t>(8));
  auto tmp3 = tmp0 * tmp2;
  auto tmp6 = tmp3 + tmp5;
  auto tmp7 = at::vec::clamp_min(tmp6, decltype(tmp6)(0));

[PASS] lowering 后只有一个 kernel  (1)
[PASS] affine 的乘法/加法进入 kernel  (tmp_ops=8)
```

这就是练习脚本要复制的基本模式：先编译、抓模块、读源码、打印关键行、做数值与结构断言。

### 实验 2：运行你的 `practice_c_second_lowering.py`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File compilers\inductor_deep_dive\code\tools\run.ps1 compilers\inductor_deep_dive\code\labs\practice_c_second_lowering.py
```

参考实现会打印三组信息：

```text
==== 1) affine_bcast：广播后的生成代码 ====
  for(int64_t x0=...        <-- 你的实际步长
  ...                       <-- loadu / Vectorized<float>(...) 的实际形态

==== 2) scale_int64：整型 kernel ====
  for(int64_t x0=...        <-- 本机实测是 8；也可能是 1
  ...                       <-- VectorizedN<int64_t,2> 或标量 int64_t

==== 3) 断言 ====
[PASS] broadcast 数值 ...
[PASS] int64 数值 ...
[PASS] broadcast 单 kernel ...
[PASS] broadcast 无自定义调用 ...
[PASS] int64 单 kernel ...
[PASS] int64 无自定义调用 ...
[PASS] int64 kernel 含 int64_t ...
```

具体步长与 `tmp_ops` 会随 shape、torch 版本、后端策略变化，不要抄成本章的数字。你需要记录的是“实际值 + 解释”。

### 实验 3：只改 shape，观察 tiling 是否变化

把 `x` 从 `(64, 1)` 改成 `(1, 1)`，`a` 保持 `(1, 64)`。输出仍是 `(1, 64)`。观察：

- 循环层数是否从 2 层变成 1 层？
- `x` 的 load 是否只剩一次？
- `a` 是否仍走 `loadu`？

再把 `a` 改成 `(64, 64)`，`x` 仍是 `(64, 1)`。此时两个输入都有连续维，`TilingSelect` 可能选择最后一维 tiling，`x` 在最后一维 stride 0，仍然走标量 load + 广播。把结果填进报告，解释为什么“输入 shape 变了，lowering 没变，但生成代码变了”。

### 实验 4：把 int64 换成 int32

新增一个 `scale_int32`，或把输入 dtype 改成 `torch.int32`。`int32` 是 4 字节，`_get_num_vectors(int32)` 为 1，生成 `Vectorized<int32_t>`，一次 8 个元素。观察类型是否从 `VectorizedN<int64_t,2>` 变成 `Vectorized<int32_t>`，步长是否仍为 8。这个对照能验证“类型由位宽决定，tiling 由 float 基准与 `_get_num_vectors` 共同决定”。

## 源码落点

| 文件 | 函数 / 类 | 读什么 |
|---|---|---|
| `torch/_inductor/lowering.py` | `register_lowering` | 注册表、`type_promotion_kind` 的作用 |
| `torch/_inductor/lowering.py` | `mul` / `add` / `make_pointwise` | 自定义 lowering 如何落到标准 IR 算子 |
| `torch/_inductor/codegen/cpp.py` | `TilingSelect.select_tiling` | 连续维、非连续阈值、小范围阈值 |
| `torch/_inductor/codegen/cpp.py` | `TilingSelect._select_tiling_indices` | `stride_at_vec_range` 如何选出 tiling 维 |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel.load` | stride 0 的标量 load、stride 1 的 `loadu` |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel.broadcast` | `Vectorized<T>(scalar)` 的生成 |
| `torch/_inductor/codegen/cpp.py` | `CppVecKernel._get_num_vectors` | lane 数公式 |
| `torch/_inductor/codegen/cpp.py` | `CppVecOverrides.__new__` | 标量/向量混合时的常量提升 |
| `torch/_inductor/cpu_vec_isa.py` | `VecAVX2._dtype_nelements` | float/int64 的 lane 数来源 |
| `torch/_inductor/ops_handler.py` | `ExtractConstantsHandler` | 常量提取（`L.mul(x, 3)` 的 3 如何进 IR） |

阅读顺序建议：先读 `register_lowering` 与 `make_pointwise`，确认 lowering 只负责翻译；再读 `TilingSelect` 与 `CppVecKernel.load`，确认广播与 dtype 影响的是后端策略；最后读 `_get_num_vectors`，把 lane 数公式与生成代码对上。

## 常见坑

1. **lowering 注册太晚。** 如果先 `torch.compile` 再 `register_lowering`，已经编译的图不会失效，你会看到自定义调用仍在 wrapper 里。每次实验前 `torch._dynamo.reset()`，并在第一次调用前完成注册。

2. **Meta 实现返回错 shape。** `affine_bcast` 的 Meta 实现必须用 `torch.broadcast_shapes`，不能直接 `empty_like(x)`；否则 FakeTensor 传播会得到错误 shape，后续断言或编译直接失败。

3. **以为 lowering 里要手写广播。** `L.mul(x, a)` 已经带广播语义；在 lowering 里手动 `expand` 或 `broadcast_to` 往往引入额外 IR，反而阻止融合。先让标准算子处理广播，再看生成代码。

4. **把 `int64` 的常量写成 float。** `L.mul(x, 3.0)` 可能触发类型提升或额外的 `to_dtype`，生成代码里出现不必要的转换。用 Python `int`，让 lowering 生成整型常量。

5. **用 `torch.equal` 断言浮点结果。** broadcast 的浮点乘法/加法顺序可能与 eager 不同，应该用 `assert_close` 的容差；`int64` 可以要求完全相等。

6. **在同一进程里反复编译不同 dtype。** Dynamo/Inductor 缓存可能复用产物，导致“代码看起来是 int64，实际跑的是 float”。用 `fresh_inductor_cache()`，必要时像 lab_d 那样子进程隔离。

7. **把 `VectorizedN<int64_t,2>` 不存在当成 lowering 错误。** 后端可能因为 op 数、范围、非连续索引而选择标量；先看 `TilingSelect` 的条件，再决定是否调整 shape 或配置。

8. **忘了 `torch.library` 的命名空间只能定义一次。** 同一进程重复 `Library("mylab2", "DEF")` 并 `define` 同名 schema 会报重复注册；把实验拆成独立进程，或复用同一个 `lib` 对象。

9. **用 `kernel_stats["vectorized"]` 判断 int64。** 它只检查 `Vectorized<float>`；`VectorizedN<int64_t,2>` 会被判为 `False`。用 `VectorizedN<`、`loadu`、`x0+=` 三项一起判断，或者扩展一个专用的 stats 函数。

## 自我检查

- [ ] 我能在不修改 lab_c 的前提下，写出 `affine_bcast` 与 `scale_int64` 两个 lowering。
- [ ] 我能解释 `type_promotion_kind=None` 对 `scale_int64` 的意义。
- [ ] 我能从生成代码里找出 stride 0 的标量 load 与 `Vectorized<float>(...)` 广播。
- [ ] 我能说出 `int64` 在 AVX2 上的硬件 lane 数（4），以及为什么生成代码一次前进 8 个元素（`VectorizedN<int64_t,2>`）。
- [ ] 我能区分“lowering 决定 IR”与“TilingSelect 决定是否向量化”。
- [ ] 我能写出数值断言与结构断言，并说明为什么不断言固定步长。
- [ ] 我能解释 `fresh_inductor_cache()` 在这个练习里解决什么问题。
- [ ] 我能根据实际生成代码填写报告表，而不是抄本章示例。

## 延伸阅读

- **`torch/_inductor/lowering.py` 的 `register_lowering` 与 `make_pointwise`**：读哪里——注册表与 pointwise 包装；学什么——lowering 的职责边界；关联——本章两个 lowering。
- **`torch/_inductor/codegen/cpp.py` 的 `CppVecKernel.load` 与 `broadcast`**：读哪里——stride 0/1/其他三条路径、`Vectorized<T>(scalar)`；学什么——广播如何变成标量 load + 向量构造；关联——`affine_bcast` 的生成代码。
- **`torch/_inductor/codegen/cpp.py` 的 `_get_num_vectors`**：读哪里——`ceil(tiling_factor * itemsize * 8 / bit_width)`；学什么——float 得到 1 个 `Vectorized`、int64 得到 2 个 `VectorizedN` 寄存器的原因；关联——`scale_int64` 的步长。
- **`torch/_inductor/cpu_vec_isa.py` 的 `VecAVX2`**：读哪里——`_bit_width` 与 `_dtype_nelements`；学什么——ISA 位宽与 dtype lane 数；关联——本机只有 AVX2 的事实。
- **`torch/_inductor/ops_handler.py` 的 `ExtractConstantsHandler`**：读哪里——常量如何从 IR 中提取；学什么——`L.mul(x, 3)` 的 3 如何变成 `static_cast<int64_t>(3)`；关联——整型常量构造。
- **`compilers/inductor_deep_dive/05-lowering-rules.md`**：读哪里——`register_lowering`、type promotion、fallback；学什么——lowering 的系统规则；关联——lab_c 的前置知识。
- **`compilers/inductor_deep_dive/15-vectorization.md`**：读哪里——tiling、`loadu`、尾部；学什么——本章生成代码的解释框架；关联——broadcast 与 int64 的结构差异。

<details>
<summary>参考实现：完整的 `practice_c_second_lowering.py`</summary>

```python
"""练习 C：给自定义算子族加第二个 lowering。

对比：
  1. affine_bcast：三输入广播，观察 stride 0 的标量 load + 延迟广播；
  2. scale_int64：int64 乘法，观察类型从 Vectorized<float> 变成 VectorizedN<int64_t,2>。

运行：
    .\code\tools\run.ps1 code\labs\practice_c_second_lowering.py
"""
from __future__ import annotations

import torch
import torch._inductor.lowering as L
from torch._inductor.lowering import register_lowering
from torch._inductor.utils import fresh_inductor_cache

from labs import common

lib = torch.library.Library("mylab2", "DEF")
lib.define("affine_bcast(Tensor x, Tensor a, Tensor b) -> Tensor")
lib.define("scale_int64(Tensor x, int k) -> Tensor")


def _bcast(x, a, b):
    return x * a + b


def _scale(x, k):
    return x * k


torch.library.impl(lib, "affine_bcast", "CPU")(_bcast)
torch.library.impl(lib, "scale_int64", "CPU")(_scale)
torch.library.impl(lib, "affine_bcast", "Meta")(
    lambda x, a, b: torch.empty(
        torch.broadcast_shapes(x.shape, a.shape, b.shape),
        device=x.device,
        dtype=x.dtype,
    )
)
torch.library.impl(lib, "scale_int64", "Meta")(lambda x, k: torch.empty_like(x))


@register_lowering(torch.ops.mylab2.affine_bcast, type_promotion_kind=None)
def _lower_bcast(x, a, b):
    return L.add(L.mul(x, a), b)


@register_lowering(torch.ops.mylab2.scale_int64, type_promotion_kind=None)
def _lower_scale(x, k):
    return L.mul(x, k)


def model_bcast(x, a, b):
    return torch.relu(torch.ops.mylab2.affine_bcast(x, a, b))


def model_int(x):
    return torch.ops.mylab2.scale_int64(x, 3)


def compile_once(fn, args, label):
    torch._dynamo.reset()
    with fresh_inductor_cache():
        before = common.modules_snapshot()
        cf = torch.compile(fn, fullgraph=True)
        out = cf(*args)
        mods = common.modules_since(before)
        if not mods:
            raise SystemExit(f"{label}: 没有捕获到生成的模块")
        src = common.read_text_any(mods[-1].__file__)
    print(f"[compile] {label}: {len(mods)} module(s)")
    return cf, src, out


x = torch.randn(64, 1)
a = torch.randn(1, 64)
b = torch.randn(1, 64)
ref_b = torch.relu(x * a + b)

xi = torch.randint(-10, 10, (256, 256), dtype=torch.int64)
ref_i = xi * 3

fn_b, src_b, out_b = compile_once(model_bcast, (x, a, b), "affine_bcast")
fn_i, src_i, out_i = compile_once(model_int, (xi,), "scale_int64")

common.section("1) affine_bcast：广播后的生成代码")
body_b = common.extract_kernel(src_b)
for line in body_b.splitlines():
    s = line.strip()
    if s.startswith("for(int64_t") or "loadu" in s or "Vectorized<float>(" in s or "clamp_min" in s:
        print("  " + s)

common.section("2) scale_int64：整型 kernel")
body_i = common.extract_kernel(src_i)
for line in body_i.splitlines():
    s = line.strip()
    if s.startswith("for(int64_t") or "int64_t" in s or "Vectorized" in s or "x0+=" in s:
        print("  " + s)

common.section("3) 结构统计")
print("  broadcast:", common.kernel_stats(src_b))
print("  int64    :", common.kernel_stats(src_i))

common.section("4) 断言")
common.assert_close("broadcast 数值", out_b, ref_b)
common.assert_close("int64 数值", out_i, ref_i)
common.check(
    "broadcast 单 kernel",
    src_b.count('extern "C" __declspec(dllexport)') == 1,
    str(src_b.count('extern "C" __declspec(dllexport)')),
)
common.check("broadcast 无自定义调用", "torch.ops.mylab2" not in src_b)
common.check(
    "int64 单 kernel",
    src_i.count('extern "C" __declspec(dllexport)') == 1,
    str(src_i.count('extern "C" __declspec(dllexport)')),
)
common.check("int64 无自定义调用", "torch.ops.mylab2" not in src_i)
common.check("int64 kernel 含 int64_t", "int64_t" in body_i)

print()
print("把实际 step、vectorized、tmp_ops 填进报告表，并解释每一个差异。")
```

</details>

<details>
<summary>思考题答案</summary>

1. **哪个输入走 `Vectorized<float>(tmp)`？** 在最后一维上 stride 为 0 的那个。典型是 `x: (64, 1)`：它在输出 `(64, 64)` 的最后一维不前进，`CppVecKernel.load` 看到 `stride == 0`，做一次标量 load，再由 `broadcast` 生成 `Vectorized<float>(tmp)`。`a: (1, 64)` 在最后一维连续，走 `loadu`。

2. **`scale_int64` 的步长是 8 还是 4？** 本机实测是 8。`TilingSelect` 先用 float 的 `nelements=8` 作为 `tiling_factor`，`_get_num_vectors(int64) = ceil(8 * 8 * 8 / 256) = 2`，所以生成 `VectorizedN<int64_t,2>`，一次迭代 8 个元素。每个 256 位寄存器装 4 个 int64，两个寄存器合起来是 8 个。

3. **`affine_bcast` 完全没向量化时，可能是哪条规则？** 优先检查 `TilingSelect.select_tiling` 的三个提前返回：非连续索引数量超过 35 或比例超过阈值；内层范围小于 `tiling_factor / 4` 且 op 数小于 10；dtype 不在 `VECTORIZABLE_DTYPES`。其次检查 `cpp.vec_isa_ok=False`、`cpp.simdlen` 设置、输入是否连续。用 `ir_post_fusion.txt` 看索引表达式，用 `TilingSelect` 的日志或源码确认。

4. **为什么 lowering 不直接写向量代码？** 因为 lowering 的产物是 IR，不是文本。向量化是后端在 `TilingSelect` 与 `CppVecKernel` 里做的决策；同一个 lowering 在不同 dtype、shape、ISA 上会得到不同的代码。把向量逻辑写进 lowering 会让它绑定到某个 ISA，失去可移植性。

5. **`scale_int64` 用 `L.mul(x, 3)`，3 会变成什么？** 在 IR 里是一个整型常量，代码生成时变成 `static_cast<int64_t>(3)`，向量路径再广播成 `VectorizedN<int64_t,2>(tmp)`。如果写成 `3.0`，可能触发类型提升或额外的 `to_dtype`，所以练习要求用 Python `int`。

</details>

<details>
<summary>如果 broadcast 没有向量化，按这个顺序排查</summary>

1. 先看 `kernel_stats(src_b)["vectorized"]` 与 `step`，确认是“完全标量”还是“只向量化了一部分”。
2. 打开 `ir_post_fusion.txt`，找 `index0`、`index1`，看广播维是否以 stride 0 出现。
3. 检查 `cpp.vec_isa_ok`、`cpp.simdlen`、`torch.get_num_threads()`；这些是全局开关，可能被之前的实验改过。
4. 检查输入 layout：`x.stride()`、`a.stride()`，以及 `x` 是否真的是 `(64, 1)` 而不是 `(64, 64)`。
5. 数 `TilingSelect` 的 `non_contig_indexing_op_counter` 与 `op_num`；如果比例超过阈值，向量化会被主动放弃。
6. 最后才怀疑 lowering：`L.mul`/`L.add` 是否真的被调用，wrapper 里是否还有 `torch.ops.mylab2`。如果 lowering 没生效，先解决注册顺序与缓存问题。

</details>

[上一章：Ch 17 extern kernel 与模板](./17-extern-kernels-and-templates.md) | [下一章：Ch 18 产物与 ABI](./18-artifacts-and-abi.md)