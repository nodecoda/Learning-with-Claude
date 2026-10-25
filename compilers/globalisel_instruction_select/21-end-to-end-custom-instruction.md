# Ch 21 — 端到端案例：从 spec 到 lit（guided walkthrough）

> **本章目标**：把“定义一个 codegen-only 的 AArch64 pseudo，让 GlobalISel 选到它，再由 `PseudoInstExpansion` 展开成真实指令”这条链路完整走一遍。重点不是发明新硬件语义，而是学会把 spec、TableGen、pattern、matcher、MIR 测试和最终汇编串起来。
>
> **前置**：第 5–8、11、12、16、20 章。**环境**：LLVM 23.1.4，AArch64 主线，RISC-V 对照。**案例**：`ADDXri_Custom`，复用现有 `ADDXri` 的编码与展开。

---

## 21.1 先明确：这一章哪些是实测，哪些是 guided

两类证据必须分开看，否则容易把“设计预期”当成“已经编译通过”。

| 标记 | 含义 | 本章中的例子 |
| --- | --- | --- |
| **【实测】** | 在 LLVM 23.1.4 预编译包上实际运行 `llc` | 现有 `ADDXri` / `ADDXrr` / `ADDWri`、汇编、编码、fallback |
| **【guided】** | 修改源码并重新构建；给出 diff/命令/预期，但**未在本环境编译** | `ADDXri_Custom`、生成的 matcher、custom MIR/asm |

本环境只有预编译 `llc`，没有可增量构建的源码树：不声称运行过 `ninja`，不伪造构建日志，不把 guided 输出说成本机实测。所有现有指令的 MIR/asm 都可复现。

> 建议顺序：先跑 21.4 基线，再看 21.6 diff，最后用 21.8 的命令验证。

---

## 21.2 为什么选 `ADDXri_Custom`

一个适合教学的 custom instruction 应该满足四个条件：

1. **语义简单**：能用一个已有 pattern 描述，不需要新写 C++ selector；
2. **编码可控**：不引入新的 encoder/decoder，避免把章节变成 ISA 实现课；
3. **选择可见**：能在 `-stop-after=instruction-select` 的 MIR 里看到 custom opcode；
4. **展开可验证**：最终汇编和机器码能与原指令逐字节对照。

`ADDXri_Custom` 正好满足：

- 语义等同于 64 位 `add` 加一个 12 位算术立即数（可带 `lsl #12`）；
- 复用 `ADDXri` 的编码和 MC 层；
- 作为 pseudo，它没有自己的编码，最终必须展开；
- 选中与展开发生在两个不同阶段，正好能讲清 GlobalISel 与 AsmPrinter 的边界。

### 21.2.1 spec（先写清楚，再写 TableGen）

| 项目 | 设计 |
| --- | --- |
| 名字 | `ADDXri_Custom` |
| 语义 | `dst = lhs + imm`，`lhs/dst` 为 64 位通用寄存器，`imm` 为 `addsub_shifted_imm64` |
| 是否真实指令 | 否，codegen-only pseudo |
| 是否有编码 | 否；最终展开为 `ADDXri` |
| 选择条件 | `G_ADD` + `GPR64sp` + 可表示的 12 位立即数 |
| 展开方式 | `PseudoInstExpansion<(ADDXri ...)>` |
| 汇编器可见 | 否 |
| 测试 | MIR/lit 看选中；asm/objdump 看展开 |

这里的“custom”不是指新硬件指令，而是“在 codegen 里新增一个可被选择的 pseudo”。这是后端实验最安全的起点：即使 pattern 写错，也不会产生错误的机器码，只会选不中或展开失败。

### 21.2.2 不做什么
不新增编码、不改 assembler/parser、不做多指令展开、不暴露给 Clang builtin；这些都会把案例升级成另一量级的 ISA 实现任务。

## 21.3 端到端链路总览

