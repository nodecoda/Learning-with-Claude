# Ch 6 — 语义层：类型、提升、广播与指针

## 目标

读完本章，你应该能回答下面几个问题：

- 一行 `x * 2.0 + 1` 在 Triton 里到底经过了哪些 Python 对象、哪几次类型检查，最后变成哪条 TTIR op？
- 为什么 `int32_tensor + uint32_tensor` 得到 `uint32`，而 `int8_tensor + int32_tensor` 得到 `int32`？
- 为什么 `int32_tensor / uint32_tensor` 会直接报错，而不是悄悄转成浮点？
- 为什么 `ptr + 1.0` 不合法，而 `ptr + 1` 会变成 `tt.addptr`？
- `tl.load` 的 `mask`、`other`、`pointer` 三者形状不一致时，语义层在哪一步把它们广播成同一形状？
- `tl.dot` 和 `tl.reduce` 在语义层分别检查什么，哪些检查留给了 TTIR verifier？

本章的主线是 `python/triton/language/semantic.py` 里的 `TritonSemantic`。它不是一个“类型检查器”那么简单：它同时负责标量常量物化、类型提升、广播、指针算术、内存 op 构造和部分 op 的合法性检查。理解这一层，才能判断一个 Python 表达式为什么会生成你看到的 TTIR。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92`，版本 3.9.0。
- 关键文件：`python/triton/language/semantic.py`、`python/triton/language/core.py`、`python/triton/compiler/code_generator.py`。
- 本章的源码行号均以该 commit 为准，使用 `nl -ba` 核对。
- 写作环境里 `python3` 没有可导入的 `triton` 模块，因此本章不伪造运行输出。标 `源码依据` 的结论直接来自源码；标 `guided` 的 IR 形态是按源码推导的预期，不作为实测结果。

## 为什么需要这个

Python AST 本身没有类型。`a + b` 在 AST 里只是 `BinOp(left=Name('a'), op=Add, right=Name('b'))`。Triton 的 `CodeGenerator` 看到它以后，会调用 `a.__add__(b)`；而 `a` 是一个 `tl.tensor`，它的 `__add__` 最终进入 `TritonSemantic.add`。也就是说，**类型系统不在 parser 里，也不在 MLIR 里，而是在 semantic 层**。

这一层决定了三件事：

1. **合法性**：哪些组合允许，哪些组合必须报错。比如有符号和无符号整数做 `/` 会报错。
2. **计算类型**：两个操作数先被提升到什么类型，再构造 IR。比如 `fp16 + bf16` 会先提升到 `fp32`。
3. **IR 形状**：标量要不要 splat 成 block，指针加法要不要变成 `tt.addptr`，mask 要不要广播。

如果只看最终 TTIR，你会看到 `arith.mulf`、`arith.addf`、`tt.addptr`，但看不到“为什么选了这个类型”。本章就是把这段空白补上。

## 先建立直觉

先看一条最小链路：

```python
@triton.jit
def kernel(p, n, BLOCK: tl.constexpr):
    offs = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    x = tl.load(p + offs, mask=offs < n, other=0.0)
    tl.store(p + offs, x * 2.0 + 1.0, mask=offs < n)
```

这条链路可以粗略拆成：

```text
Python AST
  -> CodeGenerator.visit_BinOp / visit_Call
  -> tl.tensor.__add__ / tl.load / tl.store  (core.py)
  -> TritonSemantic.add / load / store       (semantic.py)
  -> ir.builder.create_*                     (C++ builder)
  -> TTIR: tt.get_program_id, tt.make_range, tt.addptr, tt.load, arith.*, tt.store
