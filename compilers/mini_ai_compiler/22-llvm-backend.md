# 第 22 章 llvmlite 后端：从自有 IR 到 JIT

> **目标**：用 `llvmlite` 把 mini_ai 的计算图构造成 LLVM 模块，经过解析、校验、目标机器和 MCJIT 变成可调用函数；理解 workspace 的所有权、ctypes 参数顺序和 JIT 生命周期；能解释同一张 MLP 在 C 后端与 LLVM 后端上的误差、benchmark 差异和各自代价；能用真实输出判断「LLVM 更快」这句话在什么条件下成立。
>
> **环境**：Windows 10/11 x86_64，Python 3.11.12，torch 2.14.1+cpu，numpy 2.4.6，llvmlite 0.50.0，MSVC 14.43。命令在仓库根目录 `D:\Learning-with-Claude` 执行。设置 `$env:PYTHONPATH = "compilers\mini_ai_compiler\code"` 与 `$env:PYTHONIOENCODING = "utf-8"`。本章数值与耗时来自本机实际运行，命令附在对应小节。

## 为什么需要第二个后端

Ch 16–20 的 C 后端已经能把图变成 DLL：生成 C 源码、调用 `cl /LD /O2 /fp:fast`、用 `ctypes` 加载。它有三个现实优势：循环结构直观、MSVC 优化成熟、产物是普通 DLL，方便交付。它也有三个限制：

1. **编译依赖外部工具链**：没有 `cl` 就什么都跑不了，跨平台要换 `gcc`/`clang` 和另一套编译选项。
2. **IR 不可直接检查**：你看到的是 C 源码，优化后的机器码在编译器内部，想在中途插 pass 或看优化结果要另外的工具。
3. **每次改动都要走一遍外部编译**：迭代慢，错误信息是 C 编译器的文本，不是结构化 IR。

LLVM 后端补上另一条路径：用 `llvmlite` 在进程内构造 LLVM IR，直接即时编译（JIT）成可执行函数。它让你能：

- 在 Python 里检查模块文本，定位每一条 `load`/`fadd`/`store`。
- 用 `binding.parse_assembly` 和 `verify()` 在 JIT 前抓住非法 IR。
- 在同一进程里编译、运行、测量，不用等外部编译器。
- 以后接入 LLVM 的优化 pass、目标文件输出、ORC JIT。

代价是：你需要理解 LLVM IR 的类型和 静态单赋值（SSA，Ch 21），需要管理 JIT 生命周期，还要接受全展开带来的模块膨胀。两条后端不是替代关系，而是同一张图的两种 lowering 策略。Final 会要求两条都跑通，用同一个解释器做金标准对照。

## 先建立直觉

LLVM 后端的完整链路：

```text
Graph
  inputs / constants / ops / outputs
        |
        |  plan_memory
        v
MemoryPlan（arena_bytes、每个 Value 的 offset）
        |
        |  LLVMCodegen.build()
        v
ir.Module（常量全局、函数、entry 块、逐元素指令）
        |
        |  str(module)
        v
LLVM IR 文本 ── llvm_ir(g) 可直接打印
        |
        |  binding.parse_assembly
        v
ModuleRef ── verify()
        |
        |  Target.from_default_triple().create_target_machine()
        v
TargetMachine
        |
        |  binding.create_mcjit_compiler(module, tm)
        v
MCJIT engine ── finalize_object()
        |
        |  get_function_address("mini_forward_llvm")
        v
机器码地址
        |
        |  ctypes.CFUNCTYPE(None, float*, ..., float*)
        v
Python 可调用函数 ── run(feed)
```

两个对象贯穿全程：`ir.Module` 是「怎么写」，`binding` 是「怎么编译和调用」。`llvmlite.ir` 不依赖本机 LLVM 库，负责构造文本；`llvmlite.binding` 绑定 LLVM C++ API，负责解析、校验、优化、代码生成。混淆这两层，就会在「为什么 `ir.Module` 没有 `verify` 方法」这类问题上浪费时间。

