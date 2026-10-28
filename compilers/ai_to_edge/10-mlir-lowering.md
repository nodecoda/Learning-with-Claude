# Ch 10 — MLIR：把「算子」变成「循环」

> **目标**：看清一个高层算子（`linalg.matmul`）降级成三层 `scf.for`、再降级成 LLVM 函数调用的完整路径；能解释为什么编译器要分成多层 dialect、每层各保留什么信息；能手写一段 MLIR 14 的 IR 并跑通 `mlir-opt`，也能在版本不匹配时报出正确的错。

> **环境**：MLIR 14.0.0（Ubuntu）、`/usr/lib/llvm-14/bin/mlir-opt`、`mlir-translate`、`llc`，x86-64 主机。

## 为什么需要这个？

在 Ch 6 里，Inductor 生成的 C++ 里已经有循环了：

```cpp
for (int64_t x0 = 0; x0 < 8192; x0 += 8) { ... }
```

那为什么还需要 MLIR？

因为 Inductor 的循环是**为 x86 手写出来**的。换一个目标（AArch64、Cortex-M、RISC-V、带向量单元的加速器），循环形状、向量宽度、内存布局全都要改。如果「算子 → 循环」这一步是硬编码的 C++ 模板，每加一个后端就要重写一遍代码生成器。

MLIR（Multi-Level Intermediate Representation，多级中间表示）的思路相反：

> **不要把「算子」直接翻译成「循环」。先翻译成「一个可以被继续翻译的中间物」，然后让每一层只关心一件事。**

这就是「多级 IR」。代价是要维护多套语法和一堆 pass；回报是同一份 `linalg.matmul` 既能降到 CPU 循环，也能降到 GPU 的 `gpu.launch`，还能降到向量指令——**源 IR 一个字都不用改**。

本章只做一件事：把这条路走一遍，看清每一层剩下什么、丢了什么。

## 先建立直觉

把降级想成**把一份建筑图纸翻译成施工步骤**。

```
建筑师的语言            施工队的语言              工人的语言
┌──────────────┐      ┌──────────────┐        ┌──────────────┐
│ linalg       │      │ scf          │        │ llvm         │
│ 「4x4 矩阵乘」 │ ──►  │ 「i 从 0 到 4」 │  ──►   │ 「load、fmul、│
│              │      │ 「j 从 0 到 4」 │        │  fadd、store」│
│ 知道这是矩阵乘 │      │ 只知道循环嵌套  │        │ 只知道标量指令 │
└──────────────┘      └──────────────┘        └──────────────┘
   语义最丰富              语义被削掉              语义全没了
   优化空间最大            只剩结构                只剩指令
```

三层各有各的用处：`linalg` 层知道「这是矩阵乘」，能做**算子级**优化（融合、tiling、选算法）；`scf`（Structured Control Flow，结构化控制流）层知道「这是三层循环」，能做**循环级**优化（交换、展开、分块）；`llvm` 层只知道「寄存器、指针、加乘」，能做**指令级**优化（指令选择、寄存器分配）。

**每降一层，丢一些信息，换一些确定性。**这就是多级 IR 的核心权衡；后面所有现象都能用它解释。

## 底层机制：一次降级到底发生了什么

从 `linalg.matmul` 到 LLVM 函数，要跨四道方言（dialect）边界：

```
linalg.matmul
   │  ① --convert-linalg-to-loops
   │     用「下标映射」（indexing map）展开成循环
   ▼
scf.for × 3 + memref.load/store + arith.mulf/addf
   │  ② --convert-scf-to-std  --convert-std-to-llvm
   │     scf 的结构化循环 → 基本块 + 条件跳转
   ▼
llvm.br / llvm.cond_br / llvm.load / llvm.fmul
   │  ③ --convert-memref-to-llvm
   │     带形状的 memref → 扁平指针 + 描述符（descriptor）
   ▼
llvm.func + llvm.getelementptr
   │  ④ --reconcile-unrealized-casts
   │     清掉中间产物里的「临时类型转换」
   ▼
合法的 LLVM dialect
```

### 第①步凭什么能展开

`linalg.matmul` 这个 op 自带**索引映射**。用 `--mlir-print-op-generic` 看它的内部结构：

