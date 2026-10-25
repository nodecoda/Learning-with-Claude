# Ch 12 TVM TIR 与 schedule：0.27 的新 API

> **目标**：看懂 TVM 0.27 的 TIR 长什么样，知道 `PrimFunc`、`sblock`、`axis`、`buffer` 各自负责什么；能用 `s_tir.Schedule` 的 `split/reorder/fuse/parallel/vectorize/unroll` 真正改写 IR，并把旧教程的 API 一一迁移过来。
## 为什么需要这个？

Ch 11 把 schedule 当成「调循环的工具」；这一章要往下看一层：**schedule 到底改了 IR 的什么**。

如果你只会背 `split`、`reorder`，遇到下面这些问题就会卡住：

- 为什么 `split` 之后 `k` 变成了 `k_0 * 4 + k_1`？
- 为什么 `fuse` 之后多了 `//` 和 `%`？
- 为什么 `vectorize` 有时生成 `<16 x float>`，有时退化成标量？
- 为什么旧教程的 `sch.get_block`、`sch.tile`、`tvm.nd.array` 在 0.27 里全部报错？

答案都在 TIR 里。TIR 不是一个黑盒；它是「带循环、带块、带 buffer 访问」的结构化 IR。schedule 原语本质上是在这个结构上做**合法的重写**。
## 先建立直觉：TIR 是「带循环的块图」

把一段 TIR 想成三层：

```text
PrimFunc
  └── 参数 + buffer_map
        └── 循环（for）
              └── sblock（一次计算）
                    ├── axis（循环变量到块迭代变量的映射）
                    ├── reads / writes（buffer 访问声明）
                    └── init + update（累加类计算的初始化与更新）
```

以 matmul 为例：

```python
@T.prim_func(s_tir=True)
def main(A: T.Buffer((16, 16), "float32"), B: T.Buffer((16, 16), "float32"), C: T.Buffer((16, 16), "float32")):
    T.func_attr({"tirx.noalias": True})
    for i, j, k in T.grid(16, 16, 16):
        with T.sblock("C"):
            v_i, v_j, v_k = T.axis.remap("SSR", [i, j, k])
            T.reads(A[v_i, v_k], B[v_k, v_j])
            T.writes(C[v_i, v_j])
            with T.init():
                C[v_i, v_j] = T.float32(0.0)
            C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
```

这段文本不是「示意」，它是 `te.create_prim_func` 真实生成、`script()` 真实打印的格式。下面逐层拆。
## 核心概念一：PrimFunc 与 buffer_map

`PrimFunc` 是 TIR 的函数单位。它的参数在文本里是 `A: T.Buffer((16, 16), "float32")`，在内部是「一个 handle 参数 + buffer_map 绑定」。

几个关键点：

- **shape 和 dtype 是静态信息**：`(16, 16)`、`float32` 写进 buffer 类型；codegen 靠它算地址和向量宽度。
- **`T.func_attr({"tirx.noalias": True})`**：告诉编译器这些 buffer 不重叠。如果你把同一个 `numpy` 数组同时传给 `A` 和 `C`，行为未定义——这不是 TVM 的 bug，是你违反了契约。
`te.create_prim_func([A, B, C])` 做的是「把 TE 的 compute 表达式降成 PrimFunc」。它的源码入口在 `tvm/te/operation.py:538`（`源码依据`）。旧教程的 `te.create_schedule(C.op)` 在 0.27 已经不存在。
## 核心概念二：sblock 与 axis

`sblock` 是 schedule 的最小单位。你可以把它理解成「一次带名字的计算」：

```python
with T.sblock("C"):
    v_i, v_j, v_k = T.axis.remap("SSR", [i, j, k])
```

`T.axis.remap("SSR", [i, j, k])` 的意思是：

- `i -> v_i`，`S` = spatial（输出空间）
- `j -> v_j`，`S` = spatial
- `k -> v_k`，`R` = reduction（归约）

