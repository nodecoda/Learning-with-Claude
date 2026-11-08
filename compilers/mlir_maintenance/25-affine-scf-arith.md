# Ch 25 — Affine / SCF / Arith：可分析性与通用性的取舍

> **目标**：能判断一段控制流“应该写成 `affine.for` 还是 `scf.for`”，说清 affine 多出来的分析能力从哪来、代价是什么；能在 `--lower-affine` 之后跟踪信息损失；能从 `arith` 的 27 个 TODO 里挑出一个可上手的补丁。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`。
>
> **前置**：Ch 11（分析框架）、Ch 24（Linalg 降级默认产出 `scf.for`）。

---

## 25.1 为什么三个方言要放在一章

因为它们回答同一个问题的三个层次：

```text
arith   ── 标量运算本身（加法、比较、转换），语义最"硬"
scf     ── 通用结构化控制流（for/while/if/parallel），能表达任何东西
affine  ── 受限但可分析的循环/下标，能证明的东西最多
```

维护者在这三者之间反复横跳，原因只有一个：**可分析性是有代价的**。

- `affine.for` 的边界必须是 affine 表达式，下标必须是 affine 表达式 → 换来“能算出精确 trip count、能做 dependence analysis、能证明 fusion 合法”。
- `scf.for` 什么都能写 → 但优化器只能做保守的、基于 SSA 的推理。
- `arith` 的每个 op 都有精确的 poison/UB 语义 → 前端喜欢它，但折叠规则写错就是静默的错误编译（miscompile）。

实测规模（本章写作时）：

```bash
find mlir/test/Dialect/Affine -name '*.mlir' | wc -l    # 72
find mlir/test/Dialect/SCF    -name '*.mlir' | wc -l    # 47
find mlir/test/Dialect/Arith  -name '*.mlir' | wc -l    # 31
grep -rn 'TODO\|FIXME' mlir/lib/Dialect/Affine | wc -l  # 114
grep -rn 'TODO\|FIXME' mlir/lib/Dialect/SCF    | wc -l  # 27
grep -rn 'TODO\|FIXME' mlir/lib/Dialect/Arith  | wc -l  # 27
```

Affine 的 TODO 数量是 SCF 的四倍多——它不是“成熟的旧方言”，而是**还有很多分析没做完的活跃区域**。

## 25.2 先建立直觉：两根轴——表达力 vs 可证明性

```text
   可证明性 高
        ▲
        │   affine.for / affine.if
        │   ├─ bounds: affine 表达式（常量、IV、符号常量）
        │   ├─ 下标: affine.load/store 的 index 是 affine 表达式
        │   └─ 结果: 精确 trip count、dependence distance、fusion 合法性
        │
        │        scf.for / scf.while / scf.if
        │        ├─ bounds: 任意 SSA 值
        │        ├─ 下标: memref.load/store 支持任意 index
        │        └─ 结果: 只能做 SSA/dominance 级推理
        │
        │               cf.br / 任意 CFG
        └──────────────────────────────────────────► 表达力 高
```

再具体一点，同一个 saxpy 的两种写法：

```text
affine 版：                               scf 版（lower 之后）：
affine.for %i = 0 to 16 {                 %c0 = arith.constant 0
  %x = affine.load %X[%i]                 %c16 = arith.constant 16
  %y = affine.load %Y[%i]                 %c1 = arith.constant 1
  ...                                     scf.for %i = %c0 to %c16 step %c1 {
  affine.store %s, %Y[%i]                   %x = memref.load %X[%i]
}                                           ...
                                            memref.store %s, %Y[%i]
                                          }
