# Ch 09 — 调用约定 lowering：参数从哪来，返回值往哪去

> 本章目标：把 `CC_LoongArch` 这个 300 行的 C++ 函数读成一张“分配表”；能解释 9 个 `i64` 参数为什么第 9 个在栈上、`sret` 为什么只是 `$a0`、varargs 为什么平白多出 80 字节栈帧。

---

## 9.1 一个反直觉的事实：LoongArch 的 CC 规则不在 `.td` 里

X86/AArch64 的 `CallingConv.td` 里通常有一堆 `CCIfType`/`CCAssignToReg` 规则。LoongArch 不是：

```tablegen
// LoongArchCallingConv.td 全文只有 CSR 定义
def CSR_ILP32S_LP64S : CalleeSavedRegs<(add R1, (sequence "R%u", 22, 31))>;
def CSR_ILP32F_LP64F : CalleeSavedRegs<(add CSR_ILP32S_LP64S, (sequence "F%u", 24, 31))>;
def CSR_ILP32D_LP64D : CalleeSavedRegs<(add CSR_ILP32S_LP64S, (sequence "F%u_64", 24, 31))>;
```

真正的分配逻辑全在 `LoongArchISelLowering.cpp` 的 `CC_LoongArch()`（`:9822`）。它被三处调用：

| 调用点 | 场景 |
|---|---|
| `analyzeInputArgs`（`:10004`） | callee 侧：形参怎么收 |
| `analyzeOutputArgs`（`:10027`） | caller 侧：实参怎么传；以及返回值怎么给 |
| `CanLowerReturn`/`LowerReturn`（`:10880`、`:10897`） | 返回值是否可直接返回 |

**为什么这很重要**：如果你要改 ABI 行为（比如某个类型该走寄存器还是栈），改的是 C++ 里的 `switch` 和 `State.AllocateStack`，不是 `.td`。reviewer 也会按这个路径看你的补丁。

---

## 9.2 寄存器池

```cpp
const MCPhysReg ArgGPRs[] = {R4, R5, R6, R7, R8, R9, R10, R11};   // $a0-$a7
const MCPhysReg ArgFPR32s[] = {F0..F7};                            // $fa0-$fa7
const MCPhysReg ArgFPR64s[] = {F0_64..F7_64};
const MCPhysReg ArgVRs[] = {VR0..VR7};                             // LSX
const MCPhysReg ArgXRs[] = {XR0..XR7};                             // LASX
```

要点：

- 整数参数 **8 个**（`$a0`–`$a7` = `$r4`–`$r11`）。
- 浮点参数 **8 个**（`$f0`–`$f7`），32/64 位共用同一批物理寄存器（`F0` 与 `F0_64` 是别名）。
- LSX/LASX 向量参数各 8 个（`$vr0`–`$vr7` / `$xr0`–`$xr7`）。
- 第 9 个整数参数开始走栈。

`PreserveNoneArgGPRs` 是另一套池子（`preserve_none` 调用约定用），包含 `R23`–`R30`、`R4`–`R20`——不要和默认池混。

---

## 9.3 实测：9 个 `i64` 参数

```llvm
define i64 @many(i64 %a0, i64 %a1, i64 %a2, i64 %a3, i64 %a4,
                 i64 %a5, i64 %a6, i64 %a7, i64 %a8, i64 %a9) {
  %s = add i64 %a8, %a9
  ret i64 %s
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
many:
	.cfi_startproc
# %bb.0:
	ld.d	$a0, $sp, 8
	ld.d	$a1, $sp, 0
	add.d	$a0, $a1, $a0
	ret
```

解读：

- `%a8` 在 `$sp+0`，`%a9` 在 `$sp+8`——**栈参数从 offset 0 开始，callee 直接读调用者的栈**（LoongArch 没有 x86 那种“home area/影子空间”）。
- 前 8 个参数根本没出现在函数体里：它们进了 `$a0`–`$a7`，而函数没用到，所以没有搬运指令。
- `add` 的操作数顺序被交换成 `add.d $a0, $a1, $a0`，因为 `%a8` 已经加载进 `$a0`、`%a9` 在 `$a1`；加法可交换，寄存器分配器选了代价最低的排列。

