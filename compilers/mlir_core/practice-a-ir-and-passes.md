# 练习 A：读写 MLIR 与观察 pass

> **目标**：拿一段混合 dialect 的 IR，先判断每个 op 属于哪一层、跑 pipeline 后会变成什么，再用真实输出验证。最后你自己写一个四层混合的小程序，并解释每一次 pass 的边界。

## 为什么做这个练习？

Ch 0–4 都是“看别人写好的例子”。真实工作是反过来的：同事丢给你一段 IR 和一条 pipeline，问你“跑完会变成什么”。

预测错了，通常不是记错 pass 名，而是**误判了 op 所在的层**。两个典型：

- 以为 `convert-linalg-to-loops` 能处理 tensor 上的 `linalg.matmul`，跑完发现 IR 没变；
- 以为 `convert-scf-to-cf` 会把 `affine.for` 也一起降掉，结果它原样留着。

这个练习要交付四样东西：

1. 一张填完的“op → 层 → 变化”预测表；
2. 一条能跑通的 P1（到 `cf` 为止）与 P2（到 `llvm` 为止）；
3. 一个你自己写的、单函数内四层混合的 `my_mixed.mlir`；
4. 两份可复现的错误报告。

## 先建立直觉：pass 只碰它认识的 op

先做一个小实验，建立“静默 no-op”的直觉。把下面内容保存为 `.\noop.mlir`（就是 Ch 4 的 `linalg_add.mlir`）：

```mlir
func.func @add_tensors(%A: tensor<4x4xf32>, %B: tensor<4x4xf32>) -> tensor<4x4xf32> {
  %init = tensor.empty() : tensor<4x4xf32>
  %0 = linalg.add ins(%A, %B : tensor<4x4xf32>, tensor<4x4xf32>)
                    outs(%init : tensor<4x4xf32>) -> tensor<4x4xf32>
  return %0 : tensor<4x4xf32>
}
```

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\noop.mlir --pass-pipeline='builtin.module(func.func(convert-linalg-to-loops))'
```

实测输出：

```mlir
module {
  func.func @add_tensors(%arg0: tensor<4x4xf32>, %arg1: tensor<4x4xf32>) -> tensor<4x4xf32> {
    %0 = tensor.empty() : tensor<4x4xf32>
    %1 = linalg.add ins(%arg0, %arg1 : tensor<4x4xf32>, tensor<4x4xf32>) outs(%0 : tensor<4x4xf32>) -> tensor<4x4xf32>
    return %1 : tensor<4x4xf32>
  }
}
```

**一个 op 都没变，也没有报错。** 原因在 Ch 4 说过：`Loops.cpp:266` 要求 `hasPureBufferSemantics()`。tensor 版本不满足，pattern 直接 `notifyMatchFailure`，pass 正常结束。

记住这个感觉：**没有报错 ≠ 发生了变换**。本练习的每一步，你都要先预测“会不会变”，再用 `--mlir-print-ir-after-change` 或输出 diff 验证。

## 任务规格

| 项 | 内容 |
|---|---|
| 环境 | MLIR 23.1.4，PowerShell，`$mlir` 指向 `.scratch\mlir-env\Library\bin` |
| 工作目录 | 本章目录下的 `.\practice-a\` |
| 输入 | 本练习给出的 `predict.mlir`（照抄，不要改） |
| 自写文件 | `my_mixed.mlir`，单函数内混合四层 |
| 时间 | 60–90 分钟 |
| 规则 | 先填预测卡再跑命令；每条命令保留完整输出；不要改 pipeline 顺序 |

**P1（到 CFG 为止）**：

```text
builtin.module(
  one-shot-bufferize{bufferize-function-boundaries},
  func.func(convert-linalg-to-loops),
  func.func(lower-affine),
  func.func(convert-scf-to-cf)
)
```

**P2（到 LLVM dialect 为止）**：在 P1 后面追加

```text
convert-func-to-llvm, convert-cf-to-llvm, convert-arith-to-llvm,
finalize-memref-to-llvm, reconcile-unrealized-casts
```

### 评分点

| 交付物 | 权重 | 判据 |
|---|---|---|
| 预测卡 | 30% | 12 行里至少 10 行判断正确 |
| P1 / P2 输出 | 20% | 与预期一致，并能指出差异发生在哪一步 |
| `my_mixed.mlir` | 30% | 单函数含四层，能跑通 P1 与 P2 |
| 错误报告 | 10% | 两条报错原文 + 一句话区别 |
| 解释 | 10% | 能说明为什么第 2、3 个函数在 bufferize 时没变 |

## 准备：建目录、写 `predict.mlir`

```powershell
New-Item -ItemType Directory -Force -Path .\practice-a | Out-Null
```

把下面内容保存为 `.\practice-a\predict.mlir`：

```mlir
// Stage 1: tensor + linalg
func.func @stage_tensor_linalg(%A: tensor<8x8xf32>, %B: tensor<8x8xf32>) -> tensor<8x8xf32> {
  %init = tensor.empty() : tensor<8x8xf32>
  %0 = linalg.matmul ins(%A, %B : tensor<8x8xf32>, tensor<8x8xf32>)
                      outs(%init : tensor<8x8xf32>) -> tensor<8x8xf32>
  return %0 : tensor<8x8xf32>
}

