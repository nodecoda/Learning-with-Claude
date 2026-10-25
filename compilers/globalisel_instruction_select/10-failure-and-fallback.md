# 第 10 章 失败路径与 fallback

> **本章目标**：理解 InstructionSelect 失败后到底发生了什么：为什么 `selected: true` 和 `failedISel: true` 会同时出现；MachineVerifier 为什么放过仍然含 `G_*` 的函数；`-global-isel-abort=0/1/2` 分别改变哪一步；以及怎样把“无 pattern”“类型/bank 不匹配”“不能 constrain regclass”“Legalizer 失败”区分开。
>
> **前置**：第 2 章的选择契约、第 4 章的 Legalizer/RegBankSelect、第 9 章的嵌套匹配。**环境**：LLVM 23.1.4，AArch64，预编译 `llc`。

---

## 10.1 失败不是抛异常：两个属性同时为真

`InstructionSelect` 在 pass 元数据里声明自己“会把函数标记为 selected”：

```cpp
MachineFunctionProperties getSetProperties() const override {
  return MachineFunctionProperties().setSelected();
}
```

而 `MachineFunctionPass` 的公共入口在 `runOnMachineFunction` 返回之后，**无条件**执行：

```cpp
MFProps.set(SetProperties);
return RV;
```

注意它不看 `RV`。即使 `InstructionSelect::runOnMachineFunction` 中途失败并返回 `false`，`selected` 仍会被置为 true。这就是第一个反直觉点。

失败路径本身则走 `reportGISelFailure`。在 `Utils.cpp`/`Utils2.cpp` 里：

```cpp
void llvm::reportGISelFailure(MachineFunction &MF,
                              MachineOptimizationRemarkEmitter &MORE,
                              MachineOptimizationRemarkMissed &R) {
  MF.getProperties().setFailedISel();
  reportGISelDiagnostic(DS_Error, MF, MORE, R);
}
```

它先把 `failedISel` 置 true，再决定是否把诊断变成 fatal error。因此一次选择失败会留下：

```yaml
selected:        true
failedISel:      true
```

实跑 `llvm.eh.dwarf.cfa`（AArch64 23.1.4 没有对应 pattern），用 `-stop-after=instruction-select` 观察：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel \
    -stop-after=instruction-select eh_dwarf_cfa.ll -o -
```

MIR 的关键部分是：

```yaml
regBankSelected: true
selected:        true
failedISel:      true
body:             |
  bb.1 (%ir-block.0):
    %0:gpr(i32) = COPY $w0
    %1:gpr(p0) = G_INTRINSIC_W_SIDE_EFFECTS intrinsic(@llvm.eh.dwarf.cfa), %0(i32)
    $x0 = COPY %1(p0)
    RET_ReallyLR implicit $x0
```

`selected: true` 表示“这个 pass 已经跑过并把函数标记为 selected”，不表示“每条指令都选成功了”。判断成功必须同时看两个属性：`selected == true && failedISel == false`。第 2 章已经强调过这一点，本章解释它为什么会这样。

---

## 10.2 MachineVerifier 为什么放过含 `G_*` 的函数

正常的选择后函数不应再含 generic 指令。MachineVerifier 里有：

```cpp
void MachineVerifier::verifyPreISelGenericInstruction(const MachineInstr *MI) {
  if (isFunctionSelected)
    report("Unexpected generic instruction in a Selected function", MI);
  ...
}
```

以及检查 generic vreg 的路径：

```cpp
if (isFunctionSelected) {
  report("Generic virtual register invalid in a Selected function",
         MO, MONum);
  return;
}
```

但 verifier 在函数入口先做了一个提前返回：

```cpp
const MachineFunctionProperties &Props = MF.getProperties();
const bool isFunctionFailedISel = Props.hasFailedISel();

// If we're mid-GlobalISel and we already triggered the fallback path then
// it's expected that the MIR is somewhat broken but that's ok since we'll
// reset it and clear the FailedISel attribute in ResetMachineFunctions.
if (isFunctionFailedISel)
  return true;
