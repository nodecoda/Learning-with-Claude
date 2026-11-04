# 练习 C — 写一个 TTGIR pass 并接入 pipeline

> 目标：从零实现一个真正能跑、能被 lit 锁住、能进 `triton-opt` 和 Python pipeline 的 TTGIR pass。做完这个练习，你应该能独立回答“一个新 pass 要改哪几个文件、为什么顺序不能乱、怎么证明它没改坏语义”。

> **环境**：Triton 3.9.0（commit `a972119e92a27b445cd160017ea461e56148590c`），源码 `/home/ubuntu/aicomp/triton`。本练习需要一份能构建的 Triton（`pip install -e . --no-build-isolation` 或 CMake 构建）。如果构建还没跑通，代码和测试可以照写，但构建/lit 输出必须标 `guided`，不要伪造。

## 为什么做这个练习

读 pass 和写 pass 是两种能力。读的时候你只需要理解 `runOnOperation` 干了什么；写的时候你必须同时处理：

- **注册**：`Passes.td`、`Passes.h`、生成的头文件、`triton-opt` 的注册、Python binding；
- **构建**：CMakeLists 里加文件，否则链接不到；
- **匹配**：pattern 的 `matchAndRewrite` 契约，什么时候返回 `failure()`；
- **语义**：替换后类型是否仍然合法、verifier 是否通过、有没有留下死代码；
- **顺序**：这个 pass 放在 pipeline 的哪一段，前后依赖是什么；
- **证据**：lit 怎么写出“只匹配该匹配的”，pytest 怎么证明数值没变。

这个练习选了一个小但真实的变换：**把 `ttg.convert_layout(tt.splat(x))` 折叠成直接用目标 layout 的 `tt.splat(x)`**。它足够小，一天能做完；又足够真实，能覆盖上面所有环节。Triton 自己的 `RemoveLayoutConversions` 会做 rematerialization，但没有专门处理 `splat`；你可以把它当成一个“上游可能接受的小优化”来写。

## 任务规格

### 要实现的变换

输入 TTGIR：

```mlir
%0 = tt.splat %arg0 : f32 -> tensor<128xf32, #blocked0>
%1 = ttg.convert_layout %0 : tensor<128xf32, #blocked0> -> tensor<128xf32, #blocked1>
```

输出：

```mlir
%1 = tt.splat %arg0 : f32 -> tensor<128xf32, #blocked1>
```

约束：

1. 只有当 `convert_layout` 的源值**直接**由 `tt.splat` 定义时才折叠；
2. `tt.splat` 的原结果如果还有别的 user，**不能删**原 op，只新增一个目标 layout 的 splat（死代码交给 canonicalize）；
3. 不能改变任何数值语义；`splat` 是纯操作，layout 只影响数据分布，不影响每个逻辑元素的值；
4. 折叠后必须通过 `triton-opt` 的 verifier。

### 验收标准

| 项 | 标准 |
|---|---|
| pass 可被 `triton-opt` 发现 | `triton-opt --help` 里出现 `--tritongpu-fold-splat-layout` |
| 正例 | `convert_layout(splat)` 消失，剩下一个目标 layout 的 `tt.splat` |
| 反例 | `convert_layout(tt.load)` 原样保留 |
| 多 use 边界 | 原 `tt.splat` 保留，新 `tt.splat` 出现，两个 layout 并存 |
| lit | 新增 `test/TritonGPU/fold-splat-layout.mlir`，`ninja check-triton-lit-tests` 通过 |
| Python pipeline（加分） | `passes.ttgpuir.add_fold_splat_layout(pm)` 可用，且能插进 `make_ttgir` |
| 数值（加分） | 一颗 vector-add kernel 在开关 pass 前后结果一致 |

### 限制

- 不要直接改 `RemoveLayoutConversions.cpp` 来“顺便”实现它；本练习要你走完整的 pass 生命周期。
- 不要用 `applyPatternsGreedily` 之外的重写驱动（比如手写 walk + erase），除非你能解释为什么 pattern 驱动不够。
- 不要在 pass 里做 layout 选择（比如“把 blocked 改成 mma”）；那是 `AccelerateMatmul` 的职责。

## 预备知识：一个新 pass 的五个落点

Triton 3.9 的 pass 不是“写一个类就完事”。一个能被 `triton-opt` 和 Python 同时使用的 TTGIR pass，至少涉及五处：

