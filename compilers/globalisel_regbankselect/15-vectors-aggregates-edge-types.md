# Ch 15 — 向量、聚合与边角类型

> **目标**：把“一个值一定住在一个寄存器里”的直觉拆掉。学完你能解释：为什么 128 位向量在 AArch64 上是 `fpr`，而 512 位向量要先被拆开；为什么 `i128` 有时是 FPR、有时是两条 `gpr(i64)` 指令；`s1` 为什么不进 CC bank；`v3s32` 为什么在 MIR 里变成 4 个 lane；以及 `<vscale x 4 x i32>` 与 RISC-V `vrb` 的映射依据。
>
> **环境**：LLVM 23.1.4，Windows x86_64 官方预编译包（实测 MIR 与汇编）；AArch64 主 target，RISC-V 对照。`vec.ll`、`wide.ll`、`i128bc.ll`、`agg.ll`、`sve2.ll`、`rv.ll` 都在官方包上跑过。
>
> **证据约定**：标“实测”的是官方 23.1.4 输出；标“源码依据”的是 `AArch64RegisterBanks.td`、`AArch64RegisterBankInfo.cpp`、`AArch64GenRegisterBankInfo.def`、`RISCVRegisterBanks.td`、`RISCVRegisterBankInfo.cpp` 的结论；SVE predicate 的完整 lowering 标 guided，因为 23.1.4 在 legalizer 就崩了，没有 RegBankSelect 输出可看。

## 为什么需要这个？

Part 2 的前四章都在处理“一个标量值住进一个 bank”。真实代码里还有一整类值不符合这个前提：

- **向量**：128 位、256 位、512 位，甚至长度在编译期未知的 scalable vector。
- **大整数**：`i128` 没有一条 AArch64 整数指令能一次算完。
- **布尔**：`i1` 在 IR 里是 1 位，在机器上却没有 1 位通用寄存器。
- **非 2 的幂向量**：`<3 x i32>` 在 NEON 上没有原生 lane 数。
- **聚合**：`{i64, double}` 根本不是一个 LLT，MIR 里没有“struct 寄存器”。

这些类型最容易让人产生三种误判：看到向量就断言 `fpr`，忽略 RISC-V 的 `vrb` 和 SVE predicate；看到 `i128` 就断言“两个 GPR”，忽略 legalizer 可能把它拆成两条 `i64` 加法，也可能让一个 `i128` 值默认落 FPR；看到 `<3 x i32>` 就按 3 个 lane 推理，忽略它已经被 padding 成 `<4 x i32>`。

关键点是：**RegBankSelect 不看源语言类型，也不看“逻辑上有几个字段”；它只看 legalizer 留下的 LLT 和 G_* 指令。** 本章讨论的“边角”，本质上都是 legalizer 先做了什么，以及 bank 的容量/覆盖范围能否接住剩下的 LLT。

## 先建立直觉

把 bank 想成仓库，把类型想成货物：

- **GPR 仓库**：货架格口最大 128 位（AArch64 通过 `XSeqPairsClass`）。
- **FPR 仓库**：货架格口最大 512 位（通过 `QQQQ` 与 `ZPR`），但真正干活的 NEON 指令一次只处理 128 位。
- **RISC-V VRB 仓库**：按 64/128/256/512 位分格，向量只能进这里。
- **聚合**：不是一件货，而是几张独立的运单，早在入库前就被拆开了。

```text
                    legalizer 之后的 LLT
   <4 x i32>      i128        <vscale x 4 x i32>     <8 x i64>      {i64, double}
       │            │                  │                  │               │
       ▼            ▼                  ▼                  ▼               ▼
    fpr(Q)     fpr(Q) 或 2×gpr(i64)   fpr(Z)          4 × fpr(Q)     拆成独立 vreg
```

同样是 128 位，`<4 x i32>` 和 `i128` 可以都落 FPR，但它们的指令选择完全不同；同样是 512 位，AArch64 的 FPR 能“覆盖”它，不代表一条 NEON 指令能一次算完。仓库能放下，不等于叉车能一次搬走。