```

`CodeGenerator.visit_BinOp` 只负责取左右操作数和运算符，然后调用对应的 dunder；真正的类型分派在 `semantic.py`。源码位置：

- `python/triton/compiler/code_generator.py:882-909`：`visit_BinOp` 与运算符表。
- `python/triton/language/core.py:928-945`：`tensor.__add__` / `__radd__` 转发到 `tl.add`。
- `python/triton/language/semantic.py:180-199`：`binary_op_type_checking_impl`，所有二元算术的共同入口。

`guided`：上面 kernel 的 TTIR 形状大致如下。不同版本的常量折叠、`tt.splat` 消除和 mask 规范化会改变细节，所以这里只展示“有哪些 op”，不把它当作逐字输出。

```mlir
%pid = tt.get_program_id x : i32
%c256 = arith.constant 256 : i32
%base = arith.muli %pid, %c256 : i32
%r = tt.make_range {start = 0 : i32, end = 256 : i32} : tensor<256xi32>
%s = tt.splat %base : i32 -> tensor<256xi32>
%offs = arith.addi %s, %r : tensor<256xi32>
%np = tt.splat %n : i32 -> tensor<256xi32>
%mask = arith.cmpi slt, %offs, %np : tensor<256xi32>
%pp = tt.splat %p : !tt.ptr<f32> -> tensor<256x!tt.ptr<f32>>
%addr = tt.addptr %pp, %offs : tensor<256x!tt.ptr<f32>>, tensor<256xi32>
%zero = arith.constant 0.0 : f32
%zero_b = tt.splat %zero : f32 -> tensor<256xf32>
%x = tt.load %addr, %mask, %zero_b : tensor<256x!tt.ptr<f32>>
%two = arith.constant 2.0 : f32
%two_b = tt.splat %two : f32 -> tensor<256xf32>
%y = arith.mulf %x, %two_b : tensor<256xf32>
%one = arith.constant 1.0 : f32
%one_b = tt.splat %one : f32 -> tensor<256xf32>
%z = arith.addf %y, %one_b : tensor<256xf32>
tt.store %addr, %z, %mask : tensor<256x!tt.ptr<f32>>
```

注意 `p + offs` 并没有变成 `arith.add`，而是 `tt.addptr`。这是 semantic 层按“指针 + 整数”专门分派的。

## 核心概念

### 1. `dtype`、`pointer_type`、`block_type`

`core.py` 把类型分成三层：

- `dtype`：标量类型，如 `int32`、`fp16`、`bf16`。它同时保存 `primitive_bitwidth`、`int_signedness`、`fp_mantissa_width` 等信息。
- `pointer_type`：`dtype` 的子类，额外保存 `element_ty`、`address_space`、`const`。
- `block_type`：`dtype` 的子类，保存 `element_ty` 和 `shape`，表示一个 block tensor 的类型。

源码：

- `python/triton/language/core.py:401-423`：`dtype.__init__` 和类型集合。
- `python/triton/language/core.py:543-551`：`kind()` 返回 `BOOLEAN < INTEGRAL < FLOATING`，这个顺序后面会用于标量不参与提升的规则。
- `python/triton/language/core.py:690-730`：`pointer_type`。
- `python/triton/language/core.py:732-784`：`block_type`，注意 `0d block_type is forbidden`。

`tl.tensor` 的 `type` 可以是 `dtype`（标量）或 `block_type`（block）。它的 `dtype` 属性统一取 `type.scalar`，所以标量 tensor 的 `dtype` 就是它自己，block tensor 的 `dtype` 是元素类型。

源码：`python/triton/language/core.py:885-922`。

### 2. 标量常量的类型由值决定

`TritonSemantic.to_tensor_type` 对 Python 标量做默认推断：

- `bool` -> `int1`
- `-2**31 <= x < 2**31` -> `int32`
- `2**31 <= x < 2**32` -> `uint32`
- `-2**63 <= x < 2**63` -> `int64`
- `2**63 <= x < 2**64` -> `uint64`
- 能放进 fp32 的 float -> `fp32`，否则 `fp64`

源码：`python/triton/language/semantic.py:134-163`。

这解释了一个常见现象：`x + 1` 里的 `1` 是 `int32`，不是“无限精度整数”。如果 `x` 是 `int8`，`1` 仍然先被当成 `int32`，然后按下面的规则提升。

### 3. 计算类型：先定类型，再 cast，再广播

`binary_op_type_checking_impl` 的顺序非常关键：

1. 判断左右是不是 Python 标量（`numbers.Number`）。
2. 取标量类型或 tensor 的 `dtype`。
3. 做指针合法性检查。
4. 如果两边都不是指针且 `arithmetic_check=True`，调用 `computation_type_impl` 得到计算类型，再把两边 cast 到该类型。
5. 最后做 `broadcast_impl_value`。

源码：`python/triton/language/semantic.py:180-199`。

注意第 4 步和第 5 步的顺序：**先统一元素类型，再统一形状**。所以 `tensor<4x8xf16> + tensor<8xbf16>` 会先变成两个 `tensor<...,fp32>`，再广播成 `4x8`。这也会影响你看到的 `tt.splat` / `tt.broadcast` 位置。

## 底层机制

### 1. `computation_type_impl` 的规则

源码：`python/triton/language/semantic.py:74-121`。按代码顺序读：

- 特殊类型 `float4e2m1x2`、`float8e8m0fnu` 不做隐式混合提升；两边必须相同，否则 `TypeError`。
- 标量不参与提升：如果一边是标量、一边是 tensor，且标量的 `kind()` 小于等于 tensor 的 `kind()`，就把标量类型替换成 tensor 类型。比如 `int32_tensor + 1.0` 里 `1.0` 是 `fp32`，`fp32.kind() > int32.kind()`，所以标量不会被忽略；而 `int32_tensor + 1` 里 `int32.kind() == int32.kind()`，标量被忽略，结果仍是 `int32`。
- 任一边 `fp64` -> `fp64`。
- `div_or_mod` 且任一边是浮点 -> `fp32`。原因是低位宽浮点没有除法/取模指令。
- 任一边 `fp32` -> `fp32`。
- 任一边 `fp16` -> `fp16`。
- 两边都是 `bf16` -> `bf16`；只有一边 `bf16` -> `fp32`。
- 两边都是 fp8：相同则保持，不同则 `fp16`。
- 两边都是整数：如果 `div_or_mod` 且符号性不同，直接 `TypeError`；否则走 `integer_promote_impl`。

`integer_promote_impl` 的规则在 `semantic.py:59-72`：

- 符号性相同：取位宽更大的。
- 一边无符号：无符号位宽 `>=` 另一边就取无符号，否则取有符号。
- 一边有符号：对称处理。

这基本对应 C 的 usual arithmetic conversions，但 Triton 对“标量不参与提升”做了 PyTorch 风格的简化。

### 2. 类型提升实例

`源码依据`：下面的结果都可以直接从 `computation_type_impl` / `integer_promote_impl` 推出。

| 表达式 | 计算类型 | 原因 |
|---|---|---|
| `int8_tensor + int32_tensor` | `int32` | 同符号，取更大位宽 |
| `int32_tensor + uint32_tensor` | `uint32` | 无符号位宽 >= 有符号位宽 |
| `int32_tensor + uint64_tensor` | `uint64` | 无符号位宽更大 |
| `int64_tensor + uint32_tensor` | `int64` | 有符号位宽更大 |
| `fp16_tensor + bf16_tensor` | `fp32` | 只有一边 bf16 |
| `bf16_tensor + bf16_tensor` | `bf16` | 两边都是 bf16 |
| `fp16_tensor + fp32_tensor` | `fp32` | 任一边 fp32 |
| `fp16_tensor / fp16_tensor` | `fp32` | `div_or_mod=True` 且是浮点 |
| `int32_tensor / uint32_tensor` | 报错 | 不同符号性做 `/` |
| `int32_tensor + 1` | `int32` | 标量 kind 不高于 tensor，被忽略 |
| `int32_tensor + 1.0` | `fp32` | 标量 kind 高于 tensor，参与提升 |

最后一个例子值得单独说：`int32_tensor + 1.0` 的结果是 `fp32`，不是 `int32`。因为 `1.0` 的 `to_tensor_type` 是 `fp32`，`FLOATING.kind() > INTEGRAL.kind()`，所以标量不会被忽略。

### 3. 失败案例：混合符号的除法

```python
@triton.jit
def bad_div(a, b, out):
    x = tl.load(a)
    y = tl.load(b)
    tl.store(out, x / y)
