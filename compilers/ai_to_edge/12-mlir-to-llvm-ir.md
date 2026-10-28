# Ch 12 — 从 MLIR 到 LLVM IR：方言降级的真实样子

> **目标**：把 MLIR 的 `llvm` dialect 变成真正的 LLVM IR，看清这一转换里丢了什么、加了什么。学完这章，你能自己把一段 MLIR 一路降到 AArch64 并跑出数字。

> **环境**：MLIR 14.0.0、LLVM 14.0.0（Ubuntu 包）、`/usr/lib/llvm-14/bin/{mlir-opt,mlir-translate,llc}`

## 为什么需要这个？

Ch 10 停在「MLIR 的 `llvm` dialect」。那还不是 LLVM IR——它是**长得像** LLVM IR 的另一种 MLIR 方言。

这两者的差别很实际：

| | MLIR `llvm` dialect | LLVM IR |
|---|---|---|
| 语法 | `%0 = llvm.add %a, %b : i32` | `%0 = add i32 %a, %b` |
| 容器 | `llvm.func` 在 `module` 里 | `define` 在模块里 |
| 谁懂 | `mlir-opt` | `opt` / `llc` / `llvm-as` |
| 有无 dialect 概念 | 有（可以混别的 dialect） | 没有（纯 LLVM） |

`mlir-translate --mlir-to-llvmir` 就是这条边界。它是**单向**的（IR 进、IR 出），不做优化——优化交给后面的 `opt`/`llc`。

## 先建立直觉

把这一步想成**换一种文件格式**：

```
MLIR 文件（多种 dialect 混居）
    │  降级：全部收敛到 llvm dialect
    ▼
MLIR 文件（只剩 llvm dialect + builtin）
    │  mlir-translate
    ▼
LLVM IR 文本（.ll）
    │  llc
    ▼
目标汇编（.s）
```

关键：**在跨过这条边界之前，所有「MLIR 特有的东西」必须被消解掉。**

具体有三类必须消解：

1. `memref` → 拆成指针 + 尺寸 + stride（Ch 21 详谈）
2. `builtin.unrealized_conversion_cast` → 必须 reconcile 掉
3. 高阶函数（`func`）→ `llvm.func`，带明确调用约定

## 完整链路实测

源 IR（`code/02_mlir_lowering/matmul.mlir`）：

```mlir
module {
  func @matmul(%A: memref<4x8xf32>, %B: memref<8x4xf32>, %C: memref<4x4xf32>) {
    linalg.matmul ins(%A, %B : memref<4x8xf32>, memref<8x4xf32>)
                  outs(%C : memref<4x4xf32>)
    return
  }
}
```

### 第一步：降到循环

```bash
mlir-opt matmul.mlir --convert-linalg-to-loops
```

实测（节选）：

```mlir
scf.for %arg3 = %c0 to %c4 step %c1 {
  scf.for %arg4 = %c0 to %c4 step %c1 {
    scf.for %arg5 = %c0 to %c8 step %c1 {
      %0 = memref.load %arg0[%arg3, %arg5] : memref<4x8xf32>
      %1 = memref.load %arg1[%arg5, %arg4] : memref<8x4xf32>
      %2 = memref.load %arg2[%arg3, %arg4] : memref<4x4xf32>
      %3 = arith.mulf %0, %1 : f32
      %4 = arith.addf %2, %3 : f32
      memref.store %4, %arg2[%arg3, %arg4] : memref<4x4xf32>
    }
  }
}
```

到这里 `linalg.matmul` 的语义已经没了——只剩三层嵌套循环。这是**不可逆**的。

### 第二步：一路降到底

```bash
mlir-opt matmul.mlir \
  --convert-linalg-to-loops \
  --lower-affine \
  --convert-scf-to-std \
  --convert-std-to-llvm \
  --convert-arith-to-llvm \
  --convert-memref-to-llvm \
  --reconcile-unrealized-casts
```

六个 pass 的分工：

