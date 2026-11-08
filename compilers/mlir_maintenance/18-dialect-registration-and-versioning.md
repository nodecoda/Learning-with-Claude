# Ch 18 — 方言注册、扩展与版本：为什么它“找不到 dialect”

> **目标**：把“注册 → 加载 → 使用”这条链讲清楚：`DialectRegistry` 里存什么、`MLIRContext` 何时真正构造 `Dialect`、`--allow-unregistered-dialect` 放行了什么、dialect extension 如何在不改方言的前提下加能力，以及 bytecode 版本如何影响可读性。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（只读），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM/MLIR `24.0.0git`，assertions ON。

> **前置**：Ch 17 的 context/storage；知道 `mlir-opt` 会注册一批方言。

## 18.1 为什么“找不到 dialect”是最常见的用户错误

MLIR 用户遇到最多的错误不是类型不匹配，而是：

```text
error: operation being parsed with an unregistered dialect.
If this is intended, please use -allow-unregistered-dialect
```

或者：

```text
error: can't load dialect 'foo': missing registration?
```

这两条消息指向同一套机制的两个阶段：

1. **注册（registration）**：把“namespace → 构造函数”放进 `DialectRegistry`。
2. **加载（loading）**：`MLIRContext` 第一次需要某个 namespace 时，调用构造函数，真正创建 `Dialect` 实例，并执行它的 `initialize()`。

维护者必须分清这两件事，因为：

- **注册了不等于加载了**。`mlir-opt` 注册 50 个方言，但一段只含 `arith` 的 IR 不会加载 `gpu`。
- **加载了不等于能用所有 op**。方言可以懒加载，op 的 parser 需要 `OperationName` 已注册。
- **未注册方言可以“放行”**，但拿不到 op 的 C++ 类、verifier、接口；generic form 能解析，custom form 不能。

版本问题则出现在第三个维度：**bytecode 文件里的 dialect version**。注册/加载决定“能不能读”，version 决定“读出来的语义对不对”。

## 18.2 先建立直觉：registry、context、dialect 三层

```text
  DialectRegistry（静态表）
  ├── "arith"  → [](MLIRContext *ctx) { return ctx->getOrLoadDialect<ArithDialect>(); }
  ├── "func"   → ...
  ├── "test"   → ...
  └── extensions: [TestTransformDialectExtension, ...]
          │
          │  registry.appendTo(contextRegistry)
          ▼
  MLIRContext
  ├── loadedDialects: {"builtin" → BuiltinDialect*, ...}
  ├── typeUniquer / attributeUniquer
  └── registry（context 自己的副本）
          │
          │  第一次 parse/使用 "arith"
          ▼
  Dialect 实例（ArithDialect）
  ├── initialize()：注册 op、type、attr、interface
  └── namespace = "arith"
```

关键点：

- **registry 是“构造函数表”**，不是方言实例表；它可以被复制/合并（`appendTo`）。
- **context 拥有 dialect 实例**；同一个 context 里一个 namespace 只有一个实例。
- **`initialize()` 是注册 op/type/attr 的地方**；它在 dialect 第一次加载时执行一次。
- **extension 挂在 registry 上**，等它依赖的 dialect 都加载后，才应用到这个 dialect/context。

## 18.3 注册 API：insert、insertDynamic、appendTo、extensions

最常用的注册是模板 `insert<T>()`（`DialectRegistry.h:162`）：

```cpp
template <typename ConcreteDialect>
void insert() {
  insert(TypeID::get<ConcreteDialect>(),
         ConcreteDialect::getDialectNamespace(),
         static_cast<DialectAllocatorFunction>(([](MLIRContext *ctx) {
           return ctx->getOrLoadDialect<ConcreteDialect>();
         })));
}
```

它做三件事：拿 `TypeID`、拿 namespace、存一个 allocator。真正创建实例要等到 `getOrLoadDialect`。

