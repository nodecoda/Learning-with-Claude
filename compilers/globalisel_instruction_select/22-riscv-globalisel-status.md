# Ch 22 — RISC-V GlobalISel 现状与迁移策略

> **本章目标**：以 LLVM 23.1.4 的 RISC-V 源码和 `llvm/test/CodeGen/RISCV/GlobalISel/` 为依据，画出“已经能选什么、还要靠 C++/SelectionDAG 什么、什么时候 fallback”的边界；最后给出一份把新 target 接到 GlobalISel 的检查清单。
>
> **前置**：第 2、5–8、12、16、20、21 章。**环境**：LLVM 23.1.4，AArch64 作对照，RISC-V 64 位为主。**注意**：本章不提供覆盖率/通过率数字；这类数字必须由特定构建、特定测试集和特定选项重新测量。

---

## 22.1 证据边界：只看 23.1.4 能确认的东西

本章的判断来自以下可核对材料：

| 材料 | 用途 |
| --- | --- |
| `RISCVInstructionSelector.cpp` | `select()` 的控制流、C++ 特例、fallback 点 |
| `RISCVLegalizerInfo.cpp` | 哪些 generic op/类型被标成 legal/custom/libcall/lower |
| `RISCVRegisterBankInfo.cpp` | GPR/FPR/VRB 的默认映射与特例 |
| `RISCVCallLowering.cpp` | 参数/返回值/调用支持范围 |
| `RISCVInstrGISel.td` | target generic opcode 与 `GINodeEquiv` |
| `llvm/test/CodeGen/RISCV/GlobalISel/` | 上游实际覆盖到的 MIR/IR 行为 |

本章的 MIR/asm 例子都在本机 LLVM 23.1.4 预编译包上实际跑过；涉及“源码里没有实现”的判断，则以 23.1.4 的源码为准，不外推未来版本。

> 读法：先看 22.3 的支持矩阵，再看 22.4 的控制流，最后用 22.6–22.8 的例子验证。不要把“某个测试存在”误读成“所有同类 IR 都支持”。

---

## 22.2 心智模型：先 TableGen，后 C++，失败就 fallback

RISC-V 的 `select()` 不是“一个大 switch 包打天下”。它的顺序是：

```text
select(MI)
  ├─ preISelLower(MI)          // G_PTR_ADD -> G_ADD, G_PTRMASK -> G_AND
  ├─ 非 pre-isel / G_PHI 特判
  ├─ selectImpl(MI, Coverage)  // TableGen 导入的 matcher
  └─ switch (Opcode)           // C++ 特例
       └─ default: return false
```

`selectImpl()` 先跑，意味着大量常规整数操作并不在 C++ switch 里，而是由 `RISCVInstrGISel.td`、`RISCVInstrInfo.td` 的 pattern 导入后匹配。C++ switch 处理的是：

- 需要手工构造多条指令的扩展/常量；
- 内存、分支、调用、intrinsic 等有额外语义的操作；
- target 特有的 pseudo 或地址计算；
- 以及明确“暂不支持”的 `return false`。

`return false` 的后果不是“再试一条 C++ 规则”，而是整个函数走 GlobalISel 的 fallback 路径：

- `-global-isel-abort=0`（默认）：静默回退到 SelectionDAG；
- `-global-isel-abort=1`：立即报错；
- `-global-isel-abort=2`：保留 remark/warning，继续 fallback。

第 10 章讲过这条契约；本章只关心 RISC-V 在哪些点触发它。

---

## 22.3 支持矩阵：按源码和测试能确认的范围

下表的“选择方式”不是猜测，而是按 23.1.4 的控制流归类；“测试依据”列的是上游 `GlobalISel/` 目录中能对上的文件。

