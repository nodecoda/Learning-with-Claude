# 练习 A — 前端追踪器：把一颗 kernel 从 Python AST 追到 TTIR

> **目标**：不靠“看源码猜”，而是用 AST dump、Triton 的 TTIR dump 和一张映射表，逐节点解释一颗 kernel 如何变成 SSA IR。完成后你应该能对任意一颗中等复杂度的 kernel 说出：这一行 Python 在 TTIR 里变成了什么，为什么。

> **环境**：Triton 3.9.0（commit `a972119e92`），Python 3.10.12，Linux x86_64。需要能 import `triton` 并能编译一颗最小 kernel；如果源码构建还没跑通，先用 `guided` 方式完成 AST 部分，TTIR 部分等构建完成后再补。

## 背景与动机

传统编译器前端通常分成 lexer → parser → AST → semantic analysis → IR。Triton 看起来“没有 lexer/parser”，不是因为它跳过了这些阶段，而是因为它把 **Python 的 lexer/parser 借走了**：`@triton.jit` 函数先由 CPython 编译成 `ast`，Triton 的 `CodeGenerator` 再遍历这棵树，调用 `TritonSemantic` 发射 MLIR op。

这带来一个非常实际的调试问题：当 TTIR 长得很怪时，你很难判断是

1. Python 层面的 AST 就和你想的不一样（比如 `range` 的边界、`if` 的常量折叠）；
2. `CodeGenerator` 把某个 AST 节点翻译错了；
3. `TritonSemantic` 的类型提升/广播选错了；
4. 后面的 pass 把 IR 改成了另一个样子。

这个练习就是把这条链拆开，逐层留证据。

## 任务规格

给定下面这颗 kernel（不要改它的语义）：

```python
import triton
import triton.language as tl


@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask)
    y = tl.load(y_ptr + offs, mask=mask)
    acc = x + y
    if pid == 0:
        acc = acc * 2.0
    tl.store(out_ptr + offs, acc, mask=mask)
```

你要交付三样东西：

| 交付物 | 格式 | 验收标准 |
|---|---|---|
| AST 证据 | `ast.dump(..., indent=2)` 的片段 + 注释 | 能指出 `FunctionDef`、`Assign`、`If`、`Call`、`BinOp`、`Compare` 分别对应哪些源码行 |
| TTIR 证据 | `compiled.asm["ttir"]` 或 dump 文件 | 能指出 `tt.load`、`tt.store`、`arith.addf`、`scf.if`（或等价控制流）出现的位置 |
| 映射表 | Markdown 表格 | 每一行 Python 构造都能对应到一个 TTIR 概念，并说明为什么 |

限制：

- 不许把 `if pid == 0` 改写成 `tl.where` 来“让 IR 好看”；这个练习要观察真实控制流。
- 不许只看最终 PTX；必须看 TTIR。
- 如果你在 T4 上跑，`BLOCK` 用 128 或 256，`n` 用一个不被 `BLOCK` 整除的值（例如 1000），这样 mask 不会被常量折叠掉。

## 期望输出示例

AST 片段应该类似下面这样（这是用 Python 3.10 的 `ast` 模块真实 dump 出来的节选，不是伪代码）：

```text
FunctionDef(
  name='add_kernel',
  args=arguments(
    args=[
      arg(arg='x_ptr'),
      arg(arg='y_ptr'),
      arg(arg='out_ptr'),
      arg(arg='n'),
      arg(arg='BLOCK',
          annotation=Attribute(value=Name(id='tl'), attr='constexpr'))]),
  body=[
    Assign(targets=[Name(id='pid', ctx=Store())],
           value=Call(func=Attribute(value=Name(id='tl'), attr='program_id'),
                      args=[Constant(value=0)])),
    Assign(targets=[Name(id='offs', ctx=Store())],
           value=BinOp(left=BinOp(left=Name(id='pid'), op=Mult(),
                                  right=Name(id='BLOCK')),
                       op=Add(),
                       right=Call(func=Attribute(value=Name(id='tl'), attr='arange'),
                                  args=[Constant(value=0), Name(id='BLOCK')]))),
    ...
    If(test=Compare(left=Name(id='pid'), ops=[Eq()],
                    comparators=[Constant(value=0)]),
       body=[Assign(targets=[Name(id='acc')],
                    value=BinOp(left=Name(id='acc'), op=Mult(),
                                right=Constant(value=2.0)))],
       orelse=[]),
    Expr(value=Call(func=Attribute(value=Name(id='tl'), attr='store'),
                    args=[BinOp(left=Name(id='out_ptr'), op=Add(),
                                right=Name(id='offs')),
                          Name(id='acc')],
                    keywords=[keyword(arg='mask', value=Name(id='mask'))]))])]
```

