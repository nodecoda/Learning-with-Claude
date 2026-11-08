# Ch 05 — 寄存器、寄存器类与调用约定

> **目标**：从 `GPR`/`FPR`/`VR` 的候选名单读到 `CSR_*` 的保存名单，理解"寄存器模型"如何同时约束寄存器分配、栈帧布局与跨模块 ABI；能在 MIR 里指出一次 spill/reload 是被哪张名单逼出来的。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，HEAD `0fc5afa04c23`，分支 `analysis/riscv-maintenance`。本章所有"实测"均来自该构建；未跑过的会明确标"未实测"。

> **前置**：Ch 02（ABI 与扩展）、Ch 03（MIR 与流水线）、Ch 04（TableGen）。

---

## 5.1 为什么维护者要读寄存器模型

寄存器模型是后端里"最像配置文件、最容易改错、最难在测试里发现"的一块。它有三个真实缺陷面：

1. **候选名单的顺序**决定寄存器分配结果。顺序写错不会让任何测试失败，只会让压缩指令变少、spill 变多、prologue 变长——这种回归通常只在 benchmark 或 `llvm-test-suite` 里出现。
2. **`CalleeSavedRegs` 名单**就是 ABI 契约。少列一个寄存器、或者 `getCalleeSavedRegs` 与 `getCallPreservedMask` 不一致，会产生"单个文件自洽、跨文件崩溃"的 bug，而且只在特定调用组合下复现。
3. **寄存器类与值类型的绑定**错了，会在指令选择阶段直接断言或生成非法 MIR。这类错误在 TableGen 生成期不一定被抓到。

本章的三份源码规模都不大，但信息密度极高：

| 文件 | 行数 | 内容 |
|---|---|---|
| `llvm/lib/Target/RISCV/RISCVRegisterInfo.td` | 1,070 | 寄存器定义、寄存器类、顺序 |
| `llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp` | 1,216 | 保留寄存器、CSR 选择、帧索引消除、分配 hint |
| `llvm/lib/Target/RISCV/RISCVCallingConv.td` | 101 | 全部 `CSR_*` 保存名单 |

---

## 5.2 先建立直觉：寄存器类 = 候选名单 + 顺序

一个寄存器类（`RegisterClass`）不是"集合"，而是**有序候选名单**：

```
虚拟寄存器 %0:gpr
      │
      ▼
  ┌─────────────────────────────────────────────┐
  │ GPR 的候选名单（顺序即优先级）               │
  │ x10 x11 x12 x13 x14 x15 x16 x17   ← 参数/临时 │
  │ x5  x6  x7                        ← 临时      │
  │ x28 x29 x30 x31                   ← 临时      │
  │ x8  x9                            ← 保存      │
  │ x18 ... x27                       ← 保存      │
  │ x0  x1  x2  x3  x4                ← 特殊      │
  └─────────────────────────────────────────────┘
      │  逐个试：已被占用？被保留？类型不兼容？
      ▼
  第一个可用的物理寄存器
```

三条推论，后面每一节都在用：

- **顺序 = 性能旋钮**。把 callee-saved 放前面，跨调用时少一次 prologue 保存，但普通函数里会多用"需要保存"的寄存器；反过来则 spill 更多。
- **子类 = 名单子集**。`GPRNoX0` 就是 `GPR` 去掉 `x0`，顺序不变。
- **类型决定选哪个类**。`i64` 在 RV64 走 `GPR`，`f64` 走 `FPR64`，`<vscale x 2 x i32>` 走 `VR`。

反直觉的地方：**顺序不是寄存器编号顺序**。GPR 从 `x10` 开始，FPR 从 `fa5`（`f15`）开始，VR 从 `v8` 开始。这不是笔误，每一条都有理由，5.3–5.5 逐条解释。

---

## 5.3 GPR：一张名单，六种切法

主名单在 `RISCVRegisterInfo.td:326`：

```tablegen
// The order of registers represents the preferred allocation sequence.
// Registers are listed in the order caller-save, callee-save, specials.
def GPR : GPRRegisterClass<(add (sequence "X%u", 10, 17),   // a0-a7
                                (sequence "X%u", 5, 7),     // t0-t2
                                (sequence "X%u", 28, 31),   // t3-t6
                                (sequence "X%u", 8, 9),     // s0-s1
                                (sequence "X%u", 18, 27),   // s2-s11
                                (sequence "X%u", 0, 4))> {  // zero,ra,sp,gp,tp
```

注释里那句话是整章的题眼：**先 caller-save，再 callee-save，最后特殊寄存器**。`x0`/`x1`/`x2`/`x3`/`x4` 排在最后，因为其中四个是保留寄存器（5.6 节），实际永远轮不到；`x0` 还能被选到是因为它在 `GPR` 里但语义特殊，主要给 `addi x0, ...` 这类模式用。

围绕这张主名单切出来的类，每个都有明确用途：

