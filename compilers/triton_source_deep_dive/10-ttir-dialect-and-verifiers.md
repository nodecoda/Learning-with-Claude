# Ch 10 — TTIR 方言：类型、op、属性与验证器

> TTIR 不是“更漂亮的 Python”，而是前端和后端之间那份必须能被机器检查的合同。

## 目标

读完本章，你应该能：

- 说清楚 TTIR 的 `!tt.ptr`、builtin `tensor`、`!tt.tensordesc` 各自表达什么；
- 读懂 `tt.load`、`tt.store`、`tt.dot`、`tt.reduce`、descriptor op、`tt.func`/`tt.return` 的 ODS 定义；
- 区分 parser/type inference、ODS trait、op 自定义 `verify()`、region verifier、interface verifier 五层检查；
- 手写一段 TTIR，用 `triton-opt` 验证，并故意写错 shape/返回类型看报错；
- 解释 `tt.divisibility`/`tt.contiguity` 是什么、谁消费它们、写错了会怎样；
- 说清楚 TTIR 为什么是“tile 级”而不是“线程级”，以及 trait/interface 如何驱动后续 pass。

本章的主线是 `include/triton/Dialect/Triton/IR/TritonOps.td` 和 `TritonTypes.td`：ODS 定义决定 op 长什么样，`lib/Dialect/Triton/IR/Ops.cpp` 决定额外检查什么。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 `3.9.0`。
- 机器：Tesla T4（`sm_75`），驱动 535.216.01，CUDA 12.4，系统 Python 3.10.12。
- 基础环境没有安装 3.9.0 的 `triton`，但本机有一个 3.6.0 的 `triton-opt`：`/home/ubuntu/flagai/builds/t4-smoke/cmake/bin/triton-opt`。本章标 `实测` 的 TTIR 解析/验证输出用它跑；标 `源码依据` 的行号来自 3.9.0 源码。

**命名约定**：`TritonOps.td` 指 `include/triton/Dialect/Triton/IR/TritonOps.td`，`TritonTypes.td` 指同目录的 `TritonTypes.td`，`Ops.cpp` 指 `lib/Dialect/Triton/IR/Ops.cpp`。
- 3.6.0 与 3.9.0 在 descriptor 类型的文本格式上有差异：3.6.0 是 `!tt.tensordesc<tensor<64x64xf16>>`，3.9.0 是 `!tt.tensordesc<64x64xf16>`（`lib/Dialect/Triton/IR/Types.cpp:29-55`）。涉及 descriptor 的例子标 `guided`。
- T4 是 `sm_75`，没有 TMA；descriptor op 在 T4 上会被 `RewriteTensorDescriptorToPointer` 改写成指针访问（`third_party/nvidia/backend/compiler.py:295-296`），所以本章只讨论 TTIR 的类型/验证，不讨论 TMA 性能。

**实测**：

```bash
$ /home/ubuntu/flagai/builds/t4-smoke/cmake/bin/triton-opt --version | head -2
LLVM (http://llvm.org/):
  LLVM version 22.0.0git
```

## 为什么需要这个

TTIR 是三条链的交汇点：

```text
Python AST -> semantic.py -> TTIR -> TTGIR -> LLIR -> PTX -> cubin
```

- 对前端：TTIR 是 `semantic.py` 构造 op 的目标；类型提升、广播、mask 都在这里落成具体 op。
- 对后端：TTIR 是 layout 分配、优化、lowering 的输入；TTGIR 才引入 `#ttg.blocked` 等 encoding。
- 对调试：TTIR 是“用户代码还看得懂”的最后一层；再往下就是 layout 和线程级细节。

如果 TTIR 不合法，后面所有 pass 都可能在奇怪的地方崩。Triton 的做法是**尽量早检查**：

- ODS 的类型约束和 trait 检查 operand/result 的 shape、类型、编码一致性；
- op 的 `verify()` 检查 cache policy、dot bit width、reduce axis、return 签名；
- region verifier 检查 `tt.reduce` 的 combine block；
- interface verifier 检查 `tt.dot` 的 M/N/K 维度。

