# Ch 21 — 动态 legality 与 unrealized_conversion_cast

> **目标**：理解“partial conversion 成功但 IR 没降干净”这一现象的成因；能用 `markUnknownOpDynamicallyLegal` 写出正确的局部 lowering；能读懂并清理 `builtin.unrealized_conversion_cast`；能从 `failed to legalize` 的三种诊断反推出契约错在哪。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance`；`mlir-opt` 带 `-test-legalize-patterns`、`-convert-scf-to-cf`、`--convert-to-llvm`、`-reconcile-unrealized-casts`。

> **前置**：Ch 19 的 legality 三态、Ch 20 的 `TypeConverter` 与 materialization。

## 21.1 为什么 partial conversion 会“成功但留下垃圾”

Ch 19 说过：full conversion 要求**所有** op 合法，partial 只要求**被显式标非法**的 op 被消掉。这个差别在实际 lowering 里非常要命：

```bash
# 一段带 tensor 参数的代码
mlir-opt tensorarg.mlir --convert-to-llvm
```

结果不是报错，而是退出码 0，并且留下这种 IR：

```text
module {
  func.func @f(%arg0: tensor<4xf32>) -> f32 {
    %0 = llvm.mlir.constant(0 : i64) : i64
    %1 = builtin.unrealized_conversion_cast %0 : i64 to index
    %extracted = tensor.extract %arg0[%1] : tensor<4xf32>
    llvm.return %extracted : f32
  }
}
```

注意三件事同时成立：`func.func` 没被换掉、`func.return` 变成了 `llvm.return`、中间多了一个 `builtin.unrealized_conversion_cast`。**这份 IR 是自相矛盾的**，但 `mlir-opt` 认为 pass 成功了。

原因在 `--convert-to-llvm` 的实现：它用 `applyPartialConversion`，target 里只把 LLVM dialect 标成 legal（`mlir/lib/Conversion/ConvertToLLVM/ConvertToLLVMPass.cpp:118`、`:136`），既没有把 `func`/`tensor` 标成 illegal，也没有 `markUnknownOpDynamicallyLegal`。于是：

- `func.func` 的签名转换失败 → 它不是 “explicitly illegal” → partial 模式放过它；
- `func.return` 的 pattern 成功 → 换成 `llvm.return`；
- 索引常量被转成 `i64`，但 `tensor.extract` 需要 `index` → 插入 `unrealized_conversion_cast`。

一个 pass，三种结果并存。这就是本章要讲的全部问题。

## 21.2 先建立直觉：四态 legality 与“欠条”

`ConversionTarget` 的注释（`mlir/include/mlir/Transforms/DialectConversion.h:1266`）明确说 legality 是**四态**：

```
                    ┌──────────────────────────────┐
                    │  某个 op 实例的 legality      │
                    └──────────────┬───────────────┘
                                   │
        ┌──────────────┬───────────┴──────────┬────────────────┐
        ▼              ▼                      ▼                ▼
  Legal(rec)     Legal(not rec)          Illegal           Unknown
  不进 region    只本身合法          partial/full 都失败   取决于模式
        │              │                      │                │
        │              │                      │        ┌───────┴────────┐
        │              │                      │        ▼                ▼
        │              │                      │   partial: 放过     full: 失败
        │              │                      │   （记入 unlegalizedOps）
        └──────────────┴──────────────────────┴────────────────────────┘
```

“Unknown” 不是一个显式枚举值，而是 `getOpInfo` 返回 `nullopt`：既没给这个 op 设 action，也没给它的 dialect 设 action，也没有 unknown 回调。

`ConversionTarget::isLegal`（`DialectConversion.cpp:3999`）与 `isIllegal`（`:4034`）对 Unknown 的处理是不对称的：

| 查询 | Unknown 时的返回 |
|---|---|
| `isLegal(op)` | `nullopt`（不知道） |
| `isIllegal(op)` | `false`（**不**非法） |

partial conversion 只在 `isIllegal(op)` 为真时才把失败升级为错误（`DialectConversion.cpp:3340` 附近）。所以 Unknown = “partial 会放过，full 会失败”。

### 欠条：unrealized_conversion_cast

当 driver 需要 materialization 但类型转换器没有提供回调时，它不会立刻失败，而是插一个 `builtin.unrealized_conversion_cast` 当“欠条”，等 conversion 结束后再统一结算（`mlir/docs/DialectConversion.md:543`）：

```
   转换中                                    转换结束后
