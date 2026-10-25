# Ch 29 设计取舍：MLIR 与其他 IR

> **目标**：把前 28 章的技术判断升级成选型判断。学完本章，你能回答三个问题：为什么 LLVM IR 不能直接承载高层语义、一个 op 该放进哪个 dialect、以及什么情况下根本不该用 MLIR。每个论点都尽量落到本机跑出来的 IR 上，而不是“架构上更优雅”。

## 为什么需要这个？

用 MLIR 的人迟早会撞上三个问题：

1. **“这个优化为什么不能直接在 LLVM IR 上做？”** 如果你答不出，很可能会把高层信息过早丢掉。
2. **“这个 op 该放哪个 dialect？”** 放错了，后面每个 pass 都要为这个错误买单。
3. **“我这个项目到底该不该用 MLIR？”** 不是所有编译器都值得引入一套多级 IR 基础设施。

前 28 章给了你工具，这一章给判断。判断错了，后面每一章的技术都会用来维护一个错误的结构。

## 先建立直觉：IR 是给谁看的

一个 IR 有三种消费者，它们想要的东西互相冲突：

| 消费者 | 想要什么 | 抽象层次偏高 | 抽象层次偏低 |
|---|---|---|---|
| 人 | 一眼看懂结构 | 好读 | 淹没在细节里 |
| pass | 稳定、可验证的不变量 | 优化空间大，但需要更多 pass | 优化空间小 |
| 后端 | 能直接选指令 | 要更多降级步骤 | 直接可用 |

LLVM IR 主要是**给后端看的**：类型窄、控制流平、指令少，所以后端好写，但高层语义必须先被压扁。

MLIR 的目标是**给下一层 pass 看的**：同一份 IR 里可以有多个抽象层，每层只对下一层负责。代价是“现在在哪一层”必须始终清楚。

## 核心概念一：LLVM IR 为什么装不下高层语义

四个具体缺口，每一个都能在本机复现。

**缺口一：类型系统是封闭的。** LLVM IR 的 first-class 类型只有整数、浮点、指针、向量、数组、结构体等少数几类。`memref<4xf32>`、`tensor<2x3xf32>`、`index` 都不在其中（`index` 会按目标位宽降成整数）。MLIR 的类型是开放的：任何 dialect 都能定义自己的 type（Ch 14）。这是“多级”能成立的前提——否则每换一层就要换一套 IR。

**缺口二：控制流只有 CFG。** LLVM 只有基本块和分支。“这是一个带累加器的循环”这件事，在降成基本块之后就没了。Ch 28 的 `ForLowering` 把 `iter_args` 变成 block argument，信息没有丢，但**结构**丢了。

**缺口三：指令基本是单结果。** LLVM 的一条指令产出一个值（聚合也只是一个值）。MLIR 的 op 可以多结果。`实测`（节选，外层 `func.func` 省略）：

```mlir
#map = affine_map<(d0) -> (d0)>
%0:2 = linalg.generic {indexing_maps = [#map, #map, #map], iterator_types = ["parallel"]}
    ins(%arg0 : tensor<4xf32>) outs(%arg0, %arg0 : tensor<4xf32>, tensor<4xf32>) {
    ^bb0(%in: f32, %out: f32, %out_0: f32):
      %1 = arith.addf %in, %in : f32
      %2 = arith.mulf %in, %in : f32
      linalg.yield %1, %2 : f32, f32
    } -> (tensor<4xf32>, tensor<4xf32>)
```

**缺口四：没有 region。** LLVM 函数体是一张扁平 CFG；MLIR 的 op 可以带嵌套 region，region 里还能有自己的 block argument。上面那段 `linalg.generic` 的 body 就是一个 region。没有 region，就没法在“一条 op”里表达一个完整的计算。

**代价也要说清楚。** 开放类型 + 开放 op + region，换来的是规模爆炸。本机实测：

- `mlir/include/mlir` 下 336 个 `.td` 文件里，按 `^def ... : ...Op<` 粗算有 **2063 个 ODS op 定义**；
- 其中 `arith` 53 个、`linalg` 81 个、`llvm` dialect 248 个；
- `mlir-opt --help` 里以 `--` 开头的选项有 **955 条**（含每个 pass 的子选项）。

调试成本也随之上升：一个 bug 可能在三层 pipeline 的任意一层，`--mlir-print-ir-after-all` 的输出会长到需要靠 pass 名过滤。

