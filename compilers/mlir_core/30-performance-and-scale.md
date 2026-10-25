# Ch 30 性能与规模：让 MLIR 跑得动大 IR

> **目标**：把「性能」从玄学变成可测量的量。学完本章，你能判断一次 `mlir-opt` 的时间到底花在 context、文本解析、uniquing 还是 pass 遍历上；能自己搭一套最小测量协议（固定 N、取中位数、关 dump、说明机器）；也能在十万 op 的 IR 上解释一次 `--canonicalize` 为什么可能比线性更慢。

## 为什么需要这个？

写 MLIR 的第一年，大家关心的都是「能不能降下去」；第二年开始，问题会变成「为什么这么慢」。

真实场景里的慢，通常不是 pattern 写错了，而是这几件事：

- **每个请求新建一个 `MLIRContext`**，把几十个 dialect 的注册和初始化重做一遍；
- **pass 里套 `walk`**，10 万 op 变成 100 亿次回调；
- **在多线程 pass 里疯狂创建不同的 Attribute**，撞在同一批 storage 分片的写锁上；
- **每次都 parse 文本 IR**，而中间产物本可以用 bytecode；
- **开着 `--mlir-print-ir-after-all` 做 benchmark**，测到的是打印和锁，不是 pass。

这些坑的共同点是：**不测量就看不见**。本章的实验都在本机跑过，数字只对本机有效，但协议可以带走。

> 测量环境：Intel Core i7-10510U（4 核 8 线程，基频 1.8 GHz）、39.8 GB RAM、Windows 10 19045、MLIR 23.1.4 Optimized build（conda-forge）、PowerShell 5.1。笔记本会降频，同一命令的波动可以到 ±30%，所以本章一律报「N 次的中位数」，并把 min/max 一起贴出来。

## 先建立直觉：一条 IR 的四个时间桶

拿到一条 `mlir-opt` 命令，先把时间拆成四个桶：

```text
进程启动 + context + dialect 加载     几十 ms（固定成本）
文本 parse / print                    与 op 数线性，常数很大
pass 算法                             取决于你写的遍历
uniquing 的锁竞争                     只在多线程 + 大量新建 Type/Attribute 时明显
```

四个桶的量级完全不同，优化手段也完全不同：

| 桶 | 典型量级（本机） | 怎么降 |
|---|---|---|
| 进程 + dialect | 40–90 ms | 复用 context、少加载 dialect、别每请求起进程 |
| parse/print 文本 | 10 万 op 约 1.2–2.4 s | 中间产物用 `.mlirbc`、减少 print |
| pass | 10 万 op 从几十 ms 到几秒 | 改遍历结构、减少 rewrite、切分函数 |
| uniquing 锁 | 多线程下才显现 | 少建唯一属性、减少共享 context 上的并发创建 |

一个反直觉的结论先说：**在很多 `mlir-opt` 场景里，parse + print 比 pass 本身贵**。本章示例二会看到，10 万 op 的 `--canonicalize` 只比「解析 + 写 bytecode」多出约 1.3 秒，而后者本身要 1.2 秒。

## 核心概念一：`MLIRContext` 是资源边界

`MLIRContext` 拥有所有 dialect、Type、Attribute、Identifier 和线程池。它的生命周期决定了太多东西：

- 同一个 context 里，`IntegerType::get(ctx, 32)` 永远拿到同一个对象；
- 不同 context 的 Type/Attribute **不能混用**，传过去就是悬空指针或错误 uniquing；
- dialect 是**惰性加载**的：注册（registry）不等于加载（loaded）。

源码里这条路径很短。`MLIRContext::getOrLoadDialect(StringRef)` 先查已加载表，再问 registry 要 allocator：

```cpp
// mlir/lib/IR/MLIRContext.cpp:468-475（源码依据）
Dialect *MLIRContext::getOrLoadDialect(StringRef name) {
  Dialect *dialect = getLoadedDialect(name);
  if (dialect)
    return dialect;
  DialectAllocatorFunctionRef allocator =
      impl->dialectsRegistry.getDialectAllocator(name);
  return allocator ? allocator(this) : nullptr;
}
```

真正重的是 allocator 里的 `Dialect` 构造函数：它要注册 op、type、attribute、interface、pass，还可能递归加载 dependent dialects。所以「加载 47 个方言」和「加载 3 个方言」不是一个量级的操作。