| 类 | 行号 | 名单差异 | 谁需要它 |
|---|---|---|---|
| `GPR` | 326 | 全部 32 个 | 通用 |
| `GPRNoX0` | 362 | 去掉 `x0` | 大多数真实指令（`x0` 作目的寄存器没意义） |
| `GPRNoX2` | 370 | 去掉 `x2`(sp) | 不能写 sp 的指令 |
| `GPRNoX0X2` | 375 | 去掉 `x0`、`x2` | 同上，更严 |
| `GPRX0` | 345 | 只有 `x0` | `x0` 作为固定操作数 |
| `GPRX1` / `GPRX5` | 350 / 356 | 只有 `x1` / `x5` | `ret`/`tail` 的 hint 形式、`sspush` |
| `GPRJALR` | 386 | 去掉 `x0`–`x5` | `jalr` 的目的寄存器 |
| `GPRJALRNonX7` | 388 | 再去掉 `x7` | `cm.jt` 之类 |
| `GPRC` | 390 | `x10`–`x15`、`x8`、`x9` | **压缩指令**能编码的 8 个寄存器 |
| `GPRTC` | 400 | `x6,x7,x10-x17,x28-x31` | 间接尾调用 |
| `GPRS07` | 412 | `x8,x9,x18-x23` | Zcmp/Xqccmp 的 push/pop |
| `GPRX1X5` | 418 | `x1`、`x5` | Zicfiss 的 `sspopchk`/`sspush` |
| `GPRNoX31` | 427 | 去掉 `x31` | 需要留一个临时寄存器的场景 |
| `GPRPair` | 460 | 偶数/奇数对 | Zilsd 的 64 位 load/store |

三个值得记住的设计理由：

**`GPRJALR` 为什么排除 `x1` 和 `x5`**（`.td:381-385` 的注释）：这两个寄存器在部分微架构上被当作"弹出返回地址栈"的 hint。用它们做普通 `jalr` 的目的寄存器，会触发硬件不该发生的 RAS 操作。这是"编码合法但微架构上有害"的典型例子。

**`GPRTC` 为什么不能用 callee-saved**（`.td:396-399`）：尾调用在跳转前会先恢复 callee-saved 寄存器，如果跳转地址恰好放在被恢复的寄存器里，地址就被覆盖了。同一个理由也让 `x5` 被排除。

**`GPRC` 为什么只有 8 个**：RVC 的 `c.addi`/`c.mv` 等指令的寄存器字段只有 3 位，编码上只能表示 `x8`–`x15`。所以编译器想生成压缩指令，就必须把值放进这 8 个寄存器之一——这就是 5.9 节 `getRegAllocationHints` 存在的理由。

`GPRPair` 是 Zilsd 引入的"寄存器对"类，有两个不寻常的地方：

```tablegen
def XLenPairRI : RegInfoByHwMode<
      [RV32,                RV64],
      [RegInfo<64, 64, 32>, RegInfo<128, 128, 64>]>;

let RegInfos = XLenPairRI, CopyCost = 2 in {
def GPRPair : RISCVRegisterClass<[XLenPairVT, ...], 64, (add X10_X11, ...)>;
```

- `CopyCost = 2`：复制一个寄存器对等价于两条 move，分配器要用它来权衡 spill 代价；
- 寄存器对由 `X10_X11` 这种"子寄存器组"构成，`X11` 是 `X10_X11` 的 `sub_gpr_odd`，所以偶数/奇数的配对关系是**编码事实**，不是分配器约定。

---

## 5.4 FPR：为什么顺序从 `fa5` 开始

FPR 的模具在 `RISCVRegisterInfo.td:530`：

```tablegen
class FPRRegisterClass<list<ValueType> regTypes, int align, string regSuffix>
    : RISCVRegisterClass<regTypes, align,
                         (add (sequence "F%u_" # regSuffix, 15, 10), // fa5-fa0
                              (sequence "F%u_" # regSuffix, 0, 7),   // ft0-f7
                              (sequence "F%u_" # regSuffix, 16, 17), // fa6-fa7
                              (sequence "F%u_" # regSuffix, 28, 31), // ft8-ft11
                              (sequence "F%u_" # regSuffix, 8, 9),   // fs0-fs1
                              (sequence "F%u_" # regSuffix, 18, 27)) // fs2-fs11
```

注释给了理由：**"We start by allocating argument registers in reverse order since they are compressible."** RVC 的 `c.fld`/`c.fsd` 用 3 位寄存器字段，能编码的是 `f8`–`f15`。`fa0`–`fa5` 正好是 `f10`–`f15`，落在压缩区间内，所以优先、并且**倒序**（`fa5` 最先），这样连续的临时值都留在可压缩区。

这条注释可以直接在实测里看到。`double fd(double a, double b, double c, double d) { return a*b + c*d; }`：

```asm
fd:
	fmul.d	fa5, fa2, fa3
	fmadd.d	fa0, fa0, fa1, fa5
	ret
```

参数在 `fa0`–`fa3`，乘法的临时结果落在 **`fa5`**——不是 `fa4`，正是"倒序优先"的结果。随后 `fmul` + `fadd` 被合成一条 `fmadd.d`（这是 DAG combine 的活，Ch 16 再讲）。

FPR 的类划分与 GPR 同构：

| 类 | 行号 | 值类型 | 说明 |
|---|---|---|---|
| `FPR16` | 554 | `f16`、`bf16` | 半精度 |
| `FPR32` | 564 | `f32` | 单精度 |
| `FPR64` | 577 | `f64` | 双精度 |
| `FPR128` | 587 | `f128` | 四精度（`Q` 扩展） |
| `FPR16C`/`32C`/`64C` | 559/569/582 | 同上 | 压缩编码可表示的 `f8`–`f15` |
| `GPRF16`/`GPRF32` | 597/612 | `f16`/`f32` | Zhinx：把浮点值放在 GPR 里 |
| `FPR256` | 1046 | `v8i32`/`v8f32` | XAIFET 的 256 位浮点寄存器 |

