# 练习 A — 用手写 IR 逼出 ABI 与地址空间

> **目标**：不看 Ch 4/5 的答案，自己写一份 `.ll`，让 `llc` 产出指定的 `.param` 列表、`.shared` 声明与 `cvta` 行为。完成后你能独立判断「这段 PTX 的 ABI 对不对」。

## 背景与动机

真实工作里，你很少从零写 IR，更多是**读别人生成的 IR/PTX 并判断语义**。这个练习反过来：你控制 IR，观察 ABI 规则如何一条条显形。做过一次，以后看 clang 的输出就不会再猜。

## 任务规格

写一个文件 `pa.ll`，满足：

1. triple 为 `nvptx64-nvidia-cuda`。
2. 一个 `ptx_kernel`，参数依次为：
   - `ptr %out`（generic）
   - `ptr addrspace(1) %g`（global）
   - `i1 %flag`
   - `i8 %x`
   - `i16 %y`
   - `half %h`
   - `ptr byval(%S) align 8 %s`，其中 `%S = type { float, float }`
3. 一个模块级 `addrspace(3)` 的 `[8 x float]` 数组，并在 kernel 里写一次、`barrier0`、再读一次。
4. 从 `%g` 读一个 float，写进 shared，再从 shared 读回。
5. 把 `%x`、`%y` 零扩展相加，转成 float，加上 `%h` 提升后的 float。
6. 用 `%flag` 在「计算结果」与「shared 读回值」之间做 `select`。
7. 结果写到 `%out` 的 `tid.x` 位置。

验收命令：

```bash
$BIN/llc -mtriple=nvptx64 -mcpu=sm_75 -O2 pa.ll -o pa.ptx
```

## 期望输出范例

参数列表**必须**长这样（顺序、宽度、限定符都要对）：

```ptx
.visible .entry pa(
	.param .u64 .ptr .align 1 pa_param_0,
	.param .u64 .ptr .global .align 1 pa_param_1,
	.param .b8 pa_param_2,
	.param .b8 pa_param_3,
	.param .b16 pa_param_4,
	.param .align 2 .b8 pa_param_5[2],
	.param .align 8 .b8 pa_param_6[8]
)
```

shared 声明（注意 `demoted` 注释）：

```ptx
	// demoted variable
	.shared .align 4 .b8 sh[32];
```

关键指令应能 grep 到：

```bash
grep -E "ld.param.b8|ld.param.b16|ld.global.b32|st.shared.b32|bar.sync|selp.f32|cvt.f32.f16" pa.ptx
```

边界输入：把 `%flag` 改成常量 `true`，`select` 应消失（变成直接使用计算结果）；把 `%out` 改成 `ptr addrspace(1)`，`cvta.to.global` 应消失。

## 如果你卡住了

1. **参数宽度不对**：先查 Ch 4 的两条提升规则（kernel ≥8 位、device ≥32 位），再确认 `half` 走的是数组。
2. **找不到 `cvta.to.global`**：只有 generic 指针才需要；`addrspace(1)` 参数本身已经是 global。
3. **shared 没出现**：检查全局变量是否写了 `addrspace(3)`，以及是否真的被 kernel 使用（未使用会被删）。
4. **`bar.sync` 没出现**：`llvm.nvvm.barrier0()` 要声明成 `void` 并在两次访问之间调用。
5. **`selp` 变成分支**：`select` 是值选择；如果你写成 `br`，就会变成 `@%p bra`。

## 实作步骤建议

### Step 1：搭骨架
写 triple、`%S`、shared 全局、三个 intrinsic 声明，先让空 kernel 编过。

### Step 2：参数与 ABI
把 7 个参数写全，编译，对照期望参数列表逐项修。

### Step 3：地址空间路径
先写 global 读 → shared 写 → barrier → shared 读，确认 `cvta` 与 `.global` 限定符符合预期。

### Step 4：宽度与 select
加 i8/i16/half 运算和 `select`，确认出现 `and.b16`/`setp`/`cvt`/`selp`。

### Step 5：验收
用上面的 grep 命令逐条检查，再故意改坏一处（例如把 `%y` 写成 `i32`），确认参数列表随之变化。

