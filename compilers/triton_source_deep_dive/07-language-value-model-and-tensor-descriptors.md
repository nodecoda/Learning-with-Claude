# Ch 7 — 语言值模型：tensor、dtype、constexpr、descriptor

> `tl.tensor` 不是数据，它只是 `ir.value` 的一层包装。

## 目标

读完本章，你应该能回答：

- `tl.tensor` 的 `handle`、`type`、`dtype`、`shape` 分别是什么？哪些是 MLIR 的，哪些是 Python 的？
- 为什么 `tl.constexpr` 也是 `base_value`，但它没有 handle？`_unwrap_if_constexpr` 到底在防什么？
- `_flatten_ir` / `_unflatten_ir` 这对方法为什么是函数调用、返回值、`scf` region 边界的关键？
- `tl.tuple` 和 namedtuple 在 IR 层长什么样？为什么一个 Python 参数会变成多个 kernel 参数？
- `tensor_descriptor` 的 `block_shape` 和 `strides` 分别约束什么？在 T4 上它走哪条路径？

本章讲“值在 Python 侧长什么样”。op 怎么构造、类型怎么提升属于 Ch 6；MLIR 的 Operation/Value 属于 Ch 11。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 `3.9.0` |
| GPU | Tesla T4，`sm_75`（无 TMA 硬件） |
| 驱动 / CUDA | 535.216.01 / 12.4 |
| Python | 3.10.12 |
| `import triton` | `ModuleNotFoundError`（系统环境与 `.venv` 均如此） |

本章没有 Triton 运行时输出。所有对象关系与检查逻辑标 `源码依据`；涉及 sm_75 上 descriptor 实际走哪条 lowering 的推断标 `guided`，并说明推断依据。

**源码依据**：`python/triton/language/core.py` 共 3901 行；`tensor` 在 `885`，`tuple` 在 `1339`，`tensor_descriptor` 在 `1573`，`make_tensor_descriptor` 在 `2547`。

## 为什么需要这个

因为你在写 kernel 时看到的每一个报错，几乎都来自这一层：

- `unsupported tensor index: ...` 来自 `tensor.__getitem__`（`1135-1159`），不是 MLIR。
- `0d block_type is forbidden` 来自 `block_type.__init__`（`743-744`）。
- `Tensor descriptor last dim must be 1` 来自 `semantic.make_tensor_descriptor`（`2030-2032`）。

更重要的是：**Triton 的 Python 对象不持有数据**。`x = tl.load(...)` 之后，`x` 里没有 128 个 float；只有一个 MLIR `Value`（SSA 句柄）和一份类型描述。所有“值”的语义都在编译期确定，运行时只有 GPU 寄存器/内存。理解这一点，才能理解为什么 `tensor` 可以自由复制、为什么 `constexpr` 不占寄存器、为什么 `tuple` 会被“摊平”。

## 先建立直觉

看一个最小 kernel 的对象图：

```python
@triton.jit
def k(p, n, BLOCK: tl.constexpr):
    offs = tl.arange(0, BLOCK)
    x = tl.load(p + offs, mask=offs < n, other=0.0)
    tl.store(p + offs, x * 2.0, mask=offs < n)
```

`x` 这个 Python 对象大致是：

```text
tensor
├── handle : ir.value          # MLIR 里的 %x，SSA 句柄
├── type   : block_type        # <(128,), fp32>
│            ├── element_ty : dtype(fp32)
│            └── shape      : (128,)
├── dtype  : dtype(fp32)       # = type.scalar
├── shape  : (constexpr(128),) # 注意是 constexpr 元组
└── numel  : constexpr(128)
```

`源码依据`：`tensor.__init__` 在 `905-917`；`shape` 被转成 `tuple([constexpr(s) for s in self.shape])`（`916`）；`dtype = type.scalar`（`915`）。

`offs` 是 `tensor<128xi32>`，`p + offs` 是 `tensor<128x!tt.ptr<f32>>`，`mask` 是 `tensor<128xi1>`。它们的 Python 类都是 `tensor`，区别只在 `type`。

整条链：

```text
Python 源码
  -> CodeGenerator 调用 tl.load / tensor.__add__ / tl.store
     -> core.py 做参数归一化（unwrap constexpr、cache policy、shape 检查）
        -> semantic.py 做类型检查/广播，调 builder.create_*
           -> MLIR Value
              -> 被 tensor(handle, type) 包回 Python
```

