# Ch 20 — 软浮点与 libcall

> **目标**：能解释一个没有 F/D 扩展的 target 如何把 `fadd/fdiv/fptosi/sitofp/fcmp` 变成 `__adddf3`、`__divdf3`、`__fixdfdi` 等调用，能读懂 MiniRV 的 soft-float 与 libm/libcall 注册，并能判断什么时候该发 libcall、什么时候该拒绝。

> **环境**：LLVM 24.0.0git，`/home/ubuntu/llvm/trees/dsl-tablegen`，构建 `/home/ubuntu/llvm/builds/dsl-tablegen`。`B=/home/ubuntu/llvm/builds/dsl-tablegen/bin`，`LDLLD=/home/ubuntu/llvm/builds/pr-lane/bin/ld.lld`；MiniRV triple 是 `minirv64`，RISC-V 参考用 `-mtriple=riscv64 -mattr=+m`。

## 为什么需要这个？

MiniRV 只有整数寄存器类，没有 F/D 扩展，也没有浮点寄存器。IR 里的 `fadd double` 不能选成一条指令，后端必须把它“降级”成一次函数调用：`__adddf3(a, b)`。这类调用叫 **libcall**，符号由 compiler-rt 或 libgcc 提供，ABI 由 target 的调用约定决定。

没有这一层会怎样？三种坏结果：

1. **选指失败**：`Cannot select: fadd f64`，后端直接崩。
2. **静默生成空函数**：合法化器知道要发 libcall，但 target 没注册名字，`makeLibCall` 报 `no libcall available`，函数体退化成一条 `jalr`，返回值是垃圾。这比崩溃更危险。
3. **错误地内联软件浮点**：把 IEEE 754 的舍入、NaN、异常标志全部手写进后端，代码尺寸和维护成本都失控。

所以“没有 FPU”不是“不能做浮点”，而是“把浮点运算变成对运行时库的调用”。这一章要讲清三件事：

- **soft-float 的合法化路径**：f32/f64 如何被软化到 i32/i64，libcall 如何被创建；
- **ABI**：软浮点的参数和返回值走哪些寄存器，为什么 f32 要清零高 32 位；
- **库的边界**：哪些操作是 compiler-rt 的 `__addsf3`，哪些是 libm 的 `sqrt/sin/pow`，哪些根本没有注册。

## 先建立直觉

先看一条 `fadd double` 的完整路径：

```
LLVM IR:  %r = fadd double %a, %b
   │
   ▼
SelectionDAG:  ISD::FADD f64
   │
   │  MiniRV 没有 f64 寄存器类 → TargetLoweringBase 把 f64 标成 TypeSoftenFloat
   ▼
类型合法化:  f64 → i64（值还在整数寄存器里）
   │
   │  DAGTypeLegalizer::SoftenFloatRes_FADD
   ▼
RTLIB::ADD_F64  ──►  LibcallLoweringInfo 查表
   │
   │  MiniRVSubtarget::initLibcallLoweringInfo:
   │  {RTLIB::ADD_F64, RTLIB::impl___adddf3}
   ▼
符号 "__adddf3" + 调用约定（x10, x11 传参，x10 返回）
   │
   ▼
SelectionDAG 的 call 节点 → PseudoCALL &__adddf3
   │
   ▼
MC:  jal x1, __adddf3   +   R_RISCV_JAL __adddf3
   │
   ▼
链接器/runtime: compiler-rt 的 __adddf3 提供实现
```

注意两个容易混的点：

- **f64 不是寄存器对**。一个 `double` 是 64 位，正好放进一个 GPR（x10）；只有 `i128` 才是 x10/x11 对。软浮点里“f64 用两个寄存器”是常见误解。
- **f32 也不是只用低 32 位就完事**。MiniRV 在调用前会 `slli 32; srli 32` 把高 32 位清零，保证传进 `__addsf3` 的 i64 值是零扩展的 f32 位模式。

## 主线：soft-float 的合法化

### 没有 FP 寄存器类，类型先被“软化”

MiniRV 的 `MiniRVISelLowering` 构造函数只声明了整数寄存器类：

```cpp
// llvm/lib/Target/MiniRV/MiniRVISelLowering.cpp
addRegisterClass(MVT::i64, &MiniRV::GPRRegClass);
```

