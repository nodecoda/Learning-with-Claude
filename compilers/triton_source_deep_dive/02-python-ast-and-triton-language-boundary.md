# Ch 2 — Python AST 与 Triton 的语言边界

> Triton 没有自己的 parser；它借用 CPython 的 AST，然后在访问器里重新定义什么叫“合法”。

## 目标

读完本章，你应该能回答：
- `@triton.jit` 的函数源码在哪一步离开 CPython、在哪一步进入 Triton？
- `inspect.getsource`、`JITFunction.src`、`fn.parse()` 三者是什么关系？
- Triton 为什么接受 `if`/`for`/`with`，却拒绝 `break`、嵌套 `def`、`x[i] = v`？
- 同一个 `ast.If`，为什么有时只生成一个分支、有时生成 `scf.if`、有时生成 CFG？
- kernel 里的函数调用是怎么变成 MLIR 内联代码的？

主线是 `python/triton/compiler/code_generator.py`：它把“CPython 能解析的 Python 子集”收缩成“Triton 能降低的语言子集”，这条收缩线就是**语言边界**。

## 环境/版本

**实测**：源码 `/home/ubuntu/aicomp/triton`，commit `a972119e92`，版本 `3.9.0`（`python/triton/__init__.py:2`）；GPU 为 Tesla T4（compute capability 7.5，15360 MiB），驱动 535.216.01，CUDA 12.4（`V12.4.131`），系统 Python 3.10.12。

**实测**：`python3 -c "import triton"` 报 `ModuleNotFoundError`；仓库内 `.venv` 的 `site-packages` 也只有 `pip`/`certifi`。所以任何需要 `libtriton.so` 的步骤都跑不了。

证据分三类：**实测**（只依赖 CPython 的 `inspect.getsource`/`ast.parse`/`ast.dump` 和异常行为，用系统 Python 3.10.12 真跑）、**源码依据**（`文件:行号`，均用 `nl -ba` 在当前 commit 核对）、**guided**（TTIR 片段按源码路径推导，说明“应该看到什么”，不冒充本机输出）。装好 Triton 后可用 `TRITON_KERNEL_DUMP=1` 把 guided 换成实测。

## 为什么需要这个

常见误解是“Triton 是新语言，所以有自己的 parser”。不是：输入永远是合法 Python，CPython 先完成词法/语法分析生成 `ast.Module`，Triton 只在其上做一次 `NodeVisitor` 遍历。源码里没有 `.y`/`.l`，也没有独立 grammar。

三个直接后果：

1. **语法错误来自 CPython**。少冒号得到 `SyntaxError`，不是 `CompilationError`。
2. **“不支持 X”不是语法错误，而是访问器错误**。`break` 能被 `ast.parse` 解析，但 `CodeGenerator` 没有 `visit_Break`，于是落到 `generic_visit` 抛 `UnsupportedLanguageConstruct`。
3. **AST 没有类型**。`a + b` 只是 `BinOp`；最终是 `arith.addi`、`arith.addf` 还是 `tt.addptr`，取决于 `a`/`b` 在 Python 侧的对象类型（Ch 6）。

所以语言边界是四层叠加：

```text
CPython 语法（什么能 parse）
  -> CodeGenerator 访问器（什么能 visit）
  -> 语义层类型规则（什么能算）
  -> TTIR verifier（什么能通过）
```

本章负责前两层；第三层见 Ch 5/Ch 6，第四层见 Ch 10。

## 先建立直觉

取 `python/tutorials/01-vector-add.py:29-54` 同形的 kernel：

```python
@triton.jit
def add_kernel(x_ptr, y_ptr, output_ptr, n_elements, BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(axis=0)
    block_start = pid * BLOCK_SIZE
    offsets = block_start + tl.arange(0, BLOCK_SIZE)
    mask = offsets < n_elements
    x = tl.load(x_ptr + offsets, mask=mask)
    y = tl.load(y_ptr + offsets, mask=mask)
    output = x + y
    tl.store(output_ptr + offsets, output, mask=mask)
```

**实测**（Python 3.10.12；用 identity 装饰器替代 `triton.jit`，只观察 CPython）：

```text
body node types: ['Assign', 'Assign', 'Assign', 'Assign', 'Assign', 'Assign', 'Assign', 'Expr']
call: tl.load keywords: ['mask']
```