TTIR 的**形状**应该包含这些角色（具体文本随 Triton 版本和 target 变化，不要背字符串）：

- 一个 `tt.func`，参数是 `!tt.ptr<f32>`、`i32` 和 `i32`（`BLOCK` 是 constexpr，通常不会作为运行时参数）；
- `tt.get_program_id`（或等价 op）产生 `pid`；
- `arith.muli` / `arith.addi` 计算 `offs`；
- `tt.splat` 把标量 `pid * BLOCK` 变成 block；
- `tt.make_range` 产生 `tl.arange(0, BLOCK)`；
- `tt.load` 两次，带 `mask` 和 `other`（如果没有显式 `other`，语义上要说明未命中元素的值）；
- `arith.addf` 做 `x + y`；
- `scf.if` 包住 `pid == 0` 的分支，`acc` 作为 loop/if 的 result 或通过 `scf.yield` 返回；
- `tt.store` 写回。

> 注意：如果 `if` 的条件被特化成常量，`scf.if` 可能被 canonicalize 掉；这正是你要观察的“前端/优化边界”。

## 如果你卡住了

1. **先不要碰 Triton**：用纯 Python 的 `ast.parse` + `ast.dump` 看源码树。先确认 `if`、`for`、`Call` 的 AST 形状。
2. **找不到 TTIR**：先确认 `compiled = add_kernel.warmup(..., grid=(1,))` 返回的是 `CompiledKernel`，再试 `compiled.asm["ttir"]`；如果 `asm` 里没有 `ttir`，用 `TRITON_KERNEL_DUMP=1` 和 `TRITON_DUMP_DIR` 把每个 stage 落到文件。
3. **`BLOCK` 没被特化**：检查它是否标了 `tl.constexpr`；constexpr 参数在 `ASTSource` 里会进 `constants`，不会进运行时 signature。
4. **`if` 变成了 `select`**：检查 `pid` 是否被常量传播成常量；换一个运行时才知道的条件，例如 `if n > 1000:`。
5. **mask 消失了**：`n=1024` 且 `BLOCK=1024` 时，`offs < n` 可能被证明恒真；换 `n=1000`。

## 实作步骤建议

### Step 1：固定一颗 kernel 和输入

把 kernel 放到 `labs/practice_a.py`，用 `torch` 或 Triton 的 `testing` 工具准备 `x`、`y`、`out`。如果暂时没有 GPU，先用 `TRITON_INTERPRET=1` 走解释器，把 AST/TTIR 的静态部分做完。

### Step 2：打印 AST

```python
import ast
import inspect
import textwrap

src = textwrap.dedent(inspect.getsource(add_kernel))
# inspect.getsource 会带 @triton.jit 装饰器；ast.parse 能直接吃
tree = ast.parse(src)
print(ast.dump(tree, indent=2, include_attributes=False))
```

把 `FunctionDef`、`If`、`Call`、`BinOp` 的节点形状抄到笔记里。注意：`@triton.jit` 是 `decorator_list` 里的 `Attribute`，不是函数体的一部分。

### Step 3：拿 TTIR

```python
compiled = add_kernel.warmup(
    x, y, out, n,
    BLOCK=128,
    grid=(1,),
)
print(compiled.asm["ttir"])
```

如果 `warmup` 的参数签名在你的版本里不同，用 `add_kernel[(1,)](x, y, out, n, BLOCK=128)` 先跑一次，再读 `add_kernel.cache` 或 dump 目录。

### Step 4：做映射表

至少覆盖下面这些行：