没有 `addRegisterClass(MVT::f32, ...)`，也没有 `MVT::f64`。于是 `TargetLoweringBase::computeRegisterProperties` 会做：

```cpp
// llvm/lib/CodeGen/TargetLoweringBase.cpp
if (!isTypeLegal(MVT::f64)) {
  TransformToType[MVT::f64] = MVT::i64;
  ValueTypeActions.setTypeAction(MVT::f64, TypeSoftenFloat);
}
if (!isTypeLegal(MVT::f32)) {
  TransformToType[MVT::f32] = MVT::i32;
  ValueTypeActions.setTypeAction(MVT::f32, TypeSoftenFloat);
}
```

`TypeSoftenFloat` 的意思是：这个浮点值在寄存器里按整数保存，运算本身用 libcall 实现。`DAGTypeLegalizer::SoftenFloatRes_FADD` 会调用：

```cpp
return SoftenFloatRes_Binary(N, GetFPLibCall(N->getValueType(0),
                                             RTLIB::ADD_F32,
                                             RTLIB::ADD_F64, ...));
```

`SoftenFloatRes_Binary` 用整数类型的操作数构造一个 `call`，所以你在汇编里看到的是“整数寄存器传参 + `jal __addsf3`”，而不是浮点指令。

### `setOperationAction` 在这里为什么没出现

很多后端会在 lowering 构造函数里写：

```cpp
setOperationAction(ISD::FADD, MVT::f32, Expand);
setOperationAction(ISD::FDIV, MVT::f64, Expand);
```

MiniRV 没有这么写。原因是 **f32/f64 根本不是合法类型**：类型合法化阶段已经把 `fadd f64` 软化成“i64 操作数 + libcall”，等不到 operation legalization 去查 `FADD` 的 action。`setOperationAction` 对“类型合法但没有对应指令”的 target 才关键，例如你有 f32 寄存器类、但没有 `fadd` 指令。

这也是自己 bring-up 时最容易照抄错的地方：先看类型是否合法，再决定是设 action，还是靠 `TypeSoftenFloat`。

### `setLibcallName` 与 `setLibcallImpl`

老版本的 LLVM 里，target 会写：

```cpp
TLI.setLibcallName(RTLIB::ADD_F32, "__addsf3");
```

这棵树里已经没有 `TargetLowering::setLibcallName` 了。现在的路径是 `TargetSubtargetInfo::initLibcallLoweringInfo(LibcallLoweringInfo &)`，MiniRV 在 `MiniRVSubtarget.cpp` 里实现：

```cpp
void MiniRVSubtarget::initLibcallLoweringInfo(LibcallLoweringInfo &Info) const {
  Info.setLibcallImpl(RTLIB::ADD_F32, RTLIB::impl___addsf3);
  Info.setLibcallImpl(RTLIB::ADD_F64, RTLIB::impl___adddf3);
  ...
}
```

三个概念要分清：

| 概念 | 例子 | 含义 |
|---|---|---|
| `RTLIB::Libcall` | `RTLIB::ADD_F64` | “f64 加法”这个操作，与符号名无关 |
| `RTLIB::LibcallImpl` | `RTLIB::impl___adddf3` | 选哪个实现；名字由 TableGen 生成 |
| 符号名 | `__adddf3` | `RuntimeLibcallsInfo::getLibcallImplName` 返回的字符串 |

`LibcallLoweringInfo` 的默认值是 `RTLIB::Unsupported`。如果 triple 不在 runtime libcall 表里，又没有 target override，`getLibcallName` 返回空字符串，`makeLibCall` 就会报 “no libcall available”。MiniRV 的 triple `minirv64` 对默认表来说是未知的，所以它必须自己把用到的条目全部注册。

如果只是想给某个 libcall 换名字，旧 API 可以直接传字符串；新 API 需要换一个 `LibcallImpl`。标准名字都在 `llvm/include/llvm/IR/RuntimeLibcallsImpl.td` 里定义，想加自定义符号就要加一条 `RuntimeLibcallImpl` 记录，然后重新生成头文件。这是 **guided** 的源码改动，本章不展开。

### 一张表：浮点操作 → helper