| 类别 | 23.1.4 的状态 | 选择方式 | 测试/源码依据 |
| --- | --- | --- | --- |
| `G_ADD` / `G_SUB` | `sXLen` legal；RV64 的 `s32` custom | 导入的 TableGen pattern（`ADD`/`ADDI`/`ADDW`/`ADDIW`） | `alu-roundtrip.ll`、`add-imm.ll`；Legalizer 的 `{G_ADD,G_SUB}` |
| `G_AND` / `G_OR` / `G_XOR` | `sXLen` legal；RV64 `s32` 走 widen/clamp 到 `sXLen` | 导入 pattern | `alu-roundtrip.ll`、`alu-rv64.mir` |
| shifts | `sXLen` legal；RV64 `s32` custom | 导入 pattern；target pseudo 处理 32 位变体 | `alu-roundtrip.ll`；`G_SLLW/G_SRLW/G_SRAW` |
| `G_MUL` | 仅 `+zmmul` 时 legal；否则 libcall | 导入 pattern / libcall | Legalizer 的 `hasStdExtZmmul()` 分支 |
| `G_SDIV/G_UDIV/G_UREM` | `+m` 时 legal；否则 libcall | 导入 pattern / libcall | Legalizer 的 `hasStdExtM()` 分支、`div-by-constant.ll` |
| `G_CONSTANT` | `p0`、RV32 `s32` legal；RV64 `s64` custom | C++ `materializeImm` | `constant64.mir` |
| `G_ZEXT/G_SEXT` | 多数形态 legal/custom | C++：`ADDIW`、`ADD_UW`、`SEXT_H`、`ZEXT_H`、shift pair | `select()` 的 `G_ZEXT/G_SEXT` case |
| `G_LOAD/G_STORE` | 标量 `s16/s32/p0`，RV64 加 `s64`；带内存大小/对齐条件 | C++ `selectRegImmLoadStoreOp` / 原子路径 | `load-rv64.mir`、`store-rv64.mir`、`atomic-load-store.ll` |
| 原子 load/store | `+a`、`+zalasr`、`+ztso` 等组合 | C++：强于 monotonic 时走 `selectZalasrLoadStoreOp` | `atomic-load-store.ll` |
| `G_BRCOND` | `sXLen` legal | C++ `RISCVCC::getBrCond` | `brcond-rv64.mir` |
| `G_BRINDIRECT` | `p0` legal | C++：`PseudoBRIND` | `brindirect-rv64.mir` |
| `G_SELECT` | `s32/sXLen`（RV64 或 D 时含 `s64`） | C++ `selectSelect`；实际可落到 `Select_GPR_Using_CC_GPR` | `select-rv64.mir` |
| `G_GLOBAL_VALUE` | `p0` legal | C++ `selectAddr`；TLS 直接 `return false` | `global-value64.mir` |
| `G_JUMP_TABLE/G_CONSTANT_POOL` | `p0` legal | C++ `selectAddr` | `global-value64.mir` 相关路径 |
| `G_BRJT` | Legalizer 标为 custom | 主 switch 未单列；不要假设已完整支持 | Legalizer 的 `G_BRJT` |
| calls/returns | 类型检查接受整数、FP、指针、数组、固定 vector 等；仍受 ABI/legalizer 限制 | `RISCVCallLowering` | `calls.ll`、`calllowering-ret.ll` |
| byval / i128 inline asm | 明确 fallback | `lowerCall` 对 byval 返回 false | `riscv-unsupported.ll` |
| FP 标量 | 依 F/D/Zfh 走 legal/custom/libcall | 导入 pattern + C++ `G_FCONSTANT/G_FCMP` | Legalizer 的 FP 分支 |
| RVV | 依 V 与元素类型；部分 load/store/vector intrinsic | 导入 pattern + C++ intrinsic 路径 | `RISCVInstrGISel.td` 的 RVV pseudo、Legalizer 的 RVV 分支 |

这张表最重要的结论不是“支持很多”，而是**支持是分层的**：

1. 整数 ALU 主要靠导入 pattern；
2. 常量、扩展、内存、分支、调用靠 C++；
3. 任何一环 `return false`，整个函数就可能回退到 SelectionDAG；
4. byval、TLS、部分 inline asm 是明确的未支持点，不是“偶发失败”。

### 22.3.1 不要用“测试文件存在”推导覆盖率

`llvm/test/CodeGen/RISCV/GlobalISel/` 里有 `add-imm.ll`、`alu-roundtrip.ll`、`atomic-load-store.ll`、`calls.ll`、`div-by-constant.ll`、`load-rv64.mir` 等文件，只能证明这些形状被测试到。它们不能证明：

