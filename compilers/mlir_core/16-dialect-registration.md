# Ch 16 Dialect 注册、bytecode 与兼容性

> **目标**：把"我的方言能跑"和"我的方言能被别人加载"区分开。学完你要能解释 `DialectRegistry`、dependent dialects、`--load-dialect-plugin` 三条加载路径各自解决什么问题，并且能读懂一个 `.mlirbc` 文件的头部、判断它能不能被当前版本读回来。

## 为什么需要这个？

写方言时最常见的三个失败长这样：

```text
error: Dialect `arith' not found for custom op 'arith.constant'
error: unregistered dialect: toy
error: input file is not a valid MLIR bytecode file
```

第一条是**依赖没声明**，第二条是**注册表没给到 context**，第三条是**版本/格式不匹配**。三个错误发生在三条不同的链路上：

```text
编译期：DialectRegistry::insert<ToyDialect>()       ← 告诉框架"存在这个类"
    ↓
context 期：registry.appendTo(context) / loadDialect  ← 真正实例化
    ↓
解析期：parser 看到 "toy.xxx" → 找已加载的 dialect   ← 语法分派
    ↓
bytecode：读文件头 → 校验 magic/version → 反序列化
```

大多数"方言加载不了"的困惑，是把这四步当成一件事。本章按顺序拆开。

## 先建立直觉：注册是"登记"，加载是"实例化"

`DialectRegistry` 里存的不是 dialect 对象，而是**构造函数和扩展信息**。它的类声明在 `include/mlir/IR/DialectRegistry.h:150`。

```cpp
DialectRegistry registry;
registry.insert<toy::ToyDialect, func::FuncDialect>();
```

`insert<T>()` 只是把 `T` 的 `TypeID`、allocator、`getDialectNamespace()` 记下来，**没有创建任何 dialect 实例**。实例是在 context 需要时才创建的：

```cpp
Dialect *MLIRContext::getOrLoadDialect(StringRef name) { ... }
```

这个函数在 `lib/IR/MLIRContext.cpp:468`，逻辑就是"hash 表里有就返回，没有就从 registry 里找构造函数、创建、调 `initialize()`"。

所以：

- **`insert` 过但没用到**：不占资源。
- **`insert` 了、解析到该方言的 op**：自动创建。
- **没 `insert`**：解析时报 `Dialect 'x' not found`，即使你的可执行文件链接了这个方言的静态库。

第三条是新手最容易踩的：链接成功 ≠ 注册成功。静态库里的符号被链接进来了，但没有任何代码把它加进 registry。

## 核心概念一：注册表如何进 context

`MlirOptMain` 的标准用法是一行：

```cpp
return asMainReturnCode(
    MlirOptMain(argc, argv, "toy optimizer driver\n", registry));
```

它内部会 `registry.appendTo(context)`（`MLIRContext::appendDialectRegistry`，`lib/IR/MLIRContext.cpp:424`）。如果你自己写 driver，这行要自己来：

```cpp
MLIRContext context;
context.appendDialectRegistry(registry);
context.loadDialect<toy::ToyDialect>();   // 也可以按需
```

`appendDialectRegistry` 而不是 `appendDialect` 的意义是：把"将来可能需要"的方言一次性交给 context，具体创建时机交给它自己决定。

一个实用模式是分层注册：

```cpp
void registerToyDialects(DialectRegistry &registry) {
  registry.insert<toy::ToyDialect>();
}

