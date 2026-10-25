# Ch 4 — AArch64 的 GPR / FPR / CC bank（RISC-V 对照）

> **目标**：把 AArch64 的三个 bank 落到 TableGen 与源码上：它们覆盖哪些 regclass、能装多少位、跨 bank 拷贝为什么两个方向是 4 与 5（以及 `GPR→FPR=5` 这种常见写法为什么会把方向说反）。重点纠正一个高频误解：**普通 AArch64 GlobalISel 路径上，布尔值不在 CC bank**。最后用 RISC-V 的 GPRB / FPRB / VRB 做对照。
>
> **环境**：LLVM 23.1.4 官方预编译包（MIR 实测）；TD 与 C++ 行来自 `llvm-23.1.4-src`（源码依据）。RISC-V 使用 `-mattr=+f`。
>
> **证据约定**：`copyCost` 的 5/4 无法在官方包上直接观测（需要 `-debug-only`，官方包没有），标为源码依据；CC 不被用作布尔 bank 的结论是“实测 MIR + 测试集检索”的有限结论，本章会明确它的适用范围。

## 为什么需要这个？

调 bank 时，90% 的问题最后都落在这三句话上：

- “这个值为什么在 GPR 而不是 FPR？”
- “这几条 COPY 是从哪冒出来的，为什么这么多？”
- “比较结果不是应该在 CC 里吗？”

前两个问题需要知道每个 bank 覆盖什么、代价怎么算；第三个问题则是一个流传很广的误解。答案很干脆：AArch64 的 CC bank 只覆盖物理的 `$nzcv`，它不是“布尔值的默认 bank”。`G_ICMP` / `G_FCMP` 的结果在 RegBankSelect 阶段是 **GPR 里的 i32**，直到 InstructionSelect 才通过 `CSINC` 之类指令把 flag 物化成整数值。

本章还会顺带解释一个设计取舍：AArch64 只有三个 bank，向量被塞进 FPR；RISC-V 也有三个，但向量单独一个 VRB。两种切法各有代价。

## 先建立直觉

把三个 bank 想成三个园区：

```text
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
   │  GPR 园区     │   │  FPR 园区     │   │  CC 园区      │
   │ 整数/指针/地址 │   │ 浮点/向量/SVE │   │ 只有 $nzcv    │
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
          │  过桥 5 块         │                 │
          ├──────────────────▶│                 │  没有桥
          │◀──────────────────┤                 │  （不能 COPY）
          │  过桥 4 块         │                 │
          └───────────────────┘                 └──────────────┘
```

- 桥的收费**不对称**：GPR→FPR 收 4，FPR→GPR 收 5（按 `copyCost(A, B)` 的 A=目的、B=源语义）。这不是笔误；常见资料把 5/4 写成 GPR→FPR/FPR→GPR，是把参数顺序当成了方向。
- CC 园区没有桥：`CCR` 这个 regclass 的 `CopyCost = -1`，意思是“禁止拷贝状态寄存器”。
- 三个园区的大小也不一样：GPR 128 位（靠寄存器对）、FPR 512 位（靠 QQQQ）、CC 32 位。

## 核心概念

### 三个 bank 的 TableGen 定义

```tablegen
// llvm/lib/Target/AArch64/AArch64RegisterBanks.td:12-19
/// General Purpose Registers: W, X.
def GPRRegBank : RegisterBank<"GPR", [XSeqPairsClass]>;

/// Floating Point, Vector, Scalable Vector Registers: B, H, S, D, Q, Z.
def FPRRegBank : RegisterBank<"FPR", [QQQQ, ZPR]>;

/// Conditional register: NZCV.
def CCRegBank : RegisterBank<"CC", [CCR]>;
```

| bank | 覆盖的关键 regclass | 最大位数 | 源码依据 |
| --- | --- | --- | --- |
| GPR | `XSeqPairsClass`（coverage 同时覆盖 `GPR32`/`GPR64`） | 128 | 构造函数断言 `getMaximumSize == 128` |
| FPR | `QQQQ`、`ZPR`（含 `FPR16/32/64/128`） | 512 | 断言 `== 512`，"via QQQQ sequence" |
| CC | `CCR`（唯一物理寄存器 `$nzcv`） | 32 | 断言 `== 32` |

这些断言在 `AArch64RegisterBankInfo.cpp:80-97`，是构造 `AArch64RegisterBankInfo` 时跑的（assertions build 才生效）。它们同时验证了两件事：覆盖关系（`RBGPR.covers(GPR32RegClassID)`）和尺寸上限。

