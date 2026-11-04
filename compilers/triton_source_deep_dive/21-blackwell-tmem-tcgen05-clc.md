# Ch 21 — Blackwell：TMEM、tcgen05 与 CLC

> Blackwell 把累加器从寄存器搬进了 TMEM，又把“下一个 CTA 是谁”交给了硬件。

## 目标

读完本章，你应该能回答：

- TMEM 到底是什么内存？为什么它不能当成 shared memory 用？
- `ttng.tc_gen5_mma` 的 A/B/D 分别允许放在哪里？累加器为什么是 `memdesc` 而不是 `tensor`？
- 一个 `tl.dot` 在 SM100 上会变成哪几条 TTGIR op？累加器从分配到读回的完整生命周期是什么？
- CLC 解决什么问题？`clc=True` 把 kernel 的控制流改成了什么形状？为什么本章所有 Blackwell 运行证据都只能标 `guided`？


## 环境/版本

**实测**：

| 项目 | 值 |
|---|---|
| 源码 | `/home/ubuntu/aicomp/triton` |
| commit | `a972119e92a27b445cd160017ea461e56148590c` |
| 版本 | `3.9.0`（`python/triton/__init__.py:2`） |
| GPU | Tesla T4，compute capability 7.5（`sm_75`） |
| 驱动 / CUDA | 535.216.01 / 12.4 |
| 可用 Python | 仓库内 `.venv`，Python 3.12.16，`triton 3.9.0` + `torch 2.5.1+cu124` |

**实测**（在 T4 上触发后端 capability 检查）：

```text
clc=True requires NVIDIA SM100+ (Blackwell); current target is sm_75
num_ctas > 1 requires NVIDIA SM90+ (Hopper). Current target is sm_75. This configuration will fail. Please set num_ctas=1 or target an SM90+ GPU.
```

两条错误分别来自 `third_party/nvidia/backend/compiler.py:241-242` 和 `244-247`。

**源码依据**：

- `third_party/nvidia/backend/compiler.py:113-116` 把 capability 拼成 `sm_{capability}`，T4 是 `sm_75`；
- `include/triton/Dialect/TritonNvidiaGPU/IR/TargetFeatures.h:62-65` 的 `supportsTcgen05()` 只在 `capability/10 == 10 || == 11` 时为真（sm100–sm119），sm120 被排除；
- 同文件 `66-69`：只有 sm107（Rubin）支持 exclusive TMEM alloc；TMEM 列数上限 512（sm100）或 576（sm107）。

**guided 声明**：本章出现的 TTGIR 片段取自仓库自带 lit 测试的真实语法（会注明行号），PTX 片段由 lowering 源码的 opcode 拼接逻辑推导（会注明函数与行号）。它们都不是在 Blackwell 上跑出来的输出。

## 为什么需要这个

Blackwell 之前，Tensor Core 的累加器一直在寄存器里。Hopper 的 WGMMA 已经能让 A/B 直接从 shared memory 读入，但结果仍写回 warpgroup 寄存器：`ttng.warp_group_dot` 的 `$c` 是 `TT_FpIntTensor`，结果 `$d` 也是 tensor（`include/triton/Dialect/TritonNvidiaGPU/IR/TritonNvidiaGPUOps.td:240-250`）。

这带来两个硬约束：

1. **寄存器压力**。一个 `128x256` 的 f32 累加器有 32768 个 32-bit 值；分到 128 个线程上是每人 256 个寄存器，超过单线程上限。真实 kernel 只能缩小 tile，或把累加器拆块、反复 spill。
2. **数据往返**。K 循环里累加器一直活着，warpgroup 的寄存器被它钉死，warp specialization 想把 producer/consumer 分开更难。

Blackwell 的答案是：把累加器放到一块独立的、由 MMA 直接读写的片上存储，也就是 TMEM；寄存器只负责把结果取出来做 epilogue。CLC 解决另一个问题：**尾效应与启动开销**。静态 grid 下，提前做完的 CTA 会让 SM 空着，grid 很大时最后一批 CTA 又形成长尾；CLC 允许已在运行的 CTA 向硬件“取消”一个尚未启动的 cluster 并接管它的 program id，于是 kernel 可以在 grid 很小时自己“领活干”。

## 先建立直觉

三代 Tensor Core 路径在 IR 上的差别：

| 代际 | 代表 op | 累加器位置 | 操作数来源 | 完成同步 |
|---|---|---|---|---|
| Ampere（MMAv2） | `tt.dot` + `#mma` encoding | 寄存器（distributed tensor） | 寄存器（`ldmatrix` 搬入） | 同步指令 |
| Hopper（MMAv3/WGMMA） | `ttng.warp_group_dot` | 寄存器（`$c` 是 tensor） | A 可来自寄存器或 SMEM，B 来自 SMEM | `warp_group_dot_wait` |
| Blackwell（MMAv5/tcgen05） | `ttng.tc_gen5_mma` | **TMEM**（`$d` 是 `memdesc`） | A 来自 SMEM 或 TMEM，B 来自 SMEM | `tc_gen5_commit` + mbarrier |

