# Ch 06 — Properties、属性与 bytecode

> **目标**：分清 operation 的三层数据（inherent properties / discardable attributes / 真正运行时值），知道 ODS 的 `properties` 生成什么、`getInherentAttr` 为什么被标成“过渡设施”，以及 bytecode 的版本号如何决定“这份 `.mlirbc` 还能不能被新工具读”。读完你能判断一次 ODS 改动是 NFC 还是会破坏 bytecode 兼容。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `e44fff5decc7`，`T=/home/ubuntu/llvm/builds/ct-instr-trunk`。bytecode 实验产物都写在 `/tmp/mlirc`。

> **前置**：Ch 05（Attribute 的 storage 与 uniquing）；知道 ODS 的 `arguments` 里 `attr` 与 `prop` 的区别更好。

## 06.1 为什么维护者要分清“属性”和“property”

一个真实场景：你给某个 op 加了一个 `attr`：

```tablegen
let arguments = (ins I64Attr:$count);
```

本地测试全过，CI 也过。三个月后下游抱怨：“用新工具读旧 bytecode，`count` 丢了。”

根因是：**在 bytecode v5 之前，inherent attribute 和 discardable attribute 被合并成一个 legacy dictionary 写出**；读回时靠 “inherent-attribute setter” 还原。如果你的新字段没有走 property 的 attribute 转换，或者转换是 lossy 的，旧版本 reader 只能保留默认值。

`mlir/docs/BytecodeFormat.md:38` 把这条规则写得很直白：

```text
Bytecode version 5 introduced native operation property encoding. Versions 0
through 4 store only the operation's legacy attribute dictionary, formed by
combining its inherent and discardable attributes. When reading these versions,
operation properties are initialized to their defaults and then populated using
the legacy inherent-attribute setters.
```

所以维护者必须能回答三个问题：

1. 这个字段是 **op 语义的一部分**（inherent）还是**可丢弃的注解**（discardable）？
2. 它在 C++ 里是 **property struct 字段**还是 **`DictionaryAttr` 里的键**？
3. 这次改动会不会让 **v0–v4 的 back-deployment 变得 lossy**？

## 06.2 先建立直觉：一个 op 的两套属性

```text
Operation
  ├── propertiesStorage ──▶ ODS 生成的 struct（inherent）
  │      { sym_name, function_type, ... }         // 语义的一部分
  │
  └── attrs : DictionaryAttr（discardable）
         { foo = 42 : i64, test.flag }            // 注解，可丢

generic 打印把两者分开：
  "func.func"() <{function_type = ..., sym_name = "f"}>   {...}
                  └──────── inherent ────────┘            └ discardable ┘
```

用真实输出验证（实验 1）：

```text
%0 = "arith.addi"(%arg0, %arg1) <{overflowFlags = #arith.overflow<none>}> {foo = 42 : i64} : (i32, i32) -> i32
```

`<{...}>` 是 inherent properties，`{...}` 是 discardable attributes。**括号形状就是类型信息**：任何时刻你都可以用 generic print 判断一个字段属于哪一层。

## 06.3 两条 API 线：inherent 与 discardable

`Operation.h` 把两套访问器并排放：

```cpp
std::optional<Attribute> getInherentAttr(StringRef name);          // :470
Attribute getDiscardableAttr(StringRef name) { return attrs.get(name); }  // :487
void setDiscardableAttr(StringAttr name, Attribute value);         // :514
auto getDiscardableAttrs();                                        // :540
```

注意 `getInherentAttr` 的注释（`Operation.h:465-469`）：

```text
This method is available as a transient facility in the migration process
to use Properties instead.
```

“transient” 是关键词：**它是给还在迁移中的 dialect 用的兼容层**，不是新代码应该依赖的主路径。新代码应当直接读 property 字段：

```cpp
auto count = op.getCount();          // ODS 生成的 getter，直读 properties
```

而 `getInherentAttr`/`setInherentAttr`（实现 `Operation.cpp:337`/`:341`）通过 `OperationName` 的模型转到 property：

