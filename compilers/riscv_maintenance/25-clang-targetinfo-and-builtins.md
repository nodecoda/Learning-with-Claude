# Ch 25 — Clang TargetInfo 与 builtins：宏、类型与 interrupt attribute

> **目标**：把 RISC-V 的“用户可见面”从前端到底端串起来：`-march` 如何变成 `__riscv_*` 宏，`long double`/`__int128`/RVV 类型有多宽，`__attribute__((interrupt("machine")))` 如何走完 Sema → CodeGen → 后端，`__builtin_riscv_*` 与 `__builtin_rvv_*` 又在哪里定义。

> **环境**：源码 `/home/ubuntu/llvm/trees/ct-instr-trunk`（`analysis/riscv-maintenance`，HEAD `0fc5afa04c23`），构建 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`（LLVM `24.0.0git`，`X86;AArch64;RISCV`，assertions ON）。本章所有“实测”均来自该构建，源码只读。

> **前置**：读完 Ch 02（`-march` / `-mattr` → `Feature` → `hasStdExt*()`）与 Ch 24（driver 如何把 `-march` / `-mabi` 变成 cc1 参数）。本章不重复 feature 配电盘和 driver 优先级，只讲 TargetInfo 之后的那一段。

---

## 25.1 为什么维护者要读 TargetInfo

后端只看见 `+i +m +a +f +d +c ... +zba` 这样的扁平 feature 列表；用户看见的却是宏、类型宽度、attribute 和 builtin。把这两端接起来的是 `clang/lib/Basic/Targets/RISCV.cpp`（668 行）、`RISCV.h`（230 行）和 `clang/lib/CodeGen/Targets/RISCV.cpp`（1190 行）。文件不大，但一个新扩展的“可被用户使用”几乎全在这里落地：

| 用户可见面 | 落点 | 漏测后的症状 |
|---|---|---|
| `__riscv_*` 宏 | `RISCV.cpp:147`（`getTargetDefines`） | 代码走了错误分支，或 `#ifdef` 永远不成立 |
| 类型宽度 / ABI | `RISCV.h:38`、`CodeGen/Targets/RISCV.cpp:739` | `sizeof` 对、传参错；或反过来 |
| `interrupt` attribute | `Attr.td:2476`、`SemaRISCV.cpp:1644`、`CodeGen/Targets/RISCV.cpp:1128` | 编译通过但入口不保存寄存器，运行时才炸 |
| `__builtin_riscv_*` | `BuiltinsRISCV.td:18` | 名字写错、feature 不满足，报错不在后端 |
| `__builtin_rvv_*` | `Targets/RISCV.cpp:333` + TableGen | intrinsic 能用但 overload/策略选错 |
| 原子宽度 | `RISCV.h:193` / `:224` | `atomicrmw` 被错误地展开或内联（Ch 20） |

前端改动比后端更容易漏测，因为**后端可以用 `llc -mattr=+v` 直接绕过 TargetInfo**。只跑 `llc`，你验证的是“后端收到 +v 后对不对”；`clang -march=rv64gcv` 这一层仍然可能是坏的。上游对应测试也分三层：`clang/test/Preprocessor/riscv-target-features.c`（宏）、`clang/test/Sema/riscv-interrupt-attr.c`（校验）、`clang/test/CodeGen/RISCV/riscv-interrupt-attr.c`（汇编）。改 TargetInfo 时三层都要看。

---

## 25.2 先建立直觉：一个 `-march` 特征如何变成用户能看到的宏

```text
-march=rv64gcv
   │
   │  driver：clang/lib/Driver/ToolChains/Arch/RISCV.cpp
   │  getRISCVArch → RISCVISAInfo::parseArchString → toFeatures()
   ▼
cc1：-target-feature +v +zve32x +zvl128b ... -target-abi lp64d
   │
   │  TargetInfo：clang/lib/Basic/Targets/RISCV.cpp
   │  initFeatureMap(:342) → parseFeatures(XLen, FeaturesVec)
   ▼
handleTargetFeatures(:414) → ISAInfo = parseFeatures(...)
   │
   ├─ ISAInfo->hasExtension("zve32x")  → defineMacro("__riscv_vector", 1)      :223
   ├─ ISAInfo->getMinVLen()            → defineMacro("__riscv_v_min_vlen", 128) :214
   ├─ ISAInfo->getFLen()               → defineMacro("__riscv_flen", 64)        :208
   ├─ ISAInfo->getExtensions()         → __riscv_zvl128b / __riscv_v ...         :180
   └─ getVersionValue(1, 0)            → defineMacro("__riscv_v_intrinsic", 1000000) :227
   ▼
用户代码：#ifdef __riscv_vector / #if __riscv_v_intrinsic >= 1000000
   ▼
Sema 类型：__rvv_int8m1_t（sizeless）→ CodeGen：<vscale x 8 x i8>
   ▼
后端：vsetvli + vadd.vv
```

有三点容易看错：

