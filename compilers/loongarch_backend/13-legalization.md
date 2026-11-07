# Ch 13 — 合法化：哪些类型/操作在 LoongArch 上“不存在”

> 本章目标：把 `LoongArchTargetLowering` 构造函数的 `setOperationAction` 读成一张“裁判表”；能解释 `i128` 为什么在 LA64 上加法是内联的、`half` 为什么变成三次 libcall、`i8` 加法为什么不需要截断。

---

## 13.1 合法化的两个层次

SelectionDAG 在选指前做两轮：

1. **类型合法化**（Type Legalization）：把“目标不认识的类型”拆/提升/软化成目标认识的。
2. **操作合法化**（Operation Legalization）：把“目标不认识的指令”换成目标支持的序列或 libcall。

LoongArch 认识的类型（`LoongArchISelLowering.cpp:86`）：

```cpp
addRegisterClass(GRLenVT, &GPRRegClass);          // i32（LA32）或 i64（LA64）
if (Subtarget.hasBasicF()) addRegisterClass(MVT::f32, &FPR32RegClass);
if (Subtarget.hasBasicD()) addRegisterClass(MVT::f64, &FPR64RegClass);
if (Subtarget.hasExtLSX())  ... LSX128RegClass;   // v16i8 ... v2f64
if (Subtarget.hasExtLASX()) ... LASX256RegClass;  // v32i8 ... v4f64
```

也就是说：

- **没有 `i8`/`i16` 寄存器类** → 提升到 `i32`；
- **没有 `i128`** → 拆成两个 GRLen；
- **没有 `f16`** → 提升到 `f32`（再靠 libcall 转换）；
- **没有 `f128`** → 软化成 libcall；
- `f32`/`f64` 只有在 `+f`/`+d` 时才注册；`-mattr=-d` 时 `f64` 直接不合法。

---

## 13.2 实测：五类“不合法”的落地方式

```llvm
define i128 @add128(i128 %a, i128 %b) { %r = add i128 %a, %b
  ret i128 %r }
define i64 @mul64(i64 %a, i64 %b) { %r = mul i64 %a, %b
  ret i64 %r }
define half @addh(half %a, half %b) { %r = fadd half %a, %b
  ret half %r }
define i8 @add8(i8 %a, i8 %b) { %r = add i8 %a, %b
  ret i8 %r }
```

### LA64

```text
add128:
	add.d	$a1, $a1, $a3
	add.d	$a2, $a0, $a2
	add.d	$a1, $a1, $a0
	move	$a0, $a2
	ret
```

`i128` 的加法被拆成“低 64 位 + 高 64 位 + 进位”。**没有 libcall**——因为 `ISD::ADD` 对 `i128` 会先被拆成 `ADD_PARTS`，而 LoongArch 的 `ADD_PARTS` 是 Legal（默认），直接用 `add.d` + 进位链。

注意寄存器对：`i128` 参数在 `$a0:$a1` 和 `$a2:$a3`（低:高），返回值在 `$a0:$a1`。

### LA32 的 `i64` 乘法

```text
mul64:
	mul.w	$a3, $a0, $a3
	mulh.wu	$a4, $a0, $a2
	add.w	$a3, $a4, $a3
	mul.w	$a1, $a1, $a2
	add.w	$a1, $a3, $a1
	mul.w	$a0, $a0, $a2
	ret
```

LA32 没有 `mul.d`，`i64` 乘法被展开成 4 次 `mul.w` + `mulh.wu` + 两次 `add.w` 的经典 64×64 低 64 位算法。这是 `ISD::MUL` 对 `i64` 的 Expand 结果，不是 libcall。

### `half`

LA64：

```text
	addi.d	$sp, $sp, -32
	st.d	$ra, $sp, 24
	pcaddu18i	$ra, %call36(__extendhfsf2)
	jirl	$ra, $ra, 0
	pcaddu18i	$ra, %call36(__extendhfsf2)
	jirl	$ra, $ra, 0
	fadd.s	$fa0, $fa0, $fs1
	pcaddu18i	$ra, %call36(__truncsfhf2)
	jirl	$ra, $ra, 0
```

`f16` 没有硬件，`fadd half` 被软化成：

```text
__extendhfsf2(a) -> f32
__extendhfsf2(b) -> f32
fadd.s
__truncsfhf2    -> f16
```

LA32 更惨：连 `fadd.s` 都没有（无 `+f` 时），变成 `__addsf3` 的软浮点调用。

### `i8`

```text
add8:
	add.d	$a0, $a0, $a1
	addi.w	$a1, $a1, 0
	addi.w	$a0, $a0, 0
	ret
```

