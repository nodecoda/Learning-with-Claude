# Ch 10 — `ADT` 与 `Support`：为什么 LLVM 不用 STL 的那一半

> **目标**：把 LLVM 自己的容器、字符串视图、错误类型和调试基础设施读成一套有取舍的设计，而不是一堆「和 STL 差不多」的类。学完你能在 review 里指出「这个 `StringRef` 会悬垂」「这个 `DenseMap` 引用在 insert 后失效」「这个 `Expected<T>` 没被消费」，并知道每个判断对应哪个头文件的哪一行。

> **环境**：LLVM 24.0.0git，源码 `/home/ubuntu/llvm/llvm-project`，`main` @ `7c7a9072f736`（2026-11-20）；实验构建 `/home/ubuntu/llvm/builds/pr-lane`（`RelWithDebInfo`，assertions ON）。本章所有 C++ 实验都用 `llvm-config --cxxflags --ldflags --libs support` 编译并真实运行，输出见各节与「动手实验」。

## 为什么需要这个？

Review 一个 LLVM PR 时，最容易漏掉的不是算法，而是**类型背后的生命周期和失效规则**。这三类 comment 在真实 review 里反复出现：

- 「`StringRef Name = getName();` 之后 `getName()` 的临时对象已经没了。」
- 「你在循环里 `Map[Key] = ...`，上一轮的 `auto &V = Map[Key]` 已经指向被 rehash 丢掉的 bucket。」
- 「这个函数返回 `Expected<T>`，调用方 `(void)call();` 丢掉了错误，assertion 构建会直接 abort。」

这些都不是风格问题，而是会让程序在 assertions 构建里崩、在 release 构建里静默读错数据的缺陷。本章的目标是把「LLVM 的 ADT 和 STL 哪里不一样、为什么不一样」变成你能在 diff 上快速匹配的模式。

## 先建立直觉

LLVM 的 `ADT`（Abstract Data Types）不是「重新发明 STL」，而是**针对编译器负载重写的一层容器**。编译器负载有三个特点：对象生命周期短、分配次数极多、很多容器在 99% 的情况下只有几个元素。

```
llvm/ADT/*.h        SmallVector  DenseMap  ilist  ArrayRef  StringRef  Twine
      │ 只依赖
      ▼
llvm/Support/*.h    Error/Expected  raw_ostream  CommandLine  Statistics  BumpPtrAllocator
      │ 再往下
      ▼
lib/Support/*.cpp   上述类型的实现
```

这条依赖方向是硬约束：`ADT` 不能反向依赖 `IR`，`Support` 不能依赖 `CodeGen`。Ch 12 会把这条规则展开成 CMake 层面的检查；本章只关心「为什么这些类长这样」。

## 设计约束：LLVM 为什么不用 STL 全覆盖