`FPR16C` 的名单特别短：`(add (sequence "F%u_" # regSuffix, 15, 10), (sequence "F%u_" # regSuffix, 8, 9))`——只有 8 个。任何"想用压缩浮点指令"的优化，都必须把值约束到这 8 个寄存器上。

---

## 5.5 VR：LMUL 与 `v0` 的代价

向量寄存器类在 `RISCVRegisterInfo.td:855` 起，由 `VReg` 模板生成，多带三个字段：`VLMul`、`NF`、`isVRegClass`。这三个字段会写进 `TSFlags`，机器层靠它判断"这条指令用的是哪个 LMUL 的类"：

```tablegen
class VReg<list<ValueType> regTypes, dag regList, int Vlmul, int nf = 1>
    : RISCVRegisterClass<regTypes, ..., regList> {
  let IsVRegClass = 1;
  let VLMul = Vlmul;
  let NF = nf;
}
```

主名单在 `RISCVRegisterInfo.td:902`，注释解释了那个奇怪的"后半段倒序"：

```tablegen
// We reverse the order of last 8 registers so that we don't needlessly prevent
// allocation of higher lmul register groups while still putting v0 last in the
// allocation order.
def VR : VReg<!listconcat(VM1VTs, VMaskVTs),
              (add (sequence "V%u", 8, 31),
                   (sequence "V%u", 7, 0)), 1>;
```

两个约束同时作用：

- **`v0` 必须是名单最后一个**。`v0` 是 mask 寄存器的默认位置，几乎所有 masked 指令都隐式读它；把它排在前面会让每次分配都"占住 mask"。
- **`v8`–`v31` 必须先于 `v7`–`v0`**。`v0` 之后那 8 个是"低编号区"，LMUL=8 的寄存器组只能从 `v0`/`v8`/`v16`/`v24` 开始；如果先把 `v4` 之类的低编号寄存器分出去，就可能把一个 M8 组切碎。

实测能直接看到起点：`vint32m1_t vadd(vint32m1_t a, vint32m1_t b, size_t n)` 编译成

```asm
vadd:
	vsetvli	zero, a0, e32, m1, ta, ma
	vadd.vv	v8, v8, v9
	ret
```

参数 `a`/`b` 落在 `v8`/`v9`——名单的前两个。函数返回类型是 `vint32m1_t`，走的是 `RISCV_VectorCall` 调用约定（汇编里能看到 `.variant_cc vadd`），所以 `v8`–`v9` 是参数寄存器，同时也是 M1 名单的头部。

其余向量类都是 `VR` 的 LMUL 变体：

| 类 | 行号 | LMUL | 名单 |
|---|---|---|---|
| `VR` | 902 | 1 | `v8`–`v31`，`v7`–`v0` |
| `VRNoV0` | 908 | 1 | 同上，去掉 `v0` |
| `VRM2` | 910 | 2 | `v8,v10,...,v30`，再 `v6,...,v0` |
| `VRM4` | 917 | 4 | `v8,v12,v16,v20,v24,v28,v4,v0` |
| `VRM8` | 924 | 8 | `v8,v16,v24,v0` |
| `MR` | 1057 | — | `m1`–`m7`、`m0`（mask 寄存器，`i8`/`v8i1`） |

`VRM8` 只有 4 个成员这件事，是"一个 M8 组吃掉整个寄存器文件"的直接体现。写 RVV 相关 pass 时，任何 `VRM8` 的虚拟寄存器都会把可用性压到极低。

---

## 5.6 保留寄存器：`getReservedRegs`

`RISCVRegisterInfo.cpp:165` 的 `getReservedRegs` 决定"哪些物理寄存器永远不进候选名单"。按原因分类：

| 寄存器 | 何时保留 | 代码位置 |
|---|---|---|
| `x0`（zero） | 永远 | 180 |
| `x2`（sp） | 永远 | 181 |
| `x3`（gp） | 永远 | 182 |
| `x4`（tp） | 永远 | 183 |
| `x8`（s0/fp） | 函数需要帧指针时 | 184–185 |
| `bp`（`x8` 或 `x18`） | 需要动态重对齐栈时 | 187–189 |
| `x16`–`x31` | RVE（只有 16 个 GPR） | 191–194 |
| `VL`/`VTYPE`/`VXSAT`/`VXRM`/`VLENB` | 永远 | 197–201 |
| `FRM`/`FFLAGS` | 永远 | 204–205 |
| `SSP` | 永远 | 213 |
| `T0`–`T15` | 永远 | 219–221 |

两个容易误读的点：

**`T0`–`T15` 不是 `x5`–`x20`。** 它们是 XSfmm 扩展的 tile 寄存器，`RISCVRegisterInfo.td:1010` 附近定义，汇编名是 `mt0`–`mt15`。这个循环无条件执行，是因为这些寄存器本来就不在通用名单里，重复标记无害。

**`markSuperRegs` 而不是 `set`。** 寄存器有别名（`x10` 与 `x10_w`、`x10_h` 是同一个物理寄存器的不同宽度视图）。只标记 `x10` 而漏掉 `x10_w`，会让 `f32` 值被分配到"已被占用的"同一个物理寄存器上。文件末尾有 `assert(checkAllSuperRegsMarked(Reserved))` 兜底。

用户保留寄存器走 Subtarget feature：`RISCVFeatures.td:2041` 起为 `x1`–`x31` 各生成一个 `reserve-xN`。实测（夹具 `csr.ll` 全文给出，20 行，可直接复制）：