本章就是把“谁检查什么”分清楚。

## 先建立直觉

先看一段真实的 vector-add TTIR（**实测**，`triton-opt` 解析通过）：

```mlir
module {
  tt.func public @add_kernel(%x: !tt.ptr<f32> {tt.divisibility = 16 : i32},
                             %y: !tt.ptr<f32> {tt.divisibility = 16 : i32},
                             %out: !tt.ptr<f32> {tt.divisibility = 16 : i32},
                             %n: i32) {
    %pid = tt.get_program_id x : i32
    %c1024 = arith.constant 1024 : i32
    %base = arith.muli %pid, %c1024 : i32
    %r = tt.make_range {start = 0 : i32, end = 1024 : i32} : tensor<1024xi32>
    %b = tt.splat %base : i32 -> tensor<1024xi32>
    %offs = arith.addi %b, %r : tensor<1024xi32>
    %np = tt.splat %n : i32 -> tensor<1024xi32>
    %mask = arith.cmpi slt, %offs, %np : tensor<1024xi32>
    %xp = tt.splat %x : !tt.ptr<f32> -> tensor<1024x!tt.ptr<f32>>
    %yp = tt.splat %y : !tt.ptr<f32> -> tensor<1024x!tt.ptr<f32>>
    %op = tt.splat %out : !tt.ptr<f32> -> tensor<1024x!tt.ptr<f32>>
    %xa = tt.addptr %xp, %offs : tensor<1024x!tt.ptr<f32>>, tensor<1024xi32>
    %ya = tt.addptr %yp, %offs : tensor<1024x!tt.ptr<f32>>, tensor<1024xi32>
    %oa = tt.addptr %op, %offs : tensor<1024x!tt.ptr<f32>>, tensor<1024xi32>
    %xv = tt.load %xa, %mask : tensor<1024x!tt.ptr<f32>>
    %yv = tt.load %ya, %mask : tensor<1024x!tt.ptr<f32>>
    %sum = arith.addf %xv, %yv : tensor<1024xf32>
    tt.store %oa, %sum, %mask : tensor<1024x!tt.ptr<f32>>
    tt.return
  }
}
```

这段 IR 里最重要的不是语法，而是**一个 `tt.load` 代表 1024 个元素**：

- `%xa` 是 `tensor<1024x!tt.ptr<f32>>`，不是“某个线程手里的 4 个指针”；
- `%xv` 是 `tensor<1024xf32>`，不是 `float4`；
- TTIR 里没有 `threadIdx.x`，只有 `tt.get_program_id`（`TritonOps.td:720`）；
- 线程怎么分、每个线程拿几个元素、用什么 layout，要到 TTGIR 才出现。

所以 TTIR 是 **tile 级**：一个 op 描述一整块 tile 的语义，后端再决定这块 tile 怎么摊到线程上。

## 核心概念

### 1. 类型：`!tt.ptr`、`tensor`、`!tt.tensordesc`

**指针**：`!tt.ptr<f32>` 是标量指针，`tensor<1024x!tt.ptr<f32>>` 是指针 tile。`PointerType` 只有两个参数：pointee type 和 address space（`TritonTypes.td:63-90`）。默认 address space 是 `Global`，descriptor 用 `"descriptor"`（`Types.cpp:67-96`）。verifier 要求 pointee 是整数/浮点/`PackedFloat4E2M1Type`（`Types.cpp:117-123`）。

**张量**：TTIR 没有 `tt.tensor` 这个类型。文本里写的是 MLIR builtin 的 `tensor<...>`；`TT_Tensor` 只是 TableGen 的约束，定义是 `RankedTensorOf<[TT_Float, TT_Int, TT_Ptr, TT_FP4E2M1x2]>`（`TritonTypes.td:101-106`）。所以看到 `tt.tensor` 的说法，要理解成“TTIR 的 tensor 类型约束”，不是 dialect type。

