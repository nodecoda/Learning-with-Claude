# Ch 22 — Bufferization：One-Shot 分析与 in-place 决策

> **目标**：理解 One-Shot Bufferize 的“两阶段 + 全函数”契约；能读懂 `__inplace_operands_attr__` 与 `C_i[CONFL-WRITE]` 注解；能判断一次 `memref.copy` 为什么被插入；能给一个自定义 op 接上 `BufferizableOpInterface`。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance`；`mlir-opt` 带 `-one-shot-bufferize`、`-buffer-deallocation-pipeline`、`-convert-bufferization-to-memref`。

> **前置**：Ch 19–21 的 conversion/legality 只是背景。Bufferization 不走 dialect conversion driver，它有自己的 driver 与 analysis。

## 22.1 为什么 bufferization 是“全函数契约”

tensor 是值语义，memref 是引用语义。把 tensor 换成 memref 时，一个局部决定会改变整个函数的语义：

- 如果 `%r = tensor.insert ...` 原地写进 `%t` 的 buffer，而 `%t` 后面还要被读，结果就错了；
- 如果每个写都分配新 buffer，语义正确但性能崩掉；
- 如果函数参数是 tensor，bufferize 之后它变成 memref，调用者必须知道这块内存的布局与所有权。

所以 bufferization 不是“逐 op 重写”，而是**先对整个函数做一次别名/读写分析，再统一重写**。这就是 One-Shot Bufferize 的设计（`mlir/docs/Bufferization.md:30`）：

| 属性 | 含义 |
|---|---|
| Monolithic | 一个 pass 干完全部 |
| Extensible | 通过 `BufferizableOpInterface` 扩展 |
| Whole-function | 分析范围是整个函数，不是单个 op |
| 2-Phase | 先分析、后重写 |
| Greedy | 逐个 op 决定 in-place 与否 |
| Modular | 分析可替换（`AnalysisState`） |

维护者最常见的问题不是“某个 op 没降”，而是“为什么这里多了一次 copy”。答案几乎总在分析阶段。

## 22.2 先建立直觉：两层世界与两阶段

```
             tensor 世界（值语义）                    memref 世界（引用语义）
    ┌────────────────────────────────┐      ┌────────────────────────────────┐
    │ %a : tensor<4x4xf32>           │      │ %a : memref<4x4xf32>           │
    │ %b = linalg.matmul ...         │      │ linalg.matmul ... outs(%buf)   │
    │ return %b                      │      │ return %buf                    │
    └────────────────────────────────┘      └────────────────────────────────┘
                    │                                      ▲
                    │        ┌───────────────────┐         │
                    └───────▶│  两阶段 bufferize │─────────┘
                             │                   │
                  ┌──────────┴─────────┐   ┌─────┴──────────────────┐
                  │ 1. analysis        │   │ 2. rewrite             │
                  │ 建 alias/equiv set │   │ 按决定插 copy / 改 op   │
                  │ 决定 in-place      │   │ 生成 memref IR          │
                  └────────────────────┘   └────────────────────────┘
```

关键点：**分析结果可以单独观察**。`test-analysis-only` 会只跑第一阶段，把决定写成属性挂回 IR，不改变任何类型。

```
   analysis-only 的输出（还是 tensor IR，但多了注解）
   ┌──────────────────────────────────────────────────────────────┐
   │ %1 = linalg.fill {__inplace_operands_attr__ = ["none","true"]}│
   │ %2 = linalg.matmul {__inplace_operands_attr__ = ["true",...]} │
   └──────────────────────────────────────────────────────────────┘
```

## 22.3 Two-Phase 的具体分工

`mlir/include/mlir/Dialect/Bufferization/Transforms/OneShotAnalysis.h` 里两个核心类型：

- `OneShotBufferizationOptions`（:29）：所有开关；
- `OneShotAnalysisState`（:71）：分析状态，`isInPlace(OpOperand&)`（:138）是重写阶段唯一要问的问题。

入口函数（`OneShotAnalysis.h:329` 与 `:334`）：

```cpp
// 只做分析：决定写进 state
LogicalResult analyzeOp(Operation *op, OneShotAnalysisState &state,
                        BufferizationStatistics *statistics = nullptr);

// 分析 + 插 copy + 重写
LogicalResult runOneShotBufferize(Operation *op,
                                  const OneShotBufferizationOptions &options,
                                  BufferizationState &state,
                                  BufferizationStatistics *statistics = nullptr);
