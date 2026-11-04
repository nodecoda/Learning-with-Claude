# Ch 24 — load/store/atomic 的 lowering

> 一条 `tt.load` 落到 GPU 上不是一个 `ld.global`，而是“每个线程、每个向量、每个谓词”拼出来的一组指令。

## 目标

读完本章，你应该能回答：

- `tt.load` 的 `mask`、`other`、`cache_modifier`、`eviction_policy` 分别在 lowering 的哪一步生效？
- 为什么同一个 `tl.load` 有时生成 `ld.global.b32`，有时生成 `ld.global.v4.b32`，有时又退回四条带谓词的标量 load？
- `tl.atomic_add` 什么时候生成 `red.global...`，什么时候必须生成 `atom.global...`？`sem` 和 `scope` 如何进入 PTX？
- `other` 在 PTX 里是 `select`，还是寄存器初始化？
- `tt.load` 的地址空间为什么总是 `.global`？shared memory 的 load/store 走的是哪条路径？
- 对齐信息（`tl.multiple_of`）和 mask 的 constancy 为什么会改变向量宽度？

本章的主线是 `tt.load` / `tt.store` / `tt.atomic_*` 从 TTGIR 到 LLVM dialect、再到 inline PTX 的过程。重点不是背 PTX，而是理解“张量级 op”如何被拆成“线程级指令”。

## 环境/版本

- Triton 源码：`/home/ubuntu/aicomp/triton`，commit `a972119e92a27b445cd160017ea461e56148590c`，版本 3.9.0。
- GPU：Tesla T4，`sm_75`，驱动 535.216.01，CUDA 12.4。
- 仓库源码没有在本机构建；系统 Python 也没有 `triton`。
- 本章标 **实测** 的 IR/PTX/SASS，来自 `/tmp` 下独立 venv 安装的 Triton **3.8.0** wheel，目标 `cuda:75`。它用于观察 lowering 的形态，不作为 3.9.0 的逐字输出。
- 标 **源码依据** 的结论来自 3.9.0 仓库，行号用 `nl -ba` 核对。
- 标 **guided** 的内容是 T4 不支持或本机无法端到端验证的：TMA/async copy（sm90+）、Hopper/Blackwell、AMD 后端。

**版本导航（源码依据）**：本章重点文件在 3.9.0 中位于
`third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/LoadStoreOpToLLVM.cpp`（1887 行），
而不是基类目录下的同名文件。基类目录现在是
`lib/Conversion/TritonGPUToLLVM/MemoryOpToLLVM.cpp`（718 行），负责 `local_alloc/local_load/local_store` 等。
AMD 对应文件是 `third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp`（guided）。

## 为什么需要这个

在 TTIR 里，`tt.load` 看起来像一个“整块读”：

```mlir
%x = tt.load %ptr, %mask, %other : tensor<256x!tt.ptr<f32>>
```

但 GPU 没有“整块”指令。一个 CTA 有 128 个线程，每个线程只拥有 2 个元素；硬件只认识
`ld.global`、`ld.global.v4`、`ld.shared` 这些线程级指令。lowering 必须回答四个问题：

1. 这个线程拥有哪些元素？——由 layout 的 `register/lane/warp/block` 基决定。
2. 这些元素在地址上是否连续、是否对齐？——由 AxisInfo 的 contiguity/divisibility 决定。
3. 哪些元素需要被 mask 掉？——mask 被拆成每个线程的谓词。
4. 被 mask 掉的元素返回什么？——`other` 被广播、转型，再写进目标寄存器。

这四件事任何一件判断错，结果不是“慢一点”，而是读错地址、越界、或者 masked-off 元素变成垃圾值。理解这一层，才能解释为什么加一行 `tl.multiple_of` 会让 PTX 从 4 条 `ld.global.b32` 变成 1 条 `ld.global.v4.b32`。

## 先建立直觉

先看一条最小 kernel：

```python
@triton.jit
def masked_load(x_ptr, y_ptr, n, BLOCK: tl.constexpr):
    offs = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    m = offs < n
    x = tl.load(x_ptr + offs, mask=m, other=0.0, cache_modifier=".cg")
    tl.store(y_ptr + offs, x * 2.0, mask=m)
```

它在 TTGIR 里仍然是一个张量 op：