| API | 位置 | 用途 |
|---|---|---|
| `insert<T>()` | `DialectRegistry.h:162` | 注册静态方言 |
| `insert(TypeID, name, ctor)` | `DialectRegistry.h:183`；`Dialect.cpp:217` | 注册任意 constructor |
| `insertDynamic(name, ctor)` | `DialectRegistry.h:197`；`Dialect.cpp:251` | 运行期动态方言 |
| `appendTo(destination)` | `DialectRegistry.h:206` | 合并 registry（含 extension） |
| `addDialectToPreload(name)` | `DialectRegistry.h` 约 190 | 只预加载名字，不提供 allocator |
| `addExtension<E>()` | `DialectRegistry.h:262` | 注册 dialect extension |
| `applyExtensions(ctx/dialect)` | `DialectRegistry.h:248/252`；`Dialect.cpp:269/318` | 应用 extension（内部调用） |

一个容易忽略的约束：**同一 namespace 不能注册两个不同 TypeID**。`Dialect.cpp:217` 的 `insert` 会检查：

```cpp
if (!inserted.second && inserted.first->second.first != typeID) {
  llvm::report_fatal_error(
      "Trying to register different dialects for the same namespace: " + name);
}
```

维护含义：如果你 fork 了一个方言并保留同名 namespace，注册时会直接 fatal error，而不是“后注册的覆盖前面的”。

## 18.4 加载与放行：`getOrLoadDialect` 与 `--allow-unregistered-dialect`

`MLIRContext::getOrLoadDialect(name)`（`MLIRContext.cpp:488`）做三件事：

1. 查 `loadedDialects`；已加载则直接返回。
2. 查 context 的 registry；有 allocator 则调用它，构造并 `initialize()`。
3. 没有 allocator 则返回 `nullptr`（随后 parser 报 “unregistered dialect”）。

`mlir-opt` 在启动时把所有已链接方言 `insert` 进 registry，所以正常情况下第 2 步能命中。**用户遇到 “missing registration” 通常是因为**：

- 他们用的是自己写的工具，只注册了部分方言；
- 他们从 plugin 加载方言，但 plugin 没找到；
- 他们复制了 registry，但忘了 `appendTo`。

`--allow-unregistered-dialect` 影响的是 **parser**，不是 registry：

- 允许创建 `OperationName` 为“未注册”的 op；
- op 以 generic form 存在，没有 C++ 类、没有 verifier、没有 interface；
- custom assembly format 不可用，因为 custom parser 属于方言。

实测（见 18.7 实验 2/3）：不加 flag 直接报错；加了 flag 能 parse，但打印出来仍是 generic 形式。

### 18.4.1 preload：只知道名字，还没有 allocator

有些工具的方言列表来自字符串选项，直到运行期才知道要加载谁。`addDialectToPreload(name)` 允许先把名字放进 registry，等 context 自己的 registry 里出现对应 allocator 时再加载。加载失败时，`preloadSelectDialects` 会给出比 parser 更清楚的诊断（`Dialect.cpp:232-234`）：

```text
can't load dialect 'foo': missing registration?
```

维护含义：如果你看到这条消息，先查 `appendTo` 是否合并了 registry，再查 plugin/链接是否把方言带进来；不要先去改 parser。

## 18.5 Dialect extension：不改方言也能加能力

`DialectExtension` 是一个“等 dialect 加载后执行”的钩子（`DialectRegistry.h:77`）：

```cpp
template <typename DerivedT, typename... DialectsT>
class DialectExtension : public DialectExtensionBase {
public:
  virtual void apply(MLIRContext *context, DialectsT *...dialects) const = 0;
  ...
};
```

它解决的问题是：**下游想在 `transform` 方言上注册自己的 op/attr/interface，但不能改 `transform` 的源码**。做法是写一个 extension，声明依赖 `transform::TransformDialect`，在 `apply` 里调用 `registerTransformOps` / `registerAttributes`。

真实例子：`TestTransformDialectExtension.cpp:950` 的 `TestTransformDialectExtension`，在 `init()` 里注册 op 和 attr；注册入口在同文件 1015 行：

