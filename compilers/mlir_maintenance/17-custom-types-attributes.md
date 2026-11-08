# Ch 17 — 自定义 Type 与 Attribute：storage、uniquing 与解析

> **目标**：理解 MLIR 的 Type/Attribute 为什么必须由 context 拥有、如何用参数做 uniquing、`StorageUserBase` 提供了什么、以及自定义类型/属性的 parser 为什么会在“方言没加载”时失败。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（只读），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM/MLIR `24.0.0git`，assertions ON。

> **前置**：Ch 05 的 builtin Type/Attribute、Ch 14 的 ODS 生成物；知道 `MLIRContext` 是 IR 对象的分配根。

## 17.1 为什么维护者要关心 storage

Type 和 Attribute 是 MLIR 里“到处传”的值：每个 `Value` 有类型，每条 attribute 挂在 op 上，pattern 匹配、bytecode、pass 分析都在复制它们。如果每个 `IntegerType::get(ctx, 32)` 都分配一个新对象，内存会爆炸，指针比较也会失效。MLIR 的解法是 **uniquing（唯一化）**：

> 相同 context、相同参数 → 永远返回同一个 storage 实例。

这条不变量带来四个后果，每个都是维护者的日常：

1. **指针相等就是语义相等**。`typeA == typeB` 可以直接比 storage 指针；不需要递归比较参数。
2. **对象不可变**。uniquing 的前提是“参数不会变”；所以 Type/Attribute 没有 setter，所有“修改”都是创建新实例。
3. **生命周期由 context 决定**。storage 活在 `MLIRContext` 的 uniquer 里；`Type` 只是一个指针大小的 handle，context 销毁后全部失效。
4. **参数必须参与 hash/equality**。你漏掉一个参数，两个不同参数的类型会被错误地合并；这是最隐蔽的一类 bug。

维护自定义类型/属性时，90% 的问题都落在这四条上。

## 17.2 先建立直觉：context 拥有的三套 uniquer

```text
  MLIRContext
  ├── typeUniquer      (StorageUniquer)
  │     ├── IntegerTypeStorage{32, signless}
  │     ├── !test.smpla 的 storage
  │     └── !test.int<unsigned, 8> 的 storage
  ├── attributeUniquer (StorageUniquer)
  │     ├── StringAttrStorage{"x"}
  │     ├── #test.i64_elements<[...]> 的 storage
  │     └── distinct[0]<42 : i32>
  └── affineUniquer    (StorageUniquer)

  Type handle ──getTypeID()──▶ 具体 StorageUserBase
       │                              │
       └──────── 参数 hash ───────────┘
                     │
                     ▼
              uniquer 查表：命中则复用，未命中则分配
```

三个要点：

- **uniquer 是 context 的成员**，不是全局单例；两个 `MLIRContext` 的 `i32` 不是同一个对象。
- **Type 和 Attribute 走不同的 uniquer**（`MLIRContext.cpp:223` 与 `:249`），但机制相同：`StorageUniquer` + `StorageUserBase`。
- **storage 的 key 是参数元组**。参数可以是整数、字符串、`Type`、`Attribute`、`ArrayRef`；但必须可 hash、可比较。

`StorageUniquer` 本身在 `mlir/include/mlir/Support/StorageUniquer.h:82`；Type/Attribute 的 CRTP 包装在 `StorageUniquerSupport.h:95`。

## 17.3 Type 与 Attribute 的异同

| 维度 | Type | Attribute |
|---|---|---|
| 基类 | `Type` | `Attribute` |
| uniquer | `MLIRContext::getTypeUniquer()` | `MLIRContext::getAttributeUniquer()` |
| ODS 定义 | `TypeDef<...>` | `AttrDef<...>` |
| 生成文件 | `XOpsTypes.h.inc/.cpp.inc` | `XOpsAttributes.h.inc/.cpp.inc` |
| 文本前缀 | `!dialect.name<...>` | `#dialect.name<...>` |
| 典型用途 | 值类型、布局、形状 | 常量、编译选项、元数据 |
| 都能做 | 参数化、自定义 parser/printer、接口 | 同左 |

两者共享 `StorageUserBase` 的绝大部分逻辑：