| IR / DAG | RTLIB | f32 helper | f64 helper |
|---|---|---|---|
| `fadd` | `ADD_F32/F64` | `__addsf3` | `__adddf3` |
| `fsub` | `SUB_F32/F64` | `__subsf3` | `__subdf3` |
| `fmul` | `MUL_F32/F64` | `__mulsf3` | `__muldf3` |
| `fdiv` | `DIV_F32/F64` | `__divsf3` | `__divdf3` |
| `frem` | `REM_F32/F64` | `fmodf` | `fmod` |
| `fpext f32→f64` | `FPEXT_F32_F64` | `__extendsfdf2` | — |
| `fptrunc f64→f32` | `FPROUND_F64_F32` | `__truncdfsf2` | — |
| `fptosi f32→i64` | `FPTOSINT_F32_I64` | `__fixsfdi` | — |
| `fptosi f64→i64` | `FPTOSINT_F64_I64` | — | `__fixdfdi` |
| `fptoui f64→i64` | `FPTOUINT_F64_I64` | — | `__fixunsdfdi` |
| `sitofp i32→f32` | `SINTTOFP_I32_F32` | `__floatsisf` | — |
| `sitofp i64→f64` | `SINTTOFP_I64_F64` | — | `__floatdidf` |
| `uitofp i64→f64` | `UINTTOFP_I64_F64` | — | `__floatundidf` |
| `fcmp olt` | `FCMP3_PRED_OLT_*` | `__ltsf2` | `__ltdf2` |
| `fcmp uno` | `UO_*` | `__unordsf2` | `__unorddf2` |
| `llvm.sqrt` | `SQRT_*` | `sqrtf` | `sqrt` |
| `llvm.fma` | `FMA_*` | `fmaf` | `fma` |
| `llvm.floor` | `FLOOR_*` | `floorf` | `floor` |
| `llvm.fabs` | 无 | 位运算 | 位运算 |

`llvm.fabs` 不是 libcall：它只是清符号位，MiniRV 直接用 `lui/addi/slli/or` 造掩码再 `and`。这也说明“浮点 intrinsic”不等于“一定要调用 libm”。

## 实测：软浮点调用

### 1. `fadd/fdiv/fptosi/sitofp/fcmp`

```console
$ $B/llc -mtriple=minirv64 -O2 sf.ll -o - | grep -E '^[a-z0-9_]+:|^[[:space:]]+(addi|addiw|slli|srli|sd|ld|jal|jalr|slt|sltu|xor)'
fadd32:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	slli	x3, x10, 32
	srli	x10, x3, 32
	slli	x3, x11, 32
	srli	x11, x3, 32
	jal	x1, __addsf3
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
fadd64:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, __adddf3
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
fdiv64:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, __divdf3
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
f2i:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	slli	x3, x10, 32
	srli	x10, x3, 32
	jal	x1, __fixsfdi
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
i2f:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	addiw	x10, x10, 0
	jal	x1, __floatsisf
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
fcmp_olt:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	slli	x3, x10, 32
	srli	x10, x3, 32
	slli	x3, x11, 32
	srli	x11, x3, 32
	jal	x1, __ltsf2
	addiw	x3, x10, 0
	addi	x4, x0, 0
	slt	x10, x3, x4
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

解读：

- `fadd32` 的两个 `slli/srli` 是 f32 的零扩展。MiniRV 的调用约定把 `i32` 提升成 `i64`，所以传参前把高 32 位清掉；`__addsf3` 只读低 32 位。
- `fadd64` 没有清零，因为 double 已经是一个完整的 64 位值，直接放在 x10/x11。
- `f2i` 是 `fptosi float → i32`，但生成的是 `__fixsfdi`，不是 `__fixsfsi`。原因：i32 结果在 MiniRV 上不合法，被提升成 i64，转换节点变成 `fptosi f32 → i64`，所以调用 64 位结果的 helper。返回值低 32 位就是 i32。
- `i2f` 是 `sitofp i32 → float`，生成 `addiw x10,x10,0`（把低 32 位符号扩展到 64）再调 `__floatsisf`。这里用的又是 32 位源 helper；源宽度没有被提升成 i64 的 helper。
- `fcmp olt`：`__ltsf2` 返回 -1/0/1，`addiw` 符号扩展，`slt x10,x3,x4`（x4=0）把“结果 < 0”变成 0/1。`fcmp uno` 则用 `__unorddf2`，返回值非零表示 unordered，再用 `sltu x10,x0,x3` 归一化成 0/1。

### 2. ABI：浮点值确实走整数寄存器

```console
$ $B/llc -mtriple=minirv64 -O2 abi.ll -o - | grep -E '^[a-z0-9_]+:|^[[:space:]]+(addi|addiw|slli|srli|sd|ld|jal|jalr)'
fbits:
	jalr	x0, 0(x1)