```

如果 `a` 是 `*i32`、`b` 是 `*u32`，`x / y` 会进入 `TritonSemantic.truediv`，再进入 `binary_op_type_checking_impl(..., div_or_mod=True)`。`computation_type_impl` 在 `semantic.py:117-120` 直接抛出：

```text
TypeError: Cannot use /, #, or % with int32 and uint32 because they have different signedness;
this is unlikely to result in a useful answer. Cast them to the same signedness.
```

`源码依据`：错误文本来自 `semantic.py:118-120`。注意它把 `/`、`#`、`%` 都列出来了，但 `#` 并不是 Python 运算符；这段文本是历史遗留的共享错误信息。

为什么不让它自动转 `fp32`？因为 `int32 / uint32` 在 C 里会先做 usual arithmetic conversion，再按整数除法；Triton 的 `truediv` 又希望得到浮点结果。两种语义都不“自然”，所以直接拒绝，要求显式 cast。

正确写法：

```python
x = x.to(tl.int64)
y = y.to(tl.int64)
z = x / y          # 仍然走浮点除法，结果是 fp32
# 或者
z = x.to(tl.float32) / y.to(tl.float32)
```

注意 `int / int` 在 `truediv` 里会被 cast 到 `fp32`（`semantic.py:334-337`），所以即使两边都是 `int32`，`/` 的结果也是 `fp32`，不是整数除法。想要整数除法用 `//`，它走 `floordiv`，同符号时生成 `arith.divsi` / `arith.divui`（`semantic.py:349-361`）。

