# 第 21 章 LLVM IR 基础：类型、SSA、phi 与 getelementptr

> **目标**：能逐行读懂 `mini_ai.codegen_llvm.llvm_ir(graph)` 生成的模块；分清 LLVM IR 里的类型、SSA 值、内存对象和地址计算；能解释 `float`、`i32`、`i1`、`void`、`[N x float]`、`float*` 在真实模块里的作用；能说明本后端的全展开 IR 为什么没有 `phi`，以及改成循环后为什么必须有；能用函数签名和 `getelementptr` 定位 workspace、输入和输出。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，llvmlite 0.50.0，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。先设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本章 IR 全部由本机 `llvm_ir` 实际生成，phi 示例由 llvmlite 现场构造。

## 为什么需要读 LLVM IR

C 后端把计算图变成 `for` 循环，读者能看到循环、索引和 `acc`。LLVM 后端把同一张图变成几百到几千行 IR，循环被完全展开，索引变成常量，临时值变成 `load`、`fadd`、`fmul`、`store`。看起来更啰嗦，但它把三类信息暴露得很清楚：

1. **类型**：每个值是什么类型，指针指向什么，函数收什么、返回什么。
2. **数据流**：每个 SSA 值由哪条指令定义，被谁使用。
3. **内存**：哪些值在寄存器里，哪些值通过指针读写，workspace 的偏移是多少。

调 LLVM 后端时，错误通常不是「C 循环写错了」，而是「GEP 的索引算错了」「workspace 偏移按字节当成了按元素」「函数参数顺序和 ctypes 不一致」「该用 `phi` 的地方没有 `phi`」。这些问题只能靠读 IR 定位。`llvm_ir` 是后端最重要的调试接口，不是展示用的装饰。

本章不追求覆盖 LangRef 的全部语法。我们只读本课后端真正会生成的子集，再补上循环场景必需的 `phi`。读完本章，你应该能拿一段 IR，指出输入指针、输出指针、workspace 指针、常量数组、每个临时值的存储位置，以及最后的结果拷贝。

## 先建立直觉

一张计算图在 LLVM 后端里经历四层变化：

```text
Graph（有 shape 的张量）
  x: tensor<2,3>  b: tensor<3>
  add(x,b) -> relu -> sum(axis=1)
        |
        |  plan_memory：给每个临时值分配 arena 字节偏移
        v
MemoryPlan（偏移表）
  add_0  offset 0    size 64
  relu_1 offset 64   size 64
  sum_2  offset 0    size 64
        |
        |  LLVMCodegen：把每个张量当成一段扁平 float 数组
        v
LLVM 函数
  void mini_forward_llvm(float* in0, float* out0, float* workspace)
        |
        |  逐元素展开：每个下标一条 load / 运算 / store
        v
IR 指令
  %p = getelementptr float, float* %in0, i32 0
  %x = load float, float* %p
  %y = fadd float %x, %b
  store float %y, float* %slot
```

关键转换是「张量变成扁平数组」。LLVM IR 里没有 `tensor<2,3>`。shape 只存在于编译器的 `TensorType` 和生成代码时的索引计算里。运行时看到的是 `float*` 加一段连续内存。`getelementptr`（GEP，地址计算指令）负责把「第几个 float」变成指针，`load` 和 `store` 负责真正读写。

第二层直觉是「SSA 值不等于张量」。LLVM IR 的每个 `%".N"` 只能被赋值一次，这是静态单赋值（SSA，Static Single Assignment）。但张量的每个元素在内存里可以被反复覆盖。后端用 `store` 写 workspace，再用 `load` 读回来，所以 IR 是 SSA 的，内存不是。理解这一点，才不会把 `%".3"` 当成一个「变量」；它是一个指针值，指向的缓冲区内容会变。

## 核心内容

### 1. 模块头：triple 与 datalayout

`llvm_ir` 对一个只有输入、加法和输出的图生成如下开头：

```llvm
; ModuleID = "mini_ai"
target triple = "x86_64-pc-windows-msvc"
target datalayout = ""
```

`target triple` 描述目标平台：x86_64 架构、PC 厂商、Windows 系统、MSVC 环境。`LLVMCodegen.__init__` 里这样设置：