```

左边优化器知道：`%i` 永远在 `[0,16)`，`%X[%i]` 与 `%Y[%i]` 是仿射下标，可以算 dependence。右边只知道 `%i` 是某个 `index`，范围要靠 `scf.for` 的边界值再推一遍（`ValueBoundsAnalysis` 能做，但更弱）。

**这就是核心取舍**：affine 是“把可分析性写进 IR”，scf 是“把通用性写进 IR，让分析去猜”。

## 25.3 Affine 的三个承诺与三个限制

### 25.3.1 承诺一：循环边界可分析

```bash
cd /tmp/mlir_p5
cat > affine.mlir <<'EOF'
func.func @saxpy(%a: f32, %x: memref<16xf32>, %y: memref<16xf32>) {
  affine.for %i = 0 to 16 {
    %xi = affine.load %x[%i] : memref<16xf32>
    %yi = affine.load %y[%i] : memref<16xf32>
    %t = arith.mulf %a, %xi : f32
    %s = arith.addf %t, %yi : f32
    affine.store %s, %y[%i] : memref<16xf32>
  }
  return
}
EOF
$T/bin/mlir-opt affine.mlir --affine-loop-tile='tile-size=4'
```

真实输出：

```text
#map = affine_map<(d0) -> (d0)>
#map1 = affine_map<(d0) -> (d0 + 4)>
module {
  func.func @saxpy(%arg0: f32, %arg1: memref<16xf32>, %arg2: memref<16xf32>) {
    affine.for %arg3 = 0 to 16 step 4 {
      affine.for %arg4 = #map(%arg3) to #map1(%arg3) {
        %0 = affine.load %arg1[%arg4] : memref<16xf32>
        %1 = affine.load %arg2[%arg4] : memref<16xf32>
        %2 = arith.mulf %arg0, %0 : f32
        %3 = arith.addf %2, %1 : f32
        affine.store %3, %arg2[%arg4] : memref<16xf32>
      }
    }
    return
  }
}
```

注意内层循环的边界是 **`affine.apply` 形式的 map**（`#map(%arg3)` 到 `#map1(%arg3)`），而不是先算出 `%ub = %arg3 + 4` 再 `to %ub`。这个差别很关键：map 形式保留了“边界与 IV 的仿射关系”，后续 pass 还能继续推；如果变成普通 SSA 值，这层信息就丢了。

trip count 的计算在 `lib/Dialect/Affine/Analysis/LoopAnalysis.cpp:210`（`getConstantTripCount`），它依赖 `:164` 的 `getTripCountMapAndOperands` 把边界归一化成 `(map, operands)` 对。

### 25.3.2 承诺二：下标可做 dependence analysis

```text
两个 affine.load/store 之间是否有依赖？
        │
        ▼
  checkMemrefAccessDependence(  ← lib/Dialect/Affine/Analysis/AffineAnalysis.cpp:617
      srcAccess, dstAccess, depth)
        │
        ├─ 构造两者的下标表达式（仿射）
        ├─ 在循环域上解 Diophantine/整数线性约束
        └─ 返回 NoDependence / Dependence / 未知
```

这个能力是 `affine-loop-fusion` 的前提。实测把两个循环融合：

```bash
cat > fusion.mlir <<'EOF'
func.func @fusion(%A: memref<16xf32>, %B: memref<16xf32>, %C: memref<16xf32>) {
  affine.for %i = 0 to 16 {
    %a = affine.load %A[%i] : memref<16xf32>
    %b = affine.load %B[%i] : memref<16xf32>
    %s = arith.addf %a, %b : f32
    affine.store %s, %C[%i] : memref<16xf32>
  }
  affine.for %i = 0 to 16 {
    %c = affine.load %C[%i] : memref<16xf32>
    %m = arith.mulf %c, %c : f32
    affine.store %m, %C[%i] : memref<16xf32>
  }
  return
}
EOF
$T/bin/mlir-opt fusion.mlir --affine-loop-fusion
```

真实输出：

```text
module {
  func.func @fusion(%arg0: memref<16xf32>, %arg1: memref<16xf32>, %arg2: memref<16xf32>) {
    affine.for %arg3 = 0 to 16 {
      %0 = affine.load %arg0[%arg3] : memref<16xf32>
      %1 = affine.load %arg1[%arg3] : memref<16xf32>
      %2 = arith.addf %0, %1 : f32
      affine.store %2, %arg2[%arg3] : memref<16xf32>
      %3 = affine.load %arg2[%arg3] : memref<16xf32>
      %4 = arith.mulf %3, %3 : f32
      affine.store %4, %arg2[%arg3] : memref<16xf32>
    }
    return
  }
}
```

融合能成立，是因为 pass 证明了：第二个循环对 `C[i]` 的读，恰好被第一个循环对 `C[i]` 的写 covering（同一下标、同一 trip）。如果第二个循环写 `C[i+1]`，就要看 dependence distance——这正是 `canFuseLoops` 在 `lib/Dialect/Affine/Transforms/LoopFusion.cpp:995` 判断的东西。