### 4. 失败案例：指针 + 浮点

```python
@triton.jit
def bad_ptr(p, out):
    q = p + 1.5
    tl.store(out, tl.load(q))
```

`p + 1.5` 进入 `binary_op_type_checking_impl`，先调用 `check_ptr_type_impl(p.dtype, fp32, allow_lhs_ptr=True)`。源码 `semantic.py:169-178`：

- 如果 `type_a` 是指针且 `allow_ptr_a` 为真；
- 如果 `type_b` 是浮点，直接 `raise IncompatibleTypeErrorImpl`。

错误文本是：

```text
invalid operands of type pointer<fp32> and fp32
```

`源码依据`：`IncompatibleTypeErrorImpl` 的 message 在 `semantic.py:17-23`。

指针只能和整数偏移相加。`semantic.py:260-286` 的 `add` 会把 `ptr + int` 变成 `tt.addptr`；如果偏移是无符号且位宽小于 64，会先零扩展到 `int64`，避免被 `addptr` 当成负数。这个细节在写 `uint32` 偏移时很重要。

### 5. 广播：先补 rank，再按 1 扩展

`broadcast_impl_value` 在 `semantic.py:766-817`：

- block + scalar：把 scalar `tt.splat` 成和 block 同形状。
- scalar + block：对称处理。
- block + block：rank 小的在**前面**补 `1` 维（`create_expand_dims(..., 0)`），然后逐维比较：
  - `left == 1` -> 取 `right`
  - `right == 1` 或 `right == left` -> 取 `left`
  - 否则报 `Cannot make_shape_compatible`
- 最后对形状不等的边生成 `tt.broadcast`。

这和 NumPy 的广播规则一致：从尾部对齐，缺失维度补 1。区别是 Triton 在 IR 里显式生成 `tt.broadcast` / `tt.splat`，而不是隐式语义。

`broadcast_impl_shape` 在 `semantic.py:750-764` 还要求 rank 必须相同，且非单例维度必须匹配。它主要用于 `tl.broadcast_to` 这类显式 API。

### 6. `tl.load`：mask/other 先广播，再 cast

`TritonSemantic.load` 在 `semantic.py:1047-1102`。按顺序：