内存所有权也要先想清楚：

```text
输入数组      Python/NumPy 分配，调用者持有
输出数组      Python/NumPy 分配，调用者持有
workspace     Python/NumPy 分配，调用者持有
常量数组      编译进 LLVM 模块，JIT 后只读
IR 文本        Python 字符串，可丢弃
JIT engine     LLVMJIT 持有，必须比函数指针活得久
```

生成的 LLVM 函数不调用 `malloc`。它只接收三个（或更多）`float*`，所有临时值写进 workspace。这个设计让 JIT 函数没有分配器依赖，也让调用者能复用 workspace、控制对齐、做内存统计。代价是调用者必须算对 workspace 大小，并在函数返回前保持数组存活。

## 核心内容

### 1. 初始化 binding

`codegen_llvm.py` 顶部有一个模块级标志：

```python
_initialized = False

def _init_binding() -> None:
    global _initialized
    if _initialized:
        return
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    _initialized = True
```

`initialize_native_target()` 注册当前平台的目标后端，`initialize_native_asmprinter()` 注册汇编打印器。没有前者，`Target.from_default_triple()` 找不到目标；没有后者，某些调试/反汇编路径不可用。只初始化一次，重复调用在部分 LLVM 版本会报错或浪费初始化时间。

`llvm_ir(g)` 本身不需要 JIT，但它调用 `build_llvm_module`，只构造 `ir.Module`，不触碰 binding 的目标机器。只有 `LLVMJIT.__init__` 调用 `_init_binding()`。这意味着你可以只打印 IR 而不初始化 JIT，适合快速检查。

### 2. 构造模块：常量、签名、算子、拷贝

`LLVMCodegen.build` 的顺序固定：

```python
def build(self) -> ir.Module:
    self.graph.validate()
    for v in self.graph.values:
        if v.kind == "constant":
            flat = np.asarray(v.data, dtype=np.float32).reshape(-1)
            if flat.size == 0:
                flat = np.zeros(1, dtype=np.float32)
            arr_ty = ir.ArrayType(self.float_ty, len(flat))
            gv = ir.GlobalVariable(self.module, arr_ty, name=f"C_{v.id}")
            gv.linkage = "internal"
            gv.global_constant = True
            gv.initializer = ir.Constant(arr_ty, [float(x) for x in flat])
            self.const_globals[id(v)] = gv
    n_args = len(self.graph.inputs) + len(self.graph.outputs) + 1
    fnty = ir.FunctionType(ir.VoidType(), [ir.PointerType(self.float_ty)] * n_args)
    self.func = ir.Function(self.module, fnty, name=self.function_name)
    entry = self.func.append_basic_block("entry")
    self.builder = ir.IRBuilder(entry)
    self.ws_arg = self.func.args[-1]
    for op in self.graph.ops:
        self._emit_op(op)
    self._copy_outputs()
    self.builder.ret_void()
    return self.module
```

四个决定值得记住：

1. **先 validate**。坏图在生成前被拒绝，不会产生半截模块。
2. **常量展平**。`tensor<4,16>` 的权重变成 64 个 float 的一维数组，shape 信息不进入全局类型。
3. **空常量补一个 0**。`flat.size == 0` 时用 `np.zeros(1)`，避免 `[0 x float]` 这种零长度数组在后续 GEP 中出边界问题。
4. **workspace 是最后一个参数**。`self.ws_arg = self.func.args[-1]`，`_ptr` 用它做临时值基址。

`_emit_op` 按 kind 分派：

```python
if op.kind in {"add", "sub", "mul", "div", "relu", "exp", "add_relu"}:
    self._emit_elementwise(op)
elif op.kind in {"matmul", "matmul_bias", "matmul_bias_relu"}:
    self._emit_matmul(op)
elif op.kind == "sum":
    self._emit_sum(op)
else:
    raise IRError(f"unsupported op: {op.kind}")
```

