# Ch 5 — `Type` 与 `DataLayout`：类型系统、布局与 opaque pointer

> **目标**：把 LLVM IR 的「类型形状」和「目标布局」拆成两个可独立验证的问题。学完这章，你能在源码里判断一个 `Type *` 到底回答什么、为什么 `ptr` 不再告诉你元素类型、`TypeSize` 为什么不能随手转成整数，以及一次 `getTypeAllocSize` / `getABITypeAlign` 调用在目标 ABI 下会算出什么。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，`RelWithDebInfo` + assertions ON。本章命令与输出都在这个构建上真实运行。

## 为什么需要这个？

Review 里大量「看起来只是查了个类型」的改动，真正的风险在布局：

- 一个 pass 把 `i64` 当成 4 字节对齐，在 x86_64 上算出的结构体偏移就少了一半。
- 一段代码对 `<vscale x 4 x i32>` 调 `getFixedValue()`，在 assertions 构建里直接 abort；在 release 构建里则可能悄悄按最小尺寸处理，生成只在特定硬件上错的代码。
- 一个 diff 还在调用 `getPointerElementType()`，它要么编译不过，要么说明作者在读旧版本的 API。

这三个问题对应本章的三条主线：

1. `Type` 描述 IR 的形状，不描述目标上的字节数。
2. opaque pointer 把「指针指向什么」从类型里拿掉，放回指令本身。
3. `DataLayout` 把类型映射成目标上的 size / store size / alloc size / ABI align / preferred align。

## 先建立直觉

把 `Type` 想成一张**形状图纸**：它告诉你这是 `i32`、`[4 x i32]`、`{i8, i32}`，还是 `ptr`。图纸不告诉你「在这台机器上占几个字节」，因为同一张图纸在 x86_64 和 i386 上可以有不同的对齐和尾部填充。它还不可变，并在同一 `LLVMContext` 内唯一化（`Type.h:38-44`），所以比较两个 `Type *` 就是比较类型是否相同。

`DataLayout` 才是**施工规范**：它按目标 ABI 决定每个字段放哪、结构体尾巴补多少、指针在地址空间 0 是几字节。一个 `Module` 带一份 `DataLayout`，所有布局查询都从 `Module::getDataLayout()` 出发。

```text
Type（形状，跨目标）
  i32 / i64 / {i8, i32} / <4 x i32> / ptr
        │
        │  DataLayout（目标规范）
        ▼
字节数、store size、alloc size、ABI align、preferred align、字段偏移
```

## `Type` 层次：一张图看完

`Type` 基类在 `llvm/include/llvm/IR/Type.h:46`，所有具体类型在 `llvm/include/llvm/IR/DerivedTypes.h`。`TypeID` 枚举在 `Type.h:55`，`getTypeID()` 在 `Type.h:138`；`isa` / `cast` / `dyn_cast` 通过各子类的 `classof` 读这个 ID。

```text
Type (Type.h:46)
├─ IntegerType        (DerivedTypes.h:42)    iN
├─ FunctionType       (DerivedTypes.h:165)   ret (params...)
├─ StructType         (DerivedTypes.h:278)   { ... } / %name
├─ ArrayType          (DerivedTypes.h:453)   [N x T]
├─ VectorType         (DerivedTypes.h:485)
│  ├─ FixedVectorType    (DerivedTypes.h:645)
│  └─ ScalableVectorType (DerivedTypes.h:692)
├─ PointerType        (DerivedTypes.h:753)   ptr / ptr addrspace(N)
└─ TargetExtType      (DerivedTypes.h:828)   target("...")
```