## 核心概念二：AST 是树，IR 是图

AST 的结构是“一个节点一个父”。这带来两个限制：

- **共享靠复制**：同一个子表达式出现两次，AST 里就是两棵子树（除非你额外做 hash-consing）。
- **信息沿父子传递**：你看不到“这个值在别处还被谁用”。

IR 的 use-def 是图：一个 `Value` 可以有多个 user，一个 op 的 operand 可以来自任意支配它的位置。优化需要的正是这张图：

| 优化 | 需要什么 | AST 上为什么难 |
|---|---|---|
| CSE | 判断两个表达式等价且互相支配 | 树里没有支配关系 |
| LICM | 把指令移到循环外且不破坏支配 | AST 没有循环的 CFG 表示 |
| 别名分析 | 追踪谁写了同一块内存 | 树里没有“同一块内存”的全局视图 |
| 内联后再优化 | 跨函数重排 | AST 边界把作用域切死了 |

“一遍 AST 走完”能做什么？常量折叠、局部改写、语法糖消除。不能做什么？任何需要跨语句、跨基本块、跨函数的判断。**一旦你的优化需要问“这个值还被谁用”，你就需要一个图 IR。**

MLIR 的位置很特别：它既有 SSA 的图（use-def、支配），又保留了 region 的结构（`scf.for`、`linalg.generic`）。这使它能在结构化信息还没被拆掉之前做高层优化。

## 核心概念三：dialect 边界怎么划

三条判据，按顺序问：

1. **同一抽象层次？** 两种类型能不能互相解释，而不需要先做类型转换。
2. **同一组不变量？** 能不能写一个 verifier 同时覆盖它们，而不需要“看情况”。
3. **同一批 pass 消费？** 优化能不能在这个 dialect 内闭环，而不是每个 pass 都要跨两个世界。

用 `arith` / `linalg` / `memref` 检验：

| dialect | 类型 | 不变量 | 主要消费者 |
|---|---|---|---|
| `arith` | 标量、向量 | 无内存、无别名、纯函数式 | 常量折叠、强度削减、向量化 |
| `linalg` | tensor/memref + `indexing_maps` | 迭代空间与操作数形状一致 | tiling、fusion、vectorization |
| `memref` | 指针 + offset + stride | 别名与生命周期可追踪 | bufferization、循环优化、降级 |

这三层恰好对应三种“世界”：纯值、结构化张量、地址。它们之间的转换是显式的（bufferization、lowering），不是隐式的。

**反例一：把 tensor 和 memref 塞进一个 dialect。** 每个 pass 都要写两套逻辑，verifier 也无法统一（tensor 没有别名，memref 有）。bufferization 之所以要单独成章（Ch 20），就是因为这个边界必须显式化。

**反例二：把 `linalg.matmul` 和 `arith.addi` 放进同一个 dialect。** 抽象层次差太远：一个 pass 的 legality 无法同时覆盖“结构化张量运算”和“标量加法”，结果只能是每个 pass 都加 `if (isa<...>)`。

**判断口诀**：如果一个 op 的 verifier 需要知道另一个 dialect 的语义，它们大概率不该在同一个 dialect 里。

## 底层机制：降级时到底丢掉了什么

降级不是“翻译”，是“有损压缩”。把高层的每个信息列出来，看它在 LLVM IR 里还剩什么：

| 高层信息 | 到 LLVM IR 变成 | 丢了什么 |
|---|---|---|
| `tensor<2x3xf32>` 的静态形状 | 指针 + 运行时长度/步长 | 编译期形状约束（除非完全展开） |
| `scf.for` 的 `iter_args` | block argument + 回边 | 循环结构本身 |
| `linalg` 的 `indexing_maps` | 展开后的下标算术 | 迭代空间语义 |
| `memref` 的别名信息 | 裸指针 | 可验证性 |
| dialect 的 verifier 不变量 | 无 | 只能在降级前检查 |

这张表解释了为什么“先降到 LLVM IR 再优化”是个坏主意：你丢掉的东西，正是高层优化需要的。**降级要在该做的层次做完，而不是尽早做。**

## 示例一：`memref` 在 LLVM IR 里变成 6 个参数

输入：

```mlir
func.func @sum(%a: memref<4xf32>, %i: index) -> f32 {
  %v = memref.load %a[%i] : memref<4xf32>
  return %v : f32
}
```