dbits:
	jalr	x0, 0(x1)
sum3d:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	sd	x8, 0(x2)
	addi	x8, x12, 0
	jal	x1, __adddf3
	addi	x11, x8, 0
	jal	x1, __adddf3
	ld	x8, 0(x2)
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

`fbits(float)` 直接把 x10 原样返回，`dbits(double)` 也直接把 x10 返回——如果浮点走浮点寄存器，这里必然有 `fmv` 之类的搬移；没有，说明值就在 GPR。`sum3d(double a, double b, double c)` 的三个参数分别在 x10、x11、x12；第一次 `__adddf3(x10,x11)` 算 `a+b`，把第三个参数 c 先存到 x8（callee-saved）以免被调用破坏，第二次把 c 放到 x11 再算 `(a+b)+c`。

再用 MIR 看调用点：

```console
$ $B/llc -mtriple=minirv64 -O2 -stop-after=finalize-isel sf.ll -o - | sed -n '/name: *fadd32/,/PseudoRET/p'
liveins:
  - { reg: '$x10', virtual-reg: '%0' }
  - { reg: '$x11', virtual-reg: '%1' }
body: |
  bb.0 (%ir-block.0):
    %1:gpr = COPY $x11
    %0:gpr = COPY $x10
    %2:gpr = SLLI %0, 32
    %3:gpr = SRLI %2, 32
    %4:gpr = SLLI %1, 32
    %5:gpr = SRLI %4, 32
    $x10 = COPY %3
    $x11 = COPY %5
    PseudoCALL &__addsf3, csr_minirv, implicit-def dead $x1, implicit $x10, implicit $x11, ...
    %6:gpr = COPY $x10
    $x10 = COPY %6
    PseudoRET implicit $x10
```

`liveins` 只有 `$x10/$x11`，虚拟寄存器类是 `gpr`；`PseudoCALL &__addsf3` 是 libcall 在 MIR 里的形态。到 AsmPrinter 才变成 `jal x1, __addsf3`。

### 3. 对象文件里的重定位

```console
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj sf.ll -o sf.o
$ $B/llvm-readobj -r sf.o
    0x18 R_RISCV_JAL __addsf3 0x0
    0x30 R_RISCV_JAL __adddf3 0x0
    0x48 R_RISCV_JAL __divdf3 0x0
    0x68 R_RISCV_JAL __fixsfdi 0x0
    0x84 R_RISCV_JAL __floatsisf 0x0
    0xAC R_RISCV_JAL __ltsf2 0x0
    0xD0 R_RISCV_JAL __unorddf2 0x0
    0xFC R_RISCV_JAL __extendsfdf2 0x0
```

这些是**未定义符号的重定位**，不是地址 0 的直接调用。`llc` 只负责生成对象文件；符号解析是链接器的事。

## 主线：libm intrinsic

### `sqrt` 能用，`sin/pow` 没注册

`llvm.sqrt.f64` 在 MiniRV 上会变成 `sqrt`：

```console
$ $B/llc -mtriple=minirv64 -O2 libm.ll -o - 2>&1 | grep -E '^error|^[a-z0-9_]+:|^[[:space:]]+(addi|sd|ld|jal|jalr|slli|srli)'
error: no libcall available for fsin with type f64
error: no libcall available for fpow with type f64
my_sqrt:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, sqrt
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
my_sin:
	jalr	x0, 0(x1)
my_pow:
	jalr	x0, 0(x1)
```

这是本章最值得记住的“真实行为”：

