# Ch 20 — TypeConverter 与 materialization

> **目标**：能读懂并改写一个 `TypeConverter`：1:1、1:N、1:0 三种 conversion，source/target 两种 materialization，以及 block argument 的签名转换。读完你应该能判断“这个补丁该改 conversion 还是该加 materialization”。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance`（HEAD `e44fff5decc7`）；`mlir-opt` 内含 `test-legalize-type-conversion` 测试 pass；assertions ON。

> **前置**：Ch 19 的 legality 三态与 `applyPartialConversion` / `applyFullConversion`。本章不再重复 legality，直接从“类型怎么变”开始。

## 20.1 为什么类型转换是最容易留下隐蔽 bug 的环节

Ch 19 解决的是“哪些 op 必须被重写”。真正开始写 lowering 时，第二个问题立刻出现：**一个值的类型变了，所有用它的人怎么办？**

维护者遇到的 conversion bug，按出现频率排：

1. pattern 写对了，但类型转换漏了一条规则，某个 `i32` 被静默丢掉（1:0），最后报 `failed to legalize unresolved source materialization`；
2. 类型转换对了，但 materialization 返回 `nullptr`，partial conversion 直接失败，错误信息却指向一个看起来无关的 `func.return`；
3. 函数边界（`func.func` 签名、block argument）没走 `SignatureConversion`，region 内部类型对、region 入口类型错；
4. context-aware conversion 注册顺序写反，缓存把“按 IR 决定类型”的规则短路了。

这些都不是 pattern 的问题，而是 **TypeConverter 的契约**问题。这一章把这个契约拆成三层：

| 层 | 你写什么 | 框架拿它做什么 |
|---|---|---|
| conversion | `addConversion` | 决定“旧类型 → 新类型” |
| materialization | `addSourceMaterialization` / `addTargetMaterialization` | 在类型不匹配的边界上补一个 op |
| signature | `SignatureConversion` / `convertRegionTypes` | 重写 block argument 与函数签名 |

三层里任何一层不完整，driver 都会以一个“看起来在别处”的错误失败。

### conversion 不产生 IR，materialization 产生 IR

这是最重要的一条分界，`mlir/include/mlir/Transforms/DialectConversion.h:170` 附近的注释说得很清楚：

- `addConversion` 的回调**只能返回类型**，不能建 op；
- `addSourceMaterialization` / `addTargetMaterialization` 的回调**必须建 op**，因为它们的任务就是在 IR 里放一个类型转换操作。

所以看到一个“类型对不上”的失败，先问：这是 conversion 没覆盖，还是 materialization 拒绝建 op？两者的修法完全不同。

## 20.2 先建立直觉：换轨与接驳

把一条 SSA use-def 链想成一条铁路。conversion 决定“新轨道铺到哪里”，materialization 是“新旧轨距不同处的接驳段”。

```
          原始 IR                                conversion 之后
  ┌────────────────────────────┐        ┌──────────────────────────────┐
  │ %r = "x.producer"()        │        │ %r = "x.producer"()          │
  │        : () -> f32         │  ───▶  │        : () -> f64           │
  │                            │        │                              │
  │ "y.consume"(%r) : (f32)->()│        │ "y.consume"(%r) : (f32)->()  │
  └────────────────────────────┘        └──────────────┬───────────────┘
                                                       │ 类型不匹配
                                                       ▼
                                        ┌──────────────────────────────┐
                                        │ %c = materialize f64 -> f32  │
                                        │ "y.consume"(%c) : (f32)->()  │
                                        └──────────────────────────────┘
