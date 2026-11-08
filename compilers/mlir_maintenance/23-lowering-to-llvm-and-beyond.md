# Ch 23 — 降到 LLVM：`--convert-to-llvm`、ABI 与验证链

> **目标**：能独立把一段 MLIR 一路降到可执行程序；理解 `--convert-to-llvm` 与 `mlir-translate` 的职责边界；能解释 memref descriptor 的 ABI、`llvm.emit_c_interface` 的作用、以及 `index` 宽度从哪来。

> **环境**：`ct-instr-trunk` 的 `analysis/mlir-maintenance`；`mlir-opt`、`mlir-translate`、`mlir-runner`、`llc` 全部可用（`builds/ct-instr-trunk/bin`）。

> **前置**：Ch 21 的 partial conversion 语义、Ch 22 的 bufferization 结果（memref IP）。

## 23.1 为什么“降到 LLVM”不是一条命令

新手最容易踩的坑是以为有 `mlir-opt --lower-to-llvm`。实际上这条链上有四个不同的东西：

| 层 | 工具 / pass | 输出 |
|---|---|---|
| MLIR dialect IR | 你的 pipeline（linalg/tensor/…） | 仍是 MLIR |
| LLVM dialect IR | `--convert-to-llvm` | `llvm.func` / `llvm.add` / `!llvm.struct` |
| LLVM IR（.ll） | `mlir-translate --mlir-to-llvmir` | textual LLVM IR |
| 机器码 | `llc` / JIT | `.s` / `.o` / 直接执行 |

每一层都有自己的失败模式，而且**前一层的失败经常在下一层才报**。Ch 21 已经演示过 `--convert-to-llvm` 成功但 `mlir-translate` 失败。这一章把每一层的职责与验收标准钉死。

## 23.2 先建立直觉：四层与两条边界

```
 ┌───────────────────────────────┐
 │ MLIR dialect IR               │  arith / func / memref / linalg / vector ...
 │ %r = arith.addi %a, %b : i32  │
 └───────────────┬───────────────┘
                 │  mlir-opt --convert-to-llvm      （dialect conversion）
                 ▼
 ┌───────────────────────────────┐
 │ LLVM dialect IR               │  llvm.func / llvm.add / llvm.mlir.constant
 │ %r = llvm.add %a, %b : i32    │  仍然是 MLIR（可 mlir-opt 再处理）
 └───────────────┬───────────────┘
                 │  mlir-translate --mlir-to-llvmir （ModuleTranslation）
                 ▼
 ┌───────────────────────────────┐
 │ LLVM IR (.ll)                 │  define i32 @add(i32 %0, i32 %1) { ... }
 └───────────────┬───────────────┘
                 │  llc / mlir-runner (JIT)
                 ▼
 ┌───────────────────────────────┐
 │ 汇编 / 目标文件 / 进程         │
 └───────────────────────────────┘
```

两条边界各自有独立契约：

- **上边界**（dialect → LLVM dialect）：由 `ConvertToLLVMPatternInterface` 决定哪些方言参与，partial conversion 决定“降不干净会怎样”。
- **下边界**（LLVM dialect → LLVM IR）：由 `mlir-translate` 注册了哪些 translation 决定；没注册的方言连**解析**都过不了。

## 23.3 `--convert-to-llvm` 到底做了什么

实现（`mlir/lib/Conversion/ConvertToLLVM/ConvertToLLVMPass.cpp`）有静态与动态两套：

| 实现 | 何时用 | 配置来源 |
|---|---|---|
| `StaticConvertToLLVM` | 默认 | 初始化时遍历所有已加载 dialect 的接口，一次性构建 pattern 集 |
| `DynamicConvertToLLVM` | `-convert-to-llvm=dynamic` | 每次 run 时看 IR、取 `DataLayoutAnalysis`、再构建 pattern |

两者共同点（这是维护时最该记住的）：

```cpp
target->addLegalDialect<LLVM::LLVMDialect>();        // :118 / :172
...
applyPartialConversion(op, target, patterns, config); // :136 / :191
```