```text
IR -> IRTranslator: G_ADD -> Legalizer -> RegBankSelect: GPR
   -> InstructionSelect: selectImpl(pattern) -> ADDXri_Custom
   -> AsmPrinter: PseudoInstExpansion -> ADDXri -> MCInst
```

| 阶段 | 看什么 | 命令 |
| --- | --- | --- |
| Legalizer | `G_ADD` 合法 | `-stop-after=legalizer` |
| RegBankSelect | `gpr64sp` / `gpr64` | `-stop-after=regbankselect` |
| InstructionSelect | `ADDXri_Custom` | `-stop-after=instruction-select` |
| AsmPrinter / MC | `add ...` 与编码 | 直接输出 `.s`；`-show-mc-encoding` |

最后两行要分开：`-stop-after=instruction-select` 看不到 `PseudoInstExpansion`，pseudo 展开在 AsmPrinter 的 MCInst lowering 路径。

## 21.4 基线：先看没有 custom pseudo 时是什么样

### 21.4.1 测试 IR
保存为 `add-baseline.ll`：

```llvm
define i64 @add_i64_42(i64 %a) {
  %r = add i64 %a, 42
  ret i64 %r
}
define i64 @add_i64_4095(i64 %a) {
  %r = add i64 %a, 4095
  ret i64 %r
}
define i64 @add_i64_4096(i64 %a) {
  %r = add i64 %a, 4096
  ret i64 %r
}
define i64 @add_i64_5000(i64 %a) {
  %r = add i64 %a, 5000
  ret i64 %r
}
define i64 @add_i64_reg(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}
define i32 @add_i32_42(i32 %a) {
  %r = add i32 %a, 42
  ret i32 %r
}
```

### 21.4.2 只看 instruction-select

```bash
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=instruction-select add-baseline.ll -o add-baseline-sel.mir
```

**【实测】** 关键输出（省略了函数头、liveins 和部分 COPY）：

```text
add_i64_42:
  %2:gpr64sp = ADDXri %0, 42, 0
  $x0 = COPY %2
  RET_ReallyLR implicit $x0

add_i64_4095:
  %2:gpr64sp = ADDXri %0, 4095, 0

add_i64_4096:
  %2:gpr64sp = ADDXri %0, 1, 12

add_i64_5000:
  %3:gpr32 = MOVi32imm 5000
  %1:gpr64 = SUBREG_TO_REG %3, %subreg.sub_32
  %2:gpr64 = ADDXrr %0, %1

add_i64_reg:
  %2:gpr64 = ADDXrr %0, %1

add_i32_42:
  %2:gpr32sp = ADDWri %0, 42, 0
```

每个函数都有：

```yaml
selected:        true
failedISel:      false
```

这张表已经包含本章最重要的三个边界：

- `4095` 仍能放进 12 位无符号字段；
- `4096 = 1 << 12` 走“低 12 位 + `lsl #12`”；
- `5000` 不能由单条 `ADDXri` 表示，选择器改为先物化常量，再用 `ADDXrr`。

### 21.4.3 最终汇编
```bash
llc -mtriple=aarch64-linux-gnu -O0 -global-isel add-baseline.ll -o -
```

**【实测】** 过滤掉标签和伪指令后：

```asm
add     x0, x0, #42
add     x0, x0, #4095
add     x0, x0, #1, lsl #12        // =4096
mov     w8, #5000                  // =0x1388
add     x0, x0, x8
add     x0, x0, x1
add     w0, w0, #42
```

`mov w8, #5000` 的寄存器号由寄存器分配决定，不保证永远是 `w8`；本章的 lit 测试会用正则匹配寄存器号，而不是硬编码。

### 21.4.4 编码基线

【实测】`-show-mc-encoding` 给出：`#42` 为 `[0x00,0xa8,0x00,0x91]`，`#4095` 为 `[0x00,0xfc,0x3f,0x91]`，`#1, lsl #12` 为 `[0x00,0x04,0x40,0x91]`。custom pseudo 的最终编码必须逐字节相同；若不同，说明改到了编码而不是只加 pseudo。