`XSeqPairsClass` 本身是寄存器对类：

```tablegen
// llvm/lib/Target/AArch64/AArch64RegisterInfo.td:812-815
def XSeqPairsClass   : RegisterClass<"AArch64", [untyped], 64,
                                     (add XSeqPairs)>{
  let Size = 128;
}
```

`QQQQ` 是四个 Q 的拼接：

```tablegen
// llvm/lib/Target/AArch64/AArch64RegisterInfo.td:591-594
def QQQQ : RegisterClass<"AArch64", [untyped], 128, (add QSeqQuads)> {
  let Size = 512;
}
```

所以“GPR 能装 128 位”说的是“两个 64 位寄存器拼起来可以表示 128 位值”，不是“有一个 128 位的通用寄存器”。

### 跨 bank 代价：5 与 4

```cpp
// llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp:226-245
unsigned AArch64RegisterBankInfo::copyCost(const RegisterBank &A,
                                           const RegisterBank &B,
                                           const TypeSize Size) const {
  // What do we do with different size?
  // copy are same size.
  // Will introduce other hooks for different size:
  // * extract cost.
  // * build_sequence cost.

  // Copy from (resp. to) GPR to (resp. from) FPR involves FMOV.
  // FIXME: This should be deduced from the scheduling model.
  if (&A == &AArch64::GPRRegBank && &B == &AArch64::FPRRegBank)
    // FMOVXDr or FMOVWSr.
    return 5;
  if (&A == &AArch64::FPRRegBank && &B == &AArch64::GPRRegBank)
    // FMOVDXr or FMOVSWr.
    return 4;

  return RegisterBankInfo::copyCost(A, B, Size);
}
```

三个要点：

1. **方向敏感**。`A` 是目的，`B` 是源（`RegisterBankInfo.h:634-637`：`A = COPY B`）。于是 `copyCost(GPR, FPR)`（FPR→GPR）走 `FMOVXDr`/`FMOVWSr`，代价 5；`copyCost(FPR, GPR)`（GPR→FPR）走 `FMOVDXr`/`FMOVSWr`，代价 4。两个方向的指令不同，代价也不同。注意 `GPR→FPR=5` 的常见写法是按参数顺序读的，语义方向恰好相反。
2. **只对 GPR↔FPR 特判**。其余情况落到基类：同 bank 返回 0，跨 bank 返回 1（`RegisterBankInfo.h:640-647`：`return &A != &B;`）。
3. **这是手工值**。注释里的 `FIXME: This should be deduced from the scheduling model` 说明它并非从调度模型推导，而是一个“够用”的经验值。上游 review 时这类常量通常要求有 benchmark 支撑。

为什么不对称？源码没有写理由。能确定的是：两个方向用的是不同的 FMOV 编码（`FMOVXDr` vs `FMOVDXr`），在多数微架构上延迟/端口不同。**除此之外的解释都是推测**，本章不编。

### CC bank 的特殊性

`CCR` 的 regclass 定义里有两个关键属性：

```tablegen
// llvm/lib/Target/AArch64/AArch64RegisterInfo.td:309-314
def CCR : RegisterClass<"AArch64", [FlagsVT], 32, (add NZCV)> {
  let CopyCost = -1;  // Don't allow copying of status registers.

  // CCR is not allocatable.
  let isAllocatable = 0;
}
```

- `CopyCost = -1`：不允许拷贝。状态寄存器不是普通值，复制它没有语义。
- `isAllocatable = 0`：寄存器分配不会把它分配给 vreg。

这两条在 bank 层的延伸是：**CC bank 没有 copy mapping**。

```cpp
// llvm/lib/Target/AArch64/AArch64GenRegisterBankInfo.def:217-234（节选）
const AArch64GenRegisterBankInfo::PartialMappingIdx
    AArch64GenRegisterBankInfo::BankIDToCopyMapIdx[]{
        PMI_None,     // CCR
        PMI_FirstFPR, // FPR
        PMI_FirstGPR, // GPR
    };
...
  assert(DstRBIdx != PMI_None && "No such mapping");
  assert(SrcRBIdx != PMI_None && "No such mapping");
```

`BankIDToCopyMapIdx[0]` 是 `PMI_None`，注释写着 `CCR`。所以任何试图生成“到 CC”或“从 CC”拷贝的代码都会撞上 `assert`（assertions build）或落到无效映射（release build）。

