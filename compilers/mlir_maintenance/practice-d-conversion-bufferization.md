# 练习 D — 端到端 conversion 与 bufferization

> **目标**：把 Ch 19–23 拼成一条能跑通的 pipeline：`tensor + linalg` → `memref` → LLVM dialect → LLVM IR → 可执行结果。中途故意制造两类失败，并用诊断信息定位到具体缺了哪个 pass。

> **环境**：`/home/ubuntu/llvm/trees/ct-instr-trunk`（只读），`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，LLVM/MLIR `24.0.0git`。所有产物写在 `/tmp/mlir-practice-d`。

> **前置**：Ch 19（legality）、Ch 20（TypeConverter）、Ch 21（partial conversion 与 cast）、Ch 22（One-Shot Bufferize）、Ch 23（ABI 与 translate）。

## D.1 任务与验收

你要交付一条**别人能照着复现**的 pipeline，而不是一堆命令。

四个检查点：

| 检查点 | 输入 | 期望输出 | 验收命令 |
|---|---|---|---|
| C1 | `arith` 常量加法 | runner 打印 `42` | `mlir-runner c1.llvm.mlir -e main --entry-point-result=i32` |
| C2 | `memref.alloc/store/load` | runner 打印 `7`，`llc` 出汇编 | 同上 + `llc -O2` |
| C3 | `tensor.empty + linalg.fill + tensor.extract` | runner 打印 `2.000000e+00` | `--entry-point-result=f32` |
| C4 | C3/C4 的 pipeline 各去掉一个 pass | 出现可定位的错误，且你能说出缺的是哪个 | 见 D.7 / D.8 |

交付物：

1. `c1.mlir`、`c2.mlir`、`c3.mlir`、`c4.mlir` 四份输入；
2. 每条 pipeline 的完整命令与真实输出（贴进答案）；
3. 一份“失败 → 诊断 → 根因 → 修法”的记录（C4）；
4. `c2.s` 里 `malloc` 的实参是多少字节，为什么。

验收标准：

- C1–C3 的输出与本文一致（数值可以不同，但类型与格式一致）；
- C4 的两类失败都能在**不改 IR** 的前提下，仅靠增删 pass 修好；
- 能指出 C3 的 pipeline 里每个 pass 的作用，以及删掉它会发生什么。

> 本练习不要求编译 C++，也不要求链接外部库；`mlir-runner` 的 JIT 足够验证语义。涉及 `llc` 的部分只验证“能出汇编”，不验证可执行文件。

## D.2 先建立直觉：五个检查点

```
  c3.mlir（tensor + linalg）
        │
        │ ① -one-shot-bufferize=bufferize-function-boundaries
        ▼
   memref + linalg-on-memref
        │
        │ ② -convert-linalg-to-loops
        ▼
   memref + scf.for + arith
        │
        │ ③ -convert-scf-to-cf
        ▼
   memref + cf.br + arith
        │
        │ ④ --convert-to-llvm
        ▼
   LLVM dialect（llvm.func / llvm.call / !llvm.struct）
        │
        ├── ⑤a mlir-runner（JIT，最快）
        └── ⑤b mlir-translate --mlir-to-llvmir → llc
```

四个箭头对应四个“验收点”。任何一步没有真正完成，错误都会推到下一步：

| 漏掉 | 现象 |
|---|---|
| ① | 函数签名还是 tensor，`--convert-to-llvm` 只降一半 |
| ② | `linalg.fill` 留在 LLVM dialect IR 里，translate 解析失败 |
| ③ | `scf.for` 留在里面，`mlir-runner` 报 `Dialect 'scf' not found` |
| ④ | 没有任何 `llvm.*`，translate 直接失败 |

## D.3 Step 0：环境

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
mkdir -p /tmp/mlir-practice-d && cd /tmp/mlir-practice-d
$T/bin/mlir-opt --version
```

本机实测：

```text
LLVM (http://llvm.org/):
  LLVM version 24.0.0git
  Optimized build with assertions.
```

确认三个工具都在：

```bash
ls $T/bin/mlir-opt $T/bin/mlir-translate $T/bin/mlir-runner $T/bin/llc
```

## D.4 Step 1（C1）：最小 LLVM 路径

### 输入