1. 检查 `ptr.type.scalar.is_ptr()`，否则 `ValueError`。
2. `other` 不能在没有 `mask` 时提供。
3. 如果 `ptr` 是标量指针，`mask` / `other` 不能是 block。
4. 如果 `ptr` 是 block，先 `broadcast_impl_value(ptr, mask)`，再 `broadcast_impl_value(ptr, other)`。
5. 取 `elt_ty = ptr_ty.element_ty`。
6. 如果 `elt_ty == int1`，把指针当成 `int8` 指针，load 完再 cast 回 `int1`。
7. 把 `other` cast 到 `elt_ty`。
8. 根据 `ptr` 是否 block 构造结果类型。
9. 没有 mask -> `create_load`；有 mask -> `create_masked_load`。

`core.py` 的 `tl.load` 只做 `constexpr` 解包、把 `mask` / `other` 转成 tensor、规范化 cache policy，然后调用 semantic。源码：`python/triton/language/core.py:2424-2466`。

一个边界：`other` 的 cast 发生在广播之后。也就是说，`other=0.0` 会先被 splat 成 block，再 cast 到 `elt_ty`。如果 `elt_ty` 是 `int8`，`0.0` 会变成 `int8 0`；如果 `elt_ty` 是 `int1`，会先走 `int8` 分支再 cast 回 `int1`。

### 7. `tl.store`：value 必须 cast 到 pointee 类型

`TritonSemantic.store` 在 `semantic.py:1248-1287`：

1. 拒绝 const 指针。
2. 检查 `ptr.type.scalar.is_ptr()`。
3. 标量指针时，`val` / `mask` 不能是 block。
4. block 指针时，`_broadcast_ptr_val_mask` 把 `ptr`、`val`、`mask` 广播到同一形状，并检查 `ptr` 形状没有被改变。
5. `val = self.cast(val, elt_ty)`。
6. `int1` 指针按 `int8` 处理。
7. 有 mask 时检查 `mask.type.scalar.is_bool()`，再 `create_masked_store`。

注意第 5 步：`tl.store(p, x)` 不会因为 `x` 是 `fp32`、`p` 是 `*i8` 就报错；它会隐式 cast 到 `i8`。这是 Triton 和强类型 IR 的一个区别：**Python 侧的 store 是隐式窄化的**。如果你不想窄化，先显式 `.to(...)` 或检查 `p.dtype.element_ty`。

### 8. `tl.dot`：语义层检查形状和 dtype，编码留给 TTGIR

`TritonSemantic.dot` 在 `semantic.py:1557-1655`。它检查：

- `lhs` / `rhs` 必须是 block，不能是标量。
- 非 fp8 情况下，dtype 必须在 `int8/uint8/fp16/bf16/fp32/fp64` 里，且两边相同。
- fp8 x fp8 允许任意组合；`fp8e4b15`、`fp8e4b8`、`fp8e5b16` 在特定目标上会 upcast 到 `fp16`。
- rank 必须是 2 或 3。
- `lhs.shape[-1] == rhs.shape[-2]`。
- 通过 `codegen_fns["min_dot_size"]` 检查 M/N/K 下界。
- 整数只支持 `int8`，累加类型是 `int32`。
- `out_dtype=bf16` 直接拒绝，要求用 `fp32/fp16` 再 cast。
- 如果传了 `acc`，检查形状和 `out_dtype` 一致。
- `max_num_imprecise_acc` 只对 fp8 -> fp32 dot 有意义。

`源码依据`：上述检查分别位于 `semantic.py:1559-1610`、`1611-1627`、`1634-1652`。

`dot` 不检查 layout encoding。TTIR 的 `tt.dot` 只要求 tensor 类型；到了 TTGIR，`DotOp::verify()` 才会检查 A/B/C 的 encoding 是否匹配。源码：`lib/Dialect/Triton/IR/Ops.cpp:378-400`。这是 TTIR 与 TTGIR 分工的一个典型例子。

### 9. `tl.reduce`：形状推导 + region 构造