```mlir
%x_7 = tt.load %x_6, %m_5, %cst cacheModifier = cg : tensor<256x!tt.ptr<f32>, #blocked>
```

到了 LLVM dialect，它已经变成“每个线程两条标量 load”：

```llvm
%18 = tail call i32 asm sideeffect
  "mov.u32 $0, $1;\0A\09@$3 ld.global.cg.b32 { $0 }, [ $2 + 0 ];",
  "=r,r,l,b"(i32 0, ptr addrspace(1) %15, i1 %12)
%20 = tail call i32 asm sideeffect
  "mov.u32 $0, $1;\0A\09@$3 ld.global.cg.b32 { $0 }, [ $2 + 0 ];",
  "=r,r,l,b"(i32 0, ptr addrspace(1) %17, i1 %13)
```

PTX 里则是：

```ptx
mov.u32 %r1, %r2;
@%p1 ld.global.cg.b32 { %r1 }, [ %rd1 + 0 ];
mov.u32 %r3, %r2;
@%p2 ld.global.cg.b32 { %r3 }, [ %rd2 + 0 ];
@%p1 st.global.b32 [ %rd3 + 0 ], { %r4 };
@%p2 st.global.b32 [ %rd4 + 0 ], { %r5 };
```

**实测（Triton 3.8.0，sm_75，BLOCK=256，4 warps）**：这段 PTX 与上面的 LLVM 片段来自同一份编译产物。注意三件事：

- `ld.global.cg` 是 `.cg` 直接落到 PTX 的结果；
- `mov.u32 %r1, %r2` 是 `other=0.0` 的初始化，不是 `select`；
- `@%p1` 是 mask 谓词，masked-off 时 load 不执行，寄存器保留初始化值。

再看一个反例：如果每个线程拥有 4 个连续元素，并且地址对齐已知，lowering 会合成一条向量指令：

```ptx
ld.global.v4.b32 { %r1, %r2, %r3, %r4 }, [ %rd1 + 0 ];
st.global.v4.b32 [ %rd2 + 0 ], { %r1, %r2, %r3, %r4 };
```

**实测（Triton 3.8.0，sm_75，BLOCK=512，4 warps，配合 `tl.multiple_of`/`tl.max_contiguous`）**：SASS 为
`LDG.E.128.SYS` 和 `STG.E.128.SYS`。同一份 kernel 去掉对齐提示后，PTX 退回 4 条 `ld.global.b32`，SASS 退回 `LDG.E.SYS`。

## 核心概念

### 1. `tt.load` / `tt.store` / `tt.atomic_*` 的 op 契约

**源码依据**：

- `include/triton/Dialect/Triton/IR/TritonOps.td:257-302`：`TT_LoadOp`，操作数是 `ptr`、可选 `mask`、可选 `other`、`cachePolicy`、`isVolatile`；结果类型必须与 pointee 类型一致。
- `include/triton/Dialect/Triton/IR/TritonOps.td:304-339`：`TT_StoreOp`，操作数是 `ptr`、`value`、可选 `mask`、`cachePolicy`、`ignore_cta`。
- `include/triton/Dialect/Triton/IR/TritonOps.td:458-495`：`TT_AtomicRMWOp`，显式带 `sem` 和 `scope`。
- `include/triton/Dialect/Triton/IR/TritonOps.td:497-534`：`TT_AtomicCASOp`，同样带 `sem` 和 `scope`。

注意 `tt.load` 的结果类型就是 pointee 类型，mask 必须是同形状的 `i1`。这些约束在 TTIR verifier 里检查，不等到 lowering。

### 2. mask、other 在语义层的广播

**源码依据**：`python/triton/language/semantic.py:1047-1102`。

```python
if ptr.type.is_block():
    if mask is not None:
        ptr, mask = self.broadcast_impl_value(ptr, mask)
    if other is not None:
        ptr, other = self.broadcast_impl_value(ptr, other)
...
if other is not None:
    other = self.cast(other, elt_ty)
```

所以 `tl.load(p, mask=scalar_mask, other=0.0)` 在 TTIR 里已经是同形状的 `tensor<...xi1>` 和 `tensor<...xf32>`。lowering 不需要再做广播，只需要按线程拆元素。