所以 `core.py` 是“语法糖 + 参数整形”，`semantic.py` 才是“语义”。

## 核心概念

### 1. 两条继承线：`base_value` 与 `base_type`

```text
base_value                 base_type
├── constexpr              ├── constexpr_type
├── tensor                 ├── dtype
├── tuple                  │    ├── pointer_type
└── tensor_descriptor_base │    └── block_type
                           ├── tuple_type
                           └── tensor_descriptor_base_type
```

`base_value` 的契约只有两个方法（`156-168`）：

```python
def _set_name(self, builder, name): ...   # 给 IR 值挂 name loc
def _flatten_ir(self, handles): ...       # 把自己摊成若干 ir.value
```

`base_type` 的契约是 `__eq__`、`mangle`、`_flatten_ir_types`、`_unflatten_ir`（`171-190`）。

这条分界非常重要：**值可以摊平成多个 handle，类型可以摊平成多个 `ir.type`**。`tuple` 和 `tensor_descriptor` 都利用了这个能力。

### 2. `tensor` = handle + type

```python
def __init__(self, handle, type: dtype):
    self.handle = handle
    self.shape = type.shape if type.is_block() else ()
    self.numel = constexpr(math.prod(self.shape))
    self.type = type
    self.dtype = type.scalar
```

`源码依据`：`905-917`。

- 标量 tensor 的 `type` 是 `dtype`，`shape == ()`，`numel == 1`。
- block tensor 的 `type` 是 `block_type`，`shape` 是 Python tuple of `constexpr`。
- `dtype` 永远是元素类型（对标 PyTorch 的 `tensor.dtype`），不是 `block_type`。

`_flatten_ir` 只做一件事：`handles.append(self.handle)`（`921-922`）。一个 tensor 对应一个 MLIR 值，仅此而已。

### 3. `dtype` 是类型对象，`pointer_type` / `block_type` 也是 `dtype`

`dtype` 的子类关系容易让人意外：

```python
class pointer_type(dtype): ...
class block_type(dtype): ...
```

`源码依据`：`690`、`732`。

所以 `t.is_block()`、`t.is_ptr()`、`t.scalar` 这些方法在所有“类型”上都能调。`pointer_type.scalar` 返回自己（`725-726`），`block_type.scalar` 返回 `element_ty`（`773-774`）。

`dtype.__init__`（`419-467`）从名字推导 `primitive_bitwidth`、`itemsize`、`int_signedness`、`fp_mantissa_width`、`exponent_bias`。这是一张纯 Python 的查表，不碰 MLIR。

`pointer_type` 额外带 `address_space` 和 `const`（`692-699`）；`const` 指针的 `to_ir` 会把地址空间改成 `"constant"`（`701-704`）。

`block_type` 带 `element_ty` 和 `shape`，并且**禁止 0d**：

```python
self.shape = tuple(_unwrap_shape(shape))
if not self.shape:
    raise TypeError('0d block_type is forbidden')
self.numel = validate_block_shape(self.shape)
```

`源码依据`：`742-746`。

### 4. `constexpr` 是“没有 handle 的 base_value”

```python
class constexpr(base_value):
    def __init__(self, value):
        while isinstance(value, constexpr):
            value = value.value
        self.value = value
        self.type = constexpr_type(value)

    def _flatten_ir(self, handles):
        return
```

`源码依据`：`221-243`。

三个后果：

1. `_flatten_ir` 是空操作 → `constexpr` 永远不占 kernel 参数、不占寄存器。
2. `constexpr_type.mangle()` 是 `c{repr(value)}`（`207-212`）→ 它参与函数名修饰与 cache key。
3. 所有“既可能是 Python 值、又可能是 `constexpr`”的 API 入口都要 `_unwrap_if_constexpr`。

`_unwrap_if_constexpr`（`370-378`）递归处理 list / tuple / `language.tuple`，但对 tensor 原样返回。它**不**会把 tensor 变成 Python 值，只剥 `constexpr` 这层壳。

### 5. `tuple` 是聚合值，不是 IR 类型

