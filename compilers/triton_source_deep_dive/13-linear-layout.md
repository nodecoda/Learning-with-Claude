# Ch 13 — LinearLayout：把布局变成可计算的矩阵

> 一旦布局是线性的，比较两个布局就是比较两个矩阵；组合布局就是矩阵乘法；求“从 A 布局搬到 B 布局”就是求逆再复合。

## 目标

读完本章，你应该能：

- 把 `(register, lane, warp, block)` 的位向量写成 GF(2) 上的矩阵；
- 给定一组 base/basis，手算任意线程、任意寄存器的元素坐标；
- 解释 `#ttg.linear` 的序列化形式，以及它和 `#ttg.blocked` 的等价关系；
- 说清楚为什么线性布局能判断“两个布局是否等价”、能做组合、能求逆；
- 看懂 `BlockedEncodingAttr::toLinearLayout`、`NvidiaMmaEncodingAttr::toLinearLayout`、`SliceEncodingAttr::toLinearLayout` 的实现；
- 解释 `removeZeroBasesAlongDim("register")` 在 `MakeRange` 和 `convert_layout` 里的作用；
- 知道 `minimalCvtLayout` 如何把一次转换分类成“寄存器重排 / warp shuffle / shared memory”。

本章的主线是 `include/triton/Tools/LinearLayout.h` 和 `lib/Dialect/TritonGPU/IR/LinearLayoutConversions.cpp`。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 3.9.0。
- 机器：Tesla T4，`sm_75`；本章的 TMEM 例子只适用于 Blackwell，标 **guided**。
- 本章源码行号以该 commit 为准，使用 `nl -ba` 核对。
- **实测**：4×4 布局的矩阵、16×16 布局的基、`tensor<2xi32>` 的 register 基，来自一个独立 Python 脚本，按 `BlockedEncodingAttr::toLinearLayout` 的公式复算；不是 Triton 工具输出。
- **源码依据**：`LinearLayout` 的数学、各 `toLinearLayout` 的实现、dispatcher、`combineCtaCgaWithShape`、`minimalCvtLayout` 都来自源码。
- **guided**：TMEM、Hopper/Blackwell 的 shared 布局、真实 GPU 上的转换代价。

## 为什么需要这个

Ch 12 里我们用“线程 → 元素”的表格理解 blocked 布局。表格很直观，但有几个问题：

1. **太大**。128×128 tensor、128 个线程，表格有 16384 项；编译器不可能对每个布局都存一张表。
2. **不好比较**。两个布局是否等价，逐项比表格是 O(元素数)；如果布局是线性映射，只需比 base。
3. **不好组合**。`convert_layout` 本质上是“把 src 布局的坐标搬到 dst 布局”，需要求逆、复合；表格做不了符号运算。
4. **不好判断代价**。一次转换是寄存器重排、warp shuffle，还是要走 shared memory？这取决于转换在 lane/warp/block 维上是否恒等；线性布局能直接算出来。

线性布局把“表格”压缩成“基向量”：

```text
L(a) = a1*B1 xor a2*B2 xor ... xor aM*BM
```

其中 `a` 是输入位向量（register/lane/warp/block 的位拼起来），`Bi` 是输出坐标的基向量，`xor` 是 GF(2) 加法。只要存 M 个基向量，就能算出 2^M 个输入对应的输出。

`源码依据`：`LinearLayout.h:190-210` 解释“为什么映射 hardware loc → tensor index，而不是反过来”；`230-290` 给出 GF(2) 的矩阵-向量乘法；`315-330` 定义 `bases[inDim][i] = L(..., inDim=2^i, ...)`。

## 先建立直觉

先看一个 4×4 的 blocked 布局：

```mlir
#b = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [2, 2], warpsPerCTA = [1, 1], order = [1, 0]}>
```

4 个线程、每个线程 4 个寄存器，覆盖 16 个元素。**实测**它的线性布局是：