**实测**：`inspect.getsource` 返回的内容**包含装饰器行**。`JITCallable.__init__` 先 `textwrap.dedent`，再用正则从第一个 `def` 处切掉装饰器（`python/triton/runtime/jit.py:501-503`）。我在 CPython 里按同样三步复现，`'@triton.jit\ndef k(x):\n    return x\n'` 被切成 `'def k(x):\n    return x\n'`（**实测**）。

```text
fn.__code__.co_filename
  -> inspect.getsourcelines(fn)            # jit.py:487
  -> dedent + 去装饰器                       # jit.py:501-503
  -> JITFunction.src -> fn.parse()          # jit.py:588-596, 564-568
  -> CodeGenerator.visit(ast.Module)        # code_generator.py:1745-1789
```

`JITFunction.src` 是只读的：`_set_src` 直接 `raise AttributeError`，唯一改法 `_unsafe_update_src` 还要调用方自己清 hash（`jit.py:578-596`）。**源码文本就是编译输入的一部分，改它等于改 cache key。**

## 核心概念

### CPython 到哪里结束

| 阶段 | 归属 | 产物 |
|---|---|---|
| 词法/语法分析、装饰器调用 | CPython | `ast.Module`、`JITFunction` |
| AST 遍历 | Triton `CodeGenerator` | MLIR `ModuleOp`（TTIR） |
| 类型检查/op 构造 | Triton `TritonSemantic` | `tt.*` / `arith.*` |
| IR 验证 | MLIR verifier | 通过或 `RuntimeError` |

源码依据：`jit.py:564-568` 的 `parse()` 很短：`ast.parse(self._src)` 后两个 `assert`，再 `return tree`；`assert len(tree.body) == 1` 且 `assert isinstance(tree.body[0], ast.FunctionDef)`——**一个 `@triton.jit` 函数必须解析出恰好一个顶层 `FunctionDef`**。

### 语言边界：`visit_*` 方法表

`CodeGenerator` 继承 `ast.NodeVisitor`（`code_generator.py:294`）。支持什么语法，取决于有没有对应方法：

| Python 语法 | 处理 | 源码 |
|---|---|---|
| `x = ...` / `x: tl.constexpr = 42` | `visit_Assign` / `visit_AnnAssign` | `789-798` / `720-738` |
| `x += 1` / `x := ...` | 改写成普通赋值再 visit | `800-816` |
| `if` / 三元 / `for` / `while` | `visit_If` / `visit_IfExp` / `visit_For` / `visit_While` | `1044-1127` / `1288-1422` / `1204-1269` |
| `with` | 构造 CM 并调 `__enter__`/`__exit__` | `1128-1148` |
| 调用 / 常量 / 名字 | `visit_Call` / `constexpr(...)` / 作用域查找 | `1541-1562` / `1564-1565` / `818-821` |
| 二元运算 / `assert` | 查表调 dunder / `device_assert` | `882-909` / `1436-1439` |
| 其他 | `generic_visit` 直接报错 | `1697-1698` |

最后一行是硬墙：`generic_visit` 只有一句 `raise self._unsupported(node, "unsupported AST node type: {}".format(type(node).__name__))`（`code_generator.py:1697-1698`）。`break`、`continue`、`class`、`lambda`、`try`、`global`、`yield` 都没有 `visit_*`，都会走到这里。

所有访问都经过 `CodeGenerator.visit`（`1664-1695`）：设置 source location、调 `super().visit(node)`、把非 `CompilationError` 的异常包装成带源码片段的 `CompilationError`（`1670-1686`）。这解释了为什么内核里的 `TypeError` 最终显示成“某行源码附近的编译错误”。

### `tl.constexpr` 在哪一层被识别

**AST 层不识别 `tl.constexpr`。** 在 AST 里注解只是普通表达式：`BLOCK_SIZE: tl.constexpr` 是 `arg(annotation=Attribute(value=Name('tl'), attr='constexpr'))`；写成字符串则是 `annotation=Constant('tl.constexpr')`。两者 AST 完全不同，Triton 却都能处理。

原因是 Triton 不看注解节点，而是看 `inspect.signature` 的注解对象再做**字符串归一化**：