| 落点 | 文件 | 作用 |
|---|---|---|
| 声明 | `include/triton/Dialect/TritonGPU/Transforms/Passes.td` | 定义 CLI 名、op 类型、options、dependent dialects |
| 生成声明 | `include/triton/Dialect/TritonGPU/Transforms/Passes.h` | `GEN_PASS_DECL` / `GEN_PASS_REGISTRATION` |
| 实现 | `lib/Dialect/TritonGPU/Transforms/FoldSplatLayout.cpp` | `GEN_PASS_DEF_*` + `runOnOperation` |
| 构建 | `lib/Dialect/TritonGPU/Transforms/CMakeLists.txt` | 把 `.cpp` 加进 `TritonGPUTransforms` |
| Python 绑定（可选） | `python/src/passes.cc` | `ADD_PASS_WRAPPER_0("add_fold_splat_layout", ...)` |

`triton-opt` 不需要你手写注册：`bin/RegisterTritonDialects.h:74` 调了 `mlir::triton::gpu::registerTritonGPUPasses()`，而这个函数由 `Passes.h` 里的 `GEN_PASS_REGISTRATION` 生成。你只要在 `.td` 里加 `def`，重新构建，CLI flag 就出现了。

**源码依据**：

- `include/triton/Dialect/TritonGPU/Transforms/Passes.h:9-17`（`GEN_PASS_DECL` / `GEN_PASS_REGISTRATION`）
- `bin/RegisterTritonDialects.h:69-78`（`registerTritonGPUPasses()`）
- `lib/Dialect/TritonGPU/Transforms/Prefetch.cpp:45-46`（`GEN_PASS_DEF_TRITONGPUPREFETCH` + `impl::...Base`）
- `python/src/passes.h:1-3`（`ADD_PASS_WRAPPER_0` 宏）
- `python/src/passes.cc:78-84`（TTGIR pass 的绑定写法）

## 分段实现

### Step 1 — 在 `Passes.td` 里声明 pass

在 `include/triton/Dialect/TritonGPU/Transforms/Passes.td` 末尾（`#endif` 之前）加：

```tablegen
def TritonGPUFoldSplatLayout : Pass<"tritongpu-fold-splat-layout", "mlir::ModuleOp"> {
  let summary = "Fold ttg.convert_layout of tt.splat into a splat with the target encoding";

  let description = [{
    `ttg.convert_layout(tt.splat(x))` only changes the layout of a broadcasted
    scalar. Rebuilding the splat with the destination encoding removes the
    layout conversion without changing any element value.
  }];

  let dependentDialects = ["mlir::triton::TritonDialect",
                           "mlir::triton::gpu::TritonGPUDialect"];
}
```

三个细节：

- `Pass<"cli-name", "mlir::ModuleOp">` 的第一个参数就是 `triton-opt` 的 flag；命名习惯是 `tritongpu-<kebab-case>`。
- `dependentDialects` 必须包含你创建/查询的 dialect。这里会创建 `tt.splat`（Triton dialect），会读 `#ttg.*` encoding（TritonGPU dialect）。
- 如果 pass 有 option（比如 `int32_t numStages`），在这里加 `let options = [...]`，生成代码会变成 `createTritonGPUFoldSplatLayout({val})`。

**验证**：重新构建后跑 `triton-opt --help | grep fold-splat`。如果没出现，先检查 `.td` 是否被 CMake 的 `add_mlir_dialect_library`/`tablegen` 覆盖到，再检查 `Passes.h` 是否 include 了生成的 `.h.inc`。

### Step 2 — 写实现

新建 `lib/Dialect/TritonGPU/Transforms/FoldSplatLayout.cpp`：