┌──────────────────┐                   ┌──────────────────────────┐
│ %new : f64       │                   │ 1. 有 callback → 换成真 cast │
│   ↓ (欠条)        │   ───────────▶    │ 2. 链式可消 → 直接替换       │
│ %old : f32       │                   │ 3. 仍活着 → 报错/留给后面    │
└──────────────────┘                   └──────────────────────────┘
```

“欠条”这个词很重要：**它不代表类型安全**，只代表“我知道这里类型不匹配，但我先记下来”。任何在 pipeline 里长期存活的 `unrealized_conversion_cast` 都是待还的债。

## 21.3 unknown op 的默认语义

`ConversionTarget::getOpInfo`（`DialectConversion.cpp:4105`）的查找顺序是：

1. 这个 op 有没有单独设置 action；
2. 它所属 dialect 有没有设置 action；
3. 有没有 `markUnknownOpDynamicallyLegal` 注册的全局回调；
4. 都没有 → 返回 `nullopt`。

第 3 步是关键。`markUnknownOpDynamicallyLegal`（`DialectConversion.h:1236`）注册的是一个**全局兜底回调**，它只对“前两步都没命中”的 op 生效。

回调可以叠加：`composeLegalityCallbacks`（`DialectConversion.cpp:4050`）让新回调先问，返回 `nullopt` 时再问旧回调。所以多次调用 `markUnknownOpDynamicallyLegal` 不是覆盖，而是**链式回退**。“后注册的会覆盖前面的”这种直觉是错的。

## 21.4 两种真实写法

树里 24 个文件用了 `markUnknownOpDynamicallyLegal`（`grep -rl ... mlir/lib | wc -l`）。它们基本落在两种模式。

### 模式 A：只降我这几个 op，其它一律放行

`SCFToControlFlow`（`mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp:757`）：

```cpp
target.addIllegalOp<scf::ForallOp, scf::ForOp, scf::IfOp, scf::IndexSwitchOp,
                    scf::ParallelOp, scf::WhileOp, scf::ExecuteRegionOp>();
target.markUnknownOpDynamicallyLegal([](Operation *) { return true; });
```

语义非常清楚：**只有列出来的 op 必须消失，其余全部合法**。这是“局部 lowering pass”的标准写法，也是最容易 review 的写法。

实测（`scf.mlir` 的输入见 21.9 实验 8）：

```bash
$T/bin/mlir-opt scf.mlir -convert-scf-to-cf
```

本机实测：

```text
module {
  func.func @loop(%arg0: index, %arg1: i32) -> i32 {
    %c1 = arith.constant 1 : index
    cf.br ^bb1(%c1, %arg1 : index, i32)
  ^bb1(%0: index, %1: i32):  // 2 preds: ^bb0, ^bb2
    %2 = arith.cmpi slt, %0, %arg0 : index
    cf.cond_br %2, ^bb2, ^bb3
  ^bb2:  // pred: ^bb1
    %3 = arith.addi %1, %1 : i32
    %4 = arith.addi %0, %c1 overflow<nsw> : index
    cf.br ^bb1(%4, %3 : index, i32)
  ^bb3:  // pred: ^bb1
    return %1 : i32
  }
}
```

`scf.for` 没了，`arith.*` 和 `func.func` 原样保留，也没有欠条——这正是“partial 只保证我承诺的部分”。

### 模式 B：按 dialect 排除

`ControlFlowToLLVM`（`mlir/lib/Conversion/ControlFlowToLLVM/ControlFlowToLLVM.cpp:288`）需要改其它 op 的 block signature，但不想降它们：

```cpp
target.markUnknownOpDynamicallyLegal([&](Operation *op) {
  return op->getDialect() != ctx->getLoadedDialect<cf::ControlFlowDialect>();
});
```

含义是：**cf dialect 的 op 非法（必须被这个 pass 降掉），其它 dialect 全部合法**。比模式 A 更精确，因为它没有枚举 op 列表，新增 cf op 会自动被覆盖。

### 递归合法：不要再往下钻

`markOpRecursivelyLegal`（`DialectConversion.h:1183`）是第三种工具：把一个 op 标成“连同内部 region 一起合法”。这不是偷懒，而是契约——比如 `builtin.module` 里已经是 LLVM IR、由另一个 pass 负责。

实测（full conversion，但内部 illegal op 仍然存活；`reclegal.mlir` 见 21.9 实验 9）：

```bash
$T/bin/mlir-opt reclegal.mlir -allow-unregistered-dialect \
  -test-legalize-patterns='test-legalize-mode=full'