Blackwell 一个 SM 上大致有三块程序员可见的存储：

```text
寄存器文件     每线程 255 个 32-bit 寄存器（上限）        延迟最低，容量最小
shared memory  每 SM 228KB（可配置）                      block 内共享，可字节寻址
TMEM           128 行 × 512 列 × 4B = 256KB               仅 tcgen05 指令可访问
```

TMEM 的关键点不是“又一块片上内存”，而是：

- **不是字节可寻址**。没有 `ld.global`/`ld.shared` 能读它，只有 `tcgen05.ld` / `tcgen05.st` / `tcgen05.cp` 和 MMA 本身。
- **行固定 128 行**，一行对应一个 lane；4 个 warp 的 128 个 lane 正好覆盖 128 行。
- **分配是 kernel 级的**：入口用 `tcgen05.alloc` 拿基地址，整个 kernel 共用，退出时 `tcgen05.dealloc`。

## 核心概念

### 1. TMEM 的类型表达

TMEM buffer 在 Triton IR 里是一个 `!ttg.memdesc`，带 memory space 和专用 encoding：

```mlir
#tmem_f32 = #ttng.tensor_memory_encoding<blockM = 128, blockN = 128, colStride = 1>
%0 = ttng.tmem_alloc %cst :
    (tensor<128x128xf32, #blocked>)
    -> !ttg.memdesc<128x128xf32, #tmem_f32, #ttng.tensor_memory, mutable>
```

这段语法来自 `test/TritonNvidiaGPU/test_tensor_memory_allocation.mlir:6` 和 `:25`（**guided**，但语法是仓库测试原文）。

- `#ttng.tensor_memory` 是 memory space，声明“这块 buffer 在 TMEM 里”（`TritonNvidiaGPUAttrDefs.td:45-69`）。它同时规定物理分布：按 `blockM × blockN` 分块，每块铺到 128 行，先沿 M 再沿 N 排。
- `#ttng.tensor_memory_encoding<blockM, blockN, colStride, CGALayout, twoCTAs, fp4Padded>` 是 layout encoding（同文件 `96-133`）。`colStride` 是同一行内相邻元素的列步长；`colStride = 1` 表示紧密排列，大于 1 时中间的空洞是未定义值。

注意第四个参数 `mutable`。TMEM 分配默认 mutable，因为 MMA 要往里写；`TMEMStoreOp::verify()` 会拒绝往 immutable alloc 里写（`lib/Dialect/TritonNvidiaGPU/IR/Ops.cpp:1711-1713`）。

### 2. tcgen05 MMA 的 IR 形状

`ttng.tc_gen5_mma` 定义在 `TritonNvidiaGPUOps.td:751-813`：

```text
a:  TTG_MemDescType      // SMEM 或 TMEM
b:  TTG_MemDescType      // 只能是 SMEM
d:  TTG_MemDescType      // TMEM，累加器
acc_dep: optional token  // 累加器的 RAW 依赖
useD: i1                 // 是否把 D 的旧值加进来（D += A*B）
pred: i1                 // 整个 MMA 的谓词
barriers + barrier_preds // 异步完成时 arrive 的 mbarrier
is_async / two_ctas / multicast / is_unsigned
```

一个真实的 TTGIR 例子（**guided**，语法取自 `test/TritonNvidiaGPU/canonicalize.mlir:102-105`）：

```mlir
ttng.tc_gen5_mma %a, %b, %d, %false, %false, %bar[%true] {is_async}
  : !ttg.memdesc<128x128xf16, #shared_a, #smem>,
    !ttg.memdesc<128x128xf16, #shared_b, #smem>,
    !ttg.memdesc<128x128xf32, #tmem, #ttng.tensor_memory, mutable>,
    !ttg.memdesc<1xi64, #local_barrier, #smem, mutable>
```

和 Hopper 的 `warp_group_dot` 对比，差别一眼可见：没有“结果 tensor”，只有第三个 memdesc `%d`；也没有寄存器累加器，`useD` 是 i1，告诉硬件“要不要读 D 的旧值”。

### 3. TMEM 侧的 load/store/copy

| op | 方向 | 作用 |
|---|---|---|
| `ttng.tmem_load` | TMEM → 寄存器 tensor | 读累加器；可选 `redOp` 做 N 方向归约 |
| `ttng.tmem_store` | 寄存器 tensor → TMEM | 写初值 / 写回 |
| `ttng.tmem_copy` | SMEM → TMEM | `tcgen05.cp`，常用于把 scales 搬进 TMEM |
| `ttng.tmem_subslice` | view | 取 TMEM buffer 的子视图 |
| `ttng.tmem_wait` | 同步 | 等本 warp 之前的 `tcgen05.ld/st` |
| `ttng.tc_gen5_commit` | 同步 | 让 mbarrier 跟踪之前所有异步 tcgen05 op |