- 所有 RV32/RV64 组合都支持；
- 所有扩展组合都支持；
- 所有 ABI/调用约定都支持；
- fallback 率是多少。

要得到覆盖率，必须用固定构建、固定测试集、固定 `-mattr` 组合跑完整 lit，并记录 pass/fail 与 fallback remark；这不是本章能凭空给出的数字。

---

## 22.4 源码走读：五个文件的分工

### 22.4.1 `RISCVInstructionSelector.cpp`

入口是 `RISCVInstructionSelector::select(MachineInstr &MI)`。23.1.4 的顺序可以压缩成：

```cpp
preISelLower(MI);
if (!MI.isPreISelOpcode() || Opc == G_PHI) { /* PHI/COPY/已选指令 */ }
if (selectImpl(MI, *CoverageInfo))
  return true;
switch (Opc) {
  case G_CONSTANT: ...
  case G_ZEXT/G_SEXT: ...
  case G_GLOBAL_VALUE: ...
  case G_BRCOND: ...
  case G_SELECT: ...
  case G_LOAD/G_STORE: ...
  default: return false;
}
```

两个细节值得单独记：

- `preISelLower()` 把 `G_PTR_ADD` 改写成 `G_ADD`、把 `G_PTRMASK` 改写成 `G_AND`，并把结果类型改为 `sXLen`。所以地址计算最终仍走整数 pattern。
- `G_LOAD/G_STORE` 的 C++ 路径先检查值寄存器的 bank 是否为 GPR，再检查地址空间是否为 0；不满足就 `return false`。这不是“没写 pattern”，而是选择器主动拒绝。

### 22.4.2 `RISCVLegalizerInfo.cpp`

Legalizer 决定“进入 selector 之前，操作长什么样”。以 `G_ADD/G_SUB` 为例：

```cpp
getActionDefinitionsBuilder({G_ADD, G_SUB})
    .legalFor({sXLen})
    .legalIf(typeIsLegalIntOrFPVec(0, IntOrFPVecTys, ST))
    .customFor(ST.is64Bit(), {s32})
    .widenScalarToNextPow2(0)
    .clampScalar(0, sXLen, sXLen);
```

这说明：

- RV64 的 `s64` add 是 legal；
- RV32 的 `s32` 是 `sXLen`，也 legal；
- RV64 的 `s32` 是 custom，需要 target 处理 32 位语义；
- 其他宽度会被 widen/clamp。

`G_MUL` 的条件更能说明“ISA 扩展影响 legalizer”：

```cpp
if (ST.hasStdExtZmmul()) {
  getActionDefinitionsBuilder(G_MUL).legalFor({sXLen})...
} else {
  getActionDefinitionsBuilder(G_MUL).libcallFor({sXLen, sDoubleXLen})...
}
```

同一个 IR，在 `+zmmul` 和默认 subtarget 上会走向完全不同的路径；测试必须写清 `-mattr`。

### 22.4.3 `RISCVRegisterBankInfo.cpp`

RISC-V 的 bank 世界比 AArch64 多一维：GPR、FPR、VRB（向量）。23.1.4 的默认规则是：

- 标量 → GPR；
- FP opcode → 对应宽度的 FPR；
- vector → VRB；
- 默认 mapping cost = 1。

特例包括：

- `G_LOAD`：指针在 GPR；vector 走 VRB；原子走 GPR；RV32 的 `s64` 可走 FPR64；如果用途只走 FP，也可走 FPR。
- `G_STORE`：与 load 对称。
- `G_SELECT`：条件始终 GPR；标量结果根据 FP 使用/定义数量决定，`NumFP >= 2` 时走 FPR，否则 GPR。
- `G_FCMP`：结果 GPR，操作数 FPR。

这些特例不是“优化”，而是为了避免在 RV32 无 GPR64 时把 `s64` 放错 bank。迁移新 target 时，先写清这些 bank 约束，再谈 pattern。

### 22.4.4 `RISCVCallLowering.cpp`

`isSupportedArgumentType()` 在类型检查层面接受整数、FP、指针、数组和固定 vector；scalable vector 只在 `IsLowerArgs=true`、有 V、元素类型合法时接受。这不等于所有形状都能成功 lowering。`lowerCall()` 还有一条硬限制：