- `llvm.sqrt.f64` → `RTLIB::SQRT_F64` → `RTLIB::impl_sqrt` → 符号 `sqrt`，工作正常。
- `llvm.sin.f64` → `RTLIB::SIN_F64`，`llvm.pow.f64` → `RTLIB::POW_F64`；在 RTLIB 的默认实现里它们分别是 `sin` 和 `pow`。
- 但 MiniRV 的 `FloatCalls` 表只注册了 `SQRT/FMA/FLOOR/CEIL/TRUNC/ROUND/RINT/NEARBYINT/FMIN/FMAX/REM`，**没有注册 SIN/POW**。默认 `Unsupported` 导致 `getLibcallName` 返回空，`llc` 报错，并且函数体退化成只有 `jalr` 的空函数。这不是“调用了一个不存在的库”，而是“后端在合法化阶段就没有名字可用”。

如果要在 MiniRV 上支持这两个 intrinsic，需要在 `MiniRVSubtarget::initLibcallLoweringInfo` 的 `FloatCalls` 表里加：

```cpp
{RTLIB::SIN_F64, RTLIB::impl_sin},
{RTLIB::POW_F64, RTLIB::impl_pow},
```

然后重新构建并加测试。这一步是 **guided**（会改 target 源码并 rebuild），本章没有执行；`impl_sin/impl_pow` 在生成的 `RuntimeLibcalls.inc` 里确实存在，符号名是 `sin`/`pow`。

另一个办法是不形成 intrinsic：如果前端因为 `-fno-builtin` 或 `nobuiltin` 保留了普通调用，`llc` 会直接发 `jal sin`，根本不需要 RTLIB 注册：

```console
$ $B/llc -mtriple=minirv64 -O2 nb.ll -o -
f:
	addi	x2, x2, -16
	sd	x1, 8(x2)
	jal	x1, sin
	ld	x1, 8(x2)
	addi	x2, x2, 16
	jalr	x0, 0(x1)
```

这解释了 `nobuiltin`/`-fno-builtin` 的作用：它阻止优化器把普通调用改写成 `llvm.sin` intrinsic。`TargetLibraryInfo` 会检查 `CallBase::isNoBuiltin()`；函数或调用点上的 `nobuiltin` 属性会让 TLI 不再把它当已知库函数。对于自定义的 `sin` 实现，这一点很重要。

### 为什么没有 libc 也能 emit 这些调用

`llc` 不链接 libc，也不检查 `sqrt` 是否存在。它只做两件事：

1. 在 DAG 里创建一个外部符号调用；
2. 在对象文件里写一条 `R_RISCV_JAL sqrt` 重定位。

符号最终由链接器解析：链接 libm/compiler-rt/libgcc 就有实现；裸机没有库，就在链接时报未定义。用 MiniRV 的 `start.o` 直接链 `libm2.o`，会看到：

```console
$ $B/llc -mtriple=minirv64 -O2 -filetype=obj libm2.ll -o libm2.o
$ $LDLLD -m elf64lriscv --no-relax start.o libm2.o -o libm2.elf
ld.lld: error: undefined symbol: sqrt
>>> referenced by libm2.ll
>>>               libm2.o:(s)
ld.lld: error: undefined symbol: fma
>>> referenced by libm2.ll
>>>               libm2.o:(f)
ld.lld: error: undefined symbol: floor
>>> referenced by libm2.ll
>>>               libm2.o:(fl)
```

这不是编译器 bug，而是“运行时库没参与链接”。MiniRV 的 e2e 只跑整数程序，`start.s` 里没有 libc，所以这些测试不会碰 `sqrt`。

## 主线：compiler-rt helpers

### `__muldi3`：RISC-V 没有 M 时的 64 位乘法

同一份 `mul.ll`，三种配置：

```console
$ $B/llc -mtriple=riscv64 -O2 mul.ll -o -          # 没有 +m
mul64:
	tail	__muldi3
div64:
	tail	__divdi3

$ $B/llc -mtriple=riscv64 -mattr=+m -O2 mul.ll -o -
mul64:
	mul	a0, a0, a1
	ret
div64:
	div	a0, a0, a1
	ret

$ $B/llc -mtriple=minirv64 -O2 mul.ll -o -
mul64:
	mul	x10, x10, x11
	jalr	x0, 0(x1)
div64:
	div	x10, x10, x11
	jalr	x0, 0(x1)
```