定义位置：`TritonNvidiaGPUOps.td:941-956`（wait）、`957-1057`（load）、`1058-1091`（store）、`1092-1118`（alloc）、`1119-1142`（subslice）、`1143-1207`（copy）、`893-940`（commit）。

`tmem_load` 的 verifier 值得单独读（`Ops.cpp:1730-1776`）：源必须是 `#ttng.tensor_memory` + `TensorMemoryEncodingAttr`；`redOp` / `abs` / `NaN` / `red` 四者必须一致；`abs` 和 `NaN` 只允许 f32。

### 4. CLC 的五个 op

CLC 的 op 在 `TritonNvidiaGPUOps.td:128-227`：

| op | 语义 |
|---|---|
| `ttng.clc_try_cancel_sync` | 前端用的“同步”形式：发一次 try_cancel，返回 `tensor<2xi64>` 响应 |
| `ttng.clc_try_cancel` | 异步形式：结果写到 SMEM，mbarrier 完成时 arrive |
| `ttng.clc_load_result` | 从 SMEM 读 128-bit 响应到 `i128` |
| `ttng.clc_is_canceled` | 解码响应，返回 i1：是否成功取消了一个 cluster |
| `ttng.clc_get_program_id` | 解码响应，返回被取消 cluster 在 x/y/z 上的 program id |

`clc_try_cancel` 的约束写在描述里：结果 buffer 必须是 16 字节对齐的 `2xi64` shared memdesc，mbarrier 必须 8 字节对齐（`TritonNvidiaGPUOps.td:147-152`）。

### 5. 谁把 `tl.dot` 变成 tcgen05

`tl.dot` 先到 TTIR 的 `tt.dot`，再由 `lib/Dialect/TritonGPU/Transforms/AccelerateMatmul.cpp` 选 MMA 版本：

```cpp
if (computeCapability < 75)        versionsSupported = {1};
else if (computeCapability < 90)   versionsSupported = {2};
else if (computeCapability < 100)  versionsSupported = {3, 2};
else if (computeCapability < 120)  versionsSupported = {5, 2};
```

（`AccelerateMatmul.cpp:46-66`）

`versionMajor == 5` 由 `BlockedToMMAv5`（同文件 `757` 起）处理，生成 `ttng.tc_gen5_mma` 和 TMEM alloc。注意 `isUnsupportedMMAv5Int8Dot`（`37-43`）：只有 sm100 的 `supportsI8Tcgen05MMA()` 为真，sm103/sm110 的 int8 dot 会退回 `{2}`。

**实测**（T4，sm_75）：`tt.dot` 在 TTGIR 里仍是 `#blocked` encoding，PTX 里 `mma` 指令数为 0，`fma.rn.f32` 有 1024 条。Triton 3.9 在 sm_75 上直接走 FMA，不生成 MMAv2。源码依据 `AccelerateMatmul.cpp:516-520`：

```cpp
if (computeCapability < 80) {
  dotOp.emitRemark() << "Dot op using MMA for compute capability "
                     << computeCapability << " has been deprecated. It falls back to the FMA path.";
  return failure();
}
```

这条 `< 80` 的早退发生在 `getMMAVersionSafe` 之前，所以即使函数表里给 `< 90` 列了 `{2}`，sm_75 也拿不到 MMAv2。

## 底层机制

### 1. TMEM 分配：二维 bitmap 上的 first-fit

`TensorMemoryAllocation.cpp` 是一个独立 module pass，核心是 512 列 × 2 个 64 行块的 bitmap：

- `allocGranularity = 64`（`29`）：分配粒度 64 行；
- `MemoryBitMap::kNumRows = 2`（`116`）：把 128 行压成 2 个 64 行单元；
- `findFirstFit`（`62-103`）先按 `columnAlignment` 对齐列，再在行单元里找第一个放得下的位置；
- `columnAlignment = 4`（`368`）：列偏移必须 4 列对齐。

分配大小走 LinearLayout，不是按 shape 直接乘：

```cpp
auto ll = toLinearLayout(memDescType);
int nRow = ll.getInDimSize(kRow);
int nCol = divideCeil(ll.getInDimSize(kCol) * bitwidth, 32);
if (ll.getBasis(kRow, Log2_32(16)) == ArrayRef{0, 0})
  nRow /= 2;                          // 只有 16 行/块的布局用 64 行
if (memDescType.getRank() > 2)
  nCol *= memDescType.getDimSize(0);  // multibuffering
```

（`lib/Dialect/TritonNvidiaGPU/IR/Dialect.cpp:134-157`）

这解释了两件容易误解的事：

1. TMEM 的“列”是 **32-bit 列**，不是元素列。f32 的 `colStride=1`、f16 的 `colStride=2`，都让 `getInDimSize(kCol) * bitwidth / 32` 落在 128 列。
2. 测试的 CHECK 行比背公式可靠：`test_tensor_memory_allocation.mlir:24-31` 显示四个 alloc 的列偏移是 0、128、256、320。

分配完成后，pass 给每个 `tmem_alloc` 挂两个属性：

```mlir
{tensor_memory_col_offset = 0 : i32, tensor_memory_row_offset = 0 : i32}
```