```cpp
if (AInfo.Flags[0].isByVal())
  return false;
```

这意味着带 `byval` 的调用不会“部分 lowering”，而是直接让调用 lowering 失败，最终走 fallback。`riscv-unsupported.ll` 正是用这个形状验证 remark/warning。

### 22.4.5 `RISCVInstrGISel.td`

这个文件定义 target generic opcode，例如：

```tablegen
def G_SRAW : RISCVGenericInstruction { ... }
def : GINodeEquiv<G_SRAW, riscv_sraw>;

def G_DIVW : RISCVGenericInstruction { ... }
def : GINodeEquiv<G_DIVW, riscv_divw>;
```

还有 `G_BREV8`、`G_CLZW`、`G_CTZW`、`G_FCVT_W_RV64`、`G_FCLASS`、`G_READ_VLENB` 和一组 RVV pseudo。它们不是“所有 RISC-V 指令的替代品”，而是：

- Legalizer/Combiner 需要产生的 target 操作；
- 或 imported pattern 需要匹配的 SDNode 等价物。

常规 `G_ADD` 不在这个文件里，它的 `GINodeEquiv<G_ADD, add>` 在通用 `SelectionDAGCompat.td`；这也是为什么 RISC-V 的 `ADD/ADDI` pattern 能被 GI 直接导入。

---

## 22.5 为什么 RISC-V 比 AArch64 保守

这不是“RISC-V 后端开发者不努力”这种空话，而是几个结构性原因：

1. **ISA 组合更多**：RV32/RV64、F/D/Zfh、M/Zmmul、A/Zalasr、V/Zv* 都会改变 legalizer 和 pattern 的合法条件。AArch64 也有扩展，但 RISC-V 的基础 ISA 组合更碎片化。
2. **调用约定与类型 lowering 更晚成熟**：RISC-V 的 `lowerCall` 明确拒绝 byval；AArch64 的 `AArch64CallLowering.cpp` 有 byval 处理。调用 lowering 不完整，就会把整函数推向 SelectionDAG。
3. **TLS/地址模式仍有 TODO**：`G_GLOBAL_VALUE` 遇到 TLS 直接 `return false`；AArch64 的地址 lowering 覆盖更广。
4. **C++ selector 承担了更多“胶水”**：常量、扩展、内存、分支、intrinsic 都在 switch 里；任何一个形状没覆盖，fallback 就会发生。
5. **测试面更窄**：RISC-V GlobalISel 测试目录能证明的形状有限；AArch64 的 GI 测试和默认使用历史更长，边界更容易被回归测试锁住。

因此，RISC-V 的策略是**渐进式**：先把常见标量 ALU、load/store、分支、简单调用跑通，再用 `abort=2` 的 remark/warning 记录未支持形状，逐步缩小 fallback 面。这是一种工程上合理的保守，不是“功能已经完整”。

---

## 22.6 例子一：同一组 add 立即数，AArch64 与 RISC-V 的边界不同

测试 IR：

```llvm
define i64 @i2047(i64 %a) { %r = add i64 %a, 2047
  ret i64 %r }
define i64 @i2048(i64 %a) { %r = add i64 %a, 2048
  ret i64 %r }
define i64 @i4094(i64 %a) { %r = add i64 %a, 4094
  ret i64 %r }
define i64 @i4095(i64 %a) { %r = add i64 %a, 4095
  ret i64 %r }
define i64 @i4096(i64 %a) { %r = add i64 %a, 4096
  ret i64 %r }
```

### 22.6.1 RISC-V 64 位实际输出

```bash
llc -mtriple=riscv64-linux-gnu -O0 -global-isel \
    -stop-after=instruction-select rv-imm.ll -o rv-imm.mir
```

**【实测】** instruction-select 的关键结果：

```text
2047 -> %2:gpr = ADDI %0, 2047
2048 -> %3:gpr = ADDI %0, 2047; %2:gpr = ADDI %3, 1
4094 -> %3:gpr = ADDI %0, 2047; %2:gpr = ADDI %3, 2047
4095 -> %3:gpr = LUI 1; %1:gpr = ADDI %3, -1; %2:gpr = ADD %0, %1
4096 -> %1:gpr = LUI 1; %2:gpr = ADD %0, %1
```

