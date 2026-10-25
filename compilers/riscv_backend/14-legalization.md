# 第 14 章 合法化：把 IR 的类型翻译成目标真正会做的操作

LLVM 的 IR 允许 `i64`、`half`、`fp128`、`<4 x i32>`、`<vscale x 4 x half>` 这些类型同时存在，而 RISC-V 的一条指令只认识有限的寄存器宽度和有限的运算。合法化（legalization）就是这两者之间的翻译层：它决定一个 IR 操作是直接变成机器指令，还是被拆成多条，还是退化成运行时库调用。

这一章讨论三件事：合法化规则怎么写、RISC-V 后端对 `i64`/`f16`/soft-float/向量分别做了什么、以及当一条路径不存在时 llc 会给出哪一种错误。最后一件尤其重要，因为 `Don't know how to legalize`、`Cannot select`、`unable to legalize instruction` 来自三个不同的层，修错的方向完全不同。

## 为什么

先看一个具体差距。同一个 `i64` 乘加函数，在 RV64 上只有三条指令：

```asm
mul	a0, a0, a1
div	a1, a0, a1
add	a0, a0, a1
```

在 RV32 上会展开成 `mul`/`mulhu` 的组合、一个 `call __divdi3`，以及用 `sltu` 补进位的加法。IR 没有变，triple 变了，合法化结果就完全变了。

再看浮点。`half` 在 RISC-V 上不是天然可用的类型：没有 `Zfh` 时，一个 `fadd half` 会变成 `__extendhfsf2`、`fadd.s`、`__truncsfhf2` 的调用链；有 `Zfh` 时才是一条 `fadd.h`。`double` 在没有 `D` 时同样会退化成 `__adddf3`。这些都不是“优化”，而是合法化的必然结果。

向量更明显。固定长度 `<4 x i32>` 在没有 `V` 时会被拆成四条标量 `lw`/`add`/`sw`；有 `V` 时是一条 `vadd.vv`。可伸缩向量 `<vscale x 4 x half>` 在没有 `Zvfh` 时直接让 llc 报错退出。

所以合法化不是边角料，它决定了后端能不能把一类 IR 接住。读 RISC-V 后端的代码时，先看 `RISCVISelLowering` 里的 `setOperationAction`，再看 `GISel/RISCVLegalizerInfo.cpp` 里的规则，基本就能判断“这个操作会走哪条路”。

本章还要纠正一个常见说法。很多资料写“用 `setAction` 设置合法化动作”。在 23.1.4 的 `llvm/CodeGen/GlobalISel/LegalizerInfo.h` 里检索 `setAction` 的结果是 0 次；当前接口是 `getActionDefinitionsBuilder({...})` 返回一个 `LegalizeRuleSet`，再用 `.legalFor()`、`.customFor()`、`.libcallFor()`、`.widenScalarToNextPow2()`、`.clampScalar()` 之类的链式调用描述规则。`setAction` 是更早的表述或别的后端的旧接口，照着它写会编译不过。

## 主线

### 14.1 合法化在两条管线里的位置

SelectionDAG 管线的顺序是：

```text
IR -> SelectionDAG 构建 -> 类型合法化 -> DAG 合法化 -> 指令选择 -> 调度 -> 寄存器分配
```

类型合法化负责把“目标不支持的机器类型”变成“目标支持的类型”，例如 RV32 上的 `i64` 拆成两个 `i32`。DAG 合法化负责把“类型已经合法、但目标没有这条指令”的操作变成别的操作或 libcall，例如没有 `M` 时的 `mul`。

GlobalISel 管线的顺序是：

```text
IR -> IRTranslator（生成 G_* 泛型指令）-> Legalizer -> RegBankSelect -> InstructionSelect
```

GlobalISel 的 Legalizer 使用 `LegalizerInfo` 规则表，输入是 `G_ADD`、`G_FADD` 这类泛型 opcode 和 `LLT` 类型，输出是一个 `LegalizeActionStep`。SelectionDAG 的合法化则使用 `TargetLowering` 里的 `setOperationAction`、`setCondCodeAction`、`setLoadExtAction` 等表。

两条管线都会做“把不合法变成合法”，但规则写法、错误信息和可扩展点都不一样。RISC-V 两套都实现了，所以同一个 IR 用 `-global-isel` 和不加它，输出可能有差别。

### 14.2 `LegalizerInfo` 的规则 DSL

`LegalizerInfo.h` 里的 `LegalizeAction` 枚举包含这些值：

```cpp
Legal
NarrowScalar
WidenScalar
FewerElements
MoreElements
Bitcast
Lower
Libcall
Custom
Unsupported
NotFound
```

含义可以按“动作强度”来记：