```

`bufferizeOp`（`Bufferize.cpp:281`）是更低层的“只重写”入口，`copyBeforeWrite` 模式也走它。

`runOneShotBufferize` 的真实主干（`OneShotAnalysis.cpp:1681`，去掉 assert 与注释）：

```cpp
if (options.copyBeforeWrite) {
  // Copy buffer before each write. No analysis is needed.
} else {
  // 1+2. 分析 + 在 tensor 层插入 copy；testAnalysisOnly 时在这里直接返回
  if (failed(insertTensorCopies(op, options, state, statistics)))
    return failure();
  if (options.testAnalysisOnly)
    return success();
}
// 3. 重写
return bufferizeOp(op, options, state, statistics);
```

两个容易看漏的点：

- 分析和 copy 插入都在 `insertTensorCopies`（`TensorCopyInsertion.cpp:20`）里完成：内部调 `analyzeOp`，开启函数边界时改调 `analyzeModuleOp`；
- `testAnalysisOnly` 的返回点在 `bufferizeOp` **之前**，所以“只注解”真的一个类型都不改。

这也是为什么 `print-conflicts` 能看到 `CONFL-WRITE` 注释——copy 插入阶段手里就有每条冲突的三元组。


## 22.4 `BufferizableOpInterface` 的五个必答问题

接口定义在 `mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td`。真正必须实现的是下面几个（行号为本树）：

| 方法 | 行 | 回答什么问题 |
|---|---|---|
| `bufferizesToMemoryRead(OpOperand&, state)` | 54 | 这个 operand 会被读吗？ |
| `bufferizesToMemoryWrite(OpOperand&, state)` | 82 | 这个 operand 会被写吗（若 in-place）？ |
| `getAliasingValues(OpOperand&, state)` | 284 | 这个 operand 可能与哪些 result 共享 buffer？ |
| `getAliasingOpOperands(Value, state)` | 356 | 这个 result 可能别名到哪些 operand？ |
| `bufferize(RewriterBase&, ...)` | 433 | 真正把 op 改写成 memref 形式 |

另外两个常用的可选钩子：

- `mustBufferizeInPlace`（:218）：即使有冲突也必须 in-place（例如 `tensor.extract` 的 source 不被写，但某些 op 有强制语义）；
- `isWritable(Value, state)`（:460）：这块 buffer 可写吗？`arith.constant` 产生的全局 buffer 永远不可写，这是“非冲突型 copy”的来源。

接口注释里有一句维护者必须记住的话（`.td:19`）：

> All "bufferizesTo*" and "getAliasing*" interface methods must be implemented conservatively.

保守的意思是：**不确定就返回“可能读/可能写/可能别名”**。漏报一个别名，analysis 会认为可以 in-place，生成错误代码；多报一个别名，只是多一次 copy。前者是正确性 bug，后者是性能损失。

### 一个最容易过时的知识点

`mlir/docs/Bufferization.md:352` 仍然写着 `getAliasingOpResult`，但代码里已经是 `getAliasingValues`。照着文档写会编译失败。这类 doc drift 在 bufferization 这种快速演进的子系统里很常见——**以 `.td` 为准**。

## 22.5 边界：`to_tensor` / `to_buffer` 与 unknown ops

默认情况下，One-Shot Bufferize 遇到“有 tensor 语义但不实现接口”的 op 会直接失败：

```text
error: op was not bufferized
```

加 `allow-unknown-ops` 之后，它会在边界插入一对 op：

```mlir
%t = bufferization.to_tensor %m : memref<4xf32, strided<[?], offset: ?>> to tensor<4xf32>
%r = "foo.bar"(%t) : (tensor<4xf32>) -> tensor<4xf32>
%m2 = bufferization.to_buffer %r : tensor<4xf32> to memref<4xf32, strided<[?], offset: ?>>
```

这就是“部分 bufferize”的形态：未知 op 留在 tensor 世界，边界用 `to_tensor`/`to_buffer` 连接。

注意 `to_buffer` 的类型（`strided<[?], offset: ?>`）是**故意最动态**的。文档（`Bufferization.md:290`）解释了原因：未来另一个 pass 可能用任意布局 bufferize 这个 op，只有完全动态的布局才能保证兼容。想要静态布局，要么让整个 IR 都可 bufferize，要么用 `unknown-type-conversion=identity-layout-map` 显式承担风险。

## 22.6 RaW 冲突与 copy 插入

One-Shot 处理的核心冲突是 RaW（Read-after-Write 的反向视角）：一个 tensor 定义之后，有人想写它的 buffer，但还有人要读旧值。

```
    %t = def                    ← 定义（DEF）
    %w = write(%t)              ← 若 in-place，会覆盖 %t 的 buffer（CONFL-WRITE）
    %r = read(%t)               ← 还要读旧值（READ）
                 │
                 ▼
        三者构成一条 RaW 冲突 → 给 %w 插一次 memref.copy
