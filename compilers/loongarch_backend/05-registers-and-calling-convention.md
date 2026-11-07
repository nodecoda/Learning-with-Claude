# Ch 05 — 寄存器、寄存器类与调用约定

> 本章目标：理解 `GPR`/`FPR`/`CFR`/`SCR` 的层级，知道哪些寄存器被保留、哪些是 callee-saved，并能从 prologue 的 `st.d $ra, ...` 反推出 `CalleeSavedRegs` 表。

---

## 5.1 寄存器不是平铺的：`LoongArchReg32/64/128/256`

`LoongArchRegisterInfo.td` 一开始就定义了一条“宽度链”：

```tablegen
class LoongArchReg32<bits<16> Enc, string n, list<string> alt = []>
    : Register<n> { let HWEncoding = Enc; let AltNames = alt; }

class LoongArchReg64<LoongArchReg32 subreg>
    : LoongArchRegWithSubRegs<subreg.HWEncoding, subreg.AsmName, [subreg],
                              subreg.AltNames> {
  let SubRegIndices = [sub_32];
}

class LoongArchReg128<LoongArchReg64 subreg, string n>
    : LoongArchRegWithSubRegs<subreg.HWEncoding, n, [subreg]> {
  let SubRegIndices = [sub_64];
}

class LoongArchReg256<LoongArchReg128 subreg, string n>
    : LoongArchRegWithSubRegs<subreg.HWEncoding, n, [subreg]> {
  let SubRegIndices = [sub_128];
}
```

层级：

```text
F0 (32-bit)
 └─ F0_64 (64-bit, sub_32)
      └─ (LSX128 的 128 位视图)
           └─ (LASX256 的 256 位视图)
```

这解释了：

- 为什么 MIR 里会看到 `F0_64` 而不是 `F0`；
- 为什么 `FPR32` 和 `FPR64` 是两个不同的寄存器类；
- 为什么 `copyPhysReg` 要处理 `F0` ↔ `F0_64` 的 `movfr2gr.s`/`movgr2fr.d` 之类转换。

`R0`–`R31` 是 `LoongArchReg32`，但它们不参与 64 位子寄存器链；`GPR` 的 `GRLenVT` 由 `HwMode` 决定是 `i32` 还是 `i64`：

```tablegen
def GRLenVT : ValueTypeByHwMode<[LA32, LA64], [i32, i64]>;
def GRLenRI : RegInfoByHwMode<
      [LA32, LA64],
      [RegInfo<32,32,32>, RegInfo<64,64,64>]>;
```

---

## 5.2 `GPR` 的分配顺序不是编号顺序

```tablegen
def GPR : GPRRegisterClass<(add
    // Argument registers (a0...a7)
    (sequence "R%u", 4, 11),
    // Temporary registers (t0...t8)
    (sequence "R%u", 12, 20),
    // Static register (s9/fp, s0...s8)
    (sequence "R%u", 22, 31),
    // Specials (r0, ra, tp, sp)
    (sequence "R%u", 0, 3),
    // Reserved (Non-allocatable)
    R21)>;
```

注释写得很清楚：**先 caller-save，再 callee-save，最后 specials**。寄存器分配器按这个顺序拿寄存器，所以：

- 短函数优先用 `$a0`–`$a7`/`$t0`–`$t8`，不需要保存；
- 只有活跃值多到 caller-save 不够，才会用 `$s0`–`$s8`/`$fp`，并在 prologue 里保存；
- `$r0`/`$ra`/`$tp`/`$sp` 排在最后，正常情况下不会被分配（它们被 `getReservedRegs` 保留）。

特殊用途的寄存器类：

| 类 | 内容 | 为什么 |
|---|---|---|
| `GPRT` | `a0-a7` + `t0-t8` | 间接尾调用不能用 callee-saved（尾调用前会恢复旧值，覆盖跳转地址） |
| `GPRJR` | `GPR - R1` | `jr $ra` 会被微架构当成 return，干扰返回预测 |
| `GPRNoR0R1` | `GPR - R0 - R1` | `csrxchg` 的 `rj` 编码为 0/1 时会变成 `csrrd`/`csrwr` |

这三条都是“硬件语义限制”，不是优化偏好；改它们会直接生成错误指令。

---

## 5.3 哪些寄存器被保留

`LoongArchRegisterInfo::getReservedRegs()`：

```cpp
markSuperRegs(Reserved, LoongArch::R0);  // zero
markSuperRegs(Reserved, LoongArch::R2);  // tp
markSuperRegs(Reserved, LoongArch::R3);  // sp
markSuperRegs(Reserved, LoongArch::R21); // non-allocatable
if (TFI->hasFP(MF))
  markSuperRegs(Reserved, LoongArch::R22); // fp
if (TFI->hasBP(MF))
  markSuperRegs(Reserved, LoongArchABI::getBPReg()); // bp
```