行偏移是 `startRow * 16`（`TensorMemoryAllocation.cpp:377`），所以 64 行块的第二个位置是 `row_offset = 16`。这就是为什么测试里 `%4` 和 `%5` 都是 64×128 f16，却分别落在 `(col=0,row=0)` 和 `(col=0,row=16)`（`test_tensor_memory_allocation.mlir:38-41`）。

最后 pass 把总列数写成 module attr `ttg.tensor_memory_size = 512`（`TensorMemoryAllocation.cpp:433`；测试 CHECK 在 `:13`）。

### 2. 地址编码：row 在高 16 位，col 在低 16 位

TMEM 地址是 32-bit 值，不是指针：

```text
bits 31..16  row
bits 15..0   col
```

证据在 lowering 里到处可见：

```cpp
int staticOffset = col | (row << 16);                     // TensorMemoryToLLVM.cpp:289-293
Value allocAddress = b.add(baseInt, b.i32_val(colOffset | rowOffset << 16));  // 同文件:518
```

`getTMemSubSliceOffset` 也返回同样的编码，最后一行是 `return offsetCol | offsetRow << 16;`（`Dialect.cpp:159-185`）。

### 3. warp 与行的映射：为什么 TMEM ld/st 要 4 个 warp 一起做

`lowerTMemLdSt` 里有一行关键代码：

```cpp
Value warpId = WarpIdOp::create(rewriter, loc);
auto warpIdInGroup = b.and_(warpId, b.i32_val(3));
tmemBase = b.add(tmemBase, b.shl(warpIdInGroup, b.i32_val(5 + 16)));
```

（`TensorMemoryToLLVM.cpp:273-276`）

`5 + 16` 的意思是：warp id 左移 5 位得到 32 行，再左移 16 位放进地址的 row 字段。于是：

```text
warp 0 → 行 0..31      warp 1 → 行 32..63
warp 2 → 行 64..95     warp 3 → 行 96..127
```

这解释了 TMEM 的三个使用约束：

- `tcgen05.ld/st` 是 **warp 级**指令，一个 warp 的 32 个 lane 覆盖 32 行；
- 一个 128 行 buffer 需要 4 个 warp 协同，缺一个就有 32 行没人处理；
- `numWarps > 4` 时，多出的 warp group 通过 `reps.getInDimSize(kWarp) > 4` 分支再加一次 row/col 偏移（`TensorMemoryToLLVM.cpp:279-286`）。

### 4. 累加器的生命周期

```text
tl.dot(a, b, acc)
  → tt.dot                          (TTIR)
  → ttng.tc_gen5_mma + tmem_alloc   (AccelerateMatmul: BlockedToMMAv5)
  → 循环里：tc_gen5_mma(is_async) → tc_gen5_commit(barrier)
  → ttng.wait_barrier
  → ttng.tmem_load                  (读回 distributed tensor)
  → epilogue（elementwise / store）
  → kernel 退出：tcgen05.dealloc
```

几个容易漏掉的点：

**a) 累加器初值。** `useD` 为真时硬件读 D 的旧值；为假时 D 被覆盖。`OptimizeAccumulatorInit` 会把“循环前的零初始化”折叠成 `useD = false`，省掉一次 TMEM 写（`compiler.py:346`）。

**b) 异步完成。** `is_async` 的 MMA 不阻塞，`tc_gen5_commit` 让 mbarrier 跟踪之前所有异步 tcgen05 op，arrive 计数为 1（`TritonNvidiaGPUOps.td:901-903`）。commit 组是**顺序**的：即使后一个 commit 组为空，它的 arrive 也一定在前一个之后（同文件 `906-918`）。

**c) 读回前的等待。** `tmem_load` 是同步指令，但同一 warp 之前发出的 `tmem_store` 不一定完成；`ttng.tmem_wait` 对应 `tcgen05.wait::ld/st.sync.aligned`（`TritonNvidiaGPUOps.td:947-955`），只同步本 warp，不等异步 MMA。

**d) token 的生命周期。** `acc_dep` / `token` 是 `!ttg.async.token`，表达 TMEM buffer 的 RAW/WAR；pipelining 结束后由 `RemoveTMEMTokens` 删掉（`compiler.py:357`）。TTGIR 里 token 消失不是分析没跑，是这一步主动清的。

**e) 释放。** `tcgen05.alloc` 在 kernel 入口执行一次，只有前 32 个线程参与：

```cpp
Value threadId = NVVM::ThreadIdXOp::create(rewriter, loc, i32_ty);
Value pred = b.icmp_ult(threadId, b.i32_val(32));
```

（`third_party/nvidia/lib/NVGPUToLLVM/NVGPUToLLVMPass.cpp:625-628`）

随后 `tcgen05.relinquish_alloc_permit`（`558-566`）表示“不再申请”，退出时在每个 `llvm.return` 前插 `tcgen05.dealloc`（`568-592`）。总大小超过硬件上限时，`initTensorMemory` 直接发 `LLVM::Trap`，让 python 侧抛 `OutOfResources`（`594-617`；python 侧在 `python/triton/compiler/compiler.py:475-481`）。