**descriptor**：`!tt.tensordesc<64x64xf32>` 是 TMA descriptor 的可移植抽象，参数是 block shape、element type、可选 shared layout（`TritonTypes.td:116-176`）。verifier 禁止 descriptor 再包一层 tensor 类型（`Types.cpp:105-115`）。

### 2. op：load/store/dot/reduce/descriptor/func

| op | 作用 | ODS 位置 |
|---|---|---|
| `tt.load` | 从标量指针或指针 tile 读 | `TritonOps.td:257-302` |
| `tt.store` | 写回标量指针或指针 tile | `TritonOps.td:304-339` |
| `tt.dot` | 矩阵乘加 `d = a*b + c` | `TritonOps.td:762-798` |
| `tt.reduce` | 带 combine region 的归约 | `TritonOps.td:856-881` |
| `tt.make_tensor_descriptor` | 构造 descriptor | `TritonOps.td:1119-1146` |
| `tt.descriptor_load/store` | 通过 descriptor 读写 | `TritonOps.td:1362-1403` |
| `tt.func`/`tt.return` | 函数与返回 | `TritonOps.td:1232-1359` |

`tt.load` 的 operands 是 `ptr`、可选 `mask`、可选 `other`、可选 `cachePolicy`、`isVolatile`（`TritonOps.td:272-279`）。`tt.store` 是 `ptr`、`value`、可选 `mask`、`cachePolicy`、`ignore_cta`（`TritonOps.td:316-322`）。注意 `tt.store` 的 `ptr` 带 `MemWrite<GlobalMemory>`，这是 `MemoryEffectsOpInterface` 的声明，不是注释。

### 3. 属性：`tt.divisibility`、`tt.contiguity`、`cache`、`axis`

- `tt.divisibility = 16 : i32`：告诉编译器“每组连续元素的首地址能被 16 整除”；
- `tt.contiguity = 4 : i32`：告诉编译器“沿某维最短连续段长度是 4”；
- `cache = ...`：load/store 的 cache policy，由 `verifyCachePolicy` 检查（`Ops.cpp:133-136`、`188-191`）；
- `axis = 1 : i32`：reduce 的归约维；
- `inputPrecision`、`maxNumImpreciseAcc`：dot 的精度控制（`TritonOps.td:780-787`）。

`tt.divisibility`/`tt.contiguity` 不是“验证出来的事实”，而是**前端给后端的假设**。它们由 `AxisInfo` 消费：

```cpp
AxisInfo::initDimVectorFromHint(op->getDiscardableAttr("tt.contiguity"), &newContiguity);
AxisInfo::initDimVectorFromHint(op->getDiscardableAttr("tt.divisibility"), &newDivisibility);
```

**源码依据**：`lib/Analysis/AxisInfo.cpp:1305-1316`。

`AxisInfo` 还会把结果写回 callee 的参数属性（`AxisInfo.cpp:1597-1599`）。TTIR -> TTGIR 的转换会把 named attrs 原样带过去（`TritonToTritonGPUPass.cpp:26-31`）。

### 4. trait/interface：让 pass 不用认识每个 op

- `Pure`：无副作用，可 DCE/CSE；
- `SameOperandsAndResultShape` / `SameOperandsAndResultEncoding`：shape/encoding 必须一致；
- `MemoryEffectsOpInterface`：声明读/写全局内存；
- `PredicatedOpInterface`：有 mask/predicate，pipeliner 用它做谓词化（`PipeliningUtility.cpp:199`）；
- `InferTypeOpInterface`：结果类型可由 operands 推导，pass 可重新推断（`Utility.cpp:877`）；
- `DotOpInterface`：dot 家族的统一接口，layout pass 用它识别 anchor（`RemoveLayoutConversions.cpp:230`），verifier 用它检查 M/N/K（`OpInterfaces.cpp:49-84`）。

这些 trait/interface 是“pass 的公共语言”。没有它们，每个 pass 都要 `isa<LoadOp> || isa<StoreOp> || ...`，很快就会失控。

### 5. verifier 的五层