```python
self.module = ir.Module(name="mini_ai")
try:
    self.module.triple = binding.get_default_triple()
except Exception:
    pass
```

`binding.get_default_triple()` 返回当前 llvmlite 绑定的默认三元组。它决定后续 `Target.from_default_triple()` 选择哪个目标机器。`target datalayout = ""` 是空字符串，表示使用目标机器的默认布局。生产编译器通常填入明确的 datalayout，避免跨平台时指针宽度、对齐和字节序假设不一致。本课保持为空，因为所有代码都在本机 JIT 执行。

模块级还有常量。MLP 的第一个常量是 16 个 float 的 bias：

```llvm
@"C_2" = internal constant [16 x float] [float 0x3fc27c5140000000, float 0x3fc1e8960000000, ...]
```

`internal` 表示符号只在当前模块可见，`constant` 表示不可修改，`[16 x float]` 是数组类型。名字 `C_2` 来自 `Value.id`。常量不放进 workspace，而是直接嵌入模块，JIT 后位于可执行内存的只读数据区。

### 2. 类型系统：只用到七种类型

本后端的 IR 只出现这些类型：

| IR 类型 | 含义 | 在本后端中的位置 |
| --- | --- | --- |
| `void` | 无返回值 | `mini_forward_llvm` 的返回类型 |
| `float` | 32 位浮点 | 所有张量元素、算术指令 |
| `i32` | 32 位整数 | GEP 下标、循环计数（将来） |
| `i1` | 1 位布尔 | `fcmp` 的比较结果、`select` 的条件 |
| `float*` | 指向 float 的指针 | 输入、输出、workspace 参数 |
| `[N x float]` | 长度 N 的 float 数组 | 常量全局 |
| `[N x float]*` | 指向数组的指针 | GEP 访问常量全局时 |

张量类型在 IR 里被彻底擦除。`tensor<4,16>` 和 `tensor<4,4>` 都只是 `float*`，区别只存在于编译器生成的索引常量。比如 MLP 的第一个 matmul 输出是 4x16，第二个是 4x4，但函数签名里看不出这两个 shape：

```llvm
define void @"mini_forward_llvm"(float* %".1", float* %".2", float* %".3")
```

一个输入、一个输出、一个 workspace，共三个 `float*`。返回值是 `void`，错误通过 C ABI 的整数返回码传递是 C 后端的做法；LLVM JIT 版本把错误检查放在 Python 侧，函数本身不返回状态。

### 3. 函数签名：参数顺序是 ABI 的一部分

`LLVMCodegen.build` 里计算参数个数：

```python
n_args = len(self.graph.inputs) + len(self.graph.outputs) + 1
fnty = ir.FunctionType(ir.VoidType(), [ir.PointerType(self.float_ty)] * n_args)
self.func = ir.Function(self.module, fnty, name=self.function_name)
```

顺序固定为：

```text
(输入0, 输入1, ..., 输出0, 输出1, ..., workspace)
```

为什么输出也作为参数传入？因为 JIT 函数不负责分配返回数组。调用者准备好输出缓冲区，函数把结果写进去。这样签名对所有图都统一，ctypes 侧按数量传指针即可，不需要处理结构体返回或动态数组。

为什么 workspace 放最后？因为它不是模型语义的一部分，而是编译器的临时内存。把它放在最后，输入和输出的相对位置保持稳定，读签名时容易对应。

真实调用侧在 `LLVMJIT.run`：

```python
inputs = [np.ascontiguousarray(feed[v.name], dtype=np.float32) for v in self.graph.inputs]
outputs = [np.zeros(v.type.shape, dtype=np.float32) for v in self.graph.outputs]
ws = np.zeros(max(1, self.plan.arena_bytes // 4), dtype=np.float32)
args = [a.ctypes.data_as(self.ptr_ty) for a in inputs]
args += [a.ctypes.data_as(self.ptr_ty) for a in outputs]
args.append(ws.ctypes.data_as(self.ptr_ty))
self.cfn(*args)
```

`self.plan.arena_bytes` 是字节数，`np.zeros` 的参数是 float 个数，所以除以 4。`max(1, ...)` 处理 arena 为 0 的图，比如输出直接等于输入的恒等图。这个细节在边界例子里会看到。

