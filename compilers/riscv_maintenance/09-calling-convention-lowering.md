# Ch 09 — 调用约定 lowering

> **目标**：读懂 `CC_RISCV` 如何把 IR 的调用/返回翻译成 RISC-V 的寄存器与栈槽；能对任意签名（聚合、`sret`、`byval`、varargs、`i128`、RVV）说出"它会落在哪里"，并能在汇编里验证。

> **环境**：同 Ch 00。`T=/home/ubuntu/llvm/builds/ct-instr-trunk`，HEAD `0fc5afa04c23`。

> **前置**：Ch 02（ABI 与 `-mabi`）、Ch 05（`CSR_*` 名单与寄存器类）。Ch 05 讲"哪些寄存器必须保存"，本章讲"参数和返回值怎么摆进去"。

---

## 9.1 为什么维护者要单独读 lowering

`RISCVCallingConv.td` 只描述"保存名单"，真正的分配逻辑在 C++ 里。这个分工带来一个典型现象：**ABI 文档读起来很简单，实现却有一百多个分支**。

维护者遇到的 lowering bug 有三类：

1. **前端与后端的责任边界错了。** RISC-V 走后端 lowering（不像 x86 那样前端就把结构体拆好），所以"结构体怎么传"这件事由 `CC_RISCV` 决定。`clang` 的 `CodeGen` 只负责决定 `sret`/`byval` 属性，剩下的都在后端。
2. **某个角落在某条路径上漏了。** `LowerFormalArguments`、`LowerCall`（含调用点的返回值位置）、`LowerReturn` 三条路径必须一致；只改一条，另一个方向就炸。
3. **属性和调用约定不匹配。** 同一个函数在一个翻译单元里声明成 `riscv_vector_cc`、在另一个里没有，链接能过，运行时栈会被写坏。这类 bug 在 Ch 05 已经见过一次（向量跨默认 CC 全 spill）。

本章的代码分布：

| 文件 | 行数 | 内容 |
|---|---|---|
| `llvm/lib/Target/RISCV/RISCVCallingConv.cpp` | 877 | `CC_RISCV`/`RetCC_RISCV`/FastCC/GHC，全部参数分配逻辑 |
| `llvm/lib/Target/RISCV/RISCVCallingConv.h` | 34 | 对外接口：`CC_RISCV`、`getArgGPRs/getArgFPRs/getArgVRs` |
| `RISCVISelLowering.cpp` 的三处 | — | `LowerFormalArguments`(28701)、`LowerCall`(29013)、`LowerReturn`(29538) |

注意：`RISCVCallingConv.cpp` 是本章的主战场，不是 `.td`。很多人读 ABI 时只翻 `.td`，然后在 `RISCVISelLowering.cpp` 里找不到"为什么第 9 个参数在栈上"的代码——答案在 `RISCVCallingConv.cpp:392` 的 `CC_RISCV_Impl`。

---

## 9.2 先建立直觉：一次调用的三段式

```
        IR: call i64 @f(i64 %a, double %b, %S* byval(%S) %s)
                              │
                    ┌─────────▼──────────┐
                    │ CC_RISCV_Impl      │   逐个值问四个问题：
                    │ (每个值调用一次)    │   1) 有 FPR 可用吗？（且不是 vararg）
                    └─────────┬──────────┘   2) 否则有 GPR 可用吗？
                              │              3) 都不行 → 栈槽
                              ▼              4) 是 split 的一半吗？→ 挂起
              ┌───────────────────────────────┐
              │ CCState 的分配结果             │
              │  %a  → a0        (Reg, i64)   │
              │  %b  → fa0       (Reg, f64)   │
              │  %s  → a1        (Indirect)   │  byval: 只传指针
              └───────────────┬───────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  LowerFormalArguments   LowerCall            LowerReturn
  （被调用者：把位置      （调用者：把值        （把返回值放进
    变成 COPY/MOV）        放进寄存器/栈）       a0/a1 或 fa0/fa1）
```

三个必须同时成立的性质：