axis 是 schedule 合法性的核心。`split` 之所以能把 `k` 拆成 `k_0, k_1`，是因为它同时把 `v_k` 的绑定改写成 `k_0 * 4 + k_1`；`reorder` 之所以能换循环顺序，是因为 axis 仍然指向同一个逻辑迭代空间。

`T.reads` / `T.writes` 不是装饰：它们声明 block 读了哪些 buffer、写了哪些 buffer，依赖分析靠它。`T.init()` 里的赋值是归约的初始值；下面的 `C[...] = C[...] + ...` 是 update。把 init 和 update 分开，TVM 才能正确地做 `rfactor`、`decompose_reduction` 等变换。
## 核心概念三：Schedule 是 IR 重写器

`s_tir.Schedule` 不是「一个循环配置对象」，而是「一个持有 IRModule 的重写器」：

```python
sch = s_tir.Schedule(func)
print(type(sch.mod))          # tvm.ir.module.IRModule
blk = sch.get_sblock("C")     # SBlockRV：block 的句柄
loops = sch.get_loops(blk)    # list[LoopRV]：循环的句柄
```

`SBlockRV` 和 `LoopRV` 是**句柄**，不是 IR 节点本身。你拿着句柄调用原语，原语去改 `sch.mod` 里的真实 IR：

```python
ko, ki = sch.split(loops[2], factors=[4, None])
sch.reorder(loops[0], loops[1], ko, ki)
print(sch.mod.script())       # 看真实 IR
```

这也是为什么「打印 script」是最有效的调试手段：你不需要猜 `split` 做了什么，IR 会告诉你。
## 底层机制：从 TE 到 TIR 再到 schedule

完整链路是：

```text
te.placeholder / te.compute
        │  te.create_prim_func
        ▼
PrimFunc（TIR，带 sblock/axis/buffer）
        │  s_tir.Schedule(func)
        ▼
IRModule + ScheduleState
        │  split / reorder / fuse / parallel / vectorize / unroll
        ▼
改写后的 IRModule（sch.mod）
        │  tvm.build(sch.mod, target=...)
        ▼
LLVM IR / C / CUDA
```

`Schedule` 在构造时会分析 block 的依赖关系，建立 `ScheduleState`。每个原语都做三件事：

1. 检查合法性（循环是否存在、依赖是否允许、block 是否在同一作用域）。
2. 改写 IR（循环、axis 绑定、annotation）。
3. 更新 `sch.mod`。

如果原语非法，它会抛 `ScheduleError` 或 `TVMError`，而不是悄悄生成错误代码。这是好事：错误发生在编译期，不是运行期。
## 示例一：最小 matmul 的 TIR

下面这段是 `te.create_prim_func([A, B, C])` 对 N=16 matmul 的真实输出（`实测`）：

```python
import tvm
from tvm import te, s_tir

N = 16
A = te.placeholder((N, N), name="A", dtype="float32")
B = te.placeholder((N, N), name="B", dtype="float32")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

func = te.create_prim_func([A, B, C])
print(func.script())
```

真实输出：

```text
# from tvm.script import tirx as T
# from tvm.tirx.layout import Axis

@T.prim_func(s_tir=True)
def main(A: T.Buffer((16, 16), "float32"), B: T.Buffer((16, 16), "float32"), C: T.Buffer((16, 16), "float32")):
    T.func_attr({"tirx.noalias": True})
    # with T.sblock("root"):
    for i, j, k in T.grid(16, 16, 16):
        with T.sblock("C"):
            v_i, v_j, v_k = T.axis.remap("SSR", [i, j, k])
            T.reads(A[v_i, v_k], B[v_k, v_j])
            T.writes(C[v_i, v_j])
            with T.init():
                C[v_i, v_j] = T.float32(0.0)
            C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
```

几个容易忽略的细节：