| Python 构造 | AST 节点 | TTIR/MLIR 角色 | 备注 |
|---|---|---|---|
| `pid = tl.program_id(0)` | `Assign(Call(Attribute(...)))` | `tt.get_program_id` | 标量，不是 block |
| `pid * BLOCK` | `BinOp(Mult)` | `arith.muli` | `BLOCK` 是 constexpr，先变成常量 |
| `tl.arange(0, BLOCK)` | `Call` | `tt.make_range` | 结果是 block |
| `pid * BLOCK + arange` | `BinOp(Add)` | `tt.splat` + `arith.addi` | 标量 broadcast 成 block |
| `offs < n` | `Compare(Lt)` | `arith.cmpi` | 产生 `i1` block |
| `tl.load(..., mask=...)` | `Call` | `tt.load` | mask 是 block；`other` 决定未命中值 |
| `x + y` | `BinOp(Add)` | `arith.addf` | 类型提升后决定 f32/f16 |
| `if pid == 0:` | `If` | `scf.if` | 动态条件；结果通过 `scf.yield` |
| `tl.store(...)` | `Expr(Call)` | `tt.store` | 无结果值 |

### Step 5：故意弄坏一个地方

把 `mask = offs < n` 改成 `mask = offs < n + 1`，重新 dump TTIR。观察：

- `arith.addi` 是否多了一个常量；
- `arith.cmpi` 的 predicate 是否变化；
- 如果 `n` 是 constexpr，是否发生常量折叠。

再试 `if pid == 0` 改成 `if BLOCK == 128`。如果 `BLOCK` 是 constexpr，这个 `if` 应该在前端就被折叠，TTIR 里可能根本没有 `scf.if`。这就是“AST 层控制流”和“IR 层控制流”的分界。

## 完整参考解答

**写完之后再看。** 下面的脚本把 AST、TTIR 和映射表串起来；它假设你已经能 import Triton 并有一颗可用的 GPU。

<details>
<summary>点开参考实现</summary>

```python
# labs/practice_a.py
import ast
import inspect
import textwrap

import torch
import triton
import triton.language as tl


@triton.jit
def add_kernel(x_ptr, y_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    x = tl.load(x_ptr + offs, mask=mask)
    y = tl.load(y_ptr + offs, mask=mask)
    acc = x + y
    if pid == 0:
        acc = acc * 2.0
    tl.store(out_ptr + offs, acc, mask=mask)


def dump_ast(fn):
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    print("=" * 80)
    print("PYTHON AST")
    print("=" * 80)
    print(ast.dump(tree, indent=2, include_attributes=False))


def dump_ttir(fn, x, y, out, n, block=128):
    compiled = fn.warmup(x, y, out, n, BLOCK=block, grid=(1,))
    print("=" * 80)
    print("TTIR")
    print("=" * 80)
    print(compiled.asm["ttir"])
    return compiled


def main():
    n = 1000
    block = 128
    x = torch.randn(n, device="cuda", dtype=torch.float32)
    y = torch.randn(n, device="cuda", dtype=torch.float32)
    out = torch.empty_like(x)

    dump_ast(add_kernel)
    compiled = dump_ttir(add_kernel, x, y, out, n, block)

    # 真正跑一次，确认语义
    grid = (triton.cdiv(n, block),)
    add_kernel[grid](x, y, out, n, BLOCK=block)
    torch.cuda.synchronize()
    ref = x + y
    ref[:block] *= 2.0  # 对应 pid == 0 的整个 block
    print("max abs err:", (out - ref).abs().max().item())
    print("cache dir:", compiled.metadata_group)


if __name__ == "__main__":
    main()
```

**解答说明**：

- `inspect.getsource` 拿到的是带装饰器的源码；`ast.parse` 不执行它，所以不会触发 Triton 的 JIT。
- `warmup` 只编译不启动（`warmup=True`），适合拿 IR；真正跑用 `kernel[grid](...)`。
- `ref[:block] *= 2.0` 对应 `pid == 0` 的整个 block。一个常见错误是只改 `ref[0]`；因为 `BLOCK=128`，`pid == 0` 覆盖 `offs[0:128]`，只改一个元素会让数值校验失败。这个坑故意写在“踩雷”里，不要照抄错误版本。
- 如果 `compiled.asm` 里没有 `ttir`，用 `TRITON_KERNEL_DUMP=1 TRITON_DUMP_DIR=/tmp/triton-dump` 重跑，然后在目录里找 `*.ttir`。