## 21.5 源码链：pattern 是怎么被导入的

在写 diff 之前，先把 23.1.4 的实际结构对上。否则很容易把 `GINodeEquiv`、`ComplexPattern` 和 `PseudoInstExpansion` 混成一层。

### 21.5.1 `AArch64InstrGISel.td` 里有什么
这个文件主要定义 target generic opcode，而不是所有 GI pattern：

```tablegen
class AArch64GenericInstruction : GenericInstruction {
  let Namespace = "AArch64";
}

def G_ADD_LOW : AArch64GenericInstruction {
  let OutOperandList = (outs type0:$dst);
  let InOperandList = (ins type1:$src, type2:$imm);
  let hasSideEffects = 0;
}
```

文件后半段是 `G_REV32`、`G_UZP1`、`G_DUP` 等的 `GINodeEquiv`。但 `G_ADD` 的映射在通用 `SelectionDAGCompat.td`：

```tablegen
def : GINodeEquiv<G_ADD, add>;
def : GINodeEquiv<G_SUB, sub>;
```

因此 `ADDXri` 上的 `(add ...)` pattern 可被 importer 直接理解为 `G_ADD`，不需要为普通 add 再写映射。

### 21.5.2 `ADDXri` 的定义链

`AArch64InstrInfo.td` 里：

```tablegen
defm ADD : AddSub<0, "add", "sub", add>;
```

`AddSub` multiclass 生成 `Xri`：

```tablegen
let AddedComplexity = 6 in
def Xri : AddSubImmShift<isSub, 0, GPR64sp, GPR64sp,
                         addsub_shifted_imm64, mnemonic, OpNode> {
  let Inst{31} = 1;
}
```

`AddSubImmShift` 的 pattern 是：

```tablegen
(set dstRegtype:$Rd,
     (OpNode srcRegtype:$Rn, immtype:$imm))
```

当 `OpNode = add`、`srcRegtype = GPR64sp`、`immtype = addsub_shifted_imm64` 时，就是 `G_ADD` + 64 位栈指针寄存器类 + 12 位算术立即数。`addsub_shifted_imm64` 是一个 `ComplexPattern`，GI 侧通过 `gi_addsub_shifted_imm64` 映射到 `selectArithImmed`：

```tablegen
def gi_addsub_shifted_imm64 :
    GIComplexOperandMatcher<s64, "selectArithImmed">,
    GIComplexPatternEquiv<addsub_shifted_imm64>;
```

`selectArithImmed` 的 23.1.4 规则：

```cpp
if (Immed >> 12 == 0) {
  ShiftAmt = 0;
} else if ((Immed & 0xfff) == 0 && Immed >> 24 == 0) {
  ShiftAmt = 12;
  Immed = Immed >> 12;
} else
  return std::nullopt;
```

所以 `42`/`4095` 是 `shift=0`，`4096` 是 `imm=1, shift=12`，`5000` 返回 `nullopt`、pattern 不命中。

### 21.5.3 `PseudoInstExpansion` 在这一层的哪里

`AArch64InstrGISel.td` 里没有 `PseudoInstExpansion` 实例；实际例子在 `AArch64InstrInfo.td`：

```tablegen
def MRS_FPCR : Pseudo<(outs GPR64:$dst), (ins),
                      [(set GPR64:$dst, (int_aarch64_get_fpcr))]>,
               PseudoInstExpansion<(MRS GPR64:$dst, 0xda20)>,
               Sched<[WriteSys]>;

def BLRNoIP : Pseudo<(outs), (ins GPR64noip:$Rn), []>,
              Sched<[WriteBrReg]>,
              PseudoInstExpansion<(BLR GPR64:$Rn)>;
```

`PseudoInstExpansion` 在 `Target.td` 里只记录展开 dag，并设置 `isPseudo = true`：

```tablegen
class PseudoInstExpansion<dag Result> {
  dag ResultInst = Result;
  bit isPseudo = true;
}
```