- 调用者按 `CC_RISCV` 放，被调用者按同一个 `CC_RISCV` 取；
- 调用点的返回值位置用 `RetCC_RISCV` 算（在 `LowerCall` 内部，`RISCVISelLowering.cpp:29484`）；
- 函数自己的返回值用 `RetCC_RISCV` 放（`LowerReturn`，29538）。

任何一处换了规则而另两处没换，就是 ABI break。

---

## 9.3 五个调用约定入口

`RISCVCallingConv.cpp` 里对外暴露的分配函数只有这几个：

| 函数 | 行号 | 用途 |
|---|---|---|
| `CC_RISCV` | 34 | 普通参数（`C`/`Fast`/`PreserveMost`/`GRAAL`/`VectorCall` 等） |
| `RetCC_RISCV` | 49 | 返回值 |
| `CC_RISCV_Impl` | 392 | 上面两个共用的实现，多一个 `bool IsRet` |
| `CC_RISCV_FastCC` | 688 | `fastcc`：额外用 `x28`–`x31` 做参数寄存器 |
| `CC_RISCV_GHC` | 809 | GHC：要求 F/D（或 Zfinx/Zdinx），RVE 直接报错 |

`CC_RISCV` 本体只有十几行，是个薄包装：

```cpp
bool llvm::CC_RISCV(unsigned ValNo, MVT ValVT, MVT LocVT,
                    CCValAssign::LocInfo LocInfo, ISD::ArgFlagsTy ArgFlags,
                    CCState &State) {
  return CC_RISCV_Impl(ValNo, ValVT, LocVT, LocInfo, ArgFlags,
                       /*OrigTy=*/nullptr, State, /*IsRet=*/false);
}
```

真正的分支全在 `CC_RISCV_Impl`（392–686）。读它的顺序建议：先看 `IsRet` 的两条早退（返回值最多两个寄存器），再看 FPR 分支，再看 GPR 分支，最后看 split/vararg 收尾。

参数寄存器池由三个小函数给出，它们就是 ABI 的"字典"：

```cpp
ArrayRef<MCPhysReg> RISCV::getArgGPRs(const RISCVSubtarget &STI) {
  static const MCPhysReg ArgIGPRs[] = {X10, X11, X12, X13, X14, X15, X16, X17};
  static const MCPhysReg ArgEGPRs[] = {X10, X11, X12, X13, X14, X15};
  if (RISCVABI::isRVE(ABI)) return ArrayRef(ArgEGPRs);
  return ArrayRef(ArgIGPRs);
}
```

`getArgGPRs`(167) 给 8 个（RVE 给 6 个），`getArgFPRs`(185) 在软浮点或没有 F 扩展时返回空，`getArgVRs`(198) 只在有 V 时非空。`fastcc` 另有一份 `getFastCCArgGPRs`，多出 `x28`–`x31` 四个。

---

## 9.4 整数参数：8 个寄存器，然后进栈

`nine(long a..i)` 与它的调用者是最好的一对样本：

```asm
nine:                                   # 第 9 个参数从栈上取
	ld	t0, 0(sp)
	add	a0, a0, a1
	...

ninecall:
	addi	sp, sp, -16
	sd	ra, 8(sp)
	ld	t0, 16(sp)      # 自己的第 9 个入参（调整 sp 前在 0(sp)）
	sd	t0, 0(sp)       # 放到出参的 0(sp)
	call	g9
```

要点：

- `a0`–`a7` 是前 8 个整数/指针参数；
- 第 9 个起进 **outgoing argument area**，位于 `sp` 处（不是"压栈"那种 push，而是固定偏移）；
- 调用者必须预留这块区域（`maxCallFrameSize`），`LowerCall` 用 `ArgCCInfo.getStackSize()` 算出来（`RISCVISelLowering.cpp:29059` 附近）。

### RV32 的 `i64` 占两个槽

```asm
add64:                                  # long long add64(long long a, long long b)
	add	a0, a0, a2      # a.lo + b.lo
	add	a1, a1, a3      # a.hi + b.hi
	sltu	a2, a0, a2      # 进位
	add	a1, a1, a2
```