```bash
/usr/lib/llvm-14/bin/mlir-opt matmul.mlir --mlir-print-op-generic
```

实测（节选）：

```
#map0 = affine_map<(d0, d1, d2) -> (d0, d2)>
#map1 = affine_map<(d0, d1, d2) -> (d2, d1)>
#map2 = affine_map<(d0, d1, d2) -> (d0, d1)>

"linalg.matmul"(%arg0, %arg1, %arg2) ({
^bb0(%arg3: f32, %arg4: f32, %arg5: f32):
  %0 = "arith.mulf"(%arg3, %arg4) : (f32, f32) -> f32
  %1 = "arith.addf"(%arg5, %0) : (f32, f32) -> f32
  "linalg.yield"(%1) : (f32) -> ()
}) {linalg.memoized_indexing_maps = [#map0, #map1, #map2], ...}
```

这三行 `affine_map` 是全部秘密：

| map | 含义 | 对应 `C[i,j] += A[i,k] * B[k,j]` 的哪一项 |
|---|---|---|
| `(d0,d1,d2) -> (d0,d2)` | 输出下标 `(i,j)`、归约下标 `k` → `A[i,k]` | A 的寻址 |
| `(d0,d1,d2) -> (d2,d1)` | `(i,j,k)` → `B[k,j]` | B 的寻址 |
| `(d0,d1,d2) -> (d0,d1)` | `(i,j,k)` → `C[i,j]` | 输出位置 |

`linalg.matmul` 只声明了「怎么用下标找到元素」和「每个元素做什么运算」（`mulf` + `addf` + `yield`），**没有声明循环怎么写**。循环是第①步根据这三个 map 推出来的。

> **这就是「语义」的价值**：因为有 map，编译器可以自己决定循环顺序、要不要交换 `i`/`j`、要不要分块。如果源 IR 直接写成三层 `for`，这些信息就没了，只能靠分析循环体反推。

## 例子一：matmul 从算子到三层循环（实测）

源文件 `code/02_mlir_lowering/matmul.mlir`。**注意 `func` 这个词**——MLIR 14 的写法：

```mlir
module {
  func @matmul(%A: memref<4x8xf32>, %B: memref<8x4xf32>, %C: memref<4x4xf32>) {
    linalg.matmul ins(%A, %B : memref<4x8xf32>, memref<8x4xf32>)
                  outs(%C : memref<4x4xf32>)
    return
  }
}
```

跑第一条降级：

```bash
/usr/lib/llvm-14/bin/mlir-opt matmul.mlir --convert-linalg-to-loops
```

实测输出（完整）：

```mlir
module {
  func @matmul(%arg0: memref<4x8xf32>, %arg1: memref<8x4xf32>, %arg2: memref<4x4xf32>) {
    %c4 = arith.constant 4 : index
    %c8 = arith.constant 8 : index
    %c0 = arith.constant 0 : index
    %c1 = arith.constant 1 : index
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
    return
  }
}
```

### 读这段输出要注意的三件事

**第一，`linalg.matmul` 不见了。**整个文件里没有 `matmul` 这个词，只剩三个 `scf.for`、三个 `memref.load`、一个 `memref.store`，以及 `arith.mulf` / `arith.addf` 各一条。`grep -c` 实测：`scf.for` 出现 3 次，`memref.load|memref.store|arith.mulf|arith.addf` 合计 6 行。

**第二，循环顺序是 `i`（外层）→ `j` → `k`（最内层）。**归约维 `k` 放最内层，`C[i,j]` 在寄存器里累积，内层每轮只做一次 load、一次 store。这是**朴素但正确**的顺序；想更快就要换顺序 + 分块（Ch 8 的 tiling 在这里重新出现）。

**第三，`%2 = memref.load %arg2[...]` 每次都被读出来。**它读的是 `C[i,j]` 的旧值。这是 `C += A*B` 的累加语义，不是编译器疏忽。要改成 `C = A*B` 就得先清零——量化与算子融合时经常要处理这个细节。

### 边界：`--lower-affine` 在这份 IR 上不起作用

完整管线里有一条 `--lower-affine`。在这份源 IR 上它**不产生可见变化**——因为 `matmul.mlir` 里没有任何 `affine.apply` / `affine.load` 之类的表达式，索引映射已经被第①步消耗掉了。