`TritonSemantic.reduction` 在 `semantic.py:1794-1810`：

- `axis is None` 时，先把每个输入 reshape 成 1D，再令 `axis=0`。
- 取第一个输入的 shape，检查 `axis < rank`。
- `ret_shape` 是去掉 `axis` 后的 shape。
- 所有输入 shape 必须相同。
- `create_reduce` 创建 op，`region_builder_fn(reduce_op)` 填充 combine region。
- `reduce_op.verify()` 立刻做一次 op 级验证。
- 结果用 `wrap_tensor` 包装；`ret_shape` 为空时返回标量，否则返回 block。

`wrap_tensor` 在 `semantic.py:1786-1792`。注意 `0d-tensor -> scalar` 的分支：`tl.sum(x)` 对 1D 输入返回标量，而不是 `tensor<1xf32>`。

TTIR 侧的 `tt.reduce` 定义在 `include/triton/Dialect/Triton/IR/TritonOps.td:856-888`，它带一个 `SizedRegion<1>` 的 `combineOp`，并声明了 `InferTypeOpInterface`。verifier 在 `lib/Dialect/Triton/IR/Ops.cpp:667-767`，检查输入/输出数量、axis 范围、rank 一致、region 参数类型、terminator 和返回类型。

## 进一步用法/重要变体

### `cache_modifier` / `eviction_policy`

`tl.load` / `tl.store` 的 `cache_modifier` 和 `eviction_policy` 会被 `_normalize_cache_policy` 转成 `_CachePolicy`，再在 semantic 里 `_to_ir(builder)` 变成 `#tt.cache_policy<...>`。源码：`core.py:2413-2421`、`semantic.py:1047-1049`、`1248-1249`。

这些属性不改变类型，但会进入 TTIR op 的 `cachePolicy` 属性，并影响后续 lowering 到 PTX 的 `ld.global.ca/cg/cv`、`st.global.wb/cg/cs/wt`。

### `volatile`

`tl.load(..., volatile=True)` 会把 `isVolatile` 传给 `tt.load`。语义层只做布尔传递，不改变类型。源码：`core.py:2454-2465`、`semantic.py:1095-1099`。

### 标量指针与 block 指针

`tl.load(p)` 中 `p` 是 `pointer_type<f32>`，结果是标量 `f32`；`tl.load(p + offs)` 中 `p + offs` 是 `block_type<pointer_type<f32>>`，结果是 `block_type<f32>`。语义层用 `ptr.type.is_block()` 分流。源码：`semantic.py:1058-1091`。

一个常见错误是把标量指针和 block mask 混用：

```python
x = tl.load(p, mask=offs < n)   # p 是标量指针，mask 是 block
```

会命中 `semantic.py:1058-1060` 的 `ValueError: Mask argument cannot be block type if pointer argument is not a block`。

### `int1` 指针

Triton 没有真正的 `i1` 内存 load/store。`semantic.py:1075-1080` 和 `1274-1279` 把 `pointer<int1>` 当成 `pointer<int8>`，load 后 cast 回 `int1`，store 前 cast 到 `int8`。这解释了为什么 `*i1` 的 TTIR 里会看到 `tt.int_to_ptr` 或 `tt.bitcast` 之类的转换，而不是直接的 `i1` load。

## 对比与取舍

| 维度 | Triton semantic | C usual arithmetic conversions | PyTorch type promotion |
|---|---|---|---|
| 标量参与提升 | 标量 kind 不高于 tensor 时忽略 | 所有操作数都参与 | 标量按“wrapped number”规则弱参与 |
| 混合符号除法 | 直接报错 | 先转无符号/有符号再除 | 通常转浮点 |
| 广播 | 显式 `tt.broadcast` / `tt.splat` | 无 | 隐式 stride-0 广播 |
| 指针算术 | 只允许 `ptr + int`，生成 `tt.addptr` | 指针 + 整数 | 不暴露 |
| 浮点低位宽除法 | 提升到 `fp32` | 按 C 规则 | 按 dtype 规则 |

取舍点：