```

三个问题对应三块：

- **往哪铺**：`addConversion` 的返回值；
- **接不接得上**：`addSourceMaterialization` / `addTargetMaterialization` 能不能建出桥；
- **谁负责铺到函数入口**：`SignatureConversion` 与 `convertRegionTypes`。

`source` 与 `target` 的命名经常被记反，记住一句话：

> **source = 回到“源类型”**（用户还期待着旧类型），**target = 变成“目标类型”**（pattern 期待新类型）。

一个值被转换后仍被未转换的 op 使用 → 需要 source materialization（把新值转回旧类型）。
一个未转换的值被已转换的 pattern 使用 → 需要 target materialization（把旧值转成 pattern 期待的类型）。

## 20.3 `addConversion` 的四种回调形状

`mlir/include/mlir/Transforms/DialectConversion.h:174` 的 `addConversion` 接受四种可调用对象。决定形状的是**第一个参数**是 `Type` 还是 `Value`，以及**有没有输出参数**：

```cpp
// 1) 1:1，失败返回 nullopt（让后面的规则继续尝试）
converter.addConversion([](Type t) -> std::optional<Type> { ... });

// 2) 1:N / 1:0，往 results 里 push
converter.addConversion([](IntegerType t, SmallVectorImpl<Type> &results)
                            -> std::optional<LogicalResult> { ... });

// 3) context-aware 1:1：看 Value 所在的 IR
converter.addConversion([](Value v) -> std::optional<Type> { ... });

// 4) context-aware 1:N
converter.addConversion([](Value v, SmallVectorImpl<Type> &results)
                            -> std::optional<LogicalResult> { ... });
```

两个返回值语义必须分清：

| 返回 | 含义 |
|---|---|
| `std::nullopt` | “我不管这个类型”，**继续尝试下一条规则** |
| `failure()` / 返回 `Type()` | “我认领这个类型，但转换失败”，**停止尝试** |

把“失败”写成 `std::nullopt` 是很常见的错误：规则本来应该报错，结果被后面的兜底规则接住，bug 被推迟到 materialization 阶段才炸。

### 1:0 是合法的

第二种形状允许 `results` 为空，含义是“这个类型被完全丢弃”。这时所有使用该值的 op 都必须在 conversion 中被重写掉；否则 driver 需要 materialization **从空列表造出一个值**。

`test-legalize-type-conversion` 的转换器正是这样（`mlir/test/lib/Dialect/Test/TestPatterns.cpp:2262`）：

```cpp
converter.addConversion([](IntegerType type, SmallVectorImpl<Type> &types) {
  if (type.isInteger(1))  types.push_back(type);   // i1 保留
  if (type.isInteger(38)) types.push_back(type);   // i38 保留
  // 其余整数类型全部丢弃
  return success();
});
```

## 20.4 注册顺序就是优先级

`addConversion` 的注释里有一句很容易漏掉的话：**最近注册的 conversion 最先被调用**（`DialectConversion.h:171` 附近 “the mostly recently added conversions will be invoked first”）。

这带来两个实践后果。

### 后果一：兜底规则要最先注册

```cpp
converter.addConversion([](Type t) { return t; });            // 兜底：全部合法
converter.addConversion([](FloatType t) -> Type { ... });     // 特化：先被调用
```

写反了，`FloatType` 永远走不到，因为兜底已经把它原样返回。

### 后果二：context-aware 规则会截断缓存

`mlir/docs/DialectConversion.md:398` 明确写了：

> context-aware conversion 一旦出现，只有**在它之后注册**的 context-unaware conversion 才能被缓存。

因为 context-aware 规则的结果依赖 IR，不能按 `Type` 缓存。文档因此建议：**context-aware 规则尽量早注册**（这样它最后才被调用），把可缓存的规则留在它后面。

这条在性能敏感的 lowering 里很重要：一个 `Value` 重载的规则插在中间，会让后面所有 `Type` 规则的缓存全部失效。

## 20.5 1:N 还在，但 1:N driver 已经不在了

这是 2024–2025 年 MLIR conversion 最大的一次 API 变动，维护老代码时必须知道。

`TypeConverter` 层仍然支持一个类型转成多个类型（上面的第二种形状），因为 block argument 与函数签名确实需要 1:N。但**独立的 1:N dialect conversion driver 已被删除**：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
git show --stat 23e3cbb2e82b | tail -4
```