```text
#ttg.linear<{
  register = [[2, 0], [0, 2]],
  lane     = [[0, 1], [1, 0]],
  warp     = [],
  block    = []
}>
```

读法：

- `register` 有 2 个 bit：bit0 让行 +2，bit1 让列 +2；
- `lane` 有 2 个 bit：bit0 让列 +1，bit1 让行 +1；
- 没有 warp/block bit。

把输入写成整数 `a = r0 + 2*r1 + 4*l0 + 8*l1`，输出就是：

```text
dim0 = (r0 ? 2 : 0) xor (l1 ? 1 : 0)
dim1 = (r1 ? 2 : 0) xor (l0 ? 1 : 0)
```

例如 lane 1（`l0=1`）的 4 个寄存器：

```text
r=0: (0,1)
r=1: (2,1)
r=2: (0,3)
r=3: (2,3)
```

这和 Ch 12 的表格一致，但这里只用了 4 个基向量。

## 核心概念

### 1. 输入维：register、lane、warp、block

Triton 的分布式线性布局固定用四个输入维：

| 输入维 | 含义 | 大小 |
|---|---|---|
| `register` | 每线程第几个寄存器 | `sizePerThread` 的乘积 |
| `lane` | warp 内第几个 lane | 通常 32 |
| `warp` | CTA 内第几个 warp | `warpsPerCTA` 的乘积 |
| `block` | CGA 内第几个 CTA | `CTAsPerCGA` 的乘积 |

`源码依据`：`LinearLayout.h:100-110` 的概述；`LinearEncodingAttr` 的输入维在 `TritonGPUAttrDefs.td:679-721`；shared 布局用 `offset`（和可选的 `block`），见 `LinearLayoutConversions.cpp:53-99`。

注意：`register` 不是“寄存器编号”这么简单。它是“这个线程持有的第几个逻辑元素”。同一个线程的多个寄存器，可能对应完全不同的 tensor 坐标。

### 2. 输出维：dim0、dim1、...

分布式布局的输出维是 `dim0, dim1, ...`，和 tensor 的 rank 一致。shared 布局也是 `dim0, dim1, ...`，但输入维是 `offset`。

`源码依据`：`standardOutDimNames` 在 `lib/Tools/LayoutUtils.cpp:201-208`；CGA 的输出维也必须是 `dim0, dim1, ...`，见 `Dialect.cpp:615-640`。

### 3. base/basis：矩阵的列

`LinearLayout` 内部存的是：

```cpp
bases[inDim][i] = L(0, ..., inDim=2^i, ..., 0)
```

也就是“只把某个输入维的第 i 个 bit 置 1，其他全 0”时的输出坐标。所有其他输入值都由这些基异或出来。

`源码依据`：`LinearLayout.h:315-330`。

把每个基向量看成一列，就得到矩阵 `B`。输入 `a` 是列向量，输出是 `B a`（GF(2) 上的矩阵-向量乘）。`LinearLayout.h:230-290` 有完整的 4×4 例子。

### 4. 为什么是 GF(2)

坐标是整数，但每个坐标的二进制位是独立的。`dim0=2` 表示“dim0 的 bit1 置 1”，`dim0=3` 表示 bit0 和 bit1 都置 1。两个坐标相加时，同一位上的 1 会进位；但我们想要的是“两个基同时选中时，坐标按位异或”，不是整数加法。

所以 Triton 把坐标看成 GF(2) 上的位向量：

- 加法是 XOR；
- 乘法是 AND；
- 每个基向量是一个位掩码。

这就是为什么 `L(a xor b) = L(a) xor L(b)`，也是为什么“组合布局”可以写成矩阵乘法。

### 5. 广播 = 零基

如果某个输入 bit 对输出没有任何影响，它的基就是全零。例如：

```text
lane = [[0, 1], [0, 0]]
```

第二个 lane bit 是零基：lane 0 和 lane 2 会映射到同一个元素。这不是 bug，而是 broadcast。