### 25.3.3 承诺三：`affine.if` 的条件可分析

`affine.if` 的条件必须是 affine 约束（`affine.apply` + `cmpi` 的组合），所以能做“这个分支在这个 tile 里恒真/恒假”的推理。`scf.if` 的条件是任意 `i1`，做不到。

### 25.3.4 限制：一旦违反，verifier 立刻拦

affine 的限制不是“建议”，是**验证规则**。实测把两个 IV 相乘当下标：

```bash
cat > bad_affine.mlir <<'EOF'
func.func @bad(%x: memref<16x16xf32>) {
  affine.for %i = 0 to 16 {
    affine.for %j = 0 to 16 {
      %v = affine.load %x[%i * %j, %j] : memref<16x16xf32>
    }
  }
  return
}
EOF
$T/bin/mlir-opt bad_affine.mlir --lower-affine
```

真实输出：

```text
bad_affine.mlir:4:30: error: non-affine expression: at least one of the multiply operands has to be either a constant or symbolic
      %v = affine.load %x[%i * %j, %j] : memref<16x16xf32>
                             ^
```

这条错误信息值得背下来：**affine 乘法只允许“常量 × 符号”**。`%i * %j`（两个 IV 相乘）不是仿射函数，`%i * 4` 才是。

## 25.4 `--lower-affine`：从可分析世界降到通用世界

```bash
$T/bin/mlir-opt affine.mlir --lower-affine
```

真实输出：

```text
module {
  func.func @saxpy(%arg0: f32, %arg1: memref<16xf32>, %arg2: memref<16xf32>) {
    %c0 = arith.constant 0 : index
    %c16 = arith.constant 16 : index
    %c1 = arith.constant 1 : index
    scf.for %arg3 = %c0 to %c16 step %c1 {
      %0 = memref.load %arg1[%arg3] : memref<16xf32>
      %1 = memref.load %arg2[%arg3] : memref<16xf32>
      %2 = arith.mulf %arg0, %0 : f32
      %3 = arith.addf %2, %1 : f32
      memref.store %3, %arg2[%arg3] : memref<16xf32>
    }
    return
  }
}
```

信息损失清单（维护者必须心里有数）：

| 降级前 | 降级后 | 丢了什么 |
|---|---|---|
| `affine.for %i = 0 to 16` | `scf.for %i = %c0 to %c16 step %c1` | 边界从“字面常量”变成“SSA 值 + 常量定义” |
| `affine.load %x[%i]` | `memref.load %x[%i]` | 下标不再被声明为 affine；分析器要重新证明 |
| `affine.apply #map(%i)` | `arith.addi/muli` 链 | 仿射表达式结构变成普通算术 |
| `affine.if` | `scf.if` | 条件不再可做整数推理 |

所以 pipeline 设计原则是：**能在 affine 层做完的，别提前降**。`--lower-affine` 应该尽量靠后。

实现上要注意：这个 pass 的名字是 `--lower-affine`，**不是** `--convert-affine-to-std`（后者在当前 trunk 里不存在，早期版本才有）：

```bash
$T/bin/mlir-opt --help 2>&1 | grep -E '^      --.*affine'
```

真实输出（节选）：

```text
      --affine-data-copy-generate                            -   Generate explicit copying for affine memory operations
      --affine-expand-index-ops                              -   Lower affine operations operating on indices into more fundamental operations
      --affine-loop-coalescing                               -   Coalesce nested loops with independent bounds into a single loop
      --affine-loop-fusion                                   -   Fuse affine loop nests
      --affine-loop-invariant-code-motion                    -   Hoist loop invariant instructions outside of affine loops
      --affine-loop-normalize                                -   Apply normalization transformations to affine loop-like ops
      --affine-loop-tile                                     -   Tile affine loop nests
      --affine-loop-unroll                                   -   Unroll affine loops
      --lower-affine                                         -   Lower Affine operations to a combination of Arith and SCF operations
      --raise-scf-to-affine                                  -   Raise SCF operations to affine operations (best effort)
```

注意最后一行：`--raise-scf-to-affine` 是反向的、“best effort”的。它的存在说明一件事——**从 scf 回到 affine 是有损且不保证成功的**，所以不要指望“先降再升”能把分析能力找回来。