1. **parser/type inference**：文本解析时就把 operand 类型和 op 期望对上；对不上会在 parser 报错。
2. **ODS trait**：`SameOperandsAndResultShape`、`TypesMatchWith` 等生成 `verifyInvariants`。
3. **op 自定义 `verify()`**：cache policy、dot bit width/encoding、reduce axis、return 签名、descriptor numel。
4. **region verifier**：`tt.reduce` 的 combine block 参数/终结符/返回类型。
5. **interface verifier**：`DotOpInterface::verify` 调 `verifyDotOpInterface`。

## 底层机制

### 1. Load/Store：trait 管 shape，`verify()` 管 cache

`TT_LoadOp` 的 trait 里有：

```tablegen
SameLoadStoreOperandsAndResultShape,
SameLoadStoreOperandsAndResultEncoding,
TypesMatchWith<"result matches ptr type", "ptr", "result", "getPointeeType($_self)">,
TypesMatchWith<"mask type matches ptr type", "ptr", "mask", "getI1SameShape(getPointeeType($_self))", ...)
```

**源码依据**：`TritonOps.td:257-268`。

这几条把“ptr tile 的 shape = result 的 shape”“mask 是 i1 且 shape 相同”“result element type = pointee type”都交给了 ODS。手写的 `LoadOp::verify()` 只做一件事：

```cpp
return verifyCachePolicy(*this, getCachePolicyAttr(), CachePolicyOperation::Load);
```

**源码依据**：`Ops.cpp:133-136`。

`StoreOp::verify()` 同样只检查 cache policy（`Ops.cpp:188-191`）。所以看到 shape 错误时，不要先去 `Ops.cpp` 找 `LoadOp::verify`；那是 ODS trait/type inference 的活。

### 2. Dot：interface 管维度，`verify()` 管 bit width/encoding

`TT_DotOp` 声明了 `DotOpInterface`（`TritonOps.td:762-766`）。interface 的 verifier 在 `OpInterfaces.cpp:49-84`：

- 至少 3 个 operands；
- rank 必须是 2 或 3；
- A/B/C rank 相同；
- `verifyDims()`：`aShape.back() == bShape[bShape.size()-2]`；
- 3D 时 batch 维一致；
- `verifyOutputDims()`：C 的 M/N 与 A/B 对应。

`DotOp::verify()` 自己检查：

- A/B element bit width 相同，否则报 `element types of operands A and B must have same bit width`；
- 如果 A/B 都没有 encoding，直接成功（TTIR 阶段允许无 encoding）；
- 只有一个有 encoding，报 `mismatching encoding between A and B operands`；
- C 没有 encoding，报 `miss encoding of C operand`；
- 都有 encoding 时，调 `DialectInferLayoutInterface::verifyDotOpEncodingCompatibility`。

**源码依据**：`Ops.cpp:378-400`。

这就是“TTIR 不检查 layout，TTGIR 才检查 layout”的源码证据：`if (!aEncoding && !bEncoding) return success();`（`Ops.cpp:387-388`）。

### 3. Reduce：axis/rank 在 `verify()`，combine block 在 `verifyRegions()`

`ReduceOp::verify()` 调 `verifyReduceScan`（`Ops.cpp:667-694`）：

- 至少 1 个 operand；
- operand 数 = result 数；
- `axis` 在 `[0, rank)`；
- 所有 operand rank 相同；
- operand element type = result element type。

`ReduceOp::verifyRegions()` 调 `verifyRegionsImpl<ReduceReturnOp>`（`Ops.cpp:696-739`）：

- combine block 的参数数 = `2 * operands.size()`；
- 参数类型按 operand element type 交替；
- 终结符必须是 `ReduceReturnOp`；
- 返回数量 = operand 数；
- 每个返回类型 = 对应 element type。

这解释了为什么 `tt.reduce` 的 region 不能随便写：它不是普通 block，而是有严格签名约定的 combine function。

### 4. Descriptor：类型 verifier + op verifier

`TensorDescType::verify` 只做一件事：禁止 descriptor 包 tensor 类型（`Types.cpp:105-115`）。`DescriptorLoadOp::verify`/`StoreOp::verify`/`ReduceOp::verify` 都走 `verifyDescriptorLoadStoreOp`（`Ops.cpp:1892-1931`）：

