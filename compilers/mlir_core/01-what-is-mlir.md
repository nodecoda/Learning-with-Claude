# Ch 1 MLIR 是什么：多级 IR 与 dialect 组合

> **目标**：建立本课最重要的心智模型——MLIR 不是“另一种 LLVM IR 语法”，而是一个**能同时容纳多个抽象层次的 IR 容器**。学完本章，你看到任何一段 `.mlir`，都能先判断它处在哪一层、由哪些 dialect 组成、下一步该往哪里降。

## 为什么需要这个？

一个真实的编译器栈，通常有这些层次：

```text
源语言 AST → 高层语义 IR（带类型/形状） → 循环/张量层 → 控制流层 → LLVM IR → 机器码
```

传统做法是**每一层定义一个 IR，层与层之间写一个转换器**。层次一多，转换器数量呈平方增长，而且每套 IR 都要自己的 parser、printer、verifier、pass 基础设施。

MLIR 的答案是：**只做一套通用 IR 基础设施，把“这一层长什么样”交给 dialect 定义**。于是：

- 一套 parser/printer/verifier/pass 框架，服务所有层；
- 同一份 module 里可以同时出现 `linalg`、`scf`、`arith`、`llvm`；
- 降级不是“换一个 IR”，而是“把某些 op 替换成另一个 dialect 的 op”。

代价是：你必须始终知道“现在在哪一层”。这正是本课要训练的判断力。

## 先建立直觉：同一个循环，四种形态

下面四段代码描述的是**同一个求和循环**。先不要纠结语法，只看“抽象层次”如何变化。

### 形态一：`scf` 层（结构化控制流）

```mlir
func.func @sum(%n: index) -> i32 {
  %c0 = arith.constant 0 : i32
  %c1 = arith.constant 1 : i32
  %zero = arith.constant 0 : index
  %one = arith.constant 1 : index
  %r = scf.for %i = %zero to %n step %one iter_args(%acc = %c0) -> (i32) {
    %next = arith.addi %acc, %c1 : i32
    scf.yield %next : i32
  }
  return %r : i32
}
```

这里有三个层次同时在场：`func`（函数）、`scf`（结构化循环）、`arith`（标量运算）。`scf.for` 自带 `iter_args`，把“循环携带变量”写进了语法，而不是靠 phi 节点。

### 形态二：`cf` 层（显式基本块）

对上一段跑 `convert-scf-to-cf`：

```powershell
& "$mlir\mlir-opt.exe" sum.mlir --pass-pipeline='builtin.module(func.func(convert-scf-to-cf))'
```

实测输出：

```mlir
module {
  func.func @sum(%arg0: index) -> i32 {
    %c0_i32 = arith.constant 0 : i32
    %c1_i32 = arith.constant 1 : i32
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    cf.br ^bb1(%c0, %c0_i32 : index, i32)
  ^bb1(%0: index, %1: i32):  // 2 preds: ^bb0, ^bb2
    %2 = arith.cmpi slt, %0, %arg0 : index
    cf.cond_br %2, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %3 = arith.addi %1, %c1_i32 : i32
    %4 = arith.addi %0, %c1 : index
    cf.br ^bb1(%4, %3 : index, i32)
  ^bb3:  // pred: ^bb1
    return %1 : i32
  }
}
```

观察：

- 循环变成了**基本块 + 显式跳转**；
- `iter_args` 变成了 **block argument**（`^bb1(%0: index, %1: i32)`）；
- `arith` 还在，因为这一步只降 `scf`；
- `return` 还是 `func.return`，因为 `func` 也没降。

### 形态三：`llvm` dialect 层

继续把 `func`、`cf`、`arith` 全部降到 `llvm` dialect：

```powershell
& "$mlir\mlir-opt.exe" sum.mlir `
  --pass-pipeline='builtin.module(func.func(convert-scf-to-cf),convert-func-to-llvm,convert-cf-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)'