本机实测：

```text
 mlir/tools/mlir-opt/mlir-opt.cpp                     |   2 -
 utils/bazel/llvm-project-overlay/mlir/BUILD.bazel    |   2 -
 .../llvm-project-overlay/mlir/test/BUILD.bazel       |  17 -
 23 files changed, 4 insertions(+), 1801 deletions(-)
```

提交信息的关键句（`git log -1 --format=%B 23e3cbb2e82b`）：

```text
The 1:N dialect conversion driver has been deprecated. Use the regular
dialect conversion driver instead. This commit deletes the 1:N dialect
conversion driver.

Note for LLVM integration: If you are already using the regular dialect
conversion, but still have argument materializations in your code base,
simply delete all `addArgumentMaterialization` calls.
```

所以在当前 trunk 上：

- `mlir/include/mlir/Transforms/OneToNTypeConversion.h` 不存在；
- `addArgumentMaterialization` 不存在（`grep -rn ArgumentMaterialization mlir/` 无输出）；
- 仍然存在的是 `TypeConverter` 的 1:N conversion + `SignatureConversion`，以及 `materializeTargetConversion` 返回 `SmallVector<Value>` 的重载（`DialectConversion.h:356`）。

**维护含义**：升级 downstream 代码时，如果看到 `addArgumentMaterialization`，直接删；如果看到 `OneToNTypeConversion.h`，要按 RFC（discourse 82513）迁移到普通 driver + `convertRegionTypes`。这是“补丁本身很小、但语义要重新验证”的典型场景。

## 20.6 两个 materialization 钩子

`mlir/include/mlir/Transforms/DialectConversion.h:200` 与 `:224` 分别注册 source 与 target materialization。两者的回调签名一致：

```cpp
Value(OpBuilder &builder, T resultType, ValueRange inputs, Location loc)
```

差异在“谁调用、期待什么”：

| 钩子 | 何时被调用 | 回调职责 |
|---|---|---|
| source | 转换后的值仍被**未转换**的 op 使用，且该 op 期待旧类型 | 用新值造出一个旧类型的值 |
| target | **已转换**的 pattern 拿到一个尚未转换（或类型不同）的 operand | 把旧值转成 pattern 期待的 `resultType` |

`T` 可以是 `Type` 的任意子类，也可以是 `TypeRange`（此时回调返回 `SmallVector<Value>`）。target materialization 还可以多接一个 `Type originalType` 参数——因为“从新类型反推原始类型”是不可能的，框架只能把它传进来（`DialectConversion.h:216` 的注释给了完整例子）。

回调失败时返回 `nullptr` / 空 `Value`，框架会尝试下一个 materialization；全部失败才是硬错误。

### 实测：materialization 真的会被调用

```bash
cd /tmp/mlir-part4
cat > demo20.mlir <<'EOF'
func.func @demo() {
  %r = "test.type_producer"() : () -> f32
  "foo.return"(%r) : (f32) -> ()
}
EOF
$T/bin/mlir-opt demo20.mlir -allow-unregistered-dialect -test-legalize-type-conversion
```

本机实测输出：

```text
module {
  func.func @demo() {
    %0 = "test.type_producer"() : () -> f64
    %1 = "test.cast"(%0) : (f64) -> f32
    "foo.return"(%1) : (f32) -> ()
  }
}
```

三件事同时发生了：`f32` 被 conversion 改成 `f64`；`foo.return` 不在合法集合里、没被重写，仍期待 `f32`；于是 driver 调用 source materialization，测试转换器用 `test.cast` 建出 `f64 -> f32`。

### 实测：materialization 拒绝就整体失败

```bash
cat > bad20.mlir <<'EOF'
func.func @bad(%arg0: i16) {
  "foo.return"(%arg0) : (i16) -> ()
}
EOF
$T/bin/mlir-opt bad20.mlir -allow-unregistered-dialect -test-legalize-type-conversion 2>&1 | head -6
```

