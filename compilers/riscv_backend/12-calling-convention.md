# Ch 12 调用约定：a0-a7、fa0-fa7 与它们背后的规则

> 目标：把 RISC-V 调用约定拆成“ABI 家族选择、整数与浮点各自的寄存器计数器、返回值与 sret、byval 拷贝、varargs 对齐”五块。读完后你能解释为什么第 9 个 `double` 会出现在 `a0`、为什么可变参数的 `double` 不进 `fa0`、为什么 `byval` 的拷贝发生在调用点而不是被调函数，以及 `-target-abi` 写错时会发生什么。

> 环境：Windows；LLVM 23.1.4；PowerShell。工具目录与统一入口同第 08 章。源码依据来自 `.scratch/llvm-23.1.4-riscv-td/llvm/lib/Target/RISCV`。

> 约定：标“实测”的片段来自本机重跑；“源码依据”给文件名与符号名；需要重编译 llc 才能验证的结论标 `guided`。

## 为什么

调用约定是“两个函数之间的二进制合同”。它不写进函数体，却决定了调用者把值放在哪、被调者从哪里读、返回值从哪里取。合同一旦不一致，链接器未必报错，程序却会读到垃圾。

RISC-V 的调用约定有一个很容易误导人的结构。打开 `RISCVCallingConv.td`，你会看到 101 行、全是 `CalleeSavedRegs`，没有一行 `CCIfType` 或 `CCAssignToReg`。文件第 13-14 行的注释还写着“调用约定由 `RISCVISelLowering.cpp` 中的自定义代码 `CC_RISCV` 处理”。但 23.1.4 的实际实现已经搬到 `RISCVCallingConv.cpp`：`CC_RISCV`、`RetCC_RISCV`、`CC_RISCV_Impl`、`CC_RISCV_FastCC`、`CC_RISCV_GHC` 都在那里。只读 `.td` 会得到“RISC-V 没有参数分配规则”的错误结论。

本机实测能直接看到几条反直觉的规则：

- 9 个 `double` 在 `lp64d` 下，前 8 个在 `fa0-fa7`，第 9 个不在栈上，而在 `a0`，随后 `fmv.d.x fa4, a0` 搬回浮点寄存器。
- 可变参数调用 `vsum(i32 1, double 1.5)` 在 `lp64d` 下，`double` 放在 `a1`，不在 `fa0`。
- `byval` 的大结构体，被调函数只拿到一个 `a0` 指针；40 字节的拷贝发生在调用点，栈帧因此从 16 字节涨到 48 字节。
- `-target-abi=lp64d` 但没开 `+d` 时，llc 只打印一条 warning，然后**忽略**这个 ABI，静默回退到 `lp64f`。
- 两个目标文件如果浮点 ABI 不同，`ld.lld` 会报 `cannot link object files with different floating-point ABI`；依据是 ELF header 的 `e_flags`，不是 `.riscv.attributes` 的字符串。

这些现象都不是“实现 bug”，而是 psABI 与 LLVM 实现共同作用的结果。逐个拆开之后，调用约定就变成一组可以手算的计数器。

## 主线

### 12.1 两个文件的分工：`.td` 只有 callee-saved，参数规则在 `.cpp`

`RISCVCallingConv.td` 的真实内容是四组基础 `CalleeSavedRegs` 加若干特殊集合：

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

这里没有参数寄存器列表。参数规则在 `RISCVCallingConv.cpp`，入口有三个：

```cpp
bool llvm::CC_RISCV(unsigned ValNo, MVT ValVT, MVT LocVT,
                    CCValAssign::LocInfo LocInfo, ISD::ArgFlagsTy ArgFlags,
                    Type *OrigTy, CCState &State) {
  if (State.getCallingConv() == CallingConv::GHC)
    return CC_RISCV_GHC(...);
  if (State.getCallingConv() == CallingConv::Fast)
    return CC_RISCV_FastCC(...);
  return CC_RISCV_Impl(..., /*IsRet=*/false);
}

bool llvm::RetCC_RISCV(...) {
  return CC_RISCV_Impl(..., /*IsRet=*/true);
}
```

真正的规则集中在 `CC_RISCV_Impl`：它多了 `bool IsRet`，返回 true 表示“这个值不能按当前规则分配，交给调用者用间接方式处理”。调用点在 `RISCVISelLowering.cpp`：

```text
25487: CCInfo.AnalyzeFormalArguments(Ins, CC_RISCV);
25724: ArgCCInfo.AnalyzeCallOperands(Outs, CC_RISCV);
26157: RetCCInfo.AnalyzeFormalArguments(Ins, RetCC_RISCV);
26208: return CCInfo.CheckReturn(Outs, RetCC_RISCV);
26226: CCInfo.AnalyzeCallOperands(Outs, RetCC_RISCV);
```

这五行覆盖了“函数入口收参数、调用点传参数、函数出口收返回值、调用点检查返回值”四条路径。读调用约定时，先确定自己在看哪一条，再去对 `CC_RISCV_Impl` 里的分支。