```python
@cached_property
def annotation(self) -> str:
    if not self._param.annotation or self._param.annotation == inspect.Parameter.empty:
        return ""
    return _normalize_ty(self._param.annotation)

@cached_property
def is_constexpr(self):
    return "constexpr" in self.annotation
```

源码依据：`jit.py:320-324`、`jit.py:337-339`。`_normalize_ty`（`280-303`）对 `str` 去掉 `tl.` 前缀，对 `type` 取 `__name__`，所以 `"tl.constexpr"` 和 `tl.constexpr` 都归一成 `"constexpr"`。判定是**子串匹配**，不是 AST 求值，也不是 `isinstance`。

局部变量不同：`visit_AnnAssign` 先 `self.visit(node.annotation)` 求值成对象，再与 `constexpr` 类做 `==`（`722-730`）。裸注解 `x: tl.constexpr` 则先存入 `pending_annotations`，赋值时才生效（`725-728`、`783-786`）。

### 一个形参的三种身份

constexpr 参数（注解含 `constexpr`）不进入 IR 签名，值直接烘进 IR；普通标量/指针默认进入；`do_not_specialize` 标量也进入，但不带对齐属性。源码依据：`jit.py:417-436` 生成 binder 时，constexpr 参数写成 `("constexpr", name)`，其他走 `specialize_impl(...)`。这是 AST 边界与特化边界的接缝，Ch 4 展开。

## 底层机制

### 入口：`ast_to_ttir`

`ast_to_ttir` 是 `ASTSource.make_ir` 的唯一实现（`compiler.py:78-81`），做四步（`code_generator.py:1745-1789`）：

```python
def ast_to_ttir(fn, src, context, options, codegen_fns, module_map, module=None):
    arg_types = [None] * len(fn.arg_names)
    for k, v in src.signature.items():
        arg_types[fn.arg_names.index(k)] = str_to_ty(v, None)
    # ... 用 src.constants 覆盖 constexpr 位置的类型 ...
    generator = CodeGenerator(..., jit_fn=fn, is_kernel=True, ...)
    generator.visit(fn.parse())
    module = generator.module
    module.context = context
    assert module.verify()
    return module
```

关键点：

- **签名来自 `src.signature`，不是 AST**。它是 `_pack_args` 根据特化结果构造的 `{参数名: 类型字符串}`（`jit.py:724-726`）。
- **constexpr 参数在进入 `CodeGenerator` 前就替换成 `constexpr_type`**（`1756-1769`），所以函数体看到的是编译期常量。
- **返回前执行 `module.verify()`**（`1783-1787`）：前端产物必须通过 TTIR verifier。

### 两套作用域

`CodeGenerator.__init__`（`296-364`）建立 `gscope`（全局/闭包可见名字，来自 `fn.get_capture_scope()`）、`lscope`（当前局部作用域）、`local_defs`（SSA 构造用）、`pending_annotations`（裸注解），以及 `self.semantic = TritonSemantic(self.builder)`。

`gscope` 不是简单拷贝：模块对象按 `module_map` 替换（如 libdevice 换成 CUDA 实现），函数对象按 `__module__` 重映射（`326-340`）。这解释了 kernel 里 `import` 的模块名如何对应到后端实现。

### 全局变量只允许 constexpr

`_define_name_lookup`（`390-427`）顺序是局部、全局、builtin，但全局查找有白名单（节选）：

```python
if any([val is absent, name in self.builtin_namespace, type(val) is ModuleType,
        isinstance(val, JITCallable), self._is_constexpr_global(name),
        knobs.compilation.allow_non_constexpr_globals]):
    return val
raise NameError("Cannot access global variable ...")
```

源码依据：`404-427`。所以 `GLOBAL = 42` 在 kernel 里**不能**直接用，必须 `GLOBAL = tl.constexpr(42)`，或设 `TRITON_ALLOW_NON_CONSTEXPR_GLOBALS=1`（不保证长期支持）。

### 常量、名字、调用

```python
def visit_Constant(self, node):
    return constexpr(node.value)

def visit_Name(self, node):
    if type(node.ctx) is ast.Store:
        return node.id
    return self.dereference_name(node.id)
```

源码依据：`1564-1565`、`818-821`。所有 Python 字面量先包成 `tl.constexpr`，进入语义层后才物化成 `arith.constant` 或 splat 成 tensor。