每类算子都逐元素展开，不生成循环。Ch 21 已展示 matmul 的三重 Python 循环。`_copy_outputs` 最后把每个输出值从它的存储位置拷贝到输出参数：

```python
def _copy_outputs(self) -> None:
    for i, v in enumerate(self.graph.outputs):
        dst = self.func.args[len(self.graph.inputs) + i]
        for idx in range(max(1, v.type.numel)):
            self.builder.store(self._load(v, idx), self.builder.gep(dst, [self._c(idx)], inbounds=True))
```

`max(1, ...)` 再次处理零元素张量：至少拷贝一个 float，避免生成空循环。对本课的静态 shape，零元素张量只在极端边界出现，但这个防御让签名和行为保持一致。

### 3. workspace：大小、偏移与复用

`LLVMJIT.run` 分配 workspace：

```python
ws = np.zeros(max(1, self.plan.arena_bytes // 4), dtype=np.float32)
```

`arena_bytes` 是 MemoryPlan 的字节数，`np.zeros` 要元素个数，所以除以 4。MLP 的 `arena_bytes=320`，workspace 是 80 个 float。`plan.allocations` 里两个值：

```text
relu_fused           [  0,  1]    256       0
linear_1_fused       [  1,  2]     64     256
```

第一个值占 offset 0 到 255，第二个占 256 到 319。`_ptr` 把 offset 256 转成 GEP 下标 64。真实 IR 末尾能看到：

```llvm
%".6400" = getelementptr inbounds float, float* %".3", i32 64
%".6401" = getelementptr inbounds float, float* %".6400", i32 15
%".6402" = load float, float* %".6401"
%".6403" = getelementptr inbounds float, float* %".2", i32 15
store float %".6402", float* %".6403"
```

这是第二个 matmul 输出的最后一个元素：workspace 第 64 个 float 起（offset 256），第 15 个元素（4x4 的最后一行最后一列），拷贝到输出参数第 15 个位置。

workspace 不在函数内分配，有三个后果：

- 每次调用不触发 `malloc`，适合高频小算子。
- 调用者可以复用同一块 workspace，减少分配噪声。
- 调用者必须保证 `arena_bytes` 足够；如果传小了，函数会越界写。

`LLVMJIT.run` 每次都新建 `np.zeros`，所以它复用的是「计划」而不是「缓冲区」。如果要进一步压测，可以把 workspace 缓存在 `LLVMJIT` 实例上，但要注意并发调用时不能共享同一块临时内存。

### 4. JIT 流水线：从文本到函数地址

`LLVMJIT.__init__` 是整章的核心：

```python
class LLVMJIT:
    def __init__(self, graph: Graph, *, plan: MemoryPlan | None = None) -> None:
        _init_binding()
        module, self.plan = build_llvm_module(graph, plan)
        backing = binding.parse_assembly(str(module))
        backing.verify()
        target = binding.Target.from_default_triple()
        self.tm = target.create_target_machine()
        self.engine = binding.create_mcjit_compiler(backing, self.tm)
        self.engine.finalize_object()
        self.engine.run_static_constructors()
        addr = self.engine.get_function_address("mini_forward_llvm")
        if not addr:
            raise RuntimeError("LLVM JIT did not find mini_forward_llvm")
        self.graph = graph
        self.ptr_ty = ctypes.POINTER(ctypes.c_float)
        self.cfn = ctypes.CFUNCTYPE(
            None,
            *([self.ptr_ty] * (len(graph.inputs) + len(graph.outputs) + 1)),
        )(addr)
```

逐步解释：