`CC_RISCV_Impl` 的前几段还有几个“非典型”分支，考试里常被忽略：

- `ArgFlags.isNest()`：静态链参数不进普通参数寄存器，而是 `t2`（x7）；开启控制流保护时用 `t3`（x28）。ILP32E/LP64E 加控制流保护直接 fatal。
- `IsRet && ValNo > 1`：返回值超过两个寄存器宽度的部分不能再直接返回，`return true` 让上层走 sret。向量是例外，向量有自己的返回寄存器。
- P 扩展的双宽打包类型只允许返回一个。

这几条说明 `CC_RISCV_Impl` 不是“只做寄存器分配”，它同时承担“什么时候必须改成间接”的判定。

### 12.2 ABI 家族：XLEN、FLEN 与两个布尔开关

RISC-V 的 ABI 名字由“整数宽度 + 浮点参数传递方式”组成：

| ABI | XLEN | 浮点参数 | 典型三元组 |
|---|---|---|---|
| `ilp32` | 32 | 软浮点，全部走 GPR | `riscv32` |
| `ilp32f` | 32 | `float` 走 FPR，`double` 走 GPR 对 | `riscv32 +f` |
| `ilp32d` | 32 | `float`/`double` 都走 FPR | `riscv32 +d` |
| `lp64` | 64 | 软浮点，全部走 GPR | `riscv64` |
| `lp64f` | 64 | `float` 走 FPR，`double` 走 GPR | `riscv64 +f` |
| `lp64d` | 64 | `float`/`double` 都走 FPR | `riscv64 +d` |

`CC_RISCV_Impl` 用两个布尔量表达这张表：

```cpp
bool AllowFPRForF16_F32 = false;
bool AllowFPRForF64 = false;

switch (ABI) {
case RISCVABI::ABI_ILP32:
case RISCVABI::ABI_ILP32E:
case RISCVABI::ABI_LP64:
case RISCVABI::ABI_LP64E:
  break;
case RISCVABI::ABI_ILP32D:
case RISCVABI::ABI_LP64D:
  AllowFPRForF64 = !ArgFlags.isVarArg();
  [[fallthrough]];
case RISCVABI::ABI_ILP32F:
case RISCVABI::ABI_LP64F:
  AllowFPRForF16_F32 = !ArgFlags.isVarArg();
  break;
}
```

`fallthrough` 是关键：D 家族先打开 F64，再落进 F 家族打开 F16/F32。所以 `lp64d` 两个布尔都为真，`lp64f` 只有 F16/F32 为真，`lp64` 两个都为假。

`!ArgFlags.isVarArg()` 同样关键：可变参数不走 FPR。哪怕 ABI 是 `lp64d`，`...` 里的 `double` 也按整数规则传。第 12.6 节会用实测证明。

参数寄存器数组在同一个文件里：

```cpp
static const MCPhysReg ArgFPR16s[] = {F10_H, F11_H, ..., F17_H};
static const MCPhysReg ArgFPR32s[] = {F10_F, F11_F, ..., F17_F};
static const MCPhysReg ArgFPR64s[] = {F10_D, F11_D, ..., F17_D};
```

从 `F10` 开始，对应 `fa0`。`getArgGPRs` 返回 `X10..X17`（`a0-a7`）；如果是 `ILP32E/LP64E`，缩到 `X10..X15`（`a0-a5`）。`getArgFPRs` 先看 ABI 是否软浮点，软浮点返回空；再看 `Q`/`D`/`F` 选 128/64/32 位数组。

这两套数组是独立的分配器。`a0-a7` 用满不影响 `fa0-fa7`，反之亦然。`mixed(i64, double, i64, double)` 的实测正好说明这一点：两个整数在 `a0/a1`，两个 `double` 在 `fa0/fa1`。

`e_flags` 是 ABI 的另一个出口。`RISCVTargetELFStreamer::finish` 按 ABI 设置：

```cpp
case RISCVABI::ABI_ILP32F:
case RISCVABI::ABI_LP64F:
  EFlags |= ELF::EF_RISCV_FLOAT_ABI_SINGLE;
  break;
case RISCVABI::ABI_ILP32D:
case RISCVABI::ABI_LP64D:
  EFlags |= ELF::EF_RISCV_FLOAT_ABI_DOUBLE;
  break;
```

同时还会按特性加 `EF_RISCV_RVC`、`EF_RISCV_TSO`，按 ABI 加 `EF_RISCV_RVE`。`llvm-readelf -h` 把 `0x4` 显示成 `double-float ABI`，把 `0x0` 显示成软浮点。链接器就是靠这个字段拒绝混链的。

### 12.3 参数分配：两个计数器与“用尽之后”

先看整数。9 个 `i64` 参数求和：

```llvm
define i64 @sum9(i64 %a, i64 %b, i64 %c, i64 %d, i64 %e,
                 i64 %f, i64 %g, i64 %h, i64 %i) {
  %s1 = add i64 %a, %b
  %s2 = add i64 %s1, %c
  %s3 = add i64 %s2, %d
  %s4 = add i64 %s3, %e
  %s5 = add i64 %s4, %f
  %s6 = add i64 %s5, %g
  %s7 = add i64 %s6, %h
  %s8 = add i64 %s7, %i
  ret i64 %s8
}
```

`llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -target-abi=lp64d -O2` 的实测：

```asm
sum9:
	ld	t0, 0(sp)
	add	a0, a0, a1
	add	a2, a2, a3
	add	a4, a4, a5
	add	a0, a0, a2
	add	a4, a4, a6
	add	a0, a0, a4
	add	a0, a0, a7
	add	a0, a0, t0
	ret
```

前 8 个在 `a0-a7`，第 9 个在调用者的栈参数区，偏移 0。`sum9` 是叶子函数，没有调整 `sp`，所以直接 `ld t0, 0(sp)` 就能读到。这里没有 `sd`，因为参数已经由调用者放好。

再看浮点。9 个 `double` 求和，同样 `lp64d`：

```asm
sumd9:
	fadd.d	fa1, fa0, fa1
	fadd.d	fa2, fa1, fa2
	fadd.d	fa3, fa2, fa3
	fadd.d	fa4, fa3, fa4
	fadd.d	fa5, fa4, fa5
	fadd.d	fa5, fa5, fa6
	fadd.d	fa5, fa5, fa7
	fmv.d.x	fa4, a0
	fadd.d	fa0, fa5, fa4
	ret
```

前 8 个在 `fa0-fa7`。第 9 个没有走栈，而是走了 `CC_RISCV_Impl` 的“Bitcast FP to GPR”分支：

```cpp
// Bitcast FP to GPR if we can use a GPR register.
if ((XLen == 32 && LocVT == MVT::f32) || (XLen == 64 && LocVT == MVT::f64)) {
  if (MCRegister Reg = State.AllocateReg(ArgGPRs)) {
    LocVT = XLenVT;
    LocInfo = CCValAssign::BCvt;
    ...
  }
}
```

FPR 用尽后，`double` 被当作 64 位整数放进 `a0`，在函数入口用 `fmv.d.x fa4, a0` 搬回浮点寄存器。所以“FPR 用完就溢出到栈”是错的：只要 GPR 还有空位，就先借 GPR。

`lp64f` 与 `lp64` 下的 `sumd9` 完全不同。两种 ABI 的实测都调用 `__adddf3`，因为 `double` 要么放不进 32 位 FPR（lp64f），要么根本没有 FPR 参数（lp64）。`lp64f` 的帧是 64 字节，保存 `ra` 与 `s0-s6`，第 9 个 `double` 从 `64(sp)` 读：

```asm
	ld	s6, 64(sp)
	call	__adddf3
```

`sumf9`（9 个 `float`）能区分 F 与 D 家族。`lp64d` 与 `lp64f` 的输出完全相同：

```asm
sumf9:
	fadd.s	fa1, fa0, fa1
	...
	fadd.s	fa5, fa5, fa6
	fmv.w.x	fa4, a0
	fadd.s	fa5, fa5, fa7
	fadd.s	fa0, fa5, fa4
	ret
```

第 9 个 `float` 在 `a0`，用 `fmv.w.x` 搬回来。`lp64`（软浮点）则完全不同，出现 64 字节栈帧与大量 `s0-s6` 保存。

`mixed` 验证两个计数器互不干扰：

```llvm
define double @mixed(i64 %a, double %x, i64 %b, double %y) {
  %ai = sitofp i64 %a to double
  %bi = sitofp i64 %b to double
  %s1 = fadd double %ai, %x
  %s2 = fadd double %bi, %y
  %r = fadd double %s1, %s2
  ret double %r
}
```

`lp64d` 实测：

```asm
mixed:
	fcvt.d.l	fa5, a0
	fcvt.d.l	fa4, a1
	fadd.d	fa5, fa5, fa0
	fadd.d	fa4, fa4, fa1
	fadd.d	fa0, fa5, fa4
	ret
```

`%a` 在 `a0`，`%b` 在 `a1`；`%x` 在 `fa0`，`%y` 在 `fa1`。整数与浮点各自从 0 开始编号，没有“第 3 个参数所以用 a2”这种混编规则。

RV32 上 `double` 的参数传递还有一层：`f64` 要拆成两个 32 位半。`riscv32 -target-abi=ilp32d` 的 `sumd9` 第 9 个参数实测：

```asm
	sw	a0, 8(sp)
	sw	a1, 12(sp)
	fld	fa4, 8(sp)
```

`a0/a1` 组成一个 64 位对，先存到栈上再 `fld` 回来。`fa0-fa7` 已经全部用于前 8 个参数，所以第 9 个走“GPR 对”分支；`a0/a1` 是当时第一对可用的 GPR。这个细节在跨 32/64 位移植时最容易出错。
### 12.4 返回值：a0/a1、fa0/fa1 与 sret

两个 `i64` 的返回值：

```llvm
define %pair @ret_pair_i(i64 %a, i64 %b) {
  %x = add i64 %a, %b
  %y = sub i64 %a, %b
  ...
}
```