要点：

- **`$r2`（`$tp`）永远保留。** TLS 代码依赖它指向线程指针；如果分配器把它当普通寄存器，TLS 序列会被破坏。
- **`$r3`（`$sp`）永远保留。**
- **`$r22`（`$fp`）只在 `hasFP()` 为真时保留**：函数有可变大小 alloca、需要栈对齐、取 frame address、或禁用 FP elimination 时。
- **`$r21` 不可分配**（保留给 ABI/未来用途）。

`getFrameRegister()` 也依赖 `hasFP()`：

```cpp
return TFI->hasFP(MF) ? LoongArch::R22 : LoongArch::R3;
```

没有 frame pointer 时，frame index 相对 `$sp` 计算；有 frame pointer 时相对 `$fp`。

---

## 5.4 浮点/条件码/向量寄存器

### 5.4.1 `FPR32` / `FPR64`

```tablegen
def FPR32 : RegisterClass<"LoongArch", [f32], 32, (sequence "F%u", 0, 31)>;
def FPR64 : RegisterClass<"LoongArch", [f64], 64, (sequence "F%u_64", 0, 31)>;
```

别名（实测 `LoongArchRegisterInfo.td`）：

| 区间 | 别名 | 用途 |
|---|---|---|
| `$f0`–`$f7` | `$fa0`–`$fa7` | 浮点参数 |
| `$f8`–`$f23` | `$ft0`–`$ft15` | 浮点临时 |
| `$f24`–`$f31` | `$fs0`–`$fs7` | 浮点 callee-saved |

汇编器同时接受 `$f0` 和 `$fa0`；`InstPrinter` 默认打印哪个取决于 `RegAltNameIndex`（`NoRegAltName` 时打印主名）。

### 5.4.2 `CFR` / `FCSR` / `SCR`

| 类 | 寄存器 | 用途 |
|---|---|---|
| `CFR` | `$fcc0`–`$fcc7` | 浮点比较结果（条件码） |
| `FCSR` | `$fcsr0`–`$fcsr3` | 浮点控制/状态 |
| `SCR` | `$scr0`–`$scr3` | 向量 scratch |

`CFR` 的存在解释了为什么 `lowerSETCC`/`lowerSELECT` 要把 `i1` 结果在 GPR 和 FCC 之间搬（Ch 15/17）。`SCR` 是 LSX/LASX 某些 shuffle/reduction 的临时寄存器。

### 5.4.3 `LSX128` / `LASX256`

```tablegen
def LSX128 : RegisterClass<"LoongArch", [v16i8, v8i16, v4i32, v2i64,
                                         v4f32, v2f64], 128,
                           (sequence "V%u", 0, 31)>;
def LASX256 : RegisterClass<"LoongArch", [v32i8, v16i16, v8i32, v4i64,
                                          v8f32, v4f64], 256,
                            (sequence "X%u", 0, 31)>;
```

`V0`–`V31` 是 128 位视图，`X0`–`X31` 是 256 位视图；LASX 的 `X` 寄存器复用 LSX 的物理寄存器（低 128 位）。这就是为什么 `xvld` 和 `vld` 能操作同一组物理寄存器。

---

## 5.5 callee-saved 集合与调用约定

`LoongArchCallingConv.td`：

```tablegen
def CSR_ILP32S_LP64S
    : CalleeSavedRegs<(add R1, (sequence "R%u", 22, 31))>;

def CSR_ILP32F_LP64F
    : CalleeSavedRegs<(add CSR_ILP32S_LP64S, (sequence "F%u", 24, 31))>;

def CSR_ILP32D_LP64D
    : CalleeSavedRegs<(add CSR_ILP32S_LP64S, (sequence "F%u_64", 24, 31))>;

def CSR_NoRegs : CalleeSavedRegs<(add)>;
def CSR_NoneRegs : CalleeSavedRegs<(add R1, R22)>;
def CSR_MostRegs : CalleeSavedRegs<(add CSR_ILP32S_LP64S,
                                        (sequence "R%u", 4, 11),
                                        (sequence "R%u", 16, 19))>;
```

映射到 `getCalleeSavedRegs()`：

```cpp
if (CC == CallingConv::GHC)          return CSR_NoRegs_SaveList;
if (CC == CallingConv::PreserveNone) return CSR_NoneRegs_SaveList;
if (CC == CallingConv::PreserveMost) return CSR_MostRegs_SaveList;
switch (Subtarget.getTargetABI()) {
case ABI_ILP32S: case ABI_LP64S: return CSR_ILP32S_LP64S_SaveList;
case ABI_ILP32F: case ABI_LP64F: return CSR_ILP32F_LP64F_SaveList;
case ABI_ILP32D: case ABI_LP64D: return CSR_ILP32D_LP64D_SaveList;
}
```