RV64I 没有 M 扩展时，`mul/div/rem` 都变成 `__muldi3/__divdi3/__moddi3`；有 M 时就是单指令。MiniRV 的 ISA 是 RV64IM，所以 `MUL/SDIV/UDIV/SREM/UREM` 在 `MiniRVISelLowering` 里是 `Legal`，不会走这些 helper。

这也解释了为什么 `__muldi3` 和 `__multi3` 不是一回事：

- `__muldi3` 是 64×64→64 的乘法；
- `__multi3` 是 128×128→128 的乘法，通常内部再用 `__muldi3` 或 64 位 `mul` 实现。

Ch 19 已经看到：RISC-V 无 M 时 `i128 mul` 直接 `call __multi3`；有 M 时内联成 `mul/mulhu`。MiniRV 有 M，所以内联，不调用 `__multi3`。

### `__divti3`：128 位除法

`__divti3` 与 M 扩展无关，因为 RV64 根本没有 128 位除法指令。RISC-V 无 M 和有 M 都发同一个调用：

```console
$ $B/llc -mtriple=riscv64 -O2 i128.ll -o - | sed -n '/^sdiv128:/,/End function/p'
sdiv128:
	call	__divti3
$ $B/llc -mtriple=riscv64 -mattr=+m -O2 i128.ll -o - | sed -n '/^sdiv128:/,/End function/p'
sdiv128:
	call	__divti3
```

MiniRV 的 `sdiv128` 也是 `jal x1, __divti3`（Ch 19 已给完整输出）。四兄弟是 `__divti3/__udivti3/__modti3/__umodti3`，分别对应 `SDIV_I128/UDIV_I128/SREM_I128/UREM_I128`。

### `__addsf3`：软浮点的核心

`__addsf3` 是 compiler-rt 的“单精度加法”入口，签名等价于：

```c
float __addsf3(float a, float b);
```

但在 MiniRV 的软浮点 ABI 里，它表现为：

```
x10 = a 的 32 位位模式（零扩展）
x11 = b 的 32 位位模式（零扩展）
返回 x10 = a+b 的 32 位位模式
```

所以 `fadd float` 的汇编是“清零高 32 位 + `jal __addsf3`”，而不是 `fadd.s`。同理 `__adddf3` 的 x10/x11 是两个完整的 64 位 double 位模式。

## 源码落点

| 层 | 文件 | 关键符号 | 看什么 |
|---|---|---|---|
| 类型软化 | `TargetLoweringBase.cpp` | `computeRegisterProperties`、`TypeSoftenFloat` | f32/f64 → i32/i64 |
| 浮点合法化 | `LegalizeFloatTypes.cpp` | `SoftenFloatRes_FADD` | `RTLIB::ADD_F32/ADD_F64` |
| Libcall 注册 | `MiniRVSubtarget.cpp` | `initLibcallLoweringInfo`、`FloatCalls` | `setLibcallImpl` 表 |
| 调用约定 | `MiniRVCallingConv.td` | `CC_MiniRV`、`RetCC_MiniRV` | x10..x17 传参、x10 返回 |
| 调用 lowering | `MiniRVISelLowering.cpp` | `LowerCall`、`LowerFormalArguments` | 参数/返回值与 PseudoCALL |

## 反直觉与踩雷

1. **错误直觉**：`double` 是 64 位，所以软浮点里要用两个 32 位寄存器或一个寄存器对。  
   **正确认识**：RV64 的 GPR 是 64 位，一个 `double` 正好放一个 GPR。`sum3d(a,b,c)` 的参数在 x10/x11/x12，不是 x10..x15；只有 `i128` 才是 x10/x11 对。

2. **错误直觉**：`fptosi float → i32` 一定调用 `__fixsfsi`。  
   **正确认识**：MiniRV 的 i32 不合法，结果被提升成 i64，所以实际调用 `__fixsfdi`（64 位结果）。反过来，`sitofp i32 → float` 仍用 32 位源 helper `__floatsisf`，并在前面加 `addiw` 做符号扩展。helper 的名字由“合法化后的类型”决定，不是你源码里的类型。

