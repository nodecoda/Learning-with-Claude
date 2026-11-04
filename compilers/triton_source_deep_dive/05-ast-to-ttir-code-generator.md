# Ch 5 — AST → TTIR：CodeGenerator 与控制流

> AST 里没有 SSA。SSA 是 `CodeGenerator` 一边遍历一边现造出来的。

## 目标

读完本章，你应该能回答：

- `lscope` 和 `local_defs` 各自回答什么问题？为什么不能合并？
- Python 的 `x = x + 1` 如何变成 SSA？为什么中间不需要 phi 节点？
- 同一个 `if`，什么时候生成 `scf.if`，什么时候生成 `cf.cond_br`，什么时候在编译期消失？
- 循环携带值是怎么被“发现”的？为什么发现过程要先把循环体空跑一遍？
- `break` / `continue` 在 Triton 里到底发生了什么？

本章只讲前端：Python AST 进，TTIR 出。类型提升与广播属于 Ch 6，MLIR 的 C++ 对象模型属于 Ch 11。

## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码目录 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0` |
| GPU | Tesla T4，compute capability 7.5（`sm_75`） |
| 驱动 / CUDA | 535.216.01 / 12.4（`V12.4.131`） |
| 系统 Python | 3.10.12 |
| `import triton` | `ModuleNotFoundError`（系统环境与仓库 `.venv` 均如此） |

因此本章**没有任何 Triton 运行时输出**。凡是从源码推出的 IR 形状都标 `guided`，只说明“有哪些 op、谁产生谁”，不作为逐字 dump。标 `源码依据` 的结论都用 `awk 'NR>=..'` 核对过行号。

**源码依据**：`code_generator.py` 共 1790 行；入口 `ast_to_ttir` 在 `1745-1789`，`CodeGenerator` 类在 `294`，`generic_visit` 在 `1697-1698`。

## 为什么需要这个

只读 TTIR 容易产生三个误解：

1. **以为 `scf.for` 是优化结果**。不是。它从第一版 TTIR 就是 `scf.for`，因为前端只会发射结构化循环。
2. **以为重赋值会自动转成 SSA 重命名**。不是。每次赋值只是把 `lscope[name]` 指向新的 `base_value`，旧值仍被之前的 op 引用。
3. **以为 `if` 里定义的变量外面一定可见**。只有“livein 被改写”或“两个分支都定义”的变量才会被带出 `if`，其余被静默丢弃。

这一层解释了为什么某些写法会报 `Loop-carried variable ... has initial type ...`，为什么 `break` 是编译失败而不是“慢一点”，以及为什么一个无害的 `if` 会把 kernel 变成多个基本块。

## 先建立直觉

**实测**（CPython 3.10.12，`ast.dump`，与 Triton 无关）：

```text
Assign(
  targets=[
    Name(id='acc', ctx=Store())],
  value=BinOp(
    left=Name(id='acc', ctx=Load()),
    op=Add(),
    right=Subscript(
      value=Name(id='p', ctx=Load()),
      slice=Name(id='i', ctx=Load()),
      ctx=Load())))
```

一条语句里 `acc` 同时是 `Store` 和 `Load`。AST 没有“第几个版本”，也没有块参数。Triton 必须自己做三件事：

```text
1. 遍历时维护 name -> 当前值 的映射（lscope）
2. 遇到赋值就替换映射，而不是修改旧值
3. 遇到控制流汇合点，把两边的新值变成 block argument / scf 结果，再重新绑定名字
```

整条链路：

```text
fn.parse() -> ast.Module
  -> CodeGenerator.visit(...)
     -> visit_Assign / visit_If / visit_For / visit_Call ...
        -> self.semantic.*       # 类型与 op 构造（Ch 6）
        -> self.builder.create_* # MLIR builder（C++，Ch 11）
  -> module.verify()              # 1745-1789
```

注意最后一步：前端生成完还要过 MLIR verifier，前端不负责所有合法性。

## 核心概念

### 1. 三张表：`gscope`、`lscope`、`local_defs`

| 表 | 内容 | 生命周期 | 源码 |
|---|---|---|---|
| `gscope` | 全局名字，经 `module_map` 重写 | 整个 `CodeGenerator` | `329-339` |
| `lscope` | 当前可见的 `name -> base_value / constexpr` | 子区域进出时保存/恢复 | `341`、`129-149` |
| `local_defs` | 当前子区域内**定义过**的名字 | 子区域进出时清空/恢复 | `357`、`134-147` |