```bash
cat > c1.mlir <<'EOF'
func.func @main() -> i32 {
  %a = arith.constant 40 : i32
  %b = arith.constant 2 : i32
  %r = arith.addi %a, %b : i32
  return %r : i32
}
EOF
```

### 降级

```bash
$T/bin/mlir-opt c1.mlir --convert-to-llvm -o c1.llvm.mlir
```

本机实测：

```text
module {
  llvm.func @main() -> i32 {
    %0 = llvm.mlir.constant(40 : i32) : i32
    %1 = llvm.mlir.constant(2 : i32) : i32
    %2 = llvm.mlir.constant(42 : i32) : i32
    llvm.return %2 : i32
  }
}
```

### 验证（JIT）

```bash
$T/bin/mlir-runner c1.llvm.mlir -e main --entry-point-result=i32
```

本机实测：

```text
42
```

### 验证（translate + llc）

```bash
$T/bin/mlir-translate --mlir-to-llvmir c1.llvm.mlir -o c1.ll
$T/bin/llc -O2 c1.ll -o c1.s
grep -A6 '^main:' c1.s
```

本机实测：

```text
main:                                   # @main
	.cfi_startproc
# %bb.0:
	movl	$42, %eax
	retq
```

C1 达成。这一步没有 memref、没有 bufferization，所以 `--convert-to-llvm` 一步到位。

## D.5 Step 2（C2）：引入 memref

### 输入

```bash
cat > c2.mlir <<'EOF'
func.func @main() -> i32 {
  %c0 = arith.constant 0 : index
  %cst = arith.constant 7 : i32
  %buf = memref.alloc() : memref<4xi32>
  memref.store %cst, %buf[%c0] : memref<4xi32>
  %v = memref.load %buf[%c0] : memref<4xi32>
  return %v : i32
}
EOF
```

### 降级并验证

```bash
$T/bin/mlir-opt c2.mlir --convert-to-llvm -o c2.llvm.mlir
$T/bin/mlir-runner c2.llvm.mlir -e main --entry-point-result=i32
```

本机实测：

```text
7
```

### 看 ABI：descriptor 与 malloc

```bash
$T/bin/mlir-translate --mlir-to-llvmir c2.llvm.mlir -o c2.ll
$T/bin/llc -O2 c2.ll -o c2.s
sed -n '/^main:/,/retq/p' c2.s
```

本机实测（节选）：

```text
main:                                   # @main
	.cfi_startproc
# %bb.0:
	pushq	%rax
	.cfi_def_cfa_offset 16
	movl	$16, %edi
	callq	malloc@PLT
	movl	$7, (%rax)
	movl	$7, %eax
	popq	%rcx
	.cfi_def_cfa_offset 8
	retq
```

`malloc(16)` 的来源：4 个 `i32` = 16 字节。这里的 `memref.alloc` **没有** alignment 属性，所以不需要对齐 slack。

对比 C3：bufferization 生成的 `memref.alloc() alignment = 64` 会变成 `malloc(80)`（16 + 64 slack），并且汇编里多出 `addq $63, %rax; andq $-64, %rax`。同一个 op，属性不同，ABI 与开销就不同。

`movl $7, (%rax)` 是 `memref.store`，`movl $7, %eax` 是返回值——优化器把 load 直接折叠成了常量。

## D.6 Step 3（C3）：tensor + linalg

### 输入

```bash
cat > c3.mlir <<'EOF'
func.func @main() -> f32 {
  %c0 = arith.constant 0 : index
  %cst = arith.constant 2.0 : f32
  %empty = tensor.empty() : tensor<4xf32>
  %f = linalg.fill ins(%cst : f32) outs(%empty : tensor<4xf32>) -> tensor<4xf32>
  %v = tensor.extract %f[%c0] : tensor<4xf32>
  return %v : f32
}
EOF
```

### 完整 pipeline

```bash
$T/bin/mlir-opt c3.mlir \
  -one-shot-bufferize=bufferize-function-boundaries \
  -convert-linalg-to-loops \
  -convert-scf-to-cf \
  --convert-to-llvm \
  -o c3.llvm.mlir
```

顺序不能换：bufferize 之前不能 `convert-linalg-to-loops`（linalg-on-tensor 的 loop 版本会引入更多 tensor op）；`convert-scf-to-cf` 必须在 `--convert-to-llvm` 之前，因为 scf 没有注册 `ConvertToLLVMPatternInterface`。