栈布局（callee 视角，`$sp` 指向返回地址之上、调用者分配的出参区）：

```text
$sp + 8   -> %a9
$sp + 0   -> %a8
```

---

## 9.4 `sret`：结构体返回只是“第 0 个参数”

ABI 规定：大于 2×GRLen 的返回值由调用者分配空间，并把指针作为**隐藏的第 0 个参数**传入。

```llvm
define void @sret(ptr sret({i64,i64,i64}) %p, i64 %x) {
  %p2 = getelementptr {i64,i64,i64}, ptr %p, i64 0, i32 0
  store i64 %x, ptr %p2
  ret void
}
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
sret:
	st.d	$a1, $a0, 0
	ret
```

`%p` 在 `$a0`（第 0 个整数参数），`%x` 在 `$a1`（第 1 个）。也就是说：**`sret` 在 CC 层没有任何特殊代码**，它只是让前端把指针放在参数列表最前面。`CC_LoongArch` 里的 `IsRet && ValNo > 1` 早退（`:9833`）处理的是“返回值被拆成超过两个值”的情况，不是 `sret`。

这条“`sret` 不是特例”的认知能省掉很多 debug 时间。

---

## 9.5 浮点参数：`UseGPRForFloat` 这个名字要看清楚

```cpp
// If passing a variadic argument, or if no FPR is available.
bool UseGPRForFloat = true;

switch (ABI) {
case ABI_ILP32F: case ABI_LP64F: case ABI_ILP32D: case ABI_LP64D:
  UseGPRForFloat = ArgFlags.isVarArg();
  break;
case ABI_ILP32S: case ABI_LP64S:
  break;
}
...
// FPR32 and FPR64 alias each other.
if (State.getFirstUnallocated(ArgFPR32s) == std::size(ArgFPR32s))
  UseGPRForFloat = true;
```

含义：

| 情况 | 浮点参数去哪 |
|---|---|
| `lp64s`（soft-float） | GPR（按位搬运，`BCvt`） |
| `lp64f` / `lp64d` 的普通参数 | FPR（`$fa0`–`$fa7`） |
| `lp64f` / `lp64d` 的 **varargs** 参数 | GPR |
| FPR 用尽 | **GPR**（不是直接走栈） |
| GPR 也用尽 | 栈 |

**关键**：FPR 用尽后是“**退回 GPR**”，不是“直接走栈”。源码里 `UseGPRForFloat = true` 就是这条回退。实测 9 个 `double`：

```llvm
define double @d9(double %d0, ..., double %d8) { ret double %d8 }
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
d9:
	movgr2fr.d	$fa0, $a0
	ret
```

前 8 个进 `$fa0`–`$fa7`，第 9 个（`%d8`）落在 `$a0`；返回值要放 `$fa0`，所以多了一条 `movgr2fr.d`（GPR → FPR）。

**varargs 的浮点参数一定走 GPR**，这是 ABI 要求（可变参数函数不知道调用者传了什么类型，只能用通用寄存器）。所以 `printf` 这类函数在 LoongArch 上浮点参数要先 `fmov`/`movgr2fr` 到整数寄存器。实测 `double @vdouble(double %x, ...)` 的保存区会把 `$a0`–`$a7` 全存一遍（见 9.6 的 `varargs` 同类输出）。

> 读这段代码时把三条路径分开看：`varargs` 强制 GPR；F/D ABI 非 varargs 用 FPR；FPR 用尽回退 GPR。很容易把第二条和第三条混成“FPR 用尽就走栈”，那是错的。

## 9.6 varargs 的实测：80 字节栈帧

```llvm
define i32 @varargs(i32 %n, ...) { ret i32 %n }
```

```console
$ llc -mtriple=loongarch64-unknown-linux-gnu -O2
varargs:
	addi.d	$sp, $sp, -80
	.cfi_def_cfa_offset 80
	st.d	$a7, $sp, 72
	st.d	$a6, $sp, 64
	st.d	$a5, $sp, 56
	st.d	$a4, $sp, 48
	st.d	$a3, $sp, 40
	st.d	$a2, $sp, 32
	st.d	$a1, $sp, 24
	addi.d	$sp, $sp, 80
	ret
```