- `Legal`：目标能直接选指，什么都不用做。
- `NarrowScalar`：用更窄的标量操作合成，例如 64 位加法用 32 位加法加进位。
- `WidenScalar`：用更宽的类型做，再忽略高位。
- `FewerElements` / `MoreElements`：拆分子向量或补齐向量元素。
- `Bitcast`：换成同尺寸的另一种类型。
- `Lower`：用更简单的操作组合实现，例如 `srem` 变成 `sdiv` 加减法。
- `Libcall`：调用运行时库，例如 `__divdi3`、`__adddf3`。
- `Custom`：交给目标自己的 C++ 回调。
- `Unsupported`：目标明确不支持，属于编程错误。
- `NotFound`：规则表里没有匹配项，通常导致编译失败。

注意 GlobalISel 的 `LegalizeAction` 里没有名为 `Expand` 的动作；最接近的是 `Lower` 和 `Libcall`。`Expand` 是 SelectionDAG 的 `setOperationAction` 使用的动作名，两者不要混用。

规则不是一条一条 `setAction`，而是围绕一个 opcode 建规则集。RISC-V 的 `G_ADD`/`G_SUB` 规则是：

```cpp
getActionDefinitionsBuilder({G_ADD, G_SUB})
    .legalFor({sXLen})
    .customFor(ST.is64Bit(), {s32})
    .widenScalarToNextPow2(0)
    .clampScalar(0, sXLen, sXLen);
```

逐行读：

1. `getActionDefinitionsBuilder({G_ADD, G_SUB})`：为这两个泛型 opcode 建一张规则集。
2. `.legalFor({sXLen})`：XLEN 宽度的标量加法/减法是合法的。RV32 时 `sXLen` 是 `s32`，RV64 时是 `s64`。
3. `.customFor(ST.is64Bit(), {s32})`：RV64 上 `s32` 的加法走 `legalizeCustom`，因为要处理 32 位结果在 64 位寄存器里的符号扩展/高位清零。
4. `.widenScalarToNextPow2(0)`：其他宽度先扩到下一个 2 的幂。
5. `.clampScalar(0, sXLen, sXLen)`：最后把类型夹到 XLEN。RV32 上的 `s64` 因此被导向 32 位规则，Legalizer 再按 64 位语义把它拆成低位/高位两段。

`LegalizeRuleSet::apply` 返回 `LegalizeActionStep`，里面有 `Action`、`TypeIdx`、`NewType` 三个字段。Legalizer 拿到 step 后决定怎么改指令。`Custom` 最终会进入 `RISCVLegalizerInfo::legalizeCustom`，`Libcall` 会进入 `LegalizerHelper` 的库调用生成逻辑。

同一份规则表里还能看到 `G_SHL`/`G_ASHR`/`G_LSHR` 的写法：

```cpp
getActionDefinitionsBuilder({G_SHL, G_ASHR, G_LSHR})
    .legalFor({{sXLen, sXLen}})
    .customFor(ST.is64Bit(), {{s32, s32}})
    .widenScalarToNextPow2(0)
    .clampScalar(1, sXLen, sXLen)
    .clampScalar(0, sXLen, sXLen);
```

注意 `clampScalar(1, ...)` 和 `clampScalar(0, ...)` 的顺序：位移量（type index 1）先夹，结果类型（type index 0）后夹。规则是按书写顺序匹配的，顺序写反会得到不同的 step。

### 14.3 `i64` 在 RV32 上如何被拆开

先用一个同时包含乘、除、加的函数：

```llvm
define i64 @i64ops(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  %d = sdiv i64 %m, %b
  %s = add i64 %m, %d
  ret i64 %s
}
```

RV32 加 `+m` 的实测输出：

```asm
i64ops:
	addi	sp, sp, -16
	sw	ra, 12(sp)
	sw	s0, 8(sp)
	sw	s1, 4(sp)
	mul	a4, a0, a3
	mulhu	a5, a0, a2
	mul	s1, a1, a2
	mul	s0, a0, a2
	add	a4, a5, a4
	add	s1, a4, s1
	mv	a0, s0
	mv	a1, s1
	call	__divdi3
	add	a0, s0, a0
	add	a1, s1, a1
	sltu	a2, a0, s0
	add	a1, a1, a2
	lw	ra, 12(sp)
	lw	s0, 8(sp)
	lw	s1, 4(sp)
	addi	sp, sp, 16
	ret
```

几个关键点：

- 参数 `i64 %a` 在 RV32 上占 `a0`（低 32 位）和 `a1`（高 32 位），`%b` 占 `a2`/`a3`。这是 ABI 的拆分，不是合法化临时决定的。
- `mul` 生成 64 位积需要四个 32 位乘：`mulhu` 取高半、三个 `mul` 取交叉项，再用 `add` 合并。`mulhu` 是 `M` 扩展提供的无符号高位乘。
- `sdiv i64` 没有 RV32 指令，直接 `call __divdi3`。注意即使开了 `+m`，64 位除法仍然是 libcall；`M` 只提供 XLEN 宽度的 `div`/`rem`。
- 低位加法产生的进位用 `sltu` 检测：`sltu a2, a0, s0` 在结果小于被加数 `s0` 时为 1，再把它加到高位。这就是 `NarrowScalar` 的典型形态。

RV64 同一个函数：

```asm
i64ops:
	mul	a0, a0, a1
	div	a1, a0, a1
	add	a0, a0, a1
	ret
```