## 核心概念

### 1. bank 有容量上限，LLT 有实际位宽

AArch64 的 bank 定义很短（`AArch64RegisterBanks.td`）：

```tablegen
def GPRRegBank : RegisterBank<"GPR", [XSeqPairsClass]>;
def FPRRegBank : RegisterBank<"FPR", [QQQQ, ZPR]>;
def CCRegBank  : RegisterBank<"CC", [CCR]>;
```

构造函数用断言把上限写死（源码依据）：

```cpp
assert(getMaximumSize(RBGPR.getID()) == 128 &&
       "GPRs should hold up to 128-bit");
assert(getMaximumSize(RBFPR.getID()) == 512 &&
       "FPRs should hold up to 512-bit via QQQQ sequence");
```

- 128 位是 GPR 的上限，所以 `i128` 不可能“自然地”塞进一个通用寄存器。
- 512 位是 FPR 的上限，所以 `<8 x i64>` 在 bank 层面“放得下”；但 NEON 指令只处理 128 位，legalizer 仍会把它拆成 4 个 `<2 x i64>`。
- RISC-V 的 `VRB` 覆盖 `VRM8`，`PartMappings` 分成 64/128/256/512 四档；`getVRBValueMapping` 按 known-min 选档，超出 512 直接 `report_fatal_error`。

### 2. 默认判定：向量去 FPR，大标量也去 FPR

AArch64 `getInstrMapping` 的默认循环里有一段“顶层猜测”（源码依据）：

```cpp
if (Ty.isVector())
  OpRegBankIdx[Idx] = PMI_FirstFPR;
else if (isPreISelGenericFloatingPointOpcode(Opc) ||
         (MO.isDef() && onlyDefinesFP(MI, MRI, TRI)) ||
         (MO.isUse() && onlyUsesFP(MI, MRI, TRI)) ||
         Ty.getSizeInBits() > 64)
  OpRegBankIdx[Idx] = PMI_FirstFPR;
else
  OpRegBankIdx[Idx] = PMI_FirstGPR;
```

逐条读：任何向量（固定或 scalable）默认 `FPR`；标量只要大于 64 位也默认 `FPR`，这就是“纯值”的 `i128` 会落 FPR 的原因；小于等于 64 位的标量默认 `GPR`，除非它是浮点指令的操作数，或定义/使用只涉及浮点。

这只是默认映射。target 可以在后面的 `switch (Opc)` 里改写单个 operand 的 bank；legalizer 也可以更早把 `i128` 加法拆掉，让 RegBankSelect 根本看不到 `i128` 算术。

### 3. 固定长度向量：宽度决定是否拆分，lane 数可能被 padding

AArch64 上 128 位是 NEON 的自然宽度：`<4 x i32>`、`<2 x double>`、`<8 x i16>` 都落 `fpr`。超过 128 位的固定向量会被 legalizer 拆成多个 128 位片段：

- `<8 x i64>`（512 位）→ 4 个 `<2 x i64>`，4 条 `G_ADD`。
- `<16 x i32>`（512 位）→ 4 个 `<4 x i32>`，4 条 `G_ADD`。

这不是 RegBankSelect 拆的。等它看到指令时，已经是 4 条独立的 128 位 `G_ADD`，每条按普通向量规则落 `fpr`。

`<3 x i32>` 在 AArch64 上没有 3-lane NEON 指令，legalizer 会把它扩展成 `<4 x i32>`，第 4 个 lane 用未定义值填充。实测 MIR 里看到的类型是：

```text
%26:fpr(<4 x i32>) = G_ADD %2, %7
```

所以在 RegBankSelect 阶段不会看到 `<3 x i32>`。拿源 IR 的 lane 数去解释 bank 或 COPY，一定会错。

### 4. scalable vector：按“最小位宽”映射，但寄存器是 Z

`<vscale x 4 x i32>` 的 LLT 带 scalable 标记，编译期只知道最小长度是 4 个 i32（128 位）。AArch64 的 FPR bank 覆盖 `ZPR`，所以它落 `fpr`；RISC-V 则落 `vrb`：