```cpp
template <typename ConcreteT, typename BaseT, typename StorageT,
          typename UniquerT, template <typename T> class... Traits>
class StorageUserBase : public BaseT, public Traits<ConcreteT>... {
public:
  static TypeID getTypeID() { return TypeID::get<ConcreteT>(); }

  template <typename... Args>
  static ConcreteT get(MLIRContext *ctx, Args &&...args) {
    assert(succeeded(ConcreteT::verifyInvariants(
        getDefaultDiagnosticEmitFn(ctx), args...)));
    return UniquerT::template get<ConcreteT>(ctx, std::forward<Args>(args)...);
  }
  ...
};
```

（`StorageUniquerSupport.h:95`；`get` 在 177 行；`getChecked` 在 189/205 行。）

维护含义：**Type 和 Attribute 的“构造”不是 `new`，而是 `get(ctx, args...)`**。`get` 会先跑 `verifyInvariants(args...)`，再进 uniquer。如果你在参数里放了非法值，assertion 构建会直接 abort；release 构建则依赖 `getChecked`。

## 17.4 参数就是 storage key

一个参数化类型的最小 ODS 形态（`TestTypeDefs.td:88` 的 `IntegerType` 是真实例子）：

```tablegen
def IntegerType : Test_Type<"TestInteger"> {
  let mnemonic = "int";
  let genVerifyDecl = 1;
  let parameters = (
    ins
    "unsigned":$width,
    "::test::TestIntegerType::SignednessSemantics":$signedness
  );
  let hasCustomAssemblyFormat = 1;
}
```

生成物会做三件事：

1. **Storage 结构体**：把 `width`、`signedness` 存起来。
2. **hash/equality**：对所有参数做 `llvm::hash_combine` 和逐字段比较。
3. **`get(ctx, width, isSigned)`**：以参数元组为 key 查 uniquer。

维护者必须保证：**所有影响语义的字段都在 `parameters` 里**。如果你加了一个 C++ 成员但没放进 parameters，两个“看起来不同”的实例会被 uniquer 合并成一个。

### 17.4.1 `ArrayRefParameter` 的坑

`ArrayRef` 不能按值存；ODS 用 `ArrayRefParameter` 在构造时把元素复制进 storage 的尾部。生成物里会出现“参数存储 + 尾部数组”的布局。维护时要注意：

- 传给 `get` 的 `ArrayRef` 只在调用期间有效；uniquer 会复制。
- 参数的 hash 必须覆盖数组内容，而不是指针。
- 空数组和非空数组是不同 key。

### 17.4.2 默认值不改变 key 的语义

`DefaultValuedParameter` 让“省略参数”和“显式传默认值”生成同一个 storage。这是正确的：语义相同就该唯一化。但如果你希望“省略”和“显式默认”在文本上可区分，就不能靠 storage，得靠 parser/printer 或额外的 unit 参数。

### 17.4.3 参数类型速查

| C++ 参数类型 | 推荐 ODS 写法 | 存储方式 | 维护注意 |
|---|---|---|---|
| `unsigned`/`int64_t` | `"unsigned":$w` | 按值 | 最简单，参与 hash |
| `bool`/enum | `"bool":$b` 或自定义 enum | 按值 | enum 需要可 hash |
| `StringRef` | `StringRefParameter<>` | 复制进 storage allocator | 不要直接存裸 `StringRef` |
| `Type`/`Attribute` | `"::mlir::Type":$t` | handle | 自动参与 hash |
| `ArrayRef<T>` | `ArrayRefParameter<"T">` | 尾部数组 | 必须复制内容，不能只存指针 |
| 自定义结构体 | 自定义 `AttrParameter`/`TypeParameter` | 由 allocator 决定 | 需要提供 comparator/allocator |

这张表的第三列是维护重点：**“按值”不等于“安全”**。`ArrayRef` 按值复制的是 `{ptr, size}`，原数组失效后 storage 里就是悬空指针；`ArrayRefParameter` 的 allocator 会把元素复制进 context 的 allocator，才是正确做法。

## 17.5 解析与打印：mnemonic + 格式

自定义 Type/Attribute 的文本形式由三部分组成：

```text
!test.int<unsigned, 8>
 │    │   └── 参数
 │    └────── mnemonic
 └─────────── dialect namespace
```

三种解析方式：

| 方式 | 写法 | 生成物 |
|---|---|---|
| 无参数 | 只写 `mnemonic` | 直接 `get(ctx)` |
| 声明式格式 | `let assemblyFormat = "..."` | 生成的 parser/printer |
| 自定义格式 | `let hasCustomAssemblyFormat = 1` | 你手写 `parse`/`print` |

声明式格式的指令与 op 类似：`$param`、`struct(...)`、`custom<...>`、`qualified`。完整列表在 `AttributesAndTypes.md:786` 起。

