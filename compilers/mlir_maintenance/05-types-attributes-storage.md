# Ch 05 — Type、Attribute 与 storage

> **目标**：理解 `Type`/`Attribute` 的 uniquing（去重）机制：为什么同一个 context 里 `i32` 只有一个对象、`StorageUserBase` 如何把“句柄”和“storage”拆开、`TypeID` 在 `classof` 里起什么作用、context 销毁顺序为什么会让自定义 type 崩。读完你能判断“我该不该给这个参数加进 `KeyTy`”。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`，HEAD `e44fff5decc7`，`T=/home/ubuntu/llvm/builds/ct-instr-trunk`（24.0.0git，assertions ON，shared libs）。本章有两个实验用本树 shared lib 链接独立小程序，编译命令在 05.9 给出。

> **前置**：Ch 04（Value 句柄与所有权）；知道 TableGen 的 `def`/`class`，读过 ODS 的 `TypeDef`/`AttrDef` 更好。

## 05.1 为什么维护者要懂 storage

先看三个维护现场：

1. **内存。** 一份 10 万行的 IR 里，`i32` 可能出现几十万次。如果每个 `i32` 都分配一个对象，内存直接翻倍；uniquing 之后，整个 context 里只有一个 `IntegerTypeStorage`，所有 `i32` 句柄都指向它。
2. **指针相等就是语义相等。** MLIR 大量代码用 `a == b` 判断 type/attr 是否相同。这个 `operator==` 在 `StorageUserBase` 里被实现成**指针比较**——只有当同构对象被 unique 成同一份 storage 时才正确。
3. **自定义 type 崩在奇怪的地方。** 你给自定义 type 加了一个 `StringRef` 成员，但没有把字符串 copy 进 allocator；parse 完立刻 print 没问题，等 source buffer 释放后，`print` 读出乱码。根因是 storage 的生命周期比输入 buffer 长。

所以维护 Type/Attribute 的核心问题不是“怎么写 getter”，而是：**这个对象的 identity 由哪些字段决定？这些字段的内存归谁管？**

## 05.2 先建立直觉：两条 uniquing 流水线

```text
MLIRContext
  ├── typeUniquer : StorageUniquer        (MLIRContext.cpp:223)
  ├── attributeUniquer : StorageUniquer   (MLIRContext.cpp:249)
  └── affineUniquer : StorageUniquer      (MLIRContext.cpp:216)

StorageUniquer
  └── DenseSet<HashedStorage>             // 以 hash(KeyTy) 分桶
        ├── IntegerTypeStorage { width=32, signedness=signless }
        ├── IntegerTypeStorage { width=64, signedness=signless }
        └── StringAttrStorage  { "hello", type=null }

Type / Attribute（8 字节句柄）
  impl ─────────────▶ 上面某一个 storage 实例
  （get() 先查 DenseSet；命中就返回同一个 impl 指针）
```

关键点：**句柄本身不存数据**。`Type` 和 `Attribute` 内部只有一个 `impl` 指针（`StorageUserBase` 继承链的 `BaseT::ImplType *`）。所有字段都在 storage 里。

于是“两个 `i32` 相等”实际是“两个句柄的 `impl` 指针相同”。这也解释了为什么不能手写一个“内容相同但 storage 不同”的 type——那会破坏 `operator==` 的语义。

## 05.3 `StorageUserBase`：句柄与 storage 的胶水

`StorageUniquerSupport.h:95` 定义了这套 CRTP 模板：

```cpp
template <typename ConcreteT, typename BaseT, typename StorageT,
          typename UniquerT, template <typename T> class... Traits>