AArch64 的 CMake 用 `tablegen(LLVM AArch64GenMCPseudoLowering.inc -gen-pseudo-lowering)` 生成展开表；`AArch64AsmPrinter::emitInstruction()` 先调用：

```cpp
if (MCInst OutInst; lowerPseudoInstExpansion(MI, OutInst)) {
  EmitToStreamer(*OutStreamer, OutInst);
  return;
}
```

所以展开发生在 AsmPrinter，不在 instruction-select。`PseudoLoweringEmitter.cpp` 还限制：展开结果不能是 pseudo/codegen-only，且当前只支持一条真实指令；多指令要用 `expandPostRAPseudo`（AArch64 是 `AArch64ExpandPseudoInsts.cpp`）。

### 21.5.4 三个“伪”概念不要混

| 概念 | 有无编码 | 谁能看见 | 典型用途 |
| --- | --- | --- | --- |
| `Pseudo` + `PseudoInstExpansion` | 无 | codegen 内部；AsmPrinter 前展开 | `MRS_FPCR`、`BLRNoIP`、本章的 `ADDXri_Custom` |
| `isCodeGenOnly` 真实指令 | 有 | codegen 可选；assembler matcher 忽略 | 复用编码的 codegen 专用变体 |
| target generic opcode | 无 | Legalizer/Selector 的 generic 世界 | `G_ADD_LOW`、`G_REV32` |

`PseudoInstExpansion` 的限制要看 `PseudoLoweringEmitter.cpp`：

- 展开结果不能是 pseudo 或 codegen-only；
- 当前只支持展开成**一条**真实指令；
- 需要多条指令时，用 target 的 `expandPostRAPseudo`（AArch64 是 `AArch64ExpandPseudoInsts.cpp`）。

这三点决定了本章的设计：`ADDXri_Custom` 必须展开成真实的 `ADDXri`，而且只能展开成一条。

---

## 21.6 guided diff：加入 `ADDXri_Custom`

下面的 diff 是**实验性**的，只改一个 TableGen 文件。它没有在本环境编译过；请把它当作可复现的 guided patch。

### 21.6.1 指令定义

文件：`llvm/lib/Target/AArch64/AArch64InstrInfo.td`，位置放在 `defm ADD : AddSub<...>;` 之后、其他 `ADD` 变体之前或之后都可以，只要 `ADDXri` 已经由 multiclass 生成。

```diff
--- a/llvm/lib/Target/AArch64/AArch64InstrInfo.td
+++ b/llvm/lib/Target/AArch64/AArch64InstrInfo.td
@@ -2767,6 +2767,17 @@ defm ADD : AddSub<0, "add", "sub", add>;
 defm SUB : AddSub<1, "sub", "add">;
 
+// Guided lab: a codegen-only pseudo that reuses ADDXri's encoding.
+// It exists only to make GlobalISel selection observable in MIR.
+let AddedComplexity = 7 in
+def ADDXri_Custom : Pseudo<(outs GPR64sp:$Rd),
+                           (ins GPR64sp:$Rn, addsub_shifted_imm64:$imm),
+                           [(set GPR64sp:$Rd,
+                                 (add GPR64sp:$Rn,
+                                      addsub_shifted_imm64:$imm))]>,
+                    PseudoInstExpansion<(ADDXri GPR64sp:$Rd,
+                                                GPR64sp:$Rn,
+                                                addsub_shifted_imm64:$imm)>,
+                    Sched<[WriteI, ReadI]>;
+
 def : InstAlias<"mov $dst, $src",
                 (ADDWri GPR32sponly:$dst, GPR32sp:$src,
```

### 21.6.2 每一行在做什么