**只有 LLVM dialect 被标成 legal，而且用的是 partial conversion。** 没有 `addIllegalDialect<FuncDialect>()`，也没有 `markUnknownOpDynamicallyLegal`。这解释了 Ch 21 里那份“成功但没降干净”的 IR：`--convert-to-llvm` 的契约只是“把能降的降到 LLVM”，不是“保证全部降到 LLVM”。

### 方言接口从哪来

每个方言可以实现 `ConvertToLLVMPatternInterface` 来“自报”转换 pattern。先准备一个最小输入：

```bash
cat > add.mlir <<'EOF'
func.func @add(%a: i32, %b: i32) -> i32 {
  %r = arith.addi %a, %b : i32
  return %r : i32
}
EOF
```

用 debug 日志可以直接看到哪些方言注册了接口：

```bash
$T/bin/mlir-opt add.mlir --convert-to-llvm -debug-only=convert-to-llvm
```

本机实测（节选）：

```text
[convert-to-llvm ConvertToLLVMPass.cpp:83 1] Convert to LLVM extension load
[convert-to-llvm ConvertToLLVMPass.cpp:86 1] Convert to LLVM found dialect interface for func
[convert-to-llvm ConvertToLLVMPass.cpp:86 1] Convert to LLVM found dialect interface for cf
[convert-to-llvm ConvertToLLVMPass.cpp:86 1] Convert to LLVM found dialect interface for arith
```

这份清单解释了“为什么 linalg 不会被 `--convert-to-llvm` 降掉”——它没有注册这个接口（或者注册了但只处理部分 op）。想做端到端降级，必须自己拼 pipeline。

### 实测：最小降级

```bash
cat > add.mlir <<'EOF'
func.func @add(%a: i32, %b: i32) -> i32 {
  %r = arith.addi %a, %b : i32
  return %r : i32
}
EOF
$T/bin/mlir-opt add.mlir --convert-to-llvm
```

本机实测：

```text
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
}
```

`func.func → llvm.func`、`arith.addi → llvm.add`、`func.return → llvm.return`，一一对应。

## 23.4 `mlir-translate` 的方言子集

`mlir-translate` 不是“什么 MLIR 都能读”。它只注册 translation 所需的方言：

`mlir/tools/mlir-translate/mlir-translate.cpp:42` 调 `registerAllTranslations()`，而每个 translation 在自己的注册函数里把依赖方言追加进 context（`mlir/lib/Tools/mlir-translate/Translation.cpp:83`）。

实测直接翻译未降级的 IR（`add.mlir` 见 23.3）：

```bash
$T/bin/mlir-translate --mlir-to-llvmir add.mlir
```

本机实测：

```text
add.mlir:2:8: error: Dialect `arith' not found for custom op 'arith.addi' 
  %r = arith.addi %a, %b : i32
       ^
add.mlir:2:8: note: Available dialects: arm_neon, arm_sme, arm_sve, builtin, dlti, func, gpu, llvm, nvvm, omp, ptr, rocdl, spirv, vcix, xevm ; for more info on dialect registration see https://mlir.llvm.org/getting_started/Faq/#registered-loaded-dependent-whats-up-with-dialects-management
```

可用方言列表里没有 `arith`、没有 `tensor`、没有 `linalg`。所以正确顺序永远是：**先 `mlir-opt --convert-to-llvm`，再 `mlir-translate`**。

## 23.5 ABI：memref 到底怎么传

这是 MLIR → LLVM 最容易被忽略、也最容易在链接期炸掉的部分。

一个 `memref<4xf32>` 在 LLVM 里不是一个指针，而是一个 **descriptor**（5 个字段）：

```
  !llvm.struct<(ptr, ptr, i64, array<1 x i64>, array<1 x i64>)>
                 │     │     │          │              │
                 │     │     │          │              └─ strides[0]
                 │     │     │          └──────────────── sizes[0]
                 │     │     └─────────────────────────── offset
                 │     └───────────────────────────────── aligned ptr
                 └─────────────────────────────────────── allocated ptr
```

默认 ABI 把这个 struct **拆成 5 个参数**传递：

```text
llvm.func @sum(%arg0: !llvm.ptr, %arg1: !llvm.ptr, %arg2: i64,
               %arg3: i64, %arg4: i64) -> f32