// Stage 2: scf + memref + arith
func.func @stage_scf_memref(%scale: f32, %in: memref<8x8xf32>, %out: memref<8x8xf32>) {
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  %c8 = arith.constant 8 : index
  %s0 = arith.constant 0.0 : f32
  scf.for %i = %c0 to %c8 step %c1 iter_args(%acc = %s0) -> (f32) {
    %v = memref.load %in[%i, %i] : memref<8x8xf32>
    %t = arith.mulf %v, %scale : f32
    %n = arith.addf %acc, %t : f32
    memref.store %n, %out[%i, %i] : memref<8x8xf32>
    scf.yield %n : f32
  }
  return
}

// Stage 3: affine + memref
func.func @stage_affine(%scale: f32, %in: memref<8x8xf32>, %out: memref<8x8xf32>) {
  affine.for %i = 0 to 8 {
    %v = affine.load %in[%i, %i] : memref<8x8xf32>
    %t = arith.mulf %v, %scale : f32
    affine.store %t, %out[%i, %i] : memref<8x8xf32>
  }
  return
}
```

这个 module 故意把三种“层”放进三个函数：预测时你不必同时追多个 region，但 bufferize 的边界会更明显。

## 第 1 步：先读，不跑

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir
```

实测输出：

```mlir
module {
  func.func @stage_tensor_linalg(%arg0: tensor<8x8xf32>, %arg1: tensor<8x8xf32>) -> tensor<8x8xf32> {
    %0 = tensor.empty() : tensor<8x8xf32>
    %1 = linalg.matmul ins(%arg0, %arg1 : tensor<8x8xf32>, tensor<8x8xf32>) outs(%0 : tensor<8x8xf32>) -> tensor<8x8xf32>
    return %1 : tensor<8x8xf32>
  }
  func.func @stage_scf_memref(%arg0: f32, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    %c8 = arith.constant 8 : index
    %cst = arith.constant 0.000000e+00 : f32
    %0 = scf.for %arg3 = %c0 to %c8 step %c1 iter_args(%arg4 = %cst) -> (f32) {
      %1 = memref.load %arg1[%arg3, %arg3] : memref<8x8xf32>
      %2 = arith.mulf %1, %arg0 : f32
      %3 = arith.addf %arg4, %2 : f32
      memref.store %3, %arg2[%arg3, %arg3] : memref<8x8xf32>
      scf.yield %3 : f32
    }
    return
  }
  func.func @stage_affine(%arg0: f32, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    affine.for %arg3 = 0 to 8 {
      %0 = affine.load %arg1[%arg3, %arg3] : memref<8x8xf32>
      %1 = arith.mulf %0, %arg0 : f32
      affine.store %1, %arg2[%arg3, %arg3] : memref<8x8xf32>
    }
    return
  }
}
```

现在填预测卡。**不要跑 P1，先写完再对照。**