```text
AArch64:  %2:fpr(<vscale x 4 x i32>) = G_ADD %0, %1
RISC-V:   %2:vrb(<vscale x 4 x s32>) = G_ADD %0, %1
```

两边都用 `TypeSize::getKnownMinValue()` 选 partial mapping：AArch64 走 `getValueMapping(..., Size, FPR)`，RISC-V 走 `getVRBValueMapping(Size.getKnownMinValue())`。运行时 vscale 是多少，不影响 bank 选择；它只影响最终指令和寄存器占用。
> AArch64 侧需要 `-aarch64-enable-gisel-sve=1`；不加这个 flag 时，23.1.4 会在 Legalizer 崩掉，而不是给出没有 bank 的 MIR。RISC-V 侧需要 `-mattr=+v`。

### 5. `i128`：值和算术是两件事

`i128` 在 23.1.4 的 AArch64 上有两种完全不同的形态：

- **作为“值”**：默认映射看到 `size > 64`，落 `fpr(i128)`。`G_TRUNC` 的 s128 特例、`G_MERGE_VALUES` 产生 `i128`、`G_BITCAST` 的源/目的，都属于这一类。
- **作为“算术”**：legalizer 把 `add i128` 拆成 `G_UADDO` + `G_UADDE`，每个操作数都是 `gpr(i64)`。RegBankSelect 看到的已经是 64 位整数链。

所以“`i128` 在哪个 bank”没有唯一答案。先问：这条指令是 legalizer 留下的 `i128` 值操作，还是已经被拆成 64 位算术？

### 6. `s1` 与聚合：CC 不是布尔 bank，struct 不是 LLT

CC bank 只覆盖 `CCR`（NZCV），它是条件码寄存器，不是“所有 1 位值”的默认归宿。实测 `and i1` 的 MIR：

```text
%4:gpr(i32) = COPY $w0
%5:gpr(i32) = COPY $w1
%13:gpr(i32) = G_AND %4, %5
%14:gpr(i32) = G_CONSTANT i32 1
%10:gpr(i32) = G_AND %13, %14
```

`i1` 被 legalizer 扩成 `i32`，布尔值放在 GPR 的低位，最后用 `G_AND 1` 清掉高位。`select i1` 的条件同样是 `gpr(i32)`：`%6:fpr(f32) = G_SELECT %7(i32), %1, %2`。

聚合则根本没有 LLT。`{i64, double}` 会被拆成字段，字段各自成为独立 vreg：`extractvalue %s, 0` → `COPY $x0`，`extractvalue %s, 1` → `COPY $d0`。`insertelement` 链变成 `G_INSERT_VECTOR_ELT`：向量是 FPR，插入的标量可以是 GPR，索引必须是 GPR。

## 底层机制

### legalizer 先决定“还剩什么”

RegBankSelect 的输入是 legalized gMIR。向量的拆分、`i128` 的拆解、`s1` 的扩宽、`v3s32` 的 padding，都发生在它之前：

```text
IR 类型
  │ IRTranslator：生成 G_*，类型还很大
  ▼
Legalizer：widen / split / pad / scalarize
  │ 例如 <8 x i64> -> 4 × <2 x i64>
  │ 例如 add i128 -> G_UADDO + G_UADDE
  ▼
RegBankSelect：只给剩下的 LLT 选 bank
```

调试“向量 bank 不对”时，第一步不是看 `AArch64RegisterBankInfo`，而是先 `-stop-after=legalizer`，确认 legalizer 把类型变成了什么。

### mapping 生成：容量检查与 value mapping

`RegisterBankInfo` 用一个 `ValueMapping` 描述“这个值如何分布到 bank 的哪些位上”。大多数 AArch64 指令是 `NumBreakDowns == 1`：整个值落在一个 partial mapping 里。`PartialMapping::verify` 会检查 bank 的最大容量（源码依据）：

```cpp
assert(RBI.getMaximumSize(RegBank->getID()) >= Length &&
       "Register bank too small for Mask");
```