```cpp
//===- FoldSplatLayout.cpp - fold convert_layout(tt.splat) ---------------===//
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
#include "triton/Dialect/Triton/IR/Dialect.h"
#include "triton/Dialect/TritonGPU/IR/Dialect.h"
#include "triton/Dialect/TritonGPU/Transforms/Passes.h"

namespace mlir::triton::gpu {

#define GEN_PASS_DEF_TRITONGPUFOLDSPLATLAYOUT
#include "triton/Dialect/TritonGPU/Transforms/Passes.h.inc"

namespace {

/// ttg.convert_layout(tt.splat(x)) -> tt.splat(x) with the target encoding.
struct FoldConvertOfSplat : public OpRewritePattern<ConvertLayoutOp> {
  using OpRewritePattern::OpRewritePattern;

  LogicalResult matchAndRewrite(ConvertLayoutOp op,
                                PatternRewriter &rewriter) const override {
    auto splat = op.getSrc().getDefiningOp<triton::SplatOp>();
    if (!splat)
      return failure();

    // 只重建 splat；原 splat 如果还有别的 user，保持不动。
    // 没有别的 user 时，它会在后续 canonicalize 中变成死代码被删除。
    rewriter.replaceOpWithNewOp<triton::SplatOp>(op, op.getType(),
                                                 splat.getSrc());
    return success();
  }
};

struct FoldSplatLayoutPass
    : public impl::TritonGPUFoldSplatLayoutBase<FoldSplatLayoutPass> {
  void runOnOperation() override {
    MLIRContext *ctx = &getContext();
    RewritePatternSet patterns(ctx);
    patterns.add<FoldConvertOfSplat>(ctx);
    if (failed(applyPatternsGreedily(getOperation(),
                                            std::move(patterns))))
      signalPassFailure();
  }
};

} // namespace
} // namespace mlir::triton::gpu
```

为什么这样写：

- `op.getSrc().getDefiningOp<triton::SplatOp>()` 只匹配“直接定义”，不跨越 `arith`/`ttg` 中间 op；这是本练习的刻意限制。
- `replaceOpWithNewOp<triton::SplatOp>(op, op.getType(), splat.getSrc())` 直接把 `convert_layout` 的结果替换成新 splat 的结果，use-def 自动更新。
- 用 `applyPatternsGreedily` 而不是手写 walk：pattern 驱动会处理“替换后新 op 又匹配”的情况，也会顺手做 folding；手写 walk 在 erase 时很容易踩迭代器失效。
- `signalPassFailure()` 是必须的：pattern 驱动返回 `failure` 通常意味着收敛失败（比如 pattern 自己制造了新的匹配），不能静默吞掉。

**源码依据**：

- `lib/Dialect/TritonGPU/Transforms/ReduceDataDuplication.cpp:25-27`（`GEN_PASS_DEF_*` 的写法）
- `lib/Dialect/TritonGPU/Transforms/Prefetch.cpp:727-729`（`impl::...Base` 的写法）
- `include/triton/Dialect/Triton/IR/TritonOps.td:539-551`（`TT_SplatOp` 的 operands/results）
- `include/triton/Dialect/TritonGPU/IR/TritonGPUOps.td:119-133`（`TTG_ConvertLayoutOp`）

### Step 3 — 接进 CMake

编辑 `lib/Dialect/TritonGPU/Transforms/CMakeLists.txt`，在 `add_triton_library(TritonGPUTransforms` 的源文件列表里加一行：

```cmake
  FoldSplatLayout.cpp
```

位置不重要，但建议按字母序放在 `F32DotTC.cpp` 附近。加完重新构建：

```bash
cd /home/ubuntu/aicomp/triton
cmake --build python/build/cmake.linux-x86_64-cpython-312 --target triton-opt -j"$(nproc)"
```

> 构建目录名取决于你的 Python 版本和平台；用 `find python/build -maxdepth 2 -name build.ninja` 确认。**guided**：本机当前尚未完成 Triton 构建，这条命令的产物没有实测。

如果只改 `.cpp` 而没改 `CMakeLists.txt`，典型症状是：`triton-opt` 能构建，但 `--tritongpu-fold-splat-layout` 报 “unknown pass”；或者链接时报 `undefined symbol: createTritonGPUFoldSplatLayout`。后者说明 `GEN_PASS_REGISTRATION` 生成了注册函数，但实现没进库。

### Step 4 — 用 lit 锁行为

新建 `test/TritonGPU/fold-splat-layout.mlir`。先写三个用例：正例、反例、多 use。