`visit_Call`（`1541-1562`）先看“静态实现函数”表（`1735-1742`）：`tl.static_assert`、`tl.static_print`、`int`、`len` 在 **Python 侧直接执行**，参数必须能 `_unwrap_if_constexpr` 成普通值。所以 `tl.static_assert(BLOCK % 16 == 0)` 是编译期断言，设备端断言要用 `tl.device_assert`。

### `if` 的三条路径

`visit_If`（`1044-1073`；为聚焦主路径，省略了 block 条件的 `_unsplat` 警告分支和 constexpr 类型检查——非 `bool`/`int`/`None` 会抛 `UnsupportedLanguageConstruct`）：

```python
cond = self.visit(node.test)
if _is_triton_tensor(cond):
    if _is_non_scalar_tensor(cond):
        raise self._unsupported(node, "Boolean value of Tensor with more than one value is ambiguous")
    cond = cond.to(language.int1, _semantic=self.semantic)
    if ContainsReturnChecker(self.gscope).visit(node):
        if self.scf_stack:
            raise self._unsupported(node, "Cannot have `return` statements inside `while` or `for` statements in triton.")
        self.visit_if_top_level(cond, node)
    else:
        self.visit_if_scf(cond, node)
else:
    # constexpr：只 visit 选中分支
    self.visit_compound_statement(node.body if _unwrap_if_constexpr(cond) else node.orelse)
```

三条路径：

1. **constexpr 条件**：只 visit 选中分支，另一支完全不生成 IR（AST 层死代码消除）。
2. **tensor 条件 + 分支含 `return`**：CFG（`cond_br` + 两基本块 + 汇合块），见 `visit_if_top_level`（`970-1006`）。
3. **tensor 条件 + 无 `return`**：`scf.if`，用 `yield` 回传被修改变量，见 `visit_if_scf`（`1009-1042`）。

第 2 条还有限制：`if` 在 `for`/`while` 里（`scf_stack` 非空）且含 `return` 时直接报错（`1056-1059`），因为 SCF 区域不能跳出外层循环返回。

### 例子 1：AST 与 TTIR 对照

逐句映射到 **guided** TTIR（`BLOCK_SIZE=1024`）：

```mlir
%pid = tt.get_program_id x : i32
%c1024 = arith.constant 1024 : i32
%block_start = arith.muli %pid, %c1024 : i32
%r = tt.make_range {start = 0 : i32, end = 1024 : i32} : tensor<1024xi32>
%s = tt.splat %block_start : i32 -> tensor<1024xi32>
%offsets = arith.addi %s, %r : tensor<1024xi32>
%n = tt.splat %n_elements : i32 -> tensor<1024xi32>
%mask = arith.cmpi slt, %offsets, %n : tensor<1024xi1>
%xp = tt.splat %x_ptr : !tt.ptr<f32> -> tensor<1024x!tt.ptr<f32>>
%xa = tt.addptr %xp, %offsets : tensor<1024x!tt.ptr<f32>>, tensor<1024xi32>
%x = tt.load %xa, %mask : tensor<1024x!tt.ptr<f32>>
%yp = tt.splat %y_ptr : !tt.ptr<f32> -> tensor<1024x!tt.ptr<f32>>
%ya = tt.addptr %yp, %offsets : tensor<1024x!tt.ptr<f32>>, tensor<1024xi32>
%y = tt.load %ya, %mask : tensor<1024x!tt.ptr<f32>>
%out = arith.addf %x, %y : tensor<1024xf32>
%op = tt.splat %output_ptr : !tt.ptr<f32> -> tensor<1024x!tt.ptr<f32>>
%oa = tt.addptr %op, %offsets : tensor<1024x!tt.ptr<f32>>, tensor<1024xi32>
tt.store %oa, %out, %mask : tensor<1024x!tt.ptr<f32>>
```

| Python AST | 语义层入口 | TTIR |
|---|---|---|
| `tl.program_id` / `tl.arange` | `semantic.program_id` / `arange` | `tt.get_program_id` / `tt.make_range` |
| `pid * BLOCK_SIZE` | `tensor.__mul__` | `arith.muli` |
| `scalar + tensor` / `offsets < n` | `semantic.add` / `cmp` | `tt.splat` + `arith.addi` / `arith.cmpi` |
| `ptr + offsets` | `semantic.add`（指针分支） | `tt.splat` + `tt.addptr` |
| `tl.load` / `tl.store` | `semantic.load` / `store` | `tt.load` / `tt.store` |