```llvm
; csr.ll：10 次调用，10 个返回值都跨后续调用活跃 → 必须进 callee-saved
declare i64 @g()
define i64 @f(i64 %a0, i64 %a1) {
  %v0 = call i64 @g()
  %v1 = call i64 @g()
  %v2 = call i64 @g()
  %v3 = call i64 @g()
  %v4 = call i64 @g()
  %v5 = call i64 @g()
  %v6 = call i64 @g()
  %v7 = call i64 @g()
  %v8 = call i64 @g()
  %v9 = call i64 @g()
  %r1 = add i64 %v0, %v1
  %r2 = add i64 %r1, %v2
  %r3 = add i64 %r2, %v3
  %r4 = add i64 %r3, %v4
  %r5 = add i64 %r4, %v5
  %r6 = add i64 %r5, %v6
  %r7 = add i64 %r6, %v7
  %r8 = add i64 %r7, %v8
  %r9 = add i64 %r8, %v9
  ret i64 %r9
}
```

```bash
for M in "" ",+reserve-x18"; do
  echo "== -mattr=+m,+a,+f,+d,+c$M"
  $T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c$M \
    -stop-after=prolog-epilog -simplify-mir csr.ll -o - \
    | sed -n '/name: *f/,$p' | grep -E 'stackSize|liveins' | head -4
done
```

实测输出：

```
== -mattr=+m,+a,+f,+d,+c
  stackSize:       80
    liveins: $x1, $x8, $x9, $x18, $x19, $x20, $x21, $x22, $x23, $x24
== -mattr=+m,+a,+f,+d,+c,+reserve-x18
  stackSize:       80
    liveins: $x1, $x8, $x9, $x19, $x20, $x21, $x22, $x23, $x24, $x25
```

两件事同时发生：

1. `$x18` 从入口 livein 里消失了——分配器完全没碰它；
2. `$x25` 顶了上来，**栈帧仍是 80 字节**（10 个 spill slot × 8 字节）。

这正是"保留一个寄存器"的真实代价：不是栈变大，而是少了一个可用的 callee-saved 寄存器。这类 feature 在内核/RTOS 里用来"给手写汇编留一个寄存器"。

> 注意：`+reserve-x18` 只影响**分配器**，不影响 ABI——`x18` 仍在 `CSR_ILP32_LP64` 名单里。若手写汇编破坏了 `x18`，调用者仍会认为它被保存了；正确做法是同时约定不用它（或改 ABI），这也是内核文档强调"reserve 不是 ABI 保证"的原因。

---

## 5.7 调用约定：`CSR_*` 是一张"被调用者保存"名单

`RISCVCallingConv.td` 只有 101 行，但它是整个 ABI 的实现。核心是 `CalleeSavedRegs` 这个 TableGen 类：**列出的寄存器 = 函数必须保存并恢复的寄存器**。

```tablegen
def CSR_ILP32E_LP64E : CalleeSavedRegs<(add X1, X8, X9)>;

def CSR_ILP32_LP64
    : CalleeSavedRegs<(add CSR_ILP32E_LP64E, (sequence "X%u", 18, 27))>;

def CSR_ILP32F_LP64F
    : CalleeSavedRegs<(add CSR_ILP32_LP64,
                       F8_F, F9_F, (sequence "F%u_F", 18, 27))>;

def CSR_ILP32D_LP64D
    : CalleeSavedRegs<(add CSR_ILP32_LP64,
                       F8_D, F9_D, (sequence "F%u_D", 18, 27))>;
```

注意 `F` 版本是在 `CSR_ILP32_LP64` 上**叠加**浮点寄存器，而不是替换。这就是"软浮点 ABI 不保存 FPR、硬浮点 ABI 保存 `fs0`/`fs1`/`fs2`–`fs11`"的编码方式。

完整名单：

| 名单 | 内容 | 触发条件 |
|---|---|---|
| `CSR_ILP32E_LP64E` | `x1,x8,x9` | RVE |
| `CSR_ILP32_LP64` | `+x18`–`x27` | 软浮点 |
| `CSR_ILP32F_LP64F` | `+f8,f9,f18`–`f27`（`.s` 视图） | 单精度硬浮点 |
| `CSR_ILP32D_LP64D` | `+f8,f9,f18`–`f27`（`.d` 视图） | 双精度硬浮点 |
| `CSR_ILP32_LP64_V` 等三个 | 上述 + `CSR_V` | `RISCV_VectorCall` |
| `CSR_V` | `v1`–`v7`、`v24`–`v31` 及对应 M2/M4/M8 组 | 向量调用 |
| `CSR_NoRegs` | 空 | GHC CC / `getNoPreservedMask` |
| `CSR_IPRA` | 只有 `x1` | IPRA |
| `CSR_Interrupt` | `x1`、`x5`–`x31` | `interrupt` 属性 |
| `CSR_XLEN_F32_Interrupt` 等 | 上述 + 全部 FPR | `interrupt` + F/D |
| `CSR_XLEN_V_Interrupt` 等 | 上述 + 全部 V | `interrupt` + V |
| `CSR_*_RVE` | 上述按 RVE 裁剪 | RVE |
| `CSR_RT_MostRegs` | `CSR_Interrupt` 减去 `x5,x6,x7,x28` | `preserve_mostcc` |

C++ 侧的选择逻辑在 `RISCVRegisterInfo.cpp:70`，是一棵按**函数属性 → ABI → 扩展**展开的分支树：