1. **`parseFeatures` 被调用两次**：`initFeatureMap` 先为 `FeatureMap` 补全隐含扩展；`handleTargetFeatures` 再解析一次，把结果存进 `ISAInfo`，并决定默认 ABI。`getTargetDefines` 读的是后者。
2. **扩展宏是循环自动生成的**，所以新增 `zba` 不需要在 `getTargetDefines` 里手写 `__riscv_zba`；但 `__riscv_atomic`、`__riscv_compressed`、`__riscv_vector` 这些“语义宏”是手写条件。
3. **`__riscv_v_intrinsic` 是无条件定义的**（`:227`），即使 `-march=rv64gc` 也有。它是“intrinsic 规范版本”，不是“V 扩展开关”；开 V 要用 `__riscv_vector`。

`getVersionValue(Major, Minor)` 的公式是 `Major * 1000000 + Minor * 1000`（`:143`），所以 `1.0 → 1000000`，`2.0 → 2000000`（实测 `__riscv_m 2000000`）。

---

## 25.3 宏清单与实测

`getTargetDefines` 的完整条件可以按“无条件 → ABI → 扩展 → 特殊语义”四组读：

| 宏 | 条件 | 源码行 | 实测值（rv64gc/lp64d） |
|---|---|---|---|
| `__riscv` | 永远 | 149 | `1` |
| `__riscv_xlen` | 永远 | 151 | `64`（RV32 为 `32`） |
| `__riscv_float_abi_soft/single/double` | `-mabi` | 167–173 | `lp64=soft`、`lp64f=single`、`lp64d=double` |
| `__riscv_compressed` | `c`（`zca` 也会命中） | 220 | `1` |
| `__riscv_<ext>` | 每个扩展 | 180–186 | `__riscv_zba 1000000` |
| `__riscv_mul` | `zmmul` | 188 | `1` |
| `__riscv_div` / `__riscv_muldiv` | `m` | 191–194 | `1` / `1` |
| `__riscv_atomic` | `a`（或 `zaamo`+`zalrsc`） | 196–198 | `1` |
| `__GCC_HAVE_SYNC_COMPARE_AND_SWAP_*` | `zalrsc` | 200–206 | 1/2/4/8 |
| `__riscv_flen` | `FLen != 0` | 208 | `64` |
| `__riscv_v_min_vlen` / `_v_elen` / `_v_elen_fp` | `MinVLen != 0` | 214–218 | `128` / `64` / `64` |
| `__riscv_vector` | `zve32x` | 223 | 不定义 |
| `__riscv_v_intrinsic` | 永远 | 227 | `1000000` |
| `__riscv_v_fixed_vlen` | `VScaleMin == VScaleMax` | 229–232 | 不定义 |

`-march=rv64gc` 的真实片段（`rv64gcv` 另加 `__riscv_vector`、`__riscv_v_min_vlen 128`、`__riscv_v_elen 64`；`rv64gc_zba` 另加 `__riscv_zba 1000000`）：

```text
#define __riscv 1
#define __riscv_arch_test 1
#define __riscv_atomic 1
#define __riscv_compressed 1
#define __riscv_div 1
#define __riscv_flen 64
#define __riscv_m 2000000
#define __riscv_mul 1
#define __riscv_v_intrinsic 1000000
#define __riscv_xlen 64
```

注意两个反直觉点：

- `__riscv_atomic` 不是“有 `zaamo` 就算”。实测 `rv64i_zaamo` 只有 `__riscv_zaamo`，`rv64i_zaamo_zalrsc` 才同时给出 `__riscv_atomic`。`a` 是 `zaamo` + `zalrsc` 的组合。
- `__riscv_compressed` 对 `c` 和 `zca` 都成立；不要用 `__riscv_c` 判断“有没有压缩指令”，用 `__riscv_compressed`。

---

## 25.4 类型与 ABI：long double、__int128、char、max_align_t、RVV 类型

RISC-V 的 `long double` 在所有 ABI 下都是 **128 位 IEEE quad**，与 `-mabi=lp64d` 的 `d` 无关。`LongDoubleWidth`/`Align` 在 `RISCV.h:43-46` 固定为 128，`SuitableAlign` 也是 128。实测：

| 表达式 | rv64gc/lp64d | rv64gc/lp64 | rv32imac/ilp32 |
|---|---|---|---|
| `sizeof/_Alignof(long double)` | 16 / 16 | 16 / 16 | 16 / 16 |
| `sizeof/_Alignof(__int128)` | 16 / 16 | 16 / 16 | **不支持** |
| `(char)-1 < 0` | 0（unsigned） | 0 | 0 |
| `sizeof/_Alignof(max_align_t)` | 32 / 16 | 32 / 16 | 32 / 16 |

`__int128` 在 RV32 的报错原文：

```text
error: __int128 is not supported on this target
```

`long double` 虽然 128 位，却**不走 FPR**。`classifyArgumentType`（`CodeGen/Targets/RISCV.cpp:739`）先判断 `FLen >= Size`；`FLen=64 < 128`，于是落到 `Size <= 2*XLen` 的整数对路径：

```asm
store:                                  # void store(long double *p, long double a)
	sd	a1, 0(a0)                       # a 的低 64 位
	sd	a2, 8(a0)                       # a 的高 64 位
	ret
```

所以 `lp64d` 下 `long double` 参数在 `a1/a2`，不在 `fa0/fa1`；运算走 `__addtf3` 软浮点库调用。把 `__riscv_flen=64` 误当成“long double 是 64 位”是常见错误。