1. `build_llvm_module` 返回 `ir.Module` 和 `MemoryPlan`。`plan` 必须和模块一起保留；`run` 用它算 workspace 大小。
2. `str(module)` 把模块转成文本，`parse_assembly` 重新解析成 LLVM 的 `ModuleRef`。这一步会做语法检查。
3. `backing.verify()` 做 IR 语义校验：SSA 定义、类型匹配、phi 前驱、基本块终止符。校验失败会抛异常，不会进入 JIT。
4. `Target.from_default_triple()` 选本机目标，`create_target_machine()` 创建代码生成器。`create_target_machine()` 的 `opt` 参数默认是 2，代码生成阶段会做优化；本课没有额外跑 IR 层的 pass manager。
5. `create_mcjit_compiler(backing, self.tm)` 创建 MCJIT 引擎，`finalize_object()` 完成代码生成和重定位，`run_static_constructors()` 执行模块级构造函数（本课没有，但这是标准步骤）。
6. `get_function_address("mini_forward_llvm")` 拿到机器码地址。找不到时返回 0，代码显式报错。
7. `ctypes.CFUNCTYPE(None, *pointers)` 把地址包装成 Python 可调用对象。`None` 表示返回 `void`，参数个数和顺序必须与 IR 签名完全一致。

生命周期是硬约束：`self.engine`、`self.tm`、`backing` 都必须活着。如果只把 `engine` 放在局部变量里，函数返回后引擎可能被回收，`self.cfn` 指向已释放的机器码，调用时会崩溃。`LLVMJIT` 把它们都挂在 `self` 上，就是为了拖住生命周期。

### 5. 调用：参数顺序与数组存活

`run` 的实现：

```python
def run(self, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
    inputs = [np.ascontiguousarray(feed[v.name], dtype=np.float32) for v in self.graph.inputs]
    outputs = [np.zeros(v.type.shape, dtype=np.float32) for v in self.graph.outputs]
    ws = np.zeros(max(1, self.plan.arena_bytes // 4), dtype=np.float32)
    args = [a.ctypes.data_as(self.ptr_ty) for a in inputs]
    args += [a.ctypes.data_as(self.ptr_ty) for a in outputs]
    args.append(ws.ctypes.data_as(self.ptr_ty))
    self.cfn(*args)
    return outputs
```

几个细节：

- `np.ascontiguousarray(..., dtype=np.float32)` 保证行主序连续、类型正确。如果传入非连续数组，它会复制；复制后的数组必须在调用期间存活，局部变量 `inputs` 做到了这一点。
- 输出数组按 `graph.outputs` 的顺序和 shape 创建，函数返回后直接返回这些数组。
- workspace 是 `np.zeros`，不是 `np.empty`。零初始化让未写满的边界值也有确定内容，便于调试；代价是每次调用多一次清零。生产实现可以用 `np.empty`，但必须先保证每个输出元素都被写。
- `a.ctypes.data_as(self.ptr_ty)` 拿到数据指针。`args` 列表本身也必须存活到 `self.cfn(*args)` 返回；Python 的局部变量保证这一点。
- `run` 不检查 `feed` 的 shape，只按名字取数组。如果 shape 和 IR 不一致，函数会按 IR 的索引读写，可能越界。调用者要在外层验证 shape；这是当前实现的明确边界。

`jit_run(graph, feed)` 是便利函数：

```python
def jit_run(graph: Graph, feed: dict[str, np.ndarray], *, plan: MemoryPlan | None = None) -> list[np.ndarray]:
    return LLVMJIT(graph, plan=plan).run(feed)
```

它每次调用都新建 JIT 引擎、重新编译。正确性测试可以用它，benchmark 不要用：编译时间会混进测量。复用 `LLVMJIT` 实例才能把编译和运行分开。

### 6. 正常路径：MLP 端到端

运行课程 demo：

```powershell
cd D:\Learning-with-Claude
$env:PYTHONPATH = "compilers\mini_ai_compiler\code"
$env:PYTHONIOENCODING = "utf-8"
& ".scratch\aic-env\Scripts\python.exe" "compilers\mini_ai_compiler\code\examples\demo_e2e.py"
```

本机一次实际输出（节选）：