```python
class tuple(base_value):
    def __init__(self, args, type=None):
        self.values = [i for i in args]
        self.type = type if isinstance(type, tuple_type) else _type_for_tuple_values(self.values)
```

`源码依据`：`1339-1348`。

- `tuple._flatten_ir` 逐个调用成员的 `_flatten_ir`（`1406-1408`），所以 `(tensor<128xf32>, tensor<64xf32>)` 摊成 2 个 handle。
- `tuple_type._flatten_ir_types` 同理摊成 2 个 `ir.type`（`804-806`）。
- `tuple.__getitem__` 是纯 Python 索引，支持 `constexpr` 下标和 slice（`1350-1357`）。
- `tuple.__getattr__` 支持 namedtuple 风格字段访问，字段名来自 `tuple_type.fields`（`1359-1363`）。

`_type_for_tuple_values`（`1336-1337`）对 `int` / `float` / `dtype` 用 `constexpr_type`，其余用 `x.type`。所以 `return 1, x` 的类型是 `tuple_type([constexpr_type(1), block_type])`。

### 6. `tensor_descriptor` = handle + shape + strides + block_type

```python
class tensor_descriptor(tensor_descriptor_base):
    def __init__(self, handle, shape, strides, block_type):
        super().__init__(handle, block_type)
        self.shape = tuple(shape)
        self.strides = tuple(strides)
        self.type = tensor_descriptor_type(block_type, self.shape.type, self.strides.type)
```

`源码依据`：`1573-1586`。

它比 tensor 多两样东西：

- `shape` / `strides`：全局张量的形状与步长，类型是 `tuple_type`，**会作为额外参数传给 kernel**。
- `block_type`：每次 `load` / `store` 搬多少。

`_flatten_ir` 因此是“1 + N + N”个 handle：

```python
super()._flatten_ir(handles)     # descriptor 本体
self.shape._flatten_ir(handles)  # N 个 shape
self.strides._flatten_ir(handles)# N 个 stride
```

`源码依据`：`1595-1598`；对应的类型摊平在 `1563-1566`。

这解释了一个常见困惑：**为什么一个 descriptor 参数会变成一堆 kernel 参数**。因为在 ABI 上它不是单个指针，而是“描述符 + 形状 + 步长”。

## 底层机制

### 1. flatten / unflatten 协议

两个自由函数把协议串起来（`code_generator.py:111-123`）：

```python
def flatten_values_to_ir(values):
    handles = []
    for v in values:
        v._flatten_ir(handles)
    return handles

def unflatten_ir_values(handles, types):
    cursor = 0
    for ty in types:
        value, cursor = ty._unflatten_ir(handles, cursor)
        yield value
    assert cursor == len(handles)
```

`源码依据`：`111-123`。

`_unflatten_ir(handles, cursor)` 的契约是“从 cursor 开始消费若干 handle，返回 (前端值, 新 cursor)”。各类型的实现：

| 类型 | 消费几个 handle | 源码 |
|---|---|---|
| `dtype` | 1 | `665-666` |
| `constexpr_type` | 0 | `217-219` |
| `tuple_type` | 成员之和 | `814-819` |
| `tensor_descriptor_base_type` | 1 | `1428-1434` |
| `tensor_descriptor_type` | 1 + shape + strides | `1553-1566` |

这条协议出现在所有“边界”上：

- 函数调用：`call_JitFunction` 里 `flatten_values_to_ir(args)` → `builder.call` → `unflatten_ir_values(results, [ret_type])`（`1475-1479`）。
- 函数返回：`handle_returns` 里 `flatten_values_to_ir([ret])`（`636-641`）。
- `scf.if` / `scf.for` / `scf.while` 的 carry：`_find_carries` 比较 `live_handles != loop_handles`（`503-511`）。
- `scf` 的 block argument：`unflatten_ir_values(block_handles, init_tys)`（`1370-1372`）。

**为什么必须成对存在？** 因为 MLIR 只认扁平的 SSA 值列表，而前端要有结构（tuple、descriptor）。`flatten` 是“结构 → 扁平”，`unflatten` 是“扁平 → 结构”，两者必须严格对称，否则 `cursor` 断言会失败。

### 2. `dtype.to_ir`：Python 类型 → MLIR 类型