```powershell
& "$mlir\mlir-opt.exe" .\memref.mlir --convert-to-llvm -o .\ll.mlir
& "$mlir\mlir-translate.exe" .\ll.mlir --mlir-to-llvmir
```

`实测`：

```llvm
define float @sum(ptr %0, ptr %1, i64 %2, i64 %3, i64 %4, i64 %5) {
  %7 = insertvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } poison, ptr %0, 0
  %8 = insertvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } %7, ptr %1, 1
  %9 = insertvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } %8, i64 %2, 2
  %10 = insertvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } %9, i64 %3, 3, 0
  %11 = insertvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } %10, i64 %4, 4, 0
  %12 = extractvalue { ptr, ptr, i64, [1 x i64], [1 x i64] } %11, 1
  %13 = getelementptr inbounds nuw float, ptr %12, i64 %5
  %14 = load float, ptr %13, align 4
  ret float %14
}
```

一个 `memref<4xf32>` 参数变成了 **6 个标量参数**（分配指针、对齐指针、offset、1 个 size、1 个 stride……再加上索引）。二维 memref 是 7 个参数；三个二维 memref 就是 21 个（示例二的函数签名）。

这不是实现偷懒，是 LLVM 的类型系统只能表达这些。MLIR 把 `memref` 的信息保留到最后一刻，正是为了让 bufferization 和循环优化还能看见形状。

## 示例二：`linalg` → `scf` → `llvm`，三层各干什么

**第一层，`linalg` 只描述“算什么”。**

```mlir
func.func @matmul_buf(%A: memref<2x3xf32>, %B: memref<3x4xf32>, %C: memref<2x4xf32>) {
  linalg.matmul ins(%A, %B : memref<2x3xf32>, memref<3x4xf32>) outs(%C : memref<2x4xf32>)
  return
}
```

**第二层，`--convert-linalg-to-loops` 把它展开成循环。** `实测`（节选，省略 `func.func` 外壳与常量定义）：

```mlir
scf.for %arg3 = %c0 to %c2 step %c1 {
  scf.for %arg4 = %c0 to %c4 step %c1 {
    scf.for %arg5 = %c0 to %c3 step %c1 {
      %0 = memref.load %arg0[%arg3, %arg5] : memref<2x3xf32>
      %1 = memref.load %arg1[%arg5, %arg4] : memref<3x4xf32>
      %2 = memref.load %arg2[%arg3, %arg4] : memref<2x4xf32>
      %3 = arith.mulf %0, %1 : f32
      %4 = arith.addf %2, %3 : f32
      memref.store %4, %arg2[%arg3, %arg4] : memref<2x4xf32>
```

**第三层，`--convert-to-llvm` 再把这些变成 LLVM 的 load/store 和基本块。**

先别急着跑。单独用 `--convert-to-llvm` 并不会动 `scf.for`。对一个 `scf.for` 求和函数实测（节选，`...` 为省略）：

```mlir
...
%0 = builtin.unrealized_conversion_cast %arg0 : i64 to index
%7 = scf.for %arg1 = %4 to %0 step %6 iter_args(%arg2 = %1) -> (i32) {
  %8 = llvm.add %arg2, %2 : i32
  scf.yield %8 : i32
}
llvm.return %7 : i32
```

`scf.for` 还在，`index` 两边挂满了 `unrealized_conversion_cast`。因为 `convert-to-llvm` 是**接口驱动**的，而 `scf` 没有到 LLVM 的直连接口——它必须先降到 `cf`。补上一步就通了：

```powershell
& "$mlir\mlir-opt.exe" .\matmul_buf.mlir `
  --convert-linalg-to-loops --convert-scf-to-cf --convert-to-llvm -o .\matmul_ll.mlir