```cpp
void ::test::registerTestTransformDialectExtension(DialectRegistry &registry) {
  registry.addExtensions<TestTransformDialectExtension>();
}
```

维护含义：

- **extension 的 apply 时机由 registry 控制**；依赖的 dialect 没加载，extension 不会执行。
- **extension 可以声明 `declareDependentDialect`**，把依赖写进 dialect 的依赖图。
- **extension 注册的 interface 可以被 `dyn_cast` 到**，即使原 dialect 源码里没有。

> **未实测**：本构建的 `mlir-opt` 没有注册 `TestTransformDialectExtension`，因此无法用一条命令直接观察 extension 的 apply。上面的行号来自源码树；运行验证需要自己写一个注册该 extension 的工具并重编，本章不伪造输出。

## 18.6 bytecode：版本是文件的一部分

`mlir-opt --emit-bytecode` 生成的文件有一个固定头：

```text
4d 4c ef 52     "ML\xefR"        magic（4 字节）
0d              version 6 的 varint 编码
4d 4c 49 52 ... "MLIR24.0.0git"  producer
```

`0d` 看起来像 13，但它是 **varint**：单字节形式的编码是 `(value << 1) | 0x1`（`BytecodeWriter.cpp:242`）。所以 `0d = (6 << 1) | 1`，表示版本 6。不要手算文件头；用 `--emit-bytecode-version` 或 reader 的 `parseVersion` 验证。

`Encoding.h:29-53` 定义了版本演进：

| 版本 | 常量 | 引入的能力 |
|---:|---|---|
| 0 | `kMinSupportedVersion` | 最低可读版本 |
| 1 | `kDialectVersioning` | dialect version 段 |
| 2 | `kLazyLoading` | 惰性加载 isolated region |
| 3 | `kUseListOrdering` | 编码 use-list 顺序 |
| 4 | `kElideUnknownBlockArgLocation` | 省略 block arg 的 unknown location |
| 5 | `kNativePropertiesEncoding` | properties 原生编码 |
| 6 | `kNativePropertiesODSSegmentSize` | ODS segment size 作为原生 properties |
| 6 | `kVersion` | 当前版本 |

writer 在 `BytecodeWriter.cpp:750` 检查范围：

```cpp
if (config.bytecodeVersion < bytecode::kMinSupportedVersion ||
    config.bytecodeVersion > bytecode::kVersion)
  return rootOp->emitError()
         << "unsupported version requested " << config.bytecodeVersion
         << ", must be in range [...]";
```

reader 在 `BytecodeReader.cpp:2020` 做双向检查：太旧不能升级，太新不能读。并且：

```cpp
// Override any request to lazy-load if the bytecode version is too old.
if (version < bytecode::kLazyLoading)
  lazyLoading = false;
```

这就是为什么 `-test-lazy-loading{bytecode-version=1}` 不会 materialize：版本 1 没有 lazy loading。

### 18.6.1 dialect version：语义版本，不是文件版本

除了文件级 `kVersion`，每个 dialect 还可以有自己的 version。接口在 `BytecodeDialectInterface.td:6`，读取钩子在 91 行 `readVersion`。`mlir/test/lib/IR/TestBytecodeRoundtrip.cpp:95` 的 `test-dialect-version` 选项就是用来测这条路径的。

维护含义：**文件版本决定“能不能读”，dialect version 决定“读出来的 op 语义按哪一版解释”**。升级 dialect 语义时，通常要同时提供 downgrade/upgrade 回调，否则旧 bytecode 会被静默按新语义解释。

| 问题 | 文件版本（`bytecode::kVersion`） | dialect version |
|---|---|---|
| 这个文件能不能读？ | reader 的 `parseVersion` 检查 | 不参与 |
| op 的语义按哪版解释？ | 不参与 | `BytecodeDialectInterface::readVersion` |
| 谁负责升级/降级？ | writer/reader 的格式逻辑 | dialect 自己的回调 |
| 典型失败 | “bytecode version 7 is newer” | “does not support emitting version X” |

## 18.7 可跑实验

> 全部真跑；`$T`/`$S` 见 14.4。