```

本机实测：

```text
module {
  func.func @recursively_legal_invalid_op() {
    builtin.module attributes {test.recursively_legal} {
      %0 = "test.illegal_op_f"() : () -> i32
    }
    builtin.module {
      func.func @dynamic_func(%arg0: f64) attributes {test.recursively_legal} {
        %0 = "test.illegal_op_f"() : () -> i32
        "test.return"() : () -> ()
      }
    }
    "test.return"() : () -> ()
  }
}
```

`test.illegal_op_f` 在递归合法区域内被忽略；但 `dynamic_func` 的参数 `i64` 在它“变成递归合法”之前已经被签名转换改成 `f64`。这说明递归合法只影响**legality 判定**，不影响 pattern 是否已经跑过。

## 21.5 `unrealized_conversion_cast` 的生命周期

一个 cast 从生到死有四条路：

| 阶段 | 谁负责 | 结果 |
|---|---|---|
| 需要 materialization 但没有回调 | driver | 插 `unrealized_conversion_cast` |
| materialization 回调存在 | `TypeConverter` | 换成真正的 op（如 `test.cast`） |
| 链式冗余 | `reconcileUnrealizedCasts` | 直接替换成源值 |
| 结算后仍有活用户 | driver / 后续 pass | partial 可能留；full 报错 |

“链式冗余”指这种情况：

```mlir
%0 = builtin.unrealized_conversion_cast %a : i32 to i64
%1 = builtin.unrealized_conversion_cast %0 : i64 to i32
```

两个 cast 的输入/输出类型对上了（`i32 -> i64 -> i32`），可以整体删掉。`reconcileUnrealizedCastsImpl`（`DialectConversion.cpp:3130`）的做法是自底向上遍历，找“输入类型等于当前 cast 结果类型”的上游 cast，然后 `replaceAllUsesWith`。

实测（`casts.mlir` 见 21.9 实验 5）：

```bash
$T/bin/mlir-opt casts.mlir -reconcile-unrealized-casts
```

本机实测（转换前 → 转换后）：

```text
// before
    %0 = builtin.unrealized_conversion_cast %arg0 : i32 to i64
    %1 = builtin.unrealized_conversion_cast %0 : i64 to i32
    %2 = builtin.unrealized_conversion_cast %arg1 : f32 to f32
    %3 = arith.addi %1, %arg0 : i32

// after
    %0 = arith.addi %arg0, %arg0 : i32
```

三个 cast 全消失：往返链被折叠、同类型 cast 被删。注意这个 pass **不做类型检查**，它只按照“类型对得上就替换”的规则工作；所以它能清理欠条，但不能证明欠条是对的。

## 21.6 结算失败：三种诊断

`legalizeUnresolvedMaterialization`（`DialectConversion.cpp:3360`）产生两类错误，加上 full/partial 的 legalize 失败，一共三种常见诊断。

三种诊断可以放在一张表里（消息均为本机实测原文）：

| 诊断 | 原文关键片段 | 含义 / 修法 |
|---|---|---|
| 1 | `unresolved source materialization from ('f64') to ('f16') ... (no matching callback)` | 有 materialization 注册，但没有回调接受这组类型 → 补一条 |
| 2 | `unresolved materialization from ('f64') to ('f32') ... (no type converter specified)` | pattern 没绑定 `TypeConverter` → 给 pattern 传 converter |
| 3 | `failed to legalize operation 'scf.for' that was explicitly marked illegal` | partial 下被 `addIllegalOp` 标过且 pattern 没匹配 → 补 pattern 或改 target |

诊断 3 的 `that was explicitly marked illegal` 只在 **partial** 出现；full conversion 的同一错误没有这半句（`DialectConversion.cpp:3332` 的分支差异）。看错诊断就会改错地方。

## 21.7 analysis conversion：只问“能不能降”

`applyAnalysisConversion`（`DialectConversion.h:1547`，实现 `DialectConversion.cpp:4311`）不修改 IR，只把“可合法化”的 op 收进 `config.legalizableOps`。

实测：

```bash
$T/bin/mlir-opt test-legalizer-analysis.mlir -allow-unregistered-dialect \
  -test-legalize-patterns='test-legalize-mode=analysis'