`other` 不能脱离 `mask` 单独存在：`semantic.py:1054-1055` 直接报错。`other` 会被 cast 到 pointee 的元素类型，所以 `other=0` 对 `fp32` 指针会变成 `0.0`。

### 3. cache policy：modifier、eviction、prefetch 是三件事

**源码依据**：

- `include/triton/Dialect/Triton/IR/TritonAttrEnums.td:6-18`：`none/ca/cg/wb/cs/wt/cv`。
- `include/triton/Dialect/Triton/IR/TritonAttrDefs.td:21-46`：`CachePolicyAttr` 同时带 `cache_modifier` 和 `eviction_policy`。
- `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/LoadStoreOpToLLVM.cpp:48-152`：`CachePolicy`、`createCachePolicy`、`getL1EvictionPriority`、`getCachePolicy`。
- 同文件 `:154-172`：`verifyDetailedCachePolicySupport`。

三者的落点不同：

| 概念 | 进入哪里 | 约束 |
|---|---|---|
| `cache_modifier` | `ld.global.ca/cg/cs/cv`、`st.global.wb/cg/cs/wt` | 直接拼进 PTX opcode |
| L1 eviction | `ld/st ... L1::evict_first/evict_last/...` | sm70+ |
| L2 eviction | `createpolicy.fractional.L2::...b64` + `L2::cache_hint` | sm80+ |
| L2 prefetch | `ld.global.L2::64B/128B/256B` | 仅 load，sm75+ |

**源码依据**：`LoadStoreOpToLLVM.cpp:78-117` 在 `computeCapability >= 80` 时才生成 `createpolicy`；`:122-134` 生成 L1 policy 字符串；`:420-447` 把 modifier、L1 policy、prefetch、cache hint 拼进 `ld`；`:613-636` 对 `st` 做同样的事。

T4 是 sm75：`eviction_policy="evict_last"` 不会生成 L2 policy，只会尝试 L1 policy；`cache_modifier` 仍然有效。不要以为设置了 eviction policy 就一定能在 T4 上看到 `createpolicy`。

### 4. 向量宽度：contiguity、对齐、mask 三者取最小

**源码依据**：`LoadStoreOpToLLVM.cpp:184-199`。

```cpp
unsigned getVectorSize(Value ptr) const {
  auto contiguity = getContiguity(ptr);
  auto pointeeBitWidth = triton::getPointeeBitWidth(tensorTy);
  unsigned maxVecBits = 128;
  if (targetInfo.getComputeCapability() >= 100 &&
      targetInfo.getPtxVersion() >= 88) {
    maxVecBits = 256;
  }
  return std::min<unsigned>(maxVecBits / pointeeBitWidth, contiguity);
}
```

然后在 load 里：

```cpp
unsigned vec = getVectorSize(ptr);
...
if (llMask) {
  vec = std::min<size_t>(vec, getMaskAlignment(mask));
}
```

**源码依据**：`LoadStoreOpToLLVM.cpp:263-272`；store 在 `:515-532`。

`getMaskAlignment` 不是“mask 有多少个 true”，而是 mask 沿最内层维度连续为真的长度：

**源码依据**：`lib/Analysis/AxisInfo.cpp:1526-1544`，取 `constancy(maskOrder[0])`。

所以：

- 地址连续 + 对齐已知 + 没有 mask → 可以 `v4`；
- 地址连续 + 对齐已知 + 有 mask 但 mask 的 constancy 只有 1 → 退回标量；
- 地址不连续（stride=2）→ 即使没有 mask 也只能标量。

### 5. 地址空间与对齐

`tt.load` / `tt.store` 在 NVIDIA 后端只走 global memory。**源码依据**：`LoadStoreOpToLLVM.cpp:421-423` 无条件 `.global()`；store 在 `:614-615` 同样 `.global()`。

shared memory 不走 `tt.load`，而走：

- `ttg.local_alloc` / `ttg.local_load` / `ttg.local_store`；
- `ttg.async_copy_global_to_local`（sm80+，T4 不支持，guided）；
- TMA descriptor load/store（sm90+，guided）。

对齐信息来自 AxisInfo 的 divisibility。`tl.multiple_of(x_ptr + offs, 16)` 告诉编译器“这个指针值能被 16 整除”，`tl.max_contiguous(offs, 16)` 告诉编译器“沿最内层最多 16 个元素连续”。两者一起，`getVectorSize` 才能从 1 涨到 4（fp32）。