- 头部是 `# from tvm.script import tirx as T`，不是 `tvm.tir`。0.27 把新 TIR 放在 `tvm.tirx`，把调度 API 放在 `tvm.s_tir`。
- `@T.prim_func(s_tir=True)` 的 `s_tir=True` 表示这是「可调度的 TIR」；`te.create_prim_func` 生成的就是它。没有这个标记，`s_tir.Schedule` 不会把它当作可调度的 PrimFunc。
- `# with T.sblock("root"):` 是被注释掉的 root block；真正的计算 block 叫 `"C"`。
- `T.grid(16, 16, 16)` 是三重循环的简写，等价于三层嵌套 `for`。
## 示例二：split / reorder / fuse 前后对照

### split：把 k 拆成 ko × ki

```python
sch = s_tir.Schedule(func)
i, j, kk = sch.get_loops(sch.get_sblock("C"))
ko, ki = sch.split(kk, factors=[4, None])
print(sch.mod.script())
```

真实输出（只列循环和 block 部分）：

```text
for i, j, k_0, k_1 in T.grid(16, 16, 4, 4):
    with T.sblock("C"):
        v_i, v_j = T.axis.remap("SS", [i, j])
        v_k = T.axis.reduce(16, k_0 * 4 + k_1)
        T.reads(A[v_i, v_k], B[v_k, v_j])
        T.writes(C[v_i, v_j])
        with T.init():
            C[v_i, v_j] = T.float32(0.0)
        C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
```

`split` 改了两件事：

1. `T.grid(16, 16, 16)` 变成 `T.grid(16, 16, 4, 4)`：一个 `k` 循环变成两个。
2. `v_k` 的绑定从 `v_k = T.axis.reduce(16, k)` 变成 `v_k = T.axis.reduce(16, k_0 * 4 + k_1)`：逻辑索引不变，物理循环变了。

`factors=[4, None]` 的意思是「外层 4，内层自动」。如果写成 `[None, 4]`，就是「外层自动，内层 4」——tiling 通常要后者，因为内层是 tile。

### 不可整除的 split：predicate 从哪来

把 16 按 5 拆：

```python
ko, ki = sch.split(kk, factors=[5, None])
print(sch.mod.script())
```

真实输出：

```text
for i, j, k_0, k_1 in T.grid(16, 16, 5, 4):
    with T.sblock("C"):
        v_i, v_j = T.axis.remap("SS", [i, j])
        v_k = T.axis.reduce(16, k_0 * 4 + k_1)
        T.where(k_0 * 4 + k_1 < 16)
        T.reads(A[v_i, v_k], B[v_k, v_j])
        T.writes(C[v_i, v_j])
        ...
```

`5 × 4 = 20 > 16`，所以 TVM 加了 `T.where(k_0 * 4 + k_1 < 16)`。这不是「优化」，是正确性补丁：多出来的 4 个迭代必须被屏蔽。predicate 会让 codegen 生成条件分支，通常比整除版本慢；这就是为什么调优时尽量让 factor 整除 shape。

### reorder：换循环顺序

```python
sch.reorder(j, i, ko, ki)
print(sch.mod.script())
```

真实输出（循环头）：

```text
for j, i, k_0, k_1 in T.grid(16, 16, 4, 4):
```

`reorder` 只改 `for` 的顺序，不改 axis 绑定。合法性由依赖分析保证：如果交换两个循环会改变 reduction 的语义，TVM 会拒绝。注意：**合法不等于快**；`j` 外层、`i` 内层对 `C` 的写回是跳行的，可能更慢。性能要在 Ch 11 的计时框架里测。

### fuse：把两个循环合并成一个

用一个 8×8 的 elementwise 例子更清楚：

```python
M = 8
X = te.placeholder((M, M), name="X", dtype="float32")
Y = te.compute((M, M), lambda a, b: X[a, b] + 1.0, name="Y")
sch = s_tir.Schedule(te.create_prim_func([X, Y]))
a, b = sch.get_loops(sch.get_sblock("Y"))
a_b = sch.fuse(a, b)
print(sch.mod.script())
```

真实输出：