### 5. `tcgen05.mma` 的 PTX 是怎么拼出来的

`createGen5MMA` 在 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/DotOpToLLVM/MMAv5.cpp:386-410`：

```cpp
std::string opcode =
    "tcgen05.mma.cta_group::" + std::to_string(twoCTAs ? 2 : 1) +
    ".kind::" + kind.str() + getCollectorModifier(reuse.a, "a") +
    getCollectorModifier(reuse.b, "b");
auto *accOp = ptxBuilder.newAddrOperand(d.base, "r", *d.offset);
auto *aOp  = a.offset ? ptxBuilder.newAddrOperand(a.base, "r", *a.offset)
                      : ptxBuilder.newOperand(a.base, "l");
auto *bOp  = ptxBuilder.newOperand(b, "l");
auto *instDescOp = ptxBuilder.newOperand(inst.descriptor, "r");
```

操作数顺序是 `acc, a, b, instDesc, [scaleA, scaleB], useInitAcc`（`400-406`）。`a` 有 offset 时用 `"r"`（32-bit TMEM 地址），没有时用 `"l"`（64-bit SMEM 描述符）——这一行就是“A 可以在 SMEM 或 TMEM”在 lowering 里的体现。

**guided**：按这段拼接逻辑，指令形状是

```text
tcgen05.mma.cta_group::1.kind::f16 [$acc], $a, $b, $idesc, $useInitAcc;
```

具体寄存器号和描述符位域由 ptxas 决定；这里不伪造完整指令。commit 的 opcode 在 `412-445`：

```text
@$0 tcgen05.commit.cta_group::1.mbarrier::arrive::one.shared::cluster.b64 [$1];
```

有 multicast 时追加 `.multicast::cluster` 和一个 16-bit mask（`433-440`）。

### 6. CLC 把 kernel 改写成 while 循环

`ToCLC` pass（`lib/Dialect/TritonNvidiaGPU/Transforms/ToCLC.cpp`）在 TTGIR 阶段跑，前提是 `clc=True`（`compiler.py:316-317`）。它做五件事：

1. 收集 kernel body 里所有 `tt.get_program_id`，按 axis 分组（`63-72`）；
2. 在 `tt.return` 前建 `scf.while`，state 是“每个用到的 axis 的 pid + 一个 active 标志”（`74-95`）；
3. 把原 kernel body 整体搬进 `after` 区域（`108-109`）；
4. 把 body 里的 `tt.get_program_id` 全部替换成 loop block argument（`111-117`）；
5. 每次迭代末尾 `clc_try_cancel_sync` → `clc_load_result` → `clc_is_canceled`；成功就用 `clc_get_program_id` 取新 pid 继续，否则退出（`119-142`）。

**guided**：改写后的控制流形状（按 `ToCLC.cpp:82-142` 推导，不是实际 dump）：

```mlir
%loop = scf.while (%pid0 = %initial_pid, %active = %true) {
  scf.condition(%active) %pid0, %active
} do {
^bb0(%pid0: i32, %active: i1):
  %resp = ttng.clc_try_cancel_sync : tensor<2xi64>
  %buf  = ttg.local_alloc %resp : ...  // 16B 对齐
  <原 kernel body，tt.get_program_id 已替换为 %pid0>
  %r    = ttng.clc_load_result %buf : i128
  %next = ttng.clc_is_canceled %r : i1
  %pid1 = scf.if %next -> i32 { ttng.clc_get_program_id %r, x } else { %pid0 }
  scf.yield %pid1, %next
}
```

随后 `CLCLowering`（`CLCLowering.cpp:41-109`）把“同步”形式拆成真正的异步序列：

```text
ttng.init_barrier
ttng.barrier_expect 16 bytes
ttng.clc_try_cancel        // 异步，完成时 arrive mbarrier
ttng.wait_barrier          // phase 每次迭代翻转
ttng.clc_load_result
```

phase 处理很讲究：loop 多带一个 i32 phase 参数，yield 时 `phase ^ 1`（`75-89`）。mbarrier 的 phase 必须和 try_cancel 次数对齐，否则第二次迭代会等在一个永远不会翻转的 phase 上。

`CLCLowering` 目前只支持 `num_ctas == 1`：

```cpp
if (ttg::lookupNumCTAs(issue) != 1) {
  issue.emitError("CLC lowering supports num_cta=1 at the moment");
  return signalPassFailure();
}
```

（`CLCLowering.cpp:124-127`）

PTX 层的 `CLCTryCancelOpConversion` 其实已经写了 multicast 分支（`BarrierOpToLLVM.cpp:507-518`），只是 pass 层还没放开。这是“lowering 比 pass 走得远”的典型例子。

### 7. Blackwell 的 TTGIR pass 顺序

`compiler.py:342-357` 是 `capability/10 >= 10` 的分支：

```text
optimize_accumulator_init → hoist_tmem_alloc(false) → promote_lhs_to_tmem
→ assign_latencies → schedule_loops → warp_specialize → pipeline
→ optimize_partition_warps → combine_tensor_select_and_if
→ hoist_tmem_alloc(true) → remove_tmem_tokens
```

之后所有目标共享（`366-373`）：

```text
optimize_tmem_layouts → tmem_load_reduce → [sm90+] tma_lowering
→ [sm100+] lower_clc → remove_layout_conversions → interleave_tmem
→ reduce_data_duplication → reorder_instructions
```

`make_llir`（`422-467`）里与 TMEM 相关的顺序是：`allocate_shared_memory_nv → allocate_tensor_memory → proxy_fence_insertion → membar → tmem_barrier_insertion → optimize_mbarrier_arrivals → cluster_barrier_mbar_allocator → allocate_global_scratch_memory → to_llvmir → warp_specialize_to_llvm → nvgpu_to_llvm → nvvm_to_llvm`。

两个“为什么”：**`allocate_tensor_memory` 必须在 `to_llvmir` 之前**，它要把列/行偏移算好，lowering 才能把偏移直接折进地址（`TensorMemoryAllocOpConversion` 读属性，不重新算）；**`tmem_barrier_insertion` 也在 `to_llvmir` 之前**，它插的是 `ttng.tmem_wait` 这类 IR 级同步，到 LLVM 才变成 `NVVM::Tcgen05WaitOp`（`TensorMemoryToLLVM.cpp:409-422`）。

## 进一步用法/重要变体

### 1. Scaled MMA

`ttng.tc_gen5_mma_scaled`（`TritonNvidiaGPUOps.td:815-891`）多带两个 scale memdesc 和两个 `ScaleDotElemTypeAttr`，对应 MXFP/NVFP4 的块缩放：

```mlir
ttng.tc_gen5_mma_scaled %A_sh, %B_sh, %C_tmem, %A_scale_sh, %B_scale_sh,
    %true, %pred lhs = e5m2 rhs = e5m2, %barrier[%true] {is_async}