一个真实例子（`TestTypeDefs.td:186`）：

```tablegen
def TestTypeWithFormat : Test_Type<"TestTypeWithFormat"> {
  let parameters = (
    ins TestParamOne:$one,
    TestParamTwo:$two,
    "::mlir::Attribute":$three
  );
  let mnemonic = "type_with_format";
  let assemblyFormat = "`<` $one `,` struct($three, $two) `>`";
}
```

维护含义：**格式是 dialect 的文本 ABI**。改 `assemblyFormat` 会让所有手写该类型的 `.mlir` 失效；改 `mnemonic` 更糟，因为它是文本里的名字。两者都需要 release note 和测试更新。

### 17.5.1 alias 从哪来

实验 2 里的 `!test_ui8` 不是 storage 的名字，而是 printer 的 alias。来源是 dialect 注册的 `OpAsmDialectInterface`：

```cpp
AliasResult getAlias(Type type, raw_ostream &os) const final {
  ...
  if (auto intType = dyn_cast<TestIntegerType>(type)) {
    if (intType.getWidth() == 8 &&
        intType.getSignedness() ==
            TestIntegerType::SignednessSemantics::Unsigned) {
      os << "test_ui8";
      return AliasResult::FinalAlias;
    }
  }
  ...
}
```

（`TestDialectInterfaces.cpp:282`，条件判断在 292–296。）

维护含义：

- alias 只影响打印，不影响解析后的 storage；`!test_ui8` 与 `!test.int<unsigned, 8>` 是同一个对象。
- alias 冲突（两个类型都建议同一个名字）时，printer 会加后缀区分；不要依赖 alias 做跨版本稳定标识。
- 如果 downstream 测试用 `CHECK: !test_ui8`，改 alias 规则会让测试失败，但 IR 语义没变。

## 17.6 distinct attribute：故意不唯一化

有时你**需要**两个参数完全相同、但身份不同的 attribute。典型场景是“每个 op 一个唯一 ID”“去重会改变语义的元数据”。`DistinctAttr` 就是为此设计的：

```cpp
class DistinctAttr
    : public detail::StorageUserBase<DistinctAttr, Attribute,
                                     detail::DistinctAttrStorage,
                                     detail::DistinctAttributeUniquer> {
public:
  static DistinctAttr create(Attribute referencedAttr);
  Attribute getReferencedAttr() const;
};
```

（`BuiltinAttributes.h:1031`，`create` 实现在 `BuiltinAttributes.cpp:1655`。）

它用了一个**独立的 uniquer**（`DistinctAttributeUniquer`），每次 `create` 都分配新实例，并给它一个递增 ID。打印出来是：

```text
#distinct = distinct[0]<42 : i32>
#distinct1 = distinct[1]<42 : i32>
```

`-test-distinct-attrs` 的实测输出（节选）：

```text
#distinct = distinct[0]<42 : i32>
#distinct1 = distinct[1]<42 : i32>
#distinct2 = distinct[2]<42 : i32>
#distinct3 = distinct[3]<42 : i32>
#distinct4 = distinct[4]<43 : i32>
#distinct5 = distinct[5]<43 : i32>
#distinct6 = distinct[6]<@foo_1>
#distinct7 = distinct[7]<@foo_1>
```

注意：`42 : i32` 出现了两次（`[0]` 和 `[1]`），`43 : i32` 也出现了两次，`@foo_1` 也是。它们**内容相同、身份不同**，这正是 distinct 的目的。

维护提示：不要把普通 attribute 换成 distinct 来“修复”去重问题。distinct 会破坏“相同内容可比较”的假设，也会让 bytecode 变大；只有确实需要身份语义时才用。

### 17.6.1 `-test-distinct-attrs` 在测什么

这个 pass（`mlir/test/lib/IR/TestBuiltinDistinctAttributes.cpp`）做一件很小的事：

```cpp
auto distinctAttr =
    op->getDiscardableAttrOfType<DistinctAttr>("distinct.input");
if (!distinctAttr)
  return;
op->setDiscardableAttr(
    "distinct.output",
    DistinctAttr::create(distinctAttr.getReferencedAttr()));
```

（`TestBuiltinDistinctAttributes.cpp:33-39`。）

它遍历每个 op，读入一个 `distinct.input`，再 `create` 一个新的 `distinct.output`。测试断言的是：**同一个输入 attr 在多个 op 中复用（input 相同），但每个 op 的 output 是不同实例（ID 递增）**。这正是 distinct 的语义：引用内容可以共享，身份必须独立。

