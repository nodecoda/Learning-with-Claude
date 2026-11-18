# 练习 E — 内存序与编码对照

> **目标**：把 Ch 18 的 scope/ordering 矩阵与 Ch 19 的 128 位编码接起来：同一段并发代码，从 IR 到 PTX 到 SASS 到控制位，逐层解释。

## 背景与动机

真实工作里，你不会只被问「这个 atomic 是什么 scope」，而是「这段代码在 T4 上为什么偶发读到 0」。回答需要三件事：语义（模型）、产物（PTX/SASS）、证据（编码/实验）。这个练习一次练完。

## 任务规格

1. 手写 `memx.ll`，包含四个 `ptx_kernel`：
   - `k_relaxed`：`atomicrmw add ... syncscope("device") monotonic`
   - `k_acquire`：`load atomic ... syncscope("device") acquire`
   - `k_release`：`store atomic ... syncscope("device") release`
   - `k_seqcst`：`load atomic ... syncscope("device") seq_cst`
   每个 kernel 把结果写到 `ptr addrspace(1) %out`（或直接 store）。
2. 生成 PTX，填出 scope/ordering → 后缀的表。
3. 用 `ptxas` 编 cubin，用 `cuobjdump -sass` 填出 PTX → SASS 的表。
4. 用 `nvdisasm -hex` 找一对「低字相同、高字不同」的指令，解释原因。
5. 回答：下面这段 PTX 的 message-passing 是否正确？如果错，错在哪一行？

```ptx
st.global.b32 	[data], 42;
st.relaxed.cta.global.b32 	[flag], 1;    // producer
...
ld.relaxed.gpu.global.b32 	%r, [flag];   // consumer
ld.global.b32 	%v, [data];
```

## 期望输出范例

PTX 侧（本机实测）：

| IR | PTX |
|---|---|
| `atomicrmw ... syncscope("device") monotonic` | `atom.relaxed.gpu.global.add.u32` |
| `load atomic ... syncscope("device") acquire` | `ld.acquire.gpu.global.b32` |
| `store atomic ... syncscope("device") release` | `st.release.gpu.global.b32` |
| `load atomic ... syncscope("device") seq_cst` | `fence.sc.gpu;` + `ld.acquire.gpu.global.b32` |

SASS 侧：

| PTX | SASS |
|---|---|
| `ld.acquire.gpu` | `LDG.E.STRONG.GPU` |
| `st.release.gpu` | `STG.E.STRONG.GPU` |
| `atom.acq_rel.gpu` | `ATOMG.E.ADD.STRONG.GPU` |
| `fence.sc.gpu` | `MEMBAR.SC.GPU` |

编码侧（来自 `memk.cubin` 的真实观测）：

```
000fe2000c10e904   ← STG.E.SYS 的高字（某个上下文）
004fe2000c10e904   ← 另一种上下文
低字都是 00000000ff007986
```

问题 5 的答案：**错误**。producer 用 `.cta` scope、consumer 用 `.gpu` scope，不构成 morally strong；且缺少 release/acquire 或 fence，`data` 的写入可能晚于 `flag` 可见。

## 如果你卡住了

1. **`syncscope("system")` 报错**：这是 Ch 18 的坑；用默认 scope 或 NVVM intrinsic。
2. **PTX 没有 `fence.sc`**：确认 `seq_cst`，不是 `acquire`。
3. **SASS 里没有 `STRONG`**：确认你传的是 `syncscope("device")`，不是默认 `.sys`。
4. **找不到低字相同的指令**：找同一函数里重复出现的 `STG`/`LDG`，或跨 kernel 比。
5. **问题 5 看不出错**：先画出「谁能看见谁」的 scope 偏序。

## 实作步骤建议

### Step 1：写 IR
四个 kernel，参数用 `ptr addrspace(1)`，结果写回。

### Step 2：PTX
逐条对照 Ch 18 的表，确认后缀。

