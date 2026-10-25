# Ch 26 Transform dialect：用 IR 写变换

> **目标**：把“pass pipeline 是一串固定名字”升级成“变换本身也是一段 IR”。学完本章，你能读懂 `transform.sequence`、`transform.named_sequence`、handle、`transform.match.*`、`transform.apply_patterns` 与 `transform.foreach_match` 的分工，能自己写一个可被 `--transform-interpreter` 执行的脚本，并且能解释 handle 为什么会被 consumed、失效之后为什么不能再碰。

## 为什么需要这个？

[Ch 5](./05-pass-manager.md) 的 pipeline 是**线性**的：`canonicalize, cse, convert-scf-to-cf, ...`，顺序写死，参数写死。它对“所有输入都走同一条路”的场景很好用，但遇到下面这类需求就捉襟见肘：

- 只对满足某个形状的 `linalg.generic` 做 tiling，其他 op 不动；
- 先匹配 `scf.for`，再对它循环体里的某类 op 应用 pattern；
- 同一套变换要在不同函数、不同层级的 IR 上重复执行；
- 变换顺序依赖 payload IR 的实际结构，而不是命令行里预先写好的顺序。

Transform dialect 的答案是：**把“找谁、做什么、按什么顺序做”写成一段 IR**。这段 IR 和它要改的 IR 是两套东西：

```text
payload IR：你要优化的程序（func / scf / linalg / arith ...）
transform IR：描述怎么改 payload 的脚本（transform.* op）
```

解释器读 transform IR，按 handle 找到 payload IR 里的 op，再调用对应的变换。于是“选择”与“变换”第一次变成了同一种可组合、可复用、可测试的表示。

代价也很直接：你多了一套 IR、一套类型系统、一套失效规则。写得不对时，报错不再是“pass 失败”，而是“handle 失效”“matcher 不匹配”“操作定义了 2 个结果却绑定 3 个值”。本章就是把这些规则拆开。

## 先建立直觉：两套 IR，一套解释器

把 transform 脚本想成一段“带指针的宏”：

```text
%for = transform.structured.match ops{["scf.for"]} in %root
        │
        │  在 payload IR 里找到所有 scf.for，把它们的地址记进 handle
        ▼
transform.apply_patterns to %for { canonicalization }
        │
        │  对 handle 指向的 op 的 **body** 跑 greedy rewrite
        ▼
payload IR 被就地修改
```

这里有三个角色：

1. **payload root**：解释器作用的根 op。`mlir-opt --transform-interpreter` 默认把 pass 的锚点 op（通常是顶层 `module`）当 root。
2. **transform module**：装着 `transform.named_sequence` 的 symbol table。它通常和 payload 在同一个文件里，但逻辑上是两份 IR。
3. **handle**：transform IR 里的 SSA value，指向 payload IR 里的一组 op 或 value。它不是指针字面量，而是解释器维护的映射。

`mlir-opt` 里这两套 IR 可以放在同一个文件中。用 `module attributes {transform.with_named_sequence}` 标出“这个 symbol table 里有 transform 脚本”，解释器会把 `@__transform_main` 当作默认入口。

## 核心概念一：handle 是带类型的“载荷指针集合”

Transform IR 的 SSA value 大致分四类：

| 类型 | 指向什么 | 典型来源 |
|---|---|---|
| `!transform.any_op` | 任意 payload op 的集合 | `transform.structured.match`、`foreach_match` |
| `!transform.op<"scf.for">` | 被约束为 `scf.for` 的 op 集合 | named sequence 的参数、`transform.cast` |
| `!transform.any_value` | payload value 的集合 | `get_result`、`get_operand` 等导航 op |
| `!transform.param<i64>` | 参数值，不是 payload 实体 | `transform.param.constant` |

两个细节经常被忽略：