| # | op | 我的判断：方言 / 层 | P1 跑完后变成什么 |
|---|---|---|---|
| 1 | `func.func`（三个） |  |  |
| 2 | `tensor.empty` |  |  |
| 3 | `linalg.matmul` |  |  |
| 4 | `arith.constant` |  |  |
| 5 | `scf.for`（带 `iter_args`） |  |  |
| 6 | `scf.yield` |  |  |
| 7 | `memref.load` / `memref.store` |  |  |
| 8 | `arith.mulf` / `arith.addf` |  |  |
| 9 | `affine.for` |  |  |
| 10 | `affine.load` / `affine.store` |  |  |
| 11 | `arith.mulf`（affine 函数里） |  |  |
| 12 | `func.return` |  |  |

提示：第 1 行有三个函数，答案不是一句话；第 4、8、11 行要想清楚“常量会不会被复制”。

## 第 2 步：跑 P1，一次只加一个 pass

不要一口气跑完。每加一个 pass 就打印一次 IR，你才能看到“边界”发生在哪一步。

### 2.1 只做 bufferize

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir --one-shot-bufferize="bufferize-function-boundaries"
```

实测输出：

```mlir
module {
  func.func @stage_tensor_linalg(%arg0: memref<8x8xf32, strided<[?, ?], offset: ?>>, %arg1: memref<8x8xf32, strided<[?, ?], offset: ?>>) -> memref<8x8xf32> {
    %alloc = memref.alloc() {alignment = 64 : i64} : memref<8x8xf32>
    linalg.matmul ins(%arg0, %arg1 : memref<8x8xf32, strided<[?, ?], offset: ?>>, memref<8x8xf32, strided<[?, ?], offset: ?>>) outs(%alloc : memref<8x8xf32>)
    %cast = memref.cast %alloc : memref<8x8xf32> to memref<8x8xf32, strided<[?, ?], offset: ?>>
    return %alloc : memref<8x8xf32>
  }
  func.func @stage_scf_memref(%arg0: f32, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    %c8 = arith.constant 8 : index
    %cst = arith.constant 0.000000e+00 : f32
    %0 = scf.for %arg3 = %c0 to %c8 step %c1 iter_args(%arg4 = %cst) -> (f32) {
      %1 = memref.load %arg1[%arg3, %arg3] : memref<8x8xf32>
      %2 = arith.mulf %1, %arg0 : f32
      %3 = arith.addf %arg4, %2 : f32
      memref.store %3, %arg2[%arg3, %arg3] : memref<8x8xf32>
      scf.yield %3 : f32
    }
    return
  }
  func.func @stage_affine(%arg0: f32, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    affine.for %arg3 = 0 to 8 {
      %0 = affine.load %arg1[%arg3, %arg3] : memref<8x8xf32>
      %1 = arith.mulf %0, %arg0 : f32
      affine.store %1, %arg2[%arg3, %arg3] : memref<8x8xf32>
    }
    return
  }
}
```

注意：**只有第一个函数变了**。第 2、3 个函数里根本没有 tensor，bufferize 无事可做。如果你预测它们也变了，问题不在 pass，而在“bufferization 到底改什么”的理解。

### 2.2 加上 `convert-linalg-to-loops`

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops))'
```

实测输出（只贴第一个函数，后两个与 2.1 完全相同）：

```mlir
  func.func @stage_tensor_linalg(%arg0: memref<8x8xf32, strided<[?, ?], offset: ?>>, %arg1: memref<8x8xf32, strided<[?, ?], offset: ?>>) -> memref<8x8xf32> {
    %c1 = arith.constant 1 : index
    %c8 = arith.constant 8 : index
    %c0 = arith.constant 0 : index
    %alloc = memref.alloc() {alignment = 64 : i64} : memref<8x8xf32>
    scf.for %arg2 = %c0 to %c8 step %c1 {
      scf.for %arg3 = %c0 to %c8 step %c1 {
        scf.for %arg4 = %c0 to %c8 step %c1 {
          %0 = memref.load %arg0[%arg2, %arg4] : memref<8x8xf32, strided<[?, ?], offset: ?>>
          %1 = memref.load %arg1[%arg4, %arg3] : memref<8x8xf32, strided<[?, ?], offset: ?>>
          %2 = memref.load %alloc[%arg2, %arg3] : memref<8x8xf32>
          %3 = arith.mulf %0, %1 : f32
          %4 = arith.addf %2, %3 : f32
          memref.store %4, %alloc[%arg2, %arg3] : memref<8x8xf32>
        }
      }
    }
    return %alloc : memref<8x8xf32>
  }
```