`源码依据`：`LinearLayout.h:164-180` 说 LL 不一定是单射；`removeZeroBasesAlongDim` 在 `LinearLayout.h:796`，用来在需要“唯一元素”时删掉零基。

## 底层机制

### 1. 手算 4×4 blocked 的矩阵

还是这个布局：

```mlir
#b = #ttg.blocked<{sizePerThread = [1, 1], threadsPerWarp = [2, 2], warpsPerCTA = [1, 1], order = [1, 0]}>
```

基向量：

```text
r0 = (2,0)
r1 = (0,2)
l0 = (0,1)
l1 = (1,0)
```

把输出坐标拆成 4 个 bit（dim0 bit0、dim0 bit1、dim1 bit0、dim1 bit1），矩阵 `B` 的列是 `[r0, r1, l0, l1]`：

```text
        r0 r1 l0 l1
dim0:0 [ 0  0  0  1 ]
dim0:1 [ 1  0  0  0 ]
dim1:0 [ 0  0  1  0 ]
dim1:1 [ 0  1  0  0 ]
```

验证：

```text
a = 1 (r0)  -> B a = (0,1,0,0) -> dim0 bit1 -> (2,0)  ✓
a = 2 (r1)  -> B a = (0,0,0,1) -> dim1 bit1 -> (0,2)  ✓
a = 4 (l0)  -> B a = (0,0,1,0) -> dim1 bit0 -> (0,1)  ✓
a = 8 (l1)  -> B a = (1,0,0,0) -> dim0 bit0 -> (1,0)  ✓
```

这个 `B` 是一个置换矩阵：每一行、每一列恰好一个 1。所以这个布局是双射，`isInvertible()` 为真（`LinearLayout.h:437`）。

**实测**：`B^2` 是：

```text
[ 0 1 0 0 ]
[ 0 0 0 1 ]
[ 0 0 1 0 ]
[ 1 0 0 0 ]
```

而 `B^3 = I`。所以 `B^-1 = B^2`：求逆就是再做两次同样的布局。这是“组合 = 矩阵乘法”的最小例子。

### 2. `apply`：从输入位到坐标

给定 `(reg, lane, warp, block)`，`apply` 的做法是：

```text
out = 0
for each input dim d:
  for each bit i of value[d]:
    if bit set: out ^= bases[d][i]
```

`源码依据`：`LinearLayout::apply` 在 `LinearLayout.h:719-722`；实现里把多维输入拼成一维位向量，再做 XOR。

注意：`apply` 的输入是“索引”，不是“位向量”。例如 `lane=5` 表示 bit0 和 bit2 置 1，不是“第 5 个基”。

### 3. `toLinearLayout`：blocked 怎么变成矩阵

`BlockedEncodingAttr::toLinearLayout` 只有三行核心逻辑：

```cpp
LinearLayout ctaLayout =
    identityStandardND("register", sizePerThread, order) *
    identityStandardND("lane", threadsPerWarp, order) *
    identityStandardND("warp", warpsPerCTA, order);
return combineCtaCgaWithShape(ctaLayout, getCGALayout(), shape)
    .removeZeroBasesAlongDim("register");
```

`源码依据`：`LinearLayoutConversions.cpp:869-880`。

三个 identity 按 `order` 把 register/lane/warp 的 bit 分配到各维。`combineCtaCgaWithShape` 负责三件事：

1. 如果 tensor 比 CTA tile 大，沿 `register` 维补基（repeat）；
2. 如果 tensor 比 CTA tile 小，把超出 shape 的基置零（broadcast）；
3. 最后乘上 CGA 布局，处理 CTA 之间的切分。

`源码依据`：`combineCtaCgaWithShape` 在 `LinearLayoutConversions.cpp:1335-1363`；`ensureLayoutNotSmallerThan` / `ensureLayoutNotLargerThan` 在 `lib/Tools/LayoutUtils.cpp:174-199` 和 `120-172`。