还有一个顺便的结论：**bank ID 的顺序是 CC=0、FPR=1、GPR=2**。TableGen 按名字排序生成，MIR 里打印的是名字，所以平时注意不到。写代码时不要假设“GPR 是 0 号”。

## 底层机制

### 为什么布尔值不进 CC

三个层次的原因，从下往上：

1. **Legalizer 先把 `i1` 扩成 `i32`**。AArch64 的寄存器没有 1 位的通用容器，`i1` 在寄存器里按 32 位处理。这一步发生在 RegBankSelect 之前。
2. **RegBankSelect 只能给“有 LLT 的 vreg”选 bank**。`G_ICMP` 的结果此时是 `i32`。AArch64 只在 `G_FCMP` 上显式把标量结果记成 `PMI_FirstGPR`（`AArch64RegisterBankInfo.cpp:1110-1117`）；`G_ICMP` 没有专门分支，走默认的约束推导（Ch 6），结果同样是 GPR（实测见 `cmp-regbankselect.mir`）。
3. **flags 是物理寄存器的副作用**。ISel 把 `G_FCMP` 选成 `FCMPSrr`，它 `implicit-def $nzcv`；再用 `CSINCWr ... implicit $nzcv` 把 flag 转成 GPR 里的 0/1。CC bank 在这里只作为**物理寄存器 `$nzcv` 的归属**出现，不参与 vreg 的 bank 选择。

一句话：**CC bank 描述的是“flags 住在哪”，不是“布尔值住在哪”。**

### RISC-V 的三个 bank

```tablegen
// llvm/lib/Target/RISCV/GISel/RISCVRegisterBanks.td:12-19
/// General Purpose Registers: X.
def GPRBRegBank : RegisterBank<"GPRB", [GPR, GPRPair]>;

/// Floating Point Registers: F.
def FPRBRegBank : RegisterBank<"FPRB", [FPR64]>;

/// Vector Registers : V.
def VRBRegBank : RegisterBank<"VRB", [VRM8]>;
```

| bank | 覆盖 | 特点 |
| --- | --- | --- |
| GPRB | `GPR`、`GPRPair` | 整数与指针；`GPRPair` 支持 128 位（RV64 下两个 x 寄存器） |
| FPRB | `FPR64` | 标量浮点；`f16/f32/f64` 都在这一个类里按子寄存器处理 |
| VRB | `VRM8` | 向量；`VRM8` 是 LMUL=8 的宽寄存器类（`V0M8`/`V8M8`/`V16M8`/`V24M8`） |

RISC-V **没有覆写 `copyCost`**（`RISCVRegisterBankInfo.cpp` 与 `.h` 里都搜不到 `copyCost`），所以跨 bank 拷贝用基类的 0/1。这与 AArch64 的 5/4 形成鲜明对比：同样是“搬一次值”，代价模型完全不同。

另一个差异是 LLT：RISC-V 的 `f32` 在 MIR 里写作 `s32`，AArch64 写作 `f32`。这直接影响你读 MIR 时的直觉。

## 示例一：布尔值在 GPR，flag 在 `$nzcv`（实测）

```llvm
define i32 @fcmp(float %a, float %b) {
entry:
  %c = fcmp olt float %a, %b
  %r = zext i1 %c to i32
  ret i32 %r
}

declare { i64, i1 } @llvm.uadd.with.overflow.i64(i64, i64)

define { i64, i1 } @uaddo(i64 %a, i64 %b) {
entry:
  %s = call { i64, i1 } @llvm.uadd.with.overflow.i64(i64 %a, i64 %b)
  ret { i64, i1 } %s
}
```

```text
; fcmp, -stop-after=regbankselect
%0:fpr(f32) = COPY $s0
%1:fpr(f32) = COPY $s1
%4:gpr(i32) = G_FCMP floatpred(olt), %0(f32), %1     ; ← 结果在 GPR
$w0 = COPY %4(i32)

; fcmp, -stop-after=instruction-select
%0:fpr32 = COPY $s0
%1:fpr32 = COPY $s1
nofpexcept FCMPSrr %0, %1, implicit-def $nzcv, implicit $fpcr
%4:gpr32 = CSINCWr $wzr, $wzr, 5, implicit $nzcv       ; ← flag 转成 GPR 值
$w0 = COPY %4
```