### 4. SSA：每个值只定义一次

看一个真实的小图。构造代码：

```python
g = Graph()
x = g.add_input("x", (2, 3))
b = g.add_const("b", np.array([1, 2, 3], np.float32))
y = g.add_op("add", [x, b])
r = g.add_op("relu", [y])
s = g.add_op("sum", [r], {"axis": 1})
g.add_output(s)
```

`plan_memory` 给出的布局：

```text
arena_bytes=128 alignment=64 peak_live_bytes=128
value                 interval     size   offset
add_0                [  0,  1]     64       0
sum_2                [  2,  3]     64       0
relu_1               [  1,  2]     64      64
```

`sum_2` 复用了 `add_0` 的 offset 0，因为 `add_0` 的区间是 `[0,1]`，`sum_2` 从 op 2 开始，满足 `end < start`。`relu_1` 在 op 1 同时读写，不能和 `add_0` 共用。

生成的 IR 前 36 行：

```llvm
; ModuleID = "mini_ai"
target triple = "x86_64-pc-windows-msvc"
target datalayout = ""

@"C_1" = internal constant [3 x float] [float 0x3ff0000000000000, float 0x4000000000000000, float 0x4008000000000000]
define void @"mini_forward_llvm"(float* %".1", float* %".2", float* %".3")
{
entry:
  %".5" = getelementptr inbounds float, float* %".1", i32 0
  %".6" = load float, float* %".5"
  %".7" = getelementptr inbounds [3 x float], [3 x float]* @"C_1", i32 0, i32 0
  %".8" = getelementptr inbounds float, float* %".7", i32 0
  %".9" = load float, float* %".8"
  %".10" = fadd float %".6", %".9"
  %".11" = getelementptr inbounds float, float* %".3", i32 0
  %".12" = getelementptr inbounds float, float* %".11", i32 0
  store float %".10", float* %".12"
  %".14" = getelementptr inbounds float, float* %".1", i32 1
  %".15" = load float, float* %".14"
  %".16" = getelementptr inbounds [3 x float], [3 x float]* @"C_1", i32 0, i32 0
  %".17" = getelementptr inbounds float, float* %".16", i32 1
  %".18" = load float, float* %".17"
  %".19" = fadd float %".15", %".18"
  %".20" = getelementptr inbounds float, float* %".3", i32 0
  %".21" = getelementptr inbounds float, float* %".20", i32 1
  store float %".19", float* %".21"
  %".23" = getelementptr inbounds float, float* %".1", i32 2
  %".24" = load float, float* %".23"
  %".25" = getelementptr inbounds [3 x float], [3 x float]* @"C_1", i32 0, i32 0
  %".26" = getelementptr inbounds float, float* %".25", i32 2
  %".27" = load float, float* %".26"
  %".28" = fadd float %".24", %".27"
  %".29" = getelementptr inbounds float, float* %".3", i32 0
  %".30" = getelementptr inbounds float, float* %".29", i32 2
  store float %".28", float* %".30"
```

这段只覆盖 `add` 的 6 个元素。逐个看：

- `%".1"` 是输入 `x`，`%".2"` 是输出，`%".3"` 是 workspace。
- `%".5"` 是指向 `x[0]` 的指针，`%".6"` 是它指向的 float。
- `%".7"` 指向常量数组 `C_1` 本身，`%".8"` 指向数组第 0 个元素。访问全局数组需要两个下标：第一个 0 穿过数组指针，第二个 0 选元素。
- `%".10" = fadd float %".6", %".9"` 完成 `x[0] + b[0]`。
- `%".11"` 指向 workspace offset 0（`add_0` 的缓冲区），`%".12"` 指向其中第 0 个元素。
- `store` 把结果写进 workspace。

每个 `%".N"` 只出现一次定义。`%".12"` 和 `%".21"` 都是「指向 workspace 第 0 个块的不同元素」的指针值，不是变量赋值。SSA 约束的是这些指针值和算术结果，不约束它们指向的内存。

### 5. getelementptr：按元素算地址，不按字节