```cpp
const MCPhysReg *
RISCVRegisterInfo::getCalleeSavedRegs(const MachineFunction *MF) const {
  if (MF->getFunction().getCallingConv() == CallingConv::GHC)
    return CSR_NoRegs_SaveList;
  if (MF->getFunction().getCallingConv() == CallingConv::PreserveMost)
    return Subtarget.hasStdExtE() ? CSR_RT_MostRegs_RVE_SaveList
                                  : CSR_RT_MostRegs_SaveList;
  if (MF->getFunction().hasFnAttribute("interrupt")) { /* ... */ }

  bool HasVectorCSR =
      MF->getFunction().getCallingConv() == CallingConv::RISCV_VectorCall &&
      Subtarget.hasVInstructions();

  RISCVABI::ABI ABI = Subtarget.getTargetABI();
  if (RISCVABI::isRVE(ABI))       return CSR_ILP32E_LP64E_SaveList;
  if (RISCVABI::isSoftFP(ABI))    return HasVectorCSR ? CSR_ILP32_LP64_V_SaveList
                                                      : CSR_ILP32_LP64_SaveList;
  /* ... */
}
```

这里有一个维护者必须知道的耦合：`CalleeSavedRegs` 会为每个名单生成两个符号——`<名>_SaveList`（`getCalleeSavedRegs` 用）和 `<名>_RegMask`（`getCallPreservedMask` 用，`RISCVRegisterInfo.cpp:889`）。**两个函数必须用同一张名单**，否则调用点的掩码和被调用者的 prologue 对不上。好在符号名不同、类型不同，写错会编译失败，不会静默通过——这是 TableGen 帮我们挡住的一类错误。

### 实测：名单名字直接印在 MIR 上

`press.c`（5.8 节会展开）在 `-stop-after=prolog-epilog` 下有一行：

```mir
PseudoCALL target-flags(riscv-call) @bar, csr_ilp32d_lp64d, implicit-def dead $x1, implicit-def $x2
```

`csr_ilp32d_lp64d` 就是 `CSR_ILP32D_LP64D_RegMask` 的小写形式，作为**调用指令的操作数**出现。读 MIR 时看到这一项，就知道"这次调用承诺保留哪些寄存器"。这是排查 ABI 类 bug 最快的一条线索：调用点的名字和被调用函数的 prologue 名单必须一致。

### 实测：`interrupt` 与 `preserve_mostcc`

```c
__attribute__((interrupt("machine"))) void isr(void) { extern volatile long g; g = g + 1; }
```

```asm
isr:
	addi	sp, sp, -16
	sd	a0, 8(sp)                       # 8-byte Folded Spill
	sd	a1, 0(sp)                       # 8-byte Folded Spill
	...
	mret
```

只保存了实际用到的 `a0`/`a1`，而不是 `x5`–`x31` 全部 27 个：`CSR_Interrupt` 是"候选保存名单"，prologue 只会保存**函数里真正写了**的那些（`RISCVFrameLowering` 会按 `determineCalleeSaves` 过滤）。返回指令是 `mret` 而不是 `ret`。

`preserve_mostcc` 在 C 层不被 clang 接受（会警告 `'preserve_most' calling convention is not supported for this target`），但 IR 层可用：

```llvm
declare preserve_mostcc void @callee()
define preserve_mostcc i64 @pm(i64 %a, i64 %b) {
  %1 = mul i64 %a, 3
  %2 = add i64 %1, %b
  call preserve_mostcc void @callee()
  ret i64 %2
}
```

```mir
    frame-setup SD killed $x1, $x2, 8 :: (store (s64) into %stack.0)
    frame-setup SD $x10, $x2, 0 :: (store (s64) into %stack.1)
    ...
    PseudoCALL target-flags(riscv-call) @callee, csr_rt_mostregs, ...
```

`a0`（`x10`）被当成 callee-saved 保存了——这正是 `CSR_RT_MostRegs` 的定义（把 `x5,x6,x7,x28` 以外的调用者保存寄存器都变成被调用者保存）。同时它省掉了 `x5`/`x6`/`x7`/`x28` 的保存，所以叫 "most regs" 而不是 "all regs"。

---

## 5.8 spill/reload：谁决定，长什么样

寄存器不够用时，分配器把值写进栈槽（spill slot），用时再读回。**决定"够不够"的是候选名单大小 + 活跃区间长度**，不是函数大小。

### 标量 spill

```c
extern long g[64];
extern void bar(void);
long press(void) {
  long v0=g[0], ... v15=g[15];   // 24 个值
  bar();
  return v0+...+v23;
}
```

24 个值全部活过 `bar()` 调用点，而调用点只知道 `csr_ilp32d_lp64d` 保住了 13 个寄存器（`ra` + `s0`/`s1` + `s2`–`s11`），剩下的只能进栈：

```mir
frameInfo:
  stackSize:       208
stack:
  - { id: 0, type: spill-slot, offset: -112, size: 8, alignment: 8 }
  ...
  - { id: 11, type: spill-slot, offset: -200, size: 8, alignment: 8 }
  - { id: 12, type: spill-slot, offset: -8, size: 8, alignment: 8, callee-saved-register: '$x1' }
  ...
  - { id: 24, type: spill-slot, offset: -104, size: 8, alignment: 8, callee-saved-register: '$x27' }
```

读法：

