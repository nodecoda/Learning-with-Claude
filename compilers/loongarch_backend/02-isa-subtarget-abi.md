# Ch 02 — ISA、Subtarget 与 ABI：`-march`/`-mabi` 到底改变了什么

> 本章目标：把“LoongArch 是什么”拆成四层可检查的事实：ISA 版本（LA32/LA64）、Feature、CPU、ABI。读完你能解释 `-march=la664 -mabi=lp64d` 与 `-march=loongarch64 -mabi=lp64s` 在编译器内部到底改了哪些位。

---

## 2.1 两个版本、两个子版本

`LoongArch.td` 一开头就把版本写死了：

```tablegen
// LoongArch is divided into two versions, the 32-bit version (LA32) and the
// 64-bit version (LA64).

def Feature32S
    : SubtargetFeature<"32s", "Has32S", "true",
                       "LA32 Standard Basic Instruction Extension">;
def Feature64Bit
    : SubtargetFeature<"64bit", "HasLA64", "true",
                       "LA64 Basic Integer and Privilege Instruction Set",
                       [Feature32S]>;
def Feature32Bit
    : SubtargetFeature<"32bit", "HasLA32", "true",
                       "LA32 Basic Integer and Privilege Instruction Set">;
```

要点：

- **LA64 隐含 32S。** `Feature64Bit` 的依赖列表是 `[Feature32S]`，所以 `+64bit` 自动带来 `+32s` 的指令（`revb`/`bitrev` 等）。
- **LA32 分 LA32R 和 LA32S。** `32s` 是“标准 32 位”，LA32R 是精简版；`has32S()` 决定 `BITREVERSE`、`BSWAP` 等操作是 `Legal` 还是 `Expand`。
- **`HasLA32 == HasLA64` 会 fatal。** `LoongArchSubtarget::initializeSubtargetDependencies()` 里：

  ```cpp
  if (HasLA32 == HasLA64)
    report_fatal_error("Please use one feature of 32bit and 64bit.");
  if (Is64Bit && HasLA32)
    report_fatal_error("Feature 32bit should be used for loongarch32 target.");
  if (!Is64Bit && HasLA64)
    report_fatal_error("Feature 64bit should be used for loongarch64 target.");
  ```

  这解释了为什么 `llc -mtriple=loongarch64 -mattr=+32bit` 会直接报错，而不是“悄悄按 32 位生成”。

---

## 2.2 Feature 全景

`LoongArch.td` 里定义的 `SubtargetFeature`（实测 `grep`）：

| Feature | 名称 | 含义 | 依赖 |
|---|---|---|---|
| `Feature32S` | `+32s` | LA32 标准指令 | — |
| `Feature64Bit` | `+64bit` | LA64 基础整数/特权指令 | `32s` |
| `Feature32Bit` | `+32bit` | LA32 基础指令 | — |
| `FeatureBasicF` | `+f` | 单精度浮点 | — |
| `FeatureBasicD` | `+d` | 双精度浮点 | `f` |
| `FeatureExtLSX` | `+lsx` | 128 位 SIMD | `d` |
| `FeatureExtLASX` | `+lasx` | 256 位 SIMD | `lsx` |
| `FeatureExtLVZ` | `+lvz` | 虚拟化 | — |
| `FeatureExtLBT` | `+lbt` | 二进制翻译 | — |
| `FeatureUAL` | `+ual` | 允许非对齐访存 | — |
| `FeatureRelax` | `+relax` | 链接器 relaxation | — |
| `FeatureFrecipe` | `+frecipe` | `frecipe.{s/d}`/`frsqrte.{s/d}` | — |
| `FeatureLAM_BH` | `+lam-bh` | 字节/半字原子 swap/add | — |
| `FeatureLAMCAS` | `+lamcas` | 字节/半字/字/双字原子 CAS | — |
| `FeatureLD_SEQ_SA` | `+ld-seq-sa` | 不需要同地址 load-load barrier | — |
| `FeatureDiv32` | `+div32` | `div.w[u]`/`mod.w[u]` 可处理非符号扩展输入 | — |
| `FeatureSCQ` | `+scq` | `sc.q` | — |
| `TunePreferWInst` | `+prefer-w-inst` | 优先 `W` 后缀 | — |
| `TuneSlowMOVCF2GR` | `+slow-movcf2gr` | `movcf2gr` 较慢 | — |