`i8` 参数按 ABI 已经符号扩展到 32 位，所以直接 `add.d`；两条 `addi.w ...,0` 是把 32 位结果**符号扩展到 64 位**（`addi.w` 的语义），并**不是 i8 截断**。i8 的溢出位留在高位，ABI 只要求低 8 位正确——调用方读 i8 时只取低字节。

---

## 13.3 操作裁判表

构造函数的 `setOperationAction` 可以按“为什么”分组：

### 因为“没有这条指令”而 Expand

| 操作 | 动作 | 原因 |
|---|---|---|
| `ROTL` | Expand | 没有 `rotl`，用 `slli/srli/or` |
| `CTPOP` | Expand | 没有 popcount（除非 `+lbt`？默认没有） |
| `BSWAP` | Expand（无 32S 时） | `revb.d` 需要 32S |
| `CTTZ`/`CTLZ` | Expand（仅 LA32R） | LA32R 没有 `ctz`/`clz` |
| `SMUL_LOHI`/`UMUL_LOHI` | Expand | 用 `mulh` 系列拆 |
| `BR_CC`/`SELECT_CC` | Expand | 没有条件寄存器，靠 `BRCOND`+`SELECT` 组合 |
| `UINT_TO_FP` | Expand | 需要额外序列处理无符号 |

### 因为“需要自定义序列”而 Custom

| 操作 | 原因 |
|---|---|
| `SHL_PARTS`/`SRA_PARTS`/`SRL_PARTS` | 128 位移位要跨寄存器拼 |
| `FP_TO_SINT`/`FP_TO_UINT` | 需要 `ftintrz` + 边界处理 |
| `GlobalAddress`/`GlobalTLSAddress`/`JumpTable`… | 代码模型、TLS 模型（Ch 11） |
| `DYNAMIC_STACKALLOC` | 动态栈（Ch 10） |
| `BRCOND` | 需要处理比较结果 |
| `PREFETCH` | `preld` 的提示编码 |
| `VASTART` | varargs 保存区（Ch 09） |
| `EH_DWARF_CFA` | 需要读 `$sp`/`$fp` |

### 因为“LA32 没有 64 位指令”而 Custom

`if (!IsLA64)` 分支里给 `i32` 设了一堆 Custom：

```cpp
setOperationAction(ISD::ADD, MVT::i32, Custom);
setOperationAction(ISD::SUB, MVT::i32, Custom);
setOperationAction(ISD::SHL, MVT::i32, Custom);
...
setOperationAction({ISD::SDIV, ISD::UDIV, ISD::SREM, ISD::UREM}, MVT::i32, Custom);
```

LA32 的 `i32` 是“原生宽度”，但 `add.w` 会做 32 位截断；在 64 位寄存器里保持符号扩展需要额外处理，所以走 Custom。

### 因为“LA32R 更弱”而 Expand

```cpp
if (STI.has32S()) {
  setOperationAction(ISD::BITREVERSE, GRLenVT, Legal);
  ...
} else {
  setOperationAction(ISD::BSWAP, GRLenVT, Expand);
  setOperationAction(ISD::CTTZ, GRLenVT, Expand);
  setOperationAction(ISD::CTLZ, GRLenVT, Expand);
  setOperationAction(ISD::ROTR, GRLenVT, Expand);
  ...
}
```

**LA32R（reduced）没有 32S 的那些指令**，所以同一份 IR 在 `loongarch32r` 上会生成更长的序列。

---

## 13.4 怎么自己查“某操作合不合法”

三种办法，从粗到细：

1. **看 `-debug-only=legalize-types,legalize-dag`**（需要 assertions 构建）：

   ```bash
   llc -mtriple=loongarch64-unknown-linux-gnu -O2 \
     -debug-only=legalize-types,legalize-dag fn.ll -o /dev/null 2>&1 | head -50
   ```

2. **看 `-print-before=legalize-types` / `-print-after=legalize-dag`** 的 DAG：

   ```bash
   llc ... -print-after=legalize-dag fn.ll -o /dev/null 2>&1 | grep -A5 'add'
   ```

3. **直接看生成物**：如果出现 `__addti3`/`__extendhfsf2`，说明走了 libcall；如果是几条 `add.d`，说明 Expand 成了序列。

**注意**：`-debug-only` 需要 `LLVM_ENABLE_ASSERTIONS=ON`；本树构建默认开（Ch 00 的配置）。没有 assertions 时用 `-print-after`。

---

## 13.5 反直觉点