```text
=== optimized graph ===
  00 %relu_fused = matmul_bias_relu(x0, linear_weight_t, fc1_bias) : tensor<4, 16>
  01 %linear_1_fused = matmul_bias(relu_fused, linear_1_weight_t, fc2_bias) : tensor<4, 4>
outputs:
  linear_1_fused: tensor<4, 4>
=== memory plan ===
arena_bytes=320 alignment=64 peak_live_bytes=320
=== C ===
max_abs_error: 4.470348358154297e-08
=== LLVM ===
ir_lines: 6413
max_abs_error: 4.470348358154297e-08
=== benchmark ===
C median ms: 0.1805
LLVM median ms: 0.0592
PyTorch median ms: 0.2104
```

同一脚本第二次运行：

```text
C median ms: 0.2013
LLVM median ms: 0.0605
PyTorch median ms: 0.2164
```

两次的 `max_abs_error` 完全一致：`4.470348358154297e-08`。这个值是与 PyTorch 输出比较的最大绝对误差。C 和 LLVM 都落在同一个量级，说明两条后端的数值路径一致到 float32 舍入范围。IR 行数 6413 也稳定，因为 shape 和融合结果不变。

benchmark 的差异需要解释，不能直接下结论「LLVM 比 C 快 3 倍」。demo 的闭包是：

```python
c_stats = benchmark(lambda: run_c_graph(graph, {"x0": x.numpy()}, result.dll_path), warmup=5, repeat=30)
llvm_stats = benchmark(lambda: jit.run({"x0": x.numpy()}), warmup=5, repeat=30)
```

`run_c_graph` 每次都调用 `load_c_function`，也就是每次都 `ctypes.CDLL(dll_path)`、重新设置 `argtypes`、创建包装函数。这个开销算在 C 的 0.1805 ms 里。LLVM 的 `jit.run` 不重新编译，但每次都分配输入副本、输出数组和 workspace。两个闭包包含的工作不同，绝对耗时不能直接当作 kernel 时间。

### 7. C 与 LLVM 对照

| 维度 | C 后端 | LLVM 后端 |
| --- | --- | --- |
| 产物 | `.c` 源码 + `.dll` | 进程内机器码；也可 `emit_object` 出目标文件 |
| 编译依赖 | MSVC `cl`（或其它 C 编译器） | llvmlite 自带的 LLVM 绑定 |
| 循环形态 | 显式 `for`，可读 | 全展开，无 `phi` |
| 临时内存 | 函数内 `_aligned_malloc(arena_bytes, 64)` | 调用者传入 workspace |
| 输出 | `float* out0`，函数内拷贝 | 同样的输出参数，`_copy_outputs` |
| 错误码 | `int` 返回 0/1 | `void`，Python 侧检查 |
| 优化 | `/O2 /fp:fast`，MSVC 自动向量化 | 默认 codegen；可另跑 LLVM pass |
| 调试 | 看 C 源码、`build.log` | 看 IR 文本、`verify()`、反汇编 |
| 启动成本 | 外部进程编译，秒级 | JIT 编译，毫秒到百毫秒级 |
| 适合 | 交付 DLL、看循环 | 快速迭代、IR 实验、跨后端对照 |

C 后端的 arena 在函数内分配和释放：

```c
const size_t arena_bytes = 320;
float* arena = (float*)_aligned_malloc(arena_bytes, 64);
if (!arena) return 1;
...
_aligned_free(arena);
return 0;
```

LLVM 后端把这块内存交给调用者：

```python
ws = np.zeros(max(1, self.plan.arena_bytes // 4), dtype=np.float32)
```

这个差异直接影响 benchmark：C 每次调用都 `malloc/free`，LLVM 每次调用都 `np.zeros`（也分配），但分配器不同、清零策略不同。如果要做公平的 kernel 对比，需要把 workspace 缓存在外面，C 侧也改成调用者传 workspace，或者至少把分配从计时循环里拿掉。Ch 23 会专门讲测量方法。

### 8. 边界例子：恒等图、NaN 与缺失输入

**恒等图。** 输入直接作为输出，`arena_bytes=64`，`peak_live_bytes=0`。`LLVMJIT` 仍然创建 1 个 float 的 workspace，函数体只有逐元素拷贝。实际运行：