如果 legalizer 留下一个 1024 位向量，而 target 没有对应 partial mapping，RegBankSelect 就拿不到合法 mapping，最终失败或 fallback。“bank 能覆盖”与“指令能选择”必须分开看。

### 多寄存器值的 special case

AArch64 对几个和“拆/合”有关的 opcode 有专门处理（源码依据）：

- `G_UNMERGE_VALUES`：源是向量、`s128`，或任一 use 只涉及 FP 时，所有 operand 都设成 FPR。
- `G_BUILD_VECTOR`：第一个源已在 FPR，或定义是浮点 op，或元素小于 32 位时，整个 build_vector 保持 FPR；全常量例外，留给 pattern 匹配。
- `G_EXTRACT_VECTOR_ELT`：目的和源必须是 FPR，索引必须是 GPR。
- `G_INSERT_VECTOR_ELT`：目的和源是 FPR，元素可以是 GPR 或 FPR，索引是 GPR。

这些规则解释了为什么 `i128` 的 merge/bitcast 链整条留在 FPR，而 `i128` 的加法却在 GPR。

## 例子一：固定向量、padding 与 load（实测）

```llvm
define <4 x i32> @v4i32_add(<4 x i32> %a, <4 x i32> %b) { %r = add <4 x i32> %a, %b; ret <4 x i32> %r }
define <2 x double> @v2f64_add(<2 x double> %a, <2 x double> %b) { %r = fadd <2 x double> %a, %b; ret <2 x double> %r }
define <8 x i16> @v8i16_add(<8 x i16> %a, <8 x i16> %b) { %r = add <8 x i16> %a, %b; ret <8 x i16> %r }
define <3 x i32> @v3i32_add(<3 x i32> %a, <3 x i32> %b) { %r = add <3 x i32> %a, %b; ret <3 x i32> %r }
```

`-stop-after=regbankselect` 的关键 MIR：

```text
name: v4i32_add
  %0:fpr(<4 x i32>) = COPY $q0
  %1:fpr(<4 x i32>) = COPY $q1
  %2:fpr(<4 x i32>) = G_ADD %0, %1
  $q0 = COPY %2(<4 x i32>)

name: v3i32_add
  %2:fpr(<4 x i32>) = COPY $q0
  %7:fpr(<4 x i32>) = COPY $q1
  %26:fpr(<4 x i32>) = G_ADD %2, %7
```

要点：

- 这些 128 位向量都在 `fpr`，但 LLT 保留各自的元素类型（`<4 x i32>`、`<2 x i64>`、`<8 x i16>`）。
- 在完整输出里，`v2f64_add` 的 `COPY` 类型是 `<2 x i64>`，`G_FADD` 的 def 才是 `<2 x f64>`；这是 MIR 里常见的“同宽不同类型”。
- `v3i32_add` 已经被 padding 成 `<4 x i32>`，MIR 里没有任何 3-lane 痕迹。

再看 load：

```llvm
define <4 x i32> @v4i32_load(ptr %p) {
  %r = load <4 x i32>, ptr %p, align 16
  ret <4 x i32> %r
}
```

```text
%0:gpr(p0) = COPY $x0
%1:fpr(<4 x i32>) = G_LOAD %0(p0) :: (load (<4 x i32>) from %ir.p)
$q0 = COPY %1(<4 x i32>)
```

地址在 `gpr(p0)`，数据在 `fpr(<4 x i32>)`。这正是 Ch 13 的规则在向量上的延伸：地址 bank 与数据 bank 是两条独立的决策。

## 例子二：`i128`、128 位 bitcast 与聚合（实测）

### `i128` 加法：legalizer 先拆

```llvm
define i128 @i128_add(i128 %a, i128 %b) { %r = add i128 %a, %b; ret i128 %r }
```

```text
%2:gpr(i64) = COPY $x0
%3:gpr(i64) = COPY $x1
%4:gpr(i64) = COPY $x2
%5:gpr(i64) = COPY $x3
%13:gpr(i64), %19:gpr(i32) = G_UADDO %2, %4
%15:gpr(i64), %18:gpr(i32) = G_UADDE %3, %5, %19
$x0 = COPY %13(i64)
$x1 = COPY %15(i64)
```

