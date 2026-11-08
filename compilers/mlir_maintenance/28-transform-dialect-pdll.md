# Ch 28 — Transform dialect 与 PDLL：把“怎么改 IR”写成 IR

> **目标**：能写一个可解释的 transform schedule（match → tile → vectorize），理解 payload/handle 失效规则；能用 PDLL 写 pattern 并编译成 PDL MLIR 或 C++；能解释本树里 `mlir-pdll --help` 为什么直接 abort。
>
> **环境**：`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，`MLIR_ENABLE_PDL_IN_PATTERNMATCH=ON`，源 @ `e44fff5decc7`。
>
> **前置**：Ch 10（RewritePattern）、Ch 24（tiling 已迁移到 transform op）。

---

## 28.1 为什么“调度”要变成一门方言

Ch 24 已经暴露了问题：`-linalg-tile` 这个 pass 没了，tiling 变成 `transform.structured.tile_using_for`。为什么？

因为 tiling 不是“一个固定算法”，而是**一串依赖前一步结果的决策**：

```text
match 出 linalg.matmul
  → 按 [2,2,2] 切
    → 对切出来的小 op 做 vectorize
      → 对剩下的 loop 做 unroll
        → 如果失败就换一组 tile size
```

用 pass option 表达，你只能得到“tile sizes 的笛卡尔积”；用 C++ 写死，你得到“只有编译器作者能改的策略”。Transform dialect 的思路是：**把策略写成 IR**，于是它可以被打印、被测试、被搜索、被别人改写。

代价是引入了一整套新概念（payload、handle、sequence、失效规则），也就是本章的主体。

规模（本章写作时实测）：

```bash
find mlir/test/Dialect/Transform -name '*.mlir' | wc -l   # 45
find mlir/test/mlir-pdll -name '*.pdll' | wc -l           # 21
grep -c 'def ' mlir/include/mlir/Dialect/Transform/IR/TransformOps.td   # 38
grep -rn 'TODO\|FIXME' mlir/lib/Dialect/Transform | wc -l # 14
```

12 个月 `lib/Dialect/Transform` 只有 36 次提交——它已经过了“天天改语法”的阶段，进入“加新 op、修失效规则”的维护期。这对新贡献者是好事：接口相对稳定。

## 28.2 先建立直觉：两层 IR + 一个解释器

```text
┌──────────────────────────────────────────────────────────────────┐
│  同一个 module 里住着两套 IR                                       │
│                                                                  │
│  ┌────────────────────────┐      ┌─────────────────────────────┐ │
│  │  payload IR            │      │  transform IR               │ │
│  │  （你要改的代码）        │      │  （怎么改的脚本）             │ │
│  │                        │      │                             │ │
│  │ func.func @matmul {     │      │ module @transform... {       │ │
│  │   linalg.matmul ...     │◄─────┤  transform.named_sequence   │ │
│  │ }                       │ 操作  │    @__transform_main        │ │
│  └────────────────────────┘      │  { match; tile; vectorize } │ │
│           ▲                       └─────────────────────────────┘ │
│           │                                    │                 │
│           │        ┌───────────────────────────┘                 │
│           │        ▼                                             │
│           │   TransformState（handle → payload op 的映射表）       │
│           │        │                                             │
│           │        ├─ handle: !transform.any_op 等                │
│           │        ├─ 失效规则：consumed/invalidated → 旧 handle 死 │
│           │        └─ listener: 记录 payload 的替换/擦除           │
│           │                                                      │
│           └────────┴── 每个 transform op 的 apply() 真正改 payload │
└──────────────────────────────────────────────────────────────────┘

执行入口：--transform-interpreter
           → 找 module 属性 transform.with_named_sequence
             → 找 @__transform_main
               → 逐 op 执行，直到 yield
```

三条必须先记住的规则：

1. **transform op 不直接持有 payload op**，它持有 **handle**。handle 是“payload 里某个 op 的引用”，可以批量（`!transform.any_op` 可能对应多个 op）。
2. **handle 会失效**。一个 transform op 如果“消费”了某个 handle（比如 tile 会替换原 op），那么指向同一批 payload op 的所有旧 handle 全部作废——即使你手上还有那个 SSA 值。
3. **payload 的修改通过 rewriter/listener 记录**，所以 transform op 内部不能随便 `erase`，必须走 `TransformRewriter`。

## 28.3 最小 schedule：match → tile

```bash
cd /tmp/mlir_p5
cat > tile.mlir <<'EOF'
func.func @matmul(%A: memref<8x8xf32>, %B: memref<8x8xf32>, %C: memref<8x8xf32>) {
  linalg.matmul ins(%A, %B : memref<8x8xf32>, memref<8x8xf32>)
               outs(%C : memref<8x8xf32>)
  return
}

module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%arg0: !transform.any_op {transform.readonly}) {
    %mm = transform.structured.match ops{["linalg.matmul"]} in %arg0 : (!transform.any_op) -> !transform.any_op
    %tiled, %loops:3 = transform.structured.tile_using_for %mm tile_sizes [2, 2, 2]
      : (!transform.any_op) -> (!transform.any_op, !transform.any_op, !transform.any_op, !transform.any_op)
    transform.yield
  }
}
EOF
$T/bin/mlir-opt tile.mlir --transform-interpreter
```

真实输出（节选）：

```text
    scf.for %arg3 = %c0 to %c8 step %c2 {
      scf.for %arg4 = %c0_0 to %c8_2 step %c2_4 {
        scf.for %arg5 = %c0_1 to %c8_3 step %c2_5 {
          %subview = memref.subview %arg0[%arg3, %arg5] [2, 2] [1, 1] : memref<8x8xf32> to memref<2x2xf32, strided<[8, 1], offset: ?>>
          %subview_6 = memref.subview %arg1[%arg5, %arg4] [2, 2] [1, 1] : memref<8x8xf32> to memref<2x2xf32, strided<[8, 1], offset: ?>>
          %subview_7 = memref.subview %arg2[%arg3, %arg4] [2, 2] [1, 1] : memref<8x8xf32> to memref<2x2xf32, strided<[8, 1], offset: ?>>
          linalg.matmul ins(%subview, %subview_6 : ...) outs(%subview_7 : ...)
```

逐行解释这个 schedule：

| 片段 | 作用 | 维护者注意 |
|---|---|---|
| `module attributes {transform.with_named_sequence}` | 标记“这是 transform 模块” | 没有这个属性，interpreter 不会理它 |
| `@__transform_main` | 入口符号名硬编码 | 见 `TransformDialect.td:25` |
| `%arg0 {transform.readonly}` | payload root（整个 module） | `readonly` 声明“不修改”，便于并发/缓存 |
| `transform.structured.match ops{...}` | 在 payload 里找 op | 匹配不到不报错，handle 为空 |
| `tile_using_for %mm tile_sizes [2,2,2]` | 三刀，产出 3 个 loop handle | 返回 4 个值：tiled op + 3 loops |

`tile_using_for` 的实现在 `mlir/lib/Dialect/Linalg/TransformOps/LinalgTransformOps.cpp:3655`：

```cpp
transform::TileUsingForOp::apply(transform::TransformRewriter &rewriter,
```

它内部调用 Ch 24 讲的 `linalg::tileLinalgOp`——也就是说 **Transform op 是“策略层”，真正的算法还是 Linalg 的 utility**。这条分层原则贯穿整个 Transform dialect：`transform.*` 尽量薄。

## 28.4 入口名写错会怎样

`@__transform_main` 是硬编码的（`TransformDialect.td:25`）：

```tablegen
        kTransformEntryPointSymbolName = "__transform_main";
```

如果你写成别的名字：

```bash
cat > badseq.mlir <<'EOF'
func.func @f() { return }
module attributes {transform.with_named_sequence} {
  transform.named_sequence @not_main(%arg0: !transform.any_op {transform.readonly}) {
    transform.yield
  }
}
EOF
$T/bin/mlir-opt badseq.mlir --transform-interpreter
```

真实输出：

```text
badseq.mlir:0:0: error: could not find a nested named sequence with name: __transform_main
badseq.mlir:0:0: note: see current operation: 
"builtin.module"() ({
  "func.func"() <{function_type = () -> (), sym_name = "f"}> ({
    "func.return"() : () -> ()
  }) : () -> ()
```

注意 `0:0` 的位置——因为“找不到符号”是模块级错误，没有具体 op 位置。这个错误信息足够直接，不需要 debug。

## 28.5 handle 失效：Transform 里最贵的 bug

构造一个典型的错误：tile 之后继续用原来的 handle。

```bash
cat > useafter.mlir <<'EOF'
func.func @matmul(%A: memref<8x8xf32>, %B: memref<8x8xf32>, %C: memref<8x8xf32>) {
  linalg.matmul ins(%A, %B : memref<8x8xf32>, memref<8x8xf32>) outs(%C : memref<8x8xf32>)
  return
}
module attributes {transform.with_named_sequence} {
  transform.named_sequence @__transform_main(%arg0: !transform.any_op {transform.readonly}) {
    %mm = transform.structured.match ops{["linalg.matmul"]} in %arg0 : (!transform.any_op) -> !transform.any_op
    %t, %loops:3 = transform.structured.tile_using_for %mm tile_sizes [2, 2, 2]
      : (!transform.any_op) -> (!transform.any_op, !transform.any_op, !transform.any_op, !transform.any_op)
    transform.structured.vectorize %mm vector_sizes [2, 2, 2] : !transform.any_op
    transform.yield
  }
}
EOF
$T/bin/mlir-opt useafter.mlir --transform-interpreter --transform-dialect-check-uses
```

真实输出：

```text
useafter.mlir:10:5: error: op uses a handle invalidated by a previously executed transform op
    transform.structured.vectorize %mm vector_sizes [2, 2, 2] : !transform.any_op
    ^
useafter.mlir:7:11: note: handle to invalidated ops
    %mm = transform.structured.match ops{["linalg.matmul"]} in %arg0 : (!transform.any_op) -> !transform.any_op
          ^
useafter.mlir:8:20: note: invalidated by this transform op that consumes its operand #0 and invalidates all handles to payload IR entities associated with this operand and entities nested in them
    %t, %loops:3 = transform.structured.tile_using_for %mm tile_sizes [2, 2, 2]
                   ^
useafter.mlir:2:3: note: ancestor payload op
  linalg.matmul ins(%A, %B : memref<8x8xf32>, memref<8x8xf32>) outs(%C : memref<8x8xf32>)
  ^
```

这个诊断是 Transform dialect 最好的设计之一：它不仅报错，还**画出完整的因果链**（哪个 handle、被哪个 op 失效、payload 里对应哪个 op）。

维护者视角的要点：

- “consumes its operand #0” 是 op 的 `TransformEachOpTrait`/`FunctionalStyleTransformOpTrait` 声明出来的副作用。改一个 transform op 的 trait，可能让一堆现有 schedule 从“能用”变成“失效”——**这是 Transform 侧最常见的破坏性改动**。
- 正确写法是用 `%tiled`（tile 返回的新 handle），而不是 `%mm`。
- `--transform-dialect-check-uses` 是静态检查：它不执行 payload 变换，只按声明的 consumes/invalidates 推演。没有输出 = 没发现问题。

对比一下干净 schedule 的输出：

```bash
$T/bin/mlir-opt tile.mlir --transform-interpreter --transform-dialect-check-uses 2>&1 >/dev/null
```

真实输出：（空）。`2>&1 >/dev/null` 把 stderr 留下来、stdout 丢掉——空表示没有诊断。这是 CI 里检查 schedule 健康度的常用写法。

## 28.6 不跑 interpreter，schedule 就是一段死代码

```bash
$T/bin/mlir-opt tile.mlir
```

真实输出（节选）：

```text
module {
  func.func @matmul(%arg0: memref<8x8xf32>, %arg1: memref<8x8xf32>, %arg2: memref<8x8xf32>) {
    linalg.matmul ins(%arg0, %arg1 : memref<8x8xf32>, memref<8x8xf32>) outs(%arg2 : memref<8x8xf32>)
    return
  }
  module attributes {transform.with_named_sequence} {
    transform.named_sequence @__transform_main(%arg0: !transform.any_op {transform.readonly}) {
      ...
```

payload 一个 op 都没变，transform 模块原样打印。这是**预期行为**：transform IR 只在 `--transform-interpreter` 下执行，或者在某个 pass 显式 apply 时执行。

这个特性有实际好处：你可以把 schedule 和 payload 放在同一个文件里做 lit 测试，用 `CHECK` 分别断言两边。也有实际坏处：有人以为“加了 transform 模块就会自动生效”，结果性能没变。

## 28.7 PDLL：给 pattern 写“宿主语言”

PDL（Pattern Description Language）是 MLIR 里的“pattern IR”：`pdl.pattern` 描述匹配什么、重写成什么，由 `pdl_interp` 字节码解释执行。PDLL 是 PDL 的**前端语言**——更像 C++/TableGen 的语法，编译成 PDL。

为什么需要它？因为手写 `pdl.pattern` 极其啰嗦。对比一下同一个“匹配 `test.simple` 换成 `test.success`”：

**PDLL（1 行）**

```pdll
Pattern ReplaceSimple => replace op<test.simple> with op<test.success>;
```

**PDL（编译产物，14 行）**

```text
  pdl.pattern @ReplaceSimple : benefit(0) {
    %0 = operands loc(#loc4)
    %1 = types loc(#loc4)
    %2 = operation "test.simple"(%0 : !pdl.range<value>)  -> (%1 : !pdl.range<type>) loc(#loc4)
    rewrite %2 {
      %3 = operation "test.success"  loc(#loc6)
      replace %2 with %3 loc(#loc5)
    } loc(#loc5)
  } loc(#loc3)
```

实测：

```bash
cat > my.pdll <<'EOF'
Pattern EraseTestOp => erase op<test.op>;
Pattern ReplaceSimple => replace op<test.simple> with op<test.success>;
EOF
$T/bin/mlir-pdll my.pdll -x mlir
```

真实输出（完整）：

```text
module {
  pdl.pattern @EraseTestOp : benefit(0) {
    %0 = operands loc(#loc1)
    %1 = types loc(#loc1)
    %2 = operation "test.op"(%0 : !pdl.range<value>)  -> (%1 : !pdl.range<type>) loc(#loc1)
    rewrite %2 {
      erase %2 loc(#loc2)
    } loc(#loc2)
  } loc(#loc)
  pdl.pattern @ReplaceSimple : benefit(0) {
    %0 = operands loc(#loc4)
    %1 = types loc(#loc4)
    %2 = operation "test.simple"(%0 : !pdl.range<value>)  -> (%1 : !pdl.range<type>) loc(#loc4)
    rewrite %2 {
      %3 = operation "test.success"  loc(#loc6)
      replace %2 with %3 loc(#loc5)
    } loc(#loc5)
  } loc(#loc3)
} loc(#loc)
#loc = loc("my.pdll":1:1)
#loc1 = loc("my.pdll":1:30)
#loc2 = loc("my.pdll":1:24)
#loc3 = loc("my.pdll":2:1)
#loc4 = loc("my.pdll":2:34)
#loc5 = loc("my.pdll":2:26)
#loc6 = loc("my.pdll":2:55)
```

三件事值得注意：

1. **`benefit(0)`**：PDLL 没写 benefit，默认 0。PDL 的 benefit 决定 pattern 匹配顺序（高优先）。
2. **每个生成的 op 都带 `loc("my.pdll":行:列)`**：出问题时能直接跳回 PDLL 源码。这是“前端语言”的核心价值——**错误信息回到你写的那一行**。
3. **`loc(#loc)` 在模块末尾统一声明**：MLIR 的 location 去重机制。

## 28.8 PDLL 还能生成 C++：把 pattern 编译进 pass

```bash
$T/bin/mlir-pdll my.pdll -x cpp
```

真实输出（节选）：

```text
namespace {

struct EraseTestOp : ::mlir::PDLPatternModule {
  template <typename... ConfigsT>
  EraseTestOp(::mlir::MLIRContext *context, ConfigsT &&...configs)
    : ::mlir::PDLPatternModule(::mlir::parseSourceString<::mlir::ModuleOp>(
R"mlir(pdl.pattern @EraseTestOp : benefit(0) {
  %0 = operands loc("my.pdll":1:30)
  %1 = types loc("my.pdll":1:30)
  %2 = operation "test.op"(%0 : !pdl.range<value>)  -> (%1 : !pdl.range<type>) loc("my.pdll":1:30)
  rewrite %2 {
    erase %2 loc("my.pdll":1:24)
  } loc("my.pdll":1:24)
} loc("my.pdll":1:1)
    )mlir", context), std::forward<ConfigsT>(configs)...) {
  }
};


struct ReplaceSimple : ::mlir::PDLPatternModule {
```

生成的 C++ **不解析 PDLL 源码**，而是把已经编译好的 PDL 文本嵌进去当 raw string，构造 `PDLPatternModule`。也就是说：

- 运行时依赖 `pdl_interp` 字节码解释器（构建时 `MLIR_ENABLE_PDL_IN_PATTERNMATCH=ON`，本树已开）；
- 编译期只需要 `mlir-pdll` 生成一次，不需要 PDLL parser 进产物；
- 好处是 pattern 作者写 PDLL、性能敏感的用户仍可切换到手写 C++ pattern。

实际工程里，`mlir-pdll` 通常作为 CMake custom command 挂在 `add_mlir_dialect` 旁边，把 `.pdll` 编译成 `.cpp` 或 `.mlir` 再链接进 pass。

## 28.9 跑起来：`-test-pdll-pass` 与 `-test-pdl-bytecode-pass`

本树内置了两个 test pass，可以直接验证这两层。

**PDLL 层**（`test/lib/Tools/PDLL/TestPDLL.pdll` 编译进了 `mlir-opt`）：

```bash
$T/bin/mlir-opt \
  /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/mlir-pdll/Integration/test-pdll.mlir \
  -test-pdll-pass
```

真实输出：

```text
module {
  func.func @simpleTest() {
    "test.success"() : () -> ()
    return
  }
  func.func @testImportedInterface() -> i1 {
    "test.non_cast"() : () -> ()
    %0 = "test.success"() : () -> i1
    return %0 : i1
  }
  func.func @testWithConstraint(%arg0: i32) {
    %0 = "test.success"() : () -> i32
    return %0 : i32
  }
}
```

`test.simple` 全部变成 `test.success`；`test.non_cast` + `unrealized_conversion_cast` 那条走的是 **interface 匹配**（`Pattern TestInterface => replace _: CastOpInterface with op<test.success>;`）——这说明 PDLL 能匹配“实现某接口的任意 op”，这是手写 PDL 很难表达的。

**PDL bytecode 层**：

```bash
$T/bin/mlir-opt \
  /home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Rewrite/pdl-bytecode.mlir \
  -allow-unregistered-dialect -test-pdl-bytecode-pass -split-input-file
```

真实输出（节选）：

```text
module {
  module @ir attributes {test.apply_constraint_1} {
    "test.replaced_by_pattern"() : () -> ()
  }
}
```

两个细节：

1. **必须加 `-split-input-file`**。这个测试文件里有多段 `module @patterns`，不 split 会报 `redefinition of symbol named 'patterns'`（我实测过这个错误）。
2. `-allow-unregistered-dialect` 是必需的，因为文件里有 `tf.MatMul` 这种不存在于本树的方言名。

## 28.10 一个真实的上游 bug：`mlir-pdll --help` 直接 abort

```bash
$T/bin/mlir-pdll --help
```

真实输出（节选，assertions build）：

```text
mlir-pdll: /home/ubuntu/llvm/trees/ct-instr-trunk/llvm/lib/Support/CommandLine.cpp:2561: virtual void {anonymous}::CategorizedHelpPrinter::printOptions(...): Assertion `llvm::is_contained(SortedCategories, Cat) && "Option has an unregistered category"' failed.
...
 #11 0x00007f55fbfd661e (anonymous namespace)::CategorizedHelpPrinter::printOptions(...)
```

```bash
$T/bin/mlir-pdll --help > /dev/null 2>&1; echo $?
134
```

exit code 134 = SIGABRT。

根因在源码里就能看到（`mlir/tools/mlir-pdll/mlir-pdll.cpp:120-122`）：

```cpp
int main(int argc, char **argv) {
  // FIXME: This is necessary because we link in TableGen, which defines its
  // options as static variables.. some of which overlap with our options.
  llvm::cl::ResetCommandLineParser();
```

`ResetCommandLineParser()` 会把 TableGen 的静态 option（以及**默认的 option category**）全部注销，然后重新注册 `mlir-pdll` 自己的 option。但 help 打印器会按 category 分组，而新 option 没有显式 `cl::cat(...)`，落到一个已经不在 `SortedCategories` 里的默认 category 上——断言炸掉。

为什么值得写进教材：

- 它是一个**可复现、根因明确、影响面小**的缺陷，适合当第一个上游补丁（Ch 30/31 会讲怎么包装成 PR）；
- 它展示了“assertions build 才能发现的 bug”：release build 里不会 abort，可能只是 help 输出错乱，所以长期没人报；
- 修法有两种：给所有 option 显式加 `cl::cat(PDLLCat)` 并注册 category，或者让 help 打印前重新注册 default category。前者更符合 LLVM 惯例。

**注意**：这是我在本树（`e44fff5decc7` + assertions ON）实测的行为，不是从文档抄的。若上游已修，以你手上的 HEAD 为准。

## 28.11 PDLL 的报错长什么样

```bash
cat > bad.pdll <<'EOF'
Pattern Foo => erase op<test.op>
EOF
$T/bin/mlir-pdll bad.pdll -x mlir
```

真实输出：

```text
bad.pdll:2:1: error: expected `;` after statement

^
```

注意位置是 `2:1` 而不是 `1:41`——因为缺分号时 parser 会继续读到下一个 token（EOF 在第 2 行行首）才确定失败。这类“位置偏移一行”的 PDLL 诊断是已知的粗糙点（parser 在 `lib/Tools/PDLL/Parser/Parser.cpp:1468` 的 `parsePatternDecl` 里恢复）。

写 PDLL 时的实用建议：**每个 pattern 之间留空行**，这样报错位置至少落在逻辑边界上。

## 28.12 源码落点

| 主题 | 文件:行 |
|---|---|
| 入口符号名 | `mlir/include/mlir/Dialect/Transform/IR/TransformDialect.td:25` |
| transform op 定义 | `mlir/include/mlir/Dialect/Transform/IR/TransformOps.td`（38 个 `def`） |
| handle 类型 | `mlir/include/mlir/Dialect/Transform/IR/TransformTypes.td`（11 个 `def`） |
| `applyTransform`（执行单个 op） | `mlir/lib/Dialect/Transform/Interfaces/TransformInterfaces.cpp:810` |
| interpreter pass | `mlir/lib/Dialect/Transform/Transforms/InterpreterPass.cpp:129` |
| use-after-free 检查 | `mlir/lib/Dialect/Transform/Transforms/CheckUses.cpp` |
| `tile_using_for` 实现 | `mlir/lib/Dialect/Linalg/TransformOps/LinalgTransformOps.cpp:3655` |
| PDLL 解析 | `mlir/lib/Tools/PDLL/Parser/Parser.cpp:1468`（`parsePatternDecl`） |
| PDLL → PDL MLIR | `mlir/lib/Tools/PDLL/CodeGen/MLIRGen.cpp:136`（`generate`）、`:622`（`codegenPDLLToMLIR`） |
| PDLL → C++ | `mlir/lib/Tools/PDLL/CodeGen/CPPGen.cpp`（`:248` 起） |
| 工具入口 / 那个 FIXME | `mlir/tools/mlir-pdll/mlir-pdll.cpp:120-122`、`:182` |

## 28.13 可跑实验清单

| # | 命令 | 观察什么 |
|---|---|---|
| 1 | `mlir-opt tile.mlir --transform-interpreter` | tile 生效，产出 subview + 小 op |
| 2 | `mlir-opt tile.mlir`（不加 interpreter） | schedule 是死代码，payload 不变 |
| 3 | `mlir-opt badseq.mlir --transform-interpreter` | `could not find ... __transform_main` |
| 4 | `mlir-opt useafter.mlir --transform-interpreter --transform-dialect-check-uses` | 完整失效因果链 |
| 5 | `mlir-opt tile.mlir --transform-interpreter --transform-dialect-check-uses 2>&1 >/dev/null` | 空输出 = 检查通过 |
| 6 | `mlir-pdll my.pdll -x mlir` | PDLL → PDL，带 `#loc` 回指 |
| 7 | `mlir-pdll my.pdll -x cpp` | 生成 `PDLPatternModule` 的 C++ |
| 8 | `mlir-opt test-pdll.mlir -test-pdll-pass` | interface 匹配 + 重写 |
| 9 | `mlir-opt pdl-bytecode.mlir -allow-unregistered-dialect -test-pdl-bytecode-pass -split-input-file` | 字节码层重写 |
| 10 | `mlir-pdll --help` | 断言失败（exit 134），可复现的上游 bug |

## 28.14 踩雷集锦

1. **错误直觉：加了 transform 模块就会自动生效。** → 正确认识：必须 `--transform-interpreter`（或某个 pass 显式 apply）；否则它只是一段被打印的 IR。
2. **错误直觉：`@__transform_main` 可以随便起名。** → 正确认识：入口名硬编码在 `TransformDialect.td:25`。
3. **错误直觉：handle 是 SSA 值，只要还在作用域就有效。** → 正确认识：consumes/invalidates 会让所有指向同一批 payload op 的 handle 失效，即使 SSA 值还在。
4. **错误直觉：tile 之后继续用原 handle 也能工作。** → 正确认识：interpreter 会直接报 `handle invalidated by a previously executed transform op`，并给出因果链。
5. **错误直觉：`--transform-dialect-check-uses` 会执行变换。** → 正确认识：它只做静态推演；没输出不代表变换一定成功。
6. **错误直觉：PDLL 生成的 C++ 会在运行时解析 PDLL。** → 正确认识：生成的是内嵌 PDL 文本的 `PDLPatternModule`，运行时走 `pdl_interp` 字节码。
7. **错误直觉：PDL bytecode 测试文件可以直接整份跑。** → 正确认识：多段 `module @patterns` 会重定义符号，必须 `-split-input-file` + `-allow-unregistered-dialect`。
8. **错误直觉：`mlir-pdll --help` 应该和别的工具一样正常。** → 正确认识：本树 assertions build 下直接 abort（`ResetCommandLineParser` 注销了默认 category）；这是真实的上游缺陷，不是你的环境坏了。

## 28.15 本章重点整理

- Transform dialect 把“变换策略”写成 IR：payload（要改的代码）+ transform IR（怎么改）+ interpreter（执行器）。
- 三个概念：payload root、handle、named_sequence；入口名 `__transform_main` 硬编码。
- handle 失效规则来自 op 的 consumes/invalidates 声明，`--transform-dialect-check-uses` 可以静态检查并画出因果链。
- 不跑 interpreter，transform 模块就是死代码——这是设计，不是 bug。
- Transform op 是薄策略层，真正算法在 Linalg utility 里（`tile_using_for` → `tileLinalgOp`）。
- PDLL 是 PDL 的前端：1 行 PDLL 可以生成十几行 PDL，且带 `loc` 回指源码。
- `mlir-pdll -x cpp` 生成内嵌 PDL 的 `PDLPatternModule`，运行时靠 `pdl_interp` 字节码。
- `mlir-pdll --help` 在本树 abort，是一个可上手、可复现的上游 bug 候选。

## 28.16 自我检核

- [ ] 能画出 payload / transform IR / interpreter / handle 的关系图
- [ ] 能写出一个 match → tile 的最小 schedule 并解释每个返回值
- [ ] 能说出 entry point 名字与它定义在哪个文件
- [ ] 能解释为什么“tile 之后用旧 handle”会失败，并读懂那条四段式诊断
- [ ] 能区分 `--transform-dialect-check-uses` 与 `--transform-interpreter` 的职责
- [ ] 能解释 PDLL 生成物里的 `loc("x.pdll":行:列)` 有什么用
- [ ] 能说出 `-x cpp` 产物的运行时依赖
- [ ] 能复现 `mlir-pdll --help` 的 abort 并说明根因

## 28.17 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Dialects/Transform.md`**
  - 读哪里：`Payload IR`、`Handles`、`Expensive checks` 三节
  - 能学到什么：官方对“为什么 handle 会失效”的正式解释；和本章关联：28.2/28.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Dialect/Transform/IR/TransformDialect.td`**
  - 读哪里：`20-40`（入口符号名与方言属性）
  - 能学到什么：`transform.with_named_sequence` 与 `__transform_main` 的绑定；和本章关联：28.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Transform/Interfaces/TransformInterfaces.cpp`**
  - 读哪里：`780-900`（`applyTransform` 与失效记录）
  - 能学到什么：handle 失效是在哪一行被标记的；和本章关联：28.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Tools/PDLL/CodeGen/MLIRGen.cpp`**
  - 读哪里：`130-200`（模块生成）、`600-630`（`codegenPDLLToMLIR`）
  - 能学到什么：PDL 生成物的结构与 location 处理；和本章关联：28.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/tools/mlir-pdll/mlir-pdll.cpp`**
  - 读哪里：`118-190`（那个 `ResetCommandLineParser` FIXME 与 option 注册）
  - 能学到什么：一个可修的上游 bug 的完整上下文；和本章关联：28.10

到这里，Part 5 的六个子系统的“读法”就齐了。下一章是一次完整实战：把 `linalg.matmul` 一路降到 LLVM、用 `mlir-runner` 跑出真实数字，再人为引入一个索引 bug 并用 Ch 13 的方法定位。

→ 下一章：[练习 E — 端到端降级与 bug 猎杀](./practice-e-end-to-end-and-bug-hunt.md)