## 25.5 pass 的 op anchor：为什么 `--affine-loop-unroll` 直接写会失败

这是新手最常撞的墙：

```bash
$T/bin/mlir-opt affine.mlir --affine-loop-unroll='unroll-factor=4'
```

真实输出：

```text
<unknown>:0: error: unable to schedule pass 'AffineLoopUnroll' on a PassManager intended to run on 'builtin.module'!
```

原因：这个 pass 是 `OperationPass<func::FuncOp>`，只能挂在 `func.func` 上；而 `mlir-opt` 顶层是 `builtin.module`。正确写法是显式给 pipeline：

```bash
$T/bin/mlir-opt affine.mlir --pass-pipeline='builtin.module(func.func(affine-loop-unroll{unroll-factor=4}))'
```

真实输出（节选）：

```text
#map = affine_map<(d0) -> (d0 + 1)>
#map1 = affine_map<(d0) -> (d0 + 2)>
#map2 = affine_map<(d0) -> (d0 + 3)>
module {
  func.func @saxpy(%arg0: f32, %arg1: memref<16xf32>, %arg2: memref<16xf32>) {
    affine.for %arg3 = 0 to 16 step 4 {
      %0 = affine.load %arg1[%arg3] : memref<16xf32>
      ...
      %4 = affine.apply #map(%arg3)
      %5 = affine.load %arg1[%4] : memref<16xf32>
```

三件事同时被演示了：unroll 把 step 改成 4、用 `affine.apply` 表示 `%i+1/%i+2/%i+3`、每个副本的下标仍是 affine 的（所以后续还能分析）。

**通用规则**：看到 `unable to schedule pass ... on 'builtin.module'`，就去查 `include/mlir/Dialect/*/Transforms/Passes.td` 里的 `let dependentDialects` 和基类——是 `OperationPass<func::FuncOp>` 还是 `OperationPass<ModuleOp>`。这个错误在 Part 5 会重复出现（Ch 26 的 `acc-cg-to-gpu`、Ch 27 的 GPU pass 都有同样的坑）。

## 25.6 SCF 的契约：`iter_args` 与 `yield` 必须成对

`scf.for` 不是“带 phi 的循环”，而是**函数式的循环值传递**：

```bash
cat > scf.mlir <<'EOF'
func.func @sum(%n: index) -> f32 {
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  %init = arith.constant 0.0 : f32
  %r = scf.for %i = %c0 to %n step %c1 iter_args(%acc = %init) -> (f32) {
    %f = arith.index_cast %i : index to i32
    %ff = arith.sitofp %f : i32 to f32
    %s = arith.addf %acc, %ff : f32
    scf.yield %s : f32
  }
  return %r : f32
}
EOF
$T/bin/mlir-opt scf.mlir --convert-scf-to-cf
```

真实输出：

```text
module {
  func.func @sum(%arg0: index) -> f32 {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    %cst = arith.constant 0.000000e+00 : f32
    cf.br ^bb1(%c0, %cst : index, f32)
  ^bb1(%0: index, %1: f32):  // 2 preds: ^bb0, ^bb2
    %2 = arith.cmpi slt, %0, %arg0 : index
    cf.cond_br %2, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %3 = arith.index_cast %0 : index to i32
    %4 = arith.sitofp %3 : i32 to f32
    %5 = arith.addf %1, %4 : f32
    %6 = arith.addi %0, %c1 overflow<nsw> : index
    cf.br ^bb1(%6, %5 : index, f32)
  ^bb3:  // pred: ^bb1
    return %1 : f32
  }
}
```

`iter_args` 在这里变成了 block argument `%1`，`yield` 变成了 `cf.br` 的第二个实参。这个映射是 `scf` 结构化语义的**全部秘密**：循环携带值就是块参数，所以 SSA dominance 仍然成立，不需要 phi。

verifier 在 `lib/Dialect/SCF/IR/SCF.cpp:356`（`ForOp::verify`），它检查两件事：

```cpp
  // Check that the body block has at least the induction variable argument.
  if (getBody()->getNumArguments() < getNumInductionVars())
    return emitOpError("expected body to have at least ")
           << getNumInductionVars()
           << " argument(s) for the induction variable, but got "
           << getBody()->getNumArguments();

  // Check that the number of init args and op results is the same.
  if (getInitArgs().size() != getNumResults())
    return emitOpError(
        "mismatch in number of loop-carried values and defined values");
```