`char` 在 RISC-V 上无符号（`char_signed=0`），`wchar_t` 是有符号（`RISCV.h:47`）。RVV 类型是 **sizeless**：`__rvv_int8m1_t` / `vint8m1_t` 存在，但不能定义全局变量，也不能 `sizeof`：

```text
error: non-local variable with sizeless type '__rvv_int8m1_t'
error: invalid application of 'sizeof' to sizeless type '__rvv_int8m1_t'
```

需要固定长度时用 `__attribute__((riscv_rvv_vector_bits(N)))`，但它要求 `-mrvv-vector-bits` 同时给出 `zvl` 或 `[64,65536]` 内的 2 的幂，否则：

```text
error: 'riscv_rvv_vector_bits' is only supported when '-mrvv-vector-bits=<bits>' is specified with a value of "zvl" or a power 2 in the range [64,65536]
```

最后，原子宽度也在 TargetInfo：`RISCV64TargetInfo::setMaxAtomicWidth` 在 `zalrsc` 存在时把 `MaxAtomicInlineWidth` 设为 64（RV32 为 32），`MaxAtomicPromoteWidth` 两边都是 128（`RISCV.h:189/193/220/224`）。Ch 20 的 `atomicrmw` 分叉就是读这两个值。

---

## 25.5 `interrupt` attribute 全链

`__attribute__((interrupt("machine")))` 不是后端自己认的字符串，它从前端一路变成 LLVM 函数属性：

```text
Attr.td:2476  RISCVInterrupt（Spellings = GCC<"interrupt">，VariadicEnumArgument）
   │  TableGen 生成 RISCVInterruptAttr / ParseKind = "Interrupt"
   ▼
SemaDeclAttr.cpp:6714 handleInterruptAttr → :6738 S.RISCV().handleInterruptAttr
   ▼
SemaRISCV.cpp:1644
   ├─ :1662 必须是函数；:1668 不能有参数；:1674 必须返回 void
   ├─ :1680 最多 3 个参数；:1687 去重
   ├─ :1732 非法组合（supervisor/machine 混用等）
   ├─ :1749 rnmi / qci-* / SiFive-CLIC-* 的扩展检查
   └─ :1792 写入 RISCVInterruptAttr
   ▼
CodeGen/Targets/RISCV.cpp:1128 setTargetAttributes
   ├─ :1145 遍历 kind
   └─ :1180 Fn->addFnAttr("interrupt", Kind)
   ▼
LLVM：RISCVISelLowering.cpp:29650 读取 "interrupt"
   ├─ supervisor → SRET_GLUE（sret）
   ├─ rnmi      → MNRET_GLUE（mnret）
   ├─ qci-*    → QC_C_MILEAVERET_GLUE
   └─ 其它      → MRET_GLUE（mret）
   ▼
RISCVRegisterInfo.cpp:77 选 CSR_*_Interrupt_SaveList；RISCVCallingConv.td:49 定义保存集
```

支持的值与扩展要求（`AttrDocs.td:3289`、`SemaRISCV.cpp:1749`）：

| kind | 返回指令 | 扩展要求 |
|---|---|---|
| `machine`（默认） | `mret` | 无 |
| `supervisor` | `sret` | Sema 不检查 S |
| `rnmi` | `mnret` | `Smrnmi` |
| `qci-nest` / `qci-nonest` | `qc.c.mileaveret` | `Xqciint` |
| `SiFive-CLIC-preemptible` / `-stack-swap` | `mret` + 额外 CSR 保存 | `XSfmclic`（experimental） |

实测 `-O2`，同一份源码里的两个函数：

```asm
m:                                      # interrupt("machine")
	addi	sp, sp, -16
	sd	a0, 8(sp)
	sd	a1, 0(sp)
	lui	a0, %hi(g)
	li	a1, 1
	sw	a1, %lo(g)(a0)
	ld	a0, 8(sp)
	ld	a1, 0(sp)
	addi	sp, sp, 16
	mret

s:                                      # interrupt("supervisor")
	... 同样的保存/恢复 ...
	sret
```

当 handler 里调用普通函数时，保存集会膨胀到 288 字节（实测）：`ra`、`t0-t2`、`a0-a7`、`t3-t6` 以及 `ft0-ft11`/`fa0-fa7` 全部落栈，然后 `call f`，最后 `mret`。这正是 Ch 10 的栈帧逻辑：`CSR_Interrupt` 把 `X1, X5-X31` 标成 callee-saved（`RISCVCallingConv.td:49`），所以寄存器分配器会把所有被 handler 改写的寄存器都保存；不用的寄存器不会被无谓保存。

两个容易误解的点：

- **没有 `CC_RISCV_INTERRUPT` 这样的独立调用约定。** Clang 用默认 C 调用约定，只在函数上写 `"interrupt"="machine"`；后端在 `CC_RISCV` 路径里读这个属性切换 prologue/epilogue 和返回指令。
- **`supervisor` 不检查 S 扩展。** 实测 `-march=rv64gc`（没有 `s`）也能编译出 `sret`；`rnmi`/`qci-*` 则会报错：

```text
error: RISC-V 'interrupt' attribute 'rnmi' requires extension 'Smrnmi'
error: RISC-V 'interrupt' attribute 'qci-nest' requires extension 'Xqciint'
error: RISC-V 'interrupt' attribute contains invalid combination of interrupt types
```

---

## 25.6 builtins：`__builtin_riscv_*` 与 `__builtin_rvv_*`