```text
for a_b_fused in range(64):
    with T.sblock("Y"):
        v_a = T.axis.spatial(8, a_b_fused // 8)
        v_b = T.axis.spatial(8, a_b_fused % 8)
        T.reads(X[v_a, v_b])
        T.writes(Y[v_a, v_b])
        Y[v_a, v_b] = X[v_a, v_b] + T.float32(1.0)
```

`fuse` 把 `8 × 8` 变成 `64`，并用 `// 8`、`% 8` 还原两个逻辑索引。它适合「把多维输出摊平成一维」的场景（比如后面接一个一维向量化或一维线程绑定）。`fuse` 要求循环是完美嵌套、同一个父作用域；跨 block 或跨非完美嵌套会失败。
## 示例三：tile 在 0.27 不存在

旧教程经常写：

```python
sch.tile(i, j, [4, 4])
```

0.27 直接报错（`实测`）：

```text
AttributeError: 'Schedule' object has no attribute 'tile'
```

`tile` 不在 `Schedule` 的方法表里。等价的写法是 `split` + `reorder`：

```python
io, ii = sch.split(i, factors=[4, None])
jo, ji = sch.split(j, factors=[4, None])
sch.reorder(io, jo, ii, ji)
```

真实循环头：

```text
for a_0, b_0, a_1, b_1 in T.grid(4, 4, 2, 2):
```

对 8×8 来说，`4×4` 的外层 tile、`2×2` 的内层 tile，正好覆盖 64 个点。tile 不是原语，而是「两个 split + 一个 reorder」的组合；知道这一点，你就不会被旧 API 卡住。
## 示例四：完整 schedule 与它的 TIR

把 Ch 11 的套路组合起来：`parallel` + `unroll` + `vectorize`，N=64：

```python
N = 64
A = te.placeholder((N, N), name="A", dtype="float32")
B = te.placeholder((N, N), name="B", dtype="float32")
k = te.reduce_axis((0, N), name="k")
C = te.compute((N, N), lambda i, j: te.sum(A[i, k] * B[k, j], axis=k), name="C")

sch = s_tir.Schedule(te.create_prim_func([A, B, C]))
i, j, kk = sch.get_loops(sch.get_sblock("C"))
io, ii = sch.split(i, factors=[2, None])
jo, ji = sch.split(j, factors=[None, 8])
ko, ki = sch.split(kk, factors=[None, 8])
sch.reorder(io, jo, ko, ii, ki, ji)
sch.vectorize(ji)
sch.unroll(ki)
sch.parallel(io)
print(sch.mod.script())
```

真实输出：

```text
@I.ir_module
class Module:
    @T.prim_func(s_tir=True)
    def main(A: T.Buffer((64, 64), "float32"), B: T.Buffer((64, 64), "float32"), C: T.Buffer((64, 64), "float32")):
        T.func_attr({"tirx.noalias": True})
        # with T.sblock("root"):
        for i_0 in T.parallel(2):
            for j_0, k_0, i_1 in T.grid(8, 8, 32):
                for k_1 in T.unroll(8):
                    for j_1 in T.vectorized(8):
                        with T.sblock("C"):
                            v_i = T.axis.spatial(64, i_0 * 32 + i_1)
                            v_j = T.axis.spatial(64, j_0 * 8 + j_1)
                            v_k = T.axis.reduce(64, k_0 * 8 + k_1)
                            T.reads(A[v_i, v_k], B[v_k, v_j])
                            T.writes(C[v_i, v_j])
                            with T.init():
                                C[v_i, v_j] = T.float32(0.0)
                            C[v_i, v_j] = C[v_i, v_j] + A[v_i, v_k] * B[v_k, v_j]
```

逐行对应：

