# 练习 B — analysis + interface

> **目标**：交付一个**可被 lit 断言**的分析 + 一个**可被泛化算法消费**的 op interface：前者用 `DataFlowSolver`（或 `ValueBoundsOpInterface`）算出事实，后者让这个事实能跨方言被查询。你要证明的不是“代码能编过”，而是“换一个 op 就换一个结果，而且这个结果被 CHECK 钉住”。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，源码 `/home/ubuntu/llvm/trees/ct-instr-trunk` @ `e44fff5decc7`（**只读**）。所有实验输出都来自本机。
>
> **前置**：Ch 09（analysis 失效）、Ch 11（DataFlow）、Ch 12（interface），以及 Ch 08（`PatternRewriter` 的安全边界）。
>
> **时间预算**：4–6 小时。其中 1.5 小时是读 `mlir/test/lib` 的现成模板，1 小时写代码，1.5 小时调 lit，1 小时写 patch。

---

## B.1 交付物

| # | 交付物 | 验收方式 |
|---|---|---|
| 1 | 一个 analysis（`DataFlowAnalysis` 子类 **或** 基于 `ValueBoundsConstraintSet` 的查询） | 有一个 test pass 打印它的结果 |
| 2 | 一个 op interface（`.td` + 至少一个实现者） | `dyn_cast` 成功，且至少一个方法返回非默认值 |
| 3 | 一个 lit 测试（`.mlir` + `RUN:` + `CHECK:`） | `llvm-lit -sv` 通过 |
| 4 | 一段 5–10 行的 PR 描述 | 说清“为什么需要这个 analysis / interface” |

**不要求**上游接受。要求是：**你自己能在 30 分钟内从零复现这个结果**，且别人能按你的 lit 测试判断对错。

## B.2 评分表

| 维度 | 及格 | 良好 | 优秀 |
|---|---|---|---|
| analysis 正确性 | 单个 op 上算对 | 跨 block 传播正确 | 处理 region 分支 / call |
| interface 设计 | 能 `dyn_cast` | 有 `defaultImplementation` | 有 `ExternalModel` 且注册点正确 |
| 测试 | 一条 `CHECK` | `split-input-file` + 正负控 | 覆盖“接口未实现 → 保守路径” |
| 可维护性 | 能编过 | 无 `TODO`、命名符合 MLIR 惯例 | 有 doxygen 注释说明 lattice 语义 |

## B.3 两条路线，先选一条

```text
路线 A（推荐）  DataFlowAnalysis 子类
                 抄 TestLivenessAnalysis.cpp → 换成自己的 lattice
路线 B（更快）  ValueBoundsConstraintSet 查询
                 抄 TestReifyValueBounds.cpp → 用 test.reify_bound 当入口

两条路线的接口部分相同：
   .td 定义 OpInterface → tblgen 生成 → test op 实现 → pass 里 dyn_cast
```

**本文两条都覆盖**：B.5 走路线 A，B.7 走路线 B 的验证方式。

## B.4 Step 0：先把现成模板跑通（可实测）

写代码前先用现成 test pass 建立“预期长什么样”的直觉。三个模板各对应一条路线。

### 模板 1：liveness（sparse backward）

```bash
cat > /tmp/mw/pb/live.mlir <<'EOF'
func.func @live(%arg0: memref<i32>) {
  %c0 = arith.constant {tag = "zero"} 0 : i32
  memref.store %c0, %arg0[] : memref<i32>
  %dead = arith.constant {tag = "dead"} 42 : i32
  return
}
EOF
$T/bin/mlir-opt /tmp/mw/pb/live.mlir -test-liveness-analysis
```

```text
test_tag: zero:
 result #0: live
test_tag: dead:
 result #0: not live
```

要抄的三件事：`tag` 属性做选择器、`getAnalysis<RunLivenessAnalysis>()` 拿 analysis、`lattice->print(os)` 输出可断言文本。

### 模板 2：divisibility（sparse forward，且内部消费 interface）