`set_value` 一次写两张表：

```python
def set_value(self, name: str, value) -> None:
    self.lscope[name] = value
    self.local_defs[name] = value
```

`源码依据`：`453-460`。`lscope` 用于**查找**，`local_defs` 用于**汇合点分析**——判断某个名字是否在本子区域被重新定义过。

`enter_sub_region`（`129-149`）进入 `if` / `for` / `while` body 前保存 `lscope`、清空 `local_defs`；退出时恢复。子区域里的临时变量默认泄漏不出去。

### 2. `builder` 与 `semantic` 的分工

```python
self.builder = ir.builder(context)
self.semantic = TritonSemantic(self.builder)
```

`源码依据`：`302-308`（Gluon 走 `GluonOpBuilder` / `GluonSemantic` 分支）。

- `builder` 是 C++ `ir.builder` 的薄绑定：`create_for_op`、`create_if_op`、`create_yield_op`、`create_cond_branch`、`set_insertion_point_to_start` 这类纯结构操作。
- `semantic` 负责“该生成什么 op、类型是什么、要不要 cast/广播”。

分界示例：`visit_BinOp`（`882-909`）只取操作数并调用 dunder，类型规则在 `tensor.__add__` → `semantic.add`；而 `visit_If`（`1044-1073`）几乎不碰 `semantic`，只做 `cond.to(int1)` 和分支选择。

### 3. 插入点必须和 loc 一起保存

MLIR builder 有状态：`create_*` 往“当前插入点”塞 op。因此：

```python
def _get_insertion_point_and_loc(self):
    loc = self.builder.get_loc()
    ip = self.builder.get_insertion_point()
    return ip, loc

def _set_insertion_point_and_loc(self, ip, loc):
    self.builder.restore_insertion_point(ip)
    self.builder.set_loc(loc)
```

`源码依据`：`462-473`。`visit` 每次进入节点都会 `builder.set_loc(...)`（`1664-1695`）；只恢复插入点不恢复 loc，后续 op 会挂到错误的源码行。

### 4. “SSA 化”就是换字典指针

```python
a = 1.0
a = a + 1.0
```

第一行 `set_value("a", constexpr(1.0))`；第二行先 `dereference_name("a")` 取旧值，算出 `constexpr(2.0)`，再 `set_value("a", ...)`。若 `a` 是 tensor，第二行生成一个 `arith.addf`，结果是新 `ir.value`；旧值没被修改，也没被删除，只是不再被这个名字引用。

## 底层机制

### 1. `visit_FunctionDef`：先建原型，再填 body

顺序（`653-698`）：`visit(args)` → 把默认值变成 `Assign` / `AnnAssign`（`659-674`）→ `prototype.serialize` → `get_or_insert_function` → `add_entry_block` + `prototype.deserialize` → 逐个 `set_value` 绑定参数 → `visit_compound_statement(body)` → `handle_returns()`。

`visit_compound_statement`（`529-538`）遇到 `ast.Return` 立刻停止：return 之后是死代码，不生成。

`handle_returns`（`631-651`）先收集所有 `return` 值，用 `decide_return_type`（`581-610`）合并唯一返回类型，再回到每个 return 的插入点补 `builder.ret(...)`，最后在函数末尾补一条返回 poison 的 `ret`（`650`）。所以 TTIR 里的 `tt.poison` 有时只是“函数末尾不可达但必须终结”。

### 2. `visit_If`：一个语法节点，三种命运

```text
cond 是 tensor?
  ├─ 多元素 tensor        -> 直接报错（1048-1049）
  ├─ 1 元素 block         -> warn + _unsplat 成标量（1050-1057）
  ├─ to(int1)
  │    ├─ 内部有 return   -> visit_if_top_level（CFG + cf.cond_br）
  │    └─ 内部无 return   -> visit_if_scf（scf.if + scf.yield）
  └─ 非 tensor（constexpr / bool / int / None）
       └─ 编译期二选一，只 visit 一个分支（1062-1073）
```

**命运一：constexpr 条件 → 编译期消失。**

```python
@triton.jit
def k(p, BLOCK: tl.constexpr, USE_RELU: tl.constexpr):
    offs = tl.arange(0, BLOCK)
    x = tl.load(p + offs)
    if USE_RELU:
        x = tl.maximum(x, 0.0)
    tl.store(p + offs, x)
```