| pass | 作用 |
|---|---|
| `--convert-linalg-to-loops` | `linalg.matmul` → `scf.for` + `memref` |
| `--lower-affine` | affine 表达式（索引计算）→ 普通算术 |
| `--convert-scf-to-std` | `scf.for` → `std.br` 与分支（CFG 化） |
| `--convert-std-to-llvm` | `func`/`return`/`call` → `llvm.func`/`llvm.return`/`llvm.call` |
| `--convert-arith-to-llvm` | `arith.mulf`/`addf` → `llvm.fmulf`/`llvm.fadd` |
| `--convert-memref-to-llvm` | `memref.load`/`store`/类型 → `llvm.load`/`llvm.store`/结构体 |
| `--reconcile-unrealized-casts` | 清理转换过程产生的桥接节点 |

实测降级后的函数签名：

```mlir
llvm.func @matmul(%arg0: !llvm.ptr<f32>, %arg1: !llvm.ptr<f32>, %arg2: i64, %arg3: i64,
                  %arg4: i64, %arg5: i64, %arg6: i64,
                  %arg7: !llvm.ptr<f32>, %arg8: !llvm.ptr<f32>, %arg9: i64, %arg10: i64,
                  %arg11: i64, %arg12: i64, %arg13: i64,
                  %arg14: !llvm.ptr<f32>, %arg15: !llvm.ptr<f32>, %arg16: i64, %arg17: i64,
                  %arg18: i64, %arg19: i64, %arg20: i64)
```

**21 个参数**。三个 `memref` 各展开成 7 个：

```
memref<4x8xf32>  →  ptr, ptr, i64, i64, i64, i64, i64
                     ↑    ↑    ↑    ↑    ↑    ↑    ↑
                allocated aligned offset size0 size1 stride0 ... （stride1 共 7）
```

数一下：2 个 ptr + 5 个 i64 = 7。三个 memref = 21 个参数。

> **这就是 Ch 21 的主题**：你写的是「传一个 memref」，机器看到的是「传七个标量」。这个转换发生在**这一行命令**里。

### 第三步：转成 LLVM IR

```bash
mlir-translate --mlir-to-llvmir matmul.llvm.mlir > matmul.ll
```

实测输出（节选）：

```llvm
declare i8* @malloc(i64)
declare void @free(i8*)

define void @matmul(float* %0, float* %1, i64 %2, i64 %3, i64 %4, i64 %5, i64 %6,
                    float* %7, float* %8, i64 %9, i64 %10, i64 %11, i64 %12, i64 %13,
                    float* %14, float* %15, i64 %16, i64 %17, i64 %18, i64 %19, i64 %20) {
```

注意两件事：

1. **参数一模一样**（21 个），只是语法定量变成 LLVM 的 `float*` / `i64`。
2. **凭空出现 `declare i8* @malloc` 和 `declare void @free`**。

第二点是本章最重要的发现。

## 那个凭空出现的 `malloc`

源 IR 里没有任何堆分配。`memref<4x8xf32>` 是静态大小的。但 LLVM IR 里就是有这两个声明。

原因：`--convert-memref-to-llvm` 会为**每一种 memref 类型**准备好「运行时辅助函数」的声明，因为它不知道下游会不会用到动态分配。声明本身不占空间（只是 `declare`，不是 `define`），但：

> **在裸机（`-nostdlib`）上，这个 `declare` 可能变成链接错误**——如果你的 IR 真的调用了它。

实测验证一下：能不能在 Cortex-M3 上链接这段 IR？

```bash
llc -march=thumbv7m -filetype=obj matmul.ll -o matmul_cm3.o
arm-none-eabi-gcc -nostdlib -Wl,-T,link.ld driver.o matmul_cm3.o -o out.elf
```

如果链接器报 `undefined reference to 'malloc'`，说明**声明被用到了**（或者链接器保守处理）。

> **边缘部署的实用规则**：降级到 LLVM IR 之后，`grep` 一遍 `declare.*malloc`，并检查是否有 `call.*@malloc`。前者可以忽略（未使用的声明会被 llc 丢掉），后者必须处理（Ch 22 讲怎么改）。

## `unrealized_conversion_cast`：不加会怎样

实测对比。**不加** `--reconcile-unrealized-casts`：