### 每步验收

先只跑前两步，确认 bufferization 与 linalg 降级：

```bash
$T/bin/mlir-opt c3.mlir -one-shot-bufferize=bufferize-function-boundaries \
  -convert-linalg-to-loops | head -16
```

本机实测：

```text
module {
  func.func @main() -> f32 {
    %c1 = arith.constant 1 : index
    %c4 = arith.constant 4 : index
    %c0 = arith.constant 0 : index
    %cst = arith.constant 2.000000e+00 : f32
    %alloc = memref.alloc() alignment = 64 : memref<4xf32>
    scf.for %arg0 = %c0 to %c4 step %c1 {
      memref.store %cst, %alloc[%arg0] : memref<4xf32>
    }
    %0 = memref.load %alloc[%c0] : memref<4xf32>
    return %0 : f32
  }
}
```

`linalg.fill` 变成了 `scf.for` + `memref.store`，函数已经是 memref（`bufferize-function-boundaries` 生效）。

### 运行

```bash
$T/bin/mlir-runner c3.llvm.mlir -e main --entry-point-result=f32
```

本机实测：

```text
2.000000e+00
```

### 检查是否降干净

```bash
grep -nE 'scf\.|arith\.|memref\.|tensor\.|linalg\.' c3.llvm.mlir
grep -c 'unrealized_conversion_cast' c3.llvm.mlir
```

本机实测：

```text
（第一条无输出）
0
```

没有残留方言、没有欠条，才算 C3 真正完成。

## D.7 Step 4（C4a）：漏掉 `convert-scf-to-cf`

故意从 pipeline 里删掉 `-convert-scf-to-cf`：

```bash
$T/bin/mlir-opt c3.mlir -one-shot-bufferize=bufferize-function-boundaries \
  -convert-linalg-to-loops --convert-to-llvm -o c3-bad1.mlir
echo "mlir-opt exit=$?"
$T/bin/mlir-runner c3-bad1.mlir -e main --entry-point-result=f32 2>&1 | head -3
```

本机实测：

```text
mlir-opt exit=0
loc("c3-bad1.mlir":33:5): error: Dialect `scf' not found for custom op 'scf.for' 
could not parse the input IR
```

### 诊断思路

1. `mlir-opt` 退出码是 0 → 说明这不是 conversion 失败，而是 partial conversion 放过；
2. runner 报 `Dialect 'scf' not found` → 说明 IR 里还有 `scf.for`；
3. 为什么 `--convert-to-llvm` 没降它？因为 `scf` 没有实现 `ConvertToLLVMPatternInterface`。

用 debug 日志验证第 3 点（debug 必须加在完整 pipeline 上，单独跑 `mlir-opt c3-bad1.mlir` 不会重新发现接口）：

```bash
$T/bin/mlir-opt c3.mlir -one-shot-bufferize=bufferize-function-boundaries \
  -convert-linalg-to-loops --convert-to-llvm -debug-only=convert-to-llvm 2>&1 \
  | grep 'dialect interface' | sed 's/.*found dialect interface for //' | sort -u
```

本机实测：

```text
arith
cf
complex
func
index
math
memref
ub
```

清单里没有 `scf`——这就是根因。

### 修法

把 `-convert-scf-to-cf` 加回去即可，不需要改任何 IR。

## D.8 Step 5（C4b）：不开函数边界 bufferize

前面 C3 的 `@main` 不接收 tensor，所以函数边界开不开都能跑。要暴露边界问题，需要一个**签名里带 tensor 的函数**：

```bash
cat > c4.mlir <<'EOF'
func.func @pub(%t: tensor<4xf32>) -> f32 {
  %c0 = arith.constant 0 : index
  %v = tensor.extract %t[%c0] : tensor<4xf32>
  return %v : f32
}
func.func @main() -> f32 {
  %cst = arith.constant 2.0 : f32
  return %cst : f32
}
EOF
$T/bin/mlir-opt c4.mlir -one-shot-bufferize -convert-linalg-to-loops \
  -convert-scf-to-cf --convert-to-llvm -o c4-bad.mlir