注释里那句 “before any region trait verifiers ... to avoid crashing with an out-of-bounds drop_front on an empty arg list” 是维护者留的疤：曾经有个崩溃是空 block 参数列表导致的，现在把检查提前了。**读 verifier 的注释比读 verifier 的代码更有价值**，因为注释里写着“为什么必须按这个顺序”。

`--canonicalize` 对 `scf` 的效果通常有限（实测上面的 `sum` 只有常量折叠，循环结构不变）：

```bash
$T/bin/mlir-opt scf.mlir --canonicalize
```

真实输出（节选）：

```text
    %cst = arith.constant 0.000000e+00 : f32
    %0 = scf.for %arg1 = %c0 to %arg0 step %c1 iter_args(%arg2 = %cst) -> (f32) {
```

没有循环不变量外提、没有 trip count specialization——因为 `%n` 是未知的。想让 scf 也“可分析”，要么补 `ValueBoundsOpInterface` 的实现，要么把边界变成常量。

## 25.7 Arith：语义最硬、也最容易写出 miscompile

`arith` 的每个 op 都有精确定义的 poison/UB 边界。三个真实例子。

### 25.7.1 折叠 `x * 1.0` 是安全的

```bash
cat > arith.mlir <<'EOF'
func.func @divzero(%x: i32, %y: f32) -> i32 {
  %c0 = arith.constant 0 : i32
  %d = arith.divui %x, %c0 : i32
  return %d : i32
}
func.func @fmul1(%y: f32) -> f32 {
  %one = arith.constant 1.0 : f32
  %r = arith.mulf %y, %one : f32
  return %r : f32
}
EOF
$T/bin/mlir-opt arith.mlir --canonicalize
```

真实输出：

```text
module {
  func.func @divzero(%arg0: i32, %arg1: f32) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %0 = arith.divui %arg0, %c0_i32 : i32
    return %0 : i32
  }
  func.func @fmul1(%arg0: f32) -> f32 {
    return %arg0 : f32
  }
}
```

`fmul1` 被折叠成 `return %arg0`，`divzero` **原样保留**。

### 25.7.2 为什么 `divui %x, 0` 不折叠成 poison

因为折叠成 poison 会**删除一个可能 trap 的除法**（在目标上 `div` 除以 0 常常是硬件异常），而当前语义里 `arith.divui` 对 0 是 UB 但没被建模成 poison。源码里写得很清楚（`lib/Dialect/Arith/IR/ArithOps.cpp:849`）：

```cpp
  // TODO: divui (x, 0) -> poison. Division by zero is undefined behaviour and
```

同族还有 `:901`（`divsi`）、`:957`（`ceildivui`）、`:999`（`ceildivsi`）、`:1060`（`floordivsi`）、`:1106`（`remui`）、`:1142`（`remsi`）。**七个 TODO，同一类问题**：

```bash
grep -n "TODO.*poison" mlir/lib/Dialect/Arith/IR/ArithOps.cpp
```

这就是“可上手的第一个补丁”的典型形状：规则清楚、测试好写、review 面窄。但它**不容易**——要先确认 poison 语义不会破坏现有 lowering（例如某些后端把 `div` 的 trap 行为当契约用）。Ch 31 会讲怎么评估这种补丁的影响面。

### 25.7.3 `arith.constant` 的类型约束也是 TODO

`:279` 的 `TODO: disallow arith.constant to return anything other than signless integer` 和 `:110` 的 `TODO: Handle index separately; its storage width is not its target width` 是另外两条线：前者关于“常量能不能携带 signed/unsigned 类型”，后者关于 `index` 在 32 位目标上的宽度。它们都属于“语义收紧”类改动——**改完会让一批现有 IR 变成非法**，所以必须配合 deprecation 策略。

## 25.8 选型表：什么时候用哪个

