# 练习 B — 三路对拍（手写 IR / clang / nvcc）

> **目标**：对同一个向量加法 kernel，用三条路径生成 PTX，解释每一处差异的来源。做完你就能在真实工作里快速判断「这份 PTX 是谁生成的、为什么长这样」。

## 背景与动机

CUDA 兼容编译器团队每天都在做「对拍」：自家编译器的输出 vs nvcc 的输出。对拍最难的不是跑命令，而是**知道哪些差异是本质的、哪些只是风格**。这个练习就是训练这个判断。

## 任务规格

准备一个 kernel：

```cuda
extern "C" __global__ void vadd(const float *a, const float *b, float *c, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i < n) c[i] = a[i] + b[i];
}
```

用三条路径生成 PTX：

```bash
BIN=/home/ubuntu/llvm/builds/ct-instr-trunk/bin

# 路径 1：手写等价 IR（或直接用 Ch 0 的 add.ll）
$BIN/llc -mtriple=nvptx64 -mcpu=sm_75 -O2 add.ll -o p1-llc.ptx

# 路径 2：clang device-only
$BIN/clang -x cuda --cuda-path=/usr/local/cuda --cuda-gpu-arch=sm_75 \
  --cuda-device-only -O3 -S -emit-llvm -o vadd-dev.ll vadd.cu
$BIN/llc -mtriple=nvptx64 -mcpu=sm_75 -O2 vadd-dev.ll -o p2-clang.ptx

# 路径 3：nvcc
/usr/local/cuda/bin/nvcc -arch=sm_75 -O3 -ptx vadd.cu -o p3-nvcc.ptx
```

验收：填出下表，并对每一行写出「为什么」。

| 维度 | p1（手写 llc） | p2（clang+llc） | p3（nvcc） |
|---|---|---|---|
| `.version` | | | |
| 参数声明 | | | |
| 参数读取 | | | |
| 浮点寄存器 | | | |
| 索引计算 | | | |
| 访存指令 | | | |

## 期望输出范例

本机实测结果（你可以直接对照）：

| 维度 | p1 | p2 | p3 |
|---|---|---|---|
| `.version` | `6.3` | `8.4` | `8.4` |
| 参数声明 | `.param .u64 .ptr .align 1 p_param_0` | 同左 | `.param .u64 vadd_param_0`（无 `.ptr`/`.align`） |
| 参数读取 | `ld.param.b64` | `ld.param::entry.b64` | `ld.param.u64` |
| 浮点寄存器 | 与整数共用 `%r` | 同左 | 独立 `.reg .f32 %f<4>` |
| 索引计算 | `mad.lo.s32` | `mad.lo.s32` | `mad.lo.s32` |
| 访存 | `cvta.to.global` + `ld.global.b32` | 同左 | 同左 |

三份产物都能被 `ptxas` 接受，且 SASS 的寄存器用量相同（本机 `REG:12`）。**版本、语法糖、寄存器命名不同，语义一致。**

## 如果你卡住了

1. **clang 报 fatbinary 错**：加 `--cuda-device-only`（Ch 0）。
2. **diff 全是噪声**：先只比 `.version`、参数列表、`.reg` 三处，再看指令。
3. **nvcc 的 `.f32` 寄存器对不上**：nvcc 用类型化寄存器前缀，llc 统一用 `%r`；这是命名约定，不是语义差异。
4. **参数名不同**：符号名来自函数名，先统一函数名再比。
5. **找不到 `mad`**：确认 `-O2`/`-O3`，并检查 `mul` 结果是否只有一个用户。

## 实作步骤建议

### Step 1：先跑通三条命令
不管差异，先确保三份 `.ptx` 都生成成功、都能 `ptxas`。

### Step 2：对齐头部
把三份的 `.version`、`.target`、`.address_size` 抄进表里。

### Step 3：比参数
逐项比 `.param` 的宽度、限定符、对齐。

### Step 4：比指令
按「索引 → 地址 → 访存 → 计算」的顺序比，避免被顺序差异干扰。

### Step 5：解释差异
每一处差异标注：语义差异 / 版本差异 / 风格差异。

## 完整参考解答

**写完再看！**

<details>
<summary>点开参考结论</summary>

本机实测的关键片段：

```ptx
; p1（llc，手写 IR）
.version 6.3
.param .u64 .ptr .align 1 add_param_0,
ld.param.b64 	%rd4, [add_param_0];
mad.lo.s32 	%r5, %r3, %r4, %r2;
mul.wide.s32 	%rd10, %r5, 4;
cvta.to.global.u64 	%rd6, %rd5;
ld.global.b32 	%r6, [%rd1];
```

```ptx
; p2（clang device-only + llc）
.version 8.4
.param .u64 .ptr .align 1 vadd_param_0,
ld.param::entry.b64 	%rd4, [vadd_param_0];
mad.lo.s32 	%r5, %r2, %r3, %r4;
```

```ptx
; p3（nvcc）
.version 8.4
.param .u64 vadd_param_0,
.reg .f32 	%f<4>;
ld.param.u64 	%rd1, [vadd_param_0];
mad.lo.s32 	%r1, %r3, %r4, %r5;
```

差异归因：

1. `.version`：clang/nvcc 走 CUDA 12.4 的 `+ptx84`；llc 用 sm_75 的最低 6.3（Ch 3）。
2. `::entry`：PTX 8.4 的 entry 参数空间写法；6.3 没有（Ch 3）。
3. `.ptr` / `.align`：llc 显式标注指针与对齐；nvcc 省略。两者都合法，但 llc 的形式信息更全（Ch 4）。
4. `%f` vs `%r`：nvcc 用类型化寄存器名；llc 统一 32 位银行。**语义相同**。
5. `mad` / `mul.wide`：三者一致，说明这是 PTX 层的标准选择，不是某个前端的偏好（Ch 10）。

</details>

## 卡点提示

1. 先统一函数名与参数名，否则 diff 全是符号噪声。
2. 比 `.reg` 数量没意义，先比指令。
3. nvcc 的 `-O3` 和 clang 的 `-O3` 不是同一套优化；不要期待逐指令一致。
4. 三份产物都要过 `ptxas`，否则可能是「看起来对但不可汇编」。

## 加分挑战

1. 把 `vadd` 改成 `a[i]*b[i]+c[i]`，观察三者是否都生成 `fma.rn.f32`。
2. 加 `__restrict__ const`，观察三者是否都出现 `ld.global.nc`。
3. 把 `-O3` 改成 `-O0`，重新填表，找出「哪些差异消失了」。
4. 把三份 PTX 都编成 cubin，比较 `cuobjdump -res-usage` 的 `REG` 是否一致。

→ [Ch 11 寄存器类与「没有寄存器分配」](./11-registers-and-no-regalloc.md)