`a` 在 `a0/a1`、`b` 在 `a2/a3`。注意**没有**"必须落在偶数寄存器"的对齐要求——`mix(int x, long long a, long long b)` 里 `x` 占 `a0`，`a` 落在 `a1/a2`（奇数起）。偶数对齐只在 varargs 的特定场景生效（9.7 节）。

### `i128` 在 RV64 也是两个寄存器

```asm
pair_i128(__int128 x):
	add	a0, a0, a1      # x.lo + x.hi
	ret
```

`x` 在 `a0/a1`。返回 `__int128` 时同样用 `a0/a1`，但要注意高低位顺序（`ret_i128` 里能看到 `mv a2, a0; mv a0, a1` 这类搬移）。

---

## 9.5 浮点参数：先问"是不是 vararg"

`CC_RISCV_Impl` 开头有一对开关（`RISCVCallingConv.cpp:437-452`）：

```cpp
bool IsDoubleFPABI = RISCVABI::isDoubleFP(ABI);
if (IsDoubleFPABI || RISCVABI::isSingleFP(ABI))
  AllowFPRForF16_F32 = !ArgFlags.isVarArg();
if (IsDoubleFPABI)
  AllowFPRForF64 = !ArgFlags.isVarArg();
```

`!ArgFlags.isVarArg()` 是 RISC-V ABI 的一条硬规则：**可变参数里的浮点值走整数寄存器**，因为被调用者不知道参数类型。实测：

```c
double variadic_caller(double x) { return variadic_callee(2, x, 3.5); }
```

```asm
variadic_caller:
	fmv.x.d	a1, fa0          # 参数 x 从 fa0 搬到整数寄存器 a1
	lui	a2, 4099
	slli	a2, a2, 38       # 3.5 的位模式
	li	a0, 2
	tail	variadic_callee
```

三个参数全部在 `a0`/`a1`/`a2`，一个 FPR 都没用。

### `lp64` vs `lp64d`：四条指令的差距

```c
double both(double a, double b) { return a+b; }
```

```asm
# -mabi=lp64d
both:
	fadd.d	fa0, fa0, fa1
	ret

# -mabi=lp64
both:
	fmv.d.x	fa5, a1
	fmv.d.x	fa4, a0
	fadd.d	fa5, fa4, fa5
	fmv.x.d	a0, fa5
	ret
```

软浮点 ABI 下，`double` 在整数寄存器里传，进函数先 `fmv.d.x` 搬进 FPR，算完再 `fmv.x.d` 搬回。**这是 ABI 选择直接决定代码质量的最直观例子**：同一份源码，`lp64d` 一条指令，`lp64` 五条。

### Zfinx / Zdinx / Zhinx

没有 F/D 扩展时，RISC-V 有"浮点运算用整数寄存器"的变体。`CC_RISCV_Impl` 里对应三段：

```cpp
if (LocVT == MVT::f16 && Subtarget.hasStdExtZhinxmin()) { /* GPR16 */ }
if (LocVT == MVT::f32 && Subtarget.hasStdExtZfinx())     { /* GPR32 */ }
if (LocVT == MVT::f64 && XLen == 64 && Subtarget.hasStdExtZdinx()) { /* 直接 GPR */ }
```

注意 `Zdinx` 的分支在 RV64 上是"不需要 bitcast 直接放 GPR"，而 RV32 上的 `f64` 走的是后面"拆成两个 GPR"的路径（9.6 节）。

---

## 9.6 聚合类型：三条规则决定一切

`RISCVCallingConv.cpp:57-95` 有一段长注释，实际上是 RISC-V psABI 的摘要。压成三条：

1. **能放进两个 XLEN 的，拆成寄存器**（字段分别走 GPR/FPR，允许 `fp+fp`、`int+fp`）；
2. **超过两个 XLEN 的，传指针**（`byval`：调用者负责复制）；
3. **返回值超过两个 XLEN 的，用 `sret`**（隐藏指针放 `a0`）。