```

`print-conflicts` 会把三元组直接写进 IR：

```mlir
%inserted = tensor.insert %arg2 into %arg0[%arg1]
    {"C_0[CONFL-WRITE: 1]", __inplace_operands_attr__ = ["none", "false", "none"]}
```

`C_0` 是冲突编号，`DEF` / `CONFL-WRITE` / `READ` 分别标在三个 op 上，数字是 operand/result 序号。这套注解是维护 bufferization 时最有用的调试工具。

## 22.7 函数边界与 deallocation

### 函数边界默认不 bufferize

`bufferize-function-boundaries` 是显式开关，因为函数图 bufferization 有已知限制（递归调用不支持，见 `Bufferization.md:279`）。不开时，函数参数/返回值保持 tensor，内部用 `to_buffer`/`to_tensor` 过渡。

### deallocation 是另一个 pass

One-Shot Bufferize **不负责释放**。它只产生 `memref.alloc`，释放交给 ownership-based 的 `-buffer-deallocation-pipeline`（`mlir/docs/Bufferization.md:24` 明确说旧的 buffer deallocation pass 已废弃，可能泄漏）。

实测对比（`dealloc.mlir` 见 22.9 实验 7）：

```bash
$T/bin/mlir-opt dealloc.mlir -one-shot-bufferize=bufferize-function-boundaries
$T/bin/mlir-opt dealloc.mlir -one-shot-bufferize=bufferize-function-boundaries \
    -buffer-deallocation-pipeline
```

第二条命令的输出比第一条多一行：

```text
    memref.dealloc %alloc : memref<4xf32>
```

**忘记加 deallocation pipeline 是最常见的“功能对、内存漏”问题**，而且它不会让任何测试失败。

## 22.8 源码落点

| 文件 | 行 | 读什么 |
|---|---|---|
| `mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td` | 14 | 接口定义与“必须保守”的注释 |
| 同上 | 54 / 82 | 读/写判定 |
| 同上 | 284 / 356 | 别名查询的双向版本 |
| 同上 | 433 | `bufferize` 重写入口 |
| 同上 | 460 | `isWritable`（非冲突 copy 的来源） |
| `mlir/include/mlir/Dialect/Bufferization/Transforms/OneShotAnalysis.h` | 29 | `OneShotBufferizationOptions` |
| 同上 | 71 / 138 | `OneShotAnalysisState` / `isInPlace` |
| 同上 | 329 / 334 | `analyzeOp` / `runOneShotBufferize` |
| `mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.h` | 539 | `AnalysisState::isInPlace` 默认实现 |
| `mlir/lib/Dialect/Bufferization/Transforms/OneShotAnalysis.cpp` | 1681 | `runOneShotBufferize` |
| 同上 | 1695 | `insertTensorCopies` 调用点 |
| `mlir/lib/Dialect/Bufferization/Transforms/Bufferize.cpp` | 281 | 无分析版 `bufferizeOp` |
| `mlir/docs/Bufferization.md` | 30–70 | One-Shot 的六个设计属性 |
| 同上 | 378–450 | `print-conflicts` 的完整例子 |

## 22.9 可跑实验

统一前置：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
mkdir -p /tmp/mlir-part4 && cd /tmp/mlir-part4
```

### 实验 1：接口方法清单

```bash
grep -n 'methodName=' \
  /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td \
  | head -8
```

本机实测：

```text
36:        /*methodName=*/"bufferizesToAllocation"
54:        /*methodName=*/"bufferizesToMemoryRead"
82:        /*methodName=*/"bufferizesToMemoryWrite"
139:        /*methodName=*/"bufferizesToElementwiseAccess"
195:        /*methodName=*/"resultBufferizesToMemoryWrite"
218:        /*methodName=*/"mustBufferizeInPlace"
284:        /*methodName=*/"getAliasingValues"
356:        /*methodName=*/"getAliasingOpOperands"
```

### 实验 2：默认 one-shot 只在函数内部 bufferize