RegBankSelect 看到的已经是两条 64 位整数指令，全部 `gpr`。它没有“把 i128 放哪个 bank”的问题，因为 legalizer 已经把这个问题消掉了。

### `i128` 值：merge / bitcast / unmerge 留在 FPR

```llvm
define <2 x i64> @i128_to_v2i64(i128 %x) { %r = bitcast i128 %x to <2 x i64>; ret <2 x i64> %r }
define i128 @v2i64_to_i128(<2 x i64> %x) { %r = bitcast <2 x i64> %x to i128; ret i128 %r }
```

```text
name: i128_to_v2i64
  %1:gpr(i64) = COPY $x0
  %2:gpr(i64) = COPY $x1
  %0:fpr(i128) = G_MERGE_VALUES %1(i64), %2(i64)
  %3:fpr(<2 x i64>) = G_BITCAST %0(i128)
  $q0 = COPY %3(<2 x i64>)

name: v2i64_to_i128
  %0:fpr(<2 x i64>) = COPY $q0
  %1:fpr(i128) = G_BITCAST %0(<2 x i64>)
  %2:fpr(i64), %3:fpr(i64) = G_UNMERGE_VALUES %1(i128)
  $x0 = COPY %2(i64)
  $x1 = COPY %3(i64)
```

`G_MERGE_VALUES` 把两个 GPR 拼成一个 `i128`，这个 `i128` 默认映射到 `fpr(i128)`；`G_BITCAST` 在 Fast 下复用源 bank，所以 `<2 x i64>` 也是 `fpr`。反向路径同理：`G_UNMERGE_VALUES` 的源是 `s128`，AArch64 把它的所有 operand 都设成 FPR，最后两条 `COPY $x0/$x1` 才是 FPR→GPR。

最终汇编（实测）：

```asm
i128_to_v2i64:
	mov	v0.d[0], x0
	mov	v0.d[1], x1
	ret
; 反向路径：fmov/mov 从 d0、v0.d[1] 取到 x0/x1
```

`v2i64_to_v4i32` 这种纯 bitcast 在最终汇编里可能只剩一条 `ret`：值已经在 `q0`，类型换了但寄存器没换。

### 聚合：字段是独立 vreg

```llvm
define <4 x i32> @build_vec(i32 %x) { %v0 = insertelement <4 x i32> poison, i32 %x, i32 0; %v1 = insertelement <4 x i32> %v0, i32 %x, i32 1; ret <4 x i32> %v1 }
define i64 @extract_struct({ i64, double } %s) { %r = extractvalue { i64, double } %s, 0; ret i64 %r }
```

`build_vec` 的 MIR：

```text
%0:gpr(i32) = COPY $w0
%2:fpr(<4 x i32>) = G_IMPLICIT_DEF
%3:gpr(i64) = G_CONSTANT i64 0
%1:fpr(<4 x i32>) = G_INSERT_VECTOR_ELT %2, %0(i32), %3(i64)
%5:gpr(i64) = G_CONSTANT i64 1
%4:fpr(<4 x i32>) = G_INSERT_VECTOR_ELT %1, %0(i32), %5(i64)
...
```

`extract_struct` 的 MIR 只剩 `%0:gpr(i64) = COPY $x0` 和 `$x0 = COPY %0(i64)`。没有 `G_EXTRACT`，也没有“struct bank”。聚合在进入 RegBankSelect 之前就被拆成字段，字段各自按标量规则选 bank。

## 例子三：SVE 与 RISC-V 的边界（实测 + guided）

AArch64 打开 SVE 后，scalable vector 仍然走 FPR（`ZPR` 被 `FPRRegBank` 覆盖）：

```llvm
define <vscale x 4 x i32> @sve_add(<vscale x 4 x i32> %a, <vscale x 4 x i32> %b) {
  %r = add <vscale x 4 x i32> %a, %b; ret <vscale x 4 x i32> %r
}
```

