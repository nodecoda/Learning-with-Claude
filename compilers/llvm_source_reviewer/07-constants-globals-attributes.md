# Ch 7 — 常量、全局值与调用契约：`Constant`、`GlobalValue`、`AttributeList`、`CallingConv`

> **目标**：把「一个值在编译期是什么」拆成三层：常量对象如何表示、全局符号如何链接、函数和调用点之间的属性与调用约定如何形成契约。学完这章，你能在 review 中判断一个 `ConstantExpr` 为什么会挡住 RAUW，一个 `available_externally` 定义为什么可以消失，以及一条 `nonnull` 或 `musttail` 什么时候只是声明、什么时候会变成 UB。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`；实验构建 `/home/ubuntu/llvm/builds/pr-lane`，`RelWithDebInfo` + assertions ON。源码引用按这个 commit；命令输出由 `pr-lane` 生成。该构建的 `clang --version` 打印的 revision 是 `a5eaf1948b6b`，与源码树不是同一个提交，因此前端细节以实测输出为准，IR 语义与源码位置以 `7c7a9072f736` 为准。

## 心智模型：常量是结构，全局是身份，属性是承诺

```
                    ┌─────────────────────────────┐
                    │        Constant             │
                    │  不可变、按结构唯一化        │
                    └─────────────┬───────────────┘
                                  │
          ┌───────────────────────┼────────────────────────┐
          │                       │                        │
   ConstantData            ConstantExpr              GlobalValue
   （无 operands）          （有 opcode/operands）     （有链接名字）
   int/fp/null/undef        gep/add/bitcast/...       Function/GlobalVariable
   poison/zero/array                                  GlobalAlias/GlobalIFunc

   AttributeList ── 挂在 Function / CallBase 上 ── 优化器据此假设参数和返回值的性质
   CallingConv   ── 决定参数传递、varargs、尾调用能否成立