```bash
cat > buf.mlir <<'EOF'
func.func @matmul_like(%a: tensor<4x4xf32>, %b: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %cst = arith.constant 0.0 : f32
  %init = tensor.empty() : tensor<4x4xf32>
  %filled = linalg.fill ins(%cst : f32) outs(%init : tensor<4x4xf32>) -> tensor<4x4xf32>
  %r = linalg.matmul ins(%a, %b : tensor<4x4xf32>, tensor<4x4xf32>)
                     outs(%filled : tensor<4x4xf32>) -> tensor<4x4xf32>
  return %r : tensor<4x4xf32>
}
EOF
$T/bin/mlir-opt buf.mlir -one-shot-bufferize
```

本机实测：

```text
module {
  func.func @matmul_like(%arg0: tensor<4x4xf32>, %arg1: tensor<4x4xf32>) -> tensor<4x4xf32> {
    %0 = bufferization.to_buffer %arg1 : tensor<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
    %1 = bufferization.to_buffer %arg0 : tensor<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
    %cst = arith.constant 0.000000e+00 : f32
    %alloc = memref.alloc() alignment = 64 : memref<4x4xf32>
    linalg.fill ins(%cst : f32) outs(%alloc : memref<4x4xf32>)
    linalg.matmul ins(%1, %0 : memref<4x4xf32, strided<[?, ?], offset: ?>>, memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%alloc : memref<4x4xf32>)
    %2 = bufferization.to_tensor %alloc : memref<4x4xf32> to tensor<4x4xf32>
    return %2 : tensor<4x4xf32>
  }
}
```

函数签名没变（还是 tensor），内部变成 memref + 边界转换。`memref.alloc() alignment = 64` 是默认对齐。

### 实验 3：bufferize 函数边界

```bash
$T/bin/mlir-opt buf.mlir -one-shot-bufferize=bufferize-function-boundaries
```

本机实测：

```text
module {
  func.func @matmul_like(%arg0: memref<4x4xf32, strided<[?, ?], offset: ?>>, %arg1: memref<4x4xf32, strided<[?, ?], offset: ?>>) -> memref<4x4xf32> {
    %cst = arith.constant 0.000000e+00 : f32
    %alloc = memref.alloc() alignment = 64 : memref<4x4xf32>
    linalg.fill ins(%cst : f32) outs(%alloc : memref<4x4xf32>)
    linalg.matmul ins(%arg0, %arg1 : memref<4x4xf32, strided<[?, ?], offset: ?>>, memref<4x4xf32, strided<[?, ?], offset: ?>>) outs(%alloc : memref<4x4xf32>)
    %cast = memref.cast %alloc : memref<4x4xf32> to memref<4x4xf32, strided<[?, ?], offset: ?>>
    return %alloc : memref<4x4xf32>
  }
}
```

注意参数布局是动态 strided，而 `%alloc` 是静态 `memref<4x4xf32>`；`memref.cast` 是布局适配，不是 copy。

### 实验 4：只看分析结果

```bash
$T/bin/mlir-opt buf.mlir -one-shot-bufferize='test-analysis-only print-conflicts'
```

本机实测：

```text
module {
  func.func @matmul_like(%arg0: tensor<4x4xf32>, %arg1: tensor<4x4xf32>) -> tensor<4x4xf32> {
    %cst = arith.constant 0.000000e+00 : f32
    %0 = tensor.empty() : tensor<4x4xf32>
    %1 = linalg.fill {__inplace_operands_attr__ = ["none", "true"]} ins(%cst : f32) outs(%0 : tensor<4x4xf32>) -> tensor<4x4xf32>
    %2 = linalg.matmul {__inplace_operands_attr__ = ["true", "true", "true"]} ins(%arg0, %arg1 : tensor<4x4xf32>, tensor<4x4xf32>) outs(%1 : tensor<4x4xf32>) -> tensor<4x4xf32>
    return {__inplace_operands_attr__ = ["true"]} %2 : tensor<4x4xf32>
  }
}
```

所有可写 operand 都是 `true`（in-place），没有冲突，所以没有 `C_i` 注解。这份 IR **完全没被 bufferize**，只是被分析过。

### 实验 5：制造一次 RaW 冲突

```bash
cat > copy.mlir <<'EOF'
func.func @insert_and_keep(%t: tensor<4xf32>, %idx: index, %v: f32)
    -> (tensor<4xf32>, tensor<4xf32>) {
  %0 = tensor.insert %v into %t[%idx] : tensor<4xf32>
  return %0, %t : tensor<4xf32>, tensor<4xf32>
}
EOF
$T/bin/mlir-opt copy.mlir -one-shot-bufferize='test-analysis-only print-conflicts bufferize-function-boundaries'
```