`clang/include/clang/Basic/BuiltinsRISCV.td` 在快照里有 **459 个 `def`**。每个 `def` 用 `RISCVBuiltin` 模板生成名字、原型和 feature 约束（`:18-22`）：

```tablegen
class RISCVBuiltin<string prototype, string features = ""> : TargetBuiltin {
  let Spellings = ["__builtin_riscv_" # NAME];
  let Prototype = prototype;
  let Features = features;
}
```

例如 `clz_32` 的原型是 `unsigned int(unsigned int)`，feature 是 `zbb|xtheadbb`；`pause` 是 `void()`，feature 是 `zihintpause`。feature 不满足时，报错发生在 `CodeGenUtils/FunctionUtils.cpp:65` 的 `checkTargetFeatures`（CodeGen 阶段，`-fsyntax-only` 不触发）：

```text
error: '__builtin_riscv_clz_32' needs target feature zbb|xtheadbb
error: '__builtin_riscv_pause' needs target feature zihintpause
```

实测可用的三个：

```asm
clzw	a0, a0          # __builtin_riscv_clz_32，-march=rv64gc_zbb
orc.b	a0, a0          # __builtin_riscv_orc_b_32，-march=rv64gc_zbb
sext.w	a0, a0
pause                   # __builtin_riscv_pause，-march=rv64gc_zihintpause
```

这里有一个名字陷阱：**不存在 `__builtin_riscv_thread_pointer`**。线程指针是通用 builtin `__builtin_thread_pointer`（`Builtins.td:1232`），RISC-V 上落到：

```asm
mv	a0, tp
```

RVV 走另一条路。`clang/include/clang/Basic/riscv_vector.td` 的 `defm vadd : RVVIntBinBuiltinSet;`（`:767`）经 `riscv_vector_common.td` 的 `RVVBuiltin` 模板展开成 LMUL × 类型 × policy 的组合；本构建的 `riscv_vector_builtins.inc` 有 **3475 个 `BI__builtin_rvv_*` 枚举**。它们不是每个 intrinsic 在 `.td` 里手写一行，而是一条记录生成一族：

```tablegen
defm vadd : RVVIntBinBuiltinSet;   // → __builtin_rvv_vadd_vv_i8m1、_tu、_m、_tum ...
```

`__builtin_rvv_*` 的名字由 `getTargetBuiltins` 注册（`Basic/Targets/RISCV.cpp:333`），前缀是 `"__builtin_rvv_"`。用户写 `#include <riscv_vector.h>` 后，`#pragma clang riscv intrinsic vector` 打开 `DeclareRVVBuiltins`（`ParsePragma.cpp:4269`），未限定名查找时 `SemaLookup.cpp:962` 调 `RISCVIntrinsicManager` 按需构造 `FunctionDecl`（`SemaRISCV.cpp:212/372/498`）。所以头文件里**没有** `vadd_vv_i8m1` 的声明，只有 `vint8m1_t` 等 typedef、`__riscv_vsetvl_*` 宏和 `__riscv_intrinsic_*` 扩展标记。

为什么用“头文件 + `__builtin_rvv_*`”而不是每个 intrinsic 一个 `__builtin_riscv_*`？因为 RVV 的签名是组合爆炸：一条 `vadd` 就有 7 种 LMUL、8 种元素类型、6 种 policy/mask 变体。手写 3475 个 builtin 不可维护；TableGen 把“记录 → 枚举/字符串/原型/IR 映射”一次生成，Sema 再按名字和参数类型做 overload。代价是：**拼错一个后缀，报错是“undeclared function”，不是“unknown builtin”**，因为名字查找根本没命中。

---

## 25.7 RVV intrinsic 版本宏与 `riscv_vector.h` 的分派

`__riscv_v_intrinsic` 的值是 `1000000`（1.0），由 `getVersionValue(1, 0)` 生成（`Basic/Targets/RISCV.cpp:227`）。它无条件定义：实测 `-march=rv64i` 和 `-march=rv64gc` 都有 `#define __riscv_v_intrinsic 1000000`。因此：

- 判断“有没有 V”用 `__riscv_vector`（`:223`，条件 `zve32x`）。
- 判断“intrinsic 规范版本”用 `#if __riscv_v_intrinsic >= 1000000`。
- 不要用 `#ifdef __riscv_v_intrinsic` 当扩展开关，它永远成立。

`__riscv_v_min_vlen` 来自 `ISAInfo->getMinVLen()`，即 `-march` 里的 `zvl*b`；`__riscv_v_fixed_vlen` 来自 `getVScaleRange`（`:373`），只有 `VScaleMin == VScaleMax` 才定义。`-mrvv-vector-bits` 在 driver 里被翻译成 `-mvscale-min/-mvscale-max`（`Clang.cpp:2117-2152`），然后 `getVScaleRange` 把 `LangOpts.VScaleMin/Max` 与 `Zvl*b` 取大。实测组合：

| `-march` | `-mrvv-vector-bits` | `__riscv_v_min_vlen` | `__riscv_v_fixed_vlen` | `vscale_range` |
|---|---|---|---|---|
| `rv64gcv` | 无 | 128 | 不定义 | `(2,1024)` |
| `rv64gcv` | `zvl` | 128 | 128 | `(2,2)` |
| `rv64gcv` | `256` | 128 | 256 | `(4,4)` |
| `rv64gcv_zvl256b` | 无 | 256 | 不定义 | `(4,1024)` |
| `rv64gcv_zvl256b` | `zvl` | 256 | 256 | `(4,4)` |