先看构建系统自己给的答案。`llvm-config` 打印的编译参数里有两个关键开关：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
$B/llvm-config --cxxflags
```

实测（截断到关键部分）：

```text
-I/home/ubuntu/llvm/trees/pr-lane/llvm/include -I/home/ubuntu/llvm/builds/pr-lane/include -std=c++17  -D_GNU_SOURCE -D_DEBUG -D_GLIBCXX_ASSERTIONS ... -fno-exceptions -fno-rtti
```

`-fno-exceptions -fno-rtti` 不是构建选项的偶然组合，而是编码规范写死的：`llvm/docs/CodingStandards.md:573` 的标题就是 “Do not use RTTI or Exceptions”，理由是 “reduce code and executable size”。这意味着：

| 约束 | 直接后果 | 本章对应的 ADT |
| --- | --- | --- |
| 无异常 | `std::vector::at`、`std::string` 的分配失败路径不能靠 `throw` 表达；错误必须显式返回 | `Error` / `Expected<T>` |
| 无 RTTI | 不能用 `dynamic_cast` 做类型分派；LLVM 用 `isa<>`/`dyn_cast<>` 的 hand-rolled RTTI | `Cast.h`（Ch 3 已讲） |
| allocator 控制 | 编译器的分配热点需要 bump allocator、arena、按对象类型复用 | `BumpPtrAllocator`（`llvm/Support/Allocator.h:69`） |
| 编译期成本 | 模板实例化要可控，头文件不能拖进整个 STL 算法库 | `SmallVector`、`ArrayRef` 这类薄封装 |
| 迭代器稳定性 | IR 对象在 pass 里被插入/删除，容器不能因为一次插入就让所有迭代器失效 | `ilist`（侵入式链表） |

「无异常」这一条值得多说一句：没有异常不等于「错误被忽略」，而是**错误必须出现在类型里**。`Expected<T>` 就是把「可能失败」编码进返回类型的办法；它的代价是调用方必须显式处理，编译器（在 assertion 构建里）会替你抓住漏处理的情况。这一点在本章最后一节用真实崩溃演示。

不是「全部用 STL，只在热点换」，因为有些差异是**语义问题**而非性能问题：`std::vector` 扩容让迭代器全部失效，而 IR 容器需要「插入一个指令，其他指令地址不变」；`std::unordered_map` 的迭代顺序绑定 hash 实现，而 LLVM 需要可复现输出，所以有 `MapVector`/`SetVector`；`std::string` 的分配行为不可控，而 `StringRef` 干脆不拥有内存。这三条分别给出了 `ilist`、插入序容器和视图类的存在理由。

## `SmallVector` / `SmallString` / `SmallPtrSet`：inline capacity 的代价

`SmallVector<T, N>` 的第一个模板参数是元素类型，第二个是**内联容量**：前 N 个元素直接放在对象内部，不碰堆。`llvm/docs/ProgrammersManual.md:1516` 起的 `llvm/ADT/SmallVector.h` 一节给的定位是「usually small」的 vector，例如一个基本块的前驱/后继通常少于 8 个。

代价写在类型大小里。实测：

```cpp
llvm::SmallVector<int, 2> V;
std::printf("sizeof=%zu\n", sizeof(V));
```

```text
sizeof(SmallVector<int,2>)=24
```

24 字节 = 两个 `int` 的内联存储（8）+ 指针/大小/容量（16）。换成 `SmallVector<int, 64>`，对象本身就是 272 字节；把它放进 `std::vector` 或按值传参，复制成本立刻变成主要开销。头文件对此有硬性保护：`llvm/include/llvm/ADT/SmallVector.h:1201` 的 `CalculateSmallVectorDefaultInlinedElements` 把「默认 N」设计成让 `sizeof(SmallVector<T>)` 接近 64 字节，并且对 `sizeof(T) > 256` 直接 `static_assert` 失败，逼你显式写明 `SmallVector<T, N>`。

增长策略也不是常见实现的 1.5x/2x。`llvm/lib/Support/SmallVector.cpp:107` 写的是：

```cpp
size_t NewCapacity = 2 * OldCapacity + 1; // Always grow.
```

所以从内联容量 2 开始 push，第三次会直接跳到 5：

```text
push 0: size=1 cap=2 data=0x7ffc13137180 moved=no
push 1: size=2 cap=2 data=0x7ffc13137180 moved=no
push 2: size=3 cap=5 data=0x558bf4bcdec0 moved=yes
push 3: size=4 cap=5 data=0x558bf4bcdec0 moved=no
push 4: size=5 cap=5 data=0x558bf4bcdec0 moved=no
```

`data` 从栈地址（`0x7ffc...`）跳到堆地址（`0x558b...`）的那一行，就是「溢出到 heap」的时刻。读 review 时，如果你看到有人在热循环里反复构造 `SmallVector<T, 0>` 然后 push 两次，问一句「为什么不用 `SmallVector<T, 2>`」通常比讨论算法更有价值。

### 传参用 `SmallVectorImpl<T>`，不是 `SmallVector<T, N>`

`SmallVector<T, N>` 继承自 `SmallVectorImpl<T>`（`SmallVector.h:601` 定义 `SmallVectorImpl`，`SmallVector.h:1266` 起是具体类）。这层继承的唯一目的就是让你在函数签名里写：

```cpp
void collect(llvm::SmallVectorImpl<Foo> &Out);   // 接受任意 N
void broken(llvm::SmallVector<Foo, 2> &Out);     // 只接受 N == 2
```

官方文档把这个写成明确建议（`ProgrammersManual.md:1561` 起）：「Prefer to use `ArrayRef<T>` or `SmallVectorImpl<T>` as a parameter type」，并给了 `hardcodedSmallSize` 被标成 DISCOURAGED 的例子。实测中 `Sum(llvm::SmallVectorImpl<int> &)` 可以接收 `SmallVector<int, 2>`：

```text
sum=10 sizeof(SmallVector<int,2>)=24
```

一个例外要记住：`SmallVectorImpl` 的移动构造会丢失具体类型的 N（`SmallVector.h:153` 的注释说 “The inline capacity is unknown here”），所以不要按值返回 `SmallVectorImpl`，也不要在接口里暴露它作为返回值。

`SmallString<N>` 是 `SmallVector<char, N>` 的子类（`llvm/ADT/SmallString.h:27`），只多了字符串习惯用法，`SmallPtrSet<PtrType, N>` 则是小集合专用：小模式下用线性扫描，元素数超过 N 才建 hash 表。`SmallPtrSet` 的 `SmallSize` 没有默认值，必须手写，这一点和 `SmallVector` 不同。

## `ArrayRef` / `StringRef` / `Twine`：非拥有视图的生命周期

这三个类的共同点是**不拥有数据**，只是「指针 + 长度」或「表达式树」的薄包装。头文件把风险写在第一段注释里：

- `llvm/ADT/ArrayRef.h:34`：“This class does not own the underlying data... it is not in general safe to store an ArrayRef.”
- `llvm/ADT/StringRef.h:54`：“it is not in general safe to store a StringRef.”
- `llvm/ADT/Twine.h:38`：“A Twine is not intended for use directly and should not be stored... pointers to temporary stack objects which may be deallocated at the end of a statement.”

`ArrayRef` 和 `StringRef` 都带 `LLVM_GSL_POINTER` 标注（`ArrayRef.h:44`、`StringRef.h:57`），构造函数参数还有 `LLVM_LIFETIME_BOUND`（`ArrayRef.h:73`、`StringRef.h:94`），让 clang 在明显悬垂时给 warning。但 warning 不是证明，下面这个函数能编译通过：

```cpp
static llvm::StringRef longName() {
  std::string Tmp(64, 'x'); // heap buffer, freed at return
  return Tmp;               // BUG
}
```

实测（ASan 构建）：

```text
==3873347==ERROR: AddressSanitizer: heap-use-after-free on address 0x507000000100
READ of size 1 at 0x507000000100 thread T0
    #0 0x55609d2605cc in llvm::StringRef::operator[](unsigned long) const .../ADT/StringRef.h:234
    #1 0x55609d25fc12 in main .../exp2_stringref.cpp:20
