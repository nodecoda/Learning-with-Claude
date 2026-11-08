# Ch 01 — MLIR 在 LLVM 中的位置

> **目标**：能在 8,000 多个文件里快速定位“这条改动属于哪一层、会被谁链接、测试该放哪”，并说清 MLIR 与 LLVM IR 的边界。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance` 分支，`builds/ct-instr-trunk` 已启用 `clang;lld;mlir`；`mlir-opt` / `mlir-translate` 可用。

## 01.1 为什么维护者需要一张地图

MLIR 的目录结构不是按“文件类型”分的，而是按**抽象层**分的：

- 有人改 `mlir/lib/IR/Operation.cpp`，影响所有方言；
- 有人改 `mlir/lib/Dialect/Arith/IR/ArithOps.cpp`，只影响 arith；
- 有人改 `mlir/lib/Conversion/ArithToLLVM/`，只影响 arith → LLVM 的降级；
- 有人改 `mlir/lib/Target/LLVMIR/`，影响“MLIR 模块 → LLVM IR 模块”的翻译。

这四类改动的测试位置、review 人、回归风险完全不同。维护者最常见的错误不是“不会写 pattern”，而是**把改动放错层**：

- 把一个通用 canonicalization 写进某个 dialect，导致别的 dialect 享受不到；
- 把应该在 conversion 里做的类型转换写进 verifier；
- 改了 `mlir/lib/IR` 的公共头，却只跑了 arith 的测试；
- 把下游 dialect 的依赖加进 core，让 `mlir-opt` 越来越重。

所以第一章不急着读 `Operation` 的字段，而是先建立一张“层与依赖”的地图。

## 01.2 先建立直觉：一座五层塔

```
┌──────────────────────────────────────────────────────────────┐
│ 前端 / 下游方言：tosa、linalg、stablehlo、torch-mlir ...      │
│   （本仓库只有一部分；很多在 out-of-tree）                    │
├──────────────────────────────────────────────────────────────┤
│ Dialect：arith、func、scf、cf、memref、tensor、vector、gpu ... │
│   IR 对象 + ODS 定义 + verifier + canonicalization             │
├──────────────────────────────────────────────────────────────┤
│ 基础设施：IR / Pass / Rewrite / Analysis / Interfaces         │
│   Operation、Block、Region、Type、Attribute、Pattern、DataFlow │
├──────────────────────────────────────────────────────────────┤
│ 转换与出口：Conversion / Target / Bufferization                │
│   arith→LLVM、func→LLVM、gpu→NVVM、MLIR→LLVM IR               │
├──────────────────────────────────────────────────────────────┤
│ 支撑：Support（LLVM ADT/Support）、TableGen、Bytecode、C API  │
└──────────────────────────────────────────────────────────────┘
```

这张图有三个重要含义：

1. **越往下越稳定，越往上越活跃。** `mlir/lib/IR` 的接口改动要过 core maintainer；某个下游 dialect 的 op 可以自己演化的空间大得多。
2. **依赖只能向下。** `IR` 不能依赖 `Dialect/Arith`；`Arith` 可以依赖 `IR`；`Conversion` 可以依赖两边的 dialect；`Target/LLVMIR` 可以依赖 LLVM 的 IR 库。
3. **`mlir-opt` 是“全量链接”的测试工具，不是库的边界。** 它把大多数 dialect、pass、conversion 都链进来，所以你能在 `mlir-opt` 里跑的东西，不一定能在某个下游二进制里跑。

## 01.3 目录地图：8,038 个文件怎么分

先看本树的 tracked 文件数：

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
for d in mlir/include mlir/lib mlir/tools mlir/test mlir/docs mlir/examples mlir/python; do
  printf '%s %s\n' "$(git ls-files $d | wc -l)" "$d"
done
```

本机实测：

```text
1452 mlir/include
1655 mlir/lib
75 mlir/tools
4129 mlir/test
99 mlir/docs
197 mlir/examples
151 mlir/python
```

几个反直觉点：