class StorageUserBase : public BaseT, public Traits<ConcreteT>... {
public:
  using ImplType = StorageT;
  static TypeID getTypeID() { return TypeID::get<ConcreteT>(); }
  template <typename T> static bool classof(T val) {
    return val.getTypeID() == getTypeID();
  }
  ...
};
```

它把四件事绑在一起：

| 模板参数 | 作用 | 例子 |
|---|---|---|
| `ConcreteT` | 具体的 type/attr 类 | `IntegerType` |
| `BaseT` | 句柄基类 | `Type` 或 `Attribute` |
| `StorageT` | storage 结构 | `detail::IntegerTypeStorage` |
| `UniquerT` | 去重器 | `TypeUniquer` / `AttributeUniquer` |

在 uniquer 之上还有一层 `AbstractType`/`AbstractAttribute`：它是**每个具体类型的元数据**（`TypeID`、dialect、名字、接口表、`hasTrait` 函数），由 dialect 注册时插入 `MLIRContext`。`StorageUserBase::classof` 比较的是句柄里取出的 `TypeID`，而 `attachInterface` 改的是 `AbstractTy` 上的接口表：

```cpp
static void attachInterface(MLIRContext &context) {              // :155
  typename ConcreteT::AbstractTy *abstract =
      ConcreteT::AbstractTy::lookupMutable(TypeID::get<ConcreteT>(), &context);
  if (!abstract)
    llvm::report_fatal_error("Registering an interface for an attribute/type "
                             "that is not itself registered.");
  ...
}
```

这解释了维护时的第一个检查项：**接口挂不上，先查 dialect 是否已 load。** `lookupMutable` 找不到 `AbstractTy` 就直接 fatal。

`get()` 的实现（`:177` 起）先跑 `verifyInvariants`，再交给 uniquer：

```cpp
template <typename... Args>
static ConcreteT get(MLIRContext *ctx, Args &&...args) {
  assert(succeeded(ConcreteT::verifyInvariants(getDefaultDiagnosticEmitFn(ctx), args...)));
  return UniquerT::template get<ConcreteT>(ctx, std::forward<Args>(args)...);
}
```

而 `getChecked`（`:189`）不 assert，改为返回 null 并 emit diagnostic。这就是为什么 parser 里用 `getChecked`，内部构造用 `get`：**用户输入不能 abort，编译器内部不变量可以 abort。**

`mutate`（`:225`）需要显式 trait：

```cpp
template <typename... Args>
LogicalResult mutate(Args &&...args) {
  static_assert(std::is_base_of<StorageUserTrait::IsMutable<ConcreteT>, ConcreteT>::value,
                "The `mutate` function expects mutable trait ...");
  return UniquerT::template mutate<ConcreteT>(this->getContext(), getImpl(), ...);
}
```

所以“Attribute 不可变”更准确的说法是：**没有 `IsMutable` trait 的 storage 不可变**。给类型打上该 trait 后，`mutate` 仍要求不改变 unique key，否则会破坏 DenseSet。

## 05.4 uniquing 的三个契约：`KeyTy`、`hashKey`、`operator==`

每个 storage 必须回答三个问题。看内置 `IntegerTypeStorage`（`TypeDetail.h:28`）：

```cpp
struct IntegerTypeStorage : public TypeStorage {
  using KeyTy = std::tuple<unsigned, IntegerType::SignednessSemantics>;
  static llvm::hash_code hashKey(const KeyTy &key) { return llvm::hash_value(key); }
  bool operator==(const KeyTy &key) const {
    return KeyTy(width, signedness) == key;
  }
  static IntegerTypeStorage *construct(TypeStorageAllocator &allocator, KeyTy key) {
    return new (allocator.allocate<IntegerTypeStorage>())
        IntegerTypeStorage(std::get<0>(key), std::get<1>(key));
  }
  KeyTy getAsKey() const { return KeyTy(width, signedness); }