| 类型 | 工厂 / 读法 | 关键语义 | 布局入口 |
| --- | --- | --- | --- |
| `IntegerType` | `Type::getInt32Ty(C)`、`IntegerType::get(C, N)` | 任意位宽，`1 <= N <= 1<<23`；`i1` 是布尔，`i8` 常被当字节 | 位宽来自 `getBitWidth()`，见 `DerivedTypes.h:82` |
| `FunctionType` | `FunctionType::get(Ret, Params, VarArg)` | 返回类型 + 参数类型 + 是否 varargs；本身不是可分配对象 | 函数类型没有 size，函数指针才是 `ptr` |
| `StructType` | `StructType::get(C, {...})` / `StructType::create` | literal 或 identified；可 packed、可 opaque；字段顺序就是布局顺序 | `StructLayout`，见 `DataLayout.h:746` |
| `ArrayType` | `ArrayType::get(Elem, N)` | 固定元素个数，元素类型相同 | `N * getTypeAllocSizeInBits(Elem)`，见 `DataLayout.h:802` |
| `VectorType` | `FixedVectorType::get` / `ScalableVectorType::get` | 固定或 scalable；元素个数是 `ElementCount` | vector spec 或自然对齐，见 `DataLayout.cpp:922` |
| `PointerType` | `PointerType::get(C, AS)` / `getUnqual(C)` | 只带地址空间，不带 pointee 类型 | pointer spec，见 `DataLayout.h:504` |
| `TargetExtType` | `TargetExtType::get(C, Name, Types, Ints)` | 目标扩展类型，参数由 target 解释；有 `getLayoutType()` | 按 layout type 算，见 `DataLayout.cpp:988` |

几个 reviewer 常打交道的边界：

- `IntegerType` 的位宽存在 `SubclassData` 里，`MAX_INT_BITS` 是 `1<<23`（`DerivedTypes.h:52-57`）。看到 `i8388608` 不是笔误，是上限。
- `StructType` 可以是 opaque（`isOpaque()`，`DerivedTypes.h:354`）。对 opaque struct 调 `getStructLayout` 会命中 `DataLayout.cpp:50` 的断言，因为还没有字段可排。
- `StructType::isSized()` 不是简单看有没有 body：包含 scalable vector 的结构体默认不算 sized，只有「所有字段都是同一种 scalable vector」是例外（`Type.cpp:696-721`）。

## Opaque pointer：为什么 `ptr` 不再带元素类型

### 旧模型的问题

传统 IR 的指针类型带 pointee：`i32*` 表示「指向 i32 的指针」。问题是这个 pointee **不携带真实语义**：

- 指针可以随便 `bitcast` 成别的指针类型，pointee 不保证是内存里真实的类型。
- 大量 intrinsic 只关心「一个指针 + 一个长度」，于是 IR 里堆满 no-op bitcast。
- pass 沿 def-use 链找底层指针时容易忘记 `stripPointerCasts()`；沿 use 链走时又要跳过 bitcast。
- 前端做地址空间转换时，bitcast 还可能丢掉地址空间信息。

`llvm/docs/OpaquePointers.md:5-18` 把这套动机写得很清楚；`OpaquePointers.md:89-95` 还拿整数 signedness 做过类比：以前有 `i32` / `u32` 之分，后来把 signedness 放进指令 flags，类型只留位宽。

### 新模型

opaque pointer 只保留地址空间，文本写成 `ptr`；需要知道「按什么类型访问内存」的指令，自己带类型操作数：

```llvm
; 旧
%v = load i32, i32* %p
; 新
%v = load i32, ptr %p
```

`llvm/docs/OpaquePointers.md:14-25` 给了同一组 before/after。`getelementptr` 也一样，源元素类型在指令里：`getelementptr i8, ptr %p, i64 1`。