- **测试比实现还多。** `mlir/test` 4,129 个文件，超过 `lib` + `include` 的总和。MLIR 的很多“规范”是用 lit 测试写下来的，不是文档。
- **`tools` 只有 75 个文件。** 工具本身很薄，真正的逻辑在 `lib/Tools` 和被链接的库里。
- **`docs` 只有 99 个文件，但质量高。** `LangRef.md`、`PassManagement.md`、`DialectConversion.md`、`DefiningDialects/` 是必读；不要只靠博客。
- **`examples` 不是玩具。** `examples/standalone` 是 Out-of-Tree 方言的最小完整工程，维护者加新方言时常拿它对照。

### `mlir/include` 与 `mlir/lib` 的对应关系

大多数目录是一一对应的：

| 目录 | 头文件 | 实现 | 维护者常改什么 |
|---|---|---|---|
| `IR` | `include/mlir/IR/` | `lib/IR/` | Operation/Block/Region/Type/Attribute、verifier、printer |
| `Pass` | `include/mlir/Pass/` | `lib/Pass/` | PassManager、analysis、crash recovery、statistics |
| `Rewrite` | `include/mlir/Rewrite/` | `lib/Rewrite/` | PatternApplicator、bytecode、frozen pattern set |
| `Analysis` | `include/mlir/Analysis/` | `lib/Analysis/` | dominance、dataflow、alias、value bounds |
| `Conversion` | `include/mlir/Conversion/` | `lib/Conversion/` | 各 dialect 到 LLVM/SPIR-V/GPU 的 lowering |
| `Dialect` | `include/mlir/Dialect/` | `lib/Dialect/` | 方言的 op/type/attr/transform |
| `Target` | `include/mlir/Target/` | `lib/Target/` | LLVM IR / SPIR-V / Cpp 出口 |
| `Interfaces` | `include/mlir/Interfaces/` | `lib/Interfaces/` | op/type/attr 接口与 external model |
| `Transforms` | `include/mlir/Transforms/` | `lib/Transforms/` | 通用 pass（canonicalize、inline、CSE、loop） |
| `TableGen` | `include/mlir/TableGen/` | `lib/TableGen/` | `.td` 的 C++ 侧封装 |
| `Tools` | `include/mlir/Tools/` | `lib/Tools/` | `mlir-opt`/`mlir-translate` 的公共驱动 |

`lib` 的顶层 `CMakeLists.txt` 就是这张表的构建顺序：

```bash
grep -n 'add_subdirectory' mlir/lib/CMakeLists.txt | head -25
```

本机实测（节选）：

```text
4:add_subdirectory(Support)
5:add_subdirectory(IR)
7:add_subdirectory(Analysis)
8:add_subdirectory(AsmParser)
9:add_subdirectory(Bytecode)
10:add_subdirectory(Dialect)
11:add_subdirectory(Conversion)
13:add_subdirectory(Interfaces)
15:add_subdirectory(Pass)
19:add_subdirectory(Rewrite)
20:add_subdirectory(TableGen)
21:add_subdirectory(Target)
22:add_subdirectory(Tools)
23:add_subdirectory(Transforms)
```

顺序不是随便排的：`Support` 最先，`IR` 紧随其后，`Dialect` 在 `IR` 之后，`Conversion` 在 `Dialect` 之后，`Tools` 最后。CMake 的 `add_subdirectory` 顺序不直接等于链接依赖，但它反映了“谁能用谁”的直觉。

## 01.4 五个层的职责边界

### 01.4.1 Support：不碰 IR 的底座

`mlir/lib/Support` 只有少量文件，主要复用 LLVM 的 `ADT`、`Support`、`CommandLine`。它不应该知道 `Operation` 是什么。

维护含义：如果你在 `Support` 里 `#include "mlir/IR/Operation.h"`，基本可以判定层次错了。

### 01.4.2 IR：所有方言共享的对象模型

`mlir/lib/IR` 定义：

- `MLIRContext`：uniquing、dialect 加载、线程池；
- `Operation` / `Block` / `Region` / `Value` / `OpOperand` / `OpResult`；
- `Type` / `Attribute` 及其 storage；
- `Verifier`、`AsmParser`/`AsmPrinter` 的入口；
- `PatternMatch`、`Builders`、`Diagnostics`。

这层的改动影响所有方言，review 最严。典型维护动作：

- 给 `Operation` 加一个通用字段/方法（很少见，风险高）；
- 修 use-list / erase / dominance 的边界；
- 改 verifier 的调用顺序；
- 改 printer 的通用格式。