- **显式广播**让 IR 可分析，但会让 TTIR 更长；后续 canonicalize 会消掉一部分 `tt.splat` / `tt.broadcast`。
- **混合符号除法报错**避免了“结果到底是整数还是浮点”的歧义，但要求用户写显式 cast。
- **标量弱参与**让 `x + 1` 保持 `x` 的类型，符合直觉；但也意味着 `uint8_tensor + 300` 会在 `_cast_to_computation_type` 里检查范围并可能报 `Scalar 300 is out of range for type uint8`（`semantic.py:207-208`）。

## 踩雷集锦

1. **`int32 / uint32` 不是自动转浮点，而是报错。**
   `computation_type_impl` 在 `div_or_mod=True` 且符号性不同时直接 `TypeError`。先显式 `.to(tl.int64)` 或 `.to(tl.float32)`。

2. **`ptr + 1.0` 不合法。**
   `check_ptr_type_impl` 明确拒绝 `T* + float`。指针偏移必须是整数；浮点偏移要先 `to(tl.int32/int64)`。

3. **`other` 不能单独出现。**
   `tl.load(p, other=0.0)` 会命中 `semantic.py:1054-1055` 的 `ValueError`。`other` 只用于 mask 为 false 时的替代值，必须同时给 `mask`。

4. **标量指针不能配 block mask。**
   `tl.load(p, mask=block_mask)` 会报 `Mask argument cannot be block type if pointer argument is not a block`。要么把 `p` 变成 block 指针，要么把 mask 变成标量。

5. **`tl.store` 会隐式窄化。**
   `tl.store(p_i8, x_fp32)` 不会报错，而是把 `x_fp32` cast 到 `i8`。如果你依赖精确值，先显式 cast 并确认范围。

6. **`tl.dot` 的 `out_dtype=bf16` 被拒绝。**
   `semantic.py:1615-1618` 要求用 `fp32/fp16` 累加再 `.to(tl.bfloat16)`。这是为了避免后端没有 bf16 累加器时产生隐式降级。

7. **`tl.dot` 的 K 下界不是 1。**
   `min_dot_size` 由后端提供，`semantic.py:1605-1610` 会检查 `M/N/K` 下界。小矩阵 dot 可能直接断言失败，而不是生成慢速 fallback。

## 进阶

- 读 `semantic.py` 的 `ternary_op_type_checking_impl`（`211-237`），看 `tl.where` 如何分别合并 tensor 类型和 scalar 类型，再统一 cast。它比二元版本多了一次“先分别合并，再合并 tensor/scalar”的处理，避免标量在中间类型里被提前舍入。
- 读 `semantic.py:850-961` 的 `cast`，看 fp8、bf16、fp16、int、ptr 之间的转换分支。特别注意 `bf16 <-> fp16` 走 `create_fp_to_fp`，`bf16 <-> fp32` 走 `fp_ext/fp_trunc`。
- 读 `semantic.py:2013-2058` 的 `make_tensor_descriptor`，看它如何在语义层检查 rank、stride、16 字节对齐和 last stride == 1。这是 Ch 7 的铺垫。
- 读 `lib/Dialect/Triton/IR/Ops.cpp:378-400` 的 `DotOp::verify()`，对比 semantic 层的检查，理解“前端检查”和“IR verifier”为什么需要同时存在。

## 动手练习

1. **推导类型**：不运行代码，根据 `computation_type_impl` 写出下面表达式的结果类型，并说明理由：
   - `uint16_tensor + int16_tensor`
   - `int64_tensor + uint64_tensor`
   - `bf16_tensor + fp16_tensor`
   - `fp16_tensor % fp16_tensor`
   - `int8_tensor // uint8_tensor`

2. **构造失败**：写一个最小 kernel，让 `tl.load` 触发 `Mask argument cannot be block type if pointer argument is not a block`，并指出是哪一行源码抛出的。

3. **追踪广播**：给 `tensor<4x1xf32>` 和 `tensor<1x8xf32>` 相加，画出 `broadcast_impl_value` 的每一步：rank 是否补齐、`ret_shape` 是什么、哪一边生成 `tt.broadcast`。