```bash
cat > /tmp/mw/pb/div.mlir <<'EOF'
func.func @muli_constant(%arg0: index) -> index {
  %c4 = arith.constant 4 : index
  %0 = arith.muli %arg0, %c4 : index
  %1 = "test.int_divisibility"(%0) : (index) -> index
  return %1 : index
}
EOF
$T/bin/mlir-opt /tmp/mw/pb/div.mlir --test-int-divisibility-analysis
```

```text
    %1 = "test.int_divisibility"(%0) {divisibility = "udiv = 4, sdiv = 4"} : (index) -> index
```

这个 pass 内部会 `dyn_cast<InferIntDivisibilityOpInterface>` —— 就是 B.7 要复现的模式。

### 模板 3：ValueBounds（路线 B 的入口）

```bash
cat > /tmp/mw/pb/vb.mlir <<'EOF'
func.func @vb(%a: index, %b: index) -> index {
  %0 = affine.apply affine_map<()[s0, s1] -> (s0 + s1)>()[%a, %b]
  %1 = "test.reify_bound"(%0) {type = "EQ"} : (index) -> index
  return %1 : index
}
EOF
$T/bin/mlir-opt /tmp/mw/pb/vb.mlir -pass-pipeline='builtin.module(func.func(test-affine-reify-value-bounds))'
```

```text
#map = affine_map<()[s0, s1] -> (s0 + s1)>
module {
  func.func @vb(%arg0: index, %arg1: index) -> index {
    %0 = affine.apply #map()[%arg0, %arg1]
    %1 = affine.apply #map()[%arg0, %arg1]
    return %1 : index
  }
}
```

`%1` 被替换成**同样的 `affine.apply`**：这就是“把 bound 重新物化”。失败形态同样要看：

```bash
$T/bin/mlir-opt /tmp/mw/pb/vb2.mlir -pass-pipeline='builtin.module(func.func(test-affine-reify-value-bounds))'
```

```text
/tmp/mw/pb/vb2.mlir:2:8: error: 'test.reify_bound' op could not reify bound
```

（`vb2.mlir` 只有一个对函数参数取 `LB` 的 `test.reify_bound`。）**你的 analysis 也要有“失败要说清楚”的路径**，而不是返回 `0` 假装有结果。

## B.5 Step 1：写 analysis（路线 A 骨架）

（本小节代码**未编译实测**：源码树只读，且本任务不重编 `mlir-opt`。骨架与 `TestLivenessAnalysis.cpp` 逐行对应。）

lattice 的三态与单调 join：

```cpp
// 文件：mlir/test/lib/Analysis/DataFlow/TestZeroOriginAnalysis.cpp（你要新建的）
struct ZeroOrigin : public AbstractSparseLattice {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ZeroOrigin)
  using AbstractSparseLattice::AbstractSparseLattice;

  enum State { Unknown, No, Yes };   // Unknown ≠ No！
  State state = Unknown;

  ChangeResult join(const AbstractSparseLattice &other) override {
    const auto &rhs = static_cast<const ZeroOrigin &>(other);
    if (state == Yes || rhs.state == Unknown)
      return ChangeResult::NoChange;
    if (rhs.state == Yes) { state = Yes; return ChangeResult::Change; }
    if (state == No) return ChangeResult::NoChange;
    state = No;
    return ChangeResult::Change;
  }
  void print(raw_ostream &os) const override {
    os << (state == Yes ? "zero" : state == No ? "not-zero" : "unknown");
  }
};
```

三处必须和框架对齐：

| 点 | 为什么 |
|---|---|
| `join` 单调 | 框架靠 `ChangeResult` 决定重新入队；非单调会震荡不收敛 |
| `Unknown` 单独一态 | “没算”不能当“No”，否则提前收敛（Ch 11 的未初始化语义） |
| `print` 输出稳定文本 | lit 的 `CHECK` 依赖它；不要打指针地址 |

transfer function：**先看 interface，再看 op**。