```cpp
std::optional<Attribute> Operation::getInherentAttr(StringRef name) {
  return getName().getInherentAttr(this, name);
}
void Operation::setInherentAttr(StringAttr name, Attribute value) {
  getName().setInherentAttr(this, name, value);
}
```

`OperationName` 有两个模型：注册 op 走 ODS 生成的 `populateInherentAttrs`/`setInherentAttr`；未注册 op（`:1002`/`:1011`）把所有属性塞进一个 `DictionaryAttr` 当作 properties。于是 `getInherentAttr(name)` 能从这本字典查到，而 `getDiscardableAttrs()` 因为 `getPropertiesStorage()` 非空也会返回同一批条目——**不要用这两个 API 去区分未注册 op 的“固有/可丢”**。

`getDiscardableAttrs()` 的实现（`:540`）也体现了这个分支：

```cpp
return llvm::make_filter_range(attrs.getValue(),
    [this, attributeNames](const NamedAttribute attribute) {
      return getPropertiesStorage() ||
             !llvm::is_contained(attributeNames, attribute.getName());
    });
```

有 property storage 时谓词直接为真（字典本身只装 discardable；未注册 op 是例外，见上）；没有 property storage 时，才需要过滤掉 inherent 名单里的键。

## 06.4 Properties 从哪来：ODS 的 `prop-dialog`

`Properties.td` 定义了 property 的“类型语言”。最小的 `Property`（`:29`）：

```tablegen
class Property<string storageTypeParam = "", string desc = "",
               string interfaceTypeParam = storageTypeParam>
    : PropConstraint<TruePred, desc, interfaceTypeParam> {
  code storageType = storageTypeParam;
  code convertFromStorage = "$_storage";
  code assignToStorage = "$_storage = $_value";
  code convertToAttribute = ...
}
```

常用预定义（都在 `Properties.td`）：

| 名字 | 行 | C++ storage 类型 | 用途 |
|---|---|---|---|
| `I32Prop` / `I64Prop` | 237 | `int32_t` / `int64_t` | 整数参数 |
| `StringProp` | 262 | `std::string`（接口 `StringRef`） | 名字、符号 |
| `BoolProp` | 275 | `bool` | 开关 |
| `ArrayProp<...>` | 458 | `SmallVector` | 列表 |
| `OptionalProp<...>` | 603 | `std::optional` | 可缺省 |

在 ODS 里，`arguments` 的 `prop` 与 `attr` 是两种 dag 参数：

```tablegen
let arguments = (ins
  I64Prop:$count,          // 进 properties struct
  OptionalAttr<I64Attr>:$hint  // discardable / inherent attribute 路径
);
```

`convertToAttribute` / `convertFromAttribute` 是**给 legacy bytecode 与通用 attribute 视图用的桥**。维护者最常改错的就是这对函数：加字段时只改了 storage，没改转换，于是 v0–v4 往返丢值。

## 06.5 bytecode：magic、section、version

### 06.5.1 文件头

`BytecodeFormat.md:62` 规定的 magic 是四个字节：

```text
'4D' '4C' 'EF' '52'      // 'M' 'L' 'ï' 'R'
```

实测（实验 2）：

```text
00000000: 4d4c ef52 0d4d 4c49 5232 342e 302e 3067  ML.R.MLIR24.0.0g
```

`0d` 是随后 producer 字符串的长度（`MLIR24.0.0git` 正好 13 字节）。所以看到 `4d 4c ef 52` 就能确认“这是 MLIR bytecode，不是文本”。

### 06.5.2 版本号

`mlir/include/mlir/Bytecode/Encoding.h` 用枚举记录每次格式变化：

```text
32:  kDialectVersioning = 1,
   kLazyLoading = 2,
   kUseListOrdering = 3,
   kElideUnknownBlockArgLocation = 4,
46:  kNativePropertiesEncoding = 5,
   kNativePropertiesODSSegmentSize = 6,
53:  kVersion = 6,
```

维护者要记住两个数字：**当前版本 6**；**v5 起原生编码 properties**。v6 又把 ODS 的 operand/result `segment_size` 从 attribute 改成 native property。

### 06.5.3 写出与读入路径