## 17.7 生命周期：Type 比你想的长

```cpp
Type t = IntegerType::get(ctx, 32);   // t 只是 handle
// ... ctx 还活着 ...
Type u = IntegerType::get(ctx, 32);   // u == t，指向同一 storage
```

三个必须记住的事实：

1. **Type/Attribute 不能跨 context 使用**。把 `ctx1` 的 `i32` 传给 `ctx2` 的 op，verifier 会报 “type from a different MLIRContext”（`Verifier.cpp` 的 operand context 检查）。
2. **storage 不会因为 handle 离开作用域而释放**。它活在 context 的 uniquer 里，直到 context 销毁；这意味着长时间运行的进程里，动态创建大量不同参数的类型会持续增长。
3. **不要在 storage 里存指向 IR 的裸指针**。storage 的生命周期与具体 op 无关；存 `Operation *` 很容易悬空。需要引用 IR 时，存 `Attribute`/`Type` 或符号名。

`getChecked` 是 `get` 的可诊断版本（`StorageUniquerSupport.h:208`）：参数非法时不 abort，而是发 diagnostic 并返回 null。维护自定义类型时，**parser 里应该用 `getChecked`**，这样非法输入会变成漂亮的错误信息，而不是 assertion。

### 17.7.1 mutable storage：唯一的例外

Type/Attribute 默认不可变，但 MLIR 允许“部分参数可变”，用于递归类型这类“构造时还填不满”的场景。规则很硬：

- **可变字段不能进 `KeyTy`**，否则 uniquing 的哈希会失效。
- storage 提供 `LogicalResult mutate(StorageAllocator &, Args...)`。
- 目前 mutable parameters **不支持 ODS 声明式定义**，必须手写 C++ storage（`AttributesAndTypes.md:1084` 明确写了“目前不支持声明式定义”）。

维护含义：如果你发现自己在“构造后再改类型参数”，先问是不是应该创建新类型。mutable storage 是给递归/自引用结构留的窄门，不是常规优化手段。

## 17.8 源码落点

| 内容 | 路径 | 关键行/符号（本树 `grep -n` 核对） |
|---|---|---|
| `StorageUserBase` | `mlir/include/mlir/IR/StorageUniquerSupport.h` | 95；`get` 177；`getChecked` 189/205 |
| `StorageUniquer` | `mlir/include/mlir/Support/StorageUniquer.h` | 82 `class StorageUniquer` |
| context 的 type uniquer | `mlir/lib/IR/MLIRContext.cpp` | 223 `typeUniquer`；1169 `getTypeUniquer` |
| context 的 attribute uniquer | 同上 | 249 `attributeUniquer`；1256 `getAttributeUniquer` |
| `AttributeUniquer` | `mlir/include/mlir/IR/AttributeSupport.h` | 215 |
| 测试类型定义 | `mlir/test/lib/Dialect/Test/TestTypeDefs.td` | 26 `Test_Type`；88 `IntegerType`；146 `TestType`；186 `TestTypeWithFormat` |
| 测试属性定义 | `mlir/test/lib/Dialect/Test/TestAttrDefs.td` | 100 `TestI64ElementsAttr`；197 `TestAttrWithFormat` |
| distinct attribute | `mlir/include/mlir/IR/BuiltinAttributes.h` | 1031 `DistinctAttr` |
| distinct 创建 | `mlir/lib/IR/BuiltinAttributes.cpp` | 1655 `DistinctAttr::create` |
| 定义指南：参数 | `mlir/docs/DefiningDialects/AttributesAndTypes.md` | 220 Parameters；239 参数类型 |
| 定义指南：解析打印 | 同上 | 518 Parsing and Printing；573 `assemblyFormat`；786 指令 |
| 定义指南：storage | 同上 | 989 Storage Classes；1011 定义 storage |
| 定义指南：可变 storage | 同上 | 1084 Mutable attributes and types |

## 17.9 可跑实验

> 全部真跑；`$T`/`$S` 见 14.4。

### 实验 1：无参数自定义类型的 roundtrip（实测）

```bash
printf 'func.func @g(%%x: !test.smpla) {\n  return\n}\n' > /tmp/type1.mlir
$T/bin/mlir-opt /tmp/type1.mlir
```

实测：

```text
module {
  func.func @g(%arg0: !test.smpla) {
    return
  }
}
```