`mlir-opt` 没有暴露「加载全部」的开关，但 `MLIRContext` 有 API：`loadAllAvailableDialects()` 就是 `for (StringRef name : getAvailableDialects()) getOrLoadDialect(name);`（`MLIRContext.cpp:578-581`，源码依据）。

**边界一：`allowUnregisteredDialects` 是调试开关，不是生产方案。** 头文件里的注释写得很直白：

```cpp
// mlir/include/mlir/IR/MLIRContext.h:141-146（源码依据）
/// Enables creating operations in unregistered dialects.
/// This option is **heavily discouraged**: it is convenient during testing
/// but it is not a good practice to use it in production code.
```

实测一下差别：

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
& "$mlir\mlir-opt.exe" .\unreg.mlir
& "$mlir\mlir-opt.exe" .\unreg.mlir --allow-unregistered-dialect
```

输入只有一行 `"toy.print"() : () -> ()`。实测第一条报错：

```text
error: operation being parsed with an unregistered dialect. If this is intended,
please use -allow-unregistered-dialect with the MLIR tool used
```

第二条原样输出：

```mlir
module {
  "toy.print"() : () -> ()
}
```

注意它只是「能 parse」，这个 op 没有 verifier、没有 canonicalization、没有 lowering——一旦进了 pass pipeline 就是个黑洞。

**边界二：多线程 context 里不要加载 dialect。** `isMultithreadingEnabled()` 的定义是「context 自己开着 && LLVM 本身编译了线程」：

源码依据（`MLIRContext.cpp:605-607`）：`return impl->threadingIsEnabled && llvm::llvm_is_multithreaded();`——context 自己开着，且 LLVM 本身编译了线程，两者都满足才算开。

`disableMultithreading()` 不只改一个 bool，它会把三个 uniquer 一起切到「不加锁」模式（`:610-624`）。反过来说，只要 context 允许多线程，所有 uniquing 都按线程安全路径走。

在 pass 执行期间加载新 dialect，源码里是有保护的，但只在 assertions 构建里生效：

```cpp
// mlir/lib/IR/MLIRContext.cpp:489-496（源码依据，位于 #ifndef NDEBUG 内）
if (impl.multiThreadedExecutionContext != 0)
  llvm::report_fatal_error("Loading a dialect (" + dialectNamespace +
                           ") while in a multi-threaded execution context ...");