`_unwrap_if_constexpr(cond)` 得到 Python `bool`，通过 `_condition_types = {bool, int, type(None)}`（`126`）检查后，只 visit 活跃分支（`1062-1073`）。`USE_RELU=False` 时 TTIR 里**根本没有** `maximum` 和 `scf.if`。这是 Triton 最有效的零成本分支。

**命运二：tensor 条件 + 无 return → `scf.if`。**

```python
if tl.sum(x) > 0:
    y = x * 2.0
else:
    y = x - 1.0
tl.store(p + offs, y)
```

`visit_if_scf`（`1009-1042`）依次：建 then/else block → `visit_then_else_blocks` 分别生成并收集 `names` → 回到原插入点 `create_if_op([types], cond.handle, True)` → 把临时 block `merge_block_before` 进 `if_op` 的两个 region → 各写一条 `create_yield_op` → 用 `if_op.get_result(i)` 反序列化新值并 `set_value`。

`guided`：TTIR 形状（只示意结构）：

```mlir
%cond = arith.cmpf ogt, %s, %c0 : f32
%y = scf.if %cond -> (tensor<128xf32>) {
  %r = arith.mulf %x, %two : tensor<128xf32>
  scf.yield %r : tensor<128xf32>
} else {
  %r2 = arith.subf %x, %one : tensor<128xf32>
  scf.yield %r2 : tensor<128xf32>
}
tt.store %addr, %y, %mask : tensor<128x!tt.ptr<f32>>
```

`y` 不是“两个 block 各定义一次”，而是 `scf.if` 的**结果值**；`names` 决定结果顺序。

**命运三：tensor 条件 + 有 return → 顶层 CFG。**

`return` 要终结函数，而 SCF region 只能 `yield`，所以 `visit_if_top_level`（`970-1007`）改用 `cf.cond_br` 建真实基本块：then/else 各自 `cf.br` 到 `endif`，`endif` 用 block argument 汇合。`源码依据`：`977-999`；C++ 绑定在 `python/src/ir.cc:1242-1251`。这是唯一允许函数中途 return 的形态；在 `for` / `while` 里 return 会直接报错。

### 3. `visit_For`：先空跑一遍，再正式生成

支持的迭代器只有 `tl.static_range` 和 `range`（`tl.range` 也走 `range` 分支），其他一律 `RuntimeError('Only `range` and `static_range` iterators are currently supported')`（`1329`）。

**`static_range` 在 Python 层展开**（`1293-1301`）：循环体在 TTIR 里出现 N 次，没有 `scf.for`。

**`range` 走结构化循环**，难点是“哪些变量是 carry”。`_find_carries`（`485-527`）：

1. `create_block()` 建临时 block，插入点设进去；
2. 用 `_discovering_loop_carries()` 包住，**干跑**一遍 body；
3. `block.erase()` 丢掉干跑产生的 IR；
4. 对每个 livein 比较 `live_handles` 与 `loop_handles`：不同则是 carry，记录类型与初始 handle；
5. `lscope = liveins.copy()`、`local_defs = {}` 复原现场。

嵌套循环时，干跑外层会再次进入内层；若内层完整干跑，复杂度指数级。因此有深度计数（`475-483`），内层在干跑期间只生成结果占位。源码注释：

```text
Nested loops need result placeholders, not a second complete body,
while generating IR that an enclosing _find_carries will discard.
```

`源码依据`：`476-479`。

正式生成（`1368-1399`）：`create_for_op(lb, ub, step, init_handles)` → `get_body(0)` → `arg(0)` 是 IV、`arg(1..)` 是 iter_args → 用 `unflatten_ir_values` 绑定 carry 名 → 生成 body → `flatten_values_to_ir(lscope[name])` + `create_yield_op`。循环后 `for_op.get_result(i)` 就是新 carry 值（`1414-1418`）。

```python
acc = tl.zeros((BLOCK,), tl.float32)
for i in range(0, n, BLOCK):
    acc += tl.load(p + i + offs, mask=i + offs < n, other=0.0)
```

`guided`：

```mlir
%res = scf.for %i = %c0 to %n step %c128 iter_args(%acc = %init) -> (tensor<128xf32>) {
  %v = tt.load ... : tensor<128x!tt.ptr<f32>>
  %next = arith.addf %acc, %v : tensor<128xf32>
  scf.yield %next : tensor<128xf32>
}
```

`acc` 在循环里从未“被修改”，只有 `scf.yield` 把新值传给下一次迭代。