- 25 个 8 字节槽 = 200 字节，`stackSize` 是 208（对齐填充 8 字节）；
- `id 0`–`11` 是**真 spill**：没有 `callee-saved-register` 字段；
- `id 12`–`24` 是 **callee-saved 保存槽**：有 `callee-saved-register: '$xN'`，它们不是"值 spill"，而是 ABI 要求；
- 汇编里 `sd s0, 192(sp) # 8-byte Folded Spill` 这种注释容易误导——`s0` 是 callee-saved 保存，不是溢出。判据是 MIR 里有没有 `callee-saved-register` 字段。

### 向量 spill：可伸缩栈对象

同样的手法换成向量。用 20 个 `vint32m1_t` 活过 `bar()`：

```mir
stack:
  - { id: 0, type: spill-slot, offset: -8, size: 8, alignment: 8, stack-id: scalable-vector }
  ...
    VS1R_V killed renamable $v8, killed $x11 :: (store (<vscale x 1 x s64>) into %stack.0)
    PseudoCALL target-flags(riscv-call) @bar, csr_ilp32d_lp64d, ...
```

三个要点：

1. **`stack-id: scalable-vector`**：这个槽的大小是 `vscale × 8` 字节，编译期未知，所以不能算进固定 `stackSize`（这里 `stackSize` 只有 32，是标量部分）。
2. 汇编里体现为动态调整：`csrr a1, vlenb` → `li a2, 20` → `mul` → `sub sp, sp, a1`，再配 `vs1r.v`（整寄存器存储，不受 `vl` 影响）。
3. **`csr_ilp32d_lp64d` 不含任何 `v` 寄存器**。`bar` 用的是默认整数调用约定，不是 `RISCV_VectorCall`，所以这条掩码对向量一个都不保。这就是为什么 20 个向量全都要落栈——它们不是"callee-saved 冲突"，而是"调用点根本没承诺保住向量"。

第 3 点是 RVV 相关 bug 的高发区。写优化时如果假设"向量跨调用是便宜的"，就会在真实 workload 上遇到大量 `vs1r.v`/`vl1re32.v`。要减少这种 spill，只能让调用点用 `RISCV_VectorCall`（`__attribute__((riscv_vector_cc))`），或者缩短活跃区间。

### 分配的质量旋钮

两个最近常被改的钩子：

- `getSpillWeightScaleFactor`（`RISCVRegisterInfo.cpp:975`）：返回寄存器类的权重，影响 spill 代价估算。给 `GPRPair` 调大权重，分配器就更不愿意拆散一对。
- `getRegAllocationHints`（`RISCVRegisterInfo.cpp:982`）：给分配器"建议值"。RISC-V 这里做两件事——Zilsd 的偶数/奇数配对 hint，以及"把两地址指令的操作数放到同一个可压缩寄存器"的 hint（`CLOpts().regalloc_hints` 开关）。

改这两个函数不需要动指令定义，但会全局改变分配结果。任何这类 patch 都必须跑 `llvm-test-suite` 的 code size 对比，光靠 lit 测试看不出效果。

---

## 5.9 从虚拟到物理：MIR 上的三态

同一个值在流水线不同阶段有三种写法，读 MIR 时要能一眼分清：

| 阶段 | 形态 | 例子 |
|---|---|---|
| 指令选择后 | 虚拟寄存器 + 寄存器类 | `%0:gpr = ADDI %1:gpr, 1` |
| 分配后 | 物理寄存器 + `renamable` | `renamable $x10 = ADDI renamable $x11, 1` |
| prologue/epilogue 后 | 物理寄存器 + 帧信息 | `$x10 = ADDI $x2, 16` |

`renamable` 的意思是"这个物理寄存器还可以被改名（比如为了消除 copy）"，它不改变语义。