| 需求 | 选 Affine | 选 SCF |
|---|---|---|
| 循环边界是常量或符号常量的线性组合 | ✅ | 可以，但分析更弱 |
| 需要 dependence analysis / 精确 fusion | ✅ | ❌ |
| 需要 `affine.if` 的分支可证明性 | ✅ | ❌ |
| 边界来自任意 `tensor.dim` / 函数返回值 | ❌（要先用 `affine.min` 夹住） | ✅ |
| 需要 `scf.while` / `scf.parallel` / `scf.forall` | ❌ | ✅ |
| 目标是把 IR 交给上游框架（Linalg/TOSA） | 只在 affine 专用 pipeline | ✅ 默认 |
| 需要和 `scf.forall` 的 mapping 交互（GPU） | ❌ | ✅ |

维护者判断“该不该引入 affine”的经验法则：**如果这个 pass 需要证明“两个下标访问的是同一块内存”，就用 affine；如果只是要表达控制流，用 scf。**

## 25.9 源码落点

| 主题 | 文件:行 |
|---|---|
| trip count 计算 | `mlir/lib/Dialect/Affine/Analysis/LoopAnalysis.cpp:164`、`:210` |
| 依赖分析 | `mlir/lib/Dialect/Affine/Analysis/AffineAnalysis.cpp:617`（`checkMemrefAccessDependence`）、`:718`（`getDependenceComponents`） |
| loop fusion | `mlir/lib/Dialect/Affine/Transforms/LoopFusion.cpp:53`（pass）、`:995`（`canFuseLoops`） |
| affine → scf+arith | `mlir/lib/Conversion/AffineToStandard/AffineToStandard.cpp` |
| SCF 结构 verifier | `mlir/lib/Dialect/SCF/IR/SCF.cpp:356`（`ForOp::verify`）、`:102`（`verifyAndGetTerminator`） |
| scf → cf | `mlir/lib/Dialect/SCF/IR/SCF.cpp:237`（把 `scf.yield` 换成 `cf.br`） |
| arith 折叠 TODO | `mlir/lib/Dialect/Arith/IR/ArithOps.cpp:110`、`:279`、`:457`、`:691`、`:849`、`:901`、`:957`、`:999`、`:1060`、`:1106`、`:1142` |
| arith 接口 | `mlir/include/mlir/Dialect/Arith/IR/ArithOps.td`、`ArithInterfaces.td` |

## 25.10 可跑实验清单

| # | 命令 | 观察什么 |
|---|---|---|
| 1 | `mlir-opt affine.mlir --affine-loop-tile='tile-size=4'` | 内层边界是 `affine.apply` map，不是普通 SSA 值 |
| 2 | `mlir-opt fusion.mlir --affine-loop-fusion` | 两个循环合成一个，顺序保持 |
| 3 | `mlir-opt bad_affine.mlir --lower-affine` | `%i * %j` 被 verifier 拒绝的原文 |
| 4 | `mlir-opt affine.mlir --lower-affine` | affine→scf+memref，信息损失 |
| 5 | `mlir-opt affine.mlir --affine-loop-unroll='unroll-factor=4'` | op anchor 错误原文 |
| 6 | `mlir-opt affine.mlir --pass-pipeline='builtin.module(func.func(affine-loop-unroll{unroll-factor=4}))'` | 正确写法与 `affine.apply` 产物 |
| 7 | `mlir-opt scf.mlir --convert-scf-to-cf` | `iter_args` → block 参数、`yield` → `cf.br` |
| 8 | `mlir-opt arith.mlir --canonicalize` | `*1.0` 折叠、`divui x,0` 不折叠 |

## 25.11 踩雷集锦