LLVM 17 起 opaque pointer 成为默认模式；本树已经不再提供 typed pointer 模式。把旧 IR 喂给当前 `opt`，它会把 `i8*` 自动升级成 `ptr`：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
cat > typed.ll <<'LL'
define i8* @test(i8* %p) {
  %p2 = getelementptr i8, i8* %p, i64 1
  ret i8* %p2
}
LL
$B/opt -S -passes=verify typed.ll -o -
```

实测输出（节选）：

```text
define ptr @test(ptr %p) {
  %p2 = getelementptr i8, ptr %p, i64 1
  ret ptr %p2
}
```

注意 `OpaquePointers.md:99-105` 仍然写着可以用 `-opaque-pointers=0` 关闭。这段文档在**当前树已经过时**：

```bash
$B/opt -opaque-pointers=0 -S -passes=verify typed.ll -o /dev/null
```

实测：

```text
opt: Unknown command line argument '-opaque-pointers=0'.
```

这正是 review 的一条通用规则：文档里的开关可能比代码晚删一个版本；判断 API 是否存在，以你正在读的树和实际构建为准。

### `getPointerTo` 与 `getPointerElementType` 的移除

当前 `PointerType` 只剩两个工厂重载：

- `PointerType::get(LLVMContext &C, unsigned AddressSpace)`，`DerivedTypes.h:759`
- `PointerType::getUnqual(LLVMContext &C)`，`DerivedTypes.h:766`

`Type::getPointerTo(unsigned)` 曾经是「给当前类型套一层指针」的快捷方式。它在 2024-11-06 被标记 deprecated（commit `29e467fc78eb`），在 2026-07-31 被删除（commit `bfc237a51541`）。`Type::getPointerElementType()` 更早，在 2023-07-19 删除（commit `8606cbf97f09`）。这意味着：

- 你在当前树里 `rg getPointerElementType`，在 `llvm/include/llvm/IR` 下找不到定义；看到的只可能是旧分支、下游 fork 或文档。
- 读旧 commit / 旧 PR 时，必须意识到当时的代码可能靠指针类型拿元素类型；把那段逻辑照搬到当前树会编译失败。
- 新代码要拿「内存访问的类型」，只能从指令或全局对象上拿。

迁移映射（`OpaquePointers.md:138-150` 的官方清单）：

| 旧写法 | 当前写法 |
| --- | --- |
| `LI->getPointerOperandType()->getPointerElementType()` | `LI->getType()` |
| `SI->getPointerOperandType()->getPointerElementType()` | `SI->getValueOperand()->getType()` |
| load/store 共用 | `getLoadStoreType()` |
| `GEP->getPointerOperandType()->getPointerElementType()` | `GEP->getSourceElementType()` |
| `Call->getCalledOperand()->getType()->getPointerElementType()` | `Call->getFunctionType()` |
| `Alloca->getPointerOperandType()->getPointerElementType()` | `Alloca->getAllocatedType()` |
| `GV->getType()->getPointerElementType()` | `GV->getValueType()` |

对源码阅读的影响，可以总结成一句话：**看到 `Type *` 参数时，先问它表示的是「值类型」还是「内存类型」**。opaque pointer 之后，很多旧 API 的 `Type *Ty` 参数失去了「从指针类型推出来」的来源，调用方必须显式传入内存类型。一个 diff 如果新增了「从 `ptr` 反推 pointee」的辅助函数，基本可以直接怀疑它逆着当前设计走。

边界：`TypeID` 里仍然保留 `TypedPointerTyID`（`Type.h:75`），对应 `llvm/include/llvm/IR/TypedPointerType.h:26` 的 `TypedPointerType`，主要给少数 GPU target 使用。它不是通用 IR 的指针表示，不能拿来论证「LLVM 还支持 typed pointer」。

## `TypeSize` 与 scalable vector

### fixed 与 scalable 的区别

固定向量 `<4 x i32>` 在编译期就知道有 4 个元素；scalable 向量 `<vscale x 4 x i32>` 只知道「至少 4 个」，运行时元素个数是 `4 * vscale`。`vscale` 是目标相关的正整数，按 LangRef 是 2 的幂（`llvm/docs/LangRef.md:5029-5033`）。同一份 IR 在不同硬件上，`vscale` 可以不同。

`VectorType` 把元素个数存成 `ElementCount`（字段说明在 `DerivedTypes.h:493-503`；`ElementCount` 类本身在 `llvm/include/llvm/Support/TypeSize.h:294`）：一个最小数量 + 一个 `scalable` 布尔。`TypeSize` 继承同一套表示（`llvm/include/llvm/Support/TypeSize.h:337`），所以任何返回 `TypeSize` 的布局查询都可能带回一个「最小值 + 是否 scalable」。

关键 API：

| API | 含义 | scalable 时能不能直接用 |
| --- | --- | --- |
| `getKnownMinValue()` | 编译期已知的最小值 | 可以，但它不是完整大小 |
| `isScalable()` / `isFixed()` | 是否乘 `vscale` | 可以 |
| `getFixedValue()` | 要求精确固定值 | 不可以；非零 scalable 会断言 |
| 隐式转 `uint64_t` | 旧代码的兼容路径 | 不可以；scalable 会 fatal error |

`getFixedValue()` 的实现就在 `TypeSize.h:201-205`：

```cpp
constexpr ScalarTy getFixedValue() const {
  assert((!isScalable() || isZero()) &&
         "Request for a fixed element count on a scalable object");
  return getKnownMinValue();
}
```

隐式转换更狠：`TypeSize.h:384-391` 在 scalable 时直接 `reportFatalInternalError`，因为 release 构建没有断言，继续算下去会得到「把最小值当精确值」的错误代码。

### 失败实验：对 scalable vector 调 `getFixedValue()`

```cpp
ScalableVectorType *SV = ScalableVectorType::get(I32, 4);
DataLayout DL("e-m:e-p:64:64-i64:64-i128:128-n32:64-S128");
TypeSize Bits = DL.getTypeSizeInBits(SV);
TypeSize Alloc = DL.getTypeAllocSize(SV);
errs() << "scalable=" << Bits.isScalable()
       << " knownMinBits=" << Bits.getKnownMinValue()
       << " allocKnownMin=" << Alloc.getKnownMinValue()
       << " allocScalable=" << Alloc.isScalable() << '\n';