依据是 `CC_RISCV_Impl:425` 的早退：

```cpp
// Any return value split in to more than two values can't be returned directly.
if ((!LocVT.isVector() || Subtarget.isPExtPackedType(LocVT)) && IsRet && ValNo > 1)
  return true;
```

`return true` 的意思是"这个值不能直接返回"，前端就会改写成 `sret`。

### 实测四连

```c
typedef struct { long a, b; } S2;
typedef struct { double x, y; } D2;
typedef struct { long a; double d; } LD;
typedef struct { long a, b, c; } S3;

S3   ret3(void);
void pass3(S3 s);
void pass2(S2 s);
void passd2(D2 s);
void passld(LD s);
```

```asm
ret3:                       # sret：a0 是隐藏的返回缓冲区指针
	li	a1, 3
	li	a2, 1
	li	a3, 2
	sd	a2, 0(a0)
	sd	a3, 8(a0)
	sd	a1, 16(a0)
	ret

pass3:                      # byval：a0 是调用者复制好的指针
	addi	sp, sp, -32
	sd	ra, 24(sp)
	ld	a1, 0(a0)
	ld	a2, 8(a0)
	ld	a0, 16(a0)
	sd	a1, 0(sp)       # 再复制到自己的栈上给 take_s3
	sd	a2, 8(sp)
	sd	a0, 16(sp)
	mv	a0, sp
	call	take_s3

pass2:                      # 两个 long → a0, a1
	add	a0, a0, a1
	tail	take_l

passd2:                     # 两个 double → fa0, fa1（fp+fp）
	fadd.d	fa0, fa0, fa1
	tail	take_d

passld:                     # long + double → a0 + fa0（int+fp）
	fcvt.l.d	a1, fa0, rtz
	add	a0, a0, a1
	tail	take_l
```

四个形态各不相同：

- `S3`（24 字节 > 16）→ 指针；
- `S2`（16 字节）→ `a0`/`a1`；
- `D2` → `fa0`/`fa1`；
- `LD` → 整数在 `a0`、浮点在 `fa0`，**各自用各自的寄存器堆**。

`pass3` 里那个"再复制一次到 `sp`"值得注意：`byval` 的语义是"被调用者拥有自己的副本"，所以调用者把指针指向的对象复制到自己的 outgoing argument area，再把 `sp` 传下去。

### RV32 上的 `f64`

`CC_RISCV_Impl:544-570` 专门处理 `XLen == 32 && LocVT == MVT::f64`：能凑到两个 GPR 就走两个 GPR，凑不到就"低半在 GPR、高半在栈"，再不够就整个进栈。这段代码的注释写得很清楚，维护者改栈布局时必须回来看。

---

## 9.7 varargs：被调用者要给自己留一个"寄存器转存区"

可变参数函数的 prologue 会把参数寄存器写进一块连续内存，`va_arg` 再从这里取。这块区域的大小就是 MIR 里的 `varArgsSaveSize`：

```bash
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c \
  -stop-after=prolog-epilog -simplify-mir va.ll -o - | grep -E "varArgs|stackSize"
```

```
  stackSize:       80
  varArgsFrameIndex: -1
  varArgsSaveSize: 64
```

64 字节 = 8 个整数参数寄存器 × 8 字节。**没有 FPR 保存区**，因为（9.5 节）varargs 的浮点值走整数寄存器。

同一个文件里还能看到 `SD killed renamable $x11, $x2, 24 :: (store (s64) into %fixed-stack.1)` 这一串：这就是把 `a1`–`a7` 写进 `va_list` 的 save area。

### 偶数寄存器规则

`CC_RISCV_Impl:519-536` 有一段容易忽略的逻辑：

```cpp
// If this is a variadic argument, the RISC-V calling convention requires
// that it is assigned an 'even' or 'aligned' register if it has 8-byte
// alignment (RV32) or 16-byte alignment (RV64).
unsigned TwoXLenInBytes = (2 * XLen) / 8;
if (ArgFlags.isVarArg() && ArgFlags.getNonZeroOrigAlign() == TwoXLenInBytes && ...
    ABI != RISCVABI::ABI::ILP32E) {
  unsigned RegIdx = State.getFirstUnallocated(ArgGPRs);
  if (RegIdx != std::size(ArgGPRs) && RegIdx % 2 == 1)
    State.AllocateReg(ArgGPRs);       // 跳过奇数号寄存器
}
```