```

实测输出：

```mlir
module {
  llvm.func @sum(%arg0: i64) -> i32 {
    %0 = llvm.mlir.constant(0 : i32) : i32
    %1 = llvm.mlir.constant(1 : i32) : i32
    %2 = llvm.mlir.constant(0 : index) : i64
    %3 = llvm.mlir.constant(1 : index) : i64
    llvm.br ^bb1(%2, %0 : i64, i32)
  ^bb1(%4: i64, %5: i32):  // 2 preds: ^bb0, ^bb2
    %6 = llvm.icmp "slt" %4, %arg0 : i64
    llvm.cond_br %6, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %7 = llvm.add %5, %1 : i32
    %8 = llvm.add %4, %3 : i64
    llvm.br ^bb1(%8, %7 : i64, i32)
  ^bb3:  // pred: ^bb1
    llvm.return %5 : i32
  }
}
```

两个容易忽略的细节：

- **`index` 变成了 `i64`**：MLIR 的 `index` 是目标相关的整数类型，默认按 64 位降级。它不是 `i64` 的别名。
- **`arith.cmpi` 变成了 `llvm.icmp "slt"`**：谓词从属性变成了字符串字面量，因为 `llvm.icmp` 的语法就是 LLVM 的写法。

### 形态四：LLVM IR

最后交给 `mlir-translate`：

```powershell
& "$mlir\mlir-opt.exe" sum.mlir `
  --pass-pipeline='builtin.module(func.func(convert-scf-to-cf),convert-func-to-llvm,convert-cf-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)' `
  | & "$mlir\mlir-translate.exe" --mlir-to-llvmir
```

实测输出：

```llvm
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