### 4. `toLinearLayout`：MMA、dot_op、slice

#### MMA

`NvidiaMmaEncodingAttr::toLinearLayout` 先用 `nvidiaMmaTile` 构造一个 warp tile，再乘 `warpsPerCTA`，最后扩到 shape：

```cpp
auto ctaLayout = nvidiaMmaTile(ctx, tileShape, kWidth, order, getRepOrder());
ctaLayout *= identityStandardND("warp", getWarpsPerCTA(), warpOrder)...
return combineCtaCgaWithShape(ctaLayout, getCGALayout(), shape);
```

`源码依据`：`LinearLayoutConversions.cpp:963-988`；`nvidiaMmaTile` 在 `922-961`。

对 v2，`tileShape` 是 `instrShape`（16×8）；`kWidth=2`；tile 的基按“inner dim 2 个 register、4 个 lane；outer dim 8 个 lane、m/8 个 register、n/(kWidth*4) 个 register”排列。这就是 Ch 12 那张 MMA 坐标表的来源。

#### dot_op

`DotOperandEncodingAttr::toLinearLayout` 按 parent 分派：

- parent 是 blocked：`fmaDotToLinearLayout`；
- parent 是 `MmaEncodingTrait`：`mma.dotOperandToLinearLayout`；
- 其他：`report_fatal_error`。

`源码依据`：`LinearLayoutConversions.cpp:1026-1035`；FMA 版本在 `882-920`；MMA 版本在 `990-1024`。

#### slice

`SliceEncodingAttr::toLinearLayout` 先把 parent shape 在 `dim` 处插一个 1，求 parent 的 LL，再 `removeStandardDim`，最后删掉 register 的零基：

```cpp
SmallVector<int64_t> parentShape(shape);
parentShape.insert(parentShape.begin() + getDim(), 1);
LinearLayout parentLL = toLinearLayout(parentShape, getParent());
auto sliceLL = removeStandardDim(parentLL, getDim());
return sliceLL.removeZeroBasesAlongDim("register");
```

`源码依据`：`LinearLayoutConversions.cpp:1037-1049`。

#### TMEM（Blackwell）

`tensorMemoryToLinearLayout` 处理 `TensorMemoryEncodingAttr`：`blockM` 只能是 64/128，`colStride` 控制列零基，`fp4Padded` 多一个 padding bit，`isM64TwoCTA` 在行方向加 `blockN/2` 基。`源码依据`：`LinearLayoutConversions.cpp:1051-1106`。**guided**：T4 没有 TMEM。

### 5. dispatcher 和缓存

`TritonGPUDialect::toLinearLayout` 是统一入口：distributed 走 trait，否则断言 shape 是 2 的幂，再按 shared/TMEM/scales 分派；结果以 `(shape, layout)` 为 key 缓存在 `llCache`。`源码依据`：`LinearLayoutConversions.cpp:1193-1227`。缓存很重要，因为 lowering 里会反复调用 `toLinearLayout`。

### 6. 等价、组合、求逆

**等价**：两个布局相等就是基向量相等；`operator==` 声明在 `LinearLayout.h:800`、定义在 `lib/Tools/LinearLayout.cpp:1250-1260`，它调用 `equalIgnoringOutDimSizes`（`LinearLayout.h:804`，实现 `LinearLayout.cpp:1262`）逐维比较映射。更实用的是忽略 broadcast：`src.removeZeroBasesAlongDim("register") == dst.removeZeroBasesAlongDim("register")`（`Dialect.cpp:3678-3715`）。

**组合**：`compose` 是矩阵乘法；`operator*` 会自动拼接输入维、扩展输出维（`LinearLayout.h:653-670`、`735-745`）。

**求逆**：`invertAndCompose(outer)` 算 `outer^-1 * this`；outer 不可逆但 surjective 时用伪逆，选最小 shared offset（`LinearLayout.h:745-790`）。给定寄存器布局 `R` 和 shared 布局 `S`，`R.invertAndCompose(S)` 就是“每个线程写到哪个 shared offset”。

