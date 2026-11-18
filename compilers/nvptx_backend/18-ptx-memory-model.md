# Ch 18 — PTX 内存一致性模型：scope、ordering 与 fence

> **目标**：能用 PTX 的正式术语（weak/strong、scope、ordering、causality）解释一段并发 kernel 为什么对/错，并能从 IR 一路看到 SASS 的 `STRONG.GPU` / `MEMBAR.SC`。

> **环境**：LLVM `24.0.0git`（`ct-instr-trunk`）、CUDA 12.4、Tesla T4（sm_75）。

## 为什么需要这个？

GPU 是弱内存模型：对共享数据的普通访问（没有 `volatile`、没有 atomic、没有 fence）可以被编译器和硬件重排。CUDA 程序里最难查的 bug 不是算错，而是「在 T4 上跑一万次都对，换一张卡就挂」。

PTX 的内存模型是这份契约的正式定义。读懂它，你才能判断：

- 这个 `__threadfence()` 到底够不够；
- 这个 atomic 的 scope 该用 `.gpu` 还是 `.sys`；
- 为什么 `volatile` 不是同步原语。

## 先建立直觉

三个正交维度：

```
操作类型：weak（普通 ld/st）  vs  strong（atomic / fence）
作用域：  .cta  <  .cluster  <  .gpu  <  .sys
顺序：    .relaxed < .acquire / .release < .acq_rel < .seq_cst
          （acquire 与 release 方向不同，并非可比较的强度；这里只表达约束递增）
```

- **scope 回答「谁能看见」**：`.cta` 只在 block 内，`.gpu` 在整个设备，`.sys` 跨设备/主机。
- **ordering 回答「能怎么重排」**：relaxed 只保证原子性，acquire/release 建立单向屏障，seq_cst 再加全序。
- **fence 是独立的同步点**：它不访问内存，只约束前后的访问顺序。

一个常见的误解：**scope 越大越安全，所以都用 `.sys`**。错。`.sys` 会强制跨设备一致性，代价高；绝大多数 kernel 只需要 `.gpu`。

## 一、IR → PTX 的完整矩阵（实测）

先看 LLVM IR 的几种写法（`mem2.ll`）：

```llvm
; monotonic + device scope
%v = load atomic i32, ptr addrspace(1) %p syncscope("device") monotonic, align 4
; acquire + device scope
%v = load atomic i32, ptr addrspace(1) %p syncscope("device") acquire, align 4
; seq_cst + device scope
%v = load atomic i32, ptr addrspace(1) %p syncscope("device") seq_cst, align 4
; release + block scope
store atomic i32 %v, ptr addrspace(1) %p syncscope("block") release, align 4
; acq_rel RMW + device scope
%v = atomicrmw add ptr addrspace(1) %p, i32 1 syncscope("device") acq_rel, align 4
```

`llc` 的产物（逐字）：

```ptx
ld.relaxed.gpu.global.b32 	%r1, [%rd1];     ; monotonic + device
ld.acquire.gpu.global.b32 	%r1, [%rd1];     ; acquire + device
fence.sc.gpu;                                  ; seq_cst load 的前半
ld.acquire.gpu.global.b32 	%r1, [%rd1];
st.release.cta.global.b32 	[%rd1], %r1;     ; release + block
atom.acq_rel.gpu.global.add.u32 %r1, [%rd1], 1;
```

几条可以直接背下来的映射：

| IR | PTX |
|---|---|
| `monotonic` | `.relaxed` |
| `acquire` | `.acquire` |
| `release` | `.release` |
| `acq_rel` | `.acq_rel` |
| `seq_cst` | `fence.sc.<scope>` + `.acquire`（load / RMW；release 由前置 fence 承担） |
| `syncscope("device")` | `.gpu` |
| `syncscope("block")` | `.cta` |
| 默认（无 syncscope） | `.sys` |

## 二、三个必须知道的坑

### 坑 1：显式写 `syncscope("system")` 会被拒绝

```bash
llc -mtriple=nvptx64 -mcpu=sm_75 mem.ll
# LLVM ERROR: NVPTX backend does not support syncscope "system" (ID=2).
# Supported syncscopes are: singlethread, <empty string>, block, cluster, device.
```

但**默认**（不写 syncscope）就是 system scope，并且会被 lower 成 `.sys`：

```ptx
ld.relaxed.sys.global.b32 	%r1, [%rd1];
atom.relaxed.sys.global.add.u32 ...
```