写出端：`BytecodeWriter.cpp` 的 `writeAttribute`（`:411`）、`writeType`（`:423`）、`writeOp`（`:1042`）。方言可以接管自己的编码：

```cpp
// BuiltinDialectBytecode.cpp:558
Attribute readAttribute(DialectBytecodeReader &reader) const override {
  return ::readAttribute(getContext(), reader);
}
```

读入端按 section 解：string → dialect → attribute/type → resource → IR（`BytecodeFormat.md:144` 起的 `### Sections`）。

### 06.5.4 back-deployment 的拒绝条件

`BytecodeFormat.md:54` 明确：

```text
For versions 0 through 4, the writer rejects back-deployment when reconstructing
properties from the legacy dictionary would produce property storage unequal to
the original.
```

也就是说：**v0–v4 的写出不是“尽力而为”，而是会主动报错。** 维护者看到 `--emit-bytecode-version=0` 失败，不应绕过，而应检查 property 的 attribute 转换是否完整。

## 06.6 兼容性规则：什么能改，什么不能

把规则压缩成表：

| 改动 | 影响 | 需要做什么 |
|---|---|---|
| 加一个 discardable attr | 向后兼容 | 无需版本处理 |
| 给 property 加字段 + 完整 `convertToAttribute` | v5+ 兼容 | 补 v0–v4 的 inherent-attr 钩子 |
| 给 property 加字段但转换 lossy | **破坏 v0–v4** | writer 会拒绝 back-deploy |
| 改 op 名字 / 删字段 | 破坏旧 bytecode | 走 dialect versioning + `upgradeFromVersion` |
| 改 `assemblyFormat` | 只影响文本，不影响 bytecode | 补 parse/print 往返测试 |

方言级别的版本由 `BytecodeDialectInterface` 承担：`parseVersion`、`writeVersion`、`readVersion`、`upgradeFromVersion`（测试 dialect 的实现见 `mlir/test/lib/Dialect/Test/TestDialectInterfaces.cpp:111/145/159/169`）。选择版本的命令行是：

```bash
--emit-bytecode-version=<n>
--emit-bytecode-dialect-version=<dialect>=<version>
```

`BytecodeFormat.md:24` 说明两者独立：前者是整体格式，后者是单个方言的版本。

## 06.7 源码落点

```bash
grep -n 'getInherentAttr\|getDiscardableAttr\|setDiscardableAttr\|getDiscardableAttrs' mlir/include/mlir/IR/Operation.h | head
grep -n 'getInherentAttr(StringRef\|setInherentAttr(StringAttr' mlir/lib/IR/Operation.cpp
grep -n 'kDialectVersioning\|kNativePropertiesEncoding\|kVersion =' mlir/include/mlir/Bytecode/Encoding.h
grep -n 'writeAttribute\|writeType\|LogicalResult BytecodeWriter::writeOp' mlir/lib/Bytecode/Writer/BytecodeWriter.cpp
grep -n 'class Property\|def I32Prop\|def ArrayProp\|def OptionalProp' mlir/include/mlir/IR/Properties.td
```

本机实测：

```text
mlir/include/mlir/IR/Operation.h:470:  std::optional<Attribute> getInherentAttr(StringRef name);
mlir/include/mlir/IR/Operation.h:487:  Attribute getDiscardableAttr(StringRef name) { return attrs.get(name); }
mlir/include/mlir/IR/Operation.h:514:  void setDiscardableAttr(StringAttr name, Attribute value) {
mlir/include/mlir/IR/Operation.h:540:  auto getDiscardableAttrs() {
mlir/lib/IR/Operation.cpp:337:std::optional<Attribute> Operation::getInherentAttr(StringRef name) {
mlir/lib/IR/Operation.cpp:341:void Operation::setInherentAttr(StringAttr name, Attribute value) {
mlir/include/mlir/Bytecode/Encoding.h:32:  kDialectVersioning = 1,
mlir/include/mlir/Bytecode/Encoding.h:46:  kNativePropertiesEncoding = 5,
mlir/include/mlir/Bytecode/Encoding.h:53:  kVersion = 6,
mlir/lib/Bytecode/Writer/BytecodeWriter.cpp:411:  void writeAttribute(Attribute attr) override {
mlir/lib/Bytecode/Writer/BytecodeWriter.cpp:423:  void writeType(Type type) override {
mlir/lib/Bytecode/Writer/BytecodeWriter.cpp:1042:LogicalResult BytecodeWriter::writeOp(EncodingEmitter &emitter, Operation *op) {
mlir/include/mlir/IR/Properties.td:29:class Property<string storageTypeParam = "", string desc = "",
mlir/include/mlir/IR/Properties.td:237:def I32Prop : IntProp<"int32_t">;
mlir/include/mlir/IR/Properties.td:458:class ArrayProp<Property elem = Property<>, string newSummary = ""> :
mlir/include/mlir/IR/Properties.td:603:class OptionalProp<Property p, bit canDelegateParsing = 1>
```