### Step 3：SASS
`ptxas -arch=sm_75`，`cuobjdump -sass`，记录 `STRONG`/`MEMBAR`。

### Step 4：编码
`nvdisasm -hex -c`，找重复指令，比较两个 64 位字。

### Step 5：推理
回答 message-passing 问题，并写出最小修复（release/acquire 或 fence）。

### Step 6：验证
把修复版写成 IR，确认 PTX 出现 `st.release.gpu` / `ld.acquire.gpu`。

## 完整参考解答

**写完再看！**

<details>
<summary>点开参考结论</summary>

```llvm
target triple = "nvptx64-nvidia-cuda"

define ptx_kernel void @k_relaxed(ptr addrspace(1) %p, ptr addrspace(1) %o) {
  %v = atomicrmw add ptr addrspace(1) %p, i32 1 syncscope("device") monotonic, align 4
  store i32 %v, ptr addrspace(1) %o, align 4
  ret void
}
define ptx_kernel void @k_acquire(ptr addrspace(1) %p, ptr addrspace(1) %o) {
  %v = load atomic i32, ptr addrspace(1) %p syncscope("device") acquire, align 4
  store i32 %v, ptr addrspace(1) %o, align 4
  ret void
}
define ptx_kernel void @k_release(ptr addrspace(1) %p, i32 %x) {
  store atomic i32 %x, ptr addrspace(1) %p syncscope("device") release, align 4
  ret void
}
define ptx_kernel void @k_seqcst(ptr addrspace(1) %p, ptr addrspace(1) %o) {
  %v = load atomic i32, ptr addrspace(1) %p syncscope("device") seq_cst, align 4
  store i32 %v, ptr addrspace(1) %o, align 4
  ret void
}
```

实测 PTX：

```ptx
atom.relaxed.gpu.global.add.u32 	%r1, [%rd2], 1;
ld.acquire.gpu.global.b32 	%r1, [%rd2];
st.release.gpu.global.b32 	[%rd2], %r1;
fence.sc.gpu;
ld.acquire.gpu.global.b32 	%r1, [%rd2];
```

实测 SASS：

```
ATOMG.E.ADD.STRONG.GPU PT, R2, [UR4], R2
LDG.E.STRONG.GPU R0, [UR4]
STG.E.STRONG.GPU [UR4], R0
MEMBAR.SC.GPU
```

问题 5 的最小修复：

```ptx
st.global.b32 	[data], 42;
st.release.gpu.global.b32 	[flag], 1;    // release，gpu scope
...
ld.acquire.gpu.global.b32 	%r, [flag];   // acquire，gpu scope
ld.global.b32 	%v, [data];
```

或者保留 relaxed，但在两侧各加 `membar.gl`。

</details>

## 卡点提示

1. 先只写一个 kernel，编通再复制。
2. `.cta` 与 `.gpu` 的混用是最容易漏的 bug；先检查 scope 是否一致。
3. 编码对照时，固定 `ptxas` 版本与 `-O` 级别。
4. SASS 的 `.SYS` 不等于 strong；看 `.STRONG`。

## 加分挑战

1. 把 `k_seqcst` 改成 `syncscope("block")`，确认 `fence.sc.cta`。
2. 用 `__threadfence_system()` 生成 `MEMBAR.SC.SYS`，比较它与 `.gpu` 的编码差异。
3. 用 `nvdisasm -hex` 统计：同一 kernel 里 `LDG.E.STRONG.GPU` 的高字是否总相同。
4. 用 `-mcpu=sm_80` 与 `-mcpu=sm_90` 各编一次 `syncscope("cluster")`，确认 `.cta` vs `.cluster` 的静默降级。
5. 写一个最小 litmus 测试设计（不用真跑），说明如何区分「允许重排」与「实现 bug」。

→ [Final Project：PTX → 自研 ISA 的最小闭环](./final-project-ptx-to-own-isa.md)