无参数类型只需要 `mnemonic`；parser 直接 `get(ctx)`，printer 直接打 mnemonic。

### 实验 2：参数化类型与 alias（实测）

```bash
printf 'func.func @f(%%x: !test.int<unsigned, 8>, %%y: !test.smpla) {\n  return\n}\n' > /tmp/type2.mlir
$T/bin/mlir-opt /tmp/type2.mlir
```

实测：

```text
!test_ui8 = !test.int<unsigned, 8>
module {
  func.func @f(%arg0: !test_ui8, %arg1: !test.smpla) {
    return
  }
}
```

两件事：

1. `!test.int<unsigned, 8>` 被打印成 alias `!test_ui8`。alias 是 **printer 层** 的优化，不影响 storage；`!test_ui8` 和完整形式解析后是同一个 storage。
2. `!test.smpla` 没有 alias，因为 test dialect 的 `OpAsmDialectInterface` 只对特定类型建议 alias。

### 实验 3：非法参数被 `getChecked` 拒绝（实测）

```bash
printf 'func.func @f(%%x: !test.int<weird, 8>) {\n  return\n}\n' > /tmp/type_bad.mlir
$T/bin/mlir-opt /tmp/type_bad.mlir
```

实测：

```text
/tmp/type_bad.mlir:1:28: error: expected signed, unsigned, or none
func.func @f(%x: !test.int<weird, 8>) {
                           ^
```

这是 parser 层的诊断，不是 assertion。自定义类型的 parser 应该尽量给出这种“人话”错误，而不是让 `get` abort。

### 实验 4：自定义 Attribute 的 roundtrip（实测）

```bash
printf 'func.func @f() {\n  %%0 = arith.constant #test.i64_elements<[1, 2, 3]> : tensor<3xi64>\n  return\n}\n' > /tmp/attr1.mlir
$T/bin/mlir-opt /tmp/attr1.mlir
```

实测：

```text
module {
  func.func @f() {
    %cst = arith.constant #test.i64_elements<[1, 2, 3]> : tensor<3xi64>
    return
  }
}
```

`#test.i64_elements<...>` 的存储参数是 `ArrayRef<uint64_t>`（外加一个 `AttributeSelfTypeParameter`）；ODS 在构造时复制数组，所以 `[1, 2, 3]` 和另一个 `[1, 2, 3]` 会命中同一个 storage。

### 实验 5：distinct attribute 的身份（实测）

```bash
$T/bin/mlir-opt -test-distinct-attrs \
  $S/mlir/test/IR/test-builtin-distinct-attrs.mlir 2>&1 | head -8
```

实测：

```text
#distinct = distinct[0]<42 : i32>
#distinct1 = distinct[1]<42 : i32>
#distinct2 = distinct[2]<42 : i32>
#distinct3 = distinct[3]<42 : i32>
#distinct4 = distinct[4]<43 : i32>
#distinct5 = distinct[5]<43 : i32>
#distinct6 = distinct[6]<@foo_1>
#distinct7 = distinct[7]<@foo_1>
```

同一个 pass 每次运行都从 `[0]` 开始编号，说明 ID 是 **per-context / per-pass** 的，不是全局持久 ID。不要把 `distinct[0]` 写进跨运行断言。

### 实验 6：ElementsAttr 接口的行为（实测）

```bash
$T/bin/mlir-opt -test-elements-attr-interface \
  $S/mlir/test/IR/elements-attr-interface.mlir 2>&1 | head -6
```

实测：

```text
/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/IR/elements-attr-interface.mlir:16:1: error: Test iterating `int64_t`: unable to iterate type
arith.constant #test.i64_elements<[10, 11, 12, 13, 14]> : tensor<5xi64>
^
/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/IR/elements-attr-interface.mlir:16:1: note: see current operation: %0 = "arith.constant"() <{value = #test.i64_elements<[10, 11, 12, 13, 14]> : tensor<5xi64>}> : () -> tensor<5xi64>
```

这个 pass 故意把 `ElementsAttr` 的迭代结果打成 error，用 `-verify-diagnostics` 匹配。它展示了一个重要区别：**attribute 的“存储表示”与“接口视图”可以不同**。`#test.i64_elements` 存的是 `int64_t` 数组，但通过 `ElementsAttr` 接口可以按 `uint64_t`/`APInt`/`IntegerAttr` 迭代；不是所有视图都可用（`int64_t` 对无符号存储就失败）。

## 17.10 踩雷集锦