没有拆分、没有 libcall、没有栈帧。`i64` 就是 XLEN，`mul`/`div` 都是原生指令。

如果 RV64 连 `M` 都没有，`mul i64` 会走 libcall：

```asm
i64mul:
	tail	__muldi3
```

`tail` 而不是 `call`，是因为函数尾部没有其他工作，可以直接尾调用运行时库。这一条来自 `RISCVISelLowering` 的：

```cpp
if (!Subtarget.hasStdExtZmmul()) {
  setOperationAction({ISD::MUL, ISD::MULHS, ISD::MULHU}, XLenVT, Expand);
}
```

`Zmmul` 是只提供乘法、不提供除法的子扩展。没有它时 `MUL` 被标成 `Expand`，合法化器把 `MUL` 变成 libcall；有它但仍是 RV32 时，`MUL i64` 被标成 `Custom`，由目标代码展开成 32 位乘。

GlobalISel 的表现可以对照。RV32 的 `i64add` 实测：

```asm
i64add:
	add	a0, a0, a2
	sltu	a2, a0, a2
	add	a1, a1, a3
	add	a1, a1, a2
	ret
```

RV64 的同一个函数只有 `add a0, a0, a1; ret`。RV32 的 `i64mul` 在 GlobalISel 下是 `mulhu`/三个 `mul`/两个 `add` 的组合，和 SelectionDAG 的乘法部分形态一致。这说明两条管线在 `i64` 拆分上遵循同一套 ABI 和同样的数学分解，差别主要在寄存器和指令选择的细节。

### 14.4 `f16`：libcall、`Zfhmin`、`Zfh` 三条路

`half` 是最能说明“合法化是多条路”的类型。先看没有任何半精度扩展的 RV64：

```asm
hadd:
	addi	sp, sp, -32
	sd	ra, 24(sp)
	fsd	fs0, 16(sp)
	fsd	fs1, 8(sp)
	fmv.s	fs0, fa0
	fmv.s	fa0, fa1
	call	__extendhfsf2
	fmv.s	fs1, fa0
	fmv.s	fa0, fs0
	call	__extendhfsf2
	fadd.s	fa0, fa0, fs1
	call	__truncsfhf2
	fmv.x.w	a0, fa0
	lui	a1, 1048560
	or	a0, a0, a1
	fmv.w.x	fa0, a0
	ld	ra, 24(sp)
	fld	fs0, 16(sp)
	fld	fs1, 8(sp)
	addi	sp, sp, 32
	ret
```

这条链是：两个 `half` 参数分别用 `__extendhfsf2` 扩成 `float`，用 `fadd.s` 相加，再用 `__truncsfhf2` 缩回 `half`。返回值还要做 NaN-boxing：`lui a1, 1048560` 得到 `0xFFFF0000`，`or` 进低 16 位结果，再 `fmv.w.x` 放回 `fa0`。RISC-V 的浮点寄存器是 32 位或 64 位宽，`half` 只占低 16 位，高位必须置 1 表示这是一个合法的 NaN-boxed 值。

开 `Zfhmin`（只保证转换指令）时：

```asm
hadd:
	fcvt.s.h	fa5, fa1
	fcvt.s.h	fa4, fa0
	fadd.s	fa5, fa4, fa5
	fcvt.h.s	fa0, fa5
	ret
```

仍然用 `fadd.s`，但转换由 `fcvt.s.h`/`fcvt.h.s` 完成，不再调用库函数。

开完整 `Zfh` 时：

```asm
hadd:
	fadd.h	fa0, fa0, fa1
	ret
```

一条指令。三种结果的差别不是优化等级，而是扩展是否存在。

GlobalISel 的规则在 `RISCVLegalizerInfo` 里写得很直白：

```cpp
getActionDefinitionsBuilder({G_FADD, G_FSUB, G_FMUL, G_FDIV, G_FMA, G_FSQRT,
                             G_FMAXNUM, G_FMINNUM, G_FMAXIMUMNUM,
                             G_FMINIMUMNUM})
    .legalFor(ST.hasStdExtF(), {s32})
    .legalFor(ST.hasStdExtD(), {s64})
    .legalFor(ST.hasStdExtZfh(), {s16})
    .libcallFor({s32, s64})
    .libcallFor(ST.is64Bit(), {s128});
```

`s16` 只有在 `Zfh` 存在时才合法；`s32`/`s64` 在没有对应扩展时走 libcall。这里没有给 `s16` 写 libcall 规则，所以在 RV64 且没有 `Zfh` 时，GlobalISel 会报：

```text
LLVM ERROR: unable to legalize instruction: %4:_(s16) = G_FADD %0:_, %1:_ (in function: hadd)
```

SelectionDAG 走的是另一条合法化路径（promote + libcall），所以同样的 IR 在默认管线里能编过。这个差异是真实存在的，不是配置错误。

### 14.5 软浮点与 libcall：`dadd` 的真正差异

用 `+f,-d`（有单精度、没有双精度）编：

```llvm
define double @dadd(double %a, double %b) {
  %r = fadd double %a, %b
  ret double %r
}
```