```text
; uaddo, -stop-after=regbankselect
%0:gpr(i64) = COPY $x0
%1:gpr(i64) = COPY $x1
%2:gpr(i64), %6:gpr(i32) = G_UADDO %0, %1              ; ← 溢出位在 GPR
$x0 = COPY %2(i64)
$w1 = COPY %6(i32)

; uaddo, -stop-after=instruction-select
%0:gpr64 = COPY $x0
%1:gpr64 = COPY $x1
%2:gpr64 = ADDSXrr %0, %1, implicit-def $nzcv
%6:gpr32 = CSINCWr $wzr, $wzr, 3, implicit $nzcv
$x0 = COPY %2
$w1 = COPY %6
```

两个函数给出同一个结论：**RegBankSelect 阶段没有任何 vreg 落在 CC**。`$nzcv` 只在 ISel 之后作为 `implicit-def` / `implicit` 出现，它是物理寄存器，不是 vreg 的 bank。

结论的适用范围（重要）：这是在本课覆盖的**普通 AArch64 GlobalISel 路径 + 23.1.4 自带测试集**上的结论。我检索了 `llvm/test/CodeGen/AArch64/GlobalISel/*.mir`，没有任何 `:cc(` 形式的 vreg；`AArch64RegisterBankInfo.cpp` 里也没有把 `G_ICMP`/`G_FCMP` 映射到 `CCRegBankID` 的分支。不排除某些 target-specific 或实验路径会用 CCR，但“CC 是默认布尔 bank”这个说法在 23.1.4 上不成立。

## 示例二：同一段 bitcast，两种 bank（实测）

```llvm
; .scratch/part0/bitcast.ll
define float @bc(i32 %x) {
entry:
  %f = bitcast i32 %x to float
  %r = fadd float %f, %f
  ret float %r
}
```

```text
; AArch64
%0:gpr(i32) = COPY $w0
%1:gpr(f32) = G_BITCAST %0(i32)
%3:fpr(f32) = COPY %1(f32)
%4:fpr(f32) = COPY %1(f32)
%2:fpr(f32) = G_FADD %3, %4
$s0 = COPY %2(f32)

; RISC-V（-mattr=+f）
%1:gprb(s64) = COPY $x10
%0:gprb(s32) = G_TRUNC %1(s64)
%3:fprb(s32) = COPY %0(s32)
%4:fprb(s32) = COPY %0(s32)
%2:fprb(s32) = G_FADD %3, %4
$f10_f = COPY %2(s32)
```

两边的结构惊人地一致：都是“整数在 GPR(B)，bitcast 结果留在 GPR(B)，然后每个浮点使用点各插一条 COPY”。差别只在名字与 LLT：

- AArch64：`gpr` / `fpr`，LLT 是 `i32` 与 `f32`；
- RISC-V：`gprb` / `fprb`，LLT 是 `s32`（浮点也是 `s32`）。

这说明 bank 抽象本身是 target 无关的，**具体覆盖与命名才是 target 相关**。

## 示例三：RISC-V 的两个 bank（实测）

```text
; @fadd(float, float)，riscv64 -mattr=+f，-stop-after=regbankselect
%0:fprb(s32) = COPY $f10_f
%1:fprb(s32) = COPY $f11_f
%2:fprb(s32) = G_FADD %0, %1
$f10_f = COPY %2(s32)
PseudoRET implicit $f10_f

; @cmp(i32, i32)，riscv64，-stop-after=regbankselect
%2:gprb(s64) = COPY $x10
%3:gprb(s64) = COPY $x11
%7:gprb(s64) = G_SEXT_INREG %2, 32
%8:gprb(s64) = G_SEXT_INREG %3, 32
%9:gprb(s64) = G_ICMP intpred(ult), %7(s64), %8
$x10 = COPY %9(s64)
PseudoRET implicit $x10
```

两个观察：

- RISC-V 的浮点是 `s32` 而不是 `f32`，所以你不能靠 LLT 判断“这是浮点值还是整数”——只能看指令与 bank。
- `G_ICMP` 在 RISC-V 上同样是 GPRB，不是某个“标志 bank”。RISC-V 根本没有 CC bank。

## 对比与取舍