- **handle 是集合，不是单个 op**。`transform.structured.match` 可能匹配到 0 个、1 个或多个 op。很多 op 对“多个”有额外要求，比如 `transform.match.operation_name` 明确要求 handle 只关联一个 op，否则是 definite failure。
- **类型是约束，不是运行时检查的全部**。`!transform.op<"scf.for">` 在 verifier 层约束 handle 的类型；真正关联到哪些 payload op，由解释器在运行时决定。`foreach_match` 会在应用 matcher 时再检查一次实际 payload 是否满足类型约束。

handle 让变换可以“先选、后做”，也带来本章最重要的规则：**选出来的 handle 可能因为你刚才的变换而失效**。后面单独讲。

## 核心概念二：sequence、named_sequence 与 include

`transform.sequence` 是“按顺序执行的一段脚本”。它可以带一个 root handle，也可以作为顶层 sequence 直接绑定 payload root：

```mlir
transform.sequence %root : !transform.any_op failures(propagate) {
^bb0(%arg0: !transform.any_op):
  transform.yield
}
```

`failures(propagate)` 表示内部 op 的 silenceable failure 会立刻让整个 sequence 失败；`failures(suppress)` 则忽略 silenceable failure 继续往下跑。注意源码里的警告：**suppress 之后 payload 可能处于不适合继续变换的状态**，调用者要自己保证后续操作足够健壮（`TransformOps.td:1256` 附近的 `SequenceOp` 文档）。

`transform.named_sequence` 是可复用的函数式脚本：

```mlir
transform.named_sequence @canonicalize_target(
    %target: !transform.any_op {transform.readonly}) {
  transform.apply_patterns to %target {
    transform.apply_patterns.canonicalization
  } : !transform.any_op
  transform.yield
}
```

它必须是单 block、以 `transform.yield` 结束。参数上的 `{transform.readonly}` / `{transform.consumed}` 是**对外契约**：前者承诺只读，后者表示可能消费。入口 `@__transform_main` 的名字来自 `TransformDialect.td:23-25` 的 `kTransformEntryPointSymbolName`。

调用 named sequence 有两种方式：

- `transform.include @name failures(propagate) (%handle) : (!transform.any_op) -> ()`（本机用该写法跑过，payload 输出与示例一一致）
- `transform.foreach_match` 把 matcher/action 两个 named sequence 配对，对每个匹配到的 payload op 调用 action。

`include` 适合“我已知要处理哪个 handle”；`foreach_match` 适合“遍历一棵子树，对每个满足条件的 op 做动作”。两者都要求被调用的 sequence 存在、签名匹配、调用图无递归；`transform.with_named_sequence` 的 verifier 会检查这些（`TransformDialect.cpp:206-229`）。

## 核心概念三：选择、重写与失效

### 3a. match 与 apply_patterns 的分工

匹配 op 有两套常用工具：

- `transform.structured.match ops{["scf.for"]} in %root`：按 op 名（以及可选的接口/属性条件）在 payload 子树里找 op，返回一个 handle。它来自 structured transform ops，名字里的 `structured` 是历史命名，不意味着只能匹配 Linalg。
- `transform.match.operation_name %op ["scf.for"]`：检查**已经拿到的单个 handle** 是否是给定名字之一。名字不符时产生 silenceable failure；handle 关联多个 op 时产生 definite failure。

`transform.apply_patterns` 则负责“在 handle 指向的 op 的 **body** 上跑 greedy pattern rewrite”：

```mlir
%for = transform.structured.match ops{["scf.for"]} in %root
    : (!transform.any_op) -> !transform.any_op
transform.apply_patterns to %for {
  transform.apply_patterns.canonicalization
} : !transform.any_op
```

源码写得很清楚：**patterns are not applied to the targeted op itself**（`TransformOps.td:299-360`）。这解释了一个常见误解：如果你 match 的是 `arith.addi`，然后对它的 handle 调 `apply_patterns`，`addi` 自己不会被折叠；你匹配它的父 op（比如 `scf.for` 或 `func.func`），它才会作为 body 的一部分被 rewrite。