1. **错误直觉：`-convert-affine-to-std` 是现行 pass 名。** → 正确认识：现在是 `--lower-affine`；旧名字在帮助里已经搜不到。
2. **错误直觉：affine 只是“带糖的 scf”。** → 正确认识：affine 的关键是**下标也被约束**，verifier 会拒绝非仿射表达式；这正是它能做 dependence analysis 的原因。
3. **错误直觉：`--lower-affine` 是无损的。** → 正确认识：边界、下标结构、`affine.if` 的可证明性都退化；`--raise-scf-to-affine` 只是 best effort，找不回来。
4. **错误直觉：pass 名能直接写在 `mlir-opt` 后面就一定能跑。** → 正确认识：`OperationPass<func::FuncOp>` 的 pass 需要 `builtin.module(func.func(...))` 嵌套，否则报 `unable to schedule pass`。
5. **错误直觉：`scf.for` 的 `iter_args` 是可选的语法糖。** → 正确认识：它是循环携带值的唯一载体；少一个 init arg 或 yield 数量不匹配，verifier 直接报错（`SCF.cpp:356`）。
6. **错误直觉：`--canonicalize` 会帮我把 `scf.for` 变成常量 trip。** → 正确认识：边界是未知 `%n` 时什么也不做；要么用 `affine`，要么补 `ValueBounds` 实现。
7. **错误直觉：`arith.divui %x, 0` 应该折叠成 poison，没折叠就是 bug。** → 正确认识：这是**已知 TODO**（`ArithOps.cpp:849`），七个同类 op 都没做；折叠它会改变 trap 行为，需要谨慎设计。
8. **错误直觉：`affine.for` 里的 `affine.load` 和 `memref.load` 可以混用。** → 正确认识：`affine.load` 的下标必须是 affine 表达式；混用非仿射下标会被拒。

## 25.12 本章重点整理

- 三层的分工：`arith` 定义标量语义，`scf` 提供通用结构化控制流，`affine` 用受限语法换可分析性。
- affine 的三个承诺：可算 trip count、可做 dependence analysis、`affine.if` 条件可证明；代价是下标与边界必须仿射。
- 非仿射表达式的报错是 verifier 级的硬约束（`%i * %j` 非法，`%i * 4` 合法）。
- `--lower-affine` 是有损降级；pipeline 里应尽量靠后；反向的 `--raise-scf-to-affine` 只是 best effort。
- `scf.for` 的 `iter_args`/`yield` 就是 block 参数与分支实参，SSA 语义因此保持。
- `arith` 的折叠规则直接决定 miscompile 风险；`divui x,0 → poison` 这一族还有 7 个 TODO 没关。
- op anchor（`OperationPass<func::FuncOp>`）是 Part 5 反复出现的坑，`--pass-pipeline` 是通用解法。

## 25.13 自我检核

- [ ] 能说出 affine 相比 scf 多出的三项分析能力，以及各自依赖什么语法约束
- [ ] 能解释 `%i * %j` 为什么非法、`%i * 4` 为什么合法
- [ ] 能列出 `--lower-affine` 丢掉的至少三项信息
- [ ] 能写出把 func-level pass 塞进 module pipeline 的正确语法
- [ ] 能解释 `scf.for` 的 block 参数与 `iter_args` 的对应关系
- [ ] 能说出 `scf.for` verifier 检查的两件事以及注释里提到的历史崩溃
- [ ] 能解释 `arith.mulf %x, 1.0` 能折叠而 `arith.divui %x, 0` 不能
- [ ] 能在 `ArithOps.cpp` 里定位至少三个可上手的 TODO

## 25.14 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/Affine.md`**
  - 读哪里：开头“Motivation”与 `affine.for`/`affine.if` 的语法约束部分
  - 能学到什么：官方对“为什么需要 affine 而不是直接用 scf”的论证；和本章关联：25.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Affine/Analysis/LoopAnalysis.cpp`**
  - 读哪里：`164-240`（trip count 的归一化与常量提取）
  - 能学到什么：`(map, operands)` 表示法如何支撑后续分析；和本章关联：25.3.1
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Affine/Analysis/AffineAnalysis.cpp`**
  - 读哪里：`617-760`（`checkMemrefAccessDependence` 与 components）
  - 能学到什么：依赖判定的输入输出与“未知”情形；和本章关联：25.3.2
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/SCF/IR/SCF.cpp`**
  - 读哪里：`100-135`（terminator 校验）、`356-375`（`ForOp::verify`）
  - 能学到什么：结构化控制流的 verifier 顺序为何重要；和本章关联：25.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Arith/IR/ArithOps.cpp`**
  - 读哪里：`840-1160`（除法/取余的 poison TODO 群）
  - 能学到什么：可上手的补丁长什么样；和本章关联：25.7.2

Affine/SCF/Arith 是“单设备内”的故事。下一章去两条真实的 offload 路径：OpenMP 走 translation interface，OpenACC 走 conversion——同一个目标（LLVM IR），两套完全不同的架构。

→ 下一章：[Ch 26 OpenMP / OpenACC / LLVM IR 翻译](./26-openmp-openacc-llvmir.md)