这张表就是本章核心：**AST 只决定“调哪个 dunder”，TTIR 形状由语义层决定**。`+` 可能是 `arith.addi`、`arith.addf` 或 `tt.addptr`，AST 完全不知道。

### 例子 2：constexpr `if` 与 tensor `if`

```python
@triton.jit
def k(n, BLOCK: tl.constexpr):
    if BLOCK == 16:          # (A) constexpr
        y = n + 1
    else:
        y = n - 1
    if n > 0:                # (B) 运行期
        y = y * 2
    return y
```

**实测** AST 对比（Python 3.10.12）：

```text
# if#0: BLOCK == 16
Compare(left=Name(id='BLOCK', ctx=Load()), ops=[Eq()], comparators=[Constant(value=16)])
# if#1: n > 0
Compare(left=Name(id='n', ctx=Load()), ops=[Gt()], comparators=[Constant(value=0)])
```

两个节点都是 `If` + `Compare`，AST 里没有“谁是编译期”的信息。区别只在 `self.visit(node.test)` 的**返回类型**：`BLOCK == 16` 返回 `constexpr(True)`，走 constexpr 分支；`n > 0` 返回标量 `int1` tensor（非 block），走 tensor 分支。

**guided**：`BLOCK=16` 时 (A) 不产生 `scf.if`，只有一条加法：

```mlir
%c1 = arith.constant 1 : i32
%y0 = arith.addi %n, %c1 : i32
```

**guided**：(B) 的分支里没有 `return`，`ContainsReturnChecker` 返回 false，走 `visit_if_scf`；只有 `return` 出现在分支内部（提前返回）时才走 `visit_if_top_level` 的 CFG 形式：

```mlir
%y1 = scf.if %cond -> (i32) {
  %t = arith.muli %y0, %c2 : i32
  scf.yield %t : i32
} else {
  scf.yield %y0 : i32
}
```

### 例子 3：三种循环写法

```python
for i in range(0, BLOCK):          # (1) Python range
    acc = acc + i
for j in tl.range(0, BLOCK):       # (2) tl.range
    acc = acc + j
for k in tl.static_range(0, 4):    # (3) 静态展开
    acc = acc + k
```

**实测** `ast.dump(node.iter.func)`（Python 3.10.12）：

```text
Name(id='range', ctx=Load())
Attribute(value=Name(id='tl', ctx=Load()), attr='range', ctx=Load())
Attribute(value=Name(id='tl', ctx=Load()), attr='static_range', ctx=Load())
```

`visit_For` 正是比较这三个对象（`1289-1329`）：

```python
IteratorClass = self.visit(node.iter.func)
if IteratorClass == language.static_range:
    ...  # Python 层 for 循环，逐次 visit body
elif IteratorClass is language.range or IteratorClass is range:
    ...  # 取 num_stages 等属性，生成 scf.for
else:
    raise RuntimeError('Only `range` and `static_range` iterators are currently supported')
```

| 写法 | 展开时机 | IR |
|---|---|---|
| `tl.static_range(0, 4)` | Python 编译期展开 4 次 | 4 份 body，无循环 |
| `range(0, BLOCK)` | 运行期 | `scf.for`，无 `tt.num_stages` |
| `tl.range(..., num_stages=3)` | 运行期 + 属性 | `scf.for` + `tt.num_stages = 3` |

**guided**：`static_range` 展开成一串 SSA（`%c0`..`%c3` 为常量）：

```mlir
%acc1 = arith.addi %acc0, %c0 : i32
%acc2 = arith.addi %acc1, %c1 : i32
%acc3 = arith.addi %acc2, %c2 : i32
%acc4 = arith.addi %acc3, %c3 : i32
```

**guided**：`tl.range` 生成 `scf.for`，循环体最后 `scf.yield %next`，并带 `{tt.num_stages = 3 : i32}` 属性。

注意 `visit_For` 直接访问 `node.iter.func`：写 `for x in some_list:` 时 `node.iter` 是 `Name`，会抛 `AttributeError`，再被包装成 `CompilationError`（`1681-1686`）。这是“看起来像 Python，实际不是 Python”的典型。