**IV 占位符**：body 生成前先 `set_value(target, tensor(iv_placeholder, iv_type))`，`iv_placeholder` 是 `tt.poison`（`1357-1358`）；body 里对 `i` 的使用先引用 poison，生成完再 `iv_placeholder.replace_all_uses_with(iv)`（`1405-1410`）。负步长还要做 `ub - iv + lb` 换算（`1405-1409`），因为 `scf.for` 不支持负 step。

### 4. `visit_While`：before/after 两个 region

`visit_While`（`1204-1269`）同样先 `_find_carries`，再建 `scf.while`：`before_block` 里 `visit(node.test)` 后 `create_condition_op(cond.handle, block_args)`；`after_block` 里生成 body 后 `create_yield_op`（`1211-1258`）。`scf.condition` 同时承担“条件”和“把值传进 after region”，所以必须把**全部** carry 再传一遍。支持 `tl.condition(c, disable_licm=...)` 包装（`1226-1231`），用于挂 `llvm.loop_annotation`。

### 5. `call_JitFunction`：名字修饰 + 按需生成

```python
arg_types = [arg.type for arg in args]
fn_name = mangle_fn(get_full_name(fn), arg_types, caller_context)
if not self.module.has_function(fn_name):
    prototype = ASTFunction([], arg_types, dict())
    generator = CodeGenerator(..., module=self.module, function_name=fn_name, ...)
    generator.visit(fn.parse())
    callee_ret_type = generator.ret_type
```

`源码依据`：`1441-1473`。

- 同一个 `@triton.jit` 函数按实参类型生成多份 `func.func`。
- `mangle_fn`（`32-42`）拼出 `{full_name}__{type_mangles}`；block 类型 mangle 是 `{elt}S{shape}S`（`core.py:780-783`）。`guided`：形如 `mymod._scale__fp32S128S_c2.0`，`c2.0` 来自 `constexpr_type.mangle()`（`core.py:207-212`）。
- 返回类型由被调函数自己算（`decide_return_type`），调用点只做 `unflatten_ir_values`。
- 递归调用会在 `function_ret_types[fn_name]` 尚未写入时失败（返回类型还没算出来），不要依赖递归。

`visit_Call`（`1541-1562`）很薄：`_unwrap_if_constexpr(visit(node.func))`、展开 `Starred`（必须是 `language.tuple`）、收集 kwargs，交给 `call_Function`（`1481-1534`）。后者还检查 `_must_use_result`（`1489-1495`）、给 builtin 注入 `_semantic` / `_generator`（`1503-1513`）、把裸 `tuple` 包成 `language.tuple`（`1516-1518`）。

## 进一步用法/重要变体

`tl.range` 的属性会被搬到 `scf.for` 上（`1368-1384`）：

| Python | IR attribute |
|---|---|
| `num_stages=k` | `tt.num_stages = k` |
| `loop_unroll_factor=k` | `tt.loop_unroll_factor = k` |
| `disallow_acc_multi_buffer=True` | `tt.disallow_acc_multi_buffer`（unit） |
| `flatten=True` | `tt.flatten`（unit） |
| `warp_specialize=True` | `tt.warp_specialize`（unit） |
| `disable_licm=True` | `llvm.loop_annotation`（disable LICM） |

`源码依据`：`1368-1384`；参数定义在 `core.py:3697-3709`。注意 `num_stages` 的两层含义：作为 kernel 参数时只流水化喂给 `dot` 的 load；作为 `tl.range` 属性时流水化该循环里大多数 load（`core.py:3669-3675`）。Ch 19 展开。

`a if cond else b` 走 `visit_IfExp`（`1075-1126`）：tensor 条件下生成带返回值的 `scf.if`，要求两分支类型完全一致，且不定义新变量（`1088` 注释），所以不需要 carry 处理。

`with` 语句由 `visit_With`（`1128-1146`）支持，但内部不允许 `return`（`1145`）。Gluon 的显式资源管理依赖它。

## 对比与取舍