Feature 不是“编译器开关”那么简单：它们直接控制：

- **寄存器类是否注册**：`hasBasicF()` → `FPR32`，`hasBasicD()` → `FPR64`，`hasExtLSX()` → `LSX128`，`hasExtLASX()` → `LASX256`。
- **操作的 legal 性**：`has32S()` 决定 `BITREVERSE`/`BSWAP`；`isSoftFPABI()` 决定 `FP16_TO_FP` 是 `Custom` 还是 `LibCall`。
- **指令谓词**：`.td` 里的 `Requires<[HasExtLSX]>` 会在汇编器/反汇编器/AsmPrinter 三处统一检查。
- **调度模型**：CPU 选择不同 `ProcessorModel`。

---

## 2.3 CPU 表

实测 `LoongArch.td`：

| CPU | 调度模型 | Feature 集合 | 说明 |
|---|---|---|---|
| `generic-la32` | `NoSchedModel` | `32bit` | LA32 默认 |
| `generic-la64` | `NoSchedModel` | `64bit, ual, lsx` | LA64 默认；注意默认带 LSX |
| `loongarch32` | `NoSchedModel` | `32bit` | 兼容名 |
| `loongarch64` | `NoSchedModel` | `64bit, ual, d` | 兼容名；不带 LSX |
| `generic` | `NoSchedModel` | 空 | 由 triple 决定 32/64 |
| `la464` | `LA464Model` | `64bit, ual, lasx, lvz, lbt` + `TuneSlowMOVCF2GR` | 3A5000 一代 |
| `la664` | `LA664Model` | `64bit, ual, lasx, lvz, lbt, frecipe, lam-bh, lamcas, ld-seq-sa, div32, scq` | 3A6000 一代 |

两个容易踩的坑：

1. **`-march=loongarch64` 不等于 `-march=generic-la64`。** 前者只带 `d`，后者带 `lsx`。如果你写向量测试却只给 `-march=loongarch64`，会得到“标量展开”而不是 LSX 指令。
2. **`la464`/`la664` 的 `TuneSlowMOVCF2GR` 只影响 `la464`。** `la664` 的 `movcf2gr` 被认为够快，所以没有这个 Tune feature；这会影响 `MOVCF2GR` vs `MOVCF2FR+MOVFR2GR` 的选择。

### Subtarget 的对齐/prefetch 默认值

`LoongArchSubtarget::initializeProperties()`（实测注释）：

```cpp
PrefFunctionAlignment = Align(32);
PrefLoopAlignment = Align(16);
MaxBytesForAlignment = 16;

PrefetchDistance = 0;
MinPrefetchStride = 1;
MaxPrefetchIterationsAhead = UINT_MAX;

if (TuneCPU == "la464" || TuneCPU == "la664") {
  PrefetchDistance = 200;
  MinPrefetchStride = 2048;
}
```

这解释了为什么 `.s` 里会看到 `.prefalign 5`（32 字节）和 `LoopDataPrefetch` 只在 LA464/LA664 上启用（Ch 03 的 pass 列表里有 `loop-data-prefetch`）。

---

## 2.4 ABI：四种基础组合 + 两种浮点变体

`LoongArchBaseInfo.h`：

```cpp
namespace LoongArchABI {
enum ABI {
  ABI_ILP32S, ABI_ILP32F, ABI_ILP32D,
  ABI_LP64S,  ABI_LP64F,  ABI_LP64D,
  ABI_Unknown
};
}
```

| ABI | 指针/长整型 | 浮点参数 | 典型场景 |
|---|---|---|---|
| `ilp32s` | 32 位 | 软浮点 | LA32 无 FPU |
| `ilp32f` | 32 位 | 单精度寄存器 | LA32 + F |
| `ilp32d` | 32 位 | 双精度寄存器 | LA32 + D |
| `lp64s` | 64 位 | 软浮点 | LA64 无 FPU |
| `lp64f` | 64 位 | 单精度寄存器 | LA64 + F |
| `lp64d` | 64 位 | 双精度寄存器 | LA64 + D（最常见） |