```mlir
// RUN: triton-opt %s -split-input-file -tritongpu-fold-splat-layout -canonicalize | FileCheck %s

#blocked0 = #ttg.blocked<{sizePerThread = [1], threadsPerWarp = [32], warpsPerCTA = [4], order = [0]}>
#blocked1 = #ttg.blocked<{sizePerThread = [4], threadsPerWarp = [8], warpsPerCTA = [4], order = [0]}>

module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32} {
  // CHECK-LABEL: @fold_splat
  // CHECK-NOT:   ttg.convert_layout
  // CHECK:       %[[S:.*]] = tt.splat %arg0 : f32 -> tensor<128xf32, #blocked1>
  // CHECK:       tt.return %[[S]]
  tt.func @fold_splat(%arg0: f32) -> tensor<128xf32, #blocked1> {
    %0 = tt.splat %arg0 : f32 -> tensor<128xf32, #blocked0>
    %1 = ttg.convert_layout %0 : tensor<128xf32, #blocked0> -> tensor<128xf32, #blocked1>
    tt.return %1 : tensor<128xf32, #blocked1>
  }
}

// -----

module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32} {
  // CHECK-LABEL: @keep_non_splat
  // CHECK:       ttg.convert_layout
  tt.func @keep_non_splat(%p: !tt.ptr<f32>) -> tensor<128xf32, #blocked1> {
    %0 = tt.splat %p : !tt.ptr<f32> -> tensor<128xf32, !tt.ptr<f32>, #blocked0>
    %1 = tt.load %0 : tensor<128xf32, !tt.ptr<f32>, #blocked0>
    %2 = ttg.convert_layout %1 : tensor<128xf32, #blocked0> -> tensor<128xf32, #blocked1>
    tt.return %2 : tensor<128xf32, #blocked1>
  }
}

// -----

module attributes {"ttg.num-warps" = 4 : i32, "ttg.num-ctas" = 1 : i32} {
  // CHECK-LABEL: @multi_use
  // CHECK-DAG:   tt.splat %arg0 : f32 -> tensor<128xf32, #blocked0>
  // CHECK-DAG:   tt.splat %arg0 : f32 -> tensor<128xf32, #blocked1>
  // CHECK-NOT:   ttg.convert_layout
  tt.func @multi_use(%arg0: f32) -> (tensor<128xf32, #blocked0>, tensor<128xf32, #blocked1>) {
    %0 = tt.splat %arg0 : f32 -> tensor<128xf32, #blocked0>
    %1 = ttg.convert_layout %0 : tensor<128xf32, #blocked0> -> tensor<128xf32, #blocked1>
    tt.return %0, %1 : tensor<128xf32, #blocked0>, tensor<128xf32, #blocked1>
  }
}
```

运行：

```bash
cd /home/ubuntu/aicomp/triton
lit -v test/TritonGPU/fold-splat-layout.mlir
# 或者走 CMake target
cmake --build python/build/cmake.linux-x86_64-cpython-312 --target check-triton-lit-tests
```

三个用例分别验证：

1. **正例**：`convert_layout` 消失，splat 的目标 layout 变成 `#blocked1`；
2. **反例**：`convert_layout` 的源是 `tt.load`，不是 `splat`，必须保留；
3. **多 use**：原 splat 仍被 `tt.return` 使用，所以必须保留 `#blocked0` 的 splat，同时新增 `#blocked1` 的 splat。

注意 `CHECK-NOT` 的作用域：它只检查当前 `CHECK-LABEL` 到下一个 `CHECK-LABEL` 之间。不要把它写在文件开头就以为全局生效。

### Step 5 — 接入 Python pipeline（加分）

如果只做 lit，pass 只能手动跑。要让它进入真实编译，需要两件事：

**(a) Python binding**。在 `python/src/passes.cc` 的 `init_triton_passes_ttgpuir` 里加：

```cpp
  ADD_PASS_WRAPPER_0("add_fold_splat_layout", createTritonGPUFoldSplatLayout);
```

`createTritonGPUFoldSplatLayout` 由 `GEN_PASS_DECL` 生成；`ADD_PASS_WRAPPER_0` 在 `python/src/passes.h:1-3` 定义。重新构建 Python 扩展后，`passes.ttgpuir.add_fold_splat_layout(pm)` 就可用。

**(b) 插进 pipeline**。在 `third_party/nvidia/backend/compiler.py` 的 `make_ttgir` 里，找一个 layout 已经确定、但 `RemoveLayoutConversions` 之前的位置：

```python
    passes.ttgpuir.add_fold_splat_layout(pm)
```

具体插在哪一行要看 `make_ttgir` 的现有顺序（约 307-393）。建议先插在 `add_remove_layout_conversions` 之前，理由：