三个停点配合使用：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m -stop-after=finalize-isel  -simplify-mir f.ll   # 虚拟寄存器
$T/bin/llc -mtriple=riscv64 -mattr=+m -stop-after=greedy         -simplify-mir f.ll   # 物理寄存器
$T/bin/llc -mtriple=riscv64 -mattr=+m -stop-after=prolog-epilog  -simplify-mir f.ll   # 栈槽 + CFI
```

`prolog-epilog` 阶段的 MIR 里还会出现 `frame-setup`/`frame-destroy` 标记，它们对应 `MachineInstr::FrameSetup`/`FrameDestroy`——`adjustReg`（`:236`）靠这个标记区分"在 prologue 里调整栈"还是"在函数体里调整栈"，进而决定用 `vsetvli` 还是 `vlenb` 读 VLEN（见 5.8 节的 `csrr` 序列）。

---

## 5.10 踩雷集锦

1. **改了 `GPR` 顺序，测试全绿但性能掉了。** lit 测试只 CHECK 指令助记符，不 CHECK 寄存器编号。要验证顺序变化，必须看 `objdump` 的压缩指令比例或跑 benchmark。
2. **新增一个"被调用者保存"的寄存器，只改了 `getCalleeSavedRegs`。** 结果是调用点掩码不匹配——链接期不会报错，运行时才炸。两个函数必须同时改，且用同一张 `CSR_*`。
3. **`CSR_Interrupt` 写成 `CSR_ILP32_LP64`。** 中断处理函数只保存 `s0`–`s11`，被中断的代码用到的 `t0`–`t6` 全被破坏。这个 bug 在 qemu 上可能"看起来正常"，因为中断恰好没打断用 `t` 寄存器的代码。
4. **`GPRC`/`FPRC` 里多加了一个寄存器。** 汇编器能过，但 `RISCVCompressInstEmitter` 生成的压缩指令编码会错位——3 位字段装不下第 9 个寄存器。改这类类必须同步 `DecoderMethod` 里的基址。
5. **`VR` 顺序调整时动了 `v0` 的位置。** masked 指令隐式读 `v0`，`v0` 一旦被普通值占用，mask 就没了。`VRNoV0` 存在就是为了让需要 mask 的场景显式排除它。
6. **给 `GPRPair` 加值类型但忘了 `RegInfos`。** `XLenPairVT` 在 RV32 是 64 位、RV64 是 128 位，靠 `RegInfoByHwMode` 表达。写成固定 `RegInfo` 会让 RV32 直接断言。
7. **以为 `T0`–`T15` 是 `x5`–`x20`。** 它们是 XSfmm 的 `mt0`–`mt15`。在 `getReservedRegs` 里"顺手清理"这段循环，会让 tile 寄存器进入通用分配。
8. **假设向量跨调用是免费的。** 默认整数调用约定的掩码不含 `v` 寄存器，20 个活跃向量 = 20 次 `vs1r.v`。这类"性能悬崖"只能靠 `-mattr=+v` 的真实 workload 或 MIR 观察发现。

---

## 5.11 可跑实验

### 实验 1：验证 GPR 分配顺序

```bash
cd /tmp && cat > order.c <<'EOF'
extern void bar(void);
long f(long a, long b) {
  long x0=a*3, x1=b+7, x2=a^b, x3=a<<2, x4=b>>1, x5=a-b, x6=a|b, x7=a&b,
       x8=a+1, x9=b+2, x10=a*5, x11=b*7, x12=a^3, x13=b^5, x14=a+9, x15=b+11;
  bar();
  return x0+x1+x2+x3+x4+x5+x6+x7+x8+x9+x10+x11+x12+x13+x14+x15;
}
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O2 -S -o - order.c \
  | grep -E "sd\s+(ra|s[0-9]+)|^f:|call" | head -20
```

应该看到 prologue 保存 `ra` + `s0`–`s7`，说明 16 个长整型值优先落在 `a0`–`a7` 和 `t0`–`t6`，跨调用时才轮到 `s0`–`s7`。

### 实验 2：把 CSR 名单名字打出来

```bash
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O2 -S -emit-llvm -o /tmp/p.ll /tmp/press.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -stop-after=prolog-epilog -simplify-mir /tmp/p.ll -o - \
  | grep -E "PseudoCALL|callee-saved-register" | head -5
```

第一行是掩码名，后面是保存槽。试试把 `-mabi=lp64d` 换成目标属性里的软浮点（用 `-mattr` 去掉 `+d` 会报 ABI 错误，见 Ch 02），观察 `csr_ilp32_lp64` 与 `csr_ilp32d_lp64d` 的差别。

### 实验 3：观察 RVE 的 16 个 GPR

```bash
cd /tmp && cat > rve.c <<'EOF'
long f(long a, long b, long c, long d) { return a*b+c*d; }
EOF
$T/bin/clang --target=riscv64-unknown-elf -march=rv32e -mabi=ilp32e -O2 -S -o - rve.c | sed -n '1,25p'
```

关键观察：`a3` 被显式 spill 到栈（`sw a3, 0(sp)`）。因为在 ILP32E 里只有 `x1`/`x8`/`x9` 是被调用者保存，`a3`(`x13`) 不在其中，跨两次 `__mulsi3` 调用必须自己存。

### 实验 4：`+reserve-xN` 的效果

```bash
for X in 5 18; do
  echo "--- reserve-x$X ---"
  $T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c,+reserve-x$X \
    -stop-after=prolog-epilog -simplify-mir /tmp/csr.ll -o - \
    | sed -n '/name: *f/,$p' | grep "liveins:" | head -1