```

本机实测（节选）：

```text
remark: op 'func.func' is legalizable
remark: op 'test.illegal_op_a' is legalizable
remark: op 'builtin.module' is legalizable
remark: op 'test.invalid' is legalizable
```

**没有一条 IR 被改**，而且 remark 顺序不是源码顺序而是 driver 的递归顺序。想知道“我的 pattern 覆盖了哪些 op”，跑一次 analysis 比读 pattern 列表快。

## 21.8 源码落点

| 文件 | 行 | 读什么 |
|---|---|---|
| `mlir/include/mlir/Transforms/DialectConversion.h` | 1236 | `markUnknownOpDynamicallyLegal` |
| 同上 | 1183 | `markOpRecursivelyLegal` |
| 同上 | 1266 | legality 四态的注释 |
| 同上 | 1383 | `ConversionConfig`（`unlegalizedOps` / `legalizableOps` / `allowPatternRollback`） |
| `mlir/lib/Transforms/Utils/DialectConversion.cpp` | 3999 | `ConversionTarget::isLegal` |
| 同上 | 4034 | `ConversionTarget::isIllegal`（Unknown → false） |
| 同上 | 4050 | `composeLegalityCallbacks`（链式回退） |
| 同上 | 4105 | `getOpInfo` 的四级查找 |
| 同上 | 3332–3350 | full / partial / analysis 的失败分支 |
| 同上 | 3130 | `reconcileUnrealizedCastsImpl` |
| 同上 | 3360 | `legalizeUnresolvedMaterialization` |
| `mlir/lib/Conversion/SCFToControlFlow/SCFToControlFlow.cpp` | 757 | 模式 A 的真实写法 |
| `mlir/lib/Conversion/ControlFlowToLLVM/ControlFlowToLLVM.cpp` | 288 | 模式 B 的真实写法 |

## 21.9 可跑实验

统一前置：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
mkdir -p /tmp/mlir-part4 && cd /tmp/mlir-part4
```

### 实验 1：partial conversion 留下矛盾 IR

```bash
cat > tensorarg.mlir <<'EOF'
func.func @f(%t: tensor<4xf32>) -> f32 {
  %c0 = arith.constant 0 : index
  %v = tensor.extract %t[%c0] : tensor<4xf32>
  return %v : f32
}
EOF
$T/bin/mlir-opt tensorarg.mlir --convert-to-llvm; echo "exit=$?"
```

本机实测：

```text
module {
  func.func @f(%arg0: tensor<4xf32>) -> f32 {
    %0 = llvm.mlir.constant(0 : i64) : i64
    %1 = builtin.unrealized_conversion_cast %0 : i64 to index
    %extracted = tensor.extract %arg0[%1] : tensor<4xf32>
    llvm.return %extracted : f32
  }
}
exit=0
```

**退出码 0** 是这节最重要的一行。

### 实验 2：把矛盾推迟到 translate 阶段

```bash
$T/bin/mlir-opt tensorarg.mlir --convert-to-llvm -o tensorarg.llvm.mlir
$T/bin/mlir-translate --mlir-to-llvmir tensorarg.llvm.mlir 2>&1 | head -3
```

本机实测：

```text
tensorarg.llvm.mlir:5:18: error: Dialect `tensor' not found for custom op 'tensor.extract' 
    %extracted = tensor.extract %arg0[%1] : tensor<4xf32>
```

第二个工具才报错。这就是“partial 的沉默成本”：错误被推到了 pipeline 的下一个消费者。

### 实验 3：partial 放过 unknown，full 不放过

```bash
cat > nomat.mlir <<'EOF'
func.func @dropped_input_in_use(%arg: i16, %arg2: i64) {
  "work"(%arg) : (i16) -> ()
}
EOF
echo '--- partial'
$T/bin/mlir-opt nomat.mlir -allow-unregistered-dialect -test-legalize-patterns='test-legalize-mode=partial'
echo '--- full'
$T/bin/mlir-opt nomat.mlir -allow-unregistered-dialect -test-legalize-patterns='test-legalize-mode=full' 2>&1 | head -3
```

本机实测：

```text
--- partial
module {
  func.func @dropped_input_in_use(%arg0: f64) {
    %0 = "test.cast"() : () -> i16
    "work"(%0) : (i16) -> ()
  }
}
--- full
nomat.mlir:2:3: error: failed to legalize operation 'work': "work"(<<UNKNOWN SSA VALUE>>) : (i16) -> ()
  "work"(%arg) : (i16) -> ()