```

（**guided**，语法取自 `test/TritonNvidiaGPU/mma_lowering.mlir:28`。）

scale 在 TMEM 里用另一种 encoding：`#ttng.tensor_memory_scales_encoding`（`AttrDefs.td:135-168`）。布局不是普通二维块，而是“32×4×4B 的块复制 4 份铺满 128 行”。`tmem_copy` 的描述里花了很大篇幅解释 SMEM 侧要先做 `trans(0,3,2,1,4).reshape(...)`（`TritonNvidiaGPUOps.td:1170-1200`）。只记一句：**scale 的物理布局和逻辑布局不是同一个 reshape，编译器把它折叠进了 `tmem_copy` 的语义里**。

### 2. two-CTA MMA

`two_ctas` 让一条 MMA 跨两个连续 CTA 执行。IR 上只是 `{two_ctas}`，但 verifier 会检查 RHS 的 CGA layout 第一个 basis 必须是 `[0,1]` 或 `[0,0]`（`Ops.cpp:1196-1200`），lowering 里 `cta_group::2` 和 lead-CTA predicate 也要对上（`MMAv5.cpp:391-394`、`TensorMemoryToLLVM.cpp:665-670`）。T4 上 `num_ctas=2` 在 python 层就被拒（**实测**，见章首）。

### 3. `OptimizeTMemLayouts` 的五个 pattern

`OptimizeTMemLayouts.cpp` 里有五个 rewrite：

| pattern | 行号 | 做什么 |
|---|---|---|
| `TMemSplitLoadPattern` | `60-142` | `tmem_load → reshape → trans → split` 拆成两个 `tmem_subslice + tmem_load` |
| `TMemStoreJoinPattern` | `144-200` | `join → trans → reshape → tmem_store` 拆成两个 `tmem_store` |
| `TMemLoadReducePattern` | `207-255` | 8 warp 且沿 N 归约时，把 load 布局改成沿 M 分布 |
| `TMemFromSharedMemPattern` | `259-324` | 为 16-bit `local_load → tmem_store` 选 `16x256b` 原子 |
| `TMemToSharedMemPattern` | `326-407` | 为 `tmem_load → local_store` 选 `16x256b`，让 stmatrix 可用 |

共同动机：TMEM 访问原子（`32x32b`、`16x64b`、`16x128b`、`16x256b`、`16x32bx2`）决定指令数和是否需要额外 `tcgen05.wait`。布局选错不会算错，但会多出几倍 ld/st。

### 4. 手写路径：Gluon

`python/triton/experimental/gluon/language/nvidia/blackwell/` 暴露了 `tmem`、`tcgen05`、`clc` 的显式 API。例如 `clc.py:25-36` 的 `try_cancel(result, barrier)` 直接调 `create_clc_try_cancel`，`clc_result.is_canceled()` / `.program_id(dim)` 对应两个 query op（`89-112`）。想在没有自动调度的情况下验证 CLC 语义，这是最直接的路。

## 对比与取舍

### TMEM vs shared memory