### 6. atomic 的 sem 与 scope

**源码依据**：

- `python/triton/language/semantic.py:1019-1045`：字符串到 `MemSemantic` / `MemSyncScope`。
- `include/triton/Dialect/Triton/IR/TritonAttrDefs.td:10-19`：`relaxed/acquire/release/acq_rel`。
- 同文件 `:91-99`：`gpu/cta/sys`。
- `LoadStoreOpToLLVM.cpp:762-775`：`useRed` 条件。
- 同文件 `:838-847`：`ld.acquire` 提升的条件。
- `third_party/nvidia/include/TritonNVIDIAGPUToLLVM/AtomicPTXBuilder.h:92-100`：先 `.global/.shared`，再 scope。
- 同文件 `:140-145`：再拼 `sem`、rmw op、向量宽度、类型后缀。

PTX 的顺序是：

```text
atom.global.<scope>.<sem>.<op>.<type>
red.global.<scope>.<sem>.<op>.<type>
```

**实测（Triton 3.8.0，sm_75）**：

```ptx
@%p1 atom.global.gpu.relaxed.add.f32 %r1, [ %rd5 + 0 ], %r2;
@%p1 atom.global.gpu.acq_rel.add.f32 %r5, [ %rd5 + 0 ], %r1;
```

对应的 SASS：

```text
ATOMG.E.ADD.F32.FTZ.RN.STRONG.GPU PT, R7, [R4], R7 ;
MEMBAR.ALL.GPU ;
```

`acq_rel` 在 SASS 里多出一条 `MEMBAR.ALL.GPU`，这不是“装饰”，是内存序语义的一部分。

## 底层机制

### 1. load 的 lowering 步骤

**源码依据**：`LoadStoreOpToLLVM.cpp:235-475`。

按执行顺序拆开：

1. **取 cache policy**（`:238-243`）：`getCachePolicy` 解析 legacy 或 detailed attr；`verifyDetailedCachePolicySupport` 检查硬件能力。
2. **取元素类型和每线程元素数**（`:261-264`）：`elemTy`、`valueElemTy`、`vec`、`numElems = getUniqueElemsPerThread(ptr.getType())`。
3. **用 mask 收紧 vec**（`:266-272`）：`vec = min(vec, getMaskAlignment(mask))`。
4. **拆指针、mask、other**（`:282-303`）：`unpackUniqueTensorElements` 把每个线程的 SSA 值展开成 `ptrElems`、`maskElems`、`otherElems`。
5. **按 vec 分组**（`:330-357`）：`totalWidth = elemBits * vec`，`width = min(totalWidth, max(32, elemBits))`，`nWords = totalWidth / width`。
6. **初始化目标寄存器**（`:361-414`）：`pred = maskElems[vecStart]`；如果有 `other`，每个 word 先 `mov` 成 `other` 的对应值；如果是可证明的整数常量，直接用立即数。
7. **发 PTX**（`:416-447`）：地址操作数 `ptrElems[runStart] + in_off`；opcode 拼 `ld.global`、modifier、L1 policy、prefetch、`L2::cache_hint`、`.v{nWords}.b{width}`；`maybePredicate(pred, "b")`。
8. **拆返回值并重新打包**（`:455-473`）：`extract_val`/`bitcast`/`unpackLLVector`，最后 `packUniqueTensorElements` 回张量。

关键点：`numElems` 是“每个线程唯一拥有的元素数”，不是张量总元素数。`BLOCK=256`、128 个线程时 `numElems=2`；`BLOCK=512` 时 `numElems=4`。

### 2. 为什么 mask 会变成 predicate，而不是 select

mask 是 `i1` 张量。lowering 把每个线程的 mask 元素展开成 `maskElems`，在每次发 load 时取 `maskElems[vecStart]` 作为谓词：

```cpp
Value pred = mask ? maskElems[vecStart] : Value{};
...
ld(dstsOpr, addrOpr).maybePredicate(pred, "b");
```

**源码依据**：`LoadStoreOpToLLVM.cpp:361`、`:445`。