void registerToyPasses() { ... }
```

库对外只暴露这两个函数，driver 负责组装。这样下游用你的方言时，不用 include 你的每一个头文件。

## 核心概念二：dependent dialects

`registry.insert<ToyDialect>()` 只解决"toy 自己"。可是 toy 的 op 里可能出现 `arith.constant`——比如练习 C 里的 `toy.store` 需要常量。这时有两种写法：

- 在 driver 里再 `insert<arith::ArithDialect>()`；
- 在 `Toy_Dialect` 上声明 `dependentDialects`。

后者是 ODS 提供的：

```tablegen
def Toy_Dialect : Dialect {
  let name = "toy";
  let cppNamespace = "::toy";
  let dependentDialects = ["::mlir::arith::ArithDialect"];
}
```

生成的代码在 dialect 构造函数里（本机 `gen/toy/ToyOpsDialect.cpp.inc:18`）：

```cpp
ToyDialect::ToyDialect(::mlir::MLIRContext *context)
    : ::mlir::Dialect(getDialectNamespace(), context, ::mlir::TypeID::get<ToyDialect>()) {
  getContext()->loadDialect<::mlir::arith::ArithDialect>();
  initialize();
}
```

生成这段代码的位置在 `tools/mlir-tblgen/DialectGen.cpp:128`，字符串是 `"getContext()->loadDialect<{0}>();"`。

**注意语义**：dependent dialects 是"我这个方言一被加载，就顺手把这些也加载"。它是**无条件**的，不是"用到才加载"。所以不要在 `dependentDialects` 里塞一堆你偶尔才用的方言，那会让每个使用你方言的进程都多实例化几个 dialect。

## 示例一：dependent dialect 的 A/B 实测

driver 里我故意只注册两个：

```cpp
DialectRegistry registry;
// Note: arith is intentionally *not* registered here; it arrives through
// ToyDialect::getDependentDialects-style loading generated from ODS.
registry.insert<toy::ToyDialect, func::FuncDialect>();
```

测试输入 `test/mixed.mlir`：

```mlir
func.func @mixed() {
  %p = toy.alloc : !toy.pointer<f32>
  %c = arith.constant 1.000000e+00 : f32
  return
}
```

**有 `dependentDialects`** 时：

```mlir
module {
  func.func @mixed() {
    %0 = toy.alloc : !toy.pointer<f32>
    %cst = arith.constant 1.000000e+00 : f32
    return
  }
}
```

把 `.td` 里那一行注释掉、重新编译同一份代码，实测报错：

```text
error: Dialect `arith' not found for custom op 'arith.constant'
  %c = arith.constant 1.000000e+00 : f32
       ^
note: Available dialects: builtin, func, toy ; for more info on dialect
registration see https://mlir.llvm.org/getting_started/Faq/#registered-loaded-dependent-whats-up-with-dialects-management
```

`Available dialects: builtin, func, toy` 这行是排障的关键：它直接告诉你 context 里到底有什么。看到 arith 不在列表里，就不用再怀疑 parser 或 pass 了。

## 核心概念三：`--load-dialect-plugin`

`mlir-opt` 支持从动态库加载方言（`lib/Tools/mlir-opt/MlirOptMain.cpp:293`）：

```text
--load-dialect-plugin=<string>  - Load dialects from plugin library
```

它的用途是：**不重新编译 `mlir-opt`，把新方言塞进去**。对我们这种 out-of-tree 方言非常香。

但本机做不到，原因要说清楚（这一节是 `guided`，不是实测）：

- conda-forge 的 MLIR 是**静态库**版本。`Library\bin` 下没有 `MLIR*.dll`，只有运行期辅助 DLL（`mlir_runner_utils.dll` 等）。插件是 DLL，它需要和宿主共享同一份 MLIR 运行时状态；静态链接做不到。
- 上游也承认这点：`examples/standalone/CMakeLists.txt` 里插件子目录被包在 `if(NOT WIN32)` 中（第 81 行），Windows 上默认不构建。

要真的测，需要一套 `LLVM_BUILD_LLVM_DYLIB=ON` 的共享库构建，或者用 `-DMLIR_ENABLE_DIALECT_PLUGINS` 相关配置的上游构建。本课不伪造输出，所以这条路径标 guided。

**替代方案**（本章所有实测都走这条路）：自己写一个 `toy-opt`，链接 `MlirOptMain` + 你的方言库。多写 20 行 `main`，换来完全可控的注册表。

## 核心概念四：bytecode 是什么

`.mlirbc` 是 MLIR 的二进制序列化格式，官方文档在 `mlir/docs/BytecodeFormat.md`。开头几句很重要：

> This format is versioned and stable: we don't plan to ever break [...]
> older version of the format can be targetted.

也就是说：**新版本读得懂旧版本，旧版本读不了新版本**。这决定了你的发布策略。

`mlir-opt --help` 里与 bytecode 相关的选项（实测）：

```text
--elide-resource-data-from-bytecode   - Elide resources when generating bytecode
--emit-bytecode                       - Emit bytecode when generating output
--emit-bytecode-producer=<string>     - Use specified producer when generating bytecode output
--emit-bytecode-version=<value>       - Use specified bytecode when generating output
```