  unsigned width : 30;
  IntegerType::SignednessSemantics signedness : 2;
};
```

四个契约：

1. **`KeyTy` 是 identity。** 两个对象当且仅当 `KeyTy` 相等时被合并。`i32` 与 `si32` 的 `signedness` 不同，所以是两份 storage。
2. **`hashKey` 必须与 `operator==` 一致。** 相等必同 hash；否则 DenseSet 找不到已有项，uniquing 失效（表现为“明明一样却不相等”，非常难查）。
3. **`operator==` 是跨实例比较。** 注意它和 `KeyTy` 比，而不是和另一个 storage 比。
4. **`construct` 负责分配与拷贝。** `allocator.copyInto` 把外部内存拷进 storage 的 arena；**不 copy** 就是悬垂 `StringRef`。

`IntegerTypeStorage` 还用位域省内存：`width : 30` 意味着整数宽度上限是 `2^30-1`。维护者加字段时如果挤爆位域，编译器不会提醒“语义变了”。

`StringAttrStorage`（`AttributeDetail.h:238`）展示了正确 copy：

```cpp
struct StringAttrStorage : public AttributeStorage {
  using KeyTy = std::pair<StringRef, Type>;
  bool operator==(const KeyTy &key) const {
    return value == key.first && type == key.second;
  }
  static StringAttrStorage *construct(AttributeStorageAllocator &allocator, const KeyTy &key) {
    return new (allocator.allocate<StringAttrStorage>())
        StringAttrStorage(allocator.copyInto(key.first), key.second);
  }
};
```

`allocator.copyInto(key.first)` 是“输入 buffer 可以释放”的保证。

## 05.5 `TypeID`：为什么 `classof` 不用 RTTI

MLIR 编译时关闭 RTTI（`-fno-rtti`），所以 `dyn_cast<IntegerType>(t)` 不能靠 `dynamic_cast`。它靠 `TypeID`：

```cpp
static TypeID getTypeID() { return TypeID::get<ConcreteT>(); }        // StorageUniquerSupport.h:105
template <typename T> static bool classof(T val) {                   // :113
  return val.getTypeID() == getTypeID();
}
```

`TypeID`（`mlir/include/mlir/Support/TypeID.h:107`）内部只有一个指针（64 位上 8 字节）。`TypeID::get<T>()` 对同一个 `T` 返回同一个值；显式 ID 由链接器保证跨库一致，隐式回退只按类型名生成、不保证唯一。

显式 ID 的写法在头/源两处：

```cpp
// MyType.h（顶层，不在 namespace/class 内部）
class MyType : public Type::TypeBase<MyType, Type, detail::MyTypeStorage> { ... };
MLIR_DECLARE_EXPLICIT_TYPE_ID(mlir::mydialect::MyType);

// MyType.cpp（顶层）
MLIR_DEFINE_EXPLICIT_TYPE_ID(mlir::mydialect::MyType);
```

宏必须放在文件顶层，不能塞进 class 或 namespace——`TypeID.h:69` 的注释明确写了这一点。

宏展开后是一份显式特化的 `TypeIDResolver`，绕过隐式回退。`TypeID.h:95-104` 的注释说明了回退的代价：它用类型名生成 ID，**对匿名 namespace 中的类型直接禁止**，且“字符串名不保证这些上下文中的唯一性”。所以跨共享库或匿名 namespace 的类必须用显式 ID——这正是 out-of-tree 插件最容易出错的地方。

维护视角的两个后果：

- **不要在头文件里定义 `TypeID::get` 需要的类型。** 模板要求 `T` 完整可见，否则 `static_assert("TypeID::get<> requires the complete definition of T")`。
- **`MLIR_DECLARE_EXPLICIT_TYPE_ID` / `MLIR_DEFINE_EXPLICIT_TYPE_ID` 成对出现。** 只声明不定义会在链接期炸，尤其在 out-of-tree 插件里（练习 A 会踩）。

## 05.6 context 生命周期：storage 比 IR 活得久，但比 context 短

`MLIRContext` 持有两条 uniquer（`MLIRContext.cpp:223`、`:249`）。这决定了一个严格的**从属关系**：

```text
MLIRContext  必须最后死
  └── StorageUniquer（type/attribute 的 storage 都在这里）
        └── 所有 Type / Attribute / Operation / Value 句柄