PTX 的 `@%p` 谓词执行意味着：谓词为假时，load 根本不访问内存。这比“先 load 再 select”安全，因为越界地址不会被读。masked-off 的返回值由目标寄存器的初始化决定。

### 3. `other` 如何广播

语义层已经把 `other` 广播成和 `ptr` 同形状，并 cast 到元素类型。**源码依据**：`semantic.py:1064-1084`。

lowering 层再做两件事：

- 如果 `other` 是整数常量，且 AxisInfo 能证明它是常量，直接用立即数，避免物化一个常量寄存器（`LoadStoreOpToLLVM.cpp:293-302`、`:381-386`）。
- 否则把 `otherElems` 按 word 打包，`mov` 到目标寄存器（`:378-413`）。

所以 `other` 不是 `select(other, loaded, mask)`，而是“先把结果寄存器写成 other，再用谓词 load 覆盖”。这也解释了为什么没有 `other` 时，masked-off 的值在实现上可能是 0，但语义上应视为 undefined：不要依赖它。

### 4. store 的 lowering

**源码依据**：`LoadStoreOpToLLVM.cpp:490-643`。

store 没有 `other`，流程更直接：

1. `vec = getVectorSize(ptr)`，有 mask 时再被 `getMaskAlignment` 收紧（`:515-532`）。
2. `threadPred = emitMemoryThreadPredicate(...)`（`:553-554`）：处理 broadcast 导致的重复线程，避免同一地址被多个线程写。
3. 把 value 按 word 打包（`:579-598`）。
4. `pred = threadPred`，有 mask 时 `pred = maybeAnd(threadPred, mask)`（`:604-608`）。
5. 发 `st.global...`，同样 `maybePredicate`（`:613-636`）。

注意 store 的谓词是 `threadPred && mask`，而 load 的谓词只有 mask。`threadPred` 来自 layout 的 free variable mask：如果某个维度在寄存器/线程上是广播的，多个线程会拥有同一个元素，store 必须只让一个线程写。

### 5. atomic 的 lowering

**源码依据**：`LoadStoreOpToLLVM.cpp:648-948`。

CAS 最简单：逐元素、无向量化、无 packed：

```cpp
for (size_t i = 0; i < elemsPerThread; i += 1) {
  Value old = NVIDIA::emitPtxAtomicCAS(..., op.getSem(), op.getScope(), threadPred);
  resultVals[i] = old;
}
```

**源码依据**：`:679-688`。

RMW 复杂得多：

- `useRed`：结果没人用、不是 XCHG、且 `sem` 是 relaxed/release（`:772-775`）。满足时发 `red`，不返回旧值。
- 向量化：最多 128 bit；Hopper 上 FADD 的 f16/bf16/f32 可以 `.v2/.v4`（`:706-716`、`:799-815`）。
- packed：f16 可以打成 `f16x2`（`:811-815`）。
- `ld.acquire` 提升：标量、常量 0 的 ADD/FADD、acquire/relaxed、scope 合法时，直接把 atomic add 0 降成 `ld.acquire`（`:718-758`、`:838-868`）。
- 否则发 `atom`/`red`（`:912-916`）。

**实测（Triton 3.8.0）**：`tl.atomic_add(..., sem="relaxed")` 即使结果未使用，也生成了 `atom.global.gpu.relaxed.add.f32`，而不是 `red`。这说明“源码有 `useRed` 分支”不等于“你手上的构建一定走它”；版本和行为要分开验证。

### 6. `emitIndices` 与地址计算

`tt.load` 的地址在 TTIR 里已经由 `tt.addptr` 算好，lowering 只做 `unpackUniqueTensorElements`，不调用 `emitIndices`。**源码依据**：`LoadStoreOpToLLVM.cpp:282-283`。

`emitIndices` 用在需要“从 layout 反推每个元素坐标”的 op 上，例如 gather、local load/store、convert_layout：

**源码依据**：`lib/Conversion/TritonGPUToLLVM/Utility.cpp:538-587`；声明在 `include/triton/Conversion/TritonGPUToLLVM/Utility.h:650-664`。

它的核心是把 `{register, lane, warp, block}` 作为输入维度，调用 `applyLinearLayoutVec`，一次算出该线程所有寄存器元素的坐标：