一个 `linalg.matmul` 变成了三层 `scf.for` + 标量 `arith`。元素级实现回到了 `arith`。

### 2.3 加上 `lower-affine`

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops),func.func(lower-affine))'
```

实测输出（只贴第三个函数）：

```mlir
  func.func @stage_affine(%arg0: f32, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    %c0 = arith.constant 0 : index
    %c8 = arith.constant 8 : index
    %c1 = arith.constant 1 : index
    scf.for %arg3 = %c0 to %c8 step %c1 {
      %0 = memref.load %arg1[%arg3, %arg3] : memref<8x8xf32>
      %1 = arith.mulf %0, %arg0 : f32
      memref.store %1, %arg2[%arg3, %arg3] : memref<8x8xf32>
    }
    return
  }
```

`affine.for` → `scf.for`，`affine.load/store` → `memref.load/store`。step 从“隐式 1”变成了显式的 `%c1`。

### 2.4 加上 `convert-scf-to-cf`

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops),func.func(lower-affine),func.func(convert-scf-to-cf))'
```

实测输出（只贴第二个函数）：

```mlir
  func.func @stage_scf_memref(%arg0: f32, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
    %c8 = arith.constant 8 : index
    %cst = arith.constant 0.000000e+00 : f32
    cf.br ^bb1(%c0, %cst : index, f32)
  ^bb1(%0: index, %1: f32):  // 2 preds: ^bb0, ^bb2
    %2 = arith.cmpi slt, %0, %c8 : index
    cf.cond_br %2, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %3 = memref.load %arg1[%0, %0] : memref<8x8xf32>
    %4 = arith.mulf %3, %arg0 : f32
    %5 = arith.addf %1, %4 : f32
    memref.store %5, %arg2[%0, %0] : memref<8x8xf32>
    %6 = arith.addi %0, %c1 : index
    cf.br ^bb1(%6, %5 : index, f32)
  ^bb3:  // pred: ^bb1
    return
  }
```

两件事同时发生：

- `scf.for` 的 `iter_args` 变成了 block argument：`^bb1(%0: index, %1: f32)`，回边传 `(%6, %5)`；
- `scf.yield` 消失，值直接由 `cf.br` 携带。

到这里，**P1 结束**。IR 里还有 `arith`、`memref`、`func`、`cf`，但已经没有 `scf`、`linalg`、`affine`、`tensor`。