`apply_patterns` 的花括号区域是 **pattern descriptor region**，里面放 `transform.apply_patterns.canonicalization`、`transform.apply_patterns.scf.for_loop_canonicalization` 之类的描述 op。它们不是普通变换，而是告诉 driver “要收集哪些 pattern”。CSE 用单独的 `transform.apply_cse`，或给 `apply_patterns` 加 `apply_cse` 属性，让两者交替跑到不动点。

### 3b. foreach_match 与 handle 失效

`transform.foreach_match` 的模型是“后序遍历 payload 子树，对每个 op 依次试 matcher；第一个成功的 matcher 决定 action”：

```mlir
transform.foreach_match in %root
    @match_for -> @canonicalize_target
    : (!transform.any_op) -> !transform.any_op
```

几个行为必须记住（均来自 `TransformOps.td:526-620` 的 `ForeachMatchOp` 文档）：

- matcher 的 **silenceable failure 会被吞掉**，继续试下一个 matcher；所有 matcher 都不匹配也算成功，只是什么都不改。
- matcher 的 **definite failure** 会立刻终止整个 `foreach_match`。
- action 失败会向上传播；如果只是 silenceable failure，遍历会继续，但最终 `foreach_match` 返回 silenceable failure。
- action 只能修改当前匹配 op 及其嵌套 op，不能改兄弟或父 op；需要改父 op 时，先匹配父 op，再从父 op 往下找。
- 默认不匹配 root 本身；加 `restrict_root` 才会把 root 也纳入匹配，前提是你保证 action 不会让 root 失效。

最重要的语义是：**`foreach_match` 会消费它的 root 操作数**。源码说这是为了触发“root 内嵌套 op 的 handle 失效”，因为这些 op 很可能被 action 改掉。消费不是“引用计数减一”，而是“这个 handle 以及指向其嵌套实体的 handle 都作废”。

失效规则在 `TransformInterfaces.cpp:696-760` 实现，官方文档在 `docs/Dialects/Transform.md:272-320`。简化成一句话：

> 一个 handle 被 consume 后，解释器会把“指向同一 payload 实体”和“指向其嵌套实体”的所有 handle 标记为 invalidated；之后再拿它当 operand，直接报错。

这不是可选的卫生习惯，而是正确性边界。失效 handle 类似悬空指针；解释器会在下一次使用前检查并给出诊断，而不是让你读到已删除的 op。

## 底层机制：解释器如何把脚本跑起来

本机先确认注册了哪些相关 pass：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" --help 2>&1 | Select-String -Pattern 'transform'
```

实测输出（节选）：

```text
  --transform-dialect-check-uses    - warn about potential use-after-free in the transform dialect
  --transform-infer-effects         - infer transform side effects for symbols
  --transform-interpreter           - transform dialect interpreter
  --transform-preload-library       - preload transform dialect library
```

注意：**没有** `--test-transform-dialect-interpreter`。那是 test dialect 的配套 pass，本机是 Optimized build、没有 test dialect，所以本章全部用正式版 `--transform-interpreter`。

`InterpreterPass` 的执行顺序（`InterpreterPass.cpp:129-153`）：

```text
取 preloaded transform module
  → 找 payload root（默认是 pass 锚点，可用 transform.target_tag 指定）
  → 找 entry point（默认 @__transform_main，可用 entry-point= 改）
  → 绑定 root 到入口的第一个参数
  → applyTransformNamedSequence(entryPoint, transformModule, options)