`lp64d` 实测：

```asm
ret_pair_i:
	add	a2, a0, a1
	sub	a1, a0, a1
	mv	a0, a2
	ret
```

第一个字段在 `a0`，第二个在 `a1`。注意中间用了 `a2` 做临时：`a1` 先被写成第二个字段，第一个字段的和暂存在 `a2`，最后 `mv a0, a2`。这说明参数寄存器在返回值确定之前可以当临时用。

两个 `double` 的返回值：

```asm
ret_pair_d:
	fadd.d	fa5, fa0, fa1
	fsub.d	fa1, fa0, fa1
	fmv.d	fa0, fa5
	ret
```

第一个字段在 `fa0`，第二个在 `fa1`；`fa5` 是临时。与整数的模式完全平行。

超过两个寄存器宽度的返回值改走 sret。`%big = { i64, i64, i64, i64, i64 }` 的 `ret_big` 实测：

```asm
ret_big:
	sd	a5, 32(a0)
	sd	a1, 0(a0)
	sd	a2, 8(a0)
	sd	a3, 16(a0)
	sd	a4, 24(a0)
	ret
```

`a0` 是隐藏的 sret 指针，五个字段分别来自 `a1-a5`，依次写到 `0/8/16/24/32(a0)`。函数本身不分配栈，也不需要返回寄存器；调用者负责提供缓冲区。源码依据是 `CC_RISCV_Impl` 开头的：

```cpp
// Any return value split in to more than two values can't be returned
// directly. Vectors are returned via the available vector registers.
if ((!LocVT.isVector() || Subtarget.isPExtPackedType(LocVT)) && IsRet &&
    ValNo > 1)
  return true;
```

`ValNo > 1` 的意思是“第三个及以后的返回值片段”无法直接返回，上层据此把整个返回改成 sret。`RetCC_RISCV` 只是把 `IsRet` 置真后复用同一套逻辑，所以“参数规则”和“返回值规则”共享一份代码，差别只在几个 `IsRet` 分支。

一个容易忽略的点：sret 指针占用的是 `a0`，不是额外寄存器。于是“返回大结构体”和“第一个参数”竞争同一个位置。`ret_big` 的五个输入从 `a1` 开始，就是因为 `a0` 被 sret 拿走了。

### 12.5 聚合类型：byval 的拷贝在调用点

先用 Clang 看“C 语言里按值传大结构体”的真实 ABI：

```c
struct big { long a,b,c,d,e; };
long take_big(struct big v) { return v.a + v.b + v.c + v.d + v.e; }
long call_big(struct big *p) { return take_big(*p); }
```

`clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S` 的实测：

```asm
take_big:
	ld	a1, 0(a0)
	ld	a2, 8(a0)
	ld	a3, 16(a0)
	ld	a4, 24(a0)
	ld	a0, 32(a0)
	add	a1, a1, a2
	add	a3, a3, a4
	add	a1, a1, a3
	add	a0, a0, a1
	ret
```

`take_big` 只拿到 `a0` 一个指针，然后从 `0(a0)` 读到 `32(a0)`。这就是 psABI 的“大于 2×XLEN 的结构体间接传递”：调用者准备一份副本，把副本地址放进 `a0`。Clang 生成的 IR 也直接体现了这一点：

```llvm
define dso_local i64 @take_big(ptr nofree noundef readonly align 8 captures(none) dead_on_return %0)
```

注意这是 Clang 已经做完 ABI lowering 之后的 IR：`struct big` 已经变成一个 `ptr` 参数。

如果绕过 Clang，直接写“按值传 `%big`”的 IR，会得到不同的形状。实测：

```llvm
define %big @grow(%big %v) {
  %f0 = extractvalue %big %v, 0
  %f4 = extractvalue %big %v, 4
  %r = insertvalue %big %v, i64 %f0, 4
  %r2 = insertvalue %big %r, i64 %f4, 0
  ret %big %r2
}
```

`lp64d` 下 `grow` 的实测：

```asm
grow:
	sd	a1, 32(a0)
	sd	a5, 0(a0)
	sd	a2, 8(a0)
	sd	a3, 16(a0)
	sd	a4, 24(a0)
	ret
```

`sret` 指针在 `a0`，五个字段在 `a1-a5`。这与 `take_big` 的“一个指针”完全不同：LLVM 把按值聚合展开成了 5 个 `i64`，再按普通标量规则分配。也就是说，**手写 IR 测出来的“结构体 ABI”不一定是 C 的 ABI**。测试 psABI 时，要么用 Clang 生成 IR/汇编，要么显式写 `byval`/`sret` 属性。

`byval` 的语义是“被调者拿到一份可以随意修改的副本”。实测被调函数：

```llvm
define i64 @byval_write(ptr byval(%big) align 8 %p) {
  %f0 = load i64, ptr %p
  store i64 0, ptr %p
  ret i64 %f0
}
```

```asm
byval_write:
	mv	a1, a0
	ld	a0, 0(a0)
	sd	zero, 0(a1)
	ret
```