## 第 3 步：跑 P2，降到 LLVM dialect

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops),func.func(lower-affine),func.func(convert-scf-to-cf),convert-func-to-llvm,convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,reconcile-unrealized-casts)'
```

完整输出有 198 行，这里只贴关键片段：

```mlir
module {
  llvm.func @malloc(i64) -> !llvm.ptr
  llvm.func @stage_tensor_linalg(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64, %arg3: i64, %arg4: i64, %arg5: i64, %arg6: i64, %arg7: !llvm.ptr, %arg8: !llvm.ptr, %arg9: i64, %arg10: i64, %arg11: i64, %arg12: i64, %arg13: i64) -> !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)> {
    %0 = llvm.mlir.poison : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
    %1 = llvm.insertvalue %arg7, %0[0] : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
...
    %77 = llvm.fmul %60, %70 : f32
    %78 = llvm.fadd %76, %77 : f32
    llvm.store %78, %83 : f32, !llvm.ptr
    llvm.return %44 : !llvm.struct<(ptr, ptr, i64, array<2 x i64>, array<2 x i64>)>
```

对照 Ch 4 的三个示例，这次多了三件事：

1. **`memref` 被拆开了**：一个 `memref<8x8xf32>` 参数变成 7 个标量（两个指针 + offset + 2 个 size + 2 个 stride），返回值变成 `!llvm.struct`。这就是 `finalize-memref-to-llvm` 的工作。
2. **`malloc` 出现了**：`memref.alloc` 变成对 `@malloc` 的调用，函数声明自动插到 module 顶部。
3. **`index` 全变成 `i64`**：循环边界、`arith.addi` 的索引加法，一个不剩。

可选再走一步，确认它真的是合法 LLVM dialect：

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\predict.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops),func.func(lower-affine),func.func(convert-scf-to-cf),convert-func-to-llvm,convert-cf-to-llvm,convert-arith-to-llvm,finalize-memref-to-llvm,reconcile-unrealized-casts)' | & "$mlir\mlir-translate.exe" --mlir-to-llvmir
```

实测能产出以 `define { ptr, ptr, i64, [2 x i64], [2 x i64] } @stage_tensor_linalg(...)` 开头的 LLVM IR。

## 第 4 步：自己写一个四层混合程序

现在反过来：给你需求，你写 IR。

`.\practice-a\my_mixed.mlir` 必须满足：

1. **只有一个函数**；
2. 同一个函数里同时出现四层：
   - `tensor` + `linalg`（至少一个 `linalg.matmul` 或 `linalg.add`）；
   - `scf`（一个带 `iter_args` 的 `scf.for`）；
   - `affine`（一个 `affine.for` + `affine.load`/`affine.store`）；
   - `arith`（至少两条标量运算）；
3. 函数返回一个 `tensor`；
4. 不加任何 pass 也能通过 `mlir-opt`；
5. 跑 P1 后必须出现 `cf.br`，并且 `linalg`、`affine`、`scf`、`tensor` 全部消失。

两个容易踩的点，先想清楚再写：

- **`affine` 的下标只能来自仿射作用域**。`affine.for` 可以嵌在 `scf.for` 里（本机实测能 parse），但外层 `scf.for` 的归纳变量不能直接喂给 `affine.load`——会报 `operand cannot be used as a dimension id`。最稳妥的写法是把 `scf` 和 `affine` 放在函数的两个位置。
- **让 `scf.for` 的结果被使用**。如果 `iter_args` 的结果没人用，greedy driver 可能把整个循环当死代码删掉；你会以为“pass 没跑”。

写完后跑：

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\my_mixed.mlir
& "$mlir\mlir-opt.exe" .\practice-a\my_mixed.mlir --pass-pipeline='builtin.module(one-shot-bufferize{bufferize-function-boundaries},func.func(convert-linalg-to-loops),func.func(lower-affine),func.func(convert-scf-to-cf))'
```

判据：第二条命令的输出里，`tensor.`、`linalg.`、`affine.`、`scf.` 一个都不剩，且能看到 `cf.br`。

## 第 5 步：未知 dialect 的两种报错

### 5.1 custom 语法：parser 先拦

`.\practice-a\unknown_custom.mlir`：

```mlir
func.func @f(%x: i32) -> i32 {
  %0 = foo.bar %x : i32
  return %0 : i32
}
```

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\unknown_custom.mlir
```

实测报错（去掉路径）：

```text
error: Dialect `foo' not found for custom op 'foo.bar'
note: Available dialects: acc, affine, amdgpu, ...
```

`foo.bar %x` 是 custom 语法，parser 需要 `foo` dialect 提供的 parser。没有注册，连“读进来”这一步都过不去。

### 5.2 generic 语法：注册门禁拦

`.\practice-a\unknown_generic.mlir`：

```mlir
func.func @f(%x: i32) -> i32 {
  %0 = "foo.bar"(%x) : (i32) -> i32
  return %0 : i32
}
```

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\unknown_generic.mlir
```

实测报错：

```text
error: operation being parsed with an unregistered dialect. If this is intended, please use -allow-unregistered-dialect with the MLIR tool used
```

generic 形式不需要 parser，但需要“允许未注册”。加 flag：

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\unknown_generic.mlir --allow-unregistered-dialect
```

实测输出：

```mlir
module {
  func.func @f(%arg0: i32) -> i32 {
    %0 = "foo.bar"(%arg0) : (i32) -> i32
    return %0 : i32
  }
}
```

**两种报错的区别**：5.1 死在 parser（没有语法），5.2 死在注册检查（有语法，但方言未注册）。`--allow-unregistered-dialect` 只解决后者，而且只对 generic 形式有效。

### 5.3 额外一步：它最后会在哪里爆

未知 op 能通过 parse，也能通过一部分 conversion，但出不了门：

```powershell
& "$mlir\mlir-opt.exe" .\practice-a\unknown_generic.mlir --allow-unregistered-dialect --pass-pipeline='builtin.module(convert-func-to-llvm,convert-arith-to-llvm,reconcile-unrealized-casts)' | & "$mlir\mlir-translate.exe" --mlir-to-llvmir --allow-unregistered-dialect
```

实测报错：

```text
error: cannot be converted to LLVM IR: missing `LLVMTranslationDialectInterface` registration for dialect for op: foo.bar
```

`llvm.func` 里可以暂时残留未知 op，但 `mlir-translate` 需要每个 op 的翻译接口。这是“混合中间状态”与“最终产物”的分界线。

<details>
<summary>参考解答（先自己填完预测卡再看）</summary>

### 预测卡答案

| # | op | 方言 / 层 | P1 后 |
|---|---|---|---|
| 1 | `func.func`（三个） | `func` / 函数 | 只有 stage 1 的签名变成 memref；另外两个不变 |
| 2 | `tensor.empty` | `tensor` / 值张量 | 消失，换成 `memref.alloc` |
| 3 | `linalg.matmul` | `linalg` / 结构化计算 | 三层 `scf.for` + `memref.load/store` + `arith` |
| 4 | `arith.constant` | `arith` / 标量 | 保留；常量可能被复制到不同函数/循环 |
| 5 | `scf.for`（带 `iter_args`） | `scf` / 结构化控制流 | `cf.br` + `cf.cond_br`；`iter_args` 变 block args |
| 6 | `scf.yield` | `scf` | 消失，值由 `cf.br` 携带 |
| 7 | `memref.load/store` | `memref` / 内存 | 不变 |
| 8 | `arith.mulf/addf` | `arith` / 标量 | 不变 |
| 9 | `affine.for` | `affine` / 多面体 | `scf.for` |
| 10 | `affine.load/store` | `affine` | `memref.load/store` |
| 11 | `arith.mulf`（affine 函数里） | `arith` / 标量 | 不变 |
| 12 | `func.return` | `func` | 不变（P1 不降 `func`） |

### 三个必须能解释的点

- **bufferize 只动第一个函数**：bufferization 的输入契约是 tensor，输出是 memref。已经用 memref 的函数不在它的工作范围里。
- **stage 2 的 `scf` 直到 2.4 才变**：`scf → cf` 是独立 pass，`lower-affine` 只负责 affine。顺序不能反：如果先 `convert-scf-to-cf` 再 `lower-affine`，新生成的 `scf.for` 会留下来（Ch 4 踩雷 7）。
- **`llvm.func` 的签名认不出**：`memref` 在 LLVM 侧被拆成 `struct`（两个指针 + offset + sizes + strides），传参时再拆成 7 个标量。ABI 是 `finalize-memref-to-llvm` 定的，不是你写的。

### `my_mixed.mlir` 参考版本

```mlir
func.func @mixed_one_func(%A: tensor<8x8xf32>, %B: tensor<8x8xf32>,
                          %in: memref<8x8xf32>, %out: memref<8x8xf32>,
                          %scale: f32) -> tensor<8x8xf32> {
  %init = tensor.empty() : tensor<8x8xf32>
  %mm = linalg.matmul ins(%A, %B : tensor<8x8xf32>, tensor<8x8xf32>)
                       outs(%init : tensor<8x8xf32>) -> tensor<8x8xf32>
  %c0 = arith.constant 0 : index
  %c1 = arith.constant 1 : index
  %c8 = arith.constant 8 : index
  %s0 = arith.constant 0.0 : f32
  %sum = scf.for %i = %c0 to %c8 step %c1 iter_args(%acc = %s0) -> (f32) {
    %v = memref.load %in[%i, %i] : memref<8x8xf32>
    %t = arith.mulf %v, %scale : f32
    %n = arith.addf %acc, %t : f32
    scf.yield %n : f32
  }
  memref.store %sum, %out[%c0, %c0] : memref<8x8xf32>
  affine.for %j = 0 to 8 {
    %v = affine.load %in[%j, %j] : memref<8x8xf32>
    affine.store %v, %out[%j, %j] : memref<8x8xf32>
  }
  return %mm : tensor<8x8xf32>
}
```

`memref.store %sum, ...` 不是装饰：它让 `scf.for` 的结果有使用者，否则循环可能被当死代码删掉。

跑 P1 后，关键片段（`^bb9` 之后）：

```mlir
  ^bb9:  // pred: ^bb1
    cf.br ^bb10(%c0, %cst : index, f32)
  ^bb10(%14: index, %15: f32):  // 2 preds: ^bb9, ^bb11
    %16 = arith.cmpi slt, %14, %c8 : index
    cf.cond_br %16, ^bb11, ^bb12
  ^bb11:  // pred: ^bb10
    %17 = memref.load %arg2[%14, %14] : memref<8x8xf32>
    %18 = arith.mulf %17, %arg4 : f32
    %19 = arith.addf %15, %18 : f32
    %20 = arith.addi %14, %c1 : index
    cf.br ^bb10(%20, %19 : index, f32)
  ^bb12:  // pred: ^bb10
    memref.store %15, %arg3[%c0, %c0] : memref<8x8xf32>
```

`scf.for` 的 `iter_args` 在这里变成 `^bb10(%14: index, %15: f32)`，回边传 `(%20, %19)`。这就是“循环携带变量”在 CFG 里的样子。

</details>

## 常见卡点

1. **少了调用运算符 `&`。** 必须写 `& "$mlir\mlir-opt.exe"`；直接写 `$mlir\mlir-opt.exe`，PowerShell 会报 `Unexpected token '\mlir-opt.exe'`（本机实测）。
2. **IR dump 走 stderr，看起来“没输出”。** 加 `2>&1`，或 `| Tee-Object -FilePath out.txt`。
3. **用 `>` 存 IR，文件变成 UTF-16。** Windows PowerShell 5.1 的 `>` 默认写 UTF-16LE，`mlir-opt` 读不了（报 `unexpected character`）。用 `| Out-File -Encoding utf8` 或直接管道给 `mlir-translate`。
4. **`one-shot-bufferize` 忘了 `{bufferize-function-boundaries}`。** 函数签名仍是 tensor，函数入口会出现 `bufferization.to_buffer`，后续 `convert-func-to-llvm` 接不上。实测会看到 `%0 = bufferization.to_buffer %arg1 : tensor<8x8xf32> to memref<...>`。
5. **pass 嵌套层级写错，静默无效。** 把 `convert-linalg-to-loops` 写成 `builtin.module(convert-linalg-to-loops)`，本机实测：不报错，也不变。函数级 pass 写进 `func.func(...)`。

## 自我检核

- [ ] 我能预测 bufferize 只会改带 tensor 的那个函数
- [ ] 我能解释 `iter_args` 为什么变成 block argument
- [ ] 我能说出 P1 之后还剩哪些方言、消失了哪些
- [ ] 我能写出单函数四层混合，并让它通过 P1
- [ ] 我能复述未知 dialect 的两种报错，并说出 flag 只解决哪一种
- [ ] 我知道 `--mlir-print-ir-after-change` 能证明“这个 pass 什么也没做”

---

练习 A 的重点不是“跑通”，而是“预测—验证—解释”这个循环。下一章把这个循环拆开：PassManager 怎么嵌套、analysis 什么时候失效、pipeline 语法为什么长这样。

→ [Ch 5 PassManager 与 pass 管线](./05-pass-manager.md)