### 实验 1：注册了多少方言（实测）

```bash
$T/bin/mlir-opt --show-dialects | tr ',' '\n' | wc -l
$T/bin/mlir-opt --show-dialects | cut -c1-200
```

实测：

```text
50
Available Dialects: acc,affine,amdgpu,arith,arm_neon,arm_sme,arm_sve,async,bufferization,builtin,cf,complex,dlti,emitc,func,gpu,index,irdl,linalg,llvm,math,memref,ml_program,mpi,nvgpu,nvvm,omp,pdl,pdl
```

50 个方言，但这是“已注册”，不是“已加载”。一段只有 `arith` 的 IR 只会加载 `builtin`/`arith`/`func` 等实际用到的方言。

### 实验 2：未注册方言的解析错误（实测）

```bash
printf 'func.func @f() {\n  %%0 = "my.unknown"() : () -> i32\n  return\n}\n' > /tmp/unreg.mlir
$T/bin/mlir-opt /tmp/unreg.mlir
```

实测：

```text
/tmp/unreg.mlir:2:20: error: operation being parsed with an unregistered dialect. If this is intended, please use -allow-unregistered-dialect with the MLIR tool used
  %0 = "my.unknown"() : () -> i32
                   ^
```

### 实验 3：`--allow-unregistered-dialect` 放行（实测）

```bash
$T/bin/mlir-opt --allow-unregistered-dialect /tmp/unreg.mlir
```

实测：

```text
module {
  func.func @f() {
    %0 = "my.unknown"() : () -> i32
    return
  }
}
```

注意输出仍是 generic 形式 `"my.unknown"()`；没有方言就没有 custom printer。

### 实验 4：bytecode roundtrip 与文件头（实测）

```bash
cat > /tmp/asm1.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %0 = arith.addi %a, %b : i32
  %1 = arith.constant 42 : i32
  %2 = arith.muli %0, %1 : i32
  return %2 : i32
}
EOF
$T/bin/mlir-opt --emit-bytecode /tmp/asm1.mlir -o /tmp/asm1.mlirbc
ls -l /tmp/asm1.mlirbc
xxd /tmp/asm1.mlirbc | head -2
$T/bin/mlir-opt /tmp/asm1.mlirbc
```

实测：

```text
-rw-rw-r-- 1 ubuntu ubuntu 307 Nov  8 19:18 /tmp/asm1.mlirbc
00000000: 4d4c ef52 0d4d 4c49 5232 342e 302e 3067  ML.R.MLIR24.0.0g
00000010: 6974 0001 2f07 0307 0303 0707 0303 090d  it../...........
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %c42_i32 = arith.constant 42 : i32
    %1 = arith.muli %0, %c42_i32 : i32
    return %1 : i32
  }
}
```

`4d4c ef52` 是 magic `ML\xefR`；`0d` 之后是 producer 字符串 `MLIR24.0.0git`。roundtrip 后文本与源文件语义一致（SSA 名按 printer 规则重写）。

### 实验 5：版本越界（实测）

```bash
for v in 0 1 6 7 99; do
  echo "== version $v =="
  $T/bin/mlir-opt --emit-bytecode --emit-bytecode-version=$v /tmp/asm1.mlir -o /tmp/v$v.mlirbc 2>&1 | head -2
done
```

实测：

```text
== version 0 ==
== version 1 ==
== version 6 ==
== version 7 ==
/tmp/asm1.mlir:0:0: error: unsupported version requested 7, must be in range [0, 6]
/tmp/asm1.mlir:0:0: note: see current operation:
== version 99 ==
/tmp/asm1.mlir:0:0: error: unsupported version requested 99, must be in range [0, 6]
/tmp/asm1.mlir:0:0: note: see current operation:
```

0/1/6 合法（读侧不一定支持所有旧特性，但写侧允许）；7/99 越界。

### 实验 6：dialect version 的回调（实测）

```bash
$T/bin/mlir-opt $S/mlir/test/Bytecode/bytecode_callback.mlir \
  --test-bytecode-roundtrip="test-dialect-version=1.2" -verify-diagnostics | head -3
```