被调函数没有做 40 字节拷贝，只是通过 `a0` 写回。拷贝在哪？在调用点。实测调用者：

```llvm
@g = dso_local global %big zeroinitializer, align 8
declare i64 @byval_read(ptr byval(%big) align 8)
define i64 @caller_byval() {
  %r = call i64 @byval_read(ptr byval(%big) align 8 @g)
  ret i64 %r
}
```

```asm
caller_byval:
	addi	sp, sp, -48
	sd	ra, 40(sp)
	lui	a0, %hi(g)
	addi	a0, a0, %lo(g)
	ld	a1, 32(a0)
	sd	a1, 32(sp)
	ld	a1, 24(a0)
	sd	a1, 24(sp)
	ld	a1, 16(a0)
	sd	a1, 16(sp)
	ld	a1, 8(a0)
	sd	a1, 8(sp)
	ld	a0, 0(a0)
	sd	a0, 0(sp)
	mv	a0, sp
	call	byval_read
	ld	ra, 40(sp)
	addi	sp, sp, 48
	ret
```

40 字节从 `g` 复制到 `0(sp)-32(sp)`，然后 `mv a0, sp` 把副本地址传进去。栈帧 48 字节 = 40 字节副本 + 8 字节 `ra`，且 48 满足 16 字节对齐。这个例子把“byval 的拷贝在调用点”钉死了：被调者只看到指针，看不到原始全局变量。

### 12.6 varargs：寄存器保存区与 8 字节对齐

可变参数函数要在入口把参数寄存器存到栈上，`va_start` 才能按顺序取。实测：

```llvm
define i64 @vararg(i32 %n, ...) {
  ret i64 0
}
```

```asm
vararg:
	addi	sp, sp, -64
	li	a0, 0
	sd	a5, 40(sp)
	sd	a6, 48(sp)
	sd	a7, 56(sp)
	sd	a1, 8(sp)
	sd	a2, 16(sp)
	sd	a3, 24(sp)
	sd	a4, 32(sp)
	addi	sp, sp, 64
	ret
```

`a1-a7` 被存到 `8(sp)-56(sp)`，每个 8 字节。`a0` 是命名参数 `%n`，函数体里被 `li a0, 0` 覆盖，所以不需要保存。这个函数是叶子，没有保存 `ra`。

真实带 `va_start` 的函数也是同一模式。用 Clang 编译 `double sumv(int n, ...)`（`--target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O1 -S`），入口实测：

```asm
sumv:
	addi	sp, sp, -80
	sd	a5, 56(sp)
	sd	a6, 64(sp)
	sd	a7, 72(sp)
	addi	a5, sp, 24
	sd	a1, 24(sp)
	sd	a2, 32(sp)
	sd	a3, 40(sp)
	sd	a4, 48(sp)
	sd	a5, 8(sp)
```

它只保存 `a1-a7`，没有保存 `fa0-fa7`，因为可变参数的 `double` 按 GPR 传；也没有保存 `ra`，因为这个函数是叶子。“先把参数寄存器落栈”的模式不变，具体保存哪些寄存器由 ABI 与函数是否调用别人决定。

调用方的规则更反直觉。`lp64d` 下：

```llvm
declare double @vsum(i32, ...)
define double @call_vsum() {
  %r = call double (i32, ...) @vsum(i32 1, double 1.5)
  ret double %r
}
```

```asm
call_vsum:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	li	a1, 2047
	slli	a1, a1, 51
	li	a0, 1
	call	vsum
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

`1.5` 的 IEEE754 位模式是 `0x3FF8000000000000`，也就是 `2047 << 51`。它被放进 `a1`，而不是 `fa0`。`a0` 是命名参数 `1`。原因在 12.2 节已经给出：`AllowFPRForF64 = !ArgFlags.isVarArg()`，可变参数的 `double` 不走 FPR。

RV32 上还有一条对齐规则。实测 `riscv32 -target-abi=ilp32`：

```llvm
declare double @vsum(i32, ...)
define double @call_vsum32() {
  %r = call double (i32, ...) @vsum(i32 1, double 1.5)
  ret double %r
}
declare double @vsum2(i32, i32, ...)
define double @call_vsum32b() {
  %r = call double (i32, i32, ...) @vsum2(i32 1, i32 2, double 1.5)
  ret double %r
}
```

```asm
call_vsum32:
	li	a0, 1
	lui	a3, 262016
	li	a2, 0
	call	vsum

call_vsum32b:
	li	a0, 1
	li	a1, 2
	lui	a3, 262016
	li	a2, 0
	call	vsum2