`getelementptr`（GEP）是 LLVM IR 里最容易读错、也最容易写错的指令。它的语义是：给定一个指针和若干下标，算出新指针。对 `float*`，下标单位是 float，不是字节。

```llvm
%".14" = getelementptr inbounds float, float* %".1", i32 1
```

这行的意思是「从 `%".1"` 向后走 1 个 float，得到 `x + 4` 字节处的指针」。`inbounds` 是给优化器的承诺：这个地址计算不越出原对象，溢出也不回绕。违反承诺会产生未定义行为（UB，undefined behavior），不是「结果可能错一点」，而是整个程序的行为不再有保证。

后端用三个小函数封装 GEP：

```python
def _ptr(self, v: Value) -> ir.Value:
    if id(v) in self.input_index:
        return self.func.args[self.input_index[id(v)]]
    if v.kind == "constant":
        gv = self.const_globals[id(v)]
        return self.builder.gep(gv, [self._c(0), self._c(0)], inbounds=True)
    if v in self.plan.allocations:
        off = self.plan.offset_of(v)
        if off % 4 != 0:
            raise IRError(f"{v.name}: byte offset {off} is not divisible by 4")
        return self.builder.gep(self.ws_arg, [self._c(off // 4)], inbounds=True)
    raise IRError(f"value {v.name} has no storage")
```

三种情况对应三类存储：

1. 输入：直接返回函数参数指针，不额外 GEP。
2. 常量：先对全局数组做 `[0, 0]`，拿到 `float*`。
3. 临时值：workspace 指针加上 `off // 4` 个 float。

`off` 是字节偏移，GEP 的索引是元素个数，所以必须除以 4。代码还显式检查 `off % 4 == 0`，因为 float 不能从任意字节开始。`plan_memory` 用 64 字节对齐，正常情况下一定满足；这个检查是防御性的，防止以后改对齐策略时悄悄生成错位访问。

`_load` 和 `_store` 在 `_ptr` 之上再加一层元素下标：

```python
def _load(self, v: Value, idx: int) -> ir.Value:
    return self.builder.load(self.builder.gep(self._ptr(v), [self._c(idx)], inbounds=True))

def _store(self, v: Value, idx: int, value: ir.Value) -> None:
    self.builder.store(value, self.builder.gep(self._ptr(v), [self._c(idx)], inbounds=True))
```

所以一次 `x[1]` 读取会生成两层 GEP：先定位缓冲区，再定位元素。MLP 的 IR 有 2928 条 `getelementptr`，大部分来自这两层展开。看起来冗余，LLVM 的优化器会把常量 GEP 折叠掉一部分；即使不折叠，地址也都是编译期常量偏移，执行时只是 `lea` 或直接寻址。

### 6. phi：循环携带的值

`phi`（φ 节点）是 SSA 形式处理控制流汇合的方式。一个基本块如果有多个前驱，块开头的 `phi` 根据「从哪个前驱来」选择不同的值。它解决的是：循环里的变量每轮都被重新赋值，但 SSA 不允许同一个值定义多次。

本后端的全展开 IR **没有 `phi`**。所有下标在 Python 里展开成常量，循环在生成 IR 之前就消失了。实测 MLP 模块：

```text
total_lines 6413
phi_count 0
gep_count 2928
fmul_count 768
```

一旦你改成「生成一个真正的循环」，`phi` 就出现了。下面是用 llvmlite 现场构造的 `count_to(n)`，从 0 加到 n：

```llvm
define float @"count_to"(i32 %"n")
{
entry:
  %".3" = icmp sgt i32 %"n", 0
  br i1 %".3", label %"loop", label %"exit"
loop:
  %".5" = phi  i32 [0, %"entry"], [%".9", %"body"]
  %".6" = phi  float [             0x0, %"entry"], [%".10", %"body"]
  %".7" = icmp slt i32 %".5", %"n"
  br i1 %".7", label %"body", label %"exit"
body:
  %".9" = add i32 %".5", 1
  %".10" = fadd float %".6", 0x3ff0000000000000
  br label %"loop"
exit:
  ret float %".6"
}
```

读法：