3. **错误直觉**：`llvm.sin.f64`/`llvm.pow.f64` 会自动变成 `sin`/`pow`。  
   **正确认识**：RTLIB 里确实有 `SIN_F64`/`POW_F64` 和 `impl_sin/impl_pow`，但 MiniRV 的 `FloatCalls` 表没有注册它们。实测 `llc` 报 `no libcall available for fsin/fpow`，函数体退化成空函数。要么在 target 里加两行注册，要么用 `nobuiltin`/`-fno-builtin` 保留普通调用。

4. **错误直觉**：`llc` 能生成 `jal sqrt`，说明 LLVM 自带 libm。  
   **正确认识**：`llc` 只生成重定位。`R_RISCV_JAL sqrt` 需要链接器解析；裸机没有 libm 就报 undefined symbol。编译器负责 ABI 合同，运行库负责实现。

5. **错误直觉**：soft-float 的 f32 参数只保证低 32 位有效，所以不用管高 32 位。  
   **正确认识**：MiniRV 会显式 `slli 32; srli 32` 清零高半。虽然 `__addsf3` 只读低 32 位，但把高位清零让 ABI 更可预测，也避免后续把该 i64 当整数使用时出现脏位。代价是每条 f32 调用前多两条指令。

6. **错误直觉**：target 应该重写 `setLibcallName`。  
   **正确认识**：这棵树已经用 `TargetSubtargetInfo::initLibcallLoweringInfo` + `LibcallLoweringInfo::setLibcallImpl`。`RTLIB::ADD_F32` 是操作，`RTLIB::impl___addsf3` 是实现，符号名由 `RuntimeLibcallsImpl.td` 生成。旧文章里的 `setLibcallName(RTLIB::ADD_F32, "__addsf3")` 只能当历史背景读。

## 进阶：再往深一层

- **soft-float 的舍入模式与异常标志**：libcall 版本通常遵循默认舍入和 errno 约定；`llvm.sqrt` 在 RTLIB 里是 `ErrnoMathLibcall`，而 `fadd` 是 `PureLibcall`。这会影响 `-fno-math-errno`、`fenv` 和严格浮点的处理。
- **half/bfloat**：MiniRV 的 `FloatCalls` 还注册了 `FPEXT_F16_F32`、`FPROUND_F32_F16`、`BF16` 等。half 没有算术 helper，先提升到 f32 再算；这是“最小软浮点”的常见做法。
- **libm 的完整性**：`llvm.sin/cos/exp/log/pow` 都可以通过 `RTLIB` 映射到 libm，但每个都要在 target 表里出现，否则就是本章的 `no libcall available`。成熟后端通常按“目标是否声明有 libm”来批量注册，而不是逐个手写。
- **ABI 变体**：RISC-V 有 `lp64`/`lp64d`/`lp64f` 等 ABI；soft-float 只是其中一种。MiniRV 的 `CC_MiniRV` 只有 i64 规则，所以它天然是 soft-float；如果你的 target 同时支持硬浮点和软浮点，要在 subtarget 层按 feature 切换 libcall 表和调用约定。

## 本章重点整理

- 没有 FP 寄存器类时，f32/f64 被 `TypeSoftenFloat` 软化成 i32/i64，运算由 libcall 完成；`setOperationAction` 不是必需入口。
- MiniRV 在 `MiniRVSubtarget::initLibcallLoweringInfo` 里用 `setLibcallImpl` 注册 `RTLIB::ADD_F32 → impl___addsf3` 等映射；默认 `Unsupported` 会导致 “no libcall available”。
- f32 参数在 x10/x11 中零扩展；f64 参数各占一个 GPR；i128 才是寄存器对。
- `fptosi f32→i32` 因 i32 提升而调用 `__fixsfdi`；`sitofp i32→f32` 用 `__floatsisf` 并先 `addiw`。
- `fcmp` 用三路比较 helper（`__ltsf2`）或 unordered helper（`__unordsf2`），再用 `slt/sltu` 归一化成 i1。
- `llvm.sqrt` 能映射到 `sqrt`；`llvm.sin/pow` 在 MiniRV 上没注册，实测报错。
- `llc` 只发重定位，libm/compiler-rt 由链接器和运行时提供；没有库就报 undefined symbol。
- 无 M 的 RISC-V 用 `__muldi3/__divdi3`；i128 除法始终用 `__divti3`；MiniRV 有 M，64 位乘除是单指令。
- `nobuiltin`/`-fno-builtin` 阻止普通调用被改写成 intrinsic；普通 `call @sin` 可以直接 `jal sin`，不需要 RTLIB。