```cpp
SmallVector<std::pair<StringAttr, Value>> commonIndices = {
    {kRegister, b.i32_val(0)},
    {kLane, laneId},
    {kWarp, warpId},
    {kBlock, blockId}};
...
auto vecIndices = applyLinearLayoutVec(loc, rewriter, ll, commonIndices, registerIndices);
```

理解这一点，就能区分“地址已经在 IR 里”的 load/store 和“地址需要按 layout 现算”的 gather/convert_layout。

## 进一步用法/重要变体

### 变体 1：cache modifier 对 PTX 的影响

**实测（Triton 3.8.0，sm_75）**：

| 写法 | PTX |
|---|---|
| 默认 | `ld.global.b32` |
| `cache_modifier=".cg"` | `ld.global.cg.b32` |
| `cache_modifier=".ca"` | `ld.global.ca.b32` |
| `cache_modifier=".cv"` | `ld.global.cv.b32` |
| store 默认 | `st.global.b32` |
| store `.cg` | `st.global.cg.b32` |
| store `.wt` | `st.global.wt.b32` |

`.cg` 的语义是“只缓存到 L2 及以下，不进 L1”。它不会让访问绕过 L2，也不会改变可见性；它只是一个 cache hint。

### 变体 2：eviction policy 与 L2 prefetch

**源码依据**：`LoadStoreOpToLLVM.cpp:54-120`。

sm80+ 上，`eviction_policy="evict_last"` 会生成：

```ptx
createpolicy.fractional.L2::evict_last.b64 %rd, 1.0;
ld.global.L2::cache_hint.b32 { %r }, [ %rd1 ], %rd;
```

T4 上不会生成 `createpolicy`，因为 `hardwareSupport = computeCapability >= 80` 为假。**guided**：如果你在 A100/H100 上实验，才能看到完整的 L2 policy 形式。

### 变体 3：`volatile`

`tt.load` 的 `isVolatile` 直接拼进 PTX：

```cpp
auto &ld = ptxBuilder.create("ld")->o("volatile", op.getIsVolatile())
```

**源码依据**：`LoadStoreOpToLLVM.cpp:420-423`。

`volatile` 禁止编译器合并/删除该 load，但不会自动给你 acquire 语义。需要内存序时用 atomic，不要用 volatile 代替。

### 变体 4：向量化 atomic

**源码依据**：`LoadStoreOpToLLVM.cpp:706-716`、`:799-815`。

- 只有 Hopper 的 `supportVectorizedAtomics()` 才允许 `.v2/.v4`；
- 只支持 FADD 的 f16/bf16/f32；
- 其他情况 `packed=2`（f16x2）或标量；
- 总宽度最多 128 bit。

T4 上不要期待向量 atomic。**guided**：Hopper 的 `atom.global.v2.f32.add` 形式需要 sm90 硬件验证。

### 变体 5：`ld.acquire` 提升

`tl.atomic_add(ptr, 0, sem="acquire")` 这种“只读的 atomic”在标量、常量 0、scope 合法时，会被降成 `ld.acquire`，而不是 `atom.add 0`。**源码依据**：`LoadStoreOpToLLVM.cpp:718-758`、`:838-868`。

这是一个重要的优化：atomic add 0 的语义等价于 acquire load，但硬件路径更便宜。

### 变体 6：AMD 后端（guided）

AMD 的 `LoadStoreOpToLLVM.cpp` 有独立的 cache policy、buffer 指令和 atomic 路径。**源码依据**：`third_party/amd/lib/TritonAMDGPUToLLVM/LoadStoreOpToLLVM.cpp` 存在且被 AMD backend 使用。不要把 NVIDIA 的 `.cg`/`createpolicy` 结论直接套到 AMD。

## 对比与取舍

| 方案 | 优点 | 代价 | 适用 |
|---|---|---|---|
| 标量 `ld.global.b32` | 实现简单、mask 精确 | 指令多、带宽利用率低 | 地址不连续、mask 复杂 |
| `ld.global.v4.b32` | 一条指令 16B，吞吐高 | 需要连续 + 对齐 + mask constancy | 连续块、无 mask 或 mask 对齐 |
| `ld.global.cg` | 不污染 L1 | 仍然占 L2 | 流式读、复用低 |
| `ld.global.ca` | 利用 L1 | 可能挤掉有用数据 | 复用高 |
| `atom` | 返回旧值 | 比 `red` 贵 | 需要旧值、CAS 循环 |
| `red` | 不返回旧值，便宜 | 结果必须无人使用 | 直方图、累加 |
| `ld.acquire` 提升 | 比 atomic add 0 便宜 | 仅标量常量 0 | 自旋/标志位 |