| 片段 | 作用 |
| --- | --- |
| `Pseudo<...>` | 声明为 pseudo，不提供编码 |
| `(outs GPR64sp:$Rd)` | 输出约束到 64 位栈指针寄存器类 |
| `(ins GPR64sp:$Rn, addsub_shifted_imm64:$imm)` | 输入是 64 位寄存器 + 复杂立即数 |
| `[(set ... (add ...))]` | 复用 `G_ADD` 的 SelectionDAG pattern；`add` 已有 `GINodeEquiv<G_ADD, add>` |
| `PseudoInstExpansion<(ADDXri ...)>` | 告诉 `-gen-pseudo-lowering` 展开成什么 |
| `AddedComplexity = 7` | 试图压过 `ADDXri` 的 `AddedComplexity = 6` |
| `Sched<[WriteI, ReadI]>` | 给调度器一个合理的资源模型 |

### 21.6.3 为什么不是 `isCodeGenOnly`
`isCodeGenOnly` 的真实指令仍然有编码，只是 assembler matcher 不收录。本章要验证“pseudo 被展开”，所以必须让它在最终汇编前没有编码；`PseudoInstExpansion` 才是正确工具。

反过来说，如果你只是想要一个“有编码、但不让汇编器解析”的别名，`isCodeGenOnly` 更合适。两者的测试策略不同：

- `isCodeGenOnly`：`-show-mc-encoding` 应该直接有编码；
- `PseudoInstExpansion`：`-stop-after=instruction-select` 看到 pseudo，最终 asm 才看到真实指令。

### 21.6.4 `AddedComplexity` 的边界

`AddedComplexity = 7` 只是排序提示，不是语义保证。若复杂度相同、predicate 不同或 importer 改变顺序，仍可能选到普通 `ADDXri`。构建后先看：

```bash
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=instruction-select add-baseline.ll -o - | grep ADDXri
```

只看到 `ADDXri` 时，先查 pattern 是否导入、复杂度是否生效，不要改测试去迁就。

## 21.7 预期的 matcher 与展开路径

这一节是 guided 的“应该发生什么”，不是生成物原文。

### 21.7.1 概念化的 matcher

对 `add i64 %a, 42`，importer 应生成大致等价于下面的序列（伪代码，不是 `.inc` 原文）：

```text
check opcode == G_ADD; check type(op0) == s64
check GPR64sp; check complex addsub_shifted_imm64(op2)
  -> selectArithImmed: imm=42, shift=0
mutate to ADDXri_Custom; constrain operands
```

`G_ADD` 来自 `GINodeEquiv<G_ADD, add>`；custom 只改变选中后的 opcode，不改变立即数合法性。

### 21.7.2 预期 MIR

在 `-stop-after=instruction-select` 下，预期看到：

```text
add_i64_42:
  %2:gpr64sp = ADDXri_Custom %0, 42, 0

add_i64_4095:
  %2:gpr64sp = ADDXri_Custom %0, 4095, 0

add_i64_4096:
  %2:gpr64sp = ADDXri_Custom %0, 1, 12
```

这只是**设计预期**。如果实际构建后看到的是 `ADDXri`，说明 custom pattern 没有赢；如果看到 `ADDXri_Custom` 但后续崩溃，说明展开或操作数映射有问题。

### 21.7.3 预期展开

最终 asm 应该仍然是：

```asm
add     x0, x0, #42
add     x0, x0, #4095
add     x0, x0, #1, lsl #12
```

`-show-mc-encoding` 应该与基线逐字节一致：

```text
add x0, x0, #42          encoding: [0x00,0xa8,0x00,0x91]
add x0, x0, #4095        encoding: [0x00,0xfc,0x3f,0x91]
add x0, x0, #1, lsl #12  encoding: [0x00,0x04,0x40,0x91]
```

如果编码变了，说明你实际上改到了编码/操作数，而不是只加了一个 pseudo。

### 21.7.4 生成物在哪

重新构建后重点看两个文件：

```text
build/lib/Target/AArch64/AArch64GenGlobalISel.inc
build/lib/Target/AArch64/AArch64GenMCPseudoLowering.inc
```

- 前者应包含 `ADDXri_Custom` 的匹配/变更逻辑；
- 后者应包含 `case AArch64::ADDXri_Custom:` 和 `Inst.setOpcode(AArch64::ADDXri)` 之类的展开逻辑。