### 7. `minimalCvtLayout`：一次转换到底贵不贵

`minimalCvtLayout(src, dst)` 先算 `dst.invertAndCompose(src)`，再对公共慢速维做 quotient（`LinearLayoutConversions.cpp:1855-1881`）。注释写得很直白：block 恒等就不需要 distributed shared；warp 也恒等就能用 warp shuffle；lane 也恒等就只需重排寄存器（`1855-1858`）。

`cvtReordersRegisters` / `cvtNeedsWarpShuffle` / `cvtNeedsSharedMemory` 在 `lib/Analysis/Utility.cpp:1118-1157` 把它变成三个布尔判断；`ConvertLayoutOpToLLVM.cpp:46-66` 据此选择直接替换、`transferWithinThread`、`transferWithinWarp` 或 `transferSwizzlingLocalMem`。这就是“有些 convert 免费，有些很贵”的原因。

### 8. `removeZeroBasesAlongDim` 在 `MakeRange` 里做什么

`MakeRangeOpConversion` 生成索引前先 `toLinearLayout(ty).removeZeroBasesAlongDim("register")`（`MakeRangeOpToLLVM.cpp:31-33`）。`emitIndices` 会为每个 register 生成一个索引；零基会产生重复索引，而 `MakeRange` 只想要每个线程唯一持有的元素。源码注释提到 slice 布局可能有“比预期更多的元素”（`36-38`）。

**实测**：`tensor<2xi32>` 配 `sizePerThread=[4]` 的 blocked，LL 是 `register=[[1]], lane=[[0]×5], warp=[[0],[0]]`；`[2]` 超出 shape 被置零并删掉，所以每线程只生成 2 个索引。注意只删 register 零基；lane/warp 的零基是 broadcast，要保留。

## 进一步用法/重要变体

### 1. `#ttg.linear` 的序列化

真实例子（来自 `test/TritonGPU/canonicalize.mlir:243`）：

```mlir
#linear = #ttg.linear<{
  register = [[0, 1], [8, 0], [16, 0]],
  lane     = [[0, 2], [0, 4], [1, 0], [2, 0], [4, 0]],
  warp     = [[0, 8], [0, 16]],
  block    = []
}>
```

读法：

- `register` 的 3 个 bit：列 +1、行 +8、行 +16；
- `lane` 的 5 个 bit：列 +2、列 +4、行 +1、行 +2、行 +4；
- `warp` 的 2 个 bit：列 +8、列 +16；
- `block=[]`：没有 CGA 切分。

`源码依据`：`LinearEncodingAttr::print/parse` 在 `Dialect.cpp:1207-1232`；输入维固定为 `register, lane, warp, block`。

### 2. `LinearEncoding` vs `GenericLinearEncoding`

- `LinearEncodingAttr`：每个基最多一个非零输出分量（非 swizzled）；去掉 broadcast 基后必须是置换矩阵。
- `GenericLinearEncodingAttr`：warp 基可以是 swizzled（多个非零分量）；去掉 broadcast 基后只要求 surjective，不要求双射。

`源码依据`：`TritonGPUAttrDefs.td:679-747`；verifier 在 `Dialect.cpp:1182-1194` 和 `1458-1485`。

所以手写 `#ttg.linear` 时，如果 warp 基同时动了两个维度，`LinearEncoding` 会拒绝；要用 `#ttg.generic_linear`。

### 3. 用 `triton-tensor-layout` 打印

```bash
triton-tensor-layout \
  -l "#ttg.blocked<{sizePerThread = [1, 4], threadsPerWarp = [4, 8], warpsPerCTA = [4, 1], order = [1, 0]}>" \
  -t "tensor<16x16xf16>"
```

真实期望输出在 `test/Tools/tensor_layout_print.mlir:17-25`。**源码依据**：第一行是