- descriptor block 的 element type 必须等于 tensor 的 element type；
- descriptor block 的 numel 必须等于 tensor 的 numel；
- 不要求 shape 完全一样，只要求元素总数一样。

**源码依据**：`Ops.cpp:1892-1915`。

`tt.make_tensor_descriptor` 本身没有手写 verifier（`TritonOps.td:1119-1146`），它靠 ODS 的 operand/result 类型约束：`base` 是 `TT_Ptr`，`shape` 是 `Variadic<I32>`，`strides` 是 `Variadic<I64>`，result 是 `TT_TensorDescType`。

### 5. Func/Return：region 隔离和签名检查

`FuncOp` 的 trait 里有 `IsolatedFromAbove`（`TritonOps.td:1232-1236`）。意思是函数体不能隐式捕获外面的 SSA value；外部引用只能走参数或 symbol。

`ReturnOp` 的 verifier 检查：

- operand 数 = 函数签名 result 数；
- 每个 operand 类型 = 对应 result 类型。

**源码依据**：`Ops.cpp:1534-1552`。

`tt.return` 还有 `HasParent<"FuncOp">` 和 `Terminator`（`TritonOps.td:1333`），所以它只能出现在 `tt.func` 的 block 末尾。

### 6. 属性如何驱动 AxisInfo

`tt.divisibility`/`tt.contiguity` 的语义在 `AxisInfo.h:45-92`：

- `contiguity[d]`：沿第 d 维最短连续整数段的长度，必须是 2 的幂；
- `divisibility[d]`：每组连续段首元素能被多大的 2 的幂整除。

它们不是 verifier 检查的“正确性条件”，而是优化假设。写错了不会在 TTIR 报错，但可能让 Coalesce/vectorize 生成错误的对齐假设。

### 7. TTIR -> TTGIR：encoding 是什么时候加上的

TTIR 的 tensor 没有 encoding。`ConvertTritonToTritonGPU` 用 `TritonGPUTypeConverter` 给 tensor 加 `#ttg.blocked` 等 encoding：

```cpp
TritonGPUTypeConverter typeConverter(context, numWarps, threadsPerWarp, numCTAs, enableSourceRemat);
...
applyPartialConversion(mod, target, std::move(patterns), config);
```

**源码依据**：`lib/Conversion/TritonToTritonGPU/TritonToTritonGPUPass.cpp:699-727`。

所以：

- TTIR：`tensor<1024xf32>`，tile 级语义；
- TTGIR：`tensor<1024xf32, #ttg.blocked<...>>`，开始有 layout；
- LLIR：tensor 被拆成每个线程的标量/struct（Ch 11 的 TypeConverter）。

## 进一步用法/重要变体

### 1. 手写 TTIR 并用 `triton-opt` 验证

把上面的 vector-add 存成 `add.ttir`，运行：

```bash
triton-opt add.ttir -o /dev/null
```

**实测**：exit code `0`，无输出。说明类型、shape、terminator 都合法。

### 2. 故意写错 dot 的 K 维

```mlir
%a = arith.constant dense<0.0> : tensor<16x32xf32>
%b = arith.constant dense<0.0> : tensor<16x32xf32>
%c = arith.constant dense<0.0> : tensor<16x16xf32>
%d = tt.dot %a, %b, %c : tensor<16x32xf32> * tensor<16x32xf32> -> tensor<16x16xf32>
```

**实测**：

```text
error: 'tt.dot' op expected the last dimension of the first operand to be equal to the second-to-last dimension of the second operand
```

这是 `DotOpInterface` 的 `verifyDims()` 报的，不是 `DotOp::verify()`。**源码依据**：`OpInterfaces.cpp:67-71`。

### 3. 故意写错 `tt.return`

```mlir
tt.func public @bad_return() -> i32 {
  %c = arith.constant 0.0 : f32
  tt.return %c : f32
}
```

**实测**：