```

也就是说：**IR 对象不能比 context 活得久。** 常见的错误是全局/静态缓存里存了 `Type`：

```cpp
static DenseMap<Type, unsigned> cache;   // 危险：跨 context、跨进程生命周期
```

如果这个 `Type` 来自一个已经销毁的 context，后续访问就是 UAF。正确做法是把缓存挂在 context 上（`ctx->getDiagEngine()` 之外的 `MLIRContext::getOrLoadDialect` 模式），或用 `TypeID` + 重新 `get`。

线程边界也在 context 上：`setThreadSafeLocking` 默认在单线程构造时关闭、`enableMultithreading` 时打开（`MLIRContext.cpp:297`、`:643`、`:671`）：

```cpp
void setThreadSafeLocking(bool enable) {
  affineUniquer.setThreadSafeLocking(enable);
  attributeUniquer.setThreadSafeLocking(enable);
  typeUniquer.setThreadSafeLocking(enable);
}
```

所以“多线程建 type”只有在开启多线程的 context 上安全。自己 `std::thread` 里 `IntegerType::get` 一个默认构造的 context，是数据竞争。

### 05.6.1 dialect 的懒加载与注册顺序

context 默认**不加载任何方言**；parser 第一次看到 `arith.addi` 时才按需加载。这带来一个维护者常踩的顺序问题：

```text
1. ctx 构造，注册 DialectRegistry（或依赖 build 时的静态注册）
2. parse 遇到 "mydialect.foo"
3. context 查 registry -> loadDialect<mydialect::MyDialect>()
4. MyDialect::initialize() 里 addTypes/addAttributes
5. 此时 MyType 的 AbstractType 才存在，get() 才可用
```

如果在第 4 步之前（比如某个全局构造函数里）调用 `MyType::get(&ctx, ...)`，`StorageUniquer` 没有登记该 storage，行为是 assertion 或未定义。修法不是“提前 load”，而是**不要在静态初始化期碰 context**。

## 05.7 自定义 Type/Attribute 的维护落点

ODS 的 `TypeDef`/`AttrDef` 会生成 `StorageUserBase` 的子类、`get`/`getChecked`、parser/printer 骨架。维护者真正要手改的通常只有三处：

1. **storage 的 `KeyTy`。** 新增一个字段（比如 `tensor` 的 encoding）必须进 `KeyTy`，否则两个不同 encoding 的 type 会被错误合并。
2. **`construct` 里的内存归属。** 引用外部内存（`StringRef`、`ArrayRef`）必须 `copyInto`。
3. **`verifyInvariants`。** parser 走 `getChecked` 时，这里是唯一能拒绝非法参数的地方。

ODS 会为每个 Type/Attr 生成这些部件：

| 生成物 | 来源 | 维护者何时要读它 |
|---|---|---|
| `class MyType` 声明 | `TypeDef` | 加 getter/自定义方法 |
| `MyTypeStorage` | `genStorageClass` 或手写 | 改 `KeyTy`、加字段 |
| `parse`/`print` | `assemblyFormat` | 语法变更、往返测试失败 |
| `verify` | `genVerifyDecl` + 手写 | 新增约束 |
| `get`/`getChecked` | 固定模板 | 确认走哪条错误路径 |

一个判断方法：**问“两个只在这个字段上不同的对象，应不应该相等？”** 应该相等 → 不进 `KeyTy`（只做派生缓存）；不应该相等 → 必须进 `KeyTy`。

## 05.8 源码落点

```bash
grep -n 'struct IntegerTypeStorage\|struct FunctionTypeStorage' mlir/lib/IR/TypeDetail.h
grep -n 'struct StringAttrStorage' mlir/lib/IR/AttributeDetail.h
grep -n 'class StorageUserBase\|static TypeID getTypeID\|static bool classof' mlir/include/mlir/IR/StorageUniquerSupport.h
grep -n 'class StorageUniquer\|setThreadSafeLocking\|registerParametricStorageType' mlir/include/mlir/Support/StorageUniquer.h
grep -n 'typeUniquer\|attributeUniquer\|affineUniquer' mlir/lib/IR/MLIRContext.cpp
grep -n 'class TypeID' mlir/include/mlir/Support/TypeID.h
```

本机实测：

```text
mlir/lib/IR/TypeDetail.h:28:struct IntegerTypeStorage : public TypeStorage {
mlir/lib/IR/TypeDetail.h:57:struct FunctionTypeStorage : public TypeStorage {
mlir/lib/IR/AttributeDetail.h:238:struct StringAttrStorage : public AttributeStorage {
mlir/include/mlir/IR/StorageUniquerSupport.h:95:class StorageUserBase : public BaseT, public Traits<ConcreteT>... {
mlir/include/mlir/IR/StorageUniquerSupport.h:105:  static TypeID getTypeID() { return TypeID::get<ConcreteT>(); }
mlir/include/mlir/IR/StorageUniquerSupport.h:113:    return val.getTypeID() == getTypeID();
mlir/include/mlir/Support/StorageUniquer.h:82:class StorageUniquer {
mlir/include/mlir/Support/StorageUniquer.h:143:  void setThreadSafeLocking(bool enable);
mlir/lib/IR/MLIRContext.cpp:223:  StorageUniquer typeUniquer;
mlir/lib/IR/MLIRContext.cpp:249:  StorageUniquer attributeUniquer;
mlir/include/mlir/Support/TypeID.h:107:class TypeID {
```

## 05.9 可跑实验

### 实验 1：uniquing 真的返回同一个指针（实测）

用本树 shared lib 链接（编译细节见练习 A）：

```cpp
MLIRContext ctx;
auto t1 = IntegerType::get(&ctx, 32);
auto t2 = IntegerType::get(&ctx, 32);
auto s1 = StringAttr::get(&ctx, "hello");
auto s2 = StringAttr::get(&ctx, "hello");
```

真实输出：

```text
IntegerType i32 == i32: 1, same impl: 1
StringAttr "hello" == "hello": 1
```

`==` 与 `impl` 指针都相同，说明 uniquing 生效。注意 `get(&ctx, 32)` 每次都要 hash 查表，所以热路径里应把 `Type` 缓存进局部变量。

### 实验 2：`DistinctAttr` 故意不去重（实测）

```cpp
auto base = IntegerAttr::get(t1, 7);
auto d1 = DistinctAttr::create(base);
auto d2 = DistinctAttr::create(base);
llvm::outs() << "DistinctAttr same payload == : " << (d1 == d2) << "\n";
llvm::outs() << "d1 refers to base: " << (d1.getReferencedAttr() == base) << "\n";
```

真实输出：

```text
DistinctAttr same payload == : 0
DistinctAttr same payload same impl: 0
d1 refers to base: 1
```

这正是 `DistinctAttr` 的用途：**同样的内容，不同的 identity**。它常用于需要“每处出现都不同”的语义（例如资源 id、独立缓存的 key）。维护者看到 `DistinctAttr` 就应意识到：这里的 `==` 永远为假。

### 实验 3：type/attribute 的语法层（实测）

```bash
cat > /tmp/mlirc/types.mlir <<'EOF'
func.func @f(%a: !test.smpla, %b: tensor<4x!test.smpla>) {
  %c = arith.constant dense<[[1, 2], [3, 4]]> : tensor<2x2xi32>
  return
}
EOF
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/mlir-opt /tmp/mlirc/types.mlir
```

真实输出：

```text
module {
  func.func @f(%arg0: !test.smpla, %arg1: tensor<4x!test.smpla>) {
    %cst = arith.constant dense<[[1, 2], [3, 4]]> : tensor<2x2xi32>
    return
  }
}
```

generic 形式（`--mlir-print-op-generic`）把函数类型整体放进 inherent property：

```text
"func.func"() <{function_type = (!test.smpla, tensor<4x!test.smpla>) -> (), sym_name = "f"}> ({
```

注意 `!test.smpla` 是自定义 type 的**别名写法**，`#...` 是 attribute。print 稳定说明 `assemblyFormat`/`printType` 没被改坏；维护自定义 type 时，这段输出就是往返测试的基线。

### 实验 4：ElementsAttr 接口的“真实报错”（实测）

```bash
cat > /tmp/mlirc/dense.mlir <<'EOF'
func.func @f() -> tensor<3xi32> {
  %c = arith.constant dense<[1, 2, 3]> : tensor<3xi32>
  return %c : tensor<3xi32>
}
EOF
$T/bin/mlir-opt /tmp/mlirc/dense.mlir -test-elements-attr-interface
```

真实输出（节选）：

```text
dense.mlir:2:8: error: Test iterating `int64_t`: unable to iterate type
dense.mlir:2:8: note: see current operation: %0 = "arith.constant"() <{value = dense<[1, 2, 3]> : tensor<3xi32>}> : () -> tensor<3xi32>
dense.mlir:2:8: error: Test iterating `uint64_t`: unable to iterate type
dense.mlir:2:8: error: Test iterating `APInt`: 1, 2, 3
dense.mlir:2:8: error: Test iterating `IntegerAttr`: 1 : i32, 2 : i32, 3 : i32
```

这个 pass 故意用 `emitError` 打印每种迭代结果。它说明 `ElementsAttr` 的“元素视图”是有类型约束的：`i32` 的 dense 不能按 `int64_t` 迭代，但可以按 `APInt`。维护 attribute 接口时，这种 pass 是最好的行为快照。

### 实验 5：distinct attribute 的并发创建（实测）

```bash
$T/bin/mlir-opt /tmp/mlirc/simple.mlir -test-distinct-attrs
```

真实输出：

```text
module {
  func.func @f(%arg0: i32, %arg1: i32) -> i32 {
    %0 = arith.addi %arg0, %arg1 : i32
    %1 = arith.muli %0, %arg0 : i32
    return %1 : i32
  }
}
```

这个 pass 在并行线程里反复创建 distinct attributes 并检查互不相同；IR 不变就是通过。它同时验证了 `setThreadSafeLocking(true)` 的路径：**并行 uniquing 不能产生重复 identity，也不能丢对象。**

### 实验 6：未注册 dialect 的边界（实测）

```bash
printf 'func.func @f() {\n  "foo.bar"() : () -> ()\n  return\n}\n' > /tmp/mlirc/unreg.mlir
$T/bin/mlir-opt /tmp/mlirc/unreg.mlir
```

真实输出：

```text
unreg.mlir:2:12: error: operation being parsed with an unregistered dialect. If this is intended, please use -allow-unregistered-dialect with the MLIR tool used
```

加 `--allow-unregistered-dialect` 后才通过。这直接关系到本章：**未注册方言没有 `TypeID`、没有 `AbstractType`，它的 type/attr 不走 uniquing**，只能作为 opaque 语法存在。你 debug“为什么我的自定义 type 没有 `classof`”时，先确认 dialect 注册了。

### 实验 7：attribute 的别名打印（实测）

```bash
cat > /tmp/mlirc/map.mlir <<'EOF'
#map = affine_map<(d0, d1) -> (d1, d0)>
func.func @f(%m: memref<4x4xf32>) {
  linalg.generic {indexing_maps = [#map, #map], iterator_types = ["parallel", "parallel"]} ins(%m : memref<4x4xf32>) outs(%m : memref<4x4xf32>) {
  ^bb0(%in: f32, %out: f32):
    linalg.yield %in : f32
  }
  return
}
EOF
$T/bin/mlir-opt /tmp/mlirc/map.mlir
```

真实输出（节选）：

```text
#map = affine_map<(d0, d1) -> (d1, d0)>
module {
  func.func @f(%arg0: memref<4x4xf32>) {
    linalg.generic {indexing_maps = [#map, #map], iterator_types = ["parallel", "parallel"]} ins(%arg0 : memref<4x4xf32>) outs(%arg0 : memref<4x4xf32>) {
```

`#map` 是 printer 给“被多处引用的 attribute”生成的别名。uniquing 保证 `[#map, #map]` 里两个元素是同一个 storage，所以别名只需打印一次。这也是维护 attribute print 时的一个隐性契约：**别名复用依赖 uniquing，不能给同一语义创建两个 storage。**

### 实验 8：方言清单（实测）

```bash
$T/bin/mlir-opt --show-dialects | tr ',' '\n' | wc -l
```

真实输出：

```text
50
```

50 个已注册方言，每个都贡献自己的 type/attr 存储。这个数字是本章事实基线：`--show-dialects` 列出的名字就是 context 里可被 `getOrLoadDialect` 的命名空间。

## 05.10 踩雷集锦

1. **错误直觉：`Type`/`Attribute` 里存了字段。** → 正确认识：它们只是 8 字节句柄，字段全在 storage；拷贝句柄不拷贝数据。
2. **错误直觉：新字段只影响 print。** → 正确认识：不进 `KeyTy` 就会被错误合并；两个语义不同的对象变成同一个，`==` 给出错误答案。
3. **错误直觉：`StringRef` 存进 storage 就行。** → 正确认识：必须 `allocator.copyInto`。否则输入 buffer 释放后 storage 悬垂，且往往在很晚才炸。
4. **错误直觉：`hashKey` 随便写。** → 正确认识：必须与 `operator==` 一致；不一致时 uniquing 静默失效，表现为“内容相同却不相等”。
5. **错误直觉：Attribute 永远不可变。** → 正确认识：带 `IsMutable` trait 的 storage 可以 `mutate`，但不能改 unique key；否则 DenseSet 失配。
6. **错误直觉：全局缓存 `Type` 很安全。** → 正确认识：storage 归 context 所有；缓存跨 context 就是 UAF。缓存要挂在 context 或每次重新 `get`。
7. **错误直觉：多线程 `get` 一定安全。** → 正确认识：取决于 context 的 `setThreadSafeLocking`；默认单线程构造下关闭。
8. **错误直觉：`classof` 用 `dynamic_cast`。** → 正确认识：MLIR 关闭 RTTI，靠 `TypeID`；`MLIR_DECLARE/DEFINE_EXPLICIT_TYPE_ID` 必须成对。

## 05.11 本章重点整理

- `Type`/`Attribute` 是句柄，storage 是唯一实体；`==` 本质上是指针比较。
- uniquing 由 `KeyTy`/`hashKey`/`operator==`/`construct` 四个契约维持，缺一不可。
- `KeyTy` 决定 identity；新增语义字段几乎总要进 `KeyTy`。
- `construct` 负责内存归属，外部内存一律 `copyInto`。
- `TypeID` 替代 RTTI 支撑 `classof`；显式 ID 必须声明+定义成对。
- 每个 context 有独立的 type/attribute uniquer；storage 不能比 context 活得久。
- 线程安全由 context 的 locking 开关决定，不是无条件成立。
- 未注册 dialect 的 type/attr 不走 uniquing，也没有 `TypeID`。

## 05.12 自我检核

- [ ] 能画出 context → StorageUniquer → storage → 句柄的指向关系
- [ ] 能说出 `KeyTy` 加字段与不加字段各自会发生什么
- [ ] 能解释为什么 `operator==` 是指针比较而不是深比较
- [ ] 能指出 `StringAttrStorage::construct` 里哪一行防止了悬垂
- [ ] 能说明 `get` 与 `getChecked` 的使用边界
- [ ] 能解释 `TypeID` 在没有 RTTI 时如何支撑 `dyn_cast`
- [ ] 能说出 storage 与 context 的销毁顺序，以及违反后的症状
- [ ] 能用 `-test-elements-attr-interface` / `-test-distinct-attrs` 做行为快照

## 05.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/TypeDetail.h`**
  - 读哪里：`IntegerTypeStorage`（28）、`FunctionTypeStorage`（57）以及后面的 `MemRefTypeStorage`
  - 能学到什么：内置 type 的 `KeyTy` 如何表达 identity，位域如何省内存
  - 和本章关联：05.4 的所有契约
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/IR/AttributeDetail.h`**
  - 读哪里：`StringAttrStorage`（238）、`DistinctAttributeUniquer`（292）
  - 能学到什么：`copyInto` 的正确用法与 distinct identity 的实现
  - 和本章关联：05.4、05.9 实验 2
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/IR/StorageUniquerSupport.h`**
  - 读哪里：`StorageUserBase`（95）、`get`/`getChecked`（178/188）、`mutate`（224）
  - 能学到什么：句柄与 storage 的胶水层，以及 invariant 检查位置
  - 和本章关联：05.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Support/StorageUniquer.h`**
  - 读哪里：`StorageUniquer`（82）、`registerParametricStorageType`（149）、`setThreadSafeLocking`（143）
  - 能学到什么：DenseSet 去重与线程锁的开关
  - 和本章关联：05.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/DefiningDialects/AttributesAndTypes.md`**
  - 读哪里：`TypeDef`/`AttrDef`、`assemblyFormat`、storage 参数与 `genStorageClass`
  - 能学到什么：ODS 生成物与你手写 storage 的对应关系
  - 和本章关联：05.7 的三个维护落点

Type/Attribute 解决了“值怎么存”，但没有回答“op 自己的固有数据放哪”。下一章看 properties：哪些 attribute 是 op 的一部分，哪些只是可丢弃的注解，以及它们怎么进 bytecode。

→ 下一章：[Ch 06 Properties、属性与 bytecode](./06-properties-and-bytecode.md)