```text
T0:0|  T4:0,   T0:1|  T4:1,   T0:2|  T4:2,   T0:3|  T4:3, ...
```

这个布局的线性形式（**实测**）是：

```text
#ttg.linear<{
  register = [[0, 1], [0, 2]],
  lane     = [[0, 4], [0, 8], [0, 0], [1, 0], [2, 0]],
  warp     = [[4, 0], [8, 0]],
  block    = []
}>
```

`lane` 的第 3 个基是 `[0,0]`：lane bit2 不影响输出。所以 lane 0 和 lane 4 持有相同元素，打印成 `T0:0|T4:0`。这正好演示了“零基 = broadcast”。

### 4. `SharedLinearEncoding`

`#ttg.shared_linear<{offset = [[0,1],[0,2],[1,0],[2,2]]}, alignment = 16>` 把 shared offset 的 4 个 bit 映射到 4×4 的 `(dim0,dim1)`。最后一项 `(2,2)` 是 swizzle。

`源码依据`：`TritonGPUAttrDefs.td:421-453`；`toLinearLayout` 在 `Dialect.cpp:2140-2150`；例子在 `test/TritonGPU/ops.mlir:515-516`。

### 5. `combineCtaCgaWithShape` 的顺序

这个函数做了三件事：

1. 用 `ensureLayoutNotSmallerThan` 把 CTA layout 扩到 `shapePerCTA`（补 register 基）；
2. 用 `ensureLayoutNotLargerThan` 把超出 `shapePerCTA` 的基置零（broadcast）；
3. 乘上 CGA layout，再 `transposeOuts` 到标准维序。

`源码依据`：`LinearLayoutConversions.cpp:1335-1363`。

顺序很重要：先扩/截 CTA，再乘 CGA。如果反过来，CGA 的基会被 CTA 的 shape 截断，结果不对。

## 对比与取舍

| 方式 | 存储 | 比较 | 组合 | 求逆 | 适用 |
|---|---|---|---|---|---|
| thread→element 表 | O(threads×regs) | O(n) | 难 | 难 | 教学、调试 |
| `#ttg.blocked` | 4 个数组 | 字段比较 | 有限 | 不直接 | 常见 load/store |
| `#ttg.linear` | 基向量 | 矩阵比较 | 矩阵乘 | 有 | 统一表示、转换 |
| `#ttg.generic_linear` | 基向量 | 同上 | 同上 | 有 | swizzled warp |

取舍：

- **blocked 更紧凑、更贴近硬件**，但只能表达“规则 tile”；
- **linear 更通用、可计算**，但 verifier 更严格，手写更容易错；
- **generic_linear 更宽松**，但 lowering 路径更少，性能不一定好；
- **shared_linear 用于 shared**，输入维是 `offset`，不是 `register/lane/warp`。

## 踩雷集锦

1. **把基向量当成行。**
   `bases[inDim][i]` 是矩阵的列，不是行。输入位选择列，输出是列的 XOR。写反了矩阵转置，坐标全错。

2. **把坐标值当成 bit 下标。**
   `2` 表示“bit1 置 1”，不是“第 2 个 bit”。`(2,0)` 是 dim0 的 bit1，不是 dim0=2 的“第二个元素”。

3. **以为零基是垃圾。**
   零基表示 broadcast，是合法且有意义的。`removeZeroBasesAlongDim` 只在需要“唯一元素”时删；不要在所有地方无脑删。

4. **只删 register 零基，却以为 lane/warp 零基也删了。**
   `removeZeroBasesAlongDim("register")` 只处理 register。lane/warp 的零基仍然表示“多个线程持有同一元素”，这是 broadcast，不是同一线程的重复。

5. **手写 `#ttg.linear` 时违反 verifier。**
   `LinearEncoding` 要求去掉 broadcast 基后是置换矩阵（`Dialect.cpp:1182-1194`）；warp 基不能 swizzled。要 swizzled warp 就用 `#ttg.generic_linear`。