errs() << "forcing fixed value: " << Bits.getFixedValue() << '\n';
```

实测输出：

```text
scalable=1 knownMinBits=128 allocKnownMin=16 allocScalable=1
forcing fixed value: scalable: .../TypeSize.h:203: ... Assertion
`(!isScalable() || isZero()) && "Request for a fixed element count on a scalable object"' failed.
Aborted (core dumped)
```

`<vscale x 4 x i32>` 的已知最小尺寸是 128 bit / 16 字节，运行时是 `16 * vscale` 字节。`getTypeAllocSize` 返回的 `TypeSize` 仍然带 scalable 标记；把它当 16 字节常量去算数组 stride，只有在 `vscale == 1` 的机器上才对。

正确的处理分三种：

1. 算法对 scalable 也成立：用 `getKnownMinValue()` 做下界推理，保留 `isScalable()`。
2. 算法只对 fixed 成立：先 `if (TS.isScalable()) return false;` 或走保守路径，再取 fixed 值。
3. 只想知道「至少多大」：明确写下「这是下界」，不要把它当精确 alloc size。

## `DataLayout`：把类型映射到目标

### spec 字符串怎么读

`DataLayout` 由 `target datalayout` 构造，解析入口是 `DataLayout::parseLayoutString`（`llvm/lib/IR/DataLayout.cpp:685`），按 `-` 分段，每段调用 `parseSpecification`。最常用的几段：

| 段 | 含义 | 例子 |
| --- | --- | --- |
| `e` / `E` | 小端 / 大端 | `e` |
| `p[AS]:<size>:<abi>[:<pref>[:<idx>]]` | 指针大小与对齐 | `p:64:64`、`p270:32:32` |
| `i<size>:<abi>[:<pref>]` | 整数位宽与对齐 | `i64:64`、`i128:128` |
| `f<size>:<abi>[:<pref>]` | 浮点大小与对齐 | `f80:128` |
| `v<size>:<abi>[:<pref>]` | 向量大小与对齐 | `v128:128` |
| `a:<abi>[:<pref>]` | 聚合（结构体）对齐 | `a:8:64` |
| `S<size>` | 栈自然对齐 | `S128` |
| `n<widths>` | 原生整数宽度 | `n32:64` |