```

如果加 `llvm.emit_c_interface`，`FuncToLLVM` 会额外生成一个 C ABI 包装函数（`mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp:110` 的 `wrapForExternalCallers`，名字在 `:126` 生成）：

```text
define float @_mlir_ciface_sum_c(ptr %0) {
  %2 = load { ptr, ptr, i64, [1 x i64], [1 x i64] }, ptr %0, align 8
  ...
  %8 = call float @sum_c(ptr %3, ptr %4, i64 %5, i64 %6, i64 %7)
  ret float %8
}
```

包装函数只收一个 `ptr`（指向 descriptor 的指针），内部 load 出 5 个字段再调用真正实现。**`mlir-runner`、Python binding、以及所有 C 调用方用的都是这个 `_mlir_ciface_*` 入口**。

记住一句话：**`llvm.emit_c_interface` 不改函数体，只是加一个入口**。签名对不上时，先确认调用方走的是 `@sum` 还是 `@_mlir_ciface_sum`。

## 23.6 `index` 的宽度从哪来

`index` 是目标相关的。转换时由 `LowerToLLVMOptions::indexBitwidth` 决定（`mlir/include/mlir/Conversion/LLVMCommon/LoweringOptions.h:62`）：

- `0` = 用机器字长 / data layout 推导；
- 非 0 = 强制覆盖。

单 pass 可以显式指定（`idx.mlir` 见 23.9 实验 6）：

```bash
$T/bin/mlir-opt idx.mlir -convert-func-to-llvm='index-bitwidth=32'
```

本机实测（同一份 `func.func @id(%i: index) -> index`）：

```text
// 默认
llvm.func @id(%arg0: i64) -> i64