6. **shared 布局用错 shape。**
   `toLinearLayout(shape, layout)` 对 shared 布局要求 `allocShape`，不是逻辑 shape。源码在 `LinearLayoutConversions.cpp:1270-1272` 明确写了 `UNSAFE OVERLOAD`。用错会得到错误的 offset。

7. **以为 `toLinearLayout` 对任意 shape 都行。**
   非 distributed 布局要求 shape 是 2 的幂（`LinearLayoutConversions.cpp:1206-1210`）。TMEM/shared 的 shape 也必须是 2 的幂。

8. **以为 `invertAndCompose` 要求 outer 可逆。**
   它只要求 outer surjective；不可逆时用伪逆，选最小 offset（`LinearLayout.h:745-790`）。但如果你需要唯一映射，必须自己保证 injective。

9. **以为 `minimalCvtLayout` 总是能 quotient 掉慢速维。**
   只有公共维（从输入维尾部开始匹配）才能 quotient；`block`/`warp` 不匹配时，转换就只能走 shared memory。

10. **在 T4 上调试 TMEM。**
    TMEM 是 SM100+ 的；T4 上相关布局和 lowering 不会走。不要把 Blackwell 的 LL 结论套到 T4。

## 进阶

- 读 `lib/Tools/LinearLayout.cpp` 的 `operator*`（`704-765`）、`compose`、`invertAndCompose`，自己实现一个 20 行的 Python LL 类，验证本章的矩阵。
- 用 `triton-tensor-layout` 打印同一个 blocked 布局在 8×8、16×16、32×32 下的 LL，观察 register/lane 基怎么随 shape 变化。
- 读 `MakeRangeOpToLLVM.cpp:31-45`，理解 `emitIndices` 和 `packUniqueTensorElements` 如何配合零基。
- 读 `minimalCvtLayout` 的 quotient 逻辑（`1855-1881`），构造一个“只需要寄存器重排”的转换和一个“必须走 shared”的转换，比较它们的 `conversion`。
- 读 `TensorMemoryEncodingAttr` 的 LL（`1051-1106`），理解 Blackwell 的 64/128 行、2CTA、fp4 padding。

## 动手练习

1. **手算矩阵**：对 4×4 blocked（`sizePerThread=[1,1]`, `threadsPerWarp=[2,2]`, `order=[1,0]`），写出 `B`，验证 `B^3=I`，并求 `B^-1`。**实测**：用 Python 做 GF(2) 矩阵乘法。

2. **apply**：用上面的 `B`，算 `(reg=1, lane=3, warp=0, block=0)` 的坐标。**实测**：对照 Ch 12 的表。

3. **打印 16×16**：用 `triton-tensor-layout` 打印 `test/Tools/tensor_layout_print.mlir:9` 的布局，解释 `T0:0|T4:0`。**guided**：需要构建工具。

4. **MakeRange**：构造 `tensor<2xi32>` + `sizePerThread=[4]` 的布局，打印 LL，指出哪个 register 基被删。**实测**：用本章的 Python 脚本。

5. **转换分类**：给定 `#blocked` 和 `#ttg.linear` 两个 16×16 布局，用 `minimalCvtLayout` 的思路判断它是寄存器重排、warp shuffle 还是 shared。**guided**：需要 `triton-opt` 或自己实现 LL。

6. **GenericLinear**：把 4×4 blocked 的 lane 基改成 `[[0,1],[1,1]]`，判断 `#ttg.linear` 是否接受；如果不接受，换成 `#ttg.generic_linear` 再判断。**源码依据**：verifier 在 `Dialect.cpp:1182-1194`、`1458-1485`。

## 本章重点