不要手改这些 `.inc`。它们是 TableGen 的输出，下一次构建会覆盖。

---

## 21.8 跑起来：lit RUN 与四层验证

### 21.8.1 lit RUN 行
把下面的检查放进 `llvm/test/CodeGen/AArch64/GlobalISel/custom-addxri.ll`：

```llvm
; RUN: llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
; RUN:     -stop-after=instruction-select %s -o - \
; RUN:   | FileCheck %s --check-prefix=GISEL
; RUN: llc -mtriple=aarch64-linux-gnu -O0 -global-isel %s -o - \
; RUN:   | FileCheck %s --check-prefix=ASM

define i64 @add42(i64 %a) {
; GISEL-LABEL: name:            add42
; GISEL: %2:gpr64sp = ADDXri_Custom %0, 42, 0
; ASM-LABEL: add42:
; ASM: add x0, x0, #42
  %r = add i64 %a, 42
  ret i64 %r
}

define i64 @add4096(i64 %a) {
; GISEL-LABEL: name:            add4096
; GISEL: %2:gpr64sp = ADDXri_Custom %0, 1, 12
; ASM: add x0, x0, #1, lsl #12
  %r = add i64 %a, 4096
  ret i64 %r
}

define i32 @add32(i32 %a) {
; GISEL-LABEL: name:            add32
; GISEL: %2:gpr32sp = ADDWri %0, 42, 0
; ASM: add w0, w0, #42
  %r = add i32 %a, 42
  ret i32 %r
}

define i64 @add5000(i64 %a) {
; GISEL-LABEL: name:            add5000
; GISEL: %3:gpr32 = MOVi32imm 5000
; GISEL: %1:gpr64 = SUBREG_TO_REG %3, %subreg.sub_32
; GISEL: %2:gpr64 = ADDXrr %0, %1
; ASM: mov {{w[0-9]+}}, #5000
; ASM: add x0, x0, {{x[0-9]+}}
  %r = add i64 %a, 5000
  ret i64 %r
}
```

`%2` / `%3` 编号可能因 COPY 或调试信息变化；第一次跑不通时，先看完整 MIR。`ASM` 的寄存器号已经用正则，避免绑定寄存器分配结果。

### 21.8.2 四层验证，不要只做一层

| 层 | 命令 | 成功标准 |
| --- | --- | --- |
| 选择 | `-stop-after=instruction-select` | MIR 出现 `ADDXri_Custom`，且 `failedISel: false` |
| 展开 | 直接输出 `.s` | 汇编是 `add ...`，不是 `ADDXri_Custom` |
| 编码 | `-show-mc-encoding` | 与基线 `ADDXri` 的字节完全一致 |
| 反汇编 | `llvm-objdump -d` | 反汇编仍是同一条 `add` |

示例：

```bash
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -show-mc-encoding add-baseline.ll -o custom.s
llvm-mc -triple=aarch64 -filetype=obj custom.s -o custom.o
llvm-objdump -d custom.o | grep -A2 '<add_i64_42>'
```

### 21.8.3 为什么 `-stop-after` 看不到展开

`-stop-after=instruction-select` 在 InstructionSelect 结束时停机；此时只有 `ADDXri_Custom`。`PseudoInstExpansion` 发生在 AsmPrinter 的 `lowerPseudoInstExpansion()`，最终才变成 `ADDXri` 和 MCInst。因此：MIR 里找不到 `ADDXri`、asm 里找不到 `ADDXri_Custom`，都是正常的。判断“没选中”看 MIR，判断“没展开”看 asm。

---

## 21.9 三个验证场景

### 21.9.1 场景 A：正常命中

输入 `42`、`4095`、`4096`。预期：

```text
%2:gpr64sp = ADDXri_Custom %0, 42, 0
%2:gpr64sp = ADDXri_Custom %0, 4095, 0
%2:gpr64sp = ADDXri_Custom %0, 1, 12
```