```text
%0:fpr(<vscale x 4 x i32>) = COPY $z0
%1:fpr(<vscale x 4 x i32>) = COPY $z1
%2:fpr(<vscale x 4 x i32>) = G_ADD %0, %1
$z0 = COPY %2(<vscale x 4 x i32>)
```

同一份 IR 在 RISC-V（`+v`）上落 `vrb`：

```text
%0:vrb(<vscale x 4 x s32>) = COPY $v8m2
%1:vrb(<vscale x 4 x s32>) = COPY $v10m2
%2:vrb(<vscale x 4 x s32>) = G_ADD %0, %1
$v8m2 = COPY %2(<vscale x 4 x s32>)
```

两者都不是“按运行时 vscale 选 bank”，而是按 LLT 的 scalable 标记和最小位宽选 partial mapping。

边界一：SVE predicate。给 `sve2.ll` 加一个返回 `<vscale x 4 x i1>` 的函数，官方 23.1.4 在 Legalizer 就崩：

```console
LLVM ERROR: Possible incorrect use of LLT::getNumElements() for scalable vector.
Scalable flag may be dropped, use LLT::getElementCount() instead
2. Running pass 'Legalizer' on function '@sve_pred'
```

没有 RegBankSelect 输出，因此“predicate 应该进哪个 bank”在 23.1.4 上只能标 guided/源码依据：AArch64 的 predicate 寄存器是 `P` 系列，不在 FPR bank 的 `[QQQQ, ZPR]` 覆盖里；真要支持，需要 target 增加对应 bank/寄存器类，而不是指望现有 FPR 映射。

边界二：RISC-V 固定长度向量。`rv.ll` 里的 `<4 x i32>` 在 legalizer 阶段失败：

```console
LLVM ERROR: unable to legalize instruction:
%6:_(<vscale x 2 x s32>) = G_INSERT_SUBVECTOR %5:_, %4:_(<4 x s32>), 0
(in function: rv_v4i32_add)
```

`-global-isel-abort=0` 时可以看到 `failedISel: true`，vreg 的 class 仍是 `_`，说明问题发生在 RegBankSelect 之前，不是 bank 选错。RISC-V 的 scalable 路径能走通，不代表固定长度向量路径也完整；这是 23.1.4 的支持边界，不应归因于 `VRB` 映射。

## 对比与取舍

| 类型 | AArch64 legalized 形态 | bank | RISC-V 对照 | 关键点 |
| --- | --- | --- | --- | --- |
| `<4 x i32>` | 原样 | `fpr` | `vrb` | 128 位，NEON 自然宽度 |
| `<8 x i16>` | 原样 | `fpr` | `vrb` | 同宽不同元素类型 |
| `<3 x i32>` | `<4 x i32>` | `fpr` | 视 legalizer 而定 | padding，不是 3 lane |
| `<8 x i64>` | 4 × `<2 x i64>` | `fpr` | 可能拆成多个 VRB 值 | bank 能覆盖 ≠ 一条指令能算 |
| `<vscale x 4 x i32>` | 原样 | `fpr`（ZPR） | `vrb` | 按 known-min 选 mapping |
| `i128` 加法 | `G_UADDO` + `G_UADDE` | `gpr(i64)` | `gprb(s64)` | legalizer 先拆 |
| `i128` 值/bitcast | `i128` / `<2 x i64>` | `fpr` | 视路径而定 | `>64` 默认 FPR |
| `i1` | `i32` | `gpr` | `gprb` | CC 不是布尔 bank |
| `{i64, double}` | 独立字段 | 各自决定 | 各自决定 | MIR 无 struct 寄存器 |

取舍的核心不是“哪种类型更该在 FPR”，而是：legalizer 的合法化策略决定了 RegBankSelect 的输入形态；bank 的容量与覆盖决定了 mapping 是否合法；target 的指令宽度决定了 512 位值是否还要再拆；ABI / 调用约定决定了聚合与 i128 在边界上如何传递（Ch 12）。

## 踩雷集锦

1. **“所有向量都在 FPR”**：AArch64 的固定/scalable 向量确实默认 FPR，但 RISC-V 用 `vrb`，SVE predicate 也不是 FPR。先看 target 的 bank 定义和 LLT，再下结论。