对应汇编：

```asm
addi    a0, a0, 2047
addi    a0, a0, 2047
addi    a0, a0, 1
addi    a0, a0, 2047
addi    a0, a0, 2047
lui     a1, 1
addi    a1, a1, -1
add     a0, a0, a1
lui     a1, 1
add     a0, a0, a1
```

这里体现了 RISC-V 的 12 位有符号立即数边界：`2048` 不能直接放进 `ADDI`，会拆成 `2047 + 1`；`4095` 也不是“无脑一条 `ADDI`”，而是 `LUI 1; ADDI -1` 再 `ADD`。

### 22.6.2 AArch64 同一 IR 的实际输出

```text
2047 -> ADDXri %0, 2047, 0
2048 -> ADDXri %0, 2048, 0
4094 -> ADDXri %0, 4094, 0
4095 -> ADDXri %0, 4095, 0
4096 -> ADDXri %0, 1, 12
```

对应汇编是 `add x0, x0, #2047` … `add x0, x0, #1, lsl #12`。AArch64 的 12 位无符号立即数 + 可选 `lsl #12` 覆盖了这组值，所以不需要拆成 `LUI + ADD`。

> 对照点：同样是“12 位”，AArch64 的 `addsub_shifted_imm64` 是**无符号 12 位 + 可选左移 12**；RISC-V 的 `ADDI` 是**有符号 12 位**。不要把两者的边界混为一谈。

---

## 22.7 例子二：同一份 IR 的 AArch64 / RISC-V MIR 与 asm

测试 IR（`rv.ll`）：

```llvm
define i64 @add_i64(i64 %a, i64 %b) {
  %r = add i64 %a, %b
  ret i64 %r
}
define i64 @add_i64_42(i64 %a) {
  %r = add i64 %a, 42
  ret i64 %r
}
define i32 @add_i32(i32 %a, i32 %b) {
  %r = add i32 %a, %b
  ret i32 %r
}
define i32 @load_store(ptr %p) {
  %v = load i32, ptr %p, align 4
  %r = add i32 %v, 1
  store i32 %r, ptr %p, align 4
  ret i32 %r
}
define i32 @branch(i32 %a) {
entry:
  %c = icmp eq i32 %a, 0
  br i1 %c, label %yes, label %no
yes:
  ret i32 7
no:
  ret i32 9
}
define i32 @call(i32 %x) {
  %r = call i32 @callee(i32 %x)
  ret i32 %r
}
declare i32 @callee(i32)
```

### 22.7.1 instruction-select 对照

RISC-V：

```text
add_i64:      %2:gpr = ADD %0, %1
add_i64_42:   %2:gpr = ADDI %0, 42
add_i32:      %8:gpr = ADDW %2, %3
              %9:gpr = ADDIW %8, 0
load_store:   %1:gpr = LW %0, 0
              %8:gpr = ADDIW %1, 1
              SW %8, %0, 0
branch:       %9:gpr = ADDIW %1, 0
              BEQ %9, %14, %bb.2
              PseudoBR %bb.3
call:         ADJCALLSTACKDOWN 0, ...
              PseudoCALL target-flags(riscv-call) @callee, ...
              ADJCALLSTACKUP 0, ...
              PseudoRET implicit $x10
```

AArch64：

```text
add_i64:      %2:gpr64 = ADDXrr %0, %1
add_i64_42:   %2:gpr64sp = ADDXri %0, 42, 0
add_i32:      %2:gpr32 = ADDWrr %0, %1
load_store:   %1:gpr32common = LDRWui %0, 0
              %3:gpr32common = ADDWri %1, 1, 0
              STRWui %3, %0, 0
branch:       CBZW %0, %bb.2
              B %bb.3
call:         BL @callee, csr_aarch64_aapcs, ...
```

两者都已经把同一份 IR 选完，`failedISel: false`；差别在指令选择和调用约定：