```

先记住三个边界：

- `ConstantData` 没有 operand，也没有 use list；`ConstantExpr` 有 operand，因此会参与常量的 use 关系。
- `GlobalValue` 本身也是 `Constant`，所以它可以出现在常量表达式和全局初始化器里。
- `Attribute` 不是 IR 指令，它不产生 SSA 值；它只把「调用者保证什么」编码进 `AttributeList`。

## `Constant` 层次：从字面量到常量表达式

`Constant` 继承 `User`，但它的不可变性由类本身保证。`llvm/include/llvm/IR/Constant.h:39-44` 的注释写得很直接：常量一旦创建就不再改变，结构等价的常量共享同一个地址，按需创建且不删除。这解释了为什么常量可以安全地跨函数、跨全局初始化器共享，也解释了为什么「修改一个常量」实际上总是「创建一个新常量」。

| 类 | 源码位置 | 关键语义 |
| --- | --- | --- |
| `Constant` | `llvm/include/llvm/IR/Constant.h:44` | 所有常量的根；不可变、结构化唯一 |
| `ConstantData` | `llvm/include/llvm/IR/Constants.h:56` | 无 operand、无 use list，`use_empty()` 恒为真 |
| `ConstantInt` | `llvm/include/llvm/IR/Constants.h:87` | 整数/布尔字面量，内部持有 `APInt` |
| `ConstantFP` | `llvm/include/llvm/IR/Constants.h:420` | 浮点字面量，内部持有 `APFloat` |
| `ConstantPointerNull` | `llvm/include/llvm/IR/Constants.h:716` | 空指针常量，不是 `ConstantInt 0` |
| `UndefValue` | `llvm/include/llvm/IR/Constants.h:1657` | 每次使用可以有不同内容 |
| `PoisonValue` | `llvm/include/llvm/IR/Constants.h:1705` | 传播性更强的「非法值」，触发 UB 的条件不同 |
| `ConstantAggregateZero` | `llvm/include/llvm/IR/Constants.h:514` | 聚合类型全零 |
| `ConstantDataArray` | `llvm/include/llvm/IR/Constants.h:865` | 连续数据数组，常用于字符串和初始化器 |
| `ConstantExpr` | `llvm/include/llvm/IR/Constants.h:1316` | 把一条指令的 opcode 和 operands 塞进常量 |

这张表里最容易被忽略的是 `ConstantData`。它没有 operand，也没有 use list；`llvm/include/llvm/IR/Constants.h:50-56` 明确说「不要检查它的 uses，行为上等价于没有 uses」。所以 `ConstantInt`、`ConstantFP`、`ConstantPointerNull`、`UndefValue`、`PoisonValue`、`ConstantAggregateZero`、`ConstantDataArray` 都不会成为 RAUW 的障碍。真正需要小心的是 `ConstantExpr`，以及继承 `GlobalValue` 的全局对象。

`undef` 和 `poison` 也不是同一种东西。`UndefValue` 的注释说它「没有指定内容」，多次使用可以出现不同 bit pattern（`Constants.h:1647-1653`）；`PoisonValue` 则用于「非法操作数不立即 UB，而是产生 poison」的 speculative 场景（`Constants.h:1690-1696`）。在 review 里把两者混用，通常会把一个可延迟的 poison 误判成立即 UB，或者反过来把一个真正的 UB 当成无害的未定义内容。

### 一个最小常量 IR

```llvm
@str = private constant [6 x i8] c"hello\00"
@zero = global i32 0
@p = global ptr null
@u = global i32 undef
@poison = global i32 poison
```

这段 IR 里，`@str` 是 `ConstantDataArray`，`@zero` 的初始化器是 `ConstantInt`，`@p` 的初始化器是 `ConstantPointerNull`。它们都没有 operand，所以不会像 `ConstantExpr` 那样把别的全局值「挂住」。下面这句则会：

```llvm
@a = global [4 x i32] zeroinitializer
@p = global ptr getelementptr inbounds ([4 x i32], ptr @a, i64 0, i64 2)
```

`@p` 的初始化器是一个 `ConstantExpr`。它是 `@a` 的一个 user，但它不属于任何函数，也不在任何基本块里。这个位置很关键。

## `ConstantExpr` 为什么危险

`ConstantExpr` 的定义在 `llvm/include/llvm/IR/Constants.h:1316`。它用 `Instruction` 的 opcode 表示运算，操作数仍然是 `Constant`。这带来一个非常实用的能力：全局初始化器可以写 `getelementptr`、`bitcast`、`add` 等表达式，而不需要先造一条指令。但它也带来三个 review 风险。

第一，**它把指令语义放进了常量池**。常量池是模块级的，没有基本块、没有支配关系、没有指令迭代器。一个 pass 如果只会遍历 `Function` 里的指令，就会漏掉 `ConstantExpr` 里的使用关系。

第二，**它会阻止全局值被删除或 RAUW**。假设一个 pass 想删除 `@a`，但 `@p` 的初始化器还引用 `@a`。`@a` 的 `use_empty()` 不会是 true，因为 `ConstantExpr` 是它的 user。更麻烦的是，这个 user 不在任何函数里，不能用「遍历函数指令再替换」的办法处理。

第三，**它的 operands 一旦改变，影响范围是全局的**。常量按结构唯一化，两个看起来独立的 `ConstantExpr` 可能指向同一个对象；改一个 operand，所有使用这个常量的地方都会看到新值。这就是为什么 LLVM 提供了 `convertUsersOfConstantsToInstructions`（`llvm/lib/IR/ReplaceConstant.cpp:51`）：先把常量表达式物化成指令，再在函数范围内做 RAUW。

`getAsInstruction()` 是这条路径的核心。声明在 `llvm/include/llvm/IR/Constants.h:1606`，实现从 `llvm/lib/IR/Constants.cpp:3831` 开始。它的注释说得很清楚：返回一条实现相同操作的 `Instruction`，但**不插入任何基本块**。调用者必须自己决定插到哪里。

```cpp
Instruction *ConstInst = CE->getAsInstruction();
ConstInst->insertBefore(*InsertPt->getParent(), InsertPt);
```

这段形状来自 `llvm/lib/IR/ReplaceConstant.cpp:25-31`。注意 `getAsInstruction()` 只负责「造指令」，不负责替换 uses、不负责删除旧常量、不负责更新 DominatorTree。把它当成一键修复会留下半成品。

### 一个 `ConstantExpr` 的形状

```llvm
@a = global [4 x i32] zeroinitializer
@p = global ptr getelementptr inbounds ([4 x i32], ptr @a, i64 0, i64 2)