```

对应源码 `mlir/include/mlir/Dialect/Transform/Transforms/Passes.td:64-108`。几个实用选项：

- `--transform-interpreter="entry-point=foo"`：换入口 symbol。
- `--transform-interpreter="debug-payload-root-tag=tag"`：只处理带 `transform.target_tag = "tag"` 的 payload op。
- `--transform-interpreter="disable-expensive-checks"`：关掉解释器的昂贵检查，调试时别关。
- `--transform-dialect-check-uses`：静态检查潜在的 use-after-free，适合放进 CI。

脚本和 payload 放在同一个文件时，`transform.with_named_sequence` 必须挂在一个 symbol table（通常是 `module`）上。缺了它会直接报：

```text
error: expects the parent symbol table to have the 'transform.with_named_sequence' attribute
```

## 示例一：在 scf.for 里做 canonicalization

目标：只对 `scf.for` 的 body 应用 canonicalization，把 `%acc + 0` 折掉，证明 `apply_patterns` 作用在 body 而不是目标 op 自己。

输入 `basic.mlir`：

```mlir
func.func @fold_in_loop(%n: index, %x: i32) -> i32 {
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  %zero = arith.constant 0 : i32
  %r = scf.for %i = %c0 to %n step %c1 iter_args(%acc = %x) -> (i32) {
    %next = arith.addi %acc, %zero : i32
    scf.yield %next : i32
  }
  return %r : i32
}