`i64:64` 读作「64 位整数，ABI 对齐 64 位」，也就是 8 字节。`parsePrimitiveSpec` 在 `DataLayout.cpp:360-393` 解析 `[ifv]<size>:<abi>[:<pref>]`，并且要求对齐是「2 的幂 × 字节宽度」；`i8` 还必须是 8 位对齐。

真实的前端输出：

```bash
$B/clang -cc1 -triple x86_64-unknown-linux-gnu -emit-llvm -o - t.c | head -5
$B/clang -cc1 -triple i386-unknown-linux-gnu -emit-llvm -o - t.c | head -5
```

实测：

```text
target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-i128:128-f80:128-n8:16:32:64-S128"
target datalayout = "e-m:e-p:32:32-p270:32:32-p271:32:32-p272:64:64-i128:128-f64:32:64-f80:32-n8:16:32-S128"
```

x86_64 明确有 `i64:64`；i386 没有 `i64` 段，于是走 `DataLayout.cpp:180-185` 的默认表：`i64` 的 ABI 对齐是 4 字节，preferred 对齐是 8 字节。这就是同一份 `{i8, i64}` 在两个目标上布局不同的根源。

### 五个必须分清的查询

| 查询 | 回答什么 | 典型误用 |
| --- | --- | --- |
| `getTypeSizeInBits` (`DataLayout.h:573`) | 类型有多少 bit | 以为它已经按字节或对齐取整 |
| `getTypeStoreSize` (`DataLayout.h:582`) | 一次 store 最多写多少字节 | 和 alloc size 混用 |
| `getTypeAllocSize` (`DataLayout.h:618`) | 连续两个对象之间的字节距离，含对齐填充 | 以为等于 store size |
| `getABITypeAlign` (`DataLayout.h:633`) | ABI 要求的最小对齐 | 用 preferred align 做正确性判断 |
| `getPrefTypeAlign` (`DataLayout.h:652`) | 优化偏好的对齐，至少不低于 ABI | 把它当成 ABI 保证 |

`i36` 是最直观的例子：

- `getTypeSizeInBits(i36)` = 36
- `getTypeStoreSize(i36)` = 5（向上取整到字节，`DataLayout.h:595-599`）
- `getTypeAllocSize(i36)` = 8（5 字节按 `i64` 的 ABI 对齐 8 取整，`DataLayout.cpp:951-958`）
- `getABITypeAlign(i36)` = 8（`getIntegerAlignment` 找「下一个不小于 36 的 spec」，命中 `i64`，`DataLayout.cpp:780-793`）

`getIntegerAlignment` 的规则值得记住：它不要求精确匹配，而是取第一个 `BitWidth >= 请求位宽` 的 spec；如果没有，退回最大的那个。所以 `i19` 会命中 `i32` 的 4 字节对齐，`i36` 会命中 `i64` 的 8 字节对齐。

### 结构体布局

`StructLayout` 的构造函数在 `DataLayout.cpp:48-87`，算法可以压缩成四步：

1. 每个字段按 ABI 对齐（packed struct 强制 1）向上取整当前偏移。
2. 记录字段偏移，再把 `getTypeAllocSize(FieldTy)` 加到结构体大小。
3. 结构体对齐取所有字段 ABI 对齐的最大值。
4. 尾部按结构体对齐补齐，保证结构体数组的每个元素都对齐。

```text
struct { i8, i64 } on x86_64:
offset 0: i8
offset 1..7: padding
offset 8: i64
size: 16, align: 8

struct { i8, i64 } on i386:
offset 0: i8
offset 1..3: padding
offset 4: i64
size: 12, align: 4
```

`StructLayout` 是惰性创建并缓存的（`DataLayout.cpp:798-815`），所以同一 `DataLayout` 上反复查同一个结构体不会重复排版。缓存 key 是 `StructType *`：literal struct 按结构唯一化，字段相同的 literal struct 共享同一个 key；named struct 即使字段相同也是不同 key。