echo "mlir-opt exit=$?"
$T/bin/mlir-translate --mlir-to-llvmir c4-bad.mlir 2>&1 | head -3
```

本机实测：

```text
mlir-opt exit=0
c4-bad.mlir:4:10: error: Dialect `bufferization' not found for custom op 'bufferization.to_buffer' 
    %1 = bufferization.to_buffer %arg0 : tensor<4xf32> to memref<4xf32, strided<[?], offset: ?>>
```

### 诊断思路

1. `mlir-opt` 退出码 0 → 又是 partial conversion 放过；
2. 错误指向 `bufferization.to_buffer`，说明函数签名没变 memref，bufferization 只能在函数内部用边界 op 过渡；
3. `func.func @pub` 仍是 tensor 签名 → `--convert-to-llvm` 无法转换 → 留下 `func.func` + `to_buffer`。

看一眼就明白：

```bash
grep -nE 'func.func|to_buffer|to_tensor' c4-bad.mlir | head
```

本机实测：

```text
2:  func.func @pub(%arg0: tensor<4xf32>) -> f32 {
4:    %1 = bufferization.to_buffer %arg0 : tensor<4xf32> to memref<4xf32, strided<[?], offset: ?>>
```

### 修法

加上 `bufferize-function-boundaries`：

```bash
$T/bin/mlir-opt c4.mlir -one-shot-bufferize=bufferize-function-boundaries \
  -convert-linalg-to-loops -convert-scf-to-cf --convert-to-llvm -o c4-good.mlir
$T/bin/mlir-runner c4-good.mlir -e main --entry-point-result=f32
```

本机实测：

```text
2.000000e+00
```

这次 `@pub` 的参数也变成 `memref<4xf32, strided<[?], offset: ?>>`，边界 op 消失。

## D.9 评分表

| 项 | 分值 | 判据 |
|---|---|---|
| C1 跑出 42 | 15 | `mlir-runner` 输出 |
| C2 跑出 7 + 汇编 | 20 | runner + `llc` + `malloc` 字节数解释 |
| C3 跑出 2.0 | 25 | 四个 pass 顺序正确，且 `grep` 无残留 |
| C4a 诊断 | 15 | 能说出 scf 没有 ConvertToLLVM 接口 |
| C4b 诊断 | 15 | 能说出函数边界默认不 bufferize |
| 可复现性 | 10 | 命令与输出能被他人在同版本上复现 |

80 分以上算通过。C3 的 `grep` 检查是关键：只跑出数字不算完成，必须证明 IR 降干净了。

## D.10 参考解答与排错

<details>
<summary>参考解答 1：C3 的完整命令与预期输出</summary>

```bash
cd /tmp/mlir-practice-d
$T/bin/mlir-opt c3.mlir \
  -one-shot-bufferize=bufferize-function-boundaries \
  -convert-linalg-to-loops \
  -convert-scf-to-cf \
  --convert-to-llvm \
  -o c3.llvm.mlir
$T/bin/mlir-runner c3.llvm.mlir -e main --entry-point-result=f32
```

预期最后一行：

```text
2.000000e+00
```

可选：再走一遍 translate + llc，确认 `malloc(80)` 与 `movl $0x40000000`（2.0f 的位模式）出现在汇编里。

```bash
$T/bin/mlir-translate --mlir-to-llvmir c3.llvm.mlir -o c3.ll
$T/bin/llc -O2 c3.ll -o c3.s
grep -E 'malloc|0x40000000|2\.0' c3.s | head
```

</details>

<details>
<summary>参考解答 2：为什么 `--convert-to-llvm` 不降 scf，却也不报错</summary>

两个事实叠加：

1. `--convert-to-llvm` 用 `applyPartialConversion`，只把 LLVM dialect 标 legal（`ConvertToLLVMPass.cpp:118` / `:172`）；
2. `scf` 没有注册 `ConvertToLLVMPatternInterface`，target 里也没把 `scf` 标 illegal。

于是在 partial 语义下，`scf.for` 是 “Unknown”：`isIllegal` 返回 `false`（`DialectConversion.cpp:4034`），driver 不认为失败。

要让它报错，只有两条路：

- 把 `scf` 显式标 illegal（自己写 pass）；
- 或者在 pipeline 里先把它降掉（本练习的做法）。

这也是为什么“看退出码”不能作为降级是否完成的判据。

</details>