- RISC-V 用 `ADDW` + `ADDIW 0` 表达 32 位 add 的符号扩展；
- AArch64 直接用 `ADDWrr`，返回路径由 ABI 处理；
- RISC-V 的调用是 `PseudoCALL`，AArch64 是 `BL`；
- 分支在 RISC-V 是 `BEQ + PseudoBR`，AArch64 折叠成 `CBZW + B`。

### 22.7.2 最终汇编对照

RISC-V：

```asm
add     a0, a0, a1
addi    a0, a0, 42
addw    a0, a0, a1
sext.w  a0, a0
mv      a1, a0
lw      a0, 0(a1)
addiw   a0, a0, 1
sw      a0, 0(a1)
beq     a0, a1, .LBB4_1
j       .LBB4_2
call    callee
```

AArch64：

```asm
add     x0, x0, x1
add     x0, x0, #42
add     w0, w0, w1
mov     x8, x0
ldr     w9, [x8]
add     w0, w9, #1
str     w0, [x8]
cbz     w0, .LBB4_1
b       .LBB4_2
bl      callee
```

【实测】分支函数在 `-O0` 下仍会插入栈上的常量 spill（RISC-V 侧更明显），上面的过滤输出省略了 `addi sp, sp, -16`、`sd/ld` 等栈帧指令。不要把这段当成“优化后”的最终代码。

### 22.7.3 这组例子说明了什么

- 同一 IR 在两边都能完成 instruction-select，不代表选择结果相同；
- RISC-V 的 `G_ADD` 走 imported pattern，不靠 C++ switch；
- load/store 走 C++ `selectRegImmLoadStoreOp`，并且只在 GPR bank、地址空间 0 下成立；
- 调用能跑通普通整数参数，不代表 byval、inline asm、TLS 也能跑通。

---

## 22.8 例子三：让 fallback 显形

上游的 `riscv-unsupported.ll` 用 `-global-isel-abort=2` 和 `-pass-remarks-missed='gisel*'` 展示“已知不支持”的形状。核心 IR 是：

```llvm
%byval.class = type { i32 }
declare void @test_byval_arg(ptr byval(%byval.class) %x)

define void @test_byval_param(ptr %x) {
  call void @test_byval_arg(ptr byval(%byval.class) %x)
  ret void
}

define i128 @inline_asm_multi_reg_input(i128 %x) {
  %r = call i128 asm sideeffect "/* $0 $1 */", "=&r,r"(i128 %x)
  ret i128 %r
}
```

运行：

```bash
llc -mtriple=riscv64 -verify-machineinstrs -global-isel \
    -global-isel-abort=2 -pass-remarks-missed='gisel*' \
    riscv-unsupported.ll -o - 2>&1
```

**【实测】** 关键输出（exit code 0）：

```text
remark: ... unable to translate instruction: call
warning: Instruction selection used fallback path for test_byval_param
remark: ... unable to translate instruction: call
warning: Instruction selection used fallback path for inline_asm_multi_reg_input
```

把 `-global-isel-abort=2` 换成 `1`：

```text
LLVM ERROR: unable to translate instruction: call (in function: test_byval_param)
```

**【实测】** exit code 非 0；本机预编译包随后打印 stack dump。这里不粘贴地址或栈帧，只强调：`abort=1` 是硬失败，`abort=2` 才是“记录并继续 fallback”的观察模式。

这也解释了为什么上游测试用 `abort=2`：它既能让 FileCheck 看到 remark/warning，又不会让整个 lit 进程在第一个未支持形状上终止。

---

## 22.9 迁移一个新 target 的检查清单

下面按“从 Legalizer 到 lit”的顺序列；每一项都要有可观察的验收条件，而不是“代码写完就算”。

### 22.9.1 Legalizer

- [ ] 列出目标支持的标量宽度（`s8/s16/s32/s64`、`p0`）和向量形状。
- [ ] 为每个 op 明确 `legal` / `custom` / `libcall` / `lower`，不要留默认路径。
- [ ] 用 feature predicate 区分 ISA 扩展；同一 op 在不同 `-mattr` 下允许不同 action。
- [ ] 为 memory 操作写清大小、对齐、地址空间；不支持的组合要能落到 lower/libcall。
- [ ] 验收：`-stop-after=legalizer` 的 MIR 中没有“打印出来但不知道怎么处理”的 generic op。