define ptr @addr() {
entry:
  ret ptr getelementptr inbounds ([4 x i32], ptr @a, i64 0, i64 2)
}
```

实测 `opt -passes=verify` 通过；再跑 `opt -passes=instcombine`，`@p` 和 `ret` 里仍然是常量表达式，只是 GEP 被规范化成 `getelementptr inbounds nuw (i8, ptr @a, i64 8)`。InstCombine 不会因为它在函数里就自动把它变成 `GetElementPtrInst`。review 里看到 `ConstantExpr`，先问「谁会在什么作用域替换它」，不要假设指令遍历会覆盖到。

## `GlobalValue` 家族：符号身份与链接语义

`GlobalValue` 继承 `Constant`（`llvm/include/llvm/IR/GlobalValue.h:49`），这解释了为什么全局变量、函数、别名和 ifunc 都能出现在常量表达式里。它有三个经常一起被误读的维度：

1. **linkage**：这个符号在链接时如何合并、是否可以丢弃。
2. **visibility / dso_local**：这个符号在共享库边界上是否可以被抢占。
3. **section / comdat / thread_local**：这个符号落在哪个段、是否随 COMDAT 一起丢弃、是否线程局部。

`GlobalValue::LinkageTypes` 从 `llvm/include/llvm/IR/GlobalValue.h:52` 开始。常用值如下：

| linkage | 语义 | review 时的典型问题 |
| --- | --- | --- |
| `external` | 外部可见的强定义/声明 | 是否真的由本模块提供定义 |
| `internal` | 模块内可见，目标文件里是 local symbol | 是否可以安全内联、删除 |
| `private` | 只在本模块可直接访问，通常不出现在符号表 | 是否假定没有外部引用 |
| `linkonce` | 链接时合并同名符号，未引用副本可丢弃 | 是否误以为可以像 `linkonce_odr` 一样内联 |
| `weak` | 合并语义类似 `linkonce`，未引用也不保证可丢弃 | 是否覆盖了强定义、是否依赖空值检查 |
| `available_externally` | 只为优化提供定义，不发射到目标文件 | 是否被错误地当成真实定义 |

`available_externally` 的定义在 LangRef 里写得很清楚：它不会被发射到 object file，链接器视角等价于外部声明，存在目的只是让内联和其他优化看到定义；它可以在任何时候被丢弃（`llvm/docs/LangRef.md:229-236`）。`GlobalValue::isAvailableExternallyLinkage` 在 `GlobalValue.h:401`，`hasAvailableExternallyLinkage` 在 `:538`。

这解释了一个常见 review 场景：为什么 `-O2` 之后某个 `inline` 函数从 IR 里消失了？因为它以 `available_externally` 进入模块，内联后不再需要，`GlobalDCE` 删掉它是合法行为。它不是「优化器误删了用户代码」。

### 实测：`available_externally` 在 `-O2` 下被删除

```bash
cd /tmp/llvm-ch78
cat > avail.c <<'C'
inline int square(int x) {
  return x * x;
}