```cpp
struct ZeroOriginAnalysis : public SparseForwardDataFlowAnalysis<ZeroOrigin> {
  using SparseForwardDataFlowAnalysis::SparseForwardDataFlowAnalysis;

  void setToEntryState(ZeroOrigin *lattice) override {
    propagateIfChanged(lattice, lattice->join(ZeroOrigin::No));  // 参数：悲观
  }
  LogicalResult visitOperation(Operation *op,
                               ArrayRef<const ZeroOrigin *> operands,
                               ArrayRef<ZeroOrigin *> results) override {
    if (auto iface = dyn_cast<ZeroOriginOpInterface>(op)) {       // ① 先问接口
      for (ZeroOrigin *res : results)
        propagateIfChanged(res, res->join(iface.resultsAreZero()
                                             ? ZeroOrigin::Yes : ZeroOrigin::No));
      return success();
    }
    for (ZeroOrigin *res : results)                              // ② 保守兜底
      propagateIfChanged(res, res->join(ZeroOrigin::No));
    return success();
  }
};
```

**这是本章的核心设计**：analysis 不认识 `arith.constant`，只知道“谁实现了 `ZeroOriginOpInterface`”。于是 arith 不用改，你在测试方言里实现接口即可。反过来（硬编码 `arith::ConstantOp`）会带来两个问题：analysis 依赖具体方言；每加一个方言都要碰核心文件。

## B.6 Step 2：写 interface（可实测：tblgen 生成）

`.td` 可以完全放在 `/tmp`，`mlir-tblgen` 只依赖 `-I mlir/include`。下面是**真实跑过**的最小接口：

```bash
mkdir -p /tmp/mw/pb
cat > /tmp/mw/pb/MyIface.td <<'EOF'
#ifndef MY_IFACE_TD
#define MY_IFACE_TD

include "mlir/IR/OpBase.td"

def CountingOpInterface : OpInterface<"CountingOpInterface"> {
  let cppNamespace = "::mypkg";
  let description = [{ Report how many "units" an op costs. }];

  let methods = [
    InterfaceMethod<
      /*description=*/"Return the unit cost of this operation.",
      /*retTy=*/"int64_t",
      /*methodName=*/"getUnitCost",
      /*args=*/(ins),
      /*methodBody=*/"",
      /*defaultImplementation=*/[{ return 1; }]
    >,
    InterfaceMethod<
      /*description=*/"Return the name shown in statistics.",
      /*retTy=*/"::llvm::StringRef",
      /*methodName=*/"getCostName",
      /*args=*/(ins)
    >
  ];
}

#endif // MY_IFACE_TD
EOF
$T/bin/mlir-tblgen -gen-op-interface-decls -I mlir/include /tmp/mw/pb/MyIface.td -o /tmp/mw/pb/MyIface.h
$T/bin/mlir-tblgen -gen-op-interface-defs  -I mlir/include /tmp/mw/pb/MyIface.td -o /tmp/mw/pb/MyIface.cpp
wc -l /tmp/mw/pb/MyIface.h /tmp/mw/pb/MyIface.cpp
```

```text
 106 /tmp/mw/pb/MyIface.h
  18 /tmp/mw/pb/MyIface.cpp
```

生成物里能直接看到四种模型的骨架与薄转发：

```text
  struct Concept {
    int64_t (*getUnitCost)(const Concept *impl, ::mlir::Operation *);
    ::llvm::StringRef (*getCostName)(const Concept *impl, ::mlir::Operation *);
  };
  template<typename ConcreteOp> class Model : public Concept { ... };
  template<typename ConcreteOp> class FallbackModel : public Concept { ... };
  template<typename ConcreteModel, typename ConcreteOp>
  class ExternalModel : public FallbackModel<ConcreteModel> { ... };
...
int64_t mypkg::CountingOpInterface::getUnitCost() {
      return getImpl()->getUnitCost(getImpl(), getOperation());
  }
```

三个可验证的结论：