```

第一个调用里，`a0` 放了 `n=1`，下一个空闲寄存器是 `a1`（奇数）。但 `double` 要求 8 字节对齐，于是跳过 `a1`，放到 `a2/a3`。第二个调用里，`a0=1`、`a1=2`，下一个空闲是 `a2`（偶数），不需要跳过。`lui a3, 262016` 是 `0x3FF80000` 的高半，`li a2, 0` 是低半。

源码依据（`CC_RISCV_Impl`）：

```cpp
unsigned TwoXLenInBytes = (2 * XLen) / 8;
if (ArgFlags.isVarArg() && ArgFlags.getNonZeroOrigAlign() == TwoXLenInBytes &&
    DL.getTypeAllocSize(OrigTy) == TwoXLenInBytes &&
    ABI != RISCVABI::ABI_ILP32E) {
  unsigned RegIdx = State.getFirstUnallocated(ArgGPRs);
  if (RegIdx != std::size(ArgGPRs) && RegIdx % 2 == 1)
    State.AllocateReg(ArgGPRs);
}
```

三个条件同时成立才跳过：可变参数、原始对齐等于 2×XLEN、类型分配大小等于 2×XLEN。RV64 的 2×XLEN 是 16 字节，所以 `double`（8 字节）不触发；RV32 的 2×XLEN 是 8 字节，所以 `double` 触发。ILP32E 被显式排除，注释说明这是为了兼容 GCC 当前行为。

### 12.7 失败与诊断：warning 不是 error

`-target-abi` 的错误大多不会让 llc 退出非零。本机实测五组：

```text
llc -mtriple=riscv64 -target-abi=ilp32
  32-bit ABIs are not supported for 64-bit targets (ignoring target-abi)

llc -mtriple=riscv32 -target-abi=lp64
  64-bit ABIs are not supported for 32-bit targets (ignoring target-abi)

llc -mtriple=riscv64 -target-abi=lp64q
  'lp64q' is not a recognized ABI for this target (ignoring target-abi)

llc -mtriple=riscv64 -target-abi=foo
  'foo' is not a recognized ABI for this target (ignoring target-abi)

llc -mtriple=riscv64 -mattr=+m,+a,+f -target-abi=lp64d
  Hard-float 'd' ABI can't be used for a target that doesn't support
  the D instruction set extension (ignoring target-abi)
```

五组的 `exit` 都是 0。“ignoring target-abi” 是字面意思：后端丢掉你给的 ABI，用目标与特性推导出的默认值继续生成代码。最后一组尤其危险：`+f` 可用，于是回退到 `lp64f`，`float` 仍然进 `fa0`，`double` 变成 `__adddf3` 软浮点。你从命令行看不出代码已经换了 ABI，只能从输出或 warning 发现。

第二道防线是链接器。用 `lp64` 与 `lp64d` 各编一个对象：

```powershell
llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -target-abi=lp64  -O2 -filetype=obj callee.ll -o callee-lp64.o
llc -mtriple=riscv64 "-mattr=+m,+a,+f,+d" -target-abi=lp64d -O2 -filetype=obj caller.ll -o caller-lp64d.o
ld.lld -shared caller-lp64d.o callee-lp64.o -o mismatch.so
```

`ld.lld` 的真实输出：

```text
ld.lld: error: callee-lp64.o: cannot link object files with different
floating-point ABI from caller-lp64d.o
```

依据是 `e_flags`。实测：

```text
caller-lp64d.o  Flags: 0x4, double-float ABI
callee-lp64.o   Flags: 0x0
```

`0x4` 是 `EF_RISCV_FLOAT_ABI_DOUBLE`，`0x0` 是软浮点。这个检查只看 `e_flags`，不看 `.riscv.attributes` 的 arch 字符串，也不看符号名。

第三道防线是架构不匹配。把 `riscv32` 与 `riscv64` 的对象混链：

```text
ld.lld: error: m32.o is incompatible with m64.o
```

这条来自 ELF machine/class 与属性检查，与浮点 ABI 是两套独立机制。

第四种情况最隐蔽：手写汇编可以用 `.attribute` 声称自己是 D ABI，但函数体仍按软浮点读写。实测：

```asm
	.attribute 5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2"
	.text
	.globl callee
callee:
	fcvt.d.l	fa0, a0
	ret
```

`llvm-mc -triple=riscv64 "-mattr=+m,+a,+f,+d" -filetype=obj` 生成的对象 `Flags: 0x4, double-float ABI`，与 `lp64d` 调用者一致，于是链接成功（`link exit=0`）。反汇编：

```asm
0000000000001324 <caller>:
    132c: li      a0, 0x7ff
    1330: slli    a0, a0, 0x33
    1334: fmv.d.x fa5, a0
    1338: fadd.d  fa0, fa0, fa5
    1340: jalr    0x44(ra) <callee@plt>
    134c: ret
0000000000001350 <callee>:
    1350: fcvt.d.l fa0, a0
    1354: ret