四件事：写不写、写哪个版本、谁是生产者、要不要省掉 resource 数据。

## 示例二：bytecode 往返与文件头

先用上游 `mlir-opt` 做一个最小实验：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$plain = ".\plain.mlir"
& "$mlir\mlir-opt.exe" $plain --emit-bytecode -o .\plain.mlirbc
```

实测大小 305 字节。头部 16 字节：

```text
4D 4C EF 52 0D 4D 4C 49 52 32 33 2E 31 2E 34 00
ML.R.MLIR23.1.4.
```

逐段读：

| 字节 | 含义 |
| --- | --- |
| `4D 4C EF 52` | magic，等价于 `ML\xEFR` |
| `0D` | varint 长度 13 |
| `MLIR23.1.4` | producer 字符串，13 字节 |
| `00` | 结束符 |

producer 就是 `LLVM_VERSION_STRING`，本地是 `MLIR23.1.4`。**这是排障时第一个该看的东西**：文件是谁写的、版本是多少。

我们自己的方言也能往返。`toy` 的模块（含 `!toy.pointer<f32>`、`!toy.array<4 x f32>`、`#toy.const<7 : f32>`）：

```powershell
& "...\toy-opt.exe" .\test\asm.mlir --emit-bytecode -o .\test\asm.mlirbc
Get-Item .\test\asm.mlirbc | Select-Object Length
```

```text
Length
------
   520
```

读回来再打印，三个自定义类型/属性都完整恢复：

```mlir
module {
  func.func @memory() {
    %0 = toy.alloc : !toy.pointer<f32>
    %1 = toy.load %0 : !toy.pointer<f32> -> f32
    toy.store %1, %0 : f32, !toy.pointer<f32>
    return
  }
  ...
  func.func @attrs(%arg0: f32, %arg1: f32) -> f32 {
    %0 = toy.add %arg0, %arg1 {tag = #toy.const<7 : f32>} : f32
    return %0 : f32
  }
}
```

为什么不用写任何序列化代码？因为默认路径会把 dialect type/attr 按 **textual 形式**写进 bytecode 的常量区，读的时候再走一遍你的 `parseType` / `parseAttribute`。这意味着：

- **你的 printer 和 parser 必须互相兼容**，否则 bytecode 往返会炸（而且错误信息会指向 bytecode 读取，不指向你的 parser）。
- 想省空间或做 schema 演进，才需要实现 `BytecodeDialectInterface` 的 `writeType` / `readType`。

## 示例三：版本区间与错误诊断

当前构建支持的 bytecode 版本区间是 **0 到 6**（6 是写出的默认版本）。实测：

```powershell
& "$mlir\mlir-opt.exe" $plain --emit-bytecode --emit-bytecode-version=99 -o out.mlirbc
```

```text
error: unsupported version requested 99, must be in range [0, 6]
```

用 5 和 0 写出来的文件，当前版本都能读回来（实测输出与默认版本一致）：

```text
=== read v5 ===
module {
  func.func @f(%arg0: i32) -> i32 {
    %c1_i32 = arith.constant 1 : i32
    %0 = arith.addi %arg0, %c1_i32 : i32
    return %0 : i32
  }
}
```

改坏一个字节会得到带上下文的诊断：

```text
error: invalid string index: 31
note: in bytecode version 6 produced by: MLIR23.1.4
```

`note` 里的两个信息（版本、生产者）来自文件头，这就是为什么头部值得记。读者要注意：错误文本可能随版本变化，但"版本 + 生产者"这条 note 是稳定的定位手段。

## 版本兼容与 `#dialect.version`

这里要澄清一个常见误解。MLIR 23.1.4 **没有**可手写的 `#dialect.version` 这种文本语法。你不可能在 `.mlir` 里写一行 `#dialect.version<3>` 来声明方言版本。

版本信息实际存在于 bytecode 的 **dialect section** 里（`BytecodeFormat.md:182-188`）：

```text
nameAndIsVersioned: varint  // (dialectID << 1) | (hasVersion),
version: dialect_version_section  // only if versioned
dialect_version_section {
  version: byte[]
}
```