| 维度 | TMEM | shared memory |
|---|---|---|
| 容量 | 128×512×4B = 256KB/SM | 228KB/SM（可配置） |
| 寻址 | 32-bit 行/列地址，非字节寻址 | 字节寻址 |
| 访问指令 | `tcgen05.ld/st/cp`，warp 级 | `ld.shared` / `st.shared` / `ldmatrix` |
| 谁能写 | MMA、`tcgen05.st`、`tcgen05.cp` | 任意线程 |
| 分配 | kernel 入口 `tcgen05.alloc`，整 kernel 生命周期 | pass 静态分配，按需复用 |
| 典型用途 | 累加器、A 操作数、scales | 操作数 staging、跨 warp 交换、reduce scratch |


### WGMMA vs tcgen05

| 维度 | Hopper WGMMA | Blackwell tcgen05 |
|---|---|---|
| IR op | `ttng.warp_group_dot` | `ttng.tc_gen5_mma` |
| 累加器 | 寄存器 tensor `$c` | TMEM memdesc `$d` |
| 发射者 | 一个 warpgroup | 单线程（elect） |
| 完成 | commit group / `warp_group_dot_wait` | `tc_gen5_commit` + mbarrier |
| 结果读回 | 无（本来就在寄存器） | 必须 `tmem_load` |
| 寄存器压力 | 高 | 低 |

### CLC vs 静态 persistent kernel

| 方案 | 工作分配 | 优点 | 缺点 |
|---|---|---|---|
| 普通 grid | launch 时静态 | 简单、可预测 | 尾效应、CTA 数受 tile 数限制 |
| 软件 persistent loop | `while pid < num_tiles: pid += gridDim` | 不需要新硬件 | 负载不均时仍空转 |
| CLC | 硬件队列，运行中“偷”未启动 cluster | 动态均衡、启动开销低 | 仅 SM100+；当前只支持 `num_ctas=1` |

## 踩雷集锦

1. **把 TMEM 当成 shared memory 的替代品。**
   TMEM 只能通过 `tcgen05.*` 访问，普通 `tt.load`/`tt.store` 到不了。`TypeConverter` 里 TMEM memdesc 被降成一个指针，而且借用了 `addrspace(3)`（shared）的类型（`TensorMemoryToLLVM.cpp:534-538`）。看到 `addrspace(3)` 就以为它是 SMEM，会在读 LLIR 时得出完全错误的结论。

2. **以为 `tmem_alloc` 的偏移是“按 shape 顺序”排的。**
   实际是 bitmap first-fit，列必须 4 对齐，行只有 0/16 两个位置（`TensorMemoryAllocation.cpp:366-377`）。两个同 shape 的 alloc 完全可能一个在 `(0,0)`、一个在 `(0,16)`，而不是 `(0,0)`、`(128,0)`。`test_tensor_memory_allocation.mlir:38-41` 就是这种情况。

3. **在 `num_ctas > 1` 时开 CLC。**
   `CLCLowering` 直接报 `CLC lowering supports num_cta=1 at the moment`（`CLCLowering.cpp:124-127`）。虽然 `clc_try_cancel` 的 lowering 有 multicast 分支，但 pass 层没放开；这不是 kernel 写错。

4. **忘了 CLC 的 phase 翻转。**
   `CLCLowering` 给 while 加了一个 i32 phase，yield 时 `phase ^ 1`（`75-89`）。手写 CLC 或用 Gluon 拼时，phase 不翻，第二次 `wait_barrier` 会等一个已经用过的 phase，表现为挂死而不是报错。

5. **以为 `tmem_wait` 会等 MMA。**
   `ttng.tmem_wait` 只等本 warp 之前的 `tcgen05.ld/st`，不等异步 MMA，也不同步其他 warp（`TritonNvidiaGPUOps.td:947-955`）。等 MMA 要用 `tc_gen5_commit` + `wait_barrier`。

7. **在 T4 上用 `TRITON_OVERRIDE_ARCH=sm100` 交叉编译。**
   **实测**：在 T4 上把 arch 覆盖成 `sm100` 编译一个 matmul，LLVM 在指令选择阶段直接 abort：`LLVM ERROR: Cannot select: intrinsic %llvm.nvvm.elect.sync`，进程 core dump。不要把它当成获得 Blackwell IR 的捷径；本章 guided IR 一律以 lit 测试和源码为准。

## 进阶

- 读 `TensorMemoryAllocation.cpp:120-185` 的 `getLiveIntervals` + `updateMap` + `allocFirstFit`，理解它如何处理 `memdesc_subslice` / `arith.select` / `scf.if` 的活跃区间。`getAlloc`（`187-270`）沿 SSA 链回溯到 `tmem_alloc`，遇到未处理的 branch 直接 `report_fatal_error`。
- 读 `OptimizeTMemLayouts.cpp:60-142`，对照 `getDefaultLayoutForTmemLdSt` 和 `TMemAccessAtom`，理解“为什么 split 之后每个 subslice 要重新选布局”。
- 读 `python/test/unit/language/test_clc.py:13-18` 的 IR 断言：`scf.while` 数量等于 warp-specialize partition 数加一，`ttng.clc_try_cancel` 只有一处。这是理解 CLC 与 warp specialization 如何共存的最短路径。