输出：

```asm
dadd:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	call	__adddf3
	ld	ra, 8(sp)
	addi	sp, sp, 16
	ret
```

`.attribute 5` 是：

```text
"rv64i2p1_f2p2_zicsr2p0"
```

开 `+d` 后：

```asm
dadd:
	fadd.d	fa0, fa0, fa1
	ret
```

`.attribute 5` 变成：

```text
"rv64i2p1_f2p2_d2p2_zicsr2p0"
```

`fp128` 更彻底。即使开了 `+d`，`fadd fp128` 仍然是：

```asm
call	__addtf3
```

RISC-V 没有 128 位浮点指令，`fp128` 在合法化时被 `Libcall` 到四精度运行时库。

这里要注意 `-mattr=-d` 的写法。默认 triple 没有 `f`，写 `-mattr=-d` 得到的是 `rv64i2p1`，不是“保留 F 只关 D”。要表达“有 F 无 D”，写 `-mattr=+f,-d`，得到的属性才是 `rv64i2p1_f2p2_zicsr2p0`。

在 SelectionDAG 里，控制这些行为的是 `setOperationAction`。例如没有 `M` 时：

```cpp
setOperationAction({ISD::SDIV, ISD::UDIV, ISD::SREM, ISD::UREM}, XLenVT,
                   Expand);
```

`Expand` 的意思是“合法化器用更简单的操作或 libcall 实现它”。`Custom` 则是“调用 `RISCVTargetLowering::ReplaceNodeResults` 或 `LowerOperation`”。`Legal` 是“已经可以直接选指”。这三个词在源码里反复出现，读表时先分清它们。

### 14.6 向量类型：固定向量与可伸缩向量

固定长度 `<4 x i32>` 在没有 `V` 时会被完全标量化：

```asm
vadd:
	lw	a3, 0(a2)
	lw	a4, 8(a2)
	lw	a5, 16(a2)
	lw	a2, 24(a2)
	lw	a6, 0(a1)
	lw	a7, 8(a1)
	lw	t0, 16(a1)
	lw	a1, 24(a1)
	add	a3, a6, a3
	add	a4, a7, a4
	add	a5, t0, a5
	add	a1, a1, a2
	sw	a3, 0(a0)
	sw	a4, 4(a0)
	sw	a5, 8(a0)
	sw	a1, 12(a0)
	ret
```

这不是“向量化失败”，而是 `FewerElements`/标量化的结果：目标没有向量寄存器，合法化器把向量拆成标量元素。

开 `+v` 后：

```asm
vadd:
	vsetivli	zero, 4, e32, m1, ta, ma
	vadd.vv	v8, v8, v9
	ret
```

`vsetivli` 的 `4` 是 VL，`e32` 是元素宽度，`m1` 是 LMUL。固定长度向量的元素数直接进 VL。

可伸缩向量 `<vscale x 4 x i32>` 在 `+v` 下：

```asm
vadds:
	vsetvli	a0, zero, e32, m2, ta, ma
	vadd.vv	v8, v8, v10
	ret
```

注意 `vsetvli` 而不是 `vsetivli`：VL 不是常数，由硬件 VLEN 决定；`m2` 是因为 4 个 `i32` 在可伸缩向量里需要两个寄存器组来容纳。具体 LMUL 由后端按类型和 VLEN 约束选择，这里的实测值是 `m2`。

半精度可伸缩向量更严格。没有 `Zvfh` 时：

```text
LLVM ERROR: Don't know how to legalize this scalable vector type
```

进程退出码是 `-1073741795`（Windows 上的 `0xC0000409`，LLVM 的 fatal error 路径）。开 `+v,+zvfh` 后：

```asm
vhalf:
	vsetvli	a0, zero, e16, m1, ta, ma
	vfadd.vv	v8, v8, v9
	ret
```

`.attribute 5` 里会出现 `zvfh1p0`、`zvfhmin1p0`、`zve32f1p0` 等一串扩展名。这里的结论是：可伸缩向量的元素类型必须被目标明确支持，不能靠“拆成标量”兜底，因为元素数量在编译期未知。

`RISCVLegalizerInfo` 用两个辅助谓词描述向量合法性：

```cpp
typeIsLegalBoolVec(0, BoolVecTys, ST)
typeIsLegalIntOrFPVec(0, IntOrFPVecTys, ST)
```

`G_ICMP`、`G_SELECT`、`G_FREEZE` 等规则里都能看到它们。布尔向量（mask）和整数/浮点向量是分开判断的，`nxv4i1` 这类类型只有在 `V` 下才有合法表示。

### 14.7 `setOperationAction` 在 `RISCVISelLowering` 里的角色

`RISCVTargetLowering` 构造函数里有一张很大的表。几个和本章直接相关的条目：