```bash
mlir-opt matmul.mlir --convert-linalg-to-loops --lower-affine \
  --convert-scf-to-std --convert-std-to-llvm --convert-arith-to-llvm \
  --convert-memref-to-llvm | grep -c unrealized
```

实测：输出里残留 **6 个** `builtin.unrealized_conversion_cast`（`grep -c unrealized`）。

**加上** `--reconcile-unrealized-casts` 之后：

```bash
... --reconcile-unrealized-casts | grep -c unrealized
```

实测：`0`

残留会怎样？`mlir-translate` 直接失败（exit code 1），原始报错：

```
norecon.mlir:11:10: error: cannot be converted to LLVM IR: missing `LLVMTranslationDialectInterface`
registration for dialect for op: builtin.unrealized_conversion_cast
    %8 = builtin.unrealized_conversion_cast %7 : !llvm.struct<(ptr<f32>, ptr<f32>, i64, array<2 x i64>, array<2 x i64>)> to memref<4x8xf32>
         ^
```

注意错误里泄露了一个关键信息：`!llvm.struct<(ptr<f32>, ptr<f32>, i64, array<2 x i64>, array<2 x i64>)>`。

**这就是 memref 的真实内存表示**——2 个指针 + 1 个 i64（offset）+ 2 个「2 元素 i64 数组」（sizes 和 strides）。所以前面算的 7 个标量参数是：`2 ptr + 1 offset + 2 sizes + 2 strides`。看到这个结构，21 参数就不再是魔数。

> **错误直觉**：「`--reconcile-unrealized-casts` 是可选的美化」。  
> **正确认识**：它是**必需的收尾**。不加就无法 translate。它做的是「把转换过程中 A 类型 → B 类型的桥接节点消掉」，属于降级链路的固定步骤。

## 语义丢失的三个层次

这是本章要建立的判断力。「降级」不是一次性的，而是分层的：

```
① linalg.matmul
   知道：这是矩阵乘、维度 4×8×4、数据类型 f32
   能做：换成更好的循环顺序、向量化、tile
   ↓ --convert-linalg-to-loops
② scf.for × 3 + memref.load/store
   知道：三层循环、每次读三个值、算一次乘加
   能做：循环展开、循环交换、软件流水
   ↓ --convert-scf-to-std → --convert-std-to-llvm
③ llvm.br / llvm.load / llvm.fmulf
   知道：基本块、分支、地址计算
   能做：寄存器分配、指令选择、调度
   ↓ llc
④ 机器指令
```

**每降一层，就永久失去上一层能做的部分优化。**

这条规律解释了一个工程现象：为什么 AI 编译器要在**降级之前**尽量做完优化（Ch 7、Ch 8），而不是指望 LLVM 帮你做。LLVM 看不到 matmul，它只能看到循环。

## 跑通验证

```bash
cd code/02_mlir_lowering && ./build.sh
```

实测输出：

```
== 1) linalg -> loops -> LLVM dialect ==
== 2) LLVM dialect -> LLVM IR ==
== 3) LLVM IR -> AArch64 object ==
== 4) link (static) ==
== 5) run under qemu ==
11.0 18.0 10.0 11.0
12.0 20.0 19.0 12.0
13.0 17.0 18.0 13.0
14.0 14.0 17.0 14.0
```

这些数字是 **qemu 里跑的 AArch64 机器码**算出来的。Python 独立核对：

```
11.0 18.0 10.0 11.0
12.0 20.0 19.0 12.0
13.0 17.0 18.0 13.0
14.0 14.0 17.0 14.0
```

一致。

## 踩雷集锦

1. **以为 `llvm` dialect 就是 LLVM IR**：两者语法相似但不是一回事。`mlir-opt` 能吃 `llvm` dialect，`opt` 不能。必须 `mlir-translate`。
2. **漏掉 `--reconcile-unrealized-casts`**：translate 阶段直接失败。这是固定收尾步骤，不是可选优化。
3. **只看 `declare @malloc` 就以为要用堆**：`declare` 不代表调用。要 `grep call.*malloc` 才是真用了。
4. **指望 LLVM 保留 matmul 语义**：降级后它就是三层循环。所有矩阵乘级别的优化必须在这之前做完。
5. **在 MLIR 14 上用 MLIR 15+ 的写法**：`func.func` 不存在，写 `func`。`std.func` 也不存在（虽然它是 MLIR 14 内部的真实 op 名，但语法上只接受 `func`）。