实测：

```text
Overriding IntegerType encoding...
Overriding IntegerType encoding...
Overriding parsing of IntegerType encoding...
```



版本 2.0 时这三条 “Overriding” 都不再出现，因为回调只对 `< 2.0` 生效。这个实验说明：**dialect version 是给“旧语义兼容”用的，不是文件格式版本**。

### 实验 7：downgrade 诊断（实测）

```bash
$T/bin/mlir-opt $S/mlir/test/Bytecode/bytecode_downgrade.mlir \
  --test-bytecode-roundtrip="test-dialect-version=1.2 test-kind=6" \
  -verify-diagnostics
```

实测：

```text
downgrading op...
module {
  "test.versionedA"() <{dims = 123 : i64, modifier = false}> : () -> ()
}
module {
  "test.versionedA"() <{dims = 123 : i64, modifier = false}> : () -> ()
}
```

“downgrading op...” 是 dialect 自己的 downgrade 回调打的；两个 module 分别是写入前/读回后的打印。

### 实验 8：lazy loading 与版本门槛（实测）

```bash
$T/bin/mlir-opt -test-lazy-loading \
  $S/mlir/test/Bytecode/bytecode-lazy-loading.mlir 2>&1 | head -14
```

实测：

```text
Has 1 ops to materialize


Before Materializing...

"builtin.module"() ({
}) : () -> ()

Materializing...

"builtin.module"() ({
  "func.func"() <{function_type = () -> (), sym_name = "op_with_passthrough_region_args"}> ({
  }) : () -> ()
}) : () -> ()
```

第一行 `Has 1 ops to materialize` 说明 `func.func` 被惰性加载；materialize 后 region 内 op 才出现。版本 1 时这个 pass 不会 lazy load（`BytecodeReader.cpp:2032`）。

## 18.8 源码落点

| 内容 | 路径 | 关键行/符号（本树 `grep -n` 核对） |
|---|---|---|
| registry 的 `insert<T>` | `mlir/include/mlir/IR/DialectRegistry.h` | 162 |
| `insert(TypeID,...)` | 同上 / `mlir/lib/IR/Dialect.cpp` | 183 / 217 |
| `insertDynamic` | 同上 / 同上 | 197 / 251 |
| `appendTo` | `DialectRegistry.h` | 206 |
| `applyExtensions` | 同上 / `Dialect.cpp` | 248/252 / 269/318 |
| `addExtension`/`addExtensions` | `DialectRegistry.h` | 255 / 262 |
| `DialectExtension` | 同上 | 45 base；77 模板 |
| `getOrLoadDialect(name)` | `mlir/lib/IR/MLIRContext.cpp` | 488 |
| `getOrLoadDialect<T>()` | `mlir/include/mlir/IR/MLIRContext.h` | 108 |
| dialect 构造 | `mlir/lib/IR/Dialect.cpp` | 35 |
| promised interface | `mlir/include/mlir/IR/Dialect.h` | 211 |
| bytecode 版本枚举 | `mlir/include/mlir/Bytecode/Encoding.h` | 29–53 |
| writer 版本检查 | `mlir/lib/Bytecode/Writer/BytecodeWriter.cpp` | 750 |
| reader 版本检查 | `mlir/lib/Bytecode/Reader/BytecodeReader.cpp` | 2020 |
| lazy loading 门槛 | 同上 | 2032 |
| dialect version 接口 | `mlir/include/mlir/Bytecode/BytecodeDialectInterface.td` | 6；91 `readVersion` |
| 测试 extension | `mlir/test/lib/Dialect/Transform/TestTransformDialectExtension.cpp` | 950 类；1015 注册 |

## 18.9 踩雷集锦