```

所以 `selected: true, failedISel: true` 的函数会被整体跳过：它本来就是 fallback 前的半成品，后续会被重置，SelectionDAG 会重新生成机器指令。**这不是 verifier 漏检，而是有意的契约。**

### 10.2.1 两个真实验证结果

对 10.1 的 fallback MIR 直接跑：

```console
llc -mtriple=aarch64-linux-gnu -run-pass=verify -verify-machineinstrs fallback.mir -o NUL
```

因为 `failedISel: true`，verifier 提前返回，退出码为 0，没有 `Bad machine code`。

把同一份 MIR 的 `failedISel` 手工改成 `false`，再跑同一条命令：

```text
*** Bad machine code: Unexpected generic instruction in a Selected function ***
*** Bad machine code: Generic virtual register invalid in a Selected function ***
LLVM ERROR: Found 5 machine code errors.
```

第二个 MIR 只含一条 `G_INTRINSIC_W_SIDE_EFFECTS intrinsic(@llvm.eh.unwind.init)` 和 `RET_ReallyLR`，则只报：

```text
*** Bad machine code: Unexpected generic instruction in a Selected function ***
LLVM ERROR: Found 1 machine code errors.
```

这两个输出说明：**错误不是“selected 函数不能含 generic”这条规则不存在，而是 `failedISel` 让 verifier 不去执行它。**

---

## 10.3 fallback 的条件与 `-global-isel-abort`

### 10.3.1 每函数、由 `failedISel` 触发

GlobalISel 是 per-function 的流水线。某个函数在 IRTranslator、Legalizer、RegBankSelect 或 InstructionSelect 中失败，都会设置 `failedISel`；管线随后丢弃该函数的 GlobalISel 结果，并对这个函数重新跑 SelectionDAG。其他函数不受影响。

`-stop-after=instruction-select` 只跑到选择阶段，因此你看到的是“失败后的半成品 MIR”，不会看到 SelectionDAG 的最终汇编。把 `-stop-after` 去掉，才会看到 fallback 后的结果：

```asm
f:
  stp     x29, x30, [sp, #-16]!
  mov     x29, sp
  ...
  mov     x8, x29
  add     x0, x8, w0, sxtw
  ldp     x29, x30, [sp], #16
  ret
```

汇编是正确的，但这条路径已经和 GlobalISel 无关。**fallback 不是“GlobalISel 选择成功”，而是“GlobalISel 放弃，交给另一套选择器”。**

### 10.3.2 23.1.4 的准确语义：0、1、2

不要凭记忆写 `-global-isel-abort`。本机 `llc --help-hidden` 的输出是：

```text
--global-isel-abort=<value>  - Enable abort calls when "global" instruction
                               selection fails to lower/select an instruction
  =0                         -   Disable the abort
  =1                         -   Enable the abort
  =2                         -   Disable the abort but emit a diagnostic on failure
```

三档的行为：

| 取值 | 选择失败时 | 是否 fallback | 典型输出 |
| --- | --- | --- | --- |
| `0` | 静默设置 `failedISel` | 是 | 只有最终汇编，没有警告 |
| `1` | `reportFatalUsageError` | 否，进程终止 | `LLVM ERROR: cannot select: ...` |
| `2` | 设置 `failedISel`，并发出诊断 | 是 | `warning: Instruction selection used fallback path for f` |

`1` 的 fatal 判断在 `reportGISelDiagnostic` 里：

```cpp
bool IsGlobalISelAbortEnabled =
    MF.getTarget().Options.GlobalISelAbort == GlobalISelAbortMode::Enable;
bool IsFatal = Severity == DS_Error && IsGlobalISelAbortEnabled;
...
if (IsFatal)
  reportFatalUsageError(Twine(R.getMsg()));
else
  MORE.emit(R);
```

`2` 不是“更严格的 abort”，而是“不要 abort，但把 fallback 说出来”。这一点和某些旧版/其他工具的 `2/3` 语义不同，必须以本机帮助为准。

### 10.3.3 同一个 IR 的三种输出

IR：

```llvm
target triple = "aarch64-unknown-linux-gnu"
declare ptr @llvm.eh.dwarf.cfa(i32)
define ptr @f(i32 %o) {
  %r = call ptr @llvm.eh.dwarf.cfa(i32 %o)
  ret ptr %r
}
```

只写 `-global-isel` 时默认等价于 abort=0：成功产出汇编，退出码 0，没有诊断。`-global-isel-abort=1`：

```text
LLVM ERROR: cannot select: %1:gpr(p0) = G_INTRINSIC_W_SIDE_EFFECTS intrinsic(@llvm.eh.dwarf.cfa), %0:gpr(i32) (in function: f)
...
Running pass 'InstructionSelect' on function '@f'
```

退出码 1。`-global-isel-abort=2`：仍然产出同样的汇编，退出码 0，但 stderr 多一行：

```text
warning: Instruction selection used fallback path for f
```

注意 warning 来自管线层的 fallback，而不是 `reportGISelFailure` 的 fatal 分支；`=2` 只是让 `reportGISelDiagnostic` 走 `MORE.emit(R)`，再由管线报告“用了 fallback”。

---

## 10.4 失败来源分类

“cannot select”只是最外层的一句话。真正的根因要回到 RBS MIR 和源码里找。下面按常见程度分类。

### 10.4.1 没有 pattern / 类型或 bank 不匹配

这两类经常表现成同一句 `cannot select`。例如把 `fadd float` 的 vreg 故意标成 `gpr`：

```yaml
registers:
  - { id: 0, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 1, class: gpr, preferred-register: '', flags: [  ] }
  - { id: 2, class: gpr, preferred-register: '', flags: [  ] }
body:             |
  bb.1.entry:
    liveins: $s0, $s1
    %0:gpr(f32) = COPY $s0
    %1:gpr(f32) = COPY $s1
    %2:gpr(f32) = G_FADD %0, %1
    $s0 = COPY %2(f32)
    RET_ReallyLR implicit $s0
```

运行：

```console
llc -mtriple=aarch64-linux-gnu -O0 -run-pass=instruction-select \
    -pass-remarks-missed=gisel-select fadd-wrongbank.mir -o -
```

得到：

```text
remark: <unknown>:0:0: cannot select: %2:gpr(f32) = G_FADD %0:gpr, %1:gpr (in function: fadd)
```

MIR 里是：

```yaml
selected:        true
failedISel:      true
...
%2:gpr(f32) = G_FADD %0, %1
```

这里 `G_FADD` 本身有 pattern，但 pattern 检查的是 FPR bank（`fpr(f32)`）；`gpr(f32)` 过不了 `GIM_CheckRegBankForClass`，于是整条规则被拒绝。**先看 RBS 的 bank 和 LLT，再谈“是不是没写 pattern”。**

真正的“没有 pattern”可以看 `llvm.eh.dwarf.cfa`：RBS 是合法的 `gpr(p0) = G_INTRINSIC_W_SIDE_EFFECTS ...`，但没有规则能选它，所以同样报 `cannot select`。区别在于：前者是“有规则但 operands 不匹配”，后者是“根本没有规则”。

### 10.4.2 未处理的 `G_*`

AArch64 并不是每个 generic opcode 都有规则。`llvm.coro.id` 会产生一个选择器没处理的 `G_INTRINSIC_W_SIDE_EFFECTS`：

```llvm
target triple = "aarch64-unknown-linux-gnu"
declare token @llvm.coro.id(i32, ptr, ptr, ptr)
define void @f() {
  %t = call token @llvm.coro.id(i32 0, ptr null, ptr null, ptr null)
  ret void
}
```

`-global-isel-abort=1` 实跑：

```text
LLVM ERROR: cannot select: %0:gpr(s0) = G_INTRINSIC_W_SIDE_EFFECTS intrinsic(@llvm.coro.id), %1:gpr(i32), %2:gpr(p0), %2:gpr(p0), %2:gpr(p0) (in function: f)
...
Running pass 'InstructionSelect' on function '@f'
```

`=0` 时同一份 IR 正常产出汇编（这里 `coro.id` 的结果未被使用，最终只剩 `ret`），`=2` 时打印 fallback warning。**“能 fallback”不代表“这条 G_* 被支持了”。**

### 10.4.3 无法 constrain regclass

选择器生成目标指令后，要把 vreg 约束到操作数要求的寄存器类。`constrainOperandRegClass` 通常通过插入 COPY 或新建 vreg 解决；如果寄存器类/银行根本不兼容，它不会神奇地“转换类型”。

23.1.4 的选择后检查（`#ifndef NDEBUG`）写得很直接：

```cpp
const TargetRegisterClass *RC = MRI.getRegClassOrNull(VReg);
if (!RC) {
  reportGISelFailure(MF, MORE, "gisel-select",
                     "VReg has no regclass after selection", *MI);
  return false;
}
```

还有一条尺寸检查：

```cpp
if (Ty.isValid() &&
    TypeSize::isKnownGT(Ty.getSizeInBits(), TRI.getRegSizeInBits(*RC))) {
  reportGISelFailure(
      MF, MORE, "gisel-select",
      "VReg's low-level type and register class have different sizes", *MI);
  return false;
}
```

**源码阅读结论（未构造实跑复现）**：这类失败通常不会以 `cannot select` 出现，而是以 `VReg has no regclass after selection` 或 `VReg's low-level type and register class have different sizes` 出现。若在 release 构建里跳过这些检查，问题会推迟到 verifier 或汇编器，表现为“选了指令但寄存器不合法”。

### 10.4.4 选择器试图插入基本块

InstructionSelect 的契约是不改变 CFG。23.1.4 在 `#ifndef NDEBUG` 里保存了入口块数：

```cpp
const size_t NumBlocks = MF.size();
...
if (MF.size() != NumBlocks) {
  MachineOptimizationRemarkMissed R("gisel-select", "GISelFailure",
                                    MF.getFunction().getSubprogram(),
                                    /*MBB=*/nullptr);
  R << "inserting blocks is not supported yet";
  reportGISelFailure(MF, MORE, R);
  return false;
}
```

**源码阅读结论（未构造实跑复现）**：目标 selector 若调用了会创建 MachineBasicBlock 的 API，会得到 `inserting blocks is not supported yet`，随后 fallback。正确做法是把 CFG 变换放到 Legalizer/RegBankSelect 或更早的 IR pass，而不是在选择阶段做。

### 10.4.5 verifier 失败

verifier 失败和选择失败是两条不同的路径：

- 选择失败：设置 `failedISel`，verifier 提前返回，管线 fallback。
- verifier 失败：没有 `failedISel`，verifier 报 `Bad machine code`，进程通常直接失败。

10.2.1 的 `bad-selected.mir` 就是后者。它不会 fallback，因为 verifier 认为“函数已经被标记为 selected，却仍有 generic 指令”，这是编译器 bug，不是可恢复的选择失败。

### 10.4.6 C++ selector 崩溃

如果 target 的 `select` 实现里触发了 `llvm_unreachable`、assert、空指针或越界，进程会直接崩溃或报 `LLVM ERROR`，**不会走 `reportGISelFailure` 的 fallback 路径**。本机预编译包不是 assertions 构建，本章没有复现这类崩溃；定位时需要在 assertions 构建下用 `-debug-only=instruction-select` 看最后匹配的规则，再用调试器抓栈。

### 10.4.7 Legalizer 失败要单独归类

`G_UMULO i128` 在 AArch64 上：

```text
LLVM ERROR: unable to legalize instruction: %6:_(i128), %7:_(i1) = G_UMULO %0:_, %1:_ (in function: f)
...
Running pass 'Legalizer' on function '@f'
```

`llvm.vscale`：

```text
LLVM ERROR: unable to legalize instruction: %0:_(i64) = G_VSCALE i64 1 (in function: f)
...
Running pass 'Legalizer' on function '@f'
```

这两条不是 InstructionSelect 失败。看到 `Running pass 'Legalizer'`，应去补 Legalizer 规则或 `LegalizerInfo`，而不是在 `AArch64InstructionSelector.cpp` 里找 pattern。**先确认失败发生在哪个 pass，再决定改哪一层。**

---

## 10.5 `reportGISelFailure` 与诊断工具

`reportGISelFailure` 是一个重载家族：一个接收已经构造好的 `MachineOptimizationRemarkMissed`，一个接收 `PassName + Msg + MI`。后者会：

```cpp
MachineOptimizationRemarkMissed R(PassName, "GISelFailure: ",
                                  MI.getDebugLoc(), MI.getParent());
R << Msg;
// Printing MI is expensive; only do it if expensive remarks are enabled.
if (MF.getTarget().Options.GlobalISelAbort == GlobalISelAbortMode::Enable ||
    MORE.allowExtraAnalysis(PassName))
  R << ": " << ore::MNV("Inst", MI);
reportGISelFailure(MF, MORE, R);
```

因此：

- `-pass-remarks-missed=gisel-select` 能看到 `cannot select: <MI>`；
- `-global-isel-abort=1` 会打印完整 MI 并 fatal；
- 普通 `=0` 通常不打印，因为 remark 没有被请求。

实跑 `-pass-remarks-missed=gisel-select`：

```text
remark: <unknown>:0:0: cannot select: %1:gpr(p0) = G_INTRINSIC_W_SIDE_EFFECTS intrinsic(@llvm.eh.dwarf.cfa), %0:gpr(i32) (in function: f)
```

退出码仍是 0，因为 remark 不是 error。

### 10.5.1 `-stats` 与 fallback 统计

`llc --help` 对 `-stats` 的说明是：

```text
Enable statistics output from program (available with Asserts)
```

本机预编译包是 Optimized build（非 assertions）。实跑：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=2 \
    -stats eh_dwarf_cfa.ll -o NUL
```

只看到 fallback warning，没有统计输出。**因此本章不声称存在可用的“GISel fallback 计数器”。** 可用的替代证据是：

- `-pass-remarks-missed=gisel-select` 的 `cannot select`；
- `-global-isel-abort=2` 的 fallback warning；
- `-stop-after=instruction-select` MIR 里的 `failedISel: true`；
- 最终汇编与 `-stop-after=instruction-select` 的差异。

---

## 10.6 设计取舍：fallback 不是失败，但也不能长期依赖

### 10.6.1 为什么说它不是失败

从用户角度看，`-global-isel-abort=0` 的程序通常仍能生成正确汇编；SelectionDAG 是成熟的 fallback。但它不是万能兜底：`llvm.vscale` 在 abort=0 时仍会在 SelectionDAG 报 `Cannot select: ... vscale`。GlobalISel 的设计目标之一是渐进迁移：某些 opcode/目标特性没覆盖时，允许函数级回退。

从编译器角度看，它确实是失败：`failedISel` 被设置，GlobalISel 的结果被丢弃，选择阶段的工作白做。**“用户可见的成功”和“GlobalISel 的成功”是两件事。**

### 10.6.2 为什么不能长期依赖

- **性能**：fallback 函数走的是另一套选择器，GlobalISel 的收益（编译时间、可维护性、目标特性）在这个函数上全部消失。
- **覆盖率**：fallback 会掩盖缺失的 pattern。一个函数 fallback 后，后续的 GlobalISel 组合/优化也看不到它。
- **可诊断性与代码质量**：用户只看到“能编译”，维护者却不知道某条新指令一直没有 GI 支持；两套路径还可能产生不同的指令选择/调度结果，让测试和性能分析分裂。

### 10.6.3 该补 pattern 还是接受 fallback

先定位失败阶段，再按下面决策：

| 现象 | 优先动作 |
| --- | --- |
| RBS 里已是合法 generic，但 ISel 报 `cannot select` | 补 TableGen pattern；若涉及地址/复杂操作数，补 ComplexPattern |
| RBS 的 bank/LLT 明显不对 | 修 RegBankSelect 或约束规则，不要先写 pattern |
| 失败在 Legalizer | 补 `LegalizerInfo`/legalizer 回调，ISel 不背这个锅 |
| 失败在 verifier（无 `failedISel`） | 当 bug 修；不要用 fallback 掩盖 |
| 只在冷门 intrinsic/特性上失败 | 可短期接受 fallback，但加 remark 测试和 issue |
| 热点函数、核心 intrinsic、常见类型 | 不应接受长期 fallback；补 pattern 并加 MIR 测试 |

判断“该不该接受”的经验法则：**先问这个函数是否在热路径、这个 opcode 是否常见、fallback 是否改变了性能关键指令。** 如果三者都是“是”，fallback 只是临时止血。

---

## 10.7 踩雷清单

1. **只看 `selected`。** 失败时它仍为 true；必须同时看 `failedISel`。
2. **把 `-global-isel-abort=2` 当成“更严格的 abort”。** 23.1.4 的 `2` 是“不 abort，但打印诊断”；真正 fatal 的是 `1`。
3. **把 `cannot select` 当成“没写 pattern”。** 先看 RBS 的 bank/LLT；`gpr(f32)` 上的 `G_FADD` 是 operands 不匹配，不是没有 `fadd` 规则。
4. **把 verifier 报错当成可 fallback 的选择失败。** 没有 `failedISel` 的 `Bad machine code` 是 bug，不会自动回退。
5. **把 Legalizer 失败归到 InstructionSelect。** 看 `Running pass 'Legalizer'`；改错文件会浪费大量时间。

---

## 10.8 进阶：把失败变成可观测性

- **给每个 fallback 加 remark 测试**：用 `-pass-remarks-missed=gisel-select` 的 `cannot select` 文本锁定“确实是这条指令没选”。
- **区分“选择失败”和“验证失败”**：前者 `failedISel: true`，后者 `Bad machine code`；两者的 CI 断言应不同。
- **在 RBS 阶段做断言**：如果某条 generic 指令本应被 Legalizer 消掉，RBS 还在就不应继续往下跑。
- **统计 fallback 率**：非 assertions 构建没有 `-stats` 时，可以用 `-global-isel-abort=2` 的 warning 计数（按函数）作为粗粒度指标；更精细的统计需要 assertions 构建或自定义 remark。

---

## 10.9 动手练习

### 练习 10.1 观察双 true

用 10.1 的 `eh.dwarf.cfa`，分别跑：

```console
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -stop-after=instruction-select in.ll -o -
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=1 in.ll -o NUL
llc -mtriple=aarch64-linux-gnu -O0 -global-isel -global-isel-abort=2 in.ll -o NUL
```

记录三次的 `selected`/`failedISel`、退出码和 stderr。解释为什么 `=1` 看不到 MIR。

### 练习 10.2 制造 bank 不匹配

把 `fadd float` 的 RBS MIR 中三个 vreg 的 `class: fpr` 改成 `class: gpr`，保留 `f32` 类型。用 `-run-pass=instruction-select -pass-remarks-missed=gisel-select` 跑，确认 remark 文本和 `failedISel`。再改回 `fpr`，确认成功。

### 练习 10.3 把 verifier 放回来

把 10.1 fallback MIR 的 `failedISel: true` 改成 `false`，跑 `-run-pass=verify -verify-machineinstrs`。记录两条 `Bad machine code` 文本，并解释为什么这时不会 fallback。

### 练习 10.4 区分 Legalizer 与 ISel

用 `llvm.umul.with.overflow.i128` 和 `llvm.vscale.i64` 各跑一次 `-global-isel-abort=1`，记录 `Running pass` 行。说明应该改哪个文件，以及为什么不应在 `AArch64InstructionSelector.cpp` 里找答案。

### 练习 10.5 设计一个 fallback 断言

为一个你关心的 intrinsic 写一个 lit 测试：`-global-isel-abort=1` 时检查 `cannot select`，`-global-isel-abort=2` 时检查 fallback warning。思考：如果未来补了 pattern，这个测试应该如何更新？

---

## 10.10 重点整理

- `selected` 由 pass 元数据无条件设置；失败时它仍为 true。成功判据是 `selected && !failedISel`。
- `failedISel` 由 `reportGISelFailure` 设置；verifier 看到它会提前返回，不检查 generic 指令。
- `-global-isel-abort` 在 23.1.4 有 0/1/2 三档：0 静默 fallback，1 fatal，2 诊断后 fallback。
- `cannot select` 可能来自无 pattern、bank/LLT 不匹配、未处理 `G_*` 或 constrain 失败；必须回到 RBS MIR 和源码区分。
- `VReg has no regclass after selection`、`inserting blocks is not supported yet` 是选择后检查的诊断；本章只做了源码核对，未构造实跑复现。
- Legalizer 失败与 ISel 失败是不同阶段；看 `Running pass` 再决定改哪一层。
- fallback 让用户得到正确汇编，但不等于 GlobalISel 支持了该指令；热点路径不应长期依赖。

---

## 10.11 自我检核

1. 为什么 `selected: true` 不能单独证明选择成功？`MFProps.set(SetProperties)` 在什么位置执行？
2. `reportGISelFailure` 做了哪两件事？顺序重要吗？
3. MachineVerifier 在什么条件下跳过 generic 检查？如果没有这个提前返回，fallback MIR 会报什么？
4. `-global-isel-abort=1` 和 `=2` 的核心区别是什么？各自是否 fallback？
5. 看到 `cannot select` 时，如何用 RBS MIR 区分“没 pattern”和“bank 不匹配”？
6. 为什么 `VReg has no regclass after selection` 更可能出现在选择后检查，而不是 matcher 的拒绝路径？
7. 什么时候可以接受 fallback？给出三个判断维度。

---

## 10.12 延伸阅读

- **`llvm/lib/CodeGen/GlobalISel/Utils.cpp`** — `reportGISelFailure`、`reportGISelDiagnostic`、`reportGISelWarning`。重点看 `IsFatal` 如何依赖 `GlobalISelAbortMode::Enable`。
- **`llvm/lib/CodeGen/GlobalISel/InstructionSelect.cpp`** — 选择主循环、`machineFunctionIsIllegal`、`VReg has no regclass after selection`、`inserting blocks is not supported yet`。
- **`llvm/lib/CodeGen/MachineVerifier.cpp`** — `isFunctionFailedISel` 的提前返回，以及 `verifyPreISelGenericInstruction` 的两条报错。
- **`llvm/lib/CodeGen/MachineFunctionPass.cpp`** — `MFProps.set(SetProperties)` 为什么无条件执行；这是“双 true”现象的根。
- **`llvm/lib/Target/AArch64/AArch64InstructionSelector.cpp`** — 目标 selector 的失败返回和 `constrainOperandRegClass` 调用点；对照 RBS MIR 读。

---

## 下一章

[第 11 章 ComplexPattern 迁移：从 SDNode 匹配到 GIComplexOperandMatcher](./11-complex-pattern-migration.md) —— 当失败原因不是“少一条简单 pattern”，而是地址、立即数或复杂操作数无法直接表达时，下一章拆开 `GIM_CheckComplexPattern` 和 `GIComplexOperandMatcher` 的契约。