它出现的原因：从 `linalg` 的 `affine` 变体、或自己写的 `affine.for` 开始降级时，会留下 `affine` 表达式，必须由这条 pass 清掉。**理论如此，本例未触发**（实测：加与不加这条 pass，输出逐字节相同）。保留它是为了让管线能吃更广的输入。

## 例子二：走完四步，签名从 3 个参数变成 21 个（实测）

完整命令（`code/02_mlir_lowering/build.sh` 的第 1 步）：

```bash
mlir-opt matmul.mlir --convert-linalg-to-loops --lower-affine \
  --convert-scf-to-std --convert-std-to-llvm --convert-arith-to-llvm \
  --convert-memref-to-llvm --reconcile-unrealized-casts
```

实测输出的 `llvm.func` 签名：

```mlir
llvm.func @matmul(%arg0: !llvm.ptr<f32>, %arg1: !llvm.ptr<f32>,
                  %arg2: i64, %arg3: i64, %arg4: i64, %arg5: i64, %arg6: i64,
                  %arg7: !llvm.ptr<f32>, %arg8: !llvm.ptr<f32>,
                  %arg9: i64, %arg10: i64, %arg11: i64, %arg12: i64, %arg13: i64,
                  %arg14: !llvm.ptr<f32>, %arg15: !llvm.ptr<f32>,
                  %arg16: i64, %arg17: i64, %arg18: i64, %arg19: i64, %arg20: i64)
```

三个 `memref<...xf32>` 变成 **21 个标量参数**，每个 memref 摊成 7 个：

```
memref<4x8xf32>  →  {  ptr, ptr, i64, i64, i64, i64, i64  }
                       │    │    │    │    │    │    └─ stride[1]
                       │    │    │    │    │    └────── stride[0]
                       │    │    │    │    └─────────── size[1]
                       │    │    │    └──────────────── size[0]
                       │    │    └───────────────────── offset
                       │    └────────────────────────── aligned ptr
                       └─────────────────────────────── allocated ptr
```

**7 = 2（两个指针）+ 1（offset）+ 2（rank=2 的 size）+ 2（rank=2 的 stride）**。rank 变成 3 就是 2+1+3+3 = 9 个。这条公式在 Ch 21 会被重新拿出来，因为**它直接决定了你能不能从 C 调用 MLIR 生成的函数**。

两个指针为什么都要？`allocated ptr` 是 `malloc` 返回的原始地址，`aligned ptr` 是实际数据起点（可能因对齐而偏移）。释放内存用前者，访问数据用后者。

### 失败分支：不加 `--reconcile-unrealized-casts`

把最后一个 pass 去掉，再数一次：

```bash
mlir-opt matmul.mlir --convert-linalg-to-loops --lower-affine \
  --convert-scf-to-std --convert-std-to-llvm --convert-arith-to-llvm \
  --convert-memref-to-llvm
```

实测残留 **6 个** `builtin.unrealized_conversion_cast`；加上最后一条 pass 后是 **0 个**。

这 6 个不是「错误」，是降级过程中的**临时胶水**：每一步只换掉自己负责的类型，不匹配的地方先插占位，等所有 pass 跑完再统一清账。它的语义是「我知道这里类型不一致，但后面会有人处理」。

清账的时机很关键。直接喂给 `mlir-translate`：

```bash
/usr/lib/llvm-14/bin/mlir-translate --mlir-to-llvmir m3.mlir
```

实测报错（原文）：

```
m3.mlir:11:10: error: cannot be converted to LLVM IR: missing `LLVMTranslationDialectInterface` registration for dialect for op: builtin.unrealized_conversion_cast
    %8 = builtin.unrealized_conversion_cast %7 : !llvm.struct<(ptr<f32>, ptr<f32>, i64, array<2 x i64>, array<2 x i64>)> to memref<4x8xf32>
```

报错说的是「dialect 没有注册翻译接口」——它在告诉你：**这个 op 不该留到最后**。这不是 bug，是流程漏了一步。

### 第二个意外：源 IR 没有堆分配，输出却有 `malloc`

```bash
/usr/lib/llvm-14/bin/mlir-translate --mlir-to-llvmir matmul.llvm.mlir > matmul.ll
head -8 matmul.ll
```

实测：