```

同一份 IR、同一个 pass，只改 mode：partial 成功（`work` 是 unknown，放过），full 失败（任何 op 不合法都失败）。

### 实验 4：让欠条显形

```bash
$T/bin/mlir-opt nomat.mlir -allow-unregistered-dialect \
  -test-legalize-patterns='allow-pattern-rollback=0 build-materializations=0 attach-debug-materialization-kind=1'
```

本机实测：

```text
module {
  func.func @dropped_input_in_use(%arg0: f64) {
    %0 = builtin.unrealized_conversion_cast to i16 {__kind__ = "source"}
    "work"(%0) : (i16) -> ()
  }
}
```

`{__kind__ = "source"}` 是测试专用属性，告诉你这条欠条是 source materialization。生产代码里没有这个属性，但 cast 的形状一样。

### 实验 5：结算欠条

```bash
cat > casts.mlir <<'EOF'
func.func @roundtrip(%a: i32, %b: f32) -> i32 {
  %0 = builtin.unrealized_conversion_cast %a : i32 to i64
  %1 = builtin.unrealized_conversion_cast %0 : i64 to i32
  %2 = builtin.unrealized_conversion_cast %b : f32 to f32
  %3 = arith.addi %1, %a : i32
  return %3 : i32
}
EOF
$T/bin/mlir-opt casts.mlir -reconcile-unrealized-casts
```

本机实测：

```text
module {
  func.func @roundtrip(%arg0: i32, %arg1: f32) -> i32 {
    %0 = arith.addi %arg0, %arg0 : i32
    return %0 : i32
  }
}
```

### 实验 6：analysis 模式不改 IR

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Transforms/test-legalizer-analysis.mlir \
  -allow-unregistered-dialect -test-legalize-patterns='test-legalize-mode=analysis' 2>&1 \
  | grep remark | head -4
```

本机实测（去掉文件名前缀）：

```text
remark: op 'func.func' is legalizable
remark: op 'test.illegal_op_a' is legalizable
remark: op 'builtin.module' is legalizable
remark: op 'test.invalid' is legalizable
```

### 实验 7：rollback 的代价

```bash
$T/bin/mlir-opt mlir/test/Transforms/test-legalizer-rollback.mlir \
  -allow-unregistered-dialect -split-input-file -test-legalize-patterns 2>&1 | head -8
```

本机实测（节选，已去掉路径前缀）：

```text
test-legalizer-rollback.mlir:7:3: error: failed to legalize operation 'test.region_builder' that was explicitly marked illegal: "test.region_builder"() : () -> ()
  "test.region_builder"() : () -> ()
test-legalizer-rollback.mlir:4:1: remark: applyPartialConversion failed
```

`remark: applyPartialConversion failed` 是 test driver 自己加的汇总；真正的错误仍是 per-op 的 `failed to legalize`。

### 实验 8：只降 SCF，其它放行

```bash
cat > scf.mlir <<'EOF'
func.func @loop(%n: index, %init: i32) -> i32 {
  %c1 = arith.constant 1 : index
  %r = scf.for %i = %c1 to %n step %c1 iter_args(%acc = %init) -> i32 {
    %s = arith.addi %acc, %acc : i32
    scf.yield %s : i32
  }
  return %r : i32
}
EOF
$T/bin/mlir-opt scf.mlir -convert-scf-to-cf
```

输出与 21.4 引用的完全一致：`scf.for` 变成 `cf.br`/`cf.cond_br`，`arith.*` 原样保留。

### 实验 9：递归合法

```bash
cat > reclegal.mlir <<'EOF'
func.func @recursively_legal_invalid_op() {
  builtin.module attributes {test.recursively_legal} {
    %ignored = "test.illegal_op_f"() : () -> (i32)
  }
  builtin.module {
    func.func @dynamic_func(%arg: i64) attributes {test.recursively_legal} {
      %ignored = "test.illegal_op_f"() : () -> (i32)
      "test.return"() : () -> ()
    }
  }
  "test.return"() : () -> ()
}
EOF
$T/bin/mlir-opt reclegal.mlir -allow-unregistered-dialect \
  -test-legalize-patterns='test-legalize-mode=full'
```

输出与 21.4 引用的完全一致：递归合法区域里的 `test.illegal_op_f` 存活，`dynamic_func` 的参数已变成 `f64`。

## 21.10 踩雷集锦

1. **“`--convert-to-llvm` 成功就等于降完了” → 不等于。** 它是 partial conversion，只保证 LLVM dialect 合法；`func`/`tensor`/自定义 op 可能原样留下。