1. **`i128` 加法在 LA64 上是内联的**，不是 `__addti3`。只有目标完全没有对应序列时才走 libcall。
2. **`half` 的“加法”是三次 libcall**（两次 extend + 一次 trunc），因为 `f16` 不是合法类型。
3. **`i8` 加法不截断**；ABI 只保证低 8 位，高位是垃圾。
4. **`ROTL` Expand 但 `ROTR` 可能 Legal**：LoongArch 有 `rotri.d`（立即数右旋），没有对应左旋立即数，所以左旋要拼。
5. **`CTTZ`/`CTLZ` 只在 LA32R 上 Expand**；LA64/LA32S 有 `ctz`/`clz`，是 Legal。
6. **`f64` 的合法性取决于 `+d`**，不是取决于 triple；`-mattr=-d` 时 `f64` 会走软浮点。
7. **`setOperationAction(..., Custom)` 不等于“有自定义指令”**，很多只是 C++ 里拼序列（Ch 15）。

---

## 13.6 本章证据

| 结论 | 源码/命令 |
|---|---|
| 寄存器类注册 | `LoongArchISelLowering.cpp:86-103` |
| 操作动作表 | `:107-230` |
| LA32R 分支 | `:139-158` |
| LA32 Custom 列表 | `:173-195` |
| i128 实测 | 本章 13.2 |
| half 实测 | 本章 13.2 |
| i8 实测 | 本章 13.2 |
| LA32 i64 mul 实测 | 本章 13.2 |

---

## 13.7 练习

1. 用 `-mattr=-d` 编译一个 `double` 函数，确认它变成软浮点调用，并列出用到的 `__*df*` 符号。
2. 用 `-print-after=legalize-dag` 找 `ROTL` 被展开的证据。
3. 在 `loongarch32r` triple 上编译 `@llvm.cttz.i32`，对比 `loongarch32` 的输出。
4. 解释 `SMUL_LOHI` 为什么是 Expand 而不是 Legal：`mulh` 不够吗？
5. 找一个 `Custom` 操作，读它的 lowering 代码，确认它没有对应单条指令。

---

## 13.8 延伸阅读

1. **SelectionDAG Legalize 的官方流程** — `llvm/docs/WritingAnLLVMBackend.md`
   - 读什么：`### The SelectionDAG Legalize Phase`（1358 行起），包括 `addRegisterClass` 和 `setOperationAction` 等回调列表。
   - 学到什么：本章 13.1 把合法化分成“类型/操作”两层；这一节给出 `setLoadExtAction`/`setTruncStoreAction`/`setIndexedLoadAction` 等本章没提的回调，说明 `setOperationAction` 不是唯一入口。
   - 前提：读过本章 13.1 和 13.3。

2. **LoongArch 的裁判表源码** — `llvm/lib/Target/LoongArch/LoongArchISelLowering.cpp`
   - 读什么：构造函数里的 `setOperationAction` 段（约 107–230 行），特别是 `if (STI.has32S())` 分支（140 行起）、`if (Subtarget.is64Bit())` 段（172 行起）和 `if (!Subtarget.is64Bit())` 段（201 行起）。
   - 学到什么：本章 13.3 按“为什么”分组；源码里 172–200 行是 LA64 的 `i32` Custom 列表，201 行起才是 LA32 专属动作。直接读能确认每条动作的 feature 条件，避免只背表格。
   - 前提：读过本章 13.3 的分组表。

3. **`half` 软化的回归测试** — `llvm/test/CodeGen/LoongArch/fp16-promote.ll`
   - 读什么：`test_load_store`、`test_fadd_reg` 等函数的 `LA32`/`LA64` `CHECK`，重点看 `ld.h`/`st.h` 与 `__extendhfsf2`/`__truncsfhf2` 的分界。
   - 学到什么：本章 13.2 用 `fadd half` 展示三次 libcall；这份测试补上“什么时候 `half` 只做 load/store 提升、不调用 `__extendhfsf2`”，帮助区分类型提升与操作软化。
   - 前提：读过本章 13.2 的 `half` 段。

4. **GlobalISel 的另一套合法化词汇** — `llvm/docs/GlobalISel/Legalizer.md`
   - 读什么：`### Rule Actions`（146 行起）里的 `legalIf`/`narrowScalarIf`/`widenScalarIf`/`lowerIf`/`libcallIf`/`customIf`/`unsupportedIf`，以及 `## Minimum Rule Set`。
   - 学到什么：本章讲的是 SelectionDAG 的 `Legal`/`Expand`/`Custom`；GlobalISel 用不同的动作名和规则粒度表达同一件事，读这一节可以避免把两套术语混用。
   - 前提：知道本章 13.1 的两层合法化。

## 13.9 下一步

下一章看选指：`LoongArchISelDAGToDAG::Select` 和 `.td` 里的 `Pat<>` 如何把合法化后的 DAG 映射到具体 opcode，以及 `ComplexPattern` 在什么时候必须手写 C++。