## 完整参考解答

**写完再看！不要偷看。**

<details>
<summary>点开参考实作</summary>

```llvm
target triple = "nvptx64-nvidia-cuda"
%S = type { float, float }

@sh = internal addrspace(3) global [8 x float] undef, align 4

declare i32 @llvm.nvvm.read.ptx.sreg.tid.x()
declare void @llvm.nvvm.barrier0()

define ptx_kernel void @pa(ptr %out, ptr addrspace(1) %g, i1 %flag,
                           i8 %x, i16 %y, half %h,
                           ptr byval(%S) align 8 %s) {
entry:
  %t = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
  %t64 = zext i32 %t to i64
  %gp = getelementptr float, ptr addrspace(1) %g, i64 %t64
  %v = load float, ptr addrspace(1) %gp, align 4
  %sp = getelementptr [8 x float], ptr addrspace(3) @sh, i64 0, i64 %t64
  store float %v, ptr addrspace(3) %sp, align 4
  call void @llvm.nvvm.barrier0()
  %w = load float, ptr addrspace(3) %sp, align 4
  %e8 = zext i8 %x to i32
  %e16 = zext i16 %y to i32
  %sum = add i32 %e8, %e16
  %sumf = sitofp i32 %sum to float
  %hv = fpext half %h to float
  %acc = fadd float %sumf, %hv
  %sel = select i1 %flag, float %acc, float %w
  %op = getelementptr float, ptr %out, i64 %t64
  store float %sel, ptr %op, align 4
  ret void
}
```

本机实测产物（关键部分）：

```ptx
.visible .entry pa(
	.param .u64 .ptr .align 1 pa_param_0,
	.param .u64 .ptr .global .align 1 pa_param_1,
	.param .b8 pa_param_2,
	.param .b8 pa_param_3,
	.param .b16 pa_param_4,
	.param .align 2 .b8 pa_param_5[2],
	.param .align 8 .b8 pa_param_6[8]
)
{
	.reg .pred 	%p<2>;
	.reg .b16 	%rs<4>;
	.reg .b32 	%r<11>;
	.reg .b64 	%rd<9>;
	// demoted variable
	.shared .align 4 .b8 sh[32];
	ld.param.b8 	%rs1, [pa_param_2];
	and.b16 	%rs2, %rs1, 1;
	setp.ne.b16 	%p1, %rs2, 0;
	...
	mov.b64 	%rd6, sh;
	add.s64 	%rd7, %rd6, %rd4;
	st.shared.b32 	[%rd7], %r4;
	bar.sync 	0;
	ld.shared.b32 	%r5, [%rd7];
	...
	selp.f32 	%r10, %r9, %r5, %p1;
```

两个容易看漏的点：

- `ptr addrspace(1)` 参数打印成 `.ptr .global`，函数体里**没有** `cvta.to.global`（已经是 global）。
- `%flag` 虽然只是 i1，但参数槽是 `.b8`，读出来先 `and.b16 ...,1` 再 `setp.ne.b16` 才变成谓词。

</details>

## 卡点提示（再看一遍）

1. 参数顺序错一位，后面全错；先只放两个参数编一次，再逐个加。
2. `byval` 必须带 `align`，否则 `.param .align` 会是 1，和你以为的 8 对不上。
3. `half` 在 PTX 里不是标量参数，而是 `.b8[2]`；这是 `shouldPassAsArray` 的规则。
4. shared 读写要落在同一个下标上，否则优化器可能把其中一次删掉。

## 加分挑战

1. 把 `%g` 改成 generic `ptr`，确认出现 `cvta.to.global`，并解释为什么。
2. 给 `%out` 加 `align 16`，看 `.param .ptr .align` 怎么变。
3. 把 `barrier0` 换成 `llvm.nvvm.barrier0()` 之外的同步（例如 `membar.gl`），观察 PTX 差异。
4. 用 `-O0` 重编，统计 `ld.b32`（generic）出现了几次。

→ [Ch 6 CUDA → NVVM IR：clang 的 device 前端](./06-cuda-to-nvvm-ir.md)