define i32 @sum(i64 %0) {
  br label %2

2:                                                ; preds = %6, %1
  %3 = phi i64 [ %8, %6 ], [ 0, %1 ]
  %4 = phi i32 [ %7, %6 ], [ 0, %1 ]
  %5 = icmp slt i64 %3, %0
  br i1 %5, label %6, label %9

6:                                                ; preds = %2
  %7 = add i32 %4, 1
  %8 = add i64 %3, 1
  br label %2

9:                                                ; preds = %2
  ret i32 %4
}
```

注意 `phi` 出现了。MLIR 用 **block argument** 表达 SSA 合并，LLVM IR 用 **phi 指令**；`mlir-translate` 负责这层机械转换。这也是 Ch 2 要讲“Region/Block 为什么这样设计”的原因之一。

## 核心概念一：Operation 是唯一的基本单元

MLIR 里没有“表达式”“语句”“声明”的区分，只有 **Operation**。`module`、`func.func`、`arith.addi`、`return`、`linalg.add` 全是 Operation。

一个 Operation 由这些部分组成：

| 部分 | 含义 | 例子 |
|---|---|---|
| 名字 | `dialect.op` 形式的唯一标识 | `arith.addi` |
| 操作数 | 它消费的 SSA value | `%a, %b` |
| 结果 | 它产生的 SSA value 及其类型 | `%0 : i32` |
| 属性 | 编译期常量信息 | `{fastmath = #arith.fastmath<none>}` |
| Region | 嵌套的 IR 区域 | `scf.for` 的循环体 |
| Successor | CFG 后继块 | `cf.br ^bb1` |
| Properties | 有类型的内在属性（23.x 的新机制） | op 的固有字段 |
| Location | 源码位置 | `loc("x.mlir":3:5)` |

关键点：**Operation 是通用的**。parser 不需要为每个 op 写一套语法树；它先构造通用结构，再让 dialect 决定“怎么打印成好看的 custom 语法”。

`--mlir-print-op-generic` 会把所有糖剥掉。以 Ch 0 的 `arith.addi` 为例：

```mlir
%0 = "arith.addi"(%arg0, %arg1) <{overflowFlags = #arith.overflow<none>}> : (i32, i32) -> i32
```

这就是 op 的真实结构：名字是字符串，操作数和结果带类型，属性在尖括号里。custom 语法 `%0 = arith.addi %a, %b : i32` 只是打印器的一层皮。

## 核心概念二：Dialect 是“命名空间 + 语义包”

一个 dialect 通常包含：

- **op 集合**：`arith.addi`、`scf.for`、`linalg.add`；
- **类型**：`memref<4x4xf32>`、`tensor<4x4xf32>`、`!llvm.ptr`；
- **属性**：`#arith.fastmath<none>`、`#linalg.iterator_type<parallel>`；
- **接口**：让 op 参与 canonicalize、bufferize、conversion 等框架（Ch 8）；
- **验证规则**：每个 op 的 `verify()`。

本机 `mlir-opt` 注册了 **47 个 dialect**：

```text
acc, affine, amdgpu, arith, arm_neon, arm_sme, arm_sve, async, bufferization,
builtin, cf, complex, dlti, emitc, func, gpu, index, irdl, linalg, llvm, math,
memref, ml_program, mpi, nvgpu, nvvm, omp, pdl, pdl_interp, ptr, quant, rocdl,
scf, shape, shard, smt, sparse_tensor, spirv, tensor, tosa, transform, ub,
vector, wasmssa, x86, xegpu, xevm
```

这解释了 Ch 0 的错误：`mlir-translate` 只注册了其中一小部分，所以它不认识 `arith.addi`。

**注册不等于加载**。dialect 需要被显式加载到 `MLIRContext`，或者由某个 op 的 parser 触发加载。Ch 16 会讲 `dependent dialects`：为什么你的 dialect 引用了 `arith` 的类型时，必须声明这个依赖。

## 核心概念三：Region 让 IR 可以嵌套

Region 是一串 Block，Block 是一串 Operation。听起来抽象，看一个真实例子就清楚了。

```mlir
func.func @mixed(%A: tensor<4x4xf32>, %B: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %empty = tensor.empty() : tensor<4x4xf32>
  %0 = linalg.add ins(%A, %B : tensor<4x4xf32>, tensor<4x4xf32>) outs(%empty : tensor<4x4xf32>) -> tensor<4x4xf32>
  return %0 : tensor<4x4xf32>
}
```

这段 module 里同时有 `func`、`tensor`、`linalg` 三个 dialect。跑 `--mlir-print-op-generic`：

```mlir
"builtin.module"() ({
  "func.func"() <{function_type = (tensor<4x4xf32>, tensor<4x4xf32>) -> tensor<4x4xf32>, sym_name = "mixed"}> ({
  ^bb0(%arg0: tensor<4x4xf32>, %arg1: tensor<4x4xf32>):
    %0 = "tensor.empty"() : () -> tensor<4x4xf32>
    %1 = "linalg.add"(%arg0, %arg1, %0) <{operandSegmentSizes = array<i32: 2, 1>}> ({
    ^bb0(%arg2: f32, %arg3: f32, %arg4: f32):
      %2 = "arith.addf"(%arg2, %arg3) <{fastmath = #arith.fastmath<none>}> : (f32, f32) -> f32
      "linalg.yield"(%2) : (f32) -> ()
    }) : (tensor<4x4xf32>, tensor<4x4xf32>, tensor<4x4xf32>) -> tensor<4x4xf32>
    "func.return"(%1) : (tensor<4x4xf32>) -> ()
  })
})
```

这里有 **四层嵌套**：

```text
builtin.module
└── func.func
    └── linalg.add        ← 注意：它自己带一个 region
        └── arith.addf + linalg.yield
```

`linalg.add` 的 region 描述了“每个元素怎么算”。也就是说，**一个 op 内部可以再装一整段 IR**。这就是“多级 IR”在数据结构上的含义：层次不是靠不同的文件/不同的类实现的，而是靠 region 嵌套实现的。

`func.func` 的函数体也是一个 region，只有一个 block，block argument 就是函数参数。这解释了 Ch 0 里的 `%arg0`：它是 block argument，不是“变量声明”。

## 底层机制：多级 IR 如何共存而不冲突

MLIR 能做到“一个 module 里混多个 dialect”，靠的是三件事：

1. **op 用字符串名字标识**，没有全局 opcode 枚举。`arith.addi` 和 `my.add` 在数据结构上完全同构。
2. **每个 op 自带验证器**。verifier 遍历 IR 时，把每个 op 交回它所属的 dialect 检查。所以 `linalg.add` 的 region 结构和 `arith.addi` 的操作数类型可以有不同的规则。
3. **pass 按层嵌套**。`builtin.module(...)`、`func.func(...)` 是 PassManager 的嵌套语法，不是装饰。你在 `func.func` 这一层跑 `convert-scf-to-cf`，就只处理函数体里的 op。

这也带来一个必须接受的现实：**混合层次是合法的中间状态**。`func.func` 里可以有 `linalg`，`llvm.func` 里可以暂时残留 `arith.addi`。不要指望“一次 pass 之后 IR 就干净了”；真正的 pipeline 是分阶段收敛的。

## 示例一：只降一层，观察“剩下什么”

前面 `scf → cf` 的输出里，`arith` 和 `func` 都还在。这不是 bug，而是 conversion 的粒度决定的：`convert-scf-to-cf` 只负责 `scf` 的 op。

你可以用 `--mlir-print-ir-after-all` 验证每一步的边界。实测的关键片段：

```text
// IR Dump After SCFToControlFlowPass: convert-scf-to-cf{allow-pattern-rollback=true} //-----
...
  ^bb1(%0: index, %1: i32):
    %2 = arith.cmpi slt, %0, %arg0 : index
...
// IR Dump After ConvertFuncToLLVMPass: convert-func-to-llvm //-----
  llvm.func @sum(%arg0: i64) -> i32 {
    %0 = llvm.mlir.constant(0 : i32) : i32
...
```

注意 `func → llvm` 那一步，函数签名已经变成 `llvm.func`，但函数体里的 `arith` op 还没动（直到 `convert-arith-to-llvm`）。**转换是“按 dialect 切片”的**，这是 Part 3 的核心。

## 示例二：一个 op 内部的层次

回到 `linalg.add`。在 custom 语法里，它看起来像一条“指令”：

```mlir
%0 = linalg.add ins(%A, %B : tensor<4x4xf32>, tensor<4x4xf32>) outs(%empty : tensor<4x4xf32>) -> tensor<4x4xf32>
```

但 generic form 暴露了它内部有一个 region，里面是 `arith.addf` 和 `linalg.yield`。这意味着：

- `linalg.add` 不是“一条硬件加法”，而是“一个结构化循环 + 元素级加法”的抽象；
- 它的 region 可以被 rewrite（例如把 `addf` 换成别的）；
- 它的 lowering 可以走不同路线（向量化、并行化、直接生成循环）。

同一个 op，在不同层次看，含义不同。这是 MLIR 最反直觉、也最有用的地方。

## 示例三：从 MLIR 到 LLVM IR，SSA 表示怎么变

前面已经看到：MLIR 的 block argument 在 LLVM IR 里变成 `phi`。再看一个细节：

```mlir
^bb1(%4: i64, %5: i32):  // 2 preds: ^bb0, ^bb2
```

`^bb1` 有两个参数，分别承接两个前驱传来的值。LLVM IR 没有“block 参数”，只能在块首用 `phi` 指令合成：

```llvm
2:
  %3 = phi i64 [ %8, %6 ], [ 0, %1 ]
  %4 = phi i32 [ %7, %6 ], [ 0, %1 ]
```

两个表示是等价的，但 MLIR 的形式对做数据流分析更友好：你不必扫描块内所有指令才能知道“这个块接收什么值”。

## 示例四：未知 dialect 到底怎么处理

写一个不存在的 op：

```mlir
foo.bar %x : i32
```

实测报错：

```text
error: Dialect `foo' not found for custom op 'foo.bar'
note: Available dialects: acc, affine, amdgpu, ...
```

换成 **generic 形式**，并加 `--allow-unregistered-dialect`：

```mlir
func.func @f(%x: i32) {
  %0 = "foo.bar"(%x) : (i32) -> (i32)
  return
}
```

实测输出：

```mlir
module {
  func.func @f(%arg0: i32) {
    %0 = "foo.bar"(%arg0) : (i32) -> i32
    return
  }
}
```

这说明两件事：

- **custom 语法必须由 dialect 提供 parser**，所以未注册 dialect 的 `foo.bar %x` 连解析都过不去；
- `--allow-unregistered-dialect` 只放行 **generic 形式**的未知 op，不会凭空创造语法。

这个区别在调试真实 pipeline 时很重要：看到 “Dialect not found” 先判断是“语法解析”还是“op 合法化”的问题。

## 对比：MLIR、LLVM IR、AST

| 维度 | AST | LLVM IR | MLIR |
|---|---|---|---|
| 结构 | 树 | CFG + 指令 | Operation 树 + Region 嵌套 |
| opcode | 节点类型固定 | 全局固定 | 字符串，由 dialect 定义 |
| 扩展性 | 改语言就要改 AST | 不能扩展 | 新 dialect 即扩展 |
| 抽象层 | 一层（源语言） | 一层（机器无关） | 多层共存 |
| SSA | 通常没有 | 有（phi） | 有（block argument） |
| 验证 | 类型检查器 | verifier | 每个 op 的 verifier |
| 典型用途 | 前端 | 后端优化/代码生成 | 端到端多级编译 |

一句话总结：**LLVM IR 是“一个层次的通用 IR”，MLIR 是“多个层次的通用 IR 容器”**。

## 设计决策：为什么不是直接扩展 LLVM IR？

这不是一个纯理论问题。LLVM IR 的很多优化假设“所有 op 的语义已知”：别名规则、内存模型、指令副作用、类型系统。你要往里塞一个 `linalg.add`，就得让所有既有 pass 知道它是什么、能不能重排、有没有副作用——成本极高，而且会破坏既有优化。

MLIR 的选择是：

- **不要求所有 pass 理解所有 op**：pass 只处理它认识的 dialect，其余 op 原样保留；
- **用 interface 表达“可被某框架处理”**（Ch 8）：canonicalize、bufferize、conversion 都通过接口发现能力；
- **把“层次”变成数据**：region 嵌套，而不是继承层次。

代价也要说清楚：

- **IR 可能长期处于混合状态**，需要严格的 pipeline 纪律；
- **conversion 是主要工作量**，每个 dialect 边界都要写 legality 与 type 映射；
- **调试成本更高**，因为“这段 IR 属于哪一层”不是一眼能看出的。

## 踩雷集锦

1. **以为 MLIR 是“LLVM IR 的新语法”。** 两者服务不同层次。MLIR 可以降到 LLVM IR，但它本身不是 LLVM IR 的替代品；`mlir-translate` 才是边界。

2. **以为一个 `.mlir` 文件只能用一个 dialect。** 本章的 `linalg` 例子同时用了 `func`、`tensor`、`linalg`、`arith`。混合是常态，不是错误。

3. **以为 lowering 是一步到位的。** `convert-func-to-llvm` 之后函数体里仍可能有 `arith`。conversion 按 dialect 切片，必须逐步收敛。

4. **把 custom 语法当成语义。** `linalg.add ins(...) outs(...)` 只是打印形式；generic form 才是真实结构。写测试时锁 custom 语法可以，但理解机制要看 generic form。

5. **以为 `--allow-unregistered-dialect` 能解析任意自定义语法。** 它只放行 generic 形式的未知 op；custom 语法仍然需要 dialect 的 parser。

6. **以为 `index` 就是 `i64`。** `index` 是目标相关类型；在默认 64 位配置下才降成 `i64`。换 target 或改 `index-bitwidth`，结果会变。

7. **以为 region 只属于函数。** `scf.for`、`linalg.add`、`func.func` 都有 region；任何 op 都可以带 region。这是 MLIR 可嵌套的根本原因。

## 进阶：再往深一层

- **Properties vs Attributes**：23.x 把 op 的固有字段迁移到 `properties`，属性字典更多用于“可选/可扩展”信息。Ch 2 会讲这个区分对 verifier 和 bytecode 的影响。
- **Op interfaces**：`MemoryEffectOpInterface`、`BufferizableOpInterface`、`CallOpInterface` 让框架在不认识具体 op 的情况下工作。Ch 8 的核心。
- **Multi-threading**：MLIRContext 支持多线程编译，但 pass 默认单线程遍历；并行 pass 需要显式声明。Ch 30 会讲。
- **Bytecode**：`.mlirbc` 是 MLIR 的二进制格式，加载比文本快得多；Ch 16 会用它观察 dialect 版本兼容。

## 动手练习

1. **复现四种形态**：把 `sum.mlir` 存盘，依次跑 `scf`、`scf→cf`、全降级、`mlir-translate`，确认每一步的输出与教材一致。
2. **改写成 `scf.while`**：把 `for` 改成 `while`，再降级，观察 block argument 的数量与顺序如何变化。
3. **混合 dialect**：写一个函数，同时用 `arith`、`math`、`scf`，确认 `mlir-opt` 能解析并打印。
4. **generic 练习**：用 `--mlir-print-op-generic` 打印你的混合 module，找出每个 op 的属性字典，解释 `operandSegmentSizes` 的作用。
5. **未知 dialect**：分别用 custom 语法和 generic 语法写 `foo.bar`，记录两次报错/成功的行为差异。
6. **`index` 实验**：给三个 conversion pass 都加 `index-bitwidth=32`：

   ```powershell
   & "$mlir\mlir-opt.exe" sum.mlir `
     --pass-pipeline='builtin.module(func.func(convert-scf-to-cf),convert-func-to-llvm{index-bitwidth=32},convert-cf-to-llvm{index-bitwidth=32},convert-arith-to-llvm{index-bitwidth=32},reconcile-unrealized-casts)'
   ```

   实测结果：`llvm.func @sum(%arg0: i32)`，`llvm.mlir.constant(0 : index) : i32`。只给 `convert-func-to-llvm` 加会留下 `builtin.unrealized_conversion_cast`，因为其它 pass 仍按 64 位处理——这也是 Ch 19 的伏笔。

## 本章重点整理

- MLIR 是**多级 IR 容器**：一套基础设施，多个 dialect，层次靠 region 嵌套。
- 所有东西都是 Operation；custom 语法只是打印糖，generic form 才是真实结构。
- Dialect 由 op、类型、属性、接口、验证规则组成；本机注册了 47 个。
- 混合 dialect 是合法中间态；conversion 按 dialect 切片，逐步收敛。
- `index` 是目标相关类型，默认降为 `i64`。
- MLIR 的 block argument 在 LLVM IR 里变成 `phi`。
- `--allow-unregistered-dialect` 只放行 generic 形式的未知 op。

## 自我检核

- [ ] 我能不看教材，说出 MLIR 与 LLVM IR 的三个本质差异
- [ ] 我能解释 `func.func` 的函数体为什么是 region
- [ ] 我能说出 `linalg.add` 的 region 里有什么，以及它为什么存在
- [ ] 我能解释为什么 `convert-func-to-llvm` 之后还会残留 `arith.addi`
- [ ] 我知道 `index` 不是 `i64`，并知道它什么时候变成 `i64`
- [ ] 我能解释 block argument 与 phi 的对应关系
- [ ] 我能判断一段 `.mlir` 处在哪一层、由哪些 dialect 组成

## 延伸阅读

- **[MLIR: A Compiler Infrastructure for the End of Moore's Law (arXiv:2002.11054)](https://arxiv.org/abs/2002.11054)**
  - **读哪里**：第 2–3 节（多级 IR 的动机与设计）。
  - **学什么**：为什么“一个 IR 容纳多层”能减少转换器数量。
  - **和本章的关联**：本章的四种形态就是论文论点的具体化。

- **[MLIR 文档：Understanding the IR Structure](https://mlir.llvm.org/docs/Tutorials/UnderstandingTheIRStructure/)**
  - **读哪里**：Operation/Block/Region 的关系图。
  - **学什么**：官方对 IR 结构的定义与遍历方式。
  - **和本章的关联**：Ch 2 会逐项展开。

- **[MLIR 文档：Dialects](https://mlir.llvm.org/docs/Dialects/)**
  - **读哪里**：dialect 列表与每个 dialect 的职责。
  - **学什么**：47 个 dialect 分别属于哪个抽象层。
  - **和本章的关联**：Ch 4 会做一张自己的地图。

- **[Toy Tutorial: Chapter 2](https://mlir.llvm.org/docs/Tutorials/Toy/Ch-2/)**
  - **读哪里**：Toy dialect 的定义与 `toy.print` 的语义。
  - **学什么**：一个最小 dialect 需要哪些部分。
  - **和本章的关联**：Part 2 的练习 C 会写一个同规模的 dialect。

- **[mlir/include/mlir/IR/Operation.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/IR/Operation.h)**
  - **读哪里**：`Operation` 的成员、`getRegions()`、`getProperties()`。
  - **学什么**：本章表格里的每个字段在源码里叫什么。
  - **和本章的关联**：Ch 2 的源码走读起点。

---

下一章把本章的“Operation 是唯一单元”拆开：Operation 到底由哪些字段组成，Block 和 Region 的边界怎么定义，SSA 值的作用域如何计算。我们会直接用 generic form 和源码对照。

→ [Ch 2 IR 对象模型：Operation、Block、Region、Value](./02-ir-object-model.md)