本机实测：

```text
bad20.mlir:1:16: error: failed to legalize unresolved source materialization from () to ('i16') that remained live after conversion (no matching callback)
func.func @bad(%arg0: i16) {
               ^
bad20.mlir:1:16: note: see current operation: %0 = "builtin.unrealized_conversion_cast"() : () -> i16
bad20.mlir:2:3: note: see existing live user here: "foo.return"(%0) : (i16) -> ()
```

注意 `from ()`：`i16` 被 1:0 规则丢掉了，框架需要“无中生有”造一个 `i16`，而 source materialization 只允许 `f64 -> f32`、`() -> i32/i64`、整数互转，没有 `() -> i16`，于是失败。

这条错误信息里有两个 note，第二个 `see existing live user here` 才是定位关键：出问题的不是 `func.func`，而是那个不该活下来的 user。

## 20.7 签名转换：block argument 是另一套 API

conversion 处理的是 SSA value 的类型；**block argument 的类型不在 SSA use-def 里**，它属于 region signature，必须单独处理。

`TypeConverter::SignatureConversion`（`DialectConversion.h:65`）描述“旧参数列表 → 新参数列表”的映射：

```cpp
TypeConverter::SignatureConversion conversion(entry->getNumArguments());
conversion.addInputs(0, {i64Type});        // 第 0 个参数换成 i64
conversion.addInputs(1, {f32Type, f32Type}); // 第 1 个参数裂成两个
conversion.remapInput(2, replacementValues); // 第 2 个参数直接换成已有值
```

三个 API 的分工：

| API | 位置 | 用途 |
|---|---|---|
| `convertBlockSignature(Block*)` | `DialectConversion.h:345` | 让 converter 自己算出一份 `SignatureConversion` |
| `applySignatureConversion` | `DialectConversion.h:861` | 把一个 block 按映射原地重写 |
| `convertRegionTypes` | `DialectConversion.h:878` | 对整个 region 的入口 block 做签名转换，返回新入口 block |

`convertRegionTypes` 返回 `FailureOr<Block *>`，失败时 region 保持原样——这正是 `test.signature_conversion_undo` 那个回归测试在保护的行为（“Should not segfault here but gracefully fail”）。

函数级签名有现成的 pattern：

```cpp
// 只处理指定名字的 function-like op
mlir::populateFunctionOpInterfaceTypeConversionPattern("func.func", patterns, converter);
// 处理所有 function-like op
mlir::populateAnyFunctionOpInterfaceTypeConversionPattern(patterns, converter);
```

实现在 `mlir/lib/Transforms/Utils/DialectConversion.cpp:3964` 与 `:3971`。SCF/CF 的结构化转换另有 `populateSCFStructuralTypeConversionsAndLegality`（`mlir/include/mlir/Dialect/SCF/Transforms/Patterns.h:52`），它同时注册 pattern 和 legality，避免你手写 `scf.for` 的 iter_arg 类型约定。

### 实测：context-aware conversion 看 IR 决定类型

```bash
cat > ctx.mlir <<'EOF'
func.func @ctx_inc2() attributes {increment = 2 : i64} {
  %0 = "test.context_op"() : () -> (i37)
  "test.replace_with_legal_op"(%0) : (i37) -> ()
  return
}
EOF
$T/bin/mlir-opt ctx.mlir -test-legalize-type-conversion
```

本机实测：

```text
module {
  func.func @ctx_inc2() attributes {increment = 2 : i64} {
    %0 = "test.context_op"() : () -> i37
    %1 = builtin.unrealized_conversion_cast %0 : i37 to i39
    "test.legal_op_d"(%1) : (i39) -> ()
    return
  }
}
```

把 `increment` 改成 `1`，结果变成 `i38`。同一个 `i37`，转换结果依赖所在函数——这就是 context-aware conversion 存在的理由，也是它不能被缓存的理由。