| TIR 里的东西 | 由哪个原语产生 |
|---|---|
| `for i_0 in T.parallel(2)` | `sch.parallel(io)` |
| `for j_0, k_0, i_1 in T.grid(8, 8, 32)` | `split` + `reorder` |
| `for k_1 in T.unroll(8)` | `sch.unroll(ki)` |
| `for j_1 in T.vectorized(8)` | `sch.vectorize(ji)` |
| `v_i = ... i_0 * 32 + i_1` | `split(i, [2, None])` |
| `v_j = ... j_0 * 8 + j_1` | `split(j, [None, 8])` |
| `v_k = ... k_0 * 8 + k_1` | `split(k, [None, 8])` |

这就是「schedule 原语改了 IR 的什么」的完整答案：**它们改循环、改 axis 绑定、加 annotation；不碰 block 的数学表达式**。
## 旧 API → 0.27 迁移表

下面每一行的旧写法都在 0.27 里真实报错（`实测`），新写法都真实可跑。源码行号来自本机安装的 TVM 包（`源码依据`）。

| 旧教程写法 | 0.27 正确写法 | 证据 |
|---|---|---|
| `te.create_schedule(C.op)` | `te.create_prim_func([A,B,C])` + `s_tir.Schedule(func)` | `te.create_schedule` 不存在；`te/operation.py:538` |
| `sch.get_block("C")` | `sch.get_sblock("C")` | `s_tir/schedule/schedule.py:483` |
| `sch.get_loops(func)` | `sch.get_loops(sch.get_sblock("C"))` | `schedule.py:508` |
| `sch.tile(i, j, [4, 4])` | `split` + `reorder` | `Schedule` 没有 `tile`；`schedule.py` 方法表 |
| `tvm.nd.array(x)` | `tvm.runtime.tensor(x, tvm.cpu())` | `tvm.nd` 不存在；`runtime/_tensor.py:336` |
| `tvm.nd.empty(shape, dtype)` | `tvm.runtime.empty(shape, dtype, tvm.cpu())` | `runtime/_tensor.py:306` |
| `tvm.tir` | `tvm.s_tir`（调度）+ `tvm.tirx`（新 TIR） | `tvm/__init__.py:83`；`s_tir/__init__.py:33` |
| `mod.get_source()` | `mod.inspect_source()` | `tvm_ffi/module.py:301` |
| `tvm.autotvm` | `tvm.s_tir.meta_schedule` | `tvm.autotvm` 不存在；Ch 13 |

真实报错样例：

```text
AttributeError: 'Schedule' object has no attribute 'get_block'
AttributeError: 'Schedule' object has no attribute 'tile'
AttributeError: 'IRModule' object has no attribute 'get_source'
ModuleNotFoundError: No module named 'tvm.tir'
```
## 对比与取舍

| 原语 | 改了什么 | 典型用途 | 主要限制 |
|---|---|---|---|
| `split` | 一个循环变两个，改写 axis 绑定 | tiling、向量化前的内层拆分 | 不整除会产生 predicate |
| `reorder` | 循环顺序 | 改善局部性、把 reduction 放内层 | 合法不等于快 |
| `fuse` | 多个循环变一个，引入 `//`、`%` | 摊平输出、接一维并行/向量化 | 要求完美嵌套 |
| `parallel` | 加线程绑定 | 多核 | 只能并行输出空间；线程数受物理核限制 |
| `vectorize` | 加向量标记 | SIMD | 宽度受硬件限制；tail 可能退化 |
| `unroll` | 展开循环体 | 减少循环开销、暴露 ILP | 依赖链和代码体积 |
| `tile` | 0.27 不存在 | — | 用 `split` + `reorder` 代替 |
## 踩雷集锦