## 06.8 可跑实验

先准备两个输入（都在 `/tmp/mlirc`）：

```bash
mkdir -p /tmp/mlirc && cd /tmp/mlirc
cat > simple.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %s = arith.addi %a, %b : i32
  %m = arith.muli %s, %a : i32
  return %m : i32
}
EOF
cat > attrs.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 attributes {test.flag} {
  %s = arith.addi %a, %b {foo = 42 : i64} : i32
  return %s : i32
}
EOF
```

### 实验 1：inherent 与 discardable 的语法边界（实测）

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt attrs.mlir --mlir-print-op-generic
```

真实输出：

```text
"builtin.module"() ({
  "func.func"() <{function_type = (i32, i32) -> i32, sym_name = "f"}> ({
  ^bb0(%arg0: i32, %arg1: i32):
    %0 = "arith.addi"(%arg0, %arg1) <{overflowFlags = #arith.overflow<none>}> {foo = 42 : i64} : (i32, i32) -> i32
    "func.return"(%0) : () -> ()
  }) {test.flag} : () -> ()
}) : () -> ()
```

三个层次一次看全：`function_type`/`sym_name` 是 inherent；`test.flag` 与 `foo` 是 discardable；`overflowFlags` 是 inherent property 且**默认值也会打印**（generic 形式不做省略）。

### 实验 2：bytecode magic（实测）

```bash
$T/bin/mlir-opt simple.mlir --emit-bytecode -o simple.mlirbc
xxd simple.mlirbc | head -2
```

真实输出：

```text
00000000: 4d4c ef52 0d4d 4c49 5232 342e 302e 3067  ML.R.MLIR24.0.0g
00000010: 6974 0001 2d07 0307 0303 0707 0303 090b  it..-...........
```

`4d 4c ef 52` 是 magic；`0d` 是 producer 长度；`MLIR24.0.0git` 是 producer。维护者排查“文件被当成文本解析”时，先看前四字节。

### 实验 3：版本 0 与版本 6 的大小与往返（实测）

```bash
$T/bin/mlir-opt simple.mlir --emit-bytecode --emit-bytecode-version=0 -o v0.mlirbc
$T/bin/mlir-opt simple.mlir --emit-bytecode --emit-bytecode-version=6 -o v6.mlirbc
ls -l v0.mlirbc v6.mlirbc
$T/bin/mlir-opt v0.mlirbc
```

真实输出：

```text
-rw-rw-r-- 1 ubuntu ubuntu 307 Nov  8 18:54 v0.mlirbc
-rw-rw-r-- 1 ubuntu ubuntu 276 Nov  8 18:54 v6.mlirbc
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %1 = arith.muli %0, %arg0 : i32
    return %1 : i32
  }
}
```

v0 更大（307 vs 276）：一个合理推断是旧格式把 property 摊进 legacy dictionary、键与字符串重复，因而更占空间（这是推断，未逐字节对照 section）。语义往返仍然成立，因为 `arith.addi` 的 `overflowFlags` 是默认值。

### 实验 4：非默认 property 也能往返（实测）

```bash
cat > ov.mlir <<'EOF'
func.func @f(%a: i32, %b: i32) -> i32 {
  %s = arith.addi %a, %b overflow<nuw> : i32
  return %s : i32
}
EOF
$T/bin/mlir-opt ov.mlir --emit-bytecode --emit-bytecode-version=0 -o ov0.mlirbc
$T/bin/mlir-opt ov0.mlirbc
```

真实输出：

```text
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 overflow<nuw> : i32
    return %0 : i32
  }
}
```

`overflow<nuw>` 在 v0 里作为 inherent attribute 保留下来——这正说明“能往返”取决于 dialect 是否提供了 lossless 的 inherent-attr 钩子。换成没有钩子的自定义 property，writer 会拒绝（见 06.5.4）。

### 实验 5：方言级版本选择（实测）

```bash
$T/bin/mlir-opt simple.mlir --emit-bytecode --emit-bytecode-dialect-version=builtin=0 -o b0.mlirbc
ls -l b0.mlirbc
$T/bin/mlir-opt simple.mlir --emit-bytecode --emit-bytecode-dialect-version=test=1 -o t1.mlirbc
```

真实输出（第二条故意失败）：

```text
-rw-rw-r-- 1 ubuntu ubuntu 272 Nov  8 18:54 b0.mlirbc
simple.mlir:0:0: error: dialect 'test' does not support emitting bytecode version '1'
```

`builtin=0` 成功且文件更小（272 字节）；`test=1` 被 dialect 的 `verifyVersion` 拒绝。这条错误来自 writer 的预检，不是 reader——**维护方言版本时，`verifyVersion` 是你拒绝“无法无损表示”的最后一道闸。**

### 实验 6：内置 roundtrip pass（实测）

```bash
$T/bin/mlir-opt simple.mlir -test-bytecode-roundtrip
```

真实输出（打印两次，第一次是原文，第二次是 roundtrip 后）：

```text
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %1 = arith.muli %0, %arg0 : i32
    return %1 : i32
  }
}
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    ...
  }
}
```

这个 pass 还能测测试方言的自定义编码：

```bash
$T/bin/mlir-opt simple.mlir --test-bytecode-roundtrip="test-dialect-version=2.0"
```

真实输出：正常两段 IR。换成 `test-dialect-version=1.2` 则出现：

```text
loc("-":0:0): error: dialect 'funky' does not implement the bytecode interface
simple.mlir:0:0: error: failed to read bytecode
```

这是测试方言故意构造的边界：某些版本会启用自定义 type 编码，而 `funky` 方言没有实现 bytecode 接口。

### 实验 7：lazy loading 与版本（实测）

```bash
$T/bin/mlir-opt simple.mlir --emit-bytecode -o s.mlirbc
$T/bin/mlir-opt s.mlirbc -test-lazy-loading
```

真实输出（节选）：

```text
Has 1 ops to materialize