也就是说，**只有实现了 `BytecodeDialectInterface` 并返回非空 version 的方言，才会在文件里带自己的版本段**。接口在 `include/mlir/Bytecode/BytecodeDialectInterface.td`：

- `writeVersion`（第 63 行）：写文件时决定写什么版本号；
- `readVersion`（第 70 行）：读文件时拿到版本号；
- `upgradeFromVersion`（第 83 行）：读到旧版本时，把 IR 升级到当前语义。

一个真实的演进流程长这样：

1. v1 的方言把 `toy.pointer<T>` 序列化成 `[pointee]`。
2. 你给类型加了 `addressSpace` 字段，版本升到 v2。
3. 读 v1 文件时，`upgradeFromVersion(1)` 给所有 pointer 补上默认 address space = 0。
4. 写文件时按 v2 写；如果目标版本是 v1（`--emit-bytecode-version` 只控全局版本，方言版本另有机制），则要降级或拒绝。

没有这套机制时，你的选择只有两个：**永远兼容旧字段**（读不到就当默认值），或者**直接不兼容**（新版本读旧文件报错）。教学项目选哪个都行，生产项目必须显式选。

## 底层机制：为什么注册顺序会出问题

`insert<ToyDialect, FuncDialect>()` 的顺序不影响解析，但**加载顺序**可能影响 `dependentDialects` 的连锁。考虑 A 依赖 B、B 依赖 C：

- 解析到 A 的 op → 创建 A → A 的构造函数 `loadDialect<B>()` → 创建 B → B 的构造函数 `loadDialect<C>()` → 创建 C。
- 如果 B 依赖 A，就是循环。MLIR 不检测这个环，你会得到递归构造或者未初始化状态。

实践规则：**依赖方向必须是 DAG，且只依赖比你"更低层"的方言**。toy 依赖 arith 合理；arith 依赖 toy 就是设计错误。

另一条规则是**不要在 `initialize()` 里加载别的方言**。`initialize()` 的职责是 `addOperations` / `addTypes` / `addAttributes`，此时 dialect 还没完全登记进 context，加载别人容易踩到半初始化状态。要声明依赖，用 `dependentDialects`，让框架在构造函数里按顺序处理。

## 对比与取舍

| 加载方式 | 适用场景 | 代价 |
| --- | --- | --- |
| `registry.insert<T>()` + 自建 driver | out-of-tree 方言、教学项目 | 每个工具都要写一个 `main` |
| `dependentDialects` | 你的 IR 里必然出现别的方言 | 无条件加载，启动成本固定 |
| `--load-dialect-plugin` | 已有 `mlir-opt` 想临时加方言 | 需要共享库构建；Windows 上默认不可用 |
| `--allow-unregistered-dialect` | 只想 dump/搬运不认识的 IR | 不验证、不打印自定义语法，容易掩盖问题 |

最后一行值得多说一句：`--allow-unregistered-dialect` 会让未知 op 以 generic 形式通过，看起来"能跑"，但所有依赖具体 op 的 pass 都不会生效。它是排障工具，不是部署方案。

## 踩雷集锦

1. **链接了方言库就以为注册好了**。静态库链接 ≠ registry 里有条目。症状是 `Dialect 'toy' not found`，但符号明明在。
2. **只注册 toy，忘了 arith**，而 IR 里正好有 `arith.constant`。用 `dependentDialects` 或显式 `insert` 解决。
3. **在 `dependentDialects` 里放了个会反向依赖你的方言**。构造函数递归，症状是栈溢出或半初始化。
4. **以为 `--emit-bytecode-version` 能控方言版本**。它控的是**格式**版本（0–6），方言版本在 dialect section 里，由 `BytecodeDialectInterface` 决定。
5. **改了 printer 没改 parser，直接跑 bytecode 往返**。文本能读、bytecode 读不回来，报错位置在 reader 里，很难联想到 printer。
6. **在 `.mlir` 里找 `#dialect.version`**。23.1.4 没有这个语法，别在文档里被旧版本或别的项目误导。
7. **用 `--allow-unregistered-dialect` 跑生产 pipeline**。IR 能过，但你的 canonicalize 一条都不会匹配。

## 进阶：再往深一层