int user(int x) {
  return square(x) + 1;
}
C
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/clang -std=c99 -O1 -Xclang -disable-llvm-passes -S -emit-llvm avail.c -o avail.O1raw.ll
rg -n 'define|available_externally|square|user' avail.O1raw.ll
$B/opt -passes='default<O2>' -S avail.O1raw.ll -o avail.O2.ll
echo '--- after opt O2 ---'
rg -n 'define|available_externally|square|user' avail.O2.ll
```

实测输出：

```text
7:define dso_local i32 @user(i32 noundef %x) #0 {
12:  %call = call i32 @square(i32 noundef %0)
18:define available_externally i32 @square(i32 noundef %x) #1 {
--- after opt O2 ---
7:define dso_local range(i32 -2147483647, -2147483648) i32 @user(i32 noundef %x) local_unnamed_addr #0 {
```

`@square` 的定义在优化后不再存在，`@user` 里只剩内联后的乘法。这里的 `range(...)` 是 Clang/opt 对 `x*x+1` 的推断，其中还牵涉有符号溢出的 UB 前提；本章不展开它，只把它当作「定义被删、调用被内联」的证据。

### 实测：linkage、visibility、TLS、section、comdat 的真实拼写

```bash
cd /tmp/llvm-ch78
cat > globals2.c <<'C'
static int local_static __attribute__((used)) = 1;
__attribute__((visibility("hidden"))) int hidden_global = 2;
__thread int tls_counter = 3;
int sectioned __attribute__((section(".mysec"))) = 4;
C
$B/clang -O1 -S -emit-llvm globals2.c -o globals2.ll
sed -n '1,14p' globals2.ll
```

实测输出：

```llvm
@local_static = internal global i32 1, align 4
@hidden_global = hidden local_unnamed_addr global i32 2, align 4
@tls_counter = dso_local thread_local local_unnamed_addr global i32 3, align 4
@sectioned = dso_local local_unnamed_addr global i32 4, section ".mysec", align 4
```

C++17 inline 变量的实测拼写多一个 comdat：`$inline_var = comdat any`，定义是 `@inline_var = linkonce_odr dso_local ... global i32 7, comdat, align 4`。

这三段输出足以覆盖 review 中最常见的符号问题：`internal` 是模块私有强定义，`hidden` 影响共享库边界，`dso_local` 表示可假设不会被外部抢占，`thread_local` 表示每个线程一份，`section` 改变落段，`comdat` 让链接器按组去重。`GlobalObject` 的 `hasSection()`/`getSection()`/`setSection()` 在 `llvm/include/llvm/IR/GlobalObject.h:111-127`，`getComdat()`/`setComdat()` 在 `:137-140`；`GlobalValue` 的 `ThreadLocalMode` 在 `GlobalValue.h:209`，`isDSOLocal()` 在 `:319`。

`GlobalAlias` 和 `GlobalIFunc` 都只有一条常量 operand：alias 指向 aliasee（`llvm/include/llvm/IR/GlobalAlias.h:29`），ifunc 指向 resolver（`llvm/include/llvm/IR/GlobalIFunc.h:35`）。它们不创建新的数据或函数，只创建新的符号。alias 的 aliasee 必须满足链接约束；如果 alias 或 aliasee 可能在链接/运行时被替换，优化不能把 alias 直接替换成 aliasee（`llvm/docs/LangRef.md:1105-1133`）。ifunc 的 resolver 在 ELF 上由动态链接器在加载时调用（`llvm/docs/LangRef.md:1136-1145`）。review 里看到 alias/ifunc，先确认「这个符号的身份是否可能在链接后改变」，再谈内联或常量折叠。

## 属性：`AttributeList` 与 `Attribute`

`Attribute` 是轻量值对象，`llvm/include/llvm/IR/Attributes.h:107` 的类注释说它「应该按值传递」。`AttributeList` 在 `Attributes.h:609`，它把函数属性、返回属性、参数属性放在同一个索引空间里：

```cpp
enum AttrIndex : unsigned {
  ReturnIndex = 0U,
  FunctionIndex = ~0U,
  FirstArgIndex = 1,
};
```

这段在 `Attributes.h:615-619`。所以 `AttributeList` 不是「一个属性」，而是「一组按位置分组的属性」；`getFnAttrs()`、`getRetAttrs()`、`getParamAttrs()` 分别在 `Attributes.h:962-965` 附近。修改属性返回新的 `AttributeList`，不是原地 mutate。

属性声明在 `llvm/include/llvm/IR/Attributes.td`。本章重点看四个：

| 属性 | 定义 | 语义与 review 风险 |
| --- | --- | --- |
| `memory(...)` | `Attributes.td:161` | 描述函数能读写哪些内存位置；写错会让 AA/优化越界 |
| `noundef` | `Attributes.td:110` | 参数/返回值不得含 undef 或 poison bit；违反是 UB |
| `nonnull` | `Attributes.td:240` | 指针参数/返回值非空；单独违反产生 poison，不是立即 UB |
| `dereferenceable(n)` | `Attributes.td:123` | 指针可安全读写 n 字节；在 addrspace 0 隐含 nonnull，且要求指针 well-defined |

`memory(...)` 的语法在 LangRef `llvm/docs/LangRef.md:2326-2337`：`memory(none)`、`memory(read)`、`memory(write)`、`memory(readwrite)`，以及 `memory(argmem: read)`、`memory(inaccessiblemem: write)` 这类按位置限定的形式。它描述的是函数对内存的影响，不是「函数有没有副作用」这么粗的标签。把 `memory(read)` 写成 `memory(none)`，优化器就可能把两次调用合并、把调用移出循环，或者删掉看似冗余的读取。

`noundef` 的语义在 LangRef `llvm/docs/LangRef.md:1734-1742`：只要值的表示里含 undefined 或 poison bit，行为就是 undefined。它不涉及类型存储引入的 padding。`nonnull` 在 `LangRef.md:1640-1655`：它只约束地址位，指针为空时返回/传入 poison；LangRef 明确建议和 `noundef` 组合，否则「空指针」本身未必立即 UB。`dereferenceable(n)` 在 `LangRef.md:1657-1671`：addrspace 0 下隐含 `nonnull`，并且要求指针 well-defined，因此也隐含 `noundef`。

### 实测：Clang 生成的参数属性和函数属性

```bash
cd /tmp/llvm-ch78
cat > attrs.c <<'C'
extern int use(int *);