```llvm
; ModuleID = 'LLVMDialectModule'
source_filename = "LLVMDialectModule"

declare i8* @malloc(i64)

declare void @free(i8*)

define void @matmul(float* %0, float* %1, ...)
```

`matmul.mlir` 里一个 `memref.alloc` 都没有，输出里却有 `malloc` / `free` 的**声明**。

这两行是 `--convert-memref-to-llvm` 顺带产生的：memref 方言里存在会分配内存的 op（`memref.alloc`、`memref.alloca` 的堆变体），翻译时把运行时支持函数的声明一并附上。**声明不等于调用**——本文件的 `matmul` 函数体里没有任何 `call @malloc`。

但这一条对边缘部署仍然重要：**你以为没有分配，其实别的 pass 偷偷插了分配**。检查方法：

```bash
grep -c 'call.*@malloc' matmul.ll
```

实测结果：`0`。声明在，调用不在。**「有没有声明」和「有没有调用」必须分开看。**

## 例子三：版本的坑——`func.func` 在 MLIR 14 上是错的

现在把源 IR 改成你在 MLIR 15+ 教程里看到的样子：

```mlir
module {
  func.func @f(%a: i32) -> i32 {
    return %a : i32
  }
}
```

```bash
/usr/lib/llvm-14/bin/mlir-opt f14.mlir
```

实测报错：

```
f14.mlir:2:3: error: custom op 'func.func' is unknown
  func.func @f(%a: i32) -> i32 {
  ^
```

换 `std.func` 也不行：

```
s14.mlir:2:3: error: custom op 'std.func' is unknown
  std.func @g(%a: i32) -> i32 {
  ^
```

正确写法只有 `func`。用 generic 形式看它到底是什么：

```bash
/usr/lib/llvm-14/bin/mlir-opt matmul.mlir --mlir-print-op-generic | head -10
```

实测（节选）：

```
"builtin.module"() ({
  "builtin.func"() ({
```

所以：

| 你写的 | 实际是 | MLIR 14 | MLIR 15+ |
|---|---|---|---|
| `func` | `builtin.func` 的语法糖 | 可用 | 已移除/改语义 |
| `builtin.func` | 完整名 | 可用 | 不同 |
| `func.func` | Func dialect 的 op | **报 unknown** | 可用 |
| `std.func` | 旧 Standard dialect | **报 unknown** | 不存在 |

`std` 这个 dialect 在 MLIR 14 里还存在（`Available Dialects` 里有 `std`），但 `std.func` 这个具体 op 不在里面——`func` 从来属于 `builtin`。这就是为什么这两个都报错，而 `func` 能过。

### 第一查证手段：`--help` 里的 `Available Dialects`

任何「这个 op 不存在」的怀疑，第一步都是看这个版本装了哪些 dialect：

```bash
/usr/lib/llvm-14/bin/mlir-opt --help | sed -n '/Available Dialects/,/^$/p'
```

实测（原文）：

```
Available Dialects: acc, affine, amx, arith, arm_neon, arm_sve, async, bufferization, builtin, complex, dlti, emitc, gpu, linalg, llvm, math, memref, nvvm, omp, pdl, pdl_interp, quant, rocdl, scf, shape, sparse_tensor, spv, std, tensor, test, tosa, vector, x86vector
```

这里能直接读出三件事：

1. **有 `linalg`、`memref`、`scf`、`arith`、`llvm`**——本章的四个层级都在。
2. **有 `quant`**——Ch 11 会用到，MLIR 自己有量化 dialect。
3. **没有 `func`**。不是漏了，是 `func` 属于 `builtin`。看列表里 `builtin` 在第四位。

同时数一下 `convert-` 前缀的 pass：

```bash
/usr/lib/llvm-14/bin/mlir-opt --help | grep -c 'convert-'
```

实测：**45**。这 45 条 pass 就是「方言之间怎么互相翻译」的全部官方答案。遇到「A 方言怎么降到 B 方言」，先在这 45 条里搜 `--convert-a-to-b`，而不是上网找教程——教程的版本很可能和你装的不一样。

### 版本坑的通用形态

```
教程写 func.func            → 你的 14 报 unknown
教程写 --convert-scf-to-std → 15+ 已改成 --convert-scf-to-cf
教程写 memref.subview 的某些属性 → 属性名可能不同
```