- **`--emit-bytecode-producer`**。把 producer 字符串改成自己的工具名，排障时能一眼看出文件是谁写的。
- **resource 数据**。`--elide-resource-data-from-bytecode` 会省掉 dialect resource 的内容，只留引用；对包含大块外部数据的 IR 很有用，但读回来需要资源文件在位。
- **bytecode 作为测试输入**。`mlir-opt` 能直接吃 `.mlirbc`，但 lit 测试里通常还是用 `.mlir`，因为 diff 可读。bytecode 更适合做"序列化稳定性"测试。
- **跨版本读**。新版读旧版是承诺，旧版读新版一定失败。做工具链发布时，把"最低可读版本"写进文档，比让用户猜要好。

## 动手练习

1. **给 toy 加一个新的 dependent dialect**（比如 `cf`），用 `--mlir-print-ir-after-all` 之类的观察无法直接看到加载，那就写个 3 行 driver 打印 `context.getLoadedDialects()`，确认它被加载了。
2. **构造一个循环依赖**（A 依赖 B，B 依赖 A），观察实际行为，并解释为什么框架不检测。
3. **把 `toy` 的 printer 改成输出 `!toy.ptr<...>`，parser 不改**，然后做 bytecode 往返，贴出失败信息，说明为什么文本路径没暴露这个问题。
4. **用 `--emit-bytecode-version=0` 写文件**，再用当前 `mlir-opt` 读，确认兼容性承诺。
5. **手工改坏文件头里的 producer 字符串**（长度不变），看诊断是否仍然指向正确的版本。
6. **对比 `--allow-unregistered-dialect`**：写一段含未知 op 的 IR，分别加与不加这个 flag 跑 `--canonicalize`，解释差异。

## 本章重点整理

- 注册（registry）→ 加载（context）→ 解析（parser）→ 反序列化（bytecode）是四条不同的链路。
- `insert<T>()` 只登记构造信息，实例化发生在 `getOrLoadDialect`。
- `dependentDialects` 生成的是构造函数里的 `loadDialect<T>()`，无条件执行，方向必须是 DAG。
- `--load-dialect-plugin` 需要共享库构建；本机 conda 包是静态的，Windows 上游也默认跳过，标 guided。
- bytecode 头部 = magic + producer 长度 + producer 字符串，版本区间 0–6。
- 23.1.4 没有 `#dialect.version` 文本语法；方言版本在 bytecode 的 dialect section，由 `BytecodeDialectInterface` 控制。
- 默认序列化走 textual 形式，所以 parser/printer 的一致性直接决定 bytecode 能否往返。

## 自我检核

- [ ] 能区分"链接了方言库"和"registry 里有方言"这两件事
- [ ] 能解释 `Available dialects: builtin, func, toy` 这行诊断说明了什么
- [ ] 知道 `dependentDialects` 生成在构造函数里，而不是 `initialize()` 里
- [ ] 能说出 bytecode 头部三个字段的含义
- [ ] 知道本机为什么测不了 `--load-dialect-plugin`，以及替代方案
- [ ] 能解释为什么"改了 printer 会炸 bytecode 往返"
- [ ] 不会再去找 `#dialect.version` 这种语法

## 延伸阅读

- **`mlir/docs/BytecodeFormat.md`**：读 130–200 行；学文件头与 section 布局；对应本章的 hexdump 解读。
- **`mlir/include/mlir/Bytecode/BytecodeDialectInterface.td`**：读 1–100 行；学 `writeVersion` / `readVersion` / `upgradeFromVersion` 的契约；是"版本兼容"一节的依据。
- **`mlir/lib/IR/MLIRContext.cpp`**：读 420–500 行；学 registry 如何进 context、`getOrLoadDialect` 如何实例化；能解释注册与加载的区别。
- **`mlir/tools/mlir-tblgen/DialectGen.cpp`**：读 120–140 行；看 `dependentDialects` 生成的代码；对照本章 `gen/toy/ToyOpsDialect.cpp.inc:18`。
- **`mlir/examples/standalone/`**：读 `CMakeLists.txt` 与 `standalone-plugin/`；学上游如何组织 out-of-tree 方言与插件；注意 Windows 上的 `if(NOT WIN32)`。

→ [练习 C：实现一个 standalone dialect](./practice-c-standalone-dialect.md)