</details>

## 测试用例

| 输入 | 预期输出 | 说明 |
|---|---|---|
| `n=1000, BLOCK=128` | 数值与 PyTorch 参考一致 | 覆盖 mask 边界（最后一个 block 不满） |
| `n=1024, BLOCK=1024` | 数值一致；mask 仍在 | `pid` 是运行时值，`offs < n` 不会因为 n 是 2 的幂而消失（**实测**：TTIR 里仍有 `arith.cmpi`） |
| `n=1, BLOCK=128` | 只写第一个元素，其余不写 | 边界最小输入 |
| `n=0` | 不启动或空 grid | 检查 grid 计算与 kernel 语义 |
| `BLOCK=128, num_warps=8` | TTIR 不变，TTGIR layout 变 | 说明前端与 layout 的分层 |

## 常见错误对照（实测）

下面四条是跑这个练习时最容易出现的“以为自己看错了”，每条都给出 T4 上的真实 TTIR 差异和判断方法。

### 错误 1：把 `if pid == 0` 当成“前端会折叠的常量分支”

`pid` 是 `tt.get_program_id`，运行时才知道。**实测**（`n=1000, BLOCK=128`）TTIR 里有 1 条 `scf.if`、2 条 `arith.cmpi`（一条来自 `mask`，一条来自 `pid == 0`）：

```mlir
%pid = tt.get_program_id x : i32
%mask_4 = arith.cmpi slt, %offs_3, %mask : tensor<128xi32>
%0 = arith.cmpi eq, %pid, %c0_i32 : i32
%1 = scf.if %0 -> (tensor<128xf32>) {
  %acc_9 = arith.mulf %acc, %cst : tensor<128xf32>
  scf.yield %acc_9 : tensor<128xf32>
} else {
  scf.yield %acc : tensor<128xf32>
}
```

（`loc(...)` 已省略；SSA 编号会随版本和源码行号变化，形状和 op 名是稳定的。）

判断方法：数 `scf.if` 的个数，而不是看 Python 里有没有 `if`。

### 错误 2：把条件换成 constexpr，却以为“控制流丢了”

把 `if pid == 0` 改成 `if BLOCK == 128`（`BLOCK` 是 `tl.constexpr`），**实测**同一颗 kernel 的 TTIR 里 `scf.if` 变成 0 条、`arith.cmpi` 只剩 1 条，`arith.mulf` 变成无条件执行。这不是 bug，而是前端在 `CodeGenerator` 里对 constexpr 条件做了静态求值（`python/triton/compiler/code_generator.py:1044` 的 `visit_If` 会先 `visit_compound_statement`/求值条件，再决定发射 `scf.if` 还是只走一个分支）。

判断方法：如果 `if` 的条件只由 `tl.constexpr` 和字面量组成，先假设它会被折叠；要看真实控制流，就把条件换成依赖指针/`program_id`/`tl.load` 结果的表达式。

### 错误 3：期望 `n=1024, BLOCK=1024` 时 mask 自动消失

**实测**：即使 `n` 正好等于 `BLOCK` 且 grid 只有 1 个 block，TTIR 里仍然保留 `arith.cmpi`。原因是 `offs = pid * BLOCK + arange(0, BLOCK)` 里的 `pid` 是运行时值，编译器不能假设 `pid == 0`；只有把 `n` 也写成 `tl.constexpr`，比较才会退化成“常量 vs 常量”的张量常量（**实测**：`arith.constant dense<1024>` 出现，但 `pid` 项仍在）。

判断方法：看 `tt.get_program_id` 是否还在；只要它在，任何含 `offs` 的谓词都不该被完全折叠。

### 错误 4：在 notebook / REPL 里用 `inspect.getsource`

`JITFunction.__init__` 在**装饰时**就调用 `inspect.getsourcelines(fn)`（`python/triton/runtime/jit.py:478-480`）。在 notebook 单元格、`exec("...")`、stdin/`python -c` 或 `python - <<EOF` 里，源码不可定位，装饰器直接失败（**实测**）：