2. **“Unknown 等于 illegal” → 不等于。** `isIllegal` 对 Unknown 返回 `false`；partial 因此放过它，full 才失败。

3. **“多次 `markUnknownOpDynamicallyLegal` 会覆盖” → 是链式回退。** 新回调返回 `nullopt` 时会落到旧回调。

4. **“`unrealized_conversion_cast` 只是临时语法糖” → 是待还的债。** 它活在最终 IR 里就说明类型契约没闭合；`reconcile` 只能消冗余，不能证明正确。

5. **“`reconcile-unrealized-casts` 会做类型检查” → 不会。** 它只按“输入类型 == 输出类型”做替换，错误的 cast 链也会被折叠掉。

6. **“递归合法 = 跳过 pattern” → 只是跳过 legality 判定。** 递归合法区域内已经跑过的 pattern 不会被撤销（见 21.4 的 `dynamic_func` 例子）。

7. **“partial 失败信息一定带 `explicitly marked illegal`” → 只有 partial 带。** full 的同一错误没有这半句，看错会误判模式。

8. **“analysis conversion 会顺手修 IR” → 完全只读。** 它只填 `legalizableOps`，不改一个 op。

## 21.11 本章重点整理

- legality 是四态；Unknown 在 partial 与 full 下行为不同，这是“成功但没降干净”的根源。
- `markUnknownOpDynamicallyLegal` 是全局兜底，且多次注册是链式回退而不是覆盖。
- 局部 lowering 有两种标准写法：枚举 illegal + 全放行；或按 dialect 排除。
- `markOpRecursivelyLegal` 只影响 legality 判定，不影响已应用的 pattern。
- `unrealized_conversion_cast` 是 materialization 的欠条；三种失败诊断对应三种不同的修法。
- `reconcile-unrealized-casts` 只做结构折叠，不做语义证明。
- analysis conversion 是只读的“覆盖范围探针”，适合在改 pattern 前先跑。
- 判断一个 conversion pass 是否降干净，不能看退出码，要看输出里还剩什么 dialect。

## 21.12 自我检核

- [ ] 能解释为什么 `--convert-to-llvm` 会留下 `func.func`，以及为什么它不报错。
- [ ] 能说出 `isLegal` 与 `isIllegal` 在 Unknown 上的不对称返回。
- [ ] 能手写模式 A 与模式 B 两种 `ConversionTarget` 配置，并说明各自适用场景。
- [ ] 能解释多次 `markUnknownOpDynamicallyLegal` 的组合语义。
- [ ] 能从三种 `failed to legalize` 诊断反推出该改 materialization、converter 还是 pattern。
- [ ] 能构造一个可被 `reconcile-unrealized-casts` 消掉的 cast 链，并说明它为什么不是类型安全证明。
- [ ] 能用 analysis 模式列出某组 pattern 覆盖的 op。
- [ ] 知道 rollback 模式会让 context-aware type converter 失效（Ch 20 的测试注释已提示）。

## 21.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DialectConversion.md`**
  - 读哪里：16–152 行（Modes of Conversion / Conversion Target / Recursive Legality）
  - 能学到什么：官方对 partial/full/analysis 与递归合法的定义
  - 和本章关联：21.2–21.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Transforms/Utils/DialectConversion.cpp`**
  - 读哪里：3999–4140（legality 查询）、3123–3260（reconcile）
  - 能学到什么：Unknown 的实际处理路径与 cast 折叠算法
  - 和本章关联：21.2、21.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/ConvertToLLVM/ConvertToLLVMPass.cpp`**
  - 读哪里：30–200 行
  - 能学到什么：一个真实 pass 如何用 partial conversion + dialect interface
  - 和本章关联：21.1、21.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Transforms/test-legalizer-rollback.mlir`**
  - 读哪里：整份
  - 能学到什么：rollback 的每一种边界（block 创建、参数替换、region 内失败）
  - 和本章关联：21.9 实验 7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Transforms/test-legalizer-full.mlir`**
  - 读哪里：36–60 行的递归合法例子
  - 能学到什么：递归合法与动态合法的组合
  - 和本章关联：21.4

legality 与 cast 处理的是“op 层”的边界。下一章进入另一条边界：tensor 与 memref 之间的 bufferization，它的契约不在 op 上，而在**整个函数的别名关系**上。

→ 下一章：[Ch 22 Bufferization：One-Shot 分析与 in-place 决策](./22-bufferization.md)