```cpp
setOperationAction(ISD::DYNAMIC_STACKALLOC, XLenVT, Custom);
setOperationAction(ISD::BRCOND, MVT::Other, Custom);
setOperationAction(ISD::SELECT_CC, XLenVT, Expand);
setOperationAction(ISD::VASTART, MVT::Other, Custom);
setOperationAction({ISD::VAARG, ISD::VACOPY, ISD::VAEND}, MVT::Other, Expand);

if (!Subtarget.hasStdExtZmmul()) {
  setOperationAction({ISD::MUL, ISD::MULHS, ISD::MULHU}, XLenVT, Expand);
} else if (Subtarget.is64Bit()) {
  setOperationAction(ISD::MUL, MVT::i128, Custom);
  setOperationAction(ISD::MUL, MVT::i32, Custom);
} else {
  setOperationAction(ISD::MUL, MVT::i64, Custom);
}

if (!Subtarget.hasStdExtM()) {
  setOperationAction({ISD::SDIV, ISD::UDIV, ISD::SREM, ISD::UREM}, XLenVT,
                     Expand);
} else if (Subtarget.is64Bit()) {
  setOperationAction({ISD::SDIV, ISD::UDIV, ISD::UREM},
                     {MVT::i8, MVT::i16, MVT::i32}, Custom);
}
```

可以按下面的方式读：

- `DYNAMIC_STACKALLOC` 是 `Custom`，因为动态 `alloca` 要改 `sp` 并处理对齐，不能简单展开。
- `BRCOND` 是 `Custom`，因为 RISC-V 的分支条件需要把 `ISD::SETCC` 翻成 `BEQ`/`BNE` 等，目标有自己的 `lowerBRCOND`。
- `SELECT_CC` 是 `Expand`，先变成 `SETCC` + `SELECT`，再由 `SELECT` 的规则处理。
- `VASTART` 是 `Custom`，因为 `va_list` 的布局由 ABI 决定；`VAARG`/`VACOPY`/`VAEND` 是 `Expand`，用通用展开。
- `MUL` 没有 `Zmmul` 时 `Expand` 到 libcall；RV64 有 `Zmmul` 时 `i32`/`i128` 走 `Custom`，因为 32 位乘法结果要符号扩展，128 位乘法要拆。
- 除法没有 `M` 时 `Expand` 到 `__divdi3` 等；RV64 有 `M` 时 `i8`/`i16`/`i32` 的除法要 `Custom`，因为 RV64 的 `div` 是 64 位操作，窄类型要先扩展再截断。

`Expand`、`Custom`、`Legal` 的区别在调试点上很重要：`Expand` 失败通常表现为 libcall 或更简单的 DAG；`Custom` 失败会直接落到 `LowerOperation` 的 `llvm_unreachable`/`Unimplemented ... Case`；`Legal` 失败才会到指令选择阶段报 `Cannot select`。

### 14.8 诊断路径：三类错误，三个层

把本章见到的错误放在一起：

| 错误信息 | 出现层 | 含义 | 典型修法 |
|---|---|---|---|
| `Don't know how to legalize this scalable vector type` | 类型合法化 | 可伸缩向量的元素类型没有合法表示 | 开对应扩展（如 `+zvfh`），或不要用该类型 |
| `unable to legalize instruction: ... G_FADD ...` | GlobalISel Legalizer | 规则表没有匹配到该 opcode/类型组合 | 补 `LegalizerInfo` 规则或换管线 |
| `LLVM ERROR: Cannot select: intrinsic %llvm.coro.frame` | SelectionDAG 指令选择 | 节点通过了合法化，但没有 pattern/custom 选指 | 先跑必要的前置 pass，或补 lowering/pattern |
| `LLVM ERROR: Unimplemented RISCVTargetLowering::LowerOperation Case` | SelectionDAG `Custom` lowering | 表里标了 `Custom`，但 `LowerOperation` 没实现该节点 | 实现 `LowerOperation` 分支 |

`Cannot select` 这条值得单独说。构造一个直接调用 `llvm.coro.frame` 的函数：

```llvm
define ptr @f(ptr %hdl) {
  %r = call ptr @llvm.coro.frame()
  ret ptr %r
}
declare ptr @llvm.coro.frame()
```

用 `llc -mtriple=riscv64 -mattr=+m,+f,+d -O2 coro.ll -o coro.s` 会得到：

```text
LLVM ERROR: Cannot select: intrinsic %llvm.coro.frame
```

这不是合法化器不认识 `coro.frame`，而是 `coro.frame` 需要协程相关的 IR pass 先把它消除；直接把未处理的 intrinsic 丢给 llc，SelectionDAG 会把它变成一个 `ISD::INTRINSIC_WO_CHAIN` 节点，合法化器认为它合法，但指令选择找不到 pattern。诊断顺序应该是：先确认前置 pass 是否跑过，再看 `-print-after` 的 DAG/MIR，最后才怀疑合法化规则。

另一个真实例子是 `llvm.addressofreturnaddress`：

```text
LLVM ERROR: Unimplemented RISCVTargetLowering::LowerOperation Case
```

这说明该节点被标成了 `Custom`，但 `RISCVTargetLowering::LowerOperation` 的 `switch` 没有对应分支。它和 `Cannot select` 的区别是：错误发生在 lowering 回调里，而不是 pattern 匹配阶段。