- `%".5"` 是循环计数器。第一次从 `entry` 进来取 0；从 `body` 回来取 `%".9"`，也就是上一轮加 1 的结果。
- `%".6"` 是累加器。第一次取 0.0；从 `body` 回来取 `%".10"`，也就是上一轮加 1.0 的结果。
- `phi` 的 incoming 列表必须和实际前驱一一对应。`loop` 的前驱是 `entry` 和 `body`，所以每个 phi 有两个 incoming。
- `exit` 里的 `%".6"` 是最后一次从 `loop` 传出的值。SSA 让「循环结束后的累加器」有明确来源。

如果要把 mini_ai 的 matmul 改成循环 IR，每个 `i/j/k` 循环都需要 phi：`i`、`j`、`k`、`acc`，以及每个 tile 的边界。全展开避开了这些，代价是代码体积随 shape 线性甚至乘积增长。Ch 22 会讨论这个取舍。

### 7. 全展开的规模

MLP 只有两个 `Linear`、一个 ReLU，原始 IR 5 个算子，融合后 2 个。LLVM 模块 6413 行，其中 768 条 `fmul`。为什么是 768？

第一个 matmul：4x16 个输出元素，每个元素 8 次乘加，4 * 16 * 8 = 512 次乘法。
第二个 matmul：4x4 个输出元素，每个元素 16 次乘加，4 * 4 * 16 = 256 次乘法。
合计 768。每条乘法一条 `fmul`，每条累加一条 `fadd`，加法还包含 bias，所以 `fadd` 更多。

C 后端对同一张图生成的是三重循环：

```c
for (int i5 = 0; i5 < 4; ++i5) {
    for (int j5 = 0; j5 < 16; ++j5) {
        float acc = 0.0f;
        for (int k5 = 0; k5 < 8; ++k5) {
            acc += v_0_x0[(i5)*8 + (k5)] * v_3_linear_weight_t[(k5)*16 + (j5)];
        }
        acc += v_2_fc1_bias[j5];
        acc = fmaxf(acc, 0.0f);
        v_12_relu_fused[(i5)*16 + (j5)] = acc;
    }
}
```

同一份计算，C 用 14 行循环表达，LLVM IR 用 6413 行展开表达。两者不是「谁更高级」的关系，而是两种策略：C 让编译器和 CPU 去处理循环，LLVM 后端把循环决策提前到自己的代码生成阶段。全展开适合小 shape、固定 shape、想快速验证 IR 构造；不适合大模型，否则模块会爆炸。

### 8. 边界例子：恒等图与坏拓扑序

先看一个没有算子的图：输入直接作为输出。

```python
g = Graph()
x = g.add_input("x", (3,))
g.add_output(x)
```

`plan_memory` 的结果是：

```text
arena_bytes=64 alignment=64 peak_live_bytes=0
value                 interval     size   offset
```

没有任何 allocation，`peak_live_bytes=0`，但 `arena_bytes` 仍然是 64（`plan_memory` 最后取 `max(alignment, align_up(0, 64))`）。JIT 侧用 `max(1, 0 // 4)` 分配一个 float 的 workspace，避免 `np.zeros(0)` 传入空指针。生成的函数体只有拷贝：

```llvm
entry:
  %".5" = getelementptr inbounds float, float* %".1", i32 0
  %".6" = load float, float* %".5"
  %".7" = getelementptr inbounds float, float* %".2", i32 0
  store float %".6", float* %".7"
  ...
  ret void
```

`LLVMJIT(g).run({"x": np.array([1.5, -2.0, 3.25], np.float32)})` 返回 `[array([ 1.5 , -2.  ,  3.25], dtype=float32)]`。这个例子说明：workspace 不是必需的语义对象，只是临时值的存放处；没有临时值时，函数仍然要维持统一的签名。

再看坏拓扑序。把两个 `relu` 的 op 列表反转后调用 `validate`：

```text
bad order -> IRError op relu uses relu_0 before it is defined
```

`Graph.validate` 按 op 顺序建立 `op_index`，发现某个输入的生产者还没出现，就报这个错。LLVM 后端在 `build()` 第一行调用 `graph.validate()`，所以坏图不会走到 IR 生成。这个顺序检查是 SSA 构造的前提：LLVM 允许前向引用基本块，但不允许使用尚未定义的值。