最终汇编与基线一致。这个场景证明：custom pseudo 确实被 matcher 选中，并且 `selectArithImmed` 的两种 shift 形态都能工作。

### 21.9.2 场景 B：类型与立即数边界
- `i32 @add32`：custom 定义是 `GPR64sp`，不会匹配 32 位；预期仍是 `ADDWri`。
- `5000`：不是 12 位可表示立即数；预期 `MOVi32imm` + `SUBREG_TO_REG` + `ADDXrr`。
- `add i64 %a, -1`：23.1.4 的 AArch64 会把它规范化为 `SUBSXri %0, 1, 0, implicit-def dead $nzcv`（【实测】），不要假设它一定走 custom add pattern。

这三条说明：**pattern 未命中不一定是失败**。选择器可以合法地改走另一条指令；只有 `failedISel: true` 才是 GlobalISel 放弃。

### 21.9.3 场景 C：真正的 fallback

用错误 bank 的手写 MIR（`bad-bank.mir`）把 `G_ADD` 放在 `fpr` 上：

```yaml
--- |
  define i64 @bad_bank(i64 %a, i64 %b) { %r = add i64 %a, %b
    ret i64 %r }
...
---
name:            bad_bank
isSSA:           true
legalized:       true
regBankSelected: true
registers:
  - { id: 0, class: fpr }
  - { id: 1, class: fpr }
  - { id: 2, class: fpr }
body: |
  bb.0.entry:
    liveins: $x0, $x1
    %0:fpr(s64) = COPY $x0
    %1:fpr(s64) = COPY $x1
    %2:fpr(s64) = G_ADD %0, %1
    $x0 = COPY %2(s64)
    RET_ReallyLR implicit $x0
```

```bash
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    bad-bank.mir -o bad-bank-out.mir
```

**【实测】** 输出中同时出现：

```yaml
selected:        true
failedISel:      true
```

`G_ADD` 仍在，说明选择失败但没有立刻终止。加 `-global-isel-abort=1`：

```text
LLVM ERROR: cannot select: %2:fpr(s64) = G_ADD %0:fpr, %1:fpr (in function: bad_bank)
```

结论：错误 bank 不会被 custom pattern “纠正”；`selected: true` 也不等于成功。

## 21.10 调试清单

| 现象 | 先查什么 | 常见原因 |
| --- | --- | --- |
| MIR 还是 `ADDXri` | custom pattern 是否导入 | `AddedComplexity` 没赢、pattern 写错、类型/银行不匹配 |
| MIR 是 custom，asm 报错 | `AArch64GenMCPseudoLowering.inc` | 展开目标仍是 pseudo/codegen-only；操作数名或数量不一致 |
| asm 是 custom 文本 | AsmPrinter 是否调用 lowering | 没有重新生成 `.inc`，或走了错误的输出路径 |
| `failedISel: true` | 加 `-global-isel-abort=1` | 银行/类型/操作数不合法，或 C++ `select()` 没有 case |
| 编码与基线不同 | 展开目标是否真是 `ADDXri` | 误改了编码、shift 或寄存器类 |

调试顺序建议：先 `-stop-after=instruction-select`，再 `-global-isel-abort=1`，最后才看最终汇编。反过来会被 AsmPrinter 的表象干扰。

---

## 21.11 踩雷清单

1. **把 `selected: true` 当成成功**：`failedISel: true` 时 MIR 会保留 generic 指令，后续可能静默 fallback。必须同时检查两个字段。
2. **在 `-stop-after=instruction-select` 里找展开结果**：`PseudoInstExpansion` 在 AsmPrinter，不在这个 pass。看不到 `ADDXri` 不是 bug。
3. **展开目标写成 pseudo 或 codegen-only**：`PseudoLoweringEmitter` 会直接报错；展开目标必须是真实、可编码的指令。
4. **以为 `AddedComplexity` 是硬保证**：它只是排序权重。相同 pattern 的胜负还受 importer 顺序和 predicate 影响。
5. **忽略负立即数的规范化**：`add -1` 可能先变成 `sub 1`，再去匹配 `SUBSXri`；不要用负数去证明 custom add pattern 一定命中。