### 函数调用不是调用，是内联

`call_JitFunction`（`1441-1479`）做五件事：`signature.bind` + `apply_defaults`；`normalize_value`；按参数类型 `mangle_fn`；若模块没有该函数就用同一个 `module` 新建 `CodeGenerator` 生成函数体；最后 `builder.call(symbol, args_val)`。

所以 **`@triton.jit` 辅助函数默认是“每个类型组合生成一份私有函数，再调用”**，不是 Python 运行时调用。`noinline=True` 只是标记不内联（`1459`、`jit.py:835`），仍是模块内调用。这也解释了为什么同一 helper 被 `int32`/`float32` 各调一次，TTIR 里会出现两个 mangled 名不同的 `tt.func private`。

## 进一步用法/重要变体

- **`with` 不是 Python 的 with**：`visit_With`（`1128-1148`）先求值所有 `context_expr` 构造 CM，再依次 `__enter__`、visit body、逆序 `__exit__(None,None,None)`；体内有 `return` 直接报错（`1144-1145`）。
- **`noinline` 限制**：`_check_fn_args`（`68-74`）规定 `noinline` 函数不能接收非标量 tensor 参数，因为 block 值跨函数边界的 ABI 没有定义。
- **`static_assert` vs `device_assert`**：前者要求编译期可求 `bool`（`1700-1720`），后者生成设备端断言（`1436-1439`）；三元表达式 `visit_IfExp`（`1075-1127`）要求两支类型完全一致（`1097-1098`），不会帮你做类型提升。

## 对比与取舍

### 编译期分支 vs 运行期分支

| 维度 | constexpr `if` | tensor `if` |
|---|---|---|
| 条件来源 | `tl.constexpr` / Python 标量 | `tl.tensor` |
| 生成 IR | 只生成选中分支 | `scf.if` 或 CFG |
| 代码体积 | 小（死分支消失） | 大（两支都在） |
| 灵活性 | 编译期固定 | 运行期可变 |

能用 constexpr 就用，因为它同时减少 IR 体积和寄存器压力；但不要为了 constexpr 把运行期数据烘进编译，否则 cache 爆炸（Ch 4）。

### `tl.static_range` vs `range`

| 维度 | `tl.static_range` | `range` / `tl.range` |
|---|---|---|
| 展开时机 | Python 编译期 | 运行期 |
| 循环变量 | `constexpr` | `tl.tensor`（标量） |
| 能否做 shape | 能 | 不能 |
| IR 体积 | 随 trip count 线性增长 | 固定 |

`static_range.__iter__` 在 Python 里直接 `raise RuntimeError`（`core.py:3645-3649`），所以它只能在 `visit_For` 里被特殊处理。

## 踩雷集锦

### 1. 在 kernel 里定义嵌套函数

```python
@triton.jit
def k(x):
    def helper(y):      # 报错
        return y + 1
    return helper(x)
```

报错：`nested function definitions are not allowed inside a @triton.jit kernel`（`653-657`）。

**纠正**：把 helper 移到模块级并加 `@triton.jit`，它会按 `call_JitFunction` 被内联。

### 2. 用 `break` / `continue`

```python
for i in range(0, n):
    if i == 4:
        break           # UnsupportedLanguageConstruct
```

没有 `visit_Break`/`visit_Continue`，落到 `generic_visit`（`1697-1698`）。

**纠正**：用 mask/条件赋值改写，或改成固定 trip count 的 `tl.static_range` 加 `if`。不要指望 Triton 帮你重构控制流。

### 3. 用 `x[i] = v` 写内存

```python
x[0] = 1.0              # NotImplementedError
```

`visit_Subscript_Store` 抛 `__setitem__ is not supported in triton`（`1279-1280`）。

**纠正**：用 `tl.store(ptr + i, v)`。Triton 的 tensor 是不可变值，不是可变容器。

### 4. 对 block tensor 做 `if`

```python
v = tl.load(x + tl.arange(0, BLOCK))
if v:                   # Boolean value of Tensor with more than one value is ambiguous
    ...
```

源码依据：`1047-1049`。

**纠正**：先归约成标量（`tl.sum`/`tl.max`）再判断，或用 `tl.where`/mask 做逐元素选择。

## 进阶