实测能直接看到"跳过一个"：

```c
extern void vcallee(int n, ...);
void vcaller(long a, long b) { vcallee(1, a, b, (__int128)1); }
```

```asm
vcaller:
	mv	a2, a1          # b → a2
	mv	a1, a0          # a → a1
	li	a0, 1           # n → a0
	li	a4, 1           # __int128 低半 → a4
	li	a5, 0           # 高半 → a5
	tail	vcallee
```

`a3` 被跳过，`__int128` 落在 `a4/a5`。理由：16 字节对齐的参数必须从偶数号寄存器开始，否则 `va_arg` 的对齐计算会错。

`ILP32E` 被显式排除，注释说得很直白：为了兼容 GCC 行为，等 RV32E/ILP32E 正式化后再改。这类"已知不一致"是维护者最该小心的地方——改它需要先对 GCC 和 psABI 做调研。

---

## 9.8 向量参数与特殊调用约定

### `riscv_vector_cc`

向量参数走 `getArgVRs`（`RISCVCallingConv.cpp:198`）与 `allocateRVVReg`（346）。`allocateRVVReg` 会按 LMUL 找连续可用的对齐寄存器组，找不到就退化成"传地址 + 间接"：

```cpp
Reg = allocateRVVReg(LocVT, ValNo, State, TLI);
if (Reg) { /* 直接放向量寄存器 */ }
else {
  if (IsRet) return true;                     // 返回值必须全在寄存器或全在栈
  if ((Reg = State.AllocateReg(ArgGPRs))) {   // 用 GPR 传地址
    LocVT = XLenVT; LocInfo = CCValAssign::Indirect;
  }
  ...
}
```

这里有一个 Ch 05 埋下的坑，值得用实测钉死。两次编译同一段代码，只差被调用者声明上有没有 `riscv_vector_cc`：

```c
// 版本 A：ext 用默认 C 调用约定
extern vint32m1_t ext(vint32m1_t, vint32m1_t);
__attribute__((riscv_vector_cc))
vint32m1_t caller(vint32m1_t a, vint32m1_t b, vint32m1_t c, vint32m1_t d);
```

```asm
caller:
	addi	sp, sp, -48
	sd	ra, 40(sp)
	csrr	a0, vlenb
	li	a1, 18
	mul	a0, a0, a1
	sub	sp, sp, a0          # 预留 18 个 VLENB
	...
	vs1r.v	v1, (a0)           # vscale x 8-byte Folded Spill
	vs2r.v	v2, (a0)
	vs4r.v	v4, (a0)
	vs8r.v	v24, (a0)
```

版本 B 只把 `ext` 的声明改成 `extern __attribute__((riscv_vector_cc)) ...`：

```asm
caller:
	addi	sp, sp, -48
	sd	ra, 40(sp)
	csrr	a0, vlenb
	slli	a1, a0, 1
	add	a0, a0, a1
	sub	sp, sp, a0          # 只预留 3 个 VLENB
	...
	vs1r.v	v26, (a0)
	vs2r.v	v24, (a0)
```

18 → 3。差别不是"优化开关"，而是**调用点掩码里有没有向量寄存器**。声明不匹配时，`ext` 被当成普通 C 函数，`csr_ilp32d_lp64d` 一个向量都不保，所以所有活跃向量必须落栈。

这类 bug 的排查方法：看 `PseudoCALL` 后面的掩码名，再对照被调用函数的 `.variant_cc` 汇编指令。

### `fastcc` 与 `GHC`