1. **把 `get_loops` 传给 PrimFunc**：它要的是 block 或 block 名。先 `get_sblock("C")`，再 `get_loops(blk)`。
2. **`split` 的 factors 顺序写反**：`[4, None]` 是外层 4；`[None, 4]` 是内层 4。tiling 的内层通常是你想要的 tile 大小。
3. **以为 `reorder` 会检查性能**：它只检查合法性。`reorder(j, i, k)` 合法但可能更慢。
4. **`fuse` 跨 block**：`fuse` 要求循环在同一个完美嵌套里；跨 block 会失败。
5. **`parallel` 并行 reduction**：多个线程写同一个 `C[i,j]`，结果错误；并行输出 `i` 或 `i,j`。
6. **`vectorize` 不整除的循环**：会产生 predicate 或退化，性能可能下降；先检查 `N % vj == 0`。
7. **忽略 `tirx.noalias`**：把重叠的数组传给 A/B/C，结果未定义；TVM 不会替你检查别名。
8. **改完不打印 `sch.mod.script()`**：TIR 是你唯一的真相来源；不要靠猜。
## 进阶：再往深一层

- **读 `Schedule` 源码**：`s_tir/schedule/schedule.py` 里每个原语都有 docstring 和 FFI 调用；`get_sblock` 在 483 行，`split` 在 739 行，`fuse` 在 676 行。看懂参数校验，就知道什么情况下会抛 `ScheduleError`。
- **看 `T.where` 的代价**：把不可整除的 split 生成 C 代码，观察 predicate 是否变成 `if`；这是 Ch 15 tail handling 的伏笔。
- **试 `rfactor`**：把 reduction 拆成「部分和 + 最终归约」，需要 `T.init`/update 的分离；这是理解 block 语义的好练习。
## 动手练习

### 练习 1：不可整除的 split

**任务**：把 N=16 matmul 的 `k` 按 5 拆，打印 TIR，找到 `T.where`，并解释 `5 × 4` 为什么多出 4 个迭代。

**测试用例**：编译并运行，`maxerr < 1e-4`；TIR 中必须出现 `T.where(k_0 * 4 + k_1 < 16)`。

**步骤**：

1. 复制示例一的代码。
2. `ko, ki = sch.split(kk, factors=[5, None])`。
3. 打印 `sch.mod.script()`。
4. 把 factor 改成 4，再打印一次，比较有无 predicate。

<details>
<summary>参考解答</summary>

factor=5 时，外层 extent 是 5，内层是 `ceil(16/5)=4`，总迭代 20；多出的 4 个必须用 `k_0*4+k_1 < 16` 屏蔽。factor=4 时，外层 4、内层 4，正好 16，没有 predicate。

```python
ko, ki = sch.split(kk, factors=[5, None])
print(sch.mod.script())   # 有 T.where
```

</details>

**常见卡点**：`T.where` 出现在 block 内部，不是 `for` 上；它作用于整个 block 的 body。

### 练习 2：复现完整 schedule

**任务**：把示例四的 `par=2` 改成 `par=4`，`vj=8` 改成 `vj=16`，重新打印 TIR，标出每个 `T.parallel/T.unroll/T.vectorized` 对应的原语。

**测试用例**：N=64，`maxerr < 1e-4`；TIR 中出现 `T.parallel(4)`、`T.vectorized(16)`。

**步骤**：

1. 复制示例四。
2. 改 `split(i, factors=[4, None])`、`split(j, factors=[None, 16])`。
3. `reorder` 的顺序不变。
4. 打印并逐行标注。

<details>
<summary>参考解答</summary>

```python
io, ii = sch.split(i, factors=[4, None])
jo, ji = sch.split(j, factors=[None, 16])
ko, ki = sch.split(kk, factors=[None, 8])
sch.reorder(io, jo, ko, ii, ki, ji)
sch.vectorize(ji); sch.unroll(ki); sch.parallel(io)
print(sch.mod.script())
```

预期循环头：`for i_0 in T.parallel(4): for j_0, k_0, i_1 in T.grid(4, 8, 16): for k_1 in T.unroll(8): for j_1 in T.vectorized(16):`（extent 以实际 N=64 计算）。

</details>

**常见卡点**：`split(i, [4, None])` 的 `ii` extent 是 `64/4=16`；不要手算错。

### 练习 3：用 block 名直接取 loops