## 20.8 源码落点

| 文件 | 行 | 读什么 |
|---|---|---|
| `mlir/include/mlir/Transforms/DialectConversion.h` | 41 | `class TypeConverter` 定义 |
| 同上 | 65 | `SignatureConversion`：`addInputs` / `remapInput` |
| 同上 | 174 | `addConversion` 的四种形状与注释 |
| 同上 | 200 / 224 | source / target materialization |
| 同上 | 345 | `convertBlockSignature` |
| 同上 | 351–357 | `materializeSourceConversion` / `materializeTargetConversion` |
| `mlir/lib/Transforms/Utils/DialectConversion.cpp` | 1568 | `convertRegionTypes` 实现 |
| 同上 | 1599 | `applySignatureConversion` 实现 |
| 同上 | 3642–3670 | `TypeConverter::convertType` 的缓存与递归 |
| 同上 | 3766–3790 | 两个 materialize 入口 |
| 同上 | 3360 | `legalizeUnresolvedMaterialization`（错误信息出处） |
| `mlir/test/lib/Dialect/Test/TestPatterns.cpp` | 2227 | `TestTypeConversionDriver`，本章所有实验的转换器 |
| 同上 | 2251–2370 | 四条 conversion + 两个 materialization 的完整写法 |

## 20.9 可跑实验

以下命令统一先执行：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
mkdir -p /tmp/mlir-part4 && cd /tmp/mlir-part4
```

### 实验 1：确认 API 位置

```bash
grep -n 'class TypeConverter {\|void addConversion\|void addSourceMaterialization\|void addTargetMaterialization' \
  /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Transforms/DialectConversion.h