### 2.4.1 Clang driver 的优先级

`clang/lib/Driver/ToolChains/Arch/LoongArch.cpp` 的 `getLoongArchABI()`：

1. **`-mdouble-float` / `-msingle-float` / `-msoft-float` 优先级最高**，会直接覆盖 `-mabi`/`-mfpu`（冲突时只 warning）。
2. 然后看 **`-mabi=`**。
3. 然后看 **`-mfpu=64/32/0`**。
4. 然后看 **triple 的 environment**：
   - `GNUSF`/`MuslSF` → `*s`
   - `GNUF32`/`MuslF32` → `*f`
   - `GNUF64`/默认 → `*d`
5. 如果 triple 没有 environment，则按 `-march` 的 FPU 能力选“最宽的 ABI”（最近的 #231441 行为）。

> 注意：`-mabi=lp64f` 和 `-mabi=ilp32f` 在 LLVM 里会打印 `has not been standardized` warning（`checkABIStandardized()`）。这是真实的源码行为，不是你的命令写错。

### 2.4.2 后端怎么算 ABI

`LoongArchABI::computeTargetABI(TT, FeatureBits, ABIName)`（`MCTargetDesc/LoongArchBaseInfo.cpp`）：

- 先 `getTargetABI(ABIName)` 解析显式名字；
- 再 `getTripleABI(TT)` 根据 environment 推；
- 如果显式 ABI 和 triple/feature 冲突，会报错或 warning；
- 最后 `checkABIStandardized()` 决定是否 warning。

### 2.4.3 ABI 落到 ELF：`e_flags`

`LoongArchELFStreamer.cpp` 把 ABI 映射成：

```cpp
case LoongArchABI::ABI_LP64S:
  EFlags |= ELF::EF_LOONGARCH_ABI_SOFT_FLOAT;
case LoongArchABI::ABI_LP64F:
  EFlags |= ELF::EF_LOONGARCH_ABI_SINGLE_FLOAT;
case LoongArchABI::ABI_LP64D:
  EFlags |= ELF::EF_LOONGARCH_ABI_DOUBLE_FLOAT;
```

实测 `llvm-readobj -h /tmp/la-smoke.o`：

```text
Flags: EF_LOONGARCH_ABI_DOUBLE_FLOAT | EF_LOONGARCH_OBJABI_V1
```

`EF_LOONGARCH_OBJABI_V1` 是对象 ABI 版本；`EF_LOONGARCH_ABI_*` 是浮点 ABI。链接器会用它们检查输入对象是否兼容（`lld` 的 `calcEFlags()` 会合并/校验）。

---

## 2.5 寄存器表

`LoongArchRegisterInfo.td` 实测：

| 编号 | 汇编名 | ABI 名 | 用途 | 是否 callee-saved |
|---:|---|---|---|---|
| 0 | `$r0` | `$zero` | 常量 0 | — |
| 1 | `$r1` | `$ra` | 返回地址 | 是（`CSR_*` 含 `R1`） |
| 2 | `$r2` | `$tp` | TLS 指针 | 否（保留） |
| 3 | `$r3` | `$sp` | 栈指针 | 否（保留） |
| 4–11 | `$r4`–`$r11` | `$a0`–`$a7` | 参数/返回值 | 否 |
| 12–20 | `$r12`–`$r20` | `$t0`–`$t8` | 临时 | 否 |
| 21 | `$r21` | — | 保留 | 否 |
| 22 | `$r22` | `$fp`/`$s9` | 帧指针 | 是 |
| 23–31 | `$r23`–`$r31` | `$s0`–`$s8` | 保存寄存器 | 是 |

浮点/向量：

| 名称 | 数量 | 说明 |
|---|---:|---|
| `$f0`–`$f31` | 32 | 32 位 FP 寄存器 |
| `$f0_64`–`$f31_64` | 32 | 64 位视图（`FPR64`） |
| `$fcc0`–`$fcc7` | 8 | 条件码寄存器（`CFR`） |
| `$fcsr0`–`$fcsr3` | 4 | FP 控制/状态 |
| `$scr0`–`$scr3` | 4 | 向量 scratch（`SCR`） |

寄存器类（`LoongArchRegisterInfo.td`）：