```text
OSError: could not get source code
...
ValueError: @jit functions should be defined in a Python file
```

注意它发生在 `@triton.jit` 这一行，而不是编译时；所以你连 AST 都拿不到。这时不要把 kernel 改成字符串再 `ast.parse` 就以为等价——你测的就不再是 `@triton.jit` 实际拿到的那棵树了。

判断方法：把 kernel 放进 `.py` 文件再跑；在 notebook 里先把源码写进临时 `.py`，再 `import` 那个模块。

## 延伸挑战（加分）

- 把 `if pid == 0` 改成 `if n > 1000`，观察 `scf.if` 是否还在；如果 `n` 是 constexpr，会发生什么？
- 在 kernel 里加一个 `for i in range(0, 4)` 的循环，观察 `scf.for` 的 `iter_args` 和 `scf.yield`；再改成 `tl.static_range`，对比 TTIR 是否被展开。
- 把 `acc = x + y` 改成 `acc = x.to(tl.float16) + y`，观察 `arith.extf`/`arith.truncf` 或 `tt.fp_to_fp` 出现的位置，并解释类型提升规则。
- 用 `TRITON_INTERPRET=1` 跑同一颗 kernel，比较解释器路径和编译路径的差异；解释器不是“另一个 Triton”，它只是用 NumPy 模拟 block 语义。

## 延伸阅读

1. **`python/triton/compiler/code_generator.py:789-900`（`visit_Assign`、`visit_BinOp`）**
   - 读哪里：赋值如何创建 SSA 值、`BinOp` 如何先访问左右子树再调用语义层。
   - 学什么：Python AST 的“表达式”怎样变成 MLIR 的 `Value`；为什么 Triton 的赋值不是“变量”，而是名字到 `Value` 的映射。
   - 与本章关系：映射表里 `Assign`/`BinOp` 两行的直接源码。

2. **`python/triton/compiler/code_generator.py:1044-1074`（`visit_If`）、`:1204-1287`（`visit_While`）、`:1288-1400`（`visit_For`）**
   - 读哪里：条件求值、`scf.if` 的 result/yield 构造、循环的 `iter_args`。
   - 学什么：为什么 `if` 的两个分支必须产生同类型、同形状的 yield；循环携带值如何变成 `scf.for` 的 `iter_args`。
   - 与本章关系：解释“错误 1/错误 2”的源码；也是 Ch 5 的主线。

3. **`python/triton/compiler/code_generator.py:1541-1700`（`visit_Call`）与 `python/triton/language/semantic.py`**
   - 读哪里：`visit_Call` 如何区分内建函数、`@triton.jit` 函数和普通 Python 函数；`semantic.py` 里 `load`/`store`/`arange` 的实现。
   - 学什么：`tl.load` 不是一个“函数调用”，而是“查表 + 发射 op”；mask/other 的参数检查发生在语义层。
   - 与本章关系：映射表里 `tl.load`/`tl.store`/`tl.arange` 三行。

4. **`python/triton/compiler/compiler.py:52-116`（`ASTSource`）**
   - 读哪里：`ASTSource` 如何保存 `fn`、`signature`、`constants`、`attrs`，以及 `make_ir` 如何调用 `CodeGenerator`。
   - 学什么：constexpr 参数为什么进 `constants` 而不是运行时 signature；cache key 里到底有哪些字段。
   - 与本章关系：Step 1/Step 3 里 `warmup` 与 `BLOCK=128` 的行为依据；也是 Ch 4 的主线。

## 自我检核

- [ ] 能不看笔记说出 `@triton.jit` 函数从 Python 源码到 TTIR 经过哪三个对象（`JITFunction`、`ASTSource`、`CodeGenerator`/`TritonSemantic`）
- [ ] 能解释为什么 Triton 不需要自己写 lexer/parser，以及这带来的限制
- [ ] 能指出 `if`、`for`、`tl.load`、`tl.store` 在 TTIR 中的对应物
- [ ] 能解释 constexpr 参数为什么不进运行时 signature
- [ ] 能说出至少两个“前端看起来一样、TTIR 不一样”的例子

→ [Ch 10 — TTIR 方言：类型、op、属性与验证器](./10-ttir-dialect-and-verifiers.md)