2. **“`i128` 一定是两个 GPR”**：只有被 legalizer 拆成 64 位算术的 `i128` 才是 `gpr(i64)` 链。作为值的 `i128`、`G_MERGE_VALUES` 的结果、`s128` 的 `G_TRUNC` 源，都会默认落 FPR。

3. **“`s1` 应该进 CC bank”**：CC 只覆盖 `CCR`/NZCV。`i1` 被扩成 `i32` 放 GPR，布尔语义靠 `G_AND 1` 维持；`G_SELECT` 的条件也是 `gpr(i32)`。

4. **“`<3 x i32>` 在 MIR 里还是 3 个 lane”**：legalizer 会 padding 成 `<4 x i32>`。按 3 个 lane 去解释 `G_ADD`、COPY 或寄存器压力都会错。

5. **“scalable vector 的 bank 由运行时 vscale 决定”**：mapping 用的是 `TypeSize::getKnownMinValue()`；运行时长度只影响实际寄存器占用和指令，不影响 RegBankSelect 选哪个 bank。

## 进阶

1. **partial mapping 与多寄存器值**：AArch64 的 `PartMappings` 把 16/32/64/128/256/512 位分别映射到 FPR，把 32/64/128 位映射到 GPR。给一个 256 位向量构造 MIR，观察 RegBankSelect 直接用 256 位 partial mapping，还是 legalizer 已经把它拆成两个 128 位值。

2. **SVE predicate 的 bank 设计**：23.1.4 在 `<vscale x 4 x i1>` 上崩在 Legalizer。若要继续，需要先修 legalizer 的 scalable 类型处理，再决定 predicate 进哪个寄存器类。读 `AArch64RegisterBanks.td` 时注意：FPR 只列了 `QQQQ` 和 `ZPR`，没有 `PPR`。这部分标 guided，不要伪造 RegBankSelect 输出。

3. **RISC-V 固定向量与 scalable 向量的支持差异**：`rv_sve_add` 能生成 `vrb`，`rv_v4i32_add` 却在 legalizer 失败。对比 `RISCVRegisterBankInfo.cpp` 里 `G_LOAD` / `G_STORE` / `G_SELECT` 的 `Ty.isVector()` 分支和 legalizer 的 `G_INSERT_SUBVECTOR` 规则，能看出“bank 支持”与“legalizer 支持”是两件事。

## 动手练习

1. 把 `vec.ll` 的 `v4i32_add` 改成 `<8 x i32>`，先预测 MIR 里有几条 `G_ADD`、每条 LLT 是什么，再跑 `-stop-after=legalizer` 和 `-stop-after=regbankselect` 验证。
2. 给 `i128_add` 加一个 `bitcast i128 -> <2 x i64>` 的返回值，观察 `G_UADDO/G_UADDE` 链与 `G_MERGE_VALUES/G_BITCAST` 链是否同时出现，以及各自的 bank。
3. 把 `bool_and` 改成 `select i1` 返回 `double`，确认条件仍是 `gpr(i32)`、数据是 `fpr(f64)`，并解释为什么没有 `CC`。
4. 用 `-mattr=+sve -aarch64-enable-gisel-sve=1` 跑 `sve2.ll`，再把其中一个函数改成返回 `<vscale x 4 x i1>`，记录崩溃点；说明这是 legalizer 还是 RegBankSelect 的问题。
5. 在 RISC-V 上分别跑 `rv_sve_add` 和 `rv_v4i32_add`，对比 `failedISel`、vreg class 和错误信息，写一段“支持边界”说明。

## 本章重点整理