关键结论：**`-mrvv-vector-bits` 不改变 `__riscv_v_min_vlen`，也不改变类型宽度；它只改 vscale（以及由此产生的 `__riscv_v_fixed_vlen`）和 codegen 的 AVL 折叠。** 类型仍然是 sizeless。要固定长度类型，仍要 `riscv_rvv_vector_bits` + `-mrvv-vector-bits`。

`riscv_vector.h` 在本快照里是**生成物**：`clang/lib/Headers/CMakeLists.txt:526-530` 用 `clang_generate_header(-gen-riscv-vector-header riscv_vector.td riscv_vector.h)` 生成到 `builds/ct-instr-trunk/tools/clang/lib/Headers/riscv_vector.h`。它没有 `#if __riscv_v_intrinsic` 的版本分派；分派信息是每个扩展的 `__riscv_intrinsic_*` 宏和一整套 `typedef __rvv_*`。如果你在旧资料里看到 v0.7/v1.0 的 `#if` 分支，那不是这棵树的形状；在这个版本，版本判断是消费者写在 `#if __riscv_v_intrinsic >= ...` 里的。

---

## 25.8 三个必须同步的钩子：`initFeatureMap`、`handleTargetFeatures`、`hasFeature`

| 钩子 | 行号 | 职责 | 改错时的表现 |
|---|---|---|---|
| `initFeatureMap` | 342 | 解析 feature、补全隐含扩展、写入 `Features` map | `-mattr` 与 `-march` 行为不一致 |
| `handleTargetFeatures` | 414 | 存 `ISAInfo`、定默认 ABI、校验 `ilp32e + d` | `unknown target ABI` 或 `invalid feature combination` |
| `getTargetDefines` | 147 | 读 `ISAInfo` 生成宏 | 宏缺失/值不对，但后端正常 |
| `getVScaleRange` | 373 | `Zvl*b` + `VScaleMin/Max` → vscale | `__riscv_v_fixed_vlen` 不出现或范围错 |
| `hasFeature` | 397 | 给 Sema 查扩展（如 interrupt 的 `xqciint`） | attribute 该报错没报，或该放行被拒 |

`hasFeature` 上方的注释写得很直白：“need to sync with handleTargetFeatures”。它先处理 `riscv`/`riscv32`/`riscv64`/`32bit`/`64bit`/`experimental`，其余全部落到 `ISAInfo->hasExtension(Feature)`。所以改 feature 名字时，`RISCVFeatures.td`、`RISCVISAInfo`、`hasFeature` 和 Sema 的字符串必须一起改。

---

## 25.9 改一个 TargetInfo 特性的验收清单

1. **宏**：`clang -dM -E -x c /dev/null --target=... -march=...` 看新增/消失的 `__riscv_*`，并和 `getTargetDefines` 的条件对齐。
2. **类型**：用 `-S -emit-llvm` 看 `sizeof`/`_Alignof` 常量，不要只读 `TargetInfo` 的字段。
3. **attribute**：正例看 IR 的 `"interrupt"` / `"target-features"`，负例看 Sema 报错原文。
4. **builtin**：正例看汇编，负例删掉 feature 看 `needs target feature`。
5. **测试**：`clang/test/Preprocessor`、`clang/test/Sema`、`clang/test/CodeGen/RISCV` 三层至少各一条；只加 `llvm/test/CodeGen/RISCV` 会漏掉整个前端。

---

## 25.10 源码落点

| 主题 | 文件:行 |
|---|---|
| 宏总入口 | `clang/lib/Basic/Targets/RISCV.cpp:147` |
| 扩展宏循环 | `clang/lib/Basic/Targets/RISCV.cpp:180` |
| `__riscv_vector` / `__riscv_v_intrinsic` | `clang/lib/Basic/Targets/RISCV.cpp:223` / `:227` |
| `initFeatureMap` | `clang/lib/Basic/Targets/RISCV.cpp:342` |
| `getVScaleRange` | `clang/lib/Basic/Targets/RISCV.cpp:373` |
| `hasFeature` | `clang/lib/Basic/Targets/RISCV.cpp:397` |
| `handleTargetFeatures` | `clang/lib/Basic/Targets/RISCV.cpp:414` |
| 类型宽度 / `LongDouble` | `clang/lib/Basic/Targets/RISCV.h:38-49` |
| 原子宽度 | `clang/lib/Basic/Targets/RISCV.h:189-226` |
| RVV 类型表 | `clang/include/clang/Basic/RISCVVTypes.def` |
| `interrupt` 定义 | `clang/include/clang/Basic/Attr.td:2476` |
| Sema 分派 | `clang/lib/Sema/SemaDeclAttr.cpp:6714` / `:6738` |
| Sema 校验 | `clang/lib/Sema/SemaRISCV.cpp:1644-1793` |
| CodeGen 属性 | `clang/lib/CodeGen/Targets/RISCV.cpp:1128-1180` |
| ABI 分类 | `clang/lib/CodeGen/Targets/RISCV.cpp:739` / `:886` |
| `__builtin_riscv_*` 定义 | `clang/include/clang/Basic/BuiltinsRISCV.td:18` |
| builtin feature 检查 | `clang/lib/CodeGenUtils/FunctionUtils.cpp:65-90` |
| RVV 记录 | `clang/include/clang/Basic/riscv_vector.td:767` |
| RVV Sema 管理器 | `clang/lib/Sema/SemaRISCV.cpp:212/372/498` |
| RVV 名字查找 | `clang/lib/Sema/SemaLookup.cpp:962` |
| 头文件生成 | `clang/lib/Headers/CMakeLists.txt:526-530` |
| 返回指令选择 | `llvm/lib/Target/RISCV/RISCVISelLowering.cpp:29650-29672` |
| interrupt 保存集 | `llvm/lib/Target/RISCV/RISCVCallingConv.td:49-73` |