- `CC_RISCV_FastCC`(688)：参数寄存器扩到 `x10`–`x17` + `x28`–`x31`（12 个），但**不用** `x5`/`x6`（save-restore libcall 要用）、不用 `x7`（Zicfilp 的 label 寄存器）。注释还老实承认："FastCC has less than 1% performance improvement for some particular benchmark."
- `CC_RISCV_GHC`(809)：要求 `(Zfinx/F)` 与 `(Zdinx/D)`，RVE 上直接 `reportFatalUsageError`。GHC 只支持 `void` 返回（`LowerReturn` 里有断言）。

---

## 9.9 返回值：两个寄存器封顶

`RetCC_RISCV`(49) 与 `CC_RISCV_Impl` 共用实现，`IsRet=true` 时：

- `ValNo > 1` 且不是向量 → 失败（`return true`），前端改 `sret`；
- `f64` 在 RV32 软浮点 → 走 `CC_RISCVAssign2XLen`（299）拆两个 GPR；
- P-ext 的 double-wide 打包类型只能返回 1 个（`:432-434`）；
- 向量返回值必须"全寄存器或全栈"，不允许一半一半。

实测 `S3` 的 `sret` 已经在 9.6 节看过。`i128` 返回在 `ret_i128` 里能看到 `a0/a1` 的配对搬移。

---

## 9.10 尾调用资格

`LowerCall` 在 `RISCVISelLowering.cpp:29050` 调用 `isEligibleForTailCallOptimization`（定义在 28923）。不满足时退回普通 `call`，满足时生成 `tail`。

常见的不合格原因（都能在实测里构造）：

- 有 `byval`/`sret` 参数需要调用者复制；
- 可变参数（`va_list` 可能指向自己的栈帧）；
- 返回值需要 `sret` 而当前栈帧还没销毁；
- `musttail` 但签名不兼容 → `reportFatalInternalError`。

本章的样本里 `pass2`/`passd2`/`passld` 都变成了 `tail`，而 `pass3`（`byval`）没有——这是一个干净的对比例子。

---

## 9.11 源码落点

- **`llvm/lib/Target/RISCV/RISCVCallingConv.cpp`**（877 行）
  - 21–56：`CC_RISCV_Impl`、`CC_RISCV`、`RetCC_RISCV` 的接口与 `IsRet` 参数说明。
  - 57–95：psABI 的六条规则注释，**整章最好的导读**。
  - 96–215：`ArgVRs`/`ArgFPR*`/`ArgGPR*` 寄存器池与 `getArgGPRs`/`getArgFPRs`/`getArgVRs`。
  - 299–388：`CC_RISCVAssign2XLen`，2×XLEN 值的拆分。
  - 392–686：`CC_RISCV_Impl`，全部主逻辑；`IsRet` 早退在 425，vararg 偶数规则在 519，RV32 `f64` 在 544，向量在 610 左右。
  - 688–808：`CC_RISCV_FastCC`。
  - 809–877：`CC_RISCV_GHC`。

- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**
  - 28701–28920：`LowerFormalArguments`，含 `interrupt` 属性、`byval` 复制、`varArgsSaveSize` 设置。
  - 28920–29012：`isEligibleForTailCallOptimization`。
  - 29013–29528：`LowerCall`，含 `AnalyzeCallOperands`(29046)、出参栈布局、调用点返回值位置(29484)。
  - 29529–29537：`CanLowerReturn`。
  - 29538–29640：`LowerReturn`。

- **`llvm/lib/Target/RISCV/RISCVCallingConv.td`**（101 行）
  - 只看"保存名单"即可，参数分配不在这里（Ch 05 已展开）。

- **`clang/include/clang/Basic/AttrDocs.td`**
  - 6946 起：`riscv::vector_cc / riscv_vector_cc` 的语义（保留 15 个向量寄存器等）。

- **测试**
  - `llvm/test/CodeGen/RISCV/calling-conv-ilp32e.ll`、`vararg-ilp32e.ll`：RVE 的 6 个参数寄存器与 vararg。
  - `llvm/test/CodeGen/RISCV/calling-conv-preserve-most.ll`：`preserve_mostcc`。
  - `llvm/test/CodeGen/RISCV/rvv/callee-saved-regs.ll`、`rvv-cfi-info.ll`：`riscv_vector_cc` 的保存行为。
  - `llvm/test/CodeGen/RISCV/vararg.ll`、`varargs-with-fp-and-second-adj.ll`：`varArgsSaveSize`、save area 与浮点参数相邻的场景。