为什么是 80 字节、为什么存 `$a1`–`$a7`：

- 可变参数函数需要一块**寄存器保存区**（register save area），让 `va_arg` 能像读栈一样顺序读。
- 已用的固定参数是 `$a0`（`%n`），所以只需保存 `$a1`–`$a7` 共 7 个 × 8 字节 = 56 字节。
- 加上对齐/预留，帧大小 80（`$a1` 存在 `sp+24`，说明保存区基址在 `sp+16` 左右；具体布局由 `LowerFormalArguments` 的 `VarArgSaveArea` 决定）。
- `cfi_def_cfa_offset 80` 让 unwinder 知道 CFA 变了。

**注意**：函数体完全没用 varargs，所以 `llc` 仍然生成了保存区——这是 ABI 强制的，不是优化能删的。这也解释了为什么“空 varargs 函数”也有 prologue。

---

## 9.7 LA32 上的 `i64`

Ch 08 末尾已经看到：

```llvm
define i64 @x() { ret i64 4294967296 }
```

```console
$ llc -mtriple=loongarch32-unknown-linux-gnu -O2
	ori	$a1, $zero, 1
	move	$a0, $zero
	ret
```

`i64` 在 LA32 上被拆成两个 GPR：低 32 位 `$a0`，高 32 位 `$a1`。这就是 `CC_LoongArchAssign2GRLen()`（`:9786`）的作用：把一个 `2*GRLen` 的值分配到两个连续寄存器，必要时用 `PendingLocs` 等第二个寄存器到位。

这也解释了 `LowerReturn` 里 `ValNo > 1` 的早退：一个返回值最多拆成两个值；超过两个就没法直接返回，必须 `sret`。

---

## 9.8 三个入口的分工

```text
caller (LowerCall)
  Outs[] ──analyzeOutputArgs──> CCState ──> 每条实参的 CCValAssign
                                            （Reg / Mem / 拆分）

callee (LowerFormalArguments)
  Ins[]  ──analyzeInputArgs───> CCState ──> COPY_FROM_REG / 从栈 load
```

`LowerFormalArguments`（`:10218`）拿到 `CCValAssign` 后做两种事：

- `isRegLoc()`：`CopyFromReg`，把物理寄存器变成虚拟寄存器；
- `isMemLoc()`：按 `getMemOffset()` 生成 `load`（LA64 是 `ld.d`，LA32 是 `ld.w` 对）。

`LowerCall`（`:10480`）则相反：把实参 `CopyToReg` 或 `store` 到出参区，再 `PseudoCALL`。`PseudoCALL` 在 MC 层展开成 `bl` 或 `pcaddu18i + jirl`（取决于距离和 `-mcmodel`）。

---

## 9.9 反直觉点

1. **`sret` 不是 CC 特殊路径**，只是指针放参数 0。
2. **栈参数从 `sp+0` 开始**，没有 home area；callee 直接读 caller 的栈。
3. **varargs 的浮点参数走 GPR**，即使 `-mabi=lp64d`。
4. **`UseGPRForFloat` 默认是 `true`**，只有 F/D ABI 的非 varargs 参数才置 `false`。变量名容易读反。
5. **2×GRLen 对齐规则**只对 varargs 生效：`i128` 这类参数要落在偶数号寄存器（跳过奇数号）。源码里那段 `RegIdx % 2 == 1` 就是干这个。
6. **`preserve_none` 是另一套寄存器池**，别拿默认的 `ArgGPRs` 去推。
7. **`LowerCall` 里 `PseudoCALL` 的距离判断在 MC 层**，不在 CC 层；`-mcmodel=large` 会影响展开成几条指令（Ch 11）。

---

## 9.10 本章证据