`dtype.to_ir`（`603-643`）是一张长 `if/elif`：`int1 -> get_int1_ty()`、`int8/uint8 -> get_int8_ty()`、`int32/uint32 -> get_int32_ty()`、`fp16 -> get_half_ty()`、`bf16 -> get_bf16_ty()`、`fp32 -> get_float_ty()`、`fp64 -> get_double_ty()`，fp8 各变体有独立 `get_fp8*_ty()`。

注意 `int8` 和 `uint8` 映射到**同一个** MLIR 类型（`612-613`）。符号性只存在于 Python 侧和 op 属性里，MLIR 的整数类型本身不区分。这是后面 `arith.divsi` vs `arith.divui`、`extsi` vs `extui` 必须显式选的原因（Ch 6、Ch 23）。

fp8 还有架构 gate：

```python
if self.name.startswith("fp8"):
    if hasattr(builder, "options") and self.name not in builder.options.supported_fp8_dtypes:
        raise ValueError(f'type {self} not supported in this architecture. ...')
```

`源码依据`：`607-611`。T4 的 `supported_fp8_dtypes` 是 `("fp8e5", "fp8e4b15")`（`third_party/nvidia/backend/compiler.py:143`），所以 `fp8e4nv` 在 sm_75 上会在这里就被拒绝。

### 3. `block_type` 的形状约束

`validate_block_shape` 会检查每一维是 2 的幂且总元素数不超过上限。`block_type` 还要求 shape 非空（`743-744`），因为 0d 用 `dtype` 表示就够了。

`block_type.mangle()` 是 `{elt}S{shape}S`（`780-783`），例如 `fp32S128S`、`fp16S64_64S`。这直接进入函数名修饰（Ch 5 的 `mangle_fn`）和 cache key。

### 4. descriptor 的构造与检查

`tl.make_tensor_descriptor`（`core.py:2547-2600`）只是转发到 `semantic.make_tensor_descriptor`（`2013-2059`）。检查顺序值得背下来：

```text
1 <= ndim <= 5；strides/block_shape 长度等于 ndim；base 必须是 pointer（2016-2022）
block_shape[-1] * elem_size >= 16；strides[-1] == 1（2024-2032）
除最后一维外，静态 stride * elem_size 必须是 16 的倍数（1985-1992）
shape 转 int32、strides 转 int64；block_shape 必须静态；整数 block 禁止 padding="nan"（2037-2054）
```

然后 `create_make_tensor_descriptor(...)` 并 `return tl.tensor_descriptor(handle, shape, strides, type)`（`2056-2059`）。

两个例子：

```python
# 合法（fp32）：BN*4 >= 16；stride N 满足 N*4 % 16 == 0
desc = tl.make_tensor_descriptor(
    base, shape=[M, N], strides=[N, 1], block_shape=[BM, BN])
v = desc.load([m_off, n_off])
desc.store([m_off, n_off], v)
```

```python
# 非法：最后一维 stride 不是 1
tl.make_tensor_descriptor(base, shape=[M, N], strides=[1, N], block_shape=[BM, BN])
# ValueError: Tensor descriptor last dim must be 1 but got N
```

`guided`：在 sm_75 上不会走 TMA 硬件路径。依据是 `semantic._has_native_tma` 要求 `arch >= 90`（`1137-1139`），而 launcher 只有在 metadata 带 `tensordesc_meta` 时才选 `TensorDescABI.CUDA_TMA`，否则选 `DECOMPOSED`（`third_party/nvidia/backend/driver.py:268-270`）；`decompose_descriptor` 会把 base、shape、strides 以及 padding/round 标志摊成参数（`python/triton/backends/driver.py:10-13`）。设备端具体如何降级到普通 load/store 属于 Ch 15/24，本章不展开，也没有本机实测。

### 5. `tl.load` / `tl.store` / `tl.dot` 的 Python / semantic 分界

**`tl.load`（`2425-2467`）在 Python 层做**：

```python
mask = _unwrap_if_constexpr(mask)
other = _unwrap_if_constexpr(other)
if mask is not None:  mask = _semantic.to_tensor(mask)
if other is not None: other = _semantic.to_tensor(other)
volatile = _unwrap_if_constexpr(volatile)
cache_policy = _normalize_cache_policy(None, cache_modifier, eviction_policy)
return _semantic.load(pointer, mask, other, cache_policy, volatile)
```