**判断方法不是记版本表，是每次先跑 `--version` 和 `--help`。**这比背下来可靠。

## 「语义丢失」既是代价，也是机会

回到开头那句话：每降一层，丢一些信息，换一些确定性。

### 代价

降到 `scf.for` 之后，编译器**再也看不出这是矩阵乘**。它看到的是三个嵌套循环、六个内存访问。后果：

- 想换成 Strassen 算法？做不到，因为「矩阵乘」这个概念已经不存在了。
- 想识别「这是 GEMM，应该调 BLAS」？只能靠模式匹配去认循环形状，容易认错。
- 想融合后面的 `bias + relu`？可以在循环层做，但要在循环体里加语句，而不是在算子层声明。

### 机会

反过来看，正因为语义被削掉，**后面每一层都能自由改造**：

| 层级 | 能做的改造 | 因为丢了什么才成为可能 |
|---|---|---|
| `linalg` | tiling、算子融合、算法替换 | ——（这层语义最全） |
| `scf` | 循环交换、展开、分块、向量化 | 丢了「这是矩阵乘」，所以可以任意重排下标 |
| `llvm` | 指令选择、寄存器分配、窥孔优化 | 丢了「这是循环」，所以可以展开成直线代码 |

层与层之间的契约（dialect 的 op 语义）是稳定的，层内的实现可以随便换。这就是 MLIR 相对「一个大 IR 走到底」的核心优势：不是更快，是**可替换**。换后端只涉及最后一两级 dialect 的翻译规则。

### 一个具体例子：`memref` 不是指针

很多人看到 `memref<4x8xf32>` 就当成 `float*`。上面的 21 参签名已经说明了它不是——它带着 offset、size、stride。

这个区别在什么时候咬人：如果 C 侧按 `void matmul(float* a, float* b, float* c)` 声明，而实际符号要求 21 个参数，调用后第 4 个参数开始全部错位——不会报错，只会算出垃圾值。

`memref` 是**带形状的视图**，可以表示非连续内存（转置过的矩阵）、可以带偏移（切片）。代价是每次调用都要传这 7 个数。在 MCU 上，7 个参数里 5 个 `i64` = 40 字节的栈/寄存器开销——**一次调用比函数体本身还大**。这是 Ch 21 和 Ch 22 的伏笔。

## 踩雷集锦

1. **抄 MLIR 15+ 的教程跑 MLIR 14**：`func.func` 直接报 `custom op 'func.func' is unknown`。同理 `std.func` 也报错。正确写法是 `func`（`builtin.func` 的语法糖）。**先在 `--help` 的 `Available Dialects` 里确认这个版本有什么**，再抄。
2. **忘 `--reconcile-unrealized-casts`**：会残留 6 个 `builtin.unrealized_conversion_cast`，然后 `mlir-translate` 报「missing LLVMTranslationDialectInterface registration」。这个报错看起来像「MLIR 不支持这个 op」，实际是「你的管线少了一步」。
3. **以为 `memref` 是指针**：一个 `memref<4x8xf32>` 降级后是 7 个参数（2 指针 + 1 offset + 2 size + 2 stride）。从 C 调用时按 `float*` 写签名，栈上参数会全部错位。
4. **以为 `declare @malloc` 就是用了 `malloc`**：`mlir-translate` 会无条件附上 `malloc`/`free` 的声明。判断有没有真的分配，要 `grep 'call.*@malloc'`，不是看文件头。
5. **以为降级顺序可以随便调**：`--convert-memref-to-llvm` 要吃的是 std/arith 形式的内存访问，所以它必须排在 `--convert-std-to-llvm` 之后。顺序错了不会报明确错误，只会看到「某个 op 没被转换」的残留。

## 本章重点整理

- 多级 IR 的核心权衡：**每降一层丢一些语义，换一些可改造的自由度**。
- 四个层级的分工：`linalg`（有语义算子）→ `scf`（结构化循环）→ `arith`/`memref`（标量/内存）→ `llvm`（贴后端）。
- `linalg.matmul` 能展开成循环，靠的是三个 `affine_map`；循环顺序由编译器推出来，不是源 IR 写的。
- 一次完整降级把 3 个 memref 参数变成 **21 个标量参数**（每个 7 个）。
- 不加 `--reconcile-unrealized-casts` 会残留 6 个 `unrealized_conversion_cast`，`mlir-translate` 随即报错。
- MLIR 14 里函数写 `func`，不是 `func.func`；第一查证手段是 `mlir-opt --help` 的 `Available Dialects`（45 个 `convert-` pass）。