```text
error: type of return operand 0 ('f32') doesn't match function result type ('i32') in function @bad_return
```

**源码依据**：`Ops.cpp:1534-1552`。

### 4. shape 错误有时在 parser 就报了

```mlir
%m = arith.constant dense<true> : tensor<512xi1>
tt.store %oa, %v, %m : tensor<1024x!tt.ptr<f32>>
```

**实测**：

```text
error: use of value '%m' expects different type than prior uses: 'tensor<1024xi1>' vs 'tensor<512xi1>'
```

这说明 parser 在构造 op 时已经用 op 的期望类型去约束 operand；不是所有 shape 错误都能走到 `verify()`。

### 5. `tt.divisibility`/`tt.contiguity` 的合法写法

```mlir
tt.func public @attrs(%p: !tt.ptr<f32> {tt.divisibility = 16 : i32, tt.contiguity = 4 : i32},
                      %n: i32 {tt.divisibility = 16 : i32}) {
  ...
}
```

**实测**：`triton-opt` exit code `0`。这些是 discardable attrs，verifier 不会检查它们的真假；它们只影响 `AxisInfo`。

### 6. `tt.reduce` 的 region 写法

```mlir
%s = "tt.reduce"(%e) <{axis = 1 : i32}> ({
^bb0(%a: f32, %b: f32):
  %m = arith.addf %a, %b : f32
  tt.reduce.return %m : f32
}) : (tensor<64x128xf32>) -> tensor<64xf32>
```

**实测**：`triton-opt` exit code `0`。注意 `tt.reduce.return` 只能返回 element type，不是 tensor type；返回数量和 operand 数必须一致。

### 7. descriptor 例子（guided）

3.9.0 的文本格式：

```mlir
%sd = tt.make_tensor_descriptor %src, [%c128, %c64], [%s0, %s1] : <f32>, <64x64xf32>
%v = tt.descriptor_load %sd[%i0, %i1] : !tt.tensordesc<64x64xf32> -> tensor<64x64xf32>
tt.descriptor_store %dd[%i0, %i1], %v : !tt.tensordesc<64x64xf32>, tensor<64x64xf32>
```

**guided**：本机 3.6.0 的 `triton-opt` 用旧格式 `!tt.tensordesc<tensor<...>>`，不能直接验证这段；3.9.0 的 parser 在 `Types.cpp:29-55`，descriptor op 在 `TritonOps.td:1119-1146`、`1362-1403`。T4 没有 TMA，这段只用于理解类型/验证，不要期待在 T4 上生成 TMA 指令。

## 对比与取舍

| 维度 | TTIR | TTGIR |
|---|---|---|
| tensor 类型 | `tensor<1024xf32>` | `tensor<1024xf32, #ttg.blocked<...>>` |
| 线程 | 不出现 | layout 决定每线程元素 |
| dot 检查 | bit width + 无 encoding 时跳过 | encoding 兼容性 |
| 主要 pass | inliner、canonicalizer、combine、loop unroll | coalesce、remove-layout-conversions、accelerate-matmul |
| 调试价值 | 看前端生成了什么 op | 看 layout 怎么传播 |

| 选择 | 优点 | 代价 |
|---|---|---|
| 用 trait 表达 shape/类型约束 | 自动生成 verifier，op 定义短 | 报错信息偏模板化 |
| 用自定义 `verify()` | 能写领域检查、错误信息具体 | 要自己维护，容易漏 |
| 用 interface verifier | dot 家族共享检查 | 间接一层，初读不直观 |
| 用 region verifier | 能检查 combine block 签名 | 只对带 region 的 op 有用 |

## 踩雷集锦