```text
run: [array([ 1.5 , -2.  ,  3.25], dtype=float32)]
```

这个例子验证了「workspace 可以为空」「输出可以直接来自输入指针」。`_copy_outputs` 对输入值调用 `_load`，`_ptr` 返回输入参数，不需要 allocation。

**NaN。** 构造 `relu(x)`，输入 `[NaN, -1.0]`：

```text
interpreter: [nan  0.]
llvm: [0. 0.]
nan agree: False
```

解释器用 `np.maximum(a[0], 0.0)`，NaN 传播；LLVM 用 `fcmp ogt` + `select`，NaN 比较为 false，结果 0。两者都「没有崩溃」，但语义不同。如果你的模型可能出现 NaN，必须明确选择哪一种，并在测试里写死。当前课程没有声明 NaN 策略，这是一个已知差异，不是 JIT 的随机错误。

**缺失输入。** `run` 按名字取 `feed[v.name]`，缺键会抛 `KeyError`：

```text
KeyError: 'x0'
```

它不会静默用零填充。这个设计比「缺输入就补零」安全，因为补零会产生一个看似合理但错误的结果。调用者要在进入 JIT 前校验输入集合和 shape。

## 底层机制：为什么 LLVM 可能更快，也可能更慢

LLVM 后端的潜在优势：

1. **常量索引**：全展开后每个地址都是编译期常量，LLVM 可以把 GEP 折叠成基址加立即数，减少运行时整数运算。
2. **寄存器分配**：中间值可以留在寄存器里，不像 C 的 `acc` 变量那样依赖编译器优化。
3. **无函数调用开销**：整个前向是一个函数，没有逐算子调度。
4. **可选优化 pass**：以后可以在 JIT 前跑 `mem2reg`、`instcombine`、`loop-unroll` 等。

潜在劣势：

1. **代码体积**：6413 行 IR 对应一个小 MLP；大 shape 会导致指令缓存压力和编译时间爆炸。
2. **没有循环级优化**：全展开让 LLVM 看不到「这是一个矩阵乘」，难以做 tiling、循环交换、cache blocking。
3. **默认 codegen 不等于最优**：本课没有跑 LLVM 优化 pass，生成的机器码质量取决于后端默认行为。
4. **测量开销**：`jit.run` 每次分配 NumPy 数组，Python 侧开销可能大于 kernel 本身。
5. **JIT 启动成本**：`LLVMJIT` 构造一次要解析、校验、代码生成；对小模型，这部分可能比运行还贵。

因此，「LLVM 0.0592 ms vs C 0.1805 ms」只能说明：在这个 demo 的闭包、这个 shape、这台机器、这次运行里，LLVM 路径的端到端耗时更低。它不能说明 LLVM 生成的 kernel 一定比 MSVC 的 C kernel 快。要回答后者，需要固定 workspace、去掉 DLL 重载、用同一套计时框架，并重复多轮。Ch 23 给出方法。

## 进一步用法与变体

**ORC JIT。** MCJIT 是较老的引擎，LLVM 现在主推 ORC（On-Request Compilation）。ORC 支持分层编译、懒编译、并发，但 llvmlite 的高层封装较少。教学用 MCJIT 足够，生产要考虑 ORC。

**AOT 目标文件。** `TargetMachine.emit_object(module)` 可以输出 `.obj`，再链接成 DLL/可执行文件。这样保留 LLVM 的代码生成，但不需要在运行时 JIT。适合交付和跨机器部署。

**优化 pass。** `binding.ModulePassManager` 可以加 `InstructionCombiningPass`、`ReassociatePass`、`GVNPass`、`LoopRotatePass` 等。跑 pass 前要 `initialize()`，跑完再 `finalize()`。本课没有启用，因为全展开 IR 的优化空间和循环 IR 不同，盲目加 pass 可能让数值结果变化。