`StructABIAlignment` 的默认值是 1，`StructPrefAlignment` 的默认值是 8（`DataLayout.h:150-152`）。这解释了一个容易误读的输出：`{i8, i32}` 的 ABI align 是 4，但 `getPrefTypeAlign` 可能是 8，因为 preferred 对齐取 `max(StructPrefAlignment, 字段最大对齐)`。**preferred 不是 ABI 保证**，不能拿它算「最小合法偏移」。

### 实测：用 `llvm-config` 编译布局打印程序

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/clang++ $($B/llvm-config --cxxflags) layout.cpp -o layout \
  $($B/llvm-config --ldflags --libs core support)
LD_LIBRARY_PATH=/home/ubuntu/llvm/builds/pr-lane/lib ./layout
```

`layout.cpp` 的核心：

```cpp
static void dump(const char *Name, Type *Ty, const DataLayout &DL) {
  TypeSize Alloc = DL.getTypeAllocSize(Ty);
  TypeSize Store = DL.getTypeStoreSize(Ty);
  outs() << Name << " bits=" << DL.getTypeSizeInBits(Ty).getFixedValue()
         << " store=" << Store.getFixedValue()
         << " alloc=" << Alloc.getFixedValue()
         << " abi=" << DL.getABITypeAlign(Ty).value()
         << " pref=" << DL.getPrefTypeAlign(Ty).value() << '\n';
}

LLVMContext C;
Type *I8 = Type::getInt8Ty(C);
Type *I32 = Type::getInt32Ty(C);
Type *I64 = Type::getInt64Ty(C);
StructType *T = StructType::get(C, {I8, I64});
FixedVectorType *V4 = FixedVectorType::get(I32, 4);

DataLayout LP64("e-m:e-p:64:64-i64:64-i128:128-n32:64-S128");
DataLayout I386("e-m:e-p:32:32-p270:32:32-p271:32:32-p272:64:64-i128:128-f64:32:64-f80:32-n8:16:32-S128");
dump("i64", I64, LP64);
dump("{i8,i64}", T, LP64);
dump("<4 x i32>", V4, LP64);
outs() << "LP64 T offset(1)=" << LP64.getStructLayout(T)->getElementOffset(1).getFixedValue()
       << " size=" << LP64.getStructLayout(T)->getSizeInBytes().getFixedValue() << '\n';
dump("i64", I64, I386);
dump("{i8,i64}", T, I386);
outs() << "i386 T offset(1)=" << I386.getStructLayout(T)->getElementOffset(1).getFixedValue()
       << " size=" << I386.getStructLayout(T)->getSizeInBytes().getFixedValue() << '\n';
```

实测输出（保留完整数值）：

```text
== LP64 (i64:64) ==
i19 bits=19 store=3 alloc=4 abi=4 pref=4
i32 bits=32 store=4 alloc=4 abi=4 pref=4
i36 bits=36 store=5 alloc=8 abi=8 pref=8
i64 bits=64 store=8 alloc=8 abi=8 pref=8
{i8,i32} bits=64 store=8 alloc=8 abi=4 pref=8
{i8,i64} bits=128 store=16 alloc=16 abi=8 pref=8
<4 x i32> bits=128 store=16 alloc=16 abi=16 pref=16
== i386 (default i64:32:64) ==
i64 bits=64 store=8 alloc=8 abi=4 pref=8
{i8,i64} bits=96 store=12 alloc=12 abi=4 pref=8
LP64 T offset(1)=8 size=16
i386 T offset(1)=4 size=12
```

用 clang 的 record layout 侧证，输出（节选）一致：

```bash
printf 'struct T { char c; long long x; }; struct T t;\n' > lay.c && $B/clang -cc1 -fdump-record-layouts -triple x86_64-unknown-linux-gnu -emit-obj -o /dev/null lay.c
```

```text
0 | struct T
0 |   char c
8 |   long long x
  | [sizeof=16, align=8]