这是 LLVM 的接口怪癖：`.sys` 只能通过「省略 syncscope」或 NVVM intrinsic 得到，不能显式写 `"system"`。写兼容层时，这是一个真实的兼容性陷阱。

### 坑 2：`seq_cst` load 会多一条 fence

```ptx
fence.sc.gpu;
ld.acquire.gpu.global.b32 	%r1, [%rd1];
```

`seq_cst` 不是「更强的 acquire」，它在 acquire 之前加了一条全序 fence。代价是真实的：fence 会阻止周围的访存重排，也阻止 `ptxas` 的自由调度。

`seq_cst` 的 `cmpxchg` 也是同样的形态：

```ptx
fence.sc.sys;
atom.acquire.sys.global.cas.b32 	%r3, [%rd1], %r1, %r2;
```

release 部分由前面的 fence 承担，所以 CAS 本身只带 `.acquire`。这是一个容易看漏的细节。

### 坑 3：`cluster` scope 在 sm_90 之前会静默降级成 `.cta`

`syncscope("cluster")` 在 sm_75 / sm_80 上不会报错，而是直接输出 `.cta`；sm_90 起才是 `.cluster`：

```bash
# 同一份 cluster.ll，只换 -mcpu
-mcpu=sm_80   ->  st.release.cta.global.b32
-mcpu=sm_90   ->  st.release.cluster.global.b32
-mcpu=sm_90a  ->  st.release.cluster.global.b32
```

原因在 `NVPTXISelDAGToDAG.cpp` 的 `resolveScope()`：

```cpp
// Clusters contain exactly 1 block on targets without cluster support.
if (S == NVPTX::Scope::Cluster && !T->hasClusters())
  return NVPTX::Scope::Block;
```

`hasClusters()` 等价于 `hasFeature(SM90)`。这在语义上是对的（无 cluster 支持时，一个 cluster 就是一个 block），但**IR 层写的 scope 与 PTX 层看到的 scope 可能不一致**。做兼容层、diff 工具或性能模型时，要把这条降级规则算进去。

## 三、system scope 的正确写法

既然 `syncscope("system")` 不被接受，system scope 从哪来？

1. **默认 syncscope**（省略）→ `.sys`，见上。
2. **NVVM intrinsic**：

```llvm
declare void @llvm.nvvm.membar.sys()
declare void @llvm.nvvm.membar.gl()
declare void @llvm.nvvm.membar.cta()
```

```ptx
membar.sys;
membar.gl;
membar.cta;
```

CUDA 源码里的 `__threadfence_system()` / `__threadfence()` / `__threadfence_block()` 就是这三条。实测（CUDA 12.4 / sm_75）`atomicAdd_system(p,1)` 的产物取决于返回值是否被使用：

```ptx
; 返回值不用：reduction，无返回值
red.global.sys.add.u32 	[%rd2], 1;

; 返回值要用：atomic，带返回值
atom.global.sys.add.u32 	%r1, [%rd4], 1;
```

注意 nvcc 这里**没有**打印 `.relaxed`。如果你走 LLVM IR，`atomicrmw add ... monotonic`（默认 system scope）会得到 `atom.relaxed.sys.global.add.u32`——`.relaxed` 是 LLVM 显式写出的 ordering 后缀；PTX 里省略 ordering 时默认就是 relaxed。两者语义相同，但文本不同，做 diff/兼容性测试时不要被这个差异骗到。

### 源码落点：为什么 `"system"` 不被接受

看两处就懂了：

- `NVPTXISelDAGToDAG.cpp` 的 `NVPTXScopes` 构造函数：它把 `AtomicScope::System` 映射到 `getAtomicScopeIRString()` 的返回值。
- `include/llvm/TargetParser/AtomicScope.h` 的 `getAtomicScopeIRString()`：NVPTX 的 `System` 分支返回 **空字符串**。

```cpp
case AtomicScope::Device:
  return "device";
case AtomicScope::System:
  return "";
```

也就是说，system scope 的 IR 拼写就是「不写 syncscope」，而不是 `"system"`。同一个表里 `singlethread` 是被注册的，但 `tryFence()` 会把 thread-scope fence 直接删掉（无跨线程语义）。这解释了本章两个看似矛盾的现象：**默认是 `.sys`，显式写 `"system"` 反而 fatal**。

## 四、SASS 层：strong / weak 是显式的

把上面的 kernel 编成 cubin，`cuobjdump -sass`（实测）：