### 01.4.3 Dialect：每个方言自己的语义

`mlir/lib/Dialect/<Name>/` 通常分成：

```
Dialect/Arith/
├── IR/            ← op/type/attr 定义、verifier、fold
├── Transforms/    ← 方言专属 pass
├── Utils/         ← 公共工具
└── CMakeLists.txt
```

维护含义：方言内部的 canonicalization 放 `Dialect/<Name>/IR` 或 `Transforms`；跨方言的降级放 `Conversion/`。不要把 `arith` 的 canonicalization 写进 `Transforms/Canonicalizer.cpp`，也不要把 `arith → LLVM` 的逻辑写进 `Dialect/Arith`。

### 01.4.4 Conversion：跨抽象层的重写

`mlir/lib/Conversion/` 按“源 → 目标”组织：

```
Conversion/ArithToLLVM/
Conversion/FuncToLLVM/
Conversion/SCFToControlFlow/
Conversion/GPUToNVVM/
Conversion/GPUToROCDL/
...
```

每个目录通常只有一个 `Convert*.cpp` 和一个 `CMakeLists.txt`。维护含义：

- 新增一条 lowering，先找同源/同目标的现有目录，照它的 `ConversionTarget` 和 `TypeConverter` 写；
- 测试放 `mlir/test/Conversion/<Source>To<Target>/`；
- reviewer 通常是该 target 的 owner（`mlir/Maintainers.md` 的 Egress 区）。

### 01.4.5 Target：出口

`mlir/lib/Target` 负责把 MLIR 模块变成外部表示：

- `Target/LLVMIR/`：MLIR → LLVM IR（`mlir-translate --mlir-to-llvmir`）；
- `Target/SPIRV/`：MLIR → SPIR-V；
- `Target/Cpp/`：MLIR → C++；
- `Target/Wasm/`：MLIR → WebAssembly。

维护含义：`Target/LLVMIR` 的 bug 往往表现为“MLIR 看着对，LLVM IR 不对”。测试要看 `mlir/test/Target/LLVMIR/`，不是 `mlir/test/Dialect/LLVMIR/`。

## 01.5 工具链：谁链接了谁

`mlir-opt` 是一个“全量链接”的可执行文件。看它链接了多少 MLIR 共享库：

```bash
ldd "$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-opt" | grep -c MLIR
```

本机实测：

```text
383
```

383 个 MLIR `.so`。这解释了三个现象：

1. **启动不慢，但链接很重。** shared libs 让增量链接快，但 `mlir-opt` 的依赖图非常大。
2. **改一个 core 头会触发大范围重编。** 因为几百个库都 include 它。
3. **`mlir-opt` 能跑的 pass 不等于你的下游工具能跑。** 下游通常只链接自己需要的 dialect/转换。

`mlir-opt` 的入口在 `mlir/tools/mlir-opt/mlir-opt.cpp`，它做三件事：

```cpp
registerAllPasses();
DialectRegistry registry;
registerAllDialects(registry);
return mlir::asMainReturnCode(mlir::MlirOptMain(...));
```

本机核对：

```bash
grep -n 'registerAllPasses\|registerAllDialects\|MlirOptMain' mlir/tools/mlir-opt/mlir-opt.cpp
```

本机实测：

```text
326:  registerAllPasses();
331:  registerAllDialects(registry);
347:  return mlir::asMainReturnCode(mlir::MlirOptMain(
```

维护含义：如果你新增一个 test pass，只在 `mlir/test/lib/...` 里注册还不够；`mlir-opt.cpp` 的 `registerTestPasses()` 必须调用到它，否则 lit 会报 `Unknown command line argument`。

## 01.6 生成物：`.inc` 不是源码

`mlir-tblgen` 把 `.td` 生成成 `.h.inc` / `.cpp.inc`，放在 build 目录：

```bash
ls "$LLVM_HOME/builds/ct-instr-trunk/tools/mlir/include/mlir/Dialect/Arith/IR/" | head
```

本机实测：

```text
ArithOps.cpp.inc
ArithOps.cpp.inc.d
ArithOps.h.inc
ArithOps.h.inc.d
ArithOpsAttributes.cpp.inc
ArithOpsAttributes.h.inc
ArithOpsDialect.cpp.inc
ArithOpsDialect.h.inc
```