---

## 25.11 可跑实验

以下命令都先 `source /home/ubuntu/llvm/toolset/env.sh`，并设 `T=/home/ubuntu/llvm/builds/ct-instr-trunk`。

### 实验 1：三种 `-march` 的宏 diff
```bash
for M in rv64gc rv64gcv rv64gc_zba; do
  "$T/bin/clang" --target=riscv64-unknown-elf -march=$M -dM -E -x c /dev/null \
    | grep '__riscv' | sort > /tmp/$M.txt
done
diff -u /tmp/rv64gc.txt /tmp/rv64gcv.txt
diff -u /tmp/rv64gc.txt /tmp/rv64gc_zba.txt
```

真实输出（`rv64gc → rv64gcv`）新增 `__riscv_v`、`__riscv_vector`、`__riscv_v_min_vlen 128`、`__riscv_v_elen 64`、`__riscv_zve32x/zve64x/zvl128b` 等；`rv64gc → rv64gc_zba` 只多一行：

```text
+#define __riscv_zba 1000000
```

### 实验 2：`long double` / `__int128` / `char` / `max_align_t`
```bash
cat > /tmp/sz.c <<'EOF'
#include <stddef.h>
const unsigned long a = sizeof(long double);
const unsigned long b = _Alignof(long double);
const unsigned long c = sizeof(max_align_t);
const unsigned long d = _Alignof(max_align_t);
const int e = (char)-1 < 0;
const unsigned long i = sizeof(__int128);
EOF
"$T/bin/clang" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -S -emit-llvm /tmp/sz.c -o - | grep '^@'
"$T/bin/clang" --target=riscv32-unknown-elf -march=rv32imac -mabi=ilp32 -S -emit-llvm /tmp/sz.c -o - 2>&1 | head -3
```

RV64 实测 `@a=16 @b=16 @c=32 @d=16 @e=0 @i=16`；RV32 在 `__int128` 处报：

```text
error: __int128 is not supported on this target
```

### 实验 3：`interrupt("machine")` vs `interrupt("supervisor")`
```bash
cat > /tmp/irq.c <<'EOF'
extern int g;
__attribute__((interrupt("machine"))) void m(void) { g = 1; }
__attribute__((interrupt("supervisor"))) void s(void) { g = 2; }
EOF
"$T/bin/clang" --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S /tmp/irq.c -o -
```

实测 `m` 以 `mret` 结束，`s` 以 `sret` 结束；两者都保存 `a0/a1`（handler 用到了它们），栈帧 16 字节。把 `g = 1` 换成 `f()`（普通调用）后，帧变成 288 字节并保存 `ra`、`t0-t6`、`a0-a7`、`ft0-ft11`、`fa0-fa7`。

### 实验 4：`-mrvv-vector-bits=zvl` 对 `__riscv_v_min_vlen` 的影响
```bash
for A in "rv64gcv" "rv64gcv -mrvv-vector-bits=zvl" "rv64gcv_zvl256b"; do
  echo "=== $A ==="
  "$T/bin/clang" --target=riscv64-unknown-elf -march=$A -dM -E -x c /dev/null \
    | grep -E '__riscv_v_(min_vlen|fixed_vlen|intrinsic)'
done
```

实测：

```text
=== rv64gcv ===
#define __riscv_v_intrinsic 1000000
#define __riscv_v_min_vlen 128
=== rv64gcv -mrvv-vector-bits=zvl ===
#define __riscv_v_fixed_vlen 128
#define __riscv_v_intrinsic 1000000
#define __riscv_v_min_vlen 128
=== rv64gcv_zvl256b ===
#define __riscv_v_intrinsic 1000000
#define __riscv_v_min_vlen 256
```

结论：`zvl` 只加 `fixed_vlen`，不改 `min_vlen`；要改 `min_vlen` 必须改 `-march` 的 `zvl*b`。把 AVL 写成 `__riscv_v_fixed_vlen/8` 时，`zvl` 下 IR 从 `vscale_range(2,1024)` 变成 `(2,2)`，汇编从 `vsetvli a0, zero` 变成 `vsetivli zero, 16`。

### 实验 5：`#ifdef __riscv_vector` 编译开关
```bash
cat > /tmp/vsw.c <<'EOF'
extern int r;
#ifdef __riscv_vector
const int mode = 1;
#else
const int mode = 0;
#endif
EOF
for M in rv64gc rv64gcv; do
  echo "=== $M ==="
  "$T/bin/clang" --target=riscv64-unknown-elf -march=$M -S -emit-llvm /tmp/vsw.c -o - | grep '@mode'
done
```