module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%root: !transform.any_op) {
    %0 = transform.structured.match ops{["scf.for"]} in %root
        : (!transform.any_op) -> !transform.any_op
    transform.apply_patterns to %0 {
      transform.apply_patterns.canonicalization
    } : !transform.any_op
    transform.yield
  }
}
```

执行：

```powershell
& "$mlir\mlir-opt.exe" .\basic.mlir --transform-interpreter
```

实测输出（payload 部分，省略脚本 module）：

```mlir
module {
  func.func @fold_in_loop(%arg0: index, %arg1: i32) -> i32 {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    %c0_i32 = arith.constant 0 : i32
    %0 = scf.for %arg2 = %c0 to %arg0 step %c1 iter_args(%arg3 = %arg1) -> (i32) {
      scf.yield %arg3 : i32
    }
    return %0 : i32
  }
}
```

三个观察：

1. `arith.addi %acc, %zero` 没了，`scf.yield` 直接返回 `%arg3`；这是 canonicalization 在 body 里折叠的结果。
2. `%c0_i32` 还在。canonicalization 不做通用 DCE，未使用的常量不会被它顺手删掉；需要的话再加 `transform.apply_dce`。
3. 脚本 module 原样留在输出里。解释器只改 payload，不清理 transform IR；真实 pipeline 里通常在脚本之后加一个 `symbol-dce` 或把脚本放在独立文件。

对照实验：如果把 match 改成 `arith.addi`，输出里的 `addi` **不会**被折叠。这是 `apply_patterns` “not applied to the targeted op itself” 的直接后果，也是本章最容易踩的坑之一。

## 示例二：用 structured.tile_using_for 切 matmul

目标：匹配 `linalg.generic`，按 `[4, 4]` 切前两个维度，观察 loop nest、`tensor.extract_slice` / `insert_slice` 和 tiled op 的关系。

输入 `tile.mlir`（payload 是一个 16×16 的 matmul）：

```mlir
#map = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>
func.func @matmul(%A: tensor<16x16xf32>, %B: tensor<16x16xf32>,
                  %C: tensor<16x16xf32>) -> tensor<16x16xf32> {
  %0 = linalg.generic {indexing_maps = [#map, #map1, #map2],
                       iterator_types = ["parallel", "parallel", "reduction"]}
      ins(%A, %B : tensor<16x16xf32>, tensor<16x16xf32>)
      outs(%C : tensor<16x16xf32>) {
  ^bb0(%a: f32, %b: f32, %c: f32):
    %m = arith.mulf %a, %b : f32
    %s = arith.addf %m, %c : f32
    linalg.yield %s : f32
  } -> tensor<16x16xf32>
  return %0 : tensor<16x16xf32>
}
```

脚本：

```mlir
module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%root: !transform.any_op) {
    %mm = transform.structured.match ops{["linalg.generic"]} in %root
        : (!transform.any_op) -> !transform.any_op
    %tiled, %loops:2 = transform.structured.tile_using_for %mm tile_sizes [4, 4]
        : (!transform.any_op) -> (!transform.any_op, !transform.any_op, !transform.any_op)
    transform.yield
  }
}
```

执行：

```powershell
& "$mlir\mlir-opt.exe" .\tile.mlir --transform-interpreter
```

实测输出（节选，省略脚本 module）：

```mlir
%c0 = arith.constant 0 : index
%c16 = arith.constant 16 : index
%c4 = arith.constant 4 : index
%0 = scf.for %arg3 = %c0 to %c16 step %c4 iter_args(%arg4 = %arg2) -> (tensor<16x16xf32>) {
  %1 = scf.for %arg5 = %c0_0 to %c16_1 step %c4_2 iter_args(%arg6 = %arg4) -> (tensor<16x16xf32>) {
    %extracted_slice = tensor.extract_slice %arg0[%arg3, 0] [4, 16] [1, 1] : tensor<16x16xf32> to tensor<4x16xf32>
    %extracted_slice_3 = tensor.extract_slice %arg1[0, %arg5] [16, 4] [1, 1] : tensor<16x16xf32> to tensor<16x4xf32>
    %extracted_slice_4 = tensor.extract_slice %arg6[%arg3, %arg5] [4, 4] [1, 1] : tensor<16x16xf32> to tensor<4x4xf32>
    %2 = linalg.generic {indexing_maps = [#map, #map1, #map2],
                         iterator_types = ["parallel", "parallel", "reduction"]}
        ins(%extracted_slice, %extracted_slice_3 : tensor<4x16xf32>, tensor<16x4xf32>)
        outs(%extracted_slice_4 : tensor<4x4xf32>) {
    ^bb0(%in: f32, %in_5: f32, %out: f32):
      %3 = arith.mulf %in, %in_5 : f32
      %4 = arith.addf %3, %out : f32
      linalg.yield %4 : f32
    } -> tensor<4x4xf32>
    %inserted_slice = tensor.insert_slice %2 into %arg6[%arg3, %arg5] [4, 4] [1, 1]
        : tensor<4x4xf32> into tensor<16x16xf32>
    scf.yield %inserted_slice : tensor<16x16xf32>
  }
  scf.yield %1 : tensor<16x16xf32>
}
```

关键点：

- `tile_sizes [4, 4]` 产生 **两个** `scf.for`，所以结果句柄是 `%tiled, %loops:2`，类型列表一共三个 `!transform.any_op`（tiled op + 两个 loop）。写成一个 `%loops` 会直接报 “expected number of loops to tile (2) to match number of `loops` results (1)”。
- `tile_using_for` 负责生成 loop、slice、insert，并返回 **tiled op** 和 **loop handles**；它不负责后续的 vectorize、bufferize。
- 这个 transform 来自 Linalg transform ops（`LinalgTransformOps.td:2248`），所以 `mlir-opt` 必须链接了 Linalg transforms。本机 conda 包满足；如果换成裁剪构建，op 可能不存在。

## 示例三：失败与失效的真实诊断

### 消费后再用：真实错误

把示例一的脚本改成先 `foreach_match`，再拿同一个 `%root` 调 `apply_patterns`：

```mlir
%updated = transform.foreach_match in %root
    @match_for -> @canonicalize_target
    : (!transform.any_op) -> !transform.any_op
transform.apply_patterns to %root {
  transform.apply_patterns.canonicalization
} : !transform.any_op
```

实测诊断（路径与行号来自本机临时文件，核心信息不变）：

```text
error: op uses a handle invalidated by a previously executed transform op
    transform.apply_patterns to %root {
    ^
note: handle to invalidated ops
  transform.named_sequence @__transform_main(%root: !transform.any_op) {
                                             ^
note: invalidated by this transform op that consumes its operand #0
      and invalidates all handles to payload IR entities associated with
      this operand and entities nested in them
    %updated = transform.foreach_match in %root
               ^
```

这段诊断把三件事说全了：哪个 op 用了失效 handle、哪个 handle 失效、谁导致的失效。修法不是“加个 cast”，而是用 `%updated` 代替 `%root`，或者重新 match 一个新的 handle。

## 对比与取舍：pipeline 还是 transform IR

| 维度 | pass pipeline（Ch 5） | transform IR |
|---|---|---|
| 表达单位 | 具名 pass + 选项 | transform op + handle |
| 选择能力 | 固定，按 pass 内部逻辑 | 可按 op 名、接口、属性、嵌套关系选择 |
| 复用 | 命令行/代码里拼 | `named_sequence` + `include`/`foreach_match` |
| 调试 | `--mlir-print-ir-after-all` 看 pass 边界 | 看脚本、handle、`transform.print`，再多一层映射 |
| 稳定性 | pass 名相对稳定 | transform op 名与语义随版本演进更快 |
| 适合 | 生产 lowering 的固定骨架 | 调度、tiling、按形状选择、实验性变换 |

我们的建议是**混合**：

- 稳定的 lowering 用 pipeline：`canonicalize`、`convert-scf-to-cf`、`convert-func-to-llvm` 这些不需要 payload 级选择。
- 需要“看 IR 再决定”的部分用 transform IR：tiling、fusion、按 shape 选策略。
- transform 脚本里可以调 `transform.apply_registered_pass` 复用已有 pass（`TransformOps.td:402`），不必把所有逻辑重写成 transform op。

Autotuning 只提边界：MLIR 有 `TuneExtension` 之类的搜索空间表达，能把 tile size 等参数交给外部搜索器；本课不展开。手工脚本和 autotuning 的分界线是“参数是否在编译期由人决定”；一旦要跑几百个配置，脚本本身也会变成被搜索的对象。

## 踩雷集锦

1. **对目标 op 自己 `apply_patterns`**。它只处理 body，不处理 target 本身。想折叠 `arith.addi`，匹配它的父 op，或者用 `transform.apply_registered_pass` 跑 `canonicalize`。
2. **忘了 `transform.with_named_sequence`**。`named_sequence` 的父 symbol table 没这个属性，verifier 直接报错，解释器根本不会启动。
3. **把 `foreach_match` 的 root 再用一次**。它消费 root 并让嵌套 handle 失效；用返回值 `%updated`，或重新 match。
4. **以为 matcher 不匹配会失败**。silenceable failure 被吞掉，整体成功；测试要断言“输出变了”，不能只断言退出码为 0。
5. **`tile_using_for` 的结果个数写错**。`tile_sizes` 里每个非零项对应一个 loop 结果；`[4, 4]` 要 `%tiled, %loops:2`，类型列表也要三个 handle。
6. **在 release 包里找 `--test-transform-dialect-interpreter`**。本机只有 `--transform-interpreter`；test 版本依赖 test dialect，Optimized build 没有。
7. **把 transform IR 当成“更高级的 pipeline”**。它多一层 handle 映射和失效规则；如果只是固定顺序跑 pass，pipeline 更简单、更稳。

## 进阶：再往深一层

- **`transform-dialect-check-uses`**：静态扫描 transform IR 的潜在 use-after-free，适合在 CI 里对脚本做 lint；它不能证明所有分支都安全，但能抓住大部分“消费后复用”。
- **`transform.target_tag`**：给 payload 的某个 op 打 tag，用 `debug-payload-root-tag` 把脚本作用域缩到它；大文件里调试单个函数时很好用。
- **自定义 transform op**：实现 `TransformOpInterface`，用 `applyToOne` 或 `TransformEachOpTrait` 接入自己的 IR；这时你要自己声明 memory effects，尤其是 `Free`，否则失效分析会漏报。

## 动手练习

### 练习 1：只折叠循环内的加法

**任务**：构造一个函数，循环体内有 `arith.addi %acc, 0`，循环外也有一个 `arith.addi %x, 0`；写脚本只折叠循环内的那个，循环外的保持原样。

**测试用例**：
- 用例 A：只有循环内加法，期望 `scf.yield` 直接返回 iter_arg。
- 用例 B：只有循环外加法，期望 `addi` 原样保留。
- 用例 C：两者都有，期望只改循环内，循环外仍是 `arith.addi`。

**步骤**：
1. 先只跑 `--canonicalize`，确认两个加法都会被折叠，建立基线。
2. 改用 `transform.structured.match ops{["scf.for"]}`，把 handle 交给 `apply_patterns`。
3. 用 `Select-String` 或 `FileCheck` 断言循环内没有 `addi`，循环外仍有。

<details>
<summary>参考解答</summary>
核心脚本就是示例一：match `scf.for`，再 `apply_patterns`。因为 `apply_patterns` 只处理 target 的 body，循环外的 `addi` 不在 handle 的 body 里，所以不会被折叠。若把 match 改成 `func.func`，两个都会被折叠——这正好反证了作用域。
</details>

**常见卡点**：match 到 `arith.addi` 本身再 `apply_patterns`，结果什么都不变；这是“不作用于 target 自己”的规则，不是 pattern 失效。

### 练习 2：tile size 与结果个数

**任务**：用同一个 matmul，把 `tile_sizes` 从 `[4, 4]` 改成 `[4, 0]`，观察 loop 数量和结果绑定的变化；再试 `[8, 8]`。

**测试用例**：
- 用例 A：`[4, 4]`，期望两个 `scf.for`，脚本写 `%tiled, %loops:2`。
- 用例 B：`[4, 0]`，期望一个 `scf.for`，脚本写 `%tiled, %loops`。
- 用例 C：`[8, 8]`，期望两个 `scf.for`，step 变成 8。

**步骤**：
1. 先跑示例二，数输出里的 `scf.for` 个数。
2. 改 tile_sizes，同步改结果绑定和类型列表。
3. 故意少写一个 `:2`，记录 verifier 报错，再改回。

<details>
<summary>参考解答</summary>
`tile_using_for` 的结果个数 = 1 个 tiled op + “静态非零 tile size 的个数”个 loop handle。`[4, 0]` 只有第一个维度切，所以是 `%tiled, %loops`，类型列表两个 handle；`[0, 0]` 按源码说明是 no-op，不生成 loop。
</details>

**常见卡点**：只改 `tile_sizes` 不改结果绑定，verifier 会在解释前报 “expected number of loops to tile ...”。

### 练习 3：修一个失效 handle

**任务**：复现示例三的 “handle invalidated” 错误，然后用三种方式修复：改用 `%updated`、重新 match、把 `foreach_match` 移到 `apply_patterns` 之后。

**测试用例**：
- 用例 A：`foreach_match` 后用 `%root`，期望报 invalidated。
- 用例 B：改用 `%updated`，期望通过且循环内加法被折叠。
- 用例 C：`foreach_match` 后重新 `structured.match` 一个 `scf.for`，期望通过。

**步骤**：
1. 保存会报错的脚本，确认诊断里的 “consumes its operand #0”。
2. 把 `%root` 替换成 `%updated`，重跑。
3. 再试“先 apply_patterns，后 foreach_match”，比较两种顺序的语义差异。

<details>
<summary>参考解答</summary>
`foreach_match` 返回的新 handle 就是更新后的 root；旧 root 被消费，指向其嵌套实体的 handle 也一并失效。修复不是 `transform.cast`，而是使用返回值或重新匹配。顺序反过来之所以可行，是因为 `apply_patterns` 还没消费 root。
</details>

**常见卡点**：以为“handle 只是指针，复制一份就能绕过”。复制 SSA value 不会产生新的映射，失效仍然跟着原 handle。

### 练习 4：include 与 foreach_match 的等价与差异

**任务**：把示例一的脚本改写成 `transform.include @canonicalize_target` 调用；再造一个有两个 `scf.for` 的 payload，比较 `include` 和 `foreach_match` 的行为。

**测试用例**：
- 用例 A：一个 `scf.for`，两种写法都应折叠 body。
- 用例 B：两个 `scf.for`，`include` 对 `structured.match` 返回的集合逐个应用；`foreach_match` 对每个匹配 op 调用 action。
- 用例 C：没有 `scf.for`，两种写法都不应报错；`foreach_match` 的 matcher 失败被静默。

**步骤**：
1. 先用 `include` 版本跑通，确认输出与示例一一致。
2. 换成 `foreach_match` + `match.operation_name`，确认输出一致。
3. 去掉所有 `scf.for`，观察两者都成功但 payload 不变。

<details>
<summary>参考解答</summary>
`include` 适合“已经有一个 handle，直接调用一个 sequence”；`foreach_match` 适合“遍历子树，对每个满足 matcher 的 op 调用 action”。前者不消费 root（除非被调 sequence 自己消费），后者消费 root 并返回更新后的 handle。二者都能复用同一个 `@canonicalize_target`。
</details>

**常见卡点**：`foreach_match` 的 matcher 返回类型必须和 action 的参数类型兼容；matcher 不能消费参数，因为它可能被多个 matcher 复用。

## 本章重点整理

- payload IR 是被变换的程序，transform IR 是描述变换的脚本；两者共用 MLIR 基础设施，但逻辑上分离。
- handle 是带类型的 payload 实体集合；`!transform.any_op` 不保证只有一个 op。
- `transform.sequence` 管顺序与失败传播，`transform.named_sequence` 管复用，`transform.include` / `foreach_match` 管调用。
- `apply_patterns` 不作用于 target 本身；这是最高频的语义误解。
- `foreach_match` 消费 root，并让指向嵌套实体的 handle 失效；用返回值，不要复用旧 handle。
- 本机只有 `--transform-interpreter`，没有 test 版本；入口默认 `@__transform_main`。

## 自我检核

- [ ] 我能用一句话说清 payload IR 与 transform IR 的分工
- [ ] 我能解释 `transform.named_sequence` 为什么需要 `transform.with_named_sequence`
- [ ] 我知道 `apply_patterns` 作用于 target 的 body，而不是 target 自己
- [ ] 我能说出 `foreach_match` 消费 root 后，哪些 handle 会失效
- [ ] 我能读懂 “op uses a handle invalidated by a previously executed transform op”
- [ ] 我知道本机没有 `--test-transform-dialect-interpreter`，以及替代方案

## 延伸阅读

- **`mlir/include/mlir/Dialect/Transform/IR/TransformOps.td`**
  - **读哪里**：`ApplyPatternsOp`（299 行起）、`ForeachMatchOp`（526 行起）、`NamedSequenceOp`（1009 行起）、`SequenceOp`（1256 行起）。
  - **学什么**：每个 op 的语义、失败模式、结果个数和 verifier 约束。
  - **和本章的关联**：本章的示例与踩雷都能在这几个定义里找到依据。
- **`mlir/lib/Dialect/Transform/Interfaces/TransformInterfaces.cpp`**
  - **读哪里**：696–760 行的 `checkAndRecordHandleInvalidationImpl`。
  - **学什么**：`Free` effect 如何触发 handle 失效，为什么嵌套实体的 handle 也会被标记。
  - **和本章的关联**：示例三的诊断就来自这条路径。
- **`mlir/docs/Dialects/Transform.md`**
  - **读哪里**：272–320 行的 “Handle Invalidation”，以及前面的 execution model。
  - **学什么**：失效规则的完整列表，包括 value handle 和 block argument 的情况。
  - **和本章的关联**：比源码更好读，适合先读文档再对照实现。

→ [Ch 27 工程化](./27-engineering.md)