```

调用者把 `x + 1.5` 放在 `fa0`，`a0` 里只是常量 `1.5` 的位模式（`132c/1330/1334` 三条指令物化了它）。被调者却按软浮点约定把 `a0` 当整数参数，`fcvt.d.l` 把这个整数值转成 double；它拿到的不是 `x + 1.5`，而是 `0x3FF8000000000000` 这个整数的转换结果。链接器没有任何理由报错：`e_flags` 一致、架构一致、符号能解析。这类错误只能靠 ABI 审计或运行测试发现。

因此，“ABI 不匹配会报错”只在编译器生成的对象之间成立。手写汇编、内联汇编、JIT 拼出来的对象，都可能绕过 `e_flags` 检查。第 12 章的实验 D 会把这条链路完整重放一遍。

## 可跑实验

实验脚本：`code/labs/12-calling-convention.ps1`，五个用例。所有输出加 `OUT:` 前缀。

### 实验 A：同一份 IR，三种 ABI

```powershell
.\code\tools\run.ps1 labs\12-calling-convention.ps1 -Case A
```

脚本用 `sum9 / sumd9 / sumf9 / mixed` 四个函数，分别以 `lp64d`、`lp64f`、`lp64` 编译。核对：

- `sum9` 在三种 ABI 下相同：第 9 个整数从 `0(sp)` 读。
- `sumd9` 在 `lp64d` 下第 9 个走 `a0` + `fmv.d.x`；在 `lp64f/lp64` 下走 `__adddf3`。
- `sumf9` 在 `lp64d` 与 `lp64f` 下相同；在 `lp64` 下变成软浮点。
- `mixed` 在 `lp64d` 下同时使用 `a0/a1` 与 `fa0/fa1`。

### 实验 B：varargs 的 GPR 与对齐

```powershell
.\code\tools\run.ps1 labs\12-calling-convention.ps1 -Case B
```

脚本编译两个目标：`riscv64 -target-abi=lp64d` 的 `call_vsum`，与 `riscv32 -target-abi=ilp32` 的 `call_vsum32 / call_vsum32b`。核对 `li a1, 2047; slli a1, a1, 51`（RV64 把 1.5 放 a1）与 `lui a3, 262016; li a2, 0`（RV32 放 a2/a3，跳过 a1）。

### 实验 C：`-target-abi` 的 warning、`e_flags` 与链接错误

```powershell
.\code\tools\run.ps1 labs\12-calling-convention.ps1 -Case C
```

脚本依次跑五组非法 ABI，打印 warning 与 exit。然后用 `llvm-readelf -h` 打印 `lp64`/`lp64d` 对象的 `Flags`，最后用 `ld.lld` 尝试混链，预期：

```text
Flags: 0x4, double-float ABI
Flags: 0x0
ld.lld: error: ... cannot link object files with different floating-point ABI ...
```

### 实验 D：手写汇编绕过 `e_flags` 检查

```powershell
.\code\tools\run.ps1 labs\12-calling-convention.ps1 -Case D
```

脚本生成一个 `lp64d` 调用者与一个“声称 D ABI、实际读 a0”的汇编被调者，链接后反汇编。预期：链接 exit=0；`caller` 把 `x + 1.5` 放在 `fa0`，`callee` 却把 `a0` 当整数参数（`fcvt.d.l`），而 `a0` 里只是常量 `1.5` 的位模式。这是本章最重要的失败例子：能链接不等于 ABI 一致。

### 实验 E：sret 与 byval

```powershell
.\code\tools\run.ps1 labs\12-calling-convention.ps1 -Case E
```

脚本编译 `ret_big`、`caller_byval`、`byval_write`。核对 `ret_big` 的 `sd a5, 32(a0)` 与 `sd a1, 0(a0)`（sret 指针在 a0），以及 `caller_byval` 的 48 字节帧与 5 次 `ld/sd` 拷贝。

## 源码落点

1. `RISCVCallingConv.td`
   - `CSR_ILP32E_LP64E`、`CSR_ILP32_LP64`、`CSR_ILP32F_LP64F`、`CSR_ILP32D_LP64D`。
   - `CSR_Interrupt`、`CSR_RT_MostRegs`、`CSR_NoRegs`。
   - 注意：这个文件没有 `CCIfType`/`CCAssignToReg`，不要在这里找参数规则。
2. `RISCVCallingConv.cpp`
   - `CC_RISCV`：GHC/Fast/普通三条分派。
   - `CC_RISCV_Impl`：ABI switch、`AllowFPRForF64/F16_F32`、`isVarArg`、sret 判定、GPR 对、varargs 对齐。
   - `ArgFPR16s/32s/64s/128s`：`fa0-fa7` 的物理编号。
   - `RISCV::getArgGPRs`：`a0-a7` 与 ILP32E 的 `a0-a5`。
   - `RISCV::getArgFPRs`：软浮点返回空，按 Q/D/F 选数组。
3. `RISCVISelLowering.cpp`
   - `AnalyzeFormalArguments(Ins, CC_RISCV)`（约 25487）。
   - `AnalyzeCallOperands(Outs, CC_RISCV)`（约 25724）。
   - `RetCCInfo.AnalyzeFormalArguments(Ins, RetCC_RISCV)`（约 26157）。
   - `CheckReturn(Outs, RetCC_RISCV)`（约 26208）。
   - 这些调用点之后就是 `LowerFormalArguments` / `LowerCall` / `LowerReturn` 的实际搬运代码。
4. `MCTargetDesc/RISCVELFStreamer.cpp`
   - `RISCVTargetELFStreamer::finish`：`EF_RISCV_FLOAT_ABI_SINGLE/DOUBLE`、`EF_RISCV_RVC`、`EF_RISCV_TSO`、`EF_RISCV_RVE`。
5. `MCTargetDesc/RISCVTargetStreamer.cpp`
   - `emitTargetAttributes`：`stack_align` 的 4/8/16 与 `arch` 文本。
6. `RISCVFrameLowering.cpp`
   - varargs 的寄存器保存区、`byval` 副本所在的栈对象、prologue 的 `sp` 调整。

## 常见坑

1. **在 `RISCVCallingConv.td` 里找 `CCIfType`**。RISC-V 的参数规则是 C++ 的 `CC_RISCV_Impl`，`.td` 只有 callee-saved。文件顶部的注释还指向旧位置，以实际符号为准。
2. **以为可变参数的浮点走 FPR**。`AllowFPRForF64/F16_F32` 都带 `!ArgFlags.isVarArg()`；`lp64d` 下 `double` 也进 GPR。
3. **以为 FPR 用尽就溢出到栈**。`lp64d` 的第 9 个 `double` 先借 `a0`（Bitcast FP to GPR），只有 GPR 也没有时才上栈。
4. **以为第 9 个参数一定在栈上**。整数是；浮点不是；`lp64f` 的 `double` 也不是（走 GPR 对）。先看 ABI，再看类型。
5. **以为 `byval` 的拷贝在被调函数**。拷贝在调用点，被调者只拿到指向副本的指针；被调者写 `%p` 改的是副本。
6. **用手写 IR 的按值聚合测 C ABI**。LLVM 会把 `%big` 展开成 5 个 `i64`，而 Clang 对大结构体用 `byval`/指针。两者形状不同。
7. **把 `-target-abi` 的 warning 当没事**。它是“忽略你的 ABI”，代码会按默认 ABI 生成，可能从硬浮点变软浮点。
8. **以为链接器一定能发现 ABI 不一致**。它只看 `e_flags`；手写汇编用 `.attribute` 伪造 `e_flags` 后可以静默通过。
9. **在 ILP32E 上假设 8 个参数寄存器**。`getArgGPRs` 在 E ABI 下只给 `a0-a5`；varargs 的偶数对齐规则也显式排除 ILP32E。
10. **把 RV32 的 `f64` 当一个寄存器**。`f64` 要拆成两个 32 位半；FPR 用尽时可能用 `a0/a1` 对，也可能一半在寄存器一半在栈上。

## 自我检查

- [ ] 能说出 `RISCVCallingConv.td` 与 `RISCVCallingConv.cpp` 各自负责什么，并指出 `CC_RISCV_Impl` 的位置。
- [ ] 能写出 `lp64`、`lp64f`、`lp64d` 下 `float` 与 `double` 的参数去向。
- [ ] 能解释 `AllowFPRForF64 = !ArgFlags.isVarArg()` 的作用，并用 `call_vsum` 的 `a1` 证明。
- [ ] 能解释 9 个 `double` 在 `lp64d` 下第 9 个为什么在 `a0`，以及 `fmv.d.x` 的作用。
- [ ] 能区分 `a0/a1` 返回值与 sret：什么时候用前者，什么时候用后者。
- [ ] 能解释 `byval` 的 40 字节拷贝为什么出现在 `caller_byval` 而不是 `byval_write`。
- [ ] 能复述 RV32 varargs 的“跳过奇数寄存器”规则，并说明三个成立条件。
- [ ] 能解释 `e_flags` 的 `0x4` 与 `0x0`，以及为什么手写汇编可以绕过链接器检查。
- [ ] 能列出至少三条 `-target-abi` 的真实 warning，并说明它们不会让 llc 失败。
- [ ] 能解释 `-target-abi=lp64d` 但没开 `+d` 时，代码实际回退到了哪个 ABI。

## 延伸阅读

- LLVM 源码：`RISCVCallingConv.cpp`、`RISCVCallingConv.td`、`RISCVISelLowering.cpp` 的 `LowerFormalArguments` / `LowerCall` / `LowerReturn`。
- RISC-V psABI：整数与浮点参数寄存器、sret、byval、varargs 的对齐规则、RV32E 的限制。
- LLVM 文档：`CCState`、`CCValAssign`、`ISD::ArgFlagsTy`、`CallingConv::Fast/GHC`。
- ELF 规范：`EF_RISCV_FLOAT_ABI_SINGLE/DOUBLE`、`EF_RISCV_RVC`、`EF_RISCV_RVE`。
- 课程第 11 章：参数寄存器与分配顺序；第 13 章：参数保存区与栈帧布局；第 21 章：链接与重定位。
- 本机命令：`clang --target=riscv64-unknown-elf -march=rv64gc -mabi=lp64d -O2 -S`，用 Clang 的真实 ABI lowering 对照手写 IR。

[上一章：寄存器与寄存器类](./11-registers-and-classes.md) | [下一章：栈帧](./13-frame-lowering.md)