- RegBankSelect 只看 legalized LLT；向量的拆分、`i128` 的拆解、`s1` 的扩宽、`v3s32` 的 padding 都发生在它之前。
- AArch64 的 GPR 上限 128 位，FPR 上限 512 位（`QQQQ` + `ZPR`）；RISC-V 的 VRB 按 64/128/256/512 位分格。
- 默认映射里，任何向量和大于 64 位的标量都倾向 FPR；`i128` 的“值”和“算术”因此可能落在不同 bank。
- `s1` 不进 CC；CC 只覆盖 NZCV。布尔值在 GPR 里用 `i32` 承载。
- 聚合不是 LLT：字段被拆成独立 vreg，`G_MERGE_VALUES` / `G_UNMERGE_VALUES` / `G_INSERT_VECTOR_ELT` 是常见残留形态。
- 23.1.4 的 SVE predicate 与 RISC-V 固定长度向量都有 legalizer 边界；不要把 legalizer 失败误判成 bank 选错。

## 自我检核

- [ ] 能解释为什么 `<4 x i32>` 和 `i128` 都可能落 FPR，但它们的指令选择不同
- [ ] 能说出 AArch64 GPR/FPR 的容量上限，以及 `QQQQ`、`ZPR` 各覆盖什么
- [ ] 能解释 `i128` 加法为什么在 MIR 里变成 `G_UADDO` / `G_UADDE`
- [ ] 能说明 `s1` 为什么不进 CC bank
- [ ] 能区分“bank 能覆盖 512 位”和“一条 NEON 指令能算 512 位”
- [ ] 能指出 SVE predicate 崩溃发生在哪个 pass，以及为什么不能拿它当 RegBankSelect 结论

## 延伸阅读

### 源码

- **[AArch64RegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterBanks.td)**
  - **读哪里**：`GPRRegBank`、`FPRRegBank`、`CCRegBank` 的寄存器类列表。
  - **学什么**：`XSeqPairsClass`、`QQQQ`、`ZPR`、`CCR` 分别覆盖哪些寄存器。
  - **和本章的关联**：FPR 能覆盖 scalable vector、GPR 上限 128 位的直接依据。

- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：构造函数里的容量断言、`getInstrMapping` 的默认 bank 判定、`G_UNMERGE_VALUES` / `G_BUILD_VECTOR` / `G_INSERT_VECTOR_ELT` 分支。
  - **学什么**：向量与大标量为什么默认 FPR，以及拆/合指令如何保持 bank。
  - **和本章的关联**：例子一、二、三的 mapping 依据。

- **[AArch64GenRegisterBankInfo.def](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def)**
  - **读哪里**：`PartMappings` 的 16..512 位 FPR 项与 32/64/128 位 GPR 项、`ValMappings` 的 cross-bank copy 项。
  - **学什么**：partial mapping 的位宽粒度，以及 128 位以上跨 bank copy 为什么是无效项。
  - **和本章的关联**：解释 `i128` 为什么要走 merge/unmerge 而不是直接跨 bank COPY。

- **[RISCVRegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/RISCV/GISel/RISCVRegisterBanks.td) 与 [RISCVRegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/RISCV/GISel/RISCVRegisterBankInfo.cpp)**
  - **读哪里**：`GPRB` / `FPRB` / `VRB` 定义，`PartMappings` 的 VRB64..512，`getVRBValueMapping`。
  - **学什么**：RISC-V 为什么需要独立的向量 bank，以及 `getKnownMinValue()` 如何选 mapping。
  - **和本章的关联**：`rv_sve_add` 的 `vrb` 输出与固定向量失败案例。

### 文档

- **[LLVM GlobalISel — Generic Opcode](https://llvm.org/docs/GlobalISel/GenericOpcode.html)**
  - **读哪里**：`G_MERGE_VALUES`、`G_UNMERGE_VALUES`、`G_BUILD_VECTOR`、`G_INSERT_VECTOR_ELT`、`G_EXTRACT_VECTOR_ELT`。
  - **学什么**：这些 opcode 的 operand 顺序、类型约束和“谁负责拆/合”。
  - **和本章的关联**：读例子二 MIR 的语法依据。

Part 2 到此结束。练习 C 把前五章的真实语义收束成一次实现：给 AArch64 加一个受 feature gate 控制的 custom opcode，并写出它的默认 mapping 与 alternative。

→ [练习 C：实现一个 custom mapping](./practice-c-custom-mapping.md)