1. **`defaultImplementation` 落在 `FallbackModel`**；`Model<ConcreteOp>` 仍要求实现（或用默认）。
2. **`ExternalModel` 继承 `FallbackModel<ConcreteModel>`**，所以外部模型天然可复用默认实现。
3. **接口方法签名多两个隐藏参数**（`const Concept *impl, Operation *tablegen_opaque_val`）——所以不要手写 `Concept`。

接进测试方言（**未编译实测**：要改 `mlir/test/lib/Dialect/Test/` 并重编，本任务禁止）：

```tablegen
def MyCostOp : TEST_Op<"my_cost", [DeclareOpInterfaceMethods<CountingOpInterface>]> {
  let arguments = (ins I64Attr:$units);
  let assemblyFormat = "$units attr-dict";
}
```

```cpp
int64_t MyCostOp::getUnitCost() { return getUnits().getInt(); }  // 覆盖默认的 1
StringRef MyCostOp::getCostName() { return "my_cost"; }           // 无默认实现，必须写
```

## B.7 Step 3：让 analysis 消费 interface

B.5 的 `visitOperation` 已经展示了消费方式。三个细节决定它能不能过 review：

1. **`dyn_cast` 而不是 `cast`**。没实现接口的 op 必须走保守路径（Ch 12 的“查不到就退化”）。
2. **接口方法要能失败**。用 `FailureOr<T>` 或返回 `std::optional`，不要用“返回 0 表示没有”。
3. **结果写回 lattice 必须 `propagateIfChanged`**。直接 `res->state = ...` 是静默不收敛的头号原因。

对照 `-test-int-divisibility-analysis` 的真实行为：它对不认识的 op 返回“divisibility = 1”（无信息），而不是报错。你的 analysis 也应该这样——**保守 ≠ 错误**。

## B.8 Step 4：测试（可实测的部分）

### 4.1 先用现成 pass 校准预期格式

```bash
$T/bin/mlir-opt /tmp/mw/pb/div.mlir --test-int-divisibility-analysis | grep divisibility
```

```text
    %1 = "test.int_divisibility"(%0) {divisibility = "udiv = 4, sdiv = 4"} : (index) -> index
```

### 4.2 你的 lit 测试

```mlir
// RUN: mlir-opt %s -test-zero-origin --split-input-file | FileCheck %s

// CHECK-LABEL: func @const_zero
func.func @const_zero() -> index {
  // CHECK: test_tag: c0
  // CHECK-NEXT: result #0: zero
  %c0 = arith.constant {tag = "c0"} 0 : index
  return %c0 : index
}

// -----
// CHECK-LABEL: func @const_one
func.func @const_one() -> index {
  // CHECK: test_tag: c1
  // CHECK-NEXT: result #0: not-zero
  %c1 = arith.constant {tag = "c1"} 1 : index
  return %c1 : index
}
```

三个要点：`split-input-file` 隔离用例；`CHECK-LABEL` 用函数名定位、`CHECK-NEXT` 钉相邻行（不要用裸 `CHECK`）；**必须有负控**——只测正例的 analysis 很容易“永远返回 Yes”而全绿。

### 4.3 运行与已验证的替代检查

```bash
# 重编后（本任务未执行，源码树只读）
ninja -C $T mlir-opt
$T/bin/llvm-lit -sv /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Analysis/DataFlow/zero-origin.mlir
```

预期 `1 passed`。**未实测**：需新增文件 + 重编。

重编前可以用现成 pass 确认你的锚点理解：

```bash
$T/bin/mlir-opt /tmp/mw/ch12.mlir -pass-pipeline='builtin.module(func.func(test-interface-pass))'
```

```text
/tmp/mw/ch12.mlir:1:1: remark: Executing interface pass on operation
```

```bash
$T/bin/mlir-opt /tmp/mw/ch12.mlir -pass-pipeline='builtin.module(test-interface-pass)'
```

```text
<unknown>:0: error: unable to schedule pass '{anonymous}::TestInterfacePass' on a PassManager intended to run on 'builtin.module'!
```