## 踩雷集锦

### 雷 1：以为 `.cg` 会绕过 L2

`.cg` 是 “cache global”，意思是缓存在 L2 及以下，**不**进 L1。它不会让访问绕过 L2，也不会提供任何内存序。要绕过 cache 用 `.cv`，要内存序用 atomic/fence。

### 雷 2：以为 mask 只影响结果，不影响向量化

mask 的 constancy 会直接收紧 `vec`。**实测**：同一个 512 元素 fp32 kernel，没有 mask 时是 `ld.global.v4.b32`；加上 `mask=offs<n` 且 `n` 的对齐未知时，变成 4 条带谓词的 `ld.global.b32`。如果你想要向量化，除了 `tl.multiple_of` 指针，还要让 mask 的边界对齐（例如 `n = tl.multiple_of(n, 16)`），或者接受标量路径。

### 雷 3：以为 `other` 是 `select`

`other` 是目标寄存器的初始化，不是 load 之后的 select。**实测**：`other=0.0` 生成 `mov.u32 %r2, %r1`，然后 `@%p ld...`。没有 `other` 时，masked-off 的值在实现上可能是 0，但语义上是 undefined，不能依赖。

### 雷 4：以为 `tl.atomic_add` 一定是 `red`

`red` 需要结果无人使用、不是 XCHG、且 `sem` 是 relaxed/release。**实测**：3.8.0 在结果未使用时仍生成 `atom`。不同版本、不同 mask、不同 scope 都可能改变选择。看 PTX，不要看源码注释猜。

### 雷 5：以为 `tt.load` 能读 shared memory

`tt.load` 在 NVIDIA 后端固定 `.global`。shared memory 走 `ttg.local_load` / `ttg.local_store`，async copy 走 `ttg.async_copy_global_to_local`。把 `tt.load` 用在 shared 指针上，IR 可能能过，但 lowering 不会给你 `ld.shared`。

### 雷 6：以为 T4 支持 256-bit load

`getVectorSize` 的 `maxVecBits` 默认 128；只有 sm100+ 且 PTX 8.8+ 才 256。T4 上最大是 128 bit（`v4.b32`）。**源码依据**：`LoadStoreOpToLLVM.cpp:192-197`。

## 进阶

1. **读 AxisInfo 的 `addptr` 规则**：`lib/Analysis/AxisInfo.cpp` 里 `AddPtrOp` 的 contiguity/divisibility 合并逻辑，决定了 `tl.multiple_of` 为什么能提升 vec。
2. **看 `PTXBuilder` 的 `maybePredicate` 实现**：`third_party/nvidia/include/TritonNVIDIAGPUToLLVM/PTXBuilder.h`，理解 `@%p` 是如何拼进 inline asm 的。
3. **对比 AMD 的 buffer 指令**：AMD 用 `buffer_load_dwordx4` 等，和 NVIDIA 的 `ld.global.v4` 不是一一对应。**guided**。
4. **看 TMA 的 descriptor load**：`AsyncTMACopyGlobalToLocalOpConversion` 在 `LoadStoreOpToLLVM.cpp:1168+`。T4 不支持，**guided**。

## 动手练习

### 练习 1：让标量 load 变成向量 load

写一个 `BLOCK=512`、4 warps 的 kernel，先不加提示，dump PTX；再加：

```python
offs = tl.max_contiguous(tl.multiple_of(offs, 16), 16)
p = tl.multiple_of(x_ptr + offs, 16)
```

观察 `ld.global.b32` 是否变成 `ld.global.v4.b32`，SASS 是否从 `LDG.E.SYS` 变成 `LDG.E.128.SYS`。

### 练习 2：让 mask 杀死向量化

在练习 1 的基础上加 `mask=offs < n`，不告诉编译器 `n` 的对齐；观察 `vec` 是否退回 1。再把 `n` 改成 `tl.multiple_of(n, 16)`，看是否恢复向量化。

### 练习 3：对比 atomic 的 sem/scope