Before Materializing...

"builtin.module"() ({
}) : () -> ()

Materializing...

"builtin.module"() ({
  "func.func"() <{function_type = (i32, i32) -> i32, sym_name = "f"}> ({
```

lazy loading 是 bytecode v2 引入的能力：reader 可以先只读顶层结构，再按需 materialize 子 region。这对大模块的启动时间很关键，也是“为什么 bytecode 读入不是一个 `parseSourceFile` 能替代”的原因。

## 06.9 踩雷集锦

1. **错误直觉：`attr` 和 `prop` 只是写法不同。** → 正确认识：`prop` 进 properties struct（inherent），`attr` 走 attribute 字典；generic print 的 `<{...}>` 与 `{...}` 就是分界线。
2. **错误直觉：property 也是 attribute，可以随便塞进字典。** → 正确认识：property 是 C++ struct 字段；只有通过 `convertToAttribute` 才有一个 attribute 视图，且可能 lossy。
3. **错误直觉：`getInherentAttr` 是新代码该用的 API。** → 正确认识：它被标注为迁移期的 transient facility；新代码直接读 property getter。
4. **错误直觉：给 property 加字段不影响 bytecode。** → 正确认识：v0–v4 依赖 inherent-attr 转换；转换不完整时 writer 拒绝 back-deploy。
5. **错误直觉：discardable 的键名可以随便改。** → 正确认识：discardable 属性会参与 legacy dictionary，重名/改名会让旧 reader 读到错误语义；命名要带 dialect 前缀。
6. **错误直觉：v5 之前和之后可以共用一套写法。** → 正确认识：v5 起原生编码 properties，v6 又改了 segment_size 的表示；跨版本测试要用 `--emit-bytecode-version`。
7. **错误直觉：roundtrip 通过就代表 loc 也保住了。** → 正确认识：不同版本对 location 的压缩策略不同（v4 起省略 block arg 的 unknown loc）；要单独比对 loc。
8. **错误直觉：`--emit-bytecode-dialect-version` 只是写个标记。** → 正确认识：writer 会先调 `verifyVersion`；不支持的版本直接失败，不是警告。

## 06.10 本章重点整理

- 一个 op 的数据分两层：inherent properties（C++ struct）与 discardable attributes（字典）。
- generic print 的 `<{...}>` / `{...}` 是判断字段归属的最快手段。
- `getInherentAttr` 是迁移兼容层，不是新代码主路径。
- ODS 用 `prop` 声明 property，`Properties.td` 提供 `I32Prop`/`StringProp`/`ArrayProp`/`OptionalProp` 等积木。
- bytecode magic 是 `4D 4C EF 52`，当前格式版本 6，v5 起原生编码 properties。
- back-deployment 到 v0–v4 会在 lossy 时被 writer 主动拒绝。
- 方言版本走 `BytecodeDialectInterface`，`verifyVersion` 是写出前的闸门。
- lazy loading（v2+）改变的是读入时机，不改变语义。

## 06.11 自我检核

- [ ] 能用 generic print 指出任意一个 op 的 inherent 与 discardable 部分
- [ ] 能说出 `getInherentAttr` 为什么被标成 transient
- [ ] 能解释 properties struct 与 attribute 字典在内存上的区别
- [ ] 能背出 bytecode magic 的四个字节，并解释文件头后面的 producer 长度
- [ ] 能说出 `kNativePropertiesEncoding` 与 `kVersion` 各是多少、差在哪
- [ ] 能解释什么情况下 `--emit-bytecode-version=0` 会失败
- [ ] 能区分整体 bytecode 版本与单个 dialect 版本
- [ ] 能用 `-test-bytecode-roundtrip` 和 `-test-lazy-loading` 做回归

## 06.12 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/BytecodeFormat.md`**
  - 读哪里：`Operation Properties and Back-Deployment`（38）、`Magic Number`（62）、`Sections`（144）、`Operation Encoding`（387）
  - 能学到什么：格式的权威定义与每个版本的兼容承诺
  - 和本章关联：06.5、06.6 的全部规则
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/Properties.td`**
  - 读哪里：`Property`（29）、`IntProp`（216）、`ArrayProp`（458）、`OptionalProp`（603）
  - 能学到什么：property 的 storage/interface 双类型与 attribute 转换钩子
  - 和本章关联：06.4
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Bytecode/Encoding.h`**
  - 读哪里：`BytecodeVersion` 枚举（26–55）
  - 能学到什么：每个版本号对应的能力边界
  - 和本章关联：06.5.2
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Bytecode/Writer/BytecodeWriter.cpp`**
  - 读哪里：`writeAttribute`（411）、`writeType`（423）、`writeOp`（1042）
  - 能学到什么：写出端如何决定 section 与 dialect 编码
  - 和本章关联：06.5.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DefiningDialects/Operations.md`**
  - 读哪里：`prop-dialog`、`Optional and default-valued properties`（344）、`Inherent properties and discardable attributes`（490）
  - 能学到什么：ODS 层如何声明 property，以及默认值何时被省略
  - 和本章关联：06.4

数据怎么存、怎么写进文件都清楚了。下一章看“谁能拒绝坏 IR”：verifier 的调用顺序、diagnostic 的匹配规则，以及 `--verify-diagnostics` 为什么经常比报错本身更难写。

→ 下一章：[Ch 07 Verifier 与 diagnostics](./07-verifier-and-diagnostics.md)