- `GPR`：分配顺序是 caller-save（`a0-a7`, `t0-t8`）→ callee-save（`s0-s8`, `fp`）→ 特殊；
- `GPRT`：只含 caller-save；
- `GPRJR`：`GPR - R1`（用于 `jirl` 目标不能用 `$ra` 的场景）；
- `GPRNoR0R1`：去掉 `$r0`/`$r1`；
- `FPR32`/`FPR64`/`CFR`/`FCSR`/`LSX128`/`LASX256`/`SCR`。

callee-saved 集合在 `LoongArchCallingConv.td`：

```tablegen
def CSR_ILP32S_LP64S : CalleeSavedRegs<(add R1, (sequence "R%u", 22, 31))>;
def CSR_ILP32F_LP64F : CalleeSavedRegs<(add CSR_ILP32S_LP64S, (sequence "F%u", 24, 31))>;
def CSR_ILP32D_LP64D : CalleeSavedRegs<(add CSR_ILP32S_LP64S, (sequence "F%u_64", 24, 31))>;
```

注意：`$f24`–`$f31` 是 callee-saved 的浮点寄存器；`$f0`–`$f23` 是 caller-save。这会影响 prologue 里到底要存哪些寄存器（Ch 10）。

---

## 2.6 代码模型

`LoongArchTargetMachine.cpp` 的 `getEffectiveLoongArchCodeModel()`：

```cpp
if (!CM)
  return TT.isArch64Bit() ? CodeModel::Medium : CodeModel::Small;

switch (*CM) {
case CodeModel::Small:
case CodeModel::Medium:
  return *CM;
case CodeModel::Large:
  if (!TT.isArch64Bit())
    report_fatal_error("Large code model requires LA64");
  return *CM;
default:
  report_fatal_error("Only small, medium and large code models are allowed on LoongArch");
}
```

| 模型 | 地址加载方式 | 限制 |
|---|---|---|
| `small` | `pcalau12i + addi` / `la.pcrel` | 代码/数据在 ±2GB |
| `medium`（LA64 默认） | `pcalau12i + addi` / `la.pcrel`，必要时 `lu32i.d/lu52i.d` | 更大的地址范围 |
| `large` | 5 指令序列（`pcalau12i + addi + lu32i.d + lu52i.d + add.d`） | 仅 LA64 |

代码模型直接改变 `PseudoLA_*` 的展开（Ch 11/18）。同一个全局变量，在 `small` 下是两条指令，在 `large` 下是五条。

---

## 2.7 DataLayout

`llvm/lib/TargetParser/TargetDataLayout.cpp`：

```cpp
static std::string computeLoongArchDataLayout(const Triple &TT) {
  if (TT.isLoongArch64())
    return "e-m:e-p:64:64-i64:64-i128:128-n32:64-S128";
  return "e-m:e-p:32:32-i64:64-n32-S128";
}
```

| 字段 | LA64 | LA32 |
|---|---|---|
| 指针 | 64 位，对齐 64 | 32 位，对齐 32 |
| i64 | 64 位，对齐 64 | 64 位，对齐 64（注意：32 位目标上 i64 仍 8 字节对齐） |
| i128 | 128 位，对齐 128 | 未声明，按默认 |
| 原生整数宽度 | 32/64 | 32 |
| 栈对齐 | 128 | 128 |

`i128` 在 LA64 上存在，意味着 `__int128` 是合法的；这会影响合法化（Ch 13）和 ABI（大整数参数怎么传）。

---

## 2.8 一条命令看全部

```bash
T=/home/ubuntu/llvm/builds/ct-instr-trunk
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx,+frecipe -mcpu=la664 \
  -print-after=loongarch-isel /tmp/la-smoke.ll -o /dev/null 2>&1 | head -20
```

最可靠的 feature 对照是看 **CodeGen 输出**，而不是只看汇编器是否接受：

```llvm
define <8 x i32> @load8(ptr %p) {
  %v = load <8 x i32>, ptr %p, align 32
  ret <8 x i32> %v
}
```

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lasx /tmp/v8.ll -o - | grep -E 'xvld|vld'
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -mattr=+lsx,-lasx /tmp/v8.ll -o - | grep -E 'xvld|vld'
```

实测：

```text
# +lasx
	xvld	$xr0, $a0, 0