`源码依据`：`2457-2466`。

即：剥 `constexpr`、把 Python 标量变成 tensor、校验 cache modifier/eviction policy 字符串、打包成 `_CachePolicy`。**广播、类型提升、`tt.load` 构造**都在 `semantic.load`。

`_CachePolicy.__post_init__`（`2387-2400`）是纯 Python 校验：`cache_modifier` 必须在 `{none, ca, cg, wb, cs, wt, cv}`，`eviction_policy` 必须在 `{evict_normal, evict_first, evict_last}`。写错字符串在这里就报 `ValueError`，不会等到 PTX。

**`tl.store`（`2485-2520`）** 同样是先 `_semantic.to_tensor(value)`，再 unwrap mask，再 `_normalize_cache_policy`。

**`tl.dot`（`2253-2325`）** 在 Python 层做形状断言和批量维折叠：

```python
assert len(a_shape) == len(b_shape) >= 2
assert a_shape[:-2] == b_shape[:-2]
assert a_shape[-1] == b_shape[-2]
c_shape = a_shape[:-1] + [b_shape[-1]]
if rank >= 4:
    input = _semantic.reshape(input, [batch_size] + a_shape[-2:])
    ...
res = _semantic.dot(input, other, acc, input_precision, max_num_imprecise_acc, out_dtype)
```

`源码依据`：`2291-2322`。

所以 `tl.dot` 支持 4D+ 是靠 Python 层 reshape 成 3D batched dot，再 reshape 回来；语义层只处理 2D/3D。dtype 合法性、min shape 等检查在 `semantic.dot`。

## 进一步用法/重要变体

**namedtuple 聚合值**：`tuple_type.fields` 非空时，`tuple.__getattr__` 支持 `x.field`，`_set_name` 会给每个字段挂 `name.field` 的 loc（`1397-1404`）。这让 `return (a=..., b=...)` 在 IR 里可读。

**`slice`**：`core.slice`（`1414-1420`）只是三元组容器，`visit_Slice`（`code_generator.py:1424-1429`）构造它，`tensor.__getitem__` 用它做 `expand_dims` 或校验索引。

**`_unsplat`**：把 `tensor<1xi1>` 条件降成标量，`visit_If` 里对 1 元素 block 条件会 warn 后调用（`code_generator.py:1053-1057`）。它属于“兼容旧代码”的路径，新代码应显式归约。

**`tensor.to` / `cast`**：`to`（`1161-1166`）只是 `cast` 的别名，真正实现在 `semantic.cast`（Ch 6）。Python 层不做类型转换。

**descriptor 的 atomics / gather / scatter**：`tensor_descriptor_base` 上有一组 `atomic_*`、`gather`、`scatter`（`1516-1544`），全部转发到 `semantic.descriptor_*`。它们对架构有额外要求，T4 上不要假设可用。

## 对比与取舍

| 选择 | 优点 | 代价 | 什么时候用 |
|---|---|---|---|
| `tl.tensor` + 手工地址 | 兼容性最好，任何架构可用 | 地址计算/掩码全手写，易错 | 非连续访问、gather、老代码 |
| `tensor_descriptor` | 形状/步长交给硬件或运行时，代码短 | 有 16 字节对齐、last stride=1 等约束；TMA 需 SM90+ | 2D/3D 规则块搬移 |
| `constexpr` 参数 | 零运行时开销，可做模板分支 | 每个取值一份编译产物 | block size、开关、dtype 选择 |
| 运行时标量参数 | 一份产物适配多组值 | 不能用于 `block_shape`、不能做编译期分支 | `n`、`M`、`N` 这类动态尺寸 |
| `tuple` 返回 | 一次返回多个值，IR 扁平 | Python 参数个数 ≠ kernel 参数个数 | 多结果辅助函数 |
| namedtuple | 字段名可读，loc 更清晰 | 仍然是位置参数 | 结构化的多返回值 |

一个实用判断：**需要在编译期决定的东西用 `constexpr`，需要在运行时变化的东西用标量参数**。把 `BLOCK` 写成运行时参数不会报错，但 `tl.arange(0, BLOCK)` 会直接失败，因为 `arange` 要求编译期常量。

## 踩雷集锦