## 可跑实验

下面的命令都假设已经设置好工具目录。也可以在仓库根目录用统一入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File 'D:\Learning-with-Claude\compilers\riscv_backend\code\tools\run.ps1' 'labs\14-legalization.ps1'
```

脚本 `code/labs/14-legalization.ps1` 会生成 `legal.ll`、`i64.ll`、`vhalf.ll`，并跑完本章的主要对比。下面把关键命令拆开，便于单独复现。

### 实验 1：同一个 `i64` 函数在 RV32 与 RV64 上

```powershell
$LLVM = 'D:\Learning-with-Claude\.scratch\llvm-23.1.4-root\clang+llvm-23.1.4-x86_64-pc-windows-msvc\bin'
$w = Join-Path $env:TEMP 'riscv-course-lab14'
New-Item -ItemType Directory -Force -Path $w | Out-Null
Set-Location $w

@"
target triple = "riscv64-unknown-elf"
define i64 @i64ops(i64 %a, i64 %b) {
  %m = mul i64 %a, %b
  %d = sdiv i64 %m, %b
  %s = add i64 %m, %d
  ret i64 %s
}
"@ | Set-Content -Encoding ascii legal.ll

& "$LLVM\llc.exe" -mtriple=riscv32 '-mattr=+m,+f,+d' -O2 legal.ll -o legal32.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+f,+d' -O2 legal.ll -o legal64.s
Get-Content legal32.s | Select-String '^i64ops:' -Context 0,32
Get-Content legal64.s | Select-String '^i64ops:' -Context 0,10
```

预期：RV32 出现 `mulhu`、`sltu`、`call __divdi3` 和 16 字节栈帧；RV64 只有 `mul`/`div`/`add`/`ret`。如果 RV32 没有出现 `__divdi3`，检查 `-mattr` 是否被 PowerShell 的逗号解析破坏；含逗号的参数要加单引号。

再跑一个只含加法和乘法的版本，可以看到没有 `M` 时的 libcall：

```powershell
@"
target triple = "riscv64-unknown-elf"
define i64 @i64add(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}
define i64 @i64mul(i64 %a, i64 %b) {
  %r = mul i64 %a, %b
  ret i64 %r
}
"@ | Set-Content -Encoding ascii i64.ll

& "$LLVM\llc.exe" -mtriple=riscv64 -mattr= -O2 i64.ll -o nomul64.s
Get-Content nomul64.s | Select-String '^i64mul:' -Context 0,6
```

预期：`i64mul` 的正文是 `tail __muldi3`。这就是 `ISD::MUL` 被 `Expand` 到 libcall 的直接证据。

### 实验 2：软浮点、硬浮点、`f16` 的三条路

```powershell
@"
target triple = "riscv64-unknown-elf"
define double @dadd(double %a, double %b) {
  %r = fadd double %a, %b
  ret double %r
}
define half @hadd(half %a, half %b) {
  %r = fadd half %a, %b
  ret half %r
}
"@ | Set-Content -Encoding ascii fp.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+f,-d' -O2 fp.ll -o soft-d.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+d' -O2 fp.ll -o hard-d.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+f,+d,+zfhmin' -O2 fp.ll -o zfhmin.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+f,+d,+zfh' -O2 fp.ll -o zfh.s

Get-Content soft-d.s | Select-String '^dadd:' -Context 0,9
Get-Content hard-d.s | Select-String '^dadd:' -Context 0,5
Get-Content zfhmin.s | Select-String '^hadd:' -Context 0,8
Get-Content zfh.s | Select-String '^hadd:' -Context 0,5
```

预期：

- `soft-d.s` 的 `dadd` 是 `call __adddf3`。
- `hard-d.s` 的 `dadd` 是 `fadd.d`。
- `zfhmin.s` 的 `hadd` 是 `fcvt.s.h` + `fadd.s` + `fcvt.h.s`。
- `zfh.s` 的 `hadd` 是一条 `fadd.h`。

还可以检查 `.attribute 5`：`+f,-d` 是 `"rv64i2p1_f2p2_zicsr2p0"`，`+d` 是 `"rv64i2p1_f2p2_d2p2_zicsr2p0"`。属性字符串是判断“扩展到底开没开”的可靠证据，比看命令行更稳。

### 实验 3：固定向量与可伸缩向量

```powershell
@"
target triple = "riscv64-unknown-elf"
define <4 x i32> @vadd(<4 x i32> %a, <4 x i32> %b) {
  %r = add <4 x i32> %a, %b
  ret <4 x i32> %r
}
"@ | Set-Content -Encoding ascii vec-fixed.ll

@"
target triple = "riscv64-unknown-elf"
define <vscale x 4 x i32> @vadds(<vscale x 4 x i32> %a, <vscale x 4 x i32> %b) {
  %r = add <vscale x 4 x i32> %a, %b
  ret <vscale x 4 x i32> %r
}
"@ | Set-Content -Encoding ascii vec-scalable.ll