- 它只做局部折叠，不依赖 layout propagation 的结果；
- 放在 `remove_layout_conversions` 之前，能让后者少处理一个 `convert_layout`；
- 放在 `coalesce` 之后，因为 `coalesce` 可能改变 load/store 的 layout，但不会改变 splat 的语义。

改完跑一颗最小 kernel，确认 `TRITON_KERNEL_DUMP=1` 的 TTGIR 里没有多余的 `convert_layout(splat)`，并且数值结果不变。

### Step 6 — 端到端 pytest（加分）

lit 证明 IR 变了，pytest 证明数值没变。最小测试：

```python
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
    tl.store(out_ptr + offs, x + y, mask=mask)


def test_add_with_fold_splat():
    n = 1000
    x = torch.randn(n, device="cuda")
    y = torch.randn(n, device="cuda")
    out = torch.empty_like(x)
    add_kernel[(triton.cdiv(n, 128),)](x, y, out, n, BLOCK=128)
    torch.testing.assert_close(out, x + y)
```

这颗 kernel 不一定能触发 `convert_layout(splat)`（`tl.arange` 产生的不是 splat）。要稳定触发，可以写一个显式 broadcast 的 kernel：

```python
@triton.jit
def splat_kernel(x_ptr, out_ptr, n, BLOCK: tl.constexpr):
    pid = tl.program_id(0)
    offs = pid * BLOCK + tl.arange(0, BLOCK)
    mask = offs < n
    v = tl.load(x_ptr)          # 标量 load
    acc = v + tl.zeros((BLOCK,), tl.float32)  # 可能产生 splat/broadcast
    tl.store(out_ptr + offs, acc, mask=mask)
```

具体会不会出现 `convert_layout(splat)` 取决于 layout 分配；用 `TRITON_KERNEL_DUMP=1` 看 TTGIR 确认。如果没有出现，不要硬说“pass 生效了”；改用 lit 用例作为主要证据，pytest 只作为“数值不变”的回归。

## 参考解答

<details>
<summary>完整文件（点击展开）</summary>

### `include/triton/Dialect/TritonGPU/Transforms/Passes.td`（追加）

```tablegen
def TritonGPUFoldSplatLayout : Pass<"tritongpu-fold-splat-layout", "mlir::ModuleOp"> {
  let summary = "Fold ttg.convert_layout of tt.splat into a splat with the target encoding";

  let description = [{
    `ttg.convert_layout(tt.splat(x))` only changes the layout of a broadcasted
    scalar. Rebuilding the splat with the destination encoding removes the
    layout conversion without changing any element value.
  }];

  let dependentDialects = ["mlir::triton::TritonDialect",
                           "mlir::triton::gpu::TritonGPUDialect"];
}
```

### `lib/Dialect/TritonGPU/Transforms/FoldSplatLayout.cpp`

```cpp
//===- FoldSplatLayout.cpp - fold convert_layout(tt.splat) ---------------===//
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/GreedyPatternRewriteDriver.h"
#include "triton/Dialect/Triton/IR/Dialect.h"
#include "triton/Dialect/TritonGPU/IR/Dialect.h"
#include "triton/Dialect/TritonGPU/Transforms/Passes.h"

namespace mlir::triton::gpu {

#define GEN_PASS_DEF_TRITONGPUFOLDSPLATLAYOUT
#include "triton/Dialect/TritonGPU/Transforms/Passes.h.inc"

namespace {

struct FoldConvertOfSplat : public OpRewritePattern<ConvertLayoutOp> {
  using OpRewritePattern::OpRewritePattern;

  LogicalResult matchAndRewrite(ConvertLayoutOp op,
                                PatternRewriter &rewriter) const override {
    auto splat = op.getSrc().getDefiningOp<triton::SplatOp>();
    if (!splat)
      return failure();

    rewriter.replaceOpWithNewOp<triton::SplatOp>(op, op.getType(),
                                                 splat.getSrc());
    return success();
  }
};

struct FoldSplatLayoutPass
    : public impl::TritonGPUFoldSplatLayoutBase<FoldSplatLayoutPass> {
  void runOnOperation() override {
    MLIRContext *ctx = &getContext();
    RewritePatternSet patterns(ctx);
    patterns.add<FoldConvertOfSplat>(ctx);
    if (failed(applyPatternsGreedily(getOperation(),
                                            std::move(patterns))))
      signalPassFailure();
  }
};

} // namespace
} // namespace mlir::triton::gpu
```