## 自我检核

- [ ] 不看表，说出 `linalg` / `scf` / `arith`+`memref` / `llvm` 四层各自「知道什么、不知道什么」
- [ ] 能解释为什么 `linalg.matmul` 可以展开成循环，而一段手写的三层循环不能反推成 `linalg.matmul`
- [ ] 给你一个 rank-3 的 `memref<2x3x4xf32>`，算出它在 LLVM dialect 里摊成几个参数（提示：2+1+3+3）
- [ ] 能说出 `--reconcile-unrealized-casts` 在做什么，以及漏掉它的**具体报错原文**
- [ ] 能解释「源 IR 没有 alloc，输出却有 `declare @malloc`」这件事，并说出怎么确认有没有真的调用
- [ ] 遇到 `custom op 'X' is unknown`，你的第一步是什么

## 延伸阅读

### 官方文档

- **[MLIR Language Reference](https://mlir.llvm.org/docs/LangRef/)**
  - **读哪里**：`Builtin Dialect` 一节里的 `func` 与 `module`，以及 `Dialects` 总览页
  - **学什么**：`builtin.func` 的完整语义、为什么函数属于 builtin 而不是独立 dialect
  - **和本章的关联**：本章 `func.func` 报错的根源就在这里
  - **前提**：读完本章的例子再看，否则全是没见过的语法

- **[MLIR Passes 文档](https://mlir.llvm.org/docs/Passes/)**
  - **读哪里**：`Conversion` 分类下的 `convert-linalg-to-loops`、`convert-memref-to-llvm`、`reconcile-unrealized-casts`
  - **学什么**：每条 pass 的输入/输出契约，以及 `unrealized_conversion_cast` 的设计意图
  - **和本章的关联**：本章 45 条 `convert-` pass 的官方说明
  - **注意**：文档跟着 main 分支走，版本可能比你装的 14 新；以 `--help` 为准

- **[MLIR Dialect Conversion 基础设施](https://mlir.llvm.org/docs/DialectConversion/)**
  - **读哪里**：「Type Conversion」与「Unrealized Conversion Casts」两节
  - **学什么**：「部分转换 + 占位 cast + 最后清账」这套机制为什么必要
  - **和本章的关联**：那 6 个 `unrealized_conversion_cast` 的设计出处
  - **前提**：知道 SSA 与基本块即可

### 论文

- **[MLIR: A Compiler Infrastructure for the End of Moore's Law](https://arxiv.org/abs/2002.11054)** — Lattner et al., 2020
  - **核心贡献**：提出「多级 IR + dialect 可扩展」的编译器基础设施，论证为什么单一 IR 无法同时服务多个硬件域
  - **读哪里**：Section 2（Dialects）与 Section 4（Progressive Lowering，渐进降级）
  - **和本章的关联**：本章「每层丢语义换自由度」的理论出处；Section 4 就是本章那条四步管线的抽象版本
  - **前提**：读过本章的例子，否则 Section 4 的图会很抽象

### 源码与工具

- **[llvm-project/mlir/test/Conversion](https://github.com/llvm/llvm-project/tree/release/14.x/mlir/test/Conversion)**
  - **读哪里**：`LinalgToLoops/`、`MemRefToLLVM/` 两个目录下的 `.mlir` 测试文件
  - **学什么**：每条 pass 的**最小输入输出对**——比文档更精确，因为它们是 CI 跑过的
  - **和本章的关联**：本章 matmul 例子的同族用例，能看到 `memref` 描述符展开的更多变体
  - **前提**：会读 FileCheck 的 `// CHECK:` 注释（占位匹配，扫一眼就能懂）

到这里，「算子 → 循环 → LLVM 函数」的路已经走通了一遍。但真实模型里还有一个更前置的问题：**循环里算的是 `f32`，而边缘设备可能根本没有浮点单元**。下一章看编译器怎么把这件事解决掉。

→ [Ch 11 量化在编译器中的位置](./11-quantization.md)