## 底层机制：图算子到 IR 的映射

| 图算子 | IR 形态 | 关键点 |
| --- | --- | --- |
| `add` / `sub` / `mul` / `div` | 逐元素 `load`、`fadd`/`fsub`/`fmul`/`fdiv`、`store` | 每个元素一组指令，broadcast 用 stride 算下标 |
| `relu` | `fcmp ogt` + `select` | NaN 走 false 分支，结果为 0 |
| `exp` | 调用 `expf` | 声明外部函数，JIT 链接 libm/CRT |
| `matmul` | 三重 Python 循环展开成 `fmul`/`fadd` | 行主序：`a[i*k+kk]`、`b[kk*n+j]` |
| `matmul_bias` | matmul 累加后加 bias | bias 下标由 `_broadcast_flat` 计算 |
| `matmul_bias_relu` | 再加 `fcmp`/`select` | 融合后的 epilogue |
| `sum` | 先清零输出，再逐元素累加 | `axis=None` 全部加到 `out[0]` |
| 输出 | `_copy_outputs` 逐元素 `load`/`store` | 从 workspace 或输入拷贝到输出参数 |

以 matmul 为例，`_emit_matmul` 的核心是三层 Python `for`：

```python
for i in range(m):
    for j in range(n):
        acc = self._zero()
        for kk in range(k):
            av = self._load(a, i * k + kk)
            bv = self._load(b, kk * n + j)
            acc = self.builder.fadd(acc, self.builder.fmul(av, bv))
        if bias is not None:
            bias_idx = _broadcast_flat(bias.type.shape, (m, n), (i, j))
            acc = self.builder.fadd(acc, self._load(bias, bias_idx))
        if op.kind == "matmul_bias_relu":
            acc = self._relu(acc)
        self._store(op.output, i * n + j, acc)
```

`acc` 是 Python 侧的 `ir.Value`，每轮 `fadd` 生成一个新的 SSA 值。循环结束时 `acc` 是最后一次 `fadd` 的结果，直接 `store` 到输出槽。这里没有 `phi`，因为 `acc` 的每一轮都是新的 SSA 值，Python 循环在生成期展开。

`relu` 的实现值得单独看：

```python
def _relu(self, x: ir.Value) -> ir.Value:
    cond = self.builder.fcmp_ordered(">", x, self._zero())
    return self.builder.select(cond, x, self._zero())
```

`fcmp_ordered` 生成 `fcmp ogt`。对 NaN，ordered 比较返回 false，`select` 选 0。NumPy 的 `np.maximum(NaN, 0)` 返回 NaN，所以解释器和 LLVM 在 NaN 输入上不一致。这不是本章的 IR 语法问题，而是语义选择问题，Ch 24 会把它当成真实差分测试案例。读 IR 时看到 `fcmp ogt`，就要想到「NaN 被归到 else 分支」。

## 进一步用法与变体

**循环 + phi。** 把全展开改成按 tile 循环，需要为每个归纳变量建 phi，并在循环体末尾 `br` 回循环头。LLVM 的规范循环形态是 `loop` 头放 phi 和条件分支，`body` 更新值，`exit` 使用最终值。手写时容易漏 incoming 或把 `br` 放在 phi 之前；llvmlite 会直接报错或让 `verify()` 失败。

**alloca + mem2reg。** 另一种构造循环的方式是用 `alloca` 分配局部变量，用 `load`/`store` 更新，再跑 `mem2reg` 提升成 phi。Clang 的 `-O0` 输出大量 alloca，`-O2` 前后对比能看到 mem2reg 的效果。本课直接构造 SSA，跳过了这一步。

**不透明指针（opaque pointer）。** LLVM 15 之后默认用 `ptr` 取代 `float*` 这类类型化指针。llvmlite 0.50 的打印器在本机仍输出 `float*`，所以你从新版 LangRef 复制 `ptr` 示例时，可能和本课后端的文本不一致。语义上两者都能表达同样的地址；写代码时以当前 `str(module)` 的输出为准，不要凭记忆拼语法。