| 选择 | 优点 | 代价 | 什么时候用 |
|---|---|---|---|
| `scf.if` + `yield` | 结构化，可被 SCF pass 分析 | 不能中途 `return`，出口值必须显式 yield | 绝大多数数据相关分支 |
| 顶层 `cf.cond_br` CFG | 支持 `return` | 破坏结构化，SCF 优化不适用 | 只有“提前返回”的 kernel |
| `constexpr` 分支 | 零运行时成本，IR 更小 | 每个取值触发一次特化/编译 | 模板参数、dtype 分支、开关 |
| `tl.static_range` | 完全展开，便于常量折叠 | 代码体积随 trip count 线性增长 | trip count 小且已知 |
| `range` / `tl.range` | 代码体积恒定，可流水化 | 需要 carry 分析，类型必须稳定 | 主循环 |
| 嵌套 `@triton.jit` 调用 | 复用代码、可 noinline | 每个类型组合一份函数 | 复杂辅助逻辑 |

经验规则：**能用 `constexpr` 解决的分支不要用 tensor 条件**。前者编译期消失，后者真的生成 `scf.if`，还会阻止跨分支优化。

## 踩雷集锦

**1. 循环里改变量类型。**

```python
acc = 0.0                                  # fp32 标量
for i in range(0, 10):
    acc = tl.zeros((BLOCK,), tl.float32)   # 变成 block
```

`_verify_loop_carried_variable`（`1194-1202`）报：

```text
Loop-carried variable acc has initial type fp32 but is re-assigned to
tensor<128xf32> in loop! Please make sure that the type stays consistent.
```

`scf.for` 的 `iter_args` 类型在 op 创建时已固定，`int32` 累加器变 `int64` 同理。

**2. 在 `for` / `while` 里 `return`。** 报错文本在 `1056-1059`：

```text
Cannot have `return` statements inside `while` or `for` statements in triton.
```

因为 `scf.for` body 只能 `yield`。想提前退出只能改写循环边界（mask 或先算上界），或在循环外判断。

**3. `break` / `continue` 不是“慢”，是“不存在”。** `CodeGenerator` 没有 `visit_Break` / `visit_Continue`，会落到 `generic_visit`（`1697-1698`）抛出 `unsupported AST node type: Break`。替代写法是用 mask 让后续迭代空转：

```python
for i in range(0, n):
    active = i < stop
    v = tl.load(p + i, mask=active, other=0.0)
    acc += tl.where(active, v, 0.0)
```

**4. `for ... else` / `while ... else` 直接 assert。** `visit_For` 是 `assert False, "Don't know what to do with else after for"`（`1420-1422`），`visit_While` 是 `assert False, "Not implemented"`（`1267-1269`）。用布尔 carry 自己记“是否提前退出”。

**5. 只在 `then` 里定义的变量，出了 `if` 就没了。** `visit_if_scf` 只把 `names` 里的变量写回；`names` 来自“livein 被改写”或“then/else 都定义”（`953-966`）。单分支定义不在其中，`enter_sub_region.__exit__` 又恢复了 `lscope`，所以外面是 `NameError`。`源码依据`：`911-968`、`134-147`、`1035-1041`。

**6. 两个分支给同一个 livein 赋不同类型。** 触发 `947-951` 的断言：

```text
initial value for `v` is of type ... but the then block redefines it as ...
```

`scf.if` 的结果类型必须唯一；混用标量和 tensor 时显式 `tl.full` / `tl.broadcast_to`。

**7. `if` 条件写成多元素 tensor。** 报 `Boolean value of Tensor with more than one value is ambiguous`（`1048-1049`）。先归约：`tl.sum(x) > 0`、`tl.max(x) > 0`，或改用 `tl.where` 做逐元素选择。

## 进阶

- 读 `visit_then_else_blocks`（`911-968`）的两轮扫描：第一轮找“livein 被改写”，第二轮找“then/else 都新定义”。构造“只在 else 改写”的 case，确认它是否进 `names`。
- 读 `decide_return_type`（`581-610`）对 `tuple_type` / `constexpr_type` 的合并规则，解释 `return 1, 2.0` 与 `return 1.0, 2.0` 的类型差异。
- 读 `visit_For` 的负步长处理（`1332-1336`、`1405-1409`），手工推导 `range(10, 0, -2)` 会变成什么 `scf.for`。
- 读 `python/src/ir.cc:1242-1274` 的 builder 绑定，确认 `create_if_op` 的 `withElse` 与 `create_for_op` 的 `initArgs` 顺序。

## 动手练习

1. **空跑分析**：给下面代码画出 `_find_carries` 的结果，指出哪些名字进 `names`、`init_tys` 是什么。**guided**。

   ```python
   a = tl.zeros((BLOCK,), tl.float32)
   b = 0.0
   for i in range(0, n):
       a += tl.load(p + i * BLOCK + tl.arange(0, BLOCK))
       b = b + 1.0
   ```