1. **以为 `Type::get` 每次分配新对象。** → 相同 context + 相同参数返回同一 storage；指针相等可用。
2. **漏把字段放进 `parameters`。** → 不同语义的实例被 uniquer 合并；症状是“改了参数但行为没变”。
3. **在 storage 里存 `Operation *` 或 `Value`。** → storage 生命周期长于具体 IR；悬空指针会在很久以后才炸。
4. **用 `get` 而不是 `getChecked` 解析用户输入。** → 非法参数触发 assertion abort，而不是 diagnostic。
5. **把 distinct attribute 当普通 attribute 用。** → 内容相同也不相等；去重、缓存、bytecode 都会受影响。
6. **跨 context 传递 Type/Attribute。** → verifier 报 “from a different MLIRContext”；不要试图“转换”，要重新 `get`。
7. **改 `mnemonic` 却不改测试。** → 所有手写 `!dialect.old` / `#dialect.old` 的 `.mlir` 立刻 parse 失败。
8. **把 alias 当存储形式。** → `!test_ui8` 只是打印别名；解析后仍是 `!test.int<unsigned, 8>`，不要用 alias 做语义判断。

## 17.11 本章重点整理

- Type/Attribute 由 `MLIRContext` 的 uniquer 拥有；相同 context + 相同参数 → 同一 storage。
- `StorageUserBase` 提供 `get`/`getChecked`/`getTypeID`；`get` 会先跑参数验证，再查 uniquer。
- 参数元组就是 storage key；所有语义字段必须参与 hash/equality。
- `ArrayRefParameter` 在构造时复制元素；hash 必须覆盖内容。
- 文本形式 = dialect namespace + mnemonic + 格式；`assemblyFormat` 是文本 ABI。
- `DistinctAttr` 故意绕过 uniquing，用独立 uniquer 给每个实例身份；只在需要身份语义时用。
- storage 生命周期由 context 决定；handle 离开作用域不释放，跨 context 使用非法。

## 17.12 自我检核

- [ ] 能解释为什么 `IntegerType::get(ctx, 32) == IntegerType::get(ctx, 32)` 是同一个对象
- [ ] 能说出 `StorageUserBase::get` 在查 uniquer 之前做了什么
- [ ] 能列出至少三个必须放进 `parameters` 的字段类型
- [ ] 能解释 `ArrayRefParameter` 为什么需要特殊存储布局
- [ ] 能判断什么时候该用 `DistinctAttr`，什么时候不该
- [ ] 能说出 `get` 与 `getChecked` 在错误处理上的区别
- [ ] 能解释 alias 与 storage 的关系
- [ ] 能定位 `MLIRContext` 里 type/attribute uniquer 的成员

## 17.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/StorageUniquerSupport.h`**（272 行）
  - 读哪里：95–150 的 `StorageUserBase`；200–240 的 `get`/`getChecked`。
  - 能学到什么：CRTP 如何把 `ConcreteT` 接到 uniquer，以及参数验证的时机。
  - 和本章关联：17.3/17.7 的所有结论都能在这里核对。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Support/StorageUniquer.h`**（346 行）
  - 读哪里：82–200 的 `StorageUniquer` 接口；`get`/`getWithTypeID`。
  - 能学到什么：uniquing 的哈希表实现、线程安全锁。
  - 和本章关联：解释“为什么相同参数返回同一 storage”。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DefiningDialects/AttributesAndTypes.md`**（1,244 行）
  - 读哪里：220–310 的 Parameters；518–610 的 Parsing and Printing；989–1090 的 Storage Classes。
  - 能学到什么：官方推荐的参数/storage/格式写法。
  - 和本章关联：17.4/17.5 的规则来源。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/lib/Dialect/Test/TestTypeDefs.td`**（522 行）
  - 读哪里：26–100 的简单/参数化类型；146–200 的 `TestType` 与 `TestTypeWithFormat`。
  - 能学到什么：每种参数形态的最小定义。
  - 和本章关联：17.9 的实验输入来自这里。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/BuiltinAttributes.h`**
  - 读哪里：1012–1050 的 `DistinctAttributeUniquer` 与 `DistinctAttr`。
  - 能学到什么：如何用自定义 uniquer 实现“不唯一化”的 attribute。
  - 和本章关联：17.6 的机制说明。

下一章把 Type/Attribute 放回 dialect：注册、懒加载、扩展，以及 bytecode 版本兼容。

→ 下一章：[Ch 18 方言注册、扩展与版本](./18-dialect-registration-and-versioning.md)