4. **读 IR**：用 `TRITON_KERNEL_DUMP=1` 导出本章开头 kernel 的 TTIR，找到 `tt.addptr`、`tt.load`、`arith.mulf`、`tt.store`，对照 `semantic.py` 的行号说明每条 op 由哪个函数创建。

## 本章重点

- `TritonSemantic` 是 Python 表达式到 TTIR 的类型检查与 op 构造层。
- 二元运算的顺序是：取类型 -> 指针检查 -> 计算类型 -> cast -> 广播。
- `computation_type_impl` 的规则决定了标量弱参与、fp16/bf16 提升、混合符号除法报错。
- 指针只允许和整数相加，生成 `tt.addptr`；`ptr + float` 直接报错。
- `tl.load` / `tl.store` 会先广播 `mask` / `other` / `value`，再 cast 到 pointee 类型。
- `tl.dot` 在语义层检查 dtype、rank、K 和 min_dot_size；layout encoding 留给 TTGIR verifier。
- `tl.reduce` 在语义层推导 `ret_shape` 并构造 combine region，`0d` 结果会变成标量。

## 自我检核

- 你能解释为什么 `int32_tensor + 1.0` 是 `fp32`，而 `int32_tensor + 1` 是 `int32` 吗？
- 你能说出 `binary_op_type_checking_impl` 里“先 cast 再广播”的顺序，并解释如果反过来会有什么问题吗？
- 你能指出 `tl.load` 的 `other` 是在广播前还是广播后 cast 吗？
- 你能说出 `tl.dot` 的哪些检查在 semantic 层，哪些在 TTIR verifier 吗？
- 你能写出一个触发 `T* + float` 错误的最小 kernel 吗？

## 延伸阅读

1. **`python/triton/language/semantic.py:59-121`**
   - 读哪里：`integer_promote_impl` 和 `computation_type_impl`。
   - 学什么：Triton 的整数/浮点提升规则，以及为什么混合符号除法被拒绝。
   - 前提：知道 C 的 usual arithmetic conversions 和 Python 的标量类型。

2. **`python/triton/language/semantic.py:1047-1102`、`1248-1287`**
   - 读哪里：`load` 和 `store`。
   - 学什么：mask/other/value 的广播、`int1` 指针处理、cache policy 传递。
   - 前提：知道 `tt.load` / `tt.store` 的基本语法。

3. **`python/triton/language/core.py:690-784`**
   - 读哪里：`pointer_type`、`block_type`。
   - 学什么：Python 类型对象如何携带 element type、shape、address space。
   - 前提：读过 Ch 7 的值模型，或愿意先看 `base_type`。

4. **`include/triton/Dialect/Triton/IR/TritonOps.td:257-343`、`762-798`、`856-888`**
   - 读哪里：`TT_LoadOp`、`TT_StoreOp`、`TT_DotOp`、`TT_ReduceOp`。
   - 学什么：TTIR op 的 operand/result/attr/region 定义，以及 verifier 声明。
   - 前提：知道 MLIR TableGen 的基本语法。

5. **`lib/Dialect/Triton/IR/Ops.cpp:378-400`、`667-767`**
   - 读哪里：`DotOp::verify()`、`verifyReduceScan`、`verifyRegionsImpl`。
   - 学什么：TTIR verifier 如何检查 encoding、axis、region 参数和返回类型。
   - 前提：知道 MLIR 的 `LogicalResult`、`Operation`、`Block`。

## 下一章

下一章进入 `core.py` 的值模型：`tl.tensor`、`dtype`、`constexpr`、`tuple` 和 `tensor_descriptor` 如何在 Python 侧包装 MLIR handle，以及 `_flatten_ir` / `_unflatten_ir` 为什么是区域边界和函数调用的关键。

→ [Ch 7 语言值模型：tensor、dtype、constexpr、descriptor](./07-language-value-model-and-tensor-descriptors.md)