## 自我检核

- [ ] 不看笔记，画出 `fadd double` 从 IR 到 `jal __adddf3` 的路径，并标出 `TypeSoftenFloat` 和 `RTLIB::ADD_F64` 的位置。
- [ ] 解释为什么 MiniRV 没有写 `setOperationAction(ISD::FADD, MVT::f64, Expand)`，却仍然能 lower `fadd double`。
- [ ] 说出 `__fixsfdi` 和 `__floatsisf` 分别对应哪条 IR 转换，为什么一个用 64 位结果 helper、一个用 32 位源 helper。
- [ ] 解释 f32 调用前 `slli 32; srli 32` 的作用；f64 为什么不需要。
- [ ] 解释 `__ltsf2` 的 -1/0/1 返回值如何变成 `fcmp olt` 的 i1。
- [ ] 判断：`llvm.sin.f64` 在 MiniRV 上为什么失败？给出两种修复路径。
- [ ] 解释 `RTLIB::ADD_F32`、`RTLIB::impl___addsf3` 和字符串 `__addsf3` 三者的区别。
- [ ] 解释 `llc` 生成 `jal sqrt` 与“链接到 libm”之间的关系。

## 延伸阅读

- **[LLVM Language Reference — Floating-Point Intrinsics](https://llvm.org/docs/LangRef.html)** — LLVM Project
  - **读哪里**：`llvm.sqrt`、`llvm.fma`、`llvm.sin`、`llvm.pow`、`llvm.fabs`、`llvm.minnum/maxnum` 的语义和属性。
  - **能学到什么**：哪些 intrinsic 允许 errno/异常标志，哪些是 pure；这决定了 RTLIB 里 `ErrnoMathLibcall` 与 `PureLibcall` 的区别。
  - **前提**：知道 IEEE 754 的基本概念，以及 `-fno-math-errno` 为什么会改变前端生成的 IR。

- **[RISC-V ELF psABI — Floating-Point Calling Convention](https://github.com/riscv-non-isa/riscv-elf-psabi-doc)** — RISC-V International
  - **读哪里**：soft-float ABI、`lp64`/`lp64f`/`lp64d` 的区别、参数和返回值的寄存器分配。
  - **能学到什么**：为什么 f32/f64 在 soft-float 下走整数寄存器，为什么 f64 不需要寄存器对；这些规则直接决定 MiniRV 的 `CC_MiniRV` 和 libcall ABI。
  - **前提**：读过 Ch 10 的调用约定和 Ch 17 的参数/返回值；否则容易把 ABI 和 legalization 混为一谈。

- **[compiler-rt Builtins](https://compiler-rt.llvm.org/)** — LLVM Project
  - **读哪里**：`lib/builtins` 下的 `addsf3.c`、`adddf3.c`、`divti3.c`、`multi3.c`、`fixsfdi.c`、`floatsisf.c`。
  - **能学到什么**：这些符号的真实签名、舍入行为、特殊值处理和内部实现；以及为什么同一个 `__addsf3` 能被不同 target 共用。
  - **前提**：能读 C 和少量内联汇编；先看本章的 helper 表，再去对照源码。

- **[LLVM RuntimeLibcalls.td / RuntimeLibcallsImpl.td](https://github.com/llvm/llvm-project/blob/main/llvm/include/llvm/IR/RuntimeLibcalls.td)** — LLVM Project
  - **读哪里**：`ADD_F32`、`FCMP3_PRED_OLT_F32`、`FPTOSINT_F32_I64`、`SQRT_F64`、`SIN_F64`、`POW_F64` 的定义，以及 `impl___addsf3`/`impl_sqrt`/`impl_sin` 的生成规则。
  - **能学到什么**：`RTLIB::Libcall` 和 `RTLIB::LibcallImpl` 的分层，为什么 target 只需要注册 impl，符号名由 TableGen 统一生成。
  - **前提**：读过本章的 `setLibcallImpl` 段落；否则会被 `impl_*` 的命名规则绕晕。

→ [Ch 21 异常、CFI 与调试信息](./21-exceptions-cfi-debug.md)