| PTX | SASS |
|---|---|
| `ld.relaxed.gpu` / `ld.acquire.gpu` | `LDG.E.STRONG.GPU` |
| `st.release.gpu` | `STG.E.STRONG.GPU` |
| `atom.acq_rel.gpu` | `ATOMG.E.ADD.STRONG.GPU` |
| `fence.sc.gpu` | `MEMBAR.SC.GPU` |
| `membar.gl` | `MEMBAR.SC.GPU` |
| `membar.sys` | `MEMBAR.SC.SYS` |
| `membar.cta` | `MEMBAR.SC.CTA` |
| 普通 load/store | `LDG.E.SYS` / `STG.E.SYS` |
| `volatile` load/store | `LDG.E.STRONG.SYS` / `STG.E.STRONG.SYS` |
| `ld.global.nc` | `LDG.E.CONSTANT.SYS` |

三个反直觉的观察：

- **普通访存在 SASS 里也带 `.SYS`**（`LDG.E.SYS`），但它是 weak 的；strong 与否由 `.STRONG` 标记，不是由 `.SYS` 标记。
- **`volatile` 在 SASS 里是 `STRONG.SYS`**：它确实阻止重排与缓存优化，但**不提供原子性**（不是 `ATOM`）。`volatile` 不能代替 atomic。
- **`STRONG` 不等于 acquire**：它表示这是一条 strong operation（带 scope 的有序访存/原子），`relaxed` 也是 strong，只是 ordering 最弱。判断 ordering 要看 PTX 的 `.relaxed` / `.acquire` / `.release`，不要只看 SASS 的 `.STRONG`。

还有一个 sm_75 上的实测细节：`ptxas` 不会把 acquire/release 只做成一条 `LDG/STG`。它会在 release store 前插入 `MEMBAR.ALL.GPU`，在 acquire load 后插入 `CCTL.IVALL`：

```
st.release.gpu  ->  MEMBAR.ALL.GPU ; STG.E.STRONG.GPU
ld.acquire.gpu  ->  LDG.E.STRONG.GPU ; CCTL.IVALL
```

所以「PTX 一条、SASS 一条」只在 relaxed/普通访存上成立；带 ordering 的访存会被拆成「屏障 + 访存」。做性能模型时，这部分额外指令必须计入。

## 五、正式术语：morally strong 与 causality

PTX ISA 用几个正式概念描述这套模型：

- **weak operation**：普通 `ld`/`st`，不参与同步。
- **strong operation**：带 ordering 的 atomic/fence。
- **morally strong**：两个操作在同一个 scope 内、且访问同一地址时，满足「强」的条件，才能建立同步。
- **causality order**：由 release/acquire 模式与 fence 建立的偏序。
- **communication order**：同一地址上的读写顺序。

实用推论：

1. 两个线程要同步，双方的 atomic 必须使用**同一个（或规格明确允许的兼容）scope**；一边 `.cta`、一边 `.gpu` 不构成 morally strong。实践上让两端写同一个 scope 最安全。
2. `relaxed` 只保证「不撕裂」，不保证看到顺序；经典 message-passing 必须 release/acquire。
3. fence 的 scope 必须覆盖被排序的访问：`.gpu` fence 管不了 `.sys` 访问；反过来，`.sys` fence 可以给更小 scope 的访问兜底。

## 六、一个正确的 message-passing 骨架

```cuda
__device__ int data;
__device__ int flag;

// producer
data = 42;
__threadfence();                 // membar.gl
atomicExch(&flag, 1);            // atom.global.exch.b32（默认 device scope）

// consumer
while (atomicAdd(&flag, 0) == 0) {}
__threadfence();                 // membar.gl
int v = data;                    // 必须看到 42
```

对应的 PTX 关键行：

```ptx
st.global.b32 	[data], 42;
membar.gl;
atom.global.exch.b32 	%r2, [flag], 1;
...
membar.gl;
ld.global.b32 	%r, [data];
```

如果用 `st.release.gpu` / `ld.acquire.gpu`，就不需要显式 fence：

```ptx
st.release.gpu.global.b32 	[flag], 1;
ld.acquire.gpu.global.b32 	%r, [flag];
```

**优先用 acquire/release，少用全序 fence**：前者给编译器和硬件的自由度更大。

## 踩雷集锦