整个生成头目录的体积：

```bash
du -sh "$LLVM_HOME/builds/ct-instr-trunk/tools/mlir/include/mlir"
```

本机实测：

```text
136M
```

三条维护规则：

1. **不要手改 `.inc`。** 下次 `ninja` 会被覆盖；真正要改的是 `.td` 或 `OpDefinitionsGen.cpp`。
2. **生成物进不了 PR。** 上游只 review `.td` 和生成器；生成物是 build artifact。
3. **读生成物是定位 ODS 问题的捷径。** `getODSOperands`、builder、parser/printer 的实际代码都在 `.inc` 里；`grep -n` 生成物比猜宏展开快。

## 01.7 与 LLVM IR 的边界

MLIR 不是 LLVM IR 的语法糖。两者至少有三处本质差异：

| 维度 | MLIR | LLVM IR |
|---|---|---|
| 抽象层 | 多级 dialect，可自定义 | 单一、固定的中端 IR |
| 类型系统 | 可扩展（dialect type） | 固定（整数/浮点/指针/向量/结构） |
| 结构 | Operation 可嵌套 Region | 函数/基本块两级 |
| 出口 | 通过 `Target/LLVMIR` 翻译 | 直接进后端 |

边界工具是 `mlir-translate`。但这里有一个高频坑：**`mlir-translate` 只注册了一部分 dialect**。直接翻译 `arith` 会失败：

```bash
mlir-translate --mlir-to-llvmir /tmp/map.mlir
```

本机实测：

```text
map.mlir:2:8: error: Dialect `arith' not found for custom op 'arith.constant'
  %c = arith.constant 1 : i32
       ^
note: Available dialects: arm_neon, arm_sme, arm_sve, builtin, dlti, func, gpu,
llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm
```

正确路径是先 `mlir-opt --convert-to-llvm`，再翻译：

```bash
mlir-opt --convert-to-llvm /tmp/map.mlir | mlir-translate --mlir-to-llvmir
```

本机实测：

```text
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

define i32 @f(i32 %0, i32 %1) {
  %3 = add i32 %0, %1
  ret i32 %3
}
```

注意 `%c = arith.constant 1` 被常量折叠掉了：`muli %s, 1` 在 conversion 前已经被 canonicalize/constant fold 消掉。这是另一个维护含义：**你看到的 LLVM IR 不是 MLIR 的逐句翻译，中间可能发生了优化**。要验证“某条 op 的 lowering”，先关掉无关的 canonicalization，或直接对 LLVM dialect 做 `mlir-translate`。

## 01.8 可跑实验

### 实验 1：数一数每个层的文件

```bash
cd /home/ubuntu/llvm/trees/ct-instr-trunk
for d in mlir/include/mlir/IR mlir/include/mlir/Dialect mlir/lib/IR mlir/lib/Dialect mlir/lib/Conversion mlir/lib/Target mlir/test/Dialect mlir/test/Conversion; do
  printf '%6s %s\n' "$(git ls-files $d | wc -l)" "$d"
done
```

本机实测：

```text
    90 mlir/include/mlir/IR
   824 mlir/include/mlir/Dialect
    48 mlir/lib/IR
   835 mlir/lib/Dialect
   250 mlir/lib/Conversion
    95 mlir/lib/Target
  1379 mlir/test/Dialect
   450 mlir/test/Conversion
```

`lib/IR` 只有 48 个实现文件，却支撑 90 个 IR 头、824 个 dialect 头、835 个 dialect 实现文件。这就是“核心小、生态大”的具体数字。

### 实验 2：确认 `mlir-opt` 的全量链接

```bash
ldd "$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-opt" | grep MLIR | wc -l
ldd "$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-opt" | grep -E 'MLIR(OptLib|RegisterAll|TestFuncToLLVM)' | head
```

本机实测：

```text
383
libMLIROptLib.so.24.0git => .../lib/libMLIROptLib.so.24.0git
libMLIRRegisterAllDialects.so.24.0git => .../lib/libMLIRRegisterAllDialects.so.24.0git
libMLIRRegisterAllPasses.so.24.0git => .../lib/libMLIRRegisterAllPasses.so.24.0git
libMLIRTestFuncToLLVM.so.24.0git => .../lib/libMLIRTestFuncToLLVM.so.24.0git
```

`MLIRTestFuncToLLVM` 也被链进来，说明 `mlir-opt` 连测试 dialect 的 lowering 都包含；这也是为什么 lit 能直接跑 test pass。

### 实验 3：看 `mlir-opt` 的 pass 命名空间

```bash
"$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-opt" --help | grep -E '^  --(convert-to-llvm|canonicalize|inline|symbol-dce|test-greedy)' | head
```

本机实测：

```text
  --canonicalize     -   Canonicalize operations
  --convert-to-llvm  -   Convert to LLVM dialect
  --inline           -   Inline function calls
  --symbol-dce       -   Eliminate dead symbols
  --test-greedy-patterns -   Test greedy pattern rewrites