| 维度 | AArch64 | RISC-V |
| --- | --- | --- |
| bank 数 | 3（GPR / FPR / CC） | 3（GPRB / FPRB / VRB） |
| 向量归属 | FPR（与标量浮点共用） | 独立 VRB |
| 布尔/标志 | CC bank 存在，但只服务 `$nzcv` | 无 CC bank |
| 跨 bank 代价 | GPR→FPR 4、FPR→GPR 5，其余 0/1 | 全部 0/1（未覆写） |
| 浮点 LLT | `f16/f32/f64` | `s16/s32/s64` |
| 128 位整数 | GPR 通过寄存器对；算术被 Legalizer 拆 | GPRB 通过 `GPRPair` |

两种设计的取舍：

- **AArch64 把向量并入 FPR**：bank 更少，指令选择的“园区”更集中，代价是向量与标量浮点共享同一个 copyCost 域，需要用 alternative mapping（Ch 9）去修正个别指令的选择。
- **RISC-V 把向量单列 VRB**：标量浮点与向量的代价模型可以分开，但 bank 数与 mapping 表更大，且跨 VRB/FPRB 的搬移同样要 COPY。
- **CC**：AArch64 把它建模成 bank（因为 `$nzcv` 确实是独立寄存器类），RISC-V 不建。这说明“bank 的划分”是 target 对寄存器文件的建模选择，不是 LLVM 的硬性规定。

## 踩雷集锦

**1. “`i1` / 比较结果应该在 CC bank”**

实测反例：`G_FCMP` 与 `G_UADDO` 的结果在 RBS 阶段都是 `gpr(i32)`，`$nzcv` 只在 ISel 之后出现。CC bank 不是布尔值的 bank。

**2. “CC 的拷贝很便宜，反正就一位”**

`CCR` 的 `CopyCost = -1`（禁止拷贝），`isAllocatable = 0`，bank 侧也没有 copy mapping（`BankIDToCopyMapIdx[CCR] = PMI_None`）。任何“把值搬进 CC”的设计都会在 mapping 层撞墙。

**3. “FPR 只放浮点”**

AArch64 的 FPR 覆盖 `QQQQ` 与 `ZPR`，向量、可伸缩向量、甚至 `i128` 标量都可能出现在这里（Ch 3 示例三）。`FPR = floating point` 只是名字。

**4. “GPR 最多 64 位”**

`getMaximumSize(GPR) == 128`，靠 `XSeqPairsClass`（两个 64 位寄存器）。但这不意味着有 128 位的算术指令：`i128` 的加法会被 Legalizer 拆成 `G_UADDO`/`G_UADDE`。

**5. “copyCost 5/4 是 LLVM 的通用规则”**

只有 AArch64 覆写了 GPR↔FPR 两个方向。RISC-V 完全没有 `copyCost`，用基类的 0/1。把 5/4 套到别的 target 上会得到错误的代价比较。

## 进阶

- **23.1.4 的 alternative mappings 很有限**。`getInstrAlternativeMappings` 只处理 `G_OR`（32/64 位、3 操作数）、`G_BITCAST`（32/64 位、2 操作数）、`G_LOAD`（64 位、2 操作数），其余走基类返回空（`AArch64RegisterBankInfo.cpp:259-364`）。这解释了为什么 Ch 1 里 `G_BITCAST` 加 greedy 没变化：候选确实存在，但代价不占优。
- **`G_LOAD` 的默认 bank 是“猜”出来的**。`isLoadFromFPType(MI)` 看 MMO，`onlyUsesFP` / `prefersFPUse` 看使用点（`1153-1173`）。所以同一段 `load i32`，喂给 `fadd` 就在 FPR，喂给 `add` 就在 GPR。
- **`G_FCONSTANT` 有 GPR 偏好逻辑**。`preferGPRForFPImm`（`366-400`）会在“只用于 store”或“无法编码成 fmov 立即数”时把浮点常量放进 GPR，这也是一种“LLT 不决定 bank”。
- **SVE 的 ZPR 覆盖**：FPR bank 覆盖 `ZPR`，所以可伸缩向量在 AArch64 上仍归 FPR。RISC-V 则用 `VRM8` 覆盖所有 LMUL 宽度。两者的“向量 bank”粒度不同，Ch 15 会展开。

## 动手练习

1. 把 `fcmp` 的 `zext` 去掉，直接 `ret i1`（需要改返回类型与 ABI），观察 legalized MIR 与 RBS 输出。i1 的返回值在寄存器里是怎么表示的？CC 有没有出现？
2. 写一个同时含 `load i32` → `add` 与 `load i32` → `bitcast` → `fadd` 的函数，预测两个 load 的 bank，跑 `-stop-after=regbankselect` 验证。再用 `-regbankselect-greedy` 跑一次，解释差异（或没有差异）。
3. 用 `-mattr=+f` 与不加 `-mattr` 各跑一次 RISC-V 的 `fadd`，对比 bank 与是否出现 libcall（`__addsf3`）。这能说明 bank 选择与“有没有硬件浮点”是什么关系？