**任务**：不调用 `get_sblock`，直接 `sch.get_loops("C")`，看是否等价；再试 `sch.get_loops(func)`，记录报错。

**测试用例**：`get_loops("C")` 返回 3 个 `LoopRV`；`get_loops(func)` 抛 `TypeError`。

**步骤**：

1. `loops = sch.get_loops("C")`，打印长度。
2. `sch.get_loops(func)`，捕获异常并打印第一行。
3. 解释为什么 API 要这么设计。

<details>
<summary>参考解答</summary>

`get_loops` 的签名是 `get_loops(block: SBlockRV | str)`（`schedule.py:508`），所以传 block 名可以；传 PrimFunc 不行。设计上，循环属于 block 的作用域，必须先定位 block。

```python
print(len(sch.get_loops("C")))   # 3
try:
    sch.get_loops(func)
except Exception as e:
    print(type(e).__name__, str(e).splitlines()[0])
```

</details>

**常见卡点**：`get_loops("root")` 在本机返回空列表；root 是容器，真正参与调度的是 `"C"` 这样的计算 block。
## 本章重点整理

- TIR 是「PrimFunc + buffer_map + 循环 + sblock + axis + reads/writes」的结构化 IR。
- `te.create_prim_func` 把 TE 表达式降成可调度的 PrimFunc；`s_tir.Schedule` 持有并改写 IRModule。
- `split` 改循环数量和 axis 绑定；不整除时插入 `T.where` predicate。
- `reorder` 只改循环顺序；合法不等于快。
- `fuse` 合并循环，用 `//`、`%` 还原索引；要求完美嵌套。
- `parallel`/`vectorize`/`unroll` 是循环 annotation，最终落到 TIR 的 `T.parallel`/`T.vectorized`/`T.unroll`。
- 0.27 没有 `tile`；用 `split` + `reorder` 代替。
- 旧 API（`get_block`、`tvm.nd`、`tvm.tir`、`get_source`）全部变了；迁移表要背下来。
## 自我检核

- [ ] 我能画出 PrimFunc → 循环 → sblock → axis 的层次
- [ ] 我能解释 `T.axis.remap("SSR", [i,j,k])` 里每个字母的含义
- [ ] 我能说出 `split` 对 axis 绑定做了什么
- [ ] 我能解释不可整除 split 的 `T.where` 从哪来
- [ ] 我能写出 `fuse` 后的 `//`、`%` 索引还原
- [ ] 我知道 0.27 没有 `tile`，并能用 `split`+`reorder` 等价实现
- [ ] 我能列出至少 6 条旧 API → 0.27 的迁移
- [ ] 我能用 `sch.mod.script()` 定位每个原语改了什么
## 延伸阅读

- **TVM TIR 文档**（https://tvm.apache.org/docs/reference/api/python/tir.html）：读 `PrimFunc`、`Buffer`、`For`、`Block` 的定义。与本课关联：本章的结构基础。
- **TVMScript 文档**（https://tvm.apache.org/docs/reference/api/python/script.html）：读 `T.grid`、`T.axis`、`T.sblock` 的语法。与本课关联：看懂 `script()` 输出。
- **TVM Schedule Primitives 教程**（https://tvm.apache.org/docs/tutorials/language/schedule_primitives.html）：读 `split`、`reorder`、`fuse`、`tile` 的官方示例，注意 0.27 的 API 差异。与本课关联：原语的行为对照。
- **TVM 源码 `s_tir/schedule/schedule.py`**（https://github.com/apache/tvm/blob/main/python/tvm/s_tir/schedule/schedule.py）：读 `get_sblock`、`split`、`reorder`、`fuse` 的 docstring 和参数校验。与本课关联：迁移表的行号依据。
- **TVM RFC: TIR Script**（https://github.com/apache/tvm/tree/main/rfcs）：读 TIR Script 的设计动机。与本课关联：为什么 TIR 要可打印、可回读。

---

→ [Ch 13 自动调优：meta_schedule、搜索空间与实测](./13-autotuning.md)