__attribute__((nonnull(1))) int read_or(int *p) {
  if (!p)
    return 7;
  return *p;
}

int call_with_null(void) {
  return read_or(0);
}

int caller(int *p) {
  return read_or(p);
}
C
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/clang -O1 -fno-builtin -S -emit-llvm attrs.c -o attrs.O1.ll
sed -n '6,22p' attrs.O1.ll
```

实测输出：

```llvm
; Function Attrs: mustprogress nofree norecurse nosync nounwind willreturn memory(argmem: read) uwtable
define dso_local i32 @read_or(ptr nofree noundef nonnull readonly captures(none) %p) local_unnamed_addr #0 {
entry:
  %0 = load i32, ptr %p, align 4, !tbaa !10
  ret i32 %0
}

; Function Attrs: mustprogress nofree norecurse noreturn nosync nounwind willreturn memory(none) uwtable
define dso_local noundef i32 @call_with_null() local_unnamed_addr #1 {
entry:
  unreachable
}
```

这里有两个信号。第一，`read_or` 的参数带 `nonnull`、`noundef`、`readonly`、`captures(none)`，函数带 `memory(argmem: read)`；第二，`call_with_null` 直接变成 `unreachable`，因为把 null 传给 `nonnull` 参数已经违反了契约。注意这不是「优化器发现了 bug」，而是**前端把源码里的承诺写进 IR，优化器按承诺推导**。源码自己写了 `if (!p)`，又声明 `nonnull`，这两个事实互相矛盾；Clang 甚至给出了 `-Wpointer-bool-conversion` 和 `-Wnonnull` 警告。

命令里的 `-fno-builtin` 会体现在属性组的 `"no-builtins"` 和调用点 `nobuiltin` 上；本段只展开 `nonnull` 的主线。

### 实测：`inferattrs` 只从声明原型推断，不从函数体推断

`InferFunctionAttrsPass` 的源码在 `llvm/lib/Transforms/IPO/InferFunctionAttrs.cpp:21-40`。它遍历 `M.functions()`，条件是 `F.isDeclaration() && !F.hasOptNone()`，然后调用 `inferNonMandatoryLibFuncAttrs` 和 `inferAttributesFromOthers`。这段代码直接回答了「为什么有函数体的函数没被 inferattrs 改」。

```bash
cd /tmp/llvm-ch78
cat > infer2.ll <<'IR'
declare i64 @strlen(ptr)
declare ptr @malloc(i64)
declare void @free(ptr)