---

## 9.12 可跑实验

### 实验 1：9 个参数的分界线

```bash
cd /tmp && cat > n9.c <<'EOF'
long nine(long a,long b,long c,long d,long e,long f,long g,long h,long i) { return a+b+c+d+e+f+g+h+i; }
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 -S -o - n9.c | sed -n '/nine:/,/Lfunc_end/p'
```

看 `ld t0, 0(sp)`：第 9 个参数在栈上。

### 实验 2：软/硬浮点的四条指令差

```bash
cd /tmp && cat > sf.c <<'EOF'
double both(double a, double b) { return a+b; }
EOF
for ABI in lp64 lp64d; do
  echo "--- $ABI ---"
  $T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=$ABI -O1 -S -o - sf.c | sed -n '/both:/,/ret/p'
done
```

### 实验 3：varargs 的 save area

```bash
cd /tmp && cat > va.c <<'EOF'
#include <stdarg.h>
double sum(int n, ...) {
  va_list ap; va_start(ap, n);
  double s = 0;
  for (int i = 0; i < n; i++) s += va_arg(ap, double);
  va_end(ap);
  return s;
}
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 -S -emit-llvm -o va.ll va.c
$T/bin/llc -mtriple=riscv64 -mattr=+m,+a,+f,+d,+c -stop-after=prolog-epilog -simplify-mir va.ll -o - \
  | grep -E "varArgsSaveSize|varArgsFrameIndex|stackSize"
```

### 实验 4：偶数寄存器规则

```bash
cd /tmp && cat > va3.c <<'EOF'
extern void vcallee(int n, ...);
void vcaller(long a, long b) { vcallee(1, a, b, (__int128)1); }
EOF
$T/bin/clang --target=riscv64-linux-gnu -march=rv64gc -mabi=lp64d -O1 -S -o - va3.c | sed -n '/vcaller:/,/tail/p'
```

应该看到 `li a4, 1` / `li a5, 0`，`a3` 被跳过。

### 实验 5：声明不一致的代价

把 9.8 节的两个版本各编译一次，比较 `csrr vlenb` 之后 `li a1, N` 里的 N（18 vs 3）。需要 `riscv_vector.h` 与一个可用的 `stdint.h`；若环境缺少 C 库头文件，可加 `-nostdinc -isystem <clang-资源目录>/include -isystem <自备的 stdint 目录>`。

---

## 9.13 踩雷集锦

1. **`CC_RISCV` 改了，`RetCC_RISCV` 没改。** 参数布局对了，返回值错位；症状是"函数能进能出，返回值是垃圾"。
2. **`LowerCall` 的返回值位置用了 `CC_RISCV`。** 必须是 `RetCC_RISCV`（`RISCVISelLowering.cpp:29484`），否则调用点从错误的寄存器取结果。
3. **忘了 `!ArgFlags.isVarArg()`。** 把 varargs 的 `double` 放进 `fa0`，被调用者按整数寄存器取，拿到的是位模式的一半。这个 bug 只在特定调用组合下出现。
4. **改 vararg 偶数规则时顺手"修"了 ILP32E。** 代码里显式排除 ILP32E 是为了兼容 GCC；改它必须先对 GCC 与 psABI 做调研，并在 PR 里给出两边行为对比。
5. **`sret` 当普通指针参数处理。** `sret` 必须占 `a0`（`LowerFormalArguments` 里靠 `ArgFlags.isSRet()` 前置处理），否则所有后续参数整体错位。
6. **`byval` 直接传原指针。** 语义要求被调用者拥有副本；不复制会在被调用者改写参数时污染调用者的对象。
7. **`riscv_vector_cc` 只写在使用处、没写进声明。** 链接能过，运行时栈被写坏；表现为"向量函数返回后数据错了"。
8. **`musttail` 不满足资格时静默降级。** 实际会 `reportFatalInternalError`；如果你在改尾调用逻辑，先跑 `llvm/test/CodeGen/RISCV/musttail-call.ll` 与 `musttail-indirect-args.ll`。
9. **以为 RV32 的 `i64` 必须偶数对齐。** 只有 varargs 且 16 字节对齐的参数才需要；普通参数按顺序占两个槽。