```

这些名字是 pipeline 文本的原子；Ch 09 会把它们组合成嵌套 pipeline。

### 实验 4：观察生成物

```bash
grep -n 'getODSOperands' "$LLVM_HOME/builds/ct-instr-trunk/tools/mlir/include/mlir/Dialect/Arith/IR/ArithOps.h.inc" | head -5
```

本机实测：

```text
1399:  RangeT getODSOperands(unsigned index) {
1406:    return (*getODSOperands(0).begin());
1410:    return (*getODSOperands(1).begin());
1473:  ::mlir::Operation::operand_range getODSOperands(unsigned index) {
1479:    return ::llvm::cast<::mlir::TypedValue<::mlir::Type>>(*getODSOperands(0).begin());
```

生成物行号随 TableGen 版本变化；本章只把它当“ODS getter 在 `.inc` 里”的证据，不作为稳定锚点。

### 实验 5：验证 MLIR → LLVM IR 的边界

```bash
cat > /tmp/map.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %s = arith.addi %a, %b : i32
  return %s : i32
}
EOF
"$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-opt" --convert-to-llvm /tmp/map.mlir \
  | "$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-translate" --mlir-to-llvmir
```

本机实测：

```text
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

define i32 @f(i32 %0, i32 %1) {
  %3 = add i32 %0, %1
  ret i32 %3
}
```

### 实验 6：确认 `mlir-translate` 的 dialect 子集

```bash
"$LLVM_HOME/builds/ct-instr-trunk/bin/mlir-translate" --mlir-to-llvmir /tmp/map.mlir 2>&1 | head -4
```

本机实测：

```text
/tmp/map.mlir:2:8: error: Dialect `arith' not found for custom op 'arith.addi'
  %s = arith.addi %a, %b : i32
       ^
/tmp/map.mlir:2:8: note: Available dialects: arm_neon, arm_sme, arm_sve, builtin, dlti, func, gpu,
llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm
```

这条错误本身就是一个维护知识点：`mlir-translate` 的 registry 和 `mlir-opt` 的 registry 不是同一套。

## 01.9 源码落点

| 文件 | 行号/入口 | 作用 |
|---|---|---|
| `mlir/lib/CMakeLists.txt` | 4–23 | 库的构建顺序与层次 |
| `mlir/tools/CMakeLists.txt` | 1–15 | 工具列表；`mlir-runner` 条件编译 |
| `mlir/tools/mlir-opt/mlir-opt.cpp` | 326–347 | pass/dialect 注册与主入口 |
| `mlir/include/mlir/InitAllDialects.h` | 全文件 | `registerAllDialects` 声明 |
| `mlir/lib/IR/CMakeLists.txt` | `add_mlir_library(MLIRIR` | 核心 IR 库的源文件清单 |
| `mlir/lib/Dialect/Arith/IR/CMakeLists.txt` | 13: `add_mlir_dialect_library(MLIRArithDialect` | 方言库的典型写法 |
| `mlir/lib/Conversion/ArithToLLVM/CMakeLists.txt` | `add_mlir_conversion_library` | conversion 库的典型写法 |

核对命令：

```bash
grep -n 'add_subdirectory' mlir/lib/CMakeLists.txt | head -25
grep -n 'add_subdirectory' mlir/tools/CMakeLists.txt
grep -n 'registerAllPasses\|registerAllDialects\|MlirOptMain' mlir/tools/mlir-opt/mlir-opt.cpp
```

## 01.10 踩雷集锦

1. **把 MLIR 当 LLVM IR 的替代品。** MLIR 是多级 IR 框架，LLVM IR 只是它的一个出口目标；`llvm` dialect 不等于 LLVM IR。
2. **以为 `mlir-opt` 的 dialect 集合等于库的依赖。** `mlir-opt` 全量链接 383 个 `.so`，下游工具通常只链一小部分。
3. **直接 `mlir-translate --mlir-to-llvmir` 翻译 arith。** registry 里没有 arith；先 `--convert-to-llvm`。
4. **改 `.inc` 生成物。** 下次构建被覆盖；改 `.td` 或生成器。
5. **把方言专属 canonicalization 放进 `Transforms/Canonicalizer.cpp`。** 那是通用 driver，不是放方言规则的地方。
6. **把 conversion 逻辑放进 dialect 的 `IR/`。** 跨方言降级应放 `lib/Conversion/`，否则依赖方向会乱。
7. **只看 `mlir/test/Dialect`，不看 `mlir/test/Conversion` / `mlir/test/Target`。** 一个 bug 可能横跨三层，测试位置由“改动层”决定。
8. **在 `lib/Support` 里 include `mlir/IR`。** 这说明层次搞反了；Support 是最底层。

## 01.11 本章重点整理

- MLIR 按层组织：Support → IR → Dialect → Conversion → Target；`mlir-opt` 是测试用全量链接，不是库边界。
- `mlir/lib/IR` 只有 48 个实现文件，却支撑 90 个 IR 头、824 个 dialect 头、835 个 dialect 实现文件。
- `mlir/test` 4,129 个文件，测试数量超过实现；测试位置由改动层决定。
- `.inc` 是 build artifact；ODS 问题的根因在 `.td` 或 `OpDefinitionsGen.cpp`。
- MLIR → LLVM IR 的正确路径是 `--convert-to-llvm` 再 `mlir-translate`；`mlir-translate` 的 registry 是子集。
- 找 reviewer 看 `mlir/Maintainers.md` 的分区，不要随机 @。

## 01.12 自我检核

- [ ] 能画出 Support → IR → Dialect → Conversion → Target 的依赖方向，并说出每一层的典型目录
- [ ] 能解释为什么 `mlir/lib/IR` 文件少但影响面最大
- [ ] 能说出 `mlir-opt` 与下游工具在 dialect/pass 集合上的差异
- [ ] 能解释 `.td` → `.inc` 的生成关系，并知道为什么不能手改 `.inc`
- [ ] 能复现 `mlir-translate` 的 arith registry 错误，并用 `--convert-to-llvm` 绕过
- [ ] 能判断一个改动应该放在 `Dialect/`、`Conversion/` 还是 `Target/`

## 01.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/LangRef.md`**
  - 读哪里：先读 “High-Level Structure” 与 “Operations” 两节
  - 能学到什么：MLIR 的通用语法与 IR 结构，不绑定某个方言
  - 和本章关联：01.4 的 IR 层就是这份 LangRef 的实现
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Rationale/Rationale.md`**
  - 读哪里：整篇（不长）
  - 能学到什么：为什么 MLIR 要有多级 dialect、为什么不用单一 IR
  - 和本章关联：01.2 的五层塔就是这份 rationale 的工程化
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DefiningDialects/`**
  - 读哪里：`Operations.md`、`Assembly.md`、`AttributesAndTypes.md`
  - 能学到什么：一个方言从 `.td` 到 op/type/attr 的完整定义
  - 和本章关联：Part 3 会逐篇展开
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/examples/standalone/`**
  - 读哪里：`include/Standalone/`、`lib/`、`test/`
  - 能学到什么：Out-of-Tree 方言的最小完整工程，含 CMake
  - 和本章关联：练习 C 会照这个结构做
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/CMakeLists.txt`**
  - 读哪里：第 4–23 行的 `add_subdirectory`
  - 能学到什么：库的层次与构建顺序
  - 和本章关联：01.3 的目录表就是按这份 CMake 排的

地图有了。下一章把“构建与测试矩阵”拆开：同一个源码，为什么会有 assertions / release、shared / static、full / dependency-only 几种完全不同的行为。

→ 下一章：[Ch 02 构建系统与测试矩阵](./02-build-and-test-matrix.md)