**fast-math 标志。** 可以给 `fadd`、`fmul` 加 `fast` 标志，允许重排、融合和假设无 NaN/Inf。它可能提升性能，也可能改变数值结果。本课后端没有加，所以浮点顺序和解释器一致，误差只有 4.47e-08 量级。

**向量类型。** 把 4 个 float 打包成 `<4 x float>`，用 `load <4 x float>` 和 `fmul <4 x float>` 可以生成 SIMD 指令。代价是尾部处理、对齐要求和 broadcast 逻辑变复杂。当前逐元素标量 IR 交给 LLVM 的自动向量化，MSVC 目标下不一定能识别；这是后续优化点。

## 对比与取舍

| 选择 | 优点 | 代价 | 适用场景 |
| --- | --- | --- | --- |
| 全展开 | 无 phi、无循环边界、IR 构造直接 | 代码体积随元素数增长，6413 行只对应一个小 MLP | 小 shape、教学、快速验证 |
| 循环 + phi | 代码体积与 shape 无关 | 需要归纳变量、边界、phi incoming | 大 shape、生产后端 |
| C 后端 | 循环直观，MSVC 优化成熟 | 依赖 `cl`，IR 不可直接 JIT | 需要 DLL、想看 C 循环 |
| LLVM 后端 | 可 JIT、可跑 LLVM 优化、IR 可检查 | 构建链长，错误信息偏底层 | 快速迭代、跨后端对照 |
| 类型化指针 | 文本里能看出指向 float | 与新版 opaque pointer 文档不一致 | 本课当前 llvmlite |
| `inbounds` GEP | 优化器能假设不越界 | 越界即 UB，调试更难 | 确信索引正确时 |

全展开不是「更差」的实现。它把复杂度从运行时循环移到了编译期生成，换来无 phi、无边界分支、每个地址都是常量。对 4x8 的 MLP，这个选择合理；对 512x512 的 matmul，它会生成几百万条指令，编译时间和内存都不可接受。工程判断的标准是 shape 上界和编译预算，不是「哪种更高级」。

## 踩雷集锦

1. **把 workspace 字节偏移直接当 GEP 下标。** `offset_of` 返回字节，GEP 对 `float*` 的索引是元素。少除 4 会让所有临时值错位，结果可能仍是有限数，差分测试才会抓到。代码里的 `off // 4` 和 `off % 4` 检查就是为这个准备的。
2. **对 `[N x float]` 只给一个下标。** `getelementptr [3 x float], [3 x float]* @C_1, i32 0` 得到的是指向整个数组的指针，不是 `float*`。需要两个下标：第一个穿过数组，第二个选元素。后端用 `[0, 0]`，再对结果做第二层 GEP。
3. **phi 的 incoming 和前驱对不上。** 每个 phi 的条目数必须等于前驱数，且块标签必须是真实前驱。少一个条目，`verify()` 报错；标签写错，IR 解析失败。构造循环时先把 CFG 画出来，再写 phi。
4. **在已终止的基本块里插指令。** `br`、`ret` 之后再调用 `builder` 会抛异常或生成非法 IR。llvmlite 的 builder 有当前位置，切块后要重新 `position_at_end`。
5. **忽略 `fcmp` 的 ordered/unordered。** `ogt` 对 NaN 返回 false，`ugt` 对 NaN 返回 true。ReLU 用哪一个，决定了 NaN 输入的行为。不要只看正常输入测试通过就认为语义正确。

## 进阶

- 读 LLVM LangRef 的 Types、Functions、`getelementptr`、`phi` 四节，对照本章的真实输出逐条查语法。
- 用 `llvmlite.binding.parse_assembly` 解析本章 IR，调用 `verify()`，再故意删掉一个 phi incoming，观察错误信息。
- 把 `_emit_matmul` 改成 tile 循环版本，为 `i/j/k/acc` 建 phi，先用 8x8x8 小 shape 验证，再和全展开版本的输出逐元素比较。
- 给 `fadd`/`fmul` 加 `fast` 标志，测量 MLP 的误差和耗时变化，判断是否值得。
- 用 `llc` 或 `TargetMachine.emit_object` 把模块编译成目标文件，反汇编看 `fmul`/`fadd` 是否变成 SIMD 指令。