2. **constexpr 分支**：用 `ACT: tl.constexpr` 在 `relu` / `gelu` 间切换，说明为什么 TTIR 里只出现一个激活函数。**guided**。

3. **制造 carry 类型错误**：让 `acc` 从 `int32` 变 `int64`，记录报错并对照 `_verify_loop_carried_variable` 行号。**guided**。

4. **改写提前退出**：把踩雷 2 的代码改成“循环里不 return”，写出 mask 版与上界版两种等价写法。**guided**。

5. **AST 观察**：用 `ast.dump` 打印含 `if` / `for` 的 kernel 源码，标出所有 `Store` 的 `Name`，解释为什么它们不需要 phi。**实测**（只依赖标准库）。

6. **函数修饰**：写两个 `@triton.jit` 辅助函数，分别接受 `fp32` 与 `fp16` block，推断 mangled name 差异。**guided**。

## 本章重点

- `CodeGenerator` 的核心是**维护名字到 SSA 值的映射**；`lscope` 管可见性，`local_defs` 管汇合点分析。
- 赋值是替换映射，旧值仍被旧 op 引用；这就是不需要显式 SSA 重命名的原因。
- 汇合点必须把新值变成 block argument 或 `scf` 结果再重新绑定；类型不一致直接断言失败。
- `if` 有三种命运：`constexpr` 消除、`scf.if`、顶层 CFG（有 return 时）。
- `for` 只支持 `range` / `tl.static_range`；carry 靠“空跑再比较 handle”发现；IV 先用 poison 占位再替换。
- `break` / `continue` / `for-else` / `while-else` 都不支持，失败方式分别是 `unsupported AST node` 和 `assert False`。

## 自我检核

1. 为什么 `lscope` 和 `local_defs` 不能合并？举一个只有 `local_defs` 能回答的问题。
2. `visit_if_scf` 里 `then_handles == else_handles` 时为什么可以跳过该变量？对应 Python 的什么语义？
3. `_find_carries` 为什么必须 `block.erase()`？不 erase 会怎样？
4. `scf.for` 的 IV 为什么先用 `tt.poison` 占位？不占位会有什么顺序问题？
5. 有 `return` 的 `if` 为什么不能生成 `scf.if`？是 MLIR 的哪条约束阻止了它？
6. `tl.static_range` 与 `range` 在 TTIR 层面的根本差异是什么？各适合什么 trip count？

## 延伸阅读

1. **`python/triton/compiler/code_generator.py:911-968`**
   - 读哪里：`visit_then_else_blocks` 的两轮扫描。
   - 学什么：livein 改写 vs 双分支新定义，为什么只有这两类变量能被带出 `if`。
   - 与本章关系：这是“Python 作用域 → SSA 汇合”的核心，也是踩雷 5/6 的根源。

2. **`python/triton/compiler/code_generator.py:1288-1422`**
   - 读哪里：`visit_For` 全文，重点 `1332-1336`（负步长）、`1357-1358`（poison）、`1368-1399`（iter_args/yield）。
   - 学什么：结构化循环的完整构造，以及 carry 如何从 `_find_carries` 流到 `scf.for`。
   - 与本章关系：本章主线；读完应能手工写出等价 TTIR 骨架。

3. **`python/triton/language/core.py:3652-3709`**
   - 读哪里：`class range` 的构造与属性。
   - 学什么：`num_stages` / `flatten` / `warp_specialize` 的语义与限制。
   - 与本章关系：`visit_For` 只是把这些属性搬到 IR attribute，语义定义在这里。

4. **`python/src/ir.cc:1242-1274`**
   - 读哪里：`create_cond_branch`、`create_for_op`、`create_if_op`、`create_yield_op`、`create_while_op`、`create_condition_op`。
   - 学什么：Python builder 调用如何落到 MLIR 的 `cf` / `scf` op。
   - 与本章关系：把本章流程接到 Ch 11 的 MLIR 对象模型。

## 下一章

本章结束时，你已经知道“哪个 op 在哪个插入点被创建”。但 `x * 2.0 + 1` 为什么是 `fp32`、`p + offs` 为什么变成 `tt.addptr`、`mask` 何时被广播——这些都不在 `CodeGenerator` 里。

→ [Ch 6 语义层：类型、提升、广播与指针](./06-semantic-layer-types-broadcast-and-pointers.md)