```

本机实测：

```text
41:class TypeConverter {
174:  void addConversion(FnT &&callback) {
200:  void addSourceMaterialization(FnT &&callback) {
224:  void addTargetMaterialization(FnT &&callback) {
```

### 实验 2：跑通官方 type conversion 测试

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
$T/bin/mlir-opt mlir/test/Transforms/test-legalize-type-conversion.mlir \
  -test-legalize-type-conversion -allow-unregistered-dialect -split-input-file \
  -verify-diagnostics && echo "ALL SPLITS OK"
```

本机实测（末尾）：

```text
// -----
ALL SPLITS OK
```

通过时会把每个成功合法化的 split 原样打印出来（本例 19 个 split），最后一行是 `ALL SPLITS OK`。`-verify-diagnostics` 把 `expected-error` 全部消费掉；去掉它会看到 12 条诊断——这正是测试文件在验证“失败路径也必须给出精确诊断”。

### 实验 3：观察 1:0 + “无中生有” materialization

```bash
cat > drop20.mlir <<'EOF'
func.func @drop(%arg0: i32) {
  "test.type_consumer"(%arg0) : (i32) -> ()
  return
}
EOF
$T/bin/mlir-opt drop20.mlir -test-legalize-type-conversion
```

本机实测：

```text
module {
  func.func @drop() {
    %0 = "test.type_producer"() : () -> i32
    "test.type_consumer"(%0) : (i32) -> ()
    return
  }
}
```

参数被整个丢掉（函数变成零参数），但 user 还活着，于是 source materialization 用 `() -> i32` 造了一个新值。**这是 1:0 最容易被误读的输出**：看起来“类型没变”，实际上原值已经不存在了。

### 实验 4：观察 context-aware conversion

```bash
for inc in 1 2 3; do
  sed "s/increment = 2/increment = $inc/" ctx.mlir > ctx_$inc.mlir
  echo "--- increment=$inc"
  $T/bin/mlir-opt ctx_$inc.mlir -test-legalize-type-conversion | grep unrealized
done
```

本机实测（三次）：

```text
--- increment=1
    %1 = builtin.unrealized_conversion_cast %0 : i37 to i38
--- increment=2
    %1 = builtin.unrealized_conversion_cast %0 : i37 to i39
--- increment=3
    %1 = builtin.unrealized_conversion_cast %0 : i37 to i40
```

同一个 converter、同一份 IR 形状，类型结果随属性变化。这就是“不能缓存”的直接证据。

### 实验 5：`-debug-only` 看 driver 的决策树

```bash
cat > add20.mlir <<'EOF'
func.func @add(%a: i32, %b: i32) -> i32 {
  %r = arith.addi %a, %b : i32
  return %r : i32
}
EOF
$T/bin/mlir-opt add20.mlir --convert-to-llvm -debug-only=dialect-conversion 2>&1 | head -18
```

本机实测：

```text
[dialect-conversion:1] 
[dialect-conversion:1] //===-------------------------------------------===//
[dialect-conversion:1] Legalizing operation : 'builtin.module' (0x…) {
[dialect-conversion:1]   * Fold {
[dialect-conversion:1]   } -> FAILURE : unable to fold
[dialect-conversion:1] } -> FAILURE : no matched legalization pattern
[dialect-conversion:1] //===-------------------------------------------===//
[dialect-conversion:1] 
[dialect-conversion:1] //===-------------------------------------------===//
[dialect-conversion:1] Legalizing operation : 'func.func' (0x…) {
[dialect-conversion:1]   * Fold {
[dialect-conversion:1]   } -> FAILURE : unable to fold
[dialect-conversion:1] 
[dialect-conversion:1]   * Pattern : 'func.func -> ()' {
[dialect-conversion:1]     ** Insert  : 'llvm.func' (0x…) (was detached)
```

（op 地址每次运行都不同，这里写成 `0x…`。）三点值得注意：`builtin.module` 会先被尝试合法化（失败但不致命，因为 partial conversion 允许）；每个 op 先试 fold 再试 pattern；`[dialect-conversion:1]` 前缀是新的 `LDBG` 日志格式，老博客里的无前缀格式已经过时。**materialization 的插入没有单独日志行**——要观察它，回到 IR 或 Ch 21 的 `attach-debug-materialization-kind`。

### 实验 6：确认 1:N driver 已删除

```bash
ls /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Transforms/OneToNTypeConversion.h 2>&1
grep -rn 'ArgumentMaterialization' /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/ | wc -l
```

本机实测：

```text
ls: cannot access '.../OneToNTypeConversion.h': No such file or directory
0
```

### 实验 7：`convertRegionTypes` 的失败回滚

```bash
$T/bin/mlir-opt /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Transforms/test-legalize-type-conversion.mlir \
  -test-legalize-type-conversion -allow-unregistered-dialect -split-input-file 2>&1 \
  | grep -A4 'test_signature_conversion_undo' | head -8
```

本机实测（节选）：

```text
  func.func @test_signature_conversion_undo() {
    "test.signature_conversion_undo"() ({
    ^bb0(%arg0: f32):
```

签名转换被 undo 之后，block argument 仍是原来的 `f32`——没有半转换状态泄漏。

## 20.10 踩雷集锦

1. **“conversion 里可以顺手建 op” → 不行。** `addConversion` 只返回类型；需要 IR 就必须用 materialization，否则 driver 无法在回滚时撤销你的 op。

2. **“返回 nullopt 和返回 failure 差不多” → 差很远。** `nullopt` 是“换下一条规则”，`failure` 是“我认领但失败”。写错会让错误信息指向错误的位置。

3. **“注册顺序无所谓” → 有所谓。** 最近注册的先执行；兜底规则必须最先注册，context-aware 规则要尽量早注册以保住缓存。

4. **“1:0 就是删掉参数” → 还要处理所有 user。** user 没被重写时，框架会尝试从空列表 materialize，失败信息是 `from () to (...)`。

5. **“有 `addArgumentMaterialization` 可以调” → 已删除。** 2025-04 的 #121389 删掉了 1:N driver 和这个钩子；downstream 升级时直接删调用。

6. **“source 是转向新类型” → 反了。** source = 回到源类型（给未转换的 user），target = 转成目标类型（给已转换的 pattern）。

7. **“签名转换失败会保持部分结果” → 会回滚。** `convertRegionTypes` 失败时 region 原样保留；依赖“半转换”的代码会写出未定义行为。

8. **“materialization 一定要返回非空” → 可以返回空让框架试下一个。** 只有所有 materialization 都失败才是硬错误；但返回空时不要留下半建的 op。

## 20.11 本章重点整理

- `TypeConverter` 分三层：conversion（改类型）、materialization（补 IR）、signature（改 block argument）。
- `addConversion` 有四种形状；`nullopt` 与 `failure` 语义不同，决定“继续尝试”还是“立即失败”。
- 注册顺序 = 优先级（后注册先执行）；context-aware 规则会截断缓存，应尽早注册。
- 1:0 合法，但要求所有 user 被重写，否则需要“从空 materialize”。
- 1:N 的 `TypeConverter` 能力仍在，独立的 1:N driver 与 `addArgumentMaterialization` 已删除。
- source 回到旧类型、target 变成新类型；target 可拿到 `originalType`，source 拿不到。
- 函数/region 签名必须走 `SignatureConversion`、`convertRegionTypes` 或 function-like pattern。
- 失败诊断的两个 note（current operation / existing live user）比主错误更有定位价值。

## 20.12 自我检核

- [ ] 能不看文档写出四种 `addConversion` 形状，并说出 `nullopt` 与 `failure` 的差别。
- [ ] 能解释为什么兜底规则要**最先**注册。
- [ ] 能说出 context-aware conversion 对缓存的影响，以及正确的注册位置。
- [ ] 能区分 source 与 target materialization 的触发场景，并各举一个例子。
- [ ] 知道 `addArgumentMaterialization` 已被删除，能说出删除它的提交与替代方案。
- [ ] 能用 `convertRegionTypes` + `SignatureConversion` 处理一个 block argument 类型变化。
- [ ] 看到 `from () to ('i16')` 时能立刻想到“1:0 + 无匹配 materialization”。
- [ ] 能用 `-debug-only=dialect-conversion` 找到 materialization 的插入点。

## 20.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Transforms/DialectConversion.h`**
  - 读哪里：41–360 行，以及 839–960 行
  - 能学到什么：`TypeConverter` 与 `ConversionPatternRewriter` 的完整契约
  - 和本章关联：20.3–20.7 的全部 API
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DialectConversion.md`**
  - 读哪里：374–596 行（Type Conversion / Region Signature Conversion）
  - 能学到什么：官方对 conversion 与 materialization 的措辞，以及缓存说明
  - 和本章关联：20.3、20.4、20.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Transforms/Utils/DialectConversion.cpp`**
  - 读哪里：3642–3800（convertType / materialize）、3360–3420（失败诊断）
  - 能学到什么：缓存、递归转换、unresolved materialization 的判定
  - 和本章关联：20.5、20.6、20.9 实验 3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Test/TestPatterns.cpp`**
  - 读哪里：2227–2380 行
  - 能学到什么：一个覆盖 1:1 / 1:N / 1:0 / context-aware / 两种 materialization 的完整转换器
  - 和本章关联：本章所有实验
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/LLVM.md`**
  - 读哪里：Type conversion 相关小节
  - 能学到什么：真实 lowering 里 `LLVMTypeConverter` 怎么组合这些钩子
  - 和本章关联：Ch 23 的前置

类型转换决定“新类型长什么样”，但不决定“旧 op 什么时候必须消失”。下一章处理这个边界：动态 legality、unknown op，以及 partial conversion 留下的 `unrealized_conversion_cast`。

→ 下一章：[Ch 21 动态 legality 与 unrealized_conversion_cast](./21-dynamic-legality-and-casts.md)