define i64 @use(ptr %p) {
entry:
  %n = call i64 @strlen(ptr %p)
  ret i64 %n
}
IR
$B/opt -passes=inferattrs -S infer2.ll -o infer2.out.ll
sed -n '1,20p' infer2.out.ll
```

实测输出：

```llvm
; Function Attrs: mustprogress nocallback nofree nosync nounwind willreturn memory(argmem: read)
declare i64 @strlen(ptr captures(none)) #0

; Function Attrs: mustprogress nofree nounwind willreturn allockind("alloc,uninitialized") allocsize(0) memory(inaccessiblemem: readwrite, errnomem: write)
declare noalias noundef ptr @malloc(i64 noundef) #1

; Function Attrs: mustprogress nounwind willreturn allockind("free") memory(argmem: readwrite, inaccessiblemem: readwrite)
declare void @free(ptr allocptr noundef captures(none)) #2
```

这段输出展示了 `memory(argmem: read)`、`allockind(...)`、`allocsize(0)`、`noalias`、`noundef` 等属性的真实拼写。它们不是「建议」，而是后续 AA、DCE、IPO 的前提。review 属性相关 diff 时，先看这个属性有没有被证明，再看它会不会改变内存效果或空值语义。

### 失败例子：属性撒谎，优化器删掉 null 检查

先看手写 IR。这个函数自己检查 null，但参数上写了 `nonnull`：

```llvm
define i32 @read_or(ptr nonnull %p) {
entry:
  %isnull = icmp eq ptr %p, null
  br i1 %isnull, label %null, label %deref

null:
  ret i32 7

deref:
  %v = load i32, ptr %p, align 4
  ret i32 %v
}
```

跑 `opt -passes='default<O1>'` 后，实测输出：

```llvm
define i32 @read_or(ptr nofree nonnull readonly captures(none) %p) local_unnamed_addr #0 {
entry:
  %v = load i32, ptr %p, align 4
  ret i32 %v
}
```

null 分支和检查都消失了。这个例子里，`nonnull` 是**手写进去的谎话**；如果它来自一个 pass，那个 pass 必须能证明「所有调用点都不会传 null」，否则就是把 UB 引进 IR。注意单有 `nonnull` 时，传 null 得到 poison；当这个 poison 被用于 load 的地址时，才落成 UB。若同时加了 `noundef`，契约更硬，违反的路径更容易被优化器直接判成不可达。

review 这类 diff 时，不要只看「属性名对不对」。要沿着调用点问三件事：

1. 这个属性是在函数定义、声明、还是某个调用点添加的？
2. 添加它的 pass 有没有 DominatorTree/AssumptionCache/AA 的证据链？
3. 如果属性不成立，产生的是 poison 还是立即 UB？下游有没有 `freeze` 或检查把它兜住？

## `CallingConv`、varargs 与 `musttail`

`CallingConv` 的命名空间从 `llvm/include/llvm/IR/CallingConv.h:21` 开始，`ID` 是 `unsigned`（`:24`）。默认 C 约定是 `C = 0`（`:34`），`Fast = 8`（`:41`），target-specific 约定从 `FirstTargetCC = 64`（`:94`）开始。源码注释里有一句关键约束：默认 C 约定是唯一支持 varargs 的调用约定；通用 LLVM 约定不支持 varargs，并且要求调用者和被调用者原型完全匹配（`CallingConv.h:26-40`）。

`CallBase` 的 `isVarArg()` 来自函数类型，而不是调用点单独的标志。varargs 函数在 IR 里写成 `i32 (i32, ...)`；调用时多出来的实参类型由调用点决定。review 里看到 `...`，先确认目标 ABI 和调用约定，再看 `va_start`/`va_arg` 的 lowering 是否一致。

`musttail` 的约束集中在 `Verifier::verifyMustTailCall`（`llvm/lib/IR/Verifier.cpp:4533`）。源码检查顺序如下：

- 不能是 inline asm（`Verifier.cpp:4534`）。
- 调用者和被调用者的 varargs 属性必须一致（`:4539`）。
- 返回类型必须一致（`:4541`）。
- 调用约定必须一致（`:4545`）。
- 调用后面必须紧跟 `ret`（`:4554`）。
- `ret` 必须返回调用结果、`undef` 或 void（`:4557`）。
- `tailcc`/`swifttailcc` 还有额外的 ABI 属性限制（`:4560-4578`）。

LangRef 的规则更完整：`musttail` 调用必须紧邻 `ret`，调用约定必须匹配，callee 的 varargs 属性必须与 caller 一致，返回类型不能自动转成 `sret`；非 `swifttailcc`/`tailcc` 时，所有 ABI-impacting 属性（`sret`、`byval`、`inreg`、`returned`、`inalloca`）和原型都必须匹配（`llvm/docs/LangRef.md:14327-14355`）。

### 实测：返回类型不匹配的 `musttail` 被 Verifier 拒绝

```bash
cd /tmp/llvm-ch78
cat > musttail.ll <<'IR'
declare i32 @callee(i32)