- 线性布局把 `(register, lane, warp, block)` 的位向量线性映射到 tensor 坐标；基向量是矩阵的列。
- 4×4 blocked 的矩阵是置换矩阵；`B^3=I`，所以 `B^-1=B^2`。
- `toLinearLayout` 对 blocked/MMA/dot_op/slice/TMEM 各有实现，最后由 dispatcher 缓存。
- `combineCtaCgaWithShape` 先扩/截 CTA，再乘 CGA。
- 等价 = 基向量相等；组合 = 矩阵乘；求逆 = `invertAndCompose`。
- `minimalCvtLayout` 把转换分类成寄存器重排、warp shuffle、shared memory。
- `removeZeroBasesAlongDim("register")` 删掉同一线程内的重复寄存器基，`MakeRange` 靠它生成唯一索引。
- `#ttg.linear` 要求置换矩阵；`#ttg.generic_linear` 允许 swizzled warp。

## 自我检核

1. 4×4 blocked 的 `B` 是什么？为什么 `B^3=I`？
2. `bases[inDim][i]` 是矩阵的行还是列？输入位怎么选它？
3. 零基表示什么？为什么不能在所有地方删掉？
4. `removeZeroBasesAlongDim("register")` 和 `removeZeroBasesAlongDim("lane")` 的语义有什么区别？
5. `combineCtaCgaWithShape` 为什么要先扩/截 CTA，再乘 CGA？
6. `minimalCvtLayout` 如何判断一次转换能不能只用寄存器重排？
7. `LinearEncoding` 和 `GenericLinearEncoding` 的 verifier 差别是什么？
8. 为什么 shared 布局的 `toLinearLayout` 要用 `allocShape`？
9. `MakeRange` 为什么需要删 register 零基？不删会怎样？
10. T4 上哪些 LL 分支不会走？为什么？

## 延伸阅读

- **`include/triton/Tools/LinearLayout.h`**
  - **读哪里**：`20-110`（概述和例子）、`120-180`（维度顺序、surjective/injective）、`190-290`（为什么 hardware→tensor、GF(2)）、`315-330`（bases）、`437`（isInvertible）、`745-800`（invertAndCompose、free variables、removeZeroBases）。
  - **学什么**：线性布局的数学定义和 API。
  - **与本章关系**：本章的矩阵和求逆都来自这里。
- **`lib/Dialect/TritonGPU/IR/LinearLayoutConversions.cpp`**
  - **读哪里**：`869-880`（Blocked）、`922-988`（MMA）、`1026-1049`（DotOperand/Slice）、`1051-1106`（TMEM）、`1193-1227`（dispatcher）、`1335-1363`（combine）、`1855-1881`（minimalCvtLayout）。
  - **学什么**：每种布局如何变成 LL，转换如何分类。
  - **与本章关系**：本章的“底层机制”就是这些函数的逐段解释。
- **`lib/Tools/LinearLayout.cpp`**
  - **读哪里**：`704-765`（operator*）、`compose`、`invertAndCompose`。
  - **学什么**：矩阵乘、拼接、求逆的实现细节。
  - **与本章关系**：手算矩阵后，用代码验证。
- **`lib/Conversion/TritonGPUToLLVM/MakeRangeOpToLLVM.cpp`**
  - **读哪里**：`21-48`。
  - **学什么**：`removeZeroBasesAlongDim("register")` 的实际用途。
  - **与本章关系**：把“零基”从概念变成 lowering 行为。
- **`test/Tools/tensor_layout_print.mlir`**
  - **读哪里**：`1-25`。
  - **学什么**：真实工具输出，`T0:0|T4:0` 的 broadcast 读法。
  - **与本章关系**：LL 的“表格视图”。
- **CUTLASS CuTe 文档**
  - **读哪里**：`00_quickstart.md` 的 layout/composition。
  - **学什么**：另一种可组合布局系统。
  - **与本章关系**：`LinearLayout.h:300-320` 有对比。

## 下一章

→ [Ch 14 — 布局传播与转换消除](./14-layout-propagation-and-conversion-elimination.md)