## 本章重点整理

- `mlir-translate --mlir-to-llvmir` 是 MLIR 与 LLVM 的单向边界。
- 跨边界前必须消解三样东西：memref（拆 7 参数）、unrealized cast（reconcile）、高阶函数（改调用约定）。
- 每个 `memref<MxNxT>` 变成 7 个标量参数，三个 memref 就是 21 个。
- 降级后的 LLVM IR 会带 `declare @malloc`，即使源 IR 无堆分配。
- 每降一层就永久失去上一层的优化能力——这是「优化必须前置」的根本原因。

## 自我检核

- [ ] 能说出 MLIR `llvm` dialect 与 LLVM IR 的三个区别
- [ ] 不看笔记，数出「3 个 memref 参数」降级后是多少个标量参数，并说明为什么是 7
- [ ] 能解释 `--reconcile-unrealized-casts` 不加会怎样
- [ ] 知道 `declare @malloc` 与 `call @malloc` 的区别，以及为什么这个区别在裸机上很重要
- [ ] 能用「语义丢失的层次」解释为什么图级优化必须在降级前完成

## 延伸阅读

### 官方文档

- **[MLIR — Conversion to the LLVM Dialect](https://mlir.llvm.org/docs/ConversionToLLVMDialect/)**
  - **读哪里**：开头 `Overview` 与 `MemRef` 类型转换那一节
  - **为什么值得读**：本章 21 参数展开规则的权威定义，包括 7 个字段的确切含义与顺序
  - **注意**：文档跟主干走，MLIR 14 的字段顺序以你本机 `mlir-opt` 输出为准

- **[LLVM Language Reference — Functions](https://llvm.org/docs/LangRef.html#functions)**
  - **读哪里**：`Function Structure` 与 `Calling Conventions`
  - **和本章的关联**：解释 `define` 与 `declare` 的差别（对应本章 malloc 那一节）

### 源码依据

- **[llvm-project/mlir/lib/Conversion/MemRefToLLVM/](https://github.com/llvm/llvm-project/tree/main/mlir/lib/Conversion/MemRefToLLVM)**
  - **读哪里**：`MemRefDescriptor` 的定义（7 个字段就在这里）
  - **为什么值得读**：想确认「为什么是 7 个而不是 6 个」时，源码是唯一权威

### 论文

- **[MLIR: A Compiler Infrastructure for the End of Moore's Law](https://arxiv.org/abs/2002.11054)** — Lattner et al., 2020
  - **核心贡献**：多层 IR 与 dialect 并存的架构，解释为什么需要「降级」这个动作
  - **读哪里**：Section 3（Design Principles）里关于 progressive lowering 的部分
  - **和本章的关联**：本章整条降级链的理论基础

### 博客 / 工程文章

- **[MLIR 官方 — Toy Tutorial（Chapter 5–6）](https://mlir.llvm.org/docs/Tutorials/Toy/Ch-5/)**
  - **这系列说什么**：从具名方言一路降到 LLVM，官方手把手教程
  - **读哪里**：Ch-5（Partial Lowering to Lower-Level Dialects）与 Ch-6（Lowering to LLVM）
  - **为什么值得读**：和本章做的是同一件事，但有完整可运行的代码
  - **前提**：Toy 教程基于较新 MLIR，14 上语法要改成 `func`

### 书籍

- **《Engineering a Compiler》** — Cooper & Torczon（第 2 版）
  - **读哪几章**：第 4 章（IR 设计）与第 5 章（SSA）
  - **和本章的关联**：理解「为什么要分多层 IR」的一般原理

拿到了 LLVM IR，下一章看 `llc` 在里面执行了什么——为什么同样一段 IR，`-O0` 和 `-O2` 的产物差那么大。

→ [Ch 13 LLVM 优化管线：pass 顺序为什么重要](./13-llvm-pipeline.md)