第二种错说明问题在锚点，不在接口。

## B.9 Step 5：写 patch

固定形状（细节见 Ch 30）：

```text
mlir/test/lib/Dialect/Test/TestOps.td                       + 8   DeclareOpInterfaceMethods
mlir/test/lib/Dialect/Test/TestOps.cpp                      + 6   方法实现
mlir/test/lib/Analysis/DataFlow/TestZeroOriginAnalysis.cpp  + 90  新文件
mlir/test/lib/Analysis/DataFlow/CMakeLists.txt              + 1
mlir/test/Analysis/DataFlow/zero-origin.mlir                + 24  新测试
```

PR 描述模板：

```text
[mlir][test] Add ZeroOriginAnalysis and ZeroOriginOpInterface

Adds a data-flow analysis proving whether an SSA value originates from a
zero constant, plus an op interface letting dialects report this without the
analysis knowing about them. Exposed via `-test-zero-origin` for lit tests.

Testing: llvm-lit -sv mlir/test/Analysis/DataFlow/zero-origin.mlir
```

## B.10 参考解答

<details>
<summary>参考解答 1：analysis + test pass 的骨架（B.5 的完整形态）</summary>

```cpp
//===- TestZeroOriginAnalysis.cpp - zero-origin dataflow analysis ---------===//
#include "mlir/Analysis/DataFlow/SparseAnalysis.h"
#include "mlir/Analysis/DataFlowFramework.h"
#include "mlir/IR/BuiltinAttributes.h"
#include "mlir/Pass/Pass.h"

using namespace mlir;
using namespace mlir::dataflow;

namespace {
struct ZeroOrigin : public AbstractSparseLattice {          // 见 B.5
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ZeroOrigin)
  using AbstractSparseLattice::AbstractSparseLattice;
  enum State { Unknown, No, Yes };
  State state = Unknown;
  ChangeResult join(const AbstractSparseLattice &other) override;  // 单调 join
  void print(raw_ostream &os) const override;                     // "zero"/"not-zero"
};

struct ZeroOriginAnalysis : public SparseForwardDataFlowAnalysis<ZeroOrigin> {
  using SparseForwardDataFlowAnalysis::SparseForwardDataFlowAnalysis;
  void setToEntryState(ZeroOrigin *lattice) override;             // 参数 = No
  LogicalResult visitOperation(Operation *, ArrayRef<const ZeroOrigin *>,
                               ArrayRef<ZeroOrigin *>) override;  // 先 dyn_cast 接口
};

struct TestZeroOriginPass : public PassWrapper<TestZeroOriginPass,
                                               OperationPass<>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(TestZeroOriginPass)
  StringRef getArgument() const override { return "test-zero-origin"; }

  void runOnOperation() override {
    Operation *op = getOperation();
    DataFlowSolver solver;
    solver.load<ZeroOriginAnalysis>();
    if (failed(solver.initializeAndRun(op)))
      return signalPassFailure();

    op->walk([&](Operation *tagged) {
      auto tag = tagged->getDiscardableAttrOfType<StringAttr>("tag");
      if (!tag)
        return;
      llvm::outs() << "test_tag: " << tag.getValue() << ":\n";
      for (auto [i, res] : llvm::enumerate(tagged->getResults())) {
        const ZeroOrigin *lat = solver.lookupState<ZeroOrigin>(
            solver.getProgramPointAfter(tagged));   // 不是 Before！
        llvm::outs() << " result #" << i << ": ";
        if (lat) lat->print(llvm::outs());
        else llvm::outs() << "no-state";
        llvm::outs() << "\n";
      }
    });
  }
};
} // namespace

namespace mlir::test {
void registerTestZeroOriginPass() { PassRegistration<TestZeroOriginPass>(); }
} // namespace mlir::test
```