1. **把“注册了”当“加载了”。** → registry 只是构造函数表；`getOrLoadDialect` 才创建实例。
2. **以为 `--allow-unregistered-dialect` 会补上 C++ 类。** → 它只让 parser 放行 generic op；没有 verifier、interface、custom printer。
3. **同 namespace 注册两个方言。** → `Dialect.cpp:217` 直接 `report_fatal_error`；fork 时改 namespace。
4. **忘了 `appendTo` 合并 registry。** → 子工具只拿到自己的方言；报 “missing registration”。
5. **以为 extension 会立即执行。** → 它等依赖 dialect 加载后才 apply；依赖没加载就永远不跑。
6. **手算 bytecode 文件头的版本字节。** → 版本是 varint，且后面跟着 producer/section；用 reader 或 `--emit-bytecode-version` 验证。
7. **把 dialect version 当文件版本。** → 文件版本决定“能不能读”，dialect version 决定“按哪版语义解释”。
8. **在旧 bytecode 上开 lazy loading。** → reader 会在 `version < kLazyLoading` 时强制关掉；不是“选项没生效”，是版本不允许。

## 18.10 本章重点整理

- 方言生命周期分注册（registry）与加载（context）；两者不是同一件事。
- `insert<T>()` 存的是 allocator，不是实例；`getOrLoadDialect` 才构造并 `initialize()`。
- 同 namespace 只能有一个 TypeID；冲突会 fatal error。
- `--allow-unregistered-dialect` 只影响 parser，不提供 C++ 语义。
- `DialectExtension` 是“依赖 dialect 加载后执行”的注册钩子，可跨项目加 op/attr/interface。
- bytecode 版本 0–6 逐级引入 dialect versioning、lazy loading、use-list、properties 等能力。
- 文件版本与 dialect version 是两套版本；升级语义要提供 downgrade/upgrade 回调。

## 18.11 自我检核

- [ ] 能说出 registry 里存的三样东西（TypeID、namespace、allocator）
- [ ] 能解释 `insert<T>()` 与 `getOrLoadDialect<T>()` 的分工
- [ ] 能复现“未注册方言”的错误，并说明 `--allow-unregistered-dialect` 放行了什么
- [ ] 能说出 extension 的 apply 时机，以及依赖 dialect 未加载时的行为
- [ ] 能背出 bytecode 版本 1/2/5/6 各自引入的能力
- [ ] 能解释为什么 `--emit-bytecode-version=7` 会失败
- [ ] 能区分文件版本与 dialect version
- [ ] 能定位 reader 里强制关闭 lazy loading 的那行代码

## 18.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/DialectRegistry.h`**（314 行）
  - 读哪里：45–110 的 `DialectExtension`；162–215 的 `insert`；248–300 的 extension API。
  - 能学到什么：注册表的完整能力，以及 extension 的模板约束。
  - 和本章关联：18.3/18.5 的所有 API 都在这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/Dialect.cpp`**（362 行）
  - 读哪里：35 的 `Dialect` 构造；217–330 的 registry 实现。
  - 能学到什么：namespace 冲突、dynamic dialect、extension apply 的真实代码。
  - 和本章关联：18.3 的错误检查来自这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Bytecode/Encoding.h`**（141 行）
  - 读哪里：25–60 的 `BytecodeVersion` 枚举。
  - 能学到什么：每个版本号对应的能力边界。
  - 和本章关联：18.6 的版本表就是这份枚举。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Bytecode/Reader/BytecodeReader.cpp`**
  - 读哪里：2020–2040 的 `parseVersion`；1050–1080 的 lazy loading 处理。
  - 能学到什么：版本检查、旧版本降级、lazy loading 门槛。
  - 和本章关联：18.6/18.7 的 reader 行为来源。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Transform/TestTransformDialectExtension.cpp`**
  - 读哪里：950–1016 的 extension 类与注册函数。
  - 能学到什么：真实 extension 如何注册 op/attr、如何声明依赖。
  - 和本章关联：18.5 的模板来自这里。

下一章是练习 C：把本章的 ODS 知识落到一个 `/tmp` 小方言上，用 `mlir-tblgen` 做三次演化并逐行解释 diff。

→ 下一章：[练习 C 用 ODS 演化一个小方言](./practice-c-ods-dialect-evolution.md)