---

## 21.12 进阶与练习

**进阶**

- 给 custom pseudo 加 subtarget predicate，只在某个 feature 下启用，观察 matcher 是否会生成额外的 predicate check。
- 把 custom pseudo 改成多指令展开，改用 `expandPostRAPseudo`，比较它与 `PseudoInstExpansion` 的测试差异。
- 用 `-debug-only=global-isel`（assertions build）观察 matcher 命中了哪条规则，以及为什么另一条规则没赢。
- 对同一 IR 比较 SelectionDAG 与 GlobalISel 的 MIR，确认 custom pseudo 只影响 GI 路径。

**练习**

1. 增加 `ADDWri_Custom`，复用 `ADDWri` 编码，验证 32 位路径。
2. 让 custom pseudo 只在 `+custom-add` feature 下可用，写出对应的 lit RUN。
3. 把 `5000` 改成 `4096 + 904` 的两条指令序列，确认选择器是否仍能保持正确性。
4. 给 `bad-bank.mir` 增加 `-verify-machineinstrs`，记录第一个 verifier 错误。
5. 用 `llvm-objdump -d` 对比 custom 与基线目标文件，确认 `.text` 字节完全一致。

---

## 21.13 重点整理
- **spec 先行**：先定义语义、类型、立即数、展开目标，再写 TableGen。
- **pattern 复用**：`add` 已由 `GINodeEquiv<G_ADD, add>` 映射到 `G_ADD`，不需要重复造节点。
- **两阶段**：InstructionSelect 选 pseudo，AsmPrinter 展开 pseudo；测试必须分层。
- **三字段**：`selected`、`failedISel`、最终汇编，缺一不可。
- **边界**：12 位立即数、`lsl #12`、32/64 位寄存器类、负立即数规范化，是这类 pattern 最常见的四个边界。

## 21.14 自我检核

1. 为什么 `ADDXri_Custom` 不需要新的 `GINodeEquiv`？
   - 因为它复用了 `add` pattern，而 `G_ADD` 到 `add` 的映射已经在 `SelectionDAGCompat.td`。
2. 为什么 `-stop-after=instruction-select` 看不到 `ADDXri`？
   - 因为 `ADDXri_Custom` 还没到 AsmPrinter 的 `lowerPseudoInstExpansion`。
3. `4096` 在 MIR 里为什么是 `ADDXri %0, 1, 12`？
   - `selectArithImmed` 把 `4096` 拆成低 12 位 `1` 和 `lsl #12`。
4. `5000` 为什么不是 fallback？
   - 它仍然被合法地选成 `MOVi32imm + ADDXrr`；fallback 的标志是 `failedISel: true`。
5. 如果 custom pseudo 被选中但汇编器报错，先看哪里？
   - 先看 `AArch64GenMCPseudoLowering.inc` 的展开目标和操作数映射。

## 21.15 延伸阅读
- LLVM GlobalISel — InstructionSelect：<https://llvm.org/docs/GlobalISel/InstructionSelect.html>
- LLVM GlobalISel — Pipeline：<https://llvm.org/docs/GlobalISel/Pipeline.html>
- `llvm/lib/Target/AArch64/AArch64InstrInfo.td` 中的 `AddSub`、`PseudoInstExpansion` 实例（23.1.4）
- `llvm/lib/Target/AArch64/AArch64InstrFormats.td` 中的 `AddSubImmShift`、`addsub_shifted_imm64`
- `llvm/utils/TableGen/PseudoLoweringEmitter.cpp` 与 `llvm/lib/Target/AArch64/AArch64AsmPrinter.cpp`

> 下一章：[Ch 22 — RISC-V GlobalISel 现状与迁移策略](./22-riscv-globalisel-status.md)