**1. `tl.make_block_ptr` / `tl.advance` 已经删除。**

```python
tl.make_block_ptr(...)
# NotImplementedError: Block pointers have been removed in favor of the tensor descriptor API
```

`源码依据`：`core.py:2524-2528`；`advance` 在 `2536-2543` 抛同样的错。网上大量旧教程仍在用 block pointer，在 3.9 上直接失败。

**2. `0d block_type` 被禁止。** 手工构造空 shape 的 block 会报错：

```python
tl.block_type(tl.float32, [])
# TypeError: 0d block_type is forbidden
```

`源码依据`：`block_type.__init__` 的 `743-744`。0d 值用标量 tensor（`dtype` 作 type）表示。注意 `tl.zeros((), ...)` 和 `tl.reshape(x, [])` 都不会触发它：前者走 `splat` 的空 shape 分支直接返回标量（`semantic.py:672-677`），后者在 `core.py:2178-2179` 被转成 `_unsplat`。

**3. descriptor 的 last stride 必须是 1。**

```python
tl.make_tensor_descriptor(base, shape=[M, N], strides=[1, N], block_shape=[BM, BN])
# ValueError: Tensor descriptor last dim must be 1 but got N
```

`源码依据`：`semantic.py:2030-2032`。转置矩阵请先 `tl.trans` 或改用普通指针，不要指望 descriptor 支持任意 stride 顺序。

**4. descriptor 的最后一维必须至少 16 字节。** fp32 下 `block_shape=[BM, 2]` 只有 8 字节，报 `Descriptor block shape must have at least 16 bytes in the last dimension`。`源码依据`：`semantic.py:2024-2028`。除最后一维外的静态 stride 还必须 16 字节对齐（`1985-1992`），动态 stride 的对齐只在 `iisan` 插桩下做设备端断言（`1994-2010`）。

**5. `constexpr` 全局变量只能用 `tl.constexpr(...)` 包装，不能用类型注解。** `BLOCK: tl.constexpr = 128` 不支持，`BLOCK = tl.constexpr(128)` 才支持。`源码依据`：`code_generator.py:426-433` 的报错文本明确区分了这两种写法；注解在模块层只是 Python 注解，不会变成 `constexpr` 对象。

**6. `tensor.shape` 的元素是 `constexpr`，不是 `int`。** `x.shape[0] + 1` 得到 `constexpr`，但 `range(0, x.shape[0])` 会失败。需要真整数时用 `_unwrap_if_constexpr(x.shape[0])`，或在 kernel 外算好再传进来。`tensor.__init__` 在 `916` 把 shape 全部包成 `constexpr`。

**7. `tensor.T` 不能直接用。** `x.T` 会命中 `core.py:1157-1158` 的 `assert False`（"Transposition must be created by the AST Visitor"）。转置必须写成 `tl.trans(x)` 或 `x.trans(...)`，由 `visit_Attribute` 特殊处理（`code_generator.py:177-190`）。

## 进阶

- 读 `tuple_type._unflatten_ir`（`814-819`）和 `tensor_descriptor_type._unflatten_ir`（`1553-1561`），对照 `flatten_values_to_ir` 验证 cursor 必须严格对齐。
- 读 `tensor.__getitem__`（`1135-1159`）的三种合法索引：`None`（expand_dims）、全 `slice(None)`（保持维）、`constexpr`（报错）。它比 NumPy 窄得多。
- 读 `semantic.make_tensor_descriptor`（`2013-2059`）与 `third_party/nvidia/backend/driver.py:202-262` 的 `make_tensordesc_arg`，对照 host 侧如何填 `CUtensorMap`。

## 动手练习

1. **画对象图**：给 `x = tl.load(p + tl.arange(0, 64))` 画出 `tensor` 的 `handle/type/dtype/shape/numel`，标出哪些是 MLIR 对象。**guided**。
2. **数 handle**：`desc = tl.make_tensor_descriptor(base, shape=[M, N], strides=[N, 1], block_shape=[BM, BN])` 在 `_flatten_ir` 之后产生几个 handle？写出顺序。**guided**（答案依据 `1595-1598`）。
3. **触发检查**：分别构造 last stride ≠ 1、最后一维 <16 字节、手工 0d block_type 三个失败例子，记录报错文本与源码行号。**guided**。
4. **constexpr 传播**：写一个 `@triton.jit` 辅助函数，参数 `S: tl.constexpr`，在函数内用 `tl.arange(0, S)`；再从另一个 kernel 用 `S=64` 和 `S=128` 调用，推断会生成几个 mangled 函数。**guided**。
5. **tuple 摊平**：写一个返回 `(tensor, constexpr)` 的辅助函数，说明调用点为什么只看到 1 个 MLIR result，而 `tuple_type` 有 2 个成员。**guided**。