<details>
<summary>参考解答 3：ABI 检查——`_mlir_ciface_` 何时出现</summary>

给函数加 `llvm.emit_c_interface`：

```bash
cat > abi.mlir <<'EOF'
func.func @sum(%m: memref<4xf32>) -> f32 attributes {llvm.emit_c_interface} {
  %c0 = arith.constant 0 : index
  %v = memref.load %m[%c0] : memref<4xf32>
  return %v : f32
}
EOF
$T/bin/mlir-opt abi.mlir --convert-to-llvm -o abi.llvm.mlir
$T/bin/mlir-translate --mlir-to-llvmir abi.llvm.mlir | grep '^define'
```

预期：

```text
define float @sum(ptr %0, ptr %1, i64 %2, i64 %3, i64 %4) {
define float @_mlir_ciface_sum(ptr %0) {
```

两条 `define` 都要在。如果只有第一条，说明属性没写对（拼写是 `llvm.emit_c_interface`，不是 `emit_c_interface`）。

`mlir-runner` 调的是哪个入口取决于 `-e` 的写法：`-e sum` 走 `@sum`，`-e _mlir_ciface_sum` 走包装函数。

</details>

<details>
<summary>参考解答 4（加分）：`malloc` 的字节数是怎么算出来的</summary>

两个例子要分开看。

**C2（手写 `memref.alloc`，无 alignment）**

```text
memref<4xi32> = 4 × 4 = 16 字节
→ malloc(16)
```

**C3（bufferization 生成的 `memref.alloc() alignment = 64`）**

```text
payload = 16 字节
slack   = 64 字节（保证 aligned ptr 能落在 64 字节边界）
→ malloc(80)
```

C3 的汇编里因此多出对齐代码：

```text
	movl	$80, %edi
	callq	malloc@PLT
	addq	$63, %rax
	andq	$-64, %rax
```

验证方法：把 C3 的 bufferization 加上 `buffer-alignment=16`，重新生成，观察 `malloc` 参数与对齐指令变化：

```bash
$T/bin/mlir-opt c3.mlir -one-shot-bufferize='bufferize-function-boundaries buffer-alignment=16' \
  -convert-linalg-to-loops -convert-scf-to-cf --convert-to-llvm -o c3-a16.mlir
$T/bin/mlir-translate --mlir-to-llvmir c3-a16.mlir -o c3-a16.ll
$T/bin/llc -O2 c3-a16.ll -o c3-a16.s
grep -B2 -A2 'malloc' c3-a16.s
```

本机实测：

```text
	movl	$32, %edi
	callq	malloc@PLT
	addq	$15, %rax
	andq	$-16, %rax
```

`malloc` 从 80 降到 32（16 payload + 16 slack），对齐指令也从 `$63/-64` 变成 `$15/-16`。**alignment 不是免费参数**，它直接改变分配大小与指令数。

</details>

## D.11 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/ConvertToLLVM/ConvertToLLVMPass.cpp`**
  - 读哪里：100–200 行
  - 能学到什么：为什么 partial conversion 会放过 scf
  - 和本练习关联：D.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Dialect/Bufferization/Transforms/OneShotAnalysis.cpp`**
  - 读哪里：1681–1710
  - 能学到什么：`testAnalysisOnly` 与 `copyBeforeWrite` 的分支
  - 和本练习关联：D.6、D.8
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/Bufferization.md`**
  - 读哪里：255–290（`bufferize-function-boundaries` 的说明）
  - 能学到什么：为什么函数边界默认不开
  - 和本练习关联：D.8
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/TargetLLVMIR.md`**
  - 读哪里：translation 注册与类型映射
  - 能学到什么：`mlir-translate` 的方言子集从哪来
  - 和本练习关联：D.7
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/test/Integration/`**
  - 读哪里：挑一个 `*-runner` 的 `.mlir`，看它的 RUN 行
  - 能学到什么：官方端到端测试的 pipeline 写法（比本练习长得多）
  - 和本练习关联：D.6

练习到这里，Part 4 的四个环节（legality、type conversion、casts、bufferization）已经串成一条可验证的链。下一章开始进入真实子系统：Linalg / Tensor / Vector 的 lowering 与它的维护热点。

→ 下一章：[Ch 24 Linalg、Tensor、Vector](./24-linalg-tensor-vector.md)