### `lib/Dialect/TritonGPU/Transforms/CMakeLists.txt`

在 `add_triton_library(TritonGPUTransforms` 的源文件列表里加：

```cmake
  FoldSplatLayout.cpp
```

### `python/src/passes.cc`

在 `init_triton_passes_ttgpuir` 里加：

```cpp
  ADD_PASS_WRAPPER_0("add_fold_splat_layout", createTritonGPUFoldSplatLayout);
```

### `third_party/nvidia/backend/compiler.py`

在 `make_ttgir` 中、`add_remove_layout_conversions` 之前加：

```python
    passes.ttgpuir.add_fold_splat_layout(pm)
```

### `test/TritonGPU/fold-splat-layout.mlir`

见 Step 4 的三个用例。

</details>

## 常见卡点

1. **`unknown pass`**：`.td` 改了但没重新跑 TableGen/构建；或者 `Passes.h` 没 include `.h.inc`；或者 `.cpp` 没进 CMakeLists。
2. **`undefined symbol: createTritonGPUFoldSplatLayout`**：`GEN_PASS_DEF_*` 只生成类定义，注册函数来自 `GEN_PASS_REGISTRATION`；如果实现文件没进库，链接器找不到。
3. **pattern 不触发**：`op.getSrc().getDefiningOp<triton::SplatOp>()` 只匹配直接定义。如果中间有 `ttg.convert_layout` 或 `arith.select`，就不会命中；这是刻意的，不要为了“多匹配”把条件放宽到 `getDefiningOp()` 的递归搜索。
4. **verifier 报错**：`tt.splat` 的结果类型必须和 `convert_layout` 的结果类型完全一致（shape + encoding + element type）。用 `op.getType()` 而不是手动拼 `RankedTensorType`。
5. **lit 通过但数值错**：`splat` 是纯操作，理论上不会错；如果错了，先检查 `tt.splat` 的 src 是不是你以为什么（比如它其实是 `tt.load` 的标量结果），再检查 pass 是否被插到了错误的 pipeline 位置。
6. **`CHECK-NOT` 不生效**：它只作用于当前 `CHECK-LABEL` 范围；跨函数要重新写 `CHECK-LABEL`。

## 延伸挑战

1. **把匹配扩展到 `tt.broadcast`**：`convert_layout(broadcast(x))` 是否也能折叠？注意 `broadcast` 改变 shape，不能简单替换，需要重建 `broadcast` 的目标类型。
2. **加一个 option**：`--tritongpu-fold-splat-layout=only-single-use`，只在原 splat 没有其他 user 时折叠。练习 `let options = [...]` 和 `ADD_PASS_OPTION_WRAPPER_1`。
3. **写一个 analysis**：统计一个 module 里 `convert_layout` 的数量和来源 op，输出到 `llvm::dbgs()`。练习 `ModuleAnalysis` 和 `getAnalysis<>`。
4. **把它做成上游 PR**：写 commit message、补 `test/TritonGPU/`、跑 `ninja check-triton-lit-tests`、说明为什么放在 `remove_layout_conversions` 之前。参考 Ch 32 的上游流程。

## 检核清单

- [ ] `triton-opt --help` 能看到 `--tritongpu-fold-splat-layout`
- [ ] 正例：`convert_layout(splat)` 消失
- [ ] 反例：`convert_layout(load)` 保留
- [ ] 多 use：原 splat 保留，新 splat 出现
- [ ] `lit -v test/TritonGPU/fold-splat-layout.mlir` 通过
- [ ] `passes.ttgpuir.add_fold_splat_layout(pm)` 可调用（加分）
- [ ] 插进 `make_ttgir` 后，最小 kernel 数值不变（加分）
- [ ] 能解释为什么用 `applyPatternsGreedily` 而不是手写 walk
- [ ] 能解释为什么 pass 放在 `remove_layout_conversions` 之前

## 下一章

回到 [Ch 16](./16-pass-infrastructure-and-nvidia-pipeline.md) 看 pass 在完整 pipeline 中的位置；然后去 [Ch 22](./22-tritongpu-to-llvm-conversion.md) 看这些 TTGIR op 最终如何变成 LLVM。