done
```

`+reserve-x18` 那一组不应出现 `$x18`；`+reserve-x5` 那一组不应出现 `$x5`。两组栈帧大小都应是 80 字节——证明"少一个寄存器"只是换人，不是必然变差。

---

## 5.12 源码落点

- **`llvm/lib/Target/RISCV/RISCVRegisterInfo.td`**（1,070 行）
  - 300–470：`RISCVRegisterClass`/`GPRRegisterClass` 模板与全部 GPR 类；`GPRC` 在 390，`GPRJALR` 在 386，`GPRPair` 在 460。
  - 530–640：`FPRRegisterClass` 与 FPR 类；顺序注释在 545–548。
  - 855–930：`VReg` 模板与 `VR`/`VRM2`/`VRM4`/`VRM8`；`v0` 倒序的理由在 899–901。
  - 1000–1060：`SSP`、XSfmm 的 `T*`、XAIFET 的 `FPR256`、mask 类 `MR`。

- **`llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp`**（1,216 行）
  - 70–115：`getCalleeSavedRegs`，按 CC → ABI → 扩展展开。
  - 165–226：`getReservedRegs`，保留寄存器的全部分类。
  - 236–330：`adjustReg`，prologue/函数体里调整栈的两种策略。
  - 583–723：`eliminateFrameIndex`，帧索引如何落成 `addi`/`ld`/`sd`。
  - 975–1180：`getSpillWeightScaleFactor`、`getRegAllocationHints`、`updateRegAllocHint`。

- **`llvm/lib/Target/RISCV/RISCVCallingConv.td`**（101 行）
  - 16–43：ILP32/LP64 的四个基础名单。
  - 29–41：`CSR_V` 与三个 `_V` 变体。
  - 49–100：`interrupt`、RVE、`RT_MostRegs`、`IPRA`。

- **生成物**
  - `$T/lib/Target/RISCV/RISCVGenRegisterInfoMCDesc.inc`：寄存器类描述表（含顺序）。`GPRRegClass` 的 32 项顺序在文件后半段，`GPRNoX0` 紧随其后。
  - `$T/lib/Target/RISCV/RISCVGenRegisterInfoTargetDesc.inc`（1.16 MB）：`getCalleeSavedRegs` 用到的 `_SaveList` 与子寄存器关系。

- **测试**
  - `llvm/test/CodeGen/RISCV/callee-saved-gprs.ll`、`callee-saved-fpr32s.ll`、`callee-saved-fpr64s.ll`：CSR 名单的正面测试，按 ABI 变体分组。
  - `llvm/test/CodeGen/RISCV/calling-conv-ilp32e.ll`、`calling-conv-rv32f-ilp32e.ll`：RVE 的 16 GPR 与 ABI 交叉。
  - `llvm/test/CodeGen/RISCV/interrupt-attr.ll`、`qci-interrupt-attr.ll`：中断属性的保存行为。
  - `llvm/test/CodeGen/RISCV/rvv/rv64-spill-vector.ll`、`rv64-spill-vector-csr.ll`：向量 spill 与可伸缩栈对象。

---

## 5.13 本章重点整理

- 寄存器类是**有序候选名单**，顺序即分配优先级；改顺序不改语义，只改质量。
- GPR 顺序 = 参数/临时 → callee-saved → 特殊；FPR 从 `fa5` 倒序起（压缩编码）；VR 从 `v8` 起且 `v0` 最后（mask 与 LMUL 组）。
- `GPRC`/`FPRC` 只含压缩编码可表示的 8 个寄存器，这是"压缩指令比例"的硬约束。
- 保留寄存器由 `getReservedRegs` 决定：`x0`/`sp`/`gp`/`tp`/`VL`/`VTYPE`/`FRM`/`FFLAGS` 永远保留；`x8` 只在需要帧指针时保留；RVE 只有 16 个 GPR。
- `CSR_*` 是 ABI 的唯一实现：`CSR_ILP32D_LP64D` = `ra` + `s0`/`s1` + `s2`–`s11` + `fs0`/`fs1` + `fs2`–`fs11`。
- 掩码名字会作为 `PseudoCALL` 的操作数出现在 MIR 上，是排查 ABI bug 的第一线索。
- spill slot 与 callee-saved 保存槽长得一样，靠 `callee-saved-register:` 字段区分。
- 向量 spill 用 `stack-id: scalable-vector` + `vs1r.v` + 运行时 `vlenb` 调整 `sp`；默认整数调用约定不保任何向量寄存器。

---

## 5.14 自我检核

- [ ] 能默写 GPR 的六段顺序，并解释为什么 `x0`–`x4` 在最后
- [ ] 能解释 FPR 为什么从 `fa5` 开始、`GPRC` 为什么只有 8 个
- [ ] 能说清 `v0` 为什么必须排在 `VR` 名单最后
- [ ] 能列出 5 个"永远保留"的寄存器及其原因
- [ ] 给定一个函数属性（`interrupt`/`preserve_mostcc`/`riscv_vector_cc`），能说出它用哪张 `CSR_*`
- [ ] 能在 MIR 里区分真 spill 与 callee-saved 保存
- [ ] 能解释为什么向量跨默认调用约定一定 spill
- [ ] 能说出 `-mtriple` + `-mattr=+reserve-x18` 的效果并验证

---

## 5.15 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVRegisterInfo.td`**（本树，1,070 行）
  - 读哪里：300–470 的 GPR 全家、530–640 的 FPR、855–930 的 VR。
  - 为什么读：所有"寄存器不够/分配不好"的问题，答案都在这张名单里。
- **`llvm/lib/Target/RISCV/RISCVCallingConv.td`**（本树，101 行）
  - 读哪里：全文。它短到可以一次读完，但每次读都会有新发现。
  - 为什么读：ABI 类 bug 全部落在这 101 行。
- **`llvm/lib/Target/RISCV/RISCVRegisterInfo.cpp`**
  - 读哪里：70–226（CSR + 保留）、236–330（`adjustReg`）、583–723（帧索引消除）。
  - 为什么读：`adjustReg` 是 Ch 10 的前置，`eliminateFrameIndex` 是所有栈访问的落地处。
- **`llvm/docs/RISCVUsage.md`**（本树，808 行）
  - 读哪里：ABI 与 `-mabi` 的取值、`interrupt` 属性、`preserve_most` 的限制。
  - 为什么读：把本章的名单和用户可见行为对上。
- **`clang/include/clang/Basic/AttrDocs.td`**（本树）
  - 读哪里：6946 行起 `riscv::vector_cc / riscv_vector_cc`。
  - 为什么读：向量调用约定是"少 spill"的唯一正路，属性语义必须准确。
- **`llvm/test/CodeGen/RISCV/callee-saved-gprs.ll`**
  - 读哪里：每个 `-mabi` 变体的 CHECK 行。
  - 为什么读：这是"改 CSR 名单必须同步更新的测试"的模板。

---

→ 下一章：[Ch 06 — 指令编码与 MC 层](./06-instruction-encoding-and-mc.md)