1. **以为 TTIR 有 `tt.tensor` 类型。** 文本里是 builtin `tensor<...>`；`TT_Tensor` 只是 TableGen 约束（`TritonTypes.td:101-106`）。写 `!tt.tensor<...>` 会直接 parse 失败。
2. **在 `LoadOp::verify()` 里找 shape 检查。** shape/mask/result 的约束在 ODS trait 里（`TritonOps.td:257-268`）；`LoadOp::verify()` 只检查 cache policy（`Ops.cpp:133-136`）。
3. **以为 TTIR 的 `tt.dot` 会检查 layout。** A/B 都没有 encoding 时 `DotOp::verify()` 直接成功（`Ops.cpp:387-388`）；layout 兼容性要到 TTGIR 才检查。
4. **把 `tt.divisibility` 当成“编译器会验证的承诺”。** 它只是 `AxisInfo` 的 hint（`AxisInfo.cpp:1305-1316`）；写错不会报错，但可能生成错误的向量化/对齐假设。
5. **`tt.reduce` 的 region 返回 tensor。** `tt.reduce.return` 返回的是 element type，不是 tensor type；`verifyRegionsImpl` 会检查返回类型等于 operand element type（`Ops.cpp:729-737`）。
6. **在 `tt.func` 里引用外面的 SSA value。** `FuncOp` 有 `IsolatedFromAbove`（`TritonOps.td:1232-1236`），外部值只能通过参数传入。
7. **用 3.6.0 的 descriptor 语法写 3.9.0 的 TTIR。** 3.6.0 是 `!tt.tensordesc<tensor<...>>`，3.9.0 是 `!tt.tensordesc<...>`（`Types.cpp:29-55`）。跨版本抄 IR 要先看 parser。

## 进阶

- 读 `TritonOps.td` 的 `TT_LoadOp`/`TT_StoreOp` builders（`283-293`、`324-330`），对比 `Ops.cpp` 里手写的 builder（`121-125`、`170-180`），理解 ODS 生成和手写重载怎么共存。
- 读 `DotScaledOp::verifyDims`/`verifyOutputDims`（`Ops.cpp:403-440`），看 fp4 packed pair 如何改变 K 维计算；这是 TTIR 里少见的“数据格式影响维度”的例子。
- 读 `verifyReduceScan`（`Ops.cpp:667-694`）和 `verifyRegionsImpl`（`696-739`），自己写一个多输入 reduce 的 TTIR，观察 combine block 参数怎么排。
- 读 `TritonToTritonGPUPass.cpp` 的 `addNamedAttrs`（`26-31`）和 `TritonGPUTypeConverter`（`699-701`），理解 `tt.divisibility` 如何从 TTIR 带到 TTGIR。
- 用 `triton-opt -mlir-print-op-generic` 打印 generic form，对比 custom assembly format，理解 ODS 的 `assemblyFormat` 到底生成了什么。

## 动手练习

1. **手写 TTIR**：把本章的 vector-add 写进 `add.ttir`，用 `triton-opt add.ttir -o /dev/null` 验证；然后删掉 `tt.return`，看报错。**实测（3.6.0 可跑）**。
2. **制造 dot 错误**：把 `%b` 改成 `tensor<16x16xf32>`，记录 `verifyDims` 的报错；再改成 `tensor<16x32xf16>`，记录 bit width 报错。**实测（3.6.0 可跑）**。
3. **制造 return 错误**：写一个 `-> (i32, i32)` 但只返回一个值的 `tt.func`，记录 `tt.return` 的 operand 数报错。**实测（3.6.0 可跑）**。
4. **属性实验**：给同一个指针参数加/不加 `tt.divisibility = 16`，dump TTGIR，比较 `#ttg.blocked` 或 vectorization 是否变化。**guided**。
5. **reduce region**：写一个 2D `tt.reduce`，把 `tt.reduce.return` 改成返回 tensor，观察 region verifier 报错。**guided（3.6.0 的 reduce verifier 在部分错误路径会崩，建议以 3.9.0 源码为准）**。
6. **descriptor 类型**：按 3.9.0 格式写 `!tt.tensordesc<64x64xf32>`，用 3.9.0 的 `triton-opt` 验证；本机只能用 3.6.0，标 guided。**guided**。

## 本章重点