& "$LLVM\llc.exe" -mtriple=riscv64 -O2 vec-fixed.ll -o vec-nov.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+v' -O2 vec-fixed.ll -o vec-v.s
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+v' -O2 vec-scalable.ll -o vec-scalable-v.s
Get-Content vec-nov.s | Select-String '^vadd:' -Context 0,18
Get-Content vec-v.s | Select-String '^vadd:' -Context 0,5
Get-Content vec-scalable-v.s | Select-String '^vadds:' -Context 0,5
```

预期：`vec-nov.s` 的 `vadd` 是 4 组 `lw`/`add`/`sw`；`vec-v.s` 的 `vadd` 是 `vsetivli zero, 4, e32, m1, ta, ma` 加 `vadd.vv`；`vadds` 是 `vsetvli a0, zero, e32, m2, ta, ma` 加 `vadd.vv`。

### 实验 4：失败路径与 GlobalISel 差异

可伸缩 `half` 没有 `Zvfh`：

```powershell
@"
target triple = "riscv64-unknown-elf"
define <vscale x 4 x half> @vhalf(<vscale x 4 x half> %a, <vscale x 4 x half> %b) {
  %r = fadd <vscale x 4 x half> %a, %b
  ret <vscale x 4 x half> %r
}
"@ | Set-Content -Encoding ascii vhalf.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+v' -O2 vhalf.ll -o vhalf.s
```

预期：进程报 `LLVM ERROR: Don't know how to legalize this scalable vector type`，退出码 `-1073741795`。把 `-mattr=+v` 改成 `-mattr=+v,+zvfh` 后成功，汇编里是 `vsetvli a0, zero, e16, m1, ta, ma` 和 `vfadd.vv`。

`Cannot select`：

```powershell
@"
target triple = "riscv64-unknown-elf"
define ptr @f(ptr %hdl) {
  %r = call ptr @llvm.coro.frame()
  ret ptr %r
}
declare ptr @llvm.coro.frame()
"@ | Set-Content -Encoding ascii coro.ll

& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m,+f,+d' -O2 coro.ll -o coro.s
```

预期：`LLVM ERROR: Cannot select: intrinsic %llvm.coro.frame`，退出码 `-1073741795`。

GlobalISel 的 `f16` 差异：

```powershell
& "$LLVM\llc.exe" -mtriple=riscv64 '-mattr=+m' -O2 -global-isel fp.ll -o gisel-fp.s
```

预期：`LLVM ERROR: unable to legalize instruction: %4:_(s16) = G_FADD %0:_, %1:_ (in function: hadd)`，退出码 1。把 `-global-isel` 去掉，同一个文件能编过，只是 `hadd` 变成 `__extendhfsf2`/`__truncsfhf2` 链。

## 源码落点

- `llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp`
  - `RISCVLegalizerInfo::RISCVLegalizerInfo`：全部 `getActionDefinitionsBuilder` 规则。
  - `{G_ADD, G_SUB}` 规则：`.legalFor({sXLen})`、`.customFor(ST.is64Bit(), {s32})`、`.widenScalarToNextPow2(0)`、`.clampScalar(0, sXLen, sXLen)`。
  - `{G_SHL, G_ASHR, G_LSHR}` 规则：两个 `clampScalar` 的顺序展示 type index 的作用。
  - `G_FADD` 规则：`legalFor` 按 `F`/`D`/`Zfh` 分别给 `s32`/`s64`/`s16`，其余走 `libcallFor`。
  - `G_CONSTANT` 规则：`.legalFor({p0})`、RV32 的 `s32`、RV64 的 `s64` custom，配合 `widenScalarToNextPow2`。
  - `legalizeCustom`：处理 `G_FCONSTANT`、`G_CONSTANT`、`G_LOAD`、`G_STORE` 等 `Custom` 动作。
  - `legalizeIntrinsic`：intrinsic 的 GlobalISel 合法化入口。
  - `shouldBeInConstantPool`：判断大常量是否放常量池，和 `RISCVMatInt::generateInstSeq` 的成本估计配合。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`
  - `RISCVTargetLowering::RISCVTargetLowering`：`setOperationAction` 主表。
  - `ISD::DYNAMIC_STACKALLOC`、`ISD::BRCOND`、`ISD::SELECT_CC`、`ISD::VASTART`、`ISD::VAARG` 等条目。
  - `ISD::MUL` 分支：`Zmmul` 缺失时 `Expand`；RV64 的 `i32`/`i128` `Custom`；RV32 的 `i64` `Custom`。
  - `ISD::SDIV`/`UDIV`/`SREM`/`UREM` 分支：无 `M` 时 `Expand`，RV64 窄类型 `Custom`。
- `llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h`
  - `LegalizeActions::LegalizeAction`：`Legal` 到 `NotFound` 的完整枚举。
  - `LegalizeActionStep`：`Action`、`TypeIdx`、`NewType`。
  - `LegalizeRuleSet`：`legalFor`、`customFor`、`libcallFor`、`lowerFor`、`widenScalarToNextPow2`、`clampScalar`、`unsupported`。
  - 该头文件里没有 `setAction`；当前 API 是规则集 DSL。
- `llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.h`
  - `RISCVLegalizerInfo` 的私有辅助函数：`legalizeShlAshrLshr`、`legalizeExt`、`legalizeLoadStore`、`shouldBeInConstantPool`。
- `llvm/lib/Target/RISCV/RISCVSubtarget.h`
  - `hasStdExtM`、`hasStdExtZmmul`、`hasStdExtF`、`hasStdExtD`、`hasStdExtZfh`、`hasStdExtZfhmin`、`is64Bit` 等谓词，规则表里大量使用。

## 常见坑

1. **把 `setAction` 当成当前 API。** 23.1.4 的 `LegalizerInfo.h` 里没有这个成员；写规则要用 `getActionDefinitionsBuilder(...).legalFor(...)...`。看到旧文章里的 `setAction`，先确认它讲的是不是别的版本或别的后端。

2. **在 PowerShell 里不加引号地传 `-mattr=+m,+a,+f,+d`。** PowerShell 会把逗号当成数组分隔符，实际传给 llc 的参数会被截断。所有含逗号的 `-mattr` 都要写成 `'-mattr=+m,+a,+f,+d'`。

3. **把 `-mattr=-d` 当成“只关 D”。** 默认 triple 没有 `f`，`-mattr=-d` 得到的是 `rv64i2p1`，连 F 也没有。要构造“有 F 无 D”的软双精度场景，用 `+f,-d`。

4. **以为 RV32 开了 `+m` 就有 64 位除法。** `M` 只给 XLEN 宽度的 `div`/`rem`；`i64` 除法在 RV32 上仍然 `call __divdi3`。看到 `__divdi3` 不是配置错误。

5. **把 `Cannot select` 当成合法化失败。** `Cannot select` 发生在指令选择阶段，说明节点已经通过了合法化但没有 pattern；合法化失败通常报 `Don't know how to legalize` 或 `unable to legalize instruction`。两者的修法不同。