## 动手练习

1. **画生命周期**：给定 `128x256xf32` 累加器、K 循环 4 次、`num_stages=3`，画出 TMEM 分配、MMA、commit、load、dealloc 的时间线，标出哪些点需要 `tmem_wait`。
2. **算分配**：两个 `128x128xf32`、一个 `128x128xf16`（`colStride=2`）、一个 `64x64xf16`（`colStride=2`）依次 alloc，按 first-fit + 4 列对齐写出四个 `(col,row)`，对照 `test_tensor_memory_allocation.mlir:24-31`。
3. **读 verifier**：找出 `tc_gen5_mma` 对 A 是 TMEM 时的两条额外限制（`colStride` 和 `fp4Padded`），说明为什么只有 scaled MMA 能用 padded fp4。
4. **对照 PTX**：在 SM100 机器上用 `TRITON_KERNEL_DUMP=1` 跑一个 128×128×64 的 f16 matmul，把 `.ptx` 里所有 `tcgen05.*` 行抄下来，和 `MMAv5.cpp:391-394`、`412-445` 的 opcode 拼接对照。**guided**。

## 本章重点

- TMEM 是 Blackwell 独立的片上存储：128 行 × 512 列 × 4B，只能被 `tcgen05.*` 访问，不是 shared memory。
- `ttng.tc_gen5_mma` 的累加器是 TMEM memdesc；A 可来自 SMEM 或 TMEM，B 只能来自 SMEM。
- 累加器生命周期：`tmem_alloc` → `tc_gen5_mma` → `tc_gen5_commit`/`wait_barrier` → `tmem_load` → `tcgen05.dealloc`。
- TMEM 地址是 `row << 16 | col`；warp w 负责行 `32w..32w+31`，所以 128 行 buffer 需要 4 个 warp。
- CLC 把 kernel body 包进 `scf.while`，用 `clc_try_cancel` + mbarrier 动态领取未启动的 cluster；当前只支持 `num_ctas=1`。

## 自我检核

- 你能说出 TMEM 与 SMEM 的三个本质差别，并解释为什么累加器放 TMEM 能降低寄存器压力吗？
- 你能写出 `tmem_alloc` 的地址编码，并解释 `row_offset = 16` 对应多少行吗？
- 你能说清 `tc_gen5_commit`、`tmem_wait`、`wait_barrier` 三者各自等什么、不等什么吗？

## 延伸阅读

1. **`include/triton/Dialect/TritonNvidiaGPU/IR/TritonNvidiaGPUOps.td:751-1207`**
   - 读哪里：`TCGen5MMAOp`、`TCGen5CommitOp`、`TMEMWaitOp`、`TMEMLoadOp`、`TMEMStoreOp`、`TMEMAllocOp`、`TMEMSubSliceOp`、`TMEMCopyOp`。
   - 学什么：每个 op 的 operand/result/attr、verifier 声明，以及描述里写的硬件约束（对齐、minK、colStride）。
   - 与本章关系：本章所有 IR 形状的权威来源；先读描述，再读 verifier。

2. **`lib/Dialect/TritonNvidiaGPU/Transforms/TensorMemoryAllocation.cpp:299-434`**
   - 读哪里：`allocateTMem` 的 walk、row 约束、first-fit 循环、最终 `ttg.tensor_memory_size`。
   - 学什么：TMEM 分配的真实算法，以及“为什么偏移不是按 shape 顺序”。
   - 与本章关系：把“TMEM 是二维资源”从概念变成可计算的偏移。

3. **`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/TensorMemoryToLLVM.cpp:154-541`**
   - 读哪里：`createTensorMemoryStore`、`createTensorMemoryLoad`、`lowerTMemLdSt`、`TensorMemoryAllocOpConversion`。
   - 学什么：TMEM 地址如何变成 `tcgen05.st/ld` 的操作数；warp→row 映射；alloc 如何读属性。
   - 与本章关系：IR 到 PTX 的最后一段。

4. **`lib/Dialect/TritonNvidiaGPU/Transforms/ToCLC.cpp:53-143` 与 `CLCLowering.cpp:41-109`**
   - 读哪里：两段控制流改写。
   - 学什么：CLC 的 while 形状、phase 翻转、mbarrier expect/wait 的配对。
   - 与本章关系：CLC 不是一条指令，而是一次 CFG 重写。

## 下一章

下一章离开 Blackwell 专有路径，进入所有 NVIDIA 目标都要走的 `ConvertTritonGPUToLLVM`：conversion target 怎么划合法/非法、TypeConverter 怎么把 tensor 变成 struct、为什么先 lower function 再 lower op，以及 `UnrealizedConversionCastOp` 什么时候出现。

→ [Ch 22 TritonGPU → LLVM 的转换框架](./22-tritongpu-to-llvm-conversion.md)