## 动手练习

1. **标注恒等图。** 生成 `x -> output` 的 IR，标出每个 `%".N"` 的类型、定义指令、使用点。解释为什么 `arena_bytes=64` 但 `peak_live_bytes=0`。
2. **改一个输出。** 构造两个输出（`relu(x)` 和 `sum(relu(x))`），打印签名，确认参数顺序是「输入、输出0、输出1、workspace」。用 `LLVMJIT` 跑一次，检查返回列表顺序。
3. **写一个 phi。** 用 llvmlite 构造 `sum_to(n)`，从 0 加到 n，验证 `n=0`、`n=1`、`n=10` 的返回值。再删掉一个 incoming，记录 `verify()` 的报错。
4. **制造坏 GEP。** 把 `_ptr` 里的 `off // 4` 临时改成 `off`（只在练习副本里改），跑一个小图，观察结果是错位、崩溃还是静默错误。恢复代码后说明为什么这类 bug 需要差分测试。
5. **读 MLP 的一个 matmul 块。** 在 6413 行 IR 里找到第一个 `fmul`，往后读 20 行，写出它对应的 C 表达式。

## 本章重点

- LLVM IR 是类型化、SSA 的三地址表示；张量 shape 在 IR 里被擦除，只剩 `float*` 和常量索引。
- 函数签名固定为「输入们、输出们、workspace」，全部是 `float*`，返回 `void`。
- `getelementptr` 按元素算地址；workspace 偏移是字节，必须除以 4。
- 常量是 `internal constant [N x float]` 全局，访问需要两层 GEP。
- 全展开 IR 没有 `phi`；循环 IR 的每个归纳变量和累加器都需要 `phi`。
- MLP 的 6413 行 IR 对应 768 次乘法，规模来自逐元素展开，不是实现臃肿。
- `fcmp ogt` 让 ReLU 对 NaN 返回 0，和 NumPy 的 `maximum` 不同，这是真实的语义差异。

## 自我检核

- [ ] 我能说出 `float`、`i32`、`i1`、`void`、`[N x float]`、`float*` 各自出现在哪里。
- [ ] 我能解释 `%".1"`、`%".2"`、`%".3"` 分别是什么参数。
- [ ] 我能从一段 IR 里找出输入、输出、workspace 和常量数组。
- [ ] 我能说明 `getelementptr inbounds float, float* %p, i32 1` 走了多少字节。
- [ ] 我能写出一个带 `phi` 的循环，并解释每个 incoming 的来源。
- [ ] 我能解释为什么全展开 IR 没有 `phi`，以及它的规模代价。
- [ ] 我知道 `fcmp ogt` 对 NaN 的行为，并能把它和解释器差异联系起来。
- [ ] 我能用 `Graph.validate` 的报错定位坏拓扑序。

## 延伸阅读

- **LLVM Language Reference Manual，Types 与 Functions 两节**（https://llvm.org/docs/LangRef.html）：读 `void`、`float`、`iN`、指针、数组、函数签名的定义；对照本章的模块头、常量全局和 `define void`，确认每个类型的确切语义。
- **LLVM Language Reference Manual，`getelementptr` 一节**：重点读「索引按元素计算」「inbounds 的承诺」「数组和结构体的多下标形式」；对照 `_ptr` 的 `off // 4` 和常量数组的 `[0,0]`，理解为什么两种 GEP 下标层数不同。
- **LLVM Language Reference Manual，`phi` 一节**：读 phi 的 incoming 语义和支配关系要求；对照本章 `count_to` 的两个 phi，再尝试把 MLP 的 matmul 改成循环版本。
- **《Engineering a Compiler》第 4 章与第 9 章**：读 SSA 构造、支配边界和循环优化；本章只用了 SSA 的读写形态，这两章补上「为什么 SSA 便于优化」。
- **llvmlite 文档的 IR 构造部分**（https://llvmlite.readthedocs.io/）：读 `IRBuilder`、`phi`、`gep`、`load`/`store` 的 API；对照 `codegen_llvm.py` 的调用方式，写出一个最小模块并 `verify()`。

---

[下一章：Ch 22 llvmlite 后端](./22-llvm-backend.md)