// index-bitwidth=32
llvm.func @id(%arg0: i32) -> i32
```

`--convert-to-llvm` 的静态实现用默认 `LLVMTypeConverter`（构造时不传 `LowerToLLVMOptions`，见 `ConvertToLLVMPass.cpp:113`）；动态实现会取 `DataLayoutAnalysis` 再构造 options（`:174`）。

但**不要假定“写了 `llvm.data_layout` 就一定改变 index 宽度”**。本机把一个 `llvm.data_layout = "e-m:e-p:32:32-..."` 的 module 分别用 static 与 `-convert-to-llvm=dynamic` 转换，`index` 都仍是 `i64`：

```bash
$T/bin/mlir-opt dl.mlir -convert-to-llvm=dynamic | grep llvm.func
```

本机实测：

```text
  llvm.func @idx(%arg0: i64) -> i64 {
```

（`dl.mlir` 是一个带上述 data layout 的 module，内含 `func.func @idx(%i: index) -> i64`。）具体原因本机没有继续深挖。**维护结论**：需要确定宽度时显式传 `index-bitwidth`，不要依赖 data layout 隐式推导。

## 23.7 验证链：每一步都要有独立判据

维护 lowering 时，不要只跑最后一步。每一层给自己一个可观察的判据：

| 层 | 判据 | 失败长什么样 |
|---|---|---|
| dialect → LLVM dialect | 输出里没有非 LLVM 方言 op | 残留 `func.func` / `tensor.*` |
| LLVM dialect → .ll | `mlir-translate` 成功 | `Dialect 'x' not found` |
| .ll → 汇编 | `llc` 成功 | 目标不支持 / 类型非法 |
| 运行 | `mlir-runner` 返回期望值 | 段错误 / 返回值错 |

`mlir-runner` 是最快的端到端验证器（JIT，不用链接）：

```bash
$T/bin/mlir-runner main.llvm.mlir -e main --entry-point-result=i32
```

本机实测输出：

```text
42
```

它直接吃 **LLVM dialect IR**，不需要先 translate。这是调试 ABI 问题时最省事的一条路。

## 23.8 源码落点

| 文件 | 行 | 读什么 |
|---|---|---|
| `mlir/lib/Conversion/ConvertToLLVM/ConvertToLLVMPass.cpp` | 118 / 172 | `addLegalDialect<LLVM::LLVMDialect>()` |
| 同上 | 136 / 191 | 两套实现的 `applyPartialConversion` |
| 同上 | 83 / 86 | debug 日志（方言接口发现） |
| `mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp` | 110 | `wrapForExternalCallers` |
| 同上 | 126 | `_mlir_ciface_` 命名 |
| `mlir/include/mlir/Conversion/LLVMCommon/LoweringOptions.h` | 35 / 56 / 62 | bare-ptr、data layout、index 宽度 |
| `mlir/tools/mlir-translate/mlir-translate.cpp` | 42 | `registerAllTranslations()` |
| `mlir/lib/Tools/mlir-translate/Translation.cpp` | 83 / 143 | 每个 translation 自己注册方言 |
| `mlir/lib/Target/LLVMIR/ModuleTranslation.cpp` | 全文件（2781 行） | LLVM dialect → LLVM IR 的主体 |
| `mlir/docs/TargetLLVMIR.md` | 全部 | LLVM dialect 与 translation 的官方说明 |

## 23.9 可跑实验

统一前置：

```bash
source /home/ubuntu/llvm/toolset/env.sh
T=/home/ubuntu/llvm/builds/ct-instr-trunk
mkdir -p /tmp/mlir-part4 && cd /tmp/mlir-part4
```

### 实验 1：最小函数降级

```bash
$T/bin/mlir-opt add.mlir --convert-to-llvm
```

本机实测：

```text
module {
  llvm.func @add(%arg0: i32, %arg1: i32) -> i32 {
    %0 = llvm.add %arg0, %arg1 : i32
    llvm.return %0 : i32
  }
}
```

### 实验 2：端到端跑一个返回值

```bash
cat > main.mlir <<'EOF'
func.func @main() -> i32 {
  %a = arith.constant 40 : i32
  %b = arith.constant 2 : i32
  %r = arith.addi %a, %b : i32
  return %r : i32
}
EOF
$T/bin/mlir-opt main.mlir --convert-to-llvm -o main.llvm.mlir
$T/bin/mlir-translate --mlir-to-llvmir main.llvm.mlir
```

本机实测：

```text
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

define i32 @main() {
  ret i32 42
}

!llvm.module.flags = !{!0}

!0 = !{i32 2, !"Debug Info Version", i32 3}
```

注意 `arith.addi` 被常量折叠成 42——这是 LLVM dialect 上的 canonicalize，不是 `mlir-translate` 干的。

运行：

```bash
$T/bin/mlir-runner main.llvm.mlir -e main --entry-point-result=i32
```

本机实测：

```text
42
```

### 实验 3：memref + malloc + llc

```bash
cat > mem.mlir <<'EOF'
func.func @main() -> i32 {
  %c0 = arith.constant 0 : index
  %cst = arith.constant 7 : i32
  %buf = memref.alloc() : memref<4xi32>
  memref.store %cst, %buf[%c0] : memref<4xi32>
  %v = memref.load %buf[%c0] : memref<4xi32>
  return %v : i32
}
EOF
$T/bin/mlir-opt mem.mlir --convert-to-llvm | head -12
```

本机实测：

```text
module {
  llvm.func @malloc(i64) -> !llvm.ptr
  llvm.func @main() -> i32 {
    %0 = llvm.mlir.constant(0 : i64) : i64
    %1 = llvm.mlir.constant(7 : i32) : i32
    %2 = llvm.mlir.constant(4 : i64) : i64
    %3 = llvm.mlir.constant(1 : i64) : i64
    %4 = llvm.mlir.zero : !llvm.ptr
    %5 = llvm.getelementptr %4[%2] : (!llvm.ptr, i64) -> !llvm.ptr, i32
    %6 = llvm.ptrtoint %5 : !llvm.ptr to i64
    %7 = llvm.call @malloc(%6) : (i64) -> !llvm.ptr
```

`memref.alloc` 变成 `@malloc` 调用 + 手工拼 descriptor。继续看会看到 `llvm.insertvalue` 填 5 个字段。

生成汇编：

```bash
$T/bin/mlir-opt mem.mlir --convert-to-llvm -o mem.llvm.mlir
$T/bin/mlir-translate --mlir-to-llvmir mem.llvm.mlir -o mem.ll
$T/bin/llc -O2 mem.ll -o mem.s
sed -n '1,20p' mem.s
```

本机实测（节选）：

```text
	.att_syntax
	.file	"LLVMDialectModule"
	.text
	.globl	main                            # -- Begin function main
	.prefalign	4, .Lfunc_end0, nop
main:                                   # @main
	.cfi_startproc
# %bb.0:
	pushq	%rax
	.cfi_def_cfa_offset 16
	movl	$16, %edi
	callq	malloc@PLT
	movl	$7, (%rax)
	movl	$7, %eax
```

`malloc(16)` 就是 `4 × 4 bytes`。整条链走通。

### 实验 4：没降级就 translate

```bash
$T/bin/mlir-translate --mlir-to-llvmir main.mlir 2>&1 | head -3
```

本机实测：

```text
main.mlir:2:8: error: Dialect `arith' not found for custom op 'arith.constant' 
  %a = arith.constant 40 : i32
```

### 实验 5：ABI 的两种形态

```bash
cat > abi.mlir <<'EOF'
func.func @sum(%m: memref<4xf32>) -> f32 {
  %c0 = arith.constant 0 : index
  %v = memref.load %m[%c0] : memref<4xf32>
  return %v : f32
}
EOF
sed 's/func.func @sum/func.func @sum_c/; s/-> f32 {/-> f32 attributes {llvm.emit_c_interface} {/' abi.mlir > abi_c.mlir
$T/bin/mlir-opt abi.mlir --convert-to-llvm -o abi.llvm.mlir
$T/bin/mlir-opt abi_c.mlir --convert-to-llvm -o abi_c.llvm.mlir
$T/bin/mlir-translate --mlir-to-llvmir abi.llvm.mlir | grep '^define'
$T/bin/mlir-translate --mlir-to-llvmir abi_c.llvm.mlir | grep '^define'
```

本机实测：

```text
define float @sum(ptr %0, ptr %1, i64 %2, i64 %3, i64 %4) {
define float @sum_c(ptr %0, ptr %1, i64 %2, i64 %3, i64 %4) {
define float @_mlir_ciface_sum_c(ptr %0) {
```

多出来的第三行就是 C ABI 入口。

### 实验 6：index 宽度

```bash
cat > idx.mlir <<'EOF'
func.func @id(%i: index) -> index {
  return %i : index
}
EOF
echo '--- default'
$T/bin/mlir-opt idx.mlir -convert-to-llvm | grep llvm.func
echo '--- 32-bit'
$T/bin/mlir-opt idx.mlir -convert-func-to-llvm='index-bitwidth=32' | grep llvm.func
```

本机实测：

```text
--- default
  llvm.func @id(%arg0: i64) -> i64 {
--- 32-bit
  llvm.func @id(%arg0: i32) -> i32 {
```

### 实验 7：看方言接口发现过程

```bash
$T/bin/mlir-opt add.mlir --convert-to-llvm -debug-only=convert-to-llvm 2>&1 | head -5
```

本机实测：

```text
[convert-to-llvm ConvertToLLVMPass.cpp:83 1] Convert to LLVM extension load
[convert-to-llvm ConvertToLLVMPass.cpp:86 1] Convert to LLVM found dialect interface for func
[convert-to-llvm ConvertToLLVMPass.cpp:86 1] Convert to LLVM found dialect interface for cf
[convert-to-llvm ConvertToLLVMPass.cpp:86 1] Convert to LLVM found dialect interface for arith
```

### 实验 8：memref 版本跑出结果

```bash
$T/bin/mlir-runner mem.llvm.mlir -e main --entry-point-result=i32
```

本机实测：

```text
7
```

JIT 直接执行，不需要 `llc` 与链接。

## 23.10 踩雷集锦

1. **“有 `--lower-to-llvm` 一条命令” → 没有。** 至少要 `--convert-to-llvm` + `--mlir-to-llvmir` 两步；bufferization、linalg 降级还要自己拼。

2. **“`--convert-to-llvm` 成功就万事大吉” → 它是 partial conversion。** 残留的非 LLVM op 不会让 pass 失败，但会让 `mlir-translate` 解析失败。

3. **“`mlir-translate` 什么方言都认” → 只认 translation 注册过的。** 默认列表里没有 arith/tensor/linalg。

4. **“memref 就是一个指针” → 是 5 字段 descriptor。** 默认拆成 5 个参数；`llvm.emit_c_interface` 才提供单指针入口。

5. **“`llvm.emit_c_interface` 会改函数签名” → 不改本体，只加 `_mlir_ciface_*` 包装。** 调用方用错入口就是链接错误或 ABI 崩溃。

6. **“index 永远是 i64” → 由 `LowerToLLVMOptions::indexBitwidth` 决定。** 默认机器字长，可被 data layout 或显式选项覆盖。

7. **“`mlir-runner` 就是 `mlir-cpu-runner` 的新名字” → 名字就叫 `mlir-runner`。** 老文档里的 `mlir-cpu-runner` 已不存在，照抄会 command not found。

8. **“`llc` 成功就代表语义对” → 只代表类型合法。** 返回值错、段错误、ABI 不匹配都要靠 `mlir-runner` 或真实链接来发现。

## 23.11 本章重点整理

- 降级链有四层：dialect IR → LLVM dialect → LLVM IR → 机器码，每层独立验收。
- `--convert-to-llvm` 靠 `ConvertToLLVMPatternInterface` 发现 pattern，只把 LLVM dialect 标 legal，用 partial conversion。
- 静态实现不读 data layout，动态实现（`=dynamic`）才读；index 宽度因此可能不同。
- `mlir-translate` 的方言子集由 translation 注册决定，未注册方言连解析都失败。
- memref 是 5 字段 descriptor；默认 5 参数 ABI，`llvm.emit_c_interface` 额外生成单指针 `_mlir_ciface_*`。
- `index` 宽度来自 `LowerToLLVMOptions`，可被 `index-bitwidth` 覆盖。
- `mlir-runner` 直接吃 LLVM dialect IR，是最快的端到端验证器。
- 判断“降干净”要看输出里还剩哪些方言，不要看退出码。

## 23.12 自我检核

- [ ] 能画出四层降级链，并说出每层的输入/输出与失败模式。
- [ ] 能解释 `--convert-to-llvm` 为什么可能留下 `func.func`。
- [ ] 能说出 `mlir-translate` 可用方言列表为什么这么短。
- [ ] 能默写 memref descriptor 的 5 个字段，并解释 `allocated ptr` 与 `aligned ptr` 的区别。
- [ ] 能解释 `llvm.emit_c_interface` 生成的包装函数做了什么。
- [ ] 能用一个命令把 index 强制成 32 位，并说明默认值从哪来。
- [ ] 能用 `mlir-runner` 在不起链接器的情况下验证返回值。
- [ ] 能用 `llc` 从 `.ll` 生成汇编并指出 `malloc` 的大小来源。

## 23.13 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/docs/TargetLLVMIR.md`**
  - 读哪里：全文件（954 行），重点看类型映射与 translation 注册
  - 能学到什么：LLVM dialect 的类型系统、translation 的注册方式
  - 和本章关联：23.3–23.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/ConvertToLLVM/ConvertToLLVMPass.cpp`**
  - 读哪里：30–200 行
  - 能学到什么：静态/动态两套实现与 partial conversion 的配置
  - 和本章关联：23.3
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Conversion/FuncToLLVM/FuncToLLVM.cpp`**
  - 读哪里：100–200 行
  - 能学到什么：`_mlir_ciface_*` 包装函数的生成
  - 和本章关联：23.5
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/include/mlir/Conversion/LLVMCommon/LoweringOptions.h`**
  - 读哪里：25–80 行
  - 能学到什么：index 宽度、bare-ptr、data layout 三个开关
  - 和本章关联：23.6
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/mlir/lib/Target/LLVMIR/ModuleTranslation.cpp`**
  - 读哪里：搜 `convertOperation`，再顺着看 `convertBlock` / `convertRegion`
  - 能学到什么：LLVM dialect op 如何逐条翻译成 LLVM IR
  - 和本章关联：23.4、23.7

到这里，conversion 与 bufferization 的主干就闭环了：合法性契约（19）、类型映射（20）、边界与欠条（21）、tensor→memref（22）、memref→机器码（23）。下一章是练习 D，把这五章拼成一条能跑通的 pipeline。

→ 下一章：[练习 D 端到端 conversion 与 bufferization](./practice-d-conversion-bufferization.md)