### 22.9.2 RegisterBank

- [ ] 定义 GPR/FPR/VRB（或等价）bank 与每个宽度的 mapping。
- [ ] 为 load/store、select、merge/unmerge、FP 转换写特例，避免跨 bank 的隐式 COPY。
- [ ] 检查 RV32 的 `s64`、向量、指针分别落在哪个 bank。
- [ ] 验收：`-stop-after=regbankselect -verify-machineinstrs` 通过，MIR 中没有意外 bank。

### 22.9.3 CallLowering

- [ ] 明确参数/返回支持的类型：整数、FP、指针、数组、结构、固定/可伸缩 vector。
- [ ] 明确 byval、sret、varargs、inreg、swift error 等属性是否支持。
- [ ] 不支持时返回 false，让上层走 fallback；不要“半 lowering”。
- [ ] 验收：`calls.ll`、`calllowering-ret.ll` 级别的 IR 能跑通；未支持形状有 remark。

### 22.9.4 InstructionSelector

- [ ] `select()` 先 `preISelLower()`，再 `selectImpl()`，最后才 switch。
- [ ] 每个 C++ case 都要 `constrainSelectedInstRegOperands()` 或等价约束。
- [ ] 对地址空间、bank、原子序、TLS 等边界显式 `return false`，不要静默生成错误指令。
- [ ] 验收：`-run-pass=instruction-select -verify-machineinstrs` 通过；失败时 `abort=1` 能给出具体 opcode。

### 22.9.5 GINodeEquiv 与 pattern

- [ ] 通用 op 用 `SelectionDAGCompat.td` 的映射；target 特有 op 才在 target 的 `*InstrGISel.td` 定义。
- [ ] pattern 的寄存器类、类型、立即数谓词与 Legalizer 的输出一致。
- [ ] 用 `AddedComplexity` 解决重叠 pattern 时，写测试锁住优先级。
- [ ] 验收：`-stop-after=instruction-select` 能看到预期 opcode，而不是“合法但不同”的替代。

### 22.9.6 pseudo 与展开

- [ ] 单条真实指令：`PseudoInstExpansion`。
- [ ] 多条指令：target 的 `expandPostRAPseudo`。
- [ ] 展开目标不能是 pseudo/codegen-only；操作数名和数量必须对得上。
- [ ] 验收：`-stop-after=instruction-select` 看 pseudo，最终 asm/objdump 看真实指令，编码与基线一致。

### 22.9.7 测试

- [ ] MIR：`-run-pass=instruction-select`，覆盖正常、边界、错误 bank。
- [ ] IR/lit：`-global-isel` 看最终 asm；必要时加 `-verify-machineinstrs`。
- [ ] fallback：`abort=2` + `-pass-remarks-missed='gisel*'`，把 remark/warning 写进 FileCheck。
- [ ] 反汇编/编码：`-show-mc-encoding` 或 `llvm-objdump -d`。
- [ ] 回归：同一测试在 RV32/RV64、关键 `-mattr` 组合下都跑。

### 22.9.8 fallback 策略

| 选项 | 行为 | 适用场景 |
| --- | --- | --- |
| `-global-isel-abort=0` | 静默回退 SelectionDAG | 生产默认；要配合 remark 观察 |
| `-global-isel-abort=1` | 立即报错 | 开发/调试；定位第一个失败点 |
| `-global-isel-abort=2` | remark/warning 后继续 fallback | lit 测试；记录未支持形状 |

迁移时不要只追求“abort=0 能出汇编”。那可能只是 SelectionDAG 在兜底。真正的进度指标是：fallback remark 的数量和原因在减少，且新增 pattern 有测试锁住。

---

## 22.10 踩雷清单

1. **把 `selected: true` 当成 GI 成功**：还要看 `failedISel`；失败时可能已经 fallback。
2. **用 `-global-isel-abort=0` 的汇编证明“GI 支持”**：那可能是 SelectionDAG 输出的。
3. **忽略 `-mattr`**：`G_MUL` 在 `+zmmul` 与默认 subtarget 上走完全不同的 legalizer 路径。
4. **把 RISC-V 的 12 位立即数当成 AArch64 的无符号 12 位**：`ADDI` 是有符号 12 位，边界在 `-2048..2047`。
5. **在 C++ selector 里“补齐”一个本应由 Legalizer 处理的类型**：短期能选，长期会破坏 legalizer/regbank 契约，测试也难写。