# +lsx,-lasx
	vld	$vr0, $a0, 0
	vld	$vr1, $a0, 16
```

同一个 `v8i32` load：有 LASX 是一条 `xvld`，没有 LASX 被拆成两条 128 位 `vld`。这就是“寄存器类 + 合法化 + pattern”三层一起作用的结果。

> 一个容易误解的点：本树里 LASX 指令用的是 `Predicates = [HasExtLASX]`（CodeGen/调度用），不是 `AssemblerPredicate`；实测 `llvm-mc -mattr=-lasx` 仍然接受 `xvld`。所以**不要用 `llvm-mc` 是否报错来判断 feature 是否生效**，要看 `llc` 的输出或 `-show-encoding` 的指令选择。

### 2.8.1 用 `-###` 看 driver 到底传了什么

```bash
$T/bin/clang --target=loongarch64-unknown-linux-gnu -march=la664 -mabi=lp64d \
  -O2 -### -c /tmp/la.c 2>&1 | grep -E 'target-cpu|target-feature|target-abi'
```

实测（节选）：

```text
"-target-cpu" "la664"
"-target-feature" "+relax" "-target-feature" "+64bit"
"-target-feature" "+f" "-target-feature" "+d"
"-target-feature" "+lsx" "-target-feature" "+lasx"
"-target-feature" "+ual" "-target-feature" "+frecipe"
"-target-feature" "+lam-bh" "-target-feature" "+lamcas"
"-target-feature" "+ld-seq-sa" "-target-feature" "+div32"
"-target-feature" "+scq"
"-target-abi" "lp64d"
```

这张表就是 `la664` 的 Feature 集合在 driver 层的展开；它和后端 `LoongArch.td` 的 `ProcessorModel<"la664", ...>` 必须保持一致。改 CPU feature 时，通常要同时改 `.td` 和 clang 的 `TargetInfo`/driver 测试。

`-mabi=lp64f` 的实测诊断：

```text
warning: triple-implied ABI conflicts with provided target-abi 'lp64f', using target-abi
warning: 'lp64f' has not been standardized
```

这验证了 2.4.1 的“显式 ABI 优先、非标准 ABI warning”行为。

---

## 2.9 本章证据

| 结论 | 源码/命令 |
|---|---|
| LA64 隐含 32S | `LoongArch.td` 的 `Feature64Bit` 依赖 `[Feature32S]` |
| 32/64 冲突 fatal | `LoongArchSubtarget.cpp` 的 `initializeSubtargetDependencies()` |
| CPU/Feature 表 | `LoongArch.td` 的 `ProcessorModel` |
| ABI 枚举 | `MCTargetDesc/LoongArchBaseInfo.h` |
| ABI 优先级 | `clang/lib/Driver/ToolChains/Arch/LoongArch.cpp` |
| e_flags | `MCTargetDesc/LoongArchELFStreamer.cpp` + `llvm-readobj -h` |
| 寄存器/CSR | `LoongArchRegisterInfo.td`、`LoongArchCallingConv.td` |
| 代码模型 | `LoongArchTargetMachine.cpp` |
| DataLayout | `llvm/lib/TargetParser/TargetDataLayout.cpp` |

---

## 2.10 练习

1. 用 `llc -mattr=+32s` 和 `-mattr=-32s` 编译 `llvm/test/CodeGen/LoongArch/bitreverse.ll`，解释 `revb`/`bitrev` 的差异。
2. 构造 `-mabi=lp64f` 的编译，观察 warning，并检查 `.o` 的 `e_flags`。
3. 用 `-mattr=+lasx` 编一个 `v8i32` 的 IR，确认寄存器类变成 `LASX256`；去掉 `+lasx` 后看它怎么被拆成 LSX 或标量。
4. 读 `getEffectiveLoongArchCodeModel()`，解释为什么 LA32 不允许 `large`。

---

## 2.11 下一步

下一章把“编译一次”拆成真实的 pass 序列：`llc -debug-pass=Structure` 的每一行是什么、`-stop-after` 停在哪、MIR 里怎么看 `PseudoLA_TLS_LE`。