**两个踩点**：① 查 result 的 lattice 用 `getProgramPointAfter`，用 `Before` 会拿到 operands 的 state；② 注册函数要加进 `mlir/test/lib/Analysis/DataFlow/CMakeLists.txt` 的 `registerTestPasses`，否则 `mlir-opt` 里看不到 `-test-zero-origin`。

</details>

<details>
<summary>参考解答 2：interface + 两个实现者（含 ExternalModel）</summary>

```tablegen
// 你的接口（也可以用 B.6 的 /tmp/MyIface.td，名字换成 ZeroOriginOpInterface）
def ZeroOriginOpInterface : OpInterface<"ZeroOriginOpInterface"> {
  let cppNamespace = "::mlir::test";
  let description = [{ Reports whether an op's result is provably a zero constant. }];
  let methods = [
    InterfaceMethod<
      /*description=*/"Return true if all results are provably zero.",
      /*retTy=*/"bool",
      /*methodName=*/"resultsAreZero",
      /*args=*/(ins),
      /*methodBody=*/"",
      /*defaultImplementation=*/[{ return false; }]
    >
  ];
}
```

```cpp
// 实现者 1：直接实现（改 ODS 的 interface 列表）
// def ZeroOp : TEST_Op<"zero", [DeclareOpInterfaceMethods<ZeroOriginOpInterface>]>
bool ZeroOp::resultsAreZero() { return true; }

// 实现者 2：ExternalModel（不改原 op 定义）
struct ArithConstZeroModel
    : public ZeroOriginOpInterface::ExternalModel<ArithConstZeroModel,
                                                  arith::ConstantOp> {
  bool resultsAreZero(Operation *op) const {
    auto cst = cast<arith::ConstantOp>(op);
    auto intAttr = dyn_cast<IntegerAttr>(cst.getValue());
    return intAttr && intAttr.getValue().isZero();
  }
};

// 注册点：必须在一个“context 已经建好、方言已注册”的地方
void registerZeroOriginExternalModels(DialectRegistry &registry) {
  registry.addExtension(+[](MLIRContext *ctx, arith::ArithDialect *) {
    arith::ConstantOp::attachInterface<ArithConstZeroModel>(*ctx);
  });
}
```

**为什么用 `addExtension`**：它把“给 arith 挂模型”写到你的库里，而不是改 arith 的源码。这正是 Ch 12 说的“不能改 op 定义就挂 ExternalModel”。

</details>

<details>
<summary>参考解答 3：lit 测试与三条易错 CHECK</summary>

```mlir
// RUN: mlir-opt %s -test-zero-origin --split-input-file | FileCheck %s

// CHECK-LABEL: func @const_zero
func.func @const_zero() -> index {
  // CHECK: test_tag: c0
  // CHECK-NEXT: result #0: zero
  %c0 = arith.constant {tag = "c0"} 0 : index
  return %c0 : index
}

// -----

// CHECK-LABEL: func @const_one
func.func @const_one() -> index {
  // CHECK: test_tag: c1
  // CHECK-NEXT: result #0: not-zero
  %c1 = arith.constant {tag = "c1"} 1 : index
  return %c1 : index
}

// -----

// CHECK-LABEL: func @no_interface
func.func @no_interface(%n: index) -> index {
  // CHECK: test_tag: add
  // CHECK-NEXT: result #0: not-zero
  %0 = arith.addi %n, %n {tag = "add"} : index
  return %0 : index
}
```

三条易错点：

1. `CHECK-NEXT` 紧跟 `CHECK`，中间不能有空行注释；格式化后容易被换行破坏。
2. `--split-input-file` 必须加，否则三个 `@const_*` 会在同一个 module 里，`CHECK-LABEL` 仍然能定位，但错误信息会难读得多。
3. 第三个用例是**接口未实现**的负控：`arith.addi` 没实现 `ZeroOriginOpInterface`，必须走保守路径输出 `not-zero`。没有这条，你的 analysis 可能“永远返回 zero”而测试全绿。

</details>

<details>
<summary>参考解答 4：常见失败与对照修法</summary>