| 结论 | 源码/命令 |
|---|---|
| CC 规则在 C++ | `LoongArchISelLowering.cpp:9822` |
| 三个调用点 | `:10004`、`:10027`、`:10897` |
| 参数寄存器池 | `:9730-9763` |
| `preserve_none` 池 | `:9746` |
| 9 参数实测 | 本章 9.3 `llc` 输出 |
| `sret` 实测 | 本章 9.4 |
| varargs 实测 | 本章 9.6 |
| 2×GRLen 对齐 | `:9857-9868` |
| LA32 i64 拆分 | `CC_LoongArchAssign2GRLen`（`:9786`）+ Ch 08 实测 |
| `ValNo > 1` 早退 | `:9833` |

---

## 9.11 练习

1. 写一个收 10 个 `double` 的函数，用 `-mabi=lp64d` 编译，找出第 9、10 个参数在栈上的偏移。
2. 同一个函数用 `-mabi=lp64s` 编译，对比参数落在 GPR 还是 FPR，并解释 `BCvt` 的作用。
3. 写一个 `i128` 参数的函数，观察它是否占用两个连续寄存器，以及是否遵守“偶数号”规则。
4. 用 `-stop-after=loongarch-isel` 看 `LowerFormalArguments` 生成的 `COPY_FROM_REG` 和 `LOAD` 的顺序。
5. 找出 `LowerCall` 里处理 `sret` 的代码，确认它只是把指针当普通参数传。

---

## 9.12 延伸阅读

1. **通用调用约定的 `.td` 写法** — `llvm/docs/WritingAnLLVMBackend.md`
   - 读什么：`### Calling Conventions`（1490 行起）里的 `CCIfType`/`CCAssignToReg` 例子，以及 `RetCC_Sparc32` 的定义。
   - 学到什么：本章 9.1 的核心反直觉点是“LoongArch 的规则不在 `.td`”；这一节给出“常规后端长什么样”的基线，读完才能判断 LoongArch 为什么把 `CC_LoongArch` 写成 C++。
   - 前提：知道 `CallingConv.td` 里 `CallingConv<[...]>` 的基本语法。

2. **`CC_LoongArch()` 的分配状态机** — `llvm/lib/Target/LoongArch/LoongArchISelLowering.cpp`
   - 读什么：`CC_LoongArch()`（9822 行起）、`CC_LoongArchAssign2GRLen()`（9786 行起），以及 `analyzeInputArgs`/`analyzeOutputArgs` 两个调用点（10004、10027 行附近）。
   - 学到什么：本章 9.5 说“FPR 用尽回退 GPR”，源码里还包含 `PendingLocs`、2×GRLen 对齐和 `preserve_none` 池；这些分支只有直接读函数才能串起来。
   - 前提：读过本章 9.2 的寄存器池和 9.8 的三个入口。

3. **varargs 保存区的 lit 证据** — `llvm/test/CodeGen/LoongArch/vararg.ll`
   - 读什么：`va1` 的 `LA64-FPELIM` 与 `LA64-WITHFP` 两组 `CHECK`，重点看 `addi.d $sp, $sp, -80` 和 `st.d $a1`–`$a7` 的保存顺序。
   - 学到什么：本章 9.6 只给了一个 `llc` 片段；测试里同时跑了 `--frame-pointer=all`，可以对照看 `$fp` 是否改变保存区布局。
   - 前提：读过本章 9.6，知道 80 字节帧的来历。

4. **RISC-V 后端的同题对照** — `compilers/riscv_backend/README.md`（相对 `/home/ubuntu/Learning-with-Claude`）
   - 读什么：Part 3 的 Ch 12《调用约定》，覆盖 `ilp32`/`lp64`/`lp64d`、参数寄存器、`sret`/`byval`/varargs。
   - 学到什么：本章只讲 LoongArch 的 C++ 实现；对照 RISC-V 的课程可以看到另一套 ABI（同样有浮点 ABI 变体和 varargs）如何组织同一组问题，便于判断哪些是 ABI 共性、哪些是 LoongArch 特有。
   - 前提：读过本章 9.3–9.6。

## 9.13 下一步

下一章看栈帧：`$fp` 什么时候出现、callee-saved 寄存器怎么存、`adjustReg` 为什么对大栈偏移特殊处理，以及 `shrink-wrapping` 在 LoongArch 上是否启用。