---

## 9.14 本章重点整理

- 参数分配逻辑在 `RISCVCallingConv.cpp`，不在 `.td`；`.td` 只给保存名单。
- 三条路径必须一致：`LowerFormalArguments`（取）、`LowerCall`（放 + 调用点取返回）、`LowerReturn`（放返回）。
- 整数：`a0`–`a7`，第 9 个进 outgoing argument area；RV32 的 `i64`/RV64 的 `i128` 占两个槽。
- 浮点：`fa0`–`fa7`，但 **varargs 一律走整数寄存器**；软浮点 ABI 用 `fmv.d.x`/`fmv.x.d` 搬运。
- 聚合：≤2×XLEN 拆寄存器（支持 fp+fp、int+fp）；>2×XLEN 用 byval 指针；返回值 >2×XLEN 用 `sret`（`a0`）。
- varargs：`varArgsSaveSize` = 参数寄存器字节数；16 字节对齐的参数跳过奇数号寄存器（ILP32E 除外）。
- 向量：`allocateRVVReg` 按 LMUL 找对齐组；`riscv_vector_cc` 必须声明与定义一致，否则全部 spill。
- `fastcc` 多 4 个参数寄存器（`x28`–`x31`），不用 `x5`/`x6`/`x7`。

---

## 9.15 自我检核

- [ ] 能说出 `LowerFormalArguments`/`LowerCall`/`LowerReturn` 各自负责什么，以及为什么 `LowerCall` 里还要算返回值位置
- [ ] 给定 `void f(long, double, struct{long,long,long})`，能写出每个参数落在哪里
- [ ] 能解释 `AllowFPRForF64 = !isVarArg()` 的 ABI 理由
- [ ] 能解释 `lp64` 下 `double` 参数为什么先 `fmv.d.x`
- [ ] 能说出 `sret`/`byval` 的区别，以及各自在汇编里的形状
- [ ] 能解释 varargs 的偶数寄存器规则，并知道 ILP32E 的例外
- [ ] 能解释为什么声明不匹配 `riscv_vector_cc` 会导致大量 `vs1r.v`
- [ ] 能列出至少 3 个尾调用不合格的原因

---

## 9.16 延伸阅读

- **`llvm/lib/Target/RISCV/RISCVCallingConv.cpp`**（本树，877 行）
  - 读哪里：57–95 的 psABI 摘要、392–686 的 `CC_RISCV_Impl`。
  - 为什么读：所有"参数落在哪"的答案都在这里，且注释质量高于文档。
- **`llvm/lib/Target/RISCV/RISCVISelLowering.cpp`**
  - 读哪里：28701–28920、29013–29528、29538–29640 三段。
  - 为什么读：这是 lowering 的"手"，`CC_RISCV` 只是"规则"。
- **`llvm/docs/RISCVUsage.md`**（本树，808 行）
  - 读哪里：ABI 与 `-mabi` 的可选值、`interrupt` 属性。
  - 为什么读：用户可见的承诺与实现要对得上。
- **`clang/include/clang/Basic/AttrDocs.td`**
  - 读哪里：6946 行起 `riscv_vector_cc`。
  - 为什么读：属性语义是"少 spill"的前提，写错就是 ABI bug。
- **`llvm/test/CodeGen/RISCV/vararg.ll`** 与 `calling-conv-ilp32e.ll`
  - 读哪里：CHECK 行里的寄存器与偏移。
  - 为什么读：改 lowering 时，这两个文件是最先该跑的反向测试。

---

→ 下一章：[Ch 10 — 栈帧与 prologue/epilogue](./10-frame-lowering-and-stack.md)