## 本章重点整理

- AArch64 三个 bank：GPR（`XSeqPairsClass`，128）、FPR（`QQQQ`/`ZPR`，512）、CC（`CCR`，32）。
- `copyCost`：按语义方向 GPR→FPR = 4、FPR→GPR = 5（即 `copyCost(FPR, GPR)` / `copyCost(GPR, FPR)`），其余走基类 0/1；数值是手工经验值，注释带 FIXME。
- CC bank 只覆盖 `$nzcv`；`CCR` 的 `CopyCost = -1`、不可分配、无 copy mapping。
- 普通 AArch64 GlobalISel 路径上，`G_ICMP` / `G_FCMP` / `G_UADDO` 的布尔结果在 GPR，`$nzcv` 是 ISel 之后的物理副作用。
- RISC-V 三个 bank：GPRB（`GPR`/`GPRPair`）、FPRB（`FPR64`）、VRB（`VRM8`），未覆写 `copyCost`。
- 同一段 IR 在两个 target 上 bank 名与 LLT 都不同，但“GPR 侧算、跨 bank 插 COPY”的结构一致。

## 自我检核

- [ ] 能默写三个 AArch64 bank 的 TD 定义与各自的最大位数
- [ ] 能解释 5 与 4 的方向含义（谁是目的、谁是源），以及为什么不能套用到其他 target
- [ ] 能解释 `CCR` 的 `CopyCost = -1` 与 `BankIDToCopyMapIdx[CCR] = PMI_None` 的关系
- [ ] 能举出一个实测例子说明布尔值不在 CC bank
- [ ] 能说出 AArch64 与 RISC-V 在“向量归哪个 bank”上的差异
- [ ] 能解释为什么 `i128` 在 GPR 里能表示，但 `i128` 加法仍会被拆

## 延伸阅读

- **[AArch64RegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterBanks.td)**
  - **读哪里**：三行 `def`（12-19）。
  - **学什么**：每个 bank 覆盖哪个“序列类”，以及注释里对寄存器种类的说明。
  - **和本章的关联**：本章所有 coverage 结论的源头。
- **[AArch64RegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/GISel/AArch64RegisterBankInfo.cpp)**
  - **读哪里**：构造函数断言（52-97）、`copyCost`（226-245）、`getInstrAlternativeMappings`（259-364）、`G_FCMP`/`G_LOAD` 的映射（1110-1179）。
  - **学什么**：三个 bank 的实际约束、5/4 的来源、alternative mapping 的覆盖范围。
  - **和本章的关联**：示例一与示例二的直接依据。
- **[AArch64RegisterInfo.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/AArch64/AArch64RegisterInfo.td)**
  - **读哪里**：`CCR`（309-314）、`QQQQ`（591-594）、`XSeqPairsClass`（812-815）。
  - **学什么**：`CopyCost = -1`、`isAllocatable = 0`、`Size = 128/512` 这些物理约束。
  - **和本章的关联**：解释 CC 为什么“没有桥”。
- **[RISCVRegisterBanks.td](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/RISCV/GISel/RISCVRegisterBanks.td)**
  - **读哪里**：三行 `def`（12-19）。
  - **学什么**：GPRB/FPRB/VRB 的覆盖，与 AArch64 的差异。
  - **和本章的关联**：对比表的依据。
- **[RISCVRegisterBankInfo.cpp](https://github.com/llvm/llvm-project/blob/llvmorg-23.1.4/llvm/lib/Target/RISCV/GISel/RISCVRegisterBankInfo.cpp)**
  - **读哪里**：`getRegBankFromRegClass`（116-127）、`getInstrMapping`（219 起）。
  - **学什么**：RISC-V 如何把 `s32` 的浮点值映射到 FPRB，以及没有 `copyCost` 时的默认行为。
  - **和本章的关联**：示例三与“代价模型对比”的依据。

Part 0 到此结束。下一站是练习 A：拿手写 MIR，先预测每个 vreg 的 bank，再用 `-stop-after=regbankselect` 对答案。

→ [练习 A：手写 MIR，预测 bank](./practice-a-predict-banks.md)