- TTIR 是 tile 级 IR：一个 `tt.load` 代表一整块 tensor，不出现线程号；layout 到 TTGIR 才出现。
- `!tt.ptr<f32>` 是标量指针，`tensor<...x!tt.ptr<f32>>` 是指针 tile；TTIR 没有 `tt.tensor` 类型，只有 builtin `tensor` 和 `TT_Tensor` 约束。
- `tt.load`/`tt.store` 的 shape/type 约束主要在 ODS trait；手写 `verify()` 只管 cache policy。
- `tt.dot` 的维度检查在 `DotOpInterface`，bit width/encoding 检查在 `DotOp::verify()`；TTIR 阶段允许无 encoding。
- `tt.reduce` 的 axis/rank 在 `verify()`，combine block 签名在 `verifyRegions()`。
- `tt.func` 是 `IsolatedFromAbove`；`tt.return` 必须匹配函数签名。
- `tt.divisibility`/`tt.contiguity` 是 `AxisInfo` 的 hint，不是 verifier 检查的事实。
- descriptor op 在 T4 上会被改写成指针访问；TTIR 类型/验证与 TMA 性能要分开看。

## 自我检核

1. 为什么说 TTIR 是 tile 级而不是线程级？给出两个 IR 层面的证据。
2. `tt.load` 的 shape 检查在哪里？`LoadOp::verify()` 到底检查什么？
3. `tt.dot` 的 K 维错误由谁报？bit width 错误由谁报？
4. `tt.reduce` 的 combine block 必须满足哪些签名条件？
5. `tt.divisibility` 写错了会在哪一层报错？为什么？
6. 3.9.0 的 descriptor 类型文本格式和 3.6.0 有什么不同？

## 延伸阅读

1. **`include/triton/Dialect/Triton/IR/TritonOps.td:257-343`、`762-798`、`856-881`**
   - **读哪里**：`TT_LoadOp`、`TT_StoreOp`、`TT_DotOp`、`TT_ReduceOp`。
   - **学什么**：ODS 如何用 trait、type constraint、region、assemblyFormat 定义一个 op。
   - **与本章关系**：这是 TTIR 的“法律条文”；后面所有 verifier 行为都能在这里找到根。

2. **`include/triton/Dialect/Triton/IR/TritonTypes.td:63-176`**
   - **读哪里**：`TT_PtrType`、`TT_Ptr`、`TT_Tensor`、`TT_TensorDescType`。
   - **学什么**：TTIR 类型系统的参数、约束和自定义 assembly format。
   - **与本章关系**：解释 `!tt.ptr<f32>`、`tensor<...>`、`!tt.tensordesc<...>` 为什么长这样。

3. **`lib/Dialect/Triton/IR/Ops.cpp:133-191`、`378-400`、`667-767`、`1534-1553`、`1892-1931`**
   - **读哪里**：Load/Store、Dot、Reduce、Return、Descriptor 的 verifier。
   - **学什么**：哪些检查是手写的，哪些交给 ODS/interface；错误信息怎么产生。
   - **与本章关系**：把“verifier 五层”落到具体函数。

4. **`lib/Dialect/Triton/IR/OpInterfaces.cpp:44-84`**
   - **读哪里**：`verifyDotOpInterface`。
   - **学什么**：interface verifier 如何统一 dot 家族的维度检查。
   - **与本章关系**：解释为什么 `tt.dot` 的 K 维错误不是 `DotOp::verify()` 报的。

5. **`lib/Analysis/AxisInfo.cpp:1305-1316`、`1597-1599`**
   - **读哪里**：hint 覆盖和属性写回。
   - **学什么**：`tt.divisibility`/`tt.contiguity` 如何进入 axis 分析。
   - **与本章关系**：连接 TTIR 属性和后续 layout/vectorization。

## 下一章

下一章从“方言定义”转到“MLIR 对象模型”：用 Triton 的真实代码讲 `Context`、`ModuleOp`、`Operation`、`Region`、`Block`、`Value`、`Attribute`、`Type`、`Builder`、`IRMapping`、`PatternRewriter`，以及为什么不能在 `walk` 里随手 `erase`。

→ [Ch 11 MLIR 对象模型在 Triton 中的落点](./11-mlir-object-model-in-triton.md)