1. **以为 `volatile` 能同步**：它只阻止优化，不提供原子性；SASS 里是 `STRONG.SYS` 但不是 `ATOM`。
2. **显式写 `syncscope("system")`**：LLVM 直接报错；用默认或 NVVM intrinsic。
3. **scope 混用**：一边 `.cta`、一边 `.gpu` 不构成同步；这是最隐蔽的竞态。
4. **把 `relaxed` 当 acquire 用**：原子性有了，顺序没有。
5. **在 T4 上跑通就以为正确**：弱内存 bug 可能只在特定调度/架构上出现；要么用形式化 litmus，要么用工具（compute-sanitizer racecheck）。
6. **`seq_cst` 到处用**：多出的 `fence.sc` 会显著限制调度，性能代价真实存在。
7. **以为 `cluster` 在旧架构会报错**：sm_90 之前它会静默降级成 `.cta`；IR 与 PTX 的 scope 可能对不上。

## 进阶：proxy memory 与 async

TMA / `cp.async.bulk` 引入了 **async proxy memory**：异步代理的写入对普通线程不可见，直到 `fence.proxy.async`。这类 fence 不在上面的 ordinary memory 模型里，是 PTX 7.0+ 的新维度。sm_75 不支持 `cp.async`，本课没有实测；做 Hopper/Blackwell 后端时必须补上这一块。

另外，cluster scope（`.cluster`，addrspace 7）是 Hopper 新增的层级，介于 `.cta` 与 `.gpu` 之间。scope 的偏序是：

```
singlethread < .cta < .cluster < .gpu < .sys
```

## 动手练习

1. 用 `mem2.ll` 的写法生成 `.relaxed.gpu` / `.acquire.gpu` / `st.release.cta` 三份 PTX，确认后缀。
2. 把 `syncscope("device")` 删掉，看 PTX 变成 `.sys`。
3. 显式写 `syncscope("system")`，记录报错原文。
4. 用 `__threadfence_system()` 编一个 kernel，确认 PTX `membar.sys` 与 SASS `MEMBAR.SC.SYS`。
5. 把 `volatile` 与 `atomicAdd` 各编一次，对比 SASS 的 `STRONG` 与 `ATOM`。

## 本章重点整理

- GPU 内存模型 = 操作类型 × scope × ordering；三者缺一不可。
- 默认 scope 是 `.sys`，显式 `"system"` 反而不被 LLVM 接受。
- `seq_cst` = fence + acquire/acq_rel；fence 有真实性能代价。
- `volatile` 是 `STRONG.SYS` 但不是 atomic；不能当同步原语。
- SASS 用 `.STRONG.<scope>` 与 `MEMBAR.SC.*` 显式表达强度。

## 自我检核

- [ ] 能画出 scope 的偏序，并说出 `.cluster` 的位置
- [ ] 能解释为什么 `relaxed` 不够、`acquire/release` 够
- [ ] 能说出 `volatile` 与 atomic 的本质区别
- [ ] 能解释 `fence.sc` 为什么影响性能
- [ ] 知道 `syncscope("system")` 的兼容性怪癖，以及 `cluster` 的降级条件

## 延伸阅读

- **[PTX ISA: Memory Consistency Model](https://docs.nvidia.com/cuda/parallel-thread-execution/#memory-consistency-model)** — NVIDIA
  - 读「Morally Strong Operations」「Causality Order」「Scoped Synchronization」三节；本章术语的权威定义。
- **[CUDA C++ Programming Guide: Memory Fence Functions](https://docs.nvidia.com/cuda/cuda-c-programming-guide/#memory-fence-functions)** — NVIDIA
  - 读 `__threadfence*` 三节，对照本章的 `membar.*`。
- **[CUDA C++ Programming Guide: Atomic Functions](https://docs.nvidia.com/cuda/cuda-c-programming-guide/#atomic-functions)** — NVIDIA
  - 看 `atomicAdd_system` 等 system-scope 变体，理解 `.sys` 的使用场景。
- **[PTX Writers Guide to Interoperability: Atomic ABI](https://docs.nvidia.com/cuda/ptx-writers-guide-to-interoperability/atomic-abi.html)** — NVIDIA
  - 解释为什么 `seq_cst` 要被拆成 `fence.sc` + `atom.acquire`；LLVM 源码注释也直接引用这份文档。
- **[compute-sanitizer](https://docs.nvidia.com/cuda/compute-sanitizer/)** — NVIDIA
  - 读 `racecheck` 一节；弱内存 bug 的实用检测手段。

内存模型讲的是「语义契约」，下一章把契约落到比特：SASS 指令到底怎么编码、控制位从哪来。

→ [Ch 19 SASS 编码与调度控制位](./19-sass-encoding-and-control-bits.md)