```

换成 `-triple i386-unknown-linux-gnu`，同一份 C 代码变成 `offset 4, sizeof=12, align=4`。C 结构体布局和 `DataLayout` 的 `StructLayout` 在这里是对齐的，这给了我们一条可重复的交叉验证路径。

### 失败例子：把 `i64` 当 4 字节对齐

假设一个 pass 在计算 `{i8, i64}` 的字段偏移时写死：

```cpp
uint64_t Offset = alignTo(1, 4); // 以为 i64 按 4 字节对齐
```

在 i386 上这算出 4，是对的；在 x86_64 上真实偏移是 8，于是：

- 对字段 1 的 `getelementptr` 少加了 4 字节，读到 padding 或前一个字段的尾部。
- 如果再用 `sizeof = 1 + 8 = 9` 或 `alignTo(9, 4) = 12` 算数组 stride，真实 stride 是 16，第二个元素开始就错位。
- 这类错误在只看 IR 文本时很难发现，因为 IR 里只写 `getelementptr %struct.T, ptr %p, i32 0, i32 1`，偏移由 `DataLayout` 决定；手写的常量偏移则绕过了它。

正确的写法只有一条：**偏移和对齐都从 `DataLayout` 查询，不跨目标复用常量**。如果必须缓存，缓存 key 至少包含 `DataLayout` 或目标 triple。

## 踩雷集锦

1. **把 `TypeSize` 当 `uint64_t` 用**：`getTypeAllocSize` 返回的是「最小值 + scalable 标记」。对 scalable vector 调 `getFixedValue()` 会断言，隐式转换会 fatal error。错误直觉是「尺寸就是个整数」；正确认识是「尺寸可能是 `vscale` 的倍数，先判断 `isScalable()`」。
2. **还在找 `getPointerElementType()` / `getPointerTo()`**：这两个 API 在当前树已经删除（`8606cbf97f09`、`bfc237a51541`）。错误直觉是「指针类型能告诉我 pointee」；正确认识是「pointee 在 load/store/gep/alloca/global/call 指令上」。
3. **把 `getPrefTypeAlign` 当 ABI 对齐**：preferred 只表示「这样排更快」，可以大于 ABI。用 preferred 算最小合法偏移，会在 packed 或跨目标场景下多插 padding，改变 ABI。
4. **把 `getTypeStoreSize` 当 `getTypeAllocSize`**：`i36` 的 store size 是 5，alloc size 是 8。用 store size 算数组 stride，第二个元素就错位。
5. **拿 host 的 `DataLayout` 解释 target 的 IR**：`DataLayout` 是 per-`Module` 的，来自 `target datalayout`。在 x86_64 主机上分析 i386 模块，却用 host 的 `i64:64` 算偏移，是 review 里很隐蔽的一类错误。

## 动手练习

1. 把 `layout.cpp` 里的 `i64` 换成 `i128`，在 x86_64 和 i386 两个 `DataLayout` 上打印 size / store / alloc / ABI align，解释差异来自哪一段 spec。
2. 用 `clang -cc1 -fdump-record-layouts` 打印一个带 `char`、`long long`、`double` 的结构体，把输出和 `layout.cpp` 的 `StructLayout` 结果逐项对上。
3. 写一个函数，只接受 fixed-size 类型：遇到 `TypeSize::isScalable()` 就返回 `false`。再用 `<vscale x 4 x i32>` 调它，确认没有触发断言。

## 本章重点整理

- `Type` 是不可变、按 `LLVMContext` 唯一化的形状描述；比较 `Type *` 就是比较类型。
- opaque pointer 只保留地址空间；元素类型在指令上。`getPointerTo` / `getPointerElementType` 已从当前树删除。
- `TypeSize` 是「已知最小值 + scalable 标记」；scalable 时不能用 `getFixedValue()` 或隐式转整数。
- `DataLayout` 是 per-`Module` 的目标规范；`i64:64` 表示 64 位整数、64 位 ABI 对齐。
- size / store size / alloc size / ABI align / preferred align 是五个不同问题，不能互相替代。
- 结构体布局由字段 ABI 对齐、字段 alloc size、尾部填充共同决定；packed 只把字段对齐降到 1。

## 自我检核

- [ ] 不看源码，能画出 `Type` 到 `IntegerType` / `FunctionType` / `StructType` / `ArrayType` / `VectorType` / `PointerType` / `TargetExtType` 的层次，并说出每个类的一个关键读法
- [ ] 能解释为什么 `Type *` 比较不需要结构递归，以及这个性质依赖哪个前提
- [ ] 能用自己的话说明 opaque pointer 解决了哪三类问题，并给出至少三个「用指令拿内存类型」的替代 API
- [ ] 知道 `getPointerTo` 和 `getPointerElementType` 分别在哪一年、哪个 commit 被删除；能在当前树里验证它们不存在
- [ ] 能解释 `TypeSize` 的 `getKnownMinValue`、`isScalable`、`getFixedValue` 三者各自的适用条件
- [ ] 能说出 `<vscale x 4 x i32>` 的已知最小 bit 数和运行时字节数之间的关系
- [ ] 能区分 `getTypeSizeInBits`、`getTypeStoreSize`、`getTypeAllocSize`，并用 `i36` 举例
- [ ] 能解释 `getABITypeAlign` 与 `getPrefTypeAlign` 的差别，以及为什么不能用后者做正确性判断
- [ ] 能构造一个「把 `i64` 当 4 字节对齐」的错误例子，并指出它在 x86_64 上会错在哪

## 延伸阅读

- **[LLVM Opaque Pointers](https://llvm.org/docs/OpaquePointers.html)**
  - **读哪里**：`The Opaque Pointer Type`、`Issues with explicit pointee types`、`Migration Instructions`
  - **能学到什么**：为什么 pointee 类型没有语义、no-op bitcast 如何污染 def-use 链，以及迁移到 `getLoadStoreType()` / `getSourceElementType()` 的完整清单
  - **前提**：读过本章的 `PointerType` 一节，知道 `ptr` 只带地址空间
- **[LLVM Language Reference: Data Layout](https://llvm.org/docs/LangRef.html#data-layout)**
  - **读哪里**：整节，重点看 `i<n>:<abi>:<pref>`、`p[n]:<size>:<abi>:<pref>`、`a:<abi>:<pref>`、`S<size>`
  - **能学到什么**：spec 字符串的权威语法；本章只讲了常见段，LangRef 还有地址空间名、non-integral、指针 flags 等边界
  - **前提**：能读懂 `target datalayout` 这一行
- **[LLVM Language Reference: Vector Type](https://llvm.org/docs/LangRef.html#vector-type)**
  - **读哪里**：fixed 与 scalable vector 的定义、`vscale` 的说明
  - **能学到什么**：`<vscale x 4 x i32>` 的运行时语义，以及为什么 `vscale` 不是编译期常量
  - **前提**：知道 `TypeSize` 的 known-min 表示
- **[LLVM Programmer's Manual: The Type class and derived types](https://llvm.org/docs/ProgrammersManual.html#the-type-class-and-derived-types)**
  - **读哪里**：`Type`、`DerivedTypes`、`isSized` 相关段落
  - **能学到什么**：官方对类型唯一化、`isa/cast`、`isSized` 的表述，可以对照本章的源码行号阅读
  - **前提**：熟悉 C++ 的继承与 `dyn_cast`
- **[LLVM Doxygen: DataLayout](https://llvm.org/doxygen/classllvm_1_1DataLayout.html)**
  - **读哪里**：`getTypeAllocSize`、`getTypeStoreSize`、`getABITypeAlign`、`getPrefTypeAlign` 的注释
  - **能学到什么**：每个查询的精确契约；比二手博客可靠
  - **前提**：知道 `TypeSize` 和 `Align` 的基本用法

→ [Ch 6 — `Module` / `Function` / `BasicBlock` / `Instruction`：容器、所有权、迭代器](./06-module-function-basicblock-instruction.md)