6. **在 SelectionDAG 和 GlobalISel 之间直接对比 `f16`。** 同一个 `hadd`，SelectionDAG 能用 promote + libcall 编过，GlobalISel 在没有 `Zfh` 时可能报 `G_FADD s16` 无法合法化。对比时要确认两条管线都支持该类型。

7. **忽略 `.attribute 5`。** 属性字符串是扩展是否真正启用的证据。命令行写了 `+d` 但属性里没有 `d2p2`，说明参数没有生效或被 ABI 检查忽略。

## 自我检查

- [ ] 能说出 `LegalizeAction` 里 `Legal`、`Lower`/`Libcall`、`Custom` 的区别。
- [ ] 能在 `RISCVLegalizerInfo.cpp` 里找到 `G_ADD` 规则，并解释四个链式调用的顺序为什么不能随意换。
- [ ] 能解释 RV32 上 `i64` 加法的 `sltu` 进位链，以及为什么 `sdiv i64` 走 `__divdi3`。
- [ ] 能分别用 `+f,-d`、`+f,+d`、`+f,+d,+zfhmin`、`+f,+d,+zfh` 编 `dadd`/`hadd`，并说出汇编差异。
- [ ] 能解释 `lui a1, 1048560` 在 `half` 返回值里的作用（NaN-boxing）。
- [ ] 能区分 `Don't know how to legalize this scalable vector type`、`unable to legalize instruction`、`Cannot select` 三个错误分别来自哪一层。
- [ ] 能构造一个可伸缩 `half` 向量，验证没有 `Zvfh` 时的失败和加上 `+zvfh` 后的成功。
- [ ] 能说明为什么 `-mattr=-d` 不等于“保留 F 只关 D”。

## 延伸阅读

- `llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp`：本章最直接的规则表，建议从构造函数读到 `legalizeCustom`。
- `llvm/include/llvm/CodeGen/GlobalISel/LegalizerInfo.h`：`LegalizeAction`、`LegalizeActionStep`、`LegalizeRuleSet` 的完整接口。
- `llvm/lib/Target/RISCV/RISCVISelLowering.cpp`：`setOperationAction` 主表和 `LowerOperation`/`ReplaceNodeResults` 的分工。
- `llvm/lib/Target/RISCV/RISCVSubtarget.h`：扩展谓词，规则表里的 `hasStdExt*` 都从这里来。
- `llvm/test/CodeGen/RISCV/`：同版本测试目录里可以按 `f16`、`rv32`、`global-isel` 搜索对应 lit 用例；本机的 sparse clone 只包含 `lib/Target/RISCV`，测试文件需要从完整源码树获取。
- RISC-V psABI 文档：解释 `i64` 在 RV32 上的寄存器拆分和 `half` 的 NaN-boxing 约定，和本章的汇编现象对应。
- LLVM GlobalISel 上游文档：Legalizer 的规则 DSL、`LegalizeActionStep` 和 `legalizeCustom` 的设计说明。

[上一章：练习 C：ABI 与栈帧审计](./practice-c-abi-audit.md) | [下一章：SelectionDAG 选指](./15-selection-dag.md)