0x507000000100 is located 0 bytes inside of 65-byte region
freed by thread T0 here:
    #0 ... in operator delete(void*, unsigned long)
```

换成长度 3 的短字符串（落进 `std::string` 的 SSO，字节在栈上），同一个 ASan 构建**没有报错**，只是读出一个错误值：

```text
size=3 first=4294967287
owned: a1
```

这就是最危险的一类：没有崩溃、没有 sanitizer 报告，只是「名字比较失败」或「打印出乱码」。`4294967287` 是 `0xFFFFFF87`，来自把栈上残留字节按 `unsigned` 解释。ASan 抓不到 SSO 悬垂，因为那块内存在函数返回后仍然可读；能抓到的只有堆分配的情况。**不要把「ASan 没报」当成「生命周期正确」的证据。**

`Twine` 的规则更窄：它只能在**一个完整表达式内**使用。它是延迟拼接的 rope，叶子节点存的是指向临时对象的指针（`Twine.h:38`）。安全的写法是立刻落成拥有内存的类型：

```cpp
std::string Owned = (llvm::Twine("a") + llvm::Twine(1)).str(); // 安全
```

不安全的写法是把它存起来或返回：

```cpp
const llvm::Twine &Bad = llvm::Twine("a") + llvm::Twine(1); // 表达式结束即悬垂
```

实测中 `.str()` 的结果是 `owned: a1`。API 设计上，`StringRef`/`Twine` 的正确用法是「只在参数位置接收」，需要保存就 `.str()` 或 `std::string`。

## `DenseMap` / `DenseSet` / `StringMap` / `SetVector` / `MapVector`

这五个容器的区别不在接口，而在**三个语义维度**：hash 要求、迭代顺序、插入时的失效行为。

| 容器 | 底层 | 迭代顺序 | 插入/删除的影响 |
| --- | --- | --- | --- |
| `DenseMap<K,V>` | 开放寻址 + 线性探测 | bucket 顺序（hash 决定） | rehash 后所有 bucket 地址和迭代器失效 |
| `DenseSet<T>` | `DenseMap<T, Empty>` 包装 | 同上 | 同上 |
| `StringMap<V>` | 线性探测，key 存在 entry 里 | 不保证确定（`ProgrammersManual.md:2286`） | 插入/删除使迭代器失效（同节 2289） |
| `SetVector<T>` | `SmallVector` + `DenseSet` | **插入序** | 删除是线性时间（文档 2163） |
| `MapVector<K,V>` | `SmallVector<pair>` + `DenseMap<K,unsigned>` | **插入序** | 底层 vector 扩容会让引用失效 |

`DenseMap` 的 key 需要 `DenseMapInfo<T>` 提供 `getHashValue()` 和 `isEqual()`（`llvm/ADT/DenseMapInfo.h:51` 的注释列的就是这两个）。这一版的实现**不用空 key / tombstone 哨兵**，而是在 bucket 数组后面跟一段 used 位图（`llvm/ADT/DenseMap.h:145`：「Buckets and the used array share one allocation」），删除只清 used bit。网上和旧教材里「必须实现 `getEmptyKey()`/`getTombstoneKey()`」的规则属于旧版 DenseMap；在 `7c7a9072f736` 上 `rg getEmptyKey llvm/include` 只在 `CachedHashString.h` 命中，扩 key 类型前先确认自己读的是哪一版。

迭代顺序不是实现细节，实测差异很明显：

```text
DenseMap:  0 7 2 4 6 1 3 5
StringMap: delta bravo charlie alpha
SetVector: 3 1 2
MapVector: 3 1 2
```

`DenseMap` 按 bucket 走，`StringMap` 按 hash 位置走，只有 `SetVector`/`MapVector` 保证插入序。所以任何「输出必须可复现」的地方（诊断信息、`-print-after` 的稳定 diff、序列化）都不该直接迭代 `DenseMap`；要么排序，要么换 `MapVector`。

失效行为可以用一个最小程序打出来：

```cpp
llvm::DenseMap<int, int> M;
for (int I = 0; I < 4; ++I) M[I] = I * 100;
int &Ref = M[2];        // 指向 bucket 内部
M.reserve(1024);        // 触发 rehash
llvm::outs() << Ref;    // BUG
```

实测（ASan）：

```text
==3873728==ERROR: AddressSanitizer: heap-use-after-free on address 0x5160000000d4
READ of size 4 at 0x5160000000d4 thread T0
    #0 0x56099d59383b in main .../exp3_densemap.cpp:14