本机实测：

```text
module {
  func.func @insert_and_keep(%arg0: tensor<4xf32> {bufferization.access = "read"}, %arg1: index, %arg2: f32) -> (tensor<4xf32>, tensor<4xf32>) attributes {"C_0[DEF: bbArg 0]"} {
    %inserted = tensor.insert %arg2 into %arg0[%arg1] {"C_0[CONFL-WRITE: 1]", __inplace_operands_attr__ = ["none", "false", "none"]} : tensor<4xf32>
    return {"C_0[READ: 1]", __equivalent_func_args__ = [-1, 0], __inplace_operands_attr__ = ["true", "true"]} %inserted, %arg0 : tensor<4xf32>, tensor<4xf32>
  }
}
```

三个 `C_0` 注解齐全：定义在 `bbArg 0`，冲突写是 `tensor.insert` 的第 1 个 operand，读发生在 `return`。`__inplace_operands_attr__` 里对应位置是 `false` → 会插 copy。

实际 bufferize：

```bash
$T/bin/mlir-opt copy.mlir -one-shot-bufferize=bufferize-function-boundaries
```

本机实测：

```text
    %alloc = memref.alloc() alignment = 64 : memref<4xf32>
    memref.copy %arg0, %alloc : memref<4xf32, strided<[?], offset: ?>> to memref<4xf32>
    memref.store %arg2, %alloc[%arg1] : memref<4xf32>
```

`memref.copy` 就是分析阶段那次 `false` 的落地。

### 实验 6：未知 op 的两种命运

```bash
cat > unknown_tensor.mlir <<'EOF'
func.func @f(%a: tensor<4xf32>) -> tensor<4xf32> {
  %0 = "foo.bar"(%a) : (tensor<4xf32>) -> tensor<4xf32>
  return %0 : tensor<4xf32>
}
EOF
echo '--- strict'
$T/bin/mlir-opt unknown_tensor.mlir -allow-unregistered-dialect -one-shot-bufferize 2>&1 | head -3
echo '--- allow-unknown-ops'
$T/bin/mlir-opt unknown_tensor.mlir -allow-unregistered-dialect \
  -one-shot-bufferize='allow-unknown-ops bufferize-function-boundaries'
```

本机实测：

```text
--- strict
unknown_tensor.mlir:2:8: error: op was not bufferized
  %0 = "foo.bar"(%a) : (tensor<4xf32>) -> tensor<4xf32>
--- allow-unknown-ops
module {
  func.func @f(%arg0: memref<4xf32, strided<[?], offset: ?>>) -> memref<4xf32, strided<[?], offset: ?>> {
    %0 = bufferization.to_tensor %arg0 : memref<4xf32, strided<[?], offset: ?>> to tensor<4xf32>
    %1 = "foo.bar"(%0) : (tensor<4xf32>) -> tensor<4xf32>
    %2 = bufferization.to_buffer %1 : tensor<4xf32> to memref<4xf32, strided<[?], offset: ?>>
    return %2 : memref<4xf32, strided<[?], offset: ?>>
  }
}
```

### 实验 7：加上 deallocation

```bash
cat > dealloc.mlir <<'EOF'
func.func @tmp(%a: tensor<4xf32>) -> f32 {
  %c0 = arith.constant 0 : index
  %cst = arith.constant 1.0 : f32
  %0 = tensor.empty() : tensor<4xf32>
  %1 = linalg.fill ins(%cst : f32) outs(%0 : tensor<4xf32>) -> tensor<4xf32>
  %2 = tensor.extract %1[%c0] : tensor<4xf32>
  return %2 : f32
}
EOF
$T/bin/mlir-opt dealloc.mlir -one-shot-bufferize=bufferize-function-boundaries \
  -buffer-deallocation-pipeline
```

本机实测（节选）：

```text
    %alloc = memref.alloc() alignment = 64 : memref<4xf32>
    linalg.fill ins(%cst : f32) outs(%alloc : memref<4xf32>)
    %0 = memref.load %alloc[%c0] : memref<4xf32>
    memref.dealloc %alloc : memref<4xf32>
    return %0 : f32
```

去掉 `-buffer-deallocation-pipeline` 就没有 `memref.dealloc`。

## 22.10 踩雷集锦

1. **“bufferize 就是类型替换” → 是先分析后重写。** 直接改类型会破坏别名语义；所有 in-place 决定必须来自 `AnalysisState`。