& "$mlir\mlir-translate.exe" .\matmul_ll.mlir --mlir-to-llvmir
```

`实测`（节选）：

```llvm
define void @matmul_buf(ptr %0, ptr %1, i64 %2, i64 %3, i64 %4, i64 %5, i64 %6, ptr %7, ptr %8, i64 %9, i64 %10, i64 %11, i64 %12, i64 %13, ptr %14, ptr %15, i64 %16, i64 %17, i64 %18, i64 %19, i64 %20) {
  ...
  br label %43

43:                                   ; preds = %79, %21
  %44 = phi i64 [ %80, %79 ], [ 0, %21 ]
  %45 = icmp slt i64 %44, 2
  br i1 %45, label %46, label %81

46:                                   ; preds = %43
  br label %47
```

三个观察：

- 函数签名是 **21 个参数**（三个二维 memref，每个 7 个）。
- MLIR 的 block argument 在 LLVM 文本里变成了 **`phi`**——LLVM IR 没有 block argument，只有 phi。
- 三层 pipeline 一步都不能少：`linalg` → `scf` → `cf` → `llvm`。

注意一个容易踩的边界：**同一个 `linalg.matmul` 如果操作数是 `tensor`，`--convert-linalg-to-loops` 不会动它**。`实测` 输出与输入逐字相同。因为这个 pass 针对的是 buffer 语义；张量版要先 bufferize（Ch 20）。这就是“每层都有前置条件”的具体样子。

## 示例三：`--convert-to-llvm` 直接吃 `linalg` 会怎么失败

```powershell
& "$mlir\mlir-opt.exe" .\matmul.mlir --convert-to-llvm
```

`实测`：

```text
matmul.mlir:2:8: error: expected add/mul op in the body
note: see current operation:
%0 = "linalg.matmul"(%arg0, %arg1, %arg2) <{indexing_maps = [affine_map<(d0, d1, d2) -> (d0, d2)>, affine_map<(d0, d1, d2) -> (d2, d1)>, affine_map<(d0, d1, d2) -> (d0, d1)>], operandSegmentSizes = array<i32: 2, 1>}> ({
^bb0(%arg3: f32, %arg4: f32, %arg5: f32):
  %1 = "llvm.fmul"(%arg3, %arg4) <{fastmathFlags = #llvm.fastmath<none>}> : (f32, f32) -> f32
  %2 = "llvm.fadd"(%arg5, %1) <{fastmathFlags = #llvm.fastmath<none>}> : (f32, f32) -> f32
```

这里有两个信息量很大的细节：

- `--convert-to-llvm` 是**接口驱动 + partial conversion**：`ConvertToLLVMPass.cpp:120` 只把 LLVM dialect 标成 legal。`linalg` 没有实现 `ConvertToLLVMPatternInterface`，但 `arith` 实现了，所以 body 里的 `arith.addf`/`arith.mulf` 先被改成了 `llvm.fadd`/`llvm.fmul`。
- 随后 `linalg` 自己的 verifier 报错：`LinalgInterfaces.cpp:576` 返回的就是 `expected add/mul op in the body`。它只认 add/mul，body 已经变成了 LLVM op。
- 失败发生在**部分改写之后**：父 op 的 body 已经被改了，父 op 却过不了自己的 verifier。这说明“一条 pass 搞定所有层”不成立，你必须按层组织 pipeline。

这也是 Ch 17 那条规则的另一种说法：**partial conversion 的成功不等于降级完成**。这里 partial 直接失败了，而且失败点不是 legality 判定，是父 op 被改完 body 之后过不了自己的 verifier。

## 对比与取舍：MLIR vs 传统多级 IR

下表是设计对照，不是本机实测；每条都能在对应项目的公开文档里找到依据。

| 维度 | LLVM IR | GIMPLE / RTL | TVM TIR | Sea of Nodes | FIRRTL | MLIR |
|---|---|---|---|---|---|---|
| 抽象层次可扩展 | 否（固定一层） | 否（固定两层） | 有限（单层 + 少量原语） | 否 | 否（硬件层） | **是（dialect）** |
| SSA 形式 | 是 | GIMPLE 是，RTL 否 | 语句式，非通用 SSA | 类 SSA（数据+控制边） | last-connect 连线（非软件 SSA） | 是（SSA + region） |
| 结构化控制流 | 否（只有 CFG） | GIMPLE 有部分结构化，RTL 无 | 是（循环/块） | 否（控制是边） | 无软件控制流 | **是（scf/affine）** |
| 类型系统 | 固定 | 绑定 C/C++ | 张量/缓冲 | 由语言决定 | 位宽/硬件类型 | **开放（每 dialect 自定义）** |
| 生态 | 极大（后端/工具链） | 仅 GCC | TVM 内部 | JVM/JS JIT | Chisel/CIRCT | 快速增长（IREE/Torch-MLIR/CIRCT） |

读这张表的正确方式：**MLIR 不是每一格都赢**。它的优势集中在“可扩展”和“结构化”两列；在生态成熟度、后端质量、编译速度上，LLVM IR 仍然领先。MLIR 的定位是“LLVM IR 之前的那几层”，不是替代它。

### 什么时候不该用 MLIR

四条明确判断，命中两条以上就该慎重：

1. **语言小、目标单一。** 如果只有一门小语言、一个后端，直接 AST → LLVM IR 就够了。多级 IR 的收益来自“多个抽象层要被多个 pass 消费”，没有这个需求，收益是零。
2. **团队不熟 MLIR。** MLIR 的坑集中在 conversion/legality、bufferization、type converter，不在语法。学习曲线是真实的，出错时的调试成本也高（Ch 17-21 整段都在讲这个）。
3. **编译时间极度敏感。** op 注册、pattern 匹配、pass pipeline 都有固定开销。对“几毫秒就要出结果”的场景，手写 AST pass 可能更合适。
4. **只需要一次遍历的检查器。** linter、格式化器、简单的 DSL 校验，用 AST + visitor 就够，引入 dialect 是把简单问题复杂化。

压缩成一张判断表：

| 你的情况 | 判断 | 更合适的做法 |
|---|---|---|
| 一门小语言、一个 CPU 后端 | 不该 | AST → LLVM IR，两层足够 |
| 多前端、多后端、多层优化 | 该 | MLIR，收益来自层间复用 |
| 硬件/加速器 DSL | 通常该 | MLIR + 自定义 dialect（CIRCT 路线） |
| 只做 lint / 格式化 / 语法检查 | 不该 | AST visitor，一遍走完 |
| 极低延迟的 JIT | 慎重 | 先量固定开销，再决定要不要建 IR |

反过来说，**该用 MLIR 的信号**是：你已经有不止一层抽象、每层都有独立的优化、并且希望这些层能共存而不是互相转换。

### 演化与兼容：版本化、bytecode、breaking change

MLIR 的 bytecode 有自己的版本号。先看命令：

```powershell
& "$mlir\mlir-opt.exe" .\matmul_buf.mlir --emit-bytecode -o .\matmul.mlirbc
& "$mlir\mlir-opt.exe" .\matmul_buf.mlir --emit-bytecode --emit-bytecode-version=7
```

`实测`（去掉 PowerShell 的 stderr 包装）：

```text
文件头（前 8 字节）：4D 4C EF 52 0D 4D 4C 49      # "ML\xEFR"
error: unsupported version requested 7, must be in range [0, 6]
```

`--emit-bytecode-version=0.0.0` 会被拒绝，提示 `only integer is supported`。也就是说，版本只是一个整数，不是语义化的兼容承诺。

dialect 自己也可以带版本。`源码依据`，`mlir/include/mlir/Bytecode/BytecodeImplementation.h:27-35`：

```cpp
// Dialect Version Interface.
class DialectVersion {
public:
  virtual ~DialectVersion() = default;
};
```

读写接口可以通过 `getDialectVersion()`（`:56-62`）拿到它，从而在 bytecode 里做自己的迁移。注意这是**可选**的：不实现就等价于“没有版本”。

breaking change 的成本分三块：

- **文本 IR**：没有版本概念。删 op、改 assemblyFormat 会直接让旧 `.mlir` 解析失败。
- **bytecode**：有全局版本，但版本只覆盖“格式”，不覆盖“你删了一个 op”。旧文件里的 op 名找不到就是找不到。
- **pass pipeline**：flag 改名会让脚本失效。23.1.4 的 `--help` 里只有 `--finalize-memref-to-llvm` 和 `--convert-to-llvm`，没有 `--convert-memref-to-llvm`。

工程建议：**加 op 比改 op 便宜，改 op 比删 op 便宜**。真要删，先保留一个 deprecated 别名，至少跨一个 release。

## 踩雷集锦

1. **“先降到 LLVM IR，再在 LLVM 上做高层优化”。** 降级是有损的（见底层机制表），丢掉的正是你要的信息。
2. **把不同抽象层次的 op 塞进一个 dialect。** 结果是每个 pass 都要写 `if (isa<...>)`，legality 永远说不清。
3. **以为 `--convert-to-llvm` 能一把梭。** 示例三证明它会先改 region body 再失败。
4. **以为 `linalg` 的 lowering 对 tensor 和 memref 一样。** `convert-linalg-to-loops` 只动 buffer 语义；张量要先 bufferize。
5. **把 LLVM IR 的类型系统当成“够用”。** `memref` 变成 6 个参数不是细节，是设计边界。
6. **用 bytecode 版本号当兼容承诺。** 它只管格式，不管 op 语义。
7. **以为 dialect 越多越“模块化”。** 每个边界都是一次转换成本；没有独立不变量和独立 pass 的 dialect，只是多了一层 cast。

## 进阶：再往深一层

- **读 MLIR 论文的动机章节**：arXiv:2002.11054 第 2-3 节把“为什么需要多级 IR”讲得比任何博客都清楚。
- **看 CIRCT**：它把 FIRRTL 搬进 MLIR，是“硬件 IR + 多级”的活样本；也是 dialect 边界划分的真实案例。
- **看 IREE 的 pipeline**：一个生产级 MLIR 使用者怎么组织 flow/halo/stream/hal 四层，比 toy 例子更能说明“层次”是什么。
- **对比 `--convert-to-llvm` 与 `--convert-arith-to-llvm`**：前者接口驱动、后者 pattern 驱动，失败模式完全不同（Ch 17/21）。

## 动手练习

统一约定：在 `.scratch\mlir-29\` 下新建文件，`$mlir` 指向本机 bin 目录。

### 练习 1：数清 memref 的参数

**任务**：把一维、二维 memref 函数降到 LLVM，数清每个 memref 占几个参数。
**用例**：(a) `memref<4xf32>`；(b) `memref<2x3xf32>`；(c) 三个二维 memref。
**步骤**：1) 写三个函数；2) `--convert-to-llvm`；3) `--mlir-to-llvmir`；4) 数 `define` 行里的参数。

<details><summary>参考解答</summary>

(a) 描述符 5 个，加上额外的 `index` 参数共 6 个；(b) 7 个；(c) 21 个。描述符规律是 `2 + 1 + rank + rank`：分配指针、对齐指针、offset，再是 rank 个 size 和 rank 个 stride。

</details>

**卡点**：把 `index` 参数也算进 memref 描述符；它不属于描述符。

### 练习 2：复现并解释 `--convert-to-llvm` 的失败

**任务**：对一个 `linalg.matmul`（tensor 版）跑 `--convert-to-llvm`，保存完整报错并解释 note 里的 IR。
**用例**：(a) 只有 `linalg.matmul`；(b) 先 `--convert-linalg-to-loops` 再转换；(c) 先 bufferize 再转换。
**步骤**：1) 跑三个用例；2) 记录退出码；3) 读 note 里 `llvm.fmul` 的来源；4) 解释为什么 (b) 对 tensor 版不起作用。

<details><summary>参考解答</summary>

(a) 失败：`expected add/mul op in the body`，note 显示 body 已被改写成 `llvm.fmul`/`llvm.fadd`。(b) 仍失败——`convert-linalg-to-loops` 不处理 tensor 版。(c) 加 `--one-shot-bufferize='bufferize-function-boundaries'` 后按 `convert-linalg-to-loops → convert-scf-to-cf → convert-to-llvm` 走，得到 21 个参数的 `llvm.func`。省略 `bufferize-function-boundaries` 时退出码仍是 0，但函数签名还是 tensor、`func.func` 也还在——退出码 0 不等于降级完成。

</details>

**卡点**：以为报错来自 `linalg.matmul` 本身；其实失败点在“body 已改、op 改不了”的半完成状态。

### 练习 3：给三个 op 划 dialect 边界

**任务**：给 `arith.addi`、`linalg.matmul`、`memref.load` 各写一条“属于这个 dialect 的理由”，再写一条“不该放一起”的反例。
**用例**：(a) 三者各自的类型；(b) 三者的 verifier 不变量；(c) 三者各自的消费者 pass。
**步骤**：1) 填三列表；2) 用“同一抽象层次/同一组不变量/同一批 pass”三条判据逐条打分；3) 指出哪两个放一起会破坏哪条判据。

<details><summary>参考解答</summary>

`arith` 是纯值、无内存；`linalg` 是结构化张量、有迭代空间；`memref` 是地址与别名。把 `arith` 和 `linalg` 合并会破坏“同一抽象层次”（标量 vs 结构化）；把 `linalg` 和 `memref` 合并会破坏“同一组不变量”（形状 vs 别名）。

</details>

**卡点**：只看“都属于数值计算”就合并，忽略不变量差异。

### 练习 4：bytecode 版本实验

**任务**：生成不同版本的 bytecode，找出本机支持的范围，并解释版本号与 op 兼容性的区别。
**用例**：(a) 默认版本；(b) `--emit-bytecode-version=0`；(c) `--emit-bytecode-version=7`。
**步骤**：1) 各生成一次；2) 记录退出码与文件头；3) 读回 (a) 验证 roundtrip；4) 用一句话说明“版本 6 能读”为什么不等于“旧 op 还在”。

<details><summary>参考解答</summary>

本机范围是 `[0, 6]`，7 会报 `unsupported version requested 7`。文件头固定以 `4D 4C EF 52` 开头。版本号只保证格式可读；如果一个 op 被删除，旧 bytecode 里的 op 名仍然找不到。

</details>

**卡点**：把 `--emit-bytecode-version` 当成“选择 MLIR 版本”；它只影响序列化格式。

### 练习区自我检核

- [ ] 我能说出 memref 描述符的参数构成
- [ ] 我能解释 `--convert-to-llvm` 失败时 note 里的 IR 意味着什么
- [ ] 我能用三条判据判断两个 op 该不该在同一个 dialect

## 本章重点整理

- LLVM IR 的四个缺口：封闭类型、只有 CFG、基本单结果、没有 region。这不是缺陷，是它为后端优化的取舍。
- MLIR 的代价：op 数量爆炸（2063 个 ODS 定义）、pass 组合复杂（955 条选项）、调试要始终知道在哪一层。
- AST 是树，IR 是图；需要“这个值还被谁用”的优化，必须用图 IR。
- dialect 边界的三条判据：同一抽象层次、同一组不变量、同一批 pass 消费。
- 降级是有损压缩；不要为了“统一”而过早降到 LLVM IR。
- bytecode 有全局版本（本机 `[0, 6]`），但版本不等于 op 兼容。

## 自我检核

- [ ] 我能用本机的 `memref` 例子说明“类型系统封闭”具体意味着什么
- [ ] 我能说出 `linalg.generic` 的两个结果在 LLVM IR 里会变成什么
- [ ] 我能解释为什么 `--convert-linalg-to-loops` 不动 tensor 版
- [ ] 我能用三条判据给一个自定义 op 选 dialect
- [ ] 我能列出“不该用 MLIR”的四种场景
- [ ] 我能解释 bytecode 版本号与 breaking change 的关系
- [ ] 我能说出 CIRCT 为什么适合用 MLIR 承载 FIRRTL

## 延伸阅读

- **[MLIR: A Compiler Infrastructure for the End of Moore's Law (arXiv:2002.11054)](https://arxiv.org/abs/2002.11054)**
  - **读哪里**：第 2-3 节（动机与多级 IR 论证）。
  - **学什么**：dialect 组合的设计目标，以及作者如何论证“一套基础设施服务多层”。
  - **关联**：本章核心概念一、三的理论版本。

- **[MLIR: Scaling Compiler Infrastructure for Domain Specific Computation (CGO 2021)](https://doi.org/10.1109/CGO51591.2021.9370308)**
  - **读哪里**：系统设计、dialect 生态两节。
  - **学什么**：正式论文版的工程取舍与案例。
  - **关联**：对比与取舍一节的依据。

- **[LLVM Developers' Meeting 的 MLIR 演讲](https://llvm.org/devmtg/)**
  - **读哪里**：近年的 MLIR 进展、IREE/CIRCT 实战场次。
  - **学什么**：真实项目怎么划 dialect 边界、怎么组织 pipeline。
  - **关联**：进阶一节的入口。

- **[MLIR 文档：Dialects](https://mlir.llvm.org/docs/Dialects/)**
  - **读哪里**：Dialect 定义、dialect 注册两节。
  - **学什么**：官方对“什么是 dialect”的边界定义。
  - **关联**：核心概念三的文档版。

- **[mlir/include/mlir/Bytecode/BytecodeImplementation.h](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/include/mlir/Bytecode/BytecodeImplementation.h)**
  - **读哪里**：`DialectVersion`（27-35）、`getDialectVersion`（56-62）。
  - **学什么**：dialect 版本化在 bytecode 层的钩子。
  - **关联**：演化与兼容一节的源码依据。

---

下一章换一个维度：不谈“选什么”，谈“跑得多快、能撑多大”。`MLIRContext`、storage uniquing、pass 复杂度，都会在那章变成可测量的东西。

→ [Ch 30 性能与规模](./30-performance-and-scale.md)