实测 `rv64gc` 输出 `@mode = ... i32 0`，`rv64gcv` 输出 `@mode = ... i32 1`。同时验证：`-march=rv64i` 也有 `__riscv_v_intrinsic 1000000`，所以用 `#ifdef __riscv_v_intrinsic` 当 V 开关会永远走真分支。

### 实验 6：`__builtin_riscv_*` 正例与 feature 负例
```bash
cat > /tmp/b.c <<'EOF'
unsigned a(unsigned x) { return __builtin_riscv_clz_32(x); }
unsigned b(unsigned x) { return __builtin_riscv_orc_b_32(x); }
void c(void) { __builtin_riscv_pause(); }
EOF
"$T/bin/clang" --target=riscv64-unknown-elf -march=rv64gc_zbb_zihintpause -O2 -S /tmp/b.c -o -
"$T/bin/clang" --target=riscv64-unknown-elf -march=rv64gc -O2 -S /tmp/b.c -o /dev/null
```

`-march=rv64gc_zbb_zihintpause` 实测 `clzw a0,a0`、`orc.b a0,a0; sext.w a0,a0` 与 `pause`；去掉 `zbb` 后报：

```text
error: '__builtin_riscv_clz_32' needs target feature zbb|xtheadbb
```

`__builtin_riscv_pause` 需要 `zihintpause`，输出单条 `pause`；另测通用 `__builtin_thread_pointer()` 生成 `mv a0, tp`，写 `__builtin_riscv_thread_pointer` 会报 `use of unknown builtin`。

### 实验 7：非法 attribute 的报错原文
```bash
cat > /tmp/irqbad.c <<'EOF'
__attribute__((interrupt("bogus"))) void a(void) {}
__attribute__((interrupt("machine","supervisor"))) void b(void) {}
__attribute__((interrupt("machine"))) int d(int x) { return x; }
__attribute__((interrupt("rnmi"))) void r(void) {}
EOF
"$T/bin/clang" --target=riscv64-unknown-elf -march=rv64gc -S /tmp/irqbad.c -o /dev/null
```

实测：

```text
warning: 'interrupt' attribute argument not supported: "bogus" [-Wignored-attributes]
error: RISC-V 'interrupt' attribute contains invalid combination of interrupt types
warning: RISC-V 'interrupt' attribute only applies to functions that have no parameters [-Wignored-attributes]
error: RISC-V 'interrupt' attribute 'rnmi' requires extension 'Smrnmi'
```

注意 `bogus` 只是 warning（attribute 被忽略），而 `machine+supervisor` 是 error；负例测试要断言级别，不要只 grep 文本。

### 实验 8：RVV intrinsic 与 sizeless 类型
```bash
cat > /tmp/rvv.c <<'EOF'
#include <riscv_vector.h>
vint8m1_t f(vint8m1_t a, vint8m1_t b, size_t vl) {
  return __riscv_vadd(a, b, vl);
}
EOF
"$T/bin/clang" --target=riscv64-unknown-elf -march=rv64gcv -mabi=lp64d -O2 -S /tmp/rvv.c -o -
```

实测：

```asm
	vsetvli	zero, a0, e8, m1, ta, ma
	vadd.vv	v8, v8, v9
	ret
```

把 `vint8m1_t` 放到全局或对它取 `sizeof`，分别得到 `non-local variable with sizeless type` 和 `invalid application of 'sizeof' to sizeless type`。要固定长度，写 `typedef __rvv_int8m1_t v8 __attribute__((riscv_rvv_vector_bits(128)));` 且命令行必须带 `-mrvv-vector-bits=128` 或 `zvl`。

---

## 25.12 踩雷集锦

1. **宏和 `-march` 不同步，通常不是循环漏了，而是手写条件漏了。** `__riscv_zba` 由扩展循环自动生成；`__riscv_atomic`、`__riscv_compressed`、`__riscv_vector` 是手写条件。加扩展后先跑 `-dM`，再读 `getTargetDefines`。
2. **`__riscv_v_intrinsic` 不是 V 扩展开关。** 本树在 `rv64i` 上也定义 `1000000`；用 `#ifdef` 判断 V 会永远成立，正确开关是 `__riscv_vector`。
3. **`interrupt` + `naked` 不报错，但 `naked` 赢。** 实测 `__attribute__((naked, interrupt("machine")))` 生成的函数没有 prologue/epilogue，也没有寄存器保存，只剩你写的 `#APP mret #NO_APP`。要自己写完整保存/恢复。
4. **`lp64d` 的 `long double` 仍是 128 位，而且在 GPR 对里。** 不要因为 `__riscv_flen=64` 就以为它是 64 位，也不要指望它走 `fa0`；`__addtf3` 是软浮点。
5. **`__int128` 在 RV32 不存在。** `error: __int128 is not supported on this target`；不要用“RV32 也能编译”作为跨平台假设。
6. **`-mrvv-vector-bits` 不改类型。** 它只改 vscale 和 codegen；`__rvv_*_t` 永远 sizeless，固定长度要 `riscv_rvv_vector_bits` 且命令行带 flag。
7. **builtin 名字大小写敏感，且没有 `__builtin_riscv_thread_pointer`。** 线程指针用通用的 `__builtin_thread_pointer`；RISC-V 专用 builtin 的前缀是 `__builtin_riscv_`，RVV 是 `__builtin_rvv_`。
8. **`supervisor` interrupt 不要求 S 扩展。** Sema 只对 `rnmi`/`qci-*`/`SiFive-CLIC-*` 查扩展；`-march=rv64gc` 也能生成 `sret`。如果目标真的没有 S，错误要由你来暴露，不能指望前端。