define i32 @bad_ret(i32 %x) {
entry:
  %r = musttail call i64 @callee(i32 %x)
  ret i32 %x
}
IR
$B/opt -passes=verify -disable-output musttail.ll 2>&1 | head -20
```

实测输出：

```text
cannot guarantee tail call due to mismatched return types
  %r = musttail call i64 @callee(i32 %x)
/home/ubuntu/llvm/builds/pr-lane/bin/opt: musttail.ll: error: input module is broken!
```

这条错误对应 `Verifier.cpp:4541` 的 `Check(CallerTy->getReturnType() == CalleeTy->getReturnType(), "cannot guarantee tail call due to mismatched return types", &CI);`。review 里看到 `musttail`，不要只检查「后面有没有 ret」；varargs、返回类型、调用约定和 ABI 属性任何一项不一致，都可能让后端无法保证栈帧复用。

## 实测：bitcode 往返保留属性与 metadata

```bash
cd /tmp/llvm-ch78
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/llvm-as attrs.O1.ll -o attrs.bc
$B/llvm-dis attrs.bc -o attrs.roundtrip.ll
diff -u attrs.O1.ll attrs.roundtrip.ll | head -20
```

实测 `diff -u` 只有一行差异：`; ModuleID = 'attrs.c'` 变成 `; ModuleID = 'attrs.bc'`。

diff 只有 `ModuleID` 注释不同。`nonnull`、`noundef`、`memory(argmem: read)`、`!tbaa` 和函数属性组都完整保留。这个实验的意义不是「bitcode 一定无损」；它的意义是给你一个低成本检查：如果 review 怀疑属性在序列化/反序列化中丢了，先做 `llvm-as | llvm-dis`，再用 `diff` 看差异是否只在注释或 metadata 编号。
## Review 检查表

看到一个涉及常量、全局值或属性的 diff，按这个顺序问：

1. 这个 `ConstantExpr` 会不会成为 RAUW/删除的障碍？如果会，改动是否调用了 `convertUsersOfConstantsToInstructions` 或等价逻辑？
2. `GlobalValue` 的 linkage 是否支持这个优化？`available_externally` 是否被误当成真实定义？`linkonce` 是否被误当成 `linkonce_odr`？
3. visibility/dso_local 是否影响抢占？在共享库边界上，能否假设这个符号不会被替换？
4. `section`/`comdat`/`thread_local` 是否改变了 ABI 或链接行为？
5. 属性是在定义、声明还是调用点添加的？证据链是什么？
6. `nonnull` 是否和 `noundef`/`dereferenceable` 组合？违反时是 poison 还是 UB？
7. `memory(...)` 是否覆盖了函数实际访问的所有位置？有没有漏掉 `inaccessiblemem`/`errnomem`？
8. `musttail` 的 varargs、返回类型、调用约定、ABI 属性是否全部匹配？
9. 测试是否覆盖了属性不成立时的边界，而不只是 happy path？
## 踩雷集锦

1. **把 `ConstantData` 当成有 use 的节点**。`ConstantInt`、`ConstantFP` 没有 operand，也没有 use list；检查它们的使用者会得到空结果。真正要追的是 `ConstantExpr` 和 `GlobalValue`。
2. **以为 `getAsInstruction()` 会自动替换 uses**。它只造一条未插入的指令。替换、插入、删除旧常量、更新分析结果，都是调用者的责任。
3. **把 `available_externally` 当成「弱定义」**。它不会发射到 object file，允许被丢弃；它的价值是让优化器看到定义，不是提供一个链接时可用的实体。
4. **看到 `nonnull` 就认为「空指针一定 UB」**。单独 `nonnull` 产生 poison；只有 poison 被用于触发 UB 的操作，或者叠加 `noundef` 等更硬契约，才会落成 UB。review 时要把「违反契约」和「立即 UB」分开。
5. **把 `musttail` 当成普通 `tail`**。`tail` 是提示，`musttail` 是正确性条件；varargs、返回类型、调用约定、ABI 属性任何一项不匹配，Verifier 都会拒绝。
## 本章重点整理

- `Constant` 不可变、按结构唯一化；`ConstantData` 没有 operand/use list，`ConstantExpr` 有 operand，会把全局值挂住。
- `ConstantExpr` 的危险不在「它是常量」，而在「它不在任何基本块里」。RAUW 前先用 `getAsInstruction()` 物化，再在正确作用域替换。
- `GlobalValue` 同时是 `Constant` 和符号；linkage、visibility、dso_local、TLS、section、comdat 分别回答不同问题，不能用一个「是不是 static」概括。
- `available_externally` 只服务优化，不发射；`-O2` 删除它是合法且常见的。
- `AttributeList` 是按位置分组的属性集合；`memory(...)`、`noundef`、`nonnull`、`dereferenceable` 都会变成优化前提。
- 属性写错不是「少一条优化」，而是可能把 UB 引进 IR；review 要追证据链。
- `musttail` 是硬约束集合，varargs、返回类型、调用约定和 ABI 属性必须一起检查。
## 自我检核

- [ ] 不看笔记，能画出 `Constant` → `ConstantData`/`ConstantExpr`/`GlobalValue` 的层次，并说明哪一类有 use list
- [ ] 能解释为什么 `ConstantExpr` 会让 `@g->use_empty()` 为 false，以及 `getAsInstruction()` 解决的是哪一步
- [ ] 能说出 `internal`、`private`、`linkonce`、`weak`、`available_externally` 在链接语义上的至少两个差异
- [ ] 能解释 `hidden`、`dso_local`、`thread_local` 分别影响什么，不要把三者混成「可见性」
- [ ] 能说出 `nonnull` 单独违反时产生什么，以及为什么 LangRef 建议和 `noundef` 组合
- [ ] 能解释 `dereferenceable(n)` 为什么在 addrspace 0 隐含 `nonnull` 和 `noundef`
- [ ] 能说出 `inferattrs` 为什么只改声明、不改有函数体的定义
- [ ] 能区分「属性撒谎导致 UB」和「优化器有 bug」这两种 review 结论

## 延伸阅读

- **[LLVM Language Reference: Linkage Types](https://llvm.org/docs/LangRef.html#linkage-types)**
  - **读哪里**：`private`、`internal`、`available_externally`、`linkonce`、`weak` 五段
  - **能学到什么**：把「链接时合并」「是否发射」「能否丢弃」分开，而不是只记关键字
  - **前提**：知道 object file 的 local/global symbol 大致是什么
- **[LLVM Language Reference: Attributes](https://llvm.org/docs/LangRef.html#parameter-attributes)**
  - **读哪里**：`nonnull`、`noundef`、`dereferenceable`、`memory(...)` 四节，以及函数属性里的 `memory`
  - **能学到什么**：每个属性违反时到底产生 poison 还是 UB；这是 review 属性 diff 的判据
  - **前提**：读过 Ch 5 的指针类型和 Ch 9 的 Verifier 边界
- **[LLVM Language Reference: Call Instruction](https://llvm.org/docs/LangRef.html#call-instruction)**
  - **读哪里**：`musttail` 的 additional rules 和 `tail`/`musttail` 的区别
  - **能学到什么**：为什么尾调用约束是 ABI 问题，不只是优化提示
  - **前提**：知道调用约定和 varargs 的基本概念

→ [Ch 8 — `Metadata` 与调试信息：`MDNode`、`DILocation`、`!tbaa`、`assume`](./08-metadata-and-debuginfo.md)