| 现象 | 根因 | 修法 |
|---|---|---|
| `test pass 没输出` | 没有 `tag` 属性 | 加 `{tag = "..."}`，或在 pass 里 walk 所有 op |
| 分析结果永远是 `unknown` | `initializeAndRun` 失败 / 没 load analysis | 检查 `solver.load<T>()` 与返回值 |
| 结果在第二个函数里不对 | 跨函数传播（solver 的 `isInterprocedural`） | 在 `func.func` 上建 solver，或显式处理 `CallOpInterface` |
| `dyn_cast` 总是空 | 接口没注册 / 挂错 op 类型 | `--show-dialects` 确认方言；检查 `attachInterface` 的模板参数 |
| `attachInterface` 报重复注册 | 在 pass 里调了 | 移到 `Dialect::initialize` 或 `addExtension` |
| lit 报 `expected string not found` | `CHECK-NEXT` 跨了空行 | 改用 `CHECK`，或调整打印格式 |

</details>

<details>
<summary>参考解答 5：如果只做路线 B（ValueBounds），交付物怎么变</summary>

不用写 `DataFlowAnalysis`，改成：① 在测试方言里加一个实现 `ValueBoundsOpInterface` 的 op；② 用 `ValueBoundsConstraintSet::computeConstantBound`/`computeBound` 写查询；③ 用 `test.reify_bound` 或自写 pass 输出结果。

已验证的参照（B.4 模板 3）：`%1 = "test.reify_bound"(%0) {type = "EQ"}` 被替换成 `%1 = affine.apply #map()[%arg0, %arg1]`；失败时报 `'test.reify_bound' op could not reify bound`。

优势是不用碰 DataFlow 框架；劣势是只能回答“某方向的界”，不能回答任意 lattice 属性。

</details>

## B.11 自检清单

- [ ] analysis 的 `join` 是单调的，且有独立的 `Unknown` 状态
- [ ] 每次修改 lattice 都走 `propagateIfChanged`
- [ ] interface 用 `dyn_cast` 消费，未实现时走保守路径
- [ ] interface 有 `defaultImplementation` 或明确的“必须实现”理由
- [ ] lit 测试同时包含正例与负控，且用了 `split-input-file`
- [ ] tester/测试命令用绝对路径或 `$T/bin`，不依赖 `PATH`
- [ ] patch 里 CMakeLists 与测试文件都加了
- [ ] PR 描述写了“为什么需要这个 interface”，而不只是“新增了什么”

## B.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Analysis/DataFlow/TestLivenessAnalysis.cpp`**
  - 读哪里：`30-90`
  - 能学到什么：analysis + pass + 文本输出三件套的最小组合；和本章关联：B.5/B.10-1
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Analysis/DataFlow/TestIntegerDivisibilityAnalysis.cpp`**
  - 读哪里：全文
  - 能学到什么：analysis 如何消费 `InferIntDivisibilityOpInterface`；和本章关联：B.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/DLTI/DLTIOpInterfaces.td`**
  - 读哪里：`14-57`
  - 能学到什么：带默认实现的接口范本；和本章关联：B.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Affine/TestReifyValueBounds.cpp`**
  - 读哪里：`77-180`
  - 能学到什么：`ValueBoundsConstraintSet` 的 stop condition 与 reify 流程；和本章关联：B.4 模板 3、B.10-5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Test/TestTypes.cpp`**
  - 读哪里：`411-423`
  - 能学到什么：`ExternalModel` + `attachInterface` 的最小可编译样例；和本章关联：B.10-2

练习 B 把 Ch 09–12 串成一条闭环：pass 驱动 analysis，analysis 消费 interface，interface 由 tblgen 生成、用 lit 钉住。下一章进入 Part 3，从 `.td` 这一侧重看整条链：`mlir-tblgen` 到底生成了什么，以及为什么“只改一行 ODS”会改变 ABI。

→ 下一章：[Ch 14 ODS 与生成代码](./14-ods-and-generated-code.md)