## 本章重点

- `tensor` 只是 `ir.value` + 类型描述的薄包装；它不持有数据。
- `base_value` 负责“摊平成 handle”，`base_type` 负责“摊平成 ir.type 并反序列化”；两者必须对称。
- `constexpr` 是 `base_value` 但没有 handle，`_flatten_ir` 是空操作，因此不占参数、不占寄存器。
- `tuple` / `tensor_descriptor` 通过 flatten 变成多个 handle；descriptor 是“1 + N shape + N stride”。
- `dtype.to_ir` 把 Python 类型映射到 MLIR 类型；`int8`/`uint8` 共用 MLIR 类型，符号性靠 op 属性。
- `tl.load` / `tl.store` / `tl.dot` 的 Python 层只做参数整形与形状断言，真正的类型规则在 `semantic.py`。
- descriptor 在 sm_75 上不走 TMA，ABI 走 `DECOMPOSED`（`guided`，依据见本章「底层机制」第 4 节）。

## 自我检核

1. `tensor.dtype` 和 `tensor.type` 在什么情况下相同？什么情况下不同？
2. 为什么 `constexpr_type._unflatten_ir` 消费 0 个 handle，却仍然要返回 `constexpr(value)`？
3. `tuple` 的 `_flatten_ir` 与 `tuple_type._unflatten_ir` 如果顺序不一致，会在哪里炸？
4. `pointer_type` 为什么继承 `dtype` 而不是 `block_type`？`pointer_type.scalar` 返回什么？
5. descriptor 的 `shape` / `strides` 为什么必须作为 kernel 参数传递，而不能只存在 host 侧？
6. 在 T4 上，`tl.make_tensor_descriptor` 的哪些检查仍然会执行？哪些硬件能力不会被使用？

## 延伸阅读

1. **`python/triton/language/core.py:156-243`**
   - 读哪里：`base_value`、`base_type`、`constexpr_type`、`constexpr`。
   - 学什么：值/类型协议的最小契约，以及 `constexpr` 为什么能“零成本”。
   - 与本章关系：整章的地基；后面所有 flatten/unflatten 都建立在这里。

2. **`python/triton/language/core.py:885-1411`**
   - 读哪里：`tensor` 的 `__init__`、`_flatten_ir`、`__getitem__`；`tuple` 的全部方法。
   - 学什么：handle 包装、shape 的 `constexpr` 化、tuple 的 Python 层索引与字段访问。
   - 与本章关系：本章的两个主角；也是踩雷 6/7 的来源。

3. **`python/triton/language/semantic.py:2013-2059`**
   - 读哪里：`make_tensor_descriptor` 的全部检查。
   - 学什么：descriptor 的合法性边界（rank、对齐、last stride、静态 block）。
   - 与本章关系：把“Python 对象”变成“IR op + 类型”的最后一步。

4. **`python/triton/backends/driver.py:10-13`、`third_party/nvidia/backend/driver.py:202-270`**
   - 读哪里：`decompose_descriptor`、`make_tensordesc_arg`、`CudaLauncher.__init__` 的 ABI 选择。
   - 学什么：descriptor 在 ABI 上如何被摊成参数；TMA 与 DECOMPOSED 的分叉点。
   - 与本章关系：解释“一个 descriptor 参数为什么变成多个 kernel 参数”，并接到 Ch 8 的 launcher。

## 下一章

值模型解决了“编译期对象长什么样”。下一步是这些对象如何变成一次真正的 GPU launch：grid 怎么规范化、参数怎么打包、`CompiledKernel` 怎么拿到 CUfunction、autotuner 怎么选 config。

→ [Ch 8 运行时：driver、launcher、CompiledKernel、autotuner](./08-runtime-driver-launcher-and-autotuner.md)