---

## 25.13 本章重点整理

- **TargetInfo 是“用户可见面”的总闸**：宏、类型宽度、attribute、builtin、原子宽度都在这里；后端 `llc -mattr` 绕过它，所以前端改动必须单独测。`-march` 的路径是 driver → cc1 `-target-feature` → `initFeatureMap` / `handleTargetFeatures` 两次 `parseFeatures` → `getTargetDefines`。
- **扩展宏自动生成**（`__riscv_zba`），语义宏手写（`__riscv_atomic`、`__riscv_compressed`、`__riscv_vector`、`__riscv_v_intrinsic`）。
- **`__riscv_v_intrinsic` 是无条件版本宏**（1.0 = 1000000），不是 V 开关；V 开关是 `__riscv_vector`（`zve32x`）。`__riscv_v_min_vlen` 来自 `-march` 的 `zvl*b`；`-mrvv-vector-bits` 只改 vscale / `__riscv_v_fixed_vlen` / codegen。
- **`long double` = 128 位 IEEE quad**，`lp64d` 下也走整数对 + `__addtf3`；`__int128` 仅 RV64；`char` 无符号；`max_align_t` 32 字节 / 16 对齐。
- **`interrupt` 全链**：`Attr.td` → `SemaDeclAttr.cpp:6738` → `SemaRISCV.cpp:1644` → `CodeGen/Targets/RISCV.cpp:1128` → `"interrupt"` 函数属性 → `RISCVISelLowering.cpp:29650` 选 `mret`/`sret`/`mnret`；没有独立 CC，保存集由 `CSR_*_Interrupt_SaveList` 决定。
- **`__builtin_riscv_*` 是 459 个 TableGen 记录**，feature 不满足报 `needs target feature`；RVV 是 3475 个 `__builtin_rvv_*` 枚举，头文件是生成物，intrinsic 声明由 `#pragma clang riscv intrinsic vector` + `RISCVIntrinsicManager` 按需构造。

---

## 25.14 自我检核

- [ ] 能画出 `-march=rv64gcv` → `RISCVISAInfo` → `handleTargetFeatures` → `__riscv_vector` / `__riscv_v_min_vlen` → CodeGen 的完整路径
- [ ] 能说出 `__riscv_zba` 为什么自动出现，而 `__riscv_atomic` 为什么要手写条件
- [ ] 能用 `clang -dM -E -x c /dev/null` 对比 `rv64gc` / `rv64gcv` / `rv64gc_zba` 并解释每一行差异
- [ ] 能解释 `long double` 在 `lp64d` 下为什么是 128 位、为什么走 `a1/a2` 而不是 `fa0`
- [ ] 能复现 `__int128` 在 RV32 的报错，并说出 `max_align_t` 的大小/对齐
- [ ] 能背出 `interrupt` 的 Sema → CodeGen → 后端返回指令三段，并解释 `mret` / `sret` / `mnret`
- [ ] 能解释 `naked + interrupt` 为什么不报错、实际生成了什么
- [ ] 能说明 `__riscv_v_intrinsic` 与 `__riscv_vector` 的区别，以及 `-mrvv-vector-bits` 到底改了什么
- [ ] 能写出 `__builtin_riscv_clz_32` 的正例和缺 `zbb` 的负例，并指出 `riscv_vector.h` 是生成物、名字查找在 `SemaLookup.cpp:962`

---

## 25.15 延伸阅读

- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Basic/Targets/RISCV.cpp`**：读 143–257（宏）、342–370（`initFeatureMap`）、373–394（`getVScaleRange`）、397–438（`hasFeature` / `handleTargetFeatures`）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Basic/Targets/RISCV.h`**：读 38–52（`LongDouble` / `WCharType` / `HasRISCVVTypes`）、150–226（32/64 位 ABI 与原子宽度）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/CodeGen/Targets/RISCV.cpp`**：读 131–190（`computeInfo`）、739–900（`classifyArgumentType` / `classifyReturnType`）、1120–1180（`interrupt` 属性映射）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/lib/Sema/SemaRISCV.cpp`**：读 1644–1791（interrupt 校验）、212–532（RVV intrinsic manager）。
- **`/home/ubuntu/llvm/trees/ct-instr-trunk/clang/include/clang/Basic/BuiltinsRISCV.td`** 与 **`Builtins.td:1232`**：前者是 RISC-V 专用 builtin 的完整清单，后者解释 `__builtin_thread_pointer` 为什么不在 RISC-V 文件里；**`riscv_vector.td`** / **`riscv_vector_common.td`** 看 `defm vadd` 如何展开成 3475 个枚举。返回指令与保存集见 **`RISCVISelLowering.cpp:29650-29672`**、**`RISCVCallingConv.td:49-73`**。

→ 下一章：[Ch 26 — compiler-rt、libc 与 qemu](./26-compiler-rt-libc-qemu.md)