关键结论：

- **软浮点 ABI 只保存整数 CSR**：`$ra` + `$s0`–`$s8`（`R22`–`R31`）。
- **单精度 ABI 额外保存 `$f24`–`$f31`**（32 位视图）。
- **双精度 ABI 保存 `$f24_64`–`$f31_64`**（64 位视图）。
- **`PreserveNone` 只保存 `$ra`/`$fp`**；`PreserveMost` 保存 `$ra`、`$s0`–`$s8`、`$a0`–`$a7`、`$t4`–`$t7`（`R16`–`R19`）。

`getNoPreservedMask()` 返回 `CSR_NoRegs_RegMask`，用于 `GHC` 调用约定（不保留任何寄存器）。

---

## 5.6 从 prologue 反推 CSR

源码：

```llvm
declare void @g()
define i64 @h(i64 %a) {
  %x = alloca i64
  store i64 %a, ptr %x
  call void @g()
  %v = load i64, ptr %x
  ret i64 %v
}
```

```bash
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 /tmp/frame.ll -o -
```

实测（节选）：

```asm
h:
	addi.d	$sp, $sp, -16
	.cfi_def_cfa_offset 16
	st.d	$ra, $sp, 8
	.cfi_offset 1, -8
	st.d	$a0, $sp, 0
	pcaddu18i	$ra, %call36(g)
	jirl	$ra, $ra, 0
	ld.d	$a0, $sp, 0
	ld.d	$ra, $sp, 8
	addi.d	$sp, $sp, 16
	ret
```

观察：

- `st.d $ra, $sp, 8`：`$ra` 是 CSR（`CSR_*` 都含 `R1`），因为函数调用会覆盖它。
- `$a0` 被存到栈上：它不是 callee-saved，但这里跨调用活跃，所以必须 spill。
- 没有保存 `$s0`–`$s8`：这个函数没用到它们。
- 栈大小 16 字节：8 字节 `$ra` + 8 字节 `$a0`，按 16 字节对齐。

如果把 `-mabi` 换成 `lp64s`，CSR 集合不变（整数部分相同）；但如果函数用了 `$f24`，`lp64s` 不会保存它，`lp64d` 会。这就是 ABI 与 CSR 表的直接关系。

---

## 5.7 怎么观察寄存器分配

```bash
# 1. 看 RA 后的 MIR（物理寄存器）
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
  -stop-after=greedy /tmp/frame.ll -o - 2>/dev/null | grep -A20 '^body:'

# 2. 看最终汇编
$T/bin/llc -mtriple=loongarch64-unknown-linux-gnu -O2 /tmp/frame.ll -o -

# 3. 看 CSR 表（生成的 inc）
grep -n 'CSR_ILP32D_LP64D' $T/lib/Target/LoongArch/LoongArchGenRegisterInfo.inc | head
```

MIR 里物理寄存器用 `$r4`/`$a0` 两种写法都可能出现；`-print-after=greedy` 之后主要看 `$r*`。

---

## 5.8 本章证据

| 结论 | 源码/命令 |
|---|---|
| 寄存器宽度链 | `LoongArchRegisterInfo.td` 的 `LoongArchReg32/64/128/256` |
| GPR 分配顺序 | `def GPR` 的注释与列表 |
| 保留寄存器 | `LoongArchRegisterInfo::getReservedRegs()` |
| FP 别名 | `F0`–`F31` 的 `AltNames` |
| CSR 集合 | `LoongArchCallingConv.td` |
| ABI→CSR | `getCalleeSavedRegs()` |
| prologue 实例 | `llc -O2 /tmp/frame.ll` |

---

## 5.9 练习

1. 写一个使用 `$s0`–`$s3` 的函数（例如 4 个跨调用活跃的局部变量），确认 prologue 保存了它们；再提高优化级别，看能否减少保存。
2. 用 `-mabi=lp64s` 和 `-mabi=lp64d` 编译同一个使用 `double` 的函数，比较 prologue 的浮点保存。
3. 解释为什么 `GPRJR` 要排除 `$ra`；构造一个间接尾调用，看编译器选了哪个寄存器。
4. 读 `getReservedRegs()`，说明 `$r21` 为什么不可分配；如果强行分配会发生什么？

---

## 5.10 下一步

下一章进入 MC 层：从 `Fmt3R` 的位域到 `MCCodeEmitter`、fixup、`AsmBackend`，把 `add.d` 的 32 位字和 `%pc_hi20` 的重定位都走一遍。