---

## 22.11 进阶与练习

**进阶**

- 用 `-pass-remarks-missed='gisel*'` 统计一组 IR 的 fallback 原因，按 opcode/调用形状分类，而不是只看总数。
- 对比 `+m` 与 `+zmmul` 下同一段 `mul` IR 的 legalizer MIR，确认 libcall 与 legal 的分界。
- 给一个未支持的调用形状写最小 MIR，分别用 `abort=0/1/2` 跑，记录三种行为。
- 读 `RISCVInstrGISel.td` 中 `G_SRAW/G_DIVW` 的定义，追踪它们从 Legalizer 到 pattern 的产生链。

**练习**

1. 把 `rv.ll` 的 `add_i32` 改成 `add i64`，比较 RV64 与 AArch64 的 MIR 差异。
2. 给 `load_store` 加 `addrspace(1)`，观察 RISC-V 是否 fallback，并解释源码中的检查点。
3. 用 `+zalasr` 与不加该扩展分别编译 `atomic-load-store.ll` 的一个小例子，对比原子指令。
4. 写一个带 `byval` 的调用，使用 `abort=2` 复现 remark/warning，再用 `abort=1` 观察硬失败。
5. 为 `G_SELECT` 构造 FP 使用占多数的 IR，验证 `NumFP >= 2` 时 bank 选择的变化。

---

## 22.12 重点整理

- RISC-V 的 GI 路径是 **imported pattern 优先，C++ switch 兜底**；`default: return false` 就是 fallback 入口。
- 整数 ALU 主要已能选；常量、扩展、内存、分支、调用、intrinsic 更多依赖 C++。
- `G_LOAD/G_STORE` 只在 GPR bank、地址空间 0 等条件下成立；TLS 明确未实现。
- `byval` 调用、i128 inline asm 是上游测试明确记录的不支持形状。
- 迁移的关键不是“写更多 pattern”，而是把 Legalizer、RegBank、CallLowering、Selector、pseudo 展开和 fallback 测试连成闭环。

## 22.13 自我检核

1. 为什么 `G_ADD` 不在 `RISCVInstructionSelector.cpp` 的 switch 里？
   - 它由 `selectImpl()` 的 imported pattern 处理；C++ switch 只处理需要手工构造的特例。
2. `G_LOAD` 在什么情况下直接 `return false`？
   - 值寄存器不是 GPR bank，或指针地址空间不是 0；此外原子/普通路径还有各自的 opcode 选择。
3. 为什么 `byval` 会触发 fallback？
   - `RISCVCallLowering::lowerCall()` 对 `AInfo.Flags[0].isByVal()` 明确返回 false。
4. `-global-isel-abort=1` 与 `2` 的区别？
   - `1` 立即报错终止；`2` 打印 remark/warning 后继续 fallback。
5. 迁移时最先该写哪个测试？
   - 一个最小 MIR 的 `-run-pass=instruction-select`，先确认 opcode/bank/类型契约，再写完整 IR 的 asm 测试。

## 22.14 延伸阅读

- LLVM GlobalISel — Pipeline：<https://llvm.org/docs/GlobalISel/Pipeline.html>
- LLVM GlobalISel — InstructionSelect：<https://llvm.org/docs/GlobalISel/InstructionSelect.html>
- `llvm/lib/Target/RISCV/GISel/RISCVInstructionSelector.cpp`（23.1.4）
- `llvm/lib/Target/RISCV/GISel/RISCVLegalizerInfo.cpp`、`RISCVRegisterBankInfo.cpp`、`RISCVCallLowering.cpp`
- `llvm/lib/Target/RISCV/RISCVInstrGISel.td`
- `llvm/test/CodeGen/RISCV/GlobalISel/`：`add-imm.ll`、`alu-roundtrip.ll`、`atomic-load-store.ll`、`calls.ll`、`riscv-unsupported.ll`

> 下一章：[Ch 23 — 设计空间与下一步](./23-design-space-next-steps.md)