```

本机是 Optimized build，这段检查被编掉，所以这个问题在本机**测不到**，标 `guided`：在 assertions 构建里，缺 `dependentDialects` 的 pass 会直接 fatal，而不是悄悄在某个线程里构造 dialect。

## 核心概念二：uniquing、锁分片与「getType」

MLIR 的 Type 和 Attribute 都是**唯一化**的：同样的参数只存在一份 storage，指针相等就代表结构相等。入口不在 `MLIRContext` 上——**23.1.4 没有 `MLIRContext::getType`**，而是在 `Type`/`Attribute` 的静态 `get` 上：

```cpp
// mlir/include/mlir/IR/TypeSupport.h:210-216（源码依据）
struct TypeUniquer {
  template <typename T, typename... Args>
  static T get(MLIRContext *ctx, Args &&...args) {
    return getWithTypeID<T, Args...>(ctx, T::getTypeID(),
                                     std::forward<Args>(args)...);
  }
```

```cpp
// mlir/include/mlir/IR/AttributeSupport.h:215-222（源码依据）
class AttributeUniquer {
public:
  template <typename T, typename... Args>
  static T get(MLIRContext *ctx, Args &&...args) {
    return getWithTypeID<T, Args...>(ctx, T::getTypeID(),
                                     std::forward<Args>(args)...);
  }
```

所以「`getType` 的缓存语义」准确说法是：**没有额外的缓存层**。uniquing 集合本身就是缓存，存进去就不再淘汰，直到 context 析构。线程本地还有一层加速，但它只是减少锁，不是语义缓存。

`StorageUniquer` 的结构决定了锁行为：

```cpp
// mlir/lib/Support/StorageUniquer.cpp:106-114（源码依据）
ParametricStorageUniquer(function_ref<void(BaseStorage *)> destructorFn,
                         size_t numShards = 8)
    : shards(new std::atomic<Shard *>[numShards]), numShards(numShards),
      destructorFn(destructorFn) {
  assert(llvm::isPowerOf2_64(numShards) &&
         "the number of shards is required to be a power of 2");
```

- 默认 **8 个分片**，每个分片一把 `SmartRWMutex`（`:69-78`）；
- 分片号是 `hashValue & (numShards - 1)`（`:170-172`），哈希分布不好就堆在一起；
- 查询先走线程本地 cache，再读锁查表，miss 才升级写锁插入（`:133-151`）；
- 线程本地 cache 的注释说得很清楚：`This helps to reduce the lock contention when an object already existing in the cache.`（`:189-191`）。

**为什么大量构造 Attribute 会撞锁？** 因为线程本地 cache 只对「重复的同一个属性」有效。你在多线程 pass 里给每个 op 建一个不同的 `IntegerAttr`/`StringAttr`，每次都是新 key：本地 cache miss → 读锁 miss → 写锁插入。所有线程还只有 8 个分片，热点 hash 会撞在同一把写锁上。

这也是为什么「给 10 万个 op 各加一个不同字符串属性」这种看似无害的调试代码，会把并行 pass 直接打成串行。

## 核心概念三：pass 的复杂度藏在遍历里

`walk` 会递归访问所有嵌套 op：

```cpp
// mlir/include/mlir/IR/Operation.h:822（源码依据）
walk(FnT &&callback) {
  return detail::walk(this, std::forward<FnT>(callback));
}
```

而 `getOps<OpTy>()` 只给你**当前 block 的直接子 op**：

```cpp
// mlir/include/mlir/IR/Block.h:217（源码依据）
iterator_range<op_iterator<OpT>> getOps() {
```

两者没有优劣，只有场景：要递归就用 `walk` 一次走完；只关心一层就用 `getOps`。**错误用法是「在外层 `walk` 的回调里再 `walk` 整个 module」**，复杂度立刻从 O(n) 变成 O(n²)。

下面这段是典型的坏味道（`guided`：需要 out-of-tree pass 才能编译测量，本章没有把它编进二进制，代码按 23.1.4 API 书写）：

```cpp
// O(n²)：每个 addi 都重新数一遍全模块的 addi
void tagAllSlow(ModuleOp module) {
  MLIRContext *ctx = module.getContext();
  auto i32 = IntegerType::get(ctx, 32);
  module.walk([&](arith::AddIOp add) {
    unsigned count = 0;
    module.walk([&](arith::AddIOp) { ++count; });  // 每次 O(n)
    add->setAttr("count", IntegerAttr::get(i32, count));
  });
}
```

改成一次收集、一次改写：

```cpp
// O(n)：收集一遍，属性只建一次
void tagAllFast(ModuleOp module) {
  MLIRContext *ctx = module.getContext();
  auto i32 = IntegerType::get(ctx, 32);
  SmallVector<arith::AddIOp, 64> adds;             // 容量可复用
  module.walk([&](arith::AddIOp add) { adds.push_back(add); });
  auto attr = IntegerAttr::get(i32, adds.size());  // 只做一次 uniquing
  for (arith::AddIOp add : adds)
    add->setAttr("count", attr);
}
```

第二版还有两个细节：`SmallVector` 的容量在多次调用间可以复用（`clear()` 不清容量），以及**属性只构造一次**——这直接把 10 万次 uniquing 写锁降成 1 次。`SmallVector` 在 MLIR 里通过 `mlir/Support/LLVM.h` 直接转出（`:123-133`）。

第三个成本点是 `IRRewriter` 的 listener。`RewriterBase` 在每次插入/替换/删除时都会通知监听者：

```cpp
// mlir/include/mlir/IR/PatternMatch.h:368-403（源码依据）
class RewriterBase : public OpBuilder {
  struct Listener : public OpBuilder::Listener {
    ...
    virtual void notifyOperationErased(Operation *op) {}
```

转发路径在 `:437-465`，`IRRewriter` 本体在 `:780`。listener 不是免费的：它给每次 rewrite 增加一次虚调用和一层簿记，而 pattern 驱动本来就会做大量 rewrite。**热循环里挂 listener 做统计，等于给每个 rewrite 加税**；要么采样，要么只在调试构建里开。

## 底层机制：一次 dialect 加载到底做了什么

把 `getOrLoadDialect` 的慢路径摊开看（`mlir/lib/IR/MLIRContext.cpp:481-519`，源码依据）：

1. 在 `loadedDialects` 里占位（`try_emplace`，值为 `nullptr` 表示「正在加载」）；
2. 调 `ctor()` 构造 `Dialect`，构造函数里跑 `initialize()`：注册所有 op/type/attr/interface，按需递归加载 dependent dialects；
3. 因为递归加载可能让 DenseMap rehash，所以重新查一次地址（源码注释 `:497-500` 明说）；
4. 刷新所有已存在的、以该 dialect 为前缀的 `StringAttr` 的 `referencedDialect` 指针（`:506-514`）；
5. 应用 registry 里的 extensions（`:516-517`）。

第 4 步容易被忽略：**先创建字符串属性、后加载 dialect，是有成本的**，它要遍历该 namespace 下所有已存在的 StringAttr storage。这也是为什么「先注册、按需加载、尽量早加载」比「边跑边加载」稳。

加载成本并不均匀（`arith`/`func` 很轻，`tosa`/`spirv` 这类要注册的 op/interface 多得多），但本章只测「全部 vs 两个」的总差距；要拆到单个 dialect，得自己写 C++ 对 `getOrLoadDialect` 计时。

## 示例一：只加载必要 dialect vs 加载全部（实测）

`mlir-opt` 没有 `--load-all-available-dialects`，但 `--canonicalize` 有个 `filter-dialects` 选项，会把列出的 dialect **强制加载进 context 作为 dependent dialects**。用它做一个诚实的代理实验：IR 是一个空 module，差异只来自「加载并收集这些 dialect 的 canonicalization patterns」。

```powershell
$mlir = "D:\Learning-with-Claude\.scratch\mlir-env\Library\bin"
$all = "acc,affine,amdgpu,arith,arm_neon,arm_sme,arm_sve,async,bufferization,builtin,cf,complex,dlti,emitc,func,gpu,index,irdl,linalg,llvm,math,memref,ml_program,mpi,nvgpu,nvvm,omp,pdl,pdl_interp,ptr,quant,rocdl,scf,shape,shard,smt,sparse_tensor,spirv,tensor,tosa,transform,ub,vector,wasmssa,x86,xegpu,xevm"
& "$mlir\mlir-opt.exe" .\empty.mlir
& "$mlir\mlir-opt.exe" .\empty.mlir --canonicalize=filter-dialects=arith,func
& "$mlir\mlir-opt.exe" .\empty.mlir "--canonicalize=filter-dialects=$all"
```

实测（N=15，三组交替跑以抵消降频，取中位数）：

| 配置 | 中位数 | min | max |
|---|---|---|---|
| 空 module，只 parse + print | 41.3 ms | 32.7 | 144.6 |
| `--canonicalize` 强制加载 `arith,func` | 41.8 ms | 31.9 | 137.2 |
| `--canonicalize` 强制加载全部 47 个 | 69.7 ms | 57.7 | 121.1 |

结论：**加载 45 个额外 dialect 大约多花 28 ms**（69.7 − 41.8）。单看不大，但它有两个放大场景：

- 每次请求都新建 context / 起进程的服务，这 28 ms 是纯税；
- 加载全部 dialect 还会把它们的 canonicalization patterns 全收进来，greedy driver 的匹配集合更大，IR 越大越明显。

注意实验设计：这里没有单独测「纯加载」，因为 `filter-dialects` 同时影响 pattern 收集。数字应读作「加载 + 收集的上界」，而不是「每个 dialect 的构造时间」。要拆得更细，得写 C++ 直接调 `loadAllAvailableDialects()` 并计时，本章不做。

## 示例二：1k / 1 万 / 10 万 op 的 pass 实测

生成器很简单：一条 `addi` 链，每个 op 都依赖前一个，且第二个操作数是函数参数（不会被常量折叠，也不会被 CSE 合并）。

```powershell
function Gen-Big([int]$n, [string]$path) {
  $sb = [Text.StringBuilder]::new()
  [void]$sb.AppendLine("func.func @big(%seed: i32) -> i32 {")
  $prev = "%seed"
  for ($i = 0; $i -lt $n; $i++) {
    $v = "%v$i"
    [void]$sb.AppendLine("  $v = arith.addi $prev, %seed : i32")
    $prev = $v
  }
  [void]$sb.AppendLine("  return $prev : i32")
  [void]$sb.AppendLine("}")
  [IO.File]::WriteAllText($path, $sb.ToString(), (New-Object Text.UTF8Encoding($false)))
}
```

测量协议：同一命令跑 7 次，`*> $null` 丢掉输出，取中位数。为了让「pass 成本」从「打印成本」里浮出来，这一组统一用 `--emit-bytecode -o tmp.mlirbc` 输出（bytecode 写比文本 print 便宜得多）。

```powershell
$exe = "$mlir\mlir-opt.exe"
function Med([string[]]$a, [int]$n = 7) {
  $ts = for ($i = 0; $i -lt $n; $i++) { (Measure-Command { & $exe @a *> $null }).TotalMilliseconds }
  [math]::Round(($ts | Sort-Object)[[int]($n / 2)], 1)
}
```

实测（本机，N=7，单位 ms）：

| op 数 | 文件大小 | 仅解析 + 写 bytecode | +`--canonicalize` | +`--cse` | +10 次 `canonicalize` |
|---|---|---|---|---|---|
| 1 万 | 417.8 KB | 94.7 | 153.9 | 222.2 | 738.8 |
| 10 万 | 4372.9 KB | 1248.5 | 2604.8 | 2817.9 | 4632.9 |

| pass | 1 万 op | 10 万 op | 规模比（op ×10） |
|---|---|---|---|
| 第一次 `canonicalize` | 59.2 ms | 1356.3 ms | **22.9×** |
| `cse` | 127.5 ms | 1569.4 ms | **12.3×** |
| 10 次 `canonicalize` 合计 | 644.1 ms | 3384.4 ms | 5.3× |

三个观察：

1. **第一次 `canonicalize` 明显超线性**：op 数 ×10，pass 时间 ×22.9。这符合「greedy driver 反复扫描 + 每次 rewrite 触发 use-list 更新」的直觉，但本章没有做 profile，具体归因标 `guided`。
2. **`cse` 更接近线性**：×12.3，构建哈希表的成本是可预期的。
3. **重复跑 10 次反而摊薄**：第一次之后的 `canonicalize` 面对的是已经稳定的 IR，每次约 225 ms（10 万）/ 65 ms（1 万），所以总增量只有 5.3 倍。

默认的文本输出路径也要看一眼（text → text，N=7，单位 ms）：

| op 数 | 仅 parse + print | +`canonicalize` | +`cse` |
|---|---|---|---|
| 1 千 | 53.4 | 56.1 | 67.5 |
| 1 万 | 332.1 | 337.9 | 381.2 |
| 10 万 | 2431.0 | 2773.3 | 2377.9 |

这张表最重要的信息不是 pass 多快，而是：**print 把差异吃掉了**。10 万 op 时 `cse` 甚至「比基线还快」，那只是 ±30% 噪声——凡是小于噪声的差值都不要下结论。

内存用采样峰值工作集（每 5 ms 读一次 `PeakWorkingSet64`）：

```powershell
$p = Start-Process -FilePath $exe -ArgumentList @($f,'--canonicalize') -NoNewWindow -PassThru -RedirectStandardOutput .\out.tmp
$peak = 0
while (-not $p.HasExited) {
  $p.Refresh(); if ($p.PeakWorkingSet64 -gt $peak) { $peak = $p.PeakWorkingSet64 }
  Start-Sleep -Milliseconds 5
}
```

| op 数 | parse + print | +`--canonicalize` | +`--cse` |
|---|---|---|---|
| 1 万 | 18.8 MB | 18.7 MB | 18.8 MB |
| 10 万 | 65.7 MB | 66.4 MB | 66.0 MB |

用 10k 与 100k 的差值除 9 万，粗略折算 **每个 op 约 0.55 KB**（含 IR、类型、uniquing 表、解析临时对象）；这个数字比时间稳定得多，适合预估内存。

## 示例三：bytecode 与文本 parse 的差距（实测）

MLIR 的 bytecode 不是「压缩文本」，而是一种带版本号的二进制序列化。写和读都很直接（本机 `mlir-translate --help` 里没有 `--mlir-to-bytecode`，bytecode 由 `mlir-opt --emit-bytecode` 负责）：

```powershell
& "$mlir\mlir-opt.exe" .\big100k.mlir --emit-bytecode -o .\big100k.mlirbc
& "$mlir\mlir-opt.exe" .\big100k.mlirbc          # 按扩展名/魔数自动识别
```

10 万 op 的实测（N=5，单位 ms；与示例二不是同一次会话，绝对值有 ±10% 波动，看比值）：

| 输入 → 输出 | 中位数 |
|---|---|
| 文本 → 文本 | 2170.0 |
| 文本 → bytecode | 1193.6 |
| bytecode → 文本 | 1390.3 |
| bytecode → bytecode | 702.4 |

文件大小：文本 4372.9 KB，bytecode 1905.1 KB（约 2.3×）。

- 文本 parse 约等于「文本 → bytecode」减去「bytecode 写」，即 1193.6 − 702.4 ≈ **0.49 s** 的额外解析开销；bytecode 解码快得多。
- 端到端「读 + 写」用 bytecode 是 702 ms，文本是 2170 ms，**约 3.1×**。
- 但 bytecode 不可读、跨版本不保证兼容（有 `--emit-bytecode-version`、`--emit-bytecode-producer` 可调）。**人读用文本，机器传用 bytecode**，这是管线的常规做法。

## 示例四：多线程下的 pass 顺序（实测）

把 32 个函数、每个 1000 个 `addi` 写成 `multi32.mlir`，用**嵌套 pipeline** 让 pass manager 按函数并行：

```powershell
$pipe = 'builtin.module(func.func(canonicalize))'
& "$mlir\mlir-opt.exe" .\multi32.mlir "--pass-pipeline=$pipe" --mlir-print-ir-after-all
& "$mlir\mlir-opt.exe" .\multi32.mlir "--pass-pipeline=$pipe" --mlir-print-ir-after-all --mlir-disable-threading
```

实测（`--mlir-print-ir-after-all` 打到 stderr，共 32 个 dump）：

```text
线程启用：g4, g0, g2, g3, g5, g1, g6, g12, g9, g11, g7, g10, g13, g15, g16, ...
单线程  ：g0, g1, g2, g3, g4, g5, g6, g7, g8, g9, g10, g11, g12, g13, g14, ...
```

两次多线程运行的 dump **内容哈希不同**；两次单线程运行**逐字节相同**。

原因不神秘：`func.func` 是 isolated-from-above 的 op，嵌套 adaptor 会把它的 pass 丢进线程池；每个线程跑完就抢着打印。打印本身有锁，但**抢锁顺序就是完成顺序**，而完成顺序由调度和机器负载决定。

三个实用结论：

1. **要可复现的 dump，加 `--mlir-disable-threading`**。Ch 10 的调试流程在这个模式下才稳定。
2. **要测性能，绝对不要开 `--mlir-print-ir-after-all`**。dump 会串行化输出、放大 IO，测出来的是打印时间。
3. **注意作用域**：module 级的 `--canonicalize` 是对整个 module 的一次 pass，只有一个 dump，顺序天然稳定；只有嵌套到 `func.func(...)` 才有并行和乱序。别把「module 级稳定」误当成「多线程稳定」。

顺便说一句并行收益：同一份 `multi32.mlir`，嵌套 `canonicalize` 多线程中位数 598 ms、单线程 631 ms（N=9）。**只有约 5%**，因为 parse/print 占了 500 ms 左右，真正可并行的部分不大。在这台 4 核笔记本上，多线程不是银弹；IR 切不出多个 isolated op 时（比如一个 10 万 op 的巨型函数），并行度就是 1。

## 对比与取舍

**长驻 context vs 每任务 context。** 编译器服务（LSP、编译服务器）应该复用 context，把 dialect 加载成本摊掉；但 context 不是线程安全的容器，跨线程共享要靠 pass manager 的并行机制，而不是自己乱传 IR。每个任务一个 context 更容易隔离，代价是每次几十毫秒起步。

**文本 vs bytecode。** 文本适合人读、适合 diff、适合教学；bytecode 适合 pipeline 中间落盘、适合大 IR 的快速重启。别在 CI 里反复 parse 几百 MB 文本。

**多线程 vs 单线程。** 多线程的收益上限是「可并行 pass 时间 / 总时间」。parse 和 print 不可并行，巨型单函数也不可并行。先量比例，再决定要不要为并行去切分函数。

**早优化 vs 晚优化。** pass 越多，IR 被扫描的次数越多。canonicalize 放在 pipeline 的每个阶段很舒服，但 10 次 canonicalize 在 10 万 op 上就是 3.4 秒。能合并的就合并，能在小 IR 上做的就别拖到大 IR。

## 踩雷集锦

1. **只跑一次就下结论。** 本机同一命令的 min/max 能差 4 倍（见示例一的 max 144.6 ms）。至少 N=7，报中位数，顺手报 min/max。
2. **开着 `--mlir-print-ir-after-all` 做 benchmark。** 你测的是打印锁。
3. **把 dump 顺序当 pass 顺序。** 多线程下 32 个函数的 dump 顺序每次都不一样；要看顺序就 `--mlir-disable-threading`。
4. **在 `walk` 回调里再 `walk` 整个 module。** O(n²)，10 万 op 直接卡死。
5. **每线程新建 `MLIRContext` 以为能加速。** 每个 context 都要重新加载 dialect、重建 uniquing 表，冷启动更贵。
6. **在多线程 pass 里给每个 op 建不同 Attribute。** 8 个 storage 分片的写锁会被打爆；能共享就共享，能延迟就延迟。
7. **把 `--allow-unregistered-dialect` 当正式方案，或跨 context 传 Type/Attribute。** 前者只让 parse 过关，verifier、canonicalize、lowering 全部失效；后者是错误 uniquing 甚至崩溃。

## 进阶：再往深一层

- **看 `StorageUniquer` 的分片数**：`ParametricStorageUniquer` 的默认值是 8（`StorageUniquer.cpp:106-114`）。如果你的 dialect 有超多唯一属性，可以考虑自己在构造时给更多分片；但先量，别猜。
- **看 `MLIRContext::getRegistryHash`**（`MLIRContext.cpp:583-591`）：它把已加载 dialect、注册的 attribute/op/type 数量揉进一个 hash，用于缓存失效判断。这解释了一个现象：**加载新 dialect 会让依赖 registry 的缓存失效**。
- **看 pass 的 `dependentDialects`**：`Passes.td` 里的声明决定了 pass 会不会在运行时偷偷加载 dialect。缺了它，assertions 构建会 fatal（见前面的 guided 说明）。

## 优化检查清单

1. **context 复用**：进程/服务级别复用，不要每请求新建。
2. **按需加载 dialect**：别在工具里无脑 `loadAllAvailableDialects()`；`mlir-opt` 场景下 47 个方言约多 28 ms。
3. **中间产物用 `.mlirbc`**：10 万 op 端到端约快 3 倍。
4. **测量前关 dump**：`--mlir-print-ir-after-all` 只用于调试。
5. **固定 N、取中位数、说明机器**：小于噪声的差值不算结论。
6. **pass 内禁止嵌套 walk**：一次收集 + 批量处理。
7. **复用 `SmallVector` 容量**：`clear()` 不清 capacity；热点路径 `reserve`。
8. **属性只建一次**：把 uniquing 从 O(n) 次降到 O(1) 次。
9. **热循环里不挂 listener**：listener 是调试工具，不是统计工具。
10. **先切函数再谈并行**：没有多个 isolated op，线程池也帮不了你。

## 动手练习

**任务规格**：在自己的机器上复现本章的两组核心数字，并解释差异。

1. 用示例一的命令测「只加载 `arith,func`」与「加载全部 47 个」，N=15，三组交替跑，报中位数和 min/max。
2. 用示例二的生成器造 1 千 / 1 万 / 10 万 op，测 `--canonicalize` 与 `--cse`，算出「pass 增量」和规模比。
3. 把 10 万 op 的 IR 转成 bytecode，比较四种读写的耗时与文件大小。
4. 用 `multi32.mlir` 跑两次多线程 dump，比较哈希；再加 `--mlir-disable-threading` 跑两次。
5. 把示例二的 O(n²) 代码改写成 O(n)，口述你会怎么验证改写没有改变语义。

**测试用例**（至少跑这三个）：

- 1 万 op + `--canonicalize`：应看到 IR 基本不变（链式 addi 不可折叠），耗时在几十到几百 ms。
- 10 万 op + `--cse`：应看到 IR 不变，内存约 60–70 MB（本机）。
- `multi32.mlir` + 嵌套 `canonicalize` + dump：两次多线程哈希应不同，两次单线程哈希应相同。

**分段步骤**：先固定测量协议（N、输出重定向、交替跑），再改 IR 规模，最后改线程设置。不要一次改两个变量。

<details>
<summary>参考解答与常见卡点</summary>

- 示例一的正确答案不是某个固定毫秒数，而是「全部加载 > 按需加载」这个方向和量级（本机约 +28 ms）。你的机器可能只有 +10 ms 或 +60 ms。
- 示例二里如果 `canonicalize` 的增量是负的，说明噪声大于信号：加大 N、关掉后台程序、把输出改成 `--emit-bytecode`。
- 如果 10 万 op 的 `--cse` 比 `--canonicalize` 慢很多，检查你的 IR 是否有大量重复的常量/属性：CSE 要建哈希表，canonicalize 只做局部折叠。
- 多线程 dump 哈希相同，通常是机器只有 1 个可用核，或者 IR 只有 1 个函数（没有并行任务）。
- 记忆点：`Measure-Command` 包住的是整个进程，不是单次 pass；想拆细只能上 profile 工具或自己写 C++。

</details>

**常见卡点**：PowerShell 里 `*> $null` 必须加，否则控制台输出会污染计时；`Set-Content -Encoding utf8NoBOM` 在 PowerShell 5.1 不存在，用 `[IO.File]::WriteAllText` + `UTF8Encoding($false)`。

## 本章重点整理

- 性能要拆成四个桶：context/dialect、parse/print、pass、uniquing 锁。
- `MLIRContext` 是资源边界：dialect 惰性加载，`loadAllAvailableDialects()` 很贵，`allowUnregisteredDialects` 只是调试开关。
- Type/Attribute 的唯一化入口是 `TypeUniquer::get` / `AttributeUniquer::get`，不是 `MLIRContext::getType`（23.1.4 没有这个 API）。
- `StorageUniquer` 默认 8 个分片，每片一把读写锁；大量唯一属性 = 大量写锁。
- `walk` 递归、`getOps` 单层；回调里再 `walk` 就是 O(n²)。
- 本机实测：10 万 op 第一次 `canonicalize` 的增量约是 1 万 op 的 22.9 倍；`cse` 约 12.3 倍。
- bytecode 让 10 万 op 的读写从 2.17 s 降到 0.70 s，文件小 2.3 倍。
- 多线程会让 `--mlir-print-ir-after-all` 的 dump 顺序不稳定；要确定性就 `--mlir-disable-threading`。

## 自我检核

- [ ] 我能说出一次 `mlir-opt` 的四个时间桶，并各举一个优化手段
- [ ] 我知道 `loadAllAvailableDialects()` 在哪、为什么不该在生产路径里调
- [ ] 我能解释 `allowUnregisteredDialects` 放开后失去了哪些保证
- [ ] 我能说清 `TypeUniquer::get` 和「缓存」的关系（没有额外缓存层，uniquing 集合就是缓存）
- [ ] 我能解释 8 个 storage 分片为什么会成为多线程写热点
- [ ] 我能写出 O(n) 版本的「给所有 op 打标签」，并说明为什么属性只该建一次
- [ ] 我能复现「多线程 dump 顺序不稳定」，并知道测性能时要关 dump
- [ ] 我知道 bytecode 适合机器传、文本适合人读，且 bytecode 跨版本不保证兼容

## 延伸阅读

- **[MLIRContext.h / MLIRContext.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/IR/MLIRContext.cpp)**
  - **读哪里**：`getOrLoadDialect`（468-519）、`loadAllAvailableDialects`（578-581）、`disableMultithreading`（610-624）。
  - **学什么**：加载路径、线程开关和 uniquer 的联动。
  - **和本章的关联**：示例一和「边界二」都直接从这几段来。

- **[StorageUniquer.h / StorageUniquer.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/mlir/lib/Support/StorageUniquer.cpp)**
  - **读哪里**：分片构造函数（106-114）、`getOrCreate`（125-151）、`getShard`（170-187）。
  - **学什么**：线程本地 cache、读写锁、分片哈希。
  - **和本章的关联**：解释「大量构造 Attribute 会撞锁」。

- **[LLVM Programmer's Manual](https://llvm.org/docs/ProgrammersManual.html)**
  - **读哪里**：`SmallVector`、`DenseMap`、`DenseSet` 三节。
  - **学什么**：容量复用、哈希容器复杂度、迭代器失效规则。
  - **和本章的关联**：O(n) 写法的两个关键工具。

- **[MLIR Bytecode 文档](https://mlir.llvm.org/docs/Bytecode/)**
  - **读哪里**：格式版本、资源（resource）编码、版本兼容策略。
  - **学什么**：为什么 bytecode 快、为什么不能当长期存储格式。
  - **和本章的关联**：示例三的数字背后的设计。


性能视角讲完，下一步把整门课收成一条真实链路：从一段 `scf.for + memref` 的 IR 一路降到 LLVM IR，再用 `llc` 出汇编，逐段解释每一次变形。

→ [Ch 31 端到端复盘](./31-end-to-end.md)