0x5160000000d4 is located 84 bytes inside of 520-byte region
freed by thread T0 here:
    #1 ... in llvm::densemap::detail::growRelocatable(...) .../lib/Support/DenseMap.cpp:97
```

注意栈帧里的 `growRelocatable`：rehash 真的把旧 bucket 数组 `delete` 掉了，不是原地搬。这解释了为什么 `DenseMap` 的 reference、pointer、iterator 在**任何可能触发扩容的插入**之后都不能继续用。`DenseMap` 的 `find()` 返回的迭代器同样如此。

## `ilist` / `iplist` / `simple_ilist`：侵入式链表

`Instruction` 能在 O(1) 时间内从基本块里摘掉，不是因为它记录了前驱后继，而是因为**它自己就是链表节点**。看继承列表：

```cpp
// llvm/include/llvm/IR/Instruction.h:63
class Instruction : public User,
                    public ilist_node_with_parent<Instruction, BasicBlock,
                                                  ilist_iterator_bits<true>,
                                                  ilist_parent<BasicBlock>> {
```

`ilist_node_with_parent`（`llvm/ADT/ilist_node.h:316`）继承 `ilist_node`（同文件 208），后者内含 `prev`/`next` 指针。删除时不需要遍历找前驱：

```cpp
// llvm/lib/IR/Instruction.cpp:95
void Instruction::removeFromParent() {
  handleMarkerRemoval();
  getParent()->getInstList().remove(getIterator());
}
```

`remove(getIterator())` 只改两个邻居的指针，然后把节点从链表摘掉；`eraseFromParent()`（同文件 109）在此之上调用 `ilist_alloc_traits<Instruction>::deleteNode`（`Instruction.h:1134` 调 `V->deleteValue()`）真正释放对象。

实测（构造一个函数，删掉中间两条死指令）：

```text
before: size=4
  a=0x55dc20c482c0 dead1=0x55dc20c48370 dead2=0x55dc20c48420
after erase dead1: size=3 next=dead2
after erase dead2: size=2
  a=0x55dc20c482c0 (address unchanged)
```

`a` 的地址在两次删除前后完全不变，这是 `std::vector<unique_ptr<Instruction>>` 做不到的：后者删除中间元素会移动后面的元素（或至少移动指针数组）。IR 里大量代码持有 `Instruction *`，地址稳定是硬需求。

三个名字的关系要分清：

- `simple_ilist<T>`（`llvm/ADT/simple_ilist.h:78`）是「没有 traits 的最小侵入式链表」，节点的增删不会通知外部。
- `ilist<T, Traits>`（`llvm/ADT/ilist.h`）在 `simple_ilist` 之上加了 traits 回调：插入/删除时通知 owner（`addNodeToList`/`removeNodeFromList`，见 `llvm/IR/SymbolTableListTraits.h:99`）。`BasicBlock` 的指令列表、`Function` 的基本块列表都用这层。
- `iplist` 是 `ilist` 的历史别名（`ProgrammersManual.md:1704` 明确说 “An `ilist` and an `iplist` are `using` aliases to one another and the latter only currently exists for historical purposes.”）。新代码不要写 `iplist`。

review 推论：`for (auto I = BB.begin(); I != BB.end(); ++I) { ... I = BB.erase(I); }` 要确认 `erase` 的返回值被用上；「先收集指针、再统一删除」要确认删除后没有别处还持有已释放的 `Instruction *`。侵入式链表不检查悬垂。

## `APInt` / `APFloat`：任意位宽与溢出语义

`APInt` 是任意位宽的整数，位宽是运行时值（`APInt(128, 1)`）。它的运算语义和 C 的 `int` 不同：**溢出是定义好的回绕**，不是 UB；有符号/无符号由调用者选择的运算函数决定（`sdiv` vs `udiv`，`APInt.h:66` 的注释专门解释了这一点）。

最容易写错的是「把 APInt 挤进 `uint64_t`」的两条路：

```cpp
llvm::APInt A(128, 1);
A <<= 64;   // 2^64，需要 65 个有效位
A.getLimitedValue();   // 饱和到 UINT64_MAX
A.getZExtValue();      // assertion
```

实测：

```text
bitwidth=128 active=65 value=0x10000000000000000
getLimitedValue() = 18446744073709551615 (UINT64_MAX=18446744073709551615)
```

`getLimitedValue()` 的实现是 `ugt(Limit) ? Limit : getZExtValue()`（`APInt.h:471`），默认 `Limit = UINT64_MAX`。它**饱和**，不截断：一个 65 位的值会变成 `UINT64_MAX`，而不是低 64 位。如果下游逻辑把 `UINT64_MAX` 当成「合法最大值」，就会得到一个错误的边界判断。

想要低 64 位，得显式 `trunc(64)`；想要「不溢出才取」，用 `tryZExtValue()`（`APInt.h:1572`）而不是 `getLimitedValue()`。直接调 `getZExtValue()` 在超过 64 位时触发 assertion：

```text
exp4: .../ADT/APInt.h:1563: uint64_t llvm::APInt::getZExtValue() const:
Assertion `getActiveBits() <= 64 && "Too many bits for uint64_t"' failed.
```

另一个边界：`APInt(32, 0x100000005ULL)` 在 assertions 构建里直接 abort：

```text
exp4: .../ADT/APInt.h:127: llvm::APInt::APInt(unsigned int, uint64_t, bool, bool):
Assertion `llvm::isUIntN(BitWidth, val) && "Value is not an N-bit unsigned value"' failed.
```

release 构建里这个 assertion 被编译掉，构造函数直接执行 `U.VAL = val`；无符号路径不调用 `clearUnusedBits()`（`APInt.h:127` 的实现），于是 `BitWidth=32` 的对象内部留着 33 位的值，违反「高位为零」的不变量——不是干净的截断。要显式截断得传 `implicitTrunc=true`，实测 `APInt(32, 0x100000005, false, true)` 得到 `value=0x5 zext=5 active=3`。这就是 review 要求 assertions 构建的具体理由：同一段代码在 release 下不崩，但对象已经违反自己的不变量。

`APFloat` 对应任意精度浮点（`llvm/ADT/APFloat.h:1074`），舍入模式、NaN payload、非规格化数都显式建模。常量折叠里看到 `APFloat::add`，要检查舍入模式参数（`APFloat.h:1431` 附近说明 underflow/overflow 如何报告），不要默认「浮点加法就是 IEEE 默认舍入」。

## `Error` / `Expected` / `cantFail`：错误必须被消费

LLVM 的错误模型是「错误是值，不是控制流」。`Error` 表示可能失败但无返回值的操作；`Expected<T>` 表示可能失败且成功时有 `T`。头文件的规则写得很硬：

- `llvm/Support/Error.h:156`：“*All* Error instances must be checked before destruction, even if they are success values.”
- `Expected<T>` 的析构函数调用 `assertIsChecked()`（`Error.h:565`），未检查时打印 “Expected<T> must be checked before access or destruction.”（`Error.h:714`）。

实测三种路径：

```text
=== ok ===
value=42
=== fail ===
error: bad input
=== drop ===
Expected<T> must be checked before access or destruction.
Unchecked Expected<T> contained error:
bad input
Aborted (core dumped)
```

`drop` 分支的代码是 `auto V = parse(true); (void)V;`——看起来「我明确忽略了」，但 `(void)` 不构成检查。这是 review 里最常见的错误处理缺陷：调用方以为 `(void)` 是「有意忽略」，实际是「未消费」。

正确的消费方式有三种：

1. `if (!V) return V.takeError();` —— 向上传播。
2. `if (auto Err = V.takeError()) { ... }` —— 就地处理。
3. `consumeError(V.takeError())` —— 明确丢弃（只用于「这个错误确实无关」的场合）。

`cantFail`（`Error.h:769` / `797`）是「我确信这里不会失败」的断言式解包：失败时 `llvm_unreachable`。它适合「调用方已经检查过前置条件」的场景，不适合「我不想处理错误」。review 时看到新增的 `cantFail`，要问一句「为什么这里可以证明不会失败」，答不上来就该改成正常的错误传播。

## `raw_ostream` / `formatv` / `CommandLine` / `Statistics`

这四样是写工具和调试输出的基础设施，review 里出现的频率比想象中高。

`raw_ostream`（`llvm/Support/raw_ostream.h:53`）是 LLVM 的输出抽象，`outs()`、`errs()`、`dbgs()` 都是它的实例。它比 `std::ostream` 快，原因是去掉了 locale、虚函数链和格式化状态；代价是没有 `<< std::setw` 那一套，需要格式化时用 `formatv`。

`formatv`（`llvm/Support/FormatVariadic.h:247`）用 `{0}`、`{1:x}` 这种占位符，返回一个可流式输出的对象。实测：

```text
row     0: 0x0
row     1: 0xff
row     2: 0x1fe
info    0: 0x0
info    1: 0xff
```

第一段来自 `formatv("{0,-6}{1,3}: {2:x}\n", Label, I, I * 255)`：`-6` 左对齐 6 列，`3` 右对齐 3 列，`x` 十六进制。`formatv` 的对象只能在表达式内用（和 `Twine` 同类风险），要保存就 `.str()` 或 `.sstr<N>()`。

`CommandLine`（`llvm/Support/CommandLine.h:1472` 的 `cl::opt`）把 flag 定义成全局对象，`ParseCommandLineOptions`（同文件 71）在 `main` 里解析。实测错误路径：

```text
exp7: Unknown command line argument '-nope'.  Try: './exp7 --help'
exp7: Did you mean '--count'?
```

这条「Did you mean」是 `CommandLine` 自带的能力，review 新工具时可以直接要求作者给出 `--help` 输出，判断 flag 是否自解释。

`Statistics`（`llvm/ADT/Statistic.h`）是「计数器 + 按需输出」的基础设施：`STATISTIC(NumRows, "...")` 展开成 `TrackingStatistic`（`Statistic.h:50`），计数总会累加，只有 `-stats` 或 `EnableStatistics()` 才把它加入打印列表（`Statistic.cpp:98` 的 `RegisterStatistic`）。实测 `opt` 的输出：

```text
===-------------------------------------------------------------------------===
                          ... Statistics Collected ...
===-------------------------------------------------------------------------===

3 instcombine       - Number of insts combined
1 instcombine       - Number of functions with two iterations
2 instcombine       - Number of instruction combining iterations performed
1 last-run-tracking - Number of LastRunTracking queries
```

review 性能相关 PR 时，`-stats` 是最便宜的「这个 pass 到底做了什么」的证据；比读代码猜次数可靠。

## 动手实验：复现本章的所有输出

所有实验在 `~/llvm-lab/ch10`，编译命令统一为：

```bash
B=/home/ubuntu/llvm/builds/pr-lane/bin
CF=$($B/llvm-config --cxxflags)
LF=$($B/llvm-config --ldflags --libs support)
g++ -O0 -g $CF exp1_smallvector.cpp -o exp1 $LF
```

第一次运行会遇到动态库找不到：

```text
./exp1_smallvector: error while loading shared libraries: libLLVMSupport.so.24.0git:
cannot open shared object file: No such file or directory
```

这不是编译失败，是运行时 loader 找不到 `builds/pr-lane/lib`。补上：

```bash
export LD_LIBRARY_PATH=/home/ubuntu/llvm/builds/pr-lane/lib
```

八个实验分别对应本章各节：`exp1` inline capacity 与 `SmallVectorImpl` 传参，`exp2` `StringRef` 悬垂，`exp3` `DenseMap` rehash，`exp4` `APInt` 边界，`exp5` 未消费 `Expected<T>`，`exp6` 删除中间 `Instruction`，`exp7` 工具基础设施，`exp8` 容器迭代顺序。`exp2`、`exp3` 另编译一份 `-fsanitize=address` 版本展示失败路径；非 ASan 版本用来展示静默错误。

## 踩雷集锦

1. **把 `StringRef` 当 `std::string` 存**。错误直觉：「`StringRef` 是轻量字符串，存起来省一次拷贝。」正确认识：它是指针 + 长度，被指向的对象一死就悬垂；SSO 情况下 ASan 都可能抓不到，只是读出垃圾。要存就 `.str()`。
2. **函数参数写 `SmallVector<T, N>&`**。错误直觉：「我只要 4 个元素，写死 N=4 更明确。」正确认识：这把调用方锁死在同一个 N 上，`SmallVector<T, 8>` 传不进来；参数类型应该是 `SmallVectorImpl<T>&`（`ProgrammersManual.md:1561`）。
3. **在 `DenseMap` 插入后继续用旧 reference/iterator**。错误直觉：「我只插了一个 key，其他元素没动。」正确认识：插入可能触发 rehash，整个 bucket 数组被 `delete` 后重建（`DenseMap.cpp:97` 的 `growRelocatable`）；任何 reference、pointer、iterator 都失效。
4. **用 `getLimitedValue()` 当「取低 64 位」**。错误直觉：「它会截断。」正确认识：它饱和到 `Limit`，默认 `UINT64_MAX`；65 位的 `2^64` 会变成 `UINT64_MAX`，而不是 0。要截断用 `trunc(64)`，要安全检查用 `tryZExtValue()`。
5. **`(void)expected;` 当作「已处理」**。错误直觉：「我显式忽略了返回值。」正确认识：`Expected<T>` 的析构会 `assertIsChecked()`，assertion 构建直接 abort；这不是「静默忽略」，而是「未消费」。
6. **以为 ASan 绿了就没有生命周期问题**。错误直觉：「sanitizer 没报就是对的。」正确认识：ASan 只覆盖它能看到的地址；栈上 SSO 缓冲、被复用的栈槽不在覆盖范围内，`size=3 first=4294967287` 就是它放过的例子。

## 本章重点整理

- LLVM 不用 STL 全覆盖，根因是 `-fno-exceptions -fno-rtti`（`CodingStandards.md:573`）、allocator 控制、编译期成本和迭代器稳定性这四条硬约束，不是「STL 不好」。
- `SmallVector<T, N>` 用对象内存储换 malloc；默认 N 让对象接近 64 字节，`sizeof(T) > 256` 会被 `static_assert` 拦住；增长是 `2*Capacity + 1`。
- `ArrayRef`/`StringRef`/`Twine` 都是非拥有视图，生命周期由调用方负责；`Twine` 只能活在一个表达式内。
- `DenseMap`/`StringMap` 的迭代顺序由 hash 决定，插入会 rehash 并让引用/迭代器失效；需要插入序就用 `SetVector`/`MapVector`。
- `Instruction` 继承 `ilist_node_with_parent`，所以 `removeFromParent` 是 O(1) 的指针改接，地址稳定。
- `APInt::getLimitedValue()` 饱和而非截断；`getZExtValue()` 在超过 64 位时 assertion；release 构建里构造参数的 assertion 消失，可能留下违反不变量的对象。
- `Expected<T>` 必须被消费，否则 assertion 构建 abort；`cantFail` 是断言，不是忽略错误的工具。

## 自我检核

- [ ] 不看笔记，能说出 LLVM 关闭异常和 RTTI 之后，错误处理被推到了哪个类型上
- [ ] 能解释 `SmallVector<int, 2>` 为什么是 24 字节，以及第三次 `push_back` 后 `capacity()` 为什么是 5
- [ ] 能写出一个返回 `StringRef` 的悬垂函数，并说明为什么 SSO 情况下 ASan 可能不报
- [ ] 能解释 `Twine` 为什么不能存进变量，以及正确的落盘方式
- [ ] 能说出这个版本的 `DenseMap` 对 `DenseMapInfo` 只要求哪两个函数，以及为什么它不需要 tombstone
- [ ] 能解释为什么 `Instruction` 删除是 O(1)，以及这和 `std::vector<Instruction*>` 的差别
- [ ] 能区分 `simple_ilist`、`ilist`、`iplist` 三者的关系，并说出为什么新代码不该用 `iplist`
- [ ] 能说出 `getLimitedValue()` 和 `getZExtValue()` 在 65 位输入上的不同结局
- [ ] 能写出 `Expected<T>` 的三种正确消费方式，并说明 `(void)V` 为什么不算
- [ ] 能给出一个必须用 `MapVector` 而不是 `DenseMap` 的真实场景，并说明原因

## 延伸阅读

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`llvm/ADT/SmallVector.h`（1516 起）、`llvm/ADT/ilist.h`（1691 起）、`SetVector`（2143 起）、`DenseMap`（2308 起）、`Error`（436 起）、`Expected<T>`（496 起）
  - **能学到什么**：本章每个类的官方定位和「什么时候不该用」，尤其是 `SmallVector` 的参数类型建议和 `DenseMap` 的迭代器失效说明
  - **前提知识**：读过 Ch 3 的 `dyn_cast`/模板基础；不需要先读 `lib/Support` 实现
- **[LLVM Coding Standards](https://llvm.org/docs/CodingStandards.html)**
  - **读哪里**：“Do not use RTTI or Exceptions” 一节，以及 “Prefer C++-style casts” 的开头
  - **能学到什么**：`-fno-exceptions -fno-rtti` 的官方理由，以及 LLVM 为什么用 hand-rolled RTTI 替代 `dynamic_cast`
  - **前提知识**：无；这一节是本章设计约束的原始出处
- **[LLVM `ADT` 目录源码](https://github.com/llvm/llvm-project/tree/main/llvm/include/llvm/ADT)**
  - **读哪里**：`SmallVector.h` 的 `CalculateSmallVectorDefaultInlinedElements`（1201 起）、`DenseMap.h` 的 `grow`（242 起 / 446 起）、`ilist_node.h` 的 `ilist_node_with_parent`（316 起）
  - **能学到什么**：本章所有「为什么是这个数」的一手机制；配合 `lib/Support/SmallVector.cpp:107` 的 `2*OldCapacity+1` 一起读
  - **前提知识**：能读模板和 CRTP；`DenseMap` 部分需要知道开放寻址的基本概念
- **[C++ 标准库 `std::vector` 的迭代器失效规则（cppreference）](https://en.cppreference.com/w/cpp/container/vector)**
  - **读哪里**：“Iterator invalidation” 小节
  - **能学到什么**：对照理解为什么 LLVM 需要 `ilist`；`std::vector` 的失效规则是 `SmallVector` 和 `DenseMap` 行为的参照系
  - **前提知识**：无；这是本章「为什么不用 STL」的对照组

→ [Ch 11 — TableGen 在 LLVM 中的位置：`.td` 如何变成 `.inc`](./11-tablegen-in-llvm.md)