2. **“多一次 copy 是 canonicalization 没跑” → 多半是保守的别名声明。** 先看 `print-conflicts`，再回去看对应 op 的 `getAliasingValues`。

3. **“`allow-unknown-ops` 等于跳过未知 op” → 是在边界插 `to_tensor`/`to_buffer`。** 之后还要有人 bufferize 它们，否则 IR 仍是混合态。

4. **“bufferize 完就自动释放” → 不会。** 必须接 ownership-based 的 `-buffer-deallocation-pipeline`；旧 buffer deallocation pass 已废弃。

5. **“函数边界默认会被 bufferize” → 默认不会。** 需要 `bufferize-function-boundaries`，且递归调用仍不支持。

6. **“`to_buffer` 用静态布局更快” → 默认必须用最动态的布局。** 否则后续 bufferize 同一块内存时布局不兼容；要静态布局就整段都 bufferize 或用 `unknown-type-conversion`。

7. **“文档里的 `getAliasingOpResult` 可以直接抄” → 已改名为 `getAliasingValues`。** 以 `.td` 为准。

8. **“`bufferization.access = "read"` 是用户属性” → 是分析阶段加的注解。** 它会出现在 `print-conflicts` 的输出里，不要当成输入契约。

## 22.11 本章重点整理

- Bufferization 是**全函数、两阶段**：先建 alias/equivalence 集合做 in-place 决策，再统一重写。
- 必须实现的是读写判定、双向别名查询、`bufferize`；`isWritable` 负责“非冲突型 copy”。
- 接口实现必须保守：漏报别名是正确性 bug，多报只是性能损失。
- `test-analysis-only` + `print-conflicts` 是定位 copy 的标准工具；`C_i[DEF/CONFL-WRITE/READ]` 是三元组。
- unknown op 默认报 `op was not bufferized`；`allow-unknown-ops` 在边界插 `to_tensor`/`to_buffer`。
- 函数边界需要显式开关；deallocation 是另一个 pipeline。
- `to_buffer` 的默认布局故意最动态，这是为了与后续 bufferization 组合。

## 22.12 自我检核

- [ ] 能说出 One-Shot 的六个设计属性，并解释“2-Phase”各自做什么。
- [ ] 能列出 `BufferizableOpInterface` 的五个必答方法及各自语义。
- [ ] 能解释“保守实现”为什么是安全方向，并举一个漏报别名的后果。
- [ ] 能读懂 `__inplace_operands_attr__` 与 `C_i[...]` 注解，并定位对应的 copy。
- [ ] 能解释 `to_buffer` 为什么用 `strided<[?], offset: ?>`。
- [ ] 知道函数边界默认不 bufferize，以及递归调用的限制。
- [ ] 知道 deallocation 必须单独跑，且旧 pass 已废弃。
- [ ] 能用 `print-conflicts` 判断一次 copy 是 RaW 冲突还是不可写 buffer。

## 22.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Bufferization.md`**
  - 读哪里：24–90（设计目标）、255–380（用法与布局）、378–470（调试 copy）
  - 能学到什么：One-Shot 的官方语义与 `print-conflicts` 的完整例子
  - 和本章关联：22.1–22.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/Bufferization/IR/BufferizableOpInterface.td`**
  - 读哪里：14–120（保守性说明与读写判定）、280–470（别名与重写）
  - 能学到什么：接口的真实签名（文档已过时）
  - 和本章关联：22.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Bufferization/Transforms/OneShotAnalysis.cpp`**
  - 读哪里：1600–1720
  - 能学到什么：`runOneShotBufferize` 的阶段顺序与统计
  - 和本章关联：22.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Bufferization/Transforms/Bufferize.cpp`**
  - 读哪里：260–330
  - 能学到什么：无分析版本如何做到“每次写都 copy”
  - 和本章关联：22.3、22.10 第 1 条
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Dialect/Bufferization/Transforms/one-shot-bufferize.mlir`**
  - 读哪里：整份（很大，按 `CHECK-LABEL` 跳读）
  - 能学到什么：每种 op 的 in-place 期望与冲突注解
  - 和本章关联：22.6、22.9

tensor 变成 memref 之后，IR 离机器只差最后一跳。下一章把 memref/arith/func 一路降到 LLVM dialect、LLVM IR、汇编，并解释 ABI 与 `mlir-translate` 的边界。

→ 下一章：[Ch 23 降到 LLVM：convert-to-llvm、ABI 与验证链](./23-lowering-to-llvm-and-beyond.md)