**fast-math。** 给 `fadd`/`fmul` 设置 `fast` 标志，允许重排和融合。它能提升向量化机会，但会破坏与解释器的逐位一致性。是否启用要看容忍度，不要默认打开。

**缓存 JIT 结果。** 对固定图，可以按 `(graph 结构哈希, shape, dtype)` 缓存 `LLVMJIT` 实例，避免重复编译。缓存键要包含常量内容或常量哈希，否则换权重后还在用旧机器码。

**多输出与多输入。** 当前签名按 `inputs + outputs + workspace` 排列。增加输入或输出只改变参数个数，`run` 的列表构造会自动适配。ctypes 的 `CFUNCTYPE` 也在构造时按数量展开，不需要手写每个 arity。

## 对比与取舍

| 目标 | 选择 | 理由 |
| --- | --- | --- |
| 快速验证数值 | 解释器 | 无编译、易调试、作为金标准 |
| 交付独立 DLL | C 后端 | ABI 直接、部署方便、循环可读 |
| 进程内快速迭代 | LLVM JIT | 不用外部编译器、IR 可检查 |
| 大 shape 高性能 | 循环 IR + LLVM pass，或 C + tiling | 全展开会爆炸 |
| 跨平台 | LLVM 目标文件 / C 源码 | 不依赖 MSVC |
| 严格 NaN 语义 | 解释器或显式 NaN 处理 | 当前 ReLU 路径不一致 |

一个常见误区是「先用 LLVM，性能自然更好」。LLVM 只是基础设施，性能来自 IR 形态、循环结构、向量化和内存布局。全展开 IR 把优化机会交给 LLVM 的指令级优化，但丢掉了循环级 tiling；C 后端保留了循环，却依赖 MSVC 的自动优化。两者都需要你明确目标，再选择结构。

## 踩雷集锦

1. **忘记初始化 native target。** 直接 `Target.from_default_triple()` 可能报「no targets are registered」。`LLVMJIT` 在 `__init__` 里调用 `_init_binding()`，手写 JIT 时要照做。
2. **JIT engine 的生命周期与图不匹配。** 只保留 `ctypes` 函数对象、不保留 `engine`/`target_machine`，调用时可能访问已释放的机器码；另一边，`LLVMJIT` 持有 `self.graph` 和 `self.plan`，机器码已按旧图生成。把引擎挂在实例上并让它活到所有调用结束；图一旦修改，就重新构造 JIT。
3. **workspace 字节数当元素数。** `np.zeros(plan.arena_bytes)` 会分配 4 倍内存，虽然不一定报错，但浪费且掩盖真实占用；正确是 `arena_bytes // 4`。反过来，传小了会越界写。
4. **ctypes 参数个数或顺序错。** IR 是 `inputs, outputs, workspace`，少传一个或把输出放前面，函数会把输出指针当输入读，结果可能是垃圾或崩溃。用 `len(inputs)+len(outputs)+1` 统一构造。
5. **用 `jit_run` 做 benchmark。** 它每次新建引擎、重新编译，测到的是「编译 + 运行」。基准测试要用 `LLVMJIT` 实例，把编译放在计时循环外。

## 进阶

- 用 `binding.ModulePassManager` 加 `InstructionCombiningPass` 和 `GVNPass`，对比优化前后的 IR 行数、误差和耗时。
- 用 `TargetMachine.emit_object` 输出 `.obj`，再用 `cl` 链接成 DLL，比较 AOT 与 JIT 的启动时间和运行时间。
- 把 workspace 缓存在 `LLVMJIT` 实例上，去掉每次 `np.zeros`，重新测 MLP；注意并发调用时不能共享同一块临时内存。
- 给 ReLU 加一个 NaN 策略开关：`nan_to_zero` 或 `propagate`，分别用 `fcmp ogt` 和 `fcmp ord` + `select` 实现，写差分测试固定行为。
- 把全展开 matmul 改成 8x8 tile 循环，用 `phi` 维护 `i/j/k/acc`，和全展开版本逐元素比较。

## 动手练习