- 读 `ContainsReturnChecker`（`150-233`）：它决定 `if` 走 CFG 还是 SCF；`visit_Attribute` 只对 `Name` 做函数解析（`177-190`），用于区分 `tl.load(x).to(y)` 这类链式调用。
- 读 `_find_carries`（`485-524`）：先“空跑”一遍循环体找出被修改的变量，再构造 `scf.for` 的 `iter_args`。这是 Triton 版 SSA 构造的核心。

## 动手练习

1. **预测 IR**：不运行代码，写出下面 kernel 的 TTIR 会出现哪些 op，并说明每个 `+` 走了哪条语义路径：

   ```python
   @triton.jit
   def k(p, n, BLOCK: tl.constexpr):
       offs = tl.arange(0, BLOCK)
       v = tl.load(p + offs, mask=offs < n, other=0.0)
       tl.store(p + offs, v * 2.0 + 1.0, mask=offs < n)
   ```

2. **验证边界**：把 `break`、`continue`、`lambda`、`class`、`try` 分别写进 kernel，用 `ast.parse` 确认它们都是合法 Python，再按 `visit_*` 表预测报错信息。

3. **对照 AST**：用系统 Python 跑 `ast.dump(ast.parse(inspect.getsource(fn)))`，找出 `BLOCK_SIZE: tl.constexpr` 的节点；改成字符串注解后比较 AST 差异，并解释为什么 Triton 都能处理。

4. **改造分支**：把例子 2 的 `if n > 0` 改成 `if BLOCK == 16`，预测 IR 变化；再把 `return y` 移进 `for`，预测报错位置。

## 本章重点

- Triton 没有 parser；输入是 CPython 生成的 AST，Triton 只提供 `ast.NodeVisitor`。
- `inspect.getsource` 含装饰器，`JITCallable` 会 dedent 并切掉装饰器；`parse()` 断言恰好一个顶层 `FunctionDef`。
- 语言边界由 `visit_*` 方法表决定；没有方法的节点落到 `generic_visit` 报错。
- `tl.constexpr` 的识别不在 AST 层，而在 `KernelParam.annotation` 的字符串归一化 + 子串匹配。
- `if` 有 constexpr、SCF、CFG 三条路径；constexpr 只生成选中一侧。
- `for` 只接受 `range`/`tl.range`/`tl.static_range`；`static_range` 在 Python 层展开，另外两个生成 `scf.for`。
- `@triton.jit` 函数调用默认是“按类型 mangle + 模块内内联”。

## 自我检核

- 你能说出 `JITFunction.src` 与 `inspect.getsource` 的两个差异吗？
- 你能解释为什么 `BLOCK: "tl.constexpr"` 和 `BLOCK: tl.constexpr` 都能工作吗？
- 你能画出 `if` 的三条代码生成路径及各自触发条件吗？
- 你能解释 `tl.static_range` 为什么不能在普通 Python 函数里迭代吗？

## 延伸阅读

1. **`python/triton/runtime/jit.py:481-596`**
   - 读哪里：`JITCallable.__init__`、`cache_key`、`parse`、`src` property。
   - 学什么：源码文本如何变成 AST，为什么源码是 cache key 的一部分；这是“CPython 结束、Triton 开始”的精确切点。

2. **`python/triton/compiler/code_generator.py:294-427`**
   - 读哪里/学什么：`CodeGenerator.__init__`、`builtin_namespace`、`_define_name_lookup`；两套作用域、builtin 白名单、全局变量限制；这是本章“语言边界”在作用域层面的实现。

3. **`python/triton/compiler/code_generator.py:970-1127`**
   - 读哪里/学什么：`visit_if_top_level`、`visit_if_scf`、`visit_If`、`visit_IfExp`；CFG 与 SCF 两种控制流构造，`yield` 如何回传变量；对应本章“`if` 的三条路径”。

## 下一章

下一章把本章的 AST/TTIR 片段放进完整流水线：一颗 vector-add kernel 如何从 `kernel[grid](...)` 走到 `ttir -> ttgir -> llir -> ptx -> cubin`，每层产物长什么样、metadata 里有什么、`kernel.asm["sass"]` 从哪里来。

→ [Ch 3 第一颗 kernel 的完整旅程](./03-first-kernel-end-to-end.md)