编译三个版本：

```python
tl.atomic_add(p, v, sem="relaxed", scope="gpu")
tl.atomic_add(p, v, sem="acq_rel", scope="gpu")
tl.atomic_add(p, v, sem="release", scope="cta")
```

用 `grep` 找 `atom.global` / `red.global`，再用 `nvdisasm` 看是否有 `MEMBAR`。解释每条指令的语义差异。

### 练习 4：读源码核对行号

```bash
cd /home/ubuntu/aicomp/triton
nl -ba third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/LoadStoreOpToLLVM.cpp | sed -n '184,199p'
nl -ba third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/LoadStoreOpToLLVM.cpp | sed -n '361,447p'
```

确认 `getVectorSize` 的 128-bit 上限、`pred` 的取法、`maybePredicate` 的位置。

## 本章重点

- `tt.load`/`tt.store` 是张量 op，lowering 后变成每个线程的一组标量/向量 PTX。
- `vec` 由 `min(128/bitwidth, contiguity, mask constancy)` 决定；T4 上限 128 bit。
- mask 变成 `@%p` 谓词；`other` 变成目标寄存器初始化，不是 select。
- `cache_modifier` 直接进 opcode；L2 eviction 需要 sm80+；T4 只有 L1 policy。
- `tt.load` 固定 `.global`；shared 走 `local_load/local_store`。
- atomic 的 PTX 顺序是 `atom.global.<scope>.<sem>.<op>.<type>`；`red` 只在结果无人使用时才可能。
- `emitIndices` 用于需要按 layout 反推坐标的 op，不是 `tt.load` 的地址计算路径。

## 自我检核

1. 为什么 `BLOCK=256`、4 warps 的 masked load 生成 2 条 load，而不是 1 条？
2. `getVectorSize` 为什么会返回 1？列出至少两种原因。
3. `other=0.0` 在 PTX 里对应哪条指令？为什么不是 `select`？
4. `.cg` 和 `.cv` 的区别是什么？各自适合什么访问模式？
5. 为什么 T4 上 `eviction_policy="evict_last"` 看不到 `createpolicy`？
6. `tt.atomic_add` 满足什么条件才会走 `red`？
7. `acq_rel` 在 SASS 里多出了什么？它为什么必要？
8. `tt.load` 能不能读 shared memory？不能的话应该用什么 op？
9. `emitIndices` 和 `unpackUniqueTensorElements` 的分工是什么？
10. 如果 mask 的 constancy 是 4，但指针 contiguity 是 8，`vec` 是多少？

## 延伸阅读

### 1. `LoadStoreOpToLLVM.cpp` 的 load 主体

读 `third_party/nvidia/lib/TritonNVIDIAGPUToLLVM/LoadStoreOpToLLVM.cpp:225-478`。重点看 `vec` 的收紧、`other` 的初始化、`ld` 的 opcode 拼接。学到“张量 op 如何拆成线程级指令”。与本章的“为什么不是一个 ld.global”直接对应。

### 2. `AtomicPTXBuilder.h`

读 `third_party/nvidia/include/TritonNVIDIAGPUToLLVM/AtomicPTXBuilder.h:39-150`。重点看 `atom/red`、scope、sem、suffix 的拼接顺序。学到 PTX atomic 的完整语法。与本章的 atomic 例子对应。

### 3. `AxisInfo.cpp` 的 mask alignment

读 `lib/Analysis/AxisInfo.cpp:1526-1544` 以及 `AddPtrOp` 的 visitor。学到 contiguity/divisibility/constancy 如何从 IR 推导出来。与本章的“向量宽度由对齐和连续性决定”对应。

### 4. PTX ISA 的 `ld`/`st`/`atom` 章节

读 NVIDIA PTX ISA 文档的 `ld`、`st`、`atom`、`red`、`createpolicy` 小节。学到每个 modifier 的精确语义。与本章的 cache policy 和 atomic scope 对应。

## 下一章

下一章进入 tensor core：`tt.dot` 如何选择 FMA / MMAv2 / MMAv3(WGMMA) / MMAv5(tcgen05)，以及为什么 T4 在 3.9.0 上会走 FMA 而不是 MMAv2。

[Ch 25 — dot/MMA lowering](25-dot-mma-lowering.md)