1. **打印并注释。** 对 `x(2,3) + b(3) -> relu -> sum(axis=1)` 调用 `llvm_ir`，标出输入、输出、workspace、常量数组、每个临时值的 offset。
2. **改多输出。** 构造两个输出，打印签名，确认参数顺序；用 `LLVMJIT` 跑一次，检查返回列表的元素顺序和 shape。
3. **测编译与运行。** 分别计时 `LLVMJIT(g)` 构造和 `jit.run(feed)`，说明为什么 benchmark 不能把两者混在一起。
4. **制造 NaN 差异。** 用 `[NaN, -1]` 跑 `relu`，记录解释器和 LLVM 的输出；再写一个 `nan_to_zero=False` 的变体，让两者一致。
5. **公平对比。** 把 C 侧的 `run_c_graph` 改成「只加载一次 DLL」，LLVM 侧缓存 workspace，再用同一个 `benchmark` 函数测 30 次，比较 median/min/max。

## 本章重点

- llvmlite 分两层：`ir` 构造模块文本，`binding` 解析、校验、JIT。
- 函数签名固定为「输入、输出、workspace」，全部 `float*`，返回 `void`。
- workspace 由调用者分配，大小是 `arena_bytes // 4` 个 float；JIT 函数不调用 `malloc`。
- JIT 流水线是 parse → verify → target machine → MCJIT → finalize → `get_function_address` → ctypes。
- demo 实测 `max_abs_error=4.470348358154297e-08`，C 与 LLVM 一致；`ir_lines=6413`。
- demo benchmark 两次为 C 0.1805/0.2013 ms、LLVM 0.0592/0.0605 ms，但闭包包含 DLL 重载和数组分配，不能直接当作 kernel 时间。
- 恒等图、NaN、缺失输入是三个有用的边界案例：分别验证空 workspace、语义差异和错误处理。

## 自我检核

- [ ] 我能画出从 `Graph` 到 `ctypes` 函数指针的完整链路。
- [ ] 我能解释 `parse_assembly`、`verify`、`create_mcjit_compiler` 各自做什么。
- [ ] 我知道 workspace 为什么用 `arena_bytes // 4`，以及 `max(1, ...)` 处理什么。
- [ ] 我能说明 C 与 LLVM 的临时内存所有权差异。
- [ ] 我能解释 demo benchmark 为什么不能直接证明 LLVM kernel 更快。
- [ ] 我知道 `jit_run` 和 `LLVMJIT` 的区别，以及 benchmark 应该用哪个。
- [ ] 我能复现 NaN 输入下解释器与 LLVM 的差异。
- [ ] 我能列出至少三个 JIT 生命周期或 ctypes 参数的踩雷点。

## 延伸阅读

- **llvmlite 文档，Binding 层**（https://llvmlite.readthedocs.io/en/latest/user-guide/binding/）：读 `parse_assembly`、`verify`、`Target`、`create_mcjit_compiler`、`get_function_address` 的 API；对照 `LLVMJIT.__init__`，确认每一步的作用和生命周期。
- **LLVM MCJIT 设计文档**（https://llvm.org/docs/MCJITDesignAndImplementation.html）：读 MCJIT 的对象缓存、最终化和重定位流程；理解为什么 `finalize_object()` 之后才能取函数地址。
- **llvmlite 示例 `examples/llvmir.py` 与 `examples/jit.py`**：读最小 IR 构造和 JIT 调用；对照本课的 workspace/ctypes 版本，比较多了哪些工程约束。
- **CPython `ctypes` 文档，`CFUNCTYPE` 与指针类型**（https://docs.python.org/3/library/ctypes.html）：读回调函数类型、`POINTER`、`data_as` 的用法；本章的参数顺序和数组存活规则都依赖这些语义。
- **《Engineering a Compiler》第 10 章与第 11 章**：读指令选择、寄存器分配和代码生成；本章只到 IR 和 JIT，这两章解释机器码阶段的取舍。

---

[下一章：Ch 23 自动调优](./23-autotuning.md)